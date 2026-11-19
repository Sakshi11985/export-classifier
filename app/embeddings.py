"""Embedding backends (L1/D7).

* sentence-transformers  - runs locally, free; the model name comes from EMBEDDING_MODEL.
* hashing                - deterministic signed feature-hashing of word uni/bi-grams. Needs no
                           download, so tests and offline demos work anywhere. It is a lexical
                           embedding, not a semantic one; the README states this limitation.

Embeddings are cached in Redis keyed by (backend, model, text) when a Redis client is supplied."""
from __future__ import annotations

import hashlib
import json
import math
import threading
from typing import Protocol

from .config import Settings
from .textutil import tokenize


class Embedder(Protocol):
    name: str
    version: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    version = "1"

    def __init__(self, dim: int = 512):
        self.dim = dim
        self.name = f"hashing-{dim}"

    def _vec(self, text: str) -> list[float]:
        toks = tokenize(text)
        feats: list[tuple[str, float]] = [(t, 1.0) for t in toks]
        feats += [(a + "_" + b, 0.7) for a, b in zip(toks, toks[1:])]
        v = [0.0] * self.dim
        for f, w in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
            idx = h % self.dim
            sign = 1.0 if (h >> 63) & 1 else -1.0
            v[idx] += sign * w
        # sublinear tf, L2 normalise
        v = [math.copysign(math.log1p(abs(x)), x) for x in v]
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class SentenceTransformerEmbedder:
    version = "1"

    def __init__(self, model_name: str, device: str | None = None):
        if not model_name:
            raise RuntimeError(
                "EMBEDDING_MODEL is not set. Set it in .env (see .env.example) or use EMBEDDING_BACKEND=hashing.")
        self.name = f"st:{model_name}"
        self._model_name = model_name
        self._device = device
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                from sentence_transformers import SentenceTransformer  # heavy import, lazy

                self._model = SentenceTransformer(self._model_name, device=self._device)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        m = self._load()
        arr = m.encode(texts, normalize_embeddings=True, show_progress_bar=False, batch_size=32)
        return [list(map(float, r)) for r in arr]


class CachedEmbedder:
    """Wraps an embedder with a Redis cache (sync client; safe to call from threads)."""

    def __init__(self, inner: Embedder, redis_client=None, ttl_s: int = 7 * 24 * 3600, metrics=None):
        self.inner = inner
        self.name = inner.name
        self.version = inner.version
        self._r = redis_client
        self._ttl = ttl_s
        self._metrics = metrics
        self.hits = 0
        self.misses = 0

    def _key(self, text: str) -> str:
        return "emb:" + hashlib.sha256(f"{self.name}|{self.version}|{text}".encode()).hexdigest()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._r is None:
            return self.inner.embed(texts)
        out: list[list[float] | None] = [None] * len(texts)
        todo: list[int] = []
        try:
            cached = self._r.mget([self._key(t) for t in texts])
        except Exception:
            cached = [None] * len(texts)
        for i, c in enumerate(cached):
            if c:
                out[i] = json.loads(c)
                self.hits += 1
                if self._metrics:
                    self._metrics.cache_hits.labels("embedding").inc()
            else:
                todo.append(i)
                self.misses += 1
                if self._metrics:
                    self._metrics.cache_misses.labels("embedding").inc()
        if todo:
            vecs = self.inner.embed([texts[i] for i in todo])
            pipe = None
            try:
                pipe = self._r.pipeline()
            except Exception:
                pass
            for i, v in zip(todo, vecs):
                out[i] = v
                if pipe is not None:
                    pipe.set(self._key(texts[i]), json.dumps([round(x, 6) for x in v]), ex=self._ttl)
            if pipe is not None:
                try:
                    pipe.execute()
                except Exception:
                    pass
        return out  # type: ignore[return-value]


def build_embedder(s: Settings, redis_client=None, metrics=None) -> CachedEmbedder:
    if s.embedding_backend == "hashing":
        inner: Embedder = HashingEmbedder(s.embedding_dim_hashing)
    elif s.embedding_backend == "sentence-transformers":
        inner = SentenceTransformerEmbedder(s.embedding_model)
    else:
        raise ValueError(f"unknown EMBEDDING_BACKEND {s.embedding_backend!r}")
    return CachedEmbedder(inner, redis_client if s.embedding_cache_enabled else None, metrics=metrics)


try:  # LangChain adapter (L1: orchestrate retrieval with LangChain)
    from langchain_core.embeddings import Embeddings

    class LCEmbeddings(Embeddings):
        def __init__(self, embedder: Embedder):
            self.embedder = embedder

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return self.embedder.embed(list(texts))

        def embed_query(self, text: str) -> list[float]:
            return self.embedder.embed([text])[0]
except ImportError:  # pragma: no cover
    LCEmbeddings = None  # type: ignore
