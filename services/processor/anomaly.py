"""
Anomaly detector.

Runs on a background thread, waking up every 60 seconds to evaluate each
project's sliding window snapshot. Fires an anomaly signal when:

    current_errors >= SPIKE_MULTIPLIER * baseline_avg
    AND baseline_avg >= MIN_BASELINE_RATE  (ignore near-zero baselines)
    AND this condition has been true for CONSECUTIVE_WINDOWS_REQUIRED windows

When an anomaly is confirmed, a JSON signal is published to the
`engram.alerts.anomalies` Kafka topic for the AI service to consume.
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from aggregator import SlidingWindowAggregator, WindowSnapshot

logger = logging.getLogger(__name__)

# Anomaly fires when current rate >= SPIKE_MULTIPLIER × baseline average
SPIKE_MULTIPLIER = 1.5

# Require at least this many errors/min in baseline before we can call it an anomaly.
# Prevents false positives on brand-new projects with zero error history.
MIN_BASELINE_RATE = 1.0

# How many consecutive anomalous windows before we publish an alert
CONSECUTIVE_WINDOWS_REQUIRED = 1

# Evaluation interval in seconds
EVAL_INTERVAL_SECONDS = 30


class AnomalyDetector:
    def __init__(
        self,
        aggregator: SlidingWindowAggregator,
        publish_fn: Callable[[dict], None],
    ) -> None:
        """
        Args:
            aggregator:  The SlidingWindowAggregator to read snapshots from.
            publish_fn:  Callable that publishes a dict payload to the anomaly topic.
                         Injected so this class has no direct Kafka dependency.
        """
        self._aggregator = aggregator
        self._publish = publish_fn

        # project_id → consecutive anomalous window count
        self._consecutive_counts: dict[str, int] = {}

        self._thread = threading.Thread(
            target=self._evaluation_loop,
            name="engram-anomaly-detector",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        logger.info(
            f"Anomaly detector started | interval={EVAL_INTERVAL_SECONDS}s "
            f"spike_multiplier={SPIKE_MULTIPLIER}× "
            f"consecutive_required={CONSECUTIVE_WINDOWS_REQUIRED}"
        )

    def _evaluation_loop(self) -> None:
        while True:
            time.sleep(EVAL_INTERVAL_SECONDS)
            for project_id in self._aggregator.all_project_ids():
                try:
                    self._evaluate(project_id)
                except Exception as e:
                    logger.error(f"Anomaly evaluation failed for {project_id}: {e}")

    def _evaluate(self, project_id: str) -> None:
        snapshot = self._aggregator.snapshot(project_id)
        if snapshot is None:
            return  # not enough history yet

        is_anomalous = (
            snapshot.baseline_avg >= MIN_BASELINE_RATE
            and snapshot.current_errors >= SPIKE_MULTIPLIER * snapshot.baseline_avg
        )

        if is_anomalous:
            self._consecutive_counts[project_id] = (
                self._consecutive_counts.get(project_id, 0) + 1
            )
            logger.info(
                f"[{project_id}] Anomaly window "
                f"{self._consecutive_counts[project_id]}/{CONSECUTIVE_WINDOWS_REQUIRED} | "
                f"errors={snapshot.current_errors} baseline={snapshot.baseline_avg:.1f}"
            )
        else:
            if self._consecutive_counts.get(project_id, 0) > 0:
                logger.info(
                    f"[{project_id}] Error rate normalised — resetting consecutive counter"
                )
            self._consecutive_counts[project_id] = 0

        if self._consecutive_counts.get(project_id, 0) >= CONSECUTIVE_WINDOWS_REQUIRED:
            self._fire_anomaly(snapshot)
            # Reset so we don't spam the AI service with repeated signals for
            # the same ongoing incident
            self._consecutive_counts[project_id] = 0

    def _fire_anomaly(self, snapshot: WindowSnapshot) -> None:
        signal = {
            "project_id": snapshot.project_id,
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "current_bucket": snapshot.current_bucket,
            "current_errors": snapshot.current_errors,
            "baseline_avg": round(snapshot.baseline_avg, 2),
            "spike_ratio": round(
                snapshot.current_errors / snapshot.baseline_avg
                if snapshot.baseline_avg > 0 else 0,
                2,
            ),
            "historical_buckets": snapshot.historical_buckets,
        }
        logger.warning(
            f"🚨 ANOMALY CONFIRMED [{snapshot.project_id}] | "
            f"errors={snapshot.current_errors} baseline={snapshot.baseline_avg:.1f} "
            f"ratio={signal['spike_ratio']}×"
        )
        self._publish(signal)
