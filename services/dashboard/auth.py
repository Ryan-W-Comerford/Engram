"""
services/dashboard/auth.py

Optional shared-token gate for the Engram dashboard.

Engram is single-tenant. If AUTH_TOKEN is unset the dashboard is fully open.
If it is set:
  1. User POSTs the token to /login
  2. We compare it (constant-time) to AUTH_TOKEN
  3. On match we sign a short marker into a cookie with itsdangerous
  4. get_current_view() verifies that cookie on every request

Cookie:
  name:     engram_session
  value:    signed marker ("ok")
  max-age:  30 days
  flags:    HttpOnly, SameSite=Lax; Secure when HTTPS=1
"""

import hmac
import logging
import os
import secrets

from fastapi import Depends, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from shared.db.models import Project
from shared.db.session import get_db
from shared.db.tenant import get_or_create_default_project

logger = logging.getLogger(__name__)

AUTH_TOKEN      = os.getenv("AUTH_TOKEN", "").strip()
AUTH_REQUIRED   = bool(AUTH_TOKEN)
SESSION_COOKIE  = "engram_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30   # 30 days
SECURE_COOKIES  = os.getenv("HTTPS", "").lower() in ("1", "true", "yes")
_SESSION_MARKER = "ok"

_secret = os.getenv("DASHBOARD_SECRET_KEY", "").strip()
if AUTH_REQUIRED and not _secret:
    logger.warning(
        "AUTH_TOKEN is set but DASHBOARD_SECRET_KEY is not — generating a random "
        "signing key. Logins will not survive a restart. Set DASHBOARD_SECRET_KEY "
        "to a fixed value: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
if not _secret:
    _secret = secrets.token_hex(32)

_signer = URLSafeTimedSerializer(_secret)


class NotAuthenticated(Exception):
    """Raised by get_current_view when AUTH_TOKEN is set and no valid cookie is present."""


def token_matches(presented: str) -> bool:
    return bool(AUTH_TOKEN) and hmac.compare_digest(presented.strip(), AUTH_TOKEN)


def make_session_token() -> str:
    return _signer.dumps(_SESSION_MARKER)


def verify_session_token(token: str) -> bool:
    try:
        return _signer.loads(token, max_age=SESSION_MAX_AGE) == _SESSION_MARKER
    except (BadSignature, SignatureExpired):
        return False


def get_current_view(request: Request, db: Session = Depends(get_db)) -> Project:
    """
    FastAPI dependency. Returns the single implicit project.
    If AUTH_TOKEN is set, raises NotAuthenticated unless a valid session cookie
    is present. If AUTH_TOKEN is unset, always succeeds.
    """
    if AUTH_REQUIRED:
        token = request.cookies.get(SESSION_COOKIE)
        if not token or not verify_session_token(token):
            raise NotAuthenticated()
    return get_or_create_default_project(db)
