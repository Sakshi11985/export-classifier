"""D7 (rate limiting, run-cache key), reliable queue (orphan requeue), D4 (no secrets/endpoints/model ids in the repo),
D5 (spans carry run id, traceparent propagates), L5 (cost arithmetic), L2 (prompt versions)."""
import re
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
async def store(settings):
    from app.redis_util import async_redis
    from app.store import Store
    r = async_redis(settings)
    await r.flushdb()
    yield Store(r, settings)
    await r.flushdb()


async def test_rate_limit_token_bucket(store, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("RATE_LIMIT_BURST", "3")
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "60")
    store.s = get_settings()
    got = [(await store.take_token("c1"))[0] for _ in range(5)]
    assert got == [True, True, True, False, False]
    assert (await store.take_token("other-client"))[0] is True                         # per client, not global
    time.sleep(1.1)
    assert (await store.take_token("c1"))[0] is True                                   # refills


async def test_inflight_cap(store, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("MAX_INFLIGHT_PER_CLIENT", "2")
    store.s = get_settings()
    assert await store.inflight_add("c", "r1") and await store.inflight_add("c", "r2")
    assert not await store.inflight_add("c", "r3")
    await store.inflight_remove("c", "r1")
    assert await store.inflight_add("c", "r3")


async def test_orphaned_run_is_requeued_when_worker_dies(store):
    await store.enqueue("run_a")
    got = await store.claim("w-dead", 1)
    assert got == "run_a" and await store.queue_depth() == 0
    await store.heartbeat("w-dead")                                                    # alive: must not be stolen
    assert await store.requeue_orphans() == 0
    await store.r.delete("worker:w-dead")                                              # heartbeat expired
    assert await store.requeue_orphans() == 1 and await store.queue_depth() == 1
    assert await store.claim("w-new", 1) == "run_a"
    await store.ack("w-new", "run_a")
    assert await store.processing_count() == 0


async def test_run_cache_key_changes_with_index_prompt_provider(store):
    req = {"product_description": "Plastic hair comb"}
    k = store.run_cache_key(req, "idx1", "v2", "offline")
    assert k == store.run_cache_key({**req, "client_reference": "ABC"}, "idx1", "v2", "offline")
    assert len({k, store.run_cache_key(req, "idx2", "v2", "offline"), store.run_cache_key(req, "idx1", "v1", "offline"),
                store.run_cache_key(req, "idx1", "v2", "openai_compat"),
                store.run_cache_key({"product_description": "Plastic hair brush"}, "idx1", "v2", "offline")}) == 5


async def test_quarantine_and_feedback_roundtrip(store):
    qid = await store.quarantine({"product_description": ""}, [{"field": "product_description", "message": "too short"}],
                                 "schema_validation", "c", "corr1")
    q = await store.get_quarantine(qid)
    assert q["errors"][0]["field"] == "product_description" and q["agent_run_started"] is False
    await store.create_run("run_f", {"product_description": "x y z"}, "c", None)
    await store.add_feedback("run_f", "unsatisfactory", "wrong heading")
    await store.add_feedback("run_f", "satisfactory", None)
    summ = await store.feedback_summary()
    assert summ["total"] == 2 and summ["satisfactory"] == 1 and summ["unsatisfactory"] == 1


def test_cost_accounting_per_step_and_total():
    from app.agents.context import UsageTotals
    u = UsageTotals()
    u.add("classifier.plan#1", 1000, 100, 0.002, False)
    u.add("classifier.plan#2", 2000, 50, 0.003, False)
    u.add("verifier.judge#3", 500, 10, 0.0005, False)
    d = u.as_dict()
    assert d["prompt_tokens"] == 3500 and d["completion_tokens"] == 160 and d["llm_calls"] == 3
    assert abs(d["cost_usd"] - 0.0055) < 1e-9 and set(d["by_step"]) == {"classifier.plan#1", "classifier.plan#2", "verifier.judge#3"}


def test_prompts_are_versioned_with_hash():
    from app.prompts.registry import active
    a, b = active("classifier", "v1"), active("classifier", "v2")
    assert a["ref"] != b["ref"] and a["sha256"] != b["sha256"] and re.fullmatch(r"[0-9a-f]{12}", a["sha256"])


def test_spans_and_traceparent_propagation():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from app import telemetry
    from app.config import get_settings
    exp = InMemorySpanExporter()
    telemetry._configured = False
    telemetry.setup_tracing(get_settings(), "test", exporter=exp)
    with telemetry.span("api.submit", run_id="run_x"):
        tp = telemetry.current_traceparent()
    assert tp.startswith("00-")
    from opentelemetry import context as otctx
    token = otctx.attach(telemetry.context_from_traceparent(tp))
    try:
        with telemetry.span("worker.run", run_id="run_x"):
            pass
    finally:
        otctx.detach(token)
    spans = {s.name: s for s in exp.get_finished_spans()}
    assert spans["api.submit"].attributes["run_id"] == "run_x"
    assert spans["worker.run"].context.trace_id == spans["api.submit"].context.trace_id      # one trace across the queue


# ---------------------------------------------------------------- D4 configuration hygiene
TEXT_EXT = {".py", ".md", ".yml", ".yaml", ".json", ".txt", ".ini", ".sh", ".toml", ".env", ".example", ".html", ".cfg", ".jsonl"}
SKIP_DIRS = {".git", "__pycache__", "data", ".pytest_cache", "node_modules", "venv", ".venv", "corpus"}
SECRET_PATTERNS = [r"sk-[A-Za-z0-9]{20,}", r"AKIA[0-9A-Z]{16}", r"AIza[0-9A-Za-z_\-]{30,}", r"gsk_[A-Za-z0-9]{20,}",
                   r"hf_[A-Za-z0-9]{30,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"xox[baprs]-[A-Za-z0-9-]{10,}"]
MODEL_IDS = [r"gpt-[0-9]", r"claude-(?:3|4|5|opus|sonnet|haiku)", r"gemini-[0-9]", r"llama-?[0-9]", r"mistral-(?:7b|large|small)",
             r"mixtral", r"qwen[0-9]", r"gemma-?[0-9]"]
PUBLIC_ENDPOINTS = [r"api\.openai\.com", r"api\.anthropic\.com", r"generativelanguage\.googleapis\.com", r"api\.groq\.com",
                    r"openrouter\.ai", r"api\.together\.xyz", r"api\.mistral\.ai"]


def _files():
    for p in ROOT.rglob("*"):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS) and p.suffix in TEXT_EXT or p.name in {".env.example", "Dockerfile", "Makefile"}:
            if not (set(p.relative_to(ROOT).parts) & SKIP_DIRS) and p.name != "test_store_hygiene.py":
                yield p


def test_no_secrets_endpoints_or_model_ids_in_repository():
    hits = []
    pats = [re.compile(x, re.I) for x in SECRET_PATTERNS + MODEL_IDS + PUBLIC_ENDPOINTS]
    for f in _files():
        txt = f.read_text(errors="ignore")
        for rx in pats:
            m = rx.search(txt)
            if m:
                hits.append(f"{f.relative_to(ROOT)}: {m.group(0)}")
    assert not hits, "credentials/endpoints/model identifiers must be injected via env (D4):\n" + "\n".join(hits)


def test_env_example_documents_every_setting():
    from app import config
    names = set(re.findall(r'_(?:s|i|f|b)\("([A-Z_0-9]+)"', Path(config.__file__).read_text()))
    example = (ROOT / ".env.example").read_text()
    missing = [n for n in sorted(names) if not re.search(rf"^#?\s*{n}=", example, re.M)]
    assert not missing, f".env.example lacks: {missing}"
    assert not re.search(r"(?i)^(LLM_API_KEY|LLM_FALLBACK_API_KEY)=\S+", example, re.M)    # no real key committed
