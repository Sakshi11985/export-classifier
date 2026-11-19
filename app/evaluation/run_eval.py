"""Batch evaluation from the command line (L3).

    python -m app.evaluation.run_eval --label after
    python -m app.evaluation.run_eval --label before --strategy baseline_single_pass --no-verifier
    python -m app.evaluation.run_eval --compare before after            # print a side by side table

Each scenario in eval_set.jsonl has an expected outcome and the passages that support it. Scenarios that
must be rejected or blocked go through the same validation and guardrail code as the API. The report
contains task metrics (route / heading accuracy) and the four RAG metrics defined in scoring.py."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from pydantic import ValidationError

from .. import metrics as prom
from ..config import Settings, get_settings
from ..contracts import ClassificationRequest
from ..embeddings import build_embedder
from ..guardrails import scan_request_fields
from ..llm.client import LLMClient
from ..logging_setup import setup_logging
from ..redis_util import sync_redis
from ..retrieval import Corpus
from ..vectorstore import TariffIndex
from ..agents.runner import execute_run
from .scoring import aggregate, score_run

HERE = Path(__file__).parent
REPORTS = Path("data/eval_reports")


def load_set(path: Path | None = None) -> list[dict]:
    return [json.loads(l) for l in (path or HERE / "eval_set.jsonl").read_text().splitlines() if l.strip()]


async def run_scenario(s: Settings, corpus, llm, sc: dict, strategy: str | None, verifier: bool) -> dict:
    inp = sc["input"]
    exp = sc["expected"]
    t0 = time.time()
    row = {"id": sc["id"], "tag": sc.get("tag", ""), "input": inp["product_description"][:80], "expected": exp}
    try:
        req = ClassificationRequest.model_validate(inp)
    except ValidationError:
        row.update(outcome="rejected", route=None, heading=None, passed=exp["outcome"] == "rejected",
                   agent_run_started=False, latency_s=round(time.time() - t0, 3))
        return row
    fields = req.model_dump(mode="json", exclude_none=True)
    g = scan_request_fields(fields)
    if not g.allowed:
        row.update(outcome="blocked", route=None, heading=None, passed=exp["outcome"] == "blocked",
                   agent_run_started=False, categories=g.categories, latency_s=round(time.time() - t0, 3))
        return row
    res, trace, ctx = await execute_run(s, corpus, llm, f"run_eval_{sc['id']}", fields, time.time(), strategy=strategy,
                                        verifier_enabled=verifier)
    cls = res.get("classification")
    head = cls["heading"] if cls else None
    route = res["route"]
    outcome = {"clear-for-filing": "classified", "seek-product-clarification": "clarification"}.get(route, "escalated")
    ok = (route == exp["route"]) and (head == exp.get("heading"))
    if exp.get("reason"):
        ok = ok and res["route_basis"]["code"] == exp["reason"]
    sc_scores = score_run(res, ctx.retrieved, fields, corpus, sc.get("supporting"))
    row.update(outcome=outcome, route=route, heading=head, basis=res["route_basis"]["code"], passed=ok,
               agent_run_started=True, verified=res["verification"]["passed"], attempts=res["verification"]["attempts"],
               steps=ctx.steps, tool_calls=ctx.tool_calls, cost_usd=res["usage"]["cost_usd"],
               prompt_tokens=res["usage"]["prompt_tokens"], completion_tokens=res["usage"]["completion_tokens"],
               cost_by_step=res["usage"]["by_step"], latency_s=round(time.time() - t0, 3), scores=sc_scores,
               confident=res["confident"])
    return row


async def run_all(s: Settings, label: str, strategy: str | None, verifier: bool, only: list[str] | None) -> dict:
    r = sync_redis(s)
    corpus = Corpus(TariffIndex(s, build_embedder(s, r, prom)), s, r).load()
    llm = LLMClient(s)
    scenarios = [x for x in load_set() if not only or x["id"] in only]
    rows = []
    for sc in scenarios:
        rows.append(await run_scenario(s, corpus, llm, sc, strategy, verifier))
    agent_rows = [x for x in rows if x.get("agent_run_started")]
    scores = aggregate([x["scores"] for x in agent_rows])
    wrong_confident = [x["id"] for x in agent_rows if x["confident"] and not x["passed"]]
    summary = {
        "label": label, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": {"strategy": strategy or s.first_strategy, "verifier_enabled": verifier, "prompt_variant": s.prompt_variant,
                   "llm_provider": s.llm_provider, "embedding_backend": s.embedding_backend,
                   "index_version": corpus.version},
        "scenarios": len(rows), "passed": sum(1 for x in rows if x["passed"]),
        "pass_rate": round(sum(1 for x in rows if x["passed"]) / max(1, len(rows)), 4),
        "heading_accuracy_on_classified": _acc([x for x in rows if x["expected"]["outcome"] == "classified"]),
        "route_accuracy": round(sum(1 for x in agent_rows if x["route"] == x["expected"]["route"]) / max(1, len(agent_rows)), 4),
        "confident_wrong_answers": wrong_confident,
        "metrics": scores,
        "cost": {"total_usd": round(sum(x.get("cost_usd", 0) for x in agent_rows), 6),
                 "per_100_runs_usd": round(sum(x.get("cost_usd", 0) for x in agent_rows) / max(1, len(agent_rows)) * 100, 6),
                 "largest_step_driver": _largest_driver(agent_rows)},
        "avg_steps": round(sum(x["steps"] for x in agent_rows) / max(1, len(agent_rows)), 2),
        "avg_tool_calls": round(sum(x["tool_calls"] for x in agent_rows) / max(1, len(agent_rows)), 2),
        "rows": rows,
    }
    return summary


def _acc(rows):
    rows = [x for x in rows]
    return round(sum(1 for x in rows if x.get("heading") == x["expected"].get("heading")
                     and x.get("route") == x["expected"]["route"]) / max(1, len(rows)), 4)


def _largest_driver(rows) -> dict:
    agg: dict[str, float] = {}
    toks: dict[str, int] = {}
    for x in rows:
        for step, v in (x.get("cost_by_step") or {}).items():
            k = step.split("#")[0]
            agg[k] = agg.get(k, 0) + v["cost_usd"]
            toks[k] = toks.get(k, 0) + v["prompt_tokens"] + v["completion_tokens"]
    if not agg:
        return {"step": None, "note": "no model cost recorded (offline provider or zero price)"}
    k = max(agg, key=lambda a: (agg[a], toks[a]))
    return {"step": k, "cost_usd": round(agg[k], 6), "tokens": toks[k]}


def save(summary: dict) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    p = REPORTS / f"{summary['label']}.json"
    p.write_text(json.dumps(summary, indent=2))
    return p


def print_summary(sm: dict) -> None:
    print(f"\n=== {sm['label']}  ({sm['config']})")
    print(f"scenarios {sm['scenarios']}  passed {sm['passed']}  pass_rate {sm['pass_rate']}  "
          f"route_acc {sm['route_accuracy']}  confident_wrong {sm['confident_wrong_answers']}")
    for k in ("groundedness", "context_relevance", "faithfulness", "hallucination", "context_recall"):
        print(f"  {k:18s} {sm['metrics'][k]}  (n={sm['metrics'][k + '_n']})")
    print(f"  avg steps {sm['avg_steps']}  avg tool calls {sm['avg_tool_calls']}  cost/100 runs ${sm['cost']['per_100_runs_usd']}")
    for r in sm["rows"]:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"  [{mark}] {r['id']} {r['tag']:3s} exp={r['expected']['route'] or r['expected']['outcome']}/{r['expected'].get('heading')} "
              f"got={r.get('route') or r['outcome']}/{r.get('heading')} basis={r.get('basis', '-')}  :: {r['input']}")


def compare(a: str, b: str) -> int:
    A = json.loads((REPORTS / f"{a}.json").read_text())
    B = json.loads((REPORTS / f"{b}.json").read_text())
    print(f"\n{'metric':24s}{a:>14s}{b:>14s}{'delta':>10s}")
    rows = [("pass_rate", A["pass_rate"], B["pass_rate"]), ("route_accuracy", A["route_accuracy"], B["route_accuracy"])]
    for k in ("groundedness", "context_relevance", "faithfulness", "hallucination", "context_recall"):
        rows.append((k, A["metrics"][k], B["metrics"][k]))
    rows.append(("confident_wrong_answers", len(A["confident_wrong_answers"]), len(B["confident_wrong_answers"])))
    rows.append(("avg_steps", A["avg_steps"], B["avg_steps"]))
    rows.append(("avg_tool_calls", A["avg_tool_calls"], B["avg_tool_calls"]))
    for k, x, y in rows:
        d = "" if x is None or y is None else f"{y - x:+.4f}"
        print(f"{k:24s}{str(x):>14s}{str(y):>14s}{d:>10s}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--strategy", default=None, help="default|article_first|baseline_single_pass ...")
    ap.add_argument("--no-verifier", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--compare", nargs=2)
    ap.add_argument("--min-pass-rate", type=float, default=None, help="exit 1 if the pass rate is lower")
    a = ap.parse_args(argv)
    if a.compare:
        return compare(*a.compare)
    s = get_settings()
    setup_logging("eval", "WARNING")
    sm = asyncio.run(run_all(s, a.label, a.strategy, not a.no_verifier, [x for x in a.only.split(",") if x] or None))
    p = save(sm)
    print_summary(sm)
    print(f"\nreport written to {p}")
    if a.min_pass_rate is not None and sm["pass_rate"] < a.min_pass_rate:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
