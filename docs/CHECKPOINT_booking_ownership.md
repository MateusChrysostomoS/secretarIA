# CHECKPOINT — Dono do agendamento, serviço canônico e escopo de tools do agente

Built 2026-08-17. Três correções que são o mesmo assunto visto de três ângulos: **toda
consulta pertence a UM profissional, e o serviço agendado é um item do catálogo — não um
texto livre**. Continuação direta de `CHECKPOINT_onboarding_multiprofessional.md`
(camada por profissional), `CHECKPOINT_multi_doctor_flow.md` (fluxo multi-médico) e
`CHECKPOINT_pix_deposit.md` (quem consome esses dois campos).

Suite state: **1382 passed** (`uv run python -m pytest -q`) — baseline era 1304, então
**+78 testes novos, zero regressão**. `ruff check .` continua com os mesmos **8 achados
pré-existentes** (3 × `UP042` nos enums, `E501` em `scripts/chat_tenant.py`,
`tests/test_ehr_plugin.py`, `tests/test_post_booking_plugin.py`) — nenhum novo, nenhum
mascarado com `noqa`.

**Status: NÃO COMMITADO / NÃO DEPLOYADO.** Sem migração. Ver "Pendências".

---

## 1. O problema (um sintoma, três causas)

Clínica real com **um único profissional ativo**: serviços, preços e horários todos
configurados no profissional, `tenants.appointment_types` legado **vazio**. O paciente
listava serviços, escolhia horário, o evento nascia no Google — e o sinal Pix **nunca era
cobrado**. O log dizia `pix_deposit_skipped_unparseable_price`, o que parecia problema de
preço. Não era.

### 1a. O fluxo determinístico perdia o dono

`workers/tasks.py::_flow_tenant_snapshot` já resolvia serviços/horários **pelo único
profissional** (é o mesmo "exatamente um profissional É a clínica" do
`load_tenant_config`), mas não levava a identidade dele adiante.
`flow_router::_handle_confirmation` só escrevia `professional_id` quando havia **seleção
multi-profissional** — que single-prof nunca faz. Resultado:
`Appointment.professional_id = NULL`.

Aí `deposit_lifecycle::_price_text_for_appointment` fazia exatamente o que está escrito
nele: sem `professional_id`, procura o preço no catálogo **do tenant** — o legado, vazio.
Preço não encontrado → sem cobrança, silenciosamente.

### 1b. A tool base gravava o título do Google como serviço

`ai/tools.py::create_event` passava o `summary` (título livre do evento, ex.
`"Consulta - João Silva"`) como `appointment_type`, e `professional_id=None` por padrão.
`Appointment.appointment_type` é lido rio abaixo como **chave de catálogo** (o Pix casa
por nome exato); um título livre não casa com nada.

### 1c. O agente multi-profissional ainda tinha as tools erradas na mão

`ai/graph.py::_BASE_TOOLS` sempre incluía `check_availability`, `list_free_slots`,
`create_event` e `cancel_event` — todas tenant-level. Num tenant multi-profissional o
prompt pedia para usar as tools por profissional, mas **só o prompt**. Entitlement não
resolve isso: `plugins/registry.py` apenas **adiciona** tools, nunca remove a alternativa
insegura. Uma escolha ruim do modelo criava evento na agenda da clínica e Appointment com
`professional_id=NULL` — o mesmo estado do item 1a, por outro caminho.

---

## 2. A decisão

Uma regra só, em um lugar só: **`services/booking_scope.py`** — funções puras, sem
nenhum import além da stdlib, para que `services/`, `ai/`, `plugins/` e `workers/` possam
compartilhá-las sem ciclo de import.

| Função | Regra |
| --- | --- |
| `booking_topology(professionals)` | `None` → `unknown` (não sabemos), `[]` → `none`, 1 → `sole`, 2+ → `multi`. "Ninguém" e "não sei" são coisas diferentes. |
| `sole_active_professional(...)` | O profissional quando há **exatamente um**; senão `None`. |
| `resolve_booking_owner_id(roster, selected_id)` | Seleção explícita ganha, **mas só se estiver no roster ativo**; sem seleção, o único ativo é o dono; zero ou 2+ sem seleção → `None`. **Nunca inventa dono, nunca chuta pela ordem do roster.** |
| `canonical_service_name(catalog, candidate)` | Nome do catálogo (case/espaço-insensível, ciente do truncamento de 24 chars das linhas do WhatsApp). `None` = "esta clínica não tem esse serviço" → falhar fechado. |

Consequência de desenho: **quem teve o catálogo renderizado é provadamente quem é dono da
reserva** — `_flow_tenant_snapshot` e `resolve_booking_owner_id` usam o mesmo
`sole_active_professional` sobre o mesmo roster que acompanha o snapshot em
`route(professionals=...)`.

---

## 3. O que entrou onde

### `services/booking_scope.py` (novo)
As quatro regras acima + `service_entry_name`/`service_names` (o catálogo circula em duas
formas: dicts armazenados e `RuntimeAppointmentType`).

### `services/flow_router.py`
- `_match_service` delega o casamento de nome para `canonical_service_name` (uma regra só).
- `_handle_confirmation` recebe `professionals` e resolve o dono por
  `resolve_booking_owner_id`; grava o **nome canônico** do serviço (cai para o rótulo
  guardado se o serviço foi renomeado/desativado no meio do fluxo — a reserva acontece, o
  Pix é que pula com motivo honesto).
- **Convênio (correção 19):** `_wants_insurance_step` virou
  `_insurance_step_skip_reason(tenant)` e **não exige mais seleção de profissional**.
  `collect_insurance`/`insurances` são configuração **da clínica** e a resposta é
  informativa; agora zero/um/vários profissionais recebem a mesma pergunta depois de
  confirmar o serviço. Motivos: `disabled`, `empty_catalog`.

### `ai/tools.py`
- Novo `_booking_topology_ctx` (ContextVar, mesmo padrão dos outros; `run_agent` seta e
  reseta no `finally`).
- `create_event` ganhou `appointment_type` **separado de `summary`**: `summary` é o título
  do Google, `appointment_type` é o serviço. Resolução: casa no catálogo → nome canônico;
  não casa → **erro listando as opções válidas, sem criar evento**; omitido com catálogo
  de 1 item → deriva; omitido com vários → exige; **sem catálogo → `NULL`** (honesto; o
  título livre não é).
- `_sole_professional_id()` usa `TenantRuntimeConfig.professional_id` **apenas** em turno
  `sole` — em outra topologia esse campo pode ter sido sobreposto pelo profissional
  selecionado só para o prompt (`graph._config_with_selected_professional`), e isso não é
  dono para uma tool tenant-level.
- `_blocked_tenant_level(tool)`: `check_availability`, `list_free_slots`, `create_event`,
  `cancel_event` **falham fechado em turno `multi`, antes de qualquer chamada ao Google**.
- `_persist_appointment` documenta que `appointment_type` já chega canônico e emite
  `booking_owner_resolved` / `booking_service_resolved`.

### `ai/graph.py`
- `_BASE_TOOLS` foi separado em `_TENANT_LEVEL_CALENDAR_TOOLS` + `_SCOPE_FREE_TOOLS`
  (`iniciar_pre_consulta`, `list_patient_appointments`, `show_main_menu`). O nome
  `_BASE_TOOLS` e seu conteúdo continuam idênticos.
- `base_tools_for(topology)`: `multi` recebe **só** as scope-free. Independente de
  entitlement — multi sem o addon fica sem nenhuma tool de agendamento e degrada para o
  menu determinístico, que já sabe fazer multi-médico. Degradar para a agenda tenant-level
  era exatamente o erro.
- `build_agent(extra_tools, topology)`: a chave de cache é o `frozenset` dos nomes do
  conjunto **efetivo**, então topologias/entitlements diferentes nunca compartilham grafo.
- `run_agent(..., booking_topology=...)` + log `agent_capabilities_resolved`.

### `plugins/multi_professional.py` / `plugins/multi_unit.py`
- `create_event_for_professional` ganhou `appointment_type`, validado contra o catálogo
  **daquele profissional** (`_professional_services`).
- `create_event_at_unit` é mutação tenant-level também (usa `_get_calendar()`), então
  recebeu o mesmo guard + `appointment_type`.

### `services/payments/deposit_lifecycle.py`
- `_resolve_service_and_price` separa "não existe esse serviço" de "existe mas sem preço
  legível" e casa pelo mesmo `canonical_service_name`.
- `_log_deposit_skip`: cada motivo mantém **o nome de evento histórico**
  (`pix_deposit_skipped_<reason>`) e ganha `reason`/`tenant_id`/`appointment_id`/
  `professional_id`/`has_owner`. Motivo novo: `no_service` (antes caía em
  `unparseable_price`) — é o interessante, porque é problema de configuração/ownership,
  não de preço.

### `workers/tasks.py`
- `_flow_tenant_snapshot` usa `sole_active_professional`.
- `run_agent(booking_topology=booking_topology(professional_rows))`.
- `_log_booking_scope` emite os dois eventos no ponto de persistência do fluxo.

### `ai/prompts.py`
Duas frases novas: as tools de agenda da clínica **não são oferecidas** em clínica
multi-profissional (e se as por-profissional também não estiverem, chame `show_main_menu`);
e `summary` ≠ `appointment_type`.

### `scripts/report_ownerless_appointments.py` (novo, READ-ONLY)
Relatório dos agendamentos **futuros** já salvos sem dono: quantos existem, quantos são
resolvíveis sem ambiguidade (clínica com um único profissional hoje) e quantos têm
`appointment_type` que não casa mais com o catálogo. **Não faz backfill** — mudar a quem
uma consulta passada pertence mexe em analytics e metering, então é procedimento
operacional separado, auditável e reversível, nunca efeito colateral de deploy.

---

## 4. Observabilidade (tudo sanitizado — só ids e enums)

| Evento | Onde | Campos |
| --- | --- | --- |
| `booking_owner_resolved` | `ai/tools.py`, `workers/tasks.py` | tenant_id, appointment_id, professional_id, has_owner, source |
| `booking_service_resolved` | idem | tenant_id, appointment_id, professional_id, has_type, source |
| `agent_capabilities_resolved` | `ai/graph.py` | conversation_id, tenant_id, topology, capabilities (nomes de tool), capability_count |
| `agent_tool_blocked` | `ai/tools.py` | tool, reason (`wrong_topology`/`unknown_service`/`ambiguous_service`), topology, tenant_id |
| `insurance_step_presented` / `insurance_selected` / `insurance_step_skipped` | `flow_router.py` | conversation_id, plan_count / from_catalog / reason |
| `pix_deposit_skipped_*` | `deposit_lifecycle.py` | reason, tenant_id, appointment_id, professional_id, has_owner |

Nada de nome, telefone, texto clínico, preço, nome de plano ou conteúdo de prompt.
`insurance_selected` registra **se** o plano veio do catálogo, nunca **qual**.

---

## 5. Testes

Novos (78):

- `tests/test_booking_scope.py` — a matriz das regras puras.
- `tests/test_booking_owner_persistence.py` — o tenant quebrado (catálogo legado vazio +
  um profissional com preço) reproduzido: fluxo e tool gravam o mesmo dono e o mesmo tipo
  canônico; serviço inexistente falha fechado sem evento nem linha; catálogo vazio grava
  `NULL`; preço resolve pelo profissional e **não** resolve sem dono; e o E2E com Meta/
  Google/Asaas fakes provando **duas reservas → dois enqueues → exatamente uma cobrança
  Pix cada**.
- `tests/test_flow_router_insurance.py` — a tabela on/off × vazio/preenchido × 0/1/vários.
- `tests/test_agent_tool_enforcement.py` — nomes exatos das tools por topologia ×
  entitlement, invocação defensiva falhando sem tocar no calendário, isolamento de
  cache/ContextVar entre dois tenants concorrentes.

Alterado: `tests/test_flow_router_multiprofessional.py::test_single_professional_flow_never_asks_insurance`
virou `test_tenant_level_flow_also_asks_insurance` — o teste antigo **exigia** o bug da
correção 19.

---

## 6. Pendências

1. **Commit + push + deploy dos DOIS serviços** (`secretaria_api` e `secretaria-worker`,
   mesmo SHA — a regra de paridade do `CLAUDE.md`; isto toca `workers/` e `flow_router.py`,
   então o worker é obrigatório). Sem migração.
2. **Smoke com tenant/número autorizados e Asaas sandbox/fake.** Confirmar
   `booking_owner_resolved` com `has_owner=true` e uma cobrança por reserva; monitorar
   `pix_deposit_skipped_no_service` (novo) e `agent_tool_blocked`.
3. **Rollback**: reverter API e worker juntos. Não apagar appointments nem cobranças.
4. **Reconciliação de linhas antigas**: rodar
   `scripts/report_ownerless_appointments.py` em produção e decidir caso a caso. Nada
   automático.
5. **Hub**: a UI de convênio agora produz comportamento em qualquer clínica — vale
   revisar o texto do toggle para deixar explícito que o dado é **informativo** (não filtra
   agenda por plano).
