"""Record LLM API usage for analytics; failures must never break product flows."""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from app.db.database import AsyncSessionLocal
from app.models.llm_usage import LlmUsageEvent

logger = logging.getLogger(__name__)


def _openai_chat_usage(response: Any) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    u = getattr(response, "usage", None)
    if u is None:
        return None, None, None
    inp = getattr(u, "prompt_tokens", None)
    out = getattr(u, "completion_tokens", None)
    tot = getattr(u, "total_tokens", None)
    return inp, out, tot


def _openai_embedding_usage(response: Any) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    u = getattr(response, "usage", None)
    if u is None:
        return None, None, None
    pt = getattr(u, "prompt_tokens", None)
    tt = getattr(u, "total_tokens", None)
    return pt, None, tt if tt is not None else pt


def _anthropic_message_usage(response: Any) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    u = getattr(response, "usage", None)
    if u is None:
        return None, None, None
    inp = getattr(u, "input_tokens", None)
    out = getattr(u, "output_tokens", None)
    tot = (inp + out) if inp is not None and out is not None else None
    return inp, out, tot


async def record_llm_usage(
    *,
    provider: str,
    model: str,
    operation: str,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
) -> None:
    try:
        async with AsyncSessionLocal() as session:
            session.add(
                LlmUsageEvent(
                    provider=provider,
                    model=model,
                    operation=operation,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                )
            )
            await session.commit()
    except Exception:
        logger.debug("Failed to record LLM usage", exc_info=True)


async def record_from_openai_chat(*, model: str, operation: str, response: Any) -> None:
    inp, out, tot = _openai_chat_usage(response)
    await record_llm_usage(
        provider="openai",
        model=model,
        operation=operation,
        input_tokens=inp,
        output_tokens=out,
        total_tokens=tot,
    )


async def record_from_openai_embedding(*, model: str, response: Any) -> None:
    inp, out, tot = _openai_embedding_usage(response)
    await record_llm_usage(
        provider="openai",
        model=model,
        operation="embedding",
        input_tokens=inp,
        output_tokens=out,
        total_tokens=tot,
    )


async def record_from_anthropic_message(*, model: str, operation: str, response: Any) -> None:
    inp, out, tot = _anthropic_message_usage(response)
    await record_llm_usage(
        provider="anthropic",
        model=model,
        operation=operation,
        input_tokens=inp,
        output_tokens=out,
        total_tokens=tot,
    )


async def record_whisper_call(*, model: str, response: Any) -> None:
    """Whisper often omits token usage; still log the call."""
    u = getattr(response, "usage", None)
    inp = out = tot = None
    if u is not None:
        inp = getattr(u, "prompt_tokens", None) or getattr(u, "input_tokens", None)
        out = getattr(u, "completion_tokens", None) or getattr(u, "output_tokens", None)
        tot = getattr(u, "total_tokens", None)
    await record_llm_usage(
        provider="openai",
        model=model,
        operation="whisper",
        input_tokens=inp,
        output_tokens=out,
        total_tokens=tot,
    )
