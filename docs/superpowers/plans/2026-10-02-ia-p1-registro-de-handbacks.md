# P1 — Registro de hand-backs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Todo hand-back da IA ao fluxo guiado — por qualquer ferramenta, com sucesso ou caindo no menu — escreve UM evento estruturado `conversation_handback_entered` (onde o paciente caiu, o que a IA mandou, o que foi aceito ou descartado, sem valores), e o `llm_turn_trace` passa a listar as ferramentas chamadas.

**Architecture:** Um módulo novo e pequeno, `workers/shared/handback_log.py`, é o único lugar que escreve o evento e o dono do vocabulário fechado de códigos (ferramenta de origem, etapa de pouso, motivo de fallback, nomes de campo). Os handlers de `workers/shared/sentinels.py` e `workers/shared/handover.py` passam a sair por helpers privados (`_fallback_to_menu`, `_log_no_landing`, `_land_handback`) que registram o evento ANTES de gravar o estado e enviar, de modo que cada saída — pouso, queda no menu ou retorno silencioso — registra exatamente uma vez. `tools_called` sai de `ai/trace.py::summarize_turn`. É só log: nenhum comportamento visto pelo paciente muda.

**Tech Stack:** Python 3.12, structlog, pytest + pytest-asyncio (SQLite em memória), ruff.

**Spec:** `docs/superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md` — §3 (bullet "Log"), §4.9 (Observabilidade), §7 (linha "Hand-backs não registram a etapa"), §9 (linha P1) e critério de sucesso 7 (100% dos hand-backs geram `conversation_handback_entered`).

## Global Constraints

- Trabalhar no worktree `C:\TECH\BRAIN-worktrees\TASK-030\secretarIA` (branch `task/TASK-030-ia-entra-em-qualquer-etapa`). Tarefas **sequenciais**: P1 e P2 editam `sentinels.py`, nunca dois agentes ao mesmo tempo.
- Testes rodam do Git Bash, na raiz do worktree: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest <arquivo> -q` (PowerShell não consegue setar variável vazia — ela é apagada; nunca `pytest` solto).
- Nunca `ruff format .`. `uvx ruff check <arquivos tocados>` sempre. `uvx ruff format <arquivo>` **só nos arquivos criados por este plano** (`handback_log.py` e os dois arquivos de teste novos) e em `ai/trace.py` / `tests/test_llm_turn_trace.py` (já limpos). `sentinels.py`, `handover.py` e `graph.py` já têm hunks de formatação pré-existentes (10, 4 e 2 em `ruff format --diff | grep -c '^@@'`): neles escreva no estilo ruff e confira que a contagem **não aumenta**.
- A árvore de trabalho está em CRLF (índice em LF, `core.autocrlf=true`). Antes de cada commit: `git diff --cached --stat` precisa mostrar só as linhas deste passo (dezenas, não centenas por arquivo). Se um arquivo existente aparecer inteiro como alterado, a conversão de fim de linha estragou — `git restore --staged <arquivo>`, desfaça e refaça a edição com âncoras de uma linha.
- Camadas: api → workers → services/ai → models → core. `workers/shared/*` não importa `workers.whatsapp`, `workers.portal` nem `workers.tasks` (`tests/test_workers_layering.py`). `handback_log.py` importa só `core`/`services`.
- Sem PII nem segredo em log: o evento leva ids, NOMES de campo e CÓDIGOS de motivo — nunca o serviço, o convênio, o nome, o horário nem o UUID de médico/paciente que a IA ou o paciente escreveu.
- Os eventos atuais continuam sendo emitidos, sem mudar de campo: `conversation_booking_draft_entered`, `conversation_guided_booking_entered`, `conversation_menu_rendered`, `ai_run_agent_*`, `llm_turn_trace` (só ganha `tools_called`).
- Texto de UI em português; código e comentários em inglês, na densidade dos arquivos vizinhos.
- Cada commit termina com a linha `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Commit local é permitido; push e deploy só com pedido explícito do dono.

## Review Focus

- **Fallback que não registra nada.** Cada saída para o menu e cada retorno silencioso dos cinco handlers tem teste que exige UM evento com o código certo (Tasks 2–7).
- **Handler que levanta antes de registrar.** O evento sai ANTES de `_apply_flow_result`, então falha ao gravar/enviar não apaga o pouso (Task 3). Falha de infraestrutura ANTES de o pouso ser conhecido (leitura no banco) segue propagando como hoje e não gera evento — decisão fixada em teste (Task 4); a diferença entre `ai_run_agent_*` e `conversation_handback_entered` é o sinal para detectá-la. Hand-off humano que não consegue gravar o estado não registra pouso (Task 7).
- **Redação.** Teste com valores hostis (serviço com nome de pessoa, convênio com e-mail e telefone, UUID de médico/paciente) afirma que nada disso aparece no evento (Task 6); o evento sobrevive ao `redact_secrets` sem campo apagado e valor fora do vocabulário vira `"other"` (Task 1).
- **Retornos silenciosos com `tenant is None`.** `show_main_menu`, manage, guided e draft ganham evento `no_tenant` com `landing_step=None` (Tasks 2, 4, 5, 6); `tenant_id` sai `None`, nunca quebra.
- **Contagem dupla.** O fallback para o menu chama `_handle_show_main_menu(source="sentinel_fallback")`, que NÃO registra hand-back; só `source="agent_tool"` (a ferramenta da IA) registra. Todo teste de fallback afirma exatamente um evento, e `/menu`, nome e cartões de identidade não geram nenhum (Tasks 2–3).
- **Lacuna conhecida, não corrigida aqui:** um turno que termina em hand-back levanta a exceção da ferramenta e desenrola o `ainvoke` antes de `_log_agent_trace` — esses turnos nunca emitem `llm_turn_trace`, então `tools_called` só aparece em turnos que terminam em texto. Nos de hand-back, `conversation_handback_entered.source_tool` diz a ferramenta final (Task 8 documenta; ver "Decisões").

## Vocabulário do evento (contrato para P2–P5)

Evento `conversation_handback_entered`, nível INFO, **um por hand-back**, emitido por `handback_log.log_handback_entered(...)`:

```python
def log_handback_entered(
    *,
    conversation_id,            # UUID | str | None  -> logged as str | None
    tenant_id,                  # UUID | str | None  -> logged as str | None
    source_tool: str,           # one of SOURCE_TOOLS
    landing_step: str | None,   # None = nothing landed (no menu, no flow step)
    supplied: Sequence[str] = (),             # field NAMES the agent filled in
    accepted: Sequence[str] = (),             # field NAMES that passed validation
    dropped: Mapping[str, str] | None = None, # field name -> DROP_* code
    fallback: str | None = None,              # None on a clean landing, else FALLBACK_*
    topology: str | None = None,              # "unknown" | "none" | "sole" | "multi"
    channel: str | None = None,               # "whatsapp" | "brain_message"
) -> None: ...
```

Campos logados (sempre todos): `conversation_id`, `tenant_id`, `source_tool`, `landing_step`, `supplied` (lista), `accepted` (lista), `dropped` (dict, `{}` quando vazio), `fallback`, `topology`, `channel`. Qualquer string fora dos vocabulários abaixo sai como `"other"`.

`source_tool` (`SOURCE_*`): `set_booking_draft`, `show_main_menu`, `manage_existing_appointment`, `select_professional`, `start_guided_booking`, `request_human_handoff`.

`landing_step`: o `flow_step` do resultado (`awaiting_service`, `awaiting_day`, `manage_cancel_confirm`… — os valores `STEP_*` de `services/flow_router.py`); sem step, o estado em minúsculas (`menu`, `idle`…); ou um pseudo-passo: `menu`, `human_handover` (calendário fora do ar ou transferência para pessoa), `config_incomplete` (médico sem serviços/horários: alerta). `None` = nada pousou.

`fallback` (`FALLBACK_*`) — por que o hand-back não pousou onde a ferramenta pediu:

| Código | Significado | Quem emite hoje |
|---|---|---|
| `bad_sentinel` | payload do sentinel corrompido (JSON do rascunho, id de médico que não é UUID, ação desconhecida) → menu | draft, select_professional, manage |
| `no_tenant` | o handler não recebeu tenant (retorno silencioso; menu só se houver tenant) | os cinco, quando `tenant is None` |
| `without_flows` | `flows_enabled(tenant)` falso (defensivo; hoje sempre verdadeiro) | manage, guided, draft |
| `no_patient` | a conversa não tem paciente (manage, retorno silencioso) | manage |
| `invalid_selection` | item do rascunho reprovado ou combinação sem pouso → menu (hoje um item ruim derruba tudo) | draft |
| `missing_professional` | clínica multi-médico, serviço dado, sem médico → menu | draft |
| `unknown_professional` | médico fora do roster ativo → menu | select_professional |
| `no_bookable_catalog` | o fluxo não consegue abrir um agendamento (sem serviços) → menu | draft (só seleção) |
| `multi_professional` | `start_guided_booking` em clínica multi-médico → menu | guided |
| `no_appointments` | nada para gerenciar: pousa no menu com explicação | manage |
| `calendar_unavailable` | agenda indisponível → transferência para pessoa | guided, draft, manage |
| `professional_config_incomplete` | médico sem serviços/horários (alerta enviado; no draft só-seleção vira menu sem alerta) | select_professional, draft |

`dropped` (`DROP_*`, por campo): `not_in_catalog` (serviço), `unknown_professional` (médico), `unmatched_plan` (convênio). Nomes de campo (`FIELD_*`): `service`, `professional`, `insurance`, `for_whom`, `day`, `time`, `action`, `appointment`, `reason` (os quatro do meio e `appointment` são reservados para P2/P3).

Outros planos da TASK-030 acrescentam códigos NO MESMO commit do primeiro ponto de chamada que os usa; `tests/test_handback_log.py` mantém cada lista igual às constantes.

## Decisões deste plano (mudar só se o dono discordar)

1. **Evento antes de gravar/enviar.** `conversation_handback_entered` sai assim que o pouso é conhecido, antes de `_apply_flow_result`. O nome diz "entered".
2. **Só `source="agent_tool"` conta no menu.** `_handle_show_main_menu` também serve `/menu`, o passo do nome e os cartões de identidade (não são a IA) e o fallback de sentinel (já contado por quem caiu). Só a chamada da ferramenta `show_main_menu` registra hand-back ali.
3. **Falha anterior ao pouso não gera evento.** Leitura no banco que levanta antes de o pouso ser conhecido propaga como hoje (job falha, rede de segurança do turno responde).
4. **Vocabulário fechado com guarda.** Valor fora da lista vira `"other"` em vez de ser logado cru.
5. **`tools_called` só no turno que termina em texto** (ver Review Focus, última linha).

## File Structure

- Create `src/secretaria/workers/shared/handback_log.py` — o evento, os vocabulários e `landing_of(result)`.
- Modify `src/secretaria/workers/shared/sentinels.py` — `_handle_show_main_menu`, `_handle_select_professional`, `_handle_manage_appointment`, `_handle_start_guided_booking`, `_handle_set_booking_draft` + helpers privados `_fallback_to_menu`, `_log_no_landing`, `_land_handback`, `_draft_verdicts`.
- Modify `src/secretaria/workers/shared/handover.py` — `_handle_human_handoff`.
- Modify `src/secretaria/ai/trace.py` — `summarize_turn` ganha `tools_called` (o `**summary` do `llm_turn_trace` em `ai/graph.py` o leva sem mudança no `graph.py`).
- Create `tests/test_handback_log.py` — módulo: forma do evento, vocabulário, redação, `landing_of`.
- Create `tests/test_handback_events.py` — cada saída de cada handler (fixtures próprias: este repo redefine `db` em cada arquivo).
- Modify `tests/test_workers_layering.py` — `handback_log` entra no teste de import isolado.
- Modify `tests/test_llm_turn_trace.py` — `tools_called`.
- Modify `docs/CHECKPOINT_llm_observabilidade_rede_de_seguranca.md` — o evento novo, o vocabulário e a lacuna.

---

### Task 1: `handback_log` — o evento, o vocabulário e `landing_of`

**Files:**
- Create: `src/secretaria/workers/shared/handback_log.py`
- Create: `tests/test_handback_log.py`
- Modify: `tests/test_workers_layering.py:76-87`

**Interfaces:**
- Consumes: `secretaria.core.logging.get_logger`; `secretaria.services.booking_scope.BOOKING_TOPOLOGY_*`; `secretaria.services.channel_sender.CHANNEL_*`; `FlowRouterResult` (só em tipo).
- Produces (os outros planos dependem destes nomes exatos):
  - `log_handback_entered(*, conversation_id, tenant_id, source_tool: str, landing_step: str | None, supplied: Sequence[str] = (), accepted: Sequence[str] = (), dropped: Mapping[str, str] | None = None, fallback: str | None = None, topology: str | None = None, channel: str | None = None) -> None` — nunca levanta.
  - `landing_of(result: FlowRouterResult) -> tuple[str, str | None]` — `(landing_step, fallback)` implícitos no resultado.
  - Constantes `EVENT_NAME`, `OTHER`; `SOURCE_*` + `SOURCE_TOOLS`; `LANDING_MENU`, `LANDING_HUMAN_HANDOVER`, `LANDING_CONFIG_INCOMPLETE`; `FALLBACK_*` + `FALLBACK_REASONS`; `DROP_*` + `DROP_REASONS`; `FIELD_*` + `FIELD_NAMES`; `TOPOLOGIES`; `CHANNELS`.

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_handback_log.py`:

```python
"""The hand-back event: one shape, a closed vocabulary, no values (TASK-030 P1)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.core.logging import redact_secrets  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services.flow_router import FlowRouterResult  # noqa: E402
from secretaria.workers.shared import handback_log  # noqa: E402


class _LogRecorder:
    """Records every structlog call (`caplog` is empty for structlog in this suite)."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        def _log(event: str, **fields) -> None:
            self.records.append((level, event, fields))

        return _log


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    recorder = _LogRecorder()
    monkeypatch.setattr(handback_log, "logger", recorder)
    return recorder


def _only_event(log: _LogRecorder) -> dict:
    assert [event for _level, event, _fields in log.records] == [handback_log.EVENT_NAME]
    level, _event, fields = log.records[0]
    assert level == "info"
    return fields


def test_the_event_carries_ids_as_strings_and_names_as_lists(log: _LogRecorder) -> None:
    conversation_id, tenant_id = uuid4(), uuid4()

    handback_log.log_handback_entered(
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        source_tool="set_booking_draft",
        landing_step="awaiting_day",
        supplied=("service", "professional", "insurance"),
        accepted=("service", "professional"),
        dropped={"insurance": "unmatched_plan"},
        fallback=None,
        topology="multi",
        channel="whatsapp",
    )

    assert _only_event(log) == {
        "conversation_id": str(conversation_id),
        "tenant_id": str(tenant_id),
        "source_tool": "set_booking_draft",
        "landing_step": "awaiting_day",
        "supplied": ["service", "professional", "insurance"],
        "accepted": ["service", "professional"],
        "dropped": {"insurance": "unmatched_plan"},
        "fallback": None,
        "topology": "multi",
        "channel": "whatsapp",
    }


def test_optional_arguments_default_to_empty_and_none(log: _LogRecorder) -> None:
    handback_log.log_handback_entered(
        conversation_id=None,
        tenant_id=None,
        source_tool="show_main_menu",
        landing_step=None,
    )

    assert _only_event(log) == {
        "conversation_id": None,
        "tenant_id": None,
        "source_tool": "show_main_menu",
        "landing_step": None,
        "supplied": [],
        "accepted": [],
        "dropped": {},
        "fallback": None,
        "topology": None,
        "channel": None,
    }


def test_a_value_outside_the_vocabulary_is_logged_as_other_never_as_itself(
    log: _LogRecorder,
) -> None:
    leak = "Maria Silva maria@example.com"

    handback_log.log_handback_entered(
        conversation_id=uuid4(),
        tenant_id=uuid4(),
        source_tool=leak,
        landing_step=leak,
        supplied=(leak, "service"),
        accepted=[leak],
        dropped={leak: leak},
        fallback=leak,
        topology=leak,
        channel=leak,
    )

    fields = _only_event(log)
    assert leak not in repr(fields)
    assert fields["source_tool"] == "other"
    assert fields["landing_step"] == "other"
    assert fields["supplied"] == ["other", "service"]
    assert fields["accepted"] == ["other"]
    assert fields["dropped"] == {"other": "other"}
    assert fields["fallback"] == "other"
    assert fields["topology"] == "other"
    assert fields["channel"] == "other"


def test_logging_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    warned: list[str] = []

    class _Broken:
        def info(self, *_args, **_kwargs) -> None:
            raise RuntimeError("log backend down")

        def warning(self, event: str, **_fields) -> None:
            warned.append(event)

    monkeypatch.setattr(handback_log, "logger", _Broken())

    handback_log.log_handback_entered(
        conversation_id=uuid4(),
        tenant_id=uuid4(),
        source_tool="show_main_menu",
        landing_step="menu",
    )

    assert warned == ["conversation_handback_log_failed"]


def test_no_field_is_blanked_by_the_redaction_processor(log: _LogRecorder) -> None:
    handback_log.log_handback_entered(
        conversation_id=uuid4(),
        tenant_id=uuid4(),
        source_tool="manage_existing_appointment",
        landing_step="manage_cancel_confirm",
        supplied=("action",),
        accepted=("action",),
        dropped={"service": "not_in_catalog"},
        fallback="no_appointments",
        topology="sole",
        channel="brain_message",
    )
    fields = _only_event(log)

    scrubbed = redact_secrets(None, "info", {"event": handback_log.EVENT_NAME, **fields})

    assert scrubbed == {"event": handback_log.EVENT_NAME, **fields}


@pytest.mark.parametrize(
    "result, expected",
    [
        (
            FlowRouterResult(
                action="reply", flow_state=FlowState.SERVICE_CATALOG, flow_step="awaiting_day"
            ),
            ("awaiting_day", None),
        ),
        (FlowRouterResult(action="reply", flow_state=FlowState.MENU), ("menu", None)),
        (FlowRouterResult(action="reply", flow_state=FlowState.IDLE), ("idle", None)),
        (
            FlowRouterResult(
                action="calendar_unavailable",
                flow_state=FlowState.SERVICE_CATALOG,
                flow_step="awaiting_day",
            ),
            ("human_handover", "calendar_unavailable"),
        ),
        (
            FlowRouterResult(action="professional_config_incomplete", flow_state=FlowState.IDLE),
            ("config_incomplete", "professional_config_incomplete"),
        ),
    ],
)
def test_landing_of_names_the_step_or_the_pseudo_step(
    result: FlowRouterResult, expected: tuple[str, str | None]
) -> None:
    assert handback_log.landing_of(result) == expected


@pytest.mark.parametrize(
    "prefix, vocabulary",
    [
        ("SOURCE_", "SOURCE_TOOLS"),
        ("FALLBACK_", "FALLBACK_REASONS"),
        ("DROP_", "DROP_REASONS"),
        ("FIELD_", "FIELD_NAMES"),
    ],
)
def test_every_constant_is_in_its_vocabulary_and_back(prefix: str, vocabulary: str) -> None:
    constants = {
        value
        for name, value in vars(handback_log).items()
        if name.startswith(prefix) and name != vocabulary and isinstance(value, str)
    }
    assert constants == set(getattr(handback_log, vocabulary))


def test_source_tools_are_the_six_agent_hand_backs() -> None:
    assert handback_log.SOURCE_TOOLS == {
        "set_booking_draft",
        "show_main_menu",
        "manage_existing_appointment",
        "select_professional",
        "start_guided_booking",
        "request_human_handoff",
    }
```

Em `tests/test_workers_layering.py`, na lista de `test_each_entry_point_imports_alone`, acrescentar uma linha depois de `"secretaria.workers.shared.sentinels",`:

```python
        "secretaria.workers.shared.handback_log",
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_log.py -q`
Expected: erro de coleta — `ImportError: cannot import name 'handback_log' from 'secretaria.workers.shared'`.

- [ ] **Step 3: Write the module**

Criar `src/secretaria/workers/shared/handback_log.py`:

```python
"""One structured event per AI hand-back: where did the patient land?

The agent hands a conversation back to the guided workflow through a few tools
(`set_booking_draft`, `show_main_menu`, `manage_existing_appointment`,
`select_professional_and_continue`, `start_guided_booking`, `request_human_handoff`). Each
one ends in a worker handler (`workers/shared/sentinels.py`, `workers/shared/handover.py`)
that either lands the patient on a flow step or falls back - to the main menu, to a human,
or to nothing at all. This module is the one place that records the outcome, as
`conversation_handback_entered`, so the share of patients landing on each step (and the
share bouncing to the menu) can be counted from the logs alone.

Privacy: the event carries ids, field NAMES and reason CODES. Never a value the patient or
the model wrote - a service, a convênio, a name, a time, a doctor's or a patient's UUID.
Every free-form argument is checked against the closed vocabularies below and anything
outside them is logged as "other", so a call site that passes the wrong thing cannot leak it.

Other TASK-030 plans add their codes here, in the same commit as the first call site that
uses them; tests/test_handback_log.py keeps each list in sync with its constants.
"""

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING
from uuid import UUID

from secretaria.core.logging import get_logger
from secretaria.services.booking_scope import (
    BOOKING_TOPOLOGY_MULTI,
    BOOKING_TOPOLOGY_NONE,
    BOOKING_TOPOLOGY_SOLE,
    BOOKING_TOPOLOGY_UNKNOWN,
)
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, CHANNEL_WHATSAPP

if TYPE_CHECKING:
    from secretaria.services.flow_router import FlowRouterResult

logger = get_logger(__name__)

EVENT_NAME = "conversation_handback_entered"
# What any value outside a closed vocabulary is logged as.
OTHER = "other"

# --- source_tool: which agent tool asked for the hand-back -------------------------------
SOURCE_SET_BOOKING_DRAFT = "set_booking_draft"
SOURCE_SHOW_MAIN_MENU = "show_main_menu"
SOURCE_MANAGE_EXISTING_APPOINTMENT = "manage_existing_appointment"
SOURCE_SELECT_PROFESSIONAL = "select_professional"
SOURCE_START_GUIDED_BOOKING = "start_guided_booking"
SOURCE_REQUEST_HUMAN_HANDOFF = "request_human_handoff"
SOURCE_TOOLS = frozenset(
    {
        SOURCE_SET_BOOKING_DRAFT,
        SOURCE_SHOW_MAIN_MENU,
        SOURCE_MANAGE_EXISTING_APPOINTMENT,
        SOURCE_SELECT_PROFESSIONAL,
        SOURCE_START_GUIDED_BOOKING,
        SOURCE_REQUEST_HUMAN_HANDOFF,
    }
)

# --- landing_step -------------------------------------------------------------------------
# Normally the result's own `flow_step` (the `STEP_*` values of services/flow_router.py) or,
# when it has none, the lower-cased flow state. These pseudo steps cover landings that are
# not a place in the flow. The shape check below is all that guards the open part.
LANDING_MENU = "menu"
LANDING_HUMAN_HANDOVER = "human_handover"
LANDING_CONFIG_INCOMPLETE = "config_incomplete"
_STEP_SHAPE = re.compile(r"[a-z][a-z0-9_]{0,47}")

# --- fallback: why the hand-back did not land where the tool asked ------------------------
FALLBACK_BAD_SENTINEL = "bad_sentinel"
FALLBACK_NO_TENANT = "no_tenant"
FALLBACK_WITHOUT_FLOWS = "without_flows"
FALLBACK_NO_PATIENT = "no_patient"
FALLBACK_INVALID_SELECTION = "invalid_selection"
FALLBACK_MISSING_PROFESSIONAL = "missing_professional"
FALLBACK_UNKNOWN_PROFESSIONAL = "unknown_professional"
FALLBACK_NO_BOOKABLE_CATALOG = "no_bookable_catalog"
FALLBACK_MULTI_PROFESSIONAL = "multi_professional"
FALLBACK_NO_APPOINTMENTS = "no_appointments"
FALLBACK_CALENDAR_UNAVAILABLE = "calendar_unavailable"
FALLBACK_PROFESSIONAL_CONFIG_INCOMPLETE = "professional_config_incomplete"
FALLBACK_REASONS = frozenset(
    {
        FALLBACK_BAD_SENTINEL,
        FALLBACK_NO_TENANT,
        FALLBACK_WITHOUT_FLOWS,
        FALLBACK_NO_PATIENT,
        FALLBACK_INVALID_SELECTION,
        FALLBACK_MISSING_PROFESSIONAL,
        FALLBACK_UNKNOWN_PROFESSIONAL,
        FALLBACK_NO_BOOKABLE_CATALOG,
        FALLBACK_MULTI_PROFESSIONAL,
        FALLBACK_NO_APPOINTMENTS,
        FALLBACK_CALENDAR_UNAVAILABLE,
        FALLBACK_PROFESSIONAL_CONFIG_INCOMPLETE,
    }
)

# --- field names (supplied / accepted / dropped keys) -------------------------------------
# `for_whom`, `day`, `time` and `appointment` are reserved for the draft v2 and the manage
# v2 (TASK-030 P2/P3); the tools do not carry them yet.
FIELD_SERVICE = "service"
FIELD_PROFESSIONAL = "professional"
FIELD_INSURANCE = "insurance"
FIELD_FOR_WHOM = "for_whom"
FIELD_DAY = "day"
FIELD_TIME = "time"
FIELD_ACTION = "action"
FIELD_APPOINTMENT = "appointment"
FIELD_REASON = "reason"
FIELD_NAMES = frozenset(
    {
        FIELD_SERVICE,
        FIELD_PROFESSIONAL,
        FIELD_INSURANCE,
        FIELD_FOR_WHOM,
        FIELD_DAY,
        FIELD_TIME,
        FIELD_ACTION,
        FIELD_APPOINTMENT,
        FIELD_REASON,
    }
)

# --- dropped: why a supplied field did not survive ----------------------------------------
DROP_NOT_IN_CATALOG = "not_in_catalog"
DROP_UNKNOWN_PROFESSIONAL = "unknown_professional"
DROP_UNMATCHED_PLAN = "unmatched_plan"
DROP_REASONS = frozenset({DROP_NOT_IN_CATALOG, DROP_UNKNOWN_PROFESSIONAL, DROP_UNMATCHED_PLAN})

TOPOLOGIES = frozenset(
    {
        BOOKING_TOPOLOGY_UNKNOWN,
        BOOKING_TOPOLOGY_NONE,
        BOOKING_TOPOLOGY_SOLE,
        BOOKING_TOPOLOGY_MULTI,
    }
)
CHANNELS = frozenset({CHANNEL_WHATSAPP, CHANNEL_BRAIN_MESSAGE})


def _id(value: object) -> str | None:
    return None if value is None else str(value)


def _vocab(value: object, allowed: frozenset[str]) -> str | None:
    """`value` when it is in the closed vocabulary, "other" when it is not, None for None."""
    if value is None:
        return None
    text = str(value)
    return text if text in allowed else OTHER


def _names(values: Sequence[str]) -> list[str]:
    return [name if name in FIELD_NAMES else OTHER for name in map(str, values)]


def _dropped(dropped: Mapping[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for field, reason in (dropped or {}).items():
        field_name, reason_code = str(field), str(reason)
        out[field_name if field_name in FIELD_NAMES else OTHER] = (
            reason_code if reason_code in DROP_REASONS else OTHER
        )
    return out


def _step(landing_step: str | None) -> str | None:
    if landing_step is None:
        return None
    text = str(landing_step)
    return text if _STEP_SHAPE.fullmatch(text) else OTHER


def log_handback_entered(
    *,
    conversation_id: UUID | str | None,
    tenant_id: UUID | str | None,
    source_tool: str,
    landing_step: str | None,
    supplied: Sequence[str] = (),
    accepted: Sequence[str] = (),
    dropped: Mapping[str, str] | None = None,
    fallback: str | None = None,
    topology: str | None = None,
    channel: str | None = None,
) -> None:
    """Emit the ONE `conversation_handback_entered` event for this hand-back.

    `landing_step` is None when nothing landed (no menu, no flow step). `fallback` is None on
    a clean landing and a `FALLBACK_*` code when the hand-back bounced. Never raises:
    observability must not be able to fail a turn.
    """
    try:
        logger.info(
            EVENT_NAME,
            conversation_id=_id(conversation_id),
            tenant_id=_id(tenant_id),
            source_tool=_vocab(source_tool, SOURCE_TOOLS),
            landing_step=_step(landing_step),
            supplied=_names(supplied),
            accepted=_names(accepted),
            dropped=_dropped(dropped),
            fallback=_vocab(fallback, FALLBACK_REASONS),
            topology=_vocab(topology, TOPOLOGIES),
            channel=_vocab(channel, CHANNELS),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("conversation_handback_log_failed", error_type=type(exc).__name__)


def landing_of(result: "FlowRouterResult") -> tuple[str, str | None]:
    """The `(landing_step, fallback)` a flow result implies.

    `landing_step` is the result's own `flow_step` when it has one, else the lower-cased flow
    state (`menu`, `idle`, ...). Two results are not a place in the flow at all and get a
    pseudo step plus a fallback code - the patient did not land where the tool wanted: a
    calendar outage hands them to a person, and a doctor with no configured services or
    hours ends in an alert.
    """
    if result.action == "calendar_unavailable":
        return LANDING_HUMAN_HANDOVER, FALLBACK_CALENDAR_UNAVAILABLE
    if result.action == "professional_config_incomplete":
        return LANDING_CONFIG_INCOMPLETE, FALLBACK_PROFESSIONAL_CONFIG_INCOMPLETE
    return result.flow_step or result.flow_state.value.lower(), None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_log.py tests/test_workers_layering.py -q`
Expected: PASS (15 testes novos em `test_handback_log.py` + o import isolado novo do layering).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff check src/secretaria/workers/shared/handback_log.py tests/test_handback_log.py tests/test_workers_layering.py
uvx ruff format src/secretaria/workers/shared/handback_log.py tests/test_handback_log.py
git add src/secretaria/workers/shared/handback_log.py tests/test_handback_log.py tests/test_workers_layering.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(observability): conversation_handback_entered event and its closed vocabulary

One module owns the hand-back event, the reason-code vocabulary and landing_of(). Free-form
values outside the vocabulary are logged as "other"; ids, field names and codes only.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 2: A ferramenta `show_main_menu` registra o hand-back (e só ela)

**Files:**
- Modify: `src/secretaria/workers/shared/sentinels.py` (imports; constante `_AGENT_TOOL_SOURCE`; `_handle_show_main_menu`)
- Create: `tests/test_handback_events.py`

**Interfaces:**
- Consumes: `handback_log.log_handback_entered`, `handback_log.SOURCE_SHOW_MAIN_MENU`, `handback_log.FALLBACK_NO_TENANT`, `handback_log.LANDING_MENU` (Task 1); `booking_topology` (já importado em `sentinels.py`).
- Produces: `_handle_show_main_menu(reply, tenant, professionals, patient_wa, redis=None, waba_token=None, source="agent_tool")` (assinatura inalterada) registra `conversation_handback_entered` quando `source == "agent_tool"`; `_AGENT_TOOL_SOURCE = "agent_tool"`. O arquivo de teste e suas fixtures (`db`, `_seed`, `_seed_sole`, `_snapshots`, `_reply_ctx`, `_captured_bubbles`, `_stub_calendar`, `_seed_future_appointment`, `log`, `_events`) são reutilizados pelas Tasks 3–7.

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_handback_events.py`:

```python
"""Every AI hand-back writes exactly ONE `conversation_handback_entered` event.

TASK-030 P1. The agent's hand-back tools end in worker handlers that land the patient on a
flow step - or fall back to the menu, to a human, or to nothing at all. These tests pin that
every handler exit, success or fallback, records where the patient landed, once, with field
names and reason codes only. DB-backed pieces use the in-memory-sqlite pattern of
tests/test_agent_menu_tools.py.
"""

import os

from tests._patching import workers_ns

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    FlowState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Professional,
    Tenant,
)
from secretaria.services.flow_router import MenuBubble  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import handback_log  # noqa: E402


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _patch_session_factory(monkeypatch: pytest.MonkeyPatch, db):
    # ai/tools imports async_session_factory lazily (patch the source); the workers import
    # it at module level (patch every split module through workers_ns).
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    yield


async def _seed(db, *, flow_state=FlowState.LLM, selected=None, insurance=None):
    """A two-doctor clinic (Dra. Ana, Dr. Bruno), one patient, one conversation."""
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            initial_flows={"enabled": True, "menu_label": "Como posso ajudar?"},
            appointment_types=[{"name": "Consulta Geral", "duration_min": 30, "is_active": True}],
            business_hours={
                day: [{"start": "08:00", "end": "18:00"}]
                for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
            },
        )
        session.add(tenant)
        await session.flush()
        ana = Professional(
            tenant_id=tenant.id,
            name="Dra. Ana",
            specialty="Cardiologia",
            about="Atendo com foco em prevenção.",
            context_doctor_message="Prefere retornos pela manhã.",
            is_active=True,
        )
        bruno = Professional(tenant_id=tenant.id, name="Dr. Bruno", is_active=True)
        session.add_all([ana, bruno])
        await session.flush()
        patient = Patient(tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id,
            patient_id=patient.id,
            flow_state=flow_state,
            flow_selected_professional_id=(ana.id if selected else None),
            flow_selected_insurance=insurance,
        )
        session.add(conversation)
        await session.flush()
        session.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.PATIENT,
                wam_id="wamid.keep",
                body="oi",
            )
        )
        await session.commit()
        for obj in (tenant, ana, bruno, patient, conversation):
            await session.refresh(obj)
        return tenant, ana, bruno, patient, conversation


async def _seed_sole(db, **kw):
    """The `_seed` clinic reduced to ONE active professional (a SOLE tenant)."""
    tenant, ana, bruno, patient, conversation = await _seed(db, **kw)
    async with db() as session:
        row = await session.get(Professional, bruno.id)
        row.is_active = False
        await session.commit()
    return tenant, ana, patient, conversation


async def _seed_future_appointment(db, tenant, patient, *, start_at):
    async with db() as session:
        appt = Appointment(
            tenant_id=tenant.id,
            patient_id=patient.id,
            google_event_id=f"evt-{uuid4()}",
            appointment_type="Consulta Geral",
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(appt)
        await session.commit()
        await session.refresh(appt)
        return appt


def _snapshots(professionals):
    return [
        SimpleNamespace(
            id=p.id,
            name=p.name,
            specialty=p.specialty,
            about=p.about,
            context_doctor_message=p.context_doctor_message,
            appointment_types=p.appointment_types,
            business_hours=p.business_hours,
        )
        for p in professionals
    ]


def _reply_ctx(conversation) -> tasks._ReplyContext:
    return tasks._ReplyContext(
        conversation_id=conversation.id,
        patient_ref="5511999999999",
        inbound_body="tanto faz",
    )


@pytest.fixture
def _captured_bubbles(monkeypatch: pytest.MonkeyPatch):
    captured: list = []

    async def _fake_dispatch(reply, bubbles, tenant=None, waba_token=None):
        captured.extend(bubbles)
        return len(bubbles)

    monkeypatch.setattr(workers_ns, "_dispatch_bubbles", _fake_dispatch)
    return captured


class _StubCalendar:
    """Every day of the window is free; no Google, no credentials."""

    def __init__(self):
        self.tzinfo = ZoneInfo("America/Sao_Paulo")

    async def list_available_days(self, start_day, days, slot_minutes=None):
        base = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return [base + timedelta(days=offset) for offset in range(min(days, 3))]


@pytest.fixture
def _stub_calendar(monkeypatch: pytest.MonkeyPatch) -> _StubCalendar:
    calendar = _StubCalendar()

    async def _fake(session, tenant, target):
        return calendar

    monkeypatch.setattr(workers_ns, "_appointment_calendar", _fake)
    return calendar


class _LogRecorder:
    """Records every structlog call - `caplog` is empty for structlog in this suite."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        def _log(event: str, **fields) -> None:
            self.records.append((level, event, fields))

        return _log


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    recorder = _LogRecorder()
    monkeypatch.setattr(workers_ns, "logger", recorder)
    return recorder


def _events(log: _LogRecorder, name: str = handback_log.EVENT_NAME) -> list[dict]:
    return [fields for _level, event, fields in log.records if event == name]


# --------------------------------------------------------------------------
# show_main_menu: the agent's own tool is a hand-back; every other menu is not
# --------------------------------------------------------------------------


async def test_the_agents_menu_tool_logs_one_handback_at_the_menu(
    db, _captured_bubbles, log
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    # Called the way `_send_bot_reply` calls it for the show_main_menu sentinel: no `source`.
    await tasks._handle_show_main_menu(
        _reply_ctx(conversation), tenant, _snapshots([ana, bruno]), patient.wa_id
    )

    (event,) = _events(log)
    assert event == {
        "conversation_id": str(conversation.id),
        "tenant_id": str(tenant.id),
        "source_tool": "show_main_menu",
        "landing_step": "menu",
        "supplied": [],
        "accepted": [],
        "dropped": {},
        "fallback": None,
        "topology": "multi",
        "channel": "whatsapp",
    }
    assert isinstance(_captured_bubbles[0], MenuBubble)
    # The pre-existing event keeps being emitted, once.
    assert len(_events(log, "conversation_menu_rendered")) == 1


async def test_the_menu_tool_without_a_tenant_is_counted_not_silent(
    db, _captured_bubbles, log
) -> None:
    _tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_show_main_menu(
        _reply_ctx(conversation), None, _snapshots([ana, bruno]), patient.wa_id
    )

    (event,) = _events(log)
    assert event["source_tool"] == "show_main_menu"
    assert event["fallback"] == "no_tenant"
    assert event["landing_step"] is None
    assert event["tenant_id"] is None
    assert _captured_bubbles == []


@pytest.mark.parametrize(
    "source", ["command", "name_captured", "verified_account", "sentinel_fallback"]
)
async def test_a_menu_rendered_for_any_other_reason_is_not_a_hand_back(
    db, _captured_bubbles, log, source
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_show_main_menu(
        _reply_ctx(conversation), tenant, _snapshots([ana, bruno]), patient.wa_id, source=source
    )

    assert _events(log) == []
    (rendered,) = _events(log, "conversation_menu_rendered")
    assert rendered["source"] == source
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_events.py -q`
Expected: 2 FAIL (`ValueError: not enough values to unpack (expected 1, got 0)` — o evento ainda não é escrito) e 4 PASS (os quatro "outras origens" já passam; são a guarda contra contagem dupla).

- [ ] **Step 3: Implement**

Em `src/secretaria/workers/shared/sentinels.py`:

1. Nos imports, logo ANTES de `from secretaria.workers.shared.context import (`, acrescentar:

```python
from secretaria.workers.shared import handback_log as hb
```

2. Trocar o trecho (o `logger` e a assinatura de `_handle_show_main_menu`):

```python
logger = get_logger(__name__)


async def _handle_show_main_menu(
    reply: _ReplyContext,
    tenant: Tenant | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
    source: str = "agent_tool",
) -> None:
```

por:

```python
logger = get_logger(__name__)

# `_handle_show_main_menu`'s `source` when the agent's own `show_main_menu` tool asked for the
# menu - the only caller of that function that is an AI hand-back.
_AGENT_TOOL_SOURCE = "agent_tool"


async def _handle_show_main_menu(
    reply: _ReplyContext,
    tenant: Tenant | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
    source: str = _AGENT_TOOL_SOURCE,
) -> None:
```

3. No fim do docstring de `_handle_show_main_menu`, trocar

```python
    Idempotent by construction: it consumes no input and derives the menu from
    the tenant + roster, so running it twice sends the same menu twice and
    leaves the same state.
    """
    if tenant is None:
        logger.warning(
            "worker_show_main_menu_without_tenant",
            conversation_id=str(reply.conversation_id),
            source=source,
        )
        return
```

por:

```python
    Idempotent by construction: it consumes no input and derives the menu from
    the tenant + roster, so running it twice sends the same menu twice and
    leaves the same state.

    Hand-back accounting: only `source="agent_tool"` records a
    `conversation_handback_entered` event here. `/menu`, the name step and the
    identity cards render the same menu without the AI being involved, and the
    malformed-sentinel fallbacks are recorded by the handler that fell back
    (`_fallback_to_menu`), so one hand-back is never counted twice.
    """
    if tenant is None:
        logger.warning(
            "worker_show_main_menu_without_tenant",
            conversation_id=str(reply.conversation_id),
            source=source,
        )
        if source == _AGENT_TOOL_SOURCE:
            hb.log_handback_entered(
                conversation_id=reply.conversation_id,
                tenant_id=reply.tenant_id,
                source_tool=hb.SOURCE_SHOW_MAIN_MENU,
                landing_step=None,
                fallback=hb.FALLBACK_NO_TENANT,
                topology=booking_topology(professionals),
                channel=reply.channel,
            )
        return
```

4. Entre a construção de `result = FlowRouterResult(... flow_state=FlowState.MENU,\n    )` e `rendered = await _apply_flow_result(`, inserir:

```python
    if source == _AGENT_TOOL_SOURCE:
        # Logged before the write and the send, so a failure there cannot erase the fact
        # that the hand-back reached the menu.
        hb.log_handback_entered(
            conversation_id=reply.conversation_id,
            tenant_id=tenant.id,
            source_tool=hb.SOURCE_SHOW_MAIN_MENU,
            landing_step=hb.LANDING_MENU,
            topology=booking_topology(professionals),
            channel=reply.channel,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_events.py tests/test_agent_menu_tools.py tests/test_menu_command.py -q`
Expected: PASS (os testes antigos do menu continuam verdes; a assinatura não mudou).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff check src/secretaria/workers/shared/sentinels.py tests/test_handback_events.py
uvx ruff format tests/test_handback_events.py
uvx ruff format --diff src/secretaria/workers/shared/sentinels.py | grep -c '^@@'   # must not exceed 10
git add src/secretaria/workers/shared/sentinels.py tests/test_handback_events.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(observability): the agent's show_main_menu tool records its hand-back

Only source="agent_tool" counts; /menu, the name step and identity cards render the same
menu without the AI, and sentinel fallbacks are recorded by the handler that fell back.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 3: `select_professional` + helpers de pouso e de fallback

**Files:**
- Modify: `src/secretaria/workers/shared/sentinels.py` (import `Mapping, Sequence`; helpers `_fallback_to_menu` e `_land_handback`; `_handle_select_professional`)
- Modify: `tests/test_handback_events.py`

**Interfaces:**
- Consumes: Task 1 (`hb.*`), Task 2 (`_handle_show_main_menu`, fixtures do arquivo de teste).
- Produces (Tasks 4–7 usam):
  - `async def _fallback_to_menu(reply: _ReplyContext, *, source_tool: str, reason: str, tenant: Tenant | None, professionals: list | None, patient_wa: str | None, redis=None, waba_token: str | None = None, supplied: Sequence[str] = (), accepted: Sequence[str] = (), dropped: Mapping[str, str] | None = None, topology: str | None = None) -> None` — registra UM evento (`landing_step="menu"`, ou `None` sem tenant) e então chama `_handle_show_main_menu(source="sentinel_fallback")`.
  - `async def _land_handback(reply: _ReplyContext, result: FlowRouterResult, patient_wa: str | None, *, source_tool: str, tenant: Tenant | None, professionals: list | None, redis=None, waba_token: str | None = None, supplied: Sequence[str] = (), accepted: Sequence[str] = (), dropped: Mapping[str, str] | None = None, fallback: str | None = None, topology: str | None = None) -> bool` — registra UM evento a partir de `hb.landing_of(result)` (o `fallback` explícito vence o do resultado) e então `_apply_flow_result(...)`.

- [ ] **Step 1: Write the failing tests**

Em `tests/test_handback_events.py`: acrescentar ao bloco de imports, imediatamente antes de `from secretaria.core import database as core_database  # noqa: E402` (ordem do ruff `I001`)

```python
from secretaria.ai.graph import SELECT_PROFESSIONAL_SENTINEL_PREFIX  # noqa: E402
```

e ao fim do arquivo:

```python
# --------------------------------------------------------------------------
# select_professional_and_continue
# --------------------------------------------------------------------------


async def test_select_professional_logs_the_step_it_landed_on(db, _captured_bubbles, log) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_select_professional(
        _reply_ctx(conversation),
        f"{SELECT_PROFESSIONAL_SENTINEL_PREFIX}{ana.id}",
        tenant,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["source_tool"] == "select_professional"
    assert event["landing_step"] == "awaiting_service"
    assert event["supplied"] == ["professional"]
    assert event["accepted"] == ["professional"]
    assert event["dropped"] == {}
    assert event["fallback"] is None
    assert event["topology"] == "multi"
    assert event["tenant_id"] == str(tenant.id)


async def test_an_unknown_professional_falls_back_to_the_menu_and_is_counted_once(
    db, _captured_bubbles, log
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_select_professional(
        _reply_ctx(conversation),
        f"{SELECT_PROFESSIONAL_SENTINEL_PREFIX}{uuid4()}",  # not on the roster
        tenant,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["fallback"] == "unknown_professional"
    assert event["landing_step"] == "menu"
    assert event["supplied"] == ["professional"]
    assert event["accepted"] == []
    assert event["dropped"] == {"professional": "unknown_professional"}
    # The menu the fallback renders is the pre-existing event - NOT a second hand-back.
    (rendered,) = _events(log, "conversation_menu_rendered")
    assert rendered["source"] == "sentinel_fallback"
    assert len(_captured_bubbles) == 1
    assert isinstance(_captured_bubbles[0], MenuBubble)


async def test_a_malformed_professional_id_is_a_bad_sentinel(db, _captured_bubbles, log) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_select_professional(
        _reply_ctx(conversation),
        f"{SELECT_PROFESSIONAL_SENTINEL_PREFIX}not-a-uuid",
        tenant,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["fallback"] == "bad_sentinel"
    assert event["landing_step"] == "menu"
    assert event["supplied"] == []
    assert event["dropped"] == {}


async def test_select_professional_without_a_tenant_is_counted_not_silent(
    db, _captured_bubbles, log
) -> None:
    _tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_select_professional(
        _reply_ctx(conversation),
        f"{SELECT_PROFESSIONAL_SENTINEL_PREFIX}{ana.id}",
        None,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["fallback"] == "no_tenant"
    assert event["landing_step"] is None  # no tenant, so no menu was rendered either
    assert event["accepted"] == ["professional"]
    assert event["tenant_id"] is None
    assert _captured_bubbles == []


async def test_a_doctor_with_no_services_lands_on_the_config_alert(
    db, _captured_bubbles, log, monkeypatch
) -> None:
    alerted: list = []

    async def _fake_alert(reply, result, **_kwargs):
        alerted.append(result.professional_config_gap)

    monkeypatch.setattr(workers_ns, "_handle_professional_config_incomplete", _fake_alert)
    tenant, ana, bruno, patient, conversation = await _seed(db)
    snapshots = _snapshots([ana, bruno])
    snapshots[0].appointment_types = []  # her own empty list: nothing to book

    await tasks._handle_select_professional(
        _reply_ctx(conversation),
        f"{SELECT_PROFESSIONAL_SENTINEL_PREFIX}{ana.id}",
        tenant,
        None,
        snapshots,
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["landing_step"] == "config_incomplete"
    assert event["fallback"] == "professional_config_incomplete"
    assert event["accepted"] == ["professional"]
    assert alerted == ["services"]


async def test_the_event_is_logged_before_the_flow_state_is_written(
    db, _captured_bubbles, log, monkeypatch
) -> None:
    """A failure while persisting/sending must not erase the fact that the hand-back landed."""

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("persist exploded")

    monkeypatch.setattr(workers_ns, "_apply_flow_result", _boom)
    tenant, ana, bruno, patient, conversation = await _seed(db)

    with pytest.raises(RuntimeError, match="persist exploded"):
        await tasks._handle_select_professional(
            _reply_ctx(conversation),
            f"{SELECT_PROFESSIONAL_SENTINEL_PREFIX}{ana.id}",
            tenant,
            None,
            _snapshots([ana, bruno]),
            patient.wa_id,
        )

    (event,) = _events(log)
    assert event["landing_step"] == "awaiting_service"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_events.py -q`
Expected: os 6 testes novos FAIL (`ValueError: not enough values to unpack (expected 1, got 0)`); os 6 da Task 2 continuam passando.

- [ ] **Step 3: Implement**

Em `src/secretaria/workers/shared/sentinels.py`:

1. Nos imports stdlib, trocar

```python
import json
from types import SimpleNamespace
```

por

```python
import json
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
```

2. Imediatamente ANTES de `async def _handle_select_professional(`, inserir os dois helpers (duas linhas em branco antes e depois de cada um):

```python
async def _fallback_to_menu(
    reply: _ReplyContext,
    *,
    source_tool: str,
    reason: str,
    tenant: Tenant | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
    supplied: Sequence[str] = (),
    accepted: Sequence[str] = (),
    dropped: Mapping[str, str] | None = None,
    topology: str | None = None,
) -> None:
    """A hand-back that could not land: record it ONCE, then show the plain menu.

    The menu itself goes through `_handle_show_main_menu(source="sentinel_fallback")`, which
    records no hand-back event of its own - this is the single place the fallback is counted,
    so a hand-back is never logged twice or not at all. `landing_step` is None without a
    tenant: `_handle_show_main_menu` renders nothing then, so nothing landed.
    """
    hb.log_handback_entered(
        conversation_id=reply.conversation_id,
        tenant_id=tenant.id if tenant is not None else reply.tenant_id,
        source_tool=source_tool,
        landing_step=hb.LANDING_MENU if tenant is not None else None,
        supplied=supplied,
        accepted=accepted,
        dropped=dropped,
        fallback=reason,
        topology=topology if topology is not None else booking_topology(professionals),
        channel=reply.channel,
    )
    await _handle_show_main_menu(
        reply,
        tenant,
        professionals,
        patient_wa,
        redis=redis,
        waba_token=waba_token,
        source="sentinel_fallback",
    )


async def _land_handback(
    reply: _ReplyContext,
    result: FlowRouterResult,
    patient_wa: str | None,
    *,
    source_tool: str,
    tenant: Tenant | None,
    professionals: list | None,
    redis=None,
    waba_token: str | None = None,
    supplied: Sequence[str] = (),
    accepted: Sequence[str] = (),
    dropped: Mapping[str, str] | None = None,
    fallback: str | None = None,
    topology: str | None = None,
) -> bool:
    """Record where a hand-back lands, THEN persist the flow state and send the bubbles.

    The event goes first on purpose: `_apply_flow_result` writes the flow row and talks to
    WhatsApp / the Portal, and a failure there must not erase the fact that the hand-back
    reached a step. `fallback` names a bounce the CALLER detected (e.g. nothing to manage);
    otherwise the result itself decides - a calendar outage or an unbookable doctor is a
    fallback too (`handback_log.landing_of`).
    """
    landing_step, result_fallback = hb.landing_of(result)
    hb.log_handback_entered(
        conversation_id=reply.conversation_id,
        tenant_id=tenant.id if tenant is not None else reply.tenant_id,
        source_tool=source_tool,
        landing_step=landing_step,
        supplied=supplied,
        accepted=accepted,
        dropped=dropped,
        fallback=fallback or result_fallback,
        topology=topology if topology is not None else booking_topology(professionals),
        channel=reply.channel,
    )
    return await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )


```

3. Em `_handle_select_professional`, trocar o bloco

```python
    raw_id = reply_text[len(SELECT_PROFESSIONAL_SENTINEL_PREFIX) :]
    professional = None
    try:
        professional_id = UUID(raw_id)
    except ValueError:
        logger.error("worker_select_professional_bad_sentinel", raw=raw_id[:64])
    else:
        professional = next((p for p in professionals or [] if p.id == professional_id), None)
    tenant_snapshot = flow_snapshot[1] if flow_snapshot is not None else tenant
    if professional is None or tenant_snapshot is None:
        logger.warning(
            "worker_select_professional_unresolved",
            conversation_id=str(reply.conversation_id),
        )
        await _handle_show_main_menu(
            reply,
            tenant,
            professionals,
            patient_wa,
            redis=redis,
            waba_token=waba_token,
            source="sentinel_fallback",
        )
        return
    result = _enter_professional_services(professional, tenant_snapshot)
```

por

```python
    raw_id = reply_text[len(SELECT_PROFESSIONAL_SENTINEL_PREFIX) :]
    professional = None
    bad_sentinel = False
    try:
        professional_id = UUID(raw_id)
    except ValueError:
        bad_sentinel = True
        logger.error("worker_select_professional_bad_sentinel", raw=raw_id[:64])
    else:
        professional = next((p for p in professionals or [] if p.id == professional_id), None)
    tenant_snapshot = flow_snapshot[1] if flow_snapshot is not None else tenant
    if professional is None or tenant_snapshot is None:
        logger.warning(
            "worker_select_professional_unresolved",
            conversation_id=str(reply.conversation_id),
        )
        # Most specific reason first: a malformed id, a doctor who is no longer on the
        # active roster, or no tenant to render a menu for.
        supplied: tuple[str, ...] = ()
        accepted: tuple[str, ...] = ()
        dropped: dict[str, str] = {}
        if bad_sentinel:
            reason = hb.FALLBACK_BAD_SENTINEL
        elif professional is None:
            reason = hb.FALLBACK_UNKNOWN_PROFESSIONAL
            supplied = (hb.FIELD_PROFESSIONAL,)
            dropped = {hb.FIELD_PROFESSIONAL: hb.DROP_UNKNOWN_PROFESSIONAL}
        else:
            reason = hb.FALLBACK_NO_TENANT
            supplied = accepted = (hb.FIELD_PROFESSIONAL,)
        await _fallback_to_menu(
            reply,
            source_tool=hb.SOURCE_SELECT_PROFESSIONAL,
            reason=reason,
            tenant=tenant,
            professionals=professionals,
            patient_wa=patient_wa,
            redis=redis,
            waba_token=waba_token,
            supplied=supplied,
            accepted=accepted,
            dropped=dropped,
        )
        return
    result = _enter_professional_services(professional, tenant_snapshot)
```

4. No fim da mesma função, trocar

```python
        result.flow_selected_insurance = getattr(
            flow_snapshot[0], "flow_selected_insurance", None
        )
    await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )

async def _handle_manage_appointment(
```

por

```python
        result.flow_selected_insurance = getattr(
            flow_snapshot[0], "flow_selected_insurance", None
        )
    await _land_handback(
        reply,
        result,
        patient_wa,
        source_tool=hb.SOURCE_SELECT_PROFESSIONAL,
        tenant=tenant,
        professionals=professionals,
        redis=redis,
        waba_token=waba_token,
        supplied=(hb.FIELD_PROFESSIONAL,),
        accepted=(hb.FIELD_PROFESSIONAL,),
    )

async def _handle_manage_appointment(
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_events.py tests/test_agent_menu_tools.py -q`
Expected: PASS (inclusive `test_handle_select_professional_*` antigos).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff check src/secretaria/workers/shared/sentinels.py tests/test_handback_events.py
uvx ruff format tests/test_handback_events.py
uvx ruff format --diff src/secretaria/workers/shared/sentinels.py | grep -c '^@@'   # must not exceed 10
git add src/secretaria/workers/shared/sentinels.py tests/test_handback_events.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(observability): select_professional records where the patient landed

Adds the two helpers every handler exit goes through (_fallback_to_menu, _land_handback):
the event is written before the flow state and the send, and a fallback counts once.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 4: `manage_existing_appointment` — pouso, "nada para gerenciar" e retornos silenciosos

**Files:**
- Modify: `src/secretaria/workers/shared/sentinels.py` (helper `_log_no_landing`; `_handle_manage_appointment`)
- Modify: `tests/test_handback_events.py`

**Interfaces:**
- Consumes: Tasks 1–3 (`hb.*`, `_fallback_to_menu`, `_land_handback`).
- Produces: `def _log_no_landing(reply: _ReplyContext, *, source_tool: str, reason: str, tenant: Tenant | None, professionals: list | None, supplied: Sequence[str] = (), accepted: Sequence[str] = (), topology: str | None = None) -> None` — o evento de um hand-back que não fez nada (sem menu, sem etapa): `landing_step=None`, `fallback=reason`.

- [ ] **Step 1: Write the failing tests**

Em `tests/test_handback_events.py`, trocar o import `from secretaria.workers.shared import handback_log  # noqa: E402` por

```python
from secretaria.workers.shared import handback_log, sentinels  # noqa: E402
```

e acrescentar ao fim do arquivo:

```python
# --------------------------------------------------------------------------
# manage_existing_appointment
# --------------------------------------------------------------------------


async def test_manage_logs_the_step_it_landed_on(db, _captured_bubbles, log) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)
    await _seed_future_appointment(
        db, tenant, patient, start_at=datetime.now(UTC) + timedelta(days=2)
    )

    await tasks._handle_manage_appointment(
        _reply_ctx(conversation), "cancel", tenant, _snapshots([ana, bruno]), patient.wa_id
    )

    (event,) = _events(log)
    assert event["source_tool"] == "manage_existing_appointment"
    assert event["landing_step"] == "manage_cancel_confirm"
    assert event["supplied"] == ["action"]
    assert event["accepted"] == ["action"]
    assert event["fallback"] is None
    assert event["topology"] == "multi"
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_step == event["landing_step"]


async def test_manage_with_two_appointments_lands_on_the_pick_list(
    db, _captured_bubbles, log
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)
    now = datetime.now(UTC)
    await _seed_future_appointment(db, tenant, patient, start_at=now + timedelta(days=1))
    await _seed_future_appointment(db, tenant, patient, start_at=now + timedelta(days=5))

    await tasks._handle_manage_appointment(
        _reply_ctx(conversation), "cancel", tenant, _snapshots([ana, bruno]), patient.wa_id
    )

    (event,) = _events(log)
    assert event["landing_step"] == "manage_pick_cancel"
    assert event["fallback"] is None


async def test_manage_with_nothing_to_manage_lands_on_the_menu_and_says_why(
    db, _captured_bubbles, log
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_manage_appointment(
        _reply_ctx(conversation), "reschedule", tenant, _snapshots([ana, bruno]), patient.wa_id
    )

    (event,) = _events(log)
    assert event["landing_step"] == "menu"
    assert event["fallback"] == "no_appointments"
    assert event["accepted"] == ["action"]


async def test_manage_with_an_unknown_action_is_a_bad_sentinel(db, _captured_bubbles, log) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_manage_appointment(
        _reply_ctx(conversation), "excluir", tenant, _snapshots([ana, bruno]), patient.wa_id
    )

    (event,) = _events(log)
    assert event["fallback"] == "bad_sentinel"
    assert event["landing_step"] == "menu"
    assert event["supplied"] == []
    assert isinstance(_captured_bubbles[0], MenuBubble)


async def test_manage_without_a_tenant_is_counted_not_silent(db, _captured_bubbles, log) -> None:
    _tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_manage_appointment(
        _reply_ctx(conversation), "cancel", None, _snapshots([ana, bruno]), patient.wa_id
    )

    (event,) = _events(log)
    assert event["fallback"] == "no_tenant"
    assert event["landing_step"] is None
    assert event["tenant_id"] is None
    assert _captured_bubbles == []


async def test_manage_without_flows_is_counted_not_silent(
    db, _captured_bubbles, log, monkeypatch
) -> None:
    # `flows_enabled` is always True today; the guard is defensive and still has to count.
    monkeypatch.setattr(sentinels, "flows_enabled", lambda _tenant: False)
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_manage_appointment(
        _reply_ctx(conversation), "cancel", tenant, _snapshots([ana, bruno]), patient.wa_id
    )

    (event,) = _events(log)
    assert event["fallback"] == "without_flows"
    assert event["landing_step"] is None
    assert _captured_bubbles == []


async def test_manage_for_a_conversation_without_a_patient_is_counted_not_silent(
    db, _captured_bubbles, log
) -> None:
    tenant, ana, bruno, patient, _conversation = await _seed(db)
    orphan = tasks._ReplyContext(
        conversation_id=uuid4(), patient_ref=patient.wa_id, inbound_body="tanto faz"
    )

    await tasks._handle_manage_appointment(
        orphan, "cancel", tenant, _snapshots([ana, bruno]), patient.wa_id
    )

    (event,) = _events(log)
    assert event["fallback"] == "no_patient"
    assert event["landing_step"] is None
    assert _captured_bubbles == []


async def test_a_failure_before_the_landing_is_known_propagates_and_logs_no_event(
    db, _captured_bubbles, log, monkeypatch
) -> None:
    """Decision (plan P1): an infrastructure failure before the landing is known is not a
    landing. It propagates exactly as before (the job fails, the turn safety net answers);
    the gap between `ai_run_agent_manage_appointment` and this event is how it is spotted."""

    async def _db_down(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(workers_ns, "load_upcoming_appointments", _db_down)
    tenant, ana, bruno, patient, conversation = await _seed(db)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await tasks._handle_manage_appointment(
            _reply_ctx(conversation), "cancel", tenant, _snapshots([ana, bruno]), patient.wa_id
        )

    assert _events(log) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_events.py -q`
Expected: os 7 primeiros testes novos FAIL (`ValueError: not enough values to unpack (expected 1, got 0)`); `test_a_failure_before_the_landing...` PASSA já agora (fixa a decisão); Tasks 2–3 continuam verdes.

- [ ] **Step 3: Implement**

Em `src/secretaria/workers/shared/sentinels.py`:

1. Imediatamente depois de `_land_handback` (e antes de `async def _handle_select_professional(`), inserir:

```python
def _log_no_landing(
    reply: _ReplyContext,
    *,
    source_tool: str,
    reason: str,
    tenant: Tenant | None,
    professionals: list | None,
    supplied: Sequence[str] = (),
    accepted: Sequence[str] = (),
    topology: str | None = None,
) -> None:
    """A hand-back that did nothing at all - no menu, no flow step - still gets its event."""
    hb.log_handback_entered(
        conversation_id=reply.conversation_id,
        tenant_id=tenant.id if tenant is not None else reply.tenant_id,
        source_tool=source_tool,
        landing_step=None,
        supplied=supplied,
        accepted=accepted,
        fallback=reason,
        topology=topology if topology is not None else booking_topology(professionals),
        channel=reply.channel,
    )


```

2. Em `_handle_manage_appointment`, trocar

```python
    if action not in ("reschedule", "cancel"):
        logger.warning("worker_manage_appointment_bad_action", action=action[:32])
        await _handle_show_main_menu(
            reply,
            tenant,
            professionals,
            patient_wa,
            redis=redis,
            waba_token=waba_token,
            source="sentinel_fallback",
        )
        return
    if tenant is None or not flows_enabled(tenant):
        logger.warning(
            "worker_manage_appointment_without_flows",
            conversation_id=str(reply.conversation_id),
        )
        return
```

por

```python
    if action not in ("reschedule", "cancel"):
        logger.warning("worker_manage_appointment_bad_action", action=action[:32])
        await _fallback_to_menu(
            reply,
            source_tool=hb.SOURCE_MANAGE_EXISTING_APPOINTMENT,
            reason=hb.FALLBACK_BAD_SENTINEL,
            tenant=tenant,
            professionals=professionals,
            patient_wa=patient_wa,
            redis=redis,
            waba_token=waba_token,
        )
        return
    if tenant is None or not flows_enabled(tenant):
        logger.warning(
            "worker_manage_appointment_without_flows",
            conversation_id=str(reply.conversation_id),
        )
        _log_no_landing(
            reply,
            source_tool=hb.SOURCE_MANAGE_EXISTING_APPOINTMENT,
            reason=hb.FALLBACK_NO_TENANT if tenant is None else hb.FALLBACK_WITHOUT_FLOWS,
            tenant=tenant,
            professionals=professionals,
            supplied=(hb.FIELD_ACTION,),
            accepted=(hb.FIELD_ACTION,),
        )
        return
```

3. Trocar

```python
        if patient_id is None:
            logger.warning(
                "worker_manage_appointment_no_patient",
                conversation_id=str(reply.conversation_id),
            )
            return
```

por

```python
        if patient_id is None:
            logger.warning(
                "worker_manage_appointment_no_patient",
                conversation_id=str(reply.conversation_id),
            )
            _log_no_landing(
                reply,
                source_tool=hb.SOURCE_MANAGE_EXISTING_APPOINTMENT,
                reason=hb.FALLBACK_NO_PATIENT,
                tenant=tenant,
                professionals=professionals,
                supplied=(hb.FIELD_ACTION,),
                accepted=(hb.FIELD_ACTION,),
            )
            return
```

4. Trocar o fim da função

```python
    result = await enter_manage_action(
        action, tenant, appointments, professionals, calendar=manage_calendar
    )
    await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )
```

por

```python
    result = await enter_manage_action(
        action, tenant, appointments, professionals, calendar=manage_calendar
    )
    await _land_handback(
        reply,
        result,
        patient_wa,
        source_tool=hb.SOURCE_MANAGE_EXISTING_APPOINTMENT,
        tenant=tenant,
        professionals=professionals,
        redis=redis,
        waba_token=waba_token,
        supplied=(hb.FIELD_ACTION,),
        accepted=(hb.FIELD_ACTION,),
        # A patient with nothing to manage lands on the menu with an explanation: the
        # hand-back worked, just not where the agent meant it to.
        fallback=None if appointments else hb.FALLBACK_NO_APPOINTMENTS,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_events.py tests/test_agent_menu_tools.py -q`
Expected: PASS (inclusive `test_handle_manage_appointment_*` antigos).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff check src/secretaria/workers/shared/sentinels.py tests/test_handback_events.py
uvx ruff format tests/test_handback_events.py
uvx ruff format --diff src/secretaria/workers/shared/sentinels.py | grep -c '^@@'   # must not exceed 10
git add src/secretaria/workers/shared/sentinels.py tests/test_handback_events.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(observability): manage_existing_appointment records its landing and every silent return

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 5: `start_guided_booking`

**Files:**
- Modify: `src/secretaria/workers/shared/sentinels.py` (`_handle_start_guided_booking`)
- Modify: `tests/test_handback_events.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: `_handle_start_guided_booking` (assinatura inalterada) registra um evento por saída; `conversation_guided_booking_entered` segue sendo emitido.

- [ ] **Step 1: Write the failing tests**

Em `tests/test_handback_events.py`, trocar o import `from secretaria.ai.graph import SELECT_PROFESSIONAL_SENTINEL_PREFIX  # noqa: E402` por

```python
from secretaria.ai.graph import (  # noqa: E402
    SELECT_PROFESSIONAL_SENTINEL_PREFIX,
    START_GUIDED_BOOKING_SENTINEL_PREFIX,
)
```

e acrescentar ao fim:

```python
# --------------------------------------------------------------------------
# start_guided_booking
# --------------------------------------------------------------------------


async def test_guided_booking_logs_the_step_it_landed_on(
    db, _captured_bubbles, _stub_calendar, log
) -> None:
    tenant, ana, patient, conversation = await _seed_sole(db)

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation),
        f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral",
        tenant,
        _snapshots([ana]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["source_tool"] == "start_guided_booking"
    assert event["landing_step"] == "awaiting_day"
    assert event["supplied"] == ["service"]
    assert event["accepted"] == ["service"]
    assert event["fallback"] is None
    assert event["topology"] == "sole"
    # The pre-existing event keeps being emitted, once.
    assert len(_events(log, "conversation_guided_booking_entered")) == 1


async def test_guided_booking_turns_a_multi_doctor_clinic_away_and_says_so(
    db, _captured_bubbles, _stub_calendar, log
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation),
        f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral",
        tenant,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["fallback"] == "multi_professional"
    assert event["landing_step"] == "menu"
    assert event["topology"] == "multi"
    assert event["supplied"] == ["service"]
    assert isinstance(_captured_bubbles[0], MenuBubble)


async def test_guided_booking_without_a_tenant_is_counted_not_silent(
    db, _captured_bubbles, _stub_calendar, log
) -> None:
    _tenant, ana, patient, conversation = await _seed_sole(db)

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation),
        f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral",
        None,
        _snapshots([ana]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["fallback"] == "no_tenant"
    assert event["landing_step"] is None
    assert event["supplied"] == ["service"]
    assert _captured_bubbles == []


async def test_guided_booking_with_no_agenda_lands_on_a_person(
    db, _captured_bubbles, log, monkeypatch
) -> None:
    handed_off: list = []

    async def _no_calendar(session, tenant, target):
        return None

    async def _fake_unavailable(reply, redis=None, tenant=None, waba_token=None):
        handed_off.append(reply.conversation_id)

    monkeypatch.setattr(workers_ns, "_appointment_calendar", _no_calendar)
    monkeypatch.setattr(workers_ns, "_handle_calendar_unavailable", _fake_unavailable)
    tenant, ana, patient, conversation = await _seed_sole(db)

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation),
        f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral",
        tenant,
        _snapshots([ana]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["landing_step"] == "human_handover"
    assert event["fallback"] == "calendar_unavailable"
    assert handed_off == [conversation.id]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_events.py -q`
Expected: os 4 testes novos FAIL (`ValueError: not enough values to unpack (expected 1, got 0)`); o resto verde.

- [ ] **Step 3: Implement**

Em `_handle_start_guided_booking` (`src/secretaria/workers/shared/sentinels.py`):

1. Trocar

```python
    appointment_type = reply_text[len(START_GUIDED_BOOKING_SENTINEL_PREFIX) :].strip() or None
    if tenant is None or not flows_enabled(tenant):
        # The tool is only ever exposed to flow-enabled tenants, so this is a
        # defensive count-only warning, same style as _handle_manage_appointment.
        logger.warning(
            "worker_start_guided_booking_without_flows",
            conversation_id=str(reply.conversation_id),
        )
        return
```

por

```python
    appointment_type = reply_text[len(START_GUIDED_BOOKING_SENTINEL_PREFIX) :].strip() or None
    # The tool already proved the service against the catalog, so it is both supplied and
    # accepted; None means the clinic has no catalog and nothing was supplied.
    supplied = (hb.FIELD_SERVICE,) if appointment_type is not None else ()
    if tenant is None or not flows_enabled(tenant):
        # The tool is only ever exposed to flow-enabled tenants, so this is a
        # defensive count-only warning, same style as _handle_manage_appointment.
        logger.warning(
            "worker_start_guided_booking_without_flows",
            conversation_id=str(reply.conversation_id),
        )
        _log_no_landing(
            reply,
            source_tool=hb.SOURCE_START_GUIDED_BOOKING,
            reason=hb.FALLBACK_NO_TENANT if tenant is None else hb.FALLBACK_WITHOUT_FLOWS,
            tenant=tenant,
            professionals=professionals,
            supplied=supplied,
            accepted=supplied,
        )
        return
```

2. Trocar

```python
        logger.warning(
            "worker_start_guided_booking_multi_professional",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        await _handle_show_main_menu(
            reply,
            tenant,
            professionals,
            patient_wa,
            redis=redis,
            waba_token=waba_token,
            source="sentinel_fallback",
        )
        return
```

por

```python
        logger.warning(
            "worker_start_guided_booking_multi_professional",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        await _fallback_to_menu(
            reply,
            source_tool=hb.SOURCE_START_GUIDED_BOOKING,
            reason=hb.FALLBACK_MULTI_PROFESSIONAL,
            tenant=tenant,
            professionals=professionals,
            patient_wa=patient_wa,
            redis=redis,
            waba_token=waba_token,
            supplied=supplied,
            accepted=supplied,
            topology=booking_topology(professional_rows),
        )
        return
```

3. Trocar o fim da função (ancorado na função seguinte)

```python
        has_type=appointment_type is not None,
        flow_step=result.flow_step,
    )
    await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )

async def _handle_set_booking_draft(
```

por

```python
        has_type=appointment_type is not None,
        flow_step=result.flow_step,
    )
    await _land_handback(
        reply,
        result,
        patient_wa,
        source_tool=hb.SOURCE_START_GUIDED_BOOKING,
        tenant=tenant,
        professionals=professionals,
        redis=redis,
        waba_token=waba_token,
        supplied=supplied,
        accepted=supplied,
        topology=booking_topology(professional_rows),
    )

async def _handle_set_booking_draft(
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_events.py tests/test_agent_menu_tools.py -q`
Expected: PASS (inclusive os `test_handle_start_guided_booking_*` antigos).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff check src/secretaria/workers/shared/sentinels.py tests/test_handback_events.py
uvx ruff format tests/test_handback_events.py
uvx ruff format --diff src/secretaria/workers/shared/sentinels.py | grep -c '^@@'   # must not exceed 10
git add src/secretaria/workers/shared/sentinels.py tests/test_handback_events.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(observability): start_guided_booking records its landing and its bounces

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 6: `set_booking_draft` — o ramo "só seleção" e todas as saídas

**Files:**
- Modify: `src/secretaria/workers/shared/sentinels.py` (helper `_draft_verdicts`; `_handle_set_booking_draft` inteira — é a última função do arquivo)
- Modify: `tests/test_handback_events.py`

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces: `def _draft_verdicts(*, appointment_type: str | None, canonical_type: str | None, professional_id: UUID | None, professional: object | None, insurance_text: str | None, insurance: str | None) -> tuple[tuple[str, ...], dict[str, str]]` — `(accepted, dropped)`: nomes de campo e códigos, nunca valores; só olha campos que o agente enviou (convênio guardado na conversa não é "enviado"). `_handle_set_booking_draft` (assinatura inalterada) registra um evento por saída; `conversation_booking_draft_entered` segue emitido no caminho de sucesso. P2 reescreve este handler e mantém o contrato de `log_handback_entered`.

- [ ] **Step 1: Write the failing tests**

Em `tests/test_handback_events.py`, acrescentar a linha `import json  # noqa: E402` imediatamente antes de `from datetime import UTC, datetime, timedelta  # noqa: E402` (sem linha em branco entre elas), trocar o import de `secretaria.ai.graph` por

```python
from secretaria.ai.graph import (  # noqa: E402
    BOOKING_DRAFT_SENTINEL_PREFIX,
    SELECT_PROFESSIONAL_SENTINEL_PREFIX,
    START_GUIDED_BOOKING_SENTINEL_PREFIX,
)
```

e acrescentar ao fim do arquivo:

```python
# --------------------------------------------------------------------------
# set_booking_draft
# --------------------------------------------------------------------------


@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize("attendee", [None, "", "Atendido Teste"])
async def test_selection_only_draft_logs_the_administrative_step_it_lands_on(
    db, _captured_bubbles, _stub_calendar, log, multi, attendee
) -> None:
    """The most common hand-back: an empty draft. It used to log nothing about where it landed."""
    if multi:
        tenant, ana, bruno, patient, conversation = await _seed(db)
    else:
        tenant, ana, patient, conversation = await _seed_sole(db)
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        conv.flow_attendee_name = attendee
        await session.commit()

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + "{}",
        tenant,
        None,
        [],
        patient.wa_id,
    )

    expected = (
        "awaiting_attendee_choice"
        if attendee is None
        else "awaiting_professional"
        if multi
        else "awaiting_service"
    )
    (event,) = _events(log)
    assert event["source_tool"] == "set_booking_draft"
    assert event["landing_step"] == expected
    assert event["supplied"] == []
    assert event["accepted"] == []
    assert event["dropped"] == {}
    assert event["fallback"] is None
    assert event["topology"] == ("multi" if multi else "sole")
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_step == event["landing_step"]


async def test_selection_only_draft_on_a_clinic_with_no_bookable_catalog_is_a_counted_fallback(
    db, _captured_bubbles, _stub_calendar, log
) -> None:
    tenant, ana, patient, conversation = await _seed_sole(db)
    async with db() as session:
        doctor = await session.get(Professional, ana.id)
        doctor.appointment_types = []
        await session.commit()

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + "{}",
        tenant,
        None,
        [],
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["fallback"] == "no_bookable_catalog"
    assert event["landing_step"] == "menu"
    assert isinstance(_captured_bubbles[0], MenuBubble)


@pytest.mark.parametrize(
    "multi, build, fallback, supplied, accepted, dropped",
    [
        pytest.param(
            True,
            lambda ana: {"t": "Botox", "p": str(ana.id)},
            "invalid_selection",
            ["service", "professional"],
            ["professional"],
            {"service": "not_in_catalog"},
            id="service_not_in_catalog",
        ),
        pytest.param(
            True,
            lambda ana: {"t": "Consulta Geral", "p": str(uuid4())},
            "invalid_selection",
            ["service", "professional"],
            ["service"],
            {"professional": "unknown_professional"},
            id="professional_not_on_the_roster",
        ),
        pytest.param(
            True,
            lambda ana: {"t": "Consulta Geral"},
            "missing_professional",
            ["service"],
            ["service"],
            {},
            id="multi_clinic_service_without_doctor",
        ),
        pytest.param(
            False,
            lambda ana: {"p": str(ana.id)},
            "invalid_selection",
            ["professional"],
            ["professional"],
            {},
            id="sole_clinic_doctor_without_service",
        ),
    ],
)
async def test_a_draft_that_cannot_land_bounces_to_the_menu_with_its_reason(
    db, _captured_bubbles, _stub_calendar, log, multi, build, fallback, supplied, accepted, dropped
) -> None:
    if multi:
        tenant, ana, bruno, patient, conversation = await _seed(db)
    else:
        tenant, ana, patient, conversation = await _seed_sole(db)

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps(build(ana)),
        tenant,
        None,
        [],
        patient.wa_id,
    )

    (event,) = _events(log)  # exactly one: the menu fallback is not a second hand-back
    assert event["fallback"] == fallback
    assert event["landing_step"] == "menu"
    assert event["supplied"] == supplied
    assert event["accepted"] == accepted
    assert event["dropped"] == dropped
    assert isinstance(_captured_bubbles[0], MenuBubble)


@pytest.mark.parametrize(
    "service, landing, accepted",
    [
        ("Consulta Geral", "awaiting_day", ["service", "professional"]),
        (None, "awaiting_service", ["professional"]),
    ],
)
async def test_a_draft_that_lands_logs_the_step_and_keeps_the_old_event(
    db, _captured_bubbles, _stub_calendar, log, service, landing, accepted
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps({"t": service, "p": str(ana.id)}),
        tenant,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["landing_step"] == landing
    assert event["supplied"] == accepted
    assert event["accepted"] == accepted
    assert event["fallback"] is None
    assert event["topology"] == "multi"
    assert len(_events(log, "conversation_booking_draft_entered")) == 1


@pytest.mark.parametrize(
    "typed, accepted, dropped",
    [
        ("unimed", ["insurance"], {}),
        ("Inventado", [], {"insurance": "unmatched_plan"}),
    ],
)
async def test_a_typed_convenio_is_accepted_or_dropped_by_name_only(
    db, _captured_bubbles, _stub_calendar, log, typed, accepted, dropped
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.collect_insurance = True
        row.insurances = ["Unimed"]
        await session.commit()
        await session.refresh(row)
        tenant = row

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps({"i": typed}),
        tenant,
        None,
        [],
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["supplied"] == ["insurance"]
    assert event["accepted"] == accepted
    assert event["dropped"] == dropped
    assert event["fallback"] is None


@pytest.mark.parametrize("suffix", ["oops", "[]", "null", '{"t": 7}', '{"p": "invalid"}'])
async def test_a_corrupt_draft_is_a_bad_sentinel_with_nothing_trusted(
    db, _captured_bubbles, log, suffix
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + suffix,
        tenant,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["fallback"] == "bad_sentinel"
    assert event["landing_step"] == "menu"
    assert event["supplied"] == []
    assert event["accepted"] == []
    assert event["dropped"] == {}


@pytest.mark.parametrize("reason", ["no_tenant", "without_flows"])
async def test_a_draft_that_cannot_run_is_counted_not_silent(
    db, _captured_bubbles, log, monkeypatch, reason
) -> None:
    if reason == "without_flows":
        # `flows_enabled` is always True today; the guard is defensive and still has to count.
        monkeypatch.setattr(sentinels, "flows_enabled", lambda _tenant: False)
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps({"t": "Consulta Geral"}),
        None if reason == "no_tenant" else tenant,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["fallback"] == reason
    assert event["landing_step"] is None
    assert event["supplied"] == ["service"]
    assert _captured_bubbles == []


async def test_no_hand_back_event_carries_what_the_patient_or_the_model_wrote(
    db, _captured_bubbles, _stub_calendar, log
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)
    typed_service = "Limpeza da Maria Silva"
    typed_plan = "Plano da Maria joao@example.com 11999998888"
    payload = {"t": typed_service, "p": str(ana.id), "i": typed_plan}

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps(payload),
        tenant,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["fallback"] == "invalid_selection"
    assert event["dropped"] == {"service": "not_in_catalog", "insurance": "unmatched_plan"}
    rendered = repr(event)
    for secret in (
        typed_service,
        typed_plan,
        "Silva",
        "joao@example.com",
        "11999998888",
        str(ana.id),
        str(bruno.id),
        str(patient.id),
        patient.wa_id,
        patient.name,
    ):
        assert secret not in rendered, secret


def test_draft_verdicts_names_what_survived_and_why_the_rest_did_not() -> None:
    accepted, dropped = sentinels._draft_verdicts(
        appointment_type="Botox",
        canonical_type=None,
        professional_id=uuid4(),
        professional=SimpleNamespace(),
        insurance_text="Inventado",
        insurance=None,
    )

    assert accepted == ("professional",)
    assert dropped == {"service": "not_in_catalog", "insurance": "unmatched_plan"}


def test_draft_verdicts_ignores_what_the_agent_did_not_supply() -> None:
    # A convênio already stored on the conversation is not something the agent supplied.
    accepted, dropped = sentinels._draft_verdicts(
        appointment_type=None,
        canonical_type=None,
        professional_id=None,
        professional=None,
        insurance_text=None,
        insurance="Unimed",
    )

    assert (accepted, dropped) == ((), {})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_events.py -q`
Expected: os testes novos de `set_booking_draft` FAIL (`ValueError: not enough values to unpack (expected 1, got 0)` e `AttributeError: module ... has no attribute '_draft_verdicts'`); o resto verde.

- [ ] **Step 3: Implement**

Em `src/secretaria/workers/shared/sentinels.py`:

1. Imediatamente depois de `_log_no_landing` (e antes de `async def _handle_select_professional(`), inserir:

```python
def _draft_verdicts(
    *,
    appointment_type: str | None,
    canonical_type: str | None,
    professional_id: UUID | None,
    professional: object | None,
    insurance_text: str | None,
    insurance: str | None,
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Which of the fields the agent supplied survived validation, and why the rest did not.

    Pure and value-free: it returns field NAMES and reason CODES (`handback_log`), never the
    service, doctor or convênio themselves. A field the agent did not supply is in neither
    result - a convênio already stored on the conversation is not "supplied".
    """
    accepted: list[str] = []
    dropped: dict[str, str] = {}
    if appointment_type is not None:
        if canonical_type is not None:
            accepted.append(hb.FIELD_SERVICE)
        else:
            dropped[hb.FIELD_SERVICE] = hb.DROP_NOT_IN_CATALOG
    if professional_id is not None:
        if professional is not None:
            accepted.append(hb.FIELD_PROFESSIONAL)
        else:
            dropped[hb.FIELD_PROFESSIONAL] = hb.DROP_UNKNOWN_PROFESSIONAL
    if insurance_text is not None:
        if insurance is not None:
            accepted.append(hb.FIELD_INSURANCE)
        else:
            dropped[hb.FIELD_INSURANCE] = hb.DROP_UNMATCHED_PLAN
    return tuple(accepted), dropped


```

2. Substituir `_handle_set_booking_draft` inteira (da linha `async def _handle_set_booking_draft(` até o fim do arquivo — é a última função) por:

```python
async def _handle_set_booking_draft(
    reply: _ReplyContext,
    reply_text: str,
    tenant: Tenant | None,
    flow_snapshot: tuple[SimpleNamespace, SimpleNamespace] | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
) -> None:
    """LLM hand-back: resume the booking at the first step still missing.

    The superset of `_handle_start_guided_booking`: the agent may also name the
    doctor (multi-doctor clinics, which `start_guided_booking` turns away) and
    the convênio. Everything is re-read FRESH (same reason as its sibling) and
    the result goes through `_apply_flow_result`, the one persistence seam.

    Every exit records ONE `conversation_handback_entered`
    (workers/shared/handback_log.py): the landing step on success, a `fallback`
    code on every bounce to the menu and on every silent return. Field NAMES and
    reason codes only - never the service, convênio or doctor the agent wrote.
    """
    try:
        payload = json.loads(reply_text[len(BOOKING_DRAFT_SENTINEL_PREFIX) :])
        if not isinstance(payload, dict) or any(
            payload.get(key) is not None and not isinstance(payload[key], str)
            for key in ("t", "p", "i")
        ):
            raise ValueError("Invalid booking draft payload")
        appointment_type = payload.get("t") or None
        professional_id = UUID(payload["p"]) if payload.get("p") else None
        insurance_text = payload.get("i") or None
    except (ValueError, TypeError, KeyError):
        logger.warning(
            "worker_booking_draft_bad_sentinel", conversation_id=str(reply.conversation_id)
        )
        await _fallback_to_menu(
            reply,
            source_tool=hb.SOURCE_SET_BOOKING_DRAFT,
            reason=hb.FALLBACK_BAD_SENTINEL,
            tenant=tenant,
            professionals=professionals,
            patient_wa=patient_wa,
            redis=redis,
            waba_token=waba_token,
        )
        return
    # The field NAMES the agent filled in - the only thing about the draft that is logged.
    supplied = tuple(
        name
        for name, value in (
            (hb.FIELD_SERVICE, appointment_type),
            (hb.FIELD_PROFESSIONAL, professional_id),
            (hb.FIELD_INSURANCE, insurance_text),
        )
        if value is not None
    )
    if tenant is None or not flows_enabled(tenant):
        logger.warning(
            "worker_booking_draft_without_flows", conversation_id=str(reply.conversation_id)
        )
        _log_no_landing(
            reply,
            source_tool=hb.SOURCE_SET_BOOKING_DRAFT,
            reason=hb.FALLBACK_NO_TENANT if tenant is None else hb.FALLBACK_WITHOUT_FLOWS,
            tenant=tenant,
            professionals=professionals,
            supplied=supplied,
        )
        return

    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        selected_id = professional_id or (
            conversation.flow_selected_professional_id if conversation is not None else None
        )
        stored_insurance = (
            conversation.flow_selected_insurance if conversation is not None else None
        )
        attendee = conversation.flow_attendee_name if conversation is not None else None
        stored_type = conversation.flow_selected_type if conversation is not None else None
        professional_rows = await list_active_professionals(session, tenant.id)
        service_catalog = await load_service_catalog(session, tenant.id)
        tenant_insurance = await load_tenant_insurance(session, tenant.id)
        selection_only = appointment_type is None and professional_id is None
        booking_calendar = None
        if not selection_only:
            booking_calendar = await _appointment_calendar(
                session,
                tenant,
                _appointment_calendar_target({"professional_id": selected_id}, professional_rows),
            )

    tenant_snapshot = _flow_tenant_snapshot(
        tenant, professional_rows, service_catalog, tenant_insurance
    )
    # A convênio the patient typed only counts when it names a real plan; anything
    # else is dropped so the flow ASKS instead of storing a made-up label.
    insurance = (
        match_insurance_plan(tenant_snapshot, insurance_text)
        if insurance_text is not None else stored_insurance
    )

    topology = booking_topology(professional_rows)
    is_multi = topology == BOOKING_TOPOLOGY_MULTI
    professional = next((p for p in professional_rows if p.id == selected_id), None)
    fresh_services = (
        professional_appointment_types(professional, tenant_snapshot, service_catalog)
        if professional is not None else tenant_snapshot.appointment_types
    )
    canonical_type = canonical_service_name(fresh_services, appointment_type)
    accepted, dropped = _draft_verdicts(
        appointment_type=appointment_type,
        canonical_type=canonical_type,
        professional_id=professional_id,
        professional=professional,
        insurance_text=insurance_text,
        insurance=insurance,
    )
    if selection_only:
        # An unspecified service means administrative choices, never an inferred
        # procedure or a jump to availability. Revalidate stored selections using
        # this tenant's current roster/catalog rather than the LLM snapshot.
        stored_type = canonical_service_name(fresh_services, stored_type)
        if insurance_text is None and stored_insurance is not None:
            insurance = match_insurance_plan(tenant_snapshot, stored_insurance)
        if professional is not None:
            result = _enter_professional_services(professional, tenant_snapshot)
        elif attendee is not None or stored_type is not None:
            # `attendee is not None` includes ATTENDEE_SELF (""): the patient
            # already answered "Essa consulta é pra você?" earlier in this
            # booking, so the hand-back must NOT ask it again.
            result = _start_booking(tenant_snapshot, professional_rows, insurance=insurance)
        else:
            result = enter_booking(tenant_snapshot, professional_rows)
        if result.flow_state != FlowState.SERVICE_CATALOG:
            await _fallback_to_menu(
                reply,
                source_tool=hb.SOURCE_SET_BOOKING_DRAFT,
                # A doctor with no services ends in the config-incomplete result, whose
                # alert this branch does not send (it renders the menu instead).
                reason=(
                    hb.FALLBACK_PROFESSIONAL_CONFIG_INCOMPLETE
                    if result.action == "professional_config_incomplete"
                    else hb.FALLBACK_NO_BOOKABLE_CATALOG
                ),
                tenant=tenant,
                professionals=professional_rows,
                patient_wa=patient_wa,
                redis=redis,
                waba_token=waba_token,
                supplied=supplied,
                accepted=accepted,
                dropped=dropped,
                topology=topology,
            )
            return
        result.flow_selected_insurance = insurance
        result.flow_attendee_name = attendee
        if result.flow_step not in ATTENDEE_STEPS:
            result.flow_selected_type = stored_type
        await _land_handback(
            reply,
            result,
            patient_wa,
            source_tool=hb.SOURCE_SET_BOOKING_DRAFT,
            tenant=tenant,
            professionals=professional_rows,
            redis=redis,
            waba_token=waba_token,
            supplied=supplied,
            accepted=accepted,
            dropped=dropped,
            topology=topology,
        )
        return
    if (
        (appointment_type is not None and canonical_type is None)
        or (appointment_type is None and (not is_multi or professional_id is None))
        or (selected_id is not None and professional is None)
    ):
        logger.warning(
            "worker_booking_draft_invalid_selection", conversation_id=str(reply.conversation_id)
        )
        await _fallback_to_menu(
            reply,
            source_tool=hb.SOURCE_SET_BOOKING_DRAFT,
            reason=hb.FALLBACK_INVALID_SELECTION,
            tenant=tenant,
            professionals=professional_rows,
            patient_wa=patient_wa,
            redis=redis,
            waba_token=waba_token,
            supplied=supplied,
            accepted=accepted,
            dropped=dropped,
            topology=topology,
        )
        return
    appointment_type = canonical_type
    if is_multi:
        professional = next((p for p in professional_rows if p.id == selected_id), None)
        if professional is None:
            # A service without a doctor on a multi-doctor clinic: no one to book with yet.
            await _fallback_to_menu(
                reply,
                source_tool=hb.SOURCE_SET_BOOKING_DRAFT,
                reason=hb.FALLBACK_MISSING_PROFESSIONAL,
                tenant=tenant,
                professionals=professionals,
                patient_wa=patient_wa,
                redis=redis,
                waba_token=waba_token,
                supplied=supplied,
                accepted=accepted,
                dropped=dropped,
                topology=topology,
            )
            return
        if appointment_type is None:
            result = _enter_professional_services(professional, tenant_snapshot)
            if result.flow_state == FlowState.SERVICE_CATALOG:
                result.flow_selected_insurance = insurance
                result.flow_attendee_name = attendee
        else:
            result = await enter_guided_booking(
                tenant_snapshot,
                booking_calendar,
                appointment_type,
                conversation_id=reply.conversation_id,
                professional_id=selected_id,
                insurance=insurance,
                services=professional_appointment_types(
                    professional, tenant_snapshot, service_catalog
                ),
                professionals=professional_rows,
                attendee_name=attendee,
            )
    else:
        result = await enter_guided_booking(
            tenant_snapshot,
            booking_calendar,
            appointment_type,
            conversation_id=reply.conversation_id,
            professional_id=selected_id,
            insurance=insurance,
            professionals=professional_rows,
            attendee_name=attendee,
        )
    logger.info(
        "conversation_booking_draft_entered",
        conversation_id=str(reply.conversation_id),
        tenant_id=str(tenant.id),
        multi=is_multi,
        has_type=appointment_type is not None,
        flow_step=result.flow_step,
    )
    await _land_handback(
        reply,
        result,
        patient_wa,
        source_tool=hb.SOURCE_SET_BOOKING_DRAFT,
        tenant=tenant,
        professionals=professional_rows,
        redis=redis,
        waba_token=waba_token,
        supplied=supplied,
        accepted=accepted,
        dropped=dropped,
        topology=topology,
    )
```

(O corpo entre `async with async_session_factory()` e o `if selection_only:` é o original; só mudaram as linhas de `topology`, `accepted, dropped = _draft_verdicts(...)` e as saídas.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_events.py tests/test_agent_menu_tools.py tests/test_attendee_booking.py tests/test_set_booking_draft.py -q`
Expected: PASS (todos os testes antigos do rascunho, inclusive os de `selection_only`, continuam verdes: o comportamento ao paciente não mudou).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff check src/secretaria/workers/shared/sentinels.py tests/test_handback_events.py
uvx ruff format tests/test_handback_events.py
uvx ruff format --diff src/secretaria/workers/shared/sentinels.py | grep -c '^@@'   # must not exceed 10
git add src/secretaria/workers/shared/sentinels.py tests/test_handback_events.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(observability): set_booking_draft records every exit, including the selection-only branch

The most common hand-back (an empty draft landing on an administrative step) used to log
nothing about where the patient landed. Every landing and every bounce now has its event;
supplied/accepted/dropped carry field names and reason codes only.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 7: `request_human_handoff`

**Files:**
- Modify: `src/secretaria/workers/shared/handover.py` (`_handle_human_handoff`)
- Modify: `tests/test_handback_events.py`

**Interfaces:**
- Consumes: `hb.log_handback_entered`, `hb.SOURCE_REQUEST_HUMAN_HANDOFF`, `hb.LANDING_HUMAN_HANDOVER`, `hb.FIELD_REASON` (Task 1).
- Produces: `_handle_human_handoff(reply, reason, tenant, flow_snapshot, redis=None, waba_token=None)` (assinatura inalterada) registra o evento DEPOIS de o estado "pessoa assumiu" ser gravado e ANTES de a mensagem sair; se o estado não grava, levanta como hoje e não registra.

- [ ] **Step 1: Write the failing tests**

Em `tests/test_handback_events.py`, acrescentar ao fim:

```python
# --------------------------------------------------------------------------
# request_human_handoff
# --------------------------------------------------------------------------


async def test_human_handoff_is_a_counted_hand_back_to_a_person(
    db, _captured_bubbles, log, monkeypatch
) -> None:
    async def _notify(**_kwargs):
        return 1

    monkeypatch.setattr(workers_ns, "notify_human_handoff", _notify)
    tenant, ana, _bruno, _patient, conversation = await _seed(db, selected=True)

    await tasks._handle_human_handoff(
        _reply_ctx(conversation), "patient_requested_human", tenant, None
    )

    (event,) = _events(log)
    assert event == {
        "conversation_id": str(conversation.id),
        "tenant_id": str(tenant.id),
        "source_tool": "request_human_handoff",
        "landing_step": "human_handover",
        "supplied": ["reason"],
        "accepted": ["reason"],
        "dropped": {},
        "fallback": None,
        "topology": None,
        "channel": "whatsapp",
    }
    assert len(_captured_bubbles) == 1  # the patient's confirmation still goes out


async def test_a_handoff_that_could_not_commit_logs_no_hand_back(
    db, _captured_bubbles, log, monkeypatch
) -> None:
    async def _not_committed(*_args, **_kwargs):
        raise RuntimeError("handoff_state_not_committed")

    monkeypatch.setattr(workers_ns, "_set_conversation_human_active", _not_committed)
    tenant, _ana, _bruno, _patient, conversation = await _seed(db)

    with pytest.raises(RuntimeError, match="handoff_state_not_committed"):
        await tasks._handle_human_handoff(_reply_ctx(conversation), "could_not_help", tenant, None)

    assert _events(log) == []
    assert _captured_bubbles == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_events.py -q`
Expected: `test_human_handoff_is_a_counted_hand_back_to_a_person` FAIL (`ValueError: not enough values to unpack (expected 1, got 0)`); `test_a_handoff_that_could_not_commit_logs_no_hand_back` PASSA já agora (fixa o contrato).

- [ ] **Step 3: Implement**

Em `src/secretaria/workers/shared/handover.py`:

1. Nos imports, logo ANTES de `from secretaria.workers.shared.context import (`, acrescentar:

```python
from secretaria.workers.shared import handback_log as hb
```

2. Trocar o corpo de `_handle_human_handoff`

```python
    is already on it). The mail is best-effort: `notify_human_handoff` never
    raises and logs its own alarm.
    """
    await _set_conversation_human_active(reply.conversation_id, reason=reason)
    await _dispatch_bubbles(
        reply, [TextBubble(body=SCOPED_HELP_ESCALATE_MESSAGE)], tenant=tenant, waba_token=waba_token
    )
```

por

```python
    is already on it). The mail is best-effort: `notify_human_handoff` never
    raises and logs its own alarm.

    The hand-back event is recorded after the human state is committed and before the
    patient's confirmation goes out: a handoff that could not commit raised above and
    landed nowhere, so it is not counted as a landing.
    """
    await _set_conversation_human_active(reply.conversation_id, reason=reason)
    hb.log_handback_entered(
        conversation_id=reply.conversation_id,
        tenant_id=tenant.id if tenant is not None else reply.tenant_id,
        source_tool=hb.SOURCE_REQUEST_HUMAN_HANDOFF,
        landing_step=hb.LANDING_HUMAN_HANDOVER,
        supplied=(hb.FIELD_REASON,),
        accepted=(hb.FIELD_REASON,),
        channel=reply.channel,
    )
    await _dispatch_bubbles(
        reply, [TextBubble(body=SCOPED_HELP_ESCALATE_MESSAGE)], tenant=tenant, waba_token=waba_token
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_events.py tests/test_agent_menu_tools.py -q`
Expected: PASS (inclusive `test_handle_human_handoff_*` e `test_human_handoff_commit_failure_does_not_confirm_success` antigos).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff check src/secretaria/workers/shared/handover.py tests/test_handback_events.py
uvx ruff format tests/test_handback_events.py
uvx ruff format --diff src/secretaria/workers/shared/handover.py | grep -c '^@@'   # must not exceed 4
git add src/secretaria/workers/shared/handover.py tests/test_handback_events.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(observability): request_human_handoff records the hand-back to a person

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 8: `tools_called` no `llm_turn_trace`

**Files:**
- Modify: `src/secretaria/ai/trace.py:88-102`
- Modify: `tests/test_llm_turn_trace.py`

**Interfaces:**
- Consumes: `summarize_turn` (já existente) e o `**summary` que `ai/graph.py::_log_agent_trace` espalha no `llm_turn_trace`.
- Produces: `summarize_turn(...)["tools_called"]: list[str]` — nomes das ferramentas, na ordem em que o modelo as pediu (repetições mantidas), sem argumentos; `[]` sem ferramentas. `llm_turn_trace` passa a carregar o campo sem mudança em `ai/graph.py`.

- [ ] **Step 1: Write the failing tests**

Em `tests/test_llm_turn_trace.py`, ao fim do arquivo:

```python
class _LogRecorder:
    """Records every structlog call (`caplog` is empty for structlog in this suite)."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        def _log(event: str, **fields) -> None:
            self.records.append((level, event, fields))

        return _log


def test_tools_called_lists_names_in_call_order_without_arguments() -> None:
    msgs = [
        _ai(tool_calls=[{"name": "list_free_slots", "args": {"date": "2026-10-03"}, "id": "c1"}]),
        ToolMessage(content="[...]", name="list_free_slots", tool_call_id="c1"),
        _ai(
            tool_calls=[
                {"name": "set_booking_draft", "args": {"service": "Limpeza da Maria"}, "id": "c2"},
                {"name": "list_free_slots", "args": {}, "id": "c3"},
            ]
        ),
        ToolMessage(content="ok", name="set_booking_draft", tool_call_id="c2"),
        ToolMessage(content="[...]", name="list_free_slots", tool_call_id="c3"),
        _ai(content="Pronto."),
    ]

    summary = summarize_turn(msgs)

    assert summary["tools_called"] == ["list_free_slots", "set_booking_draft", "list_free_slots"]
    assert "2026-10-03" not in str(summary["tools_called"])
    assert "Maria" not in str(summary["tools_called"])


def test_a_turn_without_tools_lists_none() -> None:
    assert summarize_turn([_ai(content="Olá!")])["tools_called"] == []


def test_the_llm_turn_trace_event_carries_tools_called(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _LogRecorder()
    monkeypatch.setattr(graph, "logger", recorder)
    msgs = [
        _ai(tool_calls=[{"name": "show_main_menu", "args": {}, "id": "c1"}]),
        ToolMessage(content="ok", name="show_main_menu", tool_call_id="c1"),
        _ai(content="Aqui está o menu."),
    ]

    graph._log_agent_trace(msgs, elapsed_ms=12)

    (fields,) = [f for _level, event, f in recorder.records if event == "llm_turn_trace"]
    assert fields["tools_called"] == ["show_main_menu"]
    assert fields["tool_calls"] == [{"name": "show_main_menu", "arg_keys": []}]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_llm_turn_trace.py -q`
Expected: os 3 testes novos FAIL com `KeyError: 'tools_called'`.

- [ ] **Step 3: Implement**

Em `src/secretaria/ai/trace.py`, no dicionário devolvido por `summarize_turn`, trocar

```python
        "tool_call_count": len(tool_calls),
        "tool_calls": tool_calls,
```

por

```python
        "tool_call_count": len(tool_calls),
        "tool_calls": tool_calls,
        # Names only, in the order the model asked for them: the flat list a log search can
        # filter on ("every turn that called set_booking_draft") without unpacking tool_calls.
        "tools_called": [str(call["name"]) for call in tool_calls if call.get("name")],
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_llm_turn_trace.py tests/test_agent_menu_tools.py -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff check src/secretaria/ai/trace.py tests/test_llm_turn_trace.py
uvx ruff format src/secretaria/ai/trace.py tests/test_llm_turn_trace.py
git add src/secretaria/ai/trace.py tests/test_llm_turn_trace.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(observability): llm_turn_trace lists the tools the model called

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 9: Documentação e validação final

**Files:**
- Modify: `docs/CHECKPOINT_llm_observabilidade_rede_de_seguranca.md`

**Interfaces:**
- Consumes: tudo acima (só depois de validado: regra de docs do repo).
- Produces: o evento, o vocabulário e a lacuna documentados onde os planos P2–P5 e quem investiga logs vão procurar.

- [ ] **Step 1: Atualizar o CHECKPOINT de observabilidade**

Em `docs/CHECKPOINT_llm_observabilidade_rede_de_seguranca.md`:

1. Na tabela de eventos, trocar a linha do `llm_turn_trace` por (acrescenta `tools_called`):

```markdown
| `llm_turn_trace` | depois de cada run do agente que termina em texto | `verdict`, `model_calls`, `tool_calls[{name,arg_keys}]`, `tools_called` (só nomes, em ordem), `tool_errors`, `input_tokens`, `output_tokens`, `reasoning_tokens`, `finish_reasons`, `truncated`, `elapsed_ms` |
```

e acrescentar, logo depois da linha de `ai_run_agent_turn_timeout`:

```markdown
| `conversation_handback_entered` | todo hand-back da IA ao fluxo guiado (pouso OU fallback), no worker, antes de gravar o estado | `source_tool`, `landing_step`, `supplied`, `accepted`, `dropped`, `fallback`, `topology`, `channel` — onde o paciente caiu |
```

2. Antes da seção `## Como investigar um caso (roteiro)`, inserir:

```markdown
### 4. Hand-backs da IA (TASK-030, plano P1) — `workers/shared/handback_log.py`

Um evento `conversation_handback_entered` por hand-back, com **só ids, nomes de campo e códigos** (nunca o
serviço, o convênio, o nome ou o horário que a IA escreveu; valor fora do vocabulário vira `"other"`). Emitido
por `log_handback_entered(...)`, chamado de `sentinels.py` (menu, médico, gerenciar, guiado, rascunho) e de
`handover.py` (pessoa). Os eventos antigos (`conversation_booking_draft_entered`,
`conversation_guided_booking_entered`, `conversation_menu_rendered`, `ai_run_agent_*`) continuam.

- `source_tool`: `set_booking_draft`, `show_main_menu`, `manage_existing_appointment`, `select_professional`,
  `start_guided_booking`, `request_human_handoff`. No menu só conta a chamada da ferramenta
  (`source="agent_tool"`); `/menu`, passo do nome e cartões de identidade não são hand-back.
- `landing_step`: o `flow_step` do resultado, ou o estado em minúsculas (`menu`), ou `human_handover`
  (agenda fora do ar / transferência), `config_incomplete` (médico sem serviços/horários); `null` = nada pousou.
- `fallback` (`null` = pousou onde a ferramenta pediu): `bad_sentinel`, `no_tenant`, `without_flows`,
  `no_patient`, `invalid_selection`, `missing_professional`, `unknown_professional`, `no_bookable_catalog`,
  `multi_professional`, `no_appointments`, `calendar_unavailable`, `professional_config_incomplete`.
- `dropped` (campo → motivo): `not_in_catalog`, `unknown_professional`, `unmatched_plan`.

Perguntas que o evento responde: em que etapa caem os pacientes (`landing_step`), quantos voltam ao menu e por
quê (`fallback`), qual item do rascunho a IA erra mais (`dropped`).

Limites conhecidos: (a) turno que termina em hand-back levanta a exceção da ferramenta e nunca emite
`llm_turn_trace` — `tools_called` só existe em turno que termina em texto; nos de hand-back, `source_tool` é a
ferramenta final; (b) falha de infraestrutura ANTES de o pouso ser conhecido (leitura no banco) propaga como
sempre e não gera evento — a diferença entre as contagens de `ai_run_agent_*` e de
`conversation_handback_entered` a revela; (c) `without_flows` é defensivo (`flows_enabled` é sempre verdadeiro).
```

- [ ] **Step 2: Rodar os testes tocados e vizinhos**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_log.py tests/test_handback_events.py tests/test_llm_turn_trace.py tests/test_agent_menu_tools.py tests/test_attendee_booking.py tests/test_set_booking_draft.py tests/test_menu_command.py tests/test_workers_layering.py tests/test_workers_ns_patching.py -q`
Expected: PASS.

- [ ] **Step 3: Suíte completa**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest -q`
Expected: sem falhas novas (o CHECKPOINT de 2026-10-01 registra 2778 passed, 10 skipped como linha de base, mais os testes novos deste plano). Se algo falhar, separar falha pré-existente de regressão antes de reportar: rodar o mesmo arquivo em `main` (`git stash` NÃO — use um segundo worktree ou `git worktree add` em `b0ac5ee`). `secretaria-human-backup-utc-flake` falha entre 00h e 03h UTC por motivo antigo e não relacionado.

- [ ] **Step 4: Conferir que nada além do planejado mudou**

Run: `git diff --stat b0ac5ee..HEAD -- . ':!docs/superpowers'`
Expected: só `src/secretaria/workers/shared/{handback_log,sentinels,handover}.py`, `src/secretaria/ai/trace.py`, os quatro arquivos de teste e o CHECKPOINT. `src/secretaria/ai/graph.py` NÃO aparece (o `**summary` já leva `tools_called`).

- [ ] **Step 5: Commit**

```bash
git add docs/CHECKPOINT_llm_observabilidade_rede_de_seguranca.md
git commit -F - <<'EOF'
docs(observability): record the hand-back event, its vocabulary and its known gaps

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Cobertura da spec

| Spec | Onde |
|---|---|
| §4.9 evento único em todo hand-back (`source_tool`, `landing_step`, `supplied`, `accepted`, `dropped`, `fallback`, `topology`, `channel`), sem valores | Tasks 1–7 |
| §4.9 eventos atuais continuam | Tasks 2, 5, 6 (afirmam `conversation_menu_rendered` / `..._guided_booking_entered` / `..._booking_draft_entered`) |
| §4.9 `llm_turn_trace` ganha `tools_called` | Task 8 |
| §3 / §7: ramo `selection_only`, médico e gerenciar não registram a etapa | Tasks 3, 4, 6 |
| Critério 7: 100% dos hand-backs geram o evento | uma saída = um evento: testes de cada saída nas Tasks 2–7 |
| Spec §7: `selection_only` perde o alerta `professional_config_incomplete` | só registrado (`fallback=professional_config_incomplete`, `landing_step=menu`); a correção é do P2 |

## Deploy e liberação

Só muda log: nenhum comportamento visto pelo paciente, sem migração e sem flag. Mesmo assim API e worker sobem **juntos** (README: "Deploy both services, or neither"; a paridade em `GET /build` deve voltar `match`). Deploy nunca é parte deste plano — só com pedido explícito do dono.
