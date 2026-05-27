# Graph Report - .  (2026-05-27)

## Corpus Check
- Corpus is ~10,003 words - fits in a single context window. You may not need a graph.

## Summary
- 351 nodes · 459 edges · 31 communities (25 shown, 6 thin omitted)
- Extraction: 78% EXTRACTED · 21% INFERRED · 0% AMBIGUOUS · INFERRED: 98 edges (avg confidence: 0.79)
- Token cost: 40,000 input · 9,960 output

## Community Hubs (Navigation)
- [[_COMMUNITY_App Runtime & Agent Core|App Runtime & Agent Core]]
- [[_COMMUNITY_Domain Models & Schema|Domain Models & Schema]]
- [[_COMMUNITY_FastAPI App & Webhook API|FastAPI App & Webhook API]]
- [[_COMMUNITY_Worker Pipeline (Architecture)|Worker Pipeline (Architecture)]]
- [[_COMMUNITY_Webhook Event Processing Tasks|Webhook Event Processing Tasks]]
- [[_COMMUNITY_Google Calendar Integration|Google Calendar Integration]]
- [[_COMMUNITY_LangGraph ReAct Agent|LangGraph ReAct Agent]]
- [[_COMMUNITY_Logging & Worker Bootstrap|Logging & Worker Bootstrap]]
- [[_COMMUNITY_Meta Signature Verification|Meta Signature Verification]]
- [[_COMMUNITY_PreCheck Anamnese Client|PreCheck Anamnese Client]]
- [[_COMMUNITY_Webhook Payload Schemas|Webhook Payload Schemas]]
- [[_COMMUNITY_BotHuman Handover State|Bot/Human Handover State]]
- [[_COMMUNITY_WhatsApp Cloud API Client|WhatsApp Cloud API Client]]
- [[_COMMUNITY_Alembic Migration Env|Alembic Migration Env]]
- [[_COMMUNITY_Webhook Endpoint Tests|Webhook Endpoint Tests]]
- [[_COMMUNITY_Health Endpoint|Health Endpoint]]
- [[_COMMUNITY_Test Fixtures|Test Fixtures]]
- [[_COMMUNITY_Claude Code Hooks Config|Claude Code Hooks Config]]
- [[_COMMUNITY_Test Suite Index|Test Suite Index]]
- [[_COMMUNITY_secretaria Package Init|secretaria Package Init]]
- [[_COMMUNITY_Models Package Init|Models Package Init]]
- [[_COMMUNITY_Initial Schema Downgrade|Initial Schema Downgrade]]
- [[_COMMUNITY_Settings Class (legacy ref)|Settings Class (legacy ref)]]

## God Nodes (most connected - your core abstractions)
1. `get_settings()` - 18 edges
2. `get_settings (lru_cache)` - 14 edges
3. `Conversation` - 12 edges
4. `Message` - 12 edges
5. `HandoverManager` - 12 edges
6. `Base` - 11 edges
7. `CalendarService` - 11 edges
8. `_persist_inbound_message()` - 11 edges
9. `verify_meta_signature()` - 10 edges
10. `_persist_human_echo()` - 10 edges

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

## Communities (31 total, 6 thin omitted)

### Community 0 - "App Runtime & Agent Core"
Cohesion: 0.06
Nodes (51): CalendarService._build_service, CalendarService.cancel_event, CalendarService.check_availability, CalendarService.create_event, SCOPES = calendar.events, CalendarService, check_scopes.main, check_scopes._short (+43 more)

### Community 1 - "Domain Models & Schema"
Cohesion: 0.07
Nodes (35): Base, Bot/human handover state, Base, get_session(), Async SQLAlchemy 2.0 engine, session factory and declarative Base., Declarative base shared by every ORM model., FastAPI dependency that yields a database session., Base (DeclarativeBase) (+27 more)

### Community 2 - "FastAPI App & Webhook API"
Cohesion: 0.07
Nodes (25): _filter_new_event_ids(), WhatsApp webhook endpoints.  GET  /webhook  - Meta verification handshake (echoe, Meta verification handshake.      Meta calls this once when the webhook is confi, Return the subset of `event_ids` not yet in `processed_events`.      Fail-open:, Receive a webhook event.      GOLDEN RULE: return 200 in well under 5 seconds. O, receive_webhook(), verify_webhook(), BaseSettings (+17 more)

### Community 3 - "Worker Pipeline (Architecture)"
Cohesion: 0.09
Nodes (30): WorkerSettings (arq config), postgres compose service, redis compose service, HandoverManager, PrecheckClient, ProcessedEvent (idempotency ledger), Idempotency ledger pattern, MVP single-tenant auto-provision (+22 more)

### Community 4 - "Webhook Event Processing Tasks"
Cohesion: 0.13
Nodes (23): _event_already_processed(), _extract_sent_wam_id(), _get_or_create_conversation(), _get_or_create_patient(), _handle_human_echoes(), _handle_patient_messages(), _persist_human_echo(), _persist_inbound_message() (+15 more)

### Community 5 - "Google Calendar Integration"
Cohesion: 0.11
Nodes (14): cancel_event(), check_availability(), create_event(), _get_calendar(), LangChain tools for the LangGraph agent.  Each tool wraps a CalendarService meth, Lista eventos que conflitam com o intervalo [start, end) no calendário     da cl, Cria um evento (consulta) no calendário da clínica. Use SOMENTE depois     de ch, Cancela (deleta) um evento existente pelo seu id.      Args:         event_id: I (+6 more)

### Community 6 - "LangGraph ReAct Agent"
Cohesion: 0.11
Nodes (19): build_agent(), invoke_agent(), _load_history(), _looks_like_meta_output(), _prompt_with_today(), LangGraph agent for SecretarIA (Phase 5 / Fase B).  create_react_agent gives us, arq-side entry point: build history + run agent + return reply text.      `messa, Prepend a freshly-rendered system prompt so today's date is current.      Called (+11 more)

### Community 7 - "Logging & Worker Bootstrap"
Cohesion: 0.11
Nodes (15): get_logger(), Structured logging setup using structlog.  JSON output in non-dev environments,, Configure structlog + stdlib logging. Safe to call more than once., Return a bound structlog logger., setup_logging(), Create a development tenant from environment settings.  Usage:     uv run python, Insert a single development tenant if one does not already exist., seed() (+7 more)

### Community 8 - "Meta Signature Verification"
Cohesion: 0.23
Nodes (13): Security helpers - Meta webhook HMAC-SHA256 signature validation.  Meta signs ev, Return True if `signature_header` is a valid HMAC of `raw_body`.      Args:, verify_meta_signature(), Tests for the Meta webhook HMAC-SHA256 signature validation., Build a valid `X-Hub-Signature-256` header value for `body`., _sign(), test_empty_app_secret_is_rejected(), test_invalid_signature_is_rejected() (+5 more)

### Community 9 - "PreCheck Anamnese Client"
Cohesion: 0.18
Nodes (8): PrecheckClient, HTTP client for the Precheck service (a separate FastAPI app).  Precheck runs in, Async client for the Precheck (anamnese) service., Issue an authenticated request, logging structured errors., Start an anamnese (pre-consultation questionnaire) for a patient., # TODO: PRECHECK_CONTRACT_NEEDED - confirmar path e payload com o dono da API, Fetch the result of a previously started anamnese., # TODO: PRECHECK_CONTRACT_NEEDED - confirmar path e payload com o dono da API

### Community 10 - "Webhook Payload Schemas"
Cohesion: 0.24
Nodes (13): BaseModel, Pydantic schemas for the Meta WhatsApp webhook payload.  The models are intentio, WebhookChange, WebhookContact, WebhookContactProfile, WebhookEntry, WebhookMessage, WebhookMetadata (+5 more)

### Community 11 - "Bot/Human Handover State"
Cohesion: 0.17
Nodes (6): HandoverManager, Handover logic - switching a conversation between the bot and a human.  Coexiste, Reads and mutates the handover state of a conversation.      All mutations `flus, True when the bot is allowed to answer automatically., Pause the bot - a human secretary has taken over., True when a HUMAN_ACTIVE conversation has been idle long enough to         hand

### Community 12 - "WhatsApp Cloud API Client"
Cohesion: 0.25
Nodes (6): _extract_message_id(), WhatsApp Cloud API client - sends outbound messages., Pull the wamid from a Cloud API send response, tolerating bad shapes., Async client for the Meta WhatsApp Cloud API.      For the MVP (single tenant) c, Send a plain-text WhatsApp message.          Args:             to: recipient wa_, WhatsAppClient

### Community 13 - "Alembic Migration Env"
Cohesion: 0.29
Nodes (5): Alembic environment - async (asyncpg) configuration., Run migrations in 'offline' mode (emit SQL, no live DB connection)., Run migrations in 'online' mode using an async engine., run_migrations_offline(), run_migrations_online()

### Community 15 - "Health Endpoint"
Cohesion: 0.40
Nodes (4): health(), Health check endpoint., Liveness probe. Intentionally does not touch Postgres or Redis., Health router

### Community 16 - "Test Fixtures"
Cohesion: 0.50
Nodes (3): client(), Shared pytest fixtures and deterministic test environment., An httpx AsyncClient bound to the FastAPI app via ASGITransport.      The app li

### Community 19 - "Test Suite Index"
Cohesion: 0.67
Nodes (3): client fixture (ASGITransport), HMAC signature tests, Webhook endpoint tests

## Ambiguous Edges - Review These
- `Graphify Bash PreToolUse Hook` → `receive_webhook (POST)`  [AMBIGUOUS]
  .claude/settings.json · relation: conceptually_related_to

## Knowledge Gaps
- **19 isolated node(s):** `PreToolUse`, `Graphify Bash PreToolUse Hook`, `run_migrations_offline`, `_do_run_migrations`, `Initial schema downgrade` (+14 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **6 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Graphify Bash PreToolUse Hook` and `receive_webhook (POST)`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `get_settings()` connect `FastAPI App & Webhook API` to `Webhook Event Processing Tasks`, `Google Calendar Integration`, `LangGraph ReAct Agent`, `Logging & Worker Bootstrap`, `PreCheck Anamnese Client`, `Bot/Human Handover State`, `WhatsApp Cloud API Client`?**
  _High betweenness centrality (0.287) - this node is a cross-community bridge._
- **Why does `get_settings (lru_cache)` connect `App Runtime & Agent Core` to `Domain Models & Schema`?**
  _High betweenness centrality (0.219) - this node is a cross-community bridge._
- **Are the 15 inferred relationships involving `get_settings()` (e.g. with `main()` and `main()`) actually correct?**
  _`get_settings()` has 15 INFERRED edges - model-reasoned connections that need verification._
- **Are the 2 inferred relationships involving `get_settings (lru_cache)` (e.g. with `run_migrations_offline` and `run_migrations_online`) actually correct?**
  _`get_settings (lru_cache)` has 2 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `Conversation` (e.g. with `Base` and `HandoverManager`) actually correct?**
  _`Conversation` has 5 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `Message` (e.g. with `Base` and `_persist_inbound_message()`) actually correct?**
  _`Message` has 5 INFERRED edges - model-reasoned connections that need verification._