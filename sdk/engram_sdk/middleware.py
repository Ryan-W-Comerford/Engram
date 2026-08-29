"""
Middleware for automatic event capture.

FastAPI  → use EngramFastAPIMiddleware (Starlette BaseHTTPMiddleware)
Flask    → use EngramFlaskMiddleware
Both capture trace events on every request and error events on exceptions,
then hand them off to the Transport queue (non-blocking).
"""

import sys
import traceback
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import PulseAI


# ── FastAPI / Starlette ────────────────────────────────────────────────────────

try:
    import time
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response

    class EngramFastAPIMiddleware(BaseHTTPMiddleware):
        """
        Add to your FastAPI app:

            from engram_sdk import PulseAI
            pulse = Engram(host="http://localhost:8000")
            pulse.auto_instrument(app)   # ← one line

        Every request generates a trace event. Unhandled exceptions also
        generate an error event with the full stack trace attached.
        """

        def __init__(self, app, pulse: "Engram") -> None:
            super().__init__(app)
            self._pulse = pulse

        async def dispatch(self, request: Request, call_next) -> Response:
            start = time.perf_counter()
            exc_info = None

            try:
                response = await call_next(request)
            except Exception:
                exc_info = sys.exc_info()
                # Re-raise so FastAPI's own error handling still fires
                raise
            finally:
                duration_ms = (time.perf_counter() - start) * 1000
                endpoint = request.url.path
                method = request.method

                if exc_info and exc_info[0] is not None:
                    exc_type, exc_value, tb = exc_info
                    self._pulse.capture_error(
                        exception_type=exc_type.__name__,
                        message=str(exc_value),
                        stack_trace="".join(traceback.format_tb(tb)),
                        endpoint=endpoint,
                        method=method,
                    )
                else:
                    status_code = response.status_code
                    # Capture 5xx responses as errors too — they indicate server faults
                    if status_code >= 500:
                        self._pulse.capture_error(
                            exception_type="HTTPServerError",
                            message=f"{method} {endpoint} returned {status_code}",
                            endpoint=endpoint,
                            method=method,
                        )

                    self._pulse.capture_trace(
                        endpoint=endpoint,
                        method=method,
                        status_code=status_code,
                        duration_ms=round(duration_ms, 2),
                    )

            return response

except ImportError:
    # Starlette not installed — FastAPI middleware unavailable
    class EngramFastAPIMiddleware:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise ImportError(
                "starlette is required for FastAPI instrumentation. "
                "pip install engram-sdk"
            )


# ── Flask ──────────────────────────────────────────────────────────────────────

try:
    import time as _time
    import flask as _flask

    class EngramFlaskMiddleware:
        """
        Add to your Flask app:

            from engram_sdk import PulseAI
            pulse = Engram(host="http://localhost:8000")
            pulse.auto_instrument(app)   # ← one line
        """

        def __init__(self, app: "_flask.Flask", pulse: "Engram") -> None:
            self._pulse = pulse
            self._register(app)

        def _register(self, app: "_flask.Flask") -> None:
            app.before_request(self._before_request)
            app.after_request(self._after_request)
            app.teardown_request(self._teardown_request)

        def _before_request(self) -> None:
            _flask.g._engram_start = _time.perf_counter()

        def _after_request(self, response: "_flask.Response") -> "_flask.Response":
            start = getattr(_flask.g, "_engram_start", None)
            duration_ms = (((_time.perf_counter() - start) * 1000) if start else 0)

            endpoint = _flask.request.path
            method = _flask.request.method
            status_code = response.status_code

            if status_code >= 500:
                self._pulse.capture_error(
                    exception_type="HTTPServerError",
                    message=f"{method} {endpoint} returned {status_code}",
                    endpoint=endpoint,
                    method=method,
                )

            self._pulse.capture_trace(
                endpoint=endpoint,
                method=method,
                status_code=status_code,
                duration_ms=round(duration_ms, 2),
            )
            return response

        def _teardown_request(self, exc: Exception | None) -> None:
            if exc is not None:
                tb = traceback.format_exc()
                self._pulse.capture_error(
                    exception_type=type(exc).__name__,
                    message=str(exc),
                    stack_trace=tb,
                    endpoint=_flask.request.path,
                    method=_flask.request.method,
                )

except ImportError:
    class EngramFlaskMiddleware:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise ImportError(
                "flask is required for Flask instrumentation. "
                "pip install 'engram-sdk[flask]'"
            )
