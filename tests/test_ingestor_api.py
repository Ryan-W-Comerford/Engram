"""
tests/test_ingestor_api.py

FastAPI TestClient smoke tests for services/ingestor/main.py.

The DB and Kafka producer are replaced with lightweight mocks so these tests
run without a running Docker stack. They exercise the routing, auth, and
request-validation layers of the ingestor.
"""

import hashlib
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
#   • Set os.environ["ADMIN_API_KEY"] = "test-admin-key-for-unit-tests"

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

ADMIN_KEY    = "test-admin-key-for-unit-tests"
TEST_API_KEY = "pk_live_smoke_test_key_abcdef0123456789"
TEST_KEY_HASH = hashlib.sha256(TEST_API_KEY.encode()).hexdigest()


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _make_mock_project():
    p = MagicMock()
    p.id          = uuid.UUID("abb7108b-375e-43a2-8015-7875d25f5f3f")
    p.name        = "smoke-test-project"
    p.api_key_hash = TEST_KEY_HASH
    p.created_at  = datetime(2026, 1, 1, 0, 0, 0)
    return p


def _make_session(project=None):
    """Return a mock SQLAlchemy session pre-wired for auth + CRUD."""
    session  = MagicMock()
    returned = project or _make_mock_project()

    # project lookup (get_current_project and create_project both call query().filter().first())
    session.query.return_value.filter.return_value.first.return_value = returned

    # db.refresh() sets any ORM-populated defaults; all fields are already set
    # in the constructor, so a no-op is fine
    session.refresh.side_effect = lambda obj: None

    return session


@pytest.fixture
def client_auth():
    """Client whose mock DB returns a valid project for the test API key."""
    mock_sess = _make_session()

    def override():
        yield mock_sess

    ingestor_main.app.dependency_overrides[get_db] = override
    with TestClient(ingestor_main.app, raise_server_exceptions=True) as c:
        yield c
    ingestor_main.app.dependency_overrides.clear()


@pytest.fixture
def client_no_project():
    """Client whose mock DB returns None — simulates an unknown API key."""
    mock_sess = _make_session(project=None)
    mock_sess.query.return_value.filter.return_value.first.return_value = None

    def override():
        yield mock_sess

    ingestor_main.app.dependency_overrides[get_db] = override
    with TestClient(ingestor_main.app, raise_server_exceptions=False) as c:
        yield c
    ingestor_main.app.dependency_overrides.clear()


# ── GET /health ────────────────────────────────────────────────────────────────

def test_health_returns_ok(client_auth):
    resp = client_auth.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # kafka will show "error" (mocked confluent_kafka raises AttributeError on
    # AdminClient().list_topics()), which is the correct graceful-degradation path
    assert "kafka" in body
    assert "db" in body


# ── POST /projects (admin auth) ────────────────────────────────────────────────

def test_create_project_missing_admin_key(client_auth):
    """Omitting X-Admin-Key header should return 422 (missing required header)."""
    resp = client_auth.post("/projects", json={"name": "new-project"})
    assert resp.status_code == 422


def test_create_project_wrong_admin_key(client_auth):
    resp = client_auth.post(
        "/projects",
        json={"name": "new-project"},
        headers={"X-Admin-Key": "wrong-key"},
    )
    assert resp.status_code == 401


def test_create_project_success(client_auth):
    resp = client_auth.post(
        "/projects",
        json={"name": "my-app"},
        headers={"X-Admin-Key": ADMIN_KEY},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "api_key" in body
    assert body["api_key"].startswith("pk_live_")
    assert "project_id" in body
    assert body["name"] == "my-app"  # echoes the requested name back


# ── POST /ingest (API key auth) ────────────────────────────────────────────────

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


def test_ingest_missing_api_key(client_auth):
    """Omitting X-API-Key header → 422 (missing required header)."""
    resp = client_auth.post("/ingest", json=_error_payload())
    assert resp.status_code == 422


def test_ingest_invalid_api_key(client_no_project):
    """An API key that doesn't match any project → 401."""
    resp = client_no_project.post(
        "/ingest",
        json=_error_payload(),
        headers={"X-API-Key": "pk_live_definitely_not_valid"},
    )
    assert resp.status_code == 401


def test_ingest_valid_error_event(client_auth):
    resp = client_auth.post(
        "/ingest",
        json=_error_payload(),
        headers={"X-API-Key": TEST_API_KEY},
    )
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
        headers={"X-API-Key": TEST_API_KEY},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "received"


def test_ingest_invalid_event_type_rejected(client_auth):
    """An unknown event_type should fail Pydantic validation (422)."""
    resp = client_auth.post(
        "/ingest",
        json={"event_type": "typo", "timestamp": "2026-01-01T00:00:00Z", "data": {}},
        headers={"X-API-Key": TEST_API_KEY},
    )
    assert resp.status_code == 422
