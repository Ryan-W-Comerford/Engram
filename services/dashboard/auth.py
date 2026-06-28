"""
services/dashboard/auth.py

Session-based auth for the Engram dashboard.

Login flow:
  1. User POSTs their project API key to /login
  2. We SHA-256 hash it and look it up in projects.api_key_hash
  3. On match, we sign {project_id} into a cookie with itsdangerous
  4. Every subsequent request reads and verifies that cookie via
     the get_session_project() FastAPI dependency

Cookie:
  name:     pulsession
  value:    URLSafeTimedSerializer token (project_id as str UUID)
  max-age:  30 days
  flags:    HttpOnly, SameSite=Lax
            Secure=True when HTTPS=1 is set in env
"""

import hashlib
import logging
import os
import secrets
import uuid

from fastapi import Depends, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from shared.db.models import Project
from shared.db.session import get_db

logger = logging.getLogger(__name__)

SESSION_COOKIE  = "pulsession"
SESSION_MAX_AGE = 60 * 60 * 24 * 30   # 30 days
SECURE_COOKIES  = os.getenv("HTTPS", "").lower() in ("1", "true", "yes")

_secret = os.getenv("DASHBOARD_SECRET_KEY", "")
if not _secret:
    logger.warning(
        "DASHBOARD_SECRET_KEY not set — generating a random key. "
        "Sessions will not survive a restart. "
        "Generate a permanent key with: "
        "python -c \"import secrets; print(secrets.token_hex(32))\""
    )
    _secret = secrets.token_hex(32)

_signer = URLSafeTimedSerializer(_secret)


class NotAuthenticated(Exception):
    """Raised by get_session_project when no valid session cookie is present."""


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def make_session_token(project_id: str) -> str:
    return _signer.dumps(project_id)


def verify_session_token(token: str) -> str | None:
    try:
        return _signer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def get_session_project(request: Request, db: Session = Depends(get_db)) -> Project:
    """
    FastAPI dependency that resolves the session cookie to a Project row.
    Raises NotAuthenticated if the cookie is absent, invalid, expired, or the
    project has been deleted.
    """
    token      = request.cookies.get(SESSION_COOKIE)
    project_id = verify_session_token(token) if token else None
    if not project_id:
        raise NotAuthenticated()
    try:
        pid = uuid.UUID(project_id)
    except ValueError:
        raise NotAuthenticated()
    project = db.query(Project).filter(Project.id == pid).first()
    if not project:
        raise NotAuthenticated()
    return project
