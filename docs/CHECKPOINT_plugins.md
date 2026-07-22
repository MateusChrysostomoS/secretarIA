# CHECKPOINT — Plugin architecture (entitlement-gated capabilities)

> See also `docs/CHECKPOINT_audio_transcricao.md` for inbound WhatsApp voice-note
> transcription, which lands on this same `workers/tasks.py` reply path
> (`_persist_inbound_message` / `_send_bot_reply`) via its own dedicated arq job.

Validated 2026-07-02 (`uv run python -m pytest` → 350 passed). This is the record of
the "add-ons as plugins" round: every capability beyond the ferro core is a plugin
toggled by the tenant's LIVE entitlement in brain-api — nothing is always-on.

## The core (tier "ferro") — was already real, now gated

The end-to-end loop (WhatsApp inbound → LangGraph agent → Google Calendar booking →
reply) existed and works per tenant (`ai/graph.py`, `services/calendar.py`,
`load_tenant_config`). This round added the two things it lacked:

- **Entitlement gate on the bot path** (`workers/tasks.py::_send_bot_reply`): one
  Redis-cached read of brain-api's entitlement summary per inbound message
  (`services/entitlements_client.py::get_entitlements` — fresh TTL
  `ENTITLEMENT_CACHE_TTL_SECONDS` = 300s, stale fallback 24h, fail-closed when neither
  exists). Not active / secretaria disabled → the bot stays silent (message still
  persisted).
- **Per-tenant WhatsApp token on the reply path**: `WhatsAppClient.for_tenant(tenant,
  waba_token)` with the token decrypted only in `services/tenant_config.get_waba_token`.

## The registry (`src/secretaria/plugins/`)

- `base.py::PluginSpec` — a capability declares `entitlement_keys` (any-of: addon ids
  or tiers) and its extension points: `agent_tools` (LangChain tools appended to the
  agent), `on_inbound` (pre-flow hook, first True short-circuits the bot), and
  `post_booking` (runs in a dedicated arq job AFTER a booking commits — off the hot
  path).
- `registry.py` — `enabled_plugins(summary)` / `agent_tools_for` / `run_on_inbound` /
  `run_post_booking`. Gating semantics mirror brain-api's `is_entitled` exactly
  (status active/trialing; addon flag from the normalized full keyset; cumulative
  tiers ferro < bronze_1 < bronze_2).
- The agent is cached PER CAPABILITY SET (`ai/graph.py::_AGENTS` keyed by frozenset of
  tool names) — a disabled add-on contributes nothing and costs nothing.

## The plugins

| plugin | enabled by | what it does |
|---|---|---|
| `reminders` | **core** — any ACTIVE secretarIA subscription (`summary.active` + `secretaria_enabled`; UNGATED 2026-07-22, was tier `bronze_1` OR addon `reactivation_pack`) | arq cron (every 5 min) sends 24h + 1h appointment reminders. Idempotent per appointment per window (`ProcessedEvent` key `reminder:{kind}:{appointment_id}`). Free text inside the Meta 24h window; approved HSM template (`REMINDER_TEMPLATE_NAME`) outside it + fail-open usage event (`feature="reminders"`) to brain-api `POST /internal/usage-events`. Honors `Patient.reminder_opt_out`. Appointments with a PAID Pix deposit get the 3-button variant (Confirmar/Reagendar/Cancelar) — see `docs/CHECKPOINT_pix_deposit.md`. NOTE: quota semantics were deliberately NOT changed — `secretaria_ferro` still grants `limits["reminders"]=0` in brain-api's catalog, so usage now accrues against 0 for ferro-only tenants (flagged, pending product decision). |
| `human_backup_24_7` | addon | inbound outside business hours → conversation flips to HUMAN_ACTIVE, ack text (`HUMAN_BACKUP_ACK_TEXT`), best-effort email alert. Empty business-hours config → never fires. |
| `multi_professional` | addon | `Professional` model (own optional Google calendar id, falls back to the tenant's); agent tools `list_professionals` / `list_free_slots_for_professional` / `create_event_for_professional`; hub CRUD `/tenants/me/professionals` with `limits["professionals"]` enforcement (409 on exceeding, 403 when not entitled, only the "add active row" transition is gated). |
| `multi_unit` | addon | `Unit` model; `list_units` / `create_event_at_unit` tools; optional `unit_name` on professional bookings; hub CRUD `/tenants/me/units` with `limits["units"]`. |
| `ehr` | addon | post-booking push through the `services/ehr` provider seam (`Tenant.ehr_provider` selects; `iclinic` is a logged STUB — Doctoralia/Memed/Conexa are future providers). |
| `pix_deposit` | addon (RENAMED from `pix_whatsapp` 2026-07-22, pre-launch, no subscribers; the old stub `services/payments/pix.py` + `Tenant.pix_key` path was deleted) | post-booking REAL Pix deposit via the clinic's own Asaas account: dynamic charge (30% of the parsed service price by default), copia-e-cola sent on WhatsApp, webhook-driven payment lifecycle, refund-window/retention policy on cancel, reschedule limit. Full write-up: `docs/CHECKPOINT_pix_deposit.md`. |
| `analytics_bi` | addon `analytics_bi` OR `analytics_bi_advanced` | records minimal non-personal `analytics_events` rows post-booking (any-of gate so an advanced-only tenant still accumulates rows); hub `GET /tenants/me/analytics/summary` (bookings totals / last-30d / by type), 403 when not entitled, 503 on entitlement-fetch failure. |
| `analytics_bi_advanced` | addon | hub `GET /tenants/me/analytics/advanced` — a superset of the summary over the SAME rows (90-day window, per-professional + per-source breakdowns, dense last-12-months trend). Fixed-price add-on; **distinct** key, does not grant the basic summary. 403 when not entitled, 503 on entitlement-fetch failure. No new plugin/recorder — the `analytics_bi` hook already records for this key too. |

## LGPD pieces that landed with this round

- `api/internal_privacy.py` (same `X-Internal-Api-Key` gate): `GET
  /internal/privacy/tenants/{tenant_id}/subjects/{wa_id}/export` and `DELETE
  .../subjects/{wa_id}` — brain-api's erasure orchestrator calls these. Erase
  hard-deletes messages/conversations/consent events + the patient row; appointments
  are ANONYMIZED (patient_id nulled, phone blanked), never deleted (operational
  agenda record). Idempotent; audit log carries `sha256(wa_id)`, never the raw id.
- `models/consent_event.py`: one `first_contact_service` event at patient creation
  (legal basis text is an explicit LAWYER TODO — see
  `PreCheck/docs/legal/pendencias_advogado.md`); the `reminder_opt_out`/`opt_in`
  convention is documented on the model.

## Migrations added this round (chain)

`d7e8f9a0b1c2` (encrypt WABA token, drops `tenants.access_token`) →
`e9f0a1b2c3d4` (`patients.reminder_opt_out`) →
`f1a2b3c4d5e6` (professionals, units, appointment FKs) →
`a4b5c6d7e8f9` (ehr_provider, pix_key, analytics_events, consent_events) ← head.

## Honesty notes (sandbox vs production)

- `REMINDER_TEMPLATE_NAME` must be a Meta-APPROVED utility template on the tenant's
  WABA before reminders work in production; nothing here creates/approves templates.
- EHR and Pix are seams with stub providers — sellable as "ready to integrate", not
  as live integrations.
- The outbound WhatsApp rate limiter (~5 msg/s cap) is still a TODO in
  `services/whatsapp.py`.
