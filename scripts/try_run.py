"""Run one description straight through the agent (no API, no queue). Useful for debugging and for
demonstration item 6, because it prints every verification attempt and the checks that failed.

    python scripts/try_run.py [--fault=KIND] [--json] "<product description>" ["<another>" ...]

Faults (need ENABLE_FAULT_INJECTION=true; they simulate a bad model answer so the verifier is seen working):
    naive_first_pass     classify by material first - the section 4.1 trap (a Chapter 73 heading)
    fabricated_citation  add a citation to a note that was never retrieved
    wrong_product        answer about a different product entirely (a cited vacuum-flask answer)

Under Docker:
    docker compose exec -e ENABLE_FAULT_INJECTION=true worker python scripts/try_run.py --fault=naive_first_pass "..."
"""
import asyncio, json, sys, time
from app.config import get_settings
from app.embeddings import build_embedder
from app.vectorstore import TariffIndex
from app.retrieval import Corpus
from app.redis_util import sync_redis
from app.llm.client import LLMClient
from app.agents.runner import execute_run


def print_attempts(res):
    """Attempt by attempt: what was proposed, whether the verifier accepted it, and why not (A3)."""
    hist = res["verification"].get("history") or []
    if len(hist) <= 1 and res["verification"]["passed"]:
        return
    print("   verification:")
    for a in hist:
        verdict = {True: "PASSED", False: "REJECTED", None: "not verified"}[a.get("verification_passed")]
        print(f"     attempt {a['attempt']} strategy={a['strategy']:<16} proposed heading={a.get('heading')}  -> {verdict}")
        for issue in a.get("issues") or []:
            print(f"        - {issue[:160]}")
    failed = [c["name"] for c in res["verification"].get("final_checks") or [] if not c["passed"]]
    if failed:
        print(f"     final failed checks: {failed}")


async def main(descs, fault="none", show=False):
    s = get_settings(); r = sync_redis(s)
    corpus = Corpus(TariffIndex(s, build_embedder(s, r)), s, r).load()
    llm = LLMClient(s)
    for d in descs:
        t = time.time()
        res, tr, ctx = await execute_run(s, corpus, llm, "run_dbg", {"product_description": d, "options": {"fault": fault}}, time.time())
        c = res["classification"]
        print(f"\n== {d}" + (f"   [fault injected: {fault}]" if fault != "none" else ""))
        print(f"   route={res['route']} basis={res['route_basis']['code']} heading={c and c['heading']} gri={c and c['gri_applied']} "
              f"verified={res['verification']['passed']} attempts={res['verification']['attempts']} steps={ctx.steps} tools={ctx.tool_calls} "
              f"({time.time() - t:.2f}s)")
        print_attempts(res)
        print("   alt:", [(a['heading'], a['status'], a.get('note_id')) for a in (c or {}).get('alternatives', [])])
        print("   cites:", [x['id'] for x in res['citations']], "policy:", res['export_policy'] and res['export_policy']['summary'])
        if show: print(json.dumps(res, indent=1)[:3500])
        else: print("   why:", res['route_basis']['explanation'][:230])


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__); sys.exit(0)
    fault, show = "none", False
    while args and args[0].startswith("--"):
        a = args.pop(0)
        if a.startswith("--fault="): fault = a.split("=", 1)[1]
        elif a == "--json": show = True
        else: print(f"unknown option {a}\n"); print(__doc__); sys.exit(2)
    if not args:
        print("no product description given\n"); print(__doc__); sys.exit(2)
    asyncio.run(main(args, fault, show))
