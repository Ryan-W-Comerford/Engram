"""
Engram Ingestor — admin API + direct event ingest for Python SDK.
"""

import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional, Union

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from sqlalchemy.orm import Session

from shared.db.models import Event, EventType, Project
from shared.db.session import get_db
from shared.kafka_config import producer_config
from shared.logging_config import setup_logging
from auth import generate_api_key, get_current_project, hash_api_key
from schemas import CreateProjectRequest, CreateProjectResponse, HealthResponse
from webhooks import router as webhooks_router

setup_logging("ingestor")
logger = logging.getLogger(__name__)

KAFKA_TOPIC_RAW = os.getenv("KAFKA_TOPIC_RAW", "engram.events.raw")
ADMIN_API_KEY   = os.getenv("ADMIN_API_KEY", "")

if not ADMIN_API_KEY:
    logger.warning("ADMIN_API_KEY is not set — POST /projects will return 503 until configured")

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

class DeploymentEventData(BaseModel):
    commit_sha: str
    branch: str
    pusher: Optional[str] = None
    commit_message: Optional[str] = None

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

class DeploymentEvent(BaseModel):
    event_type: Literal["deployment"]
    timestamp: datetime
    environment: str = "production"
    service: Optional[str] = None
    data: DeploymentEventData

IngestEventRequest = Annotated[
    Union[ErrorEvent, TraceEvent, DeploymentEvent],
    Field(discriminator="event_type"),
]

class IngestResponse(BaseModel):
    event_id: str
    status: str = "received"


# ── Auth ───────────────────────────────────────────────────────────────────────

def require_admin(x_admin_key: str = Header(..., alias="X-Admin-Key")) -> None:
    if not ADMIN_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ADMIN_API_KEY is not configured on this server",
        )
    if not hmac.compare_digest(x_admin_key, ADMIN_API_KEY):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin key")


# ── Rate limiting (per API key, falls back to IP) ─────────────────────────────

def _rate_limit_key(request: Request) -> str:
    return request.headers.get("X-API-Key") or (request.client.host if request.client else "unknown")

limiter = Limiter(key_func=_rate_limit_key)


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Engram Admin API", version="0.3.0", docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.include_router(webhooks_router)


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
    _: None = Depends(require_admin),
) -> HealthResponse:
    db_status = "ok"
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        logger.error(f"DB health check failed: {e}")
        db_status = "error"
    return HealthResponse(status="ok", kafka=_check_kafka(), db=db_status)


# ── Projects ───────────────────────────────────────────────────────────────────

@app.post("/projects", response_model=CreateProjectResponse, status_code=201, tags=["projects"])
def create_project(
    body: CreateProjectRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> CreateProjectResponse:
    api_key = generate_api_key()
    project = Project(
        id=uuid.uuid4(),
        name=body.name,
        api_key_hash=hash_api_key(api_key),
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    logger.info(f"Created project '{project.name}' id={project.id}")
    return CreateProjectResponse(
        project_id=str(project.id),
        name=project.name,
        api_key=api_key,
        created_at=project.created_at,
    )


# ── Ingest ─────────────────────────────────────────────────────────────────────

@app.post("/ingest", response_model=IngestResponse, tags=["events"])
@limiter.limit("200/minute")
def ingest_event(
    request: Request,
    body: Annotated[IngestEventRequest, Body(...)],
    project: Project = Depends(get_current_project),
    db: Session = Depends(get_db),
) -> IngestResponse:
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

    logger.info(f"Ingested {body.event_type} event {event_id} for project '{project.name}'")
    return IngestResponse(event_id=str(event_id))
