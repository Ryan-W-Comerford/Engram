import hashlib
import secrets

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from shared.db.models import Project
from shared.db.session import get_db


def generate_api_key() -> str:
    """Generate a new API key in the format pk_live_<48 hex chars>."""
    return "pk_live_" + secrets.token_hex(24)


def hash_api_key(key: str) -> str:
    """SHA-256 hash of an API key — this is what we store in the DB."""
    return hashlib.sha256(key.encode()).hexdigest()


def get_current_project(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> Project:
    """
    FastAPI dependency that validates the X-API-Key header and returns the
    associated Project. Raises 401 if the key is missing or unknown.

    Usage:
        @app.post("/ingest")
        def ingest(project: Project = Depends(get_current_project)):
            ...
    """
    key_hash = hash_api_key(x_api_key)
    project = db.query(Project).filter(Project.api_key_hash == key_hash).first()
    if not project:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return project
