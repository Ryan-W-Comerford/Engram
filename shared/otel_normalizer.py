"""
shared/otel_normalizer.py

Translates OTel-encoded JSON messages (written by the collector's kafkaexporter)
into Engram's internal event dict schema before the stream processor or AI
service consumes them.

OTel JSON from the kafkaexporter wraps spans inside:
  { "resourceSpans": [ { "resource": {...}, "scopeSpans": [ { "spans": [...] } ] } ] }

And logs inside:
  { "resourceLogs": [ { "resource": {...}, "scopeLogs": [ { "logRecords": [...] } ] } ] }

Engram internal event schema (what the rest of the pipeline expects):
  {
    "event_id":    str,
    "project_id":  str,
    "event_type":  "error" | "trace" | "deployment",
    "timestamp":   ISO-8601 str,
    "environment": str,
    "service":     str | None,
    "data": {
      # for error: exception_type, message, stack_trace, endpoint, method
      # for trace: endpoint, method, status_code, duration_ms
    }
  }

This module is intentionally pure (no DB or Kafka deps) so it's testable
with plain dicts.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_NS_PER_MS = 1_000_000


def _ns_to_iso(ns: Optional[int]) -> str:
    """Convert OTel nanosecond epoch timestamp to ISO-8601 string."""
    if ns is None or ns <= 0:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat()


def _attr(attributes: list[dict], key: str) -> Optional[str]:
    """Extract a string value from an OTel attribute list by key."""
    for attr in attributes or []:
        if attr.get("key") == key:
            val = attr.get("value", {})
            # OTel attribute values are typed objects: {"stringValue": "..."}
            return (
                val.get("stringValue")
                or str(val.get("intValue", ""))
                or str(val.get("doubleValue", ""))
                or None
            )
    return None


def _resource_attr(resource: dict, key: str) -> Optional[str]:
    return _attr(resource.get("attributes", []), key)


def _span_to_event(span: dict, resource: dict) -> Optional[dict]:
    """Convert a single OTel span into a Engram event dict, or None if unroutable."""
    attrs = span.get("attributes", [])

    project_id = _attr(attrs, "engram.project_id") or _resource_attr(resource, "engram.project_id")
    if not project_id:
        logger.debug("Span missing engram.project_id — skipping")
        return None

    timestamp   = _ns_to_iso(span.get("startTimeUnixNano"))
    service     = _attr(attrs, "service.name") or _resource_attr(resource, "service.name")
    environment = _attr(attrs, "deployment.environment") or _resource_attr(resource, "deployment.environment") or "production"

    # ── Deployment span (emitted by the GitHub webhook handler) ───────────────
    # Detected by the engram.event_type attribute set in webhooks.py.
    # Span name "deployment" is a secondary signal for forward compatibility.
    pulseai_event_type = _attr(attrs, "engram.event_type")
    is_deployment = pulseai_event_type == "deployment" or span.get("name") == "deployment"

    if is_deployment:
        return {
            "event_id":    str(uuid.uuid4()),
            "project_id":  project_id,
            "event_type":  "deployment",
            "timestamp":   timestamp,
            "environment": environment,
            "service":     service,
            "data": {
                "commit_sha":      _attr(attrs, "deployment.commit_sha"),
                "branch":          _attr(attrs, "deployment.branch"),
                "pusher":          _attr(attrs, "deployment.pusher"),
                "commit_message":  _attr(attrs, "deployment.commit_message"),
                "commits_count":   int(_attr(attrs, "deployment.commits_count") or 0),
                "repository":      _attr(attrs, "deployment.repository"),
            },
            "_source": "otel",
        }

    # ── Error / trace span ────────────────────────────────────────────────────
    # OTel status code 2 = ERROR
    status_code_otel = span.get("status", {}).get("code", 0)
    is_error = status_code_otel == 2

    duration_ns = (span.get("endTimeUnixNano") or 0) - (span.get("startTimeUnixNano") or 0)
    duration_ms = round(duration_ns / _NS_PER_MS, 2)

    endpoint    = _attr(attrs, "http.route") or _attr(attrs, "url.path") or span.get("name", "/")
    method      = _attr(attrs, "http.request.method") or _attr(attrs, "http.method") or "UNKNOWN"
    http_status = int(_attr(attrs, "http.response.status_code") or _attr(attrs, "http.status_code") or 0)

    if is_error:
        data = {
            "exception_type": _attr(attrs, "exception.type") or "SpanError",
            "message":        _attr(attrs, "exception.message") or span.get("status", {}).get("message", ""),
            "stack_trace":    _attr(attrs, "exception.stacktrace"),
            "endpoint":       endpoint,
            "method":         method,
        }
    else:
        data = {
            "endpoint":    endpoint,
            "method":      method,
            "status_code": http_status or 200,
            "duration_ms": duration_ms,
        }

    return {
        "event_id":   str(uuid.uuid4()),
        "project_id": project_id,
        "event_type": "error" if is_error else "trace",
        "timestamp":  timestamp,
        "environment": environment,
        "service":    service,
        "data":       data,
        "_source":    "otel",   # internal marker — not stored in DB
    }


def _log_to_event(log_record: dict, resource: dict) -> Optional[dict]:
    """Convert an OTel LogRecord into a Engram event dict, or None if unroutable."""
    attrs = log_record.get("attributes", [])

    project_id = _attr(attrs, "engram.project_id") or _resource_attr(resource, "engram.project_id")
    if not project_id:
        logger.debug("LogRecord missing engram.project_id — skipping")
        return None

    # Severity number >= 17 means ERROR or above in OTel
    severity_number = log_record.get("severityNumber", 0)
    is_error = severity_number >= 17

    timestamp   = _ns_to_iso(log_record.get("timeUnixNano"))
    service     = _resource_attr(resource, "service.name")
    environment = _resource_attr(resource, "deployment.environment") or "production"

    # Log body is an AnyValue object
    body_val = log_record.get("body", {})
    body_str = body_val.get("stringValue", "") if isinstance(body_val, dict) else str(body_val)

    if is_error:
        data = {
            "exception_type": _attr(attrs, "exception.type") or log_record.get("severityText", "ERROR"),
            "message":        _attr(attrs, "exception.message") or body_str,
            "stack_trace":    _attr(attrs, "exception.stacktrace"),
            "endpoint":       _attr(attrs, "http.route") or _attr(attrs, "url.path"),
            "method":         _attr(attrs, "http.method"),
        }
    else:
        data = {
            "endpoint":    _attr(attrs, "http.route") or _attr(attrs, "url.path") or "/",
            "method":      _attr(attrs, "http.method") or "UNKNOWN",
            "status_code": int(_attr(attrs, "http.status_code") or 0),
            "duration_ms": 0.0,
        }

    return {
        "event_id":    str(uuid.uuid4()),
        "project_id":  project_id,
        "event_type":  "error" if is_error else "trace",
        "timestamp":   timestamp,
        "environment": environment,
        "service":     service,
        "data":        data,
        "_source":     "otel",
    }


def normalize(raw_payload: dict) -> list[dict]:
    """
    Accepts a message from engram.events.raw and returns a list of Engram
    internal event dicts.

    Two formats are accepted:
      - Native Engram JSON (from /ingest endpoint via the Python SDK)
      - OTel JSON (from the OTel Collector's kafkaexporter)
    """
    events = []

    # Native Engram format: { event_type, project_id, timestamp, data, ... }
    # Published by the ingestor's /ingest endpoint.
    if "event_type" in raw_payload and "project_id" in raw_payload:
        # Normalise timestamp — Pydantic serialises datetime via default=str
        raw_ts = raw_payload.get("timestamp")
        if isinstance(raw_ts, str):
            ts_str = raw_ts
        else:
            ts_str = datetime.now(timezone.utc).isoformat()

        events.append({
            "event_id":    raw_payload.get("event_id", str(uuid.uuid4())),
            "project_id":  raw_payload["project_id"],
            "event_type":  raw_payload["event_type"],
            "timestamp":   ts_str,
            "environment": raw_payload.get("environment", "production"),
            "service":     raw_payload.get("service"),
            "data":        raw_payload.get("data", {}),
            "_source":     "native",
        })
        return events

    # OTel trace payload (spans — includes deployment spans from webhooks)
    for resource_span in raw_payload.get("resourceSpans", []):
        resource = resource_span.get("resource", {})
        for scope_span in resource_span.get("scopeSpans", []):
            for span in scope_span.get("spans", []):
                event = _span_to_event(span, resource)
                if event:
                    events.append(event)

    # OTel log payload (structured logs from any language)
    for resource_log in raw_payload.get("resourceLogs", []):
        resource = resource_log.get("resource", {})
        for scope_log in resource_log.get("scopeLogs", []):
            for log_record in scope_log.get("logRecords", []):
                event = _log_to_event(log_record, resource)
                if event:
                    events.append(event)

    if not events and raw_payload:
        logger.debug(f"No events extracted from payload keys={list(raw_payload.keys())}")

    return events
