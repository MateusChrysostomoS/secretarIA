# CHECKPOINT — Pix deposit (sinal) add-on + appointment payment lifecycle

**Status (2026-07-22): BUILT + fully tested (suite 1011 green), NOT deployed.**
Migration `b06ff85998bf` pending deploy — the chain `32e87fcf926b` (post-consult) →
`e51cd84e1959` (flow_managing_appointment_id) → `b06ff85998bf` (this feature) is ALL
pending; one `alembic upgrade head` covers the three. External wiring pendências at the
end of this doc.

The `pix_deposit` add-on (R$79/mês, flat Stripe line item, entitlement key renamed from
`pix_whatsapp` pre-launch — brain-api `66d3872`, no live subscribers, no data migration)
charges a partial deposit ("sinal") via Pix when an appointment is booked, through the
**clinic's own Asaas account** (the clinic receives the money and pays the ~R$1.99 PSP
fee — never our platform cost, nothing metered). Deposit value = `pix_deposit_percent`
(default 30%) of the service price parsed from the free-text `appointment_types[].price`
(`services/payments/money.py::parse_brl_to_cents`; unparseable/absent price → NO deposit,
silent skip + log — locked product rule).

## State machine (`PixDeposit.status`, SEPARATE from `AppointmentStatus`)

One `pix_deposits` row per appointment (`appointment_id` UNIQUE — reschedule updates the
appointment row in place, so the deposit follows it with zero migration machinery).
Payment state is deliberately orthogonal to the outcome status: a no_show can be retido
or reembolsado depending on timing.

| status | meaning | entered by |
|---|---|---|
| `aguardando_sinal` | charge created, unpaid | `maybe_create_deposit` (post-booking hook) |
| `confirmado_pago` | deposit paid | Asaas webhook PAYMENT_RECEIVED/CONFIRMED |
| `cancelado_reembolsado` | cancelled EARLIER than `pix_refund_window_hours` → full refund fired | any cancel path |
| `cancelado_retido` | cancelled INSIDE the window → `pix_retention_policy` applied (`total`: no refund; `partial`: refund `pix_partial_refund_percent`%) | any cancel path |
| `no_show_retido` | doctor marked NO_SHOW with a paid deposit (bookkeeping only — money already with the clinic) | hub PATCH status |
| `expirado` | charge expired unpaid (Asaas same-day due date) or voided on unpaid cancel; slot freed | webhook PAYMENT_OVERDUE/DELETED, or cancel while unpaid |

Refund API failure → deposit STAYS `confirmado_pago` + ERROR log `pix_refund_failed`
(ids only) + outcome `refund_failed`; patient copy says the clinic will process the
estorno. Never pretend a refund happened.

## Where everything lives

- **Models**: `models/pix_deposit.py` (status enum + row), `models/processed_asaas_event.py`
  (webhook idempotency ledger, mirrors `processed_events`); `Tenant` gained the 6
  non-secret policy fields (`pix_deposit_enabled/pix_deposit_percent/pix_refund_window_hours/
  pix_retention_policy/pix_partial_refund_percent/pix_reschedule_limit`, defaults
  false/30/24/total/50/2); `TenantCredentials` gained `asaas_api_key_encrypted` +
  `asaas_webhook_token_encrypted` (Fernet, decrypt ONLY in `services/tenant_config.py`);
  `Patient.asaas_customer_id` (clinic-account customer id, not a secret).
- **Money brain**: `services/payments/deposit_lifecycle.py` — ALL transitions and ALL
  patient-facing pt-BR money copy. `maybe_create_deposit` (guards → Asaas customer →
  charge → QR → persist → send copia-e-cola; NEVER blocks a booking),
  `on_appointment_cancelled` (window/policy branching), `cancellation_notice` (honest
  copy per outcome — refunds "caem em poucos dias úteis", never "devolvido" instantly),
  `on_no_show`, `register_reschedule` (increment + limit), `apply_asaas_event` (worker
  core: per-tenant token auth via `hmac.compare_digest`, claim-in-same-transaction
  idempotency, transition, best-effort notifications + GCal delete on expiry).
- **Asaas client**: `services/payments/asaas.py` (async httpx, v3, per-clinic key,
  `ASAAS_BASE_URL`/`ASAAS_TIMEOUT_SECONDS` settings; errors never carry response bodies).
- **Webhook**: `api/webhook_asaas.py` `POST /webhooks/asaas` — dumb fast-ACK (malformed
  → 200 ignored; queue down → 503), enqueues `process_asaas_event`
  (`workers/payments_tasks.py`). Authenticity is verified in the WORKER (needs the
  per-tenant token from DB), unlike Meta's pure-HMAC in-handler check. Unknown payment →
  drop without claim; auth mismatch → drop without claim; unknown event types →
  claimed no-op (forward-compatible); duplicate event id → no-op.
- **Plugin**: `plugins/pix_deposit.py` post_booking hook (entitlement key `pix_deposit`
  gates the WHOLE lifecycle; `Tenant.pix_deposit_enabled` is the tenant-level switch —
  fail-closed both ways). Re-fetches rows in its own session (hook context is detached).
- **Reminders** (`plugins/reminders.py`): now CORE — gate is `summary.active` +
  `secretaria_enabled` only (see `CHECKPOINT_plugins.md` row + the basico quota flag).
  PAID-deposit appointments get 3 buttons: inside the Meta 24h window →
  `send_buttons`; outside → `REMINDER_DEPOSIT_TEMPLATE_NAME` HSM with quick-reply
  payloads (`WhatsAppClient.send_template(button_payloads=...)`), falling back to the
  plain template if the deposit template isn't approved yet (`deposit_template_fallback`
  log). Non-deposit reminders byte-identical to before.
- **Button family** (`schemas/webhook.py::extract_action_button`, precedent `slot|`):
  `apptconfirm|<id>` / `apptresched|<id>` / `apptcancel|<id>` / `apptcancelyes|<id>`,
  parsed from BOTH carriers (interactive `button_reply.id` and template quick-reply
  `button.payload`). Intercepted in `workers/tasks.py::_handle_action_button` BEFORE
  handover/flow/LLM routing (reminder replies arrive outside any flow context);
  tenant-scoped lookups, foreign ids → polite miss. Confirmar → `CONFIRMED` (no money);
  Reagendar → limit pre-check (at limit → keep-or-cancel buttons) else enters the
  existing manage-reschedule sub-flow preselected (`enter_manage_action(preselected_id=…)`);
  Cancelar with paid deposit inside the window → WARN FIRST (policy-specific copy) with
  `apptcancelyes`/`apptresched` buttons — never silently retain.
- **Money hooks on ALL FOUR cancel paths** + no-show + reschedule:
  flow cancel (`tasks.py::_apply_flow_result` cancel site), LLM tool
  (`ai/tools.py::_mark_appointment_cancelled` — returns the notice for the agent to
  relay), hub `POST /appointments/{id}/cancel`, hub `PATCH /appointments/{id}/status`
  (CANCELLED → `on_appointment_cancelled`, NO_SHOW → `on_no_show`). Flow-cancel confirm
  question is deposit-aware (warning injected at `tasks.py::_apply_deposit_awareness`,
  the single seam that also enforces the reschedule-limit entry pre-check — flow_router
  keeps its no-DB-I/O invariant). Reschedule completion increments
  `reschedule_count` (flow path); hub reschedule deliberately does NOT (doctor moves
  must not consume the patient's allowance) and its pre-existing bug of never writing
  `start_at`/`end_at` is FIXED.
- **Hub API**: `AppointmentRead.deposit_status`/`deposit_outcome`;
  config GET/PUT the 6 policy fields + READ-ONLY `asaas_connected`;
  `POST /internal/tenants/{id}/asaas-connection` {api_key, webhook_token} (internal
  channel — the PSP credential NEVER passes through the doctor-facing form, matching the
  WABA-token precedent). brain-frontend section: `c3b55f8`.

## External pendências (do NOT fabricate — nothing works end-to-end until these)

1. Stripe Prices + EasyPanel `STRIPE_PRICE_MAP` keys: `analytics_bi_advanced` (R$49),
   `pix_deposit` (R$79). Until then checkout 503s `price_not_configured:<key>`.
2. Deploy migrations (`alembic upgrade head` → `b06ff85998bf`).
3. Per clinic: Asaas account + API key + a generated webhook token, provisioned via the
   internal endpoint; register `https://<secretaria>/webhooks/asaas` in the clinic's
   Asaas panel with that same token as the auth header value.
4. Meta approval of the 3-quick-reply template (`REMINDER_DEPOSIT_TEMPLATE_NAME`,
   default `appointment_reminder_deposit`); until approved the code falls back to the
   plain reminder template.

## Flags / known gaps (deliberate, pending product decisions)

- ~~**basico reminders quota**~~ — RESOLVED 2026-07-22, same day as the tier collapse:
  `LIMIT_REMINDERS` is no longer a quota at all. It joined `billable_patients`/
  `active_professionals` as a third metering-only dimension of the fully-metered
  `secretaria_basico` plan (`STRIPE_METER_EVENT_REMINDERS`, companion price
  `secretaria_basico_metered_reminders`) — every plan grants `limits["reminders"]=0`
  (unlimited-by-quota) and each billable send is charged per-unit via the Stripe meter
  instead. See brain-api's `CONTRACTS.md` §13.3/§13.5 for the wiring.
- **RESCHEDULED is not remindable** (`_REMINDABLE_STATUSES` unchanged): after a
  reschedule, no further 24h/1h reminders fire — so the deposit's 3-button loop ends
  there. Pre-existing semantics, kept; revisit together with the hub-reschedule
  status model.
- Hub `PATCH /status` → CANCELLED still does NOT touch Google Calendar (pre-existing).
- QR code IMAGE is not sent (no media-send method on `WhatsAppClient`) — copia-e-cola
  only, which is sufficient for payment; QR image is future polish.
- `Tenant.pix_key` is now a dead column (old stub's field) — retained, drop in some
  future migration round.
- `tests/test_human_backup_plugin.py::test_on_inbound_inside_hours_returns_false` is
  pre-existing-flaky (the TEST computes "today" in UTC, plugin uses tenant tz; fails
  ~3h/day window). Untouched by this round.
