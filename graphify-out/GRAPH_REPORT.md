# Graph Report - secretarIA  (2026-06-08)

## Corpus Check
- 71 files · ~22,387 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 764 nodes · 1037 edges · 70 communities (58 shown, 12 thin omitted)
- Extraction: 80% EXTRACTED · 20% INFERRED · 0% AMBIGUOUS · INFERRED: 204 edges (avg confidence: 0.77)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `c1a1ea2e`
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
- [[_COMMUNITY_Community 64|Community 64]]
- [[_COMMUNITY_Community 65|Community 65]]
- [[_COMMUNITY_Community 68|Community 68]]
- [[_COMMUNITY_Community 69|Community 69]]
- [[_COMMUNITY_Community 70|Community 70]]

## God Nodes (most connected - your core abstractions)
1. `get_settings()` - 29 edges
2. `parse()` - 23 edges
3. `CalendarService` - 17 edges
4. `_send_bot_reply()` - 16 edges
5. `_persist_inbound_message()` - 15 edges
6. `Base` - 14 edges
7. `Message` - 14 edges
8. `get_settings (lru_cache)` - 14 edges
9. `_handle_menu_command()` - 13 edges
10. `_persist_human_echo()` - 13 edges

## Surprising Connections (you probably didn't know these)
- `main()` --calls--> `get_settings()`  [INFERRED]
  scripts/gcal_auth.py → src/secretaria/config.py
- `Graphify Bash PreToolUse Hook` --conceptually_related_to--> `receive_webhook (POST)`  [AMBIGUOUS]
  .claude/settings.json → src/secretaria/api/webhook.py
- `WhatsApp Coexistence mode` --rationale_for--> `HandoverManager`  [INFERRED]
  README.md → src/secretaria/services/handover.py
- `Handover via smb_message_echoes` --rationale_for--> `_handle_human_echoes`  [INFERRED]
  README.md → src/secretaria/workers/tasks.py
- `check_scopes.main` --semantically_similar_to--> `CalendarService._build_service`  [INFERRED] [semantically similar]
  scripts/check_scopes.py → src/secretaria/services/calendar.py

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

## Communities (70 total, 12 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.18
Nodes (14): CalendarService._build_service, CalendarService.cancel_event, CalendarService.check_availability, CalendarService.create_event, SCOPES = calendar.events, CalendarService, check_scopes.main, check_scopes._short (+6 more)

### Community 1 - "Community 1"
Cohesion: 0.12
Nodes (22): Base, Bot/human handover state, Base, Declarative base shared by every ORM model., Base (DeclarativeBase), DeclarativeBase, Initial schema upgrade, Conversation (+14 more)

### Community 2 - "Community 2"
Cohesion: 0.25
Nodes (7): create_app(), lifespan(), FastAPI application entrypoint.  Run with:     uvicorn secretaria.main:app --hos, Create the arq Redis pool on startup, close it on shutdown.      The pool is sto, Create the arq Redis pool on startup, close it on shutdown.      The pool is sto, Build and configure the FastAPI application., Build and configure the FastAPI application.

### Community 3 - "Community 3"
Cohesion: 0.09
Nodes (30): WorkerSettings (arq config), postgres compose service, redis compose service, HandoverManager, PrecheckClient, ProcessedEvent (idempotency ledger), Idempotency ledger pattern, MVP single-tenant auto-provision (+22 more)

### Community 4 - "Community 4"
Cohesion: 0.11
Nodes (22): _bubble_history_body(), _extract_sent_wam_id(), arq job functions - all async webhook processing happens here.  This code runs O, Generate a reply, send it via the Cloud API, and record it., Generate a reply, split it into bubbles, send each, and record them., Dispatch a single bubble to the right WhatsAppClient method., Render an outbound bubble as the text the LLM should see in history.      Intera, Pull the wamid from a Cloud API send response, tolerating bad shapes. (+14 more)

### Community 5 - "Community 5"
Cohesion: 0.10
Nodes (15): CalendarService, Google Calendar integration for the clinic.  Fase A (single tenant): credentials, Create an event on the clinic's calendar. Returns the inserted event., Return free [start, end) slots on `day` within business hours.          Walks th, Return free [start, end) slots on `day` within business hours.          Walks th, Delete an event by id. 404/410 are treated as success (idempotent)., Create an event on the clinic's calendar. Returns the inserted event., Create an event on the clinic's calendar. Returns the inserted event. (+7 more)

### Community 6 - "Community 6"
Cohesion: 0.07
Nodes (33): build_agent(), invoke_agent(), _invoke_agent_with_retry(), _load_history(), _looks_like_meta_output(), _prompt_with_today(), LangGraph agent for SecretarIA (Phase 5 / Fase B).  create_react_agent gives us, Reconstruct LangChain message history from the DB.      Pulls the most recent HI (+25 more)

### Community 7 - "Community 7"
Cohesion: 0.29
Nodes (5): MessageDirection, MessageSender, Message model - a single message inside a conversation., Relative to the clinic's WhatsApp number., Who authored the message.

### Community 8 - "Community 8"
Cohesion: 0.11
Nodes (23): _filter_new_event_ids(), WhatsApp webhook endpoints.  GET  /webhook  - Meta verification handshake (echoe, Meta verification handshake.      Meta calls this once when the webhook is confi, Return the subset of `event_ids` not yet in `processed_events`.      Fail-open:, Receive a webhook event.      GOLDEN RULE: return 200 in well under 5 seconds. O, receive_webhook(), verify_webhook(), Security helpers - Meta webhook HMAC-SHA256 signature validation.  Meta signs ev (+15 more)

### Community 9 - "Community 9"
Cohesion: 0.27
Nodes (5): PrecheckClient, Async client for the Precheck (anamnese) service., Issue an authenticated request, logging structured errors., Start an anamnese (pre-consultation questionnaire) for a patient., Fetch the result of a previously started anamnese.

### Community 10 - "Community 10"
Cohesion: 0.50
Nodes (4): main(), _print_tool_calls(), Terminal agent loop for SecretarIA (Fase A smoke test).  Uses the SAME LangGraph, Surface tool invocations so the dev can see what the LLM decided.

### Community 11 - "Community 11"
Cohesion: 0.17
Nodes (6): HandoverManager, Handover logic - switching a conversation between the bot and a human.  Coexiste, Reads and mutates the handover state of a conversation.      All mutations `flus, True when the bot is allowed to answer automatically., Pause the bot - a human secretary has taken over., True when a HUMAN_ACTIVE conversation has been idle long enough to         hand

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
Cohesion: 0.10
Nodes (33): ButtonBubble, _clean(), _finalise(), parse(), _parse_slot_rows(), _pop_preceding_text_for(), Parse the agent's text output into a sequence of WhatsApp message bubbles.  The, Split a free-text chunk into one or more text bubbles on `---`. (+25 more)

### Community 33 - "Community 33"
Cohesion: 0.15
Nodes (16): cancel_event(), check_availability(), create_event(), _get_calendar(), list_free_slots(), LangChain tools for the LangGraph agent.  Each tool wraps a CalendarService meth, Lista eventos que conflitam com o intervalo [start, end) no calendário     da cl, Lista eventos que conflitam com o intervalo [start, end) no calendário     da cl (+8 more)

### Community 34 - "Community 34"
Cohesion: 0.13
Nodes (19): extract_inbound_body(), Return the human-readable text body of an inbound message.      Handles text mes, Tests for the webhook payload parser, focused on interactive replies., test_empty_interactive_yields_none(), test_extract_button_reply(), test_extract_list_reply_non_slot_id_falls_back_to_title(), test_extract_list_reply_slot_includes_iso_in_body(), test_extract_text_body() (+11 more)

### Community 35 - "Community 35"
Cohesion: 0.22
Nodes (8): Auxiliary scripts (Fase A scaffolding), graphify, Implementation status, Per-tenant config (the rows we must grow), Product vision, SecretarIA, The hardcoded "Eye Company" is placeholder test data, What the agent needs to become

### Community 36 - "Community 36"
Cohesion: 0.09
Nodes (32): decrypt(), encrypt(), EncryptionError, _fernet(), Symmetric encryption for tenant secrets at rest (Fernet / AES-128-CBC + HMAC)., Raised when encryption/decryption cannot be performed., Build the process-wide Fernet from settings.ENCRYPTION_KEY.      Cached so we va, Encrypt `plaintext`, returning a urlsafe-base64 ciphertext string. (+24 more)

### Community 37 - "Community 37"
Cohesion: 0.09
Nodes (34): _appointment_read(), cancel_appointment(), create_appointment(), create_block(), _get_appointment(), _get_calendar(), list_events(), Doctor hub — calendar platform endpoints (authenticated).  GET   /tenants/me/cal (+26 more)

### Community 38 - "Community 38"
Cohesion: 0.09
Nodes (32): BaseModel, AppointmentStatus, Appointment model - links a Google Calendar event to a patient + phone., AppointmentCancel, AppointmentCreate, AppointmentRead, AppointmentReschedule, AppointmentStatusUpdate (+24 more)

### Community 39 - "Community 39"
Cohesion: 0.08
Nodes (25): get_config(), Doctor hub — tenant configuration endpoints (authenticated).  GET  /tenants/me/c, _read_model(), update_config(), AppointmentType, _check_business_hours(), _end_after_start(), _parse_hhmm() (+17 more)

### Community 40 - "Community 40"
Cohesion: 0.18
Nodes (12): _bearer_token(), get_current_tenant(), Shared FastAPI dependencies for the doctor hub.  `get_current_tenant` is the sin, Extract the token from an Authorization header, tolerating a missing     'Bearer, Load the claimed tenant, or the only tenant when the claim has no id., Authenticate the request and return the caller's Tenant row.      401 for a miss, _resolve_tenant(), Subscription-token verification — the seam where the future Payments API plugs i (+4 more)

### Community 41 - "Community 41"
Cohesion: 0.14
Nodes (13): appointment_duration_min, appointment_types, business_hours, friday, monday, saturday, thursday, tuesday (+5 more)

### Community 42 - "Community 42"
Cohesion: 0.15
Nodes (8): Exception, _FakeError, _noop_sleep(), Tests for the transient-network retry inside run_agent.  Verifies that a single, Skip the 1-second backoff so the test suite stays fast., Stand-in for a non-transient runtime error (e.g. a bug in the agent)., Bypass DB history loading so the tests don't need Postgres., _skip_history()

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
Cohesion: 0.10
Nodes (17): get_logger(), Structured logging setup using structlog.  JSON output in non-dev environments,, Configure structlog + stdlib logging. Safe to call more than once., Return a bound structlog logger., setup_logging(), main(), Dev shortcut: store the Fase A GOOGLE_REFRESH_TOKEN as the tenant's encrypted Ca, Create a development tenant from environment settings.  Usage:     uv run python (+9 more)

### Community 48 - "Community 48"
Cohesion: 0.27
Nodes (10): Multi-tenant routing by phone_number_id, get_settings (lru_cache), async_session_factory, async engine, get_session dependency, _do_run_migrations, run_migrations_offline, run_migrations_online (+2 more)

### Community 49 - "Community 49"
Cohesion: 0.14
Nodes (13): Administrative endpoints (currently: data wipe).  Every route here is guarded by, FastAPI dependency: 403 unless `X-Admin-Token` matches the env token.      Uses, Reset the database. Requires `confirm: true` to actually run., require_admin(), reset_data(), ResetRequest, ResetResponse, main() (+5 more)

### Community 50 - "Community 50"
Cohesion: 0.14
Nodes (13): ProcessedEvent, ProcessedEvent model - idempotency ledger for incoming webhook events., One row per Meta event id already handled.      The unique constraint on `event_, _event_already_processed(), _persist_human_echo(), Record a human echo and switch the conversation to HUMAN_ACTIVE., True when `event_id` is already in the `processed_events` ledger., Record a human echo and switch the conversation to HUMAN_ACTIVE. (+5 more)

### Community 51 - "Community 51"
Cohesion: 0.40
Nodes (5): _get_or_create_patient(), Return the patient for (tenant, wa_id), creating it if needed., Return the patient for (tenant, wa_id), creating it if needed., Return the patient for (tenant, wa_id), creating it if needed., Return the patient for (tenant, wa_id), creating it if needed.

### Community 52 - "Community 52"
Cohesion: 0.50
Nodes (3): get_session(), Async SQLAlchemy 2.0 engine, session factory and declarative Base., FastAPI dependency that yields a database session.

### Community 53 - "Community 53"
Cohesion: 0.29
Nodes (4): BaseSettings, Application configuration loaded from environment variables / .env file., Strongly-typed settings. Values come from the environment or `.env`.      Real e, Settings

### Community 54 - "Community 54"
Cohesion: 0.25
Nodes (9): _handle_patient_messages(), _persist_inbound_message(), Record an inbound message in its own transaction.      Returns a `_ReplyContext`, Persist inbound patient messages and, if the bot is active, reply., Persist inbound patient messages and, if the bot is active, reply., Record an inbound message in its own transaction.      Returns a `_ReplyContext`, Record an inbound message in its own transaction.      Returns a `_ReplyContext`, Persist inbound patient messages and, if the bot is active, reply. (+1 more)

### Community 55 - "Community 55"
Cohesion: 0.50
Nodes (3): HTTP client for the Precheck service (a separate FastAPI app).  Precheck runs in, # TODO: PRECHECK_CONTRACT_NEEDED - confirmar path e payload com o dono da API, # TODO: PRECHECK_CONTRACT_NEEDED - confirmar path e payload com o dono da API

### Community 56 - "Community 56"
Cohesion: 0.33
Nodes (6): Eye Company is Placeholder Scaffold, Multi-tenant SaaS Vision, Per-tenant Config (identity/services/hours/tz/lang/calendar/whatsapp), WhatsApp Cloud API Coexistence Model, ai.graph._prompt_with_today, secretary_system_prompt

### Community 58 - "Community 58"
Cohesion: 0.25
Nodes (8): get_settings(), Return a cached Settings instance (read once per process)., Return a cached Settings instance (read once per process)., build_authorization_url(), exchange_code_for_refresh_token(), Google Calendar OAuth helpers (platform-level hub onboarding).  A manual authori, Build the Google consent URL the doctor is redirected to., Exchange an authorization `code` for tokens; return the refresh_token.      Retu

### Community 59 - "Community 59"
Cohesion: 0.40
Nodes (5): Tests for the /menu reset-command predicate., test_non_triggers(), test_recognised_triggers(), is_menu_command(), True when the patient typed a `/menu`-style reset command.

### Community 61 - "Community 61"
Cohesion: 0.67
Nodes (3): main(), Diagnose Fase A auth: what scopes does our refresh token actually carry?  A refr, _short()

### Community 64 - "Community 64"
Cohesion: 0.25
Nodes (8): _get_or_create_conversation(), _handle_menu_command(), Reset the conversation and send a fresh button menu.      Wipes every prior mess, Reset the conversation and send a fresh button menu.      Wipes every prior mess, Return the conversation for (tenant, patient), creating it if needed., Return the conversation for (tenant, patient), creating it if needed., Return the conversation for (tenant, patient), creating it if needed., Return the conversation for (tenant, patient), creating it if needed.

### Community 65 - "Community 65"
Cohesion: 0.22
Nodes (8): Tenant model - one clinic, with its own WhatsApp Business credentials., A clinic using SecretarIA.      The system is multi-tenant in the data model. Fo, Tenant, Find the tenant for an inbound event.      MVP single-tenant convenience: when n, Find the tenant for an inbound event.      MVP single-tenant convenience: when n, Find the tenant for an inbound event.      MVP single-tenant convenience: when n, Find the tenant for an inbound event.      MVP single-tenant convenience: when n, _resolve_tenant()

## Ambiguous Edges - Review These
- `Graphify Bash PreToolUse Hook` → `receive_webhook (POST)`  [AMBIGUOUS]
  .claude/settings.json · relation: conceptually_related_to

## Knowledge Gaps
- **58 isolated node(s):** `PreToolUse`, `greeting_message`, `persona_notes`, `language`, `timezone` (+53 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **12 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Graphify Bash PreToolUse Hook` and `receive_webhook (POST)`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `get_settings()` connect `Community 58` to `Community 65`, `Community 2`, `Community 36`, `Community 37`, `Community 6`, `Community 5`, `Community 8`, `Community 40`, `Community 10`, `Community 11`, `Community 9`, `Community 12`, `Community 46`, `Community 47`, `Community 49`, `Community 53`, `Community 61`?**
  _High betweenness centrality (0.171) - this node is a cross-community bridge._
- **Why does `_send_bot_reply()` connect `Community 4` to `Community 32`, `Community 1`, `Community 36`, `Community 37`, `Community 6`, `Community 12`, `Community 54`?**
  _High betweenness centrality (0.118) - this node is a cross-community bridge._
- **Why does `Base (DeclarativeBase)` connect `Community 1` to `Community 48`?**
  _High betweenness centrality (0.107) - this node is a cross-community bridge._
- **Are the 29 inferred relationships involving `str` (e.g. with `main()` and `seed()`) actually correct?**
  _`str` has 29 INFERRED edges - model-reasoned connections that need verification._
- **Are the 25 inferred relationships involving `get_settings()` (e.g. with `main()` and `main()`) actually correct?**
  _`get_settings()` has 25 INFERRED edges - model-reasoned connections that need verification._
- **Are the 13 inferred relationships involving `parse()` (e.g. with `_send_bot_reply()` and `test_empty_reply_yields_no_bubbles()`) actually correct?**
  _`parse()` has 13 INFERRED edges - model-reasoned connections that need verification._