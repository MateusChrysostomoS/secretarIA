# Serviço × unidade — follow-up do dono (endereço obrigatório, "Alterar dados" com unidade, IA com as combinações) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Origem:** pedido do dono em 2026-11-14, depois da entrega do plano `2026-11-14-servico-por-unidade.md` (mesma branch `task/TASK-083-multi-unidades`, commits locais; nada de push/merge/deploy nesta fase). Spec de base: `docs/superpowers/specs/2026-11-14-servico-por-unidade-design.md` (continua valendo). Pedido do dono, resumido:

1. A unidade não pode ficar sem endereço; o erro aparece durante o preenchimento em /contexto.
2. "Alterar dados": (a) se o novo serviço não é feito na unidade da consulta, um novo passo mostra as unidades (do médico) onde o serviço é feito; (b) se o novo médico não faz o serviço atual na unidade pré-selecionada, mostrar primeiro os serviços que esse médico oferece nessa unidade, com um botão "Alterar unidade" — **só** quando o médico faz o serviço pré-selecionado em outra unidade; ao tocar, lista as unidades onde o médico atende e, em seguida, os serviços que ele oferece na unidade escolhida.
3. Prioridade é médico e serviço; unidade é secundária. A IA precisa conhecer as combinações profissional × serviço × unidade para ajudar o paciente a marcar (inclusive quando ele quer outra unidade).

## Global Constraints

- Regra de dados (inalterada): sem linhas em `professional_service_units` para (profissional, serviço) ⇒ todas as unidades do médico; com linhas ⇒ só nelas; entrada sem `service_id` ⇒ sem restrição. "Unidades do médico" para oferecer ao paciente = `flow_units.bookable_units` (vinculada, ativa, com horário).
- Clínica sem unidades / consulta sem `unit_id` ⇒ "Alterar dados" e prompt da IA idênticos ao de hoje (byte a byte nos textos já existentes).
- Endereço: `address` obrigatório (não vazio após `strip`) ao criar unidade e ao editar (não pode virar vazio/null). Erro do servidor: HTTP 422 `detail={"code": "unit_address_required", "message": "Informe o endereço da unidade."}`. Unidades antigas sem endereço continuam existindo: a tela as marca com erro e pede o endereço; o fluxo do paciente mantém o texto de reserva sem endereço só como defesa.
- Textos novos ao paciente (exatos):
  - `EDIT_UNIT_FOR_SERVICE_BODY = "O serviço {servico} é feito nestas unidades. Em qual você prefere?"`
  - `EDIT_SERVICES_AT_UNIT_BODY = "{medico} atende na unidade {unidade} com estes serviços. Qual você quer?"`
  - `EDIT_DOCTOR_UNITS_BODY = "Em qual unidade você prefere ser atendido por {medico}?"`
  - Linha extra da lista de serviços: título `"Alterar unidade"`, descrição `"Ver serviços em outra unidade"`, id `edit|change_unit`.
  - Botões de lista: unidades `"Ver unidades"`/seção `"Unidades"`; serviços `"Ver serviços"`/seção `"Serviços"` (como hoje).
- Isolamento por clínica em toda leitura; nenhum dado de paciente em log.
- Comandos secretarIA (cwd = seu worktree): `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest <arquivos> -q -p no:cacheprovider`; lint só `uv run ruff check <arquivos>`. Falhas já conhecidas: 2 em `tests/test_service_change_agenda.py` (UTC); ordem-dependentes em `test_ai_toolset_v2`/agent-cache quando `llm_eval` roda antes. Frontend: `npm test`, `npm run typecheck`, `npm run build`; nunca `npm run lint`.
- Commits só com os arquivos da tarefa (nunca `git add -A`); mensagem termina com `Co-Authored-By: Claude <Modelo> 5.5 <noreply@anthropic.com>` (o modelo que implementou). Sem push.

## Decisões do controlador (valem como requisito)

- D1: na lista de serviços do passo (b), o botão "Alterar unidade" é uma **linha extra no fim da lista** (mesmo padrão das linhas "⬅️ Outra unidade" do fluxo de agendamento).
- D2: no passo (b), se o novo médico **não atende** na unidade pré-selecionada (nenhum serviço ali), pula direto para a lista de unidades onde ele atende (depois, serviços na unidade escolhida).
- D3: no passo (a), a lista mostra todas as unidades do médico onde o serviço é feito, mesmo que seja uma só (é uma escolha explícita, não a pergunta Sim/Não). Nenhuma ⇒ aviso atual `EDIT_SERVICE_NOT_AT_UNIT` e mantém o serviço antigo.
- D4: a troca de unidade passa a aparecer no resumo: `changed()` inclui `"unidade"`; o resumo mostra a unidade (nome) e o endereço **da unidade**; sem unidade, o endereço da clínica formatado por `booking_details.clinic_address_line` (corrige o `.strip()` em dict).
- D5: a IA no "Alterar dados" pode propor unidade (campo novo `unit` em `propose_appointment_edit`); proposta inválida não é mais silenciosa: leva aos mesmos passos (a)/(b).
- D6: o contexto da IA ganha, por médico, as unidades reserváveis e os serviços feitos em cada uma, só quando a clínica tem unidades; `list_units` ganha `professional_name`.

## Ordem e paralelismo

U1 ‖ E1 ‖ U2 (U1 no worktree paralelo `secretarIA-par`, E1 no worktree principal, U2 no frontend) → A1 (no `secretarIA-par`, depois de U1) → integração + revisão final. Arquivos sem sobreposição entre tarefas paralelas; E1 pode acrescentar **uma** entrada em `services/llm_context.py::_STEP_LABELS` (A1 não mexe nesse dicionário).

---

### Task U1: Endereço obrigatório na API de unidades (secretarIA)

**Files:** Modify `src/secretaria/schemas/unit.py`, `src/secretaria/api/hub/units.py`; Test `tests/test_hub_units.py` (ajustar criações sem endereço e acrescentar testes) e qualquer outro teste que crie unidade **via HTTP** sem endereço (`grep -rn "/tenants/me/units" tests`).

- [ ] **Step 1: Testes que falham:** `test_create_unit_without_address_is_422_unit_address_required`, `test_create_unit_with_blank_address_is_422`, `test_patch_unit_address_to_blank_is_422_and_nothing_saved`, `test_patch_unit_name_only_on_legacy_unit_without_address_still_works` (**decisão:** PATCH que não envia `address` não exige endereço; PATCH que envia `address` vazio/null ⇒ 422), `test_create_unit_strips_address`.
- [ ] **Step 2–4:** FAIL → implementar (validação no handler ou no schema, devolvendo o 422 estruturado exato das Global Constraints) → PASS + `tests/test_hub_units.py tests/test_hub_professional_service_units.py tests/test_professional_units.py`; ruff.
- [ ] **Step 5:** Commit `feat(units): a unit always has an address (follow-up U1)`.

### Task U2: Endereço obrigatório no /contexto (Brain-Message-Frontend)

**Files:** Modify `lib/contexto/units.ts` (`validateUnitDraft` exige endereço; copy `addressRequired: "Informe o endereço da unidade."`; header do arquivo deixa de dizer que é opcional; `unitErrorMessage` mapeia o código `unit_address_required`), `components/contexto/UnitsSection.tsx` (unidade antiga sem endereço aparece com aviso visível `"Falta o endereço — edite a unidade para o paciente saber onde ir."` no lugar de "Sem endereço informado"; o rótulo do campo indica obrigatório), `components/contexto/ProfessionalPlaces.tsx` + `lib/contexto/professional-units.ts` (mesmo aviso no cartão "Unidades" em vez de `noAddress` neutro), `lib/mock/units.ts` (mock recusa criação/edição sem endereço com o mesmo 422); Test `lib/contexto/__tests__/units.test.ts`, `components/contexto/__tests__/units-section.test.tsx`, `components/contexto/__tests__/professional-places.test.tsx`, `lib/real/__tests__/secretaria-hub-units.test.ts`, `lib/contexto/__tests__/professional-units.test.ts` (ajustar criações com endereço vazio).

- [ ] **Step 1: Testes:** `validateUnitDraft refuses an empty address`, `create without address shows the plain error and makes no call`, `edit that clears the address is refused`, `server unit_address_required becomes the plain message`, `legacy unit without address shows the missing-address warning`, `professional card flags a linked unit without address`, `mock refuses a unit without address`.
- [ ] **Step 2–4:** FAIL → implementar → `npm test`, `npm run typecheck`, `npm run build` → PASS.
- [ ] **Step 5:** Commit `feat(contexto): a unit needs an address; missing ones are flagged (follow-up U2)`.

### Task E1: "Alterar dados" com escolha de unidade (secretarIA)

**Files:** Modify `src/secretaria/services/appointment_edit_flow.py`, `src/secretaria/services/appointment_edit.py` (textos, `changed()`, resumo), `src/secretaria/services/flow_router.py` (só a constante `STEP_EDIT_UNIT = "edit_unit"` junto das outras `STEP_EDIT_*` e o que for preciso para o passo ser roteado ao `edit_step`), `src/secretaria/services/flow_units.py` (helpers puros, se precisar: ex. `services_at_unit(professional, entries, unit_id)`, `units_doing_service(professional, service_id)`), `src/secretaria/ai/tools.py` (só `propose_appointment_edit`: parâmetro `unit: str = ""`), `src/secretaria/ai/graph.py` (só o bloco "EDIÇÃO DE CONSULTA EM ANDAMENTO": uma linha sobre propor unidade), `src/secretaria/services/llm_context.py` (só uma entrada em `_STEP_LABELS` para `edit_unit`); Test `tests/test_appointment_edit_units.py` (estender; fixtures `_doctor(..., units, service_units)` já existem), `tests/test_appointment_edit_ai.py` (proposta com `unit`).

**Comportamento:**
- (a) `_service_step` sem troca de médico: se o novo serviço não é feito na unidade atual (`flow_units.unit_refused`), abre `STEP_EDIT_UNIT` com as unidades reserváveis do médico onde o serviço é feito (D3), texto `EDIT_UNIT_FOR_SERVICE_BODY`; o serviço escolhido fica no `stage` (`pending_service`), e só é gravado no draft ao escolher a unidade; escolhida a unidade ⇒ `with_changes(service, end_at, unit_id)` ⇒ `_after_slot_affecting_change` (a unidade muda o horário possível). Zero unidades ⇒ `_refuse(..., EDIT_SERVICE_NOT_AT_UNIT)` como hoje.
- (b) `_doctor_step`: se o novo médico faz o serviço atual na unidade pré-selecionada (`draft.current.unit_id`) ⇒ como hoje (sem mudança de unidade). Senão: lista de serviços que ele oferece **nessa** unidade (`STEP_EDIT_SERVICE`, `stage.at_unit = <unit_id>`, `stage.after_doctor = True`, texto `EDIT_SERVICES_AT_UNIT_BODY`), com a linha "Alterar unidade" (D1) **somente** se o médico faz o serviço pré-selecionado em outra unidade reservável; se ele não atende na unidade pré-selecionada ⇒ D2 (lista de unidades). Tocar "Alterar unidade" ⇒ `STEP_EDIT_UNIT` com as unidades reserváveis do médico (texto `EDIT_DOCTOR_UNITS_BODY`, `stage.unit_purpose = "doctor"`) ⇒ escolhida a unidade ⇒ lista de serviços do médico nessa unidade (sem nova linha "Alterar unidade" se não houver outra unidade) ⇒ escolhido o serviço ⇒ `with_changes(professional_id, service, end_at, unit_id)` ⇒ `_after_slot_affecting_change`. Remover o desfazer silencioso (`_undo_doctor_change`) onde estes passos o substituem; o aviso `EDIT_DOCTOR_SERVICE_NO_UNIT` fica só para quando o médico não tem nenhuma unidade reservável.
- Sem unidades (clínica sem unidades ou consulta sem `unit_id`) ⇒ fluxo de hoje.
- Texto livre nos passos novos: uma volta do LLM (`delegate_llm`), como os outros passos; nome de unidade digitado casa com `flow_units.match_unit`.
- Remarcar (mesmo médico e serviço) continua na unidade original (§9.4 do spec).
- D4 (resumo com unidade e endereço certo; `changed()` com "unidade"; corrige `.strip()` em `tenant.address` dict nas duas chamadas).
- D5: `apply_ai_edit` — proposta de serviço/médico que cairia em unidade inválida abre (a)/(b) em vez de `unchanged()`; campo `unit` (nome) validado contra as unidades reserváveis do médico onde o serviço é feito: válido ⇒ grava `unit_id` e segue para o horário; inválido ⇒ abre a lista do passo (a) para o serviço.

- [ ] **Step 1: Testes:** `test_service_not_at_current_unit_opens_unit_list_of_that_service`, `test_picking_unit_applies_service_and_unit_and_rechecks_slot`, `test_service_done_nowhere_keeps_old_service_with_notice`, `test_new_doctor_without_service_at_unit_lists_his_services_there`, `test_change_unit_row_only_when_doctor_does_service_elsewhere`, `test_change_unit_lists_doctor_units_then_services_at_chosen_unit`, `test_new_doctor_not_at_preselected_unit_goes_to_unit_list`, `test_new_doctor_doing_service_at_unit_keeps_unit_as_today`, `test_reschedule_keeps_original_unit`, `test_clinic_without_units_edit_flow_unchanged`, `test_summary_lists_unit_change_and_unit_address`, `test_summary_with_clinic_address_dict_does_not_crash`, `test_ai_proposal_service_not_at_unit_opens_unit_list`, `test_ai_proposal_with_valid_unit_sets_unit`, `test_ai_proposal_with_invalid_unit_opens_unit_list`, `test_free_text_on_unit_step_delegates_once`.
- [ ] **Step 2–4:** FAIL → implementar → PASS + todos os `tests/test_appointment_edit*.py tests/test_edit_day_picker.py tests/test_free_text_edit_entry.py tests/test_flow_router_units.py tests/test_flow_units_service.py`; ruff.
- [ ] **Step 5:** Commit(s) `feat(edit): change unit inside "Alterar dados" when the service or doctor needs it (follow-up E1)`.

### Task A1: A IA conhece médico × serviço × unidade (secretarIA)

**Files:** Modify `src/secretaria/services/llm_context.py` (`_topology_lines` e o bloco de edição; **não** o dicionário `_STEP_LABELS`), `src/secretaria/plugins/multi_unit.py` (`list_units(service_name="", professional_name="")`), `src/secretaria/ai/prompts_v2.py` e `src/secretaria/ai/prompts.py` (só a descrição de `list_units` e uma linha de orientação sobre combinações), `src/secretaria/workers/shared/llm_context.py` (`appointment_context_text` nomeia a unidade da consulta quando o dado já está disponível sem consulta nova); Test `tests/test_llm_context*.py` (ou o arquivo de testes de `build_conversation_state`), `tests/test_unit_booking_ai_tools.py`.

**Comportamento:**
- Com unidades: cada médico aparece com suas unidades reserváveis e, em cada uma, os serviços feitos ali, ex.: `  - Dra. Ana (Oftalmologia): Unidade Sede (Rua das Flores, 100): Consulta, Retorno | Unidade Centro (Av. Brasil, 50): Retorno`. Serviço sem `service_id` ou sem marcas aparece em todas as unidades do médico. Um médico só: mesma informação numa linha. Sem unidades: texto atual byte a byte.
- Uma linha de orientação no mesmo bloco (só com unidades): `- Ofereça só as combinações acima; se o paciente quiser outra unidade, diga em quais o serviço é feito com esse médico ou quais médicos fazem o serviço lá.`
- No modo edição (draft com `unit_id`): `- Unidade atual da consulta: {nome}`.
- `list_units(professional_name=...)` resolve o médico pelo nome (só da clínica) e devolve as unidades dele (filtradas pelo serviço se `service_name`); nome desconhecido ⇒ erro claro com os nomes válidos; sem `professional_name` ⇒ comportamento atual.
- Tamanho: o bloco não pode crescer sem limite — no máximo as unidades/serviços já existentes no snapshot (sem consulta nova).

- [ ] **Step 1: Testes:** `test_topology_lists_units_and_services_per_doctor`, `test_topology_service_without_marks_in_every_unit`, `test_topology_without_units_is_unchanged`, `test_single_doctor_topology_with_units`, `test_edit_state_names_current_unit`, `test_list_units_by_professional_name`, `test_list_units_unknown_professional_is_clear_error`, `test_appointment_context_names_unit`.
- [ ] **Step 2–4:** FAIL → implementar → PASS + `tests/test_ai_toolset_v2.py tests/test_unit_booking_ai_tools.py` e os testes de prompt existentes (`grep -l "secretary_system_prompt\|build_conversation_state" tests`); ruff.
- [ ] **Step 5:** Commit `feat(ai): the assistant knows which services each doctor does at each unit (follow-up A1)`.
