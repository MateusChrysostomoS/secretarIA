# CHECKPOINT — Inbound WhatsApp audio transcription

Validated 2026-07-02 (`uv run python -m pytest -q` → 425 passed, 1 pre-existing
unrelated failure — see Pendências). This is the record of wiring inbound WhatsApp
**voice notes** through the `transcription-core` library into the exact same reply
path text messages already use.

## What was built

The webhook already scans every inbound event for `messages`/`smb_message_echoes`
ids (`iter_event_ids`) for its idempotency fast-path. This round adds a second,
audio-specific scan — `schemas/webhook.py::iter_audio_messages` — that walks the SAME
raw payload for `type == "audio"` messages, but ONLY under `field == "messages"`
changes (never `smb_message_echoes`: that field is the human secretary's own outbound
audio sent from the WhatsApp app via Coexistence, never something to transcribe). For
each voice note found, `api/webhook.py::receive_webhook` enqueues a dedicated arq job,
`transcribe_audio_message`, right alongside the existing `process_webhook_event`
enqueue. The job's payload is deliberately minimal (`media_id`, `phone_number_id`,
`wa_id`, `message_id`, `patient_name`) — never the full webhook body — keeping the
webhook handler itself unchanged in cost (light dict parsing + enqueue only).

`transcribe_audio_message` (`workers/tasks.py`), in order:

1. Inbound rate limit (`_is_rate_limited`) — the ONE increment for an audio message,
   since `_handle_patient_messages` now skips audio entirely (see below), so this is
   the single place that counts/checks it.
2. `ProcessedEvent` idempotency check, BEFORE any STT spend.
3. Resolves the tenant (`_resolve_tenant` — same MVP single-tenant auto-provision
   scaffold text messages use) and decrypts the WABA token via
   `services/tenant_config.get_waba_token` (the one decrypt seam, untouched).
4. Tenant not active yet → the exact same zero-STT-spend fallback as a text message:
   `_persist_inbound_message(..., body=None)`.
5. Builds a `transcription_core.TranscriptionConfig` from settings
   (`_transcription_config`) and calls `transcribe_whatsapp_media` — downloads the
   voice note from the Graph API and calls OpenAI/Groq STT, entirely inside
   transcription-core (stateless: no DB, no disk, no logging, no credential
   resolution of its own).
6. Permanent failure (`MediaTooLarge`, `NotAudio`) or a low-confidence/empty
   transcript (`TranscriptionResult.is_low_confidence`): the `ProcessedEvent` is
   claimed explicitly (`_mark_audio_event_processed`) and the patient gets
   `AUDIO_UNINTELLIGIBLE_MESSAGE` — the LLM is never invoked with a bad transcript.
7. Any other `TranscriptionError` (`MediaFetchError`, `AllProvidersFailed`): logged
   and re-raised so **arq retries** the job. The `ProcessedEvent` id is deliberately
   NOT claimed on this path, so the retry is safe and the (short-lived) media URL is
   fetched fresh on the next attempt.
8. Success: `_persist_inbound_message(..., body=result.text)` — EXACTLY the function
   the text path uses — then `_send_bot_reply` if it returns a reply. The transcript
   is persisted only as a normal `Message.body`, through the pre-existing path; there
   is no new persistence path, and the audio bytes themselves are never persisted
   anywhere (transcription-core's own invariant, preserved end to end).

`_handle_patient_messages` now skips any message with `type == "audio"` and a truthy
`audio.id` (the code comment explains why this interlock matters: letting it fall
through would let the quiet `body=None` path claim the `ProcessedEvent` id first,
which would make the transcript arriving later look like a duplicate and get
silently dropped). Audio WITHOUT a media id — a shape Meta shouldn't send, but the
parser stays defensive — still falls through to today's quiet `body=None` handling,
unchanged.

The worker's shared `httpx.AsyncClient` (`ctx["http_client"]`, created in
`arq_worker.on_startup`, closed in `on_shutdown`) is reused for both the WhatsApp
media download and the STT call; `transcribe_audio_message` also tolerates running
without one (owned-client fallback, opened and closed per call) — the path the test
suite exercises.

## Key design points

- **Echo skip is structural, not a runtime check.** `iter_audio_messages` only scans
  `field == "messages"` changes, so `smb_message_echoes` audio is never even seen by
  the parser, let alone transcribed.
- **Idempotency.** Exactly one of two places claims the `ProcessedEvent` row for an
  audio wamid: `_persist_inbound_message` on success (same seam as text), or
  `_mark_audio_event_processed` on a permanent failure / low confidence. A transient
  failure claims nothing, so an arq retry is always safe and STT is never paid for
  twice on the same wamid.
- **No audio persistence.** Bytes never touch disk or the DB; only the resulting
  transcript text is persisted, via the same `Message.body` column text messages use.
- **Low confidence is handled like a failure, not a crash.** A stripped transcript
  shorter than `min_transcript_chars` (default 2) gets the same clarification path as
  a permanent failure — never fed to the LLM.
- **Logging discipline (LGPD).** Every log line uses `wam_id` / `provider` /
  `char_count` — never the transcript text, the WhatsApp access token, the media
  URL/id list, or raw audio bytes. `webhook_audio_enqueued` logs only a count.

## Config

New settings (`config.py`, `.env.example`):

- `OPENAI_TRANSCRIPT_MODEL` (default `gpt-4o-mini-transcribe`) — the STT model.
- `GROQ_API_KEY` (optional fallback STT provider, empty = openai-only).
- `AUDIO_TRANSCRIPTION_PRIMARY` (`"openai"` | `"groq"`, default `"openai"`).
- `AUDIO_DOMAIN_PROMPT` — clinic-vocabulary bias prompt sent to the STT model.

Renamed: `OPENAI_MODEL` → `OPENAI_SECRETARIA_MODEL` (the chat/agent model). This is a
deliberate split, not just a rename: the STT model must never read the same env var
as the chat model — transcription-core's `TranscriptionConfig` fails fast if
`openai_model` isn't a transcription model, and `_transcription_config()` reads
`OPENAI_TRANSCRIPT_MODEL` exclusively. `OPENAI_MODEL` is kept as a **legacy env
alias** (`pydantic.AliasChoices("OPENAI_SECRETARIA_MODEL", "OPENAI_MODEL")` — the new
name wins if both are set) so a deployment that only sets `OPENAI_MODEL` keeps
working unchanged after this upgrade. Every reader was migrated (`ai/graph.py`,
`scripts/test_agent.py`) — `settings.OPENAI_MODEL` no longer exists as an attribute.

## Pendências

- **Library not pushed yet.** `pyproject.toml` points at
  `git+https://github.com/MateusChrysostomoS/transcription-core.git@v0.1.0`, but that
  repo isn't pushed / tagged yet. Implementation and the full test suite were
  validated against a temporary local `path = "../transcription-core", editable =
  true` source, flipped back to the git+tag form before finishing this round —
  `uv.lock` is unchanged by this round (see below). Once the human pushes the
  library and creates the `v0.1.0` tag: run `uv lock`, then `uv sync`.
- **`uv.lock`**: `uv.lock` already carried a small pre-existing uncommitted diff
  (an `aiosqlite` dev-dependency addition) before this round started, unrelated to
  audio transcription. This round's own `uv sync`/local-source round-trip left
  `uv.lock` byte-for-byte back at that same pre-existing state — i.e. `git diff
  uv.lock` still shows that one pre-existing 11-line diff, not nothing. Do not
  `git checkout -- uv.lock` to "clean" it without checking first; that would discard
  the pre-existing `aiosqlite` change too.
- **Deploy env vars**: set `OPENAI_SECRETARIA_MODEL` (or rely on the legacy
  `OPENAI_MODEL` alias already in place) and `OPENAI_TRANSCRIPT_MODEL` in the deploy
  environment; optionally `GROQ_API_KEY` + `AUDIO_TRANSCRIPTION_PRIMARY=groq` for the
  fallback provider.
- **End-to-end WhatsApp test pending** — validated so far via the unit/integration
  suite only (fakes installed at the `transcribe_whatsapp_media` boundary, plus a
  real sqlite DB for the persistence/idempotency assertions); a real voice note
  through a live WABA number has not been exercised yet.
- **Pre-existing, unrelated test failure**: `test_human_backup_plugin.py::
  test_on_inbound_inside_hours_returns_false` fails identically with or without this
  change (confirmed against a pre-task baseline run before touching any code) — looks
  time-of-day dependent; not touched as part of this round.
