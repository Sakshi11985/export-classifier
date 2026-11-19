"""Idempotent ingestion pipeline (D2): read -> parse -> chunk -> embed -> index.

    python -m app.corpus.ingest                 # incremental: only new/changed chunks are embedded
    python -m app.corpus.ingest --reembed       # re-embed every chunk in place (e.g. after changing the model)
    python -m app.corpus.ingest --reembed-ids HEADING-9617,GRI-1
    python -m app.corpus.ingest --dry-run       # parse and report, touch nothing

Running it twice in a row embeds nothing the second time. Freshness (build time, document
versions = sha256, chunk count, throughput) is stored in Redis (key index:meta) and in
<CHROMA_PERSIST_DIR>/../index_meta.json, and exposed as Prometheus gauges by the API."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from .. import metrics
from ..config import Settings, get_settings
from ..embeddings import build_embedder
from ..logging_setup import get_logger, setup_logging
from ..redis_util import sync_redis
from ..vectorstore import TariffIndex
from .parser import parse_corpus

log = get_logger("ingest")


def index_version(chunks, embed_sig: str) -> str:
    h = hashlib.sha256(embed_sig.encode())
    for c in sorted(chunks, key=lambda c: c.id):
        h.update(f"{c.id}:{c.content_hash}".encode())
    return h.hexdigest()[:12]


def run_ingest(s: Settings, reembed: bool = False, reembed_ids: set[str] | None = None,
               dry_run: bool = False, index: TariffIndex | None = None, redis_client=None) -> dict:
    t0 = time.time()
    chunks, manifest = parse_corpus(Path(s.corpus_dir))
    log.info("parsed corpus", extra={"chunks": len(chunks), "documents": len(manifest)})
    if dry_run:
        return {"dry_run": True, "chunk_count": len(chunks), "documents": manifest,
                "by_type": _count_types(chunks)}
    r = redis_client if redis_client is not None else sync_redis(s)
    embedder = build_embedder(s, r, metrics)
    embed_sig = f"{embedder.name}:{embedder.version}"
    idx = index or TariffIndex(s, embedder)
    existing = idx.existing_signatures()
    wanted = {c.id: c for c in chunks}
    todo = []
    for c in chunks:
        old = existing.get(c.id)
        if (reembed or (reembed_ids and c.id in reembed_ids) or old is None
                or old.get("content_hash") != c.content_hash or old.get("embed_sig") != embed_sig):
            todo.append(c)
    stale = [i for i in existing if i not in wanted]
    t_embed = time.time()
    batch = 32
    for i in range(0, len(todo), batch):
        idx.upsert(todo[i:i + batch], embed_sig)
    embed_s = time.time() - t_embed
    idx.delete(stale)
    dur = time.time() - t0
    meta = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "built_at_ts": time.time(),
        "chunk_count": len(chunks),
        "indexed_count": idx.count(),
        "embedded_this_run": len(todo),
        "skipped_this_run": len(chunks) - len(todo),
        "deleted_this_run": len(stale),
        "embedding": embed_sig,
        "index_version": index_version(chunks, embed_sig),
        "documents": manifest,
        "by_type": _count_types(chunks),
        "pipeline_duration_s": round(dur, 3),
        "embed_duration_s": round(embed_s, 3),
        "chunks_per_second": round(len(todo) / embed_s, 2) if todo and embed_s > 0 else 0.0,
        "forced_reembed": bool(reembed or reembed_ids),
    }
    payload = json.dumps(meta)
    if r is not None:
        r.set("index:meta", payload)
    try:
        p = Path(s.chroma_persist_dir).parent / "index_meta.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(payload)
    except OSError:
        pass
    metrics.update_index_gauges(meta)
    metrics.ingest_runs.labels("ok").inc()
    log.info("ingest complete", extra={k: meta[k] for k in (
        "chunk_count", "embedded_this_run", "skipped_this_run", "deleted_this_run", "index_version",
        "pipeline_duration_s", "chunks_per_second")})
    return meta


def _count_types(chunks) -> dict:
    out: dict[str, int] = {}
    for c in chunks:
        out[c.type] = out.get(c.type, 0) + 1
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ingest the tariff corpus into Chroma")
    ap.add_argument("--reembed", action="store_true", help="re-embed all chunks in place")
    ap.add_argument("--reembed-ids", default="", help="comma separated chunk ids to re-embed")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    s = get_settings()
    setup_logging("ingest", s.log_level)
    try:
        meta = run_ingest(s, a.reembed, set(filter(None, a.reembed_ids.split(","))), a.dry_run)
    except Exception as e:  # noqa: BLE001
        metrics.ingest_runs.labels("error").inc()
        log.exception("ingest failed")
        print(f"INGEST FAILED: {e}", file=sys.stderr)
        return 1
    print(json.dumps({k: v for k, v in meta.items() if k != "documents"}, indent=2))
    for d in meta["documents"]:
        print(f"  {d['file']}: {d['pages']} pages, text layer={d['has_text_layer']}, "
              f"{d['chunks']} chunks, sha256={d['sha256'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
