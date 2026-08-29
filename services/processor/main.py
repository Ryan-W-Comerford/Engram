"""
Engram Stream Processor — updated for OTel

Consumes from `engram.events.raw`, which receives both:
  - OTel JSON (from the OTel Collector's kafkaexporter) — any language
  - Engram-native JSON (from the ingestor's /ingest endpoint via the SDK)

The shared otel_normalizer translates both formats into Engram internal
event dicts before they reach the aggregator and anomaly detector.

Messages that cannot be parsed are forwarded to `engram.events.dlq` so
they can be inspected and replayed rather than silently dropped.
"""

import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer

from shared.logging_config import setup_logging
from shared.kafka_config import consumer_config, producer_config
from aggregator import make_aggregator
from anomaly import AnomalyDetector
from shared.otel_normalizer import normalize

setup_logging("processor")
logger = logging.getLogger(__name__)

KAFKA_TOPIC_RAW       = os.getenv("KAFKA_TOPIC_RAW",       "engram.events.raw")
KAFKA_TOPIC_ANOMALIES = os.getenv("KAFKA_TOPIC_ANOMALIES", "engram.alerts.anomalies")
KAFKA_TOPIC_DLQ       = os.getenv("KAFKA_TOPIC_DLQ",       "engram.events.dlq")
KAFKA_CONSUMER_GROUP  = os.getenv("KAFKA_CONSUMER_GROUP",  "engram-processor")


def _anomaly_delivery_report(err, msg):
    if err:
        logger.error(f"Anomaly signal delivery failed: {err}")
    else:
        logger.debug(f"Anomaly signal delivered → {msg.topic()} offset={msg.offset()}")


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


def main() -> None:
    aggregator = make_aggregator()

    anomaly_producer = Producer(producer_config("engram-processor-anomaly"))
    dlq_producer     = Producer(producer_config("engram-processor-dlq"))

    def publish_anomaly(signal_data: dict) -> None:
        anomaly_producer.produce(
            topic=KAFKA_TOPIC_ANOMALIES,
            key=signal_data["project_id"],
            value=json.dumps(signal_data, default=str),
            callback=_anomaly_delivery_report,
        )
        anomaly_producer.poll(0)

    detector = AnomalyDetector(aggregator=aggregator, publish_fn=publish_anomaly)
    detector.start()

    consumer = Consumer(consumer_config(KAFKA_CONSUMER_GROUP, {
        "session.timeout.ms": 30_000,
    }))
    consumer.subscribe([KAFKA_TOPIC_RAW])
    logger.info(f"Stream processor started | topic={KAFKA_TOPIC_RAW}")

    running = True

    def _shutdown(*_):
        nonlocal running
        logger.info("Shutdown signal received")
        running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        while running:
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    logger.error(f"Kafka error: {msg.error()}")
                continue

            _process_message(msg, aggregator, dlq_producer)
            consumer.commit(message=msg, asynchronous=False)

    except KafkaException as e:
        logger.error(f"Fatal Kafka error: {e}")
        sys.exit(1)
    finally:
        consumer.close()
        remaining = anomaly_producer.flush(timeout=10)
        if remaining:
            logger.warning(f"{remaining} anomaly messages not delivered on shutdown")
        dlq_producer.flush(timeout=5)
        logger.info("Stream processor stopped")


def _process_message(msg, aggregator, dlq_producer: Producer) -> None:
    raw = msg.value()

    try:
        raw_dict = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"Could not parse message: {e}")
        _send_to_dlq(dlq_producer, raw, str(e))
        return

    events = normalize(raw_dict)

    for event in events:
        if event.get("event_type") != "error":
            logger.debug(f"Skipping non-error event: {event.get('event_type')}")
            continue

        project_id = event.get("project_id")
        if not project_id:
            logger.warning("Event missing project_id — skipping")
            continue

        raw_ts = event.get("timestamp")
        try:
            event_ts = datetime.fromisoformat(raw_ts) if raw_ts else datetime.now(timezone.utc)
        except ValueError:
            event_ts = datetime.now(timezone.utc)

        aggregator.record_error(project_id=project_id, event_timestamp=event_ts)
        logger.info(
            f"Error recorded | project={project_id} "
            f"source={event.get('_source', 'native')} "
            f"ts={event_ts.strftime('%H:%M:%S')}"
        )


if __name__ == "__main__":
    main()
