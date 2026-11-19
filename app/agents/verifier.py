"""Verifier agent (A2, A3): a separate agent with its own responsibility, prompt and tool use.

It does not trust the classifier. Before any answer is returned it checks:
  citations_resolve        every cited id is a passage the classifier actually retrieved
  quotes_verbatim          every quote is a verbatim substring of that retrieved passage
  heading_supported        the chosen heading's own passage was retrieved and cited, chapter is in scope
  rule_cited               the GRI rule claimed was retrieved and cited
  notes_considered         the notes applying to the chosen heading were retrieved
  exclusions_supported     every 'excluded_by_note' alternative cites a retrieved note that really names
                           the chosen heading's chapter or heading
  named_heading_considered the verifier runs ITS OWN article-name search; a heading that names the article
                           and was neither chosen nor explained is a failure (the section 4.1 failure mode)
  article_grounded_in_request  the article the proposal classifies is actually named in the request
  heading_matches_request  the chosen heading names the request's article (verifier's own search), or is a
                           Chapter 73 material heading and the request states iron/steel
  flags_coherent           out_of_scope / clarification / tie flags agree with independent evidence
  output_clean             no forbidden claims in the free text
  llm_judge                (real providers) an independent model call must also judge the proposal supported
A failed verification names the next search strategy to try."""
from __future__ import annotations

import json
import re

from .. import metrics
from ..entities import IRON_STEEL, article_tokens, extract_entities
from ..guardrails import scan_output_text, wrap_untrusted
from ..prompts.registry import active
from ..textutil import normalise, squash, stem
from ..telemetry import span
from ..tools import execute_tool
from .classifier import extract_json
from .context import RunContext

IN_SCOPE = (73, 96)


def _chk(name: str, ok: bool, detail: str = "") -> dict:
    return {"name": name, "passed": bool(ok), "detail": detail}


async def verify_proposal(ctx: RunContext, proposal: dict, attempt: int) -> dict:
    ctx.trace.emit("agent_start", agent="verifier", attempt=attempt)
    prompt = active("verifier", "v1")
    ctx.prompt_versions["verifier"] = prompt["ref"] + ":" + prompt["sha256"]
    checks: list[dict] = []
    retr = ctx.retrieved
    cits = proposal.get("citations") or []
    heading = proposal.get("heading")
    cited_ids = {c["id"] for c in cits}

    # --- citations resolve + verbatim
    unresolved = [c["id"] for c in cits if c["id"] not in retr]
    checks.append(_chk("citations_resolve", not unresolved,
                       f"unresolved citation ids: {unresolved}" if unresolved else f"{len(cits)} citation(s) resolve to retrieved text"))
    bad_quotes = [c["id"] for c in cits if c["id"] in retr and squash(c["quote"]) not in squash(retr[c["id"]])]
    checks.append(_chk("quotes_verbatim", not bad_quotes,
                       f"quote not found in retrieved passage for: {bad_quotes}" if bad_quotes else "all quotes are verbatim"))

    # --- structural support for a classification
    ent = extract_entities(ctx.request.get("product_description", ""), ctx.request)
    if heading:
        chap = None
        hp = retr.get(f"HEADING-{heading}")
        if hp:
            m = re.match(r"Heading \d{4} \(Chapter (\d+)\)", hp)
            chap = int(m.group(1)) if m else None
        ok = bool(hp) and f"HEADING-{heading}" in cited_ids and chap in IN_SCOPE
        checks.append(_chk("heading_supported", ok,
                           f"heading {heading} passage retrieved={bool(hp)}, cited={f'HEADING-{heading}' in cited_ids}, chapter={chap}"))
        gri = proposal.get("gri_applied")
        ok = bool(gri) and gri in retr and gri in cited_ids
        checks.append(_chk("rule_cited", ok, f"{gri} retrieved={gri in retr if gri else False}, cited={gri in cited_ids if gri else False}"))
        notes_ok = True
        detail = "notes for the chosen heading were retrieved"
        if hp and chap:
            sec_notes = [k for k in retr if k.startswith("SEC-") or k.startswith(f"CH{chap}-NOTE")]
            ch_notes_exist = ctx.corpus.get(f"CH{chap}-NOTE-1") is not None
            sec_exist = any(k.startswith("SEC-") for k in ctx.corpus.chunks) and chap == 73
            if (ch_notes_exist or sec_exist) and not sec_notes:
                notes_ok, detail = False, "no applicable section/chapter note was retrieved for the chosen heading"
        checks.append(_chk("notes_considered", notes_ok, detail))
        bad = []
        for alt in proposal.get("alternatives") or []:
            if alt.get("status") == "excluded_by_note":
                nid = alt.get("note_id")
                txt = retr.get(nid or "", "")
                chapters = {int(x) for x in re.findall(r"Chapter (\d{1,2})", txt)}
                heads = set(re.findall(r"headings? (\d{4})", txt))
                if not nid or nid not in cited_ids or not txt or not (chap in chapters or heading in heads):
                    bad.append(alt.get("heading"))
        na_bad = [a.get("heading") for a in proposal.get("alternatives") or []
                  if a.get("status") == "not_applicable" and f"HEADING-{a.get('heading')}" not in retr]
        bad += na_bad
        checks.append(_chk("exclusions_supported", not bad,
                           f"exclusion not supported by a cited retrieved note for: {bad}" if bad else "exclusions are supported by cited notes"))

    # --- independent article search (the verifier's own tool use)
    strong: dict[str, dict] = {}
    for a in ent.articles:
        res, meta = await execute_tool(ctx, "search_headings_by_article", {"article": a.text}, ctx.settings.tool_timeout_s)
        ctx.tool_calls += 1
        ctx.trace.emit("tool_call", agent="verifier", name="search_headings_by_article", args={"article": a.text},
                       status=meta["status"], latency_ms=meta["latency_ms"],
                       passages=[c["chunk_id"] for c in res.get("candidates", [])[:3]], attempt=attempt)
        metrics.step_latency.labels("verifier", "tool:search_headings_by_article").observe(meta["latency_ms"] / 1000)
        for c in res.get("candidates", []):
            if c["strong"]:
                strong[c["heading"]] = c
    explained = {a.get("heading") for a in proposal.get("alternatives") or []}
    if heading:
        missing = [h for h in strong if h != heading and h not in explained]
        checks.append(_chk("named_heading_considered", not missing,
                           f"heading(s) {missing} name the article but were neither chosen nor explained" if missing
                           else "no unexplained heading names the article"))

        # --- the answer must be about THIS request (catches a model answering a different product,
        #     e.g. a hallucinated or cached 'vacuum flask' answer to 'cotton t-shirt')
        req_tokens = {stem(w) for w in re.findall(r"[a-z]+", normalise(ctx.request.get("product_description", "")))}
        prop_article = proposal.get("article") or ""
        prop_toks = article_tokens(prop_article)
        grounded = bool(prop_toks) and prop_toks[-1] in req_tokens
        checks.append(_chk("article_grounded_in_request", grounded,
                           f"proposed article '{prop_article}' is named in the request" if grounded else
                           f"proposed article '{prop_article}' does not appear in the request description"))

        # --- the chosen heading must name the request's article (verifier's own search), or be a
        #     material-based Chapter 73 heading for an iron/steel article
        req_steel = bool(set(ent.materials) & IRON_STEEL)
        by_name = heading in strong
        by_material = str(heading).startswith("73") and req_steel
        checks.append(_chk("heading_matches_request", by_name or by_material,
                           f"heading {heading} names the request's article" if by_name else
                           f"heading {heading} is a Chapter 73 heading and the request states iron/steel" if by_material else
                           f"heading {heading} neither names the request's article nor matches its stated material"))
    # --- flag coherence
    coherent, why = True, "flags agree with independent evidence"
    if proposal.get("out_of_scope") and strong:
        coherent, why = False, f"marked out_of_scope but headings {sorted(strong)} name the article"
    if proposal.get("needs_clarification") and len(strong) == 1 and not heading:
        coherent, why = False, f"clarification requested but heading {sorted(strong)} unambiguously names the article"
    if proposal.get("tie") and len([a for a in proposal.get("alternatives") or [] if a.get("status") == "tied"]) < 2:
        coherent, why = False, "tie declared without two tied headings"
    if not heading and not (proposal.get("out_of_scope") or proposal.get("needs_clarification") or proposal.get("tie")):
        coherent, why = False, "no heading and no escalation flag"
    checks.append(_chk("flags_coherent", coherent, why))

    out = scan_output_text(proposal.get("reasoning_summary", ""))
    checks.append(_chk("output_clean", out.allowed, out.detail or "no forbidden claims"))

    # --- independent model judgement (real providers only; offline policy cannot judge)
    judge = None
    if ctx.settings.llm_provider != "offline" and all(c["passed"] for c in checks):
        ctx.steps += 1
        step = f"verifier.judge#{ctx.steps}"
        msgs = [{"role": "system", "content": prompt["text"]},
                {"role": "user", "content": wrap_untrusted(json.dumps({
                    "request": ctx.request, "proposal": proposal, "passages": retr}))[:24000]}]
        with span("agent.verifier.llm", step=step, run_id=ctx.run_id):
            resp = await ctx.llm.chat("verifier", step, msgs, None, ctx)
        ctx.trace.emit("llm_call", agent="verifier", step=step, prompt_version=prompt["ref"], prompt_sha=prompt["sha256"],
                       provider=resp.provider, latency_ms=round(resp.latency_ms, 2), prompt_tokens=resp.prompt_tokens,
                       completion_tokens=resp.completion_tokens, cost_usd=round(resp.cost_usd, 8),
                       usage_estimated=resp.estimated_usage, retries=resp.retries, fallback=resp.fallback_used,
                       tool_calls=[], attempt=attempt)
        judge = extract_json(resp.content)
        ok = bool(judge) and judge.get("supported") is True
        checks.append(_chk("llm_judge", ok, "; ".join(judge.get("issues", [])) if judge else "judge returned no verdict (fail closed)"))

    passed = all(c["passed"] for c in checks)
    failed = [c for c in checks if not c["passed"]]
    nxt = None
    if not passed:
        names = {c["name"] for c in failed}
        nxt = "article_first" if names & {"named_heading_considered", "flags_coherent", "exclusions_supported", "heading_supported",
                                          "article_grounded_in_request", "heading_matches_request"} \
            else "exhaustive_notes"
    report = {"passed": passed, "attempt": attempt, "checks": checks, "next_strategy": nxt,
              "issues": [f"{c['name']}: {c['detail']}" for c in failed]}
    ctx.trace.emit("verification", agent="verifier", attempt=attempt, passed=passed, checks=checks,
                   next_strategy=nxt, steps=ctx.steps, tool_calls=ctx.tool_calls)
    metrics.verifications.labels("pass" if passed else "fail").inc()
    metrics.agent_tasks.labels("verifier", "success").inc()
    return report
