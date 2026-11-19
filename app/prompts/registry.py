"""Prompts as versioned artifacts (L2). Every response records the prompt name, version and a
content hash, so a result can always be tied to the exact text that produced it."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

TEMPLATES = Path(__file__).parent / "templates"


@lru_cache(maxsize=None)
def load_prompt(name: str, version: str) -> dict:
    path = TEMPLATES / f"{name}.{version}.txt"
    if not path.exists():
        raise FileNotFoundError(f"prompt {name}@{version} not found at {path}")
    text = path.read_text(encoding="utf-8")
    return {"name": name, "version": version, "text": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest()[:12], "ref": f"{name}@{version}"}


def available_versions(name: str) -> list[str]:
    return sorted(p.name.split(".")[1] for p in TEMPLATES.glob(f"{name}.*.txt"))


def active(role: str, variant: str) -> dict:
    """classifier follows PROMPT_VARIANT (v1|v2); verifier has a single version."""
    if role == "classifier":
        return load_prompt("classifier", variant)
    return load_prompt("verifier", "v1")
