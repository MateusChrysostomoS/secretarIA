# Lembretes R7 — Ações da clínica na agenda avisam o paciente (API) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every clinic action on an appointment in the agenda tells the patient on the patient's own channel — staff "Confirmar" (a card with Confirmar / Cancelar / Alterar Dados that reuses the reminder tap flow), "Compareceu" (the post-consult message, once), the new single "Editar/Remarcar" (`POST …/edit`, before → after + the same three buttons) and "Cancelar" for Portal patients — with the 24 h / paid-template rules and a clinic-level standing authorisation for paid notices; plus the status guards (`no_show_before_start`, `not_live`). Plus the owner's second round of 2026-10-09 (spec §5, Tasks 13–16): **(A)** agenda visibility by role enforced in this backend — owner/manager and secretary see every doctor, a manager who is also a doctor can narrow to "só os meus", any other doctor sees and reaches only his own appointments (404 like a foreign id) and creates consultations/blocks only on his own agenda (owner's decision 2026-10-09: the doctor defaults to him, another doctor is 403 `professional_not_allowed`); **(B)** the extra-reminder lead (`reminder_extra_lead_minutes`) and the read-only `reminders_v2_enabled` on the hub configuration, a change replanning the still-pending extra reminders; **(C)** `GET /events` carries what Editar/Remarcar pre-fills (doctor, service, attendee, contact phone, patient channel). Backend only (secretarIA); R5 (front) consumes the "Produces for R5" section verbatim.

**Architecture:** One channel-aware sender for every clinic notice, `staff_patient_message.send_clinic_notice` (R4's module, generalised: Portal chat + e-mail nudge, WhatsApp free text / buttons inside 24 h, the one-variable paid template outside 24 h only when authorised, a usage event when billed) returning the R4 `NOTICE_*` vocabulary. The buttons of the confirm and edit cards point at a new kind of `appointment_reminders` row (`staff_confirm` / `staff_edit`), born already sent with no clinic warning, so the unchanged R6 tap handler (`workers/shared/reminder_actions.py::handle_reminder_button`) validates and counts them exactly like a reminder; the reconcile cron and the R4 warning cron exclude those kinds. The edit reuses the R6 row writer (extracted to `services/appointment_edit_write.py`), the R6 doctor e-mail outbox and the R2 reschedule hook; Google is moved first and fail-closed. Two additive columns (one migration): `tenants.paid_notices_auto_approved` and `appointments.post_consult_notified_at` (the once-per-appointment marker that also stops the next-open follow-up from repeating it). Visibility (Tasks 13–15): brain-api — the identity authority — answers `agenda_scope` (`"clinic"` | `"own"`) and `professional_id` on the hub-token introspection; `core/subscription.py` keeps them on the claim, `services/agenda_visibility.py::AgendaViewer` turns them into one value, `api/hub/deps.py::get_agenda_viewer` serves it to the agenda routes, `GET /events` filters by it and every appointment route resolves its id through `_get_appointment(..., viewer)`. Config (Task 16): `services/reminder_schedule.py::replan_custom_reminders` runs inside the configuration save's transaction.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async (Postgres in prod, in-memory SQLite in tests), Alembic, arq, WhatsApp Cloud API (`services/whatsapp.py`), Brain-Message sender (`services/channel_sender.py`), SMTP (`services/email.py`), pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md` (binding; owner's decisions of 2026-10-09). Earlier contracts this plan builds on: `docs/superpowers/plans/2026-10-03-lembretes-r4-avisos-e-liberar.md` (R4 — release/message, `NOTICE_*`), `docs/superpowers/plans/2026-10-07-lembretes-alterar-dados.md` (R6 — "Alterar Dados", doctor e-mail outbox), `docs/CHECKPOINT_lembretes_r4.md`, `docs/CHECKPOINT_lembretes_r6.md`. Spec §5 (A/B/C) is the owner's second round of the same day.

**Order across plans (owner decisions A/B/C):** execute `C:\TECH\BRAIN\brain-api\docs\superpowers\plans\2026-10-09-hub-token-papel-profissional.md` (brain-api: `agenda_scope` on the hub-token introspection) **first**, then **this plan** (R7, secretarIA), then the R5 front plan (`docs/superpowers/plans/2026-10-03-lembretes-r5-front-agenda.md`, to be updated from "Produces for R5"). This plan's tests do not need the brain-api change (Task 13 tolerates the old introspection answer). **Deploy order (no plan deploys anything):** `alembic upgrade head` (this repo, `d8e3a5c1f7b2`) → brain-api → `secretaria_api` + `secretaria-worker` together → Brain-Message-Frontend (R5).

**Code base (verified 2026-10-09):** `origin/main` 5e66c9d (R1, R2, R3, R4, R6, TASK-042). Single Alembic head `c2d5f8a1e4b6` (R6 outbox). All paths below are relative to the worktree; Python package root `src/secretaria/`.

**Execution context:** create a fresh worktree `C:\TECH\BRAIN-worktrees\TASK-044\secretarIA` from `origin/main` on branch `task/TASK-044-lembretes-r7-avisos` (TASK-044 = next free id after TASK-043); register it in `C:\TECH\BRAIN\tasks\TASK-044\TASK.md` (copy of `tasks/TEMPLATE.md`). Never run this plan in the main checkout (`C:\TECH\BRAIN\secretarIA`), which carries unrelated uncommitted edits.

## Global Constraints

- The spec is binding. Wire vocabulary for notices is R4's, extended by exactly one value the spec lists: `whatsapp_queued | whatsapp_sent | whatsapp_outside_window | portal_chat | portal_chat_email | no_channel | queue_unavailable | notice_failed`; `whatsapp_link` accompanies `whatsapp_outside_window`.
- Effective paid authorisation = request `notify_outside_window` **or** `Tenant.paid_notices_auto_approved` (default `false`), on every action that notifies: PATCH status (confirmed/attended), edit, cancel, R4 release, R4 message.
- Portal: no 24 h window; the message goes into the chat (author `HUMAN`, like R4) and a generic e-mail nudge (template `clinic_message_patient`, never the content). WhatsApp outside 24 h: only the paid one-variable template `Settings.REMINDER_TEMPLATE_NAME` (text without buttons) until Meta approves button templates (documented in Task 17, not created).
- The patient's identity is never changed by an edit: `Patient.wa_id`, `Patient.email`, `Patient.name` are untouched; "nome do paciente" = `Appointment.attendee_name`; "telefone de contato" = `Appointment.phone`. Every notice goes to `patient.wa_id` first (identity), the contact phone only as the existing fallback.
- Staff actions work regardless of `Tenant.reminders_v2_enabled`; that switch only decides whether a notice carries the three buttons (a tap on a switched-off clinic is ignored by today's handler, so no dead buttons are sent).
- Every query on appointments, patients, professionals, conversations and reminder rows filters by `tenant_id`; a foreign or malformed appointment id is **404** `{"detail":"Appointment not found"}` and nothing is touched or sent.
- Patient-facing and clinic-facing text in Portuguese; code, comments and log event names in English. Logs carry ids, codes, counts and error classes only — never a name, phone, e-mail, message text or link.
- Layering (`CLAUDE.md`): `api -> workers -> services -> models -> core`; `services/*` never imports `workers/*` or `plugins/*`. The hub route may import `workers/shared/appointment_edit_notification.py` / `appointment_edit_apply.py` (api is above workers). Channel branching lives only where R4 already put it (`services/staff_patient_message.py`, `services/appointment_release.py`); nothing is added to `workers/shared/**` that compares a channel (`tests/test_channel_branches.py`).
- One additive migration (Task 1): `tenants.paid_notices_auto_approved BOOLEAN NOT NULL DEFAULT false`, `appointments.post_consult_notified_at TIMESTAMPTZ NULL`. Tests pin a single head. Deploy order (recorded in the checkpoint, not executed): `alembic upgrade head` → `secretaria_api` **and** `secretaria-worker` together (`GET /build` `deploy_parity=match`).
- **Tests** run from Git Bash with the base interpreter (App Control blocks the venv's `python.exe`). Define once per shell:
  `pyt() { PYTHONPATH="src;.;.venv/Lib/site-packages" BOT_ALLOWLIST_WA_IDS="" /c/Users/mateu/AppData/Roaming/uv/python/cpython-3.12-windows-x86_64-none/python.exe -m pytest "$@"; }`
  Every "Run:" line below uses `pyt …`; if your shell does not keep functions between calls, paste the full form. Where it works, `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest <file> -q` is equivalent.
- Lint: never `ruff format .` (lint is red at HEAD). On files this plan **creates**: `uvx ruff format <file>` then `uvx ruff check --fix <file>`. On files it **modifies**: `uvx ruff check <file>` only, fixing findings in the lines you touched by hand. Files in the worktree are CRLF: after each task run `git diff --stat` and confirm only the touched lines changed (a whole-file diff means the line endings flipped — `git checkout -- <file>` and redo the edit). After every Write/Edit, grep the touched files for stray bidi/escape code points (memory `unicode-escape-virou-caractere-literal`): `LC_ALL=C.UTF-8 grep -nP '[\x{202A}-\x{202E}\x{2066}-\x{2069}]' <file>` must print nothing.
- Commit with `git add <explicit paths>` (never `git add -A`). Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. **Never push, merge, deploy, run a migration against any real database, create or submit a Meta template.**
- Run the full suite once at the end (Task 17) and record the count; separate pre-existing failures (prove them on `origin/main` with the same command) from regressions.
- **Agenda visibility (spec §5.A) is enforced here, never only in the UI.** Viewer = `services/agenda_visibility.py::AgendaViewer(scope, professional_id)` from brain-api's introspection (`agenda_scope` `"clinic"` | `"own"`, `professional_id`). `"own"`: `GET /events` returns only events whose local appointment belongs to that professional (no Google-only event, no appointment without a doctor, no other doctor's); every route that takes an appointment id answers the **same** 404 `{"detail":"Appointment not found"}` as a nonexistent id for anything else, before touching Google, the queue or the patient. A professional id that is not a row of this clinic is dropped (the viewer then reaches nothing). An `"own"` viewer creates consultations/blocks only on his own agenda (owner 2026-10-09): `POST /appointments` / `POST /blocks` default `professional_id` to his, refuse another (403 `professional_not_allowed`) and refuse him without a professional (422 `no_own_agenda`), all before Google. Google Calendar reads/writes are unchanged.
- **Rollout without brain-api's field (fail-closed choice):** `agenda_scope` absent or unknown → `"own"` when the token carries a `professional_id` (a doctor never sees more because a deploy is late), clinic-wide when it does not (a receptionist / manager without a professional row has no own agenda to narrow to; locking them out would stop the reception). Wrong-order symptom: a manager-doctor sees only his own and no toggle.
- Extra-reminder lead (spec §5.B): `reminder_extra_lead_minutes` `1500..20160` or `null` (off); `reminders_v2_enabled` is read-only on the hub (spec 4.5). A change replans, in the same transaction, only `pending` `custom` rows of live future appointments that already have a plan; sent rows stay; a new due that is not in the future cancels the row (never sent late).

## Consumes (verified against `origin/main` 5e66c9d)

- **R1/R2:** `models/appointment_reminder.py` (`AppointmentReminder`, kinds `custom/day/hour/chat`, statuses, channels `whatsapp/email/chat`, `REMINDER_ANSWER_CONFIRM`, unique partial index `(appointment_id, kind, appointment_start_at) WHERE invalidated_at IS NULL`); `services/reminder_schedule.py` (`MAX_CONFIRMATIONS = 2`, `register_confirmation(session, *, appointment, reminder_id, source, now) -> int`, `CONFIRMATION_SOURCE_STAFF`, `cancel_reminders`, `reset_confirmation`, `display_state`, `_as_utc`); `services/reminder_hooks.py` (`enabled_for`, `after_appointment_rescheduled`, `after_appointment_closed`, `reconcile_missing_reminders` — filters `kind != 'chat'` today); `services/reminder_text.py` (`ReminderContent`, `load_reminder_content`, `local_start`, `reminder_buttons(reminder_id)` → `remconfirm|<id>` / `remcancel|<id>` / `remedit|<id>`); `workers/reminder_engine.py::due_reminder_ids` (selects `status='pending'` only).
- **R3/R6 tap flow:** `workers/shared/reminder_actions.py::handle_reminder_button(reply, action, reminder_id)` — accepts any row kind; refuses foreign tenant/patient (`NOT_FOUND_TEXT`), non-live/started (`NOT_ACTIVE_TEXT`), an older start (`MOVED_TEXT`), invalidated/cancelled rows; returns `("reminder_edit", appointment_id)` for Alterar Dados; ignores every tap when `reminders_v2_enabled` is false. Driven in tests through `workers.tasks._handle_action_button(reply, action, value)`.
- **R4:** `services/staff_patient_message.py` (`conversation_id_for`, `nudge_portal_patient`, `EMAIL_SENT`, `send_staff_message`, `_one_line`, `_wam_id`), `services/appointment_release.py` (`notify_released_patient`, `_notify_portal`, `NOTICE_*`, `RELEASE_JUSTIFICATION`, `PORTAL_REBOOK_LINE`), `workers/confirmation_warnings.py::due_candidates(session, now)` (filters `kind != 'chat'`, `warn_due_at IS NOT NULL`), `api/hub/calendar.py` helpers `_get_appointment`, `_get_calendar`, `_professional_name`, `_detail`, `_owning_calendar`, `_appointment_read`, `_deposit_status_value`.
- **R6:** `workers/shared/appointment_edit_apply.py` (`apply_appointment_edit`, `_row_draft`, `AppliedEdit`), `services/appointment_edit.py` (`EditDraft`, `appointment_email_version`), `services/professional_edit_outbox.py::record_professional_edit(session, appointment, fields)`, `workers/shared/appointment_edit_notification.py::enqueue_professional_edit_notification(redis, tenant_id, applied)` (job `send_professional_edit_notification`, fields vocabulary `data, horário, serviço, médico, convênio, paciente`; the cron `dispatch_pending_professional_edits` recovers lost enqueues).
- **Existing:** `services/cancellation_notice.py` (`last_inbound_at`, `is_inside_window`, `whatsapp_deep_link`, `build_cancellation_text`, `meta_language_code`, `join_blocks`), `workers/whatsapp/notifications.py::send_cancellation_notice` (arq; args `tenant_id, appointment_id, professional_name, justification, extra_notice, allow_paid`), `services/insurance_catalog.py::resolve_booking_plan_ids`, `services/tenant_config.py` (`active_appointment_types`, `professional_appointment_types`, `resolve_professional_calendar`, `load_tenant_config`), `services/service_catalog.py` (`load_service_catalog`, `normalize`), `services/calendar.py` (`CalendarService.update_event_details/create_event/cancel_event/is_slot_free/references_same_calendar`, `CalendarUnavailableError`, `build_event_description`), `services/patient_context.py::find_post_consult_followup`, `schemas/config.py` (`TenantConfigRead`, `TenantConfigUpdate`), `services/hub_configuration.py` (`TENANT_SCALAR_FIELDS`, `tenant_read_model`).
- **Hub auth / brain-api (Tasks 13–15):** `api/hub/deps.py` (`get_current_tenant(authorization, session)`, `_bearer_token`) → `core/subscription.py::verify_subscription_token(token) -> SubscriptionClaim(tenant_id, active) | None` (positive answers cached in-process for `SUBSCRIPTION_CACHE_TTL_SECONDS`; the body is read with `body.get(...)`, unknown keys ignored). brain-api's `POST /internal/secretaria/hub-token/verify` answers today `{active, tenant_id, professional_id}` (`professional_id` = the acting user's secretarIA `Professional.id`, NULL for a secretary); after the brain-api plan also `agenda_scope: "clinic" | "own"`. `GET /events` (`api/hub/calendar.py::list_events`) reads ONE Google calendar (the tenant's, `_get_calendar`) and joins `appointments` by `google_event_id`, tenant-scoped, one query per page.
- **Config (Task 16):** `models/tenant.py` (`reminders_v2_enabled`, `reminder_extra_lead_minutes` — no DB constraint), `services/reminder_schedule.py` (`_planned_kinds`, `_arm`, `_WARN_AFTER`, `schedule_reminders` — idempotent, skips due ≤ now), `services/reminder_hooks.py::reconcile_missing_reminders` (plans only appointments with NO non-chat row of the current start), `services/hub_configuration.py::apply_tenant_config` (mutates, never commits; both `PUT /config` and `PUT /configuration` commit once after it).
- **Test helpers:** `tests/_reminder_fixtures.py` (`db`, `tenant`, `other_tenant`, `make_appointment`), `tests/_reminders_v2.py` (`NOW`, `WA_ID`, `FakeWhatsAppClient`, `fake_waba_token`, `seed_world`, `add_reminder`, `get_reminder`, `reload_appointment`, `outbound_messages`, `seed_paid_deposit`), `tests/_reminders_r3.py` (`wire`, `set_conversation`, `reminder_rows`), `tests/_edit_flow_support.py::draft_for`, `tests/test_appointment_edit_apply.py` (`_apply`, `_edit`), conftest `client`.

## Produces for R5 (front) — exact wire shapes

All paths are under `/tenants/me` and need the hub bearer token. Error bodies are FastAPI's `{"detail": {...}}` with a machine-readable `code` and a Portuguese `message` the front may show as is. An appointment id of another clinic, or a malformed id, is **404** `{"detail":"Appointment not found"}` on every route below.

**Notice vocabulary (`patient_notice`).** `null` = no notice was due for this action. Otherwise:

| value | meaning for the clinic | tone |
|---|---|---|
| `whatsapp_sent` | sent now on WhatsApp (free inside 24 h, or the paid template outside it when authorised) | success |
| `whatsapp_queued` | handed to the queue that sends it (cancel / release use the existing job) | success |
| `whatsapp_outside_window` | NOT sent: outside 24 h and not authorised; `whatsapp_link` (`https://wa.me/<digits>` or `null`) is the free alternative | warning |
| `portal_chat` | written in the Portal chat; no e-mail (no address or e-mail failed) | success |
| `portal_chat_email` | written in the Portal chat and the e-mail nudge left | success |
| `no_channel` | there is no way to reach this patient (no patient, no number, Portal patient without a conversation) | warning |
| `queue_unavailable` | the queue was down; NOT sent | danger |
| `notice_failed` | the channel refused; NOT sent | danger |

An unknown future value must be treated as success-neutral (R5 already does this for release).

**`AppointmentActionRead`** (response of PATCH status, POST edit, POST cancel) = the existing `AppointmentRead` keys (`id, tenant_id, patient_id, conversation_id, google_event_id, google_event_link, appointment_type, start_at, end_at, phone, status, confirmation_count, created_at, updated_at, deposit_status, deposit_outcome`) **plus** `patient_notice: string | null` and `whatsapp_link: string | null` (non-null only with `whatsapp_outside_window`).

**Clinic configuration.** `GET /tenants/me/config` → adds `"paid_notices_auto_approved": false|true`. Write it with the endpoint R5 already uses, `PUT /tenants/me/configuration` body `{"tenant": {"paid_notices_auto_approved": true}}` (200 → `HubConfigurationRead`, whose `tenant.paid_notices_auto_approved` echoes the stored value); the legacy `PUT /tenants/me/config` accepts the same key. Absent = unchanged; `null` = **422** (pydantic). "Não perguntar novamente" = send `true` once; "Configurações → voltar a perguntar" = send `false`.

**Deciding before acting (unchanged endpoint).** `GET /tenants/me/calendar/appointments/{id}/cancel-preview` → `{"inside_window": bool, "professional_name": str|null, "template_cost_brl": str, "cost_is_estimate": bool, "whatsapp_link": str|null}`. The front asks "avisar pago?" only when `inside_window` is false **and** `paid_notices_auto_approved` is false; with the flag on, the backend sends without asking. The question is meaningless for a Portal patient (`inside_window` false but no cost) — R5 decides channel from its own patient data as today.

**`PATCH /calendar/appointments/{id}/status`** — request `{"status": "scheduled"|"confirmed"|"rescheduled"|"cancelled"|"attended"|"no_show", "notify_outside_window": false}` (`notify_outside_window` optional, default false).
- 200 `AppointmentActionRead`. `patient_notice`: for `confirmed` — the confirm card's code, or `null` when the patient already received the staff-confirm card for this same date/time, or the appointment already started; for `attended` — the post-consult message's code, or `null` when it was already sent for this appointment; every other status → `null`.
- Transition rules: any live status (`scheduled/confirmed/rescheduled`) → anything; `attended` ↔ `no_show` correction allowed; repeating `attended` or `no_show` = 200 no-op (nothing re-sent, money untouched); `cancelled` target keeps today's behaviour.
- **409** `{"code":"no_show_before_start","message":"Só é possível marcar falta depois do horário da consulta.","status":"<current>","start_at":"<ISO UTC>"}`.
- **409** `{"code":"not_live","message":"Esta consulta já foi cancelada ou encerrada.","status":"<current>"}` — `confirmed/scheduled/rescheduled/attended/no_show` on a cancelled appointment, or a live status on an attended/no-show one.
- **422** pydantic for an unknown status value.

**`POST /calendar/appointments/{id}/edit`** (Editar/Remarcar) — request, every field optional but at least one of the six editable ones must be present (`extra` keys forbidden):
```json
{
  "start_at": "2026-10-20T13:30:00Z",
  "service": "Retorno",
  "professional_id": "<uuid>",
  "insurance": "Unimed",
  "attendee_name": "João Pedro",
  "phone": "+55 (11) 98888-7777",
  "notify_outside_window": false,
  "allow_overlap": false
}
```
- `start_at`: timezone-aware ISO datetime (naive → 422); must be in the future. The duration is the new service's `duration_min` when the service changes, else the appointment's current duration. `start_at`, `service`, `professional_id` cannot be `null`.
- `service`: a service name the (new or current) professional offers; matched by the clinic's canonical spelling (accents/case ignored); the stored name is the canonical one.
- `professional_id`: an **active** professional of this clinic.
- `insurance`: convênio label (`"Unimed"`, `"Particular"`, a typed name); `null` or `""` clears it (shown as "não informado"). Plan ids are re-resolved by the TASK-006/008 catalogue rules.
- `attendee_name`: who the appointment is for; `null` or `""` = the patient themself.
- `phone`: the appointment's contact phone, with country code (12–15 digits after stripping non-digits); stored as digits; `null` or `""` clears it. Never changes the patient's WhatsApp number or login e-mail.
- `allow_overlap`: `true` skips the "slot is free and inside the hours" check (a deliberate "encaixe").
- 200 `AppointmentActionRead` (status becomes `rescheduled` when date or time changed, and `confirmation_count` returns to 0 then); `patient_notice` is always a code here (never `null`).
- **409** `{"code":"not_live","message":...,"status":"<current>"}`; **409** `{"code":"slot_unavailable","message":"Este horário não está livre na agenda."}` (resend with `allow_overlap:true` if the clinic insists); **409** `{"code":"appointment_changed","message":"A consulta foi alterada por outra pessoa. Recarregue e tente de novo."}` (another edit won meanwhile; Google was put back — reload the appointment); **409** `{"code":"calendar_unresolved","message":...}`; **422** `{"code":"not_a_patient_appointment",...}` (a block); **422** `{"code":"nothing_changed","message":"Nada foi alterado."}`; **422** `{"code":"start_in_past",...}`; **422** `{"code":"unknown_professional",...}`; **422** `{"code":"service_not_offered",...}`; **422** `{"code":"invalid_phone","message":"Informe o telefone com DDI e DDD, por exemplo 5511988887777."}`; **422** plain string `"Google Calendar not connected. Complete OAuth onboarding first."`; **502** `{"code":"calendar_unavailable","message":...}` (Google refused: nothing changed, safe to retry); **422** pydantic (empty body, unknown key, naive `start_at`, `null` on start/service/professional).
- `POST …/reschedule` keeps working unchanged until R5 migrates (it still returns `AppointmentRead` and notifies only through `custom_message`).

**`POST /calendar/appointments/{id}/cancel`** — request unchanged `{"confirm": true, "justification": null|str, "notify_outside_window": false}`. 200 `AppointmentActionRead`: WhatsApp patient → `whatsapp_queued` (inside 24 h, or authorised) or `whatsapp_outside_window` (+ `whatsapp_link`); Portal patient → `portal_chat` / `portal_chat_email`; no number → `no_channel`; queue down → `queue_unavailable`. Errors unchanged (409 plain `"Appointment already cancelled"`, 422 plain `"confirm must be true"`).

**`POST /calendar/appointments/{id}/release`** (R4) — request unchanged; response `AppointmentReleaseRead` gains `"whatsapp_link": str|null` (non-null with `whatsapp_outside_window`); `notify_outside_window` is now OR-ed with the clinic flag.

**`POST /calendar/appointments/{id}/message`** (R4) — request and response unchanged; with `paid_notices_auto_approved=true` a WhatsApp patient outside 24 h gets the paid template (200 `delivery:"whatsapp_template"`) instead of 409 `outside_window_not_authorised`.

**`GET /calendar/events`** — `reminders[].kind` may now also be `"staff_confirm"` (the clinic's confirmation card) or `"staff_edit"` (the clinic's change card); both are born `status:"sent"`, never warn (`warned_at` stays null) and carry the patient's `answer` like any reminder. Suggested labels: "Confirmação da clínica", "Alteração da clínica".

**Who is looking — `GET /calendar/viewer`** (Task 13; call it once when the agenda opens):
```json
{"agenda_scope": "clinic", "professional_id": "<uuid>", "professional_name": "Dra. Ana", "can_filter_own": true}
```
- `agenda_scope`: `"clinic"` (sees every doctor) | `"own"` (only `professional_id`'s appointments; with `professional_id: null` sees none). `can_filter_own` = `agenda_scope == "clinic"` and a linked professional → show the "Todos / Só os meus" switch only then. Never show other doctors' filters/labels to `"own"`. 401 as any hub route.

**`GET /calendar/events?start=&end=&mine=true|false`** (Task 14) — `mine` optional, default `false`:
- `mine=true` with `can_filter_own` → only that professional's appointments ("Só os meus"); `mine=false` → everything (unchanged).
- `"own"` viewer: always only his own, whatever `mine` says (no 4xx).
- `mine=true` for a clinic viewer without a professional (a receptionist) → **422** `{"code":"no_own_agenda","message":"Seu usuário não está ligado a um profissional da clínica."}`.
- When narrowed (`"own"` or `mine=true`), Google events with no local appointment (blocks typed in Google, events created outside the hub) and appointments without a doctor are **omitted**, not anonymised.
- Each item gains six keys (all `null` for an event with no local appointment) — the Editar/Remarcar pre-fill:

| key | value |
|---|---|
| `professional_id` | `str` (uuid) of the appointment's doctor in THIS clinic, else `null` |
| `professional_name` | `str` \| `null` (same rule) |
| `service` | `str` \| `null` — the stored service name (`appointment_type`; a hub block reads `"Bloqueado"`) → edit's `service` |
| `attendee_name` | `str` \| `null` — who the appointment is for (`null` = the patient themself) → edit's `attendee_name` |
| `phone` | `str` \| `null` — the appointment's CONTACT phone, digits (never the patient's identity) → edit's `phone` |
| `patient_channel` | `"whatsapp"` \| `"brain_message"` \| `null` — the patient's own channel; `null` = no patient record (a block or a phone-only booking: only cancel can still reach a WhatsApp number) |

`insurance` (already there) pre-fills edit's `insurance`; `start` pre-fills `start_at`.

**Every route that takes an appointment id** (`cancel-preview`, `cancel`, `release`, `message`, `reschedule`, `status`, `edit`) — for an `"own"` viewer, another doctor's appointment, an appointment without a doctor, or any appointment when he has no professional → **404** `{"detail":"Appointment not found"}`, byte-identical to a nonexistent id; nothing is touched or sent. Clinic viewers: unchanged.

**`POST /calendar/appointments`** and **`POST /calendar/blocks`** (Task 15; owner's decision 2026-10-09) — both bodies gain an optional `"professional_id": "<uuid>" | null` (absent = `null`); everything else and the 201 `AppointmentRead` response are unchanged. Checked **before** Google is touched:
- `"own"` viewer: `professional_id` absent/`null` → the appointment/block is created on **his own** agenda (`professional_id` = his); equal to his → same; **any other** id → **403** `{"detail":{"code":"professional_not_allowed","message":"Você só pode marcar consultas e bloqueios na sua própria agenda."}}`, nothing created; an `"own"` viewer with `professional_id: null` in `GET /viewer` → **422** `{"detail":{"code":"no_own_agenda","message":"Seu usuário não está ligado a um profissional da clínica."}}`.
- `"clinic"` viewer: absent/`null` → no doctor (today's behaviour); an id → must be an **active** professional of this clinic, else **422** `{"detail":{"code":"unknown_professional","message":"Este profissional não pertence a esta clínica."}}`.
- Front: for `"own"`, show "Nova consulta"/"Bloquear horário", preselect and lock the doctor to `GET /viewer`'s `professional_id`/`professional_name` and send it; for `"own"` with `professional_id: null`, disable both buttons (he has no agenda). A malformed uuid is a pydantic **422**.

**`POST /calendar/appointments/{id}/edit`** — `"own"` viewer sending a `professional_id` other than his own → **403** `{"detail":{"code":"professional_not_allowed","message":"Você só pode manter a consulta na sua própria agenda. Peça à recepção para trocar o médico."}}`, nothing changed; in the form, lock "Médico" to himself for `"own"`.

**Clinic configuration — reminders** (Task 16; exactly the shape R5 Tasks 9–10 already expect):
- `GET /tenants/me/config` (and `HubConfigurationRead.tenant`) adds `"reminders_v2_enabled": bool` (read-only) and `"reminder_extra_lead_minutes": int | null`.
- Write with `PUT /tenants/me/configuration` `{"tenant": {"reminder_extra_lead_minutes": 2880}}` (or legacy `PUT /tenants/me/config`): integer `1500..20160` (more than the 1-day reminder, at most 14 days); `null` = no extra reminder; absent = unchanged; out of range / not an integer → **422** pydantic, nothing saved. `reminders_v2_enabled` in a PUT body is ignored (never written).
- Effect: already-planned extra reminders of future appointments that are still pending move to the new time (or are cancelled when switched off or when the new time already passed); sent ones stay as history; the agenda's `reminders[]` shows the new `due_at` on the next `GET /events`.

## Decisions taken where the spec is silent (for the owner)

1. **Where the config field lives:** on the shared `TenantConfigRead`/`TenantConfigUpdate`, so it is read by `GET /tenants/me/config` and written by `PUT /tenants/me/configuration` (the endpoint R5 already uses) and the legacy `PUT /config` — one schema, no drift. `null` is refused (422) so the column can never be emptied.
2. **How a staff card's buttons get a valid row:** two new `appointment_reminders` kinds, `staff_confirm` and `staff_edit`, born `sent`, `warn_due_at = NULL`, one active row per appointment start (the R1 unique index), reused when the same start is notified again (the patient's answer is kept, so the same card never counts twice). The R6 tap handler is unchanged. The reconcile cron and the R4 warning cron exclude these kinds explicitly; the reminder engine never sees them (they are never `pending`). A card that could not be delivered retires its fresh row so a retry creates a new one.
3. **Staff "Confirmar" notifies once per appointment time:** a second click (or a confirm after the patient already has this card) answers `patient_notice: null`. A confirm after the start sends nothing (the buttons would be refused).
4. **Buttons only when they can work:** reminders switch ON, fewer than 2 confirmations, appointment not started. Otherwise the same text goes without buttons.
5. **PATCH transitions (minimal):** live → anything; `attended ↔ no_show` as a correction; repeating `attended`/`no_show` is a no-op; `cancelled` is not guarded (today's behaviour, including its money hook). A `no_show → attended` correction does **not** reverse a retained deposit (refunds stay manual in Asaas).
6. **Post-consult push:** `post_consult_message` verbatim when configured, else "Como foi a sua consulta com <médico>? Conte pra gente como você está se sentindo. Se precisar de algo, é só responder por aqui." Text only (no buttons). Claimed with `appointments.post_consult_notified_at` before sending and released if it was not delivered — so the existing next-open follow-up still covers a patient the clinic chose not to pay for; `find_post_consult_followup` ignores an appointment whose marker is set.
7. **Edit, "nome do paciente":** `Appointment.attendee_name` only. **Telefone:** digits with country code (12–15), stored as digits; no country guessing.
8. **Edit checks the slot** (free + inside the hours, like R6) unless `allow_overlap: true`; never touches the Pix deposit and never consumes the patient's reschedule allowance (same as today's `/reschedule`). A paid deposit does not block a clinic edit.
9. **Doctor e-mail** only when date, time or doctor changed (spec), through the R6 outbox; contact-phone changes are never e-mailed.
10. **Cancel parity:** the WhatsApp side is byte-for-byte today's enqueue (same job, same args, same `appointment.phone` condition — the job still decides the window); the response only reports what will happen. Portal patients now get the cancellation text in the chat plus the e-mail nudge.
11. **`/message` keeps its R4 response shape** (`delivery`/`email_nudge`); only the authorisation changes. **`/reschedule` is untouched.**
12. **Confirm/edit/attended notices are sent synchronously by the API** (like R4 `/message`), hence the `whatsapp_sent` code; release/cancel keep the existing queued job (`whatsapp_queued`).
13. **Doctor naming in texts:** "com <nome do profissional>" or, without a professional, "com a equipe da <clínica>" (R2's rule).
14. **Who decides the role:** brain-api, LIVE from the acting user's row at every introspection (new `agenda_scope`), not a role claim in the token and not secretarIA re-deriving the taxonomy (`manager`, `doctor`+`is_owner`/`is_manager`, legacy `tenant_owner`, `secretary` → `"clinic"`; anything else → `"own"`). A promotion/demotion counts within the 60 s positive cache.
15. **Rollout fail-closed rule:** missing/unknown `agenda_scope` + a `professional_id` → `"own"`; without a professional → clinic-wide (today's behaviour). A `professional_id` that is not a row of THIS clinic is dropped, so the viewer reaches nothing rather than trusting a foreign id.
16. **Non-appointment events for a restricted doctor:** omitted (not shown as anonymous "busy"). `GET /events` reads only the clinic's calendar (unchanged), so those events are clinic-level blocks or things typed into Google whose title can carry another patient's name; the doctor's own calendar is not read today, so "his busy blocks" cannot be shown without changing Google behaviour, which the owner kept as is.
17. **Appointments without a doctor** (`professional_id` NULL) are invisible to a restricted doctor (no proof they are his).
18. **A restricted doctor creates consultations and blocks only on his own agenda** (owner's decision 2026-10-09, replaces the earlier "cannot create" proposal): `POST /appointments` and `POST /blocks` take an optional `professional_id`; for an `"own"` viewer a missing one defaults to his (so the new row is visible in his own view at once), a different one is 403 `professional_not_allowed`, and an `"own"` viewer with no professional of his own is 422 `no_own_agenda`. He still cannot hand his appointment to another doctor in Editar/Remarcar (403 `professional_not_allowed`, same code). A clinic-wide viewer may name any **active** professional of this clinic (422 `unknown_professional` otherwise) or none — omitting it keeps today's doctor-less hub booking. The Google event still goes to the clinic's calendar (`_get_calendar`, unchanged: the owner kept Google behaviour as is).
19. **"Todos / Só os meus"** is a read filter (`mine` on `GET /events`), not a permission: a manager-doctor acting on any appointment stays allowed. A receptionist asking `mine=true` gets 422 `no_own_agenda` (a front bug, not silently "all").
20. **Extra-reminder bounds `1500..20160` minutes**, the shape R5 already expects: it must be more than the 1-day reminder (1440) so the two never coincide, and at most 14 days. `reminders_v2_enabled` stays read-only.
21. **A lead change touches already-planned future rows:** only `pending` `custom` rows of live future appointments; re-armed in place (same row id, so a button already out is impossible — pending rows were never sent); cancelled + invalidated when switched off or when the new due is not in the future (never sent late, like `schedule_reminders`); created only for appointments that already have a day/hour plan (an appointment with no plan is left to the reconcile cron, which plans all kinds with the new lead — creating only `custom` there would hide it from the cron and lose its day/hour reminders). Same transaction as the configuration save.
22. **Event pre-fill fields** (`professional_id/name`, `service`, `attendee_name`, `phone`, `patient_channel`) ride the SAME appointments query (outer join to `patients`, tenant-scoped) plus at most one `professionals` query per page; `patient_channel` is null without a patient record.

## Review Focus

Each line is pinned by a test in the task named in brackets.

1. Double click / repeated "Confirmar" for the same time sends exactly one card; the second response says `patient_notice: null` [Task 7].
2. Staff confirm (count 1) then the patient taps Confirmar on that card → count 2 and no further prompts; a tap on a card whose time was later edited answers "horário antigo" and counts nothing [Tasks 7, 11].
3. Clinic with reminders switched OFF: the confirm/edit text goes without buttons (no dead buttons) [Task 7].
4. "Compareceu" twice, or "Compareceu" then the patient opens the Portal the next day → one post-consult message; not delivered (outside 24 h, not authorised) → the marker is released [Task 8].
5. Edit of only the contact phone of a WhatsApp patient: Google untouched, no doctor e-mail, notice to the patient's own `wa_id` [Tasks 10, 11]; a Portal patient whose appointment got a contact phone is cancelled → chat, never the WhatsApp job [Task 12].
6. Clinic edit while the patient has an "Alterar Dados" draft open → the patient's later confirm of the stale draft writes nothing [Task 11].
7. Google refuses during the edit → 502 and the row is untouched; the database write fails after Google → Google is undone [Task 10].
8. `paid_notices_auto_approved = true`: release queues with `allow_paid=True`, message sends the template, confirm sends the template — none asks [Tasks 4, 7].
9. `no_show` before the start, and any resurrection of a cancelled appointment, are 409 with nothing changed (no money hook, no notice) [Task 6].
10. A professional of another clinic in the edit is 422 `unknown_professional`; a foreign appointment is 404 with Google untouched [Tasks 10, 11].
11. The staff rows never reach the clinic warning cron, never block the reconcile backfill and never turn the agenda red [Task 5].
12. **Cross-doctor leak:** a restricted doctor never receives another doctor's appointment — not in `GET /events` (nor a Google-only event, nor an appointment without a doctor), and every read/action by id (all seven routes) answers the same 404 as a nonexistent id with Google, queue, chat and e-mail untouched [Tasks 14, 15].
13. **Manager-doctor toggle:** `mine=true` narrows to his own, `mine=false` shows all, a restricted doctor cannot widen with `mine=false`, a receptionist's `mine=true` is 422 `no_own_agenda` [Task 14].
14. **Token without role during rollout:** an introspection answer without `agenda_scope` restricts a token carrying a professional to it and keeps a token without one clinic-wide; an unknown scope value reads as absent; a professional of another clinic is dropped; no live claim for this clinic is 401 [Task 13].
15. **Extra-lead change:** only pending future `custom` rows move; sent rows untouched; a due already past is cancelled, never sent late; an appointment with no plan is left to the cron (its day/hour reminders still get planned); another clinic and a switched-off clinic are untouched [Task 16].
16. **Extra-lead input:** `1499`, `20161`, `0`, `-5` → 422 with nothing saved; `null` turns it off; `reminders_v2_enabled` in a PUT is never written [Task 16].
17. **A restricted doctor creating:** no `professional_id` → the row is his own; his own id → accepted; another doctor's id → 403 `professional_not_allowed` with Google untouched and no row; no professional of his own → 422 `no_own_agenda`; a clinic viewer naming another clinic's or an inactive professional → 422 `unknown_professional`, naming none → no doctor as today [Task 15].

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `migrations/versions/d8e3a5c1f7b2_clinic_action_notices.py` | create | the two additive columns |
| `src/secretaria/models/tenant.py` | modify | `paid_notices_auto_approved` |
| `src/secretaria/models/appointment.py` | modify | `post_consult_notified_at` |
| `src/secretaria/schemas/config.py` | modify | the flag on `TenantConfigRead`/`TenantConfigUpdate` |
| `src/secretaria/services/hub_configuration.py` | modify | `TENANT_SCALAR_FIELDS`, read model |
| `src/secretaria/services/staff_patient_message.py` | modify (append) | `NOTICE_*` home, `NoticeResult`, `paid_notice_authorised`, `send_clinic_notice` |
| `src/secretaria/services/appointment_release.py` | modify | import `NOTICE_*` from above; `notify_cancelled_patient` (Task 12) |
| `src/secretaria/models/appointment_reminder.py` | modify | kinds `staff_confirm`/`staff_edit`, `REMINDER_KINDS_STAFF`, `REMINDER_KINDS_UNPLANNED` |
| `src/secretaria/services/reminder_schedule.py` | modify (append) | `current_staff_notice_row`, `ensure_staff_notice_row`, `retire_staff_notice_row` |
| `src/secretaria/services/reminder_hooks.py` | modify | reconcile excludes unplanned kinds |
| `src/secretaria/workers/confirmation_warnings.py` | modify | warning selection excludes unplanned kinds |
| `src/secretaria/services/appointment_status.py` | modify (append) | `staff_transition`, `StaffTransitionRefused` |
| `src/secretaria/services/clinic_action_notice.py` | create | confirm / post-consult / edit texts and the card notice |
| `src/secretaria/services/patient_context.py` | modify | follow-up skips an appointment already notified |
| `src/secretaria/services/appointment_edit_write.py` | create | the row writer shared by R6 and R7 (`row_draft`, `write_appointment_edit`) |
| `src/secretaria/workers/shared/appointment_edit_apply.py` | modify | delegates the write to the shared writer |
| `src/secretaria/services/staff_appointment_edit.py` | create | validate, move Google fail-closed, write the row, outbox |
| `src/secretaria/schemas/calendar.py` | modify | `AppointmentActionRead`, `AppointmentEdit`, `notify_outside_window` on status, `whatsapp_link` on release |
| `src/secretaria/api/hub/calendar.py` | modify | flag on release/message, PATCH guards + notices, `POST …/edit`, cancel parity |
| `tests/_r7_support.py` | create | hub wiring, calendar double, arq double, seeds |
| `tests/test_migration_clinic_action_notices.py` | create | migration + head |
| `tests/test_flow_edit_draft_column.py`, `tests/test_migration_professional_edit_notices.py` | modify | head pin moves to the new revision |
| `tests/test_hub_config_paid_notices.py` | create | the config flag |
| `tests/test_clinic_notice_delivery.py` | create | `send_clinic_notice` |
| `tests/test_hub_release.py`, `tests/test_hub_staff_message.py` | modify (append) | the flag on R4 paths |
| `tests/test_staff_notice_rows.py` | create | staff rows and their inertness |
| `tests/test_appointment_reminder_model.py` | modify | kinds vocabulary |
| `tests/test_hub_status_guards.py` | create | PATCH guards |
| `tests/test_hub_calendar_confirmation.py` | modify | three R1 tests that encoded "no validation" |
| `tests/test_hub_staff_confirm_notice.py` | create | staff confirm card end to end |
| `tests/test_hub_attended_post_consult.py` | create | attended push + follow-up marker |
| `tests/test_appointment_edit_write.py` | create | the shared writer |
| `tests/test_staff_appointment_edit.py` | create | the edit service |
| `tests/test_hub_edit_appointment.py` | create | `POST …/edit` end to end |
| `tests/test_hub_cancel_portal_notice.py` | create | cancel parity |
| `src/secretaria/core/subscription.py` | modify | `SubscriptionClaim.professional_id/agenda_scope`, parsed fail-safe (Task 13) |
| `src/secretaria/services/agenda_visibility.py` | create | `AgendaViewer`, `CLINIC_WIDE`, `viewer_from_claim` — the rule's one home (Task 13) |
| `src/secretaria/api/hub/deps.py` | modify | `get_agenda_viewer` (Task 13) |
| `src/secretaria/schemas/calendar.py` | modify | `AgendaViewerRead` (Task 13); six pre-fill keys on `CalendarEventRead` (Task 14); optional `professional_id` on `AppointmentCreate`/`BlockCreate` (Task 15) |
| `src/secretaria/api/hub/calendar.py` | modify | `GET /viewer` (13); `list_events` scoping + `mine` + pre-fill (14); `_get_appointment(..., viewer)` on every id route, `_creation_professional` on create/blocks, edit hand-over guard (15) |
| `src/secretaria/services/reminder_schedule.py` | modify (append) | `CustomReplan`, `replan_custom_reminders` (Task 16) |
| `src/secretaria/schemas/config.py`, `src/secretaria/services/hub_configuration.py` | modify | `reminders_v2_enabled` (read), `reminder_extra_lead_minutes` (read/write, replan) (Task 16) |
| `tests/conftest.py` | modify | default clinic-wide viewer for every test (Task 13) |
| `tests/_agenda_viewer.py` | create | `view_as(viewer)` (Task 13) |
| `tests/test_subscription.py` | modify (append) | claim parsing (Task 13) |
| `tests/test_agenda_viewer.py` | create | rule, dependency, `GET /viewer` (Task 13) |
| `tests/test_hub_events_visibility.py` | create | events scoping, `mine`, pre-fill keys (Task 14) |
| `tests/test_calendar_events_insurance.py`, `tests/test_hub_calendar_confirmation.py` | modify | event key-set pins grow by the six keys (Task 14) |
| `tests/test_hub_agenda_scope_actions.py` | create | 404 on every id route, create on his own agenda, edit hand-over guard (Task 15) |
| `tests/test_reminder_lead_replan.py` | create | `replan_custom_reminders` (Task 16) |
| `tests/test_hub_config_reminder_lead.py` | create | config read/write/validation + replan wiring (Task 16) |
| `docs/CHECKPOINT_lembretes_r7.md` | create | state, what lives where, decisions, Meta templates, deploy order |
| `docs/LEMBRETES_MODELOS_META.md` | modify (append) | the templates R7 would need for buttons outside 24 h |
| `CLAUDE.md` | modify | one pointer line |

---

## Task 1: The two columns (migration + models)

**Files:**
- Create: `migrations/versions/d8e3a5c1f7b2_clinic_action_notices.py`
- Modify: `src/secretaria/models/tenant.py` (after `reminder_extra_lead_minutes`)
- Modify: `src/secretaria/models/appointment.py` (after `last_confirmed_at`)
- Modify: `tests/test_flow_edit_draft_column.py` (`test_the_migration_is_the_single_head_on_top_of_r3`), `tests/test_migration_professional_edit_notices.py` (first assertion)
- Test: `tests/test_migration_clinic_action_notices.py`

**Interfaces:**
- Consumes: Alembic head `c2d5f8a1e4b6`.
- Produces: `Tenant.paid_notices_auto_approved: Mapped[bool]` (default False), `Appointment.post_consult_notified_at: Mapped[datetime | None]`; revision `d8e3a5c1f7b2` (the single head).

- [ ] **Step 1: Write the failing test**

Create `tests/test_migration_clinic_action_notices.py`:

```python
"""tenants.paid_notices_auto_approved + appointments.post_consult_notified_at (TASK-032 R7)."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from secretaria.models import Appointment, Tenant
from tests._reminder_fixtures import db  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
REVISION = "d8e3a5c1f7b2"
DOWN_REVISION = "c2d5f8a1e4b6"


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_clinic_action_notices.py"
    spec = importlib.util.spec_from_file_location("clinic_action_notices_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_is_the_single_head_on_top_of_the_edit_outbox():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [REVISION]
    assert _migration().down_revision == DOWN_REVISION


def test_upgrade_adds_both_columns_with_safe_values_and_downgrade_removes_them(tmp_path):
    migration = _migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'r7.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE tenants (id CHAR(32) PRIMARY KEY)"))
        conn.execute(sa.text("CREATE TABLE appointments (id CHAR(32) PRIMARY KEY)"))
        conn.execute(sa.text("INSERT INTO tenants (id) VALUES ('t')"))
        conn.execute(sa.text("INSERT INTO appointments (id) VALUES ('a')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            tenants = {c["name"]: c for c in sa.inspect(conn).get_columns("tenants")}
            appointments = {c["name"]: c for c in sa.inspect(conn).get_columns("appointments")}
            assert tenants["paid_notices_auto_approved"]["nullable"] is False
            assert appointments["post_consult_notified_at"]["nullable"] is True
            flag = conn.execute(sa.text("SELECT paid_notices_auto_approved FROM tenants")).scalar()
            assert flag in (0, False)
            marker = conn.execute(
                sa.text("SELECT post_consult_notified_at FROM appointments")
            ).scalar()
            assert marker is None
            migration.downgrade()
            assert "paid_notices_auto_approved" not in {
                c["name"] for c in sa.inspect(conn).get_columns("tenants")
            }
            assert "post_consult_notified_at" not in {
                c["name"] for c in sa.inspect(conn).get_columns("appointments")
            }
    engine.dispose()


async def test_the_models_map_the_columns_with_their_defaults(db):  # noqa: F811
    assert Tenant.__table__.c.paid_notices_auto_approved.nullable is False
    assert Appointment.__table__.c.post_consult_notified_at.nullable is True
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clínica", phone_number_id=None)
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        assert tenant.paid_notices_auto_approved is False
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pyt tests/test_migration_clinic_action_notices.py -q`
Expected: FAIL — `FileNotFoundError` for the migration file and `AttributeError: ... has no attribute 'paid_notices_auto_approved'`.

- [ ] **Step 3: Write the migration**

Create `migrations/versions/d8e3a5c1f7b2_clinic_action_notices.py`:

```python
"""R7: clinic actions on the agenda notify the patient - two additive columns.

TASK-032 R7 (docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md):

    ADD tenants.paid_notices_auto_approved     BOOLEAN NOT NULL DEFAULT false
    ADD appointments.post_consult_notified_at  TIMESTAMPTZ NULL

`paid_notices_auto_approved` is the clinic's standing "não perguntar novamente" for
billed WhatsApp notices outside the 24 h window (spec §2). `post_consult_notified_at`
marks that the post-consult message of THIS appointment was delivered after the
clinic marked "Compareceu", so it is never sent twice and the next-open follow-up
(services/patient_context.py::find_post_consult_followup) does not repeat it.

Both are metadata-only on Postgres 11+ (constant default / nullable). DEPLOY ORDER -
the database moves FIRST (the ORM names every mapped column on every read):

    1. `alembic upgrade head` from the NEW image (one-off), both services still old;
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity).

Rollback: the OLD code on both services first, then `alembic downgrade`. Data lost:
the clinic's auto-approval choice and the post-consult markers (the follow-up then
behaves exactly as before R7).

Revision ID: d8e3a5c1f7b2
Revises: c2d5f8a1e4b6
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8e3a5c1f7b2"
down_revision: str | None = "c2d5f8a1e4b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "paid_notices_auto_approved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "appointments",
        sa.Column("post_consult_notified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appointments", "post_consult_notified_at")
    op.drop_column("tenants", "paid_notices_auto_approved")
```

- [ ] **Step 4: Map the columns**

In `src/secretaria/models/tenant.py`, right after the `reminder_extra_lead_minutes` column, insert:

```python
    # TASK-032 R7 (spec 2026-10-09 §2): the clinic's standing yes ("não perguntar
    # novamente") to BILLED WhatsApp notices outside Meta's 24 h window. With it, every
    # clinic action that notifies the patient sends the paid template without asking;
    # without it, each action asks (`notify_outside_window`). Editable in the hub
    # configuration (schemas/config.py).
    paid_notices_auto_approved: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), default=False
    )
```

In `src/secretaria/models/appointment.py`, right after the `last_confirmed_at` column, insert:

```python
    # TASK-032 R7: when the post-consult message was delivered because the clinic marked
    # "Compareceu" (services/clinic_action_notice.py::notify_attended). Claimed before the
    # send and cleared when it could not be delivered - one message per appointment, and
    # the next-open follow-up skips an appointment that already got it.
    post_consult_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

- [ ] **Step 5: Move the two older head pins**

In `tests/test_flow_edit_draft_column.py`, inside `test_the_migration_is_the_single_head_on_top_of_r3`, replace:

```python
    assert scripts.get_heads() == ["c2d5f8a1e4b6"]
```

with:

```python
    assert len(scripts.get_heads()) == 1  # the head itself is pinned by the newest migration
```

In `tests/test_migration_professional_edit_notices.py`, replace:

```python
    assert scripts.get_heads() == [migration.revision]
```

with:

```python
    heads = scripts.get_heads()
    assert len(heads) == 1
    assert migration.revision in {r.revision for r in scripts.walk_revisions(head=heads[0])}
```

- [ ] **Step 6: Run the tests**

Run: `pyt tests/test_migration_clinic_action_notices.py tests/test_flow_edit_draft_column.py tests/test_migration_professional_edit_notices.py tests/test_migration_reminders_foundation.py -q`
Expected: PASS.

- [ ] **Step 7: Lint and commit**

```bash
uvx ruff format migrations/versions/d8e3a5c1f7b2_clinic_action_notices.py tests/test_migration_clinic_action_notices.py
uvx ruff check --fix migrations/versions/d8e3a5c1f7b2_clinic_action_notices.py tests/test_migration_clinic_action_notices.py
uvx ruff check src/secretaria/models/tenant.py src/secretaria/models/appointment.py tests/test_flow_edit_draft_column.py tests/test_migration_professional_edit_notices.py
git diff --stat
git add migrations/versions/d8e3a5c1f7b2_clinic_action_notices.py src/secretaria/models/tenant.py src/secretaria/models/appointment.py tests/test_migration_clinic_action_notices.py tests/test_flow_edit_draft_column.py tests/test_migration_professional_edit_notices.py
git commit -m "feat(reminders): R7 columns - paid notice auto-approval and post-consult marker (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 2: The clinic flag in the hub configuration

**Files:**
- Modify: `src/secretaria/schemas/config.py` (`TenantConfigUpdate`, `TenantConfigRead`)
- Modify: `src/secretaria/services/hub_configuration.py` (`TENANT_SCALAR_FIELDS`, `tenant_read_model`)
- Test: `tests/test_hub_config_paid_notices.py`

**Interfaces:**
- Consumes: `Tenant.paid_notices_auto_approved` (Task 1).
- Produces: `TenantConfigRead.paid_notices_auto_approved: bool`; `TenantConfigUpdate.paid_notices_auto_approved: bool | None` (explicit `null` → 422) on `GET/PUT /tenants/me/config` and `PUT /tenants/me/configuration`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_hub_config_paid_notices.py`:

```python
"""The clinic's standing authorisation for paid notices on the hub config (TASK-032 R7)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.models import Tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402, F401

CONFIG = "/tenants/me/config"
CONFIGURATION = "/tenants/me/configuration"


@pytest_asyncio.fixture
async def clinic(db) -> Tenant:  # noqa: F811
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clínica", phone_number_id=None)
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


@pytest.fixture(autouse=True)
def _override(db, clinic):  # noqa: F811
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, clinic.id)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _stored(db, tenant_id) -> bool:  # noqa: F811
    async with db() as session:
        return (await session.get(Tenant, tenant_id)).paid_notices_auto_approved


async def test_get_config_exposes_the_flag_off_by_default(client: AsyncClient):
    body = (await client.get(CONFIG)).json()
    assert body["paid_notices_auto_approved"] is False


async def test_the_configuration_save_r5_uses_turns_it_on_and_echoes_it(
    client: AsyncClient, db, clinic  # noqa: F811
):
    response = await client.put(
        CONFIGURATION, json={"tenant": {"paid_notices_auto_approved": True}}
    )

    assert response.status_code == 200, response.text
    assert response.json()["tenant"]["paid_notices_auto_approved"] is True
    assert await _stored(db, clinic.id) is True
    assert (await client.get(CONFIG)).json()["paid_notices_auto_approved"] is True


async def test_the_legacy_config_save_turns_it_back_off(client: AsyncClient, db, clinic):  # noqa: F811
    await client.put(CONFIGURATION, json={"tenant": {"paid_notices_auto_approved": True}})

    response = await client.put(CONFIG, json={"paid_notices_auto_approved": False})

    assert response.status_code == 200
    assert response.json()["paid_notices_auto_approved"] is False
    assert await _stored(db, clinic.id) is False


async def test_a_save_that_omits_it_leaves_it_untouched(client: AsyncClient, db, clinic):  # noqa: F811
    await client.put(CONFIGURATION, json={"tenant": {"paid_notices_auto_approved": True}})

    response = await client.put(CONFIGURATION, json={"tenant": {"persona_notes": "Gentil"}})

    assert response.status_code == 200
    assert await _stored(db, clinic.id) is True


async def test_null_is_refused_and_nothing_changes(client: AsyncClient, db, clinic):  # noqa: F811
    response = await client.put(
        CONFIGURATION, json={"tenant": {"paid_notices_auto_approved": None}}
    )

    assert response.status_code == 422
    assert await _stored(db, clinic.id) is False
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pyt tests/test_hub_config_paid_notices.py -q`
Expected: FAIL — `KeyError: 'paid_notices_auto_approved'` on the GET body.

- [ ] **Step 3: Implement the schema**

In `src/secretaria/schemas/config.py`, inside `TenantConfigUpdate`, right after `pix_reschedule_limit: ...` and before `is_active: bool | None = None`, insert:

```python
    # TASK-032 R7 (spec 2026-10-09 §2/§3): "não perguntar novamente" - the clinic's
    # standing yes to BILLED WhatsApp notices outside the 24 h window. Absent = left
    # untouched; an explicit null is refused below (the column is NOT NULL).
    paid_notices_auto_approved: bool | None = None
```

In the same class, right after the `_check_timezone` validator, add:

```python
    @field_validator("paid_notices_auto_approved")
    @classmethod
    def _paid_flag_is_true_or_false(cls, value: bool | None) -> bool:
        if value is None:
            raise ValueError("paid_notices_auto_approved must be true or false")
        return value
```

In `TenantConfigRead`, right after `asaas_connected: bool`, add:

```python
    # TASK-032 R7: the clinic's standing authorisation for billed notices (see
    # TenantConfigUpdate). Defaulted so an older reader building this model never 500s.
    paid_notices_auto_approved: bool = False
```

- [ ] **Step 4: Implement the write and the read**

In `src/secretaria/services/hub_configuration.py`, append `"paid_notices_auto_approved",` as the last entry of the `TENANT_SCALAR_FIELDS` tuple (after `"pix_reschedule_limit",`). In `tenant_read_model`, right after `asaas_connected=asaas_connected,` add:

```python
        paid_notices_auto_approved=bool(tenant.paid_notices_auto_approved),
```

- [ ] **Step 5: Run the tests**

Run: `pyt tests/test_hub_config_paid_notices.py tests/test_hub_config.py tests/test_hub_config_pix.py tests/test_hub_configuration.py -q`
Expected: PASS.

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff format tests/test_hub_config_paid_notices.py && uvx ruff check --fix tests/test_hub_config_paid_notices.py
uvx ruff check src/secretaria/schemas/config.py src/secretaria/services/hub_configuration.py
git diff --stat
git add src/secretaria/schemas/config.py src/secretaria/services/hub_configuration.py tests/test_hub_config_paid_notices.py
git commit -m "feat(hub): expose paid_notices_auto_approved on the clinic configuration (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 3: One sender for every clinic notice (`send_clinic_notice`)

**Files:**
- Modify: `src/secretaria/services/staff_patient_message.py` (append; imports)
- Modify: `src/secretaria/services/appointment_release.py` (the `NOTICE_*` constants now come from `staff_patient_message`)
- Test: `tests/test_clinic_notice_delivery.py`

**Interfaces:**
- Consumes: R4's `conversation_id_for`, `nudge_portal_patient`, `EMAIL_SENT`, `_one_line`, `_wam_id`; `cancellation_notice.last_inbound_at/is_inside_window/whatsapp_deep_link/meta_language_code`; `Settings.REMINDER_TEMPLATE_NAME`.
- Produces (all in `secretaria.services.staff_patient_message`):
  - constants `NOTICE_WHATSAPP_QUEUED = "whatsapp_queued"`, `NOTICE_WHATSAPP_SENT = "whatsapp_sent"`, `NOTICE_WHATSAPP_OUTSIDE_WINDOW = "whatsapp_outside_window"`, `NOTICE_PORTAL_CHAT = "portal_chat"`, `NOTICE_PORTAL_CHAT_EMAIL = "portal_chat_email"`, `NOTICE_NO_CHANNEL = "no_channel"`, `NOTICE_QUEUE_UNAVAILABLE = "queue_unavailable"`, `NOTICE_FAILED = "notice_failed"`, `DELIVERED_NOTICES: frozenset[str]` (queued, sent, portal_chat, portal_chat_email); `TEMPLATE_LINE_MAX_CHARS = 900`.
  - `@dataclass(frozen=True) class NoticeResult: code: str; whatsapp_link: str | None = None; message_id: UUID | None = None` with property `delivered -> bool`.
  - `def paid_notice_authorised(tenant, requested: bool) -> bool`.
  - `async def send_clinic_notice(session, tenant, appointment, patient, *, body: str, buttons: list[tuple[str, str]] | None = None, allow_paid: bool, usage_key: str, now: datetime | None = None) -> NoticeResult` — never raises; commits on delivery; on a Portal write failure rolls back; on non-delivery otherwise writes nothing.
  - `appointment_release.NOTICE_*` keep resolving (re-exported names).

- [ ] **Step 1: Write the failing test**

Create `tests/test_clinic_notice_delivery.py`:

```python
"""send_clinic_notice: every clinic-action notice on the patient's own channel (TASK-032 R7)."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from secretaria.models import Appointment, Conversation, MessageSender, Patient, Tenant
from secretaria.services import appointment_release, staff_patient_message as spm
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.email import EmailOutcome
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import (
    NOW,
    WA_ID,
    FakeWhatsAppClient,
    fake_waba_token,
    outbound_messages,
    seed_world,
)

BUTTONS = [("remconfirm|r1", "Confirmar"), ("remcancel|r1", "Cancelar"), ("remedit|r1", "Alterar Dados")]
BODY = "Seu médico confirmou sua consulta.\nVocê está ciente?"


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch):
    FakeWhatsAppClient.reset()
    monkeypatch.setattr(spm, "WhatsAppClient", FakeWhatsAppClient)
    monkeypatch.setattr(spm, "get_waba_token", fake_waba_token)
    monkeypatch.setattr(spm, "portal_conversation_link", lambda tenant_id: "https://portal/x")


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

    async def _send(to, template, variables) -> EmailOutcome:
        sent.append((to, template, variables))
        return EmailOutcome.SENT

    monkeypatch.setattr(spm, "send_transactional_email_result", _send)
    return sent


async def _notice(db, world, *, buttons=None, allow_paid=False, patient=True):  # noqa: F811
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        who = await session.get(Patient, world.patient.id) if patient else None
        return await spm.send_clinic_notice(
            session,
            tenant,
            appointment,
            who,
            body=BODY,
            buttons=buttons,
            allow_paid=allow_paid,
            usage_key="test",
            now=NOW,
        )


def test_the_vocabulary_is_r4s_plus_whatsapp_sent_and_release_reexports_it():
    assert spm.NOTICE_WHATSAPP_SENT == "whatsapp_sent"
    assert appointment_release.NOTICE_WHATSAPP_QUEUED == spm.NOTICE_WHATSAPP_QUEUED
    assert appointment_release.NOTICE_PORTAL_CHAT_EMAIL == spm.NOTICE_PORTAL_CHAT_EMAIL
    assert spm.DELIVERED_NOTICES == {
        "whatsapp_queued",
        "whatsapp_sent",
        "portal_chat",
        "portal_chat_email",
    }


@pytest.mark.parametrize(
    ("requested", "standing", "expected"),
    [(False, False, False), (True, False, True), (False, True, True), (True, True, True)],
)
def test_paid_authorisation_is_the_request_or_the_clinics_standing_yes(
    requested, standing, expected
):
    tenant = SimpleNamespace(paid_notices_auto_approved=standing)
    assert spm.paid_notice_authorised(tenant, requested) is expected


async def test_whatsapp_inside_the_window_sends_the_buttons_and_records_the_card(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))

    result = await _notice(db, world, buttons=BUTTONS)

    assert result.code == "whatsapp_sent" and result.delivered and result.whatsapp_link is None
    assert FakeWhatsAppClient.all_sent() == [("buttons", WA_ID, BODY, BUTTONS)]
    [row] = await outbound_messages(db, world.conversation.id)
    assert row.sender == MessageSender.HUMAN and row.wam_id == "wamid.buttons"
    assert row.interactive["kind"] == "buttons"
    assert [o["id"] for o in row.interactive["options"]] == [b[0] for b in BUTTONS]
    assert result.message_id == row.id
    assert usage == []


async def test_whatsapp_inside_the_window_without_buttons_is_plain_text(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))

    result = await _notice(db, world)

    assert result.code == "whatsapp_sent"
    assert FakeWhatsAppClient.all_sent() == [("text", WA_ID, BODY)]


async def test_outside_the_window_without_authorisation_nothing_leaves(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=30))

    result = await _notice(db, world, buttons=BUTTONS)

    assert result.code == "whatsapp_outside_window" and not result.delivered
    assert result.whatsapp_link == f"https://wa.me/{WA_ID}"
    assert FakeWhatsAppClient.all_sent() == []
    assert await outbound_messages(db, world.conversation.id) == []


async def test_outside_the_window_with_authorisation_the_paid_template_carries_one_line(
    db, usage  # noqa: F811
):
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=30))

    result = await _notice(db, world, buttons=BUTTONS, allow_paid=True)

    assert result.code == "whatsapp_sent"
    [(kind, to, template, lang, variables, payloads)] = FakeWhatsAppClient.all_sent()
    assert (kind, to, template, lang) == ("template", WA_ID, "appointment_reminder", "pt_BR")
    assert variables == ["Seu médico confirmou sua consulta. Você está ciente?"]
    assert payloads is None  # no approved button template yet: the text goes alone
    [event] = usage
    assert event["feature"] == "reminders" and event["event_id"].startswith("test:")


async def test_a_refusing_channel_is_notice_failed_and_records_nothing(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    FakeWhatsAppClient.fail_everything = True

    result = await _notice(db, world)

    assert result.code == "notice_failed"
    assert await outbound_messages(db, world.conversation.id) == []


async def test_no_patient_or_a_foreign_patient_is_no_channel(db):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    assert (await _notice(db, world, patient=False)).code == "no_channel"

    other = await seed_world(db, phone_number_id="pnid-2", wa_id="5511900000002")
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        foreign = await session.get(Patient, other.patient.id)
        result = await spm.send_clinic_notice(
            session, tenant, appointment, foreign, body=BODY, allow_paid=True, usage_key="t"
        )
    assert result.code == "no_channel"
    assert FakeWhatsAppClient.all_sent() == []


async def test_portal_writes_the_card_in_the_chat_and_nudges_by_email(db, mail):  # noqa: F811
    world = await seed_world(
        db, channel=CHANNEL_BRAIN_MESSAGE, wa_id=None, email="maria@exemplo.com"
    )

    result = await _notice(db, world, buttons=BUTTONS)

    assert result.code == "portal_chat_email" and result.delivered
    [row] = await outbound_messages(db, world.conversation.id)
    assert row.sender == MessageSender.HUMAN
    assert row.interactive["kind"] == "buttons"
    assert result.message_id == row.id
    [(to, template, variables)] = mail
    assert (to, template) == ("maria@exemplo.com", "clinic_message_patient")
    assert BODY not in str(variables)  # the nudge never carries the content
    assert FakeWhatsAppClient.all_sent() == []


async def test_portal_without_email_is_still_delivered_in_the_chat(db, mail):  # noqa: F811
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE, wa_id=None, email=None)

    result = await _notice(db, world)

    assert result.code == "portal_chat" and mail == []
    assert len(await outbound_messages(db, world.conversation.id)) == 1


async def test_portal_without_a_conversation_is_no_channel(db, mail):  # noqa: F811
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE, wa_id=None, email="a@b.c")
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.conversation_id = None
        await session.delete(await session.get(Conversation, world.conversation.id))
        await session.commit()

    result = await _notice(db, world)

    assert result.code == "no_channel" and mail == []


async def test_the_patient_number_wins_over_the_appointment_contact_phone(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.phone = "5511977776666"
        await session.commit()

    await _notice(db, world)

    assert FakeWhatsAppClient.all_sent()[0][1] == WA_ID
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pyt tests/test_clinic_notice_delivery.py -q`
Expected: FAIL — `AttributeError: module 'secretaria.services.staff_patient_message' has no attribute 'NOTICE_WHATSAPP_SENT'`.

- [ ] **Step 3: Implement**

In `src/secretaria/services/staff_patient_message.py`:

(a) extend the imports: change `from uuid import UUID, uuid4` (already present) to stay; add `from secretaria.core.whatsapp_limits import truncate_plain`; change `from secretaria.services.channel_sender import (CHANNEL_BRAIN_MESSAGE, RECORDED_MESSAGE_ID, BrainMessageSender,)` to also import `interactive_history_body`; change `from secretaria.services.whatsapp import WhatsAppClient` to `from secretaria.services.whatsapp import WhatsAppClient, interactive_buttons_record`.

(b) right after the `EMAIL_NOT_SENT = "not_sent"` line, insert the vocabulary (moved here from `appointment_release.py`, which imports it back):

```python
# What happened to a clinic-action notice (TASK-032 R4 vocabulary + R7 `whatsapp_sent`).
# The hub sends these to the front verbatim as `patient_notice`.
NOTICE_WHATSAPP_QUEUED = "whatsapp_queued"
NOTICE_WHATSAPP_SENT = "whatsapp_sent"
NOTICE_WHATSAPP_OUTSIDE_WINDOW = "whatsapp_outside_window"
NOTICE_PORTAL_CHAT = "portal_chat"
NOTICE_PORTAL_CHAT_EMAIL = "portal_chat_email"
NOTICE_NO_CHANNEL = "no_channel"
NOTICE_QUEUE_UNAVAILABLE = "queue_unavailable"
NOTICE_FAILED = "notice_failed"
DELIVERED_NOTICES = frozenset(
    {NOTICE_WHATSAPP_QUEUED, NOTICE_WHATSAPP_SENT, NOTICE_PORTAL_CHAT, NOTICE_PORTAL_CHAT_EMAIL}
)
# A template parameter is one line and Meta caps the whole body at 1024 characters
# including the template's own words (same budget as reminder_text.SINGLE_LINE_MAX_CHARS).
TEMPLATE_LINE_MAX_CHARS = 900
```

(c) append at the end of the module:

```python
# --- TASK-032 R7: every clinic action on the agenda tells the patient ---------------


@dataclass(frozen=True)
class NoticeResult:
    """One `NOTICE_*` code, the free `wa.me` link when the window stopped it, the row id."""

    code: str
    whatsapp_link: str | None = None
    message_id: UUID | None = None

    @property
    def delivered(self) -> bool:
        return self.code in DELIVERED_NOTICES


def paid_notice_authorised(tenant, requested: bool) -> bool:
    """Spec 2026-10-09 §3: the request's own yes, or the clinic's standing one."""
    return bool(requested) or getattr(tenant, "paid_notices_auto_approved", False) is True


async def send_clinic_notice(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient | None,
    *,
    body: str,
    buttons: list[tuple[str, str]] | None = None,
    allow_paid: bool,
    usage_key: str,
    now: datetime | None = None,
) -> NoticeResult:
    """Tell the appointment's patient what the clinic did. Never raises.

    * Portal: the text (or the button card) is written in the conversation, authored
      by the clinic (`HUMAN`, like R4), then the generic e-mail nudge. Commits.
    * WhatsApp inside 24 h: free text or the button card; the history row is recorded.
    * WhatsApp outside 24 h: only with `allow_paid`, and then the one-variable
      `REMINDER_TEMPLATE_NAME` with the body flattened to one line - buttons cannot
      ride along until Meta approves a button template (docs/LEMBRETES_MODELOS_META.md);
      a usage event bills it (after the send, fail-open). Without `allow_paid` nothing
      is sent and the free `wa.me` link comes back.

    The caller has proved the appointment belongs to `tenant`. Rows the caller left
    pending in `session` are committed with the delivery.
    """
    appointment_id = appointment.id  # before a rollback can expire the row
    try:
        if patient is None or patient.tenant_id != tenant.id:
            return NoticeResult(NOTICE_NO_CHANNEL)
        if patient.channel == CHANNEL_BRAIN_MESSAGE:
            return await _clinic_notice_portal(session, tenant, appointment, patient, body, buttons)
        return await _clinic_notice_whatsapp(
            session,
            tenant,
            appointment,
            patient,
            body,
            buttons,
            allow_paid=allow_paid,
            usage_key=usage_key,
            now=now,
        )
    except Exception as exc:  # defensive: the clinic's action already stands
        logger.error(
            "clinic_notice_failed",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )
        return NoticeResult(NOTICE_FAILED)


async def _clinic_notice_portal(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient,
    body: str,
    buttons: list[tuple[str, str]] | None,
) -> NoticeResult:
    appointment_id = appointment.id
    conversation_id = await conversation_id_for(session, tenant.id, appointment, patient)
    if conversation_id is None:
        return NoticeResult(NOTICE_NO_CHANNEL)
    sender = BrainMessageSender(
        conversation_id=conversation_id, session=session, author=MessageSender.HUMAN
    )
    to = patient.external_id or ""
    try:
        if buttons:
            response = await sender.send_buttons(to, body, buttons)
        else:
            response = await sender.send_text_message(to=to, body=body)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.error(
            "clinic_notice_failed",
            appointment_id=str(appointment_id),
            delivery=DELIVERY_PORTAL_CHAT,
            error_type=type(exc).__name__,
        )
        return NoticeResult(NOTICE_FAILED)
    nudge = await nudge_portal_patient(tenant, patient)
    code = NOTICE_PORTAL_CHAT_EMAIL if nudge == EMAIL_SENT else NOTICE_PORTAL_CHAT
    return NoticeResult(code, message_id=UUID(response[RECORDED_MESSAGE_ID]))


async def _clinic_notice_whatsapp(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient,
    body: str,
    buttons: list[tuple[str, str]] | None,
    *,
    allow_paid: bool,
    usage_key: str,
    now: datetime | None,
) -> NoticeResult:
    tenant_id, appointment_id = tenant.id, appointment.id
    to = patient.wa_id or appointment.phone
    if not to:
        return NoticeResult(NOTICE_NO_CHANNEL)
    last_inbound = await cancellation_notice.last_inbound_at(session, tenant_id, patient.id)
    inside = cancellation_notice.is_inside_window(last_inbound, now=now)
    if not inside and not allow_paid:
        logger.info(
            "clinic_notice_not_sent",
            appointment_id=str(appointment_id),
            reason="outside_window_not_authorised",
        )
        return NoticeResult(
            NOTICE_WHATSAPP_OUTSIDE_WINDOW,
            whatsapp_link=cancellation_notice.whatsapp_deep_link(to),
        )

    waba_token = await get_waba_token(session, tenant_id)
    try:
        client = WhatsAppClient.for_tenant(tenant, waba_token)
        if inside and buttons:
            response = await client.send_buttons(to, body, buttons)
            history = interactive_history_body(body, [label for _, label in buttons])
            interactive = interactive_buttons_record(body, buttons)
        elif inside:
            response = await client.send_text_message(to=to, body=body)
            history, interactive = body, None
        else:
            line = truncate_plain(_one_line(body), TEMPLATE_LINE_MAX_CHARS)
            response = await client.send_template(
                to=to,
                template=get_settings().REMINDER_TEMPLATE_NAME,
                lang=cancellation_notice.meta_language_code(tenant.language),
                variables=[line],
            )
            history, interactive = line, None
    except Exception as exc:
        logger.error(
            "clinic_notice_failed",
            appointment_id=str(appointment_id),
            inside_window=inside,
            error_type=type(exc).__name__,
        )
        return NoticeResult(NOTICE_FAILED)

    wam_id = _wam_id(response)
    message_id = None
    try:
        conversation_id = await conversation_id_for(session, tenant_id, appointment, patient)
        if conversation_id is not None:
            message = Message(
                conversation_id=conversation_id,
                direction=MessageDirection.OUTBOUND,
                sender=MessageSender.HUMAN,
                wam_id=wam_id,
                body=history,
                interactive=interactive,
            )
            session.add(message)
            await session.flush()
            message_id = message.id
        await session.commit()
    except Exception as exc:  # the message already left: never resend it
        await session.rollback()
        message_id = None
        logger.warning(
            "clinic_notice_history_failed",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )
    if not inside:
        try:
            await emit_usage_event(
                tenant_id=str(tenant_id),
                feature="reminders",
                amount=1,
                event_id=f"{usage_key}:{wam_id or uuid4().hex}",
            )
        except Exception as exc:
            logger.warning("usage_emit_failed", error_type=type(exc).__name__)
    return NoticeResult(NOTICE_WHATSAPP_SENT, message_id=message_id)
```

(d) In `src/secretaria/services/appointment_release.py`, delete the eight lines `NOTICE_WHATSAPP_QUEUED = ...` through `NOTICE_FAILED = "notice_failed"` and replace the existing `from secretaria.services.staff_patient_message import (...)` block with:

```python
from secretaria.services.staff_patient_message import (  # noqa: F401 - NOTICE_* re-exported
    EMAIL_SENT,
    NOTICE_FAILED,
    NOTICE_NO_CHANNEL,
    NOTICE_PORTAL_CHAT,
    NOTICE_PORTAL_CHAT_EMAIL,
    NOTICE_QUEUE_UNAVAILABLE,
    NOTICE_WHATSAPP_OUTSIDE_WINDOW,
    NOTICE_WHATSAPP_QUEUED,
    conversation_id_for,
    nudge_portal_patient,
)
```

- [ ] **Step 4: Run the tests**

Run: `pyt tests/test_clinic_notice_delivery.py tests/test_staff_patient_message.py tests/test_hub_release.py tests/test_hub_staff_message.py -q`
Expected: PASS (R4's tests unchanged and green).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format tests/test_clinic_notice_delivery.py && uvx ruff check --fix tests/test_clinic_notice_delivery.py
uvx ruff check src/secretaria/services/staff_patient_message.py src/secretaria/services/appointment_release.py
git diff --stat
git add src/secretaria/services/staff_patient_message.py src/secretaria/services/appointment_release.py tests/test_clinic_notice_delivery.py
git commit -m "feat(reminders): one channel-aware sender for clinic-action notices (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 4: The clinic flag on R4's release and message; `whatsapp_link` on release

**Files:**
- Modify: `src/secretaria/schemas/calendar.py` (`AppointmentReleaseRead`)
- Modify: `src/secretaria/api/hub/calendar.py` (`release_appointment`, `message_patient`, new helper `_notice_link`)
- Test: `tests/test_hub_release.py` (append), `tests/test_hub_staff_message.py` (append)

**Interfaces:**
- Consumes: `staff_patient_message.paid_notice_authorised`, `NOTICE_WHATSAPP_OUTSIDE_WINDOW` (Task 3).
- Produces: `api/hub/calendar.py::_notice_link(code: str | None, patient: Patient | None, appt: Appointment) -> str | None` (used again in Tasks 7, 11, 12); `AppointmentReleaseRead.whatsapp_link: str | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hub_release.py`:

```python
# --- TASK-032 R7: the clinic's standing authorisation and the wa.me link ------------


async def _approve_paid_notices(db, tenant_id) -> None:
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.paid_notices_auto_approved = True
        await session.commit()


async def test_r7_with_the_clinics_standing_yes_the_release_notice_may_bill(  # noqa: F811
    client: AsyncClient, db, acting
):
    pool = _FakeArqPool()
    _install_pool(pool)
    world = await _setup(db, acting, last_inbound_at=_recent(30))
    await _approve_paid_notices(db, world.tenant.id)

    response = await _release(client, world.appointment.id)  # no notify_outside_window

    assert response.json()["patient_notice"] == "whatsapp_queued"
    assert response.json()["whatsapp_link"] is None
    assert pool.calls[0][-1] is True


async def test_r7_outside_the_window_the_release_answers_the_free_link(  # noqa: F811
    client: AsyncClient, db, acting
):
    _install_pool(_FakeArqPool())
    world = await _setup(db, acting, last_inbound_at=_recent(30))

    body = (await _release(client, world.appointment.id)).json()

    assert body["patient_notice"] == "whatsapp_outside_window"
    assert body["whatsapp_link"] == "https://wa.me/5511988887777"
```

Append to `tests/test_hub_staff_message.py`:

```python
async def test_r7_the_clinics_standing_yes_sends_the_template_without_asking(  # noqa: F811
    client: AsyncClient, db, acting
):
    world = await _world(db, acting, last_inbound_at=_recent(30))
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        tenant.paid_notices_auto_approved = True
        await session.commit()

    response = await _message(client, world.appointment.id)

    assert response.status_code == 200, response.text
    assert response.json()["delivery"] == "whatsapp_template"
    assert FakeWhatsAppClient.all_sent()[0][0] == "template"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pyt tests/test_hub_release.py tests/test_hub_staff_message.py -q -k r7`
Expected: FAIL — the release queues with `allow_paid=False` (`whatsapp_outside_window`), `KeyError: 'whatsapp_link'`, and the message answers 409.

- [ ] **Step 3: Implement**

In `src/secretaria/schemas/calendar.py`, replace the body of `AppointmentReleaseRead` with:

```python
class AppointmentReleaseRead(AppointmentRead):
    """The released appointment, plus what happened to the patient notice.

    `patient_notice` is one of: whatsapp_queued, whatsapp_outside_window,
    portal_chat, portal_chat_email, no_channel, queue_unavailable, notice_failed.
    `whatsapp_link` (TASK-032 R7) is the free `wa.me` link, only with
    whatsapp_outside_window.
    """

    patient_notice: str = "not_attempted"
    whatsapp_link: str | None = None
```

In `src/secretaria/api/hub/calendar.py`, right after the `_detail` function, add:

```python
def _notice_link(code: str | None, patient: Patient | None, appt: Appointment) -> str | None:
    """The free `wa.me` link, only when the 24 h window is why the patient was not told."""
    if code != staff_patient_message.NOTICE_WHATSAPP_OUTSIDE_WINDOW:
        return None
    number = (patient.wa_id if patient is not None else None) or appt.phone
    return cancellation_notice.whatsapp_deep_link(number)
```

In `release_appointment`, replace `        allow_paid=body.notify_outside_window,` (inside the `notify_released_patient(...)` call) with:

```python
        allow_paid=staff_patient_message.paid_notice_authorised(
            tenant, body.notify_outside_window
        ),
```

and replace its last line `    return AppointmentReleaseRead(**read.model_dump(), patient_notice=patient_notice)` with:

```python
    return AppointmentReleaseRead(
        **read.model_dump(),
        patient_notice=patient_notice,
        whatsapp_link=_notice_link(patient_notice, patient, appt),
    )
```

In `message_patient`, replace `session, tenant, appt, patient, body.text, allow_paid=body.notify_outside_window` with:

```python
            session,
            tenant,
            appt,
            patient,
            body.text,
            allow_paid=staff_patient_message.paid_notice_authorised(
                tenant, body.notify_outside_window
            ),
```

- [ ] **Step 4: Run the tests**

Run: `pyt tests/test_hub_release.py tests/test_hub_staff_message.py tests/test_staff_patient_message.py -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff check src/secretaria/schemas/calendar.py src/secretaria/api/hub/calendar.py tests/test_hub_release.py tests/test_hub_staff_message.py
git diff --stat
git add src/secretaria/schemas/calendar.py src/secretaria/api/hub/calendar.py tests/test_hub_release.py tests/test_hub_staff_message.py
git commit -m "feat(hub): R4 release/message honour the clinic's paid-notice approval (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 5: Staff notice rows (`staff_confirm` / `staff_edit`) and their inertness

**Files:**
- Modify: `src/secretaria/models/appointment_reminder.py` (kinds)
- Modify: `src/secretaria/services/reminder_schedule.py` (append three functions)
- Modify: `src/secretaria/services/reminder_hooks.py` (`reconcile_missing_reminders`)
- Modify: `src/secretaria/workers/confirmation_warnings.py` (`due_candidates`)
- Modify: `tests/test_appointment_reminder_model.py` (`test_string_constants_are_the_spec_vocabulary`)
- Test: `tests/test_staff_notice_rows.py`

**Interfaces:**
- Consumes: R1's `AppointmentReminder`, `_as_utc`.
- Produces:
  - `models.appointment_reminder`: `REMINDER_KIND_STAFF_CONFIRM = "staff_confirm"`, `REMINDER_KIND_STAFF_EDIT = "staff_edit"`, `REMINDER_KINDS` = `("custom", "day", "hour", "chat", "staff_confirm", "staff_edit")`, `REMINDER_KINDS_STAFF = ("staff_confirm", "staff_edit")`, `REMINDER_KINDS_UNPLANNED = ("chat", "staff_confirm", "staff_edit")`.
  - `reminder_schedule.current_staff_notice_row(session, appointment, *, kind: str) -> AppointmentReminder | None`
  - `reminder_schedule.ensure_staff_notice_row(session, appointment, *, kind: str, channel: str, with_prompt: bool, now: datetime) -> tuple[AppointmentReminder, bool]` (`True` = created now). Flushes; never commits.
  - `reminder_schedule.retire_staff_notice_row(session, reminder_id: UUID, *, tenant_id: UUID) -> None`. Never commits.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_staff_notice_rows.py`:

```python
"""The rows behind the clinic's confirm/edit cards are tap targets and nothing else (R7)."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from secretaria.core import database as core_database
from secretaria.models import Appointment, AppointmentReminder, Tenant
from secretaria.services import reminder_hooks, reminder_opening, reminder_schedule
from secretaria.workers import confirmation_warnings, reminder_engine
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import NOW, seed_world


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    monkeypatch.setattr(core_database, "async_session_factory", db)


async def _ensure(db, world, kind="staff_confirm", *, with_prompt=True, now=NOW):  # noqa: F811
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        row, created = await reminder_schedule.ensure_staff_notice_row(
            session, appointment, kind=kind, channel="whatsapp", with_prompt=with_prompt, now=now
        )
        await session.commit()
        return row, created


async def _rows(db, appointment_id):  # noqa: F811
    async with db() as session:
        return list(
            await session.scalars(
                select(AppointmentReminder).where(
                    AppointmentReminder.appointment_id == appointment_id
                )
            )
        )


async def test_a_staff_row_is_born_sent_without_a_clinic_warning(db):  # noqa: F811
    world = await seed_world(db)

    row, created = await _ensure(db, world)

    assert created is True
    assert (row.kind, row.status, row.channel, row.with_prompt) == (
        "staff_confirm",
        "sent",
        "whatsapp",
        True,
    )
    assert row.warn_due_at is None and row.warned_at is None and row.invalidated_at is None
    assert row.patient_id == world.patient.id


async def test_the_same_start_reuses_the_row_and_keeps_the_answer(db):  # noqa: F811
    world = await seed_world(db)
    first, _ = await _ensure(db, world)
    async with db() as session:
        stored = await session.get(AppointmentReminder, first.id)
        stored.answer = "confirm"
        await session.commit()

    again, created = await _ensure(db, world, now=NOW + timedelta(minutes=5), with_prompt=False)

    assert created is False and again.id == first.id
    assert again.answer == "confirm" and again.with_prompt is False
    assert len(await _rows(db, world.appointment.id)) == 1


async def test_a_new_start_or_a_retired_row_gets_a_fresh_row(db):  # noqa: F811
    world = await seed_world(db)
    first, _ = await _ensure(db, world)
    async with db() as session:
        await reminder_schedule.retire_staff_notice_row(
            session, first.id, tenant_id=world.tenant.id
        )
        await session.commit()

    second, created = await _ensure(db, world)
    assert created is True and second.id != first.id

    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.start_at = world.start_at + timedelta(days=1)
        await session.commit()
    third, created = await _ensure(db, world)
    assert created is True and third.id not in {first.id, second.id}


async def test_kinds_are_validated_and_independent(db):  # noqa: F811
    world = await seed_world(db)
    confirm, _ = await _ensure(db, world, "staff_confirm")
    edit, _ = await _ensure(db, world, "staff_edit")
    assert confirm.id != edit.id
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        with pytest.raises(ValueError):
            await reminder_schedule.current_staff_notice_row(session, appointment, kind="day")


async def test_the_warning_cron_never_selects_a_staff_row(db):  # noqa: F811
    world = await seed_world(db)
    await _ensure(db, world)

    async with db() as session:
        found = await confirmation_warnings.due_candidates(session, NOW + timedelta(days=2))

    assert found == []


async def test_the_engine_never_picks_a_staff_row(db):  # noqa: F811
    world = await seed_world(db)
    await _ensure(db, world)

    assert await reminder_engine.due_reminder_ids(NOW + timedelta(days=2), limit=50) == []


async def test_the_reconcile_still_plans_an_appointment_that_only_has_a_staff_row(db):  # noqa: F811
    world = await seed_world(db)
    await _ensure(db, world)

    created = await reminder_hooks.reconcile_missing_reminders(now=NOW)

    kinds = {row.kind for row in await _rows(db, world.appointment.id)}
    assert created >= 2 and {"staff_confirm", "day", "hour"} <= kinds


async def test_a_staff_row_never_turns_the_agenda_red_nor_blocks_the_opening(db):  # noqa: F811
    world = await seed_world(db)
    row, _ = await _ensure(db, world)

    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        tenant = await session.get(Tenant, world.tenant.id)
        assert reminder_schedule.display_state(appointment, [row]) == "unconfirmed"
        decision = await reminder_opening.decide_reminder_opening(
            session,
            tenant=tenant,
            patient_id=world.patient.id,
            last_activity_at=NOW - timedelta(hours=7),
            now=NOW,
        )
    assert decision.reason == reminder_opening.OPEN
```

In `tests/test_appointment_reminder_model.py`, replace:

```python
    assert ar.REMINDER_KINDS == ("custom", "day", "hour", "chat")
```

with:

```python
    assert ar.REMINDER_KINDS == ("custom", "day", "hour", "chat", "staff_confirm", "staff_edit")
    assert ar.REMINDER_KINDS_STAFF == ("staff_confirm", "staff_edit")
    assert ar.REMINDER_KINDS_UNPLANNED == ("chat", "staff_confirm", "staff_edit")
    assert all(len(kind) <= 16 for kind in ar.REMINDER_KINDS)  # kind is VARCHAR(16)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pyt tests/test_staff_notice_rows.py tests/test_appointment_reminder_model.py -q`
Expected: FAIL — `AttributeError: module 'secretaria.services.reminder_schedule' has no attribute 'ensure_staff_notice_row'`.

- [ ] **Step 3: Implement the kinds**

In `src/secretaria/models/appointment_reminder.py`, replace the block from `REMINDER_KIND_CHAT = "chat"  # ...` through the closing `)` of `REMINDER_KINDS` with:

```python
REMINDER_KIND_CHAT = "chat"  # the opening message of the chat (born already sent)
# TASK-032 R7: the cards the CLINIC's actions put in front of the patient - "Seu médico
# confirmou ..." and "A clínica alterou ...". Born already sent, never warned about; they
# exist so the Confirmar / Cancelar / Alterar Dados taps resolve to a row exactly like a
# reminder's (workers/shared/reminder_actions.py).
REMINDER_KIND_STAFF_CONFIRM = "staff_confirm"
REMINDER_KIND_STAFF_EDIT = "staff_edit"
REMINDER_KINDS: tuple[str, ...] = (
    REMINDER_KIND_CUSTOM,
    REMINDER_KIND_DAY,
    REMINDER_KIND_HOUR,
    REMINDER_KIND_CHAT,
    REMINDER_KIND_STAFF_CONFIRM,
    REMINDER_KIND_STAFF_EDIT,
)
REMINDER_KINDS_STAFF: tuple[str, ...] = (REMINDER_KIND_STAFF_CONFIRM, REMINDER_KIND_STAFF_EDIT)
# Kinds that are shown, never planned: the cron engine, the reconcile backfill and the
# clinic warnings ignore them.
REMINDER_KINDS_UNPLANNED: tuple[str, ...] = (REMINDER_KIND_CHAT, *REMINDER_KINDS_STAFF)
```

- [ ] **Step 4: Implement the row functions**

In `src/secretaria/services/reminder_schedule.py`, extend the `from secretaria.models.appointment_reminder import (...)` block with `REMINDER_KINDS_STAFF,` and `REMINDER_STATUS_SENT,`, and append at the end of the module:

```python
# --- TASK-032 R7: the rows behind the clinic's confirm / edit cards -----------------


def _check_staff_kind(kind: str) -> None:
    if kind not in REMINDER_KINDS_STAFF:
        raise ValueError(f"not a staff notice kind: {kind!r}")


async def current_staff_notice_row(
    session: AsyncSession, appointment: Appointment, *, kind: str
) -> AppointmentReminder | None:
    """The active `kind` row of the appointment's CURRENT start, or None."""
    _check_staff_kind(kind)
    if appointment.start_at is None:
        return None
    start = _as_utc(appointment.start_at)
    rows = await session.scalars(
        select(AppointmentReminder).where(
            AppointmentReminder.tenant_id == appointment.tenant_id,
            AppointmentReminder.appointment_id == appointment.id,
            AppointmentReminder.kind == kind,
            AppointmentReminder.invalidated_at.is_(None),
        )
    )
    return next((row for row in rows if _as_utc(row.appointment_start_at) == start), None)


async def ensure_staff_notice_row(
    session: AsyncSession,
    appointment: Appointment,
    *,
    kind: str,
    channel: str,
    with_prompt: bool,
    now: datetime,
) -> tuple[AppointmentReminder, bool]:
    """The staff card row of the current start: re-shown (False) or created (True).

    Born `sent` with NO clinic warning (`warn_due_at` NULL) - the R4 cron must never
    warn about a card the clinic itself sent. A re-shown row keeps its `answer`, so
    tapping Confirmar on the same card twice still counts once (R1 dedupes on the
    row). One active row per (appointment, kind, start) - R1's unique index. Flushes;
    the caller commits.
    """
    if appointment.start_at is None:
        raise ValueError("an appointment without a start has no card")
    row = await current_staff_notice_row(session, appointment, kind=kind)
    if row is not None:
        row.sent_at = now
        row.with_prompt = with_prompt
        await session.flush()
        return row, False
    row = AppointmentReminder(
        tenant_id=appointment.tenant_id,
        appointment_id=appointment.id,
        patient_id=appointment.patient_id,
        kind=kind,
        appointment_start_at=appointment.start_at,
        due_at=now,
        status=REMINDER_STATUS_SENT,
        channel=channel,
        with_prompt=with_prompt,
        sent_at=now,
        warn_due_at=None,
    )
    session.add(row)
    await session.flush()
    logger.info(
        "staff_notice_row_created",
        appointment_id=str(appointment.id),
        kind=kind,
    )
    return row, True


async def retire_staff_notice_row(
    session: AsyncSession, reminder_id: UUID, *, tenant_id: UUID
) -> None:
    """Retire a staff row whose card never reached the patient (a retry gets a fresh one)."""
    await session.execute(
        update(AppointmentReminder)
        .where(
            AppointmentReminder.id == reminder_id,
            AppointmentReminder.tenant_id == tenant_id,
        )
        .values(invalidated_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
```

- [ ] **Step 5: Exclude the kinds from the backfill and the warnings**

In `src/secretaria/services/reminder_hooks.py`, change the import `from secretaria.models.appointment_reminder import REMINDER_KIND_CHAT` to `from secretaria.models.appointment_reminder import REMINDER_KINDS_UNPLANNED`, and inside `reconcile_missing_reminders` replace:

```python
                # TASK-032 R3: a `chat` row (the opening card) is not a plan -
                # an appointment that only has one still needs its cron rows.
                AppointmentReminder.kind != REMINDER_KIND_CHAT,
```

with:

```python
                # TASK-032 R3/R7: a `chat` row (the opening card) or a clinic card
                # (`staff_*`) is not a plan - an appointment that only has those
                # still needs its cron rows.
                AppointmentReminder.kind.not_in(REMINDER_KINDS_UNPLANNED),
```

In `src/secretaria/workers/confirmation_warnings.py`, add `REMINDER_KINDS_UNPLANNED,` to the `from secretaria.models.appointment_reminder import (...)` block, and in `due_candidates` replace `            AppointmentReminder.kind != REMINDER_KIND_CHAT,` with:

```python
            # R3's opening card and R7's clinic cards are never warned about.
            AppointmentReminder.kind.not_in(REMINDER_KINDS_UNPLANNED),
```

If `REMINDER_KIND_CHAT` is then unused in `confirmation_warnings.py`, remove it from that import (`uvx ruff check` will say so).

- [ ] **Step 6: Run the tests**

Run: `pyt tests/test_staff_notice_rows.py tests/test_appointment_reminder_model.py tests/test_reminder_v2_hooks.py tests/test_reminder_opening_decision.py tests/test_confirmation_warnings_select.py tests/test_confirmation_warnings_cron.py -q`
Expected: PASS.

- [ ] **Step 7: Lint and commit**

```bash
uvx ruff format tests/test_staff_notice_rows.py && uvx ruff check --fix tests/test_staff_notice_rows.py
uvx ruff check src/secretaria/models/appointment_reminder.py src/secretaria/services/reminder_schedule.py src/secretaria/services/reminder_hooks.py src/secretaria/workers/confirmation_warnings.py tests/test_appointment_reminder_model.py
git diff --stat
git add src/secretaria/models/appointment_reminder.py src/secretaria/services/reminder_schedule.py src/secretaria/services/reminder_hooks.py src/secretaria/workers/confirmation_warnings.py tests/test_staff_notice_rows.py tests/test_appointment_reminder_model.py
git commit -m "feat(reminders): staff_confirm/staff_edit rows - tap targets that never warn or plan (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 6: PATCH status guards and the action response shape

**Files:**
- Modify: `src/secretaria/services/appointment_status.py` (append `StaffTransitionRefused`, `STAFF_STATUS_CORRECTIONS`, `staff_transition`)
- Modify: `src/secretaria/schemas/calendar.py` (`AppointmentStatusUpdate.notify_outside_window`, new `AppointmentActionRead`)
- Modify: `src/secretaria/api/hub/calendar.py` (`update_appointment_status` rewritten; imports)
- Modify: `tests/test_hub_calendar_confirmation.py` (three tests that encoded "no validation")
- Create: `tests/_r7_support.py` (shared by Tasks 6, 7, 8, 11, 12)
- Test: `tests/test_hub_status_guards.py`

**Interfaces:**
- Consumes: `reminder_schedule.register_confirmation/reset_confirmation/cancel_reminders`, `deposit_lifecycle.on_appointment_cancelled/on_no_show`.
- Produces:
  - `appointment_status.StaffTransitionRefused(code: str, message: str)` (attributes `code`, `message`); `appointment_status.staff_transition(current: AppointmentStatus, target: AppointmentStatus, *, start_at: datetime | None, now: datetime) -> bool` (`True` apply, `False` no-op).
  - `schemas.calendar.AppointmentActionRead(AppointmentRead)` with `patient_notice: str | None = None`, `whatsapp_link: str | None = None`; `AppointmentStatusUpdate.notify_outside_window: bool = False`.
  - In `update_appointment_status`, a variable `notice: staff_patient_message.NoticeResult | None = None` right after the commit/refresh; Tasks 7 and 8 fill it.
  - `tests/_r7_support.py`: fixtures `acting`, `r7` (autouse when imported; returns `{"mail": [...], "usage": [...]}`), `FakeCalendar`, `FakeArqPool`, `install_pool(pool)`, `soon(days=3)`, `recent(hours=1.0)`, `setup_world(db, acting, **seed_world_kwargs)`, `set_tenant(db, tenant_id, **fields)`, `set_appointment(db, appointment_id, **fields)`, `add_professional(db, tenant_id, name)`, `staff_rows(db, appointment_id, kind)`, `sent()`, `patch_status(client, appointment_id, status, **extra)`, `tap(world, action, reminder_id)`, `CALENDAR`.

- [ ] **Step 1: Create the shared test support**

Create `tests/_r7_support.py`:

```python
"""Wiring shared by the TASK-032 R7 hub tests (status, confirm, attended, edit, cancel).

Import the fixtures by name - `from tests._r7_support import acting, r7  # noqa: F401` -
and the database fixture from `tests._reminder_fixtures`. `r7` is autouse: it overrides
the hub dependencies, points every worker module and `core.database` at the in-memory
DB (R3's `wire`), installs the WhatsApp double on the hub's sender module and a Google
Calendar double on the hub route, and records the Portal e-mails and usage events.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from secretaria.api.hub import calendar as hub_calendar  # noqa: E402
from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentReminder,
    Professional,
    Tenant,
)
from secretaria.services import staff_patient_message as spm  # noqa: E402
from secretaria.services.email import EmailOutcome  # noqa: E402
from secretaria.services.tenant_config import set_google_refresh_token  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from tests._reminders_r3 import wire as wire_workers  # noqa: E402
from tests._reminders_v2 import WA_ID, FakeWhatsAppClient, fake_waba_token, seed_world  # noqa: E402

CALENDAR = "/tenants/me/calendar"


def soon(days: int = 3) -> datetime:
    """A start in the future, on a whole minute (the agenda never has seconds)."""
    return (datetime.now(UTC) + timedelta(days=days)).replace(second=0, microsecond=0)


def recent(hours: float = 1.0) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)


class FakeCalendar:
    """Google Calendar double for the hub route: one instance per agenda label."""

    tzinfo = ZoneInfo("America/Sao_Paulo")
    instances: dict[str, "FakeCalendar"] = {}

    def __init__(self, label: str = "tenant") -> None:
        self.label = label
        self.free = True
        self.fail: Exception | None = None
        self.updated: list[tuple] = []
        self.created: list[tuple] = []
        self.cancelled: list[str] = []
        self.checked: list[tuple] = []
        self.during = None  # optional async callable run inside update_event_details

    @classmethod
    def get(cls, label: str) -> "FakeCalendar":
        return cls.instances.setdefault(label, cls(label))

    @classmethod
    def reset(cls) -> None:
        cls.instances = {}

    @classmethod
    def from_tenant_config(cls, config) -> "FakeCalendar":
        return cls.get("tenant")

    def references_same_calendar(self, other) -> bool:
        return getattr(other, "label", None) == self.label

    def _gate(self) -> None:
        if self.fail is not None:
            raise self.fail

    async def is_slot_free(self, start, end, *, ignore_event_id=None) -> bool:
        self._gate()
        self.checked.append((start, end, ignore_event_id))
        return self.free

    async def update_event_details(self, event_id, start, end, summary, description="") -> dict:
        self._gate()
        self.updated.append((event_id, start, end, summary, description))
        if self.during is not None:
            during, self.during = self.during, None  # once: an undo must not re-trigger it
            await during()
        return {"id": event_id}

    async def create_event(self, start, end, summary, description="", reminders=None) -> dict:
        self._gate()
        self.created.append((start, end, summary, description))
        return {"id": f"evt-new-{self.label}", "htmlLink": f"https://calendar.google.com/{self.label}"}

    async def cancel_event(self, event_id) -> None:
        self._gate()
        self.cancelled.append(event_id)

    async def update_event(self, event_id, start, end) -> dict:
        self._gate()
        return {"id": event_id}

    async def check_availability(self, start, end) -> list:
        return []


class FakeArqPool:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail = fail

    async def enqueue_job(self, name: str, *args, **kwargs) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        self.calls.append((name, *args))


def install_pool(pool) -> None:
    from secretaria.main import app

    app.state.arq_pool = pool


@pytest.fixture
def acting() -> dict:
    return {"tenant_id": None}


@pytest.fixture(autouse=True)
def r7(db, acting, monkeypatch: pytest.MonkeyPatch):
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, acting["tenant_id"])

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    app.state.arq_pool = None
    wire_workers(monkeypatch, db)  # core.database + worker modules + WhatsApp double (reset)
    FakeCalendar.reset()
    monkeypatch.setattr(hub_calendar, "CalendarService", FakeCalendar)

    async def _resolve(session, tenant, professional, *, tenant_config=None, **_):
        return FakeCalendar.get(str(professional.id))

    monkeypatch.setattr(hub_calendar, "resolve_professional_calendar", _resolve)
    monkeypatch.setattr(spm, "WhatsAppClient", FakeWhatsAppClient)
    monkeypatch.setattr(spm, "get_waba_token", fake_waba_token)
    monkeypatch.setattr(spm, "portal_conversation_link", lambda tenant_id: "https://portal/x")
    doubles: dict[str, list] = {"mail": [], "usage": []}

    async def _send(to, template, variables) -> EmailOutcome:
        doubles["mail"].append((to, template, variables))
        return EmailOutcome.SENT

    async def _emit(**kwargs) -> bool:
        doubles["usage"].append(kwargs)
        return True

    monkeypatch.setattr(spm, "send_transactional_email_result", _send)
    monkeypatch.setattr(spm, "emit_usage_event", _emit)
    yield doubles
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)
    app.state.arq_pool = None


async def setup_world(db, acting, **kwargs):
    """`seed_world` with a future start and a recent inbound by default, acting as its clinic."""
    kwargs.setdefault("start_at", soon())
    kwargs.setdefault("last_inbound_at", recent())
    world = await seed_world(db, **kwargs)
    acting["tenant_id"] = world.tenant.id
    async with db() as session:
        await set_google_refresh_token(session, world.tenant.id, "fake-refresh-token")
        await session.commit()
    return world


async def set_tenant(db, tenant_id, **fields) -> None:
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        for name, value in fields.items():
            setattr(tenant, name, value)
        await session.commit()


async def set_appointment(db, appointment_id, **fields) -> None:
    async with db() as session:
        appointment = await session.get(Appointment, appointment_id)
        for name, value in fields.items():
            setattr(appointment, name, value)
        await session.commit()


async def add_professional(db, tenant_id, name: str, *, active: bool = True) -> UUID:
    async with db() as session:
        professional = Professional(id=uuid4(), tenant_id=tenant_id, name=name, is_active=active)
        session.add(professional)
        await session.commit()
        return professional.id


async def staff_rows(db, appointment_id, kind: str) -> list[AppointmentReminder]:
    async with db() as session:
        return list(
            await session.scalars(
                select(AppointmentReminder)
                .where(
                    AppointmentReminder.appointment_id == appointment_id,
                    AppointmentReminder.kind == kind,
                )
                .order_by(AppointmentReminder.created_at)
            )
        )


def sent() -> list[tuple]:
    return FakeWhatsAppClient.all_sent()


async def patch_status(client, appointment_id, status_value: str, **extra):
    return await client.patch(
        f"{CALENDAR}/appointments/{appointment_id}/status",
        json={"status": status_value, **extra},
    )


async def tap(world, action: str, reminder_id) -> None:
    """The patient taps a button of a card (WhatsApp; a Portal tap is the same handler)."""
    reply = tasks._ReplyContext(
        channel="whatsapp",
        conversation_id=world.conversation.id,
        patient_ref=world.patient.wa_id or WA_ID,
        inbound_body=action,
    )
    await tasks._handle_action_button(reply, action, str(reminder_id))
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_hub_status_guards.py`:

```python
"""PATCH /appointments/{id}/status guards (TASK-032 R7, spec 2026-10-09 §1/§3)."""

# ruff: noqa: F811

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from secretaria.api.hub import calendar as hub_calendar
from secretaria.models import AppointmentStatus
from secretaria.services.appointment_status import StaffTransitionRefused, staff_transition
from tests._r7_support import acting, patch_status, r7, set_appointment, setup_world  # noqa: F401
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import reload_appointment

S = AppointmentStatus
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
FUTURE = NOW + timedelta(hours=2)
PAST = NOW - timedelta(hours=2)


# ------------------------------------------------------------------ the pure rule


@pytest.mark.parametrize("current", [S.SCHEDULED, S.CONFIRMED, S.RESCHEDULED])
@pytest.mark.parametrize("target", [S.SCHEDULED, S.CONFIRMED, S.RESCHEDULED, S.ATTENDED])
def test_a_live_appointment_accepts_any_live_target_and_attended(current, target):
    assert staff_transition(current, target, start_at=FUTURE, now=NOW) is True


def test_no_show_before_the_start_is_refused_and_after_it_is_accepted():
    with pytest.raises(StaffTransitionRefused) as err:
        staff_transition(S.SCHEDULED, S.NO_SHOW, start_at=FUTURE, now=NOW)
    assert err.value.code == "no_show_before_start"
    assert staff_transition(S.SCHEDULED, S.NO_SHOW, start_at=PAST, now=NOW) is True
    assert staff_transition(S.SCHEDULED, S.NO_SHOW, start_at=None, now=NOW) is True


@pytest.mark.parametrize("target", [S.SCHEDULED, S.CONFIRMED, S.RESCHEDULED, S.ATTENDED, S.NO_SHOW])
def test_a_cancelled_appointment_never_comes_back(target):
    with pytest.raises(StaffTransitionRefused) as err:
        staff_transition(S.CANCELLED, target, start_at=PAST, now=NOW)
    assert err.value.code == "not_live"


def test_attended_and_no_show_correct_each_other_and_repeat_as_a_no_op():
    assert staff_transition(S.ATTENDED, S.NO_SHOW, start_at=PAST, now=NOW) is True
    assert staff_transition(S.NO_SHOW, S.ATTENDED, start_at=PAST, now=NOW) is True
    assert staff_transition(S.ATTENDED, S.ATTENDED, start_at=PAST, now=NOW) is False
    assert staff_transition(S.NO_SHOW, S.NO_SHOW, start_at=PAST, now=NOW) is False
    with pytest.raises(StaffTransitionRefused):
        staff_transition(S.ATTENDED, S.CONFIRMED, start_at=FUTURE, now=NOW)
    with pytest.raises(StaffTransitionRefused):
        staff_transition(S.ATTENDED, S.NO_SHOW, start_at=FUTURE, now=NOW)  # still before start


@pytest.mark.parametrize("current", list(AppointmentStatus))
def test_cancelled_keeps_todays_unguarded_behaviour(current):
    assert staff_transition(current, S.CANCELLED, start_at=FUTURE, now=NOW) is True


# ------------------------------------------------------------------ the endpoint


async def test_no_show_before_the_start_is_409_and_changes_nothing(
    client: AsyncClient, db, acting, monkeypatch
):
    world = await setup_world(db, acting)
    calls: list = []

    async def _spy(*args, **kwargs):
        calls.append(1)

    monkeypatch.setattr(hub_calendar.deposit_lifecycle, "on_no_show", _spy)

    response = await patch_status(client, world.appointment.id, "no_show")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "no_show_before_start" and detail["status"] == "scheduled"
    assert detail["start_at"].startswith(world.start_at.strftime("%Y-%m-%dT%H:%M"))
    assert (await reload_appointment(db, world.appointment.id)).status == S.SCHEDULED
    assert calls == []


async def test_no_show_after_the_start_is_accepted(client: AsyncClient, db, acting):
    world = await setup_world(db, acting, start_at=datetime.now(UTC) - timedelta(hours=1))

    response = await patch_status(client, world.appointment.id, "no_show")

    assert response.status_code == 200 and response.json()["status"] == "no_show"
    assert response.json()["patient_notice"] is None


@pytest.mark.parametrize("target", ["confirmed", "attended", "scheduled", "no_show"])
async def test_a_cancelled_appointment_is_never_resurrected(
    client: AsyncClient, db, acting, target
):
    world = await setup_world(db, acting, status=S.CANCELLED)

    response = await patch_status(client, world.appointment.id, target)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "not_live"
    assert response.json()["detail"]["status"] == "cancelled"
    row = await reload_appointment(db, world.appointment.id)
    assert row.status == S.CANCELLED and row.confirmation_count == 0


async def test_attended_twice_is_a_quiet_no_op(client: AsyncClient, db, acting, monkeypatch):
    world = await setup_world(db, acting, start_at=datetime.now(UTC) - timedelta(hours=1))
    await set_appointment(db, world.appointment.id, status=S.ATTENDED)
    logged: list = []
    monkeypatch.setattr(hub_calendar, "log_status_transition", lambda **kw: logged.append(kw))

    response = await patch_status(client, world.appointment.id, "attended")

    assert response.status_code == 200 and response.json()["status"] == "attended"
    assert response.json()["patient_notice"] is None and logged == []


async def test_attended_can_be_corrected_to_no_show_after_the_start(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting, start_at=datetime.now(UTC) - timedelta(hours=1))
    await set_appointment(db, world.appointment.id, status=S.ATTENDED)

    response = await patch_status(client, world.appointment.id, "no_show")

    assert response.status_code == 200 and response.json()["status"] == "no_show"


async def test_the_action_response_carries_the_notice_keys(client: AsyncClient, db, acting):
    world = await setup_world(db, acting)

    body = (await patch_status(client, world.appointment.id, "scheduled")).json()

    assert body["patient_notice"] is None and body["whatsapp_link"] is None
    assert body["status"] == "scheduled"
```

In `tests/test_hub_calendar_confirmation.py`:

(a) change `@pytest.mark.parametrize("terminal", ["cancelled", "attended", "no_show"])` to `@pytest.mark.parametrize("terminal", ["cancelled", "attended"])`, and right after that test add:

```python
async def test_patch_no_show_after_the_start_cancels_the_pending_reminders(
    client: AsyncClient, db, tenant
):  # noqa: F811
    """R7: no_show is only accepted after the start (spec 2026-10-09 §1)."""
    await _connect(db, tenant)
    appt = await _booked(db, tenant)
    await _set(db, appt.id, start_at=datetime.now(UTC) - timedelta(minutes=5))
    assert await _statuses(db, appt.id) == {"pending"}

    response = await _patch(client, appt.id, "no_show")

    assert response.status_code == 200
    assert await _statuses(db, appt.id) == {"cancelled"}
```

(b) replace the whole `test_patch_keeps_accepting_the_transitions_it_always_accepted` test with:

```python
async def test_patch_no_longer_resurrects_a_cancelled_booking(client: AsyncClient, db, tenant):  # noqa: F811
    """R7 (spec 2026-10-09 §3): confirmed/attended on a cancelled booking is 409 not_live."""
    await _connect(db, tenant)
    appt = await _booked(db, tenant, status=AppointmentStatus.CANCELLED)

    attended = await _patch(client, appt.id, "attended")
    confirmed = await _patch(client, appt.id, "confirmed")

    assert attended.status_code == confirmed.status_code == 409
    assert confirmed.json()["detail"]["code"] == "not_live"
    assert (await _reload(db, appt.id)).status == AppointmentStatus.CANCELLED
```

(c) in `test_existing_appointment_read_keys_are_unchanged`, replace `    assert set(body) == legacy | {"confirmation_count"}` with:

```python
    assert set(body) == legacy | {"confirmation_count", "patient_notice", "whatsapp_link"}
```

- [ ] **Step 3: Run them to verify they fail**

Run: `pyt tests/test_hub_status_guards.py tests/test_hub_calendar_confirmation.py -q`
Expected: FAIL — `ImportError: cannot import name 'StaffTransitionRefused'`.

- [ ] **Step 4: Implement the rule**

Append to `src/secretaria/services/appointment_status.py` (add `from datetime import UTC, datetime` to its imports):

```python
# --- TASK-032 R7: what a staff PATCH /status may do ----------------------------------


class StaffTransitionRefused(ValueError):
    """The PATCH would contradict the appointment's state (spec 2026-10-09 §1/§3)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


# After the start the agenda offers both "Compareceu" and "Faltou", so a mis-click must
# be correctable; nothing else ever leaves a terminal status through this endpoint.
STAFF_STATUS_CORRECTIONS: dict[AppointmentStatus, tuple[AppointmentStatus, ...]] = {
    AppointmentStatus.ATTENDED: (AppointmentStatus.NO_SHOW,),
    AppointmentStatus.NO_SHOW: (AppointmentStatus.ATTENDED,),
}


def staff_transition(
    current: AppointmentStatus,
    target: AppointmentStatus,
    *,
    start_at: datetime | None,
    now: datetime,
) -> bool:
    """True = apply `target`; False = a repeated attended/no_show (a no-op). Pure.

    * `cancelled` keeps its pre-R7, unguarded behaviour (and its money hook).
    * Any other target needs a LIVE appointment - a cancelled booking never comes back
      (it used to: PATCH confirmed "resurrected" it) - except the attended <-> no_show
      correction.
    * `no_show` before the start is refused: "Faltou" does not exist before the time.

    Raises `StaffTransitionRefused` with code `not_live` or `no_show_before_start`.
    """
    if target == AppointmentStatus.CANCELLED:
        return True
    if target in STAFF_STATUS_CORRECTIONS and current == target:
        return False
    if not (is_live_status(current) or current in STAFF_STATUS_CORRECTIONS.get(target, ())):
        raise StaffTransitionRefused("not_live", "Esta consulta já foi cancelada ou encerrada.")
    if target == AppointmentStatus.NO_SHOW and start_at is not None:
        start = start_at if start_at.tzinfo is not None else start_at.replace(tzinfo=UTC)
        if start > now:
            raise StaffTransitionRefused(
                "no_show_before_start",
                "Só é possível marcar falta depois do horário da consulta.",
            )
    return True
```

- [ ] **Step 5: Implement the schemas**

In `src/secretaria/schemas/calendar.py`, replace `class AppointmentStatusUpdate` with:

```python
class AppointmentStatusUpdate(BaseModel):
    """PATCH /appointments/{id}/status."""

    status: AppointmentStatus
    # TASK-032 R7: authorises the BILLED template for the notice that `confirmed` and
    # `attended` send to a WhatsApp patient outside the 24 h window (OR-ed with the
    # clinic's `paid_notices_auto_approved`). Ignored for every other status.
    notify_outside_window: bool = False
```

and right after the `AppointmentRead` class add:

```python
class AppointmentActionRead(AppointmentRead):
    """An appointment after a clinic action that may tell the patient (TASK-032 R7).

    `patient_notice` is null when no notice was due, else one of: whatsapp_queued,
    whatsapp_sent, whatsapp_outside_window, portal_chat, portal_chat_email,
    no_channel, queue_unavailable, notice_failed. `whatsapp_link` is the free
    `wa.me` link, only with whatsapp_outside_window.
    """

    patient_notice: str | None = None
    whatsapp_link: str | None = None
```

- [ ] **Step 6: Rewrite the endpoint**

In `src/secretaria/api/hub/calendar.py`: add `AppointmentActionRead,` to the `from secretaria.schemas.calendar import (...)` block; extend the `from secretaria.services.appointment_status import (...)` block with `StaffTransitionRefused,` and `staff_transition,`. Replace the whole `update_appointment_status` function (from `@router.patch("/appointments/{appointment_id}/status", ...)` to its final `return`) with:

```python
@router.patch("/appointments/{appointment_id}/status", response_model=AppointmentActionRead)
async def update_appointment_status(
    appointment_id: str,
    body: AppointmentStatusUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> AppointmentActionRead:
    """Mark scheduled / confirmed / attended / no-show / cancelled (TASK-032 R1 + R7).

    R7 guards (spec 2026-10-09 §1/§3, rule in services/appointment_status.py::
    staff_transition): `no_show` before the start is 409 `no_show_before_start`; a
    live target, `attended` or `no_show` on a cancelled booking is 409 `not_live` (it
    used to resurrect); attended <-> no_show is a correction; repeating attended or
    no_show changes nothing. `cancelled` keeps its old, unguarded behaviour.
    """
    appt = await _get_appointment(session, tenant, appointment_id)
    now = datetime.now(UTC)
    try:
        applies = staff_transition(appt.status, body.status, start_at=appt.start_at, now=now)
    except StaffTransitionRefused as exc:
        extra: dict = {"status": appt.status.value}
        if exc.code == "no_show_before_start" and appt.start_at is not None:
            extra["start_at"] = as_utc(appt.start_at).isoformat()
        raise HTTPException(
            status.HTTP_409_CONFLICT, _detail(exc.code, exc.message, **extra)
        ) from None
    if not applies:
        deposit_status = await _deposit_status_value(session, appt.id)
        return AppointmentActionRead(
            **_appointment_read(appt, deposit_status=deposit_status).model_dump()
        )

    deposit_outcome: str | None = None
    if body.status == AppointmentStatus.CONFIRMED:
        # One staff confirmation (counts only from 0, R1) and the CONFIRMED status,
        # logged with source `hub` by register_confirmation itself.
        await reminder_schedule.register_confirmation(
            session,
            appointment=appt,
            reminder_id=None,
            source=reminder_schedule.CONFIRMATION_SOURCE_STAFF,
            now=now,
        )
    else:
        previous_status = appt.status
        appt.status = body.status
        appt.updated_at = now
        log_status_transition(
            appointment_id=appt.id,
            tenant_id=tenant.id,
            old_status=previous_status,
            new_status=body.status,
            source=SOURCE_HUB,
            idempotency_key=f"status:{appt.id}:{body.status.value}",
        )
        # Money hooks (PROMPT S3 section 4): a CANCELLED/NO_SHOW transition is a real
        # money event for a Pix deposit, exactly like POST /cancel.
        if body.status == AppointmentStatus.CANCELLED:
            deposit_outcome = await deposit_lifecycle.on_appointment_cancelled(
                session, tenant=tenant, appointment=appt, waba_token=None
            )
        elif body.status == AppointmentStatus.NO_SHOW:
            deposit_outcome = await deposit_lifecycle.on_no_show(
                session, tenant=tenant, appointment=appt
            )
        # TASK-032: the reminder schedule follows the status.
        if body.status == AppointmentStatus.SCHEDULED:
            reminder_schedule.reset_confirmation(appt)
        elif body.status in TERMINAL_APPOINTMENT_STATUSES:
            await reminder_schedule.cancel_reminders(
                session, appt.id, reason=f"status_{body.status.value}"
            )

    await session.commit()
    await session.refresh(appt)
    notice: staff_patient_message.NoticeResult | None = None

    logger.info(
        "calendar_appointment_status_updated",
        appointment_id=str(appt.id),
        status=body.status.value,
        deposit_outcome=deposit_outcome,
        patient_notice=notice.code if notice is not None else None,
    )
    deposit_status = await _deposit_status_value(session, appt.id)
    read = _appointment_read(appt, deposit_status=deposit_status, deposit_outcome=deposit_outcome)
    return AppointmentActionRead(
        **read.model_dump(),
        patient_notice=notice.code if notice is not None else None,
        whatsapp_link=notice.whatsapp_link if notice is not None else None,
    )
```

- [ ] **Step 7: Run the tests**

Run: `pyt tests/test_hub_status_guards.py tests/test_hub_calendar_confirmation.py tests/test_hub_calendar_money.py tests/test_reminder_v2_hub_wiring.py -q`
Expected: PASS.

- [ ] **Step 8: Lint and commit**

```bash
uvx ruff format tests/_r7_support.py tests/test_hub_status_guards.py
uvx ruff check --fix tests/_r7_support.py tests/test_hub_status_guards.py
uvx ruff check src/secretaria/services/appointment_status.py src/secretaria/schemas/calendar.py src/secretaria/api/hub/calendar.py tests/test_hub_calendar_confirmation.py
git diff --stat
git add src/secretaria/services/appointment_status.py src/secretaria/schemas/calendar.py src/secretaria/api/hub/calendar.py tests/_r7_support.py tests/test_hub_status_guards.py tests/test_hub_calendar_confirmation.py
git commit -m "feat(hub): PATCH status guards - no_show_before_start, no resurrection (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 7: Staff "Confirmar" tells the patient, with the reminder's three buttons

**Files:**
- Create: `src/secretaria/services/clinic_action_notice.py`
- Modify: `src/secretaria/api/hub/calendar.py` (helper `_patient_of`; the `notice` block of `update_appointment_status`; import)
- Test: `tests/test_hub_staff_confirm_notice.py`

**Interfaces:**
- Consumes: `send_clinic_notice`, `NoticeResult`, `paid_notice_authorised` (Task 3); `ensure_staff_notice_row`, `current_staff_notice_row`, `retire_staff_notice_row`, `REMINDER_KIND_STAFF_CONFIRM` (Task 5); `load_reminder_content`, `local_start`, `reminder_buttons`; `reminder_hooks.enabled_for`; `MAX_CONFIRMATIONS`.
- Produces (in `secretaria.services.clinic_action_notice`):
  - `CONFIRM_TEXT = "Seu médico confirmou {subject} de {day} às {time} com {doctor}. Você está ciente?"`
  - `def confirm_text(content: ReminderContent) -> str`
  - `def buttons_allowed(tenant, appointment, now: datetime) -> bool`
  - `async def _card_notice(session, tenant, appointment, patient, *, kind: str, body: str, allow_paid: bool, now: datetime, once_per_start: bool) -> NoticeResult | None` (reused by Task 11)
  - `async def notify_staff_confirmation(session, tenant, appointment, patient, *, allow_paid: bool, now: datetime) -> NoticeResult | None`
  - `api/hub/calendar.py::_patient_of(session, tenant, appt) -> Patient | None` (reused by Tasks 8, 11, 12)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hub_staff_confirm_notice.py`:

```python
"""Staff "Confirmar" tells the patient and reuses the reminder's tap flow (TASK-032 R7 §2)."""

# ruff: noqa: F811

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from httpx import AsyncClient

from secretaria.core import database as core_database
from secretaria.models import AppointmentStatus, Tenant
from secretaria.services import reminder_opening
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.workers.shared import reminder_actions as ra
from secretaria.workers.shared.greeting import _format_appointment_when
from tests._r7_support import (  # noqa: F401
    acting,
    patch_status,
    r7,
    recent,
    sent,
    set_appointment,
    set_tenant,
    setup_world,
    staff_rows,
    tap,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import WA_ID, outbound_messages, reload_appointment

SP = ZoneInfo("America/Sao_Paulo")


def _expected_text(world, doctor: str = "Dra. Ana", subject: str = "sua consulta") -> str:
    local = world.start_at.astimezone(SP)
    return (
        f"Seu médico confirmou {subject} de {local:%d/%m/%Y} às {local:%H:%M} "
        f"com {doctor}. Você está ciente?"
    )


async def test_confirm_counts_once_and_sends_the_card_with_the_three_buttons(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting, professional_name="Dra. Ana")

    response = await patch_status(client, world.appointment.id, "confirmed")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmed" and body["confirmation_count"] == 1
    assert body["patient_notice"] == "whatsapp_sent" and body["whatsapp_link"] is None
    [row] = await staff_rows(db, world.appointment.id, "staff_confirm")
    assert row.status == "sent" and row.warn_due_at is None and row.invalidated_at is None
    assert sent() == [
        (
            "buttons",
            WA_ID,
            _expected_text(world),
            [
                (f"remconfirm|{row.id}", "Confirmar"),
                (f"remcancel|{row.id}", "Cancelar"),
                (f"remedit|{row.id}", "Alterar Dados"),
            ],
        )
    ]


async def test_the_patient_confirming_the_card_reaches_two_and_stops_the_prompts(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting, professional_name="Dra. Ana")
    await patch_status(client, world.appointment.id, "confirmed")
    [row] = await staff_rows(db, world.appointment.id, "staff_confirm")

    await tap(world, "remconfirm", row.id)

    appointment = await reload_appointment(db, world.appointment.id)
    assert appointment.confirmation_count == 2
    when = _format_appointment_when(world.start_at, "America/Sao_Paulo")
    assert ("text", WA_ID, ra.CONFIRMED_TEXT.format(when=when)) in sent()
    async with core_database.async_session_factory() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        decision = await reminder_opening.decide_reminder_opening(
            session,
            tenant=tenant,
            patient_id=world.patient.id,
            last_activity_at=datetime.now(UTC) - timedelta(hours=7),
            now=datetime.now(UTC),
        )
    assert decision.reason == reminder_opening.SKIP_CONFIRMED_TWICE


async def test_cancelar_and_alterar_dados_follow_the_reminder_paths(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting)
    await patch_status(client, world.appointment.id, "confirmed")
    [row] = await staff_rows(db, world.appointment.id, "staff_confirm")
    reply = ra._ReplyContext(
        channel="whatsapp", conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
    )

    assert await ra.handle_reminder_button(reply, "remedit", str(row.id)) == (
        ra.CONTINUE_EDIT,
        str(world.appointment.id),
    )
    await tap(world, "remcancel", row.id)

    last = sent()[-1]
    assert last[0] == "buttons" and last[2].startswith("Tem certeza que quer cancelar")
    assert (await reload_appointment(db, world.appointment.id)).status == AppointmentStatus.CONFIRMED


async def test_a_second_confirm_for_the_same_time_sends_nothing_more(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting)
    first = await patch_status(client, world.appointment.id, "confirmed")

    again = await patch_status(client, world.appointment.id, "confirmed")

    assert first.json()["patient_notice"] == "whatsapp_sent"
    assert again.status_code == 200 and again.json()["patient_notice"] is None
    assert len(sent()) == 1
    assert len(await staff_rows(db, world.appointment.id, "staff_confirm")) == 1


async def test_outside_the_window_it_asks_and_a_paid_retry_sends_the_template(
    client: AsyncClient, db, acting, r7
):
    world = await setup_world(db, acting, last_inbound_at=recent(30))

    refused = await patch_status(client, world.appointment.id, "confirmed")

    assert refused.json()["patient_notice"] == "whatsapp_outside_window"
    assert refused.json()["whatsapp_link"] == f"https://wa.me/{WA_ID}"
    assert sent() == []
    [retired] = await staff_rows(db, world.appointment.id, "staff_confirm")
    assert retired.invalidated_at is not None  # no card went out: a retry gets a fresh row

    paid = await patch_status(
        client, world.appointment.id, "confirmed", notify_outside_window=True
    )

    assert paid.json()["patient_notice"] == "whatsapp_sent"
    [(kind, to, template, _lang, variables, _payloads)] = sent()
    assert (kind, to, template) == ("template", WA_ID, "appointment_reminder")
    assert variables == [_expected_text(world, doctor="a equipe da Clínica Olhar")]
    assert len(r7["usage"]) == 1


async def test_the_clinics_standing_yes_needs_no_question(client: AsyncClient, db, acting):
    world = await setup_world(db, acting, last_inbound_at=recent(30))
    await set_tenant(db, world.tenant.id, paid_notices_auto_approved=True)

    response = await patch_status(client, world.appointment.id, "confirmed")

    assert response.json()["patient_notice"] == "whatsapp_sent"
    assert sent()[0][0] == "template"


async def test_with_reminders_switched_off_the_text_goes_without_dead_buttons(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting, v2=False)

    response = await patch_status(client, world.appointment.id, "confirmed")

    assert response.json()["patient_notice"] == "whatsapp_sent"
    assert sent()[0][0] == "text"
    [row] = await staff_rows(db, world.appointment.id, "staff_confirm")
    assert row.with_prompt is False


async def test_a_patient_who_already_confirmed_twice_gets_the_text_only(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting, confirmation_count=2)
    await set_appointment(db, world.appointment.id, status=AppointmentStatus.CONFIRMED)

    response = await patch_status(client, world.appointment.id, "confirmed")

    assert response.json()["confirmation_count"] == 2
    assert sent()[0][0] == "text"


async def test_an_appointment_for_someone_else_names_them(client: AsyncClient, db, acting):
    world = await setup_world(db, acting, attendee_name="João Pedro", professional_name="Dra. Ana")

    await patch_status(client, world.appointment.id, "confirmed")

    assert sent()[0][2] == _expected_text(world, subject="a consulta de João Pedro")


async def test_a_portal_patient_gets_the_card_in_the_chat_and_an_email(
    client: AsyncClient, db, acting, r7
):
    world = await setup_world(
        db, acting, channel=CHANNEL_BRAIN_MESSAGE, wa_id=None, email="maria@exemplo.com"
    )

    response = await patch_status(client, world.appointment.id, "confirmed")

    assert response.json()["patient_notice"] == "portal_chat_email"
    [row] = await staff_rows(db, world.appointment.id, "staff_confirm")
    assert row.channel == "chat"
    [message] = await outbound_messages(db, world.conversation.id)
    assert [o["id"] for o in message.interactive["options"]][0] == f"remconfirm|{row.id}"
    assert [m[1] for m in r7["mail"]] == ["clinic_message_patient"]
    assert sent() == []


async def test_a_confirm_after_the_start_counts_but_sends_nothing(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting, start_at=datetime.now(UTC) - timedelta(minutes=10))

    response = await patch_status(client, world.appointment.id, "confirmed")

    assert response.json()["confirmation_count"] == 1
    assert response.json()["patient_notice"] is None and sent() == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pyt tests/test_hub_staff_confirm_notice.py -q`
Expected: FAIL — `patient_notice` is `None` and nothing was sent.

- [ ] **Step 3: Create the notice module**

Create `src/secretaria/services/clinic_action_notice.py`:

```python
"""What the patient is told when the clinic acts on the agenda (TASK-032 R7).

Spec: docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md §2.

* "Confirmar" (staff) -> "Seu médico confirmou ... Você está ciente?" + Confirmar /
  Cancelar / Alterar Dados.
* "Editar/Remarcar"   -> "A clínica alterou ..." with before -> after + the same buttons.
* "Compareceu"        -> the post-consult message, once per appointment.

The buttons are R2's reminder ids pointing at an `appointment_reminders` row of kind
`staff_confirm` / `staff_edit` (services/reminder_schedule.py::ensure_staff_notice_row),
so a tap runs the unchanged reminder flow (workers/shared/reminder_actions.py): Confirmar
counts one more confirmation (R1 dedupes on the row), Cancelar and Alterar Dados follow
R3/R6. Buttons go only where they can work: the clinic's reminders switch ON, fewer than
two confirmations, the appointment not started - otherwise the same text goes alone.

Delivery is `staff_patient_message.send_clinic_notice` (channel, window, paid template).
These functions commit (the card's row must exist before the patient can tap it) and
never raise. Logs carry ids and codes only.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Patient, Tenant
from secretaria.models.appointment_reminder import (
    REMINDER_CHANNEL_CHAT,
    REMINDER_CHANNEL_WHATSAPP,
    REMINDER_KIND_STAFF_CONFIRM,
)
from secretaria.services import reminder_hooks, reminder_schedule
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.patient_context import as_utc
from secretaria.services.reminder_text import (
    ReminderContent,
    load_reminder_content,
    local_start,
    reminder_buttons,
)
from secretaria.services.staff_patient_message import NoticeResult, send_clinic_notice

logger = get_logger(__name__)

CONFIRM_TEXT = "Seu médico confirmou {subject} de {day} às {time} com {doctor}. Você está ciente?"


def _one_line(value) -> str:
    return " ".join(str(value or "").split())


def _subject(content: ReminderContent) -> str:
    attendee = _one_line(content.attendee_name)
    return f"a consulta de {attendee}" if attendee else "sua consulta"


def _doctor(content: ReminderContent) -> str:
    """The professional's name, or R2's fallback "a equipe da <clínica>"."""
    return _one_line(content.doctor_name) or f"a equipe da {_one_line(content.clinic_name)}"


def confirm_text(content: ReminderContent) -> str:
    local = local_start(content)
    return CONFIRM_TEXT.format(
        subject=_subject(content),
        day=f"{local:%d/%m/%Y}",
        time=f"{local:%H:%M}",
        doctor=_doctor(content),
    )


def buttons_allowed(tenant: Tenant, appointment: Appointment, now: datetime) -> bool:
    """Only buttons a tap can honour (the R6 handler refuses every other case)."""
    return (
        reminder_hooks.enabled_for(tenant)
        and (appointment.confirmation_count or 0) < reminder_schedule.MAX_CONFIRMATIONS
        and appointment.start_at is not None
        and as_utc(appointment.start_at) > now
    )


def _row_channel(patient: Patient) -> str:
    return REMINDER_CHANNEL_CHAT if patient.channel == CHANNEL_BRAIN_MESSAGE else REMINDER_CHANNEL_WHATSAPP


async def _card_notice(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient | None,
    *,
    kind: str,
    body: str,
    allow_paid: bool,
    now: datetime,
    once_per_start: bool,
) -> NoticeResult | None:
    """Row first (committed, so a tap can resolve it), then the card.

    `once_per_start`: None (nothing sent) when this start already has an active `kind`
    row - the patient already has that card. A card that did not reach the patient
    retires the row it just created, so a later retry starts clean.
    """
    tenant_id, appointment_id = tenant.id, appointment.id
    usage_key = f"{kind}:{appointment_id}"
    if (
        patient is None
        or patient.tenant_id != tenant_id
        or appointment.patient_id is None
        or appointment.start_at is None
    ):
        return await send_clinic_notice(
            session, tenant, appointment, patient, body=body, allow_paid=allow_paid,
            usage_key=usage_key, now=now,
        )
    if once_per_start and (
        await reminder_schedule.current_staff_notice_row(session, appointment, kind=kind)
        is not None
    ):
        logger.info(
            "clinic_notice_skipped", appointment_id=str(appointment_id), kind=kind,
            reason="card_already_shown",
        )
        return None
    with_prompt = buttons_allowed(tenant, appointment, now)
    row, created = await reminder_schedule.ensure_staff_notice_row(
        session, appointment, kind=kind, channel=_row_channel(patient), with_prompt=with_prompt,
        now=now,
    )
    row_id = row.id
    await session.commit()
    result = await send_clinic_notice(
        session,
        tenant,
        appointment,
        patient,
        body=body,
        buttons=reminder_buttons(row_id) if with_prompt else None,
        allow_paid=allow_paid,
        usage_key=f"{kind}:{row_id}",
        now=now,
    )
    if created and not result.delivered:
        await reminder_schedule.retire_staff_notice_row(session, row_id, tenant_id=tenant_id)
        await session.commit()
    logger.info(
        "clinic_notice",
        appointment_id=str(appointment_id),
        kind=kind,
        patient_notice=result.code,
        with_buttons=with_prompt,
    )
    return result


async def notify_staff_confirmation(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient | None,
    *,
    allow_paid: bool,
    now: datetime,
) -> NoticeResult | None:
    """The clinic confirmed: "Você está ciente?" once per appointment time.

    None when nothing was due: the appointment already started (the buttons would be
    refused) or the patient already has this time's confirm card.
    """
    if appointment.start_at is None or as_utc(appointment.start_at) <= now:
        return None
    content = await load_reminder_content(session, tenant, appointment)
    return await _card_notice(
        session,
        tenant,
        appointment,
        patient,
        kind=REMINDER_KIND_STAFF_CONFIRM,
        body=confirm_text(content),
        allow_paid=allow_paid,
        now=now,
        once_per_start=True,
    )
```

(`uvx ruff format` will re-wrap the two compressed calls above.)

- [ ] **Step 4: Wire it into PATCH**

In `src/secretaria/api/hub/calendar.py`: add `clinic_action_notice,` to the `from secretaria.services import (...)` block. Right after `_professional_name`, add:

```python
async def _patient_of(session: AsyncSession, tenant: Tenant, appt: Appointment) -> Patient | None:
    """The appointment's patient, tenant-scoped (a foreign id resolves to nobody)."""
    if appt.patient_id is None:
        return None
    return await session.scalar(
        select(Patient).where(Patient.id == appt.patient_id, Patient.tenant_id == tenant.id)
    )
```

In `update_appointment_status`, replace the line

```python
    notice: staff_patient_message.NoticeResult | None = None
```

with:

```python
    notice: staff_patient_message.NoticeResult | None = None
    allow_paid = staff_patient_message.paid_notice_authorised(tenant, body.notify_outside_window)
    if body.status == AppointmentStatus.CONFIRMED:
        # Spec §2: "Seu médico confirmou ..." + Confirmar / Cancelar / Alterar Dados.
        notice = await clinic_action_notice.notify_staff_confirmation(
            session,
            tenant,
            appt,
            await _patient_of(session, tenant, appt),
            allow_paid=allow_paid,
            now=now,
        )
        await session.refresh(appt)
```

- [ ] **Step 5: Run the tests**

Run: `pyt tests/test_hub_staff_confirm_notice.py tests/test_hub_status_guards.py tests/test_hub_calendar_confirmation.py tests/test_hub_calendar_money.py tests/test_reminder_v2_buttons.py tests/test_reminder_r6_buttons.py -q`
Expected: PASS.

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff format src/secretaria/services/clinic_action_notice.py tests/test_hub_staff_confirm_notice.py
uvx ruff check --fix src/secretaria/services/clinic_action_notice.py tests/test_hub_staff_confirm_notice.py
uvx ruff check src/secretaria/api/hub/calendar.py
git diff --stat
git add src/secretaria/services/clinic_action_notice.py src/secretaria/api/hub/calendar.py tests/test_hub_staff_confirm_notice.py
git commit -m "feat(hub): staff confirmation tells the patient with the reminder buttons (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 8: "Compareceu" pushes the post-consult message, once

**Files:**
- Modify: `src/secretaria/services/clinic_action_notice.py` (append)
- Modify: `src/secretaria/services/patient_context.py` (`find_post_consult_followup`)
- Modify: `src/secretaria/api/hub/calendar.py` (the `notice` block of `update_appointment_status`)
- Test: `tests/test_hub_attended_post_consult.py`

**Interfaces:**
- Consumes: `Appointment.post_consult_notified_at` (Task 1), `send_clinic_notice` (Task 3), `_patient_of` (Task 7).
- Produces: `clinic_action_notice.POST_CONSULT_DEFAULT`, `post_consult_text(tenant, doctor_name: str | None) -> str`, `async notify_attended(session, tenant, appointment, patient, *, allow_paid: bool, now: datetime) -> NoticeResult | None` (None = already sent for this appointment). `find_post_consult_followup` returns None for an appointment whose `post_consult_notified_at` is set.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hub_attended_post_consult.py`:

```python
"""Compareceu -> the post-consult message now, once per appointment (TASK-032 R7 §2)."""

# ruff: noqa: F811

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import update

from secretaria.models import AppointmentStatus, Message
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.patient_context import find_post_consult_followup
from tests._r7_support import (  # noqa: F401
    acting,
    patch_status,
    r7,
    recent,
    sent,
    set_appointment,
    set_tenant,
    setup_world,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import WA_ID, outbound_messages, reload_appointment

DEFAULT = (
    "Como foi a sua consulta com Dra. Ana? Conte pra gente como você está se sentindo. "
    "Se precisar de algo, é só responder por aqui."
)


def _just_now() -> datetime:
    return datetime.now(UTC) - timedelta(minutes=40)


async def test_attended_sends_the_clinics_own_text_and_marks_the_appointment(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting, start_at=_just_now())
    await set_tenant(db, world.tenant.id, post_consult_message="Obrigado pela visita! 💙")

    response = await patch_status(client, world.appointment.id, "attended")

    assert response.status_code == 200
    assert response.json()["patient_notice"] == "whatsapp_sent"
    assert sent() == [("text", WA_ID, "Obrigado pela visita! 💙")]
    assert (await reload_appointment(db, world.appointment.id)).post_consult_notified_at


async def test_without_a_configured_text_the_default_asks_how_it_went(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting, start_at=_just_now(), professional_name="Dra. Ana")

    await patch_status(client, world.appointment.id, "attended")

    assert sent() == [("text", WA_ID, DEFAULT)]


async def test_marked_twice_or_corrected_back_it_is_sent_once(client: AsyncClient, db, acting):
    world = await setup_world(db, acting, start_at=_just_now())

    await patch_status(client, world.appointment.id, "attended")
    await patch_status(client, world.appointment.id, "attended")  # no-op
    await patch_status(client, world.appointment.id, "no_show")  # correction
    again = await patch_status(client, world.appointment.id, "attended")  # correction back

    assert again.status_code == 200 and again.json()["patient_notice"] is None
    assert len(sent()) == 1


async def test_not_delivered_releases_the_marker_for_the_next_open_follow_up(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting, start_at=_just_now(), last_inbound_at=recent(30))

    response = await patch_status(client, world.appointment.id, "attended")

    assert response.json()["patient_notice"] == "whatsapp_outside_window"
    assert sent() == []
    assert (await reload_appointment(db, world.appointment.id)).post_consult_notified_at is None


async def test_a_portal_patient_gets_it_in_the_chat_and_the_next_open_does_not_repeat_it(
    client: AsyncClient, db, acting, r7
):
    start = datetime.now(UTC) - timedelta(hours=3)
    world = await setup_world(
        db, acting, start_at=start, channel=CHANNEL_BRAIN_MESSAGE, wa_id=None, email="m@x.com"
    )
    # Marked "Compareceu" while the consult was still running: the message is older
    # than the consult's end, which alone would not stop the next-open question.
    await set_appointment(db, world.appointment.id, end_at=datetime.now(UTC) + timedelta(hours=1))

    response = await patch_status(client, world.appointment.id, "attended")
    assert response.json()["patient_notice"] == "portal_chat_email"
    assert len(await outbound_messages(db, world.conversation.id)) == 1

    # Later: the consult ended AFTER every message of the conversation, so only the
    # marker can stop the next open from asking "como foi?" a second time.
    async with db() as session:
        await session.execute(
            update(Message)
            .where(Message.conversation_id == world.conversation.id)
            .values(created_at=datetime.now(UTC) - timedelta(hours=2))
        )
        await session.commit()
    await set_appointment(db, world.appointment.id, end_at=datetime.now(UTC) - timedelta(minutes=1))
    async with db() as session:
        followup = await find_post_consult_followup(
            session, world.tenant.id, world.patient.id, world.conversation.id
        )
    assert followup is None


async def test_without_the_marker_the_next_open_follow_up_is_unchanged(db, acting):
    start = datetime.now(UTC) - timedelta(hours=3)
    world = await setup_world(db, acting, start_at=start, last_inbound_at=None)
    await set_appointment(db, world.appointment.id, status=AppointmentStatus.ATTENDED)

    async with db() as session:
        followup = await find_post_consult_followup(
            session, world.tenant.id, world.patient.id, world.conversation.id
        )

    assert followup is not None and followup["id"] == str(world.appointment.id)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pyt tests/test_hub_attended_post_consult.py -q`
Expected: FAIL — `patient_notice` is `None`, nothing sent; the Portal follow-up test fails on the follow-up assertion.

- [ ] **Step 3: Implement the message**

Append to `src/secretaria/services/clinic_action_notice.py` (add `from sqlalchemy import select, update` and `Professional` to the `from secretaria.models import ...` line):

```python
# --- "Compareceu" -> the post-consult message (spec §2) -----------------------------

POST_CONSULT_DEFAULT = (
    "Como foi a sua consulta{doctor}? Conte pra gente como você está se sentindo. "
    "Se precisar de algo, é só responder por aqui."
)


def post_consult_text(tenant: Tenant, doctor_name: str | None) -> str:
    """The clinic's own `post_consult_message`, verbatim; else the default question."""
    own = (tenant.post_consult_message or "").strip()
    if own:
        return own
    doctor = _one_line(doctor_name)
    return POST_CONSULT_DEFAULT.format(doctor=f" com {doctor}" if doctor else "")


async def notify_attended(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient | None,
    *,
    allow_paid: bool,
    now: datetime,
) -> NoticeResult | None:
    """The post-consult message right after "Compareceu", once per appointment.

    `post_consult_notified_at` is claimed with a conditional UPDATE (two clicks, two
    staff: one winner) and committed BEFORE the send; it is cleared again when the
    message did not reach the patient, so the next-open follow-up
    (services/patient_context.py::find_post_consult_followup) still asks later. None =
    already sent for this appointment.
    """
    tenant_id, appointment_id = tenant.id, appointment.id
    claimed = await session.execute(
        update(Appointment)
        .where(
            Appointment.id == appointment_id,
            Appointment.tenant_id == tenant_id,
            Appointment.post_consult_notified_at.is_(None),
        )
        .values(post_consult_notified_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        logger.info("post_consult_notice_skipped", appointment_id=str(appointment_id))
        return None
    await session.commit()
    doctor = None
    if appointment.professional_id is not None:
        doctor = await session.scalar(
            select(Professional.name).where(
                Professional.id == appointment.professional_id,
                Professional.tenant_id == tenant_id,
            )
        )
    result = await send_clinic_notice(
        session,
        tenant,
        appointment,
        patient,
        body=post_consult_text(tenant, doctor),
        allow_paid=allow_paid,
        usage_key=f"postconsult:{appointment_id}",
        now=now,
    )
    if not result.delivered:
        await session.execute(
            update(Appointment)
            .where(Appointment.id == appointment_id, Appointment.tenant_id == tenant_id)
            .values(post_consult_notified_at=None)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    logger.info(
        "post_consult_notice", appointment_id=str(appointment_id), patient_notice=result.code
    )
    return result
```

- [ ] **Step 4: The next-open follow-up skips a notified appointment**

In `src/secretaria/services/patient_context.py::find_post_consult_followup`, right after

```python
    if appointment is None:
        return None
```

insert:

```python
    if appointment.post_consult_notified_at is not None:
        # TASK-032 R7: the clinic's "Compareceu" already delivered the post-consult
        # message for this appointment - asking again on the next open would repeat it.
        return None
```

- [ ] **Step 5: Wire it into PATCH**

In `src/secretaria/api/hub/calendar.py::update_appointment_status`, replace:

```python
    if body.status == AppointmentStatus.CONFIRMED:
        # Spec §2: "Seu médico confirmou ..." + Confirmar / Cancelar / Alterar Dados.
        notice = await clinic_action_notice.notify_staff_confirmation(
            session,
            tenant,
            appt,
            await _patient_of(session, tenant, appt),
            allow_paid=allow_paid,
            now=now,
        )
        await session.refresh(appt)
```

with:

```python
    if body.status == AppointmentStatus.CONFIRMED:
        # Spec §2: "Seu médico confirmou ..." + Confirmar / Cancelar / Alterar Dados.
        notice = await clinic_action_notice.notify_staff_confirmation(
            session,
            tenant,
            appt,
            await _patient_of(session, tenant, appt),
            allow_paid=allow_paid,
            now=now,
        )
        await session.refresh(appt)
    elif body.status == AppointmentStatus.ATTENDED:
        # Spec §2: the post-consult message now, once per appointment.
        notice = await clinic_action_notice.notify_attended(
            session,
            tenant,
            appt,
            await _patient_of(session, tenant, appt),
            allow_paid=allow_paid,
            now=now,
        )
        await session.refresh(appt)
```

- [ ] **Step 6: Run the tests**

Run: `pyt tests/test_hub_attended_post_consult.py tests/test_hub_status_guards.py tests/test_hub_staff_confirm_notice.py tests/test_hub_calendar_confirmation.py tests/test_portal_context_opening.py -q`
Expected: PASS.

- [ ] **Step 7: Lint and commit**

```bash
uvx ruff format tests/test_hub_attended_post_consult.py && uvx ruff check --fix tests/test_hub_attended_post_consult.py
uvx ruff format src/secretaria/services/clinic_action_notice.py && uvx ruff check --fix src/secretaria/services/clinic_action_notice.py
uvx ruff check src/secretaria/services/patient_context.py src/secretaria/api/hub/calendar.py
git diff --stat
git add src/secretaria/services/clinic_action_notice.py src/secretaria/services/patient_context.py src/secretaria/api/hub/calendar.py tests/test_hub_attended_post_consult.py
git commit -m "feat(hub): Compareceu sends the post-consult message once (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 9: One row writer for "Alterar Dados" (R6) and "Editar/Remarcar" (R7)

A pure extraction: no behaviour changes for the patient's edit. It exists so the clinic's edit (Task 10) writes the row with exactly the code R6 uses.

**Files:**
- Create: `src/secretaria/services/appointment_edit_write.py`
- Modify: `src/secretaria/workers/shared/appointment_edit_apply.py` (`_row_draft` and the write block of `apply_appointment_edit`)
- Test: `tests/test_appointment_edit_write.py`

**Interfaces:**
- Consumes: `EditDraft`, `resolve_booking_plan_ids`, `log_status_transition`, `as_utc`.
- Produces:
  - `services.appointment_edit_write.row_draft(appointment, timezone: str | None) -> EditDraft`
  - `async services.appointment_edit_write.write_appointment_edit(session, appointment, *, tenant_id: UUID, timezone: str | None, edit: dict, source: str, idempotency_key: str) -> list[str]` — mutates the (caller-locked) row, never commits; returns R6's changed-field names (`data, horário, serviço, médico, convênio, paciente`). `edit` keys: `appointment_type, professional_id, insurance, attendee_name, google_event_id, google_event_link, start_at, end_at, time_changed, doctor_changed`, optional `calendar_changed` (defaults to `doctor_changed`).
  - `workers.shared.appointment_edit_apply._row_draft` stays importable (alias).

- [ ] **Step 1: Write the failing test**

Create `tests/test_appointment_edit_write.py`:

```python
"""The shared row writer of an appointment edit (TASK-032 R6 extraction, used by R7)."""

from datetime import timedelta

import pytest

from secretaria.models import Appointment, AppointmentStatus
from secretaria.services import appointment_edit_write as writer
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import seed_world


def _edit(world, **overrides) -> dict:
    base = {
        "appointment_type": "Consulta",
        "professional_id": None,
        "insurance": None,
        "attendee_name": None,
        "google_event_id": world.appointment.google_event_id,
        "google_event_link": None,
        "start_at": world.start_at,
        "end_at": world.start_at + timedelta(minutes=30),
        "time_changed": False,
        "doctor_changed": False,
    }
    base.update(overrides)
    return base


async def _write(db, world, edit, *, source="hub"):  # noqa: F811
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        fields = await writer.write_appointment_edit(
            session,
            appointment,
            tenant_id=world.tenant.id,
            timezone="America/Sao_Paulo",
            edit=edit,
            source=source,
            idempotency_key="key-1",
        )
        await session.commit()
        await session.refresh(appointment)
        return fields, appointment


@pytest.fixture
def transitions(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(writer, "log_status_transition", lambda **kw: calls.append(kw))
    return calls


async def test_a_new_time_moves_the_row_and_logs_a_reschedule(db, transitions):  # noqa: F811
    world = await seed_world(db)
    new = world.start_at + timedelta(hours=1)

    fields, row = await _write(
        db, world, _edit(world, start_at=new, end_at=new + timedelta(minutes=30), time_changed=True)
    )

    assert fields == ["horário"]
    assert row.status == AppointmentStatus.RESCHEDULED
    assert row.start_at.replace(tzinfo=new.tzinfo) == new
    [call] = transitions
    assert (call["source"], call["idempotency_key"]) == ("hub", "key-1")
    assert call["new_status"] == AppointmentStatus.RESCHEDULED


async def test_without_a_new_time_the_status_and_start_stay(db, transitions):  # noqa: F811
    world = await seed_world(db)

    fields, row = await _write(
        db,
        world,
        _edit(world, appointment_type="Retorno", end_at=world.start_at + timedelta(minutes=40)),
    )

    assert fields == ["serviço"]
    assert row.status == AppointmentStatus.SCHEDULED and transitions == []
    assert row.appointment_type == "Retorno"
    assert (row.end_at - row.start_at) == timedelta(minutes=40)


async def test_convenio_and_attendee_are_written_and_reported(db, transitions):  # noqa: F811
    world = await seed_world(db)

    fields, row = await _write(db, world, _edit(world, insurance="Particular", attendee_name="João"))

    assert fields == ["convênio", "paciente"]
    assert (row.insurance, row.attendee_name) == ("Particular", "João")
    assert row.insurance_plan_id is None and row.insurance_professional_plan_id is None


async def test_the_event_link_changes_only_with_the_calendar(db, transitions):  # noqa: F811
    world = await seed_world(db, google_event_id="evt-1")

    _, kept = await _write(
        db, world, _edit(world, google_event_link="https://new", calendar_changed=False)
    )
    assert kept.google_event_link is None

    _, moved = await _write(
        db,
        world,
        _edit(world, google_event_id="evt-2", google_event_link="https://new", calendar_changed=True),
    )
    assert (moved.google_event_id, moved.google_event_link) == ("evt-2", "https://new")


def test_row_draft_is_the_r6_draft_of_the_row():
    from secretaria.workers.shared import appointment_edit_apply

    assert appointment_edit_apply._row_draft is writer.row_draft
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pyt tests/test_appointment_edit_write.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'secretaria.services.appointment_edit_write'`.

- [ ] **Step 3: Create the writer**

Create `src/secretaria/services/appointment_edit_write.py`:

```python
"""Write an edit onto the SAME appointment row (TASK-032 R6, shared with R7).

Two callers, one writer, so the patient's "Alterar Dados"
(workers/shared/appointment_edit_apply.py) and the clinic's "Editar/Remarcar"
(services/staff_appointment_edit.py) can never disagree about what an edit does to a
row: the convênio plan ids re-resolved by the TASK-006/008 catalogue rules, the
service / doctor / attendee / event fields, the end, and - only when the start moved -
the new start plus the RESCHEDULED status (a logged transition with the caller's
source). Each caller keeps its own guards (the patient's Pix limits; the clinic's
live/tenant checks), its own outbox decision and its own transaction: this function
never commits.
"""

from datetime import UTC
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import Appointment, AppointmentStatus
from secretaria.services.appointment_edit import EditDraft
from secretaria.services.appointment_status import log_status_transition
from secretaria.services.insurance_catalog import resolve_booking_plan_ids
from secretaria.services.patient_context import as_utc


def row_draft(appointment, timezone: str | None) -> EditDraft:
    """The appointment row as an `EditDraft` in the clinic's zone (current == original)."""
    return EditDraft.from_appointment(
        {
            "id": str(appointment.id),
            "appointment_type": appointment.appointment_type,
            "professional_id": appointment.professional_id,
            "start_at": appointment.start_at,
            "end_at": appointment.end_at,
            "insurance": appointment.insurance,
            "attendee_name": appointment.attendee_name,
        },
        ZoneInfo(timezone or "America/Sao_Paulo"),
    )


async def write_appointment_edit(
    session: AsyncSession,
    appointment: Appointment,
    *,
    tenant_id: UUID,
    timezone: str | None,
    edit: dict,
    source: str,
    idempotency_key: str,
) -> list[str]:
    """Apply `edit` (R6's dict shape) to the caller-locked row; returns what changed.

    The names are R6's (`EditDraft.changed()`: data, horário, serviço, médico,
    convênio, paciente) - what the doctor e-mail outbox records.
    """
    before = row_draft(appointment, timezone)
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
    if edit.get("calendar_changed", edit["doctor_changed"]):
        appointment.google_event_link = edit.get("google_event_link")
    appointment.end_at = as_utc(edit["end_at"]).astimezone(UTC)
    if edit["time_changed"]:
        appointment.start_at = as_utc(edit["start_at"]).astimezone(UTC)
        appointment.status = AppointmentStatus.RESCHEDULED
        log_status_transition(
            appointment_id=appointment.id,
            tenant_id=tenant_id,
            old_status=previous,
            new_status=AppointmentStatus.RESCHEDULED,
            source=source,
            idempotency_key=idempotency_key,
        )
    after = row_draft(appointment, timezone)
    return EditDraft(str(appointment.id), after.current, before.current).changed()
```

- [ ] **Step 4: Make R6 use it**

In `src/secretaria/workers/shared/appointment_edit_apply.py`:

(a) delete the whole `def _row_draft(appointment, timezone) -> EditDraft:` function and add, next to the other `secretaria.services` imports:

```python
from secretaria.services.appointment_edit_write import (
    row_draft as _row_draft,
    write_appointment_edit,
)
```

(b) inside `apply_appointment_edit`, replace the block that starts with `    previous = appointment.status` and ends with `    fields = EditDraft(str(appointment.id), after.current, before.current).changed()` (the plan-id resolution, the field assignments, the `if edit["time_changed"]:` status block, `moved = ...`, `after = ...`, `fields = ...`) with:

```python
    fields = await write_appointment_edit(
        session,
        appointment,
        tenant_id=tenant_id,
        timezone=current_tenant.timezone,
        edit=edit,
        source=SOURCE_FLOW,
        idempotency_key=f"edit:{appointment.id}:{edit['start_at'].isoformat()}",
    )
    moved = bool(edit["time_changed"]) and (
        reminder_hooks.enabled_for(tenant) or (appointment.confirmation_count or 0) > 0
    )
```

(c) remove the imports this leaves unused (ruff F401 will list them): `AppointmentStatus` from the models import, `EditDraft` (keep `appointment_email_version`), `log_status_transition` (keep `SOURCE_FLOW`), `resolve_booking_plan_ids`, and `ZoneInfo`.

- [ ] **Step 5: Run the writer test and every R6 suite**

Run: `pyt tests/test_appointment_edit_write.py tests/test_appointment_edit_apply.py tests/test_appointment_edit_safety.py tests/test_reminder_link_entry.py tests/test_professional_edit_notification.py tests/test_professional_edit_recovery.py tests/test_hub_release.py -q`
Expected: PASS (no R6 test changed).

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff format src/secretaria/services/appointment_edit_write.py tests/test_appointment_edit_write.py
uvx ruff check --fix src/secretaria/services/appointment_edit_write.py tests/test_appointment_edit_write.py
uvx ruff check src/secretaria/workers/shared/appointment_edit_apply.py
pyt tests/test_workers_layering.py tests/test_channel_branches.py -q
git diff --stat
git add src/secretaria/services/appointment_edit_write.py src/secretaria/workers/shared/appointment_edit_apply.py tests/test_appointment_edit_write.py
git commit -m "refactor(edit): one row writer shared by Alterar Dados and the hub edit (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 10: The clinic's edit — validate, move Google fail-closed, write the row

**Files:**
- Create: `src/secretaria/services/staff_appointment_edit.py`
- Test: `tests/test_staff_appointment_edit.py`

**Interfaces:**
- Consumes: `write_appointment_edit` (Task 9), `record_professional_edit`, `appointment_email_version`, `active_appointment_types`, `professional_appointment_types`, `load_service_catalog`, `normalize`, `build_event_description`, `CalendarUnavailableError`.
- Produces (in `secretaria.services.staff_appointment_edit`):
  - `EDITABLE_FIELDS = ("start_at", "service", "professional_id", "insurance", "attendee_name", "phone")`; `DOCTOR_EMAIL_FIELDS = frozenset({"data", "horário", "médico"})`; labels `LABEL_DATE="Data"`, `LABEL_TIME="Horário"`, `LABEL_SERVICE="Serviço"`, `LABEL_DOCTOR="Médico"`, `LABEL_INSURANCE="Convênio"`, `LABEL_ATTENDEE="Paciente"`, `LABEL_PHONE="Telefone de contato"`.
  - `class StaffEditError(Exception)` — attributes `status_code: int`, `code: str`, `message: str`, `extra: dict`.
  - `@dataclass(frozen=True) StaffEditRequest(fields: frozenset[str], start_at: datetime | None = None, service: str | None = None, professional_id: UUID | None = None, insurance: str | None = None, attendee_name: str | None = None, phone: str | None = None, allow_overlap: bool = False)`.
  - `@dataclass(frozen=True) FieldChange(label: str, before: str, after: str)`.
  - `@dataclass(frozen=True) StaffEditOutcome(appointment_id: UUID, changes: tuple[FieldChange, ...], changed_fields: tuple[str, ...], time_changed: bool, notice_id: str | None = None, notice_version: str | None = None)`.
  - `def normalize_phone(raw: str | None) -> str | None` (ValueError when invalid).
  - `async def apply_staff_edit(session, tenant, appointment, request, *, calendar_for: Callable[[UUID | None], Awaitable[Any]], now: datetime | None = None) -> StaffEditOutcome` — commits; raises `StaffEditError` (codes `not_live`, `not_a_patient_appointment`, `nothing_changed`, `start_in_past`, `unknown_professional`, `service_not_offered`, `invalid_phone`, `slot_unavailable`, `appointment_changed`, `calendar_unavailable`); whatever `calendar_for` raises (the hub's 409/422 `HTTPException`s) propagates untouched.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_staff_appointment_edit.py`:

```python
"""The clinic's Editar/Remarcar service (TASK-032 R7, spec 2026-10-09 §1/§3/§4)."""

# ruff: noqa: F811

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Patient,
    ProfessionalEditNotice,
    Tenant,
)
from secretaria.services import staff_appointment_edit as sae
from secretaria.services.calendar import CalendarUnavailableError
from secretaria.services.staff_appointment_edit import StaffEditError, StaffEditRequest
from tests._r7_support import FakeCalendar, add_professional, set_appointment, set_tenant, soon
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import WA_ID, reload_appointment, seed_world

SERVICES = [
    {"name": "Consulta", "is_active": True, "duration_min": 30},
    {"name": "Retorno", "is_active": True, "duration_min": 40},
]


@pytest.fixture(autouse=True)
def _calendars():
    FakeCalendar.reset()


async def calendar_for(professional_id):
    return FakeCalendar.get(str(professional_id) if professional_id else "tenant")


def _req(**values) -> StaffEditRequest:
    allow = values.pop("allow_overlap", False)
    return StaffEditRequest(fields=frozenset(values), allow_overlap=allow, **values)


async def _world(db, **kwargs):
    kwargs.setdefault("start_at", soon())
    kwargs.setdefault("google_event_id", "evt-1")
    world = await seed_world(db, **kwargs)
    await set_tenant(db, world.tenant.id, appointment_types=SERVICES)
    return world


async def _edit(db, world, request):
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        return await sae.apply_staff_edit(
            session, tenant, appointment, request, calendar_for=calendar_for
        )


async def _notices(db, appointment_id) -> list[ProfessionalEditNotice]:
    async with db() as session:
        return list(
            await session.scalars(
                select(ProfessionalEditNotice).where(
                    ProfessionalEditNotice.appointment_id == appointment_id
                )
            )
        )


def _labels(outcome) -> list[str]:
    return [change.label for change in outcome.changes]


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def test_a_new_time_moves_the_event_and_the_row_keeping_the_duration(db):
    world = await _world(db)
    new = world.start_at + timedelta(days=1, hours=1)

    outcome = await _edit(db, world, _req(start_at=new))

    assert _labels(outcome) == ["Data", "Horário"] and outcome.time_changed is True
    [(event_id, start, end, summary, description)] = FakeCalendar.get("tenant").updated
    assert (event_id, start, end, summary) == ("evt-1", new, new + timedelta(minutes=30), "Consulta - Maria")
    assert "Convênio: não informado" in description
    row = await reload_appointment(db, world.appointment.id)
    assert _utc(row.start_at) == new and row.status == AppointmentStatus.RESCHEDULED
    [notice] = await _notices(db, world.appointment.id)
    assert notice.changed_fields == ["data", "horário"]
    assert outcome.notice_id == str(notice.id)


async def test_a_new_service_takes_its_duration_and_mails_no_doctor(db):
    world = await _world(db)

    outcome = await _edit(db, world, _req(service="retorno"))

    assert [(c.label, c.before, c.after) for c in outcome.changes] == [
        ("Serviço", "Consulta", "Retorno")
    ]
    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Retorno"
    assert row.end_at - row.start_at == timedelta(minutes=40)
    assert outcome.notice_id is None and await _notices(db, world.appointment.id) == []


async def test_a_doctor_on_another_agenda_is_created_there_and_the_old_event_deleted(db):
    world = await _world(db, professional_name="Dra. Ana")
    old_pro = world.appointment.professional_id
    new_pro = await add_professional(db, world.tenant.id, "Dr. Bruno")

    outcome = await _edit(db, world, _req(professional_id=new_pro))

    assert [(c.label, c.before, c.after) for c in outcome.changes] == [
        ("Médico", "Dra. Ana", "Dr. Bruno")
    ]
    assert len(FakeCalendar.get(str(new_pro)).created) == 1
    assert FakeCalendar.get(str(old_pro)).cancelled == ["evt-1"]
    row = await reload_appointment(db, world.appointment.id)
    assert row.professional_id == new_pro and row.google_event_id == f"evt-new-{new_pro}"
    assert [n.changed_fields for n in await _notices(db, world.appointment.id)] == [["médico"]]


async def test_a_doctor_on_the_same_agenda_keeps_the_event(db):
    world = await _world(db, professional_name="Dra. Ana")
    old_pro = world.appointment.professional_id
    new_pro = await add_professional(db, world.tenant.id, "Dr. Bruno")
    FakeCalendar.instances[str(old_pro)] = FakeCalendar("shared")
    FakeCalendar.instances[str(new_pro)] = FakeCalendar("shared")

    await _edit(db, world, _req(professional_id=new_pro))

    assert len(FakeCalendar.get(str(old_pro)).updated) == 1
    assert FakeCalendar.get(str(new_pro)).created == []
    assert FakeCalendar.get(str(old_pro)).cancelled == []
    assert (await reload_appointment(db, world.appointment.id)).google_event_id == "evt-1"


async def test_a_taken_slot_is_409_unless_the_clinic_squeezes_it_in(db):
    world = await _world(db)
    FakeCalendar.get("tenant").free = False
    new = world.start_at + timedelta(hours=2)

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(start_at=new))
    assert (err.value.status_code, err.value.code) == (409, "slot_unavailable")
    assert FakeCalendar.get("tenant").updated == []
    assert _utc((await reload_appointment(db, world.appointment.id)).start_at) == world.start_at

    await _edit(db, world, _req(start_at=new, allow_overlap=True))
    assert _utc((await reload_appointment(db, world.appointment.id)).start_at) == new


async def test_google_refusing_is_502_and_nothing_changes(db):
    world = await _world(db)
    FakeCalendar.get("tenant").fail = CalendarUnavailableError("down")

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(start_at=world.start_at + timedelta(hours=2)))

    assert (err.value.status_code, err.value.code) == (502, "calendar_unavailable")
    row = await reload_appointment(db, world.appointment.id)
    assert _utc(row.start_at) == world.start_at and row.status == AppointmentStatus.SCHEDULED


async def test_a_database_failure_after_google_puts_google_back(db, monkeypatch):
    world = await _world(db)

    async def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(sae, "write_appointment_edit", _boom)

    with pytest.raises(RuntimeError):
        await _edit(db, world, _req(start_at=world.start_at + timedelta(hours=2)))

    moved, restored = FakeCalendar.get("tenant").updated
    assert restored[1] == world.start_at and restored[3] == "Consulta - Maria"
    assert _utc((await reload_appointment(db, world.appointment.id)).start_at) == world.start_at


async def test_a_row_cancelled_while_google_was_moving_is_409_and_google_is_put_back(db):
    world = await _world(db)

    async def _someone_cancels() -> None:
        await set_appointment(db, world.appointment.id, status=AppointmentStatus.CANCELLED)

    FakeCalendar.get("tenant").during = _someone_cancels

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(start_at=world.start_at + timedelta(hours=2)))

    assert (err.value.status_code, err.value.code) == (409, "not_live")
    assert len(FakeCalendar.get("tenant").updated) == 2  # moved, then restored


@pytest.mark.parametrize(
    ("request_values", "code"),
    [
        ({"insurance": None}, "nothing_changed"),
        ({"service": "Cirurgia"}, "service_not_offered"),
        ({"phone": "1234"}, "invalid_phone"),
    ],
)
async def test_validation_errors_are_422(db, request_values, code):
    world = await _world(db)

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(**request_values))

    assert (err.value.status_code, err.value.code) == (422, code)
    assert FakeCalendar.instances == {}  # Google never touched


async def test_the_same_time_is_nothing_changed_and_a_past_time_is_refused(db):
    world = await _world(db)
    with pytest.raises(StaffEditError) as same:
        await _edit(db, world, _req(start_at=world.start_at))
    assert same.value.code == "nothing_changed"

    with pytest.raises(StaffEditError) as past:
        await _edit(db, world, _req(start_at=datetime.now(UTC) - timedelta(hours=1)))
    assert (past.value.status_code, past.value.code) == (422, "start_in_past")


async def test_a_professional_of_another_clinic_or_inactive_is_unknown(db):
    world = await _world(db)
    other = await seed_world(db, phone_number_id="pnid-2", professional_name="Dr. Fora")
    retired = await add_professional(db, world.tenant.id, "Dr. Antigo", active=False)

    for professional_id in (other.appointment.professional_id, retired):
        with pytest.raises(StaffEditError) as err:
            await _edit(db, world, _req(professional_id=professional_id))
        assert (err.value.status_code, err.value.code) == (422, "unknown_professional")


async def test_only_the_contact_phone_changes_nothing_else(db):
    world = await _world(db)

    outcome = await _edit(db, world, _req(phone="+55 (11) 97777-6666"))

    assert [(c.label, c.before, c.after) for c in outcome.changes] == [
        ("Telefone de contato", "não informado", "5511977776666")
    ]
    assert FakeCalendar.instances == {}  # the event carries no phone: Google untouched
    assert outcome.notice_id is None
    row = await reload_appointment(db, world.appointment.id)
    assert row.phone == "5511977776666"
    async with db() as session:
        assert (await session.get(Patient, world.patient.id)).wa_id == WA_ID


async def test_convenio_and_who_it_is_for_change_and_can_be_cleared(db):
    world = await _world(db)

    outcome = await _edit(db, world, _req(insurance="Unimed", attendee_name="João"))

    assert [(c.label, c.before, c.after) for c in outcome.changes] == [
        ("Convênio", "não informado", "Unimed"),
        ("Paciente", "você", "João"),
    ]
    assert FakeCalendar.get("tenant").updated[0][3] == "Consulta - João"

    cleared = await _edit(db, world, _req(insurance="", attendee_name=None))
    assert [(c.label, c.after) for c in cleared.changes] == [
        ("Convênio", "não informado"),
        ("Paciente", "você"),
    ]
    row = await reload_appointment(db, world.appointment.id)
    assert row.insurance is None and row.attendee_name is None


async def test_a_closed_appointment_or_a_block_cannot_be_edited(db):
    closed = await _world(db, status=AppointmentStatus.CANCELLED)
    with pytest.raises(StaffEditError) as err:
        await _edit(db, closed, _req(insurance="Unimed"))
    assert (err.value.status_code, err.value.code) == (409, "not_live")

    block = await _world(db, phone_number_id="pnid-3")
    await set_appointment(db, block.appointment.id, patient_id=None, phone=None)
    with pytest.raises(StaffEditError) as err:
        await _edit(db, block, _req(insurance="Unimed"))
    assert (err.value.status_code, err.value.code) == (422, "not_a_patient_appointment")


def test_phone_normalisation():
    assert sae.normalize_phone("+55 (11) 98888-7777") == "5511988887777"
    assert sae.normalize_phone("  ") is None and sae.normalize_phone(None) is None
    for bad in ("11988887777", "1" * 16, "abc"):
        with pytest.raises(ValueError):
            sae.normalize_phone(bad)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pyt tests/test_staff_appointment_edit.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'secretaria.services.staff_appointment_edit'`.

- [ ] **Step 3: Implement the service**

Create `src/secretaria/services/staff_appointment_edit.py`:

```python
"""The clinic's "Editar/Remarcar" on one appointment (TASK-032 R7, spec 2026-10-09 §1/§3/§4).

One action changes any of: date/time, service (and with it the duration), doctor,
convênio, who the appointment is for (`attendee_name`) and the appointment's contact
phone. In order:

1. Resolve and validate every requested value against THIS clinic - a foreign or
   inactive professional, a service the doctor does not offer, a start in the past or a
   malformed phone is 422 - and refuse a request that changes nothing.
2. Google first, FAIL CLOSED: patch the event on its own agenda, or - when the doctor
   moves to another agenda - create it there. If Google refuses, nothing in the database
   changed (502). The slot must be free and inside the hours unless the clinic says
   `allow_overlap` (an "encaixe"). A contact-phone-only change never calls Google (the
   event carries no phone).
3. The row, locked, through the SAME writer the patient's "Alterar Dados" uses
   (services/appointment_edit_write.py), plus the contact phone; the doctor e-mail goes
   to R6's outbox only when date, time or doctor changed. A row that stopped being live -
   or was edited by someone else - meanwhile undoes step 2 (409).
4. After the commit, the old event of a doctor move is deleted from the old agenda (best
   effort, like R6's `_finish_edit`).

The hub route does the rest after this returns: the reminders replan, the doctor e-mail
enqueue and the patient notice. Never touches the Pix deposit (a clinic move does not
consume the patient's reschedule allowance - same rule as POST /reschedule) nor the
patient's identity (`Patient.wa_id`, e-mail, `Patient.name`). Logs carry ids and codes.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Patient, Professional, Tenant, is_live_status
from secretaria.services.appointment_edit import appointment_email_version
from secretaria.services.appointment_edit_write import write_appointment_edit
from secretaria.services.appointment_status import SOURCE_HUB
from secretaria.services.calendar import CalendarUnavailableError, build_event_description
from secretaria.services.patient_context import as_utc
from secretaria.services.professional_edit_outbox import record_professional_edit
from secretaria.services.service_catalog import load_service_catalog
from secretaria.services.service_catalog import normalize as normalize_service_name
from secretaria.services.tenant_config import (
    active_appointment_types,
    professional_appointment_types,
)

logger = get_logger(__name__)

EDITABLE_FIELDS: tuple[str, ...] = (
    "start_at",
    "service",
    "professional_id",
    "insurance",
    "attendee_name",
    "phone",
)
# Spec §3 / R6: the doctor is e-mailed when THEIR agenda changed.
DOCTOR_EMAIL_FIELDS = frozenset({"data", "horário", "médico"})

LABEL_DATE = "Data"
LABEL_TIME = "Horário"
LABEL_SERVICE = "Serviço"
LABEL_DOCTOR = "Médico"
LABEL_INSURANCE = "Convênio"
LABEL_ATTENDEE = "Paciente"
LABEL_PHONE = "Telefone de contato"
NOT_INFORMED = "não informado"
THE_PATIENT = "você"
NO_DOCTOR = "a clínica"
DEFAULT_SERVICE = "Consulta"
_NOT_DIGIT = re.compile(r"\D")

CalendarFor = Callable[[UUID | None], Awaitable[Any]]


class StaffEditError(Exception):
    """A refusal the hub maps to HTTP: `status_code`, machine `code`, Portuguese `message`."""

    def __init__(self, status_code: int, code: str, message: str, **extra) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra


@dataclass(frozen=True)
class StaffEditRequest:
    """`fields` names what the clinic SENT; a sent null/"" clears insurance/attendee/phone."""

    fields: frozenset[str]
    start_at: datetime | None = None
    service: str | None = None
    professional_id: UUID | None = None
    insurance: str | None = None
    attendee_name: str | None = None
    phone: str | None = None
    allow_overlap: bool = False


@dataclass(frozen=True)
class FieldChange:
    """One line of the patient notice: "<label>: <before> → <after>"."""

    label: str
    before: str
    after: str


@dataclass(frozen=True)
class StaffEditOutcome:
    appointment_id: UUID
    changes: tuple[FieldChange, ...]
    changed_fields: tuple[str, ...]  # R6 vocabulary, for the doctor e-mail
    time_changed: bool
    notice_id: str | None = None
    notice_version: str | None = None


@dataclass(frozen=True)
class _Target:
    start_at: datetime
    end_at: datetime
    service: str | None
    professional_id: UUID | None
    insurance: str | None
    attendee_name: str | None
    phone: str | None


@dataclass(frozen=True)
class _Before:
    start_at: datetime
    end_at: datetime
    service: str | None
    professional_id: UUID | None
    insurance: str | None
    attendee_name: str | None
    event_id: str


@dataclass(frozen=True)
class _Moved:
    event_id: str
    link: str | None
    calendar_changed: bool
    old_event_id: str
    old_calendar: Any
    new_calendar: Any


def normalize_phone(raw: str | None) -> str | None:
    """Digits only, with country code (12-15 digits); None for blank; ValueError otherwise."""
    if raw is None or not raw.strip():
        return None
    digits = _NOT_DIGIT.sub("", raw)
    if not 12 <= len(digits) <= 15:
        raise ValueError("a contact phone needs 12 to 15 digits, country code included")
    return digits


def _clean(value: str | None) -> str | None:
    text = " ".join(str(value or "").split())
    return text or None


def _clinic_tz(tenant: Tenant) -> ZoneInfo:
    try:
        return ZoneInfo(tenant.timezone or "America/Sao_Paulo")
    except Exception:
        return ZoneInfo("America/Sao_Paulo")


async def _professional(session: AsyncSession, tenant_id: UUID, professional_id) -> Professional | None:
    if professional_id is None:
        return None
    return await session.scalar(
        select(Professional).where(
            Professional.id == professional_id, Professional.tenant_id == tenant_id
        )
    )


async def _offered_service(
    session: AsyncSession, tenant: Tenant, professional_id: UUID | None, wanted: str | None
) -> dict | None:
    owner = await _professional(session, tenant.id, professional_id)
    catalog = await load_service_catalog(session, tenant.id)
    offered = (
        professional_appointment_types(owner, tenant, catalog or None)
        if owner is not None
        else active_appointment_types(tenant, catalog or None)
    )
    key = normalize_service_name(wanted)
    return next((s for s in offered if normalize_service_name(s.get("name")) == key), None)


async def _resolve_target(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    request: StaffEditRequest,
    now: datetime,
) -> _Target:
    fields = request.fields
    start = as_utc(appointment.start_at)
    end = (
        as_utc(appointment.end_at)
        if appointment.end_at is not None
        else start + timedelta(minutes=tenant.appointment_duration_min or 30)
    )
    duration = end - start
    professional_id = appointment.professional_id
    if "professional_id" in fields:
        professional = await _professional(session, tenant.id, request.professional_id)
        if professional is None or not professional.is_active:
            raise StaffEditError(
                422, "unknown_professional", "Este profissional não pertence a esta clínica."
            )
        professional_id = professional.id
    service = appointment.appointment_type
    if "service" in fields or professional_id != appointment.professional_id:
        wanted = _clean(request.service) if "service" in fields else appointment.appointment_type
        match = await _offered_service(session, tenant, professional_id, wanted)
        if match is None:
            raise StaffEditError(
                422, "service_not_offered", "Este serviço não é oferecido por este profissional."
            )
        service = str(match.get("name"))
        if normalize_service_name(service) != normalize_service_name(appointment.appointment_type):
            minutes = int(match.get("duration_min") or tenant.appointment_duration_min or 30)
            duration = timedelta(minutes=minutes)
    if "start_at" in fields:
        start = as_utc(request.start_at)
        if start <= now:
            raise StaffEditError(422, "start_in_past", "Escolha um horário no futuro.")
    phone = appointment.phone
    if "phone" in fields:
        try:
            phone = normalize_phone(request.phone)
        except ValueError:
            raise StaffEditError(
                422,
                "invalid_phone",
                "Informe o telefone com DDI e DDD, por exemplo 5511988887777.",
            ) from None
    return _Target(
        start_at=start,
        end_at=start + duration,
        service=service,
        professional_id=professional_id,
        insurance=_clean(request.insurance) if "insurance" in fields else appointment.insurance,
        attendee_name=(
            _clean(request.attendee_name)
            if "attendee_name" in fields
            else appointment.attendee_name
        ),
        phone=phone,
    )


async def _names(session: AsyncSession, tenant_id: UUID, ids) -> dict:
    wanted = [i for i in ids if i is not None]
    if not wanted:
        return {}
    rows = await session.execute(
        select(Professional.id, Professional.name).where(
            Professional.tenant_id == tenant_id, Professional.id.in_(wanted)
        )
    )
    return {row.id: row.name for row in rows}


def _changes(tz: ZoneInfo, before: _Before, target: _Target, names: dict, phone_before) -> list[FieldChange]:
    out: list[FieldChange] = []
    old = before.start_at.astimezone(tz)
    new = target.start_at.astimezone(tz)
    if old.date() != new.date():
        out.append(FieldChange(LABEL_DATE, f"{old:%d/%m/%Y}", f"{new:%d/%m/%Y}"))
    if (old.hour, old.minute) != (new.hour, new.minute):
        out.append(FieldChange(LABEL_TIME, f"{old:%H:%M}", f"{new:%H:%M}"))
    if normalize_service_name(before.service) != normalize_service_name(target.service):
        out.append(
            FieldChange(
                LABEL_SERVICE, before.service or DEFAULT_SERVICE, target.service or DEFAULT_SERVICE
            )
        )
    if before.professional_id != target.professional_id:
        out.append(
            FieldChange(
                LABEL_DOCTOR,
                names.get(before.professional_id) or NO_DOCTOR,
                names.get(target.professional_id) or NO_DOCTOR,
            )
        )
    if _clean(before.insurance) != _clean(target.insurance):
        out.append(
            FieldChange(
                LABEL_INSURANCE,
                _clean(before.insurance) or NOT_INFORMED,
                _clean(target.insurance) or NOT_INFORMED,
            )
        )
    if _clean(before.attendee_name) != _clean(target.attendee_name):
        out.append(
            FieldChange(
                LABEL_ATTENDEE,
                _clean(before.attendee_name) or THE_PATIENT,
                _clean(target.attendee_name) or THE_PATIENT,
            )
        )
    if _clean(phone_before) != _clean(target.phone):
        out.append(
            FieldChange(
                LABEL_PHONE, _clean(phone_before) or NOT_INFORMED, target.phone or NOT_INFORMED
            )
        )
    return out


def _summary(service: str | None, attendee: str | None, patient_name: str | None) -> str:
    return f"{service or DEFAULT_SERVICE} - {attendee or patient_name or 'Paciente'}"


async def _move_event(
    appointment_id: UUID,
    before: _Before,
    target: _Target,
    request: StaffEditRequest,
    calendar_for: CalendarFor,
    patient_name: str | None,
) -> _Moved:
    old_calendar = await calendar_for(before.professional_id)
    doctor_moved = target.professional_id != before.professional_id
    new_calendar = await calendar_for(target.professional_id) if doctor_moved else old_calendar
    calendar_changed = (
        doctor_moved
        and new_calendar is not old_calendar
        and not old_calendar.references_same_calendar(new_calendar)
    )
    window_changed = (target.start_at, target.end_at) != (before.start_at, before.end_at)
    unavailable = StaffEditError(
        502,
        "calendar_unavailable",
        "Não foi possível alterar o evento no Google Agenda. Nada foi alterado; "
        "tente de novo em instantes.",
    )
    if (window_changed or calendar_changed) and not request.allow_overlap:
        try:
            free = await new_calendar.is_slot_free(
                target.start_at,
                target.end_at,
                ignore_event_id=None if calendar_changed else before.event_id,
            )
        except CalendarUnavailableError as exc:
            raise unavailable from exc
        if not free:
            raise StaffEditError(409, "slot_unavailable", "Este horário não está livre na agenda.")
    summary = _summary(target.service, target.attendee_name, patient_name)
    description = build_event_description(
        service=target.service, insurance=target.insurance, attendee_name=target.attendee_name
    )
    try:
        if calendar_changed:
            created = await new_calendar.create_event(
                target.start_at, target.end_at, summary, description
            )
            return _Moved(
                event_id=str(created.get("id")),
                link=created.get("htmlLink"),
                calendar_changed=True,
                old_event_id=before.event_id,
                old_calendar=old_calendar,
                new_calendar=new_calendar,
            )
        await old_calendar.update_event_details(
            before.event_id, target.start_at, target.end_at, summary, description
        )
        return _Moved(
            event_id=before.event_id,
            link=None,
            calendar_changed=False,
            old_event_id=before.event_id,
            old_calendar=old_calendar,
            new_calendar=new_calendar,
        )
    except StaffEditError:
        raise
    except Exception as exc:
        logger.error(
            "hub_edit_calendar_failed",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )
        raise unavailable from exc


async def _undo_move(
    appointment_id: UUID, moved: _Moved | None, before: _Before, patient_name: str | None
) -> None:
    """Best effort: take Google back to the committed row after a failed write."""
    if moved is None:
        return
    try:
        if moved.calendar_changed:
            await moved.new_calendar.cancel_event(moved.event_id)
            return
        await moved.old_calendar.update_event_details(
            before.event_id,
            before.start_at,
            before.end_at,
            _summary(before.service, before.attendee_name, patient_name),
            build_event_description(
                service=before.service,
                insurance=before.insurance,
                attendee_name=before.attendee_name,
            ),
        )
    except Exception as exc:
        logger.error(
            "hub_edit_calendar_undo_failed",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )


async def apply_staff_edit(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    request: StaffEditRequest,
    *,
    calendar_for: CalendarFor,
    now: datetime | None = None,
) -> StaffEditOutcome:
    """Validate, move Google, write the row; commits. See the module docstring."""
    now = now or datetime.now(UTC)
    appointment_id, tenant_id = appointment.id, tenant.id
    if appointment.patient_id is None and not appointment.phone:
        raise StaffEditError(
            422, "not_a_patient_appointment", "Este horário não pertence a um paciente."
        )
    if not is_live_status(appointment.status) or appointment.start_at is None:
        raise StaffEditError(
            409,
            "not_live",
            "Esta consulta já foi cancelada ou encerrada.",
            status=appointment.status.value,
        )
    target = await _resolve_target(session, tenant, appointment, request, now)
    before = _Before(
        start_at=as_utc(appointment.start_at),
        end_at=as_utc(appointment.end_at or appointment.start_at),
        service=appointment.appointment_type,
        professional_id=appointment.professional_id,
        insurance=appointment.insurance,
        attendee_name=appointment.attendee_name,
        event_id=(appointment.google_event_id or "").strip(),
    )
    names = await _names(session, tenant_id, {before.professional_id, target.professional_id})
    changes = _changes(_clinic_tz(tenant), before, target, names, appointment.phone)
    if not changes:
        raise StaffEditError(422, "nothing_changed", "Nada foi alterado.")
    patient_name = None
    if appointment.patient_id is not None:
        patient_name = await session.scalar(
            select(Patient.name).where(
                Patient.id == appointment.patient_id, Patient.tenant_id == tenant_id
            )
        )
    time_changed = target.start_at != before.start_at
    needs_google = bool(before.event_id) and any(c.label != LABEL_PHONE for c in changes)
    moved = (
        await _move_event(appointment_id, before, target, request, calendar_for, patient_name)
        if needs_google
        else None
    )

    try:
        locked = await session.scalar(
            select(Appointment)
            .where(Appointment.id == appointment_id, Appointment.tenant_id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked is None or not is_live_status(locked.status):
            raise StaffEditError(
                409,
                "not_live",
                "Esta consulta já foi cancelada ou encerrada.",
                status=locked.status.value if locked is not None else None,
            )
        if (locked.google_event_id or "").strip() != before.event_id or as_utc(
            locked.start_at
        ) != before.start_at:
            raise StaffEditError(
                409,
                "appointment_changed",
                "A consulta foi alterada por outra pessoa. Recarregue e tente de novo.",
            )
        edit = {
            "appointment_type": target.service,
            "professional_id": target.professional_id,
            "insurance": target.insurance,
            "attendee_name": target.attendee_name,
            "google_event_id": moved.event_id if moved is not None else locked.google_event_id,
            "google_event_link": moved.link if moved is not None else locked.google_event_link,
            "start_at": target.start_at,
            "end_at": target.end_at,
            "time_changed": time_changed,
            "doctor_changed": target.professional_id != before.professional_id,
            "calendar_changed": bool(moved is not None and moved.calendar_changed),
        }
        fields = await write_appointment_edit(
            session,
            locked,
            tenant_id=tenant_id,
            timezone=tenant.timezone,
            edit=edit,
            source=SOURCE_HUB,
            idempotency_key=f"hubedit:{appointment_id}:{target.start_at.isoformat()}",
        )
        locked.phone = target.phone
        locked.updated_at = now
        notice = None
        if DOCTOR_EMAIL_FIELDS.intersection(fields):
            notice = await record_professional_edit(session, locked, fields)
        notice_id = str(notice.id) if notice is not None else None
        notice_version = appointment_email_version(locked) if notice is not None else None
        await session.commit()
    except Exception:
        await session.rollback()
        await _undo_move(appointment_id, moved, before, patient_name)
        raise

    if moved is not None and moved.calendar_changed:
        try:
            await moved.old_calendar.cancel_event(moved.old_event_id)
        except Exception as exc:
            logger.warning(
                "hub_edit_old_event_delete_failed",
                appointment_id=str(appointment_id),
                error_type=type(exc).__name__,
            )
    logger.info(
        "hub_appointment_edited",
        appointment_id=str(appointment_id),
        changed=len(changes),
        time_changed=time_changed,
        doctor_email=notice_id is not None,
    )
    return StaffEditOutcome(
        appointment_id=appointment_id,
        changes=tuple(changes),
        changed_fields=tuple(fields),
        time_changed=time_changed,
        notice_id=notice_id,
        notice_version=notice_version,
    )
```

- [ ] **Step 4: Run the tests**

Run: `pyt tests/test_staff_appointment_edit.py tests/test_appointment_edit_write.py -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format src/secretaria/services/staff_appointment_edit.py tests/test_staff_appointment_edit.py
uvx ruff check --fix src/secretaria/services/staff_appointment_edit.py tests/test_staff_appointment_edit.py
git diff --stat
git add src/secretaria/services/staff_appointment_edit.py tests/test_staff_appointment_edit.py
git commit -m "feat(hub): clinic edit service - validate, move Google fail-closed, write the row (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 11: `POST …/edit` (Editar/Remarcar) and the change card

**Files:**
- Modify: `src/secretaria/schemas/calendar.py` (new `AppointmentEdit`)
- Modify: `src/secretaria/services/clinic_action_notice.py` (append `edit_text`, `notify_staff_edit`)
- Modify: `src/secretaria/api/hub/calendar.py` (generalise `_owning_calendar`; new route; imports; module docstring)
- Test: `tests/test_hub_edit_appointment.py`

**Interfaces:**
- Consumes: `apply_staff_edit`, `StaffEditRequest`, `StaffEditError`, `FieldChange`, `EDITABLE_FIELDS` (Task 10); `_card_notice`, `_subject`, `_doctor` (Task 7); `REMINDER_KIND_STAFF_EDIT` (Task 5); `enqueue_professional_edit_notification`, `AppliedEdit` (R6); `reminder_hooks.after_appointment_rescheduled`; `_patient_of`, `_notice_link` (Tasks 4, 7).
- Produces:
  - `schemas.calendar.AppointmentEdit` (fields in "Produces for R5").
  - `clinic_action_notice.EDIT_TITLE = "A clínica alterou {subject}:"`, `EDIT_NOW = "Agora: {day} às {time} com {doctor}."`, `edit_text(changes: Sequence[FieldChange], content: ReminderContent) -> str`, `async notify_staff_edit(session, tenant, appointment, patient, *, changes: Sequence[FieldChange], allow_paid: bool, now: datetime) -> NoticeResult`.
  - `api/hub/calendar.py::_calendar_for_professional(session, tenant, professional_id: UUID | None)` (`_owning_calendar` now delegates to it); route `POST /tenants/me/calendar/appointments/{appointment_id}/edit` → `AppointmentActionRead`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hub_edit_appointment.py`:

```python
"""POST /appointments/{id}/edit - Editar/Remarcar tells the patient (TASK-032 R7 §1/§2/§3)."""

# ruff: noqa: F811

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from secretaria.models import AppointmentStatus, FlowState
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.flow_router import STEP_EDIT_MENU
from secretaria.workers.shared import reminder_actions as ra
from secretaria.workers.shared.greeting import _format_appointment_when
from tests._edit_flow_support import draft_for
from tests._r7_support import (  # noqa: F401
    CALENDAR,
    FakeArqPool,
    FakeCalendar,
    acting,
    install_pool,
    patch_status,
    r7,
    recent,
    sent,
    set_tenant,
    setup_world,
    staff_rows,
    tap,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import reminder_rows, set_conversation
from tests._reminders_v2 import WA_ID, outbound_messages, reload_appointment, seed_world
from tests.test_appointment_edit_apply import _apply, _edit

SP = ZoneInfo("America/Sao_Paulo")
SERVICES = [
    {"name": "Consulta", "is_active": True, "duration_min": 30},
    {"name": "Retorno", "is_active": True, "duration_min": 40},
]


async def _world(db, acting, **kwargs):
    kwargs.setdefault("google_event_id", "evt-1")
    world = await setup_world(db, acting, **kwargs)
    await set_tenant(db, world.tenant.id, appointment_types=SERVICES)
    return world


async def _post(client: AsyncClient, appointment_id, **body):
    return await client.post(f"{CALENDAR}/appointments/{appointment_id}/edit", json=body)


def _iso(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


async def test_a_new_time_moves_everything_and_sends_the_change_card(
    client: AsyncClient, db, acting
):
    pool = FakeArqPool()
    install_pool(pool)
    world = await _world(db, acting, confirmation_count=1, status=AppointmentStatus.CONFIRMED)
    new = world.start_at + timedelta(days=1, hours=1)

    response = await _post(client, world.appointment.id, start_at=_iso(new))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "rescheduled" and body["confirmation_count"] == 0
    assert body["patient_notice"] == "whatsapp_sent" and body["whatsapp_link"] is None
    old, now_ = world.start_at.astimezone(SP), new.astimezone(SP)
    expected = (
        "A clínica alterou sua consulta:\n"
        f"• Data: {old:%d/%m/%Y} → {now_:%d/%m/%Y}\n"
        f"• Horário: {old:%H:%M} → {now_:%H:%M}\n"
        "\n"
        f"Agora: {now_:%d/%m/%Y} às {now_:%H:%M} com a equipe da Clínica Olhar."
    )
    [row] = await staff_rows(db, world.appointment.id, "staff_edit")
    assert sent() == [
        (
            "buttons",
            WA_ID,
            expected,
            [
                (f"remconfirm|{row.id}", "Confirmar"),
                (f"remcancel|{row.id}", "Cancelar"),
                (f"remedit|{row.id}", "Alterar Dados"),
            ],
        )
    ]
    # the reminders follow the new time (R2 hook), the doctor is e-mailed (R6 outbox)
    pending = [r for r in await reminder_rows(db, world.appointment.id) if r.status == "pending"]
    assert pending and all(r.appointment_start_at.replace(tzinfo=None) == new.replace(tzinfo=None) for r in pending)
    assert [call[0] for call in pool.calls] == ["send_professional_edit_notification"]


async def test_confirmar_on_the_change_card_counts_and_an_older_card_says_horario_antigo(
    client: AsyncClient, db, acting
):
    world = await _world(db, acting, professional_name="Dra. Ana")
    await patch_status(client, world.appointment.id, "confirmed")
    [old_card] = await staff_rows(db, world.appointment.id, "staff_confirm")
    new = world.start_at + timedelta(hours=2)
    await _post(client, world.appointment.id, start_at=_iso(new))
    [edit_card] = await staff_rows(db, world.appointment.id, "staff_edit")

    await tap(world, "remconfirm", old_card.id)
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 0
    when = _format_appointment_when(new, "America/Sao_Paulo")
    assert sent()[-1] == ("text", WA_ID, ra.MOVED_TEXT.format(when=when))

    await tap(world, "remconfirm", edit_card.id)
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 1


async def test_a_convenio_only_change_keeps_the_status_and_mails_no_doctor(
    client: AsyncClient, db, acting
):
    pool = FakeArqPool()
    install_pool(pool)
    world = await _world(db, acting)

    response = await _post(client, world.appointment.id, insurance="Unimed")

    assert response.json()["status"] == "scheduled"
    assert "• Convênio: não informado → Unimed" in sent()[0][2]
    assert pool.calls == []


async def test_a_contact_phone_change_goes_to_the_patients_own_number(
    client: AsyncClient, db, acting
):
    world = await _world(db, acting)

    response = await _post(client, world.appointment.id, phone="+55 11 97777-6666")

    assert response.status_code == 200 and response.json()["phone"] == "5511977776666"
    assert sent()[0][1] == WA_ID  # identity, never the new contact
    assert "• Telefone de contato: não informado → 5511977776666" in sent()[0][2]
    assert FakeCalendar.instances == {}


async def test_a_portal_patient_gets_the_card_in_the_chat_and_an_email(
    client: AsyncClient, db, acting, r7
):
    world = await _world(db, acting, channel=CHANNEL_BRAIN_MESSAGE, wa_id=None, email="m@x.com")

    response = await _post(client, world.appointment.id, attendee_name="João Pedro")

    assert response.json()["patient_notice"] == "portal_chat_email"
    [message] = await outbound_messages(db, world.conversation.id)
    assert message.interactive["body"].startswith("A clínica alterou a consulta de João Pedro:")
    assert sent() == [] and len(r7["mail"]) == 1


async def test_outside_the_window_the_clinics_standing_yes_sends_the_paid_text(
    client: AsyncClient, db, acting
):
    world = await _world(db, acting, last_inbound_at=recent(30))
    await set_tenant(db, world.tenant.id, paid_notices_auto_approved=True)

    response = await _post(client, world.appointment.id, service="Retorno")

    assert response.json()["patient_notice"] == "whatsapp_sent"
    assert sent()[0][0] == "template"


async def test_without_authorisation_outside_the_window_the_edit_stands_and_says_so(
    client: AsyncClient, db, acting
):
    world = await _world(db, acting, last_inbound_at=recent(30))

    response = await _post(client, world.appointment.id, service="Retorno")

    body = response.json()
    assert response.status_code == 200 and body["appointment_type"] == "Retorno"
    assert body["patient_notice"] == "whatsapp_outside_window"
    assert body["whatsapp_link"] == f"https://wa.me/{WA_ID}"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"notify_outside_window": True},
        {"insurance": "Unimed", "surprise": 1},
        {"start_at": "2030-01-01T10:00:00"},
        {"service": None},
        {"professional_id": "not-a-uuid"},
    ],
)
async def test_malformed_requests_are_422_and_touch_nothing(
    client: AsyncClient, db, acting, payload
):
    world = await _world(db, acting)

    response = await client.post(f"{CALENDAR}/appointments/{world.appointment.id}/edit", json=payload)

    assert response.status_code == 422
    assert sent() == [] and FakeCalendar.instances == {}


async def test_business_refusals_carry_their_code(client: AsyncClient, db, acting):
    world = await _world(db, acting)
    FakeCalendar.get("tenant").free = False

    taken = await _post(client, world.appointment.id, start_at=_iso(world.start_at + timedelta(hours=3)))
    same = await _post(client, world.appointment.id, insurance=None)

    assert taken.status_code == 409 and taken.json()["detail"]["code"] == "slot_unavailable"
    assert same.status_code == 422 and same.json()["detail"]["code"] == "nothing_changed"
    assert sent() == []


async def test_another_clinics_appointment_is_404_and_google_is_untouched(
    client: AsyncClient, db, acting
):
    world = await _world(db, acting)
    foreign = await seed_world(db, phone_number_id="pnid-9", start_at=world.start_at)

    response = await _post(client, foreign.appointment.id, insurance="Unimed")

    assert response.status_code == 404
    assert FakeCalendar.instances == {} and sent() == []


async def test_a_stale_alterar_dados_draft_cannot_overwrite_the_clinics_edit(
    client: AsyncClient, db, acting
):
    world = await _world(db, acting)
    draft = draft_for(
        {
            "id": str(world.appointment.id),
            "google_event_id": "evt-1",
            "appointment_type": "Consulta",
            "professional_id": None,
            "start_at": world.appointment.start_at,
            "end_at": world.appointment.end_at,
            "insurance": world.appointment.insurance,
            "attendee_name": None,
        }
    )
    await set_conversation(
        db,
        world,
        flow_state=FlowState.EDIT_BOOKING,
        flow_step=STEP_EDIT_MENU,
        flow_managing_appointment_id=world.appointment.id,
        flow_edit_draft=draft.to_json(),
    )
    new = world.start_at + timedelta(hours=2)
    await _post(client, world.appointment.id, start_at=_iso(new))

    await _apply(world, _edit(world, insurance="Amil", original=dict(draft.original)))

    row = await reload_appointment(db, world.appointment.id)
    assert row.insurance != "Amil"
    assert row.start_at.replace(tzinfo=None) == new.replace(tzinfo=None)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pyt tests/test_hub_edit_appointment.py -q`
Expected: FAIL — 404/405 on `POST …/edit` (the route does not exist).

- [ ] **Step 3: The request schema**

In `src/secretaria/schemas/calendar.py`: change `from pydantic import BaseModel, Field, field_validator` to `from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator`, add `from uuid import UUID` to the stdlib imports, and right after `class AppointmentReschedule` add:

```python
_EDITABLE = ("start_at", "service", "professional_id", "insurance", "attendee_name", "phone")
_NOT_CLEARABLE = ("start_at", "service", "professional_id")


class AppointmentEdit(BaseModel):
    """POST /appointments/{id}/edit - "Editar/Remarcar" (TASK-032 R7, spec 2026-10-09 §1/§3).

    Every field is optional; at least one of the six editable ones must be SENT. A sent
    null / "" clears `insurance`, `attendee_name` and `phone`; the other three cannot be
    cleared. `phone` is the appointment's contact phone, never the patient's identity.
    """

    model_config = ConfigDict(extra="forbid")

    start_at: AwareDatetime | None = None
    service: str | None = Field(default=None, min_length=1, max_length=120)
    professional_id: UUID | None = None
    insurance: str | None = Field(default=None, max_length=120)
    attendee_name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=32)
    # Same meaning as AppointmentCancel.notify_outside_window (OR-ed with the clinic's
    # `paid_notices_auto_approved`).
    notify_outside_window: bool = False
    # True = book over a busy slot or outside the hours (an "encaixe").
    allow_overlap: bool = False

    @model_validator(mode="after")
    def _something_to_change(self) -> "AppointmentEdit":
        sent = self.model_fields_set
        if not sent.intersection(_EDITABLE):
            raise ValueError("send at least one field to change")
        for name in _NOT_CLEARABLE:
            if name in sent and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self
```

- [ ] **Step 4: The change card**

Append to `src/secretaria/services/clinic_action_notice.py` (add `from collections.abc import Sequence`, `from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS, truncate_plain`, `REMINDER_KIND_STAFF_EDIT` to the appointment_reminder import, and `from secretaria.services.staff_appointment_edit import FieldChange`):

```python
# --- "Editar/Remarcar" -> what changed + the same three buttons (spec §2) -----------

EDIT_TITLE = "A clínica alterou {subject}:"
EDIT_NOW = "Agora: {day} às {time} com {doctor}."


def edit_text(changes: Sequence[FieldChange], content: ReminderContent) -> str:
    """Before -> after of each field the clinic changed, then the appointment as it is now."""
    local = local_start(content)
    lines = [EDIT_TITLE.format(subject=_subject(content))]
    lines += [f"• {change.label}: {change.before} → {change.after}" for change in changes]
    lines += [
        "",
        EDIT_NOW.format(day=f"{local:%d/%m/%Y}", time=f"{local:%H:%M}", doctor=_doctor(content)),
    ]
    return truncate_plain("\n".join(lines), MAX_INTERACTIVE_BODY_CHARS)


async def notify_staff_edit(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient | None,
    *,
    changes: Sequence[FieldChange],
    allow_paid: bool,
    now: datetime,
) -> NoticeResult:
    """Every edit is told; the card row of this start is re-used across edits."""
    content = await load_reminder_content(session, tenant, appointment)
    result = await _card_notice(
        session,
        tenant,
        appointment,
        patient,
        kind=REMINDER_KIND_STAFF_EDIT,
        body=edit_text(changes, content),
        allow_paid=allow_paid,
        now=now,
        once_per_start=False,
    )
    # once_per_start=False never skips; the fallback only keeps the return type honest.
    return result if result is not None else NoticeResult(NOTICE_FAILED)
```

(also import `NOTICE_FAILED` from `secretaria.services.staff_patient_message` next to `NoticeResult`.)

- [ ] **Step 5: The route**

In `src/secretaria/api/hub/calendar.py`:

(a) module docstring: add the line `POST  /tenants/me/calendar/appointments/{id}/edit       - Editar/Remarcar + notify (R7).`

(b) imports: add `AppointmentEdit,` to the `schemas.calendar` import block; add `staff_appointment_edit,` to the `from secretaria.services import (...)` block; add at the end of the import section:

```python
from secretaria.workers.shared.appointment_edit_apply import AppliedEdit
from secretaria.workers.shared.appointment_edit_notification import (
    enqueue_professional_edit_notification,
)
```

(c) replace the whole `_owning_calendar` function with:

```python
async def _calendar_for_professional(
    session: AsyncSession, tenant: Tenant, professional_id: UUID | None
):
    """The Google calendar of `professional_id` (the clinic's own when None).

    A booking made with a professional lives on THAT professional's calendar;
    `cancel_event` treats a 404 as "already gone" (success), so writing to the wrong
    calendar would silently no-op while the slot stays occupied. When the professional
    cannot be resolved this refuses (409) instead of guessing the tenant calendar -
    same "don't guess, degrade" rule as workers/shared/actions.py::_calendar_for_appointment.
    """
    if professional_id is None:
        return await _get_calendar(session, tenant)
    professional = await session.scalar(
        select(Professional).where(
            Professional.id == professional_id, Professional.tenant_id == tenant.id
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
            "hub_professional_calendar_failed",
            professional_id=str(professional_id),
            error_type=type(exc).__name__,
        )
        raise unresolved from exc


async def _owning_calendar(session: AsyncSession, tenant: Tenant, appt: Appointment):
    """The Google calendar that owns `appt`'s event (see `_calendar_for_professional`)."""
    return await _calendar_for_professional(session, tenant, appt.professional_id)
```

(d) right before the `# POST /appointments/{id}/reschedule` section header, add:

```python
# ---------------------------------------------------------------------------
# POST /appointments/{id}/edit — Editar/Remarcar (TASK-032 R7)
# ---------------------------------------------------------------------------


@router.post("/appointments/{appointment_id}/edit", response_model=AppointmentActionRead)
async def edit_appointment(
    appointment_id: str,
    body: AppointmentEdit,
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> AppointmentActionRead:
    """The clinic changes date/time, service, doctor, convênio, who it is for or the
    contact phone in one action, and the patient is told (spec 2026-10-09 §1/§2/§3).

    The edit itself (validation, Google fail-closed, the row, the doctor e-mail outbox)
    is services/staff_appointment_edit.py; here: the reminders follow a new time (R2,
    same rule as /reschedule), the doctor e-mail is enqueued (the outbox cron recovers
    a lost enqueue) and the patient gets "A clínica alterou ..." with the three buttons.
    """
    appt = await _get_appointment(session, tenant, appointment_id)
    staff_request = staff_appointment_edit.StaffEditRequest(
        fields=frozenset(body.model_fields_set.intersection(staff_appointment_edit.EDITABLE_FIELDS)),
        start_at=body.start_at,
        service=body.service,
        professional_id=body.professional_id,
        insurance=body.insurance,
        attendee_name=body.attendee_name,
        phone=body.phone,
        allow_overlap=body.allow_overlap,
    )

    async def _calendar(professional_id):
        return await _calendar_for_professional(session, tenant, professional_id)

    try:
        outcome = await staff_appointment_edit.apply_staff_edit(
            session, tenant, appt, staff_request, calendar_for=_calendar
        )
    except staff_appointment_edit.StaffEditError as exc:
        raise HTTPException(exc.status_code, _detail(exc.code, exc.message, **exc.extra)) from None

    await session.refresh(appt)
    if outcome.time_changed and (
        reminder_hooks.enabled_for(tenant) or (appt.confirmation_count or 0) > 0
    ):
        await reminder_hooks.after_appointment_rescheduled(appt.id)
        await session.refresh(appt)
    if outcome.notice_id is not None:
        await enqueue_professional_edit_notification(
            getattr(request.app.state, "arq_pool", None),
            tenant.id,
            AppliedEdit(
                appointment_id=appt.id,
                moved=outcome.time_changed,
                old_event_id=None,
                old_professional_id=None,
                notice_id=outcome.notice_id,
                notice_version=outcome.notice_version,
                changed_fields=outcome.changed_fields,
            ),
        )
    patient = await _patient_of(session, tenant, appt)
    notice = await clinic_action_notice.notify_staff_edit(
        session,
        tenant,
        appt,
        patient,
        changes=outcome.changes,
        allow_paid=staff_patient_message.paid_notice_authorised(
            tenant, body.notify_outside_window
        ),
        now=datetime.now(UTC),
    )
    await session.refresh(appt)
    logger.info(
        "calendar_appointment_edited",
        appointment_id=str(appt.id),
        changed=len(outcome.changes),
        patient_notice=notice.code,
    )
    deposit_status = await _deposit_status_value(session, appt.id)
    return AppointmentActionRead(
        **_appointment_read(appt, deposit_status=deposit_status).model_dump(),
        patient_notice=notice.code,
        whatsapp_link=notice.whatsapp_link,
    )
```

- [ ] **Step 6: Run the tests**

Run: `pyt tests/test_hub_edit_appointment.py tests/test_staff_appointment_edit.py tests/test_hub_release.py tests/test_hub_staff_confirm_notice.py tests/test_reminder_v2_hub_wiring.py tests/test_hub_calendar_money.py -q`
Expected: PASS.

- [ ] **Step 7: Lint and commit**

```bash
uvx ruff format tests/test_hub_edit_appointment.py && uvx ruff check --fix tests/test_hub_edit_appointment.py
uvx ruff format src/secretaria/services/clinic_action_notice.py && uvx ruff check --fix src/secretaria/services/clinic_action_notice.py
uvx ruff check src/secretaria/schemas/calendar.py src/secretaria/api/hub/calendar.py
git diff --stat
git add src/secretaria/schemas/calendar.py src/secretaria/services/clinic_action_notice.py src/secretaria/api/hub/calendar.py tests/test_hub_edit_appointment.py
git commit -m "feat(hub): POST edit - Editar/Remarcar tells the patient what changed (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 12: Cancel parity — a Portal patient is told in the chat

**Files:**
- Modify: `src/secretaria/services/appointment_release.py` (new `notify_cancelled_patient`; `_notify_portal` accepts a missing reason)
- Modify: `src/secretaria/api/hub/calendar.py` (`cancel_appointment`)
- Test: `tests/test_hub_cancel_portal_notice.py`

**Interfaces:**
- Consumes: `_notify_portal`, `NOTICE_*` (R4/Task 3), `_patient_of`, `_notice_link`, `paid_notice_authorised`.
- Produces: `async appointment_release.notify_cancelled_patient(session, tenant, appointment, patient, *, professional_name: str | None, justification: str | None, deposit_notice: str | None, allow_paid: bool, arq_pool, now: datetime | None = None) -> str` (one `NOTICE_*`, never raises). `POST …/cancel` returns `AppointmentActionRead`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hub_cancel_portal_notice.py`:

```python
"""POST /appointments/{id}/cancel: Portal patients are told too (TASK-032 R7 §2)."""

# ruff: noqa: F811

from httpx import AsyncClient

from secretaria.models import AppointmentStatus
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from tests._r7_support import (  # noqa: F401
    CALENDAR,
    FakeArqPool,
    acting,
    install_pool,
    r7,
    recent,
    set_appointment,
    set_tenant,
    setup_world,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import WA_ID, outbound_messages, reload_appointment

PORTAL_TEXT = (
    "O médico Dra. Ana desmarcou a sua consulta!\n\n"
    'Justificativa do médico: "Imprevisto"\n\n'
    "Para marcar um novo horário, é só me escrever por aqui."
)


async def _cancel(client: AsyncClient, appointment_id, **body):
    return await client.post(
        f"{CALENDAR}/appointments/{appointment_id}/cancel", json={"confirm": True, **body}
    )


async def _portal(db, acting, **kwargs):
    return await setup_world(
        db,
        acting,
        channel=CHANNEL_BRAIN_MESSAGE,
        wa_id=None,
        professional_name="Dra. Ana",
        **kwargs,
    )


async def test_a_portal_patient_gets_the_cancellation_in_the_chat_and_an_email(
    client: AsyncClient, db, acting, r7
):
    pool = FakeArqPool()
    install_pool(pool)
    world = await _portal(db, acting, email="maria@exemplo.com")

    response = await _cancel(client, world.appointment.id, justification="Imprevisto")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "cancelled" and body["patient_notice"] == "portal_chat_email"
    assert body["whatsapp_link"] is None
    [message] = await outbound_messages(db, world.conversation.id)
    assert message.body == PORTAL_TEXT
    assert [m[1] for m in r7["mail"]] == ["clinic_message_patient"]
    assert pool.calls == []  # the WhatsApp job never sees a Portal patient


async def test_a_portal_patient_with_a_contact_phone_is_still_told_in_the_chat(
    client: AsyncClient, db, acting
):
    pool = FakeArqPool()
    install_pool(pool)
    world = await _portal(db, acting, email=None)
    await set_appointment(db, world.appointment.id, phone="5511977776666")

    response = await _cancel(client, world.appointment.id)

    assert response.json()["patient_notice"] == "portal_chat"
    assert pool.calls == []


async def test_a_whatsapp_patient_keeps_todays_job_exactly(client: AsyncClient, db, acting):
    pool = FakeArqPool()
    install_pool(pool)
    world = await setup_world(db, acting, professional_name="Dra. Ana")
    await set_appointment(db, world.appointment.id, phone=WA_ID)

    response = await _cancel(client, world.appointment.id, justification="Imprevisto")

    assert response.json()["patient_notice"] == "whatsapp_queued"
    assert pool.calls == [
        (
            "send_cancellation_notice",
            str(world.tenant.id),
            str(world.appointment.id),
            "Dra. Ana",
            "Imprevisto",
            None,
            False,
        )
    ]


async def test_outside_the_window_the_job_is_still_queued_and_the_answer_says_so(
    client: AsyncClient, db, acting
):
    pool = FakeArqPool()
    install_pool(pool)
    world = await setup_world(db, acting, last_inbound_at=recent(30))
    await set_appointment(db, world.appointment.id, phone=WA_ID)

    body = (await _cancel(client, world.appointment.id)).json()

    assert body["patient_notice"] == "whatsapp_outside_window"
    assert body["whatsapp_link"] == f"https://wa.me/{WA_ID}"
    assert pool.calls[0][-1] is False  # the job itself sends nothing without authorisation


async def test_the_clinics_standing_yes_authorises_the_paid_cancellation(
    client: AsyncClient, db, acting
):
    pool = FakeArqPool()
    install_pool(pool)
    world = await setup_world(db, acting, last_inbound_at=recent(30))
    await set_appointment(db, world.appointment.id, phone=WA_ID)
    await set_tenant(db, world.tenant.id, paid_notices_auto_approved=True)

    body = (await _cancel(client, world.appointment.id)).json()

    assert body["patient_notice"] == "whatsapp_queued" and pool.calls[0][-1] is True


async def test_a_dead_queue_or_no_number_never_undoes_the_cancellation(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting)
    await set_appointment(db, world.appointment.id, phone=WA_ID)
    no_queue = await _cancel(client, world.appointment.id)
    assert no_queue.json()["patient_notice"] == "queue_unavailable"

    other = await setup_world(db, acting, phone_number_id="pnid-2", wa_id="5511900000002")
    install_pool(FakeArqPool())
    no_number = await _cancel(client, other.appointment.id)  # appointment.phone is empty
    assert no_number.json()["patient_notice"] == "no_channel"
    for appointment_id in (world.appointment.id, other.appointment.id):
        assert (await reload_appointment(db, appointment_id)).status == AppointmentStatus.CANCELLED
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pyt tests/test_hub_cancel_portal_notice.py -q`
Expected: FAIL — `KeyError: 'patient_notice'` and no Portal message.

- [ ] **Step 3: Implement the notice**

In `src/secretaria/services/appointment_release.py`, change the `_notify_portal` parameter annotation `reason: str,` to `reason: str | None,` (the text builder already drops a blank reason), and append:

```python
async def notify_cancelled_patient(
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
    """The patient notice of POST /cancel (TASK-032 R7 §2). Never raises.

    * Portal patient: the cancellation text in the chat + the generic e-mail nudge (new:
      until R7 a Portal patient learned nothing).
    * Everyone else: TODAY's enqueue, unchanged - same job, same arguments, same
      `appointment.phone` condition. The job still decides the 24 h window itself; this
      only REPORTS what it will do (`whatsapp_queued` inside the window or with
      `allow_paid`, `whatsapp_outside_window` otherwise).
    """
    appointment_id = appointment.id
    try:
        own = patient is not None and patient.tenant_id == tenant.id
        if own and patient.channel == CHANNEL_BRAIN_MESSAGE:
            return await _notify_portal(
                session,
                tenant,
                appointment,
                patient,
                professional_name,
                justification,
                deposit_notice,
            )
        if not appointment.phone:
            return NOTICE_NO_CHANNEL
        inside = False
        if own:
            last_inbound = await cancellation_notice.last_inbound_at(session, tenant.id, patient.id)
            inside = cancellation_notice.is_inside_window(last_inbound, now=now)
        if arq_pool is None:
            logger.warning("cancel_notice_not_queued", reason="no_queue")
            return NOTICE_QUEUE_UNAVAILABLE
        try:
            await arq_pool.enqueue_job(
                "send_cancellation_notice",
                str(tenant.id),
                str(appointment_id),
                professional_name,
                justification,
                deposit_notice,
                allow_paid,
            )
        except Exception as exc:
            logger.error(
                "cancel_notice_enqueue_failed",
                appointment_id=str(appointment_id),
                error_type=type(exc).__name__,
            )
            return NOTICE_QUEUE_UNAVAILABLE
        return NOTICE_WHATSAPP_QUEUED if inside or allow_paid else NOTICE_WHATSAPP_OUTSIDE_WINDOW
    except Exception as exc:
        logger.error(
            "cancel_notice_failed",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )
        return NOTICE_FAILED
```

- [ ] **Step 4: Wire it into the cancel route**

In `src/secretaria/api/hub/calendar.py::cancel_appointment`: change the decorator to `@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentActionRead)` and the return annotation to `-> AppointmentActionRead`. Replace the block from the comment `    # Notify the patient. UNCONDITIONAL now — ...` through the end of the function with:

```python
    # Notify the patient (unconditional since the cancellation-notice round; R7 adds
    # the Portal). The WhatsApp side is today's job, byte for byte; the deposit notice
    # rides along; the doctor's justification is quoted, not the whole message.
    patient = await _patient_of(session, tenant, appt)
    patient_notice = await appointment_release.notify_cancelled_patient(
        session,
        tenant,
        appt,
        patient,
        professional_name=professional_name,
        justification=body.justification,
        deposit_notice=notice,
        allow_paid=staff_patient_message.paid_notice_authorised(
            tenant, body.notify_outside_window
        ),
        arq_pool=getattr(request.app.state, "arq_pool", None),
    )
    await session.refresh(appt)

    logger.info(
        "calendar_appointment_cancelled",
        appointment_id=str(appt.id),
        deposit_outcome=deposit_outcome,
        patient_notice=patient_notice,
    )
    deposit_status = await _deposit_status_value(session, appt.id)
    read = _appointment_read(appt, deposit_status=deposit_status, deposit_outcome=deposit_outcome)
    return AppointmentActionRead(
        **read.model_dump(),
        patient_notice=patient_notice,
        whatsapp_link=_notice_link(patient_notice, patient, appt),
    )
```

- [ ] **Step 5: Run the tests (old cancel suites included)**

Run: `pyt tests/test_hub_cancel_portal_notice.py tests/test_cancellation_notice.py tests/test_hub_calendar_money.py tests/test_reminder_v2_hub_wiring.py tests/test_hub_release.py -q`
Expected: PASS (the pre-R7 cancel tests keep their enqueue assertions unchanged).

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff format tests/test_hub_cancel_portal_notice.py && uvx ruff check --fix tests/test_hub_cancel_portal_notice.py
uvx ruff check src/secretaria/services/appointment_release.py src/secretaria/api/hub/calendar.py
git diff --stat
git add src/secretaria/services/appointment_release.py src/secretaria/api/hub/calendar.py tests/test_hub_cancel_portal_notice.py
git commit -m "feat(hub): cancel tells Portal patients in the chat; patient_notice on cancel (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 13: Who is looking at the agenda (`AgendaViewer`, `GET /viewer`)

**Files:**
- Modify: `src/secretaria/core/subscription.py` (`AGENDA_SCOPE_*`, `SubscriptionClaim`, `_optional_uuid`, the claim built in `verify_subscription_token`, module docstring contract line)
- Create: `src/secretaria/services/agenda_visibility.py`
- Modify: `src/secretaria/api/hub/deps.py` (`get_agenda_viewer`; imports)
- Modify: `src/secretaria/schemas/calendar.py` (`AgendaViewerRead`)
- Modify: `src/secretaria/api/hub/calendar.py` (route `GET /viewer`; imports; module docstring)
- Modify: `tests/conftest.py` (autouse `_clinic_wide_agenda_viewer`)
- Create: `tests/_agenda_viewer.py`
- Modify: `tests/test_subscription.py` (append)
- Test: `tests/test_agenda_viewer.py`

**Interfaces:**
- Consumes: brain-api's introspection answer `{active, tenant_id, professional_id, agenda_scope?}` (brain-api plan, Task 2); `_r7_support` (`acting`, `r7`, `setup_world`, `add_professional`, `CALENDAR`).
- Produces:
  - `core.subscription`: `AGENDA_SCOPE_CLINIC = "clinic"`, `AGENDA_SCOPE_OWN = "own"`; `SubscriptionClaim(tenant_id, active, professional_id: UUID | None = None, agenda_scope: str | None = None)` (`agenda_scope` is `"clinic"`, `"own"` or None = brain-api did not say).
  - `services.agenda_visibility`: `@dataclass(frozen=True) AgendaViewer(scope: str, professional_id: UUID | None = None)` with `restricted: bool`, `can_filter_own: bool`, `sees(professional_id: UUID | None) -> bool`; `CLINIC_WIDE = AgendaViewer("clinic")`; `viewer_from_claim(agenda_scope: str | None, professional_id: UUID | None) -> AgendaViewer`.
  - `api.hub.deps.get_agenda_viewer(authorization=Header, tenant=Depends(get_current_tenant), session=Depends(get_session)) -> AgendaViewer` (401 without a live claim for this clinic).
  - `schemas.calendar.AgendaViewerRead(agenda_scope, professional_id, professional_name, can_filter_own)`; route `GET /tenants/me/calendar/viewer`.
  - `tests/_agenda_viewer.py::view_as(viewer: AgendaViewer) -> None`; every test is clinic-wide unless it calls `view_as`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subscription.py`:

```python
# --- TASK-044 R7: who is looking at the agenda (brain-api's agenda_scope) ----------


async def test_r7_the_claim_carries_the_professional_and_the_agenda_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, professional_id = uuid4(), uuid4()
    _install_fake_client(
        monkeypatch,
        body={
            "active": True,
            "tenant_id": str(tenant_id),
            "professional_id": str(professional_id),
            "agenda_scope": "own",
        },
    )

    claim = await subscription.verify_subscription_token("doctor-token")

    assert claim == SubscriptionClaim(
        tenant_id=tenant_id, active=True, professional_id=professional_id, agenda_scope="own"
    )


async def test_r7_an_older_brain_api_without_the_new_keys_still_authenticates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    _install_fake_client(monkeypatch, body={"active": True, "tenant_id": str(tenant_id)})

    claim = await subscription.verify_subscription_token("old-token")

    assert claim == SubscriptionClaim(tenant_id=tenant_id, active=True)
    assert claim.agenda_scope is None and claim.professional_id is None


@pytest.mark.parametrize("raw", ["everyone", "", "CLINIC", 7, ["clinic"], {"x": 1}])
async def test_r7_an_unknown_scope_reads_as_not_said(
    monkeypatch: pytest.MonkeyPatch, raw: object
) -> None:
    _install_fake_client(
        monkeypatch, body={"active": True, "tenant_id": str(uuid4()), "agenda_scope": raw}
    )

    claim = await subscription.verify_subscription_token(f"token-{raw!r}")

    assert claim is not None and claim.agenda_scope is None


@pytest.mark.parametrize("raw", ["nope", 12, ["x"], ""])
async def test_r7_a_malformed_professional_id_reads_as_absent(
    monkeypatch: pytest.MonkeyPatch, raw: object
) -> None:
    _install_fake_client(
        monkeypatch, body={"active": True, "tenant_id": str(uuid4()), "professional_id": raw}
    )

    claim = await subscription.verify_subscription_token(f"token-{raw!r}")

    assert claim is not None and claim.professional_id is None
```

Create `tests/_agenda_viewer.py`:

```python
"""Choose who is looking at the hub agenda in a test (TASK-044 R7).

tests/conftest.py makes every test clinic-wide by default (the pre-R7 behaviour);
`view_as` replaces that for the current test only - conftest pops it afterwards.
"""

from secretaria.services.agenda_visibility import AgendaViewer


def view_as(viewer: AgendaViewer) -> None:
    from secretaria.api.hub.deps import get_agenda_viewer
    from secretaria.main import app

    app.dependency_overrides[get_agenda_viewer] = lambda: viewer
```

Create `tests/test_agenda_viewer.py`:

```python
"""Who is looking at the agenda: brain-api's answer -> the hub viewer (TASK-044 R7, spec §5.A)."""

# ruff: noqa: F811

from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from secretaria.api.hub import deps as hub_deps
from secretaria.core.subscription import SubscriptionClaim
from secretaria.services.agenda_visibility import CLINIC_WIDE, AgendaViewer, viewer_from_claim
from tests._agenda_viewer import view_as
from tests._r7_support import CALENDAR, acting, r7, setup_world  # noqa: F401
from tests._reminder_fixtures import db  # noqa: F401

P1, P2 = uuid4(), uuid4()


# ------------------------------------------------------------------ the pure rule


@pytest.mark.parametrize(
    ("scope", "professional_id", "expected"),
    [
        ("clinic", P1, AgendaViewer("clinic", P1)),
        ("clinic", None, AgendaViewer("clinic", None)),
        ("own", P1, AgendaViewer("own", P1)),
        ("own", None, AgendaViewer("own", None)),
        (None, P1, AgendaViewer("own", P1)),  # older brain-api + a doctor's token: fail closed
        (None, None, CLINIC_WIDE),  # older brain-api, no professional: today's view
    ],
)
def test_brain_apis_answer_becomes_the_viewer(scope, professional_id, expected):
    assert viewer_from_claim(scope, professional_id) == expected


def test_what_each_viewer_sees():
    assert CLINIC_WIDE.sees(P1) and CLINIC_WIDE.sees(None)
    assert CLINIC_WIDE.can_filter_own is False
    assert AgendaViewer("clinic", P1).can_filter_own is True
    own = AgendaViewer("own", P1)
    assert own.restricted and own.sees(P1)
    assert not own.sees(P2) and not own.sees(None)
    assert own.can_filter_own is False
    nobody = AgendaViewer("own", None)
    assert not nobody.sees(None) and not nobody.sees(P1)


# ------------------------------------------------------------------ the dependency


def _verify_as(monkeypatch, claim) -> None:
    async def _fake(token: str):
        return claim

    monkeypatch.setattr(hub_deps, "verify_subscription_token", _fake)


async def test_the_dependency_reads_brain_apis_answer(db, acting, monkeypatch):
    world = await setup_world(db, acting, professional_name="Dra. Ana")
    ana = world.appointment.professional_id
    _verify_as(
        monkeypatch,
        SubscriptionClaim(
            tenant_id=world.tenant.id, active=True, professional_id=ana, agenda_scope="own"
        ),
    )

    async with db() as session:
        viewer = await hub_deps.get_agenda_viewer(
            authorization="Bearer t", tenant=world.tenant, session=session
        )

    assert viewer == AgendaViewer("own", ana)


async def test_a_professional_of_another_clinic_is_dropped_never_trusted(db, acting, monkeypatch):
    world = await setup_world(db, acting)
    other = await setup_world(
        db, acting, phone_number_id="pnid-2", wa_id="5511900000002", professional_name="Dr. Fora"
    )
    _verify_as(
        monkeypatch,
        SubscriptionClaim(
            tenant_id=world.tenant.id,
            active=True,
            professional_id=other.appointment.professional_id,
            agenda_scope="clinic",
        ),
    )

    async with db() as session:
        viewer = await hub_deps.get_agenda_viewer(
            authorization="Bearer t", tenant=world.tenant, session=session
        )

    assert viewer == AgendaViewer("clinic", None)  # no toggle, never the other clinic's doctor


async def test_an_older_brain_api_restricts_a_doctors_token(db, acting, monkeypatch):
    world = await setup_world(db, acting, professional_name="Dra. Ana")
    ana = world.appointment.professional_id
    _verify_as(monkeypatch, SubscriptionClaim(tenant_id=world.tenant.id, active=True, professional_id=ana))

    async with db() as session:
        viewer = await hub_deps.get_agenda_viewer(
            authorization="Bearer t", tenant=world.tenant, session=session
        )

    assert viewer == AgendaViewer("own", ana)


@pytest.mark.parametrize("case", ["no_claim", "inactive", "other_tenant", "no_token"])
async def test_no_live_session_for_this_clinic_is_401(db, acting, monkeypatch, case):
    world = await setup_world(db, acting)
    claim = {
        "no_claim": None,
        "inactive": SubscriptionClaim(tenant_id=world.tenant.id, active=False),
        "other_tenant": SubscriptionClaim(tenant_id=uuid4(), active=True, agenda_scope="clinic"),
        "no_token": SubscriptionClaim(tenant_id=world.tenant.id, active=True),
    }[case]
    _verify_as(monkeypatch, claim)

    async with db() as session:
        with pytest.raises(HTTPException) as err:
            await hub_deps.get_agenda_viewer(
                authorization=None if case == "no_token" else "Bearer t",
                tenant=world.tenant,
                session=session,
            )

    assert err.value.status_code == 401


# ------------------------------------------------------------------ GET /viewer


async def test_a_manager_who_is_also_a_doctor_gets_the_toggle(client: AsyncClient, db, acting):
    world = await setup_world(db, acting, professional_name="Dra. Ana")
    view_as(AgendaViewer("clinic", world.appointment.professional_id))

    body = (await client.get(f"{CALENDAR}/viewer")).json()

    assert body == {
        "agenda_scope": "clinic",
        "professional_id": str(world.appointment.professional_id),
        "professional_name": "Dra. Ana",
        "can_filter_own": True,
    }


async def test_a_receptionist_sees_the_clinic_without_toggle(client: AsyncClient, db, acting):
    await setup_world(db, acting)  # conftest default: CLINIC_WIDE

    body = (await client.get(f"{CALENDAR}/viewer")).json()

    assert body == {
        "agenda_scope": "clinic",
        "professional_id": None,
        "professional_name": None,
        "can_filter_own": False,
    }


async def test_a_doctor_restricted_to_his_own_agenda(client: AsyncClient, db, acting):
    world = await setup_world(db, acting, professional_name="Dr. Beto")
    view_as(AgendaViewer("own", world.appointment.professional_id))

    body = (await client.get(f"{CALENDAR}/viewer")).json()

    assert body["agenda_scope"] == "own" and body["can_filter_own"] is False
    assert body["professional_name"] == "Dr. Beto"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pyt tests/test_agenda_viewer.py tests/test_subscription.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'secretaria.services.agenda_visibility'` and `TypeError: SubscriptionClaim.__init__() got an unexpected keyword argument 'professional_id'`.

- [ ] **Step 3: Carry brain-api's answer on the claim**

In `src/secretaria/core/subscription.py`:

(a) in the module docstring, replace the sentence ``200 response: {"active": bool, "tenant_id": "<uuid>" | null}.`` with ``200 response: {"active": bool, "tenant_id": "<uuid>" | null, "professional_id": "<uuid>" | null, "agenda_scope": "clinic" | "own"} (the last two since TASK-044 R7; an older brain-api omits them and that is NOT an error - see services/agenda_visibility.py).``

(b) right after `_CACHE_MAX_SIZE = 512`, add:

```python
# TASK-044 R7 (spec 2026-10-09 §5.A): brain-api's answer to "whose appointments may this
# hub session see". Any other value reads as None ("not said") - the fail-closed rule for
# that case lives in services/agenda_visibility.py::viewer_from_claim.
AGENDA_SCOPE_CLINIC = "clinic"
AGENDA_SCOPE_OWN = "own"
_AGENDA_SCOPES = frozenset({AGENDA_SCOPE_CLINIC, AGENDA_SCOPE_OWN})
```

(c) in `SubscriptionClaim`, after `active: bool`, add (and extend its docstring with the two lines):

```python
    # TASK-044 R7: the acting user's secretarIA professional (brain-api `users.professional_id`)
    # and brain-api's agenda scope ("clinic" | "own" | None = not said). Defaulted so an
    # older brain-api answer still builds a claim.
    professional_id: UUID | None = None
    agenda_scope: str | None = None
```

(d) right before `async def verify_subscription_token`, add:

```python
def _optional_uuid(raw: object) -> UUID | None:
    """A UUID from the answer, or None for anything absent or malformed (never raises)."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None
```

(e) replace the line `    claim = SubscriptionClaim(tenant_id=tenant_id, active=True)` with:

```python
    raw_scope = body.get("agenda_scope")
    agenda_scope = (
        raw_scope if isinstance(raw_scope, str) and raw_scope in _AGENDA_SCOPES else None
    )
    if raw_scope is not None and agenda_scope is None:
        logger.warning("subscription_verify_unknown_agenda_scope")
    claim = SubscriptionClaim(
        tenant_id=tenant_id,
        active=True,
        professional_id=_optional_uuid(body.get("professional_id")),
        agenda_scope=agenda_scope,
    )
```

- [ ] **Step 4: The rule's one home**

Create `src/secretaria/services/agenda_visibility.py`:

```python
"""Who may see which appointments in the hub agenda (TASK-044 R7, spec 2026-10-09 §5.A).

brain-api is the identity authority: its hub-token introspection says whether the person
behind the hub session sees the whole clinic ("clinic": owner/manager, secretary) or only
their own appointments ("own": any other doctor), plus the secretarIA professional they
are. This module turns that answer into ONE value the hub routes consult
(api/hub/deps.py::get_agenda_viewer); it never re-derives a role.

Rollout - brain-api may still be on the version without `agenda_scope`: a session whose
token carries a professional is treated as "own" (fail closed: a doctor never sees more
than his own because a deploy is late); a session without one keeps today's clinic-wide
view (a receptionist or a manager without a professional row has no own agenda to be
narrowed to, and locking them out would stop the clinic's reception).
"""

from dataclasses import dataclass
from uuid import UUID

from secretaria.core.subscription import AGENDA_SCOPE_CLINIC, AGENDA_SCOPE_OWN


@dataclass(frozen=True)
class AgendaViewer:
    """The hub session's agenda visibility.

    `professional_id` is a Professional row of THIS clinic (the dependency drops any
    other), or None when the person is not a professional here.
    """

    scope: str
    professional_id: UUID | None = None

    @property
    def restricted(self) -> bool:
        """True = only the appointments of `professional_id` exist for this viewer."""
        return self.scope != AGENDA_SCOPE_CLINIC

    @property
    def can_filter_own(self) -> bool:
        """A clinic-wide viewer who is also a professional: the "Todos / Só os meus" switch."""
        return not self.restricted and self.professional_id is not None

    def sees(self, professional_id: UUID | None) -> bool:
        """Whether an appointment owned by `professional_id` exists for this viewer."""
        if not self.restricted:
            return True
        return self.professional_id is not None and professional_id == self.professional_id


CLINIC_WIDE = AgendaViewer(scope=AGENDA_SCOPE_CLINIC)


def viewer_from_claim(agenda_scope: str | None, professional_id: UUID | None) -> AgendaViewer:
    """brain-api's answer -> the viewer. `agenda_scope` None = brain-api did not say."""
    if agenda_scope == AGENDA_SCOPE_CLINIC:
        return AgendaViewer(scope=AGENDA_SCOPE_CLINIC, professional_id=professional_id)
    if agenda_scope == AGENDA_SCOPE_OWN or professional_id is not None:
        return AgendaViewer(scope=AGENDA_SCOPE_OWN, professional_id=professional_id)
    return CLINIC_WIDE
```

- [ ] **Step 5: The dependency**

In `src/secretaria/api/hub/deps.py`: add `from dataclasses import replace` at the top of the imports; change `from secretaria.models import Tenant` to `from secretaria.models import Professional, Tenant`; add `from secretaria.services.agenda_visibility import AgendaViewer, viewer_from_claim`; append to the module docstring: ``get_agenda_viewer (TASK-044 R7) adds WHO is looking - brain-api's agenda scope - for the agenda routes.`` Append at the end of the module:

```python
async def get_agenda_viewer(
    authorization: str | None = Header(default=None),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> AgendaViewer:
    """Who is looking at the agenda (TASK-044 R7, spec 2026-10-09 §5.A).

    Re-reads the introspection `get_current_tenant` just did: a positive answer is cached
    in-process (core/subscription.py), so this is no second brain-api call within
    SUBSCRIPTION_CACHE_TTL_SECONDS. Fails closed: no live claim for THIS clinic is 401.
    The linked professional must be a Professional row of this clinic - any other id is
    dropped, and an "own" viewer without a professional then reaches no appointment.
    """
    token = _bearer_token(authorization)
    claim = await verify_subscription_token(token) if token else None
    if claim is None or not claim.active or claim.tenant_id != tenant.id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or inactive subscription token")
    viewer = viewer_from_claim(claim.agenda_scope, claim.professional_id)
    if viewer.professional_id is not None:
        owned = await session.scalar(
            select(Professional.id).where(
                Professional.id == viewer.professional_id,
                Professional.tenant_id == tenant.id,
            )
        )
        if owned is None:
            viewer = replace(viewer, professional_id=None)
    return viewer
```

- [ ] **Step 6: The default for every test**

In `tests/conftest.py`, right after the `_no_turn_apology` fixture, add:

```python
@pytest.fixture(autouse=True)
def _clinic_wide_agenda_viewer():
    """TASK-044 R7: who is looking at the hub agenda comes from brain-api's token
    introspection (api/hub/deps.py::get_agenda_viewer). Tests that override
    `get_current_tenant` never present a token, so by default they see the whole
    clinic - exactly the pre-R7 behaviour. Scoping tests call
    tests/_agenda_viewer.py::view_as; tests of the dependency call it directly.
    """
    from secretaria.api.hub.deps import get_agenda_viewer
    from secretaria.main import app
    from secretaria.services.agenda_visibility import CLINIC_WIDE

    app.dependency_overrides[get_agenda_viewer] = lambda: CLINIC_WIDE
    yield
    app.dependency_overrides.pop(get_agenda_viewer, None)
```

- [ ] **Step 7: `GET /viewer`**

In `src/secretaria/schemas/calendar.py`, right before `class CalendarEventRead`, add:

```python
class AgendaViewerRead(BaseModel):
    """GET /calendar/viewer - who is looking at the agenda (TASK-044 R7, spec §5.A).

    `agenda_scope` "clinic" sees every doctor; "own" only `professional_id`'s
    appointments (none when it is null). `can_filter_own` = clinic-wide AND a
    professional: the front shows "Todos / Só os meus" only then.
    """

    agenda_scope: Literal["clinic", "own"]
    professional_id: str | None = None
    professional_name: str | None = None
    can_filter_own: bool = False
```

In `src/secretaria/api/hub/calendar.py`: add `GET   /tenants/me/calendar/viewer                      - who is looking (role scope).` as the first line of the route list in the module docstring; change `from secretaria.api.hub.deps import get_current_tenant` to `from secretaria.api.hub.deps import get_agenda_viewer, get_current_tenant`; add `AgendaViewerRead,` to the `from secretaria.schemas.calendar import (...)` block; add `from secretaria.services.agenda_visibility import AgendaViewer` after the `from secretaria.services.appointment_status import (...)` block. Right before the `# GET /events — agenda read model` section header, add:

```python
# ---------------------------------------------------------------------------
# GET /viewer — who is looking (TASK-044 R7)
# ---------------------------------------------------------------------------


@router.get("/viewer", response_model=AgendaViewerRead)
async def agenda_viewer(
    tenant: Tenant = Depends(get_current_tenant),
    viewer: AgendaViewer = Depends(get_agenda_viewer),
    session: AsyncSession = Depends(get_session),
) -> AgendaViewerRead:
    """Whose appointments this session sees, and whether to offer "Só os meus"."""
    name = None
    if viewer.professional_id is not None:
        name = await session.scalar(
            select(Professional.name).where(
                Professional.id == viewer.professional_id,
                Professional.tenant_id == tenant.id,
            )
        )
    return AgendaViewerRead(
        agenda_scope=viewer.scope,
        professional_id=str(viewer.professional_id) if viewer.professional_id else None,
        professional_name=name,
        can_filter_own=viewer.can_filter_own,
    )
```

- [ ] **Step 8: Run the tests (auth suites included)**

Run: `pyt tests/test_agenda_viewer.py tests/test_subscription.py tests/test_hub_deps.py tests/test_hub_conversations.py tests/test_hub_calendar_confirmation.py tests/test_hub_status_guards.py -q`
Expected: PASS.

- [ ] **Step 9: Lint and commit**

```bash
uvx ruff format src/secretaria/services/agenda_visibility.py tests/_agenda_viewer.py tests/test_agenda_viewer.py
uvx ruff check --fix src/secretaria/services/agenda_visibility.py tests/_agenda_viewer.py tests/test_agenda_viewer.py
uvx ruff check src/secretaria/core/subscription.py src/secretaria/api/hub/deps.py src/secretaria/schemas/calendar.py src/secretaria/api/hub/calendar.py tests/conftest.py tests/test_subscription.py
git diff --stat
git add src/secretaria/core/subscription.py src/secretaria/services/agenda_visibility.py src/secretaria/api/hub/deps.py src/secretaria/schemas/calendar.py src/secretaria/api/hub/calendar.py tests/conftest.py tests/_agenda_viewer.py tests/test_subscription.py tests/test_agenda_viewer.py
git commit -m "feat(hub): agenda viewer from brain-api's agenda_scope, GET /viewer (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 14: `GET /events` — only what the viewer may see, "só os meus", and the Editar pre-fill

**Files:**
- Modify: `src/secretaria/schemas/calendar.py` (`CalendarEventRead`: six keys)
- Modify: `src/secretaria/api/hub/calendar.py` (`list_events` rewritten; `and_` import)
- Modify: `tests/test_calendar_events_insurance.py` (`NEW_EVENT_KEYS`), `tests/test_hub_calendar_confirmation.py` (`NEW_KEYS`)
- Test: `tests/test_hub_events_visibility.py`

**Interfaces:**
- Consumes: `AgendaViewer`, `get_agenda_viewer`, `view_as` (Task 13); `_r7_support` (`FakeCalendar`, `setup_world`, `add_professional`, `soon`, `CALENDAR`).
- Produces: `GET /tenants/me/calendar/events?start=&end=&mine=` (`mine: bool = False`); `CalendarEventRead.professional_id/professional_name/service/attendee_name/phone/patient_channel` (all `str | None`); 422 `no_own_agenda`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hub_events_visibility.py`:

```python
"""GET /events: who sees what, "só os meus" and the Editar pre-fill (TASK-044 R7, §5.A/§5.C)."""

# ruff: noqa: F811

from datetime import timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient

from secretaria.models import Appointment, AppointmentStatus, Patient
from secretaria.services.agenda_visibility import AgendaViewer
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from tests._agenda_viewer import view_as
from tests._r7_support import (  # noqa: F401
    CALENDAR,
    FakeCalendar,
    acting,
    add_professional,
    r7,
    setup_world,
    soon,
)
from tests._reminder_fixtures import db  # noqa: F401

PREFILL_KEYS = {
    "professional_id",
    "professional_name",
    "service",
    "attendee_name",
    "phone",
    "patient_channel",
}
RANGE = {"start": "2026-01-01T00:00:00Z", "end": "2028-01-01T00:00:00Z"}
ALL = {"evt-ana", "evt-beto", "evt-sem-medico", "evt-google"}


def _google(monkeypatch, *ids: str) -> None:
    """The clinic's Google calendar returns these events (titles carry patient names)."""
    events = [
        {
            "id": gid,
            "summary": f"Consulta - paciente de {gid}",
            "start": "2026-10-20T13:00:00Z",
            "end": "2026-10-20T13:30:00Z",
        }
        for gid in ids
    ]

    async def _check(self, start, end):
        return events

    monkeypatch.setattr(FakeCalendar, "check_availability", _check)


async def _clinic(db, acting, monkeypatch):
    """Dra. Ana's WhatsApp patient (evt-ana), Dr. Beto's Portal patient (evt-beto), an
    appointment without a doctor (evt-sem-medico) and an event typed into Google."""
    world = await setup_world(db, acting, professional_name="Dra. Ana", google_event_id="evt-ana")
    beto = await add_professional(db, world.tenant.id, "Dr. Beto")
    start = soon(4)
    async with db() as session:
        portal = Patient(
            id=uuid4(),
            tenant_id=world.tenant.id,
            wa_id=None,
            channel=CHANNEL_BRAIN_MESSAGE,
            external_id=str(uuid4()),
            name="João",
        )
        session.add(portal)
        await session.flush()
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=world.tenant.id,
                patient_id=portal.id,
                google_event_id="evt-beto",
                appointment_type="Retorno",
                start_at=start,
                end_at=start + timedelta(minutes=40),
                status=AppointmentStatus.SCHEDULED,
                professional_id=beto,
                attendee_name="Pedro",
                phone="5511977770000",
                insurance="Unimed",
            )
        )
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=world.tenant.id,
                patient_id=None,
                google_event_id="evt-sem-medico",
                appointment_type="Consulta",
                start_at=start,
                end_at=start + timedelta(minutes=30),
                status=AppointmentStatus.SCHEDULED,
                phone="5511966660000",
            )
        )
        await session.commit()
    _google(monkeypatch, "evt-ana", "evt-beto", "evt-sem-medico", "evt-google")
    return world, beto


async def _events(client: AsyncClient, **params):
    return await client.get(f"{CALENDAR}/events", params={**RANGE, **params})


def _by_id(body) -> dict:
    return {e["id"]: e for e in body}


async def test_the_clinic_sees_every_event_with_the_edit_prefill(
    client: AsyncClient, db, acting, monkeypatch
):
    world, beto = await _clinic(db, acting, monkeypatch)

    body = _by_id((await _events(client)).json())

    assert set(body) == ALL
    ana = body["evt-ana"]
    assert ana["professional_id"] == str(world.appointment.professional_id)
    assert ana["professional_name"] == "Dra. Ana"
    assert (ana["service"], ana["attendee_name"], ana["phone"]) == ("Consulta", None, None)
    assert ana["patient_channel"] == "whatsapp"
    joao = body["evt-beto"]
    assert (joao["professional_id"], joao["professional_name"]) == (str(beto), "Dr. Beto")
    assert (joao["service"], joao["attendee_name"]) == ("Retorno", "Pedro")
    assert (joao["phone"], joao["insurance"]) == ("5511977770000", "Unimed")
    assert joao["patient_channel"] == "brain_message"
    orphan = body["evt-sem-medico"]
    assert orphan["professional_id"] is None and orphan["professional_name"] is None
    assert orphan["patient_channel"] is None and orphan["phone"] == "5511966660000"
    assert all(body["evt-google"][key] is None for key in PREFILL_KEYS)


@pytest.mark.parametrize("mine", [None, "false", "true"])
async def test_a_restricted_doctor_sees_only_his_own_whatever_the_filter_says(
    client: AsyncClient, db, acting, monkeypatch, mine
):
    _world, beto = await _clinic(db, acting, monkeypatch)
    view_as(AgendaViewer("own", beto))

    response = await _events(client, **({} if mine is None else {"mine": mine}))

    assert response.status_code == 200
    body = response.json()
    assert [e["id"] for e in body] == ["evt-beto"]
    assert "paciente de evt-ana" not in response.text  # no other title, Google-only included


async def test_a_restricted_doctor_without_a_professional_sees_nothing(
    client: AsyncClient, db, acting, monkeypatch
):
    await _clinic(db, acting, monkeypatch)
    view_as(AgendaViewer("own", None))

    response = await _events(client)

    assert response.status_code == 200 and response.json() == []


async def test_a_manager_who_is_also_a_doctor_switches_between_all_and_his_own(
    client: AsyncClient, db, acting, monkeypatch
):
    _world, beto = await _clinic(db, acting, monkeypatch)
    view_as(AgendaViewer("clinic", beto))

    everyone = await _events(client, mine="false")
    his_own = await _events(client, mine="true")

    assert set(_by_id(everyone.json())) == ALL
    assert [e["id"] for e in his_own.json()] == ["evt-beto"]


async def test_a_receptionist_asking_for_her_own_agenda_is_422(
    client: AsyncClient, db, acting, monkeypatch
):
    await _clinic(db, acting, monkeypatch)  # conftest default viewer: CLINIC_WIDE

    response = await _events(client, mine="true")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "no_own_agenda"


async def test_a_doctor_of_another_clinic_on_the_row_never_resolves(
    client: AsyncClient, db, acting, monkeypatch
):
    world, _beto = await _clinic(db, acting, monkeypatch)
    other = await setup_world(
        db, acting, phone_number_id="pnid-2", wa_id="5511900000002", professional_name="Dr. Fora"
    )
    acting["tenant_id"] = world.tenant.id
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.professional_id = other.appointment.professional_id
        await session.commit()

    ana = _by_id((await _events(client)).json())["evt-ana"]

    assert ana["professional_id"] is None and ana["professional_name"] is None
```

In `tests/test_calendar_events_insurance.py`, replace the line

```python
NEW_EVENT_KEYS = {"status", "confirmation_count", "display_state", "attention", "reminders"}
```

with:

```python
NEW_EVENT_KEYS = {"status", "confirmation_count", "display_state", "attention", "reminders"} | {
    # TASK-044 R7 (spec §5.C): what Editar/Remarcar pre-fills
    "professional_id",
    "professional_name",
    "service",
    "attendee_name",
    "phone",
    "patient_channel",
}
```

In `tests/test_hub_calendar_confirmation.py`, replace the line

```python
NEW_KEYS = {"status", "confirmation_count", "display_state", "attention", "reminders"}
```

with:

```python
NEW_KEYS = {"status", "confirmation_count", "display_state", "attention", "reminders"} | {
    # TASK-044 R7 (spec §5.C): what Editar/Remarcar pre-fills
    "professional_id",
    "professional_name",
    "service",
    "attendee_name",
    "phone",
    "patient_channel",
}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pyt tests/test_hub_events_visibility.py tests/test_calendar_events_insurance.py tests/test_hub_calendar_confirmation.py -q`
Expected: FAIL — `KeyError: 'professional_id'`, the restricted doctor receives all four events, and the two key-set pins differ.

- [ ] **Step 3: The six keys**

In `src/secretaria/schemas/calendar.py`, at the end of `CalendarEventRead` (after `reminders: list[CalendarReminderRead] | None = None`), add:

```python
    # TASK-044 R7 (spec 2026-10-09 §5.C): what the "Editar/Remarcar" form pre-fills, so
    # the front stops guessing. All None for an event with no local Appointment.
    # `professional_id`/`professional_name` only when the owner is a professional of
    # THIS clinic; `service` is the stored service name; `phone` is the appointment's
    # CONTACT phone (never the patient's identity); `patient_channel` is the patient's
    # own channel - "whatsapp" | "brain_message" - or None without a patient record (a
    # block or a phone-only booking). A viewer restricted to his own agenda never
    # receives another doctor's event at all (api/hub/calendar.py::list_events).
    professional_id: str | None = None
    professional_name: str | None = None
    service: str | None = None
    attendee_name: str | None = None
    phone: str | None = None
    patient_channel: str | None = None
```

- [ ] **Step 4: Rewrite `list_events`**

In `src/secretaria/api/hub/calendar.py`, change `from sqlalchemy import Row, select, update` to `from sqlalchemy import Row, and_, select, update`, and replace the whole `list_events` function (from `@router.get("/events", ...)` to its `return reads`) with:

```python
@router.get("/events", response_model=list[CalendarEventRead])
async def list_events(
    start: datetime,
    end: datetime,
    mine: bool = False,
    tenant: Tenant = Depends(get_current_tenant),
    viewer: AgendaViewer = Depends(get_agenda_viewer),
    session: AsyncSession = Depends(get_session),
) -> list[CalendarEventRead]:
    """The agenda. TASK-044 R7 (spec 2026-10-09 §5.A/§5.C):

    * a viewer restricted to his own agenda gets ONLY events whose local appointment is
      his - never a Google-only event (its title can carry another patient's name), an
      appointment without a doctor or another doctor's; `mine` cannot widen that;
    * a clinic-wide viewer who is also a professional narrows with `mine=true`
      ("Só os meus"); a clinic-wide viewer without one asking for it is 422;
    * every event with a local appointment carries the Editar/Remarcar pre-fill.
    The Google read itself is unchanged (the clinic's calendar, as before).
    """
    if mine and not viewer.restricted and viewer.professional_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            _detail("no_own_agenda", "Seu usuário não está ligado a um profissional da clínica."),
        )
    own_only = viewer.restricted or mine
    cal = await _get_calendar(session, tenant)
    events = await cal.check_availability(start, end)

    # Attach the LOCAL Appointment.id to every event that has one, so the
    # agenda can call cancel/reschedule (which key on it) instead of only
    # being able to display. See CalendarEventRead's docstring.
    #
    # ONE query for the whole page, joined in memory — not one lookup per
    # event. A month of a busy clinic is hundreds of events, and the N+1
    # version would put that many round trips behind a screen the doctor
    # opens constantly.
    #
    # The `tenant_id` filter is the isolation invariant, not decoration: it is
    # what stops a google_event_id from another clinic's calendar (a shared or
    # mis-configured Google account) resolving to that clinic's appointment
    # and handing this doctor a working cancel button for someone else's
    # patient. The patient join is tenant-scoped for the same reason.
    google_ids = [e["id"] for e in events if e.get("id")]
    booked: dict[str, Row] = {}
    if google_ids:
        # The convênio columns and the TASK-044 pre-fill ride along on the SAME
        # single query: the plan ids are only needed to resolve names, never sent.
        rows = await session.execute(
            select(
                Appointment.google_event_id,
                Appointment.id,
                Appointment.insurance,
                Appointment.insurance_plan_id,
                Appointment.insurance_professional_plan_id,
                Appointment.status,
                Appointment.confirmation_count,
                Appointment.start_at,
                Appointment.professional_id,
                Appointment.appointment_type,
                Appointment.attendee_name,
                Appointment.phone,
                Patient.channel.label("patient_channel"),
            )
            .outerjoin(
                Patient,
                and_(Patient.id == Appointment.patient_id, Patient.tenant_id == tenant.id),
            )
            .where(
                Appointment.tenant_id == tenant.id,
                Appointment.google_event_id.in_(google_ids),
            )
        )
        booked = {row.google_event_id: row for row in rows.all()}

    if own_only:
        # TASK-044 R7: before anything else is loaded - nothing of another doctor's
        # appointment (plan, deposit, reminders, patient) is even read for this page.
        focus = viewer.professional_id
        booked = {
            gid: row
            for gid, row in booked.items()
            if focus is not None and row.professional_id == focus
        }
        events = [e for e in events if e.get("id") in booked]

    # At most ONE tenant-scoped query for the page's doctor names; a professional id
    # of another clinic on a row never resolves (shown as no doctor).
    owner_ids = {row.professional_id for row in booked.values() if row.professional_id}
    professional_names: dict[UUID, str] = {}
    if owner_ids:
        named = await session.execute(
            select(Professional.id, Professional.name).where(
                Professional.tenant_id == tenant.id,
                Professional.id.in_(owner_ids),
            )
        )
        professional_names = {pid: name for pid, name in named.all()}

    # At most ONE query per plan table for the whole page, both tenant-scoped
    # (services/insurance_catalog.py::load_appointment_plans) - a plan id from
    # another clinic never resolves, whatever the appointment row says.
    plans = await load_appointment_plans(
        session,
        tenant.id,
        tenant_plan_ids=[row.insurance_plan_id for row in booked.values()],
        professional_plan_ids=[row.insurance_professional_plan_id for row in booked.values()],
    )
    # ONE query for the whole page's deposit state, also tenant-scoped: a
    # pix_deposits row of another clinic never resolves. Read-only - nothing in
    # the deposit lifecycle is touched by listing the agenda.
    deposits = await deposit_lifecycle.load_deposit_views(
        session, tenant.id, [row.id for row in booked.values()]
    )

    # ONE tenant-scoped query for the whole page's reminder rows (never one per
    # event); rows of another clinic never resolve.
    reminders_by_appointment: dict[UUID, list[AppointmentReminder]] = {}
    if booked:
        reminder_rows = await session.scalars(
            select(AppointmentReminder).where(
                AppointmentReminder.tenant_id == tenant.id,
                AppointmentReminder.invalidated_at.is_(None),
                AppointmentReminder.appointment_id.in_([row.id for row in booked.values()]),
            )
        )
        for reminder in reminder_rows:
            reminders_by_appointment.setdefault(reminder.appointment_id, []).append(reminder)

    reads: list[CalendarEventRead] = []
    for e in events:
        row = booked.get(e["id"])
        plan = (
            plans.resolve(row.insurance_plan_id, row.insurance_professional_plan_id)
            if row is not None
            else None
        )
        current = (
            _current_reminders(row, reminders_by_appointment.get(row.id, []))
            if row is not None
            else None
        )
        state = reminder_schedule.display_state(row, current) if row is not None else None
        owner_name = professional_names.get(row.professional_id) if row is not None else None
        reads.append(
            CalendarEventRead(
                id=e["id"],
                summary=e.get("summary"),
                start=e["start"],
                end=e["end"],
                appointment_id=str(row.id) if row is not None else None,
                insurance=_insurance_label(row.insurance) if row is not None else None,
                insurance_plan=_insurance_plan_read(plan),
                deposit=_deposit_read(deposits.get(row.id)) if row is not None else None,
                status=row.status if row is not None else None,
                confirmation_count=row.confirmation_count if row is not None else None,
                display_state=state,
                attention=(state == reminder_schedule.DISPLAY_ATTENTION)
                if state is not None
                else None,
                reminders=[_reminder_read(r) for r in current] if current is not None else None,
                professional_id=str(row.professional_id) if owner_name is not None else None,
                professional_name=owner_name,
                service=row.appointment_type if row is not None else None,
                attendee_name=row.attendee_name if row is not None else None,
                phone=row.phone if row is not None else None,
                patient_channel=row.patient_channel if row is not None else None,
            )
        )
    return reads
```

- [ ] **Step 5: Run the tests (the statement-count tests included)**

Run: `pyt tests/test_hub_events_visibility.py tests/test_calendar_events_insurance.py tests/test_hub_calendar_confirmation.py tests/test_cancellation_notice.py tests/test_brain_onboarding.py -q`
Expected: PASS — in particular `test_calendar_events_insurance.py`'s `sql.count("FROM appointments") == 1` still holds (the patient join is in the same statement).

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff format tests/test_hub_events_visibility.py && uvx ruff check --fix tests/test_hub_events_visibility.py
uvx ruff check src/secretaria/schemas/calendar.py src/secretaria/api/hub/calendar.py tests/test_calendar_events_insurance.py tests/test_hub_calendar_confirmation.py
git diff --stat
git add src/secretaria/schemas/calendar.py src/secretaria/api/hub/calendar.py tests/test_hub_events_visibility.py tests/test_calendar_events_insurance.py tests/test_hub_calendar_confirmation.py
git commit -m "feat(hub): GET /events scoped by viewer, mine filter, edit pre-fill keys (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 15: Every appointment route respects the viewer; a restricted doctor creates only on his own agenda

**Files:**
- Modify: `src/secretaria/api/hub/calendar.py` (`_get_appointment`; the seven id routes; `_creation_professional` (new); `create_appointment`, `create_block`; `edit_appointment`)
- Modify: `src/secretaria/schemas/calendar.py` (`AppointmentCreate`, `BlockCreate`: optional `professional_id`)
- Test: `tests/test_hub_agenda_scope_actions.py`

**Interfaces:**
- Consumes: `AgendaViewer`, `get_agenda_viewer`, `view_as` (Task 13); `add_professional`, `soon`, `FakeCalendar` (`tests/_r7_support.py`, Task 6); the routes of Tasks 4, 6, 11, 12 and R4.
- Produces: `api/hub/calendar.py::_get_appointment(session, tenant, appointment_id: str, viewer: AgendaViewer) -> Appointment` (404 `"Appointment not found"` also for an appointment the viewer does not see); `api/hub/calendar.py::_creation_professional(session, tenant, viewer: AgendaViewer, requested: UUID | None) -> UUID | None`; `AppointmentCreate.professional_id: UUID | None = None`, `BlockCreate.professional_id: UUID | None = None`; on `POST /appointments` and `POST /blocks`: an `"own"` viewer's rows get his `professional_id`, another id → 403 `professional_not_allowed`, no own professional → 422 `no_own_agenda`, a clinic viewer's unknown/inactive id → 422 `unknown_professional`; 403 `professional_not_allowed` on `POST …/edit`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hub_agenda_scope_actions.py`:

```python
"""A doctor restricted to his own agenda cannot reach another doctor's appointment
(TASK-044 R7, spec 2026-10-09 §5.A)."""

# ruff: noqa: F811

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from secretaria.models import Appointment, AppointmentStatus
from secretaria.services.agenda_visibility import AgendaViewer
from tests._agenda_viewer import view_as
from tests._r7_support import (  # noqa: F401
    CALENDAR,
    FakeArqPool,
    FakeCalendar,
    acting,
    add_professional,
    install_pool,
    r7,
    sent,
    set_tenant,
    setup_world,
    soon,
)
from tests._reminder_fixtures import db, other_tenant  # noqa: F401
from tests._reminders_v2 import outbound_messages, reload_appointment

SERVICES = [{"name": "Consulta", "is_active": True, "duration_min": 30}]


def _iso(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _route(world, tail: str) -> tuple[str, dict | None]:
    later = world.start_at + timedelta(days=1)
    return {
        "cancel-preview": ("GET", None),
        "cancel": ("POST", {"confirm": True}),
        "release": ("POST", {}),
        "message": ("POST", {"text": "Oi"}),
        "reschedule": (
            "POST",
            {"new_start": _iso(later), "new_end": _iso(later + timedelta(minutes=30))},
        ),
        "status": ("PATCH", {"status": "confirmed"}),
        "edit": ("POST", {"attendee_name": "Outro nome"}),
    }[tail]


ID_ROUTES = ["cancel-preview", "cancel", "release", "message", "reschedule", "status", "edit"]


async def _call(client: AsyncClient, world, appointment_id, tail: str):
    method, body = _route(world, tail)
    url = f"{CALENDAR}/appointments/{appointment_id}/{tail}"
    return await client.request(method, url, json=body)


async def _anas_world(db, acting):
    world = await setup_world(db, acting, professional_name="Dra. Ana", google_event_id="evt-1")
    await set_tenant(db, world.tenant.id, appointment_types=SERVICES)
    return world


@pytest.mark.parametrize("tail", ID_ROUTES)
async def test_another_doctors_appointment_does_not_exist_for_a_restricted_doctor(
    client: AsyncClient, db, acting, r7, tail
):
    pool = FakeArqPool()
    install_pool(pool)
    world = await _anas_world(db, acting)
    beto = await add_professional(db, world.tenant.id, "Dr. Beto")
    view_as(AgendaViewer("own", beto))

    response = await _call(client, world, world.appointment.id, tail)

    assert response.status_code == 404
    assert response.json() == {"detail": "Appointment not found"}
    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.SCHEDULED and row.attendee_name is None
    assert sent() == [] and pool.calls == [] and r7["mail"] == []
    assert await outbound_messages(db, world.conversation.id) == []
    assert FakeCalendar.instances == {}  # Google never touched


@pytest.mark.parametrize("tail", ["cancel-preview", "status"])
async def test_the_404_is_byte_identical_to_an_id_that_does_not_exist(
    client: AsyncClient, db, acting, tail
):
    world = await _anas_world(db, acting)
    beto = await add_professional(db, world.tenant.id, "Dr. Beto")
    view_as(AgendaViewer("own", beto))

    hidden = await _call(client, world, world.appointment.id, tail)
    missing = await _call(client, world, uuid4(), tail)

    assert (hidden.status_code, hidden.content) == (missing.status_code, missing.content)


async def test_an_appointment_without_a_doctor_is_out_of_a_restricted_view(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting)  # professional_id is None
    beto = await add_professional(db, world.tenant.id, "Dr. Beto")
    view_as(AgendaViewer("own", beto))

    assert (await _call(client, world, world.appointment.id, "status")).status_code == 404


async def test_a_restricted_doctor_without_a_professional_reaches_nothing(
    client: AsyncClient, db, acting
):
    world = await _anas_world(db, acting)
    view_as(AgendaViewer("own", None))

    assert (await _call(client, world, world.appointment.id, "cancel-preview")).status_code == 404


async def test_his_own_appointment_works_as_before(client: AsyncClient, db, acting):
    world = await _anas_world(db, acting)
    view_as(AgendaViewer("own", world.appointment.professional_id))

    preview = await _call(client, world, world.appointment.id, "cancel-preview")
    edited = await _call(client, world, world.appointment.id, "edit")

    assert preview.status_code == 200 and preview.json()["professional_name"] == "Dra. Ana"
    assert edited.status_code == 200, edited.text
    assert (await reload_appointment(db, world.appointment.id)).attendee_name == "Outro nome"


async def test_a_manager_who_is_also_a_doctor_reaches_every_doctor(
    client: AsyncClient, db, acting
):
    world = await _anas_world(db, acting)
    beto = await add_professional(db, world.tenant.id, "Dr. Beto")
    view_as(AgendaViewer("clinic", beto))

    response = await _call(client, world, world.appointment.id, "cancel-preview")

    assert response.status_code == 200


def _create_body(**extra) -> dict:
    start = soon(5)
    return {
        "start": _iso(start),
        "end": _iso(start + timedelta(minutes=30)),
        "summary": "Consulta",
        **extra,
    }


async def _created(db, appointment_id: str) -> Appointment:
    async with db() as session:
        return await session.scalar(select(Appointment).where(Appointment.id == UUID(appointment_id)))


@pytest.mark.parametrize("path", ["appointments", "blocks"])
async def test_a_restricted_doctor_creates_on_his_own_agenda_by_default(
    client: AsyncClient, db, acting, path
):
    world = await _anas_world(db, acting)
    ana = world.appointment.professional_id
    view_as(AgendaViewer("own", ana))

    response = await client.post(f"{CALENDAR}/{path}", json=_create_body())

    assert response.status_code == 201, response.text
    row = await _created(db, response.json()["id"])
    assert row.professional_id == ana  # visible in his own "own" view at once
    assert len(FakeCalendar.get("tenant").created) == 1  # Google unchanged: the clinic calendar


@pytest.mark.parametrize("path", ["appointments", "blocks"])
async def test_a_restricted_doctor_may_name_himself(client: AsyncClient, db, acting, path):
    world = await _anas_world(db, acting)
    ana = world.appointment.professional_id
    view_as(AgendaViewer("own", ana))

    response = await client.post(f"{CALENDAR}/{path}", json=_create_body(professional_id=str(ana)))

    assert response.status_code == 201, response.text
    assert (await _created(db, response.json()["id"])).professional_id == ana


@pytest.mark.parametrize("path", ["appointments", "blocks"])
async def test_a_restricted_doctor_cannot_create_on_another_doctors_agenda(
    client: AsyncClient, db, acting, path
):
    world = await _anas_world(db, acting)
    beto = await add_professional(db, world.tenant.id, "Dr. Beto")
    view_as(AgendaViewer("own", world.appointment.professional_id))

    response = await client.post(f"{CALENDAR}/{path}", json=_create_body(professional_id=str(beto)))

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "professional_not_allowed"
    assert FakeCalendar.instances == {}  # refused before Google
    async with db() as session:
        rows = list(await session.scalars(select(Appointment).where(Appointment.tenant_id == world.tenant.id)))
    assert [r.id for r in rows] == [world.appointment.id]


@pytest.mark.parametrize("path", ["appointments", "blocks"])
async def test_a_restricted_viewer_without_a_professional_has_no_agenda_to_create_on(
    client: AsyncClient, db, acting, path
):
    await _anas_world(db, acting)
    view_as(AgendaViewer("own", None))

    response = await client.post(f"{CALENDAR}/{path}", json=_create_body())

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "no_own_agenda"
    assert FakeCalendar.instances == {}


async def test_a_clinic_viewer_may_name_any_active_doctor_of_the_clinic(
    client: AsyncClient, db, acting
):
    world = await _anas_world(db, acting)
    beto = await add_professional(db, world.tenant.id, "Dr. Beto")
    view_as(AgendaViewer("clinic", None))  # a receptionist

    named = await client.post(f"{CALENDAR}/appointments", json=_create_body(professional_id=str(beto)))
    unnamed = await client.post(f"{CALENDAR}/appointments", json=_create_body())

    assert named.status_code == 201 and unnamed.status_code == 201
    assert (await _created(db, named.json()["id"])).professional_id == beto
    assert (await _created(db, unnamed.json()["id"])).professional_id is None  # today's behaviour


@pytest.mark.parametrize("which", ["inactive", "foreign", "unknown"])
async def test_a_clinic_viewer_cannot_name_a_doctor_outside_the_clinic(
    client: AsyncClient, db, acting, other_tenant, which
):
    world = await _anas_world(db, acting)
    view_as(AgendaViewer("clinic", None))
    if which == "inactive":
        target = await add_professional(db, world.tenant.id, "Dr. Antigo", active=False)
    elif which == "foreign":
        target = await add_professional(db, other_tenant.id, "Dr. Outra Clínica")
    else:
        target = uuid4()

    response = await client.post(f"{CALENDAR}/blocks", json=_create_body(professional_id=str(target)))

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_professional"
    assert FakeCalendar.instances == {}


async def test_a_restricted_doctor_cannot_hand_his_appointment_to_another_doctor(
    client: AsyncClient, db, acting
):
    world = await _anas_world(db, acting)
    beto = await add_professional(db, world.tenant.id, "Dr. Beto")
    view_as(AgendaViewer("own", world.appointment.professional_id))

    response = await client.post(
        f"{CALENDAR}/appointments/{world.appointment.id}/edit",
        json={"professional_id": str(beto)},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "professional_not_allowed"
    row = await reload_appointment(db, world.appointment.id)
    assert row.professional_id == world.appointment.professional_id
    assert FakeCalendar.instances == {} and sent() == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pyt tests/test_hub_agenda_scope_actions.py -q`
Expected: FAIL — the restricted doctor gets 200/409/422 on another doctor's appointment; create answers 201 with `professional_id` None for him and 201 on another doctor's id (the body field does not exist yet: pydantic ignores it).

- [ ] **Step 3: Resolve every id through the viewer**

In `src/secretaria/api/hub/calendar.py`, replace the whole `_get_appointment` function with:

```python
async def _get_appointment(
    session: AsyncSession, tenant: Tenant, appointment_id: str, viewer: AgendaViewer
) -> Appointment:
    """Load an appointment by id, scoped to the tenant AND to what `viewer` may see.

    404 for a malformed id, another clinic's appointment and - TASK-044 R7 (spec
    2026-10-09 §5.A) - an appointment a viewer restricted to his own agenda does not
    see (another doctor's, or one without a doctor): the SAME body in every case, so a
    restricted doctor cannot even learn that the id exists. Every route that takes an
    appointment id goes through here BEFORE touching Google, the queue or the patient.
    """
    try:
        appt_uuid = UUID(appointment_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found") from None
    appt = await session.scalar(
        select(Appointment).where(
            Appointment.id == appt_uuid,
            Appointment.tenant_id == tenant.id,
        )
    )
    if appt is None or not viewer.sees(appt.professional_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found")
    return appt
```

Then, in each of the seven routes `cancel_preview`, `cancel_appointment`, `release_appointment`, `message_patient`, `reschedule_appointment`, `update_appointment_status` and `edit_appointment`: add the parameter line

```python
    viewer: AgendaViewer = Depends(get_agenda_viewer),
```

right after its `tenant: Tenant = Depends(get_current_tenant),` line, and replace `await _get_appointment(session, tenant, appointment_id)` with `await _get_appointment(session, tenant, appointment_id, viewer)`. Check that none is left: `grep -n "_get_appointment(session, tenant, appointment_id)" src/secretaria/api/hub/calendar.py` must print nothing, and `grep -c "_get_appointment(session, tenant, appointment_id, viewer)" src/secretaria/api/hub/calendar.py` must print `7`.

- [ ] **Step 4: The doctor of a created row, and the hand-over guard**

In `src/secretaria/schemas/calendar.py`, add to `AppointmentCreate` (after `patient_id`) and to `BlockCreate` (after `description`) — `UUID` is already imported there for `AppointmentEdit` (Task 11); if not, add `from uuid import UUID`:

```python
    # TASK-044 R7 (owner 2026-10-09): whose agenda the new row belongs to. A viewer
    # restricted to his own agenda may omit it (= his own) or send his own; a
    # clinic-wide viewer may name any active professional of the clinic or none.
    professional_id: UUID | None = None
```

In `src/secretaria/api/hub/calendar.py`, right after the `_detail` function, add:

```python
# TASK-044 R7 (spec 2026-10-09 §5.A + owner's decision 2026-10-09): a viewer
# restricted to his own agenda creates and keeps appointments only on it.
_PROFESSIONAL_NOT_ALLOWED = (
    "professional_not_allowed",
    "Você só pode manter a consulta na sua própria agenda. Peça à recepção para trocar o médico.",
)
_CREATE_ON_OTHER_AGENDA = (
    "professional_not_allowed",
    "Você só pode marcar consultas e bloqueios na sua própria agenda.",
)
_NO_OWN_AGENDA = ("no_own_agenda", "Seu usuário não está ligado a um profissional da clínica.")
_UNKNOWN_PROFESSIONAL = ("unknown_professional", "Este profissional não pertence a esta clínica.")


async def _creation_professional(
    session: AsyncSession, tenant: Tenant, viewer: AgendaViewer, requested: UUID | None
) -> UUID | None:
    """The doctor a hub-created consultation/block belongs to. Runs BEFORE Google.

    Restricted viewer: nothing sent or his own id -> his own (the row is in his own
    view at once); another id -> 403 professional_not_allowed; no professional of
    his own -> 422 no_own_agenda (he has no agenda to write on). Clinic-wide viewer:
    None stays None (today's doctor-less booking); an id must be an ACTIVE
    professional of THIS clinic, else 422 unknown_professional.
    """
    if viewer.restricted:
        if viewer.professional_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, _detail(*_NO_OWN_AGENDA))
        if requested is not None and requested != viewer.professional_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, _detail(*_CREATE_ON_OTHER_AGENDA))
        return viewer.professional_id
    if requested is None:
        return None
    professional = await session.scalar(
        select(Professional).where(
            Professional.id == requested,
            Professional.tenant_id == tenant.id,
        )
    )
    if professional is None or not professional.is_active:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, _detail(*_UNKNOWN_PROFESSIONAL))
    return professional.id
```

In `create_appointment` and in `create_block`: add `viewer: AgendaViewer = Depends(get_agenda_viewer),` after the `tenant` parameter, make this the first statement of the body (before `_get_calendar`):

```python
    # Spec §5.A / owner 2026-10-09: decided before Google is touched.
    professional_id = await _creation_professional(session, tenant, viewer, body.professional_id)
```

and add `professional_id=professional_id,` to the `Appointment(...)` constructor of each route (after `tenant_id=tenant.id,`). Nothing else in the two routes changes (the Google event still goes to the clinic calendar; the 201 body is the same `AppointmentRead`).

In `edit_appointment`, right after `appt = await _get_appointment(session, tenant, appointment_id, viewer)`, add:

```python
    if (
        viewer.restricted
        and "professional_id" in body.model_fields_set
        and body.professional_id != viewer.professional_id
    ):
        # Spec §5.A: a doctor who sees only his own agenda never writes on a colleague's.
        raise HTTPException(status.HTTP_403_FORBIDDEN, _detail(*_PROFESSIONAL_NOT_ALLOWED))
```

- [ ] **Step 5: Run the tests (every hub calendar suite)**

Run: `pyt tests/test_hub_agenda_scope_actions.py tests/test_hub_status_guards.py tests/test_hub_staff_confirm_notice.py tests/test_hub_attended_post_consult.py tests/test_hub_edit_appointment.py tests/test_hub_cancel_portal_notice.py tests/test_hub_release.py tests/test_hub_staff_message.py tests/test_hub_calendar_confirmation.py tests/test_hub_calendar_money.py tests/test_cancellation_notice.py tests/test_reminder_v2_hub_wiring.py -q`
Expected: PASS (every older suite runs clinic-wide through the conftest default; their create calls send no `professional_id`, so their rows keep `professional_id` None exactly as before).

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff format tests/test_hub_agenda_scope_actions.py && uvx ruff check --fix tests/test_hub_agenda_scope_actions.py
uvx ruff check src/secretaria/api/hub/calendar.py src/secretaria/schemas/calendar.py
git diff --stat
git add src/secretaria/api/hub/calendar.py src/secretaria/schemas/calendar.py tests/test_hub_agenda_scope_actions.py
git commit -m "feat(hub): appointment routes refuse what a restricted doctor cannot see; he creates only on his own agenda (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 16: The extra reminder on the hub configuration, replanned on change

**Files:**
- Modify: `src/secretaria/services/reminder_schedule.py` (append `CustomReplan`, `_move_pending_custom`, `_retire_pending_custom`, `replan_custom_reminders`; imports)
- Modify: `src/secretaria/schemas/config.py` (`TenantConfigUpdate.reminder_extra_lead_minutes`; `TenantConfigRead.reminders_v2_enabled/reminder_extra_lead_minutes`)
- Modify: `src/secretaria/services/hub_configuration.py` (`TENANT_SCALAR_FIELDS`, `apply_tenant_config`, `tenant_read_model`; imports)
- Test: `tests/test_reminder_lead_replan.py`, `tests/test_hub_config_reminder_lead.py`

**Interfaces:**
- Consumes: R1 `_as_utc`, `_arm`, `_WARN_AFTER`, `MAX_CONFIRMATIONS`, `schedule_reminders`; `LIVE_APPOINTMENT_STATUSES`; Task 2's configuration wiring.
- Produces:
  - `reminder_schedule.CustomReplan(moved: int = 0, cancelled: int = 0, created: int = 0)` (frozen dataclass); `async reminder_schedule.replan_custom_reminders(session, tenant, *, now: datetime) -> CustomReplan` (flushes, never commits).
  - `TenantConfigRead.reminders_v2_enabled: bool`, `TenantConfigRead.reminder_extra_lead_minutes: int | None`; `TenantConfigUpdate.reminder_extra_lead_minutes: int | None` (`1500..20160`, `null` = off).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reminder_lead_replan.py`:

```python
"""A new extra-reminder lead replans the still-pending extra reminders (TASK-044 R7, §5.B)."""

# ruff: noqa: F811

from datetime import timedelta

from sqlalchemy import select

from secretaria.models import Appointment, AppointmentReminder, Tenant
from secretaria.services import reminder_schedule
from secretaria.services.reminder_schedule import CustomReplan, _as_utc
from tests._reminder_fixtures import NOW, db, make_appointment, other_tenant, tenant  # noqa: F401


async def _set_tenant(db, tenant_id, **fields) -> None:
    async with db() as session:
        row = await session.get(Tenant, tenant_id)
        for name, value in fields.items():
            setattr(row, name, value)
        await session.commit()


async def _plan(db, tenant_id, appointment_id) -> None:
    async with db() as session:
        clinic = await session.get(Tenant, tenant_id)
        appointment = await session.get(Appointment, appointment_id)
        await reminder_schedule.schedule_reminders(session, appointment, clinic, now=NOW)
        await session.commit()


async def _rows(db, appointment_id, kind: str) -> list[AppointmentReminder]:
    async with db() as session:
        return list(
            await session.scalars(
                select(AppointmentReminder)
                .where(
                    AppointmentReminder.appointment_id == appointment_id,
                    AppointmentReminder.kind == kind,
                )
                .order_by(AppointmentReminder.created_at)
            )
        )


async def _row(db, appointment_id, kind: str) -> AppointmentReminder:
    rows = await _rows(db, appointment_id, kind)
    assert len(rows) == 1, rows
    return rows[0]


async def _replan(db, tenant_id, lead: int | None) -> CustomReplan:
    async with db() as session:
        clinic = await session.get(Tenant, tenant_id)
        clinic.reminder_extra_lead_minutes = lead
        result = await reminder_schedule.replan_custom_reminders(session, clinic, now=NOW)
        await session.commit()
        return result


async def test_a_new_lead_moves_the_pending_extra_reminder_and_nothing_else(db, tenant):
    start = NOW + timedelta(days=10)
    appointment = await make_appointment(db, tenant, start_at=start)
    await _plan(db, tenant.id, appointment.id)
    day_before = await _row(db, appointment.id, "day")
    custom_before = await _row(db, appointment.id, "custom")

    result = await _replan(db, tenant.id, 2880)

    custom = await _row(db, appointment.id, "custom")
    assert result == CustomReplan(moved=1)
    assert custom.id == custom_before.id and custom.status == "pending"
    assert _as_utc(custom.due_at) == start - timedelta(minutes=2880)
    assert _as_utc(custom.warn_due_at) == start - timedelta(minutes=2880) + timedelta(hours=2)
    day = await _row(db, appointment.id, "day")
    assert (day.id, _as_utc(day.due_at)) == (day_before.id, _as_utc(day_before.due_at))


async def test_switching_the_extra_reminder_off_cancels_it(db, tenant):
    appointment = await make_appointment(db, tenant, start_at=NOW + timedelta(days=10))
    await _plan(db, tenant.id, appointment.id)

    result = await _replan(db, tenant.id, None)

    custom = await _row(db, appointment.id, "custom")
    assert result == CustomReplan(cancelled=1)
    assert custom.status == "cancelled"
    assert custom.invalidated_at is not None and custom.warn_due_at is None
    assert (await _row(db, appointment.id, "day")).status == "pending"


async def test_a_new_time_already_past_cancels_instead_of_sending_late(db, tenant):
    appointment = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    await _plan(db, tenant.id, appointment.id)  # 5-day lead: due tomorrow

    result = await _replan(db, tenant.id, 8640)  # 6 days: due = now

    assert result == CustomReplan(cancelled=1)
    assert (await _row(db, appointment.id, "custom")).status == "cancelled"


async def test_an_extra_reminder_already_sent_stays_as_history(db, tenant):
    appointment = await make_appointment(db, tenant, start_at=NOW + timedelta(days=10))
    await _plan(db, tenant.id, appointment.id)
    sent_row = await _row(db, appointment.id, "custom")
    async with db() as session:
        row = await session.get(AppointmentReminder, sent_row.id)
        row.status = "sent"
        await session.commit()

    result = await _replan(db, tenant.id, 2880)

    custom = await _row(db, appointment.id, "custom")
    assert result == CustomReplan()
    assert custom.status == "sent" and _as_utc(custom.due_at) == _as_utc(sent_row.due_at)


async def test_turning_it_on_adds_it_only_where_a_plan_already_exists(db, tenant):
    await _set_tenant(db, tenant.id, reminder_extra_lead_minutes=None)
    planned = await make_appointment(db, tenant, start_at=NOW + timedelta(days=10))
    await _plan(db, tenant.id, planned.id)  # day + hour only
    never_planned = await make_appointment(db, tenant, start_at=NOW + timedelta(days=12))

    result = await _replan(db, tenant.id, 2880)

    assert result == CustomReplan(created=1)
    custom = await _row(db, planned.id, "custom")
    assert custom.status == "pending" and custom.with_prompt is True
    assert _as_utc(custom.due_at) == NOW + timedelta(days=10) - timedelta(minutes=2880)
    assert await _rows(db, never_planned.id, "custom") == []  # left to the reconcile cron


async def test_another_clinic_and_a_switched_off_clinic_are_untouched(db, tenant, other_tenant):
    start = NOW + timedelta(days=10)
    mine = await make_appointment(db, tenant, start_at=start)
    theirs = await make_appointment(db, other_tenant, start_at=start)
    await _plan(db, tenant.id, mine.id)
    await _plan(db, other_tenant.id, theirs.id)

    await _replan(db, tenant.id, 2880)

    assert _as_utc((await _row(db, theirs.id, "custom")).due_at) == start - timedelta(minutes=7200)

    await _set_tenant(db, tenant.id, reminders_v2_enabled=False)
    result = await _replan(db, tenant.id, 4320)

    assert result == CustomReplan()
    assert _as_utc((await _row(db, mine.id, "custom")).due_at) == start - timedelta(minutes=2880)
```

Create `tests/test_hub_config_reminder_lead.py`:

```python
"""The extra reminder on the hub configuration (TASK-044 R7, spec §5.B; R5 Tasks 9-10 shape)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402

import pytest  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.models import Appointment, AppointmentReminder, Tenant  # noqa: E402
from secretaria.services import reminder_schedule  # noqa: E402
from tests._reminder_fixtures import db, make_appointment, tenant  # noqa: E402, F401

CONFIG = "/tenants/me/config"
CONFIGURATION = "/tenants/me/configuration"


@pytest.fixture(autouse=True)
def _override(db, tenant):  # noqa: F811
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant.id)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _stored(db, tenant_id) -> tuple[bool, int | None]:  # noqa: F811
    async with db() as session:
        row = await session.get(Tenant, tenant_id)
        return row.reminders_v2_enabled, row.reminder_extra_lead_minutes


async def test_get_config_exposes_the_switch_and_the_lead(client: AsyncClient, tenant):  # noqa: F811
    body = (await client.get(CONFIG)).json()

    assert body["reminders_v2_enabled"] is True
    assert body["reminder_extra_lead_minutes"] == 7200


async def test_the_configuration_save_changes_the_lead_and_echoes_it(
    client: AsyncClient, db, tenant  # noqa: F811
):
    response = await client.put(
        CONFIGURATION, json={"tenant": {"reminder_extra_lead_minutes": 2880}}
    )

    assert response.status_code == 200, response.text
    assert response.json()["tenant"]["reminder_extra_lead_minutes"] == 2880
    assert await _stored(db, tenant.id) == (True, 2880)


async def test_null_switches_it_off_on_the_legacy_save_too(client: AsyncClient, db, tenant):  # noqa: F811
    response = await client.put(CONFIG, json={"reminder_extra_lead_minutes": None})

    assert response.status_code == 200
    assert response.json()["reminder_extra_lead_minutes"] is None
    assert await _stored(db, tenant.id) == (True, None)


@pytest.mark.parametrize("value", [1440, 1499, 20161, 0, -5, "dois dias"])
async def test_out_of_range_is_422_and_nothing_changes(
    client: AsyncClient, db, tenant, value  # noqa: F811
):
    response = await client.put(
        CONFIGURATION, json={"tenant": {"reminder_extra_lead_minutes": value}}
    )

    assert response.status_code == 422
    assert await _stored(db, tenant.id) == (True, 7200)


@pytest.mark.parametrize("value", [1500, 20160])
async def test_the_bounds_themselves_are_accepted(client: AsyncClient, db, tenant, value):  # noqa: F811
    response = await client.put(
        CONFIGURATION, json={"tenant": {"reminder_extra_lead_minutes": value}}
    )

    assert response.status_code == 200
    assert await _stored(db, tenant.id) == (True, value)


async def test_the_switch_is_never_written_by_the_hub(client: AsyncClient, db, tenant):  # noqa: F811
    response = await client.put(
        CONFIGURATION,
        json={"tenant": {"reminders_v2_enabled": False, "persona_notes": "Gentil"}},
    )

    assert response.status_code == 200
    assert await _stored(db, tenant.id) == (True, 7200)


async def test_a_saved_lead_moves_the_planned_extra_reminder_in_the_same_save(
    client: AsyncClient, db, tenant  # noqa: F811
):
    now = datetime.now(UTC)
    start = (now + timedelta(days=10)).replace(second=0, microsecond=0)
    appointment = await make_appointment(db, tenant, start_at=start)
    async with db() as session:
        clinic = await session.get(Tenant, tenant.id)
        row = await session.get(Appointment, appointment.id)
        await reminder_schedule.schedule_reminders(session, row, clinic, now=now)
        await session.commit()

    response = await client.put(
        CONFIGURATION, json={"tenant": {"reminder_extra_lead_minutes": 2880}}
    )

    assert response.status_code == 200
    async with db() as session:
        custom = await session.scalar(
            select(AppointmentReminder).where(
                AppointmentReminder.appointment_id == appointment.id,
                AppointmentReminder.kind == "custom",
            )
        )
    assert reminder_schedule._as_utc(custom.due_at) == start - timedelta(minutes=2880)


async def test_a_save_that_keeps_the_lead_does_not_replan(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    calls: list[int] = []

    async def _spy(*args, **kwargs):
        calls.append(1)
        return reminder_schedule.CustomReplan()

    monkeypatch.setattr(reminder_schedule, "replan_custom_reminders", _spy)

    await client.put(CONFIGURATION, json={"tenant": {"reminder_extra_lead_minutes": 7200}})
    await client.put(CONFIGURATION, json={"tenant": {"persona_notes": "Gentil"}})

    assert calls == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pyt tests/test_reminder_lead_replan.py tests/test_hub_config_reminder_lead.py -q`
Expected: FAIL — `ImportError: cannot import name 'CustomReplan'` and `KeyError: 'reminders_v2_enabled'`.

- [ ] **Step 3: Implement the replan**

In `src/secretaria/services/reminder_schedule.py`: add `from dataclasses import dataclass` to the standard-library imports, `from sqlalchemy.exc import IntegrityError` after the `from sqlalchemy import ...` line, and change `from secretaria.models import Appointment, AppointmentStatus, Tenant, is_live_status` to `from secretaria.models import LIVE_APPOINTMENT_STATUSES, Appointment, AppointmentStatus, Tenant, is_live_status`. Append at the end of the module:

```python
# --- TASK-044 R7 (spec 2026-10-09 §5.B): the clinic changed its extra-reminder lead ----

_PLANNED_KINDS = (REMINDER_KIND_CUSTOM, REMINDER_KIND_DAY, REMINDER_KIND_HOUR)


@dataclass(frozen=True)
class CustomReplan:
    """What `replan_custom_reminders` did (counts only - logged, never shown)."""

    moved: int = 0
    cancelled: int = 0
    created: int = 0


async def _move_pending_custom(
    session: AsyncSession, reminder_id: UUID, tenant_id: UUID, due: datetime
) -> int:
    """Re-arm one still-pending row at `due`; 0 when the engine claimed it meanwhile."""
    result = await session.execute(
        update(AppointmentReminder)
        .where(
            AppointmentReminder.id == reminder_id,
            AppointmentReminder.tenant_id == tenant_id,
            AppointmentReminder.status == REMINDER_STATUS_PENDING,
            AppointmentReminder.invalidated_at.is_(None),
        )
        .values(
            due_at=due,
            warn_due_at=due + _WARN_AFTER[REMINDER_KIND_CUSTOM],
            warned_at=None,
            warn_kind=None,
        )
        .execution_options(synchronize_session="fetch")
    )
    return result.rowcount or 0


async def _retire_pending_custom(
    session: AsyncSession, reminder_id: UUID, tenant_id: UUID, now_utc: datetime
) -> int:
    """Cancel + invalidate one still-pending row; 0 when the engine claimed it meanwhile."""
    result = await session.execute(
        update(AppointmentReminder)
        .where(
            AppointmentReminder.id == reminder_id,
            AppointmentReminder.tenant_id == tenant_id,
            AppointmentReminder.status == REMINDER_STATUS_PENDING,
            AppointmentReminder.invalidated_at.is_(None),
        )
        .values(status=REMINDER_STATUS_CANCELLED, warn_due_at=None, invalidated_at=now_utc)
        .execution_options(synchronize_session="fetch")
    )
    return result.rowcount or 0


async def replan_custom_reminders(
    session: AsyncSession, tenant: Tenant, *, now: datetime
) -> CustomReplan:
    """Put the clinic's NEW `reminder_extra_lead_minutes` on the reminders already planned.

    Call AFTER `tenant.reminder_extra_lead_minutes` holds the new value, inside the
    configuration save's transaction (flushes, never commits). For every live future
    appointment of the clinic that has a patient:

    * its `pending` `custom` row of the CURRENT start is re-armed in place at the new
      due time, or cancelled + invalidated when the lead was switched off or the new
      due time is not in the future (never sent late - same rule as schedule_reminders);
    * a row that already left (`sending`/`sent`/`failed`/`skipped`) stays as history;
    * with a lead and no `custom` row, one is created - only when the appointment
      already has a planned `day`/`hour` row. An appointment with no plan at all is
      left to reminder_hooks.reconcile_missing_reminders, which plans every kind with
      the new lead (creating only `custom` there would make the cron skip it and lose
      its day/hour reminders).

    A clinic with the switch off has nothing planned and is left alone. Appointments
    are locked before their reminders (R1's rule); conditional UPDATEs never move a row
    the engine claimed meanwhile; a row a concurrent booking hook created first wins.
    """
    if not tenant.reminders_v2_enabled:
        return CustomReplan()
    now_utc = _as_utc(now)
    lead = tenant.reminder_extra_lead_minutes
    lead_delta = timedelta(minutes=lead) if lead is not None and lead > 0 else None

    appointments = list(
        await session.scalars(
            select(Appointment)
            .where(
                Appointment.tenant_id == tenant.id,
                Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
                Appointment.patient_id.is_not(None),
                Appointment.start_at > now_utc,
            )
            .with_for_update()
        )
    )
    if not appointments:
        return CustomReplan()
    rows_by_appointment: dict[UUID, list[AppointmentReminder]] = {}
    for row in await session.scalars(
        select(AppointmentReminder).where(
            AppointmentReminder.tenant_id == tenant.id,
            AppointmentReminder.appointment_id.in_([a.id for a in appointments]),
            AppointmentReminder.kind.in_(_PLANNED_KINDS),
            AppointmentReminder.invalidated_at.is_(None),
        )
    ):
        rows_by_appointment.setdefault(row.appointment_id, []).append(row)

    moved = cancelled = created = 0
    for appointment in appointments:
        start = _as_utc(appointment.start_at)
        current = [
            row
            for row in rows_by_appointment.get(appointment.id, [])
            if _as_utc(row.appointment_start_at) == start
        ]
        custom = next((row for row in current if row.kind == REMINDER_KIND_CUSTOM), None)
        due = start - lead_delta if lead_delta is not None else None
        if due is None or due <= now_utc:
            if custom is not None and custom.status == REMINDER_STATUS_PENDING:
                cancelled += await _retire_pending_custom(session, custom.id, tenant.id, now_utc)
            continue
        if custom is not None:
            if custom.status == REMINDER_STATUS_PENDING and _as_utc(custom.due_at) != due:
                moved += await _move_pending_custom(session, custom.id, tenant.id, due)
            continue
        if not current:
            continue  # never planned: the reconcile cron plans every kind with the new lead
        row = AppointmentReminder(
            tenant_id=tenant.id,
            appointment_id=appointment.id,
            patient_id=appointment.patient_id,
            kind=REMINDER_KIND_CUSTOM,
            appointment_start_at=start,
            channel=REMINDER_CHANNEL_WHATSAPP,
        )
        _arm(row, due=due, with_prompt=appointment.confirmation_count < MAX_CONFIRMATIONS)
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            continue  # a concurrent booking hook planned it first
        created += 1

    logger.info(
        "custom_reminders_replanned",
        tenant_id=str(tenant.id),
        moved=moved,
        cancelled=cancelled,
        created=created,
    )
    return CustomReplan(moved=moved, cancelled=cancelled, created=created)
```

- [ ] **Step 4: Implement the configuration**

In `src/secretaria/schemas/config.py`, inside `TenantConfigUpdate`, right after the `paid_notices_auto_approved: bool | None = None` line (Task 2), add:

```python
    # TASK-044 R7 (spec 2026-10-09 §5.B): the clinic's extra ("custom") reminder, in
    # minutes before the appointment. More than the 1-day reminder (> 1440; 1500 keeps
    # them at least an hour apart) and at most 14 days. Absent = untouched; explicit
    # null = no extra reminder. A change replans the still-pending extra reminders
    # (services/reminder_schedule.py::replan_custom_reminders). `reminders_v2_enabled`
    # is deliberately NOT accepted here: the owner turns reminders on per clinic
    # (spec 4.5) - a PUT that sends it is ignored like any unknown key.
    reminder_extra_lead_minutes: int | None = Field(default=None, ge=1500, le=20160)
```

In `TenantConfigRead`, right after `paid_notices_auto_approved: bool = False` (Task 2), add:

```python
    # TASK-044 R7: the reminder switch (READ-ONLY here) and the extra reminder's lead
    # (None = no extra reminder). Defaulted so an older reader never 500s.
    reminders_v2_enabled: bool = False
    reminder_extra_lead_minutes: int | None = None
```

In `src/secretaria/services/hub_configuration.py`: add `from datetime import UTC, datetime` below `from __future__ import annotations`, and add `reminder_schedule` to the services imports as `from secretaria.services import reminder_schedule` (a separate line after `from secretaria.services import tenant_config as cfg`). Append `"reminder_extra_lead_minutes",` as the last entry of `TENANT_SCALAR_FIELDS` (after `"paid_notices_auto_approved",`). In `apply_tenant_config`, insert as the first statement of the body (before `for field_name in TENANT_SCALAR_FIELDS:`):

```python
    previous_lead = tenant.reminder_extra_lead_minutes
```

and append at the end of the function (after the `is_active` block):

```python
    # TASK-044 R7 (spec §5.B): a NEW extra-reminder lead moves the reminders already
    # planned, in this same transaction - a rolled-back save rolls the replan back too.
    if (
        "reminder_extra_lead_minutes" in data
        and tenant.reminder_extra_lead_minutes != previous_lead
    ):
        await reminder_schedule.replan_custom_reminders(session, tenant, now=datetime.now(UTC))
```

In `tenant_read_model`, right after the `paid_notices_auto_approved=...` line (Task 2), add:

```python
        reminders_v2_enabled=bool(tenant.reminders_v2_enabled),
        reminder_extra_lead_minutes=tenant.reminder_extra_lead_minutes,
```

- [ ] **Step 5: Run the tests (reminder and config suites included)**

Run: `pyt tests/test_reminder_lead_replan.py tests/test_hub_config_reminder_lead.py tests/test_hub_config_paid_notices.py tests/test_hub_config.py tests/test_hub_config_pix.py tests/test_hub_configuration.py tests/test_reminder_schedule.py tests/test_reminder_v2_hooks.py tests/test_reminder_v2_engine.py -q`
Expected: PASS.

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff format tests/test_reminder_lead_replan.py tests/test_hub_config_reminder_lead.py
uvx ruff check --fix tests/test_reminder_lead_replan.py tests/test_hub_config_reminder_lead.py
uvx ruff check src/secretaria/services/reminder_schedule.py src/secretaria/schemas/config.py src/secretaria/services/hub_configuration.py
git diff --stat
git add src/secretaria/services/reminder_schedule.py src/secretaria/schemas/config.py src/secretaria/services/hub_configuration.py tests/test_reminder_lead_replan.py tests/test_hub_config_reminder_lead.py
git commit -m "feat(reminders): extra-reminder lead on the hub config, replanned on change (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 17: Full validation, checkpoint and the Meta templates note

**Files:**
- Create: `docs/CHECKPOINT_lembretes_r7.md`
- Modify: `docs/LEMBRETES_MODELOS_META.md` (append a section)
- Modify: `CLAUDE.md` (one pointer line under "## Documentação")

**Interfaces:**
- Consumes: everything above.
- Produces: the record R5's plan update and the owner read; no code.

- [ ] **Step 1: Run the whole suite and the lint of every touched file**

```bash
pyt -q -p no:cacheprovider 2>&1 | tail -5
uvx ruff check src/secretaria/services/staff_patient_message.py src/secretaria/services/appointment_release.py src/secretaria/services/clinic_action_notice.py src/secretaria/services/staff_appointment_edit.py src/secretaria/services/appointment_edit_write.py src/secretaria/services/reminder_schedule.py src/secretaria/services/reminder_hooks.py src/secretaria/services/appointment_status.py src/secretaria/services/patient_context.py src/secretaria/services/hub_configuration.py src/secretaria/schemas/calendar.py src/secretaria/schemas/config.py src/secretaria/api/hub/calendar.py src/secretaria/workers/confirmation_warnings.py src/secretaria/workers/shared/appointment_edit_apply.py src/secretaria/models/appointment_reminder.py src/secretaria/models/tenant.py src/secretaria/models/appointment.py src/secretaria/core/subscription.py src/secretaria/services/agenda_visibility.py src/secretaria/api/hub/deps.py
```

Expected: all tests pass. For any failure, run the same test on a clean `origin/main` checkout with the same command; a failure that also happens there is pre-existing (name it in the checkpoint with that proof), anything else is a regression to fix before continuing. Write the printed totals (passed / failed / skipped) into the checkpoint in Step 2.

- [ ] **Step 2: Write the checkpoint**

Create `docs/CHECKPOINT_lembretes_r7.md` with exactly these sections, filling the two bracketed values with what Step 1 printed and the commit list from `git log --oneline origin/main..HEAD`:

```markdown
# CHECKPOINT — Lembretes R7: ações da clínica na agenda avisam o paciente (TASK-044)

Spec: `docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md`.
Plano: `docs/superpowers/plans/2026-10-09-lembretes-r7-avisos-acoes-clinica.md`.

## Estado

- Branch `task/TASK-044-lembretes-r7-avisos` (worktree `C:\TECH\BRAIN-worktrees\TASK-044\secretarIA`), commits locais: [lista do `git log --oneline origin/main..HEAD`].
- Suíte completa: [totais do Passo 1]. Falhas pré-existentes (provadas em `origin/main`): nenhuma, ou a lista com a prova.
- **Não mesclado, não pushado, não deployado. Nenhum modelo da Meta criado ou submetido. Migração não aplicada em banco real.**

## O que entrou e onde (âncoras estáveis)

- Config da clínica: `Tenant.paid_notices_auto_approved` (migração `d8e3a5c1f7b2`), lido/gravado em `schemas/config.py::TenantConfigRead/TenantConfigUpdate` e `services/hub_configuration.py::TENANT_SCALAR_FIELDS` → `GET /tenants/me/config`, `PUT /tenants/me/configuration`, `PUT /tenants/me/config`.
- Envio único dos avisos de ação: `services/staff_patient_message.py::send_clinic_notice` (+ `NoticeResult`, `paid_notice_authorised`, vocabulário `NOTICE_*`, agora definido aqui e reexportado por `appointment_release`).
- Cartões da clínica: `models/appointment_reminder.py` (`staff_confirm`, `staff_edit`, `REMINDER_KINDS_UNPLANNED`), `services/reminder_schedule.py::ensure_staff_notice_row/current_staff_notice_row/retire_staff_notice_row`; reconcile (`services/reminder_hooks.py::reconcile_missing_reminders`) e aviso à clínica (`workers/confirmation_warnings.py::due_candidates`) os ignoram. O toque é o fluxo R6 sem mudança (`workers/shared/reminder_actions.py::handle_reminder_button`).
- Textos e regras dos avisos: `services/clinic_action_notice.py` (`notify_staff_confirmation`, `notify_attended`, `notify_staff_edit`, `buttons_allowed`).
- PATCH status: `services/appointment_status.py::staff_transition` + `api/hub/calendar.py::update_appointment_status`.
- Pós-consulta: `Appointment.post_consult_notified_at` (mesma migração); `services/patient_context.py::find_post_consult_followup` ignora consulta já avisada.
- Editar/Remarcar: `POST …/edit` → `services/staff_appointment_edit.py::apply_staff_edit` → `services/appointment_edit_write.py::write_appointment_edit` (o mesmo gravador do "Alterar Dados" R6) + outbox R6 (`record_professional_edit`, `enqueue_professional_edit_notification`) + `reminder_hooks.after_appointment_rescheduled`.
- Cancelar: `services/appointment_release.py::notify_cancelled_patient` (Portal no chat + e-mail; WhatsApp = o job de sempre).
- R4: liberar e mensagem usam `paid_notice_authorised`; liberar devolve `whatsapp_link`.
- Quem vê a agenda (spec §5.A): `core/subscription.py::SubscriptionClaim` (`professional_id`, `agenda_scope` da introspecção do brain-api), `services/agenda_visibility.py` (`AgendaViewer`, `viewer_from_claim` — regra de rollout que fecha por segurança), `api/hub/deps.py::get_agenda_viewer`, `GET /calendar/viewer`; `api/hub/calendar.py::list_events` (filtro + `mine`), `_get_appointment(..., viewer)` em todas as rotas por id; `_creation_professional` em `POST /appointments` e `POST /blocks` (médico restrito cria só na própria agenda: sem médico = ele, outro médico = 403 `professional_not_allowed`, sem médico próprio = 422 `no_own_agenda`; recepção pode nomear um médico ativo da clínica) e `professional_not_allowed` no Editar/Remarcar.
- Dados do Editar na agenda (spec §5.C): `schemas/calendar.py::CalendarEventRead` (`professional_id`, `professional_name`, `service`, `attendee_name`, `phone`, `patient_channel`).
- Lembrete extra (spec §5.B): `schemas/config.py` (`reminders_v2_enabled` só leitura, `reminder_extra_lead_minutes` 1500..20160), `services/hub_configuration.py::apply_tenant_config` → `services/reminder_schedule.py::replan_custom_reminders`.

## Contrato para o R5

O contrato exato (campos, códigos, status) está na seção "Produces for R5 — exact wire shapes" do plano; o R5 deve copiá-la, não reinterpretá-la.

## Decisões onde a spec silenciou

As 22 decisões estão na seção "Decisions taken where the spec is silent" do plano (configuração única, linhas `staff_*`, um aviso de confirmação por horário, botões só quando funcionam, tabela de transições, marcador da pós-consulta, nome = `attendee_name`, telefone com DDI, encaixe com `allow_overlap`, e-mail ao médico só data/hora/médico, cancelamento WhatsApp idêntico ao de hoje, `/message` e `/reschedule` inalterados, envio síncrono na API; e, da segunda rodada: papel decidido ao vivo pelo brain-api, regra de rollout, eventos sem consulta e consultas sem médico ocultos ao médico restrito, médico restrito cria consulta/bloqueio só na própria agenda e não passa a consulta adiante (decisão do dono de 2026-10-09), "Só os meus" é filtro e não permissão, limites 1500..20160 do lembrete extra, reprogramação só dos extras pendentes já planejados, dados do Editar na mesma consulta).

## Modelos da Meta (documentados, NÃO criados)

Fora das 24 h, até a Meta aprovar modelos com botões, confirmar/alterar vão pelo modelo pago de uma variável `REMINDER_TEMPLATE_NAME`, sem botões. Os modelos propostos estão em `docs/LEMBRETES_MODELOS_META.md` (seção R7).

## Deploy (quando o dono autorizar)

1. `alembic upgrade head` (imagem nova; adiciona as duas colunas, aditivo).
2. brain-api com `agenda_scope` (`brain-api/docs/superpowers/plans/2026-10-09-hub-token-papel-profissional.md`). Sem ele, a regra de rollout vale: quem está ligado a um médico vê só o próprio (gestor-médico sem seletor) — é o sintoma de ordem trocada.
3. `secretaria_api` **e** `secretaria-worker` juntos (`GET /build` → `deploy_parity=match`): o worker lê o reconcile novo, o filtro dos avisos à clínica, o follow-up da pós-consulta e o gravador do "Alterar Dados".
4. Front (R5 atualizado) depois da API.

## Pendências

- O plano R5 (`docs/superpowers/plans/2026-10-03-lembretes-r5-front-agenda.md`) já consome este contrato (atualizado em 2026-10-09); se algo do contrato mudou na execução, atualizar o R5 antes de executá-lo.
- Modelos da Meta da seção R7 (decisão e submissão do dono).
- Prova real em clínica de teste (Portal e WhatsApp, dentro e fora das 24 h) após deploy autorizado.
- **Próximo plano (não escrito, decidido pelo dono em 2026-10-09) — console de conversas** (spec §5.D): o médico vê só as conversas dos próprios pacientes; o gestor (também quando é médico) alterna "Todos" / "Só os meus"; a secretária vê todas. Hoje `/tenants/me/conversations` (e as listas `GET /doctor/appointments` / `GET /doctor/patients` do brain-api) mostram a clínica inteira a qualquer médico; o R7 não muda isso.
- O front (R5) precisa dos três estados de criação do médico restrito: médico travado nele, 403 `professional_not_allowed`, 422 `no_own_agenda` (contrato no plano).
- Consultas que vivem na agenda Google própria de um profissional continuam fora da agenda do hub (a agenda lê só a da clínica — comportamento de hoje, mantido por decisão do dono).
```

- [ ] **Step 3: Document the Meta templates R7 would need**

Append to `docs/LEMBRETES_MODELOS_META.md`:

```markdown
## R7 — avisos das ações da clínica (2026-10-09) — PROPOSTA, não submetida

Hoje (sem modelo aprovado) o aviso fora das 24 h vai pelo modelo de uma variável `REMINDER_TEMPLATE_NAME`, só texto. Para os botões funcionarem fora das 24 h seriam necessários dois modelos UTILITY (pt_BR), cada um com três respostas rápidas na ordem **Confirmar / Cancelar / Alterar Dados** (o payload é posicional: `remconfirm|<id>`, `remcancel|<id>`, `remedit|<id>`, o mesmo do lembrete):

| Nome proposto | Corpo | Variáveis |
|---|---|---|
| `clinica_confirmou_v1` | `Seu médico confirmou {{1}} de {{2}} às {{3}} com {{4}}. Você está ciente?` | 1 = "sua consulta" / "a consulta de <nome>", 2 = dd/mm/aaaa, 3 = HH:MM, 4 = médico |
| `clinica_alterou_v1` | `A clínica alterou {{1}}: {{2}}. Agora: {{3}} às {{4}} com {{5}}.` | 1 = como acima, 2 = mudanças numa linha ("Data: 16/10 → 17/10; Horário: 14:00 → 15:30"), 3–5 = como acima |

Pós-consulta e cancelamento não precisam de modelo novo (texto sem botões e `CANCEL_TEMPLATE_NAME`, respectivamente). Depois de aprovados, o código precisa de um ajuste pequeno em `services/staff_patient_message.py::_clinic_notice_whatsapp` (nome do modelo + variáveis + `button_payloads`, atrás de uma flag `*_APPROVED`, como o `REMINDER_V2_TEMPLATE_APPROVED`).
```

- [ ] **Step 4: Point to the checkpoint**

In `CLAUDE.md`, under `## Documentação`, add as the first entry:

```markdown
`docs/CHECKPOINT_lembretes_r7.md` — TASK-044 R7: ações da clínica na agenda avisam o paciente (confirmar com botões, compareceu → pós-consulta, Editar/Remarcar, cancelar no Portal, autorização permanente de aviso pago), quem vê a agenda por papel (brain-api `agenda_scope`, aplicado no servidor), lembrete extra na configuração e dados do Editar na agenda; migração aditiva `d8e3a5c1f7b2`; local, não mesclado/pushado/deployado; deploy: migração → brain-api → API+worker → front; contrato do R5 no plano.
```

- [ ] **Step 5: Commit**

```bash
git diff --stat
git add docs/CHECKPOINT_lembretes_r7.md docs/LEMBRETES_MODELOS_META.md CLAUDE.md
git commit -m "docs(reminders): R7 checkpoint, Meta template proposal, pointer (TASK-044)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Hand back**

Report to the Integrator/owner (in plain language, per `AI_WORKFLOW.md` "Como falar com o dono"): what now happens for the patient after each clinic action, who sees which appointments in the agenda (a doctor limited to his own agenda books and blocks only on it and cannot pass an appointment to a colleague), that the conversations console is still clinic-wide for every doctor and waits for its own plan, that the extra reminder is now configurable, that nothing was deployed or sent to Meta, that brain-api must go live before this, the two Meta templates waiting for a decision, and that the agenda uses none of this until the R5 front plan (already written against "Produces for R5") is executed and deployed after this. Push, merge and deploy only on the owner's explicit request.

