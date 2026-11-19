"""Measure the effect of the whole-run cache (D7): N cold runs, then the same N requests again (cache hits).
Uses one client sequentially, spaced to stay under the rate limit. Counts real model calls through the stub's
counter, so cost avoided is measured, not inferred.

  python scripts/cache_effect.py --api http://localhost:8000 --stub http://localhost:8090 -n 12
Price per 1k tokens comes from the same env vars the worker uses (LLM_PRICE_PROMPT_PER_1K / _COMPLETION_)."""
import argparse
import json
import statistics
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


def one(api, desc, cid):
    t0 = time.perf_counter()
    st, b = call(f"{api}/v1/classify", {"product_description": desc, "materials": ["stainless steel", "plastic"]}, {"X-Client-Id": cid})
    if st == 429:
        time.sleep(b.get("retry_after_s", 2) + 0.2)
        return one(api, desc, cid)
    while b["status"] not in ("completed", "failed"):
        time.sleep(0.3)
        _, b = call(f"{api}/v1/runs/{b['run_id']}")
    return time.perf_counter() - t0, b


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--stub", default="http://localhost:8090")
    ap.add_argument("-n", type=int, default=12)
    a = ap.parse_args()
    tag = str(int(time.time()))
    descs = [f"Stainless steel vacuum flask with a moulded plastic outer body, cache trial {tag}-{i}." for i in range(a.n)]
    out = {}
    for phase in ("cold", "cached"):
        calls0 = call(f"{a.stub}/health")[1]["calls"]
        lat, hits, cost, toks = [], 0, 0.0, 0
        for d in descs:
            t, b = one(a.api, d, f"cache-{tag}")
            lat.append(t)
            hits += bool(b.get("cache_hit"))
            cost += b["result"]["usage"]["cost_usd"] if phase == "cold" else 0.0
            if phase == "cold":
                toks += b["result"]["usage"]["prompt_tokens"] + b["result"]["usage"]["completion_tokens"]
        calls1 = call(f"{a.stub}/health")[1]["calls"]
        out[phase] = {"runs": len(lat), "p50_s": round(statistics.median(lat), 3), "p95_s": round(pct(lat, 95), 3),
                      "cache_hits": hits, "model_calls_made": calls1 - calls0, "cost_usd_incurred": round(cost, 6),
                      "tokens": toks}
    print(json.dumps(out, indent=2))
    json.dump(out, open("data/loadtest/cache_effect.json", "w"), indent=2)


if __name__ == "__main__":
    main()
