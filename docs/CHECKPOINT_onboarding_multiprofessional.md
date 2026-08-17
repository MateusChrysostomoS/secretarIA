# CHECKPOINT — Onboarding & multi-professional configuration

> See also `docs/CHECKPOINT_plugins.md` — the `multi_professional` addon (CRUD,
> agent tools, entitlement gating) landed there first; this round deepens it
> (own encrypted Calendar credential, own hours/services, own context message)
> and adds the onboarding provisioning/runtime/notification legs around it.

Built 2026-07-18 against cross-service contract v1 (`CONTRACT_onboarding_v1.md`).
**brain-api is the onboarding state-machine owner** (`onboarding_state` /
`blocker_reason` / anchors / timestamps all live on brain-api's `tenants`
table — see brain-api's own checkpoint for that side). This repo implements
the secretaria-side legs: provisioning, per-professional config/completeness,
per-professional agent runtime, Coexistence webhook signals, transactional
email, and the onboarding-nudge + usage-metering crons. The implementing
session reported **~688 tests passing** (`uv run python -m pytest`); this
documentation pass verified the code below against the contract directly but
did **not** re-run the suite itself — see Honesty notes for why.

## Schema (migration `f06e85b476f2`, revises `a4b5c6d7e8f9`)

- `tenants.phone_number_id` — `NOT NULL UNIQUE` → **nullable**, unique
  constraint replaced by a **partial** unique index
  (`uq_tenants_phone_number_id_not_null`, `postgresql_where phone_number_id
  IS NOT NULL`). Lets onboarding create a tenant row before its WhatsApp
  number is connected, while still preventing two tenants from claiming the
  same connected number.
- `tenants` += `connected_at`, `mode_resolved_at`, `history_sync_status`
  (`none|in_progress|done`, default `none`), `history_synced_at`, `address`
  (JSON), `insurances` (JSON list), `collect_insurance` (bool, default
  false) — all additive/nullable-or-defaulted, no backfill needed.
- `professionals` += `specialty`, `about`, `context_doctor_message`,
  `business_hours` (JSON), `appointment_types` (JSON) — NULL means "fall back
  to the tenant's legacy column" (`services/tenant_config.py`'s
  `professional_business_hours` / `professional_appointment_types`).
- New table `professional_credentials` (PK `professional_id`, FK CASCADE):
  `google_refresh_token_encrypted`, mirroring `tenant_credentials` one level
  down — additive, never replaces the tenant-level credential.
- **Data backfill** (same migration): every tenant with zero professionals
  gets exactly one (`name=clinic_name`, `is_active=true`); each tenant's
  earliest professional (by `created_at`, tie-broken by `id`) has the
  tenant's current `business_hours`/`appointment_types` copied onto it
  wherever its own column is still NULL. `tenants.business_hours` /
  `.appointment_types` are left untouched as the permanent legacy fallback.

## Internal provisioning surface (`api/internal_provisioning.py` + `services/provisioning.py`)

Six endpoints under `/internal`, same `require_internal_api_key` gate as
`api/internal.py` (split into its own module purely for size, mirroring how
`api/internal_privacy.py` split off the LGPD routes — not re-registered
under `api/internal.py` itself):

| endpoint | behavior |
|---|---|
| `POST /internal/tenants` | idempotent create-by-caller-supplied-id (`provision_tenant`); an existing id returns unmodified, `created=false` |
| `POST /internal/tenants/{id}/whatsapp-connection` | `connect_whatsapp` — 404 unknown tenant, 409 `phone_number_conflict`; `waba_id`/`access_token` only written when truthy (a retry omitting them never wipes a stored value); `connected_at` set only once |
| `GET /internal/tenants/{id}/config-status` | `get_config_status` — wraps `services/tenant_config.py::professional_completeness` (below) |
| `POST /internal/tenants/{id}/professionals` | `create_or_attach_professional` — case-insensitive match on an existing ACTIVE professional's name, else creates one |
| `POST /internal/tenants/{id}/activate` | `activate_tenant` — professional-aware completeness gate AND `phone_number_id is not None`; already-active short-circuits to `(True, [])` without re-validating (this endpoint only ever turns activation ON) |
| `POST /internal/notifications/email` | enqueues the `send_transactional_email` arq job; `queued=false` (never an error) when the arq pool is unreachable |

Services never raise `HTTPException` — "not found" is a `None` return, the
API layer maps sentinels to status codes; every service function takes an
open session and does not commit (API layer commits once).

## Per-professional completeness & partial activation (`services/tenant_config.py`)

- `professional_completeness(session, tenant)` — per ACTIVE professional,
  `has_calendar` (own `professional_credentials` token OR the tenant-level
  one), `has_hours`, `has_services` (both via the same fallback-to-tenant
  helpers the runtime loader uses). Threshold rule
  (`settings.PARTIAL_ACTIVATION_THRESHOLD`, default 10): `total_active <=
  10` → every active professional must be complete (zero professionals is
  never complete); `total_active > 10` → one complete professional is
  enough, so a large roster doesn't block go-live on its slowest member.
- `can_activate_professional_aware(session, tenant)` — `config_complete` AND
  any Calendar connected (tenant- or professional-level); does **not** check
  `phone_number_id` (that's `activate_tenant`'s job — a WhatsApp fact, not
  config-completeness).
- The **original** `can_activate(tenant, calendar_connected)` is UNCHANGED
  and still the gate behind `api/hub/config.py`'s tenant-level `PUT
  /tenants/me/config` `is_active:true` path — two parallel gates by design,
  not a refactor of one into the other.
- `professional_completeness_item` — same per-row computation for ANY single
  professional regardless of `is_active`, used by the hub list view
  (`api/hub/professionals.py`) so a freshly created/updated row shows its
  own completeness immediately.

## Per-professional agent runtime

- `TenantRuntimeConfig` (`services/tenant_config.py`) gains `professional_id`
  / `context_doctor_message` / `specialty` / `about`. `load_tenant_config`
  populates them **only** when the tenant has exactly one active
  `Professional`: `business_hours`/`appointment_types`/`google_calendar_id`/
  `google_refresh_token` all resolve THROUGH that professional (own
  value/credential, falling back to the tenant's) instead of the tenant
  columns directly. Zero or multiple active professionals leave the base
  config at tenant-level, unchanged.
- `ai/prompts.py::_format_professional_context` renders a "SOBRE O
  PROFISSIONAL" block (specialty + about + context_doctor_message) —
  explicitly framed as context to use, not a script to recite. Empty when
  none of the three fields are set (multi-professional tenants get nothing
  here; see below). **2026-07-22 corrections round:** `secretary_system_prompt`
  now injects this right after the hardcoded, unconditional
  `_format_safety_rules` block (heading "REGRAS INEGOCIÁVEIS DE SEGURANÇA E
  CONDUTA") instead of a persona-notes section — the clinic-editable
  `persona_notes` free-text override was removed from `TenantRuntimeConfig`
  and from this prompt entirely (the `Tenant.persona_notes` column and the
  hub `TenantConfigRead`/`Update` API field survive for historical data, but
  nothing under `ai/` reads it anymore — see `services/tenant_config.py` and
  `ai/prompts.py`'s module docstrings).
- `plugins/multi_professional.py` (the addon itself predates this round —
  `docs/CHECKPOINT_plugins.md`) — `_professional_calendar` now resolves
  **per professional, not just per tenant**: own encrypted
  `professional_credentials` token first (tenant's as fallback), own
  `business_hours`/`appointment_types` JSON first (tenant's as fallback, via
  the same helpers `professional_completeness` uses), and its own first
  active service's duration as the default slot length. Once a professional
  is resolved by name, `_with_professional_context` attaches its
  `context_doctor_message` to the tool result under `professional_context` —
  how the LLM "loads" that professional's context mid-turn.

## Hub additions

- `api/hub/oauth.py` — `GET
  /tenants/me/professionals/{id}/calendar/oauth/start` (signed `state` now
  carries `tenant_id` **and** `professional_id`) and `POST
  .../calendar/disconnect`. The **same** `/oauth/google/callback` serves
  both flows: when `state` carries a `professional_id`, the refresh token
  routes to `set_professional_google_refresh_token` instead of the
  tenant-level column; ownership (professional still belongs to the tenant)
  is re-checked at callback time, not just at `/start`. Per-professional
  disconnect does **not** force `is_active=False` on anything (unlike the
  tenant-level disconnect) — the partial-activation rule may still cover the
  tenant via another professional or the tenant-level credential.
- `api/hub/professionals.py` — `GET` list now returns completeness
  (`has_calendar`/`has_hours`/`has_services`/`complete`) per row; `PUT
  /{id}/config` (business_hours, appointment_types, specialty, about,
  context_doctor_message, google_calendar_id) is **never** gated by
  entitlements/limits — same "config save is always allowed" principle as
  the tenant-level config PUT. Only creating/activating a professional
  (`is_active` false→true) checks the `multi_professional` addon +
  `limits["professionals"]`, fail-closed 503 on an entitlement-fetch failure.
- `api/hub/config.py` — `PUT /tenants/me/config` gains `address`,
  `insurances`, `collect_insurance` as plain scalar fields
  (`_SCALAR_FIELDS`); confirmed the `can_activate` 422 gate is nested under
  `if data.get("is_active") is True` only — a config-only save (no
  `is_active` in the body) never touches that gate, so it can't block saving
  config before the number is connected.

## Webhook Coexistence signals (`workers/tasks.py`)

> See also `docs/CHECKPOINT_coexistence.md` — the real-number test-window
> allowlist (`BOT_ALLOWLIST_WA_IDS`) that guards `_persist_inbound_message`
> and `_persist_human_echo` below landed later, on top of this section.

- `_handle_history` (field `history`): tracks WhatsApp's Coexistence
  chat-history sync progress ONLY — `history_sync_status` flips
  `none→in_progress` on the first chunk, `→done` (+`history_synced_at`) once
  any chunk signals completion (`history_item_is_final`, defends against an
  unknown/renamed shape). **No message content is ever read or stored** —
  only a chunk count is logged (LGPD).
- `_handle_smb_app_state_sync` (field `smb_app_state_sync`): business
  contact-list sync; contact names/numbers are never read/persisted/logged,
  only a count. `schemas/webhook.py::WebhookStateSyncItem` is the type-level
  enforcement of that.
- `_mark_mode_resolved(tenant)`: sets `mode_resolved_at=now` the first time
  ANY of the three Coexistence signals arrives (`history`,
  `smb_app_state_sync`, or the pre-existing `smb_message_echoes` handler) —
  a no-op once already set, shared by all three handlers.
- `_resolve_tenant`: the primary `phone_number_id` lookup is null-safe by
  construction (only queries when the value is truthy — a NULL column can
  never match a non-empty string in SQL). The legacy single-tenant
  auto-provision fallback is gated behind `settings.ALLOW_WEBHOOK_AUTOPROVISION`
  (default `False`); off means an unrecognized `phone_number_id` is simply
  dropped, never adopted. Bonus hardening found in code (not explicitly in
  the facts pack): `_mark_connected` sets `connected_at` on ANY webhook that
  resolves to a known tenant, as a backstop in case the internal
  whatsapp-connection endpoint (provisioning surface, above) was somehow
  skipped.
- Admin summaries (`api/admin/tenants.py`) and the outbound sending path
  (`services/whatsapp.py::WhatsAppClient.for_tenant`) were spot-checked:
  both already treat `phone_number_id` as `Optional`.
  **SUPERSEDED (PROMPT_FIX_21):** the sending path no longer falls back to the
  `META_PHONE_NUMBER_ID` / `META_ACCESS_TOKEN` env scaffold when `None` — that
  fallback could send one clinic's message from another clinic's WABA.
  `for_tenant` now raises `TenantWhatsAppCredentialMissing` before any HTTP
  call; the env scaffold survives only behind the explicit
  `WhatsAppClient.for_dev_scaffold()`. See
  `docs/CHECKPOINT_menu_rename_waba_lgpd.md`.

## Email (`services/email.py`)

New **transactional** path, independent of the pre-existing operational-alert
path (`send_calendar_alert` / `send_human_backup_alert`, unchanged, gated
only by `SMTP_HOST`):

- `send_transactional_email_message(to, template, variables)` — gated by its
  **own** `EMAIL_ENABLED` switch (default off) even when `SMTP_HOST` is
  already set for alerts; own `EMAIL_FROM_ADDRESS`/`EMAIL_FROM_NAME` identity
  (falls back to the alert path's `SMTP_FROM_EMAIL`/`SMTP_FROM_NAME`, then
  `SMTP_USERNAME`). Never raises — returns `False` on disabled/unconfigured/
  unknown-template/render-failure/send-failure, so a flaky SMTP server can
  never turn the `send_transactional_email` arq task into a retry loop.
- `_SafeDict` — a missing template variable renders as the literal
  `{placeholder}` text instead of raising (the `variables` dict is supplied
  by brain-api via `POST /internal/notifications/email`, a sibling service
  this module has no control over).
- All 10 templates confirmed present and pt-BR: `professional_invite`,
  `retry_nudge_atividade_insuficiente`, `retry_nudge_numero_em_outro_bsp`,
  `retry_nudge_sem_acesso_admin_waba`, `retry_nudge_sem_pagina_facebook`,
  `retry_nudge_outro`, `connection_success`, `config_reminder_pre_connection`,
  `config_reminder_connected`, `closing_email`. **+1 since (2026-07-22
  corrections round):** `test_window_expired` — see the Crons section below.

## Crons (`workers/onboarding_cron.py`, registered in `workers/arq_worker.py`)

- **`run_onboarding_nudges`** — hourly at `:10`. Pulls brain-api's onboarding
  tenant list once per sweep (`services/brain_onboarding.py::list_onboarding_tenants`,
  returns `None` on any ambiguity so the cron can tell "pull failed" apart
  from "legitimately empty"). Per tenant, computed in the tenant's OWN
  timezone (`Tenant.timezone`, default `America/Sao_Paulo`) via the pure
  functions in `workers/onboarding_cadence.py`:
  - Retry nudges: eligible states `{aquecimento, aguardando_elegibilidade}`,
    eligible blockers `{None, atividade_insuficiente, outro}` (manual-action
    blockers are excluded — those tenants need the doctor to act, not a
    nudge), `subscription_active`, not `retry_paused`, `next_retry_at` due,
    inside the send window. Cadence `RETRY_CADENCE_DAYS=[3,7,14,21,30]` then
    every `RETRY_BIWEEKLY_DAYS=14`, capped at `RETRY_WINDOW_TOTAL_DAYS=60`.
  - D+30 `manual_review_flagged` — posted once, NOT window-gated (it sends
    no message to anyone, only marks the tenant for ops follow-up).
  - D+60 `closing_email` — sent once, window-gated (it IS an outbound email).
  - Config reminders — independent eligibility (`config_status != 'completa'`,
    not `config_reminder_paused`), cadence `CONFIG_REMINDER_CADENCE_DAYS=[1,3,7]`
    then every `CONFIG_REMINDER_WEEKLY_DAYS=7`, **uncapped**. Template picked
    by state (`config_reminder_connected` vs `_pre_connection`).
  - **Test-window expiry email (added 2026-07-22, corrections round "Task
    2" — after this checkpoint was first written):** `_process_test_window`
    sends the one-shot `test_window_expired` template when brain-api reports
    `OnboardingTenant.test_window_email_due=True` — three new OPTIONAL item
    fields on the same `GET /internal/onboarding/tenants` response
    (`test_window_email_due: bool`, `test_window_days: int`,
    `test_window_restart_url: str`), all `.get()`-defaulted
    (`False`/`0`/`""`) in `services/brain_onboarding.py::_item_from_dict` so
    an older brain-api that hasn't shipped them yet is a silent no-op, never
    a parse failure. Unlike the retry/config-reminder families above, this
    step does NOT re-derive eligibility from `onboarding_state`/
    `blocker_reason`/`subscription_active` — it trusts brain-api's `due`
    verdict outright, deliberately including when `subscription_active` is
    already `False` (by the time the test window expires, brain-api has
    typically already auto-cancelled the Stripe subscription, and the email
    exists precisely to explain that). Still gated on the same `in_window`
    business-hours check as every other outbound owner email here. Posts
    `test_window_email_sent` back (one-shot server-side, same contract shape
    as `closing_email_sent`/`manual_review_flagged`). The full cross-service
    feature (brain-api's window computation, Stripe auto-cancel, restart
    flow) is documented in brain-api's own `CHECKPOINT_test_window.md` — this
    bullet covers only the secretaria-side send.
  - Every event send POSTs back to brain-api (`services/brain_onboarding.py::post_onboarding_event`,
    never raises) so brain-api's state machine advances `next_retry_at` /
    stamps the sent-at timestamps — this repo never derives or stores
    onboarding state itself.
- **`run_patient_usage_metering`** — daily at `03:30 UTC`. Iterates every
  LOCAL tenant (not brain-api's list, which deliberately excludes
  fully-onboarded tenants) with `subscription_active` (same entitlement read
  the reminders plugin uses). Two tallies per tenant, same loop:
  - `_tally_tenant_month` — distinct `(professional_id, patient_id)` pairs
    with ≥1 `Appointment` of ANY status in the tenant-local calendar month —
    **cancelled included** (meter-pricing round, 2026-07-19: a patient who
    booked and later cancelled still consumed the secretary's work that
    month; the old non-cancelled filter/`_BILLABLE_STATUSES` is gone). One
    idempotent usage event per pair via the existing
    `services/usage_events.py::emit_usage_event`
    (`event_id="bp:{tenant}:{professional}:{patient}:{YYYY-MM}"`, `feature=
    "billable_patients"`) — a daily re-run of the same month is free. A `NULL
    professional_id` pair is only attributed when the tenant has exactly one
    active professional (unambiguous); otherwise it's counted as
    `skipped_unattributed`, never guessed.
  - `_tally_active_professionals_month` — one event per ACTIVE
    `Professional` row (existence, not appointment activity;
    `event_id="prof:{tenant}:{professional}:{YYYY-MM}"`, `feature=
    "active_professionals"`, counter `professionals_emitted`). Backs the
    R$80/professional metered Stripe price on brain-api's side; no
    proration — active at ANY daily sweep during the month bills the full
    month, same philosophy as the patient tally. This is a point-in-time
    snapshot taken once per daily 03:30 UTC sweep, not continuous
    observation: a professional created AND deactivated again between two
    consecutive sweeps is never caught active by either one, so is never
    billed for that month (accepted sub-day blind spot — do not rely on a
    stronger guarantee than "active as of some sweep this month").
- Both crons wrap **each tenant** in its own try/except (one bad tenant never
  aborts the sweep) and never log `owner_email`/`owner_name` — only
  `tenant_id` and event/template names.

## Migrations added this round (chain)

`a4b5c6d7e8f9` (plugins round head) → **`f06e85b476f2`** (onboarding +
multi-professional config) ← head.

## Honesty notes

- **Test count not independently re-verified.** The implementing session
  reported ~688 passing (`uv run python -m pytest`). This documentation pass
  did not re-run the suite: `tests/conftest.py`'s DB-free isolation is
  confirmed only for the ASGITransport `client` fixture ("the app lifespan
  does NOT run under ASGITransport, so these tests need no Redis or Postgres
  connection") — model/service-level tests presumably hit a real Postgres,
  and `DATABASE_URL` defaults to `postgresql+asyncpg://…@localhost:5432/secretaria`
  (secretarIA's own `docker-compose.yml` maps its Postgres container to host
  port 5432). This environment has a separate, unrelated Postgres instance
  also occupying port 5432 in past sessions — running the suite blind risked
  hitting the wrong database rather than secretarIA's own docker container.
  State the count as reported, not re-verified.
- **Spec-internal edge, flagged in code, not solved:** a tenant with ≥11
  active professionals only needs ONE complete professional for
  `config_status` to read `completa` (the partial-activation threshold
  rule), which then silences config reminders for the REST of that tenant's
  still-unconfigured professionals — see
  `services/tenant_config.py::professional_completeness`'s docstring and
  `workers/onboarding_cron.py::_process_config_reminder`'s docstring (both
  call this out explicitly; nothing here resolves it).
- **Onboarding state ownership:** `onboarding_state` / `blocker_reason` /
  anchors / retry timestamps live ENTIRELY on brain-api's `tenants` table.
  This repo only reads that list and reports events back
  (`services/brain_onboarding.py`) — it never derives or stores onboarding
  state locally. See brain-api's own checkpoint doc for the state machine
  itself (`services/onboarding.py`, contract v1 §8).
- **This was not a green-field per-tenant-config round.** `TenantRuntimeConfig`,
  `load_tenant_config`, `secretary_system_prompt(config)`, and encryption at
  rest (`tenant_credentials`) already existed from the plugins round and
  earlier (`docs/CHECKPOINT_plugins.md`) — this round's actual delta is the
  **per-professional** layer on top, plus everything provisioning/webhook/
  email/cron-shaped described above. `CLAUDE.md`'s "Per-tenant config" /
  "What the agent needs to become" sections described this as still-open
  work; that was already stale before this round and has been corrected
  alongside this checkpoint (see below).
- **`multi_professional` addon predates this round.** Its CRUD/tools/
  entitlement-gating shipped in the plugins round (`docs/CHECKPOINT_plugins.md`);
  this round deepened the per-professional resolution (own Calendar
  credential, own hours/services, own context message) rather than
  introducing the addon.
- Everything flagged in `docs/CHECKPOINT_plugins.md`'s own Honesty notes
  (`REMINDER_TEMPLATE_NAME` must be Meta-approved; EHR/Pix are stub seams;
  the outbound WhatsApp rate limiter is still a TODO) is unrelated to and
  unchanged by this round — not re-verified here, still applicable.
- **2026-07-22 corrections round touched this checkpoint after the fact**
  (two small, targeted secretaria-side deltas on top of the onboarding round
  described above — this file was NOT rewritten for them, only amended
  inline where they touch a fact already documented here):
  - The clinic-editable `persona_notes` free-text prompt override was
    removed from `ai/prompts.py`/`TenantRuntimeConfig` and replaced by one
    hardcoded, unconditional safety/tone block
    (`ai/prompts.py::_format_safety_rules` — no diagnosis, urgency routes to
    pronto-socorro/192, cordial tone always, no promised outcomes/
    medication). See the "Per-professional agent runtime" section above.
  - The onboarding-nudges cron gained a third one-shot email step,
    `_process_test_window` / template `test_window_expired`, gated on a new
    brain-api-computed `test_window_email_due` flag (defensive parsing: an
    older brain-api without the field is a silent no-op). See the "Crons"
    section above; brain-api's `CHECKPOINT_test_window.md` is the main doc
    for this feature end-to-end.
