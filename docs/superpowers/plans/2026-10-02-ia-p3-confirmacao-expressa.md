# IA entra em qualquer etapa — P3: confirmação expressa, rascunho que espera qualquer pergunta e consultas já marcadas — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nas clínicas com o interruptor `ai_draft_v2` ligado, um rascunho completo da IA leva o paciente direto ao cartão de confirmação — precedido de UMA mensagem com todos os detalhes da consulta —, o rascunho espera também as perguntas de convênio, médico e serviço, e a IA leva remarcar/cancelar até o cartão de confirmação; em todos os casos quem cria, cancela ou move a consulta continua sendo o toque do paciente em "Confirmar"/"Sim", pelo caminho de hoje.

**Architecture:** Tudo se apoia no resolvedor do P2 (`services/booking_draft.py::resolve_booking_draft`). P3 preenche o gancho `_express_confirmation` (cartão via o mesmo construtor do toque no horário, `flow_router._confirmation_card`), cria `services/booking_details.py` (o texto de detalhes, cortado pelo helper único de `core/whatsapp_limits.py`) e faz cada pouso do resolvedor que pula o cartão de serviço levar os detalhes, a menos que o paciente já os tenha visto (`details_already_shown`). O rascunho passa a esperar em `flow_draft` também nas perguntas de convênio/médico/serviço (`flow_router.DRAFT_WAIT_STEPS`), e a resposta é dobrada no rascunho (`draft_resolution._fold_answer`) antes de rodar o resolvedor de novo. Para consultas já marcadas, `services/manage_request.py` traz o pedido v2 (ação + qual consulta + novo dia/horário) e um resolvedor que pousa nos MESMOS passos dos botões, com a pré-checagem de sinal/limite antes de calcular qualquer dia.

**Tech Stack:** Python 3.12, LangChain tools, SQLAlchemy async, structlog, pytest + pytest-asyncio (`asyncio_mode = "auto"`, SQLite em memória), ruff.

**Spec:** `docs/superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md` — §4.4 (confirmação expressa), §4.5 (consultas já marcadas), §5 critérios 2, 3, 6 e 8, §7 (linha P3: `manage_slot`/`manage_confirm` fora da pré-checagem; reservas no contexto do portão), §9 (linha P3) e a **decisão do dono de 2026-10-03**: o rascunho é retomado depois de QUALQUER pergunta (convênio, médico, serviço), não só depois do "pra quem".

**Depende de:** P1 (`2026-10-02-ia-p1-registro-de-handbacks.md`), P2a (`2026-10-02-ia-p2a-rascunho-coluna-e-resolvedor.md`) e P2b (`2026-10-02-ia-p2b-handbacks-ferramenta-e-estado.md`) **já executados** neste worktree. Todo trecho marcado "(texto do P2a/P2b)" abaixo é o código que aqueles planos escrevem; se a formatação dele divergir um pouco, ancore pelo nome da função e pela primeira/última linha citadas — nunca invente outra âncora.

## Global Constraints

- Worktree `C:\TECH\BRAIN-worktrees\TASK-030\secretarIA` (branch da TASK-030). Tarefas **sequenciais**: P3 edita `flow_router.py`, `sentinels.py`, `booking_draft.py` e `draft_resolution.py`, os mesmos arquivos do P1/P2 — nunca dois agentes ao mesmo tempo.
- Testes rodam do **Git Bash**, na raiz do worktree: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest <arquivo> -q` (PowerShell não consegue setar variável vazia; nunca `pytest` solto).
- Nunca `ruff format .`. `uvx ruff format <arquivo>` só em arquivos **criados por este plano** ou criados (e deixados limpos) pelo P1/P2: `services/booking_details.py`, `services/manage_request.py`, `services/booking_draft.py`, `workers/shared/draft_resolution.py`, `workers/shared/handback_log.py`, e os arquivos de teste novos do P1/P2/P3. Sempre ANTES do `uvx ruff check`. Nos demais arquivos existentes: antes de editar, anote `uvx ruff format --diff <arquivo> | grep -c '^@@'`; depois de editar, a contagem não pode aumentar (em `b0ac5ee`: `flow_router.py` 23, `sentinels.py` 10, `deposit.py` 4, `ai/tools.py` 4, `ai/graph.py` 2, `llm_context.py` (workers) 9, `orchestrator.py` 2, `whatsapp_limits.py` 0, `tests/test_flow_cancel_money.py` 1, `tests/test_agent_menu_tools.py` 37 — P1/P2 podem ter mudado esses números; vale o que você medir antes da sua edição).
- A árvore está em CRLF (índice em LF). Antes de cada commit, `git diff --cached --stat` mostra só as linhas do passo; se um arquivo existente aparecer inteiro como alterado, `git restore --staged <arquivo>` e refaça a edição com âncoras menores.
- Camadas: api → workers → services/ai → models → core. `services/` nunca importa `workers/`. `workers/shared/*` não importa `workers.whatsapp`, `workers.portal` nem `workers.tasks` (`tests/test_workers_layering.py`).
- **Interruptor por clínica** `flow_router.ai_draft_v2_enabled(tenant)` (P2b; `initial_flows["ai_draft_v2"] is True`). Todo o comportamento visível novo do P3 fica atrás dele: confirmação expressa, mensagem de detalhes, médico no cartão, rascunho esperando convênio/médico/serviço, `manage_existing_appointment` v2 e a referência das consultas no prompt. Fora dele, e neutros para o caminho de botões: a pré-checagem de sinal passa a cobrir `manage_slot`/`manage_confirm` (Task 8) e `_service_detail_text`/`_manage_handle_slot` passam a usar helpers extraídos sem mudar um byte (Tasks 1 e 7).
- **"Confirmar" não muda:** `flow_router._handle_confirmation`, `_manage_cancel` e `_manage_reschedule` NÃO são editados (verificado na Task 9 com `git log -L`). Nada que a IA mande cria evento, `Appointment`, reserva, cancelamento ou remarcação: os pousos param SEMPRE num cartão (Confirmar/Cancelar ou Sim/Não).
- Nenhum ponto de envio novo: todas as bolhas saem por `_apply_flow_result` → `_dispatch_bubbles` (skill `channel-aware-dispatch` satisfeita por reuso). O cartão de limite de remarcação continua saindo por `_reply_sender` (`deposit.py`, inalterado).
- Texto ao paciente em português; código, comentários e mensagens de commit em inglês. A IA nunca carrega nome de terceiro; a mensagem de detalhes leva o nome do atendido (como o cartão de hoje) e por isso **nunca é logada**.
- Sem PII nem segredo em log: ids, NOMES de campo, CÓDIGOS de motivo, contagens, booleanos. Nunca serviço, convênio, nome, dia, horário ou o payload do sentinel.
- Códigos novos do evento `conversation_handback_entered` entram em `workers/shared/handback_log.py` no MESMO commit do primeiro ponto que os registra (convenção do P1).
- Cada commit termina com `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Commit local permitido; push e deploy só com pedido explícito do dono.

## Review Focus

Cada linha tem o teste que a fixa, na tarefa dona do código.

- **Detalhes maiores que o limite do WhatsApp** → o texto cabe em 4096, as linhas que identificam a consulta nunca são cortadas, a descrição cai primeiro e depois os requisitos viram 3 + "…" (T1 `test_a_text_longer_than_the_whatsapp_limit_drops_the_description_then_requirements`, `test_a_pathological_identity_block_is_hard_cut_to_the_limit`).
- **Serviço sem preço, descrição nem requisitos** → só as linhas que existem, nenhuma linha vazia (T1 `test_a_service_with_no_price_description_or_requirements_still_gets_its_lines`; T3 `test_a_full_draft_on_a_sole_clinic_lands_on_details_and_the_card`).
- **Clínica sem endereço** (ou com endereço sem rua, ou com unidades) → nenhuma linha "Endereço" (T1 `test_clinic_address_line_needs_a_street`; T4 `test_the_clinic_address_is_shown_only_when_stored`, `test_a_clinic_with_units_never_shows_the_clinic_address`).
- **Clínica de um só médico** → as reservas são procuradas pelo id dele (horário reservado vira lista do dia, não cartão) e o `Appointment` sai com `professional_id` dele (T3 `test_a_time_held_on_the_sole_doctors_agenda_lands_on_the_slot_list`; T4 `test_confirmar_on_whatsapp_books_through_todays_path`).
- **O horário é tomado entre a mensagem de detalhes e o "Confirmar"** → no Portal, cartão "horário tomado" de hoje; no WhatsApp, o comportamento de hoje (sem releitura — idêntico ao cartão dos botões, fixado e relatado ao dono) (T4 `test_a_slot_taken_before_confirmar_on_the_portal_is_offered_again`, `test_whatsapp_confirmar_does_not_reread_the_agenda_exactly_like_the_button_card`).
- **Portal × WhatsApp** → portão de código só no Portal: reserva + código, nada no Google; no WhatsApp, evento + `Appointment` (T4 `test_confirmar_on_the_portal_holds_the_slot_and_asks_for_the_code`, `test_confirmar_on_whatsapp_books_through_todays_path`).
- **Paciente toca "Confirmar" duas vezes sem editar nada** → um evento, um `Appointment`; o segundo toque cai no menu (T4 `test_tapping_confirmar_twice_books_once`).
- **Dia do rascunho é hoje e o horário já passou** → lista dos horários que restam hoje; nada sobrando → seletor de dias com o aviso (T3 `test_today_with_the_time_already_past_lands_on_todays_remaining_slots`, `test_today_with_nothing_left_lands_on_the_day_picker`; T7 `test_today_with_the_new_time_already_past`).
- **Remarcação recusada pela pré-checagem de sinal** → cartão manter/cancelar de hoje, conversa no menu, nenhum dia calculado, evento com `fallback=reschedule_limit` (T8 `test_a_reschedule_at_the_deposit_limit_gets_the_keep_or_cancel_answer`; `tests/test_flow_cancel_money.py` `test_a_reschedule_landing_past_the_day_picker_is_prechecked_too`).
- **A IA nomeia a consulta de outro paciente** → nunca é alvo; descartada (`unknown_appointment`) e nada é adivinhado (T7 `test_cancel_with_a_reference_that_is_not_the_patients_never_guesses`; T8 `test_another_patients_appointment_is_never_targeted`).
- **Rascunho expira enquanto o paciente responde o convênio** → a resposta segue o fluxo de botões, `flow_draft` apagado, `booking_draft_resume_skipped` (T5 `test_a_draft_expired_while_waiting_for_the_convenio_answer_continues_the_buttons`).
- **A resposta invalida um item guardado** (médico que não oferece o serviço do rascunho) → só o serviço cai, pergunta-se o serviço DAQUELE médico e o dia/horário continuam esperando (T5 `test_a_doctor_who_does_not_offer_the_parked_service_keeps_the_day_waiting`).
- **Paciente que já viu os detalhes** não recebe de novo; outro serviço/médico recebe (T3 `test_details_are_skipped_when_the_patient_already_saw_them`, `test_a_different_service_shows_the_details_again`).
- **O pouso expresso não agenda nada** (T3 `test_the_express_landing_books_nothing`; T4 `test_a_full_draft_lands_on_the_details_and_the_card_and_books_nothing`).
- **Cancelar pela IA sempre para no cartão** "Confirmar o cancelamento?" (T7 `test_cancel_never_cancels`; T8 `test_cancel_with_the_appointment_named_stops_at_the_cancel_card`).

## Decisões deste plano (mudar só se o dono discordar)

1. **"Detalhes já vistos"** (`booking_draft.details_already_shown`): verdadeiro só quando a conversa FRESCA está num passo depois do cartão de serviço (`STEP_AWAITING_SERVICE_CONFIRM`, os três passos do dia, `STEP_AWAITING_SLOT`, `STEP_AWAITING_CONFIRMATION`, `STEP_AWAITING_RETRY`) com o MESMO serviço (nome normalizado) e o MESMO médico. No caminho de botões só se chega a esses passos tocando "Sim" no cartão de serviço; no caminho da IA (interruptor ligado), todo pouso do resolvedor que pula esse cartão — seletor de dias, lista de horários, cartão expresso — leva a mensagem de detalhes. Assim a regra vale nos dois caminhos sem coluna nova. Estado LLM (sem passo) → detalhes de novo (duplicata inofensiva, preferível a faltar).
2. **Endereço:** `Tenant.address` (JSON já existente, gravável pelo hub; `{"line", "complement", "neighborhood", "city", "state", "postal_code"}`), formatado por `booking_details.clinic_address_line`. Só aparece com rua (`line`); NUNCA numa clínica com unidade ativa (`units`), porque lá o endereço certo é o da unidade, que esta marcação não escolhe (mesma decisão de `docs/CHECKPOINT_patient_calendar_link.md`, "sem location"). Nenhum armazenamento novo (isso é TASK-025/026).
3. **Identificador da consulta para a IA:** o início dela, em `AAAA-MM-DD HH:MM` no fuso da clínica (`manage_request.APPOINTMENT_REF_FORMAT`), mostrado como `(ref …)` em cada linha do bloco "consultas marcadas" (só com o interruptor). Casado SOMENTE entre as consultas futuras do paciente desta conversa (`load_upcoming_appointments(tenant, patient)`); referência que não casa, ou que casa com duas, é descartada (`unknown_appointment`) e nunca substituída por um palpite. Sem id/UUID no prompt (a ferramenta `list_patient_appointments` já evita ids pelo mesmo motivo).
4. **Rascunho espera convênio/médico/serviço** (decisão do dono de 2026-10-03), atrás do interruptor: quando o resolvedor pousa numa dessas perguntas e o rascunho pendente ainda tem serviço ou dia, ele fica em `flow_draft` (`DRAFT_WAIT_STEPS`). A resposta — convênio respondido, médico ou serviço tocado — marca `resume_draft`; o worker dobra a resposta no rascunho (médico/serviço tocados substituem os do rascunho; convênio fica gravado como o paciente respondeu) e roda o resolvedor de novo sobre dados frescos. Item que a resposta invalida cai sozinho e o fluxo pergunta dali. "Outro convênio" (pede o nome) continua esperando; "Não sei" (ajuda) e qualquer passo fora de `DRAFT_WAIT_STEPS` apagam o rascunho. Saída por tempo: o prazo de 30 min do P2 na hora de consumir; `/menu` e o "Não" do "quer continuar?" apagam.
5. **Médico no cartão de confirmação**: linha `Profissional: <nome>` entre o serviço e o "Paciente:", vinda de `_booking_professional` (o escolhido, ou o único da clínica), atrás do interruptor (fora dele o cartão fica byte a byte igual). O mesmo construtor (`_confirmation_card`) serve o toque no horário, a retomada e o cartão expresso.
6. **"Confirmar" é o de hoje, inclusive no buraco que já existe:** no WhatsApp `_handle_confirmation` não relê a agenda nem as reservas antes de criar o evento (o portão só arma no Portal). Um horário tomado no meio do caminho por outra reserva/evento vira consulta dupla — hoje, no cartão dos botões, igualzinho. P3 não muda isso (a spec manda o caminho de hoje), fixa a paridade em teste e relata ao dono como lacuna a decidir.
7. **Consultas já marcadas:** cancelar SEMPRE para no cartão "Confirmar o cancelamento?". Remarcar: sem dia → seletor de dias da remarcação; dia inválido → seletor com o aviso; horário livre (agenda fresca do DONO da consulta menos reservas) → cartão "Remarcar para:"; senão → lista do dia. A pré-checagem de sinal/limite roda ANTES de calcular qualquer dia (`deposit._reschedule_limit_hit`) e, como defesa em profundidade, `_RESCHEDULE_PRECHECK_STEPS` passa a cobrir `manage_slot`/`manage_confirm` para qualquer entrada.
8. **Sentinel v1 intacto:** um pedido de gerenciar só com a ação serializa como hoje (`__MANAGE_APPOINTMENT__:cancel`); só o v2 com consulta/dia/horário vira JSON. Com o interruptor desligado o worker ignora os campos v2 (comportamento de hoje).

## Interfaces (o que P4/P5 consomem — nomes exatos)

```python
# core/whatsapp_limits.py
MAX_TEXT_MESSAGE_CHARS: int = 4096

# services/booking_details.py
DETAILS_HEADER: str; REQUIREMENTS_HEADER: str; REQUIREMENTS_KEEP: int = 3
def price_text(price: Any) -> str | None
def clinic_address_line(address: Any) -> str | None
def booking_details_text(*, service: Mapping[str, Any], professional: Any | None, insurance: str | None,
                         attendee_name: str | None, address: str | None,
                         max_chars: int = MAX_TEXT_MESSAGE_CHARS) -> str

# services/flow_router.py
DRAFT_WAIT_STEPS: tuple[str, ...]          # ATTENDEE_STEPS + insurance, professional, service
def _recap_text(conversation, start, *, professional: Any | None = None) -> str
def _confirmation_recap(conversation, *, professional: Any | None = None) -> str | None
def _recap_professional(conversation, tenant, professionals) -> Any | None   # switch-gated
def _confirmation_card(conversation, start, *, professional: Any | None = None) -> ButtonBubble
def _handle_slot(conversation, body, *, professional: Any | None = None) -> FlowRouterResult
def _resume_parked_draft(conversation, result: FlowRouterResult) -> FlowRouterResult
def _manage_confirm_result(managing_id: UUID | None, appt: dict | None, start: datetime,
                           selected_day: str | None) -> FlowRouterResult

# services/booking_draft.py
DETAILS_SEEN_STEPS: tuple[str, ...]
def details_already_shown(conversation, *, service_name: str | None, professional_id: Any) -> bool
async def _express_confirmation(*, state, tenant, professional, service, slot_start, duration_minutes,
                                calendar, professionals, details: str | None = None) -> FlowRouterResult | None

# services/manage_request.py
ACTION_RESCHEDULE = "reschedule"; ACTION_CANCEL = "cancel"; ACTIONS
FIELD_ACTION/FIELD_APPOINTMENT/FIELD_DAY/FIELD_TIME; FIELD_NAMES
APPOINTMENT_REF_FORMAT = "%Y-%m-%d %H:%M"
def parse_appointment_ref(text: str) -> datetime            # naive clinic-local; ValueError
def appointment_ref(start_at: datetime, tz: tzinfo) -> str
@dataclass(frozen=True) class ManageRequest(action, appointment=None, day=None, time=None):
    to_payload() -> str; from_payload(raw) -> ManageRequest (classmethod); supplied_fields() -> tuple[str, ...]
def find_appointment(appointments, ref: datetime | None, tz) -> dict | None
def manage_target(request, appointments, tz) -> dict | None
DROP_UNKNOWN_APPOINTMENT = "unknown_appointment"; DROP_APPOINTMENT_NOT_CHOSEN = "appointment_not_chosen"
DROP_REASONS; FALLBACK_NO_APPOINTMENTS = "no_appointments"; FALLBACK_REASONS
@dataclass class ManageResolution(result, landing_step, accepted=(), dropped={}, fallback=None)
async def resolve_manage_request(request, *, tenant, appointments, professionals, calendar,
                                 conversation_id, tz, now) -> ManageResolution

# ai/tools.py
ManageAppointmentRequested(action, *, appointment=None, day=None, time=None)  # .request -> ManageRequest
manage_existing_appointment_v2   # model-facing name "manage_existing_appointment", metadata cache_variant "manage_v2"
TOOL_BLOCK_BAD_ACTION = "bad_action"; TOOL_BLOCK_BAD_APPOINTMENT = "bad_appointment"

# workers/shared/llm_context.py
def _appointment_context_text(future_appointments, tz_name, professional_names, appointment_types,
                              *, with_refs: bool = False) -> str | None   # "(ref AAAA-MM-DD HH:MM)"

# workers/shared/deposit.py
def _at_reschedule_limit(deposit, tenant) -> bool
async def _reschedule_limit_hit(tenant, appointment_id) -> tuple[int, int] | None

# workers/shared/draft_resolution.py
def _fold_answer(draft: BookingDraft, answered_step: str | None, result: FlowRouterResult) -> BookingDraft
# DraftContext.tenant_snapshot.clinic_address: str | None   (set by _load_draft_context)

# workers/shared/handback_log.py
DROP_UNKNOWN_APPOINTMENT, DROP_APPOINTMENT_NOT_CHOSEN, FALLBACK_RESCHEDULE_LIMIT = "reschedule_limit"
```

## File Structure

- Modify `src/secretaria/core/whatsapp_limits.py` — `MAX_TEXT_MESSAGE_CHARS` (T1).
- Create `src/secretaria/services/booking_details.py` — texto de detalhes, preço, endereço (T1).
- Modify `src/secretaria/services/flow_router.py` — `_service_detail_text` via `price_text` (T1); cartão com o médico (T2); `DRAFT_WAIT_STEPS`, `_carry_draft`, `_resume_parked_draft`, ganchos em `_catalog_step` (T5); `_manage_confirm_result` (T7).
- Modify `src/secretaria/services/booking_draft.py` — `details_already_shown`, `_land_day` com detalhes, `_express_confirmation` (T3); estacionar nas perguntas (T5).
- Modify `src/secretaria/workers/shared/draft_resolution.py` — `clinic_address` no contexto (T4); `_fold_answer`, `_resume_booking_draft` (T5).
- Create `src/secretaria/services/manage_request.py` — pedido v2 (T6) e resolvedor (T7).
- Modify `src/secretaria/ai/tools.py`, `src/secretaria/ai/graph.py`, `src/secretaria/workers/shared/llm_context.py`, `src/secretaria/workers/orchestrator.py` — ferramenta v2, sentinel, refs (T6).
- Modify `src/secretaria/workers/shared/deposit.py`, `src/secretaria/workers/shared/handback_log.py`, `src/secretaria/workers/shared/sentinels.py` — pré-checagem, códigos, handler v2 (T8).
- Tests: Create `tests/test_booking_details.py` (T1), `tests/test_confirmation_card.py` (T2), `tests/test_express_confirmation.py` (T3), `tests/test_express_confirmation_worker.py` (T4), `tests/test_draft_wait_questions.py` e `tests/test_booking_draft_resume_questions.py` (T5), `tests/test_manage_request.py` (T6), `tests/test_manage_request_resolver.py` (T7), `tests/test_manage_v2_handback.py` (T8). Modify `tests/test_booking_draft_resolver.py` (T3), `tests/test_handback_events.py` (T4), `tests/test_agent_menu_tools.py`, `tests/test_agent_capability_cache.py` (T6), `tests/test_flow_cancel_money.py` (T8).
- Docs (T9): Create `docs/CHECKPOINT_ia_confirmacao_expressa.md`; uma linha de ponteiro em `docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md` e em `docs/CHECKPOINT_llm_observabilidade_rede_de_seguranca.md`.

---

### Task 1: O texto de detalhes da consulta (`services/booking_details.py`)

**Files:**
- Modify: `src/secretaria/core/whatsapp_limits.py` (constante logo depois de `MAX_INTERACTIVE_BODY_CHARS = 1024`)
- Create: `src/secretaria/services/booking_details.py`
- Modify: `src/secretaria/services/flow_router.py` (import de `price_text`; `_service_detail_text`)
- Test: `tests/test_booking_details.py`

**Interfaces:**
- Consumes: `core.whatsapp_limits` (`EMOJI_DOCTOR`, `EMOJI_PERSON`, `EMOJI_SERVICE`, `TRUNCATION_MARK`, `truncate_plain`); `services.attendee.real_attendee_name`.
- Produces: `MAX_TEXT_MESSAGE_CHARS = 4096`; `price_text(price) -> str | None`; `clinic_address_line(address) -> str | None`; `booking_details_text(*, service, professional, insurance, attendee_name, address, max_chars=MAX_TEXT_MESSAGE_CHARS) -> str`; constantes `DETAILS_HEADER`, `REQUIREMENTS_HEADER`, `REQUIREMENTS_KEEP`.

- [ ] **Step 0: Linha de base (uma vez, antes de qualquer edição do P3)**

Run:
```bash
git rev-parse HEAD                                   # anote como P3_BASE (o topo do P2b)
BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest -q 2>&1 | tail -5
```
Expected: a linha final `N passed[, M failed]`. Anote `P3_BASE`, N, M e os nomes das falhas no relatório do worker — a Task 9 compara contra eles.

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_booking_details.py`:

```python
"""The booking details shown before an AI-landed confirmation (TASK-030 P3, spec §4.4.2)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from secretaria.core.whatsapp_limits import MAX_TEXT_MESSAGE_CHARS, TRUNCATION_MARK  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.attendee import ATTENDEE_SELF  # noqa: E402
from secretaria.services.booking_details import (  # noqa: E402
    DETAILS_HEADER,
    REQUIREMENTS_KEEP,
    booking_details_text,
    clinic_address_line,
    price_text,
)
from tests.test_flow_router import _tenant  # noqa: E402

ANA = SimpleNamespace(name="Dra. Ana", specialty="Cardiologia")
FULL = {
    "name": "Consulta",
    "price": "250",
    "description": "Consulta de rotina.",
    "long_description": "Avaliação completa.",
    "requirements": ["Jejum de 8 horas", "Trazer exames anteriores"],
}
ADDRESS = {
    "line": "Rua A, 1",
    "complement": "Sala 2",
    "neighborhood": "Centro",
    "city": "São Paulo",
    "state": "SP",
    "postal_code": "01000-000",
}


def test_every_item_the_skipped_steps_would_have_shown():
    text = booking_details_text(
        service=FULL,
        professional=ANA,
        insurance="Unimed",
        attendee_name="Maria da Silva",
        address=clinic_address_line(ADDRESS),
    )
    assert text == (
        "Confira os detalhes da sua consulta:\n"
        "\n"
        "🥼 Profissional: Dra. Ana — Cardiologia\n"
        "🏥 Serviço: Consulta — R$250\n"
        "Convênio: Unimed\n"
        "👤 Para: Maria da Silva\n"
        "Endereço: Rua A, 1, Sala 2, Centro, São Paulo/SP, CEP 01000-000\n"
        "\n"
        "Avaliação completa.\n"
        "\n"
        "O que levar / preparo:\n"
        "• Jejum de 8 horas\n"
        "• Trazer exames anteriores"
    )


def test_a_service_with_no_price_description_or_requirements_still_gets_its_lines():
    text = booking_details_text(
        service={"name": "Consulta"},
        professional=None,
        insurance=None,
        attendee_name=ATTENDEE_SELF,
        address=None,
    )
    assert text == "Confira os detalhes da sua consulta:\n\n🏥 Serviço: Consulta\n👤 Para: você"


def test_the_short_description_stands_in_for_a_missing_long_one():
    text = booking_details_text(
        service={"name": "Consulta", "description": "Consulta de rotina."},
        professional=None,
        insurance=None,
        attendee_name=None,
        address=None,
    )
    assert text == "Confira os detalhes da sua consulta:\n\n🏥 Serviço: Consulta\n\nConsulta de rotina."


def test_a_never_answered_pra_quem_prints_no_line():
    text = booking_details_text(
        service={"name": "Consulta"}, professional=None, insurance=None, attendee_name=None, address=None
    )
    assert "Para:" not in text


def test_requirements_stored_as_one_string_are_one_item():
    text = booking_details_text(
        service={"name": "Consulta", "requirements": "Jejum de 8 horas"},
        professional=None,
        insurance=None,
        attendee_name=None,
        address=None,
    )
    assert text.endswith("O que levar / preparo:\n• Jejum de 8 horas")


def test_a_text_longer_than_the_whatsapp_limit_drops_the_description_then_requirements():
    service = {
        "name": "Consulta",
        "price": "250",
        "long_description": "x" * 2000,
        "description": "Consulta de rotina.",
        "requirements": [f"Item {n:02d} " + "y" * 290 for n in range(20)],
    }
    text = booking_details_text(
        service=service,
        professional=ANA,
        insurance="Unimed",
        attendee_name=ATTENDEE_SELF,
        address="Rua A, 1, Recife",
    )
    assert len(text) <= MAX_TEXT_MESSAGE_CHARS
    # The lines that identify the booking are never the ones cut.
    for line in (
        "🥼 Profissional: Dra. Ana — Cardiologia",
        "🏥 Serviço: Consulta — R$250",
        "Convênio: Unimed",
        "👤 Para: você",
        "Endereço: Rua A, 1, Recife",
    ):
        assert line in text
    assert "x" * 50 not in text  # the long description went first
    assert "Consulta de rotina." not in text  # then the short one
    assert text.count("• ") == REQUIREMENTS_KEEP
    assert text.endswith(f"\n{TRUNCATION_MARK}")


def test_a_pathological_identity_block_is_hard_cut_to_the_limit():
    giant = SimpleNamespace(name="D" * 5000, specialty=None)
    text = booking_details_text(
        service={"name": "Consulta"}, professional=giant, insurance=None, attendee_name=None, address=None
    )
    assert len(text) == MAX_TEXT_MESSAGE_CHARS
    assert text.startswith(DETAILS_HEADER)


def test_a_smaller_budget_is_honoured():
    text = booking_details_text(
        service=FULL, professional=ANA, insurance=None, attendee_name=None, address=None, max_chars=120
    )
    assert len(text) <= 120


@pytest.mark.parametrize(
    "address, expected",
    [
        (ADDRESS, "Rua A, 1, Sala 2, Centro, São Paulo/SP, CEP 01000-000"),
        ({"line": "Rua A, 1", "city": "Recife"}, "Rua A, 1, Recife"),
        ({"line": "Rua A, 1", "state": "PE"}, "Rua A, 1, PE"),
        ({"line": "  ", "city": "Recife"}, None),
        ({"city": "Recife"}, None),
        ({"line": 7}, None),
        ("Rua A, 1", None),
        (None, None),
    ],
)
def test_clinic_address_line_needs_a_street(address, expected):
    assert clinic_address_line(address) == expected


@pytest.mark.parametrize(
    "price, expected",
    [("250", "R$250"), ("R$ 250", "R$ 250"), ("r$ 99", "r$ 99"), (250, "R$250"), (None, None), ("", None), (0, None)],
)
def test_price_text(price, expected):
    assert price_text(price) == expected


@pytest.mark.parametrize(
    "price, first_line",
    [
        ("R$ 250", "Primeira Consulta R$ 250"),
        ("250", "Primeira Consulta R$250"),
        (None, "Primeira Consulta"),
    ],
)
def test_the_service_card_spells_the_price_exactly_as_before(price, first_line):
    service = {"name": "Primeira Consulta", "price": price, "long_description": "Avaliação completa."}
    assert fr._service_detail_text(service, _tenant()) == (
        f"{first_line}\n\nAvaliação completa.\n\nDeseja agendar esse serviço?"
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_booking_details.py -q`
Expected: erro de coleta — `ImportError: cannot import name 'MAX_TEXT_MESSAGE_CHARS' from 'secretaria.core.whatsapp_limits'`.

- [ ] **Step 3: The constant**

Em `src/secretaria/core/whatsapp_limits.py`, logo depois da linha `MAX_INTERACTIVE_BODY_CHARS = 1024`, inserir:

```python

# A plain TEXT message caps at 4096 characters (Meta). The longest text the deterministic
# flow composes - the booking details before an AI express confirmation
# (services/booking_details.py) - is trimmed to it.
MAX_TEXT_MESSAGE_CHARS = 4096
```

- [ ] **Step 4: Write the module**

Criar `src/secretaria/services/booking_details.py`:

```python
"""The booking details a patient reads before confirming an AI-landed booking (TASK-030 P3).

When the AI's draft carries everything - who it is for, the convênio, the doctor, the
service, the day and the time - the patient skips the steps that would have shown them the
doctor, the service card and its price. Spec §4.4.2: they still receive everything those
steps would have shown, as ONE text message of its own right before the confirmation card:
the doctor (and specialty), the service with its price and description, what to bring / how
to prepare (the service's `requirements`), the convênio, who the booking is for, and the
clinic's address when the clinic has one stored (`Tenant.address`, read by
workers/shared/draft_resolution.py::_load_draft_context).

Pure: plain values in, one string out - no flow_router import (flow_router imports
`price_text` from here), no database, no logging. The text names a third party and the
patient's convênio, so it is sent and never logged.

Size: a text message caps at MAX_TEXT_MESSAGE_CHARS (core/whatsapp_limits.py), and a clinic
may write a 2000-character description and twenty 300-character requirements. The body is
trimmed in a fixed order until it fits - the long description gives way to the short one,
then to none, then the requirements keep their first REQUIREMENTS_KEEP items plus a "…"
line - and only then is it hard-cut with `truncate_plain` (display-only text, nothing ever
matches against it: skill third-party-text-limits). The lines that identify the booking
(doctor, service, price, convênio, who, address) come first and are never the ones trimmed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from secretaria.core.whatsapp_limits import (
    EMOJI_DOCTOR,
    EMOJI_PERSON,
    EMOJI_SERVICE,
    MAX_TEXT_MESSAGE_CHARS,
    TRUNCATION_MARK,
    truncate_plain,
)
from secretaria.services.attendee import real_attendee_name

DETAILS_HEADER = "Confira os detalhes da sua consulta:"
REQUIREMENTS_HEADER = "O que levar / preparo:"
# Requirements kept when the whole list does not fit (followed by a "…" line).
REQUIREMENTS_KEEP = 3
_PRICE_PREFIX = "R$"


def _clean(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def price_text(price: Any) -> str | None:
    """The price as the patient reads it ("R$250", "R$ 250"), or None when there is none.

    The ONE spelling the service card (`flow_router._service_detail_text`) and the booking
    details share: a value without the "R$" prefix gets it, one that has it is kept.
    """
    if not price:
        return None
    text = str(price).strip()
    if not text.upper().startswith(_PRICE_PREFIX):
        text = f"{_PRICE_PREFIX}{text}"
    return text


def clinic_address_line(address: Any) -> str | None:
    """One line out of `Tenant.address`, or None when there is no street.

    `Tenant.address` is `{"line", "complement", "neighborhood", "city", "state",
    "postal_code"}`, every key optional (schemas/config.py::TenantAddress). The street
    (`line`) is required: a city alone tells nobody where to go, and a plausible but
    incomplete address is worse than none.
    """
    if not isinstance(address, Mapping):
        return None

    def part(key: str) -> str | None:
        value = address.get(key)
        return _clean(value) if isinstance(value, str) else None

    street = part("line")
    if street is None:
        return None
    city, state = part("city"), part("state")
    city_state = f"{city}/{state}" if city and state else city or state
    postal_code = part("postal_code")
    pieces = (
        street,
        part("complement"),
        part("neighborhood"),
        city_state,
        f"CEP {postal_code}" if postal_code else None,
    )
    return ", ".join(piece for piece in pieces if piece)


def _identity_lines(
    service: Mapping[str, Any],
    professional: Any | None,
    insurance: str | None,
    attendee_name: str | None,
    address: str | None,
) -> list[str]:
    lines: list[str] = []
    doctor = _clean(getattr(professional, "name", None)) if professional is not None else None
    if doctor:
        specialty = _clean(getattr(professional, "specialty", None))
        lines.append(
            f"{EMOJI_DOCTOR} Profissional: {doctor}" + (f" — {specialty}" if specialty else "")
        )
    name = _clean(service.get("name")) or "Consulta"
    price = price_text(service.get("price"))
    lines.append(f"{EMOJI_SERVICE} Serviço: {name}" + (f" — {price}" if price else ""))
    plan = _clean(insurance)
    if plan:
        lines.append(f"Convênio: {plan}")
    if attendee_name is not None:
        # "" (ATTENDEE_SELF) is the patient themself; None was never answered: no line.
        lines.append(f"{EMOJI_PERSON} Para: {real_attendee_name(attendee_name) or 'você'}")
    where = _clean(address)
    if where:
        lines.append(f"Endereço: {where}")
    return lines


def _requirements(service: Mapping[str, Any]) -> list[str]:
    raw = service.get("requirements") or []
    if isinstance(raw, str):
        raw = [raw]
    return [text for text in (_clean(item) for item in raw) if text]


def _assemble(
    identity: list[str], description: str | None, requirements: list[str], *, keep: int | None
) -> str:
    blocks = [DETAILS_HEADER, "\n".join(identity)]
    if description:
        blocks.append(description)
    if requirements:
        shown = requirements if keep is None else requirements[:keep]
        bullets = "\n".join(f"• {item}" for item in shown)
        if keep is not None and len(requirements) > keep:
            bullets += f"\n{TRUNCATION_MARK}"
        blocks.append(f"{REQUIREMENTS_HEADER}\n{bullets}")
    return "\n\n".join(blocks)


def booking_details_text(
    *,
    service: Mapping[str, Any],
    professional: Any | None,
    insurance: str | None,
    attendee_name: str | None,
    address: str | None,
    max_chars: int = MAX_TEXT_MESSAGE_CHARS,
) -> str:
    """The details message: everything the skipped steps would have shown, within `max_chars`.

    `service` is the catalog dict of the booking (name, price, description,
    long_description, requirements); `professional` the booking's doctor (name, specialty)
    or None; `insurance` the recorded convênio; `attendee_name` the pra-quem answer
    (ATTENDEE_SELF, a third party's name, or None = never answered); `address` an already
    formatted line (`clinic_address_line`) or None. Lines with nothing to say are left out.
    """
    identity = _identity_lines(service, professional, insurance, attendee_name, address)
    requirements = _requirements(service)
    long_text = _clean(service.get("long_description"))
    short_text = _clean(service.get("description"))
    descriptions: list[str | None] = [long_text or short_text]
    if long_text and short_text and short_text != long_text:
        descriptions.append(short_text)
    descriptions.append(None)
    for description in descriptions:
        body = _assemble(identity, description, requirements, keep=None)
        if len(body) <= max_chars:
            return body
    body = _assemble(identity, None, requirements, keep=REQUIREMENTS_KEEP)
    if len(body) <= max_chars:
        return body
    return truncate_plain(body, max_chars)
```

- [ ] **Step 5: The service card spells the price through the same helper**

Em `src/secretaria/services/flow_router.py`:

1. Inserir, imediatamente ANTES da linha que começa com `from secretaria.services.booking_hold import` (ordem do ruff: `availability` < `booking_details` < `booking_hold`):

```python
from secretaria.services.booking_details import price_text
```

2. Trocar a função `_service_detail_text` inteira por (saída byte a byte igual; o teste do Step 1 fixa os três formatos de preço):

```python
def _service_detail_text(service: dict, tenant: Tenant) -> str:
    name = str(service.get("name", "Consulta"))
    price = price_text(service.get("price"))
    long_description = service.get("long_description") or service.get("description")
    parts = [f"{name} {price}" if price else name]
    if long_description:
        parts.append(str(long_description))
    parts.append("Deseja agendar esse serviço?")
    return "\n\n".join(parts)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_booking_details.py tests/test_flow_router.py tests/test_flow_router_multiprofessional.py tests/test_workers_layering.py -q`
Expected: PASS (os 26 novos, contando os parametrizados, e os do cartão de serviço inalterados).

- [ ] **Step 7: Lint and commit**

```bash
uvx ruff format src/secretaria/services/booking_details.py tests/test_booking_details.py
uvx ruff check --fix --select I tests/test_booking_details.py
uvx ruff check src/secretaria/services/booking_details.py src/secretaria/services/flow_router.py src/secretaria/core/whatsapp_limits.py tests/test_booking_details.py
for f in src/secretaria/services/flow_router.py src/secretaria/core/whatsapp_limits.py; do echo "$f $(uvx ruff format --diff $f 2>/dev/null | grep -c '^@@')"; done   # none may exceed its count before editing
git add src/secretaria/core/whatsapp_limits.py src/secretaria/services/booking_details.py src/secretaria/services/flow_router.py tests/test_booking_details.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(flow): the booking details a patient reads before an AI-landed confirmation

One text message with everything the skipped steps would have shown: doctor and specialty,
service with price and description, what to bring, convênio, who it is for and the
clinic's address when stored. Trimmed to WhatsApp's 4096-character text cap in a fixed
order, identity lines never cut. The service card now spells its price through the same
helper, byte for byte as before.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 2: O cartão de confirmação nomeia o médico (atrás do interruptor)

**Files:**
- Modify: `src/secretaria/services/flow_router.py` (import de `MAX_INTERACTIVE_BODY_CHARS`; `_attendee_line`/`_recap_text`/`_confirmation_recap` + novos `_professional_line`, `_recap_professional`, `_confirmation_card`; `_handle_slot`; o ramo `STEP_AWAITING_SLOT` de `_catalog_step`; o ramo `STEP_AWAITING_CONFIRMATION` de `_resume_bubbles`)
- Test: `tests/test_confirmation_card.py`

**Interfaces:**
- Consumes: `ai_draft_v2_enabled` (P2b), `_booking_professional` (existente).
- Produces: `_recap_text(conversation, start, *, professional=None) -> str`; `_confirmation_recap(conversation, *, professional=None) -> str | None`; `_recap_professional(conversation, tenant, professionals) -> Any | None`; `_confirmation_card(conversation, start, *, professional=None) -> ButtonBubble`; `_handle_slot(conversation, body, *, professional=None) -> FlowRouterResult`.

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_confirmation_card.py`:

```python
"""The confirmation card names the doctor on switched-on clinics (TASK-030 P3, spec §4.4.2)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import datetime  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

from secretaria.ai.formatter import ButtonBubble  # noqa: E402
from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from tests.test_flow_router import _conversation, _FakeCalendar, _tenant  # noqa: E402

TAP = "🗓️ 08:00 (2026-10-05T08:00)"


def _doctor(name):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=None,
        about=None,
        context_doctor_message=None,
        appointment_types=None,
        business_hours=None,
    )


ANA = _doctor("Dra. Ana")
BETO = _doctor("Dr. Beto")


def _switched():
    tenant = _tenant()
    tenant.initial_flows = {**tenant.initial_flows, "ai_draft_v2": True}
    return tenant


def _at_slot(**kw):
    return _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SLOT,
        flow_selected_type="Primeira Consulta",
        flow_selected_day="2026-10-05",
        **kw,
    )


async def test_without_the_switch_the_card_is_byte_for_byte_todays():
    res = await fr.route(
        _at_slot(flow_selected_professional_id=ANA.id),
        _tenant(),
        _FakeCalendar(),
        TAP,
        professionals=[ANA, BETO],
    )
    assert res.flow_step == fr.STEP_AWAITING_CONFIRMATION
    assert res.bubbles[0].body == "Primeira Consulta\n05/10/2026 às 08:00"


async def test_with_the_switch_the_card_names_the_chosen_doctor():
    res = await fr.route(
        _at_slot(flow_selected_professional_id=ANA.id),
        _switched(),
        _FakeCalendar(),
        TAP,
        professionals=[ANA, BETO],
    )
    assert res.bubbles[0].body == "Primeira Consulta\nProfissional: Dra. Ana\n05/10/2026 às 08:00"
    assert res.flow_selected_slot == "2026-10-05T08:00"


async def test_a_single_professional_clinic_names_its_only_doctor():
    res = await fr.route(_at_slot(), _switched(), _FakeCalendar(), TAP, professionals=[ANA])
    assert res.bubbles[0].body == "Primeira Consulta\nProfissional: Dra. Ana\n05/10/2026 às 08:00"


async def test_the_doctor_comes_before_the_patient_line():
    res = await fr.route(
        _at_slot(flow_selected_professional_id=ANA.id, flow_attendee_name="Maria da Silva"),
        _switched(),
        _FakeCalendar(),
        TAP,
        professionals=[ANA, BETO],
    )
    assert res.bubbles[0].body == (
        "Primeira Consulta\nProfissional: Dra. Ana\nPaciente: Maria da Silva\n05/10/2026 às 08:00"
    )


async def test_a_clinic_without_professionals_has_no_doctor_line():
    res = await fr.route(_at_slot(), _switched(), _FakeCalendar(), TAP, professionals=[])
    assert res.bubbles[0].body == "Primeira Consulta\n05/10/2026 às 08:00"


async def test_the_resumed_card_reads_like_the_tapped_one():
    conversation = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_CONFIRMATION,
        flow_selected_type="Primeira Consulta",
        flow_selected_day="2026-10-05",
        flow_selected_slot="2026-10-05T08:00",
        flow_selected_professional_id=ANA.id,
    )
    res = await fr.resume_bubbles(conversation, _switched(), None, professionals=[ANA, BETO])
    assert res.bubbles[0].body == "Primeira Consulta\nProfissional: Dra. Ana\n05/10/2026 às 08:00"


def test_the_card_body_never_exceeds_the_interactive_limit():
    conversation = SimpleNamespace(flow_selected_type="X" * 2000, flow_attendee_name=None)
    card = fr._confirmation_card(conversation, datetime(2026, 10, 5, 8, 0), professional=ANA)
    assert isinstance(card, ButtonBubble)
    assert len(card.body) == MAX_INTERACTIVE_BODY_CHARS
    assert (card.confirm_label, card.cancel_label) == (fr.LABEL_CONFIRM, fr.LABEL_CANCEL)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_confirmation_card.py -q`
Expected: o primeiro e o de "sem profissionais" PASS; os quatro com médico FAIL (`AssertionError` — o corpo não tem "Profissional:"); `test_the_card_body_never_exceeds_the_interactive_limit` FAIL (`AttributeError: module 'secretaria.services.flow_router' has no attribute '_confirmation_card'`).

- [ ] **Step 3: Implement**

Em `src/secretaria/services/flow_router.py`:

1. No import de `secretaria.core.whatsapp_limits`, trocar

```python
    EMOJI_SERVICE,
    MAX_LIST_ROW_DESCRIPTION_CHARS,
```

por

```python
    EMOJI_SERVICE,
    MAX_INTERACTIVE_BODY_CHARS,
    MAX_LIST_ROW_DESCRIPTION_CHARS,
```

2. Trocar o bloco das três funções `_attendee_line`, `_recap_text` e `_confirmation_recap` (do `def _attendee_line(` até o `return _recap_text(conversation, start)` de `_confirmation_recap`) por:

```python
def _attendee_line(conversation: Conversation) -> str:
    """"Paciente: <name>" plus a newline on a booking for someone else, else ""."""
    name = _attendee_name(conversation)
    return f"Paciente: {name}\n" if name else ""


def _professional_line(professional: Any | None) -> str:
    """"Profissional: <name>" plus a newline, or "" when there is nobody to name."""
    name = str(getattr(professional, "name", "") or "").strip() if professional is not None else ""
    return f"Profissional: {name}\n" if name else ""


def _recap_text(
    conversation: Conversation, start: datetime, *, professional: Any | None = None
) -> str:
    """The recap card body; unchanged (byte for byte) when there is no attendee and no
    `professional` (TASK-030 P3: whom the booking is with, see `_recap_professional`)."""
    return (
        f"{conversation.flow_selected_type or 'Consulta'}\n"
        f"{_professional_line(professional)}"
        f"{_attendee_line(conversation)}"
        f"{start.strftime('%d/%m/%Y às %H:%M')}"
    )


def _confirmation_recap(
    conversation: Conversation, *, professional: Any | None = None
) -> str | None:
    """Rebuild the confirmation recap text from the stored slot, or None."""
    slot = conversation.flow_selected_slot
    if not slot:
        return None
    try:
        start = datetime.fromisoformat(slot)
    except ValueError:
        return None
    return _recap_text(conversation, start, professional=professional)


def _recap_professional(
    conversation: Conversation, tenant: Tenant, professionals: list | None
) -> Any | None:
    """Whom the confirmation card names (TASK-030 P3, spec §4.4.2: "agora com o médico").

    The booking's own professional (`_booking_professional`: the doctor the patient picked,
    or a single-professional clinic's only one) on clinics with the AI draft v2 switch;
    None elsewhere, so the card stays byte for byte today's until the clinic is switched on.
    """
    if not ai_draft_v2_enabled(tenant):
        return None
    return _booking_professional(conversation, professionals)


def _confirmation_card(
    conversation: Conversation, start: datetime, *, professional: Any | None = None
) -> ButtonBubble:
    """The Confirmar/Cancelar card - the ONE builder for the slot tap and the AI's express
    confirmation (services/booking_draft.py::_express_confirmation), so both read alike.

    "Confirmar" on it is routed by `_handle_confirmation`, whichever path drew it.
    """
    return ButtonBubble(
        body=truncate_plain(
            _recap_text(conversation, start, professional=professional),
            MAX_INTERACTIVE_BODY_CHARS,
        ),
        confirm_label=LABEL_CONFIRM,
        cancel_label=LABEL_CANCEL,
    )
```

3. Trocar a função `_handle_slot` inteira por:

```python
def _handle_slot(
    conversation: Conversation, body: str, *, professional: Any | None = None
) -> FlowRouterResult:
    start = _slot_iso_from_body(body)
    if start is None:
        return _preserve(conversation, "delegate_llm")
    slot_iso = start.replace(tzinfo=None).isoformat(timespec="minutes")
    return FlowRouterResult(
        action="reply",
        bubbles=[_confirmation_card(conversation, start, professional=professional)],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_CONFIRMATION,
        flow_selected_type=conversation.flow_selected_type,
        flow_selected_day=conversation.flow_selected_day,
        flow_selected_slot=slot_iso,
        flow_selected_professional_id=_selected_professional_id(conversation),
        flow_selected_insurance=_selected_insurance(conversation),
    )
```

4. Em `_catalog_step`, trocar

```python
        return control if control is not None else _handle_slot(conversation, body)
```

por

```python
        if control is not None:
            return control
        return _handle_slot(
            conversation,
            body,
            professional=_recap_professional(conversation, tenant, professionals),
        )
```

5. Em `_resume_bubbles`, trocar

```python
        recap = _confirmation_recap(conversation)
```

por

```python
        recap = _confirmation_recap(
            conversation, professional=_recap_professional(conversation, tenant, professionals)
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_confirmation_card.py tests/test_flow_router.py tests/test_flow_day_picker.py tests/test_flow_router_multiprofessional.py tests/test_reactivation.py tests/test_attendee_booking.py tests/test_convenio_catalogo_flow.py tests/test_booking_code_gate.py -q`
Expected: PASS (os 7 novos; todos os cartões antigos — interruptor desligado — byte a byte iguais).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format tests/test_confirmation_card.py
uvx ruff check --fix --select I tests/test_confirmation_card.py
uvx ruff check src/secretaria/services/flow_router.py tests/test_confirmation_card.py
uvx ruff format --diff src/secretaria/services/flow_router.py | grep -c '^@@'   # must not exceed the count before editing
git add src/secretaria/services/flow_router.py tests/test_confirmation_card.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(flow): the confirmation card names the doctor on switched-on clinics

One builder (_confirmation_card) for the slot tap, the resume and the AI's express
confirmation; "Profissional: <name>" sits between the service and the patient lines when
the clinic has the AI draft v2 switch. Off, the card is byte for byte today's.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 3: A confirmação expressa no resolvedor (detalhes + cartão; detalhes já vistos)

**Files:**
- Modify: `src/secretaria/services/booking_draft.py` (imports; a chamada de `_land_day` dentro de `_resolve`; tudo de `async def _land_day(` até o fim do arquivo)
- Modify: `tests/test_booking_draft_resolver.py` (troca `test_p2_has_no_express_confirmation_yet`)
- Test: `tests/test_express_confirmation.py`

**Interfaces:**
- Consumes: T1 (`booking_details_text`), T2 (`fr._confirmation_card`), P2a (`_land_day`, `_Checked`, `_booking_services`, `_recorded_service`, `DraftResolution`, `free_slots_for_day`, `fr._hold_owner`), P2b (`fr.ai_draft_v2_enabled`).
- Produces: `DETAILS_SEEN_STEPS`; `details_already_shown(conversation, *, service_name, professional_id) -> bool`; `_express_confirmation(..., details: str | None = None)` implementado; `_land_day(..., conversation, ...)`. Todo pouso do resolvedor em seletor de dias / lista de horários / cartão expresso leva os detalhes (interruptor ligado, ainda não vistos).

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_express_confirmation.py`:

```python
"""Express confirmation: a complete AI draft lands on the details and the card (TASK-030 P3).

Resolver level (services/booking_draft.py), pure: fake agenda, no database. Helpers come
from tests/test_booking_draft_resolver.py (P2a).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import datetime as dt  # noqa: E402
from datetime import datetime  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from secretaria.ai.formatter import ButtonBubble, TextBubble  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services import booking_draft as bd, flow_router as fr  # noqa: E402
from secretaria.services.booking_draft import BookingDraft, resolve_booking_draft  # noqa: E402
from tests.test_booking_draft_resolver import (  # noqa: E402
    _HELD_1000,
    DAY,
    TZ,
    _Cal,
    _conv,
    _doctor,
    _HoldGate,
    _resolve,
    _sole,
    _source,
    _tenant,
)

TEN = dt.time(10, 0)
ADDRESS = "Rua A, 1, Centro, São Paulo/SP, CEP 01000-000"
FULL = {
    "name": "Consulta",
    "duration_min": 30,
    "is_active": True,
    "sort_order": 0,
    "price": "250",
    "long_description": "Avaliação completa.",
    "requirements": ["Jejum de 8 horas", "Trazer exames anteriores"],
}


def _on(**kw):
    return _tenant(initial_flows={"ai_draft_v2": True}, **kw)


def _kinds(resolution):
    return [type(bubble).__name__ for bubble in resolution.result.bubbles]


async def test_a_full_draft_on_a_sole_clinic_lands_on_details_and_the_card():
    res = await _resolve(BookingDraft(service="Consulta", day=DAY, time=TEN), tenant=_on(), pros=[_sole()])
    assert res.landing_step == fr.STEP_AWAITING_CONFIRMATION
    details, card = res.result.bubbles
    assert isinstance(details, TextBubble) and isinstance(card, ButtonBubble)
    assert details.body == (
        "Confira os detalhes da sua consulta:\n\n"
        "🥼 Profissional: Dra. Única\n"
        "🏥 Serviço: Consulta\n"
        "👤 Para: você"
    )
    assert card.body == "Consulta\nProfissional: Dra. Única\n08/10/2026 às 10:00"
    assert (res.result.flow_selected_day, res.result.flow_selected_slot) == (
        "2026-10-08",
        "2026-10-08T10:00",
    )
    assert res.result.flow_selected_type == "Consulta"
    assert res.accepted == ("service", "day", "time")
    assert res.fallback is None


async def test_the_card_and_the_stored_slot_are_the_same_instant():
    """skill date-derived-ui-labels: the label the patient reads and the slot `Confirmar`
    books derive from ONE value."""
    res = await _resolve(BookingDraft(service="Consulta", day=DAY, time=TEN), tenant=_on(), pros=[_sole()])
    stored = datetime.fromisoformat(res.result.flow_selected_slot)
    assert stored.strftime(fr.WHEN_FORMAT) in res.result.bubbles[-1].body


async def test_every_detail_of_a_multi_doctor_booking_for_someone_else():
    ana = _doctor("Dra. Ana", [])
    ana.specialty = "Cardiologia"
    ana.appointment_types = [dict(FULL)]
    beto = _doctor("Dr. Beto", ["Retorno"])
    asked: list = []
    tenant = _on(
        collect_insurance=True,
        insurance_mode="shared",
        insurances=["Unimed"],
        clinic_address=ADDRESS,
    )
    res = await resolve_booking_draft(
        BookingDraft(
            service="Consulta",
            professional_id=ana.id,
            insurance="unimed",
            attendee="other",
            day=DAY,
            time=TEN,
        ),
        conversation=_conv(flow_attendee_name="Maria da Silva"),
        tenant=tenant,
        professional_rows=[ana, beto],
        service_catalog=[],
        tenant_insurance=None,
        calendar=_source(_Cal(), asked),
        now=dt.datetime(2026, 10, 5, 9, 0, tzinfo=TZ),
    )
    details, card = res.result.bubbles
    assert details.body == (
        "Confira os detalhes da sua consulta:\n\n"
        "🥼 Profissional: Dra. Ana — Cardiologia\n"
        "🏥 Serviço: Consulta — R$250\n"
        "Convênio: Unimed\n"
        "👤 Para: Maria da Silva\n"
        f"Endereço: {ADDRESS}\n\n"
        "Avaliação completa.\n\n"
        "O que levar / preparo:\n"
        "• Jejum de 8 horas\n"
        "• Trazer exames anteriores"
    )
    assert card.body == (
        "Consulta\nProfissional: Dra. Ana\nPaciente: Maria da Silva\n08/10/2026 às 10:00"
    )
    assert res.result.flow_selected_professional_id == ana.id
    assert res.result.flow_selected_insurance == "Unimed"
    assert res.result.flow_attendee_name == "Maria da Silva"
    assert res.accepted == ("service", "professional", "insurance", "for_whom", "day", "time")
    assert asked == [ana]


async def test_the_express_landing_books_nothing():
    res = await _resolve(BookingDraft(service="Consulta", day=DAY, time=TEN), tenant=_on(), pros=[_sole()])
    assert res.result.appointment is None
    assert res.result.booking_hold is None
    assert res.result.appointment_cancel_id is None
    assert res.result.appointment_reschedule is None


async def test_details_are_skipped_when_the_patient_already_saw_them():
    seen = _conv(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SLOT,
        flow_selected_type="Consulta",
        flow_selected_day="2026-10-08",
    )
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY, time=TEN), conv=seen, tenant=_on(), pros=[_sole()]
    )
    assert _kinds(res) == ["ButtonBubble"]
    assert res.landing_step == fr.STEP_AWAITING_CONFIRMATION


async def test_a_different_service_shows_the_details_again():
    seen = _conv(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SLOT,
        flow_selected_type="Retorno",
    )
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY, time=TEN), conv=seen, tenant=_on(), pros=[_sole()]
    )
    assert _kinds(res) == ["TextBubble", "ButtonBubble"]


async def test_without_the_switch_the_patient_lands_on_the_slot_list_as_in_p2():
    res = await _resolve(BookingDraft(service="Consulta", day=DAY, time=TEN), pros=[_sole()])
    assert res.landing_step == fr.STEP_AWAITING_SLOT
    assert _kinds(res) == ["SlotsBubble"]


async def test_a_time_held_on_the_sole_doctors_agenda_lands_on_the_slot_list():
    sole = _sole()
    gate = _HoldGate({sole.id: [_HELD_1000]})
    with fr.booking_gate_scope(gate):
        res = await _resolve(BookingDraft(service="Consulta", day=DAY, time=TEN), tenant=_on(), pros=[sole])
    assert res.landing_step == fr.STEP_AWAITING_SLOT
    assert res.dropped == {"time": "no_free_slot"}
    assert _kinds(res) == ["TextBubble", "SlotsBubble"]
    rows = [row[0] for row in res.result.bubbles[1].rows]
    assert "slot|2026-10-08T10:00" not in rows
    assert set(gate.asked) == {sole.id}


async def test_a_day_picker_landing_opens_with_the_details():
    res = await _resolve(BookingDraft(service="Consulta"), tenant=_on(), pros=[_sole()])
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert _kinds(res) == ["TextBubble", "SlotsBubble"]


async def _resolve_at(draft, *, now, cal, pros):
    return await resolve_booking_draft(
        draft,
        conversation=_conv(),
        tenant=_on(),
        professional_rows=list(pros),
        service_catalog=[],
        tenant_insurance=None,
        calendar=_source(cal),
        now=now,
    )


async def test_today_with_the_time_already_past_lands_on_todays_remaining_slots():
    # The calendar's own walk never offers a slot that already started
    # (CalendarService._walk_free_slots, `cursor < now`); the fake mirrors that.
    afternoon = dt.datetime(2026, 10, 8, 14, 0, tzinfo=TZ)
    res = await _resolve_at(
        BookingDraft(service="Consulta", day=DAY, time=TEN),
        now=afternoon,
        cal=_Cal({DAY: ["14:30", "15:00"]}),
        pros=[_sole()],
    )
    assert res.landing_step == fr.STEP_AWAITING_SLOT
    assert res.dropped == {"time": "no_free_slot"}
    assert [row[0] for row in res.result.bubbles[-1].rows][:2] == [
        "slot|2026-10-08T14:30",
        "slot|2026-10-08T15:00",
    ]


async def test_today_with_nothing_left_lands_on_the_day_picker():
    afternoon = dt.datetime(2026, 10, 8, 14, 0, tzinfo=TZ)
    res = await _resolve_at(
        BookingDraft(service="Consulta", day=DAY, time=TEN),
        now=afternoon,
        cal=_Cal({DAY: [], dt.date(2026, 10, 9): ["09:00"]}),
        pros=[_sole()],
    )
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.dropped == {"day": "day_unavailable", "time": "missing_day"}
    assert res.result.bubbles[-1].body.startswith(bd.DRAFT_DAY_UNAVAILABLE_PREFIX)


async def test_express_confirmation_is_off_without_the_switch():
    state = fr._DayPickerState(flow_selected_type="Consulta", flow_attendee_name="")
    assert (
        await bd._express_confirmation(
            state=state,
            tenant=_tenant(),
            professional=None,
            service={"name": "Consulta"},
            slot_start=dt.datetime(2026, 10, 8, 10, 0, tzinfo=TZ),
            duration_minutes=30,
            calendar=_Cal(),
            professionals=[],
        )
        is None
    )


@pytest.mark.parametrize(
    "state, step, recorded, professional_id, expected",
    [
        (FlowState.SERVICE_CATALOG, fr.STEP_AWAITING_SERVICE_CONFIRM, "Consulta", None, True),
        (FlowState.SERVICE_CATALOG, fr.STEP_AWAITING_DAY_RETRY, "consulta", None, True),
        (FlowState.SERVICE_CATALOG, fr.STEP_AWAITING_SLOT, "Consulta", None, True),
        (FlowState.SERVICE_CATALOG, fr.STEP_AWAITING_CONFIRMATION, "Consulta", None, True),
        (FlowState.SERVICE_CATALOG, fr.STEP_AWAITING_RETRY, "Consulta", None, True),
        (FlowState.SERVICE_CATALOG, fr.STEP_AWAITING_INSURANCE, "Consulta", None, False),
        (FlowState.SERVICE_CATALOG, fr.STEP_AWAITING_SLOT, "Retorno", None, False),
        (FlowState.SERVICE_CATALOG, fr.STEP_AWAITING_SLOT, "Consulta", "outro-medico", False),
        (FlowState.LLM, None, "Consulta", None, False),
        (
            FlowState.SERVICE_CATALOG,
            fr.STEP_AWAITING_ATTENDEE_CHOICE,
            fr.ATTENDEE_NEXT_BOOK,
            None,
            False,
        ),
    ],
)
def test_details_already_shown(state, step, recorded, professional_id, expected):
    conversation = SimpleNamespace(
        flow_state=state,
        flow_step=step,
        flow_selected_type=recorded,
        flow_selected_professional_id=professional_id,
    )
    assert (
        bd.details_already_shown(conversation, service_name="Consulta", professional_id=None)
        is expected
    )


def test_no_conversation_means_not_shown():
    assert bd.details_already_shown(None, service_name="Consulta", professional_id=None) is False
```

Em `tests/test_booking_draft_resolver.py` (P2a), substituir a função `test_p2_has_no_express_confirmation_yet` inteira (do `async def test_p2_has_no_express_confirmation_yet():` até a linha em branco antes de `async def test_a_time_outside_business_hours_lands_on_that_days_slot_list`) por:

```python
async def test_express_confirmation_needs_the_clinic_switch():
    """P3 filled the hook; without `initial_flows.ai_draft_v2` it still answers None."""
    assert (
        await bd._express_confirmation(
            state=None,
            tenant=None,
            professional=None,
            service={},
            slot_start=NOW,
            duration_minutes=30,
            calendar=None,
            professionals=[],
        )
        is None
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_express_confirmation.py -q`
Expected: FAIL — os pousos expressos caem em `awaiting_slot` (P2 devolve `None` no gancho), sem `TextBubble`; `test_details_already_shown` com `AttributeError: module 'secretaria.services.booking_draft' has no attribute 'details_already_shown'`. `test_without_the_switch_...` e `test_express_confirmation_is_off_without_the_switch` já PASSAM (fixam o P2).

- [ ] **Step 3: Implement**

Em `src/secretaria/services/booking_draft.py`:

1. Imports (ordem do ruff): acrescentar `from secretaria.ai.formatter import TextBubble` como a PRIMEIRA linha de import de primeira parte (antes de `from secretaria.models import FlowState`); `from secretaria.services.booking_details import booking_details_text` logo antes de `from secretaria.services.booking_hold import BookingGate`; e `from secretaria.services.service_catalog import normalize as normalize_service_name` logo antes de `from secretaria.services.tenant_config import (`.

2. Em `_resolve` (texto do P2a), na chamada `return await _land_day(`, trocar

```python
        checked,
        state,
        tenant=tenant,
```

por

```python
        checked,
        state,
        conversation=conversation,
        tenant=tenant,
```

(a única chamada de `_land_day` do arquivo.)

3. Substituir TUDO de `async def _land_day(` até o fim do arquivo (`_land_day` e `_express_confirmation`, texto do P2a A5) por:

```python
# Steps the service-detail card has already been shown by the time a conversation sits on
# them, in the button flow: "Sim" on that card is the only way past it. On clinics with the
# AI draft v2 switch, every landing of the resolver past that card (day picker, slot list,
# express card) carries the booking details message instead (`_land_day`), so reaching one
# of these steps means the details were shown on BOTH paths.
DETAILS_SEEN_STEPS = (
    fr.STEP_AWAITING_SERVICE_CONFIRM,
    fr.STEP_AWAITING_DAY,
    fr.STEP_AWAITING_DAY_RETRY,
    fr.STEP_AWAITING_DAY_ESCAPE,
    fr.STEP_AWAITING_SLOT,
    fr.STEP_AWAITING_CONFIRMATION,
    fr.STEP_AWAITING_RETRY,
)


def details_already_shown(
    conversation: Any, *, service_name: str | None, professional_id: Any
) -> bool:
    """Whether this patient already saw THIS booking's details (spec §4.4.2).

    True only when the FRESH conversation sits on a step past the service-detail card
    (`DETAILS_SEEN_STEPS`) for the same service (accent- and case-insensitive) and the same
    doctor. Anything else - the LLM state (no step), a pra-quem, convênio, doctor or service
    question, another service or another doctor - means the details go out.
    """
    if conversation is None or not service_name:
        return False
    if getattr(conversation, "flow_state", None) is not FlowState.SERVICE_CATALOG:
        return False
    if getattr(conversation, "flow_step", None) not in DETAILS_SEEN_STEPS:
        return False
    recorded = _recorded_service(conversation)
    if recorded is None or normalize_service_name(recorded) != normalize_service_name(service_name):
        return False
    return getattr(conversation, "flow_selected_professional_id", None) == professional_id


def _booking_details(
    checked: _Checked, state: Any, *, conversation: Any, tenant: Any, owner: Any | None
) -> str | None:
    """The details message for a landing past the service card, or None.

    None on a clinic without the AI draft v2 switch (P2's landings stay exactly as they
    were), without a resolved service, or when the patient already saw this booking's
    details. `tenant.clinic_address` is the line workers/shared/draft_resolution.py put on
    the snapshot (None when the clinic has none, or has units).
    """
    if not fr.ai_draft_v2_enabled(tenant) or checked.service is None:
        return None
    if details_already_shown(
        conversation,
        service_name=str(checked.service.get("name") or ""),
        professional_id=state.flow_selected_professional_id,
    ):
        return None
    return booking_details_text(
        service=checked.service,
        professional=owner,
        insurance=state.flow_selected_insurance,
        attendee_name=state.flow_attendee_name,
        address=getattr(tenant, "clinic_address", None),
    )


def _with_details(result: FlowRouterResult, details: str | None) -> FlowRouterResult:
    """Open a booking landing past the service card with the details message."""
    if (
        details is None
        or result.action != "reply"
        or result.flow_state is not FlowState.SERVICE_CATALOG
        or result.flow_step not in DETAILS_SEEN_STEPS
    ):
        return result
    result.bubbles = [TextBubble(body=details), *result.bubbles]
    return result


async def _land_day(
    draft: BookingDraft,
    checked: _Checked,
    state: Any,
    *,
    conversation: Any,
    tenant: Any,
    professionals: list,
    service_catalog: list | None,
    calendar: CalendarSource,
    now: dt.datetime,
    done: Callable[..., DraftResolution],
) -> DraftResolution:
    """Steps 5-6: the day, then the time - both re-derived from the FRESH agenda.

    Nothing the AI sends becomes a time without passing through the agenda's own free
    slots for that day, minus the slots other conversations are holding (spec §4.4.1).
    An invalid day lands on the day picker, an invalid time on the slot list of its day.

    TASK-030 P3, on clinics with the AI draft v2 switch: a valid time lands on the express
    confirmation (`_express_confirmation`); and every landing here skips the service-detail
    card, so each one - day picker, slot list, express card - opens with the booking
    details message, unless this patient already saw them (`details_already_shown`).
    """
    dropped = checked.dropped
    base = (FIELD_FOR_WHOM, FIELD_INSURANCE, FIELD_PROFESSIONAL, FIELD_SERVICE)
    services = _booking_services(checked, tenant, service_catalog)

    # The static "this doctor has no hours at all" check, before any calendar call - the
    # same one `_ask_day` makes for the button flow.
    owner = fr._booking_professional(state, professionals)
    if owner is not None and not professional_business_hours(owner, tenant):
        return done(fr._professional_config_incomplete(owner, fr.PROFESSIONAL_GAP_HOURS), *base)

    details = _booking_details(
        checked, state, conversation=conversation, tenant=tenant, owner=owner
    )
    cal = await calendar(checked.professional if checked.multi else None)

    def land(result: FlowRouterResult, *names: str) -> DraftResolution:
        return done(_with_details(result, details), *names)

    async def day_picker(prefix: str | None = None) -> DraftResolution:
        result = await fr._ask_day(state, tenant, cal, services, professionals, prefix=prefix)
        return land(result, *base)

    def dropped_day(reason: str) -> None:
        dropped[FIELD_DAY] = reason
        if draft.time is not None:
            dropped[FIELD_TIME] = DROP_MISSING_DAY

    if draft.day is None:
        return await day_picker()
    if cal is None:
        unavailable = fr._calendar_unavailable(state, fr.BOOKING_DAY_BRANCH, fr.STEP_AWAITING_DAY)
        return done(unavailable, *base)

    today = now.astimezone(cal.tzinfo).date()
    if not today <= draft.day < today + dt.timedelta(days=fr.DAY_PICKER_WINDOW_DAYS):
        dropped_day(DROP_OUT_OF_WINDOW)
        return await day_picker(DRAFT_DAY_OUT_OF_WINDOW_PREFIX)

    duration = fr._booking_duration(state, tenant, services)
    holds = await fr._hold_windows(fr._hold_owner(state, professionals))
    try:
        free = await free_slots_for_day(cal, day=draft.day, duration_minutes=duration, holds=holds)
    except CalendarUnavailableError:
        unavailable = fr._calendar_unavailable(state, fr.BOOKING_DAY_BRANCH, fr.STEP_AWAITING_DAY)
        return done(unavailable, *base)
    if not free:
        dropped_day(DROP_DAY_UNAVAILABLE)
        return await day_picker(DRAFT_DAY_UNAVAILABLE_PREFIX)

    names = (*base, FIELD_DAY)
    if draft.time is not None:
        match = next(
            (slot for slot in free if slot.time().replace(second=0, microsecond=0) == draft.time),
            None,
        )
        if match is None:
            dropped[FIELD_TIME] = DROP_NO_FREE_SLOT
        else:
            names = (*names, FIELD_TIME)
            express = await _express_confirmation(
                state=state,
                tenant=tenant,
                professional=owner,
                service=checked.service,
                slot_start=match,
                duration_minutes=duration,
                calendar=cal,
                professionals=professionals,
                details=details,
            )
            if express is not None:
                return done(express, *names)

    target = dt.datetime(draft.day.year, draft.day.month, draft.day.day)
    result = await fr._enter_slot_picker(
        state,
        tenant,
        cal,
        target,
        duration_minutes=duration,
        branch=fr.BOOKING_DAY_BRANCH,
        back_target=fr.BACK_TARGET_SERVICE,
        professionals=professionals,
    )
    return land(result, *names)


async def _express_confirmation(
    *,
    state: Any,
    tenant: Any,
    professional: Any | None,
    service: dict,
    slot_start: dt.datetime,
    duration_minutes: int,
    calendar: CalendarService,
    professionals: list,
    details: str | None = None,
) -> FlowRouterResult | None:
    """Straight to the confirmation card when every item is valid (spec §4.4) - or None.

    Only on clinics with the AI draft v2 switch (`flow_router.ai_draft_v2_enabled`); None
    elsewhere, and the patient lands on the day's slot list exactly as in P2.

    Called only with pra-quem answered, the service known, and `slot_start` re-derived from
    the agenda's free slots minus holds (aware). `state` is the resolver's `_DayPickerState`;
    `professional` is the booking owner, named on the card. The bubbles are the booking
    details (`details`, when the patient has not seen them) and the card
    (`flow_router._confirmation_card`: service, doctor, who, date/time; Confirmar/Cancelar).

    NOTHING is booked here - no event, no appointment, no hold. "Confirmar" is routed by
    `flow_router._handle_confirmation`, exactly as for the card the slot tap draws (the
    Portal's code gate, the holds, the event, the row and the deposit hooks included).
    `flow_selected_slot` is the naive ISO minute of `slot_start` in the agenda's timezone,
    the same instant the card prints (skill date-derived-ui-labels); the resolver's `_carry`
    fills type, professional, convênio and attendee.
    """
    if not fr.ai_draft_v2_enabled(tenant):
        return None
    start = (
        slot_start.astimezone(calendar.tzinfo)
        if calendar is not None and slot_start.tzinfo is not None
        else slot_start
    )
    card = fr._confirmation_card(state, start, professional=professional)
    return FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=details), card] if details else [card],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_CONFIRMATION,
        flow_selected_day=start.date().isoformat(),
        flow_selected_slot=start.replace(tzinfo=None).isoformat(timespec="minutes"),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_express_confirmation.py tests/test_booking_draft_resolver.py tests/test_booking_draft.py tests/test_availability.py tests/test_confirmation_card.py -q`
Expected: PASS (os novos; e todos os do resolvedor do P2a — interruptor desligado neles, então nenhum pouso ganhou detalhes).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format src/secretaria/services/booking_draft.py tests/test_express_confirmation.py tests/test_booking_draft_resolver.py
uvx ruff check --fix --select I src/secretaria/services/booking_draft.py tests/test_express_confirmation.py
uvx ruff check src/secretaria/services/booking_draft.py tests/test_express_confirmation.py tests/test_booking_draft_resolver.py
git add src/secretaria/services/booking_draft.py tests/test_express_confirmation.py tests/test_booking_draft_resolver.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(draft): express confirmation - a complete AI draft lands on the details and the card

On clinics with the AI draft v2 switch, a draft whose time is free on the fresh agenda
minus holds lands on the confirmation card, preceded by the booking details unless this
patient already saw them (details_already_shown: a step past the service card, same
service and doctor). Every resolver landing past the service card carries the details, so
the rule holds on both paths. Nothing is booked: "Confirmar" stays _handle_confirmation.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 4: No worker — o endereço da clínica e a confirmação expressa de ponta a ponta

**Files:**
- Modify: `src/secretaria/workers/shared/draft_resolution.py` (imports; `_load_draft_context`)
- Modify: `tests/test_handback_events.py` (o teste do P2b que esperava a lista de horários)
- Test: `tests/test_express_confirmation_worker.py`

**Interfaces:**
- Consumes: T1 (`clinic_address_line`), T3 (expresso), P2a (`_load_draft_context`, `DraftContext`, `_flow_tenant_snapshot`, `_flow_professionals`), P2b (`_handle_set_booking_draft`).
- Produces: `DraftContext.tenant_snapshot.clinic_address: str | None` (None sem endereço com rua ou com unidade ativa). Nenhuma assinatura muda.

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_express_confirmation_worker.py`:

```python
"""Express confirmation end to end in the worker (TASK-030 P3, spec §4.4).

The AI's full draft reaches `_handle_set_booking_draft` (P2b) on a clinic with the AI
draft v2 switch: the patient receives the booking details and the confirmation card, and
NOTHING is booked until "Confirmar" - which runs EXACTLY the button flow's
`flow_router._handle_confirmation` (the Portal code gate and holds included).
In-memory SQLite, same pattern as tests/test_handback_events.py.
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

import json  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai.formatter import ButtonBubble, TextBubble  # noqa: E402
from secretaria.ai.graph import BOOKING_DRAFT_SENTINEL_PREFIX  # noqa: E402
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    BookingHold,
    Conversation,
    FlowState,
    Patient,
    Professional,
    Tenant,
    Unit,
)
from secretaria.services import booking_hold as booking_hold_service, flow_router as fr  # noqa: E402
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, CHANNEL_WHATSAPP  # noqa: E402
from secretaria.services.pending_identity import (  # noqa: E402
    BOOKING_SLOT_TAKEN_MESSAGE,
    RequestCodeOutcome,
    RequestCodeResult,
)
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import handback_log  # noqa: E402
from secretaria.workers.shared.flow_runner import _run_flow  # noqa: E402
from secretaria.workers.shared.greeting import _flow_professionals, _flow_tenant_snapshot  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
DAY = (datetime.now(TZ) + timedelta(days=3)).date()
WHEN = f"{DAY.strftime('%d/%m/%Y')} às 10:00"
WA_ID = "5511999999999"
EXTERNAL_ID = "00000000-0000-4000-8000-0000000000aa"
OTHER_EXTERNAL_ID = "00000000-0000-4000-8000-0000000000bb"
ADDRESS = {
    "line": "Rua A, 1",
    "neighborhood": "Centro",
    "city": "São Paulo",
    "state": "SP",
    "postal_code": "01000-000",
}
ADDRESS_LINE = "Endereço: Rua A, 1, Centro, São Paulo/SP, CEP 01000-000"
SERVICE = {
    "name": "Consulta",
    "duration_min": 30,
    "is_active": True,
    "sort_order": 0,
    "price": "250",
    "long_description": "Avaliação completa.",
    "requirements": ["Jejum de 8 horas"],
}
EVERY_DAY = {
    day: [{"start": "08:00", "end": "18:00"}]
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}
DRAFT = {"t": "Consulta", "w": "self", "d": DAY.isoformat(), "h": "10:00"}


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


class _Agenda:
    """The doctor's agenda: 10:00 and 10:30 free on DAY. Records every event created."""

    def __init__(self):
        self.tzinfo = TZ
        self.created: list = []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        return [datetime(DAY.year, DAY.month, DAY.day, tzinfo=TZ)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        if day.date() != DAY:
            return []
        iso = DAY.isoformat()
        return [{"start": f"{iso}T{t}", "end": "", "label": t} for t in ("10:00", "10:30")][
            :max_slots
        ]

    async def create_event(self, start, end, summary, description=""):
        self.created.append((start, end, summary))
        return {"id": f"evt-{len(self.created)}", "htmlLink": "https://calendar.google.com/x"}


class _LogRecorder:
    """Records every structlog call - `caplog` is empty for structlog in this suite."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        def _log(event: str, **fields) -> None:
            self.records.append((level, event, fields))

        return _log


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, db):
    agenda = _Agenda()
    sent: list = []
    notices: list = []
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(booking_hold_service, "async_session_factory", db)

    async def _calendar(session, tenant, target):
        return agenda

    monkeypatch.setattr(workers_ns, "_appointment_calendar", _calendar)
    monkeypatch.setattr(workers_ns, "_flow_turn_calendar", lambda conv, config, cal: agenda)

    async def _dispatch(reply, bubbles, tenant=None, waba_token=None):
        sent.extend(bubbles)
        return len(bubbles)

    monkeypatch.setattr(workers_ns, "_dispatch_bubbles", _dispatch)

    async def _notice(reply, hold, *, tenant=None, waba_token=None):
        notices.append(hold)

    monkeypatch.setattr(workers_ns, "_send_booking_gate_notice", _notice)

    async def _no_hooks(*_args, **_kwargs):
        return None

    monkeypatch.setattr(workers_ns, "enqueue_post_booking_hooks", _no_hooks)

    async def _code(tenant_id, external_id):
        return RequestCodeResult(RequestCodeOutcome.SENT, email_masked="m***a@gmail.com")

    monkeypatch.setattr(booking_hold_service, "request_code", _code)
    log = _LogRecorder()
    monkeypatch.setattr(workers_ns, "logger", log)
    return SimpleNamespace(db=db, agenda=agenda, sent=sent, notices=notices, log=log)


def _ref(channel: str) -> str:
    return WA_ID if channel == CHANNEL_WHATSAPP else EXTERNAL_ID


def _reply(conversation, channel: str, body: str) -> tasks._ReplyContext:
    return tasks._ReplyContext(
        channel=channel,
        conversation_id=conversation.id,
        patient_ref=_ref(channel),
        inbound_body=body,
    )


async def _seed(db, *, channel=CHANNEL_WHATSAPP, address=ADDRESS, units=0):
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            is_active=True,
            timezone="America/Sao_Paulo",
            initial_flows={"ai_draft_v2": True},
            appointment_types=[dict(SERVICE)],
            business_hours=EVERY_DAY,
            address=address,
        )
        session.add(tenant)
        await session.flush()
        doctor = Professional(
            tenant_id=tenant.id, name="Dra. Única", specialty="Clínica Geral", is_active=True
        )
        session.add(doctor)
        for index in range(units):
            session.add(
                Unit(tenant_id=tenant.id, name=f"Unidade {index}", address="Rua B, 2", is_active=True)
            )
        if channel == CHANNEL_WHATSAPP:
            patient = Patient(tenant_id=tenant.id, wa_id=WA_ID, name="Maria")
        else:
            patient = Patient(
                tenant_id=tenant.id,
                channel=CHANNEL_BRAIN_MESSAGE,
                external_id=EXTERNAL_ID,
                name="Maria",
                lgpd_accepted_at=datetime.now(UTC),
            )
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id, patient_id=patient.id, flow_state=FlowState.LLM
        )
        session.add(conversation)
        await session.commit()
        for obj in (tenant, doctor, patient, conversation):
            await session.refresh(obj)
        return tenant, doctor, patient, conversation


async def _other_portal_conversation(db, tenant) -> Conversation:
    async with db() as session:
        patient = Patient(
            tenant_id=tenant.id,
            channel=CHANNEL_BRAIN_MESSAGE,
            external_id=OTHER_EXTERNAL_ID,
            name="Outra",
            lgpd_accepted_at=datetime.now(UTC),
        )
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.commit()
        await session.refresh(conversation)
        return conversation


async def _land(tenant, conversation, channel=CHANNEL_WHATSAPP) -> None:
    await tasks._handle_set_booking_draft(
        _reply(conversation, channel, "quinta às 10h, pra mim"),
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps(DRAFT),
        tenant,
        None,
        [],
        _ref(channel),
    )


async def _row(db, conversation) -> Conversation:
    async with db() as session:
        return await session.get(Conversation, conversation.id)


async def _confirm(wired, tenant, conversation, channel=CHANNEL_WHATSAPP) -> bool:
    """Tap "Confirmar" through the live flow runner, exactly like a real turn."""
    async with wired.db() as session:
        row = await session.get(Conversation, conversation.id)
        rows = list(
            (
                await session.scalars(select(Professional).where(Professional.tenant_id == tenant.id))
            ).all()
        )
    snapshot = SimpleNamespace(
        id=row.id,
        tenant_id=row.tenant_id,
        patient_id=row.patient_id,
        flow_state=row.flow_state,
        flow_step=row.flow_step,
        flow_selected_type=row.flow_selected_type,
        flow_selected_day=row.flow_selected_day,
        flow_selected_slot=row.flow_selected_slot,
        flow_selected_professional_id=row.flow_selected_professional_id,
        flow_selected_insurance=row.flow_selected_insurance,
        flow_managing_appointment_id=row.flow_managing_appointment_id,
        flow_attendee_name=row.flow_attendee_name,
        flow_draft=row.flow_draft,
    )
    return await _run_flow(
        _reply(conversation, channel, fr.LABEL_CONFIRM),
        snapshot,
        _flow_tenant_snapshot(tenant, rows, [], None),
        None,
        "Maria",
        _ref(channel),
        tenant=tenant,
        waba_token="t",
        professionals=_flow_professionals(rows, []),
    )


async def _count(db, model, tenant) -> int:
    async with db() as session:
        return await session.scalar(
            select(func.count()).select_from(model).where(model.tenant_id == tenant.id)
        )


def _events(log: _LogRecorder, name: str = handback_log.EVENT_NAME) -> list[dict]:
    return [fields for _level, event, fields in log.records if event == name]


# --------------------------------------------------------------------------
# The landing: details + card, nothing booked
# --------------------------------------------------------------------------


async def test_a_full_draft_lands_on_the_details_and_the_card_and_books_nothing(wired):
    tenant, _doctor, _patient, conversation = await _seed(wired.db)

    await _land(tenant, conversation)

    details, card = wired.sent
    assert isinstance(details, TextBubble) and isinstance(card, ButtonBubble)
    assert details.body == (
        "Confira os detalhes da sua consulta:\n\n"
        "🥼 Profissional: Dra. Única — Clínica Geral\n"
        "🏥 Serviço: Consulta — R$250\n"
        "👤 Para: você\n"
        f"{ADDRESS_LINE}\n\n"
        "Avaliação completa.\n\n"
        "O que levar / preparo:\n"
        "• Jejum de 8 horas"
    )
    assert card.body == f"Consulta\nProfissional: Dra. Única\n{WHEN}"
    row = await _row(wired.db, conversation)
    assert row.flow_step == fr.STEP_AWAITING_CONFIRMATION
    assert (row.flow_selected_day, row.flow_selected_slot) == (
        DAY.isoformat(),
        f"{DAY.isoformat()}T10:00",
    )
    assert row.flow_draft is None
    # Nothing the AI sends creates a booking by itself.
    assert wired.agenda.created == []
    assert await _count(wired.db, Appointment, tenant) == 0
    assert await _count(wired.db, BookingHold, tenant) == 0


async def test_the_hand_back_is_logged_on_the_confirmation_card(wired):
    tenant, _doctor, _patient, conversation = await _seed(wired.db)

    await _land(tenant, conversation)

    (event,) = _events(wired.log)
    assert event["source_tool"] == "set_booking_draft"
    assert event["landing_step"] == "awaiting_confirmation"
    assert event["accepted"] == ["service", "for_whom", "day", "time"]
    assert event["dropped"] == {}
    assert event["fallback"] is None
    assert event["topology"] == "sole"


@pytest.mark.parametrize(
    "address, shown",
    [(ADDRESS, True), (None, False), ({"city": "Recife"}, False)],
)
async def test_the_clinic_address_is_shown_only_when_stored(wired, address, shown):
    tenant, _doctor, _patient, conversation = await _seed(wired.db, address=address)

    await _land(tenant, conversation)

    assert ("Endereço:" in wired.sent[0].body) is shown


async def test_a_clinic_with_units_never_shows_the_clinic_address(wired):
    tenant, _doctor, _patient, conversation = await _seed(wired.db, units=1)

    await _land(tenant, conversation)

    assert "Endereço:" not in wired.sent[0].body


# --------------------------------------------------------------------------
# "Confirmar" is today's path
# --------------------------------------------------------------------------


async def test_confirmar_on_whatsapp_books_through_todays_path(wired):
    tenant, doctor, _patient, conversation = await _seed(wired.db)
    await _land(tenant, conversation)

    await _confirm(wired, tenant, conversation)

    assert len(wired.agenda.created) == 1
    async with wired.db() as session:
        (appointment,) = (
            await session.scalars(select(Appointment).where(Appointment.tenant_id == tenant.id))
        ).all()
    # The sole doctor owns the booking (resolve_booking_owner_id), as on the button path.
    assert appointment.professional_id == doctor.id
    assert appointment.appointment_type == "Consulta"
    assert "Pronto! Seu agendamento está confirmado." in wired.sent[-1].body
    assert (await _row(wired.db, conversation)).flow_state == FlowState.IDLE


async def test_tapping_confirmar_twice_books_once(wired):
    tenant, _doctor, _patient, conversation = await _seed(wired.db)
    await _land(tenant, conversation)

    await _confirm(wired, tenant, conversation)
    await _confirm(wired, tenant, conversation)

    assert len(wired.agenda.created) == 1
    assert await _count(wired.db, Appointment, tenant) == 1
    # The second tap reaches an IDLE conversation, which re-presents the menu.
    assert (await _row(wired.db, conversation)).flow_state == FlowState.MENU


async def test_confirmar_on_the_portal_holds_the_slot_and_asks_for_the_code(wired):
    tenant, _doctor, _patient, conversation = await _seed(wired.db, channel=CHANNEL_BRAIN_MESSAGE)
    await _land(tenant, conversation, CHANNEL_BRAIN_MESSAGE)

    await _confirm(wired, tenant, conversation, CHANNEL_BRAIN_MESSAGE)

    assert wired.agenda.created == []
    assert await _count(wired.db, Appointment, tenant) == 0
    assert await _count(wired.db, BookingHold, tenant) == 1
    assert len(wired.notices) == 1
    assert (await _row(wired.db, conversation)).flow_state == FlowState.AWAITING_EMAIL_CODE


async def test_a_slot_taken_before_confirmar_on_the_portal_is_offered_again(wired):
    tenant, doctor, _patient, conversation = await _seed(wired.db, channel=CHANNEL_BRAIN_MESSAGE)
    await _land(tenant, conversation, CHANNEL_BRAIN_MESSAGE)
    other = await _other_portal_conversation(wired.db, tenant)
    start = datetime(DAY.year, DAY.month, DAY.day, 10, 0, tzinfo=TZ)
    # Somebody else confirms the very same window first - on the sole doctor's agenda,
    # the owner a hold is PLACED with.
    assert (
        await booking_hold_service.place_hold(
            tenant_id=tenant.id,
            conversation_id=other.id,
            patient_id=None,
            professional_id=doctor.id,
            appointment_type="Consulta",
            insurance=None,
            start_at=start,
            end_at=start + timedelta(minutes=30),
        )
        is not None
    )

    await _confirm(wired, tenant, conversation, CHANNEL_BRAIN_MESSAGE)

    assert (await _row(wired.db, conversation)).flow_step == fr.STEP_AWAITING_RETRY
    assert wired.sent[-1].body == BOOKING_SLOT_TAKEN_MESSAGE
    assert wired.agenda.created == []
    assert await _count(wired.db, Appointment, tenant) == 0


async def test_whatsapp_confirmar_does_not_reread_the_agenda_exactly_like_the_button_card(wired):
    """PRE-EXISTING, pinned so P3 provably leaves it alone (plan, "Decisões" 6).

    On WhatsApp the gate is unarmed and `_handle_confirmation` never re-reads the agenda
    nor the holds before creating the event - for the card the slot tap draws as for the
    express card. Whether to add a re-check for BOTH is the owner's call; until then the
    two must behave the same.
    """
    tenant, doctor, _patient, conversation = await _seed(wired.db)
    await _land(tenant, conversation)
    other = await _other_portal_conversation(wired.db, tenant)
    start = datetime(DAY.year, DAY.month, DAY.day, 10, 0, tzinfo=TZ)
    await booking_hold_service.place_hold(
        tenant_id=tenant.id,
        conversation_id=other.id,
        patient_id=None,
        professional_id=doctor.id,
        appointment_type="Consulta",
        insurance=None,
        start_at=start,
        end_at=start + timedelta(minutes=30),
    )

    await _confirm(wired, tenant, conversation)

    assert len(wired.agenda.created) == 1
    assert await _count(wired.db, Appointment, tenant) == 1
```

Em `tests/test_handback_events.py`, substituir o teste do P2b `test_switch_on_lands_a_full_draft_on_the_days_slot_list` inteiro (do `async def` até a última asserção dele) por:

```python
async def test_switch_on_lands_a_full_draft_on_the_confirmation_card(
    db, _captured_bubbles, _slot_agenda, log
) -> None:
    """TASK-030 P3: a free time on a switched-on clinic is the express confirmation."""
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
    assert event["landing_step"] == "awaiting_confirmation"
    assert event["accepted"] == ["service", "for_whom", "day", "time"]
    assert event["topology"] == "sole"
    assert len(_events(log, "conversation_booking_draft_entered")) == 1
    assert [type(bubble).__name__ for bubble in _captured_bubbles] == ["TextBubble", "ButtonBubble"]
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_step == "awaiting_confirmation"
    assert conv.flow_selected_day == day.isoformat()
    assert conv.flow_selected_slot == f"{day.isoformat()}T10:00"
    assert conv.flow_attendee_name == ATTENDEE_SELF
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_express_confirmation_worker.py tests/test_handback_events.py -q`
Expected: FAIL — `test_the_clinic_address_is_shown_only_when_stored[address0-True]` e `test_a_full_draft_lands_on_the_details_and_the_card_and_books_nothing` (sem a linha "Endereço": `clinic_address` ainda não existe no snapshot). Os demais PASSAM já com as Tasks 1–3 (fixam o caminho de "Confirmar" e os casos sem endereço); se algum deles falhar, é regressão das Tasks 2–3 — investigue antes de seguir.

- [ ] **Step 3: Implement**

Em `src/secretaria/workers/shared/draft_resolution.py` (arquivo do P2a):

1. Imports: trocar `from secretaria.models import Conversation, FlowState, Tenant` por `from secretaria.models import Conversation, FlowState, Tenant, Unit`; acrescentar `from sqlalchemy import func, select` como import de terceiros (logo depois de `from typing import Any` e de uma linha em branco, antes do bloco `from secretaria...`); acrescentar `from secretaria.services.booking_details import clinic_address_line` logo antes de `from secretaria.services.booking_draft import (`.

2. Substituir a função `_load_draft_context` inteira por:

```python
async def _load_draft_context(reply: _ReplyContext, tenant: Tenant) -> DraftContext | None:
    """One short session of fresh reads, scoped to `tenant`. None = no such conversation.

    A conversation that belongs to another tenant is refused (count-only warning): every
    roster, catalog and agenda below is read for `tenant`, so mixing the two would book a
    patient against another clinic's data.

    TASK-030 P3: the tenant snapshot also carries `clinic_address`, the one line the
    booking details print (`services/booking_details.py::clinic_address_line` over
    `Tenant.address`) or None. Always None on a clinic with an active unit: there the patient
    goes to a UNIT, which this booking does not choose, and a plausible-but-wrong address is
    worse than none (docs/CHECKPOINT_patient_calendar_link.md, "sem location").
    """
    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        if conversation is None:
            return None
        if conversation.tenant_id != tenant.id:
            logger.warning(
                "worker_draft_context_tenant_mismatch",
                conversation_id=str(reply.conversation_id),
            )
            return None
        snapshot = _conversation_flow_snapshot(conversation)
        professional_rows = await list_active_professionals(session, tenant.id)
        service_catalog = await load_service_catalog(session, tenant.id)
        tenant_insurance = await load_tenant_insurance(session, tenant.id)
        active_units = await session.scalar(
            select(func.count())
            .select_from(Unit)
            .where(Unit.tenant_id == tenant.id, Unit.is_active.is_(True))
        )
    tenant_snapshot = _flow_tenant_snapshot(
        tenant, professional_rows, service_catalog, tenant_insurance
    )
    tenant_snapshot.clinic_address = (
        None if active_units else clinic_address_line(getattr(tenant, "address", None))
    )
    return DraftContext(
        conversation=snapshot,
        professional_rows=professional_rows,
        professionals=_flow_professionals(professional_rows, service_catalog),
        service_catalog=service_catalog,
        tenant_insurance=tenant_insurance,
        tenant_snapshot=tenant_snapshot,
        topology=booking_topology(professional_rows),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_express_confirmation_worker.py tests/test_handback_events.py tests/test_booking_draft_continuation.py tests/test_booking_code_gate.py tests/test_attendee_booking.py tests/test_workers_layering.py -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff format src/secretaria/workers/shared/draft_resolution.py tests/test_express_confirmation_worker.py tests/test_handback_events.py
uvx ruff check --fix --select I src/secretaria/workers/shared/draft_resolution.py tests/test_express_confirmation_worker.py
uvx ruff check src/secretaria/workers/shared/draft_resolution.py tests/test_express_confirmation_worker.py tests/test_handback_events.py
git add src/secretaria/workers/shared/draft_resolution.py tests/test_express_confirmation_worker.py tests/test_handback_events.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(draft): the clinic address in the booking details, and the express path end to end

The fresh draft context carries the clinic's address line (Tenant.address, street
required), never on a clinic with active units. Worker tests pin the whole express path:
details + card and nothing booked; "Confirmar" through the live runner books once on
WhatsApp, holds and asks for the code on the Portal, and offers the day again when the
slot was taken meanwhile on the Portal.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 5: O rascunho espera também convênio, médico e serviço (decisão do dono de 2026-10-03)

**Files:**
- Modify: `src/secretaria/services/flow_router.py` (`DRAFT_WAIT_STEPS` logo depois de `ATTENDEE_NEXT_CATALOG`; `_carry_draft`; `_resume_parked_draft` logo depois de `_has_draft`; três ramos de `_catalog_step`)
- Modify: `src/secretaria/services/booking_draft.py` (`_resolve`: do comentário `# 2. Convênio` até o fim da função)
- Modify: `src/secretaria/workers/shared/draft_resolution.py` (import de `flow_router`; `_fold_answer`; `_resume_booking_draft`)
- Test: `tests/test_draft_wait_questions.py` (roteador e resolvedor, puros), `tests/test_booking_draft_resume_questions.py` (worker, ponta a ponta)

**Interfaces:**
- Consumes: P2a (`_has_draft`, `_carry_draft`, `_resume_booking_draft`, `_pending`, `draft_record`, `draft_from_record`, `resume_draft`), T3.
- Produces: `fr.DRAFT_WAIT_STEPS`; `fr._resume_parked_draft(conversation, result) -> FlowRouterResult`; `draft_resolution._fold_answer(draft, answered_step, result) -> BookingDraft`; o evento `booking_draft_resumed` ganha `answered_step` (um código `STEP_*`).

**Ganchos (exatamente estes):** a resposta do convênio (`_handle_insurance`, exceto quando o resultado fica no próprio passo do convênio — "Outro convênio" pedindo o nome, ou resposta vazia), o toque num médico da lista (`STEP_AWAITING_PROFESSIONAL`, depois de o médico ser reconhecido) e o toque num serviço da lista (`STEP_AWAITING_SERVICE`, depois de o serviço ser reconhecido). "Não sei" (ajuda), texto livre e tudo o mais seguem como hoje.

- [ ] **Step 1: Write the failing tests (roteador e resolvedor)**

Criar `tests/test_draft_wait_questions.py`:

```python
"""The AI draft waits on the convênio, doctor and service questions too (TASK-030 P3).

Router and resolver level, pure. The owner's 2026-10-03 ruling: when the resolver lands on
any booking question and the draft still holds what comes after it, the patient's ANSWER
resumes the draft instead of falling into the next button step.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import datetime as dt  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.booking_draft import (  # noqa: E402
    BookingDraft,
    draft_from_record,
    draft_record,
)
from tests.test_booking_draft_resolver import (  # noqa: E402
    DAY,
    NOW,
    _doctor,
    _resolve,
    _tenant as _resolver_tenant,
)
from tests.test_flow_router import _conversation, _FakeCalendar, _tenant  # noqa: E402

RECORD = draft_record(
    BookingDraft(service="Primeira Consulta", attendee="self", day=dt.date(2026, 10, 8), time=dt.time(10, 0)),
    saved_at=dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.UTC),
)


def _doctor_snapshot(name):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=None,
        about=None,
        context_doctor_message=None,
        appointment_types=None,
        business_hours=None,
    )


ANA = _doctor_snapshot("Dra. Ana")
BETO = _doctor_snapshot("Dr. Beto")


def _collecting():
    tenant = _tenant()
    tenant.collect_insurance = True
    tenant.insurance_mode = "shared"
    tenant.insurances = ["Unimed"]
    return tenant


def _at(step, *, draft=RECORD, **kw):
    return _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=step,
        flow_attendee_name="",
        flow_draft=draft,
        **kw,
    )


# --------------------------------------------------------------------------
# Router: which answers resume, which keep waiting, which drop the draft
# --------------------------------------------------------------------------


async def test_the_convenio_answer_asks_the_worker_to_resume():
    res = await fr.route(
        _at(fr.STEP_AWAITING_INSURANCE, flow_selected_type="Primeira Consulta"),
        _collecting(),
        _FakeCalendar(),
        "Unimed",
    )
    assert res.resume_draft is True
    assert res.flow_selected_insurance == "Unimed"


async def test_outro_convenio_keeps_waiting_for_the_typed_name():
    res = await fr.route(
        _at(fr.STEP_AWAITING_INSURANCE, flow_selected_type="Primeira Consulta"),
        _collecting(),
        _FakeCalendar(),
        fr.LABEL_INSURANCE_OTHER,
    )
    assert res.flow_step == fr.STEP_AWAITING_INSURANCE
    assert res.resume_draft is False
    assert res.flow_draft == RECORD


async def test_the_doctor_tap_asks_the_worker_to_resume():
    res = await fr.route(
        _at(fr.STEP_AWAITING_PROFESSIONAL, flow_selected_type="Primeira Consulta"),
        _tenant(),
        None,
        "Dr. Beto",
        professionals=[ANA, BETO],
    )
    assert res.resume_draft is True
    assert res.flow_selected_professional_id == BETO.id


async def test_the_service_tap_asks_the_worker_to_resume():
    res = await fr.route(_at(fr.STEP_AWAITING_SERVICE), _tenant(), None, "Primeira Consulta")
    assert res.resume_draft is True
    assert res.flow_selected_type == "Primeira Consulta"


async def test_free_text_on_a_question_keeps_the_draft_for_the_llm():
    res = await fr.route(
        _at(fr.STEP_AWAITING_PROFESSIONAL),
        _tenant(),
        None,
        "hmm, qual deles é melhor?",
        professionals=[ANA, BETO],
    )
    assert res.action == "delegate_llm"
    assert res.flow_draft == RECORD
    assert res.resume_draft is False


async def test_the_help_row_drops_the_draft():
    res = await fr.route(
        _at(fr.STEP_AWAITING_PROFESSIONAL),
        _tenant(),
        None,
        fr.LABEL_DONT_KNOW,
        professionals=[ANA, BETO],
    )
    assert res.flow_step == fr.STEP_PROFESSIONAL_HELP
    assert res.flow_draft is None
    assert res.resume_draft is False


@pytest.mark.parametrize(
    "step, body, extra",
    [
        (fr.STEP_AWAITING_INSURANCE, "Unimed", {"flow_selected_type": "Primeira Consulta"}),
        (fr.STEP_AWAITING_PROFESSIONAL, "Dr. Beto", {}),
        (fr.STEP_AWAITING_SERVICE, "Primeira Consulta", {}),
    ],
)
async def test_without_a_parked_draft_the_buttons_are_untouched(step, body, extra):
    res = await fr.route(
        _at(step, draft=None, **extra),
        _collecting(),
        _FakeCalendar(),
        body,
        professionals=[ANA, BETO] if step == fr.STEP_AWAITING_PROFESSIONAL else None,
    )
    assert res.resume_draft is False
    assert res.flow_draft is None


def test_the_draft_wait_steps_are_pra_quem_plus_the_three_questions():
    assert fr.DRAFT_WAIT_STEPS == (
        *fr.ATTENDEE_STEPS,
        fr.STEP_AWAITING_INSURANCE,
        fr.STEP_AWAITING_PROFESSIONAL,
        fr.STEP_AWAITING_SERVICE,
    )


# --------------------------------------------------------------------------
# Resolver: a question landing parks what comes after it (switch on)
# --------------------------------------------------------------------------


def _switched_resolver_tenant(**kw):
    return _resolver_tenant(initial_flows={"ai_draft_v2": True}, **kw)


def _collecting_resolver_tenant(**kw):
    return _switched_resolver_tenant(
        collect_insurance=True, insurance_mode="shared", insurances=["Unimed"], **kw
    )


async def test_a_convenio_question_parks_the_day_and_time_with_the_switch_on():
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY, time=dt.time(10, 0)),
        tenant=_collecting_resolver_tenant(),
    )
    assert res.landing_step == fr.STEP_AWAITING_INSURANCE
    parked = draft_from_record(res.result.flow_draft, now=NOW)
    assert (parked.service, parked.day, parked.time) == ("Consulta", DAY, dt.time(10, 0))
    assert res.accepted == ("service", "day", "time")


async def test_without_the_switch_the_question_parks_nothing():
    tenant = _resolver_tenant(collect_insurance=True, insurance_mode="shared", insurances=["Unimed"])
    res = await _resolve(BookingDraft(service="Consulta", day=DAY, time=dt.time(10, 0)), tenant=tenant)
    assert res.landing_step == fr.STEP_AWAITING_INSURANCE
    assert res.result.flow_draft is None
    assert res.accepted == ("service",)


async def test_a_service_alone_also_waits_so_the_details_are_not_skipped():
    res = await _resolve(BookingDraft(service="Consulta"), tenant=_collecting_resolver_tenant())
    assert res.landing_step == fr.STEP_AWAITING_INSURANCE
    assert draft_from_record(res.result.flow_draft, now=NOW).service == "Consulta"


async def test_a_doctor_question_parks_the_service_and_the_day():
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Consulta"])
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY), tenant=_switched_resolver_tenant(), pros=[ana, beto]
    )
    assert res.landing_step == fr.STEP_AWAITING_PROFESSIONAL
    parked = draft_from_record(res.result.flow_draft, now=NOW)
    assert (parked.service, parked.day) == ("Consulta", DAY)


async def test_a_question_with_nothing_after_it_parks_nothing():
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Retorno"])
    res = await _resolve(
        BookingDraft(professional_id=ana.id), tenant=_switched_resolver_tenant(), pros=[ana, beto]
    )
    assert res.landing_step == fr.STEP_AWAITING_SERVICE
    assert res.result.flow_draft is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_draft_wait_questions.py -q`
Expected: FAIL — `AttributeError: module 'secretaria.services.flow_router' has no attribute 'DRAFT_WAIT_STEPS'`; os "resume" com `resume_draft is False`; os do resolvedor com `flow_draft is None` (o P2 só estaciona no "pra quem"). `test_without_the_switch_the_question_parks_nothing`, `test_the_help_row_drops_the_draft` e o parametrizado "sem rascunho" já PASSAM.

- [ ] **Step 3: Router — wait steps, carry and the hooks**

Em `src/secretaria/services/flow_router.py`:

1. Logo depois da linha `ATTENDEE_NEXT_CATALOG = "__attendee_next_catalog__"`, inserir:

```python
# The steps an AI booking draft may wait on (`Conversation.flow_draft`, TASK-030): the
# pra-quem steps (P2) and, by the owner's 2026-10-03 ruling, the convênio, doctor and
# service questions (P3). The patient's answer to any of them resumes the draft
# (`_attendee_step`, `_resume_parked_draft`); every other step drops it (`_carry_draft`).
DRAFT_WAIT_STEPS = (
    *ATTENDEE_STEPS,
    STEP_AWAITING_INSURANCE,
    STEP_AWAITING_PROFESSIONAL,
    STEP_AWAITING_SERVICE,
)
```

2. Substituir a função `_carry_draft` inteira (texto do P2a) por:

```python
def _carry_draft(conversation: Conversation, result: FlowRouterResult) -> FlowRouterResult:
    """Keep the AI's parked draft while the patient is still on a step it waits on.

    `_apply_flow_result` writes `flow_draft` unconditionally, so any result that does not
    name it clears it - which is the point: the draft waits only on `DRAFT_WAIT_STEPS`
    (pra-quem, and since TASK-030 P3 the convênio, doctor and service questions), and a
    result on any other step (the menu, the LLM, a help node, a list further on) ends that
    wait.
    """
    if result.flow_draft is None and result.flow_step in DRAFT_WAIT_STEPS:
        result.flow_draft = getattr(conversation, "flow_draft", None)
    return result
```

3. Imediatamente ANTES de `def _attendee_step(` (logo depois de `_has_draft`, do P2a), inserir:

```python
def _resume_parked_draft(conversation: Conversation, result: FlowRouterResult) -> FlowRouterResult:
    """The answer to a question an AI draft is waiting on: the worker resumes the draft.

    Set on the convênio answer and on the doctor / service taps (`_catalog_step`) when a
    draft is parked (TASK-030 P3; the pra-quem answer is P2's, in `_attendee_step`).
    workers/shared/draft_resolution.py::_resume_booking_draft folds the answer into the
    draft and re-runs the resolver over FRESH data; on any failure it keeps `result`. A
    result still ON the convênio step ("Outro convênio" asks for the name, an empty answer
    re-asks) has not answered yet and keeps waiting.
    """
    if _has_draft(conversation) and not (
        result.action == "reply" and result.flow_step == STEP_AWAITING_INSURANCE
    ):
        result.resume_draft = True
    return result


```

4. Em `_catalog_step`, trocar

```python
        if conversation.flow_selected_type:
            # A service may already be chosen when an empty LLM handback
            # reopens the professional list. Recheck the doctor's catalogue
            # before continuing to the selected service's detail card.
            return _handle_service_professional(conversation, tenant, body, professionals or [])
        return _enter_professional_services(professional, tenant)
```

por

```python
        if conversation.flow_selected_type:
            # A service may already be chosen when an empty LLM handback
            # reopens the professional list. Recheck the doctor's catalogue
            # before continuing to the selected service's detail card.
            return _resume_parked_draft(
                conversation,
                _handle_service_professional(conversation, tenant, body, professionals or []),
            )
        return _resume_parked_draft(
            conversation, _enter_professional_services(professional, tenant)
        )
```

5. Ainda em `_catalog_step`, trocar

```python
        return _enter_service_detail(service, conversation, tenant)

    if step in (STEP_SERVICE_HELP, STEP_SERVICE_HELP_FINAL):
```

por

```python
        return _resume_parked_draft(
            conversation, _enter_service_detail(service, conversation, tenant)
        )

    if step in (STEP_SERVICE_HELP, STEP_SERVICE_HELP_FINAL):
```

6. Ainda em `_catalog_step`, trocar

```python
    if step == STEP_AWAITING_INSURANCE:
        return await _handle_insurance(
            conversation, tenant, body, calendar, services, professionals
        )
```

por

```python
    if step == STEP_AWAITING_INSURANCE:
        answered = await _handle_insurance(
            conversation, tenant, body, calendar, services, professionals
        )
        return _resume_parked_draft(conversation, answered)
```

(Só este ocorre dentro de `_catalog_step`; o `if step == STEP_AWAITING_INSURANCE:` de `_resume_bubbles` devolve `_enter_insurance` e não é tocado.)

- [ ] **Step 4: Resolver — a question landing parks what comes after it**

Em `src/secretaria/services/booking_draft.py`, substituir o bloco de `_resolve` (texto do P2a, com a chamada já ajustada pela Task 3) que vai da linha `    # 2. Convênio, when the clinic collects it.` até o fim da função (`        done=done,\n    )`, a chamada a `_land_day`) por:

```python
    def asked(result: FlowRouterResult, *names: str) -> DraftResolution:
        """A convênio, doctor or service question: what comes after it waits in flow_draft.

        TASK-030 P3 (owner's ruling of 2026-10-03), behind the AI draft v2 switch: when the
        pending draft still holds a service or a day, it is parked on the question, and the
        patient's answer re-runs the resolver over it (`flow_router._resume_parked_draft`,
        `draft_resolution._resume_booking_draft`) instead of falling into the next button
        step - so neither the day nor the booking details get lost on the way. With nothing
        after the question to keep, nothing waits.
        """
        pending = _pending(draft, checked)
        if (
            fr.ai_draft_v2_enabled(tenant)
            and (pending.service is not None or pending.day is not None)
            and result.flow_state is FlowState.SERVICE_CATALOG
            and result.flow_step in fr.DRAFT_WAIT_STEPS
        ):
            result.flow_draft = draft_record(pending, saved_at=now)
            names = (*names, FIELD_DAY, FIELD_TIME)
        return done(result, *names)

    # 2. Convênio, when the clinic collects it.
    if fr._insurance_step_skip_reason(tenant) is None and checked.insurance is None:
        return asked(
            fr._enter_insurance(tenant, state), FIELD_FOR_WHOM, FIELD_PROFESSIONAL, FIELD_SERVICE
        )
    # 3. Profissional (multi-doctor clinics; implicit otherwise).
    if checked.multi and checked.professional is None:
        result = fr._enter_professional_list(tenant, professionals, insurance=checked.insurance)
        return asked(result, FIELD_FOR_WHOM, FIELD_INSURANCE, FIELD_SERVICE)
    # 4. Serviço.
    if checked.service is None:
        result = (
            fr._enter_professional_services(checked.professional, tenant)
            if checked.multi
            else fr._start_booking(tenant, professionals, insurance=checked.insurance)
        )
        return asked(result, FIELD_FOR_WHOM, FIELD_INSURANCE, FIELD_PROFESSIONAL)
    # 5-6. Dia e horário.
    return await _land_day(
        draft,
        checked,
        state,
        conversation=conversation,
        tenant=tenant,
        professionals=professionals,
        service_catalog=service_catalog,
        calendar=calendar,
        now=now,
        done=done,
    )
```

- [ ] **Step 5: Run the router/resolver tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_draft_wait_questions.py tests/test_booking_draft_resolver.py tests/test_flow_draft_column.py tests/test_express_confirmation.py -q`
Expected: PASS.

- [ ] **Step 6: Write the failing worker tests**

Criar `tests/test_booking_draft_resume_questions.py` (mesmo arnês de `tests/test_booking_draft_continuation.py`, do P2a):

```python
"""The answer to a convênio, doctor or service question resumes the AI draft (TASK-030 P3).

End to end through the worker: the AI's draft is handed back (`_handle_set_booking_draft`,
P2b) on a switched-on clinic, the resolver lands on a booking question and parks the rest
in `flow_draft`, and the patient's answer - a real WhatsApp turn - re-runs the resolver
over it. Harness: tests/test_booking_draft_continuation.py (P2a).
"""

import os

from tests._patching import workers_ns

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

import datetime as dt  # noqa: E402
import json  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
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

from secretaria.ai.graph import BOOKING_DRAFT_SENTINEL_PREFIX  # noqa: E402
from secretaria.config import Settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Conversation, FlowState, Professional, Tenant  # noqa: E402
from secretaria.services import booking_hold as booking_hold_service  # noqa: E402
from secretaria.services.booking_draft import BookingDraft, draft_record  # noqa: E402
from secretaria.services.channel_sender import CHANNEL_WHATSAPP  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    STEP_AWAITING_CONFIRMATION,
    STEP_AWAITING_DAY,
    STEP_AWAITING_INSURANCE,
    STEP_AWAITING_PROFESSIONAL,
    STEP_AWAITING_SERVICE,
)
from secretaria.services.greeting_template import CONSENT_BUTTON_LABEL  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import draft_resolution  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511988887777"
# Inside the 20-day window whatever the hour the suite runs at.
DAY = (datetime.now(TZ) + timedelta(days=3)).date()
WHEN = f"{DAY.strftime('%d/%m/%Y')} às 10:00"
EVERY_DAY = {
    day: [{"start": "08:00", "end": "18:00"}]
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}
FULL_DRAFT = {"t": "Primeira Consulta", "w": "self", "d": DAY.isoformat(), "h": "10:00"}


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


class _WireClient:
    sends: list[tuple] = []

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls()

    async def send_text_message(self, to, body):
        _WireClient.sends.append(("text", body))
        return {"messages": [{"id": f"wamid.out.{len(_WireClient.sends)}"}]}

    async def send_buttons(self, to, body, buttons):
        _WireClient.sends.append(("buttons", body))
        return {"messages": [{"id": f"wamid.out.{len(_WireClient.sends)}"}]}

    async def send_list(self, to, body, button_label, rows, section_title="Opções"):
        _WireClient.sends.append(("list", body))
        return {"messages": [{"id": f"wamid.out.{len(_WireClient.sends)}"}]}


class _Agenda:
    """Every doctor's agenda: 10:00 and 10:30 free on DAY."""

    def __init__(self):
        self.tzinfo = TZ

    async def list_available_days(self, start_day, days, slot_minutes=None):
        return [datetime(DAY.year, DAY.month, DAY.day, tzinfo=TZ)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        if day.date() != DAY:
            return []
        iso = DAY.isoformat()
        return [{"start": f"{iso}T{t}", "end": "", "label": t} for t in ("10:00", "10:30")][
            :max_slots
        ]


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, db):
    agenda = _Agenda()
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(booking_hold_service, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))
    _WireClient.sends = []
    monkeypatch.setattr(workers_ns, "WhatsAppClient", _WireClient)

    async def _fake_resolve(session, tenant_id, patient_id, **kwargs):
        return None

    async def _fake_token(session, tenant_id):
        return "decrypted-waba-token"

    async def _fake_entitlements(tenant_id, redis):
        return EntitlementSummary(
            tenant_id=str(tenant_id),
            status="active",
            active=True,
            secretaria_enabled=True,
            plan="bronze",
            secretaria_tier="basico",
            addons={},
            limits={},
        )

    async def _agenda_for(session, tenant, target):
        return agenda

    monkeypatch.setattr(workers_ns, "resolve_patient_opening_state", _fake_resolve)
    monkeypatch.setattr(workers_ns, "get_waba_token", _fake_token)
    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_entitlements)
    monkeypatch.setattr(workers_ns, "_appointment_calendar", _agenda_for)
    # The turn's own calendar (the route() half of an answer) is the same fake agenda.
    monkeypatch.setattr(workers_ns, "_flow_turn_calendar", lambda conv, config, cal: agenda)
    return db


async def _seed_tenant(db, *, collect=False, doctors=()) -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=PHONE_NUMBER_ID,
            is_active=True,
            clinic_description="Oftalmologia.",
            initial_flows={"ai_draft_v2": True},
            appointment_types=[{"name": "Primeira Consulta", "duration_min": 30, "is_active": True}],
            business_hours=EVERY_DAY,
            collect_insurance=collect,
            insurance_mode="shared" if collect else None,
            insurances=["Unimed"] if collect else None,
        )
        session.add(tenant)
        await session.flush()
        for name, services in doctors:
            session.add(
                Professional(
                    tenant_id=tenant.id,
                    name=name,
                    is_active=True,
                    appointment_types=[
                        {"name": service, "duration_min": 30, "is_active": True}
                        for service in services
                    ],
                )
            )
        await session.commit()
        await session.refresh(tenant)
        return tenant


_wam_seq = iter(range(1, 10_000))


async def _wa_turn(tenant: Tenant, body: str):
    reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=WA_ID,
        patient_name="Perfil",
        wam_id=f"wamid.in.{next(_wam_seq)}",
        body=body,
    )
    if reply is not None:
        await tasks._send_bot_reply(reply, redis=None)


async def _onboard(tenant) -> None:
    await _wa_turn(tenant, "oi")
    await _wa_turn(tenant, "joão conta")
    await _wa_turn(tenant, CONSENT_BUTTON_LABEL)


async def _conversation(db, tenant) -> Conversation:
    async with db() as session:
        return await session.scalar(select(Conversation).where(Conversation.tenant_id == tenant.id))


async def _professional(db, tenant, name) -> Professional:
    async with db() as session:
        return await session.scalar(
            select(Professional).where(Professional.tenant_id == tenant.id, Professional.name == name)
        )


async def _hand_back(db, tenant, payload: dict) -> None:
    conversation = await _conversation(db, tenant)
    reply = tasks._ReplyContext(
        channel=CHANNEL_WHATSAPP,
        conversation_id=conversation.id,
        patient_ref=WA_ID,
        inbound_body="quinta às 10h, pra mim",
        tenant_id=tenant.id,
    )
    await tasks._handle_set_booking_draft(
        reply,
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps(payload),
        tenant,
        None,
        [],
        WA_ID,
        waba_token="t",
    )


async def _park(db, tenant, *, step, draft, saved_at, selected_type=None) -> None:
    conversation = await _conversation(db, tenant)
    async with db() as session:
        async with session.begin():
            row = await session.get(Conversation, conversation.id)
            row.flow_state = FlowState.SERVICE_CATALOG
            row.flow_step = step
            row.flow_selected_type = selected_type
            row.flow_attendee_name = ""
            row.flow_draft = draft_record(draft, saved_at=saved_at)


async def test_the_convenio_answer_lands_the_parked_draft_on_the_express_card(wired) -> None:
    db = wired
    tenant = await _seed_tenant(db, collect=True)
    await _onboard(tenant)
    await _hand_back(db, tenant, FULL_DRAFT)
    parked = await _conversation(db, tenant)
    assert parked.flow_step == STEP_AWAITING_INSURANCE
    assert parked.flow_draft["d"] == DAY.isoformat()

    await _wa_turn(tenant, "Unimed")

    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_CONFIRMATION
    assert conversation.flow_selected_insurance == "Unimed"
    assert conversation.flow_selected_slot == f"{DAY.isoformat()}T10:00"
    assert conversation.flow_draft is None  # consumed
    (details_kind, details), (card_kind, card) = _WireClient.sends[-2:]
    assert (details_kind, card_kind) == ("text", "buttons")
    assert "Convênio: Unimed" in details
    assert card == f"Primeira Consulta\n{WHEN}"


async def test_the_doctor_answer_lands_the_parked_draft_with_that_doctor(wired) -> None:
    db = wired
    tenant = await _seed_tenant(
        db, doctors=[("Dra. Ana", ["Primeira Consulta"]), ("Dr. Beto", ["Primeira Consulta"])]
    )
    await _onboard(tenant)
    await _hand_back(db, tenant, FULL_DRAFT)
    assert (await _conversation(db, tenant)).flow_step == STEP_AWAITING_PROFESSIONAL

    await _wa_turn(tenant, "Dr. Beto")

    conversation = await _conversation(db, tenant)
    beto = await _professional(db, tenant, "Dr. Beto")
    assert conversation.flow_step == STEP_AWAITING_CONFIRMATION
    assert conversation.flow_selected_professional_id == beto.id
    assert _WireClient.sends[-1] == ("buttons", f"Primeira Consulta\nProfissional: Dr. Beto\n{WHEN}")


async def test_a_doctor_who_does_not_offer_the_parked_service_keeps_the_day_waiting(
    wired, monkeypatch
) -> None:
    log = _Log()
    monkeypatch.setattr(draft_resolution, "logger", log)
    db = wired
    tenant = await _seed_tenant(
        db,
        doctors=[
            ("Dra. Ana", ["Primeira Consulta", "Retorno"]),
            ("Dr. Beto", ["Primeira Consulta"]),
            ("Dr. Caio", ["Retorno"]),
        ],
    )
    await _onboard(tenant)
    await _hand_back(db, tenant, FULL_DRAFT)

    await _wa_turn(tenant, "Dr. Caio")

    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_SERVICE  # Dr. Caio's own services
    assert conversation.flow_draft["d"] == DAY.isoformat()  # the day still waits
    (resumed,) = [fields for event, fields in log.events if event == "booking_draft_resumed"]
    assert resumed["answered_step"] == STEP_AWAITING_PROFESSIONAL
    assert resumed["dropped"] == {"service": "not_offered_by_professional"}

    await _wa_turn(tenant, "Retorno")

    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_CONFIRMATION
    assert _WireClient.sends[-1] == ("buttons", f"Retorno\nProfissional: Dr. Caio\n{WHEN}")


async def test_a_draft_expired_while_waiting_for_the_convenio_answer_continues_the_buttons(
    wired, monkeypatch
) -> None:
    log = _Log()
    monkeypatch.setattr(draft_resolution, "logger", log)
    db = wired
    tenant = await _seed_tenant(db, collect=True)
    await _onboard(tenant)
    await _park(
        db,
        tenant,
        step=STEP_AWAITING_INSURANCE,
        draft=BookingDraft(service="Primeira Consulta", attendee="self", day=DAY, time=dt.time(10, 0)),
        saved_at=datetime.now(UTC) - timedelta(minutes=31),
        selected_type="Primeira Consulta",
    )

    await _wa_turn(tenant, "Unimed")

    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_DAY  # the plain next question
    assert conversation.flow_selected_insurance == "Unimed"
    assert conversation.flow_draft is None
    skipped = [fields for event, fields in log.events if event == "booking_draft_resume_skipped"]
    assert [fields["reason"] for fields in skipped] == ["expired_or_invalid"]


async def test_the_menu_drops_a_parked_draft(wired) -> None:
    db = wired
    tenant = await _seed_tenant(db, collect=True)
    await _onboard(tenant)
    await _park(
        db,
        tenant,
        step=STEP_AWAITING_INSURANCE,
        draft=BookingDraft(service="Primeira Consulta", attendee="self", day=DAY),
        saved_at=datetime.now(UTC),
        selected_type="Primeira Consulta",
    )
    conversation = await _conversation(db, tenant)
    reply = tasks._ReplyContext(
        channel=CHANNEL_WHATSAPP,
        conversation_id=conversation.id,
        patient_ref=WA_ID,
        inbound_body="/menu",
        tenant_id=tenant.id,
    )

    await tasks._handle_show_main_menu(reply, tenant, [], WA_ID, waba_token="t", source="command")

    conversation = await _conversation(db, tenant)
    assert conversation.flow_state == FlowState.MENU
    assert conversation.flow_draft is None
```

- [ ] **Step 7: Run them to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_booking_draft_resume_questions.py -q`
Expected: `test_the_doctor_answer_lands_the_parked_draft_with_that_doctor` e `test_a_doctor_who_does_not_offer_the_parked_service_keeps_the_day_waiting` FAIL — o `_resume_booking_draft` do P2a não dobra o médico tocado no rascunho (o resolvedor volta à lista de médicos) e o log não tem `answered_step`. `test_the_convenio_answer_...`, `test_a_draft_expired_...` e `test_the_menu_drops_a_parked_draft` já PASSAM (o P2a já copiava o convênio respondido; prazo e menu também são do P2a) — o Step 8 não pode quebrá-los.

- [ ] **Step 8: Worker — fold the answer into the draft**

Em `src/secretaria/workers/shared/draft_resolution.py`:

1. Trocar `from secretaria.services.flow_router import FlowRouterResult, booking_gate_scope` por:

```python
from secretaria.services.flow_router import (
    ATTENDEE_STEPS,
    STEP_AWAITING_INSURANCE,
    STEP_AWAITING_PROFESSIONAL,
    STEP_AWAITING_SERVICE,
    FlowRouterResult,
    booking_gate_scope,
)
```

2. Substituir a função `_resume_booking_draft` inteira (texto do P2a) por:

```python
def _fold_answer(
    draft: BookingDraft, answered_step: str | None, result: FlowRouterResult
) -> BookingDraft:
    """Write the patient's answer INTO the draft: it is the newest thing they said.

    Pra-quem (P2): the answer decides `attendee` ("Sim, é pra mim" after the AI said
    "other" means the patient). Doctor / service (TASK-030 P3): the one tapped replaces the
    draft's, so the resolver re-checks everything else against it - a stored service the
    new doctor does not offer is dropped and asked again from its own step. Convênio: the
    answer is recorded on the conversation exactly as given (a typed "Outro convênio"
    included) and the draft's own convênio text gives way to it.
    """
    if answered_step in ATTENDEE_STEPS:
        answered_other = real_attendee_name(result.flow_attendee_name) is not None
        return replace(
            draft, attendee=DRAFT_ATTENDEE_OTHER if answered_other else DRAFT_ATTENDEE_SELF
        )
    if answered_step == STEP_AWAITING_INSURANCE:
        return replace(draft, insurance=None)
    if answered_step == STEP_AWAITING_PROFESSIONAL and result.flow_selected_professional_id:
        return replace(draft, professional_id=result.flow_selected_professional_id)
    if answered_step == STEP_AWAITING_SERVICE and result.flow_selected_type:
        return replace(draft, service=result.flow_selected_type)
    return draft


async def _resume_booking_draft(
    reply: _ReplyContext,
    tenant: Tenant,
    result: FlowRouterResult,
    *,
    gate: BookingGate | None = None,
) -> FlowRouterResult:
    """An answer to a question the AI draft was waiting on: land the draft, not the next list.

    Pra-quem (P2) and, since TASK-030 P3, the convênio, doctor and service questions
    (`flow_router.DRAFT_WAIT_STEPS`). `result` is what `route()` computed for the answer
    (the plain button continuation); it is the fallback for a missing, expired or corrupt
    draft and for ANY resolver failure - the patient always gets the next question, never
    nothing. The answer is folded into the draft (`_fold_answer`) and the resolver checks
    everything again against FRESH data. The authorization of a third party stays recorded
    exactly as the plain path would have recorded it.
    """
    plain = replace(result, flow_draft=None, resume_draft=False)
    answered_step: str | None = None
    try:
        ctx = await _load_draft_context(reply, tenant)
        stored = ctx.conversation.flow_draft if ctx is not None else None
        draft = draft_from_record(stored, now=datetime.now(UTC))
        if ctx is None or draft is None:
            logger.info(
                "booking_draft_resume_skipped",
                conversation_id=str(reply.conversation_id),
                tenant_id=str(tenant.id),
                reason="expired_or_invalid" if stored else "missing",
            )
            return plain
        # The conversation row is still on the question being answered: route()'s result
        # has not been persisted yet.
        answered_step = ctx.conversation.flow_step
        draft = _fold_answer(draft, answered_step, result)
        if answered_step in ATTENDEE_STEPS:
            ctx.conversation.flow_attendee_name = result.flow_attendee_name
        if result.flow_selected_insurance is not None:
            ctx.conversation.flow_selected_insurance = result.flow_selected_insurance
        resolution = await _resolve_draft(reply, tenant, draft, ctx, gate=gate)
    except Exception as exc:
        logger.warning(
            "booking_draft_resume_failed",
            conversation_id=str(reply.conversation_id),
            error_type=type(exc).__name__,
        )
        return plain
    resumed = resolution.result
    resumed.attendee_authorized = (
        result.attendee_authorized and resumed.flow_state is FlowState.SERVICE_CATALOG
    )
    logger.info(
        "booking_draft_resumed",
        conversation_id=str(reply.conversation_id),
        tenant_id=str(tenant.id),
        answered_step=answered_step,
        landing_step=resolution.landing_step,
        accepted=list(resolution.accepted),
        dropped=dict(resolution.dropped),
        fallback=resolution.fallback,
    )
    return resumed
```

- [ ] **Step 9: Run every draft test**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_booking_draft_resume_questions.py tests/test_draft_wait_questions.py tests/test_booking_draft_continuation.py tests/test_flow_draft_column.py tests/test_booking_draft_resolver.py tests/test_attendee_booking.py tests/test_handback_events.py tests/test_flow_router.py tests/test_flow_router_insurance.py tests/test_flow_router_multiprofessional.py -q`
Expected: PASS (inclusive os cinco de continuação do P2a: o "pra quem" segue com a mesma regra).

- [ ] **Step 10: Lint and commit**

```bash
uvx ruff format src/secretaria/services/booking_draft.py src/secretaria/workers/shared/draft_resolution.py tests/test_draft_wait_questions.py tests/test_booking_draft_resume_questions.py
uvx ruff check --fix --select I tests/test_draft_wait_questions.py tests/test_booking_draft_resume_questions.py src/secretaria/workers/shared/draft_resolution.py
uvx ruff check src/secretaria/services/flow_router.py src/secretaria/services/booking_draft.py src/secretaria/workers/shared/draft_resolution.py tests/test_draft_wait_questions.py tests/test_booking_draft_resume_questions.py
uvx ruff format --diff src/secretaria/services/flow_router.py | grep -c '^@@'   # must not exceed the count before editing
git add src/secretaria/services/flow_router.py src/secretaria/services/booking_draft.py src/secretaria/workers/shared/draft_resolution.py tests/test_draft_wait_questions.py tests/test_booking_draft_resume_questions.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(draft): the AI draft waits on the convênio, doctor and service questions too

Owner's ruling of 2026-10-03, behind the AI draft v2 switch: when the resolver lands on a
booking question and the draft still holds a service or a day, it waits in flow_draft;
the convênio answer and the doctor/service taps resume it. The answer is folded into the
draft and every item is re-checked on fresh data, so an item the answer invalidates is
dropped and asked again. "Outro convênio" keeps waiting; help and anything else drop it.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 6: Pedido de gerenciar v2 — formato, ferramenta, sentinel e a referência das consultas

**Files:**
- Create: `src/secretaria/services/manage_request.py` (só o formato e a escolha da consulta; o resolvedor é da Task 7)
- Modify: `src/secretaria/ai/tools.py` (import; `ManageAppointmentRequested`; ferramenta v2 depois de `set_booking_draft_v2.metadata = ...`)
- Modify: `src/secretaria/ai/graph.py` (ramo `except ManageAppointmentRequested`; comentário do prefixo)
- Modify: `src/secretaria/workers/shared/llm_context.py` (imports; `_appointment_context_text`; `_flow_handback_tools`)
- Modify: `src/secretaria/workers/orchestrator.py` (import; chamada de `_appointment_context_text`)
- Test: `tests/test_manage_request.py`; Modify `tests/test_agent_menu_tools.py`, `tests/test_agent_capability_cache.py`

**Interfaces:**
- Consumes: `services.patient_context.as_utc`; P2b (`_ISO_DAY_RE`, `_HHMM_RE`, `_DAY_FORMAT_ERROR`, `_TIME_FORMAT_ERROR`, `TOOL_BLOCK_BAD_DAY`, `TOOL_BLOCK_BAD_TIME` em `ai/tools.py`; `_tool_cache_key` em `ai/graph.py`; o seletor por interruptor em `_flow_handback_tools`).
- Produces: `manage_request.{ACTION_*, ACTIONS, FIELD_*, FIELD_NAMES, APPOINTMENT_REF_FORMAT, parse_appointment_ref, appointment_ref, ManageRequest, find_appointment, manage_target}`; `ManageAppointmentRequested(action, *, appointment=None, day=None, time=None)` com `.request`; `manage_existing_appointment_v2` (nome `manage_existing_appointment`, `metadata={"cache_variant": "manage_v2"}`); `TOOL_BLOCK_BAD_ACTION`, `TOOL_BLOCK_BAD_APPOINTMENT`; `_appointment_context_text(..., *, with_refs=False)`.

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_manage_request.py`:

```python
"""Manage request v2: wire format, the appointment reference and the tool (TASK-030 P3)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import datetime as dt  # noqa: E402
import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.ai import tools as ai_tools  # noqa: E402
from secretaria.ai.tools import (  # noqa: E402
    ManageAppointmentRequested,
    manage_existing_appointment,
    manage_existing_appointment_v2,
)
from secretaria.services.booking_scope import BOOKING_TOPOLOGY_SOLE  # noqa: E402
from secretaria.services.manage_request import (  # noqa: E402
    ManageRequest,
    appointment_ref,
    find_appointment,
    manage_target,
    parse_appointment_ref,
)
from secretaria.workers.shared.llm_context import (  # noqa: E402
    _appointment_context_text,
    _flow_handback_tools,
)

TZ = ZoneInfo("America/Sao_Paulo")


def _appt(local: dt.datetime, kind: str = "Consulta") -> dict:
    start = local.replace(tzinfo=TZ).astimezone(dt.UTC)
    return {
        "id": str(uuid4()),
        "google_event_id": f"evt-{uuid4()}",
        "appointment_type": kind,
        "start_at": start,
        "end_at": start + dt.timedelta(minutes=30),
        "professional_id": None,
    }


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log


# --------------------------------------------------------------------------
# Wire format
# --------------------------------------------------------------------------


def test_a_bare_action_is_the_v1_sentinel_byte_for_byte():
    assert ManageRequest("cancel").to_payload() == "cancel"
    assert ManageRequest.from_payload("reschedule") == ManageRequest("reschedule")


def test_the_v2_payload_round_trips():
    request = ManageRequest(
        "reschedule",
        appointment=dt.datetime(2026, 10, 8, 10, 0),
        day=dt.date(2026, 10, 15),
        time=dt.time(14, 0),
    )
    assert json.loads(request.to_payload()) == {
        "a": "reschedule",
        "ap": "2026-10-08 10:00",
        "d": "2026-10-15",
        "h": "14:00",
    }
    assert ManageRequest.from_payload(request.to_payload()) == request


@pytest.mark.parametrize(
    "raw",
    [
        "excluir",
        "",
        "[]",
        '{"a": "delete"}',
        '{"a": "cancel", "ap": "08/10/2026 10:00"}',
        '{"a": "cancel", "ap": 7}',
        '{"a": "reschedule", "d": "2026-13-01"}',
        '{"a": "reschedule", "h": "10h"}',
        '{"a": "reschedule", "h": "25:00"}',
    ],
)
def test_a_corrupt_payload_raises_value_error(raw):
    with pytest.raises(ValueError):
        ManageRequest.from_payload(raw)


def test_supplied_fields_name_only_what_is_present_in_order():
    request = ManageRequest("reschedule", day=dt.date(2026, 10, 15))
    assert request.supplied_fields() == ("action", "day")


# --------------------------------------------------------------------------
# The appointment reference
# --------------------------------------------------------------------------


def test_the_reference_is_the_clinic_local_minute():
    aware = dt.datetime(2026, 10, 8, 13, 0, tzinfo=dt.UTC)
    assert appointment_ref(aware, TZ) == "2026-10-08 10:00"
    # SQLite hands timestamps back naive: they are UTC (patient_context.as_utc).
    assert appointment_ref(aware.replace(tzinfo=None), TZ) == "2026-10-08 10:00"


@pytest.mark.parametrize("text", ["2026-10-08 10:00", "2026-10-08T10:00", " 2026-10-08 10:00 "])
def test_parse_appointment_ref(text):
    assert parse_appointment_ref(text) == dt.datetime(2026, 10, 8, 10, 0)


def test_find_appointment_matches_only_the_patients_own_minute():
    first = _appt(dt.datetime(2026, 10, 8, 10, 0))
    second = _appt(dt.datetime(2026, 10, 15, 14, 0))
    appointments = [first, second]
    assert find_appointment(appointments, dt.datetime(2026, 10, 15, 14, 0), TZ) is second
    assert find_appointment(appointments, dt.datetime(2026, 10, 9, 10, 0), TZ) is None
    assert find_appointment(appointments, None, TZ) is None
    twin = _appt(dt.datetime(2026, 10, 8, 10, 0))
    # Two at the same minute is ambiguous, and ambiguous is never guessed.
    assert find_appointment([first, twin], dt.datetime(2026, 10, 8, 10, 0), TZ) is None


def test_manage_target_never_replaces_a_wrong_reference_with_a_guess():
    only = _appt(dt.datetime(2026, 10, 8, 10, 0))
    two = [only, _appt(dt.datetime(2026, 10, 15, 14, 0))]
    assert manage_target(ManageRequest("reschedule"), [only], TZ) is only
    assert manage_target(ManageRequest("reschedule"), two, TZ) is None
    assert manage_target(ManageRequest("cancel"), [only], TZ) is None  # enter_manage_action decides
    wrong = ManageRequest("reschedule", appointment=dt.datetime(2026, 10, 9, 9, 0))
    assert manage_target(wrong, [only], TZ) is None


# --------------------------------------------------------------------------
# The tool (v2) and the clinic switch
# --------------------------------------------------------------------------


def test_the_v2_tool_has_the_v1_name_and_four_args():
    assert manage_existing_appointment_v2.name == manage_existing_appointment.name
    assert set(manage_existing_appointment_v2.args) == {"action", "appointment", "day", "time"}


async def test_the_v2_tool_carries_every_field_of_a_reschedule():
    with pytest.raises(ManageAppointmentRequested) as exc:
        await manage_existing_appointment_v2.ainvoke(
            {"action": "Remarcar", "appointment": "2026-10-08 10:00", "day": "2026-10-15", "time": "14:00"}
        )
    assert exc.value.request == ManageRequest(
        "reschedule",
        appointment=dt.datetime(2026, 10, 8, 10, 0),
        day=dt.date(2026, 10, 15),
        time=dt.time(14, 0),
    )


async def test_the_v2_tool_drops_day_and_time_on_a_cancel():
    with pytest.raises(ManageAppointmentRequested) as exc:
        await manage_existing_appointment_v2.ainvoke(
            {"action": "cancel", "appointment": "2026-10-08 10:00", "day": "2026-10-15", "time": "14:00"}
        )
    assert exc.value.request == ManageRequest("cancel", appointment=dt.datetime(2026, 10, 8, 10, 0))


@pytest.mark.parametrize(
    "args, reason",
    [
        ({"action": "excluir Maria"}, "bad_action"),
        ({"action": "cancel", "appointment": "quinta 10h"}, "bad_appointment"),
        ({"action": "reschedule", "day": "15/10/2026"}, "bad_day"),
        ({"action": "reschedule", "day": "2026-10-15", "time": "14h"}, "bad_time"),
    ],
)
async def test_a_bad_format_is_a_recoverable_error_that_echoes_nothing(monkeypatch, args, reason):
    log = _Log()
    monkeypatch.setattr(ai_tools, "logger", log)
    out = await manage_existing_appointment_v2.ainvoke(args)
    assert "error" in out
    assert "Maria" not in out["error"] and "quinta" not in out["error"]
    assert [fields["reason"] for event, fields in log.events if event == "agent_tool_blocked"] == [
        reason
    ]


def test_the_clinic_switch_picks_the_manage_tool():
    off = _flow_handback_tools(SimpleNamespace(initial_flows={}), BOOKING_TOPOLOGY_SOLE, [])
    on = _flow_handback_tools(
        SimpleNamespace(initial_flows={"ai_draft_v2": True}), BOOKING_TOPOLOGY_SOLE, []
    )
    assert manage_existing_appointment in off and manage_existing_appointment_v2 not in off
    assert manage_existing_appointment_v2 in on and manage_existing_appointment not in on


# --------------------------------------------------------------------------
# The appointment context block shows the reference the tool takes
# --------------------------------------------------------------------------


def test_the_appointment_context_carries_refs_only_when_asked():
    future = [_appt(dt.datetime(2026, 10, 8, 10, 0)), _appt(dt.datetime(2026, 10, 15, 14, 0), "Retorno")]
    plain = _appointment_context_text(future, "America/Sao_Paulo", {}, [])
    assert "(ref " not in plain
    with_refs = _appointment_context_text(future, "America/Sao_Paulo", {}, [], with_refs=True)
    assert with_refs.splitlines() == [
        "Próxima consulta: 08/10 às 10:00 — Consulta (ref 2026-10-08 10:00)",
        "15/10 às 14:00 — Retorno (ref 2026-10-15 14:00)",
    ]
```

Em `tests/test_agent_menu_tools.py`, logo depois de `test_run_agent_maps_manage_appointment_to_sentinel`, acrescentar:

```python
async def test_run_agent_maps_a_v2_manage_request_to_its_sentinel(monkeypatch: pytest.MonkeyPatch):
    import datetime as dt

    from secretaria.services.manage_request import ManageRequest

    async def _fake_history(conversation_id):
        return [HumanMessage(content="remarca a de quinta pra dia 15 às 14h")]

    async def _raise(messages, conversation_id):
        raise ManageAppointmentRequested(
            "reschedule",
            appointment=dt.datetime(2026, 10, 8, 10, 0),
            day=dt.date(2026, 10, 15),
            time=dt.time(14, 0),
        )

    monkeypatch.setattr(graph, "_load_history", _fake_history)
    monkeypatch.setattr(graph, "_invoke_agent_with_retry", _raise)
    reply = await run_agent("oi", context={"conversation_id": str(uuid4())})
    assert reply.startswith(MANAGE_APPOINTMENT_SENTINEL_PREFIX)
    assert ManageRequest.from_payload(reply[len(MANAGE_APPOINTMENT_SENTINEL_PREFIX) :]) == (
        ManageRequest(
            "reschedule",
            appointment=dt.datetime(2026, 10, 8, 10, 0),
            day=dt.date(2026, 10, 15),
            time=dt.time(14, 0),
        )
    )
```

(`test_run_agent_maps_manage_appointment_to_sentinel` continua esperando exatamente `__MANAGE_APPOINTMENT__:cancel` — é a prova de que o sentinel v1 não mudou.)

Em `tests/test_agent_capability_cache.py`, acrescentar ao fim:

```python
def test_two_manage_tools_never_share_a_compiled_agent(_clean_agent_cache_and_fakes):
    """v1 and v2 share the model-facing NAME `manage_existing_appointment` (TASK-030 P3)."""
    from secretaria.ai.tools import manage_existing_appointment, manage_existing_appointment_v2

    v1 = graph.build_agent(extra_tools=[manage_existing_appointment])
    v2 = graph.build_agent(extra_tools=[manage_existing_appointment_v2])
    assert v1 is not v2
    assert graph.build_agent(extra_tools=[manage_existing_appointment_v2]) is v2
    assert len(_clean_agent_cache_and_fakes) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_manage_request.py tests/test_agent_menu_tools.py tests/test_agent_capability_cache.py -q`
Expected: erro de coleta em `test_manage_request.py` — `ImportError: cannot import name 'manage_existing_appointment_v2' from 'secretaria.ai.tools'` (ou `ModuleNotFoundError: secretaria.services.manage_request`).

- [ ] **Step 3: The wire format module**

Criar `src/secretaria/services/manage_request.py`:

```python
"""The AI's request about an EXISTING appointment (v2) - TASK-030 P3, spec §4.5.

`manage_existing_appointment` (ai/tools.py) hands a reschedule or a cancellation back to
the deterministic manage flow. v1 carried only the action; v2 may also name WHICH of the
patient's upcoming appointments - by its start, exactly as the appointment context block
shows it to the model (`APPOINTMENT_REF_FORMAT`, the "(ref ...)" written by
workers/shared/llm_context.py::_appointment_context_text) - and, for a reschedule, the
new day and time. The worker (workers/shared/sentinels.py::_handle_manage_appointment)
lands it on the SAME steps the buttons use, never further than a confirmation card.

Wire format (after `__MANAGE_APPOINTMENT__:`, ai/graph.py): the bare action
("reschedule" / "cancel") when nothing else is said - byte for byte the v1 sentinel - and
compact JSON `{"a", "ap", "d", "h"}` otherwise. Produced and consumed in the same worker
turn; the parser accepts both.

The appointment is only ever looked for AMONG THE CONVERSATION'S OWN PATIENT'S upcoming
appointments (the caller loads `load_upcoming_appointments(tenant, patient)`): a reference
that matches none of them, or two of them, is dropped - never replaced by a guess.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from secretaria.services.patient_context import as_utc

ACTION_RESCHEDULE = "reschedule"
ACTION_CANCEL = "cancel"
ACTIONS = (ACTION_RESCHEDULE, ACTION_CANCEL)

# Field NAMES, as the tool spells them and as the hand-back event logs them
# (workers/shared/handback_log.py FIELD_*). Never values.
FIELD_ACTION = "action"
FIELD_APPOINTMENT = "appointment"
FIELD_DAY = "day"
FIELD_TIME = "time"
FIELD_NAMES = (FIELD_ACTION, FIELD_APPOINTMENT, FIELD_DAY, FIELD_TIME)

# How an upcoming appointment is named to the model and how the model names it back: its
# start, to the minute, in the clinic's timezone.
APPOINTMENT_REF_FORMAT = "%Y-%m-%d %H:%M"
_REF = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})")
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_HHMM = re.compile(r"\d{2}:\d{2}")


def parse_appointment_ref(text: str) -> dt.datetime:
    """"AAAA-MM-DD HH:MM" ("T" also accepted) as a NAIVE clinic-local datetime.

    ValueError on anything else.
    """
    match = _REF.fullmatch((text or "").strip())
    if match is None:
        raise ValueError("appointment reference must be YYYY-MM-DD HH:MM")
    return dt.datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}")


def appointment_ref(start_at: dt.datetime, tz: dt.tzinfo) -> str:
    """The reference the model sees for an appointment that starts at `start_at`."""
    return as_utc(start_at).astimezone(tz).strftime(APPOINTMENT_REF_FORMAT)


def _optional_text(data: dict, key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"manage payload key {key!r} must be a string")
    return value.strip() or None


@dataclass(frozen=True)
class ManageRequest:
    """What the AI says about an existing appointment. Only `action` is required."""

    action: str
    appointment: dt.datetime | None = None  # naive, clinic-local, to the minute
    day: dt.date | None = None
    time: dt.time | None = None

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError("manage action must be 'reschedule' or 'cancel'")

    def to_payload(self) -> str:
        if self.appointment is None and self.day is None and self.time is None:
            return self.action
        return json.dumps(
            {
                "a": self.action,
                "ap": (
                    self.appointment.strftime(APPOINTMENT_REF_FORMAT)
                    if self.appointment is not None
                    else None
                ),
                "d": self.day.isoformat() if self.day is not None else None,
                "h": self.time.strftime("%H:%M") if self.time is not None else None,
            }
        )

    @classmethod
    def from_payload(cls, raw: str) -> ManageRequest:
        """The bare v1 action or the v2 JSON. ValueError on anything malformed."""
        text = (raw or "").strip()
        if text in ACTIONS:
            return cls(action=text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("manage payload is neither an action nor JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("manage payload must be a JSON object")
        reference = _optional_text(data, "ap")
        day = _optional_text(data, "d")
        hhmm = _optional_text(data, "h")
        if day is not None and not _ISO_DAY.fullmatch(day):
            raise ValueError("manage payload 'd' must be YYYY-MM-DD")
        if hhmm is not None and not _HHMM.fullmatch(hhmm):
            raise ValueError("manage payload 'h' must be HH:MM")
        return cls(
            action=str(data.get("a")),
            appointment=parse_appointment_ref(reference) if reference is not None else None,
            day=dt.date.fromisoformat(day) if day is not None else None,
            time=dt.time.fromisoformat(hhmm) if hhmm is not None else None,
        )

    def supplied_fields(self) -> tuple[str, ...]:
        """The field NAMES present, in `FIELD_NAMES` order (what the hand-back logs)."""
        values = (self.action, self.appointment, self.day, self.time)
        return tuple(
            name for name, value in zip(FIELD_NAMES, values, strict=True) if value is not None
        )


def find_appointment(
    appointments: Sequence[dict], ref: dt.datetime | None, tz: dt.tzinfo
) -> dict | None:
    """The ONE appointment in `appointments` starting at `ref` (clinic-local minute).

    None when `ref` is None, when nothing starts then, and when two do - ambiguous is never
    guessed.
    """
    if ref is None:
        return None
    wanted = ref.strftime(APPOINTMENT_REF_FORMAT)
    matches = [
        appointment
        for appointment in appointments
        if isinstance(appointment.get("start_at"), dt.datetime)
        and appointment_ref(appointment["start_at"], tz) == wanted
    ]
    return matches[0] if len(matches) == 1 else None


def manage_target(
    request: ManageRequest, appointments: Sequence[dict], tz: dt.tzinfo
) -> dict | None:
    """WHICH appointment the request is about, or None (the buttons' pick list decides).

    The one it names; for a reschedule that names none, the patient's only one. A reference
    that names nothing in `appointments` is NOT replaced by a guess, even when the patient
    has a single appointment. A cancel without a reference is left to
    `flow_router.enter_manage_action`, exactly like the "Cancelar" button.
    """
    if request.appointment is not None:
        return find_appointment(appointments, request.appointment, tz)
    if request.action == ACTION_RESCHEDULE and len(appointments) == 1:
        return appointments[0]
    return None
```

- [ ] **Step 4: The tool, the exception and the sentinel**

1. `src/secretaria/ai/tools.py`:
   - imports: acrescentar `from secretaria.services.manage_request import ManageRequest, parse_appointment_ref` logo antes de `from secretaria.services.precheck import HandoffOutcome, request_precheck_handoff`;
   - em `ManageAppointmentRequested`, trocar

```python
    def __init__(self, action: str) -> None:
        super().__init__(f"manage appointment: {action}")
        self.action = action
```

   por

```python
    def __init__(
        self,
        action: str,
        *,
        appointment: datetime | None = None,
        day: date | None = None,
        time: Any = None,
    ) -> None:
        super().__init__(f"manage appointment: {action}")
        self.action = action
        # TASK-030 P3 (v2 tool only): WHICH appointment, by its clinic-local start, and for
        # a reschedule the new day and time. Formats only - the worker validates the rest.
        self.appointment = appointment
        self.day = day
        self.time = time

    @property
    def request(self) -> ManageRequest:
        """What graph.run_agent serializes (services/manage_request.py)."""
        return ManageRequest(
            action=self.action, appointment=self.appointment, day=self.day, time=self.time
        )
```

   - logo depois da linha `set_booking_draft_v2.metadata = {"cache_variant": "draft_v2"}` (P2b), inserir:

```python


# --- manage_existing_appointment v2 (TASK-030 P3) ---------------------------------------------
# Same model-facing NAME as the v1 tool above; the clinic's switch picks one
# (`flow_router.ai_draft_v2_enabled`, workers/shared/llm_context.py::_flow_handback_tools).
# Formats only, like set_booking_draft v2: whether the appointment is THIS patient's and
# whether the new time is free is the worker's call (services/manage_request.py), which
# never goes further than the confirmation card the buttons show.
TOOL_BLOCK_BAD_ACTION = "bad_action"
TOOL_BLOCK_BAD_APPOINTMENT = "bad_appointment"
_MANAGE_ACTIONS: dict[str, str] = {
    "reschedule": "reschedule",
    "remarcar": "reschedule",
    "cancel": "cancel",
    "cancelar": "cancel",
}
_MANAGE_ACTION_ERROR = (
    "Ação não reconhecida. Use 'reschedule' para remarcar ou 'cancel' para cancelar."
)
_APPOINTMENT_REF_ERROR = (
    'appointment precisa ser a referência da consulta que aparece em "consultas marcadas" '
    "(ref AAAA-MM-DD HH:MM), ou vazio."
)


def _tool_day(text: str) -> date | None:
    """`day` as the hand-back tools accept it (AAAA-MM-DD); None when empty, else ValueError."""
    value = (text or "").strip()
    if not value:
        return None
    if not _ISO_DAY_RE.fullmatch(value):
        raise ValueError("day must be YYYY-MM-DD")
    return date.fromisoformat(value)


def _tool_time(text: str) -> Any:
    """`time` as the hand-back tools accept it (HH:MM); None when empty, else ValueError."""
    value = (text or "").strip()
    if not value:
        return None
    if not _HHMM_RE.fullmatch(value):
        raise ValueError("time must be HH:MM")
    return datetime.strptime(value, "%H:%M").time()


@tool("manage_existing_appointment")
async def manage_existing_appointment_v2(
    action: str, appointment: str = "", day: str = "", time: str = ""
) -> dict:
    """Leva o paciente ao fluxo de remarcar ou cancelar uma consulta JÁ MARCADA dele. O
    fluxo confere tudo e para no cartão de confirmação: quem confirma é o paciente, tocando
    no botão. NUNCA remarque nem cancele você mesma pelo chat.

    Args:
        action: "reschedule" (ou "remarcar") ou "cancel" (ou "cancelar").
        appointment: QUAL consulta, pela referência "(ref AAAA-MM-DD HH:MM)" mostrada em
            "consultas marcadas". Vazio se o paciente não disse qual.
        day: Só para remarcar: o novo dia, AAAA-MM-DD, no fuso da clínica (ou vazio).
        time: Só para remarcar: o novo horário, HH:MM (ou vazio). Sem `day`, é ignorado.
    """
    canonical = _MANAGE_ACTIONS.get((action or "").strip().casefold())
    if canonical is None:
        # The value is never echoed nor logged: it may carry the patient's own words.
        logger.info(
            "agent_tool_blocked", tool="manage_existing_appointment", reason=TOOL_BLOCK_BAD_ACTION
        )
        return {"error": _MANAGE_ACTION_ERROR}
    reference_text = (appointment or "").strip()
    try:
        reference = parse_appointment_ref(reference_text) if reference_text else None
    except ValueError:
        logger.info(
            "agent_tool_blocked",
            tool="manage_existing_appointment",
            reason=TOOL_BLOCK_BAD_APPOINTMENT,
        )
        return {"error": _APPOINTMENT_REF_ERROR}
    new_day = new_time = None
    if canonical == "reschedule":
        try:
            new_day = _tool_day(day)
        except ValueError:
            logger.info(
                "agent_tool_blocked", tool="manage_existing_appointment", reason=TOOL_BLOCK_BAD_DAY
            )
            return {"error": _DAY_FORMAT_ERROR}
        try:
            new_time = _tool_time(time)
        except ValueError:
            logger.info(
                "agent_tool_blocked", tool="manage_existing_appointment", reason=TOOL_BLOCK_BAD_TIME
            )
            return {"error": _TIME_FORMAT_ERROR}
    raise ManageAppointmentRequested(canonical, appointment=reference, day=new_day, time=new_time)


# Read by ai/graph.py::_tool_cache_key: the v1 and v2 tools share the name
# "manage_existing_appointment" and must never share a compiled agent.
manage_existing_appointment_v2.metadata = {"cache_variant": "manage_v2"}
```

2. `src/secretaria/ai/graph.py`:
   - trocar o ramo

```python
    except ManageAppointmentRequested as exc:
        # The agent chose to hand a reschedule/cancel request back to the
        # deterministic manage flow — same propagation path as the sentinels
        # above; it NEVER performs the action itself.
        logger.info(
            "ai_run_agent_manage_appointment",
            conversation_id=str(conversation_id),
            action=exc.action,
        )
        return f"{MANAGE_APPOINTMENT_SENTINEL_PREFIX}{exc.action}"
```

   por

```python
    except ManageAppointmentRequested as exc:
        # The agent chose to hand a reschedule/cancel request back to the
        # deterministic manage flow — same propagation path as the sentinels
        # above; it NEVER performs the action itself. A v1 request (action only)
        # serializes to the bare action, byte for byte (services/manage_request.py).
        request = exc.request
        logger.info(
            "ai_run_agent_manage_appointment",
            conversation_id=str(conversation_id),
            action=request.action,
            has_appointment=request.appointment is not None,
            has_day=request.day is not None,
            has_time=request.time is not None,
        )
        return f"{MANAGE_APPOINTMENT_SENTINEL_PREFIX}{request.to_payload()}"
```

   - no comentário acima de `MANAGE_APPOINTMENT_SENTINEL_PREFIX = "__MANAGE_APPOINTMENT__:"`, trocar a linha `# canonical action ("reschedule" or "cancel") rides after the colon and the` por `# canonical action ("reschedule" or "cancel") - or, from the v2 tool, the JSON of` e acrescentar, logo depois dela, a linha `# services/manage_request.py::ManageRequest - rides after the colon and the`.

- [ ] **Step 5: The context block and the tool switch**

1. `src/secretaria/workers/shared/llm_context.py`:
   - imports: acrescentar `from zoneinfo import ZoneInfo` logo depois de `from uuid import UUID`; acrescentar `manage_existing_appointment_v2,` logo depois de `manage_existing_appointment,` no import de `secretaria.ai.tools`; acrescentar `from secretaria.services.manage_request import appointment_ref` logo antes de `from secretaria.services.patient_context import (`.
   - substituir a função `_appointment_context_text` inteira por:

```python
def _appointment_context_text(
    future_appointments: list[dict],
    tz_name: str | None,
    professional_names: dict[str, str],
    appointment_types: list[RuntimeAppointmentType],
    *,
    with_refs: bool = False,
) -> str | None:
    """Render the per-turn "consultas marcadas" block for the LLM prompt.

    Pure formatting over already-resolved data (see
    `_should_inject_appointment_context` for the gate and `_send_bot_reply`
    for the orchestration) - no DB access, and no appointment content is ever
    logged from here (only counts are logged elsewhere, e.g.
    `resolve_patient_opening_state`). Returns None (never "") when
    `future_appointments` is empty, so the caller can tell "nothing to
    inject" apart from "injected an empty string" the same way
    `ai/prompts.py::_format_post_consult_knowledge` etc. do with a falsy check.

    `future_appointments` must be nearest-first (see
    `patient_context.load_upcoming_appointments`). The NEAREST one (index 0)
    gets the fuller "Próxima consulta" line (service, doctor when resolvable,
    price when its service matches a catalog entry) plus an "Orientações"
    line when that matched entry has `requirements`. Every OTHER appointment
    gets one brief "when — service[ — doctor]" line, mirroring the greeting's
    own brief-list rendering (`_adapt_greeting_has_upcoming`).

    `professional_names` is id -> display name, expected to be built from the
    ACTIVE-professionals roster (`flow_professionals` in `_send_bot_reply`):
    an appointment whose owner is no longer active simply renders with no
    doctor name (`_appointment_doctor_name` degrades to None on a miss).
    `appointment_types` is `TenantRuntimeConfig.appointment_types`, matched
    against the nearest appointment's stored service name by casefold/strip.

    `with_refs` (TASK-030 P3, clinics with the AI draft v2 switch): every appointment line
    ends with "(ref AAAA-MM-DD HH:MM)" - the reference `manage_existing_appointment` v2
    takes to name WHICH appointment (services/manage_request.py::appointment_ref).
    """
    if not future_appointments:
        return None
    tz = ZoneInfo(tz_name or "America/Sao_Paulo")

    def ref(appointment: dict) -> str:
        return f" (ref {appointment_ref(appointment['start_at'], tz)})" if with_refs else ""

    nearest = future_appointments[0]
    when = _format_appointment_when(nearest["start_at"], tz_name)
    service_name = nearest.get("appointment_type") or "Consulta"
    doctor = _appointment_doctor_name(nearest, professional_names)
    matched = next(
        (
            t
            for t in appointment_types
            if t.name.strip().casefold() == service_name.strip().casefold()
        ),
        None,
    )

    nearest_line = f"Próxima consulta: {when} — {service_name}"
    if doctor:
        nearest_line += f" — {doctor}"
    if matched and matched.price:
        nearest_line += f" — {matched.price}"
    nearest_line += ref(nearest)

    lines = [nearest_line]
    if matched and matched.requirements:
        lines.append("Orientações: " + "; ".join(matched.requirements))

    for appt in future_appointments[1:]:
        appt_when = _format_appointment_when(appt["start_at"], tz_name)
        line = f"{appt_when} — {appt.get('appointment_type') or 'Consulta'}"
        appt_doctor = _appointment_doctor_name(appt, professional_names)
        if appt_doctor:
            line += f" — {appt_doctor}"
        lines.append(line + ref(appt))

    return "\n".join(lines)
```

   - em `_flow_handback_tools`, trocar as linhas do P2b

```python
    # TASK-030: same model-facing name, two implementations; the clinic's switch picks one.
    draft_tool = set_booking_draft_v2 if ai_draft_v2_enabled(tenant) else set_booking_draft
    handbacks = [manage_existing_appointment, draft_tool, request_human_handoff]
```

   por

```python
    # TASK-030: same model-facing names, two implementations each; the clinic's switch
    # picks both (P2: the booking draft; P3: the manage request).
    v2 = ai_draft_v2_enabled(tenant)
    draft_tool = set_booking_draft_v2 if v2 else set_booking_draft
    manage_tool = manage_existing_appointment_v2 if v2 else manage_existing_appointment
    handbacks = [manage_tool, draft_tool, request_human_handoff]
```

2. `src/secretaria/workers/orchestrator.py`:
   - no import de `secretaria.services.flow_router`, acrescentar `ai_draft_v2_enabled,` logo antes de `flows_enabled,`;
   - trocar

```python
            tenant_config.appointment_types if tenant_config is not None else [],
        )
```

   por

```python
            tenant_config.appointment_types if tenant_config is not None else [],
            # TASK-030 P3: the "(ref ...)" manage_existing_appointment v2 takes.
            with_refs=ai_draft_v2_enabled(tenant),
        )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_manage_request.py tests/test_agent_menu_tools.py tests/test_agent_capability_cache.py tests/test_tasks_helpers.py tests/test_set_booking_draft_v2.py tests/test_prompts.py tests/test_workers_layering.py -q`
Expected: PASS (inclusive `test_run_agent_maps_manage_appointment_to_sentinel` e os testes da ferramenta v1, intactos).

- [ ] **Step 7: Lint and commit**

```bash
uvx ruff format src/secretaria/services/manage_request.py tests/test_manage_request.py
uvx ruff check --fix --select I tests/test_manage_request.py
uvx ruff check src/secretaria/services/manage_request.py src/secretaria/ai/tools.py src/secretaria/ai/graph.py src/secretaria/workers/shared/llm_context.py src/secretaria/workers/orchestrator.py tests/test_manage_request.py tests/test_agent_menu_tools.py tests/test_agent_capability_cache.py
for f in src/secretaria/ai/tools.py src/secretaria/ai/graph.py src/secretaria/workers/shared/llm_context.py src/secretaria/workers/orchestrator.py tests/test_agent_menu_tools.py tests/test_agent_capability_cache.py; do echo "$f $(uvx ruff format --diff $f 2>/dev/null | grep -c '^@@')"; done   # none may exceed its count before editing
git add src/secretaria/services/manage_request.py src/secretaria/ai/tools.py src/secretaria/ai/graph.py src/secretaria/workers/shared/llm_context.py src/secretaria/workers/orchestrator.py tests/test_manage_request.py tests/test_agent_menu_tools.py tests/test_agent_capability_cache.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(ai): manage_existing_appointment v2 names the appointment and the new time

Behind the AI draft v2 switch the tool takes WHICH appointment (its clinic-local start,
shown to the model as "(ref AAAA-MM-DD HH:MM)" in the appointment context) and, for a
reschedule, the new day and time - formats only. The sentinel stays byte for byte the v1
one when only the action is given; the compiled-agent cache tells the two tools apart.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 7: Onde o pedido de gerenciar pousa (`resolve_manage_request`, puro)

**Files:**
- Modify: `src/secretaria/services/manage_request.py` (bloco de imports; códigos; `ManageResolution`; resolvedor)
- Modify: `src/secretaria/services/flow_router.py` (`_manage_confirm_result` extraído de `_manage_handle_slot`)
- Test: `tests/test_manage_request_resolver.py`

**Interfaces:**
- Consumes: T6 (`ManageRequest`, `manage_target`, `find_appointment`, `FIELD_*`); P2a (`free_slots_for_day`, `fr._hold_owner`, `fr.booking_gate_scope`, `landing_step`, `DROP_*`, `DRAFT_DAY_*_PREFIX`, `FALLBACK_*` de `booking_draft`); flow_router (`enter_manage_action`, `_begin_reschedule`, `enter_day_picker`, `_enter_slot_picker`, `_calendar_unavailable`, `_appt_uuid`, `_appt_duration_minutes`, `MANAGE_DAY_BRANCH`, `DAY_PICKER_WINDOW_DAYS`, `_DayPickerState`).
- Produces: `DROP_UNKNOWN_APPOINTMENT`, `DROP_APPOINTMENT_NOT_CHOSEN`, `DROP_REASONS`, `FALLBACK_NO_APPOINTMENTS`, `FALLBACK_REASONS`, `ManageResolution`, `resolve_manage_request(request, *, tenant, appointments, professionals, calendar, conversation_id, tz, now) -> ManageResolution`; `fr._manage_confirm_result(managing_id, appt, start, selected_day) -> FlowRouterResult`.

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_manage_request_resolver.py`:

```python
"""Where an AI manage request lands: the buttons' own steps, never further (TASK-030 P3).

Pure (services/manage_request.py): fake agenda, a published fake gate, no database.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import datetime as dt  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.booking_draft import (  # noqa: E402
    DRAFT_DAY_OUT_OF_WINDOW_PREFIX,
    DRAFT_DAY_UNAVAILABLE_PREFIX,
)
from secretaria.services.calendar import CalendarUnavailableError  # noqa: E402
from secretaria.services.manage_request import ManageRequest, resolve_manage_request  # noqa: E402
from tests.test_flow_router import _tenant  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
NOW = dt.datetime(2026, 10, 5, 9, 0, tzinfo=TZ)
NEW_DAY = dt.date(2026, 10, 15)
TWO_PM = dt.time(14, 0)
OWNER = uuid4()


def _appt(local: dt.datetime, *, professional_id=OWNER, kind="Consulta") -> dict:
    start = local.replace(tzinfo=TZ).astimezone(dt.UTC)
    return {
        "id": str(uuid4()),
        "google_event_id": f"evt-{uuid4()}",
        "appointment_type": kind,
        "start_at": start,
        "end_at": start + dt.timedelta(minutes=30),
        "professional_id": str(professional_id) if professional_id else None,
    }


FIRST = _appt(dt.datetime(2026, 10, 7, 9, 0))
SECOND = _appt(dt.datetime(2026, 10, 9, 9, 0), kind="Retorno")
SOMEONE_ELSES = dt.datetime(2026, 10, 7, 11, 0)  # another patient's appointment time


class _Agenda:
    def __init__(self, free=None, unavailable=False):
        self.tzinfo = TZ
        self.free = free if free is not None else {NEW_DAY: ["14:00", "14:30"]}
        self.unavailable = unavailable
        self.updated: list = []
        self.cancelled: list = []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        if self.unavailable:
            raise CalendarUnavailableError("down")
        return [dt.datetime(d.year, d.month, d.day, tzinfo=TZ) for d in sorted(self.free) if self.free[d]]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        if self.unavailable:
            raise CalendarUnavailableError("down")
        times = self.free.get(day.date(), [])[:max_slots]
        return [{"start": f"{day.date().isoformat()}T{t}", "end": "", "label": t} for t in times]

    async def update_event(self, event_id, start, end):  # pragma: no cover - must never run
        self.updated.append(event_id)

    async def cancel_event(self, event_id):  # pragma: no cover - must never run
        self.cancelled.append(event_id)


class _Gate:
    armed = False

    def __init__(self, windows=None):
        self._windows = windows or {}
        self.asked: list = []

    async def busy_windows(self, professional_id):
        self.asked.append(professional_id)
        return list(self._windows.get(professional_id, []))


def _local(day: dt.date, hhmm: str) -> dt.datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


async def _resolve(request, *, appointments, agenda=None, gate=None, now=NOW):
    with fr.booking_gate_scope(gate or _Gate()):
        return await resolve_manage_request(
            request,
            tenant=_tenant(),
            appointments=appointments,
            professionals=[],
            calendar=agenda if agenda is not None else _Agenda(),
            conversation_id=uuid4(),
            tz=TZ,
            now=now,
        )


# --------------------------------------------------------------------------
# Cancel: always the "Confirmar o cancelamento?" card
# --------------------------------------------------------------------------


async def test_cancel_names_one_of_two_and_stops_at_the_confirmation_card():
    res = await _resolve(
        ManageRequest("cancel", appointment=dt.datetime(2026, 10, 9, 9, 0)),
        appointments=[FIRST, SECOND],
    )
    assert res.landing_step == fr.STEP_MANAGE_CANCEL_CONFIRM
    assert res.result.flow_managing_appointment_id == UUID(SECOND["id"])
    assert res.result.bubbles[0].body.startswith("Confirmar o cancelamento?")
    assert res.accepted == ("action", "appointment")
    assert res.dropped == {}


async def test_cancel_with_a_reference_that_is_not_the_patients_never_guesses():
    res = await _resolve(
        ManageRequest("cancel", appointment=SOMEONE_ELSES), appointments=[FIRST, SECOND]
    )
    assert res.landing_step == fr.STEP_MANAGE_PICK_CANCEL  # the patient picks, as with buttons
    assert res.result.flow_managing_appointment_id is None
    assert res.dropped == {"appointment": "unknown_appointment"}


async def test_cancel_never_cancels():
    agenda = _Agenda()
    res = await _resolve(ManageRequest("cancel"), appointments=[FIRST], agenda=agenda)
    assert res.landing_step == fr.STEP_MANAGE_CANCEL_CONFIRM
    assert res.result.appointment_cancel_id is None
    assert agenda.cancelled == []


# --------------------------------------------------------------------------
# Reschedule: day picker / slot list / reschedule card
# --------------------------------------------------------------------------


async def test_reschedule_to_a_free_time_lands_on_the_reschedule_card():
    agenda = _Agenda()
    res = await _resolve(
        ManageRequest("reschedule", appointment=dt.datetime(2026, 10, 7, 9, 0), day=NEW_DAY, time=TWO_PM),
        appointments=[FIRST, SECOND],
        agenda=agenda,
    )
    assert res.landing_step == fr.STEP_MANAGE_CONFIRM
    assert res.result.flow_state is FlowState.MANAGE_BOOKING
    assert res.result.flow_managing_appointment_id == UUID(FIRST["id"])
    assert (res.result.flow_selected_day, res.result.flow_selected_slot) == (
        "2026-10-15",
        "2026-10-15T14:00",
    )
    assert res.result.bubbles[0].body == "Remarcar para:\nConsulta\n15/10/2026 às 14:00"
    assert res.accepted == ("action", "appointment", "day", "time")
    assert res.result.appointment_reschedule is None
    assert agenda.updated == []


async def test_a_held_new_time_lands_on_that_days_slot_list():
    gate = _Gate({OWNER: [(_local(NEW_DAY, "14:00"), _local(NEW_DAY, "14:30"))]})
    res = await _resolve(
        ManageRequest("reschedule", day=NEW_DAY, time=TWO_PM), appointments=[FIRST], gate=gate
    )
    assert res.landing_step == fr.STEP_MANAGE_SLOT
    assert res.dropped == {"time": "no_free_slot"}
    rows = [row[0] for row in res.result.bubbles[0].rows]
    assert "slot|2026-10-15T14:00" not in rows and "slot|2026-10-15T14:30" in rows
    # The holds are looked up on the appointment's OWN agenda.
    assert set(gate.asked) == {OWNER}


async def test_a_new_day_outside_the_window_lands_on_the_reschedule_day_picker():
    res = await _resolve(
        ManageRequest("reschedule", day=dt.date(2026, 12, 1), time=TWO_PM), appointments=[FIRST]
    )
    assert res.landing_step == fr.STEP_MANAGE_DAY
    assert res.dropped == {"day": "out_of_window", "time": "missing_day"}
    assert res.result.bubbles[0].body.startswith(DRAFT_DAY_OUT_OF_WINDOW_PREFIX)
    assert res.result.flow_managing_appointment_id == UUID(FIRST["id"])


async def test_a_new_day_without_free_time_lands_on_the_day_picker():
    agenda = _Agenda({NEW_DAY: [], dt.date(2026, 10, 16): ["09:00"]})
    res = await _resolve(
        ManageRequest("reschedule", day=NEW_DAY), appointments=[FIRST], agenda=agenda
    )
    assert res.landing_step == fr.STEP_MANAGE_DAY
    assert res.dropped == {"day": "day_unavailable"}
    assert res.result.bubbles[0].body.startswith(DRAFT_DAY_UNAVAILABLE_PREFIX)


async def test_today_with_the_new_time_already_past():
    # The calendar's own walk never offers a started slot; the fake mirrors that.
    afternoon = dt.datetime(2026, 10, 15, 15, 0, tzinfo=TZ)
    agenda = _Agenda({NEW_DAY: ["15:30"]})
    res = await _resolve(
        ManageRequest("reschedule", day=NEW_DAY, time=TWO_PM),
        appointments=[_appt(dt.datetime(2026, 10, 20, 9, 0))],
        agenda=agenda,
        now=afternoon,
    )
    assert res.landing_step == fr.STEP_MANAGE_SLOT
    assert [row[0] for row in res.result.bubbles[0].rows][0] == "slot|2026-10-15T15:30"


async def test_reschedule_without_a_day_opens_the_day_picker():
    res = await _resolve(ManageRequest("reschedule"), appointments=[FIRST])
    assert res.landing_step == fr.STEP_MANAGE_DAY
    assert res.result.flow_managing_appointment_id == UUID(FIRST["id"])


async def test_two_appointments_and_none_named_show_the_pick_list():
    res = await _resolve(
        ManageRequest("reschedule", day=NEW_DAY, time=TWO_PM), appointments=[FIRST, SECOND]
    )
    assert res.landing_step == fr.STEP_MANAGE_PICK_RESCHEDULE
    assert res.dropped == {"day": "appointment_not_chosen", "time": "appointment_not_chosen"}
    assert res.accepted == ("action",)


async def test_nothing_to_reschedule_lands_on_the_menu():
    res = await _resolve(ManageRequest("reschedule"), appointments=[])
    assert res.landing_step == "menu"
    assert res.fallback == "no_appointments"


async def test_an_unreachable_agenda_hands_over():
    res = await _resolve(
        ManageRequest("reschedule", day=NEW_DAY, time=TWO_PM),
        appointments=[FIRST],
        agenda=_Agenda(unavailable=True),
    )
    assert res.result.action == "calendar_unavailable"
    assert (res.landing_step, res.fallback) == ("human_handover", "calendar_unavailable")


async def test_the_button_path_card_is_byte_for_byte_the_same_builder():
    conversation = SimpleNamespace(
        flow_managing_appointment_id=UUID(FIRST["id"]), flow_selected_day="2026-10-15"
    )
    tapped = fr._manage_handle_slot(conversation, [FIRST], "🗓️ 14:00 (2026-10-15T14:00)")
    built = fr._manage_confirm_result(
        UUID(FIRST["id"]), FIRST, dt.datetime(2026, 10, 15, 14, 0), "2026-10-15"
    )
    assert tapped == built
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_manage_request_resolver.py -q`
Expected: erro de coleta — `ImportError: cannot import name 'resolve_manage_request' from 'secretaria.services.manage_request'`.

- [ ] **Step 3: The shared reschedule card**

Em `src/secretaria/services/flow_router.py`, substituir a função `_manage_handle_slot` inteira por (o cartão é byte a byte o de hoje):

```python
def _manage_confirm_result(
    managing_id: UUID | None, appt: dict | None, start: datetime, selected_day: str | None
) -> FlowRouterResult:
    """The "Remarcar para:" Confirmar/Cancelar card - the ONE builder for the slot tap and
    the AI's manage hand-back (services/manage_request.py, TASK-030 P3).

    `start` is printed and stored as the same instant (naive ISO minute), and "Confirmar"
    on the card is routed by `_manage_reschedule` whichever path drew it.
    """
    appt_type = str(appt.get("appointment_type") or "Consulta") if appt else "Consulta"
    recap = f"Remarcar para:\n{appt_type}\n{start.strftime('%d/%m/%Y às %H:%M')}"
    return FlowRouterResult(
        action="reply",
        bubbles=[ButtonBubble(body=recap, confirm_label=LABEL_CONFIRM, cancel_label=LABEL_CANCEL)],
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_CONFIRM,
        flow_managing_appointment_id=managing_id,
        flow_selected_day=selected_day,
        flow_selected_slot=start.replace(tzinfo=None).isoformat(timespec="minutes"),
    )


def _manage_handle_slot(
    conversation: Conversation, appointments: list[dict], body: str
) -> FlowRouterResult:
    start = _slot_iso_from_body(body)
    if start is None:
        return _preserve(conversation, "delegate_llm")
    appt = _find_appt_by_id(appointments, _managing_appt_id_str(conversation))
    return _manage_confirm_result(
        _selected_managing_appointment_id(conversation),
        appt,
        start,
        conversation.flow_selected_day,
    )
```

- [ ] **Step 4: The resolver**

Em `src/secretaria/services/manage_request.py`:

1. Trocar o bloco de imports (o da Task 6) por:

```python
from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

from secretaria.models import FlowState
from secretaria.services import flow_router as fr
from secretaria.services.availability import free_slots_for_day
from secretaria.services.booking_draft import (
    DRAFT_DAY_OUT_OF_WINDOW_PREFIX,
    DRAFT_DAY_UNAVAILABLE_PREFIX,
    DROP_DAY_UNAVAILABLE,
    DROP_MISSING_DAY,
    DROP_NO_FREE_SLOT,
    DROP_OUT_OF_WINDOW,
    FALLBACK_CALENDAR_UNAVAILABLE,
    FALLBACK_NO_FREE_DAYS,
    landing_step,
)
from secretaria.services.booking_hold import BookingGate
from secretaria.services.calendar import CalendarUnavailableError
from secretaria.services.flow_router import FlowRouterResult
from secretaria.services.patient_context import as_utc

if TYPE_CHECKING:
    from secretaria.services.calendar import CalendarService
```

2. Ao fim do arquivo, acrescentar:

```python
# ---------------------------------------------------------------------------
# Where a manage request lands (spec §4.5)
# ---------------------------------------------------------------------------

# Why a supplied item did not survive - same strings as workers/shared/handback_log.py
# DROP_* (the day/time ones are the booking resolver's).
DROP_UNKNOWN_APPOINTMENT = "unknown_appointment"
DROP_APPOINTMENT_NOT_CHOSEN = "appointment_not_chosen"
DROP_REASONS = (
    DROP_UNKNOWN_APPOINTMENT,
    DROP_APPOINTMENT_NOT_CHOSEN,
    DROP_OUT_OF_WINDOW,
    DROP_DAY_UNAVAILABLE,
    DROP_NO_FREE_SLOT,
    DROP_MISSING_DAY,
)
FALLBACK_NO_APPOINTMENTS = "no_appointments"
FALLBACK_REASONS = (
    FALLBACK_NO_APPOINTMENTS,
    FALLBACK_CALENDAR_UNAVAILABLE,
    FALLBACK_NO_FREE_DAYS,
)


@dataclass
class ManageResolution:
    """Where a manage request landed and what happened to each supplied item."""

    result: FlowRouterResult
    landing_step: str
    accepted: tuple[str, ...] = ()
    dropped: dict[str, str] = field(default_factory=dict)
    fallback: str | None = None


def _fallback(result: FlowRouterResult, appointments: Sequence[dict]) -> str | None:
    if not appointments:
        return FALLBACK_NO_APPOINTMENTS
    if result.action == "calendar_unavailable":
        return FALLBACK_CALENDAR_UNAVAILABLE
    if result.flow_state is FlowState.MENU:
        # The only menu past the appointment check: the day picker found no free day.
        return FALLBACK_NO_FREE_DAYS
    return None


def _owner_id(appointment: dict) -> UUID | None:
    raw = appointment.get("professional_id")
    try:
        return UUID(str(raw)) if raw else None
    except ValueError:
        return None


async def resolve_manage_request(
    request: ManageRequest,
    *,
    tenant: Any,
    appointments: Sequence[dict],
    professionals: list | None,
    calendar: CalendarService | None,
    conversation_id: Any,
    tz: dt.tzinfo,
    now: dt.datetime,
) -> ManageResolution:
    """Land an AI manage request on the steps the buttons use - never further than a card.

    Cancel: "Confirmar o cancelamento?" for the named appointment (or the buttons' own
    0/1/2+ handling); the cancellation itself is only ever the patient's "Sim". Reschedule
    of a known appointment: no day -> the reschedule day picker; a day outside the window or
    without free time -> the day picker with the reason line; a time free on the OWNING
    agenda (`calendar`, fresh) minus held slots -> the "Remarcar para:" card; else that day's
    slot list. A reschedule whose appointment is not known (two of them and none named, or
    a reference that matches none) shows the buttons' pick list and drops the day/time.

    `appointments` are THIS conversation's patient's upcoming appointments - nothing outside
    them can ever be targeted. The deposit/reschedule-limit pre-check is the worker's
    (workers/shared/deposit.py); it runs before this. Runs inside a booking-gate scope so
    held slots count as busy: the caller's gate when published, else an unarmed one.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    gate = fr._ACTIVE_GATE.get()
    if gate is None:
        gate = BookingGate(
            tenant_id=getattr(tenant, "id", None),
            conversation_id=conversation_id,
            patient_id=None,
            external_id=None,
            armed=False,
        )
    with fr.booking_gate_scope(gate):
        return await _resolve_manage(
            request,
            tenant=tenant,
            appointments=list(appointments),
            professionals=professionals,
            calendar=calendar,
            tz=tz,
            now=now,
        )


async def _resolve_manage(
    request: ManageRequest,
    *,
    tenant: Any,
    appointments: list[dict],
    professionals: list | None,
    calendar: CalendarService | None,
    tz: dt.tzinfo,
    now: dt.datetime,
) -> ManageResolution:
    dropped: dict[str, str] = {}
    target = manage_target(request, appointments, tz)
    if request.appointment is not None and target is None:
        dropped[FIELD_APPOINTMENT] = DROP_UNKNOWN_APPOINTMENT
    if request.time is not None and request.day is None:
        dropped[FIELD_TIME] = DROP_MISSING_DAY

    def done(result: FlowRouterResult, *names: str) -> ManageResolution:
        return ManageResolution(
            result=result,
            landing_step=landing_step(result),
            accepted=tuple(
                name
                for name in request.supplied_fields()
                if name in names and name not in dropped
            ),
            dropped=dict(dropped),
            fallback=_fallback(result, appointments),
        )

    target_id = fr._appt_uuid(target) if target is not None else None
    if request.action == ACTION_CANCEL:
        result = await fr.enter_manage_action(
            "cancel", tenant, appointments, professionals, preselected_id=target_id
        )
        return done(result, FIELD_ACTION, FIELD_APPOINTMENT)
    if target is None:
        for name in (FIELD_DAY, FIELD_TIME):
            if getattr(request, name) is not None:
                dropped.setdefault(name, DROP_APPOINTMENT_NOT_CHOSEN)
        result = await fr.enter_manage_action(
            "reschedule", tenant, appointments, professionals, calendar=calendar
        )
        return done(result, FIELD_ACTION)
    if request.day is None:
        result = await fr._begin_reschedule(target_id, target, tenant, calendar, professionals)
        return done(result, FIELD_ACTION, FIELD_APPOINTMENT)
    return await _land_new_day(
        request,
        target,
        tenant=tenant,
        professionals=professionals,
        calendar=calendar,
        now=now,
        dropped=dropped,
        done=done,
    )


async def _land_new_day(
    request: ManageRequest,
    target: dict,
    *,
    tenant: Any,
    professionals: list | None,
    calendar: CalendarService | None,
    now: dt.datetime,
    dropped: dict[str, str],
    done: Callable[..., ManageResolution],
) -> ManageResolution:
    """The new day, then the new time - re-derived from the OWNING agenda minus holds."""
    managing_id = fr._appt_uuid(target)
    # `flow_selected_professional_id` here is only what `_hold_owner` reads: the holds are
    # looked up on the appointment's own agenda. The manage branch never persists it.
    state = fr._DayPickerState(
        flow_managing_appointment_id=managing_id,
        flow_selected_professional_id=_owner_id(target),
    )
    duration = fr._appt_duration_minutes(target, tenant)
    base = (FIELD_ACTION, FIELD_APPOINTMENT)

    async def day_picker(prefix: str) -> ManageResolution:
        result = await fr.enter_day_picker(
            state,
            tenant,
            calendar,
            duration_minutes=duration,
            branch=fr.MANAGE_DAY_BRANCH,
            prefix=prefix,
            professionals=professionals,
        )
        return done(result, *base)

    def dropped_day(reason: str) -> None:
        dropped[FIELD_DAY] = reason
        if request.time is not None:
            dropped[FIELD_TIME] = DROP_MISSING_DAY

    if calendar is None:
        return done(
            fr._calendar_unavailable(state, fr.MANAGE_DAY_BRANCH, fr.STEP_MANAGE_DAY), *base
        )
    today = now.astimezone(calendar.tzinfo).date()
    if not today <= request.day < today + dt.timedelta(days=fr.DAY_PICKER_WINDOW_DAYS):
        dropped_day(DROP_OUT_OF_WINDOW)
        return await day_picker(DRAFT_DAY_OUT_OF_WINDOW_PREFIX)
    holds = await fr._hold_windows(fr._hold_owner(state, professionals))
    try:
        free = await free_slots_for_day(
            calendar, day=request.day, duration_minutes=duration, holds=holds
        )
    except CalendarUnavailableError:
        return done(
            fr._calendar_unavailable(state, fr.MANAGE_DAY_BRANCH, fr.STEP_MANAGE_DAY), *base
        )
    if not free:
        dropped_day(DROP_DAY_UNAVAILABLE)
        return await day_picker(DRAFT_DAY_UNAVAILABLE_PREFIX)

    names = (*base, FIELD_DAY)
    if request.time is not None:
        match = next(
            (slot for slot in free if slot.time().replace(second=0, microsecond=0) == request.time),
            None,
        )
        if match is None:
            dropped[FIELD_TIME] = DROP_NO_FREE_SLOT
        else:
            card = fr._manage_confirm_result(managing_id, target, match, request.day.isoformat())
            return done(card, *names, FIELD_TIME)
    target_day = dt.datetime(request.day.year, request.day.month, request.day.day)
    result = await fr._enter_slot_picker(
        state,
        tenant,
        calendar,
        target_day,
        duration_minutes=duration,
        branch=fr.MANAGE_DAY_BRANCH,
        professionals=professionals,
    )
    return done(result, *names)
```

> O bloco novo mantém `from secretaria.services.patient_context import as_utc` (usado por `appointment_ref`). Se `uvx ruff check --fix --select I` reordenar o bloco, aceite.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_manage_request_resolver.py tests/test_manage_request.py tests/test_rebooking_flow.py tests/test_flow_router.py tests/test_flow_day_picker.py tests/test_agent_menu_tools.py -q`
Expected: PASS (os 13 novos; os do gerenciar de hoje inalterados — `_manage_handle_slot` desenha o mesmo cartão).

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff format src/secretaria/services/manage_request.py tests/test_manage_request_resolver.py
uvx ruff check --fix --select I src/secretaria/services/manage_request.py tests/test_manage_request_resolver.py
uvx ruff check src/secretaria/services/manage_request.py src/secretaria/services/flow_router.py tests/test_manage_request_resolver.py
uvx ruff format --diff src/secretaria/services/flow_router.py | grep -c '^@@'   # must not exceed the count before editing
git add src/secretaria/services/manage_request.py src/secretaria/services/flow_router.py tests/test_manage_request_resolver.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(manage): an AI manage request lands on the buttons' own steps

resolve_manage_request: cancel always stops at "Confirmar o cancelamento?"; a reschedule
of a known appointment lands on the day picker, that day's slot list or the "Remarcar
para:" card, with the new time re-derived from the owning agenda minus holds. Only the
patient's own upcoming appointments can be targeted; an unmatched reference is never
guessed. The reschedule card has one builder for the tap and the AI.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 8: Pré-checagem de sinal antes de qualquer dia, o handler v2 e os códigos novos

**Files:**
- Modify: `src/secretaria/workers/shared/deposit.py` (imports; `_RESCHEDULE_PRECHECK_STEPS`; `_at_reschedule_limit`, `_reschedule_limit_hit`; o ramo de remarcação de `_apply_deposit_awareness`)
- Modify: `src/secretaria/workers/shared/handback_log.py` (códigos novos)
- Modify: `src/secretaria/workers/shared/sentinels.py` (imports; `_handle_manage_appointment` inteira)
- Modify: `tests/test_flow_cancel_money.py`
- Test: `tests/test_manage_v2_handback.py`

**Interfaces:**
- Consumes: T6/T7 (`ManageRequest`, `manage_target`, `resolve_manage_request`, `ACTION_RESCHEDULE`); P1 (`_fallback_to_menu`, `_land_handback`, `_log_no_landing`, `hb.*`); P2a (`_turn_booking_gate`, `booking_gate_scope`); P2b (`ai_draft_v2_enabled`).
- Produces: `deposit._at_reschedule_limit(deposit, tenant) -> bool`; `deposit._reschedule_limit_hit(tenant, appointment_id) -> tuple[int, int] | None`; `_RESCHEDULE_PRECHECK_STEPS` com `STEP_MANAGE_SLOT`, `STEP_MANAGE_CONFIRM`; `handback_log.DROP_UNKNOWN_APPOINTMENT`, `DROP_APPOINTMENT_NOT_CHOSEN`, `FALLBACK_RESCHEDULE_LIMIT = "reschedule_limit"`; `_handle_manage_appointment(reply, action, tenant, professionals, patient_wa, redis=None, waba_token=None)` (assinatura inalterada; `action` é o payload do sentinel).

- [ ] **Step 1: Write the failing tests**

1. Em `tests/test_flow_cancel_money.py`: no import de `secretaria.services.flow_router`, acrescentar `STEP_MANAGE_CONFIRM,` e `STEP_MANAGE_SLOT,` (ordem: `STEP_MANAGE_CANCEL_CONFIRM, STEP_MANAGE_CONFIRM, STEP_MANAGE_DAY, STEP_MANAGE_SLOT,`); acrescentar `from secretaria.workers.shared.deposit import _reschedule_limit_hit  # noqa: E402` logo depois de `from secretaria.workers import tasks  # noqa: E402`; e ao fim do arquivo:

```python
# --------------------------------------------------------------------------
# TASK-030 P3: the AI can land a reschedule past the day picker
# --------------------------------------------------------------------------


@pytest.mark.parametrize("step", [STEP_MANAGE_SLOT, STEP_MANAGE_CONFIRM])
async def test_a_reschedule_landing_past_the_day_picker_is_prechecked_too(db, step):
    tenant, patient, conversation, appt = await _seed(db, pix_reschedule_limit=1)
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID, reschedule_count=1)

    result = FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body="Remarcar para:\nConsulta\n15/10/2026 às 14:00")],
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=step,
        flow_managing_appointment_id=appt.id,
    )
    await tasks._apply_flow_result(
        _reply_ctx(conversation), result, patient.wa_id, redis=None, tenant=tenant, waba_token="tok"
    )

    kind, _to, _body, buttons = _FakeWhatsAppClient.created[-1].sent[0]
    assert kind == "buttons"
    assert [bid for bid, _label in buttons] == [f"apptconfirm|{appt.id}", f"apptcancel|{appt.id}"]
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_state == FlowState.MENU
        assert conv.flow_managing_appointment_id is None


@pytest.mark.parametrize("step", [STEP_MANAGE_SLOT, STEP_MANAGE_CONFIRM])
async def test_an_unblocked_reschedule_past_the_day_picker_is_untouched(db, step):
    tenant, patient, conversation, appt = await _seed(db, pix_reschedule_limit=2)
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID, reschedule_count=0)
    body = "Remarcar para:\nConsulta\n15/10/2026 às 14:00"
    result = FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=body)],
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=step,
        flow_managing_appointment_id=appt.id,
    )
    await tasks._apply_flow_result(
        _reply_ctx(conversation), result, patient.wa_id, redis=None, tenant=tenant, waba_token="tok"
    )

    assert _FakeWhatsAppClient.created[-1].sent[0][2] == body
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_step == step


async def test_the_limit_check_is_read_only(db):
    tenant, _patient, _conversation, appt = await _seed(db, pix_reschedule_limit=1)
    assert await _reschedule_limit_hit(tenant, appt.id) is None  # no deposit at all
    deposit = await _seed_deposit(db, appt, status=PixDepositStatus.PAID, reschedule_count=1)
    assert await _reschedule_limit_hit(tenant, appt.id) == (1, 1)
    async with db() as session:
        assert (await session.get(PixDeposit, deposit.id)).reschedule_count == 1
```

2. Criar `tests/test_manage_v2_handback.py`:

```python
"""manage_existing_appointment v2 through the worker (TASK-030 P3, spec §4.5).

The AI names WHICH of the patient's appointments and, for a reschedule, the new day and
time; the worker lands it on the buttons' own steps - never further than a card - after
the same deposit pre-check the buttons run. In-memory SQLite.
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

import datetime as dt  # noqa: E402
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
    Patient,
    PixDeposit,
    PixDepositStatus,
    Professional,
    Tenant,
)
from secretaria.services import (  # noqa: E402
    booking_hold as booking_hold_service,
    flow_router as fr,
    manage_request,
)
from secretaria.services.manage_request import ManageRequest  # noqa: E402
from secretaria.services.patient_context import as_utc, load_upcoming_appointments  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import handback_log  # noqa: E402
from secretaria.workers.shared.flow_runner import _run_flow  # noqa: E402
from secretaria.workers.shared.greeting import _flow_professionals, _flow_tenant_snapshot  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
TODAY = datetime.now(TZ).date()
FIRST_DAY = TODAY + timedelta(days=2)
SECOND_DAY = TODAY + timedelta(days=4)
NEW_DAY = TODAY + timedelta(days=6)
WA_ID = "5511999999999"
OTHER_WA_ID = "5511900000000"
EVERY_DAY = {
    day: [{"start": "08:00", "end": "18:00"}]
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}


def _local(day: dt.date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=TZ)


def _ref(day: dt.date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0)


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


class _Agenda:
    """The owner's agenda: 14:00 and 14:30 free on NEW_DAY. Records writes and reads."""

    def __init__(self):
        self.tzinfo = TZ
        self.slot_reads: list = []
        self.updated: list = []
        self.cancelled: list = []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        return [datetime(NEW_DAY.year, NEW_DAY.month, NEW_DAY.day, tzinfo=TZ)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        self.slot_reads.append(day.date())
        if day.date() != NEW_DAY:
            return []
        iso = NEW_DAY.isoformat()
        return [{"start": f"{iso}T{t}", "end": "", "label": t} for t in ("14:00", "14:30")][
            :max_slots
        ]

    async def update_event(self, event_id, start, end):
        self.updated.append((event_id, start, end))
        return {"id": event_id}

    async def cancel_event(self, event_id):
        self.cancelled.append(event_id)


class _FakeWhatsAppClient:
    persists_outbound = False
    created: list = []

    def __init__(self):
        self.sent: list = []
        _FakeWhatsAppClient.created.append(self)

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls()

    async def send_buttons(self, to, body, buttons):
        self.sent.append(("buttons", body, buttons))
        return {"messages": [{"id": "wamid.test"}]}

    async def send_text_message(self, to, body):
        self.sent.append(("text", body))
        return {"messages": [{"id": "wamid.test"}]}


class _LogRecorder:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        def _log(event: str, **fields) -> None:
            self.records.append((level, event, fields))

        return _log


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, db):
    agenda = _Agenda()
    sent: list = []
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(booking_hold_service, "async_session_factory", db)

    async def _calendar(session, tenant, target):
        return agenda

    monkeypatch.setattr(workers_ns, "_appointment_calendar", _calendar)

    async def _dispatch(reply, bubbles, tenant=None, waba_token=None):
        sent.extend(bubbles)
        return len(bubbles)

    monkeypatch.setattr(workers_ns, "_dispatch_bubbles", _dispatch)
    _FakeWhatsAppClient.created = []
    monkeypatch.setattr(workers_ns, "WhatsAppClient", _FakeWhatsAppClient)
    log = _LogRecorder()
    monkeypatch.setattr(workers_ns, "logger", log)
    return SimpleNamespace(db=db, agenda=agenda, sent=sent, log=log)


async def _seed(db, *, switch=True, own=1, limit=2, deposit_count=None):
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            is_active=True,
            timezone="America/Sao_Paulo",
            initial_flows={"ai_draft_v2": True} if switch else {},
            appointment_types=[{"name": "Consulta", "duration_min": 30, "is_active": True}],
            business_hours=EVERY_DAY,
            pix_deposit_enabled=True,
            pix_reschedule_limit=limit,
        )
        session.add(tenant)
        await session.flush()
        doctor = Professional(tenant_id=tenant.id, name="Dra. Única", is_active=True)
        patient = Patient(tenant_id=tenant.id, wa_id=WA_ID, name="Maria")
        other = Patient(tenant_id=tenant.id, wa_id=OTHER_WA_ID, name="Outra")
        session.add_all([doctor, patient, other])
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id, patient_id=patient.id, flow_state=FlowState.LLM
        )
        other_conversation = Conversation(tenant_id=tenant.id, patient_id=other.id)
        session.add_all([conversation, other_conversation])
        appointments = []
        for day in (FIRST_DAY, SECOND_DAY)[:own]:
            start = _local(day, 9).astimezone(UTC)
            appointments.append(
                Appointment(
                    tenant_id=tenant.id,
                    patient_id=patient.id,
                    professional_id=doctor.id,
                    google_event_id=f"evt-{uuid4()}",
                    appointment_type="Consulta",
                    start_at=start,
                    end_at=start + timedelta(minutes=30),
                    status=AppointmentStatus.SCHEDULED,
                )
            )
        other_start = _local(FIRST_DAY, 11).astimezone(UTC)
        others_appointment = Appointment(
            tenant_id=tenant.id,
            patient_id=other.id,
            professional_id=doctor.id,
            google_event_id=f"evt-{uuid4()}",
            appointment_type="Consulta",
            start_at=other_start,
            end_at=other_start + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
        )
        session.add_all([*appointments, others_appointment])
        await session.flush()
        if deposit_count is not None:
            session.add(
                PixDeposit(
                    id=uuid4(),
                    tenant_id=tenant.id,
                    appointment_id=appointments[0].id,
                    patient_id=patient.id,
                    asaas_payment_id=f"pay-{uuid4()}",
                    amount_cents=10000,
                    percent_applied=30,
                    status=PixDepositStatus.PAID,
                    reschedule_count=deposit_count,
                )
            )
        await session.commit()
        for obj in (tenant, doctor, patient, conversation, other_conversation, *appointments, others_appointment):
            await session.refresh(obj)
        return SimpleNamespace(
            tenant=tenant,
            doctor=doctor,
            patient=patient,
            conversation=conversation,
            other_conversation=other_conversation,
            appointments=appointments,
            others_appointment=others_appointment,
        )


def _reply(conversation, body="remarca pra mim") -> tasks._ReplyContext:
    return tasks._ReplyContext(
        conversation_id=conversation.id, patient_ref=WA_ID, inbound_body=body
    )


async def _manage(bundle, request: ManageRequest) -> None:
    await tasks._handle_manage_appointment(
        _reply(bundle.conversation),
        request.to_payload(),
        bundle.tenant,
        _flow_professionals([bundle.doctor], []),
        WA_ID,
        waba_token="t",
    )


async def _row(db, conversation) -> Conversation:
    async with db() as session:
        return await session.get(Conversation, conversation.id)


def _events(log: _LogRecorder, name: str = handback_log.EVENT_NAME) -> list[dict]:
    return [fields for _level, event, fields in log.records if event == name]


async def test_cancel_with_the_appointment_named_stops_at_the_cancel_card(wired):
    bundle = await _seed(wired.db, own=2)

    await _manage(bundle, ManageRequest("cancel", appointment=_ref(SECOND_DAY, 9)))

    row = await _row(wired.db, bundle.conversation)
    assert row.flow_step == fr.STEP_MANAGE_CANCEL_CONFIRM
    assert row.flow_managing_appointment_id == bundle.appointments[1].id
    assert wired.sent[-1].body.startswith("Confirmar o cancelamento?")
    assert wired.agenda.cancelled == []
    (event,) = _events(wired.log)
    assert event["source_tool"] == "manage_existing_appointment"
    assert event["landing_step"] == "manage_cancel_confirm"
    assert event["supplied"] == ["action", "appointment"]
    assert event["accepted"] == ["action", "appointment"]


async def test_another_patients_appointment_is_never_targeted(wired):
    bundle = await _seed(wired.db)

    await _manage(bundle, ManageRequest("cancel", appointment=_ref(FIRST_DAY, 11)))

    row = await _row(wired.db, bundle.conversation)
    assert row.flow_managing_appointment_id == bundle.appointments[0].id  # the patient's own
    assert row.flow_managing_appointment_id != bundle.others_appointment.id
    (event,) = _events(wired.log)
    assert event["dropped"] == {"appointment": "unknown_appointment"}
    async with wired.db() as session:
        untouched = await session.get(Appointment, bundle.others_appointment.id)
    assert untouched.status == AppointmentStatus.SCHEDULED


async def test_reschedule_to_a_free_time_lands_on_the_reschedule_card(wired):
    bundle = await _seed(wired.db)

    await _manage(
        bundle,
        ManageRequest(
            "reschedule", appointment=_ref(FIRST_DAY, 9), day=NEW_DAY, time=dt.time(14, 0)
        ),
    )

    row = await _row(wired.db, bundle.conversation)
    assert row.flow_step == fr.STEP_MANAGE_CONFIRM
    assert row.flow_selected_slot == f"{NEW_DAY.isoformat()}T14:00"
    assert wired.sent[-1].body == (
        f"Remarcar para:\nConsulta\n{NEW_DAY.strftime('%d/%m/%Y')} às 14:00"
    )
    assert wired.agenda.updated == []  # nothing moved yet
    (event,) = _events(wired.log)
    assert event["landing_step"] == "manage_confirm"
    assert event["accepted"] == ["action", "appointment", "day", "time"]


async def test_confirmar_on_that_card_moves_it_through_the_button_path(wired):
    bundle = await _seed(wired.db)
    await _manage(
        bundle,
        ManageRequest(
            "reschedule", appointment=_ref(FIRST_DAY, 9), day=NEW_DAY, time=dt.time(14, 0)
        ),
    )
    async with wired.db() as session:
        row = await session.get(Conversation, bundle.conversation.id)
        upcoming = await load_upcoming_appointments(session, bundle.tenant.id, bundle.patient.id)
    snapshot = SimpleNamespace(
        id=row.id,
        tenant_id=row.tenant_id,
        patient_id=row.patient_id,
        flow_state=row.flow_state,
        flow_step=row.flow_step,
        flow_selected_type=row.flow_selected_type,
        flow_selected_day=row.flow_selected_day,
        flow_selected_slot=row.flow_selected_slot,
        flow_selected_professional_id=row.flow_selected_professional_id,
        flow_selected_insurance=row.flow_selected_insurance,
        flow_managing_appointment_id=row.flow_managing_appointment_id,
        flow_attendee_name=row.flow_attendee_name,
        flow_draft=row.flow_draft,
    )

    await _run_flow(
        _reply(bundle.conversation, fr.LABEL_CONFIRM),
        snapshot,
        _flow_tenant_snapshot(bundle.tenant, [bundle.doctor], [], None),
        None,
        "Maria",
        WA_ID,
        upcoming_appointments=upcoming,
        tenant=bundle.tenant,
        waba_token="t",
        professionals=_flow_professionals([bundle.doctor], []),
        manage_calendar=wired.agenda,
        manage_calendar_owned=True,
    )

    assert len(wired.agenda.updated) == 1
    async with wired.db() as session:
        moved = await session.get(Appointment, bundle.appointments[0].id)
    assert moved.status == AppointmentStatus.RESCHEDULED
    assert as_utc(moved.start_at) == _local(NEW_DAY, 14).astimezone(UTC)


async def test_a_reschedule_at_the_deposit_limit_gets_the_keep_or_cancel_answer(wired):
    bundle = await _seed(wired.db, limit=1, deposit_count=1)

    await _manage(
        bundle,
        ManageRequest(
            "reschedule", appointment=_ref(FIRST_DAY, 9), day=NEW_DAY, time=dt.time(14, 0)
        ),
    )

    kind, _body, buttons = _FakeWhatsAppClient.created[-1].sent[0]
    assert kind == "buttons"
    appointment_id = bundle.appointments[0].id
    assert [bid for bid, _label in buttons] == [
        f"apptconfirm|{appointment_id}",
        f"apptcancel|{appointment_id}",
    ]
    row = await _row(wired.db, bundle.conversation)
    assert row.flow_state == FlowState.MENU
    assert row.flow_managing_appointment_id is None
    assert wired.agenda.slot_reads == []  # no new time was ever computed
    (event,) = _events(wired.log)
    assert event["landing_step"] == "menu"
    assert event["fallback"] == "reschedule_limit"


async def test_a_held_new_time_lands_on_that_days_slot_list(wired):
    bundle = await _seed(wired.db)
    start = _local(NEW_DAY, 14)
    await booking_hold_service.place_hold(
        tenant_id=bundle.tenant.id,
        conversation_id=bundle.other_conversation.id,
        patient_id=None,
        professional_id=bundle.doctor.id,
        appointment_type="Consulta",
        insurance=None,
        start_at=start,
        end_at=start + timedelta(minutes=30),
    )

    await _manage(bundle, ManageRequest("reschedule", day=NEW_DAY, time=dt.time(14, 0)))

    row = await _row(wired.db, bundle.conversation)
    assert row.flow_step == fr.STEP_MANAGE_SLOT
    rows = [r[0] for r in wired.sent[-1].rows]
    assert f"slot|{NEW_DAY.isoformat()}T14:00" not in rows
    (event,) = _events(wired.log)
    assert event["dropped"] == {"time": "no_free_slot"}


async def test_switch_off_ignores_the_v2_fields(wired):
    bundle = await _seed(wired.db, switch=False)

    await _manage(bundle, ManageRequest("reschedule", day=NEW_DAY, time=dt.time(14, 0)))

    row = await _row(wired.db, bundle.conversation)
    assert row.flow_step == fr.STEP_MANAGE_DAY  # v1: the single appointment's day picker
    (event,) = _events(wired.log)
    assert event["supplied"] == ["action"]


def test_the_manage_resolver_speaks_the_hand_back_vocabulary():
    assert set(manage_request.DROP_REASONS) <= handback_log.DROP_REASONS
    assert set(manage_request.FALLBACK_REASONS) <= handback_log.FALLBACK_REASONS
    assert set(manage_request.FIELD_NAMES) <= handback_log.FIELD_NAMES
    assert handback_log.FALLBACK_RESCHEDULE_LIMIT in handback_log.FALLBACK_REASONS
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_manage_v2_handback.py tests/test_flow_cancel_money.py -q`
Expected: FAIL — o handler do P1 trata o payload v2 (JSON) como ação inválida e cai no menu; os dois `..._past_the_day_picker_is_prechecked_too` não bloqueiam; `ImportError` de `_reschedule_limit_hit`; `AttributeError` em `handback_log.FALLBACK_RESCHEDULE_LIMIT`. `test_switch_off_ignores_the_v2_fields` também falha agora (JSON lido como ação inválida).

- [ ] **Step 3: Deposit — the pre-check covers every reschedule landing, and a read-only verdict**

Em `src/secretaria/workers/shared/deposit.py`:

1. Trocar o import de `secretaria.services.flow_router` por:

```python
from secretaria.services.flow_router import (
    STEP_MANAGE_CANCEL_CONFIRM,
    STEP_MANAGE_CONFIRM,
    STEP_MANAGE_DAY,
    STEP_MANAGE_DAY_ESCAPE,
    STEP_MANAGE_DAY_RETRY,
    STEP_MANAGE_SLOT,
    FlowRouterResult,
)
```

2. Trocar o comentário e a tupla `_RESCHEDULE_PRECHECK_STEPS` inteiros por:

```python
# Steps `_apply_deposit_awareness` inspects. The day-picker retry/escape
# renders are the SAME step of the flow as STEP_MANAGE_DAY, just re-drawn
# after an unreadable free-text date, so the reschedule-limit pre-check has to
# recognise them too — otherwise a blocked target could slip past the gate by
# mistyping a date once. The slot list and the reschedule confirmation card
# joined in TASK-030 P3: the AI's manage hand-back can land a reschedule
# straight on them, skipping the day picker where the pre-check used to run.
# For the buttons it changes nothing in practice - a target already at the
# limit never got past the day picker - and it catches a limit reached between
# the day pick and the time pick.
_RESCHEDULE_PRECHECK_STEPS = (
    STEP_MANAGE_CANCEL_CONFIRM,
    STEP_MANAGE_DAY,
    STEP_MANAGE_DAY_RETRY,
    STEP_MANAGE_DAY_ESCAPE,
    STEP_MANAGE_SLOT,
    STEP_MANAGE_CONFIRM,
)


def _at_reschedule_limit(deposit, tenant: Tenant) -> bool:
    """Whether `deposit` has used up the clinic's reschedule allowance."""
    return deposit is not None and deposit.reschedule_count >= tenant.pix_reschedule_limit


async def _reschedule_limit_hit(tenant: Tenant, appointment_id) -> tuple[int, int] | None:
    """`(count, limit)` when this appointment can no longer be rescheduled, else None.

    The read-only half of `_apply_deposit_awareness`'s reschedule pre-check, for a caller
    that must know BEFORE it builds a landing: the AI's manage hand-back (TASK-030 P3)
    computes no new day for a reschedule the clinic will refuse. Non-incrementing.
    """
    async with async_session_factory() as session:
        deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment_id)
    if not _at_reschedule_limit(deposit, tenant):
        return None
    return deposit.reschedule_count, tenant.pix_reschedule_limit
```

3. Em `_apply_deposit_awareness`, trocar

```python
        else:  # any STEP_MANAGE_DAY* render
            if deposit is None or deposit.reschedule_count < tenant.pix_reschedule_limit:
                return result
```

por

```python
        else:  # any reschedule render: day picker, slot list or the confirmation card
            if not _at_reschedule_limit(deposit, tenant):
                return result
```

e, no docstring da mesma função, trocar a linha `      - a freshly-targeted STEP_MANAGE_DAY (a reschedule was just begun):` por `      - a reschedule render (STEP_MANAGE_DAY*, and since TASK-030 P3 STEP_MANAGE_SLOT / STEP_MANAGE_CONFIRM):`.

- [ ] **Step 4: The new codes**

Em `src/secretaria/workers/shared/handback_log.py`:

1. Trocar o bloco `DROP_*` + `DROP_REASONS` (texto do P2b, começando em `# TASK-030 P2: the resolver's own reasons`) por:

```python
# TASK-030 P2: the resolver's own reasons (services/booking_draft.py DROP_*, same strings).
DROP_NOT_OFFERED_BY_PROFESSIONAL = "not_offered_by_professional"
DROP_OUT_OF_WINDOW = "out_of_window"
DROP_DAY_UNAVAILABLE = "day_unavailable"
DROP_NO_FREE_SLOT = "no_free_slot"
DROP_MISSING_DAY = "missing_day"
# TASK-030 P3: the manage request's own (services/manage_request.py DROP_*, same strings).
DROP_UNKNOWN_APPOINTMENT = "unknown_appointment"
DROP_APPOINTMENT_NOT_CHOSEN = "appointment_not_chosen"
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
        DROP_UNKNOWN_APPOINTMENT,
        DROP_APPOINTMENT_NOT_CHOSEN,
    }
)
```

2. Logo depois de `FALLBACK_NO_FREE_DAYS = "no_free_days"` (P2b), inserir

```python
# TASK-030 P3: a reschedule the deposit's reschedule limit refuses (keep-or-cancel card).
FALLBACK_RESCHEDULE_LIMIT = "reschedule_limit"
```

e acrescentar `FALLBACK_RESCHEDULE_LIMIT,` dentro de `FALLBACK_REASONS = frozenset({...})`, logo depois de `FALLBACK_NO_FREE_DAYS,`.

3. Trocar o comentário dos `FIELD_*` (`# `for_whom`, `day` and `time` are carried by the draft v2 (TASK-030 P2); `appointment` is` + `# reserved for the manage v2 (P3).`) por `# `for_whom`, `day` and `time` are carried by the draft v2 (TASK-030 P2); `appointment` (and` + `# `day`/`time` again) by the manage request v2 (P3).`

- [ ] **Step 5: The handler**

Em `src/secretaria/workers/shared/sentinels.py`:

1. Imports: acrescentar `from datetime import UTC, datetime` logo depois de `from collections.abc import Mapping, Sequence` (P1) e `from zoneinfo import ZoneInfo` logo depois de `from uuid import UUID`; acrescentar `from secretaria.services.manage_request import (ACTION_RESCHEDULE, ManageRequest, manage_target, resolve_manage_request)` (em várias linhas) logo antes de `from secretaria.services.patient_context import (`; no import de `secretaria.services.flow_router`, acrescentar `_appt_uuid,` logo antes de `_enter_professional_services,` e `booking_gate_scope,` logo depois de `ai_draft_v2_enabled,`; no import de `secretaria.workers.shared.draft_resolution` (P2b), acrescentar `_turn_booking_gate,` logo depois de `_resolve_draft,`; e acrescentar

```python
from secretaria.workers.shared.deposit import (
    _reschedule_limit_hit,
)
```

   logo antes de `from secretaria.workers.shared.draft_resolution import (`. Rode `uvx ruff check --select I src/secretaria/workers/shared/sentinels.py` e acerte só a ORDEM se ele pedir (não use `--fix` em arquivo pré-existente; mova as linhas à mão).

2. Substituir `_handle_manage_appointment` inteira (versão do P1, de `async def _handle_manage_appointment(` até a linha antes de `async def _handle_start_guided_booking(`) por:

```python
async def _handle_manage_appointment(
    reply: _ReplyContext,
    action: str,
    tenant: Tenant | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
) -> None:
    """LLM hand-back: re-enter the deterministic manage (cancel/reschedule) flow.

    `action` is the sentinel payload after MANAGE_APPOINTMENT_SENTINEL_PREFIX: the bare
    action ("reschedule"/"cancel", v1) or the v2 JSON naming which appointment and, for a
    reschedule, the new day and time (services/manage_request.py). Anything else is a
    malformed sentinel and falls back to the plain menu.

    TASK-030 P3, behind the AI draft v2 switch (OFF = v1 exactly: the extra fields are
    ignored): `resolve_manage_request` lands on the same steps the buttons use - the cancel
    confirmation card (never further), the reschedule day picker, that day's slot list, or
    the "Remarcar para:" card - with the new time re-derived from the owning agenda's FRESH
    free slots minus held slots. The appointment is only ever resolved among THIS patient's
    upcoming appointments. A reschedule whose deposit is at its reschedule limit gets the
    buttons' keep-or-cancel answer BEFORE any new day is computed.

    Re-loads the patient's upcoming appointments FRESH (the model may have taken several
    tool-call turns since this turn preloaded them). Every exit records ONE
    `conversation_handback_entered`.
    """
    try:
        request = ManageRequest.from_payload(action)
    except ValueError:
        # Never the payload itself: a v2 one carries the patient's appointment times.
        logger.warning("worker_manage_appointment_bad_action", payload_chars=len(action or ""))
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
            supplied=request.supplied_fields(),
            accepted=(hb.FIELD_ACTION,),
        )
        return
    if not ai_draft_v2_enabled(tenant):
        request = ManageRequest(action=request.action)
    supplied = request.supplied_fields()

    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        patient_id = conversation.patient_id if conversation is not None else None
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
                supplied=supplied,
                accepted=(hb.FIELD_ACTION,),
            )
            return
        appointments = await load_upcoming_appointments(session, tenant.id, patient_id)
        tz = ZoneInfo(tenant.timezone or "America/Sao_Paulo")
        target = manage_target(request, appointments, tz)
        # WHICH appointment a reschedule acts on, for its agenda and its deposit: the one
        # resolved above, or - exactly as v1 - the patient's only one (a reference that
        # matched nothing still opens THAT one's day picker, never its card). A cancel, or
        # 2+ to pick from, needs no agenda at all.
        owner = None
        if request.action == ACTION_RESCHEDULE:
            owner = (
                target
                if target is not None
                else (appointments[0] if len(appointments) == 1 else None)
            )
        manage_calendar = None
        if owner is not None:
            manage_calendar = await _appointment_calendar(
                session,
                tenant,
                _appointment_calendar_target(
                    owner, await list_active_professionals(session, tenant.id)
                ),
            )

    if owner is not None and await _reschedule_limit_hit(tenant, _appt_uuid(owner)) is not None:
        # The buttons' own answer (deposit.py swaps this day picker for the keep-or-cancel
        # card in `_apply_flow_result`), decided BEFORE any new day is computed.
        result = await enter_manage_action(
            "reschedule",
            tenant,
            appointments,
            professionals,
            preselected_id=_appt_uuid(owner),
            calendar=manage_calendar,
        )
        hb.log_handback_entered(
            conversation_id=reply.conversation_id,
            tenant_id=tenant.id,
            source_tool=hb.SOURCE_MANAGE_EXISTING_APPOINTMENT,
            landing_step=hb.LANDING_MENU,
            supplied=supplied,
            accepted=(hb.FIELD_ACTION,),
            fallback=hb.FALLBACK_RESCHEDULE_LIMIT,
            topology=booking_topology(professionals),
            channel=reply.channel,
        )
        await _apply_flow_result(
            reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
        )
        return

    with booking_gate_scope(_turn_booking_gate(reply, tenant)):
        resolution = await resolve_manage_request(
            request,
            tenant=tenant,
            appointments=appointments,
            professionals=professionals,
            calendar=manage_calendar,
            conversation_id=reply.conversation_id,
            tz=tz,
            now=datetime.now(UTC),
        )
    await _land_handback(
        reply,
        resolution.result,
        patient_wa,
        source_tool=hb.SOURCE_MANAGE_EXISTING_APPOINTMENT,
        tenant=tenant,
        professionals=professionals,
        redis=redis,
        waba_token=waba_token,
        supplied=supplied,
        accepted=resolution.accepted,
        dropped=resolution.dropped,
        fallback=resolution.fallback,
    )

```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_manage_v2_handback.py tests/test_flow_cancel_money.py tests/test_handback_events.py tests/test_handback_log.py tests/test_agent_menu_tools.py tests/test_action_buttons.py tests/test_manage_request_resolver.py tests/test_workers_layering.py -q`
Expected: PASS — inclusive os de gerenciar do P1 (`test_manage_logs_the_step_it_landed_on`, `..._two_appointments_...`, `..._nothing_to_manage_...`, `..._unknown_action_is_a_bad_sentinel`, `..._without_a_tenant_...`, `..._without_flows_...`, `..._without_a_patient_...`, `test_a_failure_before_the_landing_is_known_propagates_and_logs_no_event`) e os de `test_agent_menu_tools.py::test_handle_manage_appointment_*`.

- [ ] **Step 7: Lint and commit**

```bash
uvx ruff format src/secretaria/workers/shared/handback_log.py tests/test_manage_v2_handback.py
uvx ruff check --fix --select I tests/test_manage_v2_handback.py
uvx ruff check src/secretaria/workers/shared/deposit.py src/secretaria/workers/shared/handback_log.py src/secretaria/workers/shared/sentinels.py tests/test_manage_v2_handback.py tests/test_flow_cancel_money.py
for f in src/secretaria/workers/shared/deposit.py src/secretaria/workers/shared/sentinels.py tests/test_flow_cancel_money.py; do echo "$f $(uvx ruff format --diff $f 2>/dev/null | grep -c '^@@')"; done   # none may exceed its count before editing
git add src/secretaria/workers/shared/deposit.py src/secretaria/workers/shared/handback_log.py src/secretaria/workers/shared/sentinels.py tests/test_manage_v2_handback.py tests/test_flow_cancel_money.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(manage): the AI's manage request reaches the confirmation card, after the deposit check

Behind the AI draft v2 switch, _handle_manage_appointment reads the v2 request and lands it
through resolve_manage_request: cancel stops at its card, a reschedule reaches its card
only after the same deposit/reschedule-limit pre-check the buttons run - decided before any
new day is computed (fallback reschedule_limit). The pre-check now also covers
manage_slot/manage_confirm for every entry. New hand-back codes: unknown_appointment,
appointment_not_chosen, reschedule_limit.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 9: Validação completa e documentação

**Files:**
- Create: `docs/CHECKPOINT_ia_confirmacao_expressa.md`
- Modify: `docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md`, `docs/CHECKPOINT_llm_observabilidade_rede_de_seguranca.md` (uma linha de ponteiro cada, ao fim)

**Interfaces:**
- Consumes: tudo acima (docs só depois de validar — regra do repo).
- Produces: o CHECKPOINT que P4/P5 leem antes de começar.

- [ ] **Step 1: The whole suite against the baseline**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest -q 2>&1 | tail -15`
Expected: `N' passed` com as MESMAS falhas pré-existentes anotadas no Step 0 da Task 1, e nenhuma outra. Falha nova = regressão deste plano: investigue (skill `superpowers:systematic-debugging`) antes de seguir. `secretaria-human-backup-utc-flake` falha entre 00h e 03h UTC por motivo antigo e não relacionado.

- [ ] **Step 2: "Confirmar" provably untouched, no migration, lint clean**

```bash
git log --oneline -L '/^async def _handle_confirmation/,/^# ----/:src/secretaria/services/flow_router.py' P3_BASE..HEAD
git log --oneline -L '/^async def _manage_cancel/,/^def _manage_duration/:src/secretaria/services/flow_router.py' P3_BASE..HEAD
git log --oneline -L '/^async def _manage_reschedule/,/^# ----/:src/secretaria/services/flow_router.py' P3_BASE..HEAD
git diff --stat P3_BASE..HEAD -- migrations/
git diff --name-only P3_BASE..HEAD -- '*.py' | xargs uvx ruff check
```

(troque `P3_BASE` pelo hash anotado na Task 1.) Expected: as três chamadas de `git log -L` não imprimem nenhum commit; `git diff --stat ... migrations/` vazio (P3 não tem migração); `All checks passed!`.

- [ ] **Step 3: Write the CHECKPOINT**

Criar `docs/CHECKPOINT_ia_confirmacao_expressa.md`:

```markdown
# CHECKPOINT — IA entra em qualquer etapa: confirmação expressa, rascunho que espera e consultas já marcadas (TASK-030 P3)

**Estado:** BUILT — commits locais na branch da TASK-030, **não pushado, não deployado**.
**Spec:** `docs/superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md` (§4.4, §4.5, §7 linha P3) + decisão do dono de 2026-10-03.
**Plano:** `docs/superpowers/plans/2026-10-02-ia-p3-confirmacao-expressa.md`. Depende do P2 (`docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md`).

## 1. O que muda para o paciente (só nas clínicas com `initial_flows.ai_draft_v2 = true`)

- **Confirmação expressa:** "quinta às 10h com a Dra. X, pra mim", horário livre → uma mensagem com os detalhes
  (médico e especialidade, serviço com preço e descrição, o que levar/preparo, convênio, para quem, endereço da
  clínica quando cadastrado) + o cartão de confirmação (serviço, médico, data/hora, para quem). "Confirmar" é o de
  hoje: portão de código no Portal, reservas, evento, consulta, sinal.
- **Detalhes nunca faltam nem repetem à toa:** todo pouso da IA que pula o cartão de serviço (seletor de dias, lista
  de horários, cartão expresso) leva os detalhes; quem já está depois do cartão de serviço com o mesmo serviço e
  médico não recebe de novo.
- **O cartão de confirmação nomeia o médico** ("Profissional: …"), também no caminho de botões.
- **O rascunho espera qualquer pergunta:** se a IA pousou na pergunta de convênio, médico ou serviço, a resposta
  retoma o rascunho (dia/horário inclusos) em vez de seguir o passo seguinte dos botões. Prazo: 30 min.
- **Consultas já marcadas:** a IA pode dizer qual consulta e, para remarcar, o novo dia/horário; o paciente cai no
  cartão "Confirmar o cancelamento?" ou no "Remarcar para:" (ou no seletor/lista, se o pedido não couber). Antes
  disso, a mesma checagem de sinal/limite dos botões.
- **Desligado:** tudo como antes, byte a byte.

## 2. Onde está cada peça (âncoras)

- `services/booking_details.py` — `booking_details_text`, `clinic_address_line`, `price_text`.
- `services/booking_draft.py` — `details_already_shown`, `DETAILS_SEEN_STEPS`, `_booking_details`, `_with_details`,
  `_land_day`, `_express_confirmation`; a função interna `asked` em `_resolve` estaciona o rascunho nas perguntas.
- `services/flow_router.py` — `_confirmation_card`, `_recap_professional`, `DRAFT_WAIT_STEPS`, `_carry_draft`,
  `_resume_parked_draft`, `_manage_confirm_result`.
- `workers/shared/draft_resolution.py` — `clinic_address` no snapshot (`_load_draft_context`), `_fold_answer`,
  `_resume_booking_draft`.
- `services/manage_request.py` — `ManageRequest` (sentinel), `APPOINTMENT_REF_FORMAT`, `manage_target`,
  `resolve_manage_request`.
- `ai/tools.py::manage_existing_appointment_v2` (nome `manage_existing_appointment` para o modelo);
  `workers/shared/llm_context.py::_appointment_context_text(with_refs=)` mostra `(ref AAAA-MM-DD HH:MM)`.
- `workers/shared/deposit.py` — `_RESCHEDULE_PRECHECK_STEPS` com `manage_slot`/`manage_confirm`,
  `_reschedule_limit_hit`; `workers/shared/sentinels.py::_handle_manage_appointment`.

## 3. Observabilidade

`conversation_handback_entered`: pouso expresso = `landing_step=awaiting_confirmation` com `accepted` contendo
`time`; gerenciar v2 com `supplied`/`accepted` `action`/`appointment`/`day`/`time`. Códigos novos: `dropped`
`unknown_appointment`, `appointment_not_chosen`; `fallback` `reschedule_limit`. `booking_draft_resumed` ganhou
`answered_step`. A mensagem de detalhes nunca é logada (leva nome de terceiro e convênio).

## 4. Decisões e limites conhecidos

- **WhatsApp não relê a agenda no "Confirmar"** (já era assim no cartão dos botões): horário tomado entre o cartão e
  o toque vira consulta dupla no WhatsApp; no Portal o portão responde "horário tomado". Pendente decisão do dono —
  P3 só fixou que os dois caminhos se comportam igual.
- **O cartão de serviço dos botões não mostra `requirements` nem endereço**: "quem passou pelos passos já viu os
  detalhes" vale para nome/preço/descrição. Mostrar requisitos no cartão de serviço é uma linha, se o dono quiser.
- **Endereço:** só com rua; nunca com unidade ativa (o endereço certo é o da unidade). Cadastro/curadoria do
  endereço é TASK-025/026.
- **Consulta da IA identificada pelo início** (fuso da clínica); referência que não casa com uma consulta do próprio
  paciente é descartada, nunca adivinhada.
- **"Não sei" (ajuda) apaga o rascunho** que esperava a pergunta de médico/serviço.

## 5. Pendências

P4 (`get_availability` sobre `services/availability.py`, retirada das leitoras de horário ocupado, e `create_event`/`cancel_event`
cegos que só levantam este mesmo pedido de gerenciar / o rascunho — decisão do dono de 2026-10-03) e P5 (prompt:
ensinar `for_whom`, `day`/`time`, a `ref` das consultas, "nunca descreva opções do fluxo").

## 6. Deploy (quando o dono pedir)

Sem migração no P3. Exige o P2 no ar (migração `e7d3c1a9b5f2` antes). `secretaria_api` **e** `secretaria-worker`
juntos; `GET /build` com paridade `match`. Rollback: código velho nos dois (nada a desfazer no banco).
```

- [ ] **Step 4: Pointer lines**

Acrescentar ao fim de cada arquivo, numa linha própria:

- `docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md`: `> TASK-030 P3: o gancho `_express_confirmation` foi preenchido (detalhes + cartão), o rascunho passou a esperar também convênio/médico/serviço e o gerenciar ganhou a v2 — ver `docs/CHECKPOINT_ia_confirmacao_expressa.md`.`
- `docs/CHECKPOINT_llm_observabilidade_rede_de_seguranca.md`: `> TASK-030 P3: códigos novos no vocabulário de `conversation_handback_entered` (`unknown_appointment`, `appointment_not_chosen`, `reschedule_limit`) e `answered_step` em `booking_draft_resumed` — ver `docs/CHECKPOINT_ia_confirmacao_expressa.md` §3.`

- [ ] **Step 5: Commit**

```bash
git add docs/CHECKPOINT_ia_confirmacao_expressa.md docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md docs/CHECKPOINT_llm_observabilidade_rede_de_seguranca.md
git diff --cached --stat
git commit -F - <<'EOF'
docs(checkpoint): TASK-030 P3 - express confirmation, a draft that waits on any question, manage v2

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Cobertura da spec (P3)

| Spec | Onde |
|---|---|
| §4.4 pré-condições (seis itens válidos, "pra quem" respondido) | P2a (resolvedor) + T3 (`_express_confirmation` só chamado depois deles) |
| §4.4.1 horário re-derivado da agenda fresca menos reservas | P2a `_land_day` (mantido em T3); T3 `test_a_time_held_on_the_sole_doctors_agenda_lands_on_the_slot_list` |
| §4.4.2 duas mensagens (detalhes + cartão); quem passou pelas etapas só vê o cartão com o médico | T1, T2, T3 (`details_already_shown`, `_with_details`) |
| §4.4.2 endereço "quando existir" | T1 (`clinic_address_line`), T4 (`clinic_address` no contexto, unidades) |
| §4.4.3 Confirmar pelo caminho de hoje; "horário tomado" | T4 (Portal: slot_taken; WhatsApp: paridade fixada, lacuna relatada) |
| §4.5 gerenciar v2 resolvido entre as consultas do paciente; cancelar para no cartão; remarcar só depois da pré-checagem | T6, T7, T8 |
| §7 `manage_slot`/`manage_confirm` fora da pré-checagem | T8 (`_RESCHEDULE_PRECHECK_STEPS`, `_reschedule_limit_hit`) |
| §5 critérios 2, 3, 6, 8 | T3/T4 (2, 3, 6), T4/T7/T8 (8: nada da IA cria, cancela ou move) |
| Decisão do dono 2026-10-03 (rascunho retomado depois de qualquer pergunta) | T5 |
| §4.9 um evento por hand-back, com os códigos novos | T4 (expresso), T8 (gerenciar + códigos) |
| §4.11 atrás do interruptor | Global Constraints; testes "switch off" em T2, T3, T5, T6, T8 |

## Deploy e liberação

Deploy nunca faz parte deste plano: só com pedido explícito do dono, a cada vez.

1. **Sem migração no P3** (conferido na Task 9: nada em `migrations/`). O endereço vem de `tenants.address` e as unidades de `units`, ambos já existentes; o interruptor mora em `initial_flows`. O P3 depende do P2 no ar — se o P2 ainda não foi deployado, vale a ordem do P2: migração `e7d3c1a9b5f2` primeiro.
2. **API e worker juntos** — README: "Deploy both services, or neither". A lógica dos hand-backs, do resolvedor e do gerenciar roda no worker; a API mapeia os mesmos modelos. Conferir `GET /build` dos dois: `deploy_parity` = `match`.
3. **O que liga sozinho no deploy (todas as clínicas):** nada visível. Só (a) a pré-checagem de sinal passa a olhar também a lista de horários e o cartão "Remarcar para:" — sem efeito no caminho de botões, a não ser bloquear um alvo que chegou ao limite entre escolher o dia e o horário; (b) `_service_detail_text` e `_manage_handle_slot` usam helpers extraídos, byte a byte iguais; (c) o aviso de sentinel de gerenciar inválido não loga mais o payload.
4. **O que fica atrás do interruptor `initial_flows.ai_draft_v2`:** confirmação expressa, mensagem de detalhes, médico no cartão, rascunho esperando convênio/médico/serviço, `manage_existing_appointment` v2 e a `ref` das consultas no prompt. Ligar primeiro na "Chrysostomo For Eyes", observando `conversation_handback_entered` (`landing_step=awaiting_confirmation`, `dropped`, `fallback=reschedule_limit`) e `booking_draft_resumed`; recomendação: só depois de P4 e P5 deployados (sem o prompt do P5 o modelo não é ensinado a usar `day`/`time`/`ref`).
5. **Rollback:** código velho nos DOIS serviços; nada a desfazer no banco. Um rascunho que estivesse esperando uma pergunta de convênio/médico/serviço fica inerte (o código velho só o lê nas etapas de "pra quem") e é apagado no próximo passo.
