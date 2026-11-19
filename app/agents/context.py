"""Per-run context shared by tools and agents: the validated request, the passages the agent has
actually retrieved, the trace recorder and the loop bounds."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


class Trace:
    """Records every agent step so a run can be reconstructed from its id (D5)."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.events: list[dict] = []
        self._flushed = 0
        self._seq = 0

    def emit(self, type_: str, **fields: Any) -> dict:
        self._seq += 1
        ev = {"seq": self._seq, "ts": time.time(), "run_id": self.run_id, "type": type_, **fields}
        self.events.append(ev)
        return ev

    def pending(self) -> list[dict]:
        out = self.events[self._flushed:]
        self._flushed = len(self.events)
        return out


@dataclass
class UsageTotals:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    by_step: dict = field(default_factory=dict)
    estimated: bool = False

    def add(self, step: str, p: int, c: int, cost: float, estimated: bool) -> None:
        self.prompt_tokens += p
        self.completion_tokens += c
        self.cost_usd += cost
        self.calls += 1
        self.estimated = self.estimated or estimated
        s = self.by_step.setdefault(step, {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "calls": 0})
        s["prompt_tokens"] += p
        s["completion_tokens"] += c
        s["cost_usd"] = round(s["cost_usd"] + cost, 8)
        s["calls"] += 1

    def as_dict(self) -> dict:
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "cost_usd": round(self.cost_usd, 8), "llm_calls": self.calls, "by_step": self.by_step,
                "estimated": self.estimated}


@dataclass
class RunContext:
    run_id: str
    request: dict
    corpus: Any
    settings: Any
    llm: Any
    trace: Trace
    deadline: float
    retrieved: dict = field(default_factory=dict)  # chunk id -> text exactly as returned to the agent
    steps: int = 0
    tool_calls: int = 0
    usage: UsageTotals = field(default_factory=UsageTotals)
    degraded: bool = False
    degraded_reason: str = ""
    fault: str = "none"
    strategy: str = "default"
    vector_only: bool = False
    prompt_versions: dict = field(default_factory=dict)
    limits_hit: list = field(default_factory=list)

    def time_left(self) -> float:
        return self.deadline - time.monotonic()

    def over_budget(self) -> str | None:
        s = self.settings
        if self.steps >= s.agent_max_steps:
            return "max_steps"
        if self.tool_calls >= s.agent_max_tool_calls:
            return "max_tool_calls"
        if self.time_left() <= 0:
            return "wall_clock"
        return None
