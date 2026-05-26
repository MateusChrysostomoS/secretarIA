# SecretarIA

## Product vision

SecretarIA is a **multi-tenant SaaS** that drops a conversational appointment-booking secretary into any service business's existing WhatsApp number, via the WhatsApp **Cloud API Coexistence** model (bot and human agent share the same line, the bot stays quiet when the human picks up). Initial target market is clinics, but the product is service-agnostic — clients self-schedule with the business through their own WhatsApp.

Every tenant brings its own Google Calendar (OAuth), business hours, language, services catalogue and welcome pitch. The product is **sold by adapting all of those to the customer**.

## The hardcoded "Eye Company" is placeholder test data

`src/secretaria/ai/prompts.py` currently hardcodes an Eye Company / Dr. Mateus Chrysóstomo pitch, weekday 08-18 hours, 30-minute consults and Portuguese language. **That string belongs on the tenant row, not in code.** Same for hours, consult duration, services list, and language. Treat the current prompt as a single-tenant dev scaffold validated end-to-end, NOT the shipped behaviour.

When extending the agent, do not add more hardcoded clinic-specific facts to `prompts.py`. Anything new that varies per business goes on the `Tenant` model.

## Per-tenant config (the rows we must grow)

Shipped product configures each tenant with at minimum:

- **Identity**: business name, founder/owner, positioning paragraph (= welcome pitch)
- **Services**: list of bookable services, each with its own duration (and price, channel routing rules, etc.)
- **Business hours**: per weekday + holiday/exception calendar
- **Timezone**: IANA, e.g. America/Sao_Paulo
- **Language**: conversation language (pt-BR default, en, es, ...)
- **Google Calendar**: encrypted `refresh_token` + `calendar_id` (per tenant, obtained via a hosted OAuth onboarding flow, not the dev `scripts/gcal_auth.py`)
- **WhatsApp Coexistence**: `phone_number_id` + `access_token` (already on `tenants`)

Today `Tenant` carries only the WhatsApp credentials. Everything above is open work: schema migration, onboarding flow, encryption at rest, and reads in `run_agent` / `secretary_system_prompt`.

## What the agent needs to become

`secretary_system_prompt(tz)` (currently a pure function of timezone) must become `secretary_system_prompt(tenant)` and render every business-specific fact from the tenant row. `ai/graph.py:run_agent` already loads conversation history per `conversation_id`; it must also load the matching tenant row and thread it through to the prompt + tool config (e.g., per-tenant calendar credentials inside `services/calendar.py`).

## Implementation status

- **Fase A** (terminal OpenAI + Google Calendar tool loop) — validated via `scripts/test_agent.py`.
- **Fase B** (LangGraph ReAct agent inside the arq worker) — code complete and imports clean, end-to-end WhatsApp test pending.
- **Multi-tenant adaptation** — not started. Blocks production.
- **Encryption at rest** — `Tenant.access_token` is plaintext today; future `google_refresh_token` column must be encrypted (Fernet / KMS / Vault) before any real customer.

## Auxiliary scripts (Fase A scaffolding)

- `scripts/gcal_auth.py` — single-tenant Google OAuth token generator. **Will be replaced** by a hosted onboarding web flow for the multi-tenant product.
- `scripts/test_agent.py` — dev terminal that exercises the same LangGraph agent the worker uses, with in-memory history.
- `scripts/check_scopes.py` — OAuth scope/Calendar diagnostic when Google returns 403.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
