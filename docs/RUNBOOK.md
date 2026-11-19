# Runbook (D8)

Commands assume the Docker Compose deployment (`docker compose ...`) and API at `http://localhost:8000`.
For the no-Docker development stack replace `docker compose logs worker` with `tail /tmp/ec-stack/worker1.log` and
`docker compose exec redis redis-cli` with `redis-cli -p 6391`.

The two most likely failures, in the order we expect to meet them, follow. A third short entry covers an unusable index.

---------------------------------------------------------------------------------------------------

## FM-1 The language-model provider is unavailable, rate limited or too slow

Free tiers have low rate limits (spec section 12), so this is the most likely production failure.

**Symptoms**
* Results arrive with `route = specialist-classification-review`, `route_basis.code = "degraded_mode"`, `degraded = true`, `confident = false`
  (never `clear-for-filing`), even for products that normally classify cleanly.
* `GET /health/ready` shows `"llm_circuit_breaker": "open"` (or `half-open`).
* Grafana: *Circuit breaker* panel at 2, *LLM retry rate* rising, p95 latency up before the breaker opened.
* Worker logs: `primary LLM unavailable`, `circuit breaker OPEN`.

**Confirm**
```bash
curl -s localhost:8000/health/ready | python3 -m json.tool | grep -E "breaker|llm_provider"
docker compose logs --since 5m worker | grep -iE "circuit|unavailable|provider returned"
# one affected run: look for llm_retry then llm_unavailable events with the provider error text (HTTP 429/5xx/timeout)
curl -s localhost:8000/v1/runs/<run_id>/trace | python3 -c "import sys,json;[print(e['type'],e.get('error')) for e in json.load(sys.stdin)['events'] if e['type'].startswith('llm_')]"
# talk to the provider directly with the same variables the workers use
docker compose exec worker sh -c 'curl -s -o /dev/null -w "%{http_code}\n" -m 10 -H "Authorization: Bearer $LLM_API_KEY" $LLM_BASE_URL/models'
```
HTTP 401/403 = key wrong or expired; 429 = quota/rate limit; 5xx or timeout = provider outage; connection refused = wrong `LLM_BASE_URL`.

**Recover**
1. Fix the cause: renew the key / wait for the quota window / correct `LLM_BASE_URL` in `.env`, then
   `docker compose up -d --force-recreate worker api` (config is read at start).
2. Optional resilience: set `LLM_FALLBACK_BASE_URL`, `LLM_FALLBACK_API_KEY`, `LLM_FALLBACK_MODEL` to a second provider. Runs answered by it are marked degraded (a different model was used) and still never `clear-for-filing`.
3. The breaker heals itself: after `CB_RECOVERY_S` (default 20 s) one probe call is allowed (half-open); success closes it. To force it closed after the provider is fixed:
   `docker compose exec redis redis-cli del cb:llm:open_until cb:llm:fails cb:llm:probe`.
4. Degraded results are **not cached**, so re-submitting the same product after recovery gets a fresh full run. Ask affected callers to resubmit; nothing else needs repair.
5. If the provider is slow rather than down, lower `LLM_TIMEOUT_S` and keep `LLM_MAX_RETRIES` at 2 so one slow call cannot consume the run's 90 s budget.

**Prevent**: set a spending limit at the provider; keep `RATE_LIMIT_PER_MIN` per client; watch *Cost per run*.

---------------------------------------------------------------------------------------------------

## FM-2 Runs stay `queued` (no live workers) or the queue backs up

**Symptoms**
* `POST /v1/classify` returns 202 but `GET /v1/runs/<id>` stays `queued` for longer than ~p95 (20 s).
* `/health/ready` reports `"workers_alive": 0` (status `degraded`) or `queue_depth` keeps growing; Grafana *Workers alive* = 0 or *Queue depth* rising, *queue wait* p95 dominating.
* The API starts answering `503 queue_full` once depth exceeds `MAX_QUEUE_DEPTH` (500) - deliberate backpressure.

**Confirm**
```bash
curl -s localhost:8000/health/ready | python3 -m json.tool | grep -E "workers_alive|queue_depth"
docker compose ps worker                       # exited / restarting / unhealthy?
docker compose logs --tail 50 worker           # traceback? Redis or Chroma connection error? out of memory?
docker compose exec redis redis-cli llen queue:runs          # waiting
docker compose exec redis redis-cli --scan --pattern 'processing:*'   # claimed by a worker (a dead worker's list is re-queued automatically)
docker compose exec redis redis-cli --scan --pattern 'worker:*'       # live heartbeats (expire after WORKER_HEARTBEAT_TTL_S = 15 s)
```
* `workers_alive = 0` and containers exited -> worker crash (read the traceback in the logs).
* `workers_alive >= 1`, queue growing, CPU low -> not enough concurrency (each worker runs `WORKER_CONCURRENCY` runs, each waiting on the model): scale out.
* Worker restart-looping with `chroma`/`redis` connection errors -> dependency down: `docker compose ps`.

**Recover**
1. Restart / scale: `docker compose up -d --scale worker=3` (A7 measured ~3x throughput, `docs/LOADTEST_RESULTS.md`).
2. **No work is lost.** A run claimed by a worker that dies sits in `processing:<worker>`; other workers (and the dead worker's replacement) move it back to the queue once its heartbeat has expired (<= 15 s). Verify: `llen queue:runs` drops after the new worker starts.
3. If one specific request crashes workers repeatedly (poison input), find it from the worker traceback (it logs `run_id`), then `GET /v1/runs/<id>` for the request, and remove it:
   `docker compose exec redis redis-cli lrem queue:runs 0 <run_id>` and, if it is in a processing list, `lrem processing:<worker_id> 0 <run_id>`.
4. If the backlog is stale and callers have given up, drain it: `docker compose exec redis redis-cli del queue:runs` (callers polling a dropped id will see `queued` forever, so tell them to resubmit).

---------------------------------------------------------------------------------------------------

## FM-3 (short) The index is empty or from an older corpus

**Symptom** `/health/ready` returns 503 with `index_chunks: 0`; or answers cite passages that differ from the PDFs.
**Confirm** `curl -s localhost:8000/v1/corpus` (chunk count, `built_at`, document sha256s) and compare with `sha256sum corpus/*.pdf`.
**Recover** `docker compose run --rm ingest` (idempotent: embeds only what changed). After changing the embedding model use
`docker compose run --rm ingest python -m app.corpus.ingest --reembed` (no image rebuild; for a few passages: `--reembed-ids HEADING-9617,GRI-1`).
Cached run results are keyed on the index version, so they are invalidated automatically.
