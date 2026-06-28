#!/usr/bin/env python3
"""
trigger_incident.py — fire a test incident through the Engram pipeline.

Detection thresholds (services/processor/anomaly.py):
  baseline_avg >= 0    (MIN_BASELINE_RATE — no minimum)
  current      >= 1.5x baseline_avg  (SPIKE_MULTIPLIER)
  1 consecutive window  (CONSECUTIVE_WINDOWS_REQUIRED)
  evaluated every 30s  (EVAL_INTERVAL_SECONDS)

Strategy:
  1. Send 5 errors in minute N  (baseline bucket)
  2. Wait for wall clock to cross into minute N+1
  3. Send 20 errors in minute N+1  (spike: 20 >= 1.5 * 5 = 7.5)
  4. Wait up to 90s for the detector to evaluate and AI to analyse

Usage:
  python trigger_incident.py
  python trigger_incident.py --api-key pk_live_...
  python trigger_incident.py --url http://localhost:8000 --api-key pk_live_...
"""

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
GREEN = "\033[32m"; YELLOW = "\033[33m"; RED = "\033[31m"
CYAN  = "\033[36m"; WHITE  = "\033[97m"

def ok(msg):   print(f"  {GREEN}+{RESET}  {msg}")
def warn(msg): print(f"  {YELLOW}!{RESET}  {msg}")
def err(msg):  print(f"  {RED}x{RESET}  {msg}", file=sys.stderr)
def info(msg): print(f"  {CYAN}>{RESET}  {msg}")
def dim(msg):  print(f"     {DIM}{msg}{RESET}")

def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

def post_json(url, payload, headers):
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

def get_json(url, headers):
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

ERRORS = [
    {"exception_type": "DatabaseTimeoutError",   "message": "Connection pool exhausted",         "endpoint": "/api/users",    "method": "GET"},
    {"exception_type": "NullPointerException",   "message": "Cannot read property 'id'",         "endpoint": "/api/orders",   "method": "POST"},
    {"exception_type": "ConnectionRefusedError", "message": "Redis refused on port 6379",         "endpoint": "/api/cache",    "method": "GET"},
    {"exception_type": "AuthenticationError",    "message": "JWT signature verification failed",  "endpoint": "/api/auth",     "method": "POST"},
    {"exception_type": "MemoryError",            "message": "Heap limit exceeded: 2048MB",        "endpoint": "/api/reports",  "method": "GET"},
]

def make_error(service):
    t = random.choice(ERRORS)
    return {
        "event_type":  "error",
        "timestamp":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "environment": "production",
        "service":     service,
        "data":        dict(t),
    }

def send(url, headers, service):
    try:
        post_json(url, make_error(service), headers)
        return True
    except urllib.error.HTTPError as e:
        err(f"HTTP {e.code}: {e.read().decode()}")
        sys.exit(1)
    except Exception as e:
        warn(f"send blip (non-fatal): {e}")
        return False

def countdown(seconds, label):
    for r in range(seconds, 0, -1):
        print(f"\r  {CYAN}o{RESET}  {label}: {BOLD}{r}s{RESET}   ", end="", flush=True)
        time.sleep(1)
    print(f"\r  {GREEN}+{RESET}  {label}: done{' '*20}")

def main():
    load_dotenv(Path(__file__).parent / ".env")
    p = argparse.ArgumentParser()
    p.add_argument("--api-key", default=os.getenv("ENGRAM_API_KEY"))
    p.add_argument("--url",     default=os.getenv("INGESTOR_URL", "http://localhost:8000"))
    p.add_argument("--service", default="api")
    args = p.parse_args()

    if not args.api_key:
        err("No API key. Set ENGRAM_API_KEY in .env or pass --api-key.")
        sys.exit(1)

    ingest_url = args.url.rstrip("/") + "/ingest"
    health_url = args.url.rstrip("/") + "/health"
    headers    = {"Content-Type": "application/json", "X-API-Key": args.api_key}

    print()
    print(f"  {BOLD}{WHITE}Engram — Incident Trigger{RESET}")
    print(f"  {DIM}{args.url}{RESET}")
    print()

    info("Checking ingestor…")
    try:
        h = get_json(health_url, {})
        ok(f"Ingestor up ({h.get('status','ok')})")
    except Exception as e:
        err(f"Cannot reach ingestor: {e}")
        sys.exit(1)

    # ── Baseline: 5 errors in current minute ──────────────────────────────────
    print()
    print(f"  {BOLD}Step 1 — Baseline (5 errors){RESET}")
    dim("Fills the current minute-bucket so the aggregator has a reference point.")
    print()

    for i in range(5):
        send(ingest_url, headers, args.service)
        print(f"\r  {GREEN}+{RESET}  Sent {BOLD}{i+1}/5{RESET} baseline errors   ", end="", flush=True)
        if i < 4:
            time.sleep(2)
    print()
    ok("Baseline done")
    print()

    # ── Wait for minute boundary ───────────────────────────────────────────────
    wait_s = 60 - datetime.now().second + 2
    info(f"Waiting {wait_s}s for the clock to roll into the next minute…")
    countdown(wait_s, "Minute boundary")
    print()

    # ── Spike: 20 errors — 20 >= 1.5 × 5 = 7.5 ───────────────────────────────
    print(f"  {BOLD}Step 2 — Spike (20 errors){RESET}")
    dim("20 errors in the new minute is 4x the baseline — well above the 1.5x threshold.")
    print()

    for i in range(20):
        send(ingest_url, headers, args.service)
        filled = int((i+1)/20*24)
        bar = "#"*filled + "-"*(24-filled)
        print(f"\r  {RED}^{RESET}  [{bar}] {BOLD}{i+1}/20{RESET}   ", end="", flush=True)
        if i < 19:
            time.sleep(1)
    print()
    ok("Spike fired")
    print()

    # ── Wait for detector + AI ─────────────────────────────────────────────────
    print(f"  {BOLD}Step 3 — Waiting for analysis{RESET}")
    dim("Detector runs every 30s, then AI service calls Claude (~30s).")
    print()
    countdown(90, "Waiting for incident")

    print()
    print(f"  {BOLD}{GREEN}Done!{RESET}")
    print()
    ok("Check the dashboard Incidents page for the new incident.")
    dim("Dashboard: http://localhost:8080/incidents")
    print()
    dim("If nothing appears, restart the processor first:")
    dim("  docker compose build processor && docker compose up -d processor")
    dim("Then run this script again.")
    print()

if __name__ == "__main__":
    main()
