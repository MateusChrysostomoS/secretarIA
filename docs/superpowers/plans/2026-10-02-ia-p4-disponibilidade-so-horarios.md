# IA entra em qualquer etapa — P4: `get_availability` (só horários livres), ferramentas de agenda cegas e o conjunto v2 — Implementation Plan

> **Revisão de 2026-10-03 (decisão do dono):** a IA **pode manter** `create_event` e `cancel_event`; a exigência dura é que ela **nunca** veja nome nem nenhum outro dado de paciente — nem pseudonimizado, nem de outra forma. Desenho escolhido: "ferramentas cegas que só PREPARAM o cartão". No v2, `create_event`/`cancel_event` continuam com o mesmo NOME, mas são variantes novas (`ai/staging_tools.py`) que nunca escrevem na agenda: entregam ao fluxo o mesmo pedido que `set_booking_draft` v2 (P2b) e `manage_existing_appointment` v2 (P3) entregam, e o toque do paciente é o que marca/cancela. Só as três LEITORAS de ocupado continuam retiradas (`check_availability`, `list_free_slots`, `list_free_slots_for_professional`), junto com as duas escritoras de plugin que não têm variante cega (`create_event_for_professional`, `create_event_at_unit`). Entraram: a correção de segurança do `cancel_event` legado (Task 1, vale para TODAS as clínicas), a lista de saída declarada por ferramenta (Task 8) e os testes com dados-isca (Task 9). O nome do arquivo deste plano não mudou.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Em TODAS as clínicas, o `cancel_event` legado só cancela evento do próprio paciente. Numa clínica com o interruptor `ai_draft_v2` ligado, a IA fica **cega para dados de agenda e de outros pacientes**: perde as ferramentas que leem horário ocupado (`check_availability`, `list_free_slots`, `list_free_slots_for_professional`) e as escritoras de plugin sem variante cega (`create_event_for_professional`, `create_event_at_unit`); ganha `get_availability`, que devolve só janelas livres `{day, start, end}` no fuso da clínica; e mantém `create_event`/`cancel_event` pelo NOME, mas como ferramentas cegas que só preparam o cartão de confirmação (quem marca/cancela é o toque do paciente). Toda ferramenta v2 só devolve as chaves que declarou, e testes com dados-isca provam que nenhum nome, telefone, e-mail, título, id de evento ou dado de outro tenant chega ao modelo. Com o interruptor desligado, o conjunto de ferramentas é exatamente o de hoje (só o `cancel_event` legado ganhou a checagem de dono).

**Architecture:** Seis peças. (1) **Correção do `cancel_event` legado** (`ai/tools.py`): antes de qualquer chamada ao Google, o id precisa ser de um `Appointment` deste paciente neste tenant — a MESMA prova do `do_paciente` (`_own_google_event_ids`). (2) `services/availability_windows.py`: puro e sem noção de "evento" — valida e limita o intervalo de dias, agrupa horários livres em janelas e varre a agenda **só** com `services/availability.py` (P2a). (3) `ai/availability_tool.py`: `get_availability` — resolve profissional e serviço como o rascunho resolve, obtém a agenda pela resolução por tenant/profissional do fluxo, desconta reservas e devolve só janelas. (4) `ai/staging_tools.py`: `create_event`/`cancel_event` v2 (variantes `cache_variant = "blind_v2"`, como o P2b/P3 fazem com `set_booking_draft`/`manage_existing_appointment`) — levantam `BookingDraftRequested` (P2b) e `ManageAppointmentRequested("cancel", appointment=ref)` (P3), o mesmo caminho sentinel → resolvedor → cartão. (5) A montagem, com as duas travas do repositório (estilo `_blocked_tenant_level`): `graph.effective_tools` não entrega as retiradas nem a versão LEGADA de um nome de preparo (trava 1) e cada uma delas recusa sozinha num turno v2 (trava 2); `_flow_handback_tools` entrega `get_availability` e as duas cegas; o trabalhador decide o interruptor uma vez por turno e o passa a `run_agent(toolset_v2=...)`. (6) `ai/tool_output.py`: a lista de saída declarada por ferramenta v2, aplicada por `build_agent` por DENTRO do guarda de pseudonimização (`ai/pii.py`), que fica como segunda parede — ele não mascara nome de terceiro desconhecido (o próprio docstring diz), por isso não é a defesa principal.

**Tech Stack:** Python 3.12+, LangChain tools / LangGraph, structlog, SQLAlchemy async (SQLite em memória nos testes), pytest + pytest-asyncio (`asyncio_mode = "auto"`), ruff.

**Spec:** `docs/superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md` — §2 decisão 4 e §4.6 (ferramentas da IA depois da mudança — **emendadas pelo orquestrador em 2026-10-03** com a decisão do dono acima), §5 critérios 5, 6 e 8 (a IA nunca recebe título, participante nem id de evento; nada chega à confirmação sem horário re-derivado; o toque em Confirmar é o único jeito de criar a consulta), §4.8 (o que continua fechado), §4.11 (interruptor por tenant), §7 (linha P4: `create_event` & cia. como porta dos fundos).

**Depende de:** P1 (`2026-10-02-ia-p1-registro-de-handbacks.md`), **P2a** (`…-p2a-rascunho-coluna-e-resolvedor.md`: `services/availability.py`), **P2b** (`…-p2b-handbacks-ferramenta-e-estado.md`: `flow_router.ai_draft_v2_enabled`, `graph._tool_cache_key`, `_flow_handback_tools` com `draft_tool`, `BookingDraftRequested` v2, `_draft_professional_id`, `services/llm_context.py::build_conversation_state`) e **P3** (`…-p3-confirmacao-expressa.md`: `ManageAppointmentRequested` v2, `services/manage_request.py`, `TOOL_BLOCK_BAD_APPOINTMENT`, `manage_existing_appointment_v2`, `_appointment_context_text(..., with_refs=)`) executados neste worktree, nessa ordem. Não edita `flow_router.py`, `sentinels.py` nem `manage_request.py`: as ferramentas cegas só levantam as exceções que o P2b/P3 já tratam. O P5 (`ai/prompts_v2.py`) depende deste plano: a seção "Para o P5" abaixo lista os nomes e o texto exatos.

## Global Constraints

- Worktree `C:\TECH\BRAIN-worktrees\TASK-030\secretarIA` (branch da TASK-030). Tarefas **sequenciais**: P1 → P2a → P2b → P3 → P4 → P5 editam arquivos em comum (`ai/tools.py`, `ai/graph.py`, `workers/shared/llm_context.py`); nunca dois agentes ao mesmo tempo. Dentro do P4, as tarefas também são sequenciais (1 → 10).
- Testes rodam do **Git Bash**, na raiz do worktree: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest <arquivo> -q` (PowerShell não consegue setar variável vazia; nunca `pytest` solto).
- Nunca `ruff format .`. `uvx ruff format <arquivo>` só nos arquivos **criados** por este plano (`services/availability_windows.py`, `ai/availability_tool.py`, `ai/staging_tools.py`, `ai/tool_output.py` e os nove arquivos de teste novos), e ANTES do `uvx ruff check` (ele quebra as linhas longas que o `E501` acusaria). `uvx ruff check <arquivos tocados>` sempre. Em arquivo existente: antes de editar, anote `uvx ruff format --diff <arquivo> | grep -c '^@@'`; depois de editar, a contagem não pode aumentar (escreva no estilo ruff: o P1 mediu `graph.py` 2; em `b0ac5ee`, `ai/tools.py` 4 e `tests/test_ai_tools_cancel_money.py` 0; confira os demais antes de começar).
- A árvore está em CRLF (índice em LF). Antes de cada commit, `git diff --cached --stat` mostra só as linhas do passo; se um arquivo existente aparecer inteiro como alterado, `git restore --staged <arquivo>` e refaça a edição com âncoras pequenas. Arquivo novo criado pelo plano: LF, como o P2 fez.
- Camadas: api → workers → services/ai → models → core. `services/` nunca importa `ai/` nem `workers/` (`availability_windows.py` só importa `services/availability.py`). `ai/availability_tool.py` e `ai/staging_tools.py` importam `services/*` e as variáveis de contexto/exceções de `ai/tools.py`; `ai/tool_output.py` não importa nada do projeto além de `core.logging`; `plugins/multi_professional.py` só por import TARDIO (os plugins importam `ai.tools`; o contrário em nível de módulo faria ciclo). `ai/graph.py` importa `ai.tool_output`; `workers/shared/llm_context.py` importa `ai.availability_tool` e `ai.staging_tools`.
- **A IA v2 é cega para dados de paciente e de agenda (decisão do dono, 2026-10-03):** nenhuma ferramenta do conjunto v2 devolve nome, telefone, e-mail, título, descrição, participante, local, link ou id de evento, nem nada de outro paciente ou de outro tenant — nem pseudonimizado. Toda ferramenta v2 declara em `ai/tool_output.py::V2_TOOL_OUTPUTS` as chaves que pode devolver (e, para listas de registros, as chaves de cada registro); `build_agent` descarta qualquer outra antes de o resultado voltar ao modelo e registra só o NOME da chave. Nenhum dict da API do Google é repassado em lugar nenhum. `get_availability` só devolve as chaves documentadas (`windows`, `timezone`, `slot_minutes`, `day_from`, `day_to`, `professional`, `clamped`, `note`; cada janela só `day`/`start`/`end`); `create_event`/`cancel_event` v2 só devolvem `{"error": "<frase>"}` (o sucesso é a devolução ao fluxo, que encerra o turno).
- **Nada que o modelo digita chega à agenda:** as ferramentas cegas não têm argumento de título, descrição, fim, nome nem id de evento; o título e a descrição do evento são compostos pelo servidor, no toque do paciente (`flow_router._handle_confirmation`: `f"{serviço} - {atendido ou paciente}"` + `build_event_description`), a partir do registro gravado.
- **Segredos e tenant:** o refresh token do Google só é decriptado em `services/tenant_config.py`; a ferramenta obtém a agenda pela resolução que o fluxo usa (`plugins.multi_professional._professional_calendar` → `resolve_professional_calendar`, ou a agenda que o `run_agent` montou do mesmo config) e **nunca** pelo calendário de ambiente que `ai/tools.py::_get_calendar` usa como atalho de desenvolvimento. Roster, catálogo, agenda e reservas são lidos para `_tenant_id_ctx`, o tenant DESTE turno.
- **Fuso:** "hoje" e todo horário saem no fuso da clínica (`calendar.tzinfo`), nunca em UTC (skills `naive-timestamp-serialization`, `date-derived-ui-labels`: uma âncora só; testes com relógio fixo, nunca `datetime.now()` solto).
- Sem PII nem segredo em log: ids, contagens, booleanos e códigos de motivo. Nunca nome de profissional/serviço, dia, horário nem texto do paciente. A frase de erro para o modelo nunca ecoa um valor recebido (pode ser fala do paciente).
- Não tocar `ai/prompts.py` (dono: P5). As frases de erro/nota que o modelo lê são em português; código, comentários e mensagens de commit em inglês.
- Com o interruptor desligado o conjunto não muda: `toolset_v2` e `_ai_toolset_v2_ctx` têm padrão `False`; os testes de regressão da Task 4 e da Task 7 fixam o conjunto de hoje, nome por nome. **A única mudança fora do interruptor** é a da Task 1 (correção de segurança): o `cancel_event` legado passa a recusar evento que não é do paciente da conversa, em todas as clínicas.
- Cada commit termina com `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Commit local permitido; push e deploy só com pedido explícito do dono.

## Review Focus

Cada linha tem o teste que a fixa, na tarefa dona do código.

- **A IA cancela o evento de OUTRA pessoa pelo `cancel_event` legado** (todas as clínicas) → recusado antes do Google: outro paciente do mesmo tenant, outro tenant, id desconhecido, id vazio, sem contexto de paciente, linha sem paciente, banco fora do ar; o do próprio paciente cancela como antes, com a nota do Pix (T1 `test_an_event_that_is_not_the_patients_is_refused_before_google`, `test_the_events_tenant_with_another_patient_is_still_refused`, `test_without_a_patient_context_nothing_is_cancelled`, `test_an_event_row_without_a_patient_is_nobodys_to_cancel`, `test_a_database_outage_refuses_instead_of_cancelling`, `test_the_patients_own_event_is_cancelled_as_before`; `tests/test_ai_tools_cancel_money.py` inteiro).
- **Nome, telefone, e-mail, título, id de evento ou dado de outro tenant chegando ao modelo por QUALQUER caminho** (resultado de ferramenta, prompt, histórico) → nunca: agenda-isca e banco-isca com strings únicas, todas as ferramentas v2 executadas, prompt e mensagens capturados na costura `invoke_agent` (T9 `test_no_decoy_reaches_the_model_on_a_v2_turn`, `test_every_v2_tool_was_run_against_the_decoys`; T5 `test_a_calendar_event_never_reaches_the_ai_in_any_field`; T3 `test_a_busy_event_removes_its_time_and_none_of_its_fields_survive`).
- **Ferramenta v2 devolvendo uma chave que não declarou** (um dict do Google repassado, uma ferramenta de plugin nova) → a chave cai antes do modelo, o log leva só o nome; ferramenta sem declaração responde erro; o teste estático falha (T8 `test_every_tool_of_every_v2_turn_declares_its_output`, `test_an_undeclared_key_is_dropped_and_logged_by_name_only`, `test_an_undeclared_tool_answers_an_error_instead_of_its_data`; T9 `test_every_raw_v2_output_stays_inside_its_allowlist`).
- **A IA cancela por uma referência que não é do paciente** (horário de outro paciente, de outro tenant, dois do paciente no mesmo minuto, id de evento, texto livre) → erro curto, nada adivinhado, nada ecoado (T6 `test_an_appointment_that_is_not_the_patients_is_never_targeted`, `test_another_tenants_appointment_at_the_same_minute_is_not_ours`, `test_two_of_the_patients_appointments_at_the_same_minute_are_never_guessed`, `test_an_event_id_or_free_text_is_not_a_reference`).
- **A IA "marca" por `create_event` com título/nome/fim inventados ou horário em formato estranho** (offset, segundos, `25:00`, `30/02`, dígitos árabes, 5000 caracteres) → nada disso chega ao rascunho nem ao log; o pouso é o mesmo do `set_booking_draft` com dia e horário (T6 `test_a_title_or_a_name_the_model_adds_never_reaches_the_draft`, `test_an_unusable_start_is_a_short_error_that_echoes_nothing`, `test_create_lands_exactly_like_the_draft_with_day_and_time`, `test_cancel_lands_exactly_like_the_manage_request`).
- **Médico sem nenhuma janela livre** → lista vazia e uma frase, não erro; e custa UMA leitura de agenda (T5 `test_a_doctor_with_no_free_window_gets_an_empty_list_and_a_sentence`; T3 `test_an_agenda_with_nothing_free_costs_one_calendar_read`).
- **Intervalo que atravessa "hoje" com horas já passadas** → só o que ainda não começou (T5 `test_today_with_hours_already_passed_goes_through_the_real_calendar`; T3 `test_a_range_that_starts_today_drops_the_hours_that_already_passed`, com o `CalendarService` real).
- **Fuso da clínica ≠ UTC e a virada de horário de verão** — o "hoje" omitido é o da clínica, no mesmo horário UTC em datas diferentes de NY (T5 `test_an_omitted_first_day_is_the_clinics_today_not_the_servers`); o dia da virada lê como os vizinhos (T3 `test_a_dst_transition_day_reads_like_its_neighbours`; T2 `test_the_wall_clock_is_read_as_is_whatever_the_utc_offset`); a referência de cancelamento é lida no fuso da clínica (T6 `test_the_reference_is_read_in_the_clinics_zone`).
- **Dois tenants** — médico de mesmo nome em duas clínicas lê a agenda de cada uma, em paralelo; reserva de um tenant não esconde horário do outro (T5 `test_two_tenants_with_a_same_named_doctor_each_read_their_own_agenda`, `test_another_tenants_tenant_level_hold_hides_nothing`, `test_a_config_of_another_tenant_is_refused_before_anything_is_read`).
- **Profissional que o tenant não tem** → erro curto, sem montar agenda nenhuma e sem ecoar o nome (T5 `test_a_doctor_the_tenant_does_not_have_is_unknown_and_no_agenda_is_built`; tabela `test_a_multi_professional_request_that_cannot_be_resolved_is_a_short_error`).
- **Intervalos absurdos** — 365 dias (cortado em 14 e avisado), negativo, passado, ano 9999 (sem estourar a aritmética de datas), formato `20261008`/`2026-W41-4`/dígitos árabes, texto de 5000 caracteres (T2 `test_an_unusable_range_is_refused_with_a_stable_code`; T5 `test_an_unusable_day_range_is_an_error_dict_and_never_reads_the_agenda`, `test_a_365_day_range_is_clamped_and_the_answer_says_so`, `test_the_range_stops_where_booking_stops`).
- **Falha da API de calendário** — decisão e teste: queda do Google (token revogado incluído) **propaga como hoje** e `run_agent` a transforma na passagem para humano + alerta ao dono; QUALQUER outra falha vira `{"error": ...}` com log só do tipo (T5 `test_a_calendar_outage_propagates_like_every_other_calendar_read`, `test_any_other_failure_is_an_error_dict_not_a_crash`; T7 `test_a_calendar_outage_inside_get_availability_reaches_the_handover_sentinel`); banco fora do ar no `cancel_event` cego → erro que proíbe dizer "cancelado" (T6 `test_a_database_failure_is_an_error_and_never_a_cancel`).
- **Reservas** — o horário segurado por outra conversa não é oferecido; o segurado pela própria conversa sim; reserva de outro médico ou de outro tenant não esconde nada (T5 `test_a_slot_another_conversation_is_holding_is_not_offered`, `test_the_patients_own_hold_does_not_hide_the_slot_from_them`, `test_a_hold_on_another_doctor_hides_nothing_on_this_doctors_agenda`).
- **Interruptor desligado = conjunto de hoje** (T4 `test_switch_off_leaves_every_base_tool_as_it_was`; T7 `test_switch_off_is_exactly_todays_composition` e `test_a_clinic_without_the_switch_keeps_todays_toolset`); **ligado = nenhuma leitora de ocupado, nenhuma escritora, e `create_event`/`cancel_event` só na versão cega**, nem se um chamador passar a legada (T7 `test_switch_on_swaps_the_busy_readers_for_get_availability_and_the_blind_writers`, `test_switch_on_strips_a_withheld_or_legacy_tool_even_when_a_caller_passes_it`); **ferramenta nova de calendário não passa em branco** (T4 `test_every_calendar_touching_tool_is_classified`).
- **Pseudonimização como segunda parede** — cada ferramenta v2 é entregue com a lista de saída POR DENTRO do guarda de pseudonimização, e o guarda não altera janelas (T8 `test_a_v2_agent_wraps_every_tool_inside_the_pseudonymization_guard`; T5 `test_the_agent_builds_the_tool_behind_the_guard`, `test_the_pseudonymization_guard_wraps_the_tool_and_leaves_windows_intact`).

## Decisões deste plano (mudar só se o dono discordar)

1. **Forma da resposta.** O spec fixa `{day, start, end}` por janela e `timezone`. Este plano acrescenta chaves documentadas e testadas: `slot_minutes` (a duração usada), `day_from`/`day_to` (o intervalo que a lista cobre de fato), `professional` (o nome do profissional cuja agenda foi lida, inclusive quando ele foi inferido do serviço; ausente numa clínica sem profissionais), e — só quando existirem — `clamped` (lista de códigos: `max_days`, `booking_window`, `max_windows`) e `note` (uma frase em português que diz o que a lista NÃO cobre, ou que não há horário livre). O teste do critério 5 do spec falha com qualquer chave fora desse conjunto e com qualquer chave extra dentro de uma janela.
2. **Corte (clamp).** No máximo 14 dias por chamada, 30 janelas no total (as mais cedo primeiro) **e nunca além do horizonte de agendamento** (`flow_router.DAY_PICKER_WINDOW_DAYS` = 20 dias): uma janela que o resolvedor do P2 descartaria como `out_of_window` não pode ser oferecida pela IA. Só um `day_to` EXPLÍCITO que passe do limite conta como corte (`clamped` + `note`); `day_to` omitido vai até o maior intervalo permitido, sem aviso. Corte por janelas: `day_to` da resposta passa a ser o dia da última janela devolvida e `clamped` ganha `max_windows`. Para saber que há mais de 30 a varredura lê um dia a mais, e para.
3. **Formato do erro.** `{"error": "<frase em português>"}`, igual ao resto do repositório, sempre recuperável pelo modelo no mesmo turno (nunca exceção). A frase lista os nomes válidos de profissional/serviço (dado da clínica) e **nunca repete** o valor recebido. O motivo vai para o log como enum em `agent_tool_blocked` (`tool="get_availability"`, `reason=...`): `no_clinic`, `tenant_mismatch`, `no_calendar`, `no_professionals`, `professional_required`, `professional_unknown`, `professional_ambiguous`, `service_unknown`, `no_professional_offers_service`, `several_professionals_offer_service`, `bad_day_format`, `past_day`, `reversed_range`, `beyond_window`.
4. **Falha do calendário.** `CalendarUnavailableError` (e `GoogleTokenRevokedError`) **propaga**, como em `check_availability`/`list_free_slots` hoje: `run_agent` devolve `CALENDAR_UNAVAILABLE_SENTINEL`, o trabalhador entrega a conversa a uma pessoa e alerta o dono (e-mail com silêncio de `CALENDAR_ALERT_SILENCE_SECONDS`). Engolir isso num `dict` esconderia do dono um token revogado e deixaria o paciente numa conversa sem agenda. Todo o resto (credencial ausente, erro de banco, bug) vira `{"error": "Não consegui consultar a agenda agora ... NUNCA invente horários."}` + `agent_availability_failed(error_type=...)`, nunca a mensagem da exceção.
5. **Sem `service`.** Usa a duração padrão da agenda (`CalendarService.default_slot_minutes`, propriedade nova que só expõe o que o calendário já usa) e a devolve em `slot_minutes`. Pedir o serviço antes de qualquer "tem horário?" criaria uma pergunta a mais para o paciente.
6. **O que sai e o que fica com variante cega (decisão do dono de 2026-10-03).** `AI_TOOLSET_V2_WITHHELD` = cinco nomes sem variante no v2: as três LEITORAS de ocupado/horários crus (`check_availability`, `list_free_slots`, `list_free_slots_for_professional` — esta sem desconto de reservas: ofereceria horário que outro paciente está confirmando) e as duas escritoras de plugin. **`create_event_for_professional` é dobrada em `create_event`**: o profissional é um campo do rascunho (`professional`), e o resolvedor do P2 já resolve a clínica multi (pergunta o médico quando falta, recusa o que ele não oferece). **`create_event_at_unit` fica retirada**: unidade não é campo do rascunho nem é gravada por `_handle_confirmation` (o fluxo de botões nunca registrou unidade), então mantê-la seria uma escritora fora do toque do paciente. `AI_TOOLSET_V2_STAGING` = `create_event`, `cancel_event`: num turno v2 a implementação LEGADA desses nomes é retirada (trava 1, por identidade da variante) e recusa sozinha (trava 2); no lugar dela entra a variante cega (`ai/staging_tools.py`, `metadata={"cache_variant": "blind_v2"}`). Consequências conhecidas, no CHECKPOINT: a IA perde o `professional_context` que o leitor antigo anexava (o bloco "SOBRE O PROFISSIONAL" do prompt continua) e, com o addon `multi_unit`, nenhum agendamento pela IA registra a unidade (o de botões também não). `list_units`, `list_professionals`, `select_professional_and_continue` ficam (P5 remove a última).
7. **Dono das reservas** = `resolve_booking_owner_id` (a regra única dos agendamentos; o mesmo `_hold_owner` do P2a): o profissional escolhido numa clínica multi; o único profissional numa clínica de um só; `None` numa clínica sem profissionais. O portão de reservas é criado SEM armar (a ferramenta nunca reserva) e exclui a reserva da própria conversa, como os seletores.
8. **De onde vem a agenda.** Um profissional ou nenhum → a agenda que `run_agent` montou (`_calendar_ctx`, do mesmo `TenantRuntimeConfig` que já resolve o profissional único); vários → `_professional_calendar` (única resolução por profissional). Sem agenda no contexto ou sem config numa clínica multi → recusa `no_calendar`, nunca o calendário de ambiente.
9. **Interruptor.** `llm_context._ai_toolset_v2(tenant)` = `flows_enabled(tenant)` E `ai_draft_v2_enabled(tenant)`: o agente v2 só marca/cancela devolvendo ao fluxo (inclusive pelas variantes cegas, que levantam as exceções de devolução) — uma clínica sem o fluxo jamais pode trocar o `create_event` legado pelo cego. Lido uma vez por turno em `orchestrator.py` e passado a `run_agent(toolset_v2=...)`.
10. **Janela não é grade.** O fluxo só aceita horários na grade da agenda (início da janela + múltiplos de `slot_minutes`). Se a IA sugerir 09:10 dentro de uma janela 08:00–10:00 de 40 minutos, o resolvedor do P2 cai na lista de horários daquele dia (critério 3 do spec): comportamento aceito, sem erro. O texto da ferramenta diz "trecho contínuo em que cabe uma consulta" e devolve `slot_minutes` para o modelo propor horários coerentes. Vale igual para `create_event` cego (mesmo resolvedor).
11. **`create_event` cego** = `create_event(start: str, service: str = "", professional: str = "")`. `start` é `AAAA-MM-DDTHH:MM` (ou com espaço; `:00` de segundos tolerado), hora de parede da clínica, só dígitos ASCII; offset, `Z`, segundos ≠ 00 e datas/horas impossíveis são `{"error"}` (`bad_start`), sem ecoar. Nenhum argumento de título, descrição, fim, convênio ou "pra quem": o profissional é resolvido por `_draft_professional_id` (P2b — nome desconhecido/ambíguo vira "não informado" e o fluxo pergunta); convênio e "pra quem" ficam de fora (`None`) e o resolvedor usa o que a conversa já gravou ou pergunta ("Essa consulta é pra você?"). A ferramenta levanta `BookingDraftRequested(serviço, profissional, None, attendee=None, day=..., time=...)` — **o mesmo sentinel byte a byte** que `set_booking_draft` v2 com os mesmos valores (T6 prova) —, então o pouso é o do P2/P3: resolvedor → detalhes + cartão de confirmação → o toque em Confirmar (`_handle_confirmation`: portão de código do Portal, reservas, autorização) é o único que cria evento e `Appointment`. Nunca lê nem escreve na agenda.
12. **`cancel_event` cego** = `cancel_event(appointment: str)`. `appointment` é a referência do P3 (`AAAA-MM-DD HH:MM`, a mesma "(ref …)" que o bloco "consultas marcadas" mostra com o interruptor). A ferramenta lê as consultas FUTURAS do paciente DESTA conversa neste tenant (`load_upcoming_appointments`; a conversa precisa ser do tenant do turno), casa a referência no fuso da clínica (`_tenant_config_ctx.timezone`, como o P3) e: nenhuma → `unknown_appointment`; duas ou mais → `ambiguous_appointment`; sem paciente → `no_patient`; formato/id de evento → `bad_appointment` (constante do P3); banco fora → `{"error"}` que proíbe dizer "cancelado" + `agent_cancel_staging_failed(error_type)`. Uma → levanta `ManageAppointmentRequested("cancel", appointment=ref)` — **o mesmo sentinel** que `manage_existing_appointment` v2 com `action="cancel"` e a mesma referência —, e o worker do P3 relê as consultas e para no cartão "Confirmar o cancelamento?". Diferença deliberada para o gerenciar v2: referência que não casa é erro AQUI (o modelo corrige no mesmo turno), em vez de cair na lista de botões.
13. **Observabilidade das cegas.** `conversation_handback_entered` (P1) sai com o `source_tool` do caminho que pousou (rascunho para `create_event`, gerenciar para `cancel_event`) — os dois pousos SÃO esses caminhos; qual nome o modelo chamou fica em `llm_turn_trace.tools_called` (P1). Recusas: `agent_tool_blocked(tool, reason)` com os códigos acima, nunca o argumento.
14. **Correção do `cancel_event` legado (Task 1, todas as clínicas).** Depois das travas existentes (`_blocked_by_toolset_v2` da Task 4, `_blocked_tenant_level`) e ANTES do Google: `event_id in await _own_google_event_ids([event_id])` — a prova do `do_paciente` (um `Appointment` com esse `google_event_id`, `tenant_id` do turno e `patient_id` da conversa). Falha fechada (sem tenant, sem conversa, sem paciente, banco fora → conjunto vazio). Recusa: `{"error": ...}` que manda usar `manage_existing_appointment` + `agent_tool_blocked(tool="cancel_event", reason="not_patients_event", tenant_id)` — nunca o id. `_mark_appointment_cancelled` e a nota do Pix ficam como estão (o caminho feliz do próprio paciente não muda; `tests/test_ai_tools_cancel_money.py` passa a semear o paciente dono, que antes não existia no teste). Consequência: evento criado pela equipe direto no Google, sem `Appointment` do paciente, não é mais cancelável pela IA (o caminho certo é a equipe ou os botões).
15. **Lista de saída declarada (`ai/tool_output.py`).** Uma entrada por NOME de ferramenta v2: chaves de topo permitidas, chaves de cada registro em listas (`windows[]`, `appointments[]`, `professionals[]`, `units[]`) e se a ferramenta pode responder texto (só `iniciar_pre_consulta`). Valor permitido = escalar ou lista de escalares; dict aninhado sob chave simples cai. Chave fora da lista → descartada, log `agent_tool_output_dropped(tool, keys=[nomes])` (chave que não é identificador simples vira `"<other>"`: um dict indexado por nome de paciente não vaza pelo log). Ferramenta sem entrada, ou síncrona → responde `{"error"}` (falha fechada). Aplicada em `build_agent` só com `toolset_v2`, por DENTRO do guarda de pseudonimização; a chave do cache de agentes ganha a marca `#toolset_v2` (um agente v1 e um v2 com os mesmos nomes — clínica multi sem extras — nunca dividem entrada).
16. **Pseudonimização é a segunda parede, não a primeira.** `ai/pii.py` continua embrulhando toda ferramenta (por fora da lista de saída): mascara padrões (telefone, CPF, e-mail) e valores já no mapa da conversa (o nome do paciente e os nomes de atendidos que ele cadastrou). Ele **não** mascara nome de terceiro desconhecido — o próprio docstring diz — e por isso a defesa principal é estrutural: ferramentas que não leem esses dados, mais a lista de saída declarada que descarta qualquer chave a mais. O guarda fica como segunda parede, atrás das duas. Documentado no docstring de `ai/tool_output.py` e no CHECKPOINT.
17. **Iscas (Task 9).** Um turno v2 inteiro por `graph.run_agent(toolset_v2=True)` com `invoke_agent` trocado por um roteiro (a costura que `tests/test_pii_pseudonymization.py` já usa): banco com outro paciente (nome, telefone, e-mail), o atendido cadastrado pelo próprio paciente, um segundo tenant (clínica, profissional, unidade, paciente, consulta) e agenda Google falsa cujos eventos carregam strings únicas em título, descrição, local, link, participante, criador e id. O roteiro renderiza o prompt que o modelo receberia, executa CADA ferramenta do conjunto v2 já embrulhada (lista de saída + pseudonimização) e renderiza o prompt de novo com os resultados; o teste falha se qualquer isca aparecer em qualquer texto. Um auto-teste prova que o verificador de iscas dispara.

## File Structure

- Modify `src/secretaria/ai/tools.py` — `TOOL_BLOCK_NOT_PATIENTS_EVENT` e a checagem de dono no `cancel_event` legado (T1); `_ai_toolset_v2_ctx`, `AI_TOOLSET_V2_WITHHELD`, `AI_TOOLSET_V2_STAGING`, `BLIND_STAGING_VARIANT`, `TOOL_BLOCK_TOOLSET_V2`, `_blocked_by_toolset_v2` e a trava dentro de `check_availability`, `list_free_slots`, `create_event`, `cancel_event` (T4).
- Create `src/secretaria/services/availability_windows.py` — intervalo de dias (valida/limita), `FreeWindow`, `slots_to_windows`, `scan_free_windows` (T2, T3).
- Modify `src/secretaria/services/calendar.py` — propriedade `CalendarService.default_slot_minutes` (T3).
- Modify `src/secretaria/plugins/multi_professional.py`, `src/secretaria/plugins/multi_unit.py` — a trava dentro de `list_free_slots_for_professional`, `create_event_for_professional`, `create_event_at_unit` (T4).
- Create `src/secretaria/ai/availability_tool.py` — `get_availability` (T5).
- Create `src/secretaria/ai/staging_tools.py` — `create_event_v2`, `cancel_event_v2` (nomes `create_event`/`cancel_event`), códigos `TOOL_BLOCK_*` das cegas (T6).
- Modify `src/secretaria/ai/graph.py` — `base_tools_for(toolset_v2=)`, `_kept_on_v2`, `effective_tools`, `build_agent(toolset_v2=)`, `invoke_agent`, `run_agent(toolset_v2=)` (T7); a lista de saída e a marca `#toolset_v2` na chave do cache em `build_agent` (T8).
- Create `src/secretaria/ai/tool_output.py` — `OutputAllowlist`, `V2_TOOL_OUTPUTS`, `allowlisted`, `undeclared_keys`, `wrap_tools_with_output_allowlist` (T8).
- Modify `src/secretaria/workers/shared/llm_context.py` — `_ai_toolset_v2`, `get_availability` + as duas cegas em `_flow_handback_tools` (T7).
- Modify `src/secretaria/workers/orchestrator.py` — passa `toolset_v2=` a `run_agent` (T7).
- Tests: Create `tests/test_cancel_event_ownership.py` (T1), `tests/test_availability_windows.py` (T2), `tests/test_availability_windows_scan.py` (T3), `tests/test_ai_toolset_v2_locks.py` (T4), `tests/test_get_availability_tool.py` (T5), `tests/test_blind_staging_tools.py` (T6), `tests/test_ai_toolset_v2.py` (T7), `tests/test_ai_tool_output_allowlist.py` (T8), `tests/test_ai_v2_blindness.py` (T9); Modify `tests/test_ai_tools_cancel_money.py` (T1), `tests/test_bot_reply_gating.py` e `tests/test_set_booking_draft_v2.py` (T7).
- Docs (T10): Create `docs/CHECKPOINT_ia_get_availability.md`; uma linha de ponteiro em `docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md`, `docs/CHECKPOINT_plugins.md` e `docs/CHECKPOINT_pseudonimizacao.md`.

## Interfaces

O que este plano **consome** (já existe depois de P1 + P2a + P2b + P3, ou no código atual — cada nome de P2b/P3 abaixo foi conferido no texto daquele plano):

```python
# services/availability.py (P2a, Task A1)
Window = tuple[datetime, datetime]
async def days_with_free_slots(calendar, *, start: datetime, window_days: int, duration_minutes: int, holds: Sequence[Window] = ()) -> list[date]
async def free_slots_for_day(calendar, *, day: date, duration_minutes: int, holds: Sequence[Window] = (), max_slots: int = 96) -> list[datetime]
#   aware datetimes in calendar.tzinfo; both raise CalendarUnavailableError like the calendar does

# services/flow_router.py
DAY_PICKER_WINDOW_DAYS: int = 20            # existing
def ai_draft_v2_enabled(tenant) -> bool     # P2b Task B1: True only for the JSON literal true in initial_flows["ai_draft_v2"]
def flows_enabled(tenant) -> bool           # existing; always True

# services/booking_hold.py (existing)
class BookingGate:  __init__(*, tenant_id, conversation_id, patient_id, external_id, armed)
    async def busy_windows(self, professional_id) -> list[Window]   # fail-open; excludes this conversation's own hold

# services/booking_scope.py (existing)
booking_topology(roster) -> "unknown"|"none"|"sole"|"multi"; resolve_booking_owner_id(roster, selected_id=None) -> UUID | None
canonical_service_name(services, text) -> str | None; service_names(services); service_entry_name(entry)

# plugins/multi_professional.py (existing helpers, imported lazily)
async def _active_professionals(tenant_id) -> list; async def _professional_services(tenant_id, professional) -> list[dict]
async def _professional_calendar(tenant_id, professional) -> CalendarService      # the workflow's per-professional resolution

# ai/tools.py (existing ContextVars) _tenant_id_ctx, _conversation_id_ctx, _calendar_ctx, _tenant_config_ctx, _effective_service_catalog()
# ai/tools.py (existing) async def _own_google_event_ids(event_ids: list[str]) -> set[str]
#   the `do_paciente` proof: ids of Appointments of THIS conversation's patient in THIS turn's tenant;
#   fail-closed (empty set) without tenant/conversation/patient or on a DB error. Used by Task 1.
# ai/tools.py (existing) async def _mark_appointment_cancelled(event_id: str) -> str | None   # Pix note; unchanged
# ai/tools.py (P2b Task B3, defined in its code block; not in its Produces list - P2b now lists it)
class BookingDraftRequested(Exception):
    def __init__(self, appointment_type, professional_id, insurance, *, attendee=None, day=None, time=None)
    draft -> BookingDraft                                   # graph serializes BOOKING_DRAFT_SENTINEL_PREFIX + draft.to_payload()
async def _draft_professional_id(tenant_id: UUID, name: str) -> UUID | None   # exact active name, else None (logged by field/reason)
set_booking_draft_v2                                        # name "set_booking_draft"; args service, professional, insurance, for_whom, day, time
# ai/tools.py (P3 Task 6)
class ManageAppointmentRequested(Exception):
    def __init__(self, action, *, appointment: datetime | None = None, day=None, time=None)
    request -> ManageRequest                                # graph serializes MANAGE_APPOINTMENT_SENTINEL_PREFIX + request.to_payload()
TOOL_BLOCK_BAD_APPOINTMENT = "bad_appointment"
manage_existing_appointment_v2                              # name "manage_existing_appointment"; args action, appointment, day, time
# services/manage_request.py (P3 Task 6)
ACTION_CANCEL = "cancel"; APPOINTMENT_REF_FORMAT = "%Y-%m-%d %H:%M"
def parse_appointment_ref(text: str) -> datetime            # naive clinic-local; ValueError on anything but "AAAA-MM-DD HH:MM"/"T"
def appointment_ref(start_at: datetime, tz: tzinfo) -> str  # the "(ref ...)" the model sees
# services/patient_context.py (existing)
async def load_upcoming_appointments(session, tenant_id, patient_id, *, now=None) -> list[dict]   # dicts with start_at, id, ...
# services/llm_context.py (P2b Task B4)  build_conversation_state(conversation, tenant, professionals) -> str | None
# workers/shared/llm_context.py (P3 Task 6)
def _appointment_context_text(future_appointments, tz_name, professional_names, appointment_types, *, with_refs=False) -> str | None
# ai/graph.py (P2b)   _tool_cache_key(tool) -> str          # NAME, plus "#<cache_variant>" when the tool declares one
# ai/graph.py (P2b/P3) BOOKING_DRAFT_SENTINEL_PREFIX, MANAGE_APPOINTMENT_SENTINEL_PREFIX (existing names)
# workers/shared/llm_context.py (P2b/P3)   _flow_handback_tools(tenant, topology, plugin_tools) with `draft_tool` and `manage_tool`
# ai/pii.py (existing) wrap_tools_with_pseudonymizer(tools) -> tuple     # second wall; cannot mask unknown third-party names
# pseudonymize_core (existing) has_unresolved_tokens(value) -> bool      # used by T9: "not even pseudonymized"
```

O que este plano **produz** (P5 e o CHECKPOINT leem estes nomes):

```python
# services/availability_windows.py
MAX_DAYS = 14; MAX_WINDOWS = 30
CLAMP_MAX_DAYS = "max_days"; CLAMP_BOOKING_WINDOW = "booking_window"; CLAMP_MAX_WINDOWS = "max_windows"
RANGE_BAD_FORMAT = "bad_day_format"; RANGE_PAST_DAY = "past_day"; RANGE_REVERSED = "reversed_range"; RANGE_BEYOND_WINDOW = "beyond_window"
class RangeError(ValueError): code: str                      # str(error) = the sentence the model reads
@dataclass(frozen=True) class DayRange: first: date; last: date; clamped: tuple[str, ...] = (); days -> int
def resolve_range(day_from: str, day_to: str, *, today: date, booking_window_days: int) -> DayRange
@dataclass(frozen=True) class FreeWindow: day: date; start: time; end: time; payload() -> {"day","start","end"}
def slots_to_windows(starts: Sequence[datetime], *, duration_minutes: int) -> list[FreeWindow]
@dataclass class WindowScan: windows: list[FreeWindow]; truncated: bool
async def scan_free_windows(calendar, *, span: DayRange, duration_minutes: int, holds: Sequence[Window] = (), max_windows: int = MAX_WINDOWS) -> WindowScan

# services/calendar.py
CalendarService.default_slot_minutes -> int

# ai/tools.py
TOOL_BLOCK_NOT_PATIENTS_EVENT = "not_patients_event"       # T1: legacy cancel_event refusal
_ai_toolset_v2_ctx: ContextVar[bool]                       # T4; default False; set by graph.run_agent
AI_TOOLSET_V2_WITHHELD: tuple[str, ...]                    # T4; the 5 names with no v2 variant (3 busy readers + 2 plugin writers)
AI_TOOLSET_V2_STAGING: tuple[str, ...] = ("create_event", "cancel_event")   # T4; legacy withheld, blind variant delivered
BLIND_STAGING_VARIANT = "blind_v2"                         # T4; the cache_variant of the blind tools
TOOL_BLOCK_TOOLSET_V2 = "toolset_v2"
def _blocked_by_toolset_v2(tool_name: str) -> dict | None  # T4; second lock; None = allowed

# ai/availability_tool.py (T5)
get_availability                                           # StructuredTool, args professional, service, day_from, day_to (all str = "")
BLOCK_* enums; def _clinic_today(tz) -> date               # the clock seam tests pin

# ai/staging_tools.py (T6)
create_event_v2   # name "create_event"; args start: str, service: str = "", professional: str = ""; raises BookingDraftRequested
cancel_event_v2   # name "cancel_event"; args appointment: str; raises ManageAppointmentRequested("cancel", appointment=ref)
#   both: metadata {"cache_variant": BLIND_STAGING_VARIANT}; return only {"error": "<frase>"}
TOOL_BLOCK_NO_CLINIC = "no_clinic"; TOOL_BLOCK_BAD_START = "bad_start"; TOOL_BLOCK_NO_PATIENT = "no_patient"
TOOL_BLOCK_UNKNOWN_APPOINTMENT = "unknown_appointment"; TOOL_BLOCK_AMBIGUOUS_APPOINTMENT = "ambiguous_appointment"

# ai/graph.py (T7, T8)
base_tools_for(topology: str, *, toolset_v2: bool = False) -> tuple
def _kept_on_v2(tool) -> bool                              # T7: extras filter of a v2 turn (P5 adds the retired names here)
effective_tools(topology: str, extra_tools: Sequence = (), *, toolset_v2: bool = False) -> list
build_agent(extra_tools=(), topology=BOOKING_TOPOLOGY_UNKNOWN, *, toolset_v2: bool = False)
#   T8: on v2 wraps with wrap_tools_with_output_allowlist (inner) then the pii guard (outer); key | {"#toolset_v2"}
run_agent(..., conversation_state=None, toolset_v2: bool = False)

# ai/tool_output.py (T8)
@dataclass(frozen=True) class OutputAllowlist: keys: frozenset[str]; records: Mapping[str, frozenset[str]] = {}; text: bool = False
V2_TOOL_OUTPUTS: Mapping[str, OutputAllowlist]             # one entry per v2 tool NAME
UNDECLARED_ERROR: str; UNUSABLE_ERROR: str
def undeclared_keys(tool_name: str, result) -> list[str]   # [] = clean (what the tests assert on raw outputs)
def allowlisted(tool_name: str, result) -> Any             # drops + logs `agent_tool_output_dropped(tool, keys)`
def wrap_tools_with_output_allowlist(tools) -> tuple       # undeclared or sync tool -> answers {"error": UNDECLARED_ERROR}

# workers/shared/llm_context.py (T7)
def _ai_toolset_v2(tenant) -> bool                         # flows_enabled and ai_draft_v2_enabled
#   _flow_handback_tools(...) appends [get_availability, create_event_v2, cancel_event_v2] on v2
```

## Para o P5 (o que a IA passa a ver — texto e nomes exatos)

**Entram** (só com o interruptor ligado; em TODAS as topologias, entregues por `_flow_handback_tools`):
- `get_availability(professional: str = "", service: str = "", day_from: str = "", day_to: str = "")` — lê janelas livres.
- `create_event(start: str, service: str = "", professional: str = "")` — **cega** (`ai/staging_tools.py::create_event_v2`): só prepara o cartão de confirmação; o toque do paciente em Confirmar é o que marca.
- `cancel_event(appointment: str)` — **cega** (`ai/staging_tools.py::cancel_event_v2`): só leva ao cartão "Confirmar o cancelamento?" de uma consulta do próprio paciente, pela "(ref …)"; o toque em Sim é o que cancela.

**Saem** (só com o interruptor ligado; `AI_TOOLSET_V2_WITHHELD`): `check_availability`, `list_free_slots`, `list_free_slots_for_professional`, `create_event_for_professional` (dobrada no `professional` do `create_event` cego), `create_event_at_unit`. **E a versão LEGADA** de `create_event`/`cancel_event` (`AI_TOOLSET_V2_STAGING`): o nome continua, a implementação é a cega.

**Ficam** (P5 remove depois `start_guided_booking` e `select_professional_and_continue`, e então atualiza os conjuntos exatos de `tests/test_ai_toolset_v2.py`; `_HANDBACKS` de `tests/test_ai_toolset_v2_locks.py` só muda quando o v1 for apagado): `show_main_menu`, `iniciar_pre_consulta`, `list_patient_appointments`, `list_professionals`, `select_professional_and_continue`, `list_units`, `manage_existing_appointment`, `set_booking_draft`, `request_human_handoff`, `start_guided_booking` (só fora de clínica multi).

**O que o prompt v2 precisa dizer das cegas** (regra do dono): nunca afirmar que algo foi marcado/cancelado — a ferramenta só PREPARA o cartão, até o toque do paciente nada mudou; `create_event` para um horário exato (de preferência de uma janela de `get_availability`), `set_booking_draft` quando o paciente disse para quem é ou o convênio (o cego não leva esses campos); `cancel_event` com a "(ref …)" de CONSULTAS MARCADAS, `manage_existing_appointment` para remarcar ou quando o paciente não disse qual.

**Texto que o modelo lê** — o docstring de `get_availability` (é o `description` da ferramenta; o nome é `get_availability` e os quatro argumentos são `str` com padrão `""`):

```text
Consulta os HORÁRIOS LIVRES da agenda de um profissional - só isso. Devolve janelas
    {day, start, end} no fuso da clínica (campo `timezone`), já sem os horários que outros
    pacientes estão reservando. Nunca devolve compromissos, nomes nem horários ocupados: o
    que não está na lista não está livre. Use para responder "tem horário?" ou "tem vaga
    semana que vem?" e para sugerir opções. Esta ferramenta só lê: se o paciente quer marcar
    um horário da lista, chame create_event com esse início (ou set_booking_draft com `day`
    e `time`) - elas só preparam o cartão de confirmação, e o fluxo confere e conduz.

    Cada janela é um trecho contínuo em que cabe uma consulta; `slot_minutes` é a duração
    usada. Mostra no máximo 14 dias e 30 janelas por chamada. Se a resposta trouxer
    `clamped`, a lista NÃO cobre tudo o que foi pedido: diga isso ao paciente e peça um dia
    ou período menor. `note` resume em uma frase o que a lista cobre: respeite-a. Sem
    janelas, diga que não há horário livre nesse período - NUNCA invente um horário nem
    prometa um que não esteja na lista.

    Args:
        professional: Nome do profissional (ou vazio). Em clínica com vários profissionais,
            informe o profissional OU o serviço (se só um profissional oferece o serviço, ele
            é usado).
        service: Nome EXATO de um serviço da clínica (ou vazio: usa a duração padrão do
            profissional, veja `slot_minutes`).
        day_from: Primeiro dia, no formato AAAA-MM-DD, no fuso da clínica (vazio = hoje).
        day_to: Último dia, no formato AAAA-MM-DD (vazio = 14 dias a partir de day_from).
```

**O que o modelo recebe** (exemplo; só estas chaves):

    {"windows": [{"day": "2026-10-06", "start": "08:00", "end": "09:30"}],
     "timezone": "America/Sao_Paulo", "slot_minutes": 30,
     "day_from": "2026-10-05", "day_to": "2026-10-18", "professional": "Dra. Ana"}

`clamped` (lista de códigos) e `note` (frase) só aparecem quando há corte ou lista vazia; o erro é `{"error": "<frase>"}`.

**Texto que o modelo lê das cegas** — os docstrings de `ai/staging_tools.py` (nome `create_event`, argumentos `start: str`, `service: str = ""`, `professional: str = ""`; nome `cancel_event`, argumento `appointment: str`):

```text
Prepara a marcação de uma consulta no horário `start` e leva o paciente ao cartão de
    confirmação. NÃO marca nada: quem marca é o paciente, tocando em Confirmar no cartão;
    até lá, nada está marcado nem reservado. O fluxo confere serviço, profissional, dia e se
    o horário está livre, e pergunta o que faltar - inclusive para quem é a consulta, se o
    paciente ainda não respondeu. Não existe título, descrição nem nome aqui: quem escreve
    na agenda é o fluxo. Se o paciente disse para quem é a consulta ou citou o convênio, use
    set_booking_draft, que leva esses campos.

    Args:
        start: Início pedido, AAAA-MM-DDTHH:MM, no fuso da clínica (ex.: 2026-10-08T10:00) -
            de preferência dentro de uma janela devolvida por get_availability.
        service: Nome EXATO de um serviço da clínica (ou vazio).
        professional: Nome do profissional (ou vazio).
```

```text
Leva o paciente ao cartão "Confirmar o cancelamento?" de UMA consulta JÁ MARCADA
    dele. NÃO cancela nada: quem cancela é o paciente, tocando em Sim no cartão; até lá a
    consulta continua marcada - nunca diga que foi cancelada.

    Args:
        appointment: QUAL consulta, pela referência "(ref AAAA-MM-DD HH:MM)" mostrada em
            "consultas marcadas" (ex.: 2026-10-13 10:00). Nunca um id de evento.
```

O que o modelo recebe delas: nada no sucesso (a devolução ao fluxo encerra o turno) e `{"error": "<frase>"}` na recusa — a frase nunca repete o argumento.

**Trechos de `ai/prompts.py` que deixam de valer na v2** (citados por texto, não por linha): a regra de gerenciar ("NUNCA use check_availability/create_event/cancel_event" — no v2 `cancel_event` existe e é seguro, mas só prepara o cartão), "chame list_free_slots(date, ...) e renderize via [SLOTS]" (as janelas **não** são linhas de `[SLOTS]`: viram texto), "check_availability(start, end) para esse slot", "Só depois de uma confirmação clara do paciente chame create_event" (no v2, chamar `create_event` NÃO marca: leva ao cartão), "prefira list_free_slots", "create_event retornou sucesso" / "create_event devolveu — copie-o inteiro" (o `create_event` cego não devolve evento nem `patient_calendar_link`), os argumentos `summary`/`end`/`description`/`event_id` (não existem nas cegas), o parágrafo da clínica multi que lista as ferramentas não oferecidas, "NUNCA confirme um agendamento sem ter chamado create_event" e "NUNCA invente horários sem chamar check_availability ou list_free_slots". Enquanto o P5 não trocar o prompt, **não ligue o interruptor** em nenhuma clínica (ver "Deploy e liberação").

---

### Task 1: Correção de segurança — o `cancel_event` legado só cancela evento do próprio paciente (todas as clínicas)

**Files:**
- Modify: `src/secretaria/ai/tools.py` (constantes logo antes de `cancel_event`; a checagem de dono dentro dele; uma frase no docstring)
- Modify: `tests/test_ai_tools_cancel_money.py` (o paciente dono passa a existir no teste)
- Test: `tests/test_cancel_event_ownership.py` (novo)

**Interfaces:**
- Consumes: `ai/tools.py::_own_google_event_ids(event_ids: list[str]) -> set[str]` (hoje: a prova do `do_paciente` — `Appointment.google_event_id ∈ ids`, `tenant_id` do turno, `patient_id` da conversa; conjunto vazio sem contexto ou com o banco fora), `_blocked_tenant_level`, `_mark_appointment_cancelled` (inalterado), `logger`, `_tenant_id_ctx`.
- Produces: `TOOL_BLOCK_NOT_PATIENTS_EVENT = "not_patients_event"`; `cancel_event(event_id)` recusa (`{"error": ...}`) todo id que não seja de um `Appointment` deste paciente neste tenant, ANTES de qualquer chamada ao Google. A Task 4 acrescenta a trava v2 antes da `_blocked_tenant_level`; a ordem final é trava v2 → trava multi → dono → Google.

Por que primeiro e fora do interruptor: hoje o `cancel_event` apaga QUALQUER id de evento que o modelo mandar — inclusive a consulta de outro paciente —, sem prova de dono. É uma porta aberta em toda clínica de um só médico (e em toda clínica `unknown`), com ou sem v2.

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_cancel_event_ownership.py`:

```python
"""The legacy `cancel_event` cancels only THIS patient's own event (TASK-030 P4, Task 1).

Before this, the tool deleted ANY Google event id the model passed - a third party's
consultation included - with no ownership check. Now the id must belong to an `Appointment`
of the conversation's patient in the turn's tenant (`ai/tools.py::_own_google_event_ids`,
the same proof `check_availability` uses for `do_paciente`), or the tool refuses BEFORE any
Google call or DB write. Applies to every clinic, switch or no switch.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from contextlib import contextmanager  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai import tools as ai_tools  # noqa: E402
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    Patient,
    Tenant,
)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _session_factory(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(core_database, "async_session_factory", db)


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log


@pytest.fixture
def log(monkeypatch):
    recorder = _Log()
    monkeypatch.setattr(ai_tools, "logger", recorder)
    return recorder


class _Calendar:
    """Records every cancel; a refused call must leave `cancelled` empty."""

    def __init__(self):
        self.tzinfo = UTC
        self.cancelled: list[str] = []

    async def cancel_event(self, event_id: str) -> None:
        self.cancelled.append(event_id)


async def _patient_with_event(db, tenant_id, event_id: str, *, wa_id: str):
    """A patient of `tenant_id`, their conversation, and one upcoming appointment."""
    start = datetime.now(UTC) + timedelta(days=3)
    async with db() as session:
        patient = Patient(tenant_id=tenant_id, wa_id=wa_id, name="Paciente")
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant_id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        appointment = Appointment(
            tenant_id=tenant_id,
            patient_id=patient.id,
            conversation_id=conversation.id,
            google_event_id=event_id,
            appointment_type="Consulta",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(appointment)
        await session.commit()
        return SimpleNamespace(
            patient_id=patient.id, conversation_id=conversation.id, appointment_id=appointment.id
        )


async def _tenant(db) -> Tenant:
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
        session.add(tenant)
        await session.commit()
        return tenant


@pytest_asyncio.fixture
async def world(db):
    """Tenant A: our patient (evt-own) and another patient (evt-other-patient).
    Tenant B: a third patient (evt-other-tenant)."""
    tenant_a, tenant_b = await _tenant(db), await _tenant(db)
    own = await _patient_with_event(db, tenant_a.id, "evt-own", wa_id="5511900000001")
    other = await _patient_with_event(db, tenant_a.id, "evt-other-patient", wa_id="5511900000002")
    foreign = await _patient_with_event(db, tenant_b.id, "evt-other-tenant", wa_id="5511900000003")
    return SimpleNamespace(tenant_a=tenant_a, own=own, other=other, foreign=foreign)


@contextmanager
def _turn(tenant_id, conversation_id, calendar):
    pairs = [
        (ai_tools._tenant_id_ctx, tenant_id),
        (ai_tools._conversation_id_ctx, conversation_id),
        (ai_tools._calendar_ctx, calendar),
    ]
    tokens = [(var, var.set(value)) for var, value in pairs]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


async def _status(db, appointment_id):
    async with db() as session:
        return (await session.get(Appointment, appointment_id)).status


async def test_the_patients_own_event_is_cancelled_as_before(db, world):
    calendar = _Calendar()
    with _turn(world.tenant_a.id, world.own.conversation_id, calendar):
        result = await ai_tools.cancel_event.ainvoke({"event_id": "evt-own"})
    assert result == {"status": "cancelled"}
    assert calendar.cancelled == ["evt-own"]
    assert await _status(db, world.own.appointment_id) == AppointmentStatus.CANCELLED


@pytest.mark.parametrize(
    "event_id",
    ["evt-other-patient", "evt-other-tenant", "evt-that-does-not-exist", ""],
    ids=["another_patient_same_tenant", "another_tenant", "unknown_id", "empty_id"],
)
async def test_an_event_that_is_not_the_patients_is_refused_before_google(db, world, log, event_id):
    calendar = _Calendar()
    with _turn(world.tenant_a.id, world.own.conversation_id, calendar):
        result = await ai_tools.cancel_event.ainvoke({"event_id": event_id})
    assert set(result) == {"error"}
    assert "manage_existing_appointment" in result["error"]
    assert calendar.cancelled == []  # Google was never called
    # Nobody's row moved.
    for appointment_id in (
        world.own.appointment_id,
        world.other.appointment_id,
        world.foreign.appointment_id,
    ):
        assert await _status(db, appointment_id) == AppointmentStatus.SCHEDULED
    (fields,) = [f for e, f in log.events if e == "agent_tool_blocked"]
    assert (fields["tool"], fields["reason"]) == ("cancel_event", "not_patients_event")
    assert fields["tenant_id"] == str(world.tenant_a.id)
    if event_id:
        # A reason code only: the id the model sent is neither logged nor echoed.
        assert event_id not in repr(log.events)
        assert event_id not in result["error"]


async def test_the_other_patient_can_still_cancel_their_own(db, world):
    """The check is per conversation, not a blanket refusal."""
    calendar = _Calendar()
    with _turn(world.tenant_a.id, world.other.conversation_id, calendar):
        result = await ai_tools.cancel_event.ainvoke({"event_id": "evt-other-patient"})
    assert result == {"status": "cancelled"}
    assert calendar.cancelled == ["evt-other-patient"]


async def test_the_events_tenant_with_another_patient_is_still_refused(db, world):
    """Ownership is tenant AND patient: pointing the turn at the event's own tenant does
    not make another tenant's patient's consultation ours."""
    async with db() as session:
        tenant_b = (await session.get(Appointment, world.foreign.appointment_id)).tenant_id
    calendar = _Calendar()
    with _turn(tenant_b, world.own.conversation_id, calendar):
        result = await ai_tools.cancel_event.ainvoke({"event_id": "evt-other-tenant"})
    assert set(result) == {"error"}
    assert calendar.cancelled == []
    assert await _status(db, world.foreign.appointment_id) == AppointmentStatus.SCHEDULED


@pytest.mark.parametrize(
    "tenant_known, conversation_known",
    [(False, True), (True, False), (False, False)],
    ids=["no_tenant", "no_conversation", "no_context"],
)
async def test_without_a_patient_context_nothing_is_cancelled(
    db, world, tenant_known, conversation_known
):
    """Fail closed, like `do_paciente`: no proof of ownership, no cancel."""
    calendar = _Calendar()
    with _turn(
        world.tenant_a.id if tenant_known else None,
        world.own.conversation_id if conversation_known else None,
        calendar,
    ):
        result = await ai_tools.cancel_event.ainvoke({"event_id": "evt-own"})
    assert set(result) == {"error"}
    assert calendar.cancelled == []
    assert await _status(db, world.own.appointment_id) == AppointmentStatus.SCHEDULED


async def test_an_event_row_without_a_patient_is_nobodys_to_cancel(db, world):
    """A row the hub created with no patient (patient_id NULL) is never this patient's."""
    start = datetime.now(UTC) + timedelta(days=5)
    async with db() as session:
        session.add(
            Appointment(
                tenant_id=world.tenant_a.id,
                patient_id=None,
                google_event_id="evt-staff",
                appointment_type="Consulta",
                start_at=start,
                end_at=start + timedelta(minutes=30),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()
    calendar = _Calendar()
    with _turn(world.tenant_a.id, world.own.conversation_id, calendar):
        result = await ai_tools.cancel_event.ainvoke({"event_id": "evt-staff"})
    assert set(result) == {"error"}
    assert calendar.cancelled == []


async def test_a_database_outage_refuses_instead_of_cancelling(world, monkeypatch):
    """`_own_google_event_ids` fails closed (empty set) when the DB is down."""

    def _broken_factory():
        raise RuntimeError("db down")

    monkeypatch.setattr(core_database, "async_session_factory", _broken_factory)
    calendar = _Calendar()
    with _turn(world.tenant_a.id, world.own.conversation_id, calendar):
        result = await ai_tools.cancel_event.ainvoke({"event_id": "evt-own"})
    assert set(result) == {"error"}
    assert calendar.cancelled == []


def test_the_refusal_reason_is_a_stable_enum():
    assert ai_tools.TOOL_BLOCK_NOT_PATIENTS_EVENT == "not_patients_event"
```

Em `tests/test_ai_tools_cancel_money.py` (o caminho feliz do Pix continua o mesmo; o teste só passa a ter o paciente DONO da consulta, que antes não existia):

1. No import de `secretaria.models`, entre `    AppointmentStatus,` e `    PixDeposit,`, inserir `    Conversation,` e `    Patient,` (uma linha cada).
2. Trocar `_agent_context` inteira por:

```python
def _agent_context(tenant_id, calendar=None, conversation_id=None):
    tok_tid = ai_tools._tenant_id_ctx.set(tenant_id)
    tok_cal = ai_tools._calendar_ctx.set(calendar)
    # TASK-030 P4: cancel_event acts only on THIS conversation's patient's own event.
    tok_conv = ai_tools._conversation_id_ctx.set(conversation_id)
    try:
        yield
    finally:
        ai_tools._conversation_id_ctx.reset(tok_conv)
        ai_tools._tenant_id_ctx.reset(tok_tid)
        ai_tools._calendar_ctx.reset(tok_cal)
```

3. Em `_seed`, trocar

```python
        session.add(tenant)
        await session.flush()
        appointment = Appointment(
            tenant_id=tenant.id,
            google_event_id="evt-cancel-1",
```

por

```python
        session.add(tenant)
        await session.flush()
        patient = Patient(tenant_id=tenant.id, wa_id="5511900000001", name="Paciente")
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        appointment = Appointment(
            tenant_id=tenant.id,
            patient_id=patient.id,
            conversation_id=conversation.id,
            google_event_id="evt-cancel-1",
```

4. Nas DUAS ocorrências de `    with _agent_context(tenant.id, calendar=calendar):` (em `test_cancel_event_appends_deposit_notice_to_note_field` e `test_cancel_event_without_deposit_has_no_note_field`), trocar por `    with _agent_context(tenant.id, calendar=calendar, conversation_id=appt.conversation_id):`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_cancel_event_ownership.py tests/test_ai_tools_cancel_money.py -q`
Expected: 11 failed, 2 passed em `test_cancel_event_ownership.py` (passam só os dois cancelamentos do próprio dono; as recusas falham com `assert {'status': 'cancelled'} ...` / `calendar.cancelled == ['evt-other-patient']` e o enum com `AttributeError: ... 'TOOL_BLOCK_NOT_PATIENTS_EVENT'`); `test_ai_tools_cancel_money.py` passa inteiro (o dono já existe e hoje nada é checado).

- [ ] **Step 3: Implement the ownership check**

Em `src/secretaria/ai/tools.py` (antes de editar, anote `uvx ruff format --diff src/secretaria/ai/tools.py | grep -c '^@@'`; em `b0ac5ee`: 4), trocar o começo de `cancel_event`

```python
@tool
async def cancel_event(event_id: str) -> dict:
    """Cancela (deleta) um evento existente pelo seu id. Se o resultado trouxer
    um campo "note", repasse essa frase ao paciente literalmente — é a
    informação honesta sobre reembolso/retenção do sinal (Pix), quando houver.

    Args:
        event_id: ID do evento no Google Calendar.
    """
    blocked = _blocked_tenant_level("cancel_event")
    if blocked is not None:
        return blocked
    await _get_calendar().cancel_event(event_id)
```

por

```python
# TASK-030 P4: `cancel_event` acts only on an event of THIS conversation's patient in THIS
# turn's tenant. Before, any Google event id the model passed was deleted - a third party's
# consultation included. Reason code for `agent_tool_blocked`; never the id itself.
TOOL_BLOCK_NOT_PATIENTS_EVENT = "not_patients_event"

_NOT_PATIENTS_EVENT_ERROR = (
    "Essa consulta não está entre as consultas deste paciente, então não posso cancelá-la. "
    'Para cancelar uma consulta dele, chame manage_existing_appointment com action "cancel": '
    "o fluxo mostra as consultas dele e pede a confirmação."
)


@tool
async def cancel_event(event_id: str) -> dict:
    """Cancela (deleta) um evento existente pelo seu id. Se o resultado trouxer
    um campo "note", repasse essa frase ao paciente literalmente — é a
    informação honesta sobre reembolso/retenção do sinal (Pix), quando houver.
    Só cancela uma consulta DESTE paciente; qualquer outro id é recusado.

    Args:
        event_id: ID do evento no Google Calendar.
    """
    blocked = _blocked_tenant_level("cancel_event")
    if blocked is not None:
        return blocked
    # Ownership BEFORE any Google call or DB write: the same proof `check_availability`
    # uses for `do_paciente` (an `Appointment` of this patient in this tenant carries the
    # id). Fails closed: no tenant, no conversation, no patient or a DB error -> refused.
    if event_id not in await _own_google_event_ids([event_id]):
        tenant_id = _tenant_id_ctx.get()
        logger.warning(
            "agent_tool_blocked",
            tool="cancel_event",
            reason=TOOL_BLOCK_NOT_PATIENTS_EVENT,
            tenant_id=str(tenant_id) if tenant_id else None,
        )
        return {"error": _NOT_PATIENTS_EVENT_ERROR}
    await _get_calendar().cancel_event(event_id)
```

(O resto de `cancel_event` — `_mark_appointment_cancelled` e a nota do Pix — não muda. `_own_google_event_ids` já está definida acima dele no módulo.)

- [ ] **Step 4: Run the tests to verify they pass, plus the suites around this tool**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_cancel_event_ownership.py tests/test_ai_tools_cancel_money.py tests/test_agent_tool_enforcement.py tests/test_pii_pseudonymization.py tests/test_multi_unit_plugin.py tests/test_multi_professional_plugin.py -q`
Expected: PASS (13 novos; a recusa em clínica multi de `test_agent_tool_enforcement.py` continua vindo da trava multi, que roda antes; a nota do Pix inalterada). Verificado numa cópia do repositório em `b0ac5ee`: 77 passed.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format tests/test_cancel_event_ownership.py
uvx ruff check src/secretaria/ai/tools.py tests/test_cancel_event_ownership.py tests/test_ai_tools_cancel_money.py
for f in src/secretaria/ai/tools.py tests/test_ai_tools_cancel_money.py; do echo "$f $(uvx ruff format --diff $f 2>/dev/null | grep -c '^@@')"; done   # none may exceed its count before editing (b0ac5ee: 4 and 0)
git add src/secretaria/ai/tools.py tests/test_cancel_event_ownership.py tests/test_ai_tools_cancel_money.py
git diff --cached --stat
git commit -F - <<'EOF'
fix(ai): cancel_event cancels only the conversation patient's own event

The legacy tool deleted any Google event id the model passed - another patient's
consultation included. It now requires the id to belong to an Appointment of this
conversation's patient in this turn's tenant (_own_google_event_ids, the do_paciente proof),
before any Google call or DB write, failing closed without context or with the DB down.
Refusals log agent_tool_blocked(reason=not_patients_event), never the id. Every clinic,
switch or no switch; the patient's own cancel and the Pix note are unchanged.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 2: `services/availability_windows.py` — o intervalo de dias e o agrupamento em janelas (puro)

**Files:**
- Create: `src/secretaria/services/availability_windows.py`
- Test: `tests/test_availability_windows.py`

**Interfaces:**
- Consumes: nada (só a biblioteca padrão).
- Produces: `MAX_DAYS`, `MAX_WINDOWS`, `CLAMP_*`, `RANGE_*`, `RangeError`, `DayRange`, `resolve_range`, `FreeWindow`, `slots_to_windows` (assinaturas em "Interfaces").

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_availability_windows.py`:

```python
"""Free time as windows: the range rules and the grouping (TASK-030 P4, spec §4.6)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import date, datetime, time  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.services import availability_windows as aw  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
TODAY = date(2026, 10, 5)  # a fixed anchor: no test here reads the wall clock
WINDOW_DAYS = 20  # flow_router.DAY_PICKER_WINDOW_DAYS


def _range(day_from="", day_to="", *, today=TODAY):
    return aw.resolve_range(day_from, day_to, today=today, booking_window_days=WINDOW_DAYS)


# --------------------------------------------------------------------------
# resolve_range: refused, clamped, defaulted
# --------------------------------------------------------------------------


def test_an_empty_range_is_today_through_the_largest_allowed_span():
    span = _range()
    assert (span.first, span.last, span.clamped) == (TODAY, date(2026, 10, 18), ())
    assert span.days == aw.MAX_DAYS == 14


def test_an_explicit_range_inside_the_limits_is_kept_untouched():
    span = _range("2026-10-08", "2026-10-09")
    assert (span.first, span.last, span.clamped, span.days) == (
        date(2026, 10, 8),
        date(2026, 10, 9),
        (),
        2,
    )


def test_a_365_day_range_is_clamped_to_14_days_and_says_so():
    span = _range("2026-10-05", "2027-10-05")
    assert (span.first, span.last) == (TODAY, date(2026, 10, 18))
    assert span.clamped == (aw.CLAMP_MAX_DAYS,)


def test_the_range_never_passes_the_booking_window():
    # Day 10 + 14 days would end on day 23, past the 20-day window the resolver accepts.
    span = _range("2026-10-15", "2026-11-30")
    assert (span.first, span.last) == (date(2026, 10, 15), date(2026, 10, 24))
    assert span.clamped == (aw.CLAMP_BOOKING_WINDOW,)
    # An omitted last day is not a clamp: it simply ends where booking ends.
    assert _range("2026-10-15").clamped == ()
    assert _range("2026-10-15").last == date(2026, 10, 24)


def test_a_range_across_a_month_and_year_boundary_counts_real_days():
    span = _range("2026-12-28", "2027-01-05", today=date(2026, 12, 28))
    assert (span.first, span.last, span.days) == (date(2026, 12, 28), date(2027, 1, 5), 9)
    assert _range(today=date(2026, 12, 28)).last == date(2027, 1, 10)


@pytest.mark.parametrize(
    "day_from, day_to, code",
    [
        ("2026-10-04", "", aw.RANGE_PAST_DAY),  # yesterday
        ("1900-01-01", "", aw.RANGE_PAST_DAY),
        ("", "2026-10-04", aw.RANGE_REVERSED),  # an omitted day_from is today
        ("2026-10-09", "2026-10-08", aw.RANGE_REVERSED),  # negative range
        ("2026-10-25", "", aw.RANGE_BEYOND_WINDOW),  # day 20 is one past the window
        ("9999-12-31", "", aw.RANGE_BEYOND_WINDOW),  # and must not overflow date arithmetic
        ("08/10/2026", "", aw.RANGE_BAD_FORMAT),
        ("2026-13-01", "", aw.RANGE_BAD_FORMAT),
        ("2026-02-30", "", aw.RANGE_BAD_FORMAT),
        ("20261008", "", aw.RANGE_BAD_FORMAT),  # date.fromisoformat accepts these two
        ("2026-W41-4", "", aw.RANGE_BAD_FORMAT),
        ("٢٠٢٦-١٠-٠٨", "", aw.RANGE_BAD_FORMAT),
        ("amanhã", "", aw.RANGE_BAD_FORMAT),
        ("", "next week", aw.RANGE_BAD_FORMAT),
    ],
)
def test_an_unusable_range_is_refused_with_a_stable_code(day_from, day_to, code):
    with pytest.raises(aw.RangeError) as error:
        _range(day_from, day_to)
    assert error.value.code == code
    # The sentence is the model's to read; it never repeats what the model sent.
    for sent in (day_from, day_to):
        assert not sent or sent not in str(error.value)


def test_the_last_bookable_day_is_accepted_and_the_next_is_not():
    assert _range("2026-10-24").first == date(2026, 10, 24)  # today + 19
    with pytest.raises(aw.RangeError):
        _range("2026-10-25")


# --------------------------------------------------------------------------
# slots_to_windows: runs of back-to-back slots, in the clinic's wall clock
# --------------------------------------------------------------------------


def _slot(day: int, hhmm: str, tz=TZ) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(2026, 10, day, hour, minute, tzinfo=tz)


def test_back_to_back_slots_become_one_window_ending_when_the_last_slot_ends():
    starts = [_slot(8, "08:00"), _slot(8, "08:40"), _slot(8, "09:20")]
    (window,) = aw.slots_to_windows(starts, duration_minutes=40)
    assert window.payload() == {"day": "2026-10-08", "start": "08:00", "end": "10:00"}


def test_a_gap_starts_a_new_window():
    starts = [_slot(8, "08:00"), _slot(8, "08:30"), _slot(8, "14:00"), _slot(8, "14:30")]
    windows = aw.slots_to_windows(starts, duration_minutes=30)
    assert [(w.start.strftime("%H:%M"), w.end.strftime("%H:%M")) for w in windows] == [
        ("08:00", "09:00"),
        ("14:00", "15:00"),
    ]


def test_slots_are_sorted_deduplicated_and_never_merged_across_days():
    starts = [_slot(9, "00:00"), _slot(8, "23:30"), _slot(8, "23:30"), _slot(8, "23:00")]
    windows = aw.slots_to_windows(starts, duration_minutes=30)
    assert [w.payload()["day"] for w in windows] == ["2026-10-08", "2026-10-09"]
    assert windows[0].start.strftime("%H:%M") == "23:00"
    assert aw.slots_to_windows([], duration_minutes=30) == []


def test_the_wall_clock_is_read_as_is_whatever_the_utc_offset():
    new_york = ZoneInfo("America/New_York")
    # 2037-03-08 is the US spring-forward Sunday: 08:00 local is UTC-4, the day before UTC-5.
    for day in (7, 8):
        starts = [
            datetime(2037, 3, day, 8, 0, tzinfo=new_york),
            datetime(2037, 3, day, 9, 0, tzinfo=new_york),
        ]
        (window,) = aw.slots_to_windows(starts, duration_minutes=60)
        assert (window.start.strftime("%H:%M"), window.end.strftime("%H:%M")) == ("08:00", "10:00")


def test_a_window_exposes_exactly_day_start_end():
    window = aw.FreeWindow(day=TODAY, start=time(8, 5), end=time(9, 5))
    assert window.payload() == {"day": "2026-10-05", "start": "08:05", "end": "09:05"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_availability_windows.py -q`
Expected: erro de coleta — `ImportError: cannot import name 'availability_windows' from 'secretaria.services'`.

- [ ] **Step 3: Write the module (sem a varredura, que é da Task 3)**

Criar `src/secretaria/services/availability_windows.py`:

```python
"""Free time as WINDOWS: the only shape of an agenda the AI may be told (TASK-030 P4).

`services/availability.py` answers "which instants are free on this agenda, holds
subtracted?". The AI's `get_availability` tool (ai/availability_tool.py) must not hand
those instants to a model one by one - thirty slots of forty minutes would exhaust the
answer in two days - so this module turns them into WINDOWS: maximal runs of consecutive
free slots, `{day, start, end}`, in the clinic's own wall clock.

Nothing here knows what a calendar EVENT is. The inputs are instants and a day range; the
outputs are dates and clock times. That is the contract the tool relies on: no title,
attendee, id, description, link or busy block can leave the calendar layer through this
module, because none of them ever enters it.

Three limits protect the model and the Google quota; each one is REPORTED when it bites
(the tool says so in its answer) instead of silently cutting the list:

  * `MAX_DAYS`          - at most 14 days per call;
  * the booking window  - never past what the patient can actually book
                          (`flow_router.DAY_PICKER_WINDOW_DAYS`, passed in by the caller),
                          so a window the AI offers is one the resolver will accept;
  * `MAX_WINDOWS`       - at most 30 windows per call, earliest first.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

MAX_DAYS = 14
MAX_WINDOWS = 30

# Which limit shortened the answer (the `clamped` list the tool returns).
CLAMP_MAX_DAYS = "max_days"
CLAMP_BOOKING_WINDOW = "booking_window"
CLAMP_MAX_WINDOWS = "max_windows"

# Why a range was refused (stable enums for the log; never the value the AI sent).
RANGE_BAD_FORMAT = "bad_day_format"
RANGE_PAST_DAY = "past_day"
RANGE_REVERSED = "reversed_range"
RANGE_BEYOND_WINDOW = "beyond_window"

# ASCII digits only: `date.fromisoformat` also accepts "20261008" and "2026-W41-4", and a
# model that sends those has not read the tool description.
_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class RangeError(ValueError):
    """The AI's day range cannot be used.

    `code` is one of the RANGE_* enums; `str(error)` is the Portuguese sentence the model
    reads and can act on. It never contains the value the model sent - a model that was
    handed a patient's words in a date field must not get them echoed back.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DayRange:
    """The days a call will scan, both ends included, after every clamp."""

    first: date
    last: date
    clamped: tuple[str, ...] = ()

    @property
    def days(self) -> int:
        return (self.last - self.first).days + 1


def _parse_day(text: str) -> date | None:
    value = (text or "").strip()
    if not value:
        return None
    try:
        if not _ISO_DAY.fullmatch(value):
            raise ValueError("not AAAA-MM-DD")
        return date.fromisoformat(value)
    except ValueError:
        raise RangeError(
            RANGE_BAD_FORMAT,
            "Os dias precisam estar no formato AAAA-MM-DD (ex.: 2026-10-08), no fuso da clínica.",
        ) from None


def resolve_range(day_from: str, day_to: str, *, today: date, booking_window_days: int) -> DayRange:
    """Validate the AI's range against the clinic's own today, then clamp it.

    `today` is the CLINIC's date, never the server's: between 21:00 and 24:00 in
    America/Sao_Paulo the UTC date is already tomorrow.

    Refused (RangeError): a bad format, a first day before today, a last day before the
    first, a first day beyond the booking window. Clamped, and reported in `clamped`: an
    explicit last day further than `MAX_DAYS` from the first, or beyond the booking window.
    An omitted last day is not a clamp - it defaults to the largest range allowed.
    """
    parsed_from = _parse_day(day_from)
    parsed_to = _parse_day(day_to)
    horizon = today + timedelta(days=booking_window_days - 1)

    first = parsed_from if parsed_from is not None else today
    if first < today:
        raise RangeError(
            RANGE_PAST_DAY,
            f"day_from já passou. Hoje é {today.isoformat()}: use hoje ou uma data futura.",
        )
    if first > horizon:
        raise RangeError(
            RANGE_BEYOND_WINDOW,
            f"A agenda só abre até {horizon.isoformat()} ({booking_window_days} dias à frente). "
            "Peça ao paciente um dia dentro desse período.",
        )

    by_days = first + timedelta(days=MAX_DAYS - 1)
    limit, code = (
        (by_days, CLAMP_MAX_DAYS) if by_days <= horizon else (horizon, CLAMP_BOOKING_WINDOW)
    )
    if parsed_to is None:
        return DayRange(first, limit)
    if parsed_to < first:
        raise RangeError(RANGE_REVERSED, "day_to não pode ser anterior a day_from.")
    if parsed_to > limit:
        return DayRange(first, limit, (code,))
    return DayRange(first, parsed_to)


@dataclass(frozen=True)
class FreeWindow:
    """One continuous stretch of free time on one day, in the clinic's wall clock."""

    day: date
    start: time
    end: time

    def payload(self) -> dict[str, str]:
        """The three keys, and only the three, the AI is shown."""
        return {
            "day": self.day.isoformat(),
            "start": self.start.strftime("%H:%M"),
            "end": self.end.strftime("%H:%M"),
        }


def slots_to_windows(starts: Sequence[datetime], *, duration_minutes: int) -> list[FreeWindow]:
    """Group free slot STARTS into maximal runs of back-to-back slots.

    A run `[08:00, 08:40, 09:20]` of 40-minute slots is the window 08:00-10:00: the end is
    when the LAST slot finishes. Grouping reads the clinic's wall clock (the tzinfo is
    dropped before any arithmetic) on purpose: a clinic's day is a row of wall-clock hours,
    and a transition day (DST) must read like its neighbours - converting through UTC is
    what would make it not. Duplicates collapse; slots of different days never merge.
    """
    delta = timedelta(minutes=max(int(duration_minutes), 1))
    runs: list[list[datetime]] = []
    for wall in sorted({start.replace(tzinfo=None) for start in starts}):
        previous = runs[-1][-1] if runs else None
        if previous is not None and wall == previous + delta and wall.date() == previous.date():
            runs[-1].append(wall)
        else:
            runs.append([wall])
    return [
        FreeWindow(day=run[0].date(), start=run[0].time(), end=(run[-1] + delta).time())
        for run in runs
    ]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_availability_windows.py -q`
Expected: PASS (25 testes, contando os parametrizados).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format src/secretaria/services/availability_windows.py tests/test_availability_windows.py
uvx ruff check src/secretaria/services/availability_windows.py tests/test_availability_windows.py
git add src/secretaria/services/availability_windows.py tests/test_availability_windows.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(availability): day-range rules and free-window grouping for the AI tool

Pure module: a day range is validated against the clinic's own today and clamped to 14 days
and to the booking window (reported, never silent); free slot starts are grouped into
back-to-back windows in the clinic's wall clock. No notion of a calendar event exists here.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 3: A varredura de janelas (`scan_free_windows`) e `CalendarService.default_slot_minutes`

**Files:**
- Modify: `src/secretaria/services/availability_windows.py` (imports; `WindowScan`, `scan_free_windows` ao fim)
- Modify: `src/secretaria/services/calendar.py` (propriedade `default_slot_minutes`)
- Test: `tests/test_availability_windows_scan.py`

**Interfaces:**
- Consumes: Task 2; P2a `days_with_free_slots`, `free_slots_for_day`, `Window`; `CalendarService` real (`list_available_days`, `list_free_slots`, `tzinfo`).
- Produces: `WindowScan`, `scan_free_windows`; `CalendarService.default_slot_minutes`.

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_availability_windows_scan.py`:

```python
"""The scan behind get_availability: windows from the real free-time code (TASK-030 P4).

`scan_free_windows` is built only from services/availability.py; here it runs over a fake
calendar (limits, holds, read counts) and over the REAL CalendarService behind a fake Google
client (the pinned clock, the DST days, and the canary that proves no event field survives).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import json  # noqa: E402
from datetime import date, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.services import (  # noqa: E402
    availability_windows as aw,
    calendar as calendar_module,
)
from secretaria.services.calendar import CalendarService, CalendarUnavailableError  # noqa: E402
from secretaria.services.tenant_config import TenantRuntimeConfig  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
TODAY = date(2026, 10, 5)  # a fixed anchor: no test here reads the wall clock

# --------------------------------------------------------------------------
# scan_free_windows: built from services/availability.py, holds subtracted
# --------------------------------------------------------------------------


class _Calendar:
    """`free` maps a day to its free slot STARTS ("HH:MM"). Counts every read."""

    default_slot_minutes = 30

    def __init__(self, free: dict[date, list[str]], *, down: bool = False):
        self.tzinfo = TZ
        self._free = free
        self._down = down
        self.day_scans = 0
        self.slot_reads: list[date] = []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        if self._down:
            raise CalendarUnavailableError("down")
        self.day_scans += 1
        first = start_day.date()
        wanted = {first + timedelta(days=i) for i in range(days)}
        return [
            datetime(d.year, d.month, d.day, tzinfo=TZ)
            for d in sorted(self._free)
            if d in wanted and self._free[d]
        ]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        if self._down:
            raise CalendarUnavailableError("down")
        self.slot_reads.append(day.date())
        return [
            {"start": f"{day.date().isoformat()}T{hhmm}", "end": "", "label": hhmm}
            for hhmm in self._free.get(day.date(), [])[:max_slots]
        ]


def _span(first="2026-10-05", last="2026-10-18"):
    return aw.DayRange(date.fromisoformat(first), date.fromisoformat(last))


async def test_the_scan_returns_windows_earliest_first():
    cal = _Calendar(
        {
            date(2026, 10, 7): ["14:00", "14:30"],
            date(2026, 10, 6): ["08:00", "08:30", "09:00", "15:00"],
        }
    )
    scan = await aw.scan_free_windows(cal, span=_span(), duration_minutes=30)
    assert [
        (w.payload()["day"], w.payload()["start"], w.payload()["end"]) for w in scan.windows
    ] == [
        ("2026-10-06", "08:00", "09:30"),
        ("2026-10-06", "15:00", "15:30"),
        ("2026-10-07", "14:00", "15:00"),
    ]
    assert scan.truncated is False


async def test_a_held_slot_is_not_part_of_any_window():
    cal = _Calendar({date(2026, 10, 6): ["08:00", "08:30", "09:00"]})
    held = (datetime(2026, 10, 6, 8, 30, tzinfo=TZ), datetime(2026, 10, 6, 9, 0, tzinfo=TZ))
    scan = await aw.scan_free_windows(cal, span=_span(), duration_minutes=30, holds=[held])
    assert [(w.start.strftime("%H:%M"), w.end.strftime("%H:%M")) for w in scan.windows] == [
        ("08:00", "08:30"),
        ("09:00", "09:30"),
    ]


async def test_an_agenda_with_nothing_free_costs_one_calendar_read():
    cal = _Calendar({})
    scan = await aw.scan_free_windows(cal, span=_span(), duration_minutes=30)
    assert (scan.windows, scan.truncated) == ([], False)
    assert (cal.day_scans, cal.slot_reads) == (1, [])


async def test_days_outside_the_span_are_ignored():
    cal = _Calendar({date(2026, 10, 4): ["08:00"], date(2026, 10, 20): ["08:00"]})
    scan = await aw.scan_free_windows(cal, span=_span(), duration_minutes=30)
    assert scan.windows == []


def _many_windows(days: int, per_day: int) -> dict[date, list[str]]:
    """`per_day` separate windows a day: one free 30-minute slot every two hours from 08:00."""
    one_day = [f"{8 + 2 * i:02d}:00" for i in range(per_day)]
    return {TODAY + timedelta(days=i): list(one_day) for i in range(days)}


async def test_more_than_30_windows_are_cut_at_30_and_reported():
    cal = _Calendar(_many_windows(days=14, per_day=3))  # 42 windows exist
    scan = await aw.scan_free_windows(cal, span=_span(), duration_minutes=30)
    assert len(scan.windows) == aw.MAX_WINDOWS == 30
    assert scan.truncated is True
    # Earliest first; ten days fill the cap, an eleventh read proves there is more, and the
    # scan stops there instead of reading the other three days.
    assert scan.windows[-1].day == TODAY + timedelta(days=9)
    assert len(cal.slot_reads) == 11


async def test_exactly_30_windows_is_not_a_truncation():
    cal = _Calendar(_many_windows(days=10, per_day=3))  # 30 windows exist
    scan = await aw.scan_free_windows(cal, span=_span(), duration_minutes=30)
    assert (len(scan.windows), scan.truncated) == (30, False)


async def test_a_calendar_outage_propagates_untouched():
    with pytest.raises(CalendarUnavailableError):
        await aw.scan_free_windows(_Calendar({}, down=True), span=_span(), duration_minutes=30)


# --------------------------------------------------------------------------
# The REAL CalendarService behind a fake Google client (no network, a pinned clock)
# --------------------------------------------------------------------------


class _GoogleEvents:
    """events.list replays `items`, shaped like Google's own payload (attendees and all)."""

    def __init__(self, items):
        self._items = items

    def list(self, **_kwargs):
        items = self._items

        class _Request:
            def execute(self):
                return {"items": items}

        return _Request()


class _GoogleService:
    def __init__(self, items):
        self._events = _GoogleEvents(items)

    def events(self):
        return self._events


def _real_calendar(monkeypatch, *, tz_name, hours, minutes=60, items=(), now_utc=None):
    """A real CalendarService whose clock is `now_utc` and whose Google client is a stub."""
    if now_utc is not None:

        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now_utc.astimezone(tz) if tz else now_utc.replace(tzinfo=None)

        monkeypatch.setattr(calendar_module, "datetime", _Clock)
    settings = SimpleNamespace(
        CLINIC_TIMEZONE=tz_name,
        GOOGLE_CALENDAR_ID="primary",
        GOOGLE_CLIENT_ID="id",
        GOOGLE_CLIENT_SECRET="secret",
        GOOGLE_REFRESH_TOKEN="token",
    )
    service = CalendarService(settings=settings)
    service._business_hours = hours
    service._default_slot_minutes = minutes
    monkeypatch.setattr(service, "_service", _GoogleService(list(items)))
    return service


_ALL_WEEK = {
    day: [{"start": "08:00", "end": "12:00"}]
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}


async def _hhmm(calendar, span, minutes):
    scan = await aw.scan_free_windows(calendar, span=span, duration_minutes=minutes)
    return [
        (w.day.isoformat(), w.start.strftime("%H:%M"), w.end.strftime("%H:%M"))
        for w in scan.windows
    ]


async def test_a_range_that_starts_today_drops_the_hours_that_already_passed(monkeypatch):
    # 17:10 UTC is 14:10 in São Paulo: the 14:00 slot has started, the 15:00 one has not.
    calendar = _real_calendar(
        monkeypatch,
        tz_name="America/Sao_Paulo",
        hours={
            "monday": [{"start": "08:00", "end": "18:00"}],
            "tuesday": [{"start": "08:00", "end": "18:00"}],
        },
        now_utc=datetime(2026, 10, 5, 17, 10, tzinfo=ZoneInfo("UTC")),
    )
    span = aw.DayRange(date(2026, 10, 5), date(2026, 10, 6))
    assert await _hhmm(calendar, span, 60) == [
        ("2026-10-05", "15:00", "18:00"),
        ("2026-10-06", "08:00", "18:00"),
    ]


@pytest.mark.parametrize(
    "now_utc, first, last",
    [
        # US spring forward (2037-03-08) and fall back (2037-11-01), New York.
        (datetime(2037, 3, 5, 15, 0, tzinfo=ZoneInfo("UTC")), date(2037, 3, 7), date(2037, 3, 9)),
        (
            datetime(2037, 10, 29, 15, 0, tzinfo=ZoneInfo("UTC")),
            date(2037, 10, 31),
            date(2037, 11, 2),
        ),
    ],
)
async def test_a_dst_transition_day_reads_like_its_neighbours(monkeypatch, now_utc, first, last):
    """08:00-12:00 is 08:00-12:00 on all three days, though the UTC offset changes on one."""
    calendar = _real_calendar(
        monkeypatch, tz_name="America/New_York", hours=_ALL_WEEK, now_utc=now_utc
    )
    windows = await _hhmm(calendar, aw.DayRange(first, last), 60)
    assert [(start, end) for _day, start, end in windows] == [("08:00", "12:00")] * 3
    assert [day for day, _s, _e in windows] == [
        (first + timedelta(days=i)).isoformat() for i in range(3)
    ]


CANARY = "CANARY-7f3a91"


async def test_a_busy_event_removes_its_time_and_none_of_its_fields_survive(monkeypatch):
    event = {
        "id": f"evt-{CANARY}",
        "summary": f"Consulta - {CANARY} Silva",
        "description": f"Notas privadas {CANARY}",
        "htmlLink": f"https://calendar.example/{CANARY}",
        "attendees": [{"email": f"{CANARY}@example.com", "displayName": CANARY}],
        "start": {"dateTime": "2026-10-06T09:00:00-03:00"},
        "end": {"dateTime": "2026-10-06T10:00:00-03:00"},
    }
    calendar = _real_calendar(
        monkeypatch,
        tz_name="America/Sao_Paulo",
        hours={"tuesday": [{"start": "08:00", "end": "12:00"}]},
        items=[event],
        now_utc=datetime(2026, 10, 5, 12, 0, tzinfo=ZoneInfo("UTC")),
    )
    scan = await aw.scan_free_windows(
        calendar, span=aw.DayRange(date(2026, 10, 6), date(2026, 10, 6)), duration_minutes=60
    )
    assert [(w.start.strftime("%H:%M"), w.end.strftime("%H:%M")) for w in scan.windows] == [
        ("08:00", "09:00"),
        ("10:00", "12:00"),
    ]
    payload = json.dumps([w.payload() for w in scan.windows])
    assert CANARY not in payload
    assert set(scan.windows[0].payload()) == {"day", "start", "end"}


def test_the_calendar_exposes_the_slot_length_it_walks_by_default():
    config = TenantRuntimeConfig(
        tenant_id=uuid4(),
        clinic_name="Clinica",
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=40,
        appointment_types=[],
        business_hours={},
        google_calendar_id="cal",
        google_refresh_token=None,
    )
    assert CalendarService.from_tenant_config(config).default_slot_minutes == 40
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_availability_windows_scan.py -q`
Expected: 12 FAIL — `AttributeError: module 'secretaria.services.availability_windows' has no attribute 'scan_free_windows'` (11) e `AttributeError: 'CalendarService' object has no attribute 'default_slot_minutes'` (1).

- [ ] **Step 3: Write the scan**

Em `src/secretaria/services/availability_windows.py`:

1. Trocar o bloco de imports (de `import re` até a linha `from datetime import date, datetime, time, timedelta`) por:

```python
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from secretaria.services.availability import Window, days_with_free_slots, free_slots_for_day

if TYPE_CHECKING:
    from secretaria.services.calendar import CalendarService
```

(o bloco acima já inclui o `if TYPE_CHECKING:`; o resto do arquivo, a partir de `MAX_DAYS = 14`, não muda.)

2. Acrescentar ao fim do arquivo (duas linhas em branco antes de `@dataclass`):

```python
@dataclass
class WindowScan:
    """What a scan found. `truncated`: more windows existed than `max_windows`."""

    windows: list[FreeWindow] = field(default_factory=list)
    truncated: bool = False


async def scan_free_windows(
    calendar: "CalendarService",
    *,
    span: DayRange,
    duration_minutes: int,
    holds: Sequence[Window] = (),
    max_windows: int = MAX_WINDOWS,
) -> WindowScan:
    """Free windows on `calendar` over `span`, earliest first, holds subtracted.

    Built ONLY from `services/availability.py`: one read finds the days with any free time
    (a fully booked or closed agenda costs a single calendar read and no more), then each of
    those days is read slot by slot - sequentially, because the Google client is not safe to
    share across threads - and the scan stops as soon as `max_windows` is exceeded.
    Raises `CalendarUnavailableError` exactly like the calendar does; the caller decides.
    """
    tz = calendar.tzinfo
    free_days = await days_with_free_slots(
        calendar,
        start=datetime(span.first.year, span.first.month, span.first.day, tzinfo=tz),
        window_days=span.days,
        duration_minutes=duration_minutes,
        holds=holds,
    )
    scan = WindowScan()
    for day in free_days:
        if not span.first <= day <= span.last:
            continue
        starts = await free_slots_for_day(
            calendar, day=day, duration_minutes=duration_minutes, holds=holds
        )
        for window in slots_to_windows(starts, duration_minutes=duration_minutes):
            if len(scan.windows) >= max_windows:
                scan.truncated = True
                return scan
            scan.windows.append(window)
    return scan
```

Em `src/secretaria/services/calendar.py`, logo antes de `    def _ensure_tz(self, dt: datetime) -> datetime:`, inserir:

```python
    @property
    def default_slot_minutes(self) -> int:
        """The slot length this agenda walks when no service is named.

        The tenant's default, or - for a professional's agenda - the first active service's
        duration (`for_professional`). The AI availability tool reads it when the model names
        no service (ai/availability_tool.py).
        """
        return self._default_slot_minutes

```

- [ ] **Step 4: Run the tests to verify they pass, plus the suites that read the same calendar code**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_availability_windows.py tests/test_availability_windows_scan.py tests/test_availability.py tests/test_flow_day_picker.py tests/test_calendar_unavailable_mapping.py -q`
Expected: PASS (12 novos; os seletores e o mapeamento de queda do calendário inalterados).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format src/secretaria/services/availability_windows.py tests/test_availability_windows_scan.py
uvx ruff check src/secretaria/services/availability_windows.py src/secretaria/services/calendar.py tests/test_availability_windows_scan.py
uvx ruff format --diff src/secretaria/services/calendar.py | grep -c '^@@'   # must not exceed the count noted before editing
git add src/secretaria/services/availability_windows.py src/secretaria/services/calendar.py tests/test_availability_windows_scan.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(availability): scan an agenda for free windows from the one definition of "free"

scan_free_windows is built only from services/availability.py: one calendar read finds the days
with free time, each of those days is read slot by slot (sequentially), holds are subtracted,
and the scan stops past 30 windows. CalendarService exposes the default slot length it already
walks by. Proven against the real calendar over a fake Google client: hours already passed
today are gone, a DST transition day reads like its neighbours, and no event field survives.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 4: A trava dentro de cada ferramenta retirada ou legada (`_blocked_by_toolset_v2`) — a segunda trava

**Files:**
- Modify: `src/secretaria/ai/tools.py` (`_ai_toolset_v2_ctx`; constantes e `_blocked_by_toolset_v2`; a trava em `check_availability`, `list_free_slots`, `create_event`, `cancel_event`)
- Modify: `src/secretaria/plugins/multi_professional.py` (import; `list_free_slots_for_professional`, `create_event_for_professional`)
- Modify: `src/secretaria/plugins/multi_unit.py` (import; `create_event_at_unit`)
- Test: `tests/test_ai_toolset_v2_locks.py`

**Interfaces:**
- Consumes: `ai/tools.py::_tenant_id_ctx`, `logger`, `_own_google_event_ids` (a checagem de dono da Task 1, que o teste do caminho desligado precisa satisfazer); as ferramentas existentes.
- Produces: `_ai_toolset_v2_ctx`, `AI_TOOLSET_V2_WITHHELD` (5 nomes), `AI_TOOLSET_V2_STAGING = ("create_event", "cancel_event")`, `BLIND_STAGING_VARIANT = "blind_v2"`, `TOOL_BLOCK_TOOLSET_V2`, `_blocked_by_toolset_v2` (assinaturas em "Interfaces"). A Task 6 marca as cegas com `BLIND_STAGING_VARIANT`; a Task 7 filtra por `AI_TOOLSET_V2_WITHHELD` e, para os nomes de `AI_TOOLSET_V2_STAGING`, deixa passar só a variante cega.

As sete implementações que esta trava cobre são as mesmas de antes da decisão de 2026-10-03: as três leitoras de ocupado, as duas escritoras de plugin E as versões LEGADAS de `create_event`/`cancel_event` (que escrevem na agenda). O que mudou é que, no v2, os dois últimos NOMES continuam existindo — na variante cega da Task 6, que não passa por esta trava (ela não está nestas funções).

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_ai_toolset_v2_locks.py`:

```python
"""The v2 toolset's second lock: each withheld or legacy tool refuses by itself (TASK-030 P4).

On a turn that runs on the AI toolset v2 the agent is built without the tools that read a busy
interval or write to the agenda: `ai/tools.py::AI_TOOLSET_V2_WITHHELD` (no v2 variant at all)
and the LEGACY implementations of `AI_TOOLSET_V2_STAGING` (`create_event`/`cancel_event`: on
v2 those names are the blind tools of ai/staging_tools.py, which only stage the patient's
confirmation card) - lock one, asserted in tests/test_ai_toolset_v2.py. This file pins lock
two, the repo's own pattern (`_blocked_tenant_level`): one of them that arrives anyway - a
stale cached graph, a hand-rolled call, a future caller - returns an error BEFORE any Google
call or DB write.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import re  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.ai import (  # noqa: E402
    graph,
    tools as ai_tools,
)
from secretaria.plugins import (  # noqa: E402
    multi_professional as mp,
    multi_unit as mu,
    registry as reg,
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402

_TENANT_LEVEL_AGENDA_TOOLS = {
    "check_availability",
    "list_free_slots",
    "create_event",
    "cancel_event",
}
_PLUGIN_AGENDA_TOOLS = {
    "list_free_slots_for_professional",
    "create_event_for_professional",
    "create_event_at_unit",
}
_BUSY_AND_WRITE = _TENANT_LEVEL_AGENDA_TOOLS | _PLUGIN_AGENDA_TOOLS
_HANDBACKS = {"manage_existing_appointment", "set_booking_draft", "request_human_handoff"}


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log


class _ExplodingCalendar:
    """Any use is a bug: a refused tool must never reach the calendar."""

    def __getattr__(self, name):
        raise AssertionError(f"calendar must not be touched (called {name})")


_WINDOW = {"start": "2026-10-08T08:00:00", "end": "2026-10-08T08:30:00"}
_LOCKED_CALLS = [
    (ai_tools.check_availability, dict(_WINDOW)),
    (ai_tools.list_free_slots, {"day": "2026-10-08"}),
    (ai_tools.create_event, {**_WINDOW, "summary": "Consulta"}),
    (ai_tools.cancel_event, {"event_id": "evt-1"}),
    (mp.list_free_slots_for_professional, {"professional_name": "Dra. Ana", "day": "2026-10-08"}),
    (
        mp.create_event_for_professional,
        {"professional_name": "Dra. Ana", **_WINDOW, "summary": "Consulta"},
    ),
    (mu.create_event_at_unit, {"unit_name": "Centro", **_WINDOW, "summary": "Consulta"}),
]


@pytest.fixture
def _v2_turn():
    tokens = [
        (ai_tools._ai_toolset_v2_ctx, ai_tools._ai_toolset_v2_ctx.set(True)),
        (ai_tools._tenant_id_ctx, ai_tools._tenant_id_ctx.set(uuid4())),
        (ai_tools._calendar_ctx, ai_tools._calendar_ctx.set(_ExplodingCalendar())),
    ]
    yield
    for var, token in reversed(tokens):
        var.reset(token)


def test_the_tested_tools_are_exactly_the_locked_names():
    locked = {*ai_tools.AI_TOOLSET_V2_WITHHELD, *ai_tools.AI_TOOLSET_V2_STAGING}
    assert {t.name for t, _ in _LOCKED_CALLS} == locked == _BUSY_AND_WRITE
    assert set(ai_tools.AI_TOOLSET_V2_STAGING) == {"create_event", "cancel_event"}
    assert set(ai_tools.AI_TOOLSET_V2_WITHHELD).isdisjoint(ai_tools.AI_TOOLSET_V2_STAGING)
    assert len(ai_tools.AI_TOOLSET_V2_WITHHELD) == 5


def test_the_blind_variant_marker_is_stable():
    assert ai_tools.BLIND_STAGING_VARIANT == "blind_v2"
    assert ai_tools.TOOL_BLOCK_TOOLSET_V2 == "toolset_v2"


@pytest.mark.parametrize(("tool", "args"), _LOCKED_CALLS, ids=lambda v: getattr(v, "name", ""))
async def test_a_withheld_or_legacy_tool_refuses_on_a_v2_turn_without_touching_the_calendar(
    _v2_turn, monkeypatch, tool, args
):
    log = _Log()
    monkeypatch.setattr(ai_tools, "logger", log)
    result = await tool.ainvoke(args)
    assert set(result) == {"error"}
    assert "get_availability" in result["error"]
    (fields,) = [f for e, f in log.events if e == "agent_tool_blocked"]
    assert (fields["tool"], fields["reason"]) == (tool.name, "toolset_v2")
    # Names and an enum only: never the arguments, which carry the patient's own words.
    assert "Consulta" not in repr(log.events)


@pytest.mark.parametrize(("tool", "args"), _LOCKED_CALLS[:4], ids=lambda v: getattr(v, "name", ""))
async def test_switch_off_leaves_every_base_tool_as_it_was(monkeypatch, tool, args):
    """The lock is armed only by the v2 context: with it off the tool reaches its own logic
    (here, the calendar - which this test makes refuse loudly). `cancel_event` must also
    pass its owner check (Task 1), so every id counts as the patient's own here."""

    async def _all_theirs(event_ids):
        return set(event_ids)

    monkeypatch.setattr(ai_tools, "_own_google_event_ids", _all_theirs)
    assert ai_tools._ai_toolset_v2_ctx.get() is False
    assert ai_tools._blocked_by_toolset_v2(tool.name) is None
    token = ai_tools._calendar_ctx.set(_ExplodingCalendar())
    try:
        with pytest.raises(AssertionError, match="calendar must not be touched"):
            await tool.ainvoke(args)
    finally:
        ai_tools._calendar_ctx.reset(token)


_CALENDAR_LIKE = re.compile(
    r"(create|cancel|update|delete|reschedule)_event|free_slots|availability"
)


def test_every_calendar_touching_tool_is_classified():
    """A tool added tomorrow that reads or writes the agenda must be put on the withheld or
    the staging list before it can reach a v2 AI - this fails until someone classifies it."""
    everything = EntitlementSummary(
        tenant_id=str(uuid4()),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="basico",
        addons={"multi_professional": True, "multi_unit": True},
        limits={},
    )
    universe = {
        *(t.name for t in graph._BASE_TOOLS),
        *(t.name for t in reg.agent_tools_for(everything)),
        *_HANDBACKS,
        "start_guided_booking",
    }
    assert {name for name in universe if _CALENDAR_LIKE.search(name)} == _BUSY_AND_WRITE
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_ai_toolset_v2_locks.py -q`
Expected: 6 failed, 7 errors, 1 passed — `AttributeError: module 'secretaria.ai.tools' has no attribute '_ai_toolset_v2_ctx'` / `'AI_TOOLSET_V2_WITHHELD'` / `'BLIND_STAGING_VARIANT'`. (O que passa já é `test_every_calendar_touching_tool_is_classified`: ele fixa o universo de hoje e só começa a proteger quando alguém criar uma ferramenta nova.)

- [ ] **Step 3: Implement the lock**

Em `src/secretaria/ai/tools.py` (uma âncora de cada vez):

1. Logo antes da linha `# Note on calendar outages: a tool that hits CalendarUnavailableError simply`, inserir:

```python
# True when THIS turn runs on the AI toolset v2 (TASK-030 P4, per-clinic switch
# `flow_router.ai_draft_v2_enabled`): the agent was built without the tools that read busy
# intervals and without the legacy agenda writers (`AI_TOOLSET_V2_WITHHELD`,
# `AI_TOOLSET_V2_STAGING`); it has `get_availability` and the blind `create_event` /
# `cancel_event` of ai/staging_tools.py instead. Set by graph.run_agent; read by
# `_blocked_by_toolset_v2`, the second lock inside each of those tools. Lives here, like the
# vars above, so a plugin tool module can read it without importing graph.py. The default
# (False) keeps every tool exactly as it was.
_ai_toolset_v2_ctx: ContextVar[bool] = ContextVar("_ai_toolset_v2", default=False)

```

2. Logo antes de `def _sole_professional_id() -> UUID | None:`, inserir:

```python
# The AI toolset v2 (TASK-030 P4, owner's decision of 2026-10-03): the AI never reads a busy
# interval, an event or another patient's data, and nothing it types reaches the agenda. Both
# lists are withheld in ai/graph.py::effective_tools (lock one) and refused again inside each
# tool by `_blocked_by_toolset_v2` (lock two):
#   - AI_TOOLSET_V2_WITHHELD: no v2 variant at all - the busy/raw-slot readers
#     (`list_free_slots_for_professional` has no holds subtracted, so it would offer a slot
#     another patient is confirming) and the two plugin writers (the professional is a field
#     of the blind `create_event`; a unit is not part of the draft at all).
#   - AI_TOOLSET_V2_STAGING: the NAMES stay, the implementation changes - on v2 they are the
#     blind tools of ai/staging_tools.py (metadata cache_variant BLIND_STAGING_VARIANT), which
#     only stage the patient's confirmation card. The legacy implementations below, which
#     write to the agenda, never run on a v2 turn.
AI_TOOLSET_V2_WITHHELD = (
    "check_availability",
    "list_free_slots",
    "list_free_slots_for_professional",
    "create_event_for_professional",
    "create_event_at_unit",
)
AI_TOOLSET_V2_STAGING = ("create_event", "cancel_event")
BLIND_STAGING_VARIANT = "blind_v2"

TOOL_BLOCK_TOOLSET_V2 = "toolset_v2"

_TOOLSET_V2_ERROR = (
    "Esta ferramenta não está disponível para você nesta clínica. Para ver horários livres "
    "use get_availability; para marcar, use create_event ou set_booking_draft; para "
    "cancelar, use cancel_event; para remarcar, use manage_existing_appointment. Todas só "
    "preparam o cartão: quem confirma é o paciente."
)


def _blocked_by_toolset_v2(tool_name: str) -> dict | None:
    """Second lock of the v2 toolset. None = allowed (the switch is off for this turn).

    Same two-lock shape as `_blocked_tenant_level`: graph.build_agent already leaves these
    tools out of a v2 turn's tool set; this catches the paths a tool set cannot - a stale
    cached graph, a hand-rolled invocation, a future caller - and returns BEFORE any Google
    call or DB write.
    """
    if not _ai_toolset_v2_ctx.get():
        return None
    tenant_id = _tenant_id_ctx.get()
    logger.warning(
        "agent_tool_blocked",
        tool=tool_name,
        reason=TOOL_BLOCK_TOOLSET_V2,
        tenant_id=str(tenant_id) if tenant_id else None,
    )
    return {"error": _TOOLSET_V2_ERROR}


```

3. Nas quatro ferramentas base, trocar cada linha `    blocked = _blocked_tenant_level("<nome>")` por duas travas (a nova primeiro). Para `check_availability`:

```python
    blocked = _blocked_by_toolset_v2("check_availability")
    if blocked is not None:
        return blocked
    blocked = _blocked_tenant_level("check_availability")
```

e igual para `"list_free_slots"`, `"create_event"` e `"cancel_event"` (o `if blocked is not None: return blocked` original que vem logo depois de cada uma fica como está; no `cancel_event`, a checagem de dono da Task 1 continua logo depois, então a ordem fica trava v2 → trava multi → dono → Google).

Em `src/secretaria/plugins/multi_professional.py`:

1. No import de `secretaria.ai.tools`, entre `    SelectProfessionalRequested,` e `    _calendar_for_professional,`, inserir a linha `    _blocked_by_toolset_v2,`.
2. Em `list_free_slots_for_professional`, trocar a linha `    from datetime import date, datetime` por:

```python
    blocked = _blocked_by_toolset_v2("list_free_slots_for_professional")
    if blocked is not None:
        return blocked
    from datetime import date, datetime
```

3. Em `create_event_for_professional`, trocar as quatro últimas linhas do docstring (a partir de `        appointment_type: Nome EXATO do serviço DESTE profissional que está`) por elas mesmas seguidas da trava:

```python
        appointment_type: Nome EXATO do serviço DESTE profissional que está
            sendo agendado. Não invente e não use o nome do paciente. Se o
            profissional tiver só um serviço, pode deixar em branco.
    """
    blocked = _blocked_by_toolset_v2("create_event_for_professional")
    if blocked is not None:
        return blocked
```

Em `src/secretaria/plugins/multi_unit.py`:

1. No import de `secretaria.ai.tools`, antes de `    _blocked_tenant_level,`, inserir `    _blocked_by_toolset_v2,`.
2. Trocar a linha `    # This books on the CLINIC's own calendar with no professional in the` (primeira do comentário logo depois do docstring de `create_event_at_unit`) por:

```python
    blocked = _blocked_by_toolset_v2("create_event_at_unit")
    if blocked is not None:
        return blocked
    # This books on the CLINIC's own calendar with no professional in the
```

(a trava nova fica antes do comentário, que continua descrevendo a `_blocked_tenant_level` logo abaixo.)

- [ ] **Step 4: Run the tests to verify they pass, plus every suite that exercises these tools**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_ai_toolset_v2_locks.py tests/test_cancel_event_ownership.py tests/test_agent_tool_enforcement.py tests/test_multi_professional_plugin.py tests/test_multi_unit_plugin.py tests/test_ai_tools_cancel_money.py tests/test_pii_pseudonymization.py tests/test_booking_owner_persistence.py -q`
Expected: PASS (14 novos; as travas existentes, a checagem de dono da Task 1 e o comportamento de hoje das ferramentas, inalterados). Verificado numa cópia do repositório em `b0ac5ee` com as Tasks 1 e 4 aplicadas.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format tests/test_ai_toolset_v2_locks.py
uvx ruff check src/secretaria/ai/tools.py src/secretaria/plugins/multi_professional.py src/secretaria/plugins/multi_unit.py tests/test_ai_toolset_v2_locks.py
for f in src/secretaria/ai/tools.py src/secretaria/plugins/multi_professional.py src/secretaria/plugins/multi_unit.py; do echo "$f $(uvx ruff format --diff $f 2>/dev/null | grep -c '^@@')"; done   # none may exceed its count before editing
git add src/secretaria/ai/tools.py src/secretaria/plugins/multi_professional.py src/secretaria/plugins/multi_unit.py tests/test_ai_toolset_v2_locks.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(ai): busy readers, plugin writers and legacy create/cancel refuse on a v2 turn

The AI toolset v2 withholds five tools with no v2 variant (AI_TOOLSET_V2_WITHHELD: the
busy/raw-slot readers and the two plugin writers) and the LEGACY create_event/cancel_event
(AI_TOOLSET_V2_STAGING: on v2 those names are blind staging tools, marked with
BLIND_STAGING_VARIANT). This is the second lock, the repo's own pattern: each legacy
implementation returns an error before any Google call or DB write when the turn's
_ai_toolset_v2_ctx is on. Off by default: nothing changes until graph.run_agent sets it.
A guard test fails when a new calendar-touching tool is added without being classified.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 5: `ai/availability_tool.py` — a ferramenta `get_availability`

**Files:**
- Create: `src/secretaria/ai/availability_tool.py`
- Test: `tests/test_get_availability_tool.py`

**Interfaces:**
- Consumes: Tasks 2–3 (`resolve_range`, `scan_free_windows`, `RangeError`, `CLAMP_*`, `MAX_*`, `CalendarService.default_slot_minutes`); `ai/tools.py` ContextVars e `_effective_service_catalog`; `plugins.multi_professional` (import tardio); `BookingGate`; `flow_router.DAY_PICKER_WINDOW_DAYS`; `booking_scope` helpers.
- Produces: `get_availability`, `BLOCK_*`, `_clinic_today`, `TOOL_NAME`.

Como a ferramenta decide (para o revisor): (1) lê o roster fresco do tenant do turno e deduz a topologia dele — nunca confia numa variável de contexto velha; (2) uma clínica multi resolve o profissional por nome exato, ou pelo único profissional que oferece o serviço, ou recusa listando os nomes; uma de um só profissional aceita só o nome dele; uma sem profissionais não aceita nome; (3) a agenda vem da resolução do fluxo (nunca do ambiente); (4) só então valida os dias, com o "hoje" da clínica; (5) desconta as reservas e varre; (6) devolve só janelas.

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_get_availability_tool.py`:

```python
"""get_availability: free WINDOWS only, never an event (TASK-030 P4, spec §4.6, criterion 5).

Three layers, all without Google or OpenAI:

  * the answer's SHAPE and the canary: a REAL `CalendarService` over a fake Google client whose
    events carry a unique string in every field (title, attendee, id, description, link) - the
    string must appear nowhere in what the AI receives, and no key outside the documented set
    may appear;
  * resolution and refusals with fake agendas (which doctor, which service, which days, which
    failures), with the clock pinned to a fixed anchor;
  * tenant isolation against a real in-memory database: two tenants, same-named doctors, holds.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import asyncio  # noqa: E402
import json  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from datetime import UTC, date, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from pseudonymize_core import Pseudonymizer  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai import (  # noqa: E402
    availability_tool as at,
    graph,
    pii,
    tools as ai_tools,
)
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Professional, Tenant  # noqa: E402
from secretaria.plugins import multi_professional as mp  # noqa: E402
from secretaria.services import (  # noqa: E402
    booking_hold as booking_hold_service,
    pii_pseudonymization as store,
)
from secretaria.services.calendar import (  # noqa: E402
    CalendarService,
    CalendarUnavailableError,
    GoogleTokenRevokedError,
)
from secretaria.services.tenant_config import (  # noqa: E402
    RuntimeAppointmentType,
    TenantRuntimeConfig,
)

TZ = ZoneInfo("America/Sao_Paulo")
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)  # Monday 09:00 in São Paulo
TUESDAY, WEDNESDAY = date(2026, 10, 6), date(2026, 10, 7)
FREE = {TUESDAY: ["08:00", "08:30", "09:00", "10:00"], WEDNESDAY: ["14:00", "14:30"]}
CANARY = "CANARY-5e2c8b"

DOCUMENTED_KEYS = {
    "windows",
    "timezone",
    "slot_minutes",
    "day_from",
    "day_to",
    "professional",
    "clamped",
    "note",
}


class _Clock(datetime):
    """`availability_tool.datetime`: the clinic's today is whatever the test says."""

    now_utc = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.now_utc.astimezone(tz) if tz else cls.now_utc.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    _Clock.now_utc = NOW
    monkeypatch.setattr(at, "datetime", _Clock)
    return _Clock


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log

    def named(self, event):
        return [fields for name, fields in self.events if name == event]


@pytest.fixture
def log(monkeypatch):
    recorder = _Log()
    monkeypatch.setattr(at, "logger", recorder)
    return recorder


class _Agenda:
    """A fixed free-time table behind the calendar surface `services/availability.py` reads."""

    def __init__(self, free=None, *, minutes=30, tz=TZ, error: Exception | None = None):
        self.tzinfo = tz
        self.default_slot_minutes = minutes
        self.free = FREE if free is None else free
        self.error = error
        self.day_scans = 0
        self.slot_reads: list[tuple[date, int | None]] = []
        self.asked_minutes: list[int | None] = []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        if self.error is not None:
            raise self.error
        self.day_scans += 1
        self.asked_minutes.append(slot_minutes)
        first = start_day.date()
        wanted = {first + timedelta(days=i) for i in range(days)}
        return [
            datetime(d.year, d.month, d.day, tzinfo=self.tzinfo)
            for d in sorted(self.free)
            if d in wanted and self.free[d]
        ]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        if self.error is not None:
            raise self.error
        self.slot_reads.append((day.date(), slot_minutes))
        return [
            {"start": f"{day.date().isoformat()}T{hhmm}", "end": "", "label": hhmm}
            for hhmm in self.free.get(day.date(), [])[:max_slots]
        ]


def _config(tenant_id, timezone="America/Sao_Paulo", types=()) -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
        tenant_id=tenant_id,
        clinic_name="Clinica",
        language="pt-BR",
        timezone=timezone,
        appointment_duration_min=30,
        appointment_types=list(types),
        business_hours={},
        google_calendar_id="tenant-cal",
        google_refresh_token="refresh-token",
    )


@contextmanager
def _turn(tenant_id, *, calendar=None, config=None, conversation_id=None):
    """What graph.run_agent sets before the agent runs."""
    pairs = [
        (ai_tools._tenant_id_ctx, tenant_id),
        (ai_tools._calendar_ctx, calendar),
        (ai_tools._tenant_config_ctx, config),
        (ai_tools._conversation_id_ctx, conversation_id),
    ]
    tokens = [(var, var.set(value)) for var, value in pairs]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


async def _ask(**args):
    return await at.get_availability.ainvoke(args)


def _doctor(name):
    return SimpleNamespace(id=uuid4(), name=name)


_CONSULTA = {"name": "Consulta", "duration_min": 30, "is_active": True, "sort_order": 0}
_RETORNO = {"name": "Retorno", "duration_min": 20, "is_active": True, "sort_order": 1}
_CIRURGIA = {"name": "Cirurgia", "duration_min": 90, "is_active": True, "sort_order": 2}


class _Clinic:
    """A roster, per-doctor catalogs and per-doctor agendas, patched into the plugin helpers."""

    def __init__(self, monkeypatch, tenant_id):
        self.tenant_id = tenant_id
        self.roster: list = []
        self.catalog: dict = {}
        self.agendas: dict = {}
        self.calendars_built: list = []

        async def _active(asked_tenant_id):
            assert asked_tenant_id == tenant_id
            return list(self.roster)

        async def _services(asked_tenant_id, professional):
            assert asked_tenant_id == tenant_id
            return list(self.catalog.get(professional.id, [_CONSULTA]))

        async def _calendar(asked_tenant_id, professional):
            assert asked_tenant_id == tenant_id
            self.calendars_built.append(professional.id)
            return self.agendas.setdefault(professional.id, _Agenda())

        monkeypatch.setattr(mp, "_active_professionals", _active)
        monkeypatch.setattr(mp, "_professional_services", _services)
        monkeypatch.setattr(mp, "_professional_calendar", _calendar)

    def add(self, name, *services, agenda=None):
        doctor = _doctor(name)
        self.roster.append(doctor)
        if services:
            self.catalog[doctor.id] = list(services)
        if agenda is not None:
            self.agendas[doctor.id] = agenda
        return doctor


@pytest.fixture
def clinic(monkeypatch):
    return _Clinic(monkeypatch, uuid4())


# --------------------------------------------------------------------------
# The answer: shape, time zone, professional, duration
# --------------------------------------------------------------------------


async def test_the_answer_is_windows_and_only_the_documented_keys(clinic):
    doctor = clinic.add("Dra. Única")
    agenda = _Agenda()
    with _turn(clinic.tenant_id, calendar=agenda, config=_config(clinic.tenant_id)):
        result = await _ask(day_from="2026-10-05", day_to="2026-10-08")
    assert set(result) <= DOCUMENTED_KEYS
    assert result == {
        "windows": [
            {"day": "2026-10-06", "start": "08:00", "end": "09:30"},
            {"day": "2026-10-06", "start": "10:00", "end": "10:30"},
            {"day": "2026-10-07", "start": "14:00", "end": "15:00"},
        ],
        "timezone": "America/Sao_Paulo",
        "slot_minutes": 30,
        "day_from": "2026-10-05",
        "day_to": "2026-10-08",
        "professional": doctor.name,
    }
    assert all(set(window) == {"day", "start", "end"} for window in result["windows"])
    json.dumps(result)  # serializable as it is


async def test_a_clinic_without_professionals_has_no_professional_key(clinic):
    with _turn(clinic.tenant_id, calendar=_Agenda(), config=_config(clinic.tenant_id)):
        result = await _ask()
    assert "professional" not in result
    assert result["windows"]


async def test_the_service_decides_the_slot_length(clinic):
    doctor = clinic.add("Dra. Única", _CONSULTA, _CIRURGIA)
    agenda = _Agenda(minutes=30)
    with _turn(clinic.tenant_id, calendar=agenda):
        result = await _ask(service=" cirurgia ")  # canonical, case/space-insensitive
    assert result["slot_minutes"] == 90
    assert set(agenda.asked_minutes) == {90}
    assert {minutes for _day, minutes in agenda.slot_reads} == {90}
    assert result["professional"] == doctor.name


async def test_without_a_service_the_agendas_default_duration_is_used(clinic):
    clinic.add("Dra. Única", _CONSULTA, _CIRURGIA)
    agenda = _Agenda(minutes=45)
    with _turn(clinic.tenant_id, calendar=agenda):
        result = await _ask()
    assert result["slot_minutes"] == 45


async def test_a_doctor_with_no_free_window_gets_an_empty_list_and_a_sentence(clinic):
    clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=_Agenda(free={})):
        result = await _ask(day_from="2026-10-05", day_to="2026-10-12")
    assert result["windows"] == []
    assert "clamped" not in result
    assert result["note"] == "Nenhum horário livre de 05/10 a 12/10."


async def test_the_timezone_is_the_clinics_even_when_it_is_not_utc(clinic):
    clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=_Agenda(tz=ZoneInfo("America/New_York"))):
        result = await _ask(day_from="2026-10-06", day_to="2026-10-06")
    assert result["timezone"] == "America/New_York"


@pytest.mark.parametrize(
    "now_utc, tz_name, clinic_today, differs_from_utc",
    [
        # 22:30-23:30 the evening before in São Paulo: the UTC date is already tomorrow.
        (datetime(2026, 10, 3, 2, 0, tzinfo=UTC), "America/Sao_Paulo", "2026-10-02", True),
        # The same UTC time of day in New York lands on different dates across a DST change.
        (datetime(2037, 7, 1, 4, 30, tzinfo=UTC), "America/New_York", "2037-07-01", False),  # UTC-4
        (datetime(2037, 12, 1, 4, 30, tzinfo=UTC), "America/New_York", "2037-11-30", True),  # UTC-5
    ],
)
async def test_an_omitted_first_day_is_the_clinics_today_not_the_servers(
    clinic, clock, now_utc, tz_name, clinic_today, differs_from_utc
):
    clock.now_utc = now_utc
    clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=_Agenda(free={}, tz=ZoneInfo(tz_name))):
        result = await _ask()
    assert result["day_from"] == clinic_today
    assert (now_utc.date().isoformat() != clinic_today) is differs_from_utc


def _real_calendar(monkeypatch, *, hours, minutes, items=(), now_utc=NOW):
    """A real CalendarService (its own slot walk) over a fake Google client, clock pinned."""
    from secretaria.services import calendar as calendar_module

    class _CalendarClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now_utc.astimezone(tz) if tz else now_utc.replace(tzinfo=None)

    monkeypatch.setattr(calendar_module, "datetime", _CalendarClock)
    settings = SimpleNamespace(
        CLINIC_TIMEZONE="America/Sao_Paulo",
        GOOGLE_CALENDAR_ID="primary",
        GOOGLE_CLIENT_ID="id",
        GOOGLE_CLIENT_SECRET="secret",
        GOOGLE_REFRESH_TOKEN="token",
    )
    real = CalendarService(settings=settings)
    real._business_hours = hours
    real._default_slot_minutes = minutes
    monkeypatch.setattr(real, "_service", _GoogleService(list(items)))
    return real


async def test_today_with_hours_already_passed_goes_through_the_real_calendar(
    clinic, clock, monkeypatch
):
    """At 14:10 clinic time the 14:00 slot is gone and 15:00 is not."""
    afternoon = datetime(2026, 10, 5, 17, 10, tzinfo=UTC)
    clock.now_utc = afternoon
    real = _real_calendar(
        monkeypatch,
        hours={
            "monday": [{"start": "08:00", "end": "18:00"}],
            "tuesday": [{"start": "08:00", "end": "18:00"}],
        },
        minutes=60,
        now_utc=afternoon,
    )
    clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=real):
        result = await _ask(day_from="2026-10-05", day_to="2026-10-06")
    assert result["windows"] == [
        {"day": "2026-10-05", "start": "15:00", "end": "18:00"},
        {"day": "2026-10-06", "start": "08:00", "end": "18:00"},
    ]


# --------------------------------------------------------------------------
# No event field of any kind leaves the calendar layer (success criterion 5)
# --------------------------------------------------------------------------


class _GoogleEvents:
    def __init__(self, items):
        self._items = items

    def list(self, **_kwargs):
        items = self._items

        class _Request:
            def execute(self):
                return {"items": items}

        return _Request()


class _GoogleService:
    def __init__(self, items):
        self._events = _GoogleEvents(items)

    def events(self):
        return self._events


def _canary_event(start, end):
    return {
        "id": f"evt-{CANARY}",
        "summary": f"Consulta - {CANARY} Silva",
        "description": f"Notas privadas de {CANARY}",
        "location": f"Sala {CANARY}",
        "htmlLink": f"https://calendar.example/{CANARY}",
        "attendees": [{"email": f"{CANARY}@example.com", "displayName": f"{CANARY} Silva"}],
        "creator": {"email": f"{CANARY}@example.com"},
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }


async def test_a_calendar_event_never_reaches_the_ai_in_any_field(clinic, monkeypatch):
    clinic.add("Dra. Única")
    events = [_canary_event("2026-10-06T09:00:00-03:00", "2026-10-06T10:00:00-03:00")]
    real = _real_calendar(
        monkeypatch,
        hours={"tuesday": [{"start": "08:00", "end": "12:00"}]},
        minutes=60,
        items=events,
    )
    with _turn(clinic.tenant_id, calendar=real):
        result = await _ask(day_from="2026-10-06", day_to="2026-10-06")

    # The busy hour is simply not offered ...
    assert result["windows"] == [
        {"day": "2026-10-06", "start": "08:00", "end": "09:00"},
        {"day": "2026-10-06", "start": "10:00", "end": "12:00"},
    ]
    # ... and nothing of the event survives, in any form.
    assert set(result) <= DOCUMENTED_KEYS
    assert CANARY not in json.dumps(result)
    assert CANARY not in repr(result)
    assert not any(key in result for key in ("busy", "events", "summary", "attendees", "id"))


async def test_the_pseudonymization_guard_wraps_the_tool_and_leaves_windows_intact(clinic):
    clinic.add("Dra. Única")
    (wrapped,) = pii.wrap_tools_with_pseudonymizer([at.get_availability])
    assert wrapped.coroutine is not at.get_availability.coroutine  # the guard is on
    assert (wrapped.name, wrapped.args) == (at.get_availability.name, at.get_availability.args)

    mapper = Pseudonymizer()
    mapper.add_identifier("PACIENTE", "Maria Souza")
    store_token = store._pseudonymizer_ctx.set(mapper)
    try:
        with _turn(clinic.tenant_id, calendar=_Agenda()):
            guarded = await wrapped.ainvoke({"day_from": "2026-10-06", "day_to": "2026-10-07"})
        with _turn(clinic.tenant_id, calendar=_Agenda()):
            plain = await _ask(day_from="2026-10-06", day_to="2026-10-07")
    finally:
        store._pseudonymizer_ctx.reset(store_token)
    assert guarded == plain


def test_the_agent_builds_the_tool_behind_the_guard(monkeypatch):
    class _Recorded:
        def __init__(self, tools):
            self.tools = tools

    graph._AGENTS.clear()
    monkeypatch.setattr(
        graph, "create_react_agent", lambda m, tools, prompt: _Recorded(list(tools))
    )
    try:
        # `toolset_v2=` only exists from Task 7 on; the pseudonymization guard wraps every
        # tool of every agent, so the plain build already proves it.
        agent = graph.build_agent([at.get_availability], "sole")
    finally:
        graph._AGENTS.clear()
    (built,) = [t for t in agent.tools if t.name == "get_availability"]
    assert built.coroutine is not at.get_availability.coroutine


def test_the_model_facing_contract_is_pinned_for_the_prompt_writer():
    tool = at.get_availability
    assert tool.name == "get_availability"
    assert set(tool.args) == {"professional", "service", "day_from", "day_to"}
    text = tool.description
    for phrase in (
        "HORÁRIOS LIVRES",
        "{day, start, end}",
        "Nunca devolve compromissos, nomes nem horários ocupados",
        "set_booking_draft",
        "create_event",
        "só preparam o cartão de confirmação",
        "14 dias e 30 janelas",
        "a lista NÃO cobre tudo o que foi pedido",
        "NUNCA invente um horário",
    ):
        assert phrase in text


# --------------------------------------------------------------------------
# Which doctor, which service: resolved like the draft does, or refused
# --------------------------------------------------------------------------


async def test_a_single_professional_clinic_reads_that_professionals_agenda(clinic):
    doctor = clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=_Agenda(), config=_config(clinic.tenant_id)):
        result = await _ask(professional="dra. única")
    assert result["professional"] == doctor.name
    assert clinic.calendars_built == []  # the tenant-level agenda IS theirs


async def test_a_multi_professional_clinic_resolves_by_name_case_insensitively(clinic):
    ana_agenda, beto_agenda = _Agenda({TUESDAY: ["08:00"]}), _Agenda({TUESDAY: ["15:00"]})
    ana = clinic.add("Dra. Ana", agenda=ana_agenda)
    clinic.add("Dr. Beto", agenda=beto_agenda)
    with _turn(clinic.tenant_id, config=_config(clinic.tenant_id)):
        result = await _ask(professional=" DRA. ana ", day_from="2026-10-06", day_to="2026-10-06")
    assert result["professional"] == ana.name
    assert result["windows"][0]["start"] == "08:00"
    assert clinic.calendars_built == [ana.id]


async def test_a_service_only_one_doctor_offers_resolves_to_that_doctor(clinic):
    clinic.add("Dra. Ana", _CONSULTA)
    beto = clinic.add("Dr. Beto", _RETORNO, agenda=_Agenda(minutes=30))
    with _turn(clinic.tenant_id, config=_config(clinic.tenant_id)):
        result = await _ask(service="retorno")
    assert result["professional"] == beto.name
    assert result["slot_minutes"] == 20
    assert clinic.calendars_built == [beto.id]


_ANA, _BETO = ("Dra. Ana", (_CONSULTA,)), ("Dr. Beto", (_RETORNO,))


@pytest.mark.parametrize(
    "roster, args, reason, fragments",
    [
        ([_ANA, _BETO], {}, "professional_required", ["Dra. Ana", "Dr. Beto", "`professional`"]),
        ([_ANA, _BETO], {"professional": "Dr. Fantasma"}, "professional_unknown", ["Dra. Ana"]),
        (
            [_ANA, _BETO, _ANA],
            {"professional": "Dra. Ana"},
            "professional_ambiguous",
            ["inequívoca"],
        ),
        (
            [_ANA, _BETO],
            {"service": "Botox"},
            "no_professional_offers_service",
            ["Nenhum profissional"],
        ),
        (
            [_ANA, ("Dr. Beto", (_CONSULTA,))],
            {"service": "Consulta"},
            "several_professionals_offer_service",
            ["Dra. Ana, Dr. Beto", "`professional`"],
        ),
        (
            [_ANA, _BETO],
            {"professional": "Dr. Beto", "service": "Consulta"},
            "service_unknown",
            ["Retorno"],
        ),
    ],
    ids=["no_hint", "unknown", "duplicate_name", "nobody_offers", "two_offer", "not_his_service"],
)
async def test_a_multi_professional_request_that_cannot_be_resolved_is_a_short_error(
    clinic, log, roster, args, reason, fragments
):
    for name, services in roster:
        clinic.add(name, *services)
    with _turn(clinic.tenant_id, config=_config(clinic.tenant_id)):
        result = await _ask(**args)
    assert set(result) == {"error"}
    for fragment in fragments:
        assert fragment in result["error"]
    # Never an echo of what the model sent: it may carry the patient's own words.
    for sent in args.values():
        assert sent not in result["error"]
    assert [f["reason"] for f in log.named("agent_tool_blocked")] == [reason]
    assert clinic.calendars_built == []
    assert log.named("ai_availability_read") == []


async def test_a_single_professional_clinic_refuses_another_doctors_name(clinic, log):
    clinic.add("Dra. Única")
    agenda = _Agenda()
    with _turn(clinic.tenant_id, calendar=agenda):
        result = await _ask(professional="Dr. Fantasma")
    assert "Dra. Única" in result["error"] and "Fantasma" not in result["error"]
    assert agenda.day_scans == 0


async def test_a_clinic_without_professionals_refuses_a_professional_name(clinic, log):
    with _turn(clinic.tenant_id, calendar=_Agenda()):
        result = await _ask(professional="Dra. Ana")
    assert set(result) == {"error"}
    assert [f["reason"] for f in log.named("agent_tool_blocked")] == ["no_professionals"]


async def test_a_clinic_without_professionals_reads_the_tenant_catalog(clinic):
    types = [RuntimeAppointmentType(name="Consulta", description=None, duration_min=25)]
    with _turn(clinic.tenant_id, calendar=_Agenda(), config=_config(clinic.tenant_id, types=types)):
        result = await _ask(service="consulta")
    assert result["slot_minutes"] == 25


async def test_an_unknown_service_lists_the_clinics_services_without_echoing_it(clinic):
    clinic.add("Dra. Única", _CONSULTA, _RETORNO)
    with _turn(clinic.tenant_id, calendar=_Agenda()):
        result = await _ask(service="Rinoplastia")
    assert "Consulta, Retorno" in result["error"]
    assert "Rinoplastia" not in result["error"]


# --------------------------------------------------------------------------
# Days: refused, clamped, absurd
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args, reason",
    [
        ({"day_from": "2026-10-04"}, "past_day"),
        ({"day_from": "1900-01-01"}, "past_day"),
        ({"day_from": "2026-10-09", "day_to": "2026-10-08"}, "reversed_range"),
        ({"day_to": "2026-10-04"}, "reversed_range"),
        ({"day_from": "2026-10-25"}, "beyond_window"),
        ({"day_from": "9999-12-31"}, "beyond_window"),
        ({"day_from": "08/10/2026"}, "bad_day_format"),
        ({"day_to": "20261008"}, "bad_day_format"),
        ({"day_from": "amanhã"}, "bad_day_format"),
        ({"day_from": "x" * 5000}, "bad_day_format"),
    ],
)
async def test_an_unusable_day_range_is_an_error_dict_and_never_reads_the_agenda(
    clinic, log, args, reason
):
    clinic.add("Dra. Única")
    agenda = _Agenda()
    with _turn(clinic.tenant_id, calendar=agenda):
        result = await _ask(**args)
    assert set(result) == {"error"}
    assert [f["reason"] for f in log.named("agent_tool_blocked")] == [reason]
    assert (agenda.day_scans, agenda.slot_reads) == (0, [])
    for sent in args.values():
        assert sent not in result["error"]


async def test_a_365_day_range_is_clamped_and_the_answer_says_so(clinic):
    clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=_Agenda()):
        result = await _ask(day_from="2026-10-05", day_to="2027-10-05")
    assert (result["day_from"], result["day_to"]) == ("2026-10-05", "2026-10-18")
    assert result["clamped"] == ["max_days"]
    assert "14 dias" in result["note"] and "05/10" in result["note"] and "18/10" in result["note"]


async def test_the_range_stops_where_booking_stops(clinic):
    clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=_Agenda()):
        result = await _ask(day_from="2026-10-20", day_to="2026-11-30")
    assert result["day_to"] == "2026-10-24"  # today + 19: the last day the flow accepts
    assert result["clamped"] == ["booking_window"]
    assert "24/10" in result["note"]


async def test_more_than_30_windows_are_cut_and_reported(clinic):
    clinic.add("Dra. Única")
    free = {
        date(2026, 10, 5) + timedelta(days=i): ["08:00", "10:00", "12:00"] for i in range(14)
    }  # 42 separate windows
    with _turn(clinic.tenant_id, calendar=_Agenda(free=free)):
        result = await _ask()
    assert len(result["windows"]) == 30
    assert result["clamped"] == ["max_windows"]
    assert result["day_to"] == result["windows"][-1]["day"] == "2026-10-14"
    assert "30" in result["note"] and "14/10" in result["note"]


# --------------------------------------------------------------------------
# Failures: an outage keeps today's behaviour, anything else is an error dict
# --------------------------------------------------------------------------


@pytest.mark.parametrize("error", [CalendarUnavailableError("down"), GoogleTokenRevokedError("x")])
async def test_a_calendar_outage_propagates_like_every_other_calendar_read(clinic, error):
    """graph.run_agent maps it to the sentinel: a person is called in and the owner alerted."""
    clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=_Agenda(error=error)):
        with pytest.raises(CalendarUnavailableError):
            await _ask()


@pytest.mark.parametrize("error", [RuntimeError("Google credentials missing"), KeyError("boom")])
async def test_any_other_failure_is_an_error_dict_not_a_crash(clinic, log, error):
    clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=_Agenda(error=error)):
        result = await _ask()
    assert set(result) == {"error"}
    assert "NUNCA invente horários" in result["error"]
    (failed,) = log.named("agent_availability_failed")
    assert failed == {"error_type": type(error).__name__}  # the type, never the message
    assert log.named("ai_availability_read") == []


async def test_a_roster_that_cannot_be_read_is_an_error_dict(clinic, monkeypatch):
    async def _broken(_tenant_id):
        raise ConnectionError("db down")

    monkeypatch.setattr(mp, "_active_professionals", _broken)
    with _turn(clinic.tenant_id, calendar=_Agenda()):
        result = await _ask()
    assert set(result) == {"error"}


async def test_without_a_clinic_there_is_nothing_to_read(log):
    result = await _ask()
    assert set(result) == {"error"}
    assert [f["reason"] for f in log.named("agent_tool_blocked")] == ["no_clinic"]


async def test_without_an_agenda_in_context_the_environment_calendar_is_never_used(clinic, log):
    clinic.add("Dra. Única")
    with _turn(clinic.tenant_id, calendar=None):
        result = await _ask()
    assert set(result) == {"error"}
    assert [f["reason"] for f in log.named("agent_tool_blocked")] == ["no_calendar"]


async def test_a_multi_professional_turn_without_a_tenant_config_is_refused(clinic, log):
    clinic.add("Dra. Ana")
    clinic.add("Dr. Beto")
    with _turn(clinic.tenant_id, calendar=_Agenda(), config=None):
        result = await _ask(professional="Dra. Ana")
    assert set(result) == {"error"}
    assert [f["reason"] for f in log.named("agent_tool_blocked")] == ["no_calendar"]
    assert clinic.calendars_built == []


async def test_a_config_of_another_tenant_is_refused_before_anything_is_read(clinic, log):
    clinic.add("Dra. Única")
    agenda = _Agenda()
    with _turn(clinic.tenant_id, calendar=agenda, config=_config(uuid4())):
        result = await _ask()
    assert set(result) == {"error"}
    assert [f["reason"] for f in log.named("agent_tool_blocked")] == ["tenant_mismatch"]
    assert agenda.day_scans == 0


# --------------------------------------------------------------------------
# The log line: counts only
# --------------------------------------------------------------------------


async def test_one_count_only_event_per_successful_read(clinic, log):
    clinic.add("Dra. Única", _CONSULTA)
    conversation_id = uuid4()
    with _turn(clinic.tenant_id, calendar=_Agenda(), conversation_id=conversation_id):
        await _ask(service="Consulta", day_from="2026-10-06", day_to="2026-10-12")
    (event,) = log.named("ai_availability_read")
    assert event == {
        "tenant_id": str(clinic.tenant_id),
        "conversation_id": str(conversation_id),
        "windows": 3,
        "days_scanned": 7,
        "clamped": False,
        "professional_resolved": True,
    }
    rendered = repr(log.events)
    for value in ("Consulta", "Dra. Única", "2026-10-06", "08:00"):
        assert value not in rendered


# --------------------------------------------------------------------------
# Tenants, holds and the per-professional agenda, against a real database
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


class _CalendarPerId:
    """Stands in for `CalendarService` in ai/tools.py: each calendar id has its own agenda."""

    agendas: dict[str, dict[date, list[str]]] = {}
    built: list[str] = []

    @classmethod
    def from_tenant_config(cls, config):
        return cls._make(config.google_calendar_id)

    @classmethod
    def for_professional(cls, tenant_config, *, google_calendar_id=None, **_ignored):
        return cls._make(google_calendar_id or tenant_config.google_calendar_id)

    @classmethod
    def _make(cls, calendar_id):
        cls.built.append(calendar_id)
        agenda = _Agenda(free=cls.agendas.get(calendar_id, {}))
        agenda.calendar_id = calendar_id
        return agenda


@pytest.fixture
def real_roster(monkeypatch, db):
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(booking_hold_service, "async_session_factory", db)
    monkeypatch.setattr(ai_tools, "CalendarService", _CalendarPerId)
    _CalendarPerId.agendas = {}
    _CalendarPerId.built = []
    return db


async def _seed_tenant(db, *doctors):
    """A tenant with `doctors` as ("name", "calendar id") active professionals."""
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            appointment_types=[_CONSULTA],
        )
        session.add(tenant)
        await session.flush()
        rows = [
            Professional(tenant_id=tenant.id, name=name, google_calendar_id=cal, is_active=True)
            for name, cal in doctors
        ]
        session.add_all(rows)
        await session.commit()
        for row in rows:
            await session.refresh(row)
        return tenant, rows


async def test_two_tenants_with_a_same_named_doctor_each_read_their_own_agenda(real_roster):
    tenant_a, _ = await _seed_tenant(real_roster, ("Dra. Ana", "ana-A"), ("Dr. Beto", "beto-A"))
    tenant_b, _ = await _seed_tenant(real_roster, ("Dra. Ana", "ana-B"), ("Dr. Caio", "caio-B"))
    _CalendarPerId.agendas = {
        "ana-A": {TUESDAY: ["08:00"]},
        "ana-B": {TUESDAY: ["16:00"]},
    }

    async def _turn_for(tenant):
        with _turn(tenant.id, config=_config(tenant.id)):
            return await _ask(professional="Dra. Ana", day_from="2026-10-06", day_to="2026-10-06")

    a, b = await asyncio.gather(_turn_for(tenant_a), _turn_for(tenant_b))
    assert [w["start"] for w in a["windows"]] == ["08:00"]
    assert [w["start"] for w in b["windows"]] == ["16:00"]
    assert sorted(_CalendarPerId.built) == ["ana-A", "ana-B"]


async def test_a_doctor_the_tenant_does_not_have_is_unknown_and_no_agenda_is_built(real_roster):
    tenant_a, _ = await _seed_tenant(real_roster, ("Dra. Ana", "ana-A"), ("Dr. Beto", "beto-A"))
    await _seed_tenant(real_roster, ("Dra. Carla", "carla-B"), ("Dr. Caio", "caio-B"))
    _CalendarPerId.agendas = {"carla-B": {TUESDAY: ["08:00"]}}
    with _turn(tenant_a.id, config=_config(tenant_a.id)):
        result = await _ask(professional="Dra. Carla")
    assert set(result) == {"error"}
    assert "Dra. Ana" in result["error"] and "Carla" not in result["error"]
    assert _CalendarPerId.built == []


async def test_the_per_professional_agenda_comes_from_the_workflows_own_resolution(real_roster):
    tenant, _ = await _seed_tenant(real_roster, ("Dra. Ana", "ana-cal"), ("Dr. Beto", None))
    _CalendarPerId.agendas = {"ana-cal": {TUESDAY: ["08:00"]}, "tenant-cal": {TUESDAY: ["11:00"]}}
    with _turn(tenant.id, config=_config(tenant.id)):
        own = await _ask(professional="Dra. Ana", day_from="2026-10-06", day_to="2026-10-06")
        fallback = await _ask(professional="Dr. Beto", day_from="2026-10-06", day_to="2026-10-06")
    assert [w["start"] for w in own["windows"]] == ["08:00"]  # her own calendar id
    assert [w["start"] for w in fallback["windows"]] == ["11:00"]  # falls back to the tenant's


async def _hold(tenant, professional_id, start_hour, minutes=30, conversation_id=None):
    """Hold `start_hour`:00 clinic time on Tuesday. Stored as UTC: SQLite drops the offset of a
    timestamptz and `booking_hold._aware` reads it back as UTC, so a UTC instant round-trips."""
    start = datetime(2026, 10, 6, start_hour, 0, tzinfo=TZ).astimezone(UTC)
    placed = await booking_hold_service.place_hold(
        tenant_id=tenant.id,
        conversation_id=conversation_id or uuid4(),
        patient_id=None,
        professional_id=professional_id,
        appointment_type="Consulta",
        insurance=None,
        start_at=start,
        end_at=start + timedelta(minutes=minutes),
    )
    assert placed is not None


async def test_a_slot_another_conversation_is_holding_is_not_offered(real_roster):
    tenant, (sole,) = await _seed_tenant(real_roster, ("Dra. Única", "sole-cal"))
    await _hold(tenant, sole.id, 10)  # holds are PLACED with the sole professional's id
    agenda = _Agenda({TUESDAY: ["09:00", "09:30", "10:00", "10:30"]})
    with _turn(tenant.id, calendar=agenda, config=_config(tenant.id)):
        result = await _ask(day_from="2026-10-06", day_to="2026-10-06")
    assert result["windows"] == [
        {"day": "2026-10-06", "start": "09:00", "end": "10:00"},
        {"day": "2026-10-06", "start": "10:30", "end": "11:00"},
    ]


async def test_the_patients_own_hold_does_not_hide_the_slot_from_them(real_roster):
    tenant, (sole,) = await _seed_tenant(real_roster, ("Dra. Única", "sole-cal"))
    mine = uuid4()
    await _hold(tenant, sole.id, 10, conversation_id=mine)
    with _turn(tenant.id, calendar=_Agenda({TUESDAY: ["10:00"]}), conversation_id=mine):
        result = await _ask(day_from="2026-10-06", day_to="2026-10-06")
    assert [w["start"] for w in result["windows"]] == ["10:00"]


async def test_a_hold_on_another_doctor_hides_nothing_on_this_doctors_agenda(real_roster):
    tenant, (ana, beto) = await _seed_tenant(
        real_roster, ("Dra. Ana", "ana-cal"), ("Dr. Beto", "beto-cal")
    )
    await _hold(tenant, beto.id, 10)
    _CalendarPerId.agendas = {"ana-cal": {TUESDAY: ["10:00"]}, "beto-cal": {TUESDAY: ["10:00"]}}
    with _turn(tenant.id, config=_config(tenant.id)):
        hers = await _ask(professional="Dra. Ana", day_from="2026-10-06", day_to="2026-10-06")
        his = await _ask(professional="Dr. Beto", day_from="2026-10-06", day_to="2026-10-06")
    assert [w["start"] for w in hers["windows"]] == ["10:00"]
    assert his["windows"] == []  # the positive control: the hold is read where it belongs


async def test_another_tenants_tenant_level_hold_hides_nothing(real_roster):
    held, _ = await _seed_tenant(real_roster)  # no professionals: the tenant-level agenda
    other, _ = await _seed_tenant(real_roster)
    await _hold(held, None, 10)
    free = {TUESDAY: ["10:00"]}
    with _turn(held.id, calendar=_Agenda(free)):
        mine = await _ask(day_from="2026-10-06", day_to="2026-10-06")
    with _turn(other.id, calendar=_Agenda(free)):
        theirs = await _ask(day_from="2026-10-06", day_to="2026-10-06")
    assert mine["windows"] == []
    assert [w["start"] for w in theirs["windows"]] == ["10:00"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_get_availability_tool.py -q`
Expected: erro de coleta — `ImportError: cannot import name 'availability_tool' from 'secretaria.ai'`.

- [ ] **Step 3: Write the tool**

Criar `src/secretaria/ai/availability_tool.py`:

```python
"""`get_availability`: the AI's one agenda read - free WINDOWS, never events (TASK-030 P4).

Spec §4.6. On the v2 toolset (`flow_router.ai_draft_v2_enabled`) the AI loses
`check_availability`, `list_free_slots` and every tool that writes to the agenda
(ai/tools.py `AI_TOOLSET_V2_WITHHELD`, `AI_TOOLSET_V2_STAGING`; its `create_event` /
`cancel_event` there only stage the confirmation card, ai/staging_tools.py). What it keeps,
to answer "tem horário semana que vem?", is this tool, and the answer has exactly one shape:

    {"windows": [{"day": "2026-10-08", "start": "08:00", "end": "11:20"}, ...],
     "timezone": "America/Sao_Paulo", "slot_minutes": 40,
     "day_from": "2026-10-05", "day_to": "2026-10-18"}      (+ `professional`, `clamped`, `note`)

The calendar's events never get this far: the free windows are computed by
`services/availability_windows.py` from `services/availability.py` (the same "free" the day
and slot pickers and the draft resolver use: business hours, busy events and held slots
already subtracted), and neither of them ever returns an event - no title, attendee, id,
description, link or busy block exists in this module's data. tests/test_get_availability_tool.py
serializes the answer and fails on any key outside the documented set.

Tenant isolation: the roster, the catalog, the agenda and the holds are all read for
`_tenant_id_ctx` - the tenant of THIS turn - and the agenda is built by the very resolution
the workflow uses (`plugins.multi_professional._professional_calendar` ->
`services/tenant_config.py::resolve_professional_calendar`, the only place a Google refresh
token is decrypted; a single-professional clinic reads the agenda `run_agent` built from the
same config). The environment-variable calendar `ai/tools.py::_get_calendar` falls back to
for dev scripts is deliberately NOT used here.

Failure policy (decided, tested): a calendar outage (`CalendarUnavailableError`, a revoked
token included) propagates, exactly as `check_availability` and `list_free_slots` always
did - graph.run_agent maps it to the CALENDAR_UNAVAILABLE sentinel, the worker hands the
conversation to a person and alerts the clinic owner, and a revoked token must keep
reaching that owner. Anything else that goes wrong is an error dict the model can read
(and a count-only log line), never a crash of the turn.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from langchain_core.tools import tool

from secretaria.ai.tools import (
    _calendar_ctx,
    _conversation_id_ctx,
    _effective_service_catalog,
    _tenant_config_ctx,
    _tenant_id_ctx,
)
from secretaria.core.logging import get_logger
from secretaria.services.availability_windows import (
    CLAMP_BOOKING_WINDOW,
    CLAMP_MAX_DAYS,
    CLAMP_MAX_WINDOWS,
    MAX_DAYS,
    MAX_WINDOWS,
    DayRange,
    RangeError,
    WindowScan,
    resolve_range,
    scan_free_windows,
)
from secretaria.services.booking_hold import BookingGate
from secretaria.services.booking_scope import (
    BOOKING_TOPOLOGY_MULTI,
    BOOKING_TOPOLOGY_SOLE,
    booking_topology,
    canonical_service_name,
    resolve_booking_owner_id,
    service_entry_name,
    service_names,
)
from secretaria.services.calendar import CalendarService, CalendarUnavailableError
from secretaria.services.flow_router import DAY_PICKER_WINDOW_DAYS

logger = get_logger(__name__)

TOOL_NAME = "get_availability"

# Why the tool refused (stable enums for `agent_tool_blocked`; never the arguments, which
# carry the patient's own words). The date reasons are availability_windows.RANGE_*.
BLOCK_NO_CLINIC = "no_clinic"
BLOCK_TENANT_MISMATCH = "tenant_mismatch"
BLOCK_NO_CALENDAR = "no_calendar"
BLOCK_NO_PROFESSIONALS = "no_professionals"
BLOCK_PROFESSIONAL_REQUIRED = "professional_required"
BLOCK_PROFESSIONAL_UNKNOWN = "professional_unknown"
BLOCK_PROFESSIONAL_AMBIGUOUS = "professional_ambiguous"
BLOCK_SERVICE_UNKNOWN = "service_unknown"
BLOCK_NO_PROFESSIONAL_OFFERS_SERVICE = "no_professional_offers_service"
BLOCK_SEVERAL_PROFESSIONALS_OFFER_SERVICE = "several_professionals_offer_service"

_TEMPORARY_ERROR = (
    "Não consegui consultar a agenda agora. Diga ao paciente que vai olhar de novo em instantes "
    "e NUNCA invente horários."
)


class _Refusal(Exception):
    """A request the tool cannot serve. `message` is what the model reads."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message


@dataclass
class _Target:
    """Whose agenda is read, with what, for how long."""

    calendar: CalendarService
    professional: Any | None  # the roster row; None on a clinic without professionals
    owner_id: UUID | None  # whose holds hide slots: the agenda a booking would land on
    slot_minutes: int


def _clinic_today(tz) -> date:
    """The clinic's date. Its own function so a test can pin the clock.

    The CLINIC's, not the server's: at 22:30 in America/Sao_Paulo the UTC date is already
    tomorrow.
    """
    return datetime.now(tz).date()


def _names(items: list) -> str:
    return ", ".join(str(getattr(item, "name", item)) for item in items)


def _entry_minutes(entry: Any) -> int | None:
    """A catalog entry's duration, whichever shape it travels in; None when unusable."""
    raw = (
        entry.get("duration_min")
        if isinstance(entry, dict)
        else getattr(entry, "duration_min", None)
    )
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        return None
    return minutes if minutes > 0 else None


async def _pick_professional(tenant_id: UUID, roster: list, name: str, service: str):
    """The roster row on a multi-professional clinic, plus THEIR catalog.

    Resolved like the draft does (`set_booking_draft`): by exact name, case-insensitive; or,
    with no name, the one professional who offers the service. Several, none, or no hint at
    all is a refusal listing the valid names - never a guess.
    """
    # Lazy: plugins import ai.tools, so the reverse import cannot be top-level.
    from secretaria.plugins.multi_professional import _professional_services

    if name:
        matches = [p for p in roster if p.name.strip().casefold() == name.casefold()]
        if not matches:
            raise _Refusal(
                BLOCK_PROFESSIONAL_UNKNOWN,
                f"Profissional não encontrado. Profissionais disponíveis: {_names(roster)}.",
            )
        if len(matches) > 1:
            raise _Refusal(
                BLOCK_PROFESSIONAL_AMBIGUOUS,
                "Mais de um profissional tem esse nome. Peça uma escolha inequívoca.",
            )
        return matches[0], await _professional_services(tenant_id, matches[0])
    if not service:
        raise _Refusal(
            BLOCK_PROFESSIONAL_REQUIRED,
            f"Esta clínica tem vários profissionais ({_names(roster)}). "
            "Informe `professional` ou `service`.",
        )
    offering = []
    for candidate in roster:
        offered = await _professional_services(tenant_id, candidate)
        if canonical_service_name(offered, service) is not None:
            offering.append((candidate, offered))
    if not offering:
        raise _Refusal(
            BLOCK_NO_PROFESSIONAL_OFFERS_SERVICE,
            "Nenhum profissional da clínica oferece esse serviço.",
        )
    if len(offering) > 1:
        raise _Refusal(
            BLOCK_SEVERAL_PROFESSIONALS_OFFER_SERVICE,
            f"Mais de um profissional atende esse serviço: {_names([p for p, _ in offering])}. "
            "Pergunte com quem o paciente prefere e chame de novo com `professional`.",
        )
    return offering[0]


async def _resolve_target(tenant_id: UUID, professional: str, service: str) -> _Target:
    """Roster -> professional -> catalog -> service duration -> agenda, all for `tenant_id`."""
    # Lazy, same reason as above.
    from secretaria.plugins import multi_professional as mp

    config = _tenant_config_ctx.get()
    if config is not None and config.tenant_id != tenant_id:
        raise _Refusal(BLOCK_TENANT_MISMATCH, _TEMPORARY_ERROR)

    roster = await mp._active_professionals(tenant_id)
    topology = booking_topology(roster)
    chosen: Any | None = None
    if topology == BOOKING_TOPOLOGY_MULTI:
        chosen, catalog = await _pick_professional(tenant_id, roster, professional, service)
    elif topology == BOOKING_TOPOLOGY_SOLE:
        chosen = roster[0]
        if professional and chosen.name.strip().casefold() != professional.casefold():
            raise _Refusal(
                BLOCK_PROFESSIONAL_UNKNOWN,
                f"Profissional não encontrado. Esta clínica atende com: {chosen.name}.",
            )
        catalog = await mp._professional_services(tenant_id, chosen)
    else:
        if professional:
            raise _Refusal(
                BLOCK_NO_PROFESSIONALS,
                "Esta clínica não tem profissionais cadastrados: não informe `professional`.",
            )
        catalog = _effective_service_catalog()

    minutes: int | None = None
    if service:
        canonical = canonical_service_name(catalog, service)
        if canonical is None:
            raise _Refusal(
                BLOCK_SERVICE_UNKNOWN,
                "Esse serviço não existe nesta agenda. Serviços disponíveis: "
                f"{', '.join(service_names(catalog)) or 'nenhum serviço cadastrado'}.",
            )
        entry = next(e for e in catalog if service_entry_name(e) == canonical)
        minutes = _entry_minutes(entry)

    if topology == BOOKING_TOPOLOGY_MULTI:
        if config is None:
            # Never the environment calendar: a multi-professional agenda is only ever built
            # from this turn's own tenant config.
            raise _Refusal(BLOCK_NO_CALENDAR, _TEMPORARY_ERROR)
        calendar = await mp._professional_calendar(tenant_id, chosen)
    else:
        calendar = _calendar_ctx.get()
        if calendar is None:
            raise _Refusal(BLOCK_NO_CALENDAR, _TEMPORARY_ERROR)
    return _Target(
        calendar=calendar,
        professional=chosen,
        # The owner a booking would be PLACED with - the same rule every booking surface
        # uses - so the holds hidden here are the ones the confirmation would collide with.
        owner_id=resolve_booking_owner_id(
            roster, chosen.id if topology == BOOKING_TOPOLOGY_MULTI else None
        ),
        slot_minutes=minutes or calendar.default_slot_minutes,
    )


def _fmt(day: date) -> str:
    return day.strftime("%d/%m")


def _note(span: DayRange, scan: WindowScan) -> str | None:
    """The sentences that tell the model what the list does NOT cover. None = nothing to say."""
    parts: list[str] = []
    if CLAMP_MAX_DAYS in span.clamped:
        parts.append(
            f"Consulto no máximo {MAX_DAYS} dias por vez: mostrei de {_fmt(span.first)} a "
            f"{_fmt(span.last)}. Para ver depois disso, chame de novo a partir do dia seguinte."
        )
    elif CLAMP_BOOKING_WINDOW in span.clamped:
        parts.append(
            f"A agenda só abre até {_fmt(span.last)}: mostrei até esse dia, que é o limite para "
            "marcar."
        )
    if scan.truncated:
        covered = _fmt(scan.windows[-1].day)
        parts.append(
            f"Mostrei só os primeiros {MAX_WINDOWS} horários livres (até {covered}); há mais. "
            "Peça ao paciente um dia ou período mais curto para ver o resto."
        )
    if not scan.windows:
        parts.append(f"Nenhum horário livre de {_fmt(span.first)} a {_fmt(span.last)}.")
    return " ".join(parts) or None


async def _read_availability(
    tenant_id: UUID, professional: str, service: str, day_from: str, day_to: str
) -> dict:
    target = await _resolve_target(tenant_id, professional.strip(), service.strip())
    calendar = target.calendar
    tz = calendar.tzinfo
    try:
        span = resolve_range(
            day_from, day_to, today=_clinic_today(tz), booking_window_days=DAY_PICKER_WINDOW_DAYS
        )
    except RangeError as error:
        raise _Refusal(error.code, str(error)) from None

    # Held slots (a Portal visitor between "Confirmar" and the code) are not free: the same
    # lookup the slot picker makes, through an UNARMED gate - this tool never reserves.
    gate = BookingGate(
        tenant_id=tenant_id,
        conversation_id=_conversation_id_ctx.get() or uuid4(),
        patient_id=None,
        external_id=None,
        armed=False,
    )
    holds = await gate.busy_windows(target.owner_id)
    scan = await scan_free_windows(
        calendar, span=span, duration_minutes=target.slot_minutes, holds=holds
    )

    clamped = [*span.clamped, *([CLAMP_MAX_WINDOWS] if scan.truncated else [])]
    last = scan.windows[-1].day if scan.truncated else span.last
    result: dict[str, Any] = {
        "windows": [window.payload() for window in scan.windows],
        "timezone": getattr(tz, "key", None) or str(tz),
        "slot_minutes": target.slot_minutes,
        "day_from": span.first.isoformat(),
        "day_to": last.isoformat(),
    }
    if target.professional is not None:
        result["professional"] = target.professional.name
    if clamped:
        result["clamped"] = clamped
    note = _note(span, scan)
    if note:
        result["note"] = note
    logger.info(
        "ai_availability_read",
        tenant_id=str(tenant_id),
        conversation_id=str(_conversation_id_ctx.get() or ""),
        windows=len(scan.windows),
        days_scanned=span.days,
        clamped=bool(clamped),
        professional_resolved=target.professional is not None,
    )
    return result


@tool
async def get_availability(
    professional: str = "",
    service: str = "",
    day_from: str = "",
    day_to: str = "",
) -> dict:
    """Consulta os HORÁRIOS LIVRES da agenda de um profissional - só isso. Devolve janelas
    {day, start, end} no fuso da clínica (campo `timezone`), já sem os horários que outros
    pacientes estão reservando. Nunca devolve compromissos, nomes nem horários ocupados: o
    que não está na lista não está livre. Use para responder "tem horário?" ou "tem vaga
    semana que vem?" e para sugerir opções. Esta ferramenta só lê: se o paciente quer marcar
    um horário da lista, chame create_event com esse início (ou set_booking_draft com `day`
    e `time`) - elas só preparam o cartão de confirmação, e o fluxo confere e conduz.

    Cada janela é um trecho contínuo em que cabe uma consulta; `slot_minutes` é a duração
    usada. Mostra no máximo 14 dias e 30 janelas por chamada. Se a resposta trouxer
    `clamped`, a lista NÃO cobre tudo o que foi pedido: diga isso ao paciente e peça um dia
    ou período menor. `note` resume em uma frase o que a lista cobre: respeite-a. Sem
    janelas, diga que não há horário livre nesse período - NUNCA invente um horário nem
    prometa um que não esteja na lista.

    Args:
        professional: Nome do profissional (ou vazio). Em clínica com vários profissionais,
            informe o profissional OU o serviço (se só um profissional oferece o serviço, ele
            é usado).
        service: Nome EXATO de um serviço da clínica (ou vazio: usa a duração padrão do
            profissional, veja `slot_minutes`).
        day_from: Primeiro dia, no formato AAAA-MM-DD, no fuso da clínica (vazio = hoje).
        day_to: Último dia, no formato AAAA-MM-DD (vazio = 14 dias a partir de day_from).
    """
    tenant_id = _tenant_id_ctx.get()
    try:
        if tenant_id is None:
            raise _Refusal(BLOCK_NO_CLINIC, "Nenhuma clínica configurada para esta conversa.")
        return await _read_availability(
            tenant_id, professional or "", service or "", day_from or "", day_to or ""
        )
    except _Refusal as refusal:
        logger.info("agent_tool_blocked", tool=TOOL_NAME, reason=refusal.reason)
        return {"error": refusal.message}
    except CalendarUnavailableError:
        # Today's behaviour for every calendar read: run_agent maps it to the sentinel that
        # hands the conversation to a person and alerts the owner (module docstring).
        raise
    except Exception as exc:
        logger.warning("agent_availability_failed", error_type=type(exc).__name__)
        return {"error": _TEMPORARY_ERROR}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_get_availability_tool.py -q`
Expected: PASS (57 testes, contando os parametrizados). Se algum teste de reserva falhar com o horário "ainda livre", confira que o `_hold` do teste grava em UTC (o SQLite descarta o deslocamento; `booking_hold._aware` lê de volta como UTC).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format src/secretaria/ai/availability_tool.py tests/test_get_availability_tool.py
uvx ruff check src/secretaria/ai/availability_tool.py tests/test_get_availability_tool.py
git add src/secretaria/ai/availability_tool.py tests/test_get_availability_tool.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(ai): get_availability, the AI's only agenda read - free windows, never events

Resolves the professional and service the way the draft does (refusing, never guessing),
reads the agenda through the workflow's own per-tenant/per-professional resolution, subtracts
held slots, and returns only {day, start, end} windows in the clinic's time zone: at most 14
days, 30 windows, never past the booking window, with the clamp reported. A calendar outage
propagates exactly as every other calendar read does (hand-over + owner alert); any other
failure is an error dict. One count-only ai_availability_read log line per read.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 6: `ai/staging_tools.py` — `create_event` e `cancel_event` cegos (só preparam o cartão)

**Files:**
- Create: `src/secretaria/ai/staging_tools.py`
- Test: `tests/test_blind_staging_tools.py`

**Interfaces:**
- Consumes: Task 4 (`BLIND_STAGING_VARIANT`); P2b (`BookingDraftRequested(appointment_type, professional_id, insurance, *, attendee=None, day=None, time=None)`, `_draft_professional_id(tenant_id, name) -> UUID | None`, `set_booking_draft_v2`, `graph._tool_cache_key`, `graph.BOOKING_DRAFT_SENTINEL_PREFIX` + `BookingDraft.to_payload`); P3 (`ManageAppointmentRequested(action, *, appointment=None, day=None, time=None)`, `TOOL_BLOCK_BAD_APPOINTMENT`, `manage_existing_appointment_v2`, `services/manage_request.py::{ACTION_CANCEL, APPOINTMENT_REF_FORMAT, appointment_ref, parse_appointment_ref}`, `graph.MANAGE_APPOINTMENT_SENTINEL_PREFIX` + `ManageRequest.to_payload`); hoje: `services/patient_context.py::load_upcoming_appointments`, `_tenant_id_ctx`, `_conversation_id_ctx`, `_tenant_config_ctx`.
- Produces: `create_event_v2` (nome `create_event`; args `start: str`, `service: str = ""`, `professional: str = ""`), `cancel_event_v2` (nome `cancel_event`; arg `appointment: str`), ambos com `metadata = {"cache_variant": BLIND_STAGING_VARIANT}` e só `{"error": ...}` como resposta; `TOOL_BLOCK_NO_CLINIC`, `TOOL_BLOCK_BAD_START`, `TOOL_BLOCK_NO_PATIENT`, `TOOL_BLOCK_UNKNOWN_APPOINTMENT`, `TOOL_BLOCK_AMBIGUOUS_APPOINTMENT`; evento `agent_cancel_staging_failed(error_type)`. A Task 7 as entrega no v2; a Task 8 as declara "só erro".

Por que um módulo novo e não `ai/tools.py`: as cegas não compartilham nada com as legadas além do nome; ficam ao lado de `ai/availability_tool.py`, importando as exceções de devolução de `ai/tools.py` (que já carrega P2b/P3 e passa de 1.400 linhas). Nada aqui escreve na agenda, lê evento ou chama o Google — os testes deixam um calendário que explode se for tocado.

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_blind_staging_tools.py`:

```python
"""`create_event` / `cancel_event` v2: blind tools that only stage the card (TASK-030 P4).

Owner's decision (2026-10-03): the AI keeps tools with these names, but on the v2 toolset
they never write to the agenda, never read an event, never take a title/description/end/
name/event id, and only hand back to the flow the very request the workflow's own hand-back
tools send - so the patient lands on the details + confirmation card (create) or on the
"Confirmar o cancelamento?" card (cancel), and the patient's tap is what books or cancels.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from contextlib import contextmanager  # noqa: E402
from datetime import UTC, date, datetime, time, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai import (  # noqa: E402
    graph,
    staging_tools as st,
    tools as ai_tools,
)
from secretaria.ai.staging_tools import cancel_event_v2, create_event_v2  # noqa: E402
from secretaria.ai.tools import (  # noqa: E402
    BookingDraftRequested,
    ManageAppointmentRequested,
    manage_existing_appointment_v2,
    set_booking_draft_v2,
)
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    Patient,
    Tenant,
)
from secretaria.plugins import multi_professional as mp  # noqa: E402
from secretaria.services import pii_pseudonymization as store  # noqa: E402
from secretaria.services.booking_scope import BOOKING_TOPOLOGY_SOLE  # noqa: E402
from secretaria.services.manage_request import appointment_ref  # noqa: E402
from secretaria.services.tenant_config import TenantRuntimeConfig  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
CANARY = "CANARY-9d41"
ANA = SimpleNamespace(id=uuid4(), name="Dra. Ana")


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log

    def blocked(self):
        return [fields for event, fields in self.events if event == "agent_tool_blocked"]


@pytest.fixture
def log(monkeypatch):
    recorder = _Log()
    monkeypatch.setattr(st, "logger", recorder)
    return recorder


class _ExplodingCalendar:
    """Any use is a bug: a staging tool never reaches the agenda."""

    def __getattr__(self, name):
        raise AssertionError(f"calendar must not be touched (called {name})")


def _config(tenant_id, timezone="America/Sao_Paulo"):
    return SimpleNamespace(tenant_id=tenant_id, timezone=timezone)


@contextmanager
def _turn(tenant_id, conversation_id=None, *, timezone="America/Sao_Paulo"):
    pairs = [
        (ai_tools._tenant_id_ctx, tenant_id),
        (ai_tools._conversation_id_ctx, conversation_id),
        (ai_tools._calendar_ctx, _ExplodingCalendar()),
        (ai_tools._tenant_config_ctx, _config(tenant_id, timezone)),
    ]
    tokens = [(var, var.set(value)) for var, value in pairs]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


# --------------------------------------------------------------------------
# The model-facing contract: names, arguments, no free text that reaches the agenda
# --------------------------------------------------------------------------


def test_the_names_are_the_legacy_ones_and_the_arguments_carry_no_free_text():
    assert create_event_v2.name == ai_tools.create_event.name == "create_event"
    assert cancel_event_v2.name == ai_tools.cancel_event.name == "cancel_event"
    assert set(create_event_v2.args) == {"start", "service", "professional"}
    assert set(cancel_event_v2.args) == {"appointment"}
    for tool in (create_event_v2, cancel_event_v2):
        assert tool.metadata == {"cache_variant": ai_tools.BLIND_STAGING_VARIANT}
        assert "NÃO" in tool.description
    assert "Confirmar" in create_event_v2.description
    assert "nunca diga que foi cancelada" in cancel_event_v2.description
    assert "Nunca um id de evento" in cancel_event_v2.description


def test_the_compiled_agent_cache_tells_them_apart_from_the_legacy_tools():
    assert graph._tool_cache_key(create_event_v2) == "create_event#blind_v2"
    assert graph._tool_cache_key(cancel_event_v2) == "cancel_event#blind_v2"
    assert graph._tool_cache_key(ai_tools.create_event) == "create_event"
    assert graph._tool_cache_key(ai_tools.cancel_event) == "cancel_event"


# --------------------------------------------------------------------------
# create_event v2: the draft, never the agenda
# --------------------------------------------------------------------------


@pytest.fixture
def roster(monkeypatch):
    async def _active(_tenant_id):
        return [ANA]

    monkeypatch.setattr(mp, "_active_professionals", _active)


async def test_create_hands_back_the_draft_and_never_touches_the_agenda(roster):
    with _turn(uuid4()), pytest.raises(BookingDraftRequested) as caught:
        await create_event_v2.ainvoke(
            {"start": "2026-10-08T10:00", "service": " Consulta ", "professional": "dra. ana"}
        )
    exc = caught.value
    assert (exc.appointment_type, exc.professional_id, exc.insurance) == ("Consulta", ANA.id, None)
    assert (exc.day, exc.time) == (date(2026, 10, 8), time(10, 0))
    # "Pra quem" stays unknown: the resolver uses the recorded answer or asks.
    assert exc.attendee is None


@pytest.mark.parametrize(
    "start",
    ["2026-10-08T10:00", "2026-10-08 10:00", "2026-10-08T10:00:00", " 2026-10-08T10:00 "],
)
async def test_the_start_is_read_as_the_clinics_wall_clock(roster, start):
    with _turn(uuid4()), pytest.raises(BookingDraftRequested) as caught:
        await create_event_v2.ainvoke({"start": start})
    assert (caught.value.day, caught.value.time) == (date(2026, 10, 8), time(10, 0))
    assert (caught.value.appointment_type, caught.value.professional_id) == (None, None)


@pytest.mark.parametrize(
    "start",
    [
        "",
        "2026-10-08",
        "10:00",
        "2026-10-08T10:00:30",
        "2026-10-08T10:00-03:00",
        "2026-10-08T10:00Z",
        "2026-02-30T10:00",
        "2026-10-08T25:00",
        "08/10/2026 10:00",
        "quinta às 10h",
        "٢٠٢٦-١٠-٠٨T10:00",
        f"Consulta - {CANARY} Silva",
        "x" * 5000,
    ],
)
async def test_an_unusable_start_is_a_short_error_that_echoes_nothing(roster, log, start):
    with _turn(uuid4()):
        result = await create_event_v2.ainvoke({"start": start})
    assert set(result) == {"error"}
    assert "AAAA-MM-DDTHH:MM" in result["error"]
    assert CANARY not in result["error"] and CANARY not in repr(log.events)
    (fields,) = log.blocked()
    assert fields == {"tool": "create_event", "reason": "bad_start"}


async def test_a_title_or_a_name_the_model_adds_never_reaches_the_draft(roster):
    """Extra arguments are not part of the schema: no title, description or end exists."""
    with _turn(uuid4()), pytest.raises(BookingDraftRequested) as caught:
        await create_event_v2.ainvoke(
            {
                "start": "2026-10-08T10:00",
                "summary": f"Consulta - {CANARY} Silva",
                "description": CANARY,
                "end": "2026-10-08T11:00",
            }
        )
    assert CANARY not in repr(vars(caught.value))


async def test_an_unknown_professional_is_left_for_the_flow_to_ask(roster):
    with _turn(uuid4()), pytest.raises(BookingDraftRequested) as caught:
        await create_event_v2.ainvoke({"start": "2026-10-08T10:00", "professional": "Dr. X"})
    assert caught.value.professional_id is None


async def test_create_without_a_clinic_is_refused(log):
    token = ai_tools._tenant_id_ctx.set(None)
    try:
        result = await create_event_v2.ainvoke({"start": "2026-10-08T10:00"})
    finally:
        ai_tools._tenant_id_ctx.reset(token)
    assert set(result) == {"error"}
    assert log.blocked() == [{"tool": "create_event", "reason": "no_clinic"}]


# --------------------------------------------------------------------------
# cancel_event v2: only this patient's own appointment, only to the cancel card
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(core_database, "async_session_factory", maker)
    yield maker
    await engine.dispose()


def _upcoming(days: int, hour: int) -> datetime:
    """A clinic-local start `days` from now at `hour`:00, as UTC (what the DB stores)."""
    local_day = datetime.now(TZ).date() + timedelta(days=days)
    return datetime(local_day.year, local_day.month, local_day.day, hour, tzinfo=TZ).astimezone(UTC)


async def _patient(db, tenant_id, wa_id, *starts, event_prefix="evt"):
    async with db() as session:
        patient = Patient(tenant_id=tenant_id, wa_id=wa_id, name="Paciente")
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant_id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        for i, start in enumerate(starts):
            session.add(
                Appointment(
                    tenant_id=tenant_id,
                    patient_id=patient.id,
                    conversation_id=conversation.id,
                    google_event_id=f"{event_prefix}-{wa_id}-{i}",
                    appointment_type="Consulta",
                    start_at=start,
                    end_at=start + timedelta(minutes=30),
                    status=AppointmentStatus.SCHEDULED,
                )
            )
        await session.commit()
        return SimpleNamespace(id=patient.id, conversation_id=conversation.id)


async def _tenant(db):
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
        session.add(tenant)
        await session.commit()
        return tenant.id


@pytest_asyncio.fixture
async def world(db):
    """Tenant A: our patient (D+3 10:00, D+5 14:00) and another patient (D+4 09:00).
    Tenant B: a patient with D+3 10:00 too (same minute as ours)."""
    tenant_a, tenant_b = await _tenant(db), await _tenant(db)
    mine = await _patient(db, tenant_a, "5511900000001", _upcoming(3, 10), _upcoming(5, 14))
    other = await _patient(db, tenant_a, "5511900000002", _upcoming(4, 9))
    foreign = await _patient(db, tenant_b, "5511900000003", _upcoming(3, 10))
    return SimpleNamespace(
        tenant_a=tenant_a, tenant_b=tenant_b, mine=mine, other=other, foreign=foreign
    )


def _ref(days: int, hour: int) -> str:
    return appointment_ref(_upcoming(days, hour), TZ)


async def test_cancel_of_an_own_appointment_hands_back_the_cancel_request(db, world):
    with _turn(world.tenant_a, world.mine.conversation_id):
        with pytest.raises(ManageAppointmentRequested) as caught:
            await cancel_event_v2.ainvoke({"appointment": _ref(5, 14)})
    exc = caught.value
    assert exc.action == "cancel"
    assert exc.appointment.strftime("%Y-%m-%d %H:%M") == _ref(5, 14)
    assert (exc.day, exc.time) == (None, None)
    async with db() as session:  # nothing was cancelled by the tool itself
        statuses = set(await session.scalars(select(Appointment.status)))
    assert statuses == {AppointmentStatus.SCHEDULED}


@pytest.mark.parametrize(
    "who, ref_of, reason",
    [
        ("mine", (4, 9), "unknown_appointment"),  # the OTHER patient's slot, same tenant
        ("mine", (6, 10), "unknown_appointment"),  # nobody's
        ("other", (3, 10), "unknown_appointment"),  # ours, asked from their conversation
    ],
    ids=["another_patients_time", "nobodys_time", "from_the_other_conversation"],
)
async def test_an_appointment_that_is_not_the_patients_is_never_targeted(
    db, world, log, who, ref_of, reason
):
    with _turn(world.tenant_a, getattr(world, who).conversation_id):
        result = await cancel_event_v2.ainvoke({"appointment": _ref(*ref_of)})
    assert set(result) == {"error"}
    assert "manage_existing_appointment" in result["error"]
    assert log.blocked() == [{"tool": "cancel_event", "reason": reason}]
    assert _ref(*ref_of) not in repr(log.events) + result["error"]


async def test_another_tenants_appointment_at_the_same_minute_is_not_ours(db, world, log):
    """Tenant B's patient has D+3 10:00 too; from tenant B's id, our conversation finds no patient
    of that tenant - and from tenant A only OUR appointment matches."""
    with _turn(world.tenant_b, world.mine.conversation_id):
        result = await cancel_event_v2.ainvoke({"appointment": _ref(3, 10)})
    assert set(result) == {"error"}
    assert log.blocked() == [{"tool": "cancel_event", "reason": "no_patient"}]


async def test_two_of_the_patients_appointments_at_the_same_minute_are_never_guessed(db, log):
    tenant = await _tenant(db)
    twin = await _patient(db, tenant, "5511900000009", _upcoming(3, 10), _upcoming(3, 10))
    with _turn(tenant, twin.conversation_id):
        result = await cancel_event_v2.ainvoke({"appointment": _ref(3, 10)})
    assert set(result) == {"error"}
    assert log.blocked() == [{"tool": "cancel_event", "reason": "ambiguous_appointment"}]


@pytest.mark.parametrize(
    "appointment",
    ["evt-5511900000001-0", "", "terça às 10h", "2026-10-13", f"{CANARY} Silva", "x" * 5000],
)
async def test_an_event_id_or_free_text_is_not_a_reference(db, world, log, appointment):
    with _turn(world.tenant_a, world.mine.conversation_id):
        result = await cancel_event_v2.ainvoke({"appointment": appointment})
    assert set(result) == {"error"}
    assert "(ref AAAA-MM-DD HH:MM)" in result["error"]
    assert log.blocked() == [{"tool": "cancel_event", "reason": "bad_appointment"}]
    assert CANARY not in repr(log.events) + result["error"]


async def test_the_reference_is_read_in_the_clinics_zone(db):
    """The "(ref ...)" lines are written in the clinic's zone (P3); so is the match."""
    tenant = await _tenant(db)
    start = _upcoming(3, 10)
    patient = await _patient(db, tenant, "5511900000008", start)
    new_york = ZoneInfo("America/New_York")
    with _turn(tenant, patient.conversation_id, timezone="America/New_York"):
        with pytest.raises(ManageAppointmentRequested):
            await cancel_event_v2.ainvoke({"appointment": appointment_ref(start, new_york)})
        result = await cancel_event_v2.ainvoke({"appointment": appointment_ref(start, TZ)})
    assert set(result) == {"error"}  # São Paulo's wall clock is not New York's


@pytest.mark.parametrize("missing", ["tenant", "conversation"])
async def test_cancel_without_a_patient_context_is_refused(db, world, log, missing):
    tenant = None if missing == "tenant" else world.tenant_a
    conversation = None if missing == "conversation" else world.mine.conversation_id
    with _turn(tenant, conversation):
        result = await cancel_event_v2.ainvoke({"appointment": _ref(3, 10)})
    assert set(result) == {"error"}
    assert log.blocked() == [{"tool": "cancel_event", "reason": "no_clinic"}]


async def test_a_database_failure_is_an_error_and_never_a_cancel(world, log, monkeypatch):
    def _broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(core_database, "async_session_factory", _broken)
    with _turn(world.tenant_a, world.mine.conversation_id):
        result = await cancel_event_v2.ainvoke({"appointment": _ref(3, 10)})
    assert set(result) == {"error"}
    assert "NUNCA diga que algo foi cancelado" in result["error"]
    assert [e for e, _ in log.events] == ["agent_cancel_staging_failed"]


# --------------------------------------------------------------------------
# The same hand-back as the workflow's own tools: same sentinel, so the same landing
# --------------------------------------------------------------------------


def _runtime_config(tenant_id) -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
        tenant_id=tenant_id,
        clinic_name="Clinica",
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[],
        business_hours={},
        google_calendar_id="cal",
        google_refresh_token=None,
    )


async def _sentinel(monkeypatch, tool, args, *, tenant_id, conversation_id) -> str:
    """What graph.run_agent hands the worker when the model calls `tool` with `args`."""

    async def _the_model_calls_the_tool(messages):
        return str(await tool.ainvoke(args))

    async def _no_history(_conversation_id):
        return []

    monkeypatch.setattr(graph, "invoke_agent", _the_model_calls_the_tool)
    monkeypatch.setattr(graph, "_load_history", _no_history)
    return await graph.run_agent(
        "oi",
        context={"conversation_id": str(conversation_id)},
        tenant_config=_runtime_config(tenant_id),
        booking_topology=BOOKING_TOPOLOGY_SOLE,
    )


async def test_create_lands_exactly_like_the_draft_with_day_and_time(db, roster, monkeypatch):
    monkeypatch.setattr(store, "async_session_factory", db)
    ids = {"tenant_id": uuid4(), "conversation_id": uuid4()}
    staged = await _sentinel(
        monkeypatch,
        create_event_v2,
        {"start": "2026-10-08T10:00", "service": "Consulta", "professional": "Dra. Ana"},
        **ids,
    )
    drafted = await _sentinel(
        monkeypatch,
        set_booking_draft_v2,
        {"service": "Consulta", "professional": "Dra. Ana", "day": "2026-10-08", "time": "10:00"},
        **ids,
    )
    assert staged.startswith(graph.BOOKING_DRAFT_SENTINEL_PREFIX)
    assert staged == drafted


async def test_cancel_lands_exactly_like_the_manage_request(db, world, monkeypatch):
    monkeypatch.setattr(store, "async_session_factory", db)
    ids = {"tenant_id": world.tenant_a, "conversation_id": world.mine.conversation_id}
    staged = await _sentinel(monkeypatch, cancel_event_v2, {"appointment": _ref(5, 14)}, **ids)
    managed = await _sentinel(
        monkeypatch,
        manage_existing_appointment_v2,
        {"action": "cancel", "appointment": _ref(5, 14)},
        **ids,
    )
    assert staged.startswith(graph.MANAGE_APPOINTMENT_SENTINEL_PREFIX)
    assert staged == managed
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_blind_staging_tools.py -q`
Expected: erro de coleta — `ModuleNotFoundError: No module named 'secretaria.ai.staging_tools'`.

- [ ] **Step 3: Write the tools**

Criar `src/secretaria/ai/staging_tools.py`:

```python
"""`create_event` and `cancel_event` on the v2 toolset: blind tools that only STAGE.

TASK-030 P4, owner's decision of 2026-10-03: the AI keeps tools NAMED `create_event` and
`cancel_event`, but on a v2 turn they are these - and they never write to the agenda, never
read an event and never take a free-text title, description, end, name or Google event id.
Each one builds exactly what the workflow's own hand-back tools build and raises the same
exception, so the patient lands on the same card through the same path:

  * `create_event(start, service, professional)` raises `BookingDraftRequested` with the
    service, the professional, and the day and time taken from `start` - the draft the P2
    resolver lands (workers/shared/sentinels.py::_handle_set_booking_draft). The "pra quem"
    is left unknown here: the resolver uses the answer the conversation already recorded,
    or asks "Essa consulta é pra você?". The card's Confirmar - today's
    `flow_router._handle_confirmation`, with the Portal code gate, the holds and the
    authorization - is the only thing that books; the event title and description are
    composed there, server side, from the stored patient/attendee record.
  * `cancel_event(appointment)` resolves the "(ref AAAA-MM-DD HH:MM)" reference ONLY among
    the upcoming appointments of THIS conversation's patient in THIS turn's tenant, and
    raises `ManageAppointmentRequested("cancel", appointment=...)` - the same request
    `manage_existing_appointment` v2 sends (P3), which stops at the "Confirmar o
    cancelamento?" card. A reference that names none, or two, of the patient's
    appointments is an error dict: nothing is guessed.

What the model reads back is never data: the hand-back ends the turn, and every refusal is
`{"error": "<frase>"}` that never echoes what the model sent (ai/tool_output.py declares
both tools as error-only). The legacy implementations of these names (ai/tools.py) stay for
the switch-off path and refuse by themselves on a v2 turn (`_blocked_by_toolset_v2`).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import tool

from secretaria.ai.tools import (
    BLIND_STAGING_VARIANT,
    TOOL_BLOCK_BAD_APPOINTMENT,
    BookingDraftRequested,
    ManageAppointmentRequested,
    _conversation_id_ctx,
    _draft_professional_id,
    _tenant_config_ctx,
    _tenant_id_ctx,
)
from secretaria.core.logging import get_logger
from secretaria.services.manage_request import (
    ACTION_CANCEL,
    APPOINTMENT_REF_FORMAT,
    appointment_ref,
    parse_appointment_ref,
)

logger = get_logger(__name__)

# Why a staging tool refused (stable enums for `agent_tool_blocked`; never the arguments).
TOOL_BLOCK_NO_CLINIC = "no_clinic"
TOOL_BLOCK_BAD_START = "bad_start"
TOOL_BLOCK_NO_PATIENT = "no_patient"
TOOL_BLOCK_UNKNOWN_APPOINTMENT = "unknown_appointment"
TOOL_BLOCK_AMBIGUOUS_APPOINTMENT = "ambiguous_appointment"

# ASCII digits only (`\d` would take any Unicode digit); seconds, if sent, must be :00.
_START_RE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})[T ]([0-9]{2}:[0-9]{2})(?::00)?")
_TEXT_MAX = 120
_DEFAULT_ZONE = "America/Sao_Paulo"

_NO_CLINIC_ERROR = "Nenhuma clínica configurada para esta conversa."
_BAD_START_ERROR = (
    "start precisa estar no formato AAAA-MM-DDTHH:MM, no fuso da clínica (ex.: 2026-10-08T10:00)."
)
_BAD_REF_ERROR = (
    'appointment precisa ser a referência "(ref AAAA-MM-DD HH:MM)" de uma consulta da lista '
    '"consultas marcadas" - nunca um id de evento.'
)
_UNKNOWN_REF_ERROR = (
    "Nenhuma consulta marcada deste paciente começa nesse horário. Use a referência exata "
    'de "consultas marcadas", ou chame manage_existing_appointment com action "cancel" '
    "para o paciente escolher pelos botões."
)
_AMBIGUOUS_REF_ERROR = (
    "Mais de uma consulta deste paciente começa nesse horário. Chame "
    'manage_existing_appointment com action "cancel" para o paciente escolher pelos botões.'
)
_NO_PATIENT_ERROR = (
    "Não consegui identificar o cadastro deste paciente. Não diga que algo foi cancelado: "
    'chame manage_existing_appointment com action "cancel" para ele escolher pelos botões.'
)
_TEMPORARY_ERROR = (
    "Não consegui consultar as consultas deste paciente agora. NUNCA diga que algo foi "
    "cancelado; tente de novo em instantes."
)


def _blocked(tool_name: str, reason: str, message: str) -> dict:
    logger.info("agent_tool_blocked", tool=tool_name, reason=reason)
    return {"error": message}


def _clinic_zone() -> ZoneInfo:
    """The clinic's zone, the one the "(ref ...)" lines were written in (P3)."""
    name = getattr(_tenant_config_ctx.get(), "timezone", None) or _DEFAULT_ZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(_DEFAULT_ZONE)


def _parse_start(text: str) -> tuple[date, time] | None:
    match = _START_RE.fullmatch((text or "").strip())
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1)), time.fromisoformat(match.group(2))
    except ValueError:
        return None


async def _own_upcoming(tenant_id: UUID, conversation_id: UUID) -> list[dict] | None:
    """THIS conversation's patient's upcoming appointments in `tenant_id`; None = no patient."""
    # Lazy, like ai/tools.py: no DB/ORM import at module load.
    from secretaria.core.database import async_session_factory
    from secretaria.models import Conversation
    from secretaria.services.patient_context import load_upcoming_appointments

    async with async_session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        if (
            conversation is None
            or conversation.patient_id is None
            or conversation.tenant_id != tenant_id
        ):
            return None
        return await load_upcoming_appointments(session, tenant_id, conversation.patient_id)


@tool("create_event")
async def create_event_v2(start: str, service: str = "", professional: str = "") -> dict:
    """Prepara a marcação de uma consulta no horário `start` e leva o paciente ao cartão de
    confirmação. NÃO marca nada: quem marca é o paciente, tocando em Confirmar no cartão;
    até lá, nada está marcado nem reservado. O fluxo confere serviço, profissional, dia e se
    o horário está livre, e pergunta o que faltar - inclusive para quem é a consulta, se o
    paciente ainda não respondeu. Não existe título, descrição nem nome aqui: quem escreve
    na agenda é o fluxo. Se o paciente disse para quem é a consulta ou citou o convênio, use
    set_booking_draft, que leva esses campos.

    Args:
        start: Início pedido, AAAA-MM-DDTHH:MM, no fuso da clínica (ex.: 2026-10-08T10:00) -
            de preferência dentro de uma janela devolvida por get_availability.
        service: Nome EXATO de um serviço da clínica (ou vazio).
        professional: Nome do profissional (ou vazio).
    """
    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        return _blocked("create_event", TOOL_BLOCK_NO_CLINIC, _NO_CLINIC_ERROR)
    parsed = _parse_start(start)
    if parsed is None:
        # The value is never echoed nor logged: the model may have copied the patient's words.
        return _blocked("create_event", TOOL_BLOCK_BAD_START, _BAD_START_ERROR)
    day, at = parsed
    professional_id = await _draft_professional_id(tenant_id, (professional or "").strip())
    # The same draft set_booking_draft v2 raises (P2b): the worker lands it through the
    # resolver and, with everything valid, on the details + confirmation card (P3). No
    # insurance and no "pra quem" here - the resolver uses what the conversation recorded.
    raise BookingDraftRequested(
        (service or "").strip()[:_TEXT_MAX] or None,
        professional_id,
        None,
        attendee=None,
        day=day,
        time=at,
    )


@tool("cancel_event")
async def cancel_event_v2(appointment: str) -> dict:
    """Leva o paciente ao cartão "Confirmar o cancelamento?" de UMA consulta JÁ MARCADA
    dele. NÃO cancela nada: quem cancela é o paciente, tocando em Sim no cartão; até lá a
    consulta continua marcada - nunca diga que foi cancelada.

    Args:
        appointment: QUAL consulta, pela referência "(ref AAAA-MM-DD HH:MM)" mostrada em
            "consultas marcadas" (ex.: 2026-10-13 10:00). Nunca um id de evento.
    """
    tenant_id = _tenant_id_ctx.get()
    conversation_id = _conversation_id_ctx.get()
    if tenant_id is None or conversation_id is None:
        return _blocked("cancel_event", TOOL_BLOCK_NO_CLINIC, _NO_CLINIC_ERROR)
    try:
        reference = parse_appointment_ref(appointment)
    except ValueError:
        return _blocked("cancel_event", TOOL_BLOCK_BAD_APPOINTMENT, _BAD_REF_ERROR)
    try:
        upcoming = await _own_upcoming(tenant_id, conversation_id)
    except Exception as exc:
        logger.warning("agent_cancel_staging_failed", error_type=type(exc).__name__)
        return {"error": _TEMPORARY_ERROR}
    if upcoming is None:
        return _blocked("cancel_event", TOOL_BLOCK_NO_PATIENT, _NO_PATIENT_ERROR)
    zone = _clinic_zone()
    wanted = reference.strftime(APPOINTMENT_REF_FORMAT)
    matches: list[dict[str, Any]] = [
        row
        for row in upcoming
        if isinstance(row.get("start_at"), datetime)
        and appointment_ref(row["start_at"], zone) == wanted
    ]
    if not matches:
        return _blocked("cancel_event", TOOL_BLOCK_UNKNOWN_APPOINTMENT, _UNKNOWN_REF_ERROR)
    if len(matches) > 1:
        return _blocked("cancel_event", TOOL_BLOCK_AMBIGUOUS_APPOINTMENT, _AMBIGUOUS_REF_ERROR)
    # The same request manage_existing_appointment v2 sends (P3): the worker re-reads the
    # patient's appointments FRESH and stops at the cancel confirmation card.
    raise ManageAppointmentRequested(ACTION_CANCEL, appointment=reference)


# Read by ai/graph.py: `_tool_cache_key` keeps these apart from the legacy tools of the same
# names, and `effective_tools` lets ONLY this variant of a staging name into a v2 turn.
create_event_v2.metadata = {"cache_variant": BLIND_STAGING_VARIANT}
cancel_event_v2.metadata = {"cache_variant": BLIND_STAGING_VARIANT}
```

- [ ] **Step 4: Run the tests to verify they pass, plus the hand-back suites they mirror**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_blind_staging_tools.py tests/test_set_booking_draft_v2.py tests/test_manage_request.py tests/test_manage_v2_handback.py tests/test_agent_capability_cache.py tests/test_workers_layering.py -q`
Expected: PASS (41 novos). A parte das ferramentas (38 testes, sem `graph.run_agent`) foi verificada numa cópia do repositório com stubs das assinaturas do P2b/P3; as duas equivalências de sentinel e a chave de cache dependem do código real do P2b/P3 e rodam aqui pela primeira vez. Se `test_create_lands_exactly_like_the_draft_with_day_and_time` falhar, compare as duas strings: a única diferença aceitável seria um campo que o `set_booking_draft` v2 preenche sozinho — não há nenhum hoje; corrija a ferramenta cega, nunca o teste.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format src/secretaria/ai/staging_tools.py tests/test_blind_staging_tools.py
uvx ruff check src/secretaria/ai/staging_tools.py tests/test_blind_staging_tools.py
git add src/secretaria/ai/staging_tools.py tests/test_blind_staging_tools.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(ai): blind create_event/cancel_event that only stage the patient's card

On the v2 toolset the AI keeps tools named create_event and cancel_event (owner's decision
of 2026-10-03), but these never write to the agenda, never read an event and take no title,
description, end, name or event id. create_event(start, service, professional) raises the
same BookingDraftRequested set_booking_draft v2 raises - byte-identical sentinel - so the
resolver lands it on the details + confirmation card and the patient's tap books.
cancel_event(appointment) resolves the "(ref ...)" only among this conversation's patient's
upcoming appointments in this tenant and raises the manage v2 cancel request, which stops at
the cancel confirmation card; a reference naming none or two is an error, never a guess.
Refusals never echo the argument and log reason codes only.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 7: A montagem do conjunto — `effective_tools`, o interruptor por turno, `get_availability` e as cegas entregues

**Files:**
- Modify: `src/secretaria/ai/graph.py`
- Modify: `src/secretaria/workers/shared/llm_context.py`
- Modify: `src/secretaria/workers/orchestrator.py`
- Test: `tests/test_ai_toolset_v2.py`; Modify `tests/test_bot_reply_gating.py`, `tests/test_set_booking_draft_v2.py`

**Interfaces:**
- Consumes: Task 4 (`AI_TOOLSET_V2_WITHHELD`, `AI_TOOLSET_V2_STAGING`, `BLIND_STAGING_VARIANT`, `_ai_toolset_v2_ctx`); Task 5 (`get_availability`); Task 6 (`create_event_v2`, `cancel_event_v2`); P2b (`_tool_cache_key`, `draft_tool` em `_flow_handback_tools`, `ai_draft_v2_enabled`); P3 (`manage_tool` em `_flow_handback_tools`).
- Produces: `base_tools_for(..., toolset_v2=)`, `_kept_on_v2(tool) -> bool`, `effective_tools`, `build_agent(..., toolset_v2=)`, `run_agent(..., toolset_v2=)`, `_ai_toolset_v2`; `_flow_handback_tools` entrega `[get_availability, create_event_v2, cancel_event_v2]` no v2. O P5 (Task 4 dele) acrescenta os nomes retirados dentro de `_kept_on_v2`.

- [ ] **Step 1: Write the failing tests**

1. Criar `tests/test_ai_toolset_v2.py`:

```python
"""The AI toolset v2: what the AI loses and what it gains, by exact tool names (TASK-030 P4).

On a clinic with `initial_flows["ai_draft_v2"] = true` the AI no longer has a tool that reads a
busy interval (`ai/tools.py::AI_TOOLSET_V2_WITHHELD`, with the two plugin writers that have no
v2 variant) nor the LEGACY `create_event`/`cancel_event` (`AI_TOOLSET_V2_STAGING`). It reads
free windows through `get_availability`, and the names `create_event`/`cancel_event` are the
blind tools of ai/staging_tools.py, which only stage the patient's confirmation card. With
the switch off, the toolset is exactly what it was.

This is lock one: the tool set (`graph.effective_tools`, asserted by exact tool NAMES, the way
tests/test_agent_tool_enforcement.py does it for topologies, and by IDENTITY where a name has
two implementations). Lock two - each withheld or legacy tool refusing by itself - is
tests/test_ai_toolset_v2_locks.py. `create_react_agent` is replaced by a recording fake:
nothing here reaches OpenAI, Google or a DB.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402

from secretaria.ai import (  # noqa: E402
    graph,
    tools as ai_tools,
)
from secretaria.ai.availability_tool import get_availability  # noqa: E402
from secretaria.ai.staging_tools import cancel_event_v2, create_event_v2  # noqa: E402
from secretaria.plugins import (  # noqa: E402
    multi_professional as mp,
    registry as reg,
)
from secretaria.services.booking_scope import (  # noqa: E402
    BOOKING_TOPOLOGY_MULTI,
    BOOKING_TOPOLOGY_NONE,
    BOOKING_TOPOLOGY_SOLE,
    BOOKING_TOPOLOGY_UNKNOWN,
)
from secretaria.services.calendar import CalendarService, CalendarUnavailableError  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.tenant_config import TenantRuntimeConfig  # noqa: E402
from secretaria.workers.shared.llm_context import (  # noqa: E402
    _ai_toolset_v2,
    _flow_handback_tools,
)

TOPOLOGIES = [
    BOOKING_TOPOLOGY_UNKNOWN,
    BOOKING_TOPOLOGY_NONE,
    BOOKING_TOPOLOGY_SOLE,
    BOOKING_TOPOLOGY_MULTI,
]
TENANT_ON = SimpleNamespace(initial_flows={"ai_draft_v2": True})
TENANT_OFF = SimpleNamespace(initial_flows={})

_SCOPE_FREE = {"iniciar_pre_consulta", "list_patient_appointments", "show_main_menu"}
_TENANT_LEVEL_AGENDA_TOOLS = {
    "check_availability",
    "list_free_slots",
    "create_event",
    "cancel_event",
}
_PLUGIN_AGENDA_TOOLS = {
    "list_free_slots_for_professional",
    "create_event_for_professional",
    "create_event_at_unit",
}
_BUSY_AND_WRITE = _TENANT_LEVEL_AGENDA_TOOLS | _PLUGIN_AGENDA_TOOLS
_WITHHELD = {
    "check_availability",
    "list_free_slots",
    "list_free_slots_for_professional",
    "create_event_for_professional",
    "create_event_at_unit",
}
_STAGING = {"create_event", "cancel_event"}
_V2_READS_AND_STAGING = {"get_availability", *_STAGING}
_PLUGIN_TOOLS_KEPT = {"list_professionals", "select_professional_and_continue", "list_units"}
_HANDBACKS = {"manage_existing_appointment", "set_booking_draft", "request_human_handoff"}

_ADDONS_OFF = {
    "reactivation_pack": False,
    "verified_identity": False,
    "multi_professional": False,
    "multi_unit": False,
    "ehr": False,
    "pix_deposit": False,
    "analytics_bi": False,
    "analytics_bi_advanced": False,
    "human_backup_24_7": False,
}


def _summary(**addons) -> EntitlementSummary:
    return EntitlementSummary(
        tenant_id=str(uuid4()),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="basico",
        addons={**_ADDONS_OFF, **addons},
        limits={},
    )


ALL_ADDONS = _summary(multi_professional=True, multi_unit=True)


def _config(tenant_id=None) -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
        tenant_id=tenant_id or uuid4(),
        clinic_name="Clinica",
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[],
        business_hours={},
        google_calendar_id="cal",
        google_refresh_token=None,
    )


def _names(tools) -> set[str]:
    return {getattr(t, "name", str(t)) for t in tools}


def _staging_tools(tools) -> list:
    return [t for t in tools if getattr(t, "name", None) in _STAGING]


def _turn_tools(tenant, topology, *, addons=ALL_ADDONS):
    """What the worker hands the agent for one turn: `extra_tools` + the v2 flag."""
    extra = _flow_handback_tools(tenant, topology, reg.agent_tools_for(addons))
    return graph.effective_tools(topology, extra, toolset_v2=_ai_toolset_v2(tenant))


class _RecordingAgent:
    def __init__(self, tools):
        self.tools = tools

    async def ainvoke(self, state):
        return {"messages": [AIMessage(content="ok")]}


@pytest.fixture(autouse=True)
def _fake_compile(monkeypatch: pytest.MonkeyPatch):
    graph._AGENTS.clear()
    monkeypatch.setattr(
        graph, "create_react_agent", lambda model, tools, prompt: _RecordingAgent(list(tools))
    )

    async def _empty_history(_conversation_id):
        return []

    monkeypatch.setattr(graph, "_load_history", _empty_history)
    yield
    graph._AGENTS.clear()


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log


# --------------------------------------------------------------------------
# The switch
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tenant, expected",
    [
        (None, False),
        (SimpleNamespace(initial_flows=None), False),
        (TENANT_OFF, False),
        (SimpleNamespace(initial_flows={"ai_draft_v2": False}), False),
        (SimpleNamespace(initial_flows={"ai_draft_v2": "true"}), False),
        (TENANT_ON, True),
    ],
)
def test_the_toolset_switch_reads_only_an_explicit_true(tenant, expected):
    assert _ai_toolset_v2(tenant) is expected


# --------------------------------------------------------------------------
# Lock one: the tool set, by exact names (and identity where a name has two tools)
# --------------------------------------------------------------------------


def _todays_tool_names(topology):
    """The tool names a turn had before TASK-030 P4, written out: the regression oracle."""
    names = _SCOPE_FREE | _PLUGIN_TOOLS_KEPT | _PLUGIN_AGENDA_TOOLS | _HANDBACKS
    if topology != BOOKING_TOPOLOGY_MULTI:
        names |= _TENANT_LEVEL_AGENDA_TOOLS | {"start_guided_booking"}
    return names


@pytest.mark.parametrize("topology", TOPOLOGIES)
def test_switch_off_is_exactly_todays_composition(topology):
    extras = reg.agent_tools_for(ALL_ADDONS)
    assert graph.effective_tools(topology, extras) == [*graph.base_tools_for(topology), *extras]
    tools = _turn_tools(TENANT_OFF, topology)
    assert _names(tools) == _todays_tool_names(topology)
    # Where today's set has create_event/cancel_event, they are the legacy ones.
    assert all(t in (ai_tools.create_event, ai_tools.cancel_event) for t in _staging_tools(tools))


@pytest.mark.parametrize("topology", TOPOLOGIES)
def test_switch_on_swaps_the_busy_readers_for_get_availability_and_the_blind_writers(topology):
    tools = _turn_tools(TENANT_ON, topology)
    expected = _SCOPE_FREE | _PLUGIN_TOOLS_KEPT | _HANDBACKS | _V2_READS_AND_STAGING
    if topology != BOOKING_TOPOLOGY_MULTI:
        expected |= {"start_guided_booking"}
    assert _names(tools) == expected
    assert _names(tools).isdisjoint(_WITHHELD)
    # Same names as the legacy writers, but the blind implementations - on every topology,
    # a multi-professional clinic included (the professional is a field of the draft).
    assert _staging_tools(tools) == [create_event_v2, cancel_event_v2]


@pytest.mark.parametrize("topology", TOPOLOGIES)
def test_switch_on_strips_a_withheld_or_legacy_tool_even_when_a_caller_passes_it(topology):
    extras = [
        *reg.agent_tools_for(ALL_ADDONS),
        ai_tools.create_event,
        ai_tools.cancel_event,
        ai_tools.check_availability,
    ]
    tools = graph.effective_tools(topology, extras, toolset_v2=True)
    assert _names(tools).isdisjoint(_BUSY_AND_WRITE)


@pytest.mark.parametrize("topology", TOPOLOGIES)
def test_a_staging_name_passes_only_as_its_blind_variant(topology):
    extras = [ai_tools.create_event, create_event_v2, ai_tools.cancel_event, cancel_event_v2]
    tools = graph.effective_tools(topology, extras, toolset_v2=True)
    assert _staging_tools(tools) == [create_event_v2, cancel_event_v2]


def test_without_the_addons_the_v2_set_still_has_the_way_back_to_the_flow():
    names = _names(_turn_tools(TENANT_ON, BOOKING_TOPOLOGY_SOLE, addons=_summary()))
    assert names == _SCOPE_FREE | _HANDBACKS | _V2_READS_AND_STAGING | {"start_guided_booking"}


@pytest.mark.parametrize("topology", TOPOLOGIES)
def test_the_v2_tools_are_offered_only_through_the_switch(topology):
    v2_tools = (get_availability, create_event_v2, cancel_event_v2)
    on = _flow_handback_tools(TENANT_ON, topology, [])
    assert all(t in on for t in v2_tools)
    for tenant in (TENANT_OFF, None):
        off = _flow_handback_tools(tenant, topology, [])
        assert not any(t in off for t in v2_tools)


# --------------------------------------------------------------------------
# build_agent / run_agent carry the switch
# --------------------------------------------------------------------------


def test_the_switch_yields_a_distinct_cached_agent_per_set():
    extras = reg.agent_tools_for(ALL_ADDONS)
    v1 = graph.build_agent(extras, BOOKING_TOPOLOGY_SOLE)
    v2 = graph.build_agent(extras, BOOKING_TOPOLOGY_SOLE, toolset_v2=True)
    assert v1 is not v2
    assert graph.build_agent(extras, BOOKING_TOPOLOGY_SOLE, toolset_v2=True) is v2
    assert graph.build_agent(extras, BOOKING_TOPOLOGY_SOLE) is v1
    assert _names(v2.tools).isdisjoint(_BUSY_AND_WRITE)
    assert "create_event" in _names(v1.tools)


async def test_run_agent_threads_the_switch_and_always_resets_it(monkeypatch):
    seen: list[tuple[bool, set[str]]] = []

    async def _capture(messages):
        agent = graph.build_agent(
            graph._extra_tools_ctx.get(),
            ai_tools._booking_topology_ctx.get(),
            toolset_v2=ai_tools._ai_toolset_v2_ctx.get(),
        )
        seen.append((ai_tools._ai_toolset_v2_ctx.get(), _names(agent.tools)))
        return "ok"

    monkeypatch.setattr(graph, "invoke_agent", _capture)
    assert ai_tools._ai_toolset_v2_ctx.get() is False
    for flag in (True, False):
        await graph.run_agent(
            "oi",
            context={"conversation_id": str(uuid4())},
            tenant_config=_config(),
            extra_tools=reg.agent_tools_for(ALL_ADDONS),
            booking_topology=BOOKING_TOPOLOGY_SOLE,
            toolset_v2=flag,
        )
        assert ai_tools._ai_toolset_v2_ctx.get() is False
    (on_flag, on_names), (off_flag, off_names) = seen
    assert (on_flag, off_flag) == (True, False)
    assert on_names.isdisjoint(_BUSY_AND_WRITE)
    assert {"create_event", "check_availability"} <= off_names


async def test_run_agent_logs_the_effective_capabilities(monkeypatch):
    log = _Log()
    monkeypatch.setattr(graph, "logger", log)

    async def _noop(messages):
        return "ok"

    monkeypatch.setattr(graph, "invoke_agent", _noop)
    await graph.run_agent(
        "oi",
        context={"conversation_id": str(uuid4())},
        tenant_config=_config(),
        extra_tools=[ai_tools.create_event, create_event_v2, get_availability],
        booking_topology=BOOKING_TOPOLOGY_SOLE,
        toolset_v2=True,
    )
    (fields,) = [f for e, f in log.events if e == "agent_capabilities_resolved"]
    assert fields["toolset_v2"] is True
    # The legacy create_event is gone; the blind one (same name) is what ran.
    assert fields["capabilities"] == sorted(_SCOPE_FREE | {"get_availability", "create_event"})


async def test_concurrent_turns_never_share_the_switch(monkeypatch):
    import asyncio

    started, release = asyncio.Event(), asyncio.Event()
    observed: dict[bool, set[str]] = {}

    async def _interleaved(messages):
        flag = ai_tools._ai_toolset_v2_ctx.get()
        names = _names(graph.build_agent((), BOOKING_TOPOLOGY_SOLE, toolset_v2=flag).tools)
        if flag:
            started.set()
            await release.wait()
        else:
            await started.wait()
            release.set()
        observed[flag] = names
        assert ai_tools._ai_toolset_v2_ctx.get() is flag
        return "ok"

    monkeypatch.setattr(graph, "invoke_agent", _interleaved)
    await asyncio.gather(
        *(
            graph.run_agent(
                "oi",
                context={"conversation_id": str(uuid4())},
                tenant_config=_config(),
                booking_topology=BOOKING_TOPOLOGY_SOLE,
                toolset_v2=flag,
            )
            for flag in (True, False)
        )
    )
    assert "create_event" not in observed[True]
    assert "create_event" in observed[False]


async def test_a_calendar_outage_inside_get_availability_reaches_the_handover_sentinel(
    monkeypatch,
):
    """Today's behaviour for every calendar read, kept: outage -> the sentinel -> a person."""

    async def _active(_tenant_id):
        return []

    async def _down(self, *args, **kwargs):
        raise CalendarUnavailableError("down")

    monkeypatch.setattr(mp, "_active_professionals", _active)
    monkeypatch.setattr(CalendarService, "list_available_days", _down)

    async def _ask_the_tool(messages):
        return str(await get_availability.ainvoke({}))

    monkeypatch.setattr(graph, "invoke_agent", _ask_the_tool)
    reply = await graph.run_agent(
        "tem horário?",
        context={"conversation_id": str(uuid4())},
        tenant_config=_config(),
        booking_topology=BOOKING_TOPOLOGY_NONE,
        toolset_v2=True,
    )
    assert reply == graph.CALENDAR_UNAVAILABLE_SENTINEL
```

2. Em `tests/test_bot_reply_gating.py`, acrescentar ao fim do arquivo (duas linhas em branco antes do bloco; os nomes `Tenant`, `db`, `_make_conversation`, `_run_send_bot_reply_capturing_run_agent` e `_LLM_ESCAPE` já existem nele):

```python
# --------------------------------------------------------------------------
# TASK-030 P4: the per-clinic switch reaches run_agent (toolset v2)
# --------------------------------------------------------------------------


async def _flows_of(db, tenant: Tenant, flows: dict) -> None:
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.initial_flows = flows
        await session.commit()


async def test_a_clinic_with_the_v2_switch_gets_the_new_toolset(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    from secretaria.ai.staging_tools import cancel_event_v2, create_event_v2

    tenant, patient, conversation = await _make_conversation(db)
    await _flows_of(db, tenant, {"ai_draft_v2": True})

    (call,) = await _run_send_bot_reply_capturing_run_agent(
        monkeypatch, conversation, patient, _LLM_ESCAPE
    )

    assert call["toolset_v2"] is True
    assert "get_availability" in {t.name for t in call["extra_tools"]}
    assert create_event_v2 in call["extra_tools"] and cancel_event_v2 in call["extra_tools"]


async def test_a_clinic_without_the_switch_keeps_todays_toolset(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    tenant, patient, conversation = await _make_conversation(db)
    await _flows_of(db, tenant, {"ai_draft_v2": "true"})  # only the JSON literal true counts

    (call,) = await _run_send_bot_reply_capturing_run_agent(
        monkeypatch, conversation, patient, _LLM_ESCAPE
    )

    assert call["toolset_v2"] is False
    names = {t.name for t in call["extra_tools"]}
    # Neither the agenda read nor a blind writer: the legacy create/cancel come from the base set.
    assert names.isdisjoint({"get_availability", "create_event", "cancel_event"})
```

3. Em `tests/test_set_booking_draft_v2.py` (criado pelo P2b, Task B3), em `test_the_clinic_switch_picks_the_draft_tool`, trocar a última asserção

```python
    assert sorted(t.name for t in on) == sorted(t.name for t in off)
```

por

```python
    # TASK-030 P4: the v2 set also carries the agenda read and the two blind staging tools.
    assert sorted(t.name for t in on) == sorted(
        [*(t.name for t in off), "get_availability", "create_event", "cancel_event"]
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_ai_toolset_v2.py tests/test_bot_reply_gating.py tests/test_set_booking_draft_v2.py -q`
Expected: erro de coleta em `test_ai_toolset_v2.py` — `ImportError: cannot import name '_ai_toolset_v2' from 'secretaria.workers.shared.llm_context'`. (Sem ele: os dois testes novos do gating falham com `KeyError: 'toolset_v2'` e o de `test_set_booking_draft_v2.py` com a lista sem as três ferramentas.)

- [ ] **Step 3: `ai/graph.py`**

Em `src/secretaria/ai/graph.py` (âncoras do estado DEPOIS do P2b/P3):

1. No import de `secretaria.ai.tools`, inserir `    AI_TOOLSET_V2_STAGING,`, `    AI_TOOLSET_V2_WITHHELD,` e `    BLIND_STAGING_VARIANT,` (uma linha cada, nessa ordem) antes de `    BookingDraftRequested,`, e `    _ai_toolset_v2_ctx,` antes de `    _booking_topology_ctx,` (a ordem do ruff: constantes, classes, depois o resto, cada grupo em ordem alfabética).

2. Trocar a assinatura `def base_tools_for(topology: str) -> tuple:` por:

```python
def base_tools_for(topology: str, *, toolset_v2: bool = False) -> tuple:
```

3. Em `base_tools_for`, logo depois da linha `    precisely the wrong agenda.` (fim do último parágrafo do docstring), inserir uma linha em branco e o parágrafo:

```python

    On the AI toolset v2 (TASK-030 P4, `toolset_v2`) NO topology gets a tenant-level
    calendar tool: the AI reads availability through `get_availability` and its
    `create_event`/`cancel_event` are the blind staging tools the worker hands it
    (ai/staging_tools.py), so the base set is the scope-free one everywhere.
```

4. Trocar o corpo

```python
    if topology == BOOKING_TOPOLOGY_MULTI:
        return _SCOPE_FREE_TOOLS
    return _BASE_TOOLS
```

por (as funções novas vêm logo depois, antes do comentário `# Compiled agent cache, keyed by ...`):

```python
    if toolset_v2 or topology == BOOKING_TOPOLOGY_MULTI:
        return _SCOPE_FREE_TOOLS
    return _BASE_TOOLS


def _kept_on_v2(tool: Any) -> bool:
    """Whether an extra tool may join a v2 turn (lock one of two).

    Never a tool in `AI_TOOLSET_V2_WITHHELD`. For a name in `AI_TOOLSET_V2_STAGING`
    (`create_event`, `cancel_event`), only its blind variant - ai/staging_tools.py, marked
    `metadata["cache_variant"] == BLIND_STAGING_VARIANT` -, never the legacy implementation
    of the same name, which writes to the agenda. The second lock is each legacy tool's own
    refusal (`ai/tools.py::_blocked_by_toolset_v2`).
    """
    name = getattr(tool, "name", str(tool))
    if name in AI_TOOLSET_V2_WITHHELD:
        return False
    if name in AI_TOOLSET_V2_STAGING:
        variant = (getattr(tool, "metadata", None) or {}).get("cache_variant")
        return variant == BLIND_STAGING_VARIANT
    return True


def effective_tools(topology: str, extra_tools: Sequence = (), *, toolset_v2: bool = False) -> list:
    """THE tool list of one turn: base set + the tenant's extra tools.

    One assembly for `build_agent` and for the `agent_capabilities_resolved` log, so what
    is logged is what ran. With `toolset_v2` the extras are filtered by `_kept_on_v2` (no
    busy reader, no legacy writer; the blind create/cancel pass). With it off this is
    exactly `[*base_tools_for(topology), *extra_tools]`, as it always was.
    """
    if not toolset_v2:
        return [*base_tools_for(topology), *extra_tools]
    kept = [t for t in extra_tools if _kept_on_v2(t)]
    return [*base_tools_for(topology, toolset_v2=True), *kept]
```

5. Trocar a assinatura de `build_agent`

```python
def build_agent(extra_tools: Sequence = (), topology: str = BOOKING_TOPOLOGY_UNKNOWN) -> Any:
```

por

```python
def build_agent(
    extra_tools: Sequence = (),
    topology: str = BOOKING_TOPOLOGY_UNKNOWN,
    *,
    toolset_v2: bool = False,
) -> Any:
```

e, no corpo, trocar `    tools = [*base_tools_for(topology), *extra_tools]` por `    tools = effective_tools(topology, extra_tools, toolset_v2=toolset_v2)` (a linha `key = frozenset(_tool_cache_key(t) for t in tools)` que o P2b deixou logo abaixo não muda aqui: a chave são os nomes+variantes do conjunto EFETIVO — `create_event#blind_v2` ≠ `create_event`; a Task 8 acrescenta a marca do interruptor).

6. Em `invoke_agent`, trocar as três linhas

```python
    result = await build_agent(_extra_tools_ctx.get(), _booking_topology_ctx.get()).ainvoke(
        {"messages": messages}
    )
```

por

```python
    agent = build_agent(
        _extra_tools_ctx.get(),
        _booking_topology_ctx.get(),
        toolset_v2=_ai_toolset_v2_ctx.get(),
    )
    result = await agent.ainvoke({"messages": messages})
```

7. Em `run_agent`:
   - depois de `    conversation_state: str | None = None,` (último parâmetro), inserir `    toolset_v2: bool = False,`;
   - depois da linha `    (see services/tenant_config.py::TenantRuntimeConfig.appointment_context).` do docstring, inserir:

```python
    `toolset_v2` is the per-clinic switch `flow_router.ai_draft_v2_enabled`, decided by the
    worker (workers/shared/llm_context.py::_ai_toolset_v2): True builds the agent without
    the tools that read busy intervals or write to the agenda (`effective_tools`; the
    blind create/cancel the worker hands over stay) and makes each of them refuse if one
    arrives anyway. False (the default) changes nothing.
```

   - depois de `    tok_topology = _booking_topology_ctx.set(booking_topology)`, inserir `    tok_toolset_v2 = _ai_toolset_v2_ctx.set(toolset_v2)`;
   - trocar a linha `        getattr(t, "name", str(t)) for t in (*base_tools_for(booking_topology), *extra_tools)` por

```python
        getattr(t, "name", str(t))
        for t in effective_tools(booking_topology, extra_tools, toolset_v2=toolset_v2)
```

   - no `logger.info("agent_capabilities_resolved", ...)`, depois de `        topology=booking_topology,` inserir `        toolset_v2=toolset_v2,`;
   - no `finally:`, depois de `        _booking_topology_ctx.reset(tok_topology)`, inserir `        _ai_toolset_v2_ctx.reset(tok_toolset_v2)`.

- [ ] **Step 4: `workers/shared/llm_context.py`**

Em `src/secretaria/workers/shared/llm_context.py` (estado DEPOIS do P2b/P3):

1. Logo antes de `from secretaria.ai.tools import (`, inserir as duas linhas

```python
from secretaria.ai.availability_tool import get_availability
from secretaria.ai.staging_tools import cancel_event_v2, create_event_v2
```

2. Logo antes de `def _flow_handback_tools(tenant: Tenant | None, topology: str, plugin_tools: list) -> list:`, inserir:

```python
def _ai_toolset_v2(tenant: Tenant | None) -> bool:
    """Whether THIS tenant's agent runs on the v2 toolset (TASK-030 P4, spec §4.6).

    The per-clinic switch (`flow_router.ai_draft_v2_enabled`) AND the flow existing at all:
    on v2 the agent books and cancels only by handing back to the flow (its create_event /
    cancel_event are blind staging tools), so a tenant without those hand-backs must never
    swap the legacy `create_event` for the blind one. Read once per turn, here, and handed
    to `run_agent(toolset_v2=...)`.
    """
    return tenant is not None and flows_enabled(tenant) and ai_draft_v2_enabled(tenant)


```

3. No docstring de `_flow_handback_tools`, depois da linha `    arrives anyway.`, inserir:

```python

    On the v2 toolset (`_ai_toolset_v2`) the AI also gets `get_availability`, its only
    agenda read (free windows, never events), and the blind `create_event` /
    `cancel_event` (ai/staging_tools.py), which only stage the patient's confirmation card;
    the tools it loses are withheld in ai/graph.py::effective_tools, not here.
```

4. Trocar a última linha da função `    return [*plugin_tools, *handbacks]` por:

```python
    v2_tools = (
        [get_availability, create_event_v2, cancel_event_v2] if _ai_toolset_v2(tenant) else []
    )
    return [*plugin_tools, *handbacks, *v2_tools]
```

- [ ] **Step 5: `workers/orchestrator.py`**

Em `src/secretaria/workers/orchestrator.py`:

1. No import de `secretaria.workers.shared.llm_context`, inserir `    _ai_toolset_v2,` antes de `    _appointment_context_text,`.
2. Na chamada `reply_text = await run_agent(...)`, depois da linha `        conversation_state=conversation_state_text,`, inserir `        toolset_v2=_ai_toolset_v2(tenant),`.

- [ ] **Step 6: Run the tests to verify they pass, plus the suites around this seam**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_ai_toolset_v2.py tests/test_ai_toolset_v2_locks.py tests/test_get_availability_tool.py tests/test_blind_staging_tools.py tests/test_bot_reply_gating.py tests/test_set_booking_draft_v2.py tests/test_agent_tool_enforcement.py tests/test_agent_capability_cache.py tests/test_agent_menu_tools.py tests/test_llm_turn_trace.py tests/test_prompts.py tests/test_workers_layering.py tests/test_workers_ns_patching.py -q`
Expected: PASS (32 + 2 novos; o conjunto de hoje, nome por nome, inalterado com o interruptor desligado).

- [ ] **Step 7: Lint and commit**

```bash
uvx ruff format tests/test_ai_toolset_v2.py
uvx ruff check src/secretaria/ai/graph.py src/secretaria/workers/shared/llm_context.py src/secretaria/workers/orchestrator.py tests/test_ai_toolset_v2.py tests/test_bot_reply_gating.py tests/test_set_booking_draft_v2.py
for f in src/secretaria/ai/graph.py src/secretaria/workers/shared/llm_context.py src/secretaria/workers/orchestrator.py tests/test_bot_reply_gating.py tests/test_set_booking_draft_v2.py; do echo "$f $(uvx ruff format --diff $f 2>/dev/null | grep -c '^@@')"; done   # none may exceed its count before editing
git add src/secretaria/ai/graph.py src/secretaria/workers/shared/llm_context.py src/secretaria/workers/orchestrator.py tests/test_ai_toolset_v2.py tests/test_bot_reply_gating.py tests/test_set_booking_draft_v2.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(ai): the per-clinic switch builds the v2 toolset - free windows and blind writers

graph.effective_tools is the one assembly of a turn's tools (also behind the capabilities log):
with toolset_v2 the base set is the scope-free one, the busy readers and plugin writers are
stripped by name, and a create_event/cancel_event passes only as its blind staging variant;
off, it is exactly what it always was. The worker decides the switch once per turn (flows
enabled AND ai_draft_v2) and hands it to run_agent, which sets the context var the legacy
tools' own refusal reads. _flow_handback_tools delivers get_availability and the blind
create_event/cancel_event to v2 clinics only.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 8: A lista de saída declarada de cada ferramenta v2 (`ai/tool_output.py`) e o embrulho no agente

**Files:**
- Create: `src/secretaria/ai/tool_output.py`
- Modify: `src/secretaria/ai/graph.py` (import; `build_agent`)
- Test: `tests/test_ai_tool_output_allowlist.py`

**Interfaces:**
- Consumes: Task 7 (`graph.effective_tools(..., toolset_v2=True)`, `build_agent(..., toolset_v2=)`, `_flow_handback_tools` com as ferramentas v2); `ai/pii.py::wrap_tools_with_pseudonymizer` (hoje); P2b (`graph._tool_cache_key`).
- Produces: `ai/tool_output.py::{OutputAllowlist, V2_TOOL_OUTPUTS, UNDECLARED_ERROR, UNUSABLE_ERROR, undeclared_keys, allowlisted, wrap_tools_with_output_allowlist}`; log `agent_tool_output_dropped(tool, keys)` e `agent_tool_output_undeclared(tool)`; em `build_agent`, com `toolset_v2`: chave do cache `| {"#toolset_v2"}` e as ferramentas embrulhadas pela lista de saída ANTES do guarda de pseudonimização. A Task 9 usa `undeclared_keys` nas saídas cruas.

A lista (uma entrada por NOME; mudar só junto com a ferramenta e com este teste):

| Ferramenta | Chaves de topo | Registros | Texto |
|---|---|---|---|
| `get_availability` | `windows`, `timezone`, `slot_minutes`, `day_from`, `day_to`, `professional`, `clamped`, `note`, `error` | `windows[]`: `day`, `start`, `end` | não |
| `list_patient_appointments` | `appointments`, `count`, `nota`, `error` | `appointments[]`: `quando`, `tipo` | não |
| `list_professionals` | `professionals`, `error` | `professionals[]`: `name`, `specialty`, `about` | não |
| `list_units` | `units`, `error` | `units[]`: `name`, `address` | não |
| `iniciar_pre_consulta` | `error` | — | sim (as frases dela) |
| `create_event`, `cancel_event`, `set_booking_draft`, `manage_existing_appointment`, `request_human_handoff`, `show_main_menu`, `start_guided_booking`, `select_professional_and_continue` | `error` | — | não |

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_ai_tool_output_allowlist.py`:

```python
"""The v2 output allowlist (TASK-030 P4): every v2 tool answers only the keys it declared.

`ai/tool_output.py::V2_TOOL_OUTPUTS` is the declaration; `wrap_tools_with_output_allowlist`
enforces it on every tool of a v2 agent (ai/graph.py::build_agent), INSIDE the
pseudonymization guard. Three layers here: the filter itself (pure), the static check that
every tool a v2 turn can be built with has a declaration (every topology, with and without
the addons), and the agent wiring. The end-to-end proof against decoy data is
tests/test_ai_v2_blindness.py.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

from secretaria.ai import (  # noqa: E402
    graph,
    tool_output,
)
from secretaria.ai.tool_output import (  # noqa: E402
    UNDECLARED_ERROR,
    UNUSABLE_ERROR,
    V2_TOOL_OUTPUTS,
    allowlisted,
    undeclared_keys,
    wrap_tools_with_output_allowlist,
)
from secretaria.ai.tools import ShowMainMenuRequested  # noqa: E402
from secretaria.plugins import registry as reg  # noqa: E402
from secretaria.services.booking_scope import (  # noqa: E402
    BOOKING_TOPOLOGY_MULTI,
    BOOKING_TOPOLOGY_NONE,
    BOOKING_TOPOLOGY_SOLE,
    BOOKING_TOPOLOGY_UNKNOWN,
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.workers.shared.llm_context import _flow_handback_tools  # noqa: E402

CANARY = "CANARY-ab12"
TOPOLOGIES = [
    BOOKING_TOPOLOGY_UNKNOWN,
    BOOKING_TOPOLOGY_NONE,
    BOOKING_TOPOLOGY_SOLE,
    BOOKING_TOPOLOGY_MULTI,
]
TENANT_ON = SimpleNamespace(initial_flows={"ai_draft_v2": True})
_ADDONS_OFF = {
    "reactivation_pack": False,
    "verified_identity": False,
    "multi_professional": False,
    "multi_unit": False,
    "ehr": False,
    "pix_deposit": False,
    "analytics_bi": False,
    "analytics_bi_advanced": False,
    "human_backup_24_7": False,
}


def _summary(**addons) -> EntitlementSummary:
    return EntitlementSummary(
        tenant_id=str(uuid4()),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="basico",
        addons={**_ADDONS_OFF, **addons},
        limits={},
    )


ALL_ADDONS = _summary(multi_professional=True, multi_unit=True)
NO_ADDONS = _summary()


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log


@pytest.fixture
def log(monkeypatch):
    recorder = _Log()
    monkeypatch.setattr(tool_output, "logger", recorder)
    return recorder


# --------------------------------------------------------------------------
# The filter (pure)
# --------------------------------------------------------------------------


def test_a_clean_answer_passes_untouched(log):
    answer = {
        "windows": [{"day": "2026-10-06", "start": "08:00", "end": "09:00"}],
        "timezone": "America/Sao_Paulo",
        "slot_minutes": 30,
        "day_from": "2026-10-06",
        "day_to": "2026-10-06",
        "clamped": ["max_days"],
        "note": "x",
    }
    assert allowlisted("get_availability", answer) == answer
    assert undeclared_keys("get_availability", answer) == []
    assert log.events == []


def test_an_undeclared_key_is_dropped_and_logged_by_name_only(log):
    answer = {
        "windows": [],
        "summary": f"Consulta - {CANARY} Silva",
        "busy": [{"id": f"evt-{CANARY}", "summary": CANARY}],
    }
    out = allowlisted("get_availability", answer)
    assert out == {"windows": []}
    assert undeclared_keys("get_availability", answer) == ["summary", "busy"]
    ((event, fields),) = log.events
    assert (event, fields["tool"], fields["keys"]) == (
        "agent_tool_output_dropped",
        "get_availability",
        ["busy", "summary"],
    )
    assert CANARY not in repr(log.events)


def test_a_record_keeps_only_its_declared_fields(log):
    answer = {
        "windows": [
            {"day": "2026-10-06", "start": "08:00", "end": "09:00", "summary": CANARY},
            "not-a-record",
            {"day": "2026-10-07", "start": "08:00", "end": "09:00", "attendees": [CANARY]},
        ]
    }
    out = allowlisted("get_availability", answer)
    assert out == {
        "windows": [
            {"day": "2026-10-06", "start": "08:00", "end": "09:00"},
            {"day": "2026-10-07", "start": "08:00", "end": "09:00"},
        ]
    }
    assert CANARY not in repr(out)
    (fields,) = [f for e, f in log.events if e == "agent_tool_output_dropped"]
    assert fields["keys"] == ["windows[]", "windows[].attendees", "windows[].summary"]


def test_a_nested_structure_under_a_plain_key_is_dropped(log):
    out = allowlisted("get_availability", {"windows": [], "note": {"who": CANARY}})
    assert out == {"windows": []}
    assert CANARY not in repr(out) + repr(log.events)


def test_a_key_that_is_not_an_identifier_is_logged_as_other(log):
    out = allowlisted("list_patient_appointments", {f"Maria {CANARY}": 1, "count": 0})
    assert out == {"count": 0}
    (fields,) = [f for e, f in log.events if e == "agent_tool_output_dropped"]
    assert fields["keys"] == ["<other>"]
    assert CANARY not in repr(log.events)


def test_an_undeclared_tool_answers_an_error_instead_of_its_data(log):
    assert allowlisted("tool_from_tomorrow", {"anything": CANARY}) == {"error": UNDECLARED_ERROR}
    assert undeclared_keys("tool_from_tomorrow", {}) == ["<undeclared tool>"]
    assert log.events == [("agent_tool_output_undeclared", {"tool": "tool_from_tomorrow"})]


def test_text_only_where_declared(log):
    assert allowlisted("iniciar_pre_consulta", "Pré-consulta liberada.") == "Pré-consulta liberada."
    assert allowlisted("create_event", f"evento {CANARY}") == {"error": UNUSABLE_ERROR}
    assert undeclared_keys("create_event", "x") == ["<text>"]


def test_nothing_left_is_an_error_not_an_empty_answer(log):
    assert allowlisted("create_event", {"id": f"evt-{CANARY}", "htmlLink": CANARY}) == {
        "error": UNUSABLE_ERROR
    }


def test_the_staging_and_handback_tools_may_only_ever_say_error():
    for name in (
        "create_event",
        "cancel_event",
        "set_booking_draft",
        "manage_existing_appointment",
        "request_human_handoff",
        "show_main_menu",
    ):
        assert V2_TOOL_OUTPUTS[name].keys == frozenset({"error"})
        assert V2_TOOL_OUTPUTS[name].text is False


@tool("list_units")
async def _leaky_list_units() -> dict:
    """Stand-in with a declared name that leaks an undeclared key."""
    return {"units": [{"name": "Centro", "address": "Rua 1", "owner": CANARY}], "summary": CANARY}


@tool("show_main_menu")
async def _menu() -> str:
    """Hands back."""
    raise ShowMainMenuRequested()


@tool("list_professionals")
def _sync_tool() -> dict:
    """A sync tool would bypass the filter."""
    return {"professionals": []}


@tool
async def _undeclared() -> dict:
    """No declaration."""
    return {"anything": CANARY}


async def test_the_wrapper_filters_keeps_identity_and_lets_handbacks_propagate(log):
    _leaky_list_units.metadata = {"cache_variant": "x"}
    units, menu, sync, undeclared = wrap_tools_with_output_allowlist(
        [_leaky_list_units, _menu, _sync_tool, _undeclared]
    )
    assert (units.name, units.args, units.metadata) == (
        "list_units",
        _leaky_list_units.args,
        {"cache_variant": "x"},
    )
    assert await units.ainvoke({}) == {"units": [{"name": "Centro", "address": "Rua 1"}]}
    with pytest.raises(ShowMainMenuRequested):
        await menu.ainvoke({})
    assert await sync.ainvoke({}) == {"error": UNDECLARED_ERROR}
    assert await undeclared.ainvoke({}) == {"error": UNDECLARED_ERROR}


# --------------------------------------------------------------------------
# Static: every tool a v2 turn can be built with has a declaration
# --------------------------------------------------------------------------


def _v2_turn_tools(topology, addons):
    extras = _flow_handback_tools(TENANT_ON, topology, reg.agent_tools_for(addons))
    return graph.effective_tools(topology, extras, toolset_v2=True)


@pytest.mark.parametrize("addons", [ALL_ADDONS, NO_ADDONS], ids=["all_addons", "no_addons"])
@pytest.mark.parametrize("topology", TOPOLOGIES)
def test_every_tool_of_every_v2_turn_declares_its_output(topology, addons):
    tools = _v2_turn_tools(topology, addons)
    undeclared = {t.name for t in tools} - set(V2_TOOL_OUTPUTS)
    assert not undeclared, f"declare the output of {sorted(undeclared)} in ai/tool_output.py"
    # Async only: a sync tool would run without the filter, so the wrapper refuses it.
    assert all(getattr(t, "coroutine", None) is not None for t in tools)


# --------------------------------------------------------------------------
# The agent wiring
# --------------------------------------------------------------------------


@pytest.fixture
def _recording_build(monkeypatch):
    graph._AGENTS.clear()
    monkeypatch.setattr(
        graph,
        "create_react_agent",
        lambda model, tools, prompt: SimpleNamespace(tools=list(tools)),
    )
    yield
    graph._AGENTS.clear()


async def test_a_v2_agent_wraps_every_tool_inside_the_pseudonymization_guard(
    _recording_build, monkeypatch
):
    handed_to_the_guard: list = []
    real_guard = graph.wrap_tools_with_pseudonymizer

    def _recording_guard(tools):
        handed_to_the_guard.extend(tools)
        return real_guard(tools)

    monkeypatch.setattr(graph, "wrap_tools_with_pseudonymizer", _recording_guard)
    agent = graph.build_agent([_leaky_list_units], BOOKING_TOPOLOGY_SOLE, toolset_v2=True)

    # The guard receives tools that ALREADY pass the allowlist (the allowlist is inside) ...
    (handed,) = [t for t in handed_to_the_guard if t.name == "list_units"]
    assert await handed.ainvoke({}) == {"units": [{"name": "Centro", "address": "Rua 1"}]}
    # ... and the agent gets the guard's wrapper around it (the guard is still on).
    (built,) = [t for t in agent.tools if t.name == "list_units"]
    assert built.coroutine is not handed.coroutine
    assert await built.ainvoke({}) == {"units": [{"name": "Centro", "address": "Rua 1"}]}


async def test_a_v1_agent_is_built_exactly_as_before(_recording_build):
    agent = graph.build_agent([_leaky_list_units], BOOKING_TOPOLOGY_SOLE)
    (built,) = [t for t in agent.tools if t.name == "list_units"]
    # No allowlist on the switch-off path: the tool answers exactly what it returns.
    assert (await built.ainvoke({}))["summary"] == CANARY


def test_a_v2_agent_never_shares_a_cache_entry_with_a_v1_agent(_recording_build):
    """A multi clinic with no extras has the same three scope-free NAMES on v1 and v2."""
    v1 = graph.build_agent((), BOOKING_TOPOLOGY_MULTI)
    v2 = graph.build_agent((), BOOKING_TOPOLOGY_MULTI, toolset_v2=True)
    assert {t.name for t in v1.tools} == {t.name for t in v2.tools}
    assert v1 is not v2
    assert graph.build_agent((), BOOKING_TOPOLOGY_MULTI, toolset_v2=True) is v2
    assert graph.build_agent((), BOOKING_TOPOLOGY_MULTI) is v1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_ai_tool_output_allowlist.py -q`
Expected: erro de coleta — `ModuleNotFoundError: No module named 'secretaria.ai.tool_output'`.

- [ ] **Step 3: Write the module**

Criar `src/secretaria/ai/tool_output.py`:

```python
"""What a v2 tool may hand the model: a declared allowlist of output keys (TASK-030 P4).

The owner's rule (2026-10-03): on the v2 toolset the AI never sees a name or any other
patient's data - not pseudonymized, not in any form. The PRIMARY defense is that no v2 tool
reads such data in the first place: `get_availability` computes free windows and never
touches an event, `create_event`/`cancel_event` (ai/staging_tools.py) only stage the
patient's confirmation card, `list_patient_appointments` reads only this patient's rows.

This module is the structural wall behind that. Every tool of a v2 turn declares HERE which
keys its answer may carry (and, for a list of records, which keys each record may carry).
`wrap_tools_with_output_allowlist` - applied by ai/graph.py::build_agent on v2 turns only -
drops every other key before the answer re-enters the model's context and logs the dropped
key NAMES (never a value; a key that is not a plain identifier is logged as "<other>"). A
tool with no declaration answers an error instead of its data, so nothing - a Google API
dict included - is ever passed through by default. tests/test_ai_tool_output_allowlist.py
fails when a v2 tool has no declaration; tests/test_ai_v2_blindness.py runs every v2 tool
against decoy data and fails on any undeclared key.

The pseudonymization guard (ai/pii.py) stays as a SECOND wall, wrapped outside this one. It
is not the primary defense: it masks patterns (phone, CPF, e-mail) and values already in
the conversation's map, and - its own docstring says so - cannot mask an unknown third
party's name ("Consulta - Maria Silva" goes through it as itself).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from secretaria.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class OutputAllowlist:
    """The answer shape one tool may return to the model.

    `keys`: the top-level keys allowed. `records`: for a key whose value is a list of
    records, the keys each record may carry. `text`: the tool answers with one of its own
    sentences (a str) instead of a dict.
    """

    keys: frozenset[str]
    records: Mapping[str, frozenset[str]] = field(default_factory=dict)
    text: bool = False


_ERROR_ONLY = OutputAllowlist(frozenset({"error"}))

# Every tool a v2 turn can be built with, by model-facing NAME. A tool that only hands back
# to the flow (it raises; the model never reads a result) or refuses with an error declares
# just "error". Adding a tool to the v2 set without a line here fails
# tests/test_ai_tool_output_allowlist.py and, at runtime, makes the tool answer an error.
V2_TOOL_OUTPUTS: Mapping[str, OutputAllowlist] = {
    "get_availability": OutputAllowlist(
        frozenset(
            {
                "windows",
                "timezone",
                "slot_minutes",
                "day_from",
                "day_to",
                "professional",
                "clamped",
                "note",
                "error",
            }
        ),
        records={"windows": frozenset({"day", "start", "end"})},
    ),
    "list_patient_appointments": OutputAllowlist(
        frozenset({"appointments", "count", "nota", "error"}),
        records={"appointments": frozenset({"quando", "tipo"})},
    ),
    "list_professionals": OutputAllowlist(
        frozenset({"professionals", "error"}),
        records={"professionals": frozenset({"name", "specialty", "about"})},
    ),
    "list_units": OutputAllowlist(
        frozenset({"units", "error"}),
        records={"units": frozenset({"name", "address"})},
    ),
    "iniciar_pre_consulta": OutputAllowlist(frozenset({"error"}), text=True),
    "create_event": _ERROR_ONLY,
    "cancel_event": _ERROR_ONLY,
    "set_booking_draft": _ERROR_ONLY,
    "manage_existing_appointment": _ERROR_ONLY,
    "request_human_handoff": _ERROR_ONLY,
    "show_main_menu": _ERROR_ONLY,
    "start_guided_booking": _ERROR_ONLY,
    "select_professional_and_continue": _ERROR_ONLY,
}

_SCALARS = (str, int, float, bool, type(None))
_LOGGABLE_KEY = re.compile(r"[a-z_][a-z0-9_]{0,40}")

UNDECLARED_ERROR = (
    "Esta ferramenta não está liberada para responder nesta clínica. Não invente o "
    "resultado: siga com o paciente pelo fluxo (show_main_menu)."
)
UNUSABLE_ERROR = (
    "A ferramenta não devolveu uma resposta utilizável. Não invente o resultado: tente de "
    "novo uma vez ou siga com o paciente pelo fluxo (show_main_menu)."
)


def _log_name(key: Any) -> str:
    """A key as it may appear in a log line: a plain identifier, else "<other>"."""
    text = str(key)
    return text if _LOGGABLE_KEY.fullmatch(text) else "<other>"


def _clean_records(key: str, value: Any, allowed: frozenset[str], dropped: list[str]) -> list:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        dropped.append(key)
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, Mapping):
            dropped.append(f"{key}[]")
            continue
        record: dict = {}
        for name, field_value in item.items():
            if name in allowed and isinstance(field_value, _SCALARS):
                record[name] = field_value
            else:
                dropped.append(f"{key}[].{_log_name(name)}")
        out.append(record)
    return out


def _clean_plain(key: str, value: Any, dropped: list[str]) -> tuple[bool, Any]:
    """A declared key that is not a record list: scalars or a list of scalars only."""
    if isinstance(value, _SCALARS):
        return True, value
    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and all(isinstance(item, _SCALARS) for item in value)
    ):
        return True, list(value)
    dropped.append(key)
    return False, None


def undeclared_keys(tool_name: str, result: Any) -> list[str]:
    """The key NAMES in `result` its declaration does not allow ([] = clean).

    What the static/decoy tests assert on the RAW output of a tool, before any wrapper.
    """
    spec = V2_TOOL_OUTPUTS.get(tool_name)
    if spec is None:
        return ["<undeclared tool>"]
    if isinstance(result, str):
        return [] if spec.text else ["<text>"]
    if not isinstance(result, Mapping):
        return [f"<{type(result).__name__}>"]
    dropped: list[str] = []
    for key, value in result.items():
        if key not in spec.keys:
            dropped.append(_log_name(key))
        elif key in spec.records:
            _clean_records(key, value, spec.records[key], dropped)
        else:
            _clean_plain(key, value, dropped)
    return dropped


def allowlisted(tool_name: str, result: Any) -> Any:
    """`result` reduced to what `tool_name` declared; an error dict when nothing can pass."""
    spec = V2_TOOL_OUTPUTS.get(tool_name)
    if spec is None:
        logger.warning("agent_tool_output_undeclared", tool=tool_name)
        return {"error": UNDECLARED_ERROR}
    if isinstance(result, str):
        if spec.text:
            return result
        logger.warning("agent_tool_output_dropped", tool=tool_name, keys=["<text>"])
        return {"error": UNUSABLE_ERROR}
    if not isinstance(result, Mapping):
        logger.warning(
            "agent_tool_output_dropped", tool=tool_name, keys=[f"<{type(result).__name__}>"]
        )
        return {"error": UNUSABLE_ERROR}
    dropped: list[str] = []
    out: dict = {}
    for key, value in result.items():
        if key not in spec.keys:
            dropped.append(_log_name(key))
        elif key in spec.records:
            out[key] = _clean_records(key, value, spec.records[key], dropped)
        else:
            kept, cleaned = _clean_plain(key, value, dropped)
            if kept:
                out[key] = cleaned
    if dropped:
        logger.warning("agent_tool_output_dropped", tool=tool_name, keys=sorted(set(dropped)))
    if not out:
        return {"error": UNUSABLE_ERROR}
    return out


def _refusing(tool: Any) -> Any:
    name = getattr(tool, "name", str(tool))

    async def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        logger.warning("agent_tool_output_undeclared", tool=name)
        return {"error": UNDECLARED_ERROR}

    return tool.model_copy(update={"coroutine": _refuse})


def _wrap_one(tool: Any) -> Any:
    name = getattr(tool, "name", str(tool))
    inner = getattr(tool, "coroutine", None)
    if inner is None or name not in V2_TOOL_OUTPUTS:
        # Fail closed: a sync tool would bypass the filter, an undeclared one has no shape.
        return _refusing(tool)

    async def _allowlisted(*args: Any, **kwargs: Any) -> Any:
        # NOT wrapped in try/except: the hand-back exceptions (BookingDraftRequested,
        # ManageAppointmentRequested, ...) and CalendarUnavailableError must propagate
        # out of the ToolNode for run_agent to catch by type (same rule as ai/pii.py).
        return allowlisted(name, await inner(*args, **kwargs))

    # model_copy keeps name, description, args_schema and metadata: the model sees the same
    # tool and graph._tool_cache_key the same identity.
    return tool.model_copy(update={"coroutine": _allowlisted})


def wrap_tools_with_output_allowlist(tools: Sequence[Any]) -> tuple[Any, ...]:
    """Wrap a v2 turn's tools so each answer passes its declared allowlist (innermost)."""
    return tuple(_wrap_one(t) for t in tools)
```

- [ ] **Step 4: Wire it into `build_agent`**

Em `src/secretaria/ai/graph.py` (estado depois da Task 7; antes de editar, anote o `ruff format --diff` como sempre):

1. Logo antes de `from secretaria.ai.tools import (`, inserir `from secretaria.ai.tool_output import wrap_tools_with_output_allowlist`.
2. Em `build_agent`, trocar

```python
    key = frozenset(_tool_cache_key(t) for t in tools)
    cached = _AGENTS.get(key)
```

por

```python
    key = frozenset(_tool_cache_key(t) for t in tools)
    if toolset_v2:
        # TASK-030 P4: a v2 agent's tools are wrapped differently (below), so it never
        # shares a cache entry with a v1 agent of the same names (a multi clinic, no extras).
        key = key | {"#toolset_v2"}
    cached = _AGENTS.get(key)
```

3. Ainda em `build_agent`, trocar a linha `    tools = list(wrap_tools_with_pseudonymizer(tools))` por

```python
    if toolset_v2:
        # TASK-030 P4: every answer passes its tool's declared allowlist (ai/tool_output.py)
        # BEFORE the pseudonymization guard sees it. The guard is the second wall, not the
        # first: it cannot mask a third party's name it has never seen.
        tools = list(wrap_tools_with_output_allowlist(tools))
    tools = list(wrap_tools_with_pseudonymizer(tools))
```

- [ ] **Step 5: Run the tests to verify they pass, plus the suites that build agents**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_ai_tool_output_allowlist.py tests/test_ai_toolset_v2.py tests/test_get_availability_tool.py tests/test_blind_staging_tools.py tests/test_agent_capability_cache.py tests/test_agent_tool_enforcement.py tests/test_pii_pseudonymization.py tests/test_llm_turn_trace.py -q`
Expected: PASS (21 novos). Os 11 testes do filtro puro (e o do caminho v1) foram verificados numa cópia do repositório em `b0ac5ee`; o estático e os de montagem dependem da Task 7. Se `test_every_tool_of_every_v2_turn_declares_its_output` falhar, a mensagem diz qual ferramenta: declare a saída dela na tabela acima e em `V2_TOOL_OUTPUTS` (no mesmo commit da ferramenta) — nunca a retire do teste.

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff format src/secretaria/ai/tool_output.py tests/test_ai_tool_output_allowlist.py
uvx ruff check src/secretaria/ai/tool_output.py src/secretaria/ai/graph.py tests/test_ai_tool_output_allowlist.py
uvx ruff format --diff src/secretaria/ai/graph.py | grep -c '^@@'   # must not exceed the count noted before editing
git add src/secretaria/ai/tool_output.py src/secretaria/ai/graph.py tests/test_ai_tool_output_allowlist.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(ai): every v2 tool answers only the keys it declared

ai/tool_output.py declares, per v2 tool name, the top-level keys and record fields it may
return (and whether it may answer text). build_agent wraps every tool of a v2 turn with that
allowlist INSIDE the pseudonymization guard: undeclared keys are dropped before the model
reads them and logged by name only; an undeclared or sync tool answers an error instead of
its data. A static test fails when a tool a v2 turn can be built with has no declaration.
The v2 cache key carries a marker so a v1 agent with the same names is never reused.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 9: Iscas — nenhum nome, contato, campo de evento ou dado de outro tenant chega ao modelo num turno v2

**Files:**
- Test: `tests/test_ai_v2_blindness.py` (novo; nenhum código de produção)

**Interfaces:**
- Consumes: Tasks 1–8 inteiras, pela costura que o repositório já usa para ver o que o modelo recebe (`graph.invoke_agent` trocado por um roteiro, como em `tests/test_pii_pseudonymization.py`); `graph.run_agent(..., toolset_v2=True)`, `graph.build_agent`, `graph.effective_tools`, `graph._extra_tools_ctx`; `ai_tools._booking_topology_ctx`, `ai_tools._ai_toolset_v2_ctx`; `tool_output.undeclared_keys`; P2b `services/llm_context.py::build_conversation_state`; P3 `workers/shared/llm_context.py::_appointment_context_text(..., with_refs=True)`, `manage_request.appointment_ref`; `_flow_handback_tools`; `pseudonymize_core.has_unresolved_tokens`.
- Produces: a prova ponta a ponta da cegueira (decisão do dono, 2026-10-03): o teste que falha se qualquer isca aparecer no prompt, no histórico ou no resultado de qualquer ferramenta v2 — inclusive mascarada (`[PACIENTE_…]`).

Esta tarefa não traz código de produção: ela prova, de ponta a ponta, as garantias das Tasks 1–8. Por isso o primeiro run já deve PASSAR; o auto-teste `test_the_canary_check_itself_catches_a_leak` prova que o verificador dispara (um verificador que nunca dispara não prova nada). Se algum teste falhar, a falha é de uma tarefa anterior: corrija o código da tarefa dona (ferramenta que lê dado de terceiro, chave não declarada, bloco do prompt que mostra nome), nunca relaxe a isca.

- [ ] **Step 1: Write the decoy test**

Criar `tests/test_ai_v2_blindness.py`:

```python
"""Decoys: on a v2 turn NO name, contact, event field or other tenant's data reaches the model.

The owner's rule (2026-10-03): the AI never sees a name or any other patient's data - not
pseudonymized, not in any form. This runs one whole v2 turn through `graph.run_agent` with
`invoke_agent` replaced by a script (the seam tests/test_pii_pseudonymization.py uses to see
what the model would receive). The world is full of decoys - unique strings that must never
show up anywhere the model reads:

  * the database: another patient of the same clinic (name, phone, e-mail, appointment), the
    third party our own patient booked for (attendee name, also recorded on the conversation),
    and a second clinic with its own doctor, unit, patient and appointment at the very minute
    of ours;
  * the agenda: a REAL CalendarService over a fake Google client whose events carry the
    decoys in title, description, location, link, attendees, creator and id.

The script renders the system prompt + history the model would get, runs EVERY tool of the
v2 set as the agent holds it (output allowlist inside the pseudonymization guard), renders
the prompt again with the results appended, and also runs each tool RAW (no wrapper) to
check its declared output shape. Nothing here reaches OpenAI or Google.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import json  # noqa: E402
from datetime import UTC, date, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest_asyncio  # noqa: E402
from langchain_core.messages import HumanMessage, ToolMessage  # noqa: E402
from pseudonymize_core import has_unresolved_tokens  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai import (  # noqa: E402
    graph,
    tools as ai_tools,
)
from secretaria.ai.tool_output import undeclared_keys  # noqa: E402
from secretaria.ai.tools import (  # noqa: E402
    BookingDraftRequested,
    GuidedBookingRequested,
    HumanHandoffRequested,
    ManageAppointmentRequested,
    SelectProfessionalRequested,
    ShowMainMenuRequested,
)
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    Patient,
    Professional,
    Tenant,
    Unit,
)
from secretaria.plugins import registry as reg  # noqa: E402
from secretaria.services import (  # noqa: E402
    booking_hold as booking_hold_service,
    pii_pseudonymization as store,
)
from secretaria.services.booking_scope import BOOKING_TOPOLOGY_SOLE  # noqa: E402
from secretaria.services.calendar import CalendarService  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.llm_context import build_conversation_state  # noqa: E402
from secretaria.services.manage_request import appointment_ref  # noqa: E402
from secretaria.services.patient_context import load_upcoming_appointments  # noqa: E402
from secretaria.services.precheck import HandoffOutcome  # noqa: E402
from secretaria.services.tenant_config import (  # noqa: E402
    RuntimeAppointmentType,
    TenantRuntimeConfig,
)
from secretaria.workers.shared.llm_context import (  # noqa: E402
    _appointment_context_text,
    _flow_handback_tools,
)

TZ = ZoneInfo("America/Sao_Paulo")

# Every string below is unique and must never reach the model, in any form.
OTHER_PATIENT_NAME = "Zuleica Decoyana"
OTHER_PATIENT_PHONE = "5511987650001"
OTHER_PATIENT_EMAIL = "zuleica.decoyana@example.com"
OWN_ATTENDEE_NAME = "Hermengarda Decoyosa"
OWN_EVENT_ID = "evt-decoy-own-71c2"
OTHER_EVENT_ID = "evt-decoy-other-55a9"
EVENT_DESCRIPTION = "Notas DECOY-DESC-3381"
EVENT_LOCATION = "Sala DECOY-LOC-2210"
EVENT_LINK = "https://calendar.example/DECOY-LINK-9047"
EVENT_ATTENDEE_EMAIL = "decoy.attendee.6612@example.com"
TENANT_B_CLINIC = "Clinica Decoy-B 4471"
TENANT_B_DOCTOR = "Dr. Decoy-B Teodoro"
TENANT_B_UNIT = "Unidade Decoy-B 8830"
TENANT_B_PATIENT = "Teodora Decoy-B"
TENANT_B_PHONE = "5511987650002"
TENANT_B_EVENT_ID = "evt-decoy-b-1029"
CANARIES = (
    OTHER_PATIENT_NAME,
    OTHER_PATIENT_PHONE,
    OTHER_PATIENT_EMAIL,
    OWN_ATTENDEE_NAME,
    OWN_EVENT_ID,
    OTHER_EVENT_ID,
    EVENT_DESCRIPTION,
    EVENT_LOCATION,
    EVENT_LINK,
    EVENT_ATTENDEE_EMAIL,
    TENANT_B_CLINIC,
    TENANT_B_DOCTOR,
    TENANT_B_UNIT,
    TENANT_B_PATIENT,
    TENANT_B_PHONE,
    TENANT_B_EVENT_ID,
)
_HANDBACKS = (
    BookingDraftRequested,
    GuidedBookingRequested,
    HumanHandoffRequested,
    ManageAppointmentRequested,
    SelectProfessionalRequested,
    ShowMainMenuRequested,
)
ALL_ADDONS = EntitlementSummary(
    tenant_id=str(uuid4()),
    status="active",
    active=True,
    secretaria_enabled=True,
    plan="bronze",
    secretaria_tier="basico",
    addons={"multi_professional": True, "multi_unit": True},
    limits={},
)
_ALL_WEEK = {
    day: [{"start": "08:00", "end": "12:00"}]
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}


def _leaks(text: str) -> list[str]:
    folded = text.casefold()
    return [canary for canary in CANARIES if canary.casefold() in folded]


def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _day(offset: int) -> date:
    return datetime.now(TZ).date() + timedelta(days=offset)


def _at(offset: int, hour: int) -> datetime:
    day = _day(offset)
    return datetime(day.year, day.month, day.day, hour, tzinfo=TZ)


# --------------------------------------------------------------------------
# The decoy world
# --------------------------------------------------------------------------


class _GoogleEvents:
    def __init__(self, items):
        self._items = items

    def list(self, **_kwargs):
        items = self._items

        class _Request:
            def execute(self):
                return {"items": items}

        return _Request()


class _GoogleService:
    def __init__(self, items):
        self._events = _GoogleEvents(items)

    def events(self):
        return self._events


def _google_event(event_id: str, title: str, start: datetime) -> dict:
    return {
        "id": event_id,
        "summary": title,
        "description": EVENT_DESCRIPTION,
        "location": EVENT_LOCATION,
        "htmlLink": EVENT_LINK,
        "attendees": [{"email": EVENT_ATTENDEE_EMAIL, "displayName": OTHER_PATIENT_NAME}],
        "creator": {"email": OTHER_PATIENT_EMAIL},
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(minutes=30)).isoformat()},
    }


def _decoy_calendar(monkeypatch) -> CalendarService:
    """A REAL CalendarService (its own slot walk) over a fake Google client full of decoys."""
    settings = SimpleNamespace(
        CLINIC_TIMEZONE="America/Sao_Paulo",
        GOOGLE_CALENDAR_ID="primary",
        GOOGLE_CLIENT_ID="id",
        GOOGLE_CLIENT_SECRET="secret",
        GOOGLE_REFRESH_TOKEN="token",
    )
    calendar = CalendarService(settings=settings)
    calendar._business_hours = _ALL_WEEK
    calendar._default_slot_minutes = 30
    items = [
        _google_event(OTHER_EVENT_ID, f"Consulta - {OTHER_PATIENT_NAME}", _at(2, 10)),
        _google_event(OWN_EVENT_ID, f"Consulta - {OWN_ATTENDEE_NAME}", _at(3, 10)),
    ]
    monkeypatch.setattr(calendar, "_service", _GoogleService(items))
    return calendar


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(core_database, "async_session_factory", maker)
    monkeypatch.setattr(store, "async_session_factory", maker)
    monkeypatch.setattr(booking_hold_service, "async_session_factory", maker)
    yield maker
    await engine.dispose()


def _appointment(tenant_id, patient_id, conversation_id, event_id, start, **extra):
    return Appointment(
        tenant_id=tenant_id,
        patient_id=patient_id,
        conversation_id=conversation_id,
        google_event_id=event_id,
        appointment_type="Consulta",
        start_at=start.astimezone(UTC),
        end_at=(start + timedelta(minutes=30)).astimezone(UTC),
        status=AppointmentStatus.SCHEDULED,
        **extra,
    )


async def _seed(db):
    async with db() as session:
        clinic = Tenant(
            id=uuid4(),
            clinic_name="Clinica Aurora",
            phone_number_id=str(uuid4())[:12],
            timezone="America/Sao_Paulo",
            appointment_types=[{"name": "Consulta", "duration_min": 30, "is_active": True}],
            initial_flows={"ai_draft_v2": True},
        )
        other_clinic = Tenant(
            id=uuid4(), clinic_name=TENANT_B_CLINIC, phone_number_id=str(uuid4())[:12]
        )
        session.add_all([clinic, other_clinic])
        await session.flush()
        doctor = Professional(
            tenant_id=clinic.id, name="Dra. Ana Souza", google_calendar_id=None, is_active=True
        )
        session.add_all(
            [
                doctor,
                Professional(tenant_id=other_clinic.id, name=TENANT_B_DOCTOR, is_active=True),
                Unit(tenant_id=clinic.id, name="Unidade Centro", address="Rua Um, 1"),
                Unit(tenant_id=other_clinic.id, name=TENANT_B_UNIT, address="Rua B, 2"),
            ]
        )
        patient = Patient(tenant_id=clinic.id, wa_id="5511900000001", name="Paciente Proprio")
        other = Patient(
            tenant_id=clinic.id,
            wa_id=OTHER_PATIENT_PHONE,
            name=OTHER_PATIENT_NAME,
            email=OTHER_PATIENT_EMAIL,
        )
        foreign = Patient(tenant_id=other_clinic.id, wa_id=TENANT_B_PHONE, name=TENANT_B_PATIENT)
        session.add_all([patient, other, foreign])
        await session.flush()
        conversation = Conversation(
            tenant_id=clinic.id,
            patient_id=patient.id,
            # Our patient already answered "pra quem": the third party's name is on record.
            flow_attendee_name=OWN_ATTENDEE_NAME,
        )
        other_conversation = Conversation(tenant_id=clinic.id, patient_id=other.id)
        foreign_conversation = Conversation(tenant_id=other_clinic.id, patient_id=foreign.id)
        session.add_all([conversation, other_conversation, foreign_conversation])
        await session.flush()
        session.add_all(
            [
                _appointment(
                    clinic.id,
                    patient.id,
                    conversation.id,
                    OWN_EVENT_ID,
                    _at(3, 10),
                    professional_id=doctor.id,
                    attendee_name=OWN_ATTENDEE_NAME,
                ),
                _appointment(
                    clinic.id, other.id, other_conversation.id, OTHER_EVENT_ID, _at(2, 10)
                ),
                _appointment(
                    other_clinic.id,
                    foreign.id,
                    foreign_conversation.id,
                    TENANT_B_EVENT_ID,
                    _at(3, 10),
                ),
            ]
        )
        await session.commit()
        return SimpleNamespace(
            clinic=clinic,
            doctor=doctor,
            conversation_id=conversation.id,
            patient_id=patient.id,
        )


def _config(clinic) -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
        tenant_id=clinic.id,
        clinic_name=clinic.clinic_name,
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[
            RuntimeAppointmentType(name="Consulta", description=None, duration_min=30)
        ],
        business_hours=_ALL_WEEK,
        google_calendar_id="primary",
        google_refresh_token=None,
    )


def _scripted_calls(own_ref: str, other_ref: str) -> dict[str, list[dict]]:
    """What the script makes the "model" call. Every v2 tool must have an entry."""
    first, last = _day(2).isoformat(), _day(3).isoformat()
    return {
        "get_availability": [{"day_from": first, "day_to": last}],
        "list_patient_appointments": [{}],
        "iniciar_pre_consulta": [{}],
        "show_main_menu": [{}],
        "list_professionals": [{}],
        "list_units": [{}],
        "select_professional_and_continue": [{"professional_name": "Fulano de Tal"}],
        "start_guided_booking": [{"appointment_type": "Consulta"}],
        "set_booking_draft": [{"service": "Consulta", "day": last, "time": "09:00"}],
        "manage_existing_appointment": [{"action": "cancel", "appointment": own_ref}],
        "request_human_handoff": [{"reason": "nenhum"}],
        "create_event": [
            {"start": f"{last}T09:00", "service": "Consulta"},
            {"start": "amanhã às 10"},
        ],
        "cancel_event": [
            {"appointment": own_ref},
            {"appointment": other_ref},
            # A decoy IN the argument: a refusal must not echo it back.
            {"appointment": OTHER_EVENT_ID},
        ],
    }


@pytest_asyncio.fixture
async def turn(db, monkeypatch):
    """Run one v2 turn over the decoy world; return everything the model would have read."""
    world = await _seed(db)
    calendar = _decoy_calendar(monkeypatch)
    monkeypatch.setattr(
        graph,
        "CalendarService",
        SimpleNamespace(from_tenant_config=lambda _config: calendar),
    )

    async def _no_precheck(_tenant_id, _phone):
        return SimpleNamespace(outcome=HandoffOutcome.NOT_ENTITLED)

    monkeypatch.setattr(ai_tools, "request_precheck_handoff", _no_precheck)

    async def _history(_conversation_id):
        return [HumanMessage(content="Oi, quero ver horários e cancelar minha consulta")]

    monkeypatch.setattr(graph, "_load_history", _history)
    graph._AGENTS.clear()
    monkeypatch.setattr(
        graph,
        "create_react_agent",
        lambda model, tools, prompt: SimpleNamespace(tools=list(tools), prompt=prompt),
    )

    async with db() as session:
        clinic = await session.get(Tenant, world.clinic.id)
        conversation = await session.get(Conversation, world.conversation_id)
        doctor = await session.get(Professional, world.doctor.id)
        upcoming = await load_upcoming_appointments(session, clinic.id, world.patient_id)
        state = build_conversation_state(conversation, clinic, [doctor])
    config = _config(clinic)
    appointment_context = _appointment_context_text(
        upcoming,
        "America/Sao_Paulo",
        {str(doctor.id): doctor.name},
        config.appointment_types,
        with_refs=True,
    )
    extras = _flow_handback_tools(clinic, BOOKING_TOPOLOGY_SOLE, reg.agent_tools_for(ALL_ADDONS))
    own_ref = appointment_ref(_at(3, 10), TZ)
    calls = _scripted_calls(own_ref, appointment_ref(_at(2, 10), TZ))
    seen = SimpleNamespace(
        own_ref=own_ref, prompts=[], outputs=[], raw_outputs=[], tool_names=set(), calls=calls
    )

    async def _run(tool, args):
        try:
            return await tool.ainvoke(args)
        except _HANDBACKS as exc:
            return {"handed_back": type(exc).__name__}

    async def _scripted(messages):
        agent = graph.build_agent(
            graph._extra_tools_ctx.get(),
            ai_tools._booking_topology_ctx.get(),
            toolset_v2=ai_tools._ai_toolset_v2_ctx.get(),
        )
        raw = {
            t.name: t
            for t in graph.effective_tools(
                ai_tools._booking_topology_ctx.get(),
                graph._extra_tools_ctx.get(),
                toolset_v2=True,
            )
        }
        seen.prompts.append(agent.prompt({"messages": list(messages)}))
        replies = []
        for tool in agent.tools:
            seen.tool_names.add(tool.name)
            for i, args in enumerate(calls.get(tool.name, [])):
                out = await _run(tool, args)
                seen.outputs.append((tool.name, out))
                seen.raw_outputs.append((tool.name, await _run(raw[tool.name], args)))
                replies.append(ToolMessage(content=_text(out), tool_call_id=f"{tool.name}-{i}"))
        seen.prompts.append(agent.prompt({"messages": [*messages, *replies]}))
        return "ok"

    monkeypatch.setattr(graph, "invoke_agent", _scripted)
    reply = await graph.run_agent(
        "Oi, quero ver horários e cancelar minha consulta",
        context={"conversation_id": str(world.conversation_id)},
        tenant_config=config,
        extra_tools=extras,
        appointment_context=appointment_context,
        booking_topology=BOOKING_TOPOLOGY_SOLE,
        conversation_state=state,
        toolset_v2=True,
    )
    assert reply == "ok"
    graph._AGENTS.clear()
    return seen


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------


def test_the_canary_check_itself_catches_a_leak():
    """A check that never fires proves nothing: it must catch a decoy in any case and a token."""
    assert _leaks(f"Consulta - {OTHER_PATIENT_NAME.upper()}") == [OTHER_PATIENT_NAME]
    assert _leaks(_text({"id": TENANT_B_EVENT_ID})) == [TENANT_B_EVENT_ID]
    assert _leaks("Dra. Ana Souza, Consulta, 08:00") == []
    assert has_unresolved_tokens("Consulta - [PACIENTE_a1b2]")


async def test_every_v2_tool_was_run_against_the_decoys(turn):
    ran = {name for name, _ in turn.outputs}
    # Every tool the v2 agent holds has a scripted call, and ran.
    assert turn.tool_names <= set(turn.calls), turn.tool_names - set(turn.calls)
    assert ran == turn.tool_names
    assert {"get_availability", "create_event", "cancel_event", "list_patient_appointments"} <= ran
    # The reads really read, and the staging tools really staged or refused.
    (availability,) = [out for name, out in turn.outputs if name == "get_availability"]
    assert availability["windows"]
    (listing,) = [out for name, out in turn.outputs if name == "list_patient_appointments"]
    assert listing["count"] == 1
    own, other, event_id = [out for name, out in turn.outputs if name == "cancel_event"]
    assert own == {"handed_back": "ManageAppointmentRequested"}
    assert set(other) == {"error"} and set(event_id) == {"error"}
    staged, garbled = [out for name, out in turn.outputs if name == "create_event"]
    assert staged == {"handed_back": "BookingDraftRequested"}
    assert set(garbled) == {"error"}
    # The prompt did carry the appointment block (with its reference) and the state.
    system = str(turn.prompts[0][0].content)
    assert turn.own_ref in system


async def test_no_decoy_reaches_the_model_on_a_v2_turn(turn):
    rendered = [str(message.content) for prompt in turn.prompts for message in prompt]
    outputs = [_text(out) for _name, out in turn.outputs]
    for text in [*rendered, *outputs]:
        assert _leaks(text) == [], text[:300]
        # "Not even pseudonymized": nothing was there for the guard to mask.
        assert not has_unresolved_tokens(text), text[:300]


async def test_every_raw_v2_output_stays_inside_its_allowlist(turn):
    """The tools THEMSELVES stay inside their declarations - the wrapper is a backstop, not
    the reason the decoys stay out."""
    for name, out in turn.raw_outputs:
        if isinstance(out, dict) and "handed_back" in out:
            continue  # a hand-back ends the turn: the model never reads anything from it
        assert undeclared_keys(name, out) == [], (name, out)
        assert _leaks(_text(out)) == [], name
```

- [ ] **Step 2: Run it**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_ai_v2_blindness.py -q`
Expected: PASS (4 testes). Se `test_every_v2_tool_was_run_against_the_decoys` acusar uma ferramenta sem chamada no roteiro, acrescente a chamada em `_scripted_calls` (toda ferramenta v2 roda contra as iscas). Se `test_no_decoy_reaches_the_model_on_a_v2_turn` acusar, a mensagem traz o começo do texto: procure de que ferramenta ou bloco do prompt ele veio. Se `get_availability` vier sem janelas, confira que o roteiro pede dias com horário de atendimento (`_ALL_WEEK`) e que nenhum relógio foi fixado neste arquivo (as datas são relativas a hoje no fuso da clínica).

- [ ] **Step 3: Lint and commit**

```bash
uvx ruff format tests/test_ai_v2_blindness.py
uvx ruff check tests/test_ai_v2_blindness.py
git add tests/test_ai_v2_blindness.py
git diff --cached --stat
git commit -F - <<'EOF'
test(ai): decoys prove the v2 AI never sees a name or another patient's data

One whole v2 turn through run_agent over a decoy world - another patient (name, phone,
e-mail, appointment), the third party our patient booked for, a second clinic (doctor, unit,
patient, appointment at our minute) and a real CalendarService over fake Google events with
unique strings in title, description, location, link, attendees, creator and id. Every v2
tool runs as the agent holds it; the system prompt, the history and every result the model
would read are checked for every decoy and for pseudonymization tokens, and every raw tool
output against its declared allowlist.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 10: Validação completa e documentação

**Files:**
- Create: `docs/CHECKPOINT_ia_get_availability.md`
- Modify: `docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md`, `docs/CHECKPOINT_plugins.md`, `docs/CHECKPOINT_pseudonimizacao.md` (uma linha de ponteiro cada, ao fim)

**Interfaces:**
- Consumes: tudo das Tasks 1–9.
- Produces: o CHECKPOINT que o P5 lê antes de começar.

- [ ] **Step 1: Run the whole suite and compare with the last full run**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest -q 2>&1 | tail -15`
Expected: `N' passed` com N' = (a contagem do último run completo, o da Task 9 do P3) + 221 testes novos (13 + 25 + 12 + 14 + 57 + 41 + 32 + 21 + 4 nos nove arquivos novos, + 2 em `tests/test_bot_reply_gating.py`) e as MESMAS falhas pré-existentes, nenhuma outra. Qualquer falha nova é regressão deste plano: investigue (skill `superpowers:systematic-debugging`) antes de seguir.

- [ ] **Step 2: Lint every file this plan touched**

```bash
git diff --name-only $(git merge-base HEAD main)..HEAD -- '*.py' | xargs uvx ruff check
```

Expected: `All checks passed!`.

- [ ] **Step 3: Write the CHECKPOINT**

Criar `docs/CHECKPOINT_ia_get_availability.md`:

```markdown
# CHECKPOINT — IA entra em qualquer etapa: `get_availability`, ferramentas de agenda cegas e o conjunto v2 (TASK-030 P4)

**Estado:** BUILT — commits locais na branch da TASK-030 (P4), **não pushado, não deployado**. A correção do `cancel_event` legado vale para TODAS as clínicas no deploy; o resto fica atrás do interruptor `initial_flows.ai_draft_v2` (desligado por padrão).
**Spec:** `docs/superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md` (§2 decisão 4 e §4.6 emendadas em 2026-10-03, §5 critérios 5, 6 e 8, §4.11, §7).
**Plano:** `docs/superpowers/plans/2026-10-02-ia-p4-disponibilidade-so-horarios.md`.

## 1. O que muda

- **Todas as clínicas (correção de segurança):** o `cancel_event` legado só cancela evento que é de um `Appointment` do paciente da conversa, no tenant do turno (a mesma prova do `do_paciente`). Antes, apagava qualquer id que o modelo mandasse. Recusa → erro que manda usar `manage_existing_appointment`, log `agent_tool_blocked(reason=not_patients_event)`. Evento criado pela equipe direto no Google, sem `Appointment` do paciente, não é mais cancelável pela IA.
- **Interruptor desligado:** fora a correção acima, nada. O conjunto de ferramentas é exatamente o de antes (fixado por testes, nome por nome).
- **Interruptor ligado (decisão do dono de 2026-10-03: a IA nunca vê nome nem dado de outro paciente — nem pseudonimizado):**
  - saem `check_availability`, `list_free_slots`, `list_free_slots_for_professional` (leem ocupado/horários crus), `create_event_for_professional` (dobrada no `professional` do `create_event` cego) e `create_event_at_unit` (unidade não é campo do rascunho nem do toque);
  - entra `get_availability`, que devolve só janelas livres;
  - `create_event` e `cancel_event` **continuam com o mesmo nome, mas cegos** (`ai/staging_tools.py`): nunca escrevem na agenda nem leem evento; `create_event(start, service, professional)` entrega ao fluxo o mesmo rascunho do `set_booking_draft` (dia e horário de `start`; "pra quem" e convênio vêm do que a conversa já tem, ou o fluxo pergunta) e o paciente cai nos detalhes + cartão de confirmação; `cancel_event(appointment)` só aceita a "(ref …)" de uma consulta DO PRÓPRIO paciente e leva ao cartão "Confirmar o cancelamento?". **Quem marca e quem cancela é o toque do paciente**, pelo caminho de hoje (portão de código do Portal, reservas, autorização);
  - toda ferramenta v2 só devolve as chaves que declarou (`ai/tool_output.py`); o resto cai antes do modelo.

## 2. Onde está cada peça (âncoras)

- `ai/tools.py` — `cancel_event` (checagem de dono, `TOOL_BLOCK_NOT_PATIENTS_EVENT`); `AI_TOOLSET_V2_WITHHELD` (5), `AI_TOOLSET_V2_STAGING` (`create_event`, `cancel_event`), `BLIND_STAGING_VARIANT`, `_ai_toolset_v2_ctx`, `_blocked_by_toolset_v2` (a trava de dentro de cada ferramenta retirada ou legada); as de `plugins/multi_professional.py` e `plugins/multi_unit.py` chamam a mesma trava.
- `services/availability_windows.py` — `resolve_range` (valida e limita os dias), `FreeWindow`, `slots_to_windows`, `scan_free_windows` (só usa `services/availability.py`, a definição única de "livre").
- `services/calendar.py::CalendarService.default_slot_minutes` — propriedade de leitura.
- `ai/availability_tool.py::get_availability` — a leitura; `_clinic_today` é a costura do relógio.
- `ai/staging_tools.py` — `create_event_v2`, `cancel_event_v2` (nomes `create_event`/`cancel_event`, variante `blind_v2`).
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
- Custo no Google: uma leitura para achar os dias com horário livre mais uma por dia com horário, em sequência, até 14. As cegas não leem o Google.

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
```

- [ ] **Step 4: Pointer lines**

Acrescentar ao fim de cada arquivo a linha indicada, numa linha própria.

`docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md`:

```markdown
> TASK-030 P4: a IA v2 lê horários só por `get_availability` (janelas livres); `create_event`/`cancel_event` viram ferramentas cegas que só levantam este mesmo rascunho / o pedido de gerenciar (o toque do paciente é que marca/cancela) — ver `docs/CHECKPOINT_ia_get_availability.md`.
```

`docs/CHECKPOINT_plugins.md`:

```markdown
> TASK-030 P4: com `initial_flows.ai_draft_v2` ligado, `create_event_for_professional` (dobrada no `create_event` cego), `list_free_slots_for_professional` e `create_event_at_unit` saem do conjunto da IA — ver `docs/CHECKPOINT_ia_get_availability.md`.
```

`docs/CHECKPOINT_pseudonimizacao.md`:

```markdown
> TASK-030 P4: na v2 a pseudonimização é a SEGUNDA parede, não a defesa principal — as ferramentas v2 não leem dado de terceiro e só devolvem chaves declaradas (`ai/tool_output.py`), porque o guarda não mascara nome de terceiro desconhecido; prova com iscas em `tests/test_ai_v2_blindness.py` (ver `docs/CHECKPOINT_ia_get_availability.md`).
```

- [ ] **Step 5: Commit**

```bash
git add docs/CHECKPOINT_ia_get_availability.md docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md docs/CHECKPOINT_plugins.md docs/CHECKPOINT_pseudonimizacao.md
git diff --cached --stat
git commit -F - <<'EOF'
docs(checkpoint): TASK-030 P4 - get_availability, blind create/cancel and the v2 toolset

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Cobertura da spec (P4)

| Spec | Onde |
|---|---|
| Decisão do dono de 2026-10-03 (emenda de §2.4/§4.6): a IA mantém `create_event`/`cancel_event`, mas nunca vê nome nem dado de outro paciente | T6 (cegas), T7 (só a variante cega entra no v2), T8 (lista de saída), T9 (iscas) |
| Idem: as leitoras de ocupado continuam fora; `create_event_for_professional`/`create_event_at_unit` decididas | T4 (`AI_TOOLSET_V2_WITHHELD` + trava), T7 (`_kept_on_v2`); Decisão 6 |
| Idem: o modelo não compõe título/descrição; o servidor compõe no toque | T6 (sem argumento de texto livre; `test_a_title_or_a_name_the_model_adds_never_reaches_the_draft`); `_handle_confirmation` inalterado |
| Idem: cancelar só consulta do próprio paciente, por construção | T6 (`_own_upcoming` + referência), T1 (legado) |
| Correção de segurança para todas as clínicas: `cancel_event(event_id)` legado sem checagem de dono | T1 |
| §4.6 `get_availability(professional, service, day_from, day_to)` devolve só janelas `{day, start, end}`, no máximo 14 dias e 30 janelas | T2 (`resolve_range`, limites), T3 (`scan_free_windows`), T5 (a ferramenta) |
| §4.6 "agenda do profissional − reservas, duração do serviço" e "calculada pelo mesmo código dos seletores" | T3 (só `services/availability.py`), T5 (`BookingGate` sem armar, `resolve_booking_owner_id`, duração do serviço) |
| §4.6 "nenhum campo de evento (título, participantes, id, bloco ocupado) sai da camada de calendário" e §5.5 | T3 e T5 (canário em todos os campos; conjunto fechado de chaves), T8, T9 |
| §4.6 "as funções continuam para uso interno do fluxo" | as funções do calendário e do fluxo não mudam; as legadas continuam no v1 |
| §4.6 "ferramenta dentro do worker, não endpoint público" | T5/T6 (ferramentas do agente; nenhuma rota) |
| §4.6 "a IA pode mencionar horários que recebeu; o agendamento só pelo rascunho e pelo toque" / §5.8 | T6 (as cegas levantam o rascunho / o pedido de gerenciar), T7 (nenhum escritor legado no v2) |
| §5.6 "nenhum caminho da IA chega à confirmação sem horário re-derivado" | T6 (`create_event` cego = mesmo sentinel do rascunho → o resolvedor do P2/P3 re-deriva) |
| §4.8 "nenhuma entrada pode contornar o portão e as reservas" / §3 "porta dos fundos" | T1, T4, T6, T7 |
| §4.11 interruptor por tenant, desligado por padrão | T7 (`_ai_toolset_v2`, `toolset_v2=False` por padrão, testes de regressão do conjunto de hoje) |
| §6 "teste de formato de `get_availability`", isolamento de tenant, reservas | T5 (chaves, canário, dois tenants, reservas), T4/T7 (conjuntos exatos), T9 (outro tenant como isca) |
| §7 "`create_event` & cia. como porta dos fundos → P4" | T1, T4, T6, T7 |

## Deploy e liberação

Deploy nunca faz parte deste plano: só com pedido explícito do dono, a cada vez.

1. **Sem migração.** Nada novo em banco: o interruptor continua sendo `Tenant.initial_flows["ai_draft_v2"]` (P2b) e as ferramentas novas só leem (ou só devolvem ao fluxo).
2. **API e worker juntos** — README: "Deploy both services, or neither". As ferramentas e a montagem do conjunto rodam no worker; a API mapeia os mesmos modelos. Conferir `GET /build` dos dois: `deploy_parity` = `match`.
3. **O que muda no deploy para todas as clínicas:** a correção de segurança da Task 1 — o `cancel_event` legado passa a recusar evento que não é do paciente da conversa (`agent_tool_blocked` com `reason=not_patients_event` mostra cada recusa; a IA cai em `manage_existing_appointment`). Fora isso, nada visível: com o interruptor desligado o conjunto de ferramentas é o de hoje (testes nome por nome), `run_agent(toolset_v2=False)` é o padrão e as diferenças observáveis são o campo `toolset_v2=false` no log `agent_capabilities_resolved` e a propriedade `CalendarService.default_slot_minutes` (só leitura).
4. **O que liga por clínica, por um operador** (a mudança de dado do CHECKPOINT do P2b §3): as leitoras retiradas, `get_availability`, as cegas e a lista de saída — junto com o rascunho v2. **Só ligue depois de P3 e P5 deployados.** O prompt de hoje manda a IA chamar `create_event(start, end, summary, …)`, `list_free_slots` e `check_availability`; num turno v2 as leitoras não existem e o `create_event` tem outros argumentos (o `ToolNode` do LangGraph responde ao modelo com o erro de ferramenta inválida ou de argumento — o turno não cai, mas a conversa fica sem rumo). Primeiro na "Chrysostomo For Eyes"; observar nos logs `agent_capabilities_resolved` (`toolset_v2=true`, `capabilities`), `ai_availability_read` (contagens), `agent_tool_blocked` com `reason=toolset_v2` (deve ser zero: se aparecer, o prompt ainda cita uma ferramenta retirada), `agent_tool_output_dropped`/`agent_tool_output_undeclared` (devem ser zero: se aparecer, uma ferramenta tentou devolver algo fora da lista — investigar antes de seguir), `agent_availability_failed` e `agent_cancel_staging_failed`.
5. **Rollback imediato:** desligar a chave da clínica (efeito no turno seguinte; as ferramentas legadas voltam). Rollback de código: versão antiga nos DOIS serviços (desfaz também a correção do `cancel_event`); não há dado a desfazer.
