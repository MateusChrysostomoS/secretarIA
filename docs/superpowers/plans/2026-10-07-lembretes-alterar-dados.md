# Lembrete com "Alterar Dados" Implementation Plan
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The reminder card becomes **Confirmar / Cancelar / Alterar Dados**; "Cancelar" cancels directly (confirm → cancel → ask why); "Confirmar" is followed by the returning-patient menu; "Alterar Dados" opens a draft-based flow that changes date, time, service, doctor, convênio or patient and only touches the real appointment when the patient taps Confirmar on one complete confirmation message.

**Architecture:** A new `FlowState.EDIT_BOOKING` and one nullable JSON column `conversations.flow_edit_draft` hold the draft (current vs original values). The pure pieces (labels, texts, draft, recap, `EditContext`) live in `services/appointment_edit.py`; the steps live in `services/appointment_edit_flow.py`, called from `flow_router._route` by one dispatch line, reusing the existing day/slot pickers through a new `EDIT_DAY_BRANCH`. The router performs the Google call at Confirmar (as `_manage_reschedule` does) and returns `appointment_edit`; `workers/shared/appointment_edit_apply.py` updates the same `Appointment` row in `_apply_flow_result`'s transaction and finishes (old event deletion, reminders) after the commit — the same shape as R3's `replacement.py`. The 5-option menu is a `MenuBubble`: the Portal sender shows real buttons (cap raised), WhatsApp turns it into a list.

**Tech Stack:** Python 3.12, SQLAlchemy 2 async (Postgres prod, SQLite tests), Alembic, arq, pytest + pytest-asyncio (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-10-07-lembretes-alterar-dados-design.md` (decisions D1–D9, premissas P1–P7). Worktree: `C:\TECH\BRAIN-worktrees\TASK-032\secretarIA-r6`, branch `task/TASK-032-r6-alterar-dados`, from `origin/main` 64b7595 (R3 already in). Paths are relative to the repo root; package root `src/secretaria/`.

## Global Constraints

- **Switch:** everything here is behind `Tenant.reminders_v2_enabled` (the cards only exist for a switched-ON clinic). With it OFF nothing a patient sees changes.
- **Names consumed exactly as R1–R3 produce them:** `AppointmentReminder`, `REMINDER_ANSWER_*`, `reminder_text.{ACTION_*, LABEL_*, REMINDER_ACTIONS, REMINDER_ROW_ACTIONS, CANCEL_PATH_ACTIONS, reminder_buttons, give_up_confirm_buttons}`, `schemas/webhook.py::{decode_action_id, _ACTION_BUTTON_PREFIXES}`, `workers/shared/reminder_actions.py::{handle_reminder_button, GIVE_UP_CONFIRM_TEXT, KEPT_TEXT, CONFIRMED_TEXT, NOT_FOUND_TEXT, CONTINUE_CANCEL_AND_ASK_WHY}`, `workers/shared/actions.py::_handle_action_button`, `workers/shared/replacement.py` (pattern), test helpers `tests/_reminder_fixtures.py::db`, `tests/_reminders_v2.py::{WA_ID, FakeWhatsAppClient, seed_world, add_reminder, get_reminder, reload_appointment, outbound_messages, seed_paid_deposit}`, `tests/_reminders_r3.py::{wire, turn, consent, sent, get_conversation, set_conversation, chat_rows, age_conversation, FakeCalendar}`.
- **Migration:** one additive revision `b1c4e7a2d9f3` (`conversations.flow_edit_draft`, JSON NULL). Its `down_revision` is the single Alembic head at the start of this plan — **expected `a7e2c9d4f1b6`** (R3). `FlowState.EDIT_BOOKING` needs NO migration: `flow_state` is a non-native enum stored as `VARCHAR(32)` with no CHECK (see the note on `FlowState` in `models/conversation.py`). Never drop or rename a column (skill `frozen-contract-migration`).
- **Button limits:** reply-button labels ≤ 20 characters (`MAX_BUTTON_LABEL_CHARS`), list row titles ≤ 24, list row descriptions ≤ 72, card bodies ≤ 1024 (`core/whatsapp_limits.py`); WhatsApp shows at most 3 reply buttons (`MAX_BUTTONS_PER_MESSAGE`). Every new label below is ≤ 20 characters.
- **Layering:** `api → workers → services/ai → models → core`. `services/*` never imports `workers/*`; `workers/shared/*` never imports `workers.whatsapp`, `workers.portal` or `workers.tasks` (`tests/test_workers_layering.py`).
- **Channels:** every send goes through `_reply_sender` / `_send_buttons_reply` / `_apply_flow_result` / `_send_bubble` (skill `channel-aware-dispatch`); nothing is WhatsApp-only.
- **Text:** patient-facing text in Portuguese; code, comments and log events in English. No PII in logs: ids, counts and reasons only (never names, dates, convênio, message text).
- **Flow-state rule (skill `conversation-flow-state`):** every non-IDLE state has a time-bounded exit not gated on tenant config → Task 1 adds the floor for `EDIT_BOOKING`.
- **Commands:** tests from Git Bash at the repo root: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest <file> -q`. Never `ruff format .`. Files this plan **creates**: `uvx ruff format <file>` then `uvx ruff check --fix <file>`. Files it **modifies**: `uvx ruff check <file>` must stay clean on the lines you touch; run `uvx ruff check --fix --select I <file>` if an import block you touched is reported unsorted. The tree is CRLF: after each task `git diff --stat` must show only the touched lines.
- **Commits:** local only, `git add <explicit paths>` (never `-A`), message ending with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. No push, no deploy. The full suite is run once at the end of Task 10.
- **R3 tests that pin removed behavior are changed deliberately** (Tasks 4–5), never deleted to make a run green: each replaced assertion names the new behavior.

## Review Focus

Each line is pinned by the test named in brackets, in the task that owns the code.

1. **The draft never changes the real appointment** — tapping "Cancelar" on the final card, leaving the chat, or a calendar failure leaves the appointment exactly as it was. [Task 9 `test_the_draft_never_touches_the_appointment_before_confirm`, `test_cancel_on_the_final_card_keeps_the_appointment`, `test_a_calendar_failure_changes_nothing_and_keeps_the_draft`]
2. **A foreign `remedit|` / `remcancel|` id** (another patient's reminder row, or a stale version) → "Não encontrei essa consulta." / "horário antigo", nothing opened or cancelled. [Task 5 `test_new_ids_are_checked_against_the_patient_and_the_version`]
3. **Paid Pix deposit** hides Mudar serviço / médico / convênio; reschedule limit reached hides Mudar data / horário. [Task 7 `test_a_paid_deposit_hides_service_doctor_and_insurance`, `test_a_reached_reschedule_limit_hides_date_and_time`]
4. **A doctor who is busy at the appointment's day and time** is marked ❌ and picking them goes on to day and time instead of confirming a slot that does not exist. [Task 8 `test_the_doctor_list_marks_free_and_busy`, `test_a_busy_doctor_continues_to_day_and_time`]
5. **Doctor change moves the Google event across calendars without a window with no event:** new event created first, old one deleted only after the commit. [Task 9 `test_a_doctor_change_creates_the_new_event_then_deletes_the_old_one_after_commit`]
6. **A service change that makes the appointment longer than the free window** re-asks day/time instead of double-booking. [Task 8 `test_a_longer_service_that_does_not_fit_asks_for_a_new_time`]
7. **Five buttons on WhatsApp** are sent as a list, never silently cut to three. [Task 3 `test_five_labels_become_a_list_on_a_three_button_channel`]
8. **Old cards still on a patient's screen** (`remother|`, `remresched|`, `remnew|`, `remgiveup|`) keep working. [Task 5 `test_legacy_ids_still_resolve`]

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `migrations/versions/b1c4e7a2d9f3_conversation_flow_edit_draft.py` | Create | additive column |
| `src/secretaria/models/conversation.py` | Modify | `flow_edit_draft`, `FlowState.EDIT_BOOKING` |
| `src/secretaria/services/appointment_edit.py` | Create | labels, texts, `EditDraft`, `EditContext`, `build_edit_recap` (pure) |
| `src/secretaria/services/appointment_edit_flow.py` | Create | the steps: menu, date/time, service, doctor, convênio, patient, confirm, apply |
| `src/secretaria/services/flow_router.py` | Modify | `STEP_EDIT_*`, `EDIT_DAY_BRANCH`, `FlowRouterResult.{flow_edit_draft, appointment_edit}`, `enter_day_picker(anchor=)`, `_carry_edit_draft`, dispatch, `route(edit_context=)` |
| `src/secretaria/services/calendar.py` | Modify | `update_event_details`, `is_slot_free` |
| `src/secretaria/services/reminder_text.py`, `src/secretaria/schemas/webhook.py` | Modify | `Alterar Dados` button, `remedit` id |
| `src/secretaria/workers/shared/reminder_actions.py` | Modify | Confirmar + menu, direct Cancelar, `remedit` → edit |
| `src/secretaria/workers/shared/actions.py` | Modify | edit hand-off from a reminder tap |
| `src/secretaria/workers/shared/bubbles.py`, `src/secretaria/workers/shared/dispatch.py`, `src/secretaria/services/whatsapp.py`, `src/secretaria/services/channel_sender.py` | Modify | wide menus: buttons on the Portal, list on WhatsApp |
| `src/secretaria/workers/shared/appointment_edit_support.py` | Create | `build_edit_context`, `edit_guards` (DB-reading, worker side) |
| `src/secretaria/workers/shared/appointment_edit_apply.py` | Create | `apply_appointment_edit` (in the txn), `_finish_edit` (after commit) |
| `src/secretaria/workers/shared/flow_runner.py`, `orchestrator.py`, `state_expiry.py`, `turn_router.py`, `src/secretaria/ai/tools.py` | Modify | persist/snapshot/clear the draft; thread `edit_context`; floor |
| `src/secretaria/services/patient_context.py` | Modify | `load_upcoming_appointments` also returns `insurance`, `attendee_name` |
| `tests/_edit_flow_support.py` | Create | shared fakes for the flow tests |
| `tests/test_flow_edit_draft_column.py`, `tests/test_appointment_edit_pure.py`, `tests/test_wide_menus.py`, `tests/test_reminder_r6_buttons.py`, `tests/test_reminder_r6_actions.py`, `tests/test_calendar_edit_methods.py`, `tests/test_edit_day_picker.py`, `tests/test_appointment_edit_flow_menu.py`, `tests/test_appointment_edit_flow_fields.py`, `tests/test_appointment_edit_apply.py` | Create | one file per unit |
| `docs/CHECKPOINT_lembretes_r6.md` (+ pointers), `docs/LEMBRETES_MODELOS_META.md` | Create/Modify | state after validation |

## Interfaces → Produces (later tasks rely on these exact names)

```python
# services/appointment_edit.py
LABEL_EDIT_DATE = "Mudar data"; LABEL_EDIT_TIME = "Mudar horário"; LABEL_EDIT_SERVICE = "Mudar serviço"
LABEL_EDIT_DOCTOR = "Mudar médico"; LABEL_EDIT_MORE = "Outro"; LABEL_EDIT_INSURANCE = "Mudar convênio"
LABEL_EDIT_PATIENT = "Mudar paciente"; LABEL_EDIT_BACK = "Voltar"; LABEL_EDIT_MORE_DATA = "Alterar Mais Dados"
LABEL_TIME_TOO_YES = "Sim, mudar horário"; LABEL_TIME_TOO_NO = "Não, manter horário"
EDIT_MENU_BODY, EDIT_MORE_BODY, EDIT_TIME_TOO_BODY, EDIT_TIME_BUSY, EDIT_KEPT, EDIT_APPLIED,
EDIT_STALE, EDIT_PIX_NOTICE, EDIT_LIMIT_NOTICE, EDIT_RESLOT_NOTICE  (str constants)
MARK_FREE = "✅"; MARK_BUSY = "❌"
@dataclass(frozen=True) class EditDraft:
    appointment_id: str; current: dict; original: dict; stage: dict
    from_appointment(appt: dict, tz) -> EditDraft; to_json() -> dict; from_json(raw) -> EditDraft | None
    with_changes(**fields) -> EditDraft; with_stage(**stage) -> EditDraft; changed() -> list[str]
    start: datetime; end: datetime; duration_minutes: int
@dataclass class EditContext:
    calendars: dict[str, Any]; paid_deposit: bool = False; reschedule_blocked: bool = False
    calendar_for(professional_id: str | None) -> Any | None
build_edit_recap(draft, *, doctor, address, requirements) -> str

# services/appointment_edit_flow.py
enter_edit_menu(tenant, appt: dict, ctx: EditContext | None = None) -> FlowRouterResult
async edit_step(conversation, tenant, body, appointments, professionals, ctx, patient_name=None) -> FlowRouterResult

# services/flow_router.py
FlowRouterResult.flow_edit_draft: dict | None = None
FlowRouterResult.appointment_edit: dict | None = None
STEP_EDIT_MENU, STEP_EDIT_MORE, STEP_EDIT_DAY, STEP_EDIT_DAY_RETRY, STEP_EDIT_DAY_ESCAPE, STEP_EDIT_SLOT,
STEP_EDIT_TIME_TOO, STEP_EDIT_SERVICE, STEP_EDIT_DOCTOR, STEP_EDIT_INSURANCE, STEP_EDIT_INSURANCE_OTHER,
STEP_EDIT_ATT_CHOICE, STEP_EDIT_ATT_NAME, STEP_EDIT_ATT_AUTH, STEP_EDIT_CONFIRM
EDIT_DAY_BRANCH: DayBranch;  async enter_day_picker(..., anchor: datetime | None = None)
route(..., edit_context: EditContext | None = None)

# services/calendar.py
CalendarService.update_event_details(event_id, start, end, summary, description) -> dict
CalendarService.is_slot_free(start, end, *, ignore_event_id=None) -> bool

# workers/shared
appointment_edit_support.build_edit_context(session, tenant, tenant_config, conversation, professional_rows, upcoming) -> EditContext
appointment_edit_support.edit_guards(session, tenant, appointment) -> tuple[bool, bool]   # (paid_deposit, reschedule_blocked)
appointment_edit_apply.apply_appointment_edit(session, *, tenant, tenant_id, edit: dict) -> AppliedEdit | None
appointment_edit_apply._finish_edit(tenant, applied: AppliedEdit) -> None
```

---

## Task 1: The draft column, the state, and the persistence seams

**Files:**
- Create: `migrations/versions/b1c4e7a2d9f3_conversation_flow_edit_draft.py`
- Modify: `src/secretaria/models/conversation.py` (`FlowState`, column)
- Modify: `src/secretaria/services/flow_router.py` (`FlowRouterResult.flow_edit_draft`, `FlowRouterResult.appointment_edit`)
- Modify: `src/secretaria/workers/shared/flow_runner.py` (`_apply_flow_result`: one write)
- Modify: `src/secretaria/workers/orchestrator.py` (`flow_snapshot`)
- Modify: `src/secretaria/workers/shared/state_expiry.py` (new floor + clears), `src/secretaria/workers/turn_router.py` (floor call, "Não" and code-wait clears), `src/secretaria/ai/tools.py` (`_persist_appointment` clear)
- Test: `tests/test_flow_edit_draft_column.py`

**Interfaces:**
- Consumes: R3's `a7e2c9d4f1b6` head; the R3 pattern for the marker column (`flow_replaces_appointment_id`).
- Produces: `Conversation.flow_edit_draft: Mapped[dict | None]`, `FlowState.EDIT_BOOKING`, `FlowRouterResult.flow_edit_draft`, `FlowRouterResult.appointment_edit`, `state_expiry._expire_stale_edit_state(conversation, tenant, last_activity_at) -> bool`.

- [ ] **Step 1: Check the head**

Run:

```bash
uv run python -c "from alembic.config import Config; from alembic.script import ScriptDirectory; c=Config('alembic.ini'); c.set_main_option('script_location','migrations'); print(ScriptDirectory.from_config(c).get_heads())"
grep -rn "b1c4e7a2d9f3" migrations src tests
```

Expected: `['a7e2c9d4f1b6']` and no `grep` output. A different single head: use it as `down_revision` here and as `DOWN_REVISION` in the test. Two heads or a `grep` hit: STOP and report.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_flow_edit_draft_column.py`:

```python
"""conversations.flow_edit_draft + FlowState.EDIT_BOOKING (TASK-032 R6, spec §5.2)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

import importlib.util  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from alembic.operations import Operations  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402

from secretaria.ai.formatter import TextBubble  # noqa: E402
from secretaria.models import Conversation, FlowState  # noqa: E402
from secretaria.services.flow_router import FlowRouterResult  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import state_expiry  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402, F401
from tests._reminders_r3 import get_conversation, set_conversation, wire  # noqa: E402
from tests._reminders_v2 import WA_ID, seed_world  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REVISION = "b1c4e7a2d9f3"
DOWN_REVISION = "a7e2c9d4f1b6"


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_conversation_flow_edit_draft.py"
    spec = importlib.util.spec_from_file_location("flow_edit_draft_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_is_the_single_head_on_top_of_r3():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == [REVISION]
    assert _migration().down_revision == DOWN_REVISION


def test_the_migration_adds_and_drops_a_nullable_json_column_on_sqlite(tmp_path):
    migration = _migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'edit.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE conversations (id CHAR(32) PRIMARY KEY)"))
        conn.execute(sa.text("INSERT INTO conversations (id) VALUES ('a')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            columns = {c["name"]: c for c in sa.inspect(conn).get_columns("conversations")}
            assert columns["flow_edit_draft"]["nullable"] is True
            assert conn.execute(sa.text("SELECT flow_edit_draft FROM conversations")).scalar() is None
            migration.downgrade()
            names = {c["name"] for c in sa.inspect(conn).get_columns("conversations")}
            assert "flow_edit_draft" not in names
    engine.dispose()


def test_the_model_and_the_state_exist_without_a_state_migration():
    column = Conversation.__table__.c.flow_edit_draft
    assert column.nullable is True
    assert FlowState.EDIT_BOOKING.value == "EDIT_BOOKING"
    assert len(FlowState.EDIT_BOOKING.value) <= 32  # flow_state is VARCHAR(32), no CHECK


def _reply(world):
    return tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
    )


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


async def test_apply_writes_the_draft_a_result_names_and_clears_it_otherwise(db):  # noqa: F811
    world = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3))
    draft = {"appointment_id": str(world.appointment.id), "current": {}, "original": {}, "stage": {}}
    result = FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body="ok")],
        flow_state=FlowState.EDIT_BOOKING,
        flow_step="edit_menu",
        flow_edit_draft=draft,
    )
    await tasks._apply_flow_result(_reply(world), result, WA_ID, tenant=world.tenant, waba_token="t")
    assert (await get_conversation(db, world)).flow_edit_draft == draft

    leave = FlowRouterResult(
        action="reply", bubbles=[TextBubble(body="ok")], flow_state=FlowState.MENU
    )
    await tasks._apply_flow_result(_reply(world), leave, WA_ID, tenant=world.tenant, waba_token="t")
    assert (await get_conversation(db, world)).flow_edit_draft is None


def _parked(**kw):
    base = dict(
        flow_state=FlowState.EDIT_BOOKING,
        flow_step="edit_menu",
        flow_selected_type="Consulta",
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_managing_appointment_id=None,
        flow_attendee_name=None,
        flow_draft=None,
        flow_replaces_appointment_id=None,
        flow_edit_draft={"appointment_id": "x"},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_the_edit_floor_drops_a_long_quiet_edit_and_nothing_else():
    quiet = datetime.now(UTC) - timedelta(days=2)
    tenant = SimpleNamespace(initial_flows={})

    stale = _parked()
    assert state_expiry._expire_stale_edit_state(stale, tenant, quiet) is True
    assert stale.flow_state == FlowState.IDLE
    assert stale.flow_step is None
    assert stale.flow_edit_draft is None
    assert stale.flow_managing_appointment_id is None

    recent = _parked()
    assert state_expiry._expire_stale_edit_state(recent, tenant, datetime.now(UTC)) is False
    assert recent.flow_state == FlowState.EDIT_BOOKING

    other = _parked(flow_state=FlowState.SERVICE_CATALOG)
    assert state_expiry._expire_stale_edit_state(other, tenant, quiet) is False


async def test_no_to_quer_continuar_drops_the_edit_draft(db):  # noqa: F811
    from tests._reminders_r3 import consent, turn

    world = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3))
    await consent(db, world)
    await set_conversation(
        db,
        world,
        flow_state=FlowState.EDIT_BOOKING,
        flow_step="edit_menu",
        reactivation_origin=FlowState.EDIT_BOOKING.value,
        flow_edit_draft={"appointment_id": str(world.appointment.id)},
    )
    await turn(db, world, "Não", send=False)
    assert (await get_conversation(db, world)).flow_edit_draft is None
```

- [ ] **Step 3: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_flow_edit_draft_column.py -q`
Expected: FAIL — `FileNotFoundError` for the migration, `AttributeError: flow_edit_draft` / `EDIT_BOOKING`.

- [ ] **Step 4: The migration**

Create `migrations/versions/b1c4e7a2d9f3_conversation_flow_edit_draft.py`:

```python
"""conversations.flow_edit_draft: the in-progress "Alterar Dados" draft.

TASK-032 R6 (docs/superpowers/specs/2026-10-07-lembretes-alterar-dados-design.md §5.2).
A patient who taps "Alterar Dados" on a reminder changes date, time, service,
doctor, convênio or patient through several steps; nothing touches the real
appointment until the final Confirmar. The draft (current vs original values) is
parked on the conversation:

    ADD  conversations.flow_edit_draft  JSON NULL

NULL whenever no edit is in progress. Written unconditionally by
`_apply_flow_result` like every flow field and carried by
`flow_router._carry_edit_draft` only while the conversation stays in
`EDIT_BOOKING`. `FlowState.EDIT_BOOKING` itself needs no migration (flow_state is a
VARCHAR(32) with no CHECK).

DEPLOY ORDER - the database moves FIRST: the ORM names every mapped column on every
read and write of `conversations`, so new code against a schema WITHOUT this column
fails every turn. The column is inert to the old code:

    1. `alembic upgrade head` from the NEW image (one-off), both services on the old code;
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity `match`).

Rollback narrows: the OLD code on both services first, then `alembic downgrade`.
The only data lost is an edit in progress (the appointment itself is never touched
by a draft).

Revision ID: b1c4e7a2d9f3
Revises: a7e2c9d4f1b6
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1c4e7a2d9f3"
down_revision: str | None = "a7e2c9d4f1b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no default: metadata-only on Postgres, and NULL is the true value
    # for every row that exists today.
    op.add_column("conversations", sa.Column("flow_edit_draft", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "flow_edit_draft")
```

- [ ] **Step 5: The model**

In `src/secretaria/models/conversation.py`, in `class FlowState`, after `AWAITING_NAME = "AWAITING_NAME"` add:

```python
    # TASK-032 R6 ("Alterar Dados" on a reminder): the patient is changing date, time,
    # service, doctor, convênio or patient of ONE existing appointment through a draft
    # (`flow_edit_draft`). Same reasoning, same absence of a migration: 12 characters.
    EDIT_BOOKING = "EDIT_BOOKING"
```

and add to the class docstring list (after the `AWAITING_NAME` paragraph) one line: `EDIT_BOOKING    - patient is editing an existing appointment through a draft (services/appointment_edit_flow.py).`

In the `Conversation` model, directly above the comment `    # Set while a returning patient is mid-"quer continuar?" prompt: holds the`, insert:

```python
    # TASK-032 R6: the "Alterar Dados" draft - {"appointment_id", "current", "original",
    # "stage"} (services/appointment_edit.py::EditDraft.to_json). The real appointment is
    # never touched until the final Confirmar. Written unconditionally by
    # `_apply_flow_result` like every flow field and carried only while the conversation
    # stays in `EDIT_BOOKING` (`flow_router._carry_edit_draft`). Third-party names appear
    # only inside `current["attendee_name"]` / `original["attendee_name"]` (same PII rule as
    # `flow_attendee_name`).
    flow_edit_draft: Mapped[dict | None] = mapped_column(JSON, nullable=True)
```

- [ ] **Step 6: The result fields, the write, the snapshot**

(a) `src/secretaria/services/flow_router.py`, in `FlowRouterResult`, after the line `flow_replaces_appointment_id: UUID | None = None` append:

```python
    # TASK-032 R6 ("Alterar Dados"): the edit draft (`EditDraft.to_json()`), written
    # unconditionally by `_apply_flow_result` like every flow field and kept by
    # `_carry_edit_draft` only while the conversation stays in `EDIT_BOOKING`.
    flow_edit_draft: dict | None = None
    # TASK-032 R6: the confirmed edit, applied to the SAME appointment row by
    # `workers/shared/appointment_edit_apply.py` in `_apply_flow_result`'s transaction:
    # {"appointment_id": UUID, "old_google_event_id", "google_event_id", "google_event_link",
    #  "appointment_type", "professional_id": UUID | None, "old_professional_id": UUID | None,
    #  "insurance", "attendee_name", "start_at", "end_at", "time_changed": bool,
    #  "doctor_changed": bool}. Never persisted on the conversation.
    appointment_edit: dict | None = None
```

(b) `src/secretaria/workers/shared/flow_runner.py`, in `_apply_flow_result`, directly after `conv.flow_replaces_appointment_id = result.flow_replaces_appointment_id` insert:

```python
                    conv.flow_edit_draft = result.flow_edit_draft
```

(c) `src/secretaria/workers/orchestrator.py`, in the `flow_snapshot` `SimpleNamespace`, directly after the line `flow_replaces_appointment_id=conversation.flow_replaces_appointment_id,` insert:

```python
                            flow_edit_draft=conversation.flow_edit_draft,
```

- [ ] **Step 7: The floor and every exit**

(a) `src/secretaria/workers/shared/state_expiry.py`: in `_expire_stale_llm_state` and in `_expire_stale_attendee_step`, directly after `conversation.flow_replaces_appointment_id = None` insert `    conversation.flow_edit_draft = None`. After `_expire_stale_attendee_step` add:

```python
def _expire_stale_edit_state(
    conversation: Conversation,
    tenant: Tenant,
    last_activity_at: datetime | None,
) -> bool:
    """Drop a long-idle "Alterar Dados" edit so the next turn re-opens the menu.

    TASK-032 R6, skill conversation-flow-state: every non-IDLE state needs a
    time-bounded exit that does not depend on tenant config. The draft never touched
    the real appointment, so dropping it loses nothing but the patient's unfinished
    choices. Silent, in place, `flow_*` only - the shape of the two floors above.
    """
    if conversation.flow_state != FlowState.EDIT_BOOKING:
        return False
    if last_activity_at is None:
        return False
    gap = datetime.now(UTC) - _as_utc(last_activity_at)
    if gap < timedelta(minutes=llm_state_ttl_minutes(tenant)):
        return False
    conversation.flow_state = FlowState.IDLE
    conversation.flow_step = None
    conversation.flow_selected_type = None
    conversation.flow_selected_day = None
    conversation.flow_selected_slot = None
    conversation.flow_managing_appointment_id = None
    conversation.flow_attendee_name = None
    conversation.flow_edit_draft = None
    return True
```

(b) `src/secretaria/workers/turn_router.py`: import `_expire_stale_edit_state` next to the other two (same `from secretaria.workers.shared.state_expiry import (...)` block); after the `if _expire_stale_attendee_step(...)` block (the one logging `conversation_attendee_step_expired`) add:

```python
    if _expire_stale_edit_state(conversation, tenant, last_activity_at):
        logger.info(
            "conversation_edit_state_expired",
            conversation_id=str(conversation.id),
            tenant_id=str(tenant.id),
            ttl_minutes=llm_state_ttl_minutes(tenant),
        )
```

In the `if answer == "no":` branch, after `conversation.flow_replaces_appointment_id = None` (before `return _ReplyContext(`) add `            conversation.flow_edit_draft = None`; in the abandoned-code-wait branch after `conversation.flow_replaces_appointment_id = None` add `            conversation.flow_edit_draft = None`.

(c) `src/secretaria/ai/tools.py`, in `_persist_appointment`, directly after `booked_conversation.flow_replaces_appointment_id = None` add `                        booked_conversation.flow_edit_draft = None`.

- [ ] **Step 8: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_flow_edit_draft_column.py tests/test_flow_replaces_column.py tests/test_flow_draft_column.py tests/test_appointment_replacement_marker.py tests/test_llm_state_expiry.py -q`
Expected: all pass. If `test_flow_replaces_column.py::test_the_migration_is_the_single_head_on_top_of_the_previous_one` fails because a newer head exists, relax it exactly as R3 relaxed `test_flow_draft_column.py` (assert one head and that `a7e2c9d4f1b6` is in the chain) and ledger the ruling.

- [ ] **Step 9: Lint and commit**

```bash
uvx ruff format migrations/versions/b1c4e7a2d9f3_conversation_flow_edit_draft.py tests/test_flow_edit_draft_column.py
uvx ruff check --fix migrations/versions/b1c4e7a2d9f3_conversation_flow_edit_draft.py tests/test_flow_edit_draft_column.py
uvx ruff check src/secretaria/models/conversation.py src/secretaria/services/flow_router.py src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/orchestrator.py src/secretaria/workers/shared/state_expiry.py src/secretaria/workers/turn_router.py src/secretaria/ai/tools.py
git diff --stat
git add migrations/versions/b1c4e7a2d9f3_conversation_flow_edit_draft.py src/secretaria/models/conversation.py src/secretaria/services/flow_router.py src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/orchestrator.py src/secretaria/workers/shared/state_expiry.py src/secretaria/workers/turn_router.py src/secretaria/ai/tools.py tests/test_flow_edit_draft_column.py
git commit -F - <<'EOF'
feat(flow): conversations.flow_edit_draft and FlowState.EDIT_BOOKING (TASK-032 R6)

Additive nullable JSON column (migration b1c4e7a2d9f3, database before both
services), the draft persisted like every flow field, and a silence floor for the
new state.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 2: The pure pieces — labels, texts, draft, context, recap

**Files:**
- Create: `src/secretaria/services/appointment_edit.py`
- Test: `tests/test_appointment_edit_pure.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: everything listed under `services/appointment_edit.py` in "Interfaces".

- [ ] **Step 1: Write the failing tests**

Create `tests/test_appointment_edit_pure.py`:

```python
"""The pure pieces of "Alterar Dados" (TASK-032 R6, spec §5.2/§5.5)."""

from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from secretaria.core.whatsapp_limits import MAX_BUTTON_LABEL_CHARS, MAX_INTERACTIVE_BODY_CHARS
from secretaria.services import appointment_edit as ae

TZ = ZoneInfo("America/Sao_Paulo")
DOCTOR = str(uuid4())


def _appt(**kw):
    base = {
        "id": str(uuid4()),
        "appointment_type": "Consulta",
        "start_at": datetime(2026, 10, 16, 18, 20, tzinfo=UTC),  # 15:20 in São Paulo
        "end_at": datetime(2026, 10, 16, 19, 0, tzinfo=UTC),
        "professional_id": DOCTOR,
        "insurance": "Unimed",
        "attendee_name": None,
    }
    base.update(kw)
    return base


def test_every_label_fits_a_button_and_a_list_row():
    labels = [
        ae.LABEL_EDIT_DATE,
        ae.LABEL_EDIT_TIME,
        ae.LABEL_EDIT_SERVICE,
        ae.LABEL_EDIT_DOCTOR,
        ae.LABEL_EDIT_MORE,
        ae.LABEL_EDIT_INSURANCE,
        ae.LABEL_EDIT_PATIENT,
        ae.LABEL_EDIT_BACK,
        ae.LABEL_EDIT_MORE_DATA,
        ae.LABEL_TIME_TOO_YES,
        ae.LABEL_TIME_TOO_NO,
    ]
    assert all(0 < len(label) <= MAX_BUTTON_LABEL_CHARS for label in labels)
    assert len(ae.EDIT_MENU_BODY) <= MAX_INTERACTIVE_BODY_CHARS


def test_the_menu_text_carries_the_owners_description_of_alterar_dados():
    assert "Alterar Dados" in ae.EDIT_MENU_BODY
    assert "qualquer outra coisa dessa sua consulta já marcada" in ae.EDIT_MENU_BODY


def test_a_draft_starts_equal_to_the_appointment_in_clinic_time():
    draft = ae.EditDraft.from_appointment(_appt(), TZ)
    assert draft.current == draft.original
    assert draft.current["start_at"] == "2026-10-16T15:20"
    assert draft.current["end_at"] == "2026-10-16T16:00"
    assert draft.duration_minutes == 40
    assert draft.changed() == []


def test_changed_names_each_field_once_in_a_fixed_order():
    draft = ae.EditDraft.from_appointment(_appt(), TZ)
    changed = draft.with_changes(
        start_at="2026-10-17T09:00",
        end_at="2026-10-17T09:40",
        service="Retorno",
        professional_id=str(uuid4()),
        insurance=None,
        attendee_name="Ana",
    )
    assert changed.changed() == ["data", "horário", "serviço", "médico", "convênio", "paciente"]
    only_time = draft.with_changes(start_at="2026-10-16T16:00", end_at="2026-10-16T16:40")
    assert only_time.changed() == ["horário"]
    only_day = draft.with_changes(start_at="2026-10-19T15:20", end_at="2026-10-19T16:00")
    assert only_day.changed() == ["data"]


def test_the_original_never_moves_and_the_draft_is_immutable():
    draft = ae.EditDraft.from_appointment(_appt(), TZ)
    changed = draft.with_changes(service="Retorno")
    assert draft.current["service"] == "Consulta"
    assert changed.original["service"] == "Consulta"
    with pytest.raises(AttributeError):
        draft.appointment_id = "x"  # type: ignore[misc]


def test_json_round_trip_and_garbage_is_none():
    draft = ae.EditDraft.from_appointment(_appt(), TZ).with_stage(mode="date", target_day="2026-10-20")
    again = ae.EditDraft.from_json(draft.to_json())
    assert again == draft
    for garbage in (None, {}, {"appointment_id": "x"}, "nope", {"appointment_id": "x", "current": 1}):
        assert ae.EditDraft.from_json(garbage) is None


def test_with_stage_replaces_the_whole_stage():
    draft = ae.EditDraft.from_appointment(_appt(), TZ).with_stage(mode="date")
    assert draft.with_stage().stage == {}
    assert draft.with_stage(mode="reslot").stage == {"mode": "reslot"}


def test_the_recap_lists_everything_and_what_changed():
    draft = ae.EditDraft.from_appointment(_appt(), TZ).with_changes(
        start_at="2026-10-19T10:00", end_at="2026-10-19T10:40", attendee_name="Ana Souza"
    )
    text = ae.build_edit_recap(
        draft,
        doctor="Dr. Diogo Raposo",
        address="Rua das Flores, 10",
        requirements=["Trazer exames anteriores", "Jejum de 4 horas"],
    )
    assert "Serviço: Consulta" in text
    assert "Médico: Dr. Diogo Raposo" in text
    assert "Data: 19/10/2026 (segunda-feira)" in text
    assert "Horário: 10:00" in text
    assert "Convênio: Unimed" in text
    assert "Paciente: Ana Souza" in text
    assert "Endereço: Rua das Flores, 10" in text
    assert "• Trazer exames anteriores" in text
    assert text.rstrip().endswith("O que mudou: data, horário, paciente.")


def test_the_recap_defaults_and_the_nothing_changed_line():
    text = ae.build_edit_recap(
        ae.EditDraft.from_appointment(_appt(insurance=None), TZ),
        doctor=None,
        address=None,
        requirements=[],
    )
    assert "Médico:" not in text
    assert "Endereço:" not in text
    assert "Orientações" not in text
    assert "Convênio: não informado" in text
    assert "Paciente: você" in text
    assert text.rstrip().endswith("Ainda não mudou nada.")


def test_a_long_recap_drops_orientations_before_it_breaks_the_card_limit():
    text = ae.build_edit_recap(
        ae.EditDraft.from_appointment(_appt(), TZ),
        doctor="Dr. Diogo Raposo",
        address="Rua " + "x" * 200,
        requirements=["Trazer exames anteriores e documentos " + "y" * 120] * 12,
    )
    assert len(text) <= MAX_INTERACTIVE_BODY_CHARS
    assert "Serviço: Consulta" in text and "Horário: 15:20" in text


def test_the_context_resolves_calendars_by_doctor_or_clinic():
    clinic, doctor = object(), object()
    ctx = ae.EditContext(calendars={"tenant": clinic, DOCTOR: doctor})
    assert ctx.calendar_for(DOCTOR) is doctor
    assert ctx.calendar_for(None) is clinic
    assert ctx.calendar_for(str(uuid4())) is None
    assert ae.EditContext().paid_deposit is False and ae.EditContext().reschedule_blocked is False
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_edit_pure.py -q`
Expected: collection error `ImportError: cannot import name 'appointment_edit' from 'secretaria.services'`.

- [ ] **Step 3: Implement**

Create `src/secretaria/services/appointment_edit.py`:

```python
"""The pure pieces of the "Alterar Dados" flow (TASK-032 R6).

Spec: docs/superpowers/specs/2026-10-07-lembretes-alterar-dados-design.md.

Nothing here touches the database, the calendar or a channel: labels and texts, the
`EditDraft` (what the appointment WOULD look like, next to what it is today), the
`EditContext` the worker hands the router (calendars + the Pix guards) and the
complete confirmation text. The steps live in `appointment_edit_flow.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from typing import Any

from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS

# --- Labels: <= 20 characters (Portal buttons, WhatsApp list titles) -------------------
LABEL_EDIT_DATE = "Mudar data"
LABEL_EDIT_TIME = "Mudar horário"
LABEL_EDIT_SERVICE = "Mudar serviço"
LABEL_EDIT_DOCTOR = "Mudar médico"
LABEL_EDIT_MORE = "Outro"
LABEL_EDIT_INSURANCE = "Mudar convênio"
LABEL_EDIT_PATIENT = "Mudar paciente"
LABEL_EDIT_BACK = "Voltar"
LABEL_EDIT_MORE_DATA = "Alterar Mais Dados"
LABEL_TIME_TOO_YES = "Sim, mudar horário"
LABEL_TIME_TOO_NO = "Não, manter horário"

# Availability marks on the doctor list (owner, 2026-10-07).
MARK_FREE = "✅"
MARK_BUSY = "❌"

# --- Texts -------------------------------------------------------------------------------
EDIT_MENU_BODY = (
    "*Alterar Dados*: mudar o horário, a data, o serviço, o médico ou qualquer outra "
    "coisa dessa sua consulta já marcada.\n\n"
    "O que você quer mudar?\n\n"
    "• *Mudar data*: escolher outro dia.\n"
    "• *Mudar horário*: escolher outro horário no mesmo dia.\n"
    "• *Mudar serviço*: trocar o serviço.\n"
    "• *Mudar médico*: trocar o médico.\n"
    "• *Outro*: mudar o convênio ou o paciente."
)
EDIT_MORE_BODY = (
    "Mais opções:\n\n"
    "• *Mudar convênio*: trocar o convênio da consulta.\n"
    "• *Mudar paciente*: a consulta passa a ser para outra pessoa (ou volta a ser sua)."
)
EDIT_TIME_TOO_BODY = "Quer mudar o horário também?"
EDIT_TIME_BUSY = "Esse horário não está livre nesse dia. Escolha outro:"
EDIT_RESLOT_NOTICE = "Com essa mudança o horário atual não está livre. Escolha um novo dia e horário:"
EDIT_KEPT = "Tudo bem, mantive sua consulta como estava."
EDIT_APPLIED = "Pronto! Sua consulta foi atualizada. ✅"
EDIT_STALE = "Essa consulta não está mais ativa."
EDIT_PIX_NOTICE = (
    "Como essa consulta tem sinal pago, para mudar serviço, médico ou convênio fale com a clínica."
)
EDIT_LIMIT_NOTICE = (
    "O limite de remarcações dessa consulta foi atingido; para mudar data ou horário fale com a clínica."
)

_WEEKDAYS = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)
_FIELDS = ("service", "professional_id", "start_at", "end_at", "insurance", "attendee_name")


def _local_naive(value: datetime, tz: tzinfo) -> str:
    """`value` in the clinic zone as a naive ISO minute ("2026-10-16T15:20")."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(tz).replace(tzinfo=None).isoformat(timespec="minutes")


@dataclass(frozen=True)
class EditDraft:
    """What the appointment would look like (`current`) next to what it is (`original`).

    Immutable: every change returns a new draft. `stage` is transient bookkeeping for
    the step in progress (`mode`, `target_day`, ...) and is never part of the diff.
    `current`/`original` hold: service, professional_id (str | None), start_at/end_at
    (naive clinic-local ISO minutes), insurance, attendee_name (None = the patient).
    """

    appointment_id: str
    current: dict[str, str | None]
    original: dict[str, str | None]
    stage: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_appointment(cls, appt: dict, tz: tzinfo) -> EditDraft:
        values: dict[str, str | None] = {
            "service": appt.get("appointment_type") or None,
            "professional_id": str(appt["professional_id"]) if appt.get("professional_id") else None,
            "start_at": _local_naive(appt["start_at"], tz),
            "end_at": _local_naive(appt["end_at"], tz),
            "insurance": appt.get("insurance") or None,
            "attendee_name": appt.get("attendee_name") or None,
        }
        return cls(appointment_id=str(appt["id"]), current=dict(values), original=dict(values))

    def to_json(self) -> dict:
        return {
            "appointment_id": self.appointment_id,
            "current": dict(self.current),
            "original": dict(self.original),
            "stage": dict(self.stage),
        }

    @classmethod
    def from_json(cls, raw: Any) -> EditDraft | None:
        if not isinstance(raw, dict):
            return None
        current, original = raw.get("current"), raw.get("original")
        if not isinstance(current, dict) or not isinstance(original, dict):
            return None
        if not all(key in current and key in original for key in _FIELDS):
            return None
        if not raw.get("appointment_id"):
            return None
        stage = raw.get("stage")
        return cls(
            appointment_id=str(raw["appointment_id"]),
            current=dict(current),
            original=dict(original),
            stage=dict(stage) if isinstance(stage, dict) else {},
        )

    def with_changes(self, **fields: str | None) -> EditDraft:
        unknown = set(fields) - set(_FIELDS)
        if unknown:
            raise ValueError(f"unknown draft fields: {sorted(unknown)}")
        return replace(self, current={**self.current, **fields})

    def with_stage(self, **stage: str) -> EditDraft:
        return replace(self, stage=dict(stage))

    def changed(self) -> list[str]:
        cur, org = self.current, self.original
        out: list[str] = []
        if str(cur["start_at"])[:10] != str(org["start_at"])[:10]:
            out.append("data")
        if str(cur["start_at"])[11:16] != str(org["start_at"])[11:16]:
            out.append("horário")
        if cur["service"] != org["service"]:
            out.append("serviço")
        if cur["professional_id"] != org["professional_id"]:
            out.append("médico")
        if cur["insurance"] != org["insurance"]:
            out.append("convênio")
        if cur["attendee_name"] != org["attendee_name"]:
            out.append("paciente")
        return out

    @property
    def start(self) -> datetime:
        return datetime.fromisoformat(str(self.current["start_at"]))

    @property
    def end(self) -> datetime:
        return datetime.fromisoformat(str(self.current["end_at"]))

    @property
    def duration_minutes(self) -> int:
        return max(1, int((self.end - self.start).total_seconds() // 60))


@dataclass
class EditContext:
    """What the worker hands the router for an edit turn (the router does no I/O).

    `calendars` maps a professional id (str) or "tenant" to that agenda's CalendarService
    (None when it could not be built). `paid_deposit` hides service/doctor/convênio
    (the price may differ); `reschedule_blocked` hides date/time (the Pix reschedule
    limit was reached).
    """

    calendars: dict[str, Any] = field(default_factory=dict)
    paid_deposit: bool = False
    reschedule_blocked: bool = False

    def calendar_for(self, professional_id: str | None) -> Any | None:
        return self.calendars.get(professional_id or "tenant")


def _recap_lines(
    draft: EditDraft,
    *,
    doctor: str | None,
    address: str | None,
    requirements: list[str],
    include_extras: bool,
) -> str:
    start = draft.start
    cur = draft.current
    lines = ["Confira como vai ficar sua consulta:", ""]
    lines.append(f"Serviço: {cur['service'] or 'Consulta'}")
    if doctor:
        lines.append(f"Médico: {doctor}")
    lines.append(f"Data: {start.strftime('%d/%m/%Y')} ({_WEEKDAYS[start.weekday()]})")
    lines.append(f"Horário: {start.strftime('%H:%M')}")
    lines.append(f"Convênio: {cur['insurance'] or 'não informado'}")
    lines.append(f"Paciente: {cur['attendee_name']}" if cur["attendee_name"] else "Paciente: você")
    if include_extras and address:
        lines.append(f"Endereço: {address}")
    if include_extras and requirements:
        lines += ["", "Orientações:", *[f"• {item}" for item in requirements]]
    changed = draft.changed()
    lines += ["", f"O que mudou: {', '.join(changed)}." if changed else "Ainda não mudou nada."]
    return "\n".join(lines)


def build_edit_recap(
    draft: EditDraft,
    *,
    doctor: str | None,
    address: str | None,
    requirements: list[str],
) -> str:
    """The ONE complete confirmation text: everything the patient needs to check.

    Service, doctor, date (with weekday), time, convênio, patient, the clinic's address
    and the service's orientations (when there are any), then what changed. When it would
    pass the 1024-character card limit the address and the orientations are dropped first;
    the facts the patient is confirming are never cut.
    """
    text = _recap_lines(
        draft, doctor=doctor, address=address, requirements=requirements, include_extras=True
    )
    if len(text) <= MAX_INTERACTIVE_BODY_CHARS:
        return text
    return _recap_lines(
        draft, doctor=doctor, address=address, requirements=requirements, include_extras=False
    )[:MAX_INTERACTIVE_BODY_CHARS]
```

- [ ] **Step 4: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_edit_pure.py -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format src/secretaria/services/appointment_edit.py tests/test_appointment_edit_pure.py
uvx ruff check --fix src/secretaria/services/appointment_edit.py tests/test_appointment_edit_pure.py
git add src/secretaria/services/appointment_edit.py tests/test_appointment_edit_pure.py
git commit -F - <<'EOF'
feat(edit): pure labels, draft, context and recap for Alterar Dados (TASK-032 R6)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 3: Wide menus — real buttons on the Portal, a list on WhatsApp

**Files:**
- Modify: `src/secretaria/services/whatsapp.py` (`interactive_buttons_record(max_buttons=)`, `WhatsAppClient.MAX_BUTTONS`)
- Modify: `src/secretaria/services/channel_sender.py` (`BrainMessageSender.MAX_BUTTONS`, its `send_buttons`)
- Modify: `src/secretaria/workers/shared/bubbles.py` (`_for_client`, `_client_max_buttons`, `_send_bubble`, `_bubble_interactive`)
- Modify: `src/secretaria/workers/shared/dispatch.py` (the send/record loop)
- Test: `tests/test_wide_menus.py`

**Interfaces:**
- Consumes: `MenuBubble` (`services/flow_router.py`), `SlotsBubble` (`ai/formatter.py`), `MAX_BUTTONS_PER_MESSAGE`.
- Produces: `WhatsAppClient.MAX_BUTTONS = 3`, `BrainMessageSender.MAX_BUTTONS = 8`; `bubbles._for_client(bubble, client)`, `bubbles._client_max_buttons(client) -> int`; `interactive_buttons_record(body, buttons, *, max_buttons=MAX_BUTTONS_PER_MESSAGE)`; `_bubble_interactive(bubble, *, max_buttons=MAX_BUTTONS_PER_MESSAGE)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_wide_menus.py`:

```python
"""A menu with more labels than the channel has buttons (TASK-032 R6, spec §5.3)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from secretaria.ai.formatter import SlotsBubble  # noqa: E402
from secretaria.services.channel_sender import BrainMessageSender  # noqa: E402
from secretaria.services.flow_router import MenuBubble  # noqa: E402
from secretaria.services.whatsapp import WhatsAppClient, interactive_buttons_record  # noqa: E402
from secretaria.workers.shared.bubbles import (  # noqa: E402
    _bubble_interactive,
    _client_max_buttons,
    _for_client,
    _send_bubble,
)

LABELS = ["Mudar data", "Mudar horário", "Mudar serviço", "Mudar médico", "Outro"]


class _Recorder:
    def __init__(self, max_buttons: int) -> None:
        self.MAX_BUTTONS = max_buttons
        self.calls: list[tuple] = []

    async def send_buttons(self, to, body, buttons):
        self.calls.append(("buttons", body, buttons))
        return {}

    async def send_list(self, to, body, button_label, rows, section_title="Opções"):
        self.calls.append(("list", body, button_label, rows, section_title))
        return {}

    async def send_text_message(self, to, body):
        self.calls.append(("text", body))
        return {}


async def test_five_labels_become_a_list_on_a_three_button_channel():
    client = _Recorder(3)

    await _send_bubble(client, "to", MenuBubble(body="O que mudar?", labels=LABELS))

    [(kind, body, button_label, rows, _section)] = client.calls
    assert (kind, body, button_label) == ("list", "O que mudar?", "Ver opções")
    assert [row[0] for row in rows] == [f"menu|{i}" for i in range(5)]
    assert [row[1] for row in rows] == LABELS


async def test_five_labels_stay_buttons_on_a_wide_channel():
    client = _Recorder(8)

    await _send_bubble(client, "to", MenuBubble(body="O que mudar?", labels=LABELS))

    [(kind, _body, buttons)] = client.calls
    assert kind == "buttons"
    assert [label for _id, label in buttons] == LABELS


async def test_a_menu_that_fits_is_never_touched():
    client = _Recorder(3)
    bubble = MenuBubble(body="b", labels=["Sim", "Não"])
    assert _for_client(bubble, client) is bubble
    await _send_bubble(client, "to", bubble)
    assert client.calls[0][0] == "buttons"


def test_the_record_matches_what_was_sent_on_each_channel():
    menu = MenuBubble(body="O que mudar?", labels=LABELS)
    narrow, wide = _Recorder(3), _Recorder(8)

    as_list = _bubble_interactive(_for_client(menu, narrow), max_buttons=_client_max_buttons(narrow))
    as_buttons = _bubble_interactive(_for_client(menu, wide), max_buttons=_client_max_buttons(wide))

    assert as_list["kind"] == "list"
    assert [option["title"] for option in as_list["options"]] == LABELS
    assert as_buttons["kind"] == "buttons"
    assert [option["title"] for option in as_buttons["options"]] == LABELS


def test_the_buttons_record_cap_is_configurable_and_defaults_to_three():
    buttons = [(f"b|{i}", label) for i, label in enumerate(LABELS)]
    assert len(interactive_buttons_record("b", buttons)["options"]) == 3
    assert len(interactive_buttons_record("b", buttons, max_buttons=8)["options"]) == 5


def test_each_channel_declares_how_many_buttons_it_draws():
    assert WhatsAppClient.MAX_BUTTONS == 3
    assert BrainMessageSender.MAX_BUTTONS >= 5
    assert isinstance(_for_client(MenuBubble(body="b", labels=LABELS), _Recorder(3)), SlotsBubble)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_wide_menus.py -q`
Expected: collection error `ImportError: cannot import name '_client_max_buttons'` (and `_for_client`).

- [ ] **Step 3: The record cap and the channel attributes**

(a) `src/secretaria/services/whatsapp.py`: replace

```python
def interactive_buttons_record(body: str, buttons: list[tuple[str, str]]) -> dict:
```

with

```python
def interactive_buttons_record(
    body: str, buttons: list[tuple[str, str]], *, max_buttons: int = MAX_BUTTONS_PER_MESSAGE
) -> dict:
```

and replace `for bid, title in buttons[:MAX_BUTTONS_PER_MESSAGE]` with `for bid, title in buttons[:max_buttons]`. Update the docstring sentence "buttons past MAX_BUTTONS_PER_MESSAGE dropped" to "buttons past `max_buttons` (WhatsApp's 3 by default; the Portal draws more) dropped". In `class WhatsAppClient:` add as the first statement after its docstring (or directly under the `class` line if it has none):

```python
    # How many reply buttons this channel draws; a wider menu goes out as a list
    # (workers/shared/bubbles.py::_for_client). WhatsApp's hard limit.
    MAX_BUTTONS = MAX_BUTTONS_PER_MESSAGE
```

(b) `src/secretaria/services/channel_sender.py`: in `class BrainMessageSender:` add the same kind of attribute below its docstring:

```python
    # The Portal draws as many buttons as the card carries (owner, 2026-10-07: "é o nosso
    # portal"), so a wide menu stays buttons here and becomes a list only on WhatsApp.
    MAX_BUTTONS = 8
```

and in its `send_buttons`, replace `interactive=interactive_buttons_record(body, buttons),` with `interactive=interactive_buttons_record(body, buttons, max_buttons=self.MAX_BUTTONS),`.

- [ ] **Step 4: The bubble seam**

In `src/secretaria/workers/shared/bubbles.py`: add `from secretaria.core.whatsapp_limits import MAX_BUTTONS_PER_MESSAGE` to the imports and, directly after `_slots_rows`, add:

```python
WIDE_MENU_BUTTON_LABEL = "Ver opções"
WIDE_MENU_SECTION_TITLE = "Opções"


def _client_max_buttons(client) -> int:
    """How many reply buttons `client`'s channel draws (WhatsApp 3, the Portal more)."""
    return int(getattr(client, "MAX_BUTTONS", MAX_BUTTONS_PER_MESSAGE))


def _for_client(
    bubble: TextBubble | ButtonBubble | SlotsBubble | MenuBubble, client
) -> TextBubble | ButtonBubble | SlotsBubble | MenuBubble:
    """A menu with more labels than `client` has buttons becomes a tappable list.

    The router stays channel-neutral: it asks for N options as a `MenuBubble`. WhatsApp
    would silently cut anything past three (`interactive_buttons_record`), so there the
    same options go out as a list - same ids ("menu|<i>"), same titles, so the tap comes
    back as the label exactly like a button's. Idempotent: a list is returned as is.
    """
    if isinstance(bubble, MenuBubble) and len(bubble.labels) > _client_max_buttons(client):
        return SlotsBubble(
            body=bubble.body,
            rows=[(f"menu|{index}", label, None) for index, label in enumerate(bubble.labels)],
            button_label=WIDE_MENU_BUTTON_LABEL,
            section_title=WIDE_MENU_SECTION_TITLE,
        )
    return bubble
```

In `_send_bubble`, as the first statement of the body (after the docstring) add `    bubble = _for_client(bubble, client)`. Replace the signature and the buttons line of `_bubble_interactive` so it reads:

```python
def _bubble_interactive(
    bubble: TextBubble | ButtonBubble | SlotsBubble | MenuBubble,
    *,
    max_buttons: int = MAX_BUTTONS_PER_MESSAGE,
) -> dict | None:
```

and `return interactive_buttons_record(bubble.body, buttons, max_buttons=max_buttons)`.

- [ ] **Step 5: The dispatch loop**

In `src/secretaria/workers/shared/dispatch.py`: extend the import from `secretaria.workers.shared.bubbles` with `_client_max_buttons, _for_client`; in the loop replace

```python
            result = await _send_bubble(client, reply.patient_ref, bubble)
```

with

```python
            shown = _for_client(bubble, client)
            result = await _send_bubble(client, reply.patient_ref, shown)
```

and `interactive=_bubble_interactive(bubble),` with `interactive=_bubble_interactive(shown, max_buttons=_client_max_buttons(client)),` (the history body keeps using the original `bubble`). `shown` is defined inside the `try`; if the send raises, the `except` path `continue`s/returns before the record, so no use-before-assignment — read the surrounding code and keep it that way.

- [ ] **Step 6: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_wide_menus.py tests/test_workers_layering.py tests/test_brain_message_interactive_tap.py tests/test_interactive_bubbles.py -q`
Expected: all pass (if `tests/test_interactive_bubbles.py` does not exist, drop it from the command).

- [ ] **Step 7: Lint and commit**

```bash
uvx ruff format tests/test_wide_menus.py
uvx ruff check --fix tests/test_wide_menus.py
uvx ruff check src/secretaria/services/whatsapp.py src/secretaria/services/channel_sender.py src/secretaria/workers/shared/bubbles.py src/secretaria/workers/shared/dispatch.py
git diff --stat
git add src/secretaria/services/whatsapp.py src/secretaria/services/channel_sender.py src/secretaria/workers/shared/bubbles.py src/secretaria/workers/shared/dispatch.py tests/test_wide_menus.py
git commit -F - <<'EOF'
feat(channels): wide menus - buttons on the Portal, a list on WhatsApp (TASK-032 R6)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 4: The reminder card says Confirmar / Cancelar / Alterar Dados

**Files:**
- Modify: `src/secretaria/services/reminder_text.py`, `src/secretaria/schemas/webhook.py`
- Modify (assertions about the third button): `tests/test_reminder_v2_text.py` and any other test that pins `reminder_buttons` (see Step 4)
- Test: `tests/test_reminder_r6_buttons.py`

**Interfaces:**
- Consumes: R2/R3 `reminder_text` names.
- Produces: `LABEL_EDIT = "Alterar Dados"`, `ACTION_EDIT = "remedit"`, `LEGACY_REMINDER_ACTIONS`; `REMINDER_ACTIONS == ("remconfirm", "remcancel", "remedit")`; `CANCEL_PATH_ACTIONS == ("remgiveupyes", "remkeep")`; `REMINDER_ROW_ACTIONS` = the union of all of them; `reminder_buttons(id)` = Confirmar / Cancelar / Alterar Dados.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reminder_r6_buttons.py`:

```python
"""The reminder card's buttons and ids (TASK-032 R6, spec §5.1)."""

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


def test_the_reminder_card_is_confirmar_cancelar_alterar_dados():
    reminder_id = uuid4()
    assert rt.reminder_buttons(reminder_id) == [
        (f"remconfirm|{reminder_id}", "Confirmar"),
        (f"remcancel|{reminder_id}", "Cancelar"),
        (f"remedit|{reminder_id}", "Alterar Dados"),
    ]
    assert len(rt.LABEL_EDIT) <= MAX_BUTTON_LABEL_CHARS


def test_remedit_decodes_like_the_other_row_ids():
    reminder_id = str(uuid4())
    assert decode_action_id(f"remedit|{reminder_id}") == ("remedit", reminder_id)


def test_the_actions_are_split_between_current_legacy_and_confirmation_steps():
    assert rt.REMINDER_ACTIONS == ("remconfirm", "remcancel", "remedit")
    assert set(rt.LEGACY_REMINDER_ACTIONS) == {"remother", "remresched", "remnew", "remgiveup"}
    assert rt.CANCEL_PATH_ACTIONS == ("remgiveupyes", "remkeep")
    assert set(rt.REMINDER_ROW_ACTIONS) == (
        set(rt.REMINDER_ACTIONS) | set(rt.LEGACY_REMINDER_ACTIONS) | set(rt.CANCEL_PATH_ACTIONS)
    )


def test_the_decoder_knows_exactly_the_rem_actions():
    known = {prefix[:-1] for prefix in webhook._ACTION_BUTTON_PREFIXES if prefix.startswith("rem")}
    assert known == set(rt.REMINDER_ROW_ACTIONS)


@pytest.mark.parametrize("action", ["remother", "remresched", "remnew", "remgiveup"])
def test_legacy_ids_from_cards_still_on_screen_still_decode(action):
    reminder_id = str(uuid4())
    assert decode_action_id(f"{action}|{reminder_id}") == (action, reminder_id)


def test_the_template_payloads_keep_their_position():
    reminder_id = uuid4()
    assert rt.button_payloads(rt.reminder_buttons(reminder_id)) == [
        f"remconfirm|{reminder_id}",
        f"remcancel|{reminder_id}",
        f"remedit|{reminder_id}",
    ]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_r6_buttons.py -q`
Expected: FAIL — `AttributeError: ... 'LABEL_EDIT'` / `'LEGACY_REMINDER_ACTIONS'`, and the card still ends with "Outro".

- [ ] **Step 3: Implement**

(a) `src/secretaria/services/reminder_text.py`: after `ACTION_OTHER = "remother"` add `ACTION_EDIT = "remedit"`, and change the next line to:

```python
REMINDER_ACTIONS: tuple[str, ...] = (ACTION_CONFIRM, ACTION_CANCEL, ACTION_EDIT)
```

After `LABEL_OTHER = "Outro"` add:

```python
# TASK-032 R6 (owner, 2026-10-07): the third button of the reminder is "Alterar Dados".
# The approved WhatsApp TEMPLATE (used outside the 24 h window) still carries "Outro" until
# the owner resubmits it to Meta; its payload is positional, so that button already acts as
# "Alterar Dados" (docs/LEMBRETES_MODELOS_META.md).
LABEL_EDIT = "Alterar Dados"
```

Replace the body of `reminder_buttons` so the docstring and last pair read `Confirmar / Cancelar / Alterar Dados` and `(f"{ACTION_EDIT}|{reminder_id}", LABEL_EDIT),`. Replace the whole block from `ACTION_RESCHEDULE_THIS = "remresched"` through `REMINDER_ROW_ACTIONS: tuple[str, ...] = REMINDER_ACTIONS + CANCEL_PATH_ACTIONS` with:

```python
ACTION_RESCHEDULE_THIS = "remresched"
ACTION_BOOK_ANOTHER = "remnew"
ACTION_GIVE_UP = "remgiveup"
ACTION_GIVE_UP_CONFIRM = "remgiveupyes"
ACTION_KEEP = "remkeep"
# Ids R2/R3 put on cards a patient may still have on screen (or in a template tapped
# later): they keep working (workers/shared/reminder_actions.py maps them) but no new card
# carries them. `remgiveup` is today's "Cancelar"; the other three open the edit flow.
LEGACY_REMINDER_ACTIONS: tuple[str, ...] = (
    ACTION_OTHER,
    ACTION_RESCHEDULE_THIS,
    ACTION_BOOK_ANOTHER,
    ACTION_GIVE_UP,
)
# The two steps of the cancel confirmation card (`give_up_confirm_buttons`).
CANCEL_PATH_ACTIONS: tuple[str, ...] = (ACTION_GIVE_UP_CONFIRM, ACTION_KEEP)
REMINDER_ROW_ACTIONS: tuple[str, ...] = (
    REMINDER_ACTIONS + LEGACY_REMINDER_ACTIONS + CANCEL_PATH_ACTIONS
)
```

(`cancel_path_buttons`, `LABEL_RESCHEDULE_THIS`, `LABEL_BOOK_ANOTHER`, `LABEL_GIVE_UP` stay until Task 5 removes them with their users.)

(b) `src/secretaria/schemas/webhook.py`: directly after `    "remother|",` add `    "remedit|",` with the comment line `# TASK-032 R6: "Alterar Dados" (the reminder's third button; "remother|" is its legacy twin).` above it.

- [ ] **Step 4: Update the tests that pin the third button**

Run: `grep -rn '"Outro"\|LABEL_OTHER\|remother' tests/test_reminder_v2_*.py tests/test_reminder_r3_*.py tests/test_reminder_opening_*.py tests/test_reminder_template*.py 2>/dev/null`

For each hit that asserts **the reminder card's third button or the template's third payload** (not a test of the legacy `remother` tap itself), change the expectation to `"Alterar Dados"` / `remedit|`. Known one: `tests/test_reminder_v2_text.py::test_the_decoder_prefix_list_and_the_builders_name_the_same_reminder_actions` already compares to `REMINDER_ROW_ACTIONS` (R3) and stays green; `tests/test_reminder_r3_ids.py::test_the_row_actions_are_exactly_the_rem_prefixes_the_decoder_knows` asserts `rt.REMINDER_ACTIONS == ("remconfirm", "remcancel", "remother")` — change that literal to `("remconfirm", "remcancel", "remedit")` and `rt.REMINDER_ROW_ACTIONS == rt.REMINDER_ACTIONS + rt.CANCEL_PATH_ACTIONS` to `... == rt.REMINDER_ACTIONS + rt.LEGACY_REMINDER_ACTIONS + rt.CANCEL_PATH_ACTIONS`; and `test_the_cancel_path_ids_decode` keeps its five ids. Ledger each changed assertion as a ruling ("R6 changes the third button").

- [ ] **Step 5: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_r6_buttons.py tests/test_reminder_v2_text.py tests/test_reminder_r3_ids.py tests/test_action_buttons.py tests/test_reminder_opening_turn.py -q`
Expected: all pass. `test_reminder_r3_ids.py::test_the_cancel_card_is_the_three_way_choice` still passes here (the three-way card is removed in Task 5).

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff format tests/test_reminder_r6_buttons.py
uvx ruff check --fix tests/test_reminder_r6_buttons.py
uvx ruff check src/secretaria/services/reminder_text.py src/secretaria/schemas/webhook.py
git diff --stat
git add src/secretaria/services/reminder_text.py src/secretaria/schemas/webhook.py tests/test_reminder_r6_buttons.py tests/test_reminder_v2_text.py tests/test_reminder_r3_ids.py
git commit -F - <<'EOF'
feat(reminders): the third button is Alterar Dados, id remedit (TASK-032 R6)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

(Add any other test file you changed in Step 4 to `git add`.)

---

## Task 5: Confirmar shows the returning-patient menu; Cancelar asks "Tem certeza?" directly

**Files:**
- Modify: `src/secretaria/workers/shared/reminder_actions.py`
- Modify: `src/secretaria/workers/shared/actions.py` (remove the "Agendar Outra" branch)
- Modify: `src/secretaria/services/reminder_text.py` (remove the three-way builder and its labels)
- Modify tests that pin the removed behavior: `tests/test_reminder_r3_cancel_path.py`, `tests/test_reminder_r3_continuations.py`, `tests/test_reminder_r3_ids.py`, `tests/test_reminder_v2_buttons.py`, `tests/test_reminder_r3_portal.py`
- Test: `tests/test_reminder_r6_actions.py`

**Interfaces:**
- Consumes: Task 4's `ACTION_EDIT`, `LEGACY_REMINDER_ACTIONS`; `flow_router.menu_label`, `flow_router.main_menu_buttons`.
- Produces: `reminder_actions.handle_reminder_button` with the new semantics (below); `_send_recurring_menu(client, patient_ref, tenant)`; **interim** behavior: `remedit` (and the legacy `remother` / `remresched` / `remnew`) records the answer `other` and hands the conversation to the AI exactly as "Outro" did — Task 9 replaces that single branch with the edit menu.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reminder_r6_actions.py`:

```python
"""Confirmar + the recurring menu, Cancelar straight to the question (TASK-032 R6, spec §5.1)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from secretaria.models import AppointmentStatus, Conversation, FlowState, Patient, PixDeposit, Tenant
from secretaria.services.flow_router import main_menu_buttons, menu_label
from secretaria.services.reminder_text import give_up_confirm_buttons
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


async def test_confirm_sends_the_confirmation_then_the_recurring_menu(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world, "remconfirm", rid)

    assert [item[0] for item in sent()] == ["text", "buttons"]
    assert sent()[0][2] == ra.CONFIRMED_TEXT.format(when=_when(world.start_at))
    _kind, _to, body, buttons = sent()[1]
    assert body == menu_label(world.tenant)
    assert [label for _id, label in buttons] == main_menu_buttons()
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 1


async def test_a_confirmation_that_was_not_recorded_shows_no_menu(db):  # noqa: F811
    world = await seed_world(db, start_at=_future(), status=AppointmentStatus.CANCELLED)
    rid = await add_reminder(db, world)

    await _tap(world, "remconfirm", rid)

    assert [item[0] for item in sent()] == ["text"]
    assert sent()[0][2] == ra.NOT_ACTIVE_TEXT


async def test_cancel_goes_straight_to_the_confirmation_question(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world, "remcancel", rid)

    [(kind, _to, body, buttons)] = sent()
    assert kind == "buttons"
    assert body == ra.GIVE_UP_CONFIRM_TEXT.format(when=_when(world.start_at))
    assert buttons == give_up_confirm_buttons(rid)
    assert (await get_reminder(db, rid)).answer == "cancel"
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED


async def test_cancel_inside_the_refund_window_warns_about_the_deposit_first(db):  # noqa: F811
    world = await seed_world(db, start_at=_future(hours=5))
    await seed_paid_deposit(db, world)
    rid = await add_reminder(db, world, kind="hour")

    await _tap(world, "remcancel", rid)

    async with db() as session:
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == world.appointment.id)
        )
        tenant = await session.get(Tenant, world.tenant.id)
    [(_kind, _to, body, _buttons)] = sent()
    warning = _pix_retention_warning_line(tenant, deposit)
    assert body == f"{warning} {ra.GIVE_UP_CONFIRM_TEXT.format(when=_when(world.start_at))}"


async def test_legacy_ids_still_resolve(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world, "remgiveup", rid)  # today's "Cancelar"
    assert [item[0] for item in sent()] == ["buttons"]
    assert sent()[0][2] == ra.GIVE_UP_CONFIRM_TEXT.format(when=_when(world.start_at))

    for action in ("remother", "remresched", "remnew"):  # interim: the AI, until Task 9
        await _tap(world, action, rid)
    texts = [item[2] for item in sent() if item[0] == "text"]
    assert texts == [ra.OTHER_TEXT] * 3
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED


async def test_alterar_dados_is_recorded_and_hands_to_the_ai_until_the_edit_flow_lands(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world, "remedit", rid)

    assert [item[2] for item in sent() if item[0] == "text"] == [ra.OTHER_TEXT]
    assert (await get_conversation(db, world)).flow_state == FlowState.LLM
    assert (await get_reminder(db, rid)).answer == "other"


async def test_new_ids_are_checked_against_the_patient_and_the_version(db):  # noqa: F811
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

    for action in ("remcancel", "remedit"):
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
    await _tap(world, "remcancel", old)

    texts = [item[2] for item in sent() if item[0] == "text"]
    assert texts == [ra.NOT_FOUND_TEXT] * 2 + [ra.MOVED_TEXT.format(when=_when(world.start_at))]
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED
    assert (await get_conversation(db, world)).flow_state == FlowState.IDLE
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_r6_actions.py -q`
Expected: FAIL — `remconfirm` sends one text (no menu), `remcancel` still sends the three-way card, `remedit` is not a known action in the handler.

- [ ] **Step 3: Rework `reminder_actions.py`**

(a) Imports: in the `from secretaria.services.reminder_text import (...)` block remove `cancel_path_buttons`, add `ACTION_EDIT`; change `from secretaria.services.flow_router import FlowRouterResult` to `from secretaria.services.flow_router import FlowRouterResult, main_menu_buttons, menu_label`.

(b) Constants: delete `CANCEL_PATH_TEXT`, `BOOK_ANOTHER_INTRO`, `CONTINUE_RESCHEDULE` and `CONTINUE_BOOK_ANOTHER` (and fix the comment above `CONTINUE_CANCEL_AND_ASK_WHY` to say that "reminder_cancel_and_ask_why" is the only continuation R6 keeps; Task 9 adds `CONTINUE_EDIT`). Update the module docstring's R3 block to the R6 flow:

```
R6 (spec 2026-10-07, "Alterar Dados"), every step on the same row id:
    Confirmar     -> "Presença confirmada" + the returning-patient menu (Agendar / Outro)
    Cancelar      -> "Tem certeza?" (Pix retention line inside the refund window)
                     -> Sim, cancelar -> cancel + "por quê?" / Manter consulta
    Alterar Dados -> the edit flow (services/appointment_edit_flow.py)
The R3 ids ("remother", "remresched", "remnew", "remgiveup") still resolve for cards
already on screen: "remgiveup" is "Cancelar"; the other three are "Alterar Dados".
```

(c) Add, below `_also_scheduled`:

```python
_EDIT_ACTIONS = (ACTION_EDIT, ACTION_OTHER, ACTION_RESCHEDULE_THIS, ACTION_BOOK_ANOTHER)


async def _send_recurring_menu(client, patient_ref: str, tenant: Tenant) -> None:
    """The returning-patient menu (the greeting card: Agendar / Outro) after a confirmation."""
    await client.send_buttons(
        patient_ref,
        menu_label(tenant),
        [(f"menu|{index}", label) for index, label in enumerate(main_menu_buttons())],
    )


async def _ask_to_cancel(session, client, reply, tenant, appointment, reminder, when, now) -> None:
    """"Tem certeza?" (+ the Pix retention line inside the refund window), nothing cancelled yet."""
    body = GIVE_UP_CONFIRM_TEXT.format(when=when)
    warning = await _retention_warning(session, tenant, appointment, now)
    if warning:
        body = f"{warning} {body}"
    await client.send_buttons(reply.patient_ref, body, give_up_confirm_buttons(reminder.id))
```

(d) In `handle_reminder_button`: right after the `CONFIRMED_TEXT` send in the `ACTION_CONFIRM` branch (the `await client.send_text_message(to=reply.patient_ref, body=CONFIRMED_TEXT.format(when=when) + also)` statement, before its `return None`) add `            await _send_recurring_menu(client, reply.patient_ref, tenant)`.

Replace the whole block from `if action in (ACTION_CANCEL, ACTION_OTHER):` through the line `            return CONTINUE_BOOK_ANOTHER, str(appointment.id)` with:

```python
        if action in (ACTION_CANCEL, *_EDIT_ACTIONS):
            if reminder.answer != REMINDER_ANSWER_CONFIRM:
                # A confirmation already counted stays recorded: overwriting it
                # would let the same message count twice (R1 dedupes on it).
                reminder.answer = (
                    REMINDER_ANSWER_CANCEL if action == ACTION_CANCEL else REMINDER_ANSWER_OTHER
                )
                reminder.answered_at = now
            also = (
                await _also_scheduled(session, tenant, appointment, now)
                if from_chat and action != ACTION_CANCEL
                else ""
            )
            await session.commit()
            if action == ACTION_CANCEL:
                await _ask_to_cancel(session, client, reply, tenant, appointment, reminder, when, now)
                return None
            # Interim (Task 9 replaces this): "Alterar Dados" and the legacy "Outro" /
            # "Remarcar" / "Marcar outra" hand the conversation to the AI, as "Outro" did.
            hand_to_ai_text = OTHER_TEXT + also

        elif action == ACTION_KEEP:
            await client.send_text_message(to=reply.patient_ref, body=KEPT_TEXT.format(when=when))
            return None

        elif action == ACTION_GIVE_UP:  # the legacy "Não vou mais" = today's "Cancelar"
            await _ask_to_cancel(session, client, reply, tenant, appointment, reminder, when, now)
            return None

        elif action == ACTION_GIVE_UP_CONFIRM:
            return CONTINUE_CANCEL_AND_ASK_WHY, str(appointment.id)
```

- [ ] **Step 4: Remove the "Agendar Outra" branch and the three-way builder**

(a) `src/secretaria/workers/shared/actions.py`: delete the `if action == CONTINUE_BOOK_ANOTHER:` branch, the `book_another_handoff: tuple | None = None` declaration (and its comment), the `if book_another_handoff is not None:` application block, and from the `reminder_actions` import remove `BOOK_ANOTHER_INTRO`, `CONTINUE_BOOK_ANOTHER`, `_retention_warning`; then run `uvx ruff check --fix src/secretaria/workers/shared/actions.py` — it removes the now-unused `TextBubble` and `enter_booking` imports (verify each was only used by the removed code; keep any that are still used).

(b) `src/secretaria/services/reminder_text.py`: delete `LABEL_RESCHEDULE_THIS`, `LABEL_BOOK_ANOTHER`, `LABEL_GIVE_UP` and `cancel_path_buttons`; update the comment above the labels to "labels of the cancel confirmation card". Keep `LABEL_GIVE_UP_CONFIRM`, `LABEL_KEEP`, `give_up_confirm_buttons`.

(c) Update the R3 tests that pinned removed behavior (each change is a ledgered ruling):
- `tests/test_reminder_r3_ids.py`: delete `test_the_cancel_card_is_the_three_way_choice`; in `test_every_new_label_fits_a_whatsapp_button` keep only `rt.LABEL_GIVE_UP_CONFIRM, rt.LABEL_KEEP, rt.LABEL_EDIT`.
- `tests/test_reminder_v2_buttons.py`: replace `test_cancel_offers_the_three_way_card` with `test_cancel_asks_for_confirmation_on_this_appointment` asserting `("buttons", GIVE_UP_CONFIRM_TEXT.format(...))` and `give_up_confirm_buttons(rid)` (import both) plus the answer `cancel` / status SCHEDULED assertions the old test had.
- `tests/test_reminder_r3_cancel_path.py`: delete `test_cancel_offers_the_three_way_card`, `test_the_three_way_card_names_each_button_in_its_body`, `test_remarcar_consulta_enters_the_reschedule_of_this_appointment`, `test_remarcar_consulta_respects_the_pix_reschedule_limit` (their behavior moved: Tasks 7–9 cover the edit flow) and in `test_cancel_path_taps_are_checked_against_the_patient_and_the_version` keep the loop actions `("remgiveup", "remgiveupyes", "remkeep")` and the expected `[NOT_FOUND_TEXT] * 3 + [MOVED_TEXT...]`; remove now-unused imports.
- `tests/test_reminder_r3_continuations.py`: delete the two `remnew` tests (`test_book_another_opens_a_booking_that_replaces_this_one`, `test_book_another_inside_the_refund_window_warns_about_the_deposit`) and unused imports; keep `test_yes_cancel_cancels_closes_the_reminders_and_asks_why`.
- `tests/test_reminder_r3_portal.py`: in `test_a_portal_tap_on_the_cancel_path_reaches_its_step` replace the second tap (`remgiveup|`, "Cancelar Consulta") by nothing and tap `remkeep|` right after `remcancel|` (the "Tem certeza?" card carries `remkeep|<id>`): the expected last body stays `ra.KEPT_TEXT.format(when=when)`.

- [ ] **Step 5: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_r6_actions.py tests/test_reminder_r6_buttons.py tests/test_reminder_r3_ids.py tests/test_reminder_r3_cancel_path.py tests/test_reminder_r3_continuations.py tests/test_reminder_r3_portal.py tests/test_reminder_v2_buttons.py tests/test_reminder_v2_text.py tests/test_reminder_v2_wiring.py tests/test_action_buttons.py tests/test_reminder_opening_turn.py tests/test_workers_layering.py -q`
Expected: all pass. Any other test failing on `cancel_path_buttons`, `CANCEL_PATH_TEXT`, `BOOK_ANOTHER_INTRO` or `CONTINUE_BOOK_ANOTHER` pinned removed R3 behavior: update it the same way and ledger it.

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff format tests/test_reminder_r6_actions.py
uvx ruff check --fix tests/test_reminder_r6_actions.py
uvx ruff check src/secretaria/workers/shared/reminder_actions.py src/secretaria/workers/shared/actions.py src/secretaria/services/reminder_text.py
git diff --stat
git add src/secretaria/workers/shared/reminder_actions.py src/secretaria/workers/shared/actions.py src/secretaria/services/reminder_text.py tests/test_reminder_r6_actions.py tests/test_reminder_r3_ids.py tests/test_reminder_r3_cancel_path.py tests/test_reminder_r3_continuations.py tests/test_reminder_r3_portal.py tests/test_reminder_v2_buttons.py
git commit -F - <<'EOF'
feat(reminders): Confirmar shows the returning-patient menu, Cancelar asks first (TASK-032 R6)

Removes the three-way "Cancelar" card and "Agendar Outra"; the R3 ids still resolve.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 6: The building blocks — calendar methods, the day picker's anchor, the edit branch

**Files:**
- Modify: `src/secretaria/services/calendar.py` (`update_event_details`, `is_slot_free`)
- Modify: `src/secretaria/services/flow_router.py` (`STEP_EDIT_*`, `EDIT_DAY_BRANCH`, `_EDIT_DAY_STEPS`, `_DayPickerState.flow_edit_draft`, `_day_branch_fields`, `enter_day_picker(anchor=)`, `_carry_edit_draft`, `_preserve`)
- Test: `tests/test_calendar_edit_methods.py`, `tests/test_edit_day_picker.py`

**Interfaces:**
- Consumes: `CalendarService.update_event`'s patch code, `_windows_for_day`, `_busy_ranges`, `check_availability`; the day-picker machinery (`DayBranch`, `enter_day_picker`, `_day_branch_fields`).
- Produces: `CalendarService.update_event_details(event_id, start, end, summary, description="") -> dict`; `CalendarService.is_slot_free(start, end, *, ignore_event_id=None) -> bool`; the `STEP_EDIT_*` constants (see "Interfaces"), `EDIT_DAY_BRANCH`, `enter_day_picker(..., anchor: datetime | None = None)`, `_carry_edit_draft(conversation, result)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_calendar_edit_methods.py`:

```python
"""CalendarService.update_event_details / is_slot_free (TASK-032 R6, spec §5.6)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import datetime, timedelta  # noqa: E402

from secretaria.services.calendar import CalendarService  # noqa: E402


class _Executable:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _Events:
    def __init__(self, calls):
        self._calls = calls

    def patch(self, calendarId, eventId, body):  # noqa: N803 - Google's keyword names
        self._calls.append((calendarId, eventId, body))
        return _Executable({"id": eventId})


class _Client:
    def __init__(self):
        self.calls: list[tuple] = []

    def events(self):
        return _Events(self.calls)


def _service(client=None) -> CalendarService:
    service = CalendarService()
    service._calendar_id = "cal-1"
    service._business_hours = {}  # fallback window: 08:00-18:00 every day
    if client is not None:
        service._client = lambda: client  # type: ignore[method-assign]
    return service


def _tomorrow_at(service, hour: int, minute: int = 0) -> datetime:
    day = datetime.now(service.tzinfo) + timedelta(days=2)
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


async def test_update_event_details_patches_title_description_and_window():
    client = _Client()
    service = _service(client)
    start = _tomorrow_at(service, 10)
    end = start + timedelta(minutes=40)

    event = await service.update_event_details(
        "evt-1", start, end, "Retorno - Maria", "Serviço: Retorno\nConvênio: Unimed"
    )

    assert event == {"id": "evt-1"}
    [(calendar_id, event_id, body)] = client.calls
    assert (calendar_id, event_id) == ("cal-1", "evt-1")
    assert body["summary"] == "Retorno - Maria"
    assert body["description"] == "Serviço: Retorno\nConvênio: Unimed"
    assert body["start"]["dateTime"] == start.isoformat()
    assert body["end"]["dateTime"] == end.isoformat()


async def test_update_event_keeps_patching_only_the_window():
    client = _Client()
    service = _service(client)
    start = _tomorrow_at(service, 9)

    await service.update_event("evt-1", start, start + timedelta(minutes=30))

    [(_c, _e, body)] = client.calls
    assert set(body) == {"start", "end"}


async def _free(service, events, start, minutes=40, ignore=None) -> bool:
    async def _events(window_start, window_end, max_results=0):
        return events

    service.check_availability = _events  # type: ignore[method-assign]
    return await service.is_slot_free(
        start, start + timedelta(minutes=minutes), ignore_event_id=ignore
    )


async def test_a_free_future_slot_inside_the_hours_is_free():
    service = _service()
    assert await _free(service, [], _tomorrow_at(service, 10)) is True


async def test_outside_the_business_hours_or_in_the_past_is_not_free():
    service = _service()
    assert await _free(service, [], _tomorrow_at(service, 7)) is False
    assert await _free(service, [], _tomorrow_at(service, 17, 30)) is False  # ends after 18:00
    assert await _free(service, [], datetime.now(service.tzinfo) - timedelta(hours=1)) is False


async def test_a_busy_event_blocks_unless_it_is_the_one_being_edited():
    service = _service()
    start = _tomorrow_at(service, 10)
    busy = [
        {
            "id": "evt-mine",
            "start": (start - timedelta(minutes=10)).isoformat(),
            "end": (start + timedelta(minutes=30)).isoformat(),
        }
    ]
    assert await _free(service, busy, start) is False
    assert await _free(service, busy, start, ignore="evt-mine") is True
    other = [{**busy[0], "id": "evt-other"}]
    assert await _free(service, other, start, ignore="evt-mine") is False
```

Create `tests/test_edit_day_picker.py`:

```python
"""The day picker's anchor, the edit branch and the draft carry (TASK-032 R6, spec §5.4)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from tests.test_flow_router import _FakeCalendar, _conversation, _tenant  # noqa: E402

DRAFT = {"appointment_id": "a", "current": {}, "original": {}, "stage": {}}


def _carrier(draft=DRAFT):
    return fr._DayPickerState(flow_managing_appointment_id=uuid4(), flow_edit_draft=draft)


async def test_with_an_anchor_the_nearest_days_come_first_and_are_shown_in_order():
    days = [datetime(2026, 10, d) for d in (9, 10, 12, 14, 15, 16, 17, 19, 20, 21, 22, 23)]
    calendar = _FakeCalendar(days=days)

    result = await fr.enter_day_picker(
        _carrier(),
        _tenant(),
        calendar,
        duration_minutes=40,
        branch=fr.EDIT_DAY_BRANCH,
        anchor=datetime(2026, 10, 16, 15, 20),
    )

    rows = result.bubbles[0].rows
    day_ids = [row[0] for row in rows if row[0].startswith(fr.ROW_DAY_PREFIX)]
    assert day_ids == [f"day|2026-10-{d}|0" for d in (12, 14, 15, 16, 17, 19, 20, 21)]
    assert any(row[1] == fr.LABEL_MORE_DAYS for row in rows)  # 12 days, 8 shown


async def test_without_an_anchor_the_picker_is_unchanged():
    days = [datetime(2026, 10, d) for d in (9, 10, 12, 14)]
    result = await fr.enter_day_picker(
        _conversation(), _tenant(), _FakeCalendar(days=days), duration_minutes=40
    )
    day_ids = [row[0] for row in result.bubbles[0].rows if row[0].startswith(fr.ROW_DAY_PREFIX)]
    assert day_ids == [f"day|2026-10-{d:02d}|0" for d in (9, 10, 12, 14)]


async def test_a_far_anchor_moves_the_scan_window_to_it():
    calendar = _FakeCalendar()
    anchor = datetime.now() + timedelta(days=60)

    await fr.enter_day_picker(
        _carrier(),
        _tenant(),
        calendar,
        duration_minutes=40,
        branch=fr.EDIT_DAY_BRANCH,
        anchor=anchor,
    )

    start_day = calendar.day_scans[0][0]
    assert start_day.astimezone(calendar.tzinfo).date() == (anchor - timedelta(days=10)).date()


async def test_the_edit_branch_carries_the_appointment_and_the_draft():
    result = await fr.enter_day_picker(
        _carrier(),
        _tenant(),
        _FakeCalendar(),
        duration_minutes=40,
        branch=fr.EDIT_DAY_BRANCH,
    )
    assert result.flow_state == FlowState.EDIT_BOOKING
    assert result.flow_step == fr.STEP_EDIT_DAY
    assert result.flow_edit_draft == DRAFT
    assert result.flow_managing_appointment_id is not None


def _in_edit(**kw):
    return _conversation(
        flow_state=FlowState.EDIT_BOOKING, flow_edit_draft={"appointment_id": "x"}, **kw
    )


def test_the_draft_is_carried_only_while_the_edit_continues():
    stays = fr.FlowRouterResult(action="reply", flow_state=FlowState.EDIT_BOOKING)
    assert fr._carry_edit_draft(_in_edit(), stays).flow_edit_draft == {"appointment_id": "x"}

    leaves = fr.FlowRouterResult(action="reply", flow_state=FlowState.MENU)
    assert fr._carry_edit_draft(_in_edit(), leaves).flow_edit_draft is None

    named = fr.FlowRouterResult(
        action="reply", flow_state=FlowState.EDIT_BOOKING, flow_edit_draft={"appointment_id": "y"}
    )
    assert fr._carry_edit_draft(_in_edit(), named).flow_edit_draft == {"appointment_id": "y"}

    idle = _conversation(flow_state=FlowState.IDLE, flow_edit_draft={"appointment_id": "stale"})
    into = fr.FlowRouterResult(action="reply", flow_state=FlowState.EDIT_BOOKING)
    assert fr._carry_edit_draft(idle, into).flow_edit_draft is None


def test_an_ai_detour_inside_the_edit_keeps_the_draft():
    kept = fr._preserve(_in_edit(flow_step=fr.STEP_EDIT_CONFIRM), "delegate_llm")
    assert kept.flow_edit_draft == {"appointment_id": "x"}
    assert kept.flow_state == FlowState.EDIT_BOOKING and kept.flow_step == fr.STEP_EDIT_CONFIRM


def test_every_edit_step_fits_the_flow_step_column():
    steps = [value for name, value in vars(fr).items() if name.startswith("STEP_EDIT_")]
    assert len(steps) == 15
    assert all(len(step) <= 32 for step in steps)
    assert SimpleNamespace(steps=steps).steps  # non-empty
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_calendar_edit_methods.py tests/test_edit_day_picker.py -q`
Expected: FAIL — `AttributeError: ... 'update_event_details'` / `'is_slot_free'`, `EDIT_DAY_BRANCH`, `anchor`, `_carry_edit_draft`.

- [ ] **Step 3: The calendar methods**

In `src/secretaria/services/calendar.py`, replace the whole `update_event` method with these three (the patch call moves into `_patch_event`, unchanged):

```python
    async def update_event(
        self,
        event_id: str,
        start: datetime,
        end: datetime,
    ) -> dict:
        """Move an existing event to a new [start, end) window. Returns the updated event."""
        return await self._patch_event(event_id, self._window_body(start, end))

    async def update_event_details(
        self,
        event_id: str,
        start: datetime,
        end: datetime,
        summary: str,
        description: str = "",
    ) -> dict:
        """Move an event AND rewrite its title and description (TASK-032 R6, "Alterar Dados").

        The same single patch `update_event` does, plus the two fields that carry the
        service, the convênio and the attendee - so editing any of them leaves the doctor's
        agenda telling the truth.
        """
        body = self._window_body(start, end)
        body["summary"] = summary
        body["description"] = description
        return await self._patch_event(event_id, body)

    def _window_body(self, start: datetime, end: datetime) -> dict:
        tz_name = str(self._tz)
        return {
            "start": {"dateTime": self._ensure_tz(start).isoformat(), "timeZone": tz_name},
            "end": {"dateTime": self._ensure_tz(end).isoformat(), "timeZone": tz_name},
        }

    async def _patch_event(self, event_id: str, body: dict) -> dict:
        calendar_id = self._calendar_id

        def _patch() -> dict:
            try:
                return (
                    self._client()
                    .events()
                    .patch(calendarId=calendar_id, eventId=event_id, body=body)
                    .execute()
                )
            except HttpError as exc:
                logger.error("calendar_patch_http_error", error=str(exc))
                _raise_if_unavailable(exc)
                raise
            except _NETWORK_ERRORS as exc:
                logger.error("calendar_patch_network_error", error=str(exc))
                raise CalendarUnavailableError("Google Calendar unreachable") from exc

        event = await asyncio.to_thread(_patch)
        logger.info("calendar_event_updated", event_id=event_id)
        return event

    async def is_slot_free(
        self, start: datetime, end: datetime, *, ignore_event_id: str | None = None
    ) -> bool:
        """Whether [start, end) is bookable: in the future, inside the hours, nothing overlapping.

        Unlike `list_free_slots` this does NOT require `start` to sit on the service-length
        grid: an existing appointment keeps its time when only its doctor or service changes,
        and that time may be off the new grid. `ignore_event_id` names the event being
        edited, so an appointment never "conflicts" with itself. Raises
        `CalendarUnavailableError` like the other reads.
        """
        start_dt, end_dt = self._ensure_tz(start), self._ensure_tz(end)
        if start_dt <= datetime.now(self._tz):
            return False
        windows = self._windows_for_day(start_dt, 8, 18)
        if not any(window_start <= start_dt and end_dt <= window_end for window_start, window_end in windows):
            return False
        events = await self.check_availability(start_dt, end_dt)
        busy = self._busy_ranges([ev for ev in events if ev.get("id") != ignore_event_id])
        return not any(busy_start < end_dt and busy_end > start_dt for busy_start, busy_end in busy)
```

- [ ] **Step 4: The router building blocks**

In `src/secretaria/services/flow_router.py`:

(a) Import `EditContext` for later tasks' signatures: add `from secretaria.services.appointment_edit import EditContext` (placed with the other `secretaria.services` imports, alphabetically).

(b) After `STEP_MANAGE_PICK_CANCEL = "manage_pick_cancel"` add:

```python
# TASK-032 R6 ("Alterar Dados"): the steps of FlowState.EDIT_BOOKING
# (services/appointment_edit_flow.py). The day/slot steps belong to EDIT_DAY_BRANCH.
STEP_EDIT_MENU = "edit_menu"
STEP_EDIT_MORE = "edit_more"
STEP_EDIT_DAY = "edit_day"
STEP_EDIT_DAY_RETRY = "edit_day_retry"
STEP_EDIT_DAY_ESCAPE = "edit_day_escape"
STEP_EDIT_SLOT = "edit_slot"
STEP_EDIT_TIME_TOO = "edit_time_too"
STEP_EDIT_SERVICE = "edit_service"
STEP_EDIT_DOCTOR = "edit_doctor"
STEP_EDIT_INSURANCE = "edit_insurance"
STEP_EDIT_INSURANCE_OTHER = "edit_insurance_other"
STEP_EDIT_ATT_CHOICE = "edit_att_choice"
STEP_EDIT_ATT_NAME = "edit_att_name"
STEP_EDIT_ATT_AUTH = "edit_att_auth"
STEP_EDIT_CONFIRM = "edit_confirm"
```

(c) After `MANAGE_DAY_BRANCH = DayBranch(...)` add:

```python
EDIT_DAY_BRANCH = DayBranch(
    flow_state=FlowState.EDIT_BOOKING,
    day_step=STEP_EDIT_DAY,
    day_retry_step=STEP_EDIT_DAY_RETRY,
    day_escape_step=STEP_EDIT_DAY_ESCAPE,
    slot_step=STEP_EDIT_SLOT,
    day_body="Qual o novo dia da consulta?",
    slot_body_prefix="Horários livres em",
)
```

and after `_MANAGE_DAY_STEPS = (...)` add `_EDIT_DAY_STEPS = (STEP_EDIT_DAY, STEP_EDIT_DAY_RETRY, STEP_EDIT_DAY_ESCAPE)`.

(d) In `_DayPickerState` add the field `flow_edit_draft: dict | None = None` (after `flow_attendee_name`).

(e) In `_day_branch_fields`, before the `if branch.flow_state is FlowState.MANAGE_BOOKING:` block add:

```python
    if branch.flow_state is FlowState.EDIT_BOOKING:
        return {
            "flow_managing_appointment_id": _selected_managing_appointment_id(conversation),
            "flow_selected_professional_id": _selected_professional_id(conversation),
            "flow_edit_draft": getattr(conversation, "flow_edit_draft", None),
        }
```

(f) `enter_day_picker`: add the keyword `anchor: datetime | None = None` after `professionals`, document it in the docstring ("`anchor` - the appointment's current day when the patient is CHANGING the date (TASK-032 R6): the scan window is centred on it and the days nearest to it are the ones listed, shown in calendar order"), and replace

```python
        days = await available_day_starts(
            calendar,
            start=datetime.now(calendar.tzinfo),
            window_days=DAY_PICKER_WINDOW_DAYS,
            duration_minutes=duration_minutes,
        )
```

with

```python
        start = datetime.now(calendar.tzinfo)
        if anchor is not None:
            anchor_aware = anchor if anchor.tzinfo else anchor.replace(tzinfo=calendar.tzinfo)
            start = max(start, anchor_aware - timedelta(days=DAY_PICKER_WINDOW_DAYS // 2))
        days = await available_day_starts(
            calendar,
            start=start,
            window_days=DAY_PICKER_WINDOW_DAYS,
            duration_minutes=duration_minutes,
        )
        if anchor is not None:
            # The days NEAREST the current one come first (ties: the earlier day), so
            # pagination ("Ver mais dias") walks outwards; the page itself is shown in
            # calendar order.
            anchor_day = anchor.date()
            days = sorted(days, key=lambda day: (abs((day.date() - anchor_day).days), day))
```

and after `shown = days[offset : offset + DAY_PICKER_PAGE_SIZE]` add `    if anchor is not None:\n        shown = sorted(shown)`.

(g) After `_carry_replacement` / `_replacement_line` and before `_carry_booking`, add:

```python
def _carry_edit_draft(conversation: Conversation, result: FlowRouterResult) -> FlowRouterResult:
    """Keep the "Alterar Dados" draft while the edit continues (TASK-032 R6).

    `_apply_flow_result` writes `flow_edit_draft` unconditionally, so a result that does
    not name it clears it - exactly right for a result that LEAVES the edit (menu, idle,
    the AI). A result that stays in `EDIT_BOOKING` without naming a draft (a scoped-help
    or day-picker result built without it) keeps the conversation's own.
    """
    if result.flow_edit_draft is not None:
        return result
    if (
        getattr(conversation, "flow_state", None) == FlowState.EDIT_BOOKING
        and result.flow_state == FlowState.EDIT_BOOKING
    ):
        result.flow_edit_draft = getattr(conversation, "flow_edit_draft", None)
    return result
```

wrap it into `_carry_booking`: the body becomes `return _carry_edit_draft(conversation, _carry_replacement(conversation, _carry_draft(...)))` (keep the existing inner expression byte for byte).

(h) In `_preserve`, add to the `FlowRouterResult(` call the argument `flow_edit_draft=getattr(conversation, "flow_edit_draft", None),`; read the function and confirm it also carries `flow_managing_appointment_id` and `flow_selected_professional_id` (it does today; add them if missing).

- [ ] **Step 5: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_calendar_edit_methods.py tests/test_edit_day_picker.py tests/test_flow_router.py tests/test_appointment_replacement_router.py tests/test_attendee_booking.py -q`
Expected: all pass (the existing router suites prove the picker without an anchor did not move).

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff format tests/test_calendar_edit_methods.py tests/test_edit_day_picker.py
uvx ruff check --fix tests/test_calendar_edit_methods.py tests/test_edit_day_picker.py
uvx ruff check src/secretaria/services/calendar.py src/secretaria/services/flow_router.py
git diff --stat
git add src/secretaria/services/calendar.py src/secretaria/services/flow_router.py tests/test_calendar_edit_methods.py tests/test_edit_day_picker.py
git commit -F - <<'EOF'
feat(edit): calendar details/slot check, anchored day picker and the edit branch (TASK-032 R6)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 7: The edit flow, part 1 — menu, date, time and the complete confirmation

**Files:**
- Create: `tests/_edit_flow_support.py`
- Create: `src/secretaria/services/appointment_edit_flow.py`
- Modify: `src/secretaria/services/flow_router.py` (`route` / `_route`: `edit_context`, the dispatch)
- Test: `tests/test_appointment_edit_flow_menu.py`

**Interfaces:**
- Consumes: Tasks 2 and 6 (`EditDraft`, `EditContext`, labels, `build_edit_recap`, `EDIT_DAY_BRANCH`, `enter_day_picker(anchor=)`, `_carry_edit_draft`, `CalendarService.is_slot_free`), plus these `flow_router` helpers: `_enter_slot_picker`, `_handle_day_step`, `_handle_slot_controls`, `_day_from_body`, `_parse_day`, `_slot_iso_from_body`, `_label_match`, `_find_appt_by_id`, `_managing_appt_id_str`, `_find_professional_by_id`, `_menu_bubbles`, `_preserve`, `_match_service`, `_DayPickerState`, `MenuBubble`.
- Produces: `appointment_edit_flow.enter_edit_menu(tenant, appt, ctx=None)`, `appointment_edit_flow.edit_step(conversation, tenant, body, appointments, professionals, ctx, patient_name=None)`; the internal helpers later tasks extend: `_result`, `_leave`, `_carrier`, `_menu_result`, `_more_result`, `_confirm_result`, `_after_slot_affecting_change`, `_slot_free`, `_iso`, `_doctor`, `_requirements`.

- [ ] **Step 1: The shared test support**

Create `tests/_edit_flow_support.py`:

```python
"""Shared fakes for the TASK-032 R6 edit-flow tests (router level, no database)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from secretaria.models import FlowState
from secretaria.services import appointment_edit as ae
from secretaria.services.calendar import CalendarUnavailableError
from tests.test_flow_router import _FakeCalendar

TZ = ZoneInfo("America/Sao_Paulo")
DOCTOR_A = uuid4()
DOCTOR_B = uuid4()
# Tuesday 2030-10-15, 15:20 in São Paulo (UTC-3).
START_UTC = datetime(2030, 10, 15, 18, 20, tzinfo=UTC)


class EditCalendar(_FakeCalendar):
    """The router suite's fake agenda plus the two methods the edit flow needs."""

    def __init__(self, *, free: bool = True, **kwargs):
        super().__init__(**kwargs)
        self.free = free
        self.checked: list[tuple] = []
        self.detail_updates: list[tuple] = []

    async def is_slot_free(self, start, end, *, ignore_event_id=None):
        if self._unavailable:
            raise CalendarUnavailableError("down")
        self.checked.append((start, end, ignore_event_id))
        return self.free

    async def update_event_details(self, event_id, start, end, summary, description=""):
        if self._unavailable:
            raise CalendarUnavailableError("down")
        self.detail_updates.append((event_id, start, end, summary, description))
        return {"id": event_id}


def slots(*hours: str, day: str = "2030-10-15") -> list[dict]:
    return [
        {"start": f"{day}T{hour}", "end": f"{day}T{hour}", "label": hour} for hour in hours
    ]


def tenant(**kwargs):
    base = dict(
        initial_flows={},
        appointment_types=[{"name": "Consulta", "duration_min": 40, "is_active": True}],
        appointment_duration_min=40,
        business_hours={},
        timezone="America/Sao_Paulo",
        address="Rua das Flores, 10",
        clinic_name="Clínica Olhar",
        insurance_plans=[{"id": "p1", "name": "Unimed"}, {"id": "p2", "name": "Amil"}],
        insurance_mode="clinic_with_exceptions",
        insurance_accepted_by={str(DOCTOR_A): ["p1"], str(DOCTOR_B): ["p1", "p2"]},
        collect_insurance=True,
        pix_reschedule_limit=2,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def professionals():
    return [
        SimpleNamespace(
            id=DOCTOR_A,
            name="Dr. Diogo Raposo",
            specialty="Cardiologia",
            about=None,
            context_doctor_message=None,
            appointment_types=[
                {
                    "name": "Consulta",
                    "duration_min": 40,
                    "is_active": True,
                    "requirements": ["Trazer exames anteriores"],
                },
                {"name": "Retorno", "duration_min": 20, "is_active": True},
            ],
            business_hours=None,
        ),
        SimpleNamespace(
            id=DOCTOR_B,
            name="Dra. Ana Lima",
            specialty="Oftalmologia",
            about=None,
            context_doctor_message=None,
            appointment_types=[{"name": "Consulta", "duration_min": 30, "is_active": True}],
            business_hours=None,
        ),
    ]


def appointment(**kwargs) -> dict:
    base = {
        "id": str(uuid4()),
        "google_event_id": "evt-old",
        "appointment_type": "Consulta",
        "start_at": START_UTC,
        "end_at": START_UTC + timedelta(minutes=40),
        "professional_id": str(DOCTOR_A),
        "insurance": "Unimed",
        "attendee_name": None,
    }
    base.update(kwargs)
    return base


def draft_for(appt: dict | None = None) -> ae.EditDraft:
    return ae.EditDraft.from_appointment(appt or appointment(), TZ)


def conversation(draft: ae.EditDraft, step: str, **kwargs):
    base = dict(
        id=uuid4(),
        flow_state=FlowState.EDIT_BOOKING,
        flow_step=step,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_selected_professional_id=(
            UUID(draft.current["professional_id"]) if draft.current["professional_id"] else None
        ),
        flow_selected_insurance=None,
        flow_managing_appointment_id=UUID(draft.appointment_id),
        flow_attendee_name=None,
        flow_draft=None,
        flow_replaces_appointment_id=None,
        flow_edit_draft=draft.to_json(),
        patient_id=uuid4(),
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def context(calendar=None, *, calendars=None, **kwargs) -> ae.EditContext:
    cals = calendars if calendars is not None else {str(DOCTOR_A): calendar or EditCalendar()}
    return ae.EditContext(calendars=cals, **kwargs)
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_appointment_edit_flow_menu.py`:

```python
"""The edit flow: menu, date, time and the complete confirmation (TASK-032 R6, spec §5.3-5.5)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import datetime  # noqa: E402
from uuid import UUID  # noqa: E402

from secretaria.ai.formatter import SlotsBubble, TextBubble  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services import appointment_edit as ae  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.appointment_edit_flow import edit_step, enter_edit_menu  # noqa: E402
from tests._edit_flow_support import (  # noqa: E402
    EditCalendar,
    appointment,
    context,
    conversation,
    draft_for,
    professionals,
    slots,
    tenant,
)


def _labels(result) -> list[str]:
    return result.bubbles[0].labels


async def _step(step, body, *, draft=None, calendar=None, ctx=None, appt=None, **kw):
    appt = appt or appointment()
    draft = draft or draft_for(appt)
    conv = conversation(draft, step, **kw)
    return await edit_step(
        conv,
        tenant(),
        body,
        [appt],
        professionals(),
        ctx or context(calendar),
    )


def test_the_menu_has_five_buttons_and_the_alterar_dados_description():
    appt = appointment()
    result = enter_edit_menu(tenant(), appt, ae.EditContext())

    assert result.flow_state == FlowState.EDIT_BOOKING
    assert result.flow_step == fr.STEP_EDIT_MENU
    assert result.flow_managing_appointment_id == UUID(appt["id"])
    assert result.flow_edit_draft["appointment_id"] == appt["id"]
    assert _labels(result) == [
        ae.LABEL_EDIT_DATE,
        ae.LABEL_EDIT_TIME,
        ae.LABEL_EDIT_SERVICE,
        ae.LABEL_EDIT_DOCTOR,
        ae.LABEL_EDIT_MORE,
    ]
    assert result.bubbles[0].body.startswith("*Alterar Dados*")


def test_a_paid_deposit_hides_service_doctor_and_insurance():
    result = enter_edit_menu(tenant(), appointment(), ae.EditContext(paid_deposit=True))
    assert _labels(result) == [ae.LABEL_EDIT_DATE, ae.LABEL_EDIT_TIME, ae.LABEL_EDIT_MORE]
    assert ae.EDIT_PIX_NOTICE in result.bubbles[0].body


async def test_a_paid_deposit_also_hides_the_insurance_in_the_second_menu():
    ctx = context(paid_deposit=True)
    more = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_MORE, ctx=ctx)
    assert _labels(more) == [ae.LABEL_EDIT_PATIENT, ae.LABEL_EDIT_BACK]


def test_a_reached_reschedule_limit_hides_date_and_time():
    result = enter_edit_menu(tenant(), appointment(), ae.EditContext(reschedule_blocked=True))
    assert _labels(result) == [ae.LABEL_EDIT_SERVICE, ae.LABEL_EDIT_DOCTOR, ae.LABEL_EDIT_MORE]
    assert ae.EDIT_LIMIT_NOTICE in result.bubbles[0].body


async def test_a_hidden_option_typed_by_hand_is_not_honoured():
    ctx = context(reschedule_blocked=True)
    result = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_DATE, ctx=ctx)
    assert result.action == "delegate_llm"


async def test_outro_opens_the_second_menu_and_voltar_comes_back():
    more = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_MORE)
    assert more.flow_step == fr.STEP_EDIT_MORE
    assert _labels(more) == [ae.LABEL_EDIT_INSURANCE, ae.LABEL_EDIT_PATIENT, ae.LABEL_EDIT_BACK]

    back = await _step(
        fr.STEP_EDIT_MORE, ae.LABEL_EDIT_BACK, draft=ae.EditDraft.from_json(more.flow_edit_draft)
    )
    assert back.flow_step == fr.STEP_EDIT_MENU


async def test_mudar_horario_lists_the_free_times_of_the_current_day():
    calendar = EditCalendar(slots=slots("14:00", "16:00"))

    result = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_TIME, calendar=calendar)

    assert result.flow_step == fr.STEP_EDIT_SLOT
    assert result.flow_selected_day == "2030-10-15"
    bubble = result.bubbles[0]
    assert isinstance(bubble, SlotsBubble)
    assert [row[0] for row in bubble.rows if row[0].startswith("slot|")] == [
        "slot|2030-10-15T14:00",
        "slot|2030-10-15T16:00",
    ]
    assert result.flow_edit_draft["appointment_id"]


async def test_a_slot_tap_shows_the_complete_confirmation_and_changes_nothing():
    calendar = EditCalendar()
    appt = appointment()

    result = await _step(
        fr.STEP_EDIT_SLOT,
        "16:00 (2030-10-15T16:00)",
        draft=draft_for(appt).with_stage(mode="time"),
        calendar=calendar,
        appt=appt,
    )

    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    draft = ae.EditDraft.from_json(result.flow_edit_draft)
    assert draft.current["start_at"] == "2030-10-15T16:00"
    assert draft.current["end_at"] == "2030-10-15T16:40"
    bubble = result.bubbles[0]
    assert bubble.labels == [fr.LABEL_CONFIRM, fr.LABEL_CANCEL, ae.LABEL_EDIT_MORE_DATA]
    assert "Horário: 16:00" in bubble.body
    assert "Médico: Dr. Diogo Raposo" in bubble.body
    assert "• Trazer exames anteriores" in bubble.body
    assert "Endereço: Rua das Flores, 10" in bubble.body
    assert bubble.body.rstrip().endswith("O que mudou: horário.")
    # Review Focus 1: a draft is only a draft.
    assert result.appointment_edit is None
    assert calendar.detail_updates == [] and calendar.created == [] and calendar.cancelled == []


async def test_mudar_data_lists_the_nearest_days_then_asks_about_the_time():
    days = [datetime(2030, 10, d) for d in (10, 12, 14, 15, 16, 17, 18, 21)]
    calendar = EditCalendar(days=days)

    picker = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_DATE, calendar=calendar)

    assert picker.flow_step == fr.STEP_EDIT_DAY
    assert ae.EditDraft.from_json(picker.flow_edit_draft).stage == {"mode": "date"}
    assert any(row[0].startswith("day|2030-10-16") for row in picker.bubbles[0].rows)

    asked = await _step(
        fr.STEP_EDIT_DAY,
        "Qua, 16/10 (2030-10-16|0)",
        draft=ae.EditDraft.from_json(picker.flow_edit_draft),
        calendar=calendar,
    )
    assert asked.flow_step == fr.STEP_EDIT_TIME_TOO
    assert asked.bubbles[0].labels == [ae.LABEL_TIME_TOO_YES, ae.LABEL_TIME_TOO_NO]
    assert ae.EditDraft.from_json(asked.flow_edit_draft).stage["target_day"] == "2030-10-16"


def _asked_draft():
    return draft_for().with_stage(mode="date", target_day="2030-10-16")


async def test_keeping_the_time_goes_straight_to_the_confirmation_when_it_is_free():
    result = await _step(
        fr.STEP_EDIT_TIME_TOO,
        ae.LABEL_TIME_TOO_NO,
        draft=_asked_draft(),
        calendar=EditCalendar(free=True),
    )
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    draft = ae.EditDraft.from_json(result.flow_edit_draft)
    assert draft.current["start_at"] == "2030-10-16T15:20"
    assert draft.changed() == ["data"]


async def test_keeping_a_busy_time_lists_that_days_slots_instead():
    calendar = EditCalendar(free=False, slots=slots("09:00", day="2030-10-16"))
    result = await _step(
        fr.STEP_EDIT_TIME_TOO, ae.LABEL_TIME_TOO_NO, draft=_asked_draft(), calendar=calendar
    )
    assert result.flow_step == fr.STEP_EDIT_SLOT
    assert isinstance(result.bubbles[0], TextBubble)
    assert result.bubbles[0].body == ae.EDIT_TIME_BUSY
    assert isinstance(result.bubbles[1], SlotsBubble)


async def test_changing_the_time_too_lists_the_slots_of_the_chosen_day():
    calendar = EditCalendar(slots=slots("09:00", "10:00", day="2030-10-16"))
    result = await _step(
        fr.STEP_EDIT_TIME_TOO, ae.LABEL_TIME_TOO_YES, draft=_asked_draft(), calendar=calendar
    )
    assert result.flow_step == fr.STEP_EDIT_SLOT
    assert result.flow_selected_day == "2030-10-16"


async def test_cancel_on_the_final_card_keeps_the_appointment_and_drops_the_draft():
    draft = draft_for().with_changes(start_at="2030-10-15T16:00", end_at="2030-10-15T16:40")
    result = await _step(fr.STEP_EDIT_CONFIRM, fr.LABEL_CANCEL, draft=draft)

    assert result.flow_state == FlowState.MENU
    assert result.flow_edit_draft is None
    assert result.appointment_edit is None
    assert result.bubbles[0].body == ae.EDIT_KEPT
    assert isinstance(result.bubbles[1], fr.MenuBubble)  # the returning-patient menu follows


async def test_alterar_mais_dados_reopens_the_menu_with_the_draft_intact():
    draft = draft_for().with_changes(start_at="2030-10-15T16:00", end_at="2030-10-15T16:40")
    result = await _step(fr.STEP_EDIT_CONFIRM, ae.LABEL_EDIT_MORE_DATA, draft=draft)

    assert result.flow_step == fr.STEP_EDIT_MENU
    assert ae.EditDraft.from_json(result.flow_edit_draft).current["start_at"] == "2030-10-15T16:00"


async def test_a_missing_appointment_is_reported_and_the_edit_closes():
    appt = appointment()
    conv = conversation(draft_for(appt), fr.STEP_EDIT_MENU)
    result = await edit_step(conv, tenant(), ae.LABEL_EDIT_DATE, [], professionals(), context())
    assert result.flow_state == FlowState.MENU
    assert result.bubbles[0].body == ae.EDIT_STALE
    assert result.flow_edit_draft is None


async def test_free_text_hands_the_turn_to_the_ai_and_keeps_the_draft():
    result = await _step(fr.STEP_EDIT_MENU, "vocês têm estacionamento?")
    assert result.action == "delegate_llm"
    assert result.flow_state == FlowState.EDIT_BOOKING
    assert result.flow_edit_draft is not None


async def test_the_router_dispatches_the_edit_state_to_the_flow():
    appt = appointment()
    draft = draft_for(appt)
    result = await fr.route(
        conversation(draft, fr.STEP_EDIT_MENU),
        tenant(),
        None,
        ae.LABEL_EDIT_MORE,
        upcoming_appointments=[appt],
        professionals=professionals(),
        edit_context=context(),
    )
    assert result.flow_step == fr.STEP_EDIT_MORE
    assert result.flow_edit_draft is not None
```

- [ ] **Step 3: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_edit_flow_menu.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'secretaria.services.appointment_edit_flow'`.

- [ ] **Step 4: Implement the flow module (part 1)**

Create `src/secretaria/services/appointment_edit_flow.py`:

```python
"""The "Alterar Dados" flow: change an existing appointment through a draft (TASK-032 R6).

Spec: docs/superpowers/specs/2026-10-07-lembretes-alterar-dados-design.md.

`FlowState.EDIT_BOOKING` + `Conversation.flow_edit_draft`. Every step builds a NEW
`EditDraft` and returns it on the result; nothing touches the real appointment until the
final Confirmar (`_apply_confirm`, added with the apply step). Like the rest of the
router this is pure: calendars, the Pix guards and the appointment snapshot arrive as
arguments (`EditContext`, `appointments`, `professionals`), never read here.

This module sits beside `flow_router` and uses its private helpers on purpose - the day
and slot pickers, the label matching and the service/doctor/convênio matching exist
once, there. `flow_router._route` imports THIS module lazily (one dispatch line), which
is what keeps the import graph acyclic.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from secretaria.ai.formatter import TextBubble
from secretaria.core.logging import get_logger
from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS
from secretaria.models import FlowState
from secretaria.services import appointment_edit as ae
from secretaria.services import flow_router as fr
from secretaria.services.calendar import CalendarUnavailableError
from secretaria.services.tenant_config import professional_appointment_types

logger = get_logger(__name__)


# --- Small helpers -------------------------------------------------------------------


def _tz(tenant) -> ZoneInfo:
    return ZoneInfo(getattr(tenant, "timezone", None) or "America/Sao_Paulo")


def _uuid(value: str | None) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


def _iso(value: datetime) -> str:
    """A naive clinic-local ISO minute, the shape `EditDraft` stores."""
    return value.replace(tzinfo=None, second=0, microsecond=0).isoformat(timespec="minutes")


def _result(draft: ae.EditDraft, bubbles: list, step: str, **extra) -> fr.FlowRouterResult:
    """A reply that stays in the edit, carrying the appointment, the doctor and the draft."""
    return fr.FlowRouterResult(
        action="reply",
        bubbles=bubbles,
        flow_state=FlowState.EDIT_BOOKING,
        flow_step=step,
        flow_managing_appointment_id=_uuid(draft.appointment_id),
        flow_selected_professional_id=_uuid(draft.current["professional_id"]),
        flow_edit_draft=draft.to_json(),
        **extra,
    )


def _leave(tenant, professionals, *prefix) -> fr.FlowRouterResult:
    """Close the edit: `prefix` bubbles, then the returning-patient menu. The draft is dropped."""
    return fr.FlowRouterResult(
        action="reply",
        bubbles=[*prefix, *fr._menu_bubbles(tenant, professionals)],
        flow_state=FlowState.MENU,
    )


def _carrier(conversation, draft: ae.EditDraft) -> fr._DayPickerState:
    """What the day/slot pickers read: the appointment, the doctor and the NEW draft."""
    return fr._DayPickerState(
        id=getattr(conversation, "id", None),
        flow_step=getattr(conversation, "flow_step", None),
        flow_selected_professional_id=_uuid(draft.current["professional_id"]),
        flow_managing_appointment_id=_uuid(draft.appointment_id),
        flow_edit_draft=draft.to_json(),
    )


def _doctor(professionals, draft: ae.EditDraft):
    return fr._find_professional_by_id(professionals, _uuid(draft.current["professional_id"]))


def _services_of(tenant, professional) -> list[dict]:
    if professional is not None:
        return professional_appointment_types(professional, tenant)
    return fr.active_appointment_types(tenant)


def _requirements(tenant, professionals, draft: ae.EditDraft) -> list[str]:
    service = fr._match_service(
        _services_of(tenant, _doctor(professionals, draft)), draft.current["service"]
    )
    raw = (service or {}).get("requirements") or []
    return [str(item).strip() for item in raw if str(item).strip()]


async def _slot_free(calendar: Any, start: datetime, end: datetime, ignore: str | None) -> bool:
    """Whether [start, end) is bookable on `calendar`; unknown (no agenda, outage) = not free."""
    if calendar is None:
        return False
    try:
        return bool(await calendar.is_slot_free(start, end, ignore_event_id=ignore))
    except CalendarUnavailableError:
        return False


def _ignore_event(draft: ae.EditDraft, appt: dict) -> str | None:
    """The appointment's own event only counts as "itself" on the SAME doctor's agenda."""
    same = draft.current["professional_id"] == draft.original["professional_id"]
    return str(appt.get("google_event_id") or "") or None if same else None


# --- Menus ----------------------------------------------------------------------------


def _menu_labels(ctx: ae.EditContext) -> list[str]:
    labels: list[str] = []
    if not ctx.reschedule_blocked:
        labels += [ae.LABEL_EDIT_DATE, ae.LABEL_EDIT_TIME]
    if not ctx.paid_deposit:
        labels += [ae.LABEL_EDIT_SERVICE, ae.LABEL_EDIT_DOCTOR]
    labels.append(ae.LABEL_EDIT_MORE)
    return labels


def _menu_result(draft: ae.EditDraft, ctx: ae.EditContext) -> fr.FlowRouterResult:
    body = ae.EDIT_MENU_BODY
    if ctx.paid_deposit:
        body = f"{body}\n\n{ae.EDIT_PIX_NOTICE}"
    if ctx.reschedule_blocked:
        body = f"{body}\n\n{ae.EDIT_LIMIT_NOTICE}"
    return _result(
        draft,
        [fr.MenuBubble(body=body[:MAX_INTERACTIVE_BODY_CHARS], labels=_menu_labels(ctx))],
        fr.STEP_EDIT_MENU,
    )


def _more_result(draft: ae.EditDraft, ctx: ae.EditContext) -> fr.FlowRouterResult:
    labels = [] if ctx.paid_deposit else [ae.LABEL_EDIT_INSURANCE]
    labels += [ae.LABEL_EDIT_PATIENT, ae.LABEL_EDIT_BACK]
    return _result(
        draft, [fr.MenuBubble(body=ae.EDIT_MORE_BODY, labels=labels)], fr.STEP_EDIT_MORE
    )


def enter_edit_menu(tenant, appt: dict, ctx: ae.EditContext | None = None) -> fr.FlowRouterResult:
    """The entry from the reminder's "Alterar Dados": a fresh draft + the 5-option menu."""
    draft = ae.EditDraft.from_appointment(appt, _tz(tenant))
    return _menu_result(draft, ctx or ae.EditContext())


def _confirm_result(
    draft: ae.EditDraft, tenant, professionals
) -> fr.FlowRouterResult:
    """The ONE complete confirmation message: everything, what changed, three buttons."""
    doctor = _doctor(professionals, draft)
    text = ae.build_edit_recap(
        draft,
        doctor=str(getattr(doctor, "name", "") or "") or None,
        address=(getattr(tenant, "address", None) or "").strip() or None,
        requirements=_requirements(tenant, professionals, draft),
    )
    return _result(
        draft.with_stage(),
        [
            fr.MenuBubble(
                body=text, labels=[fr.LABEL_CONFIRM, fr.LABEL_CANCEL, ae.LABEL_EDIT_MORE_DATA]
            )
        ],
        fr.STEP_EDIT_CONFIRM,
    )


# --- Date / time ----------------------------------------------------------------------


async def _begin_date(conversation, tenant, draft, professionals, ctx) -> fr.FlowRouterResult:
    draft = draft.with_stage(mode="date")
    return await fr.enter_day_picker(
        _carrier(conversation, draft),
        tenant,
        ctx.calendar_for(draft.current["professional_id"]),
        duration_minutes=draft.duration_minutes,
        branch=fr.EDIT_DAY_BRANCH,
        anchor=draft.start,
        professionals=professionals,
    )


async def _slots_for(
    conversation, tenant, draft, day: datetime, professionals, ctx
) -> fr.FlowRouterResult:
    draft = draft.with_stage(mode="time")
    return await fr._enter_slot_picker(
        _carrier(conversation, draft),
        tenant,
        ctx.calendar_for(draft.current["professional_id"]),
        datetime.combine(day.date(), time.min),
        duration_minutes=draft.duration_minutes,
        branch=fr.EDIT_DAY_BRANCH,
        professionals=professionals,
    )


async def _after_slot_affecting_change(
    conversation, tenant, draft, appt, professionals, ctx
) -> fr.FlowRouterResult:
    """A doctor/service change may have taken the current time with it: check, then go on.

    Free -> straight to the confirmation. Not free (or unknown) -> day, then time, for the
    NEW doctor/service, behind a one-line explanation (spec P1) - never a confirmation
    for a slot that does not exist.
    """
    calendar = ctx.calendar_for(draft.current["professional_id"])
    free = await _slot_free(calendar, draft.start, draft.end, _ignore_event(draft, appt))
    if free:
        return _confirm_result(draft, tenant, professionals)
    reslot = draft.with_stage(mode="reslot")
    picker = await fr.enter_day_picker(
        _carrier(conversation, reslot),
        tenant,
        calendar,
        duration_minutes=reslot.duration_minutes,
        branch=fr.EDIT_DAY_BRANCH,
        anchor=reslot.start,
        professionals=professionals,
    )
    if picker.action == "reply":
        picker.bubbles.insert(0, TextBubble(body=ae.EDIT_RESLOT_NOTICE))
    return picker


async def _day_step(conversation, tenant, body, draft, appt, professionals, ctx):
    calendar = ctx.calendar_for(draft.current["professional_id"])
    if draft.stage.get("mode") == "date":
        target, _page = fr._day_from_body(body)
        if target is None and calendar is not None:
            target = fr._parse_day(body, datetime.now(calendar.tzinfo))
        if target is not None:
            asked = draft.with_stage(mode="date", target_day=target.date().isoformat())
            return _result(
                asked,
                [
                    fr.MenuBubble(
                        body=ae.EDIT_TIME_TOO_BODY,
                        labels=[ae.LABEL_TIME_TOO_YES, ae.LABEL_TIME_TOO_NO],
                    )
                ],
                fr.STEP_EDIT_TIME_TOO,
            )
    return await fr._handle_day_step(
        conversation,
        tenant,
        calendar,
        body,
        duration_minutes=draft.duration_minutes,
        branch=fr.EDIT_DAY_BRANCH,
        services=[],
        back_target=None,
        professionals=professionals,
    )


async def _time_too_step(conversation, tenant, body, draft, appt, professionals, ctx):
    raw = draft.stage.get("target_day")
    if not raw:
        return _menu_result(draft.with_stage(), ctx)
    day = datetime.fromisoformat(raw)
    if fr._label_match(body, ae.LABEL_TIME_TOO_YES):
        return await _slots_for(conversation, tenant, draft, day, professionals, ctx)
    if fr._label_match(body, ae.LABEL_TIME_TOO_NO):
        start = datetime.combine(day.date(), draft.start.time())
        end = start + timedelta(minutes=draft.duration_minutes)
        calendar = ctx.calendar_for(draft.current["professional_id"])
        if await _slot_free(calendar, start, end, _ignore_event(draft, appt)):
            moved = draft.with_changes(start_at=_iso(start), end_at=_iso(end))
            return _confirm_result(moved, tenant, professionals)
        picker = await _slots_for(conversation, tenant, draft, day, professionals, ctx)
        if picker.action == "reply":
            picker.bubbles.insert(0, TextBubble(body=ae.EDIT_TIME_BUSY))
        return picker
    return fr._preserve(conversation, "delegate_llm")


async def _slot_step(conversation, tenant, body, draft, appt, professionals, ctx):
    control = await fr._handle_slot_controls(
        conversation,
        tenant,
        ctx.calendar_for(draft.current["professional_id"]),
        body,
        duration_minutes=draft.duration_minutes,
        branch=fr.EDIT_DAY_BRANCH,
        services=[],
        back_target=None,
        professionals=professionals,
    )
    if control is not None:
        return control
    start = fr._slot_iso_from_body(body)
    if start is None:
        return fr._preserve(conversation, "delegate_llm")
    end = start + timedelta(minutes=draft.duration_minutes)
    return _confirm_result(
        draft.with_changes(start_at=_iso(start), end_at=_iso(end)), tenant, professionals
    )


# --- Dispatcher -----------------------------------------------------------------------


async def edit_step(
    conversation,
    tenant,
    body: str,
    appointments: list[dict],
    professionals: list | None,
    ctx: ae.EditContext | None,
    patient_name: str | None = None,
) -> fr.FlowRouterResult:
    """Route one inbound turn while the conversation is in `FlowState.EDIT_BOOKING`."""
    draft = ae.EditDraft.from_json(getattr(conversation, "flow_edit_draft", None))
    appt = fr._find_appt_by_id(appointments, fr._managing_appt_id_str(conversation))
    if draft is None or appt is None or ctx is None or str(appt.get("id")) != draft.appointment_id:
        return _leave(tenant, professionals, TextBubble(body=ae.EDIT_STALE))
    step = conversation.flow_step
    match = fr._label_match

    if step == fr.STEP_EDIT_MENU:
        if match(body, ae.LABEL_EDIT_DATE) and not ctx.reschedule_blocked:
            return await _begin_date(conversation, tenant, draft, professionals, ctx)
        if match(body, ae.LABEL_EDIT_TIME) and not ctx.reschedule_blocked:
            return await _slots_for(conversation, tenant, draft, draft.start, professionals, ctx)
        if match(body, ae.LABEL_EDIT_MORE):
            return _more_result(draft.with_stage(), ctx)
        return fr._preserve(conversation, "delegate_llm")

    if step == fr.STEP_EDIT_MORE:
        if match(body, ae.LABEL_EDIT_BACK):
            return _menu_result(draft.with_stage(), ctx)
        return fr._preserve(conversation, "delegate_llm")

    if step in fr._EDIT_DAY_STEPS:
        return await _day_step(conversation, tenant, body, draft, appt, professionals, ctx)

    if step == fr.STEP_EDIT_TIME_TOO:
        return await _time_too_step(conversation, tenant, body, draft, appt, professionals, ctx)

    if step == fr.STEP_EDIT_SLOT:
        return await _slot_step(conversation, tenant, body, draft, appt, professionals, ctx)

    if step == fr.STEP_EDIT_CONFIRM:
        if match(body, fr.LABEL_CANCEL):
            return _leave(tenant, professionals, TextBubble(body=ae.EDIT_KEPT))
        if match(body, ae.LABEL_EDIT_MORE_DATA):
            return _menu_result(draft.with_stage(), ctx)
        return fr._preserve(conversation, "delegate_llm")

    return fr._preserve(conversation, "delegate_llm")
```

Later tasks extend this dispatcher in two places only: Task 8 adds the service / doctor / convênio / paciente steps right before the final `return fr._preserve(conversation, "delegate_llm")`, and Task 9 adds the Confirmar branch of `STEP_EDIT_CONFIRM` directly above that step's own `return fr._preserve(...)`.

- [ ] **Step 5: The dispatch and the `edit_context` argument**

In `src/secretaria/services/flow_router.py`: add `edit_context: EditContext | None = None` as the last parameter of both `route(...)` and `_route(...)`, pass it through in `route`'s call (`edit_context=edit_context`), and in `_route`, directly after the `if state == FlowState.MANAGE_BOOKING:` block, add:

```python
    if state == FlowState.EDIT_BOOKING:
        # TASK-032 R6: imported here, not at module top - the flow module uses this
        # module's pickers and helpers, so a top-level import would be circular.
        from secretaria.services.appointment_edit_flow import edit_step

        return await edit_step(
            conversation,
            tenant,
            inbound_body,
            upcoming_appointments or [],
            professionals,
            edit_context,
            patient_name,
        )
```

- [ ] **Step 6: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_edit_flow_menu.py tests/test_edit_day_picker.py tests/test_flow_router.py tests/test_workers_layering.py -q`
Expected: all pass. If `_hold_windows` reaches a database inside `_enter_slot_picker` and fails in these router-level tests, make the tests patch it exactly as `tests/test_flow_router.py` does for its slot-picker tests (search that file for `_hold_windows`) and ledger it.

- [ ] **Step 7: Lint and commit**

```bash
uvx ruff format tests/_edit_flow_support.py src/secretaria/services/appointment_edit_flow.py tests/test_appointment_edit_flow_menu.py
uvx ruff check --fix tests/_edit_flow_support.py src/secretaria/services/appointment_edit_flow.py tests/test_appointment_edit_flow_menu.py
uvx ruff check src/secretaria/services/flow_router.py
git diff --stat
git add tests/_edit_flow_support.py src/secretaria/services/appointment_edit_flow.py src/secretaria/services/flow_router.py tests/test_appointment_edit_flow_menu.py
git commit -F - <<'EOF'
feat(edit): the Alterar Dados menu, date, time and the complete confirmation (TASK-032 R6)

A draft only: nothing touches the appointment before the final Confirmar.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 8: The edit flow, part 2 — service, doctor, convênio and patient

**Files:**
- Modify: `src/secretaria/services/appointment_edit.py` (three constants)
- Modify: `src/secretaria/services/appointment_edit_flow.py` (the steps + the dispatcher)
- Test: `tests/test_appointment_edit_flow_fields.py`

**Interfaces:**
- Consumes: Task 7's helpers (`_result`, `_leave`, `_carrier`, `_menu_result`, `_more_result`, `_confirm_result`, `_after_slot_affecting_change`, `_slot_free`, `_iso`, `_doctor`, `_services_of`, `_ignore_event`) and the existing router helpers `_service_row_title`, `_match_service`, `_service_duration`, `_match_professional`, `_professional_row_title`, `_selected_plan`, `_accepts_plan`, `_tenant_insurance_plans`, `_match_insurance_plan`, `_says_no_insurance`, `_reads_as_conversation`, `LABEL_INSURANCE_PARTICULAR`, `LABEL_INSURANCE_OTHER`, `INSURANCE_PROMPT_OTHER`, `MAX_CATALOG_OPTION_ROWS`, `MAX_INSURANCE_PLAN_ROWS`, and from `services/attendee.py`: `ATTENDEE_QUESTION_BODY`, `LABEL_ATTENDEE_SELF`, `LABEL_ATTENDEE_OTHER`, `ATTENDEE_NAME_REQUEST`, `ATTENDEE_NAME_INVALID`, `LABEL_ATTENDEE_AUTH_CONFIRM`, `LABEL_ATTENDEE_AUTH_BACK`, `authorization_body`, `parse_attendee_name`.
- Produces: `_service_list`, `_service_step`, `_doctor_list`, `_doctor_step`, `_insurance_list`, `_insurance_step`, `_insurance_other_step`, `_attendee_question_result`, `_attendee_step`; `ae.EDIT_NO_SERVICES`, `ae.EDIT_DOCTOR_BODY`, `ae.EDIT_SERVICE_BODY`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_appointment_edit_flow_fields.py`:

```python
"""The edit flow: service, doctor, convênio and patient (TASK-032 R6, spec §5.4)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import timedelta  # noqa: E402

from secretaria.ai.formatter import ButtonBubble, SlotsBubble, TextBubble  # noqa: E402
from secretaria.services import appointment_edit as ae  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.appointment_edit_flow import edit_step  # noqa: E402
from secretaria.services.attendee import (  # noqa: E402
    ATTENDEE_NAME_INVALID,
    ATTENDEE_NAME_REQUEST,
    ATTENDEE_QUESTION_BODY,
    LABEL_ATTENDEE_AUTH_BACK,
    LABEL_ATTENDEE_AUTH_CONFIRM,
    LABEL_ATTENDEE_OTHER,
    LABEL_ATTENDEE_SELF,
)
from tests._edit_flow_support import (  # noqa: E402
    DOCTOR_A,
    DOCTOR_B,
    START_UTC,
    EditCalendar,
    appointment,
    context,
    conversation,
    draft_for,
    professionals,
    slots,
    tenant,
)


async def _step(step, body, *, draft=None, ctx=None, appt=None, **kw):
    appt = appt or appointment()
    draft = draft or draft_for(appt)
    conv = conversation(draft, step, **kw)
    return await edit_step(conv, tenant(), body, [appt], professionals(), ctx or context())


def _draft_of(result) -> ae.EditDraft:
    return ae.EditDraft.from_json(result.flow_edit_draft)


def _rows(result):
    return result.bubbles[-1].rows


# --- Service -------------------------------------------------------------------------


async def test_mudar_servico_lists_only_the_current_doctors_services():
    result = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_SERVICE)

    assert result.flow_step == fr.STEP_EDIT_SERVICE
    assert [row[0] for row in _rows(result)] == ["svc|Consulta", "svc|Retorno"]


async def test_a_shorter_service_keeps_the_time_and_goes_to_the_confirmation():
    result = await _step(fr.STEP_EDIT_SERVICE, fr._service_row_title("Retorno"))

    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    draft = _draft_of(result)
    assert draft.current["service"] == "Retorno"
    assert draft.current["end_at"] == "2030-10-15T15:40"  # 20 minutes from 15:20
    assert draft.changed() == ["serviço"]


async def test_a_longer_service_that_does_not_fit_asks_for_a_new_time():
    appt = appointment(appointment_type="Retorno", end_at=START_UTC + timedelta(minutes=20))
    calendar = EditCalendar(free=False)

    result = await _step(
        fr.STEP_EDIT_SERVICE,
        fr._service_row_title("Consulta"),
        appt=appt,
        ctx=context(calendar),
    )

    assert result.flow_step == fr.STEP_EDIT_DAY
    assert result.bubbles[0].body == ae.EDIT_RESLOT_NOTICE
    assert _draft_of(result).stage["mode"] == "reslot"
    # Never a confirmation for a window that is not free.
    assert not any(isinstance(b, fr.MenuBubble) for b in result.bubbles)


# --- Doctor --------------------------------------------------------------------------


async def test_the_doctor_list_marks_free_and_busy():
    busy_b = EditCalendar(free=False)
    ctx = context(calendars={str(DOCTOR_A): EditCalendar(free=True), str(DOCTOR_B): busy_b})

    result = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_DOCTOR, ctx=ctx)

    assert result.flow_step == fr.STEP_EDIT_DOCTOR
    assert isinstance(result.bubbles[0], SlotsBubble)
    rows = _rows(result)
    assert [row[0] for row in rows] == [f"prof|{DOCTOR_A}", f"prof|{DOCTOR_B}"]
    assert rows[0][2].startswith(ae.MARK_FREE)
    assert rows[1][2].startswith(ae.MARK_BUSY)
    assert all(len(row[2]) <= 72 for row in rows)


async def test_the_current_doctors_own_event_is_not_a_conflict():
    calendar = EditCalendar(free=True)
    ctx = context(calendars={str(DOCTOR_A): calendar, str(DOCTOR_B): EditCalendar()})

    await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_DOCTOR, ctx=ctx)

    assert calendar.checked and calendar.checked[0][2] == "evt-old"


def _doctor_tap(name, doctor_id):
    return f"{fr._professional_row_title(name)} ({doctor_id})"


async def test_a_free_doctor_goes_to_the_confirmation():
    ctx = context(calendars={str(DOCTOR_A): EditCalendar(), str(DOCTOR_B): EditCalendar(free=True)})

    result = await _step(fr.STEP_EDIT_DOCTOR, _doctor_tap("Dra. Ana Lima", DOCTOR_B), ctx=ctx)

    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    draft = _draft_of(result)
    assert draft.current["professional_id"] == str(DOCTOR_B)
    assert draft.current["end_at"] == "2030-10-15T15:50"  # her Consulta is 30 minutes
    assert draft.changed() == ["médico"]
    assert "Médico: Dra. Ana Lima" in result.bubbles[0].body


async def test_a_busy_doctor_continues_to_day_and_time():
    ctx = context(
        calendars={str(DOCTOR_A): EditCalendar(), str(DOCTOR_B): EditCalendar(free=False)}
    )

    result = await _step(fr.STEP_EDIT_DOCTOR, _doctor_tap("Dra. Ana Lima", DOCTOR_B), ctx=ctx)

    assert result.flow_step == fr.STEP_EDIT_DAY
    assert result.bubbles[0].body == ae.EDIT_RESLOT_NOTICE
    assert _draft_of(result).current["professional_id"] == str(DOCTOR_B)


async def test_a_doctor_who_does_not_offer_the_service_lists_their_services():
    appt = appointment(appointment_type="Retorno", end_at=START_UTC + timedelta(minutes=20))
    ctx = context(calendars={str(DOCTOR_A): EditCalendar(), str(DOCTOR_B): EditCalendar()})

    result = await _step(
        fr.STEP_EDIT_DOCTOR, _doctor_tap("Dra. Ana Lima", DOCTOR_B), ctx=ctx, appt=appt
    )

    assert result.flow_step == fr.STEP_EDIT_SERVICE
    assert [row[0] for row in _rows(result)] == ["svc|Consulta"]
    assert _draft_of(result).stage == {"after_doctor": "1"}

    picked = await edit_step(
        conversation(_draft_of(result), fr.STEP_EDIT_SERVICE),
        tenant(),
        fr._service_row_title("Consulta"),
        [appt],
        professionals(),
        ctx,
    )
    assert picked.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft_of(picked).changed() == ["serviço", "médico"]


# --- Convênio ------------------------------------------------------------------------


async def test_mudar_convenio_marks_what_the_current_doctor_accepts():
    result = await _step(fr.STEP_EDIT_MORE, ae.LABEL_EDIT_INSURANCE)

    assert result.flow_step == fr.STEP_EDIT_INSURANCE
    rows = {row[0]: row for row in _rows(result)}
    assert rows["ins|Unimed"][2].startswith(ae.MARK_FREE)
    assert rows["ins|Amil"][2].startswith(ae.MARK_BUSY)  # Dr. Diogo only takes Unimed
    assert {"ins|particular", "ins|outro"} <= set(rows)


async def test_choosing_a_plan_goes_to_the_confirmation():
    result = await _step(fr.STEP_EDIT_INSURANCE, "Amil")
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft_of(result).changed() == ["convênio"]
    assert "Convênio: Amil" in result.bubbles[0].body


async def test_outro_convenio_asks_for_the_name_then_stores_it():
    asked = await _step(fr.STEP_EDIT_INSURANCE, fr.LABEL_INSURANCE_OTHER)
    assert asked.flow_step == fr.STEP_EDIT_INSURANCE_OTHER
    assert asked.bubbles[0].body == fr.INSURANCE_PROMPT_OTHER

    stored = await _step(
        fr.STEP_EDIT_INSURANCE_OTHER, "Bradesco Saúde", draft=_draft_of(asked)
    )
    assert stored.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft_of(stored).current["insurance"] == "Bradesco Saúde"


async def test_a_question_in_the_convenio_step_goes_to_the_ai_with_the_draft_kept():
    result = await _step(fr.STEP_EDIT_INSURANCE, "qual o endereço de vocês?")
    assert result.action == "delegate_llm"
    assert result.flow_edit_draft is not None


# --- Patient -------------------------------------------------------------------------


async def test_mudar_paciente_asks_who_it_is_for():
    result = await _step(fr.STEP_EDIT_MORE, ae.LABEL_EDIT_PATIENT)
    assert result.flow_step == fr.STEP_EDIT_ATT_CHOICE
    assert result.bubbles[0].body == ATTENDEE_QUESTION_BODY
    assert result.bubbles[0].labels == [LABEL_ATTENDEE_SELF, LABEL_ATTENDEE_OTHER]


async def test_for_myself_clears_a_third_party_and_confirms():
    draft = draft_for(appointment(attendee_name="Ana Souza"))
    result = await _step(fr.STEP_EDIT_ATT_CHOICE, LABEL_ATTENDEE_SELF, draft=draft)
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft_of(result).current["attendee_name"] is None
    assert _draft_of(result).changed() == ["paciente"]


async def test_for_someone_else_asks_the_name_then_the_authorization():
    asked = await _step(fr.STEP_EDIT_ATT_CHOICE, LABEL_ATTENDEE_OTHER)
    assert asked.flow_step == fr.STEP_EDIT_ATT_NAME
    assert asked.bubbles[0].body == ATTENDEE_NAME_REQUEST

    invalid = await _step(fr.STEP_EDIT_ATT_NAME, "???", draft=_draft_of(asked))
    assert invalid.bubbles[0].body == ATTENDEE_NAME_INVALID

    authorization = await _step(fr.STEP_EDIT_ATT_NAME, "Ana Souza", draft=_draft_of(asked))
    assert authorization.flow_step == fr.STEP_EDIT_ATT_AUTH
    assert isinstance(authorization.bubbles[0], ButtonBubble)
    assert authorization.flow_attendee_name == "Ana Souza"
    # Still a proposal: the draft's patient is unchanged until the sentence is confirmed.
    assert _draft_of(authorization).current["attendee_name"] is None

    confirmed = await _step(
        fr.STEP_EDIT_ATT_AUTH, LABEL_ATTENDEE_AUTH_CONFIRM, draft=_draft_of(authorization)
    )
    assert confirmed.flow_step == fr.STEP_EDIT_CONFIRM
    assert confirmed.attendee_authorized is True
    assert _draft_of(confirmed).current["attendee_name"] == "Ana Souza"
    assert "Paciente: Ana Souza" in confirmed.bubbles[0].body


async def test_declining_the_authorization_goes_back_to_the_question():
    asked = await _step(fr.STEP_EDIT_ATT_CHOICE, LABEL_ATTENDEE_OTHER)
    authorization = await _step(fr.STEP_EDIT_ATT_NAME, "Ana Souza", draft=_draft_of(asked))

    back = await _step(
        fr.STEP_EDIT_ATT_AUTH, LABEL_ATTENDEE_AUTH_BACK, draft=_draft_of(authorization)
    )

    assert back.flow_step == fr.STEP_EDIT_ATT_CHOICE
    assert _draft_of(back).current["attendee_name"] is None
    assert back.attendee_authorized is False


async def test_a_doctor_without_services_says_so_and_shows_the_menu():
    pros = professionals()
    pros[0].appointment_types = []
    conv = conversation(draft_for(), fr.STEP_EDIT_MENU)
    result = await edit_step(
        conv, tenant(appointment_types=[]), ae.LABEL_EDIT_SERVICE, [appointment()], pros, context()
    )
    assert isinstance(result.bubbles[0], TextBubble)
    assert result.bubbles[0].body == ae.EDIT_NO_SERVICES
    assert result.flow_step == fr.STEP_EDIT_MENU


async def test_slots_helper_is_importable_for_the_next_task():
    assert slots("10:00")[0]["label"] == "10:00"
```

(Note: the appointment's own `_appointment_id` in `conversation()` comes from the draft, so `_step` always finds it in `[appt]`.)

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_edit_flow_fields.py -q`
Expected: FAIL — the dispatcher falls through to `delegate_llm` for every new step (`assert result.flow_step == ...`), and `ae.EDIT_NO_SERVICES` does not exist.

- [ ] **Step 3: Three constants**

In `src/secretaria/services/appointment_edit.py`, after `EDIT_LIMIT_NOTICE` add:

```python
EDIT_NO_SERVICES = "Esse médico não tem serviços disponíveis no momento."
EDIT_SERVICE_BODY = "Qual serviço você quer para essa consulta?"
EDIT_DOCTOR_BODY = (
    "Qual médico você quer?\n\n"
    f"{MARK_FREE} = livre no dia e horário da consulta\n"
    f"{MARK_BUSY} = ocupado nesse dia e horário"
)
```

- [ ] **Step 4: The steps**

In `src/secretaria/services/appointment_edit_flow.py` add the imports

```python
from secretaria.ai.formatter import ButtonBubble, SlotsBubble, TextBubble
from secretaria.core.whatsapp_limits import MAX_LIST_ROW_DESCRIPTION_CHARS, truncate_plain
from secretaria.services.attendee import (
    ATTENDEE_NAME_INVALID,
    ATTENDEE_NAME_REQUEST,
    ATTENDEE_QUESTION_BODY,
    LABEL_ATTENDEE_AUTH_BACK,
    LABEL_ATTENDEE_AUTH_CONFIRM,
    LABEL_ATTENDEE_OTHER,
    LABEL_ATTENDEE_SELF,
    authorization_body,
    parse_attendee_name,
)
```

(merge with the existing `TextBubble` import) and append, above the `# --- Dispatcher` section:

```python
# --- Service --------------------------------------------------------------------------


def _service_list(
    draft: ae.EditDraft, tenant, professionals, ctx, *, after_doctor: bool = False
) -> fr.FlowRouterResult:
    """The services of the draft's doctor only (owner, 2026-10-07)."""
    services = _services_of(tenant, _doctor(professionals, draft))
    if not services:
        menu = _menu_result(draft.with_stage(), ctx)
        menu.bubbles.insert(0, TextBubble(body=ae.EDIT_NO_SERVICES))
        return menu
    rows = [
        (f"svc|{service.get('name', '')}", fr._service_row_title(service.get("name")))
        for service in services[: fr.MAX_CATALOG_OPTION_ROWS]
    ]
    return _result(
        draft.with_stage(**({"after_doctor": "1"} if after_doctor else {})),
        [
            SlotsBubble(
                body=ae.EDIT_SERVICE_BODY,
                rows=rows,
                button_label="Ver serviços",
                section_title="Serviços",
            )
        ],
        fr.STEP_EDIT_SERVICE,
    )


async def _service_step(conversation, tenant, body, draft, appt, professionals, ctx):
    service = fr._match_service(_services_of(tenant, _doctor(professionals, draft)), body)
    if service is None:
        return fr._preserve(conversation, "delegate_llm")
    minutes = fr._service_duration(service, tenant)
    after_doctor = bool(draft.stage.get("after_doctor"))
    new = draft.with_changes(
        service=str(service.get("name")),
        end_at=_iso(draft.start + timedelta(minutes=minutes)),
    ).with_stage()
    if after_doctor or minutes != draft.duration_minutes:
        # A different length (or a new doctor) may not fit the current time: check first.
        return await _after_slot_affecting_change(conversation, tenant, new, appt, professionals, ctx)
    return _confirm_result(new, tenant, professionals)


# --- Doctor ---------------------------------------------------------------------------


async def _availability(calendar: Any, start: datetime, end: datetime, ignore: str | None):
    """True / False, or None when the agenda is missing or unreachable (no mark then)."""
    if calendar is None:
        return None
    try:
        return bool(await calendar.is_slot_free(start, end, ignore_event_id=ignore))
    except CalendarUnavailableError:
        return None


async def _doctor_list(draft, tenant, appt, professionals, ctx) -> fr.FlowRouterResult:
    """Every doctor, marked free (✅) or busy (❌) at the appointment's current day and time."""
    plan = fr._selected_plan(tenant, draft.current["insurance"])
    when = draft.start.strftime("%d/%m %H:%M")
    rows: list[tuple] = []
    for professional in (professionals or [])[: fr.MAX_CATALOG_OPTION_ROWS]:
        pid = str(professional.id)
        own_event = str(appt.get("google_event_id") or "") or None
        ignore = own_event if pid == draft.original["professional_id"] else None
        free = await _availability(ctx.calendar_for(pid), draft.start, draft.end, ignore)
        parts: list[str] = []
        if free is not None:
            parts.append(f"{ae.MARK_FREE if free else ae.MARK_BUSY} {'Livre' if free else 'Ocupado'} {when}")
        if plan is not None and fr._accepts_plan(tenant, professional, str(plan["id"])):
            parts.append("aceita o convênio")
        specialty = (getattr(professional, "specialty", None) or "").strip() or None
        description = truncate_plain(
            " · ".join(parts) or (specialty or ""), MAX_LIST_ROW_DESCRIPTION_CHARS
        ) or None
        rows.append((f"prof|{professional.id}", fr._professional_row_title(professional.name), description))
    return _result(
        draft.with_stage(),
        [
            SlotsBubble(
                body=ae.EDIT_DOCTOR_BODY,
                rows=rows,
                button_label="Ver médicos",
                section_title="Médicos",
            )
        ],
        fr.STEP_EDIT_DOCTOR,
    )


async def _doctor_step(conversation, tenant, body, draft, appt, professionals, ctx):
    professional = fr._match_professional(professionals or [], body)
    if professional is None:
        return fr._preserve(conversation, "delegate_llm")
    new = draft.with_changes(professional_id=str(professional.id))
    service = fr._match_service(
        professional_appointment_types(professional, tenant), new.current["service"]
    )
    if service is None:
        # The new doctor does not offer the current service: theirs next, then the check.
        return _service_list(new, tenant, professionals, ctx, after_doctor=True)
    minutes = fr._service_duration(service, tenant)
    new = new.with_changes(end_at=_iso(new.start + timedelta(minutes=minutes))).with_stage()
    return await _after_slot_affecting_change(conversation, tenant, new, appt, professionals, ctx)


# --- Convênio -------------------------------------------------------------------------


def _insurance_list(draft: ae.EditDraft, tenant, professionals) -> fr.FlowRouterResult:
    """The clinic's plans, each marked by whether the CURRENT doctor takes it; never filtered."""
    doctor = _doctor(professionals, draft)
    rows: list[tuple] = []
    for plan in fr._tenant_insurance_plans(tenant)[: fr.MAX_INSURANCE_PLAN_ROWS]:
        name = str(plan["name"])
        description = None
        if doctor is not None and plan.get("id"):
            accepted = fr._accepts_plan(tenant, doctor, str(plan["id"]))
            description = (
                f"{ae.MARK_FREE} aceito pelo médico" if accepted else f"{ae.MARK_BUSY} não aceito pelo médico"
            )
        rows.append((f"ins|{name}", fr.truncate_list_row_title(name), description))
    rows.append(("ins|particular", fr.LABEL_INSURANCE_PARTICULAR, None))
    rows.append(("ins|outro", fr.LABEL_INSURANCE_OTHER, None))
    return _result(
        draft.with_stage(),
        [
            SlotsBubble(
                body="Qual o convênio dessa consulta?",
                rows=rows,
                button_label="Ver convênios",
                section_title="Convênios",
            )
        ],
        fr.STEP_EDIT_INSURANCE,
    )


def _insurance_step(conversation, tenant, body, draft, professionals):
    if fr._label_match(body, fr.LABEL_INSURANCE_OTHER):
        return _result(
            draft, [TextBubble(body=fr.INSURANCE_PROMPT_OTHER)], fr.STEP_EDIT_INSURANCE_OTHER
        )
    matched = fr._match_insurance_plan(tenant, body)
    if matched is None and fr._says_no_insurance(body):
        matched = fr.LABEL_INSURANCE_PARTICULAR
    if matched is None:
        if fr._reads_as_conversation(body):
            return fr._preserve(conversation, "delegate_llm")
        return _insurance_list(draft, tenant, professionals)
    return _confirm_result(draft.with_changes(insurance=matched[:120]).with_stage(), tenant, professionals)


def _insurance_other_step(conversation, tenant, body, draft, professionals):
    text = (body or "").strip()
    if not text or fr._reads_as_conversation(text):
        return fr._preserve(conversation, "delegate_llm")
    return _confirm_result(draft.with_changes(insurance=text[:120]).with_stage(), tenant, professionals)


# --- Patient --------------------------------------------------------------------------


def _attendee_question_result(draft: ae.EditDraft) -> fr.FlowRouterResult:
    return _result(
        draft.with_stage(),
        [
            fr.MenuBubble(
                body=ATTENDEE_QUESTION_BODY, labels=[LABEL_ATTENDEE_SELF, LABEL_ATTENDEE_OTHER]
            )
        ],
        fr.STEP_EDIT_ATT_CHOICE,
    )


def _attendee_step(conversation, tenant, body, draft, professionals):
    """pra-quem -> [name -> authorization] -> the confirmation (same wording as booking)."""
    step = conversation.flow_step
    match = fr._label_match
    if step == fr.STEP_EDIT_ATT_CHOICE:
        if match(body, LABEL_ATTENDEE_SELF):
            return _confirm_result(draft.with_changes(attendee_name=None), tenant, professionals)
        if match(body, LABEL_ATTENDEE_OTHER):
            return _result(draft, [TextBubble(body=ATTENDEE_NAME_REQUEST)], fr.STEP_EDIT_ATT_NAME)
        return fr._preserve(conversation, "delegate_llm")
    if step == fr.STEP_EDIT_ATT_NAME:
        name = parse_attendee_name(body)
        # A boolean only - never the value (skill pii-field-capture-pseudonymization).
        logger.info("edit_attendee_name_answered", parsed=name is not None)
        if name is None:
            return _result(draft, [TextBubble(body=ATTENDEE_NAME_INVALID)], fr.STEP_EDIT_ATT_NAME)
        return _result(
            draft.with_stage(pending_attendee=name),
            [
                ButtonBubble(
                    body=authorization_body(name),
                    confirm_label=LABEL_ATTENDEE_AUTH_CONFIRM,
                    cancel_label=LABEL_ATTENDEE_AUTH_BACK,
                )
            ],
            fr.STEP_EDIT_ATT_AUTH,
            flow_attendee_name=name,
        )
    # STEP_EDIT_ATT_AUTH
    pending = draft.stage.get("pending_attendee")
    if not pending or match(body, LABEL_ATTENDEE_AUTH_BACK):
        return _attendee_question_result(draft)
    if match(body, LABEL_ATTENDEE_AUTH_CONFIRM):
        result = _confirm_result(draft.with_changes(attendee_name=pending), tenant, professionals)
        result.attendee_authorized = True  # the worker writes the ConsentEvent
        return result
    return fr._preserve(conversation, "delegate_llm")
```

- [ ] **Step 5: The dispatcher**

In `edit_step`, inside the `if step == fr.STEP_EDIT_MENU:` block add before `if match(body, ae.LABEL_EDIT_MORE):`:

```python
        if match(body, ae.LABEL_EDIT_SERVICE) and not ctx.paid_deposit:
            return _service_list(draft.with_stage(), tenant, professionals, ctx)
        if match(body, ae.LABEL_EDIT_DOCTOR) and not ctx.paid_deposit:
            return await _doctor_list(draft, tenant, appt, professionals, ctx)
```

in `if step == fr.STEP_EDIT_MORE:` add before the `LABEL_EDIT_BACK` check:

```python
        if match(body, ae.LABEL_EDIT_INSURANCE) and not ctx.paid_deposit:
            return _insurance_list(draft, tenant, professionals)
        if match(body, ae.LABEL_EDIT_PATIENT):
            return _attendee_question_result(draft)
```

and add before the final `return fr._preserve(conversation, "delegate_llm")` of the function:

```python
    if step == fr.STEP_EDIT_SERVICE:
        return await _service_step(conversation, tenant, body, draft, appt, professionals, ctx)
    if step == fr.STEP_EDIT_DOCTOR:
        return await _doctor_step(conversation, tenant, body, draft, appt, professionals, ctx)
    if step == fr.STEP_EDIT_INSURANCE:
        return _insurance_step(conversation, tenant, body, draft, professionals)
    if step == fr.STEP_EDIT_INSURANCE_OTHER:
        return _insurance_other_step(conversation, tenant, body, draft, professionals)
    if step in (fr.STEP_EDIT_ATT_CHOICE, fr.STEP_EDIT_ATT_NAME, fr.STEP_EDIT_ATT_AUTH):
        return _attendee_step(conversation, tenant, body, draft, professionals)
```

- [ ] **Step 6: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_edit_flow_fields.py tests/test_appointment_edit_flow_menu.py tests/test_flow_router.py -q`
Expected: all pass. A failing expectation about a row-description length, a row id or `parse_attendee_name("???")` returning a name means a plan defect in the test data: fix the TEST data (not the helper) and ledger it.

- [ ] **Step 7: Lint and commit**

```bash
uvx ruff format tests/test_appointment_edit_flow_fields.py src/secretaria/services/appointment_edit_flow.py
uvx ruff check --fix tests/test_appointment_edit_flow_fields.py src/secretaria/services/appointment_edit_flow.py
uvx ruff check src/secretaria/services/appointment_edit.py
git diff --stat
git add src/secretaria/services/appointment_edit.py src/secretaria/services/appointment_edit_flow.py tests/test_appointment_edit_flow_fields.py
git commit -F - <<'EOF'
feat(edit): change service, doctor (free/busy marks), convenio and patient (TASK-032 R6)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Task 9: Apply the confirmed edit, wire the worker and the reminder tap

**Files:**
- Modify: `src/secretaria/services/appointment_edit.py` (`build_edit_recap(title=)`, `EDIT_NOTHING_CHANGED`)
- Modify: `src/secretaria/services/appointment_edit_flow.py` (`_reslot_picker`, `_apply_confirm`, the Confirmar branch)
- Create: `src/secretaria/workers/shared/appointment_edit_support.py`, `src/secretaria/workers/shared/appointment_edit_apply.py`
- Modify: `src/secretaria/workers/shared/flow_runner.py` (apply branch, attendee pin, `_run_flow(edit_context=)`), `src/secretaria/workers/orchestrator.py` (context building, wants-upcoming, pass-through), `src/secretaria/workers/shared/reminder_actions.py` (`CONTINUE_EDIT`), `src/secretaria/workers/shared/actions.py` (the edit hand-off), `src/secretaria/services/patient_context.py` (two keys), `tests/_edit_flow_support.py` (`fail_update`)
- Test: `tests/test_appointment_edit_apply.py`; update `tests/test_reminder_r6_actions.py`

**Interfaces:**
- Consumes: Tasks 1–8; `deposit_lifecycle.get_deposit_for_appointment`/`register_reschedule`, `insurance_catalog.resolve_booking_plan_ids`, `appointment_status.log_status_transition`, `reminder_hooks.enabled_for`, `llm_context._appointment_calendar(_target)`, `tenant_config.{list_active_professionals, resolve_professional_calendar}`, `calendar.build_event_description`.
- Produces: router `_apply_confirm`; `appointment_edit_support.{edit_guards, build_edit_context}`; `appointment_edit_apply.{AppliedEdit, apply_appointment_edit, _finish_edit}`; `reminder_actions.CONTINUE_EDIT = "reminder_edit"`; behavior: Confirmar applies the edit to the same row, the Google event is patched (same doctor) or created on the new doctor's agenda first and the old one deleted after the commit, reminders are replanned when the time moved, and a reminder tap on "Alterar Dados" opens the menu.

- [ ] **Step 1: Small plan-level adjustments to Tasks 2 and 7 code (do these first)**

(a) `appointment_edit.py`: give `build_edit_recap` an optional `title: str = "Confira como vai ficar sua consulta:"` keyword (used as the first line instead of the literal; pass it through `_recap_lines`), and add `EDIT_NOTHING_CHANGED = "Você ainda não mudou nada. O que você quer alterar?"` next to the other texts. Add one assertion to `tests/test_appointment_edit_pure.py`: `build_edit_recap(draft, doctor=None, address=None, requirements=[], title="Pronto!").startswith("Pronto!")`.

(b) `appointment_edit_flow.py`: extract the tail of `_after_slot_affecting_change` (from `reslot = draft.with_stage(mode="reslot")` to `return picker`) into a function and call it:

```python
async def _reslot_picker(conversation, tenant, draft, professionals, ctx) -> fr.FlowRouterResult:
    """Day, then time, for the draft's doctor/service - behind the one-line explanation."""
    reslot = draft.with_stage(mode="reslot")
    picker = await fr.enter_day_picker(
        _carrier(conversation, reslot),
        tenant,
        ctx.calendar_for(reslot.current["professional_id"]),
        duration_minutes=reslot.duration_minutes,
        branch=fr.EDIT_DAY_BRANCH,
        anchor=reslot.start,
        professionals=professionals,
    )
    if picker.action == "reply":
        picker.bubbles.insert(0, TextBubble(body=ae.EDIT_RESLOT_NOTICE))
    return picker
```

and make `_after_slot_affecting_change` end with `return await _reslot_picker(conversation, tenant, draft, professionals, ctx)` after the `if free:` return. Re-run `tests/test_appointment_edit_flow_menu.py tests/test_appointment_edit_flow_fields.py` — green before going on.

(c) `tests/_edit_flow_support.py`: `EditCalendar.__init__` gains `fail_update: bool = False` (stored as `self.fail_update`) and `update_event_details` / `create_event` raise `CalendarUnavailableError("down")` when it is set (override `create_event` in `EditCalendar` for that).

- [ ] **Step 2: Write the failing tests**

Create `tests/test_appointment_edit_apply.py`:

```python
"""Confirming the edit: calendar, the same row, reminders, money (TASK-032 R6, spec §5.6)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretaria.models import AppointmentStatus, FlowState
from secretaria.services import appointment_edit as ae
from secretaria.services import flow_router as fr
from secretaria.services.appointment_edit_flow import edit_step
from secretaria.workers import tasks
from secretaria.workers.shared import appointment_edit_support as support
from tests._edit_flow_support import (
    DOCTOR_A,
    DOCTOR_B,
    EditCalendar,
    appointment,
    context,
    conversation,
    draft_for,
    professionals,
    tenant,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import consent, get_conversation, reminder_rows, sent, set_conversation, turn, wire
from tests._reminders_v2 import (
    WA_ID,
    add_reminder,
    get_reminder,
    reload_appointment,
    seed_paid_deposit,
    seed_world,
)


def _moved(draft):
    return draft.with_changes(start_at="2030-10-16T09:00", end_at="2030-10-16T09:40")


async def _confirm(draft, *, ctx, appt=None, patient_name="Maria"):
    appt = appt or appointment()
    conv = conversation(draft, fr.STEP_EDIT_CONFIRM)
    return await edit_step(
        conv, tenant(), fr.LABEL_CONFIRM, [appt], professionals(), ctx, patient_name
    )


# --- The router: what Confirmar asks of the calendar -----------------------------------


async def test_confirming_a_time_change_patches_the_event_and_returns_the_edit():
    calendar = EditCalendar()
    appt = appointment()
    result = await _confirm(_moved(draft_for(appt)), ctx=context(calendar), appt=appt)

    [(event_id, start, end, summary, description)] = calendar.detail_updates
    assert event_id == "evt-old"
    assert (start.hour, start.minute, end.hour, end.minute) == (9, 0, 9, 40)
    assert summary == "Consulta - Maria"
    assert "Convênio: Unimed" in description
    edit = result.appointment_edit
    assert edit["time_changed"] is True and edit["doctor_changed"] is False
    assert edit["google_event_id"] == "evt-old" and edit["old_google_event_id"] == "evt-old"
    assert result.flow_state == FlowState.MENU and result.flow_edit_draft is None
    assert result.bubbles[0].body.startswith(ae.EDIT_APPLIED)
    assert isinstance(result.bubbles[1], fr.MenuBubble)  # the returning-patient menu


async def test_the_draft_never_touches_the_appointment_before_confirm():
    calendar = EditCalendar()
    appt = appointment()
    draft = _moved(draft_for(appt))
    conv = conversation(draft, fr.STEP_EDIT_CONFIRM)

    for body in (ae.LABEL_EDIT_MORE_DATA, fr.LABEL_CANCEL):
        result = await edit_step(conv, tenant(), body, [appt], professionals(), context(calendar))
        assert result.appointment_edit is None
    assert calendar.detail_updates == [] and calendar.created == [] and calendar.cancelled == []


async def test_a_doctor_change_creates_on_the_new_agenda_and_never_deletes_here():
    old, new = EditCalendar(), EditCalendar(free=True)
    appt = appointment()
    draft = draft_for(appt).with_changes(
        professional_id=str(DOCTOR_B), end_at="2030-10-15T15:50"
    )

    result = await _confirm(
        draft, ctx=context(calendars={str(DOCTOR_A): old, str(DOCTOR_B): new}), appt=appt
    )

    assert len(new.created) == 1 and new.detail_updates == []
    edit = result.appointment_edit
    assert edit["doctor_changed"] is True
    assert edit["google_event_id"] == "evt123" and edit["old_google_event_id"] == "evt-old"
    assert edit["old_professional_id"] == DOCTOR_A and edit["professional_id"] == DOCTOR_B
    # The old event is deleted by the worker AFTER the commit, never here.
    assert old.cancelled == [] and old.detail_updates == []


async def test_a_calendar_failure_changes_nothing_and_keeps_the_draft():
    calendar = EditCalendar(fail_update=True)
    appt = appointment()
    draft = _moved(draft_for(appt))

    result = await _confirm(draft, ctx=context(calendar), appt=appt)

    assert result.action == "calendar_unavailable"
    assert result.appointment_edit is None
    assert result.flow_state == FlowState.EDIT_BOOKING and result.flow_step == fr.STEP_EDIT_CONFIRM
    assert result.flow_edit_draft == draft.to_json()


async def test_a_slot_taken_in_the_meantime_goes_back_to_day_and_time():
    calendar = EditCalendar(free=False)
    appt = appointment()

    result = await _confirm(_moved(draft_for(appt)), ctx=context(calendar), appt=appt)

    assert result.appointment_edit is None
    assert result.flow_step == fr.STEP_EDIT_DAY
    assert result.bubbles[0].body == ae.EDIT_RESLOT_NOTICE
    assert calendar.detail_updates == []


async def test_confirming_without_any_change_shows_the_menu_instead():
    appt = appointment()
    result = await _confirm(draft_for(appt), ctx=context(EditCalendar()), appt=appt)
    assert result.appointment_edit is None
    assert result.bubbles[0].body == ae.EDIT_NOTHING_CHANGED
    assert result.flow_step == fr.STEP_EDIT_MENU


async def test_only_the_service_changing_does_not_ask_for_a_free_slot_check():
    calendar = EditCalendar(free=False)  # would fail a slot check; none is needed
    appt = appointment()
    draft = draft_for(appt).with_changes(insurance="Amil")

    result = await _confirm(draft, ctx=context(calendar), appt=appt)

    assert result.appointment_edit is not None
    assert result.appointment_edit["time_changed"] is False
    assert calendar.checked == []


# --- The worker: the same row, the old event, the reminders ----------------------------


@pytest.fixture
def calendar(monkeypatch, db):  # noqa: F811
    return wire(monkeypatch, db)


def _reply(world):
    return tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
    )


def _edit(world, **kw) -> dict:
    start = datetime(2030, 10, 16, 12, 0, tzinfo=UTC)
    base = {
        "appointment_id": world.appointment.id,
        "old_google_event_id": "evt-old",
        "google_event_id": "evt-old",
        "google_event_link": None,
        "appointment_type": "Consulta",
        "professional_id": None,
        "old_professional_id": None,
        "insurance": "Unimed",
        "attendee_name": None,
        "start_at": start,
        "end_at": start + timedelta(minutes=40),
        "time_changed": True,
        "doctor_changed": False,
    }
    base.update(kw)
    return base


async def _apply(world, edit):
    result = fr.FlowRouterResult(
        action="reply",
        bubbles=[fr.MenuBubble(body="menu", labels=["Agendar", "Outro"])],
        flow_state=FlowState.MENU,
        appointment_edit=edit,
    )
    await tasks._apply_flow_result(_reply(world), result, WA_ID, tenant=world.tenant, waba_token="t")


async def test_a_time_change_updates_the_same_row_and_replans_the_reminders(db, calendar):  # noqa: F811
    world = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3), google_event_id="evt-old")
    old_row = await add_reminder(db, world, kind="day", due_at=datetime.now(UTC) + timedelta(days=2))

    await _apply(world, _edit(world))

    row = await reload_appointment(db, world.appointment.id)
    assert row.id == world.appointment.id
    assert row.status == AppointmentStatus.RESCHEDULED
    assert (row.start_at.hour, row.end_at.minute) == (12, 40)
    assert (await get_reminder(db, old_row)).status == "cancelled"
    assert any(r.status == "pending" for r in await reminder_rows(db, world.appointment.id))
    assert calendar.cancelled == []  # same doctor: nothing to delete


async def test_a_doctor_change_creates_the_new_event_then_deletes_the_old_one_after_commit(
    db, calendar  # noqa: F811
):
    world = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3), google_event_id="evt-old")
    new_doctor = uuid4()

    await _apply(
        world,
        _edit(
            world,
            professional_id=new_doctor,
            google_event_id="evt-new",
            doctor_changed=True,
            time_changed=False,
        ),
    )

    row = await reload_appointment(db, world.appointment.id)
    assert row.google_event_id == "evt-new" and row.professional_id == new_doctor
    assert row.status == AppointmentStatus.SCHEDULED  # same time: not "rescheduled"
    assert calendar.cancelled == ["evt-old"]


async def test_service_convenio_and_patient_are_written_to_the_row(db, calendar):  # noqa: F811
    world = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3))

    await _apply(
        world,
        _edit(
            world,
            time_changed=False,
            start_at=world.start_at,
            end_at=world.start_at + timedelta(minutes=20),
            appointment_type="Retorno",
            insurance="Amil",
            attendee_name="Ana Souza",
            google_event_id=world.appointment.google_event_id,
            old_google_event_id=world.appointment.google_event_id,
        ),
    )

    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, row.insurance, row.attendee_name) == ("Retorno", "Amil", "Ana Souza")
    assert row.status == AppointmentStatus.SCHEDULED


async def test_a_cancelled_appointment_is_never_edited(db, calendar):  # noqa: F811
    world = await seed_world(
        db,
        start_at=datetime.now(UTC) + timedelta(days=3),
        status=AppointmentStatus.CANCELLED,
    )
    await _apply(world, _edit(world))
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.CANCELLED


async def test_a_time_change_counts_against_the_pix_reschedule_limit(db, calendar):  # noqa: F811
    from sqlalchemy import select

    from secretaria.models import PixDeposit

    world = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3))
    await seed_paid_deposit(db, world)

    await _apply(world, _edit(world))

    async with db() as session:
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == world.appointment.id)
        )
    assert deposit.reschedule_count == 1


# --- The guards and the context -----------------------------------------------------------


async def test_edit_guards_read_the_deposit_and_the_limit(db):  # noqa: F811
    from sqlalchemy import update

    from secretaria.models import Appointment, PixDeposit, Tenant

    world = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3))
    async with db() as session:
        appointment_row = await session.get(Appointment, world.appointment.id)
        tenant_row = await session.get(Tenant, world.tenant.id)
        assert await support.edit_guards(session, tenant_row, appointment_row) == (False, False)

    await seed_paid_deposit(db, world)
    async with db() as session:
        appointment_row = await session.get(Appointment, world.appointment.id)
        tenant_row = await session.get(Tenant, world.tenant.id)
        assert await support.edit_guards(session, tenant_row, appointment_row) == (True, False)
        await session.execute(
            update(PixDeposit)
            .where(PixDeposit.appointment_id == world.appointment.id)
            .values(reschedule_count=tenant_row.pix_reschedule_limit)
        )
        await session.commit()
    async with db() as session:
        appointment_row = await session.get(Appointment, world.appointment.id)
        tenant_row = await session.get(Tenant, world.tenant.id)
        assert await support.edit_guards(session, tenant_row, appointment_row) == (True, True)


# --- End to end through a real turn -------------------------------------------------------


@pytest.fixture
def edit_calendar(monkeypatch, db):  # noqa: F811
    cal = EditCalendar(slots=[{"start": "2030-10-15T16:00", "end": "2030-10-15T16:40", "label": "16:00"}])

    class _Service:
        @classmethod
        def from_tenant_config(cls, config):
            return cal

    monkeypatch.setattr(support, "CalendarService", _Service)
    wire(monkeypatch, db)
    return cal


async def _edit_world(db):  # noqa: F811
    world = await seed_world(
        db,
        start_at=datetime.now(UTC) + timedelta(days=3),
        last_inbound_at=datetime.now(UTC) - timedelta(minutes=1),
        google_event_id="evt-old",
    )
    await consent(db, world)
    rid = await add_reminder(db, world, kind="day", due_at=datetime.now(UTC) + timedelta(days=2))
    return world, rid


async def _tap(world, action, rid):
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
    )
    await tasks._handle_action_button(reply, action, str(rid))


async def test_alterar_dados_opens_the_edit_menu_as_a_list_on_whatsapp(db, edit_calendar):  # noqa: F811
    world, rid = await _edit_world(db)

    await _tap(world, "remedit", rid)

    [(kind, _to, body, *_rest)] = sent()
    assert kind == "list" and body.startswith("*Alterar Dados*")
    conversation_row = await get_conversation(db, world)
    assert conversation_row.flow_state == FlowState.EDIT_BOOKING
    assert conversation_row.flow_step == fr.STEP_EDIT_MENU
    assert (await get_reminder(db, rid)).answer == "other"


async def test_cancel_on_the_final_card_keeps_the_appointment(db, edit_calendar):  # noqa: F811
    world, rid = await _edit_world(db)
    await _tap(world, "remedit", rid)
    await turn(db, world, ae.LABEL_EDIT_TIME)
    await turn(db, world, "16:00", interactive_reply_id="slot|2030-10-15T16:00")
    assert (await get_conversation(db, world)).flow_step == fr.STEP_EDIT_CONFIRM

    await turn(db, world, fr.LABEL_CANCEL)

    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.SCHEDULED and row.start_at.year != 2030
    assert edit_calendar.detail_updates == []
    assert (await get_conversation(db, world)).flow_edit_draft is None


async def test_the_whole_edit_confirms_and_applies(db, edit_calendar):  # noqa: F811
    world, rid = await _edit_world(db)
    await _tap(world, "remedit", rid)
    await turn(db, world, ae.LABEL_EDIT_TIME)
    await turn(db, world, "16:00", interactive_reply_id="slot|2030-10-15T16:00")

    await turn(db, world, fr.LABEL_CONFIRM)

    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.RESCHEDULED
    assert (row.start_at.year, row.start_at.hour) == (2030, 19)  # 16:00 São Paulo = 19:00 UTC
    [(event_id, *_rest)] = edit_calendar.detail_updates
    assert event_id == "evt-old"
    texts = [item[2] for item in sent() if item[0] in ("text", "buttons")]
    assert texts[-2].startswith(ae.EDIT_APPLIED)
    conversation_row = await get_conversation(db, world)
    assert conversation_row.flow_edit_draft is None and conversation_row.flow_state == FlowState.MENU


async def test_a_foreign_remedit_opens_nothing(db, edit_calendar):  # noqa: F811
    world, rid = await _edit_world(db)
    other = await seed_world(db, phone_number_id="pnid-2", wa_id="5511900002222")

    await _tap(other, "remedit", rid)

    assert (await get_conversation(db, other)).flow_state == FlowState.IDLE
```

Update `tests/test_reminder_r6_actions.py` (the interim assertions Task 5 wrote): in `test_legacy_ids_still_resolve` replace the AI loop by the edit menu — for each of `remother`, `remresched`, `remnew` the tap opens the edit flow (`flow_state == FlowState.EDIT_BOOKING`; one `list` card each, body starting `*Alterar Dados*`); and replace `test_alterar_dados_is_recorded_and_hands_to_the_ai_until_the_edit_flow_lands` by `test_alterar_dados_records_the_answer_and_opens_the_edit_menu` asserting the same two things plus `(await get_reminder(db, rid)).answer == "other"`. Those tests need the tenant calendar patched like `edit_calendar` above (copy the fixture, or move it to `tests/_edit_flow_support.py` as `patched_edit_calendar(monkeypatch, db)` and use it in both files).

- [ ] **Step 3: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_edit_apply.py -q`
Expected: collection error (`appointment_edit_support` missing) — then, after Step 4's module stubs exist, the router tests fail (`_confirm` falls through to `delegate_llm`) and the worker tests fail (`appointment_edit` is ignored).

- [ ] **Step 4: The router's Confirmar**

In `appointment_edit_flow.py` add `from secretaria.services.calendar import CalendarUnavailableError, build_event_description` (merge with the existing import) and, above the dispatcher:

```python
def _unavailable(draft: ae.EditDraft) -> fr.FlowRouterResult:
    """The agenda refused: nothing changed, the draft stays, a human is told (as manage does)."""
    return fr.FlowRouterResult(
        action="calendar_unavailable",
        flow_state=FlowState.EDIT_BOOKING,
        flow_step=fr.STEP_EDIT_CONFIRM,
        flow_managing_appointment_id=_uuid(draft.appointment_id),
        flow_selected_professional_id=_uuid(draft.current["professional_id"]),
        flow_edit_draft=draft.to_json(),
    )


async def _apply_confirm(
    conversation, tenant, draft, appt, professionals, ctx, patient_name
) -> fr.FlowRouterResult:
    """The final Confirmar: the ONLY place the edit reaches the calendar.

    Revalidates (the slot may have been taken since the list was drawn), then patches the
    event (same doctor) or creates it on the new doctor's agenda (the old one is deleted by
    the worker AFTER the database commit - `workers/shared/appointment_edit_apply.py`). The
    row itself is updated by the worker; this returns `appointment_edit` and never persists.
    """
    changed = draft.changed()
    if not changed:
        menu = _menu_result(draft.with_stage(), ctx)
        menu.bubbles.insert(0, TextBubble(body=ae.EDIT_NOTHING_CHANGED))
        return menu
    event_id = str(appt.get("google_event_id") or "")
    new_cal = ctx.calendar_for(draft.current["professional_id"])
    doctor_changed = "médico" in changed
    time_changed = "data" in changed or "horário" in changed
    if not event_id:
        return fr._preserve(conversation, "delegate_llm")
    if new_cal is None or (not doctor_changed and new_cal is None):
        return _unavailable(draft)

    tz = new_cal.tzinfo
    start, end = draft.start.replace(tzinfo=tz), draft.end.replace(tzinfo=tz)
    original_minutes = int(
        (
            datetime.fromisoformat(str(draft.original["end_at"]))
            - datetime.fromisoformat(str(draft.original["start_at"]))
        ).total_seconds()
        // 60
    )
    if time_changed or doctor_changed or draft.duration_minutes != original_minutes:
        try:
            free = await new_cal.is_slot_free(start, end, ignore_event_id=_ignore_event(draft, appt))
        except CalendarUnavailableError:
            return _unavailable(draft)
        if not free:
            return await _reslot_picker(conversation, tenant, draft, professionals, ctx)

    who = draft.current["attendee_name"] or patient_name or "Paciente"
    summary = f"{draft.current['service'] or 'Consulta'} - {who}"
    description = build_event_description(
        service=draft.current["service"],
        insurance=draft.current["insurance"],
        attendee_name=draft.current["attendee_name"],
    )
    try:
        if doctor_changed:
            created = await new_cal.create_event(start, end, summary, description)
            new_event_id, link = str(created.get("id")), created.get("htmlLink")
        else:
            await new_cal.update_event_details(event_id, start, end, summary, description)
            new_event_id, link = event_id, None
    except CalendarUnavailableError:
        return _unavailable(draft)

    edit = {
        "appointment_id": UUID(draft.appointment_id),
        "old_google_event_id": event_id,
        "google_event_id": new_event_id,
        "google_event_link": link,
        "appointment_type": draft.current["service"],
        "professional_id": _uuid(draft.current["professional_id"]),
        "old_professional_id": _uuid(draft.original["professional_id"]),
        "insurance": draft.current["insurance"],
        "attendee_name": draft.current["attendee_name"],
        "start_at": start,
        "end_at": end,
        "time_changed": time_changed,
        "doctor_changed": doctor_changed,
    }
    doctor = _doctor(professionals, draft)
    recap = ae.build_edit_recap(
        draft,
        doctor=str(getattr(doctor, "name", "") or "") or None,
        address=(getattr(tenant, "address", None) or "").strip() or None,
        requirements=_requirements(tenant, professionals, draft),
        title=ae.EDIT_APPLIED,
    )
    logger.info("appointment_edit_confirmed", changed=len(changed), doctor_changed=doctor_changed)
    return fr.FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=recap), *fr._menu_bubbles(tenant, professionals)],
        flow_state=FlowState.MENU,
        appointment_edit=edit,
    )
```

(`if new_cal is None or (not doctor_changed and new_cal is None)` collapses to `if new_cal is None` - write it that way.) In `edit_step`'s `STEP_EDIT_CONFIRM` block add, above the final `return fr._preserve(...)` of that block:

```python
        if match(body, fr.LABEL_CONFIRM):
            return await _apply_confirm(conversation, tenant, draft, appt, professionals, ctx, patient_name)
```

- [ ] **Step 5: The worker modules**

Create `src/secretaria/workers/shared/appointment_edit_support.py`:

```python
"""What an edit turn needs from the database and the calendars (TASK-032 R6).

The router is pure, so the worker prepares its inputs: every agenda the edit may touch
and the Pix guards of the appointment being edited. Built once per edit turn
(`workers/orchestrator.py`) and handed to `route(edit_context=...)`; also used by the
reminder tap that OPENS the edit (`workers/shared/actions.py`).
"""

from typing import Any

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, PixDepositStatus, Tenant
from secretaria.services.appointment_edit import EditContext
from secretaria.services.calendar import CalendarService
from secretaria.services.payments import deposit_lifecycle
from secretaria.services.tenant_config import resolve_professional_calendar

logger = get_logger(__name__)


async def edit_guards(session, tenant: Tenant, appointment: Appointment) -> tuple[bool, bool]:
    """(paid_deposit, reschedule_blocked): what the menu must hide for a paid Pix deposit.

    A PAID deposit hides service / doctor / convênio (the price may differ); a deposit
    whose reschedule counter reached `tenant.pix_reschedule_limit` also hides date / time.
    """
    deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
    paid = deposit is not None and deposit.status == PixDepositStatus.PAID
    blocked = paid and (deposit.reschedule_count or 0) >= (tenant.pix_reschedule_limit or 0)
    return paid, bool(blocked)


async def build_edit_context(
    session,
    tenant: Tenant,
    tenant_config,
    conversation,
    professional_rows: list | None,
    upcoming: list[dict] | None,
) -> EditContext:
    """Every agenda an edit may read or write, plus the guards of the appointment in edit."""
    calendars: dict[str, Any] = {}
    if tenant_config is not None:
        calendars["tenant"] = CalendarService.from_tenant_config(tenant_config)
    for row in professional_rows or []:
        try:
            calendars[str(row.id)] = await resolve_professional_calendar(
                session, tenant, row, tenant_config=tenant_config
            )
        except Exception as exc:  # count-only: the router degrades on a missing agenda
            logger.warning(
                "edit_professional_calendar_failed",
                professional_id=str(row.id),
                error_type=type(exc).__name__,
            )
            calendars[str(row.id)] = None
    paid = blocked = False
    appointment_id = getattr(conversation, "flow_managing_appointment_id", None)
    if appointment_id is not None:
        appointment = await session.get(Appointment, appointment_id)
        if appointment is not None and appointment.tenant_id == tenant.id:
            paid, blocked = await edit_guards(session, tenant, appointment)
    return EditContext(calendars=calendars, paid_deposit=paid, reschedule_blocked=blocked)
```

Create `src/secretaria/workers/shared/appointment_edit_apply.py`:

```python
"""Apply a confirmed "Alterar Dados" edit to the SAME appointment row (TASK-032 R6).

`apply_appointment_edit` runs inside `_apply_flow_result`'s transaction (so a failed
persist rolls the whole edit back together with the flow state); `_finish_edit` runs
AFTER the commit, best-effort, like R3's `replacement._finish_replacement`: the old
Google event of a doctor change is deleted on the agenda that owned it, and a failure is
logged and never undoes the edit the patient just confirmed.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, AppointmentStatus, is_live_status
from secretaria.services import reminder_hooks
from secretaria.services.appointment_status import SOURCE_FLOW, log_status_transition
from secretaria.services.insurance_catalog import resolve_booking_plan_ids
from secretaria.services.payments import deposit_lifecycle
from secretaria.services.tenant_config import list_active_professionals
from secretaria.workers.shared.llm_context import (
    _appointment_calendar,
    _appointment_calendar_target,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class AppliedEdit:
    appointment_id: UUID
    moved: bool  # the time changed AND reminders exist/are enabled -> replan them
    old_event_id: str | None  # delete after the commit (a doctor change)
    old_professional_id: UUID | None


async def apply_appointment_edit(session, *, tenant, tenant_id, edit: dict) -> AppliedEdit | None:
    """Write the edit onto the live appointment; None (nothing written) when it is not live."""
    appointment = await session.scalar(
        select(Appointment).where(
            Appointment.id == edit["appointment_id"], Appointment.tenant_id == tenant_id
        )
    )
    if appointment is None or not is_live_status(appointment.status):
        logger.info("appointment_edit_skipped", tenant_id=str(tenant_id), found=appointment is not None)
        return None
    previous = appointment.status
    plan_id, professional_plan_id = await resolve_booking_plan_ids(
        session, tenant_id, edit["insurance"], edit["professional_id"]
    )
    appointment.appointment_type = edit["appointment_type"] or appointment.appointment_type
    appointment.professional_id = edit["professional_id"]
    appointment.insurance = edit["insurance"]
    appointment.insurance_plan_id = plan_id
    appointment.insurance_professional_plan_id = professional_plan_id
    appointment.attendee_name = edit["attendee_name"]
    appointment.google_event_id = edit["google_event_id"]
    if edit["doctor_changed"]:
        appointment.google_event_link = edit.get("google_event_link")
    appointment.end_at = edit["end_at"]
    if edit["time_changed"]:
        appointment.start_at = edit["start_at"]
        appointment.status = AppointmentStatus.RESCHEDULED
        log_status_transition(
            appointment_id=appointment.id,
            tenant_id=tenant_id,
            old_status=previous,
            new_status=AppointmentStatus.RESCHEDULED,
            source=SOURCE_FLOW,
            idempotency_key=f"edit:{appointment.id}:{edit['start_at'].isoformat()}",
        )
        if tenant is not None:
            allowed, _count = await deposit_lifecycle.register_reschedule(
                session, tenant=tenant, appointment=appointment
            )
            if not allowed:
                # Pre-checked when the menu was drawn; never unwind a persisted edit over a race.
                logger.warning(
                    "pix_reschedule_limit_race",
                    tenant_id=str(tenant_id),
                    appointment_id=str(appointment.id),
                )
    moved = bool(edit["time_changed"]) and (
        reminder_hooks.enabled_for(tenant) or (appointment.confirmation_count or 0) > 0
    )
    return AppliedEdit(
        appointment_id=appointment.id,
        moved=moved,
        old_event_id=edit["old_google_event_id"] if edit["doctor_changed"] else None,
        old_professional_id=edit.get("old_professional_id"),
    )


async def _finish_edit(tenant, applied: AppliedEdit) -> None:
    """Delete the old Google event of a doctor change on its own agenda. Never raises."""
    if not applied.old_event_id:
        return
    try:
        async with async_session_factory() as session:
            rows = await list_active_professionals(session, tenant.id)
            target = _appointment_calendar_target(
                {"professional_id": applied.old_professional_id}, rows
            )
            calendar = await _appointment_calendar(session, tenant, target)
        if calendar is None:
            logger.warning(
                "appointment_edit_calendar_missing",
                tenant_id=str(tenant.id),
                appointment_id=str(applied.appointment_id),
            )
            return
        await calendar.cancel_event(applied.old_event_id)
    except Exception as exc:
        logger.warning(
            "appointment_edit_old_event_delete_failed",
            tenant_id=str(tenant.id),
            appointment_id=str(applied.appointment_id),
            error_type=type(exc).__name__,
        )
```

- [ ] **Step 6: Wire `_apply_flow_result`, `_run_flow` and the orchestrator**

(a) `workers/shared/flow_runner.py`: import `from secretaria.workers.shared.appointment_edit_apply import AppliedEdit, _finish_edit, apply_appointment_edit` and `STEP_EDIT_ATT_AUTH` from `flow_router`; declare `applied_edit: AppliedEdit | None = None` next to `replaced`; change the attendee pin condition to `if result.flow_step in (STEP_AWAITING_ATTENDEE_AUTH, STEP_EDIT_ATT_AUTH) and result.flow_attendee_name:`; after the whole `if result.appointment_reschedule:` branch (same indentation, inside `if conv is not None:`) add:

```python
                    if result.appointment_edit:
                        # TASK-032 R6: the confirmed "Alterar Dados" edit - the SAME row, the
                        # same transaction as the flow state it closes.
                        applied_edit = await apply_appointment_edit(
                            session,
                            tenant=tenant,
                            tenant_id=conv.tenant_id,
                            edit=result.appointment_edit,
                        )
                        if applied_edit is not None and applied_edit.moved:
                            moved_appointment_id = applied_edit.appointment_id
```

and, in the post-commit section next to `if replaced is not None: await _finish_replacement(...)`'s sibling code (after the reminder hooks for `moved_appointment_id` run), add `    if persisted and applied_edit is not None and tenant is not None:\n        await _finish_edit(tenant, applied_edit)`. `_run_flow(...)` gains `edit_context=None` and passes `edit_context=edit_context` to `route(...)`.

(b) `workers/orchestrator.py`: import `build_edit_context` (from the new support module) and `EditContext`; next to `manage_calendar_owned = False` declare `edit_context: EditContext | None = None`; add `FlowState.EDIT_BOOKING` to the tuple in `wants_upcoming_appointments` (`conversation.flow_state in (FlowState.MANAGE_BOOKING, FlowState.LLM, FlowState.EDIT_BOOKING)`); after the `elif manage_target == "tenant":` block add:

```python
                    if conversation.flow_state == FlowState.EDIT_BOOKING:
                        # TASK-032 R6: every agenda the edit may touch + the Pix guards.
                        edit_context = await build_edit_context(
                            session, tenant, tenant_config, conversation,
                            professional_rows, upcoming_appointments,
                        )
```

and pass `edit_context=edit_context,` in the `_run_flow(...)` call (next to `manage_calendar_owned=manage_calendar_owned`).

(c) `services/patient_context.py::load_upcoming_appointments`: add `"insurance": appt.insurance,` and `"attendee_name": appt.attendee_name,` to each dict. Run `grep -rn "load_upcoming_appointments" tests` and update any test asserting the exact dict shape (ledger each).

- [ ] **Step 7: The reminder tap opens the edit**

(a) `workers/shared/reminder_actions.py`: add `CONTINUE_EDIT = "reminder_edit"`; in the `if action in (ACTION_CANCEL, *_EDIT_ACTIONS):` block replace the interim lines (`# Interim ...` and `hand_to_ai_text = OTHER_TEXT + also`) with `            return CONTINUE_EDIT, str(appointment.id)`; delete the now-dead `hand_to_ai_text` variable, the trailing `if hand_to_ai_text is not None:` block, `OTHER_TEXT`, and the imports that only they used (`FlowRouterResult`, `FlowState`, `_apply_flow_result`, `REMINDER_ANSWER_OTHER` stays - it is still written); update the module docstring line for "Alterar Dados" (already correct).

(b) `workers/shared/actions.py`: import `CONTINUE_EDIT` from `reminder_actions`, `enter_edit_menu` from `secretaria.services.appointment_edit_flow`, `EditContext` from `secretaria.services.appointment_edit`, `edit_guards` from `secretaria.workers.shared.appointment_edit_support`; declare `edit_handoff: tuple | None = None` next to `decline_handoff`; add above `if action == "rebookno":`:

```python
        if action == CONTINUE_EDIT:
            # TASK-032 R6: "Alterar Dados" on a reminder (the row was checked against this
            # patient by handle_reminder_button). A fresh draft + the menu; nothing changes yet.
            paid, blocked = await edit_guards(session, tenant, appointment)
            appt_view = {
                "id": str(appointment.id),
                "appointment_type": appointment.appointment_type,
                "start_at": appointment.start_at,
                "end_at": appointment.end_at,
                "professional_id": (
                    str(appointment.professional_id) if appointment.professional_id else None
                ),
                "insurance": appointment.insurance,
                "attendee_name": appointment.attendee_name,
            }
            edit_handoff = (
                tenant,
                waba_token,
                enter_edit_menu(
                    tenant, appt_view, EditContext(paid_deposit=paid, reschedule_blocked=blocked)
                ),
            )
```

and above `if decline_handoff is not None:` add the application block (same shape as the decline one): `await _apply_flow_result(reply, e_result, reply.patient_ref, redis=redis, tenant=e_tenant, waba_token=e_waba)` then `return`.

- [ ] **Step 8: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_edit_apply.py tests/test_appointment_edit_flow_menu.py tests/test_appointment_edit_flow_fields.py tests/test_reminder_r6_actions.py tests/test_reminder_r6_buttons.py tests/test_reminder_r3_cancel_path.py tests/test_reminder_r3_continuations.py tests/test_reminder_r3_portal.py tests/test_reminder_opening_turn.py tests/test_reminder_v2_wiring.py tests/test_workers_layering.py -q`
Expected: all pass. Anything failing because `load_upcoming_appointments` now has two more keys, or because a calendar is built for real in a worker test, is a test-data/patching issue: fix the test and ledger it.

- [ ] **Step 9: Lint and commit**

```bash
uvx ruff format tests/test_appointment_edit_apply.py src/secretaria/workers/shared/appointment_edit_support.py src/secretaria/workers/shared/appointment_edit_apply.py src/secretaria/services/appointment_edit_flow.py
uvx ruff check --fix tests/test_appointment_edit_apply.py src/secretaria/workers/shared/appointment_edit_support.py src/secretaria/workers/shared/appointment_edit_apply.py src/secretaria/services/appointment_edit_flow.py
uvx ruff check src/secretaria/workers src/secretaria/services/appointment_edit.py src/secretaria/services/patient_context.py
git diff --stat
git add src/secretaria tests
git status --short   # review: only files of this task; `git add` by explicit path if anything unrelated shows
git commit -F - <<'EOF'
feat(edit): apply the confirmed Alterar Dados edit and open it from the reminder (TASK-032 R6)

Same appointment row, same transaction as the flow state; the old Google event of a
doctor change is deleted only after the commit; reminders replan when the time moved.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

(In Step 9, replace `git add src/secretaria tests` by the explicit list of paths this task touched - the repo rule is never to add broad directories; the line above is shorthand for "every file named in this task's Files list plus the test files you updated".)

---

## Task 10: Checkpoint, Meta template doc, and the validation

**Files:**
- Create: `docs/CHECKPOINT_lembretes_r6.md`
- Modify: `docs/LEMBRETES_MODELOS_META.md`, `docs/CHECKPOINT_lembretes_r3.md` (one pointer line), `CLAUDE.md` (one line in "Documentação")

Only after Tasks 1–9 are green (`AI_WORKFLOW.md`: docs after validation).

- [ ] **Step 1: Full validation**

Run, from the repo root:

```bash
BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest -q -p no:cacheprovider
```

Expected: green. Record the pass count. A failure also present on `origin/main` 64b7595 (check in a scratch worktree, never `git stash`) is pre-existing: record it with evidence and do not fix it here. Any other failure is a regression of this plan: fix it first.

- [ ] **Step 2: The Meta template note**

In `docs/LEMBRETES_MODELOS_META.md`, where the three quick-reply buttons of `lembrete_consulta_v2` are listed ("Confirmar, Cancelar, Outro"), change the third to `Alterar Dados` and add a short note (Portuguese): "Desde 2026-10-07 (R6) o terceiro botão do cartão interativo chama-se *Alterar Dados* e abre o menu de mudança. Se este modelo já tiver sido aprovado com *Outro*, o botão continua funcionando (o código do botão é por posição e já é o de *Alterar Dados*), mas o rótulo na Meta só muda reenviando o modelo; enquanto isso, fora da janela de 24 h o paciente vê *Outro*."

- [ ] **Step 3: Write the checkpoint**

Create `docs/CHECKPOINT_lembretes_r6.md` (Portuguese prose, code names as anchors, no line numbers) with: **1. Estado** (local, commitado na branch `task/TASK-032-r6-alterar-dados`, SHAs dos commits das Tasks 1–9, NÃO pushado, NÃO deployado; migração `b1c4e7a2d9f3` com `down_revision` = a head da Task 1; resultado do Postgres descartável ou "Postgres run NOT done"; contagem da suíte); **2. O que entrou onde** (cartão `Confirmar / Cancelar / Alterar Dados` em `reminder_text.py`; Confirmar + menu recorrente e Cancelar direto em `reminder_actions.py`; fluxo em `services/appointment_edit.py` + `appointment_edit_flow.py` + `FlowState.EDIT_BOOKING` + `flow_edit_draft`; aplicação em `workers/shared/appointment_edit_apply.py`; menus largos em `workers/shared/bubbles.py`); **3. Regras fixadas** (a lista "Review Focus" deste plano, uma linha cada, com o teste que a prende); **4. Decisões** (D1–D9 e P1–P7 da spec e qualquer ruling do ledger); **5. Pendências** (conferir na tela do Portal que 5 botões desenham bem antes do deploy; reenviar o modelo à Meta; o evento do Google perde a linha "Agendado pelo" ao ser editado; `flow_replaces_appointment_id` e o consumo em `_apply_flow_result`/`_promote_booking_hold` ficam sem uso para uma limpeza futura; disponibilidade ✅/❌ da lista de médicos usa a duração da consulta atual, a duração real é conferida ao escolher; segurança do retorno de editar paciente: o `ConsentEvent` é gravado quando o paciente confirma a frase, mesmo que depois descarte o rascunho; ordem de deploy: banco primeiro, depois API e worker juntos).

Append to `docs/CHECKPOINT_lembretes_r3.md`: `- R6 (2026-10-07): o caminho do Cancelar e o "Agendar Outra" foram substituídos por "Alterar Dados"; ver docs/CHECKPOINT_lembretes_r6.md.` In `CLAUDE.md`, in the "Documentação" section after the R3 line, add: `` `docs/CHECKPOINT_lembretes_r6.md` — TASK-032 R6: lembrete com Confirmar / Cancelar / Alterar Dados e o fluxo de mudança por rascunho; commitado, não deployado; migração `b1c4e7a2d9f3`. ``

- [ ] **Step 4: Commit**

```bash
git diff --stat
git add docs/CHECKPOINT_lembretes_r6.md docs/LEMBRETES_MODELOS_META.md docs/CHECKPOINT_lembretes_r3.md CLAUDE.md
git commit -F - <<'EOF'
docs(reminders): checkpoint for R6 - Alterar Dados (TASK-032)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Self-review (done while writing; kept for the reviewer)

**Spec coverage:** D1 reminder buttons → Task 4; D2 Cancelar direct + motivo → Task 5 (reuses `CONTINUE_CANCEL_AND_ASK_WHY` → `enter_decline_reasons`, tested by R3's `test_yes_cancel_cancels_closes_the_reminders_and_asks_why`); D3 Confirmar + recurring menu → Task 5; D4 menus (5 + Outro) → Tasks 7–8; D5 Portal buttons / WhatsApp list → Task 3; D6 final confirmation + Alterar Mais Dados → Tasks 7, 9; D7 date → time-too → Task 7, time-only Task 7, service of the current doctor Task 8, doctor Task 8; D8 ✅/❌ doctor marks + convênio marks → Task 8; D9 description text → Task 2 (`EDIT_MENU_BODY`) + Task 7 test. P1 ❌ doctor continues to day/time → Task 8 `_after_slot_affecting_change`; P2 create-then-delete → Task 9; P3 time change resets confirmations (R1 hook) / data-only keeps → Task 9 (`moved` only when the time changed); P4 Pix guards → Tasks 7 (menu) + 9 (`edit_guards`); P5 free text to the AI keeps the draft → Tasks 6–8 tests; P6 template label → Task 10; P7 the explanation lives on the menu card (spec §5.1) → Tasks 2, 7.

**Gaps (reported, not built):** the Portal screen is not changed — Task 10's checkpoint lists "confirm in the browser that 5 buttons draw well" as a pre-deploy check; the "Agendado pelo" line of the Google description is not re-added on edit (the router does not know the booking channel); the clearing of `flow_replaces_appointment_id` and its R3 consumers is left for a later clean-up.

**Type/name consistency:** `EditDraft` / `EditContext` / `build_edit_recap(title=)` match between Tasks 2, 7, 8, 9; `STEP_EDIT_*` (15) are defined once in Task 6 and used by name afterwards; `appointment_edit` dict keys match between `_apply_confirm` (Task 9 Step 4), `apply_appointment_edit` (Step 5) and the test `_edit` helper; `edit_guards` returns `(paid, blocked)` in the support module, the actions branch and the test; `CONTINUE_EDIT = "reminder_edit"` is produced in `reminder_actions` and consumed in `actions`; `MenuBubble` labels of the final card are `[fr.LABEL_CONFIRM, fr.LABEL_CANCEL, ae.LABEL_EDIT_MORE_DATA]` in Tasks 7 and 9.

---

## Deploy e liberação

Nada neste plano faz deploy, push ou mexe em produção; cada passo abaixo exige pedido explícito do dono, na ocasião.

1. **Ordem:** banco primeiro — `alembic upgrade head` com a imagem nova (one-off), os dois serviços ainda no código antigo (a coluna é inerte para eles). Depois `secretaria_api` **e** `secretaria-worker` juntos (`GET /build` com paridade `match`). Nenhum front muda; o Portal precisa desenhar 5 botões (conferir antes).
2. **Pré-requisito de produção:** R1–R3 no ar (a migração `a7e2c9d4f1b6` vem antes desta na cadeia).
3. **Liberação:** o interruptor `reminders_v2_enabled` continua desligado nas clínicas; testar na clínica de teste, no navegador e no WhatsApp: lembrete → Alterar Dados → cada opção → mensagem completa → Confirmar (a consulta muda na agenda do Google e no banco) e Cancelar (nada muda).
4. **Reversão:** desligar o interruptor da clínica para de mostrar o cartão novo. Reverter o código: os dois serviços voltam juntos; só depois, se preciso, `alembic downgrade` para `a7e2c9d4f1b6` (perde só uma edição em andamento; a consulta nunca é tocada por um rascunho).

