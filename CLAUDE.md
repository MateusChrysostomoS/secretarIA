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

### Onde vive o código do Portal (Brain-Message)

O dono perguntou (2026-09-20, TASK-004) se cada peça do Portal não deveria virar uma pasta
`portal/` própria, como o PreCheck tem. A decisão foi **não mover nada** — e esta seção existe
para que uma sessão futura não reabra a proposta sem saber que ela já foi avaliada e recusada.
As três peças, e o motivo de cada uma:

| Peça | Natureza | Por que fica onde está |
|---|---|---|
| `services/channel_sender.py::BrainMessageSender` | exclusiva do Portal, **dentro de arquivo compartilhado** | o que está acima dela no arquivo (o Protocol `ChannelSender`, `MediaChannelSender` e os helpers `interactive_history_body` / `sender_persists_outbound` / `sender_sends_media`) é dos DOIS canais, porque `WhatsAppClient` satisfaz o mesmo Protocol estruturalmente. Tirar só o sender parte o seam em duas metades incompletas. |
| `plugins/precheck_handoff.py::_post_booking` | **ramo por canal dentro de uma função só** | `portal = ctx.patient.channel == CHANNEL_BRAIN_MESSAGE` decide tudo depois dele; é o padrão `channel-aware-dispatch` (um ponto de decisão, nunca os dois ramos disparando). Separar exigiria duplicar a função. |
| `plugins/pending_identity.py` | **100% do Portal** (guarda `ctx.patient.channel != CHANNEL_BRAIN_MESSAGE` no topo) | é 1 arquivo só. A regra de granularidade logo acima promove um domínio a subpacote com ~3+ arquivos; uma pasta para 1 arquivo furaria a própria regra do repo. |

Do lado do PreCheck a pasta **existe** e não segue este padrão: `app/services/brain_message/`
(`store.py` / `agent_client.py` / `conductor.py`) é do Portal de ponta a ponta — nenhum nó do n8n
a chama. Ou seja, o pedido do dono já está atendido lá; não renomeie nem recrie.

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

## `CORS_ALLOW_ORIGINS` aceita DOIS formatos — e o valor "certo" já derrubou o portal

`config.py::cors_origins` lê tanto uma lista JSON (`["https://a","https://b"]`)
quanto a forma legada separada por vírgula. O ramo JSON existe porque o brain-api
ganhou o dele em `46b4eb3` (2026-08-21) e este serviço não: em 2026-09-12 alguém
copiou o valor JSON do painel do brain-api pro do `secretaria_api`, o split por
vírgula partiu a string em `["https://…` e `…"]`, e o hub passou a responder
**400 `Disallowed CORS origin` a toda origem** — `/configuracao` do portal do
médico ficou vazia e read-only para uma clínica real. O valor parecia perfeito
no painel. Ver `docs/CHECKPOINT_cors_json_array_hub.md`.

**Regra que sai disso:** ao endurecer o parsing de uma variável de ambiente que
operador copia entre painéis, propague para TODO serviço que lê a mesma variável
no mesmo movimento. Meio-espelho vira armadilha silenciosa.

Como provar, sem abrir `Environment`: `GET /build` na API responde a identidade
dela e a última que o worker anunciou, mais o veredito `deploy_parity`
(`match` | `divergent` | `unknown` — **`unknown` nunca significa paridade**). O
worker prova pela própria linha `worker_started`, que carrega os mesmos campos
mais os nomes dos jobs e crons registrados. Divergência emite
`deploy_sha_divergence` (WARNING) no arranque dos dois processos e de hora em
hora no worker. Ver `core/build_info.py` e a seção "Deploy both services, or
neither" do `README.md`.

## Documentação

`docs/` é a fonte de verdade deste repo (exemplo do padrão: `docs/CHECKPOINT_plugins.md`). Regra
geral de quando/como atualizar (CHECKPOINT, âncoras estáveis) em `AI_WORKFLOW.md` — aqui só o que
diverge, se houver.

`docs/JORNADA_PACIENTE_WHATSAPP_E_PORTAL.md` (gerado 2026-09-20) é o mapa de produto leigo — sem
código, sem função — de todos os fluxos determinísticos que o paciente vê, WhatsApp e Portal
lado a lado, cada bloco marcado "no ar" / "construído, não no ar" / "não existe ainda". Releia
antes de confiar: o status muda a cada rodada de trabalho.

O contrato de integração do canal Brain-Message está em
`brain-api/docs/PORTAL_MESSAGING_API.md`; mantenha em dia ao mudar este lado do contrato.

## Prompts de correção pendentes (`.claude/prompts/`)

Gerados por uma sessão de auditoria (2026-08-21) a partir de uma lista de bugs já reportados pelo usuário. Cada arquivo é autossuficiente (causa raiz já investigada, arquivo/linha citados) para rodar em uma sessão nova. Use quando for resolver o problema correspondente — releia o arquivo primeiro, os números de linha citados podem ter mudado desde a auditoria.

## Prompt pendente — `interactive` grava a string JSON `'null'` em vez de NULL

`z_prompts/PROMPT_INTERACTIVE_NULL_PORTAL_TAP_WINDOW.md` (raiz de BRAIN, gerado 2026-09-19 via
`/prompt-generator`) — achado registrado em `docs/CHECKPOINT_brain_message_anexos_secretaria.md`
§8: `models/message.py:107` não tem `none_as_null=True` (ao contrário de `attachment`, linha 118),
então `interactive=None` numa bolha de texto grava a string `'null'`, não SQL NULL. Consequência:
`workers/tasks.py::_validated_brain_message_reply_id` (linha 6493, filtro na linha 6527,
`BRAIN_MESSAGE_TAP_WINDOW=10` na linha 6474) conta as 10 últimas mensagens de qualquer tipo, não
os 10 últimos cartões interactive — um cartão seguido de 10+ bolhas de texto teria o toque
recusado. Confirmado ainda pendente em 2026-09-19. **NÃO EXECUTADO.**

## Prompts de feature pendentes (`TECH/BRAIN/z_prompts/debug_secretaria_producao/`)

Convenção compartilhada entre repos da Brain (não uma pasta deste repo) — mesmo local onde vive a cadeia `FEAT_36`-`40`. Gerados 2026-08-28 a partir do incidente do tenant "Chrysostomo For Eyes" (ver memória `secretaria-agendar-inactive-tenant-2026-08-28`): hoje um profissional ativo mas com configuração incompleta (sem horário ou sem serviço) aparece normalmente no seletor de médicos e nunca avisa ninguém quando um paciente esbarra nele.

- `PROMPT_FEAT_41_PROFESSIONAL_CONFIG_GAP_ALERT_BACKEND.md` — detecta o gap (estático, não confundir com agenda cheia) e alerta por e-mail com debounce, espelhando `_handle_calendar_unavailable`. Fundação das outras duas fatias. **EXECUTADO 2026-08-29 — BUILT, 1857 testes verdes, uncommitted e não deployado; ver `docs/CHECKPOINT_professional_config_gap_alert.md`** (a §6 diz qual campo o `FEAT_42` deve ler em vez de criar endpoint novo; a §8 registra um bug de snapshot do worker que a suíte pegou).
- `PROMPT_FEAT_42_PROFESSIONAL_CONFIG_GAP_BANNER_FRONTENDS.md` — banner dispensável em `brain-frontend` + `secretarIA-frontend` avisando o médico/clínica. Depende do 41.
- `PROMPT_FEAT_43_PROFESSIONAL_CONFIG_GAP_BANNER_PRECHECK_OPTIONAL.md` — mesmo banner no PreCheck. OPCIONAL, só depois dos dois anteriores estarem no ar e o usuário reconfirmar.

Gerados 2026-08-30 a partir de um pedido de UX conversacional (emoji dinâmico nos botões, como o PreCheck faz, + redesenho do cancelamento em andamento). O `FEAT_44` já rodou (2026-08-30); o `FEAT_45` não.

- `PROMPT_FEAT_44_EMOJI_EM_BOTOES_E_LISTAS.md` — ✅/❌ em confirmações, 🏥 condicional em nome de serviço, 🗓️ em dia/horário, ⬅️ nas linhas de voltar. **EXECUTADO 2026-08-30 — BUILT, 1910 testes verdes (1 flake pré-existente de fuso), uncommitted e não deployado; ver §5 de `docs/CHECKPOINT_whatsapp_text_limits.md`.** Decisões do §3 resolvidas com o usuário: par ✅/❌ **semântico** (Confirmar/Cancelar também), 🗓️ também no `[SLOTS]` da LLM, 🏥 **condicional** (nome já truncado fica sem emoji). Duas descobertas que o prompt não previa: `🗓️ `/`⬅️ ` custam **3** code units (seletor `U+FE0F`), não 2; e `"⬅️ Escolher outro Serviço"` daria 25 — um a mais que o cap de 24 — então os rótulos de voltar viraram **"⬅️ Outro dia"/"⬅️ Outro serviço"**. O risco real não era o render e sim o **matcher**: decorar a constante quebraria um "sim" digitado, resolvido com `strip_decoration` dentro do `_norm` das três camadas.
- `PROMPT_FEAT_45_MENU_DE_EDICAO_NO_CANCELAMENTO.md` — substitui a tela "Sem problema! Quer escolher outro horário?" (`_handle_confirmation`, `STEP_AWAITING_RETRY`) por uma lista que deixa o paciente saltar para qualquer campo já preenchido na marcação (serviço/profissional/dia/horário/convênio) e por um botão "❓ Dúvida" que ativa a LLM. Recomenda-se rodar depois do `FEAT_44` — **que já rodou** (acima); o `FEAT_44` deixou `LABEL_RETRY_YES`/`LABEL_RETRY_MENU` sem emoji de propósito, para esta tela decidir a própria formatação. **Correção de premissa registrada no próprio prompt (§1):** "estados do fluxo" no pedido do usuário não é o enum `FlowState` (só 6 membros genéricos) — é o conjunto de campos já escolhidos dentro de `SERVICE_CATALOG`. Mecanismo de retorno da LLM para esta tela nova (§4.4) é uma decisão de arquitetura em aberto, não resolvida no prompt.

- **`PROMPT_01_llm_flow_reentry_gap.md`** — conversas presas em modo LLM sem retorno ao fluxo determinístico quando a tenant não tem `returning_greeting_message` configurado. A transição LLM→fluxo em si já é tratada (4 ferramentas de hand-back em `ai/tools.py`/`plugins/multi_professional.py`); o gap é a ausência de expiração para quem não tem reativação configurada. **Executado — commit `871b802`** ("Implement LLM state expiration logic and add tests for conversation timeout handling").
- **`PROMPT_02_specialty_leak_deploy_parity.md`** — mensagem solta com a especialidade do médico ao selecioná-lo (ex.: "Geriatria" sozinha). **Já corrigido em `main`** (commit `64d1af8`); sintoma ao vivo é provável recorrência de paridade de deploy API/worker — o prompt manda checar `GET /build`/`deploy_parity` antes de tocar em código.
- **`PROMPT_03_back_buttons_ambiguity.md`** — botões "Voltar" e "Escolher outro dia" na tela de horário. **Resolvido 2026-08-21**: usuário confirmou manter o destino ("Voltar" continua reabrindo a lista de serviços) e só trocar o texto pra deixar isso explícito — agora "Escolher outro Serviço" (`LABEL_ANOTHER_SERVICE`, calculado por `flow_router.py::_day_back_label`), tanto na picker de dia ("Ver dias") quanto na de horário. UNCOMMITTED.
- **`PROMPT_04_list_row_truncation.md`** — nomes longos (médico/serviço) truncados nas listas do WhatsApp. **Resolvido 2026-08-21** — backend commit `ef8f6dd`, frontend commit `00f343d` — ver `docs/CHECKPOINT_whatsapp_text_limits.md`. Os 12 literais mágicos viraram `core/whatsapp_limits.py` (constante + UMA função de corte usada no render **e** no matcher), o hub ganhou `maxLength`+erro+tooltip no nome do profissional. **Divergência deliberada do prompt:** o corte NÃO é por fronteira de palavra — isso colapsaria "Consulta de rotina adulto"/"…infantil" no mesmo título e faria `resolve_service_name` agendar o serviço errado; o corte preserva a cauda e marca com "…". Cobriu só o nome do profissional — continuação em `PROMPT_04B`.
- `z_prompts/PROMPT_BRAIN_MESSAGE_SECRETARIA_IDENTIDADE_PACIENTE.md`,
  `..._CONSOLE_STAFF.md`, `..._PIPELINE_CANAL.md` (raiz de BRAIN, convenção compartilhada,
  gerados 2026-09-07) — trazem este repo pra funcionar também pelo canal Brain-Message (além
  do WhatsApp, sem substituí-lo): migração aditiva de identidade de paciente por canal,
  endpoints de leitura/envio pro console de staff, e o núcleo channel-neutral do pipeline +
  endpoints internos `/internal/brain-message/*`. Parte de um conjunto de 10 prompts
  cross-repo (ver `z_prompts/PROMPT_BRAIN_MESSAGE_OTP_SWITCHBOARD.md` em brain-api pro plano
  completo e a ordem de execução).
  **`..._IDENTIDADE_PACIENTE.md` EXECUTADO 2026-09-08 — BUILT, `channel`/`external_id`
  aditivos em `Patient`, `wa_id` nullable, migração `c7e1a4b9d0f3`; ver
  `docs/CHECKPOINT_patient_channel_identity.md`. **Estado do banco de produção real (fora do
  banco de teste do `.env`) NÃO confirmado** — `..._PIPELINE_CANAL.md` exige esta migração já
  aplicada em produção antes de rodar; confirme isso antes de iniciar aquele prompt.**
  **`..._CONSOLE_STAFF.md` EXECUTADO 2026-09-08 — BUILT, `GET`/`POST
  /tenants/me/conversations/{id}/messages` em `api/hub/conversations.py`, suíte completa
  verde (2014 passed); ver `docs/CHECKPOINT_console_staff_messages.md`.**
  **`..._PIPELINE_CANAL.md` EXECUTADO 2026-09-08 — BUILT, suíte completa verde (2039 passed,
  baseline 2014), uncommitted e não deployado; ver `docs/CHECKPOINT_brain_message_pipeline.md`.**
  `_persist_inbound_message` virou wrapper WhatsApp + núcleo `_route_inbound_turn`
  channel-neutro; `services/channel_sender.py` (novo) é o Protocol `ChannelSender` com as
  MESMAS assinaturas de `WhatsAppClient` — por isso os ~60 call sites de envio ficaram
  inalterados. `_ReplyContext.patient_wa_id` → `patient_ref` + `channel`. Endpoints
  `POST /internal/brain-message/inbound` e
  `GET /internal/brain-message/conversations/{external_id}/messages`.
  **A resposta da pergunta em aberto do prompt:** a `Message` de saída é gravada SÓ DEPOIS de
  a Graph API responder, e o `wam_id` sai da resposta — então "enviar" por `brain_message` não
  podia ser só persistir; o sender do canal grava ele mesmo e os 3 sites que gravavam pulam
  (flag `persists_outbound`). **Corrigiu de passagem uma bomba-relógio:**
  `InternalPatient.wa_id` era não-opcional e o 1º paciente `brain_message` de um tenant daria
  500 em `GET /internal/tenants/{id}/patients`, derrubando a lista do portal daquela clínica.
  Reminders/HSM ficaram FORA por desenho (§ "lacuna conhecida" do checkpoint).
- **`z_prompts/PROMPT_BRAIN_MESSAGE_SECRETARIA_CONSOLE_SEND_CHANNEL_DISPATCH.md`** (raiz de BRAIN,
  gerado 2026-09-09 via `/prompt-generator` numa sessão de diagnóstico de produção) — fecha o fio
  solto entre `..._CONSOLE_STAFF.md` (peça 4) e `..._PIPELINE_CANAL.md` (peça 5): o endpoint
  `POST /tenants/me/conversations/{id}/messages` ainda chama `WhatsAppClient` incondicionalmente
  (`_send_via_whatsapp`, `api/hub/conversations.py:121`), então staff respondendo um paciente
  `channel="brain_message"` (que tem `wa_id=None`) quebra o envio. Precisa despachar por
  `patient.channel` via `services/channel_sender.py::ChannelSender`/`BrainMessageSender`, igual
  `workers/tasks.py::_reply_sender` já faz no caminho automático. **EXECUTADO em duas etapas.**
  (1) 2026-09-09, `5b8bfdf`: o sintoma (502 ao responder paciente do Portal) foi contido por um
  desvio inline que NÃO passava pelo `ChannelSender` (o `BrainMessageSender` era fixo em
  `sender=BOT`). (2) **2026-09-18 — BUILT, suíte completa verde (2176 passed; baseline 2165 + 11
  novos), UNCOMMITTED, não deployado:** o hub despacha por `patient.channel` em
  `_staff_sender` → `BrainMessageSender(author=HUMAN)` ou `WhatsAppClient` (ramo WhatsApp
  inalterado); `BrainMessageSender` ganhou `author` (padrão BOT — worker e `pending_identity`
  intocados) e `session=` (o hub grava resposta + handover num commit só) e devolve o id da linha
  em `RECORDED_MESSAGE_ID`. Ver §9 de
  `docs/CHECKPOINT_console_staff_messages.md` e a skill nova
  `TECH/.claude/skills/channel-aware-dispatch/`. Deploy: a rigor só `secretaria_api` precisa do
  código novo (o worker importa `channel_sender.py`, mas usa só o padrão BOT, idêntico), porém
  deployar os dois mantém `deploy_parity=match`. Na mesma sessão de 2026-09-09, o bug irmão do lado
  de LEITURA (`ConversationRead.patient_wa_id`
  não-opcional, 500 em `GET /tenants/me/conversations`) já foi corrigido diretamente — ver
  `docs/CHECKPOINT_patient_channel_identity.md`.
- **`_TEMPLATES` (`services/email.py`) sem a chave `"patient_access_otp"`** — achado 2026-09-09
  testando o OTP do paciente ao vivo: `request-otp` responde 200 e enfileira no arq, mas o
  worker acha `_TEMPLATES.get("patient_access_otp")` `None`, loga
  `transactional_email_unknown_template` e para — o e-mail nunca sai, sem erro visível em
  lugar nenhum. Corrigido pelo roteiro em `z_prompts/PROMPT_BRAIN_MESSAGE_E2E_QA_PRODUCAO.md`
  (raiz de BRAIN) junto com o bug acima — ambos vivem nesta mesma cadeia de teste ao vivo.
  Lembrete: quem lê `_TEMPLATES` é o **worker**, então o fix exige redeploy dos dois serviços
  (`secretaria_api` + `secretaria-worker`), não só um.
- **`z_prompts/PROMPT_PSEUDONYMIZE_SECRETARIA_ADOPTION.md`** (raiz de BRAIN, convenção
  compartilhada) — adota `pseudonymize-core` nos dois pontos de entrada de IA deste repo:
  `ai/graph.py::run_agent` (histórico + resposta) e `ai/scoped_help.py::_run`.
  **EXECUTADO 2026-09-07 — BUILT, 1993 testes verdes (baseline do HEAD era 1980),
  uncommitted, migração NÃO rodada em banco nenhum, não deployado; ver
  `docs/CHECKPOINT_pseudonimizacao.md`.**
  **A decisão que o prompt deixou em aberto (escopo das ferramentas do loop ReAct) foi
  tomada com o usuário: o escopo CRESCEU.** A pergunta original ("retorno de ferramenta
  carrega PII de terceiro?") tinha resposta *não* — mas a investigação achou o problema na
  direção oposta: `ai/tools.py::create_event` instrui o modelo a intitular o evento
  `'Consulta - João Silva'`, nome que ele tira do histórico. Mascarar o nome do paciente
  **sem** re-hidratar o argumento encheria a agenda real do médico de
  `Consulta - [PACIENTE_xxxx]` — corrupção, e pelo caminho instruído, não por acaso. Por
  isso o guard é **simétrico e mora no limite da ferramenta** (`ai/pii.py`): re-hidrata todo
  argumento na ida, escruda todo resultado na volta — cobre plugins e ferramentas futuras
  por construção. Duas divergências deliberadas do prompt, ambas em §4 do checkpoint:
  o nome do paciente usa `add_identifier` (não `add_person_name`, que faria "Dra. Ana Paula"
  virar o nome do paciente para uma paciente chamada Ana), e o `clarify.question` do
  `scoped_help` **precisa** de rehydrate (é prosa livre que o `flow_router` envia literal ao
  paciente) — a hipótese do prompt só valia para o `pick.choice`.
  **Duas decisões do usuário em 2026-09-07, depois da primeira rodada:**
  (a) **`check_availability` parou de expor evento alheio** (§3.1 do checkpoint): só devolve
  `summary`/`id` reais quando o evento é do PRÓPRIO paciente da conversa — provado pela
  tabela `appointments` (`google_event_id`), nunca pelo título; para qualquer outro devolve
  só `{start, end, do_paciente: false}`. Falha FECHADA (sem contexto, esconde tudo). O `id`
  sai junto de propósito: é o argumento do `cancel_event`. `ai/prompts.py` mudou na mesma
  rodada — o "mencione brevemente o conflito" virava vazamento por desenho. Varredura
  confirmou que nenhum teste dependia do summary de terceiro.
  (b) **Espaçamento não-canônico no nome NÃO se conserta aqui** (§4.1.1): a correção é do
  `pseudonymize-core` e já está lá; o pin daqui segue em `v0.1.0`, que **ainda vaza** —
  quando a tag nova sair, subir o pin em `pyproject.toml` e `uv sync`. Duplicar a lógica
  deste lado é exatamente a armadilha que motivou extrair o pacote.
  Pendências abertas em §9 do checkpoint: migração, deploy dos DOIS serviços, commit, pin.
- **`PROMPT_04B_list_row_limits_remaining_surfaces.md`** — continuação do `PROMPT_04`. **Resolvido 2026-08-22 (UNCOMMITTED)** — ver §4 de `docs/CHECKPOINT_whatsapp_text_limits.md`. A varredura achou **três** campos, não dois: nome de serviço (`ServiceCard.tsx`) e também **convênio** (`ContextSection.tsx`, não previsto). Os dois **avisam sem bloquear** — `/configuracao` salva oito seções atrás de um botão só, e um nome legado longo não pode sequestrar o resto. Convênio valida **por item** sobre `toWireInsurances` (um `maxLength` no campo proibiria três planos curtos legais). `ai/prompts.py` agora interpola `MAX_LIST_ROW_TITLE_CHARS` no bloco `[SLOTS]` e `_parse_slot_rows` corta no parse. `[CONFIRM]` não precisou de nada (labels fixos no código) — há teste fixando isso. Bônus: `formatter.py` declarava um QUARTO literal (`MAX_LIST_ROWS = 10`), agora re-export.
- `z_prompts/PROMPT_BRAIN_MESSAGE_INTERACTIVE_BUBBLES_RENDERING.md` (raiz de BRAIN, gerado
  2026-09-10 via `/prompt-generator` a partir de análise ao vivo no Chrome do console real) —
  botões/listas do WhatsApp (`ButtonBubble`/`MenuBubble`/`SlotsBubble`) chegam ao
  `Brain-Message-Frontend` (console de staff e portal do paciente) já achatados em texto puro por
  `workers/tasks.py::_bubble_history_body`; `models/message.py::Message` não tem coluna pra
  estrutura e `schemas/conversation.py::MessageRead` só expõe `body: str`. Pede migração nova
  (coluna JSON) + extensão do wire, mantendo `body` achatado intocado (é o que a LLM lê).
  **EXECUTADO 2026-09-11 — BUILT, suíte completa verde (2076 passed), UNCOMMITTED, migração
  `4c8e2a7f1b93` (`messages.interactive JSON NULL`, depois de `9d3b7e1f5a2c`) testada só em Postgres
  local descartável, não deployado; ver `docs/CHECKPOINT_interactive_bubbles.md`.** Só o caminho
  WhatsApp grava a coluna (o paciente Brain-Message recebe texto); `WhatsAppClient` monta o payload a
  partir do mesmo registro gravado; o hub expõe `interactive` + `interactive_reply_id`. **Deploy:
  migração ANTES da API e do worker.**
- `z_prompts/PROMPT_BRAIN_MESSAGE_INBOUND_PAYLOAD_LEAK.md` (raiz de BRAIN, gerado 2026-09-10,
  mesma sessão) — achado relacionado mas com causa própria: a resposta da paciente a uma lista com
  payload (`"prof|<uuid>"`, `flow_router.py` ~linha 1290) aparece no console como `"Dr. Fulano
  (8faa12e1-…)"` — o UUID interno vaza porque `schemas/webhook.py::extract_inbound_body` (linhas
  ~512-544) monta deliberadamente `"{title} ({payload})"` pro `flow_router` conseguir reconstruir a
  escolha a partir do texto salvo, e essa MESMA string vira `Message.body`. Pede separar o texto de
  exibição do sinal de roteamento. **EXECUTADO 2026-09-11 — BUILT, suíte completa verde (2066
  passed), UNCOMMITTED, migração `9d3b7e1f5a2c` NÃO aplicada, não deployado; ver
  `docs/CHECKPOINT_inbound_tap_display_vs_routing.md`.** Achado que o prompt não previa: o
  histórico da LLM (`ai/graph.py::_load_history`) também lia o payload do `body` — o `[SLOTS]` da LLM
  agenda um turno depois, lendo o ISO do histórico — por isso o id virou coluna
  (`messages.interactive_reply_id`) em vez de só viajar em memória. **Deploy: migração ANTES do
  worker** (§4 do checkpoint).
- `z_prompts/PROMPT_BRAIN_MESSAGE_PORTAL_INTERACTIVE_TAP.md` (raiz de BRAIN, gerado 2026-09-11) —
  continuação dos dois prompts acima: o Portal do paciente (`/conversa`, canal Brain-Message) ainda
  não manda nem recebe estrutura interativa nenhuma (`BrainMessageSender` só grava texto,
  `BrainMessageInbound`/`BrainMessageMessage` em `schemas/internal.py` não têm os campos novos).
  Cross-repo com brain-api (autorizado explicitamente pelo dono) e Brain-Message-Frontend. Inclui
  uma validação nova (id do toque vindo do navegador, não de webhook assinado). **NÃO EXECUTADO
  ainda.**
- `z_prompts/PROMPT_BRAIN_MESSAGE_SECRETARIA_EMAIL_OTP_INLINE.md` (raiz de BRAIN, gerado 2026-09-16
  via `/prompt-generator`, onda 2 de `PLANO_LOGIN_SEM_GATE_PACIENTE_NOVO.md`) — canal
  `brain_message` só: insere um passo de captura de e-mail (sem verificar) entre `GREETING_FRAME` e
  `LGPD_CONSENT_MESSAGE`, e depois de uma consulta confirmada com e-mail ainda não verificado, manda
  aviso de código e verifica inline no chat. `FlowState` novos com saída limitada por tempo. Não
  toca no fluxo de agendamento em si nem no canal WhatsApp. Depende do CHECKPOINT de
  `PROMPT_BRAIN_MESSAGE_PORTAL_SESSAO_PENDENTE.md` (brain-api, onda 1). **EXECUTADO 2026-09-17 —
  COMMITADO/PUSHED em `main` (`2bda47b`) e observado em produção no fingerprint Linux
  `8d7c6951e7fa`; o primeiro E2E revelou depois um gate de ativação do WhatsApp herdado
  indevidamente por `brain_message`, corrigido no sucessor `b2f3055`. O contrato do brain-api foi
  ampliado antes da implementação, com dois estados TTL + “quer continuar?”, OTP redigido e
  regressões WhatsApp/PreCheck; ver `docs/CHECKPOINT_secretaria_email_otp_inline.md`.**
- `z_prompts/PROMPT_BRAIN_MESSAGE_FECHAR_ONDA_2_ROLLOUT_E2E.md` (raiz de BRAIN, gerado 2026-09-17
  via `$prompt-generator`, revisado após o primeiro QA) — handoff de fechamento da onda 2. O
  fingerprint `8d7c6951e7fa` foi confirmado como o build Linux correto de `2bda47b`; o transcript
  vazio veio do gate WhatsApp `Tenant.is_active` aplicado indevidamente ao canal `brain_message`.
  A correção mínima e o teste estão commitados/pushed em `main@b2f3055` (fingerprint Linux esperado
  `4b6bd25a508d`), com 2164 testes verdes. Falta comprovar Deploy/Rebuild conjunto de API+worker e
  concluir o E2E greeting → e-mail → LGPD → consulta → OTP, redaction e idempotência.
  **EXECUTADO 2026-09-17 — ONDA 2 FECHADA.** O rollout de `b2f3055` (`4b6bd25a508d`) destravou o
  canal e expôs um defeito pré-existente a jusante: o agendamento pelo Portal gravava o `patient_ref`
  (UUID de 36 chars) em `appointments.phone`, que é `VARCHAR(32)` — o Postgres recusava o INSERT,
  a transação fazia rollback e o evento já criado no Google ficava órfão. Corrigido em `697c24a`
  (`595b1f80df8c`), deployado com autorização explícita do dono, e os passos 1 a 15 do E2E foram
  provados no runtime da clínica QA. Ver a seção "Rollout e prova em produção" de
  `docs/CHECKPOINT_secretaria_email_otp_inline.md` e `tasks/TASK-002/TASK.md` (BRAIN).
  Dois defeitos abertos ficaram registrados lá: DEF-2 (fuso na tela de cancelar/gerenciar) e
  DEF-3 (calendário indisponível e falha de persistência dizem a mesma coisa ao paciente).
- `z_prompts/PROMPT_BRAIN_MESSAGE_OTP_PORTAO_ANTES_DA_CONSULTA.md` (raiz de BRAIN, gerado 2026-09-17
  via `/prompt-generator`) — **emenda que reverte, para o canal `brain_message`, a regra "onda 2
  FECHADA" acima ("consulta antes do código")**: um E2E manual em produção (clínica "Chrysostomo
  For Eyes") achou que uma mensagem fora do formato de 6 dígitos derruba
  `FlowState.AWAITING_EMAIL_CODE` para `IDLE` (`workers/tasks.py:1026-1049`, por desenho — "a conta
  é uma oferta, não um portão") e a mensagem seguinte com o código correto cai na LLM genérica, que
  alucina "código verificado e conta ativada" sem nenhuma tool real por trás (`ai/` não tem nenhuma
  noção de `verify_code`/OTP; a única mensagem de sucesso real é
  `CODE_ACCEPTED_MESSAGE`/`services/pending_identity.py:126`, usada só em
  `workers/tasks.py:2654-2673`). O dono decidiu reverter: código de 6 dígitos vira portão antes da
  consulta ser criada de verdade (banco + Google Calendar), com reserva temporária do horário por
  10 minutos e dois botões novos na mensagem de pedido de código ("Abrir e-mail" com logo por
  provedor, "Reenviar Código" — reenvio só por botão, nunca por linguagem natural livre). Pede
  também uma proteção estrutural de propósito geral: a LLM de fallback nunca pode afirmar uma ação
  de segurança (verificação, pagamento, etc.) sem uma tool call real no mesmo turno — checar skills
  existentes e, se faltar, criar uma via `skill-creator`. **EXECUTADO 2026-09-20 — BUILT, 2328
  testes verdes (baseline 2301; a única falha é o flake de fuso pré-existente de
  `test_human_backup_plugin`, provado no HEAD limpo), COMMITADO/PUSHED em `main` (`b0c43a9`),
  migração `a7d2f4b9c013` NÃO aplicada, não deployado; ver `docs/CHECKPOINT_secretaria_booking_code_gate.md`.** Tabela nova
  `booking_holds` (reserva de 10 min, mesmo relógio do OTP do brain-api), `BookingGate` fail-open
  injetado no `flow_router`, promoção da reserva em `workers/tasks.py::_promote_booking_hold` — é
  de lá que os hooks de `post_booking` (handoff do PreCheck incluído) passam a disparar. O card de
  3 botões do TASK-003 foi **reaproveitado**, não recriado; só o corpo mudou. Bug 1 ganhou duas
  camadas: regra 5 nas REGRAS INEGOCIÁVEIS de `ai/prompts.py` e o filtro de saída
  `services/sensitive_claim_guard.py`. **Divergências do prompt:** "Abrir e-mail" saiu como LINK
  (o card tem teto de 3 botões e botão de URL exigiria o Brain-Message-Frontend, fora de escopo),
  e o logo por provedor ficou como pendência pela mesma razão. Pendências em §5 do checkpoint —
  entre elas, remarcar ainda pode cair num horário reservado.
- `z_prompts/PLANO_PORTAL_API_MVP.md` (raiz de BRAIN, gerado 2026-09-17) — prioridade atual do dono:
  terminar o MVP da API de mensageria do Portal (estilo WhatsApp, documentada, adaptável a qualquer
  produto) antes de retomar `PLANO_ATUALIZADO_LOGIN_E_FLUXO_PACIENTE_PRECHECK.md`. A Onda 0 desse
  plano é o item **`PROMPT_BRAIN_MESSAGE_SECRETARIA_CONSOLE_SEND_CHANNEL_DISPATCH.md`** listado acima
  (EXECUTADO 2026-09-18, BUILT e uncommitted — ver acima) — pré-requisito explícito, porque a peça de anexo abaixo edita a MESMA
  função. Peça deste repo: `z_prompts/PROMPT_BRAIN_MESSAGE_ANEXOS_SECRETARIA_2_SECRETARIA.md`
  (Opus 5, alto) — anexo de arquivo no canal Brain-Message: coluna JSON `messages.attachment`
  (mesmo espírito de `messages.interactive`), armazenamento PRÓPRIO em R2 (cópia do padrão do
  `PreCheck/app/services/r2.py`, credenciais e bucket novos, sem chamar o serviço do PreCheck),
  `ChannelSender.send_media` novo, e escopo deliberadamente limitado a transporte (o bot confirma o
  recebimento; não há OCR/IA de visão — isso é diferenciação do PreCheck, não desta API). Depende do
  pré-requisito acima e da parte 1 (`brain-api`, mesmo plano). **EXECUTADO 2026-09-18 — BUILT, UNCOMMITTED, não deployado; ver `docs/CHECKPOINT_brain_message_anexos_secretaria.md`.** Upload no request da API (o job leva só a referência; só `secretaria_api` precisa das 4 variáveis `ATTACHMENTS_R2_*`), migração `5e1f9a3c7d20` (coluna + índice parcial, provada em Postgres descartável), `send_media` como capacidade (`MediaChannelSender`) porque o `WhatsAppClient` precisa continuar satisfazendo o `ChannelSender`, 409 antes do aceite LGPD e cota diária persistida (429) — o brain-api precisa passar a repassar esses dois códigos.
- `z_prompts/PROMPT_BRAIN_MESSAGE_STATUS_ENTREGA_1_SECRETARIA.md` (Opus 5, alto; adicionada
  2026-09-18, achado do dono no console real) — Onda 3 de `PLANO_PORTAL_API_MVP.md`: "enviada,
  recebida e vista, assim como faz o WhatsApp" não funciona hoje. Causa confirmada nos DOIS
  canais: `workers/tasks.py::process_webhook_event` recebe `value.statuses` do Meta (evento real
  de entrega/leitura) e nunca lê esse campo; `Message` não tem coluna de status nenhuma.
  Colunas novas `delivered_at`/`read_at`/`failed_at`/`failure_reason`/`updated_at`; WhatsApp
  passa a consumir o `statuses[]` que já chega (idempotente, nunca regride um estado mais
  avançado); Brain-Message trata entrega como imediata e ganha duas rotas novas de "marcar como
  lido" (paciente e staff), nunca aplicável a um paciente `channel="whatsapp"` (só o Meta confirma
  leitura ali); o cursor `since` das listagens muda de `created_at` para `updated_at`, senão o
  tique nunca avança para quem já buscou a mensagem antes da mudança de status. Sequencie DEPOIS
  da peça de anexos acima (mesmos arquivos: `models/message.py`, `schemas/internal.py`,
  `api/internal.py`, `api/hub/conversations.py`). **EXECUTADO 2026-09-19 — BUILT, 2232 testes
  verdes (baseline 2202), UNCOMMITTED, migração `8b4d2f6e1a37` NÃO aplicada, não deployado; ver
  `docs/CHECKPOINT_brain_message_status_entrega.md`.** O recibo morria num 3º ponto que o prompt
  não previa: o fast-ACK (`schemas/webhook.py::minimal_event_payload`) nunca carregava
  `statuses`. Agora carrega, sem `recipient_id`. **`since` passou a comparar `updated_at`:
  o brain-api (parte 2) precisa fazer upsert por `id`.** Leitura manual de paciente WhatsApp
  devolve `applied: false` (ignorada, não recusada). Deploy: migração ANTES, depois os DOIS
  serviços.
- `z_prompts/PROMPT_PORTAL_PASTA_EXCLUSIVA_E_HANDOFF_PRECHECK.md` (raiz de BRAIN, gerado
  2026-09-20 via `/prompt-generator`) — fecha a peça 3 do TASK-003 (handoff do PreCheck pelo
  Portal, hoje `501`), reaproveitando `POST /internal/brain-message/inbound` do PreCheck com
  `text` vazio (confirmado por leitura de código nesta sessão que isso não fabrica bolha de
  paciente — `PreCheck/app/services/brain_message/conductor.py:260-269`), e cria a pasta
  exclusiva do Portal que o dono pediu. Use quando for terminar o fluxo do Portal antes de
  autorizar merge/deploy das 3 branches do TASK-003. **NÃO EXECUTADO ainda.**
- `z_prompts/PROMPT_PORTAL_CALENDARIO_COMPONENTE.md` (raiz de BRAIN, gerado 2026-09-20) —
  calendário clicável só no Portal (WhatsApp continua mês→dia→horário), substituindo a
  sequência de telas de chat na escolha de data. **NÃO EXECUTADO ainda.**
- `z_prompts/PROMPT_WHATSAPP_FLOW_POC_CALENDARIO.md` (raiz de BRAIN, gerado 2026-09-20) —
  prova de conceito (1 clínica de teste, sem produção) de WhatsApp Flow como o mesmo
  calendário dentro do WhatsApp; decide se vale o custo operacional por clínica (chave
  RSA por WABA) antes de abrir um TASK de verdade. **NÃO EXECUTADO ainda.**

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
