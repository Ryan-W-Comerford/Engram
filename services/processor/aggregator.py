"""
Sliding window aggregator — Redis-backed with in-memory fallback.

Call make_aggregator() to get the right implementation:
  - REDIS_URL set and reachable → RedisWindowAggregator
    State survives processor restarts and works across multiple instances.
  - REDIS_URL absent or unreachable → SlidingWindowAggregator (in-memory)
    Works for local dev; loses 5–6 min of history on restart.

Redis data model
----------------
  Hash  engram:window:{project_id}
        field = "YYYY-MM-DDTHH:MM"  value = error count (integer)
        TTL = 10 minutes (auto-expiry cleans up idle projects)

  Set   engram:active_projects
        members = project_ids with recent error events
        TTL = 10 minutes
"""

import logging
import os
import threading
from collections import defaultdict, deque
from datetime import datetime
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

WINDOW_COUNT = 6  # 5 historical + 1 current (evaluation window)
_REDIS_TTL   = 600  # 10 minutes — matches WINDOW_COUNT × 60s + headroom


class WindowSnapshot(NamedTuple):
    project_id:         str
    current_bucket:     str
    current_errors:     int
    historical_buckets: list[tuple[str, int]]
    baseline_avg:       float


def _bucket_key(ts: datetime) -> str:
    """Truncate a timestamp to its minute — '2026-06-14T12:30'."""
    return ts.replace(second=0, microsecond=0, tzinfo=None).strftime("%Y-%m-%dT%H:%M")


# ── Redis-backed aggregator ────────────────────────────────────────────────────

class RedisWindowAggregator:
    _KEY_PREFIX = "engram:window:"
    _ACTIVE_KEY = "engram:active_projects"

    def __init__(self, redis_client) -> None:
        self._r = redis_client

    def record_error(self, project_id: str, event_timestamp: datetime) -> None:
        bucket = _bucket_key(event_timestamp)
        pipe = self._r.pipeline()
        pipe.hincrby(f"{self._KEY_PREFIX}{project_id}", bucket, 1)
        pipe.expire(f"{self._KEY_PREFIX}{project_id}", _REDIS_TTL)
        pipe.sadd(self._ACTIVE_KEY, project_id)
        pipe.expire(self._ACTIVE_KEY, _REDIS_TTL)
        pipe.execute()

    def snapshot(self, project_id: str) -> Optional[WindowSnapshot]:
        raw = self._r.hgetall(f"{self._KEY_PREFIX}{project_id}")
        if not raw:
            return None

        # Decode, sort chronologically, keep last WINDOW_COUNT buckets
        items = sorted(
            [(k.decode(), int(v)) for k, v in raw.items()],
            key=lambda x: x[0],
        )[-WINDOW_COUNT:]

        if len(items) < 2:
            return None

        current_key, current_count = items[-1]
        historical = items[:-1]
        baseline_avg = sum(c for _, c in historical) / len(historical)

        return WindowSnapshot(
            project_id=project_id,
            current_bucket=current_key,
            current_errors=current_count,
            historical_buckets=historical,
            baseline_avg=baseline_avg,
        )

    def all_project_ids(self) -> list[str]:
        return [m.decode() for m in self._r.smembers(self._ACTIVE_KEY)]


# ── In-memory aggregator (local dev / Redis fallback) ─────────────────────────

class SlidingWindowAggregator:
    def __init__(self) -> None:
        self._windows: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=WINDOW_COUNT)
        )
        self._lock = threading.Lock()

    def record_error(self, project_id: str, event_timestamp: datetime) -> None:
        bucket = _bucket_key(event_timestamp)
        with self._lock:
            windows = self._windows[project_id]
            if windows and windows[-1][0] == bucket:
                key, count = windows[-1]
                windows[-1] = (key, count + 1)
            else:
                windows.append((bucket, 1))
                logger.debug(f"[{project_id}] New bucket: {bucket} (total: {len(windows)})")

    def snapshot(self, project_id: str) -> Optional[WindowSnapshot]:
        with self._lock:
            windows = self._windows.get(project_id)
            if not windows or len(windows) < 2:
                return None
            items = list(windows)

        current_key, current_count = items[-1]
        historical = items[:-1]
        baseline_avg = sum(c for _, c in historical) / len(historical)

        return WindowSnapshot(
            project_id=project_id,
            current_bucket=current_key,
            current_errors=current_count,
            historical_buckets=historical,
            baseline_avg=baseline_avg,
        )

    def all_project_ids(self) -> list[str]:
        with self._lock:
            return list(self._windows.keys())


# ── Factory ────────────────────────────────────────────────────────────────────

def make_aggregator() -> RedisWindowAggregator | SlidingWindowAggregator:
    """
    Returns a RedisWindowAggregator when REDIS_URL is set and reachable,
    otherwise falls back to the in-memory SlidingWindowAggregator with a warning.
    """
    redis_url = os.getenv("REDIS_URL", "")
    if redis_url:
        try:
            import redis as redis_lib
            client = redis_lib.from_url(
                redis_url,
                socket_timeout=2,
                socket_connect_timeout=2,
                decode_responses=False,
            )
            client.ping()
            host = redis_url.split("@")[-1] if "@" in redis_url else redis_url
            logger.info(f"Redis aggregator ready | host={host}")
            return RedisWindowAggregator(client)
        except Exception as e:
            logger.warning(
                f"Redis unavailable ({e}) — falling back to in-memory aggregator. "
                "Sliding window state will be lost on restart."
            )

    logger.warning(
        "REDIS_URL not set — using in-memory sliding window. "
        "Set REDIS_URL for production to persist state across restarts."
    )
    return SlidingWindowAggregator()
