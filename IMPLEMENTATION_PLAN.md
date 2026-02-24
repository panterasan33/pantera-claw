# Pantera Implementation Plan

This plan converts the current partial functionality into a staged roadmap with effort, dependencies, and concrete file targets.

## Scope and goals

- Close the highest-impact user-facing gaps first (bot Q&A, callback corrections, slash commands).
- Improve reliability and data correctness (recurrence math, inbox-first ingest).
- Add observability and tests as each phase ships.

## Guiding principles

1. **Ship vertical slices** (bot + API + UI + tests) instead of broad rewrites.
2. **Prefer shared services** over duplicating logic between bot and web routes.
3. **Keep behavior backward compatible** where possible.
4. **Add instrumentation** for every new workflow (success/failure counters, structured logs).

---

## Phase 0 — Foundation and safety rails (0.5–1 day)

### Deliverables
- Add missing tests scaffold for service-level and API-level behavior.
- Add lightweight metrics/logging conventions for key workflows.
- Add feature flags for risky changes where needed.

### Tasks
- Create `tests/` structure and baseline fixtures for async DB sessions.
- Add structured log events around classifier outputs and callback actions.
- Add config toggles for experimental flows (e.g., inbox-first ingest).

### Files likely touched
- `app/config.py`
- `app/db/database.py`
- `main.py`
- `requirements.txt` (if test deps are missing)

### Exit criteria
- Baseline test command passes locally.
- New logging events visible for bot message handling and callback handling.

---

## Phase 1 — Bot question answering (RAG) (1–2 days)

### Why now
Question classification already exists, but answer generation is still placeholder text.

### Deliverables
- Real bot Q&A response path using semantic retrieval.
- Top-hit citation snippets in bot replies.
- Graceful fallback when embeddings/provider unavailable.

### Tasks
- Introduce a shared search/Q&A service (e.g., `app/services/search_service.py`).
- Reuse embedding and retrieval logic currently in `/api/search`.
- Update bot `QUESTION` branch to call shared service and format answer.
- Add timeout/error handling with user-friendly fallback.

### Files likely touched
- `app/bot/handlers.py`
- `app/web/api.py`
- `app/services/embedding_service.py`
- `app/services/search_service.py` (new)

### Exit criteria
- Bot returns actual answers for known seeded data.
- API search behavior remains unchanged or improved.

---

## Phase 2 — Reclassification callbacks that truly mutate data (1–2 days)

### Why now
Buttons exist for reclassification, but currently only update message text.

### Deliverables
- Callback actions persist reclassification results.
- Data migration path between task/reminder/note/memory representations.
- Idempotent handling for repeated taps.

### Tasks
- Add persistence logic for `change_task_*`, `change_reminder_*`, `change_note_*`.
- Add helper conversion functions in dedicated service module.
- Store provenance metadata (original type/source/message id).
- Add user confirmation messages with resulting object IDs.

### Files likely touched
- `app/bot/handlers.py`
- `app/services/task_service.py`
- `app/services/reminder_service.py`
- `app/models/inbox.py` (if used as source of truth)

### Exit criteria
- Callback tap causes DB state transition, not just UI text change.
- Regression tests cover each transition path.

---

## Phase 3 — Slash commands parity with help text (1 day)

### Deliverables
- Implement `/tasks`, `/today`, `/reminders`, `/search`, `/projects`.
- Add pagination/limits for chat-safe responses.

### Tasks
- Add command handlers in bot module.
- Query DB with concise formatting and optional filters.
- Add robust argument parsing for `/search` and optional project filtering.

### Files likely touched
- `app/bot/bot.py`
- `app/bot/handlers.py`
- `app/models/task.py`

### Exit criteria
- Every command listed in `/help` is functional.
- Commands return deterministic output with >20 records (truncated + “more” indicator).

---

## Phase 4 — Document ingestion pipeline (1–2 days)

### Deliverables
- Actual processing for uploaded docs (txt/pdf/docx initial support).
- Text extraction -> classification -> persistence flow.
- Clear unsupported-format messaging.

### Tasks
- Implement document parser utility and MIME/type guards.
- Feed extracted text into existing classifier and persistence services.
- Store raw/processed data for review and reprocessing.

### Files likely touched
- `app/bot/handlers.py`
- `app/models/inbox.py`
- `app/services/classifier.py`
- `app/services/document_service.py` (new)

### Exit criteria
- Uploading supported docs creates actionable records.
- Unsupported docs fail gracefully with actionable feedback.

---

## Phase 5 — Inbox-first ingest architecture (2–3 days)

### Deliverables
- All inbound content (text/voice/photo/doc) lands in `inbox_items` first.
- Processing state machine (`new -> classified -> persisted -> confirmed`).
- Admin/web tooling for replaying failed items.

### Tasks
- Refactor handlers to always create `InboxItem` first.
- Move classifier + persistence into a single processing service.
- Add retry and dead-letter semantics for failures.

### Files likely touched
- `app/bot/handlers.py`
- `app/models/inbox.py`
- `app/web/api.py`
- `app/services/ingest_service.py` (new)

### Exit criteria
- 100% of inbound items auditable in inbox table.
- Failed processing is recoverable without manual DB edits.

---

## Phase 6 — Recurrence correctness and temporal reliability (1–2 days)

### Deliverables
- Calendar-accurate monthly/yearly recurrence behavior.
- Timezone-safe next-trigger calculations.
- Consistent reminder progression between creation, nudge, and snooze flows.

### Tasks
- Replace approximate `+30/+365` logic with calendar-aware recurrence rules.
- Normalize all scheduling operations to configured timezone.
- Add edge-case tests (month-end, leap year, DST transitions).

### Files likely touched
- `app/services/reminder_service.py`
- `app/jobs/reminder_nudge.py`
- `app/config.py`

### Exit criteria
- No drift across monthly/yearly reminders over repeated cycles.
- Test suite covers temporal edge cases.

---

## Phase 7 — Search quality upgrades (1–2 days)

### Deliverables
- Unified ranked results across task/reminder/memory.
- Include relevance score and optional type filters.
- Snippet generation for more useful result previews.

### Tasks
- Return `score`/`distance` and globally sort merged results.
- Add optional query params (`type`, `limit`, `min_score`).
- Tune default limits and add dedupe logic.

### Files likely touched
- `app/web/api.py`
- `app/services/search_service.py`
- `app/web/static/index.html`

### Exit criteria
- Higher relevance at top results in seeded evaluation set.
- API and UI both display relevance consistently.

---

## Phase 8 — API correctness and DX cleanup (1 day)

### Deliverables
- Fix task `parent_id` filter behavior.
- Align README status markers with implementation reality.
- Add changelog note for shipped improvements.

### Tasks
- Correct query logic branch in task listing endpoint.
- Add/refresh docs section for partial vs complete features.
- Validate endpoint behavior with targeted API tests.

### Files likely touched
- `app/web/api.py`
- `README.md`
- `ARCHITECTURE.md`

### Exit criteria
- Subtask filtering works as expected.
- Documentation reflects true state of implementation.

---

## Suggested sequencing and milestones

### Milestone A (week 1)
- Phase 0, 1, 2
- Outcome: Bot becomes meaningfully useful in chat interactions.

### Milestone B (week 2)
- Phase 3, 4, 5
- Outcome: Complete ingestion and command UX parity.

### Milestone C (week 3)
- Phase 6, 7, 8
- Outcome: Reliability, relevance, and maintainability hardening.

---

## Effort summary

- **Small (S):** Phase 0, 3, 8
- **Medium (M):** Phase 1, 2, 4, 6, 7
- **Large (L):** Phase 5

Estimated total: **~10–16 engineering days** depending on test depth and deployment cadence.

## Risks and mitigations

- **Risk:** Recurrence/timezone regressions.
  - **Mitigation:** Add deterministic time-freeze tests and timezone fixtures.
- **Risk:** Reclassification causes data duplication.
  - **Mitigation:** Idempotency keys + conversion audits.
- **Risk:** LLM provider variability in classification/Q&A.
  - **Mitigation:** strict fallbacks, bounded retries, and confidence gating.

## Definition of done (project-level)

- No TODO placeholders in critical user flows (question, reclassify, document).
- Help text matches implemented command set.
- Inbound content is traceable and recoverable.
- Reminder scheduling is calendar-correct.
- Search is ranked and explainable.
