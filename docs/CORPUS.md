# Corpus confirmation (Evaluation 1 table, filled from the supplied files)

| # | Document (supplied file) | Pages | Text layer | SHA-256 | Chunks |
|---|---|---|---|---|---|
| C1 | `custom_tariff_general_rules_for_interpretation.pdf` - GRI 1-6 | 41 | **Yes** | `0821f495f9a8687dbd4a18eb3e0d9ae89566752b4c4942599abce529bf244353` | 10 |
| C2-C4 | `Notification_ITCHS_ch73.pdf` - Chapter 73, Section XV notes, Chapter 73 notes, DGFT ITC(HS) export policy for Chapter 73 | 20 | **Yes** | `9ca430ea4ac9c2c23088e83366cee7b26dd4f4ea9d4a41a66f7ecf8a1562b3d3` | 65 |
| C3-C5 | `Notification_ITCHS_ch96.pdf` - Chapter 96, Chapter 96 notes, DGFT ITC(HS) export policy for Chapter 96 | 10 | **Yes** | `157252c443f851ec49f7f4df18253b9cc32240f70d25624ed71d8b29fe9cd614` | 47 |

No OCR was needed (the parser would stop with an instruction to run `ocrmypdf` for a page without text). The two ITCHS files are DGFT notifications that combine the tariff headings with the export-policy column, so the tariff and the policy schedule for each chapter come from the same file. Section XV (chapter 73) and Section XX (chapter 96) notes are inside them. **122 chunks** in total: 46 headings, 46 export-policy entries, 15 notes, 10 GRI rules, 2 chapters, 2 sections, 1 policy condition.

## Passages the section 4.1 case depends on (reference answer)
| Id | Role |
|---|---|
| `GRI-1` | classification is by the *terms of the headings* and any Section/Chapter Notes |
| `HEADING-9617` | "Vacuum flasks and other vacuum vessels, complete with cases; parts thereof other than glass inners." - names the article |
| `SEC-XV-NOTE-1` | Section XV ("This Section does not cover ...") excludes articles of Chapter 96 from Chapter 73 |
| `HEADING-7323` | table, kitchen or other household articles of iron or steel - the plausible **wrong** material-based heading |
| `POLICY-9617` | export policy items 96170011-96170090: Free |
Test values: `Stainless steel vacuum flask with a moulded plastic outer body.` -> `clear-for-filing`, heading **9617**, GRI 1, policy Free; citations GRI-1, HEADING-9617, SEC-XV-NOTE-1.

## Identifiers
`GRI-n`, `GRI-n(x)`, `SEC-XV-NOTE-n`, `CH73-NOTE-n`, `CH96-NOTE-n`, `HEADING-nnnn`, `POLICY-nnnn`, `CHn-POLICYCOND-n`, `CHAPTER-n`, `SECTION-XV|XX`.
Browse any of them: `curl localhost:8000/v1/clauses/HEADING-9617`.

## Chunking and embedding strategy (L1) and why it addresses section 3
* **Chunk by legal unit**, not by size: one chunk per GRI rule, per note, per heading (with its tariff items), per export-policy entry. A citation therefore points at exactly the provision it relies on, and the verifier can compare a quoted sentence with one passage.
* **Heading chunks start with the heading's own wording** ("Heading 9617 (Chapter 96): Vacuum flasks and ...") and carry metadata (chapter, section, linked notes). Policy chunks are separate so an export-policy statement is always traceable to the DGFT line.
* **Notes are linked** to the headings they govern (`linked_notes`), so `get_notes_for_heading` returns the overriding notes even though the request never mentions them - the "provision the request never mentions" problem of section 5.
* **Two retrieval paths** (`app/retrieval.py`): hybrid vector (Chroma) + BM25 merged by reciprocal-rank fusion for general questions, and an **article-name index** over heading/tariff-item descriptions with head-noun matching for "which heading names this article". A material query returns the steel chapter; the article query returns 9617. The agent decides which to call (A1).
* **Embeddings**: `sentence-transformers` locally (production default, model name via `EMBEDDING_MODEL`) or `hashing` (deterministic, offline). **All tests and measurements in this repository used `hashing`**; the semantic backend is implemented but untested here (`docs/LIMITATIONS.md`).
* **Re-embedding without a rebuild**: each stored chunk keeps a `content_hash` and `embed_sig` (embedder name+version). `ingest` embeds only chunks that are new, changed or have a different signature; `--reembed` forces all, `--reembed-ids A,B` forces some; chunks no longer in the corpus are deleted. Measured: 122 -> 0 -> 2 embedded on consecutive runs.
