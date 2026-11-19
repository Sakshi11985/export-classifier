"""Model interface the rest of the system depends on (A5, L5, D4).

LLMClient.chat() is the ONLY way agents call a model. It provides:
  * a timeout on every call,
  * bounded retries with exponential backoff + jitter (429/5xx/timeouts only),
  * a circuit breaker whose state is shared by all workers through Redis,
  * a defined fallback path:  primary provider -> optional fallback provider -> deterministic
    offline policy (no model). Any run that used a step of the fallback path is marked
    `degraded` and can never be returned as clear-for-filing,
  * per-call token and cost accounting.

Providers: `openai_compat` (any OpenAI-compatible /chat/completions endpoint: a hosted free tier,
Ollama, or the bundled llm-stub) and `offline` (deterministic policy, see offline.py)."""
from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field

import httpx

from .. import metrics
from ..config import Settings
from ..logging_setup import get_logger
from .offline import OfflinePolicy

log = get_logger("llm")


class LLMError(Exception):
    def __init__(self, msg: str, retryable: bool = False, status: int | None = None):
        super().__init__(msg)
        self.retryable = retryable
        self.status = status


class CircuitOpenError(LLMError):
    pass


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_usage: bool = False
    provider: str = ""
    model: str = ""
    latency_ms: float = 0.0
    retries: int = 0
    fallback_used: str | None = None
    cost_usd: float = 0.0


class CircuitBreaker:
    """closed -> open (after N failures within a window) -> half-open (after recovery time, one probe)."""

    def __init__(self, s: Settings, redis_client=None, name: str = "llm"):
        self.s = s
        self.r = redis_client  # async redis or None
        self.name = name
        self._fails: list[float] = []
        self._open_until = 0.0
        self._probe_taken = False

    async def _get_open_until(self) -> float:
        if self.r is None:
            return self._open_until
        try:
            v = await self.r.get(f"cb:{self.name}:open_until")
            return float(v) if v else 0.0
        except Exception:
            return self._open_until

    async def state(self) -> str:
        ou = await self._get_open_until()
        now = time.time()
        if ou and now < ou:
            st = "open"
        elif ou and now >= ou:
            st = "half-open"
        else:
            st = "closed"
        metrics.breaker_state.set({"closed": 0, "half-open": 1, "open": 2}[st])
        return st

    async def before_call(self) -> None:
        st = await self.state()
        if st == "open":
            raise CircuitOpenError("circuit breaker open: provider marked unavailable", retryable=False)
        if st == "half-open":
            # allow exactly one probe through
            if self.r is not None:
                try:
                    got = await self.r.set(f"cb:{self.name}:probe", "1", nx=True, ex=self.s.cb_recovery_s)
                except Exception:
                    got = True
                if not got:
                    raise CircuitOpenError("circuit breaker half-open: probe already in flight")
            else:
                if self._probe_taken:
                    raise CircuitOpenError("circuit breaker half-open: probe already in flight")
                self._probe_taken = True

    async def on_success(self) -> None:
        prev = await self.state()
        self._fails.clear()
        self._open_until = 0.0
        self._probe_taken = False
        if self.r is not None:
            try:
                await self.r.delete(f"cb:{self.name}:open_until", f"cb:{self.name}:fails", f"cb:{self.name}:probe")
            except Exception:
                pass
        if prev != "closed":
            metrics.breaker_transitions.labels("closed").inc()
            log.info("circuit breaker closed", extra={"breaker": self.name})
        metrics.breaker_state.set(0)

    async def on_failure(self) -> None:
        now = time.time()
        prev = await self.state()
        if self.r is not None:
            try:
                n = await self.r.incr(f"cb:{self.name}:fails")
                if n == 1:
                    await self.r.expire(f"cb:{self.name}:fails", self.s.cb_window_s)
            except Exception:
                n = len(self._fails) + 1
        else:
            self._fails = [t for t in self._fails if now - t < self.s.cb_window_s] + [now]
            n = len(self._fails)
        if prev == "half-open" or n >= self.s.cb_failure_threshold:
            until = now + self.s.cb_recovery_s
            self._open_until = until
            self._probe_taken = False
            if self.r is not None:
                try:
                    await self.r.set(f"cb:{self.name}:open_until", str(until), ex=self.s.cb_recovery_s * 3)
                    await self.r.delete(f"cb:{self.name}:probe", f"cb:{self.name}:fails")
                except Exception:
                    pass
            metrics.breaker_transitions.labels("open").inc()
            metrics.breaker_state.set(2)
            log.warning("circuit breaker OPEN", extra={"breaker": self.name, "recovery_s": self.s.cb_recovery_s})


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class OpenAICompatProvider:
    def __init__(self, base_url: str, api_key: str, model: str, timeout_s: float):
        if not base_url or not model:
            raise LLMError("LLM_BASE_URL and a model name must be configured for provider openai_compat")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=min(5.0, timeout_s)))

    async def chat(self, messages: list[dict], tools: list[dict] | None) -> LLMResponse:
        body = {"model": self.model, "messages": messages, "temperature": 0}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            resp = await self._client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise LLMError(f"transport error: {type(e).__name__}", retryable=True) from e
        if resp.status_code == 429 or resp.status_code >= 500:
            raise LLMError(f"provider returned HTTP {resp.status_code}", retryable=True, status=resp.status_code)
        if resp.status_code >= 400:
            raise LLMError(f"provider rejected the request: HTTP {resp.status_code} {resp.text[:200]}",
                           retryable=False, status=resp.status_code)
        try:
            data = resp.json()
            msg = data["choices"][0]["message"]
        except Exception as e:  # noqa: BLE001
            raise LLMError("malformed provider response", retryable=True) from e
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_invalid_json": fn.get("arguments")}
            calls.append({"id": tc.get("id") or f"call_{random.randint(1, 10**9)}", "name": fn.get("name"), "arguments": args})
        usage = data.get("usage") or {}
        est = not usage
        pt = usage.get("prompt_tokens") or estimate_tokens(json.dumps(messages))
        ct = usage.get("completion_tokens") or estimate_tokens((msg.get("content") or "") + json.dumps(calls))
        return LLMResponse(msg.get("content"), calls, pt, ct, est, "openai_compat", self.model)

    async def aclose(self):
        await self._client.aclose()


class LLMClient:
    def __init__(self, s: Settings, redis_client=None):
        self.s = s
        self.breaker = CircuitBreaker(s, redis_client)
        self.offline = OfflinePolicy()
        self._primary = None
        self._fallback = None
        if s.llm_provider == "openai_compat":
            self._primary = {}  # created lazily per model (classifier / verifier)
        elif s.llm_provider != "offline":
            raise LLMError(f"unknown LLM_PROVIDER {s.llm_provider!r}")

    @property
    def provider_name(self) -> str:
        return self.s.llm_provider

    def _provider_for(self, role: str, fallback: bool = False) -> OpenAICompatProvider:
        s = self.s
        if fallback:
            if self._fallback is None:
                self._fallback = OpenAICompatProvider(s.llm_fallback_base_url, s.llm_fallback_api_key,
                                                      s.llm_fallback_model, s.llm_timeout_s)
            return self._fallback
        model = s.llm_model_verifier if role == "verifier" and s.llm_model_verifier else s.llm_model_classifier
        if model not in self._primary:
            self._primary[model] = OpenAICompatProvider(s.llm_base_url, s.llm_api_key, model, s.llm_timeout_s)
        return self._primary[model]

    def cost(self, pt: int, ct: int) -> float:
        return pt / 1000 * self.s.price_prompt_per_1k + ct / 1000 * self.s.price_completion_per_1k

    async def chat(self, role: str, step: str, messages: list[dict], tools: list[dict] | None, ctx) -> LLMResponse:
        """role: classifier | verifier. step: label used for per-step cost attribution."""
        s = self.s
        if s.llm_provider == "offline":
            return await self._offline_call(role, step, messages, tools, ctx, degraded=False)
        # ---------------- primary provider with retries + breaker
        last_err: Exception | None = None
        retries = 0
        for attempt in range(s.llm_max_retries + 1):
            try:
                await self.breaker.before_call()
            except CircuitOpenError as e:
                last_err = e
                break
            t0 = time.perf_counter()
            try:
                remaining = ctx.time_left()
                if remaining <= 0:
                    raise LLMError("run wall-clock budget exhausted", retryable=False)
                resp = await asyncio.wait_for(self._provider_for(role).chat(messages, tools),
                                              timeout=min(s.llm_timeout_s + 1, remaining))
                await self.breaker.on_success()
                resp.latency_ms = (time.perf_counter() - t0) * 1000
                resp.retries = retries
                resp.cost_usd = self.cost(resp.prompt_tokens, resp.completion_tokens)
                self._account(role, step, resp, ctx, "ok")
                return resp
            except asyncio.TimeoutError:
                last_err = LLMError("model call timed out", retryable=True)
            except LLMError as e:
                last_err = e
                if not e.retryable:
                    metrics.llm_calls.labels(role, "error").inc()
                    break
            await self.breaker.on_failure()
            metrics.llm_calls.labels(role, "retryable_error").inc()
            if attempt < s.llm_max_retries:
                retries += 1
                metrics.llm_retries.labels(role).inc()
                delay = min(s.llm_backoff_max_s, s.llm_backoff_base_s * (2 ** attempt)) * (0.5 + random.random() / 2)
                ctx.trace.emit("llm_retry", agent=role, step=step, attempt=attempt + 1, error=str(last_err),
                               backoff_s=round(delay, 3))
                if delay >= ctx.time_left():
                    break
                await asyncio.sleep(delay)
        # ---------------- fallback chain
        log.warning("primary LLM unavailable", extra={"role": role, "error": str(last_err)})
        ctx.trace.emit("llm_unavailable", agent=role, step=step, error=str(last_err),
                       breaker=await self.breaker.state())
        if s.llm_fallback_base_url and s.llm_fallback_model:
            try:
                t0 = time.perf_counter()
                resp = await asyncio.wait_for(self._provider_for(role, True).chat(messages, tools),
                                              timeout=min(s.llm_timeout_s + 1, max(1.0, ctx.time_left())))
                resp.latency_ms = (time.perf_counter() - t0) * 1000
                resp.retries = retries
                resp.fallback_used = "secondary_provider"
                resp.cost_usd = self.cost(resp.prompt_tokens, resp.completion_tokens)
                ctx.degraded, ctx.degraded_reason = True, "secondary_provider_used"
                metrics.llm_fallbacks.labels("secondary_provider").inc()
                self._account(role, step, resp, ctx, "fallback_ok")
                return resp
            except Exception as e:  # noqa: BLE001
                ctx.trace.emit("llm_fallback_failed", agent=role, step=step, error=str(e))
        ctx.degraded, ctx.degraded_reason = True, "llm_unavailable_offline_policy"
        metrics.llm_fallbacks.labels("offline_policy").inc()
        metrics.degraded_runs.labels("llm_unavailable").inc()
        resp = await self._offline_call(role, step, messages, tools, ctx, degraded=True)
        resp.fallback_used = "offline_policy"
        resp.retries = retries
        return resp

    async def _offline_call(self, role, step, messages, tools, ctx, degraded: bool) -> LLMResponse:
        t0 = time.perf_counter()
        out = await asyncio.to_thread(self.offline.respond, role, messages, tools, ctx)
        resp = LLMResponse(out.get("content"), out.get("tool_calls", []), 0, 0, False, "offline", "offline-policy")
        resp.latency_ms = (time.perf_counter() - t0) * 1000
        self._account(role, step, resp, ctx, "offline" if not degraded else "offline_fallback")
        return resp

    def _account(self, role: str, step: str, resp: LLMResponse, ctx, status: str) -> None:
        metrics.llm_calls.labels(role, status).inc()
        metrics.llm_latency.labels(role).observe(resp.latency_ms / 1000)
        metrics.llm_tokens.labels(role, "prompt").inc(resp.prompt_tokens)
        metrics.llm_tokens.labels(role, "completion").inc(resp.completion_tokens)
        metrics.llm_cost.labels(role, step.split("#")[0]).inc(resp.cost_usd)
        ctx.usage.add(step, resp.prompt_tokens, resp.completion_tokens, resp.cost_usd, resp.estimated_usage)

    async def aclose(self):
        for p in list((self._primary or {}).values()) + ([self._fallback] if self._fallback else []):
            await p.aclose()
