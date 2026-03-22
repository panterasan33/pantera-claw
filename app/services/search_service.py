"""Shared semantic search and question answering helpers."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.interaction_event import InteractionEvent
from app.models.memory import MemoryItem
from app.models.reminder import Reminder
from app.models.task import Task, TaskStatus
from app.services.embedding_service import embed_text

logger = logging.getLogger(__name__)


ACTIONABLE_TASK_QUERY_HINTS = (
    "need to do",
    "todo",
    "to do",
    "pending",
    "remaining",
    "left to",
    "outstanding",
    "open tasks",
)


def is_actionable_task_query(query: str) -> bool:
    """Heuristically detect questions asking for unfinished work."""
    normalized = (query or "").strip().lower()
    return any(hint in normalized for hint in ACTIONABLE_TASK_QUERY_HINTS)


async def semantic_search(
    db: AsyncSession,
    query: str,
    *,
    limit: int = 20,
    limit_per_type: int = 7,
) -> list[dict[str, Any]]:
    """Run semantic search over tasks, reminders, semantic memory, and interaction history."""
    if not query or not query.strip():
        return []

    emb = await embed_text(query.strip())
    if not emb:
        return []

    results: list[dict[str, Any]] = []
    actionable_query = is_actionable_task_query(query)

    task_distance = Task.embedding.cosine_distance(emb).label("distance")
    q_tasks = select(Task, task_distance).where(Task.embedding.isnot(None))
    if actionable_query:
        q_tasks = q_tasks.where(Task.status != TaskStatus.DONE)
    q_tasks = q_tasks.order_by(task_distance).limit(limit_per_type)

    r = await db.execute(q_tasks)
    for t, distance in r.all():
        task_bits = [t.title]
        if t.project:
            task_bits.append(f"project: {t.project}")
        if t.notes:
            task_bits.append(t.notes)
        task_bits.append(f"status: {t.status.value}")
        results.append(
            {
                "type": "task",
                "id": t.id,
                "title": t.title,
                "content": " • ".join(task_bits),
                "score": max(0.0, 1 - float(distance)),
            }
        )

    # Lexical fallback: include non-embedded tasks by matching title/notes/project.
    lexical_query = (
        select(Task)
        .where(
            or_(
                Task.title.ilike(f"%{query.strip()}%"),
                Task.notes.ilike(f"%{query.strip()}%"),
                Task.project.ilike(f"%{query.strip()}%"),
            )
        )
        .order_by(Task.created_at.desc())
        .limit(limit_per_type)
    )
    if actionable_query:
        lexical_query = lexical_query.where(Task.status != TaskStatus.DONE)

    r = await db.execute(lexical_query)
    seen_task_ids = {
        item["id"] for item in results if item.get("type") == "task" and isinstance(item.get("id"), int)
    }
    for t in r.scalars().all():
        if t.id in seen_task_ids:
            continue
        task_bits = [t.title]
        if t.project:
            task_bits.append(f"project: {t.project}")
        if t.notes:
            task_bits.append(t.notes)
        task_bits.append(f"status: {t.status.value}")
        results.append(
            {
                "type": "task",
                "id": t.id,
                "title": t.title,
                "content": " • ".join(task_bits),
                "score": 0.25,
            }
        )

    reminder_distance = Reminder.embedding.cosine_distance(emb).label("distance")
    q_rem = (
        select(Reminder, reminder_distance)
        .where(Reminder.embedding.isnot(None))
        .where(Reminder.is_active == True)
        .order_by(reminder_distance)
        .limit(limit_per_type)
    )
    r = await db.execute(q_rem)
    for rem, distance in r.all():
        results.append(
            {
                "type": "reminder",
                "id": rem.id,
                "title": rem.content[:80],
                "content": rem.content,
                "score": max(0.0, 1 - float(distance)),
            }
        )

    memory_distance = MemoryItem.embedding.cosine_distance(emb).label("distance")
    q_mem = (
        select(MemoryItem, memory_distance)
        .where(MemoryItem.embedding.isnot(None))
        .order_by(memory_distance)
        .limit(limit_per_type)
    )
    r = await db.execute(q_mem)
    for m, distance in r.all():
        results.append(
            {
                "type": "memory",
                "id": m.id,
                "title": m.content[:80],
                "content": m.content,
                "score": max(0.0, 1 - float(distance)),
            }
        )

    event_distance = InteractionEvent.embedding.cosine_distance(emb).label("distance")
    q_events = (
        select(InteractionEvent, event_distance)
        .where(InteractionEvent.embedding.isnot(None))
        .order_by(event_distance)
        .limit(limit_per_type)
    )
    r = await db.execute(q_events)
    for event, distance in r.all():
        results.append(
            {
                "type": "interaction",
                "id": event.id,
                "title": event.summary[:80],
                "content": event.summary,
                "score": max(0.0, 1 - float(distance)),
            }
        )

    return sorted(results, key=lambda item: item.get("score", 0.0), reverse=True)[:limit]


def build_question_answer(query: str, results: list[dict[str, Any]]) -> str:
    """Build a concise answer with source snippets for bot replies (sync fallback)."""
    if not results:
        return (
            "🔍 I couldn't find anything relevant yet. "
            "Try rephrasing your question or save more notes/tasks first."
        )

    top = results[:3]
    lines = [f"🔍 *What I found for:* _{query}_", ""]
    for idx, item in enumerate(top, start=1):
        snippet = (item.get("content") or item.get("title") or "").strip()
        if len(snippet) > 120:
            snippet = f"{snippet[:117]}..."
        score = item.get("score")
        if isinstance(score, float):
            lines.append(f"{idx}. *{item.get('type', 'item').title()}* ({score:.2f}) — {snippet}")
        else:
            lines.append(f"{idx}. *{item.get('type', 'item').title()}* — {snippet}")
    return "\n".join(lines)


async def build_question_answer_llm(query: str, results: list[dict[str, Any]]) -> str:
    """Use an LLM to synthesize a real answer from search results."""
    if not results:
        return (
            "🔍 I couldn't find anything relevant yet. "
            "Try rephrasing your question or save more notes/tasks first."
        )

    context_lines = []
    for item in results[:5]:
        snippet = (item.get("content") or item.get("title") or "").strip()
        if len(snippet) > 300:
            snippet = snippet[:297] + "..."
        context_lines.append(f"[{item.get('type', 'item').upper()}] {snippet}")
    context_block = "\n".join(context_lines)

    prompt = (
        f"You are a personal assistant. Answer the user's question using ONLY the provided context. "
        f"Be concise (2-4 sentences max). If the context doesn't have enough info, say so honestly.\n\n"
        f"Question: {query}\n\n"
        f"Context from your memory:\n{context_block}"
    )

    try:
        settings = get_settings()

        if settings.anthropic_api_key:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            resp = await client.messages.create(
                model="claude-3-haiku-20240307",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            answer = resp.content[0].text.strip()
        elif settings.openai_api_key:
            import openai

            client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            answer = (resp.choices[0].message.content or "").strip()
        else:
            return build_question_answer(query, results)

        source_types = list({item.get("type", "item") for item in results[:5]})
        sources_line = "  _Sources: " + ", ".join(t.title() for t in source_types) + "_"
        return f"🔍 {answer}\n\n{sources_line}"

    except Exception as e:
        logger.warning(f"LLM Q&A failed, falling back to snippets: {e}")
        return build_question_answer(query, results)


async def answer_question(db: AsyncSession, query: str) -> str:
    """Answer a question using retrieval + LLM synthesis (used by orchestrator)."""
    results = await semantic_search(db, query, limit=8)
    if not results:
        return build_question_answer(query, results)
    return await build_question_answer_llm(query, results)
