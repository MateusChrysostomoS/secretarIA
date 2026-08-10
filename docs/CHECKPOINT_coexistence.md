# CHECKPOINT — WhatsApp Coexistence onboarding (Tasks 4 + 5)

Built 2026-08-09. **The secretaria side of Coexistence was already largely done**
before this round — see `docs/CHECKPOINT_onboarding_multiprofessional.md`'s
"Webhook Coexistence signals" section for the full prior state:

- All four Coexistence-relevant webhook fields were already processed in
  `workers/tasks.py` (`process_webhook_event` fans out by `change.field`):
  `messages` (inbound patients), `smb_message_echoes` (human-secretary echo →
  handover), `history` (chat-history sync progress only, LGPD-safe), and
  `smb_app_state_sync` (contact/app-state sync, signal only, LGPD-safe).
- Human handover itself (`HandoverManager`, `_persist_human_echo` flipping
  `Conversation.handover_state` to `HUMAN_ACTIVE`) was already built.
- The `check_handover_timeouts` cron (hands the conversation back to the bot
  after `HANDOVER_TIMEOUT_MINUTES` of silence) was already built.
- `_mark_mode_resolved` (setting `tenants.mode_resolved_at` the first time
  ANY of the three Coexistence signals arrives) was already built.

**What landed in THIS round** — a real-number test-window allowlist, plus a
docstring fix, both scoped to Tasks 4 and 5 of the "WhatsApp Coexistence
onboarding" prompt:

## Task 4 — `BOT_ALLOWLIST_WA_IDS` (test-window allowlist)

**Motivation:** in Coexistence, the webhook receives EVERY 1:1 message the
linked number sends or receives — not just clinic patients, but the owner's
own personal contacts too — plus an echo of everything the owner sends from
the WhatsApp Business app. Without a guard, testing with a real, already-used
number would greet random personal contacts as if they were clinic patients
and turn them into `Patient` rows.

- `config.py::Settings.BOT_ALLOWLIST_WA_IDS: str = ""` + `@property
  bot_allowlist_wa_ids -> frozenset[str]`. Same CSV-parsing shape as
  `CORS_ALLOW_ORIGINS`/`cors_origins` right above `HANDOVER_TIMEOUT_MINUTES`:
  each entry is quote-stripped then reduced to digits-only
  (`"".join(filter(str.isdigit, cleaned))`), empty entries dropped. Tolerates
  a human-pasted `"+55 (11) 99999-8888"` matching wa_id `5511999998888`.
  **Empty (the default) means no restriction — production behavior is
  unchanged.**
- Two guards in `workers/tasks.py`, both read `get_settings().bot_allowlist_wa_ids`
  fresh per call (no extra caching beyond the existing `lru_cache`d
  `get_settings()`):
  - `_persist_inbound_message` — guard sits immediately after the
    `tenant.is_active` check, BEFORE the `Patient`/`ConsentEvent`/
    `Conversation` are created and BEFORE `action_button` is ever attached to
    a `_ReplyContext`. A non-allowlisted `wa_id` returns `None`: no reply
    (not even `service_unavailable`), no Patient, no Conversation, no
    ConsentEvent. The `ProcessedEvent` row inserted earlier in the same
    transaction is kept as-is — the event WAS seen and is being discarded on
    purpose, not lost to a retry.
  - `_persist_human_echo` — guard sits AFTER `_mark_mode_resolved(tenant)`
    but BEFORE `_get_or_create_patient`. `_mark_mode_resolved` runs first on
    purpose: the echo is still proof Coexistence resolved for the tenant even
    when the echoed conversation itself gets discarded for being
    off-allowlist. Both statements run inside the same `async with
    session.begin():` block, whose `__aexit__` commits on any normal exit
    from the block — including an early `return` — so the `mode_resolved_at`
    write is not lost when the guard fires afterward.

### Coverage verification (read, not assumed)

- **`transcribe_audio_message`** (audio transcription job) calls
  `_persist_inbound_message` directly (no `action_button`/`greeting_button`
  passed) → fully covered by the new guard, same as any other inbound path.
- **`_handle_action_button`** (dispatched from `_send_bot_reply`, which only
  runs when `_persist_inbound_message` returns non-`None`): `action_button`
  is decoded by the caller (`_handle_patient_messages`) but only attached to
  a `_ReplyContext` **inside** `_persist_inbound_message`, at the
  reminder-action-button short-circuit — which sits AFTER the new allowlist
  guard. A non-allowlisted tap therefore never reaches
  `_handle_action_button`: the function returns `None` before `action_button`
  is ever read. A short comment was added at that short-circuit noting the
  upstream guard already covers it.
- **`_handle_history` / `_handle_smb_app_state_sync`** — read, confirmed
  neither sends any outbound message or creates a `Patient`/`Conversation`;
  both only update tenant-level sync-status columns and call
  `_mark_mode_resolved`. No allowlist guard needed there — there is nothing
  patient-identity-shaped to gate.

## Task 5 — webhook docstring

`api/webhook.py`'s module docstring previously listed only two of the four
processed webhook fields (`messages`, `smb_message_echoes`). Updated to list
all four, one line each: `messages`, `smb_message_echoes`, `history`,
`smb_app_state_sync` (matching the descriptions used in `README.md`'s
"Connecting the Meta webhook" section, also updated for consistency).
Doc-only — no behavior change.

## Status

- **BUILT.** Tests green (see below). **NOT deployed.**
- No migration — `BOT_ALLOWLIST_WA_IDS` is an env var, not a schema change.

## Tests

New file `tests/test_bot_allowlist.py` (12 tests): pure `bot_allowlist_wa_ids`
parsing (empty, CSV, formatted-number normalization, quote-stripping,
blank-entry tolerance) + DB-backed guard behavior for both
`_persist_inbound_message` and `_persist_human_echo` (empty allowlist =
unchanged; off-allowlist = dropped, `ProcessedEvent` still written, and for
the echo path `mode_resolved_at` still set; on-allowlist = normal flow;
duplicate redelivery of a dropped event still dedupes).

Full suite: `uv run python -m pytest` (via the base-interpreter + PYTHONPATH
fallback — the venv's `python.exe` is blocked by Windows App Control on this
machine; a uv-managed `cpython-3.12` matching the venv's ABI was used
instead) — **1117 passed, 1 failed**. The one failure
(`tests/test_human_backup_plugin.py::test_on_inbound_inside_hours_returns_false`)
is a pre-existing, unrelated wall-clock boundary flake (business hours
`00:00`–`23:59` "today" vs. the moment the test runs) — confirmed by
`git stash`-ing every change from this round and re-running that single test
against the unmodified baseline, where it fails identically.

## External dependencies (not part of this round, pending before a live test)

- **EasyPanel:** set `BOT_ALLOWLIST_WA_IDS` to the real test number(s) for
  the duration of the Coexistence test window; consider a shorter
  `HANDOVER_TIMEOUT_MINUTES` (e.g. `3`) during that same window so a stalled
  test conversation hands back to the bot quickly.
- **Meta App Dashboard:** confirm all four webhook fields are subscribed
  (`messages`, `smb_message_echoes`, `history`, `smb_app_state_sync`) and the
  verify token matches `META_VERIFY_TOKEN`.

## Pending

- Live validation with a real, Coexistence-eligible number has not been run
  (out of scope for this round — code + tests only).
