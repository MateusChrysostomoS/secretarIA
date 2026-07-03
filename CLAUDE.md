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
- **Multi-tenant adaptation** — LARGELY DONE (plugin round, docs/CHECKPOINT_plugins.md): per-tenant config/calendar/WhatsApp-token on the whole reply path, entitlement-gated bot + capability plugins (registry in `src/secretaria/plugins/`). Remaining: hosted OAuth onboarding polish + outbound rate limiter.
- **Encryption at rest** — DONE. Both tenant secrets are Fernet ciphertext in `tenant_credentials` (`google_refresh_token_encrypted`, `waba_token_encrypted`); `Tenant` carries no secret column (migration `d7e8f9a0b1c2` moved + dropped `access_token`). Decryption happens ONLY in `services/tenant_config.py` (`get_google_refresh_token` / `get_waba_token`); structlog runs a `redact_secrets` processor. Requires `ENCRYPTION_KEY`.

## Auxiliary scripts (Fase A scaffolding)

- `scripts/gcal_auth.py` — single-tenant Google OAuth token generator. **Will be replaced** by a hosted onboarding web flow for the multi-tenant product.
- `scripts/test_agent.py` — dev terminal that exercises the same LangGraph agent the worker uses, with in-memory history.
- `scripts/check_scopes.py` — OAuth scope/Calendar diagnostic when Google returns 403.

## Code conventions & project structure

These are the standing rules for how code is organized in this repo. Apply them to every change.

### Layering (where code belongs)
The request flows in one direction — keep it that way:
`api/` (HTTP) → `workers/` (orchestration) → `services/` + `ai/` (business logic) → `models/` (ORM) → `core/` (infra).

- **`api/`** — thin HTTP layer only: parse/validate input, call a service, shape the response. No business logic, no DB transactions beyond trivial reads. Each module exposes one `APIRouter` registered in `main.py`.
- **`workers/`** — async orchestration (arq jobs). `tasks.py` decides *which brain* answers; it does not contain calendar/whatsapp logic itself — it calls `services/`.
- **`services/`** — business logic, reusable by both brains (flow router AND the LLM agent). Calendar/availability logic lives ONLY in `services/calendar.py`.
- **`ai/`** — everything LLM-specific (the agent, prompts, tools, formatter). No business-specific clinic facts here (see the Eye Company rule above).
- **`core/`** — framework-agnostic infra (db engine, crypto, security, logging). Must not import from `api/`, `services/`, or `ai/`.

### Folder granularity — group by domain, not "one folder per file"
A flat package of route modules is the idiomatic FastAPI layout and is correct while small. **Do NOT create a folder per file.** Promote a flat module to a subpackage only when a single domain reaches **~3+ files** (router + its own schemas/deps). Until then, keep it flat.

Current `api/` domains (for reference when it grows):
- **MVP pipeline** (`webhook.py`, `health.py`) — patient/WhatsApp path. Keep flat; it is small and critical.
- **Admin** (`admin.py`, `tenants.py`) — SaaS-owner fleet view.
- **Doctor hub / CRM** (`config.py`, `oauth.py`, `calendar.py`, `deps.py`) — tenant-facing dashboard backend. This cluster is the one that has crossed the threshold; group it into `api/hub/` next time it is touched, updating `main.py` router imports and `tests/` accordingly.

When you restructure, do it as a dedicated change (move files + fix imports in `main.py` + fix `tests/`), then run `graphify update .` — never bundle a structural move with a behavioural change.

### General
- Pure decision functions over side-effects: prefer the `flow_router.route()` pattern — return a result object, let the caller persist/send. Easier to test without network/DB.
- All env config goes through `config.py::Settings` (pydantic-settings). Never read `os.environ` directly elsewhere.
- Per-tenant secrets are decrypted exactly once, in `services/tenant_config.py::load_tenant_config`. Never log or return them from the API.

## Documentação — manter em dia (obrigatório)

Os arquivos em `docs/` são a **fonte de verdade pra entender o projeto** — o objetivo é que uma
sessão nova do Claude Code (ou qualquer pessoa) entenda tudo, profundamente, só lendo `docs/`.
Por isso eles **têm que refletir o estado real** do projeto.

**Quando atualizar:** ao fazer mudanças numa sessão, atualize os docs afetados — **não
necessariamente na hora de cada mudança, mas no FIM da sessão**, depois que tudo foi **validado e
verificado** (testes passando, deploy/migração confirmados). Documentar antes de validar gera doc
errado; documentar depois garante que o doc descreve o que realmente está no ar.

**Regras:**
- Feature grande/multi-camada → um `docs/CHECKPOINT_<FEATURE>.md` (estado, o que entrou onde,
  deployado/testado, pendências) + 1 linha de ponteiro nos docs relevantes — é o padrão já usado em
  `docs/CHECKPOINT_plugins.md`.
- Cite âncoras estáveis (nome de função/módulo), não números de linha frágeis, quando possível.
- Mantenha o `CHECKPOINT_*` da feature em dia até ela ser 100% concluída/encerrada; aí vira histórico.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
