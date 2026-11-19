# Architecture and design decisions

## 1. Topology (deployment boundaries marked with `||`)

```
 browser / curl                                                   observability plane
      |                                                           ┌───────────────────────────────────┐
      v                                                           │ Prometheus <- api, worker*, cAdvisor│
┌───────────┐  validate, guardrail, rate-limit,   ┌───────────┐   │ Grafana (dashboard)  Jaeger (OTLP)  │
│   api     │──quarantine rejects──────────────── │   redis   │   └───────────────────────────────────┘
│ (FastAPI) │──enqueue run id (LPUSH)────────────>│ queue:runs│
│           │<─poll status / trace────────────────│ run store │
└───────────┘                                     │ caches    │
      ||                                          │ rate limit│
      ||                                          │ traces    │
      ||                                          └─────┬─────┘
      ||                                   BLMOVE claim │ (reliable queue, heartbeat, orphan requeue)
      ||                            ┌───────────────────v────────────────────┐
      ||                            │ worker x N  (LangGraph run graph)       │
      ||                            │  classifier agent <-> tools             │──> chroma   (vector store server)
      ||                            │        |  hand-off: proposal            │──> llm-stub / real provider (OpenAI-compatible)
      ||                            │        v                                │
      ||                            │  verifier agent (own tools + judge)     │
      ||                            │  pass -> router -> result               │
      ||                            │  fail -> retry with new strategy (<=2) -> escalate │
      ||                            └─────────────────────────────────────────┘
 ingest (one-shot job): PDFs -> parse -> chunk -> embed -> chroma + redis index:meta
```
Compose services (11): `redis`, `chroma`, `llm-stub`, `ingest` (one-shot), `api`, `worker` (scalable), `jaeger`, `prometheus`, `cadvisor`, `grafana`, `locust` (profile `loadtest`).

## 2. Agents (A1, A2, A3)
* **Run graph** (LangGraph, `app/agents/graph.py`): `classify -> verify -> {retry | route | route_fail}`. Strategy sequence on retry: `default -> article_first -> exhaustive_notes`; at most `MAX_VERIFY_ATTEMPTS=2` attempts, then escalate.
* **Classifier agent** (`classifier.py`, a LangGraph sub-graph with `llm` and `tools` nodes): the model (or the offline policy) chooses which tool to call next. **Retrieval is a tool the agent chooses**, never a fixed stage. Tools (5): `get_request_fields`, `search_corpus` (hybrid), `get_clause` (by id), `search_headings_by_article`, `get_notes_for_heading`. Procedure (prompt v2) = spec section 5: retrieve the provision that appears to apply -> look for notes that modify it -> search again with what was read -> apply GRI in order -> propose with citations.
* **Web client** (`frontend/`) - one page, no build step, fonts served from the image so it needs no external network. It polls `/v1/runs/{id}/trace` while a run executes and draws each tool call as it happens (the worker streams trace events to Redis mid-run for exactly this), then renders the determination, the quoted statute behind it, and the verifier's attempt-by-attempt audit trail.
* **Verifier agent** (`verifier.py`) - a separate agent with its own responsibility, its own prompt (`verifier.v1`) and its own tool calls (it runs its **own** `search_headings_by_article` instead of trusting the classifier's searches), plus an independent judge model call when a real provider is configured. Checks: citations resolve to retrieved text; quotes verbatim; heading supported; GRI rule cited; notes considered; exclusions backed by cited notes; no heading that names the article is left unexplained; the proposed article is named in the request and the chosen heading names that article (or is a Chapter 73 material heading for an iron/steel article) - added after INC-J; flags coherent; output clean; judge. The classifier cannot mark its own work.
* **Hand-off**: classifier -> `Proposal` (validated pydantic object) -> verifier -> `VerificationReport{passed, checks, next_strategy}` -> router.
* **Bounds (A1)**: `AGENT_MAX_STEPS`, `AGENT_MAX_TOOL_CALLS`, `AGENT_RUN_TIMEOUT_S` wall clock (also enforced around the whole graph), `TOOL_TIMEOUT_S` per tool call, `LLM_TIMEOUT_S` per model call. Hitting any bound yields `specialist-classification-review` with code `loop_limit`/`run_timeout`, never a confident answer.
* **If one agent fails mid-run**: classifier error or no proposal -> no verifier call, result is `specialist-classification-review` (`no_proposal`/`agent_failure`). Verifier unavailable or errors -> it **fails closed**: an unverified proposal is never returned as clear-for-filing. Model provider down -> retries, breaker, fallback chain below; run marked `degraded`. Whole worker dies -> the claimed run id is re-queued by another worker after the heartbeat expires.
* **Router** (F6/F7): `clear-for-filing` only if verified, non-degraded and a single heading is supported (GRI 1) or one is most specific (GRI 3(a)); `seek-product-clarification` when a fact that decides between headings is missing (material, capacity); `specialist-classification-review` for out of scope, a tie after GRI 3, failed verification, bounds hit, degraded mode.

## 3. Resilience (A5)
Timeout on every model and tool call; bounded retries (`LLM_MAX_RETRIES`) with exponential backoff and jitter on 429/5xx/timeouts only; circuit breaker shared by all workers through Redis (closed -> open after `CB_FAILURE_THRESHOLD` failures in `CB_WINDOW_S`; half-open probe after `CB_RECOVERY_S`); fallback path **primary provider -> optional secondary provider -> deterministic offline policy**; any run that touched the fallback is `degraded`, `confident=false` and cannot be `clear-for-filing` (enforced in the router **and again** in the output filter). Degraded results are not cached.

## 4. Deployment boundaries and why (D3)
| Boundary | Reason |
|---|---|
| `api` vs `worker` | **Scaling and resource profile**: the API is I/O-light and latency-sensitive; workers hold model calls open for seconds and are the throughput bottleneck (measured: 1 worker = 0.73 runs/s). They scale independently (`--scale worker=3`). **Failure isolation**: a worker crash/restart does not drop HTTP connections or accepted run ids. |
| queue (`redis`) between them | Spec A4: runs take tens of seconds, so submission returns an id at once. Redis lists with `BLMOVE` give at-least-once delivery with orphan recovery; it also serves caches, rate limits, traces, quarantine - one small dependency instead of five. |
| `chroma` server | **Resource profile / independent release**: index memory and persistence live outside the stateless app containers; API and workers share one index; re-index without restarting them. |
| `ingest` one-shot | **Independent release**: corpus changes are a data deployment, not a code deployment; idempotent; gates `api`/`worker` start via `service_completed_successfully`. |
| `llm-stub` | **Failure isolation + cost**: stands in for the provider boundary (the only external dependency) so load/chaos tests do not touch a paid API. |
| observability stack | Separate so a dashboard failure cannot affect request serving. |

## 5. Observability (F8, A6, D5)
JSON logs with `correlation_id` (set per HTTP request by the API; the worker uses the run id as the correlation id for everything in a run, so API log lines and worker log lines join on run id) and `run_id` on every line; OpenTelemetry spans (`api.submit`, `worker.run`, `agent.run`, `agent.classifier[.llm]`, `agent.verifier[.llm]`) with the W3C `traceparent` stored with the queued run so API and worker spans share one trace in Jaeger; Prometheus on API and every worker (DNS service discovery over replicas); cAdvisor for per-container CPU/memory/uptime; Grafana dashboard (`monitoring/grafana/dashboards/export-classifier.json`, 23 panels): p50/p95, SLO line, error rate, throughput, availability, queue, per-agent success, steps and tool calls per run, per-step latency, retry rate, breaker state, cost, container resources, index freshness, cache ratio, guardrail counts, live quality. **Reconstructing a run:** `GET /v1/runs/{id}/trace` returns every tool call (arguments/query, latency, returned passage ids), every model call (prompt name+version+sha, tokens, cost, latency, retries), each verification check and the route.

## 6. Decisions and what was rejected
| Decision | Rejected alternative | Why |
|---|---|---|
| LangGraph | CrewAI | explicit state machine with a bounded loop and conditional retry edges is what A1/A3 need; fewer hidden prompts |
| Chroma server | FAISS | metadata filters (chapter, type), upserts by id for partial re-embedding, no index rebuild |
| Redis for queue + state | Celery+RabbitMQ; Postgres | one component; reliable-queue semantics are 30 lines and tested; no ORM or broker to run |
| Polling | callbacks/webhooks | simplest for a browser/curl client, no inbound reachability needed, survives client restarts. Callback can be added later without changing the run model |
| Deterministic metrics | TruLens | TruLens needs a judge model (cost, rate limits, nondeterminism); proxies run offline in CI. Cost: weaker semantics (stated) |
| Legal-unit chunks | fixed-size windows | citations must point at one provision; windows would split a note from its "does not cover" list |
| Article-name index + notes links | embedding search only | spec 4.1: the deciding provision is never mentioned in the request; materials pull toward Chapter 73 |
| Offline policy as final fallback | return 503 | an answer that says "needs human review" with the evidence found is more useful than an error, and is safe because it cannot be confident |
| Grafana anonymous access | admin password in `.env` | local demonstration only; keeps D4 (no credential in the repo) |

## 7. What did not work (see `docs/INCIDENT_LOG.md` for detail)
* Material-first search for the vacuum flask lands on 7307/7323 - the reason for the article-name tool and the verifier's independent check.
* Word-overlap article matching (nail brushes), sequential material stripping, re-stripping inside the tool, and over-strict clarification coherence all produced wrong or falsely rejected answers; each fix has a regression scenario.
* A per-run tool budget of 16 starved the retry path.
* Prompt comparison cannot be done without a real model; we say so rather than invent a result.
