FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home appuser

COPY services/ai/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Needs shared DB models + session to write incidents
COPY shared/ ./shared/
COPY services/ai/ .

ENV PYTHONPATH=/app

RUN chown -R appuser:appuser /app

USER appuser

CMD ["python", "main.py"]
