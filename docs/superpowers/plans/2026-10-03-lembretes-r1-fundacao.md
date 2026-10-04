# Lembretes R1 — Fundação Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every appointment a confirmation counter and a per-reminder schedule table (`appointment_reminders`), add the two per-clinic config columns, expose the confirmation state through the hub agenda API, and route staff "confirmed" through the counter — without sending anything yet.

**Architecture:** One additive Alembic migration (3 columns on `appointments`, 2 on `tenants`, 1 new table). A new service module `services/reminder_schedule.py` owns every rule (schedule, cancel, reschedule, count a confirmation, derive the display state); nothing else writes the counter. `api/hub/calendar.py` only reads the new data and calls the service. R1 defines and tests the functions; wiring them into the creation/reschedule/cancel paths and the cron is R2.

**Tech Stack:** Python 3.12, SQLAlchemy 2 async (Postgres in prod, in-memory SQLite in tests), Alembic, FastAPI, Pydantic v2, pytest-asyncio (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-10-03-lembretes-e-confirmacao-design.md` (sections 4.1, config columns of 4.5, derived display state of 4.1, API part of 4.4). Code base: `main` b72c4ad (+ spec commit 3c39bd0). Worktree: `C:\TECH\BRAIN-worktrees\TASK-032\secretarIA`. All paths below are relative to that repo root; Python package root is `src/secretaria/`.

## Global Constraints

- Everything is additive: nullable or defaulted columns, one new table, new JSON keys only. A deployed worker that does not know the new columns must keep working (skill `frozen-contract-migration`): **never drop or rename** a column in this plan; `downgrade()` exists for local use only.
- `down_revision` of the new migration is `c3a9e5f1d7b2` (current single head, verified with `grep`; Task 2 re-verifies).
- `Appointment.confirmation_count: int` default 0, cap 2. `Tenant.reminders_v2_enabled: bool` default false. `Tenant.reminder_extra_lead_minutes: int | None`.
- Lead times and warning deadlines (spec 4.2/4.4): custom = clinic-configured minutes before start; day = 24 h; hour = 1 h. `warn_due_at` = `due_at` + 2 h (custom), + 2 h (day), + 20 min (hour).
- Same reminder confirmed twice counts once; counter cap 2; staff confirmation counts as one.
- Nothing happens for a clinic with `reminders_v2_enabled = false` (spec criterion 10): `schedule_reminders` returns `[]`.
- Datetimes: store and compare UTC-aware. SQLite returns naive datetimes even for `DateTime(timezone=True)`; every comparison goes through `_as_utc` and every datetime that leaves the API is aware UTC (skill `naive-timestamp-serialization`).
- Tenant isolation: every query on reminders filters by `tenant_id`; the API only resolves rows for the caller's tenant.
- UI/user-facing text Portuguese; code, comments, identifiers English. No PII (names, phones, message text) in logs: ids, kinds, counts only.
- Tests are run from Git Bash: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest <file> -q`. Never run `ruff format .` (repo lint is red at HEAD); run `uvx ruff check` / `uvx ruff format` **only on files this plan creates**; for files it edits use `uvx ruff check <file>` only. Check `git diff --stat` after editing existing files (CRLF files must not show whole-file diffs).
- No deploy, no push, no full suite inside this plan. Commits are local and end with the two attribution lines given in each commit step.

## Review Focus

Failure modes the spec implies; each is pinned by a test in the task named in brackets.

1. Same reminder tapped twice counts once, and a third distinct confirmation stays at 2 while the appointment stays `confirmed`. [Task 5]
2. A late or stale tap (reminder cancelled by a reschedule, appointment already cancelled/attended, reminder id from another tenant or another appointment) must not count and must not raise a 500 for the benign cases. [Task 5]
3. Reschedule zeroes the counter, cancels pending rows of the old version, keeps already-sent history, and recreates rows (also when the new start equals the old one). [Task 4]
4. Cancel / attended / no_show cancels pending rows and clears pending warnings, without touching other appointments. [Task 4, Task 8]
5. Booked too late: rows whose `due_at` is not in the future are skipped, never created already-late; booking again is idempotent. [Task 3]
6. Tenant isolation in rows and API: disabled clinic gets nothing, a foreign appointment is refused, a foreign `google_event_id` never exposes another clinic's confirmation state, PATCH on another clinic's appointment is 404 and leaves counter untouched. [Tasks 3, 5, 7, 8]
7. Naive vs aware datetimes: a naive `start_at` (SQLite read-back) and an aware `now` produce the same rows; API datetimes serialize as aware UTC (`...Z`). [Tasks 3, 7]
8. Migration on SQLite (stub schema, up and down, no drift from the model) and on a disposable Postgres on port 5433 (manual step in Task 2), single Alembic head. [Task 2]
9. Old API clients: existing keys of `GET /events` and `AppointmentRead` unchanged; new keys are additive and default to null/0. [Tasks 7, 8]
10. `PATCH .../status` keeps accepting every transition it accepts today (no new validation); only the counter side effects are new. [Task 8]

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/secretaria/models/appointment_reminder.py` | Create | `AppointmentReminder` model + str constants (kind/status/channel/answer/warn_kind) |
| `src/secretaria/models/appointment.py` | Modify | 3 new columns |
| `src/secretaria/models/tenant.py` | Modify | 2 new columns |
| `src/secretaria/models/__init__.py` | Modify | export the new model |
| `migrations/versions/b8d3f1a6c2e5_appointment_reminders_foundation.py` | Create | the one additive migration |
| `src/secretaria/services/reminder_schedule.py` | Create | schedule / cancel / reschedule / register_confirmation / display_state |
| `src/secretaria/schemas/calendar.py` | Modify | additive response fields + `CalendarReminderRead` |
| `src/secretaria/api/hub/calendar.py` | Modify | `GET /events` fields; PATCH status goes through the counter |
| `tests/_reminder_fixtures.py` | Create | shared in-memory DB fixtures and builders |
| `tests/test_appointment_reminder_model.py` | Create | model defaults/constraints |
| `tests/test_migration_reminders_foundation.py` | Create | migration up/down on SQLite, single head, no model drift |
| `tests/test_reminder_schedule.py` | Create | schedule/cancel/reschedule |
| `tests/test_reminder_confirmation.py` | Create | counter + display state |
| `tests/test_hub_calendar_confirmation.py` | Create | API contract |
| `tests/test_calendar_events_insurance.py` | Modify | two key-set assertions gain the new keys |
| `docs/CHECKPOINT_lembretes_r1.md` | Create | state, what lives where, pending (R2-R5) |

## Interfaces → Produces (R2-R5 depend on these exact names)

```python
# secretaria/models/appointment_reminder.py
class AppointmentReminder(Base):           # table "appointment_reminders"
    id: uuid.UUID                          # pk, default uuid4
    tenant_id: uuid.UUID                   # FK tenants.id CASCADE, indexed
    appointment_id: uuid.UUID              # FK appointments.id CASCADE, indexed
    patient_id: uuid.UUID | None           # FK patients.id SET NULL
    kind: str                              # REMINDER_KIND_*  (String(16))
    appointment_start_at: datetime         # tz-aware; the version of the appointment
    due_at: datetime                       # tz-aware
    status: str                            # REMINDER_STATUS_*  default "pending"
    channel: str                           # REMINDER_CHANNEL_*  default "whatsapp"
    with_prompt: bool                      # default True
    attempts: int                          # default 0
    last_error_code: str | None            # String(64)
    sent_at: datetime | None
    answered_at: datetime | None
    answer: str | None                     # REMINDER_ANSWER_*
    warn_due_at: datetime | None
    warned_at: datetime | None
    warn_kind: str | None                  # REMINDER_WARN_*  (String(24))
    created_at: datetime
    updated_at: datetime

REMINDER_KIND_CUSTOM = "custom"; REMINDER_KIND_DAY = "day"
REMINDER_KIND_HOUR = "hour";     REMINDER_KIND_CHAT = "chat"
REMINDER_KINDS: tuple[str, ...]
REMINDER_STATUS_PENDING = "pending"; REMINDER_STATUS_SENDING = "sending"
REMINDER_STATUS_SENT = "sent";       REMINDER_STATUS_FAILED = "failed"
REMINDER_STATUS_SKIPPED = "skipped"; REMINDER_STATUS_CANCELLED = "cancelled"
REMINDER_STATUSES: tuple[str, ...]
REMINDER_CHANNEL_WHATSAPP = "whatsapp"; REMINDER_CHANNEL_EMAIL = "email"
REMINDER_CHANNEL_CHAT = "chat"
REMINDER_CHANNELS: tuple[str, ...]
REMINDER_ANSWER_CONFIRM = "confirm"; REMINDER_ANSWER_CANCEL = "cancel"
REMINDER_ANSWER_OTHER = "other"
REMINDER_ANSWERS: tuple[str, ...]
REMINDER_WARN_UNCONFIRMED = "unconfirmed"; REMINDER_WARN_DELIVERY_FAILED = "delivery_failed"
REMINDER_WARN_KINDS: tuple[str, ...]

# Appointment: confirmation_count: int (0..2), first_confirmed_at: datetime | None,
#              last_confirmed_at: datetime | None
# Tenant:      reminders_v2_enabled: bool, reminder_extra_lead_minutes: int | None

# secretaria/services/reminder_schedule.py
MAX_CONFIRMATIONS = 2
CONFIRMATION_SOURCE_REMINDER_BUTTON = "reminder_button"
CONFIRMATION_SOURCE_CHAT_PROMPT = "chat_prompt"
CONFIRMATION_SOURCE_STAFF = "staff"
DISPLAY_UNCONFIRMED = "unconfirmed"; DISPLAY_CONFIRMED = "confirmed"
DISPLAY_CONFIRMED_TWICE = "confirmed_twice"; DISPLAY_ATTENTION = "attention"
class ReminderMismatchError(ValueError): ...

async def schedule_reminders(session: AsyncSession, appointment: Appointment, tenant: Tenant, *, now: datetime) -> list[AppointmentReminder]
async def cancel_reminders(session: AsyncSession, appointment_id: UUID, *, reason: str) -> int
async def reschedule_reminders(session: AsyncSession, appointment: Appointment, tenant: Tenant, *, now: datetime) -> list[AppointmentReminder]
async def register_confirmation(session: AsyncSession, *, appointment: Appointment, reminder_id: UUID | None, source: str, now: datetime) -> int
def reset_confirmation(appointment: Appointment) -> None
def display_state(appointment, reminders) -> str   # one of DISPLAY_*
```

Semantics R2-R5 must rely on: none of these functions commits (callers own the transaction; they `flush`); `cancel_reminders` takes no tenant (the caller already loaded the appointment tenant-scoped); `schedule_reminders` creates rows with `status="pending"`, `channel="whatsapp"` (R2 sets the real channel at send time) and `with_prompt = (appointment.confirmation_count < 2)` (R2 must recompute `with_prompt` at send time); `register_confirmation` with `reminder_id=None` only counts when the counter is 0.

---

## Task 1: Model and columns

**Files:**
- Create: `src/secretaria/models/appointment_reminder.py`
- Modify: `src/secretaria/models/appointment.py` (imports line `from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, String, func`; add columns after `status`)
- Modify: `src/secretaria/models/tenant.py` (columns after `insurance_mode`)
- Modify: `src/secretaria/models/__init__.py`
- Create: `tests/_reminder_fixtures.py`
- Test: `tests/test_appointment_reminder_model.py`

**Interfaces:**
- Consumes: `Base` from `secretaria.core.database`.
- Produces: everything under "models" in the Interfaces block above.

- [ ] **Step 1: Write the shared fixtures**

Create `tests/_reminder_fixtures.py`:

```python
"""Shared in-memory fixtures for the reminder foundation tests (TASK-032 R1).

Import the fixtures into a test module with
`from tests._reminder_fixtures import db, tenant, other_tenant  # noqa: F401`.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Appointment, AppointmentStatus, Patient, Tenant  # noqa: E402

# A fixed "now" so every schedule assertion is deterministic.
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _make_tenant(db, name: str, *, enabled: bool, extra_lead: int | None) -> Tenant:
    async with db() as session:
        t = Tenant(
            id=uuid4(),
            clinic_name=name,
            phone_number_id=None,
            reminders_v2_enabled=enabled,
            reminder_extra_lead_minutes=extra_lead,
        )
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


@pytest_asyncio.fixture
async def tenant(db) -> Tenant:
    """Clinic with the feature ON and a 5-day extra reminder (7200 min)."""
    return await _make_tenant(db, "Clinic", enabled=True, extra_lead=7200)


@pytest_asyncio.fixture
async def other_tenant(db) -> Tenant:
    return await _make_tenant(db, "Other clinic", enabled=True, extra_lead=7200)


async def make_patient(db, tenant: Tenant) -> Patient:
    async with db() as session:
        p = Patient(tenant_id=tenant.id, wa_id=f"55119{uuid4().int % 10**8:08d}")
        session.add(p)
        await session.commit()
        await session.refresh(p)
        return p


async def make_appointment(
    db,
    tenant: Tenant,
    *,
    start_at: datetime,
    status: AppointmentStatus = AppointmentStatus.SCHEDULED,
    with_patient: bool = True,
    google_event_id: str | None = None,
) -> Appointment:
    patient = await make_patient(db, tenant) if with_patient else None
    async with db() as session:
        appt = Appointment(
            tenant_id=tenant.id,
            patient_id=patient.id if patient else None,
            google_event_id=google_event_id or f"evt-{uuid4()}",
            appointment_type="Consulta",
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            status=status,
            phone="5511999999999",
        )
        session.add(appt)
        await session.commit()
        await session.refresh(appt)
        return appt
```

- [ ] **Step 2: Write the failing model test**

Create `tests/test_appointment_reminder_model.py`:

```python
"""AppointmentReminder model + the new columns (TASK-032 R1, spec 4.1/4.5)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from secretaria.models import Appointment, AppointmentReminder, Tenant
from secretaria.models import appointment_reminder as ar
from tests._reminder_fixtures import NOW, db, make_appointment, tenant  # noqa: F401


def test_string_constants_are_the_spec_vocabulary():
    assert ar.REMINDER_KINDS == ("custom", "day", "hour", "chat")
    assert ar.REMINDER_STATUSES == (
        "pending", "sending", "sent", "failed", "skipped", "cancelled",
    )
    assert ar.REMINDER_CHANNELS == ("whatsapp", "email", "chat")
    assert ar.REMINDER_ANSWERS == ("confirm", "cancel", "other")
    assert ar.REMINDER_WARN_KINDS == ("unconfirmed", "delivery_failed")


async def test_new_columns_have_safe_defaults(db, tenant):  # noqa: F811
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=3))
    async with db() as session:
        fresh_appt = await session.get(Appointment, appt.id)
        fresh_tenant = await session.get(Tenant, tenant.id)
    assert fresh_appt.confirmation_count == 0
    assert fresh_appt.first_confirmed_at is None
    assert fresh_appt.last_confirmed_at is None
    # `tenant` fixture sets the flag explicitly; a bare Tenant must default OFF.
    async with db() as session:
        bare = Tenant(id=uuid4(), clinic_name="Bare", phone_number_id=None)
        session.add(bare)
        await session.commit()
        await session.refresh(bare)
    assert bare.reminders_v2_enabled is False
    assert bare.reminder_extra_lead_minutes is None
    assert fresh_tenant.reminders_v2_enabled is True


async def test_reminder_row_defaults(db, tenant):  # noqa: F811
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=3))
    async with db() as session:
        row = AppointmentReminder(
            tenant_id=tenant.id,
            appointment_id=appt.id,
            patient_id=appt.patient_id,
            kind=ar.REMINDER_KIND_DAY,
            appointment_start_at=appt.start_at,
            due_at=appt.start_at - timedelta(days=1),
        )
        session.add(row)
        await session.commit()
        loaded = (await session.scalars(select(AppointmentReminder))).one()
    assert loaded.status == ar.REMINDER_STATUS_PENDING
    assert loaded.channel == ar.REMINDER_CHANNEL_WHATSAPP
    assert loaded.with_prompt is True
    assert loaded.attempts == 0
    assert loaded.sent_at is None and loaded.answered_at is None and loaded.answer is None
    assert loaded.warn_due_at is None and loaded.warned_at is None and loaded.warn_kind is None


async def test_one_row_per_appointment_kind_and_version(db, tenant):  # noqa: F811
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=3))

    def _row(start: datetime) -> AppointmentReminder:
        return AppointmentReminder(
            tenant_id=tenant.id,
            appointment_id=appt.id,
            patient_id=appt.patient_id,
            kind=ar.REMINDER_KIND_HOUR,
            appointment_start_at=start,
            due_at=start - timedelta(hours=1),
        )

    start = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    async with db() as session:
        session.add(_row(start))
        await session.commit()
    async with db() as session:
        session.add(_row(start))  # same (appointment, kind, version)
        with pytest.raises(IntegrityError):
            await session.commit()
    async with db() as session:
        session.add(_row(start + timedelta(days=1)))  # a new version is fine
        await session.commit()
```

- [ ] **Step 3: Run it to verify it fails**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_reminder_model.py -q`
Expected: collection error `ImportError: cannot import name 'AppointmentReminder' from 'secretaria.models'`.

- [ ] **Step 4: Create the model**

Create `src/secretaria/models/appointment_reminder.py`:

```python
"""One planned (or shown) reminder of one appointment - TASK-032, spec 4.1.

The schedule lives in the database, one row per reminder, instead of being
derived from time windows on every cron sweep (`plugins/reminders.py`). A row
is versioned by `appointment_start_at`: rescheduling cancels the rows of the old
start and creates new ones, so a moved booking gets fresh reminders and an old
button tap can be recognised as stale.

Vocabulary columns (`kind`, `status`, `channel`, `answer`, `warn_kind`) are plain
strings with the constants below, NOT native Postgres enums: a native enum is
what made the `appointment_status` type painful to extend, and these sets will
grow (R2-R4). Services validate against the tuples; the database does not.

PII: this table holds ids, timestamps and codes only. `last_error_code` is a
short code, never provider text, and never anything the patient wrote.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base

REMINDER_KIND_CUSTOM = "custom"  # clinic-configured lead time
REMINDER_KIND_DAY = "day"  # 24 h before
REMINDER_KIND_HOUR = "hour"  # 1 h before
REMINDER_KIND_CHAT = "chat"  # the opening message of the chat (born already sent)
REMINDER_KINDS: tuple[str, ...] = (
    REMINDER_KIND_CUSTOM,
    REMINDER_KIND_DAY,
    REMINDER_KIND_HOUR,
    REMINDER_KIND_CHAT,
)

REMINDER_STATUS_PENDING = "pending"
REMINDER_STATUS_SENDING = "sending"
REMINDER_STATUS_SENT = "sent"
REMINDER_STATUS_FAILED = "failed"
REMINDER_STATUS_SKIPPED = "skipped"
REMINDER_STATUS_CANCELLED = "cancelled"
REMINDER_STATUSES: tuple[str, ...] = (
    REMINDER_STATUS_PENDING,
    REMINDER_STATUS_SENDING,
    REMINDER_STATUS_SENT,
    REMINDER_STATUS_FAILED,
    REMINDER_STATUS_SKIPPED,
    REMINDER_STATUS_CANCELLED,
)

REMINDER_CHANNEL_WHATSAPP = "whatsapp"
REMINDER_CHANNEL_EMAIL = "email"
REMINDER_CHANNEL_CHAT = "chat"
REMINDER_CHANNELS: tuple[str, ...] = (
    REMINDER_CHANNEL_WHATSAPP,
    REMINDER_CHANNEL_EMAIL,
    REMINDER_CHANNEL_CHAT,
)

REMINDER_ANSWER_CONFIRM = "confirm"
REMINDER_ANSWER_CANCEL = "cancel"
REMINDER_ANSWER_OTHER = "other"
REMINDER_ANSWERS: tuple[str, ...] = (
    REMINDER_ANSWER_CONFIRM,
    REMINDER_ANSWER_CANCEL,
    REMINDER_ANSWER_OTHER,
)

REMINDER_WARN_UNCONFIRMED = "unconfirmed"
REMINDER_WARN_DELIVERY_FAILED = "delivery_failed"
REMINDER_WARN_KINDS: tuple[str, ...] = (
    REMINDER_WARN_UNCONFIRMED,
    REMINDER_WARN_DELIVERY_FAILED,
)

# Partial-index predicate: only warnings that still have to go out.
_UNWARNED = text("warned_at IS NULL AND warn_due_at IS NOT NULL")


class AppointmentReminder(Base):
    """A reminder of one appointment version (see the module docstring)."""

    __tablename__ = "appointment_reminders"
    __table_args__ = (
        # The cron claims `status = 'pending' AND due_at <= now`.
        Index("ix_appointment_reminders_status_due", "status", "due_at"),
        # The warning cron only scans warnings not yet sent (small, hot set).
        Index(
            "ix_appointment_reminders_warn_due",
            "warn_due_at",
            postgresql_where=_UNWARNED,
            sqlite_where=_UNWARNED,
        ),
        UniqueConstraint(
            "appointment_id",
            "kind",
            "appointment_start_at",
            name="uq_appointment_reminders_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), index=True
    )
    # SET NULL: erasing a patient (LGPD) keeps the appointment history intact.
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("patients.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    # The appointment's start when this row was created (its version).
    appointment_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(16), default=REMINDER_STATUS_PENDING, server_default=REMINDER_STATUS_PENDING
    )
    channel: Mapped[str] = mapped_column(
        String(16), default=REMINDER_CHANNEL_WHATSAPP, server_default=REMINDER_CHANNEL_WHATSAPP
    )
    # False once the patient confirmed twice: the message goes out as a plain
    # reminder without the Confirm button.
    with_prompt: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answer: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # When to warn the clinic if the appointment is still unconfirmed.
    warn_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    warned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    warn_kind: Mapped[str | None] = mapped_column(String(24), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
```

- [ ] **Step 5: Add the columns and the export**

In `src/secretaria/models/appointment.py` change the import line to
`from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Integer, String, func, text`
and, directly after the `status` column (before `created_at`), add:

```python
    # TASK-032 (spec 4.1): how many DISTINCT reminders/prompts the patient (or
    # staff) confirmed, capped at 2 by services/reminder_schedule.py - the only
    # writer. `status == CONFIRMED` follows `confirmation_count >= 1`;
    # rescheduling zeroes it. Two confirmations stop the confirmation prompts.
    confirmation_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    first_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

In `src/secretaria/models/tenant.py`, after the `insurance_mode` column add:

```python
    # TASK-032 (spec 4.5): master switch of the reminder schedule / confirmation
    # state. Default OFF for every clinic; turn it on per clinic after the test
    # clinic proves it. While False nothing is scheduled and nothing is sent by
    # the new engine.
    reminders_v2_enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), default=False
    )
    # TASK-032 (spec 4.5): lead time, in minutes before the appointment, of the
    # clinic-configured ("custom") reminder. NULL = the clinic has no extra one.
    reminder_extra_lead_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

In `src/secretaria/models/__init__.py` add (keeping import order alphabetical, after the `appointment` import block):

```python
from secretaria.models.appointment_reminder import AppointmentReminder
```

and add `"AppointmentReminder",` to `__all__` right after `"AppointmentStatus",`.

- [ ] **Step 6: Run the test to verify it passes**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_reminder_model.py -q`
Expected: `4 passed`.

- [ ] **Step 7: Lint and diff check**

Run: `uvx ruff check src/secretaria/models/appointment_reminder.py tests/_reminder_fixtures.py tests/test_appointment_reminder_model.py src/secretaria/models/appointment.py src/secretaria/models/tenant.py src/secretaria/models/__init__.py` then `uvx ruff format src/secretaria/models/appointment_reminder.py tests/_reminder_fixtures.py tests/test_appointment_reminder_model.py` (created files only), then `git diff --stat`.
Expected: no ruff errors in the created files (pre-existing findings in edited files are not yours); `git diff --stat` shows only small insertions in the three edited files (no whole-file rewrites).

- [ ] **Step 8: Commit**

```bash
git add src/secretaria/models/appointment_reminder.py src/secretaria/models/appointment.py src/secretaria/models/tenant.py src/secretaria/models/__init__.py tests/_reminder_fixtures.py tests/test_appointment_reminder_model.py
git commit -m "feat(reminders): appointment_reminders model and confirmation columns (TASK-032 R1)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ"
```

---

## Task 2: The additive migration

**Files:**
- Create: `migrations/versions/b8d3f1a6c2e5_appointment_reminders_foundation.py`
- Test: `tests/test_migration_reminders_foundation.py`

**Interfaces:**
- Consumes: Task 1 model (the drift test compares against `AppointmentReminder.__table__`).
- Produces: Alembic revision `b8d3f1a6c2e5` (head). R3 will chain its `conversations` migration after this one.

- [ ] **Step 1: Verify the head and that the id is free**

Run: `grep -rn "b8d3f1a6c2e5" migrations/ ; grep -rn "down_revision.*c3a9e5f1d7b2" migrations/versions`
Expected: no output from either (the id is unused and nothing chains after `c3a9e5f1d7b2` yet). If something already chains after it, STOP and use that revision as `down_revision` instead, updating the test constants.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_migration_reminders_foundation.py`:

```python
"""The R1 migration: additive, reversible, in sync with the model (TASK-032).

The full Alembic chain cannot run on SQLite (older revisions are Postgres
specific), so the migration module is loaded directly and its upgrade() /
downgrade() run against a stub schema holding just the tables it touches. The
Postgres run is the manual step in the plan (Task 2, Step 6).
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from secretaria.models import AppointmentReminder

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "migrations" / "versions" / "b8d3f1a6c2e5_appointment_reminders_foundation.py"


def _load():
    spec = importlib.util.spec_from_file_location("mig_b8d3f1a6c2e5", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_engine() -> sa.Engine:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        for name in ("tenants", "patients", "appointments"):
            conn.exec_driver_sql(f"CREATE TABLE {name} (id CHAR(32) PRIMARY KEY)")
    return engine


def _run(engine: sa.Engine, fn_name: str) -> None:
    module = _load()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            getattr(module, fn_name)()


def test_there_is_exactly_one_head_and_it_is_ours():
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == ["b8d3f1a6c2e5"]
    assert _load().down_revision == "c3a9e5f1d7b2"


def test_upgrade_adds_columns_and_table_then_downgrade_removes_them():
    engine = _stub_engine()
    _run(engine, "upgrade")
    insp = sa.inspect(engine)

    appt_cols = {c["name"]: c for c in insp.get_columns("appointments")}
    assert {"confirmation_count", "first_confirmed_at", "last_confirmed_at"} <= set(appt_cols)
    assert appt_cols["confirmation_count"]["nullable"] is False
    assert appt_cols["first_confirmed_at"]["nullable"] is True

    tenant_cols = {c["name"]: c for c in insp.get_columns("tenants")}
    assert tenant_cols["reminders_v2_enabled"]["nullable"] is False
    assert tenant_cols["reminder_extra_lead_minutes"]["nullable"] is True

    index_names = {i["name"] for i in insp.get_indexes("appointment_reminders")}
    assert {
        "ix_appointment_reminders_status_due",
        "ix_appointment_reminders_warn_due",
        "ix_appointment_reminders_tenant_id",
        "ix_appointment_reminders_appointment_id",
    } <= index_names
    uniques = {u["name"] for u in insp.get_unique_constraints("appointment_reminders")}
    assert "uq_appointment_reminders_version" in uniques

    # Existing rows get the safe defaults (additive: old rows stay valid).
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO appointments (id) VALUES ('a1')")
        conn.exec_driver_sql("INSERT INTO tenants (id) VALUES ('t1')")
        assert conn.exec_driver_sql(
            "SELECT confirmation_count FROM appointments"
        ).scalar() == 0
        assert not conn.exec_driver_sql(
            "SELECT reminders_v2_enabled FROM tenants"
        ).scalar()

    _run(engine, "downgrade")
    insp = sa.inspect(engine)
    assert "appointment_reminders" not in insp.get_table_names()
    assert "confirmation_count" not in {c["name"] for c in insp.get_columns("appointments")}
    assert "reminders_v2_enabled" not in {c["name"] for c in insp.get_columns("tenants")}


def test_migrated_table_has_exactly_the_model_columns():
    engine = _stub_engine()
    _run(engine, "upgrade")
    migrated = {c["name"] for c in sa.inspect(engine).get_columns("appointment_reminders")}
    modelled = {c.name for c in AppointmentReminder.__table__.columns}
    assert migrated == modelled
```

- [ ] **Step 3: Run it to verify it fails**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_migration_reminders_foundation.py -q`
Expected: FAIL (`FileNotFoundError` on the migration path / head mismatch).

- [ ] **Step 4: Write the migration**

Create `migrations/versions/b8d3f1a6c2e5_appointment_reminders_foundation.py`:

```python
"""appointment reminders foundation: confirmation counter, schedule table, clinic config.

TASK-032 R1 (spec 4.1 / 4.5). Purely additive: three nullable/defaulted columns
on appointments, two on tenants, one new table. Nothing is dropped or renamed,
so an API/worker image from before this revision keeps working against the
migrated database (it simply never reads the new columns). Deploy order:
migration first, then API and worker together.

Revision ID: b8d3f1a6c2e5
Revises: c3a9e5f1d7b2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d3f1a6c2e5"
down_revision: str | None = "c3a9e5f1d7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNWARNED = "warned_at IS NULL AND warn_due_at IS NOT NULL"


def upgrade() -> None:
    op.add_column(
        "appointments",
        sa.Column(
            "confirmation_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "appointments",
        sa.Column("first_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "reminders_v2_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "tenants",
        sa.Column("reminder_extra_lead_minutes", sa.Integer(), nullable=True),
    )

    op.create_table(
        "appointment_reminders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "appointment_id",
            sa.Uuid(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "patient_id",
            sa.Uuid(),
            sa.ForeignKey("patients.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("appointment_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column(
            "channel", sa.String(length=16), nullable=False, server_default="whatsapp"
        ),
        sa.Column(
            "with_prompt", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answer", sa.String(length=16), nullable=True),
        sa.Column("warn_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("warned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("warn_kind", sa.String(length=24), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "appointment_id",
            "kind",
            "appointment_start_at",
            name="uq_appointment_reminders_version",
        ),
    )
    op.create_index("ix_appointment_reminders_tenant_id", "appointment_reminders", ["tenant_id"])
    op.create_index(
        "ix_appointment_reminders_appointment_id", "appointment_reminders", ["appointment_id"]
    )
    op.create_index(
        "ix_appointment_reminders_status_due", "appointment_reminders", ["status", "due_at"]
    )
    op.create_index(
        "ix_appointment_reminders_warn_due",
        "appointment_reminders",
        ["warn_due_at"],
        postgresql_where=sa.text(_UNWARNED),
        sqlite_where=sa.text(_UNWARNED),
    )


def downgrade() -> None:
    # Local/dev only: never run against a database a newer image still uses.
    op.drop_table("appointment_reminders")
    op.drop_column("tenants", "reminder_extra_lead_minutes")
    op.drop_column("tenants", "reminders_v2_enabled")
    op.drop_column("appointments", "last_confirmed_at")
    op.drop_column("appointments", "first_confirmed_at")
    op.drop_column("appointments", "confirmation_count")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_migration_reminders_foundation.py tests/test_appointment_reminder_model.py -q`
Expected: `7 passed`.

- [ ] **Step 6: Run the migration on a disposable Postgres (port 5433, manual, not a CI step)**

Use a throwaway database; never the real `brain-postgres`/production URL, and never read env files of Easypanel.

```bash
docker run -d --rm --name r1-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_USER=secretaria -e POSTGRES_DB=secretaria -p 5433:5432 postgres:16
export DATABASE_URL="postgresql+asyncpg://secretaria:pw@localhost:5433/secretaria"
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
uv run alembic current
docker exec r1-pg psql -U secretaria -d secretaria -c "\d appointment_reminders"
docker stop r1-pg
```

Expected: upgrade, downgrade and upgrade finish without error; `alembic current` prints `b8d3f1a6c2e5 (head)`; `\d appointment_reminders` lists the 4 indexes (`ix_appointment_reminders_warn_due` shown with `WHERE (warned_at IS NULL AND warn_due_at IS NOT NULL)`) and the unique constraint `uq_appointment_reminders_version`. If Docker is unavailable on the machine, record "Postgres run NOT done" in the checkpoint doc (Task 9) instead of skipping silently.

- [ ] **Step 7: Lint and commit**

Run: `uvx ruff check migrations/versions/b8d3f1a6c2e5_appointment_reminders_foundation.py tests/test_migration_reminders_foundation.py` and `uvx ruff format` on those two created files.
Expected: clean.

```bash
git add migrations/versions/b8d3f1a6c2e5_appointment_reminders_foundation.py tests/test_migration_reminders_foundation.py
git commit -m "feat(reminders): additive migration for the reminder schedule (TASK-032 R1)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ"
```

---

## Task 3: `schedule_reminders`

**Files:**
- Create: `src/secretaria/services/reminder_schedule.py`
- Test: `tests/test_reminder_schedule.py`

**Interfaces:**
- Consumes: `AppointmentReminder` and constants (Task 1); `Appointment`, `Tenant`; `is_live_status` from `secretaria.models`.
- Produces: `schedule_reminders`, `_as_utc`, `MAX_CONFIRMATIONS` (rest of the module in Tasks 4-6).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reminder_schedule.py`:

```python
"""schedule_reminders: which rows exist for an appointment (TASK-032 R1, spec 4.2)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from secretaria.models import AppointmentReminder, AppointmentStatus, Tenant
from secretaria.services.reminder_schedule import schedule_reminders
from tests._reminder_fixtures import (  # noqa: F401
    NOW,
    db,
    make_appointment,
    other_tenant,
    tenant,
)


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _schedule(db, tenant_id, appt_id, *, now=NOW):
    async with db() as session:
        t = await session.get(Tenant, tenant_id)
        from secretaria.models import Appointment

        a = await session.get(Appointment, appt_id)
        rows = await schedule_reminders(session, a, t, now=now)
        await session.commit()
        return rows


async def _all_rows(db, appt_id):
    async with db() as session:
        return list(
            await session.scalars(
                select(AppointmentReminder)
                .where(AppointmentReminder.appointment_id == appt_id)
                .order_by(AppointmentReminder.due_at)
            )
        )


async def test_far_appointment_gets_custom_day_and_hour_with_warn_deadlines(db, tenant):  # noqa: F811
    start = NOW + timedelta(days=6)
    appt = await make_appointment(db, tenant, start_at=start)

    created = await _schedule(db, tenant.id, appt.id)

    assert [r.kind for r in created] == ["custom", "day", "hour"]
    rows = {r.kind: r for r in await _all_rows(db, appt.id)}
    assert _utc(rows["custom"].due_at) == start - timedelta(minutes=7200)
    assert _utc(rows["day"].due_at) == start - timedelta(hours=24)
    assert _utc(rows["hour"].due_at) == start - timedelta(hours=1)
    assert _utc(rows["custom"].warn_due_at) == _utc(rows["custom"].due_at) + timedelta(hours=2)
    assert _utc(rows["day"].warn_due_at) == _utc(rows["day"].due_at) + timedelta(hours=2)
    assert _utc(rows["hour"].warn_due_at) == _utc(rows["hour"].due_at) + timedelta(minutes=20)
    for r in rows.values():
        assert r.status == "pending" and r.with_prompt is True
        assert r.tenant_id == tenant.id and r.patient_id == appt.patient_id
        assert _utc(r.appointment_start_at) == start


async def test_no_custom_row_when_the_clinic_did_not_configure_one(db, tenant):  # noqa: F811
    async with db() as session:
        t = await session.get(Tenant, tenant.id)
        t.reminder_extra_lead_minutes = None
        await session.commit()
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))

    created = await _schedule(db, tenant.id, appt.id)

    assert [r.kind for r in created] == ["day", "hour"]


@pytest.mark.parametrize(
    ("lead", "expected"),
    [
        (timedelta(days=2), ["day", "hour"]),  # custom (5 d) already past
        (timedelta(hours=20), ["hour"]),  # day and custom already past
        (timedelta(hours=3), ["hour"]),
        (timedelta(minutes=30), []),  # booked too late for any reminder
        (timedelta(hours=1), []),  # hour reminder due exactly now: not in the future
    ],
)
async def test_reminders_already_due_are_skipped_not_created(db, tenant, lead, expected):  # noqa: F811
    appt = await make_appointment(db, tenant, start_at=NOW + lead)

    created = await _schedule(db, tenant.id, appt.id)

    assert [r.kind for r in created] == expected
    assert [r.kind for r in await _all_rows(db, appt.id)] == expected


async def test_scheduling_twice_is_idempotent(db, tenant):  # noqa: F811
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    first = await _schedule(db, tenant.id, appt.id)
    second = await _schedule(db, tenant.id, appt.id)

    assert len(first) == 3 and second == []
    assert len(await _all_rows(db, appt.id)) == 3


async def test_clinic_with_the_switch_off_gets_nothing(db, tenant):  # noqa: F811
    async with db() as session:
        t = await session.get(Tenant, tenant.id)
        t.reminders_v2_enabled = False
        await session.commit()
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))

    assert await _schedule(db, tenant.id, appt.id) == []
    assert await _all_rows(db, appt.id) == []


async def test_appointment_of_another_clinic_is_refused(db, tenant, other_tenant):  # noqa: F811
    appt = await make_appointment(db, other_tenant, start_at=NOW + timedelta(days=6))
    async with db() as session:
        from secretaria.models import Appointment

        a = await session.get(Appointment, appt.id)
        t = await session.get(Tenant, tenant.id)
        with pytest.raises(ValueError):
            await schedule_reminders(session, a, t, now=NOW)
    assert await _all_rows(db, appt.id) == []


async def test_block_slot_without_patient_and_terminal_status_get_nothing(db, tenant):  # noqa: F811
    block = await make_appointment(
        db, tenant, start_at=NOW + timedelta(days=6), with_patient=False
    )
    cancelled = await make_appointment(
        db, tenant, start_at=NOW + timedelta(days=6), status=AppointmentStatus.CANCELLED
    )
    assert await _schedule(db, tenant.id, block.id) == []
    assert await _schedule(db, tenant.id, cancelled.id) == []


async def test_naive_start_and_aware_now_give_the_same_rows(db, tenant):  # noqa: F811
    """SQLite hands back naive datetimes; the schedule must not shift or crash."""
    start = NOW + timedelta(days=6)
    appt = await make_appointment(db, tenant, start_at=start)
    async with db() as session:
        from secretaria.models import Appointment

        a = await session.get(Appointment, appt.id)
        a.start_at = start.replace(tzinfo=None)  # what a SQLite read-back looks like
        t = await session.get(Tenant, tenant.id)
        created = await schedule_reminders(session, a, t, now=NOW.replace(tzinfo=None))
        await session.commit()
    due = {r.kind: _utc(r.due_at) for r in created}
    assert due["day"] == start - timedelta(hours=24)
    assert due["hour"] == start - timedelta(hours=1)


async def test_with_prompt_is_false_after_two_confirmations(db, tenant):  # noqa: F811
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    async with db() as session:
        from secretaria.models import Appointment

        a = await session.get(Appointment, appt.id)
        a.confirmation_count = 2
        t = await session.get(Tenant, tenant.id)
        created = await schedule_reminders(session, a, t, now=NOW)
        await session.commit()
    assert created and all(r.with_prompt is False for r in created)
```

- [ ] **Step 2: Run to verify it fails**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_schedule.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'secretaria.services.reminder_schedule'`.

- [ ] **Step 3: Implement the module header and `schedule_reminders`**

Create `src/secretaria/services/reminder_schedule.py`:

```python
"""The reminder schedule and the confirmation counter - TASK-032, spec 4.1/4.2.

The ONLY writer of `appointment_reminders` rows' lifecycle (schedule / cancel /
reschedule) and of `Appointment.confirmation_count`. Nothing here sends a
message and nothing commits: callers own the transaction, these functions
`flush`. Wiring into the creation/reschedule/cancel paths and the cron is R2.

Datetimes: every comparison goes through `_as_utc`, because SQLite (the test
engine) returns naive datetimes for `DateTime(timezone=True)` columns while
Postgres returns aware ones.

LGPD: logs carry ids, kinds and counts only.
"""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, AppointmentStatus, Tenant, is_live_status
from secretaria.models.appointment_reminder import (
    REMINDER_ANSWER_CONFIRM,
    REMINDER_CHANNEL_WHATSAPP,
    REMINDER_KIND_CUSTOM,
    REMINDER_KIND_DAY,
    REMINDER_KIND_HOUR,
    REMINDER_STATUS_CANCELLED,
    REMINDER_STATUS_FAILED,
    REMINDER_STATUS_PENDING,
    REMINDER_STATUS_SENDING,
    REMINDER_STATUS_SENT,
    AppointmentReminder,
)
from secretaria.services.appointment_status import (
    SOURCE_BUTTON,
    SOURCE_FLOW,
    SOURCE_HUB,
    log_status_transition,
)

logger = get_logger(__name__)

MAX_CONFIRMATIONS = 2

CONFIRMATION_SOURCE_REMINDER_BUTTON = "reminder_button"
CONFIRMATION_SOURCE_CHAT_PROMPT = "chat_prompt"
CONFIRMATION_SOURCE_STAFF = "staff"
_CONFIRMATION_SOURCES = {
    CONFIRMATION_SOURCE_REMINDER_BUTTON: SOURCE_BUTTON,
    CONFIRMATION_SOURCE_CHAT_PROMPT: SOURCE_FLOW,
    CONFIRMATION_SOURCE_STAFF: SOURCE_HUB,
}

DISPLAY_UNCONFIRMED = "unconfirmed"
DISPLAY_CONFIRMED = "confirmed"
DISPLAY_CONFIRMED_TWICE = "confirmed_twice"
DISPLAY_ATTENTION = "attention"

_DAY_LEAD = timedelta(hours=24)
_HOUR_LEAD = timedelta(hours=1)
# How long after a reminder is due the clinic is warned if still unconfirmed.
_WARN_AFTER = {
    REMINDER_KIND_CUSTOM: timedelta(hours=2),
    REMINDER_KIND_DAY: timedelta(hours=2),
    REMINDER_KIND_HOUR: timedelta(minutes=20),
}


class ReminderMismatchError(ValueError):
    """The reminder id does not belong to this appointment/clinic."""


def _as_utc(dt: datetime) -> datetime:
    """Naive timestamps (SQLite) are UTC; aware ones are converted to UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _planned_kinds(tenant: Tenant) -> list[tuple[str, timedelta]]:
    plan: list[tuple[str, timedelta]] = []
    extra = tenant.reminder_extra_lead_minutes
    if extra is not None and extra > 0:
        plan.append((REMINDER_KIND_CUSTOM, timedelta(minutes=extra)))
    plan.append((REMINDER_KIND_DAY, _DAY_LEAD))
    plan.append((REMINDER_KIND_HOUR, _HOUR_LEAD))
    return plan


def _arm(row: AppointmentReminder, *, due: datetime, with_prompt: bool) -> None:
    """(Re)initialise a pending row's schedule fields."""
    row.status = REMINDER_STATUS_PENDING
    row.due_at = due
    row.with_prompt = with_prompt
    row.attempts = 0
    row.last_error_code = None
    row.sent_at = None
    row.answered_at = None
    row.answer = None
    row.warn_due_at = due + _WARN_AFTER[row.kind]
    row.warned_at = None
    row.warn_kind = None


async def schedule_reminders(
    session: AsyncSession,
    appointment: Appointment,
    tenant: Tenant,
    *,
    now: datetime,
) -> list[AppointmentReminder]:
    """Create the custom/day/hour rows of the appointment's CURRENT start.

    Returns only the rows created (or revived) by this call. Skips, silently:
    a clinic with the switch off, a block slot (no patient), a missing start, a
    terminal status, and every reminder whose due time is not in the future (a
    booking made too late never gets an already-late reminder). Idempotent: a
    row that already exists for (appointment, kind, start) is left alone, except
    a CANCELLED one, which is revived (a reschedule to the very same start).
    """
    if appointment.tenant_id != tenant.id:
        raise ValueError("appointment does not belong to this tenant")
    if not tenant.reminders_v2_enabled:
        return []
    if appointment.patient_id is None or appointment.start_at is None:
        return []
    if not is_live_status(appointment.status):
        return []

    start = _as_utc(appointment.start_at)
    now_utc = _as_utc(now)
    with_prompt = appointment.confirmation_count < MAX_CONFIRMATIONS

    existing = {
        row.kind: row
        for row in await session.scalars(
            select(AppointmentReminder).where(
                AppointmentReminder.tenant_id == tenant.id,
                AppointmentReminder.appointment_id == appointment.id,
                AppointmentReminder.appointment_start_at == start,
            )
        )
    }

    created: list[AppointmentReminder] = []
    for kind, lead in _planned_kinds(tenant):
        due = start - lead
        if due <= now_utc:
            continue
        row = existing.get(kind)
        if row is not None:
            if row.status != REMINDER_STATUS_CANCELLED:
                continue
            _arm(row, due=due, with_prompt=with_prompt)
        else:
            row = AppointmentReminder(
                tenant_id=tenant.id,
                appointment_id=appointment.id,
                patient_id=appointment.patient_id,
                kind=kind,
                appointment_start_at=start,
                channel=REMINDER_CHANNEL_WHATSAPP,
            )
            _arm(row, due=due, with_prompt=with_prompt)
            session.add(row)
        created.append(row)

    await session.flush()
    logger.info(
        "reminders_scheduled",
        appointment_id=str(appointment.id),
        tenant_id=str(tenant.id),
        kinds=[r.kind for r in created],
    )
    return created
```

(Imports used by later tasks — `update`, `Iterable`, `Protocol`, `UUID`, `AppointmentStatus`, the other constants — are consumed in Tasks 4-6; if ruff flags them as unused at this commit, add them in the task that first uses them instead.)

- [ ] **Step 4: Run to verify it passes**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_schedule.py -q`
Expected: `13 passed` (5 parametrized + 8 others).

- [ ] **Step 5: Lint and commit**

Run: `uvx ruff check src/secretaria/services/reminder_schedule.py tests/test_reminder_schedule.py` (fix unused-import findings by deferring those imports to Tasks 4-6) and `uvx ruff format` on both files.

```bash
git add src/secretaria/services/reminder_schedule.py tests/test_reminder_schedule.py
git commit -m "feat(reminders): schedule_reminders creates versioned custom/day/hour rows (TASK-032 R1)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ"
```


---

## Task 4: `cancel_reminders`, `reset_confirmation`, `reschedule_reminders`

**Files:**
- Modify: `src/secretaria/services/reminder_schedule.py` (append)
- Modify (append tests): `tests/test_reminder_schedule.py`

**Interfaces:**
- Consumes: `schedule_reminders`, `_as_utc`, constants from Task 3.
- Produces: `cancel_reminders(session, appointment_id: UUID, *, reason: str) -> int`, `reset_confirmation(appointment) -> None`, `reschedule_reminders(session, appointment, tenant, *, now) -> list[AppointmentReminder]`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_reminder_schedule.py` replace the line `from secretaria.services.reminder_schedule import schedule_reminders` with:

```python
from secretaria.services.reminder_schedule import (
    cancel_reminders,
    reschedule_reminders,
    schedule_reminders,
)
```

and append:

```python
async def _mark_sent(db, appt_id, kind):
    async with db() as session:
        row = await session.scalar(
            select(AppointmentReminder).where(
                AppointmentReminder.appointment_id == appt_id,
                AppointmentReminder.kind == kind,
            )
        )
        row.status = "sent"
        row.sent_at = NOW
        await session.commit()


async def test_cancel_cancels_pending_rows_and_only_for_that_appointment(db, tenant):  # noqa: F811
    a = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    b = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    await _schedule(db, tenant.id, a.id)
    await _schedule(db, tenant.id, b.id)

    async with db() as session:
        changed = await cancel_reminders(session, a.id, reason="cancelled")
        await session.commit()

    assert changed == 3
    assert {r.status for r in await _all_rows(db, a.id)} == {"cancelled"}
    assert {r.status for r in await _all_rows(db, b.id)} == {"pending"}


async def test_cancel_keeps_sent_history_but_clears_the_pending_warning(db, tenant):  # noqa: F811
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    await _schedule(db, tenant.id, appt.id)
    await _mark_sent(db, appt.id, "day")

    async with db() as session:
        changed = await cancel_reminders(session, appt.id, reason="attended")
        await session.commit()

    rows = {r.kind: r for r in await _all_rows(db, appt.id)}
    assert changed == 2  # custom + hour; the sent one is history, not cancelled
    assert rows["day"].status == "sent" and rows["day"].warn_due_at is None
    assert rows["custom"].status == "cancelled" and rows["hour"].status == "cancelled"


async def test_cancel_with_nothing_pending_is_a_noop(db, tenant):  # noqa: F811
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    async with db() as session:
        assert await cancel_reminders(session, appt.id, reason="cancelled") == 0


async def test_reschedule_zeroes_confirmation_cancels_old_rows_and_recreates(db, tenant):  # noqa: F811
    from secretaria.models import Appointment

    start = NOW + timedelta(days=6)
    appt = await make_appointment(db, tenant, start_at=start)
    await _schedule(db, tenant.id, appt.id)
    await _mark_sent(db, appt.id, "custom")
    new_start = NOW + timedelta(days=9)

    async with db() as session:
        a = await session.get(Appointment, appt.id)
        a.confirmation_count = 2
        a.first_confirmed_at = NOW
        a.last_confirmed_at = NOW
        a.start_at = new_start
        t = await session.get(Tenant, tenant.id)
        created = await reschedule_reminders(session, a, t, now=NOW)
        await session.commit()

    assert [r.kind for r in created] == ["custom", "day", "hour"]
    assert all(r.with_prompt is True for r in created)  # counter was zeroed first
    async with db() as session:
        a = await session.get(Appointment, appt.id)
    assert a.confirmation_count == 0
    assert a.first_confirmed_at is None and a.last_confirmed_at is None
    rows = await _all_rows(db, appt.id)
    old = [r for r in rows if _utc(r.appointment_start_at) == start]
    new = [r for r in rows if _utc(r.appointment_start_at) == new_start]
    assert len(old) == 3 and len(new) == 3
    by_kind = {r.kind: r.status for r in old}
    assert by_kind == {"custom": "sent", "day": "cancelled", "hour": "cancelled"}
    assert {r.status for r in new} == {"pending"}


async def test_reschedule_to_the_same_start_revives_the_rows(db, tenant):  # noqa: F811
    from secretaria.models import Appointment

    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    first = await _schedule(db, tenant.id, appt.id)
    async with db() as session:
        a = await session.get(Appointment, appt.id)
        t = await session.get(Tenant, tenant.id)
        again = await reschedule_reminders(session, a, t, now=NOW)  # no unique violation
        await session.commit()

    assert sorted(r.id for r in again) == sorted(r.id for r in first)
    assert {r.status for r in await _all_rows(db, appt.id)} == {"pending"}


async def test_reschedule_too_close_cancels_old_rows_and_creates_none(db, tenant):  # noqa: F811
    from secretaria.models import Appointment

    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    await _schedule(db, tenant.id, appt.id)
    async with db() as session:
        a = await session.get(Appointment, appt.id)
        a.start_at = NOW + timedelta(minutes=30)
        t = await session.get(Tenant, tenant.id)
        assert await reschedule_reminders(session, a, t, now=NOW) == []
        await session.commit()
    assert {r.status for r in await _all_rows(db, appt.id)} == {"cancelled"}


async def test_reschedule_on_a_disabled_clinic_still_zeroes_the_counter(db, tenant):  # noqa: F811
    from secretaria.models import Appointment

    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    async with db() as session:
        a = await session.get(Appointment, appt.id)
        a.confirmation_count = 1
        t = await session.get(Tenant, tenant.id)
        t.reminders_v2_enabled = False
        assert await reschedule_reminders(session, a, t, now=NOW) == []
        assert a.confirmation_count == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_schedule.py -q`
Expected: collection error `ImportError: cannot import name 'cancel_reminders'`.

- [ ] **Step 3: Implement**

Append to `src/secretaria/services/reminder_schedule.py`:

```python
async def cancel_reminders(session: AsyncSession, appointment_id: UUID, *, reason: str) -> int:
    """Cancel every not-yet-sent row of the appointment; returns how many.

    Rows that already went out (`sent`/`failed`) stay as history, but their
    pending clinic warning is cleared so nobody is warned about an appointment
    that no longer needs it. Takes no tenant: callers have already loaded the
    appointment scoped to their tenant (every hub/worker path does). A row a
    worker is sending right now (`sending`) is cancelled too; R2's sender must
    re-check the status after the send and not resurrect it.
    """
    result = await session.execute(
        update(AppointmentReminder)
        .where(
            AppointmentReminder.appointment_id == appointment_id,
            AppointmentReminder.status.in_((REMINDER_STATUS_PENDING, REMINDER_STATUS_SENDING)),
        )
        .values(status=REMINDER_STATUS_CANCELLED, warn_due_at=None)
        .execution_options(synchronize_session="fetch")
    )
    cancelled = result.rowcount or 0
    await session.execute(
        update(AppointmentReminder)
        .where(
            AppointmentReminder.appointment_id == appointment_id,
            AppointmentReminder.status.in_((REMINDER_STATUS_SENT, REMINDER_STATUS_FAILED)),
            AppointmentReminder.warned_at.is_(None),
            AppointmentReminder.warn_due_at.is_not(None),
        )
        .values(warn_due_at=None)
        .execution_options(synchronize_session="fetch")
    )
    logger.info(
        "reminders_cancelled",
        appointment_id=str(appointment_id),
        reason=reason,
        cancelled=cancelled,
    )
    return cancelled


def reset_confirmation(appointment: Appointment) -> None:
    """Zero the confirmation counter (a moved booking is unconfirmed again)."""
    appointment.confirmation_count = 0
    appointment.first_confirmed_at = None
    appointment.last_confirmed_at = None


async def reschedule_reminders(
    session: AsyncSession,
    appointment: Appointment,
    tenant: Tenant,
    *,
    now: datetime,
) -> list[AppointmentReminder]:
    """The appointment moved: retire the old rows, zero the counter, recreate.

    Call AFTER `appointment.start_at` holds the new start. Does not touch
    `appointment.status` (the caller sets RESCHEDULED, as today). Works even
    when the clinic's switch is off (the counter is still zeroed; no rows).
    """
    await cancel_reminders(session, appointment.id, reason="rescheduled")
    reset_confirmation(appointment)
    return await schedule_reminders(session, appointment, tenant, now=now)
```

- [ ] **Step 4: Run to verify it passes**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_schedule.py -q`
Expected: `20 passed`.

- [ ] **Step 5: Lint and commit**

Run: `uvx ruff check src/secretaria/services/reminder_schedule.py tests/test_reminder_schedule.py` and `uvx ruff format` on both (both are files this plan created).

```bash
git add src/secretaria/services/reminder_schedule.py tests/test_reminder_schedule.py
git commit -m "feat(reminders): cancel and reschedule the reminder rows (TASK-032 R1)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ"
```

---

## Task 5: `register_confirmation` (the counter)

**Files:**
- Modify: `src/secretaria/services/reminder_schedule.py` (append)
- Test: `tests/test_reminder_confirmation.py`

**Interfaces:**
- Consumes: Task 3/4 module contents; `log_status_transition` and `SOURCE_*`.
- Produces: `register_confirmation(session, *, appointment, reminder_id: UUID | None, source: str, now: datetime) -> int` and `ReminderMismatchError`.

Rules implemented (decisions, see "Decisions" in the closing section): a tap is bound to a reminder row, which must belong to this clinic and appointment, be of the current version and not cancelled; the same row counts once; `reminder_id=None` (staff or a prompt without a row) counts only when the counter is 0; cap 2; the appointment becomes `confirmed` (with a transition log) when it was `scheduled`/`rescheduled`; terminal appointments are left alone.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reminder_confirmation.py`:

```python
"""register_confirmation + display_state (TASK-032 R1, spec 4.1 'Regra de contagem')."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from secretaria.models import Appointment, AppointmentReminder, AppointmentStatus, Tenant
from secretaria.services.reminder_schedule import (
    ReminderMismatchError,
    register_confirmation,
    schedule_reminders,
)
from tests._reminder_fixtures import (  # noqa: F401
    NOW,
    db,
    make_appointment,
    other_tenant,
    tenant,
)

BUTTON = "reminder_button"


async def _booked(db, tenant, **kw):
    """An appointment 6 days out with its 3 reminder rows; returns (appt, {kind: id})."""
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6), **kw)
    async with db() as session:
        a = await session.get(Appointment, appt.id)
        t = await session.get(Tenant, tenant.id)
        rows = await schedule_reminders(session, a, t, now=NOW)
        await session.commit()
        return appt, {r.kind: r.id for r in rows}


async def _confirm(db, appt_id, reminder_id, *, source=BUTTON):
    async with db() as session:
        a = await session.get(Appointment, appt_id)
        count = await register_confirmation(
            session, appointment=a, reminder_id=reminder_id, source=source, now=NOW
        )
        await session.commit()
        return count


async def _appt(db, appt_id) -> Appointment:
    async with db() as session:
        return await session.get(Appointment, appt_id)


async def test_first_confirmation_counts_one_and_marks_confirmed(db, tenant):  # noqa: F811
    appt, ids = await _booked(db, tenant)

    assert await _confirm(db, appt.id, ids["day"]) == 1

    a = await _appt(db, appt.id)
    assert a.confirmation_count == 1 and a.status == AppointmentStatus.CONFIRMED
    assert a.first_confirmed_at is not None and a.last_confirmed_at is not None
    async with db() as session:
        row = await session.get(AppointmentReminder, ids["day"])
    assert row.answer == "confirm" and row.answered_at is not None


async def test_the_same_reminder_confirmed_twice_counts_once(db, tenant):  # noqa: F811
    appt, ids = await _booked(db, tenant)
    assert await _confirm(db, appt.id, ids["day"]) == 1
    assert await _confirm(db, appt.id, ids["day"]) == 1
    assert (await _appt(db, appt.id)).confirmation_count == 1


async def test_two_distinct_reminders_count_two_and_a_third_stays_at_two(db, tenant):  # noqa: F811
    appt, ids = await _booked(db, tenant)
    assert await _confirm(db, appt.id, ids["custom"]) == 1
    assert await _confirm(db, appt.id, ids["day"]) == 2
    assert await _confirm(db, appt.id, ids["hour"]) == 2  # confirmation after the cap
    a = await _appt(db, appt.id)
    assert a.confirmation_count == 2 and a.status == AppointmentStatus.CONFIRMED
    async with db() as session:
        row = await session.get(AppointmentReminder, ids["hour"])
    assert row.answer == "confirm"  # the tap is recorded, only the counter is capped


async def test_staff_confirmation_without_a_row_counts_one_and_is_idempotent(db, tenant):  # noqa: F811
    appt, _ = await _booked(db, tenant)
    assert await _confirm(db, appt.id, None, source="staff") == 1
    assert await _confirm(db, appt.id, None, source="staff") == 1


async def test_rescheduled_status_becomes_confirmed(db, tenant):  # noqa: F811
    appt, ids = await _booked(db, tenant, status=AppointmentStatus.RESCHEDULED)
    await _confirm(db, appt.id, ids["day"])
    assert (await _appt(db, appt.id)).status == AppointmentStatus.CONFIRMED


@pytest.mark.parametrize(
    "status",
    [AppointmentStatus.CANCELLED, AppointmentStatus.ATTENDED, AppointmentStatus.NO_SHOW],
)
async def test_terminal_appointment_is_never_confirmed(db, tenant, status):  # noqa: F811
    appt, ids = await _booked(db, tenant)
    async with db() as session:
        (await session.get(Appointment, appt.id)).status = status
        await session.commit()

    assert await _confirm(db, appt.id, ids["day"]) == 0

    a = await _appt(db, appt.id)
    assert a.confirmation_count == 0 and a.status == status


async def test_a_stale_tap_on_a_cancelled_reminder_does_not_count(db, tenant):  # noqa: F811
    appt, ids = await _booked(db, tenant)
    async with db() as session:
        (await session.get(AppointmentReminder, ids["day"])).status = "cancelled"
        await session.commit()
    assert await _confirm(db, appt.id, ids["day"]) == 0
    assert (await _appt(db, appt.id)).status == AppointmentStatus.SCHEDULED


async def test_a_tap_on_an_old_version_of_a_moved_appointment_does_not_count(db, tenant):  # noqa: F811
    appt, ids = await _booked(db, tenant)
    async with db() as session:
        (await session.get(Appointment, appt.id)).start_at = NOW + timedelta(days=9)
        await session.commit()
    assert await _confirm(db, appt.id, ids["day"]) == 0


async def test_reminder_of_another_clinic_or_appointment_is_refused(db, tenant, other_tenant):  # noqa: F811
    appt, _ = await _booked(db, tenant)
    foreign_appt, foreign_ids = await _booked(db, other_tenant)
    sibling, sibling_ids = await _booked(db, tenant)

    with pytest.raises(ReminderMismatchError):
        await _confirm(db, appt.id, foreign_ids["day"])  # other clinic
    with pytest.raises(ReminderMismatchError):
        await _confirm(db, appt.id, sibling_ids["day"])  # other appointment, same clinic
    with pytest.raises(ReminderMismatchError):
        await _confirm(db, appt.id, uuid4())  # unknown id

    assert (await _appt(db, appt.id)).confirmation_count == 0
    assert (await _appt(db, foreign_appt.id)).confirmation_count == 0
    assert (await _appt(db, sibling.id)).confirmation_count == 0


async def test_unknown_source_is_rejected(db, tenant):  # noqa: F811
    appt, ids = await _booked(db, tenant)
    with pytest.raises(ValueError):
        await _confirm(db, appt.id, ids["day"], source="carrier_pigeon")
```

- [ ] **Step 2: Run to verify it fails**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_confirmation.py -q`
Expected: collection error `ImportError: cannot import name 'register_confirmation'`.

- [ ] **Step 3: Implement**

Append to `src/secretaria/services/reminder_schedule.py`:

```python
async def register_confirmation(
    session: AsyncSession,
    *,
    appointment: Appointment,
    reminder_id: UUID | None,
    source: str,
    now: datetime,
) -> int:
    """Count one confirmation; returns the appointment's new `confirmation_count`.

    * `source` is one of CONFIRMATION_SOURCE_* (anything else -> ValueError).
    * A live appointment only: a terminal one (cancelled/attended/no_show) is
      returned untouched, so a late tap never resurrects it.
    * With a `reminder_id` the row must belong to this clinic AND this
      appointment, else `ReminderMismatchError` (the caller answers generically,
      it must not leak whose row it is). A cancelled row or one of an older
      start (the booking moved since) is a stale tap: ignored, count unchanged.
      The same row counts once; a different row counts again until the cap of 2.
    * With `reminder_id=None` (staff, or a prompt that has no row) it counts only
      when the counter is 0: re-clicking "mark as confirmed" never reaches 2.
    * On the way the status becomes CONFIRMED (a logged transition) unless it
      already is. The caller commits.
    """
    log_source = _CONFIRMATION_SOURCES.get(source)
    if log_source is None:
        raise ValueError(f"unknown confirmation source: {source!r}")
    count = appointment.confirmation_count
    if not is_live_status(appointment.status):
        return count
    now_utc = _as_utc(now)

    if reminder_id is not None:
        reminder = await session.scalar(
            select(AppointmentReminder).where(
                AppointmentReminder.id == reminder_id,
                AppointmentReminder.tenant_id == appointment.tenant_id,
                AppointmentReminder.appointment_id == appointment.id,
            )
        )
        if reminder is None:
            raise ReminderMismatchError("reminder does not belong to this appointment")
        stale = reminder.status == REMINDER_STATUS_CANCELLED or (
            appointment.start_at is not None
            and _as_utc(reminder.appointment_start_at) != _as_utc(appointment.start_at)
        )
        if stale or reminder.answer == REMINDER_ANSWER_CONFIRM:
            return count
        reminder.answer = REMINDER_ANSWER_CONFIRM
        reminder.answered_at = now_utc
        counts = True
    else:
        counts = count == 0

    if counts and count < MAX_CONFIRMATIONS:
        count += 1
        appointment.confirmation_count = count
        if appointment.first_confirmed_at is None:
            appointment.first_confirmed_at = now_utc
        appointment.last_confirmed_at = now_utc

    previous = appointment.status
    if previous != AppointmentStatus.CONFIRMED:
        appointment.status = AppointmentStatus.CONFIRMED
        log_status_transition(
            appointment_id=appointment.id,
            tenant_id=appointment.tenant_id,
            old_status=previous,
            new_status=AppointmentStatus.CONFIRMED,
            source=log_source,
            idempotency_key=f"confirm:{appointment.id}:{count}",
        )
    appointment.updated_at = now_utc
    await session.flush()
    logger.info(
        "confirmation_registered",
        appointment_id=str(appointment.id),
        source=source,
        confirmation_count=count,
    )
    return count
```

- [ ] **Step 4: Run to verify it passes**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_confirmation.py -q`
Expected: `12 passed` (3 parametrized terminal cases).

- [ ] **Step 5: Lint and commit**

Run: `uvx ruff check src/secretaria/services/reminder_schedule.py tests/test_reminder_confirmation.py` and `uvx ruff format` on both.

```bash
git add src/secretaria/services/reminder_schedule.py tests/test_reminder_confirmation.py
git commit -m "feat(reminders): register_confirmation counts each reminder once, capped at two (TASK-032 R1)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ"
```

---

## Task 6: `display_state`

**Files:**
- Modify: `src/secretaria/services/reminder_schedule.py` (append)
- Modify (append tests): `tests/test_reminder_confirmation.py`

**Interfaces:**
- Consumes: constants `DISPLAY_*`, `_as_utc`, `is_live_status`.
- Produces: `display_state(appointment, reminders) -> str`. `appointment` is duck-typed (needs `.status`, `.confirmation_count`, `.start_at`: an ORM row or a SQLAlchemy `Row` from a column select); `reminders` is any iterable whose items have `.warned_at` and `.appointment_start_at`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reminder_confirmation.py` (and add `display_state` to its `reminder_schedule` import, plus `from datetime import UTC` if missing):

```python
def _appt_row(status=AppointmentStatus.SCHEDULED, count=0, start=None):
    return SimpleNamespace(
        status=status, confirmation_count=count, start_at=start or NOW + timedelta(days=2)
    )


def _rem(warned, start=None):
    return SimpleNamespace(
        warned_at=NOW if warned else None, appointment_start_at=start or NOW + timedelta(days=2)
    )


def test_display_state_covers_the_four_states():
    assert display_state(_appt_row(), []) == "unconfirmed"
    assert display_state(_appt_row(), [_rem(warned=False)]) == "unconfirmed"
    assert display_state(_appt_row(count=1), []) == "confirmed"
    assert display_state(_appt_row(count=2), []) == "confirmed_twice"
    assert display_state(_appt_row(), [_rem(warned=True)]) == "attention"


def test_a_confirmation_beats_an_old_warning():
    assert display_state(_appt_row(count=1), [_rem(warned=True)]) == "confirmed"


def test_a_warning_of_an_older_start_does_not_turn_the_appointment_red():
    old = _rem(warned=True, start=NOW + timedelta(days=1))
    assert display_state(_appt_row(), [old]) == "unconfirmed"


def test_naive_and_aware_starts_are_the_same_version():
    start = NOW + timedelta(days=2)
    warned = _rem(warned=True, start=start.replace(tzinfo=None))
    assert display_state(_appt_row(start=start), [warned]) == "attention"


@pytest.mark.parametrize(
    "status",
    [AppointmentStatus.CANCELLED, AppointmentStatus.ATTENDED, AppointmentStatus.NO_SHOW],
)
def test_terminal_appointments_are_never_attention_or_confirmed(status):
    row = _appt_row(status=status, count=2)
    assert display_state(row, [_rem(warned=True)]) == "unconfirmed"
```

- [ ] **Step 2: Run to verify it fails**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_confirmation.py -q`
Expected: `ImportError: cannot import name 'display_state'`.

- [ ] **Step 3: Implement**

Append to `src/secretaria/services/reminder_schedule.py`:

```python
def display_state(appointment, reminders: Iterable) -> str:
    """The state the agenda shows: unconfirmed / confirmed / confirmed_twice / attention.

    `attention` = the clinic was already warned for the CURRENT start and the
    patient has not confirmed. Terminal appointments (cancelled/attended/no_show)
    return `unconfirmed`: their colour comes from `status`, which the API sends
    alongside, and a dead appointment must never look red or green.
    """
    if not is_live_status(appointment.status):
        return DISPLAY_UNCONFIRMED
    count = appointment.confirmation_count
    if count >= MAX_CONFIRMATIONS:
        return DISPLAY_CONFIRMED_TWICE
    if count >= 1:
        return DISPLAY_CONFIRMED
    start = appointment.start_at
    for reminder in reminders:
        if reminder.warned_at is None:
            continue
        if start is not None and _as_utc(reminder.appointment_start_at) != _as_utc(start):
            continue
        return DISPLAY_ATTENTION
    return DISPLAY_UNCONFIRMED
```

Delete the now-unused `Protocol` import if ruff flags it.

- [ ] **Step 4: Run to verify it passes**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_confirmation.py tests/test_reminder_schedule.py -q`
Expected: `39 passed` (19 in the confirmation file, 20 in the schedule file).

- [ ] **Step 5: Lint and commit**

Run: `uvx ruff check src/secretaria/services/reminder_schedule.py tests/test_reminder_confirmation.py` and `uvx ruff format` on both.

```bash
git add src/secretaria/services/reminder_schedule.py tests/test_reminder_confirmation.py
git commit -m "feat(reminders): display_state derives the agenda confirmation state (TASK-032 R1)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ"
```


---

## Task 7: `GET /events` returns the confirmation state

**Files:**
- Modify: `src/secretaria/schemas/calendar.py` (`CalendarEventRead`, new `CalendarReminderRead`)
- Modify: `src/secretaria/api/hub/calendar.py` (`list_events` + two small helpers)
- Modify: `tests/test_calendar_events_insurance.py` (two key-set assertions)
- Test: `tests/test_hub_calendar_confirmation.py`

**Interfaces:**
- Consumes: `display_state`, `DISPLAY_ATTENTION` (Task 6); `AppointmentReminder` (Task 1); `as_utc` from `secretaria.services.patient_context`.
- Produces (wire, additive, all optional with null defaults; null for events with no local appointment):
  `status` (the appointment status value), `confirmation_count` (int 0..2), `display_state` (`unconfirmed|confirmed|confirmed_twice|attention`), `attention` (bool), `reminders` (list of `{kind, status, due_at, sent_at, answered_at, answer, warned_at, warn_kind}`, only rows of the appointment's CURRENT start, ordered by `due_at`, datetimes aware UTC).

- [ ] **Step 1: Write the failing API tests**

Create `tests/test_hub_calendar_confirmation.py`:

```python
"""Hub agenda API: confirmation state on GET /events and PATCH status (TASK-032 R1, spec 4.4)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub import calendar as hub_calendar
from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import Appointment, AppointmentReminder, AppointmentStatus, Tenant
from secretaria.services.reminder_schedule import schedule_reminders
from secretaria.services.tenant_config import set_google_refresh_token
from tests._reminder_fixtures import (  # noqa: F401
    NOW,
    db,
    make_appointment,
    other_tenant,
    tenant,
)

CALENDAR = "/tenants/me/calendar"
LEGACY_EVENT_KEYS = {"id", "summary", "start", "end", "appointment_id"}
INSURANCE_KEYS = {"insurance", "insurance_plan", "deposit"}
NEW_KEYS = {"status", "confirmation_count", "display_state", "attention", "reminders"}


class _FakeCalendarService:
    events: list[dict] = []

    @classmethod
    def from_tenant_config(cls, config):
        return cls()

    async def check_availability(self, start, end):
        return list(type(self).events)

    async def cancel_event(self, event_id: str) -> None:
        return None

    async def update_event(self, event_id, start, end) -> dict:
        return {"id": event_id}


@pytest.fixture(autouse=True)
def _override(db, tenant, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant.id)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    monkeypatch.setattr(hub_calendar, "CalendarService", _FakeCalendarService)
    app.state.arq_pool = None
    _FakeCalendarService.events = []
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _connect(db, tenant) -> None:  # noqa: F811
    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "fake-refresh-token")
        await session.commit()


def _wire(event_id: str) -> dict:
    return {
        "id": event_id,
        "summary": "Consulta",
        "start": "2026-10-07T12:00:00+00:00",
        "end": "2026-10-07T12:30:00+00:00",
    }


async def _events(client: AsyncClient):
    return await client.get(
        f"{CALENDAR}/events", params={"start": "2026-10-01T00:00:00Z", "end": "2026-10-31T00:00:00Z"}
    )


async def _booked(db, tenant, event_id="evt-1", **kw):  # noqa: F811
    """Appointment 6 days out (real clock for the PATCH tests) with its 3 rows."""
    start = datetime.now(UTC) + timedelta(days=6)
    appt = await make_appointment(db, tenant, start_at=start, google_event_id=event_id, **kw)
    async with db() as session:
        a = await session.get(Appointment, appt.id)
        t = await session.get(Tenant, tenant.id)
        await schedule_reminders(session, a, t, now=datetime.now(UTC))
        await session.commit()
    return appt


async def _set(db, appt_id, **fields):  # noqa: F811
    async with db() as session:
        a = await session.get(Appointment, appt_id)
        for key, value in fields.items():
            setattr(a, key, value)
        await session.commit()


async def _warn_all(db, appt_id):  # noqa: F811
    async with db() as session:
        for r in await session.scalars(
            select(AppointmentReminder).where(AppointmentReminder.appointment_id == appt_id)
        ):
            r.status = "sent"
            r.warned_at = datetime.now(UTC)
            r.warn_kind = "unconfirmed"
        await session.commit()


# ----------------------------------------------------------------- GET /events


async def test_event_carries_status_count_state_and_reminder_summary(client: AsyncClient, db, tenant):  # noqa: F811
    await _connect(db, tenant)
    appt = await _booked(db, tenant)
    _FakeCalendarService.events = [_wire("evt-1")]

    body = (await _events(client)).json()[0]

    assert set(body) == LEGACY_EVENT_KEYS | INSURANCE_KEYS | NEW_KEYS
    assert body["appointment_id"] == str(appt.id)
    assert body["status"] == "scheduled"
    assert body["confirmation_count"] == 0
    assert body["display_state"] == "unconfirmed" and body["attention"] is False
    assert [r["kind"] for r in body["reminders"]] == ["custom", "day", "hour"]
    first = body["reminders"][0]
    assert set(first) == {
        "kind", "status", "due_at", "sent_at", "answered_at", "answer", "warned_at", "warn_kind",
    }
    assert first["status"] == "pending" and first["sent_at"] is None
    # aware UTC on the wire, even though SQLite hands back naive datetimes
    assert first["due_at"].endswith("Z")


@pytest.mark.parametrize(
    ("count", "expected"), [(1, "confirmed"), (2, "confirmed_twice")]
)
async def test_confirmed_states(client: AsyncClient, db, tenant, count, expected):  # noqa: F811
    await _connect(db, tenant)
    appt = await _booked(db, tenant)
    await _set(db, appt.id, confirmation_count=count, status=AppointmentStatus.CONFIRMED)
    _FakeCalendarService.events = [_wire("evt-1")]

    body = (await _events(client)).json()[0]

    assert body["confirmation_count"] == count
    assert body["display_state"] == expected and body["attention"] is False
    assert body["status"] == "confirmed"


async def test_warned_and_unconfirmed_is_attention(client: AsyncClient, db, tenant):  # noqa: F811
    await _connect(db, tenant)
    appt = await _booked(db, tenant)
    await _warn_all(db, appt.id)
    _FakeCalendarService.events = [_wire("evt-1")]

    body = (await _events(client)).json()[0]

    assert body["display_state"] == "attention" and body["attention"] is True


async def test_reminders_of_an_older_start_are_not_listed(client: AsyncClient, db, tenant):  # noqa: F811
    await _connect(db, tenant)
    appt = await _booked(db, tenant)
    await _set(db, appt.id, start_at=datetime.now(UTC) + timedelta(days=20))
    _FakeCalendarService.events = [_wire("evt-1")]

    body = (await _events(client)).json()[0]

    assert body["reminders"] == []


async def test_event_without_a_local_appointment_has_null_confirmation_keys(client: AsyncClient, db, tenant):  # noqa: F811
    await _connect(db, tenant)
    _FakeCalendarService.events = [_wire("typed-straight-into-google")]

    body = (await _events(client)).json()[0]

    assert body["appointment_id"] is None
    assert {k: body[k] for k in NEW_KEYS} == {k: None for k in NEW_KEYS}


async def test_another_clinics_appointment_never_leaks_its_state(client: AsyncClient, db, tenant, other_tenant):  # noqa: F811
    await _connect(db, tenant)
    foreign = await _booked(db, other_tenant, event_id="shared-event")
    await _set(db, foreign.id, confirmation_count=2, status=AppointmentStatus.CONFIRMED)
    _FakeCalendarService.events = [_wire("shared-event")]

    body = (await _events(client)).json()[0]

    assert body["appointment_id"] is None
    assert {k: body[k] for k in NEW_KEYS} == {k: None for k in NEW_KEYS}


async def test_reminders_are_loaded_with_one_query_for_the_whole_page(client: AsyncClient, db, tenant):  # noqa: F811
    await _connect(db, tenant)
    for n in range(4):
        await _booked(db, tenant, event_id=f"evt-{n}")
    _FakeCalendarService.events = [_wire(f"evt-{n}") for n in range(4)]
    statements: list[str] = []
    engine = db.kw["bind"].sync_engine

    def _on(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _on)
    try:
        assert len((await _events(client)).json()) == 4
    finally:
        event.remove(engine, "before_cursor_execute", _on)

    assert sum("FROM appointment_reminders" in s for s in statements) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_hub_calendar_confirmation.py -q`
Expected: failures on the key-set assertions (`status` etc. missing from the event) — the new fixtures themselves must import cleanly.

- [ ] **Step 3: Extend the schema**

In `src/secretaria/schemas/calendar.py`, directly above `class CalendarEventRead`, add:

```python
class CalendarReminderRead(BaseModel):
    """One reminder of the appointment's CURRENT start, as the agenda shows it.

    Datetimes are always aware UTC on the wire (skill naive-timestamp-
    serialization). `kind`/`status`/`answer`/`warn_kind` are plain strings (the
    constants of models/appointment_reminder.py) so a value added later never
    breaks an older client. No message text, no phone, no error text: the only
    failure detail is a code kept server-side.
    """

    kind: str
    status: str
    due_at: datetime
    sent_at: datetime | None = None
    answered_at: datetime | None = None
    answer: str | None = None
    warned_at: datetime | None = None
    warn_kind: str | None = None


```

and at the end of `CalendarEventRead` (after the `deposit` field) add:

```python
    # TASK-032 (spec 4.4). Additive; all None for an event with no local
    # Appointment (a block, or something typed into Google). `status` is the
    # appointment status VALUE; `display_state` is one of "unconfirmed",
    # "confirmed", "confirmed_twice", "attention" (terminal appointments are
    # always "unconfirmed" - colour them by `status`); `attention` is
    # `display_state == "attention"`; `reminders` lists only the rows of the
    # appointment's current start, ordered by due time. A consumer must ignore
    # any key it does not know.
    status: AppointmentStatus | None = None
    confirmation_count: int | None = None
    display_state: str | None = None
    attention: bool | None = None
    reminders: list[CalendarReminderRead] | None = None
```

- [ ] **Step 4: Extend `list_events`**

In `src/secretaria/api/hub/calendar.py`:

1. Imports: add `AppointmentReminder` to the `from secretaria.models import (...)` line, `CalendarReminderRead` to the `schemas.calendar` import list, and
```python
from secretaria.services import cancellation_notice, reminder_schedule
from secretaria.services.patient_context import as_utc
```
(replace the existing `from secretaria.services import cancellation_notice`).

2. Above `list_events` add:

```python
def _reminder_read(reminder: AppointmentReminder) -> CalendarReminderRead:
    def _opt(value: datetime | None) -> datetime | None:
        return as_utc(value) if value is not None else None

    return CalendarReminderRead(
        kind=reminder.kind,
        status=reminder.status,
        due_at=as_utc(reminder.due_at),
        sent_at=_opt(reminder.sent_at),
        answered_at=_opt(reminder.answered_at),
        answer=reminder.answer,
        warned_at=_opt(reminder.warned_at),
        warn_kind=reminder.warn_kind,
    )


def _current_reminders(row: Row, reminders: list[AppointmentReminder]) -> list[AppointmentReminder]:
    """Only the rows of the appointment's current start, oldest due first."""
    if row.start_at is not None:
        current_start = as_utc(row.start_at)
        reminders = [r for r in reminders if as_utc(r.appointment_start_at) == current_start]
    return sorted(reminders, key=lambda r: as_utc(r.due_at))
```

3. In the `select(...)` of `list_events` add three columns after `Appointment.insurance_professional_plan_id,`:
```python
                Appointment.status,
                Appointment.confirmation_count,
                Appointment.start_at,
```

4. After the `deposits = await deposit_lifecycle.load_deposit_views(...)` statement add:

```python
    # ONE tenant-scoped query for the whole page's reminder rows (never one per
    # event); rows of another clinic never resolve.
    reminders_by_appointment: dict[UUID, list[AppointmentReminder]] = {}
    if booked:
        reminder_rows = await session.scalars(
            select(AppointmentReminder).where(
                AppointmentReminder.tenant_id == tenant.id,
                AppointmentReminder.appointment_id.in_([row.id for row in booked.values()]),
            )
        )
        for reminder in reminder_rows:
            reminders_by_appointment.setdefault(reminder.appointment_id, []).append(reminder)
```

5. In the loop, before `reads.append(`, add:

```python
        current = (
            _current_reminders(row, reminders_by_appointment.get(row.id, []))
            if row is not None
            else None
        )
        state = reminder_schedule.display_state(row, current) if row is not None else None
```

and add these keyword arguments to the `CalendarEventRead(...)` call:

```python
                status=row.status if row is not None else None,
                confirmation_count=row.confirmation_count if row is not None else None,
                display_state=state,
                attention=(state == reminder_schedule.DISPLAY_ATTENTION)
                if state is not None
                else None,
                reminders=[_reminder_read(r) for r in current] if current is not None else None,
```

- [ ] **Step 5: Update the two existing key-set assertions**

In `tests/test_calendar_events_insurance.py` add below `LEGACY_EVENT_KEYS` (the second definition, around line 74):

```python
NEW_EVENT_KEYS = {"status", "confirmation_count", "display_state", "attention", "reminders"}
```

and change both assertions (around lines 88 and 825) from
`LEGACY_EVENT_KEYS | {"insurance", "insurance_plan", "deposit"}` to
`LEGACY_EVENT_KEYS | {"insurance", "insurance_plan", "deposit"} | NEW_EVENT_KEYS`.
Also add `assert dumped["status"] is None and dumped["reminders"] is None` after the existing `assert dumped["appointment_id"] is None` in `test_a_legacy_event_gains_three_null_fields_and_loses_nothing`. Do not rename the tests.

- [ ] **Step 6: Run to verify it passes**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_hub_calendar_confirmation.py tests/test_calendar_events_insurance.py -q`
Expected: all pass (the insurance file's own statement-count assertions, `FROM appointments == 1` etc., still hold because the reminders query reads `FROM appointment_reminders`).

- [ ] **Step 7: Lint and commit**

Run: `uvx ruff check tests/test_hub_calendar_confirmation.py` + `uvx ruff format tests/test_hub_calendar_confirmation.py` (created file); `uvx ruff check src/secretaria/api/hub/calendar.py src/secretaria/schemas/calendar.py` (check only; do not format); `git diff --stat` must show small insertions only.

```bash
git add src/secretaria/schemas/calendar.py src/secretaria/api/hub/calendar.py tests/test_hub_calendar_confirmation.py tests/test_calendar_events_insurance.py
git commit -m "feat(reminders): agenda events return status, confirmation count and reminder summary (TASK-032 R1)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ"
```

---

## Task 8: `PATCH .../status` goes through the counter

**Files:**
- Modify: `src/secretaria/api/hub/calendar.py` (`update_appointment_status`, `_appointment_read`)
- Modify: `src/secretaria/schemas/calendar.py` (`AppointmentRead`)
- Modify (append tests): `tests/test_hub_calendar_confirmation.py`

**Interfaces:**
- Consumes: `register_confirmation`, `reset_confirmation`, `cancel_reminders`, `CONFIRMATION_SOURCE_STAFF`.
- Produces: `AppointmentRead.confirmation_count: int = 0` (additive). Behavior: PATCH `confirmed` counts one staff confirmation (idempotent); PATCH `scheduled` zeroes the counter (keeps "confirmed iff count >= 1"); PATCH `cancelled`/`attended`/`no_show` cancels the pending reminder rows. **Every transition PATCH accepts today is still accepted** (no validation is added).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hub_calendar_confirmation.py`:

```python
# ----------------------------------------------------------------- PATCH status


async def _patch(client: AsyncClient, appt_id, status_value: str):
    return await client.patch(
        f"{CALENDAR}/appointments/{appt_id}/status", json={"status": status_value}
    )


async def _reload(db, appt_id) -> Appointment:  # noqa: F811
    async with db() as session:
        return await session.get(Appointment, appt_id)


async def _statuses(db, appt_id) -> set[str]:  # noqa: F811
    async with db() as session:
        return {
            r.status
            for r in await session.scalars(
                select(AppointmentReminder).where(AppointmentReminder.appointment_id == appt_id)
            )
        }


async def test_patch_confirmed_counts_one_staff_confirmation_and_is_idempotent(client: AsyncClient, db, tenant):  # noqa: F811
    await _connect(db, tenant)
    appt = await _booked(db, tenant)

    first = await _patch(client, appt.id, "confirmed")
    again = await _patch(client, appt.id, "confirmed")

    assert first.status_code == 200 and again.status_code == 200
    body = again.json()
    assert body["status"] == "confirmed" and body["confirmation_count"] == 1
    a = await _reload(db, appt.id)
    assert a.confirmation_count == 1
    assert a.first_confirmed_at is not None and a.last_confirmed_at is not None


async def test_patch_confirmed_does_not_raise_a_patient_confirmed_twice_booking(client: AsyncClient, db, tenant):  # noqa: F811
    await _connect(db, tenant)
    appt = await _booked(db, tenant)
    await _set(db, appt.id, confirmation_count=2, status=AppointmentStatus.CONFIRMED)

    body = (await _patch(client, appt.id, "confirmed")).json()

    assert body["confirmation_count"] == 2


async def test_patch_back_to_scheduled_zeroes_the_counter(client: AsyncClient, db, tenant):  # noqa: F811
    await _connect(db, tenant)
    appt = await _booked(db, tenant)
    await _patch(client, appt.id, "confirmed")

    body = (await _patch(client, appt.id, "scheduled")).json()

    assert body["status"] == "scheduled" and body["confirmation_count"] == 0
    a = await _reload(db, appt.id)
    assert a.first_confirmed_at is None and a.last_confirmed_at is None


@pytest.mark.parametrize("terminal", ["cancelled", "attended", "no_show"])
async def test_patch_to_a_terminal_status_cancels_the_pending_reminders(client: AsyncClient, db, tenant, terminal):  # noqa: F811
    await _connect(db, tenant)
    appt = await _booked(db, tenant)
    assert await _statuses(db, appt.id) == {"pending"}

    response = await _patch(client, appt.id, terminal)

    assert response.status_code == 200
    assert await _statuses(db, appt.id) == {"cancelled"}


async def test_patch_keeps_accepting_the_transitions_it_always_accepted(client: AsyncClient, db, tenant):  # noqa: F811
    """No validation was added: a cancelled booking can still be PATCHed to any
    status, exactly as before (this plan only adds counter side effects)."""
    await _connect(db, tenant)
    appt = await _booked(db, tenant, status=AppointmentStatus.CANCELLED)

    assert (await _patch(client, appt.id, "attended")).status_code == 200
    again = await _patch(client, appt.id, "confirmed")
    assert again.status_code == 200 and again.json()["status"] == "confirmed"


async def test_patch_on_another_clinics_appointment_is_404_and_changes_nothing(client: AsyncClient, db, tenant, other_tenant):  # noqa: F811
    await _connect(db, tenant)
    foreign = await _booked(db, other_tenant)

    response = await _patch(client, foreign.id, "confirmed")

    assert response.status_code == 404
    assert (await _reload(db, foreign.id)).confirmation_count == 0
    assert await _statuses(db, foreign.id) == {"pending"}


async def test_existing_appointment_read_keys_are_unchanged(client: AsyncClient, db, tenant):  # noqa: F811
    await _connect(db, tenant)
    appt = await _booked(db, tenant)

    body = (await _patch(client, appt.id, "confirmed")).json()

    legacy = {
        "id", "tenant_id", "patient_id", "conversation_id", "google_event_id",
        "google_event_link", "appointment_type", "start_at", "end_at", "phone", "status",
        "created_at", "updated_at", "deposit_status", "deposit_outcome",
    }
    assert set(body) == legacy | {"confirmation_count"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_hub_calendar_confirmation.py -q -k patch`
Expected: FAIL (`KeyError: 'confirmation_count'`, reminders still `pending` after terminal PATCH).

- [ ] **Step 3: Schema field**

In `AppointmentRead` (`src/secretaria/schemas/calendar.py`), after `status: AppointmentStatus`, add:

```python
    # TASK-032: how many distinct confirmations (0..2) the booking has. Additive;
    # 0 for every row from before the counter existed.
    confirmation_count: int = 0
```

and in `_appointment_read` (`api/hub/calendar.py`) add `confirmation_count=appt.confirmation_count,` after `status=appt.status,`.

- [ ] **Step 4: Wire the PATCH**

In `update_appointment_status` (`api/hub/calendar.py`), keep every existing line (the status assignment, the log line, the deposit hooks). Immediately **after** the `if body.status == AppointmentStatus.CANCELLED: ... elif ... NO_SHOW: ...` money-hook block and **before** `await session.commit()`, insert:

```python
    # TASK-032: the reminder schedule follows the status. The status above is
    # still assigned exactly as before (no new transition validation).
    now = datetime.now(UTC)
    if body.status == AppointmentStatus.CONFIRMED:
        await reminder_schedule.register_confirmation(
            session,
            appointment=appt,
            reminder_id=None,
            source=reminder_schedule.CONFIRMATION_SOURCE_STAFF,
            now=now,
        )
    elif body.status == AppointmentStatus.SCHEDULED:
        reminder_schedule.reset_confirmation(appt)
    elif body.status in TERMINAL_APPOINTMENT_STATUSES:
        await reminder_schedule.cancel_reminders(
            session, appt.id, reason=f"status_{body.status.value}"
        )
```

and add `TERMINAL_APPOINTMENT_STATUSES` to the `from secretaria.models import (...)` import. (`register_confirmation` sees the status already `CONFIRMED`, so it logs no second transition; it only moves the counter.)

- [ ] **Step 5: Run to verify it passes**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_hub_calendar_confirmation.py tests/test_hub_calendar_money.py tests/test_calendar_events_insurance.py -q`
Expected: all pass (money tests prove the Pix hooks around CANCELLED/NO_SHOW did not move).

- [ ] **Step 6: Lint, diff check, commit**

Run: `uvx ruff format tests/test_hub_calendar_confirmation.py`, `uvx ruff check tests/test_hub_calendar_confirmation.py src/secretaria/api/hub/calendar.py src/secretaria/schemas/calendar.py`, then `git diff --stat` (small insertions only).

```bash
git add src/secretaria/api/hub/calendar.py src/secretaria/schemas/calendar.py tests/test_hub_calendar_confirmation.py
git commit -m "feat(reminders): hub status PATCH goes through the confirmation counter (TASK-032 R1)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ"
```

---

## Task 9: Checkpoint doc and final validation

**Files:**
- Create: `docs/CHECKPOINT_lembretes_r1.md`
- Modify: `CLAUDE.md` (one pointer line only, in the docs-pointers area; follow the repo's local rule that `docs/` is the source of truth and is updated after validation)

**Interfaces:**
- Consumes: everything above.
- Produces: the written state R2-R5 read first.

- [ ] **Step 1: Run the six new test files plus the two touched suites**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_reminder_model.py tests/test_migration_reminders_foundation.py tests/test_reminder_schedule.py tests/test_reminder_confirmation.py tests/test_hub_calendar_confirmation.py tests/test_hub_calendar_money.py tests/test_calendar_events_insurance.py -q`
Expected: all pass. (Do NOT run the full suite in this plan; if the owner later wants it, separate pre-existing failures from regressions with evidence, per `AI_WORKFLOW.md`.)

- [ ] **Step 2: Write `docs/CHECKPOINT_lembretes_r1.md`**

Content (Portuguese headings, English identifiers), these sections, each filled from what this plan built:
1. **Estado** — R1 implemented LOCAL and committed on branch `task/TASK-032-*`; NOT pushed, NOT deployed; migration `b8d3f1a6c2e5` not applied anywhere; Postgres 5433 run done or "NOT done" (state which).
2. **O que entrou onde** — table: model `models/appointment_reminder.py`; columns on `appointments`/`tenants`; service `services/reminder_schedule.py` (list the 7 public names); API additions (`GET /events` keys, `AppointmentRead.confirmation_count`, PATCH side effects).
3. **Regras fixadas** — the Decisions list from this plan (below), verbatim.
4. **Pendências para R2-R5** — wiring `schedule_reminders` into creation paths (flow, LLM tool, Portal hold promotion, hub `POST /appointments`), `reschedule_reminders` into the three reschedule carriers (flow, button, hub `POST .../reschedule`), `cancel_reminders` into the cancel carriers (flow, button, hub `POST .../cancel`, deposit-expiry sweep), the cron, `with_prompt` recomputed at send time, backfill of existing future appointments, `sending` rows re-checked after send.
5. **Verificação** — the commands of Step 1 and the result.

Add one pointer line to `CLAUDE.md` (find the place with `grep -n CHECKPOINT CLAUDE.md`; if there is no list, add it under the docs section): ``- Lembretes/confirmação (TASK-032 R1): `docs/CHECKPOINT_lembretes_r1.md`.`` Cite function names, not line numbers. Check `git diff --stat CLAUDE.md` shows one added line (CRLF-safe).

- [ ] **Step 3: Commit**

```bash
git add docs/CHECKPOINT_lembretes_r1.md CLAUDE.md
git commit -m "docs(reminders): checkpoint for the R1 foundation (TASK-032)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0167EwR4duB1bWgEhfsCLPpJ"
```

---

## Decisions taken where the spec is silent (flag to the owner/Orchestrator)

1. **No reason column.** The spec has no field for why a row was cancelled; `cancel_reminders(reason=...)` only logs it (id + reason code, no PII).
2. **`reminder_id=None` counts only at 0.** Staff "mark as confirmed" (and any prompt without a row) is idempotent and never reaches 2; two confirmations need two distinct reminder rows.
3. **Stale taps are ignored, not errors.** A cancelled row or a row of an older start returns the current count unchanged; a row of another clinic/appointment raises `ReminderMismatchError` (callers answer generically).
4. **`display_state` for terminal statuses is `unconfirmed`.** The four values of the binding contract are kept; clients colour terminal states by `status`.
5. **PATCH side effects beyond `confirmed`:** `scheduled` zeroes the counter (keeps "confirmed iff count >= 1"); terminal statuses cancel pending rows. PATCH to `rescheduled` only sets the status (the hub `reschedule` endpoint, wired in R2, is what recreates rows).
6. **Row `channel` defaults to `whatsapp`** at creation; R2 sets the real channel (email for Portal) at send time. `with_prompt` is computed at creation and must be recomputed at send time.
7. **No DB check constraint on the counter or vocabularies** (SQLite cannot `ADD CONSTRAINT`, and the vocabularies will grow in R2-R4); the cap lives in the service and is tested.
8. **Hub create/reschedule/cancel endpoints are NOT wired here** (R2). Spec section 9 does not list `api/hub/calendar.py` among R2's files, but R2 must edit it for those three endpoints, and R4 edits it too: sequence R2 before R4 on that file.
9. `AppointmentRead.confirmation_count` is added (not requested by the spec) so the front can update the chip from the PATCH response without refetching the page.

## Spec coverage (self-review)

| Spec item | Where |
|---|---|
| 4.1 table `appointment_reminders`, all columns | Tasks 1-2 |
| 4.1 `confirmation_count`, `first/last_confirmed_at` | Tasks 1-2, 5 |
| 4.1 derived display state | Task 6 (+ Task 7 on the wire) |
| 4.1 counting rule (distinct reminder counts, cap 2, staff counts one) | Task 5 |
| 4.2 creation skips already-due rows; reschedule cancels, zeroes, recreates; cancel/attended/no_show cancels pending | Tasks 3-4, 8 |
| 4.2 `with_prompt=false` after two | Task 3 (creation); R2 at send time |
| 4.4 warn deadlines 2 h / 2 h / 20 min | Task 3 (`warn_due_at`); the warning cron is R4 |
| 4.4 API: events return status, count, attention, reminder summary; PATCH confirmed through counter | Tasks 7-8 |
| 4.5 `reminders_v2_enabled`, `reminder_extra_lead_minutes` | Tasks 1-2 (+ gate in Task 3) |
| Criterion 10 (switch off = nothing happens) | Task 3 test |
| Out of this plan: cron, texts, buttons, chat opening, e-mail, release, front, backfill, wiring of creation paths | R2-R5 |

Placeholder scan: none; the only deferred imports are named in Task 3 Step 3. Type consistency: `schedule_reminders`/`reschedule_reminders` return `list[AppointmentReminder]`, `cancel_reminders` returns `int`, `register_confirmation` returns `int`, `display_state` returns `str` — identical in the Interfaces block, the code and every test.

## Deploy e liberação

Not part of execution; only on the owner's explicit request, one step at a time.

1. **Migration first:** apply `b8d3f1a6c2e5` (`alembic upgrade head`) to the target database before any image that maps the new columns starts. It is additive, so the currently deployed API and worker keep working against the migrated schema in the gap.
2. **API and worker together** (two separate deploys; deploying only the API leaves the worker mapping the old model, harmless for R1 because nothing is wired yet, but they must match before R2).
3. **Switch stays off:** `reminders_v2_enabled` is false for every clinic after deploy; nothing is scheduled or sent. Turn it on first for the test clinic only, and only after R2 exists (R1 alone has no visible effect except the new agenda keys).
4. Frontend (R5) last. Never push or deploy from this plan. Integration target (`main` vs `feature/out-of-mvp`) is the owner's call; TASK-032 is not one of the F-phase (Pix) tasks, so the default is `main`, but ask before merging.
5. Rollback: do not run `downgrade()` against a database a newer image uses; to disable behavior set `reminders_v2_enabled = false`.
