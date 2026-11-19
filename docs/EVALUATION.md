# Evaluation (L2, L3, L6)

`app/evaluation/eval_set.jsonl` - **31 scenarios** (spec minimum 20). Each has the input, the expected outcome (route, heading, reason code) and `supporting`: the corpus passage ids that justify it.
Run: `PYTHONPATH=. python -m app.evaluation.run_eval --label after` (CLI batch; exit code 1 with `--min-pass-rate`). Reports: `data/eval_reports/<label>.json`; copies of the final ones in `docs/eval_before.json`, `docs/eval_after.json`.

Mix: the section 4.1 vacuum flask (E01, E25 variant), nine clean classifications in Chapters 73/96, capacity-dependent cases (E15 vs E16), steel wool/nails/anchor/rope regressions, a set that must tie (E06), an under-specified product that needs clarification (E04, E16), out-of-corpus products that must escalate (E05, E26-E28), two rejected-at-validation (E07, E29) and three blocked by guardrails (E08, E30, E31).

## Metrics - how each is computed (`app/evaluation/scoring.py`)
TruLens needs a judge model; to keep the evaluation runnable offline and reproducible, the four metrics are computed **deterministically** from the result, the passages the agent actually retrieved, and the corpus:
* **Groundedness** - share of the answer's citations whose id was retrieved in the run AND whose quote is a verbatim substring of the retrieved passage.
* **Context relevance** - share of retrieved passages that are relevant: cited, listed as gold-supporting for the scenario, or sharing a content word with the article. (Low by design: the agent deliberately searches broadly - about 0.43.)
* **Faithfulness** - share of the answer's atomic claims entailed by the passage cited for them (chosen heading and its description, applied GRI rule, each "excluded by note" alternative, each tied heading, the policy statement).
* **Hallucination rate** - fraction of runs with *any* unknown/unresolved citation, non-verbatim quote, non-existent heading or unsupported claim (strict: one bad claim marks the run).
* Extra: **context recall** - share of the scenario's gold passages that were retrieved.
Limits: these are string/structure checks, not semantic judgments; they cannot detect a wrong-but-well-cited heading - that is what **route/heading accuracy** against the gold labels is for. They are not TruLens numbers.

## The deliberate change, before and after (L3)
Change: **single retrieval pass, no verifier** (`--strategy baseline_single_pass --no-verifier`: the "straightforward implementation" of spec section 5) -> **tool-using agent with independent verifier and retry** (the system).
Same 31 scenarios, same corpus and provider (offline policy, hashing embeddings).

| metric | before | after | delta |
|---|---|---|---|
| scenarios passed | 21 / 31 (0.677) | **31 / 31 (1.0)** | +0.323 |
| route accuracy | 0.731 | 1.0 | +0.269 |
| confident wrong answers | **10** (E04 E05 E06 E15 E16 E21 E23 E26 E27 E28) | **0** | -10 |
| groundedness | 1.0 | 1.0 | 0 |
| context relevance | 0.411 | 0.431 | +0.020 |
| faithfulness | 0.667 | 1.0 | +0.333 |
| hallucination rate | 1.0 | 0.0 | -1.0 |
| context recall | 0.342 | 1.0 | +0.658 |
| avg steps / tool calls per run | 2.0 / 1.0 | 7.3 / 10.4 | +5.3 / +9.4 |
Reading it: the baseline's quotes are real (groundedness 1.0) but it never reads the notes or compares headings, so its claims are unsupported (faithfulness 0.67, hallucination 1.0 by the strict definition) and it answers *everything* confidently, including a t-shirt, leather handbag, aluminium foil and a two-article set. The price is ~5x the steps. Run-to-run variation of context relevance is about +-0.002 (retrieval cache/ordering); other metrics were identical in repeated runs.

**Honest caveat:** the scenarios were written against the corpus by the same authors as the system and used while developing the matcher (INC-B..G), so 31/31 is a regression suite, not an unbiased accuracy estimate. The vacuum flask result is the one the spec fixes as the reference answer.

## Prompt variants (L2)
Prompts are versioned files (`app/prompts/templates/classifier.v1.txt`, `.v2.txt`, `verifier.v1.txt`); every result carries `prompt_versions` = `name@version:sha256[:12]`. v1 is a 9-line instruction; v2 adds the explicit procedure of spec section 5 (retrieve, look for modifying notes, search again with what was read, GRI order, verify citations) and the safety rules.
`python scripts/compare_prompts.py v1 v2` runs the same set with each and reports the metric deltas.
**Result here: no difference (all deltas 0) and that is not evidence about the prompts** - the offline policy and the stub never read the prompt. **A real comparison requires a real model; it has not been run.** (Declared in `docs/LIMITATIONS.md`.)

## Live quality (L6)
`LIVE_SCORE_SAMPLE_RATE` (0.2 default) of finished runs are scored with the same four metrics inside the worker; `GET /v1/quality` returns the rolling average and series, exported as `live_quality_score{metric}` and charted in Grafana. Feedback: `POST /v1/runs/{id}/feedback`, `GET /v1/feedback/summary`.
