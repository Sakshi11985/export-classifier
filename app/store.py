"""Redis-backed state: queue, run records, trace events, quarantine, feedback, quality series,
run cache, rate limiting and worker heartbeats."""
from __future__ import annotations

import hashlib
import json
import time
import uuid

import redis.asyncio as aredis

from .config import Settings
from .textutil import squash

QUEUE = "queue:runs"

# token bucket (atomic): KEYS[1]=bucket ARGV: now, rate_per_s, burst, cost
_TOKEN_BUCKET = """
local b = redis.call('HMGET', KEYS[1], 't', 'ts')
local tokens = tonumber(b[1]); local ts = tonumber(b[2])
local now = tonumber(ARGV[1]); local rate = tonumber(ARGV[2]); local burst = tonumber(ARGV[3]); local cost = tonumber(ARGV[4])
if tokens == nil then tokens = burst; ts = now end
tokens = math.min(burst, tokens + (now - ts) * rate)
local ok = 0; local wait = 0
if tokens >= cost then tokens = tokens - cost; ok = 1 else wait = (cost - tokens) / rate end
redis.call('HSET', KEYS[1], 't', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], 3600)
return {ok, tostring(wait)}
"""


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:16]


class Store:
    def __init__(self, r: aredis.Redis, s: Settings):
        self.r = r
        self.s = s
        self._tb = r.register_script(_TOKEN_BUCKET)

    # ------------------------------------------------------------------ runs
    async def create_run(self, run_id: str, request: dict, client_id: str, traceparent: str | None,
                         cache_hit: bool = False) -> None:
        now = time.time()
        await self.r.hset(f"run:{run_id}", mapping={
            "run_id": run_id, "status": "queued", "created_at": now, "client_id": client_id,
            "request": json.dumps(request), "traceparent": traceparent or "", "cache_hit": int(cache_hit)})
        await self.r.expire(f"run:{run_id}", self.s.run_ttl_s)

    async def get_run(self, run_id: str) -> dict | None:
        h = await self.r.hgetall(f"run:{run_id}")
        if not h:
            return None
        for k in ("request", "result"):
            if h.get(k):
                h[k] = json.loads(h[k])
        for k in ("created_at", "started_at", "finished_at"):
            if h.get(k):
                h[k] = float(h[k])
        return h

    async def update_run(self, run_id: str, **fields) -> None:
        m = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in fields.items()}
        await self.r.hset(f"run:{run_id}", mapping=m)

    async def enqueue(self, run_id: str) -> None:
        await self.r.lpush(QUEUE, run_id)

    async def queue_depth(self) -> int:
        return await self.r.llen(QUEUE)

    async def processing_count(self) -> int:
        n = 0
        async for key in self.r.scan_iter("processing:*"):
            n += await self.r.llen(key)
        return n

    # reliable queue: claim moves the id to a per-worker processing list
    async def claim(self, worker_id: str, timeout_s: int = 2) -> str | None:
        return await self.r.blmove(QUEUE, f"processing:{worker_id}", timeout_s, "RIGHT", "LEFT")

    async def ack(self, worker_id: str, run_id: str) -> None:
        await self.r.lrem(f"processing:{worker_id}", 1, run_id)

    async def heartbeat(self, worker_id: str) -> None:
        await self.r.set(f"worker:{worker_id}", str(time.time()), ex=self.s.worker_heartbeat_ttl_s)

    async def workers_alive(self) -> int:
        n = 0
        async for _ in self.r.scan_iter("worker:*"):
            n += 1
        return n

    async def requeue_orphans(self) -> int:
        """Return runs claimed by workers that stopped heart-beating to the queue."""
        moved = 0
        async for key in self.r.scan_iter("processing:*"):
            wid = key.split(":", 1)[1]
            if await self.r.exists(f"worker:{wid}"):
                continue
            while True:
                rid = await self.r.rpoplpush(key, QUEUE)
                if not rid:
                    break
                moved += 1
        return moved

    # ------------------------------------------------------------------ trace
    async def add_trace(self, run_id: str, event: dict) -> None:
        await self.r.rpush(f"trace:{run_id}", json.dumps(event, default=str))
        await self.r.expire(f"trace:{run_id}", self.s.run_ttl_s)

    async def get_trace(self, run_id: str) -> list[dict]:
        return [json.loads(x) for x in await self.r.lrange(f"trace:{run_id}", 0, -1)]

    # ------------------------------------------------------------------ quarantine
    async def quarantine(self, raw: dict | str, errors: list[dict], reason: str, client_id: str,
                         correlation_id: str) -> str:
        qid = "q_" + uuid.uuid4().hex[:12]
        rec = {"quarantine_id": qid, "ts": time.time(), "reason": reason, "client_id": client_id,
               "correlation_id": correlation_id, "errors": errors, "agent_run_started": False,
               "payload": raw if isinstance(raw, dict) else str(raw)[:4000]}
        await self.r.set(f"quarantine:{qid}", json.dumps(rec, default=str), ex=self.s.run_ttl_s)
        await self.r.lpush("quarantine:ids", qid)
        await self.r.ltrim("quarantine:ids", 0, 4999)
        return qid

    async def get_quarantine(self, qid: str) -> dict | None:
        raw = await self.r.get(f"quarantine:{qid}")
        return json.loads(raw) if raw else None

    async def list_quarantine(self, limit: int = 50) -> list[dict]:
        ids = await self.r.lrange("quarantine:ids", 0, limit - 1)
        out = []
        for i in ids:
            rec = await self.get_quarantine(i)
            if rec:
                out.append(rec)
        return out

    # ------------------------------------------------------------------ feedback + quality
    async def add_feedback(self, run_id: str, rating: str, reason: str | None) -> dict:
        rec = {"run_id": run_id, "rating": rating, "reason": reason, "ts": time.time()}
        await self.r.rpush("feedback:all", json.dumps(rec))
        await self.r.hset(f"run:{run_id}", "feedback", json.dumps(rec))
        return rec

    async def feedback_summary(self) -> dict:
        rows = [json.loads(x) for x in await self.r.lrange("feedback:all", 0, -1)]
        total = len(rows)
        sat = sum(1 for x in rows if x["rating"] == "satisfactory")
        reasons: dict[str, int] = {}
        for x in rows:
            if x["rating"] == "unsatisfactory" and x.get("reason"):
                key = x["reason"].strip().lower()[:80]
                reasons[key] = reasons.get(key, 0) + 1
        return {"total": total, "satisfactory": sat, "unsatisfactory": total - sat,
                "satisfaction_ratio": round(sat / total, 4) if total else None,
                "top_unsatisfactory_reasons": sorted(reasons.items(), key=lambda kv: -kv[1])[:10]}

    async def add_quality(self, run_id: str, scores: dict) -> None:
        now = time.time()
        await self.r.zadd("quality:series", {json.dumps({"run_id": run_id, "ts": now, **scores}): now})
        await self.r.zremrangebyrank("quality:series", 0, -5001)

    async def quality_series(self, limit: int = 500) -> list[dict]:
        rows = await self.r.zrange("quality:series", -limit, -1)
        return [json.loads(x) for x in rows]

    # ------------------------------------------------------------------ run cache
    def run_cache_key(self, request: dict, index_version: str, variant: str, provider: str) -> str:
        canon = {k: v for k, v in request.items() if k not in ("client_reference", "options")}
        raw = json.dumps([squash(json.dumps(canon, sort_keys=True)), index_version, variant, provider])
        return "runcache:" + hashlib.sha256(raw.encode()).hexdigest()

    async def get_cached_result(self, key: str) -> dict | None:
        raw = await self.r.get(key)
        return json.loads(raw) if raw else None

    async def set_cached_result(self, key: str, result: dict) -> None:
        await self.r.set(key, json.dumps(result), ex=self.s.cache_ttl_s)

    # ------------------------------------------------------------------ rate limiting
    async def take_token(self, client_id: str) -> tuple[bool, float]:
        rate = self.s.rate_limit_per_min / 60.0
        res = await self._tb(keys=[f"rl:{client_id}"], args=[time.time(), rate, self.s.rate_limit_burst, 1])
        return bool(int(res[0])), float(res[1])

    async def inflight_add(self, client_id: str, run_id: str) -> bool:
        key = f"inflight:{client_id}"
        n = await self.r.scard(key)
        if n >= self.s.max_inflight_per_client:
            return False
        await self.r.sadd(key, run_id)
        await self.r.expire(key, 3600)
        return True

    async def inflight_remove(self, client_id: str, run_id: str) -> None:
        await self.r.srem(f"inflight:{client_id}", run_id)

    # ------------------------------------------------------------------ misc
    async def index_meta(self) -> dict:
        raw = await self.r.get("index:meta")
        return json.loads(raw) if raw else {}

    async def ping(self) -> bool:
        return bool(await self.r.ping())
