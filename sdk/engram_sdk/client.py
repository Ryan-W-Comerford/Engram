"""
Engram SDK client.

Usage (FastAPI):
    from engram_sdk import Engram

    engram = Engram(host="http://localhost:8000")   # add token=... if the
    engram.auto_instrument(app)                      # instance sets AUTH_TOKEN

Usage (manual):
    engram.capture_error(exception_type="ValueError", message="bad input")
    engram.capture_trace(endpoint="/api/orders", method="POST", status_code=200, duration_ms=94.3)
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
    Main Engram client. Instantiate once per application, then call
    auto_instrument(app) to enable automatic event capture.
    """

    def __init__(
        self,
        host: str = "http://localhost:8000",
        token: Optional[str] = None,
        environment: str = "production",
        service: Optional[str] = None,
    ) -> None:
        """
        Args:
            host:        URL of your Engram ingestor.
                         Falls back to ENGRAM_HOST env var, then localhost:8000.
            token:       Only needed if the Engram instance sets AUTH_TOKEN.
                         Falls back to ENGRAM_TOKEN, then AUTH_TOKEN env var.
            environment: Logical environment tag ("production", "staging", "dev").
            service:     Optional service name tag ("api-gateway", "worker", etc).
        """
        resolved_host = host or os.getenv("ENGRAM_HOST", "http://localhost:8000")
        resolved_token = token or os.getenv("ENGRAM_TOKEN") or os.getenv("AUTH_TOKEN")

        self._environment = environment
        self._service = service
        self._transport = Transport(host=resolved_host, token=resolved_token)

        # Flush remaining queue items gracefully when the process exits
        atexit.register(self._transport.shutdown)

        logger.info(
            f"Engram initialized | host={resolved_host} "
            f"env={environment} service={service} auth={'on' if resolved_token else 'off'}"
        )

    # ── Auto-instrumentation ───────────────────────────────────────────────────

    def auto_instrument(self, app) -> None:
        """
        Attach Engram middleware to a FastAPI or Flask application.
        Detects the framework automatically.

        Call this once after creating your app:
            app = FastAPI()
            engram.auto_instrument(app)
        """
        app_type = type(app).__module__

        if "fastapi" in app_type or "starlette" in app_type:
            app.add_middleware(EngramFastAPIMiddleware, engram=self)
            logger.info("Engram FastAPI middleware attached")
        elif "flask" in app_type:
            EngramFlaskMiddleware(app, engram=self)
            logger.info("Engram Flask middleware attached")
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
                engram.capture_error(
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

