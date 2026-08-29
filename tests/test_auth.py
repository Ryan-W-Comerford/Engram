"""
tests/test_auth.py

Unit tests for the two auth modules:
  • services/ingestor/auth.py  — optional shared-token check (require_token)
  • services/dashboard/auth.py — signed login-cookie helpers

All tests are pure Python; no DB or running services required.
"""

import importlib
import os
import sys

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_module(name: str, filepath: str):
    spec = importlib.util.spec_from_file_location(name, filepath)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_ingestor_auth(token: str | None):
    if token is None:
        os.environ.pop("AUTH_TOKEN", None)
    else:
        os.environ["AUTH_TOKEN"] = token
    return _load_module(
        "ingestor_auth", os.path.join(ROOT, "services", "ingestor", "auth.py")
    )


def _load_dashboard_auth(token: str | None):
    if token is None:
        os.environ.pop("AUTH_TOKEN", None)
    else:
        os.environ["AUTH_TOKEN"] = token
    return _load_module(
        "dashboard_auth", os.path.join(ROOT, "services", "dashboard", "auth.py")
    )


# ── Ingestor: require_token ───────────────────────────────────────────────────

def test_require_token_noop_when_unset():
    mod = _load_ingestor_auth(None)
    # No exception regardless of what's presented.
    assert mod.require_token(authorization=None, x_api_key=None) is None
    assert mod.require_token(authorization="Bearer anything", x_api_key=None) is None


def test_require_token_accepts_bearer():
    mod = _load_ingestor_auth("s3cret")
    assert mod.require_token(authorization="Bearer s3cret", x_api_key=None) is None


def test_require_token_accepts_x_api_key():
    mod = _load_ingestor_auth("s3cret")
    assert mod.require_token(authorization=None, x_api_key="s3cret") is None


def test_require_token_rejects_wrong_and_missing():
    mod = _load_ingestor_auth("s3cret")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e1:
        mod.require_token(authorization="Bearer nope", x_api_key=None)
    assert e1.value.status_code == 401

    with pytest.raises(HTTPException) as e2:
        mod.require_token(authorization=None, x_api_key=None)
    assert e2.value.status_code == 401


# ── Dashboard: session cookie ────────────────────────────────────────────────

def test_dashboard_open_when_token_unset():
    mod = _load_dashboard_auth(None)
    assert mod.AUTH_REQUIRED is False


def test_session_token_roundtrip():
    mod = _load_dashboard_auth("s3cret")
    token = mod.make_session_token()
    assert mod.verify_session_token(token) is True


def test_verify_tampered_token_is_false():
    mod = _load_dashboard_auth("s3cret")
    token = mod.make_session_token()
    mid = len(token) // 2
    bad = token[:mid] + ("X" if token[mid] != "X" else "Y") + token[mid + 1:]
    assert mod.verify_session_token(bad) is False


def test_verify_empty_and_garbage_are_false():
    mod = _load_dashboard_auth("s3cret")
    assert mod.verify_session_token("") is False
    assert mod.verify_session_token("not.a.real.token") is False


def test_token_matches_is_constant_time_exact():
    mod = _load_dashboard_auth("s3cret")
    assert mod.token_matches("s3cret") is True
    assert mod.token_matches(" s3cret ") is True   # trimmed
    assert mod.token_matches("s3cre") is False
    assert mod.token_matches("") is False


def teardown_module(module):
    os.environ.pop("AUTH_TOKEN", None)
