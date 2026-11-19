"""Executes one run end to end with a hard wall-clock bound, converting any failure into a
specialist-review result (A5: an incomplete run is never returned as a confident answer)."""
from __future__ import annotations

import asyncio
import time

from .. import metrics
from ..config import Settings
from ..logging_setup import get_logger, run_id_var
from ..telemetry import span
from .context import RunContext, Trace
from .graph import make_graph
from .router import build_result

log = get_logger("runner")
_GRAPHS: dict = {}


def _graph(verifier_enabled: bool, max_attempts: int):
    key = (verifier_enabled, max_attempts)
    if key not in _GRAPHS:
        _GRAPHS[key] = make_graph(verifier_enabled, max_attempts)
    return _GRAPHS[key]


async def execute_run(s: Settings, corpus, llm, run_id: str, request: dict, submitted_at: float,
                      trace: Trace | None = None, strategy: str | None = None,
                      verifier_enabled: bool = True) -> tuple[dict, Trace, RunContext]:
    run_id_var.set(run_id)
    trace = trace or Trace(run_id)
    started_at = time.time()
    fault = ((request.get("options") or {}).get("fault") or "none") if s.enable_fault_injection else "none"
    ctx = RunContext(run_id=run_id, request={k: v for k, v in request.items() if k not in ("options",)},
                     corpus=corpus, settings=s, llm=llm, trace=trace,
                     deadline=time.monotonic() + s.agent_run_timeout_s, fault=fault)
    first = strategy or ("material_first" if fault == "naive_first_pass" else s.first_strategy)
    trace.emit("run_start", provider=s.llm_provider, first_strategy=first, fault=fault,
               limits={"max_steps": s.agent_max_steps, "max_tool_calls": s.agent_max_tool_calls,
                       "run_timeout_s": s.agent_run_timeout_s}, index_version=corpus.version)
    metrics.runs_started.inc()
    graph = _graph(verifier_enabled, s.max_verify_attempts)
    state = {"attempt": 0, "strategy": first, "feedback": [], "proposal": None, "verification": None, "attempts": [],
             "failure": None, "result": None, "submitted_at": submitted_at, "started_at": started_at}
    try:
        with span("agent.run", run_id=run_id, strategy=first):
            out = await asyncio.wait_for(
                graph.ainvoke(state, config={"configurable": {"ctx": ctx}, "recursion_limit": 60}),
                timeout=s.agent_run_timeout_s + 5)
        result = out["result"]
    except asyncio.TimeoutError:
        log.error("run timed out", extra={"run_id": run_id})
        ctx.limits_hit.append("wall_clock")
        metrics.loop_limit_hits.labels("wall_clock").inc()
        result = build_result(ctx, None, None, [], submitted_at, started_at, failure="run_timeout",
                              verifier_enabled=verifier_enabled)
    except Exception as e:  # noqa: BLE001
        log.exception("run failed", extra={"run_id": run_id})
        trace.emit("run_error", error=f"{type(e).__name__}: {e}")
        result = build_result(ctx, None, None, [], submitted_at, started_at, failure="agent_failure",
                              verifier_enabled=verifier_enabled)
    trace.emit("route", route=result["route"], basis=result["route_basis"]["code"], confident=result["confident"],
               verification_passed=result["verification"]["passed"], steps=ctx.steps, tool_calls=ctx.tool_calls,
               usage=ctx.usage.as_dict(), degraded=ctx.degraded)
    # per-run metrics
    outcome = "success" if result["route_basis"]["code"] not in ("agent_failure", "loop_limit", "run_timeout", "no_proposal",
                                                                    "verification_failed") else "failed"
    metrics.runs_finished.labels(outcome, result["route"]).inc()
    metrics.steps_per_run.labels("classifier").observe(ctx.steps)
    metrics.tool_calls_per_run.labels("classifier").observe(ctx.tool_calls)
    metrics.verification_attempts.observe(max(1, result["verification"]["attempts"]))
    metrics.run_exec.labels(result["route"]).observe(result["timings"]["execution_s"])
    metrics.run_e2e.labels(result["route"]).observe(result["timings"]["end_to_end_s"])
    metrics.run_queue_wait.observe(result["timings"]["queue_wait_s"])
    return result, trace, ctx
