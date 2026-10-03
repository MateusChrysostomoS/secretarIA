# IA entra em qualquer etapa — P2b: hand-backs pelo resolvedor, "pra quem" sempre perguntado, ferramenta v2, interruptor e estado da conversa — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Todo hand-back da IA que agenda (`set_booking_draft`, `start_guided_booking`, `select_professional_and_continue`) pergunta "Essa consulta é pra você?" quando ninguém respondeu — em toda clínica; nas clínicas com o interruptor ligado, o rascunho v2 (com pra quem, dia e horário) é pousado pelo resolvedor do P2a; e a IA passa a enxergar um "ESTADO DA CONVERSA" sem marcadores internos e com as etapas nomeadas.

**Architecture:** Os três handlers de `workers/shared/sentinels.py` passam a reler tudo fresco (`draft_resolution._load_draft_context`, P2a) e a decidir em uma linha: interruptor ligado **ou** "pra quem" desconhecido → resolvedor (`_apply_draft_resolution`, que registra UM `conversation_handback_entered` pelos helpers do P1); senão → o pouso v1 de hoje, com as correções do §7. A ferramenta v2 (`set_booking_draft_v2`, mesmo nome para o modelo) só é entregue às clínicas com o interruptor ligado (`initial_flows.ai_draft_v2`); o cache de agentes compilados passa a distinguir as duas versões. O bloco de estado ganha rótulos para as etapas de pra quem/gerenciar/recusa/legadas, a resposta do pra quem, o horário gravado e a topologia.

**Tech Stack:** Python 3.12, LangChain tools, structlog, pytest + pytest-asyncio (SQLite em memória), ruff.

**Spec:** `docs/superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md` — §4.1 (ferramenta), §4.7 (estado da conversa), §4.11 (interruptor por tenant + correção de segurança fora dele), §5 critérios 1 e 4, §7 (linhas P2: rascunho pula "pra quem"; snapshot velho em `_handle_select_professional`; `_handle_start_guided_booking` não re-canoniza; `_is_agent_sentinel`; `selection_only` perde o alerta e o convênio digitado; defeitos do estado).

**Depende de:** P1 e **P2a** (`2026-10-02-ia-p2a-rascunho-coluna-e-resolvedor.md`) executados neste worktree. As interfaces consumidas daqui estão listadas na seção "Interfaces" do P2a.

## Global Constraints

- Mesmas regras do P2a (worktree, Git Bash com `BOT_ALLOWLIST_WA_IDS=""`, `uv run python -m pytest`, CRLF, camadas, commits com `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`, nada de push/deploy). Repetidas aqui as que mais pegam:
- `uvx ruff format` só nos arquivos **criados** por este plano, no `services/llm_context.py` (reescrito inteiro na B4) e nos dois que o P1 criou e deixou limpos (`tests/test_handback_events.py`, `workers/shared/handback_log.py`) — sempre ANTES do `uvx ruff check`. Nos demais arquivos existentes, a contagem de `uvx ruff format --diff <arquivo> | grep -c '^@@'` não pode aumentar (o P1 mediu: `sentinels.py` 10, `handover.py` 4, `graph.py` 2).
- Um hand-back = um `conversation_handback_entered`, registrado ANTES de `_apply_flow_result` (P1, decisão 1). Os eventos antigos continuam: `conversation_booking_draft_entered`, `conversation_guided_booking_entered`, `conversation_menu_rendered`.
- A IA nunca carrega nome de terceiro: `for_whom ∈ {"me", "other", ""}`; mensagem de erro de ferramenta nunca ecoa o valor recebido; log nunca carrega valor.
- **Interruptor por clínica** `flow_router.ai_draft_v2_enabled(tenant)`: desligado por padrão. Atrás dele: a ferramenta v2 e o pouso de TODO hand-back pelo resolvedor. **Fora dele** (vale para todas as clínicas ao deployar): "pra quem" desconhecido → o fluxo pergunta, em todos os hand-backs que agendam; e as correções do §7.
- Texto ao paciente em português; código e comentários em inglês.

## Review Focus

- **"Pra quem" nunca é presumido** (critério 1): rascunho v1 com serviço, `start_guided_booking` e `select_professional_and_continue` com `flow_attendee_name = None` pousam na pergunta e guardam o resto em `flow_draft` (B1 `test_unknown_pra_quem_is_asked_first_even_with_the_switch_off`; B2 `test_select_professional_asks_pra_quem_first_when_unknown`, `test_guided_booking_asks_pra_quem_first_when_unknown`).
- **Snapshot velho**: o "pra quem" lido no início do turno não vence o do banco, nos dois sentidos (B2 `test_select_professional_reads_the_attendee_fresh`).
- **Dois tenants**: médico de outra clínica no rascunho é descartado (`unknown_professional`), nunca usado (B1 `test_another_tenants_doctor_is_dropped_never_used`).
- **Um evento por hand-back**, nos dois caminhos (legado e resolvedor) — todo teste novo desempacota `(event,) = _events(log)`.
- **"for_whom" com nome** → erro recuperável, sem eco do nome (B3 `test_v2_for_whom_is_never_a_name`).
- **Duas ferramentas com o mesmo nome** nunca compartilham o agente compilado (B3 `test_two_draft_tools_never_share_a_compiled_agent`).
- **Marcador interno `__attendee_next_*__` nunca chega ao texto do modelo** (B4 `test_no_internal_marker_ever_reaches_the_model_text`).

## Decisões deste plano (mudar só se o dono discordar)

1. **Onde mora o interruptor: `Tenant.initial_flows["ai_draft_v2"]`** (JSON já existente, lido ao lado de `flows_enabled` — o mesmo dicionário de `menu_label`, `buttons` e `reactivation`). Sem migração; liga só com o literal JSON `true` (string `"true"` não liga). Nenhuma tela do hub grava `initial_flows` hoje (conferido em `secretarIA-frontend` e `Brain-Message-Frontend`: o campo só aparece nos tipos); se um dia uma tela regravar o dicionário sem a chave, o efeito é DESLIGAR — a direção segura.
2. **Ferramenta v2 valida só FORMATO** (`for_whom`, `day`, `time`): formato errado volta como erro recuperável para o modelo corrigir no mesmo turno. Item que não existe na clínica (serviço, convênio, médico) NÃO é erro: segue para o resolvedor, que descarta só ele e pergunta daquela etapa. Nome de médico desconhecido ou ambíguo vira `professional=None` com o evento de contagem `booking_draft_tool_item_dropped` (o hand-back não vê o que não chegou ao sentinel).
3. **Convênio digitado guardado é mantido** (decisão do §7 "decidir e testar"): no `selection_only`, `flow_selected_insurance` gravado pelo fluxo de botões (inclusive "Outro convênio" digitado) não é mais re-validado contra o catálogo. Consequência: `tests/test_agent_menu_tools.py::test_empty_booking_draft_preserves_only_fresh_valid_draft[Removed]` passa a esperar `"Removed"` — um plano removido do catálogo no meio do agendamento é raro; apagar a resposta digitada do paciente era o caso comum.
4. **Resolvedor com o interruptor desligado** só é usado quando o "pra quem" é desconhecido; a continuação depois da resposta (P2a, Task A6) usa o resolvedor em toda clínica — é parte da correção de segurança.
5. **`select_professional_and_continue` e `start_guided_booking` continuam existindo** (P5 as remove depois que o prompt deixar de citá-las); com o interruptor ligado, também pousam pelo resolvedor.

## File Structure

- Modify `src/secretaria/services/flow_router.py` — `ai_draft_v2_enabled` ao lado de `flows_enabled` (B1).
- Modify `src/secretaria/workers/shared/handback_log.py` — códigos novos do resolvedor (B1).
- Modify `src/secretaria/workers/shared/sentinels.py` — `_handle_set_booking_draft` (B1), `_handle_select_professional`, `_handle_start_guided_booking` (B2); helpers `_pra_quem_unknown`, `_apply_draft_resolution`, `_legacy_booking_draft`.
- Modify `src/secretaria/workers/shared/booking_hold.py` — `_is_agent_sentinel` (B2).
- Modify `src/secretaria/ai/tools.py` — `BookingDraftRequested` v2, `set_booking_draft_v2` (B3).
- Modify `src/secretaria/ai/graph.py` — sentinel v2; chave de cache com variante (B3).
- Modify `src/secretaria/workers/shared/llm_context.py` — `_flow_handback_tools` escolhe a versão pelo interruptor (B3).
- Modify `src/secretaria/services/llm_context.py` — bloco de estado (B4).
- Tests: Modify `tests/test_handback_events.py`, `tests/test_agent_menu_tools.py`, `tests/test_attendee_booking.py`, `tests/test_agent_capability_cache.py`, `tests/test_llm_context.py`; Create `tests/test_set_booking_draft_v2.py`.
- Docs (B5): Create `docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md`; uma linha de ponteiro em `docs/CHECKPOINT_agendar_para_terceiro.md`, `docs/CHECKPOINT_mvp_portal.md` e `docs/CHECKPOINT_llm_observabilidade_rede_de_seguranca.md`.

---

### Task B1: `set_booking_draft` pelo resolvedor — "pra quem" sempre perguntado, o interruptor e as correções do `selection_only`

**Files:**
- Modify: `src/secretaria/services/flow_router.py` (`ai_draft_v2_enabled` logo depois de `flows_enabled`)
- Modify: `src/secretaria/workers/shared/handback_log.py` (`DROP_*`, `DROP_REASONS`, `FALLBACK_NO_FREE_DAYS`, `FALLBACK_REASONS`, comentário dos `FIELD_*`)
- Modify: `src/secretaria/workers/shared/sentinels.py` (imports; `_pra_quem_unknown`, `_apply_draft_resolution`, `_legacy_booking_draft`; `_handle_set_booking_draft` inteira)
- Modify: `tests/test_handback_events.py`, `tests/test_agent_menu_tools.py`

**Interfaces:**
- Consumes: P1 (`hb.*`, `_fallback_to_menu`, `_land_handback`, `_log_no_landing`, `_draft_verdicts`); P2a (`BookingDraft`, `bd.DROP_REASONS`, `bd.FALLBACK_REASONS`, `bd.FIELD_NAMES`, `bd.landing_step`, `DraftContext`, `_load_draft_context`, `_resolve_draft`).
- Produces:
  - `flow_router.AI_DRAFT_V2_FLAG = "ai_draft_v2"`; `def ai_draft_v2_enabled(tenant: Any) -> bool` (P3/P4/P5 usam para ligar a confirmação expressa, as ferramentas novas e o prompt novo).
  - `sentinels._pra_quem_unknown(conversation: SimpleNamespace, draft: BookingDraft) -> bool`.
  - `async def sentinels._apply_draft_resolution(reply: _ReplyContext, tenant: Tenant, draft: BookingDraft, ctx: DraftContext, *, source_tool: str, patient_wa: str | None, redis=None, waba_token: str | None = None) -> None` — resolve, registra UM evento, persiste. B2 e o P3 (gerenciar) reaproveitam.
  - `async def sentinels._legacy_booking_draft(reply, tenant, draft, ctx, *, patient_wa, professionals, redis=None, waba_token=None) -> None`.
  - `handback_log`: `DROP_NOT_OFFERED_BY_PROFESSIONAL`, `DROP_OUT_OF_WINDOW`, `DROP_DAY_UNAVAILABLE`, `DROP_NO_FREE_SLOT`, `DROP_MISSING_DAY`, `FALLBACK_NO_FREE_DAYS`.

- [ ] **Step 1: Write the failing tests**

1. `tests/test_handback_events.py` — imports: acrescentar, na ordem do ruff, `from secretaria.services import booking_draft as bd, booking_hold as booking_hold_service  # noqa: E402` logo depois do import de `secretaria.models` e, logo depois dele, `from secretaria.services.attendee import ATTENDEE_QUESTION_BODY, ATTENDEE_SELF  # noqa: E402`; trocar `from secretaria.services.flow_router import MenuBubble  # noqa: E402` por

```python
from secretaria.services.flow_router import (  # noqa: E402
    ATTENDEE_NEXT_BOOK,
    FlowRouterResult,
    MenuBubble,
    ai_draft_v2_enabled,
)
```

2. No mesmo arquivo, `_seed` ganha o "pra quem": trocar `async def _seed(db, *, flow_state=FlowState.LLM, selected=None, insurance=None):` por `async def _seed(db, *, flow_state=FlowState.LLM, selected=None, insurance=None, attendee=None):` e, no `Conversation(...)` dele, acrescentar `flow_attendee_name=attendee,` depois de `flow_selected_insurance=insurance,`.

3. Testes do P1 que descrevem o pouso v1 (pra quem já respondido) passam a semear `attendee=ATTENDEE_SELF` — sem isso eles agora pousam (corretamente) na pergunta:
   - `test_a_draft_that_cannot_land_bounces_to_the_menu_with_its_reason`: `await _seed(db)` → `await _seed(db, attendee=ATTENDEE_SELF)` e `await _seed_sole(db)` → `await _seed_sole(db, attendee=ATTENDEE_SELF)`;
   - `test_a_draft_that_lands_logs_the_step_and_keeps_the_old_event`: `await _seed(db)` → `await _seed(db, attendee=ATTENDEE_SELF)`;
   - `test_no_hand_back_event_carries_what_the_patient_or_the_model_wrote`: idem.

4. Ao fim do arquivo, acrescentar:

```python
# --------------------------------------------------------------------------
# TASK-030 P2: set_booking_draft through the resolver (safety fix + switch)
# --------------------------------------------------------------------------


class _SlotAgenda(_StubCalendar):
    """The stub agenda with two free times on every day."""

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        iso = day.date().isoformat()
        return [{"start": f"{iso}T{t}", "end": "", "label": t} for t in ("10:00", "10:40")][
            :max_slots
        ]


@pytest.fixture
def _slot_agenda(monkeypatch: pytest.MonkeyPatch, db) -> _SlotAgenda:
    agenda = _SlotAgenda()

    async def _fake(session, tenant, target):
        return agenda

    monkeypatch.setattr(workers_ns, "_appointment_calendar", _fake)
    # The gate's hold lookups read the same test database.
    monkeypatch.setattr(booking_hold_service, "async_session_factory", db)
    return agenda


async def _switch_on(db, tenant) -> Tenant:
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.initial_flows = {**(row.initial_flows or {}), "ai_draft_v2": True}
        await session.commit()
        await session.refresh(row)
        return row


async def test_unknown_pra_quem_is_asked_first_even_with_the_switch_off(
    db, _captured_bubbles, log
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)  # pra-quem never answered

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps({"t": "Consulta Geral", "p": str(ana.id)}),
        tenant,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["landing_step"] == "awaiting_attendee_choice"
    assert event["supplied"] == ["service", "professional"]
    assert event["accepted"] == ["service", "professional"]
    assert event["fallback"] is None
    assert _captured_bubbles[0].body == ATTENDEE_QUESTION_BODY
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_step == "awaiting_attendee_choice"
    assert conv.flow_selected_type == ATTENDEE_NEXT_BOOK
    assert conv.flow_selected_professional_id is None
    assert (conv.flow_draft["t"], conv.flow_draft["p"]) == ("Consulta Geral", str(ana.id))


async def test_switch_on_lands_a_full_draft_on_the_days_slot_list(
    db, _captured_bubbles, _slot_agenda, log
) -> None:
    tenant, ana, patient, conversation = await _seed_sole(db)
    tenant = await _switch_on(db, tenant)
    day = (datetime.now(ZoneInfo("America/Sao_Paulo")) + timedelta(days=3)).date()
    payload = {"t": "Consulta Geral", "w": "self", "d": day.isoformat(), "h": "10:00"}

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps(payload),
        tenant,
        None,
        _snapshots([ana]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["landing_step"] == "awaiting_slot"
    assert event["accepted"] == ["service", "for_whom", "day", "time"]
    assert event["topology"] == "sole"
    assert len(_events(log, "conversation_booking_draft_entered")) == 1
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_step == "awaiting_slot"
    assert conv.flow_selected_day == day.isoformat()
    assert conv.flow_attendee_name == ATTENDEE_SELF


async def test_selection_only_reaches_the_clinic_alert_for_a_doctor_with_no_services(
    db, _captured_bubbles, log, monkeypatch
) -> None:
    alerted: list = []

    async def _fake_alert(reply, result, **_kwargs):
        alerted.append(result.professional_config_gap)

    monkeypatch.setattr(workers_ns, "_handle_professional_config_incomplete", _fake_alert)
    tenant, ana, bruno, patient, conversation = await _seed(
        db, selected=True, attendee=ATTENDEE_SELF
    )
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

    assert alerted == ["services"]  # it used to be swallowed by the menu fallback
    (event,) = _events(log)
    assert event["landing_step"] == "config_incomplete"
    assert event["fallback"] == "professional_config_incomplete"


async def test_selection_only_keeps_a_typed_convenio(db, _captured_bubbles, log) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(
        db, insurance="Amil Dental", attendee=ATTENDEE_SELF
    )
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.collect_insurance = True
        row.insurance_mode = "shared"
        row.insurances = ["Unimed"]
        await session.commit()
        await session.refresh(row)
        tenant = row

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + "{}",
        tenant,
        None,
        [],
        patient.wa_id,
    )

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_step == "awaiting_professional"
    assert conv.flow_selected_insurance == "Amil Dental"  # the patient's typed answer


async def test_another_tenants_doctor_is_dropped_never_used(db, _captured_bubbles, log) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db, attendee=ATTENDEE_SELF)
    tenant = await _switch_on(db, tenant)
    _other_tenant, foreign_doctor, _b, _p, _c = await _seed(db)

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX
        + json.dumps({"t": "Consulta Geral", "p": str(foreign_doctor.id)}),
        tenant,
        None,
        [],
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["dropped"] == {"professional": "unknown_professional"}
    assert event["landing_step"] == "awaiting_professional"
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_selected_professional_id is None


def test_the_resolver_speaks_the_hand_back_vocabulary() -> None:
    assert set(bd.DROP_REASONS) <= handback_log.DROP_REASONS
    assert set(bd.FALLBACK_REASONS) <= handback_log.FALLBACK_REASONS
    assert set(bd.FIELD_NAMES) <= handback_log.FIELD_NAMES


@pytest.mark.parametrize(
    "result",
    [
        FlowRouterResult(
            action="reply", flow_state=FlowState.SERVICE_CATALOG, flow_step="awaiting_slot"
        ),
        FlowRouterResult(action="reply", flow_state=FlowState.MENU),
        FlowRouterResult(
            action="calendar_unavailable",
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step="awaiting_day",
        ),
        FlowRouterResult(action="professional_config_incomplete", flow_state=FlowState.IDLE),
    ],
)
def test_the_resolver_names_landings_like_the_hand_back_log(result) -> None:
    assert bd.landing_step(result) == handback_log.landing_of(result)[0]


@pytest.mark.parametrize(
    "flows, expected",
    [
        ({}, False),
        (None, False),
        ({"ai_draft_v2": False}, False),
        ({"ai_draft_v2": "true"}, False),
        ({"ai_draft_v2": True}, True),
    ],
)
def test_the_switch_reads_only_an_explicit_true(flows, expected) -> None:
    assert ai_draft_v2_enabled(SimpleNamespace(initial_flows=flows)) is expected
```

(se `ZoneInfo`, `timedelta` ou `datetime` ainda não estiverem importados nesse arquivo, acrescente-os ao bloco de imports existente — `from datetime import UTC, datetime, timedelta` e `from zoneinfo import ZoneInfo` já são importados pelo P1.)

5. `tests/test_agent_menu_tools.py`:
   - imports: acrescentar `from secretaria.services.attendee import ATTENDEE_SELF  # noqa: E402` logo depois de `from secretaria.schemas.webhook import inbound_routing_text  # noqa: E402`;
   - `_seed`: trocar a assinatura por `async def _seed(db, *, flow_state=FlowState.LLM, selected=None, insurance=None, attendee=None):` e acrescentar `flow_attendee_name=attendee,` ao `Conversation(...)`, depois de `flow_selected_insurance=insurance,`;
   - `test_booking_draft_multi_resumes`: `tenant, ana, bruno, patient, conversation = await _seed(db)` → `... = await _seed(db, attendee=ATTENDEE_SELF)`;
   - `test_booking_draft_validates_insurance_and_uses_fresh_doctor`: `await _seed(db, insurance="Unimed")` → `await _seed(db, insurance="Unimed", attendee=ATTENDEE_SELF)`;
   - `test_booking_draft_rejects_removed_or_missing_service`: `await _seed(db)` → `await _seed(db, attendee=ATTENDEE_SELF)` e `await _seed_sole(db)` → `await _seed_sole(db, attendee=ATTENDEE_SELF)`;
   - `test_empty_booking_draft_preserves_only_fresh_valid_draft`: trocar o decorador `@pytest.mark.parametrize("insurance, expected", [("Unimed", "Unimed"), ("Removed", None)])` por

```python
# TASK-030 P2: a STORED convênio is the patient's own answer (a typed "Outro convênio"
# included) and is kept verbatim; only a convênio the AGENT supplies must name a plan.
@pytest.mark.parametrize("insurance, expected", [("Unimed", "Unimed"), ("Removed", "Removed")])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_events.py tests/test_agent_menu_tools.py -q`
Expected: erro de coleta em `test_handback_events.py` — `ImportError: cannot import name 'ai_draft_v2_enabled' from 'secretaria.services.flow_router'`. (Depois do Step 3.1, os novos falham por pousarem no caminho v1/menu, e `test_empty_booking_draft_preserves_only_fresh_valid_draft[Removed-Removed]` falha com `None != 'Removed'`.)

- [ ] **Step 3: Implement**

1. `src/secretaria/services/flow_router.py`, logo depois da função `flows_enabled` (antes de `def menu_buttons`), inserir:

```python
# Per-clinic switch for the AI draft v2 (TASK-030): the six-field `set_booking_draft`, every
# hand-back landed by the resolver (services/booking_draft.py) and, from P3-P5 on, the
# express confirmation, the availability tool and the new prompt. Lives in the same
# `initial_flows` JSON as `menu_label`/`buttons`/`reactivation`. NOT part of the switch:
# asking pra-quem when it is unknown - that safety fix applies to every clinic.
AI_DRAFT_V2_FLAG = "ai_draft_v2"


def ai_draft_v2_enabled(tenant: Any) -> bool:
    """True only when the clinic's `initial_flows` holds the JSON literal `true`.

    OFF by default and by every malformed value (a string "true" included): the owner turns
    it on for one clinic by a data change and watches the logs before anyone else. A hub
    save that ever rewrites `initial_flows` without the key turns it OFF - the safe side.
    """
    flows = getattr(tenant, "initial_flows", None) or {}
    return isinstance(flows, dict) and flows.get(AI_DRAFT_V2_FLAG) is True
```

2. `src/secretaria/workers/shared/handback_log.py`:
   - trocar `DROP_REASONS = frozenset({DROP_NOT_IN_CATALOG, DROP_UNKNOWN_PROFESSIONAL, DROP_UNMATCHED_PLAN})` por

```python
# TASK-030 P2: the resolver's own reasons (services/booking_draft.py DROP_*, same strings).
DROP_NOT_OFFERED_BY_PROFESSIONAL = "not_offered_by_professional"
DROP_OUT_OF_WINDOW = "out_of_window"
DROP_DAY_UNAVAILABLE = "day_unavailable"
DROP_NO_FREE_SLOT = "no_free_slot"
DROP_MISSING_DAY = "missing_day"
DROP_REASONS = frozenset(
    {
        DROP_NOT_IN_CATALOG,
        DROP_UNKNOWN_PROFESSIONAL,
        DROP_UNMATCHED_PLAN,
        DROP_NOT_OFFERED_BY_PROFESSIONAL,
        DROP_OUT_OF_WINDOW,
        DROP_DAY_UNAVAILABLE,
        DROP_NO_FREE_SLOT,
        DROP_MISSING_DAY,
    }
)
```

   - logo depois de `FALLBACK_PROFESSIONAL_CONFIG_INCOMPLETE = "professional_config_incomplete"`, inserir `FALLBACK_NO_FREE_DAYS = "no_free_days"` e acrescentar `FALLBACK_NO_FREE_DAYS,` dentro de `FALLBACK_REASONS = frozenset({...})`;
   - trocar o comentário `# `for_whom`, `day`, `time` and `appointment` are reserved for the draft v2 and the manage` + `# v2 (TASK-030 P2/P3); the tools do not carry them yet.` por `# `for_whom`, `day` and `time` are carried by the draft v2 (TASK-030 P2); `appointment` is` + `# reserved for the manage v2 (P3).`

3. `src/secretaria/workers/shared/sentinels.py`:
   - imports: acrescentar `from secretaria.services.booking_draft import BookingDraft` (logo antes de `from secretaria.services.booking_scope import (`); acrescentar `ai_draft_v2_enabled,` ao import de `secretaria.services.flow_router` (em ordem alfabética dentro do parêntese: depois de `_start_booking,`); acrescentar, logo depois do import de `secretaria.workers.shared.context`,

```python
from secretaria.workers.shared.draft_resolution import (
    DraftContext,
    _load_draft_context,
    _resolve_draft,
)
```

   - imediatamente ANTES de `async def _handle_set_booking_draft(`, inserir:

```python
def _pra_quem_unknown(conversation: SimpleNamespace, draft: BookingDraft) -> bool:
    """Neither the draft nor the conversation says who the booking is for.

    `flow_attendee_name is None` means "never asked"; ATTENDEE_SELF ("") is an answer
    (services/attendee.py). A hand-back must never read "never asked" as "for the
    patient" - that was the only barrier between "marcar pra minha mãe" and a booking in
    the patient's own name (spec §3, §4.11: the safety fix, outside the switch).
    """
    return draft.attendee is None and conversation.flow_attendee_name is None


async def _apply_draft_resolution(
    reply: _ReplyContext,
    tenant: Tenant,
    draft: BookingDraft,
    ctx: DraftContext,
    *,
    source_tool: str,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
) -> None:
    """The resolver decides; ONE hand-back event is logged; `_apply_flow_result` persists.

    The pre-existing per-tool events keep being emitted (P1's constraint), so dashboards
    built on them see resolver landings too.
    """
    resolution = await _resolve_draft(reply, tenant, draft, ctx)
    result = resolution.result
    if source_tool == hb.SOURCE_SET_BOOKING_DRAFT:
        logger.info(
            "conversation_booking_draft_entered",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
            multi=ctx.topology == BOOKING_TOPOLOGY_MULTI,
            has_type=draft.service is not None,
            flow_step=result.flow_step,
        )
    elif source_tool == hb.SOURCE_START_GUIDED_BOOKING:
        logger.info(
            "conversation_guided_booking_entered",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
            has_type=draft.service is not None,
            flow_step=result.flow_step,
        )
    await _land_handback(
        reply,
        result,
        patient_wa,
        source_tool=source_tool,
        tenant=tenant,
        professionals=ctx.professional_rows,
        redis=redis,
        waba_token=waba_token,
        supplied=draft.supplied_fields(),
        accepted=resolution.accepted,
        dropped=resolution.dropped,
        fallback=resolution.fallback,
        topology=ctx.topology,
    )


async def _legacy_booking_draft(
    reply: _ReplyContext,
    tenant: Tenant,
    draft: BookingDraft,
    ctx: DraftContext,
    *,
    patient_wa: str | None,
    professionals: list | None,
    redis=None,
    waba_token: str | None = None,
) -> None:
    """The v1 landing (switch OFF, pra-quem already answered), with the TASK-030 fixes.

    The branches are the pre-TASK-030 ones, read from `ctx` (fresh) instead of a session
    of their own, with two corrections (spec §7, P2): a doctor whose config is incomplete
    now reaches the clinic alert (the menu fallback used to swallow it), and a STORED
    convênio is kept verbatim - a typed "Outro convênio" is the patient's answer, and
    re-validating it against the plan catalog used to erase it.
    """
    conversation = ctx.conversation
    appointment_type = draft.service
    professional_id = draft.professional_id
    insurance_text = draft.insurance
    supplied = draft.supplied_fields()
    selected_id = professional_id or conversation.flow_selected_professional_id
    attendee = conversation.flow_attendee_name
    stored_type = conversation.flow_selected_type
    professional_rows = ctx.professional_rows
    service_catalog = ctx.service_catalog
    tenant_snapshot = ctx.tenant_snapshot
    topology = ctx.topology
    is_multi = topology == BOOKING_TOPOLOGY_MULTI
    selection_only = appointment_type is None and professional_id is None
    # A convênio the agent typed only counts when it names a real plan; anything else is
    # dropped so the flow ASKS instead of storing a made-up label. One already stored is
    # the patient's own answer and is kept as it is.
    insurance = (
        match_insurance_plan(tenant_snapshot, insurance_text)
        if insurance_text is not None
        else conversation.flow_selected_insurance
    )
    professional = next((p for p in professional_rows if p.id == selected_id), None)
    fresh_services = (
        professional_appointment_types(professional, tenant_snapshot, service_catalog)
        if professional is not None
        else tenant_snapshot.appointment_types
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

    async def _menu(reason: str) -> None:
        await _fallback_to_menu(
            reply,
            source_tool=hb.SOURCE_SET_BOOKING_DRAFT,
            reason=reason,
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

    async def _land(result: FlowRouterResult) -> None:
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

    if selection_only:
        # An unspecified service means administrative choices, never an inferred procedure
        # or a jump to availability. Stored selections are revalidated against the FRESH
        # roster/catalog. Pra-quem is answered on this path (an unknown one goes to the
        # resolver), so the next list is the booking's own, never the question again.
        stored_type = canonical_service_name(fresh_services, stored_type)
        if professional is not None:
            result = _enter_professional_services(professional, tenant_snapshot)
        else:
            result = _start_booking(tenant_snapshot, professional_rows, insurance=insurance)
        if result.action == "professional_config_incomplete":
            await _land(result)
            return
        if result.flow_state != FlowState.SERVICE_CATALOG:
            await _menu(hb.FALLBACK_NO_BOOKABLE_CATALOG)
            return
        result.flow_selected_insurance = insurance
        result.flow_attendee_name = attendee
        if result.flow_step not in ATTENDEE_STEPS:
            result.flow_selected_type = stored_type
        await _land(result)
        return
    if (
        (appointment_type is not None and canonical_type is None)
        or (appointment_type is None and (not is_multi or professional_id is None))
        or (selected_id is not None and professional is None)
    ):
        logger.warning(
            "worker_booking_draft_invalid_selection", conversation_id=str(reply.conversation_id)
        )
        await _menu(hb.FALLBACK_INVALID_SELECTION)
        return
    async with async_session_factory() as session:
        booking_calendar = await _appointment_calendar(
            session,
            tenant,
            _appointment_calendar_target({"professional_id": selected_id}, professional_rows),
        )
    appointment_type = canonical_type
    if is_multi:
        if professional is None:
            # A service without a doctor on a multi-doctor clinic: no one to book with yet.
            await _menu(hb.FALLBACK_MISSING_PROFESSIONAL)
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
    await _land(result)
```

   - substituir `_handle_set_booking_draft` inteira (da linha `async def _handle_set_booking_draft(` até o fim do arquivo — é a última função; o corpo do P1 que ficava aqui foi para `_legacy_booking_draft` acima, então os `log_handback_entered` do P1 desta função são substituídos pelos destes helpers, um por saída) por:

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
    """LLM hand-back: the AI's booking draft (v1 or v2 payload, services/booking_draft.py).

    TASK-030 P2. Everything is re-read FRESH (`_load_draft_context`); `flow_snapshot`
    stays in the signature for the caller and is no longer read. Who decides the landing:

      * the resolver (`resolve_booking_draft`) when the clinic's draft v2 switch is on -
        AND, for every clinic, whenever pra-quem is still unknown: the workflow asks it
        first and parks the rest in `flow_draft` (spec §4.11, the safety fix);
      * otherwise the v1 landing (`_legacy_booking_draft`).

    Every exit records ONE `conversation_handback_entered` (workers/shared/handback_log.py):
    the landing step, or a `fallback` code on every bounce and silent return. Field NAMES
    and reason codes only - never the service, convênio, doctor, day or time.
    """
    try:
        draft = BookingDraft.from_payload(reply_text[len(BOOKING_DRAFT_SENTINEL_PREFIX) :])
    except ValueError:
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
    supplied = draft.supplied_fields()
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
    ctx = await _load_draft_context(reply, tenant)
    if ctx is None:
        logger.warning(
            "worker_booking_draft_no_conversation", conversation_id=str(reply.conversation_id)
        )
        _log_no_landing(
            reply,
            source_tool=hb.SOURCE_SET_BOOKING_DRAFT,
            reason=hb.FALLBACK_NO_PATIENT,
            tenant=tenant,
            professionals=professionals,
            supplied=supplied,
        )
        return
    if ai_draft_v2_enabled(tenant) or _pra_quem_unknown(ctx.conversation, draft):
        await _apply_draft_resolution(
            reply,
            tenant,
            draft,
            ctx,
            source_tool=hb.SOURCE_SET_BOOKING_DRAFT,
            patient_wa=patient_wa,
            redis=redis,
            waba_token=waba_token,
        )
        return
    await _legacy_booking_draft(
        reply,
        tenant,
        draft,
        ctx,
        patient_wa=patient_wa,
        professionals=professionals,
        redis=redis,
        waba_token=waba_token,
    )
```

   - Rode `uvx ruff check src/secretaria/workers/shared/sentinels.py`: remova SOMENTE os imports que ele acusar como não usados (`F401`) — esperados: `json` e `enter_booking`; `load_service_catalog`/`load_tenant_insurance`/`_flow_tenant_snapshot` continuam usados até a Task B2.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_events.py tests/test_handback_log.py tests/test_agent_menu_tools.py tests/test_attendee_booking.py tests/test_set_booking_draft.py tests/test_booking_draft_continuation.py -q`
Expected: PASS. Em especial continuam verdes, sem edição: `test_selection_only_draft_logs_the_administrative_step_it_lands_on` (com `attendee=None` o resolvedor pousa na mesma pergunta), `test_selection_only_draft_on_a_clinic_with_no_bookable_catalog_is_a_counted_fallback` (menu puro, `no_bookable_catalog`), `test_a_typed_convenio_is_accepted_or_dropped_by_name_only`, `test_a_corrupt_draft_is_a_bad_sentinel_with_nothing_trusted`, `test_a_draft_that_cannot_run_is_counted_not_silent`, `test_hand_back_still_asks_pra_quem_when_it_was_never_answered` e `test_pra_mim_answer_survives_the_llm_detour`. Se outro teste do P1 em `tests/test_handback_events.py` falhar porque esperava o pouso v1 com "pra quem" nunca respondido, semeie `attendee=ATTENDEE_SELF` nele (mesmo padrão do Step 1.3) — nunca afrouxe uma asserção de campo do evento.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format tests/test_handback_events.py src/secretaria/workers/shared/handback_log.py   # both created (and left clean) by P1
uvx ruff check src/secretaria/services/flow_router.py src/secretaria/workers/shared/handback_log.py src/secretaria/workers/shared/sentinels.py tests/test_handback_events.py tests/test_agent_menu_tools.py
for f in src/secretaria/services/flow_router.py src/secretaria/workers/shared/sentinels.py tests/test_agent_menu_tools.py; do echo "$f $(uvx ruff format --diff $f 2>/dev/null | grep -c '^@@')"; done   # none may exceed its count before editing
git add src/secretaria/services/flow_router.py src/secretaria/workers/shared/handback_log.py src/secretaria/workers/shared/sentinels.py tests/test_handback_events.py tests/test_agent_menu_tools.py
git diff --cached --stat
git commit -F - <<'EOF'
fix(handback): a booking draft never assumes who the booking is for

Every clinic: when pra-quem was never answered, set_booking_draft lands on "Essa consulta
é pra você?" through the resolver and parks the rest in flow_draft. With the per-clinic
switch (initial_flows.ai_draft_v2, off by default) every draft lands through the
resolver. The v1 path keeps its landings with two fixes: an unbookable doctor reaches the
clinic alert, and a stored typed convênio is kept. The resolver's reason codes join the
hand-back vocabulary.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task B2: `select_professional_and_continue` e `start_guided_booking` — leituras frescas, "pra quem" e serviço re-canonizado

**Files:**
- Modify: `src/secretaria/workers/shared/sentinels.py` (`_handle_select_professional`, `_handle_start_guided_booking` inteiras)
- Modify: `src/secretaria/workers/shared/booking_hold.py` (`_is_agent_sentinel`)
- Modify: `tests/test_handback_events.py`, `tests/test_agent_menu_tools.py`, `tests/test_attendee_booking.py`

**Interfaces:**
- Consumes: B1 (`_pra_quem_unknown`, `_apply_draft_resolution`, `ai_draft_v2_enabled`); P1 (`_fallback_to_menu`, `_land_handback`, `_log_no_landing`); P2a (`_load_draft_context`, `BookingDraft`).
- Produces: assinaturas inalteradas de `_handle_select_professional(reply, reply_text, tenant, flow_snapshot, professionals, patient_wa, redis=None, waba_token=None)` e `_handle_start_guided_booking(reply, reply_text, tenant, professionals, patient_wa, redis=None, waba_token=None)`; `flow_snapshot` não é mais lido. `_is_agent_sentinel` reconhece `START_GUIDED_BOOKING_SENTINEL_PREFIX`.

- [ ] **Step 1: Write the failing tests**

1. `tests/test_handback_events.py` — testes do P1 que descrevem o pouso v1 passam a semear o "pra quem" ou o banco (a leitura agora é fresca):
   - `test_select_professional_logs_the_step_it_landed_on`: `await _seed(db)` → `await _seed(db, attendee=ATTENDEE_SELF)`;
   - `test_the_event_is_logged_before_the_flow_state_is_written`: idem;
   - `test_guided_booking_logs_the_step_it_landed_on` e `test_guided_booking_with_no_agenda_lands_on_a_person`: `await _seed_sole(db)` → `await _seed_sole(db, attendee=ATTENDEE_SELF)`;
   - `test_a_doctor_with_no_services_lands_on_the_config_alert`: trocar as duas linhas

```python
    snapshots = _snapshots([ana, bruno])
    snapshots[0].appointment_types = []  # her own empty list: nothing to book
```

   por

```python
    # Her own empty list: nothing to book. Set on the ROW - the handler reads fresh.
    async with db() as session:
        doctor = await session.get(Professional, ana.id)
        doctor.appointment_types = []
        await session.commit()
    snapshots = _snapshots([ana, bruno])
```

2. Ao fim de `tests/test_handback_events.py`, acrescentar:

```python
# --------------------------------------------------------------------------
# TASK-030 P2: the doctor and guided hand-backs read fresh and ask pra-quem
# --------------------------------------------------------------------------


async def test_select_professional_asks_pra_quem_first_when_unknown(
    db, _captured_bubbles, log
) -> None:
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
    assert event["landing_step"] == "awaiting_attendee_choice"
    assert event["accepted"] == ["professional"]
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_draft["p"] == str(ana.id)
    assert conv.flow_selected_professional_id is None


@pytest.mark.parametrize(
    "stored, in_snapshot, landing",
    [
        ("Maria da Silva", None, "awaiting_service"),
        (None, "Maria da Silva", "awaiting_attendee_choice"),
    ],
)
async def test_select_professional_reads_the_attendee_fresh(
    db, _captured_bubbles, log, stored, in_snapshot, landing
) -> None:
    """The turn-start snapshot may be several tool calls old: the row decides."""
    tenant, ana, bruno, patient, conversation = await _seed(db, attendee=stored)
    stale = (
        SimpleNamespace(flow_attendee_name=in_snapshot, flow_selected_insurance=None),
        tenant,
    )

    await tasks._handle_select_professional(
        _reply_ctx(conversation),
        f"{SELECT_PROFESSIONAL_SENTINEL_PREFIX}{ana.id}",
        tenant,
        stale,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["landing_step"] == landing
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_attendee_name == stored


async def test_guided_booking_asks_pra_quem_first_when_unknown(
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
    assert event["landing_step"] == "awaiting_attendee_choice"
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_draft["t"] == "Consulta Geral"


async def test_guided_booking_recanonicalizes_against_the_fresh_catalog(
    db, _captured_bubbles, _stub_calendar, log
) -> None:
    """The tool proved the name against ITS turn's catalog; the service may be gone now."""
    tenant, ana, patient, conversation = await _seed_sole(db, attendee=ATTENDEE_SELF)

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation),
        f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Removida",
        tenant,
        _snapshots([ana]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["landing_step"] == "awaiting_service"  # the service list, never typeless
    assert event["dropped"] == {"service": "not_in_catalog"}


def test_the_guided_sentinel_is_a_protocol_string() -> None:
    assert tasks._is_agent_sentinel(f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral")
```

3. `tests/test_agent_menu_tools.py` — os testes do pouso v1 semeiam "pra quem" respondido:
   - `test_handle_select_professional_reenters_flow_at_doctor`: `await _seed(db, flow_state=FlowState.LLM)` → `await _seed(db, flow_state=FlowState.LLM, attendee=ATTENDEE_SELF)`;
   - em `test_handle_start_guided_booking_opens_the_day_picker`, `test_handle_start_guided_booking_asks_convenio_first_when_configured`, `test_handle_start_guided_booking_without_a_type_still_reaches_the_picker`, `test_handle_start_guided_booking_hands_off_when_the_agenda_is_unknown`, `test_the_next_tap_after_the_handback_actually_advances` e `test_handle_start_guided_booking_slots_on_the_sole_doctors_own_duration`: `await _seed_sole(db)` → `await _seed_sole(db, attendee=ATTENDEE_SELF)`;
   - em `test_handle_start_guided_booking_keeps_an_answered_convenio`: `await _seed_sole(db, insurance="Unimed")` → `await _seed_sole(db, insurance="Unimed", attendee=ATTENDEE_SELF)`.

4. `tests/test_attendee_booking.py::test_llm_choose_doctor_hand_back_keeps_the_attendee` — o handler agora lê o roster e o "pra quem" do banco. Substituir o corpo do teste (da linha `db = wired` até o fim da função) por:

```python
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    async with db() as session:
        async with session.begin():
            doctor = Professional(
                tenant_id=tenant.id,
                name="Dra. Ana",
                specialty="Cardiologia",
                is_active=True,
                appointment_types=[
                    {"name": "Primeira Consulta", "duration_min": 40, "is_active": True}
                ],
            )
            session.add(doctor)
    conversation = await _conversation(db, tenant)
    async with db() as session:
        async with session.begin():
            row = await session.get(Conversation, conversation.id)
            row.flow_attendee_name = "Maria da Silva"
    reply = tasks._ReplyContext(
        channel=CHANNEL_WHATSAPP,
        conversation_id=conversation.id,
        patient_ref=WA_ID,
        inbound_body="quero a Dra. Ana",
        tenant_id=tenant.id,
    )
    # The turn-start snapshot is stale on purpose: the ROW is what counts now.
    snapshot = (SimpleNamespace(flow_attendee_name=None), _tenant())
    await tasks._handle_select_professional(
        reply,
        f"{tasks.SELECT_PROFESSIONAL_SENTINEL_PREFIX}{doctor.id}",
        tenant,
        snapshot,
        [doctor],
        WA_ID,
        waba_token="t",
    )
    conversation = await _conversation(db, tenant)
    assert conversation.flow_state == FlowState.SERVICE_CATALOG
    assert conversation.flow_attendee_name == "Maria da Silva"
```

   e acrescentar `Professional,` ao import de `secretaria.models` do arquivo (entre `Patient,` e `Tenant,`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_events.py tests/test_agent_menu_tools.py tests/test_attendee_booking.py -q`
Expected: os 6 novos de B2 FAIL (pousam no caminho antigo / `_is_agent_sentinel` falso) e `test_llm_choose_doctor_hand_back_keeps_the_attendee` FAIL (o handler ainda lê o snapshot velho e o roster do turno).

- [ ] **Step 3: Implement**

1. `src/secretaria/workers/shared/booking_hold.py`: acrescentar `START_GUIDED_BOOKING_SENTINEL_PREFIX,` ao import de `secretaria.ai.graph` (depois de `SHOW_MAIN_MENU_SENTINEL,`) e, em `_is_agent_sentinel`, trocar

```python
        or reply_text.startswith(BOOKING_DRAFT_SENTINEL_PREFIX)
    )
```

por

```python
        or reply_text.startswith(BOOKING_DRAFT_SENTINEL_PREFIX)
        or reply_text.startswith(START_GUIDED_BOOKING_SENTINEL_PREFIX)
    )
```

2. `src/secretaria/workers/shared/sentinels.py`, substituir `_handle_select_professional` inteira (do `async def _handle_select_professional(` até a linha antes de `async def _handle_manage_appointment(`) por:

```python
async def _handle_select_professional(
    reply: _ReplyContext,
    reply_text: str,
    tenant: Tenant | None,
    flow_snapshot: tuple[SimpleNamespace, SimpleNamespace] | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
) -> None:
    """LLM hand-back: re-enter the deterministic flow at the chosen doctor.

    TASK-030 P2: the roster, pra-quem and convênio are read FRESH (`_load_draft_context`);
    the turn-start `flow_snapshot` is no longer consulted - after several tool calls it can
    say the opposite of the row. With pra-quem unknown, or the clinic's draft v2 switch
    on, the resolver lands it (pra-quem first, the doctor parked in `flow_draft`);
    otherwise the doctor's own service list, as before. An id that does not resolve
    against the ACTIVE roster falls back to the plain menu.
    """
    raw_id = reply_text[len(SELECT_PROFESSIONAL_SENTINEL_PREFIX) :]
    professional_id = None
    bad_sentinel = False
    try:
        professional_id = UUID(raw_id)
    except ValueError:
        bad_sentinel = True
        logger.error("worker_select_professional_bad_sentinel", raw=raw_id[:64])
    ctx = (
        await _load_draft_context(reply, tenant)
        if tenant is not None and professional_id is not None
        else None
    )
    roster = ctx.professionals if ctx is not None else list(professionals or [])
    professional = next((p for p in roster if p.id == professional_id), None)
    if professional is None or tenant is None or ctx is None:
        logger.warning(
            "worker_select_professional_unresolved",
            conversation_id=str(reply.conversation_id),
        )
        # Most specific reason first: a malformed id, a doctor who is no longer on the
        # active roster, no tenant to render a menu for, or no conversation row at all.
        supplied: tuple[str, ...] = ()
        accepted: tuple[str, ...] = ()
        dropped: dict[str, str] = {}
        if bad_sentinel:
            reason = hb.FALLBACK_BAD_SENTINEL
        elif professional is None:
            reason = hb.FALLBACK_UNKNOWN_PROFESSIONAL
            supplied = (hb.FIELD_PROFESSIONAL,)
            dropped = {hb.FIELD_PROFESSIONAL: hb.DROP_UNKNOWN_PROFESSIONAL}
        elif tenant is None:
            reason = hb.FALLBACK_NO_TENANT
            supplied = accepted = (hb.FIELD_PROFESSIONAL,)
        else:
            reason = hb.FALLBACK_NO_PATIENT
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
    draft = BookingDraft(professional_id=professional.id)
    if ai_draft_v2_enabled(tenant) or _pra_quem_unknown(ctx.conversation, draft):
        await _apply_draft_resolution(
            reply,
            tenant,
            draft,
            ctx,
            source_tool=hb.SOURCE_SELECT_PROFESSIONAL,
            patient_wa=patient_wa,
            redis=redis,
            waba_token=waba_token,
        )
        return
    result = _enter_professional_services(professional, ctx.tenant_snapshot)
    # Built here, not by route(), so `_carry_booking` never saw it: keep the authorized
    # attendee and the convênio answer of the booking this hand-back continues - FRESH.
    # (An unanswered convênio is asked at "Sim, agendar".)
    if result.flow_state == FlowState.SERVICE_CATALOG:
        result.flow_attendee_name = ctx.conversation.flow_attendee_name
        result.flow_selected_insurance = ctx.conversation.flow_selected_insurance
    await _land_handback(
        reply,
        result,
        patient_wa,
        source_tool=hb.SOURCE_SELECT_PROFESSIONAL,
        tenant=tenant,
        professionals=ctx.professional_rows,
        redis=redis,
        waba_token=waba_token,
        supplied=(hb.FIELD_PROFESSIONAL,),
        accepted=(hb.FIELD_PROFESSIONAL,),
        topology=ctx.topology,
    )

```

3. Substituir `_handle_start_guided_booking` inteira (do `async def _handle_start_guided_booking(` até a linha antes de `def _pra_quem_unknown(`) por:

```python
async def _handle_start_guided_booking(
    reply: _ReplyContext,
    reply_text: str,
    tenant: Tenant | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
) -> None:
    """LLM hand-back: resume the booking in the button flow, service in hand.

    The agent's OPTIONAL offer (ai/tools.py::start_guided_booking). Everything is re-read
    FRESH (`_load_draft_context`), and TASK-030 P2 adds three things:

      * the service is RE-canonicalized against the fresh catalog - the tool proved it
        against its own turn's catalog, which may be minutes old; a service that no
        longer resolves goes to the resolver, which drops it and shows the service list
        (it used to ride on into a booking of a service the clinic no longer has);
      * pra-quem unknown -> the resolver asks it first (the safety fix);
      * the clinic's draft v2 switch on -> the resolver lands it.

    A multi-professional tenant is still turned away to the menu, decided on the FRESH
    roster: the day picker this opens would read the clinic-level agenda, which is no
    doctor's real availability.
    """
    appointment_type = reply_text[len(START_GUIDED_BOOKING_SENTINEL_PREFIX) :].strip() or None
    # The tool proved the service against the catalog of ITS turn; None means the clinic
    # had no catalog and nothing was supplied.
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
    ctx = await _load_draft_context(reply, tenant)
    if ctx is None:
        logger.warning(
            "worker_start_guided_booking_no_conversation",
            conversation_id=str(reply.conversation_id),
        )
        _log_no_landing(
            reply,
            source_tool=hb.SOURCE_START_GUIDED_BOOKING,
            reason=hb.FALLBACK_NO_PATIENT,
            tenant=tenant,
            professionals=professionals,
            supplied=supplied,
            accepted=supplied,
        )
        return
    if ctx.topology == BOOKING_TOPOLOGY_MULTI:
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
            topology=ctx.topology,
        )
        return
    canonical = (
        canonical_service_name(ctx.tenant_snapshot.appointment_types, appointment_type)
        if appointment_type is not None
        else None
    )
    draft = BookingDraft(service=appointment_type)
    if (
        ai_draft_v2_enabled(tenant)
        or _pra_quem_unknown(ctx.conversation, draft)
        or (appointment_type is not None and canonical is None)
    ):
        await _apply_draft_resolution(
            reply,
            tenant,
            draft,
            ctx,
            source_tool=hb.SOURCE_START_GUIDED_BOOKING,
            patient_wa=patient_wa,
            redis=redis,
            waba_token=waba_token,
        )
        return
    selected_id = ctx.conversation.flow_selected_professional_id
    # WHOSE agenda the day picker reads: the selection when it still resolves, the tenant
    # agenda when none was ever picked (on a sole-professional clinic `load_tenant_config`
    # has already resolved THAT professional's credentials into it), None otherwise - so
    # the picker answers `calendar_unavailable` instead of guessing.
    async with async_session_factory() as session:
        booking_calendar = await _appointment_calendar(
            session,
            tenant,
            _appointment_calendar_target({"professional_id": selected_id}, ctx.professional_rows),
        )
    result = await enter_guided_booking(
        ctx.tenant_snapshot,
        booking_calendar,
        canonical,
        conversation_id=reply.conversation_id,
        professional_id=selected_id,
        insurance=ctx.conversation.flow_selected_insurance,
        professionals=ctx.professional_rows,
        attendee_name=ctx.conversation.flow_attendee_name,
    )
    logger.info(
        "conversation_guided_booking_entered",
        conversation_id=str(reply.conversation_id),
        tenant_id=str(tenant.id),
        # Enums and flags only: whether a service came through and which step the flow
        # resumed at. Never the service name, never the convênio.
        has_type=canonical is not None,
        flow_step=result.flow_step,
    )
    await _land_handback(
        reply,
        result,
        patient_wa,
        source_tool=hb.SOURCE_START_GUIDED_BOOKING,
        tenant=tenant,
        professionals=ctx.professional_rows,
        redis=redis,
        waba_token=waba_token,
        supplied=supplied,
        accepted=supplied,
        topology=ctx.topology,
    )

```

4. Rode `uvx ruff check src/secretaria/workers/shared/sentinels.py` e remova SOMENTE os imports que ele acusar como não usados (`F401`) — esperados agora: `load_service_catalog`, `load_tenant_insurance`, `_flow_tenant_snapshot` e, se for o caso, `booking_topology`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_handback_events.py tests/test_agent_menu_tools.py tests/test_attendee_booking.py tests/test_multi_professional_plugin.py tests/test_workers_layering.py -q`
Expected: PASS. Continuam verdes sem edição: `test_handle_select_professional_unresolved_falls_back_to_menu`, `test_an_unknown_professional_falls_back_to_the_menu_and_is_counted_once`, `test_a_malformed_professional_id_is_a_bad_sentinel`, `test_select_professional_without_a_tenant_is_counted_not_silent`, `test_handle_start_guided_booking_turns_a_multi_doctor_clinic_away`, `test_guided_booking_turns_a_multi_doctor_clinic_away_and_says_so`, `test_guided_booking_without_a_tenant_is_counted_not_silent`, `test_handle_start_guided_booking_without_a_tenant_is_a_logged_noop`.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format tests/test_handback_events.py   # created (and left clean) by P1
uvx ruff check src/secretaria/workers/shared/sentinels.py src/secretaria/workers/shared/booking_hold.py tests/test_handback_events.py tests/test_agent_menu_tools.py tests/test_attendee_booking.py
for f in src/secretaria/workers/shared/sentinels.py src/secretaria/workers/shared/booking_hold.py tests/test_agent_menu_tools.py tests/test_attendee_booking.py; do echo "$f $(uvx ruff format --diff $f 2>/dev/null | grep -c '^@@')"; done   # none may exceed its count before editing
git add src/secretaria/workers/shared/sentinels.py src/secretaria/workers/shared/booking_hold.py tests/test_handback_events.py tests/test_agent_menu_tools.py tests/test_attendee_booking.py
git diff --cached --stat
git commit -F - <<'EOF'
fix(handback): doctor and guided hand-backs read fresh and ask pra-quem

select_professional_and_continue no longer trusts the turn-start snapshot for the
attendee, the convênio or the roster; start_guided_booking re-canonicalizes the service
against the fresh catalog. Both ask "Essa consulta é pra você?" when it was never
answered (every clinic) and land through the resolver with the draft v2 switch on.
_is_agent_sentinel now lists the guided-booking sentinel.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task B3: Ferramenta `set_booking_draft` v2, o sentinel v2 e o cache de agentes por variante

**Files:**
- Modify: `src/secretaria/ai/tools.py` (`import re`; `BookingDraftRequested` v2; `set_booking_draft_v2` e auxiliares depois de `set_booking_draft`)
- Modify: `src/secretaria/ai/graph.py` (ramo `except BookingDraftRequested`; `_tool_cache_key`; comentário do `BOOKING_DRAFT_SENTINEL_PREFIX`)
- Modify: `src/secretaria/workers/shared/llm_context.py` (`_flow_handback_tools`)
- Create: `tests/test_set_booking_draft_v2.py`
- Modify: `tests/test_agent_menu_tools.py` (`test_run_agent_maps_booking_draft_to_sentinel` + irmão v2), `tests/test_agent_capability_cache.py`

**Interfaces:**
- Consumes: P2a (`BookingDraft`), B1 (`ai_draft_v2_enabled`).
- Produces:
  - `BookingDraftRequested(appointment_type, professional_id, insurance, *, attendee=None, day=None, time=None)` com a propriedade `draft -> BookingDraft`.
  - `set_booking_draft_v2` — `StructuredTool` de nome `"set_booking_draft"`, args `service, professional, insurance, for_whom, day, time` (todos `str = ""`), `metadata = {"cache_variant": "draft_v2"}`.
  - `TOOL_BLOCK_BAD_FOR_WHOM = "bad_for_whom"`, `TOOL_BLOCK_BAD_DAY = "bad_day"`, `TOOL_BLOCK_BAD_TIME = "bad_time"`; evento `booking_draft_tool_item_dropped` (`field`, `reason` ∈ {`unknown_professional`, `ambiguous_professional`}).
  - `graph._tool_cache_key(tool) -> str`.
  - `_flow_handback_tools(tenant, topology, plugin_tools)` entrega `set_booking_draft_v2` quando `ai_draft_v2_enabled(tenant)`, senão `set_booking_draft` (P4 acrescenta `get_availability` no mesmo ponto; P5 troca o prompt pelo mesmo interruptor).

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_set_booking_draft_v2.py`:

```python
"""set_booking_draft v2: six fields, formats only, never a name (TASK-030 P2)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import datetime as dt  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.ai import tools as ai_tools  # noqa: E402
from secretaria.ai.tools import (  # noqa: E402
    BookingDraftRequested,
    set_booking_draft,
    set_booking_draft_v2,
)
from secretaria.plugins import multi_professional as mp  # noqa: E402
from secretaria.services.booking_draft import BookingDraft  # noqa: E402
from secretaria.services.booking_scope import BOOKING_TOPOLOGY_SOLE  # noqa: E402
from secretaria.workers.shared.llm_context import _flow_handback_tools  # noqa: E402

ANA = SimpleNamespace(id=uuid4(), name="Dra. Ana")
BETO = SimpleNamespace(id=uuid4(), name="Dr. Beto")


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log


@pytest.fixture
def clinic(monkeypatch):
    roster = [ANA, BETO]

    async def _pros(_tenant_id):
        return list(roster)

    monkeypatch.setattr(mp, "_active_professionals", _pros)
    log = _Log()
    monkeypatch.setattr(ai_tools, "logger", log)
    token = ai_tools._tenant_id_ctx.set(uuid4())
    yield roster, log
    ai_tools._tenant_id_ctx.reset(token)


def test_the_v2_tool_is_named_set_booking_draft_with_six_string_args():
    assert set_booking_draft_v2.name == "set_booking_draft"
    assert set(set_booking_draft_v2.args) == {
        "service", "professional", "insurance", "for_whom", "day", "time",
    }


async def test_v2_carries_every_field_raw_for_the_resolver(clinic):
    with pytest.raises(BookingDraftRequested) as exc:
        await set_booking_draft_v2.ainvoke(
            {
                "service": " limpeza ",
                "professional": "dra. ana",
                "insurance": "Unimed",
                "for_whom": "me",
                "day": "2026-10-08",
                "time": "10:00",
            }
        )
    assert exc.value.draft == BookingDraft(
        service="limpeza",  # the resolver canonicalizes against the FRESH catalog
        professional_id=ANA.id,
        insurance="Unimed",
        attendee="self",
        day=dt.date(2026, 10, 8),
        time=dt.time(10, 0),
    )


async def test_v2_other_is_other_and_empty_is_unknown(clinic):
    with pytest.raises(BookingDraftRequested) as exc:
        await set_booking_draft_v2.ainvoke({"for_whom": "other"})
    assert exc.value.draft.attendee == "other"
    with pytest.raises(BookingDraftRequested) as exc:
        await set_booking_draft_v2.ainvoke({})
    assert exc.value.draft == BookingDraft()


async def test_v2_for_whom_is_never_a_name(clinic):
    out = await set_booking_draft_v2.ainvoke({"for_whom": "Maria Silva"})
    assert "error" in out
    assert "Maria" not in out["error"]


@pytest.mark.parametrize(
    "field, value",
    [("day", "08/10/2026"), ("day", "2026-13-01"), ("time", "10h"), ("time", "25:00")],
)
async def test_v2_a_bad_format_is_a_recoverable_error(clinic, field, value):
    out = await set_booking_draft_v2.ainvoke({"service": "Limpeza", field: value})
    assert "error" in out


@pytest.mark.parametrize(
    "name, reason",
    [("Dr. Fantasma", "unknown_professional"), ("Dra. Ana", "ambiguous_professional")],
)
async def test_v2_an_unresolvable_doctor_is_dropped_not_an_error(clinic, name, reason):
    roster, log = clinic
    if reason == "ambiguous_professional":
        roster.append(SimpleNamespace(id=uuid4(), name="Dra. Ana"))
    with pytest.raises(BookingDraftRequested) as exc:
        await set_booking_draft_v2.ainvoke({"professional": name, "service": "Limpeza"})
    assert exc.value.draft.professional_id is None
    assert exc.value.draft.service == "Limpeza"
    dropped = [f for e, f in log.events if e == "booking_draft_tool_item_dropped"]
    assert dropped == [{"field": "professional", "reason": reason}]


async def test_v2_without_a_clinic_is_an_error():
    token = ai_tools._tenant_id_ctx.set(None)
    try:
        out = await set_booking_draft_v2.ainvoke({"service": "Limpeza"})
    finally:
        ai_tools._tenant_id_ctx.reset(token)
    assert "error" in out


def test_the_clinic_switch_picks_the_draft_tool():
    off = _flow_handback_tools(SimpleNamespace(initial_flows={}), BOOKING_TOPOLOGY_SOLE, [])
    on = _flow_handback_tools(
        SimpleNamespace(initial_flows={"ai_draft_v2": True}), BOOKING_TOPOLOGY_SOLE, []
    )
    assert set_booking_draft in off and set_booking_draft_v2 not in off
    assert set_booking_draft_v2 in on and set_booking_draft not in on
    assert sorted(t.name for t in on) == sorted(t.name for t in off)
```

Em `tests/test_agent_menu_tools.py`, trocar a última asserção de `test_run_agent_maps_booking_draft_to_sentinel`

```python
    assert payload == {"t": "Limpeza", "p": None, "i": "Unimed"}
```

por

```python
    # v1 raise sites now serialize through BookingDraft: the three v2 keys go out as null.
    assert payload == {"t": "Limpeza", "p": None, "i": "Unimed", "w": None, "d": None, "h": None}
```

e acrescentar logo depois dessa função:

```python
async def test_run_agent_maps_a_v2_booking_draft_to_its_sentinel(monkeypatch):
    import datetime as dt

    from secretaria.ai.graph import BOOKING_DRAFT_SENTINEL_PREFIX
    from secretaria.ai.tools import BookingDraftRequested
    from secretaria.services.booking_draft import BookingDraft

    async def _history(_cid):
        return [HumanMessage(content="oi")]

    async def _boom(_messages, _cid):
        raise BookingDraftRequested(
            "Limpeza", None, None, attendee="other", day=dt.date(2026, 10, 8), time=dt.time(10, 0)
        )

    monkeypatch.setattr(graph, "_load_history", _history)
    monkeypatch.setattr(graph, "_invoke_agent_with_retry", _boom)
    reply = await run_agent("oi", context={"conversation_id": str(uuid4())})
    assert BookingDraft.from_payload(reply[len(BOOKING_DRAFT_SENTINEL_PREFIX) :]) == BookingDraft(
        service="Limpeza", attendee="other", day=dt.date(2026, 10, 8), time=dt.time(10, 0)
    )
```

Em `tests/test_agent_capability_cache.py`, acrescentar ao fim:

```python
def test_two_draft_tools_never_share_a_compiled_agent(_clean_agent_cache_and_fakes):
    """v1 and v2 share the model-facing NAME `set_booking_draft` (TASK-030 P2); keyed by
    name alone, the first compiled would serve every clinic."""
    from secretaria.ai.tools import set_booking_draft, set_booking_draft_v2

    v1 = graph.build_agent(extra_tools=[set_booking_draft])
    v2 = graph.build_agent(extra_tools=[set_booking_draft_v2])
    assert v1 is not v2
    assert graph.build_agent(extra_tools=[set_booking_draft_v2]) is v2
    assert len(_clean_agent_cache_and_fakes) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_set_booking_draft_v2.py tests/test_agent_menu_tools.py tests/test_agent_capability_cache.py -q`
Expected: erro de coleta em `test_set_booking_draft_v2.py` — `ImportError: cannot import name 'set_booking_draft_v2' from 'secretaria.ai.tools'`.

- [ ] **Step 3: Implement**

1. `src/secretaria/ai/tools.py`:
   - stdlib: acrescentar `import re` como PRIMEIRA linha de import (antes de `from collections.abc import Sequence` — o ruff põe `import x` antes de `from x import y` na mesma seção);
   - primeira parte: acrescentar `from secretaria.services.booking_draft import BookingDraft` logo antes de `from secretaria.services.booking_scope import (`;
   - substituir a classe `BookingDraftRequested` inteira por:

```python
class BookingDraftRequested(Exception):
    """Raised by `set_booking_draft` (v1 and v2): the agent resolved what the patient wants.

    Same exception->sentinel mechanism as `GuidedBookingRequested`. graph.run_agent
    serializes `draft` (services/booking_draft.py) after BOOKING_DRAFT_SENTINEL_PREFIX;
    workers/shared/sentinels.py::_handle_set_booking_draft lands it. The LLM never writes
    `flow_selected_*` itself, and never a third party's name: `attendee` is only
    "self"/"other" (TASK-030 P2).
    """

    def __init__(
        self,
        appointment_type: str | None,
        professional_id: UUID | None,
        insurance: str | None,
        *,
        attendee: str | None = None,
        day: date | None = None,
        time: Any = None,
    ) -> None:
        super().__init__("set booking draft")
        self.appointment_type = appointment_type
        self.professional_id = professional_id
        self.insurance = insurance
        self.attendee = attendee
        self.day = day
        self.time = time

    @property
    def draft(self) -> BookingDraft:
        return BookingDraft(
            service=self.appointment_type,
            professional_id=self.professional_id,
            insurance=self.insurance,
            attendee=self.attendee,  # type: ignore[arg-type]
            day=self.day,
            time=self.time,
        )
```

   - logo depois da função `set_booking_draft` (antes de `HANDOFF_REASONS = (`), inserir:

```python
# --- set_booking_draft v2 (TASK-030 P2) ----------------------------------------------------
# Same model-facing NAME as the v1 tool above; the clinic's switch picks one
# (`flow_router.ai_draft_v2_enabled`, workers/shared/llm_context.py::_flow_handback_tools).
# It checks FORMATS only. Whether a service, convênio or doctor exists is the worker-side
# resolver's call (services/booking_draft.py), which drops just the invalid item and asks
# from that step - so "not in the catalog" is never an error the model would answer in text.
TOOL_BLOCK_BAD_FOR_WHOM = "bad_for_whom"
TOOL_BLOCK_BAD_DAY = "bad_day"
TOOL_BLOCK_BAD_TIME = "bad_time"
_FOR_WHOM_VALUES: dict[str, str | None] = {"": None, "me": "self", "other": "other"}
_FOR_WHOM_ERROR = (
    'for_whom aceita só "me" (a consulta é para o próprio paciente), "other" (é para outra '
    "pessoa) ou vazio (ele não disse). Nunca escreva um nome nesse campo."
)
_DAY_FORMAT_ERROR = (
    "day precisa estar no formato AAAA-MM-DD (ex.: 2026-10-08), no fuso da clínica."
)
_TIME_FORMAT_ERROR = "time precisa estar no formato HH:MM (ex.: 10:00)."
_ISO_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_HHMM_RE = re.compile(r"\d{2}:\d{2}")
_DRAFT_TEXT_MAX = 120


async def _draft_professional_id(tenant_id: UUID, name: str) -> UUID | None:
    """The ACTIVE professional with exactly this name, or None (counted, never an error)."""
    if not name:
        return None
    # Lazy: plugins import this module, so the reverse import must not be top-level.
    from secretaria.plugins.multi_professional import _active_professionals

    roster = await _active_professionals(tenant_id)
    matches = [p for p in roster if p.name.strip().casefold() == name.casefold()]
    if len(matches) == 1:
        return matches[0].id
    logger.info(
        "booking_draft_tool_item_dropped",
        field="professional",
        reason="ambiguous_professional" if matches else "unknown_professional",
    )
    return None


@tool("set_booking_draft")
async def set_booking_draft_v2(
    service: str = "",
    professional: str = "",
    insurance: str = "",
    for_whom: str = "",
    day: str = "",
    time: str = "",
) -> dict:
    """Entrega o agendamento ao fluxo guiado com TUDO o que o paciente já disse. O fluxo
    confere cada item com os dados reais da clínica e abre a próxima etapa que falta - pode
    ir até a lista de horários do dia pedido. Preencha só o que o paciente disse e deixe o
    resto vazio; não repita uma pergunta que ele já respondeu. Nunca deduza um procedimento
    a partir de sintomas e nunca invente dia ou horário.

    Args:
        service: Nome EXATO de um serviço da clínica escolhido pelo paciente (ou vazio).
        professional: Nome do profissional que o paciente escolheu (ou vazio).
        insurance: Convênio que o paciente citou (ou vazio).
        for_whom: "me" se a consulta é para o próprio paciente, "other" se é para outra
            pessoa, vazio se ele não disse. NUNCA escreva um nome aqui.
        day: Dia pedido no formato AAAA-MM-DD, no fuso da clínica (ou vazio).
        time: Horário pedido no formato HH:MM (ou vazio). Sem `day`, é ignorado.
    """
    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        return {"error": "Nenhuma clínica configurada para esta conversa."}
    who = (for_whom or "").strip().casefold()
    if who not in _FOR_WHOM_VALUES:
        # The value itself is never logged nor echoed: it may be a third party's name.
        logger.info("agent_tool_blocked", tool="set_booking_draft", reason=TOOL_BLOCK_BAD_FOR_WHOM)
        return {"error": _FOR_WHOM_ERROR}
    day_text = (day or "").strip()
    parsed_day: date | None = None
    if day_text:
        try:
            if not _ISO_DAY_RE.fullmatch(day_text):
                raise ValueError(day_text)
            parsed_day = date.fromisoformat(day_text)
        except ValueError:
            logger.info("agent_tool_blocked", tool="set_booking_draft", reason=TOOL_BLOCK_BAD_DAY)
            return {"error": _DAY_FORMAT_ERROR}
    time_text = (time or "").strip()
    parsed_time = None
    if time_text:
        try:
            if not _HHMM_RE.fullmatch(time_text):
                raise ValueError(time_text)
            parsed_time = datetime.strptime(time_text, "%H:%M").time()
        except ValueError:
            logger.info("agent_tool_blocked", tool="set_booking_draft", reason=TOOL_BLOCK_BAD_TIME)
            return {"error": _TIME_FORMAT_ERROR}
    professional_id = await _draft_professional_id(tenant_id, (professional or "").strip())
    raise BookingDraftRequested(
        (service or "").strip()[:_DRAFT_TEXT_MAX] or None,
        professional_id,
        (insurance or "").strip()[:_DRAFT_TEXT_MAX] or None,
        attendee=_FOR_WHOM_VALUES[who],
        day=parsed_day,
        time=parsed_time,
    )


# Read by ai/graph.py::_tool_cache_key: the v1 and v2 tools share the name
# "set_booking_draft" and must never share a compiled agent.
set_booking_draft_v2.metadata = {"cache_variant": "draft_v2"}
```

2. `src/secretaria/ai/graph.py`:
   - no ramo `except BookingDraftRequested as exc:` de `run_agent`, trocar

```python
        logger.info(
            "ai_run_agent_set_booking_draft",
            conversation_id=str(conversation_id),
            has_type=exc.appointment_type is not None,
            has_professional=exc.professional_id is not None,
            has_insurance=exc.insurance is not None,
        )
        return BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps(
            {
                "t": exc.appointment_type,
                "p": str(exc.professional_id) if exc.professional_id else None,
                "i": exc.insurance,
            }
        )
```

   por

```python
        draft = exc.draft
        logger.info(
            "ai_run_agent_set_booking_draft",
            conversation_id=str(conversation_id),
            has_type=draft.service is not None,
            has_professional=draft.professional_id is not None,
            has_insurance=draft.insurance is not None,
            has_for_whom=draft.attendee is not None,
            has_day=draft.day is not None,
            has_time=draft.time is not None,
        )
        return BOOKING_DRAFT_SENTINEL_PREFIX + draft.to_payload()
```

   - trocar o comentário acima de `HUMAN_HANDOFF_SENTINEL_PREFIX` (as quatro linhas que começam em `# Prefix returned when the agent called set_booking_draft; a compact JSON`) por:

```python
# Prefix returned when the agent called set_booking_draft (v1 or v2); the draft rides
# after the colon as compact JSON - keys t/p/i/w/d/h, null for absent
# (services/booking_draft.py::BookingDraft) - and
# workers/shared/sentinels.py::_handle_set_booking_draft lands it. None of the values is a
# patient's or a third party's name: "w" is only "self"/"other".
```

   - logo antes de `def build_agent(`, inserir:

```python
def _tool_cache_key(tool: Any) -> str:
    """A tool's identity in `_AGENTS`: its NAME, plus a declared variant.

    Two implementations may share one model-facing name - TASK-030's v1 and v2
    `set_booking_draft`, picked per clinic. Keyed by name alone, whichever compiled first
    would serve every clinic. A tool declares `metadata={"cache_variant": ...}` to be told
    apart; every other tool keys exactly as before.
    """
    name = getattr(tool, "name", str(tool))
    variant = (getattr(tool, "metadata", None) or {}).get("cache_variant")
    return f"{name}#{variant}" if variant else name
```

   - em `build_agent`, trocar `key = frozenset(getattr(t, "name", str(t)) for t in tools)` por `key = frozenset(_tool_cache_key(t) for t in tools)`;
   - se o `uvx ruff check src/secretaria/ai/graph.py` acusar `json` como não usado (`F401`), remova `import json`.

3. `src/secretaria/workers/shared/llm_context.py`:
   - trocar o import de `secretaria.ai.tools` por

```python
from secretaria.ai.tools import (
    manage_existing_appointment,
    request_human_handoff,
    set_booking_draft,
    set_booking_draft_v2,
    start_guided_booking,
)
```

     e o de `secretaria.services.flow_router` por

```python
from secretaria.services.flow_router import (
    LABEL_RESCHEDULE,
    ai_draft_v2_enabled,
    flows_enabled,
)
```

   - em `_flow_handback_tools`, trocar `handbacks = [manage_existing_appointment, set_booking_draft, request_human_handoff]` por

```python
    # TASK-030: same model-facing name, two implementations; the clinic's switch picks one.
    draft_tool = set_booking_draft_v2 if ai_draft_v2_enabled(tenant) else set_booking_draft
    handbacks = [manage_existing_appointment, draft_tool, request_human_handoff]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_set_booking_draft_v2.py tests/test_set_booking_draft.py tests/test_agent_menu_tools.py tests/test_agent_capability_cache.py tests/test_handback_events.py tests/test_prompts.py tests/test_llm_turn_trace.py -q`
Expected: PASS (os v1 de `test_set_booking_draft.py` inalterados: a ferramenta antiga não mudou).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format tests/test_set_booking_draft_v2.py
uvx ruff check src/secretaria/ai/tools.py src/secretaria/ai/graph.py src/secretaria/workers/shared/llm_context.py tests/test_set_booking_draft_v2.py tests/test_agent_menu_tools.py tests/test_agent_capability_cache.py
for f in src/secretaria/ai/tools.py src/secretaria/ai/graph.py src/secretaria/workers/shared/llm_context.py tests/test_agent_menu_tools.py tests/test_agent_capability_cache.py; do echo "$f $(uvx ruff format --diff $f 2>/dev/null | grep -c '^@@')"; done   # none may exceed its count before editing
git add src/secretaria/ai/tools.py src/secretaria/ai/graph.py src/secretaria/workers/shared/llm_context.py tests/test_set_booking_draft_v2.py tests/test_agent_menu_tools.py tests/test_agent_capability_cache.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(ai): set_booking_draft v2 behind the per-clinic switch

Same tool name, six optional fields (for_whom is only me/other, never a name; day and
time format-checked). Items that do not exist in the clinic are the resolver's to drop,
not tool errors. The sentinel serializes through BookingDraft (v1 raise sites included),
and the compiled-agent cache tells the two versions apart.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task B4: "ESTADO DA CONVERSA" sem marcador interno, com as etapas nomeadas, o "pra quem", o horário e a topologia

**Files:**
- Modify: `src/secretaria/services/llm_context.py` (arquivo inteiro)
- Modify: `tests/test_llm_context.py`

**Interfaces:**
- Consumes: `flow_router` (`STEP_*`, `ATTENDEE_NEXT_*`, `WHEN_FORMAT`, `professional_appointment_types`, `active_appointment_types`, `insurance_plan_names`, `_find_professional_by_id`), `attendee.ATTENDEE_SELF`.
- Produces: `build_conversation_state(conversation, tenant, professionals) -> str | None` (assinatura inalterada); `_STEP_LABELS` cobre TODO `flow_router.STEP_*`.

- [ ] **Step 1: Write the failing tests**

Em `tests/test_llm_context.py`: acrescentar aos imports `from secretaria.services import llm_context  # noqa: E402` (logo depois de `from secretaria.services import flow_router as fr  # noqa: E402`) e `from secretaria.services.attendee import ATTENDEE_SELF  # noqa: E402` (logo antes de `from secretaria.services.llm_context import build_conversation_state  # noqa: E402`). Ao fim do arquivo, acrescentar:

```python
# --------------------------------------------------------------------------
# TASK-030 P2: what the model sees about the workflow
# --------------------------------------------------------------------------


@pytest.mark.parametrize("step", fr.ATTENDEE_STEPS)
@pytest.mark.parametrize("marker", [fr.ATTENDEE_NEXT_BOOK, fr.ATTENDEE_NEXT_CATALOG])
def test_no_internal_marker_ever_reaches_the_model_text(step, marker):
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG, flow_step=step, flow_selected_type=marker
    )
    state = build_conversation_state(conv, _tenant(), [])
    prompt = secretary_system_prompt(_config(conversation_state=state))
    assert "__" not in state
    assert "attendee_next" not in prompt
    assert "Serviço já escolhido" not in state


def test_every_workflow_step_has_a_label():
    steps = {value for name, value in vars(fr).items() if name.startswith("STEP_")}
    assert steps <= set(llm_context._STEP_LABELS)


@pytest.mark.parametrize(
    "step",
    [
        fr.STEP_AWAITING_ATTENDEE_CHOICE,
        fr.STEP_MANAGE_ACTION,
        fr.STEP_MANAGE_SLOT,
        fr.STEP_DECLINE_REASON,
        fr.STEP_AWAITING_CATALOG_SERVICE,
    ],
)
def test_steps_that_used_to_read_as_the_menu_are_named(step):
    conv = _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step=step)
    assert "menu inicial" not in build_conversation_state(conv, _tenant(), [])


def test_the_pra_quem_question_is_named():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG, flow_step=fr.STEP_AWAITING_ATTENDEE_CHOICE
    )
    assert "Essa consulta é pra você?" in build_conversation_state(conv, _tenant(), [])


@pytest.mark.parametrize(
    "attendee, expected",
    [
        (None, "Pra quem é a consulta: ainda não respondido."),
        (ATTENDEE_SELF, "Pra quem é a consulta: para o próprio paciente."),
        ("Joaquim Segredo", "Pra quem é a consulta: OUTRA pessoa"),
    ],
)
def test_the_pra_quem_answer_is_spelled_out_without_the_name(attendee, expected):
    state = build_conversation_state(_conversation(flow_attendee_name=attendee), _tenant(), [])
    assert expected in state
    assert "Joaquim" not in state


def test_the_chosen_time_is_shown():
    conv = _conversation(flow_selected_day="2026-10-08", flow_selected_slot="2026-10-08T10:00")
    assert "Horário já escolhido: 08/10/2026 às 10:00" in build_conversation_state(
        conv, _tenant(), []
    )


def test_a_single_doctor_clinic_says_so_with_its_services():
    doctor = _professional("Dra. Ana", ["Consulta Ortopédica", "Retorno"])
    state = build_conversation_state(_conversation(), _tenant(), [doctor])
    assert "um só médico: Dra. Ana. Serviços: Consulta Ortopédica, Retorno." in state


def test_a_clinic_without_professionals_lists_its_services():
    state = build_conversation_state(_conversation(), _tenant(), [])
    assert "Serviços da clínica: Primeira Consulta." in state
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_llm_context.py -q`
Expected: os novos FAIL (o marcador aparece como "Serviço já escolhido: __attendee_next_book__"; etapas sem rótulo leem "no menu inicial"; sem linha de "pra quem", de horário e de topologia de um médico); os antigos PASS.

- [ ] **Step 3: Rewrite the module**

Substituir `src/secretaria/services/llm_context.py` inteiro por:

```python
"""Turn-scoped "ESTADO DA CONVERSA" block for the agent's system prompt.

The LLM used to arrive at a turn blind: a tap on "Outro"/"Não sei" reaches it as
the bare text, with no hint of which list the patient was on, which doctor or
service was already chosen, or what each doctor offers. This renders that state
as plain Portuguese for `ai/prompts.py::_format_conversation_state`.

PII rule: NO patient or attendee name goes in here. Only the message history is
pseudonymized before it leaves the process; this block rides in the SYSTEM
prompt, which is not. A booking for someone else is reported as a fact, never by name,
and convênio values are omitted (even catalog names can be verbatim legacy PII).

TASK-030 P2 (spec §4.7): every workflow step has a label (pra-quem, manage, the decline
reason and the legacy steps used to read as "no menu inicial"); the internal entry markers
`flow_selected_type` holds during the pra-quem steps (`__attendee_next_*__`) never reach
the model; the pra-quem answer, the chosen time and the clinic's topology are stated.
"""

from __future__ import annotations

from datetime import datetime

from secretaria.models import FlowState
from secretaria.services import flow_router as fr
from secretaria.services.attendee import ATTENDEE_SELF

_BOOKING = "do agendamento"
_MANAGE = "da consulta já marcada"

_STEP_LABELS: dict[str, str] = {
    # Pra quem - the first booking question.
    fr.STEP_AWAITING_ATTENDEE_CHOICE: f'a pergunta "Essa consulta é pra você?" {_BOOKING}',
    fr.STEP_AWAITING_ATTENDEE_NAME: f"o pedido do nome de quem vai ser atendido {_BOOKING}",
    fr.STEP_AWAITING_ATTENDEE_AUTH: (
        f"a frase de autorização para marcar para outra pessoa {_BOOKING}"
    ),
    # Médico e serviço.
    fr.STEP_AWAITING_PROFESSIONAL: f"a lista de médicos {_BOOKING}",
    fr.STEP_AWAITING_SERVICE_PROFESSIONAL: (
        f"a lista de médicos para o serviço escolhido {_BOOKING}"
    ),
    fr.STEP_PROFESSIONAL_HELP: f"a ajuda para escolher o médico {_BOOKING}",
    fr.STEP_PROFESSIONAL_HELP_FINAL: f"a ajuda para escolher o médico {_BOOKING}",
    fr.STEP_AWAITING_CATALOG_SERVICE: f"a lista de serviços da clínica {_BOOKING}",
    fr.STEP_AWAITING_SERVICE: f"a lista de serviços {_BOOKING}",
    fr.STEP_SERVICE_HELP: f"a ajuda para escolher o serviço {_BOOKING}",
    fr.STEP_SERVICE_HELP_FINAL: f"a ajuda para escolher o serviço {_BOOKING}",
    fr.STEP_AWAITING_SERVICE_CONFIRM: f"o detalhe do serviço escolhido {_BOOKING}",
    fr.STEP_AWAITING_INSURANCE: f"a pergunta de convênio {_BOOKING}",
    # Dia, horário, confirmação.
    fr.STEP_AWAITING_DAY: f"a escolha do dia {_BOOKING}",
    fr.STEP_AWAITING_DAY_RETRY: f"a escolha do dia {_BOOKING}",
    fr.STEP_AWAITING_DAY_ESCAPE: f"a escolha do dia {_BOOKING}",
    fr.STEP_AWAITING_SLOT: f"a escolha do horário {_BOOKING}",
    fr.STEP_AWAITING_CONFIRMATION: f"a confirmação {_BOOKING}",
    fr.STEP_AWAITING_RETRY: f"a escolha de outro horário {_BOOKING}",
    # Remarcar / cancelar.
    fr.STEP_MANAGE_PICK: f"a escolha {_MANAGE} para remarcar ou cancelar",
    fr.STEP_MANAGE_PICK_RESCHEDULE: f"a escolha {_MANAGE} a remarcar",
    fr.STEP_MANAGE_PICK_CANCEL: f"a escolha {_MANAGE} a cancelar",
    fr.STEP_MANAGE_ACTION: f"a pergunta remarcar ou cancelar {_MANAGE}",
    fr.STEP_MANAGE_CANCEL_CONFIRM: f"a confirmação do cancelamento {_MANAGE}",
    fr.STEP_MANAGE_DAY: f"a escolha do novo dia {_MANAGE}",
    fr.STEP_MANAGE_DAY_RETRY: f"a escolha do novo dia {_MANAGE}",
    fr.STEP_MANAGE_DAY_ESCAPE: f"a escolha do novo dia {_MANAGE}",
    fr.STEP_MANAGE_SLOT: f"a escolha do novo horário {_MANAGE}",
    fr.STEP_MANAGE_CONFIRM: f"a confirmação da remarcação {_MANAGE}",
    # Depois de o médico cancelar.
    fr.STEP_DECLINE_REASON: "a pergunta do motivo de não remarcar depois que o médico cancelou",
}


def _where(state: FlowState | None, step: str | None) -> str:
    if state == FlowState.LLM:
        return "conversa livre já em andamento (veja o histórico)"
    label = _STEP_LABELS.get(step or "")
    if label:
        return f"{label} — e escolheu 'Outro', 'Não sei' ou escreveu por conta própria"
    return "no menu inicial — tocou em 'Outro' ou escreveu livremente"


def _is_internal_marker(value: object) -> bool:
    """`flow_selected_type` holds `__attendee_next_*__` during the pra-quem steps: an entry
    marker, never a service the patient chose - and never something the model may read."""
    text = str(value or "")
    return text.startswith("__") and text.endswith("__")


def _pra_quem_line(attendee: str | None) -> str:
    if attendee is None:
        return "- Pra quem é a consulta: ainda não respondido."
    if attendee == ATTENDEE_SELF:
        return "- Pra quem é a consulta: para o próprio paciente."
    return "- Pra quem é a consulta: OUTRA pessoa (o nome dela é omitido de propósito)."


def _slot_text(slot: object) -> str | None:
    if not slot:
        return None
    try:
        return datetime.fromisoformat(str(slot)).strftime(fr.WHEN_FORMAT)
    except ValueError:
        return None


def _service_names(services: list[dict]) -> str:
    return ", ".join(str(s.get("name")) for s in services if s.get("name"))


def _topology_lines(tenant, professionals) -> list[str]:
    roster = list(professionals or [])
    if tenant is None:
        return []
    if len(roster) > 1:
        lines = ["- A clínica tem vários médicos. Serviços de cada médico:"]
        for professional in roster:
            names = _service_names(fr.professional_appointment_types(professional, tenant))
            lines.append(f"  - {professional.name}: {names or 'sem serviços cadastrados'}")
        return lines
    if len(roster) == 1:
        names = _service_names(fr.professional_appointment_types(roster[0], tenant))
        return [
            f"- A clínica atende com um só médico: {roster[0].name}. "
            f"Serviços: {names or 'sem serviços cadastrados'}."
        ]
    names = _service_names(fr.active_appointment_types(tenant))
    return [f"- Serviços da clínica: {names}."] if names else []


def build_conversation_state(conversation, tenant, professionals) -> str | None:
    """The state block, or None when there is no conversation snapshot."""
    if conversation is None:
        return None
    lines = [
        "- Onde o paciente estava: "
        + _where(
            getattr(conversation, "flow_state", None), getattr(conversation, "flow_step", None)
        )
    ]
    professional = fr._find_professional_by_id(
        professionals, getattr(conversation, "flow_selected_professional_id", None)
    )
    if professional is not None:
        lines.append(f"- Médico já escolhido: {professional.name}")
    selected_type = getattr(conversation, "flow_selected_type", None)
    if selected_type and not _is_internal_marker(selected_type):
        lines.append(f"- Serviço já escolhido: {selected_type}")
    lines.append(_pra_quem_line(getattr(conversation, "flow_attendee_name", None)))
    if getattr(conversation, "flow_selected_insurance", None):
        # Even catalog names can be verbatim legacy PII; SYSTEM is not scrubbed.
        lines.append("- Convênio já informado (valor omitido).")
    if getattr(conversation, "flow_selected_day", None):
        lines.append(f"- Dia já escolhido: {conversation.flow_selected_day}")
    when = _slot_text(getattr(conversation, "flow_selected_slot", None))
    if when:
        lines.append(f"- Horário já escolhido: {when}")
    plans = fr.insurance_plan_names(tenant) if tenant is not None else []
    if plans:
        lines.append(f"- Convênios cadastrados na clínica: {len(plans)} (valores omitidos).")
    lines.extend(_topology_lines(tenant, professionals))
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_llm_context.py tests/test_prompts.py -q`
Expected: PASS (os antigos inclusive: "lista de serviços", "Dra. Ana: Consulta Ortopédica", "OUTRA pessoa", "Dia já escolhido: 2026-10-05", "escolha do dia" e as provas de que nem nome nem convênio aparecem).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format src/secretaria/services/llm_context.py   # the whole file is this plan's
uvx ruff check src/secretaria/services/llm_context.py tests/test_llm_context.py
uvx ruff format --diff tests/test_llm_context.py | grep -c '^@@'   # must not increase
git add src/secretaria/services/llm_context.py tests/test_llm_context.py
git diff --cached --stat
git commit -F - <<'EOF'
fix(llm): the conversation state never shows an internal marker and names every step

The pra-quem entry marker no longer reads as "Serviço já escolhido". Pra-quem, manage,
decline and legacy steps get labels instead of "no menu inicial"; the block states
whether pra-quem was answered (never the name), the chosen time, and the clinic's
topology with its services.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task B5: Validação completa e documentação

**Files:**
- Create: `docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md`
- Modify: `docs/CHECKPOINT_agendar_para_terceiro.md`, `docs/CHECKPOINT_mvp_portal.md`, `docs/CHECKPOINT_llm_observabilidade_rede_de_seguranca.md` (uma linha de ponteiro cada, ao fim)

**Interfaces:**
- Consumes: tudo do P2a e do P2b.
- Produces: o CHECKPOINT que P3/P4/P5 leem antes de começar.

- [ ] **Step 1: Run the whole suite and compare with the P2a baseline**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest -q 2>&1 | tail -15`
Expected: `N' passed` com as MESMAS falhas pré-existentes anotadas no Step 0 da Task A1 (e nenhuma outra). Qualquer falha nova é regressão deste plano: investigue (skill `superpowers:systematic-debugging`) antes de seguir.

- [ ] **Step 2: Lint every file this plan touched**

```bash
git diff --name-only $(git merge-base HEAD main)..HEAD -- '*.py' | xargs uvx ruff check
```

Expected: `All checks passed!` (arquivos fora deste plano não entram nesta checagem).

- [ ] **Step 3: Write the CHECKPOINT**

Criar `docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md`:

```markdown
# CHECKPOINT — IA entra em qualquer etapa: rascunho v2, `flow_draft` e o resolvedor (TASK-030 P2)

**Estado:** BUILT — commits locais na branch da TASK-030 (P2a + P2b), **não pushado, não deployado**.
**Spec:** `docs/superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md` (§4.1–4.3, §4.7, §4.11, §7).
**Planos:** `docs/superpowers/plans/2026-10-02-ia-p2a-rascunho-coluna-e-resolvedor.md`, `docs/superpowers/plans/2026-10-02-ia-p2b-handbacks-ferramenta-e-estado.md`.

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

`Tenant.initial_flows["ai_draft_v2"]`, literal JSON `true`; desligado por padrão. Ligar/desligar é
mudança de dado feita por um operador (nenhuma tela do hub grava `initial_flows`):

    UPDATE tenants SET initial_flows = (COALESCE(initial_flows::jsonb, '{}'::jsonb)
        || '{"ai_draft_v2": true}'::jsonb)::json WHERE id = '<tenant>';
    UPDATE tenants SET initial_flows = (initial_flows::jsonb - 'ai_draft_v2')::json WHERE id = '<tenant>';

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
- O seletor de DIAS continua sem descontar reservas (byte a byte igual); a etapa de horário e os leitores
  da IA descontam.
- Ferramenta v2 só recusa FORMATO; existência na clínica é do resolvedor.

## 6. Pendências

P3: `_express_confirmation` (detalhes + cartão) e gerenciar até a confirmação. P4: `get_availability`
sobre `services/availability.py`. P5: prompt novo (a docstring da v2 é provisória) e remoção de
`start_guided_booking`/`select_professional_and_continue`.

## 7. Deploy (quando o dono pedir)

Migração `e7d3c1a9b5f2` primeiro (one-off da imagem nova), depois `secretaria_api` **e**
`secretaria-worker` juntos; `GET /build` com paridade `match`. Rollback: código velho nos dois, depois
`alembic downgrade c3a9e5f1d7b2`.
```

- [ ] **Step 4: Pointer lines**

Acrescentar ao fim de cada arquivo, numa linha própria:

- `docs/CHECKPOINT_agendar_para_terceiro.md`: `> TASK-030 P2: todo hand-back da IA que agenda pergunta "pra quem" quando ninguém respondeu, e o rascunho espera em `conversations.flow_draft` — ver `docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md`.`
- `docs/CHECKPOINT_mvp_portal.md`: `> TASK-030 P2: `set_booking_draft` ganhou a v2 (pra quem, dia, horário) atrás do interruptor `initial_flows.ai_draft_v2` e passa pelo resolvedor — ver `docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md`.`
- `docs/CHECKPOINT_llm_observabilidade_rede_de_seguranca.md`: `> TASK-030 P2: códigos novos no vocabulário de `conversation_handback_entered` e os eventos da continuação do rascunho — ver `docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md` §4.`

- [ ] **Step 5: Commit**

```bash
git add docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md docs/CHECKPOINT_agendar_para_terceiro.md docs/CHECKPOINT_mvp_portal.md docs/CHECKPOINT_llm_observabilidade_rede_de_seguranca.md
git diff --cached --stat
git commit -F - <<'EOF'
docs(checkpoint): TASK-030 P2 - draft v2, flow_draft and the resolver

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Cobertura da spec (P2b)

| Spec | Onde |
|---|---|
| §4.1 ferramenta `set_booking_draft(service, professional, insurance, for_whom, day, time)`; IA não escreve nome | B3 |
| §4.7 estado da conversa (rótulos, marcador, pra quem, horário, topologia; convênio omitido) | B4 |
| §4.11 interruptor por tenant, desligado por padrão; correção de segurança fora dele, inclusive hand-backs antigos | B1 (`ai_draft_v2_enabled`, `_pra_quem_unknown`), B2 |
| §5.1 "pra minha mãe" → nome + autorização; ninguém agendado como o próprio paciente sem responder | B1, B2 (+ P2a A4/A6) |
| §5.4 item inválido → pergunta daquela etapa | B1 (resolvedor) |
| §5.7 100% dos hand-backs com `conversation_handback_entered` | B1, B2 (`_land_handback`/`_fallback_to_menu`, um por saída) |
| §7 rascunho pula "pra quem"; snapshot velho; sem re-canonização; `_is_agent_sentinel`; `selection_only` sem alerta e sem convênio digitado; defeitos do estado | B1, B2, B4 |

## Deploy e liberação

Deploy nunca faz parte deste plano: só com pedido explícito do dono, a cada vez.

1. **Migração primeiro** — `alembic upgrade head` a partir da imagem NOVA, como one-off, com `secretaria_api` e `secretaria-worker` ainda no código velho (aplica `e7d3c1a9b5f2`: `ADD conversations.flow_draft JSON NULL`, só metadado no Postgres). Coluna nula e aditiva: o código velho não a enxerga (skill `frozen-contract-migration`, "widen before").
2. **API e worker juntos** — README: "Deploy both services, or neither". Os dois mapeiam `Conversation`; a lógica dos hand-backs roda no worker. Conferir `GET /build` dos dois: `deploy_parity` = `match`, mesmo `alembic_head` (`e7d3c1a9b5f2`).
3. **O que liga sozinho no deploy (todas as clínicas):** a pergunta "Essa consulta é pra você?" nos hand-backs com pra quem desconhecido (um toque a mais — decisão do dono, spec §4.11), o convênio digitado preservado, o alerta da clínica no rascunho só-seleção e a lista de horários escondendo o horário reservado numa clínica de um só médico.
4. **O que fica desligado:** a ferramenta v2 e o pouso de todo hand-back pelo resolvedor. Liga-se por clínica, por um operador, com a mudança de dado do CHECKPOINT §3 — primeiro na "Chrysostomo For Eyes", observando `conversation_handback_entered` (`landing_step`, `dropped`, `fallback`) e `booking_draft_resumed` nos logs; depois as demais; depois (plano futuro) remover o caminho v1. Recomendação: ligar só depois de P3, P4 e P5 deployados — sem o prompt do P5, o modelo não é ensinado a preencher `for_whom`/`day`/`time`.
5. **Rollback:** código velho nos DOIS serviços primeiro; só então `alembic downgrade c3a9e5f1d7b2`. Perde-se apenas um rascunho que estivesse esperando o "pra quem" — o paciente segue pelo fluxo de botões.
