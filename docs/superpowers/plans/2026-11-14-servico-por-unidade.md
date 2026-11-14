# Serviço × médico × unidade (+ /contexto "Unidades ▾", sem "Sincronizar agenda") Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** O paciente só vê, no fluxo de agendamento, as unidades onde a combinação médico + serviço é feita; a clínica configura isso em /contexto; o botão "Sincronizar agenda" sai.

**Architecture:** Tabela nova `professional_service_units` (regra: sem linhas para (profissional, serviço) = todas as unidades do médico). O worker carrega as marcas no snapshot do roteador (sem I/O no roteador); uma função pura `flow_units.unit_choice` decide `free | none | implied | confirm | list`; o roteador pede um efeito (`notify_no_unit`) que o worker executa enviando 1 e-mail/dia à clínica. O Portal grava vínculos + horários + marcas numa única chamada atômica (`PUT /professionals/{id}/units`).

**Tech Stack:** secretarIA (FastAPI, SQLAlchemy async, Alembic, arq, pytest, uv) · Brain-Message-Frontend (Next.js export estático, TypeScript, vitest) · prova no navegador com agent-browser (Vercel).

**Spec:** `docs/superpowers/specs/2026-11-14-servico-por-unidade-design.md` (secretarIA). Ler antes; as "decisões minhas" da §9 (itens 1–14) valem como requisito.

**Worktrees e branches (todos já existem):** `C:\TECH\BRAIN-worktrees\TASK-083\secretarIA` e `...\Brain-Message-Frontend`, branch `task/TASK-083-multi-unidades`. Commits locais; **nada de push, merge ou deploy.** Nunca `git add -A` (outras sessões usam o mesmo worktree): sempre os arquivos da tarefa.

## Global Constraints

- Mensagem de confirmação (texto exato, dois botões fixos): `O serviço {serviço} feito por {médico} é feito apenas na unidade {unidade}, em {endereço}. Você confirma essa unidade?` — botões `✅ Sim` e `❌ Não`.
- Regra de dados: sem linhas para (profissional, serviço) ⇒ vale em todas as unidades do médico; com linhas ⇒ só nelas; nunca persistir "nenhuma"; entrada de serviço sem `service_id` ⇒ sem restrição.
- Pergunta Sim/Não só quando o médico atende em **2+** unidades reserváveis e o filtro deixa **1**; médico com 1 unidade só segue sem perguntar; sem vínculos = fluxo de hoje, byte a byte.
- "Não" e "0 unidades" voltam para a lista de médicos que fazem o serviço; clínica com um médico só (ou só um faz o serviço) ⇒ lista de serviços.
- E-mail à clínica: destino `Tenant.contact_email`; link `{BRAIN_MESSAGE_PUBLIC_URL sem barra final}/contexto/?prof=<professional_id>`; no máximo 1 por dia por (clínica, médico, serviço); sem dado de paciente, canal ou telefone; sem e-mail configurado ou falha de envio ⇒ só log sem PII, o fluxo do paciente nunca quebra.
- Isolamento por clínica em toda leitura/escrita; "não existe" e "é de outra clínica" respondem igual (404).
- Portal: seção chama-se **"Unidades"** (antes "Locais de atendimento"); botão recolhível **"Unidades ▾"**; salvar = **"Salvar unidades"** (antes "Salvar locais"); fica o selo "Agenda compartilhada"; **sai** o botão "Sincronizar agenda". A rota `POST /tenants/me/agendas/sync` do servidor permanece.
- Comandos secretarIA (cwd = worktree secretarIA): `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest <arquivos> -q -p no:cacheprovider`; lint só `uv run ruff check <arquivos>` (`make lint` e `ruff format .` ficam de fora: vermelhos no HEAD). Falhas já conhecidas: 2 em `tests/test_service_change_agenda.py` (UTC). Frontend (cwd = worktree Brain-Message-Frontend): `npm test`, `npm run typecheck`, `npm run build`; **nunca** `npm run lint`.
- Dados de teste apenas sintéticos; nenhum `.env`, nenhum serviço externo; nada de produção.
- Commit termina com `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Entrada de serviço **sem `service_id`** do médico que tem unidades marcadas para outros serviços: vale em todas, sem erro.
2. Paciente toca **❌ Não** em clínica de **um médico só** (ou serviço escolhido antes do médico): vai à lista de serviços, sem beco sem saída nem laço.
3. Mesma combinação com 0 unidades **duas vezes no mesmo dia** ⇒ 1 e-mail; clínica **sem `contact_email`** ⇒ paciente avisado, sem exceção.
4. Desvincular (ou desativar/fechar) a única unidade de um serviço restrito: PUT recusado com 422 e **nada** gravado; unidade fechada/desativada ⇒ caso "0 unidades", nunca "todas".
5. Dois `PUT` simultâneos do mesmo profissional ⇒ um vence (409), sem marcas órfãs.

---

## File Structure

secretarIA — criar: `src/secretaria/models/professional_service_unit.py`, `migrations/versions/c6e1a9f4d2b7_professional_service_units.py`, `src/secretaria/services/professional_service_units.py`, `src/secretaria/services/no_unit_notice.py`; modificar: `models/__init__.py`, `schemas/professional_unit.py`, `api/hub/professional_units.py`, `services/unit_booking.py`, `services/flow_units.py`, `services/flow_router.py`, `workers/shared/greeting.py` (snapshot), `workers/shared/flow_runner.py` (efeito), `ai/unit_scope.py`, `ai/tools.py`, `plugins/multi_professional.py`, `plugins/multi_unit.py`, `api/hub/calendar.py`, `docs/CHECKPOINT_agenda_multi_unidades.md`.
Frontend — modificar: `lib/contexto/professional-units.ts`, `components/contexto/{ProfessionalPlaces.tsx,useProfessionalUnits.ts,contexto.css}`, `lib/mock/units.ts` e testes em `lib/contexto/__tests__/professional-units.test.ts`, `components/contexto/__tests__/professional-places.test.tsx`.

## Ordem e paralelismo (arquivos sem sobreposição entre tarefas da mesma onda)

Onda 1: **T1** ‖ **F0** · Onda 2: **T2** ‖ **T3** · Onda 3: **T4** ‖ **T7** ‖ **F1** · Onda 4: **T5** ‖ **T6** ‖ **F2** · Onda 5: **T8** (prova). T4→T5 e T4→T6 são sequenciais (mesmo `flow_router.py`/efeito).

---

### Task T1: Modelo, migração e domínio das marcas

**Files:** Create `models/professional_service_unit.py`, `migrations/versions/c6e1a9f4d2b7_professional_service_units.py` (`down_revision = "b5d9e3f7a1c4"`), `services/professional_service_units.py`, `tests/test_professional_service_units.py`; Modify `models/__init__.py` (exportar `ProfessionalServiceUnit`).

**Interfaces — Produces:**
- Modelo `ProfessionalServiceUnit(professional_id, service_id, unit_id, created_at)`, PK dos três, FKs `ondelete="CASCADE"` para `professionals`, `services`, `units`.
- `class ServiceMarkError(Exception): code: str; service_ids: list[UUID]` — `code ∈ {"service_not_offered","unit_not_linked","service_without_unit"}`.
- `def allowed_unit_ids(marks: Mapping[UUID, frozenset[UUID]], service_id: UUID | None) -> frozenset[UUID] | None` (`None` = todas).
- `async def read_marks(session, professional_id: UUID) -> dict[UUID, frozenset[UUID]]` e `async def read_marks_for(session, professional_ids: Collection[UUID]) -> dict[UUID, dict[UUID, frozenset[UUID]]]` (uma consulta).
- `async def apply_marks(session, tenant_id: UUID, professional: Professional, linked_unit_ids: Collection[UUID], marks: Mapping[UUID, Collection[UUID]] | None) -> None` — `marks=None` parte das marcas gravadas; remove marcas de unidades não vinculadas; valida que cada serviço marcado está no catálogo ativo do profissional (por `entry_service_id`, `services/service_catalog.py`) e é da mesma clínica; um serviço que **era restrito** e ficou sem nenhuma unidade vinculada ⇒ `ServiceMarkError("service_without_unit", [...])`; não faz commit.

- [ ] **Step 1: Testes que falham** (`tests/test_professional_service_units.py`, SQLite como os vizinhos `test_professional_units.py`): `test_allowed_unit_ids_none_means_all`, `test_allowed_unit_ids_restricted`, `test_apply_marks_persists_subset`, `test_apply_marks_none_keeps_stored_and_drops_unlinked_unit`, `test_apply_marks_refuses_service_left_without_unit` (assert `exc.code == "service_without_unit"` e nenhuma linha mudou), `test_apply_marks_refuses_service_not_offered`, `test_apply_marks_refuses_other_tenant_unit` (`unit_not_linked`), `test_read_marks_for_is_one_query`.
- [ ] **Step 2:** Rodar `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_professional_service_units.py -q -p no:cacheprovider` → FAIL (ImportError).
- [ ] **Step 3:** Implementar modelo, migração aditiva (`upgrade` cria a tabela + índice em `service_id`; `downgrade` derruba) e o serviço com as assinaturas acima.
- [ ] **Step 4:** Mesmo comando → PASS; `uv run ruff check` nos arquivos; `uv run alembic heads` mostra um só head.
- [ ] **Step 5:** Commit `feat(units): professional_service_units table and domain (TASK-083 follow-up T1)`.

### Task T2: API do hub — marcas no GET/PUT atômico

**Files:** Modify `schemas/professional_unit.py`, `api/hub/professional_units.py`; Test `tests/test_hub_professional_service_units.py` (novo, modelo: `tests/test_hub_units.py`).

**Interfaces — Consumes:** T1 (`read_marks`, `apply_marks`, `ServiceMarkError`). **Produces:**
- `ServiceUnitMark{service_id: str, unit_ids: list[str] (min_length=1)}`; `ProfessionalUnitsUpdate.service_marks: list[ServiceUnitMark] | None = None` (`None` = não mexer; lista = estado completo das restrições; serviço ausente = "todas").
- `ProfessionalUnitsRead` ganha `services: list[{service_id: str, name: str}]` (catálogo ativo do profissional com `service_id`) e `service_marks: list[ServiceUnitMark]` (só serviços restritos).
- Erro: HTTP 422 `detail={"code": "service_without_unit"|"service_not_offered"|"unit_not_linked", "service_ids": [str]}`; `IntegrityError` ⇒ 409 `professional_units_changed` (como hoje).

- [ ] **Step 1: Testes que falham:** `test_get_returns_services_and_marks`, `test_put_links_hours_and_marks_in_one_commit`, `test_put_without_service_marks_leaves_marks_untouched`, `test_put_unlinking_only_unit_of_restricted_service_is_422_and_nothing_saved` (relê o GET e compara), `test_put_marks_for_service_not_offered_is_422`, `test_put_marks_unit_of_other_tenant_is_404`, `test_put_requires_manager_or_own_professional` (403 como o existente), `test_two_concurrent_puts_one_wins_409`.
- [ ] **Step 2:** Rodar o arquivo → FAIL.
- [ ] **Step 3:** No `put_professional_units`, depois de `replace_links` e antes do `commit`, chamar `apply_marks` com os ids vinculados e `body.service_marks`; mapear `ServiceMarkError` ⇒ 422 com rollback; `_read` preenche `services` e `service_marks`.
- [ ] **Step 4:** Rodar o arquivo + `tests/test_hub_units.py tests/test_professional_units.py` → PASS; ruff.
- [ ] **Step 5:** Commit `feat(units): hub reads/writes service marks atomically with unit links (T2)`.

### Task T3: Snapshot do roteador + decisão pura `unit_choice`

**Files:** Modify `services/unit_booking.py` (`attach_unit_options` também grava `snapshot.service_units`), `services/flow_units.py`, `workers/shared/greeting.py` (se o snapshot precisar do campo padrão `service_units={}`); Test `tests/test_flow_units_service.py` (novo).

**Interfaces — Consumes:** T1 `read_marks_for`, `allowed_unit_ids`. **Produces** (em `flow_units.py`):
```python
@dataclass(frozen=True)
class UnitChoice:
    kind: Literal["free", "none", "implied", "confirm", "list"]
    units: list  # os UnitOption oferecidos (vazio em free/none)
def service_units_of(professional: Any | None) -> dict[UUID, frozenset[UUID]]   # getattr(..., "service_units", {})
def units_for_service(professional: Any | None, service_id: UUID | None) -> list # bookable ∩ allowed
def unit_choice(professional: Any | None, service_id: UUID | None) -> UnitChoice
def effective_unit(conversation, professional, service_id: UUID | None = None)  # escolhida (se ∈ filtrada) | única filtrada em "implied" | None
```
`kind`: sem vínculos ⇒ `free`; filtrada vazia ⇒ `none`; 1 filtrada e `len(bookable) >= 2` ⇒ `confirm`; 1 filtrada e `len(bookable) == 1` ⇒ `implied`; 2+ ⇒ `list`. `service_id=None` ⇒ sem filtro (comportamento atual). `unit_choice_needed`, `find_unit` e `bookable_units` existentes mantêm a assinatura (parâmetro novo opcional).

- [ ] **Step 1: Testes:** `test_free_when_no_links`, `test_list_when_two_allowed`, `test_confirm_when_filter_leaves_one_of_two`, `test_implied_when_doctor_has_single_unit`, `test_none_when_restricted_units_closed` (unidade marcada sem horário), `test_service_without_marks_allows_all`, `test_service_id_none_equals_today`, `test_effective_unit_ignores_pick_outside_filter`, `test_attach_unit_options_sets_service_units_one_query`.
- [ ] **Step 2:** rodar → FAIL. **Step 3:** implementar; `attach_unit_options` chama `read_marks_for` uma vez para o roster. **Step 4:** rodar + `tests/test_flow_router_units.py tests/test_unit_booking.py` (regressão: tudo igual) → PASS.
- [ ] **Step 5:** Commit `feat(units): service-aware unit choice (pure) and roster marks (T3)`.

### Task T4: Roteador — filtro, lista, caso 0 e voltas

**Files:** Modify `services/flow_router.py` (`_ask_day` ~l.4144, `_enter_unit_list` ~l.4225, `_handle_unit_step`, `_booking_back_target`, `_carry_unit`/`UNIT_KEEP_STEPS`, `FlowRouterResult`), `workers/shared/flow_runner.py` (consumir o efeito: **só repassa**, o e-mail é T6); Test `tests/test_flow_router_units.py` (estender).

**Interfaces — Consumes:** T3 `unit_choice`, `units_for_service`, `effective_unit`; `service_catalog.entry_service_id`. **Produces:**
- `def _booking_service_id(conversation, tenant, services, professional) -> UUID | None` — `_match_service(services, conversation.flow_selected_type)` → `entry_service_id(entry)`; falha de casamento ⇒ `None` (sem filtro; docstring: entrada sem `service_id` é sem restrição por definição).
- `FlowRouterResult.notify_no_unit: tuple[UUID, UUID, str] | None = None` (`professional_id`, `service_id`, nome do serviço), default `None`.
- `def _enter_no_unit_for_service(conversation, professionals, professional, service_name) -> FlowRouterResult` — texto leigo ao paciente ("Não consegui seguir com {serviço} com {médico} agora. Avisei a clínica. Vamos escolher outro médico?"), volta à lista de médicos que fazem o serviço (reusar o renderizador de `STEP_AWAITING_PROFESSIONAL`, ~l.2546) ou, com um médico só, à lista de serviços; devolve `notify_no_unit`.
- `_ask_day`: `choice = flow_units.unit_choice(professional, service_id)`; `free` ⇒ como hoje; `none` ⇒ `_enter_no_unit_for_service`; `implied` ⇒ usa a unidade; `list` ⇒ `_enter_unit_list` só com `choice.units`; `confirm` ⇒ T5. `BACK_TARGET_UNIT`/`unit_choice_needed` passam a usar `choice.kind == "list"`.

- [ ] **Step 1: Testes:** `test_unit_list_shows_only_units_of_service` (rows = só as marcadas), `test_service_without_marks_lists_all_units`, `test_zero_units_sets_notify_no_unit_and_returns_to_doctor_list`, `test_zero_units_single_doctor_clinic_returns_to_service_list`, `test_service_picked_first_then_doctor_filters_units`, `test_no_service_id_entry_is_unrestricted`, `test_change_service_clears_unit_and_refilters`, `test_back_row_unit_only_when_list` + regressão: a suíte atual de `test_flow_router_units.py` passa sem mudança.
- [ ] **Step 2:** rodar → FAIL. **Step 3:** implementar conforme Interfaces. **Step 4:** rodar `tests/test_flow_router_units.py tests/test_flow_router.py tests/test_booking_draft*.py` → PASS (separar falhas já conhecidas); ruff.
- [ ] **Step 5:** Commit `feat(units): router filters units by doctor+service and handles the empty case (T4)`.

### Task T5: Passo de confirmação "Sim / Não"

**Files:** Modify `services/flow_units.py` (constantes), `services/flow_router.py` (novo passo); Test `tests/test_flow_router_units.py`.

**Interfaces — Consumes:** T4. **Produces:** `flow_units.STEP_AWAITING_UNIT_CONFIRM = "awaiting_unit_confirm"`, `flow_units.UNIT_CONFIRM_BODY = "O serviço {servico} feito por {medico} é feito apenas na unidade {unidade}, em {endereco}. Você confirma essa unidade?"`, `flow_router._enter_unit_confirm(conversation, professional, unit, service_name) -> FlowRouterResult` (dois botões de resposta rápida fixos `✅ Sim` / `❌ Não`, mesmo helper dos outros passos fixos sim/não; unidade **não** gravada ainda) e `_handle_unit_confirm_step(...)`: Sim ⇒ grava `flow_selected_unit_id` e abre o dia dessa unidade; Não ⇒ mesmo destino do caso 0 (lista de médicos / serviços) **sem** e-mail; texto livre ⇒ uma volta do LLM no mesmo passo (botões continuam). O passo **não** está em `UNIT_KEEP_STEPS`.

- [ ] **Step 1: Testes:** `test_confirm_message_exact_text_and_buttons`, `test_yes_selects_unit_and_opens_day_picker`, `test_no_returns_to_doctor_list_without_email_effect`, `test_no_single_doctor_goes_to_service_list`, `test_doctor_with_one_unit_skips_confirm`, `test_free_text_on_confirm_step_delegates_once`, `test_confirm_survives_resume` (conversa retomada reabre a confirmação).
- [ ] **Step 2–4:** FAIL → implementar → PASS (+ `tests/test_flow_router_units.py` completo). **Step 5:** Commit `feat(units): confirm the only unit with Sim/Não (T5)`.

### Task T6: E-mail à clínica (nenhuma unidade possível)

**Files:** Create `services/no_unit_notice.py`; Modify `workers/shared/flow_runner.py` (chamar o notificador quando `result.notify_no_unit`); Test `tests/test_no_unit_notice.py` (novo; espelhar `tests/test_deposit_failure_notice.py`).

**Interfaces — Consumes:** T4 `notify_no_unit`; contador diário e envio de `services/payments/deposit_failure_notice.py` (ler `_counter_key` e o remetente usado lá antes; **reusar**, não duplicar). **Produces:** `def no_unit_link(tenant: Tenant, professional_id: UUID) -> str` e `async def send_no_unit_notice(tenant: Tenant, professional_id: UUID, professional_name: str, service_id: UUID, service_name: str) -> bool` (`True` = enviado; chave do contador `(tenant, professional, service, dia local)`; sem `contact_email` ⇒ `False`; falha de envio ⇒ `False` + log sem PII; nunca levanta).

- [ ] **Step 1: Testes:** `test_link_points_to_contexto_with_prof`, `test_first_notice_sent_second_same_day_skipped`, `test_next_day_sends_again`, `test_different_service_same_day_sends`, `test_no_contact_email_returns_false`, `test_send_failure_never_raises`, `test_body_has_service_doctor_link_and_no_patient_data` (o corpo não contém telefone/nome de paciente do fixture), `test_flow_runner_calls_notice_on_effect_and_survives_failure`.
- [ ] **Step 2–4:** FAIL → implementar → PASS. **Step 5:** Commit `feat(units): one e-mail a day to the clinic when a service has no valid unit (T6)`.

### Task T7: Bloqueio na escrita (IA e paciente) e aviso na equipe

**Files:** Modify `services/unit_booking.py` (`check_unit(..., service_id: UUID | None = None)`; nova recusa `UnitRefused("unit_service_not_offered", "Esse serviço não é feito nessa unidade.", 422)`), `ai/unit_scope.py`, `ai/tools.py`, `plugins/multi_professional.py`, `plugins/multi_unit.py` (passar o `service_id` da entrada escolhida; `list_units` só devolve as unidades válidas da combinação; erro claro + reoferta), `api/hub/calendar.py` (equipe: **não bloqueia**; resposta ganha `unit_service_not_offered: bool`); Test `tests/test_unit_booking_ai_tools.py`, `tests/test_hub_calendar_units.py` (estender).

**Interfaces — Consumes:** T3 `units_for_service`/`service_units_of`. **Produces:** `check_unit` com o parâmetro novo (compatível: `None` ⇒ comportamento atual).

- [ ] **Step 1: Testes:** `test_check_unit_refuses_service_not_offered_at_unit`, `test_check_unit_without_service_id_unchanged`, `test_ai_list_units_only_valid_for_service`, `test_ai_create_event_at_invalid_unit_returns_clear_error_and_valid_units`, `test_hub_calendar_create_invalid_combo_succeeds_with_warning_flag`.
- [ ] **Step 2–4:** FAIL → implementar → PASS (+ `tests/test_unit_booking*.py tests/test_ai_toolset_v2.py tests/test_appointment_edit_units.py`: remarcar segue na unidade original). **Step 5:** Commit `feat(units): AI/patient writes refuse a unit where the service is not done; staff hub warns (T7)`.

### Task F0: Remover "Sincronizar agenda"

**Files (frontend):** Modify `components/contexto/ProfessionalPlaces.tsx` (bloco `SharedAgendaBlock`: manter só o selo; remover `canSync`, `onSync`, `api.sync`), `components/contexto/useProfessionalUnits.ts` (tirar `syncAgendas` do `REAL_HUB`), `lib/contexto/professional-units.ts` (remover `syncSummary`, `canSyncAgendas`, textos `sync*`, estado `syncing`, ações do reducer e do controller ligadas a sync; tipos órfãos como `AgendaSyncWire`), e o cliente HTTP que exporta `syncAgendas` (apagar só se sem outro uso: `grep -rn syncAgendas`); Test: ajustar `lib/contexto/__tests__/professional-units.test.ts`, `components/contexto/__tests__/professional-places.test.tsx`.

- [ ] **Step 1: Testes:** `renders no "Sincronizar agenda" button for a manager` (ausência do texto), `still renders the shared-agenda seal with the other-clinics count`, `controller has no sync action`.
- [ ] **Step 2:** `npm test -- professional-places professional-units` → FAIL. **Step 3:** remover o listado acima. **Step 4:** `npm test`, `npm run typecheck` → PASS. **Step 5:** Commit `feat(contexto): remove the "Sincronizar agenda" button; the agenda links itself (F0)`.

### Task F1: Marcas no estado, no PUT e no mock

**Files (frontend):** Modify `lib/contexto/professional-units.ts`, `lib/mock/units.ts`, `components/contexto/useProfessionalUnits.ts`; Test `lib/contexto/__tests__/professional-units.test.ts`.

**Interfaces — Consumes:** contrato de T2 (`services`, `service_marks` no GET e no PUT). **Produces** (puro, em `professional-units.ts`):
```ts
export type OfferedService = { service_id: string; name: string };
export type ServiceMarks = Record<string, string[]>;            // service_id -> unit ids; ausente = todas
export function marksFromWire(wire: ProfessionalUnitsWire): ServiceMarks;
export function toggleServiceAtUnit(marks: ServiceMarks, services: readonly OfferedService[], linkedUnitIds: readonly string[], serviceId: string, unitId: string, on: boolean): ServiceMarks; // nunca deixa vazio; todas marcadas ⇒ remove a chave
export function serviceCheckedAt(marks: ServiceMarks, serviceId: string, unitId: string): boolean;
export function marksPutBody(rows: readonly PlaceRow[], marks: ServiceMarks): ProfessionalUnitsPutPayload; // inclui service_marks
export function placesDirty(...)  // passa a considerar também as marcas
export const PLACES_COPY // "Unidades", "Serviços feitos aqui", "Salvar unidades", erro de "última unidade do serviço" em linguagem leiga
```
Estado do controller guarda `services` e as marcas (salvas e rascunho); `service_without_unit` do servidor vira mensagem listando os serviços.

- [ ] **Step 1: Testes:** `toggleServiceAtUnit refuses to uncheck the last unit of a service`, `checking every unit removes the restriction key`, `unlinking a unit drops its marks from the draft`, `marksPutBody sends service_marks only for restricted services`, `placesDirty is true when only a mark changed`, `server service_without_unit becomes a plain-language message`, `mock hub round-trips marks`.
- [ ] **Step 2–4:** FAIL → implementar → `npm test`, `npm run typecheck` → PASS. **Step 5:** Commit `feat(contexto): service marks per unit in the places state (F1)`.

### Task F2: Cartão "Unidades ▾" + serviços por unidade

**Files (frontend):** Modify `components/contexto/ProfessionalPlaces.tsx`, `components/contexto/contexto.css`, `lib/contexto/professional-units.ts` (copy: "Locais de atendimento" → "Unidades" em tudo; `placesSummary` ⇒ "N unidades"); Test `components/contexto/__tests__/professional-places.test.tsx`, `lib/contexto/__tests__/places-layout-css.test.ts`.

**Interfaces — Consumes:** F1. **Produces:** `ProfessionalPlacesView` com um botão `Unidades` + ícone de seta (`aria-expanded`, recolhido por padrão, resumo "2 unidades" no botão); aberto, lista as unidades da clínica com a chave e, **em cada unidade ligada, sempre visíveis**: horário (geral / "Definir horário próprio") e "Serviços feitos aqui" com uma caixa por serviço ativo do profissional (`OfferedService`); botão **"Salvar unidades"** + "Descartar"; caixa da última unidade de um serviço desabilitada com a dica "Para o médico parar de fazer este serviço, desligue-o em Serviços oferecidos.". Visão sem hooks (props in, callbacks out), como hoje.

- [ ] **Step 1: Testes:** `is collapsed by default and shows the unit count`, `opens on click and shows each linked unit's hours and services without another click`, `unlinked unit shows no services block`, `unchecking the only unit of a service is blocked with the hint`, `Salvar unidades appears only when dirty`, `copy says "Unidades", never "Locais de atendimento"`.
- [ ] **Step 2–4:** FAIL → implementar (CSS no padrão do arquivo; não quebrar `places-layout-css.test.ts`) → `npm test`, `npm run typecheck`, `npm run build` → PASS. **Step 5:** Commit `feat(contexto): collapsible "Unidades" card with per-unit services (F2)`.

### Task T8: Prova ponta a ponta (navegador real + e-mail) e documentação

**Files:** Modify `docs/CHECKPOINT_agenda_multi_unidades.md` (seção nova: serviço × unidade, e-mail, remoção do botão, deploy: migração `c6e1a9f4d2b7` → secretarIA API **e worker** → frontend); `_local-test/seed_sec.py` (segunda unidade "Unidade Centro" + 2 serviços, dados fictícios); Create `tasks/TASK-083/results/servico-por-unidade.md` (relatório do worker, modelo `tasks/RESULT_TEMPLATE.md`).

- [ ] **Step 1:** Subir a stack local (`_local-test/run.sh migrate`, `seed`, `brain`, `sec`, `worker`, `sink`, `gw`; Docker Desktop ligado; reconstruir o front com `npm run build` no worktree do Brain-Message-Frontend; o gateway serve `out/`).
- [ ] **Step 2 (agent-browser da Vercel, sessão nova):** em `http://127.0.0.1:8905/contexto` — abrir o cartão, ligar 2 unidades, marcar "Consulta" só na Sede, "Retorno" nas duas, **Salvar unidades**, recarregar e conferir persistência; confirmar que não existe "Sincronizar agenda"; tentar desmarcar a última unidade (bloqueado); desvincular a Sede com "Consulta" restrita (erro leigo, nada salvo).
- [ ] **Step 3 (chat do Portal, scripts `say.sh`/`tap.sh`):** Consulta ⇒ mensagem exata de confirmação + ✅ Sim ⇒ dias da Sede; ❌ Não ⇒ lista de médicos; Retorno ⇒ lista com 2 unidades; fechar o horário da Sede e repetir Consulta ⇒ aviso ao paciente + 1 e-mail em `/__mail` (link `/contexto/?prof=...`, sem dado de paciente); repetir ⇒ **continua 1**.
- [ ] **Step 4:** Suíte completa secretarIA (`BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest -q -p no:cacheprovider`) comparada com o HEAD anterior (`c9e05f8`): só as falhas já conhecidas; `npm test`, `npm run typecheck`, `npm run build` no front; ruff nos arquivos tocados.
- [ ] **Step 5:** Escrever o relatório com evidências (capturas, saída dos testes), atualizar `tasks/TASK-083/TASK.md` (local, **não** em main, **não** deployado) e commitar docs/seed. Desligar a stack de teste.

---

## Self-review (feito)

- **Cobertura da spec:** §6.1 → T1–T3, T4, T5, T7; §6.2 → F0–F2; §6.3 → T4 (efeito) + T6; §7 → F0; §8 → testes em cada tarefa + T8; §9 itens 1–14: 1 (T3/T5), 2 (F0 mantém a rota), 3 (T7), 4 (regressão de remarcação em T7 Step 4), 5 (T1/T2), 6 (T1/T4), 8 (T4/T5), 9 (T4), 10 (T2/F1), 11 (T4 `_booking_service_id`), 12 (T3 `none`), 13 (T7), 14 (T3).
- **Consistência de tipos:** `unit_choice`/`UnitChoice` (T3) usados em T4/T5; `notify_no_unit` (T4) consumido em T6; `service_marks`/`services` (T2) consumidos em F1/F2; `allowed_unit_ids`/`read_marks_for` (T1) em T3.
- **Proporção:** tarefas listam assinaturas, nomes de teste e valores exatos da spec; corpos ficam com o implementador.
