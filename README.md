# Engram

Self-hosted application observability. Instrument your app → events flow through Kafka → Claude analyzes error spikes and writes incident reports → dashboard shows you what broke and why.

Engram is **single-tenant**: you run one instance, it watches one system. There are no accounts, projects, or per-app API keys.

## How it works

```
Your App
  │  (Python SDK or any OTel SDK)
  ▼
Ingestor  (:8000)   ← writes to DB + Kafka
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

Dashboard (:8080)    ← read-only UI
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

The only values you **must** set are the two AI keys:

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Claude — for incident analysis |
| `OPENAI_API_KEY` | Yes | text-embedding-3-small — for similarity search |
| `AUTH_TOKEN` | No | Leave blank and Engram runs open (no dashboard login, no token on ingest). Set it to require that token everywhere — see [Access control](#access-control). |
| `POSTGRES_PASSWORD` | No | DB password (default is fine for local dev) |
| `SLACK_WEBHOOK_URL` | No | Incident alerts via Slack |
| `SENDGRID_API_KEY` | No | Incident alerts via email |

### 2. Start the stack

```bash
docker compose up --build
```

Kafka takes ~30 seconds to initialize on first boot. Migrations run automatically and seed the single implicit project — there is nothing to create.

### 3. Open the dashboard

Go to **http://localhost:8080**. If `AUTH_TOKEN` is unset, you're straight in. If it's set, enter that token once.

---

## Access control

Engram has no user accounts. Access is governed by one optional shared secret, `AUTH_TOKEN`:

| `AUTH_TOKEN` | Dashboard | `POST /ingest` | OTel collector |
|---|---|---|---|
| **unset** (default) | no login | open | open |
| **set** | one-field token login | `Authorization: Bearer <token>` required | swap in `otel-collector-config.auth.yaml` (see `docker-compose.yml`) |

Leaving it unset is fine for localhost or a private network. Set it if the ports are reachable from anywhere else, and put the instance behind your own TLS/reverse proxy.

---

## Instrumenting your app

### Option A — Python SDK (FastAPI / Flask)

```bash
pip install -e ./sdk
```

```python
from engram_sdk import Engram

pulse = Engram(
    host="http://localhost:8000",  # or set ENGRAM_HOST env var
    environment="production",
    service="my-api",
    # token="..."  only if this instance sets AUTH_TOKEN (or set ENGRAM_TOKEN)
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

Point your exporter at the collector:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4317"
# only if AUTH_TOKEN is set (and you swapped in otel-collector-config.auth.yaml):
# export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <AUTH_TOKEN>"
```

No `engram.project_id` attribute is needed — everything is attributed to the one implicit project.

```bash
# Python (zero code changes with opentelemetry-instrument)
opentelemetry-instrument \
  --service_name my-api \
  --resource_attributes "deployment.environment=production" \
  python app.py
```

See [QUICKSTART.md](./QUICKSTART.md) for Node.js, Java, Go, Ruby, .NET, and C# examples.

---

## Triggering the anomaly detector

The detector fires when error rate spikes to **1.5× the baseline average** within a 1-minute window. Baseline is calculated from the previous 4 minutes of activity.

### Option A — trigger script (quickest)

```bash
python tests/trigger_incident.py
# add --token <AUTH_TOKEN> only if this instance sets one
```

The script sends 5 baseline errors, waits for the minute to roll over, then fires 20 errors in the new window — enough to trip the 1.5× threshold. It then waits up to 90 seconds and tells you to check the dashboard.

### Option B — example app + curl

```bash
pip install -e "./sdk[dev]"
python sdk/example_app.py     # add ENGRAM_TOKEN=... if AUTH_TOKEN is set
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
# → 🚨 ANOMALY CONFIRMED | errors=47 baseline=1.2 ratio=39.2×
```

Then watch Claude analyze it:
```bash
docker compose logs -f ai
# → Incident enriched | title='Error spike: ValueError in example-api' severity=high
```

The incident appears on your dashboard within seconds.

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
| Ingestor API | http://localhost:8000 | Event ingest (`POST /ingest`) |
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
│   │   ├── models.py          SQLAlchemy models (Event, Incident, DailyDigest)
│   │   ├── session.py         Engine + get_db dependency
│   │   ├── tenant.py          get_or_create_default_project (single-tenant helper)
│   │   └── similarity.py      pgvector cosine similarity helpers
│   ├── otel_normalizer.py     OTel JSON → Engram event schema translation
│   ├── kafka_config.py        Shared Kafka producer/consumer config
│   └── logging_config.py      Structured JSON logging
├── migrations/                Alembic migrations
├── services/
│   ├── ingestor/              FastAPI — event ingest API
│   ├── processor/             Kafka consumer — sliding-window anomaly detection
│   ├── ai/                    Kafka consumer — Claude incident analysis + daily digests
│   └── dashboard/             FastAPI + Jinja2 — read-only dashboard UI
├── sdk/                       Python SDK (FastAPI + Flask auto-instrumentation)
├── tests/
│   ├── trigger_incident.py    Manual incident trigger script (dev/demo)
│   ├── test_anomaly.py
│   ├── test_auth.py
│   ├── test_ingestor_api.py
│   └── test_otel_normalizer.py
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
# Liveness
curl http://localhost:8000/health          # → {"status":"ok"}
curl http://localhost:8080/health          # → {"status":"ok"}

# Ingestor dependency check (Kafka + DB). Add -H "Authorization: Bearer <AUTH_TOKEN>"
# if AUTH_TOKEN is set.
curl http://localhost:8000/internal/health
# → {"status":"ok","kafka":"ok","db":"ok"}
```
