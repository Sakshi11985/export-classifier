"""FastAPI service: validation at the ingestion boundary, asynchronous run submission, status polling,
trace reconstruction, feedback, quarantine inspection, health and metrics.

Why asynchronous + polling (A4): a run makes several model and tool calls and takes tens of seconds
(more with a free-tier model), longer than a sensible HTTP timeout. POST returns a run id at once; the
caller polls GET /v1/runs/{id}. Polling was chosen over callbacks because a plain web client and a
curl user can use it with no inbound endpoint, and it needs no delivery-retry machinery. A queue
(Redis) sits between submission and execution so workers scale and fail independently."""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError

from . import metrics
from .config import Settings, get_settings
from .contracts import ClassificationRequest, FeedbackIn, contract_schemas
from .embeddings import build_embedder
from .guardrails import scan_request_fields
from .logging_setup import correlation_id, get_logger, run_id_var, setup_logging
from .redis_util import async_redis, sync_redis
from .retrieval import Corpus
from .store import Store, new_run_id
from .telemetry import current_traceparent, setup_tracing, span
from .vectorstore import TariffIndex

log = get_logger("api")
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")


class AppState:
    s: Settings
    store: Store
    corpus: Corpus | None = None
    r_async = None
    r_sync = None


st = AppState()


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = settings or get_settings()
        setup_logging("api", s.log_level)
        setup_tracing(s, "api")
        st.s = s
        st.r_async = async_redis(s)
        st.r_sync = sync_redis(s)
        st.store = Store(st.r_async, s)
        task = asyncio.create_task(_background(s))
        try:
            await _load_corpus(s)
        except Exception as e:  # noqa: BLE001 - API still starts; /health/ready reports it
            log.warning("index not ready at startup", extra={"error": str(e)})
        yield
        task.cancel()
        await st.r_async.aclose()

    app = FastAPI(title="Export Classification Check Service", version="1.0.0", lifespan=lifespan,
                  description="Proposes an HS classification with GRI, citations and export policy. "
                              "It never files a declaration; a licensed customs broker decides.")

    @app.middleware("http")
    async def observe(request: Request, call_next):
        cid = request.headers.get("x-correlation-id") or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", cid):
            cid = uuid.uuid4().hex
        correlation_id.set(cid)
        t0 = time.perf_counter()
        route = request.scope.get("route")
        try:
            resp = await call_next(request)
        except Exception:  # noqa: BLE001
            log.exception("unhandled error")
            resp = JSONResponse({"error": "internal_error", "correlation_id": cid}, status_code=500)
        path = request.scope.get("route").path if request.scope.get("route") else "unmatched"
        dt = time.perf_counter() - t0
        metrics.http_requests.labels(request.method, path, str(resp.status_code)).inc()
        metrics.http_latency.labels(path).observe(dt)
        resp.headers["x-correlation-id"] = cid
        if path not in ("/metrics", "/health/live"):
            log.info("http", extra={"method": request.method, "path": request.url.path, "status": resp.status_code,
                                    "duration_ms": round(dt * 1000, 2)})
        return resp

    # ------------------------------------------------------------------ submission
    @app.post("/v1/classify", status_code=202, tags=["classification"])
    async def classify(request: Request):
        s, store = st.s, st.store
        client = request.headers.get("x-client-id") or (request.client.host if request.client else "anon")
        if not CLIENT_ID_RE.match(client):
            client = "invalid-client-id"
        cid = correlation_id.get()
        # 1. rate limit (cost control, D7)
        try:
            ok, wait = await store.take_token(client)
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "queue_unavailable", "detail": "Redis is not reachable"}, status_code=503)
        if not ok:
            metrics.rate_limited.labels("rate").inc()
            return JSONResponse({"error": "rate_limited", "detail": "too many requests for this client",
                                 "retry_after_s": round(wait, 2)}, status_code=429,
                                headers={"Retry-After": str(max(1, int(wait + 0.999)))})
        # 2. parse + schema/range/category/cross-field validation (D1, F2)
        body = await request.body()
        try:
            raw = json.loads(body or b"null")
            if not isinstance(raw, dict):
                raise ValueError("body must be a JSON object")
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
            qid = await store.quarantine(body[:2000].decode("utf-8", "replace"),
                                         [{"field": "$", "message": f"invalid JSON: {e}", "type": "json_invalid"}],
                                         "malformed_json", client, cid)
            metrics.validation_rejections.labels("malformed_json").inc()
            metrics.quarantined.labels("malformed_json").inc()
            return JSONResponse({"error": "validation_failed", "quarantine_id": qid, "agent_run_started": False,
                                 "errors": [{"field": "$", "message": f"invalid JSON: {e}"}]}, status_code=422)
        try:
            req = ClassificationRequest.model_validate(raw)
        except ValidationError as e:
            errs = [{"field": ".".join(str(x) for x in err["loc"]) or "$", "message": err["msg"], "type": err["type"]}
                    for err in e.errors()]
            qid = await store.quarantine(raw, errs, "schema_validation", client, cid)
            metrics.validation_rejections.labels("schema").inc()
            metrics.quarantined.labels("schema_validation").inc()
            log.warning("request rejected", extra={"quarantine_id": qid, "errors": errs})
            return JSONResponse({"error": "validation_failed", "quarantine_id": qid, "agent_run_started": False,
                                 "errors": errs}, status_code=422)
        fields = req.model_dump(mode="json", exclude_none=True)
        # 3. guardrails on every free-text field (L4)
        g = scan_request_fields(fields)
        if not g.allowed:
            qid = await store.quarantine(raw, [{"field": "free_text", "message": g.detail, "type": c} for c in g.categories],
                                         "guardrail", client, cid)
            for c in g.categories:
                metrics.guardrail_blocks.labels("input", c).inc()
            metrics.quarantined.labels("guardrail").inc()
            log.warning("request blocked by guardrail", extra={"quarantine_id": qid, "categories": g.categories})
            return JSONResponse({"error": "guardrail_blocked", "categories": g.categories, "detail": g.detail,
                                 "message": "The request was blocked before any model saw it.",
                                 "quarantine_id": qid, "agent_run_started": False}, status_code=400)
        # 4. backpressure + in-flight cap
        if await store.queue_depth() >= s.max_queue_depth:
            metrics.rate_limited.labels("queue_full").inc()
            return JSONResponse({"error": "queue_full", "detail": "the service is saturated, retry later"},
                                status_code=503, headers={"Retry-After": "5"})
        run_id = new_run_id()
        if not await store.inflight_add(client, run_id):
            metrics.rate_limited.labels("inflight").inc()
            return JSONResponse({"error": "too_many_inflight_runs",
                                 "detail": f"limit is {s.max_inflight_per_client} unfinished runs per client"},
                                status_code=429, headers={"Retry-After": "5"})
        run_id_var.set(run_id)
        base = str(request.base_url).rstrip("/")
        opts = fields.get("options") or {}
        # 5. whole-run cache (D7)
        cache_key = None
        if s.run_cache_enabled and st.corpus and not opts.get("bypass_cache") and opts.get("fault", "none") == "none":
            cache_key = store.run_cache_key(fields, st.corpus.version, s.prompt_variant, s.llm_provider)
            cached = await store.get_cached_result(cache_key)
            if cached:
                metrics.cache_hits.labels("run").inc()
                now = time.time()
                cached = {**cached, "run_id": run_id, "cache_hit": True,
                          "timings": {"queue_wait_s": 0.0, "execution_s": 0.0, "end_to_end_s": 0.0}}
                await store.create_run(run_id, fields, client, None, cache_hit=True)
                await store.update_run(run_id, status="completed", started_at=now, finished_at=now, result=cached)
                await store.inflight_remove(client, run_id)
                await store.add_trace(run_id, {"seq": 1, "ts": now, "run_id": run_id, "type": "cache_hit",
                                               "note": "result served from the whole-run cache; no agent executed",
                                               "cache_key": cache_key[-12:]})
                metrics.runs_submitted.labels("hit").inc()
                return JSONResponse({"run_id": run_id, "status": "completed", "cache_hit": True,
                                     "status_url": f"{base}/v1/runs/{run_id}", "trace_url": f"{base}/v1/runs/{run_id}/trace",
                                     "poll_after_ms": 0}, status_code=200)
            metrics.cache_misses.labels("run").inc()
        with span("api.submit", run_id=run_id, client=client):
            await store.create_run(run_id, fields, client, current_traceparent())
            await store.enqueue(run_id)
        metrics.runs_submitted.labels("miss").inc()
        log.info("run queued", extra={"run_id": run_id, "client": client})
        return {"run_id": run_id, "status": "queued", "cache_hit": False, "status_url": f"{base}/v1/runs/{run_id}",
                "trace_url": f"{base}/v1/runs/{run_id}/trace", "poll_after_ms": s.poll_after_ms}

    # ------------------------------------------------------------------ status / trace
    @app.get("/v1/runs/{run_id}", tags=["classification"])
    async def get_run(run_id: str):
        run = await _run_or_404(run_id)
        if isinstance(run, JSONResponse):
            return run
        out = {"run_id": run_id, "status": run["status"], "created_at": run["created_at"],
               "started_at": run.get("started_at"), "finished_at": run.get("finished_at"),
               "cache_hit": bool(int(run.get("cache_hit", 0))), "worker_id": run.get("worker_id")}
        if run["status"] in ("completed", "failed"):
            out["result"] = run.get("result")
        else:
            out["queue_depth"] = await st.store.queue_depth()
            out["poll_after_ms"] = st.s.poll_after_ms
        if run.get("feedback"):
            out["feedback"] = json.loads(run["feedback"])
        return out

    @app.get("/v1/runs/{run_id}/trace", tags=["observability"])
    async def get_trace(run_id: str):
        run = await _run_or_404(run_id)
        if isinstance(run, JSONResponse):
            return run
        events = await st.store.get_trace(run_id)
        llm = [e for e in events if e["type"] == "llm_call"]
        tools = [e for e in events if e["type"] == "tool_call"]
        summary = {
            "llm_calls": len(llm), "tool_calls": len(tools),
            "prompt_tokens": sum(e.get("prompt_tokens", 0) for e in llm),
            "completion_tokens": sum(e.get("completion_tokens", 0) for e in llm),
            "cost_usd": round(sum(e.get("cost_usd", 0) for e in llm), 8),
            "cost_by_step": {e["step"]: e.get("cost_usd", 0) for e in llm},
            "passages_retrieved": sorted({p for e in tools for p in e.get("passages", [])}),
            "prompt_versions": (run.get("result") or {}).get("prompt_versions"),
            "verification": [e for e in events if e["type"] == "verification"],
            "end_to_end_s": ((run.get("result") or {}).get("timings") or {}).get("end_to_end_s"),
        }
        return {"run_id": run_id, "status": run["status"], "request": run["request"], "events": events, "summary": summary}

    async def _run_or_404(run_id: str):
        if not re.fullmatch(r"run_[a-f0-9]{16}", run_id):
            return JSONResponse({"error": "not_found", "detail": "malformed run id"}, status_code=404)
        run = await st.store.get_run(run_id)
        if not run:
            return JSONResponse({"error": "not_found", "detail": f"no run {run_id}"}, status_code=404)
        return run

    # ------------------------------------------------------------------ feedback / quality
    @app.post("/v1/runs/{run_id}/feedback", tags=["feedback"])
    async def post_feedback(run_id: str, request: Request):
        run = await _run_or_404(run_id)
        if isinstance(run, JSONResponse):
            return run
        try:
            fb = FeedbackIn.model_validate(await request.json())
        except (ValidationError, json.JSONDecodeError) as e:
            errs = ([{"field": ".".join(map(str, x["loc"])), "message": x["msg"]} for x in e.errors()]
                    if isinstance(e, ValidationError) else [{"field": "$", "message": "invalid JSON"}])
            return JSONResponse({"error": "validation_failed", "errors": errs}, status_code=422)
        rec = await st.store.add_feedback(run_id, fb.rating, fb.reason)
        metrics.feedback_total.labels(fb.rating).inc()
        summ = await st.store.feedback_summary()
        if summ["satisfaction_ratio"] is not None:
            metrics.feedback_ratio.set(summ["satisfaction_ratio"])
        return {"stored": rec, "summary": summ}

    @app.get("/v1/feedback/summary", tags=["feedback"])
    async def feedback_summary():
        return await st.store.feedback_summary()

    @app.get("/v1/quality", tags=["feedback"])
    async def quality(limit: int = 200):
        rows = await st.store.quality_series(max(1, min(limit, 1000)))
        agg = {}
        for k in ("groundedness", "context_relevance", "faithfulness", "hallucination"):
            vals = [r[k] for r in rows if r.get(k) is not None]
            agg[k] = round(sum(vals) / len(vals), 4) if vals else None
        return {"scored_runs": len(rows), "average": agg, "series": rows}

    # ------------------------------------------------------------------ quarantine
    @app.get("/v1/quarantine", tags=["ingestion"])
    async def list_quarantine(limit: int = 50):
        return {"records": await st.store.list_quarantine(max(1, min(limit, 200)))}

    @app.get("/v1/quarantine/{qid}", tags=["ingestion"])
    async def get_quarantine(qid: str):
        rec = await st.store.get_quarantine(qid) if re.fullmatch(r"q_[a-f0-9]{12}", qid) else None
        if not rec:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return rec

    # ------------------------------------------------------------------ corpus + contract
    @app.get("/v1/clauses/{clause_id}", tags=["corpus"])
    async def clause(clause_id: str):
        if not st.corpus:
            return JSONResponse({"error": "index_not_ready"}, status_code=503)
        c = st.corpus.get(clause_id)
        if not c:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return {"id": c.id, "type": c.type, "text": c.text, "source": c.metadata.get("source"), "page": c.metadata.get("page"),
                "title": c.metadata.get("title")}

    @app.get("/v1/contract", tags=["contract"])
    async def contract():
        return contract_schemas()

    @app.get("/v1/corpus", tags=["corpus"])
    async def corpus_info():
        return await st.store.index_meta()

    # ------------------------------------------------------------------ health + metrics
    @app.get("/health/live", tags=["health"])
    async def live():
        return {"status": "alive"}

    async def _health() -> tuple[dict, bool]:
        checks, ok = {}, True
        try:
            await st.store.ping()
            checks["redis"] = "ok"
        except Exception as e:  # noqa: BLE001
            checks["redis"], ok = f"down: {type(e).__name__}", False
        if st.corpus is None:
            try:
                await _load_corpus(st.s)
            except Exception:  # noqa: BLE001
                pass
        if st.corpus is not None:
            try:
                n = await asyncio.to_thread(st.corpus.index.count)
                checks["chroma"] = "ok"
                checks["index_chunks"] = n
            except Exception as e:  # noqa: BLE001
                checks["chroma"], ok = f"down: {type(e).__name__}", False
        else:
            checks["chroma"], ok = "index not loaded", False
        workers = 0
        try:
            workers = await st.store.workers_alive()
            checks["workers_alive"] = workers
            checks["queue_depth"] = await st.store.queue_depth()
        except Exception:  # noqa: BLE001
            pass
        from .llm.client import CircuitBreaker
        try:
            checks["llm_circuit_breaker"] = await CircuitBreaker(st.s, st.r_async).state()
        except Exception:  # noqa: BLE001
            checks["llm_circuit_breaker"] = "unknown"
        checks["llm_provider"] = st.s.llm_provider
        # the web client greys out its fault-injection control when this is false (A5/A3 demonstration only)
        checks["fault_injection"] = st.s.enable_fault_injection
        meta = await st.store.index_meta() if ok else {}
        checks["index_built_at"] = meta.get("built_at")
        status = "ok" if ok and workers > 0 else ("degraded" if ok else "down")
        return {"status": status, "checks": checks, "uptime_s": round(time.time() - metrics.START_TIME, 1)}, ok

    @app.get("/health/ready", tags=["health"])
    async def ready():
        body, ok = await _health()
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.get("/health", tags=["health"])
    async def health():
        body, ok = await _health()
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.get("/metrics", include_in_schema=False)
    async def prom():
        try:
            metrics.queue_depth.set(await st.store.queue_depth())
            metrics.queue_processing.set(await st.store.processing_count())
            metrics.workers_alive.set(await st.store.workers_alive())
            meta = await st.store.index_meta()
            metrics.update_index_gauges(meta)
        except Exception:  # noqa: BLE001
            pass
        metrics.service_uptime.set(time.time() - metrics.START_TIME)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # ------------------------------------------------------------------ frontend
    # Fonts and any other static assets the web client needs are served from the image, so the
    # client has no dependency on an external network at demonstration time.
    assets = FRONTEND / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(FRONTEND / "index.html")

    return app


async def _load_corpus(s: Settings) -> None:
    def load():
        emb = build_embedder(s, st.r_sync, metrics)
        return Corpus(TariffIndex(s, emb), s, st.r_sync).load()
    st.corpus = await asyncio.to_thread(load)


async def _background(s: Settings) -> None:
    while True:
        await asyncio.sleep(15)
        try:
            if st.corpus is not None:
                await asyncio.to_thread(st.corpus.refresh_if_stale, 15)
        except Exception:  # noqa: BLE001
            pass


def get_app() -> FastAPI:
    return create_app()
