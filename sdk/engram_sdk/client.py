"""
PulseAI SDK client.

Usage (FastAPI):
    from engram_sdk import PulseAI

    pulse = Engram(api_key="pk_live_...", host="https://ingest.pulseai.dev")
    pulse.auto_instrument(app)   # wraps your FastAPI/Flask app automatically

Usage (manual):
    pulse.capture_error(exception_type="ValueError", message="bad input")
    pulse.capture_trace(endpoint="/api/orders", method="POST", status_code=200, duration_ms=94.3)
"""

import atexit
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from .middleware import EngramFastAPIMiddleware, EngramFlaskMiddleware
from .transport import Transport

logger = logging.getLogger(__name__)


class Engram:
    """
    Main PulseAI client. Instantiate once per application, then call
    auto_instrument(app) to enable automatic event capture.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        host: str = "http://localhost:8000",
        environment: str = "production",
        service: Optional[str] = None,
    ) -> None:
        """
        Args:
            api_key:     Your project API key (pk_live_...).
                         Falls back to ENGRAM_API_KEY env var if not provided.
            host:        URL of your Engram ingestor.
                         Falls back to ENGRAM_HOST env var, then localhost:8000.
            environment: Logical environment tag ("production", "staging", "dev").
            service:     Optional service name tag ("api-gateway", "worker", etc).
        """
        resolved_key = api_key or os.getenv("ENGRAM_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Engram API key is required. Pass api_key=... or set ENGRAM_API_KEY."
            )

        resolved_host = host or os.getenv("ENGRAM_HOST", "http://localhost:8000")

        self._environment = environment
        self._service = service
        self._transport = Transport(api_key=resolved_key, host=resolved_host)

        # Flush remaining queue items gracefully when the process exits
        atexit.register(self._transport.shutdown)

        logger.info(
            f"Engram initialized | host={resolved_host} "
            f"env={environment} service={service}"
        )

    # ── Auto-instrumentation ───────────────────────────────────────────────────

    def auto_instrument(self, app) -> None:
        """
        Attach PulseAI middleware to a FastAPI or Flask application.
        Detects the framework automatically.

        Call this once after creating your app:
            app = FastAPI()
            pulse.auto_instrument(app)
        """
        app_type = type(app).__module__

        if "fastapi" in app_type or "starlette" in app_type:
            app.add_middleware(EngramFastAPIMiddleware, pulse=self)
            logger.info("PulseAI FastAPI middleware attached")
        elif "flask" in app_type:
            EngramFlaskMiddleware(app, pulse=self)
            logger.info("PulseAI Flask middleware attached")
        else:
            raise TypeError(
                f"Unsupported framework: {app_type}. "
                "Engram supports FastAPI and Flask. Use capture_error() and "
                "capture_trace() for manual instrumentation."
            )

    # ── Manual capture helpers ─────────────────────────────────────────────────

    def capture_error(
        self,
        exception_type: str,
        message: str,
        stack_trace: Optional[str] = None,
        endpoint: Optional[str] = None,
        method: Optional[str] = None,
    ) -> None:
        """
        Manually capture an error event. Useful for caught exceptions you still
        want to track:

            try:
                result = call_external_api()
            except TimeoutError as e:
                pulse.capture_error(
                    exception_type="TimeoutError",
                    message=str(e),
                    endpoint="/api/payments",
                    method="POST",
                )
        """
        self._transport.enqueue({
            "event_type": "error",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "environment": self._environment,
            "service": self._service,
            "data": {
                "exception_type": exception_type,
                "message": message,
                "stack_trace": stack_trace,
                "endpoint": endpoint,
                "method": method,
            },
        })

    def capture_trace(
        self,
        endpoint: str,
        method: str,
        status_code: int,
        duration_ms: float,
    ) -> None:
        """Manually capture a request trace event."""
        self._transport.enqueue({
            "event_type": "trace",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "environment": self._environment,
            "service": self._service,
            "data": {
                "endpoint": endpoint,
                "method": method,
                "status_code": status_code,
                "duration_ms": duration_ms,
            },
        })

    def capture_deployment(
        self,
        commit_sha: str,
        branch: str,
        pusher: Optional[str] = None,
        commit_message: Optional[str] = None,
    ) -> None:
        """
        Manually record a deployment event. Alternative to the GitHub webhook
        when you want to trigger this from within your own CI/CD pipeline.
        """
        self._transport.enqueue({
            "event_type": "deployment",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "environment": self._environment,
            "service": self._service,
            "data": {
                "commit_sha": commit_sha,
                "branch": branch,
                "pusher": pusher,
                "commit_message": commit_message,
            },
        })
