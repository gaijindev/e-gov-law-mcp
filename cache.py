"""Tiny in-process TTL + size-bounded cache.

No external dependency (keeps the server's footprint small) — a plain dict
with a monotonic-clock TTL and FIFO eviction once `maxsize` is hit. Safe for
a single FastMCP process; not shared across replicas.

Statute text changes rarely (amendments are dated and infrequent), and our
own bridge (GaijinOS) re-resolves the same handful of laws across many
articles, so caching the law-name resolution and the raw law XML fetch turns
most `find_law_article`/`get_law_content` calls into cache hits after the
first lookup of a given law.
"""

import threading
import time
from functools import wraps


class TTLCache:
    def __init__(self, maxsize: int = 256, ttl: float = 21_600):
        self.maxsize = maxsize
        self.ttl = ttl
        self._data: dict = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None, False
            value, expires_at = entry
            if time.monotonic() > expires_at:
                del self._data[key]
                self.misses += 1
                return None, False
            self.hits += 1
            return value, True

    def set(self, key, value):
        with self._lock:
            if key not in self._data and len(self._data) >= self.maxsize:
                oldest_key = next(iter(self._data))
                del self._data[oldest_key]
            self._data[key] = (value, time.monotonic() + self.ttl)

    def clear(self):
        with self._lock:
            n = len(self._data)
            self._data.clear()
            self.hits = 0
            self.misses = 0
            return n

    def stats(self):
        with self._lock:
            total = self.hits + self.misses
            return {
                "size": len(self._data),
                "maxsize": self.maxsize,
                "ttl_seconds": self.ttl,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else None,
            }


def ttl_cached(cache: TTLCache):
    """Decorator caching a function's return value by its (args, kwargs)."""

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            value, hit = cache.get(key)
            if hit:
                return value
            result = fn(*args, **kwargs)
            cache.set(key, result)
            return result

        wrapper.cache = cache
        return wrapper

    return decorator
