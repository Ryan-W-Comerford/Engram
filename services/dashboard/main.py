"""
Engram Dashboard — read-only FastAPI service that renders the dashboard UI.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Body, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from shared.db.models import DailyDigest, Event, EventType, Incident, IncidentStatus, Project, Severity
from shared.db.similarity import find_similar_incidents
from shared.db.session import get_db
from shared.logging_config import setup_logging
from auth import (
    NotAuthenticated,
    SECURE_COOKIES,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    get_session_project,
    hash_api_key,
    make_session_token,
)

setup_logging("dashboard")
logger = logging.getLogger(__name__)

app = FastAPI(title="Engram Dashboard", docs_url=None, redoc_url=None)

BASE_DIR      = os.path.dirname(__file__)
templates     = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
INGESTOR_HOST = os.getenv("INGESTOR_HOST", "http://localhost:8000")

_static_dir = os.path.join(BASE_DIR, "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# ── Auth exception handler ─────────────────────────────────────────────────────

@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "session expired"}, status_code=401)
    return RedirectResponse(url="/login", status_code=302)


# ── Login / logout ─────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: Optional[str] = Query(None)):
    return templates.TemplateResponse(request, "login.html", {"error": bool(error)})


@app.post("/login")
def login_post(
    request: Request,
    api_key: str = Form(...),
    db: Session = Depends(get_db),
):
    if not api_key or not api_key.strip():
        return RedirectResponse(url="/login?error=1", status_code=302)

    key_hash = hash_api_key(api_key.strip())
    project  = db.query(Project).filter(Project.api_key_hash == key_hash).first()

    if not project:
        logger.warning(f"Failed login attempt | ip={request.client.host if request.client else '?'}")
        return RedirectResponse(url="/login?error=1", status_code=302)

    token    = make_session_token(str(project.id))
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=SECURE_COOKIES,
    )
    logger.info(f"Login successful | project='{project.name}' id={project.id}")
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ── Health (no auth) ───────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    project: Project = Depends(get_session_project),
):
    since_24h = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    since_1h  = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)

    total_events_24h = (
        db.query(func.count(Event.id))
        .filter(Event.project_id == project.id, Event.timestamp >= since_24h)
        .scalar() or 0
    )

    errors_1h = (
        db.query(func.count(Event.id))
        .filter(
            Event.project_id == project.id,
            Event.event_type == EventType.ERROR,
            Event.timestamp >= since_1h,
        )
        .scalar() or 0
    )

    total_1h = (
        db.query(func.count(Event.id))
        .filter(Event.project_id == project.id, Event.timestamp >= since_1h)
        .scalar() or 1
    )

    error_rate = round((errors_1h / total_1h) * 100, 1)

    open_incidents = (
        db.query(func.count(Incident.id))
        .filter(
            Incident.project_id == project.id,
            Incident.status == IncidentStatus.OPEN,
        )
        .scalar() or 0
    )

    last_deployment = (
        db.query(Event)
        .filter(
            Event.project_id == project.id,
            Event.event_type == EventType.DEPLOYMENT,
        )
        .order_by(Event.timestamp.desc())
        .first()
    )

    recent_incidents = (
        db.query(Incident)
        .filter(Incident.project_id == project.id)
        .order_by(Incident.detected_at.desc())
        .limit(8)
        .all()
    )

    recent_events = (
        db.query(Event)
        .filter(
            Event.project_id == project.id,
            Event.event_type.in_([EventType.ERROR, EventType.TRACE]),
        )
        .order_by(Event.timestamp.desc())
        .limit(20)
        .all()
    )

    latest_digest = (
        db.query(DailyDigest)
        .filter(DailyDigest.project_id == project.id)
        .order_by(DailyDigest.date.desc())
        .first()
    )

    recent_deployments = (
        db.query(Event)
        .filter(Event.project_id == project.id, Event.event_type == EventType.DEPLOYMENT)
        .order_by(Event.timestamp.desc())
        .limit(3)
        .all()
    )

    return templates.TemplateResponse(request, "index.html", {
        "project": project,
        "stats": {
            "total_events_24h": total_events_24h,
            "error_rate": error_rate,
            "open_incidents": open_incidents,
            "last_deployment": last_deployment,
        },
        "recent_incidents":   recent_incidents,
        "recent_deployments": recent_deployments,
        "recent_events":      recent_events,
        "digest":             latest_digest,
        "page": "dashboard",
    })


@app.get("/incidents/{incident_id}", response_class=HTMLResponse)
def incident_detail(
    request: Request,
    incident_id: str,
    db: Session = Depends(get_db),
    project: Project = Depends(get_session_project),
):
    try:
        iid = uuid.UUID(incident_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid incident ID")

    incident = db.query(Incident).filter(Incident.id == iid).first()

    # Verify the incident belongs to the authenticated project
    if not incident or incident.project_id != project.id:
        raise HTTPException(status_code=404, detail="Incident not found")

    analysis = {}
    if incident.ai_summary:
        try:
            analysis = json.loads(incident.ai_summary)
        except (json.JSONDecodeError, TypeError):
            analysis = {}

    deployment_event = None
    if incident.deployment_event_id:
        deployment_event = (
            db.query(Event).filter(Event.id == incident.deployment_event_id).first()
        )

    since = incident.detected_at - timedelta(minutes=30)
    sample_errors = (
        db.query(Event)
        .filter(
            Event.project_id == incident.project_id,
            Event.event_type == EventType.ERROR,
            Event.timestamp >= since,
            Event.timestamp <= incident.detected_at + timedelta(minutes=5),
        )
        .order_by(Event.timestamp.desc())
        .limit(10)
        .all()
    )

    similar_incidents = []
    if incident.embedding is not None:
        emb = incident.embedding
        if hasattr(emb, "tolist"):
            emb = emb.tolist()
        similar_incidents = find_similar_incidents(
            db=db,
            project_id=str(incident.project_id),
            current_incident_id=str(incident.id),
            query_embedding=emb,
        )

    return templates.TemplateResponse(request, "incident.html", {
        "project":          project,
        "incident":         incident,
        "analysis":         analysis,
        "deployment_event": deployment_event,
        "sample_errors":    sample_errors,
        "similar_incidents":similar_incidents,
        "page": "incidents",
    })


@app.post("/api/incidents/{incident_id}/resolve")
def resolve_incident(
    incident_id: str,
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    project: Project = Depends(get_session_project),
):
    try:
        iid = uuid.UUID(incident_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid incident ID")

    incident = db.query(Incident).filter(Incident.id == iid).first()
    if not incident or incident.project_id != project.id:
        raise HTTPException(status_code=404, detail="Incident not found")

    if incident.status == IncidentStatus.RESOLVED:
        return {"status": "already_resolved"}

    incident.status = IncidentStatus.RESOLVED
    incident.resolved_at = datetime.now(timezone.utc).replace(tzinfo=None)
    incident.resolution_note = (body.get("resolution_note") or "").strip() or None
    db.commit()
    logger.info(f"Incident resolved | id={incident.id} project={project.name}")
    return {"status": "resolved"}


@app.get("/settings", response_class=HTMLResponse)
def settings(
    request: Request,
    db: Session = Depends(get_db),
    project: Project = Depends(get_session_project),
):
    webhook_url = f"{INGESTOR_HOST}/webhooks/github/{project.id}"
    return templates.TemplateResponse(request, "settings.html", {
        "project":       project,
        "webhook_url":   webhook_url,
        "ingestor_host": INGESTOR_HOST,
        "page": "settings",
    })


# ── Live events page ──────────────────────────────────────────────────────────

@app.get("/live", response_class=HTMLResponse)
def live_events(
    request: Request,
    project: Project = Depends(get_session_project),
):
    return templates.TemplateResponse(request, "live.html", {
        "project": project,
        "page":    "live",
    })


# ── Incidents list ────────────────────────────────────────────────────────────

@app.get("/incidents", response_class=HTMLResponse)
def incidents_list(
    request: Request,
    db: Session = Depends(get_db),
    project: Project = Depends(get_session_project),
    status: Optional[str] = Query(None),
):
    q = db.query(Incident).filter(Incident.project_id == project.id)
    if status:
        try:
            q = q.filter(Incident.status == IncidentStatus(status))
        except ValueError:
            pass
    incidents = q.order_by(Incident.detected_at.desc()).limit(200).all()
    return templates.TemplateResponse(request, "incidents.html", {
        "project":       project,
        "incidents":     incidents,
        "status_filter": status,
        "page":          "incidents",
    })


# ── Deployments list ───────────────────────────────────────────────────────────

@app.get("/deployments", response_class=HTMLResponse)
def deployments_list(
    request: Request,
    db: Session = Depends(get_db),
    project: Project = Depends(get_session_project),
):
    deploys = (
        db.query(Event)
        .filter(Event.project_id == project.id, Event.event_type == EventType.DEPLOYMENT)
        .order_by(Event.timestamp.desc())
        .limit(100)
        .all()
    )
    return templates.TemplateResponse(request, "deployments.html", {
        "project":     project,
        "deployments": deploys,
        "page":        "deployments",
    })


# ── Daily digest ───────────────────────────────────────────────────────────────

@app.get("/digest", response_class=HTMLResponse)
def digest(
    request: Request,
    db: Session = Depends(get_db),
    project: Project = Depends(get_session_project),
):
    recent = (
        db.query(DailyDigest)
        .filter(DailyDigest.project_id == project.id)
        .order_by(DailyDigest.date.desc())
        .limit(7)
        .all()
    )
    return templates.TemplateResponse(request, "digest.html", {
        "project": project,
        "digest":  recent[0] if recent else None,
        "history": recent[1:] if len(recent) > 1 else [],
        "page":    "digest",
    })


# ── JSON API (polled by dashboard JS every 10s) ───────────────────────────────

@app.get("/api/metrics")
def api_metrics(
    db: Session = Depends(get_db),
    project: Project = Depends(get_session_project),
):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0, tzinfo=None)
    since = now - timedelta(minutes=30)

    rows = db.execute(text("""
        SELECT
            to_char(date_trunc('minute', timestamp), 'HH24:MI') AS bucket,
            event_type,
            COUNT(*) AS cnt
        FROM events
        WHERE project_id = :pid
          AND timestamp > :since
        GROUP BY bucket, event_type
        ORDER BY bucket
    """), {"pid": str(project.id), "since": since}).fetchall()

    labels = [
        (now - timedelta(minutes=i)).strftime("%H:%M")
        for i in range(29, -1, -1)
    ]
    errors_map: dict[str, int] = {l: 0 for l in labels}
    traces_map: dict[str, int] = {l: 0 for l in labels}

    for bucket, event_type, cnt in rows:
        if bucket in errors_map:
            if event_type == "error":
                errors_map[bucket] = cnt
            elif event_type == "trace":
                traces_map[bucket] = cnt

    dep_rows = db.execute(text("""
        SELECT to_char(date_trunc('minute', timestamp), 'HH24:MI') AS bucket
        FROM events
        WHERE project_id = :pid
          AND event_type = 'deployment'
          AND timestamp > :since
    """), {"pid": str(project.id), "since": since}).fetchall()

    return {
        "labels":      labels,
        "errors":      [errors_map[l] for l in labels],
        "traces":      [traces_map[l] for l in labels],
        "deployments": list({r[0] for r in dep_rows}),
    }


@app.get("/api/events/stream")
def api_events_stream(
    db: Session = Depends(get_db),
    project: Project = Depends(get_session_project),
    event_type: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
):
    q = db.query(Event).filter(Event.project_id == project.id)
    if event_type:
        try:
            q = q.filter(Event.event_type == EventType(event_type))
        except ValueError:
            pass
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            q = q.filter(Event.timestamp > since_dt)
        except ValueError:
            pass
    events = q.order_by(Event.timestamp.desc()).limit(limit).all()
    return {
        "events": [
            {
                "id":          str(e.id),
                "type":        e.event_type.value,
                "service":     e.service or "—",
                "environment": e.environment,
                "timestamp":   e.timestamp.isoformat(),
                "ts_display":  e.timestamp.strftime("%H:%M:%S"),
                "raw_data":    e.raw_data,
            }
            for e in events
        ]
    }


@app.post("/api/test/trigger-incident")
def api_trigger_test_incident(
    db: Session = Depends(get_db),
    project: Project = Depends(get_session_project),
):
    """
    Directly creates a test incident in the DB without going through Kafka.
    Useful for verifying the UI when the Kafka pipeline is unavailable.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    incident = Incident(
        id=uuid.uuid4(),
        project_id=project.id,
        detected_at=now,
        current_errors=25,
        baseline_avg=4.0,
        spike_ratio=6.25,
        status=IncidentStatus.OPEN,
        severity=Severity.HIGH,
        title="[Test] Error spike detected — DatabaseTimeoutError (6.25× baseline)",
        ai_summary=None,
        ai_analyzed=False,
        created_at=now,
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)
    logger.info(f"Test incident created | id={incident.id} project={project.name}")
    return {"incident_id": str(incident.id), "status": "created"}


@app.get("/api/events/recent")
def api_events_recent(
    db: Session = Depends(get_db),
    project: Project = Depends(get_session_project),
):
    events = (
        db.query(Event)
        .filter(
            Event.project_id == project.id,
            Event.event_type.in_([EventType.ERROR, EventType.TRACE]),
        )
        .order_by(Event.timestamp.desc())
        .limit(20)
        .all()
    )

    return {
        "events": [
            {
                "id":          str(e.id),
                "type":        e.event_type.value,
                "service":     e.service or "—",
                "environment": e.environment,
                "timestamp":   e.timestamp.strftime("%H:%M:%S"),
                "detail": (
                    e.raw_data.get("exception_type", "Error")
                    if e.event_type == EventType.ERROR
                    else f"{e.raw_data.get('method','GET')} {e.raw_data.get('endpoint','/')} {e.raw_data.get('status_code','')}"
                ),
            }
            for e in events
        ]
    }
