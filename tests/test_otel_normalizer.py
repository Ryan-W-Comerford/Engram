"""
tests/test_otel_normalizer.py

Unit tests for shared/otel_normalizer.py — the component that converts raw
OTel-encoded Kafka messages into Engram's internal event schema.

All tests are pure Python; no DB, Kafka, or running services required.
"""

import uuid
from datetime import datetime, timezone

import pytest

from shared.otel_normalizer import _attr, _ns_to_iso, normalize

# ── _ns_to_iso ─────────────────────────────────────────────────────────────────

def test_ns_to_iso_none_returns_now():
    """None input should fall back to current UTC time (not crash)."""
    result = _ns_to_iso(None)
    dt = datetime.fromisoformat(result)
    assert dt.tzinfo is not None
    # Should be within the last minute
    delta = (datetime.now(timezone.utc) - dt).total_seconds()
    assert abs(delta) < 60


def test_ns_to_iso_zero_returns_now():
    """Zero (invalid OTel timestamp) should fall back to current time."""
    result = _ns_to_iso(0)
    dt = datetime.fromisoformat(result)
    delta = (datetime.now(timezone.utc) - dt).total_seconds()
    assert abs(delta) < 60


def test_ns_to_iso_negative_returns_now():
    """Negative nanosecond value should fall back to current time."""
    result = _ns_to_iso(-1)
    dt = datetime.fromisoformat(result)
    delta = (datetime.now(timezone.utc) - dt).total_seconds()
    assert abs(delta) < 60


def test_ns_to_iso_valid_timestamp():
    """1_000_000_000 ns = Unix epoch second 1 = 1970-01-01T00:00:01Z."""
    result = _ns_to_iso(1_000_000_000)
    assert result.startswith("1970-01-01T00:00:01")


def test_ns_to_iso_large_timestamp():
    """A realistic 2026 timestamp converts correctly."""
    # 2026-01-01T00:00:00Z in nanoseconds
    ns = 1_767_225_600 * 1_000_000_000
    result = _ns_to_iso(ns)
    assert "2026-01-01" in result


# ── _attr ──────────────────────────────────────────────────────────────────────

def test_attr_string_value():
    attrs = [{"key": "service.name", "value": {"stringValue": "my-api"}}]
    assert _attr(attrs, "service.name") == "my-api"


def test_attr_int_value_coerced_to_str():
    attrs = [{"key": "http.status_code", "value": {"intValue": 500}}]
    assert _attr(attrs, "http.status_code") == "500"


def test_attr_missing_key_returns_none():
    attrs = [{"key": "service.name", "value": {"stringValue": "api"}}]
    assert _attr(attrs, "missing.key") is None


def test_attr_empty_list_returns_none():
    assert _attr([], "any.key") is None


def test_attr_none_list_returns_none():
    assert _attr(None, "any.key") is None


# ── normalize — native Engram format ──────────────────────────────────────────

def test_normalize_native_error_passthrough():
    """Native Engram error events should pass through unchanged (with _source added)."""
    payload = {
        "event_type": "error",
        "project_id": "proj-123",
        "timestamp": "2026-01-01T12:00:00+00:00",
        "environment": "production",
        "service": "my-api",
        "data": {"exception_type": "ValueError", "message": "bad input"},
    }
    events = normalize(payload)
    assert len(events) == 1
    e = events[0]
    assert e["event_type"] == "error"
    assert e["project_id"] == "proj-123"
    assert e["environment"] == "production"
    assert e["_source"] == "native"
    assert e["data"]["exception_type"] == "ValueError"


def test_normalize_native_trace_passthrough():
    payload = {
        "event_type": "trace",
        "project_id": "proj-456",
        "timestamp": "2026-01-01T12:00:00+00:00",
        "data": {"endpoint": "/api/users", "method": "GET", "status_code": 200, "duration_ms": 42.5},
    }
    events = normalize(payload)
    assert len(events) == 1
    assert events[0]["event_type"] == "trace"
    assert events[0]["_source"] == "native"


def test_normalize_native_assigns_event_id_if_missing():
    payload = {
        "event_type": "error",
        "project_id": "proj-abc",
        "data": {},
    }
    events = normalize(payload)
    assert len(events) == 1
    assert uuid.UUID(events[0]["event_id"])  # valid UUID


def test_normalize_native_defaults_environment_to_production():
    payload = {
        "event_type": "error",
        "project_id": "proj-abc",
        "data": {},
    }
    events = normalize(payload)
    assert events[0]["environment"] == "production"


# ── normalize — OTel span format ──────────────────────────────────────────────

def _make_span(
    project_id="test-proj",
    start_ns=1_767_225_600_000_000_000,
    end_ns=1_767_225_600_001_000_000,
    status_code=0,
    extra_attrs=None,
    span_name="GET /api/users",
):
    attrs = [
        {"key": "engram.project_id", "value": {"stringValue": project_id}},
        {"key": "http.route", "value": {"stringValue": "/api/users"}},
        {"key": "http.request.method", "value": {"stringValue": "GET"}},
        {"key": "http.response.status_code", "value": {"intValue": 200}},
        {"key": "service.name", "value": {"stringValue": "my-api"}},
        {"key": "deployment.environment", "value": {"stringValue": "staging"}},
    ]
    if extra_attrs:
        attrs.extend(extra_attrs)
    return {
        "name": span_name,
        "startTimeUnixNano": start_ns,
        "endTimeUnixNano": end_ns,
        "status": {"code": status_code},
        "attributes": attrs,
    }


def _wrap_span(span):
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [{"spans": [span]}],
            }
        ]
    }


def test_normalize_otel_trace_span():
    start = 1_767_225_600_000_000_000
    end   = start + 50_000_000  # 50 ms
    payload = _wrap_span(_make_span(start_ns=start, end_ns=end))
    events = normalize(payload)

    assert len(events) == 1
    e = events[0]
    assert e["event_type"] == "trace"
    assert e["project_id"] == "test-proj"
    assert e["environment"] == "staging"
    assert e["service"] == "my-api"
    assert e["data"]["duration_ms"] == pytest.approx(50.0)
    assert e["data"]["method"] == "GET"
    assert e["_source"] == "otel"


def test_normalize_otel_error_span():
    error_attrs = [
        {"key": "exception.type", "value": {"stringValue": "ValueError"}},
        {"key": "exception.message", "value": {"stringValue": "bad value"}},
    ]
    span = _make_span(status_code=2, extra_attrs=error_attrs)
    payload = _wrap_span(span)
    events = normalize(payload)

    assert len(events) == 1
    e = events[0]
    assert e["event_type"] == "error"
    assert e["data"]["exception_type"] == "ValueError"
    assert e["data"]["message"] == "bad value"
    assert "duration_ms" not in e["data"]


def test_normalize_otel_span_without_project_id_uses_default():
    """Engram is single-tenant: spans with no engram.project_id go to the default project."""
    span = {
        "name": "GET /health",
        "startTimeUnixNano": 1_000_000_000_000_000_000,
        "endTimeUnixNano":   1_000_000_000_100_000_000,
        "status": {"code": 0},
        "attributes": [
            {"key": "service.name", "value": {"stringValue": "api"}}
            # no engram.project_id
        ],
    }
    payload = _wrap_span(span)
    events = normalize(payload)
    assert len(events) == 1
    assert events[0]["project_id"] == "00000000-0000-0000-0000-000000000001"


# ── normalize — OTel log format ───────────────────────────────────────────────

def _make_log(severity_number=9, body_str="info message", project_id="log-proj"):
    attrs = [{"key": "engram.project_id", "value": {"stringValue": project_id}}]
    return {
        "timeUnixNano": 1_767_225_600_000_000_000,
        "severityNumber": severity_number,
        "severityText": "INFO",
        "body": {"stringValue": body_str},
        "attributes": attrs,
    }


def _wrap_log(log_record, service="svc"):
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service}}
                    ]
                },
                "scopeLogs": [{"logRecords": [log_record]}],
            }
        ]
    }


def test_normalize_otel_log_info():
    payload = _wrap_log(_make_log(severity_number=9))
    events = normalize(payload)
    assert len(events) == 1
    assert events[0]["event_type"] == "trace"


def test_normalize_otel_log_error_severity():
    """Severity >= 17 (OTel ERROR) maps to event_type='error'."""
    payload = _wrap_log(_make_log(severity_number=17, body_str="Something blew up"))
    events = normalize(payload)
    assert len(events) == 1
    assert events[0]["event_type"] == "error"
    assert events[0]["data"]["message"] == "Something blew up"


def test_normalize_empty_payload():
    assert normalize({}) == []


def test_normalize_project_id_falls_back_to_resource_attrs():
    """If span has no engram.project_id attr, it should be read from resource."""
    span = {
        "name": "GET /",
        "startTimeUnixNano": 1_000_000_000_000_000_000,
        "endTimeUnixNano":   1_000_000_001_000_000_000,
        "status": {"code": 0},
        "attributes": [],  # no project_id here
    }
    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "engram.project_id", "value": {"stringValue": "from-resource"}}
                    ]
                },
                "scopeSpans": [{"spans": [span]}],
            }
        ]
    }
    events = normalize(payload)
    assert len(events) == 1
    assert events[0]["project_id"] == "from-resource"
