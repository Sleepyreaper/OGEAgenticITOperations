"""Thread-safe, bounded TTL cache for the operations snapshot service
(app/operations/snapshot.py).

A single-purpose in-process cache: values are stored under an explicit
key, expire on wall-clock TTL (default 60s, see
OperationsConfig.snapshot_cache_ttl_seconds/OPERATIONS_SNAPSHOT_CACHE_TTL_SECONDS),
and a `threading.Lock` makes get/set/invalidate safe across Flask's
multi-threaded request handling (see docs/OPERATIONS_API.md's
concurrency note for why this is per-worker-process, not shared across
Gunicorn workers -- each worker rebuilds its own cache after a TTL
expiry or restart, which is an acceptable, documented tradeoff for a
bounded-staleness read cache, not a source of truth).

This module intentionally has no knowledge of what it's caching --
app.operations.snapshot decides what "a value" is (an OperationsSnapshot)
and what "a key" is (the normalized subscription set). Never cache a
raw exception/error as if it were a successful value: this cache is a
key/value store only -- the caller (snapshot.py) is responsible for
building a truthful value (one that still records source failures
inside it) before ever calling `set`.
"""

import threading
import time
from typing import Any, Iterable, Optional, Tuple

__all__ = ["DEFAULT_TTL_SECONDS", "SnapshotCache", "normalize_subscription_key"]

DEFAULT_TTL_SECONDS = 60


class SnapshotCache:
    """A minimal TTL cache: one `time.monotonic()`-based expiry per key.

    `time.monotonic()` (not wall-clock time) is used for expiry math so a
    system clock adjustment (NTP sync, manual change) never causes an
    entry to expire early or live longer than intended.
    """

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._entries: dict = {}  # key -> (expires_at_monotonic, value)

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    def get(self, key: Any) -> Optional[Any]:
        """Return the cached value for `key`, or None if absent/expired.
        An expired entry is evicted as a side effect of this lookup."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() >= expires_at:
                del self._entries[key]
                return None
            return value

    def set(self, key: Any, value: Any) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic() + self._ttl_seconds, value)

    def invalidate(self, key: Optional[Any] = None) -> None:
        """Evict one key, or every entry when `key` is None (explicit
        force-refresh support -- see app.operations.snapshot.get_snapshot's
        `force_refresh` parameter, which calls this before rebuilding)."""
        with self._lock:
            if key is None:
                self._entries.clear()
            else:
                self._entries.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def normalize_subscription_key(subscription_ids: Iterable[str]) -> Tuple[str, ...]:
    """Deterministic cache key for a subscription selection: trimmed,
    lowercased, de-duplicated, and sorted -- so `["A", "b", "a"]` and
    `["B", "A"]` share the same cache entry as `["a", "b"]` rather than
    each triggering its own (duplicate) Azure collection."""
    seen = set()
    normalized = []
    for raw in subscription_ids:
        value = str(raw).strip().lower()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return tuple(sorted(normalized))
