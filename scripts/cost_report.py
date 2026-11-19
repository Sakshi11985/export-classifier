"""Cost report (L5): submit N unique runs, then aggregate the per-step usage that every result carries.
Reports cost per 100 runs and the largest single cost driver (a step kind, e.g. classifier.plan).
Prices are whatever the worker was started with (LLM_PRICE_PROMPT_PER_1K / LLM_PRICE_COMPLETION_PER_1K);
token counts are what the provider returned (with the stub they are a character-count estimate)."""
import argparse
import json
import time
import urllib.request


def call(url, body=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("-n", type=int, default=20)
    a = ap.parse_args()
    tag = str(int(time.time()))
    ids = []
    for i in range(a.n):
        while True:
            st, b = call(f"{a.api}/v1/classify", {"product_description": f"Stainless steel vacuum flask with a plastic outer body, cost trial {tag}-{i}."},
                         {"X-Client-Id": f"cost-{tag}-{i % 5}"})
            if st == 429:
                time.sleep(2); continue
            ids.append(b["run_id"]); break
    by_kind: dict[str, dict] = {}
    total = {"prompt": 0, "completion": 0, "cost": 0.0, "calls": 0}
    done = 0
    for rid in ids:
        while True:
            _, r = call(f"{a.api}/v1/runs/{rid}")
            if r["status"] in ("completed", "failed"):
                break
            time.sleep(0.5)
        if r["status"] != "completed":
            continue
        done += 1
        u = r["result"]["usage"]
        total["prompt"] += u["prompt_tokens"]; total["completion"] += u["completion_tokens"]
        total["cost"] += u["cost_usd"]; total["calls"] += u["llm_calls"]
        for step, v in u["by_step"].items():
            k = step.split("#")[0]
            d = by_kind.setdefault(k, {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "calls": 0})
            d["prompt_tokens"] += v["prompt_tokens"]; d["completion_tokens"] += v["completion_tokens"]
            d["cost_usd"] += v["cost_usd"]; d["calls"] += v["calls"]
    per100 = total["cost"] / max(1, done) * 100
    biggest = max(by_kind, key=lambda k: (by_kind[k]["cost_usd"], by_kind[k]["prompt_tokens"] + by_kind[k]["completion_tokens"]))
    rep = {"runs": done, "avg_prompt_tokens": round(total["prompt"] / max(1, done)), "avg_completion_tokens": round(total["completion"] / max(1, done)),
           "avg_llm_calls": round(total["calls"] / max(1, done), 2), "cost_per_100_runs_usd": round(per100, 6),
           "largest_driver": {"step": biggest, "share_of_cost": round(by_kind[biggest]["cost_usd"] / total["cost"], 4) if total["cost"] else None,
                              "share_of_tokens": round((by_kind[biggest]["prompt_tokens"] + by_kind[biggest]["completion_tokens"]) /
                                                       max(1, total["prompt"] + total["completion"]), 4)},
           "by_step_kind": {k: {**v, "cost_usd": round(v["cost_usd"], 6)} for k, v in by_kind.items()}}
    print(json.dumps(rep, indent=2))
    json.dump(rep, open("data/loadtest/cost_report.json", "w"), indent=2)


if __name__ == "__main__":
    main()
