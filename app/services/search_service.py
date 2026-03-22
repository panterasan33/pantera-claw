"""Shared semantic search and question answering helpers."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.inbox import InboxItem
from app.models.interaction_event import InteractionEvent
from app.models.memory import MemoryItem
from app.models.reminder import Reminder
from app.models.task import Task, TaskStatus
from app.services.embedding_service import embed_text
from app.services.llm_usage_service import record_from_anthropic_message, record_from_openai_chat

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
        logger.warning("semantic_search: no embedding (check OPENAI_API_KEY); using lexical matches only")

    results: list[dict[str, Any]] = []
    actionable_query = is_actionable_task_query(query)
    qterm = query.strip()
    q_lower = qterm.lower()

    if emb:
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
                Task.title.ilike(f"%{qterm}%"),
                Task.notes.ilike(f"%{qterm}%"),
                Task.project.ilike(f"%{qterm}%"),
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

    if emb:
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

    q_rem_lex = (
        select(Reminder)
        .where(Reminder.is_active == True, Reminder.content.ilike(f"%{qterm}%"))
        .order_by(Reminder.created_at.desc())
        .limit(limit_per_type)
    )
    r = await db.execute(q_rem_lex)
    seen_rem = {item["id"] for item in results if item.get("type") == "reminder"}
    for rem in r.scalars().all():
        if rem.id in seen_rem:
            continue
        results.append(
            {
                "type": "reminder",
                "id": rem.id,
                "title": rem.content[:80],
                "content": rem.content,
                "score": 0.24,
            }
        )

    if emb:
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

    q_mem_lex = (
        select(MemoryItem)
        .where(MemoryItem.content.ilike(f"%{qterm}%"))
        .order_by(MemoryItem.created_at.desc())
        .limit(limit_per_type)
    )
    r = await db.execute(q_mem_lex)
    seen_mem = {item["id"] for item in results if item.get("type") == "memory"}
    for m in r.scalars().all():
        if m.id in seen_mem:
            continue
        results.append(
            {
                "type": "memory",
                "id": m.id,
                "title": m.content[:80],
                "content": m.content,
                "score": 0.23,
            }
        )

    if emb:
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

    q_ev_lex = (
        select(InteractionEvent)
        .where(InteractionEvent.summary.ilike(f"%{qterm}%"))
        .order_by(InteractionEvent.created_at.desc())
        .limit(limit_per_type)
    )
    r = await db.execute(q_ev_lex)
    seen_ev = {item["id"] for item in results if item.get("type") == "interaction"}
    for event in r.scalars().all():
        if event.id in seen_ev:
            continue
        results.append(
            {
                "type": "interaction",
                "id": event.id,
                "title": event.summary[:80],
                "content": event.summary,
                "score": 0.22,
            }
        )

    inbox_keywords = ("inbox", "capture", "telegram capture", "raw message", "unprocessed")
    want_inbox_browse = any(k in q_lower for k in inbox_keywords)
    q_inbox = select(InboxItem).order_by(InboxItem.created_at.desc()).limit(limit_per_type)
    if not want_inbox_browse:
        q_inbox = (
            select(InboxItem)
            .where(
                or_(
                    InboxItem.raw_content.ilike(f"%{qterm}%"),
                    InboxItem.processed_content.ilike(f"%{qterm}%"),
                    InboxItem.classification.ilike(f"%{qterm}%"),
                )
            )
            .order_by(InboxItem.created_at.desc())
            .limit(limit_per_type)
        )
    r = await db.execute(q_inbox)
    for row in r.scalars().all():
        snippet = (row.processed_content or row.raw_content or "")[:200]
        results.append(
            {
                "type": "inbox",
                "id": row.id,
                "title": (row.raw_content or "")[:80],
                "content": f"{snippet} • {row.classification or 'unclassified'} • processed={row.is_processed}",
                "score": 0.35 if want_inbox_browse else 0.26,
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
            model_id = "claude-3-haiku-20240307"
            resp = await client.messages.create(
                model=model_id,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            await record_from_anthropic_message(model=model_id, operation="search_qa", response=resp)
            answer = resp.content[0].text.strip()
        elif settings.openai_api_key:
            import openai

            client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
            model_id = "gpt-4o-mini"
            resp = await client.chat.completions.create(
                model=model_id,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            await record_from_openai_chat(model=model_id, operation="search_qa", response=resp)
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
