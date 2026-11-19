# Data contract (D1)

The machine-readable contract is served by the running service: `GET /v1/contract` (JSON Schema for the request, the
submit response, the feedback body and the internal proposal) and `GET /docs` (OpenAPI/Swagger UI). This page is the
human-readable version. It is enforced in `app/contracts.py` (pydantic, `extra="forbid"`) **at the ingestion boundary, before any
agent starts**; the tests that prove it are in `tests/test_contracts_guardrails.py` and
`tests/test_api_integration.py::test_malformed_request_rejected_quarantined_and_no_agent_started`.

## 1. `POST /v1/classify` - request

| Field | Type | Rule | Check type |
|---|---|---|---|
| `product_description` | string, **required** | 3-2000 chars after trimming; must contain at least one word of 2+ letters | schema, range, content |
| `product_name` | string | <= 120 chars | range |
| `materials` | list of strings | <= 10 items; each 2-60 chars, letters/space/`-/&,.()` only | schema, range, pattern |
| `intended_use` | string | <= 300 chars | range |
| `destination_country` | string | `^[A-Z]{2}$` (ISO 3166-1 alpha-2) | pattern |
| `shipment_mode` | enum | `sea`, `air`, `road`, `rail` | categorical |
| `quantity` | int | 1 .. 10 000 000 | range |
| `quantity_unit` | enum | `pcs`, `kg`, `litre`, `set` | categorical |
| `unit_value_usd` | number | > 0 and <= 10 000 000 | range |
| `capacity_litres` | number | > 0 and <= 100 000 | range |
| `weight_kg` | number | > 0 and <= 1 000 000 | range |
| `is_set_for_retail` | bool | - | type |
| `essential_character_hint` | string | <= 200 chars | range |
| `client_reference` | string | `^[A-Za-z0-9_\-]{1,64}$` | pattern |
| `options.fault` | enum | `none`, `naive_first_pass`, `fabricated_citation`; **ignored unless the server runs with `ENABLE_FAULT_INJECTION=true`** (demonstration only) | categorical |
| `options.bypass_cache` | bool | - | type |
| *anything else* | - | **rejected** (unknown field) | schema |

Cross-field rules: `quantity` and `quantity_unit` must be given together; `is_set_for_retail=true` needs `quantity_unit` = `set` or `pcs`;
`quantity x unit_value_usd` must not exceed 1e9 USD; with `quantity_unit=litre`, `capacity_litres` must not exceed `quantity x 1000`.

Free-text fields (`product_description`, `product_name`, `intended_use`, `essential_character_hint`) additionally pass the
injection and toxicity guardrails (L4). A guardrail hit returns **400** (the request was well formed but unsafe), a contract
violation returns **422**. Both are quarantined and neither starts an agent.

Optional header `X-Client-Id` (`^[A-Za-z0-9_.\-]{1,64}$`) identifies the caller for rate limiting; the client IP is used otherwise.

## 2. Responses

| Status | Meaning | Body |
|---|---|---|
| **202** | accepted, queued | `{run_id, status:"queued", cache_hit:false, status_url, trace_url, poll_after_ms}` |
| **200** | identical request answered from the run cache | same shape, `status:"completed"`, `cache_hit:true`, `poll_after_ms:0` |
| **400** | blocked by a guardrail | `{error:"guardrail_blocked", categories[], detail, quarantine_id, agent_run_started:false}` |
| **422** | contract violation | `{error:"validation_failed", quarantine_id, agent_run_started:false, errors:[{field, message, type}]}` |
| **429** | rate limited or too many unfinished runs | `{error:"rate_limited"\|"too_many_inflight_runs", retry_after_s}` + `Retry-After` |
| **503** | queue full / Redis unreachable | `{error:"queue_full"\|"queue_unavailable"}` + `Retry-After` |

## 3. `GET /v1/runs/{run_id}`

`status` is `queued`, `running`, `completed` or `failed`. When terminal, `result` contains:

| Field | Content |
|---|---|
| `route` | `clear-for-filing` \| `seek-product-clarification` \| `specialist-classification-review` |
| `route_basis` | `{code, explanation}`; codes: `single_heading_gri1`, `most_specific_gri3a`, `tie_after_gri3`, `out_of_scope`, `missing_product_information`, `verification_failed`, `loop_limit`, `run_timeout`, `agent_failure`, `degraded_mode`, `low_confidence`, `no_proposal` |
| `classification` | `{heading, heading_description, chapter, gri_applied, alternatives[], reasoning_summary, confidence}` or `null` when the system declines to name a heading |
| `export_policy` | policy text composed **verbatim** from the supplied DGFT schedule (items, conditions, source page) or `null` |
| `citations[]` | `{id, type, title, quote, passage, source, page}`; every `id` was retrieved in this run and every `quote` is verbatim inside `passage` |
| `escalation` | for non-clear routes: what a human must do/decide |
| `verification` | `{passed, attempts, strategies_tried, final_checks[], history[]}` |
| `prompt_versions` | `{classifier:"classifier@v2:<sha12>", verifier:"verifier@v1:<sha12>"}` |
| `usage` | tokens, cost and calls, total and **per step** |
| `corpus` | index version and build time the answer was produced against |
| `confident`, `degraded`, `limits_hit`, `timings`, `determination_authority` | see below |

`determination_authority` is always present: *"This is a proposed classification with supporting evidence. It is not a customs
determination and no declaration has been filed; a licensed customs broker decides."*
`confident=true` only for a verified, non-degraded, clear-for-filing result. **A run that hit a loop bound, timed out, ran on the
degraded fallback path or failed verification is never `clear-for-filing`.**

## 4. Other interfaces

| Interface | Contract |
|---|---|
| `GET /v1/runs/{id}/trace` | `{events[], summary}`: every tool call (name, args/query, latency, passage ids), every model call (prompt version + sha, latency, tokens, cost), every verification (per check), route |
| `POST /v1/runs/{id}/feedback` | `{rating: "satisfactory"\|"unsatisfactory", reason?}`; reason (>= 3 chars) **required** when unsatisfactory; 404 for unknown runs |
| `GET /v1/feedback/summary`, `GET /v1/quality` | aggregates; live-scored quality series |
| `GET /v1/quarantine[/{id}]` | `{quarantine_id, ts, reason, client_id, correlation_id, errors[], agent_run_started:false, payload}` |
| `GET /v1/clauses/{id}` | one corpus passage by identifier (`GRI-1`, `HEADING-9617`, `SEC-XV-NOTE-1`, `POLICY-9617`, ...) |
| `GET /v1/corpus` | index metadata: built-at, version, documents (sha256, pages, chunks), chunk counts, ingest throughput |
| `GET /health/live`, `/health/ready`, `/health`, `/metrics` | liveness / readiness (redis, chroma, index, workers, breaker) / Prometheus |
| LLM provider | OpenAI-compatible `POST {LLM_BASE_URL}/chat/completions`; the stub implements the same contract |
| Corpus | three PDFs in `CORPUS_DIR` with a text layer; see `docs/CORPUS.md` |
