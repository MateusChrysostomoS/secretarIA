# Graph Report - secretarIA  (2026-05-26)

## Corpus Check
- 43 files · ~9,367 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 373 nodes · 471 edges · 34 communities (28 shown, 6 thin omitted)
- Extraction: 80% EXTRACTED · 20% INFERRED · 0% AMBIGUOUS · INFERRED: 95 edges (avg confidence: 0.77)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `20dedb2b`
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
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]

## God Nodes (most connected - your core abstractions)
1. `get_settings()` - 19 edges
2. `CalendarService` - 13 edges
3. `Conversation` - 12 edges
4. `Message` - 12 edges
5. `HandoverManager` - 12 edges
6. `Base` - 11 edges
7. `_persist_inbound_message()` - 11 edges
8. `SecretarIA` - 11 edges
9. `verify_meta_signature()` - 10 edges
10. `_persist_human_echo()` - 10 edges

## Surprising Connections (you probably didn't know these)
- `Graphify Bash PreToolUse Hook` --conceptually_related_to--> `receive_webhook (POST)`  [AMBIGUOUS]
  .claude/settings.json → src/secretaria/api/webhook.py
- `WhatsApp Coexistence mode` --rationale_for--> `HandoverManager`  [INFERRED]
  README.md → src/secretaria/services/handover.py
- `Handover via smb_message_echoes` --rationale_for--> `_handle_human_echoes`  [INFERRED]
  README.md → src/secretaria/workers/tasks.py
- `main()` --calls--> `get_settings()`  [INFERRED]
  scripts/gcal_auth.py → src/secretaria/config.py
- `seed()` --calls--> `get_settings()`  [INFERRED]
  scripts/seed_dev.py → src/secretaria/config.py

## Hyperedges (group relationships)
- **WhatsApp webhook ingest pipeline** — webhook_receive_webhook, security_verify_meta_signature, webhook_filter_new_event_ids, concept_arq_enqueue_offload [INFERRED 0.85]
- **Patient/Conversation/Message data model** — models_patient_patient, models_conversation_conversation, models_message_message, database_base [EXTRACTED 1.00]
- **Alembic async migration setup** — env_run_migrations_online, env_do_run_migrations, initial_schema_upgrade, database_base [INFERRED 0.85]
- **Idempotent webhook processing pipeline** — tasks_process_webhook_event, tasks_event_already_processed, processed_event_ProcessedEvent, webhook_iter_event_ids [INFERRED 0.85]
- **Coexistence handover (echoes pause bot)** — tasks_handle_human_echoes, tasks_persist_human_echo, handover_HandoverManager, readme_coexistence_mode [INFERRED 0.85]
- **Inbound message persist-and-reply flow** — tasks_handle_patient_messages, tasks_persist_inbound_message, tasks_send_bot_reply, whatsapp_WhatsAppClient, tasks_ReplyContext [INFERRED 0.85]

## Communities (34 total, 6 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.06
Nodes (48): Base, Bot/human handover state, Base, Declarative base shared by every ORM model., Base (DeclarativeBase), DeclarativeBase, Initial schema upgrade, Conversation (+40 more)

### Community 1 - "Community 1"
Cohesion: 0.07
Nodes (25): BaseSettings, main(), Diagnose Fase A auth: what scopes does our refresh token actually carry?  A refr, _short(), main(), Generate a Google Calendar refresh token for Fase A (single tenant).  One-shot C, get_settings(), Application configuration loaded from environment variables / .env file. (+17 more)

### Community 2 - "Community 2"
Cohesion: 0.09
Nodes (28): health(), Health check endpoint., Liveness probe. Intentionally does not touch Postgres or Redis., Health router, Async offload via arq queue, HMAC-SHA256 webhook signature verification, Webhook idempotency fast-path, Multi-tenant routing by phone_number_id (+20 more)

### Community 3 - "Community 3"
Cohesion: 0.09
Nodes (32): WorkerSettings (arq config), CalendarService (stub), graphify workflow rules, postgres compose service, redis compose service, HandoverManager, PrecheckClient, ProcessedEvent (idempotency ledger) (+24 more)

### Community 4 - "Community 4"
Cohesion: 0.11
Nodes (19): 1. Install dependencies, 2. Create your .env, 3. Start Postgres + Redis, 4. Apply database migrations, 5. (optional) Seed a development tenant, 6. Run the API, 7. Run the worker (in a second terminal), code:sh (make install            # or: uv sync) (+11 more)

### Community 5 - "Community 5"
Cohesion: 0.11
Nodes (15): get_logger(), Structured logging setup using structlog.  JSON output in non-dev environments,, Configure structlog + stdlib logging. Safe to call more than once., Return a bound structlog logger., setup_logging(), Create a development tenant from environment settings.  Usage:     uv run python, Insert a single development tenant if one does not already exist., seed() (+7 more)

### Community 6 - "Community 6"
Cohesion: 0.23
Nodes (13): Security helpers - Meta webhook HMAC-SHA256 signature validation.  Meta signs ev, Return True if `signature_header` is a valid HMAC of `raw_body`.      Args:, verify_meta_signature(), Tests for the Meta webhook HMAC-SHA256 signature validation., Build a valid `X-Hub-Signature-256` header value for `body`., _sign(), test_empty_app_secret_is_rejected(), test_invalid_signature_is_rejected() (+5 more)

### Community 7 - "Community 7"
Cohesion: 0.24
Nodes (13): BaseModel, Pydantic schemas for the Meta WhatsApp webhook payload.  The models are intentio, WebhookChange, WebhookContact, WebhookContactProfile, WebhookEntry, WebhookMessage, WebhookMetadata (+5 more)

### Community 8 - "Community 8"
Cohesion: 0.12
Nodes (16): Architecture, code:sh (make makemigration m="describe the change"   # autogenerate ), code:block11 (uvicorn secretaria.main:app --host 0.0.0.0 --port 8000), code:block12 (arq secretaria.workers.arq_worker.WorkerSettings), code:block13 (DATABASE_URL=postgresql+asyncpg://USER:PASS@secretaria-postg), code:block14 (src/secretaria/), Connecting the Meta webhook, Database migrations (+8 more)

### Community 9 - "Community 9"
Cohesion: 0.09
Nodes (28): build_agent(), invoke_agent(), _load_history(), LangGraph agent for SecretarIA (Phase 5 / Fase B).  create_react_agent gives us, arq-side entry point: build history + run agent + return reply text.      `messa, Run the conversational agent for a single inbound message.      STUB: returns a, Compile the ReAct agent. Idempotent: cached process-wide., Run the agent on a message list, return the last assistant reply.      Shared by (+20 more)

### Community 10 - "Community 10"
Cohesion: 0.17
Nodes (6): HandoverManager, Handover logic - switching a conversation between the bot and a human.  Coexiste, Reads and mutates the handover state of a conversation.      All mutations `flus, True when the bot is allowed to answer automatically., Pause the bot - a human secretary has taken over., True when a HUMAN_ACTIVE conversation has been idle long enough to         hand

### Community 11 - "Community 11"
Cohesion: 0.12
Nodes (10): CalendarService, Google Calendar integration for the clinic.  Fase A (single tenant): credentials, Stub interface for the clinic's Google Calendar., Create an event on the clinic's calendar. Returns the inserted event., Return bookable time slots for a given day., Delete an event by id. 404/410 are treated as success (idempotent)., Create a calendar event (an appointment)., Cancel a previously created event. (+2 more)

### Community 12 - "Community 12"
Cohesion: 0.29
Nodes (5): Alembic environment - async (asyncpg) configuration., Run migrations in 'offline' mode (emit SQL, no live DB connection)., Run migrations in 'online' mode using an async engine., run_migrations_offline(), run_migrations_online()

### Community 14 - "Community 14"
Cohesion: 0.22
Nodes (9): _filter_new_event_ids(), WhatsApp webhook endpoints.  GET  /webhook  - Meta verification handshake (echoe, Meta verification handshake.      Meta calls this once when the webhook is confi, Return the subset of `event_ids` not yet in `processed_events`.      Fail-open:, Receive a webhook event.      GOLDEN RULE: return 200 in well under 5 seconds. O, receive_webhook(), verify_webhook(), iter_event_ids() (+1 more)

### Community 15 - "Community 15"
Cohesion: 0.50
Nodes (3): client(), Shared pytest fixtures and deterministic test environment., An httpx AsyncClient bound to the FastAPI app via ASGITransport.      The app li

### Community 17 - "Community 17"
Cohesion: 0.67
Nodes (3): client fixture (ASGITransport), HMAC signature tests, Webhook endpoint tests

### Community 29 - "Community 29"
Cohesion: 0.25
Nodes (6): _extract_message_id(), WhatsApp Cloud API client - sends outbound messages., Pull the wamid from a Cloud API send response, tolerating bad shapes., Async client for the Meta WhatsApp Cloud API.      For the MVP (single tenant) c, Send a plain-text WhatsApp message.          Args:             to: recipient wa_, WhatsAppClient

### Community 30 - "Community 30"
Cohesion: 0.29
Nodes (5): MessageDirection, MessageSender, Message model - a single message inside a conversation., Relative to the clinic's WhatsApp number., Who authored the message.

### Community 31 - "Community 31"
Cohesion: 0.33
Nodes (5): _prompt_with_today(), Prepend a freshly-rendered system prompt so today's date is current.      Called, System prompts for the SecretarIA conversational agent.  Lives outside ai/graph., Eye Company secretary prompt. `tz` is the IANA clinic timezone., secretary_system_prompt()

### Community 32 - "Community 32"
Cohesion: 0.50
Nodes (3): get_session(), Async SQLAlchemy 2.0 engine, session factory and declarative Base., FastAPI dependency that yields a database session.

## Ambiguous Edges - Review These
- `Graphify Bash PreToolUse Hook` → `receive_webhook (POST)`  [AMBIGUOUS]
  .claude/settings.json · relation: conceptually_related_to

## Knowledge Gaps
- **36 isolated node(s):** `PreToolUse`, `graphify`, `Architecture`, `Tech stack`, `Windows notes` (+31 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **6 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Graphify Bash PreToolUse Hook` and `receive_webhook (POST)`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `get_settings()` connect `Community 1` to `Community 0`, `Community 5`, `Community 9`, `Community 10`, `Community 11`, `Community 14`, `Community 29`, `Community 31`?**
  _High betweenness centrality (0.242) - this node is a cross-community bridge._
- **Why does `Message` connect `Community 0` to `Community 30`?**
  _High betweenness centrality (0.106) - this node is a cross-community bridge._
- **Why does `Base (DeclarativeBase)` connect `Community 0` to `Community 2`?**
  _High betweenness centrality (0.099) - this node is a cross-community bridge._
- **Are the 15 inferred relationships involving `get_settings()` (e.g. with `main()` and `main()`) actually correct?**
  _`get_settings()` has 15 INFERRED edges - model-reasoned connections that need verification._
- **Are the 2 inferred relationships involving `CalendarService` (e.g. with `Settings` and `_get_calendar()`) actually correct?**
  _`CalendarService` has 2 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `Conversation` (e.g. with `Base` and `HandoverManager`) actually correct?**
  _`Conversation` has 5 INFERRED edges - model-reasoned connections that need verification._