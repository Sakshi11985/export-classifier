# Incident log (D8)

Real failures met while building and testing this system, newest last. Each entry: what we saw, the cause, the fix, and the
test or check that now guards against it. "Detected by" says how it was found.

| ID | When (dev) | Short title | Severity |
|---|---|---|---|
| INC-A | stack bring-up | Chroma server started with the wrong entry point; ingest could not connect | blocker (no index) |
| INC-B | agent testing | A *set* of two articles was answered confidently for one of them | wrong answer, **confident** |
| INC-C | fault-injection test | Verifier-triggered retry hit the loop limit and escalated instead of recovering | degraded behaviour |
| INC-D | scenario testing | "Galvanised steel nails" classified under *nail brushes* (9603) | wrong answer |
| INC-E | scenario testing | "Steel wool" lost its noun: the material stripper removed "wool" | wrong answer |
| INC-F | scenario testing | A tool call re-stripped materials and four headings tied for "wool pad" | wrong route (escalation) |
| INC-G | scenario testing | Verifier rejected a *correct* clarification request | false rejection |
| INC-H | test-suite development | Rate-limit test passed or failed depending on timing | flaky test |
| INC-I | load-test preparation | Orchestration shell command killed its own shell | tooling |
| INC-J | first Docker Compose run | A model answer for a *different product* passed verification: "Cotton t-shirt" -> clear-for-filing 9617 | wrong answer, **confident** |
| INC-K | first Docker Compose run | `ingest` exited 1: Redis refused connections from other containers (protected mode) | blocker (no index) |
| INC-L | first Docker Compose run | `api` container failed to start: `uvicorn` executable not found in `$PATH` | blocker (no API) |
| INC-M | backend switch script | Switching the LLM backend silently scaled the workers from 3 to 1 | degraded capacity |

## INC-A  Chroma server could not be reached by the ingest job
* **Saw:** `Could not connect to a Chroma server` from `python -m app.corpus.ingest`; the index stayed empty so nothing could be retrieved.
* **Cause:** the server process was launched with `python -m chromadb.cli.cli ...`, which imports the CLI but never starts it; the process exited silently and nothing listened on the port.
* **Fix:** use the packaged `chroma run --path ... --port ...` entry point (local stack script and the compose `chroma` service). The compose service has a real healthcheck (`/api/v2/heartbeat`) and `ingest` waits on `condition: service_healthy`, so the ordering bug cannot recur silently.
* **Guard:** `tests/conftest.py::stack` starts `chroma run` and fails the whole integration module with the server log if the heartbeat never answers.

## INC-B  A product set was answered with confidence
* **Saw:** "Set containing a plastic hair comb and a stainless steel pot scourer" returned `clear-for-filing / 9615` (combs). Heading 7323 (pot scourers) had been "excluded" by Section XV Note 1 - but that note only excludes goods that Chapter 96 *names*, and pot scourers are not named there.
* **Cause:** the offline policy applied an exclusion note to every heading rather than only to headings naming the same article; sets were not evaluated per article.
* **Fix:** exclusions apply only to headings that name the same article as the heading the note is being used against; sets are evaluated per article and, when two headings remain equally supported after GRI 3(a), the route is `tie_after_gri3` (F7).
* **Guard:** eval scenario E06 and `tests/test_agent_scenarios.py::test_tie_escalates`.

## INC-C  A retry after failed verification escalated instead of recovering
* **Saw:** with `options.fault=fabricated_citation` the first answer was correctly rejected, but the retry ended `loop_limit`.
* **Cause:** the tool-call budget (initially 16) is per **run**, and the first attempt had already spent most of it.
* **Fix:** budgets raised to 20 steps / 30 tool calls (`AGENT_MAX_STEPS`, `AGENT_MAX_TOOL_CALLS`) - still bounded; the retry reuses passages already retrieved.
* **Guard:** `tests/test_agent_scenarios.py::test_fabricated_citation_is_caught`, `::test_loop_bounds_force_escalation`.

## INC-D  "Galvanised steel nails" went to the nail-brush heading
* **Saw:** heading 9603 (brushes, including nail brushes) won for a Chapter 73 article.
* **Cause:** the article-name matcher counted any shared word ("nail") as naming the article.
* **Fix:** head-noun matching (`segment_heads` in `app/retrieval.py`): a heading names the article only if the article's head noun is the head of one of the heading's segments.
* **Guard:** eval E21.

## INC-E  "Steel wool" lost "wool"
* **Saw:** the article became "pad" instead of "wool pad"; the material list contained "wool".
* **Cause:** material stripping replaced matches sequentially, which invalidated the `steel wool` look-behind for the later "wool" match.
* **Fix:** spans are computed on the original text and removed in one pass (`strip_materials`).
* **Guard:** eval E13 (steel wool pad), `tests/test_tools_entities.py::test_entities`.

## INC-F  The article search re-stripped materials and tied four headings
* **Saw:** `search_headings_by_article("wool pad")` returned four strong headings, so the verifier reported an unexplained heading.
* **Cause:** the tool reused the request-level entity extractor, which strips materials - but the caller had already passed the article's own name ("steel wool" *is* an article).
* **Fix:** a separate `article_tokens()` that keeps materials; the tool contract says "pass the article's own name".
* **Guard:** eval E13, E21.

## INC-G  The verifier rejected a correct clarification request
* **Saw:** "Steel drum" (no capacity) should ask for the capacity but failed `flags_coherent`.
* **Cause:** the check treated any clarification as incoherent when at least one heading matched strongly.
* **Fix:** it now fails only when **exactly one** heading names the article (then a clarification is unjustified); two or more candidates that differ by capacity legitimately need a question.
* **Guard:** eval E15/E16.

## INC-H  Flaky rate-limit test
* **Saw:** `test_rate_limit_per_client` failed in one of three full runs (got 422 where 429 was expected).
* **Cause:** the test stack used 600 requests/min (10/s refill), so a token had been refilled by the time the final request was sent.
* **Fix:** the test stack uses 6/min with burst 6; the assertion no longer depends on timing. Full suite passed twice in a row afterwards.

## INC-I  Test orchestration command killed its own shell
* **Saw:** the command that restarted the stack exited with code 144 and printed nothing.
* **Cause:** `pkill -f "redis-server.*6391"` matched the shell command line that contained that text.
* **Fix:** processes are stopped through the PID files written by `scripts/local_stack.sh stop`; no pattern kills.

## INC-J  An answer about a different product was verified and cleared
* **Saw:** under Docker with the default `.env` of that time (`LLM_PROVIDER=openai_compat` -> llm-stub), "Cotton t-shirt" and "Insulated bottle, material not stated" both came back `clear-for-filing / 9617`, `verification.passed=true`. The stub returns the vacuum-flask answer whatever it is asked; every citation in it is genuine corpus text, so every existing check (citations resolve, quotes verbatim, rule cited, notes considered) passed. A real model that hallucinates, or answers from stale context, would produce the same failure.
* **Cause:** no verifier check tied the proposal to *this* request. The router's explanation even claimed "the retrieved heading text names the article" - which nothing checked.
* **Fix:** two new verifier checks: `article_grounded_in_request` (the proposed article's head noun appears in the request) and `heading_matches_request` (the chosen heading names the request's article according to the verifier's *own* article search, or is a Chapter 73 heading and the request states iron/steel). Both failures trigger the `article_first` retry; a second failure escalates. Default `.env.example` changed to `LLM_PROVIDER=offline`; `scripts/set_llm.sh` switches backends.
* **Guard:** `tests/test_api_integration.py::test_model_answering_a_different_product_is_never_cleared` (real stub over HTTP), `tests/test_agent_scenarios.py::test_answer_for_a_different_product_is_caught_then_corrected` (fault `wrong_product`). All 31 evaluation scenarios still pass.

## INC-K  Redis protected mode blocked the ingest job
* **Saw:** `INGEST FAILED: DENIED Redis is running in protected mode ...`; `api`/`worker` never started (they wait for `ingest` to complete successfully).
* **Cause:** Redis started without a config file and no password enables protected mode, accepting only loopback clients. The official `redis` image's entrypoint normally disables it; the stand-in image used in the build environment did not, which exposed that the compose file relied on an image default.
* **Fix:** `--protected-mode no` set explicitly in the compose `redis` command. Safe because Redis publishes no host port and is reachable only on the compose network.
* **Guard:** `docker compose up --wait` reaches healthy for every service.

## INC-L  `uvicorn` not found when starting the api container
* **Saw:** `exec: "uvicorn": executable file not found in $PATH`.
* **Cause:** the image relied on pip's console-script launcher being on `PATH`. In the stand-in image the package was already present, so pip skipped writing the launcher; the same happens with `--user` installs or a different pip prefix.
* **Fix:** `CMD ["python", "-m", "uvicorn", ...]` and `python -m locust` for the locust service - module execution does not depend on where launchers are written. Also removed an unused `apt-get install curl` (one fewer network dependency during the build; every health check uses Python).
* **Guard:** api container healthy under `docker compose up --wait`.

## INC-M  Backend switch reset the worker count
* **Saw:** after `scripts/set_llm.sh stub`, `docker compose ps` showed one worker instead of three.
* **Cause:** `docker compose up -d --force-recreate api worker` applies the service's default scale (1).
* **Fix:** the script counts the running worker replicas and passes `--scale worker=<n>`.
* **Guard:** checked by hand under Docker (3 workers before and after the switch); documented in README section 8.

## INC-N  Three dashboard panels permanently empty: cAdvisor sees no containers under Docker Desktop
* **Saw:** on the demonstration laptop (2026-11-02) with all 11 services up and traffic flowing, *CPU per container*, *Memory per container* and *Container uptime* showed **No data**, while every other panel on the dashboard drew correctly. These three are the A6 resource-utilisation and uptime panels.
* **Cause:** not a query or label mismatch - the data is never collected. `container_cpu_usage_seconds_total` in Prometheus returned only five cgroup-level series (`id="/"`, `"/docker"`, `"/non-systemd"`, `"/restricted"`, `"/docker/buildkit"`) and **no per-container series at all**, so no series carries a `name` label and the dashboard's `name=~"export-classifier-.+"` filter matched nothing. Under Docker Desktop on Windows the containers run inside a WSL 2 VM whose cgroup tree cAdvisor can read but whose container metadata it cannot resolve, so it never maps a cgroup to a container name. A limitation of the host, not of the compose file: the same cAdvisor configuration does produce per-container series on a native Linux Docker host.
* **Fix:** the three panels now read the services' own process metrics, which `prometheus_client` exports on every `/metrics` endpoint and Prometheus already scrapes: `process_cpu_seconds_total`, `process_resident_memory_bytes` and `process_start_time_seconds`, filtered to `job=~"api|worker"`. Retitled *CPU per service*, *Memory per service* and *Service uptime*, and they now break down per API and per worker replica. This is a better source for A6 than the original: it is the application's own processes rather than a sidecar inferring from cgroups, and it behaves identically on any host. cAdvisor stays in the stack - it is still scraped and still counts as a compose service - but nothing on the dashboard depends on it now.
* **Also fixed in the same pass:** *Task success rate per agent* looked empty while actually reporting a perfect 1.0, because the panel's `max` was exactly 1 and the line was drawn underneath the top gridline (`max` raised to 1.05). *LLM retry rate* showed **No data** rather than its true value of zero, because a labelled counter that has never been incremented exports no series at all and the division had no left-hand side (`or vector(0)` added).
* **Guard:** `monitoring/make_dashboard.py` regenerates the dashboard JSON, so the queries stay reviewable as code; the three panels were confirmed populated on the demonstration laptop before `docs/evidence/grafana-agent-dashboard.png` was captured.
* **Lesson:** a panel reading *No data* has three quite different causes - the metric is absent, the metric exists but no series matches the filter, or the series exists and the value is genuinely zero or off-scale. All three occurred here, in the same dashboard, and only the middle one was a real defect. Querying Prometheus directly separated them in minutes; reading the panels alone would not have.
