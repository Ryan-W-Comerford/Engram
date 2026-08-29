"""
tests/test_ingestor_api.py

FastAPI TestClient smoke tests for services/ingestor/main.py.

The DB and Kafka producer are replaced with lightweight mocks so these tests
run without a running Docker stack. They exercise the routing, auth, and
request-validation layers of the ingestor.
"""

import importlib.util
import os
import sys
import uuid
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

# conftest.py has already:
#   • Mocked confluent_kafka, opentelemetry, psycopg2 in sys.modules
#   • Added services/ingestor to sys.path
#   • Ensured AUTH_TOKEN is unset — the suite runs the default "open" config

# Load via spec so the module is registered under a collision-free name
# instead of the generic 'main' (which might already be cached in sys.modules).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "ingestor_main",
    os.path.join(ROOT, "services", "ingestor", "main.py"),
)
ingestor_main = importlib.util.module_from_spec(_spec)
sys.modules["ingestor_main"] = ingestor_main
_spec.loader.exec_module(ingestor_main)

from shared.db.session import get_db

# Engram is single-tenant and (in the test env) open — AUTH_TOKEN is unset, so
# no token headers are needed on any endpoint.

# ── Fixtures ───────────────────────────────────────────────────────────────────

def _make_mock_project():
    p = MagicMock()
    p.id         = uuid.UUID("00000000-0000-0000-0000-000000000001")
    p.name       = "default"
    p.created_at = datetime(2026, 1, 1, 0, 0, 0)
    return p


def _make_session(project=None):
    """Return a mock SQLAlchemy session pre-wired for the default-project lookup."""
    session  = MagicMock()
    returned = project or _make_mock_project()

    # get_or_create_default_project() does query().filter().first()
    session.query.return_value.filter.return_value.first.return_value = returned
    session.refresh.side_effect = lambda obj: None

    return session


@pytest.fixture
def client_auth():
    """Client whose mock DB returns the default project."""
    mock_sess = _make_session()

    def override():
        yield mock_sess

    ingestor_main.app.dependency_overrides[get_db] = override
    with TestClient(ingestor_main.app, raise_server_exceptions=True) as c:
        yield c
    ingestor_main.app.dependency_overrides.clear()


# ── GET /health ────────────────────────────────────────────────────────────────

def test_health_returns_ok(client_auth):
    """Public /health is an unauthenticated liveness probe — status only, no internals."""
    resp = client_auth.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok"}
    # Dependency details must NOT leak from the public endpoint.
    assert "kafka" not in body
    assert "db" not in body


def test_internal_health_reports_dependencies(client_auth):
    """AUTH_TOKEN is unset in tests, so /internal/health is open."""
    resp = client_auth.get("/internal/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # kafka will show "error" (mocked confluent_kafka raises on
    # AdminClient().list_topics()), which is the correct graceful-degradation path
    assert "kafka" in body
    assert "db" in body


def test_no_projects_endpoint(client_auth):
    """Project management is gone — /projects must 404."""
    resp = client_auth.post("/projects", json={"name": "my-app"})
    assert resp.status_code == 404


# ── POST /ingest (open — no token in test env) ───────────────────────────────

def _error_payload():
    return {
        "event_type": "error",
        "timestamp": "2026-01-01T12:00:00+00:00",
        "environment": "production",
        "service": "my-api",
        "data": {
            "exception_type": "ValueError",
            "message": "something went wrong",
        },
    }


def test_ingest_valid_error_event(client_auth):
    resp = client_auth.post("/ingest", json=_error_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert "event_id" in body
    assert body["status"] == "received"
    # Confirm it's a valid UUID
    uuid.UUID(body["event_id"])


def test_ingest_valid_trace_event(client_auth):
    resp = client_auth.post(
        "/ingest",
        json={
            "event_type": "trace",
            "timestamp": "2026-01-01T12:00:00+00:00",
            "data": {
                "endpoint": "/api/users",
                "method": "GET",
                "status_code": 200,
                "duration_ms": 45.0,
            },
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "received"


def test_ingest_invalid_event_type_rejected(client_auth):
    """An unknown event_type should fail Pydantic validation (422)."""
    resp = client_auth.post(
        "/ingest",
        json={"event_type": "typo", "timestamp": "2026-01-01T00:00:00Z", "data": {}},
    )
    assert resp.status_code == 422
