"""
Optional shared-token auth for the ingestor.

Engram is single-tenant and open by default. If AUTH_TOKEN is set in the
environment, every write endpoint requires it — sent either as
`Authorization: Bearer <token>` or `X-API-Key: <token>`. If AUTH_TOKEN is
unset, the dependency is a no-op and the endpoint is fully open.
"""

import hmac
import os

from fastapi import Header, HTTPException, status

AUTH_TOKEN = os.getenv("AUTH_TOKEN", "").strip()


def _extract(authorization: str | None, x_api_key: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return (x_api_key or "").strip()


def require_token(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    if not AUTH_TOKEN:
        return
    presented = _extract(authorization, x_api_key)
    if not presented or not hmac.compare_digest(presented, AUTH_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing token. Send Authorization: Bearer <AUTH_TOKEN>.",
        )
