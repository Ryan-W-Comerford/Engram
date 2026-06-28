"""
GitHub webhook receiver — OTel-native output.

Receives GitHub push events, validates the HMAC signature, then emits a
deployment span via the Python OTel SDK to the OTel Collector (OTLP/gRPC).

The Collector writes the span to Kafka in OTel JSON format — identical to
how application traces flow — so the entire pipeline sees one uniform format.

The ingestor has zero Kafka dependency. It is purely an OTLP client for
this path.

Span model for a deployment:
  name:       "deployment"
  attributes:
    engram.project_id       → uuid of the Engram project
    engram.event_type       → "deployment"  (normalizer routing key)
    deployment.environment   → "production" | "staging" | "development"
    deployment.commit_sha    → short SHA
    deployment.branch        → git branch name
    deployment.pusher        → GitHub username
    deployment.commit_message→ first 500 chars of the commit message
    deployment.commits_count → number of commits in the push
    deployment.repository    → full repo name (owner/repo)
    service.name             → repository name (short)
"""

import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, StatusCode
from sqlalchemy.orm import Session

from shared.db.models import Project
from shared.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
OTEL_ENDPOINT         = os.getenv("OTEL_COLLECTOR_ENDPOINT", "http://otel-collector:4317")
OTEL_BEARER_TOKEN     = os.getenv("OTEL_BEARER_TOKEN", "")

if not GITHUB_WEBHOOK_SECRET:
    logger.warning("GITHUB_WEBHOOK_SECRET is not set — POST /webhooks/github/* will return 503")


# ── OTel tracer setup (module-level, initialised once) ────────────────────────
# The ingestor acts as an instrumented service that emits spans to the Collector.
# We use a dedicated TracerProvider here (not the global one) so the ingestor's
# own framework instrumentation doesn't interfere with deployment spans.

_tracer: trace.Tracer | None = None


def _get_tracer() -> trace.Tracer:
    global _tracer
    if _tracer is None:
        resource = Resource.create({
            "service.name":    "engram-ingestor",
            "service.version": "0.2.0",
        })
        headers = {}
        if OTEL_BEARER_TOKEN:
            headers["Authorization"] = f"Bearer {OTEL_BEARER_TOKEN}"
        exporter = OTLPSpanExporter(
            endpoint=OTEL_ENDPOINT,
            insecure=not OTEL_ENDPOINT.startswith("https"),
            headers=headers,
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        _tracer = provider.get_tracer("engram.webhooks")
        logger.info(f"OTel tracer initialised → {OTEL_ENDPOINT}")
    return _tracer


# ── Signature verification ─────────────────────────────────────────────────────

def _verify_github_signature(body: bytes, signature_header: str | None) -> None:
    if not GITHUB_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=503,
            detail="GITHUB_WEBHOOK_SECRET is not configured — webhook endpoint is disabled",
        )
    if not signature_header:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header")
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


def _branch_to_environment(branch: str) -> str:
    b = branch.lower()
    if b in ("main", "master"):
        return "production"
    if b.startswith(("staging", "stage", "release")):
        return "staging"
    return "development"


# ── Webhook handler ────────────────────────────────────────────────────────────

@router.post("/github/{project_id}")
async def github_push_webhook(
    project_id: str  = Path(..., description="Engram project ID"),
    x_github_event:        str | None = Header(None, alias="X-GitHub-Event"),
    x_hub_signature_256:   str | None = Header(None, alias="X-Hub-Signature-256"),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Receive a GitHub push webhook and emit it as an OTel deployment span.
    The span flows: Collector → Kafka → stream processor / AI service.
    """
    body = await request.body()
    _verify_github_signature(body, x_hub_signature_256)

    if x_github_event == "ping":
        logger.info(f"GitHub ping for project {project_id} — webhook configured OK")
        return {"status": "pong"}

    if x_github_event != "push":
        logger.info(f"Ignoring GitHub event type '{x_github_event}'")
        return {"status": "ignored", "event": x_github_event}

    try:
        project_uuid = uuid.UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    project = db.query(Project).filter(Project.id == project_uuid).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    payload     = json.loads(body)
    ref         = payload.get("ref", "")
    branch      = ref.replace("refs/heads/", "") if ref.startswith("refs/heads/") else ref
    head_commit = payload.get("head_commit") or {}
    pusher      = payload.get("pusher", {}).get("name", "unknown")
    environment = _branch_to_environment(branch)
    repo_name   = payload.get("repository", {}).get("name", "unknown")
    repo_full   = payload.get("repository", {}).get("full_name", "")
    commit_sha  = (payload.get("after") or "")[:12]
    commit_msg  = head_commit.get("message", "")[:500]
    commits_n   = len(payload.get("commits", []))

    # ── Emit OTel deployment span ─────────────────────────────────────────────
    # A deployment is modelled as an instantaneous span (start ≈ end).
    # SpanKind.INTERNAL signals this is a server-side synthetic event,
    # not an inbound request or outbound call.
    tracer = _get_tracer()
    with tracer.start_as_current_span(
        "deployment",
        kind=SpanKind.INTERNAL,
    ) as span:
        # Routing key — normalizer uses this to produce a deployment event
        span.set_attribute("engram.event_type",   "deployment")
        span.set_attribute("engram.project_id",    str(project.id))

        # OTel deployment semantic conventions
        span.set_attribute("deployment.environment",     environment)
        span.set_attribute("deployment.commit_sha",      commit_sha)
        span.set_attribute("deployment.branch",          branch)
        span.set_attribute("deployment.pusher",          pusher)
        span.set_attribute("deployment.commit_message",  commit_msg)
        span.set_attribute("deployment.commits_count",   commits_n)
        span.set_attribute("deployment.repository",      repo_full)

        # Service identity — makes the deployment appear correctly in the dashboard
        span.set_attribute("service.name",  repo_name)
        span.set_attribute("service.version", commit_sha)

        span.set_status(StatusCode.OK)

    logger.info(
        f"Deployment span emitted | project={project.name} "
        f"branch={branch} sha={commit_sha} pusher={pusher} env={environment}"
    )

    return {
        "status":      "received",
        "branch":      branch,
        "commit_sha":  commit_sha,
        "environment": environment,
        "pipeline":    "otel → collector → kafka",
    }
