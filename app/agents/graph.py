"""Top-level run graph (LangGraph): classify -> verify -> (retry with a different strategy | route).

Topology and failure handling (A2):
  classifier agent  : proposes a classification using tools
  verifier agent    : independently verifies it against retrieved passages
  router            : applies the routing rules and assembles the response
If the classifier fails mid-run it is retried once with a different strategy; a second failure, or a
second verification failure, ends in specialist-classification-review. A run is never returned as
confident unless the verifier passed."""
from __future__ import annotations

import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .. import metrics
from ..telemetry import span
from .classifier import run_classifier
from .context import RunContext
from .router import build_result
from .verifier import verify_proposal

NEXT_STRATEGY = {"default": "article_first", "article_first": "exhaustive_notes", "exhaustive_notes": "article_first",
                 "material_first": "article_first", "baseline_single_pass": "article_first"}


class RunState(TypedDict, total=False):
    attempt: int
    strategy: str
    feedback: list[str]
    proposal: dict | None
    verification: dict | None
    attempts: list[dict]
    failure: str | None
    result: dict | None
    submitted_at: float
    started_at: float


def make_graph(verifier_enabled: bool = True, max_attempts: int = 2):
    async def classify(state: RunState, config) -> RunState:
        ctx: RunContext = config["configurable"]["ctx"]
        t0 = time.perf_counter()
        with span("agent.classifier", attempt=state["attempt"], strategy=state["strategy"], run_id=ctx.run_id):
            prop = await run_classifier(ctx, state["strategy"], state.get("feedback", []), state["attempt"])
        metrics.step_latency.labels("classifier", "agent_total").observe(time.perf_counter() - t0)
        att = list(state.get("attempts", []))
        att.append({"attempt": state["attempt"], "strategy": state["strategy"], "produced_proposal": bool(prop),
                    "heading": prop.get("heading") if prop else None, "verification_passed": None, "issues": []})
        return {"proposal": prop, "attempts": att}

    def after_classify(state: RunState, config) -> str:
        ctx = config["configurable"]["ctx"]
        if state.get("proposal") and verifier_enabled:
            return "verify"
        if state.get("proposal"):
            return "route"
        if ctx.over_budget() or state["attempt"] + 1 >= max_attempts:
            return "route_fail"
        return "retry"

    async def verify(state: RunState, config) -> RunState:
        ctx: RunContext = config["configurable"]["ctx"]
        t0 = time.perf_counter()
        with span("agent.verifier", attempt=state["attempt"], run_id=ctx.run_id):
            rep = await verify_proposal(ctx, state["proposal"], state["attempt"])
        metrics.step_latency.labels("verifier", "agent_total").observe(time.perf_counter() - t0)
        att = list(state["attempts"])
        att[-1] = {**att[-1], "verification_passed": rep["passed"], "issues": rep["issues"]}
        return {"verification": rep, "attempts": att}

    def after_verify(state: RunState, config) -> str:
        ctx = config["configurable"]["ctx"]
        if state["verification"]["passed"]:
            return "route"
        if state["attempt"] + 1 >= max_attempts or ctx.over_budget():
            return "route"
        return "retry"

    async def retry(state: RunState, config) -> RunState:
        ctx: RunContext = config["configurable"]["ctx"]
        rep = state.get("verification") or {}
        nxt = rep.get("next_strategy") or NEXT_STRATEGY.get(state["strategy"], "article_first")
        if nxt == state["strategy"]:
            nxt = NEXT_STRATEGY.get(state["strategy"], "article_first")
        ctx.trace.emit("retry", from_strategy=state["strategy"], to_strategy=nxt, issues=rep.get("issues", ["classifier produced no proposal"]),
                       attempt=state["attempt"] + 1)
        return {"attempt": state["attempt"] + 1, "strategy": nxt, "proposal": None,
                "feedback": rep.get("issues", ["no proposal produced"]), "verification": None}

    async def route(state: RunState, config) -> RunState:
        ctx: RunContext = config["configurable"]["ctx"]
        failure = None
        if not state.get("proposal"):
            failure = "loop_limit" if ctx.limits_hit else "agent_failure"
        res = build_result(ctx, state.get("proposal"), state.get("verification"), state.get("attempts", []),
                           state["submitted_at"], state["started_at"], failure, verifier_enabled)
        return {"result": res}

    async def route_fail(state: RunState, config) -> RunState:
        return await route({**state, "proposal": None}, config)

    g = StateGraph(RunState)
    g.add_node("classify", classify)
    g.add_node("verify", verify)
    g.add_node("retry", retry)
    g.add_node("route", route)
    g.add_node("route_fail", route_fail)
    g.add_edge(START, "classify")
    g.add_conditional_edges("classify", after_classify, {"verify": "verify", "route": "route", "retry": "retry", "route_fail": "route_fail"})
    g.add_conditional_edges("verify", after_verify, {"route": "route", "retry": "retry"})
    g.add_edge("retry", "classify")
    g.add_edge("route", END)
    g.add_edge("route_fail", END)
    return g.compile()
