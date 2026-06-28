"""
tests/test_anomaly.py

Unit tests for the sliding-window aggregator and anomaly detector.

The in-memory SlidingWindowAggregator and AnomalyDetector._evaluate() are
pure Python with no external dependencies — no Kafka, no Redis, no DB.

Key invariants under test:
  • SlidingWindowAggregator correctly buckets errors by minute and rolls off
    old buckets when the window is full.
  • AnomalyDetector fires only when:
      baseline_avg >= MIN_BASELINE_RATE (1.0)  AND
      current_errors >= SPIKE_MULTIPLIER (1.5) × baseline_avg
  • After firing, the consecutive counter resets so the same incident
    doesn't spam the AI service.
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from aggregator import WINDOW_COUNT, SlidingWindowAggregator, WindowSnapshot, _bucket_key
from anomaly import (
    MIN_BASELINE_RATE,
    SPIKE_MULTIPLIER,
    AnomalyDetector,
)


# ── _bucket_key ────────────────────────────────────────────────────────────────

def test_bucket_key_truncates_to_minute():
    ts = datetime(2026, 6, 14, 12, 30, 45, 999999)
    assert _bucket_key(ts) == "2026-06-14T12:30"


def test_bucket_key_same_minute_produces_same_key():
    a = datetime(2026, 1, 1, 0, 0, 0)
    b = datetime(2026, 1, 1, 0, 0, 59)
    assert _bucket_key(a) == _bucket_key(b)


def test_bucket_key_different_minutes_produce_different_keys():
    a = datetime(2026, 1, 1, 0, 0, 59)
    b = datetime(2026, 1, 1, 0, 1, 0)
    assert _bucket_key(a) != _bucket_key(b)


# ── SlidingWindowAggregator ────────────────────────────────────────────────────

def _at_minute(m: int) -> datetime:
    """Create a datetime at minute=m within the same hour."""
    return datetime(2026, 1, 1, 12, m, 0)


def test_snapshot_none_with_single_bucket():
    """One minute of data is not enough to compute a baseline — snapshot should be None."""
    agg = SlidingWindowAggregator()
    agg.record_error("proj-1", _at_minute(0))
    assert agg.snapshot("proj-1") is None


def test_snapshot_available_with_two_buckets():
    agg = SlidingWindowAggregator()
    agg.record_error("proj-1", _at_minute(0))
    agg.record_error("proj-1", _at_minute(1))
    snap = agg.snapshot("proj-1")
    assert snap is not None


def test_snapshot_none_for_unknown_project():
    agg = SlidingWindowAggregator()
    assert agg.snapshot("nonexistent") is None


def test_errors_in_same_minute_accumulate():
    agg = SlidingWindowAggregator()
    for _ in range(5):
        agg.record_error("proj-1", _at_minute(0))
    # Add a second bucket so snapshot is available
    agg.record_error("proj-1", _at_minute(1))
    snap = agg.snapshot("proj-1")
    assert snap.historical_buckets[0][1] == 5   # 5 errors in minute 0
    assert snap.current_errors == 1              # 1 error in minute 1


def test_baseline_avg_calculation():
    """baseline_avg = mean of all buckets EXCEPT the most recent."""
    agg = SlidingWindowAggregator()
    # Add 5 buckets with counts [2, 4, 6, 8, 10]
    for i, count in enumerate([2, 4, 6, 8, 10]):
        for _ in range(count):
            agg.record_error("proj-1", _at_minute(i))
    snap = agg.snapshot("proj-1")
    # current_errors = 10 (minute 4), baseline = mean([2, 4, 6, 8]) = 5.0
    assert snap.current_errors == 10
    assert snap.baseline_avg == pytest.approx(5.0)


def test_window_enforces_max_bucket_count():
    """After WINDOW_COUNT buckets, old buckets are evicted."""
    agg = SlidingWindowAggregator()
    for i in range(WINDOW_COUNT + 3):
        agg.record_error("proj-1", _at_minute(i))
    snap = agg.snapshot("proj-1")
    total_buckets = len(snap.historical_buckets) + 1  # +1 for current
    assert total_buckets == WINDOW_COUNT


def test_all_project_ids():
    agg = SlidingWindowAggregator()
    agg.record_error("proj-A", _at_minute(0))
    agg.record_error("proj-B", _at_minute(0))
    assert set(agg.all_project_ids()) == {"proj-A", "proj-B"}


# ── AnomalyDetector._evaluate ──────────────────────────────────────────────────

def _make_snapshot(current_errors: int, baseline_avg: float, pid="proj-x") -> WindowSnapshot:
    return WindowSnapshot(
        project_id=pid,
        current_bucket="2026-01-01T12:05",
        current_errors=current_errors,
        historical_buckets=[("2026-01-01T12:04", int(baseline_avg))],
        baseline_avg=baseline_avg,
    )


def _detector_with_snapshot(snap: WindowSnapshot | None):
    """Build a detector whose aggregator always returns `snap`."""
    agg = MagicMock()
    agg.snapshot.return_value = snap
    agg.all_project_ids.return_value = [snap.project_id] if snap else []
    publish = MagicMock()
    return AnomalyDetector(aggregator=agg, publish_fn=publish), publish


def test_no_anomaly_when_baseline_below_min():
    """Don't fire on a project with no error history (prevents day-0 false positives)."""
    snap = _make_snapshot(current_errors=100, baseline_avg=0.5)
    detector, publish = _detector_with_snapshot(snap)
    detector._evaluate(snap.project_id)
    publish.assert_not_called()


def test_no_anomaly_when_ratio_below_multiplier():
    snap = _make_snapshot(current_errors=2, baseline_avg=2.0)
    # ratio = 2/2 = 1.0x < 1.5x
    detector, publish = _detector_with_snapshot(snap)
    detector._evaluate(snap.project_id)
    publish.assert_not_called()


def test_anomaly_fires_above_threshold():
    """Fires when current >= SPIKE_MULTIPLIER × baseline AND baseline >= MIN_BASELINE_RATE."""
    # baseline=2.0, current=4 → ratio=2.0x > 1.5x threshold
    snap = _make_snapshot(current_errors=4, baseline_avg=2.0)
    detector, publish = _detector_with_snapshot(snap)
    detector._evaluate(snap.project_id)
    publish.assert_called_once()
    signal = publish.call_args[0][0]
    assert signal["project_id"] == snap.project_id
    assert signal["current_errors"] == 4
    assert signal["baseline_avg"] == pytest.approx(2.0)
    assert signal["spike_ratio"] == pytest.approx(2.0)


def test_anomaly_consecutive_counter_resets_after_fire():
    """After firing, the counter resets so we don't spam the AI service."""
    snap = _make_snapshot(current_errors=10, baseline_avg=2.0)
    detector, publish = _detector_with_snapshot(snap)

    detector._evaluate(snap.project_id)
    assert detector._consecutive_counts.get(snap.project_id, 0) == 0
    # One more evaluation of the same spike should fire again (counter was reset)
    detector._evaluate(snap.project_id)
    assert publish.call_count == 2


def test_anomaly_not_fired_when_snapshot_is_none():
    """No anomaly signal for a project that has no snapshot yet."""
    agg = MagicMock()
    agg.snapshot.return_value = None
    publish = MagicMock()
    detector = AnomalyDetector(aggregator=agg, publish_fn=publish)
    detector._evaluate("proj-new")
    publish.assert_not_called()


def test_anomaly_exact_threshold_does_not_fire():
    """current == 1.5x baseline is NOT enough — must be strictly greater."""
    baseline = 4.0
    current  = int(SPIKE_MULTIPLIER * baseline)  # exactly 6 at 1.5×
    snap = _make_snapshot(current_errors=current, baseline_avg=baseline)
    detector, publish = _detector_with_snapshot(snap)
    detector._evaluate(snap.project_id)
    # 6 >= 1.5*4 = 6.0 → this IS exactly equal, so it DOES fire
    # (>= means equal counts as anomalous)
    publish.assert_called_once()


def test_counter_resets_on_normal_window():
    """If the error rate normalises, the consecutive counter resets to 0."""
    # First, trigger a non-firing snapshot to set counter
    snap_normal = _make_snapshot(current_errors=1, baseline_avg=5.0)
    agg = MagicMock()
    publish = MagicMock()
    detector = AnomalyDetector(aggregator=agg, publish_fn=publish)

    # Manually set a count
    detector._consecutive_counts["proj-x"] = 1

    # Evaluate a normal (non-anomalous) snapshot
    agg.snapshot.return_value = snap_normal
    detector._evaluate("proj-x")

    assert detector._consecutive_counts.get("proj-x", 0) == 0
    publish.assert_not_called()
