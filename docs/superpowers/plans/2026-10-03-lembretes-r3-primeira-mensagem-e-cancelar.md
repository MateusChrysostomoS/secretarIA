# Lembretes R3 — Primeira mensagem no chat e caminho do Cancelar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A patient with a live future appointment who writes after 6 h of silence sees that appointment's reminder (with Confirmar / Cancelar / Outro) as the first message; "Cancelar" opens Remarcar esta / Marcar outra / Não vou mais; "Marcar outra" cancels the original only when the new booking is confirmed; the Portal decodes these buttons, scoped to the conversation's patient.

**Architecture:** Four pieces, each behind the clinic switch `Tenant.reminders_v2_enabled`. (1) **Opening:** a pure-ish decision service (`services/reminder_opening.py`) chosen in `workers/turn_router.py` after the consent gate and the silence floors, instead of the returning-patient offer; the card is sent by `workers/shared/reminder_opening.py` from `_send_bot_reply_inner`, backed by ONE `kind='chat'` row per appointment version (R1's unique key), and the patient's own message is then routed as usual with the generic menu suppressed. (2) **Cancel path:** R2's `handle_reminder_button` is rewritten to offer the three-way card, a confirm card for "Não vou mais" (Pix-aware) and to hand "Outro" to the AI; it returns *continuations* that `workers/shared/actions.py` runs through its existing appointment-scoped branches (Pix limits included). (3) **"Marcar outra":** additive column `conversations.flow_replaces_appointment_id`, carried by the router only while the conversation stays in the booking, shown on the confirmation card, consumed by `services/appointment_replacement.py` in the SAME transaction that writes the new `Appointment` (flow tail and Portal hold promotion). (4) **Portal:** `workers/portal/inbound.py` decodes the reminder ids after the existing last-10-cards check.

**Tech Stack:** Python 3.12, SQLAlchemy 2 async (Postgres in prod, SQLite in tests), Alembic, arq, pytest + pytest-asyncio (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-10-03-lembretes-e-confirmacao-design.md` — implements §4.3 entirely, plus the §7 row "Dois lembretes (abertura do chat e cron) sem deduplicação" and the R3 half of "Toque de ação não escopado ao paciente". Code base: `main` b72c4ad + R1 + R2 + TASK-030 P1/P2a/P2b/P3. Worktree: `C:\TECH\BRAIN-worktrees\TASK-032\secretarIA`. Paths are relative to the repo root; Python package root is `src/secretaria/`.

## Global Constraints

- **Depends on (STOP if missing):** R1 (`docs/superpowers/plans/2026-10-03-lembretes-r1-fundacao.md`) and R2 (`.../2026-10-03-lembretes-r2-motor.md`) merged into this worktree; TASK-030 P1, P2a, P2b and P3 (`C:\TECH\BRAIN-worktrees\TASK-030\secretarIA\docs\superpowers\plans\2026-10-02-ia-p*.md`) executed/merged here too (spec §4.5 "Coordenação": R3 runs after them). Task 1 Step 1 checks all of it. This plan starts from the code **as it is after those plans**: where they edit the same function, anchors below are function names plus a line that exists both before and after them, and the step says what the TASK-030 text looks like. Never invent another anchor; if one is missing, stop and report.
- **Names consumed exactly as R1/R2 produce them:** `AppointmentReminder`, `REMINDER_KIND_CHAT`, `REMINDER_STATUS_SENT`, `REMINDER_CHANNEL_CHAT`, `REMINDER_ANSWER_*`, `REMINDER_WARN_UNCONFIRMED`; `reminder_schedule.MAX_CONFIRMATIONS`, `register_confirmation`, `CONFIRMATION_SOURCE_REMINDER_BUTTON`, `CONFIRMATION_SOURCE_CHAT_PROMPT`, `ReminderMismatchError`; `reminder_text.ReminderContent`, `load_reminder_content`, `build_reminder_body`, `reminder_buttons`, `REMINDER_ACTIONS`, `ACTION_CONFIRM/CANCEL/OTHER`; `schemas/webhook.py::decode_action_id`, `_ACTION_BUTTON_PREFIXES`; `reminder_hooks.enabled_for`, `after_appointment_closed`, `reconcile_missing_reminders`; `workers/shared/reminder_actions.py::handle_reminder_button`; test helpers `tests/_reminder_fixtures.py::db`, `tests/_reminders_v2.py::{NOW, WA_ID, FakeWhatsAppClient, fake_waba_token, entitled, seed_world, add_reminder, get_reminder, reload_appointment, outbound_messages, seed_paid_deposit}`.
- **Migration:** one additive revision `a7e2c9d4f1b6` (`conversations.flow_replaces_appointment_id`, nullable UUID, FK `appointments.id` `ON DELETE SET NULL` like `e51cd84e1959`). Its `down_revision` is the single Alembic head at the start of this plan — **expected `b8d3f1a6c2e5` (R1)**, because R1 and TASK-030 P2a (`e7d3c1a9b5f2`) were both written on top of `c3a9e5f1d7b2` and whichever merged second must already have been re-chained onto the other (R1 Task 2 instructs exactly that). If Task 1 Step 1 shows a different single head, use it and change the `DOWN_REVISION` test constant; if it shows two heads, STOP (an integrator must re-chain R1/P2a first). Never drop or rename a column; `downgrade()` is for local use (skill `frozen-contract-migration`).
- **The switch:** with `Tenant.reminders_v2_enabled = false` nothing in this plan changes what a patient sees (spec §5.10): `decide_reminder_opening` returns `switch_off` without a query, no `chat` row is created, the Portal decodes nothing new (no reminder card can exist there), and the cancel-path ids can only arrive from cards R2/R3 sent to a switched-ON clinic. The marker column is only ever written by the "Marcar outra" tap, which only exists on those cards.
- **Opening rule (spec §4.3):** only after the consent gate; only when the gap since the conversation's last activity is `>= reactivation_gap_minutes(tenant)` (default 360 — the same floor as the reactivation offer); only for the NEAREST live future appointment of the patient in this clinic; never when `confirmation_count >= 2`; never twice within the gap (one `chat` row per appointment version — R1's `uq_appointment_reminders_version` — is re-shown, never duplicated); never when an unanswered cron reminder with buttons is already the conversation's latest activity (same message never shown or counted twice); never when the `chat` row of this version was already confirmed. It replaces the returning-patient "quer continuar?" offer/greeting for that turn.
- **The patient's message is still answered** right after the card (spec §4.3): the normal pipeline runs; only the generic IDLE menu (`flow_router.is_generic_menu_result`) and the agent's `show_main_menu` hand-back are suppressed for that turn — "Outro" on the card covers them.
- **Button ids (all `<action>|<reminder_row_id>`, validated by `handle_reminder_button` against tenant AND the conversation's patient):** R2's `remconfirm|`, `remcancel|`, `remother|` plus R3's `remresched|` (Remarcar esta), `remnew|` (Marcar outra), `remgiveup|` (Não vou mais), `remgiveupyes|` (Sim, cancelar), `remkeep|` (Manter consulta). Labels ≤ 20 characters (`core/whatsapp_limits.py::MAX_BUTTON_LABEL_CHARS`); the spec's longer wording ("Remarcar esta consulta", "Marcar outra consulta") goes in the card body. Old ids keep working unchanged. R2's `REMINDER_ACTIONS` is NOT extended (R2's `test_builders_and_decoder_agree` zips it with `reminder_buttons`); the union is `REMINDER_ROW_ACTIONS`.
- **"Marcar outra":** the original is cancelled **only** in the same DB transaction that writes the new `Appointment` (flow confirmation tail and Portal hold promotion), only if it is still live, in the future, of the same clinic AND the same patient, and different from the new one. Pix: `deposit_lifecycle.on_appointment_cancelled` exactly as a normal cancel (spec: "segue as regras de cancelamento de hoje"). The Google event of the original is deleted after the commit, on its owner calendar, best-effort (same philosophy as `_execute_appointment_cancel`). The original's pending reminder rows are closed through R2's `after_appointment_closed(..., reason="replaced")`; the new appointment's rows come from R2's booking hook, unchanged. An agent-tool booking (`ai/tools.py::_persist_appointment`) never cancels anything: it only clears the marker (it shows no warning card).
- **Marker lifetime:** written by `_apply_flow_result` from every result like every `flow_*` field; `flow_router._carry_replacement` keeps it only when BOTH the conversation's current state and the result's state are in `(SERVICE_CATALOG, LLM, AWAITING_EMAIL_CODE)` and the result books nothing; the two silence floors, the "Não" to "quer continuar?", the abandoned code wait and the agent booking clear it. The confirmation card says "Ao confirmar, sua consulta anterior será cancelada." whenever it is set (skill `conversation-flow-state`: every state has a time-bounded exit).
- **Layering:** `api → workers → services/ai → models → core`. `services/*` never imports `workers/*`; `workers/shared/*` never imports `workers.whatsapp`, `workers.portal` or `workers.tasks` (`tests/test_workers_layering.py`).
- **Channels:** every send goes through `_reply_sender` / `_send_buttons_reply` / `_apply_flow_result` (skill `channel-aware-dispatch`); no new send point is WhatsApp-only. WhatsApp: the patient just wrote, so the 24 h window is open and plain buttons are allowed; Portal: `BrainMessageSender` records the card, which is what the Portal tap check reads.
- **Text:** patient-facing text in Portuguese, code/comments/log events in English. Card bodies ≤ 1024 (`MAX_INTERACTIVE_BODY_CHARS`), labels ≤ 20 (skill `third-party-text-limits`). No PII in logs: ids, reasons, counts only — never names, dates, message text.
- **Commands:** tests from Git Bash at the repo root: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest <file> -q`. Never `ruff format .`. Files this plan **creates**: `uvx ruff format <file>` then `uvx ruff check --fix <file>`. Files it **modifies**: before editing note `uvx ruff format --diff <file> 2>&1 | grep -c '^@@'`; after editing the count must not grow, and `uvx ruff check <file>` must be clean on the lines you touched. The tree is CRLF: after each task `git diff --stat` must show only the touched lines (a whole-file diff means the line endings flipped: `git checkout -- <file>` and redo). After writing any file with non-ASCII text run the bidi scan of R2 Task 2 Step 5 on it (expected: no output).
- **Commits:** local only, `git add <explicit paths>` (never `-A`, parallel sessions share worktrees), message ending with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. No push, no deploy, no full suite inside this plan (the Integrator runs the full suite).

## Review Focus

Each line is pinned by the test named in brackets, in the task that owns the code.

1. **Consent not yet given** → only the consent re-prompt, no reminder card, no `chat` row. [Task 10 `test_consent_not_yet_given_gets_only_the_consent_prompt`]
2. **Patient with two upcoming appointments** → the card is about the nearest; the answer adds "Você também tem consulta em …". [Task 9 `test_the_nearest_of_two_appointments_opens_the_chat`; Task 10 `test_two_appointments_open_with_the_nearest_and_the_answer_names_the_other`]
3. **Appointment in the past or cancelled** → no opening; a tap on an old card answers "Essa consulta não está mais ativa." [Task 9 `test_a_past_or_cancelled_appointment_never_opens_the_chat`; Task 10 `test_a_cancelled_appointment_gets_the_normal_menu`]
4. **`confirmation_count` already 2** → no confirmation request, the normal menu. [Task 9 `test_confirmed_twice_never_asks_again`; Task 10 `test_confirmed_twice_gets_the_normal_menu`]
5. **A clear request alongside the opening** ("Agendar") → card first, then the booking question; only the generic menu is suppressed. [Task 10 `test_a_clear_request_is_answered_right_after_the_card`; Task 2 `test_a_clear_request_is_not_the_generic_menu`]
6. **Portal tap carrying another patient's reminder id** → the id is not on this conversation's cards, it is dropped, nothing counted. [Task 11 `test_a_portal_tap_with_another_patients_reminder_id_counts_nothing`]
7. **"Marcar outra" abandoned** → the original stays scheduled; the marker disappears on the menu, the silence floors, the "Não", the abandoned code wait and an agent booking; a stale marker never reaches a new booking started from IDLE. [Task 2 `test_a_marker_left_on_an_idle_conversation_never_reaches_a_new_booking`; Task 3 `test_apply_clears_the_marker_when_the_result_leaves_the_booking` and the floor/"Não"/code tests]
8. **New booking committed** → original `cancelled` in the same transaction, its Google event deleted on its own calendar, its pending reminders `cancelled`, the new one's reminders planned, the confirmation says the old one was cancelled. [Task 5 `test_confirming_the_new_booking_cancels_the_original_in_the_same_turn`, `test_the_portal_promotion_replaces_the_original_too`]
9. **Pix paid original** → "Não vou mais" and "Marcar outra" show the retention line inside the refund window; the replacement runs the normal money hook and appends its notice; a failing money hook never undoes the booking. [Task 4 `test_the_money_hook_runs_and_its_notice_is_returned`, `test_a_failing_money_hook_still_cancels_the_original`; Task 7 `test_give_up_inside_the_refund_window_warns_about_the_deposit_first`; Task 8 `test_book_another_inside_the_refund_window_warns_about_the_deposit`]
10. **Duplicate opening within 6 h** → no second card (the gap) and no second row (same version re-shown). [Task 9 `test_a_chat_row_shown_inside_the_gap_is_not_shown_again`; Task 10 `test_a_second_message_right_after_the_card_gets_no_second_card`]
11. **The chat row shares the counter with the cron rows; the same message never counts twice** → cron confirm + chat confirm = 2; the same chat card confirmed twice (or re-shown and confirmed) = 1; an unanswered cron card that is already the last activity is not repeated. [Task 9 `test_an_unanswered_cron_card_that_is_the_last_activity_is_not_repeated`; Task 10 `test_chat_and_cron_confirmations_share_the_counter`, `test_the_same_chat_card_reshown_later_counts_once`]
12. **Foreign or stale cancel-path taps** (another patient's row, an old version) → "Não encontrei essa consulta." / "horário antigo", nothing cancelled or booked. [Task 7 `test_cancel_path_taps_are_checked_against_the_patient_and_the_version`]

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `migrations/versions/a7e2c9d4f1b6_conversation_flow_replaces_appointment.py` | Create | additive column + FK (Postgres) |
| `src/secretaria/models/conversation.py` | Modify | `flow_replaces_appointment_id` |
| `src/secretaria/services/flow_router.py` | Modify | result field, `_carry_replacement`, card warning, `is_generic_menu_result` |
| `src/secretaria/workers/shared/flow_runner.py` | Modify | write the marker; consume it on booking; suppress the generic menu after an opening |
| `src/secretaria/workers/shared/state_expiry.py`, `src/secretaria/workers/turn_router.py`, `src/secretaria/workers/orchestrator.py`, `src/secretaria/ai/tools.py` | Modify | clear / snapshot the marker; choose and send the opening |
| `src/secretaria/services/appointment_replacement.py` | Create | `cancel_replaced_appointment` (DB + money, caller's transaction) |
| `src/secretaria/workers/shared/replacement.py` | Create | after-commit: Google event + reminder rows of the original; the notice line |
| `src/secretaria/workers/shared/booking_hold.py` | Modify | Portal promotion consumes the marker |
| `src/secretaria/services/reminder_text.py`, `src/secretaria/schemas/webhook.py` | Modify | cancel-path ids, labels, builders, decoder prefixes |
| `src/secretaria/workers/shared/reminder_actions.py` | Rewrite | three-way card, "Não vou mais" confirm, keep, Outro → AI, continuations, "também tem consulta" |
| `src/secretaria/workers/shared/actions.py` | Modify | dispatch with continuations; "cancel and ask why"; "book another" |
| `src/secretaria/services/reminder_opening.py` | Create | `decide_reminder_opening`, `ensure_chat_reminder` |
| `src/secretaria/services/reminder_hooks.py` | Modify | reconcile ignores `chat` rows |
| `src/secretaria/workers/shared/context.py` | Modify | `_ReplyContext.reminder_opening_appointment_id` |
| `src/secretaria/workers/shared/reminder_opening.py` | Create | `_send_reminder_opening` |
| `src/secretaria/workers/portal/inbound.py` | Modify | decode reminder ids on Portal taps |
| `tests/_reminders_r3.py` | Create | shared R3 test wiring (patches, turn driver, ageing) |
| `tests/test_flow_replaces_column.py`, `tests/test_appointment_replacement_router.py`, `tests/test_appointment_replacement_marker.py`, `tests/test_appointment_replacement_service.py`, `tests/test_appointment_replacement_wiring.py`, `tests/test_reminder_r3_ids.py`, `tests/test_reminder_r3_cancel_path.py`, `tests/test_reminder_r3_continuations.py`, `tests/test_reminder_opening_decision.py`, `tests/test_reminder_opening_turn.py`, `tests/test_reminder_r3_portal.py` | Create | one file per unit |
| `tests/test_reminder_v2_buttons.py` | Modify | R2's interim-card test becomes the three-way card |
| `docs/CHECKPOINT_lembretes_r3.md` (+ pointers) | Create/Modify | state after validation |

## Interfaces → Produces (R4/R5 and later tasks rely on these exact names)

```python
# services/flow_router.py
FlowRouterResult.flow_replaces_appointment_id: UUID | None = None
REPLACEMENT_KEEP_STATES: tuple[FlowState, ...]   # SERVICE_CATALOG, LLM, AWAITING_EMAIL_CODE
REPLACEMENT_NOTICE = "Ao confirmar, sua consulta anterior será cancelada."
def _carry_replacement(conversation, result: FlowRouterResult) -> FlowRouterResult
def _replacement_line(conversation) -> str
def is_generic_menu_result(result: FlowRouterResult) -> bool

# services/appointment_replacement.py
REPLACEMENT_REASON = "replaced"
@dataclass(frozen=True) class ReplacedAppointment:
    appointment_id: UUID; google_event_id: str | None; professional_id: UUID | None
    start_at: datetime; money_note: str | None
async def cancel_replaced_appointment(session, *, tenant, patient_id, replaced_id, new_appointment_id,
                                      waba_token=None, now=None) -> ReplacedAppointment | None

# workers/shared/replacement.py
REPLACED_TEXT = "Sua consulta anterior, de {when}, foi cancelada."
def _replaced_notice(tenant, replaced: ReplacedAppointment) -> str
async def _finish_replacement(tenant, replaced: ReplacedAppointment) -> None

# services/reminder_text.py (added)
ACTION_RESCHEDULE_THIS = "remresched"; ACTION_BOOK_ANOTHER = "remnew"; ACTION_GIVE_UP = "remgiveup"
ACTION_GIVE_UP_CONFIRM = "remgiveupyes"; ACTION_KEEP = "remkeep"
CANCEL_PATH_ACTIONS: tuple[str, ...]; REMINDER_ROW_ACTIONS: tuple[str, ...]
LABEL_RESCHEDULE_THIS = "Remarcar esta"; LABEL_BOOK_ANOTHER = "Marcar outra"; LABEL_GIVE_UP = "Não vou mais"
LABEL_GIVE_UP_CONFIRM = "Sim, cancelar"; LABEL_KEEP = "Manter consulta"
def cancel_path_buttons(reminder_id) -> list[tuple[str, str]]
def give_up_confirm_buttons(reminder_id) -> list[tuple[str, str]]

# workers/shared/reminder_actions.py (rewritten)
async def handle_reminder_button(reply, action, reminder_id, redis=None) -> tuple[str, str] | None
CONTINUE_RESCHEDULE = "apptresched"; CONTINUE_CANCEL_AND_ASK_WHY = "reminder_cancel_and_ask_why"
CONTINUE_BOOK_ANOTHER = "reminder_book_another"
async def _retention_warning(session, tenant, appointment, now) -> str | None
texts: NOT_FOUND_TEXT, NOT_ACTIVE_TEXT, CONFIRMED_TEXT, MOVED_TEXT, CANCEL_PATH_TEXT, OTHER_TEXT,
       GIVE_UP_CONFIRM_TEXT, KEPT_TEXT, ALSO_SCHEDULED_TEXT, BOOK_ANOTHER_INTRO

# services/reminder_opening.py
OPEN, SKIP_SWITCH_OFF, SKIP_NO_HISTORY, SKIP_RECENT_ACTIVITY, SKIP_NO_UPCOMING, SKIP_CONFIRMED_TWICE,
SKIP_CHAT_ALREADY_CONFIRMED, SKIP_DUPLICATE_OPENING, SKIP_REMINDER_WAITING  (str constants)
@dataclass(frozen=True) class OpeningDecision: reason: str; appointment_id: UUID | None = None
async def nearest_live_appointment(session, tenant_id, patient_id, *, now) -> Appointment | None
async def decide_reminder_opening(session, *, tenant, patient_id, last_activity_at, now) -> OpeningDecision
async def ensure_chat_reminder(session, appointment, *, now) -> AppointmentReminder

# workers/shared/context.py
_ReplyContext.reminder_opening_appointment_id: UUID | None = None
# workers/shared/reminder_opening.py
async def _send_reminder_opening(reply, *, tenant, waba_token) -> bool
```

---

## Task 1: The `conversations.flow_replaces_appointment_id` column

**Files:**
- Create: `migrations/versions/a7e2c9d4f1b6_conversation_flow_replaces_appointment.py`
- Modify: `src/secretaria/models/conversation.py` (after the `flow_attendee_name` column — and after TASK-030 P2a's `flow_draft` column, which sits right below it — before the `reactivation_origin` comment)
- Test: `tests/test_flow_replaces_column.py`

**Interfaces:**
- Consumes: the Alembic chain as R1 and TASK-030 P2a left it.
- Produces: `Conversation.flow_replaces_appointment_id: Mapped[uuid.UUID | None]`; Alembic revision `a7e2c9d4f1b6` (single head).

- [ ] **Step 1: Check the dependencies and the current head**

Run (Git Bash, repo root):

```bash
ls src/secretaria/services/reminder_schedule.py src/secretaria/services/reminder_text.py src/secretaria/services/reminder_hooks.py src/secretaria/workers/shared/reminder_actions.py tests/_reminders_v2.py
grep -n "flow_draft" src/secretaria/models/conversation.py
grep -n "def _confirmation_card\|def _carry_draft" src/secretaria/services/flow_router.py
uv run python -c "from alembic.config import Config; from alembic.script import ScriptDirectory; c=Config('alembic.ini'); c.set_main_option('script_location','migrations'); print(ScriptDirectory.from_config(c).get_heads())"
grep -rn "a7e2c9d4f1b6" migrations/ docs/
```

Expected: the five files exist (R1/R2 merged); `flow_draft` is on the model and `_confirmation_card` / `_carry_draft` exist (TASK-030 P2a/P3 merged); the heads list is exactly `['b8d3f1a6c2e5']`; the last grep prints nothing. Any other outcome: STOP and report (see Global Constraints → Migration). If the single head is a different id, use that id as `down_revision` below and as `DOWN_REVISION` in the test.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_flow_replaces_column.py`:

```python
"""conversations.flow_replaces_appointment_id (TASK-032 R3, spec §4.3 "Marcar outra consulta")."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402

import sqlalchemy as sa  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from alembic.operations import Operations  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402

from secretaria.models import Conversation  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a7e2c9d4f1b6"
# The single head before this plan (Task 1 Step 1): R1's revision, re-chained after
# TASK-030 P2a's e7d3c1a9b5f2.
DOWN_REVISION = "b8d3f1a6c2e5"


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_conversation_flow_replaces_appointment.py"
    spec = importlib.util.spec_from_file_location("flow_replaces_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_is_the_single_head_on_top_of_the_previous_one():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == [REVISION]
    assert _migration().down_revision == DOWN_REVISION


def test_the_migration_adds_and_drops_a_nullable_column_on_sqlite(tmp_path):
    migration = _migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'replaces.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE appointments (id CHAR(32) PRIMARY KEY)"))
        conn.execute(sa.text("CREATE TABLE conversations (id CHAR(32) PRIMARY KEY)"))
        conn.execute(sa.text("INSERT INTO conversations (id) VALUES ('a')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            columns = {c["name"]: c for c in sa.inspect(conn).get_columns("conversations")}
            assert columns["flow_replaces_appointment_id"]["nullable"] is True
            value = conn.execute(
                sa.text("SELECT flow_replaces_appointment_id FROM conversations")
            ).scalar()
            assert value is None
            migration.downgrade()
            names = {c["name"] for c in sa.inspect(conn).get_columns("conversations")}
            assert "flow_replaces_appointment_id" not in names
    engine.dispose()


def test_the_model_maps_a_nullable_fk_that_survives_the_appointment():
    column = Conversation.__table__.c.flow_replaces_appointment_id
    assert column.nullable is True
    [fk] = column.foreign_keys
    assert fk.target_fullname == "appointments.id"
    assert fk.ondelete == "SET NULL"
```

- [ ] **Step 3: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_flow_replaces_column.py -q`
Expected: 3 FAIL — `FileNotFoundError` for the migration (two tests) and `AttributeError: flow_replaces_appointment_id` on the table columns.

- [ ] **Step 4: Write the migration**

Create `migrations/versions/a7e2c9d4f1b6_conversation_flow_replaces_appointment.py`:

```python
"""conversations.flow_replaces_appointment_id: the booking "Marcar outra consulta" replaces.

TASK-032 R3 (docs/superpowers/specs/2026-10-03-lembretes-e-confirmacao-design.md §4.3).
A patient who taps "Cancelar" on a reminder and then "Marcar outra" books a NEW
appointment while the original stays live. The original may be cancelled only
when the new booking is confirmed (owner's decision 7), so the conversation has
to remember which appointment the booking in progress replaces:

    ADD  conversations.flow_replaces_appointment_id  UUID NULL
         FK -> appointments.id  ON DELETE SET NULL

Same shape as flow_managing_appointment_id (e51cd84e1959). NULL whenever no
replacement is in progress; every flow result that does not carry it clears it
(services/flow_router.py::_carry_replacement), and the silence floors clear it.

SQLite (local tests) cannot ALTER a constraint in, so there the column is added
bare; Postgres gets the FK.

DEPLOY ORDER - the database moves FIRST: the ORM names every mapped column on
every read and write of `conversations`, so a process running the new model
against a schema WITHOUT this column fails every turn. The column is inert to
the old code, so:

    1. `alembic upgrade head` from the NEW image (one-off), both services still
       on the old code;
    2. deploy `secretaria_api` AND `secretaria-worker` together (README: "Deploy
       both services, or neither"; `GET /build` must report parity `match`).

Rollback narrows, so it goes the other way round: the OLD code on both services
first, then `alembic downgrade` to the previous revision. The only data lost is
a replacement in progress; that patient's new booking then simply does not
cancel the original (the safe direction).

Revision ID: a7e2c9d4f1b6
Revises: b8d3f1a6c2e5
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7e2c9d4f1b6"
down_revision: str | None = "b8d3f1a6c2e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no default: metadata-only on Postgres (no table rewrite), and NULL
    # is the true value for every row that exists today.
    fk_args: list = []
    if op.get_bind().dialect.name != "sqlite":
        fk_args.append(sa.ForeignKey("appointments.id", ondelete="SET NULL"))
    op.add_column(
        "conversations",
        sa.Column("flow_replaces_appointment_id", sa.Uuid(), *fk_args, nullable=True),
    )


def downgrade() -> None:
    # Dropping the column also drops its unnamed FK constraint on Postgres.
    op.drop_column("conversations", "flow_replaces_appointment_id")
```

- [ ] **Step 5: Add the model column**

In `src/secretaria/models/conversation.py`, directly above the comment `    # Set while a returning patient is mid-"quer continuar?" prompt: holds the` insert:

```python
    # TASK-032 R3 ("Marcar outra consulta", spec §4.3): the live appointment the
    # booking in progress REPLACES. The original is cancelled only in the same
    # transaction that writes the new appointment
    # (services/appointment_replacement.py). Written unconditionally by
    # `_apply_flow_result` like every flow field and carried only while the
    # conversation stays in the booking (`flow_router._carry_replacement`).
    # SET NULL so deleting the appointment never breaks the conversation row.
    flow_replaces_appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True
    )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_flow_replaces_column.py -q`
Expected: `3 passed`.

- [ ] **Step 7: Prove the migration on a disposable Postgres (port 5433)**

```bash
docker run -d --rm --name task032r3-pg -e POSTGRES_USER=secretaria -e POSTGRES_PASSWORD=secretaria -e POSTGRES_DB=secretaria -p 5433:5432 postgres:16-alpine
docker exec task032r3-pg pg_isready -U secretaria      # repeat until "accepting connections"
export MIGRATION_ENV='APP_ENV=test META_APP_SECRET=x META_VERIFY_TOKEN=x META_ACCESS_TOKEN=x META_PHONE_NUMBER_ID=1 OPENAI_API_KEY=x ENCRYPTION_KEY=gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w= DATABASE_URL=postgresql+asyncpg://secretaria:secretaria@127.0.0.1:5433/secretaria'
env $MIGRATION_ENV uv run alembic upgrade head
docker exec task032r3-pg psql -U secretaria -c '\d conversations' | grep flow_replaces
env $MIGRATION_ENV uv run alembic downgrade -1
docker exec task032r3-pg psql -U secretaria -c '\d conversations' | grep -c flow_replaces
env $MIGRATION_ENV uv run alembic upgrade head
docker stop task032r3-pg
```

Expected: the first `grep` shows ` flow_replaces_appointment_id | uuid |` and a foreign-key line `REFERENCES appointments(id) ON DELETE SET NULL`; the `grep -c` after the downgrade prints `0`; the second upgrade succeeds. If port 5433 is taken by `brain-postgres`, use a throwaway database there (`docker exec brain-postgres createdb -U brain task032_r3`, `DATABASE_URL=postgresql+asyncpg://brain:brain@127.0.0.1:5433/task032_r3`, `psql -U brain -d task032_r3`, finish with `dropdb`). If Docker is unavailable, write "Postgres run NOT done" in the checkpoint (Task 12) — never skip silently.

- [ ] **Step 8: Lint and commit**

```bash
uvx ruff format migrations/versions/a7e2c9d4f1b6_conversation_flow_replaces_appointment.py tests/test_flow_replaces_column.py
uvx ruff check --fix migrations/versions/a7e2c9d4f1b6_conversation_flow_replaces_appointment.py tests/test_flow_replaces_column.py
uvx ruff check src/secretaria/models/conversation.py
git diff --stat
git add migrations/versions/a7e2c9d4f1b6_conversation_flow_replaces_appointment.py src/secretaria/models/conversation.py tests/test_flow_replaces_column.py
git commit -F - <<'EOF'
feat(flow): conversations.flow_replaces_appointment_id for "Marcar outra consulta" (TASK-032 R3)

Additive nullable FK column (migration a7e2c9d4f1b6, before both services).

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 2: The router carries the marker only inside the booking, warns on the card, and names the generic menu

**Files:**
- Modify: `src/secretaria/services/flow_router.py` (`FlowRouterResult`; new helpers after `_carry_insurance`; `_carry_booking`; `_recap_text`; `is_generic_menu_result` after `_menu_bubbles`)
- Test: `tests/test_appointment_replacement_router.py`

**Interfaces:**
- Consumes: `FlowState`; the existing `_carry_booking` (after TASK-030 P2a it reads `return _carry_draft(conversation, _carry_insurance(conversation, _carry_attendee(conversation, result)))`); `_recap_text` (after TASK-030 P3 its signature is `_recap_text(conversation, start, *, professional=None)`).
- Produces: `FlowRouterResult.flow_replaces_appointment_id`, `REPLACEMENT_KEEP_STATES`, `REPLACEMENT_NOTICE`, `_carry_replacement`, `_replacement_line`, `is_generic_menu_result`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_appointment_replacement_router.py`:

```python
"""The router keeps "Marcar outra" only inside the booking (TASK-032 R3, spec §4.3)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import datetime  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from tests.test_flow_router import _FakeCalendar, _conversation, _tenant  # noqa: E402

ORIGINAL = uuid4()


def _in_booking(**kw):
    return _conversation(
        flow_state=FlowState.SERVICE_CATALOG, flow_replaces_appointment_id=ORIGINAL, **kw
    )


def test_a_booking_step_keeps_the_marker():
    result = fr.FlowRouterResult(
        action="reply", flow_state=FlowState.SERVICE_CATALOG, flow_step=fr.STEP_AWAITING_DAY
    )
    assert fr._carry_replacement(_in_booking(), result).flow_replaces_appointment_id == ORIGINAL


@pytest.mark.parametrize("state", [FlowState.LLM, FlowState.AWAITING_EMAIL_CODE])
def test_the_ai_and_the_portal_code_wait_keep_the_marker(state):
    result = fr.FlowRouterResult(action="delegate_llm", flow_state=state)
    assert fr._carry_replacement(_in_booking(), result).flow_replaces_appointment_id == ORIGINAL


@pytest.mark.parametrize("state", [FlowState.MENU, FlowState.IDLE, FlowState.MANAGE_BOOKING])
def test_leaving_the_booking_drops_the_marker(state):
    result = fr.FlowRouterResult(action="reply", flow_state=state)
    assert fr._carry_replacement(_in_booking(), result).flow_replaces_appointment_id is None


def test_a_marker_left_on_an_idle_conversation_never_reaches_a_new_booking():
    stale = _conversation(flow_state=FlowState.IDLE, flow_replaces_appointment_id=ORIGINAL)
    result = fr.FlowRouterResult(action="reply", flow_state=FlowState.SERVICE_CATALOG)
    assert fr._carry_replacement(stale, result).flow_replaces_appointment_id is None


def test_a_booked_result_never_carries_the_marker():
    result = fr.FlowRouterResult(
        action="reply", flow_state=FlowState.SERVICE_CATALOG, appointment={"google_event_id": "e"}
    )
    assert fr._carry_replacement(_in_booking(), result).flow_replaces_appointment_id is None


def test_a_marker_the_result_names_is_kept_as_named():
    other = uuid4()
    result = fr.FlowRouterResult(
        action="reply", flow_state=FlowState.SERVICE_CATALOG, flow_replaces_appointment_id=other
    )
    assert fr._carry_replacement(_conversation(), result).flow_replaces_appointment_id == other


async def test_route_from_idle_drops_a_stale_marker():
    result = await fr.route(
        _conversation(flow_replaces_appointment_id=ORIGINAL), _tenant(), None, fr.LABEL_BOOK
    )
    assert result.flow_state == FlowState.SERVICE_CATALOG
    assert result.flow_replaces_appointment_id is None


async def test_cancelar_on_the_card_keeps_the_marker_for_the_retry():
    conversation = _in_booking(
        flow_step=fr.STEP_AWAITING_CONFIRMATION,
        flow_selected_type="Primeira Consulta",
        flow_selected_slot="2026-10-06T09:00:00-03:00",
    )
    result = await fr.route(conversation, _tenant(), _FakeCalendar(), fr.LABEL_CANCEL)
    assert result.flow_step == fr.STEP_AWAITING_RETRY
    assert result.flow_replaces_appointment_id == ORIGINAL


def test_the_confirmation_card_warns_that_the_old_appointment_goes():
    start = datetime(2026, 10, 6, 9, 0)
    marked = fr._recap_text(_in_booking(flow_selected_type="Consulta"), start)
    plain = fr._recap_text(_conversation(flow_selected_type="Consulta"), start)
    assert marked == "Consulta\n06/10/2026 às 09:00\n\n" + fr.REPLACEMENT_NOTICE
    assert plain == "Consulta\n06/10/2026 às 09:00"


async def test_the_idle_menu_is_the_generic_menu():
    result = await fr.route(_conversation(), _tenant(), None, "oi")
    assert result.flow_state == FlowState.MENU
    assert fr.is_generic_menu_result(result) is True


async def test_a_clear_request_is_not_the_generic_menu():
    result = await fr.route(_conversation(), _tenant(), None, fr.LABEL_BOOK)
    assert fr.is_generic_menu_result(result) is False


def test_the_decline_question_and_answer_are_not_the_generic_menu():
    assert fr.is_generic_menu_result(fr.enter_decline_reasons(uuid4())) is False
    answered = fr._handle_decline_reason(
        _conversation(flow_managing_appointment_id=uuid4()), "Não preciso mais"
    )
    assert fr.is_generic_menu_result(answered) is False
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_replacement_router.py -q`
Expected: FAIL — `AttributeError: module 'secretaria.services.flow_router' has no attribute '_carry_replacement'` / `REPLACEMENT_NOTICE` / `is_generic_menu_result`, and `TypeError: ... unexpected keyword argument 'flow_replaces_appointment_id'`.

- [ ] **Step 3: The result field**

In `src/secretaria/services/flow_router.py`, inside `class FlowRouterResult`, after the last field `appointment_reschedule: dict | None = None` (TASK-030 P2a added `flow_draft`/`resume_draft` higher up — leave them), append:

```python
    # TASK-032 R3 ("Marcar outra consulta"): the live appointment the booking in
    # progress REPLACES. Written unconditionally by `_apply_flow_result` like
    # every flow field; `_carry_replacement` keeps it only while the
    # conversation stays inside the booking, and the booking tail cancels the
    # original in the same transaction as the new row
    # (services/appointment_replacement.py).
    flow_replaces_appointment_id: UUID | None = None
```

- [ ] **Step 4: The carry and the card line**

(a) Directly after the function `_carry_insurance` (before `def _carry_booking`, or before `def _carry_draft` where TASK-030 P2a put it there), insert:

```python
# The states a "Marcar outra" booking may pass through (TASK-032 R3): the
# booking itself, the AI answering a question in the middle of it (TASK-030
# hands back to the same card), and the Portal's code wait after "Confirmar"
# (the appointment is born at `_promote_booking_hold`).
REPLACEMENT_KEEP_STATES: tuple[FlowState, ...] = (
    FlowState.SERVICE_CATALOG,
    FlowState.LLM,
    FlowState.AWAITING_EMAIL_CODE,
)
REPLACEMENT_NOTICE = "Ao confirmar, sua consulta anterior será cancelada."


def _replaces_appointment_id(conversation: Conversation) -> UUID | None:
    """The appointment a booking in progress replaces (getattr: snapshots may predate it)."""
    return getattr(conversation, "flow_replaces_appointment_id", None)


def _carry_replacement(conversation: Conversation, result: FlowRouterResult) -> FlowRouterResult:
    """Keep the "Marcar outra" marker while the patient is still booking.

    `_apply_flow_result` writes `flow_replaces_appointment_id` unconditionally,
    so a result that does not name it clears it - which is the point: the
    marker must never outlive the booking it belongs to (an abandoned "Marcar
    outra" leaves the original untouched). Kept only when the conversation is
    ALREADY in one of `REPLACEMENT_KEEP_STATES` and the result stays in one, so
    a marker left behind on an IDLE conversation can never ride into a booking
    started later from the menu. A result that books (`appointment`) never
    carries it: the caller consumes the stored value in that same transaction.
    """
    if result.flow_replaces_appointment_id is not None or result.appointment is not None:
        return result
    if (
        getattr(conversation, "flow_state", None) in REPLACEMENT_KEEP_STATES
        and result.flow_state in REPLACEMENT_KEEP_STATES
    ):
        result.flow_replaces_appointment_id = _replaces_appointment_id(conversation)
    return result


def _replacement_line(conversation: Conversation) -> str:
    """The confirmation card's warning while a "Marcar outra" booking is in progress."""
    return f"\n\n{REPLACEMENT_NOTICE}" if _replaces_appointment_id(conversation) else ""
```

(b) In `def _carry_booking(`, wrap the existing return expression in `_carry_replacement(conversation, …)`. After TASK-030 P2a the body is

```python
    return _carry_draft(
        conversation, _carry_insurance(conversation, _carry_attendee(conversation, result))
    )
```

and becomes

```python
    return _carry_replacement(
        conversation,
        _carry_draft(
            conversation, _carry_insurance(conversation, _carry_attendee(conversation, result))
        ),
    )
```

(if the merged text differs only in wrapping, keep the inner expression byte for byte and wrap it the same way). Update the docstring's first line to say "The carries, applied once per public entry (route, resume, hand-back)." only if it currently counts them ("Both"/"three").

(c) In `def _recap_text(`, the returned concatenation ends with the line `        f"{start.strftime('%d/%m/%Y às %H:%M')}"` followed by `    )`. Replace those two lines with:

```python
        f"{start.strftime('%d/%m/%Y às %H:%M')}"
        f"{_replacement_line(conversation)}"
    )
```

Every card that recaps a booking (the slot tap, the resume, TASK-030's express card) goes through `_recap_text`, so all of them carry the warning; with no marker the text is byte for byte what it was.

- [ ] **Step 5: The generic-menu predicate**

Directly after the function `_menu_bubbles` insert:

```python
def is_generic_menu_result(result: FlowRouterResult) -> bool:
    """True for the plain menu card `route()` shows when a message from IDLE means nothing specific.

    TASK-032 R3: on the turn the chat opens with the appointment reminder, that
    card (whose "Outro" already offers everything the menu would) replaces this
    one, so the worker skips it (`workers/shared/flow_runner.py::_run_flow`).
    Anything that records, books, moves, cancels or asks a specific question is
    NOT generic and is always answered.
    """
    return (
        result.action == "reply"
        and result.flow_state == FlowState.MENU
        and result.flow_step is None
        and result.appointment is None
        and result.appointment_cancel_id is None
        and result.appointment_reschedule is None
        and result.decline_reason is None
        and result.booking_hold is None
        and bool(result.bubbles)
        and all(isinstance(bubble, MenuBubble) for bubble in result.bubbles)
    )
```

(`MenuBubble` is defined further down the module; the name is resolved at call time, so the order is fine.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_replacement_router.py tests/test_flow_router.py tests/test_attendee_booking.py tests/test_flow_draft_column.py -q`
Expected: all pass (the existing router suites prove nothing else moved).

- [ ] **Step 7: Lint and commit**

```bash
uvx ruff format tests/test_appointment_replacement_router.py
uvx ruff check --fix tests/test_appointment_replacement_router.py
uvx ruff check src/secretaria/services/flow_router.py
uvx ruff format --diff src/secretaria/services/flow_router.py 2>&1 | grep -c '^@@'   # not above the count noted before editing
git diff --stat
git add src/secretaria/services/flow_router.py tests/test_appointment_replacement_router.py
git commit -F - <<'EOF'
feat(flow): carry the "Marcar outra" marker only inside the booking (TASK-032 R3)

The confirmation card warns that the previous appointment will be cancelled;
is_generic_menu_result names the IDLE menu the reminder opening replaces.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 3: The worker writes the marker and every silence/reset path drops it

**Files:**
- Create: `tests/_reminders_r3.py` (shared R3 test wiring, used by Tasks 3, 5, 7, 8, 10, 11)
- Modify: `src/secretaria/workers/shared/flow_runner.py` (`_apply_flow_result`: one write)
- Modify: `src/secretaria/workers/orchestrator.py` (`_send_bot_reply_inner`: the `flow_snapshot` `SimpleNamespace`)
- Modify: `src/secretaria/workers/shared/state_expiry.py` (`_expire_stale_llm_state`, `_expire_stale_attendee_step`)
- Modify: `src/secretaria/workers/turn_router.py` (the "Não" branch of the pending "quer continuar?" answer; the abandoned code wait)
- Modify: `src/secretaria/ai/tools.py` (`_persist_appointment`)
- Test: `tests/test_appointment_replacement_marker.py`

**Interfaces:**
- Consumes: Task 1 column; Task 2 `FlowRouterResult.flow_replaces_appointment_id`, `_carry_replacement`.
- Produces: test helpers `AGENT_REPLY`, `FakeCalendar`, `wire(monkeypatch, db, calendar=None) -> FakeCalendar`, `consent(db, world)`, `set_conversation(db, world, **fields)`, `get_conversation(db, world)`, `add_appointment(db, world, *, start_at, status=SCHEDULED) -> Appointment`, `turn(db, world, body, *, interactive_reply_id=None, send=True) -> _ReplyContext | None`, `age_conversation(db, world, hours)`, `chat_rows(db, appointment_id)`, `reminder_rows(db, appointment_id)`, `sent()`; behavior: the marker survives exactly what Task 2's carry allows and nothing else.

- [ ] **Step 1: Write the shared test wiring**

Create `tests/_reminders_r3.py`:

```python
"""Wiring shared by the TASK-032 R3 tests: patches, a whole-turn driver, ageing.

The database fixture is R1's (`tests/_reminder_fixtures.py::db`) and the seeds
and the WhatsApp double are R2's (`tests/_reminders_v2.py`). This module adds
what a WHOLE inbound turn needs: the entitlement, a model-free agent, an owner
calendar that records deletes, and a driver that runs `_route_inbound_turn`
inside one transaction and then `_send_bot_reply`, exactly as the WhatsApp and
Portal wrappers do.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select

from secretaria.core import database as core_database
from secretaria.models import (
    Appointment,
    AppointmentReminder,
    AppointmentStatus,
    Conversation,
    Message,
    Patient,
    Tenant,
)
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, CHANNEL_WHATSAPP
from secretaria.workers.orchestrator import _send_bot_reply
from secretaria.workers.turn_router import _route_inbound_turn
from tests._patching import workers_ns
from tests._reminders_v2 import WA_ID, FakeWhatsAppClient, entitled, fake_waba_token

AGENT_REPLY = "Resposta da IA."


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class FakeCalendar:
    """Owner-calendar double: records deletes and creations, every day is open."""

    tzinfo = ZoneInfo("America/Sao_Paulo")

    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.created: list[tuple] = []

    async def cancel_event(self, event_id):
        self.cancelled.append(event_id)

    async def create_event(self, start, end, summary, description=""):
        self.created.append((start, end, summary))
        return {"id": "evt-new", "htmlLink": "https://calendar.google.com/evt-new"}

    async def list_available_days(self, start_day, days, slot_minutes=None):
        base = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return [base + timedelta(days=offset) for offset in range(1, days)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        return []


def wire(monkeypatch, db, calendar: FakeCalendar | None = None) -> FakeCalendar:
    """Point every worker module at the in-memory DB and the doubles."""
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    FakeWhatsAppClient.reset()
    monkeypatch.setattr(workers_ns, "WhatsAppClient", FakeWhatsAppClient)
    monkeypatch.setattr(workers_ns, "get_waba_token", fake_waba_token)
    monkeypatch.setattr(workers_ns, "get_entitlements", entitled)

    async def _agent(message, *args, **kwargs):
        return AGENT_REPLY

    monkeypatch.setattr(workers_ns, "run_agent", _agent)
    owner = calendar or FakeCalendar()

    async def _owner_calendar(*args, **kwargs):
        return owner

    monkeypatch.setattr(workers_ns, "_appointment_calendar", _owner_calendar)
    monkeypatch.setattr(workers_ns, "_calendar_for_appointment", _owner_calendar)
    return owner


def sent() -> list[tuple]:
    """Everything the WhatsApp double sent, in order."""
    return FakeWhatsAppClient.all_sent()


async def consent(db, world) -> None:
    async with db() as session:
        patient = await session.get(Patient, world.patient.id)
        patient.lgpd_accepted_at = datetime.now(UTC) - timedelta(days=30)
        await session.commit()


async def set_conversation(db, world, **fields) -> None:
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
        for name, value in fields.items():
            setattr(conversation, name, value)
        await session.commit()


async def get_conversation(db, world) -> Conversation:
    async with db() as session:
        return await session.get(Conversation, world.conversation.id)


async def add_appointment(
    db, world, *, start_at: datetime, status: AppointmentStatus = AppointmentStatus.SCHEDULED
) -> Appointment:
    async with db() as session:
        appointment = Appointment(
            id=uuid4(),
            tenant_id=world.tenant.id,
            patient_id=world.patient.id,
            conversation_id=world.conversation.id,
            google_event_id=f"evt-{uuid4()}",
            appointment_type="Consulta",
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            status=status,
        )
        session.add(appointment)
        await session.commit()
        await session.refresh(appointment)
        return appointment


def _patient_ref(world) -> str:
    return world.patient.external_id if world.patient.wa_id is None else WA_ID


async def turn(db, world, body, *, interactive_reply_id=None, send: bool = True):
    """One inbound turn: route inside a transaction, then (by default) answer it."""
    channel = CHANNEL_BRAIN_MESSAGE if world.patient.wa_id is None else CHANNEL_WHATSAPP
    async with db() as session:
        async with session.begin():
            tenant = await session.get(Tenant, world.tenant.id)
            patient = await session.get(Patient, world.patient.id)
            reply = await _route_inbound_turn(
                session,
                tenant=tenant,
                patient=patient,
                patient_ref=_patient_ref(world),
                channel=channel,
                is_returning_patient=True,
                body=body,
                inbound_wam_id=None,
                interactive_reply_id=interactive_reply_id,
            )
    if send and reply is not None:
        await _send_bot_reply(reply)
    return reply


async def age_conversation(db, world, hours: float) -> None:
    """Pretend `hours` passed: every message and every reminder `sent_at` moves back."""
    delta = timedelta(hours=hours)
    async with db() as session:
        messages = await session.scalars(
            select(Message).where(Message.conversation_id == world.conversation.id)
        )
        for message in messages:
            message.created_at = _utc(message.created_at) - delta
        rows = await session.scalars(
            select(AppointmentReminder).where(AppointmentReminder.patient_id == world.patient.id)
        )
        for row in rows:
            if row.sent_at is not None:
                row.sent_at = _utc(row.sent_at) - delta
        await session.commit()


async def reminder_rows(db, appointment_id) -> list[AppointmentReminder]:
    async with db() as session:
        return list(
            await session.scalars(
                select(AppointmentReminder).where(
                    AppointmentReminder.appointment_id == appointment_id
                )
            )
        )


async def chat_rows(db, appointment_id) -> list[AppointmentReminder]:
    return [row for row in await reminder_rows(db, appointment_id) if row.kind == "chat"]
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_appointment_replacement_marker.py`:

```python
"""Where the "Marcar outra" marker is written and where it is dropped (TASK-032 R3)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretaria.ai import tools
from secretaria.ai.formatter import TextBubble
from secretaria.models import AppointmentStatus, FlowState
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.flow_router import (
    LABEL_CANCEL,
    STEP_AWAITING_ATTENDEE_NAME,
    STEP_AWAITING_CONFIRMATION,
    STEP_AWAITING_RETRY,
    FlowRouterResult,
    MenuBubble,
)
from secretaria.workers import tasks
from secretaria.workers.shared import state_expiry
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import consent, get_conversation, set_conversation, turn, wire
from tests._reminders_v2 import WA_ID, reload_appointment, seed_world


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


def _reply(world) -> tasks._ReplyContext:
    return tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
    )


def _future() -> datetime:
    return datetime.now(UTC) + timedelta(days=3)


async def test_apply_writes_the_marker_a_result_names(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    result = FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body="ok")],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_replaces_appointment_id=world.appointment.id,
    )

    await tasks._apply_flow_result(_reply(world), result, WA_ID, tenant=world.tenant, waba_token="t")

    assert (await get_conversation(db, world)).flow_replaces_appointment_id == world.appointment.id


async def test_apply_clears_the_marker_when_the_result_leaves_the_booking(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    await set_conversation(
        db,
        world,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_replaces_appointment_id=world.appointment.id,
    )
    menu = FlowRouterResult(
        action="reply",
        bubbles=[MenuBubble(body="Como posso ajudar?", labels=["Agendar"])],
        flow_state=FlowState.MENU,
    )

    await tasks._apply_flow_result(_reply(world), menu, WA_ID, tenant=world.tenant, waba_token="t")

    assert (await get_conversation(db, world)).flow_replaces_appointment_id is None
    # Abandoned: the original is untouched.
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED


def _parked(flow_state, flow_step=None) -> SimpleNamespace:
    return SimpleNamespace(
        flow_state=flow_state,
        flow_step=flow_step,
        flow_selected_type="Consulta",
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_managing_appointment_id=None,
        flow_attendee_name="Ana",
        flow_draft=None,
        flow_replaces_appointment_id=uuid4(),
    )


def test_the_llm_floor_drops_the_marker():
    conversation = _parked(FlowState.LLM)
    quiet_since = datetime.now(UTC) - timedelta(days=2)

    assert state_expiry._expire_stale_llm_state(
        conversation, SimpleNamespace(initial_flows={}), quiet_since
    )
    assert conversation.flow_replaces_appointment_id is None


def test_the_attendee_floor_drops_the_marker():
    conversation = _parked(FlowState.SERVICE_CATALOG, STEP_AWAITING_ATTENDEE_NAME)
    quiet_since = datetime.now(UTC) - timedelta(days=2)

    assert state_expiry._expire_stale_attendee_step(
        conversation, SimpleNamespace(initial_flows={}), quiet_since
    )
    assert conversation.flow_replaces_appointment_id is None


async def test_no_to_quer_continuar_drops_the_marker(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    await consent(db, world)
    await set_conversation(
        db,
        world,
        flow_state=FlowState.SERVICE_CATALOG,
        reactivation_origin=FlowState.SERVICE_CATALOG.value,
        flow_replaces_appointment_id=world.appointment.id,
    )

    reply = await turn(db, world, "Não", send=False)

    assert reply.reactivation.kind == "reset"
    assert (await get_conversation(db, world)).flow_replaces_appointment_id is None


async def test_an_abandoned_portal_code_wait_drops_the_marker(db):  # noqa: F811
    world = await seed_world(db, start_at=_future(), channel=CHANNEL_BRAIN_MESSAGE)
    await consent(db, world)
    await set_conversation(
        db,
        world,
        flow_state=FlowState.AWAITING_EMAIL_CODE,
        flow_replaces_appointment_id=world.appointment.id,
    )

    await turn(db, world, "oi", send=False)

    conversation = await get_conversation(db, world)
    assert conversation.flow_state == FlowState.IDLE
    assert conversation.flow_replaces_appointment_id is None


async def test_the_router_snapshot_carries_the_marker_through_a_turn(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    await consent(db, world)
    await set_conversation(
        db,
        world,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_CONFIRMATION,
        flow_selected_type="Consulta",
        flow_selected_slot=(_future() + timedelta(days=1)).isoformat(),
        flow_replaces_appointment_id=world.appointment.id,
    )

    await turn(db, world, LABEL_CANCEL)

    conversation = await get_conversation(db, world)
    assert conversation.flow_step == STEP_AWAITING_RETRY
    assert conversation.flow_replaces_appointment_id == world.appointment.id


async def test_an_agent_booking_clears_the_marker_and_cancels_nothing(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    await set_conversation(
        db,
        world,
        flow_state=FlowState.LLM,
        flow_replaces_appointment_id=world.appointment.id,
    )
    tenant_token = tools._tenant_id_ctx.set(world.tenant.id)
    conversation_token = tools._conversation_id_ctx.set(world.conversation.id)
    try:
        start = _future() + timedelta(days=2)
        await tools._persist_appointment(
            {"id": "evt-agent"}, start, start + timedelta(minutes=30), "Consulta"
        )
    finally:
        tools._conversation_id_ctx.reset(conversation_token)
        tools._tenant_id_ctx.reset(tenant_token)

    assert (await get_conversation(db, world)).flow_replaces_appointment_id is None
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED
```

- [ ] **Step 3: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_replacement_marker.py -q`
Expected: FAIL — the write test (`None != <uuid>`), both floors (`flow_replaces_appointment_id` still set), the "Não" and code-wait tests (still set), the snapshot test (`None != <uuid>`: the snapshot does not carry it, so the router drops it) and the agent test (still set). `test_apply_clears_the_marker_when_the_result_leaves_the_booking` may already pass only by accident of the default (`None`) — it must keep passing.

- [ ] **Step 4: Write the marker in `_apply_flow_result`**

In `src/secretaria/workers/shared/flow_runner.py`, inside `_apply_flow_result`, after the line `                    conv.flow_attendee_name = result.flow_attendee_name` (and after TASK-030 P2a's `                    conv.flow_draft = result.flow_draft` right below it) insert:

```python
                    conv.flow_replaces_appointment_id = result.flow_replaces_appointment_id
```

- [ ] **Step 5: Give the router the marker**

In `src/secretaria/workers/orchestrator.py`, inside `_send_bot_reply_inner`, in the `SimpleNamespace(` of `flow_snapshot`, after the line `                            flow_attendee_name=conversation.flow_attendee_name,` (and after TASK-030 P2a's `flow_draft=conversation.flow_draft,`) insert:

```python
                            flow_replaces_appointment_id=conversation.flow_replaces_appointment_id,
```

- [ ] **Step 6: Drop it on the silence floors, the "Não" and the abandoned code wait**

(a) `src/secretaria/workers/shared/state_expiry.py`, in `_expire_stale_llm_state`: after `    conversation.flow_attendee_name = None` (and after TASK-030 P2a's `    conversation.flow_draft = None` that follows it) insert:

```python
    # A "Marcar outra" booking abandoned in LLM mode: the original stays
    # (TASK-032 R3) - the marker belongs to the booking that just expired.
    conversation.flow_replaces_appointment_id = None
```

(b) Same file, in `_expire_stale_attendee_step`: after its `    conversation.flow_attendee_name = None` (and P2a's `    conversation.flow_draft = None`), before `    return True`, insert:

```python
    conversation.flow_replaces_appointment_id = None
```

(c) `src/secretaria/workers/turn_router.py`, in the `if answer == "no":` branch of the pending "quer continuar?" answer: after `            conversation.flow_attendee_name = None` (and P2a's `            conversation.flow_draft = None`) insert:

```python
            conversation.flow_replaces_appointment_id = None
```

(d) Same file, the abandoned code wait. Replace

```python
            conversation.flow_state = FlowState.IDLE
            logger.info(
                "conversation_pending_code_abandoned",
```

with

```python
            conversation.flow_state = FlowState.IDLE
            # TASK-032 R3: leaving the code wait ends a "Marcar outra" booking too.
            conversation.flow_replaces_appointment_id = None
            logger.info(
                "conversation_pending_code_abandoned",
```

- [ ] **Step 7: The agent's own booking never replaces anything**

In `src/secretaria/ai/tools.py`, inside `_persist_appointment`, replace

```python
                        booked_conversation.flow_attendee_name = None
```

with

```python
                        booked_conversation.flow_attendee_name = None
                        # TASK-032 R3: "Marcar outra" replaces the original only
                        # through the flow's confirmation card, which warns about
                        # it. A booking the agent made by itself showed no such
                        # warning, so it never cancels anything; the marker of an
                        # unfinished "Marcar outra" ends here.
                        booked_conversation.flow_replaces_appointment_id = None
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_replacement_marker.py tests/test_llm_state_expiry.py tests/test_reactivation.py tests/test_attendee_booking.py tests/test_booking_code_gate.py -q`
Expected: all pass.

- [ ] **Step 9: Lint, diff check, commit**

```bash
uvx ruff format tests/_reminders_r3.py tests/test_appointment_replacement_marker.py
uvx ruff check --fix tests/_reminders_r3.py tests/test_appointment_replacement_marker.py
uvx ruff check src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/orchestrator.py src/secretaria/workers/shared/state_expiry.py src/secretaria/workers/turn_router.py src/secretaria/ai/tools.py
git diff --stat   # each modified file: only the inserted lines
git add tests/_reminders_r3.py tests/test_appointment_replacement_marker.py src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/orchestrator.py src/secretaria/workers/shared/state_expiry.py src/secretaria/workers/turn_router.py src/secretaria/ai/tools.py
git commit -F - <<'EOF'
feat(flow): persist the "Marcar outra" marker and drop it on every exit (TASK-032 R3)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 4: `cancel_replaced_appointment` — the original goes in the new booking's transaction

**Files:**
- Create: `src/secretaria/services/appointment_replacement.py`
- Test: `tests/test_appointment_replacement_service.py`

**Interfaces:**
- Consumes: `Appointment`, `AppointmentStatus`, `is_live_status`; `services/appointment_status.py::log_status_transition`, `SOURCE_FLOW`; `services/payments/deposit_lifecycle.py::on_appointment_cancelled`, `get_deposit_for_appointment`, `cancellation_notice`; `services/patient_context.py::as_utc`.
- Produces: `REPLACEMENT_REASON = "replaced"`, `ReplacedAppointment`, `cancel_replaced_appointment(session, *, tenant, patient_id, replaced_id, new_appointment_id, waba_token=None, now=None) -> ReplacedAppointment | None`. Never commits; never raises for a stale/foreign marker.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_appointment_replacement_service.py`:

```python
"""cancel_replaced_appointment: guards, money hook, transition (TASK-032 R3, spec §4.3)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from secretaria.models import Appointment, AppointmentStatus, Patient, Tenant
from secretaria.services import appointment_replacement as replacement
from secretaria.services.payments import deposit_lifecycle
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import reload_appointment, seed_paid_deposit, seed_world


def _future(days: int = 3) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


async def _new_booking(session, world) -> Appointment:
    start = _future(5)
    new = Appointment(
        tenant_id=world.tenant.id,
        patient_id=world.patient.id,
        conversation_id=world.conversation.id,
        google_event_id="evt-new",
        appointment_type="Consulta",
        start_at=start,
        end_at=start + timedelta(minutes=30),
        status=AppointmentStatus.SCHEDULED,
    )
    session.add(new)
    await session.flush()
    return new


async def _replace(db, world, *, replaced_id=None, patient_id=None, tenant=None):  # noqa: F811
    async with db() as session:
        async with session.begin():
            new = await _new_booking(session, world)
            return await replacement.cancel_replaced_appointment(
                session,
                tenant=tenant or await session.get(Tenant, world.tenant.id),
                patient_id=patient_id or world.patient.id,
                replaced_id=replaced_id or world.appointment.id,
                new_appointment_id=new.id,
            )


async def test_a_live_future_original_is_cancelled(db):  # noqa: F811
    world = await seed_world(db, start_at=_future(), google_event_id="evt-old")

    replaced = await _replace(db, world)

    assert replaced.appointment_id == world.appointment.id
    assert replaced.google_event_id == "evt-old"
    assert replaced.money_note is None
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.CANCELLED


async def test_another_patients_appointment_is_never_cancelled(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    async with db() as session:
        other = Patient(id=uuid4(), tenant_id=world.tenant.id, wa_id="5511900003333")
        session.add(other)
        await session.commit()

    assert await _replace(db, world, patient_id=other.id) is None
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED


async def test_another_clinics_appointment_is_never_cancelled(db):  # noqa: F811
    mine = await seed_world(db, start_at=_future())
    theirs = await seed_world(db, start_at=_future())

    assert await _replace(db, mine, replaced_id=theirs.appointment.id) is None
    assert (await reload_appointment(db, theirs.appointment.id)).status == AppointmentStatus.SCHEDULED


async def test_a_cancelled_or_past_original_is_left_alone(db):  # noqa: F811
    cancelled = await seed_world(db, start_at=_future(), status=AppointmentStatus.CANCELLED)
    past = await seed_world(db, start_at=datetime.now(UTC) - timedelta(hours=2))

    assert await _replace(db, cancelled) is None
    assert await _replace(db, past) is None
    assert (await reload_appointment(db, past.appointment.id)).status == AppointmentStatus.SCHEDULED


async def test_the_new_booking_never_replaces_itself(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    async with db() as session:
        async with session.begin():
            tenant = await session.get(Tenant, world.tenant.id)
            result = await replacement.cancel_replaced_appointment(
                session,
                tenant=tenant,
                patient_id=world.patient.id,
                replaced_id=world.appointment.id,
                new_appointment_id=world.appointment.id,
            )
    assert result is None
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED


async def test_the_money_hook_runs_and_its_notice_is_returned(db, monkeypatch):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    await seed_paid_deposit(db, world)
    calls = []

    async def _hook(session, *, tenant, appointment, waba_token=None, now=None):
        calls.append(appointment.id)
        return "retained"

    monkeypatch.setattr(deposit_lifecycle, "on_appointment_cancelled", _hook)

    replaced = await _replace(db, world)

    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        deposit = await deposit_lifecycle.get_deposit_for_appointment(session, world.appointment.id)
        expected = deposit_lifecycle.cancellation_notice("retained", tenant, deposit)
    assert calls == [world.appointment.id]
    assert replaced.money_note == expected


async def test_a_failing_money_hook_still_cancels_the_original(db, monkeypatch):  # noqa: F811
    world = await seed_world(db, start_at=_future())

    async def _boom(session, **kwargs):
        raise RuntimeError("asaas down")

    monkeypatch.setattr(deposit_lifecycle, "on_appointment_cancelled", _boom)

    replaced = await _replace(db, world)

    assert replaced is not None and replaced.money_note is None
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.CANCELLED
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_replacement_service.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'secretaria.services.appointment_replacement'`.

- [ ] **Step 3: Implement**

Create `src/secretaria/services/appointment_replacement.py`:

```python
"""Cancel the appointment a "Marcar outra consulta" booking replaces (TASK-032 R3).

Spec §4.3 and owner decision 7: the original is cancelled ONLY when the new
booking is confirmed, in the same transaction as the new `Appointment` row -
so a booking that fails to persist never cancels anything, and a patient who
gives up half-way keeps the appointment they had.

Called from the two places an appointment is confirmed from the flow:
`workers/shared/flow_runner.py::_apply_flow_result` (WhatsApp, and a verified
Portal patient) and `workers/shared/booking_hold.py::_promote_booking_hold`
(the Portal code gate). The caller owns the transaction; nothing here commits.
What lives outside the database - the Google event of the original and its
reminder rows - is finished after the commit by
`workers/shared/replacement.py::_finish_replacement`.

The marker is never trusted on its own: the original must belong to the same
clinic AND the same patient as the conversation, still be live and still be in
the future. Anything else is a stale marker and is ignored (logged), never an
error that could cost the patient the booking they just made.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, AppointmentStatus, Tenant, is_live_status
from secretaria.services.appointment_status import SOURCE_FLOW, log_status_transition
from secretaria.services.patient_context import as_utc
from secretaria.services.payments import deposit_lifecycle

logger = get_logger(__name__)

# The reason the reminder rows of the original are closed with
# (services/reminder_hooks.py::after_appointment_closed).
REPLACEMENT_REASON = "replaced"


@dataclass(frozen=True)
class ReplacedAppointment:
    """What the after-commit step and the patient's confirmation need to know."""

    appointment_id: UUID
    google_event_id: str | None
    professional_id: UUID | None
    start_at: datetime
    # The Pix sentence (deposit_lifecycle.cancellation_notice), or None.
    money_note: str | None


async def cancel_replaced_appointment(
    session: AsyncSession,
    *,
    tenant: Tenant,
    patient_id: UUID | None,
    replaced_id: UUID | None,
    new_appointment_id: UUID | None,
    waba_token: str | None = None,
    now: datetime | None = None,
) -> ReplacedAppointment | None:
    """Cancel the replaced appointment inside the caller's transaction.

    Returns None (and changes nothing) when there is no marker, when it points
    at the new booking itself, or when the original is not this patient's live
    future appointment in this clinic. The money hook runs exactly as on a
    normal cancel; if it raises, the original is still cancelled (the patient
    asked for it and the new booking exists) and the failure is logged.
    """
    if replaced_id is None or patient_id is None or replaced_id == new_appointment_id:
        return None
    now = now or datetime.now(UTC)
    original = await session.scalar(
        select(Appointment).where(
            Appointment.id == replaced_id,
            Appointment.tenant_id == tenant.id,
            Appointment.patient_id == patient_id,
        )
    )
    if (
        original is None
        or not is_live_status(original.status)
        or original.start_at is None
        or as_utc(original.start_at) <= now
    ):
        logger.info(
            "appointment_replacement_skipped",
            tenant_id=str(tenant.id),
            replaced_id=str(replaced_id),
            found=original is not None,
        )
        return None

    previous = original.status
    original.status = AppointmentStatus.CANCELLED
    log_status_transition(
        appointment_id=original.id,
        tenant_id=tenant.id,
        old_status=previous,
        new_status=AppointmentStatus.CANCELLED,
        source=SOURCE_FLOW,
        idempotency_key=f"replace:{original.id}",
    )
    money_note: str | None = None
    try:
        outcome = await deposit_lifecycle.on_appointment_cancelled(
            session, tenant=tenant, appointment=original, waba_token=waba_token, now=now
        )
        if outcome is not None:
            deposit = await deposit_lifecycle.get_deposit_for_appointment(session, original.id)
            if deposit is not None:
                money_note = deposit_lifecycle.cancellation_notice(outcome, tenant, deposit)
    except Exception as exc:
        logger.warning(
            "appointment_replacement_money_hook_failed",
            tenant_id=str(tenant.id),
            appointment_id=str(original.id),
            error_type=type(exc).__name__,
        )
    logger.info(
        "appointment_replaced",
        tenant_id=str(tenant.id),
        appointment_id=str(original.id),
        new_appointment_id=str(new_appointment_id),
    )
    return ReplacedAppointment(
        appointment_id=original.id,
        google_event_id=original.google_event_id or None,
        professional_id=original.professional_id,
        start_at=original.start_at,
        money_note=money_note,
    )
```

- [ ] **Step 4: Run them to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_replacement_service.py -q`
Expected: `7 passed`.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format src/secretaria/services/appointment_replacement.py tests/test_appointment_replacement_service.py
uvx ruff check --fix src/secretaria/services/appointment_replacement.py tests/test_appointment_replacement_service.py
git add src/secretaria/services/appointment_replacement.py tests/test_appointment_replacement_service.py
git commit -F - <<'EOF'
feat(flow): cancel_replaced_appointment, patient-scoped and Pix-aware (TASK-032 R3)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 5: Consume the marker where the booking is confirmed (flow tail and Portal promotion)

**Files:**
- Create: `src/secretaria/workers/shared/replacement.py`
- Modify: `src/secretaria/workers/shared/flow_runner.py` (imports; `_apply_flow_result`)
- Modify: `src/secretaria/workers/shared/booking_hold.py` (imports; `_promote_booking_hold`)
- Test: `tests/test_appointment_replacement_wiring.py`

**Interfaces:**
- Consumes: Task 4 `cancel_replaced_appointment`, `ReplacedAppointment`, `REPLACEMENT_REASON`; Task 3 marker; R2 `reminder_hooks.enabled_for`, `after_appointment_closed`; `workers/shared/llm_context.py::_appointment_calendar`, `_appointment_calendar_target`; `services/tenant_config.py::list_active_professionals`; `workers/shared/greeting.py::_format_appointment_when`.
- Produces: `REPLACED_TEXT`, `_replaced_notice(tenant, replaced) -> str`, `_finish_replacement(tenant, replaced) -> None`; behavior: a confirmed booking with a marker cancels the original in the same transaction, then deletes its Google event, closes its reminder rows and tells the patient.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_appointment_replacement_wiring.py`:

```python
"""Confirming the new booking replaces the original in the same turn (TASK-032 R3, spec §4.3)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from secretaria.ai.formatter import TextBubble
from secretaria.models import Appointment, AppointmentStatus, BookingHold, FlowState
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.flow_router import FlowRouterResult
from secretaria.workers import tasks
from secretaria.workers.shared.greeting import _format_appointment_when
from tests._patching import workers_ns
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import get_conversation, reminder_rows, sent, set_conversation, wire
from tests._reminders_v2 import WA_ID, add_reminder, get_reminder, reload_appointment, seed_world

CONFIRMED = "Pronto! Seu agendamento está confirmado. ✅"


@pytest.fixture
def calendar(monkeypatch, db):  # noqa: F811
    return wire(monkeypatch, db)


def _future(days: int = 3) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


def _booking(start: datetime) -> FlowRouterResult:
    return FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=CONFIRMED)],
        flow_state=FlowState.IDLE,
        appointment={
            "google_event_id": "evt-new",
            "appointment_type": "Consulta",
            "start_at": start,
            "end_at": start + timedelta(minutes=30),
        },
    )


async def _confirm(world, result) -> None:
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="✅ Confirmar"
    )
    await tasks._apply_flow_result(reply, result, WA_ID, tenant=world.tenant, waba_token="t")


async def _new_appointment(db) -> Appointment | None:  # noqa: F811
    async with db() as session:
        return await session.scalar(
            select(Appointment).where(Appointment.google_event_id == "evt-new")
        )


def _notice(world) -> str:
    when = _format_appointment_when(world.start_at, "America/Sao_Paulo")
    return f"Sua consulta anterior, de {when}, foi cancelada."


async def test_confirming_the_new_booking_cancels_the_original_in_the_same_turn(
    db, calendar  # noqa: F811
):
    world = await seed_world(db, start_at=_future(), google_event_id="evt-old")
    old_row = await add_reminder(db, world, kind="day", due_at=_future(2))
    await set_conversation(
        db,
        world,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_replaces_appointment_id=world.appointment.id,
    )

    await _confirm(world, _booking(_future(6)))

    new = await _new_appointment(db)
    assert new is not None and new.status == AppointmentStatus.SCHEDULED
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.CANCELLED
    assert calendar.cancelled == ["evt-old"]
    assert (await get_reminder(db, old_row)).status == "cancelled"
    assert any(row.status == "pending" for row in await reminder_rows(db, new.id))
    assert (await get_conversation(db, world)).flow_replaces_appointment_id is None
    [text] = [item[2] for item in sent() if item[0] == "text"]
    assert text == f"{CONFIRMED}\n\n{_notice(world)}"


async def test_without_a_marker_nothing_else_is_cancelled(db, calendar):  # noqa: F811
    world = await seed_world(db, start_at=_future(), google_event_id="evt-old")

    await _confirm(world, _booking(_future(6)))

    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED
    assert calendar.cancelled == []
    assert [item[2] for item in sent() if item[0] == "text"] == [CONFIRMED]


async def test_a_booking_that_fails_to_persist_cancels_nothing(db, calendar, monkeypatch):  # noqa: F811
    world = await seed_world(db, start_at=_future(), google_event_id="evt-old")
    await set_conversation(
        db,
        world,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_replaces_appointment_id=world.appointment.id,
    )

    async def _broken(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(workers_ns, "resolve_booking_plan_ids", _broken)

    await _confirm(world, _booking(_future(6)))

    assert await _new_appointment(db) is None
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED
    assert calendar.cancelled == []
    # Rolled back with the booking: the patient can still finish it.
    assert (await get_conversation(db, world)).flow_replaces_appointment_id == world.appointment.id


async def test_the_portal_promotion_replaces_the_original_too(db, calendar):  # noqa: F811
    world = await seed_world(
        db, start_at=_future(), channel=CHANNEL_BRAIN_MESSAGE, google_event_id="evt-old"
    )
    await set_conversation(
        db,
        world,
        flow_state=FlowState.AWAITING_EMAIL_CODE,
        flow_replaces_appointment_id=world.appointment.id,
    )
    start = _future(6)
    async with db() as session:
        session.add(
            BookingHold(
                tenant_id=world.tenant.id,
                conversation_id=world.conversation.id,
                patient_id=world.patient.id,
                appointment_type="Consulta",
                start_at=start,
                end_at=start + timedelta(minutes=30),
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        await session.commit()
    reply = tasks._ReplyContext(
        channel=CHANNEL_BRAIN_MESSAGE,
        conversation_id=world.conversation.id,
        patient_ref=world.patient.external_id,
        inbound_body="123456",
    )

    extra = await tasks._promote_booking_hold(
        reply, tenant=world.tenant, waba_token=None, professionals=[], redis=None
    )

    assert "Pronto! Seu agendamento está confirmado." in extra
    assert extra.endswith(_notice(world))
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.CANCELLED
    assert calendar.cancelled == ["evt-old"]
    assert (await get_conversation(db, world)).flow_replaces_appointment_id is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_replacement_wiring.py -q`
Expected: the two replacement tests FAIL (`SCHEDULED != CANCELLED`, `[] != ['evt-old']`); `test_without_a_marker...` and `test_a_booking_that_fails_to_persist...` pass already and must keep passing.

- [ ] **Step 3: The after-commit half**

Create `src/secretaria/workers/shared/replacement.py`:

```python
"""After a "Marcar outra" booking committed: finish the original (TASK-032 R3).

`services/appointment_replacement.py::cancel_replaced_appointment` cancels the
original inside the new booking's transaction. What cannot live in that
transaction runs here, AFTER the commit, best-effort - a failure is logged and
never undoes the booking the patient just confirmed:

  * the original's Google Calendar event is deleted on the calendar that OWNS
    it (its professional's, or the clinic's) - never a guessed agenda; with an
    owner that no longer resolves nothing is deleted and that is logged, the
    same "don't guess, degrade" rule as `actions.py::_calendar_for_appointment`;
  * the original's pending reminder rows are cancelled through R2's hook.
"""

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.services import reminder_hooks
from secretaria.services.appointment_replacement import REPLACEMENT_REASON, ReplacedAppointment
from secretaria.services.tenant_config import list_active_professionals
from secretaria.workers.shared.greeting import _format_appointment_when
from secretaria.workers.shared.llm_context import (
    _appointment_calendar,
    _appointment_calendar_target,
)

logger = get_logger(__name__)

REPLACED_TEXT = "Sua consulta anterior, de {when}, foi cancelada."


def _replaced_notice(tenant, replaced: ReplacedAppointment) -> str:
    """The line appended to the new booking's confirmation (plus the Pix sentence)."""
    text = REPLACED_TEXT.format(when=_format_appointment_when(replaced.start_at, tenant.timezone))
    return f"{text} {replaced.money_note}" if replaced.money_note else text


async def _finish_replacement(tenant, replaced: ReplacedAppointment) -> None:
    """Delete the original's Google event and close its reminder rows. Never raises."""
    if replaced.google_event_id:
        try:
            async with async_session_factory() as session:
                professional_rows = await list_active_professionals(session, tenant.id)
                target = _appointment_calendar_target(
                    {"professional_id": replaced.professional_id}, professional_rows
                )
                calendar = await _appointment_calendar(session, tenant, target)
            if calendar is None:
                logger.warning(
                    "appointment_replacement_calendar_missing",
                    tenant_id=str(tenant.id),
                    appointment_id=str(replaced.appointment_id),
                )
            else:
                await calendar.cancel_event(replaced.google_event_id)
        except Exception as exc:
            logger.warning(
                "appointment_replacement_calendar_cancel_failed",
                tenant_id=str(tenant.id),
                appointment_id=str(replaced.appointment_id),
                error_type=type(exc).__name__,
            )
    if reminder_hooks.enabled_for(tenant):
        await reminder_hooks.after_appointment_closed(
            replaced.appointment_id, reason=REPLACEMENT_REASON
        )
```

- [ ] **Step 4: Wire the flow tail**

In `src/secretaria/workers/shared/flow_runner.py`:

(a) Imports — after the line `from secretaria.services.attendee import (` block's closing `)` add:

```python
from secretaria.services.appointment_replacement import (
    ReplacedAppointment,
    cancel_replaced_appointment,
)
```

and after the `from secretaria.workers.shared.llm_context import (` block's closing `)` add:

```python
from secretaria.workers.shared.replacement import (
    _finish_replacement,
    _replaced_notice,
)
```

(b) Replace

```python
    cancellation_note: str | None = None
```

(the first occurrence, right after `booked_appointment: Appointment | None = None`) with

```python
    cancellation_note: str | None = None
    # TASK-032 R3: the appointment a "Marcar outra" booking replaced, if any.
    replaced: ReplacedAppointment | None = None
```

(c) Replace

```python
                conv = await session.get(Conversation, reply.conversation_id)
                if conv is not None:
                    conv.flow_state = result.flow_state
```

with

```python
                conv = await session.get(Conversation, reply.conversation_id)
                if conv is not None:
                    # TASK-032 R3: read BEFORE the writes below overwrite it.
                    replaced_id = conv.flow_replaces_appointment_id
                    conv.flow_state = result.flow_state
```

(d) Replace

```python
                        session.add(booked_appointment)
```

with

```python
                        session.add(booked_appointment)
                        # TASK-032 R3 ("Marcar outra consulta"): the original is
                        # cancelled HERE - same transaction as the new row, and
                        # only now that the new booking is confirmed. A failed
                        # persist rolls both back together.
                        if replaced_id is not None and tenant is not None:
                            await session.flush()
                            replaced = await cancel_replaced_appointment(
                                session,
                                tenant=tenant,
                                patient_id=conv.patient_id,
                                replaced_id=replaced_id,
                                new_appointment_id=booked_appointment.id,
                                waba_token=waba_token,
                            )
```

(e) Replace

```python
        await enqueue_post_booking_hooks(redis, tenant.id, booked_appointment.id, source="flow")
```

(inside `if booked_appointment is not None and persisted and tenant is not None:`) with

```python
        await enqueue_post_booking_hooks(redis, tenant.id, booked_appointment.id, source="flow")
        if replaced is not None:
            await _finish_replacement(tenant, replaced)
```

(R2's `after_appointment_booked` lines stay where R2 put them, below.)

(f) Replace

```python
            if cancellation_note:
                result.bubbles[-1].body = f"{result.bubbles[-1].body}\n\n{cancellation_note}"
```

with

```python
            if cancellation_note:
                result.bubbles[-1].body = f"{result.bubbles[-1].body}\n\n{cancellation_note}"
            if replaced is not None and persisted and tenant is not None:
                notice = _replaced_notice(tenant, replaced)
                result.bubbles[-1].body = f"{result.bubbles[-1].body}\n\n{notice}"
```

- [ ] **Step 5: Wire the Portal promotion**

In `src/secretaria/workers/shared/booking_hold.py`:

(a) Imports — after the `from secretaria.services.appointment_status import (` block add:

```python
from secretaria.services.appointment_replacement import (
    ReplacedAppointment,
    cancel_replaced_appointment,
)
```

and after the `from secretaria.workers.shared.llm_context import (` block add:

```python
from secretaria.workers.shared.replacement import (
    _finish_replacement,
    _replaced_notice,
)
```

(b) In `_promote_booking_hold`, replace

```python
    try:
        async with async_session_factory() as session:
            async with session.begin():
                (
                    appointment.insurance_plan_id,
                    appointment.insurance_professional_plan_id,
                ) = await resolve_booking_plan_ids(
                    session, tenant.id, held.insurance, held.professional_id
                )
                session.add(appointment)
```

with

```python
    # TASK-032 R3: the appointment a "Marcar outra" booking replaced, if any.
    replaced: ReplacedAppointment | None = None
    try:
        async with async_session_factory() as session:
            async with session.begin():
                (
                    appointment.insurance_plan_id,
                    appointment.insurance_professional_plan_id,
                ) = await resolve_booking_plan_ids(
                    session, tenant.id, held.insurance, held.professional_id
                )
                session.add(appointment)
                # The Portal booking is confirmed HERE (the code gate), so the
                # original goes in this same transaction (spec §4.3).
                conversation = await session.get(Conversation, reply.conversation_id)
                if (
                    conversation is not None
                    and conversation.flow_replaces_appointment_id is not None
                ):
                    await session.flush()
                    replaced = await cancel_replaced_appointment(
                        session,
                        tenant=tenant,
                        patient_id=conversation.patient_id,
                        replaced_id=conversation.flow_replaces_appointment_id,
                        new_appointment_id=appointment.id,
                        waba_token=waba_token,
                    )
                    conversation.flow_replaces_appointment_id = None
```

(c) Replace the tail of the function, from `    tz = _tenant_tzinfo(tenant)` to the end of the final `return (...)` statement:

```python
    tz = _tenant_tzinfo(tenant)
    local_start = held.start_at.astimezone(tz)
    local_end = held.end_at.astimezone(tz)
    return (
        "Pronto! Seu agendamento está confirmado. ✅\n\n"
        f"{service_type}\n"
        + (f"Paciente: {held.attendee_name}\n" if held.attendee_name else "")
        + f"{local_start.strftime(WHEN_FORMAT)}\n\n"
        "Adicionar à sua agenda:\n"
        f"{build_patient_calendar_link(local_start, local_end, summary, tz=tz)}"
    )
```

with

```python
    if replaced is not None:
        await _finish_replacement(tenant, replaced)

    tz = _tenant_tzinfo(tenant)
    local_start = held.start_at.astimezone(tz)
    local_end = held.end_at.astimezone(tz)
    confirmation = (
        "Pronto! Seu agendamento está confirmado. ✅\n\n"
        f"{service_type}\n"
        + (f"Paciente: {held.attendee_name}\n" if held.attendee_name else "")
        + f"{local_start.strftime(WHEN_FORMAT)}\n\n"
        "Adicionar à sua agenda:\n"
        f"{build_patient_calendar_link(local_start, local_end, summary, tz=tz)}"
    )
    if replaced is not None:
        confirmation = f"{confirmation}\n\n{_replaced_notice(tenant, replaced)}"
    return confirmation
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_replacement_wiring.py tests/test_booking_code_gate.py tests/test_reminder_v2_wiring.py tests/test_flow_cancel_money.py tests/test_workers_layering.py -q`
Expected: all pass.

- [ ] **Step 7: Lint, diff check, commit**

```bash
uvx ruff format src/secretaria/workers/shared/replacement.py tests/test_appointment_replacement_wiring.py
uvx ruff check --fix src/secretaria/workers/shared/replacement.py tests/test_appointment_replacement_wiring.py
uvx ruff check src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/shared/booking_hold.py
git diff --stat
git add src/secretaria/workers/shared/replacement.py src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/shared/booking_hold.py tests/test_appointment_replacement_wiring.py
git commit -F - <<'EOF'
feat(flow): the confirmed "Marcar outra" booking cancels the original (TASK-032 R3)

Same transaction as the new appointment (flow tail and Portal promotion); the
original's Google event and reminder rows are finished after the commit.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 6: The cancel-path button ids

**Files:**
- Modify: `src/secretaria/services/reminder_text.py` (append after R2's `button_payloads`)
- Modify: `src/secretaria/schemas/webhook.py` (`_ACTION_BUTTON_PREFIXES`, after R2's `"remother|",`)
- Test: `tests/test_reminder_r3_ids.py`

**Interfaces:**
- Consumes: R2 `REMINDER_ACTIONS`, `decode_action_id`, `_ACTION_BUTTON_PREFIXES`; `MAX_BUTTON_LABEL_CHARS`.
- Produces: `ACTION_RESCHEDULE_THIS`, `ACTION_BOOK_ANOTHER`, `ACTION_GIVE_UP`, `ACTION_GIVE_UP_CONFIRM`, `ACTION_KEEP`, `CANCEL_PATH_ACTIONS`, `REMINDER_ROW_ACTIONS`, `LABEL_RESCHEDULE_THIS`, `LABEL_BOOK_ANOTHER`, `LABEL_GIVE_UP`, `LABEL_GIVE_UP_CONFIRM`, `LABEL_KEEP`, `cancel_path_buttons(reminder_id)`, `give_up_confirm_buttons(reminder_id)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reminder_r3_ids.py`:

```python
"""The cancel-path button ids (TASK-032 R3, spec §4.3)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.core.whatsapp_limits import MAX_BUTTON_LABEL_CHARS  # noqa: E402
from secretaria.schemas import webhook  # noqa: E402
from secretaria.schemas.webhook import decode_action_id  # noqa: E402
from secretaria.services import reminder_text as rt  # noqa: E402


@pytest.mark.parametrize("action", ["remresched", "remnew", "remgiveup", "remgiveupyes", "remkeep"])
def test_the_cancel_path_ids_decode(action):
    reminder_id = str(uuid4())
    assert decode_action_id(f"{action}|{reminder_id}") == (action, reminder_id)


def test_a_longer_id_never_decodes_as_its_shorter_sibling():
    reminder_id = str(uuid4())
    assert decode_action_id(f"remgiveupyes|{reminder_id}") == ("remgiveupyes", reminder_id)
    assert decode_action_id(f"remgiveup|{reminder_id}") == ("remgiveup", reminder_id)


def test_the_cancel_card_is_the_three_way_choice():
    reminder_id = uuid4()
    assert rt.cancel_path_buttons(reminder_id) == [
        (f"remresched|{reminder_id}", "Remarcar esta"),
        (f"remnew|{reminder_id}", "Marcar outra"),
        (f"remgiveup|{reminder_id}", "Não vou mais"),
    ]


def test_the_give_up_confirmation_card():
    reminder_id = uuid4()
    assert rt.give_up_confirm_buttons(reminder_id) == [
        (f"remgiveupyes|{reminder_id}", "Sim, cancelar"),
        (f"remkeep|{reminder_id}", "Manter consulta"),
    ]


def test_every_new_label_fits_a_whatsapp_button():
    labels = [
        rt.LABEL_RESCHEDULE_THIS,
        rt.LABEL_BOOK_ANOTHER,
        rt.LABEL_GIVE_UP,
        rt.LABEL_GIVE_UP_CONFIRM,
        rt.LABEL_KEEP,
    ]
    assert all(0 < len(label) <= MAX_BUTTON_LABEL_CHARS for label in labels)


def test_the_row_actions_are_exactly_the_rem_prefixes_the_decoder_knows():
    known = {prefix[:-1] for prefix in webhook._ACTION_BUTTON_PREFIXES if prefix.startswith("rem")}
    assert known == set(rt.REMINDER_ROW_ACTIONS)
    assert rt.REMINDER_ACTIONS == ("remconfirm", "remcancel", "remother")
    assert rt.REMINDER_ROW_ACTIONS == rt.REMINDER_ACTIONS + rt.CANCEL_PATH_ACTIONS
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_r3_ids.py -q`
Expected: FAIL — `decode_action_id(...)` returns `None` for the five new ids and `AttributeError: module ... has no attribute 'cancel_path_buttons'`.

- [ ] **Step 3: Builders and constants**

Append to `src/secretaria/services/reminder_text.py`:

```python


# --- The "Cancelar" path (TASK-032 R3, spec §4.3) -------------------------------
# Same "<action>|<reminder_id>" shape as the trio above, so every tap of the
# path is checked against the patient who tapped
# (workers/shared/reminder_actions.py). `REMINDER_ACTIONS` stays the trio the
# reminder itself carries; the dispatcher and the Portal decoder use the union.
ACTION_RESCHEDULE_THIS = "remresched"
ACTION_BOOK_ANOTHER = "remnew"
ACTION_GIVE_UP = "remgiveup"
ACTION_GIVE_UP_CONFIRM = "remgiveupyes"
ACTION_KEEP = "remkeep"
CANCEL_PATH_ACTIONS: tuple[str, ...] = (
    ACTION_RESCHEDULE_THIS,
    ACTION_BOOK_ANOTHER,
    ACTION_GIVE_UP,
    ACTION_GIVE_UP_CONFIRM,
    ACTION_KEEP,
)
REMINDER_ROW_ACTIONS: tuple[str, ...] = REMINDER_ACTIONS + CANCEL_PATH_ACTIONS

# <= 20 characters each. The spec's full wording ("Remarcar esta consulta",
# "Marcar outra consulta") is longer than a WhatsApp button allows, so it goes
# in the card body (reminder_actions.CANCEL_PATH_TEXT) and the buttons carry
# the short form.
LABEL_RESCHEDULE_THIS = "Remarcar esta"
LABEL_BOOK_ANOTHER = "Marcar outra"
LABEL_GIVE_UP = "Não vou mais"
LABEL_GIVE_UP_CONFIRM = "Sim, cancelar"
LABEL_KEEP = "Manter consulta"


def cancel_path_buttons(reminder_id) -> list[tuple[str, str]]:
    """The "O que você prefere?" card: Remarcar esta / Marcar outra / Não vou mais."""
    return [
        (f"{ACTION_RESCHEDULE_THIS}|{reminder_id}", LABEL_RESCHEDULE_THIS),
        (f"{ACTION_BOOK_ANOTHER}|{reminder_id}", LABEL_BOOK_ANOTHER),
        (f"{ACTION_GIVE_UP}|{reminder_id}", LABEL_GIVE_UP),
    ]


def give_up_confirm_buttons(reminder_id) -> list[tuple[str, str]]:
    """The "Não vou mais" confirmation: Sim, cancelar / Manter consulta."""
    return [
        (f"{ACTION_GIVE_UP_CONFIRM}|{reminder_id}", LABEL_GIVE_UP_CONFIRM),
        (f"{ACTION_KEEP}|{reminder_id}", LABEL_KEEP),
    ]
```

- [ ] **Step 4: Teach the decoder**

In `src/secretaria/schemas/webhook.py`, inside `_ACTION_BUTTON_PREFIXES`, replace

```python
    "remother|",
```

with

```python
    "remother|",
    # TASK-032 R3: the "Cancelar" path of a reminder. Same row-id scoping as the
    # three above (workers/shared/reminder_actions.py). The trailing bar keeps
    # "remgiveupyes|" from ever matching as "remgiveup|".
    "remresched|",
    "remnew|",
    "remgiveup|",
    "remgiveupyes|",
    "remkeep|",
```

- [ ] **Step 5: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_r3_ids.py tests/test_reminder_v2_text.py tests/test_action_buttons.py -q`
Expected: all pass (R2's `test_builders_and_decoder_agree` still pairs the trio).

- [ ] **Step 6: Lint, bidi scan, commit**

```bash
uvx ruff format tests/test_reminder_r3_ids.py
uvx ruff check --fix tests/test_reminder_r3_ids.py
uvx ruff check src/secretaria/services/reminder_text.py src/secretaria/schemas/webhook.py
uv run python -c "import sys;[print(f,hex(ord(c))) for f in sys.argv[1:] for c in open(f,encoding='utf-8').read() if 0x200e<=ord(c)<=0x202e or 0x2066<=ord(c)<=0x2069]" src/secretaria/services/reminder_text.py tests/test_reminder_r3_ids.py
git diff --stat
git add src/secretaria/services/reminder_text.py src/secretaria/schemas/webhook.py tests/test_reminder_r3_ids.py
git commit -F - <<'EOF'
feat(reminders): ids and labels of the Cancelar path (TASK-032 R3)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 7: The reminder buttons — three-way card, "Não vou mais" confirm, keep, Outro → AI

**Files:**
- Rewrite: `src/secretaria/workers/shared/reminder_actions.py` (R2 created it; replace the whole file)
- Modify: `src/secretaria/workers/shared/actions.py` (the R2 dispatch block at the top of `_handle_action_button`; its `REMINDER_ACTIONS` import)
- Modify: `tests/test_reminder_v2_buttons.py` (R2's interim-card test)
- Test: `tests/test_reminder_r3_cancel_path.py`

**Interfaces:**
- Consumes: Task 6 ids/builders; R1 `register_confirmation`, `CONFIRMATION_SOURCE_REMINDER_BUTTON`, `CONFIRMATION_SOURCE_CHAT_PROMPT`, `ReminderMismatchError`, `REMINDER_ANSWER_*`, `REMINDER_KIND_CHAT`; `workers/shared/deposit.py::_hours_until_start`, `_pix_retention_warning_line`; `deposit_lifecycle.get_deposit_for_appointment`; `workers/shared/flow_runner.py::_apply_flow_result`.
- Produces: `handle_reminder_button(reply, action, reminder_id, redis=None) -> tuple[str, str] | None` (a continuation `(action, appointment_id)` for `actions.py`, or None when the tap was fully answered); `CONTINUE_RESCHEDULE = "apptresched"`, `CONTINUE_CANCEL_AND_ASK_WHY`, `CONTINUE_BOOK_ANOTHER`; `_retention_warning(session, tenant, appointment, now) -> str | None`; texts `NOT_FOUND_TEXT`, `NOT_ACTIVE_TEXT`, `CONFIRMED_TEXT`, `MOVED_TEXT`, `CANCEL_PATH_TEXT`, `OTHER_TEXT`, `GIVE_UP_CONFIRM_TEXT`, `KEPT_TEXT`, `ALSO_SCHEDULED_TEXT`, `BOOK_ANOTHER_INTRO`. R2's `cancel_path_buttons(appointment_id)` is removed (Task 6's `reminder_text.cancel_path_buttons(reminder_id)` replaces it).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reminder_r3_cancel_path.py`:

```python
"""Cancelar → Remarcar esta / Marcar outra / Não vou mais; Outro → AI (TASK-032 R3, spec §4.3)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS
from secretaria.models import (
    AppointmentStatus,
    Conversation,
    FlowState,
    Patient,
    PixDeposit,
    Tenant,
)
from secretaria.services.reminder_text import cancel_path_buttons, give_up_confirm_buttons
from secretaria.workers import tasks
from secretaria.workers.shared import reminder_actions as ra
from secretaria.workers.shared.deposit import _pix_retention_warning_line
from secretaria.workers.shared.greeting import _format_appointment_when
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import add_appointment, get_conversation, sent, wire
from tests._reminders_v2 import (
    WA_ID,
    add_reminder,
    get_reminder,
    reload_appointment,
    seed_paid_deposit,
    seed_world,
)


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


def _future(hours: int = 48) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


async def _tap(world, action, reminder_id, *, conversation_id=None, patient_ref=WA_ID) -> None:
    reply = tasks._ReplyContext(
        conversation_id=conversation_id or world.conversation.id,
        patient_ref=patient_ref,
        inbound_body="x",
    )
    await tasks._handle_action_button(reply, action, str(reminder_id))


def _when(start) -> str:
    return _format_appointment_when(start, "America/Sao_Paulo")


def _texts() -> list[str]:
    return [item[2] for item in sent() if item[0] == "text"]


async def _deposit_and_tenant(db, world):  # noqa: F811
    async with db() as session:
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == world.appointment.id)
        )
        return deposit, await session.get(Tenant, world.tenant.id)


async def test_cancel_offers_the_three_way_card(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world, "remcancel", rid)

    [(kind, _to, body, buttons)] = sent()
    assert (kind, body) == ("buttons", ra.CANCEL_PATH_TEXT)
    assert buttons == cancel_path_buttons(rid)
    assert len(ra.CANCEL_PATH_TEXT) <= MAX_INTERACTIVE_BODY_CHARS
    assert (await get_reminder(db, rid)).answer == "cancel"
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED


async def test_give_up_asks_for_confirmation_first(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world, "remgiveup", rid)

    [(kind, _to, body, buttons)] = sent()
    assert kind == "buttons"
    assert body == ra.GIVE_UP_CONFIRM_TEXT.format(when=_when(world.start_at))
    assert buttons == give_up_confirm_buttons(rid)
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED


async def test_give_up_inside_the_refund_window_warns_about_the_deposit_first(db):  # noqa: F811
    world = await seed_world(db, start_at=_future(hours=5))
    await seed_paid_deposit(db, world)
    rid = await add_reminder(db, world, kind="hour")

    await _tap(world, "remgiveup", rid)

    deposit, tenant = await _deposit_and_tenant(db, world)
    [(_kind, _to, body, _buttons)] = sent()
    warning = _pix_retention_warning_line(tenant, deposit)
    assert body == f"{warning} {ra.GIVE_UP_CONFIRM_TEXT.format(when=_when(world.start_at))}"


async def test_keep_leaves_the_appointment_and_says_so(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world, "remkeep", rid)

    assert _texts() == [ra.KEPT_TEXT.format(when=_when(world.start_at))]
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED


async def test_other_hands_the_conversation_to_the_ai(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world, "remother", rid)

    assert _texts() == [ra.OTHER_TEXT]
    assert (await get_conversation(db, world)).flow_state == FlowState.LLM
    assert (await get_reminder(db, rid)).answer == "other"


async def test_remarcar_esta_enters_the_reschedule_of_this_appointment(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world, "remresched", rid)

    conversation = await get_conversation(db, world)
    assert conversation.flow_state == FlowState.MANAGE_BOOKING
    assert conversation.flow_managing_appointment_id == world.appointment.id


async def test_remarcar_esta_respects_the_pix_reschedule_limit(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    await seed_paid_deposit(db, world)
    async with db() as session:
        await session.execute(
            update(PixDeposit)
            .where(PixDeposit.appointment_id == world.appointment.id)
            .values(reschedule_count=world.tenant.pix_reschedule_limit)
        )
        await session.commit()
    rid = await add_reminder(db, world)

    await _tap(world, "remresched", rid)

    assert [item[0] for item in sent()] == ["buttons"]
    assert (await get_conversation(db, world)).flow_state == FlowState.IDLE


async def test_cancel_path_taps_are_checked_against_the_patient_and_the_version(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)
    async with db() as session:
        intruder = Patient(id=uuid4(), tenant_id=world.tenant.id, wa_id="5511900002222")
        session.add(intruder)
        await session.flush()
        intruder_conversation = Conversation(
            id=uuid4(), tenant_id=world.tenant.id, patient_id=intruder.id
        )
        session.add(intruder_conversation)
        await session.commit()

    for action in ("remresched", "remnew", "remgiveup", "remgiveupyes", "remkeep"):
        await _tap(
            world,
            action,
            rid,
            conversation_id=intruder_conversation.id,
            patient_ref="5511900002222",
        )
    old = await add_reminder(
        db, world, kind="hour", appointment_start_at=world.start_at - timedelta(days=1)
    )
    await _tap(world, "remgiveupyes", old)

    assert _texts() == [ra.NOT_FOUND_TEXT] * 5 + [
        ra.MOVED_TEXT.format(when=_when(world.start_at))
    ]
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED
    assert (await get_conversation(db, world)).flow_state == FlowState.IDLE


async def test_a_chat_card_confirmation_names_the_other_appointments(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    other = await add_appointment(db, world, start_at=_future(hours=24 * 6))
    rid = await add_reminder(db, world, kind="chat")

    await _tap(world, "remconfirm", rid)

    also = ra.ALSO_SCHEDULED_TEXT.format(whens=_when(other.start_at))
    assert _texts() == [f"{ra.CONFIRMED_TEXT.format(when=_when(world.start_at))}\n\n{also}"]
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 1


async def test_a_cron_card_confirmation_stays_as_r2_wrote_it(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    await add_appointment(db, world, start_at=_future(hours=24 * 6))
    rid = await add_reminder(db, world, kind="day")

    await _tap(world, "remconfirm", rid)

    assert _texts() == [ra.CONFIRMED_TEXT.format(when=_when(world.start_at))]
```

- [ ] **Step 2: Update R2's interim-card test**

In `tests/test_reminder_v2_buttons.py`, replace the whole function `test_cancel_offers_reschedule_or_give_up_on_this_appointment` (from its `async def` line to the blank line before the next `async def`) with:

```python
async def test_cancel_offers_the_three_way_card(db):  # noqa: F811
    # TASK-032 R3 replaced R2's interim Remarcar / Não vou mais card.
    from secretaria.services.reminder_text import cancel_path_buttons
    from secretaria.workers.shared.reminder_actions import CANCEL_PATH_TEXT

    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world.conversation.id, "remcancel", rid)

    [(kind, _to, body, buttons)] = FakeWhatsAppClient.all_sent()
    assert (kind, body) == ("buttons", CANCEL_PATH_TEXT)
    assert buttons == cancel_path_buttons(rid)
    row = await get_reminder(db, rid)
    assert row.answer == "cancel" and row.answered_at is not None
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED
```

- [ ] **Step 3: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_r3_cancel_path.py tests/test_reminder_v2_buttons.py -q`
Expected: FAIL — `AttributeError: module 'secretaria.workers.shared.reminder_actions' has no attribute 'GIVE_UP_CONFIRM_TEXT'` (and siblings), the card body is still `"O que você prefere?"` with R2's buttons, and the five cancel-path taps are not dispatched at all (`remresched` etc. are not in `REMINDER_ACTIONS`, so `_handle_action_button` looks the ROW id up as an appointment and answers `Não encontrei essa consulta.`).

- [ ] **Step 4: Rewrite the handler**

Replace the whole content of `src/secretaria/workers/shared/reminder_actions.py` with:

```python
"""A tap on a reminder button (TASK-032 R2 + R3).

The id after the bar is an `appointment_reminders` ROW id. The older
`apptconfirm|<appointment_id>` family can only be checked against the TENANT;
the row names the patient too, so a tap is honoured only when the row's tenant
AND patient are the tapping conversation's. Anything else gets the same polite
miss, whichever check failed - never reveal whose row it is.

R2 (the reminder itself): Confirmar / Cancelar / Outro.
R3 (spec §4.3, the "Cancelar" path), every step on the same row id:
    Cancelar      -> "O que você prefere?": Remarcar esta / Marcar outra / Não vou mais
    Remarcar esta -> the existing reschedule of THIS appointment (Pix limits apply)
    Marcar outra  -> a new booking that replaces this one only once confirmed
    Não vou mais  -> "Tem certeza?" (Pix retention line inside the refund window)
                     -> Sim, cancelar -> cancel + "por quê?" / Manter consulta
    Outro         -> the conversation goes to the AI (TASK-030's hand-backs bring
                     it back to the flow)

Three of those steps need the appointment-scoped machinery in
`workers/shared/actions.py` (reschedule entry with the Pix limit, the cancel,
the booking entry). This module validates the row and returns a CONTINUATION
`(action, appointment_id)`; `_handle_action_button` then runs its own branch for
it. The continuation names are not button prefixes, so nobody can send one.

Channel-neutral: replies go through `_reply_sender`, so a Portal tap
(workers/portal/inbound.py) is answered in the Portal. Runs before the
handover/flow/LLM gates, like every action button (workers/turn_router.py).
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    LIVE_APPOINTMENT_STATUSES,
    Appointment,
    AppointmentReminder,
    Conversation,
    FlowState,
    PixDepositStatus,
    Tenant,
    is_live_status,
)
from secretaria.models.appointment_reminder import (
    REMINDER_ANSWER_CANCEL,
    REMINDER_ANSWER_CONFIRM,
    REMINDER_ANSWER_OTHER,
    REMINDER_KIND_CHAT,
)
from secretaria.services import reminder_schedule
from secretaria.services.flow_router import FlowRouterResult
from secretaria.services.payments import deposit_lifecycle
from secretaria.services.reminder_text import (
    ACTION_BOOK_ANOTHER,
    ACTION_CANCEL,
    ACTION_CONFIRM,
    ACTION_GIVE_UP,
    ACTION_GIVE_UP_CONFIRM,
    ACTION_KEEP,
    ACTION_OTHER,
    ACTION_RESCHEDULE_THIS,
    REMINDER_ROW_ACTIONS,
    cancel_path_buttons,
    give_up_confirm_buttons,
)
from secretaria.services.tenant_config import get_waba_token
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.deposit import _hours_until_start, _pix_retention_warning_line
from secretaria.workers.shared.flow_runner import _apply_flow_result
from secretaria.workers.shared.greeting import _format_appointment_when
from secretaria.workers.shared.sender import _reply_sender
from secretaria.workers.shared.text import _as_utc

logger = get_logger(__name__)

NOT_FOUND_TEXT = "Não encontrei essa consulta."
NOT_ACTIVE_TEXT = "Essa consulta não está mais ativa."
CONFIRMED_TEXT = "Presença confirmada! ✅ Até {when}."
MOVED_TEXT = "Essa mensagem era sobre um horário antigo. Sua consulta agora é em {when}."
CANCEL_PATH_TEXT = (
    "O que você prefere?\n\n"
    "• *Remarcar esta*: mudar o dia ou o horário desta consulta.\n"
    "• *Marcar outra*: escolher uma consulta diferente. Esta só é cancelada "
    "quando a nova for confirmada.\n"
    "• *Não vou mais*: cancelar esta consulta."
)
OTHER_TEXT = "Claro! Me conta como posso te ajudar com a sua consulta."
GIVE_UP_CONFIRM_TEXT = "Tem certeza que quer cancelar a consulta de {when}?"
KEPT_TEXT = "Combinado! Sua consulta continua marcada para {when}."
ALSO_SCHEDULED_TEXT = "Você também tem consulta em {whens}."
BOOK_ANOTHER_INTRO = (
    "Combinado! Vamos escolher a nova consulta. A de {when} só será cancelada "
    "quando você confirmar a nova."
)

# Continuations run by workers/shared/actions.py::_handle_action_button after
# this module checked the row. "apptresched" IS that module's existing branch
# (reschedule entry + the Pix reschedule limit); the other two are R3's.
CONTINUE_RESCHEDULE = "apptresched"
CONTINUE_CANCEL_AND_ASK_WHY = "reminder_cancel_and_ask_why"
CONTINUE_BOOK_ANOTHER = "reminder_book_another"

# How many other appointments the "você também tem consulta" line names.
_ALSO_SCHEDULED_LIMIT = 3


async def _retention_warning(session, tenant: Tenant, appointment: Appointment, now) -> str | None:
    """The Pix retention sentence when a cancel now would keep the patient's money.

    Same rule `_handle_action_button`'s `apptcancel` branch applies: a PAID
    deposit and a start inside `tenant.pix_refund_window_hours`.
    """
    deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
    hours_until = _hours_until_start(appointment, now)
    if (
        deposit is not None
        and deposit.status == PixDepositStatus.PAID
        and hours_until is not None
        and hours_until <= tenant.pix_refund_window_hours
    ):
        return _pix_retention_warning_line(tenant, deposit)
    return None


async def _also_scheduled(session, tenant: Tenant, appointment: Appointment, now) -> str:
    """"\\n\\nVocê também tem consulta em …" for the patient's OTHER live appointments, or ""."""
    starts = list(
        await session.scalars(
            select(Appointment.start_at)
            .where(
                Appointment.tenant_id == tenant.id,
                Appointment.patient_id == appointment.patient_id,
                Appointment.id != appointment.id,
                Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
                Appointment.start_at > now,
            )
            .order_by(Appointment.start_at)
            .limit(_ALSO_SCHEDULED_LIMIT)
        )
    )
    if not starts:
        return ""
    whens = [_format_appointment_when(start, tenant.timezone) for start in starts]
    joined = whens[0] if len(whens) == 1 else f"{', '.join(whens[:-1])} e {whens[-1]}"
    return f"\n\n{ALSO_SCHEDULED_TEXT.format(whens=joined)}"


async def handle_reminder_button(
    reply: _ReplyContext, action: str, reminder_id: str, redis=None
) -> tuple[str, str] | None:
    """Answer one reminder-button tap; never raises on a foreign or stale id.

    Returns a continuation `(action, appointment_id)` for
    `_handle_action_button` when the next step is one of its
    appointment-scoped branches, else None (the tap was fully answered here).
    """
    if reply.conversation_id is None or action not in REMINDER_ROW_ACTIONS:
        return None
    try:
        row_id = UUID(reminder_id)
    except ValueError:
        return None
    now = datetime.now(UTC)
    hand_to_ai_text: str | None = None
    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        tenant = await session.get(Tenant, conversation.tenant_id) if conversation else None
        if tenant is None:
            return None
        waba_token = await get_waba_token(session, tenant.id)
        client = _reply_sender(reply, tenant, waba_token)
        if client is None:
            logger.error("reminder_button_no_sender", tenant_id=str(tenant.id))
            return None

        reminder = await session.get(AppointmentReminder, row_id)
        appointment = (
            await session.get(Appointment, reminder.appointment_id) if reminder is not None else None
        )
        if (
            reminder is None
            or appointment is None
            or reminder.tenant_id != tenant.id
            or appointment.tenant_id != tenant.id
            or reminder.patient_id is None
            or reminder.patient_id != conversation.patient_id
        ):
            logger.info("reminder_button_refused", tenant_id=str(tenant.id), action=action)
            await client.send_text_message(to=reply.patient_ref, body=NOT_FOUND_TEXT)
            return None

        if (
            not is_live_status(appointment.status)
            or appointment.start_at is None
            or _as_utc(appointment.start_at) <= now
        ):
            await client.send_text_message(to=reply.patient_ref, body=NOT_ACTIVE_TEXT)
            return None
        when = _format_appointment_when(appointment.start_at, tenant.timezone)
        if _as_utc(reminder.appointment_start_at) != _as_utc(appointment.start_at):
            await client.send_text_message(to=reply.patient_ref, body=MOVED_TEXT.format(when=when))
            return None
        from_chat = reminder.kind == REMINDER_KIND_CHAT

        if action == ACTION_CONFIRM:
            try:
                await reminder_schedule.register_confirmation(
                    session,
                    appointment=appointment,
                    reminder_id=reminder.id,
                    source=(
                        reminder_schedule.CONFIRMATION_SOURCE_CHAT_PROMPT
                        if from_chat
                        else reminder_schedule.CONFIRMATION_SOURCE_REMINDER_BUTTON
                    ),
                    now=now,
                )
            except reminder_schedule.ReminderMismatchError:
                # Checked above; kept so a race can never surface as an error.
                await client.send_text_message(to=reply.patient_ref, body=NOT_FOUND_TEXT)
                return None
            also = await _also_scheduled(session, tenant, appointment, now) if from_chat else ""
            await session.commit()
            await client.send_text_message(
                to=reply.patient_ref, body=CONFIRMED_TEXT.format(when=when) + also
            )
            return None

        if action in (ACTION_CANCEL, ACTION_OTHER):
            if reminder.answer != REMINDER_ANSWER_CONFIRM:
                # A confirmation already counted stays recorded: overwriting it
                # would let the same message count twice (R1 dedupes on it).
                reminder.answer = (
                    REMINDER_ANSWER_CANCEL if action == ACTION_CANCEL else REMINDER_ANSWER_OTHER
                )
                reminder.answered_at = now
            also = (
                await _also_scheduled(session, tenant, appointment, now)
                if from_chat and action == ACTION_OTHER
                else ""
            )
            await session.commit()
            if action == ACTION_CANCEL:
                await client.send_buttons(
                    reply.patient_ref, CANCEL_PATH_TEXT, cancel_path_buttons(reminder.id)
                )
                return None
            hand_to_ai_text = OTHER_TEXT + also

        elif action == ACTION_KEEP:
            await client.send_text_message(to=reply.patient_ref, body=KEPT_TEXT.format(when=when))
            return None

        elif action == ACTION_GIVE_UP:
            body = GIVE_UP_CONFIRM_TEXT.format(when=when)
            warning = await _retention_warning(session, tenant, appointment, now)
            if warning:
                body = f"{warning} {body}"
            await client.send_buttons(reply.patient_ref, body, give_up_confirm_buttons(reminder.id))
            return None

        elif action == ACTION_RESCHEDULE_THIS:
            return CONTINUE_RESCHEDULE, str(appointment.id)
        elif action == ACTION_GIVE_UP_CONFIRM:
            return CONTINUE_CANCEL_AND_ASK_WHY, str(appointment.id)
        elif action == ACTION_BOOK_ANOTHER:
            return CONTINUE_BOOK_ANOTHER, str(appointment.id)

    if hand_to_ai_text is not None:
        # "Outro": full LLM mode through the one persistence seam (a result that
        # names nothing else also drops any half-made booking), then the
        # invitation. The patient's next message reaches the agent with the
        # appointment context (orchestrator: flow_state == LLM).
        await _apply_flow_result(
            reply,
            FlowRouterResult(action="delegate_llm", flow_state=FlowState.LLM),
            reply.patient_ref,
            redis=redis,
            tenant=tenant,
            waba_token=waba_token,
        )
        await client.send_text_message(to=reply.patient_ref, body=hand_to_ai_text)
    return None
```

- [ ] **Step 5: Dispatch with continuations**

In `src/secretaria/workers/shared/actions.py`:

(a) Replace R2's import line `from secretaria.services.reminder_text import REMINDER_ACTIONS` with `from secretaria.services.reminder_text import REMINDER_ROW_ACTIONS`.

(b) Replace R2's dispatch block

```python
    # TASK-032 R2: the reminder buttons carry a REMINDER row id, not an
    # appointment id; their handler checks the row against the patient.
    if action in REMINDER_ACTIONS:
        await handle_reminder_button(reply, action, appointment_id, redis=redis)
        return
```

with

```python
    # TASK-032 R2/R3: the reminder buttons carry a REMINDER row id, not an
    # appointment id; their handler checks the row against the patient. Some
    # steps of the "Cancelar" path continue in a branch below, with the
    # (already checked) appointment id: the reschedule entry with its Pix limit,
    # "Não vou mais" confirmed, "Marcar outra".
    if action in REMINDER_ROW_ACTIONS:
        continuation = await handle_reminder_button(reply, action, appointment_id, redis=redis)
        if continuation is None:
            return
        action, appointment_id = continuation
        appt_uuid = UUID(appointment_id)
```

- [ ] **Step 6: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_r3_cancel_path.py tests/test_reminder_v2_buttons.py tests/test_action_buttons.py tests/test_workers_layering.py -q`
Expected: all pass.

- [ ] **Step 7: Lint, bidi scan, diff check, commit**

```bash
uvx ruff format src/secretaria/workers/shared/reminder_actions.py tests/test_reminder_r3_cancel_path.py
uvx ruff check --fix src/secretaria/workers/shared/reminder_actions.py tests/test_reminder_r3_cancel_path.py
uvx ruff check src/secretaria/workers/shared/actions.py tests/test_reminder_v2_buttons.py
uv run python -c "import sys;[print(f,hex(ord(c))) for f in sys.argv[1:] for c in open(f,encoding='utf-8').read() if 0x200e<=ord(c)<=0x202e or 0x2066<=ord(c)<=0x2069]" src/secretaria/workers/shared/reminder_actions.py tests/test_reminder_r3_cancel_path.py
git diff --stat
git add src/secretaria/workers/shared/reminder_actions.py src/secretaria/workers/shared/actions.py tests/test_reminder_v2_buttons.py tests/test_reminder_r3_cancel_path.py
git commit -F - <<'EOF'
feat(reminders): Cancelar opens Remarcar esta / Marcar outra / Nao vou mais (TASK-032 R3)

Outro hands the conversation to the AI; a chat-card answer names the other
appointments; every step is checked against the patient's reminder row.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 8: The continuations — "Não vou mais" confirmed and "Marcar outra"

**Files:**
- Modify: `src/secretaria/workers/shared/actions.py` (`_handle_action_button`: imports, two new branches, one new hand-off)
- Test: `tests/test_reminder_r3_continuations.py`

**Interfaces:**
- Consumes: Task 7 `CONTINUE_CANCEL_AND_ASK_WHY`, `CONTINUE_BOOK_ANOTHER`, `BOOK_ANOTHER_INTRO`, `_retention_warning`; existing `_execute_appointment_cancel`, `enter_decline_reasons`, `enter_booking`, `list_active_professionals`, `flows_enabled`; R2 `reminder_hooks.after_appointment_closed`; Task 2 `FlowRouterResult.flow_replaces_appointment_id`.
- Produces: behavior only — "Sim, cancelar" cancels (Pix rules), closes the reminder rows, says so and asks why; "Marcar outra" opens a booking that carries the marker, with an intro (and the Pix line inside the refund window).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reminder_r3_continuations.py`:

```python
""""Não vou mais" confirmed and "Marcar outra" (TASK-032 R3, spec §4.3)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from secretaria.models import AppointmentStatus, FlowState, PixDeposit, Tenant
from secretaria.services.attendee import ATTENDEE_QUESTION_BODY
from secretaria.services.flow_router import DECLINE_REASON_QUESTION, STEP_DECLINE_REASON
from secretaria.workers import tasks
from secretaria.workers.shared import reminder_actions as ra
from secretaria.workers.shared.deposit import _pix_retention_warning_line
from secretaria.workers.shared.greeting import _format_appointment_when
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import get_conversation, sent, wire
from tests._reminders_v2 import (
    WA_ID,
    add_reminder,
    get_reminder,
    reload_appointment,
    seed_paid_deposit,
    seed_world,
)


@pytest.fixture
def calendar(monkeypatch, db):  # noqa: F811
    return wire(monkeypatch, db)


def _future(hours: int = 48) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


async def _tap(world, action, reminder_id) -> None:
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
    )
    await tasks._handle_action_button(reply, action, str(reminder_id))


def _when(world) -> str:
    return _format_appointment_when(world.start_at, "America/Sao_Paulo")


async def test_yes_cancel_cancels_closes_the_reminders_and_asks_why(db, calendar):  # noqa: F811
    world = await seed_world(db, start_at=_future(), google_event_id="evt-old")
    rid = await add_reminder(db, world, kind="day", due_at=_future(10))

    await _tap(world, "remgiveupyes", rid)

    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.CANCELLED
    assert calendar.cancelled == ["evt-old"]
    assert (await get_reminder(db, rid)).status == "cancelled"
    kinds = [item[0] for item in sent()]
    assert kinds == ["text", "list"]
    assert sent()[0][2] == "Consulta cancelada."
    assert sent()[1][2] == DECLINE_REASON_QUESTION
    conversation = await get_conversation(db, world)
    assert conversation.flow_step == STEP_DECLINE_REASON
    assert conversation.flow_managing_appointment_id == world.appointment.id


async def test_book_another_opens_a_booking_that_replaces_this_one(db, calendar):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world, "remnew", rid)

    intro = ra.BOOK_ANOTHER_INTRO.format(when=_when(world))
    assert [item[0] for item in sent()] == ["text", "buttons"]
    assert sent()[0][2] == intro
    assert sent()[1][2] == ATTENDEE_QUESTION_BODY
    conversation = await get_conversation(db, world)
    assert conversation.flow_state == FlowState.SERVICE_CATALOG
    assert conversation.flow_replaces_appointment_id == world.appointment.id
    # Nothing is cancelled before the new booking is confirmed.
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED


async def test_book_another_inside_the_refund_window_warns_about_the_deposit(
    db, calendar  # noqa: F811
):
    world = await seed_world(db, start_at=_future(hours=5))
    await seed_paid_deposit(db, world)
    rid = await add_reminder(db, world, kind="hour")

    await _tap(world, "remnew", rid)

    async with db() as session:
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == world.appointment.id)
        )
        tenant = await session.get(Tenant, world.tenant.id)
    intro = ra.BOOK_ANOTHER_INTRO.format(when=_when(world))
    assert sent()[0][2] == f"{intro}\n\n{_pix_retention_warning_line(tenant, deposit)}"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_r3_continuations.py -q`
Expected: FAIL — nothing is sent and nothing changes (the two continuation names reach no branch of `_handle_action_button`).

- [ ] **Step 3: Implement the two branches**

In `src/secretaria/workers/shared/actions.py`:

(a) Imports:
- add `    enter_booking,` to the `from secretaria.services.flow_router import (` block (alphabetical, after `enter_decline_reasons,`);
- add `from secretaria.ai.formatter import TextBubble` above `from secretaria.core.database import async_session_factory`;
- replace R2's `from secretaria.workers.shared.reminder_actions import handle_reminder_button` with

```python
from secretaria.workers.shared.reminder_actions import (
    BOOK_ANOTHER_INTRO,
    CONTINUE_BOOK_ANOTHER,
    CONTINUE_CANCEL_AND_ASK_WHY,
    _retention_warning,
    handle_reminder_button,
)
```

(b) Replace

```python
    decline_handoff: tuple | None = None
```

with

```python
    decline_handoff: tuple | None = None
    # TASK-032 R3 "Marcar outra": the booking entry, applied after the session.
    book_another_handoff: tuple | None = None
```

(c) Directly ABOVE the line `        if action == "rebookno":` insert:

```python
        if action == CONTINUE_CANCEL_AND_ASK_WHY:
            # TASK-032 R3: "Não vou mais" confirmed on a reminder (the row was
            # checked against this patient by handle_reminder_button). Cancel
            # exactly like "apptcancelyes" - Calendar delete, status, Pix
            # outcome - then ask why (FEAT_34 §8 list), like "rebookno".
            tenant_config = await load_tenant_config(session, tenant)
            text = await _execute_appointment_cancel(
                session, tenant, tenant_config, appointment, waba_token
            )
            await session.commit()
            if reminder_hooks.enabled_for(tenant):
                await reminder_hooks.after_appointment_closed(appointment.id, reason="cancelled")
            await client.send_text_message(to=reply.patient_ref, body=text)
            decline_handoff = (tenant, waba_token, appointment.id)

        if action == CONTINUE_BOOK_ANOTHER:
            # TASK-032 R3: "Marcar outra" - a NEW booking that replaces this
            # appointment only once it is confirmed (the flow tail / the Portal
            # promotion cancel it, services/appointment_replacement.py).
            if not flows_enabled(tenant):
                await client.send_text_message(
                    to=reply.patient_ref,
                    body="Para marcar outra consulta, entre em contato com a nossa equipe.",
                )
                return
            professional_rows = await list_active_professionals(session, tenant.id)
            professionals = [
                SimpleNamespace(
                    id=p.id,
                    name=p.name,
                    specialty=p.specialty,
                    about=p.about,
                    context_doctor_message=p.context_doctor_message,
                    appointment_types=p.appointment_types,
                    # Verbatim, NULL and all - see the rebooking branch below.
                    business_hours=p.business_hours,
                )
                for p in professional_rows
            ]
            result = enter_booking(tenant, professionals)
            if result.flow_state == FlowState.SERVICE_CATALOG:
                # Only a booking that really opened carries the marker; a dead
                # end (no services) is answered as it is today.
                result.flow_replaces_appointment_id = appointment.id
                intro = BOOK_ANOTHER_INTRO.format(
                    when=_format_appointment_when(appointment.start_at, tenant.timezone)
                )
                warning = await _retention_warning(session, tenant, appointment, datetime.now(UTC))
                if warning:
                    intro = f"{intro}\n\n{warning}"
                result.bubbles = [TextBubble(body=intro), *result.bubbles]
            book_another_handoff = (tenant, waba_token, result)
```

(d) Directly ABOVE the line `    if decline_handoff is not None:` insert:

```python
    if book_another_handoff is not None:
        ba_tenant, ba_waba_token, ba_result = book_another_handoff
        await _apply_flow_result(
            reply,
            ba_result,
            reply.patient_ref,
            redis=redis,
            tenant=ba_tenant,
            waba_token=ba_waba_token,
        )
        return
```

(`reminder_hooks` is already imported by R2 Task 11; `FlowState`, `SimpleNamespace`, `datetime`, `UTC`, `list_active_professionals`, `flows_enabled`, `load_tenant_config`, `_format_appointment_when` are already imported in this module.)

- [ ] **Step 4: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_r3_continuations.py tests/test_reminder_r3_cancel_path.py tests/test_action_buttons.py tests/test_reminder_v2_wiring.py -q`
Expected: all pass.

- [ ] **Step 5: Lint, diff check, commit**

```bash
uvx ruff format tests/test_reminder_r3_continuations.py
uvx ruff check --fix tests/test_reminder_r3_continuations.py
uvx ruff check src/secretaria/workers/shared/actions.py
git diff --stat
git add src/secretaria/workers/shared/actions.py tests/test_reminder_r3_continuations.py
git commit -F - <<'EOF'
feat(reminders): "Nao vou mais" cancels and asks why; "Marcar outra" opens a replacing booking (TASK-032 R3)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 9: Which appointment the chat opens with — the decision and the `chat` row

**Files:**
- Create: `src/secretaria/services/reminder_opening.py`
- Modify: `src/secretaria/services/reminder_hooks.py` (`reconcile_missing_reminders`: the `current_version` subquery)
- Test: `tests/test_reminder_opening_decision.py`

**Interfaces:**
- Consumes: R1 `AppointmentReminder` + constants, `reminder_schedule.MAX_CONFIRMATIONS`; R2 `reminder_hooks.enabled_for`; `flow_router.reactivation_gap_minutes`; `models.LIVE_APPOINTMENT_STATUSES`; `patient_context.as_utc`.
- Produces: `OPEN`, `SKIP_*` constants, `OpeningDecision`, `nearest_live_appointment`, `decide_reminder_opening(session, *, tenant, patient_id, last_activity_at, now) -> OpeningDecision`, `ensure_chat_reminder(session, appointment, *, now) -> AppointmentReminder`; the reconcile plans an appointment whose only row is a `chat` row.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reminder_opening_decision.py`:

```python
"""When the chat opens with the appointment reminder (TASK-032 R3, spec §4.3)."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from secretaria.core import database as core_database
from secretaria.models import AppointmentReminder, AppointmentStatus, Tenant
from secretaria.services import reminder_hooks
from secretaria.services import reminder_opening as ro
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import add_appointment
from tests._reminders_v2 import NOW, add_reminder, seed_world

QUIET = NOW - timedelta(hours=7)


async def _decide(db, world, *, last_activity_at=QUIET, now=NOW):  # noqa: F811
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        return await ro.decide_reminder_opening(
            session,
            tenant=tenant,
            patient_id=world.patient.id,
            last_activity_at=last_activity_at,
            now=now,
        )


async def _row(db, world, **fields):  # noqa: F811
    """A reminder row of the CURRENT version, with any fields overridden."""
    kind = fields.pop("kind", "chat")
    start = fields.pop("appointment_start_at", None)
    rid = await add_reminder(db, world, kind=kind, appointment_start_at=start)
    if fields:
        async with db() as session:
            await session.execute(
                update(AppointmentReminder).where(AppointmentReminder.id == rid).values(**fields)
            )
            await session.commit()
    return rid


async def test_a_quiet_patient_with_a_live_appointment_opens_with_it(db):  # noqa: F811
    world = await seed_world(db)
    decision = await _decide(db, world)
    assert decision == ro.OpeningDecision(ro.OPEN, world.appointment.id)


async def test_the_switch_off_opens_nothing(db):  # noqa: F811
    world = await seed_world(db, v2=False)
    assert (await _decide(db, world)).reason == ro.SKIP_SWITCH_OFF


async def test_no_history_or_recent_activity_opens_nothing(db):  # noqa: F811
    world = await seed_world(db)
    assert (await _decide(db, world, last_activity_at=None)).reason == ro.SKIP_NO_HISTORY
    recent = NOW - timedelta(hours=1)
    assert (await _decide(db, world, last_activity_at=recent)).reason == ro.SKIP_RECENT_ACTIVITY


async def test_the_nearest_of_two_appointments_opens_the_chat(db):  # noqa: F811
    world = await seed_world(db, start_at=NOW + timedelta(days=5))
    nearest = await add_appointment(db, world, start_at=NOW + timedelta(days=1))
    assert (await _decide(db, world)).appointment_id == nearest.id


@pytest.mark.parametrize(
    "start_offset, status",
    [
        (timedelta(hours=-2), AppointmentStatus.SCHEDULED),
        (timedelta(days=2), AppointmentStatus.CANCELLED),
        (timedelta(days=2), AppointmentStatus.ATTENDED),
    ],
)
async def test_a_past_or_cancelled_appointment_never_opens_the_chat(
    db, start_offset, status  # noqa: F811
):
    world = await seed_world(db, start_at=NOW + start_offset, status=status)
    assert (await _decide(db, world)).reason == ro.SKIP_NO_UPCOMING


async def test_confirmed_twice_never_asks_again(db):  # noqa: F811
    world = await seed_world(db, confirmation_count=2)
    assert (await _decide(db, world)).reason == ro.SKIP_CONFIRMED_TWICE


async def test_a_chat_card_already_confirmed_is_not_shown_again(db):  # noqa: F811
    world = await seed_world(db)
    await _row(db, world, status="sent", sent_at=NOW - timedelta(days=1), answer="confirm")
    assert (await _decide(db, world)).reason == ro.SKIP_CHAT_ALREADY_CONFIRMED


async def test_a_chat_row_shown_inside_the_gap_is_not_shown_again(db):  # noqa: F811
    world = await seed_world(db)
    await _row(db, world, status="sent", sent_at=NOW - timedelta(hours=1))
    assert (await _decide(db, world)).reason == ro.SKIP_DUPLICATE_OPENING


async def test_an_unanswered_cron_card_that_is_the_last_activity_is_not_repeated(db):  # noqa: F811
    world = await seed_world(db)
    await _row(db, world, kind="day", status="sent", sent_at=QUIET, warn_kind="unconfirmed")
    assert (await _decide(db, world)).reason == ro.SKIP_REMINDER_WAITING


async def test_an_answered_or_older_cron_card_does_not_block_the_opening(db):  # noqa: F811
    world = await seed_world(db)
    await _row(
        db, world, kind="day", status="sent", sent_at=QUIET, warn_kind="unconfirmed", answer="other"
    )
    await _row(
        db,
        world,
        kind="hour",
        status="sent",
        sent_at=QUIET - timedelta(days=2),
        warn_kind="unconfirmed",
    )
    assert (await _decide(db, world)).reason == ro.OPEN


async def test_rows_of_an_older_version_never_block(db):  # noqa: F811
    world = await seed_world(db)
    await _row(
        db,
        world,
        appointment_start_at=world.start_at - timedelta(days=1),
        status="sent",
        sent_at=NOW - timedelta(minutes=5),
        answer="confirm",
    )
    assert (await _decide(db, world)).reason == ro.OPEN


async def test_ensure_chat_reminder_reuses_the_one_row_of_this_version(db):  # noqa: F811
    world = await seed_world(db)
    ids = []
    for now in (NOW, NOW + timedelta(hours=8)):
        async with db() as session:
            appointment = await ro.nearest_live_appointment(
                session, world.tenant.id, world.patient.id, now=NOW
            )
            row = await ro.ensure_chat_reminder(session, appointment, now=now)
            ids.append(row.id)
            await session.commit()

    async with db() as session:
        rows = list(await session.scalars(select(AppointmentReminder)))
    assert ids[0] == ids[1] and len(rows) == 1
    [row] = rows
    assert (row.kind, row.status, row.channel, row.with_prompt) == ("chat", "sent", "chat", True)
    assert row.warn_due_at is None
    assert ro.as_utc(row.sent_at) == NOW + timedelta(hours=8)


async def test_the_reconcile_still_plans_an_appointment_that_only_has_a_chat_row(
    db, monkeypatch  # noqa: F811
):
    monkeypatch.setattr(core_database, "async_session_factory", db)
    world = await seed_world(db)
    await _row(db, world, status="sent", sent_at=NOW - timedelta(hours=1))

    created = await reminder_hooks.reconcile_missing_reminders(now=NOW)

    async with db() as session:
        kinds = set(
            await session.scalars(
                select(AppointmentReminder.kind).where(
                    AppointmentReminder.appointment_id == world.appointment.id
                )
            )
        )
    assert created >= 2
    assert {"chat", "day", "hour"} <= kinds
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_opening_decision.py -q`
Expected: collection error `ImportError: cannot import name 'reminder_opening' from 'secretaria.services'`.

- [ ] **Step 3: Implement the decision**

Create `src/secretaria/services/reminder_opening.py`:

```python
"""Whether - and with which appointment - a returning patient's chat opens (TASK-032 R3).

Spec §4.3: a patient with a LIVE future appointment who writes after the same
silence floor the reactivation offer uses (`reactivation_gap_minutes`, default
6 h) sees that appointment's reminder - Confirmar / Cancelar / Outro - as the
first message, instead of the generic greeting/offer. The nearest appointment
is the one shown.

The card is backed by ONE `appointment_reminders` row of kind `chat` per
appointment version (R1's unique key (appointment, kind, start)): showing it
again later re-uses that row, so the same message can never be confirmed twice
(R1 dedupes on the row). The rules below keep the chat and the cron reminders
(R2) from repeating each other:

  * never when the patient already confirmed twice (spec: the requests stop);
  * never when this version's chat card was already confirmed;
  * never twice inside the gap (the chat row's own `sent_at`);
  * never when an UNANSWERED cron reminder that carried the buttons is itself
    the conversation's latest activity - the patient has that very card in
    front of them (R2 records every sent reminder as an outbound message, so
    its `sent_at` and the conversation's last activity coincide).

`decide_reminder_opening` runs inside the inbound transaction
(workers/turn_router.py) and only reads; `ensure_chat_reminder` runs at send
time (workers/shared/reminder_opening.py). The clinic switch is checked first,
without a query: a switched-OFF clinic pays nothing per turn.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import LIVE_APPOINTMENT_STATUSES, Appointment, AppointmentReminder, Tenant
from secretaria.models.appointment_reminder import (
    REMINDER_ANSWER_CONFIRM,
    REMINDER_CHANNEL_CHAT,
    REMINDER_KIND_CHAT,
    REMINDER_STATUS_SENT,
    REMINDER_WARN_UNCONFIRMED,
)
from secretaria.services import reminder_hooks, reminder_schedule
from secretaria.services.flow_router import reactivation_gap_minutes
from secretaria.services.patient_context import as_utc

OPEN = "open"
SKIP_SWITCH_OFF = "switch_off"
SKIP_NO_HISTORY = "no_history"
SKIP_RECENT_ACTIVITY = "recent_activity"
SKIP_NO_UPCOMING = "no_upcoming"
SKIP_CONFIRMED_TWICE = "confirmed_twice"
SKIP_CHAT_ALREADY_CONFIRMED = "chat_already_confirmed"
SKIP_DUPLICATE_OPENING = "duplicate_opening"
SKIP_REMINDER_WAITING = "reminder_waiting"

# A reminder sent this close to the conversation's last activity IS that
# activity (send and record happen a moment apart).
REMINDER_WAITING_TOLERANCE = timedelta(minutes=5)


@dataclass(frozen=True)
class OpeningDecision:
    """`reason` is OPEN (then `appointment_id` is set) or one SKIP_* code (logged)."""

    reason: str
    appointment_id: UUID | None = None


async def nearest_live_appointment(
    session: AsyncSession, tenant_id: UUID, patient_id: UUID, *, now: datetime
) -> Appointment | None:
    """The patient's nearest LIVE appointment in this clinic that has not started."""
    return await session.scalar(
        select(Appointment)
        .where(
            Appointment.tenant_id == tenant_id,
            Appointment.patient_id == patient_id,
            Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
            Appointment.start_at > now,
        )
        .order_by(Appointment.start_at)
        .limit(1)
    )


async def _current_version_rows(
    session: AsyncSession, appointment: Appointment
) -> list[AppointmentReminder]:
    """Every reminder row of the appointment's CURRENT start (compared in UTC:
    SQLite hands datetimes back naive)."""
    rows = await session.scalars(
        select(AppointmentReminder).where(
            AppointmentReminder.tenant_id == appointment.tenant_id,
            AppointmentReminder.appointment_id == appointment.id,
        )
    )
    start = as_utc(appointment.start_at)
    return [row for row in rows if as_utc(row.appointment_start_at) == start]


async def decide_reminder_opening(
    session: AsyncSession,
    *,
    tenant: Tenant,
    patient_id: UUID | None,
    last_activity_at: datetime | None,
    now: datetime,
) -> OpeningDecision:
    """Decide whether this turn opens with the appointment reminder. Read-only."""
    if not reminder_hooks.enabled_for(tenant) or patient_id is None:
        return OpeningDecision(SKIP_SWITCH_OFF)
    if last_activity_at is None:
        return OpeningDecision(SKIP_NO_HISTORY)
    gap = timedelta(minutes=reactivation_gap_minutes(tenant))
    last_activity = as_utc(last_activity_at)
    if now - last_activity < gap:
        return OpeningDecision(SKIP_RECENT_ACTIVITY)
    appointment = await nearest_live_appointment(session, tenant.id, patient_id, now=now)
    if appointment is None:
        return OpeningDecision(SKIP_NO_UPCOMING)
    if appointment.confirmation_count >= reminder_schedule.MAX_CONFIRMATIONS:
        return OpeningDecision(SKIP_CONFIRMED_TWICE, appointment.id)
    for row in await _current_version_rows(session, appointment):
        if row.kind == REMINDER_KIND_CHAT:
            if row.answer == REMINDER_ANSWER_CONFIRM:
                return OpeningDecision(SKIP_CHAT_ALREADY_CONFIRMED, appointment.id)
            if row.sent_at is not None and now - as_utc(row.sent_at) < gap:
                return OpeningDecision(SKIP_DUPLICATE_OPENING, appointment.id)
        elif (
            row.status == REMINDER_STATUS_SENT
            and row.answer is None
            and row.warn_kind == REMINDER_WARN_UNCONFIRMED
            and row.sent_at is not None
            and as_utc(row.sent_at) >= last_activity - REMINDER_WAITING_TOLERANCE
        ):
            return OpeningDecision(SKIP_REMINDER_WAITING, appointment.id)
    return OpeningDecision(OPEN, appointment.id)


async def ensure_chat_reminder(
    session: AsyncSession, appointment: Appointment, *, now: datetime
) -> AppointmentReminder:
    """The `chat` row of the appointment's current version, created or re-shown.

    Born already sent (spec §4.1: "`chat` nasce já enviado"), on the `chat`
    channel, with the buttons and WITHOUT a clinic warning (`warn_due_at`
    stays NULL - R4 warns only from the cron reminders). A row shown before is
    re-used and only its `sent_at` moves; its answer is kept, so confirming the
    same card twice still counts once. Flushes; the caller commits. A twin turn
    racing on the unique key surfaces as IntegrityError at flush/commit - the
    caller retries once and finds the row.
    """
    row = next(
        (r for r in await _current_version_rows(session, appointment) if r.kind == REMINDER_KIND_CHAT),
        None,
    )
    if row is None:
        row = AppointmentReminder(
            tenant_id=appointment.tenant_id,
            appointment_id=appointment.id,
            patient_id=appointment.patient_id,
            kind=REMINDER_KIND_CHAT,
            appointment_start_at=appointment.start_at,
            due_at=now,
            status=REMINDER_STATUS_SENT,
            channel=REMINDER_CHANNEL_CHAT,
            with_prompt=True,
            sent_at=now,
            warn_due_at=None,
        )
        session.add(row)
    else:
        row.sent_at = now
    await session.flush()
    return row
```

- [ ] **Step 4: The reconcile ignores `chat` rows**

In `src/secretaria/services/reminder_hooks.py`, inside `reconcile_missing_reminders`, replace

```python
                AppointmentReminder.appointment_start_at == Appointment.start_at,
            )
            .exists()
```

with

```python
                AppointmentReminder.appointment_start_at == Appointment.start_at,
                # TASK-032 R3: a `chat` row (the opening card) is not a plan -
                # an appointment that only has one still needs its cron rows.
                AppointmentReminder.kind != REMINDER_KIND_CHAT,
            )
            .exists()
```

and add `from secretaria.models.appointment_reminder import REMINDER_KIND_CHAT` to the imports (after the `from secretaria.models import (...)` block).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_opening_decision.py tests/test_reminder_v2_hooks.py -q`
Expected: all pass.

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff format src/secretaria/services/reminder_opening.py tests/test_reminder_opening_decision.py
uvx ruff check --fix src/secretaria/services/reminder_opening.py tests/test_reminder_opening_decision.py
uvx ruff check src/secretaria/services/reminder_hooks.py
git diff --stat
git add src/secretaria/services/reminder_opening.py src/secretaria/services/reminder_hooks.py tests/test_reminder_opening_decision.py
git commit -F - <<'EOF'
feat(reminders): decide the chat opening and keep one chat row per version (TASK-032 R3)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 10: The opening in the turn — choose it, send it, answer the message, suppress the menu

**Files:**
- Modify: `src/secretaria/workers/shared/context.py` (`_ReplyContext`: one field)
- Create: `src/secretaria/workers/shared/reminder_opening.py`
- Modify: `src/secretaria/workers/turn_router.py` (imports; the opening between the silence floors and the reactivation offer)
- Modify: `src/secretaria/workers/orchestrator.py` (imports; send the card; suppress the agent's `show_main_menu`)
- Modify: `src/secretaria/workers/shared/flow_runner.py` (`_run_flow`: suppress the generic menu)
- Test: `tests/test_reminder_opening_turn.py`

**Interfaces:**
- Consumes: Task 9 `decide_reminder_opening`, `ensure_chat_reminder`, `SKIP_SWITCH_OFF`; Task 2 `is_generic_menu_result`; R2 `load_reminder_content`, `build_reminder_body`, `reminder_buttons`; `dispatch._send_buttons_reply`.
- Produces: `_ReplyContext.reminder_opening_appointment_id: UUID | None = None`; `_send_reminder_opening(reply, *, tenant, waba_token) -> bool`; log events `reminder_opening_chosen`, `reminder_opening_skipped`, `reminder_opening_sent`, `reminder_opening_dropped`, `reminder_opening_menu_suppressed`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reminder_opening_turn.py`:

```python
"""The reminder as the chat's first message, end to end (TASK-032 R3, spec §4.3)."""

from datetime import UTC, datetime, timedelta

import pytest

from secretaria.models import AppointmentStatus, FlowState, Patient
from secretaria.services.attendee import ATTENDEE_QUESTION_BODY
from secretaria.services.flow_router import LABEL_BOOK, menu_label
from secretaria.services.reminder_text import (
    ReminderContent,
    build_reminder_body,
    reminder_buttons,
)
from secretaria.workers import tasks
from secretaria.workers.shared import reminder_actions as ra
from secretaria.workers.shared.greeting import _format_appointment_when
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import (
    AGENT_REPLY,
    add_appointment,
    age_conversation,
    chat_rows,
    consent,
    get_conversation,
    sent,
    turn,
    wire,
)
from tests._reminders_v2 import (
    WA_ID,
    add_reminder,
    outbound_messages,
    reload_appointment,
    seed_world,
)


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


async def _quiet_world(db, *, consented=True, **kwargs):  # noqa: F811
    kwargs.setdefault("start_at", datetime.now(UTC) + timedelta(days=2))
    world = await seed_world(
        db, last_inbound_at=datetime.now(UTC) - timedelta(hours=7), **kwargs
    )
    if consented:
        await consent(db, world)
    return world


def _card_body(world, start=None) -> str:
    return build_reminder_body(
        ReminderContent(
            clinic_name="Clínica Olhar",
            timezone="America/Sao_Paulo",
            start_at=start or world.start_at,
            service_name="Consulta",
        )
    )


async def _tap(world, action, reminder_id) -> None:
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
    )
    await tasks._handle_action_button(reply, action, str(reminder_id))


async def test_after_six_hours_the_reminder_opens_the_chat_and_the_menu_is_suppressed(db):  # noqa: F811
    world = await _quiet_world(db)

    await turn(db, world, "oi")

    [chat] = await chat_rows(db, world.appointment.id)
    assert sent() == [("buttons", WA_ID, _card_body(world), reminder_buttons(chat.id))]
    assert (await get_conversation(db, world)).flow_state == FlowState.IDLE
    [card] = [m for m in await outbound_messages(db, world.conversation.id) if m.interactive]
    assert [option["id"] for option in card.interactive["options"]] == [
        button_id for button_id, _label in reminder_buttons(chat.id)
    ]


async def test_a_clear_request_is_answered_right_after_the_card(db):  # noqa: F811
    world = await _quiet_world(db)

    await turn(db, world, LABEL_BOOK)

    assert [item[0] for item in sent()] == ["buttons", "buttons"]
    assert sent()[0][2] == _card_body(world)
    assert sent()[1][2] == ATTENDEE_QUESTION_BODY
    assert (await get_conversation(db, world)).flow_state == FlowState.SERVICE_CATALOG


async def test_consent_not_yet_given_gets_only_the_consent_prompt(db):  # noqa: F811
    world = await _quiet_world(db, consented=False)

    await turn(db, world, "oi")

    assert await chat_rows(db, world.appointment.id) == []
    assert not any(str(item[2]).startswith("LEMBRE-SE") for item in sent())
    async with db() as session:
        assert (await session.get(Patient, world.patient.id)).lgpd_accepted_at is None


async def test_two_appointments_open_with_the_nearest_and_the_answer_names_the_other(db):  # noqa: F811
    world = await _quiet_world(db)
    other = await add_appointment(db, world, start_at=datetime.now(UTC) + timedelta(days=5))

    await turn(db, world, "oi")
    [chat] = await chat_rows(db, world.appointment.id)
    await _tap(world, "remconfirm", chat.id)

    assert sent()[0][2] == _card_body(world)
    when = _format_appointment_when(world.start_at, "America/Sao_Paulo")
    other_when = _format_appointment_when(other.start_at, "America/Sao_Paulo")
    assert sent()[-1][2] == (
        f"{ra.CONFIRMED_TEXT.format(when=when)}\n\n"
        f"{ra.ALSO_SCHEDULED_TEXT.format(whens=other_when)}"
    )


async def test_confirmed_twice_gets_the_normal_menu(db):  # noqa: F811
    world = await _quiet_world(db, confirmation_count=2)

    await turn(db, world, "oi")

    assert await chat_rows(db, world.appointment.id) == []
    assert [item[2] for item in sent()] == [menu_label(world.tenant)]


async def test_a_cancelled_appointment_gets_the_normal_menu(db):  # noqa: F811
    world = await _quiet_world(db, status=AppointmentStatus.CANCELLED)

    await turn(db, world, "oi")

    assert await chat_rows(db, world.appointment.id) == []
    assert [item[2] for item in sent()] == [menu_label(world.tenant)]


async def test_the_switch_off_keeps_todays_behaviour(db):  # noqa: F811
    world = await _quiet_world(db, v2=False)

    await turn(db, world, "oi")

    assert await chat_rows(db, world.appointment.id) == []
    assert [item[2] for item in sent()] == [menu_label(world.tenant)]


async def test_a_second_message_right_after_the_card_gets_no_second_card(db):  # noqa: F811
    world = await _quiet_world(db)

    await turn(db, world, "oi")
    await turn(db, world, "oi de novo")

    assert len(await chat_rows(db, world.appointment.id)) == 1
    assert [item[2] for item in sent()] == [_card_body(world), menu_label(world.tenant)]


async def test_chat_and_cron_confirmations_share_the_counter(db):  # noqa: F811
    world = await _quiet_world(db)
    day = await add_reminder(db, world, kind="day")
    await _tap(world, "remconfirm", day)

    await turn(db, world, "oi")
    [chat] = await chat_rows(db, world.appointment.id)
    await _tap(world, "remconfirm", chat.id)
    await _tap(world, "remconfirm", chat.id)

    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 2
    await age_conversation(db, world, hours=7)
    await turn(db, world, "oi")
    assert len(await chat_rows(db, world.appointment.id)) == 1
    assert sent()[-1][2] == menu_label(world.tenant)


async def test_the_same_chat_card_reshown_later_counts_once(db):  # noqa: F811
    world = await _quiet_world(db)

    await turn(db, world, "oi")
    await age_conversation(db, world, hours=7)
    await turn(db, world, "oi")
    [chat] = await chat_rows(db, world.appointment.id)
    cards = [item for item in sent() if item[0] == "buttons"]
    await _tap(world, "remconfirm", chat.id)
    await _tap(world, "remconfirm", chat.id)

    assert [card[3] for card in cards] == [reminder_buttons(chat.id)] * 2
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 1


async def test_outro_on_the_card_hands_the_next_message_to_the_ai(db):  # noqa: F811
    world = await _quiet_world(db)

    await turn(db, world, "oi")
    [chat] = await chat_rows(db, world.appointment.id)
    await _tap(world, "remother", chat.id)
    await turn(db, world, "quanto custa a consulta?")

    assert (await get_conversation(db, world)).flow_state == FlowState.LLM
    assert sent()[-1][2] == AGENT_REPLY
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_opening_turn.py -q`
Expected: the opening tests FAIL (no card: `[] == [chat]` unpack errors, the menu is sent instead); `test_consent_not_yet_given...`, `test_confirmed_twice...`, `test_a_cancelled_appointment...`, `test_the_switch_off...` pass already (today's behaviour) and must keep passing.

- [ ] **Step 3: The reply field**

In `src/secretaria/workers/shared/context.py`, at the end of `_ReplyContext` (after `    name_invalid: bool = False`) append:

```python
    # --- TASK-032 R3: the reminder that opens a returning patient's chat -----
    # The appointment the card is about (services/reminder_opening.py). The
    # card goes out first (`workers/shared/reminder_opening.py`), then the
    # patient's message is answered as usual - minus the generic menu, which
    # the card's "Outro" already covers (`_run_flow`, `show_main_menu`).
    reminder_opening_appointment_id: UUID | None = None
```

- [ ] **Step 4: The sender**

Create `src/secretaria/workers/shared/reminder_opening.py`:

```python
"""Send the reminder card that opens a returning patient's chat (TASK-032 R3, spec §4.3).

The decision was taken inside the inbound transaction
(services/reminder_opening.py::decide_reminder_opening); this runs from
`_send_bot_reply_inner`, after the entitlement gate, so an unentitled clinic
creates no row and sends nothing. Everything is re-checked here because time
passed since the decision: the appointment must still be this conversation's
patient's, live, in the future and below two confirmations.

The card is the SAME text and buttons as a cron reminder (R2's builders), on
the `chat` row of this appointment version - created now or re-shown. It goes
out through `_send_buttons_reply`, which picks the channel and records the card
(the Portal's tap check reads that record, workers/portal/inbound.py).
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Conversation, Tenant, is_live_status
from secretaria.services import reminder_schedule
from secretaria.services.reminder_opening import ensure_chat_reminder
from secretaria.services.reminder_text import (
    build_reminder_body,
    load_reminder_content,
    reminder_buttons,
)
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.dispatch import _send_buttons_reply
from secretaria.workers.shared.text import _as_utc

logger = get_logger(__name__)


async def _prepare_opening(
    reply: _ReplyContext, tenant: Tenant, now: datetime
) -> tuple[str, list[tuple[str, str]]] | None:
    async with async_session_factory() as session:
        async with session.begin():
            conversation = await session.get(Conversation, reply.conversation_id)
            appointment = await session.scalar(
                select(Appointment).where(
                    Appointment.id == reply.reminder_opening_appointment_id,
                    Appointment.tenant_id == tenant.id,
                )
            )
            if (
                conversation is None
                or appointment is None
                or appointment.patient_id is None
                or appointment.patient_id != conversation.patient_id
                or not is_live_status(appointment.status)
                or appointment.start_at is None
                or _as_utc(appointment.start_at) <= now
                or appointment.confirmation_count >= reminder_schedule.MAX_CONFIRMATIONS
            ):
                return None
            row = await ensure_chat_reminder(session, appointment, now=now)
            content = await load_reminder_content(session, tenant, appointment)
            return build_reminder_body(content), reminder_buttons(row.id)


async def _send_reminder_opening(
    reply: _ReplyContext, *, tenant: Tenant, waba_token: str | None
) -> bool:
    """Send the opening card. Returns False (logged) when it no longer applies."""
    if reply.reminder_opening_appointment_id is None or reply.conversation_id is None:
        return False
    now = datetime.now(UTC)
    try:
        prepared = await _prepare_opening(reply, tenant, now)
    except IntegrityError:
        # A twin turn created this version's chat row a moment ago: re-use it.
        prepared = await _prepare_opening(reply, tenant, now)
    if prepared is None:
        logger.info(
            "reminder_opening_dropped",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        return False
    body, buttons = prepared
    await _send_buttons_reply(
        reply,
        tenant=tenant,
        waba_token=waba_token,
        body=body,
        buttons=buttons,
        event="reminder_opening_sent",
    )
    return True
```

- [ ] **Step 5: Choose the opening in the turn**

In `src/secretaria/workers/turn_router.py`:

(a) Imports — after the `from secretaria.services.pending_identity import (...)` block add:

```python
from secretaria.services.reminder_opening import (
    OPEN,
    SKIP_SWITCH_OFF,
    decide_reminder_opening,
)
```

(b) Directly ABOVE the comment line `    # Returning after a silence gap (and not already greeting on` insert:

```python
    # --- TASK-032 R3: the appointment reminder as the chat's first message ----
    # After the consent gate, /menu, the pending "quer continuar?" answer and
    # the two silence floors above (so a stale LLM/attendee state is already
    # dropped), and INSTEAD of the returning-patient offer below: a patient with
    # a live future appointment who writes after the reactivation gap sees that
    # appointment's reminder card first (spec §4.3). The patient's message is
    # still answered right after it (`_send_bot_reply_inner`). Returns without a
    # query when the clinic's switch is off.
    if greeting_override is None:
        opening = await decide_reminder_opening(
            session,
            tenant=tenant,
            patient_id=patient.id,
            last_activity_at=last_activity_at,
            now=datetime.now(UTC),
        )
        if opening.reason == OPEN and opening.appointment_id is not None:
            logger.info(
                "reminder_opening_chosen",
                conversation_id=str(conversation.id),
                tenant_id=str(tenant.id),
                appointment_id=str(opening.appointment_id),
            )
            return _ReplyContext(
                channel=channel,
                conversation_id=conversation.id,
                tenant_id=tenant.id,
                patient_ref=patient_ref,
                inbound_body=body or "",
                reminder_opening_appointment_id=opening.appointment_id,
            )
        if opening.reason != SKIP_SWITCH_OFF:
            logger.info(
                "reminder_opening_skipped",
                reason=opening.reason,
                conversation_id=str(conversation.id),
                tenant_id=str(tenant.id),
            )
```

(`OpeningDecision` carries the appointment id on some skips too, for the log only; the reason is what decides.)

- [ ] **Step 6: Send it and suppress the agent's menu hand-back**

In `src/secretaria/workers/orchestrator.py`:

(a) Imports — after the `from secretaria.workers.shared.handover import (...)` block add:

```python
from secretaria.workers.shared.reminder_opening import (
    _send_reminder_opening,
)
```

(b) Directly ABOVE the comment line `    # Optional-addon inbound interception (e.g. human_backup_24_7's` insert:

```python
    # TASK-032 R3: the appointment reminder opens the chat. Past the entitlement
    # gate on purpose (an unentitled clinic sends nothing and plans no row); the
    # patient's own message is then answered by everything below, minus the
    # generic menu (`_run_flow`, and the `show_main_menu` hand-back further down).
    if reply.reminder_opening_appointment_id is not None and tenant is not None:
        await _send_reminder_opening(reply, tenant=tenant, waba_token=waba_token)
```

(c) Replace

```python
    if reply_text == SHOW_MAIN_MENU_SENTINEL:
        await _handle_show_main_menu(
```

with

```python
    if reply_text == SHOW_MAIN_MENU_SENTINEL:
        if reply.reminder_opening_appointment_id is not None:
            # The reminder card that opened this turn already offers "Outro".
            logger.info(
                "reminder_opening_menu_suppressed",
                source="agent",
                conversation_id=str(reply.conversation_id),
            )
            return
        await _handle_show_main_menu(
```

- [ ] **Step 7: Suppress the generic menu in the flow**

In `src/secretaria/workers/shared/flow_runner.py`:

(a) Add `    is_generic_menu_result,` to the `from secretaria.services.flow_router import (` block (after `    STEP_AWAITING_ATTENDEE_AUTH,` and before `    FlowRouterResult,` - keep the block sorted as ruff's isort does: constants, classes, then functions, i.e. `STEP_AWAITING_ATTENDEE_AUTH, FlowRouterResult, is_generic_menu_result, route`).

(b) In `_run_flow`, replace

```python
    return await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )

async def _apply_flow_result(
```

with

```python
    if reply.reminder_opening_appointment_id is not None and is_generic_menu_result(result):
        # TASK-032 R3: the reminder card that opened this turn replaces the plain
        # menu (its "Outro" covers it). Nothing is persisted: the conversation
        # stays where it was.
        logger.info(
            "reminder_opening_menu_suppressed",
            source="flow",
            conversation_id=str(reply.conversation_id),
        )
        return True
    return await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )

async def _apply_flow_result(
```

(After TASK-030 P2a the `if result.resume_draft and tenant is not None:` block sits right above this `return`; leave it there, above the new block.)

- [ ] **Step 8: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_opening_turn.py tests/test_reminder_opening_decision.py tests/test_reactivation.py tests/test_brain_message_interactive_tap.py tests/test_workers_layering.py tests/test_llm_state_expiry.py -q`
Expected: all pass.

- [ ] **Step 9: Lint, bidi scan, diff check, commit**

```bash
uvx ruff format src/secretaria/workers/shared/reminder_opening.py tests/test_reminder_opening_turn.py
uvx ruff check --fix src/secretaria/workers/shared/reminder_opening.py tests/test_reminder_opening_turn.py
uvx ruff check src/secretaria/workers/shared/context.py src/secretaria/workers/turn_router.py src/secretaria/workers/orchestrator.py src/secretaria/workers/shared/flow_runner.py
uv run python -c "import sys;[print(f,hex(ord(c))) for f in sys.argv[1:] for c in open(f,encoding='utf-8').read() if 0x200e<=ord(c)<=0x202e or 0x2066<=ord(c)<=0x2069]" tests/test_reminder_opening_turn.py src/secretaria/workers/turn_router.py
git diff --stat
git add src/secretaria/workers/shared/context.py src/secretaria/workers/shared/reminder_opening.py src/secretaria/workers/turn_router.py src/secretaria/workers/orchestrator.py src/secretaria/workers/shared/flow_runner.py tests/test_reminder_opening_turn.py
git commit -F - <<'EOF'
feat(reminders): the appointment reminder opens a returning patient's chat (TASK-032 R3)

After 6 h of silence, behind the consent gate and the clinic switch; the
patient's message is still answered, the generic menu is not repeated.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 11: The Portal decodes the reminder buttons

**Files:**
- Modify: `src/secretaria/workers/portal/inbound.py` (imports; `_persist_brain_message_inbound`)
- Test: `tests/test_reminder_r3_portal.py`

**Interfaces:**
- Consumes: R2 `decode_action_id`; Task 6 `REMINDER_ROW_ACTIONS`; the existing last-10-cards check `_validated_brain_message_reply_id`.
- Produces: behavior only — a Portal tap whose id is one of THIS conversation's recent card options AND a reminder-row action reaches `_handle_action_button` (which checks the row against the patient); any other offered id keeps today's title routing.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reminder_r3_portal.py`:

```python
"""Reminder buttons tapped in the Portal (TASK-032 R3, spec §4.3 "Portal")."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
)
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.workers.orchestrator import _send_bot_reply
from secretaria.workers.portal.inbound import _persist_brain_message_inbound
from secretaria.workers.shared import reminder_actions as ra
from secretaria.workers.shared.greeting import _format_appointment_when
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import chat_rows, consent, turn, wire
from tests._reminders_v2 import add_reminder, get_reminder, outbound_messages, reload_appointment, seed_world


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


async def _portal_world(db):  # noqa: F811
    world = await seed_world(
        db,
        channel=CHANNEL_BRAIN_MESSAGE,
        start_at=datetime.now(UTC) + timedelta(days=2),
        last_inbound_at=datetime.now(UTC) - timedelta(hours=7),
    )
    await consent(db, world)
    return world


async def _portal_tap(world, reply_id: str, text: str = "Confirmar"):
    return await _persist_brain_message_inbound(
        tenant_id=world.tenant.id,
        external_id=world.patient.external_id,
        text=text,
        interactive_reply_id=reply_id,
    )


async def test_a_portal_tap_on_the_opening_card_confirms(db):  # noqa: F811
    world = await _portal_world(db)
    await turn(db, world, "oi")
    [chat] = await chat_rows(db, world.appointment.id)

    reply = await _portal_tap(world, f"remconfirm|{chat.id}")
    await _send_bot_reply(reply)

    assert reply.action_button == ("remconfirm", str(chat.id))
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 1
    when = _format_appointment_when(world.start_at, "America/Sao_Paulo")
    bodies = [m.body for m in await outbound_messages(db, world.conversation.id)]
    assert bodies[-1] == ra.CONFIRMED_TEXT.format(when=when)


async def test_a_portal_tap_on_the_cancel_path_reaches_its_step(db):  # noqa: F811
    world = await _portal_world(db)
    await turn(db, world, "oi")
    [chat] = await chat_rows(db, world.appointment.id)
    # Each step's id is on the card the previous step recorded: Cancelar -> the
    # three-way card (remgiveup) -> the confirmation card (remkeep).
    await _send_bot_reply(await _portal_tap(world, f"remcancel|{chat.id}", "Cancelar"))
    await _send_bot_reply(await _portal_tap(world, f"remgiveup|{chat.id}", "Não vou mais"))

    reply = await _portal_tap(world, f"remkeep|{chat.id}", "Manter consulta")
    await _send_bot_reply(reply)

    assert reply.action_button == ("remkeep", str(chat.id))
    when = _format_appointment_when(world.start_at, "America/Sao_Paulo")
    bodies = [m.body for m in await outbound_messages(db, world.conversation.id)]
    assert bodies[-1] == ra.KEPT_TEXT.format(when=when)


async def test_a_portal_tap_with_another_patients_reminder_id_counts_nothing(db):  # noqa: F811
    world = await _portal_world(db)
    async with db() as session:
        intruder = Patient(
            id=uuid4(),
            tenant_id=world.tenant.id,
            channel=CHANNEL_BRAIN_MESSAGE,
            external_id=str(uuid4()),
        )
        session.add(intruder)
        await session.flush()
        session.add(Conversation(id=uuid4(), tenant_id=world.tenant.id, patient_id=intruder.id))
        start = datetime.now(UTC) + timedelta(days=3)
        theirs = Appointment(
            id=uuid4(),
            tenant_id=world.tenant.id,
            patient_id=intruder.id,
            google_event_id="evt-theirs",
            appointment_type="Consulta",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(theirs)
        await session.commit()
    their_world = type(world)(world.tenant, intruder, world.conversation, theirs, start)
    their_row = await add_reminder(db, their_world)

    reply = await _portal_tap(world, f"remconfirm|{their_row}")

    assert reply.action_button is None
    assert (await reload_appointment(db, theirs.id)).confirmation_count == 0
    assert (await get_reminder(db, their_row)).answer is None


async def test_older_action_ids_keep_todays_title_routing_on_the_portal(db):  # noqa: F811
    world = await _portal_world(db)
    old_id = f"rebooksame|{world.appointment.id}"
    async with db() as session:
        session.add(
            Message(
                conversation_id=world.conversation.id,
                direction=MessageDirection.OUTBOUND,
                sender=MessageSender.BOT,
                body="Quer remarcar?",
                interactive={
                    "kind": "buttons",
                    "body": "Quer remarcar?",
                    "options": [{"id": old_id, "title": "Remarcar"}],
                },
            )
        )
        await session.commit()

    reply = await _portal_tap(world, old_id, "Remarcar")

    assert reply.action_button is None
```

(`World` from `tests/_reminders_v2.py` is a dataclass `(tenant, patient, conversation, appointment, start_at)`; `type(world)(...)` builds one for the intruder so `add_reminder` writes a row for THEIR appointment and patient. `MessageSender.BOT` is the bot member of `models/message.py::MessageSender`.)

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_r3_portal.py -q`
Expected: the two tap tests FAIL (`reply.action_button is None`; the tap is routed by its title and nothing is confirmed). The intruder and old-id tests pass already and must keep passing.

- [ ] **Step 3: Decode after the existing check**

In `src/secretaria/workers/portal/inbound.py`:

(a) Imports — add

```python
from secretaria.schemas.webhook import decode_action_id
```

above `from secretaria.services.channel_sender import (`, and

```python
from secretaria.services.reminder_text import REMINDER_ROW_ACTIONS
```

below that block.

(b) Replace

```python
                if interactive_reply_id is not None:
                    interactive_reply_id = await _validated_brain_message_reply_id(
                        session, tenant=tenant, patient=patient, reply_id=interactive_reply_id
                    )
```

with

```python
                if interactive_reply_id is not None:
                    interactive_reply_id = await _validated_brain_message_reply_id(
                        session, tenant=tenant, patient=patient, reply_id=interactive_reply_id
                    )
                # TASK-032 R3: a reminder button tapped in the Portal is an ACTION,
                # as on WhatsApp - decoded only AFTER the line above proved the id
                # was on one of THIS conversation's recent cards, and only for the
                # reminder-row family, whose handler checks the row against the
                # conversation's patient again (workers/shared/reminder_actions.py).
                # Older action ids keep today's title routing on this channel.
                action_button = None
                decoded = decode_action_id(interactive_reply_id)
                if decoded is not None and decoded[0] in REMINDER_ROW_ACTIONS:
                    action_button = decoded
```

(c) In the `return await _route_inbound_turn(` call of the same function, add after `                    interactive_reply_id=interactive_reply_id,`:

```python
                    action_button=action_button,
```

- [ ] **Step 4: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_r3_portal.py tests/test_brain_message_interactive_tap.py tests/test_workers_layering.py -q`
Expected: all pass.

- [ ] **Step 5: Lint, diff check, commit**

```bash
uvx ruff format tests/test_reminder_r3_portal.py
uvx ruff check --fix tests/test_reminder_r3_portal.py
uvx ruff check src/secretaria/workers/portal/inbound.py
git diff --stat
git add src/secretaria/workers/portal/inbound.py tests/test_reminder_r3_portal.py
git commit -F - <<'EOF'
feat(portal): reminder buttons tapped in the Portal act like on WhatsApp (TASK-032 R3)

Decoded only after the recent-card check, only for the reminder-row family.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 12: Checkpoint documentation and focused validation

Only after Tasks 1–11 are green (`AI_WORKFLOW.md`: docs are updated after validation, not before).

**Files:**
- Create: `docs/CHECKPOINT_lembretes_r3.md`
- Modify: `docs/CHECKPOINT_lembretes_r2.md` (one pointer line), `docs/CHECKPOINT_context_aware_opening.md` (one pointer line)

**Interfaces:**
- Consumes: everything above.
- Produces: the written state R4/R5 read first.

- [ ] **Step 1: Focused validation**

Run:

```bash
BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_flow_replaces_column.py tests/test_appointment_replacement_router.py tests/test_appointment_replacement_marker.py tests/test_appointment_replacement_service.py tests/test_appointment_replacement_wiring.py tests/test_reminder_r3_ids.py tests/test_reminder_r3_cancel_path.py tests/test_reminder_r3_continuations.py tests/test_reminder_opening_decision.py tests/test_reminder_opening_turn.py tests/test_reminder_r3_portal.py -q
BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_buttons.py tests/test_reminder_v2_text.py tests/test_reminder_v2_hooks.py tests/test_reminder_v2_wiring.py tests/test_action_buttons.py tests/test_flow_router.py tests/test_reactivation.py tests/test_llm_state_expiry.py tests/test_attendee_booking.py tests/test_booking_code_gate.py tests/test_brain_message_interactive_tap.py tests/test_flow_cancel_money.py tests/test_flow_draft_column.py tests/test_workers_layering.py -q
```

Expected: both runs green. A failure in the second run that also fails on the commit before Task 1 (`git stash` is NOT allowed in a shared worktree — check out that commit in a scratch worktree instead) is pre-existing: record it with evidence, do not fix it here. Anything else is a regression of this plan: fix it before continuing.

- [ ] **Step 2: Write the checkpoint**

Create `docs/CHECKPOINT_lembretes_r3.md` with these sections (Portuguese prose, code names as anchors, no line numbers):

1. **Estado** — local, commitado na branch da TASK-032 (SHAs dos commits das Tasks 1–11), NÃO pushado, NÃO deployado; migração `a7e2c9d4f1b6` (down = a head encontrada na Task 1); resultado do Postgres descartável (ou "Postgres run NOT done").
2. **O que entrou onde** — abertura (`services/reminder_opening.py::decide_reminder_opening`, `workers/turn_router.py` entre os pisos de silêncio e a oferta de reativação, `workers/shared/reminder_opening.py::_send_reminder_opening`, supressão em `_run_flow` e no `show_main_menu`); caminho do Cancelar (`workers/shared/reminder_actions.py`, continuações em `workers/shared/actions.py`); "Marcar outra" (`conversations.flow_replaces_appointment_id`, `flow_router._carry_replacement`, `services/appointment_replacement.py`, `workers/shared/replacement.py`, consumo em `_apply_flow_result` e `_promote_booking_hold`); Portal (`workers/portal/inbound.py`); ids `rem*` e rótulos (`services/reminder_text.py`).
3. **Regras fixadas** — a lista "Review Focus" deste plano, uma linha cada, com o teste que a prende.
4. **Decisões** — (a) uma linha `chat` por versão da consulta, reexibida, nunca duplicada; (b) a abertura não sai quando um lembrete do cron com botões, sem resposta, é a última atividade; (c) a linha "Você também tem consulta em …" só nas respostas ao cartão do chat (Confirmar/Outro); (d) rótulos curtos (≤ 20) e a redação longa da spec no corpo do cartão; (e) o Portal só decodifica a família `rem*`; (f) reserva feita sozinha pela IA (`_persist_appointment`) nunca cancela a original, só apaga o marcador; (g) "Outro" coloca a conversa em modo IA pelo `_apply_flow_result`.
5. **Pendências** — "abre o chat" no Portal para conversa já existente não dispara abertura (a rota `open` responde `exists` antes de qualquer job; coberto pela mensagem que o R2 grava na conversa quando um lembrete sai e pela primeira mensagem escrita); a abertura substitui a oferta "quer continuar?" de quem estava no meio de uma marcação (a mensagem segue o estado guardado); evento do Google da original não é apagado quando o profissional dono foi desativado (logado como `appointment_replacement_calendar_missing`); TASK-030 P5 (prompt/filtro da IA ainda proíbem falar de lembretes); R4 (avisos e liberar horário) e R5 (front).

- [ ] **Step 3: Pointers**

Append one line to `docs/CHECKPOINT_lembretes_r2.md` (end of its "Pendências"/R3 line): `- R3 executado: ver docs/CHECKPOINT_lembretes_r3.md (abertura do chat, caminho do Cancelar, botões no Portal).`
Append one line to `docs/CHECKPOINT_context_aware_opening.md` (end of file): `- TASK-032 R3: com o interruptor de lembretes ligado, a consulta futura abre a conversa depois de 6 h de silêncio (docs/CHECKPOINT_lembretes_r3.md); a saudação HAS_UPCOMING de primeiro contato continua como estava.`

- [ ] **Step 4: Commit**

```bash
git diff --stat
git add docs/CHECKPOINT_lembretes_r3.md docs/CHECKPOINT_lembretes_r2.md docs/CHECKPOINT_context_aware_opening.md
git commit -F - <<'EOF'
docs(reminders): checkpoint for R3 - chat opening and the Cancelar path (TASK-032)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Self-review (done while writing; kept for the reviewer)

**Spec coverage (§4.3):** opening after ≥ 6 h for a live future appointment → Tasks 9–10; after the consent gate → Task 10 (placement + test); replaces the generic menu/offer → Task 10 (turn_router placement, `_run_flow`, `show_main_menu`); creates a `chat` row → Task 9 `ensure_chat_reminder`; `confirmation_count >= 2` → no request → Tasks 9–10; the message is still processed → Task 10 `test_a_clear_request_is_answered_right_after_the_card`; Confirmar → R2 + Task 7 (chat source); Cancelar → three-way card → Task 7; Remarcar (pre-selected, Pix limits) → Task 7 continuation into the existing `apptresched` branch; Marcar outra (`flow_replaces_appointment_id`, cancel only on confirmation, same transaction, original stays if abandoned, Pix rules) → Tasks 1–5 and 8; Não vou mais → confirm → cancel (Pix) → "por quê?" → Tasks 7–8; Outro → AI → Task 7; several appointments → Tasks 9–10; Portal decoding validated against the patient → Task 11 (+ R2's handler check). §7 "Dois lembretes sem deduplicação" → Task 9 rules + Task 10 counter tests. §5.10 (switch) → Tasks 9–10 tests.

**Gaps (reported, not built):** "abre o chat" for an EXISTING Portal conversation (the `open` route answers `exists` before any job — changing that contract with brain-api is outside §4.3's file list; R2's recorded reminder message covers the case the spec describes in §4.2 "Portal"); the opening replaces the "quer continuar?" offer for a patient mid-booking (the message then continues the stored step).

**Type/name consistency:** `flow_replaces_appointment_id` (column, result field, snapshot) everywhere; `cancel_replaced_appointment` / `ReplacedAppointment` / `_finish_replacement` / `_replaced_notice` match between Tasks 4, 5; `REMINDER_ROW_ACTIONS` used by Tasks 6, 7, 11; continuations `CONTINUE_*` match between Tasks 7 and 8; `reminder_opening_appointment_id` matches between Tasks 10's context, turn_router, orchestrator, flow_runner and sender; `decide_reminder_opening` signature identical in Task 9 code, tests and Task 10 call.

---

## Deploy e liberação

Nada neste plano faz deploy, push ou mexe em produção; cada passo abaixo exige pedido explícito do dono, na ocasião.

1. **Ordem:** banco primeiro — `alembic upgrade head` com a imagem nova (one-off), os dois serviços ainda no código antigo (a coluna é inerte para eles). Depois `secretaria_api` **e** `secretaria-worker` juntos (`GET /build` com paridade `match`): a abertura e os botões rodam no worker; a decodificação do Portal também (job `process_brain_message_inbound`). Nenhum front muda neste plano.
2. **Pré-requisitos de produção:** R1 e R2 no ar antes (a tabela `appointment_reminders`, o interruptor e os ids `rem*`); TASK-030 P2a/P3 no ar antes (a migração `e7d3c1a9b5f2` vem antes desta na cadeia).
3. **Liberação:** o interruptor `reminders_v2_enabled` continua desligado em todas as clínicas; ligar primeiro só na clínica de teste e conferir no navegador e no WhatsApp: paciente com consulta futura escreve depois de 6 h → recebe o lembrete com Confirmar/Cancelar/Outro; "Cancelar" → as três opções; "Marcar outra" → confirmar a nova cancela a antiga e o evento some da agenda do Google; "Não vou mais" → cartão de confirmação → cancelamento → "por quê?". Teste, build e prova ao vivo são coisas diferentes: registrar no `TASK.md` o que foi provado em cada ambiente.
4. **Reversão:** desligar o interruptor da clínica basta para parar tudo o que este plano mostra ao paciente. Reverter o código: os dois serviços voltam juntos para a versão anterior; só depois, se for preciso, `alembic downgrade` para a revisão anterior a `a7e2c9d4f1b6` (perde apenas uma "Marcar outra" em andamento — a original fica, que é o lado seguro).

