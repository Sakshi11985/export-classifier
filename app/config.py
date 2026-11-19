"""Runtime configuration. Every environment-specific value comes from environment
variables (D4). There are deliberately no endpoints, keys or model identifiers
hard-coded in this repository; see .env.example for the documented interface."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _s(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _i(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


def _f(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v in (None, ""):
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    service_name: str = field(default_factory=lambda: _s("SERVICE_NAME", "app"))
    log_level: str = field(default_factory=lambda: _s("LOG_LEVEL", "INFO"))

    # infrastructure
    redis_url: str = field(default_factory=lambda: _s("REDIS_URL"))
    chroma_mode: str = field(default_factory=lambda: _s("CHROMA_MODE", "http"))  # http|persistent|ephemeral
    chroma_host: str = field(default_factory=lambda: _s("CHROMA_HOST"))
    chroma_port: int = field(default_factory=lambda: _i("CHROMA_PORT", 8000))
    chroma_persist_dir: str = field(default_factory=lambda: _s("CHROMA_PERSIST_DIR", "./data/chroma"))
    chroma_collection: str = field(default_factory=lambda: _s("CHROMA_COLLECTION", "tariff"))

    # corpus / embeddings
    corpus_dir: str = field(default_factory=lambda: _s("CORPUS_DIR", "./corpus"))
    embedding_backend: str = field(default_factory=lambda: _s("EMBEDDING_BACKEND", "sentence-transformers"))
    embedding_model: str = field(default_factory=lambda: _s("EMBEDDING_MODEL"))
    embedding_dim_hashing: int = field(default_factory=lambda: _i("HASHING_EMBEDDING_DIM", 512))

    # LLM
    llm_provider: str = field(default_factory=lambda: _s("LLM_PROVIDER", "offline"))  # offline|openai_compat
    llm_base_url: str = field(default_factory=lambda: _s("LLM_BASE_URL"))
    llm_api_key: str = field(default_factory=lambda: _s("LLM_API_KEY"))
    llm_model_classifier: str = field(default_factory=lambda: _s("LLM_MODEL_CLASSIFIER"))
    llm_model_verifier: str = field(default_factory=lambda: _s("LLM_MODEL_VERIFIER"))
    llm_fallback_base_url: str = field(default_factory=lambda: _s("LLM_FALLBACK_BASE_URL"))
    llm_fallback_api_key: str = field(default_factory=lambda: _s("LLM_FALLBACK_API_KEY"))
    llm_fallback_model: str = field(default_factory=lambda: _s("LLM_FALLBACK_MODEL"))
    llm_timeout_s: float = field(default_factory=lambda: _f("LLM_TIMEOUT_S", 20.0))
    llm_max_retries: int = field(default_factory=lambda: _i("LLM_MAX_RETRIES", 2))
    llm_backoff_base_s: float = field(default_factory=lambda: _f("LLM_BACKOFF_BASE_S", 0.5))
    llm_backoff_max_s: float = field(default_factory=lambda: _f("LLM_BACKOFF_MAX_S", 8.0))
    cb_failure_threshold: int = field(default_factory=lambda: _i("CB_FAILURE_THRESHOLD", 5))
    cb_window_s: int = field(default_factory=lambda: _i("CB_WINDOW_S", 30))
    cb_recovery_s: int = field(default_factory=lambda: _i("CB_RECOVERY_S", 20))
    price_prompt_per_1k: float = field(default_factory=lambda: _f("LLM_PRICE_PROMPT_PER_1K", 0.0))
    price_completion_per_1k: float = field(default_factory=lambda: _f("LLM_PRICE_COMPLETION_PER_1K", 0.0))

    # agent bounds (A1)
    agent_max_steps: int = field(default_factory=lambda: _i("AGENT_MAX_STEPS", 20))
    agent_max_tool_calls: int = field(default_factory=lambda: _i("AGENT_MAX_TOOL_CALLS", 30))
    agent_run_timeout_s: float = field(default_factory=lambda: _f("AGENT_RUN_TIMEOUT_S", 90.0))
    tool_timeout_s: float = field(default_factory=lambda: _f("TOOL_TIMEOUT_S", 10.0))
    max_verify_attempts: int = field(default_factory=lambda: _i("MAX_VERIFY_ATTEMPTS", 2))
    first_strategy: str = field(default_factory=lambda: _s("AGENT_FIRST_STRATEGY", "default"))
    prompt_variant: str = field(default_factory=lambda: _s("PROMPT_VARIANT", "v2"))
    enable_fault_injection: bool = field(default_factory=lambda: _b("ENABLE_FAULT_INJECTION", False))

    # API / worker
    worker_concurrency: int = field(default_factory=lambda: _i("WORKER_CONCURRENCY", 4))
    worker_metrics_port: int = field(default_factory=lambda: _i("WORKER_METRICS_PORT", 9101))
    worker_heartbeat_ttl_s: int = field(default_factory=lambda: _i("WORKER_HEARTBEAT_TTL_S", 15))
    run_ttl_s: int = field(default_factory=lambda: _i("RUN_TTL_S", 7 * 24 * 3600))
    poll_after_ms: int = field(default_factory=lambda: _i("POLL_AFTER_MS", 700))

    # caching + rate limiting (D7)
    run_cache_enabled: bool = field(default_factory=lambda: _b("RUN_CACHE_ENABLED", True))
    retrieval_cache_enabled: bool = field(default_factory=lambda: _b("RETRIEVAL_CACHE_ENABLED", True))
    embedding_cache_enabled: bool = field(default_factory=lambda: _b("EMBEDDING_CACHE_ENABLED", True))
    cache_ttl_s: int = field(default_factory=lambda: _i("CACHE_TTL_S", 24 * 3600))
    rate_limit_per_min: int = field(default_factory=lambda: _i("RATE_LIMIT_PER_MIN", 30))
    rate_limit_burst: int = field(default_factory=lambda: _i("RATE_LIMIT_BURST", 10))
    max_inflight_per_client: int = field(default_factory=lambda: _i("MAX_INFLIGHT_PER_CLIENT", 20))
    max_queue_depth: int = field(default_factory=lambda: _i("MAX_QUEUE_DEPTH", 500))

    # observability
    otel_enabled: bool = field(default_factory=lambda: _b("OTEL_ENABLED", False))
    otel_endpoint: str = field(default_factory=lambda: _s("OTEL_EXPORTER_OTLP_ENDPOINT"))
    live_score_sample_rate: float = field(default_factory=lambda: _f("LIVE_SCORE_SAMPLE_RATE", 0.2))
    slo_p95_seconds: float = field(default_factory=lambda: _f("SLO_P95_SECONDS", 20.0))


def get_settings() -> Settings:
    """Read the environment on every call so tests can monkeypatch env vars."""
    return Settings()
