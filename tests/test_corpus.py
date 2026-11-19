"""D2 / L1: parsing, chunking, idempotent ingestion, re-embedding without a rebuild, freshness metadata."""
import json
from pathlib import Path

from app.corpus.ingest import run_ingest
from app.corpus.parser import parse_corpus

CORPUS = Path(__file__).resolve().parent.parent / "corpus"


def test_parse_counts_and_ids():
    chunks, manifest = parse_corpus(CORPUS)
    ids = {c.id for c in chunks}
    assert len(chunks) == len(ids) == 122
    by = {}
    for c in chunks:
        by[c.type] = by.get(c.type, 0) + 1
    assert by["heading"] == 46 and by["policy"] == 46 and by["note"] == 15 and by["gri"] == 10
    for must in ("GRI-1", "GRI-3(a)", "SEC-XV-NOTE-1", "HEADING-9617", "POLICY-9617", "HEADING-7323", "CH96-NOTE-1"):
        assert must in ids
    assert len(manifest) == 3 and all(m["sha256"] for m in manifest)


def test_heading_9617_names_vacuum_flasks_and_note_excludes_chapter_96():
    chunks, _ = parse_corpus(CORPUS)
    d = {c.id: c for c in chunks}
    assert "vacuum flask" in d["HEADING-9617"].text.lower()
    assert "Chapter 96" in d["SEC-XV-NOTE-1"].text
    assert d["HEADING-9617"].metadata["chapter"] == 96


def test_ingest_is_idempotent_and_reembed_is_partial(settings, ingested):
    again = run_ingest(settings)
    assert again["embedded_this_run"] == 0 and again["skipped_this_run"] == 122
    assert again["indexed_count"] == 122 and again["index_version"] == ingested["index_version"]
    part = run_ingest(settings, reembed_ids={"HEADING-9617", "GRI-1"})
    assert part["embedded_this_run"] == 2 and part["skipped_this_run"] == 120
    full = run_ingest(settings, reembed=True)
    assert full["embedded_this_run"] == 122


def test_freshness_metadata_is_recorded(settings, ingested, redis_server):
    import redis
    meta = json.loads(redis.Redis.from_url(redis_server).get("index:meta"))
    for k in ("built_at", "chunk_count", "index_version", "documents", "embedding", "chunks_per_second", "pipeline_duration_s"):
        assert k in meta
    assert meta["chunk_count"] == 122 and len(meta["documents"]) == 3


def test_dry_run_does_not_touch_index(settings):
    out = run_ingest(settings, dry_run=True)
    assert out["dry_run"] and out["chunk_count"] == 122
