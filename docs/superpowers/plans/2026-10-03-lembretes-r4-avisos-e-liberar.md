# Lembretes R4 — Avisos por e-mail, estado vermelho, liberar horário e mensagem ao paciente (API) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Warn the clinic by e-mail when a reminded patient has not confirmed (or the reminder could not be delivered), and give the clinic two staff actions on the red appointment: `POST /tenants/me/calendar/appointments/{id}/release` (free the slot) and `POST /tenants/me/calendar/appointments/{id}/message` (write to the patient on their own channel). Backend only; nothing is released automatically.

**Architecture:** R1 owns the data (`appointment_reminders`, `Appointment.confirmation_count`, `display_state`, `attention` already on `GET /events`) and R2 owns the engine that sets `warn_due_at` / `warn_kind` on sent and failed rows. R4 adds (1) a one-minute cron `process_confirmation_warnings` (`workers/confirmation_warnings.py`) that selects due rows, claims each with an atomic `UPDATE ... WHERE warned_at IS NULL` (that claim is what makes the warning fire once and what makes the agenda turn red, because `warned_at` is the red marker), and sends one e-mail per appointment to the clinic's operational alert address (`Tenant.contact_email`, the same one `send_cancellation_escalation_alert` uses); (2) two small services, `services/staff_patient_message.py` (free text on the patient's channel + Portal e-mail nudge) and `services/appointment_release.py` (the patient notice after a release); (3) two hub endpoints in `api/hub/calendar.py`; (4) a pure preview of the deposit outcome in `services/payments/deposit_lifecycle.py` so the 409 can show the retention text before anything happens.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async (Postgres in prod, in-memory SQLite in tests), arq cron, WhatsApp Cloud API (`services/whatsapp.py`), Brain-Message sender (`services/channel_sender.py`), SMTP e-mail (`services/email.py`), pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-10-03-lembretes-e-confirmacao-design.md` — §4.4 (warning, red state, release, message, manual confirm), §5 criteria 8-10, §7 rows "Horário liberado só no status deixa o Google ocupado" and "Sinal Pix pago retido ao liberar". Code base: `main` b72c4ad + spec commit 3c39bd0 + R1 + R2 (both must be merged before Task 1). Worktree: `C:\TECH\BRAIN-worktrees\TASK-032\secretarIA`; all paths are relative to it; Python package root `src/secretaria/`.

**Order constraint (from R1):** R2 must be merged before R4 starts: R2 and R4 both edit `api/hub/calendar.py`, `services/email.py` and `workers/arq_worker.py`. R4 only appends to those files and never touches R2's lines.

## Global Constraints

- Warn deadlines are R1's (`warn_due_at = due_at + 2 h` for `custom` and `day`, `+ 20 min` for `hour`) and R2's (`warn_kind` = `unconfirmed` on a sent row with buttons, `delivery_failed` on an exhausted row; `warn_due_at` empty on rows sent without buttons or closed by a guard). R4 never writes `warn_due_at`/`warn_kind`; it only reads them and writes `warned_at`.
- R4 warns **only** on rows with `status IN ('sent','failed')` (R2 contract: a `pending` row keeps R1's `warn_due_at` but was never sent), `kind != 'chat'`, whose `appointment_start_at` equals the appointment's current `start_at`, on an appointment that is live (`LIVE_APPOINTMENT_STATUSES`), in the future, with `confirmation_count = 0`, of a clinic with `reminders_v2_enabled = true`.
- At most **3** warnings per appointment (counted on the current start version). A confirmed (count >= 1), cancelled, released, attended, no-show or past appointment is never warned.
- The warning e-mail goes to `Tenant.contact_email` (stripped, non-empty). Content: patient name, doctor, service, date and time in the clinic's timezone, which reminder, a link to the appointment in the agenda. **No clinical detail** (no requirements, no insurance, no anamnese). The patient name and the link are PII/secret-adjacent: they live in the BODY only, never in a log line.
- Release: delete the Google event on the **owning** calendar first (fail closed: if Google refuses, return 502 and change nothing), then cancel the row with reason `unconfirmed`, source `hub`; run `deposit_lifecycle.on_appointment_cancelled` in the same transaction; cancel the reminder rows (`reminder_schedule.cancel_reminders(..., reason="released")`) in the same transaction; tell the patient after the commit. A **paid** Pix deposit requires `acknowledge_retention=true`, else 409 `retention_ack_required` with the warning text. Nothing is deployed, pushed or sent to Meta from this plan.
- Staff actions (`release`, `message`) are manual clinic decisions and work regardless of `Tenant.reminders_v2_enabled` (the switch gates only what the system does by itself). Every query on appointments, patients, conversations and reminders filters by `tenant_id`; a foreign appointment id is a 404.
- Patient-facing and clinic-facing text in Portuguese; code, comments and log event names in English. Logs carry ids, kinds and error classes only — never a name, phone, e-mail, message text or the agenda link.
- Layering (`CLAUDE.md`): `api -> workers -> services -> models -> core`; `services/*` never imports `workers/*` or `plugins/*`.
- No migration in R4 (R1 owns the schema). No new setting: the agenda link reuses `Settings.DOCTOR_AGENDA_URL` (`<url>?consulta=<appointment_id>`; R5 must open the appointment drawer from that query parameter).
- Tests are run from Git Bash: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest <file> -q`. Never `ruff format .` (lint is red at HEAD): `uvx ruff format <file>` then `uvx ruff check --fix <file>` **only on files this plan creates**; on files it modifies run `uvx ruff check <file>` only and fix findings in the lines you touched by hand. Files this plan modifies are CRLF in the working tree: after each task run `git diff --stat` and confirm only the touched lines changed (a whole-file diff means the line endings flipped — `git checkout -- <file>` and redo the edit).
- Commit with `git add <explicit paths>` (never `git add -A`; parallel sessions share worktrees). Every commit message ends with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Consumes (verified against the already-written plans)

From **R1** (`2026-10-03-lembretes-r1-fundacao.md`): `secretaria.models.AppointmentReminder` (fields `id, tenant_id, appointment_id, patient_id, kind, appointment_start_at, due_at, status, channel, with_prompt, attempts, last_error_code, sent_at, answered_at, answer, warn_due_at, warned_at, warn_kind, created_at, updated_at`), constants `REMINDER_KIND_CHAT`, `REMINDER_STATUS_{SENT,FAILED}`, `REMINDER_WARN_{UNCONFIRMED,DELIVERY_FAILED}` (module `secretaria.models.appointment_reminder`), `Appointment.confirmation_count`, `Tenant.reminders_v2_enabled`, `services/reminder_schedule.py::cancel_reminders(session, appointment_id, *, reason) -> int` (never commits; cancels `pending`/`sending` rows and clears pending warnings), `tests/_reminder_fixtures.py::db`. `GET /events` already returns `status`, `confirmation_count`, `display_state`, `attention`, `reminders[]`; `PATCH .../status` -> `confirmed` already counts one staff confirmation. R4 changes none of that.

From **R2** (`2026-10-03-lembretes-r2-motor.md`): `services/reminder_text.py::portal_conversation_link(tenant_id) -> str | None`; `tests/_reminders_v2.py` helpers `NOW` (2026-10-05 12:00 UTC), `WA_ID`, `FakeWhatsAppClient`, `fake_waba_token`, `World`, `seed_world(db, *, v2, channel, wa_id, email, start_at, status, professional_name, requirements, last_inbound_at, ...)`, `seed_paid_deposit(db, world)`, `get_reminder`, `reload_appointment`; the module convention `from secretaria.core import database as core_database` + `core_database.async_session_factory()` (tests monkeypatch it); the arq cron registry after R2 has **7** crons including `cron:reconcile_appointment_reminders`; `workers/reminder_engine.py` sets `warn_kind`/`warn_due_at` as described above.

From the **existing code**: `services/cancellation_notice.py` (`last_inbound_at`, `is_inside_window`, `whatsapp_deep_link`, `build_cancellation_text`, `meta_language_code`, `join_blocks`), `workers/whatsapp/notifications.py::send_cancellation_notice` (arq job, args `tenant_id, appointment_id, professional_name, justification, extra_notice, allow_paid`), `services/channel_sender.py::BrainMessageSender` (+ `RECORDED_MESSAGE_ID`, `CHANNEL_BRAIN_MESSAGE`), `services/tenant_config.py::resolve_professional_calendar`/`load_tenant_config`/`get_waba_token`, `services/email.py::send_transactional_email_result`/`EmailOutcome`/`_send_sync`, `services/payments/deposit_lifecycle.py` (`on_appointment_cancelled`, `cancellation_notice`, `get_deposit_for_appointment`), `services/appointment_status.py::log_status_transition`, `api/hub/calendar.py` helpers `_get_appointment`, `_get_calendar`, `_professional_name`, `_appointment_read`, `_deposit_status_value`.

## Produces for R5 (front) — exact wire shapes

All paths are under `/tenants/me/calendar`; all need the hub bearer token; an appointment id of another clinic or a malformed id is **404** `{"detail":"Appointment not found"}`. Error bodies below use FastAPI's `{"detail": {...}}` with a machine-readable `code`.

**`POST /appointments/{id}/release`** — request body (all optional):
`{"acknowledge_retention": false, "release_confirmed": false, "notify_outside_window": false, "justification": null}`
200 response = the existing `AppointmentRead` shape (so `status` is `"cancelled"`, `deposit_status`, `deposit_outcome`, `confirmation_count`) **plus** `"patient_notice": "whatsapp_queued" | "whatsapp_outside_window" | "portal_chat" | "portal_chat_email" | "no_channel" | "queue_unavailable" | "notice_failed"`.
Errors: **409** `{"code":"retention_ack_required","message":"<texto de retenção em português>","deposit_outcome":"retained"|"partial_refund"|"refunded","amount_cents":int}` (show `message`, then re-POST with `acknowledge_retention:true`); **409** `{"code":"not_live","message":...,"status":"cancelled"|...}` (already released/cancelled/attended); **409** `{"code":"already_confirmed","confirmation_count":1|2,...}` (patient confirmed meanwhile; re-POST with `release_confirmed:true` only after the clinic agrees); **409** `{"code":"calendar_unresolved",...}`; **422** `{"code":"not_a_patient_appointment"}` (a block) or the existing plain-string 422 when Google Calendar is not connected; **502** `{"code":"calendar_unavailable","message":...}` (Google refused: nothing changed, safe to retry). The window/cost information for the "avisar paciente fora das 24 h" checkbox comes from the existing `GET /appointments/{id}/cancel-preview` (`inside_window`, `template_cost_brl`, `cost_is_estimate`, `whatsapp_link`).

**`POST /appointments/{id}/message`** — request `{"text": "<1..1000 chars, trimmed, not blank>", "notify_outside_window": false}`.
200 `{"delivery": "whatsapp_text" | "whatsapp_template" | "portal_chat", "email_nudge": null | "sent" | "no_email" | "not_sent", "message_id": "<uuid>" | null}` (`email_nudge` is only non-null for `portal_chat`).
Errors: **409** `{"code":"outside_window_not_authorised","message":...,"template_cost_brl":"","whatsapp_link":"https://wa.me/..."|null}` (WhatsApp patient outside 24 h and `notify_outside_window` false: offer the paid template or the free `wa.me` link); **422** `{"code":"no_channel",...}` (no patient, no number, or a Portal patient with no conversation yet); **502** `{"code":"delivery_failed",...}`; **422** (pydantic) for blank/too long text.

**Warning e-mail** (clinic side, not an endpoint): link `DOCTOR_AGENDA_URL` + `?consulta=<appointment_id>` (or `&consulta=` when the URL already has a query). R5 opens the drawer of that appointment when `consulta` is present.

## Review Focus

Each line is pinned by a test in the task named in brackets. The first eleven are the ones the owner asked to pin.

1. A warning fires once per row: two ticks, two workers' claim, one e-mail and one `warned_at` [Task 2 claim, Task 3 tick].
2. Warnings stop on confirm / cancel / release / past: a confirmation between selection and claim defeats the claim; cancelled, attended, no-show, released and already-started appointments are never selected [Tasks 2, 3, 6].
3. Count >= 1 never warns, including a second confirmation that arrives after the row was selected [Task 2].
4. Second release of the same appointment: 409 `not_live`, Google called once, deposit hook once, one patient notice; two concurrent releases: exactly one wins [Task 6].
5. Google delete failure: 502, appointment stays live, deposit untouched, no notice, reminders untouched (fail closed); an already-gone event (404) counts as success (that is `cancel_event`'s own rule) [Task 6].
6. Pix paid release without acknowledgement: 409 `retention_ack_required` with the right text for retained / partial / refunded, nothing changed, Google not called; with acknowledgement it proceeds and the outcome is reported [Tasks 4, 7].
7. Release / message by staff of another clinic: 404, nothing deleted, nothing sent [Tasks 6, 9].
8. Message to a Portal patient without e-mail: the chat message is recorded, `email_nudge = "no_email"`, no error; release to the same patient: chat notice, no crash [Tasks 5, 8, 9].
9. WhatsApp patient outside the 24 h window: message needs `notify_outside_window` (else 409 with the `wa.me` link), then a template with a one-line variable and a usage event; release notice is not queued (and says so) unless authorised [Tasks 5, 8, 9].
10. Clinic without an alert address: the row is still marked warned (the agenda turns red), no e-mail, no retry loop, a log line [Task 3].
11. Tenant switch OFF: the cron selects nothing and leaves `warned_at` empty; the staff endpoints still work [Tasks 2, 3, 6].
12. E-mail transport failure: `warned_at` stays set (one attempt only, red state is the fallback), an alarm log line is emitted, the next tick does not resend [Task 3].
13. Burst after an outage (custom + day + hour all due): one e-mail per appointment, all three rows marked [Task 3].
14. Confirmed meanwhile: release refuses with 409 `already_confirmed` unless `release_confirmed=true` [Task 6].
15. A reminder row of another clinic pointing at this appointment (corrupt data) is never selected [Task 2].
16. Appointment without a Google event id (block-like or imported row) is released without calling Google [Task 6].
17. Professional-owned appointment: the delete goes to the professional's calendar, never to the tenant's; an unresolvable owner is 409 and nothing changes [Task 6].

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/secretaria/services/email.py` | modify (append) | `send_confirmation_warning_alert`, template `clinic_message_patient` |
| `src/secretaria/workers/confirmation_warnings.py` | create | selection, atomic claim, per-appointment e-mail, cron entry `process_confirmation_warnings` |
| `src/secretaria/workers/arq_worker.py` | modify | register the cron |
| `src/secretaria/services/payments/deposit_lifecycle.py` | modify (append) | `preview_cancellation_outcome`, `release_warning_text` |
| `src/secretaria/services/staff_patient_message.py` | create | channel-aware staff free text, Portal e-mail nudge, conversation lookup |
| `src/secretaria/services/appointment_release.py` | create | the patient notice after a release |
| `src/secretaria/services/appointment_status.py` | modify | `CANCEL_REASON_UNCONFIRMED`, `reason` kwarg on `log_status_transition` |
| `src/secretaria/schemas/calendar.py` | modify (append) | `AppointmentRelease`, `AppointmentReleaseRead`, `StaffMessageRequest`, `StaffMessageRead` |
| `src/secretaria/api/hub/calendar.py` | modify | `release_appointment`, `message_patient`, `_owning_calendar` |
| `tests/_confirmation_warnings.py` | create | row builder and clinic e-mail helper |
| `tests/test_confirmation_warning_email.py` | create | the two e-mail additions |
| `tests/test_confirmation_warnings_select.py` | create | selection and claim |
| `tests/test_confirmation_warnings_cron.py` | create | the tick, e-mail content, failure modes, registry |
| `tests/test_deposit_release_preview.py` | create | outcome preview |
| `tests/test_staff_patient_message.py` | create | the message service |
| `tests/test_hub_release.py` | create | the release endpoint |
| `tests/test_hub_staff_message.py` | create | the message endpoint |
| `tests/test_build_identity.py` | modify | cron registry (7 -> 8) |
| `docs/CHECKPOINT_lembretes_r4.md` | create | state, what lives where, decisions, pending |

---

## Task 1: The two e-mail additions (`services/email.py`)

**Files:**
- Modify: `src/secretaria/services/email.py` (append `send_confirmation_warning_alert` after `send_cancellation_escalation_alert`; add one entry to `_TEMPLATES`)
- Test: `tests/test_confirmation_warning_email.py`

**Interfaces:**
- Consumes: `_send_sync`, `get_settings`, `asyncio` (already imported in `email.py`), `_TEMPLATES`, `EmailTemplate`.
- Produces:
  - `async def send_confirmation_warning_alert(to_email: str, *, clinic_name: str, warn_kind: str, patient_label: str, professional_name: str, service_name: str, when_text: str, reminder_label: str, agenda_link: str | None) -> bool` — `True` only when the message was handed to SMTP; `False` when `SMTP_HOST` is empty or the send raised (never raises). Subject `[SecretarIA] Consulta sem confirmação — {clinic_name}` (or `[SecretarIA] Lembrete não entregue — {clinic_name}` when `warn_kind == "delivery_failed"`).
  - template id `"clinic_message_patient"` with variables `clinic_name`, `link_line` (used by Task 5).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_confirmation_warning_email.py`:

```python
"""The clinic warning e-mail and the Portal nudge template (TASK-032 R4, spec 4.4)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from secretaria.services import email as email_module  # noqa: E402

KWARGS = dict(
    clinic_name="Clínica Olhar",
    warn_kind="unconfirmed",
    patient_label="Maria Souza",
    professional_name="Dra. Ana",
    service_name="Consulta",
    when_text="08/10/2026 às 09:00",
    reminder_label="lembrete de 1 dia antes",
    agenda_link="https://app.exemplo/agenda?consulta=abc",
)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    sent: list[tuple] = []
    monkeypatch.setattr(
        email_module, "get_settings", lambda: SimpleNamespace(SMTP_HOST="smtp.example.com")
    )
    monkeypatch.setattr(email_module, "_send_sync", lambda *args: sent.append(args))
    return sent


async def test_unconfirmed_warning_names_everything_the_clinic_needs(smtp):
    ok = await email_module.send_confirmation_warning_alert("contato@clinica.example", **KWARGS)

    assert ok is True
    (to, subject, body) = smtp[0]
    assert to == "contato@clinica.example"
    assert subject == "[SecretarIA] Consulta sem confirmação — Clínica Olhar"
    for needle in (
        "Maria Souza",
        "Dra. Ana",
        "Consulta",
        "08/10/2026 às 09:00",
        "lembrete de 1 dia antes",
        "https://app.exemplo/agenda?consulta=abc",
        "liberar o horário",
    ):
        assert needle in body
    assert "None" not in body


async def test_delivery_failed_variant_says_the_reminder_did_not_arrive(smtp):
    await email_module.send_confirmation_warning_alert(
        "contato@clinica.example", **{**KWARGS, "warn_kind": "delivery_failed"}
    )

    (_to, subject, body) = smtp[0]
    assert subject == "[SecretarIA] Lembrete não entregue — Clínica Olhar"
    assert "não foi possível entregar" in body.lower()


async def test_without_an_agenda_link_the_body_has_no_dangling_label(smtp):
    await email_module.send_confirmation_warning_alert(
        "contato@clinica.example", **{**KWARGS, "agenda_link": None}
    )

    body = smtp[0][2]
    assert "None" not in body and "agenda?consulta" not in body


async def test_returns_false_and_never_raises_when_smtp_is_off(monkeypatch):
    monkeypatch.setattr(
        email_module, "get_settings", lambda: SimpleNamespace(SMTP_HOST="")
    )

    assert await email_module.send_confirmation_warning_alert("a@b.c", **KWARGS) is False


async def test_returns_false_and_never_raises_when_the_send_breaks(monkeypatch):
    monkeypatch.setattr(
        email_module, "get_settings", lambda: SimpleNamespace(SMTP_HOST="smtp.example.com")
    )

    def _boom(*_args):
        raise OSError("connection refused")

    monkeypatch.setattr(email_module, "_send_sync", _boom)

    assert await email_module.send_confirmation_warning_alert("a@b.c", **KWARGS) is False


def test_portal_nudge_template_is_registered_and_renders_without_the_message_text():
    assert email_module.is_known_template("clinic_message_patient")
    tpl = email_module._TEMPLATES["clinic_message_patient"]
    variables = {"clinic_name": "Clínica Olhar", "link_line": "https://portal.exemplo/c"}

    subject = tpl.subject.format_map(variables)
    body = tpl.body.format_map(variables)

    assert "Clínica Olhar" in subject
    assert "https://portal.exemplo/c" in body
    assert "{" not in body
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_confirmation_warning_email.py -q`
Expected: FAIL — `AttributeError: module 'secretaria.services.email' has no attribute 'send_confirmation_warning_alert'`.

- [ ] **Step 3: Implement**

In `src/secretaria/services/email.py`, right after the whole `send_cancellation_escalation_alert` function (before the `# WHAT each gap code means` comment), insert:

```python
_WARNING_SUBJECT = {
    "unconfirmed": "[SecretarIA] Consulta sem confirmação — {clinic_name}",
    "delivery_failed": "[SecretarIA] Lembrete não entregue — {clinic_name}",
}


async def send_confirmation_warning_alert(
    to_email: str,
    *,
    clinic_name: str,
    warn_kind: str,
    patient_label: str,
    professional_name: str,
    service_name: str,
    when_text: str,
    reminder_label: str,
    agenda_link: str | None,
) -> bool:
    """Email the clinic that a reminded patient has not confirmed (TASK-032 R4).

    Two variants, chosen by `warn_kind` (the constants of
    models/appointment_reminder.py): `unconfirmed` - the reminder went out and
    nobody answered by the deadline; `delivery_failed` - the reminder itself
    could not be delivered, so the patient may not even know. Both tell the
    clinic what it can do in the agenda: write to the patient, mark the
    appointment confirmed, or free the slot.

    No clinical detail on purpose (no requirements, insurance or anamnese):
    name, doctor, service, date/time, which reminder, a link. The name and the
    link are PII and go in the BODY only, never into a log line - the same rule
    `send_cancellation_escalation_alert` follows for its `wa.me` link.

    Returns True only when the message was handed to SMTP, so the caller can log
    an alarm without a second attempt; False when `SMTP_HOST` is empty or the
    send raised. NEVER raises: like the alerts above, it is the end of the line.
    """
    settings = get_settings()
    if not settings.SMTP_HOST:
        return False

    subject = _WARNING_SUBJECT.get(warn_kind, _WARNING_SUBJECT["unconfirmed"]).format(
        clinic_name=clinic_name
    )
    if warn_kind == "delivery_failed":
        headline = (
            f"Não foi possível entregar o {reminder_label} da consulta de {patient_label} "
            f"com {professional_name} ({service_name}) marcada para {when_text}. "
            f"O paciente pode nem ter visto o aviso."
        )
    else:
        headline = (
            f"{patient_label} ainda não confirmou a consulta com {professional_name} "
            f"({service_name}) marcada para {when_text}.\n\n"
            f"O {reminder_label} já foi enviado e não houve resposta."
        )
    link_block = f"Abrir na agenda:\n{agenda_link}\n\n" if agenda_link else ""
    body = (
        f"Olá,\n\n"
        f"{headline}\n\n"
        f"{link_block}"
        f"Na agenda você pode enviar uma mensagem ao paciente, marcar a consulta como "
        f"confirmada ou liberar o horário.\n\n"
        f"— Equipe SecretarIA"
    )

    try:
        await asyncio.to_thread(_send_sync, to_email, subject, body)
    except Exception as exc:
        logger.warning(
            "confirmation_warning_email_failed", error_type=type(exc).__name__, clinic=clinic_name
        )
        return False
    logger.info("confirmation_warning_email_sent", clinic=clinic_name, warn_kind=warn_kind)
    return True
```

In `_TEMPLATES`, right after the `"appointment_reminder_patient"` entry R2 added (if it is missing R2 is not merged — stop), add:

```python
    # TASK-032 R4: a Portal patient got a message typed by the clinic. The text
    # itself is NEVER put in the e-mail (it may carry clinical content): the mail
    # only says there is something to read and links to the conversation.
    "clinic_message_patient": EmailTemplate(
        subject="Nova mensagem de {clinic_name}",
        body=(
            "Olá!\n\n"
            "Você recebeu uma nova mensagem da {clinic_name}.\n\n"
            "Para ler e responder, abra a sua conversa:\n"
            "{link_line}\n\n"
            "— {clinic_name}"
        ),
    ),
```

- [ ] **Step 4: Run them to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_confirmation_warning_email.py tests/test_email_transactional.py -q`
Expected: all pass (the existing e-mail tests unchanged).

- [ ] **Step 5: Lint, diff check, commit**

Run: `uvx ruff format tests/test_confirmation_warning_email.py && uvx ruff check --fix tests/test_confirmation_warning_email.py`; `uvx ruff check src/secretaria/services/email.py`; `git diff --stat` (email.py: only added lines).

```bash
git add src/secretaria/services/email.py tests/test_confirmation_warning_email.py
git commit -m "feat(reminders): clinic warning e-mail and Portal nudge template (TASK-032 R4)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 2: Warning selection and the atomic claim (`workers/confirmation_warnings.py`, part 1)

**Files:**
- Create: `src/secretaria/workers/confirmation_warnings.py`
- Create: `tests/_confirmation_warnings.py`
- Test: `tests/test_confirmation_warnings_select.py`

**Interfaces:**
- Consumes: R1 `AppointmentReminder` and constants; `Appointment`, `LIVE_APPOINTMENT_STATUSES`, `Tenant`.
- Produces:
  - `MAX_WARNINGS_PER_APPOINTMENT: int = 3`, `BATCH_SIZE: int = 200`
  - `@dataclass(frozen=True) WarningCandidate(reminder_id: UUID, appointment_id: UUID, tenant_id: UUID, kind: str, warn_kind: str, start_version: datetime)`
  - `async due_candidates(session: AsyncSession, now: datetime, *, limit: int = BATCH_SIZE) -> list[WarningCandidate]` — read-only, oldest `warn_due_at` first.
  - `async claim_warning(session: AsyncSession, candidate: WarningCandidate, now: datetime) -> bool` — `True` iff THIS call set `warned_at`; re-checks every guard inside the `UPDATE` itself; does not commit (the caller owns the transaction).
  - test helpers `add_warnable_row(db, world, *, kind="day", status="sent", warn_kind="unconfirmed", warn_due_at=<NOW - 1 min>, warned_at=None, appointment_start_at=None, tenant_id=None) -> UUID`, `set_contact_email(db, tenant_id, email)`.

- [ ] **Step 1: Write the test helpers**

Create `tests/_confirmation_warnings.py`:

```python
"""Rows and clinic setup shared by the TASK-032 R4 warning tests.

Builds the row exactly as R2's engine leaves it after a send (`status='sent'`,
`warn_kind='unconfirmed'`, `warn_due_at` already past), so the tests exercise
only R4's selection, claim and e-mail.
"""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from secretaria.models import AppointmentReminder, Tenant
from tests._reminders_v2 import NOW, World

_DEFAULT = object()


async def add_warnable_row(
    db,
    world: World,
    *,
    kind: str = "day",
    status: str = "sent",
    warn_kind: str | None = "unconfirmed",
    warn_due_at=_DEFAULT,
    warned_at: datetime | None = None,
    appointment_start_at: datetime | None = None,
    tenant_id: UUID | None = None,
) -> UUID:
    due = NOW - timedelta(hours=3)
    async with db() as session:
        row = AppointmentReminder(
            id=uuid4(),
            tenant_id=tenant_id or world.tenant.id,
            appointment_id=world.appointment.id,
            patient_id=world.patient.id,
            kind=kind,
            appointment_start_at=appointment_start_at or world.start_at,
            due_at=due,
            status=status,
            channel="whatsapp",
            with_prompt=True,
            attempts=1,
            sent_at=due if status == "sent" else None,
            warn_due_at=NOW - timedelta(minutes=1) if warn_due_at is _DEFAULT else warn_due_at,
            warned_at=warned_at,
            warn_kind=warn_kind,
        )
        session.add(row)
        await session.commit()
        return row.id


async def set_contact_email(db, tenant_id: UUID, email: str | None) -> None:
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.contact_email = email
        await session.commit()
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_confirmation_warnings_select.py`:

```python
"""Which rows may be warned about, and the atomic claim (TASK-032 R4, spec 4.4)."""

from datetime import timedelta

import pytest
from sqlalchemy import update

from secretaria.models import Appointment, AppointmentStatus
from secretaria.workers import confirmation_warnings as cw
from tests._confirmation_warnings import add_warnable_row
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import NOW, get_reminder, seed_world


async def _candidates(db, now=NOW):  # noqa: F811
    async with db() as session:
        return await cw.due_candidates(session, now)


async def _claim(db, candidate, now=NOW) -> bool:  # noqa: F811
    async with db() as session:
        async with session.begin():
            return await cw.claim_warning(session, candidate, now)


async def _set_appointment(db, appointment_id, **fields):  # noqa: F811
    async with db() as session:
        await session.execute(
            update(Appointment).where(Appointment.id == appointment_id).values(**fields)
        )
        await session.commit()


async def test_a_sent_unanswered_row_past_its_deadline_is_a_candidate(db):  # noqa: F811
    world = await seed_world(db)
    rid = await add_warnable_row(db, world)

    found = await _candidates(db)

    assert [c.reminder_id for c in found] == [rid]
    assert found[0].warn_kind == "unconfirmed" and found[0].kind == "day"
    assert found[0].appointment_id == world.appointment.id


async def test_the_failed_row_is_a_candidate_too(db):  # noqa: F811
    world = await seed_world(db)
    await add_warnable_row(db, world, status="failed", warn_kind="delivery_failed")

    found = await _candidates(db)

    assert [c.warn_kind for c in found] == ["delivery_failed"]


@pytest.mark.parametrize("status", ["pending", "sending", "skipped", "cancelled"])
async def test_a_row_that_was_never_sent_is_not_a_candidate(db, status):  # noqa: F811
    world = await seed_world(db)
    await add_warnable_row(db, world, status=status)

    assert await _candidates(db) == []


async def test_the_deadline_must_have_passed_and_exist(db):  # noqa: F811
    world = await seed_world(db)
    await add_warnable_row(db, world, kind="day", warn_due_at=NOW + timedelta(minutes=5))
    await add_warnable_row(db, world, kind="hour", warn_due_at=None, warn_kind=None)

    assert await _candidates(db) == []


async def test_the_chat_opening_row_is_never_warned_about(db):  # noqa: F811
    world = await seed_world(db)
    await add_warnable_row(db, world, kind="chat")

    assert await _candidates(db) == []


async def test_a_row_of_an_older_start_is_not_a_candidate(db):  # noqa: F811
    world = await seed_world(db)
    await add_warnable_row(
        db, world, appointment_start_at=world.start_at - timedelta(days=1)
    )

    assert await _candidates(db) == []


async def test_a_confirmed_appointment_is_never_warned_about(db):  # noqa: F811
    world = await seed_world(db, confirmation_count=1)
    await add_warnable_row(db, world)

    assert await _candidates(db) == []


@pytest.mark.parametrize(
    "status",
    [AppointmentStatus.CANCELLED, AppointmentStatus.ATTENDED, AppointmentStatus.NO_SHOW],
)
async def test_a_closed_appointment_is_never_warned_about(db, status):  # noqa: F811
    world = await seed_world(db, status=status)
    await add_warnable_row(db, world)

    assert await _candidates(db) == []


async def test_a_started_appointment_is_never_warned_about(db):  # noqa: F811
    world = await seed_world(db, start_at=NOW - timedelta(minutes=10))
    await add_warnable_row(db, world)

    assert await _candidates(db) == []


async def test_a_clinic_with_the_switch_off_is_never_warned_about(db):  # noqa: F811
    world = await seed_world(db, v2=False)
    rid = await add_warnable_row(db, world)

    assert await _candidates(db) == []
    assert (await get_reminder(db, rid)).warned_at is None


async def test_a_row_of_another_clinic_pointing_here_is_never_selected(db):  # noqa: F811
    mine = await seed_world(db)
    theirs = await seed_world(db)
    await add_warnable_row(db, mine, tenant_id=theirs.tenant.id)

    assert await _candidates(db) == []


async def test_the_claim_sets_warned_at_once(db):  # noqa: F811
    world = await seed_world(db)
    rid = await add_warnable_row(db, world)
    [candidate] = await _candidates(db)

    first = await _claim(db, candidate)
    second = await _claim(db, candidate)  # a second worker holding the same candidate

    assert (first, second) == (True, False)
    assert (await get_reminder(db, rid)).warned_at is not None
    assert await _candidates(db) == []


async def test_a_confirmation_between_selection_and_claim_defeats_the_claim(db):  # noqa: F811
    world = await seed_world(db)
    rid = await add_warnable_row(db, world)
    [candidate] = await _candidates(db)

    await _set_appointment(
        db, world.appointment.id, confirmation_count=1, status=AppointmentStatus.CONFIRMED
    )

    assert await _claim(db, candidate) is False
    assert (await get_reminder(db, rid)).warned_at is None


@pytest.mark.parametrize(
    "status", [AppointmentStatus.CANCELLED, AppointmentStatus.ATTENDED]
)
async def test_a_close_between_selection_and_claim_defeats_the_claim(db, status):  # noqa: F811
    world = await seed_world(db)
    rid = await add_warnable_row(db, world)
    [candidate] = await _candidates(db)

    await _set_appointment(db, world.appointment.id, status=status)

    assert await _claim(db, candidate) is False
    assert (await get_reminder(db, rid)).warned_at is None


async def test_the_cap_of_warnings_per_appointment(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cw, "MAX_WARNINGS_PER_APPOINTMENT", 1)
    world = await seed_world(db)
    await add_warnable_row(db, world, kind="day", warn_due_at=NOW - timedelta(minutes=30))
    await add_warnable_row(db, world, kind="hour", warn_due_at=NOW - timedelta(minutes=1))
    first, second = await _candidates(db)

    assert await _claim(db, first) is True
    assert await _claim(db, second) is False  # the cap, not a race


async def test_the_default_cap_is_three(db):  # noqa: F811
    assert cw.MAX_WARNINGS_PER_APPOINTMENT == 3
```

- [ ] **Step 3: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_confirmation_warnings_select.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'secretaria.workers.confirmation_warnings'`.

- [ ] **Step 4: Implement selection and claim**

Create `src/secretaria/workers/confirmation_warnings.py`:

```python
"""Clinic warnings for unconfirmed appointments (TASK-032 R4, spec 4.4).

R2's reminder engine leaves, on every reminder row it sent (or gave up on), a
`warn_due_at` and a `warn_kind`. This module is the other half: when that
deadline passes and the patient still has not confirmed, it warns the clinic.

The row's `warned_at` is the single marker for BOTH things the spec asks for:
it makes the warning fire once (the claim below is an atomic conditional
UPDATE, so two workers or two overlapping ticks cannot both win), and it is
what R1's `display_state` reads to turn the appointment red in the agenda. That
is why the claim happens BEFORE the e-mail and is never rolled back: a clinic
with no alert address, or a transport hiccup, must still see the red state; the
e-mail is best-effort on top (see `_notify_clinic`).

Nothing here sends anything to the patient and nothing releases a slot.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import Appointment, AppointmentReminder, Tenant
from secretaria.models.appointment import LIVE_APPOINTMENT_STATUSES
from secretaria.models.appointment_reminder import (
    REMINDER_KIND_CHAT,
    REMINDER_STATUS_FAILED,
    REMINDER_STATUS_SENT,
    REMINDER_WARN_DELIVERY_FAILED,
    REMINDER_WARN_UNCONFIRMED,
)

# Spec 4.4: at most three warnings per appointment (custom, day, hour).
MAX_WARNINGS_PER_APPOINTMENT = 3
# Rows examined per one-minute tick, so a tick never outlives the next one.
BATCH_SIZE = 200

# R2 contract: only a row that was really sent (or definitively failed) is
# warned about; a `pending` row keeps R1's `warn_due_at` but never went out.
_WARNABLE_ROW_STATUSES = (REMINDER_STATUS_SENT, REMINDER_STATUS_FAILED)
_WARN_KINDS = (REMINDER_WARN_UNCONFIRMED, REMINDER_WARN_DELIVERY_FAILED)


@dataclass(frozen=True)
class WarningCandidate:
    """One reminder row whose warning is due, with what the claim must re-check."""

    reminder_id: UUID
    appointment_id: UUID
    tenant_id: UUID
    kind: str
    warn_kind: str
    # The appointment start this row was planned for (R1's version); the cap is
    # counted per version so a reschedule starts a fresh cycle.
    start_version: datetime


def _appointment_conditions(now: datetime) -> list:
    """The appointment must still need the warning: live, unconfirmed, upcoming."""
    return [
        Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
        Appointment.confirmation_count == 0,
        Appointment.start_at.is_not(None),
        Appointment.start_at > now,
    ]


async def due_candidates(
    session: AsyncSession, now: datetime, *, limit: int = BATCH_SIZE
) -> list[WarningCandidate]:
    """Rows whose warning is due. Read-only; the claim re-checks everything."""
    rows = await session.execute(
        select(
            AppointmentReminder.id,
            AppointmentReminder.appointment_id,
            AppointmentReminder.tenant_id,
            AppointmentReminder.kind,
            AppointmentReminder.warn_kind,
            AppointmentReminder.appointment_start_at,
        )
        .join(Appointment, Appointment.id == AppointmentReminder.appointment_id)
        .join(Tenant, Tenant.id == Appointment.tenant_id)
        .where(
            # A reminder row of another clinic pointing at this appointment is
            # corrupt data; never let it reach a clinic's inbox.
            AppointmentReminder.tenant_id == Appointment.tenant_id,
            AppointmentReminder.status.in_(_WARNABLE_ROW_STATUSES),
            AppointmentReminder.kind != REMINDER_KIND_CHAT,
            AppointmentReminder.warn_kind.in_(_WARN_KINDS),
            AppointmentReminder.warn_due_at.is_not(None),
            AppointmentReminder.warn_due_at <= now,
            AppointmentReminder.warned_at.is_(None),
            # Only the row of the appointment's CURRENT start: a reschedule
            # cancelled the older ones.
            AppointmentReminder.appointment_start_at == Appointment.start_at,
            Tenant.reminders_v2_enabled.is_(True),
            *_appointment_conditions(now),
        )
        .order_by(AppointmentReminder.warn_due_at, AppointmentReminder.id)
        .limit(limit)
    )
    return [
        WarningCandidate(
            reminder_id=row.id,
            appointment_id=row.appointment_id,
            tenant_id=row.tenant_id,
            kind=row.kind,
            warn_kind=row.warn_kind,
            start_version=row.appointment_start_at,
        )
        for row in rows.all()
    ]


async def claim_warning(session: AsyncSession, candidate: WarningCandidate, now: datetime) -> bool:
    """Set `warned_at` iff nobody did and the appointment still needs the warning.

    One conditional UPDATE: the row must still be unwarned and sent/failed, and
    the appointment must still be live, unconfirmed and upcoming - so a patient
    who confirms (or a clinic that cancels/releases) between the selection and
    this call defeats the claim. Returns True only for the call that won. The
    caller owns the transaction.
    """
    already = await session.scalar(
        select(func.count())
        .select_from(AppointmentReminder)
        .where(
            AppointmentReminder.appointment_id == candidate.appointment_id,
            AppointmentReminder.tenant_id == candidate.tenant_id,
            AppointmentReminder.appointment_start_at == candidate.start_version,
            AppointmentReminder.warned_at.is_not(None),
        )
    )
    if (already or 0) >= MAX_WARNINGS_PER_APPOINTMENT:
        return False
    result = await session.execute(
        update(AppointmentReminder)
        .where(
            AppointmentReminder.id == candidate.reminder_id,
            AppointmentReminder.tenant_id == candidate.tenant_id,
            AppointmentReminder.warned_at.is_(None),
            AppointmentReminder.status.in_(_WARNABLE_ROW_STATUSES),
            AppointmentReminder.appointment_id.in_(
                select(Appointment.id).where(
                    Appointment.id == candidate.appointment_id,
                    Appointment.tenant_id == candidate.tenant_id,
                    *_appointment_conditions(now),
                )
            ),
        )
        .values(warned_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1
```

- [ ] **Step 5: Run them to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_confirmation_warnings_select.py -q`
Expected: `22 passed` (parametrized cases included). If an equality on `appointment_start_at` fails only under SQLite, compare how R2's `reconcile_missing_reminders` does the same join and mirror it.

- [ ] **Step 6: Lint and commit**

Run: `uvx ruff format src/secretaria/workers/confirmation_warnings.py tests/_confirmation_warnings.py tests/test_confirmation_warnings_select.py && uvx ruff check --fix src/secretaria/workers/confirmation_warnings.py tests/_confirmation_warnings.py tests/test_confirmation_warnings_select.py`

```bash
git add src/secretaria/workers/confirmation_warnings.py tests/_confirmation_warnings.py tests/test_confirmation_warnings_select.py
git commit -m "feat(reminders): select and atomically claim due clinic warnings (TASK-032 R4)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 3: The tick — one e-mail per appointment, failure modes, cron registration

**Files:**
- Modify: `src/secretaria/workers/confirmation_warnings.py` (imports, then append)
- Modify: `src/secretaria/workers/arq_worker.py` (import, `cron_jobs`)
- Modify: `tests/test_build_identity.py` (registry)
- Test: `tests/test_confirmation_warnings_cron.py`

**Interfaces:**
- Consumes: Task 1 `send_confirmation_warning_alert`; Task 2 `due_candidates`, `claim_warning`, `WarningCandidate`; R2's `core_database.async_session_factory` convention.
- Produces:
  - `@dataclass WarningTickReport(candidates=0, claimed=0, emailed=0, no_address=0, email_failed=0, errors=0)`
  - `async run_warning_tick(*, now: datetime) -> WarningTickReport`
  - `async process_confirmation_warnings(ctx: dict) -> None` — the arq cron, every minute, cron name `cron:process_confirmation_warnings`.
  - `REMINDER_LABELS: dict[str, str]` (`custom` -> `lembrete antecipado`, `day` -> `lembrete de 1 dia antes`, `hour` -> `lembrete de 1 hora antes`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_confirmation_warnings_cron.py`:

```python
"""The warning tick: e-mail content, once-only, and the failure modes (TASK-032 R4, spec 4.4)."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from secretaria.core import database as core_database
from secretaria.models import Appointment, AppointmentStatus
from secretaria.workers import confirmation_warnings as cw
from tests._confirmation_warnings import add_warnable_row, set_contact_email
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import NOW, get_reminder, seed_world

CLINIC_EMAIL = "contato@clinica.example"
AGENDA = "https://app.exemplo/agenda"


class _AlertSpy:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, to_email: str, **kwargs) -> bool:
        self.calls.append((to_email, kwargs))
        return self.result


@pytest.fixture(autouse=True)
def _wire(db, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(cw, "get_settings", lambda: SimpleNamespace(DOCTOR_AGENDA_URL=AGENDA))


@pytest.fixture
def alert(monkeypatch: pytest.MonkeyPatch) -> _AlertSpy:
    spy = _AlertSpy()
    monkeypatch.setattr(cw, "send_confirmation_warning_alert", spy)
    return spy


async def _world(db, **kwargs):  # noqa: F811
    world = await seed_world(db, professional_name="Dra. Ana", **kwargs)
    await set_contact_email(db, world.tenant.id, CLINIC_EMAIL)
    return world


async def test_the_warning_names_what_the_clinic_needs_and_nothing_clinical(db, alert):  # noqa: F811
    world = await _world(db, requirements=["jejum de 8 horas"])
    await add_warnable_row(db, world, kind="day")

    report = await cw.run_warning_tick(now=NOW)

    assert (report.candidates, report.claimed, report.emailed) == (1, 1, 1)
    [(to, kwargs)] = alert.calls
    assert to == CLINIC_EMAIL
    assert kwargs["clinic_name"] == "Clínica Olhar"
    assert kwargs["warn_kind"] == "unconfirmed"
    assert kwargs["patient_label"] == "Maria"
    assert kwargs["professional_name"] == "Dra. Ana"
    assert kwargs["service_name"] == "Consulta"
    assert kwargs["when_text"] == "08/10/2026 às 09:00"  # 12:00 UTC in America/Sao_Paulo
    assert kwargs["reminder_label"] == "lembrete de 1 dia antes"
    assert kwargs["agenda_link"] == f"{AGENDA}?consulta={world.appointment.id}"
    assert "jejum" not in repr(kwargs)  # requirements are clinical-adjacent: never mailed


async def test_the_delivery_failed_variant_is_passed_through(db, alert):  # noqa: F811
    world = await _world(db)
    await add_warnable_row(db, world, kind="hour", status="failed", warn_kind="delivery_failed")

    await cw.run_warning_tick(now=NOW)

    assert alert.calls[0][1]["warn_kind"] == "delivery_failed"
    assert alert.calls[0][1]["reminder_label"] == "lembrete de 1 hora antes"


async def test_a_booking_for_someone_else_names_the_attendee_and_the_holder(db, alert):  # noqa: F811
    world = await _world(db, attendee_name="João")
    await add_warnable_row(db, world)

    await cw.run_warning_tick(now=NOW)

    assert alert.calls[0][1]["patient_label"] == "João (consulta marcada por Maria)"


async def test_a_row_warns_once_even_over_several_ticks(db, alert):  # noqa: F811
    world = await _world(db)
    rid = await add_warnable_row(db, world)

    await cw.run_warning_tick(now=NOW)
    await cw.run_warning_tick(now=NOW + timedelta(minutes=1))
    await cw.run_warning_tick(now=NOW + timedelta(minutes=2))

    assert len(alert.calls) == 1
    assert (await get_reminder(db, rid)).warned_at is not None


async def test_a_confirmation_before_the_tick_means_no_warning(db, alert):  # noqa: F811
    world = await _world(db)
    rid = await add_warnable_row(db, world)
    async with db() as session:
        await session.execute(
            update(Appointment)
            .where(Appointment.id == world.appointment.id)
            .values(confirmation_count=1, status=AppointmentStatus.CONFIRMED)
        )
        await session.commit()

    report = await cw.run_warning_tick(now=NOW)

    assert report.candidates == 0 and alert.calls == []
    assert (await get_reminder(db, rid)).warned_at is None


async def test_a_clinic_with_the_switch_off_gets_nothing(db, alert):  # noqa: F811
    world = await _world(db, v2=False)
    rid = await add_warnable_row(db, world)

    report = await cw.run_warning_tick(now=NOW)

    assert report.candidates == 0 and alert.calls == []
    assert (await get_reminder(db, rid)).warned_at is None


async def test_without_an_alert_address_the_row_is_still_marked_but_nothing_is_sent(db, alert):  # noqa: F811
    world = await _world(db)
    await set_contact_email(db, world.tenant.id, "   ")
    rid = await add_warnable_row(db, world)

    report = await cw.run_warning_tick(now=NOW)
    again = await cw.run_warning_tick(now=NOW + timedelta(minutes=1))

    assert alert.calls == []
    assert (report.claimed, report.no_address) == (1, 1)
    assert again.candidates == 0  # no retry loop
    assert (await get_reminder(db, rid)).warned_at is not None  # the agenda still turns red


async def test_a_failed_send_is_not_retried_and_keeps_the_red_state(db, alert):  # noqa: F811
    alert.result = False
    world = await _world(db)
    rid = await add_warnable_row(db, world)

    report = await cw.run_warning_tick(now=NOW)
    again = await cw.run_warning_tick(now=NOW + timedelta(minutes=1))

    assert (report.emailed, report.email_failed) == (0, 1)
    assert again.candidates == 0 and len(alert.calls) == 1
    assert (await get_reminder(db, rid)).warned_at is not None


async def test_a_burst_after_an_outage_sends_one_email_for_the_appointment(db, alert):  # noqa: F811
    world = await _world(db)
    ids = [
        await add_warnable_row(
            db, world, kind="custom", warn_due_at=NOW - timedelta(hours=40)
        ),
        await add_warnable_row(db, world, kind="day", warn_due_at=NOW - timedelta(hours=20)),
        await add_warnable_row(db, world, kind="hour", warn_due_at=NOW - timedelta(minutes=2)),
    ]

    report = await cw.run_warning_tick(now=NOW)

    assert (report.claimed, report.emailed) == (3, 1)
    assert alert.calls[0][1]["reminder_label"] == "lembrete de 1 hora antes"  # the latest
    for rid in ids:
        assert (await get_reminder(db, rid)).warned_at is not None


async def test_an_error_on_one_appointment_does_not_stop_the_others(db, alert, monkeypatch):  # noqa: F811
    first = await _world(db)
    second = await _world(db)
    await add_warnable_row(db, first)
    await add_warnable_row(db, second)
    real = cw.claim_warning
    seen = {"n": 0}

    async def _flaky(session, candidate, now):
        seen["n"] += 1
        if seen["n"] == 1:
            raise RuntimeError("db hiccup")
        return await real(session, candidate, now)

    monkeypatch.setattr(cw, "claim_warning", _flaky)

    report = await cw.run_warning_tick(now=NOW)

    assert report.errors == 1 and report.emailed == 1


def test_the_agenda_link_handles_an_existing_query_and_an_unset_url(monkeypatch):
    monkeypatch.setattr(cw, "get_settings", lambda: SimpleNamespace(DOCTOR_AGENDA_URL=""))
    assert cw._agenda_link("abc") is None
    monkeypatch.setattr(
        cw, "get_settings", lambda: SimpleNamespace(DOCTOR_AGENDA_URL="https://x.y/agenda?v=1")
    )
    assert cw._agenda_link("abc") == "https://x.y/agenda?v=1&consulta=abc"


async def test_the_cron_entry_point_runs_a_tick(db, alert, monkeypatch):  # noqa: F811
    world = await _world(db)
    await add_warnable_row(db, world)
    real_now = NOW + timedelta(seconds=1)
    monkeypatch.setattr(
        cw, "datetime", SimpleNamespace(now=lambda tz=None: real_now), raising=True
    )

    await cw.process_confirmation_warnings({})

    assert len(alert.calls) == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_confirmation_warnings_cron.py -q`
Expected: FAIL — `AttributeError: module 'secretaria.workers.confirmation_warnings' has no attribute 'run_warning_tick'`.

- [ ] **Step 3: Implement the tick**

In `src/secretaria/workers/confirmation_warnings.py` replace the import block (from `from dataclasses import dataclass` through the end of the `secretaria.models.appointment_reminder` import) with:

```python
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.core import database as core_database
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    AppointmentReminder,
    Patient,
    Professional,
    Tenant,
)
from secretaria.models.appointment import LIVE_APPOINTMENT_STATUSES
from secretaria.models.appointment_reminder import (
    REMINDER_KIND_CHAT,
    REMINDER_STATUS_FAILED,
    REMINDER_STATUS_SENT,
    REMINDER_WARN_DELIVERY_FAILED,
    REMINDER_WARN_UNCONFIRMED,
)
from secretaria.services.email import send_confirmation_warning_alert

logger = get_logger(__name__)
```

Then append to the end of the file:

```python


# Which reminder the clinic is told about, in the clinic's words.
REMINDER_LABELS = {
    "custom": "lembrete antecipado",
    "day": "lembrete de 1 dia antes",
    "hour": "lembrete de 1 hora antes",
}
_NO_PROFESSIONAL = "o profissional da clínica"
_DEFAULT_TIMEZONE = "America/Sao_Paulo"


@dataclass
class WarningTickReport:
    candidates: int = 0
    claimed: int = 0
    emailed: int = 0
    no_address: int = 0
    email_failed: int = 0
    errors: int = 0


def _when_text(start_at: datetime | None, timezone: str | None) -> str:
    """`DD/MM/AAAA às HH:MM` in the CLINIC's zone (SQLite hands back naive UTC)."""
    if start_at is None:
        return "horário a confirmar"
    try:
        tz = ZoneInfo(timezone or _DEFAULT_TIMEZONE)
    except Exception:
        tz = ZoneInfo(_DEFAULT_TIMEZONE)
    aware = start_at if start_at.tzinfo is not None else start_at.replace(tzinfo=UTC)
    return aware.astimezone(tz).strftime("%d/%m/%Y às %H:%M")


def _agenda_link(appointment_id) -> str | None:
    """The agenda URL the deployment configured + `consulta=<id>`; None when unset.

    `DOCTOR_AGENDA_URL` is a full URL on purpose (the two frontends serve the
    agenda at different paths); a mail with no link is fine, a broken one is not.
    """
    base = (get_settings().DOCTOR_AGENDA_URL or "").strip()
    if not base:
        return None
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}consulta={appointment_id}"


def _patient_label(appointment: Appointment, patient: Patient | None) -> str:
    holder = ((patient.name if patient else None) or "").strip()
    attendee = (appointment.attendee_name or "").strip()
    if attendee and holder:
        return f"{attendee} (consulta marcada por {holder})"
    return attendee or holder or "O paciente"


async def _notify_clinic(candidate: WarningCandidate, report: WarningTickReport) -> None:
    """E-mail the clinic once for an appointment whose rows were just claimed.

    Best-effort by design: `warned_at` is already set (the agenda is red), so a
    missing address or a transport failure is logged and counted, never retried
    - a retry loop on a dead SMTP would mail the clinic a burst when it recovers.
    """
    async with core_database.async_session_factory() as session:
        tenant = await session.get(Tenant, candidate.tenant_id)
        appointment = await session.scalar(
            select(Appointment).where(
                Appointment.id == candidate.appointment_id,
                Appointment.tenant_id == candidate.tenant_id,
            )
        )
        if tenant is None or appointment is None:
            return
        patient = None
        if appointment.patient_id is not None:
            patient = await session.scalar(
                select(Patient).where(
                    Patient.id == appointment.patient_id, Patient.tenant_id == tenant.id
                )
            )
        professional_name = None
        if appointment.professional_id is not None:
            professional_name = await session.scalar(
                select(Professional.name).where(
                    Professional.id == appointment.professional_id,
                    Professional.tenant_id == tenant.id,
                )
            )

    to_email = (tenant.contact_email or "").strip()
    if not to_email:
        report.no_address += 1
        logger.info(
            "confirmation_warning_no_address",
            tenant_id=str(tenant.id),
            appointment_id=str(appointment.id),
        )
        return

    sent = await send_confirmation_warning_alert(
        to_email,
        clinic_name=tenant.clinic_name,
        warn_kind=candidate.warn_kind,
        patient_label=_patient_label(appointment, patient),
        professional_name=professional_name or _NO_PROFESSIONAL,
        service_name=appointment.appointment_type or "Consulta",
        when_text=_when_text(appointment.start_at, tenant.timezone),
        reminder_label=REMINDER_LABELS.get(candidate.kind, "lembrete"),
        agenda_link=_agenda_link(appointment.id),
    )
    if sent:
        report.emailed += 1
        return
    report.email_failed += 1
    logger.error(
        "confirmation_warning_undelivered",
        alarm="confirmation_warning_undelivered",
        tenant_id=str(tenant.id),
        appointment_id=str(appointment.id),
    )


async def run_warning_tick(*, now: datetime) -> WarningTickReport:
    """Warn the clinic about every appointment whose deadline just passed.

    Claims are grouped per appointment inside ONE transaction, then ONE e-mail
    is sent for the group (the latest reminder), so a cron outage that leaves
    custom + day + hour all due does not mail the clinic three times. An error on
    one appointment is logged and counted and never stops the others.
    """
    report = WarningTickReport()
    async with core_database.async_session_factory() as session:
        candidates = await due_candidates(session, now)
    report.candidates = len(candidates)

    groups: dict[UUID, list[WarningCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.appointment_id, []).append(candidate)

    for appointment_id, group in groups.items():
        try:
            claimed: list[WarningCandidate] = []
            async with core_database.async_session_factory() as session:
                async with session.begin():
                    for candidate in group:
                        if await claim_warning(session, candidate, now):
                            claimed.append(candidate)
            report.claimed += len(claimed)
            if claimed:
                await _notify_clinic(claimed[-1], report)
        except Exception as exc:
            report.errors += 1
            logger.warning(
                "confirmation_warning_item_failed",
                appointment_id=str(appointment_id),
                error_type=type(exc).__name__,
            )
    logger.info("confirmation_warning_tick", **asdict(report))
    return report


async def process_confirmation_warnings(ctx: dict) -> None:
    """arq cron (every minute, workers/arq_worker.py): warn the clinics."""
    await run_warning_tick(now=datetime.now(UTC))
```

- [ ] **Step 4: Register the cron and update the registry test**

In `src/secretaria/workers/arq_worker.py`, after R2's import block `from secretaria.workers.reminder_engine import (...)` add:

```python
from secretaria.workers.confirmation_warnings import process_confirmation_warnings
```

(keep the import block sorted: `confirmation_warnings` sorts before `deploy_parity` — place it directly after `from secretaria.plugins.reminders import send_appointment_reminders`'s block and before `from secretaria.workers.deploy_parity import ...`), and in `cron_jobs`, right after R2's line `cron(reconcile_appointment_reminders, minute={4, 14, 24, 34, 44, 54}),` add:

```python
        # TASK-032 R4: warn the clinic about reminded-but-unconfirmed appointments
        # (sets `warned_at`, which turns the agenda red, and e-mails the alert
        # address). Every minute; the atomic claim makes an overlapping tick harmless.
        cron(process_confirmation_warnings, minute=set(range(60))),
```

In `tests/test_build_identity.py`, in `test_worker_registry_is_complete` replace

```python
        "cron:reconcile_appointment_reminders",
```

with

```python
        "cron:reconcile_appointment_reminders",
        "cron:process_confirmation_warnings",
```

and in `test_worker_startup_logs_identity_and_registry` replace `assert len(fields["cron_jobs"]) == 7` with `assert len(fields["cron_jobs"]) == 8`.

- [ ] **Step 5: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_confirmation_warnings_cron.py tests/test_confirmation_warnings_select.py tests/test_build_identity.py -q`
Expected: all pass. (`test_the_cron_entry_point_runs_a_tick` patches the module's `datetime`; if `datetime.now(UTC)` there breaks because `UTC` is imported separately that is fine — only `datetime.now` is replaced.)

- [ ] **Step 6: Lint, diff check, commit**

Run: `uvx ruff format src/secretaria/workers/confirmation_warnings.py tests/test_confirmation_warnings_cron.py && uvx ruff check --fix src/secretaria/workers/confirmation_warnings.py tests/test_confirmation_warnings_cron.py`; `uvx ruff check src/secretaria/workers/arq_worker.py tests/test_build_identity.py`; `git diff --stat`.

```bash
git add src/secretaria/workers/confirmation_warnings.py src/secretaria/workers/arq_worker.py tests/test_confirmation_warnings_cron.py tests/test_build_identity.py
git commit -m "feat(reminders): process_confirmation_warnings cron mails the clinic once per appointment (TASK-032 R4)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 4: Preview of the deposit outcome and the clinic-facing retention text

**Files:**
- Modify: `src/secretaria/services/payments/deposit_lifecycle.py` (append two functions at the end of the file)
- Test: `tests/test_deposit_release_preview.py`

**Interfaces:**
- Consumes: `format_brl`, `PixDepositStatus`, `_as_utc` (all already in the module).
- Produces:
  - `preview_cancellation_outcome(tenant: Tenant, deposit: PixDeposit | None, appointment: Appointment, *, now: datetime | None = None) -> str | None` — the outcome `on_appointment_cancelled` WOULD return (`"voided"`, `"refunded"`, `"partial_refund"`, `"retained"`) or `None` (no deposit / already resolved). Pure: no I/O, no mutation. It cannot predict `"refund_failed"` (that depends on the PSP call).
  - `release_warning_text(outcome: str | None, tenant: Tenant, deposit: PixDeposit) -> str` — the pt-BR sentence the clinic reads before releasing a slot with a paid deposit.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_deposit_release_preview.py`:

```python
"""Preview of what a cancellation does to a paid deposit (TASK-032 R4, spec 4.4)."""

from datetime import timedelta
from uuid import uuid4

import pytest

from secretaria.models import Appointment, PixDeposit, PixDepositStatus, Tenant
from secretaria.services.payments import deposit_lifecycle
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import NOW, seed_world


@pytest.fixture(autouse=True)
def _fake_refund(monkeypatch: pytest.MonkeyPatch):
    async def _refund(session, tenant, deposit, *, value_cents, full, now):
        return "refunded" if full else "partial_refund"

    monkeypatch.setattr(deposit_lifecycle, "_refund", _refund)


async def _setup(db, *, hours_ahead: float, policy: str = "total", status=PixDepositStatus.PAID):  # noqa: F811
    world = await seed_world(db, start_at=NOW + timedelta(hours=hours_ahead))
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        tenant.pix_retention_policy = policy
        session.add(
            PixDeposit(
                id=uuid4(),
                tenant_id=world.tenant.id,
                appointment_id=world.appointment.id,
                patient_id=world.patient.id,
                asaas_payment_id=f"pay-{uuid4()}",
                amount_cents=10000,
                percent_applied=30,
                status=status,
            )
        )
        await session.commit()
    return world


async def _predicted_and_actual(db, world):  # noqa: F811
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
        predicted = deposit_lifecycle.preview_cancellation_outcome(
            tenant, deposit, appointment, now=NOW
        )
        actual = await deposit_lifecycle.on_appointment_cancelled(
            session, tenant=tenant, appointment=appointment, now=NOW
        )
        return predicted, actual


@pytest.mark.parametrize(
    ("hours_ahead", "policy", "expected"),
    [
        (72, "total", "refunded"),
        (72, "partial", "refunded"),
        (2, "total", "retained"),
        (2, "partial", "partial_refund"),
    ],
)
async def test_the_preview_matches_what_the_cancellation_really_does(  # noqa: F811
    db, hours_ahead, policy, expected
):
    world = await _setup(db, hours_ahead=hours_ahead, policy=policy)

    predicted, actual = await _predicted_and_actual(db, world)

    assert predicted == actual == expected


async def test_an_unpaid_charge_is_voided(db):  # noqa: F811
    world = await _setup(db, hours_ahead=2, status=PixDepositStatus.AWAITING)

    predicted, actual = await _predicted_and_actual(db, world)

    assert predicted == actual == "voided"


async def test_no_deposit_and_an_already_resolved_one_have_no_outcome(db):  # noqa: F811
    bare = await seed_world(db)
    resolved = await _setup(db, hours_ahead=2, status=PixDepositStatus.CANCELLED_REFUNDED)

    for world in (bare, resolved):
        predicted, actual = await _predicted_and_actual(db, world)
        assert predicted is None and actual is None


def _deposit(amount: int = 10000) -> PixDeposit:
    return PixDeposit(amount_cents=amount, status=PixDepositStatus.PAID)


def _tenant(policy: str = "total", percent: int = 50) -> Tenant:
    return Tenant(
        clinic_name="Clínica",
        pix_refund_window_hours=24,
        pix_retention_policy=policy,
        pix_partial_refund_percent=percent,
    )


def test_retained_text_says_the_clinic_keeps_the_deposit():
    text = deposit_lifecycle.release_warning_text("retained", _tenant(), _deposit())

    assert "100,00" in text and "24h" in text and "fica com o sinal" in text


def test_partial_text_gives_the_refunded_part():
    text = deposit_lifecycle.release_warning_text(
        "partial_refund", _tenant("partial", 40), _deposit()
    )

    assert "40,00" in text and "retido" in text


def test_refunded_text_says_the_whole_deposit_goes_back():
    text = deposit_lifecycle.release_warning_text("refunded", _tenant(), _deposit())

    assert "100,00" in text and "por inteiro" in text


def test_an_unknown_outcome_still_warns():
    text = deposit_lifecycle.release_warning_text(None, _tenant(), _deposit())

    assert "100,00" in text and "sinal" in text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_deposit_release_preview.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'preview_cancellation_outcome'`. (If `PixDeposit` / `PixDepositStatus` are not exported by `secretaria.models`, import them from `secretaria.models.pix_deposit` — R2's helpers do `from secretaria.models import PixDeposit, PixDepositStatus`.)

- [ ] **Step 3: Implement**

Append at the very end of `src/secretaria/services/payments/deposit_lifecycle.py`:

```python


def preview_cancellation_outcome(
    tenant: Tenant,
    deposit: PixDeposit | None,
    appointment: Appointment,
    *,
    now: datetime | None = None,
) -> str | None:
    """What `on_appointment_cancelled` WOULD return for this deposit, with no I/O.

    Mirrors that function's branching one-to-one (same window arithmetic, same
    retention policy) so the hub can show the clinic the money consequence BEFORE
    a slot is released (TASK-032 R4). It cannot predict "refund_failed": that is
    the PSP's answer. Keep the two in step - `tests/test_deposit_release_preview.py`
    runs both on the same data and compares.
    """
    if deposit is None or deposit.status not in (PixDepositStatus.AWAITING, PixDepositStatus.PAID):
        return None
    if deposit.status is PixDepositStatus.AWAITING:
        return "voided"
    now = now or datetime.now(UTC)
    start_at = appointment.start_at
    hours_until = None if start_at is None else (_as_utc(start_at) - now).total_seconds() / 3600
    if hours_until is None or hours_until > tenant.pix_refund_window_hours:
        return "refunded"
    if tenant.pix_retention_policy == "partial":
        return "partial_refund"
    return "retained"


def release_warning_text(outcome: str | None, tenant: Tenant, deposit: PixDeposit) -> str:
    """The pt-BR warning the CLINIC reads before freeing a paid slot (not the patient notice).

    `cancellation_notice` above is the patient-facing sentence; this one is
    addressed to the clinic and always names the amount, because releasing a
    slot whose deposit was paid is the one release that moves money.
    """
    paid = f"O paciente já pagou o sinal ({format_brl(deposit.amount_cents)})."
    if outcome == "retained":
        return (
            f"{paid} Como faltam menos de {tenant.pix_refund_window_hours}h para a consulta, "
            "ao liberar o horário a clínica fica com o sinal."
        )
    if outcome == "partial_refund":
        refunded = round(deposit.amount_cents * tenant.pix_partial_refund_percent / 100)
        return (
            f"{paid} Como faltam menos de {tenant.pix_refund_window_hours}h para a consulta, "
            f"{format_brl(refunded)} serão estornados ao paciente e o restante fica retido "
            "pela clínica."
        )
    if outcome == "refunded":
        return f"{paid} Ao liberar o horário, o valor será estornado por inteiro ao paciente."
    return f"{paid} Confira a política de estorno do sinal antes de liberar o horário."
```

- [ ] **Step 4: Run them to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_deposit_release_preview.py tests/test_hub_calendar_money.py -q`
Expected: all pass.

- [ ] **Step 5: Lint, diff check, commit**

Run: `uvx ruff format tests/test_deposit_release_preview.py && uvx ruff check --fix tests/test_deposit_release_preview.py`; `uvx ruff check src/secretaria/services/payments/deposit_lifecycle.py`; `git diff --stat` (deposit_lifecycle.py: only added lines).

```bash
git add src/secretaria/services/payments/deposit_lifecycle.py tests/test_deposit_release_preview.py
git commit -m "feat(payments): preview a cancellation's deposit outcome and the clinic warning text (TASK-032 R4)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 5: Staff free text on the patient's channel (`services/staff_patient_message.py`)

**Files:**
- Create: `src/secretaria/services/staff_patient_message.py`
- Test: `tests/test_staff_patient_message.py`

**Interfaces:**
- Consumes: Task 1 template `clinic_message_patient`; R2 `reminder_text.portal_conversation_link`; `cancellation_notice.last_inbound_at/is_inside_window/whatsapp_deep_link/meta_language_code`; `BrainMessageSender`, `RECORDED_MESSAGE_ID`, `CHANNEL_BRAIN_MESSAGE`; `WhatsAppClient`; `get_waba_token`; `emit_usage_event`; `send_transactional_email_result`, `EmailOutcome`.
- Produces:
  - constants `DELIVERY_WHATSAPP_TEXT = "whatsapp_text"`, `DELIVERY_WHATSAPP_TEMPLATE = "whatsapp_template"`, `DELIVERY_PORTAL_CHAT = "portal_chat"`, `EMAIL_SENT = "sent"`, `EMAIL_NO_ADDRESS = "no_email"`, `EMAIL_NOT_SENT = "not_sent"`
  - exceptions `StaffMessageError(Exception)` (attr `code: str`), `NoChannelError` (`code="no_channel"`), `OutsideWindowError` (`code="outside_window_not_authorised"`, attr `whatsapp_link: str | None`), `DeliveryFailedError` (`code="delivery_failed"`)
  - `@dataclass(frozen=True) StaffMessageResult(delivery: str, email_nudge: str | None, message_id: UUID | None)`
  - `async conversation_id_for(session, tenant_id: UUID, appointment: Appointment, patient: Patient | None) -> UUID | None` (tenant-scoped; the appointment's own conversation first, else the patient's latest)
  - `async nudge_portal_patient(tenant: Tenant, patient: Patient) -> str` (one of `EMAIL_*`; never raises)
  - `async send_staff_message(session, tenant, appointment, patient, text: str, *, allow_paid: bool = False, now: datetime | None = None) -> StaffMessageResult` — commits `session` itself after a successful send.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_staff_patient_message.py`:

```python
"""Staff free text on the patient's own channel (TASK-032 R4, spec 4.4)."""

from datetime import timedelta

import pytest

from secretaria.models import Appointment, MessageSender, Patient, Tenant
from secretaria.services import staff_patient_message as spm
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.email import EmailOutcome
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import (
    NOW,
    FakeWhatsAppClient,
    fake_waba_token,
    outbound_messages,
    seed_world,
)

LINK = "https://portal.exemplo/clinicas/?convite=abc"


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch):
    FakeWhatsAppClient.reset()
    monkeypatch.setattr(spm, "WhatsAppClient", FakeWhatsAppClient)
    monkeypatch.setattr(spm, "get_waba_token", fake_waba_token)
    monkeypatch.setattr(spm, "portal_conversation_link", lambda tenant_id: LINK)


@pytest.fixture
def usage(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    events: list[dict] = []

    async def _emit(**kwargs) -> bool:
        events.append(kwargs)
        return True

    monkeypatch.setattr(spm, "emit_usage_event", _emit)
    return events


@pytest.fixture
def mail(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    sent: list[tuple] = []

    async def _send(to: str, template: str, variables: dict) -> EmailOutcome:
        sent.append((to, template, variables))
        return EmailOutcome.SENT

    monkeypatch.setattr(spm, "send_transactional_email_result", _send)
    return sent


async def _send(db, world, text="Olá, tudo bem?", *, allow_paid=False):  # noqa: F811
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        patient = await session.get(Patient, world.patient.id)
        return await spm.send_staff_message(
            session, tenant, appointment, patient, text, allow_paid=allow_paid, now=NOW
        )


# ------------------------------------------------------------------ WhatsApp


async def test_inside_the_window_it_is_plain_text_and_recorded_as_the_clinic(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))

    result = await _send(db, world)

    assert result.delivery == spm.DELIVERY_WHATSAPP_TEXT and result.email_nudge is None
    assert FakeWhatsAppClient.all_sent() == [("text", "5511988887777", "Olá, tudo bem?")]
    [row] = await outbound_messages(db, world.conversation.id)
    assert row.sender == MessageSender.HUMAN and row.body == "Olá, tudo bem?"
    assert row.wam_id == "wamid.text"
    assert usage == []  # free inside the window


async def test_outside_the_window_it_needs_the_clinics_authorisation(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=30))

    with pytest.raises(spm.OutsideWindowError) as err:
        await _send(db, world)

    assert err.value.code == "outside_window_not_authorised"
    assert err.value.whatsapp_link == "https://wa.me/5511988887777"
    assert FakeWhatsAppClient.all_sent() == []
    assert await outbound_messages(db, world.conversation.id) == []


async def test_a_patient_who_never_wrote_is_outside_the_window(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=None)

    with pytest.raises(spm.OutsideWindowError):
        await _send(db, world)


async def test_authorised_outside_the_window_it_is_a_one_line_template_and_metered(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=30))

    result = await _send(db, world, "Olá!\r\n\r\nPodemos  confirmar?\tAté logo", allow_paid=True)

    assert result.delivery == spm.DELIVERY_WHATSAPP_TEMPLATE
    [sent] = FakeWhatsAppClient.all_sent()
    assert sent[0] == "template" and sent[2] == "appointment_reminder" and sent[3] == "pt_BR"
    assert sent[4] == ["Olá! Podemos confirmar? Até logo"]  # no newline, tab or run of spaces
    [row] = await outbound_messages(db, world.conversation.id)
    assert row.body == "Olá!\r\n\r\nPodemos  confirmar?\tAté logo"  # history keeps what staff typed
    assert [e["feature"] for e in usage] == ["reminders"] and usage[0]["amount"] == 1


async def test_when_meta_fails_nothing_is_recorded_and_the_error_is_typed(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    FakeWhatsAppClient.fail_everything = True

    with pytest.raises(spm.DeliveryFailedError) as err:
        await _send(db, world)

    assert err.value.code == "delivery_failed"
    assert await outbound_messages(db, world.conversation.id) == []


async def test_an_appointment_without_a_patient_has_no_channel(db, usage):  # noqa: F811
    world = await seed_world(db)
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        with pytest.raises(spm.NoChannelError):
            await spm.send_staff_message(session, tenant, appointment, None, "oi", now=NOW)


# -------------------------------------------------------------------- Portal


async def test_a_portal_patient_gets_the_chat_message_and_an_email_nudge_without_the_text(db, mail):  # noqa: F811
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE, email="paciente@x.com")

    result = await _send(db, world, "Exame com resultado alterado, ligue.")

    assert result.delivery == spm.DELIVERY_PORTAL_CHAT and result.email_nudge == spm.EMAIL_SENT
    assert result.message_id is not None
    [row] = await outbound_messages(db, world.conversation.id)
    assert row.sender == MessageSender.HUMAN and row.body == "Exame com resultado alterado, ligue."
    [(to, template, variables)] = mail
    assert (to, template) == ("paciente@x.com", "clinic_message_patient")
    assert variables == {"clinic_name": "Clínica Olhar", "link_line": LINK}
    assert "Exame" not in repr(variables)  # the text never travels by e-mail
    assert FakeWhatsAppClient.all_sent() == []


async def test_a_portal_patient_without_email_still_gets_the_chat_message(db, mail):  # noqa: F811
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE, email=None)

    result = await _send(db, world)

    assert result.delivery == spm.DELIVERY_PORTAL_CHAT
    assert result.email_nudge == spm.EMAIL_NO_ADDRESS
    assert mail == []
    assert len(await outbound_messages(db, world.conversation.id)) == 1


async def test_a_failing_email_does_not_fail_the_message(db, monkeypatch):  # noqa: F811
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE, email="paciente@x.com")

    async def _down(to, template, variables):
        return EmailOutcome.SEND_FAILED

    monkeypatch.setattr(spm, "send_transactional_email_result", _down)

    result = await _send(db, world)

    assert result.email_nudge == spm.EMAIL_NOT_SENT
    assert len(await outbound_messages(db, world.conversation.id)) == 1


async def test_without_a_portal_link_the_nudge_still_says_where_to_go(db, mail, monkeypatch):  # noqa: F811
    monkeypatch.setattr(spm, "portal_conversation_link", lambda tenant_id: None)
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE, email="paciente@x.com")

    await _send(db, world)

    assert "portal" in mail[0][2]["link_line"].lower()


async def test_a_portal_patient_without_a_conversation_has_no_channel(db, mail):  # noqa: F811
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE, email="paciente@x.com")
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.conversation_id = None
        await session.delete(await session.get(type(world.conversation), world.conversation.id))
        await session.commit()

    with pytest.raises(spm.NoChannelError):
        await _send(db, world)

    assert mail == []


# ------------------------------------------------------------------ isolation


async def test_a_conversation_of_another_clinic_is_never_used(db):  # noqa: F811
    mine = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE)
    theirs = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE)
    async with db() as session:
        appointment = await session.get(Appointment, mine.appointment.id)
        appointment.conversation_id = theirs.conversation.id  # corrupt link
        await session.commit()
        appointment = await session.get(Appointment, mine.appointment.id)
        patient = await session.get(Patient, mine.patient.id)

        found = await spm.conversation_id_for(session, mine.tenant.id, appointment, patient)

    assert found == mine.conversation.id  # fell back to MY patient's own conversation
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_staff_patient_message.py -q`
Expected: collection error `ModuleNotFoundError: ... staff_patient_message`.

- [ ] **Step 3: Implement**

Create `src/secretaria/services/staff_patient_message.py`:

```python
"""Staff free text to a patient, on the patient's own channel (TASK-032 R4, spec 4.4).

The clinic sees a red appointment and wants to ask the patient "did you get our
reminder?". Where that text goes depends on the channel, and the two channels
disagree about what "sent" means (services/channel_sender.py documents the
asymmetry as `persists_outbound`):

* **WhatsApp** - inside Meta's 24 h window a free text is allowed and free;
  outside it WhatsApp accepts only an approved template and bills it. The clinic
  must say yes to that cost (`allow_paid`, shown with the price in the hub) -
  same rule as `send_cancellation_notice`. The template is the clinic's existing
  one-variable reminder template (`REMINDER_TEMPLATE_NAME`), the text flattened to
  one line (Meta rejects newlines/tabs in a parameter).
* **Portal (Brain-Message)** - there is no window and no network leg: writing the
  `Message` row IS the delivery. A Portal patient may not be looking at the
  console, so a generic e-mail nudge (never the text itself, which may be
  clinical) points them back to the conversation; no e-mail on file is fine.

Deliberate: this does NOT take the conversation over from the bot (the staff
console's `send_message` does). A nudge about a reminder should not silence the
secretaria for the patient's answer.

Never logs a phone number, an e-mail address or the message text.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)
from secretaria.services import cancellation_notice
from secretaria.services.channel_sender import (
    CHANNEL_BRAIN_MESSAGE,
    RECORDED_MESSAGE_ID,
    BrainMessageSender,
)
from secretaria.services.email import EmailOutcome, send_transactional_email_result
from secretaria.services.reminder_text import portal_conversation_link
from secretaria.services.tenant_config import get_waba_token
from secretaria.services.usage_events import emit_usage_event
from secretaria.services.whatsapp import WhatsAppClient

logger = get_logger(__name__)

DELIVERY_WHATSAPP_TEXT = "whatsapp_text"
DELIVERY_WHATSAPP_TEMPLATE = "whatsapp_template"
DELIVERY_PORTAL_CHAT = "portal_chat"

EMAIL_SENT = "sent"
EMAIL_NO_ADDRESS = "no_email"
EMAIL_NOT_SENT = "not_sent"

_PORTAL_LINK_FALLBACK = "Acesse o portal da clínica e abra a sua conversa."


class StaffMessageError(Exception):
    """Base of the typed failures the hub maps to HTTP errors."""

    code = "staff_message_error"


class NoChannelError(StaffMessageError):
    code = "no_channel"


class OutsideWindowError(StaffMessageError):
    code = "outside_window_not_authorised"

    def __init__(self, whatsapp_link: str | None) -> None:
        super().__init__("outside the 24h window and the paid template was not authorised")
        self.whatsapp_link = whatsapp_link


class DeliveryFailedError(StaffMessageError):
    code = "delivery_failed"


@dataclass(frozen=True)
class StaffMessageResult:
    delivery: str
    email_nudge: str | None
    message_id: UUID | None


async def conversation_id_for(
    session: AsyncSession,
    tenant_id: UUID,
    appointment: Appointment,
    patient: Patient | None,
) -> UUID | None:
    """The conversation a message to this appointment's patient belongs to.

    Tenant-scoped on every branch: the appointment's own `conversation_id` is
    used only when that conversation belongs to the same clinic; otherwise (or
    when it is unset) the patient's most recent conversation in the clinic.
    """
    if appointment.conversation_id is not None:
        own = await session.scalar(
            select(Conversation.id).where(
                Conversation.id == appointment.conversation_id,
                Conversation.tenant_id == tenant_id,
                Conversation.patient_id == appointment.patient_id,
            )
        )
        if own is not None:
            return own
    if patient is None:
        return None
    return await session.scalar(
        select(Conversation.id)
        .where(Conversation.tenant_id == tenant_id, Conversation.patient_id == patient.id)
        .order_by(Conversation.created_at.desc())
        .limit(1)
    )


async def nudge_portal_patient(tenant: Tenant, patient: Patient) -> str:
    """E-mail a Portal patient that the clinic wrote to them. Never raises.

    The body is generic on purpose - it never carries the clinic's text.
    """
    address = (patient.email or "").strip()
    if not address:
        return EMAIL_NO_ADDRESS
    link = portal_conversation_link(tenant.id)
    outcome = await send_transactional_email_result(
        address,
        "clinic_message_patient",
        {"clinic_name": tenant.clinic_name, "link_line": link or _PORTAL_LINK_FALLBACK},
    )
    return EMAIL_SENT if outcome is EmailOutcome.SENT else EMAIL_NOT_SENT


def _one_line(text: str) -> str:
    """A WhatsApp template parameter: no newline, tab or run of spaces (Meta's rule)."""
    return " ".join(text.split())


def _wam_id(response: dict) -> str | None:
    try:
        return response["messages"][0]["id"]
    except (KeyError, IndexError, TypeError):
        return None


async def send_staff_message(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient | None,
    text: str,
    *,
    allow_paid: bool = False,
    now: datetime | None = None,
) -> StaffMessageResult:
    """Deliver `text` to the appointment's patient; commits `session` on success.

    Raises `NoChannelError` (no patient / no number / Portal patient with no
    conversation), `OutsideWindowError` (WhatsApp, outside 24 h, not authorised)
    or `DeliveryFailedError` (the channel refused). Nothing is recorded on error.
    The caller has already proved the appointment belongs to `tenant`.
    """
    if patient is None or patient.tenant_id != tenant.id:
        raise NoChannelError("no patient on this appointment")

    if patient.channel == CHANNEL_BRAIN_MESSAGE:
        conversation_id = await conversation_id_for(session, tenant.id, appointment, patient)
        if conversation_id is None:
            raise NoChannelError("the Portal patient has no conversation yet")
        sender = BrainMessageSender(
            conversation_id=conversation_id, session=session, author=MessageSender.HUMAN
        )
        response = await sender.send_text_message(to=patient.external_id or "", body=text)
        await session.commit()
        nudge = await nudge_portal_patient(tenant, patient)
        logger.info(
            "staff_message_sent",
            tenant_id=str(tenant.id),
            appointment_id=str(appointment.id),
            delivery=DELIVERY_PORTAL_CHAT,
            email_nudge=nudge,
        )
        return StaffMessageResult(
            DELIVERY_PORTAL_CHAT, nudge, UUID(response[RECORDED_MESSAGE_ID])
        )

    to = patient.wa_id or appointment.phone
    if not to:
        raise NoChannelError("the patient has no WhatsApp number")
    last_inbound = await cancellation_notice.last_inbound_at(session, tenant.id, patient.id)
    inside = cancellation_notice.is_inside_window(last_inbound, now=now)
    if not inside and not allow_paid:
        raise OutsideWindowError(cancellation_notice.whatsapp_deep_link(to))

    waba_token = await get_waba_token(session, tenant.id)
    try:
        client = WhatsAppClient.for_tenant(tenant, waba_token)
        if inside:
            response = await client.send_text_message(to=to, body=text)
        else:
            response = await client.send_template(
                to=to,
                template=get_settings().REMINDER_TEMPLATE_NAME,
                lang=cancellation_notice.meta_language_code(tenant.language),
                variables=[_one_line(text)],
            )
    except Exception as exc:
        logger.error(
            "staff_message_failed",
            tenant_id=str(tenant.id),
            appointment_id=str(appointment.id),
            inside_window=inside,
            error_type=type(exc).__name__,
        )
        raise DeliveryFailedError("the channel refused the message") from exc

    conversation_id = await conversation_id_for(session, tenant.id, appointment, patient)
    message_id = None
    if conversation_id is not None:
        message = Message(
            conversation_id=conversation_id,
            direction=MessageDirection.OUTBOUND,
            sender=MessageSender.HUMAN,
            wam_id=_wam_id(response),
            body=text,
        )
        session.add(message)
        await session.flush()
        message_id = message.id
    await session.commit()

    if not inside:
        # After the commit and fail-open: the message already left, so a metering
        # hiccup must never turn into a second billed send (same rule as
        # workers/whatsapp/notifications.py::_emit_cancellation_usage).
        try:
            await emit_usage_event(
                tenant_id=str(tenant.id),
                feature="reminders",
                amount=1,
                event_id=f"staffmsg:{message_id or appointment.id}:{_wam_id(response) or 'x'}",
            )
        except Exception as exc:
            logger.warning("usage_emit_failed", error_type=type(exc).__name__)
    logger.info(
        "staff_message_sent",
        tenant_id=str(tenant.id),
        appointment_id=str(appointment.id),
        delivery=DELIVERY_WHATSAPP_TEXT if inside else DELIVERY_WHATSAPP_TEMPLATE,
    )
    return StaffMessageResult(
        DELIVERY_WHATSAPP_TEXT if inside else DELIVERY_WHATSAPP_TEMPLATE, None, message_id
    )
```

- [ ] **Step 4: Run them to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_staff_patient_message.py -q`
Expected: `12 passed`. If `Message` row ordering in `outbound_messages` or `Conversation.created_at` is `None` on SQLite for the no-conversation test, set the test's conversation deletion before reading — the helper only queries by id.

- [ ] **Step 5: Lint and commit**

Run: `uvx ruff format src/secretaria/services/staff_patient_message.py tests/test_staff_patient_message.py && uvx ruff check --fix src/secretaria/services/staff_patient_message.py tests/test_staff_patient_message.py`

```bash
git add src/secretaria/services/staff_patient_message.py tests/test_staff_patient_message.py
git commit -m "feat(hub): staff free text on the patient's channel, with Portal e-mail nudge (TASK-032 R4)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 6: `POST /appointments/{id}/release` — the core (guards, Google, status, reminders)

**Files:**
- Modify: `src/secretaria/services/appointment_status.py` (constant + `reason` kwarg)
- Modify: `src/secretaria/schemas/calendar.py` (append `AppointmentRelease`, `AppointmentReleaseRead`)
- Modify: `src/secretaria/api/hub/calendar.py` (imports, `_detail`, `_owning_calendar`, `release_appointment`; module docstring route list)
- Test: `tests/test_hub_release.py`

**Interfaces:**
- Consumes: R1 `reminder_schedule.cancel_reminders`; existing `_get_appointment`, `_get_calendar`, `_professional_name`, `_appointment_read`, `_deposit_status_value`; `resolve_professional_calendar`; `deposit_lifecycle.on_appointment_cancelled`.
- Produces:
  - `appointment_status.CANCEL_REASON_UNCONFIRMED = "unconfirmed"`; `log_status_transition(..., reason: str | None = None)` (additive; the log line gains `reason`).
  - `schemas.calendar.AppointmentRelease(acknowledge_retention: bool = False, release_confirmed: bool = False, notify_outside_window: bool = False, justification: str | None = None)` (max 1000 chars) and `AppointmentReleaseRead(AppointmentRead)` + `patient_notice: str = "not_attempted"`.
  - route `POST /tenants/me/calendar/appointments/{appointment_id}/release`.
  - `api/hub/calendar.py::_detail(code: str, message: str, **extra) -> dict` and `_owning_calendar(session, tenant, appt) -> CalendarService`.
- Decisions pinned here: **fail closed** on a Google error (502, nothing changes); the Google delete happens BEFORE the guarded status UPDATE (so two concurrent releases are safe: the delete is idempotent and the `UPDATE ... WHERE status IN live` has exactly one winner); a confirmed appointment needs `release_confirmed`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hub_release.py`:

```python
"""POST /appointments/{id}/release — guards, Google, status, reminders (TASK-032 R4, spec 4.4)."""

from datetime import timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub import calendar as hub_calendar
from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import Appointment, AppointmentReminder, AppointmentStatus, Tenant
from secretaria.services.tenant_config import set_google_refresh_token
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import NOW, add_reminder, seed_world

CALENDAR = "/tenants/me/calendar"


class _FakeCalendar:
    deleted: list[tuple[str, str]] = []
    fail_with: Exception | None = None
    during_delete = None  # optional async callable, to simulate a concurrent request

    def __init__(self, label: str = "tenant") -> None:
        self.label = label

    @classmethod
    def from_tenant_config(cls, config):
        return cls("tenant")

    async def cancel_event(self, event_id: str) -> None:
        if type(self).during_delete is not None:
            await type(self).during_delete()
        if type(self).fail_with is not None:
            raise type(self).fail_with
        type(self).deleted.append((self.label, event_id))


@pytest.fixture
def acting() -> dict:
    return {"tenant_id": None}


@pytest.fixture(autouse=True)
def _wire(db, acting, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, acting["tenant_id"])

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    monkeypatch.setattr(hub_calendar, "CalendarService", _FakeCalendar)
    _FakeCalendar.deleted = []
    _FakeCalendar.fail_with = None
    _FakeCalendar.during_delete = None
    app.state.arq_pool = None
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _connect(db, tenant_id) -> None:  # noqa: F811
    async with db() as session:
        await set_google_refresh_token(session, tenant_id, "fake-refresh-token")
        await session.commit()


async def _release(client: AsyncClient, appointment_id, **body):
    return await client.post(f"{CALENDAR}/appointments/{appointment_id}/release", json=body)


async def _appointment(db, appointment_id) -> Appointment:  # noqa: F811
    async with db() as session:
        return await session.get(Appointment, appointment_id)


async def _reminder_statuses(db, appointment_id) -> set[str]:  # noqa: F811
    async with db() as session:
        rows = await session.scalars(
            select(AppointmentReminder).where(AppointmentReminder.appointment_id == appointment_id)
        )
        return {r.status for r in rows}


class _TransitionSpy:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> None:
        self.calls.append(kwargs)


async def _setup(db, acting, **kwargs):  # noqa: F811
    kwargs.setdefault("last_inbound_at", NOW)
    world = await seed_world(db, **kwargs)
    acting["tenant_id"] = world.tenant.id
    await _connect(db, world.tenant.id)
    return world


async def test_release_frees_the_slot_cancels_the_row_and_the_pending_reminders(  # noqa: F811
    client: AsyncClient, db, acting, monkeypatch
):
    world = await _setup(db, acting)
    await add_reminder(db, world, kind="day", status="pending")
    spy = _TransitionSpy()
    monkeypatch.setattr(hub_calendar, "log_status_transition", spy)

    response = await _release(client, world.appointment.id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "cancelled" and body["id"] == str(world.appointment.id)
    assert _FakeCalendar.deleted == [("tenant", world.appointment.google_event_id)]
    assert (await _appointment(db, world.appointment.id)).status == AppointmentStatus.CANCELLED
    assert await _reminder_statuses(db, world.appointment.id) == {"cancelled"}
    [transition] = spy.calls
    assert transition["reason"] == "unconfirmed" and transition["source"] == "hub"
    assert transition["new_status"] == AppointmentStatus.CANCELLED


async def test_a_second_release_changes_nothing_and_says_why(client: AsyncClient, db, acting):  # noqa: F811
    world = await _setup(db, acting)

    first = await _release(client, world.appointment.id)
    second = await _release(client, world.appointment.id)

    assert first.status_code == 200 and second.status_code == 409
    detail = second.json()["detail"]
    assert detail["code"] == "not_live" and detail["status"] == "cancelled"
    assert len(_FakeCalendar.deleted) == 1  # Google was asked once


async def test_two_concurrent_releases_have_exactly_one_winner(  # noqa: F811
    client: AsyncClient, db, acting, monkeypatch
):
    """The other request cancels the row while this one is talking to Google."""
    world = await _setup(db, acting)

    async def _other_request_wins() -> None:
        async with db() as session:
            await session.execute(
                update(Appointment)
                .where(Appointment.id == world.appointment.id)
                .values(status=AppointmentStatus.CANCELLED)
            )
            await session.commit()

    _FakeCalendar.during_delete = _other_request_wins
    calls: list[str] = []

    async def _no_money(*args, **kwargs):
        calls.append("deposit hook")

    monkeypatch.setattr(hub_calendar.deposit_lifecycle, "on_appointment_cancelled", _no_money)

    response = await _release(client, world.appointment.id)

    assert response.status_code == 409 and response.json()["detail"]["code"] == "not_live"
    assert calls == []  # the loser neither touches the money nor notifies


async def test_a_google_failure_changes_nothing_and_is_retryable(  # noqa: F811
    client: AsyncClient, db, acting, monkeypatch
):
    world = await _setup(db, acting)
    await add_reminder(db, world, kind="day", status="pending")
    calls: list[str] = []

    async def _no_money(*args, **kwargs):
        calls.append("deposit hook")

    monkeypatch.setattr(hub_calendar.deposit_lifecycle, "on_appointment_cancelled", _no_money)
    _FakeCalendar.fail_with = RuntimeError("google is down")

    response = await _release(client, world.appointment.id)

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "calendar_unavailable"
    assert (await _appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED
    assert await _reminder_statuses(db, world.appointment.id) == {"pending"}
    assert calls == []

    _FakeCalendar.fail_with = None  # Google is back: the same click now works
    retry = await _release(client, world.appointment.id)
    assert retry.status_code == 200


async def test_the_clinic_without_google_connected_cannot_release(client: AsyncClient, db, acting):  # noqa: F811
    world = await seed_world(db)
    acting["tenant_id"] = world.tenant.id  # no _connect

    response = await _release(client, world.appointment.id)

    assert response.status_code == 422
    assert (await _appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED


async def test_another_clinics_staff_gets_a_404_and_nothing_happens(client: AsyncClient, db, acting):  # noqa: F811
    mine = await seed_world(db)
    theirs = await _setup(db, acting)  # the caller is THEIR staff

    response = await _release(client, mine.appointment.id)

    assert response.status_code == 404
    assert (await _appointment(db, mine.appointment.id)).status == AppointmentStatus.SCHEDULED
    assert _FakeCalendar.deleted == []
    assert theirs.tenant.id != mine.tenant.id


async def test_a_malformed_id_is_a_404(client: AsyncClient, db, acting):  # noqa: F811
    await _setup(db, acting)

    assert (await _release(client, "not-a-uuid")).status_code == 404
    assert (await _release(client, uuid4())).status_code == 404


async def test_a_block_without_a_patient_is_not_releasable(client: AsyncClient, db, acting):  # noqa: F811
    world = await _setup(db, acting)
    async with db() as session:
        await session.execute(
            update(Appointment).where(Appointment.id == world.appointment.id).values(patient_id=None)
        )
        await session.commit()

    response = await _release(client, world.appointment.id)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "not_a_patient_appointment"
    assert _FakeCalendar.deleted == []


@pytest.mark.parametrize("status", [AppointmentStatus.ATTENDED, AppointmentStatus.NO_SHOW])
async def test_a_closed_appointment_is_not_releasable(client: AsyncClient, db, acting, status):  # noqa: F811
    world = await _setup(db, acting, status=status)

    response = await _release(client, world.appointment.id)

    assert response.status_code == 409 and response.json()["detail"]["code"] == "not_live"
    assert _FakeCalendar.deleted == []


async def test_a_patient_who_confirmed_meanwhile_needs_an_explicit_override(  # noqa: F811
    client: AsyncClient, db, acting
):
    world = await _setup(db, acting, confirmation_count=1, status=AppointmentStatus.CONFIRMED)

    refused = await _release(client, world.appointment.id)
    forced = await _release(client, world.appointment.id, release_confirmed=True)

    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "already_confirmed"
    assert refused.json()["detail"]["confirmation_count"] == 1
    assert forced.status_code == 200 and forced.json()["status"] == "cancelled"


async def test_an_appointment_without_a_google_event_is_released_without_calling_google(  # noqa: F811
    client: AsyncClient, db, acting
):
    world = await _setup(db, acting, google_event_id="")

    response = await _release(client, world.appointment.id)

    assert response.status_code == 200
    assert _FakeCalendar.deleted == []


async def test_a_professional_appointment_is_deleted_on_the_professionals_calendar(  # noqa: F811
    client: AsyncClient, db, acting, monkeypatch
):
    world = await _setup(db, acting, professional_name="Dra. Ana")

    async def _resolve(session, tenant, professional, *, tenant_config=None, **_):
        assert professional.tenant_id == tenant.id
        return _FakeCalendar("professional")

    monkeypatch.setattr(hub_calendar, "resolve_professional_calendar", _resolve)

    response = await _release(client, world.appointment.id)

    assert response.status_code == 200
    assert _FakeCalendar.deleted == [("professional", world.appointment.google_event_id)]


async def test_an_unresolvable_owner_calendar_changes_nothing(  # noqa: F811
    client: AsyncClient, db, acting, monkeypatch
):
    world = await _setup(db, acting, professional_name="Dra. Ana")

    async def _boom(*args, **kwargs):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(hub_calendar, "resolve_professional_calendar", _boom)

    response = await _release(client, world.appointment.id)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "calendar_unresolved"
    assert (await _appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED
    assert _FakeCalendar.deleted == []


async def test_a_staff_release_works_with_the_clinic_switch_off(client: AsyncClient, db, acting):  # noqa: F811
    world = await _setup(db, acting, v2=False)

    response = await _release(client, world.appointment.id)

    assert response.status_code == 200 and response.json()["status"] == "cancelled"


async def test_the_response_keeps_the_appointment_read_shape(client: AsyncClient, db, acting):  # noqa: F811
    world = await _setup(db, acting)

    body = (await _release(client, world.appointment.id)).json()

    for key in ("id", "tenant_id", "patient_id", "status", "start_at", "deposit_status",
                "deposit_outcome", "confirmation_count"):
        assert key in body
    assert body["deposit_outcome"] is None and body["confirmation_count"] == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_hub_release.py -q`
Expected: FAIL — every request answers 404/405 (no route) or `AttributeError: ... has no attribute 'log_status_transition'` on the monkeypatch (it is imported in the module, so that one passes); the first assertion `response.status_code == 200` fails with 405.

- [ ] **Step 3: `appointment_status.py` — the reason**

In `src/secretaria/services/appointment_status.py`, after the line `SOURCE_SYSTEM = "system"` add:

```python

# WHY a hub cancellation happened, for the log only (there is no reason column).
# `unconfirmed`: the clinic freed the slot because the patient never confirmed
# (api/hub/calendar.py::release_appointment, TASK-032 R4).
CANCEL_REASON_UNCONFIRMED = "unconfirmed"
```

change the signature line `    idempotency_key: str | None = None,\n) -> None:` to

```python
    idempotency_key: str | None = None,
    reason: str | None = None,
) -> None:
```

and in the `logger.info("appointment_status_transition", ...)` call add `reason=reason,` after `idempotency_key=idempotency_key,`. Add one sentence to the docstring: `` `reason` is an optional closed-vocabulary code (see CANCEL_REASON_*), never free text. ``

- [ ] **Step 4: Schemas**

Append to `src/secretaria/schemas/calendar.py` (after `AppointmentRead`'s class; if `AppointmentRead` is followed by other classes put these at the end of the file):

```python


class AppointmentRelease(BaseModel):
    """POST /appointments/{id}/release (TASK-032 R4, spec 4.4).

    Freeing the slot of an unconfirmed appointment. Every flag defaults to the
    cautious side, so a client that does not know a flag can never trigger the
    risky behavior by accident.
    """

    # The clinic has read the retention text for a PAID Pix deposit and agrees.
    acknowledge_retention: bool = False
    # The patient confirmed in the meantime; release anyway (the clinic's call).
    release_confirmed: bool = False
    # Authorises the BILLED template notice when the patient is outside Meta's
    # 24 h window (same meaning as AppointmentCancel.notify_outside_window).
    notify_outside_window: bool = False
    # Optional reason quoted in the patient notice; blank = the standard sentence.
    justification: str | None = Field(default=None, max_length=1000)


class AppointmentReleaseRead(AppointmentRead):
    """The released appointment, plus what happened to the patient notice.

    `patient_notice` is one of: whatsapp_queued, whatsapp_outside_window,
    portal_chat, portal_chat_email, no_channel, queue_unavailable, notice_failed.
    """

    patient_notice: str = "not_attempted"
```

- [ ] **Step 5: The endpoint**

In `src/secretaria/api/hub/calendar.py`:

(a) Module docstring: after the `POST .../cancel` line add `POST  /tenants/me/calendar/appointments/{id}/release - free the slot of an unconfirmed appointment.`

(b) Imports. Change `from sqlalchemy import Row, select` to `from sqlalchemy import Row, select, update`; change `from secretaria.models import Appointment, AppointmentStatus, Professional, Tenant` to also import `LIVE_APPOINTMENT_STATUSES` on the next line `from secretaria.models.appointment import LIVE_APPOINTMENT_STATUSES`; in the `secretaria.schemas.calendar` import add `AppointmentRelease` and `AppointmentReleaseRead` (alphabetical); make the `secretaria.services` import line carry `reminder_schedule` (R1/R2 may already have added it — do not duplicate), e.g. `from secretaria.services import cancellation_notice, reminder_schedule`; change `from secretaria.services.appointment_status import SOURCE_HUB, log_status_transition` to `from secretaria.services.appointment_status import (CANCEL_REASON_UNCONFIRMED, SOURCE_HUB, log_status_transition)` (one name per line, ruff-sorted); change `from secretaria.services.tenant_config import load_tenant_config` to `from secretaria.services.tenant_config import load_tenant_config, resolve_professional_calendar`.

(c) After the existing `_professional_name` helper add:

```python
def _detail(code: str, message: str, **extra) -> dict:
    """A machine-readable error body: the front switches on `code`, shows `message`."""
    return {"code": code, "message": message, **extra}


async def _owning_calendar(session: AsyncSession, tenant: Tenant, appt: Appointment):
    """The Google calendar that owns `appt`'s event.

    A booking made with a professional lives on THAT professional's calendar;
    `cancel_event` treats a 404 as "already gone" (success), so deleting on the
    wrong calendar would silently no-op while the slot stays occupied. When the
    owner cannot be resolved this refuses (409) instead of guessing the tenant
    calendar - same "don't guess, degrade" rule as
    workers/shared/actions.py::_calendar_for_appointment.
    """
    if appt.professional_id is None:
        return await _get_calendar(session, tenant)
    professional = await session.scalar(
        select(Professional).where(
            Professional.id == appt.professional_id, Professional.tenant_id == tenant.id
        )
    )
    unresolved = HTTPException(
        status.HTTP_409_CONFLICT,
        _detail(
            "calendar_unresolved",
            "Não foi possível identificar a agenda do profissional desta consulta.",
        ),
    )
    if professional is None:
        raise unresolved
    try:
        config = await load_tenant_config(session, tenant)
        return await resolve_professional_calendar(
            session, tenant, professional, tenant_config=config
        )
    except Exception as exc:
        logger.warning(
            "release_owning_calendar_failed",
            appointment_id=str(appt.id),
            error_type=type(exc).__name__,
        )
        raise unresolved from exc
```

(d) After `cancel_appointment` (before the `# POST /appointments/{id}/reschedule` banner) add:

```python
# ---------------------------------------------------------------------------
# POST /appointments/{id}/release — free the slot of an unconfirmed appointment
# ---------------------------------------------------------------------------


@router.post("/appointments/{appointment_id}/release", response_model=AppointmentReleaseRead)
async def release_appointment(
    appointment_id: str,
    body: AppointmentRelease,
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> AppointmentReleaseRead:
    """The clinic frees the slot of an appointment the patient never confirmed.

    Order matters and every step is deliberate:

    1. Guards (404 foreign/malformed, 422 block, 409 not live, 409 confirmed).
    2. Delete the Google event on the OWNING calendar. FAIL CLOSED: if Google
       refuses, answer 502 and change nothing - marking the row cancelled while
       the event still blocks the slot would be the silent half-release this
       endpoint exists to avoid. The delete is idempotent (404/410 = success), so
       a retry after a partial failure is safe.
    3. A guarded `UPDATE ... WHERE status IN live` has exactly one winner when two
       requests race (a double click, two staff); the loser answers 409 and
       neither touches the money nor tells the patient twice.
    4. In the SAME transaction: the deposit outcome and the reminder rows.
    """
    appt = await _get_appointment(session, tenant, appointment_id)
    if appt.patient_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            _detail("not_a_patient_appointment", "Este horário não pertence a um paciente."),
        )
    if appt.status not in LIVE_APPOINTMENT_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            _detail(
                "not_live",
                "Esta consulta já foi cancelada ou encerrada.",
                status=appt.status.value,
            ),
        )
    if appt.confirmation_count >= 1 and not body.release_confirmed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            _detail(
                "already_confirmed",
                "O paciente já confirmou esta consulta.",
                confirmation_count=appt.confirmation_count,
            ),
        )

    if appt.google_event_id:
        calendar = await _owning_calendar(session, tenant, appt)
        try:
            await calendar.cancel_event(appt.google_event_id)
        except Exception as exc:
            logger.error(
                "calendar_release_delete_failed",
                appointment_id=str(appt.id),
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                _detail(
                    "calendar_unavailable",
                    "Não foi possível apagar o evento no Google Agenda. "
                    "Nada foi alterado; tente de novo em instantes.",
                ),
            ) from exc

    previous_status = appt.status
    now = datetime.now(UTC)
    claimed = await session.execute(
        update(Appointment)
        .where(
            Appointment.id == appt.id,
            Appointment.tenant_id == tenant.id,
            Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
        )
        .values(status=AppointmentStatus.CANCELLED, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            _detail("not_live", "Esta consulta já foi cancelada ou encerrada."),
        )
    await session.refresh(appt)
    log_status_transition(
        appointment_id=appt.id,
        tenant_id=tenant.id,
        old_status=previous_status,
        new_status=AppointmentStatus.CANCELLED,
        source=SOURCE_HUB,
        idempotency_key=f"release:{appt.id}",
        reason=CANCEL_REASON_UNCONFIRMED,
    )
    await reminder_schedule.cancel_reminders(session, appt.id, reason="released")
    deposit_outcome = await deposit_lifecycle.on_appointment_cancelled(
        session, tenant=tenant, appointment=appt, waba_token=None
    )
    await session.commit()
    await session.refresh(appt)

    logger.info(
        "calendar_appointment_released",
        appointment_id=str(appt.id),
        deposit_outcome=deposit_outcome,
    )
    deposit_status = await _deposit_status_value(session, appt.id)
    read = _appointment_read(appt, deposit_status=deposit_status, deposit_outcome=deposit_outcome)
    return AppointmentReleaseRead(**read.model_dump())
```

- [ ] **Step 6: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_hub_release.py tests/test_hub_calendar_money.py tests/test_appointment_status_taxonomy.py tests/test_hub_calendar_confirmation.py -q`
Expected: all pass (the neighbours are the cancel/confirmation tests that share the module and the transition logger). `test_two_concurrent_releases_have_exactly_one_winner` interleaves two sessions on the tests' single shared SQLite connection; if that interleaving proves flaky in this environment, keep the assertion but simulate the loser by making `during_delete` update the row through the SAME `db()` factory before the endpoint's UPDATE (as written) and re-run alone with `-p no:randomly` before concluding anything.

- [ ] **Step 7: Lint, diff check, commit**

Run: `uvx ruff format tests/test_hub_release.py && uvx ruff check --fix tests/test_hub_release.py`; `uvx ruff check src/secretaria/api/hub/calendar.py src/secretaria/schemas/calendar.py src/secretaria/services/appointment_status.py`; `git diff --stat` (three modified files: added lines only).

```bash
git add src/secretaria/services/appointment_status.py src/secretaria/schemas/calendar.py src/secretaria/api/hub/calendar.py tests/test_hub_release.py
git commit -m "feat(hub): POST /appointments/{id}/release frees an unconfirmed slot, failing closed on Google (TASK-032 R4)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 7: Release with a paid Pix deposit requires an explicit acknowledgement

**Files:**
- Modify: `src/secretaria/api/hub/calendar.py` (`release_appointment`: the guard; one import)
- Modify (append tests): `tests/test_hub_release.py`

**Interfaces:**
- Consumes: Task 4 `preview_cancellation_outcome`, `release_warning_text`; `deposit_lifecycle.get_deposit_for_appointment`; `PixDepositStatus`.
- Produces: 409 `{"code":"retention_ack_required","message":<release_warning_text>,"deposit_outcome":"retained"|"partial_refund"|"refunded","amount_cents":<int>}` when the appointment has a PAID deposit and `acknowledge_retention` is false — raised BEFORE Google is touched. With the flag the release proceeds and the response carries `deposit_outcome`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hub_release.py`. First extend the imports at the top: add `from datetime import UTC, datetime` (merge with the existing `from datetime import timedelta`), `from secretaria.models import PixDeposit, PixDepositStatus` (merge into the `secretaria.models` import), `from secretaria.services.payments import deposit_lifecycle`, and `seed_paid_deposit` to the `tests._reminders_v2` import. Then append:

```python
# ------------------------------------------------------------------- Pix deposit


async def _deposit_status(db, appointment_id):  # noqa: F811
    async with db() as session:
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == appointment_id)
        )
        return deposit.status


async def _policy(db, tenant_id, policy: str) -> None:  # noqa: F811
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.pix_retention_policy = policy
        await session.commit()


async def _paid_world(db, acting, *, hours_ahead: float, policy: str = "total", **kwargs):  # noqa: F811
    world = await _setup(
        db, acting, start_at=datetime.now(UTC) + timedelta(hours=hours_ahead), **kwargs
    )
    await _policy(db, world.tenant.id, policy)
    await seed_paid_deposit(db, world)
    return world


@pytest.fixture
def refund_ok(monkeypatch: pytest.MonkeyPatch):
    async def _refund(session, tenant, deposit, *, value_cents, full, now):
        deposit.status = (
            PixDepositStatus.CANCELLED_REFUNDED if full else PixDepositStatus.CANCELLED_RETAINED
        )
        return "refunded" if full else "partial_refund"

    monkeypatch.setattr(deposit_lifecycle, "_refund", _refund)


async def test_a_paid_deposit_inside_the_retention_window_needs_the_acknowledgement(  # noqa: F811
    client: AsyncClient, db, acting
):
    world = await _paid_world(db, acting, hours_ahead=2)

    response = await _release(client, world.appointment.id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "retention_ack_required"
    assert detail["deposit_outcome"] == "retained" and detail["amount_cents"] == 10000
    assert "100,00" in detail["message"] and "fica com o sinal" in detail["message"]
    assert (await _appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED
    assert _FakeCalendar.deleted == []  # Google was not touched
    assert await _deposit_status(db, world.appointment.id) == PixDepositStatus.PAID


async def test_with_the_acknowledgement_the_clinic_keeps_the_deposit(client: AsyncClient, db, acting):  # noqa: F811
    world = await _paid_world(db, acting, hours_ahead=2)

    response = await _release(client, world.appointment.id, acknowledge_retention=True)

    assert response.status_code == 200
    assert response.json()["deposit_outcome"] == "retained"
    assert await _deposit_status(db, world.appointment.id) == PixDepositStatus.CANCELLED_RETAINED
    assert len(_FakeCalendar.deleted) == 1


async def test_a_partial_policy_is_announced_as_such(client: AsyncClient, db, acting):  # noqa: F811
    world = await _paid_world(db, acting, hours_ahead=2, policy="partial")

    detail = (await _release(client, world.appointment.id)).json()["detail"]

    assert detail["deposit_outcome"] == "partial_refund"
    assert "50,00" in detail["message"]


async def test_even_a_full_refund_asks_first_and_then_refunds(  # noqa: F811
    client: AsyncClient, db, acting, refund_ok
):
    world = await _paid_world(db, acting, hours_ahead=96)

    refused = await _release(client, world.appointment.id)
    accepted = await _release(client, world.appointment.id, acknowledge_retention=True)

    assert refused.status_code == 409
    assert refused.json()["detail"]["deposit_outcome"] == "refunded"
    assert "por inteiro" in refused.json()["detail"]["message"]
    assert accepted.status_code == 200 and accepted.json()["deposit_outcome"] == "refunded"


async def test_an_unpaid_charge_needs_no_acknowledgement(client: AsyncClient, db, acting):  # noqa: F811
    world = await _setup(db, acting)
    async with db() as session:
        session.add(
            PixDeposit(
                id=uuid4(),
                tenant_id=world.tenant.id,
                appointment_id=world.appointment.id,
                patient_id=world.patient.id,
                asaas_payment_id=None,
                amount_cents=10000,
                percent_applied=30,
                status=PixDepositStatus.AWAITING,
            )
        )
        await session.commit()

    response = await _release(client, world.appointment.id)

    assert response.status_code == 200 and response.json()["deposit_outcome"] == "voided"


async def test_the_flag_is_harmless_without_a_deposit(client: AsyncClient, db, acting):  # noqa: F811
    world = await _setup(db, acting)

    response = await _release(client, world.appointment.id, acknowledge_retention=True)

    assert response.status_code == 200 and response.json()["deposit_outcome"] is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_hub_release.py -q -k "deposit or acknowledg or refund or partial or unpaid or harmless"`
Expected: the 409 tests FAIL (`assert 200 == 409`: the release goes through and the clinic loses the money question).

- [ ] **Step 3: Implement the guard**

In `src/secretaria/api/hub/calendar.py` add the import `from secretaria.models.pix_deposit import PixDepositStatus` (next to the other `secretaria.models` imports) and, in `release_appointment`, immediately after the `already_confirmed` block and before `if appt.google_event_id:`, insert:

```python
    # Money first, Google second: a release whose deposit was PAID moves money
    # (retained / partially or fully refunded), so the clinic must have read the
    # consequence. Raised before anything is touched.
    deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appt.id)
    if (
        deposit is not None
        and deposit.status is PixDepositStatus.PAID
        and not body.acknowledge_retention
    ):
        outcome = deposit_lifecycle.preview_cancellation_outcome(tenant, deposit, appt)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            _detail(
                "retention_ack_required",
                deposit_lifecycle.release_warning_text(outcome, tenant, deposit),
                deposit_outcome=outcome,
                amount_cents=deposit.amount_cents,
            ),
        )
```

- [ ] **Step 4: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_hub_release.py tests/test_hub_calendar_money.py -q`
Expected: all pass.

- [ ] **Step 5: Lint, diff check, commit**

Run: `uvx ruff format tests/test_hub_release.py && uvx ruff check --fix tests/test_hub_release.py`; `uvx ruff check src/secretaria/api/hub/calendar.py`; `git diff --stat`.

```bash
git add src/secretaria/api/hub/calendar.py tests/test_hub_release.py
git commit -m "feat(hub): releasing a slot with a paid Pix deposit requires acknowledging the retention (TASK-032 R4)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 8: Tell the patient (`services/appointment_release.py`) with the rebook way back

**Files:**
- Create: `src/secretaria/services/appointment_release.py`
- Modify: `src/secretaria/api/hub/calendar.py` (`release_appointment`: deposit notice, notification, response)
- Modify (append tests): `tests/test_hub_release.py`

**Interfaces:**
- Consumes: `send_cancellation_notice` (arq job, unchanged: it already sends the three rebook buttons inside the 24 h window, the billed template outside it, claims `cancelnotice:<appointment_id>` so it never goes out twice, retries and escalates by e-mail); Task 5 `conversation_id_for`, `nudge_portal_patient`, `EMAIL_SENT`; `cancellation_notice.build_cancellation_text/join_blocks/last_inbound_at/is_inside_window`.
- Produces:
  - `RELEASE_JUSTIFICATION = "a consulta não foi confirmada a tempo e o horário foi liberado."`, `PORTAL_REBOOK_LINE = "Para marcar um novo horário, é só me escrever por aqui."`
  - `NOTICE_WHATSAPP_QUEUED = "whatsapp_queued"`, `NOTICE_WHATSAPP_OUTSIDE_WINDOW = "whatsapp_outside_window"`, `NOTICE_PORTAL_CHAT = "portal_chat"`, `NOTICE_PORTAL_CHAT_EMAIL = "portal_chat_email"`, `NOTICE_NO_CHANNEL = "no_channel"`, `NOTICE_QUEUE_UNAVAILABLE = "queue_unavailable"`, `NOTICE_FAILED = "notice_failed"`
  - `async notify_released_patient(session, tenant, appointment, patient, *, professional_name: str | None, justification: str | None, deposit_notice: str | None, allow_paid: bool, arq_pool, now: datetime | None = None) -> str` — returns one `NOTICE_*`; never raises; never undoes the release.
- Decisions pinned here: WhatsApp patients go through the **existing** `send_cancellation_notice` job (rebook buttons, window, idempotency, retries, escalation all inherited — nothing re-implemented); outside the window the job is **not** enqueued unless the clinic authorised the billed template (`notify_outside_window`), and the response says `whatsapp_outside_window` so the front can offer the free `wa.me` link from `cancel-preview`; Portal patients get a plain chat message (no buttons: the Portal does not decode `rebook*|` ids until R3) plus the e-mail nudge; a patient the clinic cannot reach is reported, never an error.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hub_release.py` (add `FakeWhatsAppClient`, `outbound_messages` to the `tests._reminders_v2` import; add `from secretaria.services import appointment_release` and `from secretaria.services import staff_patient_message as spm`; add `from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE`; add `from secretaria.services.email import EmailOutcome`):

```python
# ------------------------------------------------------------- patient notice


class _FakeArqPool:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail = fail

    async def enqueue_job(self, name: str, *args) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        self.calls.append((name, *args))


def _install_pool(pool) -> None:
    from secretaria.main import app

    app.state.arq_pool = pool


def _recent(hours: float):
    return datetime.now(UTC) - timedelta(hours=hours)


@pytest.fixture
def portal_mail(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    sent: list[tuple] = []

    async def _send(to, template, variables):
        sent.append((to, template, variables))
        return EmailOutcome.SENT

    monkeypatch.setattr(spm, "send_transactional_email_result", _send)
    monkeypatch.setattr(spm, "portal_conversation_link", lambda tenant_id: "https://portal/x")
    return sent


async def test_a_whatsapp_patient_is_told_through_the_existing_notice_job(  # noqa: F811
    client: AsyncClient, db, acting
):
    pool = _FakeArqPool()
    _install_pool(pool)
    world = await _setup(db, acting, last_inbound_at=_recent(1), professional_name="Dra. Ana")

    response = await _release(client, world.appointment.id)

    assert response.json()["patient_notice"] == "whatsapp_queued"
    assert pool.calls == [
        (
            "send_cancellation_notice",
            str(world.tenant.id),
            str(world.appointment.id),
            "Dra. Ana",
            appointment_release.RELEASE_JUSTIFICATION,
            None,
            False,
        )
    ]


async def test_the_clinics_own_reason_replaces_the_standard_sentence(client: AsyncClient, db, acting):  # noqa: F811
    pool = _FakeArqPool()
    _install_pool(pool)
    world = await _setup(db, acting, last_inbound_at=_recent(1))

    await _release(client, world.appointment.id, justification="  Remarcamos a agenda  ")

    assert pool.calls[0][4] == "Remarcamos a agenda"


async def test_outside_the_window_nothing_billed_is_sent_without_authorisation(  # noqa: F811
    client: AsyncClient, db, acting
):
    pool = _FakeArqPool()
    _install_pool(pool)
    world = await _setup(db, acting, last_inbound_at=_recent(30))

    response = await _release(client, world.appointment.id)

    assert response.status_code == 200
    assert response.json()["patient_notice"] == "whatsapp_outside_window"
    assert pool.calls == []  # the release stands; the front offers the free wa.me link


async def test_outside_the_window_with_authorisation_the_job_is_told_it_may_bill(  # noqa: F811
    client: AsyncClient, db, acting
):
    pool = _FakeArqPool()
    _install_pool(pool)
    world = await _setup(db, acting, last_inbound_at=_recent(30))

    response = await _release(client, world.appointment.id, notify_outside_window=True)

    assert response.json()["patient_notice"] == "whatsapp_queued"
    assert pool.calls[0][-1] is True


async def test_a_release_survives_a_dead_queue(client: AsyncClient, db, acting):  # noqa: F811
    world = await _setup(db, acting, last_inbound_at=_recent(1))

    no_pool = await _release(client, world.appointment.id)
    assert no_pool.status_code == 200
    assert no_pool.json()["patient_notice"] == "queue_unavailable"

    other = await _setup(db, acting, last_inbound_at=_recent(1))
    _install_pool(_FakeArqPool(fail=True))
    broken = await _release(client, other.appointment.id)
    assert broken.status_code == 200 and broken.json()["patient_notice"] == "queue_unavailable"
    assert (await _appointment(db, other.appointment.id)).status == AppointmentStatus.CANCELLED


async def test_the_notice_is_queued_once_even_if_the_slot_is_released_twice(  # noqa: F811
    client: AsyncClient, db, acting
):
    pool = _FakeArqPool()
    _install_pool(pool)
    world = await _setup(db, acting, last_inbound_at=_recent(1))

    await _release(client, world.appointment.id)
    await _release(client, world.appointment.id)

    assert len(pool.calls) == 1


async def test_the_deposit_sentence_rides_along_in_the_notice(client: AsyncClient, db, acting):  # noqa: F811
    pool = _FakeArqPool()
    _install_pool(pool)
    world = await _paid_world(db, acting, hours_ahead=2, last_inbound_at=_recent(1))

    await _release(client, world.appointment.id, acknowledge_retention=True)

    assert "retido" in pool.calls[0][5]


async def test_a_portal_patient_gets_a_chat_message_and_an_email_nudge(  # noqa: F811
    client: AsyncClient, db, acting, portal_mail
):
    pool = _FakeArqPool()
    _install_pool(pool)
    FakeWhatsAppClient.reset()
    world = await _setup(db, acting, channel=CHANNEL_BRAIN_MESSAGE, email="paciente@x.com")

    response = await _release(client, world.appointment.id)

    assert response.json()["patient_notice"] == "portal_chat_email"
    [row] = await outbound_messages(db, world.conversation.id)
    assert "desmarcou a sua consulta" in row.body
    assert appointment_release.RELEASE_JUSTIFICATION in row.body
    assert appointment_release.PORTAL_REBOOK_LINE in row.body
    assert portal_mail[0][1] == "clinic_message_patient"
    assert pool.calls == [] and FakeWhatsAppClient.all_sent() == []  # never WhatsApp


async def test_a_portal_patient_without_email_is_still_told_in_the_chat(  # noqa: F811
    client: AsyncClient, db, acting, portal_mail
):
    world = await _setup(db, acting, channel=CHANNEL_BRAIN_MESSAGE, email=None)

    response = await _release(client, world.appointment.id)

    assert response.status_code == 200 and response.json()["patient_notice"] == "portal_chat"
    assert portal_mail == []
    assert len(await outbound_messages(db, world.conversation.id)) == 1


async def test_a_failing_chat_write_does_not_undo_the_release(  # noqa: F811
    client: AsyncClient, db, acting, portal_mail, monkeypatch
):
    class _Boom:
        def __init__(self, **kwargs) -> None:
            pass

        async def send_text_message(self, to, body):
            raise RuntimeError("write failed")

    monkeypatch.setattr(appointment_release, "BrainMessageSender", _Boom)
    world = await _setup(db, acting, channel=CHANNEL_BRAIN_MESSAGE, email="paciente@x.com")

    response = await _release(client, world.appointment.id)

    assert response.status_code == 200 and response.json()["patient_notice"] == "notice_failed"
    assert (await _appointment(db, world.appointment.id)).status == AppointmentStatus.CANCELLED


async def test_an_unreachable_patient_is_reported_not_an_error(client: AsyncClient, db, acting, portal_mail):  # noqa: F811
    world = await _setup(db, acting, channel=CHANNEL_BRAIN_MESSAGE)
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.conversation_id = None
        await session.delete(await session.get(type(world.conversation), world.conversation.id))
        await session.commit()

    response = await _release(client, world.appointment.id)

    assert response.status_code == 200 and response.json()["patient_notice"] == "no_channel"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_hub_release.py -q -k "notice or portal or queue or unreachable or authorisation"`
Expected: FAIL — `KeyError: 'patient_notice'`/`"not_attempted" != "whatsapp_queued"` (the field exists with its default but nothing fills it).

- [ ] **Step 3: The service**

Create `src/secretaria/services/appointment_release.py`:

```python
"""Telling a patient the clinic freed their slot (TASK-032 R4, spec 4.4).

Two channels, two mechanisms, one rule: the release itself has already been
committed and must never be undone by a notice that cannot be delivered. This
module therefore never raises; it returns a short code the hub passes to the
front (`AppointmentReleaseRead.patient_notice`) so the clinic knows whether the
patient was told, and can write to them by hand when not.

* **WhatsApp** - reuses `workers/whatsapp/notifications.py::send_cancellation_notice`
  through arq, exactly like the hub's cancel endpoint: rebooking buttons inside
  the 24 h window, the billed template outside it (only with the clinic's
  `allow_paid`), idempotent through `cancelnotice:<appointment_id>`, retried and
  escalated by e-mail when every attempt fails. Nothing of that is re-implemented.
* **Portal (Brain-Message)** - the job is WhatsApp-only (it addresses `wa_id`/
  `phone`, and a Portal appointment's `phone` can hold the portal id), so the
  notice is a plain chat message written in the caller's session plus the generic
  e-mail nudge. No buttons yet: the Portal does not decode the `rebook*|` ids
  (R3 adds that); the text tells the patient to just write back.

Never logs a phone number, an e-mail address or the message text.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, MessageSender, Patient, Tenant
from secretaria.services import cancellation_notice
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, BrainMessageSender
from secretaria.services.staff_patient_message import (
    EMAIL_SENT,
    conversation_id_for,
    nudge_portal_patient,
)

logger = get_logger(__name__)

# Quoted by `build_cancellation_text` as the doctor's justification when the
# clinic typed none: it reads "Justificativa do médico: "a consulta não foi ...".
RELEASE_JUSTIFICATION = "a consulta não foi confirmada a tempo e o horário foi liberado."
PORTAL_REBOOK_LINE = "Para marcar um novo horário, é só me escrever por aqui."

NOTICE_WHATSAPP_QUEUED = "whatsapp_queued"
NOTICE_WHATSAPP_OUTSIDE_WINDOW = "whatsapp_outside_window"
NOTICE_PORTAL_CHAT = "portal_chat"
NOTICE_PORTAL_CHAT_EMAIL = "portal_chat_email"
NOTICE_NO_CHANNEL = "no_channel"
NOTICE_QUEUE_UNAVAILABLE = "queue_unavailable"
NOTICE_FAILED = "notice_failed"


async def notify_released_patient(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient | None,
    *,
    professional_name: str | None,
    justification: str | None,
    deposit_notice: str | None,
    allow_paid: bool,
    arq_pool,
    now: datetime | None = None,
) -> str:
    """Tell the patient; returns one `NOTICE_*` code. Never raises."""
    try:
        reason = (justification or "").strip() or RELEASE_JUSTIFICATION
        if patient is None or patient.tenant_id != tenant.id:
            return NOTICE_NO_CHANNEL
        if patient.channel == CHANNEL_BRAIN_MESSAGE:
            return await _notify_portal(
                session, tenant, appointment, patient, professional_name, reason, deposit_notice
            )

        to = patient.wa_id or appointment.phone
        if not to:
            return NOTICE_NO_CHANNEL
        last_inbound = await cancellation_notice.last_inbound_at(session, tenant.id, patient.id)
        if not cancellation_notice.is_inside_window(last_inbound, now=now) and not allow_paid:
            logger.info("release_notice_not_queued", reason="outside_window_not_authorised")
            return NOTICE_WHATSAPP_OUTSIDE_WINDOW
        if arq_pool is None:
            logger.warning("release_notice_not_queued", reason="no_queue")
            return NOTICE_QUEUE_UNAVAILABLE
        try:
            await arq_pool.enqueue_job(
                "send_cancellation_notice",
                str(tenant.id),
                str(appointment.id),
                professional_name,
                reason,
                deposit_notice,
                allow_paid,
            )
        except Exception as exc:
            logger.error(
                "release_notice_enqueue_failed",
                appointment_id=str(appointment.id),
                error_type=type(exc).__name__,
            )
            return NOTICE_QUEUE_UNAVAILABLE
        return NOTICE_WHATSAPP_QUEUED
    except Exception as exc:
        logger.error(
            "release_notice_failed",
            appointment_id=str(appointment.id),
            error_type=type(exc).__name__,
        )
        return NOTICE_FAILED


async def _notify_portal(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient,
    professional_name: str | None,
    reason: str,
    deposit_notice: str | None,
) -> str:
    conversation_id = await conversation_id_for(session, tenant.id, appointment, patient)
    if conversation_id is None:
        return NOTICE_NO_CHANNEL
    text = cancellation_notice.join_blocks(
        cancellation_notice.build_cancellation_text(professional_name, reason),
        deposit_notice,
        PORTAL_REBOOK_LINE,
    )
    try:
        sender = BrainMessageSender(
            conversation_id=conversation_id, session=session, author=MessageSender.HUMAN
        )
        await sender.send_text_message(to=patient.external_id or "", body=text)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.error(
            "release_portal_notice_failed",
            appointment_id=str(appointment.id),
            error_type=type(exc).__name__,
        )
        return NOTICE_FAILED
    nudge = await nudge_portal_patient(tenant, patient)
    return NOTICE_PORTAL_CHAT_EMAIL if nudge == EMAIL_SENT else NOTICE_PORTAL_CHAT
```

- [ ] **Step 4: Wire it into the endpoint**

In `src/secretaria/api/hub/calendar.py`: add `from secretaria.services import appointment_release` to the `secretaria.services` import line (keep it alphabetical: `appointment_release, cancellation_notice, reminder_schedule`). In `release_appointment` replace

```python
    await reminder_schedule.cancel_reminders(session, appt.id, reason="released")
    deposit_outcome = await deposit_lifecycle.on_appointment_cancelled(
        session, tenant=tenant, appointment=appt, waba_token=None
    )
    await session.commit()
    await session.refresh(appt)

    logger.info(
        "calendar_appointment_released",
        appointment_id=str(appt.id),
        deposit_outcome=deposit_outcome,
    )
    deposit_status = await _deposit_status_value(session, appt.id)
    read = _appointment_read(appt, deposit_status=deposit_status, deposit_outcome=deposit_outcome)
    return AppointmentReleaseRead(**read.model_dump())
```

with

```python
    await reminder_schedule.cancel_reminders(session, appt.id, reason="released")
    professional_name = await _professional_name(session, tenant, appt)
    deposit_outcome = await deposit_lifecycle.on_appointment_cancelled(
        session, tenant=tenant, appointment=appt, waba_token=None
    )
    deposit_notice: str | None = None
    if deposit_outcome is not None:
        resolved = await deposit_lifecycle.get_deposit_for_appointment(session, appt.id)
        if resolved is not None:
            deposit_notice = deposit_lifecycle.cancellation_notice(
                deposit_outcome, tenant, resolved
            )
    patient = await session.scalar(
        select(Patient).where(Patient.id == appt.patient_id, Patient.tenant_id == tenant.id)
    )
    await session.commit()

    # After the commit: the release stands whatever happens to the notice.
    patient_notice = await appointment_release.notify_released_patient(
        session,
        tenant,
        appt,
        patient,
        professional_name=professional_name,
        justification=body.justification,
        deposit_notice=deposit_notice,
        allow_paid=body.notify_outside_window,
        arq_pool=getattr(request.app.state, "arq_pool", None),
    )
    await session.refresh(appt)

    logger.info(
        "calendar_appointment_released",
        appointment_id=str(appt.id),
        deposit_outcome=deposit_outcome,
        patient_notice=patient_notice,
    )
    deposit_status = await _deposit_status_value(session, appt.id)
    read = _appointment_read(appt, deposit_status=deposit_status, deposit_outcome=deposit_outcome)
    return AppointmentReleaseRead(**read.model_dump(), patient_notice=patient_notice)
```

- [ ] **Step 5: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_hub_release.py tests/test_cancellation_notice.py tests/test_hub_calendar_money.py -q`
Expected: all pass (`test_cancellation_notice.py` proves the unchanged job is still what the hub enqueues).

- [ ] **Step 6: Lint, diff check, commit**

Run: `uvx ruff format src/secretaria/services/appointment_release.py tests/test_hub_release.py && uvx ruff check --fix src/secretaria/services/appointment_release.py tests/test_hub_release.py`; `uvx ruff check src/secretaria/api/hub/calendar.py`; `git diff --stat`.

```bash
git add src/secretaria/services/appointment_release.py src/secretaria/api/hub/calendar.py tests/test_hub_release.py
git commit -m "feat(hub): tell the patient when the clinic frees their slot, per channel (TASK-032 R4)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 9: `POST /appointments/{id}/message`

**Files:**
- Modify: `src/secretaria/schemas/calendar.py` (append `StaffMessageRequest`, `StaffMessageRead`; add `field_validator` to the pydantic import)
- Modify: `src/secretaria/api/hub/calendar.py` (route; imports; docstring)
- Test: `tests/test_hub_staff_message.py`

**Interfaces:**
- Consumes: Task 5 `staff_patient_message.send_staff_message` and its exceptions; `settings.CANCEL_TEMPLATE_COST_BRL` / `CANCEL_TEMPLATE_COST_IS_ESTIMATE`.
- Produces: the route and the exact wire shapes documented under "Produces for R5". Manual confirm needs no new route: R1's `PATCH .../status` with `{"status":"confirmed"}` already counts one staff confirmation.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hub_staff_message.py`:

```python
"""POST /appointments/{id}/message (TASK-032 R4, spec 4.4)."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub import calendar as hub_calendar
from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import Appointment, MessageSender, Tenant
from secretaria.services import staff_patient_message as spm
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.email import EmailOutcome
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import (
    FakeWhatsAppClient,
    fake_waba_token,
    outbound_messages,
    seed_world,
)

CALENDAR = "/tenants/me/calendar"


def _recent(hours: float):
    return datetime.now(UTC) - timedelta(hours=hours)


@pytest.fixture
def acting() -> dict:
    return {"tenant_id": None}


@pytest.fixture(autouse=True)
def _wire(db, acting, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, acting["tenant_id"])

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    FakeWhatsAppClient.reset()
    monkeypatch.setattr(spm, "WhatsAppClient", FakeWhatsAppClient)
    monkeypatch.setattr(spm, "get_waba_token", fake_waba_token)
    monkeypatch.setattr(spm, "portal_conversation_link", lambda tenant_id: "https://portal/x")

    async def _emit(**kwargs) -> bool:
        return True

    monkeypatch.setattr(spm, "emit_usage_event", _emit)
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


@pytest.fixture
def mail(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    sent: list[tuple] = []

    async def _send(to, template, variables):
        sent.append((to, template, variables))
        return EmailOutcome.SENT

    monkeypatch.setattr(spm, "send_transactional_email_result", _send)
    return sent


async def _world(db, acting, **kwargs):  # noqa: F811
    world = await seed_world(db, **kwargs)
    acting["tenant_id"] = world.tenant.id
    return world


async def _message(client: AsyncClient, appointment_id, text="Podemos confirmar?", **extra):
    return await client.post(
        f"{CALENDAR}/appointments/{appointment_id}/message", json={"text": text, **extra}
    )


async def test_inside_the_window_it_is_plain_text(client: AsyncClient, db, acting):  # noqa: F811
    world = await _world(db, acting, last_inbound_at=_recent(1))

    response = await _message(client, world.appointment.id, "  Podemos confirmar?  ")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["delivery"] == "whatsapp_text" and body["email_nudge"] is None
    assert body["message_id"]
    assert FakeWhatsAppClient.all_sent() == [("text", "5511988887777", "Podemos confirmar?")]
    [row] = await outbound_messages(db, world.conversation.id)
    assert row.sender == MessageSender.HUMAN


async def test_outside_the_window_it_is_refused_with_the_free_alternative(  # noqa: F811
    client: AsyncClient, db, acting
):
    world = await _world(db, acting, last_inbound_at=_recent(30))

    response = await _message(client, world.appointment.id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "outside_window_not_authorised"
    assert detail["whatsapp_link"] == "https://wa.me/5511988887777"
    assert "template_cost_brl" in detail and "message" in detail
    assert FakeWhatsAppClient.all_sent() == []


async def test_outside_the_window_authorised_it_is_the_template(client: AsyncClient, db, acting):  # noqa: F811
    world = await _world(db, acting, last_inbound_at=_recent(30))

    response = await _message(client, world.appointment.id, notify_outside_window=True)

    assert response.status_code == 200 and response.json()["delivery"] == "whatsapp_template"
    assert FakeWhatsAppClient.all_sent()[0][0] == "template"


async def test_a_portal_patient_without_email_is_messaged_in_the_chat(  # noqa: F811
    client: AsyncClient, db, acting, mail
):
    world = await _world(db, acting, channel=CHANNEL_BRAIN_MESSAGE, email=None)

    response = await _message(client, world.appointment.id)

    assert response.status_code == 200
    assert response.json()["delivery"] == "portal_chat"
    assert response.json()["email_nudge"] == "no_email"
    assert mail == [] and len(await outbound_messages(db, world.conversation.id)) == 1


async def test_a_portal_patient_with_email_is_nudged(client: AsyncClient, db, acting, mail):  # noqa: F811
    world = await _world(db, acting, channel=CHANNEL_BRAIN_MESSAGE, email="p@x.com")

    response = await _message(client, world.appointment.id)

    assert response.json()["email_nudge"] == "sent" and mail[0][0] == "p@x.com"


async def test_another_clinics_staff_gets_a_404_and_nothing_is_sent(client: AsyncClient, db, acting):  # noqa: F811
    mine = await seed_world(db, last_inbound_at=_recent(1))
    await _world(db, acting)  # the caller belongs to ANOTHER clinic

    response = await _message(client, mine.appointment.id)

    assert response.status_code == 404
    assert FakeWhatsAppClient.all_sent() == []
    assert await outbound_messages(db, mine.conversation.id) == []


async def test_an_appointment_without_a_patient_has_no_channel(client: AsyncClient, db, acting):  # noqa: F811
    world = await _world(db, acting)
    async with db() as session:
        await session.execute(
            update(Appointment).where(Appointment.id == world.appointment.id).values(patient_id=None)
        )
        await session.commit()

    response = await _message(client, world.appointment.id)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "no_channel"


async def test_a_refusal_by_the_channel_is_a_502_and_records_nothing(client: AsyncClient, db, acting):  # noqa: F811
    world = await _world(db, acting, last_inbound_at=_recent(1))
    FakeWhatsAppClient.fail_everything = True

    response = await _message(client, world.appointment.id)

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "delivery_failed"
    assert await outbound_messages(db, world.conversation.id) == []


@pytest.mark.parametrize("text", ["", "   ", "x" * 1001])
async def test_blank_or_huge_text_is_rejected(client: AsyncClient, db, acting, text):  # noqa: F811
    world = await _world(db, acting, last_inbound_at=_recent(1))

    response = await _message(client, world.appointment.id, text)

    assert response.status_code == 422
    assert FakeWhatsAppClient.all_sent() == []


async def test_it_works_with_the_clinic_switch_off(client: AsyncClient, db, acting):  # noqa: F811
    world = await _world(db, acting, v2=False, last_inbound_at=_recent(1))

    assert (await _message(client, world.appointment.id)).status_code == 200


async def test_malformed_ids_are_a_404(client: AsyncClient, db, acting):  # noqa: F811
    await _world(db, acting)

    assert (await _message(client, "nope")).status_code == 404
```

- [ ] **Step 2: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_hub_staff_message.py -q`
Expected: FAIL — `405 Method Not Allowed` (no route yet).

- [ ] **Step 3: Schemas**

In `src/secretaria/schemas/calendar.py` change `from pydantic import BaseModel, Field` to `from pydantic import BaseModel, Field, field_validator` and append:

```python


class StaffMessageRequest(BaseModel):
    """POST /appointments/{id}/message (TASK-032 R4, spec 4.4)."""

    text: str = Field(min_length=1, max_length=1000)
    # Authorises the BILLED template when a WhatsApp patient is outside Meta's
    # 24 h window (same meaning as AppointmentCancel.notify_outside_window).
    notify_outside_window: bool = False

    @field_validator("text")
    @classmethod
    def _trimmed_and_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value


class StaffMessageRead(BaseModel):
    """What happened to the staff message.

    `delivery`: whatsapp_text | whatsapp_template | portal_chat.
    `email_nudge`: only for portal_chat - sent | no_email | not_sent.
    """

    delivery: str
    email_nudge: str | None = None
    message_id: str | None = None
```

- [ ] **Step 4: The route**

In `src/secretaria/api/hub/calendar.py`: add to the module docstring `POST  /tenants/me/calendar/appointments/{id}/message - write to the appointment's patient (any channel).`; add `StaffMessageRead` and `StaffMessageRequest` to the `secretaria.schemas.calendar` import; add `staff_patient_message` to the `secretaria.services` import line (alphabetical). After `release_appointment` add:

```python
# ---------------------------------------------------------------------------
# POST /appointments/{id}/message — write to the patient on THEIR channel
# ---------------------------------------------------------------------------


@router.post("/appointments/{appointment_id}/message", response_model=StaffMessageRead)
async def message_patient(
    appointment_id: str,
    body: StaffMessageRequest,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> StaffMessageRead:
    """Free text from the clinic to the appointment's patient (spec 4.4).

    WhatsApp patient: free text inside Meta's 24 h window; outside it only the
    billed template and only with `notify_outside_window` (else 409 with the free
    `wa.me` alternative). Portal patient: a chat message plus a generic e-mail
    nudge when the patient has an address. All the channel logic lives in
    services/staff_patient_message.py; this route only maps its errors to HTTP.
    """
    appt = await _get_appointment(session, tenant, appointment_id)
    patient = None
    if appt.patient_id is not None:
        patient = await session.scalar(
            select(Patient).where(Patient.id == appt.patient_id, Patient.tenant_id == tenant.id)
        )
    try:
        result = await staff_patient_message.send_staff_message(
            session, tenant, appt, patient, body.text, allow_paid=body.notify_outside_window
        )
    except staff_patient_message.OutsideWindowError as exc:
        settings = get_settings()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            _detail(
                exc.code,
                "O paciente não escreveu nas últimas 24 horas: só é possível enviar um "
                "modelo aprovado (cobrado) ou escrever pelo seu próprio WhatsApp.",
                template_cost_brl=settings.CANCEL_TEMPLATE_COST_BRL,
                cost_is_estimate=settings.CANCEL_TEMPLATE_COST_IS_ESTIMATE,
                whatsapp_link=exc.whatsapp_link,
            ),
        ) from None
    except staff_patient_message.NoChannelError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            _detail(exc.code, "Não há como falar com este paciente por aqui."),
        ) from None
    except staff_patient_message.DeliveryFailedError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            _detail(exc.code, "Não foi possível entregar a mensagem. Tente de novo."),
        ) from None
    return StaffMessageRead(
        delivery=result.delivery,
        email_nudge=result.email_nudge,
        message_id=str(result.message_id) if result.message_id else None,
    )
```

- [ ] **Step 5: Run the tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_hub_staff_message.py tests/test_hub_release.py tests/test_staff_patient_message.py -q`
Expected: all pass.

- [ ] **Step 6: Lint, diff check, commit**

Run: `uvx ruff format tests/test_hub_staff_message.py && uvx ruff check --fix tests/test_hub_staff_message.py`; `uvx ruff check src/secretaria/api/hub/calendar.py src/secretaria/schemas/calendar.py`; `git diff --stat`.

```bash
git add src/secretaria/schemas/calendar.py src/secretaria/api/hub/calendar.py tests/test_hub_staff_message.py
git commit -m "feat(hub): POST /appointments/{id}/message writes to the patient on their channel (TASK-032 R4)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Task 10: Checkpoint doc and focused validation

**Files:**
- Create: `docs/CHECKPOINT_lembretes_r4.md`

**Interfaces:** none (documentation and the validation run).

- [ ] **Step 1: Focused validation (no full suite)**

Run, from Git Bash in the worktree:

```bash
BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_confirmation_warning_email.py tests/test_confirmation_warnings_select.py tests/test_confirmation_warnings_cron.py tests/test_deposit_release_preview.py tests/test_staff_patient_message.py tests/test_hub_release.py tests/test_hub_staff_message.py -q
BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_hub_calendar_money.py tests/test_hub_calendar_confirmation.py tests/test_cancellation_notice.py tests/test_appointment_status_taxonomy.py tests/test_email_transactional.py tests/test_build_identity.py -q
BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_reminder_schedule.py tests/test_reminder_confirmation.py tests/test_reminder_v2_engine.py tests/test_reminder_v2_hub_wiring.py -q
uvx ruff check src/secretaria/workers/confirmation_warnings.py src/secretaria/services/staff_patient_message.py src/secretaria/services/appointment_release.py src/secretaria/api/hub/calendar.py src/secretaria/schemas/calendar.py src/secretaria/services/email.py src/secretaria/services/appointment_status.py src/secretaria/services/payments/deposit_lifecycle.py src/secretaria/workers/arq_worker.py
git diff --stat main -- src/secretaria
```

Expected: every test passes (a failure in the second or third line is a regression to investigate, not to skip — separate it from a failure that already exists at HEAD with `git stash` evidence before reporting); ruff clean on the files this plan created, and no new findings on the lines it touched in the others; `git diff --stat` shows no whole-file diff on a CRLF file (a whole-file diff means line endings flipped: `git checkout -- <file>` and redo).

Also confirm the two money invariants by reading, not by running anything against production: `grep -n "on_appointment_cancelled" src/secretaria/api/hub/calendar.py` shows exactly three call sites (`cancel_appointment`, the PATCH `cancelled` branch, `release_appointment`), and `release_appointment` calls it exactly once, after the guarded UPDATE.

- [ ] **Step 2: Write the checkpoint**

Create `docs/CHECKPOINT_lembretes_r4.md`:

```markdown
# CHECKPOINT — Lembretes R4: avisos, liberar horário e mensagem (TASK-032)

**Estado:** implementado e validado localmente conforme o plano `docs/superpowers/plans/2026-10-03-lembretes-r4-avisos-e-liberar.md`; **não** deployado. Interruptor `reminders_v2_enabled` continua desligado em todas as clínicas.

## O que entrou onde

| Peça | Onde |
|---|---|
| Cron `process_confirmation_warnings` (a cada minuto): escolhe linhas vencidas, reivindica com `UPDATE ... WHERE warned_at IS NULL` e envia **um** e-mail por consulta | `workers/confirmation_warnings.py` |
| E-mail à clínica (`Tenant.contact_email`) e modelo `clinic_message_patient` | `services/email.py` |
| `POST /tenants/me/calendar/appointments/{id}/release` | `api/hub/calendar.py::release_appointment` |
| Aviso ao paciente depois de liberar (WhatsApp: job `send_cancellation_notice`; Portal: mensagem no chat + e-mail) | `services/appointment_release.py` |
| `POST /tenants/me/calendar/appointments/{id}/message` | `api/hub/calendar.py::message_patient`, `services/staff_patient_message.py` |
| Prévia do destino do sinal e texto de retenção para a clínica | `services/payments/deposit_lifecycle.py::preview_cancellation_outcome`, `release_warning_text` |
| Motivo `unconfirmed` no log de transição | `services/appointment_status.py` |

## Regras que não são óbvias

- `warned_at` é o marcador único: faz o aviso sair uma vez **e** deixa a agenda vermelha (`display_state == "attention"`, do R1). Ele é gravado antes do e-mail e nunca desfeito: sem endereço de alerta ou com SMTP fora do ar a agenda fica vermelha mesmo assim, e não há reenvio.
- Só linhas `sent`/`failed` geram aviso (contrato do R2). Confirmar, cancelar, liberar, encerrar ou o horário passar interrompe — inclusive entre a seleção e a reivindicação.
- Liberar **falha fechado**: se o Google recusar apagar o evento, 502 e nada muda (o delete é idempotente; 404/410 contam como sucesso). A exclusão acontece no calendário **dono** (o do profissional quando houver).
- Dois cliques/duas pessoas: o `UPDATE ... WHERE status IN (vivos)` tem um único vencedor; o perdedor recebe 409 `not_live` e não mexe no dinheiro nem avisa o paciente de novo.
- Sinal Pix **pago** exige `acknowledge_retention=true` (409 `retention_ack_required` com o texto, antes de tocar no Google). Sinal não pago não exige.
- Consulta já confirmada exige `release_confirmed=true` (409 `already_confirmed`).
- Mensagem a paciente de WhatsApp fora das 24 h só com `notify_outside_window=true` (modelo `REMINDER_TEMPLATE_NAME`, uma variável, cobrado, evento de uso `reminders`); sem isso, 409 com o link `wa.me`. Paciente do Portal: mensagem no chat + e-mail genérico (nunca com o texto) se houver e-mail.
- As duas ações da equipe valem com o interruptor desligado (são decisões manuais da clínica); o interruptor só governa o que o sistema faz sozinho.
- Mandar mensagem **não** assume a conversa (não liga o atendimento humano).

## Decisões tomadas sem consulta

1. Falha do Google ao liberar = recusar (502), não "cancelar mesmo assim" (diferente do botão do paciente, que prioriza o pedido do paciente).
2. Aviso ao paciente do Portal sem botões de remarcar: o Portal ainda não decodifica `rebook*|` (R3); o texto manda escrever de volta.
3. Qualquer sinal pago exige reconhecimento, também quando o estorno seria total (o texto muda; a regra do plano/spec é "sinal pago").
4. Um e-mail por consulta por varredura, mesmo que várias linhas vençam juntas (queda do cron).
5. Link da agenda = `DOCTOR_AGENDA_URL` + `consulta=<id>` (sem configuração nova).

## Pendências

- **R5 (front):** botões Liberar / Mensagem / Marcar como confirmado na consulta vermelha; abrir a gaveta pelo parâmetro `consulta`; usar `cancel-preview` para o custo/janela; tratar os códigos 409/422/502 documentados no plano.
- `PATCH .../status` com `cancelled` continua **sem** apagar o evento do Google (lacuna antiga, fora do R4): a agenda deve usar `/release` (ou `/cancel`) para liberar horário.
- `POST /cancel` do hub ainda apaga no calendário do tenant, não no do profissional (mesma lacuna; `_owning_calendar` já existe e pode ser reaproveitado).
- Deploy: ver o plano (seção "Deploy e liberação"); nada foi enviado, pushado ou publicado.
```

- [ ] **Step 3: Commit**

```bash
git add docs/CHECKPOINT_lembretes_r4.md
git commit -m "docs(reminders): R4 checkpoint (warnings, release, staff message) (TASK-032)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Decisions taken where the spec is silent (flag to the owner/Orchestrator)

1. **Google delete failure on release fails closed** (502, nothing changes). The alternative (cancel the row anyway, as the patient's own cancel button does) would leave the slot occupied while the agenda says "cancelled" — the exact defect the spec's §7 row describes.
2. **A paid deposit always needs `acknowledge_retention`**, even when the refund would be total (the text tells which case). Cheap to relax later to "only when retained/partial".
3. **`release_confirmed` guard**: a patient who confirmed between page load and click is not released silently.
4. **Warnings are best-effort on top of the red state**: `warned_at` is set before the e-mail and never rolled back; no retry on SMTP failure (a recovered SMTP would otherwise flood the clinic).
5. **One e-mail per appointment per tick** when several rows are due at once.
6. **Staff actions are not gated by `reminders_v2_enabled`.**
7. **Message to a patient does not take over the conversation** from the bot.
8. **Free text outside the window uses the clinic's existing one-variable reminder template** (`REMINDER_TEMPLATE_NAME`); if the wording of that approved template does not suit free text, a dedicated template is a follow-up that only changes the `template=` argument in `send_staff_message`.
9. **No `reason` column**: the `unconfirmed` reason lives in the transition log only (spec: "motivo `unconfirmed` fonte `hub`"); a column would be a new migration and a second source of truth.

## Spec gaps found

- §4.4 says "libera horário apaga o evento do Google (calendário dono)" but the existing hub cancel and the PATCH `cancelled` branch do not (PATCH deletes nothing; `/cancel` uses the tenant calendar). R4 fixes this only for `/release` and records the rest as pending.
- §4.4 does not say what an unreachable patient (Portal without conversation, no number) means for a release: R4 reports `patient_notice: "no_channel"` and still releases.
- §4.4 does not define the agenda link; R4 uses `DOCTOR_AGENDA_URL?consulta=<id>` and R5 must honour it.
- §5 criterion 8 ("+2 h, +2 h e +20 min") is R1/R2's `warn_due_at`; R4 only enforces it. A `delivery_failed` warning also requires `confirmation_count = 0`.

## Self-review

**Spec coverage (§4.4 backend):** warning cron, once per row, three moments, stops on confirm/cancel/release/past, switch OFF, no clinical detail, `delivery_failed` variant, link — Tasks 1-3. `attention` — already R1 (`warned_at`), made reachable by Task 3. Release (Google delete on owning calendar, cancelled/`unconfirmed`/hub, patient notice with rebook, `on_appointment_cancelled`, Pix acknowledgement 409 with text) — Tasks 4, 6, 7, 8. Message (WhatsApp window/template, Portal chat + e-mail) — Tasks 5, 9. Manual confirm — R1's PATCH, untouched. §5 criteria 8, 9, 10 — Tasks 3, 6-8. §7 rows "Horário liberado só no status…" and "Sinal Pix pago retido…" — Tasks 6, 7.

**Placeholder scan:** no TBD/TODO; every code step is complete; the two intentionally partial edits (imports in existing files) give the exact lines.

**Type consistency:** `WarningCandidate`/`due_candidates`/`claim_warning`/`run_warning_tick` (Tasks 2-3); `send_confirmation_warning_alert` kwargs identical in Tasks 1 and 3; `StaffMessageResult`/exception names identical in Tasks 5 and 9; `NOTICE_*` and `RELEASE_JUSTIFICATION` identical in Tasks 8's service and tests; `AppointmentRelease` flags identical in Tasks 6-8; `patient_notice` values identical to the "Produces for R5" block.

**Review Focus coverage:** items 1-3 Tasks 2/3; 4-5 Task 6; 6 Tasks 4/7; 7 Tasks 6/9; 8 Tasks 5/8/9; 9 Tasks 5/8/9; 10 Task 3; 11 Tasks 2/3/6/9; 12-13 Task 3; 14 Task 6; 15 Task 2; 16-17 Task 6.

## Deploy e liberação

Nothing in this plan deploys, pushes, runs against production, touches Easypanel, sends to Meta or reads environment variables. After the owner reviews the branch and explicitly asks, each step separately and in this order:

1. Merge order on `task/TASK-032-lembretes-e-confirmacao`: R1 → R2 → R4 (R4 touches `api/hub/calendar.py`, `services/email.py`, `workers/arq_worker.py` after R2).
2. No migration in R4. Deploy API **and** worker together (the new cron and the arq job registry live in the worker; the endpoints in the API) — `secretarIA` is two deploy units; deploying only the API leaves the warnings unsent. Check that the worker's startup log lists `cron:process_confirmation_warnings` (the registry log line `registered_cron_names`).
3. Operator checklist (done by the owner in the panel, never by automation): `DOCTOR_AGENDA_URL` set to the agenda URL of the frontend in use; SMTP configured (`SMTP_HOST` and its siblings) — without it the red state still works but no warning mail leaves; each clinic that should be warned has `contact_email` set.
4. Keep `reminders_v2_enabled` OFF everywhere; turn it ON first only for the test clinic, confirm in a real agenda that a reminded, unconfirmed appointment turns red and the e-mail arrives, then try `release` and `message` on a test appointment with a test patient (never on a real patient's slot).
5. Frontend (R5) is deployed after this API, never before.
