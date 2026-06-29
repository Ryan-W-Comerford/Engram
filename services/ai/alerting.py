"""
services/ai/alerting.py

Sends notifications when a new incident is created and Claude has finished
its analysis. Supports two channels — configure either or both:

    Slack:
        SLACK_WEBHOOK_URL   — incoming webhook URL from api.slack.com/apps
                              (Settings → Incoming Webhooks → Add New Webhook)

    Email via SendGrid:
        SENDGRID_API_KEY    — from app.sendgrid.com/settings/api_keys
        ALERT_EMAIL_FROM    — verified sender address in your SendGrid account
        ALERT_EMAIL_TO      — comma-separated list of recipients

Both channels are optional and independent. If neither is configured the
function is a no-op — incidents are still saved, just no push notification.

Alerting is fire-and-forget. A failure here never prevents incident storage.
"""

import logging
import json
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SLACK_WEBHOOK_URL  = os.getenv("SLACK_WEBHOOK_URL", "")
SENDGRID_API_KEY   = os.getenv("SENDGRID_API_KEY", "")
ALERT_EMAIL_FROM   = os.getenv("ALERT_EMAIL_FROM", "")
ALERT_EMAIL_TO     = os.getenv("ALERT_EMAIL_TO", "")   # comma-separated


# ── Severity colour map ────────────────────────────────────────────────────────

_SLACK_COLOURS = {
    "critical": "#ff4d4d",
    "high":     "#ffb020",
    "medium":   "#4d9fff",
    "low":      "#8c9099",
}

_SEVERITY_EMOJI = {
    "critical": "🔴",
    "high":     "🟠",
    "medium":   "🔵",
    "low":      "⚪",
}


# ── Slack ──────────────────────────────────────────────────────────────────────

def _send_slack(incident_id: str, title: str, severity: Optional[str],
                root_cause: str, spike_ratio: str, recurrence_note: Optional[str],
                dashboard_url: str) -> None:
    """
    Post a rich Slack message using the Block Kit attachment format.
    Includes severity colour bar, root cause, spike ratio, optional
    recurrence note, and a direct link to the incident detail page.
    """
    if not SLACK_WEBHOOK_URL:
        return

    severity    = severity or "unknown"
    colour      = _SLACK_COLOURS.get(severity, "#8c9099")
    emoji       = _SEVERITY_EMOJI.get(severity, "⚪")
    recurrence  = f"\n⟳ *Recurring:* {recurrence_note}" if recurrence_note else ""

    payload = {
        "attachments": [
            {
                "color":    colour,
                "fallback": f"[{severity.upper()}] {title}",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"{emoji} *[{severity.upper()}] {title}*\n"
                                f"*Root cause:* {root_cause}\n"
                                f"*Spike ratio:* {spike_ratio}× above baseline"
                                f"{recurrence}"
                            ),
                        },
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type":  "button",
                                "text":  {"type": "plain_text", "text": "View Incident →"},
                                "url":   dashboard_url,
                                "style": "danger" if severity in ("critical", "high") else "primary",
                            }
                        ],
                    },
                ],
            }
        ]
    }

    try:
        response = httpx.post(SLACK_WEBHOOK_URL, json=payload, timeout=5.0)
        response.raise_for_status()
        logger.info(f"Slack alert sent | incident={incident_id}")
    except Exception as e:
        logger.error(f"Slack alert failed | incident={incident_id} error={e}")


# ── Email via SendGrid ─────────────────────────────────────────────────────────

def _send_email(incident_id: str, title: str, severity: Optional[str],
                root_cause: str, actions: list[str], spike_ratio: str,
                recurrence_note: Optional[str], dashboard_url: str) -> None:
    """
    Send an HTML incident alert email via SendGrid's v3 mail/send API.
    Uses direct HTTP rather than the sendgrid-python SDK to avoid an
    additional dependency.
    """
    if not all([SENDGRID_API_KEY, ALERT_EMAIL_FROM, ALERT_EMAIL_TO]):
        return

    recipients = [r.strip() for r in ALERT_EMAIL_TO.split(",") if r.strip()]
    if not recipients:
        return

    import html as _html
    severity    = severity or "unknown"
    colour      = _SLACK_COLOURS.get(severity, "#8c9099")
    safe_title      = _html.escape(title)
    safe_root_cause = _html.escape(root_cause)
    safe_recurrence = _html.escape(recurrence_note) if recurrence_note else None
    safe_actions    = [_html.escape(a) for a in (actions or [])]

    recurrence   = f"<p><strong>⟳ Recurring issue:</strong> {safe_recurrence}</p>" if safe_recurrence else ""
    actions_html = "".join(f"<li>{a}</li>" for a in safe_actions)

    html = f"""
<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, sans-serif; background: #f4f4f4; padding: 20px;">
  <div style="max-width: 600px; margin: 0 auto; background: #fff;
              border-radius: 8px; overflow: hidden; border: 1px solid #e0e0e0;">
    <div style="background: {colour}; padding: 4px 0;"></div>
    <div style="padding: 24px 28px;">
      <h2 style="margin: 0 0 8px; color: #1a1a1a;">
        [{_html.escape(severity.upper())}] {safe_title}
      </h2>
      <p style="color: #555; font-size: 13px; margin: 0 0 20px;">
        Engram detected an anomaly — {_html.escape(str(spike_ratio))}× above your error baseline.
      </p>
      <hr style="border: none; border-top: 1px solid #eee; margin: 0 0 20px;">
      <p><strong>Root cause:</strong> {safe_root_cause}</p>
      {recurrence}
      {"<p><strong>Recommended actions:</strong></p><ul>" + actions_html + "</ul>" if actions_html else ""}
      <div style="margin-top: 24px;">
        <a href="{_html.escape(dashboard_url)}"
           style="background: #0f6e56; color: #fff; padding: 10px 20px;
                  border-radius: 6px; text-decoration: none; font-size: 13px;">
          View Incident →
        </a>
      </div>
    </div>
  </div>
</body>
</html>"""

    body = {
        "personalizations": [{"to": [{"email": r} for r in recipients]}],
        "from":    {"email": ALERT_EMAIL_FROM, "name": "Engram"},
        "subject": f"[{severity.upper()}] {title}",
        "content": [{"type": "text/html", "value": html}],
    }

    try:
        response = httpx.post(
            "https://api.sendgrid.com/v3/mail/send",
            json=body,
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}"},
            timeout=10.0,
        )
        response.raise_for_status()
        logger.info(f"Email alert sent | incident={incident_id} to={recipients}")
    except Exception as e:
        logger.error(f"Email alert failed | incident={incident_id} error={e}")


# ── Public entry point ─────────────────────────────────────────────────────────

def send_incident_alert(
    incident_id: str,
    title: str,
    severity: Optional[str],
    root_cause: str,
    recommended_actions: list[str],
    spike_ratio: str,
    recurrence_note: Optional[str],
    dashboard_base_url: str = "",
) -> None:
    """
    Fire-and-forget alerting. Called after an incident is fully written to DB.
    Sends to every configured channel independently — one failure doesn't
    prevent the other from firing.
    """
    if not SLACK_WEBHOOK_URL and not all([SENDGRID_API_KEY, ALERT_EMAIL_FROM, ALERT_EMAIL_TO]):
        logger.debug("No alert channels configured — skipping notification")
        return

    dashboard_url = (
        f"{dashboard_base_url.rstrip('/')}/incidents/{incident_id}"
        if dashboard_base_url
        else f"http://localhost:8080/incidents/{incident_id}"
    )

    _send_slack(
        incident_id=incident_id,
        title=title,
        severity=severity,
        root_cause=root_cause,
        spike_ratio=spike_ratio,
        recurrence_note=recurrence_note,
        dashboard_url=dashboard_url,
    )

    _send_email(
        incident_id=incident_id,
        title=title,
        severity=severity,
        root_cause=root_cause,
        actions=recommended_actions,
        spike_ratio=spike_ratio,
        recurrence_note=recurrence_note,
        dashboard_url=dashboard_url,
    )
