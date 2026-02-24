"""Shared semantic search and question answering helpers."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.memory import MemoryItem
from app.models.reminder import Reminder
from app.models.task import Task
from app.services.embedding_service import embed_text


async def semantic_search(
    db: AsyncSession,
    query: str,
    *,
    limit: int = 20,
    limit_per_type: int = 7,
) -> list[dict[str, Any]]:
    """Run semantic search over tasks, reminders and memory."""
    if not query or not query.strip():
        return []

    emb = await embed_text(query.strip())
    if not emb:
        return []

    results: list[dict[str, Any]] = []

    q_tasks = (
        select(Task)
        .where(Task.embedding.isnot(None))
        .order_by(Task.embedding.cosine_distance(emb))
        .limit(limit_per_type)
    )
    r = await db.execute(q_tasks)
    for t in r.scalars().all():
        results.append({"type": "task", "id": t.id, "title": t.title, "content": t.title})

    q_rem = (
        select(Reminder)
        .where(Reminder.embedding.isnot(None))
        .where(Reminder.is_active == True)
        .order_by(Reminder.embedding.cosine_distance(emb))
        .limit(limit_per_type)
    )
    r = await db.execute(q_rem)
    for rem in r.scalars().all():
        results.append(
            {"type": "reminder", "id": rem.id, "title": rem.content[:80], "content": rem.content}
        )

    q_mem = (
        select(MemoryItem)
        .where(MemoryItem.embedding.isnot(None))
        .order_by(MemoryItem.embedding.cosine_distance(emb))
        .limit(limit_per_type)
    )
    r = await db.execute(q_mem)
    for m in r.scalars().all():
        results.append({"type": "memory", "id": m.id, "title": m.content[:80], "content": m.content})

    return results[:limit]


def build_question_answer(query: str, results: list[dict[str, Any]]) -> str:
    """Build a concise answer with source snippets for bot replies."""
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
        lines.append(f"{idx}. *{item.get('type', 'item').title()}* — {snippet}")
    return "\n".join(lines)

