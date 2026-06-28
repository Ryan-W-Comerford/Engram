"""
tests/conftest.py

Shared setup for the Engram test suite.

All module-level code here runs before any test file is imported, so:
  • Heavy Docker-only packages (confluent_kafka, opentelemetry, etc.) are
    replaced with MagicMock stubs so service modules can be imported.
  • Service directories are added to sys.path so bare-module imports work
    (e.g. `from aggregator import SlidingWindowAggregator` in anomaly.py).
  • Test-only environment variables are set before any module reads them.
"""

import os
import sys
from unittest.mock import MagicMock

# ── 1. Stub out packages only available inside Docker containers ───────────────
_STUB_MODULES = [
    # PostgreSQL driver — SQLAlchemy imports it at create_engine() time.
    # Mocking lets session.py initialize without a real Postgres installation.
    # The engine object is never used directly because tests override get_db.
    "psycopg2",
    "psycopg2.extensions",
    "psycopg2.extras",
    # Kafka client (requires native .so, not available locally)
    "confluent_kafka",
    "confluent_kafka.admin",
    # OpenTelemetry (pulled by services/ingestor/webhooks.py)
    "opentelemetry",
    "opentelemetry.trace",
    "opentelemetry.sdk",
    "opentelemetry.sdk.trace",
    "opentelemetry.sdk.trace.export",
    "opentelemetry.sdk.resources",
    "opentelemetry.exporter",
    "opentelemetry.exporter.otlp",
    "opentelemetry.exporter.otlp.proto",
    "opentelemetry.exporter.otlp.proto.grpc",
    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
    # AI API clients (pulled by services/ai/*)
    "anthropic",
    "openai",
    # Redis client (pulled by services/processor/aggregator.py's make_aggregator())
    "redis",
]
for _mod in _STUB_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# ── 2. Silence the JSON logging setup that main.py runs at import time ─────────
# setup_logging() removes root handlers — that breaks pytest's log capturing.
# Patch it to a no-op BEFORE any service main.py is imported.
import shared.logging_config  # noqa: E402  (must come after sys.path insertion below...
                               # but shared/ is at root, which is always in sys.path)
shared.logging_config.setup_logging = lambda service_name: None

# ── 3. Add service directories to sys.path ────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _add(subpath: str) -> None:
    p = os.path.join(ROOT, subpath)
    if p not in sys.path:
        sys.path.insert(0, p)

_add("")                    # project root  →  `from shared.xxx import ...`
_add("services/ingestor")   # `from auth import ...`, `import main`
_add("services/processor")  # `from aggregator import ...`, `from anomaly import ...`

# ── 4. Test-only environment variables ────────────────────────────────────────
# Force these values regardless of what the shell has set so tests are
# deterministic. ADMIN_API_KEY and DASHBOARD_SECRET_KEY are read at import
# time by main.py and auth.py respectively.
os.environ["ADMIN_API_KEY"]        = "test-admin-key-for-unit-tests"
os.environ["DASHBOARD_SECRET_KEY"] = "test-dashboard-secret-key-32ch"
# DATABASE_URL is read by shared/db/session.py at import time.
# Any postgresql:// URL works because psycopg2 is mocked above; no actual
# connection is ever made (tests override get_db with a mock session).
os.environ["DATABASE_URL"] = "postgresql://test:test@localhost:5432/test"
