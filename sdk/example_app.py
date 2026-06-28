"""
Test app for validating Phase 2 end-to-end.

Run this after `docker compose up` is healthy:
    pip install -e ./sdk
    ENGRAM_API_KEY=pk_live_... python sdk/example_app.py

Then in a second terminal, hammer the error endpoint to trigger the anomaly
detector (fires when errors spike to 1.5× baseline within a 1-minute window):
    for i in $(seq 1 120); do curl -s http://localhost:9000/explode > /dev/null; done
"""

import os
import random
import time

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from engram_sdk import Engram

app = FastAPI(title="Engram Example App")

pulse = Engram(
    api_key=os.environ["ENGRAM_API_KEY"],
    host=os.getenv("ENGRAM_HOST", "http://localhost:8000"),
    environment="production",
    service="example-api",
)
pulse.auto_instrument(app)


@app.get("/ok")
def ok():
    """Healthy endpoint — generates trace events with 200 status."""
    time.sleep(random.uniform(0.01, 0.05))
    return {"status": "ok"}


@app.get("/slow")
def slow():
    """Slow endpoint — generates trace events with high duration_ms."""
    time.sleep(random.uniform(0.5, 2.0))
    return {"status": "slow but ok"}


@app.get("/flaky")
def flaky():
    """Occasionally fails — mix of 200 and 500 trace events."""
    if random.random() < 0.3:
        return JSONResponse(status_code=500, content={"error": "random failure"})
    return {"status": "ok"}


@app.get("/explode")
def explode():
    """Always raises — use this to spike error rate and trigger the anomaly detector."""
    raise ValueError("Simulated unhandled exception for anomaly testing")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000, log_level="warning")
