"""Classifier agent (A1, A2): a bounded tool-using loop built as a LangGraph sub-graph.

llm_step  -> (tool calls?) -> tool_step -> llm_step ... -> proposal
The loop is bounded by steps, tool calls and wall-clock time (RunContext.over_budget)."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from .. import metrics
from ..contracts import Proposal
from ..guardrails import scan_injection, wrap_untrusted
from ..prompts.registry import active
from ..telemetry import span
from ..tools import TOOL_SPECS, execute_tool
from .context import RunContext


class CState(TypedDict, total=False):
    messages: list[dict]
    pending: list[dict]
    proposal: dict | None
    stop: str | None
    repairs: int
    attempt: int


def build_user_message(ctx: RunContext, strategy: str, feedback: list[str]) -> str:
    payload = {"task": "classify", "strategy": strategy, "verifier_feedback": feedback, "request": ctx.request}
    return wrap_untrusted(json.dumps(payload))


def extract_json(text: str | None) -> dict | None:
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


async def _llm_step(state: CState, config) -> CState:
    ctx: RunContext = config["configurable"]["ctx"]
    why = ctx.over_budget()
    if why:
        ctx.limits_hit.append(why)
        metrics.loop_limit_hits.labels(why).inc()
        ctx.trace.emit("limit_reached", agent="classifier", limit=why)
        return {"stop": why, "pending": []}
    tools = None if ctx.tool_calls >= ctx.settings.agent_max_tool_calls else TOOL_SPECS
    ctx.steps += 1
    step = f"classifier.plan#{ctx.steps}"
    prompt = active("classifier", ctx.settings.prompt_variant)
    with span("agent.classifier.llm", step=step, run_id=ctx.run_id):
        resp = await ctx.llm.chat("classifier", step, state["messages"], tools, ctx)
    metrics.step_latency.labels("classifier", "llm").observe(resp.latency_ms / 1000)
    ctx.trace.emit("llm_call", agent="classifier", step=step, prompt_version=prompt["ref"],
                   prompt_sha=prompt["sha256"], provider=resp.provider, model_role="classifier",
                   latency_ms=round(resp.latency_ms, 2), prompt_tokens=resp.prompt_tokens,
                   completion_tokens=resp.completion_tokens, cost_usd=round(resp.cost_usd, 8),
                   usage_estimated=resp.estimated_usage, retries=resp.retries, fallback=resp.fallback_used,
                   tool_calls=[c["name"] for c in resp.tool_calls], strategy=ctx.strategy, attempt=state.get("attempt", 0))
    msgs = list(state["messages"])
    if resp.tool_calls:
        msgs.append({"role": "assistant", "content": resp.content, "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
            for c in resp.tool_calls]})
        return {"messages": msgs, "pending": resp.tool_calls}
    data = extract_json(resp.content)
    try:
        if data is None:
            raise ValueError("response was not a JSON object")
        prop = Proposal.model_validate(data)
        for c in prop.citations:  # model output is untrusted too
            if not scan_injection(c.quote).allowed:
                raise ValueError("citation text rejected by guardrail")
        return {"messages": msgs, "pending": [], "proposal": prop.model_dump()}
    except (ValidationError, ValueError) as e:
        if state.get("repairs", 0) < 1:
            msgs.append({"role": "assistant", "content": resp.content or ""})
            msgs.append({"role": "user", "content": "Your reply was not a valid proposal object: "
                         f"{str(e)[:300]}. Reply with the JSON object only."})
            return {"messages": msgs, "pending": [], "repairs": state.get("repairs", 0) + 1}
        return {"messages": msgs, "pending": [], "stop": "invalid_output"}


async def _tool_step(state: CState, config) -> CState:
    ctx: RunContext = config["configurable"]["ctx"]
    calls = state["pending"]
    msgs = list(state["messages"])

    async def one(c: dict):
        if ctx.tool_calls >= ctx.settings.agent_max_tool_calls:
            metrics.loop_limit_hits.labels("max_tool_calls").inc()
            return c, {"error": "tool-call budget exhausted; answer with what you have"}, {"status": "budget", "latency_ms": 0}
        ctx.tool_calls += 1
        with span(f"tool.{c['name']}", run_id=ctx.run_id):
            res, meta = await execute_tool(ctx, c["name"], c["arguments"], ctx.settings.tool_timeout_s)
        return c, res, meta

    results = await asyncio.gather(*[one(c) for c in calls])
    for c, res, meta in results:
        ids = []
        for key in ("results", "candidates", "exclusion_notes"):
            for p in res.get(key, []) if isinstance(res, dict) else []:
                pid = p.get("id") or (p.get("heading_passage") or {}).get("id")
                if pid:
                    ids.append(pid)
        if isinstance(res, dict) and res.get("id"):
            ids.append(res["id"])
        ctx.trace.emit("tool_call", agent="classifier", name=c["name"], args=c["arguments"], status=meta["status"],
                       latency_ms=meta["latency_ms"], passages=ids, strategy=ctx.strategy,
                       result_preview=json.dumps(res)[:300], attempt=state.get("attempt", 0))
        metrics.step_latency.labels("classifier", "tool:" + c["name"]).observe(meta["latency_ms"] / 1000)
        msgs.append({"role": "tool", "tool_call_id": c["id"], "name": c["name"], "content": json.dumps(res)})
    return {"messages": msgs, "pending": []}


def _after_llm(state: CState) -> str:
    if state.get("pending"):
        return "tools"
    return "end" if (state.get("proposal") or state.get("stop")) else "llm"


def build_classifier_graph():
    g = StateGraph(CState)
    g.add_node("llm", _llm_step)
    g.add_node("tools", _tool_step)
    g.add_edge(START, "llm")
    g.add_conditional_edges("llm", _after_llm, {"tools": "tools", "end": END, "llm": "llm"})
    g.add_edge("tools", "llm")
    return g.compile()


_GRAPH = None


async def run_classifier(ctx: RunContext, strategy: str, feedback: list[str], attempt: int) -> dict | None:
    """Returns a proposal dict or None when the agent produced no usable proposal."""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_classifier_graph()
    ctx.strategy = strategy
    ctx.vector_only = strategy == "baseline_single_pass"
    prompt = active("classifier", ctx.settings.prompt_variant)
    ctx.prompt_versions["classifier"] = prompt["ref"] + ":" + prompt["sha256"]
    messages = [{"role": "system", "content": prompt["text"]},
                {"role": "user", "content": build_user_message(ctx, strategy, feedback)}]
    ctx.trace.emit("agent_start", agent="classifier", strategy=strategy, attempt=attempt, prompt_version=prompt["ref"])
    try:
        out = await _GRAPH.ainvoke({"messages": messages, "pending": [], "proposal": None, "stop": None,
                                    "repairs": 0, "attempt": attempt},
                                   config={"configurable": {"ctx": ctx}, "recursion_limit": 100})
    except Exception as e:  # noqa: BLE001 - agent failure mid-run => handled by the graph (A2)
        ctx.trace.emit("agent_error", agent="classifier", error=f"{type(e).__name__}: {e}", attempt=attempt)
        metrics.agent_tasks.labels("classifier", "error").inc()
        return None
    prop = out.get("proposal")
    ctx.trace.emit("agent_end", agent="classifier", attempt=attempt, produced_proposal=bool(prop),
                   stop=out.get("stop"), steps=ctx.steps, tool_calls=ctx.tool_calls)
    metrics.agent_tasks.labels("classifier", "success" if prop else "failure").inc()
    if prop and ctx.fault == "fabricated_citation" and attempt == 0 and ctx.settings.enable_fault_injection:
        prop["citations"].append({"id": "SEC-XV-NOTE-12", "quote": "Articles of Chapter 96 are never excluded from Section XV"})
        ctx.trace.emit("fault_injected", kind="fabricated_citation")
    if prop and ctx.fault == "wrong_product" and attempt == 0 and ctx.settings.enable_fault_injection:
        # simulate a model that answers a different product (hallucination / stale context): a fully cited
        # vacuum-flask answer whatever the request is. Citations and quotes are genuine corpus text.
        prop.update({"article": "vacuum flask", "heading": "9617", "gri_applied": "GRI-1", "confidence": "high",
                     "out_of_scope": False, "needs_clarification": [], "tie": False})
        ctx.trace.emit("fault_injected", kind="wrong_product")
    return prop
