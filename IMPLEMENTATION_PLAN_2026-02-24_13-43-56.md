# Pantera Robust Reclassification Implementation Plan (2026-02-24 13:43:56)

## Goal
Allow users to correct a misclassified message (e.g., reminder -> task) without creating duplicate items, while preserving history, ensuring idempotency, and supporting Telegram follow-up flows.

---

## Success Criteria
1. Tapping "It's a task/reminder/note" on a Telegram confirmation message updates the originally created entity instead of creating unrelated duplicates.
2. Reclassification is idempotent (double-tap / retry does not create extra rows).
3. A durable lineage exists from incoming Telegram message -> current canonical entity.
4. Follow-up prompts (e.g., reminder time) are stateful and update the same reclassification flow.
5. Audit history is preserved (what was originally classified, what it became, when, and why).
6. Tests cover conversion behavior, retries, and callback edge cases.

---

## Current State (Gap Summary)
- Inline keyboard has conversion actions (`change_task_*`, `change_reminder_*`, `change_note_*`) but callback logic is currently placeholder/partial.
- Existing behavior risks creating a new object rather than converting the previously classified one.
- There is no single canonical cross-entity linkage table for classification lifecycle.

---

## Proposed Architecture (Robust Version)

### 1) Classification Event Ledger (new model/table)
Create `classification_events` to track each inbound message and subsequent corrections.

Suggested fields:
- `id` (pk)
- `telegram_message_id` (indexed)
- `chat_id` (indexed)
- `source_kind` (text/voice/image)
- `raw_content`
- `detected_type` (task/reminder/memory/note/disclosure/question)
- `canonical_type` (current resolved type)
- `entity_table` (tasks/reminders/memory)
- `entity_id`
- `status` (active/superseded/error)
- `superseded_by_event_id` (nullable fk to same table)
- `revision_of_event_id` (nullable fk)
- `idempotency_key` (unique)
- `created_at`, `updated_at`

Rationale:
- Provides durable mapping for corrections.
- Allows historical auditing and analytics.
- Central place for idempotency and retries.

### 2) Reclassification Service Layer (new service)
Add `app/services/reclassification_service.py` with explicit conversion routines:
- `convert_task_to_reminder(...)`
- `convert_reminder_to_task(...)`
- `convert_*_to_note(...)`
- `reclassify_event(event_id, target_type, context)` orchestration

Service responsibilities:
- Open transaction.
- Load canonical event + target entity.
- If target already reached (idempotent repeat), return current mapping.
- Create converted target entity with normalized field mapping.
- Mark old entity archived/superseded (or inactive where appropriate).
- Update canonical event row.
- Return conversion result payload for message rendering.

### 3) Pending Follow-up State for Multi-step Corrections
For conversions that require more information (e.g., reminder time):
- Add a small `pending_user_actions` table or use event table JSON state:
  - `event_id`, `chat_id`, `action_type`, `required_fields`, `state_payload`, `expires_at`.
- When user taps conversion requiring extra details, prompt and persist pending state.
- Next user reply resolves pending action and updates same event/entity.

### 4) Callback Handler Refactor
In `app/bot/handlers.py`:
- Replace callback TODO branches with service calls.
- Parse callback payload into strongly typed action.
- Resolve original `classification_event` by callback context.
- Call reclassification service.
- Edit message with deterministic confirmation text:
  - "Converted reminder #123 -> task #456".
- Handle stale callbacks gracefully.

### 5) Domain Rules and Data Mapping
Define explicit mapping matrix:
- Reminder -> Task:
  - `title = reminder.content`
  - `notes` include previous trigger metadata
  - deactivate reminder (`is_active=False`) and tag superseded
- Task -> Reminder:
  - `content = task.title + notes`
  - require or infer trigger; if missing, enter pending flow
  - optionally keep task as archived reference
- Note/Memory conversions:
  - preserve source content and event metadata where possible

### 6) Idempotency + Concurrency Guarantees
- Use `idempotency_key` on callback processing (`callback_query.id` + target + event_id).
- Add row-level lock when mutating canonical event (`SELECT ... FOR UPDATE`).
- Unique constraints to prevent duplicate active canonical rows per event lineage.

### 7) Observability
- Structured logs for conversion start/success/failure.
- Metrics counters:
  - `reclassify_attempt_total`
  - `reclassify_success_total`
  - `reclassify_idempotent_hit_total`
  - `reclassify_error_total`

---

## Implementation Phases

### Phase 0: Design + Migration Planning
- Finalize schema and conversion policies.
- Write migration scripts for new tables and indexes.
- Backward compatibility strategy for existing rows.

### Phase 1: Event Ledger + Write Path
- Create `classification_events` model and migration.
- Persist event rows on all ingest paths (text, voice, image).
- Link each created entity to canonical event.

### Phase 2: Reclassification Service
- Implement conversion service with transactional guarantees.
- Implement idempotency checks and supersession updates.

### Phase 3: Telegram Callback Integration
- Wire callback actions to service.
- Add pending-flow prompts for missing required fields.
- Add stale-action handling and user feedback strings.

### Phase 4: Tests
- Unit tests for mapping functions and idempotency.
- Integration tests for callback flows and DB mutations.
- Regression tests for duplicate prevention.

### Phase 5: Rollout
- Feature flag `ROBUST_RECLASSIFICATION_ENABLED`.
- Dry-run logging mode (optional) before fully active.
- Enable metrics dashboard + error alerts.

---

## Test Plan (to execute during implementation)
1. Convert reminder -> task via callback; verify one active canonical entity.
2. Tap callback twice; verify idempotent response and no duplicate rows.
3. Convert task -> reminder without trigger; verify pending prompt + completion.
4. Process stale callback after prior conversion; verify safe no-op messaging.
5. Verify history chain (`revision_of_event_id`, `superseded_by_event_id`).
6. Voice-message path has same event linkage behavior.

---

## Risks and Mitigations
- **Risk:** Duplicate entities due to callback retries.
  - **Mitigation:** idempotency keys + row locks + unique constraints.
- **Risk:** Partial conversion on failure.
  - **Mitigation:** single transaction boundary in service layer.
- **Risk:** Complex UX for missing reminder details.
  - **Mitigation:** explicit pending-action state machine with timeout.
- **Risk:** Data drift between event ledger and entities.
  - **Mitigation:** invariants + consistency checks in tests.

---

## Estimated Effort (Robust Version)
- Design + migrations: 0.5-1 day
- Service + callbacks: 1.5-2.5 days
- Pending state flow: 0.5-1 day
- Tests + hardening: 1-1.5 days
- Rollout + monitoring: 0.5 day

**Total:** ~4-6 days

---

## Out of Scope for This Plan File
- No production code changes are implemented in this step.
- No schema migration has been run.
- No handler behavior has been altered.
