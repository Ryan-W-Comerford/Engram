FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home appuser

# Install Python deps first (layer cached unless requirements.txt changes)
COPY services/ingestor/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared module and migrations so entrypoint can run alembic
COPY shared/ ./shared/
COPY migrations/ ./migrations/
COPY alembic.ini .

# Copy ingestor service code
COPY services/ingestor/ .

# shared/ is importable as a top-level package
ENV PYTHONPATH=/app

COPY services/ingestor/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
