"""
Transport layer — sends events to the Engram ingestor over HTTP.

Uses a background daemon thread with a queue so that every .capture_*() call
returns immediately and never adds latency to the application's request cycle.
If the ingestor is unreachable the event is dropped and a warning is logged —
observability should never take down the app it's observing.
"""

import logging
import queue
import threading
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_STOP_SENTINEL = object()  # signal to shut down the background thread


class Transport:
    def __init__(
        self,
        api_key: str,
        host: str,
        timeout: float = 3.0,
        queue_size: int = 1000,
    ) -> None:
        self._api_key = api_key
        self._host = host.rstrip("/")
        self._timeout = timeout
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)

        self._thread = threading.Thread(
            target=self._worker,
            name="engram-transport",
            daemon=True,  # exits automatically when the main process exits
        )
        self._thread.start()
        logger.debug(f"Engram transport started → {self._host}")

    def enqueue(self, payload: dict) -> None:
        """
        Add an event to the outbound queue. Non-blocking — if the queue is
        full (burst of 1000+ undelivered events) the event is silently dropped
        to avoid any back-pressure on the application.
        """
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            logger.warning("Engram transport queue full — event dropped")

    def _worker(self) -> None:
        """Background thread: drains the queue and POSTs each event."""
        with httpx.Client(
            headers={"X-API-Key": self._api_key, "Content-Type": "application/json"},
            timeout=self._timeout,
        ) as client:
            while True:
                payload = self._queue.get()
                if payload is _STOP_SENTINEL:
                    break
                self._send(client, payload)

    def _send(self, client: httpx.Client, payload: dict) -> None:
        try:
            response = client.post(f"{self._host}/ingest", json=payload)
            response.raise_for_status()
            logger.debug(f"Engram event sent: {payload.get('event_type')} → {response.status_code}")
        except httpx.TimeoutException:
            logger.warning(f"Engram ingestor timed out — event dropped")
        except httpx.HTTPStatusError as e:
            logger.warning(f"Engram ingestor returned {e.response.status_code} — event dropped")
        except Exception as e:
            logger.warning(f"Engram transport error: {e} — event dropped")

    def shutdown(self, wait: bool = True) -> None:
        """Drain the queue then stop the background thread."""
        self._queue.put(_STOP_SENTINEL)
        if wait:
            self._thread.join(timeout=5)
