"""Locust load test (D6). Measures a COMPLETE run: submit -> poll until terminal status.

The submit call and every poll are reported as ordinary requests, and the whole run is reported as one
extra request named "RUN end-to-end (submit->result)". That row is the one compared with docs/SLO.md.

  locust -f loadtest/locustfile.py --headless -u 10 -r 2 -t 90s --host http://localhost:8000 \
         --csv data/loadtest/c10 --only-summary

Environment
  LOCUST_MODE   cold (default): every request is unique -> run cache miss, measures the agent
                cached:        the same request every time -> measures run-cache hits (D7)
  RUN_TIMEOUT_S give up on a run after this many seconds and count it as a failure (default 120)
The LLM behind the API must be the stub (LLM_PROVIDER=openai_compat, LLM_BASE_URL=<stub>) so the test measures
this system and not a provider's rate limits."""
import itertools
import os
import time
import uuid

from locust import HttpUser, between, events, task

MODE = os.environ.get("LOCUST_MODE", "cold")
RUN_TIMEOUT = float(os.environ.get("RUN_TIMEOUT_S", "120"))
_counter = itertools.count(1)


class Analyst(HttpUser):
    wait_time = between(0.2, 0.8)

    def on_start(self):
        # a distinct client id per simulated user: rate limiting is per client (D7) and stays at its defaults
        self.client_id = f"load-{uuid.uuid4().hex[:10]}"
        self.headers = {"X-Client-Id": self.client_id}

    def _payload(self):
        if MODE == "cached":
            return {"product_description": "Stainless steel vacuum flask with a moulded plastic outer body.",
                    "materials": ["stainless steel", "plastic"]}
        n = next(_counter)
        return {"product_description": f"Stainless steel vacuum flask with a moulded plastic outer body, model VF-{n}-{uuid.uuid4().hex[:6]}.",
                "materials": ["stainless steel", "plastic"], "capacity_litres": 1.0}

    @task
    def full_run(self):
        t0 = time.perf_counter()
        exc = None
        try:
            with self.client.post("/v1/classify", json=self._payload(), headers=self.headers,
                                  name="POST /v1/classify", catch_response=True) as r:
                if r.status_code not in (200, 202):
                    r.failure(f"submit {r.status_code}")
                    raise RuntimeError(f"submit failed {r.status_code}")
                body = r.json()
            run_id, status = body["run_id"], body["status"]
            delay = max(0.2, body.get("poll_after_ms", 700) / 1000)
            while status not in ("completed", "failed"):
                if time.perf_counter() - t0 > RUN_TIMEOUT:
                    raise TimeoutError(f"run {run_id} not finished after {RUN_TIMEOUT}s")
                time.sleep(delay)
                with self.client.get(f"/v1/runs/{run_id}", headers=self.headers, name="GET /v1/runs/{id}",
                                     catch_response=True) as p:
                    if p.status_code != 200:
                        p.failure(f"poll {p.status_code}")
                        raise RuntimeError(f"poll failed {p.status_code}")
                    status = p.json()["status"]
            if status == "failed":
                raise RuntimeError("run ended in status failed")
        except Exception as e:  # noqa: BLE001
            exc = e
        events.request.fire(request_type="RUN", name="RUN end-to-end (submit->result)",
                            response_time=(time.perf_counter() - t0) * 1000, response_length=0, exception=exc,
                            context={})
