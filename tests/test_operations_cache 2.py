#!/usr/bin/env python3
"""Test the thread-safe TTL snapshot cache (app/operations/cache.py).

Run: python3 tests/test_operations_cache.py
"""
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.cache import SnapshotCache, normalize_subscription_key  # noqa: E402

PASS = 0
FAIL = 0


def test(name, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  \u2705 {name}")
    else:
        FAIL += 1
        print(f"  \u274c {name}")


print("\n\U0001f9ea Test 1: SnapshotCache -- basic get/set/expiry")
cache = SnapshotCache(ttl_seconds=1)
test("miss on an empty cache returns None", cache.get("k1") is None)
cache.set("k1", "value1")
test("hit returns the stored value", cache.get("k1") == "value1")
test("__len__ reflects one entry", len(cache) == 1)
time.sleep(1.1)
test("entry expires after ttl_seconds", cache.get("k1") is None)
test("an expired entry is evicted (len back to 0)", len(cache) == 0)

print("\n\U0001f9ea Test 2: SnapshotCache -- invalidate")
cache2 = SnapshotCache(ttl_seconds=60)
cache2.set("a", 1)
cache2.set("b", 2)
cache2.invalidate("a")
test("invalidate(key) removes only that key", cache2.get("a") is None and cache2.get("b") == 2)
cache2.invalidate()
test("invalidate() with no key clears everything", cache2.get("b") is None and len(cache2) == 0)

print("\n\U0001f9ea Test 3: SnapshotCache -- rejects non-positive ttl_seconds")
try:
    SnapshotCache(ttl_seconds=0)
    test("ttl_seconds=0 raises ValueError", False)
except ValueError:
    test("ttl_seconds=0 raises ValueError", True)
try:
    SnapshotCache(ttl_seconds=-5)
    test("negative ttl_seconds raises ValueError", False)
except ValueError:
    test("negative ttl_seconds raises ValueError", True)

print("\n\U0001f9ea Test 4: SnapshotCache -- thread safety under concurrent get/set")
cache3 = SnapshotCache(ttl_seconds=60)
errors = []


def worker(n):
    try:
        for i in range(200):
            cache3.set(f"key-{n}", i)
            cache3.get(f"key-{n}")
    except Exception as exc:  # pragma: no cover -- would only fire on a real race/bug
        errors.append(exc)


threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
test("no exceptions raised across 8 concurrent workers", errors == [])
test("each worker's final value is its last write", all(cache3.get(f"key-{n}") == 199 for n in range(8)))

print("\n\U0001f9ea Test 5: normalize_subscription_key -- order/case/duplicate-insensitive")
test("different order produces the same key", normalize_subscription_key(["B", "A"]) == normalize_subscription_key(["A", "B"]))
test("different case produces the same key", normalize_subscription_key(["SubA"]) == normalize_subscription_key(["suba"]))
test("duplicates are collapsed", normalize_subscription_key(["a", "a", "b"]) == ("a", "b")
     or normalize_subscription_key(["a", "a", "b"]) == normalize_subscription_key(["a", "b"]))
test("blank/whitespace-only entries are dropped", normalize_subscription_key(["a", "  ", ""]) == ("a",))
test("a different subscription set produces a different key", normalize_subscription_key(["a"]) != normalize_subscription_key(["a", "b"]))


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
