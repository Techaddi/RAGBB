"""Small infra helpers: plan cache, rate limiter, API-key auth."""
from __future__ import annotations

import hmac
import re
import time
from collections import defaultdict, deque

from cachetools import TTLCache
from fastapi import Header, HTTPException, Request

from .config import Settings


def normalize_question(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip().lower()).rstrip("?.! ")


class PlanCache:
    """Caches generated plans (SQL + proposed DDL), NOT result rows - data stays fresh,
    but repeat questions skip retrieval + LLM entirely (the slow part)."""
    def __init__(self, size: int, ttl: int):
        self.c: TTLCache = TTLCache(maxsize=size, ttl=ttl)
        self.hits = self.misses = 0

    def get(self, key):
        v = self.c.get(key)
        self.hits += v is not None
        self.misses += v is None
        return v

    def set(self, key, val):
        self.c[key] = val

    def clear(self):
        self.c.clear()


class RateLimiter:
    """Sliding window per API key. In-memory = per process; use Redis if you run many replicas."""
    def __init__(self):
        self.hits: dict[str, deque] = defaultdict(deque)

    def check(self, key: str, limit: int):
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(429, "Rate limit exceeded", headers={"Retry-After": "10"})
        q.append(now)


_limiter = RateLimiter()


def _match(provided: str | None, allowed: set[str]) -> bool:
    if not provided:
        return False
    return any(hmac.compare_digest(provided.encode(), a.encode()) for a in allowed)


async def require_api_key(request: Request, x_api_key: str | None = Header(None)) -> str:
    s: Settings = request.app.state.settings
    if not _match(x_api_key, s.api_key_set):
        raise HTTPException(401, "Invalid or missing X-API-Key")
    _limiter.check(x_api_key, s.rate_limit_per_minute)
    return x_api_key


async def require_admin(request: Request, x_api_key: str | None = Header(None)):
    s: Settings = request.app.state.settings
    if not s.admin_api_key or not _match(x_api_key, {s.admin_api_key}):
        raise HTTPException(403, "Admin key required")
