# CHECKPOINT — Agendar para outra pessoa ("Essa consulta é pra você?")

**Origem**: `z_prompts/PROMPT_AGENDAR_PARA_TERCEIRO_SECRETARIA.md` (TASK-007, 2026-09-25).
**Estado**: BUILT, **não commitado / não deployado** — worktree
`C:\TECH\BRAIN-worktrees\TASK-007\secretarIA`, branch `task/TASK-007-secretaria` (base `8509267`).
**Deploy**: migração `b8e3c5a1d702` PRIMEIRO; depois `secretaria-worker` **e** `secretaria_api`
(toca `workers/` e `services/flow_router.py` — regra do `CLAUDE.md`). NOT AUTHORIZED.

## O que o paciente vê

Toda entrada de agendamento (`LABEL_BOOK` "Agendar"; no menu multi-profissional
"Escolher médico"/"Escolher serviço"; no menu de profissional único o índice 0 "Serviços e
Custo", única porta de agendamento ali e onde `show_main_menu` deixa o paciente) pergunta
primeiro **"Essa consulta é pra você? 😊"**:

- **✅ Sim, é pra mim** → exatamente a lista que a entrada abria antes (`_start_booking` /
  `_enter_clinic_service_catalog`). Nada mais muda nesse caminho.
- **👤 Pra outra pessoa** → pede o nome → mostra
  "Ao informar os dados de *{nome}*, você confirma que tem autorização para compartilhar essas
  informações com a clínica." com **✅ Confirmar** / **⬅️ Cancelar**. Confirmar grava
  `ConsentEvent(kind="third_party_booking_authorized", legal_basis="TODO_LAWYER: …")` e segue
  para a mesma lista do "Sim". Cancelar volta à pergunta pra-quem (nome descartado).

Se a entrada daria num beco sem saída (sem serviços, sem profissional), a pergunta é pulada e a
resposta de beco sai direto (`_ask_attendee_first`).

**Desvio consciente do prompt**: o botão ficou "👤 Pra outra pessoa" (18 caracteres), não
"👤 Não, é pra outra pessoa" — 25 caracteres estouram o limite de 20 do WhatsApp e o toque
voltaria truncado. Teste fixa os 4 rótulos ≤ 20.

## Onde o nome vive

| Onde | Coluna | Quando |
|---|---|---|
| Reserva em andamento | `conversations.flow_attendee_name` | do Confirmar até a reserva terminar |
| Reserva do Portal presa ao código | `booking_holds.attendee_name` | hold → promoção |
| Consulta | `appointments.attendee_name` | para sempre; NULL = o próprio paciente |

Mesmo caminho do convênio (`flow_selected_insurance` → `booking_holds.insurance` →
`appointments.insurance`). **Diferença deliberada**: o nome NÃO é carregado à mão em cada
`FlowRouterResult` — `flow_router._carry_attendee` roda uma vez em `route()` e
`resume_bubbles()` (e em `enter_guided_booking`) e copia o nome para todo resultado que fica em
`SERVICE_CATALOG`/`LLM`. Sai do fluxo (menu, gerenciar, IDLE pós-reserva, hold) → some.

Enquanto nos passos pra-quem, `flow_selected_type` guarda QUAL entrada abriu o agendamento
(`ATTENDEE_NEXT_BOOK` / `ATTENDEE_NEXT_CATALOG`); é sobrescrito pela lista seguinte.

## Quem usa o nome

- Título do evento Google: `"{serviço} - {atendido}"` (`_handle_confirmation`,
  `_promote_booking_hold`, e `ai/tools.py::create_event` quando a conversa já tem atendido
  autorizado).
- Recap e confirmação: linha `Paciente: {atendido}` (só quando há atendido — caminho "Sim"
  idêntico byte a byte).
- Lembrete (`plugins/reminders.py::_render_reminder_text`): "Lembrete: {atendido} tem …".
- E-mail ao profissional: `"{atendido} (agendado por {conta})"`.
- Handoff PreCheck: `patient_name = attendee_name or patient.name` (o questionário é sobre quem
  será atendido). Mesmo campo já aceito pelo contrato — sem mudança de schema cross-repo.
- Pix/Asaas continua no nome da conta (quem paga).

## PII — por que nenhum código novo de mascaramento

`services/pii_pseudonymization.py::load_pseudonymizer` registra, ao lado de `PACIENTE`, o
identificador `ATENDIDO` para o `flow_attendee_name` da conversa **e** todo
`attendee_name` de `appointments`/`booking_holds` da conversa — o nome continua no histórico (a
frase de autorização, a confirmação) depois que o campo de fluxo é limpo. A resposta digitada (e
qualquer resposta RECUSADA) vira `ATTENDEE_NAME_LLM_PLACEHOLDER` em `ai/graph.py::_load_history`
(`is_attendee_name_question`), mesma regra 6 da skill `pii-field-capture-pseudonymization`.
Logs só levam booleanos (`attendee_name_answered parsed=…`, `attendee_choice choice=self|other`).
Limite conhecido herdado: nome < 3 caracteres não é registrado (`MIN_TERM_LEN`).

## LLM

`ai/prompts.py`: "CONSULTA PARA OUTRA PESSOA" → nunca agenda pelo chat, chama `show_main_menu`.
Consentimento nunca passa pela LLM. `enter_guided_booking` herda um atendido já autorizado se o
paciente derivou para a LLM no meio da reserva.

## Tempo

`workers/tasks.py::_expire_stale_attendee_step`: mesmos `llm_state_ttl_minutes` do piso de
LLM, sem depender de config do tenant. Necessário porque o passo do nome aceita texto livre
("Maria bom dia" dias depois não pode virar nome de terceiro).

## Validação (2026-09-25)

- Baseline no HEAD: 3 falhas pré-existentes (2 × `test_action_buttons` sem credencial Google
  local; `test_human_backup_plugin` flake UTC) / 2415 ok; ruff 8 erros pré-existentes.
- Depois: as MESMAS 3 falhas / 2428 ok; ruff os mesmos 8.
- `tests/test_attendee_booking.py` (13): caminho "Sim" idêntico; fluxo completo "outra pessoa"
  com recusa, Cancelar e Confirmar; evento/confirmação/recap/lembrete/e-mail com o atendido;
  `ConsentEvent` 1× e 0× no "Sim"; coluna na `Appointment`; piso de tempo; PII via pipeline real
  (inclusive após a reserva terminar). Bite-check: neutralizar cada mecanismo derruba seu teste.
- 12 testes antigos ajustados para tocar "Sim, é pra mim" antes de afirmar a mesma lista
  (`_route_past_attendee`).
- Migração em Postgres 18 descartável (initdb, porta 5544, apagado depois): upgrade → downgrade
  (0 colunas) → upgrade; gravação/leitura via models.

## Revisão independente (2026-09-25) — CHANGES_REQUIRED → corrigido

| # | Achado | Correção | Teste |
|---|---|---|---|
| 1 HIGH | nome ficava grudado em modo LLM e ia para a PRÓXIMA reserva pelo chat | `_persist_appointment` consome `flow_attendee_name`; `_expire_stale_llm_state` limpa | `test_chat_booking_consumes_the_attendee`, `test_llm_expiry_drops_the_attendee` |
| 2 HIGH | "Cancelar"/reserva abandonada: o card de autorização ia cru à LLM (nenhuma linha registrava o nome) | `remember_attendee_name` grava o nome no `ConversationPiiTokenMap` na captura; `load_pseudonymizer` re-registra todo token `[ATENDIDO_*` do mapa (o mapa restaurado só reaproveita tokens, NÃO recria regras) | `test_cancelled_authorization_still_masks_the_name` |
| 3 MED | hand-back `select_professional_and_continue` não passava por `route()` e perdia o nome | `_handle_select_professional` copia do snapshot | `test_llm_choose_doctor_hand_back_keeps_the_attendee` |
| 4 MED | remarcação após o médico cancelar perdia o atendido | handoff de rebooking carrega `appointment.attendee_name` | **sem teste dedicado** (caminho do botão de ação é pesado de montar) |
| 5 MED | depois do Confirmar, lista parada por semanas mantinha o nome | piso de tempo cobre todo SERVICE_CATALOG quando há atendido | `test_stale_booking_with_an_attendee_expires` |
| 6 LOW | entradas da LLM (`create_event`/`start_guided_booking`) não perguntam pra-quem | **decisão**: só o prompt desvia a LLM para `show_main_menu`; consentimento nunca pela LLM. Documentado, não codificado. | — |

Bite-check refeito para 1, 2, 3 e 5. Suíte final: mesmas 3 falhas do baseline / 2433 ok; ruff os
mesmos 8.

## Coordenação

`PROMPT_CONVENIO_CATALOGO_ACEITACAO_1_SECRETARIA.md` ainda não rodou; ele deve inserir o
convênio DEPOIS de `_attendee_step` (a pergunta pra-quem é o passo mais externo).
