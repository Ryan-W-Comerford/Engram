"""
shared/db/similarity.py

Vector similarity search against historical incidents using pgvector.

Uses cosine distance (<=> operator) which measures the angle between
embedding vectors — ideal for semantic similarity where magnitude doesn't
matter, only direction.

A similarity score of 1.0 = identical meaning.
Threshold of 0.80 in practice catches genuinely related incidents while
filtering noise. Tune downward (e.g. 0.75) if you want broader matches.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.80   # minimum cosine similarity to be considered "similar"
MAX_SIMILAR_RESULTS  = 3      # how many past incidents to surface per new incident


@dataclass
class SimilarIncident:
    """A resolved past incident that semantically resembles the current one."""
    id:                str
    title:             str
    detected_at:       datetime
    severity:          Optional[str]
    root_cause:        str
    recommended_actions: list[str]
    similarity_score:  float          # 0.0 – 1.0, higher = more similar


def find_similar_incidents(
    db: Session,
    project_id: str,
    current_incident_id: str,
    query_embedding: list[float],
) -> list[SimilarIncident]:
    """
    Find past incidents whose embedding is semantically close to query_embedding.

    Only searches incidents that:
      - Belong to the same project
      - Have a stored embedding (ai_analyzed = 'true')
      - Are not the current incident itself
      - Meet the similarity threshold

    Returns up to MAX_SIMILAR_RESULTS results, ordered by similarity descending.
    """
    if not query_embedding:
        return []

    # pgvector cosine distance: 0 = identical, 2 = opposite
    # similarity = 1 - distance
    sql = text("""
        SELECT
            id::text,
            title,
            detected_at,
            severity,
            ai_summary,
            recommended_actions,
            1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
        FROM incidents
        WHERE project_id   = :project_id
          AND id           != :exclude_id
          AND ai_analyzed  = true
          AND embedding    IS NOT NULL
          AND 1 - (embedding <=> CAST(:embedding AS vector)) >= :threshold
        ORDER BY embedding <=> CAST(:embedding AS vector)
        LIMIT :limit
    """)

    try:
        rows = db.execute(sql, {
            "embedding":   str(query_embedding),
            "project_id":  project_id,
            "exclude_id":  current_incident_id,
            "threshold":   SIMILARITY_THRESHOLD,
            "limit":       MAX_SIMILAR_RESULTS,
        }).fetchall()
    except Exception as e:
        logger.error(f"Similarity search failed: {e}")
        return []

    results = []
    for row in rows:
        try:
            summary = json.loads(row.ai_summary) if row.ai_summary else {}
        except (json.JSONDecodeError, TypeError):
            summary = {}

        results.append(SimilarIncident(
            id               = row.id,
            title            = row.title or "Untitled incident",
            detected_at      = row.detected_at,
            severity         = row.severity,
            root_cause       = summary.get("root_cause_hypothesis", "No root cause recorded."),
            recommended_actions = row.recommended_actions or [],
            similarity_score = round(float(row.similarity), 3),
        ))

    logger.info(
        f"Similarity search | project={project_id} "
        f"found={len(results)} threshold={SIMILARITY_THRESHOLD}"
    )
    return results


def format_similar_for_prompt(similar: list[SimilarIncident]) -> str:
    """
    Format similar incidents into a concise block for Claude's prompt.
    Designed to be injected into the existing analyzer prompt.
    """
    if not similar:
        return "No similar past incidents found in this project's history."

    lines = []
    for i, inc in enumerate(similar, 1):
        actions = "; ".join(inc.recommended_actions[:2]) if inc.recommended_actions else "none recorded"
        lines.append(
            f"{i}. [{inc.similarity_score:.0%} match] \"{inc.title}\" "
            f"({inc.detected_at.strftime('%b %d %Y')}, severity: {inc.severity or 'unknown'})\n"
            f"   Root cause: {inc.root_cause}\n"
            f"   What was recommended: {actions}"
        )

    return "\n\n".join(lines)
