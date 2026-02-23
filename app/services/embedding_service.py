"""
Embedding service for RAG - OpenAI text-embedding-3-small.
"""
import logging
from typing import List, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)


def _get_client():
    try:
        from openai import AsyncOpenAI
        settings = get_settings()
        if not settings.openai_api_key:
            return None
        return AsyncOpenAI(api_key=settings.openai_api_key)
    except Exception as e:
        logger.warning(f"OpenAI client init failed: {e}")
        return None


async def embed_text(text: str) -> Optional[List[float]]:
    """Embed a single text. Returns 1536-dim vector or None if unavailable."""
    if not text or not text.strip():
        return None
    client = _get_client()
    if not client:
        return None
    try:
        r = await client.embeddings.create(
            model="text-embedding-3-small",
            input=text.strip()[:8000],
        )
        return r.data[0].embedding
    except Exception as e:
        logger.warning(f"Embedding failed: {e}")
        return None


async def embed_texts(texts: List[str]) -> List[Optional[List[float]]]:
    """Embed multiple texts. Returns list of vectors (None for failures)."""
    if not texts:
        return []
    client = _get_client()
    if not client:
        return [None] * len(texts)
    try:
        inputs = [t.strip()[:8000] if t else "" for t in texts]
        inputs = [t or " " for t in inputs]
        r = await client.embeddings.create(
            model="text-embedding-3-small",
            input=inputs,
        )
        by_idx = {d.index: d.embedding for d in r.data}
        return [by_idx.get(i) for i in range(len(texts))]
    except Exception as e:
        logger.warning(f"Batch embedding failed: {e}")
        return [None] * len(texts)
