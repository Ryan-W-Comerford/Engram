#!/bin/bash
set -e

echo "▶ Running database migrations..."
alembic upgrade head

echo "▶ Starting PulseAI ingestor..."
exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
