"""
Daily digest generator.

Queries yesterday's telemetry for a project, asks Claude to summarise the
day's health, and persists the result to `daily_digests`.  Idempotent — if
a digest already exists for the requested date it is a no-op.
"""

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone

import anthropic
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from shared.db.models import DailyDigest, Event, Incident, Project
from shared.db.session import SessionLocal

logger = logging.getLogger(__name__)

MODEL   = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    return _client


def _collect_stats(db: Session, project_id, target_date: date) -> dict:
    day_start = datetime.combine(target_date, datetime.min.time())
    day_end   = day_start + timedelta(days=1)

    # Event counts grouped by type
    type_counts = (
        db.query(Event.event_type, func.count(Event.id))
        .filter(
            Event.project_id == project_id,
            Event.timestamp  >= day_start,
            Event.timestamp  <  day_end,
        )
        .group_by(Event.event_type)
        .all()
    )
    counts = {row[0].value: row[1] for row in type_counts}

    total_errors = counts.get("error", 0)
    total_traces = counts.get("trace", 0)
    total        = total_errors + total_traces
    error_rate   = round(total_errors / max(total, 1) * 100, 1)

    # Top exception types — aggregated in SQL to avoid loading all JSONB blobs into memory
    exc_rows = db.execute(text("""
        SELECT raw_data->>'exception_type' AS exc_type, COUNT(*) AS cnt
        FROM events
        WHERE project_id  = :pid
          AND event_type  = 'error'
          AND timestamp  >= :day_start
          AND timestamp   < :day_end
          AND raw_data->>'exception_type' IS NOT NULL
        GROUP BY exc_type
        ORDER BY cnt DESC
        LIMIT 5
    """), {"pid": str(project_id), "day_start": day_start, "day_end": day_end}).fetchall()
    top_errors = [{"type": row[0], "count": row[1]} for row in exc_rows]

    # Incidents triggered on this day
    incidents = (
        db.query(Incident)
        .filter(
            Incident.project_id == project_id,
            Incident.detected_at >= day_start,
            Incident.detected_at <  day_end,
        )
        .all()
    )
    incident_summaries = [
        {
            "title":    inc.title or "Untitled incident",
            "severity": inc.severity.value if inc.severity else "unknown",
            "status":   inc.status.value,
        }
        for inc in incidents
    ]

    return {
        "total_events":   total,
        "error_count":    total_errors,
        "trace_count":    total_traces,
        "incident_count": len(incidents),
        "error_rate":     error_rate,
        "top_errors":     top_errors,
        "incidents":      incident_summaries,
    }


def _call_claude(project_name: str, stats: dict, prev_stats: dict | None) -> dict:
    lines = [
        f"You are an observability assistant writing a daily health summary for the engineering team of '{project_name}'.",
        "",
        f"Stats for the day:",
        f"  Total events : {stats['total_events']}",
        f"  Errors       : {stats['error_count']} ({stats['error_rate']}% of traffic)",
        f"  Traces       : {stats['trace_count']}",
        f"  Incidents    : {stats['incident_count']}",
    ]

    if stats["top_errors"]:
        lines.append("\nTop error types:")
        for e in stats["top_errors"]:
            lines.append(f"  - {e['type']}: {e['count']} occurrences")

    if stats["incidents"]:
        lines.append("\nIncidents triggered:")
        for i in stats["incidents"]:
            lines.append(f"  - [{i['severity'].upper()}] {i['title']} — {i['status']}")

    if prev_stats:
        lines.append(
            f"\nYesterday for comparison: "
            f"{prev_stats['error_count']} errors ({prev_stats['error_rate']}%), "
            f"{prev_stats['incident_count']} incidents"
        )

    if stats["total_events"] == 0:
        lines.append("\nNo events were recorded on this day (service may have been down or idle).")

    lines.append("""
Return ONLY a JSON object — no markdown, no preamble:
{
  "summary":        "<2-3 sentence plain-English summary of the day>",
  "health_score":   <integer 0-100; 100 = perfect; deduct for errors, incidents, regressions vs yesterday>,
  "highlights":     ["<positive observation>"],
  "concerns":       ["<issue or regression worth attention>"],
  "recommendation": "<single most important action item, or null if none>"
}

Keep highlights and concerns to max 3 each. Be concise and direct.
""")

    response = _get_client().messages.create(
        model=MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": "\n".join(lines)}],
    )
    raw = response.content[0].text.strip()
    # Strip accidental markdown fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def generate_digest_for_project(project_id, project_name: str, target_date: date) -> None:
    db = SessionLocal()
    try:
        existing = (
            db.query(DailyDigest)
            .filter(DailyDigest.project_id == project_id, DailyDigest.date == target_date)
            .first()
        )
        if existing:
            return

        stats = _collect_stats(db, project_id, target_date)

        # Pull previous day's stored data for comparison
        prev_date  = target_date - timedelta(days=1)
        prev_entry = (
            db.query(DailyDigest)
            .filter(DailyDigest.project_id == project_id, DailyDigest.date == prev_date)
            .first()
        )
        prev_stats = prev_entry.data if prev_entry else None

        try:
            ai_result = _call_claude(project_name, stats, prev_stats)
        except Exception as e:
            logger.warning(f"Claude digest failed for '{project_name}': {e} — storing stats only")
            ai_result = {
                "summary":        None,
                "health_score":   None,
                "highlights":     [],
                "concerns":       [],
                "recommendation": None,
            }

        digest = DailyDigest(
            project_id=project_id,
            date=target_date,
            summary=ai_result.get("summary"),
            data={**stats, **ai_result},
        )
        db.add(digest)
        db.commit()
        logger.info(
            f"Daily digest generated | project='{project_name}' "
            f"date={target_date} score={ai_result.get('health_score')}"
        )

    except Exception as e:
        logger.error(f"Digest generation failed for project {project_id}: {e}")
        db.rollback()
    finally:
        db.close()


def run_daily_digests() -> None:
    """Generate yesterday's digest for every project that doesn't have one yet."""
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    db = SessionLocal()
    try:
        projects = db.query(Project).all()
    finally:
        db.close()

    for project in projects:
        generate_digest_for_project(project.id, project.name, yesterday)
