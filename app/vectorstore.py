"""Chroma index through LangChain (L1). Supports adding / re-embedding individual
chunks in place (upsert) without rebuilding the collection."""
from __future__ import annotations

import json
from typing import Iterable

import chromadb
from langchain_chroma import Chroma
from langchain_core.documents import Document

from .config import Settings
from .corpus.parser import Chunk
from .embeddings import LCEmbeddings


def make_chroma_client(s: Settings):
    if s.chroma_mode == "http":
        if not s.chroma_host:
            raise RuntimeError("CHROMA_HOST is not set (see .env.example)")
        return chromadb.HttpClient(host=s.chroma_host, port=s.chroma_port)
    if s.chroma_mode == "persistent":
        return chromadb.PersistentClient(path=s.chroma_persist_dir)
    if s.chroma_mode == "ephemeral":
        return chromadb.EphemeralClient()
    raise ValueError(f"unknown CHROMA_MODE {s.chroma_mode!r}")


def embed_text(c: Chunk) -> str:
    """Text that is embedded for a chunk. The descriptive head comes first so that models with a
    short input window still see what the chunk is about."""
    title = c.metadata.get("title", c.id)
    if c.type == "heading":
        body = c.metadata.get("searchable") or c.text
        return f"{title}. {body}"[:1800]
    return f"{title}. {c.text}"[:1800]


class TariffIndex:
    def __init__(self, s: Settings, embedder, client=None):
        self.s = s
        self.embedder = embedder
        self.client = client or make_chroma_client(s)
        self.vs = Chroma(
            client=self.client,
            collection_name=s.chroma_collection,
            embedding_function=LCEmbeddings(embedder),
            collection_metadata={"hnsw:space": "cosine"},
        )

    @property
    def collection(self):
        return self.vs._collection

    # ------------------------------------------------------------------ write
    def existing_signatures(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        got = self.collection.get(include=["metadatas"])
        for i, m in zip(got["ids"], got["metadatas"]):
            out[i] = m or {}
        return out

    def upsert(self, chunks: list[Chunk], embed_sig: str) -> None:
        if not chunks:
            return
        texts = [embed_text(c) for c in chunks]
        metas = []
        for c in chunks:
            flat = {"id": c.id, "type": c.type, "chapter": int(c.metadata.get("chapter") or -1),
                    "heading": str(c.metadata.get("heading") or ""), "content_hash": c.content_hash,
                    "embed_sig": embed_sig, "title": str(c.metadata.get("title", c.id)),
                    "meta_json": json.dumps(c.metadata, default=list), "text": c.text}
            metas.append(flat)
        self.vs.add_texts(texts=texts, metadatas=metas, ids=[c.id for c in chunks])

    def delete(self, ids: Iterable[str]) -> None:
        ids = list(ids)
        if ids:
            self.collection.delete(ids=ids)

    # ------------------------------------------------------------------ read
    def all_chunks(self) -> list[Chunk]:
        got = self.collection.get(include=["metadatas"])
        out = []
        for i, m in zip(got["ids"], got["metadatas"]):
            meta = json.loads(m.get("meta_json", "{}"))
            out.append(Chunk(i, m["type"], m["text"], meta))
        return out

    def vector_search(self, query: str, k: int, where: dict | None = None) -> list[tuple[str, float]]:
        kwargs = {"k": k}
        if where:
            kwargs["filter"] = where
        res = self.vs.similarity_search_with_score(query, **kwargs)
        # Chroma cosine distance -> similarity
        return [(d.metadata["id"], 1.0 - float(dist)) for d, dist in res]

    def count(self) -> int:
        return self.collection.count()
