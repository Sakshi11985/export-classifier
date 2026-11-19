"""Prometheus metrics for the whole pipeline (F8, A6, D2, D5, D7, L5, L6).
The same module is imported by the API, the worker and the ingestion job; each process
exposes its own /metrics endpoint and Prometheus scrapes them all."""
from __future__ import annotations

import time

from prometheus_client import Counter, Gauge, Histogram

_LAT = (0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 10, 15, 20, 30, 45, 60, 90, 120)
START_TIME = time.time()

# ------------------------------------------------------------------ API
http_requests = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
http_latency = Histogram("http_request_duration_seconds", "HTTP request latency", ["route"], buckets=_LAT)
runs_submitted = Counter("runs_submitted_total", "Runs accepted and queued", ["cache"])
runs_started = Counter("agent_runs_started_total", "Agent runs started by a worker")
validation_rejections = Counter("validation_rejections_total", "Requests rejected at ingestion", ["kind"])
guardrail_blocks = Counter("guardrail_blocks_total", "Requests/outputs blocked by guardrails", ["stage", "category"])
rate_limited = Counter("rate_limited_total", "Requests rejected by the rate limiter", ["reason"])
quarantined = Counter("quarantine_records_total", "Records written to quarantine", ["reason"])
queue_depth = Gauge("queue_depth", "Runs waiting in the Redis queue")
queue_processing = Gauge("queue_processing", "Runs currently claimed by workers")
workers_alive = Gauge("workers_alive", "Workers with a live heartbeat")
service_uptime = Gauge("service_uptime_seconds", "Seconds since this process started")
feedback_total = Counter("feedback_total", "Feedback submissions", ["rating"])
feedback_ratio = Gauge("feedback_satisfaction_ratio", "Satisfactory / total feedback")

# ------------------------------------------------------------------ caches
cache_hits = Counter("cache_hits_total", "Cache hits", ["kind"])
cache_misses = Counter("cache_misses_total", "Cache misses", ["kind"])

# ------------------------------------------------------------------ worker / agents
run_e2e = Histogram("run_end_to_end_seconds", "Submission -> final result", ["route"], buckets=_LAT)
run_exec = Histogram("run_execution_seconds", "Worker execution time of a run", ["route"], buckets=_LAT)
run_queue_wait = Histogram("run_queue_wait_seconds", "Time spent waiting in the queue", buckets=_LAT)
runs_finished = Counter("agent_runs_total", "Finished runs", ["outcome", "route"])
step_latency = Histogram("agent_step_duration_seconds", "Latency per agent step", ["agent", "step"], buckets=_LAT)
steps_per_run = Histogram("agent_steps_per_run", "LLM/agent steps per run", ["agent"],
                          buckets=(1, 2, 3, 4, 5, 6, 8, 10, 12, 14, 20))
tool_calls_per_run = Histogram("agent_tool_calls_per_run", "Tool calls per run", ["agent"],
                               buckets=(0, 1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20))
tool_calls = Counter("agent_tool_calls_total", "Tool calls", ["tool", "status"])
tool_latency = Histogram("agent_tool_duration_seconds", "Tool latency", ["tool"], buckets=_LAT)
agent_tasks = Counter("agent_tasks_total", "Per-agent task outcomes", ["agent", "result"])
verifications = Counter("verification_total", "Verifier outcomes", ["outcome"])
verification_attempts = Histogram("verification_attempts_per_run", "Attempts needed", buckets=(1, 2, 3, 4))
escalations = Counter("escalations_total", "Runs routed to a human or clarification", ["route", "reason"])
loop_limit_hits = Counter("agent_loop_limit_hits_total", "Loop bounds reached", ["limit"])
degraded_runs = Counter("degraded_runs_total", "Runs completed on the fallback path", ["reason"])
live_quality = Gauge("live_quality_score", "Rolling live-scored quality metric", ["metric"])
live_scored = Counter("live_scored_runs_total", "Live runs auto-scored")
worker_up = Gauge("worker_up", "1 while the worker loop is running")
worker_inflight = Gauge("worker_inflight_runs", "Runs executing in this worker")

# ------------------------------------------------------------------ LLM (A5, L5)
llm_calls = Counter("llm_calls_total", "LLM calls", ["role", "status"])
llm_retries = Counter("llm_retries_total", "LLM retry attempts", ["role"])
llm_latency = Histogram("llm_call_duration_seconds", "LLM call latency", ["role"], buckets=_LAT)
llm_tokens = Counter("llm_tokens_total", "LLM tokens", ["role", "kind"])
llm_cost = Counter("llm_cost_usd_total", "LLM cost in USD", ["role", "step"])
llm_fallbacks = Counter("llm_fallback_total", "Fallback path activations", ["to"])
breaker_state = Gauge("llm_circuit_breaker_state", "0=closed 1=half-open 2=open")
breaker_transitions = Counter("llm_circuit_breaker_transitions_total", "Breaker transitions", ["to"])

# ------------------------------------------------------------------ ingestion (D2)
index_built_ts = Gauge("index_built_timestamp_seconds", "When the index was last built")
index_age = Gauge("index_age_seconds", "Age of the index")
index_chunks = Gauge("index_chunks_total", "Chunks in the index")
index_docs = Gauge("index_source_documents", "Source documents in the index")
ingest_duration = Gauge("ingest_duration_seconds", "Duration of the last ingestion run")
ingest_throughput = Gauge("ingest_chunks_per_second", "Throughput of the last ingestion run")
ingest_embedded = Gauge("ingest_chunks_embedded", "Chunks embedded in the last run")
ingest_skipped = Gauge("ingest_chunks_skipped", "Chunks skipped (unchanged) in the last run")
ingest_runs = Counter("ingest_runs_total", "Ingestion runs", ["status"])


def update_index_gauges(meta: dict | None) -> None:
    if not meta:
        return
    index_built_ts.set(meta.get("built_at_ts", 0))
    index_age.set(max(0.0, time.time() - meta.get("built_at_ts", time.time())))
    index_chunks.set(meta.get("chunk_count", 0))
    index_docs.set(len(meta.get("documents", [])))
    ingest_duration.set(meta.get("pipeline_duration_s", 0))
    ingest_throughput.set(meta.get("chunks_per_second", 0))
    ingest_embedded.set(meta.get("embedded_this_run", 0))
    ingest_skipped.set(meta.get("skipped_this_run", 0))
