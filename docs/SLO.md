# Service Level Objective (declared BEFORE any load measurement, requirement D6)

> This file was written and its SHA-256 recorded in `docs/SLO.sha256` **before** `loadtest/` was ever
> executed. The git history (or file timestamps) of this file and of `data/loadtest/` show the order.
> Results are in `docs/LOADTEST_RESULTS.md`; this file is not edited after measuring.

## Declared objective

| Item | Target |
|---|---|
| What is measured | One *complete run*: from the moment the client sends `POST /v1/classify` until a poll of `GET /v1/runs/{id}` first returns a terminal status (`completed`, `failed`). The submit call alone is **not** the measurement. |
| **p95 end-to-end latency** | **<= 20 seconds** |
| Reference load | 10 concurrent simulated users, each submitting, polling every ~0.7 s (the `poll_after_ms` the API returns), then submitting again. One `worker` container (default `WORKER_CONCURRENCY=4`). Cache-cold requests (every request unique). |
| LLM during the load test | The stub service (`stub/llm_stub.py`), fixed responses, `STUB_DELAY_S=1.0` (+-20 % jitter) per model call. |
| Error budget | < 1 % of runs end `failed` or time out; HTTP 5xx rate < 1 %. |
| Availability | `/health/ready` succeeds >= 99 % of the time during the test. |

## Justification against section 1 of the specification

* Section 1 says classification is done today by a trade-compliance analyst who *reads the description,
  applies the GRI in order, checks notes, and determines the policy*. That is minutes of expert time per
  line item, and the consequence of a mistake is a held consignment, not a lost web page. A tool that returns a
  reviewed proposal in well under half a minute is a clear improvement; one that takes several minutes is not
  better than asking a colleague.
* Section A4 says runs take "tens of seconds" and must be asynchronous. A 20 s p95 sits at the upper end of
  "tens of seconds" and is the longest an analyst will wait with the page open before switching task.
* Arithmetic: a default run makes 4 classifier model calls and 1 independent verifier-judge call. At the stub's
  1.0 s per call that is ~5 s of pure model latency per run, plus retrieval/verification work (tens of ms) and
  polling granularity (0.7 s). A retried run (verifier rejected the first attempt) costs roughly double.
  An unloaded p95 of ~6-12 s is therefore expected; 20 s leaves headroom for queueing under the reference load
  and for the one-retry path, and is **not** chosen to be easy: a real provider at 2-3 s per call would put an
  unretried run at 10-15 s.
* The target is deliberately tied to the stub delay. With a real provider the p95 is dominated by provider
  latency; this SLO would have to be re-declared against the provider's measured latency.

## What counts against the SLO

Runs that return a *degraded* or *escalated* result still count (they are results). Rejected (422) and blocked
(400) requests never start an agent run and are excluded from the run-latency population but counted in RPS and
in the error rate only if they were unexpected.
