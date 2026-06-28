"""
services/ai/embedder.py

Generates vector embeddings for incident summaries using OpenAI's
text-embedding-3-small model (1536 dimensions).

Why OpenAI for embeddings and Anthropic for generation:
  Anthropic doesn't offer an embeddings API. OpenAI's text-embedding-3-small
  is the industry standard — cheap ($0.02/1M tokens), fast, and pairs cleanly
  with pgvector cosine similarity. Using two providers for two distinct tasks
  is standard practice.

The text we embed is a structured summary of what the incident was about:
  title + root cause + exception types + affected endpoints

This captures semantic meaning so that "NullPointerException in payment
validation after schema migration" finds "AttributeError in checkout after
DB column rename" as highly similar — even though the words differ.
"""

import logging
import os
from typing import Optional

from openai import OpenAI

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMS  = 1536

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY environment variable is not set")
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def build_embedding_text(
    title: str,
    root_cause: str,
    exception_types: list[str],
    affected_endpoints: list[str],
) -> str:
    """
    Construct the text that will be embedded.

    Structured prose gives better semantic results than raw JSON because
    the embedding model was trained on natural language, not key-value pairs.
    Keep it under ~500 tokens — the model handles longer inputs fine but
    compresses meaning better at this length.
    """
    exc_str      = ", ".join(exception_types) if exception_types else "unknown"
    endpoint_str = ", ".join(affected_endpoints) if affected_endpoints else "unknown"

    return (
        f"Incident: {title}. "
        f"Root cause: {root_cause}. "
        f"Exception types: {exc_str}. "
        f"Affected endpoints: {endpoint_str}."
    )


def generate_embedding(text: str) -> Optional[list[float]]:
    """
    Call OpenAI to embed the given text.
    Returns a list of 1536 floats, or None if the call fails.

    Failures are non-fatal — the incident is already written to the DB.
    Missing embeddings just means that incident won't appear in future
    similarity searches until the embedding is backfilled.
    """
    try:
        client = _get_client()
        response = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text,
            dimensions=EMBEDDING_DIMS,
        )
        embedding = response.data[0].embedding
        logger.debug(f"Embedding generated | dims={len(embedding)} model={EMBEDDING_MODEL}")
        return embedding
    except Exception as e:
        logger.error(f"OpenAI embedding failed: {e}")
        return None


def embed_incident(analysis: dict) -> Optional[list[float]]:
    """
    Convenience wrapper — extracts fields from a Claude analysis dict,
    builds the embedding text, and returns the embedding vector.
    """
    text = build_embedding_text(
        title             = analysis.get("title", ""),
        root_cause        = analysis.get("root_cause_hypothesis", ""),
        exception_types   = analysis.get("exception_types_seen", []),
        affected_endpoints= analysis.get("affected_endpoints", []),
    )
    logger.info(f"Embedding incident | text_preview={text[:100]!r}")
    return generate_embedding(text)
