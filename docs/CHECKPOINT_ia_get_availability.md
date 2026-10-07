# CHECKPOINT — IA entra em qualquer etapa: `get_availability`, ferramentas de agenda cegas e o conjunto v2 (TASK-030 P4)

**Estado:** BUILT — alterações locais **sem commit** no worktree `C:/TECH/BRAIN-worktrees/TASK-030/secretarIA`, branch `task/TASK-030-ia-entra-em-qualquer-etapa`, base `4f852a4`; **não integrado à main, não pushado, não deployado**. A correção do `cancel_event` legado vale para TODAS as clínicas no deploy; o resto fica atrás do interruptor `initial_flows.ai_draft_v2` (desligado por padrão).
**Spec:** `docs/superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md` (§2 decisão 4 e §4.6 emendadas em 2026-10-03, §5 critérios 5, 6 e 8, §4.11, §7).
**Plano:** `docs/superpowers/plans/2026-10-02-ia-p4-disponibilidade-so-horarios.md`.

## 1. O que muda

- **Todas as clínicas (correção de segurança):** o `cancel_event` legado só cancela evento que é de um `Appointment` do paciente da conversa, no tenant do turno (a mesma prova do `do_paciente`). Antes, apagava qualquer id que o modelo mandasse. Recusa → erro que manda usar `manage_existing_appointment`, log `agent_tool_blocked(reason=not_patients_event)`. Evento criado pela equipe direto no Google, sem `Appointment` do paciente, não é mais cancelável pela IA.
- **Interruptor desligado:** fora a correção acima, nada. O conjunto de ferramentas é exatamente o de antes (fixado por testes, nome por nome).
- **Interruptor ligado (decisão do dono de 2026-10-03: a IA nunca vê nome nem dado de outro paciente — nem pseudonimizado):**
  - saem `check_availability`, `list_free_slots`, `list_free_slots_for_professional` (leem ocupado/horários crus), `create_event_for_professional` (dobrada no `professional` do `create_event` cego) e `create_event_at_unit` (unidade não é campo do rascunho nem do toque);
  - entra `get_availability`, que devolve só janelas livres;
  - `create_event` e `cancel_event` **continuam com o mesmo nome, mas cegos** (`ai/staging_tools.py`): nunca escrevem na agenda nem leem evento; `create_event(start, service, professional)` entrega ao fluxo o mesmo rascunho do `set_booking_draft` (dia e horário de `start`; "pra quem" e convênio vêm do que a conversa já tem, ou o fluxo pergunta) e o paciente cai nos detalhes + cartão de confirmação; `cancel_event(appointment)` só aceita a "(ref …)" de uma consulta DO PRÓPRIO paciente e leva ao cartão "Confirmar o cancelamento?". **Quem marca e quem cancela é o toque do paciente**, pelo caminho de hoje (portão de código do Portal, reservas, autorização);
  - toda ferramenta v2 só devolve as chaves que declarou (`ai/tool_output.py`); o resto cai antes do modelo. `get_service_info` (adicionado após a escrita do plano) continua disponível com seus sete campos declarados: `servico`, `duracao_min`, `preco`, `descricao`, `descricao_completa`, `orientacoes`, `error`. Erros de entrada desconhecida nele, em `start_guided_booking` e em `select_professional_and_continue` não repetem o argumento no v2; os textos do v1 permanecem.

## 2. Onde está cada peça (âncoras)

- `ai/tools.py` — `cancel_event` (checagem de dono, `TOOL_BLOCK_NOT_PATIENTS_EVENT`); `AI_TOOLSET_V2_WITHHELD` (5), `AI_TOOLSET_V2_STAGING` (`create_event`, `cancel_event`), `BLIND_STAGING_VARIANT`, `_ai_toolset_v2_ctx`, `_blocked_by_toolset_v2` (a trava de dentro de cada ferramenta retirada ou legada); as de `plugins/multi_professional.py` e `plugins/multi_unit.py` chamam a mesma trava.
- `services/availability_windows.py` — `resolve_range` (valida e limita os dias), `FreeWindow`, `slots_to_windows`, `scan_free_windows` (só usa `services/availability.py`, a definição única de "livre").
- `services/calendar.py::CalendarService.default_slot_minutes` — propriedade de leitura.
- `ai/availability_tool.py::get_availability` — a leitura; `_clinic_today` é a costura do relógio.
- `ai/staging_tools.py` — `create_event_v2`, `cancel_event_v2` (nomes `create_event`/`cancel_event`, variante `blind_v2`); `create_event_v2` carrega também `professional_unresolved` para preservar a proteção de captura recente quando o médico informado não é reconhecido.
- `ai/tool_output.py` — `V2_TOOL_OUTPUTS` (a lista de saída por ferramenta), `allowlisted`, `undeclared_keys`, `wrap_tools_with_output_allowlist`.
- `ai/graph.py` — `base_tools_for(toolset_v2=)`, `_kept_on_v2`, `effective_tools` (a montagem única, também usada no log `agent_capabilities_resolved`), `build_agent(toolset_v2=)` (lista de saída por dentro do guarda de pseudonimização; chave do cache com `#toolset_v2`), `run_agent(toolset_v2=)`.
- `workers/shared/llm_context.py` — `_ai_toolset_v2(tenant)` (clínica com fluxo E `ai_draft_v2`) e, em `_flow_handback_tools`, `get_availability` + as duas cegas; `workers/orchestrator.py` passa o interruptor a `run_agent`.

## 3. O que a IA recebe

`get_availability`:

    {"windows": [{"day": "2026-10-06", "start": "08:00", "end": "09:30"}],
     "timezone": "America/Sao_Paulo", "slot_minutes": 30,
     "day_from": "2026-10-05", "day_to": "2026-10-18", "professional": "Dra. Ana"}

Só essas chaves, mais `clamped` (lista: `max_days`, `booking_window`, `max_windows`) e `note` (frase) quando há corte ou lista vazia. `create_event`/`cancel_event` cegos: nada no sucesso (a devolução ao fluxo encerra o turno), `{"error": "<frase>"}` na recusa. Toda ferramenta v2: só as chaves de `V2_TOOL_OUTPUTS`; nunca título, participante, id, descrição, local, link nem bloco ocupado; nunca nome, telefone ou e-mail de outro paciente; nada de outro tenant. As frases de erro nunca repetem o que o modelo mandou.

## 4. As paredes da cegueira

Defesa principal (estrutural):
1. **As ferramentas não leem o dado:** `get_availability` só calcula janelas; as cegas não leem evento; `list_patient_appointments` só lê as linhas do próprio paciente e não devolve id; a "(ref …)" é só o horário.
2. **A lista de saída declarada** (`ai/tool_output.py`): chave não declarada cai antes do modelo; ferramenta sem declaração responde erro.

Segunda parede:
3. **O guarda de pseudonimização** (`ai/pii.py`, por fora da lista): mascara padrões e o que já está no mapa da conversa. **Não é a defesa principal** — o próprio docstring diz que ele não mascara nome de terceiro desconhecido.
Prova: `tests/test_ai_v2_blindness.py` roda um turno v2 inteiro sobre iscas (outro paciente, o atendido do próprio paciente, outra clínica, eventos do Google com strings únicas em todos os campos) e falha se qualquer isca — ou qualquer token `[PACIENTE_…]` — aparecer no prompt, no histórico ou num resultado.

## 5. Limites e falhas

- No máximo **14 dias** e **30 janelas** por chamada (as mais cedo primeiro) e nunca além do **horizonte de agendamento** (20 dias, `flow_router.DAY_PICKER_WINDOW_DAYS`). Só um `day_to` explícito que passe do limite conta como corte.
- Recusas de `get_availability` (erro curto): data em formato errado, dia que já passou (no "hoje" **da clínica**), `day_to` antes de `day_from`, primeiro dia além do horizonte, profissional desconhecido/duplicado/ausente numa clínica com vários, serviço inexistente.
- Recusas das cegas: `start` fora de `AAAA-MM-DDTHH:MM` (offset, segundos ≠ 00, data/hora impossível); referência que não é de consulta futura do paciente (de outro paciente, de outro tenant, de ninguém), duas consultas no mesmo minuto, id de evento ou texto livre no lugar da referência, conversa sem paciente.
- **Queda do calendário** (`CalendarUnavailableError`, token revogado incluído) **propaga** como em toda leitura de calendário de hoje: `run_agent` devolve o sentinel, o trabalhador entrega a conversa a uma pessoa e alerta o dono. **Qualquer outra falha** de `get_availability` vira `{"error": ...}` e `agent_availability_failed` (só o tipo); banco fora no `cancel_event` cego vira erro que proíbe dizer "cancelado" + `agent_cancel_staging_failed`.
- Custo no Google: uma leitura de intervalo mais uma por dia com horário, em sequência, até 14 dias. Dias tocados parcialmente por reservas também são lidos na filtragem inicial: podem ser até 29 leituras no total (uma de intervalo + duas por dia). Otimização adiada (§11). As cegas não leem o Google.

## 6. Observabilidade

`ai_availability_read` (INFO, uma por leitura): `tenant_id`, `conversation_id`, `windows`, `days_scanned`, `clamped`, `professional_resolved` — só contagens. `agent_tool_blocked` com `tool` e `reason`: de `get_availability` {`no_clinic`, `tenant_mismatch`, `no_calendar`, `no_professionals`, `professional_required`, `professional_unknown`, `professional_ambiguous`, `service_unknown`, `no_professional_offers_service`, `several_professionals_offer_service`, `bad_day_format`, `past_day`, `reversed_range`, `beyond_window`}; das cegas {`no_clinic`, `bad_start`, `bad_appointment`, `no_patient`, `unknown_appointment`, `ambiguous_appointment`}; do `cancel_event` legado `not_patients_event`; `toolset_v2` quando uma ferramenta retirada ou legada é chamada num turno v2 (deve ser zero). `agent_tool_output_dropped` (`tool`, `keys` — só nomes; deve ser zero) e `agent_tool_output_undeclared` (`tool`; deve ser zero). `agent_availability_failed`, `agent_cancel_staging_failed` (`error_type`). `agent_capabilities_resolved` ganhou `toolset_v2`. Um `create_event` cego aparece em `conversation_handback_entered` (P1) com o `source_tool` do rascunho, e um `cancel_event` cego com o do gerenciar — os pousos são esses caminhos; qual nome o modelo chamou fica em `llm_turn_trace.tools_called`.

## 7. Decisões e limites conhecidos

- Sem `service`, a duração é a padrão da agenda (`slot_minutes` na resposta).
- Janela não é grade: o fluxo só aceita horários na grade (início da janela + múltiplos de `slot_minutes`); um horário fora dela cai na lista de horários do dia (critério 3 do spec), sem erro — vale igual para o `create_event` cego.
- O seletor de DIAS do fluxo continua sem descontar reservas (P2a); a IA desconta. Um dia sem janela para a IA pode ainda aparecer no seletor.
- `create_event` cego não leva convênio nem "pra quem": se o paciente disse, o prompt manda usar `set_booking_draft` (P5); se não, o fluxo pergunta.
- `cancel_event` cego recusa referência que não casa (o modelo corrige no mesmo turno); o `manage_existing_appointment` v2 (P3) com a mesma referência cairia na lista de botões.
- Retiradas sem variante: `list_free_slots_for_professional` (sem desconto de reservas), `create_event_for_professional` (dobrada no `create_event` cego), `create_event_at_unit` (unidade não é do rascunho). Consequências: a IA perde o `professional_context` que o leitor antigo anexava (o bloco "SOBRE O PROFISSIONAL" do prompt continua) e, com o addon `multi_unit`, nenhum agendamento pela IA registra a unidade (o de botões também não).
- Reservas: o dono é `resolve_booking_owner_id` (a regra única dos agendamentos); a reserva da própria conversa não esconde o horário dela.
- O refresh token do Google continua sendo decriptado só em `services/tenant_config.py`; `get_availability` nunca usa o calendário de ambiente.

## 8. Para o P5

Nomes e texto exatos (os docstrings são o `description` que o modelo lê) estão no plano, seção "Para o P5". O prompt v1 cita `check_availability`, `list_free_slots`, `create_event` (com `summary`/`end`) e `cancel_event(event_id)` e o `patient_calendar_link`; num turno v2 as leitoras não existem e `create_event`/`cancel_event` são as cegas, que só preparam o cartão — o prompt v2 precisa dizer isso e nunca afirmar que algo foi marcado/cancelado. Quando o P5 retirar `start_guided_booking` e `select_professional_and_continue`, ele acrescenta os nomes em `graph._kept_on_v2` e atualiza os conjuntos exatos de `tests/test_ai_toolset_v2.py`; `V2_TOOL_OUTPUTS` pode manter as duas entradas (inofensivo) até o v1 sair.

## 9. Deploy (quando o dono pedir)

Sem migração. `secretaria_api` **e** `secretaria-worker` juntos; `GET /build` com paridade `match`. **No deploy, a correção do `cancel_event` legado passa a valer em todas as clínicas** (só recusa evento que não é do paciente). O resto não muda nada até um operador ligar a chave `ai_draft_v2` (CHECKPOINT `ia_rascunho_v2_resolvedor` §3) — e **só depois de P3 e P5 no ar**. Rollback: desligar a chave (vale no turno seguinte) ou voltar o código nos dois serviços (isso também desfaz a correção do `cancel_event`).


## 10. WhatsApp e Portal: implementação compartilhada e prova local

`get_availability`, as ferramentas cegas e o filtro de saída não dependem do canal. `workers/orchestrator.py` entrega o mesmo interruptor/conjunto ao agente nos dois canais; os sentinels seguem `workers/shared/sentinels.py` e o resolvedor/fluxo comum. Nenhuma duplicação em `workers/portal/` ou `workers/whatsapp/`. O `ChannelSender` existente entrega os cartões; `_handle_confirmation`, `_manage_cancel` e `_manage_reschedule` permanecem byte/AST iguais ao baseline. Os portões existentes (Portal/código, reservas e consentimento) continuam nos caminhos atuais.

`tests/test_ai_v2_blindness.py::turn` roda as ferramentas e verifica prompt/histórico/resultados em contextos `whatsapp` e `brain_message`. `test_retained_handback_errors_omit_unrecognized_input_only_on_v2` prova as duas correções da revisão e mantém os erros v1. `tests/test_express_confirmation_worker.py` e `tests/test_manage_v2_handback.py` cobrem os caminhos de cartões/confirmar existentes dos dois canais. Isso prova reaproveitamento e comportamento local; não é teste de envio real por Meta/Portal/Google nem avaliação de modelo real.

## 11. Validação, revisão e limites da prova

Baseline anterior ao P4: **3709 passed, 22 skipped, 15 warnings in 399.91s (0:06:39)**.
Final após a revisão e quatro regressões adicionais: **3937 passed, 22 skipped, 15 warnings in 455.78s (0:07:35)** (baseline +228 testes).

Ruff nos 23 arquivos Python novos/modificados e `git diff --check`: limpos. Testes RED→GREEN por tarefa; os erros de entradas desconhecidas das duas ferramentas mantidas foram corrigidos numa única passada após a revisão independente. Verificação de camadas e das funções protegidas, sem mudança de modelo ou migração. Registro completo e decisões em `C:/TECH/BRAIN/tasks/TASK-030/results/p4-completion-codex.md`; revisão original em `results/p4-review-codex.md` (CHANGES_REQUIRED na leitura; achado Important corrigido depois com regressão observada, sem segunda revisão).

Uma pendência Minor: dias tocados parcialmente por reservas são lidos duas vezes pelo scanner (correto, porém mais caro). O custo pode chegar a uma leitura de intervalo + duas por dia parcial, até 29 leituras em 14 dias; a seção 5 registra também essas releituras. Otimização adiada.

A prova com iscas cobre entradas inválidas e saídas de todas as ferramentas do conjunto v2 deste baseline, com histórico controlado. Não certifica limpeza retroativa de históricos v1, texto livre arbitrário cadastrado pela clínica, nem todos os logs antigos. Casos noturnos em hora inexistente/repetida de DST e contextos internos deliberadamente inconsistentes não ganham certificação nova. O P5 continua necessário antes de ativar; nenhuma configuração foi alterada.

Graphify atualizado por AST e diagnosticado sem duplicatas/dangling edges; mantém `STALE` formal por código sem commit, conforme política de freshness.
