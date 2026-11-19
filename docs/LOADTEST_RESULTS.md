# Load test, scaling, caching and cost (D6, A7, D7, L5)

Target declared **before** measuring: `docs/SLO.md` (p95 end-to-end <= 20 s at 10 concurrent users, 1 worker; hash in `docs/SLO.sha256`).

## Conditions (be aware of them when reading the numbers)
* First measurement: 2 vCPU, 8 GB, plain processes with `scripts/local_stack.sh` (repeated under Docker Compose afterwards - see the Docker section)
  (redis, chroma server, LLM stub, API, N worker processes, `WORKER_CONCURRENCY=4` each). The same code and env vars as the compose deployment; per-container limits and network hops of compose were **not** measured.
* LLM = `stub/llm_stub.py` through the real `openai_compat` client, `STUB_DELAY_S=1.0` (+-20 %). A run = 4 classifier calls + 1 verifier-judge call = 5 model calls (about 5.6 s of model time).
* Locust: `loadtest/locustfile.py`, each user = submit, poll every 0.7 s until terminal, repeat. Every request unique (cache cold). 60 s per level, all users started at once.
  The measured quantity is the custom row **"RUN end-to-end (submit->result)"** (the submit call alone is not the measurement).
* Locust reports percentiles rounded to coarse buckets for large values (e.g. 16 000 ms), and only runs that **finished inside the window** are counted, so overloaded levels are, if anything, flattered.

## Results (p95 versus the 20 s target)

| Workers | Users | Runs done | Failures | Runs/s | p50 | p95 | SLO |
|---|---|---|---|---|---|---|---|
| 1 | 1 | 10 | 0 | 0.17 | 5.6 s | 5.6 s | met |
| 1 | 5 | 43 | 0 | 0.73 | 5.6 s | 7.0 s | met |
| 1 | **10** (declared load) | 43 | 0 | 0.73 | 12 s | **16 s** | **met** |
| 1 | 15 | 44 | 0 | 0.74 | 19 s | 20 s | borderline (at target, within bucket rounding) |
| 1 | 20 | 38 | 0 | 0.64 | 25 s | 35 s | **not met** |
| 1 | 40 | 28 | 0 | 0.47 | 43 s | 57 s | not met |
| 3 | 10 | 97 | 0 | 1.64 | 5.6 s | 5.7 s | met |
| 3 | 20 | 129 | 0 | 2.19 | 7.8 s | 11 s | met |
| 3 | 30 | 132 | 0 | 2.24 | 11 s | 15 s | met |
| 3 | 40 | 125 | 0 | 2.12 | 16 s | 21 s | **not met** |

**Declared target met: yes** (1 worker, 10 users: p95 16 s <= 20 s, 0 failures).

**Concurrency at which the target fails:** 1 worker - between 15 and 20 users; 3 workers - between 30 and 40 users.

## Repeated under Docker Compose (`docker compose up --scale worker=N`, 2026-10-28)
Same Locust file, same stub (`scripts/set_llm.sh stub`), 60 s per level, against the containers built from this
repository's `Dockerfile`/`docker-compose.yml` (base image: a local stand-in for `python:3.13-slim` because Docker Hub was
blocked in the build environment; 2 vCPU host). Workers were changed with `docker compose up -d --no-deps --scale worker=N worker`.

| Workers | Users | Runs done | Failures | Runs/s | p50 | p95 | SLO |
|---|---|---|---|---|---|---|---|
| 1 | **10** (declared load) | 44 | 0 | 0.75 | 11 s | **15 s** | **met** |
| 1 | 20 | 38 | 0 | 0.64 | 25 s | 36 s | not met |
| 3 | 20 | 133 | 0 | 2.25 | 8.5 s | 10 s | met |
| 3 | 30 | 123 | 0 | 2.08 | 12 s | 17 s | met |
| 3 | 40 | 113 | 0 | 1.91 | 16 s | 26 s | not met |

`docker stats` right after the 40-user level: each worker 2.6-3.4 % CPU and ~105 MiB, api 0.1 % / 129 MiB, chroma 0 %,
redis 0.7 %, stub 1.5 % - the containers are idle while runs queue, which confirms the bottleneck below (run slots, not
CPU, memory or the network hops between containers). The container numbers agree with the process-stack numbers above
within Locust's bucket rounding.

## Repeated on the demonstration laptop (Windows 11 + Docker Desktop, 12 vCPU / 7.4 GB, 2026-10-29)
Same Locust file and stub, run from inside the api container (`demo.py load <users> <seconds>`), 60 s per level.
Worker count changed with `docker compose up -d --no-deps --scale worker=N worker`.

| Workers | Users | Runs done | Failures | Runs/s | p50 | p95 | SLO |
|---|---|---|---|---|---|---|---|
| 3 | 10 | 96 | 0 | 1.63 | 5.7 s | 5.7 s | met |
| 1 | 20 | 44 | 0 | 0.75 | 25.0 s | **26.0 s** | **not met** |
| 3 | 20 | 132 | 0 | 2.24 | 8.5 s | **10.0 s** | met |

**The bottleneck is confirmed by hardware that should have changed it and did not.** This laptop has 12 vCPU; the
development machine had 2. Per-worker throughput is the same on both - 0.75 vs 0.73 runs/s - and the predicted value is
`WORKER_CONCURRENCY` 4 / 5.6 s per run = 0.71 runs/s. Scaling to 3 workers gives 2.24 runs/s on this laptop and 2.2 on the
2-core machine, i.e. 3.0x in both cases. Six times the CPU buys nothing, because a run is a *sequential* chain of ~5 model
calls and a worker holds only 4 of them at once; the limit is run slots, not compute. The remedy is more workers (or a
higher `WORKER_CONCURRENCY`) until the model provider's own rate limit becomes the binding constraint.

## Effect of `--scale worker=3` (A7)
Throughput plateau 0.73 -> 2.2 runs/s (**3.0x**, linear); at 20 users p95 35 s -> 11 s (processes), 36 s -> 10 s (Docker, 2 vCPU) and 26 s -> 10 s (demonstration laptop, 12 vCPU). Scaling helps because the bottleneck is per-worker capacity, below.

## Bottleneck (the component responsible)
**Agent-worker run slots.** Each worker executes at most `WORKER_CONCURRENCY=4` runs at once and a run is a strictly sequential chain of 5 model calls of ~1.1 s,
so one worker saturates at 4 / 5.6 s = 0.71 runs/s - exactly the measured plateau 0.73. Beyond that, runs wait in the Redis queue (latency = queueing, not slower execution).
Evidence it is not CPU/IO: during a 40-user run with 3 workers `top` showed no Python process above ~10 % CPU on 2 cores; API calls stayed at p50 3-5 ms (`GET /v1/runs/{id}`);
Redis and Chroma were idle. With a real provider the same structure holds, with the provider's latency and rate limit replacing the stub delay - raise `WORKER_CONCURRENCY`/workers until the provider's limit becomes the bottleneck.
At 3 workers the plateau is 2.2 runs/s = 3 x 0.73, so the same law applies; the two sandbox cores were not the limit up to 40 users, but at higher counts the single stub process and the 2 cores would become the next limit (not measured).

## Caching (D7) - `scripts/cache_effect.py`, 10 distinct requests, then the same 10 again
| | cold | whole-run cache hit |
|---|---|---|
| p50 / p95 latency (one client, sequential) | 5.15 s / 5.45 s | 0.003 s / 0.004 s |
| model calls made | 50 | **0** |
| tokens | 161 300 | 0 |
Cache hits cost nothing and take milliseconds. Justification: classification requests repeat (same SKU, same description) and the answer is a pure function of (request, index version, prompt variant, provider); the key includes all four so a re-ingest or prompt change invalidates it; degraded results are never cached.
Retrieval and embedding caches (Redis) exist as well and show in `cache_hits_total{kind}`; their effect is small here (hashing embeddings are cheap) and was **not** isolated.
Locust in `cached` mode (`LOCUST_MODE=cached`) mostly returned 429: the per-client rate limiter (30/min, burst 10) correctly stopped one client looping on cache hits (163 of 349 submissions) - a limiter result, not a latency result.

## Cost per 100 runs (L5) - `scripts/cost_report.py`, 20 runs
Prices are **illustrative placeholders supplied through env** (0.00015 USD / 1k prompt tokens, 0.0006 / 1k completion tokens), not a quote from any provider; token counts come from the stub (character-count estimate / 4).
| | value |
|---|---|
| average per run | 15 428 prompt + 583 completion tokens, 5 model calls |
| **cost per 100 runs** | **USD 0.266** at those prices |
| **largest single driver** | `classifier.plan` steps: **88 % of cost, 87 % of tokens** (4 of the 5 calls; each re-sends the growing tool transcript - prompt tokens grow 703 -> 2 397 -> 5 016 -> 5 225) |
Per-step numbers are in every result (`usage.by_step`) and in `docs/results/cost_report.json`. Levers: fewer steps (the loop bounds), shorter tool outputs (`TEXT_LIMIT`), whole-run cache.
