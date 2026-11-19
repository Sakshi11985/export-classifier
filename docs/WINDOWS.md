# Running on Windows 10/11 (PowerShell)

Everything runs inside Linux containers, so Windows only needs **Docker Desktop**. No Python, Redis or bash is
needed on Windows: every helper runs *inside* a container with `docker compose exec ...`.

> Status: verified end to end on Windows 11 with Docker Desktop 4.97 and WSL 2 (12 vCPU / 7.4 GB) on 2026-10-29 -
> all 11 services healthy from a clean extract, all ten demonstration items of section 9 of the brief, `set_llm.ps1`,
> the Jaeger trace view and the Locust load test at 1 and 3 workers.

## 1. Install (once)
1. Install **Docker Desktop for Windows** (it enables WSL 2; accept and reboot when asked).
2. Start Docker Desktop and wait until it says *Engine running*.
3. Docker Desktop -> Settings -> Resources: give it at least **4 GB memory** (6 GB is comfortable).
4. Open **PowerShell** and check:
   ```powershell
   docker version
   docker compose version        # must print v2.20 or newer
   ```

## 2. Get the project
Extract `export-classifier.zip` to a short path that is **not** inside OneDrive (sync can lock files), e.g. `C:\projects`.
```powershell
cd C:\projects\export-classifier
Copy-Item .env.example .env
```
If you clone with git instead, the repository's `.gitattributes` keeps Linux line endings automatically.

## 3. Start (demonstration item 1)
```powershell
docker compose up -d --build --scale worker=3          # first build: several minutes (downloads images + Python packages)
docker compose ps                                       # all running/healthy, "ingest" Exited (0)
docker compose exec api python scripts/demo.py health   # want: index_chunks 122, workers_alive 3, llm_provider offline
```
Open in the browser: http://localhost:8000 (web client - this is the best surface for the demonstration) · http://localhost:8000/docs (Swagger) ·
http://localhost:3000 (Grafana) · http://localhost:16686 (Jaeger) · http://localhost:9090 (Prometheus).

Stop: `docker compose down` (add `-v` to also delete the index and Redis data).

## 4. Demonstration commands (same order as `docs/DEMO_SCRIPT.md`)
`demo.py` prints a readable summary; add `--json` to `run` for the full result.

| # | Item | Command |
|---|---|---|
| all | the 8 test values of the Evaluation 1 plan in one go | `docker compose exec api python scripts/demo.py all` |
| 2, 10 | section 4.1 case with resolving citations | `docker compose exec api python scripts/demo.py run "Stainless steel vacuum flask with a moulded plastic outer body."` |
| 3 | trace one run | `docker compose exec api python scripts/demo.py trace <run_id printed by the previous command>` (use a description not run before; a repeated one is served from the cache and the trace says `CACHE_HIT`, which is itself the D7 demonstration) |
| 4 | malformed request, quarantined, no agent started | `docker compose exec api python scripts/demo.py bad` then `... demo.py quarantine` |
| 5 | case the corpus cannot answer | `docker compose exec api python scripts/demo.py run "Cotton t-shirt"` |
| 6 | verification catches an unsupported answer | `docker compose exec -e ENABLE_FAULT_INJECTION=true worker python scripts/try_run.py --fault=naive_first_pass "Stainless steel vacuum flask with a moulded plastic outer body."` (also `--fault=wrong_product "Stainless steel kitchen sink"`, `--fault=fabricated_citation ...`) |
| 7 | prompt injection | `docker compose exec api python scripts/demo.py inject` |
| 8 | circuit breaker | switch to the stub (section 5), then `docker compose exec api python scripts/demo.py stub error`, run 3 different products, `... demo.py health` **immediately** shows `llm_circuit_breaker open` (it half-opens 20 s later); restore with `... demo.py stub ok`, wait 20 s, run one product -> breaker `closed` |
| 9 | load test against the SLO, and `--scale` | on the stub: `docker compose up -d --no-deps --scale worker=1 worker`, then `docker compose exec api python scripts/demo.py load 10 60`; repeat with `--scale worker=3` and `load 20 60`. Interactive UI: `docker compose --profile loadtest up -d locust` -> http://localhost:8089 |

## 5. Switching the reasoning backend
Default is `offline` (deterministic policy, not a model; correct answers, no key). Use `stub` only for items 8-9.
```powershell
powershell -ExecutionPolicy Bypass -File scripts\set_llm.ps1 stub      # or offline / real
```
**Manual way (always works):** open `.env` in Notepad, change the lines below, save, then run
`docker compose up -d --no-deps --force-recreate --scale worker=3 api worker`.

| Backend | Lines in `.env` |
|---|---|
| offline | `LLM_PROVIDER=offline` |
| stub | `LLM_PROVIDER=openai_compat`, `LLM_BASE_URL=http://llm-stub:8080/v1`, `LLM_API_KEY=` (empty), `LLM_MODEL_CLASSIFIER=stub-model`, `LLM_MODEL_VERIFIER=stub-model` |
| real model | `LLM_PROVIDER=openai_compat`, `LLM_BASE_URL=<provider's OpenAI-compatible URL>`, `LLM_API_KEY=<key>`, `LLM_MODEL_CLASSIFIER=<model id>`, `LLM_MODEL_VERIFIER=<model id>` |

Never put a real key anywhere except your private `.env`.

### Running a real model end to end
Any provider with an OpenAI-compatible `/chat/completions` endpoint **that supports tool
(function) calling** works; a free tier is enough for the demonstration. The provider's URL
and model id are deliberately absent from this repository (D4) - take them from the
provider's own documentation and put them only in `.env`.

```powershell
notepad .env
#   REAL_LLM_BASE_URL=<provider's OpenAI-compatible base URL, no trailing slash>
#   REAL_LLM_API_KEY=<your key>          .env is git-ignored: never commit it
#   REAL_LLM_MODEL=<exact model id from the provider's model list>
powershell -ExecutionPolicy Bypass -File scripts\set_llm.ps1 real
docker compose exec api python scripts/demo.py health
```
`health` must report `llm_provider openai_compat` and the breaker `closed`. Then run the
Section 4.1 case from the web client; with a real model each run takes seconds rather than
milliseconds, and the live step list shows the model choosing tools itself.

The client appends `/chat/completions` to `REAL_LLM_BASE_URL`, so a trailing slash or a URL
that already ends in `/chat/completions` gives a 404.

If a run comes back `degraded_mode`, the provider call failed:
```powershell
docker compose logs worker | findstr /i llm
```
`401` = key, `404` = model id or base URL, `429` = rate limit. Return to the default with
`scripts\set_llm.ps1 offline`.

**Rate limits matter here.** One run makes about five model calls, so a free tier allowing
15 requests per minute supports roughly three runs a minute. That is fine for the
demonstration and for the evaluation batch, and useless for the load test - item 9 must
keep using `set_llm.ps1 stub`, which is why the stub exists.

### What a real model unlocks
Only in this mode do these two produce a meaningful number:
```powershell
docker compose exec worker python -m app.evaluation.run_eval --label real_model
docker compose exec worker python scripts/compare_prompts.py v1 v2
```
The first gives the system's real accuracy (31 scenarios, ~155 requests, about 11 minutes at
15 req/min); the second is requirement L2, the prompt-variant comparison, which returns a
zero delta under the offline policy because that policy ignores the prompt. Record both in
`docs/EVALUATION.md`.

## 6. Running the tests (no redis-server needed on Windows)
The tests ship inside the image. This starts a throwaway container, installs `redis-server` in it and runs all 116 tests (about 3 minutes):
```powershell
docker compose run --rm --no-deps --user root worker sh -c "apt-get update -qq && apt-get install -y -qq redis-server >/dev/null && python -m pytest -q"
```
Expected last line: `116 passed`.

## 7. Windows-specific problems
| Symptom | Fix |
|---|---|
| `docker: error during connect ... pipe/docker_engine` | Docker Desktop is not running: start it and wait for *Engine running* |
| `Ports are not available ... 8000` (or 3000, 8080, 9090) | another program or a Windows-reserved range uses it. Check `netsh interface ipv4 show excludedportrange protocol=tcp`; change the left number in that service's `ports:` line in `docker-compose.yml`, e.g. `"8001:8000"`, then use http://localhost:8001 |
| `curl` in PowerShell asks for parameters / prints odd output | in Windows PowerShell `curl` is an alias for `Invoke-WebRequest`; use `curl.exe` or the `demo.py` commands above |
| `set_llm.ps1 cannot be loaded because running scripts is disabled` | run it as shown with `powershell -ExecutionPolicy Bypass -File ...`, or use the manual way |
| `cadvisor` keeps restarting | only the per-container resource panels need it; the rest works. Start without it: `docker compose up -d --scale worker=3 --scale cadvisor=0` |
| Build is very slow or `pip install` fails | the first build downloads ~1 GB; check the network/VPN and rerun `docker compose up -d --build` |
| Everything slow, containers killed | raise Docker Desktop memory (section 1) |
| `.sh` scripts | not needed on Windows; use `demo.py` / `set_llm.ps1`. If you want bash, Git Bash or WSL Ubuntu can run them |
