"""Agent worker. Scales horizontally: `docker compose up --scale worker=3`.

Each worker claims run ids from the Redis queue (reliable queue: the id moves to a per-worker
processing list and is only removed on completion), runs the agent graph with up to
WORKER_CONCURRENCY runs in parallel, stores the result and trace, and exposes Prometheus metrics.
If a worker dies, its heartbeat expires and any live worker returns its claimed runs to the queue."""
from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import socket
import time

from prometheus_client import start_http_server

from . import metrics
from .agents.context import Trace
from .agents.runner import execute_run
from .config import Settings, get_settings
from .embeddings import build_embedder
from .evaluation.scoring import score_run
from .llm.client import LLMClient
from .logging_setup import correlation_id, get_logger, run_id_var, setup_logging
from .redis_util import async_redis, sync_redis
from .retrieval import Corpus
from .store import Store
from .telemetry import context_from_traceparent, setup_tracing, span
from .vectorstore import TariffIndex

log = get_logger("worker")


class Worker:
    def __init__(self, s: Settings, corpus: Corpus, store: Store, llm: LLMClient, worker_id: str):
        self.s, self.corpus, self.store, self.llm, self.id = s, corpus, store, llm, worker_id
        self.stop = asyncio.Event()
        self.inflight = 0

    async def _stream_trace(self, run_id: str, trace: Trace) -> None:
        """Write trace events to the store while the run is still executing, so a caller polling
        /v1/runs/{id}/trace sees the agent's steps as they happen (the web client draws them live)
        and a stalled run shows where it stopped. The final flush in the run loop writes the rest;
        Trace.pending() only ever returns events not already written, so nothing is duplicated."""
        try:
            while True:
                await asyncio.sleep(0.2)
                for ev in trace.pending():
                    await self.store.add_trace(run_id, ev)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - streaming is best effort, never fail the run for it
            log.debug("trace streaming stopped", extra={"run_id": run_id})

    async def process(self, run_id: str) -> None:
        run = await self.store.get_run(run_id)
        if not run:
            log.warning("claimed unknown run", extra={"run_id": run_id})
            return
        if run.get("status") in ("completed", "failed"):
            return  # re-delivered after a worker death; already finished
        run_id_var.set(run_id)
        correlation_id.set(run_id)
        request = run["request"]
        submitted = float(run["created_at"])
        started = time.time()
        await self.store.update_run(run_id, status="running", started_at=started, worker_id=self.id)
        trace = Trace(run_id)
        from opentelemetry import context as otel_context
        parent = context_from_traceparent(run.get("traceparent"))
        token = otel_context.attach(parent) if parent is not None else None
        self.inflight += 1
        metrics.worker_inflight.set(self.inflight)
        flusher = asyncio.create_task(self._stream_trace(run_id, trace))
        try:
            with span("worker.run", run_id=run_id, worker=self.id):
                self.corpus.refresh_if_stale()
                result, trace, ctx = await execute_run(self.s, self.corpus, self.llm, run_id, request, submitted, trace)
                result["worker_id"] = self.id
                status = "completed"
                if result["route_basis"]["code"] in ("agent_failure", "run_timeout", "loop_limit"):
                    status = "failed"
        except Exception as e:  # noqa: BLE001 - last resort: never leave a run hanging
            log.exception("unhandled worker error", extra={"run_id": run_id})
            result = {"run_id": run_id, "route": "specialist-classification-review", "confident": False,
                      "route_basis": {"code": "worker_error", "explanation": f"The run failed before completing: {type(e).__name__}"},
                      "classification": None, "export_policy": None, "citations": [], "degraded": True,
                      "verification": {"passed": False, "attempts": 0}, "escalation": {"required": True, "to": "specialist classification reviewer",
                                                                                      "reason_code": "worker_error", "broker_decision_required": True},
                      "determination_authority": "No determination was made; a licensed customs broker decides."}
            status, ctx = "failed", None
        finally:
            flusher.cancel()
            if token is not None:
                otel_context.detach(token)
            self.inflight -= 1
            metrics.worker_inflight.set(self.inflight)
        for ev in trace.pending():
            await self.store.add_trace(run_id, ev)
        await self.store.update_run(run_id, status=status, finished_at=time.time(), result=result)
        await self.store.inflight_remove(run.get("client_id", "-"), run_id)
        # cache only complete, non-degraded results (D7)
        if (self.s.run_cache_enabled and status == "completed" and not result.get("degraded")
                and not (request.get("options") or {}).get("bypass_cache")
                and not ((request.get("options") or {}).get("fault") not in (None, "none"))):
            key = self.store.run_cache_key(request, self.corpus.version, self.s.prompt_variant, self.s.llm_provider)
            await self.store.set_cached_result(key, result)
        log.info("run finished", extra={"run_id": run_id, "route": result["route"], "status": status,
                                        "e2e_s": result.get("timings", {}).get("end_to_end_s")})
        if ctx is not None and random.random() < self.s.live_score_sample_rate:
            await self._live_score(run_id, result, ctx, request)

    async def _live_score(self, run_id: str, result: dict, ctx, request: dict) -> None:
        try:
            sc = score_run(result, ctx.retrieved, request, self.corpus)
            slim = {k: v for k, v in sc.items() if k in ("groundedness", "context_relevance", "faithfulness", "hallucination")}
            await self.store.add_quality(run_id, slim)
            metrics.live_scored.inc()
            for k, v in slim.items():
                if v is not None:
                    metrics.live_quality.labels(k).set(v)
            rows = await self.store.quality_series(50)
            for k in slim:
                vals = [r[k] for r in rows if r.get(k) is not None]
                if vals:
                    metrics.live_quality.labels(k).set(sum(vals) / len(vals))
        except Exception:  # noqa: BLE001
            log.exception("live scoring failed")

    async def consumer(self, n: int) -> None:
        sem_wait = 0
        while not self.stop.is_set():
            try:
                run_id = await self.store.claim(self.id, timeout_s=2)
            except Exception as e:  # noqa: BLE001
                log.error("queue claim failed", extra={"error": str(e)})
                await asyncio.sleep(1)
                continue
            if not run_id:
                continue
            try:
                await self.process(run_id)
            except Exception:  # noqa: BLE001
                log.exception("process crashed", extra={"run_id": run_id})
            finally:
                await self.store.ack(self.id, run_id)

    async def heartbeat(self) -> None:
        while not self.stop.is_set():
            try:
                await self.store.heartbeat(self.id)
                orphans = await self.store.requeue_orphans()
                if orphans:
                    log.warning("requeued runs from dead workers", extra={"count": orphans})
            except Exception as e:  # noqa: BLE001
                log.error("heartbeat failed", extra={"error": str(e)})
            await asyncio.sleep(max(1, self.s.worker_heartbeat_ttl_s / 3))

    async def run(self) -> None:
        metrics.worker_up.set(1)
        await self.store.heartbeat(self.id)
        tasks = [asyncio.create_task(self.heartbeat())] + [
            asyncio.create_task(self.consumer(i)) for i in range(self.s.worker_concurrency)]
        log.info("worker started", extra={"worker": self.id, "concurrency": self.s.worker_concurrency})
        await self.stop.wait()
        log.info("worker draining")
        await asyncio.gather(*tasks[1:], return_exceptions=True)
        tasks[0].cancel()
        metrics.worker_up.set(0)


async def amain() -> None:
    s = get_settings()
    setup_logging("worker", s.log_level)
    setup_tracing(s, "worker")
    wid = f"{socket.gethostname()}-{os.getpid()}"
    start_http_server(s.worker_metrics_port)
    r_sync, r_async = sync_redis(s), async_redis(s)
    embedder = build_embedder(s, r_sync, metrics)
    # wait for the index (the ingest job runs before workers start under compose)
    corpus = None
    for _ in range(60):
        try:
            corpus = Corpus(TariffIndex(s, embedder), s, r_sync).load()
            break
        except Exception as e:  # noqa: BLE001
            log.warning("index not ready", extra={"error": str(e)})
            await asyncio.sleep(2)
    if corpus is None:
        raise SystemExit("index never became ready")
    store = Store(r_async, s)
    llm = LLMClient(s, r_async)
    w = Worker(s, corpus, store, llm, wid)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, w.stop.set)

    async def uptime():
        while True:
            metrics.service_uptime.set(time.time() - metrics.START_TIME)
            await asyncio.sleep(2)

    asyncio.create_task(uptime())
    await w.run()
    await llm.aclose()


if __name__ == "__main__":
    asyncio.run(amain())
