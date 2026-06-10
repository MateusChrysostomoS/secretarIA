# Graph Report - secretarIA  (2026-06-10)

## Corpus Check
- 79 files · ~30,838 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 987 nodes · 1458 edges · 88 communities (72 shown, 16 thin omitted)
- Extraction: 79% EXTRACTED · 21% INFERRED · 0% AMBIGUOUS · INFERRED: 299 edges (avg confidence: 0.76)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `b294dd87`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]
- [[_COMMUNITY_Community 60|Community 60]]
- [[_COMMUNITY_Community 61|Community 61]]
- [[_COMMUNITY_Community 62|Community 62]]
- [[_COMMUNITY_Community 63|Community 63]]
- [[_COMMUNITY_Community 64|Community 64]]
- [[_COMMUNITY_Community 65|Community 65]]
- [[_COMMUNITY_Community 68|Community 68]]
- [[_COMMUNITY_Community 69|Community 69]]
- [[_COMMUNITY_Community 70|Community 70]]
- [[_COMMUNITY_Community 71|Community 71]]
- [[_COMMUNITY_Community 72|Community 72]]
- [[_COMMUNITY_Community 73|Community 73]]
- [[_COMMUNITY_Community 74|Community 74]]
- [[_COMMUNITY_Community 75|Community 75]]
- [[_COMMUNITY_Community 76|Community 76]]
- [[_COMMUNITY_Community 77|Community 77]]
- [[_COMMUNITY_Community 78|Community 78]]
- [[_COMMUNITY_Community 79|Community 79]]
- [[_COMMUNITY_Community 83|Community 83]]
- [[_COMMUNITY_Community 84|Community 84]]
- [[_COMMUNITY_Community 85|Community 85]]
- [[_COMMUNITY_Community 86|Community 86]]
- [[_COMMUNITY_Community 87|Community 87]]

## God Nodes (most connected - your core abstractions)
1. `get_settings()` - 33 edges
2. `route()` - 27 edges
3. `parse()` - 23 edges
4. `_send_bot_reply()` - 23 edges
5. `CalendarService` - 22 edges
6. `_persist_inbound_message()` - 21 edges
7. `_handle_menu_command()` - 18 edges
8. `_tenant()` - 18 edges
9. `_conversation()` - 18 edges
10. `_ReplyContext` - 17 edges

## Surprising Connections (you probably didn't know these)
- `Graphify Bash PreToolUse Hook` --conceptually_related_to--> `receive_webhook (POST)`  [AMBIGUOUS]
  .claude/settings.json → src/secretaria/api/webhook.py
- `WhatsApp Coexistence mode` --rationale_for--> `HandoverManager`  [INFERRED]
  README.md → src/secretaria/services/handover.py
- `Handover via smb_message_echoes` --rationale_for--> `_handle_human_echoes`  [INFERRED]
  README.md → src/secretaria/workers/tasks.py
- `check_scopes.main` --semantically_similar_to--> `CalendarService._build_service`  [INFERRED] [semantically similar]
  scripts/check_scopes.py → src/secretaria/services/calendar.py
- `test_agent.main` --semantically_similar_to--> `ai.graph.run_agent`  [INFERRED] [semantically similar]
  scripts/test_agent.py → src/secretaria/ai/graph.py

## Hyperedges (group relationships)
- **Alembic async migration setup** — env_run_migrations_online, env_do_run_migrations, initial_schema_upgrade, database_base [INFERRED 0.85]
- **WhatsApp webhook ingest pipeline** — webhook_receive_webhook, security_verify_meta_signature, webhook_filter_new_event_ids, concept_arq_enqueue_offload [INFERRED 0.85]
- **Patient/Conversation/Message data model** — models_patient_patient, models_conversation_conversation, models_message_message, database_base [EXTRACTED 1.00]
- **Idempotent webhook processing pipeline** — tasks_process_webhook_event, tasks_event_already_processed, processed_event_ProcessedEvent, webhook_iter_event_ids [INFERRED 0.85]
- **Coexistence handover (echoes pause bot)** — tasks_handle_human_echoes, tasks_persist_human_echo, handover_HandoverManager, readme_coexistence_mode [INFERRED 0.85]
- **Inbound message persist-and-reply flow** — tasks_handle_patient_messages, tasks_persist_inbound_message, tasks_send_bot_reply, whatsapp_WhatsAppClient, tasks_ReplyContext [INFERRED 0.85]
- **LangGraph ReAct Agent Tool Loop** — graph_build_agent, tools_check_availability, tools_create_event, tools_cancel_event, prompts_secretary_system_prompt [EXTRACTED 0.95]
- **Google Calendar Integration Layer** — calendar_service, tools_check_availability, tools_create_event, tools_cancel_event, calendar_scopes_constant [EXTRACTED 0.95]
- **Fase A OAuth Scaffolding Scripts** — gcal_auth_main, check_scopes_main, test_agent_main, config_settings [INFERRED 0.85]

## Communities (88 total, 16 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.18
Nodes (14): CalendarService._build_service, CalendarService.cancel_event, CalendarService.check_availability, CalendarService.create_event, SCOPES = calendar.events, CalendarService, check_scopes.main, check_scopes._short (+6 more)

### Community 1 - "Community 1"
Cohesion: 0.16
Nodes (16): Bot/human handover state, Base (DeclarativeBase), Initial schema upgrade, Conversation, HandoverState, Conversation model - one ongoing thread between a patient and a clinic., Who currently owns the conversation.      BOT_ACTIVE   - the AI may answer autom, A patient <-> clinic conversation and its handover state. (+8 more)

### Community 2 - "Community 2"
Cohesion: 0.25
Nodes (7): create_app(), lifespan(), FastAPI application entrypoint.  Run with:     uvicorn secretaria.main:app --hos, Create the arq Redis pool on startup, close it on shutdown.      The pool is sto, Create the arq Redis pool on startup, close it on shutdown.      The pool is sto, Build and configure the FastAPI application., Build and configure the FastAPI application.

### Community 3 - "Community 3"
Cohesion: 0.09
Nodes (30): WorkerSettings (arq config), postgres compose service, redis compose service, HandoverManager, PrecheckClient, ProcessedEvent (idempotency ledger), Idempotency ledger pattern, MVP single-tenant auto-provision (+22 more)

### Community 4 - "Community 4"
Cohesion: 0.15
Nodes (18): check_handover_timeouts(), _dispatch_bubbles(), _handle_calendar_unavailable(), arq job functions - all async webhook processing happens here.  This code runs O, arq cron: hand stale HUMAN_ACTIVE conversations back to the bot.      A conversa, Generate a reply, send it via the Cloud API, and record it., Generate a reply, split it into bubbles, send each, and record them., Generate a reply, split it into bubbles, send each, and record them. (+10 more)

### Community 5 - "Community 5"
Cohesion: 0.08
Nodes (21): CalendarService, Create an event on the clinic's calendar. Returns the inserted event., Return free [start, end) slots on `day` within business hours.          Walks th, Return free [start, end) slots on `day` within business hours.          Walks th, Return events overlapping [start, end) on the clinic calendar.          Each ite, Delete an event by id. 404/410 are treated as success (idempotent)., Create an event on the clinic's calendar. Returns the inserted event., Create an event on the clinic's calendar. Returns the inserted event. (+13 more)

### Community 6 - "Community 6"
Cohesion: 0.06
Nodes (37): build_agent(), invoke_agent(), _invoke_agent_with_retry(), _load_history(), _looks_like_meta_output(), _prompt_with_today(), LangGraph agent for SecretarIA (Phase 5 / Fase B).  create_react_agent gives us, Reconstruct LangChain message history from the DB.      Pulls the most recent HI (+29 more)

### Community 7 - "Community 7"
Cohesion: 0.20
Nodes (10): _filter_new_event_ids(), WhatsApp webhook endpoints.  GET  /webhook  - Meta verification handshake (echoe, Meta verification handshake.      Meta calls this once when the webhook is confi, Return the subset of `event_ids` not yet in `processed_events`.      Fail-open:, Receive a webhook event.      GOLDEN RULE: return 200 in well under 5 seconds. O, receive_webhook(), verify_webhook(), iter_event_ids() (+2 more)

### Community 8 - "Community 8"
Cohesion: 0.23
Nodes (13): Security helpers - Meta webhook HMAC-SHA256 signature validation.  Meta signs ev, Return True if `signature_header` is a valid HMAC of `raw_body`.      Args:, verify_meta_signature(), Tests for the Meta webhook HMAC-SHA256 signature validation., Build a valid `X-Hub-Signature-256` header value for `body`., _sign(), test_empty_app_secret_is_rejected(), test_invalid_signature_is_rejected() (+5 more)

### Community 9 - "Community 9"
Cohesion: 0.18
Nodes (8): PrecheckClient, HTTP client for the Precheck service (a separate FastAPI app).  Precheck runs in, Async client for the Precheck (anamnese) service., Issue an authenticated request, logging structured errors., Start an anamnese (pre-consultation questionnaire) for a patient., # TODO: PRECHECK_CONTRACT_NEEDED - confirmar path e payload com o dono da API, Fetch the result of a previously started anamnese., # TODO: PRECHECK_CONTRACT_NEEDED - confirmar path e payload com o dono da API

### Community 10 - "Community 10"
Cohesion: 0.17
Nodes (12): _extract_sent_wam_id(), Pull the wamid from a Cloud API send response, tolerating bad shapes., Pull the wamid from a Cloud API send response, tolerating bad shapes., Send the tenant's first-contact greeting as a single verbatim message.      With, Send the tenant's first-contact greeting as a single verbatim message., Send the tenant's first-contact greeting as a single verbatim message.      With, Pull the wamid from a Cloud API send response, tolerating bad shapes., Pull the wamid from a Cloud API send response, tolerating bad shapes. (+4 more)

### Community 11 - "Community 11"
Cohesion: 0.20
Nodes (5): HandoverManager, Handover logic - switching a conversation between the bot and a human.  Coexiste, Reads and mutates the handover state of a conversation.      All mutations `flus, True when the bot is allowed to answer automatically., Pause the bot - a human secretary has taken over.

### Community 12 - "Community 12"
Cohesion: 0.13
Nodes (12): _extract_message_id(), WhatsApp Cloud API client - sends outbound messages., Send an interactive reply-button message (max 3 buttons).          Args:, Pull the wamid from a Cloud API send response, tolerating bad shapes., Send an interactive list message (max 10 rows in one section).          Args:, Send an interactive list message (max 10 rows in one section).          Args:, Async client for the Meta WhatsApp Cloud API., Send a plain-text WhatsApp message.          Args:             to: recipient wa_ (+4 more)

### Community 13 - "Community 13"
Cohesion: 0.29
Nodes (5): Alembic environment - async (asyncpg) configuration., Run migrations in 'offline' mode (emit SQL, no live DB connection)., Run migrations in 'online' mode using an async engine., run_migrations_offline(), run_migrations_online()

### Community 15 - "Community 15"
Cohesion: 0.22
Nodes (8): health(), Health check endpoint., Liveness probe. Intentionally does not touch Postgres or Redis., Health router, FastAPI app instance, create_app, Webhook router, verify_webhook (GET handshake)

### Community 16 - "Community 16"
Cohesion: 0.50
Nodes (3): client(), Shared pytest fixtures and deterministic test environment., An httpx AsyncClient bound to the FastAPI app via ASGITransport.      The app li

### Community 19 - "Community 19"
Cohesion: 0.67
Nodes (3): client fixture (ASGITransport), HMAC signature tests, Webhook endpoint tests

### Community 31 - "Community 31"
Cohesion: 0.06
Nodes (35): 1. Install dependencies, 2. Create your .env, 3. Start Postgres + Redis, 4. Apply database migrations, 5. (optional) Seed a development tenant, 6. Run the API, 7. Run the worker (in a second terminal), Architecture (+27 more)

### Community 32 - "Community 32"
Cohesion: 0.05
Nodes (72): ButtonBubble, _clean(), _finalise(), parse(), _parse_slot_rows(), _pop_preceding_text_for(), Parse the agent's text output into a sequence of WhatsApp message bubbles.  The, Split a free-text chunk into one or more text bubbles on `---`. (+64 more)

### Community 33 - "Community 33"
Cohesion: 0.10
Nodes (24): cancel_event(), check_availability(), create_event(), _event_window(), _get_calendar(), list_free_slots(), _mark_appointment_cancelled(), LangChain tools for the LangGraph agent.  Each tool wraps a CalendarService meth (+16 more)

### Community 34 - "Community 34"
Cohesion: 0.06
Nodes (37): extract_inbound_body(), Return the human-readable text body of an inbound message.      Handles text mes, Tests for the /menu reset-command predicate., test_non_triggers(), test_recognised_triggers(), Tests for the webhook payload parser, focused on interactive replies., test_empty_interactive_yields_none(), test_extract_button_reply() (+29 more)

### Community 35 - "Community 35"
Cohesion: 0.22
Nodes (8): Auxiliary scripts (Fase A scaffolding), graphify, Implementation status, Per-tenant config (the rows we must grow), Product vision, SecretarIA, The hardcoded "Eye Company" is placeholder test data, What the agent needs to become

### Community 36 - "Community 36"
Cohesion: 0.05
Nodes (46): decrypt(), encrypt(), EncryptionError, _fernet(), Symmetric encryption for tenant secrets at rest (Fernet / AES-128-CBC + HMAC)., Raised when encryption/decryption cannot be performed., Build the process-wide Fernet from settings.ENCRYPTION_KEY.      Cached so we va, Encrypt `plaintext`, returning a urlsafe-base64 ciphertext string. (+38 more)

### Community 37 - "Community 37"
Cohesion: 0.28
Nodes (15): _appointment_read(), cancel_appointment(), create_appointment(), create_block(), _get_appointment(), _get_calendar(), list_events(), Doctor hub — calendar platform endpoints (authenticated).  GET   /tenants/me/cal (+7 more)

### Community 38 - "Community 38"
Cohesion: 0.06
Nodes (43): Administrative endpoints (currently: data wipe).  Every route here is guarded by, Reset the database. Requires `confirm: true` to actually run., reset_data(), ResetRequest, ResetResponse, BaseModel, AppointmentStatus, Appointment model - links a Google Calendar event to a patient + phone. (+35 more)

### Community 39 - "Community 39"
Cohesion: 0.07
Nodes (27): get_config(), Doctor hub — tenant configuration endpoints (authenticated).  GET  /tenants/me/c, _read_model(), update_config(), AppointmentType, _check_business_hours(), _end_after_start(), _parse_hhmm() (+19 more)

### Community 40 - "Community 40"
Cohesion: 0.18
Nodes (12): _bearer_token(), get_current_tenant(), Shared FastAPI dependencies for the doctor hub.  `get_current_tenant` is the sin, Extract the token from an Authorization header, tolerating a missing     'Bearer, Load the claimed tenant, or the only tenant when the claim has no id., Authenticate the request and return the caller's Tenant row.      401 for a miss, _resolve_tenant(), Subscription-token verification — the seam where the future Payments API plugs i (+4 more)

### Community 41 - "Community 41"
Cohesion: 0.09
Nodes (22): appointment_duration_min, appointment_types, business_hours, friday, monday, saturday, thursday, tuesday (+14 more)

### Community 42 - "Community 42"
Cohesion: 0.14
Nodes (9): _noop_sleep(), Tests for the transient-network retry inside run_agent.  Verifies that a single, Skip the 1-second backoff so the test suite stays fast., A CalendarUnavailableError from a tool must surface the degradation     sentinel, Skip the 1-second backoff so the test suite stays fast., Bypass DB history loading so the tests don't need Postgres., Bypass DB history loading so the tests don't need Postgres., _skip_history() (+1 more)

### Community 43 - "Community 43"
Cohesion: 0.18
Nodes (3): _fake_wipe(), Tests for the admin reset endpoint and the X-Admin-Token guard., Replace the destructive coroutine with a recorder.      Tests must never reach P

### Community 44 - "Community 44"
Cohesion: 0.22
Nodes (10): Fase A — Terminal Tool Loop (validated), Fase B — LangGraph ReAct in arq worker, ai.graph.build_agent, FALLBACK_REPLY constant, ai.graph.invoke_agent, ai.graph._load_history, ai.graph._looks_like_meta_output, ai.graph.run_agent (+2 more)

### Community 45 - "Community 45"
Cohesion: 0.28
Nodes (9): Async offload via arq queue, HMAC-SHA256 webhook signature verification, Webhook idempotency fast-path, get_logger, FastAPI lifespan, verify_meta_signature, Graphify Bash PreToolUse Hook, _filter_new_event_ids (+1 more)

### Community 46 - "Community 46"
Cohesion: 0.15
Nodes (10): get_logger(), Structured logging setup using structlog.  JSON output in non-dev environments,, Configure structlog + stdlib logging. Safe to call more than once., Return a bound structlog logger., setup_logging(), main(), Dev shortcut: store the Fase A GOOGLE_REFRESH_TOKEN as the tenant's encrypted Ca, Create a development tenant from environment settings.  Usage:     uv run python (+2 more)

### Community 47 - "Community 47"
Cohesion: 0.18
Nodes (9): FastAPI dependency: 403 unless `X-Admin-Token` matches the env token.      Uses, require_admin(), main(), Generate a Google Calendar refresh token for Fase A (single tenant).  One-shot C, get_settings(), Return a cached Settings instance (read once per process)., Return a cached Settings instance (read once per process)., Return a cached Settings instance (read once per process). (+1 more)

### Community 48 - "Community 48"
Cohesion: 0.27
Nodes (10): Multi-tenant routing by phone_number_id, get_settings (lru_cache), async_session_factory, async engine, get_session dependency, _do_run_migrations, run_migrations_offline, run_migrations_online (+2 more)

### Community 49 - "Community 49"
Cohesion: 0.13
Nodes (32): Exception, CalendarUnavailableError, _raise_if_unavailable(), Google Calendar integration for the clinic.  Fase A (single tenant): credentials, Raised when Google Calendar cannot be reached or refused our credentials.      D, Translate an outage/auth HttpError into CalendarUnavailableError.      Returns n, Decide the next deterministic step for this inbound turn.      `patient_name` is, route() (+24 more)

### Community 50 - "Community 50"
Cohesion: 0.12
Nodes (18): _event_already_processed(), _handle_menu_command(), _persist_human_echo(), Reset the conversation: wipe all context, then re-send the greeting.      Wipes, Reset the conversation and send a fresh button menu.      Wipes every prior mess, Reset the conversation and send a fresh button menu.      Wipes every prior mess, True when `event_id` is already in the `processed_events` ledger., Record a human echo and switch the conversation to HUMAN_ACTIVE. (+10 more)

### Community 51 - "Community 51"
Cohesion: 0.29
Nodes (7): _get_or_create_patient(), Return the patient for (tenant, wa_id), creating it if needed., Return the patient for (tenant, wa_id), creating it if needed., Return the patient for (tenant, wa_id), creating it if needed., Return the patient for (tenant, wa_id), creating it if needed., Return the patient for (tenant, wa_id), creating it if needed., Return the patient for (tenant, wa_id), creating it if needed.

### Community 52 - "Community 52"
Cohesion: 0.18
Nodes (10): on_shutdown(), on_startup(), arq worker entry point.  Start the worker with:     arq secretaria.workers.arq_w, Run once when the worker process starts., Run once when the worker process starts., Run once when the worker process stops., Run once when the worker process stops., arq worker configuration.      arq reads these as plain class attributes, so `re (+2 more)

### Community 53 - "Community 53"
Cohesion: 0.29
Nodes (4): BaseSettings, Application configuration loaded from environment variables / .env file., Strongly-typed settings. Values come from the environment or `.env`.      Real e, Settings

### Community 54 - "Community 54"
Cohesion: 0.20
Nodes (9): ProcessedEvent, ProcessedEvent model - idempotency ledger for incoming webhook events., One row per Meta event id already handled.      The unique constraint on `event_, _persist_inbound_message(), Record an inbound message in its own transaction.      Returns a `_ReplyContext`, Record an inbound message in its own transaction.      Returns a `_ReplyContext`, Record an inbound message in its own transaction.      Returns a `_ReplyContext`, Record an inbound message in its own transaction.      Returns a `_ReplyContext` (+1 more)

### Community 55 - "Community 55"
Cohesion: 0.33
Nodes (6): Dispatch a single bubble to the right WhatsAppClient method., Dispatch a single bubble to the right WhatsAppClient method., Dispatch a single bubble to the right WhatsAppClient method., Dispatch a single bubble to the right WhatsAppClient method., Dispatch a single bubble to the right WhatsAppClient method., _send_bubble()

### Community 56 - "Community 56"
Cohesion: 0.33
Nodes (6): Eye Company is Placeholder Scaffold, Multi-tenant SaaS Vision, Per-tenant Config (identity/services/hours/tz/lang/calendar/whatsapp), WhatsApp Cloud API Coexistence Model, ai.graph._prompt_with_today, secretary_system_prompt

### Community 58 - "Community 58"
Cohesion: 0.33
Nodes (5): build_authorization_url(), exchange_code_for_refresh_token(), Google Calendar OAuth helpers (platform-level hub onboarding).  A manual authori, Build the Google consent URL the doctor is redirected to., Exchange an authorization `code` for tokens; return the refresh_token.      Retu

### Community 59 - "Community 59"
Cohesion: 0.11
Nodes (17): _FakeRedis, Unit tests for worker helper functions (no DB / network)., Minimal async Redis stub covering the commands _is_rate_limited uses., test_extract_patient_name(), test_rate_limit_allows_under_cap_then_silences(), test_rate_limit_disabled_without_redis(), test_rate_limit_is_per_sender(), test_render_greeting_template_with_name() (+9 more)

### Community 61 - "Community 61"
Cohesion: 0.67
Nodes (3): main(), Diagnose Fase A auth: what scopes does our refresh token actually carry?  A refr, _short()

### Community 63 - "Community 63"
Cohesion: 0.13
Nodes (13): Base, get_session(), Async SQLAlchemy 2.0 engine, session factory and declarative Base., Declarative base shared by every ORM model., FastAPI dependency that yields a database session., DeclarativeBase, FlowState, Which deterministic (zero-LLM) flow the conversation is currently in.      IDLE (+5 more)

### Community 64 - "Community 64"
Cohesion: 0.29
Nodes (7): _get_or_create_conversation(), Return the conversation for (tenant, patient), creating it if needed., Return the conversation for (tenant, patient), creating it if needed., Return the conversation for (tenant, patient), creating it if needed., Return the conversation for (tenant, patient), creating it if needed., Return the conversation for (tenant, patient), creating it if needed., Return the conversation for (tenant, patient), creating it if needed.

### Community 65 - "Community 65"
Cohesion: 0.18
Nodes (10): Tenant model - one clinic, with its own WhatsApp Business credentials., A clinic using SecretarIA.      The system is multi-tenant in the data model. Fo, Tenant, Find the tenant for an inbound event.      MVP single-tenant convenience: when n, Find the tenant for an inbound event.      MVP single-tenant convenience: when n, Find the tenant for an inbound event.      MVP single-tenant convenience: when n, Find the tenant for an inbound event.      MVP single-tenant convenience: when n, Find the tenant for an inbound event.      MVP single-tenant convenience: when n (+2 more)

### Community 71 - "Community 71"
Cohesion: 0.22
Nodes (9): calendar_disconnect(), oauth_callback(), oauth_start(), _portal_redirect(), Doctor hub — Google Calendar OAuth (start / callback / disconnect).  The refresh, Forget the Calendar refresh token and force the tenant offline.      Without a C, Send the doctor's browser back to the portal with a status flag.      Falls back, Return the Google consent URL (the frontend redirects the browser to it). (+1 more)

### Community 72 - "Community 72"
Cohesion: 0.36
Nodes (7): _b64decode(), _b64encode(), Opaque, signed, time-limited tokens (stdlib HMAC-SHA256).  Used for the Google O, Serialise `payload` (+ a timestamp) and append an HMAC signature., Return the original payload if `token` is authentic and fresh, else None.      N, sign(), verify()

### Community 73 - "Community 73"
Cohesion: 0.33
Nodes (6): _bubble_history_body(), Render an outbound bubble as the text the LLM should see in history.      Intera, Render an outbound bubble as the text the LLM should see in history.      Intera, Render an outbound bubble as the text the LLM should see in history.      Intera, Render an outbound bubble as the text the LLM should see in history.      Intera, Render an outbound bubble as the text the LLM should see in history.      Intera

### Community 74 - "Community 74"
Cohesion: 0.40
Nodes (4): Base, Encrypted-at-rest credentials for a tenant.  Kept in a SEPARATE table from `tena, One row per tenant holding its encrypted Google Calendar refresh token., TenantCredentials

### Community 75 - "Community 75"
Cohesion: 0.50
Nodes (4): main(), _print_tool_calls(), Terminal agent loop for SecretarIA (Fase A smoke test).  Uses the SAME LangGraph, Surface tool invocations so the dev can see what the LLM decided.

### Community 76 - "Community 76"
Cohesion: 0.40
Nodes (5): arq job: send a text message to a patient on behalf of a specific tenant.      T, arq job: send a text message to a patient on behalf of a specific tenant.      T, arq job: send a text message to a patient on behalf of a specific tenant.      T, arq job: send a text message to a patient on behalf of a specific tenant.      T, send_patient_notification()

### Community 77 - "Community 77"
Cohesion: 0.50
Nodes (4): _persist_appointment(), Record a bot-created appointment. Best-effort: never raises.      Skipped silent, Appointment, Platform-side record of a clinic appointment.      Created whenever the platform

### Community 78 - "Community 78"
Cohesion: 0.50
Nodes (3): permissions, additionalDirectories, allow

### Community 79 - "Community 79"
Cohesion: 0.50
Nodes (4): flows_enabled(), True when this tenant uses the deterministic entry flows., _greeting_buttons_for(), Buttons to attach to a greeting.      When deterministic flows are enabled the g

## Ambiguous Edges - Review These
- `Graphify Bash PreToolUse Hook` → `receive_webhook (POST)`  [AMBIGUOUS]
  .claude/settings.json · relation: conceptually_related_to

## Knowledge Gaps
- **68 isolated node(s):** `PreToolUse`, `allow`, `additionalDirectories`, `clinic_name`, `greeting_message` (+63 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **16 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Graphify Bash PreToolUse Hook` and `receive_webhook (POST)`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `get_settings()` connect `Community 47` to `Community 65`, `Community 2`, `Community 36`, `Community 5`, `Community 6`, `Community 71`, `Community 7`, `Community 40`, `Community 9`, `Community 75`, `Community 12`, `Community 4`, `Community 46`, `Community 53`, `Community 58`, `Community 59`, `Community 61`?**
  _High betweenness centrality (0.150) - this node is a cross-community bridge._
- **Why does `Base (DeclarativeBase)` connect `Community 1` to `Community 48`?**
  _High betweenness centrality (0.089) - this node is a cross-community bridge._
- **Why does `_send_bot_reply()` connect `Community 4` to `Community 32`, `Community 1`, `Community 34`, `Community 36`, `Community 37`, `Community 6`, `Community 73`, `Community 10`, `Community 12`, `Community 79`, `Community 55`?**
  _High betweenness centrality (0.089) - this node is a cross-community bridge._
- **Are the 47 inferred relationships involving `str` (e.g. with `main()` and `main()`) actually correct?**
  _`str` has 47 INFERRED edges - model-reasoned connections that need verification._
- **Are the 28 inferred relationships involving `get_settings()` (e.g. with `main()` and `main()`) actually correct?**
  _`get_settings()` has 28 INFERRED edges - model-reasoned connections that need verification._
- **Are the 18 inferred relationships involving `route()` (e.g. with `_run_flow()` and `test_disabled_flows_delegate_llm()`) actually correct?**
  _`route()` has 18 INFERRED edges - model-reasoned connections that need verification._