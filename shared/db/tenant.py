"""
Single-tenant helper.

Engram no longer has a concept of multiple projects or API keys. Every event
and incident belongs to one implicit project with a fixed, well-known id.
Migration 008 seeds the row; this helper is a belt-and-braces fallback so the
services still work if they somehow start against a DB where the row is missing.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from shared.db.models import DEFAULT_PROJECT_ID, DEFAULT_PROJECT_NAME, Project


def get_or_create_default_project(db: Session) -> Project:
    project = db.query(Project).filter(Project.id == DEFAULT_PROJECT_ID).first()
    if project is None:
        project = Project(
            id=DEFAULT_PROJECT_ID,
            name=DEFAULT_PROJECT_NAME,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(project)
        db.commit()
        db.refresh(project)
    return project
