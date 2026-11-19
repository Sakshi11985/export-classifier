"""A5: timeouts, bounded retry with exponential backoff, circuit breaker, graceful degradation and the rule that a
run which did not complete cleanly is never a confident answer. The provider is replaced by in-process fakes
here; tests/test_api_integration.py repeats the breaker scenario over HTTP against the real stub."""
import asyncio
import time

import pytest

from app.config import get_settings
from app.llm.client import CircuitBreaker, LLMClient, LLMError, LLMResponse
from app.agents.runner import execute_run


class FakeProvider:
    def __init__(self, behaviours):
        self.behaviours = list(behaviours)
        self.calls = 0

    async def chat(self, messages, tools):
        self.calls += 1
        b = self.behaviours[min(self.calls - 1, len(self.behaviours) - 1)]
        if b == "ok":
            return LLMResponse('{"supported": true}', [], 10, 5, False, "fake", "m")
        if b == "503":
            raise LLMError("provider returned HTTP 503", retryable=True, status=503)
        if b == "400":
            raise LLMError("bad request", retryable=False, status=400)
        if b == "hang":
            await asyncio.sleep(30)


def client(monkeypatch, behaviours, **env):
    for k, v in {"LLM_PROVIDER": "openai_compat", "LLM_BASE_URL": "http://x/v1", "LLM_MODEL_CLASSIFIER": "m",
                 "LLM_BACKOFF_BASE_S": "0.05", "LLM_BACKOFF_MAX_S": "0.2", "LLM_MAX_RETRIES": "2", "LLM_TIMEOUT_S": "0.3",
                 "CB_FAILURE_THRESHOLD": "3", "CB_RECOVERY_S": "1", **env}.items():
        monkeypatch.setenv(k, v)
    c = LLMClient(get_settings())
    fp = FakeProvider(behaviours)
    monkeypatch.setattr(c, "_provider_for", lambda role, fallback=False: fp)
    return c, fp


async def test_retry_with_backoff_then_success(monkeypatch, make_ctx):
    c, fp = client(monkeypatch, ["503", "503", "ok"])
    ctx = make_ctx(s=c.s, llm=c)
    t0 = time.perf_counter()
    r = await c.chat("classifier", "classifier.plan#1", [{"role": "user", "content": "x"}], None, ctx)
    assert fp.calls == 3 and r.retries == 2 and r.provider == "fake" and not ctx.degraded
    assert time.perf_counter() - t0 >= 0.05                                       # backoff actually waited
    waits = [e["backoff_s"] for e in ctx.trace.events if e["type"] == "llm_retry"]
    assert len(waits) == 2 and waits[1] <= 0.2


async def test_non_retryable_error_is_not_retried(monkeypatch, make_ctx):
    c, fp = client(monkeypatch, ["400"])
    ctx = make_ctx(s=c.s, llm=c)
    r = await c.chat("classifier", "classifier.plan#1", [{"role": "user", "content": "x"}], [], ctx)
    assert fp.calls == 1 and r.fallback_used == "offline_policy" and ctx.degraded


async def test_timeout_on_every_call(monkeypatch, make_ctx):
    c, fp = client(monkeypatch, ["hang"], LLM_MAX_RETRIES="0")
    ctx = make_ctx(s=c.s, llm=c)
    t0 = time.perf_counter()
    r = await c.chat("classifier", "classifier.plan#1", [{"role": "user", "content": "x"}], [], ctx)
    assert time.perf_counter() - t0 < 3 and r.fallback_used == "offline_policy" and ctx.degraded


async def test_breaker_opens_then_half_opens_then_closes(monkeypatch, make_ctx):
    c, fp = client(monkeypatch, ["503"] * 3 + ["ok"], LLM_MAX_RETRIES="0")
    ctx = make_ctx(s=c.s, llm=c)
    for _ in range(3):
        await c.chat("classifier", "classifier.plan#1", [{"role": "user", "content": "x"}], [], ctx)
    assert await c.breaker.state() == "open"
    calls_when_open = fp.calls
    await c.chat("classifier", "classifier.plan#2", [{"role": "user", "content": "x"}], [], ctx)
    assert fp.calls == calls_when_open                                             # open breaker: provider not called
    assert any(e["type"] == "llm_unavailable" and e["breaker"] == "open" for e in ctx.trace.events)
    await asyncio.sleep(1.1)
    assert await c.breaker.state() == "half-open"
    r = await c.chat("classifier", "classifier.plan#3", [{"role": "user", "content": "x"}], [], ctx)
    assert r.provider == "fake" and await c.breaker.state() == "closed"


async def test_half_open_failure_reopens(monkeypatch, make_ctx):
    c, fp = client(monkeypatch, ["503"], LLM_MAX_RETRIES="0")
    ctx = make_ctx(s=c.s, llm=c)
    for _ in range(3):
        await c.chat("classifier", "s#1", [{"role": "user", "content": "x"}], [], ctx)
    await asyncio.sleep(1.1)
    await c.chat("classifier", "s#2", [{"role": "user", "content": "x"}], [], ctx)       # probe fails
    assert await c.breaker.state() == "open"


async def test_breaker_state_is_shared_through_redis(monkeypatch, settings):
    from app.redis_util import async_redis
    monkeypatch.setenv("CB_FAILURE_THRESHOLD", "2")
    s = get_settings()
    r1, r2 = async_redis(s), async_redis(s)
    a, b = CircuitBreaker(s, r1, "shared-test"), CircuitBreaker(s, r2, "shared-test")
    await a.on_failure(); await a.on_failure()
    assert await b.state() == "open"                                                    # another worker sees it
    await a.on_success()
    assert await b.state() == "closed"


async def test_degraded_run_is_never_clear_for_filing(monkeypatch, corpus):
    c, fp = client(monkeypatch, ["503"], LLM_MAX_RETRIES="0")
    s = c.s
    res, trace, ctx = await execute_run(s, corpus, c, "run_deg", {"product_description": "Stainless steel vacuum flask with a moulded plastic outer body."}, time.time())
    assert res["degraded"] is True and res["confident"] is False
    assert res["route"] == "specialist-classification-review" and res["route_basis"]["code"] == "degraded_mode"
    assert res["classification"] is not None                                            # best-effort proposal is shown for the broker


async def test_secondary_provider_fallback_is_marked_degraded(monkeypatch, make_ctx):
    c, fp = client(monkeypatch, ["503"], LLM_MAX_RETRIES="0", LLM_FALLBACK_BASE_URL="http://y/v1", LLM_FALLBACK_MODEL="m2")
    sec = FakeProvider(["ok"])
    monkeypatch.setattr(c, "_provider_for", lambda role, fallback=False: sec if fallback else fp)
    ctx = make_ctx(s=c.s, llm=c)
    r = await c.chat("classifier", "classifier.plan#1", [{"role": "user", "content": "x"}], [], ctx)
    assert r.fallback_used == "secondary_provider" and ctx.degraded and ctx.degraded_reason == "secondary_provider_used"
