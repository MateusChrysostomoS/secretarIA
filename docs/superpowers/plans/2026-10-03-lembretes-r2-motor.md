# Lembretes R2 — Motor de lembretes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send the three planned reminders of every appointment (custom / 1 day / 1 hour) exactly once, with doctor, date, time, service, requirements and the Confirmar / Cancelar / Outro buttons, on the patient's own channel (WhatsApp inside or outside the 24 h window, Portal by chat + e-mail), behind the per-clinic switch `Tenant.reminders_v2_enabled`.

**Architecture:** R1 (parallel plan) owns the data: the `appointment_reminders` table, the confirmation counter and the scheduling functions in `services/reminder_schedule.py`. R2 adds (1) one text builder (`services/reminder_text.py`), (2) one channel-aware delivery function (`services/reminder_delivery.py`), (3) a one-minute cron that claims due rows atomically, guards them, delivers, retries up to 4 times and marks delivery failures (`workers/reminder_engine.py`), (4) after-commit hooks that keep the schedule in step with every booking / reschedule / cancel path plus a 10-minute reconcile cron that doubles as the backfill (`services/reminder_hooks.py`), (5) the `remconfirm|` / `remcancel|` / `remother|` button ids, decoded and scoped to the patient (`schemas/webhook.py`, `workers/shared/reminder_actions.py`). The old cron `plugins/reminders.py::send_appointment_reminders` keeps running unchanged for clinics with the switch OFF, skips clinics with it ON, and stops trying Portal patients (no `wa_id`).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async (Postgres in prod, SQLite in tests), arq cron, WhatsApp Cloud API (`services/whatsapp.py`), Brain-Message sender (`services/channel_sender.py`), SMTP transactional e-mail (`services/email.py`), pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-10-03-lembretes-e-confirmacao-design.md` — this plan implements §4.2 and the §7 rows assigned to R2 ("Lembrete perdido quando o envio falha", "Remarcar não gera novo lembrete de 24 h", "Portal: marca gravada e erro a cada varredura", "Toque de ação não escopado ao paciente" (R2 half), "Fuso", "Modelo da Meta com aprovação externa").

## Global Constraints

- The whole feature is behind `Tenant.reminders_v2_enabled` (R1, default `false`). Switch OFF: no row is created, no row is processed, the old cron behaves exactly as today for WhatsApp patients. ("Nada acontece em clínica com o interruptor desligado", spec §5.10.)
- Switch ON: the old cron skips that clinic; only `workers/reminder_engine.py` sends.
- Reminder text (spec §4.2, verbatim): `LEMBRE-SE: Sua consulta com {médico} está marcada para o dia {DD/MM/AAAA} às {HH:MM} para {serviço}.` + requirements; third-party booking: `A consulta de {atendido} com {médico}…`. Date and time always in the **clinic's** timezone (`Tenant.timezone`, default `America/Sao_Paulo`), never UTC.
- Buttons: `Confirmar` / `Cancelar` / `Outro`, ids `remconfirm|<reminder_id>`, `remcancel|<reminder_id>`, `remother|<reminder_id>`. Every label ≤ 20 characters (`core/whatsapp_limits.py::MAX_BUTTON_LABEL_CHARS`). Old ids (`apptconfirm|`, `apptresched|`, `apptcancel|`, `apptcancelyes|`, `rebook*|`) keep working unchanged.
- WhatsApp template parameters never contain a newline, a tab or more than 4 consecutive spaces (Meta rule); the requirements go flattened into one line.
- At most **4** delivery attempts per row (`send_cancellation_notice` precedent: `CANCEL_NOTICE_MAX_TRIES = 4`); after that `status='failed'` and `warn_kind='delivery_failed'`.
- With `Appointment.confirmation_count >= 2` a reminder goes out **without** the confirm buttons and **without** a clinic warning (`warn_due_at` stays empty).
- Clinic-warning deadlines (`custom` +2 h, `day` +2 h, `hour` +20 min, spec §2.4) are written by R1 at scheduling time (`warn_due_at = due_at + delay`). R2 only: sets `warn_kind='unconfirmed'` on a successful send with buttons; clears `warn_due_at` when the reminder goes without buttons or a guard closes the row; sets `warn_kind='delivery_failed'` + `warn_due_at=<failure time>` on a definitive failure. R4 reads them; R2 never e-mails the clinic.
- `last_error_code` holds only a short code (`http_400`, `RuntimeError`, `no_email`…), never patient text. Logs carry ids and codes only — never a phone number, e-mail address, name or message body.
- UI / patient-facing text in Portuguese; code, comments and log event names in English.
- Layering (`CLAUDE.md`): `api -> workers -> services -> models -> core`. `services/*` never imports `workers/*` or `plugins/*`.
- No migration in R2 (R1 owns every schema change). No deploy, no push, no Easypanel access in any task.
- Tests from Git Bash: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest <file> -q`. Never `ruff format .`; run `uvx ruff format <file>` then `uvx ruff check --fix <file>` only on files this plan **creates** (ruff config: line 100, rules E/F/I/UP/B, isort `combine-as-imports`); on files it **modifies** run `uvx ruff check <file>` and fix findings in the lines you touched by hand. Wherever a lint step below writes `check` before `format`, run them in that format-then-check order. Files this plan modifies are CRLF in the working tree: after each task run `git diff --stat` and confirm only the touched lines changed (a whole-file diff means the line endings flipped — restore with `git checkout -- <file>` and redo the edit).
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Commit with `git add <explicit paths>` — never `git add -A` (parallel sessions share worktrees).

## Consumes from R1 (verified against `docs/superpowers/plans/2026-10-03-lembretes-r1-fundacao.md`)

R1 must be merged into this worktree before Task 1 (R2 depends on R1, spec §9). Names below are copied from R1's "Interfaces → Produces" block; if the merged R1 code differs, adapt the call sites here to R1's exact spelling and keep the behavior.

| Name | Used for |
|---|---|
| `secretaria.models.AppointmentReminder` (table `appointment_reminders`, file `models/appointment_reminder.py`): `id`, `tenant_id`, `appointment_id`, `patient_id`, `kind`, `appointment_start_at`, `due_at`, `status`, `channel`, `with_prompt`, `attempts`, `last_error_code`, `sent_at`, `answered_at`, `answer`, `warn_due_at`, `warned_at`, `warn_kind`, `created_at`, `updated_at`; unique per (appointment, kind, start) | everything |
| `secretaria.models.appointment_reminder` string constants: `REMINDER_KIND_{CUSTOM,DAY,HOUR,CHAT}`, `REMINDER_STATUS_{PENDING,SENDING,SENT,FAILED,SKIPPED,CANCELLED}`, `REMINDER_CHANNEL_{WHATSAPP,EMAIL,CHAT}`, `REMINDER_ANSWER_{CONFIRM,CANCEL,OTHER}`, `REMINDER_WARN_{UNCONFIRMED,DELIVERY_FAILED}` | every status/kind write and comparison |
| `Appointment.confirmation_count` (0–2), `first_confirmed_at`, `last_confirmed_at` | stop-prompts-after-two |
| `Tenant.reminders_v2_enabled` (default false), `Tenant.reminder_extra_lead_minutes` | the switch; the extra lead is read only inside `schedule_reminders` |
| `services/reminder_schedule.py::schedule_reminders(session, appointment, tenant, *, now) -> list[AppointmentReminder]` — returns `[]` for switch OFF / no patient / no start / terminal status; skips already-due kinds; idempotent; sets `warn_due_at`; never commits | booking hook + reconcile |
| `cancel_reminders(session, appointment_id, *, reason) -> int` — cancels `pending` **and `sending`** rows, clears pending warnings; never commits | close hook |
| `reschedule_reminders(session, appointment, tenant, *, now) -> list[AppointmentReminder]` — cancels old rows, zeroes the counter (even with the switch OFF), recreates | reschedule hook |
| `register_confirmation(session, *, appointment, reminder_id, source, now) -> int` — returns the new count; stale/cancelled row or same row twice → count unchanged; foreign row → raises `ReminderMismatchError`; terminal appointment → unchanged | `remconfirm` tap |
| `CONFIRMATION_SOURCE_REMINDER_BUTTON = "reminder_button"`, `ReminderMismatchError` | `remconfirm` tap |

Already wired by R1, **not** touched here: hub `PATCH /appointments/{id}/status` (confirmed → counter, scheduled → reset, terminal → `cancel_reminders`, all in the request transaction). R1's checkpoint is `docs/CHECKPOINT_lembretes_r1.md`.

## Produces for R3 / R4 / R5

- `schemas/webhook.py::decode_action_id(raw: str | None) -> tuple[str, str] | None` — pure decoder of `"<action>|<uuid>"` ids, now including `remconfirm` / `remcancel` / `remother` (R3 calls it on a Portal `interactive_reply_id`).
- `workers/shared/reminder_actions.py::handle_reminder_button(reply: _ReplyContext, action: str, reminder_id: str, redis=None) -> None` — channel-aware (uses `_reply_sender`), validates tenant **and** patient against the row. R3 wires Portal taps into it and replaces `cancel_path_buttons(appointment_id) -> list[tuple[str, str]]` (interim card `Remarcar` / `Não vou mais`) with the three-way card.
- `services/reminder_text.py`: `ReminderContent`, `load_reminder_content(session, tenant, appointment) -> ReminderContent`, `reminder_sentence(content) -> str`, `build_reminder_body(content) -> str`, `reminder_buttons(reminder_id) -> list[tuple[str, str]]`, `REMINDER_ACTIONS` — R3 reuses them for the chat opening message (`kind='chat'` rows are never touched by the cron).
- Row semantics R4 reads: after a successful send with buttons `status='sent'`, `warn_kind='unconfirmed'`, `warn_due_at` as R1 set it (`due_at` + delay); after a send without buttons `warn_due_at=NULL`, `warn_kind=NULL`; after exhausted/permanent failure `status='failed'`, `warn_kind='delivery_failed'`, `warn_due_at=<failure time>`; a row closed by a guard (`cancelled`/`skipped`) has `warn_due_at=NULL`. **R4 must only warn on `status IN ('sent','failed')`**: a row that is still `pending` (e.g. clinic subscription inactive) keeps R1's `warn_due_at` but was never sent.
- `services/reminder_hooks.py::enabled_for(tenant) -> bool`, `after_appointment_booked(appointment_id, *, now=None) -> int`, `after_appointment_rescheduled(appointment_id, *, now=None) -> int`, `after_appointment_closed(appointment_id, *, reason: str) -> int` — R4's "liberar horário" calls `after_appointment_closed(..., reason="released")` after its commit.

## Review Focus

Each line is pinned by a test in the task named at the end.

1. Two workers claiming the same row: exactly one send; the loser sees `claim_reminder` return `None` — **Task 7**.
2. Send fails, retries, then gives up: attempts 1–3 go back to `pending` with `last_error_code`; attempt 4 ends `failed` + `warn_kind='delivery_failed'` — **Task 8**.
3. Reschedule between scheduling and sending: the old-version row (its `appointment_start_at` ≠ the appointment's) is `cancelled`/`stale_version` and nothing is sent — **Task 7**.
4. `Patient.reminder_opt_out` respected: row `skipped`/`opt_out`, no client built — **Task 7**.
5. Patient with neither phone nor e-mail: WhatsApp patient without `wa_id` → `failed`/`no_channel`; Portal patient without e-mail → chat message recorded, `failed`/`no_email`; both raise the delivery-failed warning — **Tasks 6 and 8**.
6. Portal patient with e-mail: buttons recorded in the conversation, e-mail sent with the `/clinicas/?convite=<tenant_id>` link, row `sent` with `channel='email'` — **Task 6**.
7. Booking made for someone else: text says `A consulta de {atendido}` — **Task 2**.
8. Clinic timezone ≠ UTC (including a day rollover): date and time rendered in the clinic zone — **Task 2**.
9. Requirements with line breaks inside a template parameter: flattened, no `\n`/`\t`/5 spaces — **Task 2**.
10. Confirm tap carrying another patient's reminder id: `Não encontrei essa consulta.`, nothing counted — **Task 13**.
11. Confirm tap after the appointment was cancelled: `Essa consulta não está mais ativa.`, nothing counted — **Task 13**.
12. Second Confirmar on the same message: count stays 1 — **Task 13**.
13. Third reminder after two confirmations: plain text without buttons, `warn_due_at` empty — **Task 8**.
14. Switch OFF: the old cron sends the exact same text it sends today, the new cron touches nothing, no hook is called on any booking path — **Tasks 10, 11, 12**.
15. Pix paid-deposit appointment: still the 3-button `Confirmar` / `Reagendar` / `Cancelar` variant (`apptresched|`/`apptcancel|` keep the Pix limits; `Confirmar` becomes `remconfirm|` so it counts) and the deposit template outside the window — **Task 6**.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/secretaria/config.py` | modify | `REMINDER_V2_TEMPLATE_NAME`, `REMINDER_V2_TEMPLATE_APPROVED`, `REMINDER_V2_BATCH_SIZE`, `BRAIN_MESSAGE_PORTAL_URL` |
| `src/secretaria/services/reminder_text.py` | create | the words: content loader, sentence, body, one-line text, template variables, button ids, Portal link |
| `docs/LEMBRETES_MODELOS_META.md` | create | exact Meta template texts to submit + activation procedure |
| `src/secretaria/schemas/webhook.py` | modify | `remconfirm|`/`remcancel|`/`remother|` prefixes, `decode_action_id` |
| `src/secretaria/services/email.py` | modify | `appointment_reminder_patient` template |
| `src/secretaria/services/reminder_delivery.py` | create | `ReminderJob`, `DeliveryOutcome`, `deliver_reminder` (WhatsApp window/template/fallback/Pix, Portal chat + e-mail) |
| `src/secretaria/workers/reminder_engine.py` | create | due selection, atomic claim, guards, outcome bookkeeping, retries, usage, cron entry points |
| `src/secretaria/services/reminder_hooks.py` | create | after-commit hooks + reconcile (backfill) |
| `src/secretaria/workers/shared/flow_runner.py` | modify | hooks after the flow's booking / cancel / reschedule commit |
| `src/secretaria/workers/shared/booking_hold.py` | modify | hook after Portal reservation promotion |
| `src/secretaria/ai/tools.py` | modify | hooks after the agent's booking and cancel |
| `src/secretaria/api/hub/calendar.py` | modify | hooks after hub create / cancel / reschedule (the status PATCH is R1's) |
| `src/secretaria/workers/shared/actions.py` | modify | dispatch `rem*` taps; close hook after button cancel |
| `src/secretaria/workers/shared/reminder_actions.py` | create | `handle_reminder_button`, `cancel_path_buttons` |
| `src/secretaria/plugins/reminders.py` | modify | old cron skips v2 clinics and patients without `wa_id` |
| `src/secretaria/workers/arq_worker.py` | modify | register the two new crons |
| `tests/_reminders_v2.py` | create | shared seeds and fakes |
| `tests/test_reminder_v2_*.py` | create | one file per unit (text, delivery, engine, hooks, wiring, hub wiring, buttons) |
| `tests/test_reminders_plugin.py`, `tests/test_build_identity.py`, `tests/test_booking_code_gate.py` | modify | old-cron gating, cron registry, hold-promotion wiring |
| `docs/CHECKPOINT_lembretes_r2.md`, `docs/CHECKPOINT_lembretes_r1.md`, `docs/CHECKPOINT_patient_channel_identity.md`, `CLAUDE.md` | create/modify | state of R2 after validation |

---

## Task 1: Settings, shared test helpers, R1 presence check

**Files:**
- Modify: `src/secretaria/config.py` (after `REMINDER_DEPOSIT_TEMPLATE_NAME`, before the `# --- Brain-Message attachments` comment)
- Create: `tests/_reminders_v2.py`
- Test: `tests/test_reminder_v2_setup.py`

**Interfaces:**
- Consumes: R1 (`AppointmentReminder`, `Tenant.reminders_v2_enabled`, `Appointment.confirmation_count`, `tests/_reminder_fixtures.py::db`).
- Produces: `Settings.REMINDER_V2_TEMPLATE_NAME: str = "lembrete_consulta_v2"`, `Settings.REMINDER_V2_TEMPLATE_APPROVED: bool = False`, `Settings.REMINDER_V2_BATCH_SIZE: int = 200`, `Settings.BRAIN_MESSAGE_PORTAL_URL: str = ""`; test helpers `NOW`, `WA_ID`, `FakeWhatsAppClient`, `fake_waba_token`, `entitled`, `World`, `seed_world`, `add_reminder`, `get_reminder`, `reload_appointment`, `outbound_messages`, `seed_paid_deposit`, `HookSpy`.

- [ ] **Step 0: Confirm R1 is in this worktree**

Run: `cd /c/TECH/BRAIN-worktrees/TASK-032/secretarIA && git log --oneline -15 | grep -i "R1" ; ls src/secretaria/services/reminder_schedule.py src/secretaria/models/appointment_reminder.py tests/_reminder_fixtures.py`
Expected: R1 commits listed and the three files exist. If not, STOP: R2 cannot start before R1 is merged into `task/TASK-032-lembretes-e-confirmacao`.

- [ ] **Step 1: Write the shared helpers**

Create `tests/_reminders_v2.py`:

```python
"""Seeds and fakes shared by the TASK-032 R2 reminder-engine tests.

The in-memory database fixture is R1's (`tests/_reminder_fixtures.py::db`,
imported by each test module). This module only adds plain builders and a
recording WhatsApp double, so every test file stays explicit about what it
patches.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from dataclasses import dataclass  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

from sqlalchemy import select  # noqa: E402

from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentReminder,
    AppointmentStatus,
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    PixDeposit,
    PixDepositStatus,
    Professional,
    Tenant,
)
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, CHANNEL_WHATSAPP  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.whatsapp import TenantWhatsAppCredentialMissing  # noqa: E402

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
WA_ID = "5511988887777"


class FakeWhatsAppClient:
    """Records every send; installed in place of `WhatsAppClient`.

    `failing_templates` makes `send_template` refuse those names (a template
    Meta has not approved); `fail_everything` makes every send raise (Meta down).
    """

    persists_outbound = False
    created: list["FakeWhatsAppClient"] = []
    failing_templates: set[str] = set()
    fail_everything: bool = False

    def __init__(self, tenant_id=None) -> None:
        self.tenant_id = tenant_id
        self.sent: list[tuple] = []
        FakeWhatsAppClient.created.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.created = []
        cls.failing_templates = set()
        cls.fail_everything = False

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        if not (tenant.phone_number_id or "").strip() or not (waba_token or "").strip():
            raise TenantWhatsAppCredentialMissing(tenant.id, ("access_token",))
        return cls(tenant_id=tenant.id)

    @classmethod
    def all_sent(cls) -> list[tuple]:
        return [item for client in cls.created for item in client.sent]

    def _gate(self) -> None:
        if FakeWhatsAppClient.fail_everything:
            raise RuntimeError("meta unavailable")

    async def send_text_message(self, to, body):
        self._gate()
        self.sent.append(("text", to, body))
        return {"messages": [{"id": "wamid.text"}]}

    async def send_buttons(self, to, body, buttons):
        self._gate()
        self.sent.append(("buttons", to, body, list(buttons)))
        return {"messages": [{"id": "wamid.buttons"}]}

    async def send_template(self, to, template, lang, variables, button_payloads=None):
        self._gate()
        if template in FakeWhatsAppClient.failing_templates:
            raise RuntimeError("template not approved")
        self.sent.append(("template", to, template, lang, list(variables), button_payloads))
        return {"messages": [{"id": "wamid.template"}]}

    async def send_list(self, to, body, button_label, rows, section_title="Opções"):
        self._gate()
        self.sent.append(("list", to, body, button_label, rows, section_title))
        return {"messages": [{"id": "wamid.list"}]}


async def fake_waba_token(session, tenant_id) -> str:
    return "decrypted-waba-token"


async def entitled(tenant_id, redis) -> EntitlementSummary:
    return EntitlementSummary(
        tenant_id=str(tenant_id),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="basico",
        addons={},
        limits={},
    )


@dataclass
class World:
    tenant: Tenant
    patient: Patient
    conversation: Conversation
    appointment: Appointment
    start_at: datetime  # the appointment start, tz-aware UTC (SQLite reads it back naive)


async def seed_world(
    db,
    *,
    v2: bool = True,
    channel: str = CHANNEL_WHATSAPP,
    wa_id: str | None = WA_ID,
    email: str | None = None,
    opt_out: bool = False,
    start_at: datetime | None = None,
    status: AppointmentStatus = AppointmentStatus.SCHEDULED,
    attendee_name: str | None = None,
    timezone: str = "America/Sao_Paulo",
    requirements: list[str] | None = None,
    confirmation_count: int = 0,
    professional_name: str | None = None,
    last_inbound_at: datetime | None = None,
    phone_number_id: str | None = "pnid-1",
    google_event_id: str | None = None,
) -> World:
    """One clinic, one patient, one conversation, one appointment."""
    start = start_at or NOW + timedelta(days=3)
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clínica Olhar",
            phone_number_id=phone_number_id,
            is_active=True,
            timezone=timezone,
            reminders_v2_enabled=v2,
            appointment_types=[
                {"name": "Consulta", "is_active": True, "requirements": list(requirements or [])}
            ],
        )
        session.add(tenant)
        await session.flush()
        professional_id = None
        if professional_name:
            professional = Professional(id=uuid4(), tenant_id=tenant.id, name=professional_name)
            session.add(professional)
            await session.flush()
            professional_id = professional.id
        if channel == CHANNEL_BRAIN_MESSAGE:
            patient = Patient(
                id=uuid4(),
                tenant_id=tenant.id,
                wa_id=None,
                channel=CHANNEL_BRAIN_MESSAGE,
                external_id=str(uuid4()),
                name="Maria",
                email=email,
                reminder_opt_out=opt_out,
            )
        else:
            patient = Patient(
                id=uuid4(),
                tenant_id=tenant.id,
                wa_id=wa_id,
                name="Maria",
                email=email,
                reminder_opt_out=opt_out,
            )
        session.add(patient)
        await session.flush()
        conversation = Conversation(id=uuid4(), tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        if last_inbound_at is not None:
            session.add(
                Message(
                    conversation_id=conversation.id,
                    direction=MessageDirection.INBOUND,
                    sender=MessageSender.PATIENT,
                    body="oi",
                    created_at=last_inbound_at,
                )
            )
        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            conversation_id=conversation.id,
            google_event_id=google_event_id if google_event_id is not None else f"evt-{uuid4()}",
            appointment_type="Consulta",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            status=status,
            professional_id=professional_id,
            attendee_name=attendee_name,
            confirmation_count=confirmation_count,
        )
        session.add(appointment)
        await session.commit()
        for row in (tenant, patient, conversation, appointment):
            await session.refresh(row)
        return World(tenant, patient, conversation, appointment, start)


async def add_reminder(
    db,
    world: World,
    *,
    kind: str = "day",
    due_at: datetime | None = None,
    status: str = "pending",
    with_prompt: bool = True,
    attempts: int = 0,
    appointment_start_at: datetime | None = None,
) -> UUID:
    """A planned row, as R1's `schedule_reminders` would have written it."""
    due = due_at or NOW - timedelta(minutes=1)
    async with db() as session:
        row = AppointmentReminder(
            id=uuid4(),
            tenant_id=world.tenant.id,
            appointment_id=world.appointment.id,
            patient_id=world.patient.id,
            kind=kind,
            appointment_start_at=appointment_start_at or world.start_at,
            due_at=due,
            status=status,
            channel="whatsapp",
            with_prompt=with_prompt,
            attempts=attempts,
            warn_due_at=due + timedelta(hours=2),
        )
        session.add(row)
        await session.commit()
        return row.id


async def get_reminder(db, reminder_id: UUID) -> AppointmentReminder:
    async with db() as session:
        return await session.get(AppointmentReminder, reminder_id)


async def reload_appointment(db, appointment_id: UUID) -> Appointment:
    async with db() as session:
        return await session.get(Appointment, appointment_id)


async def outbound_messages(db, conversation_id: UUID) -> list[Message]:
    async with db() as session:
        rows = await session.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.direction == MessageDirection.OUTBOUND,
            )
            .order_by(Message.created_at)
        )
        return list(rows)


async def seed_paid_deposit(db, world: World) -> None:
    async with db() as session:
        session.add(
            PixDeposit(
                id=uuid4(),
                tenant_id=world.tenant.id,
                appointment_id=world.appointment.id,
                patient_id=world.patient.id,
                asaas_payment_id=f"pay-{uuid4()}",
                amount_cents=10000,
                percent_applied=30,
                status=PixDepositStatus.PAID,
            )
        )
        await session.commit()


class HookSpy:
    """Stands in for services/reminder_hooks.py's three hooks and records the calls."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def install(self, monkeypatch, hooks_module) -> "HookSpy":
        async def booked(appointment_id, *, now=None):
            self.calls.append(("booked", appointment_id))
            return 0

        async def moved(appointment_id, *, now=None):
            self.calls.append(("moved", appointment_id))
            return 0

        async def closed(appointment_id, *, reason):
            self.calls.append(("closed", appointment_id, reason))
            return 0

        monkeypatch.setattr(hooks_module, "after_appointment_booked", booked)
        monkeypatch.setattr(hooks_module, "after_appointment_rescheduled", moved)
        monkeypatch.setattr(hooks_module, "after_appointment_closed", closed)
        return self
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_reminder_v2_setup.py`:

```python
"""R2 settings and the shared helpers (TASK-032 R2)."""

from datetime import timedelta

from secretaria.config import get_settings
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import NOW, add_reminder, get_reminder, seed_world


def test_r2_settings_defaults():
    settings = get_settings()
    assert settings.REMINDER_V2_TEMPLATE_NAME == "lembrete_consulta_v2"
    assert settings.REMINDER_V2_TEMPLATE_APPROVED is False
    assert settings.REMINDER_V2_BATCH_SIZE == 200
    assert settings.BRAIN_MESSAGE_PORTAL_URL == ""


async def test_seed_world_builds_a_v2_clinic_with_a_planned_row(db):  # noqa: F811
    world = await seed_world(db)
    rid = await add_reminder(db, world, due_at=NOW - timedelta(minutes=1))

    row = await get_reminder(db, rid)
    assert world.tenant.reminders_v2_enabled is True
    assert world.appointment.confirmation_count == 0
    assert row.status == "pending" and row.kind == "day" and row.attempts == 0
```

- [ ] **Step 3: Run it to verify it fails**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_setup.py -q`
Expected: `test_r2_settings_defaults` FAILS with `AttributeError: 'Settings' object has no attribute 'REMINDER_V2_TEMPLATE_NAME'`; the seed test passes (it only needs R1).

- [ ] **Step 4: Add the settings**

In `src/secretaria/config.py`, right after the line `REMINDER_DEPOSIT_TEMPLATE_NAME: str = "appointment_reminder_deposit"`, insert:

```python

    # --- Reminder engine v2 (TASK-032 R2, workers/reminder_engine.py) ---
    # Approved Meta utility template with 6 body parameters and 3 quick-reply
    # buttons (Confirmar / Cancelar / Outro). The exact texts to submit are in
    # docs/LEMBRETES_MODELOS_META.md. Templates are approved per WABA.
    REMINDER_V2_TEMPLATE_NAME: str = "lembrete_consulta_v2"
    # False until Meta has approved REMINDER_V2_TEMPLATE_NAME on the WABAs in
    # use. While False, a reminder that must go OUTSIDE the 24h window is sent
    # with the plain REMINDER_TEMPLATE_NAME (one variable, no buttons) and the
    # patient answers through the chat. When True and Meta still refuses the
    # template on some WABA, the engine falls back to the plain template too.
    REMINDER_V2_TEMPLATE_APPROVED: bool = False
    # Rows claimed per one-minute tick, so one tick never outlives the next.
    REMINDER_V2_BATCH_SIZE: int = 200
    # Base URL of the Brain-Message patient portal - the SAME value brain-api
    # has under this name. The Portal reminder e-mail links to
    # {BRAIN_MESSAGE_PORTAL_URL}/clinicas/?convite=<tenant_id>, the invite shape
    # brain-api's core/invite_codes.py::parse_invite accepts. Empty = the
    # e-mail carries no link (it tells the patient to open the portal).
    BRAIN_MESSAGE_PORTAL_URL: str = ""
```

- [ ] **Step 5: Run it to verify it passes**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_setup.py -q`
Expected: `2 passed`.

- [ ] **Step 6: Lint, diff check, commit**

Run: `uvx ruff check tests/_reminders_v2.py tests/test_reminder_v2_setup.py && uvx ruff format tests/_reminders_v2.py tests/test_reminder_v2_setup.py` then `git diff --stat` (expect `src/secretaria/config.py | 20 +` only — no whole-file diff).

```bash
git add src/secretaria/config.py tests/_reminders_v2.py tests/test_reminder_v2_setup.py
git commit -m "feat(reminders): R2 settings and shared test helpers (TASK-032 R2)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 2: The reminder text (`services/reminder_text.py`)

**Files:**
- Create: `src/secretaria/services/reminder_text.py`
- Test: `tests/test_reminder_v2_text.py`

**Interfaces:**
- Consumes: `services/service_catalog.py::load_service_catalog`, `::normalize`; `services/tenant_config.py::active_appointment_types`, `::professional_appointment_types`; `core/whatsapp_limits.py::MAX_INTERACTIVE_BODY_CHARS`.
- Produces: `ReminderContent` (frozen dataclass: `clinic_name: str`, `timezone: str | None`, `start_at: datetime`, `service_name: str`, `doctor_name: str | None = None`, `attendee_name: str | None = None`, `requirements: tuple[str, ...] = ()`); `local_start(content) -> datetime`; `reminder_sentence(content) -> str`; `build_reminder_body(content, *, max_chars=MAX_INTERACTIVE_BODY_CHARS) -> str`; `flatten_requirements(requirements, *, max_chars=REQUIREMENTS_PARAM_MAX_CHARS) -> str`; `single_line_text(content) -> str`; `template_variables(content) -> list[str]` (6 items); `async load_reminder_content(session, tenant, appointment) -> ReminderContent`; constants `TEMPLATE_PARAM_COUNT = 6`, `REQUIREMENTS_PARAM_MAX_CHARS = 400`, `SINGLE_LINE_MAX_CHARS = 900`, `REQUIREMENTS_HEADER`, `NO_REQUIREMENTS_PARAM`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reminder_v2_text.py`:

```python
"""services/reminder_text.py — the words of the reminder (TASK-032 R2, spec §4.2)."""

from datetime import UTC, datetime

from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS
from secretaria.models import Appointment, Tenant
from secretaria.services import reminder_text as rt
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import seed_world

SENTENCE = (
    "LEMBRE-SE: Sua consulta com Dra. Ana Souza está marcada para o dia "
    "10/10/2026 às 14:30 para Consulta."
)


def _content(**overrides) -> rt.ReminderContent:
    base = dict(
        clinic_name="Clínica Olhar",
        timezone="America/Sao_Paulo",
        start_at=datetime(2026, 10, 10, 17, 30, tzinfo=UTC),
        service_name="Consulta",
        doctor_name="Dra. Ana Souza",
    )
    base.update(overrides)
    return rt.ReminderContent(**base)


def test_sentence_is_the_spec_text_in_the_clinic_timezone():
    assert rt.reminder_sentence(_content()) == SENTENCE


def test_day_rollover_uses_the_clinic_day_not_utc():
    content = _content(start_at=datetime(2026, 10, 11, 1, 30, tzinfo=UTC))
    assert "dia 10/10/2026 às 22:30" in rt.reminder_sentence(content)


def test_another_clinic_timezone_is_honoured():
    content = _content(timezone="America/Manaus")  # UTC-4
    assert "dia 10/10/2026 às 13:30" in rt.reminder_sentence(content)


def test_a_naive_start_is_read_as_utc():
    content = _content(start_at=datetime(2026, 10, 10, 17, 30))  # SQLite read-back
    assert rt.reminder_sentence(content) == SENTENCE


def test_an_unknown_timezone_falls_back_to_sao_paulo():
    assert rt.reminder_sentence(_content(timezone="Mars/Olympus")) == SENTENCE


def test_a_booking_for_someone_else_names_the_attendee():
    sentence = rt.reminder_sentence(_content(attendee_name="João Pedro"))
    assert sentence.startswith("LEMBRE-SE: A consulta de João Pedro com Dra. Ana Souza ")


def test_without_a_professional_the_clinic_team_is_named():
    sentence = rt.reminder_sentence(_content(doctor_name=None))
    assert "Sua consulta com a equipe da Clínica Olhar está marcada" in sentence


def test_body_lists_the_requirements_as_bullets():
    body = rt.build_reminder_body(_content(requirements=("Jejum de 8h", "Trazer exames.")))
    assert body == (
        f"{SENTENCE}\n\nOrientações para a consulta:\n• Jejum de 8h\n• Trazer exames"
    )


def test_body_without_requirements_is_just_the_sentence():
    assert rt.build_reminder_body(_content()) == SENTENCE


def test_body_fits_the_interactive_limit_by_dropping_requirements_never_the_sentence():
    many = tuple(f"Orientação número {i} " + "x" * 80 for i in range(20))
    body = rt.build_reminder_body(_content(requirements=many))
    assert len(body) <= MAX_INTERACTIVE_BODY_CHARS
    assert body.startswith(SENTENCE)
    assert body.endswith("\n…")


def test_requirements_with_line_breaks_become_one_valid_template_parameter():
    variables = rt.template_variables(
        _content(requirements=("Jejum de 8h\nsem água", "Trazer\texames     anteriores"))
    )
    assert len(variables) == rt.TEMPLATE_PARAM_COUNT == 6
    for value in variables:
        assert value
        assert "\n" not in value and "\t" not in value and "     " not in value
    assert variables[5] == "Orientações: Jejum de 8h sem água; Trazer exames anteriores."


def test_template_variables_are_in_the_meta_order():
    assert rt.template_variables(_content()) == [
        "Sua consulta",
        "Dra. Ana Souza",
        "10/10/2026",
        "14:30",
        "Consulta",
        "Sem orientações especiais.",
    ]


def test_single_line_text_has_no_line_break():
    line = rt.single_line_text(_content(requirements=("a\nb",)))
    assert "\n" not in line
    assert line == f"{SENTENCE} Orientações: a b."


def test_a_long_requirements_parameter_is_capped_and_marked():
    value = rt.flatten_requirements(("y" * 1000,))
    assert len(value) == rt.REQUIREMENTS_PARAM_MAX_CHARS
    assert value.endswith("…")


async def test_content_is_loaded_from_the_booking(db):  # noqa: F811
    world = await seed_world(
        db,
        professional_name="Dra. Ana Souza",
        requirements=["Jejum de 8h\nsem água"],
        attendee_name="João Pedro",
        timezone="America/Manaus",
    )
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        content = await rt.load_reminder_content(session, tenant, appointment)

    assert content.clinic_name == "Clínica Olhar"
    assert content.timezone == "America/Manaus"
    assert content.doctor_name == "Dra. Ana Souza"
    assert content.service_name == "Consulta"
    assert content.attendee_name == "João Pedro"
    assert content.requirements == ("Jejum de 8h\nsem água",)


async def test_a_professional_of_another_clinic_is_never_named(db):  # noqa: F811
    world = await seed_world(db)
    other = await seed_world(db, professional_name="Dr. Outro")
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.professional_id = other.appointment.professional_id
        await session.commit()
        tenant = await session.get(Tenant, world.tenant.id)
        content = await rt.load_reminder_content(session, tenant, appointment)
    assert content.doctor_name is None


async def test_a_service_missing_from_the_catalog_keeps_its_name_without_requirements(db):  # noqa: F811
    world = await seed_world(db, requirements=["Jejum"])
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.appointment_type = "Retorno"
        await session.commit()
        tenant = await session.get(Tenant, world.tenant.id)
        content = await rt.load_reminder_content(session, tenant, appointment)
    assert content.service_name == "Retorno"
    assert content.requirements == ()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_text.py -q`
Expected: collection error `ModuleNotFoundError` / `ImportError: cannot import name 'reminder_text'`.

- [ ] **Step 3: Implement**

Create `src/secretaria/services/reminder_text.py`:

```python
"""The words of an appointment reminder, built once for every surface (TASK-032 R2).

Spec: docs/superpowers/specs/2026-10-03-lembretes-e-confirmacao-design.md §4.2.

One content record (`ReminderContent`, loaded by `load_reminder_content`) feeds
every rendering, so the WhatsApp card, the WhatsApp templates, the Portal chat
bubble and the Portal e-mail can never disagree about the doctor, the day or
the time:

* `reminder_sentence` - the spec sentence, verbatim.
* `build_reminder_body` - sentence + requirement bullets, capped at the
  interactive-body limit by dropping bullets (never the sentence).
* `single_line_text` - the same in one line: the only variable of the plain
  fallback template and of the Pix deposit template.
* `template_variables` - the six parameters of the approved v2 template
  (docs/LEMBRETES_MODELOS_META.md).

Dates and times are always rendered in the CLINIC's timezone; a naive
timestamp (SQLite read-back) is UTC. Meta refuses a template parameter with a
newline, a tab or more than four consecutive spaces, so every parameter goes
through `_one_line`. Nothing here logs patient content.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS
from secretaria.models import Appointment, Professional, Tenant
from secretaria.services.service_catalog import (
    load_service_catalog,
    normalize as normalize_service_name,
)
from secretaria.services.tenant_config import (
    active_appointment_types,
    professional_appointment_types,
)

logger = get_logger(__name__)

DEFAULT_TIMEZONE = "America/Sao_Paulo"
DEFAULT_SERVICE_NAME = "Consulta"
REQUIREMENTS_HEADER = "Orientações para a consulta:"
NO_REQUIREMENTS_PARAM = "Sem orientações especiais."
# Display-only caps (nothing matches against these strings, so a plain marked
# cut is right - skill third-party-text-limits). A Meta template body is at
# most 1024 characters INCLUDING its fixed text, so the variables stay well
# under that.
REQUIREMENTS_PARAM_MAX_CHARS = 400
SINGLE_LINE_MAX_CHARS = 900
TEMPLATE_PARAM_COUNT = 6
CUT_MARK = "…"


@dataclass(frozen=True)
class ReminderContent:
    """Everything a reminder says, already resolved from the booking."""

    clinic_name: str
    timezone: str | None
    start_at: datetime
    service_name: str
    doctor_name: str | None = None
    attendee_name: str | None = None
    requirements: tuple[str, ...] = ()


def _one_line(value: object) -> str:
    """Collapse every run of whitespace (newline, tab, 5 spaces) into one space."""
    return " ".join(str(value or "").split())


def _cut(text: str, limit: int) -> str:
    """Keep the head and mark the cut. Idempotent for text already within `limit`."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + CUT_MARK


def _clinic_tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def local_start(content: ReminderContent) -> datetime:
    """The appointment start on the clinic's wall clock."""
    start = content.start_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    return start.astimezone(_clinic_tz(content.timezone))


def _subject(content: ReminderContent) -> str:
    attendee = _one_line(content.attendee_name)
    return f"A consulta de {attendee}" if attendee else "Sua consulta"


def _doctor(content: ReminderContent) -> str:
    return _one_line(content.doctor_name) or f"a equipe da {_one_line(content.clinic_name)}"


def _service(content: ReminderContent) -> str:
    return _one_line(content.service_name) or DEFAULT_SERVICE_NAME


def _requirement_items(requirements: Iterable[object]) -> list[str]:
    items: list[str] = []
    for raw in requirements or ():
        item = _one_line(raw).rstrip(" .;")
        if item:
            items.append(item)
    return items


def reminder_sentence(content: ReminderContent) -> str:
    """The spec sentence (§4.2), with date AND time in the clinic's timezone."""
    local = local_start(content)
    return (
        f"LEMBRE-SE: {_subject(content)} com {_doctor(content)} está marcada para o dia "
        f"{local:%d/%m/%Y} às {local:%H:%M} para {_service(content)}."
    )


def build_reminder_body(
    content: ReminderContent, *, max_chars: int = MAX_INTERACTIVE_BODY_CHARS
) -> str:
    """Sentence + one bullet per requirement, within `max_chars`.

    Over budget, requirement bullets are dropped from the end (a final "…"
    line says some were left out); the sentence itself is never cut unless it
    alone is over budget.
    """
    sentence = reminder_sentence(content)
    items = _requirement_items(content.requirements)
    for keep in range(len(items), 0, -1):
        bullets = "\n".join(f"• {item}" for item in items[:keep])
        if keep < len(items):
            bullets += f"\n{CUT_MARK}"
        body = f"{sentence}\n\n{REQUIREMENTS_HEADER}\n{bullets}"
        if len(body) <= max_chars:
            return body
    return _cut(sentence, max_chars)


def flatten_requirements(
    requirements: Iterable[object], *, max_chars: int = REQUIREMENTS_PARAM_MAX_CHARS
) -> str:
    """The requirements as ONE template-safe line (never empty: Meta rejects it)."""
    items = _requirement_items(requirements)
    if not items:
        return NO_REQUIREMENTS_PARAM
    return _cut(f"Orientações: {'; '.join(items)}.", max_chars)


def single_line_text(content: ReminderContent) -> str:
    """Sentence + flattened requirements in one line (single-variable templates)."""
    line = _one_line(f"{reminder_sentence(content)} {flatten_requirements(content.requirements)}")
    return _cut(line, SINGLE_LINE_MAX_CHARS)


def template_variables(content: ReminderContent) -> list[str]:
    """The six parameters of REMINDER_V2_TEMPLATE_NAME, in Meta's {{1}}..{{6}} order."""
    local = local_start(content)
    values = [
        _subject(content),
        _doctor(content),
        f"{local:%d/%m/%Y}",
        f"{local:%H:%M}",
        _service(content),
        flatten_requirements(content.requirements),
    ]
    return [_one_line(value) or "-" for value in values]


async def load_reminder_content(
    session: AsyncSession, tenant: Tenant, appointment: Appointment
) -> ReminderContent:
    """Resolve doctor, service requirements and attendee for one appointment.

    Same catalog resolution as the greeting's upcoming-appointment block
    (workers/shared/greeting.py::_load_upcoming_greeting_data), but the
    professional lookup is scoped to the tenant: a stray foreign id names
    nobody. A catalog failure degrades to "no requirements", never raises.
    """
    owner = None
    if appointment.professional_id is not None:
        owner = await session.scalar(
            select(Professional).where(
                Professional.id == appointment.professional_id,
                Professional.tenant_id == tenant.id,
            )
        )
    service_name = (appointment.appointment_type or "").strip() or DEFAULT_SERVICE_NAME
    requirements: tuple[str, ...] = ()
    try:
        services = await load_service_catalog(session, tenant.id)
        catalog = (
            professional_appointment_types(owner, tenant, services)
            if owner is not None
            else active_appointment_types(tenant, services)
        )
        target = normalize_service_name(service_name)
        match = next(
            (svc for svc in catalog if normalize_service_name(svc.get("name")) == target),
            None,
        )
        if match is not None:
            requirements = tuple(
                str(item).strip() for item in (match.get("requirements") or []) if str(item).strip()
            )
    except Exception as exc:
        logger.warning(
            "reminder_content_catalog_failed",
            tenant_id=str(tenant.id),
            appointment_id=str(appointment.id),
            error_type=type(exc).__name__,
        )
    return ReminderContent(
        clinic_name=tenant.clinic_name,
        timezone=tenant.timezone,
        start_at=appointment.start_at,
        service_name=service_name,
        doctor_name=owner.name if owner is not None else None,
        attendee_name=appointment.attendee_name,
        requirements=requirements,
    )
```

- [ ] **Step 4: Run them to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_text.py -q`
Expected: `17 passed`.

- [ ] **Step 5: Lint and commit**

Run: `uvx ruff check src/secretaria/services/reminder_text.py tests/test_reminder_v2_text.py && uvx ruff format src/secretaria/services/reminder_text.py tests/test_reminder_v2_text.py`. Then scan both files for invisible bidi characters (memory "unicode escape virou caractere literal"): `uv run python -c "import sys;[print(f,hex(ord(c))) for f in sys.argv[1:] for c in open(f,encoding='utf-8').read() if 0x200e<=ord(c)<=0x202e or 0x2066<=ord(c)<=0x2069]" src/secretaria/services/reminder_text.py tests/test_reminder_v2_text.py` → expected: no output.

```bash
git add src/secretaria/services/reminder_text.py tests/test_reminder_v2_text.py
git commit -m "feat(reminders): one text builder for every reminder surface (TASK-032 R2)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 3: Button ids — builders and the patient-scoped decoder

**Files:**
- Modify: `src/secretaria/services/reminder_text.py` (append a "Buttons" section)
- Modify: `src/secretaria/schemas/webhook.py` (`_ACTION_BUTTON_PREFIXES`, new `decode_action_id`, `extract_action_button` body)
- Test: `tests/test_reminder_v2_text.py` (append)

**Interfaces:**
- Consumes: `core/whatsapp_limits.py::MAX_BUTTON_LABEL_CHARS`.
- Produces: in `reminder_text`: `ACTION_CONFIRM = "remconfirm"`, `ACTION_CANCEL = "remcancel"`, `ACTION_OTHER = "remother"`, `REMINDER_ACTIONS: tuple[str, ...]`, `LABEL_CONFIRM = "Confirmar"`, `LABEL_CANCEL = "Cancelar"`, `LABEL_OTHER = "Outro"`, `LABEL_DEPOSIT_RESCHEDULE = "Reagendar"`, `reminder_buttons(reminder_id) -> list[tuple[str, str]]`, `deposit_reminder_buttons(reminder_id, appointment_id) -> list[tuple[str, str]]`, `button_payloads(buttons) -> list[str]`. In `schemas/webhook.py`: `decode_action_id(raw: str | None) -> tuple[str, str] | None`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_reminder_v2_text.py`, add to the top-of-file imports (keep them in the top block; `uvx ruff check --fix` sorts them):

```python
from uuid import uuid4

import pytest

from secretaria.core.whatsapp_limits import MAX_BUTTON_LABEL_CHARS
from secretaria.schemas.webhook import WebhookMessage, decode_action_id, extract_action_button
```

and append:

```python
# --------------------------------------------------------------------------
# Button ids (Task 3)
# --------------------------------------------------------------------------


def _interactive(button_id: str) -> WebhookMessage:
    return WebhookMessage.model_validate(
        {
            "id": "wamid.1",
            "from": "5511999999999",
            "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": "x"}},
        }
    )


def _quick_reply(payload: str) -> WebhookMessage:
    return WebhookMessage.model_validate(
        {
            "id": "wamid.2",
            "from": "5511999999999",
            "type": "button",
            "button": {"payload": payload, "text": "x"},
        }
    )


@pytest.mark.parametrize("action", ["remconfirm", "remcancel", "remother"])
def test_reminder_ids_decode_on_both_whatsapp_carriers(action):
    reminder_id = str(uuid4())
    raw = f"{action}|{reminder_id}"
    assert extract_action_button(_interactive(raw)) == (action, reminder_id)
    assert extract_action_button(_quick_reply(raw)) == (action, reminder_id)
    assert decode_action_id(raw) == (action, reminder_id)


@pytest.mark.parametrize(
    "raw", [None, "", "remconfirm|not-a-uuid", f"remconfirmx|{uuid4()}", f"rem|{uuid4()}"]
)
def test_a_tampered_or_unknown_id_decodes_to_none(raw):
    assert decode_action_id(raw) is None


@pytest.mark.parametrize(
    "action", ["apptconfirm", "apptresched", "apptcancelyes", "apptcancel", "rebooksame"]
)
def test_the_old_ids_still_decode(action):
    appointment_id = str(uuid4())
    assert decode_action_id(f"{action}|{appointment_id}") == (action, appointment_id)


def test_builders_and_decoder_agree():
    reminder_id = uuid4()
    buttons = rt.reminder_buttons(reminder_id)
    assert [decode_action_id(bid) for bid, _ in buttons] == [
        (action, str(reminder_id)) for action in rt.REMINDER_ACTIONS
    ]
    assert [label for _, label in buttons] == ["Confirmar", "Cancelar", "Outro"]
    assert rt.button_payloads(buttons) == [bid for bid, _ in buttons]


def test_every_label_fits_a_whatsapp_button():
    labels = [rt.LABEL_CONFIRM, rt.LABEL_CANCEL, rt.LABEL_OTHER, rt.LABEL_DEPOSIT_RESCHEDULE]
    assert all(0 < len(label) <= MAX_BUTTON_LABEL_CHARS for label in labels)


def test_the_pix_paid_variant_keeps_its_trio_and_pix_scoped_ids():
    reminder_id, appointment_id = uuid4(), uuid4()
    assert rt.deposit_reminder_buttons(reminder_id, appointment_id) == [
        (f"remconfirm|{reminder_id}", "Confirmar"),
        (f"apptresched|{appointment_id}", "Reagendar"),
        (f"apptcancel|{appointment_id}", "Cancelar"),
    ]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_text.py -q`
Expected: collection error `ImportError: cannot import name 'decode_action_id' from 'secretaria.schemas.webhook'`.

- [ ] **Step 3: Add the builders**

Append to `src/secretaria/services/reminder_text.py`:

```python


# --- Buttons ------------------------------------------------------------------
# "<action>|<reminder_id>": the reminder ROW id carries tenant, patient and
# appointment, so a tap can be checked against the patient who tapped
# (workers/shared/reminder_actions.py) - the older "apptconfirm|<appointment_id>"
# family can only be checked against the tenant. The decoder side lists the
# same prefixes in schemas/webhook.py::_ACTION_BUTTON_PREFIXES; a test pins
# that both agree.
ACTION_CONFIRM = "remconfirm"
ACTION_CANCEL = "remcancel"
ACTION_OTHER = "remother"
REMINDER_ACTIONS: tuple[str, ...] = (ACTION_CONFIRM, ACTION_CANCEL, ACTION_OTHER)

# <= 20 characters each (core/whatsapp_limits.py::MAX_BUTTON_LABEL_CHARS). The
# approved template's quick replies carry these exact labels, in this order.
LABEL_CONFIRM = "Confirmar"
LABEL_CANCEL = "Cancelar"
LABEL_OTHER = "Outro"
# The Pix paid-deposit variant keeps its historic trio
# (plugins/reminders.py::_DEPOSIT_REMINDER_BUTTONS). Only Confirmar moves to the
# reminder id (so it counts); Reagendar/Cancelar keep the appointment-scoped ids
# whose handlers apply the Pix reschedule limit and refund window
# (workers/shared/actions.py).
LABEL_DEPOSIT_RESCHEDULE = "Reagendar"


def reminder_buttons(reminder_id) -> list[tuple[str, str]]:
    """(id, label) pairs of a reminder: Confirmar / Cancelar / Outro."""
    return [
        (f"{ACTION_CONFIRM}|{reminder_id}", LABEL_CONFIRM),
        (f"{ACTION_CANCEL}|{reminder_id}", LABEL_CANCEL),
        (f"{ACTION_OTHER}|{reminder_id}", LABEL_OTHER),
    ]


def deposit_reminder_buttons(reminder_id, appointment_id) -> list[tuple[str, str]]:
    """The Pix paid-deposit trio: Confirmar / Reagendar / Cancelar."""
    return [
        (f"{ACTION_CONFIRM}|{reminder_id}", LABEL_CONFIRM),
        (f"apptresched|{appointment_id}", LABEL_DEPOSIT_RESCHEDULE),
        (f"apptcancel|{appointment_id}", LABEL_CANCEL),
    ]


def button_payloads(buttons: list[tuple[str, str]]) -> list[str]:
    """Just the ids, for a template's quick-reply `button_payloads` (same order)."""
    return [button_id for button_id, _label in buttons]
```

- [ ] **Step 4: Teach the decoder the new ids**

In `src/secretaria/schemas/webhook.py`, inside `_ACTION_BUTTON_PREFIXES`, after the line `    "rebookno|",` add:

```python
    # TASK-032 R2: the reminder buttons. The trailing id is an
    # `appointment_reminders` ROW id, not an appointment id -
    # workers/shared/reminder_actions.py resolves it and honours the tap only
    # for the patient of the conversation that tapped.
    "remconfirm|",
    "remcancel|",
    "remother|",
```

Then replace the whole body of `extract_action_button` from the line `    raw: str | None = None` to its final `    return None` with:

```python
    raw: str | None = None
    if msg.interactive is not None and msg.interactive.button_reply is not None:
        raw = msg.interactive.button_reply.id
    elif msg.button is not None:
        raw = msg.button.payload
    return decode_action_id(raw)


def decode_action_id(raw: str | None) -> tuple[str, str] | None:
    """Decode one "<action>|<uuid>" id or payload, whatever carried it.

    The channel-neutral half of `extract_action_button`: a WhatsApp tap reaches
    it through the webhook shapes above; a Portal tap (TASK-032 R3) through the
    stored `Message.interactive_reply_id`. None for an unknown prefix, or when
    the trailing part is not a UUID - a malformed or tampered id must never
    crash routing, and the action alone is useless to every caller.
    """
    if not raw:
        return None
    for prefix in _ACTION_BUTTON_PREFIXES:
        if raw.startswith(prefix):
            target_id = raw[len(prefix) :]
            try:
                UUID(target_id)
            except ValueError:
                return None
            return prefix[:-1], target_id
    return None
```

(`UUID` is already imported in `schemas/webhook.py` — the old body used it.)

- [ ] **Step 5: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_text.py tests/test_action_buttons.py -q`
Expected: all pass (`test_action_buttons.py` proves the old ids are untouched).

- [ ] **Step 6: Lint, diff check, commit**

Run: `uvx ruff check src/secretaria/services/reminder_text.py src/secretaria/schemas/webhook.py tests/test_reminder_v2_text.py`, `uvx ruff format src/secretaria/services/reminder_text.py tests/test_reminder_v2_text.py`, then `git diff --stat` (webhook.py: about +30/-12, not a whole-file diff).

```bash
git add src/secretaria/services/reminder_text.py src/secretaria/schemas/webhook.py tests/test_reminder_v2_text.py
git commit -m "feat(reminders): remconfirm/remcancel/remother ids and decode_action_id (TASK-032 R2)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 4: Meta template texts (doc deliverable, submit first)

The Meta approval is external and slow (spec §4.5: "Os modelos do WhatsApp são submetidos à Meta no início do plano 2"). This task produces the exact texts for the owner to submit, and a test that keeps the document and the code from drifting apart. Hand the document to the owner as soon as this task is committed — do not wait for the rest of the plan.

**Files:**
- Create: `docs/LEMBRETES_MODELOS_META.md`
- Test: `tests/test_reminder_v2_text.py` (append)

**Interfaces:**
- Consumes: `Settings.REMINDER_V2_TEMPLATE_NAME`, `Settings.REMINDER_TEMPLATE_NAME`, `Settings.REMINDER_DEPOSIT_TEMPLATE_NAME`, `reminder_text.TEMPLATE_PARAM_COUNT`, `LABEL_*`, `NO_REQUIREMENTS_PARAM`.
- Produces: `docs/LEMBRETES_MODELOS_META.md` (the submission sheet).

- [ ] **Step 1: Write the failing test**

In `tests/test_reminder_v2_text.py`, add to the top-of-file imports:

```python
import re
from pathlib import Path

from secretaria.config import get_settings
```

and append:

```python
# --------------------------------------------------------------------------
# The Meta submission sheet stays in step with the code (Task 4)
# --------------------------------------------------------------------------

META_DOC = Path(__file__).resolve().parent.parent / "docs" / "LEMBRETES_MODELOS_META.md"


def test_the_meta_sheet_matches_the_code():
    text = META_DOC.read_text(encoding="utf-8")
    settings = get_settings()

    assert f"`{settings.REMINDER_V2_TEMPLATE_NAME}`" in text
    assert f"`{settings.REMINDER_TEMPLATE_NAME}`" in text
    assert f"`{settings.REMINDER_DEPOSIT_TEMPLATE_NAME}`" in text

    body = next(line for line in text.splitlines() if line.startswith("> LEMBRE-SE:"))
    placeholders = re.findall(r"\{\{(\d+)\}\}", body)
    assert placeholders == [str(i) for i in range(1, rt.TEMPLATE_PARAM_COUNT + 1)]
    # Meta refuses a body that starts or ends with a variable.
    assert not body.removeprefix("> ").startswith("{{")
    assert not body.rstrip().endswith("}}")

    for label in (rt.LABEL_CONFIRM, rt.LABEL_CANCEL, rt.LABEL_OTHER):
        assert f"`{label}`" in text
    assert f"`{rt.NO_REQUIREMENTS_PARAM}`" in text
```

- [ ] **Step 2: Run it to verify it fails**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_text.py -q -k meta_sheet`
Expected: FAIL with `FileNotFoundError: ... docs/LEMBRETES_MODELOS_META.md`.

- [ ] **Step 3: Write the sheet**

Create `docs/LEMBRETES_MODELOS_META.md` with exactly this content:

````markdown
# Modelos do WhatsApp para os lembretes (TASK-032 R2)

Folha para submeter à Meta (WhatsApp Manager → Ferramentas da conta → Modelos de mensagem).
Modelos são aprovados **por conta do WhatsApp Business (WABA)**: submeta em cada WABA de clínica
que vai ligar o lembrete novo. A aprovação é externa (minutos a dias). Enquanto o modelo novo não
estiver aprovado, o sistema continua funcionando com o modelo simples de hoje (seção 3).

## 1. Modelo novo com 3 botões — `lembrete_consulta_v2`

| Campo | Valor |
|---|---|
| Nome | `lembrete_consulta_v2` |
| Categoria | Utilidade (Utility) |
| Idioma | Português (BR) — `pt_BR` |
| Cabeçalho | nenhum |
| Rodapé | nenhum |

Corpo (copiar exatamente, em uma única linha):

> LEMBRE-SE: {{1}} com {{2}} está marcada para o dia {{3}} às {{4}} para {{5}}. {{6}} Toque em um dos botões abaixo para responder.

Exemplos que a Meta pede para cada variável:

| Variável | O que entra | Exemplo para a Meta |
|---|---|---|
| `{{1}}` | de quem é a consulta | `Sua consulta` (consulta marcada para outra pessoa: `A consulta de João Pedro`) |
| `{{2}}` | médico(a) | `Dra. Ana Souza` (sem médico cadastrado: `a equipe da Clínica Olhar`) |
| `{{3}}` | data, no fuso da clínica | `10/10/2026` |
| `{{4}}` | hora, no fuso da clínica | `14:30` |
| `{{5}}` | serviço | `Consulta oftalmológica` |
| `{{6}}` | orientações do serviço, numa linha só | `Orientações: jejum de 8 horas; trazer exames anteriores.` (sem orientações: `Sem orientações especiais.`) |

Botões — tipo **Resposta rápida** (quick reply), **nesta ordem** (o sistema envia o código de
cada botão pela posição):

1. `Confirmar`
2. `Cancelar`
3. `Outro`

O que o sistema já garante (não precisa configurar): nenhuma variável vai vazia, com quebra de
linha, tabulação ou mais de 4 espaços seguidos (regra da Meta); as orientações vêm achatadas numa
linha e cortadas em 400 caracteres; os rótulos dos botões têm no máximo 20 caracteres.

## 2. Modelos que já existem e continuam em uso

| Nome | Uso no lembrete novo |
|---|---|
| `appointment_reminder` | Modelo simples de hoje (1 variável, sem botões). Usado fora da janela de 24 h enquanto o modelo novo não está aprovado, se a Meta recusar o modelo novo numa WABA, e sempre que o paciente já confirmou duas vezes (lembrete sem pedido de confirmação). A variável recebe agora o texto novo numa linha: `LEMBRE-SE: Sua consulta com … às 14:30 para Consulta. Orientações: …` |
| `appointment_reminder_deposit` | Consulta com sinal Pix pago: 1 variável (o mesmo texto numa linha) + 3 respostas rápidas `Confirmar` / `Reagendar` / `Cancelar`, como hoje. |

## 3. Configuração e ativação (quem faz: o dono, no painel; nenhuma automação mexe em variáveis)

| Variável de ambiente (API **e** worker) | Valor | Quando |
|---|---|---|
| `REMINDER_V2_TEMPLATE_NAME` | `lembrete_consulta_v2` | só se o nome aprovado for outro |
| `REMINDER_V2_TEMPLATE_APPROVED` | `false` → `true` | trocar para `true` só depois da aprovação na(s) WABA(s) das clínicas ligadas |
| `BRAIN_MESSAGE_PORTAL_URL` | o mesmo valor que o brain-api usa | para o e-mail do Portal levar o link "abrir minha conversa" |

Enquanto `REMINDER_V2_TEMPLATE_APPROVED=false`: dentro da janela de 24 h o paciente recebe a
mensagem com os 3 botões normalmente; fora dela recebe o modelo simples (sem botões) e responde
pela conversa — a mensagem de abertura do chat (plano R3) mostra os botões quando ele escrever.
````

- [ ] **Step 4: Run it to verify it passes**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_text.py -q`
Expected: all pass (Task 2's 17, Task 3's 16, and this one: `34 passed`).

- [ ] **Step 5: Commit**

```bash
git add docs/LEMBRETES_MODELOS_META.md tests/test_reminder_v2_text.py
git commit -m "docs(reminders): Meta template submission sheet for the v2 reminder (TASK-032 R2)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 5: Portal e-mail template and conversation link

**Files:**
- Modify: `src/secretaria/services/email.py` (`_TEMPLATES`, after `"appointment_booked_clinic"`)
- Modify: `src/secretaria/services/reminder_text.py` (append `portal_conversation_link`)
- Test: `tests/test_reminder_v2_text.py` (append)

**Interfaces:**
- Consumes: `Settings.BRAIN_MESSAGE_PORTAL_URL` (Task 1).
- Produces: e-mail template id `"appointment_reminder_patient"` with variables `clinic_name`, `when`, `reminder_text`, `link_line`; `reminder_text.portal_conversation_link(tenant_id) -> str | None`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_reminder_v2_text.py`, add to the top-of-file imports:

```python
from secretaria.services import email as email_service
from secretaria.services.email import EmailOutcome
```

and append:

```python
# --------------------------------------------------------------------------
# Portal e-mail (Task 5)
# --------------------------------------------------------------------------


def test_portal_link_is_the_clinic_invite_link(monkeypatch):
    tenant_id = uuid4()
    monkeypatch.setattr(get_settings(), "BRAIN_MESSAGE_PORTAL_URL", "https://portal.exemplo/", raising=False)
    assert rt.portal_conversation_link(tenant_id) == f"https://portal.exemplo/clinicas/?convite={tenant_id}"


def test_portal_link_is_none_while_unconfigured(monkeypatch):
    monkeypatch.setattr(get_settings(), "BRAIN_MESSAGE_PORTAL_URL", "", raising=False)
    assert rt.portal_conversation_link(uuid4()) is None


async def test_reminder_email_renders_the_reminder_and_the_link(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "EMAIL_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.test", raising=False)
    captured: dict = {}

    def _fake_send(to_email, subject, body):
        captured.update(to=to_email, subject=subject, body=body)

    monkeypatch.setattr(email_service, "_send_transactional_sync", _fake_send)

    outcome = await email_service.send_transactional_email_result(
        to="maria@example.com",
        template="appointment_reminder_patient",
        variables={
            "clinic_name": "Clínica Olhar",
            "when": "10/10/2026 às 14:30",
            "reminder_text": SENTENCE,
            "link_line": "https://portal.exemplo/clinicas/?convite=abc\n",
        },
    )

    assert outcome is EmailOutcome.SENT
    assert captured["subject"] == "Lembrete de consulta — 10/10/2026 às 14:30"
    assert SENTENCE in captured["body"]
    assert "https://portal.exemplo/clinicas/?convite=abc" in captured["body"]
    assert "{" not in captured["body"]  # every placeholder was filled
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_text.py -q -k "portal or email"`
Expected: FAIL — `AttributeError: module 'secretaria.services.reminder_text' has no attribute 'portal_conversation_link'` and `EmailOutcome.UNKNOWN_TEMPLATE`.

- [ ] **Step 3: Add the template**

In `src/secretaria/services/email.py`, inside `_TEMPLATES`, right after the closing `),` of `"appointment_booked_clinic": EmailTemplate(...)`, add:

```python
    # TASK-032 R2: a Portal patient's appointment reminder (workers/reminder_engine.py).
    # `reminder_text` is services/reminder_text.py::build_reminder_body - the very
    # words the chat shows; `link_line` arrives pre-rendered (the clinic invite
    # link, or a sentence when BRAIN_MESSAGE_PORTAL_URL is unset). Nothing beyond
    # what the patient already received when booking.
    "appointment_reminder_patient": EmailTemplate(
        subject="Lembrete de consulta — {when}",
        body=(
            "Olá!\n\n"
            "{reminder_text}\n\n"
            "Para confirmar, cancelar ou falar com a {clinic_name}, abra a sua conversa:\n"
            "{link_line}\n"
            "— {clinic_name}"
        ),
    ),
```

- [ ] **Step 4: Add the link builder**

In `src/secretaria/services/reminder_text.py` add `from secretaria.config import get_settings` to the imports (first-party block, alphabetical: before `from secretaria.core.logging import get_logger`) and append:

```python


def portal_conversation_link(tenant_id) -> str | None:
    """Where the reminder e-mail sends a Portal patient: the clinic's invite link.

    `{BRAIN_MESSAGE_PORTAL_URL}/clinicas/?convite=<tenant uuid>` is a shape
    brain-api already accepts (core/invite_codes.py::parse_invite reads a bare
    clinic UUID; tenant ids are shared across the mesh), so no brain-api call
    and no new contract. A patient with a live session lands in the
    conversation; one without signs in first. None while the URL is unset.
    """
    base = (get_settings().BRAIN_MESSAGE_PORTAL_URL or "").strip().rstrip("/")
    return f"{base}/clinicas/?convite={tenant_id}" if base else None
```

- [ ] **Step 5: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_text.py tests/test_email_transactional.py -q`
Expected: all pass.

- [ ] **Step 6: Lint, diff check, commit**

Run: `uvx ruff check src/secretaria/services/email.py src/secretaria/services/reminder_text.py tests/test_reminder_v2_text.py`, `uvx ruff format src/secretaria/services/reminder_text.py tests/test_reminder_v2_text.py`, `git diff --stat` (email.py: ~+16 lines only).

```bash
git add src/secretaria/services/email.py src/secretaria/services/reminder_text.py tests/test_reminder_v2_text.py
git commit -m "feat(reminders): Portal reminder e-mail and the clinic conversation link (TASK-032 R2)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 6: Channel-aware delivery of one reminder (`services/reminder_delivery.py`)

**Files:**
- Create: `src/secretaria/services/reminder_delivery.py`
- Test: `tests/test_reminder_v2_delivery.py`

**Interfaces:**
- Consumes: Task 2/3/5 (`reminder_text.*`), `services/cancellation_notice.py::is_inside_window`, `::meta_language_code`; `services/channel_sender.py::BrainMessageSender`, `CHANNEL_BRAIN_MESSAGE`, `interactive_history_body`; `services/whatsapp.py::WhatsAppClient`, `TenantWhatsAppCredentialMissing`, `interactive_buttons_record`; `services/brain_patients.py::fetch_patient_email_result`; `services/email.py::send_transactional_email_result`, `EmailOutcome`; R1 constants `REMINDER_CHANNEL_WHATSAPP`, `REMINDER_CHANNEL_EMAIL`.
- Produces:
  - `ReminderJob` (frozen dataclass): `reminder_id: UUID`, `kind: str`, `attempt: int`, `tenant: Tenant`, `patient: Patient`, `appointment_id: UUID`, `content: ReminderContent`, `with_prompt: bool`, `deposit_paid: bool`, `conversation_id: UUID | None`, `waba_token: str | None`, `last_inbound_at: datetime | None`, `now: datetime`.
  - `DeliveryOutcome` (frozen dataclass): `ok: bool`, `channel: str`, `error_code: str | None = None`, `permanent: bool = False`, `billable: bool = False`, `history_body: str | None = None`, `history_interactive: dict | None = None`, `wam_id: str | None = None`.
  - `async deliver_reminder(job: ReminderJob) -> DeliveryOutcome` — never raises.
  - `EMAIL_TEMPLATE = "appointment_reminder_patient"`.

Decision table implemented here (one place, channel skill: "Every outbound send picks its sender by channel"):

| Patient | Window / state | What goes out | `billable` |
|---|---|---|---|
| WhatsApp, no `wa_id` | — | nothing; `no_channel`, permanent | no |
| WhatsApp | inside 24 h, with prompt | `send_buttons` (Confirmar/Cancelar/Outro; Pix paid: Confirmar/Reagendar/Cancelar) | no |
| WhatsApp | inside 24 h, no prompt | `send_text_message` | no |
| WhatsApp | outside, prompt, Pix paid | `REMINDER_DEPOSIT_TEMPLATE_NAME` + 3 payloads → on refusal plain template | yes |
| WhatsApp | outside, prompt, v2 approved | `REMINDER_V2_TEMPLATE_NAME` (6 vars) + 3 payloads → on refusal plain template | yes |
| WhatsApp | outside, prompt, v2 not approved | plain `REMINDER_TEMPLATE_NAME` (1 var), `error_code="plain_template"` | yes |
| WhatsApp | outside, no prompt | plain `REMINDER_TEMPLATE_NAME` | yes |
| Portal | attempt 1 | chat message (buttons or text) via `BrainMessageSender`, then e-mail | no |
| Portal | attempt ≥ 2 | e-mail only (the chat copy is never duplicated) | no |
| Portal | no e-mail at brain-api nor stored | `no_email`, permanent (chat copy still written on attempt 1) | no |

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reminder_v2_delivery.py`:

```python
"""services/reminder_delivery.py — one reminder, the patient's own channel (TASK-032 R2)."""

from datetime import timedelta
from uuid import uuid4

import pytest

from secretaria.config import get_settings
from secretaria.core import database as core_database
from secretaria.services import reminder_delivery
from secretaria.services.brain_patients import PatientEmailResult
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.email import EmailOutcome
from secretaria.services.reminder_delivery import ReminderJob, deliver_reminder
from secretaria.services.reminder_text import ReminderContent, single_line_text
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import NOW, WA_ID, FakeWhatsAppClient, outbound_messages, seed_world


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    monkeypatch.setattr(core_database, "async_session_factory", db)
    FakeWhatsAppClient.reset()
    monkeypatch.setattr(reminder_delivery, "WhatsAppClient", FakeWhatsAppClient)
    yield


@pytest.fixture
def mail(monkeypatch):
    """Portal e-mail: brain-api answers an address; the mailer records the send."""
    sent: list[dict] = []
    state = {"email": "maria@example.com", "outcome": EmailOutcome.SENT}

    async def _fetch(tenant_id, external_id):
        return PatientEmailResult(available=True, email=state["email"])

    async def _send(to, template, variables):
        sent.append({"to": to, "template": template, "variables": variables})
        return state["outcome"]

    monkeypatch.setattr(reminder_delivery, "fetch_patient_email_result", _fetch)
    monkeypatch.setattr(reminder_delivery, "send_transactional_email_result", _send)
    monkeypatch.setattr(get_settings(), "BRAIN_MESSAGE_PORTAL_URL", "https://portal.exemplo", raising=False)
    return sent, state


def _job(world, *, with_prompt=True, deposit_paid=False, inbound_ago=None, attempt=1,
         waba_token="decrypted-waba-token", reminder_id=None) -> ReminderJob:
    content = ReminderContent(
        clinic_name=world.tenant.clinic_name,
        timezone="America/Sao_Paulo",
        start_at=world.start_at,
        service_name="Consulta",
        doctor_name="Dra. Ana Souza",
        requirements=("Jejum de 8h",),
    )
    return ReminderJob(
        reminder_id=reminder_id or uuid4(),
        kind="day",
        attempt=attempt,
        tenant=world.tenant,
        patient=world.patient,
        appointment_id=world.appointment.id,
        content=content,
        with_prompt=with_prompt,
        deposit_paid=deposit_paid,
        conversation_id=world.conversation.id,
        waba_token=waba_token,
        last_inbound_at=(NOW - inbound_ago) if inbound_ago is not None else None,
        now=NOW,
    )


# ---- WhatsApp, inside the 24h window ----------------------------------------


async def test_inside_the_window_the_reminder_carries_the_three_buttons(db):  # noqa: F811
    world = await seed_world(db)
    job = _job(world, inbound_ago=timedelta(hours=2))

    outcome = await deliver_reminder(job)

    [(kind, to, body, buttons)] = FakeWhatsAppClient.all_sent()
    assert kind == "buttons" and to == WA_ID
    assert body.startswith("LEMBRE-SE: Sua consulta com Dra. Ana Souza")
    assert buttons == [
        (f"remconfirm|{job.reminder_id}", "Confirmar"),
        (f"remcancel|{job.reminder_id}", "Cancelar"),
        (f"remother|{job.reminder_id}", "Outro"),
    ]
    assert outcome.ok and not outcome.billable and outcome.channel == "whatsapp"
    assert outcome.wam_id == "wamid.buttons"
    assert outcome.history_body.endswith("(opções: Confirmar, Cancelar, Outro)")
    assert outcome.history_interactive["kind"] == "buttons"


async def test_inside_the_window_without_prompt_it_is_plain_text(db):  # noqa: F811
    world = await seed_world(db)

    outcome = await deliver_reminder(_job(world, with_prompt=False, inbound_ago=timedelta(hours=2)))

    [(kind, to, body)] = FakeWhatsAppClient.all_sent()
    assert kind == "text" and to == WA_ID and "opções" not in body
    assert outcome.ok and outcome.history_interactive is None


# ---- WhatsApp, outside the window -------------------------------------------


async def test_outside_the_window_while_unapproved_the_plain_template_goes(db):  # noqa: F811
    world = await seed_world(db)
    job = _job(world, inbound_ago=timedelta(hours=30))

    outcome = await deliver_reminder(job)

    [(kind, to, template, lang, variables, payloads)] = FakeWhatsAppClient.all_sent()
    assert (kind, to, template, lang) == ("template", WA_ID, "appointment_reminder", "pt_BR")
    assert variables == [single_line_text(job.content)]
    assert payloads is None
    assert outcome.ok and outcome.billable and outcome.error_code == "plain_template"


async def test_outside_the_window_once_approved_the_v2_template_has_the_buttons(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "REMINDER_V2_TEMPLATE_APPROVED", True, raising=False)
    world = await seed_world(db)
    job = _job(world)  # never wrote: outside the window

    outcome = await deliver_reminder(job)

    [(_, _, template, _, variables, payloads)] = FakeWhatsAppClient.all_sent()
    assert template == "lembrete_consulta_v2"
    assert len(variables) == 6 and variables[0] == "Sua consulta"
    assert payloads == [
        f"remconfirm|{job.reminder_id}",
        f"remcancel|{job.reminder_id}",
        f"remother|{job.reminder_id}",
    ]
    assert outcome.ok and outcome.billable and outcome.error_code is None


async def test_a_template_meta_refuses_falls_back_to_the_plain_one(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "REMINDER_V2_TEMPLATE_APPROVED", True, raising=False)
    FakeWhatsAppClient.failing_templates = {"lembrete_consulta_v2"}
    world = await seed_world(db)

    outcome = await deliver_reminder(_job(world))

    [(_, _, template, _, _, payloads)] = FakeWhatsAppClient.all_sent()
    assert template == "appointment_reminder" and payloads is None
    assert outcome.ok and outcome.error_code == "plain_template"


async def test_after_two_confirmations_outside_the_window_no_buttons_ever(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "REMINDER_V2_TEMPLATE_APPROVED", True, raising=False)
    world = await seed_world(db)

    outcome = await deliver_reminder(_job(world, with_prompt=False))

    [(_, _, template, _, _, payloads)] = FakeWhatsAppClient.all_sent()
    assert template == "appointment_reminder" and payloads is None
    assert outcome.ok and outcome.error_code is None


# ---- Pix paid deposit keeps its 3-button variant ----------------------------


async def test_pix_paid_inside_the_window_keeps_confirm_reschedule_cancel(db):  # noqa: F811
    world = await seed_world(db)
    job = _job(world, deposit_paid=True, inbound_ago=timedelta(hours=1))

    await deliver_reminder(job)

    [(_, _, _, buttons)] = FakeWhatsAppClient.all_sent()
    assert buttons == [
        (f"remconfirm|{job.reminder_id}", "Confirmar"),
        (f"apptresched|{world.appointment.id}", "Reagendar"),
        (f"apptcancel|{world.appointment.id}", "Cancelar"),
    ]


async def test_pix_paid_outside_the_window_uses_the_deposit_template(db):  # noqa: F811
    world = await seed_world(db)  # v2 template NOT approved: irrelevant for the deposit one
    job = _job(world, deposit_paid=True)

    outcome = await deliver_reminder(job)

    [(_, _, template, _, variables, payloads)] = FakeWhatsAppClient.all_sent()
    assert template == "appointment_reminder_deposit"
    assert variables == [single_line_text(job.content)]
    assert payloads == [
        f"remconfirm|{job.reminder_id}",
        f"apptresched|{world.appointment.id}",
        f"apptcancel|{world.appointment.id}",
    ]
    assert outcome.ok and outcome.billable


# ---- WhatsApp failures -----------------------------------------------------


async def test_a_whatsapp_patient_without_phone_is_a_permanent_no_channel(db):  # noqa: F811
    world = await seed_world(db, wa_id=None)

    outcome = await deliver_reminder(_job(world))

    assert FakeWhatsAppClient.created == []
    assert (outcome.ok, outcome.error_code, outcome.permanent) == (False, "no_channel", True)


async def test_missing_clinic_credentials_are_permanent(db):  # noqa: F811
    world = await seed_world(db)

    outcome = await deliver_reminder(_job(world, waba_token=None))

    assert (outcome.ok, outcome.error_code, outcome.permanent) == (
        False,
        "whatsapp_credential_missing",
        True,
    )


async def test_a_meta_outage_is_a_retryable_failure(db):  # noqa: F811
    FakeWhatsAppClient.fail_everything = True
    world = await seed_world(db)

    outcome = await deliver_reminder(_job(world, inbound_ago=timedelta(hours=1)))

    assert (outcome.ok, outcome.error_code, outcome.permanent) == (False, "RuntimeError", False)


# ---- Portal ----------------------------------------------------------------


async def test_portal_patient_gets_the_chat_card_and_the_email_with_the_link(db, mail):  # noqa: F811
    sent, _ = mail
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE)
    job = _job(world)

    outcome = await deliver_reminder(job)

    assert FakeWhatsAppClient.created == []  # never the WhatsApp client
    [row] = await outbound_messages(db, world.conversation.id)
    assert row.body.startswith("LEMBRE-SE: Sua consulta")
    assert [o["id"] for o in row.interactive["options"]] == [
        f"remconfirm|{job.reminder_id}",
        f"remcancel|{job.reminder_id}",
        f"remother|{job.reminder_id}",
    ]
    [mail_sent] = sent
    assert mail_sent["to"] == "maria@example.com"
    assert mail_sent["template"] == "appointment_reminder_patient"
    assert mail_sent["variables"]["link_line"] == (
        f"https://portal.exemplo/clinicas/?convite={world.tenant.id}\n"
    )
    assert mail_sent["variables"]["reminder_text"].startswith("LEMBRE-SE:")
    assert outcome.ok and outcome.channel == "email" and outcome.history_body is None


async def test_portal_patient_without_email_keeps_the_chat_copy_and_fails_permanently(db, mail):  # noqa: F811
    sent, state = mail
    state["email"] = None
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE, email=None)

    outcome = await deliver_reminder(_job(world))

    assert len(await outbound_messages(db, world.conversation.id)) == 1
    assert sent == []
    assert (outcome.ok, outcome.channel, outcome.error_code, outcome.permanent) == (
        False,
        "email",
        "no_email",
        True,
    )


async def test_a_portal_retry_sends_the_email_again_but_never_a_second_chat_copy(db, mail):  # noqa: F811
    sent, _ = mail
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE)

    await deliver_reminder(_job(world, attempt=2))

    assert await outbound_messages(db, world.conversation.id) == []
    assert len(sent) == 1


async def test_a_transient_mail_failure_is_retryable(db, mail):  # noqa: F811
    _, state = mail
    state["outcome"] = EmailOutcome.SEND_FAILED
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE)

    outcome = await deliver_reminder(_job(world))

    assert (outcome.ok, outcome.error_code, outcome.permanent) == (False, "email_send_failed", False)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_delivery.py -q`
Expected: collection error `ImportError: cannot import name 'reminder_delivery'`.

- [ ] **Step 3: Implement**

Create `src/secretaria/services/reminder_delivery.py`:

```python
"""Deliver ONE appointment reminder on the patient's own channel (TASK-032 R2).

The engine (workers/reminder_engine.py) claims a row, builds a `ReminderJob`
and calls `deliver_reminder`; this module decides HOW, in one place (skill
channel-aware-dispatch: a Portal patient has `wa_id=None` by design and must
never reach the Graph API):

* WhatsApp inside Meta's 24h window: free interactive card with the three
  buttons (or plain text once the patient confirmed twice).
* WhatsApp outside the window: a Meta-approved template, billed. The v2
  template (6 variables + Confirmar/Cancelar/Outro) once
  `REMINDER_V2_TEMPLATE_APPROVED`, the Pix deposit template for a paid deposit,
  and the plain one-variable template otherwise - also as the fallback when Meta
  refuses either of the other two (precedent: plugins/reminders.py's deposit
  fallback). Never a duplicate: a refused template raised before anything
  reached the patient.
* Portal: the reminder is written into the conversation (where it waits as
  the first thing the patient sees) and mailed with the clinic's link. The
  chat copy is written on the FIRST attempt only; retries repeat the e-mail.

Returns a `DeliveryOutcome`; never raises. The engine books the outcome
(status, retries, warnings, the WhatsApp history row, usage). Logs carry ids
and codes only - never a phone number, an address or the text.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import httpx

from secretaria.config import get_settings
from secretaria.core import database as core_database
from secretaria.core.logging import get_logger
from secretaria.models import Patient, Tenant
from secretaria.models.appointment_reminder import (
    REMINDER_CHANNEL_EMAIL,
    REMINDER_CHANNEL_WHATSAPP,
)
from secretaria.services import cancellation_notice
from secretaria.services.brain_patients import fetch_patient_email_result
from secretaria.services.channel_sender import (
    CHANNEL_BRAIN_MESSAGE,
    BrainMessageSender,
    interactive_history_body,
)
from secretaria.services.email import EmailOutcome, send_transactional_email_result
from secretaria.services.reminder_text import (
    ReminderContent,
    build_reminder_body,
    button_payloads,
    deposit_reminder_buttons,
    local_start,
    portal_conversation_link,
    reminder_buttons,
    single_line_text,
    template_variables,
)
from secretaria.services.whatsapp import (
    TenantWhatsAppCredentialMissing,
    WhatsAppClient,
    interactive_buttons_record,
)

logger = get_logger(__name__)

EMAIL_TEMPLATE = "appointment_reminder_patient"
NO_LINK_LINE = "Entre no portal da clínica e abra a conversa.\n"


@dataclass(frozen=True)
class ReminderJob:
    """Everything one delivery needs, resolved by the engine inside its session."""

    reminder_id: UUID
    kind: str
    attempt: int
    tenant: Tenant
    patient: Patient
    appointment_id: UUID
    content: ReminderContent
    with_prompt: bool
    deposit_paid: bool
    conversation_id: UUID | None
    waba_token: str | None
    last_inbound_at: datetime | None
    now: datetime


@dataclass(frozen=True)
class DeliveryOutcome:
    """What happened. `permanent` failures are never retried."""

    ok: bool
    channel: str
    error_code: str | None = None
    permanent: bool = False
    billable: bool = False
    # WhatsApp only: the history copy the ENGINE writes as an outbound Message
    # (the Portal sender writes its own row - `persists_outbound`).
    history_body: str | None = None
    history_interactive: dict | None = None
    wam_id: str | None = None


def _error_code(exc: Exception) -> str:
    """A short code for `last_error_code` - never the exception text (may echo PII)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    return type(exc).__name__[:64]


def _wam_id(response) -> str | None:
    try:
        return response["messages"][0]["id"]
    except (KeyError, IndexError, TypeError):
        return None


def _buttons(job: ReminderJob) -> list[tuple[str, str]] | None:
    if not job.with_prompt:
        return None
    if job.deposit_paid:
        return deposit_reminder_buttons(job.reminder_id, job.appointment_id)
    return reminder_buttons(job.reminder_id)


def _whatsapp_ok(response, body: str, buttons, *, billable: bool, error_code=None) -> DeliveryOutcome:
    if buttons:
        history = interactive_history_body(body, [label for _, label in buttons])
        interactive = interactive_buttons_record(body, buttons)
    else:
        history, interactive = body, None
    return DeliveryOutcome(
        ok=True,
        channel=REMINDER_CHANNEL_WHATSAPP,
        billable=billable,
        history_body=history,
        history_interactive=interactive,
        wam_id=_wam_id(response),
        error_code=error_code,
    )


async def deliver_reminder(job: ReminderJob) -> DeliveryOutcome:
    """Send one reminder. Never raises: every failure is an outcome."""
    try:
        if job.patient.channel == CHANNEL_BRAIN_MESSAGE:
            return await _deliver_portal(job)
        return await _deliver_whatsapp(job)
    except Exception as exc:  # defensive: a bug here must not strand the row
        logger.warning(
            "reminder_delivery_crashed",
            reminder_id=str(job.reminder_id),
            error_type=type(exc).__name__,
        )
        return DeliveryOutcome(ok=False, channel=REMINDER_CHANNEL_WHATSAPP, error_code=_error_code(exc))


async def _deliver_whatsapp(job: ReminderJob) -> DeliveryOutcome:
    to = (job.patient.wa_id or "").strip()
    if not to:
        return DeliveryOutcome(
            ok=False, channel=REMINDER_CHANNEL_WHATSAPP, error_code="no_channel", permanent=True
        )
    try:
        client = WhatsAppClient.for_tenant(job.tenant, job.waba_token)
    except TenantWhatsAppCredentialMissing:
        return DeliveryOutcome(
            ok=False,
            channel=REMINDER_CHANNEL_WHATSAPP,
            error_code="whatsapp_credential_missing",
            permanent=True,
        )
    buttons = _buttons(job)
    try:
        if cancellation_notice.is_inside_window(job.last_inbound_at, now=job.now):
            body = build_reminder_body(job.content)
            if buttons:
                response = await client.send_buttons(to, body, buttons)
            else:
                response = await client.send_text_message(to=to, body=body)
            return _whatsapp_ok(response, body, buttons, billable=False)
        return await _send_outside_window(client, job, to, buttons)
    except Exception as exc:
        return DeliveryOutcome(ok=False, channel=REMINDER_CHANNEL_WHATSAPP, error_code=_error_code(exc))


async def _send_outside_window(client, job: ReminderJob, to: str, buttons) -> DeliveryOutcome:
    """Template send (billed). The plain template is both the default and the fallback."""
    settings = get_settings()
    lang = cancellation_notice.meta_language_code(job.tenant.language)
    line = single_line_text(job.content)
    if buttons:
        template = None
        if job.deposit_paid:
            template, variables = settings.REMINDER_DEPOSIT_TEMPLATE_NAME, [line]
        elif settings.REMINDER_V2_TEMPLATE_APPROVED:
            template, variables = settings.REMINDER_V2_TEMPLATE_NAME, template_variables(job.content)
        if template is not None:
            try:
                response = await client.send_template(
                    to=to,
                    template=template,
                    lang=lang,
                    variables=variables,
                    button_payloads=button_payloads(buttons),
                )
                return _whatsapp_ok(response, line, buttons, billable=True)
            except Exception as exc:
                logger.warning(
                    "reminder_template_fallback",
                    reminder_id=str(job.reminder_id),
                    template=template,
                    error_type=type(exc).__name__,
                )
    response = await client.send_template(
        to=to, template=settings.REMINDER_TEMPLATE_NAME, lang=lang, variables=[line]
    )
    return _whatsapp_ok(
        response, line, None, billable=True, error_code="plain_template" if buttons else None
    )


async def _portal_email(job: ReminderJob) -> str | None:
    """brain-api is the identity authority; the stored copy only when it is down."""
    stored = (job.patient.email or "").strip() or None
    if not job.patient.external_id:
        return stored
    fetched = await fetch_patient_email_result(job.tenant.id, job.patient.external_id)
    return fetched.email if fetched.available else stored


async def _deliver_portal(job: ReminderJob) -> DeliveryOutcome:
    if job.conversation_id is None:
        return DeliveryOutcome(
            ok=False, channel=REMINDER_CHANNEL_EMAIL, error_code="no_conversation", permanent=True
        )
    body = build_reminder_body(job.content)
    if job.attempt == 1:
        sender = BrainMessageSender(
            conversation_id=job.conversation_id,
            session_factory=core_database.async_session_factory,
        )
        buttons = _buttons(job)
        to = job.patient.external_id or ""
        try:
            if buttons:
                await sender.send_buttons(to, body, buttons)
            else:
                await sender.send_text_message(to, body)
        except Exception as exc:
            # Our own database refused the row: not something a retry fixes
            # without a human, and retrying would risk a second chat copy.
            return DeliveryOutcome(
                ok=False,
                channel=REMINDER_CHANNEL_EMAIL,
                error_code=f"chat_{_error_code(exc)}"[:64],
                permanent=True,
            )
    email = await _portal_email(job)
    if not email:
        return DeliveryOutcome(
            ok=False, channel=REMINDER_CHANNEL_EMAIL, error_code="no_email", permanent=True
        )
    link = portal_conversation_link(job.tenant.id)
    variables = {
        "clinic_name": job.tenant.clinic_name,
        "when": local_start(job.content).strftime("%d/%m/%Y às %H:%M"),
        "reminder_text": body,
        "link_line": f"{link}\n" if link else NO_LINK_LINE,
    }
    outcome = await send_transactional_email_result(
        to=email, template=EMAIL_TEMPLATE, variables=variables
    )
    if outcome is EmailOutcome.SENT:
        return DeliveryOutcome(ok=True, channel=REMINDER_CHANNEL_EMAIL)
    return DeliveryOutcome(
        ok=False,
        channel=REMINDER_CHANNEL_EMAIL,
        error_code=f"email_{outcome.value}",
        permanent=not outcome.is_transient,
    )
```

- [ ] **Step 4: Run them to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_delivery.py -q`
Expected: `15 passed`.

- [ ] **Step 5: Lint and commit**

Run: `uvx ruff check src/secretaria/services/reminder_delivery.py tests/test_reminder_v2_delivery.py && uvx ruff format src/secretaria/services/reminder_delivery.py tests/test_reminder_v2_delivery.py`

```bash
git add src/secretaria/services/reminder_delivery.py tests/test_reminder_v2_delivery.py
git commit -m "feat(reminders): channel-aware delivery of one reminder (TASK-032 R2)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 7: The engine — due selection, atomic claim, guards (`workers/reminder_engine.py`)

**Files:**
- Create: `src/secretaria/workers/reminder_engine.py`
- Test: `tests/test_reminder_v2_engine.py`

**Interfaces:**
- Consumes: Task 6 (`ReminderJob`, `DeliveryOutcome`, `deliver_reminder`), Task 2 (`load_reminder_content`), R1 (`AppointmentReminder`, constants, `reminder_schedule.MAX_CONFIRMATIONS`), `services/entitlements_client.py::get_entitlements`, `services/tenant_config.py::get_waba_token`, `services/cancellation_notice.py::last_inbound_at`, `services/payments/deposit_lifecycle.py::get_deposit_for_appointment`.
- Produces:
  - `TickReport` (dataclass, int fields `claimed`, `sent`, `retried`, `failed`, `closed`, `deferred`; method `add(result: str)`).
  - `async due_reminder_ids(now: datetime, *, limit: int) -> list[tuple[UUID, UUID]]` (reminder id, tenant id).
  - `async claim_reminder(reminder_id: UUID) -> int | None` (the attempt number this claim is, or None when it lost).
  - `async run_reminder_tick(*, now: datetime, redis=None) -> TickReport`.
  - `async process_appointment_reminders(ctx: dict) -> None` (the arq cron, registered in Task 12).
  - `MAX_ATTEMPTS = 4`, `MAX_LATENESS: dict[str, timedelta]` (`custom` 6 h, `day` 3 h, `hour` 30 min), `DEFAULT_MAX_LATENESS = timedelta(minutes=30)`.
  - Close codes written to `last_error_code`: `appointment_gone`, `appointment_closed`, `stale_version`, `appointment_started`, `too_late`, `opt_out`, `no_patient`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reminder_v2_engine.py`:

```python
"""workers/reminder_engine.py — claim, guards, outcomes (TASK-032 R2, spec §4.2)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select  # noqa: F401  (used by the Task 8 tests)

from secretaria.core import database as core_database
from secretaria.models import AppointmentReminder, AppointmentStatus
from secretaria.services import reminder_delivery
from secretaria.services.entitlements_client import EntitlementSummary
from secretaria.workers import reminder_engine
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import (
    NOW,
    FakeWhatsAppClient,
    add_reminder,
    entitled,
    fake_waba_token,
    get_reminder,
    seed_world,
)


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    monkeypatch.setattr(core_database, "async_session_factory", db)
    FakeWhatsAppClient.reset()
    monkeypatch.setattr(reminder_delivery, "WhatsAppClient", FakeWhatsAppClient)
    monkeypatch.setattr(reminder_engine, "get_waba_token", fake_waba_token)
    monkeypatch.setattr(reminder_engine, "get_entitlements", entitled)
    yield


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


async def _tick(now: datetime = NOW) -> reminder_engine.TickReport:
    return await reminder_engine.run_reminder_tick(now=now)


async def _set(db, reminder_id, **values) -> None:  # noqa: F811
    async with db() as session:
        row = await session.get(AppointmentReminder, reminder_id)
        for key, value in values.items():
            setattr(row, key, value)
        await session.commit()


# ---- sending once ----------------------------------------------------------


async def test_a_due_row_is_sent_once_and_marked_sent(db):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world)

    report = await _tick()

    assert (report.claimed, report.sent) == (1, 1)
    [(kind, *_rest)] = FakeWhatsAppClient.all_sent()
    assert kind == "buttons"
    row = await get_reminder(db, rid)
    assert (row.status, row.channel, row.attempts) == ("sent", "whatsapp", 1)
    assert _utc(row.sent_at) == NOW

    await _tick(NOW + timedelta(minutes=1))
    assert len(FakeWhatsAppClient.all_sent()) == 1


async def test_two_claims_on_the_same_row_only_one_wins(db):  # noqa: F811
    world = await seed_world(db)
    rid = await add_reminder(db, world)

    assert await reminder_engine.claim_reminder(rid) == 1
    assert await reminder_engine.claim_reminder(rid) is None
    assert (await get_reminder(db, rid)).status == "sending"


async def test_a_worker_holding_a_stale_list_cannot_send_twice(db, monkeypatch):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world)

    async def _stale_list(now, *, limit):
        return [(rid, world.tenant.id)]  # what a second worker read before the first claimed

    monkeypatch.setattr(reminder_engine, "due_reminder_ids", _stale_list)

    first = await _tick()
    second = await _tick()

    assert (first.sent, second.claimed, second.sent) == (1, 0, 0)
    assert len(FakeWhatsAppClient.all_sent()) == 1


# ---- what is never touched -------------------------------------------------


async def test_a_clinic_with_the_switch_off_is_never_touched(db):  # noqa: F811
    world = await seed_world(db, v2=False)
    rid = await add_reminder(db, world)

    report = await _tick()

    assert report == reminder_engine.TickReport()
    row = await get_reminder(db, rid)
    assert (row.status, row.attempts) == ("pending", 0)
    assert FakeWhatsAppClient.created == []


async def test_a_row_not_yet_due_waits(db):  # noqa: F811
    world = await seed_world(db)
    rid = await add_reminder(db, world, due_at=NOW + timedelta(minutes=5))

    assert (await _tick()).claimed == 0
    assert (await get_reminder(db, rid)).status == "pending"


async def test_chat_rows_belong_to_the_chat_and_are_never_sent(db):  # noqa: F811
    world = await seed_world(db)
    rid = await add_reminder(db, world, kind="chat")

    assert (await _tick()).claimed == 0
    assert (await get_reminder(db, rid)).status == "pending"


async def test_an_inactive_subscription_leaves_the_row_waiting(db, monkeypatch):  # noqa: F811
    async def _inactive(tenant_id, redis):
        return EntitlementSummary(
            tenant_id=str(tenant_id),
            status="canceled",
            active=False,
            secretaria_enabled=True,
            plan="bronze",
            secretaria_tier="basico",
            addons={},
            limits={},
        )

    monkeypatch.setattr(reminder_engine, "get_entitlements", _inactive)
    world = await seed_world(db)
    rid = await add_reminder(db, world)

    report = await _tick()

    assert (report.deferred, report.claimed) == (1, 0)
    row = await get_reminder(db, rid)
    assert (row.status, row.attempts) == ("pending", 0)


# ---- guards: closed without sending ----------------------------------------


async def test_a_row_planned_for_the_old_time_is_cancelled_not_sent(db):  # noqa: F811
    """Reschedule between scheduling and sending: the row's version is stale."""
    world = await seed_world(db)
    rid = await add_reminder(db, world, appointment_start_at=world.start_at - timedelta(days=1))

    report = await _tick()

    row = await get_reminder(db, rid)
    assert report.closed == 1
    assert (row.status, row.last_error_code, row.warn_due_at) == ("cancelled", "stale_version", None)
    assert FakeWhatsAppClient.created == []


@pytest.mark.parametrize(
    "status", [AppointmentStatus.CANCELLED, AppointmentStatus.ATTENDED, AppointmentStatus.NO_SHOW]
)
async def test_a_terminal_appointment_cancels_the_row(db, status):  # noqa: F811
    world = await seed_world(db, status=status)
    rid = await add_reminder(db, world)

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.last_error_code) == ("cancelled", "appointment_closed")
    assert FakeWhatsAppClient.created == []


async def test_a_row_pointing_at_another_clinics_appointment_is_cancelled(db):  # noqa: F811
    world = await seed_world(db)
    other = await seed_world(db)
    rid = await add_reminder(db, world)
    await _set(db, rid, appointment_id=other.appointment.id)

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.last_error_code) == ("cancelled", "appointment_gone")
    assert FakeWhatsAppClient.created == []


async def test_an_hour_reminder_more_than_30_minutes_late_is_skipped(db):  # noqa: F811
    world = await seed_world(db, start_at=NOW + timedelta(minutes=29))
    rid = await add_reminder(db, world, kind="hour", due_at=NOW - timedelta(minutes=31))

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.last_error_code) == ("skipped", "too_late")


async def test_an_appointment_that_already_started_is_not_reminded(db):  # noqa: F811
    world = await seed_world(db, start_at=NOW - timedelta(minutes=5))
    rid = await add_reminder(db, world)

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.last_error_code) == ("skipped", "appointment_started")


async def test_an_opted_out_patient_is_skipped_without_building_a_client(db):  # noqa: F811
    world = await seed_world(db, opt_out=True)
    rid = await add_reminder(db, world)

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.last_error_code, row.warn_due_at) == ("skipped", "opt_out", None)
    assert FakeWhatsAppClient.created == []


async def test_a_row_without_patient_fails_and_warns_the_clinic(db):  # noqa: F811
    world = await seed_world(db)
    rid = await add_reminder(db, world)
    await _set(db, rid, patient_id=None)

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.last_error_code, row.warn_kind) == (
        "failed",
        "no_patient",
        "delivery_failed",
    )
    assert _utc(row.warn_due_at) == NOW
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_engine.py -q`
Expected: collection error `ImportError: cannot import name 'reminder_engine' from 'secretaria.workers'`.

- [ ] **Step 3: Implement**

Create `src/secretaria/workers/reminder_engine.py`:

```python
"""The reminder engine: send what `appointment_reminders` planned (TASK-032 R2).

Spec §4.2. R1's services/reminder_schedule.py writes one row per planned
reminder; this cron (`process_appointment_reminders`, every minute) sends the
due ones. Per tick:

1. `due_reminder_ids` - pending, due, not `chat`, of clinics with
   `reminders_v2_enabled`. A clinic with the switch OFF is never even read
   (plugins/reminders.py keeps serving it exactly as before).
2. Entitlement gate per clinic, the old cron's rule: subscription active and
   secretarIA enabled. A gated row is NOT claimed - it waits, and the lateness
   guard retires it if the clinic stays gated for too long.
3. `claim_reminder` - `UPDATE ... SET status='sending', attempts=attempts+1
   WHERE id=:id AND status='pending'`. Only the statement that flips the row
   sees rowcount 1 (Postgres re-checks the WHERE after the row lock), so two
   worker copies - or one holding a stale list - can never both send.
4. `_prepare` - re-reads the row with its appointment and closes it without
   sending when the appointment is gone or terminal, was moved since the row
   was planned (`appointment_start_at` is the version), already started, or
   the reminder is too late to be useful; skips an opted-out patient.
5. services/reminder_delivery.py::deliver_reminder - the channel decision.
6. `_finish` - books the outcome.

Nothing here logs patient content: ids, kinds and codes only.
"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update

from secretaria.config import get_settings
from secretaria.core import database as core_database
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    AppointmentReminder,
    Conversation,
    Patient,
    PixDepositStatus,
    Tenant,
    is_live_status,
)
from secretaria.models.appointment_reminder import (
    REMINDER_KIND_CHAT,
    REMINDER_KIND_CUSTOM,
    REMINDER_KIND_DAY,
    REMINDER_KIND_HOUR,
    REMINDER_STATUS_CANCELLED,
    REMINDER_STATUS_FAILED,
    REMINDER_STATUS_PENDING,
    REMINDER_STATUS_SENDING,
    REMINDER_STATUS_SENT,
    REMINDER_STATUS_SKIPPED,
    REMINDER_WARN_DELIVERY_FAILED,
)
from secretaria.services import cancellation_notice
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.entitlements_client import EntitlementSummary, get_entitlements
from secretaria.services.payments import deposit_lifecycle
from secretaria.services.reminder_delivery import (
    DeliveryOutcome,
    ReminderJob,
    deliver_reminder,
)
from secretaria.services.reminder_schedule import MAX_CONFIRMATIONS
from secretaria.services.reminder_text import load_reminder_content
from secretaria.services.tenant_config import get_waba_token

logger = get_logger(__name__)

# Same budget as the cancellation notice (workers/whatsapp/notifications.py::
# CANCEL_NOTICE_MAX_TRIES): 4 attempts, ~1 minute apart (one per tick).
MAX_ATTEMPTS = 4
# A reminder that would arrive this late is noise, not help. Never later than
# the appointment itself either (`appointment_started`).
MAX_LATENESS: dict[str, timedelta] = {
    REMINDER_KIND_CUSTOM: timedelta(hours=6),
    REMINDER_KIND_DAY: timedelta(hours=3),
    REMINDER_KIND_HOUR: timedelta(minutes=30),
}
DEFAULT_MAX_LATENESS = timedelta(minutes=30)


@dataclass
class TickReport:
    claimed: int = 0
    sent: int = 0
    retried: int = 0
    failed: int = 0
    closed: int = 0
    deferred: int = 0

    def add(self, result: str) -> None:
        setattr(self, result, getattr(self, result) + 1)


def _as_utc(dt: datetime) -> datetime:
    """Naive timestamps (SQLite) are UTC; aware ones are converted to UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


async def due_reminder_ids(now: datetime, *, limit: int) -> list[tuple[UUID, UUID]]:
    """(reminder id, tenant id) of the rows this tick may try, oldest first."""
    async with core_database.async_session_factory() as session:
        rows = await session.execute(
            select(AppointmentReminder.id, AppointmentReminder.tenant_id)
            .join(Tenant, Tenant.id == AppointmentReminder.tenant_id)
            .where(
                Tenant.reminders_v2_enabled.is_(True),
                AppointmentReminder.status == REMINDER_STATUS_PENDING,
                AppointmentReminder.kind != REMINDER_KIND_CHAT,
                AppointmentReminder.due_at <= now,
            )
            .order_by(AppointmentReminder.due_at)
            .limit(limit)
        )
        return [(row.id, row.tenant_id) for row in rows]


async def claim_reminder(reminder_id: UUID) -> int | None:
    """Flip one row pending -> sending. The attempt number, or None if another claim won."""
    async with core_database.async_session_factory() as session:
        async with session.begin():
            result = await session.execute(
                update(AppointmentReminder)
                .where(
                    AppointmentReminder.id == reminder_id,
                    AppointmentReminder.status == REMINDER_STATUS_PENDING,
                )
                .values(
                    status=REMINDER_STATUS_SENDING,
                    attempts=func.coalesce(AppointmentReminder.attempts, 0) + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                return None
            return await session.scalar(
                select(AppointmentReminder.attempts).where(AppointmentReminder.id == reminder_id)
            )


def _guard(reminder: AppointmentReminder, appointment: Appointment | None, now: datetime):
    """(status, code) that closes the row without sending, or None to go ahead."""
    if appointment is None or appointment.tenant_id != reminder.tenant_id:
        return REMINDER_STATUS_CANCELLED, "appointment_gone"
    if not is_live_status(appointment.status):
        return REMINDER_STATUS_CANCELLED, "appointment_closed"
    if appointment.start_at is None or _as_utc(appointment.start_at) != _as_utc(
        reminder.appointment_start_at
    ):
        return REMINDER_STATUS_CANCELLED, "stale_version"
    if _as_utc(appointment.start_at) <= now:
        return REMINDER_STATUS_SKIPPED, "appointment_started"
    if now - _as_utc(reminder.due_at) > MAX_LATENESS.get(reminder.kind, DEFAULT_MAX_LATENESS):
        return REMINDER_STATUS_SKIPPED, "too_late"
    return None


def _close(reminder: AppointmentReminder, status: str, code: str) -> None:
    """Retire the row without sending; nothing to warn the clinic about."""
    reminder.status = status
    reminder.last_error_code = code
    reminder.warn_due_at = None
    logger.info(
        "reminder_v2_closed", reminder_id=str(reminder.id), status=status, code=code
    )


def _fail(reminder: AppointmentReminder, code: str, now: datetime) -> None:
    """Definitive delivery failure: R4 warns the clinic (warn_kind delivery_failed)."""
    reminder.status = REMINDER_STATUS_FAILED
    reminder.last_error_code = code[:64]
    reminder.warn_kind = REMINDER_WARN_DELIVERY_FAILED
    reminder.warn_due_at = now
    logger.error(
        "reminder_v2_undelivered",
        alarm="reminder_undelivered",
        reminder_id=str(reminder.id),
        tenant_id=str(reminder.tenant_id),
        code=reminder.last_error_code,
    )


async def _prepare(reminder_id: UUID, attempt: int, now: datetime) -> ReminderJob | None:
    """Load and guard one claimed row. None = it was closed here; nothing to send."""
    async with core_database.async_session_factory() as session:
        async with session.begin():
            reminder = await session.get(AppointmentReminder, reminder_id)
            if reminder is None:
                return None
            appointment = await session.get(Appointment, reminder.appointment_id)
            closing = _guard(reminder, appointment, now)
            if closing is not None:
                _close(reminder, *closing)
                return None
            tenant = await session.get(Tenant, reminder.tenant_id)
            patient = (
                await session.get(Patient, reminder.patient_id)
                if reminder.patient_id is not None
                else None
            )
            if tenant is None or patient is None or patient.tenant_id != reminder.tenant_id:
                _fail(reminder, "no_patient", now)
                return None
            if patient.reminder_opt_out:
                _close(reminder, REMINDER_STATUS_SKIPPED, "opt_out")
                return None
            content = await load_reminder_content(session, tenant, appointment)
            deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
            conversation_id = await session.scalar(
                select(Conversation.id)
                .where(Conversation.tenant_id == tenant.id, Conversation.patient_id == patient.id)
                .order_by(Conversation.created_at.desc())
                .limit(1)
            )
            portal = patient.channel == CHANNEL_BRAIN_MESSAGE
            last_inbound = (
                None
                if portal
                else await cancellation_notice.last_inbound_at(session, tenant.id, patient.id)
            )
            waba_token = None if portal else await get_waba_token(session, tenant.id)
            return ReminderJob(
                reminder_id=reminder.id,
                kind=reminder.kind,
                attempt=attempt,
                tenant=tenant,
                patient=patient,
                appointment_id=appointment.id,
                content=content,
                # Recomputed now, not trusted from planning time: two
                # confirmations since then silence the prompt (spec §4.2 "Parada").
                with_prompt=bool(reminder.with_prompt)
                and (appointment.confirmation_count or 0) < MAX_CONFIRMATIONS,
                deposit_paid=deposit is not None and deposit.status == PixDepositStatus.PAID,
                conversation_id=conversation_id,
                waba_token=waba_token,
                last_inbound_at=last_inbound,
                now=now,
            )


async def _finish(
    reminder_id: UUID, job: ReminderJob, outcome: DeliveryOutcome, now: datetime
) -> str:
    """Book the outcome. Returns the TickReport field to count it under."""
    async with core_database.async_session_factory() as session:
        async with session.begin():
            reminder = await session.get(AppointmentReminder, reminder_id)
            if reminder is None:
                return "closed"
            if outcome.ok:
                reminder.status = REMINDER_STATUS_SENT
                reminder.sent_at = now
                reminder.channel = outcome.channel
                reminder.with_prompt = job.with_prompt
                reminder.last_error_code = outcome.error_code
                return "sent"
            reminder.status = REMINDER_STATUS_PENDING
            reminder.last_error_code = outcome.error_code
            return "retried"


async def _release_after_crash(reminder_id: UUID, attempt: int, now: datetime) -> None:
    """A bug between claim and finish must not strand the row in 'sending'."""
    try:
        async with core_database.async_session_factory() as session:
            async with session.begin():
                reminder = await session.get(AppointmentReminder, reminder_id)
                if reminder is None or reminder.status != REMINDER_STATUS_SENDING:
                    return
                if attempt >= MAX_ATTEMPTS:
                    _fail(reminder, "engine_error", now)
                else:
                    reminder.status = REMINDER_STATUS_PENDING
                    reminder.last_error_code = "engine_error"
    except Exception as exc:
        logger.warning(
            "reminder_v2_release_failed",
            reminder_id=str(reminder_id),
            error_type=type(exc).__name__,
        )


async def run_reminder_tick(*, now: datetime, redis=None) -> TickReport:
    """One pass over the due rows. One bad row never stops the others."""
    report = TickReport()
    entitlements: dict[UUID, EntitlementSummary | None] = {}
    limit = get_settings().REMINDER_V2_BATCH_SIZE
    for reminder_id, tenant_id in await due_reminder_ids(now, limit=limit):
        attempt: int | None = None
        try:
            if tenant_id not in entitlements:
                entitlements[tenant_id] = await get_entitlements(tenant_id, redis)
            summary = entitlements[tenant_id]
            if summary is None or not summary.active or not summary.secretaria_enabled:
                report.add("deferred")
                continue
            attempt = await claim_reminder(reminder_id)
            if attempt is None:
                continue
            report.add("claimed")
            job = await _prepare(reminder_id, attempt, now)
            if job is None:
                report.add("closed")
                continue
            outcome = await deliver_reminder(job)
            report.add(await _finish(reminder_id, job, outcome, now))
        except Exception as exc:
            logger.warning(
                "reminder_v2_item_failed",
                reminder_id=str(reminder_id),
                error_type=type(exc).__name__,
            )
            if attempt is not None:
                await _release_after_crash(reminder_id, attempt, now)
    logger.info("reminder_v2_tick", **asdict(report))
    return report


async def process_appointment_reminders(ctx: dict) -> None:
    """arq cron (every minute, workers/arq_worker.py): send the due reminders."""
    await run_reminder_tick(now=datetime.now(UTC), redis=ctx.get("redis"))
```

- [ ] **Step 4: Run them to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_engine.py -q`
Expected: `16 passed` (the terminal-status test is parametrized ×3).

- [ ] **Step 5: Lint and commit**

Run: `uvx ruff format src/secretaria/workers/reminder_engine.py tests/test_reminder_v2_engine.py && uvx ruff check --fix src/secretaria/workers/reminder_engine.py tests/test_reminder_v2_engine.py`

```bash
git add src/secretaria/workers/reminder_engine.py tests/test_reminder_v2_engine.py
git commit -m "feat(reminders): reminder engine claims due rows atomically and guards them (TASK-032 R2)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 8: The engine — retries, delivery failure, warnings, history, usage, stop after two

**Files:**
- Modify: `src/secretaria/workers/reminder_engine.py` (replace `_finish`; add `_mark_sent`, `_record_history`, `_emit_usage`; new imports)
- Test: `tests/test_reminder_v2_engine.py` (append)

**Interfaces:**
- Consumes: Task 7; `services/usage_events.py::emit_usage_event`; R1 `REMINDER_WARN_UNCONFIRMED`; `models.Message`, `MessageDirection`, `MessageSender`.
- Produces: final `_finish` semantics (see Global Constraints and "Produces for R3/R4/R5"); usage event id `reminder:v2:<reminder_id>`, feature `reminders`; WhatsApp outbound `Message` rows for every sent reminder.

- [ ] **Step 1: Write the failing tests**

In `tests/test_reminder_v2_engine.py` replace the import line `from sqlalchemy import select  # noqa: F401  (used by the Task 8 tests)` with `from sqlalchemy import select`, add to the top imports:

```python
from secretaria.models import Conversation
from secretaria.services.brain_patients import PatientEmailResult
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.email import EmailOutcome
from tests._reminders_v2 import outbound_messages, seed_paid_deposit
```

and append:

```python
# --------------------------------------------------------------------------
# Outcomes (Task 8)
# --------------------------------------------------------------------------


@pytest.fixture
def usage(monkeypatch):
    calls: list[dict] = []

    async def _emit(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(reminder_engine, "emit_usage_event", _emit)
    return calls


async def test_a_failing_send_is_retried_then_marked_delivery_failed(db, usage):  # noqa: F811
    FakeWhatsAppClient.fail_everything = True
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world)

    for attempt in (1, 2, 3):
        report = await _tick(NOW + timedelta(minutes=attempt))
        row = await get_reminder(db, rid)
        assert report.retried == 1
        assert (row.status, row.attempts, row.last_error_code) == ("pending", attempt, "RuntimeError")
        assert row.warn_kind is None

    final = NOW + timedelta(minutes=4)
    report = await _tick(final)

    row = await get_reminder(db, rid)
    assert report.failed == 1
    assert (row.status, row.attempts, row.warn_kind) == ("failed", 4, "delivery_failed")
    assert _utc(row.warn_due_at) == final
    assert usage == []
    assert await outbound_messages(db, world.conversation.id) == []


async def test_a_send_that_recovers_on_the_second_attempt_is_sent(db):  # noqa: F811
    FakeWhatsAppClient.fail_everything = True
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world)
    await _tick()
    FakeWhatsAppClient.fail_everything = False

    await _tick(NOW + timedelta(minutes=1))

    row = await get_reminder(db, rid)
    assert (row.status, row.attempts, row.last_error_code) == ("sent", 2, None)


async def test_a_patient_without_any_channel_fails_at_once_and_warns(db):  # noqa: F811
    world = await seed_world(db, wa_id=None)
    rid = await add_reminder(db, world)

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.attempts, row.last_error_code, row.warn_kind) == (
        "failed",
        1,
        "no_channel",
        "delivery_failed",
    )


async def test_a_sent_prompt_keeps_the_clinic_warning_armed(db):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world)
    planned_warning = _utc((await get_reminder(db, rid)).warn_due_at)

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.with_prompt, row.warn_kind) == ("sent", True, "unconfirmed")
    assert _utc(row.warn_due_at) == planned_warning


async def test_after_two_confirmations_the_reminder_has_no_buttons_and_warns_nobody(db):  # noqa: F811
    world = await seed_world(db, confirmation_count=2, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world, with_prompt=True)  # planned before the 2nd confirmation

    await _tick()

    [(kind, _to, body)] = FakeWhatsAppClient.all_sent()
    assert kind == "text" and body.startswith("LEMBRE-SE:")
    row = await get_reminder(db, rid)
    assert (row.status, row.with_prompt, row.warn_kind, row.warn_due_at) == (
        "sent",
        False,
        None,
        None,
    )


async def test_a_whatsapp_reminder_is_recorded_in_the_conversation(db):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    await add_reminder(db, world)

    await _tick()

    [row] = await outbound_messages(db, world.conversation.id)
    assert row.wam_id == "wamid.buttons"
    assert row.body.endswith("(opções: Confirmar, Cancelar, Outro)")
    assert row.interactive["kind"] == "buttons"
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
    assert conversation.last_bot_message_at is not None


async def test_only_a_template_send_is_metered(db, usage):  # noqa: F811
    outside = await seed_world(db)  # never wrote: template
    inside = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid_outside = await add_reminder(db, outside)
    await add_reminder(db, inside)

    await _tick()

    assert usage == [
        {
            "tenant_id": str(outside.tenant.id),
            "feature": "reminders",
            "amount": 1,
            "event_id": f"reminder:v2:{rid_outside}",
        }
    ]


async def test_a_metering_failure_never_unsends_the_reminder(db, monkeypatch):  # noqa: F811
    async def _boom(**kwargs):
        raise RuntimeError("brain-api unreachable")

    monkeypatch.setattr(reminder_engine, "emit_usage_event", _boom)
    world = await seed_world(db)
    rid = await add_reminder(db, world)

    await _tick()

    assert (await get_reminder(db, rid)).status == "sent"


async def test_a_row_cancelled_while_in_flight_stays_cancelled(db, monkeypatch):  # noqa: F811
    """R1's cancel_reminders also cancels 'sending' rows; finishing must not resurrect it."""
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world)
    real_deliver = reminder_engine.deliver_reminder

    async def _deliver_while_cancelled(job):
        await _set(db, rid, status="cancelled", warn_due_at=None)
        return await real_deliver(job)

    monkeypatch.setattr(reminder_engine, "deliver_reminder", _deliver_while_cancelled)

    report = await _tick()

    row = await get_reminder(db, rid)
    assert report.closed == 1
    assert (row.status, row.sent_at, row.warn_kind) == ("cancelled", None, None)
    assert len(await outbound_messages(db, world.conversation.id)) == 1  # it did reach the patient


async def test_a_portal_patient_is_reminded_by_chat_and_email(db, monkeypatch):  # noqa: F811
    async def _fetch(tenant_id, external_id):
        return PatientEmailResult(available=True, email="maria@example.com")

    async def _send(to, template, variables):
        return EmailOutcome.SENT

    monkeypatch.setattr(reminder_delivery, "fetch_patient_email_result", _fetch)
    monkeypatch.setattr(reminder_delivery, "send_transactional_email_result", _send)
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE)
    rid = await add_reminder(db, world)

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.channel) == ("sent", "email")
    assert FakeWhatsAppClient.created == []
    assert len(await outbound_messages(db, world.conversation.id)) == 1  # written by the sender


async def test_a_pix_paid_appointment_keeps_its_three_button_variant(db):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    await seed_paid_deposit(db, world)
    rid = await add_reminder(db, world)

    await _tick()

    [(_kind, _to, _body, buttons)] = FakeWhatsAppClient.all_sent()
    assert [label for _, label in buttons] == ["Confirmar", "Reagendar", "Cancelar"]
    assert buttons[0][0] == f"remconfirm|{rid}"
    assert buttons[1][0] == f"apptresched|{world.appointment.id}"


async def test_rows_are_never_read_across_clinics(db):  # noqa: F811
    off = await seed_world(db, v2=False, last_inbound_at=NOW - timedelta(hours=1))
    on = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid_off = await add_reminder(db, off)
    await add_reminder(db, on)

    await _tick()

    async with db() as session:
        statuses = dict((await session.execute(select(AppointmentReminder.id, AppointmentReminder.status))).all())
    assert statuses[rid_off] == "pending"
    assert sorted(statuses.values()) == ["pending", "sent"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_engine.py -q`
Expected: several FAILs — e.g. `AttributeError: <module 'secretaria.workers.reminder_engine'> has no attribute 'emit_usage_event'`, `assert 'pending' == 'failed'` (no retry cap yet), `assert None == 'unconfirmed'`, `assert [] == [<Message>]` (no history row), `assert 'sent' == 'cancelled'`.

- [ ] **Step 3: Implement the full bookkeeping**

In `src/secretaria/workers/reminder_engine.py`:

(a) extend the imports — the `secretaria.models` import gains `Message`, `MessageDirection`, `MessageSender`; the `appointment_reminder` import gains `REMINDER_WARN_UNCONFIRMED`; and add `from secretaria.services.usage_events import emit_usage_event`.

(b) replace the whole `_finish` function with:

```python
def _mark_sent(reminder: AppointmentReminder, job: ReminderJob, outcome: DeliveryOutcome, now: datetime) -> None:
    reminder.status = REMINDER_STATUS_SENT
    reminder.sent_at = now
    reminder.channel = outcome.channel
    reminder.with_prompt = job.with_prompt
    # Informational on a success: "plain_template" = the buttons could not go
    # outside the window (template not approved / refused).
    reminder.last_error_code = outcome.error_code
    if job.with_prompt:
        # R1 already set warn_due_at (due + delay); R4 warns the clinic then if
        # the counter is still 0.
        reminder.warn_kind = REMINDER_WARN_UNCONFIRMED
    else:
        # No question was asked, so there is nothing to be unanswered (spec §4.2 "Parada").
        reminder.warn_kind = None
        reminder.warn_due_at = None


async def _record_history(session, job: ReminderJob, outcome: DeliveryOutcome, now: datetime) -> None:
    """WhatsApp's history copy (the Portal sender wrote its own): console + LLM history."""
    session.add(
        Message(
            conversation_id=job.conversation_id,
            direction=MessageDirection.OUTBOUND,
            sender=MessageSender.BOT,
            wam_id=outcome.wam_id,
            body=outcome.history_body,
            interactive=outcome.history_interactive,
        )
    )
    conversation = await session.get(Conversation, job.conversation_id)
    if conversation is not None:
        conversation.last_bot_message_at = now


async def _emit_usage(job: ReminderJob) -> None:
    """One billed template = one meter tick. Fail-open: the message already went out."""
    event_id = f"reminder:v2:{job.reminder_id}"
    try:
        recorded = await emit_usage_event(
            tenant_id=str(job.tenant.id), feature="reminders", amount=1, event_id=event_id
        )
        if not recorded:
            logger.warning("usage_emit_failed", event_id=event_id, tenant_id=str(job.tenant.id))
    except Exception as exc:
        logger.warning("usage_emit_failed", event_id=event_id, error_type=type(exc).__name__)


async def _finish(
    reminder_id: UUID, job: ReminderJob, outcome: DeliveryOutcome, now: datetime
) -> str:
    """Book the outcome. Returns the TickReport field to count it under.

    * delivered -> `sent` (+ the WhatsApp history row; + usage when billed);
    * failed, permanent or 4th attempt -> `failed` + delivery_failed warning;
    * failed otherwise -> back to `pending`; the next tick (one minute) retries;
    * the row stopped being `sending` meanwhile (R1's cancel_reminders retired
      it) -> left as it is; a message that did go out is still recorded.
    """
    async with core_database.async_session_factory() as session:
        async with session.begin():
            reminder = await session.get(AppointmentReminder, reminder_id)
            if outcome.ok and job.conversation_id is not None and outcome.history_body is not None:
                await _record_history(session, job, outcome, now)
            if reminder is None or reminder.status != REMINDER_STATUS_SENDING:
                logger.info("reminder_v2_retired_in_flight", reminder_id=str(reminder_id))
                result = "closed"
            elif outcome.ok:
                _mark_sent(reminder, job, outcome, now)
                result = "sent"
            elif outcome.permanent or job.attempt >= MAX_ATTEMPTS:
                reminder.channel = outcome.channel
                _fail(reminder, outcome.error_code or "send_failed", now)
                result = "failed"
            else:
                reminder.status = REMINDER_STATUS_PENDING
                reminder.last_error_code = outcome.error_code
                result = "retried"
    if outcome.ok and outcome.billable:
        await _emit_usage(job)
    return result
```

- [ ] **Step 4: Run them to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_engine.py tests/test_reminder_v2_delivery.py -q`
Expected: all pass (`28` engine + `15` delivery).

- [ ] **Step 5: Lint and commit**

Run: `uvx ruff format src/secretaria/workers/reminder_engine.py tests/test_reminder_v2_engine.py && uvx ruff check --fix src/secretaria/workers/reminder_engine.py tests/test_reminder_v2_engine.py`

```bash
git add src/secretaria/workers/reminder_engine.py tests/test_reminder_v2_engine.py
git commit -m "feat(reminders): retries, delivery-failure marking, history and usage for reminders (TASK-032 R2)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 9: Lifecycle hooks and reconcile (`services/reminder_hooks.py`)

**Why after the commit, in their own transaction:** a reminder problem must never cost a booking. Every booking path commits the appointment (and, for the agent and the flow, after the Google event already exists); a reminder hook that shared that transaction and failed would roll the booking back and orphan the event. So each path calls the hook after its own commit; a crash in between is healed by `reconcile_missing_reminders` (cron every 10 minutes, Task 12), which is also the backfill for clinics switched ON with appointments already on the books. Call sites guard with `enabled_for(tenant)` so a clinic with the switch OFF does no extra I/O at all.

**Files:**
- Create: `src/secretaria/services/reminder_hooks.py`
- Test: `tests/test_reminder_v2_hooks.py`

**Interfaces:**
- Consumes: R1 `reminder_schedule.schedule_reminders`, `cancel_reminders`, `reschedule_reminders`; `models.LIVE_APPOINTMENT_STATUSES`.
- Produces:
  - `enabled_for(tenant) -> bool`
  - `async after_appointment_booked(appointment_id: UUID, *, now: datetime | None = None) -> int` (rows created)
  - `async after_appointment_rescheduled(appointment_id: UUID, *, now: datetime | None = None) -> int` (rows created; zeroes the counter even with the switch OFF — R1 semantics)
  - `async after_appointment_closed(appointment_id: UUID, *, reason: str) -> int` (rows cancelled)
  - `async reconcile_missing_reminders(*, now: datetime, limit: int = RECONCILE_LIMIT) -> int` (rows created), `RECONCILE_LIMIT = 500`
  - All four never raise.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reminder_v2_hooks.py`:

```python
"""services/reminder_hooks.py — the schedule follows the appointment (TASK-032 R2)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from secretaria.core import database as core_database
from secretaria.models import Appointment, AppointmentReminder, AppointmentStatus
from secretaria.services import reminder_hooks, reminder_schedule
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import NOW, reload_appointment, seed_world


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    monkeypatch.setattr(core_database, "async_session_factory", db)
    yield


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


async def _rows(db, appointment_id) -> list[AppointmentReminder]:  # noqa: F811
    async with db() as session:
        return list(
            await session.scalars(
                select(AppointmentReminder).where(
                    AppointmentReminder.appointment_id == appointment_id
                )
            )
        )


def test_enabled_for_reads_only_a_true_switch():
    assert reminder_hooks.enabled_for(SimpleNamespace(reminders_v2_enabled=True)) is True
    assert reminder_hooks.enabled_for(SimpleNamespace(reminders_v2_enabled=False)) is False
    assert reminder_hooks.enabled_for(SimpleNamespace()) is False
    assert reminder_hooks.enabled_for(None) is False


async def test_a_booking_plans_the_day_and_hour_reminders(db):  # noqa: F811
    world = await seed_world(db)

    created = await reminder_hooks.after_appointment_booked(world.appointment.id, now=NOW)

    rows = await _rows(db, world.appointment.id)
    assert created == 2
    assert sorted(r.kind for r in rows) == ["day", "hour"]
    assert {r.status for r in rows} == {"pending"}


async def test_a_booking_in_a_clinic_with_the_switch_off_plans_nothing(db):  # noqa: F811
    world = await seed_world(db, v2=False)

    assert await reminder_hooks.after_appointment_booked(world.appointment.id, now=NOW) == 0
    assert await _rows(db, world.appointment.id) == []


async def test_booking_twice_is_harmless(db):  # noqa: F811
    world = await seed_world(db)
    await reminder_hooks.after_appointment_booked(world.appointment.id, now=NOW)

    assert await reminder_hooks.after_appointment_booked(world.appointment.id, now=NOW) == 0
    assert len(await _rows(db, world.appointment.id)) == 2


async def test_a_reschedule_retires_old_rows_and_plans_new_ones(db):  # noqa: F811
    world = await seed_world(db, confirmation_count=1)
    await reminder_hooks.after_appointment_booked(world.appointment.id, now=NOW)
    new_start = world.start_at + timedelta(days=2)
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.start_at = new_start
        appointment.end_at = new_start + timedelta(minutes=30)
        appointment.status = AppointmentStatus.RESCHEDULED
        await session.commit()

    created = await reminder_hooks.after_appointment_rescheduled(world.appointment.id, now=NOW)

    rows = await _rows(db, world.appointment.id)
    old = [r for r in rows if _utc(r.appointment_start_at) == world.start_at]
    new = [r for r in rows if _utc(r.appointment_start_at) == new_start]
    assert created == 2
    assert {r.status for r in old} == {"cancelled"}
    assert {r.status for r in new} == {"pending"}
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 0


async def test_a_reschedule_with_the_switch_off_still_zeroes_the_confirmation(db):  # noqa: F811
    world = await seed_world(db, v2=False, confirmation_count=1)

    assert await reminder_hooks.after_appointment_rescheduled(world.appointment.id, now=NOW) == 0
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 0


async def test_closing_an_appointment_cancels_its_pending_rows(db):  # noqa: F811
    world = await seed_world(db)
    await reminder_hooks.after_appointment_booked(world.appointment.id, now=NOW)

    cancelled = await reminder_hooks.after_appointment_closed(
        world.appointment.id, reason="cancelled"
    )

    assert cancelled == 2
    assert {r.status for r in await _rows(db, world.appointment.id)} == {"cancelled"}


async def test_a_hook_never_raises(db, monkeypatch):  # noqa: F811
    async def _boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(reminder_schedule, "schedule_reminders", _boom)
    monkeypatch.setattr(reminder_schedule, "cancel_reminders", _boom)
    monkeypatch.setattr(reminder_schedule, "reschedule_reminders", _boom)
    world = await seed_world(db)

    assert await reminder_hooks.after_appointment_booked(world.appointment.id, now=NOW) == 0
    assert await reminder_hooks.after_appointment_rescheduled(world.appointment.id, now=NOW) == 0
    assert await reminder_hooks.after_appointment_closed(world.appointment.id, reason="x") == 0


async def test_reconcile_plans_only_what_is_missing_and_only_where_switched_on(db):  # noqa: F811
    missing = await seed_world(db)
    planned = await seed_world(db)
    await reminder_hooks.after_appointment_booked(planned.appointment.id, now=NOW)
    switched_off = await seed_world(db, v2=False)
    already_past = await seed_world(db, start_at=NOW - timedelta(hours=1))
    cancelled = await seed_world(db, status=AppointmentStatus.CANCELLED)

    created = await reminder_hooks.reconcile_missing_reminders(now=NOW)

    assert created == 2
    assert len(await _rows(db, missing.appointment.id)) == 2
    assert len(await _rows(db, planned.appointment.id)) == 2
    for world in (switched_off, already_past, cancelled):
        assert await _rows(db, world.appointment.id) == []
    assert await reminder_hooks.reconcile_missing_reminders(now=NOW) == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_hooks.py -q`
Expected: collection error `ImportError: cannot import name 'reminder_hooks'`.

- [ ] **Step 3: Implement**

Create `src/secretaria/services/reminder_hooks.py`:

```python
"""Keep an appointment's reminder rows in step with the appointment (TASK-032 R2).

Every path that books, moves or closes an appointment calls one of the three
`after_*` hooks AFTER its own commit, and each hook runs in its own short
transaction. Deliberately not inside the caller's transaction: the agent and
the flow commit the appointment after the Google event already exists, so a
reminder failure that rolled the booking back would orphan the event and
lose the booking - a reminder is never worth that. The hooks never raise.

Call sites check `enabled_for(tenant)` first so a clinic with the switch OFF
does no extra I/O. The one exception is a reschedule of an appointment that
had confirmations: R1's `reschedule_reminders` zeroes the counter even with
the switch OFF (a moved booking is unconfirmed again), so those sites also
call the hook when `confirmation_count > 0`.

A crash between the caller's commit and the hook - or a clinic switched ON
with appointments already booked - leaves live future appointments without
rows of their current version. `reconcile_missing_reminders` (cron, every 10
minutes, workers/reminder_engine.py) plans exactly those: it is the backfill.
R1's `schedule_reminders` is idempotent and skips already-due kinds, so a
repeated or late call can never duplicate or send a stale reminder.

Uses `core.database.async_session_factory` through the module, looked up at
call time, so tests and the worker share one binding.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from secretaria.core import database as core_database
from secretaria.core.logging import get_logger
from secretaria.models import (
    LIVE_APPOINTMENT_STATUSES,
    Appointment,
    AppointmentReminder,
    Tenant,
)
from secretaria.services import reminder_schedule

logger = get_logger(__name__)

RECONCILE_LIMIT = 500


def enabled_for(tenant) -> bool:
    """The clinic's switch, read defensively (snapshots, None, older rows)."""
    return getattr(tenant, "reminders_v2_enabled", False) is True


async def _load(session, appointment_id: UUID) -> tuple[Appointment | None, Tenant | None]:
    appointment = await session.get(Appointment, appointment_id)
    if appointment is None:
        return None, None
    return appointment, await session.get(Tenant, appointment.tenant_id)


async def after_appointment_booked(appointment_id: UUID, *, now: datetime | None = None) -> int:
    """Plan the reminders of a new appointment. Returns how many rows were created."""
    try:
        async with core_database.async_session_factory() as session:
            async with session.begin():
                appointment, tenant = await _load(session, appointment_id)
                if appointment is None or tenant is None:
                    return 0
                rows = await reminder_schedule.schedule_reminders(
                    session, appointment, tenant, now=now or datetime.now(UTC)
                )
                return len(rows)
    except Exception as exc:
        logger.warning(
            "reminder_hook_failed",
            hook="booked",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )
        return 0


async def after_appointment_rescheduled(
    appointment_id: UUID, *, now: datetime | None = None
) -> int:
    """The appointment moved: retire the old rows, zero the counter, plan anew."""
    try:
        async with core_database.async_session_factory() as session:
            async with session.begin():
                appointment, tenant = await _load(session, appointment_id)
                if appointment is None or tenant is None:
                    return 0
                rows = await reminder_schedule.reschedule_reminders(
                    session, appointment, tenant, now=now or datetime.now(UTC)
                )
                return len(rows or [])
    except Exception as exc:
        logger.warning(
            "reminder_hook_failed",
            hook="rescheduled",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )
        return 0


async def after_appointment_closed(appointment_id: UUID, *, reason: str) -> int:
    """Cancelled / attended / no-show / released: cancel the rows still pending."""
    try:
        async with core_database.async_session_factory() as session:
            async with session.begin():
                return await reminder_schedule.cancel_reminders(
                    session, appointment_id, reason=reason
                )
    except Exception as exc:
        logger.warning(
            "reminder_hook_failed",
            hook="closed",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )
        return 0


async def reconcile_missing_reminders(*, now: datetime, limit: int = RECONCILE_LIMIT) -> int:
    """Plan every live future appointment of a switched-ON clinic that has no
    row for its CURRENT start. Returns how many rows were created. Never raises."""
    try:
        current_version = (
            select(AppointmentReminder.id)
            .where(
                AppointmentReminder.appointment_id == Appointment.id,
                AppointmentReminder.appointment_start_at == Appointment.start_at,
            )
            .exists()
        )
        async with core_database.async_session_factory() as session:
            candidates = list(
                await session.scalars(
                    select(Appointment.id)
                    .join(Tenant, Tenant.id == Appointment.tenant_id)
                    .where(
                        Tenant.reminders_v2_enabled.is_(True),
                        Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
                        Appointment.patient_id.is_not(None),
                        Appointment.start_at > now,
                        ~current_version,
                    )
                    .order_by(Appointment.start_at)
                    .limit(limit)
                )
            )
    except Exception as exc:
        logger.warning("reminder_reconcile_failed", error_type=type(exc).__name__)
        return 0
    created = 0
    for appointment_id in candidates:
        created += await after_appointment_booked(appointment_id, now=now)
    logger.info("reminders_reconciled", candidates=len(candidates), created=created)
    return created
```

- [ ] **Step 4: Run them to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_hooks.py -q`
Expected: `9 passed`.

- [ ] **Step 5: Lint and commit**

Run: `uvx ruff format src/secretaria/services/reminder_hooks.py tests/test_reminder_v2_hooks.py && uvx ruff check --fix src/secretaria/services/reminder_hooks.py tests/test_reminder_v2_hooks.py`

```bash
git add src/secretaria/services/reminder_hooks.py tests/test_reminder_v2_hooks.py
git commit -m "feat(reminders): after-commit lifecycle hooks and reconcile backfill (TASK-032 R2)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 10: Wire the hooks into every booking path

The four places an appointment is born (spec §4.2 "Criação"): the deterministic flow (`_apply_flow_result`), the Portal reservation promotion (`_promote_booking_hold`), the agent tool (`_persist_appointment`), the hub (`POST /appointments`). Block slots (`POST /blocks`) and phone-only hub bookings have no patient and are never planned.

**Files:**
- Modify: `src/secretaria/workers/shared/flow_runner.py` (import; the post-persist `if booked_appointment is not None and persisted and tenant is not None:` block)
- Modify: `src/secretaria/workers/shared/booking_hold.py` (import; after `await enqueue_post_booking_hooks(redis, tenant.id, appointment.id, source="flow")`)
- Modify: `src/secretaria/ai/tools.py` (`_persist_appointment`)
- Modify: `src/secretaria/api/hub/calendar.py` (import; `create_appointment`)
- Test: `tests/test_reminder_v2_wiring.py` (create), `tests/test_reminder_v2_hub_wiring.py` (create), `tests/test_booking_code_gate.py` (append)

**Interfaces:**
- Consumes: Task 9 (`reminder_hooks.enabled_for`, `after_appointment_booked`), `tests/_reminders_v2.py::HookSpy`.
- Produces: nothing new; behavior only.

- [ ] **Step 1: Write the failing tests (flow + agent)**

Create `tests/test_reminder_v2_wiring.py`:

```python
"""Every booking / move / cancel path calls the reminder hooks (TASK-032 R2, spec §4.2)."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from secretaria.ai import tools
from secretaria.core import database as core_database
from secretaria.models import Appointment, Tenant
from secretaria.services import reminder_hooks
from secretaria.services.flow_router import FlowRouterResult
from secretaria.workers import tasks
from tests._patching import workers_ns
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import (
    NOW,
    WA_ID,
    FakeWhatsAppClient,
    HookSpy,
    fake_waba_token,
    seed_world,
)


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    FakeWhatsAppClient.reset()
    monkeypatch.setattr(workers_ns, "WhatsAppClient", FakeWhatsAppClient)
    monkeypatch.setattr(workers_ns, "get_waba_token", fake_waba_token)
    yield


@pytest.fixture
def spy(monkeypatch) -> HookSpy:
    return HookSpy().install(monkeypatch, reminder_hooks)


def _reply(world) -> tasks._ReplyContext:
    return tasks._ReplyContext(
        conversation_id=world.conversation.id,
        tenant_id=world.tenant.id,
        patient_ref=WA_ID,
        inbound_body="",
    )


async def _tenant(db, world) -> Tenant:  # noqa: F811
    async with db() as session:
        return await session.get(Tenant, world.tenant.id)


async def _appointment_id(db, google_event_id: str):  # noqa: F811
    async with db() as session:
        return await session.scalar(
            select(Appointment.id).where(Appointment.google_event_id == google_event_id)
        )


async def _flow(db, world, result: FlowRouterResult) -> None:  # noqa: F811
    await tasks._apply_flow_result(
        _reply(world), result, WA_ID, tenant=await _tenant(db, world), waba_token="tok"
    )


def _booking(event_id: str) -> FlowRouterResult:
    start = NOW + timedelta(days=5)
    return FlowRouterResult(
        action="reply",
        bubbles=[],
        appointment={
            "google_event_id": event_id,
            "appointment_type": "Consulta",
            "start_at": start,
            "end_at": start + timedelta(minutes=30),
        },
    )


# ---- booking paths (Task 10) ------------------------------------------------


@pytest.mark.parametrize("v2", [True, False])
async def test_a_flow_booking_plans_reminders_only_with_the_switch_on(db, spy, v2):  # noqa: F811
    world = await seed_world(db, v2=v2)

    await _flow(db, world, _booking("evt-flow"))

    expected = [("booked", await _appointment_id(db, "evt-flow"))] if v2 else []
    assert spy.calls == expected


@pytest.mark.parametrize("v2", [True, False])
async def test_an_agent_booking_plans_reminders_only_with_the_switch_on(db, spy, v2):  # noqa: F811
    world = await seed_world(db, v2=v2)
    tenant_token = tools._tenant_id_ctx.set(world.tenant.id)
    conversation_token = tools._conversation_id_ctx.set(world.conversation.id)
    try:
        start = NOW + timedelta(days=4)
        await tools._persist_appointment(
            {"id": "evt-agent"}, start, start + timedelta(minutes=30), "Consulta"
        )
    finally:
        tools._conversation_id_ctx.reset(conversation_token)
        tools._tenant_id_ctx.reset(tenant_token)

    expected = [("booked", await _appointment_id(db, "evt-agent"))] if v2 else []
    assert spy.calls == expected
```

- [ ] **Step 2: Write the failing tests (Portal promotion)**

In `tests/test_booking_code_gate.py`, add to its top imports `from secretaria.services import reminder_hooks` and `from tests._reminders_v2 import HookSpy`, then append after `test_the_verified_code_is_what_creates_the_appointment`:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("v2", [True, False])
async def test_a_promoted_reservation_plans_its_reminders_only_with_the_switch_on(
    db, calls, monkeypatch, v2
):
    """TASK-032 R2: the Portal booking is born here, so its reminders are planned here."""
    spy = HookSpy().install(monkeypatch, reminder_hooks)
    tenant = await _seed_tenant(db)
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.reminders_v2_enabled = v2
        await session.commit()
    tenant.reminders_v2_enabled = v2
    patient, conversation = await _seed_conversation(
        db, tenant, flow_state=FlowState.AWAITING_EMAIL_CODE
    )
    await _place(db, tenant, conversation, datetime(2099, 1, 5, 13, 0, tzinfo=UTC), patient=patient)
    monkeypatch.setattr(
        workers_ns, "_appointment_calendar", lambda *a, **k: _async(_FakeCalendar(calls))
    )

    await tasks._promote_booking_hold(
        _reply(conversation), tenant=tenant, waba_token=None, professionals=[], redis=None
    )

    rows = await _appointments(db, tenant)
    assert spy.calls == ([("booked", rows[0].id)] if v2 else [])
```

(`Tenant`, `FlowState`, `datetime`, `UTC`, `workers_ns`, `_seed_tenant`, `_seed_conversation`, `_place`, `_async`, `_FakeCalendar`, `_appointments`, `_reply` are already defined/imported in that file; if `Tenant` is not imported there, add it to its `from secretaria.models import (...)` block.)

- [ ] **Step 3: Write the failing tests (hub)**

Create `tests/test_reminder_v2_hub_wiring.py`:

```python
"""Hub calendar endpoints call the reminder hooks (TASK-032 R2). Harness copied from
tests/test_hub_calendar_money.py: in-memory DB, fake Calendar, fake arq pool."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import Depends
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub import calendar as hub_calendar
from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import Appointment, AppointmentStatus, Patient, Tenant
from secretaria.services import reminder_hooks
from secretaria.services.tenant_config import set_google_refresh_token
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import HookSpy

CALENDAR = "/tenants/me/calendar"


class _FakeCalendarService:
    @classmethod
    def from_tenant_config(cls, config):
        return cls()

    async def cancel_event(self, event_id: str) -> None:
        return None

    async def update_event(self, event_id: str, start: datetime, end: datetime) -> dict:
        return {"id": event_id}

    async def create_event(self, start, end, summary, description="") -> dict:
        return {"id": f"evt-{uuid4()}", "htmlLink": "https://calendar.example/evt"}


class _FakeArqPool:
    async def enqueue_job(self, name: str, *args) -> None:
        return None


@pytest_asyncio.fixture
async def tenant(db) -> Tenant:  # noqa: F811
    async with db() as session:
        row = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=None, reminders_v2_enabled=True)
        session.add(row)
        await session.commit()
        await session.refresh(row)
    async with db() as session:
        await set_google_refresh_token(session, row.id, "fake-refresh-token")
        await session.commit()
    return row


@pytest.fixture(autouse=True)
def _override(db, tenant, monkeypatch):  # noqa: F811
    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant.id)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    monkeypatch.setattr(hub_calendar, "CalendarService", _FakeCalendarService)
    app.state.arq_pool = _FakeArqPool()
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)
    app.state.arq_pool = None


@pytest.fixture
def spy(monkeypatch) -> HookSpy:
    return HookSpy().install(monkeypatch, reminder_hooks)


async def _switch(db, tenant, on: bool) -> None:  # noqa: F811
    async with db() as session:
        (await session.get(Tenant, tenant.id)).reminders_v2_enabled = on
        await session.commit()


async def _patient(db, tenant) -> Patient:  # noqa: F811
    async with db() as session:
        row = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511977776666", name="Maria")
        session.add(row)
        await session.commit()
        return row


async def _appointment(db, tenant, *, confirmation_count: int = 0) -> Appointment:  # noqa: F811
    patient = await _patient(db, tenant)
    start = datetime.now(UTC) + timedelta(days=3)
    async with db() as session:
        row = Appointment(
            tenant_id=tenant.id,
            patient_id=patient.id,
            google_event_id=f"evt-{uuid4()}",
            appointment_type="Consulta",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
            phone="5511977776666",
            confirmation_count=confirmation_count,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


def _window(days: int = 3) -> dict:
    start = datetime.now(UTC) + timedelta(days=days)
    return {"start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat()}


# ---- create (Task 10) ---------------------------------------------------------


async def test_hub_booking_for_a_patient_plans_reminders(client: AsyncClient, db, tenant, spy):  # noqa: F811
    patient = await _patient(db, tenant)

    response = await client.post(
        f"{CALENDAR}/appointments",
        json={**_window(), "summary": "Consulta", "patient_id": str(patient.id)},
    )

    assert response.status_code == 201
    assert spy.calls == [("booked", UUID(response.json()["id"]))]


async def test_hub_booking_with_the_switch_off_plans_nothing(client: AsyncClient, db, tenant, spy):  # noqa: F811
    await _switch(db, tenant, False)
    patient = await _patient(db, tenant)

    response = await client.post(
        f"{CALENDAR}/appointments",
        json={**_window(), "summary": "Consulta", "patient_id": str(patient.id)},
    )

    assert response.status_code == 201
    assert spy.calls == []


async def test_hub_booking_without_a_patient_and_blocks_plan_nothing(client: AsyncClient, db, tenant, spy):  # noqa: F811
    phone_only = await client.post(
        f"{CALENDAR}/appointments",
        json={**_window(), "summary": "Consulta", "phone": "5511900001111"},
    )
    block = await client.post(f"{CALENDAR}/blocks", json={**_window(4), "summary": "Almoço"})

    assert (phone_only.status_code, block.status_code) == (201, 201)
    assert spy.calls == []
```

- [ ] **Step 4: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_wiring.py tests/test_reminder_v2_hub_wiring.py tests/test_booking_code_gate.py -q`
Expected: the `v2=True` cases FAIL with `assert [] == [('booked', UUID(...))]`; the `v2=False` cases and the block test pass.

- [ ] **Step 5: Wire the flow**

In `src/secretaria/workers/shared/flow_runner.py` add the import line `from secretaria.services import reminder_hooks` immediately above `from secretaria.services.appointment_status import (`. Then replace:

```python
    if booked_appointment is not None and persisted and tenant is not None:
        _log_booking_scope(booked_appointment, tenant.id, source=SOURCE_FLOW)
        await enqueue_post_booking_hooks(redis, tenant.id, booked_appointment.id, source="flow")
```

with:

```python
    if booked_appointment is not None and persisted and tenant is not None:
        _log_booking_scope(booked_appointment, tenant.id, source=SOURCE_FLOW)
        await enqueue_post_booking_hooks(redis, tenant.id, booked_appointment.id, source="flow")
        # TASK-032 R2: plan the reminders, in their own transaction AFTER the
        # booking committed - a reminder problem never costs a booking.
        if reminder_hooks.enabled_for(tenant):
            await reminder_hooks.after_appointment_booked(booked_appointment.id)
```

- [ ] **Step 6: Wire the Portal promotion**

In `src/secretaria/workers/shared/booking_hold.py` add `from secretaria.services import reminder_hooks` immediately above `from secretaria.services.appointment_status import (`. Then replace:

```python
    await enqueue_post_booking_hooks(redis, tenant.id, appointment.id, source="flow")

    tz = _tenant_tzinfo(tenant)
```

with:

```python
    await enqueue_post_booking_hooks(redis, tenant.id, appointment.id, source="flow")
    # TASK-032 R2: the Portal booking is born here, so its reminders are planned here.
    if reminder_hooks.enabled_for(tenant):
        await reminder_hooks.after_appointment_booked(appointment.id)

    tz = _tenant_tzinfo(tenant)
```

- [ ] **Step 7: Wire the agent tool**

In `src/secretaria/ai/tools.py`, inside `_persist_appointment`:

(a) replace the lazy import line `    from secretaria.models import Appointment, AppointmentStatus, Conversation, Patient` with:

```python
    from secretaria.models import Appointment, AppointmentStatus, Conversation, Patient, Tenant
    from secretaria.services import reminder_hooks
```

(b) replace `    appointment: Any | None = None` (the line right before `    try:`) with:

```python
    appointment: Any | None = None
    plan_reminders = False
```

(c) right after `                session.add(appointment)` (the one inside `_persist_appointment`, followed by the comment `# The attendee belonged to THIS booking`) insert:

```python
                # TASK-032 R2: read the clinic's reminder switch in this same
                # transaction; the hook itself runs after the commit, below.
                plan_reminders = reminder_hooks.enabled_for(await session.get(Tenant, tenant_id))
```

(d) replace the last statement of the function

```python
        await enqueue_post_booking_hooks(
            _redis_ctx.get(), tenant_id, appointment.id, source="agent"
        )
```

with

```python
        await enqueue_post_booking_hooks(
            _redis_ctx.get(), tenant_id, appointment.id, source="agent"
        )
        if plan_reminders:
            await reminder_hooks.after_appointment_booked(appointment.id)
```

- [ ] **Step 8: Wire the hub create**

In `src/secretaria/api/hub/calendar.py`, extend the line `from secretaria.services import cancellation_notice` (R1 may already have added `reminder_schedule` to it) so that it also imports `reminder_hooks`, e.g. `from secretaria.services import cancellation_notice, reminder_hooks, reminder_schedule`. Then in `create_appointment` replace:

```python
    session.add(appt)
    await session.commit()
    await session.refresh(appt)
    logger.info(
        "calendar_appointment_created",
```

with:

```python
    session.add(appt)
    await session.commit()
    await session.refresh(appt)
    # TASK-032 R2: plan the reminders of a consultation booked for a known
    # patient (a phone-only booking has nobody the engine can resolve).
    if appt.patient_id is not None and reminder_hooks.enabled_for(tenant):
        await reminder_hooks.after_appointment_booked(appt.id)
    logger.info(
        "calendar_appointment_created",
```

- [ ] **Step 9: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_wiring.py tests/test_reminder_v2_hub_wiring.py tests/test_booking_code_gate.py tests/test_attendee_booking.py tests/test_hub_calendar_money.py -q`
Expected: all pass (the last two prove the untouched OFF behavior of the booking paths).

- [ ] **Step 10: Lint, diff check, commit**

Run: `uvx ruff format tests/test_reminder_v2_wiring.py tests/test_reminder_v2_hub_wiring.py && uvx ruff check --fix tests/test_reminder_v2_wiring.py tests/test_reminder_v2_hub_wiring.py`; `uvx ruff check src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/shared/booking_hold.py src/secretaria/ai/tools.py src/secretaria/api/hub/calendar.py tests/test_booking_code_gate.py`; `git diff --stat` (each modified file: a handful of added lines).

```bash
git add src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/shared/booking_hold.py src/secretaria/ai/tools.py src/secretaria/api/hub/calendar.py tests/test_reminder_v2_wiring.py tests/test_reminder_v2_hub_wiring.py tests/test_booking_code_gate.py
git commit -m "feat(reminders): every booking path plans its reminders (TASK-032 R2)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 11: Wire the hooks into every reschedule and cancel path

Reschedule carriers (spec §4.2 "Remarcar (fluxo, botão, hub)"): the flow's `appointment_reschedule` persist (the `apptresched|` button and the chat "Remarcar" both end there) and hub `POST .../reschedule`. Cancel carriers: the flow's `appointment_cancel_id` persist, the `apptcancel|`/`apptcancelyes|` buttons, the agent's `_mark_appointment_cancelled`, hub `POST .../cancel`. Hub `PATCH .../status` is already wired by R1. **Not wired, on purpose:** the Pix deposit-expiry sweep (`deposit_lifecycle`, coordination with Pix F2 — spec §4.5) — its rows are closed by the engine's guard (`appointment_closed`) at their due time, and nothing is ever sent for them.

**Files:**
- Modify: `src/secretaria/workers/shared/flow_runner.py` (`_apply_flow_result`)
- Modify: `src/secretaria/workers/shared/actions.py` (import; the two `_execute_appointment_cancel` call sites in `_handle_action_button`)
- Modify: `src/secretaria/ai/tools.py` (`_mark_appointment_cancelled`)
- Modify: `src/secretaria/api/hub/calendar.py` (`cancel_appointment`, `reschedule_appointment`)
- Test: `tests/test_reminder_v2_wiring.py`, `tests/test_reminder_v2_hub_wiring.py` (append)

**Interfaces:**
- Consumes: Task 9 hooks.
- Produces: behavior only. Reason strings passed to `cancel_reminders`: `"cancelled"` on every cancel path here.

- [ ] **Step 1: Write the failing tests (worker + agent)**

Append to `tests/test_reminder_v2_wiring.py`:

```python
# ---- move and cancel paths (Task 11) ----------------------------------------


@pytest.mark.parametrize("v2", [True, False])
async def test_a_flow_cancel_cancels_the_reminders_only_with_the_switch_on(db, spy, v2):  # noqa: F811
    world = await seed_world(db, v2=v2)

    await _flow(
        db,
        world,
        FlowRouterResult(
            action="reply", bubbles=[], appointment_cancel_id=world.appointment.google_event_id
        ),
    )

    assert spy.calls == ([("closed", world.appointment.id, "cancelled")] if v2 else [])


@pytest.mark.parametrize(
    ("v2", "confirmations", "expected"),
    [(True, 0, True), (False, 0, False), (False, 1, True)],
)
async def test_a_flow_reschedule_replans_when_on_or_when_there_is_a_count_to_zero(
    db, spy, v2, confirmations, expected  # noqa: F811
):
    world = await seed_world(db, v2=v2, confirmation_count=confirmations)
    new_start = world.start_at + timedelta(days=2)

    await _flow(
        db,
        world,
        FlowRouterResult(
            action="reply",
            bubbles=[],
            appointment_reschedule={
                "google_event_id": world.appointment.google_event_id,
                "start_at": new_start,
                "end_at": new_start + timedelta(minutes=30),
            },
        ),
    )

    assert spy.calls == ([("moved", world.appointment.id)] if expected else [])


@pytest.mark.parametrize("action", ["apptcancel", "apptcancelyes"])
async def test_a_cancel_button_cancels_the_reminders(db, spy, action):  # noqa: F811
    world = await seed_world(db, google_event_id="")  # no Google event: no calendar call

    await tasks._handle_action_button(_reply(world), action, str(world.appointment.id))

    assert spy.calls == [("closed", world.appointment.id, "cancelled")]


async def test_a_cancel_button_with_the_switch_off_touches_nothing(db, spy):  # noqa: F811
    world = await seed_world(db, v2=False, google_event_id="")

    await tasks._handle_action_button(_reply(world), "apptcancel", str(world.appointment.id))

    assert spy.calls == []


@pytest.mark.parametrize("v2", [True, False])
async def test_an_agent_cancel_cancels_the_reminders_only_with_the_switch_on(db, spy, v2):  # noqa: F811
    world = await seed_world(db, v2=v2)
    tenant_token = tools._tenant_id_ctx.set(world.tenant.id)
    try:
        await tools._mark_appointment_cancelled(world.appointment.google_event_id)
    finally:
        tools._tenant_id_ctx.reset(tenant_token)

    assert spy.calls == ([("closed", world.appointment.id, "cancelled")] if v2 else [])
```

- [ ] **Step 2: Write the failing tests (hub)**

Append to `tests/test_reminder_v2_hub_wiring.py`:

```python
# ---- cancel and reschedule (Task 11) ----------------------------------------


async def test_hub_cancel_cancels_the_reminders(client: AsyncClient, db, tenant, spy):  # noqa: F811
    appt = await _appointment(db, tenant)

    response = await client.post(f"{CALENDAR}/appointments/{appt.id}/cancel", json={"confirm": True})

    assert response.status_code == 200
    assert spy.calls == [("closed", appt.id, "cancelled")]


async def test_hub_cancel_with_the_switch_off_touches_nothing(client: AsyncClient, db, tenant, spy):  # noqa: F811
    await _switch(db, tenant, False)
    appt = await _appointment(db, tenant)

    await client.post(f"{CALENDAR}/appointments/{appt.id}/cancel", json={"confirm": True})

    assert spy.calls == []


@pytest.mark.parametrize(
    ("on", "confirmations", "expected"),
    [(True, 0, True), (False, 0, False), (False, 1, True)],
)
async def test_hub_reschedule_replans_when_on_or_when_there_is_a_count_to_zero(
    client: AsyncClient, db, tenant, spy, on, confirmations, expected  # noqa: F811
):
    await _switch(db, tenant, on)
    appt = await _appointment(db, tenant, confirmation_count=confirmations)
    new_start = datetime.now(UTC) + timedelta(days=6)

    response = await client.post(
        f"{CALENDAR}/appointments/{appt.id}/reschedule",
        json={
            "new_start": new_start.isoformat(),
            "new_end": (new_start + timedelta(minutes=30)).isoformat(),
        },
    )

    assert response.status_code == 200
    assert spy.calls == ([("moved", appt.id)] if expected else [])
```

- [ ] **Step 3: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_wiring.py tests/test_reminder_v2_hub_wiring.py -q`
Expected: the "switch on" / "count to zero" cases FAIL with `assert [] == [('closed', ...)]` / `[('moved', ...)]`; the OFF cases pass.

- [ ] **Step 4: Wire the flow's cancel and reschedule**

In `src/secretaria/workers/shared/flow_runner.py`, inside `_apply_flow_result`:

(a) replace

```python
    persisted = True
    booked_appointment: Appointment | None = None
    cancellation_note: str | None = None
```

with

```python
    persisted = True
    booked_appointment: Appointment | None = None
    cancellation_note: str | None = None
    # TASK-032 R2: the appointments this turn closed or moved, for the
    # reminder hooks that run after the commit.
    closed_appointment_id = None
    moved_appointment_id = None
```

(b) in the cancel branch, replace

```python
                                idempotency_key=f"cancel:{result.appointment_cancel_id}",
                            )
```

with

```python
                                idempotency_key=f"cancel:{result.appointment_cancel_id}",
                            )
                            closed_appointment_id = cancelled_appt.id
```

(c) in the reschedule branch, replace

```python
                                idempotency_key=(
                                    f"resched:{resched['google_event_id']}"
                                    f":{resched['start_at'].isoformat()}"
                                ),
                            )
```

with

```python
                                idempotency_key=(
                                    f"resched:{resched['google_event_id']}"
                                    f":{resched['start_at'].isoformat()}"
                                ),
                            )
                            # A moved booking is unconfirmed again even with
                            # the switch OFF (R1 reschedule_reminders zeroes
                            # the counter), so a count > 0 also calls the hook.
                            if (
                                reminder_hooks.enabled_for(tenant)
                                or (resched_appt.confirmation_count or 0) > 0
                            ):
                                moved_appointment_id = resched_appt.id
```

(d) right after the booking-hook block added in Task 10 (the `if reminder_hooks.enabled_for(tenant): await reminder_hooks.after_appointment_booked(...)` lines), at function level, add:

```python
    if persisted and closed_appointment_id is not None and reminder_hooks.enabled_for(tenant):
        await reminder_hooks.after_appointment_closed(closed_appointment_id, reason="cancelled")
    if persisted and moved_appointment_id is not None:
        await reminder_hooks.after_appointment_rescheduled(moved_appointment_id)
```

- [ ] **Step 5: Wire the cancel buttons**

In `src/secretaria/workers/shared/actions.py` add `from secretaria.services import reminder_hooks` immediately above `from secretaria.services.appointment_status import (`. The snippet below appears **twice** in `_handle_action_button` (the `apptcancel` branch outside the Pix window, and `apptcancelyes`); replace both occurrences (Edit with `replace_all`):

```python
            text = await _execute_appointment_cancel(
                session, tenant, tenant_config, appointment, waba_token
            )
            await session.commit()
            await client.send_text_message(to=reply.patient_ref, body=text)
            return
```

with:

```python
            text = await _execute_appointment_cancel(
                session, tenant, tenant_config, appointment, waba_token
            )
            await session.commit()
            if reminder_hooks.enabled_for(tenant):
                await reminder_hooks.after_appointment_closed(appointment.id, reason="cancelled")
            await client.send_text_message(to=reply.patient_ref, body=text)
            return
```

- [ ] **Step 6: Wire the agent's cancel**

In `src/secretaria/ai/tools.py`, inside `_mark_appointment_cancelled`:

(a) replace `    from secretaria.services.payments import deposit_lifecycle` (the lazy import in this function) with:

```python
    from secretaria.services import reminder_hooks
    from secretaria.services.payments import deposit_lifecycle
```

(b) replace `    notice: str | None = None` with:

```python
    notice: str | None = None
    closed_id = None
```

(c) replace

```python
                    tenant = await session.get(Tenant, tenant_id)
                    if tenant is not None:
                        outcome = await deposit_lifecycle.on_appointment_cancelled(
```

with

```python
                    tenant = await session.get(Tenant, tenant_id)
                    if tenant is not None and reminder_hooks.enabled_for(tenant):
                        closed_id = appointment.id
                    if tenant is not None:
                        outcome = await deposit_lifecycle.on_appointment_cancelled(
```

(d) replace `        logger.info("tool_appointment_cancelled", event_id=event_id, rows=result.rowcount)` with:

```python
        if closed_id is not None:
            await reminder_hooks.after_appointment_closed(closed_id, reason="cancelled")
        logger.info("tool_appointment_cancelled", event_id=event_id, rows=result.rowcount)
```

- [ ] **Step 7: Wire the hub cancel and reschedule**

In `src/secretaria/api/hub/calendar.py`:

(a) in `cancel_appointment` replace

```python
    await session.commit()
    await session.refresh(appt)

    # Notify the patient. UNCONDITIONAL now
```

with

```python
    await session.commit()
    await session.refresh(appt)
    if reminder_hooks.enabled_for(tenant):
        await reminder_hooks.after_appointment_closed(appt.id, reason="cancelled")

    # Notify the patient. UNCONDITIONAL now
```

(b) in `reschedule_appointment` replace

```python
    await session.commit()
    await session.refresh(appt)

    if appt.phone and body.custom_message:
```

with

```python
    await session.commit()
    await session.refresh(appt)
    # TASK-032 R2: retire the old reminders and plan the new ones; with the
    # switch OFF only when there is a confirmation count to zero.
    if reminder_hooks.enabled_for(tenant) or (appt.confirmation_count or 0) > 0:
        await reminder_hooks.after_appointment_rescheduled(appt.id)

    if appt.phone and body.custom_message:
```

- [ ] **Step 8: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_wiring.py tests/test_reminder_v2_hub_wiring.py tests/test_action_buttons.py tests/test_flow_cancel_money.py tests/test_appointment_status_taxonomy.py tests/test_hub_calendar_money.py tests/test_ai_tools_cancel_money.py -q`
Expected: all pass.

- [ ] **Step 9: Lint, diff check, commit**

Run: `uvx ruff format tests/test_reminder_v2_wiring.py tests/test_reminder_v2_hub_wiring.py && uvx ruff check --fix tests/test_reminder_v2_wiring.py tests/test_reminder_v2_hub_wiring.py`; `uvx ruff check src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/shared/actions.py src/secretaria/ai/tools.py src/secretaria/api/hub/calendar.py`; `git diff --stat`.

```bash
git add src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/shared/actions.py src/secretaria/ai/tools.py src/secretaria/api/hub/calendar.py tests/test_reminder_v2_wiring.py tests/test_reminder_v2_hub_wiring.py
git commit -m "feat(reminders): reschedule and cancel paths keep the reminder rows in step (TASK-032 R2)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 12: Register the crons; the old cron leaves v2 clinics and Portal patients alone

**Files:**
- Modify: `src/secretaria/workers/reminder_engine.py` (append `reconcile_appointment_reminders`; import `reminder_hooks`)
- Modify: `src/secretaria/workers/arq_worker.py` (import; `cron_jobs`; the comment above it)
- Modify: `src/secretaria/plugins/reminders.py` (`_process_lead_window` query; module docstring)
- Modify: `tests/test_build_identity.py` (`test_worker_registry_is_complete`, `test_worker_startup_logs_identity_and_registry`)
- Test: `tests/test_reminders_plugin.py` (append), `tests/test_reminder_v2_engine.py` (append)

**Interfaces:**
- Consumes: Task 7 `process_appointment_reminders`, Task 9 `reconcile_missing_reminders`.
- Produces: `reminder_engine.reconcile_appointment_reminders(ctx: dict) -> None`; arq cron names `cron:process_appointment_reminders` (every minute) and `cron:reconcile_appointment_reminders` (minutes 4, 14, …, 54).

- [ ] **Step 1: Write the failing tests (old cron)**

In `tests/test_reminders_plugin.py` add `from zoneinfo import ZoneInfo  # noqa: E402` next to the other stdlib imports, then append:

```python
# --------------------------------------------------------------------------
# TASK-032 R2: the switch, and the Portal error loop
# --------------------------------------------------------------------------


async def test_a_clinic_on_the_v2_engine_is_left_to_it(db, monkeypatch: pytest.MonkeyPatch):
    tenant, patient, appointment = await _make_scenario(db, lead=timedelta(hours=1))
    async with db() as session:
        (await session.get(Tenant, tenant.id)).reminders_v2_enabled = True
        await session.commit()
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake())

    await reminders.send_appointment_reminders({"redis": None})

    assert _FakeWhatsAppClient.created == []
    assert await _ledger_count(db, reminders._reminder_key("1h", appointment.id)) == 0


async def test_a_portal_patient_is_never_claimed_nor_sent(db, monkeypatch: pytest.MonkeyPatch):
    """It used to claim the ledger first and then fail on wa_id=None at every sweep."""
    tenant, patient, appointment = await _make_scenario(db, lead=timedelta(hours=1))
    async with db() as session:
        row = await session.get(Patient, patient.id)
        row.wa_id = None
        row.channel = "brain_message"
        row.external_id = str(uuid4())
        await session.commit()
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake())

    await reminders.send_appointment_reminders({"redis": None})

    assert _FakeWhatsAppClient.created == []
    assert await _ledger_count(db, reminders._reminder_key("1h", appointment.id)) == 0


async def test_with_the_switch_off_the_text_is_byte_identical_to_today(
    db, monkeypatch: pytest.MonkeyPatch
):
    tenant, patient, appointment = await _make_scenario(
        db, lead=timedelta(hours=1), last_inbound_ago=timedelta(hours=2)
    )
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake())

    await reminders.send_appointment_reminders({"redis": None})

    when = (
        appointment.start_at.replace(tzinfo=UTC)
        .astimezone(ZoneInfo("America/Sao_Paulo"))
        .strftime("%d/%m/%Y às %H:%M")
    )
    assert _FakeWhatsAppClient.created[0].sent == [
        ("text", "5511999999", f"Lembrete: você tem Consulta agendado(a) para {when} na Clinic.")
    ]
```

- [ ] **Step 2: Write the failing tests (cron entry points + registry)**

Append to `tests/test_reminder_v2_engine.py`:

```python
# --------------------------------------------------------------------------
# Cron entry points (Task 12)
# --------------------------------------------------------------------------


async def test_the_minute_cron_sends_what_is_due_now(db):  # noqa: F811
    now = datetime.now(UTC)
    world = await seed_world(db, start_at=now + timedelta(days=2), last_inbound_at=now - timedelta(hours=1))
    rid = await add_reminder(db, world, due_at=now - timedelta(minutes=1))

    await reminder_engine.process_appointment_reminders({"redis": None})

    assert (await get_reminder(db, rid)).status == "sent"


async def test_the_reconcile_cron_plans_missing_rows(db):  # noqa: F811
    now = datetime.now(UTC)
    world = await seed_world(db, start_at=now + timedelta(days=3))

    await reminder_engine.reconcile_appointment_reminders({})

    async with db() as session:
        kinds = sorted(
            (
                await session.scalars(
                    select(AppointmentReminder.kind).where(
                        AppointmentReminder.appointment_id == world.appointment.id
                    )
                )
            ).all()
        )
    assert kinds == ["day", "hour"]
```

In `tests/test_build_identity.py`, in `test_worker_registry_is_complete` replace

```python
        "cron:send_appointment_reminders",
        "cron:run_onboarding_nudges",
```

with

```python
        "cron:send_appointment_reminders",
        "cron:process_appointment_reminders",
        "cron:reconcile_appointment_reminders",
        "cron:run_onboarding_nudges",
```

and in `test_worker_startup_logs_identity_and_registry` replace `assert len(fields["cron_jobs"]) == 5` with `assert len(fields["cron_jobs"]) == 7`.

- [ ] **Step 3: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminders_plugin.py tests/test_reminder_v2_engine.py tests/test_build_identity.py -q`
Expected: FAILs — the old cron still sends for the v2 clinic, still claims the Portal ledger row (`assert 1 == 0`); `AttributeError: ... has no attribute 'reconcile_appointment_reminders'`; the registry lists differ. (`test_with_the_switch_off_the_text_is_byte_identical_to_today` already passes: it pins today's text.)

- [ ] **Step 4: Old cron — skip v2 clinics and patients without WhatsApp**

In `src/secretaria/plugins/reminders.py`, in `_process_lead_window`, replace

```python
                .where(
                    Appointment.status.in_(_REMINDABLE_STATUSES),
                    Appointment.start_at.is_not(None),
                    Appointment.start_at > window_start,
                    Appointment.start_at <= window_end,
                )
```

with

```python
                .where(
                    Appointment.status.in_(_REMINDABLE_STATUSES),
                    Appointment.start_at.is_not(None),
                    Appointment.start_at > window_start,
                    Appointment.start_at <= window_end,
                    # TASK-032 R2: a clinic on the new engine
                    # (workers/reminder_engine.py) is reminded there, never twice.
                    Tenant.reminders_v2_enabled.is_not(True),
                    # A Portal patient has no WhatsApp number: this job cannot
                    # reach them, and claiming the ledger first made every sweep
                    # fail on the same row again.
                    Patient.wa_id.is_not(None),
                )
```

and at the end of the module docstring (before the closing `"""`), add the paragraph:

```text
TASK-032 R2: clinics with `Tenant.reminders_v2_enabled` are skipped here and
reminded by workers/reminder_engine.py instead (three reminders, buttons,
retries, Portal by e-mail). Patients without a WhatsApp number (Portal) are
skipped too - this job never reached them. Everything else is unchanged.
```

- [ ] **Step 5: Reconcile cron and registration**

Append to `src/secretaria/workers/reminder_engine.py` (and change the import `from secretaria.services import cancellation_notice` to `from secretaria.services import cancellation_notice, reminder_hooks`):

```python


async def reconcile_appointment_reminders(ctx: dict) -> None:
    """arq cron (every 10 minutes): plan what a crash or a fresh switch-ON left
    without reminder rows - the backfill (services/reminder_hooks.py)."""
    await reminder_hooks.reconcile_missing_reminders(now=datetime.now(UTC))
```

In `src/secretaria/workers/arq_worker.py`, add after `from secretaria.workers.payments_tasks import process_asaas_event`:

```python
from secretaria.workers.reminder_engine import (
    process_appointment_reminders,
    reconcile_appointment_reminders,
)
```

and in `cron_jobs` replace

```python
        cron(
            send_appointment_reminders,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
        ),
```

with

```python
        cron(
            send_appointment_reminders,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
        ),
        # TASK-032 R2: the reminder engine for clinics with reminders_v2_enabled
        # (the cron above skips them). Every minute - the atomic claim makes an
        # overlapping tick harmless. The reconcile plans rows a crash or a fresh
        # switch-ON left missing (the backfill), offset from every other minute.
        cron(process_appointment_reminders, minute=set(range(60))),
        cron(reconcile_appointment_reminders, minute={4, 14, 24, 34, 44, 54}),
```

- [ ] **Step 6: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminders_plugin.py tests/test_reminder_v2_engine.py tests/test_build_identity.py -q`
Expected: all pass (every pre-existing test of `test_reminders_plugin.py` unchanged and green).

- [ ] **Step 7: Lint, diff check, commit**

Run: `uvx ruff format src/secretaria/workers/reminder_engine.py tests/test_reminder_v2_engine.py && uvx ruff check --fix src/secretaria/workers/reminder_engine.py tests/test_reminder_v2_engine.py`; `uvx ruff check src/secretaria/plugins/reminders.py src/secretaria/workers/arq_worker.py tests/test_reminders_plugin.py tests/test_build_identity.py`; `git diff --stat`.

```bash
git add src/secretaria/workers/reminder_engine.py src/secretaria/workers/arq_worker.py src/secretaria/plugins/reminders.py tests/test_reminders_plugin.py tests/test_reminder_v2_engine.py tests/test_build_identity.py
git commit -m "feat(reminders): register the v2 crons; old cron skips v2 clinics and Portal patients (TASK-032 R2)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 13: The reminder buttons — patient-scoped handler

**Files:**
- Create: `src/secretaria/workers/shared/reminder_actions.py`
- Modify: `src/secretaria/workers/shared/actions.py` (imports; dispatch at the top of `_handle_action_button`)
- Test: `tests/test_reminder_v2_buttons.py`

**Interfaces:**
- Consumes: R1 `reminder_schedule.register_confirmation`, `CONFIRMATION_SOURCE_REMINDER_BUTTON`, `ReminderMismatchError`, `REMINDER_ANSWER_*`; Task 3 `ACTION_*`, `REMINDER_ACTIONS`; `workers/shared/sender.py::_reply_sender`; `workers/shared/greeting.py::_format_appointment_when`; `workers/shared/text.py::_as_utc`.
- Produces: `handle_reminder_button(reply: _ReplyContext, action: str, reminder_id: str, redis=None) -> None`; `cancel_path_buttons(appointment_id) -> list[tuple[str, str]]` (interim card, R3 replaces it); texts `NOT_FOUND_TEXT`, `NOT_ACTIVE_TEXT`, `CONFIRMED_TEXT`, `MOVED_TEXT`, `CANCEL_PATH_TEXT`, `OTHER_TEXT`.

Behavior (spec §4.2 ids + §4.3 "Confirmar"): every tap is validated against the row — same tenant **and** the conversation's patient — before anything else; a foreign or unknown row gets `Não encontrei essa consulta.` whichever check failed. `Confirmar` counts through R1 and answers `Presença confirmada! ✅ Até {DD/MM às HH:MM}.`; `Cancelar` records the answer and offers the interim card `Remarcar` (`apptresched|<appointment_id>`, Pix limits apply) / `Não vou mais` (`apptcancel|<appointment_id>`, Pix retention warning applies) — R3 replaces it with Remarcar / Marcar outra / Não vou mais; `Outro` records the answer and invites the patient to write (R3 hands it to the AI). A confirmation already recorded on a row is never overwritten by a later Cancelar/Outro on the same row (R1 dedupes on `answer == 'confirm'`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reminder_v2_buttons.py`:

```python
"""Taps on remconfirm| / remcancel| / remother| (TASK-032 R2): scoped to the patient."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from secretaria.core import database as core_database
from secretaria.models import (
    AppointmentStatus,
    Conversation,
    Message,
    MessageDirection,
    Patient,
)
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.workers import tasks
from secretaria.workers.shared.greeting import _format_appointment_when
from tests._patching import workers_ns
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import (
    WA_ID,
    FakeWhatsAppClient,
    add_reminder,
    fake_waba_token,
    get_reminder,
    reload_appointment,
    seed_world,
)


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    FakeWhatsAppClient.reset()
    monkeypatch.setattr(workers_ns, "WhatsAppClient", FakeWhatsAppClient)
    monkeypatch.setattr(workers_ns, "get_waba_token", fake_waba_token)
    yield


def _future() -> datetime:
    return datetime.now(UTC) + timedelta(days=2)


def _reply(conversation_id, *, channel="whatsapp", patient_ref=WA_ID) -> tasks._ReplyContext:
    return tasks._ReplyContext(
        channel=channel, conversation_id=conversation_id, patient_ref=patient_ref, inbound_body="x"
    )


async def _tap(conversation_id, action, reminder_id, **reply_kwargs) -> None:
    await tasks._handle_action_button(
        _reply(conversation_id, **reply_kwargs), action, str(reminder_id)
    )


def _texts() -> list[str]:
    return [item[2] for item in FakeWhatsAppClient.all_sent() if item[0] == "text"]


def _confirmed_text(world) -> str:
    return f"Presença confirmada! ✅ Até {_format_appointment_when(world.start_at, 'America/Sao_Paulo')}."


async def test_confirm_counts_one_and_answers_with_the_date(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world.conversation.id, "remconfirm", rid)

    appointment = await reload_appointment(db, world.appointment.id)
    assert (appointment.confirmation_count, appointment.status) == (1, AppointmentStatus.CONFIRMED)
    assert _texts() == [_confirmed_text(world)]


async def test_the_same_message_confirmed_twice_counts_once(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world.conversation.id, "remconfirm", rid)
    await _tap(world.conversation.id, "remconfirm", rid)

    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 1
    assert _texts() == [_confirmed_text(world)] * 2


async def test_two_different_reminders_count_two(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    day = await add_reminder(db, world, kind="day")
    hour = await add_reminder(db, world, kind="hour")

    await _tap(world.conversation.id, "remconfirm", day)
    await _tap(world.conversation.id, "remconfirm", hour)

    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 2


async def test_cancel_after_confirm_never_lets_the_same_message_count_again(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world.conversation.id, "remconfirm", rid)
    await _tap(world.conversation.id, "remcancel", rid)
    await _tap(world.conversation.id, "remconfirm", rid)

    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 1
    assert (await get_reminder(db, rid)).answer == "confirm"


async def test_another_patients_reminder_is_not_found_and_counts_nothing(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)
    async with db() as session:
        intruder = Patient(id=uuid4(), tenant_id=world.tenant.id, wa_id="5511900002222", name="João")
        session.add(intruder)
        await session.flush()
        conversation = Conversation(id=uuid4(), tenant_id=world.tenant.id, patient_id=intruder.id)
        session.add(conversation)
        await session.commit()

    await _tap(conversation.id, "remconfirm", rid, patient_ref="5511900002222")

    assert _texts() == ["Não encontrei essa consulta."]
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 0
    assert (await get_reminder(db, rid)).answer is None


async def test_another_clinics_reminder_is_not_found(db):  # noqa: F811
    mine = await seed_world(db, start_at=_future())
    theirs = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, theirs)

    await _tap(mine.conversation.id, "remconfirm", rid)

    assert _texts() == ["Não encontrei essa consulta."]
    assert (await reload_appointment(db, theirs.appointment.id)).confirmation_count == 0


async def test_confirm_after_the_appointment_was_cancelled_does_nothing(db):  # noqa: F811
    world = await seed_world(db, start_at=_future(), status=AppointmentStatus.CANCELLED)
    rid = await add_reminder(db, world)

    await _tap(world.conversation.id, "remconfirm", rid)

    appointment = await reload_appointment(db, world.appointment.id)
    assert _texts() == ["Essa consulta não está mais ativa."]
    assert (appointment.confirmation_count, appointment.status) == (0, AppointmentStatus.CANCELLED)


async def test_a_tap_on_a_message_about_the_old_time_points_to_the_new_one(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world, appointment_start_at=world.start_at - timedelta(days=1))

    await _tap(world.conversation.id, "remconfirm", rid)

    when = _format_appointment_when(world.start_at, "America/Sao_Paulo")
    assert _texts() == [f"Essa mensagem era sobre um horário antigo. Sua consulta agora é em {when}."]
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 0


async def test_cancel_offers_reschedule_or_give_up_on_this_appointment(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world.conversation.id, "remcancel", rid)

    [(kind, _to, body, buttons)] = FakeWhatsAppClient.all_sent()
    assert (kind, body) == ("buttons", "O que você prefere?")
    assert buttons == [
        (f"apptresched|{world.appointment.id}", "Remarcar"),
        (f"apptcancel|{world.appointment.id}", "Não vou mais"),
    ]
    row = await get_reminder(db, rid)
    assert row.answer == "cancel" and row.answered_at is not None
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED


async def test_other_invites_the_patient_to_write(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world.conversation.id, "remother", rid)

    assert _texts() == ["Claro! Me conta como posso te ajudar com a sua consulta."]
    assert (await get_reminder(db, rid)).answer == "other"


async def test_a_portal_tap_is_answered_in_the_portal(db):  # noqa: F811
    world = await seed_world(db, start_at=_future(), channel=CHANNEL_BRAIN_MESSAGE)
    rid = await add_reminder(db, world)

    await _tap(
        world.conversation.id,
        "remconfirm",
        rid,
        channel=CHANNEL_BRAIN_MESSAGE,
        patient_ref=world.patient.external_id,
    )

    assert FakeWhatsAppClient.created == []
    async with db() as session:
        bodies = list(
            await session.scalars(
                select(Message.body).where(
                    Message.conversation_id == world.conversation.id,
                    Message.direction == MessageDirection.OUTBOUND,
                )
            )
        )
    assert bodies == [_confirmed_text(world)]
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 1


async def test_a_malformed_reminder_id_is_ignored_silently(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())

    await tasks._handle_action_button(_reply(world.conversation.id), "remconfirm", "not-a-uuid")

    assert FakeWhatsAppClient.created == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_buttons.py -q`
Expected: FAILs — the tap falls into the appointment-id handler, which looks the REMINDER id up as an appointment: `assert ['Não encontrei essa consulta.'] == ['Presença confirmada! ✅ Até ...']`, counts stay 0.

- [ ] **Step 3: Implement the handler**

Create `src/secretaria/workers/shared/reminder_actions.py`:

```python
"""A tap on a reminder button: remconfirm| / remcancel| / remother| (TASK-032 R2).

The id after the bar is an `appointment_reminders` ROW id. The older
`apptconfirm|<appointment_id>` family can only be checked against the TENANT;
the row names the patient too, so a tap is honoured only when the row's tenant
AND patient are the tapping conversation's. Anything else gets the same polite
miss, whichever check failed - never reveal whose row it is.

Channel-neutral: replies go through `_reply_sender`, so this serves a WhatsApp
tap today and a Portal tap once R3 decodes Portal ids
(schemas/webhook.py::decode_action_id). Runs before the handover/flow/LLM
gates, like every action button (workers/turn_router.py), and never changes
`flow_state` itself.
"""

from datetime import UTC, datetime
from uuid import UUID

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    AppointmentReminder,
    Conversation,
    Tenant,
    is_live_status,
)
from secretaria.models.appointment_reminder import (
    REMINDER_ANSWER_CANCEL,
    REMINDER_ANSWER_CONFIRM,
    REMINDER_ANSWER_OTHER,
)
from secretaria.services import reminder_schedule
from secretaria.services.reminder_text import ACTION_CANCEL, ACTION_CONFIRM, REMINDER_ACTIONS
from secretaria.services.tenant_config import get_waba_token
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.greeting import _format_appointment_when
from secretaria.workers.shared.sender import _reply_sender
from secretaria.workers.shared.text import _as_utc

logger = get_logger(__name__)

NOT_FOUND_TEXT = "Não encontrei essa consulta."
NOT_ACTIVE_TEXT = "Essa consulta não está mais ativa."
CONFIRMED_TEXT = "Presença confirmada! ✅ Até {when}."
MOVED_TEXT = "Essa mensagem era sobre um horário antigo. Sua consulta agora é em {when}."
CANCEL_PATH_TEXT = "O que você prefere?"
OTHER_TEXT = "Claro! Me conta como posso te ajudar com a sua consulta."
# <= 20 characters each (WhatsApp reply-button cap).
LABEL_RESCHEDULE = "Remarcar"
LABEL_GIVE_UP = "Não vou mais"


def cancel_path_buttons(appointment_id) -> list[tuple[str, str]]:
    """Interim "Cancelar" card. R3 replaces it with Remarcar / Marcar outra / Não vou mais.

    Both ids are the existing appointment-scoped ones, so the Pix rules keep
    applying unchanged: `apptresched|` checks the reschedule limit and
    `apptcancel|` shows the retention warning inside the refund window
    (workers/shared/actions.py::_handle_action_button).
    """
    return [
        (f"apptresched|{appointment_id}", LABEL_RESCHEDULE),
        (f"apptcancel|{appointment_id}", LABEL_GIVE_UP),
    ]


async def handle_reminder_button(
    reply: _ReplyContext, action: str, reminder_id: str, redis=None
) -> None:
    """Answer one reminder-button tap. Never raises on a foreign or stale id."""
    if reply.conversation_id is None or action not in REMINDER_ACTIONS:
        return
    try:
        row_id = UUID(reminder_id)
    except ValueError:
        return
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        tenant = await session.get(Tenant, conversation.tenant_id) if conversation else None
        if tenant is None:
            return
        waba_token = await get_waba_token(session, tenant.id)
        client = _reply_sender(reply, tenant, waba_token)
        if client is None:
            logger.error("reminder_button_no_sender", tenant_id=str(tenant.id))
            return

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
            return

        if (
            not is_live_status(appointment.status)
            or appointment.start_at is None
            or _as_utc(appointment.start_at) <= now
        ):
            await client.send_text_message(to=reply.patient_ref, body=NOT_ACTIVE_TEXT)
            return
        when = _format_appointment_when(appointment.start_at, tenant.timezone)
        if _as_utc(reminder.appointment_start_at) != _as_utc(appointment.start_at):
            await client.send_text_message(to=reply.patient_ref, body=MOVED_TEXT.format(when=when))
            return

        if action == ACTION_CONFIRM:
            try:
                await reminder_schedule.register_confirmation(
                    session,
                    appointment=appointment,
                    reminder_id=reminder.id,
                    source=reminder_schedule.CONFIRMATION_SOURCE_REMINDER_BUTTON,
                    now=now,
                )
            except reminder_schedule.ReminderMismatchError:
                # Checked above; kept so a race can never surface as an error.
                await client.send_text_message(to=reply.patient_ref, body=NOT_FOUND_TEXT)
                return
            await session.commit()
            await client.send_text_message(
                to=reply.patient_ref, body=CONFIRMED_TEXT.format(when=when)
            )
            return

        if reminder.answer != REMINDER_ANSWER_CONFIRM:
            # A confirmation already counted stays recorded: overwriting it would
            # let the same message count twice (R1 dedupes on answer == confirm).
            reminder.answer = (
                REMINDER_ANSWER_CANCEL if action == ACTION_CANCEL else REMINDER_ANSWER_OTHER
            )
            reminder.answered_at = now
        await session.commit()
        if action == ACTION_CANCEL:
            await client.send_buttons(
                reply.patient_ref, CANCEL_PATH_TEXT, cancel_path_buttons(appointment.id)
            )
        else:
            await client.send_text_message(to=reply.patient_ref, body=OTHER_TEXT)
```

- [ ] **Step 4: Dispatch from `_handle_action_button`**

In `src/secretaria/workers/shared/actions.py`:

(a) add to the imports `from secretaria.services.reminder_text import REMINDER_ACTIONS` (after `from secretaria.services.payments import deposit_lifecycle`) and `from secretaria.workers.shared.reminder_actions import handle_reminder_button` (after the `from secretaria.workers.shared.llm_context import (...)` block).

(b) replace

```python
    try:
        appt_uuid = UUID(appointment_id)
    except ValueError:
        return  # already validated by extract_action_button; defensive only
```

with

```python
    try:
        appt_uuid = UUID(appointment_id)
    except ValueError:
        return  # already validated by extract_action_button; defensive only

    # TASK-032 R2: the reminder buttons carry a REMINDER row id, not an
    # appointment id; their handler checks the row against the patient.
    if action in REMINDER_ACTIONS:
        await handle_reminder_button(reply, action, appointment_id, redis=redis)
        return
```

- [ ] **Step 5: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_buttons.py tests/test_action_buttons.py tests/test_reminder_v2_text.py -q`
Expected: all pass (`12` new; `test_action_buttons.py` proves the old ids are unchanged).

- [ ] **Step 6: Lint, diff check, commit**

Run: `uvx ruff format src/secretaria/workers/shared/reminder_actions.py tests/test_reminder_v2_buttons.py && uvx ruff check --fix src/secretaria/workers/shared/reminder_actions.py tests/test_reminder_v2_buttons.py`; `uvx ruff check src/secretaria/workers/shared/actions.py`; the bidi scan of Task 2 Step 5 on both created files; `git diff --stat`.

```bash
git add src/secretaria/workers/shared/reminder_actions.py src/secretaria/workers/shared/actions.py tests/test_reminder_v2_buttons.py
git commit -m "feat(reminders): reminder buttons scoped to the patient who tapped (TASK-032 R2)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 14: Documentation and focused validation

Only after Tasks 1–13 are green (AI_WORKFLOW: docs are updated after validation, not before).

**Files:**
- Create: `docs/CHECKPOINT_lembretes_r2.md`
- Modify: `docs/CHECKPOINT_lembretes_r1.md` (one pointer line at the end), `docs/CHECKPOINT_patient_channel_identity.md` (the `plugins/reminders.py` row of the `.wa_id` readers table), `CLAUDE.md` (one line in "## Documentação")

- [ ] **Step 1: Run every test file this plan touched, one command per group (each well under a minute)**

```bash
BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_setup.py tests/test_reminder_v2_text.py tests/test_reminder_v2_delivery.py -q
BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_engine.py tests/test_reminder_v2_hooks.py tests/test_reminder_v2_buttons.py -q
BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_v2_wiring.py tests/test_reminder_v2_hub_wiring.py tests/test_booking_code_gate.py -q
BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminders_plugin.py tests/test_action_buttons.py tests/test_build_identity.py tests/test_email_transactional.py -q
BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_attendee_booking.py tests/test_appointment_status_taxonomy.py tests/test_flow_cancel_money.py tests/test_ai_tools_cancel_money.py tests/test_hub_calendar_money.py -q
BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_schedule.py tests/test_reminder_confirmation.py tests/test_hub_calendar_confirmation.py -q
```

Expected: all green. A failure in a file this plan did not modify must be checked against `git stash`-free evidence before being called a regression: run the same file at the R1 tip (`git worktree add` of the R1 commit into the scratchpad is fine) and record pre-existing failures separately (AI_WORKFLOW "Validação").

- [ ] **Step 2: Write the checkpoint**

Create `docs/CHECKPOINT_lembretes_r2.md`:

```markdown
# CHECKPOINT — Lembretes R2: motor de lembretes (TASK-032)

Spec: `docs/superpowers/specs/2026-10-03-lembretes-e-confirmacao-design.md` §4.2.
Plano: `docs/superpowers/plans/2026-10-03-lembretes-r2-motor.md`. Base: R1 (`docs/CHECKPOINT_lembretes_r1.md`).

## Estado

- Local + commitado na branch `task/TASK-032-lembretes-e-confirmacao`. **Não pushado, não deployado.**
- Interruptor `Tenant.reminders_v2_enabled` desligado em todas as clínicas: nada muda para ninguém até ligar.
- Modelo `lembrete_consulta_v2` ainda **não submetido/aprovado** na Meta (folha: `docs/LEMBRETES_MODELOS_META.md`).

## O que entrou onde

| Peça | Onde |
|---|---|
| Texto único (frase da spec, requisitos, uma linha, 6 variáveis, botões, link do Portal) | `services/reminder_text.py` |
| Envio por canal (janela 24 h, modelo novo/Pix/simples com fallback, Portal chat + e-mail) | `services/reminder_delivery.py::deliver_reminder` |
| Cron de 1 min: seleção, reivindicação atômica, guardas, até 4 tentativas, falha de entrega, histórico, uso | `workers/reminder_engine.py::run_reminder_tick` |
| Ganchos depois do commit + reconciliação (backfill) a cada 10 min | `services/reminder_hooks.py` |
| Chamadas dos ganchos | fluxo (`_apply_flow_result`), Portal (`_promote_booking_hold`), IA (`_persist_appointment`, `_mark_appointment_cancelled`), botões (`_handle_action_button`), hub (`create_appointment`, `cancel_appointment`, `reschedule_appointment`); `PATCH status` é do R1 |
| Botões `remconfirm|`/`remcancel|`/`remother|` | decodificação `schemas/webhook.py::decode_action_id`; tratamento `workers/shared/reminder_actions.py::handle_reminder_button` |
| Cron antigo | `plugins/reminders.py`: pula clínicas com o interruptor ligado e pacientes sem WhatsApp |
| E-mail do Portal | `services/email.py` template `appointment_reminder_patient` |

## Decisões tomadas sem consulta

1. Ganchos rodam **depois** do commit, em transação própria (um erro de lembrete nunca desfaz um agendamento); a reconciliação de 10 min cura o que faltar e é o backfill ao ligar o interruptor.
2. Link do e-mail do Portal = `{BRAIN_MESSAGE_PORTAL_URL}/clinicas/?convite=<tenant_id>` montado aqui, sem chamar o brain-api (o brain-api já aceita o UUID da clínica como convite; nenhum contrato novo). Depende da variável `BRAIN_MESSAGE_PORTAL_URL` na API e no worker.
3. Consulta com sinal Pix pago mantém os 3 botões Confirmar/Reagendar/Cancelar; só o Confirmar passou para o id do lembrete (conta e é do paciente); Reagendar/Cancelar continuam com as regras do Pix.
4. Atraso máximo para enviar: lembrete extra 6 h, de 1 dia 3 h, de 1 hora 30 min; nunca depois do início da consulta.
5. Cancelar no lembrete mostra, por enquanto, Remarcar / Não vou mais (o R3 troca pelo cartão de três opções). Outro só convida a escrever (o R3 entrega à IA).
6. Varredura de expiração do sinal Pix não chama gancho (coordenação com Pix F2): as linhas dela são fechadas pela guarda do motor na hora de enviar.
7. Paciente do Portal sem e-mail: a mensagem fica na conversa e a linha vira `failed`/`no_email` com aviso de falha de entrega.

## Pendências

- Dono: submeter `lembrete_consulta_v2` à Meta; depois da aprovação ligar `REMINDER_V2_TEMPLATE_APPROVED=true` (API e worker).
- R3: decodificar no Portal os toques `rem*` (chamar `decode_action_id` + `handle_reminder_button`), cartão de três opções do Cancelar, Outro → IA, mensagem de abertura (`kind='chat'`).
- R4: avisos à clínica lendo `warn_due_at`/`warn_kind` — só linhas `sent`/`failed`; `failed` + `delivery_failed` = falha de entrega.
- TASK-030 P5: o prompt e o filtro da IA ainda proíbem falar de lembretes.
- `reminder_opt_out` continua sem tela para o paciente gravar (fora de escopo, spec §7).
```

- [ ] **Step 3: Pointers**

- At the end of `docs/CHECKPOINT_lembretes_r1.md` add the line: `R2 (motor de lembretes) construído: ver docs/CHECKPOINT_lembretes_r2.md.`
- In `docs/CHECKPOINT_patient_channel_identity.md`, in the `.wa_id` readers table, replace the cell text `5 envios \`to=patient.wa_id\`; o join em \`:309\` não filtra \`wa_id IS NOT NULL\`` (row `plugins/reminders.py:...`) with `5 envios \`to=patient.wa_id\`; desde a TASK-032 R2 a consulta filtra \`wa_id IS NOT NULL\` (paciente do Portal é lembrado por \`workers/reminder_engine.py\`, por e-mail)`.
- In `CLAUDE.md`, under `## Documentação`, after the `docs/CHECKPOINT_portal_mensagens_recentes.md` paragraph, add: `` `docs/CHECKPOINT_lembretes_r2.md` — TASK-032 R2: motor de lembretes (3 lembretes com botões, Portal por e-mail), atrás de `reminders_v2_enabled`; commitado, não deployado; modelo da Meta em `docs/LEMBRETES_MODELOS_META.md`. ``

- [ ] **Step 4: Diff check and commit**

Run: `git diff --stat` (the three modified docs: a few lines each).

```bash
git add docs/CHECKPOINT_lembretes_r2.md docs/CHECKPOINT_lembretes_r1.md docs/CHECKPOINT_patient_channel_identity.md CLAUDE.md
git commit -m "docs(reminders): R2 checkpoint and pointers (TASK-032 R2)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Self-review (done while writing; kept for the executor)

**Spec coverage (§4.2 and the §7 rows of R2):**

| Spec item | Task |
|---|---|
| `schedule_reminders` in every creation path (flow, AI tool, Portal promotion, hub create) | 9, 10 |
| Reschedule invalidates and recreates (flow, button → flow, hub) | 9, 11 |
| Cancel/attended/no-show cancels pending (flow, buttons, AI, hub; PATCH = R1; deposit expiry = engine guard) | 7, 9, 11 |
| Backfill for existing future appointments | 9 (reconcile), 12 (cron) |
| Cron every minute, atomic claim, safe with two workers | 7, 12 |
| Max lateness per kind | 7 |
| Up to 4 attempts, then `failed` + delivery-failure marking | 8 |
| Text: doctor, date **and** time, service, requirements; attendee; clinic timezone | 2 |
| WhatsApp inside window: 3 buttons | 6 |
| WhatsApp outside window: approved template with 3 quick replies; requirements in one line; fallback to today's plain template while unapproved | 4, 6 |
| Outbound reminders recorded as `Message` | 6 (Portal), 8 (WhatsApp) |
| Portal: e-mail with link + message waiting in the chat | 5, 6 |
| New ids `remconfirm|`/`remcancel|`/`remother|`, scoped to the patient; old ids keep working | 3, 13 |
| Stop prompts after two; no warning | 6, 8 |
| Switch OFF = old cron unchanged; switch ON = old cron skips the clinic | 7, 12 |
| Portal error loop of the old cron | 12 |
| Fuso (§7) | 2 |
| Meta template submission (§4.5) | 4 |

**Spec gaps found (decided in this plan, flagged for the owner):** the spec says the e-mail link comes "do brain-api" — built locally instead from the same URL + clinic UUID that brain-api accepts (decision 2 of the checkpoint); the spec gives no lateness limit except "o de 1 hora não sai depois de 30 min" — 6 h / 3 h chosen for the other two; the spec does not say what "Cancelar" does before R3 exists — interim two-button card; Portal patient without e-mail is not covered by the spec — chat copy + delivery-failed warning.

**Type consistency:** `ReminderJob`/`DeliveryOutcome` field names match between Tasks 6, 7, 8; `claim_reminder` returns `int | None` everywhere; hook signatures in Task 9 match `HookSpy` (Task 1) and every call site (Tasks 10–11); `REMINDER_ACTIONS` is the single list both the builder (Task 3) and the dispatcher (Task 13) use.

**Placeholders:** none — every code step carries its code; every anchor quotes the exact existing lines.

---

## Deploy e liberação (only on the owner's explicit request, each step separately)

1. **Meta first (external, slow):** the owner submits `lembrete_consulta_v2` from `docs/LEMBRETES_MODELOS_META.md` on each WABA of the clinics that will use it. Nothing waits on the approval: until then the plain template is used outside the window.
2. **R1 migration must already be applied** (`alembic upgrade head`, R1 deploy section). R2 has no migration.
3. **API and worker together** (two separate Easypanel services — `CLAUDE.md` "Deploy — a API e o worker são DOIS serviços"): the hooks run in both (hub in the API; flow, Portal and agent in the worker) and only the worker runs the crons. Confirm `deploy_parity=match` in the worker's `worker_started` log and that `cron_jobs` lists `cron:process_appointment_reminders` and `cron:reconcile_appointment_reminders`.
4. **Environment (set by the owner in the panel, API and worker; no automation reads or writes Easypanel variables):** `BRAIN_MESSAGE_PORTAL_URL` (same value as brain-api); `REMINDER_V2_TEMPLATE_APPROVED` stays `false` until step 1 is approved.
5. **Switch stays OFF everywhere after deploy.** Turn `reminders_v2_enabled` ON first for the test clinic only; the next reconcile (≤ 10 min) plans its future appointments. Prove live: a WhatsApp patient inside the window gets the card with 3 buttons and Confirmar answers "Presença confirmada!"; a Portal test patient gets the chat message and the e-mail with the link; the old cron logs nothing for that clinic.
6. **After Meta approval:** set `REMINDER_V2_TEMPLATE_APPROVED=true` (API and worker) and prove one reminder outside the window arrives with the 3 quick replies.
7. Other clinics: only by the owner's decision, one at a time.

