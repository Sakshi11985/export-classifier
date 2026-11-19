"""Retrieval over the indexed corpus (F4, L1).

Two retrieval paths, both exposed to the agent as tools:

* hybrid search: Chroma vector search + BM25 over the same chunks, merged by reciprocal-rank fusion.
* article-name search: scores the *descriptions of headings and tariff items* against the article
  named in the request. This addresses the difficulty in spec section 3: a material-based search
  lands in the steel chapter, while the heading that names the article is found by its name."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass

from . import metrics
from .config import Settings
from .corpus.parser import Chunk
from .textutil import BM25, rrf, squash, stem, tokenize
from .vectorstore import TariffIndex, embed_text

SNIPPET = 420
_SEG_SPLIT = re.compile(r"[;,:()\[\]]|\band\b|\bor\b|\bof\b|\bfor\b|\bwith\b|\bwhether\b|\bsuch\b|\bincluding\b|--+|\s-\s")


def segment_heads(desc: str) -> set[str]:
    """Head nouns of a tariff description: the last content word of each phrase. 'nail brushes' has the
    head 'brush', so it does not name the article 'nail'."""
    heads = set()
    for seg in _SEG_SPLIT.split(desc.lower()):
        toks = tokenize(seg)
        if toks:
            heads.add(toks[-1])
    return heads


@dataclass
class Hit:
    id: str
    type: str
    title: str
    score: float
    vector_score: float | None
    lexical_score: float | None
    chapter: int | None
    text: str

    def as_dict(self, full: bool = False, limit: int = 1500) -> dict:
        body = self.text if full else self.text[:SNIPPET]
        return {"id": self.id, "type": self.type, "title": self.title, "chapter": self.chapter,
                "score": round(self.score, 4), "vector_score": _r(self.vector_score),
                "lexical_score": _r(self.lexical_score), "text": body[:limit]}


def _r(x):
    return None if x is None else round(x, 4)


class Corpus:
    def __init__(self, index: TariffIndex, s: Settings, redis_client=None):
        self.index = index
        self.s = s
        self.r = redis_client
        self.chunks: dict[str, Chunk] = {}
        self.version = "none"
        self.meta: dict = {}
        self._bm25: BM25 | None = None
        self._heading_docs: dict[str, dict] = {}
        self._idf: dict[str, float] = {}
        self._loaded_at = 0.0

    # ------------------------------------------------------------------ loading
    def load(self) -> "Corpus":
        chunks = self.index.all_chunks()
        if not chunks:
            raise RuntimeError("the index is empty - run the ingestion pipeline first (python -m app.corpus.ingest)")
        self.chunks = {c.id: c for c in chunks}
        self._bm25 = BM25({c.id: tokenize(embed_text(c)) for c in chunks})
        self._build_heading_docs()
        self.meta = self._read_meta()
        self.version = self.meta.get("index_version", "unknown")
        self._loaded_at = time.time()
        return self

    def _read_meta(self) -> dict:
        if self.r is None:
            return {}
        try:
            raw = self.r.get("index:meta")
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def refresh_if_stale(self, every_s: float = 30.0) -> None:
        if time.time() - self._loaded_at < every_s:
            return
        meta = self._read_meta()
        if meta and meta.get("index_version") != self.version:
            self.load()
        else:
            self._loaded_at = time.time()

    def _build_heading_docs(self) -> None:
        docs = {}
        df: dict[str, int] = {}
        for c in self.chunks.values():
            if c.type != "heading":
                continue
            desc_toks = set(tokenize(c.metadata.get("desc", "")))
            desc_heads = segment_heads(c.metadata.get("desc", ""))
            items = []
            for line in c.text.splitlines():
                if line.startswith("- "):
                    parts = line[2:].split(" ", 2)
                    code = parts[0]
                    desc = parts[2] if len(parts) > 2 else ""
                    items.append({"code": code, "desc": desc, "tokens": set(tokenize(desc)), "heads": segment_heads(desc)})
            alltok = set(desc_toks)
            for it in items:
                alltok |= it["tokens"]
            for t in alltok:
                df[t] = df.get(t, 0) + 1
            docs[c.metadata["heading"]] = {"desc": c.metadata.get("desc", ""), "desc_tokens": desc_toks, "desc_heads": desc_heads,
                                           "items": items, "chunk": c.id, "chapter": c.metadata.get("chapter")}
        n = max(1, len(docs))
        self._idf = {t: math.log(1 + n / d) for t, d in df.items()}
        self._heading_docs = docs

    # ------------------------------------------------------------------ accessors
    def get(self, cid: str) -> Chunk | None:
        return self.chunks.get(cid)

    def headings(self) -> list[Chunk]:
        return [c for c in self.chunks.values() if c.type == "heading"]

    def linked_notes(self, heading: str) -> list[Chunk]:
        h = self.chunks.get(f"HEADING-{heading}")
        if not h:
            return []
        return [self.chunks[i] for i in h.metadata.get("linked_notes", []) if i in self.chunks]

    # ------------------------------------------------------------------ search
    def _cache_key(self, query, k, types, chapter, vector_only=False) -> str:
        raw = json.dumps([self.version, squash(query), k, sorted(types or []), chapter, vector_only])
        return "ret:" + hashlib.sha256(raw.encode()).hexdigest()

    def search(self, query: str, k: int = 5, types: list[str] | None = None,
               chapter: int | None = None, vector_only: bool = False) -> tuple[list[Hit], bool]:
        """Returns (hits, cache_hit)."""
        key = self._cache_key(query, k, types, chapter, vector_only)
        if self.r is not None and self.s.retrieval_cache_enabled:
            try:
                cached = self.r.get(key)
            except Exception:
                cached = None
            if cached:
                metrics.cache_hits.labels("retrieval").inc()
                return [Hit(**h) for h in json.loads(cached)], True
            metrics.cache_misses.labels("retrieval").inc()
        hits = self._search_uncached(query, k, types, chapter, vector_only)
        if self.r is not None and self.s.retrieval_cache_enabled:
            try:
                self.r.set(key, json.dumps([h.__dict__ for h in hits]), ex=self.s.cache_ttl_s)
            except Exception:
                pass
        return hits, False

    def _search_uncached(self, query, k, types, chapter, vector_only=False) -> list[Hit]:
        allowed = {cid for cid, c in self.chunks.items()
                   if (not types or c.type in types) and (chapter is None or c.metadata.get("chapter") in (chapter, None))}
        pool = max(k * 3, 12)
        where = None
        if types and len(types) == 1:
            where = {"type": types[0]}
        vec = [(i, sc) for i, sc in self.index.vector_search(query, pool, where) if i in allowed]
        lex_scores = {} if vector_only else {i: sc for i, sc in self._bm25.score(tokenize(query)).items() if i in allowed}
        lex = sorted(lex_scores.items(), key=lambda x: -x[1])[:pool]
        fused = rrf([[i for i, _ in vec], [i for i, _ in lex]])
        vmap, lmap = dict(vec), dict(lex)
        top = sorted(fused.items(), key=lambda x: -x[1])[:k]
        out = []
        for cid, sc in top:
            c = self.chunks[cid]
            out.append(Hit(cid, c.type, c.metadata.get("title", cid), sc, vmap.get(cid), lmap.get(cid),
                           c.metadata.get("chapter"), c.text))
        return out

    # ------------------------------------------------------------------ article-name search
    def headings_for_article(self, tokens: list[str], phrase: str | None = None, top: int = 6) -> list[dict]:
        """Find headings/items whose *description* names the article. `tokens` are stemmed content
        tokens of the article phrase (materials already removed); the last token is the head noun."""
        toks = [t for t in dict.fromkeys(tokens) if t]
        if not toks:
            return []
        head = toks[-1]
        denom = sum(self._idf.get(t, 1.0) for t in toks) or 1.0
        out = []
        for h, d in self._heading_docs.items():
            best_item, best_item_cov, best_item_tok = None, 0.0, set()
            for it in d["items"]:
                m = {t for t in toks if t in it["tokens"]}
                cov = sum(self._idf.get(t, 1.0) for t in m) / denom
                if cov > best_item_cov:
                    best_item, best_item_cov, best_item_tok = it, cov, m
            hm = {t for t in toks if t in d["desc_tokens"]}
            matched = hm | best_item_tok
            if not matched:
                continue
            cov = sum(self._idf.get(t, 1.0) for t in matched) / denom
            head_hit = (head in hm and head in d["desc_heads"]) or (
                best_item is not None and head in best_item_tok and head in best_item["heads"])
            score = cov + (0.25 if head_hit else 0.0)
            where_hm = "heading" if hm >= best_item_tok else f"item {best_item['code']}"
            evidence = d["desc"] if where_hm == "heading" or not best_item else best_item["desc"]
            strong = head_hit and (cov >= 0.999 or cov >= 0.5)
            out.append({"heading": h, "chapter": d["chapter"], "score": round(score, 4),
                        "coverage": round(cov, 4), "head_noun_matched": head_hit, "strong": strong,
                        "matched_tokens": sorted(matched), "matched_in": where_hm,
                        "evidence": evidence, "chunk_id": d["chunk"], "heading_description": d["desc"]})
        out.sort(key=lambda x: (-x["score"], x["heading"]))
        return out[:top]
