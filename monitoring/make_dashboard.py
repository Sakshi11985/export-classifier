"""Generates monitoring/grafana/dashboards/export-classifier.json (so panel queries are reviewable as code).
    python monitoring/make_dashboard.py"""
import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prom"}
panels, _id = [], [0]
CONT = 'name=~"export-classifier-.+"'          # cAdvisor per-container filter (unavailable on Docker Desktop, see row 5)
APP = 'job=~"api|worker"'                      # this system's own processes: the API and every worker replica


def panel(title, exprs, x, y, w=8, h=7, kind="timeseries", unit=None, desc="", thresholds=None, minv=None, maxv=None):
    _id[0] += 1
    targets = [{"datasource": DS, "expr": e, "legendFormat": l, "refId": chr(65 + i)} for i, (e, l) in enumerate(exprs)]
    d = {"id": _id[0], "type": kind, "title": title, "description": desc, "datasource": DS, "targets": targets,
         "gridPos": {"x": x, "y": y, "w": w, "h": h}, "fieldConfig": {"defaults": {}, "overrides": []}, "options": {}}
    if unit:
        d["fieldConfig"]["defaults"]["unit"] = unit
    if minv is not None:
        d["fieldConfig"]["defaults"]["min"] = minv
    if maxv is not None:
        d["fieldConfig"]["defaults"]["max"] = maxv
    if thresholds:
        d["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": thresholds}
    panels.append(d)


def q(p, metric, by="le", w="1m"):
    return f"histogram_quantile({p}, sum by ({by}) (rate({metric}_bucket[{w}])))"


GREEN_RED = lambda t: [{"color": "green", "value": None}, {"color": "red", "value": t}]
RED_GREEN = lambda t: [{"color": "red", "value": None}, {"color": "green", "value": t}]

# row 1 - the numbers the SLO is judged on
panel("p95 end-to-end latency (SLO 20 s)", [(q(0.95, "run_end_to_end_seconds"), "p95")], 0, 0, 6, 5, "stat", "s",
      "Submission to final result, from the worker's clock. SLO: docs/SLO.md", GREEN_RED(20))
panel("Availability (API up, 5 m)", [('avg_over_time(up{job="api"}[5m])', "api")], 6, 0, 6, 5, "stat", "percentunit", "Share of scrapes in which the API answered",
      RED_GREEN(0.99), 0, 1)
panel("Workers alive", [("workers_alive", "workers")], 12, 0, 6, 5, "stat", None, "Workers with a live heartbeat")
panel("Queue depth / in progress", [("queue_depth", "waiting"), ("queue_processing", "running")], 18, 0, 6, 5, "stat")

# row 2 - latency, throughput, errors
panel("End-to-end latency p50 / p95 / p99 vs SLO", [(q(0.5, "run_end_to_end_seconds"), "p50"), (q(0.95, "run_end_to_end_seconds"), "p95"),
                                                     (q(0.99, "run_end_to_end_seconds"), "p99"), ("vector(20)", "SLO p95 (20 s)")], 0, 5, 12, 8, "timeseries", "s")
panel("Throughput (runs/s) and HTTP requests/s", [("sum(rate(agent_runs_total[1m]))", "runs finished/s"),
                                                   ("sum(rate(runs_submitted_total[1m]))", "runs submitted/s"),
                                                   ("sum(rate(http_requests_total[1m]))", "HTTP requests/s")], 12, 5, 12, 8, "timeseries", "ops")
panel("Error rate", [('sum(rate(http_requests_total{status=~"5.."}[1m])) / clamp_min(sum(rate(http_requests_total[1m])), 0.001)', "HTTP 5xx ratio"),
                     ('sum(rate(agent_runs_total{outcome="failed"}[1m])) / clamp_min(sum(rate(agent_runs_total[1m])), 0.001)', "runs failed/unverified ratio")],
      0, 13, 8, 7, "timeseries", "percentunit")
panel("Latency per step (p95)", [(q(0.95, "agent_step_duration_seconds", "le, agent, step"), "{{agent}} {{step}}")], 8, 13, 8, 7, "timeseries", "s")
panel("Queue wait vs execution (p95)", [(q(0.95, "run_queue_wait_seconds"), "queue wait p95"), (q(0.95, "run_execution_seconds"), "execution p95")],
      16, 13, 8, 7, "timeseries", "s", "If queue wait dominates under load, add workers (A7).")

# row 3 - agents (A6)
panel("Task success rate per agent", [('sum by (agent) (rate(agent_tasks_total{result="success"}[5m])) / sum by (agent) (rate(agent_tasks_total[5m]))', "{{agent}}")],
      0, 20, 8, 7, "timeseries", "percentunit", "classifier: proposal produced; verifier: verification executed", minv=0, maxv=1.05)
panel("Steps and tool calls per run (avg)", [("sum(rate(agent_steps_per_run_sum[5m])) / sum(rate(agent_steps_per_run_count[5m]))", "model steps"),
                                              ("sum(rate(agent_tool_calls_per_run_sum[5m])) / sum(rate(agent_tool_calls_per_run_count[5m]))", "tool calls")], 8, 20, 8, 7)
panel("Verification outcomes / escalations", [("sum by (outcome) (rate(verification_total[5m]))", "verify {{outcome}}"),
                                               ("sum by (route) (rate(escalations_total[5m]))", "escalated: {{route}}")], 16, 20, 8, 7, "timeseries", "ops")

# row 4 - LLM provider (A5, L5)
panel("LLM retry rate", [("(sum(rate(llm_retries_total[1m])) or vector(0)) / clamp_min(sum(rate(llm_calls_total[1m])), 0.001)", "retries per call")], 0, 27, 6, 7, "timeseries", "percentunit")
panel("Circuit breaker (0 closed, 1 half-open, 2 open)", [("max(llm_circuit_breaker_state)", "breaker"), ("sum(increase(llm_fallback_total[5m]))", "fallbacks (5 m)")], 6, 27, 6, 7)
panel("Cost per run (USD) and tokens/s", [("sum(increase(llm_cost_usd_total[5m])) / clamp_min(sum(increase(agent_runs_total[5m])), 1)", "USD per run"),
                                          ("sum(rate(llm_tokens_total[1m]))", "tokens/s")], 12, 27, 6, 7)
panel("Cost by step (USD/min)", [("sum by (step) (rate(llm_cost_usd_total[5m])) * 60", "{{step}}")], 18, 27, 6, 7, "timeseries", "currencyUSD")

# row 5 - containers (resource utilisation, uptime)
# Source: each service's own /metrics (prometheus_client's process collector), not cAdvisor.
# cAdvisor is scraped and kept in the stack, but under Docker Desktop / WSL 2 it resolves only the
# cgroup tree (id="/", "/docker", ...) and emits no per-container series, so `name=~"export-classifier-.+"`
# matches nothing there. The process metrics cover the application services on any host. See docs/LIMITATIONS.md.
panel("CPU per service (cores)", [(f"sum by (job, instance) (rate(process_cpu_seconds_total{{{APP}}}[1m]))", "{{job}} {{instance}}")], 0, 34, 8, 7,
      "timeseries", None, "API and each worker replica, from their own process counters (cAdvisor reports no per-container series on Docker Desktop).")
panel("Memory per service", [(f"process_resident_memory_bytes{{{APP}}}", "{{job}} {{instance}}")], 8, 34, 8, 7, "timeseries", "bytes",
      "Resident set size per application process.")
panel("Service uptime", [(f"time() - process_start_time_seconds{{{APP}}}", "{{job}} {{instance}}")], 16, 34, 8, 7, "timeseries", "s",
      "Seconds since each application process started (A6 uptime).")

# row 6 - data, guardrails, caching, quality (D1, D2, D7, L4, L6)
panel("Index freshness", [("time() - max(index_built_timestamp_seconds)", "index age (s)"), ("max(index_chunks_total)", "chunks")], 0, 41, 6, 7, "timeseries", None,
      "Built-at, chunk count and ingest throughput are also in /v1/corpus")
panel("Rejected / blocked / rate-limited", [("sum by (kind) (increase(validation_rejections_total[5m]))", "rejected {{kind}}"),
                                            ("sum by (category) (increase(guardrail_blocks_total[5m]))", "blocked {{category}}"),
                                            ("sum by (reason) (increase(rate_limited_total[5m]))", "rate limited {{reason}}")], 6, 41, 6, 7)
panel("Cache hit ratio", [("sum by (kind) (rate(cache_hits_total[5m])) / clamp_min(sum by (kind) (rate(cache_hits_total[5m]) + rate(cache_misses_total[5m])), 0.001)", "{{kind}}")],
      12, 41, 6, 7, "timeseries", "percentunit", minv=0, maxv=1)
panel("Live quality (auto-scored sample)", [("live_quality_score", "{{metric}}")], 18, 41, 6, 7, "timeseries", None,
      "Groundedness, context relevance, faithfulness, hallucination - same metrics as the eval set", minv=0, maxv=1)

dash = {"uid": "export-classifier", "title": "Export Classification Check Service", "schemaVersion": 39, "version": 1, "refresh": "5s",
        "time": {"from": "now-30m", "to": "now"}, "timezone": "browser", "tags": ["export-classifier"],
        "links": [{"title": "Jaeger traces", "type": "link", "url": "http://localhost:16686", "targetBlank": True}],
        "templating": {"list": []}, "annotations": {"list": []}, "panels": panels}
out = Path(__file__).parent / "grafana" / "dashboards" / "export-classifier.json"
out.write_text(json.dumps(dash, indent=1))
print("wrote", out, len(panels), "panels")
