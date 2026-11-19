"""Deterministic RAG-quality metrics (L3, L6) - the 'equivalent' to TruLens' RAG triad that needs no
judge model, so it can run offline, on every eval scenario and on sampled live runs.

All four are computed from three objects only: the final result, the passages the agent actually
retrieved (id -> text) and the corpus. No model is involved, so they are reproducible.

groundedness      share of the answer's citations whose id was retrieved AND whose quote is a verbatim
                  substring of that retrieved passage. (None if the answer makes no citations.)
context_relevance share of retrieved passages that are relevant: cited in the answer, or listed as
                  gold-supporting for the scenario (eval only), or containing at least one content
                  word of the article being classified.
faithfulness      share of the answer's atomic claims entailed by the passage cited for them. Claims:
                  chosen heading (+ its description) / applied GRI rule / each 'excluded by note'
                  alternative / each tied heading / export-policy statement.
hallucination     1 for a run if ANY citation id is unknown or unresolved, any quote is not verbatim,
                  the heading does not exist in the corpus, or any claim is unsupported; the reported
                  hallucination rate is the mean over runs."""
from __future__ import annotations

import re

from ..entities import extract_entities
from ..textutil import squash, tokenize


def _verbatim(quote: str, text: str | None) -> bool:
    return bool(text) and squash(quote) in squash(text)


def score_run(result: dict, retrieved: dict[str, str], request: dict, corpus, gold_ids: list[str] | None = None) -> dict:
    cits = result.get("citations") or []
    cls = result.get("classification")
    cited_ids = {c["id"] for c in cits}

    # ---- groundedness
    if cits:
        ok = [c for c in cits if c["id"] in retrieved and _verbatim(c["quote"], retrieved[c["id"]])]
        groundedness = len(ok) / len(cits)
    else:
        groundedness = 0.0 if result.get("route") == "clear-for-filing" else None

    # ---- claims / faithfulness
    claims: list[bool] = []
    unknown_id = any(corpus.get(c["id"]) is None for c in cits)
    if cls:
        h = cls["heading"]
        hp = retrieved.get(f"HEADING-{h}", "")
        exists = corpus.get(f"HEADING-{h}") is not None
        claims.append(exists and f"HEADING-{h}" in cited_ids and f"Heading {h}" in hp
                      and (cls.get("heading_description") or "") in hp)
        g = cls.get("gri_applied")
        if g:
            claims.append(g in cited_ids and bool(retrieved.get(g)))
        for alt in cls.get("alternatives", []):
            if alt.get("status") == "excluded_by_note":
                txt = retrieved.get(alt.get("note_id") or "", "")
                chap = cls.get("chapter")
                claims.append(alt.get("note_id") in cited_ids and bool(txt) and (
                    f"Chapter {chap}" in txt or f"heading {h}" in txt))
    for alt in (result.get("classification") or {}).get("alternatives", []) if False else []:
        pass
    pol = result.get("export_policy")
    if pol:
        txt = retrieved.get(pol["passage_id"], "")
        claims.append(bool(txt) and all((p in txt) for p in pol["policies"] if p != "not stated")
                      and all(i["code"] in txt for i in pol["items"]))
    if result.get("route_basis", {}).get("code") == "tie_after_gri3":
        tied = [c["id"] for c in cits if c["id"].startswith("HEADING-")]
        claims.append(len(tied) >= 2 and all(i in retrieved for i in tied))
    faithfulness = (sum(claims) / len(claims)) if claims else None

    # ---- context relevance
    toks: set[str] = set()
    ent = extract_entities(request.get("product_description", ""), request)
    for a in ent.articles:
        toks.update(a.tokens)
    gold = set(gold_ids or [])
    rel = 0
    for pid, text in retrieved.items():
        if pid in cited_ids or pid in gold or (toks and toks & set(tokenize(text))):
            rel += 1
    context_relevance = rel / len(retrieved) if retrieved else None
    context_recall = (len(gold & set(retrieved)) / len(gold)) if gold else None

    bad_quotes = any((c["id"] in retrieved and not _verbatim(c["quote"], retrieved[c["id"]])) for c in cits)
    unresolved = any(c["id"] not in retrieved for c in cits)
    unsupported = any(not c for c in claims)
    hallucinated = bool(unknown_id or bad_quotes or unresolved or unsupported)
    return {"groundedness": groundedness, "context_relevance": context_relevance, "faithfulness": faithfulness,
            "hallucination": 1.0 if hallucinated else 0.0, "context_recall": context_recall,
            "claims": len(claims), "citations": len(cits), "retrieved": len(retrieved)}


def aggregate(rows: list[dict]) -> dict:
    out = {}
    for k in ("groundedness", "context_relevance", "faithfulness", "hallucination", "context_recall"):
        vals = [r[k] for r in rows if r.get(k) is not None]
        out[k] = round(sum(vals) / len(vals), 4) if vals else None
        out[k + "_n"] = len(vals)
    return out
