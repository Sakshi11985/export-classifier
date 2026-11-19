"""F3-F7, A1-A3: the evaluation set run through the real agent graph (offline policy, real retrieval),
plus verification-catches-unsupported-answer, loop bounds and the section 4.1 reference answer."""
import json
import time
from pathlib import Path

import pytest

from app.agents.runner import execute_run
from app.evaluation.run_eval import load_set, run_scenario

SC = load_set()


def test_eval_set_meets_spec():
    assert len(SC) >= 20
    assert any("vacuum flask" in s["input"]["product_description"].lower() for s in SC)
    for s in SC:
        assert "expected" in s and "supporting" in s or s["expected"]["outcome"] in ("rejected", "blocked")


@pytest.mark.parametrize("sc", SC, ids=[s["id"] for s in SC])
async def test_eval_scenario(sc, corpus, settings):
    from app.llm.client import LLMClient
    row = await run_scenario(settings, corpus, LLMClient(settings), sc, None, True)
    assert row["passed"], row
    if row["outcome"] in ("rejected", "blocked"):
        assert row["agent_run_started"] is False                # D1: nothing reached an agent
    else:
        assert row["verified"] is True


async def run(settings, corpus, desc, **extra):
    from app.llm.client import LLMClient
    return await execute_run(settings, corpus, LLMClient(settings), "run_t", {"product_description": desc, **extra}, time.time())


async def test_section_4_1_reference_answer(settings, corpus):
    res, trace, ctx = await run(settings, corpus, "Stainless steel vacuum flask with a moulded plastic outer body.")
    assert res["route"] == "clear-for-filing" and res["classification"]["heading"] == "9617"
    assert res["classification"]["gri_applied"] == "GRI-1" and res["route_basis"]["code"] == "single_heading_gri1"
    cited = {c["id"] for c in res["citations"]}
    assert {"GRI-1", "HEADING-9617", "SEC-XV-NOTE-1"} <= cited
    for c in res["citations"]:                                    # every citation resolves to retrieved text
        assert c["id"] in ctx.retrieved
    assert res["export_policy"] is not None and res["verification"]["passed"]
    assert "no declaration has been filed" in res["determination_authority"]
    tools = {e["name"] for e in trace.events if e["type"] == "tool_call"}
    assert {"search_headings_by_article"} <= tools and len(tools) >= 3


async def test_naive_material_first_answer_is_caught_by_verifier_then_corrected(settings, corpus):
    res, trace, ctx = await run(settings, corpus, "Stainless steel vacuum flask with a moulded plastic outer body.",
                                options={"fault": "naive_first_pass"})
    verifs = [e for e in trace.events if e["type"] == "verification"]
    assert len(verifs) >= 2 and verifs[0]["passed"] is False       # first (material-first) answer rejected
    failed = {c["name"] for c in verifs[0]["checks"] if not c["passed"]}
    assert {"named_heading_considered"} & failed
    assert verifs[-1]["passed"] is True and res["classification"]["heading"] == "9617"
    assert res["verification"]["attempts"] >= 2


async def test_fabricated_citation_is_caught(settings, corpus):
    res, trace, ctx = await run(settings, corpus, "Stainless steel vacuum flask with a moulded plastic outer body.",
                                options={"fault": "fabricated_citation"})
    v0 = [e for e in trace.events if e["type"] == "verification"][0]
    assert v0["passed"] is False
    assert {"citations_resolve", "quotes_verbatim"} & {c["name"] for c in v0["checks"] if not c["passed"]}
    assert res["route"] != "clear-for-filing" or res["verification"]["passed"]     # never an unverified confident answer


async def test_answer_for_a_different_product_is_caught_then_corrected(settings, corpus):
    res, trace, ctx = await run(settings, corpus, "Stainless steel kitchen sink", options={"fault": "wrong_product"})
    v0 = [e for e in trace.events if e["type"] == "verification"][0]
    assert v0["passed"] is False
    assert {"article_grounded_in_request", "heading_matches_request"} & {c["name"] for c in v0["checks"] if not c["passed"]}
    assert res["classification"] is None or res["classification"]["heading"] != "9617"


async def test_verifier_disabled_baseline_can_be_wrong_which_proves_the_check_matters(settings, corpus):
    from app.llm.client import LLMClient
    res, _, _ = await execute_run(settings, corpus, LLMClient(settings), "run_b", {"product_description": "Cotton t-shirt"}, time.time(),
                                  strategy="baseline_single_pass", verifier_enabled=False)
    assert res["route"] == "clear-for-filing"                      # the unverified baseline answers anything
    res2, _, _ = await run(settings, corpus, "Cotton t-shirt")
    assert res2["route"] == "specialist-classification-review" and res2["route_basis"]["code"] == "out_of_scope"


async def test_loop_bounds_force_escalation(settings, corpus, monkeypatch):
    monkeypatch.setenv("AGENT_MAX_STEPS", "1")
    from app.config import get_settings
    s = get_settings()
    res, _, ctx = await run(s, corpus, "Stainless steel vacuum flask with a moulded plastic outer body.")
    assert res["route"] == "specialist-classification-review" and not res["confident"]
    assert ctx.limits_hit


async def test_wall_clock_bound(settings, corpus, monkeypatch):
    monkeypatch.setenv("AGENT_RUN_TIMEOUT_S", "0.001")
    from app.config import get_settings
    res, _, _ = await run(get_settings(), corpus, "Stainless steel vacuum flask with a moulded plastic outer body.")
    assert res["route"] == "specialist-classification-review" and not res["confident"]


async def test_escalation_not_fabrication_for_out_of_corpus(settings, corpus):
    res, _, _ = await run(settings, corpus, "Cotton t-shirt")
    assert res["classification"] is None and res["route_basis"]["code"] == "out_of_scope"
    assert res["export_policy"] is None and not res["confident"]


async def test_tie_escalates(settings, corpus):
    res, _, _ = await run(settings, corpus, "Set containing a plastic hair comb and a stainless steel pot scourer")
    assert res["route"] == "specialist-classification-review" and res["route_basis"]["code"] == "tie_after_gri3"
