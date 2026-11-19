# Requirement -> implementation -> proof

Status key: **V** verified by an automated test or a recorded measurement in this repository's build environment; **P** implemented, proven only partly (what is missing is stated); **U** implemented but **not run** here (external limitation).
Test run: `python -m pytest -q` -> **116 passed** (2026-10-31). Files: `tests/`.
Docker: run in two environments. Build environment - real compose file and Dockerfile, base image a local stand-in for `python:3.13-slim` (Docker Hub blocked), monitoring images not pulled. **Demonstration laptop (Windows 11 + Docker Desktop, 2026-10-29) - all 11 services up with their official images from a clean extract, and demonstration items 1-10 all run there.** See `docs/LIMITATIONS.md` item 1.

| Req | What | Implementation | Proof | Status |
|---|---|---|---|---|
| F1 | REST interface + published contract | `app/api.py`, `app/contracts.py`, `/v1/contract`, `/docs`, `docs/DATA_CONTRACT.md` | `test_api_integration::test_health_and_contract`, `::test_valid_request_async_run...` | V |
| F2 | schema/range/categorical/cross-field validation, quarantine | `ClassificationRequest`, `Store.quarantine` | `test_contracts_guardrails` (11 reject cases), `test_malformed_request_rejected_quarantined...` | V |
| F3 | article + material extraction | `app/entities.py`, offline policy, verifier second opinion | `test_tools_entities::test_entities`, `::test_set_detection` | V (rule-based; LLM extraction unmeasured) |
| F4 | candidate headings incl. heading naming the article, with notes | `search_headings_by_article`, `get_notes_for_heading`, hybrid search | `test_search_headings_by_article_finds_9617`, `::test_notes_for_heading_returns_exclusion_note` | V |
| F5 | grounded classification with citations | classifier + `build_result` | `test_section_4_1_reference_answer`, 31 eval scenarios | V |
| F6 | three routes with stated basis | `app/agents/router.py` | eval E01-E06; `route_basis.code` asserted | V |
| F7 | escalate on tie / out of corpus | router, verifier `flags_coherent` | `test_tie_escalates`, `test_escalation_not_fabrication_for_out_of_corpus` | V |
| F8 | health, metrics, structured logs | `/health*`, `/metrics` (API+worker), `logging_setup.py` | `test_health_and_contract`, `test_metrics_are_exposed` | V (JSON-log format by inspection only) |
| A1 | LangGraph, retrieval as tool, >= 3 tools, loop bounds | `app/agents/*`, `app/tools.py` (5 tools) | `test_at_least_three_tools`, `test_loop_bounds_force_escalation`, `test_wall_clock_bound`, `test_tool_timeout` | V |
| A2 | >= 2 agents, hand-off, failure documented | classifier + verifier; `docs/ARCHITECTURE.md` s.2 | `test_naive_material_first_answer_is_caught...` (verifier overrules classifier) | V |
| A3 | verify every answer; no unverified answer returned | `verifier.py` (incl. `article_grounded_in_request`, `heading_matches_request`), `guardrails.filter_result` | `test_fabricated_citation_is_caught`, `test_answer_for_a_different_product_is_caught...`, `test_model_answering_a_different_product_is_never_cleared` (HTTP), `test_output_filter_never_lets_unverified...` | V |
| A4 | FastAPI, async, queue, poll, justified | 202 + run id; Redis queue; `docs/ARCHITECTURE.md` s.4,6 | `test_valid_request_async_run...` (<1 s submit) | V |
| A5 | timeouts, backoff, breaker, degradation | `app/llm/client.py` | `test_resilience.py` (8 tests) + HTTP `test_zz_circuit_breaker...` | V |
| A6 | per-agent success, steps/tool calls, latency, retry rate, uptime on dashboard | `metrics.py`, dashboard JSON | metrics presence tested; PromQL syntax-checked; rendered panels in `docs/evidence/grafana-agent-dashboard.png` | V (panels proven by screenshot, not by an automated test - `docs/evidence/README.md`) |
| A7 | `--scale worker=3`, throughput and p95 effect | compose `worker` replicas; `scripts/local_stack.sh workers N` | `docs/LOADTEST_RESULTS.md`: 0.75 -> 2.24 runs/s on the demonstration laptop; at 20 users p95 26 s (1 worker) -> 10 s (3 workers) | V |
| A8 | web client | `frontend/index.html` served at `/`, fonts from `frontend/assets` (no external network) | `test_web_client_and_its_assets_are_served`; every route, the live agent trace, the rejection paths and feedback exercised in a real browser | V |
| L1 | LangChain + Chroma; chunking/embedding documented; re-embed without rebuild | `vectorstore.py`, `ingest.py`, `docs/CORPUS.md` | `test_corpus::test_ingest_is_idempotent_and_reembed_is_partial` | V (hashing embeddings; sentence-transformers **U**) |
| L2 | versioned prompts, version in every response, compare variants | `app/prompts`, `scripts/compare_prompts.py` | `test_prompts_are_versioned_with_hash`, `prompt_versions` asserted over HTTP | P (comparison needs a real LLM; run gives 0 delta, stated) |
| L3 | >= 20 scenarios, batch CLI, 4 metrics, before/after | `app/evaluation/*`, 31 scenarios | `test_eval_scenario[...]` x31; `docs/EVALUATION.md` | V (deterministic metrics, not TruLens) |
| L4 | injection defence, output filter, toxicity | `guardrails.py`, untrusted wrapping, tool-arg scanning | `test_injection_blocked` x6, `test_toxicity...`, `test_prompt_injection_blocked_before_any_model` (no model call made), tool arg test | V (pattern-based; a determined adversary can evade patterns) |
| L5 | tokens/cost per step and run; cost per 100 runs; driver | `UsageTotals`, `scripts/cost_report.py` | `test_cost_accounting...`, `docs/LOADTEST_RESULTS.md` | V (illustrative prices) |
| L6 | feedback endpoint, aggregate, live scoring sample, quality chart | `/v1/runs/{id}/feedback`, worker sampling, `/v1/quality` | `test_feedback_persisted_and_aggregated`, `test_live_quality_scores_are_recorded`; chart in `docs/evidence/grafana-quality-chart.png` | V |
| D1 | contract, validation before agent, quarantine with field errors | as F2 | metric `agent_runs_started_total` unchanged after a bad request | V |
| D2 | idempotent CLI pipeline, freshness + throughput metrics | `python -m app.corpus.ingest`, `index:meta`, gauges | `test_corpus.py` (5 tests) | V |
| D3 | >= 4 services in compose, health checks, depends_on, boundaries justified | `docker-compose.yml` (11 services) | `docker compose up -d --build --scale worker=3` from a clean extract on the demonstration laptop: **all 11 services** `running`/`healthy`, `ingest` exit 0 | V |
| D4 | no credentials/endpoints/model ids in repo; `.env.example` | `config.py` env only | `test_no_secrets_endpoints_or_model_ids_in_repository`, `test_env_example_documents_every_setting` | V |
| D5 | structured logs, correlation id, OTel->Jaeger, Prometheus, Grafana, run reconstructible | see ARCHITECTURE s.5 | `test_run_is_reconstructible_from_run_id`, `test_spans_and_traceparent_propagation`; `docs/evidence/jaeger-single-run-trace.png`: one trace per run across `api` + `worker` | V |
| D6 | SLO declared first, Locust with stub, RPS/p50/p95, failing concurrency + component | `docs/SLO.md` (+ sha), `loadtest/`, `docs/LOADTEST_RESULTS.md` | measured | V (process stack, 2 vCPU) |
| D7 | caching (+effect) and per-client rate limiting | run/retrieval/embedding caches, token bucket | `test_whole_run_cache_hit_is_instant`, `test_rate_limit_per_client`, `test_rate_limit_token_bucket`; cache effect measured | V |
| D8 | runbook (2 modes), incident log (real) | `docs/RUNBOOK.md`, `docs/INCIDENT_LOG.md` (14 entries, INC-A..INC-N) | recovery for FM-1 exercised in `test_zz_circuit_breaker...`; orphan requeue in `test_orphaned_run_is_requeued...` | V (shell recipes in the runbook are not automated) |

## Demonstration items -> evidence
| # | Item | Where |
|---|---|---|
| 1 | `docker compose up` from clean clone | README s.3 / `docs/WINDOWS.md` s.3 - verified on the demonstration laptop, all 11 services |
| 2 | valid request with resolving citations | `test_valid_request_async_run_with_resolving_citations` |
| 3 | one run traced end to end | `test_run_is_reconstructible_from_run_id`; `demo.py trace <run_id>`; Jaeger on the demonstration laptop shows one trace with `api` + `worker` spans |
| 4 | malformed rejected, quarantined, no agent started | `test_malformed_request_rejected...` |
| 5 | unanswerable case escalates | eval E05/E26-E28; under Docker: Cotton t-shirt -> `out_of_scope` |
| 6 | verification catches unsupported answer, then re-search | `scripts/try_run.py --fault=naive_first_pass\|fabricated_citation\|wrong_product` prints attempt 0 REJECTED with the failing checks, then the retry; live on the stub: `demo.py run "Cotton t-shirt"`; `test_naive_material_first...`, `test_fabricated_citation...` |
| 7 | injection blocked | `test_prompt_injection_blocked_before_any_model` |
| 8 | breaker opens, degradation | `test_zz_circuit_breaker_opens_and_degrades_then_recovers`; on the demonstration laptop: first failing run 2.63 s (retries+backoff) then 0.58 s (breaker open, fail fast), all `degraded_mode`, recovery to `closed` |
| 9 | Locust vs target, failing concurrency, scale effect | `docs/LOADTEST_RESULTS.md`; `demo.py load <users> <seconds>` |
| 10 | section 4.1 case | `test_section_4_1_reference_answer`, eval E01 |
