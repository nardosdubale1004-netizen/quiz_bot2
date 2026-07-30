# src/cache.py
"""
Minimal in-process TTL cache for read-mostly DB data. Not a distributed cache —
this lives in the bot process's memory only, which is exactly what we want here:
question/track data is read constantly (every button tap) but written rarely
(only on send/import/status-change), so paying one DB round trip per WRITE and
serving reads from memory is the correct trade for a network floor of ~400ms.
"""
import time
import threading

class TTLCache:
    def __init__(self, default_ttl: float = 30.0):
        self._store = {}
        self._lock = threading.Lock()
        self.default_ttl = default_ttl

    def get(self, key):
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            value, expires_at = entry
            if expires_at is not None and time.time() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key, value, ttl: float = None):
        ttl = self.default_ttl if ttl is None else ttl
        expires_at = (time.time() + ttl) if ttl is not None else None
        with self._lock:
            self._store[key] = (value, expires_at)

    def invalidate(self, key):
        with self._lock:
            self._store.pop(key, None)

    def invalidate_prefix(self, prefix: str):
        with self._lock:
            for k in list(self._store.keys()):
                if isinstance(k, str) and k.startswith(prefix):
                    del self._store[k]

    def clear(self):
        with self._lock:
            self._store.clear()

# Process-wide cache instance for track/question lookups.
# ttl=None on individual set() calls means "cache forever until explicitly invalidated",
# which is what we want for track+question data: it's only ever stale if a write
# happened, and every write path below explicitly invalidates the relevant key.
track_question_cache = TTLCache(default_ttl=None)