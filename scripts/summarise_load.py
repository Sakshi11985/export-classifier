"""Print RPS / p50 / p95 / failures of the end-to-end run row of a Locust *_stats.csv and compare with the SLO."""
import csv
import sys

SLO_P95_MS = 20000.0  # docs/SLO.md


def main(path: str, users: str) -> None:
    rows = list(csv.DictReader(open(path)))
    run = next((r for r in rows if r["Name"].startswith("RUN end-to-end")), None)
    if not run:
        print("no RUN row in", path); return
    n, fails = int(run["Request Count"]), int(run["Failure Count"])
    p50, p95, p99 = float(run["50%"]), float(run["95%"]), float(run["99%"])
    rps = float(run["Requests/s"])
    ok = p95 <= SLO_P95_MS and (fails / max(1, n)) < 0.01
    print(f"users={users:>3}  runs={n:>4}  fail={fails:>3}  runs/s={rps:6.2f}  p50={p50/1000:6.2f}s  p95={p95/1000:6.2f}s  "
          f"p99={p99/1000:6.2f}s  SLO(p95<=20s,<1% fail): {'MET' if ok else 'NOT MET'}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "?")
