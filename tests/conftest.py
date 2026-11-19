"""Shared fixtures. Unit/agent tests use the real corpus PDFs, a real Chroma store (persistent mode in a tmp dir),
a real redis-server on a private port and the deterministic offline reasoning policy with hashing embeddings.
Integration tests additionally start the stub LLM, the API and a worker as real processes."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def redis_server(tmp_path_factory):
    exe = shutil.which("redis-server")
    if not exe:
        pytest.skip("redis-server is not installed (sudo apt install redis-server)")
    port = free_port()
    d = tmp_path_factory.mktemp("redis")
    p = subprocess.Popen([exe, "--port", str(port), "--save", "", "--appendonly", "no", "--dir", str(d)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import redis
    for _ in range(50):
        try:
            redis.Redis(port=port).ping()
            break
        except Exception:
            time.sleep(0.1)
    else:
        p.kill()
        pytest.fail("redis-server did not start")
    yield f"redis://127.0.0.1:{port}/0"
    p.terminate()
    p.wait(5)


@pytest.fixture(scope="session")
def base_env(redis_server, tmp_path_factory):
    chroma_dir = tmp_path_factory.mktemp("chroma")
    return {
        "REDIS_URL": redis_server, "CHROMA_MODE": "persistent", "CHROMA_PERSIST_DIR": str(chroma_dir / "chroma"),
        "EMBEDDING_BACKEND": "hashing", "CORPUS_DIR": str(ROOT / "corpus"), "LLM_PROVIDER": "offline",
        "ENABLE_FAULT_INJECTION": "true", "LOG_LEVEL": "WARNING", "RUN_CACHE_ENABLED": "false",
        "RETRIEVAL_CACHE_ENABLED": "true", "OTEL_ENABLED": "false",
    }


@pytest.fixture(scope="session", autouse=True)
def _env(base_env):
    old = {k: os.environ.get(k) for k in base_env}
    os.environ.update(base_env)
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(scope="session")
def settings(_env):
    from app.config import get_settings
    return get_settings()


@pytest.fixture(scope="session")
def ingested(settings):
    from app.corpus.ingest import run_ingest
    return run_ingest(settings)


@pytest.fixture(scope="session")
def corpus(settings, ingested):
    from app import metrics as prom
    from app.embeddings import build_embedder
    from app.redis_util import sync_redis
    from app.retrieval import Corpus
    from app.vectorstore import TariffIndex
    r = sync_redis(settings)
    return Corpus(TariffIndex(settings, build_embedder(settings, r, prom)), settings, r).load()


@pytest.fixture()
def make_ctx(corpus, settings):
    from app.agents.context import RunContext, Trace
    from app.llm.client import LLMClient

    def _make(request=None, s=None, llm=None, budget_s=30.0):
        return RunContext(run_id="run_test", request=request or {"product_description": "Stainless steel vacuum flask"},
                          corpus=corpus, settings=s or settings, llm=llm or LLMClient(s or settings), trace=Trace("run_test"),
                          deadline=time.monotonic() + budget_s)
    return _make


class Stack:
    def __init__(self):
        self.procs: list[subprocess.Popen] = []
        self.logs: list[Path] = []

    def spawn(self, name, args, env, tmp):
        log = tmp / f"{name}.log"
        f = open(log, "w")
        p = subprocess.Popen([sys.executable, *args], cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT), **env},
                             stdout=f, stderr=subprocess.STDOUT)
        self.procs.append(p)
        self.logs.append(log)
        return p

    def stop(self):
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            try:
                p.wait(8)
            except Exception:
                p.kill()


@pytest.fixture(scope="module")
def stack(base_env, tmp_path_factory, ingested):
    """Real stub LLM + API + 1 worker. Uses the stub as an OpenAI-compatible provider so the LLM client,
    breaker and verifier-judge are exercised for real."""
    import httpx
    tmp = tmp_path_factory.mktemp("stack")
    st = Stack()
    stub_port, api_port, wport, chroma_port = free_port(), free_port(), free_port(), free_port()
    chroma_bin = shutil.which("chroma") or str(Path(sys.executable).parent / "chroma")
    cp = subprocess.Popen([chroma_bin, "run", "--path", str(tmp / "chromadb"), "--port", str(chroma_port)],
                          stdout=open(tmp / "chroma.log", "w"), stderr=subprocess.STDOUT)
    st.procs.append(cp)
    for _ in range(80):
        try:
            if httpx.get(f"http://127.0.0.1:{chroma_port}/api/v2/heartbeat", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.25)
    env = dict(base_env)
    env.update({"CHROMA_MODE": "http", "CHROMA_HOST": "127.0.0.1", "CHROMA_PORT": str(chroma_port),
                "CHROMA_PERSIST_DIR": str(tmp / "meta" / "chroma"),"LLM_PROVIDER": "openai_compat", "LLM_BASE_URL": f"http://127.0.0.1:{stub_port}/v1", "LLM_API_KEY": "test-key",
                "LLM_MODEL_CLASSIFIER": "test-model", "LLM_MODEL_VERIFIER": "test-model", "LLM_BACKOFF_BASE_S": "0.05",
                "LLM_BACKOFF_MAX_S": "0.2", "CB_FAILURE_THRESHOLD": "3", "CB_RECOVERY_S": "4", "LLM_TIMEOUT_S": "5",
                "RATE_LIMIT_PER_MIN": "6", "RATE_LIMIT_BURST": "6", "WORKER_METRICS_PORT": str(wport),
                "LLM_PRICE_PROMPT_PER_1K": "0.001", "LLM_PRICE_COMPLETION_PER_1K": "0.002", "LIVE_SCORE_SAMPLE_RATE": "1.0",
                "RUN_CACHE_ENABLED": "true", "STUB_PORT": str(stub_port), "STUB_DELAY_S": "0.05"})
    ing = subprocess.run([sys.executable, "-m", "app.corpus.ingest"], cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT), **env},
                         capture_output=True, text=True, timeout=300)
    if ing.returncode != 0:
        st.stop()
        pytest.fail("ingest into the stack's chroma server failed:\n" + ing.stdout[-1500:] + ing.stderr[-1500:])
    st.spawn("stub", ["-m", "stub.llm_stub"], env, tmp)
    st.spawn("api", ["-m", "uvicorn", "app.api:get_app", "--factory", "--port", str(api_port), "--log-level", "warning"], env, tmp)
    st.spawn("worker", ["-m", "app.worker"], env, tmp)
    api = f"http://127.0.0.1:{api_port}"
    stub = f"http://127.0.0.1:{stub_port}"
    ok = False
    for _ in range(120):
        try:
            if httpx.get(f"{api}/health/ready", timeout=2).status_code == 200 and httpx.get(f"{stub}/health", timeout=2).status_code == 200:
                ok = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    if not ok:
        logs = "\n".join(f"--- {l.name}\n{l.read_text()[-1500:]}" for l in st.logs)
        st.stop()
        pytest.fail("stack did not become ready\n" + logs)
    yield {"api": api, "stub": stub, "worker_metrics": f"http://127.0.0.1:{wport}", "env": env}
    st.stop()
