"""
Incident writer — Option C: incident memory.

Processing order for each anomaly signal:
  1.  Create incident row immediately (never lose an anomaly)
  2.  Fetch recent errors from DB
  3.  Generate a query embedding from the error context
  4.  Search for similar past incidents using pgvector cosine similarity
  5.  Call Claude — with similar incidents injected into the prompt
  6.  Store Claude's analysis + embedding on the incident row

The embedding is generated BEFORE the full Claude call so we can include
similar incident context IN the prompt. Claude then knows "this looks like
something you've seen before" and can reference it directly.
"""

import json
import os
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from shared.db.models import Event, EventType, Incident, IncidentStatus, Severity
from shared.db.session import SessionLocal
from shared.db.similarity import find_similar_incidents, format_similar_for_prompt
from analyzer import analyze_incident
from embedder import embed_incident, build_embedding_text, generate_embedding
from alerting import send_incident_alert

logger = logging.getLogger(__name__)

CONTEXT_WINDOW_MINUTES = 30
DASHBOARD_BASE_URL = os.getenv("DASHBOARD_BASE_URL", "http://localhost:8080")


def _fetch_error_events(db: Session, project_id: str, since: datetime) -> list[Event]:
    return (
        db.query(Event)
        .filter(
            Event.project_id == uuid.UUID(project_id),
            Event.event_type == EventType.ERROR,
            Event.timestamp >= since,
        )
        .order_by(Event.timestamp.desc())
        .limit(50)
        .all()
    )


def _map_severity(severity_str: Optional[str]) -> Optional[Severity]:
    if not severity_str:
        return None
    try:
        return Severity(severity_str.lower())
    except ValueError:
        return Severity.MEDIUM


def _extract_exception_types(error_events: list[Event]) -> list[str]:
    """Pull unique exception types from recent error events for the query embedding."""
    seen = set()
    for e in error_events:
        exc = (e.raw_data or {}).get("exception_type")
        if exc:
            seen.add(exc)
    return sorted(seen)


def _extract_endpoints(error_events: list[Event]) -> list[str]:
    seen = set()
    for e in error_events:
        ep = (e.raw_data or {}).get("endpoint")
        if ep:
            seen.add(ep)
    return sorted(seen)


def process_anomaly_signal(signal: dict) -> None:
    """
    Main entry point. Creates, enriches, and embeds an incident record
    for every anomaly signal consumed from Kafka.
    """
    project_id = signal.get("project_id")
    if not project_id:
        logger.error("Anomaly signal missing project_id — skipping")
        return

    detected_at_raw = signal.get("detected_at", datetime.now(timezone.utc).isoformat())
    try:
        detected_at = datetime.fromisoformat(detected_at_raw)
        if detected_at.tzinfo is not None:
            detected_at = detected_at.replace(tzinfo=None)
    except (ValueError, TypeError):
        detected_at = datetime.now(timezone.utc).replace(tzinfo=None)

    since = detected_at - timedelta(minutes=CONTEXT_WINDOW_MINUTES)

    db = SessionLocal()
    incident_persisted = False
    try:
        # ── Step 1: Create incident immediately ────────────────────────────────
        # If this commit fails (e.g. DB down), we re-raise so the caller can
        # route the signal to the DLQ — the anomaly must not be silently lost.
        incident = Incident(
            id=uuid.uuid4(),
            project_id=uuid.UUID(project_id),
            detected_at=detected_at,
            current_errors=int(signal.get("current_errors", 0)),
            baseline_avg=float(signal.get("baseline_avg", 0)),
            spike_ratio=float(signal.get("spike_ratio", 0)),
            status=IncidentStatus.INVESTIGATING,
            ai_analyzed=False,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(incident)
        db.commit()
        db.refresh(incident)
        incident_persisted = True
        logger.info(f"Incident created | id={incident.id}")

        # ── Step 2: Fetch error context ───────────────────────────────────────
        error_events = _fetch_error_events(db, project_id, since)
        logger.info(f"Context | errors={len(error_events)}")

        # ── Step 3: Generate query embedding from current error context ─────────
        # We embed a description of the current incident's symptoms BEFORE
        # calling Claude so we can search history and include it in the prompt.
        exc_types = _extract_exception_types(error_events)
        endpoints = _extract_endpoints(error_events)

        query_text = build_embedding_text(
            title              = f"Anomaly: {signal.get('spike_ratio')}x error spike",
            root_cause         = f"Error types: {', '.join(exc_types) or 'unknown'}",
            exception_types    = exc_types,
            affected_endpoints = endpoints,
        )
        query_embedding = generate_embedding(query_text)

        # ── Step 4: Similarity search against past incidents ────────────────────
        similar = []
        if query_embedding:
            similar = find_similar_incidents(
                db=db,
                project_id=project_id,
                current_incident_id=str(incident.id),
                query_embedding=query_embedding,
            )
            if similar:
                logger.info(
                    f"Similar incidents found | count={len(similar)} "
                    f"top_score={similar[0].similarity_score:.0%} "
                    f"top_title={similar[0].title!r}"
                )
            else:
                logger.info("No similar past incidents found")

        similar_text = format_similar_for_prompt(similar)

        # ── Step 5: Call Claude with full context including history ─────────────
        analysis = analyze_incident(
            anomaly_signal=signal,
            error_events=error_events,
            similar_incidents_text=similar_text,
        )

        # ── Step 6: Enrich incident with Claude's analysis + embedding ──────────
        if analysis:
            incident.title              = analysis.get("title", "Untitled incident")[:500]
            incident.severity           = _map_severity(analysis.get("severity"))
            incident.ai_summary         = json.dumps(analysis)
            incident.affected_endpoints = analysis.get("affected_endpoints", [])
            incident.recommended_actions= analysis.get("recommended_actions", [])
            incident.ai_analyzed        = True

            # Generate the final embedding from Claude's authoritative analysis
            # (more accurate than the pre-query embedding which was just symptoms)
            final_embedding = embed_incident(analysis)
            if final_embedding:
                incident.embedding = final_embedding
                logger.info(f"Embedding stored | incident={incident.id}")
            else:
                logger.warning(f"Embedding failed — incident {incident.id} won't appear in future similarity searches")

            recurrence = analysis.get("recurrence_note")
            logger.info(
                f"Incident enriched | id={incident.id} title={incident.title!r} "
                f"severity={incident.severity} similar_found={len(similar)} "
                f"recurrence={recurrence!r}"
            )
        else:
            incident.title     = f"Anomaly — {signal.get('current_errors')} errors ({signal.get('spike_ratio')}× baseline)"
            incident.severity  = Severity.HIGH
            incident.ai_analyzed = False
            logger.warning(f"Incident {incident.id} created without AI analysis")

        # INVESTIGATING → OPEN: AI analysis complete (or confirmed failed).
        # ai_analyzed=False distinguishes the "failed" case for the UI.
        incident.status = IncidentStatus.OPEN
        db.commit()
        logger.info(f"Incident {incident.id} finalised")

        # ── Step 7: Fire alerts ─────────────────────────────────────────────
        # Called after commit so the dashboard URL is already resolvable.
        # Failures here are logged but never raise — the incident is safe in DB.
        if analysis:
            send_incident_alert(
                incident_id        = str(incident.id),
                title              = incident.title or "Anomaly detected",
                severity           = incident.severity.value if incident.severity else None,
                root_cause         = analysis.get("root_cause_hypothesis", ""),
                recommended_actions= analysis.get("recommended_actions", []),
                spike_ratio        = str(signal.get("spike_ratio", "?")),
                recurrence_note    = analysis.get("recurrence_note"),
                dashboard_base_url = DASHBOARD_BASE_URL,
            )

    except Exception as e:
        db.rollback()
        if not incident_persisted:
            # Incident row was never written — re-raise so the caller can DLQ the signal
            raise
        logger.error(f"Failed to enrich incident: {e}", exc_info=True)
    finally:
        db.close()
