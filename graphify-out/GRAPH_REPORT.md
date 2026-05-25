# Graph Report - .  (2026-05-25)

## Corpus Check
- Corpus is ~6,777 words - fits in a single context window. You may not need a graph.

## Summary
- 293 nodes · 376 edges · 29 communities (24 shown, 5 thin omitted)
- Extraction: 77% EXTRACTED · 23% INFERRED · 0% AMBIGUOUS · INFERRED: 85 edges (avg confidence: 0.78)
- Token cost: 86,453 input · 11,656 output

## Community Hubs (Navigation)
- [[_COMMUNITY_ORM Models & Base|ORM Models & Base]]
- [[_COMMUNITY_Webhook Handler & Idempotency|Webhook Handler & Idempotency]]
- [[_COMMUNITY_Config, Security & Health|Config, Security & Health]]
- [[_COMMUNITY_Architecture Concepts & Stubs|Architecture Concepts & Stubs]]
- [[_COMMUNITY_Worker Job Pipeline|Worker Job Pipeline]]
- [[_COMMUNITY_Logging & Dev Seed|Logging & Dev Seed]]
- [[_COMMUNITY_HMAC Security & Tests|HMAC Security & Tests]]
- [[_COMMUNITY_Pydantic Webhook Schemas|Pydantic Webhook Schemas]]
- [[_COMMUNITY_Precheck Service Client|Precheck Service Client]]
- [[_COMMUNITY_AI Graph & Tools (Stub)|AI Graph & Tools (Stub)]]
- [[_COMMUNITY_Handover Manager|Handover Manager]]
- [[_COMMUNITY_Google Calendar (Stub)|Google Calendar (Stub)]]
- [[_COMMUNITY_Alembic Migration Env|Alembic Migration Env]]
- [[_COMMUNITY_Webhook & Health Tests|Webhook & Health Tests]]
- [[_COMMUNITY_Deployment & Worker Config|Deployment & Worker Config]]
- [[_COMMUNITY_Pytest Fixtures|Pytest Fixtures]]
- [[_COMMUNITY_Claude Code Hooks|Claude Code Hooks]]
- [[_COMMUNITY_Test Suite Index|Test Suite Index]]
- [[_COMMUNITY_Models Package Init|Models Package Init]]
- [[_COMMUNITY_Package Root|Package Root]]
- [[_COMMUNITY_Migration Downgrade|Migration Downgrade]]

## God Nodes (most connected - your core abstractions)
1. `get_settings()` - 12 edges
2. `Conversation` - 12 edges
3. `Message` - 12 edges
4. `HandoverManager` - 12 edges
5. `Base` - 11 edges
6. `_persist_inbound_message()` - 11 edges
7. `verify_meta_signature()` - 10 edges
8. `_persist_human_echo()` - 10 edges
9. `get_settings` - 9 edges
10. `Patient` - 8 edges

## Surprising Connections (you probably didn't know these)
- `Graphify Bash PreToolUse Hook` --conceptually_related_to--> `receive_webhook (POST)`  [AMBIGUOUS]
  .claude/settings.json → src/secretaria/api/webhook.py
- `WhatsApp Coexistence mode` --rationale_for--> `HandoverManager`  [INFERRED]
  README.md → src/secretaria/services/handover.py
- `Handover via smb_message_echoes` --rationale_for--> `_handle_human_echoes`  [INFERRED]
  README.md → src/secretaria/workers/tasks.py
- `seed()` --calls--> `get_settings()`  [INFERRED]
  scripts/seed_dev.py → src/secretaria/config.py
- `seed()` --calls--> `Tenant`  [INFERRED]
  scripts/seed_dev.py → src/secretaria/models/tenant.py

## Hyperedges (group relationships)
- **WhatsApp webhook ingest pipeline** — webhook_receive_webhook, security_verify_meta_signature, webhook_filter_new_event_ids, concept_arq_enqueue_offload [INFERRED 0.85]
- **Patient/Conversation/Message data model** — models_patient_patient, models_conversation_conversation, models_message_message, database_base [EXTRACTED 1.00]
- **Alembic async migration setup** — env_run_migrations_online, env_do_run_migrations, initial_schema_upgrade, database_base [INFERRED 0.85]
- **Idempotent webhook processing pipeline** — tasks_process_webhook_event, tasks_event_already_processed, processed_event_ProcessedEvent, webhook_iter_event_ids [INFERRED 0.85]
- **Coexistence handover (echoes pause bot)** — tasks_handle_human_echoes, tasks_persist_human_echo, handover_HandoverManager, readme_coexistence_mode [INFERRED 0.85]
- **Inbound message persist-and-reply flow** — tasks_handle_patient_messages, tasks_persist_inbound_message, tasks_send_bot_reply, whatsapp_WhatsAppClient, tasks_ReplyContext [INFERRED 0.85]

## Communities (29 total, 5 thin omitted)

### Community 0 - "ORM Models & Base"
Cohesion: 0.07
Nodes (33): Base, Bot/human handover state, Base, get_session(), Async SQLAlchemy 2.0 engine, session factory and declarative Base., Declarative base shared by every ORM model., FastAPI dependency that yields a database session., Base (DeclarativeBase) (+25 more)

### Community 1 - "Webhook Handler & Idempotency"
Cohesion: 0.07
Nodes (26): _filter_new_event_ids(), WhatsApp webhook endpoints.  GET  /webhook  - Meta verification handshake (echoe, Meta verification handshake.      Meta calls this once when the webhook is confi, Return the subset of `event_ids` not yet in `processed_events`.      Fail-open:, Receive a webhook event.      GOLDEN RULE: return 200 in well under 5 seconds. O, receive_webhook(), verify_webhook(), BaseSettings (+18 more)

### Community 2 - "Config, Security & Health"
Cohesion: 0.09
Nodes (28): health(), Health check endpoint., Liveness probe. Intentionally does not touch Postgres or Redis., Health router, Async offload via arq queue, HMAC-SHA256 webhook signature verification, Webhook idempotency fast-path, Multi-tenant routing by phone_number_id (+20 more)

### Community 3 - "Architecture Concepts & Stubs"
Cohesion: 0.11
Nodes (26): CalendarService (stub), HandoverManager, PrecheckClient, ProcessedEvent (idempotency ledger), Idempotency ledger pattern, MVP single-tenant auto-provision, Permissive Pydantic schemas (extra=allow), WhatsApp Coexistence mode (+18 more)

### Community 4 - "Worker Job Pipeline"
Cohesion: 0.13
Nodes (23): _event_already_processed(), _extract_sent_wam_id(), _get_or_create_conversation(), _get_or_create_patient(), _handle_human_echoes(), _handle_patient_messages(), _persist_human_echo(), _persist_inbound_message() (+15 more)

### Community 5 - "Logging & Dev Seed"
Cohesion: 0.11
Nodes (15): get_logger(), Structured logging setup using structlog.  JSON output in non-dev environments,, Configure structlog + stdlib logging. Safe to call more than once., Return a bound structlog logger., setup_logging(), Create a development tenant from environment settings.  Usage:     uv run python, Insert a single development tenant if one does not already exist., seed() (+7 more)

### Community 6 - "HMAC Security & Tests"
Cohesion: 0.23
Nodes (13): Security helpers - Meta webhook HMAC-SHA256 signature validation.  Meta signs ev, Return True if `signature_header` is a valid HMAC of `raw_body`.      Args:, verify_meta_signature(), Tests for the Meta webhook HMAC-SHA256 signature validation., Build a valid `X-Hub-Signature-256` header value for `body`., _sign(), test_empty_app_secret_is_rejected(), test_invalid_signature_is_rejected() (+5 more)

### Community 7 - "Pydantic Webhook Schemas"
Cohesion: 0.24
Nodes (13): BaseModel, Pydantic schemas for the Meta WhatsApp webhook payload.  The models are intentio, WebhookChange, WebhookContact, WebhookContactProfile, WebhookEntry, WebhookMessage, WebhookMetadata (+5 more)

### Community 8 - "Precheck Service Client"
Cohesion: 0.18
Nodes (8): PrecheckClient, HTTP client for the Precheck service (a separate FastAPI app).  Precheck runs in, Async client for the Precheck (anamnese) service., Issue an authenticated request, logging structured errors., Start an anamnese (pre-consultation questionnaire) for a patient., # TODO: PRECHECK_CONTRACT_NEEDED - confirmar path e payload com o dono da API, Fetch the result of a previously started anamnese., # TODO: PRECHECK_CONTRACT_NEEDED - confirmar path e payload com o dono da API

### Community 9 - "AI Graph & Tools (Stub)"
Cohesion: 0.21
Nodes (10): LangGraph orchestration - STUB.  Placeholder for the conversational AI graph. Th, Run the conversational agent for a single inbound message.      STUB: returns a, run_agent(), book_appointment(), check_calendar_availability(), LangGraph tools - STUBS.  These will become real LangChain tools bound to the AI, Tool: list free appointment slots for a given day (ISO date string)., Tool: book an appointment slot for a patient. (+2 more)

### Community 10 - "Handover Manager"
Cohesion: 0.17
Nodes (6): HandoverManager, Handover logic - switching a conversation between the bot and a human.  Coexiste, Reads and mutates the handover state of a conversation.      All mutations `flus, True when the bot is allowed to answer automatically., Pause the bot - a human secretary has taken over., True when a HUMAN_ACTIVE conversation has been idle long enough to         hand

### Community 11 - "Google Calendar (Stub)"
Cohesion: 0.20
Nodes (6): CalendarService, Google Calendar integration - STUB.  The real implementation (OAuth, free/busy q, Stub interface for the clinic's Google Calendar., Return bookable time slots for a given day., Create a calendar event (an appointment)., Cancel a previously created event.

### Community 12 - "Alembic Migration Env"
Cohesion: 0.29
Nodes (5): Alembic environment - async (asyncpg) configuration., Run migrations in 'offline' mode (emit SQL, no live DB connection)., Run migrations in 'online' mode using an async engine., run_migrations_offline(), run_migrations_online()

### Community 14 - "Deployment & Worker Config"
Cohesion: 0.33
Nodes (6): WorkerSettings (arq config), graphify workflow rules, postgres compose service, redis compose service, SecretarIA architecture overview, Easypanel deployment

### Community 15 - "Pytest Fixtures"
Cohesion: 0.50
Nodes (3): client(), Shared pytest fixtures and deterministic test environment., An httpx AsyncClient bound to the FastAPI app via ASGITransport.      The app li

### Community 17 - "Test Suite Index"
Cohesion: 0.67
Nodes (3): client fixture (ASGITransport), HMAC signature tests, Webhook endpoint tests

## Ambiguous Edges - Review These
- `Graphify Bash PreToolUse Hook` → `receive_webhook (POST)`  [AMBIGUOUS]
  .claude/settings.json · relation: conceptually_related_to

## Knowledge Gaps
- **15 isolated node(s):** `PreToolUse`, `Graphify Bash PreToolUse Hook`, `run_migrations_offline`, `_do_run_migrations`, `Initial schema downgrade` (+10 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **5 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Graphify Bash PreToolUse Hook` and `receive_webhook (POST)`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `get_settings()` connect `Webhook Handler & Idempotency` to `Precheck Service Client`, `Handover Manager`, `Worker Job Pipeline`, `Logging & Dev Seed`?**
  _High betweenness centrality (0.203) - this node is a cross-community bridge._
- **Why does `Base (DeclarativeBase)` connect `ORM Models & Base` to `Config, Security & Health`?**
  _High betweenness centrality (0.125) - this node is a cross-community bridge._
- **Why does `Message` connect `ORM Models & Base` to `Worker Job Pipeline`?**
  _High betweenness centrality (0.124) - this node is a cross-community bridge._
- **Are the 9 inferred relationships involving `get_settings()` (e.g. with `seed()` and `lifespan()`) actually correct?**
  _`get_settings()` has 9 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `Conversation` (e.g. with `Base` and `HandoverManager`) actually correct?**
  _`Conversation` has 5 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `Message` (e.g. with `Base` and `_persist_inbound_message()`) actually correct?**
  _`Message` has 5 INFERRED edges - model-reasoned connections that need verification._