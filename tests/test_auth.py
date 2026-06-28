"""
tests/test_auth.py

Unit tests for the two auth modules:
  • services/ingestor/auth.py  — API key generation and hashing
  • services/dashboard/auth.py — session cookie signing and verification

All tests are pure Python; no DB or running services required.
"""

import hashlib
import importlib.util
import os
import sys

import pytest

# ── Load auth modules by explicit file path to avoid the name collision ────────
# Both services/ingestor/auth.py and services/dashboard/auth.py exist and
# both would be found as `auth` once their parent dirs are in sys.path.
# importlib.util.spec_from_file_location gives each a unique module name.

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _load_module(name: str, filepath: str):
    spec = importlib.util.spec_from_file_location(name, filepath)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

ingestor_auth  = _load_module(
    "ingestor_auth",
    os.path.join(ROOT, "services", "ingestor", "auth.py"),
)
dashboard_auth = _load_module(
    "dashboard_auth",
    os.path.join(ROOT, "services", "dashboard", "auth.py"),
)


# ── Ingestor: generate_api_key ─────────────────────────────────────────────────

def test_api_key_has_correct_prefix():
    key = ingestor_auth.generate_api_key()
    assert key.startswith("pk_live_"), f"Expected prefix 'pk_live_', got: {key[:10]}"


def test_api_key_has_correct_length():
    key = ingestor_auth.generate_api_key()
    # "pk_live_" (8) + 48 hex chars = 56 total
    assert len(key) == 56, f"Expected 56 chars, got {len(key)}"


def test_api_keys_are_unique():
    keys = {ingestor_auth.generate_api_key() for _ in range(20)}
    assert len(keys) == 20, "Two API keys collided — entropy is broken"


# ── Ingestor: hash_api_key ─────────────────────────────────────────────────────

def test_hash_is_deterministic():
    key  = "pk_live_abc123"
    h1   = ingestor_auth.hash_api_key(key)
    h2   = ingestor_auth.hash_api_key(key)
    assert h1 == h2


def test_hash_is_sha256():
    key      = "test_key"
    expected = hashlib.sha256(b"test_key").hexdigest()
    assert ingestor_auth.hash_api_key(key) == expected


def test_different_keys_produce_different_hashes():
    h1 = ingestor_auth.hash_api_key("key_one")
    h2 = ingestor_auth.hash_api_key("key_two")
    assert h1 != h2


def test_hash_output_is_64_hex_chars():
    h = ingestor_auth.hash_api_key("anything")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


# ── Dashboard: session token roundtrip ────────────────────────────────────────

def test_session_token_roundtrip():
    project_id = "abb7108b-375e-43a2-8015-7875d25f5f3f"
    token = dashboard_auth.make_session_token(project_id)
    recovered = dashboard_auth.verify_session_token(token)
    assert recovered == project_id


def test_verify_tampered_token_returns_none():
    project_id = "abb7108b-375e-43a2-8015-7875d25f5f3f"
    token = dashboard_auth.make_session_token(project_id)
    # Flip one character in the middle of the token
    mid = len(token) // 2
    bad = token[:mid] + ("X" if token[mid] != "X" else "Y") + token[mid + 1:]
    assert dashboard_auth.verify_session_token(bad) is None


def test_verify_empty_token_returns_none():
    assert dashboard_auth.verify_session_token("") is None


def test_verify_garbage_token_returns_none():
    assert dashboard_auth.verify_session_token("not.a.real.token.at.all") is None


# ── Cross-service consistency ──────────────────────────────────────────────────

def test_both_modules_produce_same_hash():
    """Ingestor and dashboard must agree on how to hash an API key (SHA-256)."""
    key = "pk_live_test_consistency_check"
    assert ingestor_auth.hash_api_key(key) == dashboard_auth.hash_api_key(key)
