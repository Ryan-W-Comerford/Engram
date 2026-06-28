"""
Engram AI Service — Phase 3

Consumes anomaly signals from `engram.alerts.anomalies` and for each one:
  1. Fetches recent error + deployment context from PostgreSQL
  2. Calls Claude to generate an incident report
  3. Persists the incident (with AI analysis) to the DB

On unrecoverable failure (parse error, DB unavailable before first commit),
the raw message is forwarded to `engram.alerts.dlq` so no signal is silently
lost. Failures that occur after the incident row is written are logged but
do not go to the DLQ — the incident already exists, just without AI enrichment.
"""

import json
import logging
import os
import signal
import sys
import threading
import time

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer

from shared.logging_config import setup_logging
from shared.kafka_config import consumer_config, producer_config
from incident import process_anomaly_signal
from digest import run_daily_digests

setup_logging("ai-service")
logger = logging.getLogger(__name__)

KAFKA_TOPIC_ANOMALIES = os.getenv("KAFKA_TOPIC_ANOMALIES", "engram.alerts.anomalies")
KAFKA_TOPIC_DLQ       = os.getenv("KAFKA_TOPIC_DLQ",       "engram.alerts.dlq")
KAFKA_CONSUMER_GROUP  = os.getenv("KAFKA_CONSUMER_GROUP",  "engram-ai-service")


def _send_to_dlq(producer: Producer, raw_bytes: bytes, error: str) -> None:
    try:
        payload = json.dumps({
            "original": raw_bytes.decode("utf-8", errors="replace"),
            "error":    error,
        })
        producer.produce(topic=KAFKA_TOPIC_DLQ, value=payload)
        producer.poll(0)
        logger.warning(f"Message forwarded to DLQ | error={error[:200]}")
    except Exception as e:
        logger.error(f"Failed to write to DLQ: {e}")


def _digest_loop() -> None:
    """Run daily at 00:05 UTC; generate yesterday's digest for all projects that lack one."""
    from datetime import timedelta, timezone as _tz
    while True:
        try:
            run_daily_digests()
        except Exception as e:
            logger.error(f"Digest loop error: {e}")

        now = datetime.now(timezone.utc)
        next_run = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        sleep_secs = (next_run - now).total_seconds()
        logger.info(f"Next digest run at {next_run.isoformat()} (in {sleep_secs/3600:.1f}h)")
        time.sleep(sleep_secs)


def _validate_startup() -> None:
    missing = [k for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY") if not os.getenv(k)]
    if missing:
        logger.error(
            f"Required environment variable(s) not set: {', '.join(missing)}. "
            "Incidents will be routed to DLQ until this is corrected."
        )


def main() -> None:
    _validate_startup()

    digest_thread = threading.Thread(target=_digest_loop, daemon=True, name="digest-loop")
    digest_thread.start()
    logger.info("Daily digest background thread started")

    dlq_producer = Producer(producer_config("engram-ai-dlq"))

    consumer = Consumer(consumer_config(KAFKA_CONSUMER_GROUP, {
        # Claude + OpenAI embedding can take 20-60 s combined; give ample headroom
        # before Kafka considers the consumer dead and triggers a rebalance.
        "session.timeout.ms":     60_000,
        "max.poll.interval.ms":  300_000,
    }))
    consumer.subscribe([KAFKA_TOPIC_ANOMALIES])
    logger.info(
        f"AI service started | "
        f"topic={KAFKA_TOPIC_ANOMALIES} group={KAFKA_CONSUMER_GROUP}"
    )

    running = True

    def _shutdown(*_):
        nonlocal running
        logger.info("Shutdown signal received")
        running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        while running:
            msg = consumer.poll(timeout=2.0)

            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                logger.error(f"Kafka error: {msg.error()}")
                continue

            _handle_message(msg, dlq_producer)

            # Only commit after full incident + Claude analysis is written to DB
            consumer.commit(message=msg, asynchronous=False)

    except KafkaException as e:
        logger.error(f"Fatal Kafka error: {e}")
        sys.exit(1)
    finally:
        consumer.close()
        dlq_producer.flush(timeout=5)
        logger.info("AI service stopped")


def _handle_message(msg, dlq_producer: Producer) -> None:
    raw = msg.value()

    try:
        signal_data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"Could not parse anomaly signal: {e}")
        _send_to_dlq(dlq_producer, raw, str(e))
        return

    project_id  = signal_data.get("project_id", "unknown")
    spike_ratio = signal_data.get("spike_ratio", "?")
    current_errors = signal_data.get("current_errors", "?")

    logger.info(
        f"Anomaly signal received | project={project_id} "
        f"errors={current_errors} ratio={spike_ratio}×"
    )

    try:
        process_anomaly_signal(signal_data)
    except Exception as e:
        # process_anomaly_signal only re-raises when the incident row was never
        # committed — route to DLQ so the signal can be replayed later.
        logger.error(f"Incident creation failed, routing to DLQ | project={project_id} error={e}")
        _send_to_dlq(dlq_producer, raw, str(e))


if __name__ == "__main__":
    main()
