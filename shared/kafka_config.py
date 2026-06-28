"""
shared/kafka_config.py

Central Kafka config builder. All producers and consumers call
producer_config() / consumer_config() instead of hand-rolling dicts.

Auth modes
----------
PLAINTEXT (default)
    Local docker-compose Kafka. No credentials needed.

SASL_SSL
    Managed Kafka — Confluent Cloud, AWS MSK, Upstash Kafka.
    Set KAFKA_SECURITY_PROTOCOL=SASL_SSL plus credentials.
    Confluent Cloud example:
        KAFKA_BOOTSTRAP_SERVERS=pkc-xxx.us-east-1.aws.confluent.cloud:9092
        KAFKA_SECURITY_PROTOCOL=SASL_SSL
        KAFKA_SASL_MECHANISM=PLAIN
        KAFKA_SASL_USERNAME=<API key>
        KAFKA_SASL_PASSWORD=<API secret>
"""

import os

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
_SECURITY_PROTOCOL      = os.getenv("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT")
_SASL_MECHANISM         = os.getenv("KAFKA_SASL_MECHANISM", "PLAIN")
_SASL_USERNAME          = os.getenv("KAFKA_SASL_USERNAME", "")
_SASL_PASSWORD          = os.getenv("KAFKA_SASL_PASSWORD", "")


def _auth_config() -> dict:
    if _SECURITY_PROTOCOL == "PLAINTEXT":
        return {}
    config: dict = {
        "security.protocol": _SECURITY_PROTOCOL,
        "sasl.mechanism":    _SASL_MECHANISM,
        "sasl.username":     _SASL_USERNAME,
        "sasl.password":     _SASL_PASSWORD,
    }
    if _SECURITY_PROTOCOL in ("SASL_SSL", "SSL"):
        # Standard CA bundle location on Debian/Ubuntu-based images
        config["ssl.ca.location"] = "/etc/ssl/certs/ca-certificates.crt"
    return config


def producer_config(client_id: str, extra: dict | None = None) -> dict:
    """Build a confluent-kafka Producer config dict."""
    config = {
        "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
        "client.id":         client_id,
        "acks":              "all",
        **_auth_config(),
    }
    if extra:
        config.update(extra)
    return config


def consumer_config(group_id: str, extra: dict | None = None) -> dict:
    """Build a confluent-kafka Consumer config dict."""
    config = {
        "bootstrap.servers":  KAFKA_BOOTSTRAP_SERVERS,
        "group.id":           group_id,
        "auto.offset.reset":  "earliest",
        "enable.auto.commit": False,
        **_auth_config(),
    }
    if extra:
        config.update(extra)
    return config
