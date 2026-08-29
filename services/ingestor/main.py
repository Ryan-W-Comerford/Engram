"""
Engram Ingestor — direct event ingest for the Python SDK.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional, Union

from fastapi import Body, Depends, FastAPI, Request
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import text
from sqlalchemy.orm import Session

from shared.db.models import Event, EventType
from shared.db.session import get_db
from shared.db.tenant import get_or_create_default_project
from shared.kafka_config import producer_config
from shared.logging_config import setup_logging
from auth import AUTH_TOKEN, require_token
from schemas import HealthResponse

setup_logging("ingestor")
logger = logging.getLogger(__name__)

KAFKA_TOPIC_RAW = os.getenv("KAFKA_TOPIC_RAW", "engram.events.raw")

if AUTH_TOKEN:
    logger.info("AUTH_TOKEN is set — write endpoints require a bearer token")
else:
    logger.info("AUTH_TOKEN is not set — ingest is open (self-hosted / trusted network)")

_producer = None

def get_producer():
    global _producer
    if _producer is None:
        from confluent_kafka import Producer
        _producer = Producer(producer_config("engram-ingestor"))
    return _producer


# ── Ingest schemas ─────────────────────────────────────────────────────────────

class ErrorEventData(BaseModel):
    exception_type: str                                  = Field(max_length=256)
    message:        str                                  = Field(max_length=1024)
    stack_trace:    Optional[str]                        = Field(default=None, max_length=8192)
    endpoint:       Optional[str]                        = Field(default=None, max_length=512)
    method:         Optional[str]                        = Field(default=None, max_length=16)

class TraceEventData(BaseModel):
    endpoint: str
    method: str
    status_code: int
    duration_ms: float

class ErrorEvent(BaseModel):
    event_type: Literal["error"]
    timestamp: datetime
    environment: str = "production"
    service: Optional[str] = None
    data: ErrorEventData

class TraceEvent(BaseModel):
    event_type: Literal["trace"]
    timestamp: datetime
    environment: str = "production"
    service: Optional[str] = None
    data: TraceEventData

IngestEventRequest = Annotated[
    Union[ErrorEvent, TraceEvent],
    Field(discriminator="event_type"),
]

class IngestResponse(BaseModel):
    event_id: str
    status: str = "received"


# ── Rate limiting (per client IP) ────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Engram Ingestor", version="0.4.0", docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ── Health ─────────────────────────────────────────────────────────────────────

def _check_kafka() -> str:
    try:
        from confluent_kafka.admin import AdminClient
        from shared.kafka_config import producer_config as _pcfg
        admin = AdminClient({**_pcfg("engram-ingestor-health"), "socket.timeout.ms": 2000})
        admin.list_topics(timeout=2)
        return "ok"
    except Exception as e:
        logger.warning(f"Kafka health check failed: {e}")
        return "error"


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/internal/health", response_model=HealthResponse, tags=["meta"])
def health_internal(
    db: Session = Depends(get_db),
    _: None = Depends(require_token),
) -> HealthResponse:
    db_status = "ok"
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        logger.error(f"DB health check failed: {e}")
        db_status = "error"
    return HealthResponse(status="ok", kafka=_check_kafka(), db=db_status)


# ── Ingest ─────────────────────────────────────────────────────────────────────

@app.post("/ingest", response_model=IngestResponse, tags=["events"])
@limiter.limit("200/minute")
def ingest_event(
    request: Request,
    body: Annotated[IngestEventRequest, Body(...)],
    _: None = Depends(require_token),
    db: Session = Depends(get_db),
) -> IngestResponse:
    project = get_or_create_default_project(db)
    event_id = uuid.uuid4()

    event = Event(
        id=event_id,
        project_id=project.id,
        event_type=EventType(body.event_type),
        environment=body.environment,
        service=body.service,
        timestamp=body.timestamp.replace(tzinfo=None) if body.timestamp.tzinfo else body.timestamp,
        raw_data=body.data.model_dump(),
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(event)
    db.commit()

    try:
        producer = get_producer()
        message = {
            "event_id":   str(event_id),
            "project_id": str(project.id),
            **body.model_dump(),
        }
        producer.produce(
            topic=KAFKA_TOPIC_RAW,
            key=str(project.id),
            value=json.dumps(message, default=str),
        )
        producer.poll(0)
    except Exception as e:
        logger.error(f"Kafka publish failed (event still saved to DB): {e}")

    logger.info(f"Ingested {body.event_type} event {event_id}")
    return IngestResponse(event_id=str(event_id))
