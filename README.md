# Engram

Real-time application observability. Instrument your app → events flow through Kafka → Claude analyzes error spikes and writes incident reports → dashboard shows you what broke and why.

## How it works

```
Your App
  │  (Python SDK or any OTel SDK)
  ▼
Ingestor  (:8000)   ← validates API key, writes to DB + Kafka
  │
  ├── PostgreSQL     ← event + incident storage
  │
  └── Kafka
        │
        ├── Processor  ← sliding-window anomaly detection
        │     │         fires when errors spike > 1.5× baseline
        │     ▼
        └── AI Service ← calls Claude for incident analysis
                         stores structured report + embedding
                         fires Slack / email alerts

Dashboard (:8080)    ← read-only UI, session-auth via project API key
```

---

## Prerequisites

- Docker + Docker Compose
- An [Anthropic API key](https://console.anthropic.com/) (for incident analysis)
- An [OpenAI API key](https://platform.openai.com/api-keys) (for similarity embeddings — effectively free at low volume)

---

## Quick Start

### 1. Configure environment

```bash
cp .env.example .env
```

Open `.env` and fill in the required values:

| Variable | Required | Description |
|---|---|---|
| `ADMIN_API_KEY` | Yes | Protects `POST /projects`. Set to any strong secret you choose. |
| `ANTHROPIC_API_KEY` | Yes | Claude — for incident analysis |
| `OPENAI_API_KEY` | Yes | text-embedding-3-small — for similarity search |
| `DASHBOARD_SECRET_KEY` | Yes | Signs session cookies. Generate: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `OTEL_BEARER_TOKEN` | Yes | Authenticates OTel exporters. Generate the same way. |
| `POSTGRES_PASSWORD` | Yes | DB password (default `pulseai_dev` is fine for local dev) |
| `GITHUB_WEBHOOK_SECRET` | No | Enables GitHub push → deployment event correlation |
| `SLACK_WEBHOOK_URL` | No | Incident alerts via Slack |
| `SENDGRID_API_KEY` | No | Incident alerts via email |

### 2. Start the stack

```bash
docker compose up --build
```

Kafka takes ~30 seconds to initialize on first boot. Wait until you see the ingestor log:
```
AI service started | topic=engram.alerts.anomalies
```

### 3. Create your first project

```bash
curl -s -X POST http://localhost:8000/projects \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: <your-ADMIN_API_KEY>" \
  -d '{"name": "my-app"}' | python3 -m json.tool
```

Response:
```json
{
  "project_id": "abb7108b-...",
  "name": "my-app",
  "api_key": "pk_live_ef568530...",
  "created_at": "2026-01-01T00:00:00"
}
```

**Save the `api_key` — it is shown only once.** This is what you use to sign in to the dashboard and authenticate your app's events.

### 4. Open the dashboard

Go to **http://localhost:8080** and paste your `api_key` to sign in.

---

## Instrumenting your app

### Option A — Python SDK (FastAPI / Flask)

```bash
pip install -e ./sdk
```

```python
from engram_sdk import Engram

pulse = Engram(
    api_key="pk_live_...",         # or set ENGRAM_API_KEY env var
    host="http://localhost:8000",  # or set ENGRAM_HOST env var
    environment="production",
    service="my-api",
)
pulse.auto_instrument(app)  # works with FastAPI and Flask
```

Manual capture (for caught exceptions you still want to track):

```python
try:
    result = call_external_api()
except TimeoutError as e:
    pulse.capture_error(
        exception_type="TimeoutError",
        message=str(e),
        endpoint="/api/payments",
        method="POST",
    )
```

### Option B — OpenTelemetry (any language)

Set two environment variables before starting your app:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4317"
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <your-OTEL_BEARER_TOKEN>"
```

Then add the `engram.project_id` resource attribute so Engram knows which project to route events to:

```bash
# Python (zero code changes with opentelemetry-instrument)
opentelemetry-instrument \
  --service_name my-api \
  --resource_attributes "engram.project_id=<your-project-id>,deployment.environment=production" \
  python app.py
```

See [QUICKSTART.md](./QUICKSTART.md) for Node.js, Java, Go, Ruby, .NET, and C# examples.

---

## Environment strategy

Create a separate project per environment so dev/QA noise never reaches your production incident feed:

```bash
for env in dev staging production; do
  curl -s -X POST http://localhost:8000/projects \
    -H "Content-Type: application/json" \
    -H "X-Admin-Key: <your-ADMIN_API_KEY>" \
    -d "{\"name\": \"my-app-$env\"}" | python3 -m json.tool
done
```

Each project gets its own API key and isolated dashboard view.

---

## Triggering the anomaly detector

The detector fires when error rate spikes to **1.5× the baseline average** within a 1-minute window. Baseline is calculated from the previous 4 minutes of activity.

The included example app makes this easy to test:

```bash
export ENGRAM_API_KEY="pk_live_..."
pip install -e "./sdk[dev]"
python sdk/example_app.py
# → listening on http://localhost:9000
```

In a second terminal, hammer the error endpoint:

```bash
# macOS
for i in $(seq 1 120); do curl -s http://localhost:9000/explode > /dev/null; done

# or with watch (Linux/macOS with watch installed)
watch -n 0.5 curl -s http://localhost:9000/explode
```

Watch the processor detect the spike:
```bash
docker compose logs -f processor
# → 🚨 ANOMALY CONFIRMED [project-id] | errors=47 baseline=1.2 ratio=39.2×
```

Then watch Claude analyze it:
```bash
docker compose logs -f ai
# → Incident enriched | title='Error spike: ValueError in example-api' severity=high
```

The incident appears on your dashboard within seconds.

---

## GitHub webhook (deployment correlation)

When an error spike follows a deployment, Claude will correlate them. Set it up:

1. Set `GITHUB_WEBHOOK_SECRET` in `.env` to any strong secret
2. Restart the ingestor: `docker compose restart ingestor`
3. Add the webhook in your GitHub repo → Settings → Webhooks:
   - **Payload URL:** `http://<your-host>:8000/webhooks/github/<project-id>`
   - **Content type:** `application/json`
   - **Secret:** your `GITHUB_WEBHOOK_SECRET`
   - **Events:** just the `push` event

---

## Alerts

### Slack

1. Create a Slack app at api.slack.com/apps and enable Incoming Webhooks
2. Set `SLACK_WEBHOOK_URL` in `.env`
3. Restart the ai service: `docker compose restart ai`

### Email (SendGrid)

1. Get an API key at app.sendgrid.com/settings/api_keys
2. Set `SENDGRID_API_KEY`, `ALERT_EMAIL_FROM`, and `ALERT_EMAIL_TO` in `.env`
3. Restart the ai service: `docker compose restart ai`

---

## Services

| Service | URL | Description |
|---|---|---|
| Dashboard | http://localhost:8080 | Main UI |
| Ingestor API | http://localhost:8000 | Event ingest + admin |
| Ingestor docs | http://localhost:8000/docs | Swagger UI |
| OTel gRPC | localhost:4317 | OTel exporter endpoint |
| OTel HTTP | localhost:4318 | OTel HTTP exporter endpoint |
| Kafka | localhost:9094 | External broker (for debugging) |
| PostgreSQL | localhost:5432 | Direct DB access |

---

## Project structure

```
Engram/
├── shared/
│   ├── db/
│   │   ├── models.py          SQLAlchemy models (Project, Event, Incident, DailyDigest)
│   │   ├── session.py         Engine + get_db dependency
│   │   └── similarity.py      pgvector cosine similarity helpers
│   ├── otel_normalizer.py     OTel JSON → Engram event schema translation
│   ├── kafka_config.py        Shared Kafka producer/consumer config
│   └── logging_config.py      Structured JSON logging
├── migrations/                Alembic migrations
├── services/
│   ├── ingestor/              FastAPI — event ingest + project admin API
│   ├── processor/             Kafka consumer — sliding-window anomaly detection
│   ├── ai/                    Kafka consumer — Claude incident analysis + daily digests
│   └── dashboard/             FastAPI + Jinja2 — read-only dashboard UI
├── sdk/                       Python SDK (FastAPI + Flask auto-instrumentation)
├── docker-compose.yml
├── otel-collector-config.yaml
├── .env.example
└── QUICKSTART.md              OTel quickstart for all supported languages
```

---

## Running Alembic locally

```bash
pip install -r services/ingestor/requirements.txt
export DATABASE_URL=postgresql://pulseai:pulseai_dev@localhost:5432/pulseai
alembic upgrade head
alembic revision --autogenerate -m "describe your change"
```

---

## Health checks

```bash
# Ingestor (Kafka + DB)
curl http://localhost:8000/health
# → {"status":"ok","kafka":"ok","db":"ok"}

# Dashboard
curl http://localhost:8080/health
# → {"status":"ok"}
```
