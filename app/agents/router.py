"""Routing (F6, F7) and result assembly. Everything returned to the caller is built here from
verified material: citations carry the retrieved passage text, and the export-policy statement is
composed by code from the DGFT policy passage, never written by a model."""
from __future__ import annotations

import re
import time

from .. import metrics
from ..guardrails import filter_result
from .context import RunContext

ROUTE_CLEAR = "clear-for-filing"
ROUTE_CLARIFY = "seek-product-clarification"
ROUTE_REVIEW = "specialist-classification-review"


def _parse_items(heading_text: str, capacity: float | None) -> list[dict]:
    items = []
    for line in heading_text.splitlines():
        if not line.startswith("- "):
            continue
        parts = line[2:].split(" ", 2)
        if len(parts[0]) != 8:
            continue
        desc = parts[2] if len(parts) > 2 else ""
        match = None
        m = re.search(r"capacity (not )?exceeding ([\d.]+) ?l", desc)
        if m and capacity is not None:
            lim = float(m.group(2))
            match = (capacity <= lim) if m.group(1) else (capacity > lim)
        items.append({"code": parts[0], "description": desc, "matches_request_capacity": match})
    return items


def _policy_statement(ctx: RunContext, heading: str) -> dict | None:
    chunk = ctx.corpus.get(f"POLICY-{heading}")
    if not chunk:
        return None
    ctx.retrieved[chunk.id] = chunk.text
    ctx.trace.emit("tool_call", agent="router", name="get_clause", args={"id": chunk.id}, status="ok",
                   latency_ms=0, passages=[chunk.id])
    items = chunk.metadata.get("items", [])
    pols = sorted({(i["policy"] or "not stated") for i in items})
    conds = []
    for cid in chunk.metadata.get("linked_conditions", []):
        c = ctx.corpus.get(cid)
        if c:
            ctx.retrieved[c.id] = c.text
            conds.append({"id": c.id, "text": c.text})
    summary = (f"Export policy for heading {heading} under the supplied DGFT ITC(HS) schedule: "
               + (pols[0] if len(pols) == 1 else "varies by tariff item (" + ", ".join(pols) + ")")
               + (". Policy conditions apply to some items (see conditions)." if conds else "."))
    return {"heading": heading, "summary": summary, "policies": pols, "passage_id": chunk.id,
            "items": [{"code": i["code"], "description": i["description"], "policy": i["policy"],
                       "condition": i["condition"] or None, "notification": i["notification"] or None,
                       "notification_date": i["notification_date"] or None} for i in items],
            "conditions": conds, "source": chunk.metadata.get("source"), "page": chunk.metadata.get("page"),
            "caveat": "Taken verbatim from the supplied schedule; confirm it is current before filing."}


def _citations(ctx: RunContext, proposal: dict) -> list[dict]:
    out = []
    for c in proposal.get("citations") or []:
        chunk = ctx.corpus.get(c["id"])
        out.append({"id": c["id"], "type": chunk.type if chunk else None,
                    "title": chunk.metadata.get("title") if chunk else None, "quote": c["quote"],
                    "passage": ctx.retrieved.get(c["id"]), "resolves": c["id"] in ctx.retrieved,
                    "source": chunk.metadata.get("source") if chunk else None,
                    "page": chunk.metadata.get("page") if chunk else None})
    return out


def build_result(ctx: RunContext, proposal: dict | None, verification: dict | None, attempts: list[dict],
                 submitted_at: float, started_at: float, failure: str | None = None,
                 verifier_enabled: bool = True) -> dict:
    """Decide the route and assemble the response. `failure` is a reason code when the run could not
    complete normally (loop limit, agent error, verification failed twice, ...)."""
    now = time.time()
    verified = bool(verification and verification.get("passed")) if verifier_enabled else True
    route, code, explanation, esc_reason, questions = ROUTE_REVIEW, "", "", None, []
    classification = None
    heading = proposal.get("heading") if proposal else None

    if failure or proposal is None:
        code = failure or "no_proposal"
        explanation = {
            "verification_failed": "The proposed answer could not be verified against the retrieved passages after "
                                   "the allowed attempts, so it is not returned as an answer.",
            "loop_limit": "The agent reached its step, tool-call or time limit before finishing.",
            "run_timeout": "The run exceeded its wall-clock limit before completing.",
            "agent_failure": "An agent failed during the run.",
        }.get(code, "The run did not complete, so no answer is given.")
        esc_reason = code
    elif not verified:
        code, esc_reason = "verification_failed", "verification_failed"
        explanation = ("The proposed answer could not be verified against the retrieved passages after "
                       f"{len(attempts)} attempt(s): " + "; ".join((verification or {}).get("issues", [])[:3]))
    elif proposal.get("tie"):
        code, esc_reason = "tie_after_gri3", "tie_after_gri3"
        explanation = ("Two or more headings remain equally supported after GRI 3, so the system does not choose. "
                       + proposal.get("reasoning_summary", ""))
    elif proposal.get("out_of_scope"):
        code, esc_reason = "out_of_scope", "out_of_scope"
        explanation = proposal.get("reasoning_summary") or "The product is outside Chapters 73 and 96."
    elif proposal.get("needs_clarification"):
        route, code, esc_reason = ROUTE_CLARIFY, "missing_product_information", "missing_product_information"
        questions = list(proposal["needs_clarification"]) + [
            "Please state the material(s) the product is made of.",
            "Please describe what the product is and how it is used (for example, whether it is vacuum-insulated)."]
        questions = list(dict.fromkeys(questions))
        explanation = proposal.get("reasoning_summary") or "Information needed to classify is missing."
    elif heading and proposal.get("confidence") in ("high", "medium"):
        if ctx.degraded:
            code, esc_reason = "degraded_mode", "degraded_mode"
            explanation = (f"A candidate heading ({heading}) was found and verified, but the run used the fallback "
                           f"path ({ctx.degraded_reason}), so it is routed to a specialist instead of cleared.")
        else:
            route = ROUTE_CLEAR
            code = "single_heading_gri1" if proposal.get("gri_applied") == "GRI-1" else "most_specific_gri3a"
            explanation = (f"Heading {heading} was verified: the retrieved heading text names the article and the "
                           f"{proposal.get('gri_applied')} rule text, the applicable notes and every citation resolve to "
                           "retrieved passages. A licensed customs broker makes the final decision.")
    else:
        code, esc_reason = "low_confidence", "low_confidence"
        explanation = "The proposal's confidence is low, so it is routed to a specialist."

    show_cls = bool(heading) and verified and not failure and code in (
        "single_heading_gri1", "most_specific_gri3a", "degraded_mode", "low_confidence")
    if show_cls:
        hp = ctx.retrieved.get(f"HEADING-{heading}") or (ctx.corpus.get(f"HEADING-{heading}").text
                                                          if ctx.corpus.get(f"HEADING-{heading}") else "")
        hm = ctx.corpus.get(f"HEADING-{heading}")
        classification = {
            "heading": heading, "heading_description": hm.metadata.get("desc") if hm else None,
            "chapter": hm.metadata.get("chapter") if hm else None, "gri_applied": proposal.get("gri_applied"),
            "confidence": proposal.get("confidence"), "reasoning_summary": proposal.get("reasoning_summary"),
            "tariff_items": _parse_items(hm.text if hm else hp, ctx.request.get("capacity_litres")),
            "alternatives": proposal.get("alternatives", []),
            "item_selection_note": ("Tariff-item (8-digit) selection is left to the broker; items whose stated capacity "
                                    "limit matches capacity_litres are flagged." if ctx.request.get("capacity_litres") is not None
                                    else "Tariff-item (8-digit) selection is left to the broker."),
        }

    export_policy = _policy_statement(ctx, heading) if classification else None
    unverified = None
    if proposal and not verified:
        unverified = {"heading": heading, "gri_applied": proposal.get("gri_applied"),
                      "note": "unverified candidate shown only to help the reviewer; do not rely on it"}

    escalation = {
        "required": route != ROUTE_CLEAR,
        "to": {ROUTE_REVIEW: "specialist classification reviewer", ROUTE_CLARIFY: "product owner / submitter",
               ROUTE_CLEAR: "licensed customs broker (final decision)"}[route],
        "reason_code": esc_reason, "questions": questions, "unverified_candidate": unverified,
        "broker_decision_required": True,
    }
    if route != ROUTE_CLEAR:
        metrics.escalations.labels(route, esc_reason or "unspecified").inc()

    result = {
        "run_id": ctx.run_id, "route": route,
        "route_basis": {"code": code, "explanation": explanation},
        "classification": classification, "export_policy": export_policy,
        "entities": {"article": proposal.get("article") if proposal else None,
                     "materials": (proposal or {}).get("materials") or []},
        "citations": _citations(ctx, proposal) if (proposal and classification) or (proposal and proposal.get("tie")) or
        (proposal and proposal.get("out_of_scope")) or (proposal and proposal.get("needs_clarification")) else [],
        "escalation": escalation,
        "verification": {"passed": verified, "attempts": len(attempts), "strategies_tried": [a["strategy"] for a in attempts],
                         "final_checks": (verification or {}).get("checks", []), "history": attempts},
        "prompt_versions": ctx.prompt_versions, "degraded": ctx.degraded, "degraded_reason": ctx.degraded_reason or None,
        "limits_hit": ctx.limits_hit,
        "usage": {**ctx.usage.as_dict(), "steps": ctx.steps, "tool_calls": ctx.tool_calls},
        "corpus": {"index_version": ctx.corpus.version, "built_at": ctx.corpus.meta.get("built_at"),
                   "embedding": ctx.corpus.meta.get("embedding")},
        "timings": {"queue_wait_s": round(started_at - submitted_at, 4), "execution_s": round(now - started_at, 4),
                    "end_to_end_s": round(now - submitted_at, 4)},
        "confident": route == ROUTE_CLEAR and verified and not ctx.degraded and not failure,
    }
    result, violations = filter_result(result)
    for v in violations:
        metrics.guardrail_blocks.labels("output", v).inc()
    return result
