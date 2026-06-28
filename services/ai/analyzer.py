"""
Analyzer — the heart of Phase 3.

Takes an anomaly signal + DB context (recent errors + deployments) and calls
Claude to produce a structured incident report.

The prompt is engineered to give Claude everything it needs:
  - Quantitative anomaly data (error counts, spike ratio, time window)
  - Error patterns grouped by exception type with sample stack traces
  - Deployment timeline with commit messages and timing relative to the spike
  - Strict JSON output contract so the response is always parseable

Claude response schema:
{
  "title": "Brief one-line description of the incident",
  "severity": "low|medium|high|critical",
  "root_cause_hypothesis": "Claude's best explanation of why this is happening",
  "evidence": "Specific evidence from the errors/deployments supporting the hypothesis",
  "deployment_correlated": true|false,
  "deployment_explanation": "Why this deployment is/isn't likely the cause (if applicable)",
  "affected_endpoints": ["/api/orders", "/api/users"],
  "recommended_actions": ["action 1", "action 2", "action 3"]
}
"""

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import anthropic

from shared.db.models import Event

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_STACK_TRACE_LEN = 800   # chars — keep prompts from ballooning
MAX_ERROR_GROUPS = 5        # top N exception types to include


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


# ── Prompt construction ────────────────────────────────────────────────────────

def _group_errors(error_events: list[Event]) -> list[dict]:
    """
    Group error events by exception type and return the top N groups,
    each with a count, affected endpoints, and one sample stack trace.
    """
    groups: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "endpoints": set(), "sample_trace": None}
    )

    for event in error_events:
        data = event.raw_data or {}
        exc_type = data.get("exception_type", "UnknownError")
        groups[exc_type]["count"] += 1

        endpoint = data.get("endpoint")
        if endpoint:
            groups[exc_type]["endpoints"].add(endpoint)

        if not groups[exc_type]["sample_trace"] and data.get("stack_trace"):
            trace = data["stack_trace"]
            groups[exc_type]["sample_trace"] = (
                trace[:MAX_STACK_TRACE_LEN] + "…" if len(trace) > MAX_STACK_TRACE_LEN else trace
            )

    # Sort by count descending, take top N
    sorted_groups = sorted(groups.items(), key=lambda x: x[1]["count"], reverse=True)
    return [
        {
            "exception_type": exc_type,
            "count": info["count"],
            "affected_endpoints": sorted(info["endpoints"]),
            "sample_stack_trace": info["sample_trace"],
        }
        for exc_type, info in sorted_groups[:MAX_ERROR_GROUPS]
    ]


def _format_deployments(deployment_events: list[Event], anomaly_detected_at: datetime) -> list[dict]:
    """Format deployment events with timing relative to when the anomaly was detected."""
    result = []
    for event in deployment_events:
        data = event.raw_data or {}
        ts = event.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        detected_utc = anomaly_detected_at
        if detected_utc.tzinfo is None:
            detected_utc = detected_utc.replace(tzinfo=timezone.utc)

        minutes_before = round((detected_utc - ts).total_seconds() / 60, 1)
        result.append({
            "commit_sha": data.get("commit_sha", "unknown"),
            "branch": data.get("branch", "unknown"),
            "pusher": data.get("pusher"),
            "commit_message": data.get("commit_message", ""),
            "environment": event.environment,
            "minutes_before_anomaly": minutes_before,
        })

    return sorted(result, key=lambda d: d["minutes_before_anomaly"])


def _build_prompt(
    anomaly_signal: dict,
    error_groups: list[dict],
    deployment_context: list[dict],
    similar_incidents_text: str = "",
) -> str:
    detected_at = anomaly_signal.get("detected_at", "unknown")
    current_errors = anomaly_signal.get("current_errors", 0)
    baseline_avg = anomaly_signal.get("baseline_avg", 0)
    spike_ratio = anomaly_signal.get("spike_ratio", 0)
    current_bucket = anomaly_signal.get("current_bucket", "unknown")

    error_section = json.dumps(error_groups, indent=2)
    deployment_section = (
        json.dumps(deployment_context, indent=2)
        if deployment_context
        else "No deployments detected in the 30 minutes before this anomaly."
    )

    history_section = ""
    if similar_incidents_text and "No similar" not in similar_incidents_text:
        history_section = f"""
## Similar Past Incidents (from this project's history)
The following past incidents are semantically similar to this one.
Use them to inform your analysis — if the pattern matches, say so explicitly and
reference what worked before.

{similar_incidents_text}
"""

    return f"""You are an expert site reliability engineer analyzing a production incident.

## Anomaly Summary
- Detected at: {detected_at}
- Time window: {current_bucket} (1-minute bucket)
- Error count this window: {current_errors}
- Historical baseline (avg errors/min): {baseline_avg}
- Spike ratio: {spike_ratio}× above baseline

## Error Breakdown (last 30 minutes)
{error_section}

## Deployment Timeline (last 30 minutes before anomaly)
{deployment_section}
{history_section}
## Your Task
Analyze this incident and return a JSON object with your findings.
Base your analysis strictly on the data above — do not speculate beyond what the evidence supports.
If similar past incidents were provided, reference them in your analysis where relevant.

Severity guide:
- critical: service down or data loss possible
- high: major feature broken, many users affected
- medium: degraded performance or partial feature failure
- low: minor errors, minimal user impact

Return ONLY a valid JSON object. No markdown, no backticks, no explanation outside the JSON.

{{
  "title": "one-line incident title (max 100 chars)",
  "severity": "low|medium|high|critical",
  "root_cause_hypothesis": "your best explanation of the root cause",
  "evidence": "specific evidence from the error data and deployments supporting your hypothesis",
  "deployment_correlated": true or false,
  "deployment_explanation": "why this deployment is or is not likely the cause, or null if no deployments",
  "affected_endpoints": ["list", "of", "endpoints"],
  "exception_types_seen": ["ExceptionType1", "ExceptionType2"],
  "recommended_actions": ["action 1", "action 2", "action 3"],
  "similar_incident_ids": ["past-incident-uuid-if-applicable"],
  "recurrence_note": "brief note if this appears to be a recurring issue, or null if not"
}}"""


# ── Main analysis entry point ──────────────────────────────────────────────────

def analyze_incident(
    anomaly_signal: dict,
    error_events: list[Event],
    deployment_events: list[Event],
    similar_incidents_text: str = "",
) -> Optional[dict]:
    """
    Build a prompt from the anomaly context and call Claude.
    Returns the parsed incident dict, or None if analysis failed.
    """
    if not error_events:
        logger.warning("No error events to analyze — skipping Claude call")
        return None

    detected_at_raw = anomaly_signal.get("detected_at", "")
    try:
        detected_at = datetime.fromisoformat(detected_at_raw)
    except (ValueError, TypeError):
        detected_at = datetime.now(timezone.utc)

    error_groups = _group_errors(error_events)
    deployment_context = _format_deployments(deployment_events, detected_at)
    prompt = _build_prompt(anomaly_signal, error_groups, deployment_context, similar_incidents_text)

    logger.info(
        f"Calling Claude | model={MODEL} "
        f"error_groups={len(error_groups)} deployments={len(deployment_context)}"
    )

    try:
        client = _get_client()
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=(
                "You are an expert SRE analyzing production incidents. "
                "Always respond with valid JSON only — no markdown, no backticks, "
                "no text before or after the JSON object."
            ),
            messages=[{"role": "user", "content": prompt}],
        )

        raw_text = response.content[0].text.strip()
        logger.debug(f"Claude raw response: {raw_text[:300]}")

        # Strip any accidental backtick fences
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]

        result = json.loads(raw_text)

        # Validate required fields are present
        required = {"title", "severity", "root_cause_hypothesis", "recommended_actions"}
        missing = required - result.keys()
        if missing:
            logger.warning(f"Claude response missing fields: {missing}")

        logger.info(
            f"Claude analysis complete | "
            f"title={result.get('title', 'N/A')!r} "
            f"severity={result.get('severity')} "
            f"deployment_correlated={result.get('deployment_correlated')}"
        )
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Claude returned unparseable JSON: {e} | raw={raw_text[:500]}")
        return None
    except anthropic.APIError as e:
        logger.error(f"Anthropic API error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error calling Claude: {e}", exc_info=True)
        return None
