# IA entra em qualquer etapa — P4: `get_availability` (só horários livres) e a retirada das ferramentas de agenda — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Numa clínica com o interruptor `ai_draft_v2` ligado, a IA deixa de ter qualquer ferramenta que leia horário ocupado ou escreva na agenda (`check_availability`, `list_free_slots`, `create_event`, `cancel_event` e as irmãs dos plugins) e ganha UMA leitura — `get_availability` —, que devolve só janelas livres `{day, start, end}` no fuso da clínica (agenda do profissional − reservas, duração do serviço, no máximo 14 dias e 30 janelas), sem nenhum campo de evento. Com o interruptor desligado, o conjunto de ferramentas é exatamente o de hoje.

**Architecture:** Três peças. (1) `services/availability_windows.py`: puro e sem noção de "evento" — valida e limita o intervalo de dias, agrupa horários livres em janelas e varre a agenda **só** com `services/availability.py` (P2a), a mesma definição de "livre" dos seletores e do resolvedor. (2) `ai/availability_tool.py`: a ferramenta — resolve profissional e serviço como o rascunho resolve, obtém a agenda pela mesma resolução por tenant/profissional que o fluxo usa, desconta as reservas e devolve só janelas. (3) A montagem do conjunto, com as duas travas do repositório (estilo `_blocked_tenant_level`): `graph.effective_tools` não entrega as ferramentas retiradas (trava 1) e cada uma delas recusa sozinha quando o turno é v2 (trava 2); `_flow_handback_tools` entrega `get_availability`; o trabalhador decide o interruptor uma vez por turno e o passa a `run_agent(toolset_v2=...)`.

**Tech Stack:** Python 3.12+, LangChain tools / LangGraph, structlog, SQLAlchemy async (SQLite em memória nos testes), pytest + pytest-asyncio (`asyncio_mode = "auto"`), ruff.

**Spec:** `docs/superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md` — §4.6 (ferramentas da IA depois da mudança), §5 critério 5 (a IA nunca recebe título, participante nem id de evento), §4.8 (o que continua fechado), §4.11 (interruptor por tenant), §7 (linha P4: `create_event` & cia. como porta dos fundos).

**Depende de:** P1 (`2026-10-02-ia-p1-registro-de-handbacks.md`), **P2a** (`2026-10-02-ia-p2a-rascunho-coluna-e-resolvedor.md`: `services/availability.py`) e **P2b** (`2026-10-02-ia-p2b-handbacks-ferramenta-e-estado.md`: `flow_router.ai_draft_v2_enabled`, `graph._tool_cache_key`, `_flow_handback_tools` com `draft_tool`) executados neste worktree. Não depende do P3 e não edita `flow_router.py` nem `sentinels.py`. O P5 (`ai/prompts.py`) depende deste plano: a seção "Para o P5" abaixo lista os nomes e o texto exatos.

## Global Constraints

- Worktree `C:\TECH\BRAIN-worktrees\TASK-030\secretarIA` (branch da TASK-030). Tarefas **sequenciais**: P1 → P2a → P2b → (P3) → P4 → P5 editam arquivos em comum (`ai/tools.py`, `ai/graph.py`, `workers/shared/llm_context.py`); nunca dois agentes ao mesmo tempo.
- Testes rodam do **Git Bash**, na raiz do worktree: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest <arquivo> -q` (PowerShell não consegue setar variável vazia; nunca `pytest` solto).
- Nunca `ruff format .`. `uvx ruff format <arquivo>` só nos arquivos **criados** por este plano (`services/availability_windows.py`, `ai/availability_tool.py` e os cinco arquivos de teste novos), e ANTES do `uvx ruff check` (ele quebra as linhas longas que o `E501` acusaria). `uvx ruff check <arquivos tocados>` sempre. Em arquivo existente: antes de editar, anote `uvx ruff format --diff <arquivo> | grep -c '^@@'`; depois de editar, a contagem não pode aumentar (escreva no estilo ruff: o P1 mediu `graph.py` 2; confira os demais antes de começar).
- A árvore está em CRLF (índice em LF). Antes de cada commit, `git diff --cached --stat` mostra só as linhas do passo; se um arquivo existente aparecer inteiro como alterado, `git restore --staged <arquivo>` e refaça a edição com âncoras pequenas. Arquivo novo criado pelo plano: LF, como o P2 fez.
- Camadas: api → workers → services/ai → models → core. `services/` nunca importa `ai/` nem `workers/` (`availability_windows.py` só importa `services/availability.py`). `ai/availability_tool.py` importa `services/*` e as variáveis de contexto de `ai/tools.py`; `plugins/multi_professional.py` só por import TARDIO (os plugins importam `ai.tools`; o contrário em nível de módulo faria ciclo). `workers/shared/llm_context.py` importa `ai.availability_tool`.
- **Nenhum campo de evento sai da camada de calendário:** título, participante, id, descrição, local, link, bloco ocupado. A resposta da ferramenta só tem as chaves documentadas (`windows`, `timezone`, `slot_minutes`, `day_from`, `day_to`, `professional`, `clamped`, `note`; cada janela só `day`/`start`/`end`); o erro é `{"error": "<frase>"}`.
- **Segredos e tenant:** o refresh token do Google só é decriptado em `services/tenant_config.py`; a ferramenta obtém a agenda pela resolução que o fluxo usa (`plugins.multi_professional._professional_calendar` → `resolve_professional_calendar`, ou a agenda que o `run_agent` montou do mesmo config) e **nunca** pelo calendário de ambiente que `ai/tools.py::_get_calendar` usa como atalho de desenvolvimento. Roster, catálogo, agenda e reservas são lidos para `_tenant_id_ctx`, o tenant DESTE turno.
- **Fuso:** "hoje" e todo horário saem no fuso da clínica (`calendar.tzinfo`), nunca em UTC (skills `naive-timestamp-serialization`, `date-derived-ui-labels`: uma âncora só; testes com relógio fixo, nunca `datetime.now()` solto).
- Sem PII nem segredo em log: ids, contagens, booleanos e códigos de motivo. Nunca nome de profissional/serviço, dia, horário nem texto do paciente. A frase de erro para o modelo nunca ecoa um valor recebido (pode ser fala do paciente).
- Não tocar `ai/prompts.py` (dono: P5). As frases de erro/nota que o modelo lê são em português; código, comentários e mensagens de commit em inglês.
- Com o interruptor desligado nada muda: `toolset_v2` e `_ai_toolset_v2_ctx` têm padrão `False`; os testes de regressão da Task 3 e da Task 5 fixam o conjunto de hoje, nome por nome.
- Cada commit termina com `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Commit local permitido; push e deploy só com pedido explícito do dono.

## Review Focus

Cada linha tem o teste que a fixa, na tarefa dona do código.

- **O título de um evento nunca chega à IA, nem nenhum outro campo dele** — string canário em título, participante, id, descrição, local e link; nada dela na resposta, e nenhuma chave fora do conjunto documentado (T2 `test_a_busy_event_removes_its_time_and_none_of_its_fields_survive`; T4 `test_a_calendar_event_never_reaches_the_ai_in_any_field`, `test_the_answer_is_windows_and_only_the_documented_keys`; T1 `test_a_window_exposes_exactly_day_start_end`).
- **Médico sem nenhuma janela livre** → lista vazia e uma frase, não erro; e custa UMA leitura de agenda (T4 `test_a_doctor_with_no_free_window_gets_an_empty_list_and_a_sentence`; T2 `test_an_agenda_with_nothing_free_costs_one_calendar_read`).
- **Intervalo que atravessa "hoje" com horas já passadas** → só o que ainda não começou (T4 `test_today_with_hours_already_passed_goes_through_the_real_calendar`; T2 `test_a_range_that_starts_today_drops_the_hours_that_already_passed`, com o `CalendarService` real).
- **Fuso da clínica ≠ UTC e a virada de horário de verão** — o "hoje" omitido é o da clínica, no mesmo horário UTC em datas diferentes de NY (T4 `test_an_omitted_first_day_is_the_clinics_today_not_the_servers`); o dia da virada lê como os vizinhos (T2 `test_a_dst_transition_day_reads_like_its_neighbours`; T1 `test_the_wall_clock_is_read_as_is_whatever_the_utc_offset`).
- **Dois tenants** — médico de mesmo nome em duas clínicas lê a agenda de cada uma, em paralelo; reserva de um tenant não esconde horário do outro (T4 `test_two_tenants_with_a_same_named_doctor_each_read_their_own_agenda`, `test_another_tenants_tenant_level_hold_hides_nothing`, `test_a_config_of_another_tenant_is_refused_before_anything_is_read`).
- **Profissional que o tenant não tem** → erro curto, sem montar agenda nenhuma e sem ecoar o nome (T4 `test_a_doctor_the_tenant_does_not_have_is_unknown_and_no_agenda_is_built`; tabela `test_a_multi_professional_request_that_cannot_be_resolved_is_a_short_error`).
- **Intervalos absurdos** — 365 dias (cortado em 14 e avisado), negativo, passado, ano 9999 (sem estourar a aritmética de datas), formato `20261008`/`2026-W41-4`/dígitos árabes, texto de 5000 caracteres (T1 `test_an_unusable_range_is_refused_with_a_stable_code`; T4 `test_an_unusable_day_range_is_an_error_dict_and_never_reads_the_agenda`, `test_a_365_day_range_is_clamped_and_the_answer_says_so`, `test_the_range_stops_where_booking_stops`).
- **Falha da API de calendário** — decisão e teste: queda do Google (token revogado incluído) **propaga como hoje** e `run_agent` a transforma na passagem para humano + alerta ao dono; QUALQUER outra falha vira `{"error": ...}` com log só do tipo (T4 `test_a_calendar_outage_propagates_like_every_other_calendar_read`, `test_any_other_failure_is_an_error_dict_not_a_crash`; T5 `test_a_calendar_outage_inside_get_availability_reaches_the_handover_sentinel`).
- **Reservas** — o horário segurado por outra conversa não é oferecido; o segurado pela própria conversa sim; reserva de outro médico ou de outro tenant não esconde nada (T4 `test_a_slot_another_conversation_is_holding_is_not_offered`, `test_the_patients_own_hold_does_not_hide_the_slot_from_them`, `test_a_hold_on_another_doctor_hides_nothing_on_this_doctors_agenda`).
- **Interruptor desligado = conjunto de hoje** (T3 `test_switch_off_leaves_every_base_tool_as_it_was`; T5 `test_switch_off_is_exactly_todays_composition` e `test_a_clinic_without_the_switch_keeps_todays_toolset`); **ligado = nenhuma ferramenta de agenda**, nem se um chamador a passar (T5 `test_switch_on_swaps_the_agenda_tools_for_get_availability`, `test_switch_on_strips_a_withheld_tool_even_when_a_caller_passes_it`); **ferramenta nova de calendário não passa em branco** (T3 `test_every_calendar_touching_tool_is_classified`).
- **Pseudonimização** — a ferramenta é entregue atrás do guarda do limite de ferramentas e o resultado não é alterado por ele (T4 `test_the_agent_builds_the_tool_behind_the_guard`, `test_the_pseudonymization_guard_wraps_the_tool_and_leaves_windows_intact`).

## Decisões deste plano (mudar só se o dono discordar)

1. **Forma da resposta.** O spec fixa `{day, start, end}` por janela e `timezone`. Este plano acrescenta chaves documentadas e testadas: `slot_minutes` (a duração usada), `day_from`/`day_to` (o intervalo que a lista cobre de fato), `professional` (o nome do profissional cuja agenda foi lida, inclusive quando ele foi inferido do serviço; ausente numa clínica sem profissionais), e — só quando existirem — `clamped` (lista de códigos: `max_days`, `booking_window`, `max_windows`) e `note` (uma frase em português que diz o que a lista NÃO cobre, ou que não há horário livre). O teste do critério 5 do spec falha com qualquer chave fora desse conjunto e com qualquer chave extra dentro de uma janela.
2. **Corte (clamp).** No máximo 14 dias por chamada, 30 janelas no total (as mais cedo primeiro) **e nunca além do horizonte de agendamento** (`flow_router.DAY_PICKER_WINDOW_DAYS` = 20 dias): uma janela que o resolvedor do P2 descartaria como `out_of_window` não pode ser oferecida pela IA. Só um `day_to` EXPLÍCITO que passe do limite conta como corte (`clamped` + `note`); `day_to` omitido vai até o maior intervalo permitido, sem aviso. Corte por janelas: `day_to` da resposta passa a ser o dia da última janela devolvida e `clamped` ganha `max_windows`. Para saber que há mais de 30 a varredura lê um dia a mais, e para.
3. **Formato do erro.** `{"error": "<frase em português>"}`, igual ao resto do repositório, sempre recuperável pelo modelo no mesmo turno (nunca exceção). A frase lista os nomes válidos de profissional/serviço (dado da clínica) e **nunca repete** o valor recebido. O motivo vai para o log como enum em `agent_tool_blocked` (`tool="get_availability"`, `reason=...`): `no_clinic`, `tenant_mismatch`, `no_calendar`, `no_professionals`, `professional_required`, `professional_unknown`, `professional_ambiguous`, `service_unknown`, `no_professional_offers_service`, `several_professionals_offer_service`, `bad_day_format`, `past_day`, `reversed_range`, `beyond_window`.
4. **Falha do calendário.** `CalendarUnavailableError` (e `GoogleTokenRevokedError`) **propaga**, como em `check_availability`/`list_free_slots` hoje: `run_agent` devolve `CALENDAR_UNAVAILABLE_SENTINEL`, o trabalhador entrega a conversa a uma pessoa e alerta o dono (e-mail com silêncio de `CALENDAR_ALERT_SILENCE_SECONDS`). Engolir isso num `dict` esconderia do dono um token revogado e deixaria o paciente numa conversa sem agenda. Todo o resto (credencial ausente, erro de banco, bug) vira `{"error": "Não consegui consultar a agenda agora ... NUNCA invente horários."}` + `agent_availability_failed(error_type=...)`, nunca a mensagem da exceção.
5. **Sem `service`.** Usa a duração padrão da agenda (`CalendarService.default_slot_minutes`, propriedade nova que só expõe o que o calendário já usa) e a devolve em `slot_minutes`. Pedir o serviço antes de qualquer "tem horário?" criaria uma pergunta a mais para o paciente.
6. **Retiradas além das cinco nomeadas no spec** ("e similares"): `list_free_slots_for_professional` (leitor de horários SEM desconto de reservas: ofereceria horário que outro paciente está confirmando) e `create_event_at_unit` (escritor do addon `multi_unit`). Consequências conhecidas, listadas no CHECKPOINT: a IA perde o `professional_context` que o leitor antigo anexava ao resultado (o bloco "SOBRE O PROFISSIONAL" do prompt continua) e, com o addon `multi_unit`, nenhum agendamento passa a registrar a unidade pela IA (o fluxo de botões nunca registrou). `list_units`, `list_professionals`, `select_professional_and_continue` ficam (P5 remove a última).
7. **Dono das reservas** = `resolve_booking_owner_id` (a regra única dos agendamentos; o mesmo `_hold_owner` do P2a): o profissional escolhido numa clínica multi; o único profissional numa clínica de um só; `None` numa clínica sem profissionais. O portão de reservas é criado SEM armar (a ferramenta nunca reserva) e exclui a reserva da própria conversa, como os seletores.
8. **De onde vem a agenda.** Um profissional ou nenhum → a agenda que `run_agent` montou (`_calendar_ctx`, do mesmo `TenantRuntimeConfig` que já resolve o profissional único); vários → `_professional_calendar` (única resolução por profissional). Sem agenda no contexto ou sem config numa clínica multi → recusa `no_calendar`, nunca o calendário de ambiente.
9. **Interruptor.** `llm_context._ai_toolset_v2(tenant)` = `flows_enabled(tenant)` E `ai_draft_v2_enabled(tenant)`: o agente v2 não tem ferramenta de agendar, só devolve ao fluxo — uma clínica sem as ferramentas de devolução jamais pode perder `create_event`. Lido uma vez por turno em `orchestrator.py` e passado a `run_agent(toolset_v2=...)`.
10. **Janela não é grade.** O fluxo só aceita horários na grade da agenda (início da janela + múltiplos de `slot_minutes`). Se a IA sugerir 09:10 dentro de uma janela 08:00–10:00 de 40 minutos, o resolvedor do P2 cai na lista de horários daquele dia (critério 3 do spec): comportamento aceito, sem erro. O texto da ferramenta diz "trecho contínuo em que cabe uma consulta" e devolve `slot_minutes` para o modelo propor horários coerentes.

## File Structure

- Create `src/secretaria/services/availability_windows.py` — intervalo de dias (valida/limita), `FreeWindow`, `slots_to_windows`, `scan_free_windows` (T1, T2).
- Modify `src/secretaria/services/calendar.py` — propriedade `CalendarService.default_slot_minutes` (T2).
- Modify `src/secretaria/ai/tools.py` — `_ai_toolset_v2_ctx`, `AI_TOOLSET_V2_WITHHELD`, `TOOL_BLOCK_TOOLSET_V2`, `_blocked_by_toolset_v2` e a trava dentro de `check_availability`, `list_free_slots`, `create_event`, `cancel_event` (T3).
- Modify `src/secretaria/plugins/multi_professional.py`, `src/secretaria/plugins/multi_unit.py` — a trava dentro de `list_free_slots_for_professional`, `create_event_for_professional`, `create_event_at_unit` (T3).
- Create `src/secretaria/ai/availability_tool.py` — `get_availability` (T4).
- Modify `src/secretaria/ai/graph.py` — `base_tools_for(toolset_v2=)`, `effective_tools`, `build_agent(toolset_v2=)`, `invoke_agent`, `run_agent(toolset_v2=)` (T5).
- Modify `src/secretaria/workers/shared/llm_context.py` — `_ai_toolset_v2`, `get_availability` em `_flow_handback_tools` (T5).
- Modify `src/secretaria/workers/orchestrator.py` — passa `toolset_v2=` a `run_agent` (T5).
- Tests: Create `tests/test_availability_windows.py` (T1), `tests/test_availability_windows_scan.py` (T2), `tests/test_ai_toolset_v2_locks.py` (T3), `tests/test_get_availability_tool.py` (T4), `tests/test_ai_toolset_v2.py` (T5); Modify `tests/test_bot_reply_gating.py` e `tests/test_set_booking_draft_v2.py` (T5).
- Docs (T6): Create `docs/CHECKPOINT_ia_get_availability.md`; uma linha de ponteiro em `docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md`, `docs/CHECKPOINT_plugins.md` e `docs/CHECKPOINT_pseudonimizacao.md`.

## Interfaces

O que este plano **consome** (já existe depois de P1 + P2a + P2b, ou no código atual):

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
# ai/graph.py (P2b)   _tool_cache_key(tool) -> str          # the cache key stays the NAMES of the effective tool set
# workers/shared/llm_context.py (P2b)   _flow_handback_tools(tenant, topology, plugin_tools) with `draft_tool`
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
_ai_toolset_v2_ctx: ContextVar[bool]                       # default False; set by graph.run_agent
AI_TOOLSET_V2_WITHHELD: tuple[str, ...]                    # the 7 names the AI loses
TOOL_BLOCK_TOOLSET_V2 = "toolset_v2"
def _blocked_by_toolset_v2(tool_name: str) -> dict | None  # second lock; None = allowed

# ai/availability_tool.py
get_availability                                           # StructuredTool, args professional, service, day_from, day_to (all str = "")
BLOCK_* enums; def _clinic_today(tz) -> date               # the clock seam tests pin

# ai/graph.py
base_tools_for(topology: str, *, toolset_v2: bool = False) -> tuple
effective_tools(topology: str, extra_tools: Sequence = (), *, toolset_v2: bool = False) -> list
build_agent(extra_tools=(), topology=BOOKING_TOPOLOGY_UNKNOWN, *, toolset_v2: bool = False)
run_agent(..., conversation_state=None, toolset_v2: bool = False)

# workers/shared/llm_context.py
def _ai_toolset_v2(tenant) -> bool                         # flows_enabled and ai_draft_v2_enabled
```

## Para o P5 (o que a IA passa a ver — texto e nomes exatos)

**Entra** (só com o interruptor ligado): `get_availability(professional: str = "", service: str = "", day_from: str = "", day_to: str = "")`.

**Saem** (só com o interruptor ligado; `AI_TOOLSET_V2_WITHHELD`): `check_availability`, `list_free_slots`, `create_event`, `cancel_event`, `list_free_slots_for_professional`, `create_event_for_professional`, `create_event_at_unit`.

**Ficam** (P5 remove depois `start_guided_booking` e `select_professional_and_continue`, e então atualiza os conjuntos exatos de `tests/test_ai_toolset_v2.py` e `_HANDBACKS` de `tests/test_ai_toolset_v2_locks.py`): `show_main_menu`, `iniciar_pre_consulta`, `list_patient_appointments`, `list_professionals`, `select_professional_and_continue`, `list_units`, `manage_existing_appointment`, `set_booking_draft`, `request_human_handoff`, `start_guided_booking` (só fora de clínica multi).

**Texto que o modelo lê** — o docstring de `get_availability` (é o `description` da ferramenta; o nome é `get_availability` e os quatro argumentos são `str` com padrão `""`):

```text
Consulta os HORÁRIOS LIVRES da agenda de um profissional - só isso. Devolve janelas
    {day, start, end} no fuso da clínica (campo `timezone`), já sem os horários que outros
    pacientes estão reservando. Nunca devolve compromissos, nomes nem horários ocupados: o
    que não está na lista não está livre. Use para responder "tem horário?" ou "tem vaga
    semana que vem?" e para sugerir opções. Você NÃO agenda, remarca nem cancela por aqui:
    se o paciente quer marcar, preencha `day` e `time` em set_booking_draft e o fluxo confere
    e conduz.

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

**Trechos de `ai/prompts.py` que deixam de valer na v2** (citados por texto, não por linha): a regra de gerenciar ("NUNCA use check_availability/create_event/cancel_event"), "chame list_free_slots(date, ...) e renderize via [SLOTS]" (as janelas **não** são linhas de `[SLOTS]`: viram texto), "check_availability(start, end) para esse slot", "Só depois de uma confirmação clara do paciente chame create_event", "prefira list_free_slots", "create_event retornou sucesso" / "create_event devolveu — copie-o inteiro" (a IA não cria evento nem recebe `patient_calendar_link`), o parágrafo da clínica multi que lista as ferramentas não oferecidas, "NUNCA confirme um agendamento sem ter chamado create_event" e "NUNCA invente horários sem chamar check_availability ou list_free_slots". Enquanto o P5 não trocar o prompt, **não ligue o interruptor** em nenhuma clínica (ver "Deploy e liberação").

---

### Task 1: `services/availability_windows.py` — o intervalo de dias e o agrupamento em janelas (puro)

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

- [ ] **Step 3: Write the module (sem a varredura, que é da Task 2)**

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

### Task 2: A varredura de janelas (`scan_free_windows`) e `CalendarService.default_slot_minutes`

**Files:**
- Modify: `src/secretaria/services/availability_windows.py` (imports; `WindowScan`, `scan_free_windows` ao fim)
- Modify: `src/secretaria/services/calendar.py` (propriedade `default_slot_minutes`)
- Test: `tests/test_availability_windows_scan.py`

**Interfaces:**
- Consumes: Task 1; P2a `days_with_free_slots`, `free_slots_for_day`, `Window`; `CalendarService` real (`list_available_days`, `list_free_slots`, `tzinfo`).
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

### Task 3: A trava dentro de cada ferramenta de agenda (`_blocked_by_toolset_v2`) — a segunda trava

**Files:**
- Modify: `src/secretaria/ai/tools.py` (`_ai_toolset_v2_ctx`; constantes e `_blocked_by_toolset_v2`; a trava em `check_availability`, `list_free_slots`, `create_event`, `cancel_event`)
- Modify: `src/secretaria/plugins/multi_professional.py` (import; `list_free_slots_for_professional`, `create_event_for_professional`)
- Modify: `src/secretaria/plugins/multi_unit.py` (import; `create_event_at_unit`)
- Test: `tests/test_ai_toolset_v2_locks.py`

**Interfaces:**
- Consumes: `ai/tools.py::_tenant_id_ctx`, `logger`; as ferramentas existentes.
- Produces: `_ai_toolset_v2_ctx`, `AI_TOOLSET_V2_WITHHELD`, `TOOL_BLOCK_TOOLSET_V2`, `_blocked_by_toolset_v2` (assinaturas em "Interfaces").

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_ai_toolset_v2_locks.py`:

```python
"""The v2 toolset's second lock: each withheld tool refuses by itself (TASK-030 P4, spec §4.6).

On a turn that runs on the AI toolset v2 the agent is built without the tools that read a busy
interval or write to the agenda (`ai/tools.py::AI_TOOLSET_V2_WITHHELD`) - lock one, asserted in
tests/test_ai_toolset_v2.py. This file pins lock two, the repo's own pattern
(`_blocked_tenant_level`): a withheld tool that arrives anyway - a stale cached graph, a
hand-rolled call, a future caller - returns an error BEFORE any Google call or DB write.
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
_WITHHELD_CALLS = [
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


def test_the_tested_tools_are_exactly_the_withheld_list():
    assert {t.name for t, _ in _WITHHELD_CALLS} == set(ai_tools.AI_TOOLSET_V2_WITHHELD)
    assert set(ai_tools.AI_TOOLSET_V2_WITHHELD) == _BUSY_AND_WRITE


@pytest.mark.parametrize(("tool", "args"), _WITHHELD_CALLS, ids=lambda v: getattr(v, "name", ""))
async def test_a_withheld_tool_refuses_on_a_v2_turn_without_touching_the_calendar(
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


@pytest.mark.parametrize(
    ("tool", "args"), _WITHHELD_CALLS[:4], ids=lambda v: getattr(v, "name", "")
)
async def test_switch_off_leaves_every_base_tool_as_it_was(tool, args):
    """The lock is armed only by the v2 context: with it off the tool reaches its own logic
    (here, the calendar - which this test makes refuse loudly)."""
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
    """A tool added tomorrow that reads or writes the agenda must be put on the withheld
    list before it can reach a v2 AI - this fails until someone classifies it."""
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
Expected: 5 failed, 7 errors, 1 passed — `AttributeError: module 'secretaria.ai.tools' has no attribute '_ai_toolset_v2_ctx'` / `'AI_TOOLSET_V2_WITHHELD'`. (O que passa já é `test_every_calendar_touching_tool_is_classified`: ele fixa o universo de hoje e só começa a proteger quando alguém criar uma ferramenta nova.)

- [ ] **Step 3: Implement the lock**

Em `src/secretaria/ai/tools.py` (uma âncora de cada vez):

1. Logo antes da linha `# Note on calendar outages: a tool that hits CalendarUnavailableError simply`, inserir:

```python
# True when THIS turn runs on the AI toolset v2 (TASK-030 P4, per-clinic switch
# `flow_router.ai_draft_v2_enabled`): the agent was built without the tools that read busy
# intervals or write to the agenda (`AI_TOOLSET_V2_WITHHELD`) and has `get_availability`
# instead. Set by graph.run_agent; read by `_blocked_by_toolset_v2`, the second lock inside
# each withheld tool. Lives here, like the vars above, so a plugin tool module can read it
# without importing graph.py. The default (False) keeps every tool exactly as it was.
_ai_toolset_v2_ctx: ContextVar[bool] = ContextVar("_ai_toolset_v2", default=False)

```

2. Logo antes de `def _sole_professional_id() -> UUID | None:`, inserir:

```python
# The tools the AI loses on the v2 toolset: everything that reads a BUSY interval or lists
# raw slots (`check_availability`, `list_free_slots`, `list_free_slots_for_professional` -
# the second one has no holds subtracted, so it would offer a slot another patient is
# confirming) and everything that WRITES to the agenda (`create_event`,
# `create_event_for_professional`, `create_event_at_unit`, `cancel_event`). Booking happens
# only through the draft and the patient's tap; availability only through
# `get_availability`. ai/graph.py withholds these by NAME (lock one);
# `_blocked_by_toolset_v2` makes each of them refuse again if one arrives anyway (lock two).
AI_TOOLSET_V2_WITHHELD = (
    "check_availability",
    "list_free_slots",
    "create_event",
    "cancel_event",
    "list_free_slots_for_professional",
    "create_event_for_professional",
    "create_event_at_unit",
)

TOOL_BLOCK_TOOLSET_V2 = "toolset_v2"

_TOOLSET_V2_ERROR = (
    "Esta ferramenta não está disponível para você nesta clínica. Para ver horários livres "
    "use get_availability; para marcar, use set_booking_draft; para remarcar ou cancelar, "
    "use manage_existing_appointment."
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

e igual para `"list_free_slots"`, `"create_event"` e `"cancel_event"` (o `if blocked is not None: return blocked` original que vem logo depois de cada uma fica como está).

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

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_ai_toolset_v2_locks.py tests/test_agent_tool_enforcement.py tests/test_multi_professional_plugin.py tests/test_multi_unit_plugin.py tests/test_ai_tools_cancel_money.py tests/test_pii_pseudonymization.py tests/test_booking_owner_persistence.py -q`
Expected: PASS (13 novos; as travas existentes e o comportamento de hoje das ferramentas, inalterados).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format tests/test_ai_toolset_v2_locks.py
uvx ruff check src/secretaria/ai/tools.py src/secretaria/plugins/multi_professional.py src/secretaria/plugins/multi_unit.py tests/test_ai_toolset_v2_locks.py
for f in src/secretaria/ai/tools.py src/secretaria/plugins/multi_professional.py src/secretaria/plugins/multi_unit.py; do echo "$f $(uvx ruff format --diff $f 2>/dev/null | grep -c '^@@')"; done   # none may exceed its count before editing
git add src/secretaria/ai/tools.py src/secretaria/plugins/multi_professional.py src/secretaria/plugins/multi_unit.py tests/test_ai_toolset_v2_locks.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(ai): every agenda tool refuses by itself on a v2 turn

The AI toolset v2 withholds seven tools (AI_TOOLSET_V2_WITHHELD). This is the second lock, the
repo's own pattern: each of them returns an error before any Google call or DB write when the
turn's _ai_toolset_v2_ctx is on. Off by default: nothing changes until graph.run_agent sets it.
A guard test fails when a new calendar-touching tool is added without being classified.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 4: `ai/availability_tool.py` — a ferramenta `get_availability`

**Files:**
- Create: `src/secretaria/ai/availability_tool.py`
- Test: `tests/test_get_availability_tool.py`

**Interfaces:**
- Consumes: Tasks 1–2 (`resolve_range`, `scan_free_windows`, `RangeError`, `CLAMP_*`, `MAX_*`, `CalendarService.default_slot_minutes`); `ai/tools.py` ContextVars e `_effective_service_catalog`; `plugins.multi_professional` (import tardio); `BookingGate`; `flow_router.DAY_PICKER_WINDOW_DAYS`; `booking_scope` helpers.
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
        agent = graph.build_agent([at.get_availability], "sole", toolset_v2=True)
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
(ai/tools.py `AI_TOOLSET_V2_WITHHELD`). What it keeps, to answer "tem horário semana que
vem?", is this tool, and the answer has exactly one shape:

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
    semana que vem?" e para sugerir opções. Você NÃO agenda, remarca nem cancela por aqui:
    se o paciente quer marcar, preencha `day` e `time` em set_booking_draft e o fluxo confere
    e conduz.

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

### Task 5: A montagem do conjunto — `effective_tools`, o interruptor por turno e `get_availability` entregue

**Files:**
- Modify: `src/secretaria/ai/graph.py`
- Modify: `src/secretaria/workers/shared/llm_context.py`
- Modify: `src/secretaria/workers/orchestrator.py`
- Test: `tests/test_ai_toolset_v2.py`; Modify `tests/test_bot_reply_gating.py`, `tests/test_set_booking_draft_v2.py`

**Interfaces:**
- Consumes: Tasks 3–4 (`AI_TOOLSET_V2_WITHHELD`, `_ai_toolset_v2_ctx`, `get_availability`); P2b (`_tool_cache_key`, `draft_tool` em `_flow_handback_tools`, `ai_draft_v2_enabled`).
- Produces: `base_tools_for(..., toolset_v2=)`, `effective_tools`, `build_agent(..., toolset_v2=)`, `run_agent(..., toolset_v2=)`, `_ai_toolset_v2`.

- [ ] **Step 1: Write the failing tests**

1. Criar `tests/test_ai_toolset_v2.py`:

```python
"""The AI toolset v2: what the AI loses and what it gains, by exact tool names (TASK-030 P4).

On a clinic with `initial_flows["ai_draft_v2"] = true` the AI no longer has a tool that reads a
busy interval or writes to the agenda (`ai/tools.py::AI_TOOLSET_V2_WITHHELD`); it reads free
windows through `get_availability` and books only by handing back to the flow. With the switch
off, the toolset is exactly what it was.

This is lock one: the tool set (`graph.effective_tools`, asserted by exact tool NAMES, the way
tests/test_agent_tool_enforcement.py does it for topologies). Lock two - each withheld tool
refusing by itself - is tests/test_ai_toolset_v2_locks.py. `create_react_agent` is replaced by a
recording fake: nothing here reaches OpenAI, Google or a DB.
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
# Lock one: the tool set, by exact names
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
    assert _names(_turn_tools(TENANT_OFF, topology)) == _todays_tool_names(topology)


@pytest.mark.parametrize("topology", TOPOLOGIES)
def test_switch_on_swaps_the_agenda_tools_for_get_availability(topology):
    names = _names(_turn_tools(TENANT_ON, topology))
    expected = _SCOPE_FREE | _PLUGIN_TOOLS_KEPT | _HANDBACKS | {"get_availability"}
    if topology != BOOKING_TOPOLOGY_MULTI:
        expected |= {"start_guided_booking"}
    assert names == expected
    assert names.isdisjoint(_BUSY_AND_WRITE)


@pytest.mark.parametrize("topology", TOPOLOGIES)
def test_switch_on_strips_a_withheld_tool_even_when_a_caller_passes_it(topology):
    extras = [*reg.agent_tools_for(ALL_ADDONS), ai_tools.create_event, ai_tools.check_availability]
    names = _names(graph.effective_tools(topology, extras, toolset_v2=True))
    assert names.isdisjoint(_BUSY_AND_WRITE)


def test_without_the_addons_the_v2_set_still_has_the_way_back_to_the_flow():
    names = _names(_turn_tools(TENANT_ON, BOOKING_TOPOLOGY_SOLE, addons=_summary()))
    assert names == _SCOPE_FREE | _HANDBACKS | {"start_guided_booking", "get_availability"}


@pytest.mark.parametrize("topology", TOPOLOGIES)
def test_get_availability_is_offered_only_through_the_switch(topology):
    assert get_availability in _flow_handback_tools(TENANT_ON, topology, [])
    assert get_availability not in _flow_handback_tools(TENANT_OFF, topology, [])
    assert get_availability not in _flow_handback_tools(None, topology, [])


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
        extra_tools=[ai_tools.create_event, get_availability],
        booking_topology=BOOKING_TOPOLOGY_SOLE,
        toolset_v2=True,
    )
    (fields,) = [f for e, f in log.events if e == "agent_capabilities_resolved"]
    assert fields["toolset_v2"] is True
    assert fields["capabilities"] == sorted(_SCOPE_FREE | {"get_availability"})


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
    tenant, patient, conversation = await _make_conversation(db)
    await _flows_of(db, tenant, {"ai_draft_v2": True})

    (call,) = await _run_send_bot_reply_capturing_run_agent(
        monkeypatch, conversation, patient, _LLM_ESCAPE
    )

    assert call["toolset_v2"] is True
    assert "get_availability" in {t.name for t in call["extra_tools"]}


async def test_a_clinic_without_the_switch_keeps_todays_toolset(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    tenant, patient, conversation = await _make_conversation(db)
    await _flows_of(db, tenant, {"ai_draft_v2": "true"})  # only the JSON literal true counts

    (call,) = await _run_send_bot_reply_capturing_run_agent(
        monkeypatch, conversation, patient, _LLM_ESCAPE
    )

    assert call["toolset_v2"] is False
    assert "get_availability" not in {t.name for t in call["extra_tools"]}
```

3. Em `tests/test_set_booking_draft_v2.py` (criado pelo P2b, Task B3), em `test_the_clinic_switch_picks_the_draft_tool`, trocar a última asserção

```python
    assert sorted(t.name for t in on) == sorted(t.name for t in off)
```

por

```python
    # TASK-030 P4: the v2 set also carries the one agenda read the AI keeps.
    assert sorted(t.name for t in on) == sorted([*(t.name for t in off), "get_availability"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_ai_toolset_v2.py tests/test_bot_reply_gating.py tests/test_set_booking_draft_v2.py -q`
Expected: erro de coleta em `test_ai_toolset_v2.py` — `ImportError: cannot import name '_ai_toolset_v2' from 'secretaria.workers.shared.llm_context'`. (Sem ele: os dois testes novos do gating falham com `KeyError: 'toolset_v2'`.)

- [ ] **Step 3: `ai/graph.py`**

Em `src/secretaria/ai/graph.py` (âncoras do estado DEPOIS do P2b):

1. No import de `secretaria.ai.tools`, inserir `    AI_TOOLSET_V2_WITHHELD,` antes de `    BookingDraftRequested,` e `    _ai_toolset_v2_ctx,` antes de `    _booking_topology_ctx,`.

2. Trocar a assinatura `def base_tools_for(topology: str) -> tuple:` por:

```python
def base_tools_for(topology: str, *, toolset_v2: bool = False) -> tuple:
```

3. Em `base_tools_for`, logo depois da linha `    precisely the wrong agenda.` (fim do último parágrafo do docstring), inserir uma linha em branco e o parágrafo:

```python

    On the AI toolset v2 (TASK-030 P4, `toolset_v2`) NO topology gets a tenant-level
    calendar tool: the AI reads availability through `get_availability` and books only
    through the draft hand-back, so the base set is the scope-free one everywhere.
```

4. Trocar o corpo

```python
    if topology == BOOKING_TOPOLOGY_MULTI:
        return _SCOPE_FREE_TOOLS
    return _BASE_TOOLS
```

por (a função nova vem logo depois, antes do comentário `# Compiled agent cache, keyed by ...`):

```python
    if toolset_v2 or topology == BOOKING_TOPOLOGY_MULTI:
        return _SCOPE_FREE_TOOLS
    return _BASE_TOOLS


def effective_tools(topology: str, extra_tools: Sequence = (), *, toolset_v2: bool = False) -> list:
    """THE tool list of one turn: base set + the tenant's extra tools.

    One assembly for `build_agent` and for the `agent_capabilities_resolved` log, so what
    is logged is what ran. With `toolset_v2` the extras are also stripped of every tool in
    `AI_TOOLSET_V2_WITHHELD` (the plugin booking/free-slot tools): lock one of two, the
    second being each tool's own refusal (`ai/tools.py::_blocked_by_toolset_v2`). With it
    off this is exactly `[*base_tools_for(topology), *extra_tools]`, as it always was.
    """
    if not toolset_v2:
        return [*base_tools_for(topology), *extra_tools]
    kept = [t for t in extra_tools if getattr(t, "name", str(t)) not in AI_TOOLSET_V2_WITHHELD]
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

e, no corpo, trocar `    tools = [*base_tools_for(topology), *extra_tools]` por `    tools = effective_tools(topology, extra_tools, toolset_v2=toolset_v2)` (a linha `key = frozenset(_tool_cache_key(t) for t in tools)` que o P2b deixou logo abaixo não muda: a chave continua sendo os nomes do conjunto EFETIVO, então v1 e v2 nunca compartilham agente).

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
    the tools that read busy intervals or write to the agenda (`effective_tools`) and
    makes each of them refuse if one arrives anyway. False (the default) changes nothing.
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

Em `src/secretaria/workers/shared/llm_context.py` (estado DEPOIS do P2b):

1. Logo antes de `from secretaria.ai.tools import (`, inserir `from secretaria.ai.availability_tool import get_availability`.
2. Logo antes de `def _flow_handback_tools(tenant: Tenant | None, topology: str, plugin_tools: list) -> list:`, inserir:

```python
def _ai_toolset_v2(tenant: Tenant | None) -> bool:
    """Whether THIS tenant's agent runs on the v2 toolset (TASK-030 P4, spec §4.6).

    The per-clinic switch (`flow_router.ai_draft_v2_enabled`) AND the flow existing at all:
    the v2 agent has no booking tool of its own - it books only by handing back to the flow
    (`_flow_handback_tools`) - so a tenant without those hand-backs must never lose
    `create_event`. Read once per turn, here, and handed to `run_agent(toolset_v2=...)`.
    """
    return tenant is not None and flows_enabled(tenant) and ai_draft_v2_enabled(tenant)


```

3. No docstring de `_flow_handback_tools`, depois da linha `    arrives anyway.`, inserir:

```python

    On the v2 toolset (`_ai_toolset_v2`) the AI also gets `get_availability`, its only
    agenda read (free windows, never events); the tools it loses are withheld in
    ai/graph.py::effective_tools, not here.
```

4. Trocar a última linha da função `    return [*plugin_tools, *handbacks]` por:

```python
    reads = [get_availability] if _ai_toolset_v2(tenant) else []
    return [*plugin_tools, *handbacks, *reads]
```

- [ ] **Step 5: `workers/orchestrator.py`**

Em `src/secretaria/workers/orchestrator.py`:

1. No import de `secretaria.workers.shared.llm_context`, inserir `    _ai_toolset_v2,` antes de `    _appointment_context_text,`.
2. Na chamada `reply_text = await run_agent(...)`, depois da linha `        conversation_state=conversation_state_text,`, inserir `        toolset_v2=_ai_toolset_v2(tenant),`.

- [ ] **Step 6: Run the tests to verify they pass, plus the suites around this seam**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_ai_toolset_v2.py tests/test_ai_toolset_v2_locks.py tests/test_get_availability_tool.py tests/test_bot_reply_gating.py tests/test_set_booking_draft_v2.py tests/test_agent_tool_enforcement.py tests/test_agent_capability_cache.py tests/test_agent_menu_tools.py tests/test_llm_turn_trace.py tests/test_prompts.py tests/test_workers_layering.py tests/test_workers_ns_patching.py -q`
Expected: PASS (28 + 2 novos; o conjunto de hoje, nome por nome, inalterado com o interruptor desligado).

- [ ] **Step 7: Lint and commit**

```bash
uvx ruff format tests/test_ai_toolset_v2.py
uvx ruff check src/secretaria/ai/graph.py src/secretaria/workers/shared/llm_context.py src/secretaria/workers/orchestrator.py tests/test_ai_toolset_v2.py tests/test_bot_reply_gating.py tests/test_set_booking_draft_v2.py
for f in src/secretaria/ai/graph.py src/secretaria/workers/shared/llm_context.py src/secretaria/workers/orchestrator.py tests/test_bot_reply_gating.py tests/test_set_booking_draft_v2.py; do echo "$f $(uvx ruff format --diff $f 2>/dev/null | grep -c '^@@')"; done   # none may exceed its count before editing
git add src/secretaria/ai/graph.py src/secretaria/workers/shared/llm_context.py src/secretaria/workers/orchestrator.py tests/test_ai_toolset_v2.py tests/test_bot_reply_gating.py tests/test_set_booking_draft_v2.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(ai): the per-clinic switch builds the v2 toolset - no agenda tools, get_availability

graph.effective_tools is the one assembly of a turn's tools (also behind the capabilities log):
with toolset_v2 the base set is the scope-free one and the plugin booking/free-slot tools are
stripped by name; off, it is exactly what it always was. The worker decides the switch once per
turn (flows enabled AND ai_draft_v2) and hands it to run_agent, which sets the context var the
tools' own refusal reads. _flow_handback_tools delivers get_availability to v2 clinics only.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 6: Validação completa e documentação

**Files:**
- Create: `docs/CHECKPOINT_ia_get_availability.md`
- Modify: `docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md`, `docs/CHECKPOINT_plugins.md`, `docs/CHECKPOINT_pseudonimizacao.md` (uma linha de ponteiro cada, ao fim)

**Interfaces:**
- Consumes: tudo das Tasks 1–5.
- Produces: o CHECKPOINT que o P5 lê antes de começar.

- [ ] **Step 1: Run the whole suite and compare with the last full run**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest -q 2>&1 | tail -15`
Expected: `N' passed` com N' = (a contagem do último run completo, o da Task B5 do P2b — ou do P3, se já executado) + 137 testes novos (25 + 12 + 13 + 57 + 28 nos cinco arquivos novos, + 2 em `tests/test_bot_reply_gating.py`) e as MESMAS falhas pré-existentes, nenhuma outra. Qualquer falha nova é regressão deste plano: investigue (skill `superpowers:systematic-debugging`) antes de seguir.

- [ ] **Step 2: Lint every file this plan touched**

```bash
git diff --name-only $(git merge-base HEAD main)..HEAD -- '*.py' | xargs uvx ruff check
```

Expected: `All checks passed!`.

- [ ] **Step 3: Write the CHECKPOINT**

Criar `docs/CHECKPOINT_ia_get_availability.md`:

```markdown
# CHECKPOINT — IA entra em qualquer etapa: `get_availability` e a retirada das ferramentas de agenda (TASK-030 P4)

**Estado:** BUILT — commits locais na branch da TASK-030 (P4), **não pushado, não deployado**, atrás do interruptor `initial_flows.ai_draft_v2` (desligado por padrão).
**Spec:** `docs/superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md` (§4.6, §5 critério 5, §4.11, §7).
**Plano:** `docs/superpowers/plans/2026-10-02-ia-p4-disponibilidade-so-horarios.md`.

## 1. O que muda

- **Interruptor desligado (todas as clínicas hoje):** nada. O conjunto de ferramentas da IA é exatamente o de antes (fixado por testes, nome por nome).
- **Interruptor ligado:** a IA perde `check_availability`, `list_free_slots`, `create_event`, `cancel_event`, `list_free_slots_for_professional`, `create_event_for_professional` e `create_event_at_unit` — nenhuma lê horário ocupado e nenhuma escreve na agenda — e ganha `get_availability`, que devolve só janelas livres. Marcar continua sendo só pelo rascunho (`set_booking_draft`) e pelo toque do paciente em Confirmar; as reservas e o portão de código valem como sempre.

## 2. Onde está cada peça (âncoras)

- `services/availability_windows.py` — `resolve_range` (valida e limita os dias), `FreeWindow`, `slots_to_windows`, `scan_free_windows` (só usa `services/availability.py`, a definição única de "livre").
- `services/calendar.py::CalendarService.default_slot_minutes` — propriedade de leitura.
- `ai/availability_tool.py::get_availability` — a ferramenta; `_clinic_today` é a costura do relógio.
- `ai/tools.py` — `AI_TOOLSET_V2_WITHHELD`, `_ai_toolset_v2_ctx`, `_blocked_by_toolset_v2` (a trava de dentro de cada ferramenta retirada); as ferramentas de `plugins/multi_professional.py` e `plugins/multi_unit.py` chamam a mesma trava.
- `ai/graph.py` — `base_tools_for(toolset_v2=)`, `effective_tools` (a montagem única, também usada no log `agent_capabilities_resolved`), `build_agent(toolset_v2=)`, `run_agent(toolset_v2=)`.
- `workers/shared/llm_context.py` — `_ai_toolset_v2(tenant)` (clínica com fluxo E `ai_draft_v2`) e `get_availability` em `_flow_handback_tools`; `workers/orchestrator.py` o passa a `run_agent`.

## 3. O que a IA recebe

    {"windows": [{"day": "2026-10-06", "start": "08:00", "end": "09:30"}],
     "timezone": "America/Sao_Paulo", "slot_minutes": 30,
     "day_from": "2026-10-05", "day_to": "2026-10-18", "professional": "Dra. Ana"}

Só essas chaves, mais `clamped` (lista: `max_days`, `booking_window`, `max_windows`) e `note` (frase) quando há corte ou lista vazia. Erro: `{"error": "<frase>"}`, sem repetir o que o modelo mandou. Nunca título, participante, id, descrição, local, link nem bloco ocupado — teste com uma string canário em todos esses campos.

## 4. Limites e falhas

- No máximo **14 dias** e **30 janelas** por chamada (as mais cedo primeiro) e nunca além do **horizonte de agendamento** (20 dias, `flow_router.DAY_PICKER_WINDOW_DAYS`: uma janela oferecida é uma que o resolvedor aceita). Só um `day_to` explícito que passe do limite conta como corte.
- Recusa (erro curto): data em formato errado, dia que já passou (no "hoje" **da clínica**), `day_to` antes de `day_from`, primeiro dia além do horizonte, profissional desconhecido/duplicado/ausente numa clínica com vários, serviço inexistente.
- **Queda do calendário** (`CalendarUnavailableError`, token revogado incluído) **propaga** como em toda leitura de calendário de hoje: `run_agent` devolve o sentinel, o trabalhador entrega a conversa a uma pessoa e alerta o dono. **Qualquer outra falha** vira `{"error": ...}` e o log `agent_availability_failed` (só o tipo).
- Custo no Google: uma leitura para achar os dias com horário livre (uma agenda cheia ou fechada custa só essa) mais uma por dia com horário, em sequência (o cliente do Google não é seguro entre threads), até 14.

## 5. Observabilidade

`ai_availability_read` (INFO, uma por leitura): `tenant_id`, `conversation_id`, `windows`, `days_scanned`, `clamped`, `professional_resolved` — só contagens. `agent_tool_blocked` com `tool="get_availability"` e `reason` em {`no_clinic`, `tenant_mismatch`, `no_calendar`, `no_professionals`, `professional_required`, `professional_unknown`, `professional_ambiguous`, `service_unknown`, `no_professional_offers_service`, `several_professionals_offer_service`, `bad_day_format`, `past_day`, `reversed_range`, `beyond_window`}; com `reason="toolset_v2"` quando uma ferramenta retirada é chamada num turno v2 (deve ser zero com o prompt novo). `agent_availability_failed` (`error_type`). `agent_capabilities_resolved` ganhou `toolset_v2`.

## 6. Decisões e limites conhecidos

- Sem `service`, a duração é a padrão da agenda (`slot_minutes` na resposta).
- Janela não é grade: o fluxo só aceita horários na grade (início da janela + múltiplos de `slot_minutes`); um horário fora dela cai na lista de horários do dia (critério 3 do spec), sem erro.
- O seletor de DIAS do fluxo continua sem descontar reservas (P2a); a IA desconta. Um dia sem janela para a IA pode ainda aparecer no seletor.
- Retiradas além das cinco do spec: `list_free_slots_for_professional` (sem desconto de reservas), `create_event_at_unit`. Consequências: a IA perde o `professional_context` que o leitor antigo anexava (o bloco "SOBRE O PROFISSIONAL" do prompt continua) e, com o addon `multi_unit`, nenhum agendamento passa a registrar a unidade pela IA (o fluxo de botões nunca registrou).
- Reservas: o dono é `resolve_booking_owner_id` (a regra única dos agendamentos); a reserva da própria conversa não esconde o horário dela, como nos seletores.
- O refresh token do Google continua sendo decriptado só em `services/tenant_config.py`; a ferramenta nunca usa o calendário de ambiente.

## 7. Para o P5

Nomes e texto exatos (o docstring é o `description` que o modelo lê) estão no plano, seção "Para o P5". O prompt de hoje cita `check_availability`, `list_free_slots`, `create_event`, `cancel_event` e o `patient_calendar_link` do `create_event`; nada disso existe num turno v2. Quando o P5 remover `start_guided_booking` e `select_professional_and_continue`, atualizar os conjuntos exatos de `tests/test_ai_toolset_v2.py` e `_HANDBACKS` de `tests/test_ai_toolset_v2_locks.py`.

## 8. Deploy (quando o dono pedir)

Sem migração. `secretaria_api` **e** `secretaria-worker` juntos; `GET /build` com paridade `match`. Nada muda para clínica nenhuma até um operador ligar a chave `ai_draft_v2` (CHECKPOINT `ia_rascunho_v2_resolvedor` §3) — e **só depois de P3 e P5 no ar**. Rollback: desligar a chave (vale no turno seguinte) ou voltar o código nos dois serviços.
```

- [ ] **Step 4: Pointer lines**

Acrescentar ao fim de cada arquivo a linha indicada, numa linha própria.

`docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md`:

```markdown
> TASK-030 P4: a IA v2 lê horários só por `get_availability` (janelas livres) e perde as ferramentas que leem ocupado ou escrevem na agenda — ver `docs/CHECKPOINT_ia_get_availability.md`.
```

`docs/CHECKPOINT_plugins.md`:

```markdown
> TASK-030 P4: com `initial_flows.ai_draft_v2` ligado, `create_event_for_professional`, `list_free_slots_for_professional` e `create_event_at_unit` saem do conjunto da IA — ver `docs/CHECKPOINT_ia_get_availability.md`.
```

`docs/CHECKPOINT_pseudonimizacao.md`:

```markdown
> TASK-030 P4: na v2 a IA não recebe mais intervalos ocupados nem título de evento — `get_availability` devolve só janelas livres (ver `docs/CHECKPOINT_ia_get_availability.md`).
```

- [ ] **Step 5: Commit**

```bash
git add docs/CHECKPOINT_ia_get_availability.md docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md docs/CHECKPOINT_plugins.md docs/CHECKPOINT_pseudonimizacao.md
git diff --cached --stat
git commit -F - <<'EOF'
docs(checkpoint): TASK-030 P4 - get_availability and the v2 toolset

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Cobertura da spec (P4)

| Spec | Onde |
|---|---|
| §4.6 `get_availability(professional, service, day_from, day_to)` devolve só janelas `{day, start, end}`, no máximo 14 dias e 30 janelas | T1 (`resolve_range`, limites), T2 (`scan_free_windows`), T4 (a ferramenta) |
| §4.6 "agenda do profissional − reservas, duração do serviço" e "calculada pelo mesmo código dos seletores" | T2 (só `services/availability.py`), T4 (`BookingGate` sem armar, `resolve_booking_owner_id`, duração do serviço) |
| §4.6 "nenhum campo de evento (título, participantes, id, bloco ocupado) sai da camada de calendário" e §5.5 | T2 e T4 (canário em todos os campos; conjunto fechado de chaves) |
| §4.6 "saem: `check_availability`, `list_free_slots`, `create_event`, `create_event_for_professional`, `cancel_event` e similares (as funções continuam para uso interno do fluxo)" | T3 (`AI_TOOLSET_V2_WITHHELD` + a trava), T5 (`effective_tools`); as funções do calendário e do fluxo não mudam |
| §4.6 "ferramenta dentro do worker, não endpoint público" | T4 (`ai/availability_tool.py` é uma ferramenta do agente; nenhuma rota) |
| §4.6 "a IA pode mencionar horários que recebeu; o agendamento só pelo rascunho e pelo toque" | T3/T5 (nenhum escritor de agenda no conjunto v2); docstring da T4 manda usar `set_booking_draft` |
| §4.8 "nenhuma entrada pode contornar o portão e as reservas" / §3 "porta dos fundos" | T3 + T5 (a IA v2 não agenda fora do fluxo, em tenant de um só médico também) |
| §4.11 interruptor por tenant, desligado por padrão | T5 (`_ai_toolset_v2`, `toolset_v2=False` por padrão, testes de regressão do conjunto de hoje) |
| §6 "teste de formato de `get_availability`", isolamento de tenant, reservas | T4 (chaves, canário, dois tenants, reservas), T3/T5 (conjuntos exatos) |
| §7 "`create_event` & cia. como porta dos fundos → P4" | T3, T5 |

## Deploy e liberação

Deploy nunca faz parte deste plano: só com pedido explícito do dono, a cada vez.

1. **Sem migração.** Nada novo em banco: o interruptor continua sendo `Tenant.initial_flows["ai_draft_v2"]` (P2b) e a ferramenta só lê.
2. **API e worker juntos** — README: "Deploy both services, or neither". A ferramenta e a montagem do conjunto rodam no worker; a API mapeia os mesmos modelos. Conferir `GET /build` dos dois: `deploy_parity` = `match`.
3. **O que muda no deploy para todas as clínicas:** nada visível. Com o interruptor desligado o conjunto de ferramentas é o de hoje (testes nome por nome), `run_agent(toolset_v2=False)` é o padrão e a única diferença observável é o campo `toolset_v2=false` no log `agent_capabilities_resolved` e a propriedade `CalendarService.default_slot_minutes` (só leitura).
4. **O que liga por clínica, por um operador** (a mudança de dado do CHECKPOINT do P2b §3): a ferramenta nova, a retirada das sete e, junto, o rascunho v2. **Só ligue depois de P3 e P5 deployados.** O prompt de hoje manda a IA chamar `create_event`, `list_free_slots` e `check_availability`; num turno v2 essas ferramentas não existem (o `ToolNode` do LangGraph responde ao modelo `Error: create_event is not a valid tool, try one of [...]` — verificado com langgraph 1.2.1 — e o turno não cai, mas a conversa fica sem como marcar ou olhar a agenda). Primeiro na "Chrysostomo For Eyes"; observar nos logs `agent_capabilities_resolved` (`toolset_v2=true`, `capabilities`), `ai_availability_read` (contagens), `agent_tool_blocked` com `reason=toolset_v2` (deve ser zero: se aparecer, o prompt ainda cita uma ferramenta retirada) e `agent_availability_failed`.
5. **Rollback imediato:** desligar a chave da clínica (efeito no turno seguinte; a ferramenta retirada volta). Rollback de código: versão antiga nos DOIS serviços; não há dado a desfazer.
