# Graph Report - secretarIA  (2026-06-01)

## Corpus Check
- 46 files · ~12,740 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 477 nodes · 625 edges · 36 communities (30 shown, 6 thin omitted)
- Extraction: 80% EXTRACTED · 20% INFERRED · 0% AMBIGUOUS · INFERRED: 122 edges (avg confidence: 0.78)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `0e75d10f`
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

## God Nodes (most connected - your core abstractions)
1. `parse()` - 23 edges
2. `get_settings()` - 18 edges
3. `get_settings (lru_cache)` - 14 edges
4. `Conversation` - 12 edges
5. `Message` - 12 edges
6. `CalendarService` - 12 edges
7. `HandoverManager` - 12 edges
8. `_persist_inbound_message()` - 12 edges
9. `Base` - 11 edges
10. `_ReplyContext` - 11 edges

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

## Communities (36 total, 6 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.06
Nodes (51): CalendarService._build_service, CalendarService.cancel_event, CalendarService.check_availability, CalendarService.create_event, SCOPES = calendar.events, CalendarService, check_scopes.main, check_scopes._short (+43 more)

### Community 1 - "Community 1"
Cohesion: 0.07
Nodes (35): Base, Bot/human handover state, Base, get_session(), Async SQLAlchemy 2.0 engine, session factory and declarative Base., Declarative base shared by every ORM model., FastAPI dependency that yields a database session., Base (DeclarativeBase) (+27 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (30): _filter_new_event_ids(), WhatsApp webhook endpoints.  GET  /webhook  - Meta verification handshake (echoe, Meta verification handshake.      Meta calls this once when the webhook is confi, Return the subset of `event_ids` not yet in `processed_events`.      Fail-open:, Receive a webhook event.      GOLDEN RULE: return 200 in well under 5 seconds. O, receive_webhook(), verify_webhook(), BaseSettings (+22 more)

### Community 3 - "Community 3"
Cohesion: 0.09
Nodes (30): WorkerSettings (arq config), postgres compose service, redis compose service, HandoverManager, PrecheckClient, ProcessedEvent (idempotency ledger), Idempotency ledger pattern, MVP single-tenant auto-provision (+22 more)

### Community 4 - "Community 4"
Cohesion: 0.07
Nodes (38): _bubble_history_body(), _event_already_processed(), _extract_sent_wam_id(), _get_or_create_conversation(), _get_or_create_patient(), _handle_human_echoes(), _handle_patient_messages(), _persist_human_echo() (+30 more)

### Community 5 - "Community 5"
Cohesion: 0.15
Nodes (9): CalendarService, Google Calendar integration for the clinic.  Fase A (single tenant): credentials, Create an event on the clinic's calendar. Returns the inserted event., Return free [start, end) slots on `day` within business hours.          Walks th, Delete an event by id. 404/410 are treated as success (idempotent)., Create an event on the clinic's calendar. Returns the inserted event., Delete an event by id. 404/410 are treated as success (idempotent)., Async wrapper around the (sync) Google Calendar v3 API. (+1 more)

### Community 6 - "Community 6"
Cohesion: 0.11
Nodes (20): build_agent(), invoke_agent(), _load_history(), _looks_like_meta_output(), _prompt_with_today(), LangGraph agent for SecretarIA (Phase 5 / Fase B).  create_react_agent gives us, Reconstruct LangChain message history from the DB.      Pulls the most recent HI, arq-side entry point: build history + run agent + return reply text.      `messa (+12 more)

### Community 7 - "Community 7"
Cohesion: 0.11
Nodes (15): get_logger(), Structured logging setup using structlog.  JSON output in non-dev environments,, Configure structlog + stdlib logging. Safe to call more than once., Return a bound structlog logger., setup_logging(), Create a development tenant from environment settings.  Usage:     uv run python, Insert a single development tenant if one does not already exist., seed() (+7 more)

### Community 8 - "Community 8"
Cohesion: 0.23
Nodes (13): Security helpers - Meta webhook HMAC-SHA256 signature validation.  Meta signs ev, Return True if `signature_header` is a valid HMAC of `raw_body`.      Args:, verify_meta_signature(), Tests for the Meta webhook HMAC-SHA256 signature validation., Build a valid `X-Hub-Signature-256` header value for `body`., _sign(), test_empty_app_secret_is_rejected(), test_invalid_signature_is_rejected() (+5 more)

### Community 9 - "Community 9"
Cohesion: 0.18
Nodes (8): PrecheckClient, HTTP client for the Precheck service (a separate FastAPI app).  Precheck runs in, Async client for the Precheck (anamnese) service., Issue an authenticated request, logging structured errors., Start an anamnese (pre-consultation questionnaire) for a patient., # TODO: PRECHECK_CONTRACT_NEEDED - confirmar path e payload com o dono da API, Fetch the result of a previously started anamnese., # TODO: PRECHECK_CONTRACT_NEEDED - confirmar path e payload com o dono da API

### Community 10 - "Community 10"
Cohesion: 0.21
Nodes (15): BaseModel, Pydantic schemas for the Meta WhatsApp webhook payload.  The models are intentio, Common shape for both `button_reply` and `list_reply` sub-objects., Container the patient sends back after tapping an interactive control.      `typ, WebhookChange, WebhookContact, WebhookContactProfile, WebhookEntry (+7 more)

### Community 11 - "Community 11"
Cohesion: 0.17
Nodes (6): HandoverManager, Handover logic - switching a conversation between the bot and a human.  Coexiste, Reads and mutates the handover state of a conversation.      All mutations `flus, True when the bot is allowed to answer automatically., Pause the bot - a human secretary has taken over., True when a HUMAN_ACTIVE conversation has been idle long enough to         hand

### Community 12 - "Community 12"
Cohesion: 0.18
Nodes (9): _extract_message_id(), WhatsApp Cloud API client - sends outbound messages., Pull the wamid from a Cloud API send response, tolerating bad shapes., Send an interactive list message (max 10 rows in one section).          Args:, Async client for the Meta WhatsApp Cloud API.      For the MVP (single tenant) c, Send a plain-text WhatsApp message.          Args:             to: recipient wa_, Send a plain-text WhatsApp message.          Args:             to: recipient wa_, Send an interactive reply-button message (max 3 buttons).          Args: (+1 more)

### Community 13 - "Community 13"
Cohesion: 0.29
Nodes (5): Alembic environment - async (asyncpg) configuration., Run migrations in 'offline' mode (emit SQL, no live DB connection)., Run migrations in 'online' mode using an async engine., run_migrations_offline(), run_migrations_online()

### Community 15 - "Community 15"
Cohesion: 0.40
Nodes (4): health(), Health check endpoint., Liveness probe. Intentionally does not touch Postgres or Redis., Health router

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
Nodes (32): ButtonBubble, _clean(), _finalise(), parse(), _parse_slot_rows(), _pop_preceding_text_for(), Parse the agent's text output into a sequence of WhatsApp message bubbles.  The, Split a free-text chunk into one or more text bubbles on `---`. (+24 more)

### Community 33 - "Community 33"
Cohesion: 0.21
Nodes (12): cancel_event(), check_availability(), create_event(), _get_calendar(), list_free_slots(), LangChain tools for the LangGraph agent.  Each tool wraps a CalendarService meth, Lista eventos que conflitam com o intervalo [start, end) no calendário     da cl, Lista até `max_slots` horários livres de 30 minutos em `day`, dentro do     horá (+4 more)

### Community 34 - "Community 34"
Cohesion: 0.31
Nodes (9): extract_inbound_body(), Return the human-readable text body of an inbound message.      Handles text mes, Tests for the webhook payload parser, focused on interactive replies., test_empty_interactive_yields_none(), test_extract_button_reply(), test_extract_list_reply_non_slot_id_falls_back_to_title(), test_extract_list_reply_slot_includes_iso_in_body(), test_extract_text_body() (+1 more)

### Community 35 - "Community 35"
Cohesion: 0.22
Nodes (8): Auxiliary scripts (Fase A scaffolding), graphify, Implementation status, Per-tenant config (the rows we must grow), Product vision, SecretarIA, The hardcoded "Eye Company" is placeholder test data, What the agent needs to become

## Ambiguous Edges - Review These
- `Graphify Bash PreToolUse Hook` → `receive_webhook (POST)`  [AMBIGUOUS]
  .claude/settings.json · relation: conceptually_related_to

## Knowledge Gaps
- **46 isolated node(s):** `PreToolUse`, `Product vision`, `The hardcoded "Eye Company" is placeholder test data`, `Per-tenant config (the rows we must grow)`, `What the agent needs to become` (+41 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **6 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Graphify Bash PreToolUse Hook` and `receive_webhook (POST)`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `get_settings()` connect `Community 2` to `Community 4`, `Community 6`, `Community 7`, `Community 9`, `Community 11`, `Community 12`?**
  _High betweenness centrality (0.211) - this node is a cross-community bridge._
- **Why does `_send_bot_reply()` connect `Community 4` to `Community 32`, `Community 1`, `Community 12`, `Community 6`?**
  _High betweenness centrality (0.163) - this node is a cross-community bridge._
- **Why does `get_settings (lru_cache)` connect `Community 0` to `Community 1`?**
  _High betweenness centrality (0.158) - this node is a cross-community bridge._
- **Are the 13 inferred relationships involving `parse()` (e.g. with `_send_bot_reply()` and `test_empty_reply_yields_no_bubbles()`) actually correct?**
  _`parse()` has 13 INFERRED edges - model-reasoned connections that need verification._
- **Are the 15 inferred relationships involving `get_settings()` (e.g. with `main()` and `main()`) actually correct?**
  _`get_settings()` has 15 INFERRED edges - model-reasoned connections that need verification._
- **Are the 2 inferred relationships involving `get_settings (lru_cache)` (e.g. with `run_migrations_offline` and `run_migrations_online`) actually correct?**
  _`get_settings (lru_cache)` has 2 INFERRED edges - model-reasoned connections that need verification._