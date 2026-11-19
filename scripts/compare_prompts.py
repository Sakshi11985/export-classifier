"""L2: run the SAME evaluation set with each classifier prompt variant and report which is better, on which metric, by how much.

    python scripts/compare_prompts.py v1 v2

IMPORTANT: prompt text only influences a real language model. With LLM_PROVIDER=offline the deterministic policy ignores the
prompt, so both variants score identically - the script says so instead of reporting a difference. A meaningful comparison
needs LLM_PROVIDER=openai_compat pointing at a real model (see .env.example)."""
import json
import os
import subprocess
import sys

a, b = (sys.argv[1:3] + ["v1", "v2"])[:2] if len(sys.argv) < 3 else sys.argv[1:3]
env = dict(os.environ, PYTHONPATH=".")
if env.get("LLM_PROVIDER", "offline") in ("offline", "") or "stub" in env.get("LLM_BASE_URL", ""):
    print("WARNING: the configured provider is the offline policy or the stub. Neither reads the prompt, so any difference "
          "below is zero by construction. Use a real model for a real comparison.\n")
for v in (a, b):
    subprocess.run([sys.executable, "-m", "app.evaluation.run_eval", "--label", f"prompt_{v}"], env={**env, "PROMPT_VARIANT": v},
                   check=True, stdout=subprocess.DEVNULL)
A = json.load(open(f"data/eval_reports/prompt_{a}.json"))
B = json.load(open(f"data/eval_reports/prompt_{b}.json"))
keys = [("pass_rate", A["pass_rate"], B["pass_rate"]), ("route_accuracy", A["route_accuracy"], B["route_accuracy"])] + \
       [(k, A["metrics"][k], B["metrics"][k]) for k in ("groundedness", "context_relevance", "faithfulness", "hallucination", "context_recall")] + \
       [("confident_wrong", len(A["confident_wrong_answers"]), len(B["confident_wrong_answers"])), ("avg_steps", A["avg_steps"], B["avg_steps"]),
        ("cost_per_100_runs_usd", A["cost"]["per_100_runs_usd"], B["cost"]["per_100_runs_usd"])]
print(f"{'metric':26s}{a:>12s}{b:>12s}{'delta':>10s}")
for k, x, y in keys:
    print(f"{k:26s}{str(x):>12s}{str(y):>12s}{(y - x) if None not in (x, y) else 0:>+10.4f}")
better = [k for k, x, y in keys if None not in (x, y) and k not in ("hallucination", "confident_wrong", "avg_steps", "cost_per_100_runs_usd") and y > x]
print(f"\n{b} better than {a} on: {better or 'nothing'}")
