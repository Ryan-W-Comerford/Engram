"""
shared/logging_config.py

Call setup_logging(service_name) once at the top of each service's main.py
to replace basicConfig with a structured JSON formatter. Every log record
emitted by any logger in the process will include a `service` field, making
it easy to filter in Datadog, CloudWatch, or any log aggregator.

Output format (one JSON object per line):
  {
    "timestamp": "2026-06-14T12:00:00.123Z",
    "level": "INFO",
    "logger": "pulseai.ingestor",
    "service": "ingestor",
    "message": "Ingested error event abc-123"
  }
"""

import logging
import os

from pythonjsonlogger import jsonlogger


class _ServiceFilter(logging.Filter):
    """Injects `service` into every log record so it appears in JSON output."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service = service_name

    def filter(self, record: logging.LogRecord) -> bool:
        record.service = self._service
        return True


def setup_logging(service_name: str) -> None:
    """
    Configure the root logger with a JSON formatter.

    Args:
        service_name: Short identifier for this service (e.g. "ingestor").
                      Injected as the `service` field in every log record.
    """
    level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_str, logging.INFO)

    handler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(service)s %(message)s",
        rename_fields={
            "asctime":   "timestamp",
            "levelname": "level",
            "name":      "logger",
        },
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler.setFormatter(formatter)
    handler.addFilter(_ServiceFilter(service_name))

    root = logging.getLogger()
    root.handlers = []
    root.addHandler(handler)
    root.setLevel(level)
