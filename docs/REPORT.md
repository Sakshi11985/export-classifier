# Export Classification Check Service — report

Mini Project 07, UE24AM342AA5 (ML System Design and AgentOps). Component: Jackfruit, Level 3.

---

## 1. Problem and scope

Every export shipment must carry an HS code. That code decides the duty, whether a licence is needed and which
documents accompany the goods; a wrong code can have the consignment held at customs and attract penalties.
Finding the code is not a lookup. It is governed by the General Rules for Interpretation, applied in strict
numerical order, and by section and chapter notes that override the plain language of a heading.

The service accepts a product description, validates it, proposes a tariff **heading** with the rule applied and
the passages it relied on, states the DGFT export policy for that heading, and routes the result to one of
**clear-for-filing**, **seek-product-clarification** or **specialist-classification-review**. It never files a
declaration; a licensed customs broker decides.

**In scope:** Customs Tariff chapters 73 and 96 with their section and chapter notes, the GRI, and the DGFT
ITC(HS) export policy schedule for those chapters — three PDFs, 122 indexed passages.
**Out of scope:** duty calculation, shipping documentation, model training, cloud deployment, and any chapter
outside 73 and 96 (those escalate to a human by design). Only the 4-digit heading is proposed; the 8-digit
tariff item is shown with its policy but left to the broker.

### The case the brief fixes as the reference answer

> "Stainless steel vacuum flask with a moulded plastic outer body."

A search over the words of the request lands in chapter 73, because the description is mostly about materials.
Heading **9617** names vacuum flasks and is never mentioned in the request; Section XV Note 1 excludes articles
of chapter 96 from chapter 73. The system returns 9617 under **GRI 1**, citing `GRI-1`, `HEADING-9617` and
`SEC-XV-NOTE-1`, with 7323/7304/7306/7307 listed as excluded by that note, and export policy *Free*. GRI 3 is
never reached, because only one heading survives GRI 1.

---

## 2. Agent design

### Topology and hand-off

Two agents with different responsibilities, different prompts and their own tool calls:

| Agent | Responsibility |
|---|---|
| **Classifier** | Identifies the article and its materials, chooses which tools to call, proposes a heading with the rule applied and verbatim quotes as evidence |
| **Verifier** | Independently re-checks the proposal. It runs its *own* article-name search rather than reading the classifier's, so it can find a heading the classifier never considered. It can overrule the classifier |

Hand-off: `classifier → Proposal → verifier → VerificationReport{passed, checks, next_strategy} → router`.
The router decides the route and assembles the response; the export-policy statement is composed by code from
the retrieved DGFT passage, never written by a model. The graph is LangGraph; retrieval is a tool the agent
chooses to call, not a fixed stage.

### Tools (five)

`search_corpus(query, top_k, chapter, types)` · `search_headings_by_article(article)` ·
`get_notes_for_heading(heading)` · `get_clause(id)` · `get_request_fields()`.

`search_headings_by_article` is the tool that makes the section 4.1 case solvable: it asks which heading *names*
this article, rather than which passage resembles the request.

### Verification (requirement A3)

Before any answer is returned, eleven checks run: every cited id must be a passage this run actually retrieved;
every quote must be a verbatim substring of it; the chosen heading's passage must be retrieved, cited and in
scope; the GRI rule claimed must be retrieved and cited; the notes applying to the chosen heading must have been
retrieved; every "excluded by note" alternative must cite a retrieved note that really names that chapter or
heading; no heading that names the article may be left unchosen and unexplained; the proposed article must
appear in the request and the chosen heading must name that article; the escalation flags must agree with
independent evidence; the free text must carry no forbidden claim; and, with a real provider, an independent
judge model must also find the proposal supported.

A failed verification names the next search strategy. The run retries with that strategy, and escalates if it
fails again. **An unverified answer is never returned as a confident answer.**

### Loop limits

`AGENT_MAX_STEPS` 20 model calls, `AGENT_MAX_TOOL_CALLS` 30, `AGENT_RUN_TIMEOUT_S` 90 wall clock,
`TOOL_TIMEOUT_S` 10 per tool, `MAX_VERIFY_ATTEMPTS` 2. A run that hits a limit is routed to specialist review,
never returned as an answer.

### If an agent fails mid-run

Classifier error or no proposal → no verifier call, result is specialist review. Verifier unavailable or
erroring → it **fails closed**; an unverified proposal is never cleared. Provider down → retries with backoff,
then the circuit breaker, then the fallback chain, with the run marked `degraded` and routed to review. Whole
worker dies → its claimed run is re-queued by another worker once the heartbeat expires.

---

## 3. Architecture and deployment boundaries

The full diagram is in [ARCHITECTURE.md §1](ARCHITECTURE.md), with boundaries marked. Eleven services under
Docker Compose: `redis`, `chroma`, `llm-stub`, `ingest` (one-shot), `api`, `worker` (scalable), `jaeger`,
`prometheus`, `cadvisor`, `grafana`, and `locust` under a profile.

**Why the boundaries sit where they do.** `api` and `worker` are separate because they have opposite resource
profiles and failure modes: the API is short, cheap and must stay responsive, while a run is tens of seconds and
several model calls. Separating them lets the workers scale (`--scale worker=3`) and fail without taking the
interface down, and puts a queue between submission and execution so a burst becomes depth rather than errors.
`chroma` is separate for its own memory profile and so the index survives worker restarts. `redis` carries the
queue, run store, caches, rate limits and traces. The monitoring plane is separate so it can be dropped
entirely without touching the service.

**Asynchronous execution (A4).** `POST /v1/classify` validates, enqueues and returns `202` with a run id in
well under a second; the client polls `GET /v1/runs/{id}`. Polling was chosen over callbacks because a browser
and a `curl` user need no inbound endpoint and no delivery-retry machinery.

**Web client (A8).** One page, no build step, fonts served from the image so it needs no external network. It
polls the trace endpoint while a run executes and draws each tool call as it happens — the worker streams trace
events to Redis mid-run for exactly this — then shows the determination, the statute quoted in full with each
citation marked as resolving, and the verifier's attempt-by-attempt audit trail.

---

## 4. Design decisions, and what was rejected

| Decision | Rejected alternative | Why |
|---|---|---|
| Chunk by legal unit — one chunk per GRI rule, note, heading and policy entry, tagged with its identifier | Fixed-size overlapping windows | A note must stay whole and stay addressable: the verifier resolves citations *by id*, which fixed windows cannot provide |
| A dedicated article-name index beside the vector search | Rely on embedding similarity alone | The section 4.1 case turns on a provision the request never mentions; similarity search cannot retrieve it |
| Hybrid retrieval (vector + BM25, reciprocal rank fusion) | Vector only | Tariff text is short and highly lexical; heading numbers and exact article names matter |
| A second agent with its own searches | A second prompt in the same process | The brief rules that out, and a verifier that re-reads the classifier's own retrievals cannot catch what the classifier never searched for |
| Policy statement composed by code from the retrieved passage | Let the model write it | Export policy is quoted, not reasoned; a model adds risk and no value |
| Whole-run cache keyed on request + index version + prompt variant + provider | Cache the model call | The whole run is the expensive unit, and the key must invalidate on re-ingest or prompt change. Degraded results are never cached |
| Reliable queue with heartbeat and orphan re-queue | Simple `LPOP` | A worker dying mid-run would silently lose the run |
| Provider behind an interface, with an offline deterministic policy as the final fallback | Call the provider directly | The brief asks for the provider to be changeable by configuration; the offline policy also makes the whole system testable and demonstrable without a key |

---

## 5. Evaluation, before and after

31 scenarios (brief minimum 20), each with its expected outcome and the passages that justify it, runnable as
a batch from the command line. The deliberate change: **single retrieval pass with no verifier** — the
"straightforward implementation" of section 5 of the brief — **to the tool-using agent with an independent
verifier and retry**.

| Metric | Before | After | Change |
|---|---|---|---|
| Scenarios passed | 21/31 (0.677) | **31/31 (1.0)** | +0.323 |
| Route accuracy | 0.731 | 1.0 | +0.269 |
| **Confident wrong answers** | **10** | **0** | −10 |
| Groundedness | 1.0 | 1.0 | 0 |
| Context relevance | 0.411 | 0.431 | +0.020 |
| Faithfulness | 0.667 | 1.0 | +0.333 |
| Hallucination rate | 1.0 | 0.0 | −1.0 |
| Context recall | 0.342 | 1.0 | +0.658 |
| Steps / tool calls per run | 2.0 / 1.0 | 7.3 / 10.4 | ≈5× |

How to read it: the baseline's quotes are genuine — groundedness is already 1.0 — but it never reads the notes
and never compares headings, so its *claims* are unsupported and it answers everything confidently, including a
cotton t-shirt and a two-article set. The agent costs about five times the steps and removes all ten confident
wrong answers. How each metric is computed is in [EVALUATION.md](EVALUATION.md).

**Caveat, stated plainly:** these scenarios were written against the corpus by the same authors as the system
and were used while developing the matcher, so 31/31 is a regression result, not an unbiased accuracy estimate.
The four metrics are deterministic proxies computed from the result, the retrieved passages and the corpus —
they are not TruLens numbers, and they cannot detect a wrong-but-well-cited heading; route accuracy against the
gold labels is what covers that.

---

## 6. Service level objective, results, bottleneck and cost

**Declared before measuring** ([SLO.md](SLO.md), hash in `SLO.sha256`): p95 for a complete run, from submission
to the first terminal poll, **≤ 20 s at 10 concurrent users with one worker**, error budget < 1 %. Justified
against section 1 of the brief: the task it replaces is minutes of an analyst's time, and the brief itself says
runs take tens of seconds, so 20 s is the longest an analyst will wait with the page open.

| Configuration | Runs/s | p50 | p95 | Target |
|---|---|---|---|---|
| 1 worker, 10 users (the declared load) | 0.75 | 11 s | **15 s** | **met** |
| 1 worker, 20 users | 0.75 | 25 s | 26 s | not met |
| 3 workers, 20 users | 2.24 | 8.5 s | 10 s | met |
| 3 workers, 30 users | 2.08 | 12 s | 17 s | met |
| 3 workers, 40 users | 1.91 | 16 s | 26 s | not met |

**Where it fails:** between 15 and 20 users with one worker, and between 30 and 40 with three.
**Effect of `--scale worker=3`:** throughput 0.75 → 2.24 runs/s, a factor of 3.0, and p95 at 20 users falls from
26 s to 10 s.

**The bottleneck is agent-worker run slots, not compute.** A worker executes at most `WORKER_CONCURRENCY=4` runs
at once and a run is a strictly sequential chain of about five model calls of ~1.1 s, which predicts
4 ÷ 5.6 s = 0.71 runs/s per worker. We measured 0.73 on a 2-core machine and 0.75 on a 12-core laptop — six
times the CPU changed nothing, which is the clearest evidence the limit is structural. During a 40-user run the
containers sat at 2–3 % CPU while runs queued. The remedy is more workers or a higher `WORKER_CONCURRENCY`,
until the provider's own rate limit becomes the binding constraint.

**Caching (D7):** a whole-run cache hit returns in 3 ms with **zero** model calls, against 5.1 s and 5 calls
cold. **Cost (L5):** about **USD 0.27 per 100 runs** at illustrative prices supplied through the environment,
of which **88 % comes from the `classifier.plan` steps** — four of the five calls, each re-sending a growing
tool transcript (prompt tokens grow 703 → 2 397 → 5 016 → 5 225). The levers are the loop bounds, shorter tool
outputs and the run cache.

---

## 7. What did not work

**A wrong answer passed verification.** The first time the system ran under Docker, "Cotton t-shirt" came back
as **clear-for-filing, heading 9617, verified**. Every existing check passed, because every citation in that
answer was genuine corpus text — nothing tied the answer to the request that had been asked. Two checks were
added (`article_grounded_in_request`, `heading_matches_request`), the same request now escalates, and a
regression test for it runs over HTTP. This is INC-J in the incident log and it is the most useful thing we
found.

Other real failures, all in [INCIDENT_LOG.md](INCIDENT_LOG.md) with their fixes and guards: a product *set*
answered confidently for one of its articles; an exclusion note applied to headings it did not name; "galvanised
steel nails" classified under *nail brushes*; "steel wool" losing its noun to the material stripper; a verifier
rejecting a *correct* clarification request; Redis protected mode blocking the ingest job on the first compose
run; and the `uvicorn` console script not being on `PATH` in the image.

**What is not done.**

* **No real language model has been run.** Every result here comes from the deterministic offline policy or the
  fixed-response stub. The provider interface, timeouts, retry, breaker, fallback and cost accounting are real
  and tested, but the model's reasoning quality — and therefore the system's true accuracy — is unmeasured.
  This also blocks **L2**: the prompt-variant comparison script exists and runs, but with no model reading the
  prompts it reports zero difference, which is not evidence about the prompts.
* **Semantic embeddings were never run.** Everything was measured on the lexical `hashing` backend;
  sentence-transformers needs a model download that the build environment could not reach.
* **The four evaluation metrics are deterministic proxies, not TruLens.**
* **Not verified in the build environment:** the Jaeger, Prometheus, Grafana and cAdvisor images could not be
  pulled there. All eleven services were later started together on the demonstration laptop, where the Jaeger
  trace view and the Grafana dashboard were both confirmed working.

Full list, with what was verified where: [LIMITATIONS.md](LIMITATIONS.md). Requirement-to-test map:
[REQUIREMENTS_CHECKLIST.md](REQUIREMENTS_CHECKLIST.md).

---

## 8. Operating it

[README.md](../README.md) runs it from a clean clone · [WINDOWS.md](WINDOWS.md) for Windows and Docker Desktop ·
[RUNBOOK.md](RUNBOOK.md) for the two most likely failure modes · [DATA_CONTRACT.md](DATA_CONTRACT.md) for the
interface · [CORPUS.md](CORPUS.md) for what was indexed · [DEMO_SCRIPT.md](DEMO_SCRIPT.md) for the
demonstration. 116 automated tests pass.
