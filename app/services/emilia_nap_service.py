"""Emilia nap tracker: UK-local semantics, UTC storage, memory sync for RAG."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import AsyncSessionLocal
from app.models.emilia_nap import EmiliaNap
from app.services.datetime_parser import parse_natural_datetime
from app.services.memory_service import create_memory_from_classification

logger = logging.getLogger(__name__)

UK_TZ = ZoneInfo("Europe/London")

_AGO_RE = re.compile(
    r"\b(?P<n>\d+)\s*(?P<unit>minute|minutes|min|mins|hour|hours|hr|hrs)\s+ago\b",
    re.IGNORECASE,
)


def uk_now() -> datetime:
    return datetime.now(UK_TZ)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UK_TZ).astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)


def format_uk(dt: datetime, *, with_seconds: bool = False) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(UK_TZ)
    fmt = "%Y-%m-%d %H:%M:%S %Z" if with_seconds else "%Y-%m-%d %H:%M %Z"
    return local.strftime(fmt)


def format_duration(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total < 0:
        total = 0
    h, r = divmod(total, 3600)
    m, s = divmod(r, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m or not parts:
        parts.append(f"{m}m")
    if s and h == 0 and m < 5:
        parts.append(f"{s}s")
    return " ".join(parts)


def parse_time_hint(hint: Optional[str], *, reference: Optional[datetime] = None) -> datetime:
    """Resolve natural-language time to an aware UTC datetime (UK calendar for 'today')."""
    ref = reference or uk_now()
    if not hint or not str(hint).strip():
        return to_utc(ref)

    s = str(hint).strip()
    ago = _AGO_RE.search(s)
    if ago:
        n = int(ago.group("n"))
        unit = ago.group("unit").lower()
        if unit.startswith("hour") or unit.startswith("hr"):
            return to_utc(ref - timedelta(hours=n))
        return to_utc(ref - timedelta(minutes=n))

    sl = s.lower()
    if sl in {"now", "right now", "just now", "just"}:
        return to_utc(ref)

    # Interpret "today" / relative phrases in UK local wall time
    ref_naive = ref.astimezone(UK_TZ).replace(tzinfo=None)
    parsed = parse_natural_datetime(s, reference=ref_naive)
    if parsed:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UK_TZ)
        return to_utc(parsed)

    return to_utc(ref)


async def _get_open_nap(session: AsyncSession, chat_id: int) -> Optional[EmiliaNap]:
    r = await session.execute(
        select(EmiliaNap)
        .where(EmiliaNap.chat_id == chat_id, EmiliaNap.sleep_ended_at.is_(None))
        .order_by(desc(EmiliaNap.sleep_started_at))
        .limit(1)
    )
    return r.scalar_one_or_none()


async def _sync_memory_line(
    content: str,
    *,
    telegram_message_id: Optional[int] = None,
) -> None:
    try:
        await create_memory_from_classification(
            content=content,
            memory_subtype="note",
            tags=["emilia_nap", "emilia", "family_tracker"],
            original_message=content,
            source_type="telegram",
            telegram_message_id=telegram_message_id,
        )
    except Exception:
        logger.exception("emilia_nap: memory sync failed (nap already saved)")


async def apply_emilia_nap_action(
    *,
    chat_id: int,
    action: str,
    time_hint: Optional[str],
    notes: Optional[str],
    raw_text: str,
    telegram_message_id: Optional[int] = None,
) -> tuple[Optional[int], str]:
    """
    Run nap action for this chat. Returns (emilia_nap_row_id or None, user-facing reply).
    """
    act = (action or "status").strip().lower()
    if chat_id is None:
        return None, "🍼 I need a chat context to log Emilia's naps."

    async with AsyncSessionLocal() as session:
        if act == "start":
            when = parse_time_hint(time_hint)
            open_nap = await _get_open_nap(session, chat_id)
            if open_nap:
                return (
                    open_nap.id,
                    "🍼 There's already a nap in progress (since "
                    f"{format_uk(open_nap.sleep_started_at)}). Say when she woke up first, "
                    "or give a new *start* time to replace it.",
                )
            row = EmiliaNap(chat_id=chat_id, sleep_started_at=when, notes=(notes or "").strip() or None)
            session.add(row)
            await session.flush()
            rid = row.id
            await session.commit()
            line = (
                f"[emilia_nap] Nap started at {format_uk(when)} (UK). "
                f"(id {rid})" + (f" Note: {notes}" if notes else "")
            )
            await _sync_memory_line(line, telegram_message_id=telegram_message_id)
            msg = f"🍼 *Nap started* (UK): {format_uk(when)}"
            if notes:
                msg += f"\n📝 {notes}"
            return rid, msg

        if act == "end":
            when = parse_time_hint(time_hint)
            open_nap = await _get_open_nap(session, chat_id)
            if not open_nap:
                return (
                    None,
                    "🍼 No active nap logged. Say when she went down (e.g. "
                    "*Emilia down for a nap at 1pm*) to start one.",
                )
            if when < open_nap.sleep_started_at:
                return (
                    open_nap.id,
                    "🍼 Wake time can't be before nap start. Check the time or start a new nap.",
                )
            open_nap.sleep_ended_at = when
            if notes and str(notes).strip():
                prev = (open_nap.notes or "").strip()
                extra = str(notes).strip()
                open_nap.notes = f"{prev}\n{extra}".strip() if prev else extra
            rid = open_nap.id
            dur = when - open_nap.sleep_started_at
            await session.commit()
            line = (
                f"[emilia_nap] Nap ended at {format_uk(when)} (UK). Duration {format_duration(dur)}. (id {rid})"
            )
            await _sync_memory_line(line, telegram_message_id=telegram_message_id)
            msg = (
                f"🍼 *Nap ended* (UK): {format_uk(when)}\n"
                f"⏱️ Duration: *{format_duration(dur)}*\n"
                f"Started: {format_uk(open_nap.sleep_started_at)}"
            )
            return rid, msg

        if act == "status":
            ref = uk_now()
            now_utc = to_utc(ref)
            open_nap = await _get_open_nap(session, chat_id)
            if open_nap:
                delta = now_utc - open_nap.sleep_started_at
                return (
                    open_nap.id,
                    f"😴 Emilia has been asleep for *{format_duration(delta)}* "
                    f"(since {format_uk(open_nap.sleep_started_at)} UK).",
                )
            r = await session.execute(
                select(EmiliaNap)
                .where(EmiliaNap.chat_id == chat_id, EmiliaNap.sleep_ended_at.isnot(None))
                .order_by(desc(EmiliaNap.sleep_ended_at))
                .limit(1)
            )
            last = r.scalar_one_or_none()
            if not last:
                return None, "🍼 No nap data yet. Log when she goes down with a quick message."
            delta = now_utc - last.sleep_ended_at
            return (
                last.id,
                f"☀️ Emilia has been awake for *{format_duration(delta)}* "
                f"(woke {format_uk(last.sleep_ended_at)} UK).",
            )

        if act == "log":
            limit = 10
            r = await session.execute(
                select(EmiliaNap)
                .where(EmiliaNap.chat_id == chat_id)
                .order_by(desc(EmiliaNap.sleep_started_at))
                .limit(limit)
            )
            rows = list(r.scalars().all())
            if not rows:
                return None, "🍼 No naps logged yet."
            lines = ["🍼 *Recent naps* _(UK times)_:"]
            for n in rows:
                start_s = format_uk(n.sleep_started_at)
                if n.sleep_ended_at:
                    end_s = format_uk(n.sleep_ended_at)
                    dur = format_duration(n.sleep_ended_at - n.sleep_started_at)
                    line = f"• {start_s} → {end_s} ({dur})"
                else:
                    line = f"• {start_s} → _in progress_"
                if n.notes:
                    line += f"\n  _{n.notes.replace(chr(10), ' ')[:120]}_"
                lines.append(line)
            return None, "\n".join(lines)

        if act == "note":
            note_text = (notes or raw_text or "").strip()
            if not note_text:
                return None, "🍼 What note should I add? (e.g. *Emilia nap note: restless first hour*)"
            open_nap = await _get_open_nap(session, chat_id)
            target = open_nap
            if not target:
                r = await session.execute(
                    select(EmiliaNap)
                    .where(EmiliaNap.chat_id == chat_id, EmiliaNap.sleep_ended_at.isnot(None))
                    .order_by(desc(EmiliaNap.sleep_ended_at))
                    .limit(1)
                )
                target = r.scalar_one_or_none()
            if not target:
                return None, "🍼 No nap to attach a note to yet."
            prev = (target.notes or "").strip()
            target.notes = f"{prev}\n{note_text}".strip() if prev else note_text
            rid = target.id
            await session.commit()
            await _sync_memory_line(
                f"[emilia_nap] Note on nap id {rid}: {note_text}",
                telegram_message_id=telegram_message_id,
            )
            return rid, f"📝 Note saved on nap #{rid}."

    return None, "🍼 I didn't understand that nap action. Try start, end, status, or log."
