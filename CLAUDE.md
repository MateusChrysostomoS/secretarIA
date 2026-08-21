# SecretarIA

## Product vision

SecretarIA is a **multi-tenant SaaS** that drops a conversational appointment-booking secretary into any service business's existing WhatsApp number, via the WhatsApp **Cloud API Coexistence** model (bot and human agent share the same line, the bot stays quiet when the human picks up). Initial target market is clinics, but the product is service-agnostic — clients self-schedule with the business through their own WhatsApp.

Every tenant brings its own Google Calendar (OAuth), business hours, language, services catalogue and welcome pitch. The product is **sold by adapting all of those to the customer**.

## The "Eye Company" scaffold is gone — keep it that way

`src/secretaria/ai/prompts.py` used to hardcode an Eye Company / Dr. Mateus Chrysóstomo pitch, weekday 08-18 hours, 30-minute consults and Portuguese language. That's gone: `secretary_system_prompt(config: TenantRuntimeConfig)` renders every business-specific fact from the resolved tenant (and, when applicable, professional) config — see `services/tenant_config.py::load_tenant_config`.

When extending the agent, do not add hardcoded clinic-specific facts back into `prompts.py`. Anything that varies per business goes on the `Tenant` model (or, when it varies per doctor within a clinic, the `Professional` model).

## Per-tenant config (what each clinic can configure)

Shipped product configures each tenant with at minimum:

- **Identity**: business name, founder/owner, positioning paragraph (= welcome pitch)
- **Services**: list of bookable services, each with its own duration (and price, channel routing rules, etc.)
- **Business hours**: per weekday + holiday/exception calendar
- **Timezone**: IANA, e.g. America/Sao_Paulo
- **Language**: conversation language (pt-BR default, en, es, ...)
- **Google Calendar**: encrypted `refresh_token` + `calendar_id` (per tenant, obtained via a hosted OAuth onboarding flow, not the dev `scripts/gcal_auth.py`)
- **WhatsApp Coexistence**: `phone_number_id` + `access_token` (already on `tenants`)

All of the above now lives on `Tenant` (and, for the professional-level fields — specialty/about/context message/business hours/services/Calendar credential — on `Professional`, one or more rows per tenant). The schema migration, onboarding flow, and encryption at rest are DONE; see `docs/CHECKPOINT_onboarding_multiprofessional.md` for the per-professional layer and `docs/CHECKPOINT_plugins.md` for the encryption-at-rest / multi-tenant round that came before it.

## What the agent became

`secretary_system_prompt(config: TenantRuntimeConfig)` (`ai/prompts.py`) is a pure function of the resolved tenant config, not of a raw timezone. `ai/graph.py::run_agent` loads conversation history per `conversation_id` AND the matching tenant's resolved config (`services/tenant_config.py::load_tenant_config`), threading it through to the prompt and to per-tenant (or per-professional — contract v1 §10, `docs/CHECKPOINT_onboarding_multiprofessional.md`) Calendar credentials in `services/calendar.py`.

## Implementation status

- **Fase A** (terminal OpenAI + Google Calendar tool loop) — validated via `scripts/test_agent.py`.
- **Fase B** (LangGraph ReAct agent inside the arq worker) — code complete and imports clean, end-to-end WhatsApp test pending.
- **Multi-tenant adaptation** — LARGELY DONE (plugin round, docs/CHECKPOINT_plugins.md): per-tenant config/calendar/WhatsApp-token on the whole reply path, entitlement-gated bot + capability plugins (registry in `src/secretaria/plugins/`). Remaining: hosted OAuth onboarding polish + outbound rate limiter.
- **Encryption at rest** — DONE. Both tenant secrets are Fernet ciphertext in `tenant_credentials` (`google_refresh_token_encrypted`, `waba_token_encrypted`); `Tenant` carries no secret column (migration `d7e8f9a0b1c2` moved + dropped `access_token`). Decryption happens ONLY in `services/tenant_config.py` (`get_google_refresh_token` / `get_waba_token`); structlog runs a `redact_secrets` processor. Requires `ENCRYPTION_KEY`.
- **Pix deposit (sinal) add-on + payment lifecycle** — BUILT, pending deploy/external wiring (docs/CHECKPOINT_pix_deposit.md): `pix_deposit` entitlement (renamed from `pix_whatsapp`), per-clinic Asaas charges/refunds, `/webhooks/asaas`, ungated core reminders with 3-button deposit variant, money hooks on every cancel/no-show/reschedule path.
- **Onboarding + multi-professional config** — DONE (docs/CHECKPOINT_onboarding_multiprofessional.md): brain-api-mediated internal provisioning (`api/internal_provisioning.py`), per-professional config/completeness/partial-activation (`services/tenant_config.py`), per-professional Calendar OAuth + hub CRUD (`api/hub/oauth.py`, `api/hub/professionals.py`), Coexistence webhook signals (`history`/`smb_app_state_sync`), transactional onboarding email (`services/email.py`), and the onboarding-nudge + patient-usage-metering crons (`workers/onboarding_cron.py`). Onboarding STATE itself is owned by brain-api; this repo only reads/reports into it.

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

## Deploy — a API e o worker são DOIS serviços (regra obrigatória)

`secretaria_api` e `secretaria-worker` são dois serviços separados no EasyPanel,
implantados **manualmente**, sem auto-deploy. Nada garante que rodem o mesmo
commit — e o worker é quem responde: a API só faz fast-ACK do webhook, quem
compõe e envia toda mensagem é o job `process_webhook_event`.

**Regra: todo push que toca `src/secretaria/workers/` OU
`src/secretaria/services/flow_router.py` exige deploy do `secretaria-worker`,
não só do `secretaria_api`.** Habilitar auto-deploy nos dois serviços a partir
de `main` aposenta esta regra — é a solução preferida.

Isto não é hipotético: em 2026-08-16 o worker ficou um commit atrás e todo
greeting saiu de código velho enquanto a API parecia saudável. O sintoma leu
como "a personalização quebrou"; a causa era um clique.

Como provar, sem abrir `Environment`: `GET /build` na API responde a identidade
dela e a última que o worker anunciou, mais o veredito `deploy_parity`
(`match` | `divergent` | `unknown` — **`unknown` nunca significa paridade**). O
worker prova pela própria linha `worker_started`, que carrega os mesmos campos
mais os nomes dos jobs e crons registrados. Divergência emite
`deploy_sha_divergence` (WARNING) no arranque dos dois processos e de hora em
hora no worker. Ver `core/build_info.py` e a seção "Deploy both services, or
neither" do `README.md`.

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

## Prompts de correção pendentes (`.claude/prompts/`)

Gerados por uma sessão de auditoria (2026-08-21) a partir de uma lista de bugs já reportados pelo usuário. Cada arquivo é autossuficiente (causa raiz já investigada, arquivo/linha citados) para rodar em uma sessão nova. Use quando for resolver o problema correspondente — releia o arquivo primeiro, os números de linha citados podem ter mudado desde a auditoria.

- **`PROMPT_01_llm_flow_reentry_gap.md`** — conversas presas em modo LLM sem retorno ao fluxo determinístico quando a tenant não tem `returning_greeting_message` configurado. A transição LLM→fluxo em si já é tratada (4 ferramentas de hand-back em `ai/tools.py`/`plugins/multi_professional.py`); o gap é a ausência de expiração para quem não tem reativação configurada.
- **`PROMPT_02_specialty_leak_deploy_parity.md`** — mensagem solta com a especialidade do médico ao selecioná-lo (ex.: "Geriatria" sozinha). **Já corrigido em `main`** (commit `64d1af8`); sintoma ao vivo é provável recorrência de paridade de deploy API/worker — o prompt manda checar `GET /build`/`deploy_parity` antes de tocar em código.
- **`PROMPT_03_back_buttons_ambiguity.md`** — botões "Voltar" e "Escolher outro dia" na tela de horário. **Resolvido 2026-08-21**: usuário confirmou manter o destino ("Voltar" continua reabrindo a lista de serviços) e só trocar o texto pra deixar isso explícito — agora "Escolher outro Serviço" (`LABEL_ANOTHER_SERVICE`, calculado por `flow_router.py::_day_back_label`), tanto na picker de dia ("Ver dias") quanto na de horário. UNCOMMITTED.
- **`PROMPT_04_list_row_truncation.md`** — nomes longos (médico/serviço) truncados nas listas do WhatsApp. **Resolvido 2026-08-21 (UNCOMMITTED)** — ver `docs/CHECKPOINT_whatsapp_text_limits.md`. Os 12 literais mágicos viraram `core/whatsapp_limits.py` (constante + UMA função de corte usada no render **e** no matcher), o hub ganhou `maxLength`+erro+tooltip no nome do profissional. **Divergência deliberada do prompt:** o corte NÃO é por fronteira de palavra — isso colapsaria "Consulta de rotina adulto"/"…infantil" no mesmo título e faria `resolve_service_name` agendar o serviço errado; o corte preserva a cauda e marca com "…".

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
