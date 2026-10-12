# CHECKPOINT — IA entra em qualquer etapa: rascunho v2, `flow_draft` e o resolvedor (TASK-030 P2)

**Estado:** P2a publicado e migrado; código P2b + P3 Tasks 1–5 commitado em `6faf720` e integrado na `main` em 2026-10-06. Novo deploy pelo dono pendente; validação registrada abaixo.
**Spec:** `docs/superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md` (§4.1–4.3, §4.7, §4.11, §7).
**Planos:** `docs/superpowers/plans/2026-10-02-ia-p2a-rascunho-coluna-e-resolvedor.md`, `docs/superpowers/plans/2026-10-02-ia-p2b-handbacks-ferramenta-e-estado.md`.

> As seções 1–6 registram o marco P2 antes do P3. Para o comportamento consolidado de confirmação e retomada, consulte `docs/CHECKPOINT_ia_confirmacao_expressa.md`.

## 1. O que muda para o paciente

- **Em toda clínica (fora do interruptor):** quando a IA devolve um agendamento ao fluxo e ninguém respondeu
  "Essa consulta é pra você?", o fluxo pergunta — inclusive em `start_guided_booking` e
  `select_professional_and_continue`. O que a IA já sabia espera em `flow_draft` e é retomado depois da
  resposta (30 min de validade). Custo: um toque a mais para quem só disse "quero cirurgia de catarata".
- **Em toda clínica:** o convênio digitado ("Outro convênio") não some mais num hand-back; um médico sem
  serviço/horário configurado dispara o alerta da clínica em vez do menu; numa clínica de um só médico, a
  lista de horários esconde o horário que outra pessoa está segurando.
- **Só com o interruptor ligado:** a IA preenche serviço, médico, convênio, pra quem, dia e horário; o
  resolvedor confere tudo e leva o paciente até a lista de horários do dia pedido. Item inválido cai sozinho.

## 2. Onde está cada peça (âncoras)

- `services/availability.py` — `free_slots_for_day`, `days_with_free_slots`, `available_day_starts`,
  `without_holds` (definição única de "livre"; os seletores leem por aqui).
- `services/booking_draft.py` — `BookingDraft` (sentinel `t/p/i/w/d/h`), `draft_record`/`draft_from_record`,
  `resolve_booking_draft`, `DraftResolution`, `landing_step`, códigos `DROP_*`/`FALLBACK_*`,
  `_express_confirmation` (gancho do P3, devolve `None`).
- `workers/shared/draft_resolution.py` — `_load_draft_context` (leituras frescas, escopo do tenant),
  `_turn_booking_gate`, `_draft_calendar_source`, `_resolve_draft`, `_resume_booking_draft` (continuação).
- `Conversation.flow_draft` (JSON NULL) — migração `e7d3c1a9b5f2`; levado só nas etapas de pra quem
  (`flow_router._carry_draft`); apagado ao consumir, no menu, pelos pisos de silêncio e pelo "Não".
- `flow_router` — `_hold_owner`, `booking_gate_scope`, `FLOW_DRAFT_TTL_MINUTES`, `ai_draft_v2_enabled`,
  `FlowRouterResult.flow_draft/resume_draft`, `_ask_day(prefix=)`.
- `workers/shared/sentinels.py` — `_handle_set_booking_draft`, `_handle_select_professional`,
  `_handle_start_guided_booking`, `_apply_draft_resolution`, `_legacy_booking_draft`, `_pra_quem_unknown`.
- `ai/tools.py::set_booking_draft_v2` (nome `set_booking_draft` para o modelo) e
  `ai/graph.py::_tool_cache_key`.
- `services/llm_context.py::build_conversation_state` — rótulos de todas as etapas, sem marcador interno.

## 3. Interruptor por clínica

`Tenant.initial_flows["ai_draft_v2"]`, literal JSON `true`; desligado por padrão. A ativação é uma mudança de configuração por clínica, reservada ao operador. Nenhuma configuração remota foi alterada nesta correção.

Ligar só depois de P3 (confirmação expressa), P4 (`get_availability`) e P5 (prompt novo).

## 4. Observabilidade

`conversation_handback_entered` (P1) em todo hand-back; códigos novos: `not_offered_by_professional`,
`out_of_window`, `day_unavailable`, `no_free_slot`, `missing_day`, fallback `no_free_days`.
Continuação: `booking_draft_resumed` / `booking_draft_resume_skipped` (`missing`, `expired_or_invalid`) /
`booking_draft_resume_failed`. Ferramenta v2: `booking_draft_tool_item_dropped`, `agent_tool_blocked`
(`bad_for_whom`, `bad_day`, `bad_time`). Só nomes de campo e códigos — nunca valores.

## 5. Decisões e limites conhecidos

- `flow_draft` espera só o "pra quem" (spec §4.3); se o resolvedor pousar na pergunta de convênio, médico ou
  serviço, o dia/horário do rascunho não são retomados depois da resposta (P3 pode estender `resume_draft`).
- O seletor de dias consulta disponibilidade do calendário sem descontar reservas, conforme o contrato atual de `enter_day_picker`; a etapa de horários e os leitores da IA descontam reservas.
- Ferramenta v2 só recusa FORMATO; existência na clínica é do resolvedor.

## 6. Pendências

P3: `_express_confirmation` (detalhes + cartão) e gerenciar até a confirmação. P4: `get_availability`
sobre `services/availability.py`. P5: prompt novo (a docstring da v2 é provisória) e remoção de
`start_guided_booking`/`select_professional_and_continue`.

## 7. Deploy (quando o dono pedir)

Migração `e7d3c1a9b5f2` primeiro (one-off da imagem nova), depois `secretaria_api` **e**
`secretaria-worker` juntos; `GET /build` com paridade `match`. Rollback: código velho nos dois, depois
`alembic downgrade b8d3f1a6c2e5`.

## 8. Validação local desta correção

Validação final conjunta P2b/P3 e fuso: **3442 passed, 10 skipped, 15 warnings in 431.43s (0:07:11)**. Os testes focados e as fases RED→GREEN constam no ledger da execução. Nenhuma consulta de produção foi criada, alterada ou cancelada durante a correção. O teste real anterior permanece em 16/10/2026 às 14:40 (America/Sao_Paulo).

> TASK-030 P3: Tasks 1–9 concluídas, incluindo detalhes/cartão, retomada de qualquer pergunta e gerenciamento v2; validação, revisão, limites e estado local/publicado em `docs/CHECKPOINT_ia_confirmacao_expressa.md`.


> TASK-030 P4: a IA v2 lê horários só por `get_availability` (janelas livres); `create_event`/`cancel_event` viram ferramentas cegas que só levantam este mesmo rascunho / o pedido de gerenciar (o toque do paciente é que marca/cancela) — ver `docs/CHECKPOINT_ia_get_availability.md`.

> TASK-030 P5: prompt v2 alinhado ao rascunho e ao atendimento atual; validado localmente, sem ativacao ? ver `docs/CHECKPOINT_ia_prompt_v2.md`.
