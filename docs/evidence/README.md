# Evidence captured on the demonstration laptop

Everything else in this repository is proven by an automated test or a recorded
measurement. Three requirements can only be proven by looking at a running
browser window, so their proof is a screenshot taken on the demonstration
laptop (Windows 11, Docker Desktop, 12 vCPU) with all 11 services up.

Drop the PNGs in this folder with exactly these names. `docs/REQUIREMENTS_CHECKLIST.md`
and `docs/LIMITATIONS.md` reference them by name.

| File | What it must show | Closes |
|---|---|---|
| `grafana-agent-dashboard.png` | The agent dashboard with **data in the panels** - per-agent success rate, steps and tool calls per run, latency p50/p95, retry rate, uptime. Run `docker compose exec api python scripts/demo.py all` first so the panels have something to draw, then set the time range to **Last 15 minutes**. | A6 |
| `grafana-quality-chart.png` | The live-scoring quality panel with points on it (needs a few runs plus one `/v1/runs/{id}/feedback` call - the web client's feedback box does this). | L6 |
| `jaeger-single-run-trace.png` | One run expanded into a single trace spanning **both** `api` and `worker`, with the span count visible. This is the correlation surviving the Redis queue hop. | D5 |

## How to capture (PowerShell, from the project folder)

```powershell
docker compose exec api python scripts/demo.py all      # gives the panels data
```

Then, within the next 15 minutes:

* **Grafana** - http://localhost:3000 (admin / admin, skip the password change) ->
  Dashboards -> the export-classifier dashboard. Set the range to *Last 15 minutes*.
  If a panel reads *No data*, the panel's PromQL is syntax-checked but its metric
  has not been produced yet; run `demo.py all` again and reload.
* **Jaeger** - http://localhost:16686 -> Service `export-classifier-api` -> *Find Traces*
  -> click the newest trace -> expand it so both service names and the span counts show.

Use `Win` + `Shift` + `S` to capture, then save into this folder under the name above.

## Why these are not automated

Rendering a Grafana panel requires a browser against a live Prometheus; asserting
on the rendered pixels would test Grafana, not this system. The PromQL behind the
23 panels is syntax-checked in the build environment, and every metric the panels
read is asserted present by `test_metrics_are_exposed`. The screenshot closes the
last step: that the queries return data from this system's own metrics.
