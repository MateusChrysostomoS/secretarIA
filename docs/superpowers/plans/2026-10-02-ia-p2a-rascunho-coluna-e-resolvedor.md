# IA entra em qualquer etapa — P2a: disponibilidade, rascunho v2, `flow_draft` e o resolvedor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Existe UM lugar (`services/booking_draft.py::resolve_booking_draft`) que recebe o rascunho da IA (serviço, médico, convênio, pra quem, dia, horário), confere cada item contra os dados frescos da clínica, descarta só o item inválido e devolve o pouso certo no fluxo — inclusive guardando o rascunho em `conversations.flow_draft` enquanto o paciente responde "Essa consulta é pra você?" e retomando-o depois.

**Architecture:** P2 está dividido em dois arquivos por dependência. **Este (P2a)** constrói a base, sem mudar nada que o paciente vê hoje: (1) `services/availability.py`, a definição única de "horário livre" extraída dos seletores (com as reservas descontadas); (2) `BookingDraft` e o formato do sentinel/registro; (3) a coluna `flow_draft` + migração, levada pelo roteador só nas etapas de "pra quem"; (4) o resolvedor puro (pra quem → convênio → profissional → serviço → dia → horário), que roda dentro do contexto do portão de reservas; (5) a continuação no worker: o toque que responde "pra quem" com um rascunho guardado roda o resolvedor de novo. **P2b** (`2026-10-02-ia-p2b-handbacks-ferramenta-e-estado.md`) liga isso aos hand-backs (correção de segurança fora do interruptor), à ferramenta v2, ao interruptor por clínica e ao bloco de estado.

**Tech Stack:** Python 3.12, SQLAlchemy async + Alembic, structlog, pytest + pytest-asyncio (`asyncio_mode = "auto"`; SQLite em memória; Postgres descartável só na prova da migração), ruff.

**Spec:** `docs/superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md` — §4.1 (formato do rascunho), §4.2 (resolvedor), §4.3 (`flow_draft`), §6 (testes), §7 (linhas P2: `flow_selected_type` sobrecarregado, reservas só dentro de `route()`).

**Depende de:** P1 (`docs/superpowers/plans/2026-10-02-ia-p1-registro-de-handbacks.md`) já executado neste worktree (`workers/shared/handback_log.py`, `_land_handback`, `_fallback_to_menu`, `tests/test_handback_events.py`). P2b depende deste arquivo; P3 e P4 leem as interfaces daqui.

## Estado de execução — 2026-10-05

**A1–A6 implementados e validados localmente.** A1–A3 já commitados até `33597f9`;
A4–A6 e correções da revisão permanecem sem novo commit, conforme instrução atual do dono.
As caixas de lint/commit de A4–A6 ficam abertas apenas pela ausência do commit; lint passou.
Suíte final: **3.202 passed, 10 skipped**, Ruff clean. Migração comprovada em SQLite e
PostgreSQL 16 descartável (upgrade/downgrade/re-upgrade). Sem merge, push ou deploy.

Fonte de verdade, revisão/correções e handoff: `docs/CHECKPOINT_ia_p2a_rascunho_resolvedor.md`.
Na revisão, quatro premissas deste plano precisaram de ajuste: nome capturado não equivale
a autorização; reservas são descontadas antes do limite de exibição; tenant também deve
ser relido e levado em `DraftContext.tenant`; 96 posições são piso, expandido pela duração
de serviços curtos. Os exemplos originais abaixo são históricos; usar o código e os
testes finais como base para P2b/P3, preservando essas correções.

## Global Constraints

- Worktree `C:\TECH\BRAIN-worktrees\TASK-030\secretarIA` (branch da TASK-030). Tarefas **sequenciais**: P1 → P2a → P2b → P3 editam os mesmos arquivos (`flow_router.py`, `sentinels.py`); nunca dois agentes ao mesmo tempo.
- Testes rodam do **Git Bash**, na raiz do worktree: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest <arquivo> -q` (PowerShell não consegue setar variável vazia; nunca `pytest` solto).
- Nunca `ruff format .`. `uvx ruff format <arquivo>` só em arquivo **criado** por este plano, e ANTES do check (ele quebra as linhas de código longas que o `E501` acusaria). `uvx ruff check <arquivos tocados>` sempre, depois do format. Em arquivo existente: antes de editar, anote `uvx ruff format --diff <arquivo> | grep -c '^@@'`; depois de editar, a contagem não pode aumentar.
- A árvore está em CRLF (índice em LF). Antes de cada commit, `git diff --cached --stat` mostra só as linhas do passo; se um arquivo existente aparecer inteiro como alterado, `git restore --staged <arquivo>` e refaça a edição com âncoras pequenas.
- Camadas: api → workers → services/ai → models → core. `services/` nunca importa `workers/`. `workers/shared/*` não importa `workers.whatsapp`, `workers.portal` nem `workers.tasks` (`tests/test_workers_layering.py`).
- `_apply_flow_result` grava TODO campo `flow_*` do resultado, inclusive o novo `flow_draft`: resultado que não nomeia um campo o apaga. Todo pouso do resolvedor carrega explicitamente tipo, médico, convênio e pra quem.
- A IA nunca carrega nome de terceiro: o rascunho só tem `attendee ∈ {"self", "other", None}`; o nome é capturado pela etapa determinística (skill `pii-field-capture-pseudonymization`). `flow_draft` nunca guarda nome.
- Sem PII nem segredo em log: ids, NOMES de campo, CÓDIGOS de motivo, contagens, booleanos. Nunca serviço, convênio, nome, dia ou horário que a IA/paciente escreveu.
- Códigos de motivo e de fallback usam o vocabulário do P1 (`handback_log.DROP_*`/`FALLBACK_*`); códigos novos são acrescentados ao `handback_log` no commit do primeiro ponto que os registra (P2b, Task B1).
- Os estados `AWAITING_*` pertencem a `workers/turn_router.py`; o resolvedor nunca os escreve. Todo estado novo tem saída por tempo (skill `conversation-flow-state`): `flow_draft` expira em 30 min no consumo e é apagado pelos pisos de silêncio.
- Texto ao paciente em português; código, comentários e mensagens de commit em inglês.
- Cada commit termina com `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Commit local permitido; push e deploy só com pedido explícito do dono.
- Nada deste arquivo muda o que o paciente vê hoje, com UMA exceção deliberada e testada: a lista de horários de uma clínica de um só médico passa a esconder os horários reservados (Task A1, correção do dono das reservas).

## Review Focus

Cada linha tem o teste que a fixa, na tarefa dona do código.

- **"Outra pessoa" + serviço dito** → o fluxo pede o NOME (não a pergunta "é pra você?"), o serviço fica em `flow_draft`, e depois da autorização o paciente cai na lista de horários do dia pedido (A4 `test_other_with_a_service_asks_the_name_and_parks_the_service`; A6 `test_authorizing_a_third_party_lands_the_parked_draft_on_its_slot_list`).
- **Rascunho para um médico que não oferece o serviço** → só o serviço cai (`not_offered_by_professional`); o paciente vê a lista de serviços DESSE médico (A4 `test_a_doctor_who_does_not_offer_the_service_keeps_the_doctor`).
- **Dia no passado ou além da janela** → só o dia cai (`out_of_window`), seletor de dias com a frase de aviso (A5 `test_a_day_in_the_past_is_dropped`, `test_a_day_beyond_the_window_is_dropped`, `test_the_last_day_of_the_window_is_accepted`).
- **Horário fora do expediente** → só o horário cai (`no_free_slot`), lista de horários daquele dia (A5 `test_a_time_outside_business_hours_lands_on_that_days_slot_list`).
- **Dois pacientes/tenants** → roster/catálogo/agenda de outro tenant nunca entram (A4 `test_another_tenants_roster_and_catalog_are_never_used`; A6 `_load_draft_context` recusa conversa de outro tenant).
- **`flow_draft` com mais de 30 minutos** → a resposta do "pra quem" segue o fluxo de botões normal, sem retomar o rascunho (A2 `test_a_record_expires_after_30_minutes`; A6 `test_a_draft_older_than_30_minutes_is_not_resumed`).
- **Paciente abandona na etapa da autorização** → o piso de silêncio apaga `flow_draft` e o nome; "Cancelar" volta à pergunta levando o rascunho (A3 `test_the_draft_rides_every_attendee_step`, `test_worker_attendee_floor_drops_the_draft`).
- **Resolvedor chamado direto (fora de `route()`) vê horário reservado como ocupado** (A5 `test_a_direct_call_without_a_gate_still_sees_holds`).
- **`time` sem `day`** → horário ignorado (`missing_day`), seletor de dias (A5 `test_a_time_without_a_day_is_ignored`).
- **Payload v1 antigo** (`t`/`p`/`i`) continua sendo lido; `w` ausente = "não sabe" (A2 `test_the_v1_payload_still_parses_with_unknown_for_whom`).
- **`flow_draft` sobrevive a `_apply_flow_result`** quando o resultado o nomeia, e some quando não nomeia (A3 `test_worker_apply_writes_and_clears_the_draft`).
- **Clínica com convênio em modo "none"/não escolhido** → nunca pergunta convênio; plano reconhecido é gravado, o resto cai (A4 `test_a_clinic_that_does_not_collect_never_asks_the_convenio`).
- **Clínica de um só médico** → médico implícito, agenda do tenant (`calendar(None)`), reservas procuradas pelo id do médico (A1 `test_a_sole_professionals_hold_is_looked_up_by_their_id`; A4 `test_a_single_professional_is_implicit`; A5 `test_a_sole_professionals_holds_count`).

## Decisões deste plano (mudar só se o dono discordar)

1. **`flow_draft` só espera o "pra quem"** (spec §4.3). Quando o resolvedor pousa numa pergunta de convênio, médico ou serviço, o dia/horário do rascunho não são retomados depois da resposta (não estão em `accepted` nem em `dropped`). Estender a espera para essas perguntas é escolha do P3, que pode reaproveitar `resume_draft`.
2. **Formato do registro** = as chaves do sentinel + `saved_at` (UTC, com fuso). Vale 30 min (`flow_router.FLOW_DRAFT_TTL_MINUTES`); depois disso a resposta segue o fluxo de botões.
3. **Seletor de dias sem reservas, de propósito:** continua idêntico byte a byte (lista todo dia com tempo livre no Google; a etapa de horário esconde o reservado). Os leitores da IA (`free_slots_for_day`, `days_with_free_slots`) descontam as reservas.
4. **Dono das reservas:** o seletor de horários procurava reservas sob `None` numa clínica de um só médico, mas elas são GRAVADAS com o id desse médico (`resolve_booking_owner_id`). Corrigido com `_hold_owner` (testado; muda o comportamento só para esconder um horário já reservado).
5. **`calendar` do resolvedor é uma FONTE assíncrona** (`await calendar(professional)`), não uma agenda pronta: o resolvedor só sabe de quem é a agenda depois de validar o médico (ex.: único médico que oferece o serviço) e só a pede se chegar à etapa do dia — pouso em "pra quem" não constrói agenda nenhuma.
6. **Códigos alinhados ao P1:** `not_in_catalog`, `unknown_professional`, `unmatched_plan` (do P1) + `not_offered_by_professional`, `out_of_window`, `day_unavailable`, `no_free_slot`, `missing_day`; fallback `no_bookable_catalog` (P1) + `no_free_days` (novo). P2b acrescenta os novos ao `handback_log`.
7. **Sem catálogo agendável → menu puro** (só o `MenuBubble`), igual ao fallback de hoje (`tests/test_handback_events.py::test_selection_only_draft_on_a_clinic_with_no_bookable_catalog_is_a_counted_fallback`).
8. **`_express_confirmation` existe e devolve `None`** (gancho do P3). No P2, horário válido pousa na lista de horários do dia.

## File Structure

- Create `src/secretaria/services/availability.py` — "livre" de uma agenda menos reservas (A1).
- Create `src/secretaria/services/booking_draft.py` — `BookingDraft`, registro, códigos (A2); resolvedor (A4, A5).
- Create `migrations/versions/e7d3c1a9b5f2_conversation_flow_draft.py` — `ADD conversations.flow_draft JSON NULL` (A3).
- Create `src/secretaria/workers/shared/draft_resolution.py` — leituras frescas, portão do turno, agenda preguiçosa, continuação (A6).
- Modify `src/secretaria/services/flow_router.py` — `_hold_owner`, seletores via `availability` (A1); `FLOW_DRAFT_TTL_MINUTES` (A2); `FlowRouterResult.flow_draft/resume_draft`, `_carry_draft` (A3); `booking_gate_scope`, catálogo/oferta com catálogo, `_handle_insurance` (A4); `_ask_day(prefix=)` (A5); `resume_draft` no "pra quem" (A6).
- Modify `src/secretaria/models/conversation.py` — coluna `flow_draft` (A3).
- Modify `src/secretaria/workers/shared/flow_runner.py` — grava `flow_draft` (A3); portão extraído + continuação (A6).
- Modify `src/secretaria/workers/shared/state_expiry.py`, `src/secretaria/workers/turn_router.py`, `src/secretaria/workers/orchestrator.py` — apagar/levar `flow_draft` (A3); `_flow_professionals` (A6).
- Modify `src/secretaria/workers/shared/greeting.py` — `_flow_professionals` (A6).
- Create `tests/test_availability.py`, `tests/test_booking_draft.py`, `tests/test_flow_draft_column.py`, `tests/test_booking_draft_resolver.py`, `tests/test_booking_draft_continuation.py`; Modify `tests/test_attendee_booking.py`.

## Interfaces (o que P2b, P3, P4 e P5 consomem — nomes exatos)

```python
# services/availability.py
FREE_SLOT_SCAN_MAX: int = 96
Window = tuple[datetime, datetime]
def slot_start(raw: datetime | str, tz: tzinfo | None) -> datetime
def without_holds(slots: Sequence[dict], holds: Sequence[Window], *, duration_minutes: int, tz: tzinfo | None) -> list[dict]
async def available_day_starts(calendar: CalendarService, *, start: datetime, window_days: int, duration_minutes: int, holds: Sequence[Window] = ()) -> list[datetime]
async def days_with_free_slots(calendar: CalendarService, *, start: datetime, window_days: int, duration_minutes: int, holds: Sequence[Window] = ()) -> list[date]
async def free_slots_for_day(calendar: CalendarService, *, day: date, duration_minutes: int, holds: Sequence[Window] = (), max_slots: int = FREE_SLOT_SCAN_MAX) -> list[datetime]
#   aware datetimes in calendar.tzinfo; raise CalendarUnavailableError like the calendar does.
#   Holds for an agenda: `await flow_router._hold_windows(flow_router._hold_owner(state, professionals))`
#   inside `flow_router.booking_gate_scope(gate)` (or `await gate.busy_windows(owner_id)`).

# services/flow_router.py
FLOW_DRAFT_TTL_MINUTES: int = 30
def _hold_owner(conversation, professionals: list | None) -> UUID | None
@contextmanager
def booking_gate_scope(gate: BookingGate | None) -> Iterator[None]
async def _ask_day(conversation, tenant, calendar, services=None, professionals=None, *, prefix: str | None = None) -> FlowRouterResult
FlowRouterResult.flow_draft: dict | None = None      # persisted; carried only on ATTENDEE_STEPS
FlowRouterResult.resume_draft: bool = False         # never persisted; set on the pra-quem answer

# services/booking_draft.py
DRAFT_ATTENDEE_SELF = "self"; DRAFT_ATTENDEE_OTHER = "other"
FIELD_SERVICE/FIELD_PROFESSIONAL/FIELD_INSURANCE/FIELD_FOR_WHOM/FIELD_DAY/FIELD_TIME; FIELD_NAMES (tuple, that order)
DROP_*; DROP_REASONS; FALLBACK_*; FALLBACK_REASONS
@dataclass(frozen=True) class BookingDraft:
    service: str | None = None; professional_id: UUID | None = None; insurance: str | None = None
    attendee: Literal["self", "other"] | None = None; day: date | None = None; time: time | None = None
    def to_dict(self) -> dict[str, str | None]            # keys t, p, i, w, d, h
    def to_payload(self) -> str                           # json.dumps(to_dict())
    @classmethod def from_dict(cls, data: Any) -> BookingDraft      # ValueError on invalid
    @classmethod def from_payload(cls, raw: str) -> BookingDraft    # ValueError on invalid; v1 accepted
    def supplied_fields(self) -> tuple[str, ...]
def draft_record(draft: BookingDraft, *, saved_at: datetime) -> dict[str, str | None]
def draft_from_record(record: Any, *, now: datetime) -> BookingDraft | None   # None = absent/corrupt/expired
@dataclass class DraftResolution:
    result: FlowRouterResult; landing_step: str; accepted: tuple[str, ...] = ()
    dropped: dict[str, str] = {}; fallback: str | None = None
def landing_step(result: FlowRouterResult) -> str          # same rule as handback_log.landing_of(result)[0]
CalendarSource = Callable[[Any | None], Awaitable[CalendarService | None]]
async def resolve_booking_draft(draft: BookingDraft, *, conversation: Any, tenant: Any, professional_rows: list,
                                service_catalog: list | None, tenant_insurance: Any | None,
                                calendar: CalendarSource, now: datetime) -> DraftResolution
#   conversation: snapshot with id, tenant_id and the flow_* fields; tenant: the flow tenant snapshot
#   (workers/shared/greeting.py::_flow_tenant_snapshot); professional_rows: router-shaped roster
#   (greeting.py::_flow_professionals); calendar(professional) is called at most once, with the chosen
#   professional on a multi-professional clinic and None otherwise; now must be timezone-aware.
async def _express_confirmation(*, state: Any, tenant: Any, professional: Any | None, service: dict,
                                slot_start: datetime, duration_minutes: int, calendar: CalendarService,
                                professionals: list) -> FlowRouterResult | None   # P2: always None (P3 hook)

# workers/shared/draft_resolution.py
@dataclass class DraftContext: conversation; professional_rows; professionals; service_catalog;
                               tenant_insurance; tenant_snapshot; topology
async def _load_draft_context(reply: _ReplyContext, tenant: Tenant) -> DraftContext | None
def _turn_booking_gate(reply: _ReplyContext, tenant: Tenant | None) -> BookingGate
async def _resolve_draft(reply: _ReplyContext, tenant: Tenant, draft: BookingDraft, ctx: DraftContext,
                         *, gate: BookingGate | None = None) -> DraftResolution
async def _resume_booking_draft(reply: _ReplyContext, tenant: Tenant, result: FlowRouterResult,
                                *, gate: BookingGate | None = None) -> FlowRouterResult

# workers/shared/greeting.py
def _flow_professionals(professional_rows: list[Professional], services: list | None) -> list[SimpleNamespace]
```

---

### Task A1: `services/availability.py` — o "livre" de uma agenda, extraído dos seletores

**Files:**
- Create: `src/secretaria/services/availability.py`
- Modify: `src/secretaria/services/flow_router.py` (imports; novo `_hold_owner` logo depois de `_hold_windows`; `enter_day_picker`; `_slot_dt` removido; bloco de reservas de `_enter_slot_picker`)
- Test: `tests/test_availability.py`

**Interfaces:**
- Consumes: `secretaria.services.booking_hold.overlaps`; `secretaria.services.booking_scope.resolve_booking_owner_id`; `CalendarService.list_available_days(start_day, days, slot_minutes)` e `list_free_slots(day, slot_minutes, max_slots)`.
- Produces: `FREE_SLOT_SCAN_MAX`, `Window`, `slot_start`, `without_holds`, `available_day_starts`, `days_with_free_slots`, `free_slots_for_day` (assinaturas em "Interfaces"); `flow_router._hold_owner(conversation, professionals) -> UUID | None`.

- [x] **Step 0: Baseline da suíte (uma vez, antes de qualquer edição do P2a)**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest -q 2>&1 | tail -5`
Expected: a linha final `N passed[, M failed]`. Anote N, M e os nomes das falhas (se houver) no relatório do worker — são as falhas pré-existentes contra as quais a Task B5 compara.

- [x] **Step 1: Write the failing tests**

Criar `tests/test_availability.py`:

```python
"""Free time on one agenda minus held slots: one definition for every reader (TASK-030 P2)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import date, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from secretaria.ai.formatter import SlotsBubble  # noqa: E402
from secretaria.core.whatsapp_limits import EMOJI_SCHEDULE, decorate  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services import availability, flow_router as fr  # noqa: E402
from tests.test_flow_router import _conversation, _FakeCalendar, _tenant  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
DAY = date(2026, 10, 5)


class _Calendar:
    """`list_free_slots` answers per day; every read is recorded."""

    def __init__(self, free: dict[date, list[str]]):
        self.tzinfo = TZ
        self._free = free
        self.slot_reads: list[tuple[date, int]] = []
        self.day_scans = 0

    async def list_available_days(self, start_day, days, slot_minutes=None):
        self.day_scans += 1
        return [datetime(d.year, d.month, d.day, tzinfo=TZ) for d in sorted(self._free)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        self.slot_reads.append((day.date(), max_slots))
        return [
            {"start": f"{day.date().isoformat()}T{hhmm}", "end": "", "label": hhmm}
            for hhmm in self._free.get(day.date(), [])[:max_slots]
        ]


def _held(hhmm: str, minutes: int = 30, day: date = DAY) -> tuple[datetime, datetime]:
    hour, minute = (int(part) for part in hhmm.split(":"))
    start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
    return start, start + timedelta(minutes=minutes)


def test_slot_start_reads_a_naive_slot_in_the_clinic_timezone():
    assert availability.slot_start("2026-10-05T08:00", TZ) == datetime(2026, 10, 5, 8, 0, tzinfo=TZ)
    aware = datetime(2026, 10, 5, 11, 0, tzinfo=ZoneInfo("UTC"))
    assert availability.slot_start(aware, TZ) is aware


def test_without_holds_drops_only_the_overlapping_slots():
    slots = [
        {"start": "2026-10-05T08:00", "label": "08:00"},
        {"start": "2026-10-05T08:30", "label": "08:30"},
    ]
    kept = availability.without_holds(slots, [_held("08:00")], duration_minutes=30, tz=TZ)
    # Half-open windows: 08:30 touches the 08:00-08:30 hold, it does not overlap it.
    assert [slot["label"] for slot in kept] == ["08:30"]
    assert availability.without_holds(slots, [], duration_minutes=30, tz=TZ) == slots


async def test_free_slots_for_day_reads_the_whole_day_and_subtracts_holds():
    cal = _Calendar({DAY: ["08:00", "08:30", "10:00"]})
    free = await availability.free_slots_for_day(
        cal, day=DAY, duration_minutes=30, holds=[_held("10:00")]
    )
    assert free == [
        datetime(2026, 10, 5, 8, 0, tzinfo=TZ),
        datetime(2026, 10, 5, 8, 30, tzinfo=TZ),
    ]
    # The picker reads 8 slots; "is 16:00 free?" has to see the whole day.
    assert cal.slot_reads == [(DAY, availability.FREE_SLOT_SCAN_MAX)]


async def test_days_with_free_slots_drops_a_day_whose_every_slot_is_held():
    other = DAY + timedelta(days=1)
    cal = _Calendar({DAY: ["08:00"], other: ["09:00"]})
    days = await availability.days_with_free_slots(
        cal,
        start=datetime(2026, 10, 5, 7, 0, tzinfo=TZ),
        window_days=20,
        duration_minutes=30,
        holds=[_held("08:00")],
    )
    assert days == [other]
    # Only the day a hold touches is re-read slot by slot.
    assert [day for day, _max in cal.slot_reads] == [DAY]


async def test_without_holds_the_day_scan_is_one_calendar_read():
    cal = _Calendar({DAY: ["08:00"]})
    days = await availability.days_with_free_slots(
        cal, start=datetime(2026, 10, 5, 7, 0, tzinfo=TZ), window_days=20, duration_minutes=30
    )
    assert days == [DAY]
    assert cal.day_scans == 1
    assert cal.slot_reads == []


# --------------------------------------------------------------------------
# The two pickers keep behaving byte for byte (they now read through the module)
# --------------------------------------------------------------------------


class _Gate:
    """Duck-typed BookingGate: answers `busy_windows` per agenda, records who asked."""

    armed = False

    def __init__(self, windows=None):
        self._windows = windows or {}
        self.asked: list = []

    async def busy_windows(self, professional_id):
        self.asked.append(professional_id)
        return list(self._windows.get(professional_id, []))


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log


_SLOTS = [
    {"start": "2026-10-05T08:00", "end": "2026-10-05T08:40", "label": "08:00"},
    {"start": "2026-10-05T08:40", "end": "2026-10-05T09:20", "label": "08:40"},
]
_DAY_TAP = f"{fr._day_row_label(datetime(2026, 10, 5))} (2026-10-05|0)"
_HELD_0800 = (datetime(2026, 10, 5, 8, 0, tzinfo=TZ), datetime(2026, 10, 5, 8, 40, tzinfo=TZ))


def _at_day_step():
    return _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_DAY,
        flow_selected_type="Primeira Consulta",
    )


async def test_the_day_picker_is_byte_identical_after_the_extraction():
    days = [datetime(2026, 10, 5, tzinfo=TZ), datetime(2026, 10, 6, tzinfo=TZ)]
    cal = _FakeCalendar(days=days)
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SERVICE_CONFIRM,
        flow_selected_type="Primeira Consulta",
    )
    res = await fr.route(conv, _tenant(), cal, fr.LABEL_BOOK_SERVICE)
    (bubble,) = res.bubbles
    assert isinstance(bubble, SlotsBubble)
    assert bubble.body == fr.DAY_PICKER_BODY
    assert bubble.rows == [
        ("day|2026-10-05|0", fr._day_row_label(days[0])),
        ("day|2026-10-06|0", fr._day_row_label(days[1])),
        ("dayback|service", fr.LABEL_ANOTHER_SERVICE),
    ]
    assert (bubble.button_label, bubble.section_title) == ("Ver dias", "Dias disponíveis")
    # One calendar read, same window, the service's own 40 minutes.
    assert [(n, minutes) for _start, n, minutes in cal.day_scans] == [
        (fr.DAY_PICKER_WINDOW_DAYS, 40)
    ]


async def test_the_slot_picker_is_byte_identical_after_the_extraction():
    gate = _Gate()
    res = await fr.route(_at_day_step(), _tenant(), _FakeCalendar(slots=_SLOTS), _DAY_TAP, gate=gate)
    (bubble,) = res.bubbles
    assert bubble.body == "Horários livres em 05/10:"
    assert bubble.rows == [
        ("slot|2026-10-05T08:00", decorate(EMOJI_SCHEDULE, "08:00")),
        ("slot|2026-10-05T08:40", decorate(EMOJI_SCHEDULE, "08:40")),
        ("dayagain|0", fr.LABEL_ANOTHER_DAY),
        ("dayback|service", fr.LABEL_ANOTHER_SERVICE),
    ]
    assert (res.flow_step, res.flow_selected_day) == (fr.STEP_AWAITING_SLOT, "2026-10-05")
    assert gate.asked == [None]


async def test_a_held_slot_is_hidden_from_the_slot_picker(monkeypatch):
    log = _Log()
    monkeypatch.setattr(fr, "logger", log)
    res = await fr.route(
        _at_day_step(), _tenant(), _FakeCalendar(slots=_SLOTS), _DAY_TAP, gate=_Gate({None: [_HELD_0800]})
    )
    assert [row[0] for row in res.bubbles[0].rows][0] == "slot|2026-10-05T08:40"
    assert [f["hidden"] for e, f in log.events if e == "flow_slots_hidden_by_hold"] == [1]


async def test_a_sole_professionals_hold_is_looked_up_by_their_id():
    """Holds are PLACED with the sole professional as owner (`resolve_booking_owner_id`);
    the picker used to look them up under None and never saw them."""
    sole = SimpleNamespace(
        id=uuid4(), name="Dra. Única", specialty=None, about=None,
        appointment_types=None, business_hours=None,
    )
    gate = _Gate({sole.id: [_HELD_0800]})
    res = await fr.route(
        _at_day_step(), _tenant(), _FakeCalendar(slots=_SLOTS), _DAY_TAP,
        professionals=[sole], gate=gate,
    )
    assert gate.asked == [sole.id]
    assert "slot|2026-10-05T08:00" not in [row[0] for row in res.bubbles[0].rows]
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_availability.py -q`
Expected: erro de coleta — `ImportError: cannot import name 'availability' from 'secretaria.services'`.

- [x] **Step 3: Write the module**

Criar `src/secretaria/services/availability.py`:

```python
"""Free time on one agenda, minus the slots other conversations are holding.

The day picker and the slot picker (services/flow_router.py) used to be the only readers
of an agenda's free time; the AI draft resolver (services/booking_draft.py, TASK-030 P2)
and the AI availability tool (P4) need the same answer. This module IS that answer,
extracted, so there is one definition of "free":

  * the calendar's own walk (`CalendarService.list_available_days` / `list_free_slots`:
    business hours, busy events, slots already past);
  * minus the windows `BookingHold` rows are reserving (services/booking_hold.py), which
    Google has never heard of.

Nothing here returns an event - only instants. That is the contract the AI tool relies
on: no title, attendee or event id ever leaves the calendar layer through this module.

The day picker passes no holds, on purpose: it has always listed every day with free
Google time and let the slot step hide the held slots, and it must keep rendering byte
for byte (tests/test_availability.py). The AI readers pass the holds.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING

from secretaria.services.booking_hold import overlaps

if TYPE_CHECKING:
    from secretaria.services.calendar import CalendarService

# Slots read for ONE day when the question is "is this exact time free?" rather than
# "what can I show?": 96 = every 15-minute slot of a 24-hour day, so no real agenda is
# cut short (the slot picker reads 8, `flow_router.SLOT_PICKER_MAX_SLOTS`).
FREE_SLOT_SCAN_MAX = 96

Window = tuple[datetime, datetime]


def slot_start(raw: datetime | str, tz: tzinfo | None) -> datetime:
    """A slot's start as an AWARE datetime, whatever shape the calendar gave.

    `list_free_slots` hands back naive ISO strings; a naive value is read in the clinic's
    own timezone, the only reading that can be right - the slot was computed from that
    clinic's business hours.
    """
    value = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    if value.tzinfo is None and tz is not None:
        return value.replace(tzinfo=tz)
    return value


def without_holds(
    slots: Sequence[dict],
    holds: Sequence[Window],
    *,
    duration_minutes: int,
    tz: tzinfo | None,
) -> list[dict]:
    """`slots` minus every slot whose [start, start + duration) overlaps a held window."""
    if not holds:
        return list(slots)
    length = timedelta(minutes=duration_minutes or 0)
    kept: list[dict] = []
    for slot in slots:
        start = slot_start(slot["start"], tz)
        if not any(overlaps(start, start + length, held_start, held_end) for held_start, held_end in holds):
            kept.append(slot)
    return kept


async def free_slots_for_day(
    calendar: CalendarService,
    *,
    day: date,
    duration_minutes: int,
    holds: Sequence[Window] = (),
    max_slots: int = FREE_SLOT_SCAN_MAX,
) -> list[datetime]:
    """Every free slot START on `day` (clinic-local, aware), held windows excluded.

    Raises `CalendarUnavailableError` exactly like the calendar does.
    """
    tz = calendar.tzinfo
    slots = await calendar.list_free_slots(
        day=datetime(day.year, day.month, day.day),
        slot_minutes=duration_minutes,
        max_slots=max_slots,
    )
    kept = without_holds(slots, holds, duration_minutes=duration_minutes, tz=tz)
    return [slot_start(slot["start"], tz).astimezone(tz) for slot in kept]


def _touches_day(day_start: datetime, holds: Sequence[Window], tz: tzinfo | None) -> bool:
    start = day_start if day_start.tzinfo is not None else day_start.replace(tzinfo=tz)
    end = start + timedelta(days=1)
    return any(overlaps(start, end, held_start, held_end) for held_start, held_end in holds)


async def available_day_starts(
    calendar: CalendarService,
    *,
    start: datetime,
    window_days: int,
    duration_minutes: int,
    holds: Sequence[Window] = (),
) -> list[datetime]:
    """Clinic-local midnights of the days with at least one free slot, in order.

    ONE calendar read for the whole window (`list_available_days`). With holds, only the
    days a hold touches are re-read slot by slot and dropped when nothing is left.
    """
    days = await calendar.list_available_days(
        start_day=start, days=window_days, slot_minutes=duration_minutes
    )
    if not holds:
        return days
    kept: list[datetime] = []
    for day_start in days:
        if _touches_day(day_start, holds, calendar.tzinfo) and not await free_slots_for_day(
            calendar, day=day_start.date(), duration_minutes=duration_minutes, holds=holds
        ):
            continue
        kept.append(day_start)
    return kept


async def days_with_free_slots(
    calendar: CalendarService,
    *,
    start: datetime,
    window_days: int,
    duration_minutes: int,
    holds: Sequence[Window] = (),
) -> list[date]:
    """`available_day_starts`, as dates."""
    starts = await available_day_starts(
        calendar,
        start=start,
        window_days=window_days,
        duration_minutes=duration_minutes,
        holds=holds,
    )
    return [day_start.date() for day_start in starts]
```

- [x] **Step 4: Run the module tests**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_availability.py -q`
Expected: os 5 testes do módulo e os 3 de "byte-identical"/"held hidden" PASS; `test_a_sole_professionals_hold_is_looked_up_by_their_id` FAIL (`assert [None] == [UUID(...)]`) — o seletor ainda procura reservas sob `None`.

- [x] **Step 5: Route the pickers through the module and fix the hold owner**

Em `src/secretaria/services/flow_router.py`:

1. Imports — entre o bloco `from secretaria.services.attendee import (...)` e `from secretaria.services.booking_hold import BookingGate, overlaps`, inserir:

```python
from secretaria.services.availability import available_day_starts, without_holds
```

e trocar `from secretaria.services.booking_hold import BookingGate, overlaps` por `from secretaria.services.booking_hold import BookingGate` (o `overlaps` só era usado no bloco que sai; se `uvx ruff check` não reclamar de `overlaps`, deixe como está).

2. Logo depois da função `_hold_windows` (antes de `@dataclass\nclass FlowRouterResult`), inserir:

```python
def _hold_owner(conversation: Conversation, professionals: list | None) -> UUID | None:
    """WHOSE holds a slot list must hide: the agenda a booking would be placed on.

    The doctor the conversation selected; with none selected, the clinic's sole active
    professional - because that is the `professional_id` a hold is PLACED with
    (`_handle_confirmation` -> `resolve_booking_owner_id`). Looking holds up under None on
    a single-professional clinic missed every one of them (TASK-030 P2).
    """
    selected = _selected_professional_id(conversation)
    if selected is not None:
        return selected
    return resolve_booking_owner_id(professionals, None)
```

3. Em `enter_day_picker`, trocar

```python
        days = await calendar.list_available_days(
            start_day=datetime.now(calendar.tzinfo),
            days=DAY_PICKER_WINDOW_DAYS,
            slot_minutes=duration_minutes,
        )
```

por

```python
        # No holds on purpose: the picker lists every day with free Google time and the
        # slot step hides held slots (services/availability.py's module note).
        days = await available_day_starts(
            calendar,
            start=datetime.now(calendar.tzinfo),
            window_days=DAY_PICKER_WINDOW_DAYS,
            duration_minutes=duration_minutes,
        )
```

4. Apagar a função `_slot_dt` inteira (de `def _slot_dt(raw, calendar: CalendarService | None) -> datetime:` até o `return value` dela). Em `_enter_slot_picker`, trocar

```python
    reserved = await _hold_windows(_selected_professional_id(conversation))
    if reserved:
        before = len(slots)
        slots = [
            slot
            for slot in slots
            if not any(
                overlaps(
                    _slot_dt(slot["start"], calendar),
                    _slot_dt(slot["start"], calendar)
                    + timedelta(minutes=duration_minutes or 0),
                    held_start,
                    held_end,
                )
                for held_start, held_end in reserved
            )
        ]
        if len(slots) != before:
```

por

```python
    reserved = await _hold_windows(_hold_owner(conversation, professionals))
    if reserved:
        before = len(slots)
        slots = without_holds(
            slots, reserved, duration_minutes=duration_minutes, tz=calendar.tzinfo
        )
        if len(slots) != before:
```

(o `logger.info("flow_slots_hidden_by_hold", ...)` que segue fica igual; o comentário acima de `reserved` também.)

- [x] **Step 6: Run the tests to verify they pass, plus every picker suite**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_availability.py tests/test_flow_day_picker.py tests/test_flow_router.py tests/test_booking_code_gate.py tests/test_rebooking_flow.py tests/test_flow_router_multiprofessional.py -q`
Expected: PASS (os 9 novos e todos os antigos dos seletores).

- [x] **Step 7: Lint and commit**

```bash
uvx ruff format src/secretaria/services/availability.py tests/test_availability.py
uvx ruff check src/secretaria/services/availability.py src/secretaria/services/flow_router.py tests/test_availability.py
uvx ruff format --diff src/secretaria/services/flow_router.py | grep -c '^@@'   # must not exceed the count noted before editing
git add src/secretaria/services/availability.py src/secretaria/services/flow_router.py tests/test_availability.py
git diff --cached --stat
git commit -F - <<'EOF'
refactor(flow): one definition of an agenda's free time, holds included

services/availability.py is what the day and slot pickers already computed, extracted so
the AI draft resolver and the AI availability tool read the same answer. The pickers
render byte for byte as before (regression tests). One deliberate fix: on a
single-professional clinic the slot picker looked holds up under None while they are
placed with that professional's id, so a reserved slot stayed visible.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task A2: `BookingDraft` — o rascunho v2, o sentinel e o registro com prazo

**Files:**
- Create: `src/secretaria/services/booking_draft.py`
- Modify: `src/secretaria/services/flow_router.py` (constante `FLOW_DRAFT_TTL_MINUTES` logo depois de `pending_identity_ttl_minutes`)
- Test: `tests/test_booking_draft.py`

**Interfaces:**
- Consumes: nada novo.
- Produces: `DRAFT_ATTENDEE_SELF`, `DRAFT_ATTENDEE_OTHER`, `FIELD_*`, `FIELD_NAMES`, `DRAFT_RECORD_SAVED_AT`, `BookingDraft` (com `to_dict`, `to_payload`, `from_dict`, `from_payload`, `supplied_fields`), `draft_record`, `draft_from_record`; `flow_router.FLOW_DRAFT_TTL_MINUTES = 30`.

- [x] **Step 1: Write the failing tests**

Criar `tests/test_booking_draft.py`:

```python
"""The AI booking draft v2: wire format, v1 compatibility and the parked record (TASK-030 P2)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import datetime as dt  # noqa: E402
import json  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.booking_draft import (  # noqa: E402
    BookingDraft,
    draft_from_record,
    draft_record,
)

SAVED = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.UTC)


def test_the_v2_payload_round_trips_with_the_six_keys():
    professional_id = uuid4()
    draft = BookingDraft(
        service="Consulta",
        professional_id=professional_id,
        insurance="Unimed",
        attendee="other",
        day=dt.date(2026, 10, 8),
        time=dt.time(10, 0),
    )
    assert json.loads(draft.to_payload()) == {
        "t": "Consulta",
        "p": str(professional_id),
        "i": "Unimed",
        "w": "other",
        "d": "2026-10-08",
        "h": "10:00",
    }
    assert BookingDraft.from_payload(draft.to_payload()) == draft


def test_the_v1_payload_still_parses_with_unknown_for_whom():
    draft = BookingDraft.from_payload('{"t": "Limpeza", "p": null, "i": "Unimed"}')
    assert draft == BookingDraft(service="Limpeza", insurance="Unimed")
    assert draft.attendee is None


def test_blank_strings_are_absent_and_long_ones_are_capped():
    assert BookingDraft.from_payload('{"t": "  ", "i": ""}') == BookingDraft()
    assert len(BookingDraft.from_payload(json.dumps({"t": "x" * 500})).service) == 120


@pytest.mark.parametrize(
    "raw",
    [
        "oops",
        "[]",
        "null",
        '{"t": 7}',
        '{"p": "invalid"}',
        '{"w": "Maria Silva"}',  # never a name
        '{"d": "08/10/2026"}',
        '{"d": "2026-02-30"}',
        '{"h": "10h"}',
        '{"h": "25:00"}',
    ],
)
def test_a_corrupt_payload_raises_value_error(raw):
    with pytest.raises(ValueError):
        BookingDraft.from_payload(raw)


def test_supplied_fields_names_only_what_is_present_in_order():
    draft = BookingDraft(day=dt.date(2026, 10, 8), service="Consulta", attendee="self")
    assert draft.supplied_fields() == ("service", "for_whom", "day")
    assert BookingDraft().supplied_fields() == ()


def test_a_record_round_trips_and_carries_its_timestamp():
    draft = BookingDraft(service="Consulta", attendee="other")
    record = draft_record(draft, saved_at=SAVED)
    assert record["saved_at"] == "2026-10-05T12:00:00+00:00"
    assert draft_from_record(record, now=SAVED + dt.timedelta(minutes=29)) == draft


def test_a_record_expires_after_30_minutes():
    record = draft_record(BookingDraft(service="Consulta"), saved_at=SAVED)
    assert fr.FLOW_DRAFT_TTL_MINUTES == 30
    assert draft_from_record(record, now=SAVED + dt.timedelta(minutes=31)) is None


def test_a_naive_saved_at_is_read_as_utc():
    record = {**BookingDraft(service="Consulta").to_dict(), "saved_at": "2026-10-05T12:00:00"}
    assert draft_from_record(record, now=SAVED + dt.timedelta(minutes=5)) is not None


@pytest.mark.parametrize(
    "record",
    [None, "x", {}, {"t": "Consulta"}, {"t": 7, "saved_at": "2026-10-05T12:00:00+00:00"}],
)
def test_a_missing_or_corrupt_record_is_none(record):
    assert draft_from_record(record, now=SAVED) is None


def test_draft_record_refuses_a_naive_timestamp():
    with pytest.raises(ValueError):
        draft_record(BookingDraft(), saved_at=dt.datetime(2026, 10, 5, 12, 0))
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_booking_draft.py -q`
Expected: erro de coleta — `ModuleNotFoundError: No module named 'secretaria.services.booking_draft'`.

- [x] **Step 3: Add the TTL constant**

Em `src/secretaria/services/flow_router.py`, logo depois da função `pending_identity_ttl_minutes` (antes de `def reactivation_continue_prompt`), inserir:

```python
# How long the AI's parked booking draft (`Conversation.flow_draft`, TASK-030 P2) stays
# usable while the patient answers "Essa consulta é pra você?". Past it, the answer simply
# continues the button flow: two minutes is a pause, half an hour is another conversation.
# Not per-tenant, for the same reason PENDING_IDENTITY_TTL_MINUTES is not.
FLOW_DRAFT_TTL_MINUTES = 30
```

- [x] **Step 4: Write the module**

Criar `src/secretaria/services/booking_draft.py`:

```python
"""The AI's booking draft (v2) and the ONE resolver that decides where it lands.

`set_booking_draft` (ai/tools.py) tells the workflow what the patient already said -
service, doctor, convênio, who the booking is for, day and time - and the worker decides
where the patient lands (`resolve_booking_draft`, below). The AI never writes `flow_*` and
never carries a third party's name: `attendee` is only "self"/"other" (skill
pii-field-capture-pseudonymization); the name is captured by the deterministic attendee
step, so the pseudonymizer registers it before any model sees it.

Wire format (the `__BOOKING_DRAFT__:` sentinel, ai/graph.py): compact JSON with keys `t`
(service), `p` (professional id), `i` (convênio), `w` ("self" | "other" | null), `d`
(YYYY-MM-DD, clinic timezone) and `h` (HH:MM); null for absent. The sentinel is produced
and consumed in the same worker, in the same turn - there is no cross-service contract and
no version mix - but the parser still accepts the v1 payload (`t`/`p`/`i` only), where a
missing `w` means "unknown".

The same keys plus `saved_at` are what `Conversation.flow_draft` stores while the patient
answers "Essa consulta é pra você?" (spec §4.3); `draft_from_record` drops a record older
than `flow_router.FLOW_DRAFT_TTL_MINUTES`.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from secretaria.services import flow_router as fr

DRAFT_ATTENDEE_SELF = "self"
DRAFT_ATTENDEE_OTHER = "other"

# Field NAMES, as the AI tool spells them and as the hand-back event logs them
# (workers/shared/handback_log.py FIELD_*). Never values.
FIELD_SERVICE = "service"
FIELD_PROFESSIONAL = "professional"
FIELD_INSURANCE = "insurance"
FIELD_FOR_WHOM = "for_whom"
FIELD_DAY = "day"
FIELD_TIME = "time"
FIELD_NAMES = (
    FIELD_SERVICE,
    FIELD_PROFESSIONAL,
    FIELD_INSURANCE,
    FIELD_FOR_WHOM,
    FIELD_DAY,
    FIELD_TIME,
)

DRAFT_RECORD_SAVED_AT = "saved_at"

_TEXT_MAX = 120
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_HHMM = re.compile(r"\d{2}:\d{2}")


def _text(data: dict, key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"booking draft key {key!r} must be a string")
    return value.strip()[:_TEXT_MAX] or None


@dataclass(frozen=True)
class BookingDraft:
    """What the AI says the patient asked for. Every field optional; None = not said."""

    service: str | None = None
    professional_id: UUID | None = None
    insurance: str | None = None
    attendee: Literal["self", "other"] | None = None
    day: dt.date | None = None
    time: dt.time | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "t": self.service,
            "p": str(self.professional_id) if self.professional_id is not None else None,
            "i": self.insurance,
            "w": self.attendee,
            "d": self.day.isoformat() if self.day is not None else None,
            "h": self.time.strftime("%H:%M") if self.time is not None else None,
        }

    def to_payload(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Any) -> BookingDraft:
        """Parse the six keys (unknown keys ignored). ValueError on anything malformed."""
        if not isinstance(data, dict):
            raise ValueError("booking draft must be a JSON object")
        raw_professional = _text(data, "p")
        attendee = _text(data, "w")
        if attendee not in (None, DRAFT_ATTENDEE_SELF, DRAFT_ATTENDEE_OTHER):
            raise ValueError("booking draft 'w' must be 'self', 'other' or null")
        raw_day = _text(data, "d")
        if raw_day is not None and not _ISO_DAY.fullmatch(raw_day):
            raise ValueError("booking draft 'd' must be YYYY-MM-DD")
        raw_time = _text(data, "h")
        if raw_time is not None and not _HHMM.fullmatch(raw_time):
            raise ValueError("booking draft 'h' must be HH:MM")
        return cls(
            service=_text(data, "t"),
            professional_id=UUID(raw_professional) if raw_professional else None,
            insurance=_text(data, "i"),
            attendee=attendee,  # type: ignore[arg-type]
            day=dt.date.fromisoformat(raw_day) if raw_day else None,
            time=dt.time.fromisoformat(raw_time) if raw_time else None,
        )

    @classmethod
    def from_payload(cls, raw: str) -> BookingDraft:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("booking draft is not JSON") from exc
        return cls.from_dict(data)

    def supplied_fields(self) -> tuple[str, ...]:
        """The field NAMES present, in `FIELD_NAMES` order (what the hand-back logs)."""
        values = (
            self.service,
            self.professional_id,
            self.insurance,
            self.attendee,
            self.day,
            self.time,
        )
        return tuple(
            name for name, value in zip(FIELD_NAMES, values, strict=True) if value is not None
        )


def draft_record(draft: BookingDraft, *, saved_at: dt.datetime) -> dict[str, str | None]:
    """The `Conversation.flow_draft` value: the wire keys plus an aware UTC `saved_at`."""
    if saved_at.tzinfo is None:
        raise ValueError("saved_at must be timezone-aware")
    return {**draft.to_dict(), DRAFT_RECORD_SAVED_AT: saved_at.astimezone(dt.UTC).isoformat()}


def draft_from_record(record: Any, *, now: dt.datetime) -> BookingDraft | None:
    """The parked draft, or None when absent, corrupt or older than the TTL.

    `now` must be timezone-aware. A naive `saved_at` (a hand-edited row) is read as UTC.
    """
    if not isinstance(record, dict):
        return None
    try:
        draft = BookingDraft.from_dict(record)
        saved_at = dt.datetime.fromisoformat(str(record[DRAFT_RECORD_SAVED_AT]))
    except (KeyError, ValueError):
        return None
    if saved_at.tzinfo is None:
        saved_at = saved_at.replace(tzinfo=dt.UTC)
    if now - saved_at > dt.timedelta(minutes=fr.FLOW_DRAFT_TTL_MINUTES):
        return None
    return draft
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_booking_draft.py -q`
Expected: PASS (23 testes, contando os parametrizados).

- [x] **Step 6: Lint and commit**

```bash
uvx ruff format src/secretaria/services/booking_draft.py tests/test_booking_draft.py
uvx ruff check src/secretaria/services/booking_draft.py src/secretaria/services/flow_router.py tests/test_booking_draft.py
uvx ruff format --diff src/secretaria/services/flow_router.py | grep -c '^@@'   # must not increase
git add src/secretaria/services/booking_draft.py src/secretaria/services/flow_router.py tests/test_booking_draft.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(draft): booking draft v2 wire format and the parked record

Six optional keys (t, p, i, w, d, h); "w" is only self/other, never a name. The v1
payload still parses. The parked record adds an aware saved_at and expires after 30
minutes (flow_router.FLOW_DRAFT_TTL_MINUTES).

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task A3: Coluna `conversations.flow_draft`, migração e o transporte só nas etapas de "pra quem"

**Files:**
- Modify: `src/secretaria/models/conversation.py` (import `JSON`; coluna depois de `flow_attendee_name`)
- Create: `migrations/versions/e7d3c1a9b5f2_conversation_flow_draft.py`
- Modify: `src/secretaria/services/flow_router.py` (`FlowRouterResult`: `flow_draft`, `resume_draft`; `_carry_draft`; `_carry_booking`)
- Modify: `src/secretaria/workers/shared/flow_runner.py` (`_apply_flow_result` grava `flow_draft`)
- Modify: `src/secretaria/workers/shared/state_expiry.py` (`_expire_stale_llm_state`, `_expire_stale_attendee_step`)
- Modify: `src/secretaria/workers/turn_router.py` (ramo "Não" da pergunta "quer continuar?")
- Modify: `src/secretaria/workers/orchestrator.py` (o `flow_snapshot` leva `flow_draft`)
- Create: `tests/test_flow_draft_column.py`
- Modify: `tests/test_attendee_booking.py` (testes de worker)

**Interfaces:**
- Consumes: A2 (`BookingDraft`, `draft_record`).
- Produces: `Conversation.flow_draft: Mapped[dict | None]`; `FlowRouterResult.flow_draft: dict | None = None`, `FlowRouterResult.resume_draft: bool = False`; `flow_router._carry_draft(conversation, result) -> FlowRouterResult`; revisão Alembic `e7d3c1a9b5f2` (`down_revision = "c3a9e5f1d7b2"`).

- [x] **Step 1: Write the failing tests**

Criar `tests/test_flow_draft_column.py`:

```python
"""Conversation.flow_draft: the AI draft parked while pra-quem is answered (TASK-030 P2)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import importlib.util  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import sqlalchemy as sa  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from alembic.operations import Operations  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402

from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.attendee import (  # noqa: E402
    LABEL_ATTENDEE_AUTH_BACK,
    LABEL_ATTENDEE_OTHER,
)
from secretaria.services.booking_draft import BookingDraft, draft_record  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from tests.test_flow_router import _conversation, _tenant  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REVISION = "e7d3c1a9b5f2"
RECORD = draft_record(
    BookingDraft(service="Primeira Consulta", attendee="other"),
    saved_at=datetime(2026, 10, 5, 12, 0, tzinfo=UTC),
)


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_conversation_flow_draft.py"
    spec = importlib.util.spec_from_file_location("flow_draft_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_is_the_single_head_on_top_of_patient_email():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == [REVISION]
    assert _migration().down_revision == "c3a9e5f1d7b2"


def test_the_migration_adds_and_drops_a_nullable_json_column_on_sqlite(tmp_path):
    migration = _migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'flow_draft.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE conversations (id CHAR(32) PRIMARY KEY)"))
        conn.execute(sa.text("INSERT INTO conversations (id) VALUES ('a')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            columns = {c["name"]: c for c in sa.inspect(conn).get_columns("conversations")}
            assert columns["flow_draft"]["nullable"] is True
            assert conn.execute(sa.text("SELECT flow_draft FROM conversations")).scalar() is None
            migration.downgrade()
            names = {c["name"] for c in sa.inspect(conn).get_columns("conversations")}
            assert "flow_draft" not in names
    engine.dispose()


def _parked(step, **kw):
    return _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=step,
        flow_selected_type=fr.ATTENDEE_NEXT_BOOK,
        flow_draft=RECORD,
        **kw,
    )


async def test_the_draft_rides_every_attendee_step():
    tenant = _tenant()
    name_request = await fr.route(
        _parked(fr.STEP_AWAITING_ATTENDEE_CHOICE), tenant, None, LABEL_ATTENDEE_OTHER
    )
    assert name_request.flow_step == fr.STEP_AWAITING_ATTENDEE_NAME
    assert name_request.flow_draft == RECORD

    card = await fr.route(_parked(fr.STEP_AWAITING_ATTENDEE_NAME), tenant, None, "maria da silva")
    assert card.flow_step == fr.STEP_AWAITING_ATTENDEE_AUTH
    assert card.flow_draft == RECORD

    # The patient gives up on the authorization: back to the question, draft kept.
    back = await fr.route(
        _parked(fr.STEP_AWAITING_ATTENDEE_AUTH, flow_attendee_name="Maria da Silva"),
        tenant,
        None,
        LABEL_ATTENDEE_AUTH_BACK,
    )
    assert back.flow_step == fr.STEP_AWAITING_ATTENDEE_CHOICE
    assert back.flow_draft == RECORD

    free_text = await fr.route(
        _parked(fr.STEP_AWAITING_ATTENDEE_CHOICE), tenant, None, "hmm, deixa eu pensar"
    )
    assert free_text.action == "delegate_llm"
    assert free_text.flow_draft == RECORD


async def test_the_draft_is_dropped_by_anything_that_leaves_the_attendee_steps():
    tenant = _tenant()
    on_service = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SERVICE,
        flow_draft=RECORD,
    )
    detail = await fr.route(on_service, tenant, None, "Primeira Consulta")
    assert detail.flow_step == fr.STEP_AWAITING_SERVICE_CONFIRM
    assert detail.flow_draft is None

    llm = await fr.route(_conversation(flow_state=FlowState.LLM, flow_draft=RECORD), tenant, None, "oi")
    assert llm.flow_draft is None


def _stale(state, step):
    return SimpleNamespace(
        flow_state=state,
        flow_step=step,
        flow_selected_type=fr.ATTENDEE_NEXT_BOOK,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_managing_appointment_id=None,
        flow_attendee_name="Maria da Silva",
        flow_draft=RECORD,
    )


def test_the_attendee_step_floor_drops_the_draft():
    conversation = _stale(FlowState.SERVICE_CATALOG, fr.STEP_AWAITING_ATTENDEE_AUTH)
    stale = datetime.now(UTC) - timedelta(days=3)
    assert tasks._expire_stale_attendee_step(conversation, SimpleNamespace(initial_flows={}), stale)
    assert conversation.flow_draft is None
    assert conversation.flow_attendee_name is None


def test_the_llm_floor_drops_the_draft():
    conversation = _stale(FlowState.LLM, None)
    stale = datetime.now(UTC) - timedelta(days=3)
    assert tasks._expire_stale_llm_state(conversation, SimpleNamespace(initial_flows={}), stale)
    assert conversation.flow_draft is None
```

Em `tests/test_attendee_booking.py`:

1. No bloco de imports, acrescentar (ordem do ruff `I001`): `from secretaria.services.booking_draft import BookingDraft, draft_record  # noqa: E402` imediatamente antes de `from secretaria.services.channel_sender import CHANNEL_WHATSAPP  # noqa: E402`; no import de `secretaria.services.flow_router`, acrescentar `ATTENDEE_NEXT_BOOK,` antes de `LABEL_BOOK,`. Os demais nomes usados abaixo (`FlowRouterResult`, `TextBubble`, `STEP_AWAITING_*`, `Message`, `update`, `UTC`, `timedelta`, `_wam_seq`, `LABEL_ATTENDEE_OTHER`) já estão importados/definidos no arquivo.

2. Ao fim do arquivo, acrescentar:

```python
# --------------------------------------------------------------------------
# 5. TASK-030 P2: the AI draft parked on the conversation (flow_draft)
# --------------------------------------------------------------------------


async def _park_with_draft(db, tenant, *, step, attendee=None):
    conversation = await _conversation(db, tenant)
    async with db() as session:
        async with session.begin():
            row = await session.get(Conversation, conversation.id)
            row.flow_state = FlowState.SERVICE_CATALOG
            row.flow_step = step
            row.flow_selected_type = ATTENDEE_NEXT_BOOK
            row.flow_attendee_name = attendee
            row.flow_draft = draft_record(
                BookingDraft(service="Primeira Consulta"), saved_at=datetime.now(UTC)
            )
    return conversation


async def test_worker_apply_writes_and_clears_the_draft(wired) -> None:
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    conversation = await _conversation(db, tenant)
    reply = tasks._ReplyContext(
        channel=CHANNEL_WHATSAPP,
        conversation_id=conversation.id,
        patient_ref=WA_ID,
        inbound_body="x",
        tenant_id=tenant.id,
    )
    record = draft_record(BookingDraft(service="Primeira Consulta"), saved_at=datetime.now(UTC))
    parked = FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body="ok")],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_ATTENDEE_CHOICE,
        flow_draft=record,
    )
    await tasks._apply_flow_result(reply, parked, WA_ID, redis=None, tenant=tenant, waba_token="t")
    assert (await _conversation(db, tenant)).flow_draft == record

    moved_on = FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body="ok")],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE,
    )
    await tasks._apply_flow_result(reply, moved_on, WA_ID, redis=None, tenant=tenant, waba_token="t")
    assert (await _conversation(db, tenant)).flow_draft is None


async def test_worker_snapshot_carries_the_draft_into_the_name_step(wired) -> None:
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    await _park_with_draft(db, tenant, step=STEP_AWAITING_ATTENDEE_CHOICE)

    await _wa_turn(tenant, LABEL_ATTENDEE_OTHER)

    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_ATTENDEE_NAME
    assert conversation.flow_draft["t"] == "Primeira Consulta"


async def test_worker_attendee_floor_drops_the_draft(wired) -> None:
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    conversation = await _park_with_draft(
        db, tenant, step=STEP_AWAITING_ATTENDEE_AUTH, attendee="Maria da Silva"
    )
    async with db() as session:
        async with session.begin():
            await session.execute(
                update(Message)
                .where(Message.conversation_id == conversation.id)
                .values(created_at=datetime.now(UTC) - timedelta(days=3))
            )
    # Only the inbound leg: the in-place expiry is what is under test, not the reply.
    await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=WA_ID,
        patient_name="Perfil",
        wam_id=f"wamid.in.{next(_wam_seq)}",
        body="Maria bom dia",
    )
    conversation = await _conversation(db, tenant)
    assert conversation.flow_draft is None
    assert conversation.flow_attendee_name is None


async def test_worker_no_to_quer_continuar_drops_the_draft(wired) -> None:
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    conversation = await _park_with_draft(db, tenant, step=STEP_AWAITING_ATTENDEE_CHOICE)
    async with db() as session:
        async with session.begin():
            row = await session.get(Conversation, conversation.id)
            row.reactivation_origin = FlowState.SERVICE_CATALOG.value

    await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=WA_ID,
        patient_name="Perfil",
        wam_id=f"wamid.in.{next(_wam_seq)}",
        body="Não",
    )
    conversation = await _conversation(db, tenant)
    assert conversation.reactivation_origin is None
    assert conversation.flow_draft is None
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_flow_draft_column.py tests/test_attendee_booking.py -q`
Expected: FAIL — o arquivo da migração não existe (`FileNotFoundError` no `_migration()`), `FlowRouterResult.__init__() got an unexpected keyword argument 'flow_draft'`, e `AttributeError: 'Conversation' object has no attribute 'flow_draft'` nos testes de worker.

- [x] **Step 3: Model column and migration**

Em `src/secretaria/models/conversation.py`, trocar `from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, String, UniqueConstraint, func` por

```python
from sqlalchemy import JSON, DateTime, Enum as SAEnum, ForeignKey, String, UniqueConstraint, func
```

e, logo depois da coluna `flow_attendee_name` (antes do comentário de `reactivation_origin`), inserir:

```python
    # The AI's booking draft parked while the patient answers "Essa consulta é pra você?"
    # (TASK-030 P2, services/booking_draft.py): {"t", "p", "i", "w", "d", "h", "saved_at"},
    # never a third party's name ("w" is only self/other). NULL whenever no draft waits.
    # Written unconditionally by `_apply_flow_result` like every flow field, and carried
    # only on the attendee steps (`flow_router._carry_draft`).
    flow_draft: Mapped[dict | None] = mapped_column(JSON, nullable=True)
```

Criar `migrations/versions/e7d3c1a9b5f2_conversation_flow_draft.py`:

```python
"""conversations.flow_draft: the AI booking draft parked while pra-quem is answered.

TASK-030 P2 (docs/superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md §4.3).
When the AI hands a booking back and "Essa consulta é pra você?" is still unanswered, the
workflow asks it first. What the AI had already resolved (service, doctor, convênio, day,
time) had nowhere to wait: during those steps `flow_selected_type` holds the pra-quem entry
marker. The draft waits here:

    ADD  conversations.flow_draft  JSON NULL
         {"t", "p", "i", "w", "d", "h", "saved_at"}   (services/booking_draft.py)

No third party's name ever lands here - "w" is only "self"/"other". Every flow result that
does not name it clears it, and the two silence floors (workers/shared/state_expiry.py)
clear it too.

DEPLOY ORDER - the database moves FIRST (same reason as 4c8e2a7f1b93): the ORM names every
mapped column on every read and write of `conversations`, so a process running the new
model against a schema WITHOUT this column fails every turn. The column is inert to the
old code, so:

    1. `alembic upgrade head` from the NEW image (one-off), both services still on the old
       code;
    2. deploy `secretaria_api` AND `secretaria-worker` together (README: "Deploy both
       services, or neither"; `GET /build` must report parity `match`).

Rollback narrows, so it goes the other way round: the OLD code on both services first,
then `alembic downgrade c3a9e5f1d7b2`. The only data lost is a draft parked in between; that
patient's pra-quem answer then continues the plain button flow.

Revision ID: e7d3c1a9b5f2
Revises: c3a9e5f1d7b2
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7d3c1a9b5f2"
down_revision: str | None = "c3a9e5f1d7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no default: metadata-only on Postgres (no table rewrite), and NULL is the
    # true value for every row that exists today.
    op.add_column("conversations", sa.Column("flow_draft", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "flow_draft")
```

- [x] **Step 4: Router fields and the carry**

Em `src/secretaria/services/flow_router.py`:

1. Em `FlowRouterResult`, logo depois de `attendee_authorized: bool = False`, inserir:

```python
    # The AI's booking draft parked while the patient answers pra-quem (TASK-030 P2,
    # services/booking_draft.py record shape). Written unconditionally by the caller like
    # every field above; `_carry_booking` keeps it ONLY on the attendee steps, so it is
    # cleared the moment the conversation leaves them.
    flow_draft: dict | None = None
    # True on the result of the tap that answers pra-quem while a draft is parked: the
    # worker (workers/shared/draft_resolution.py::_resume_booking_draft) re-runs the
    # resolver over the draft instead of showing this result's list. Never persisted.
    resume_draft: bool = False
```

2. Trocar a função `_carry_booking` inteira por:

```python
def _carry_draft(conversation: Conversation, result: FlowRouterResult) -> FlowRouterResult:
    """Keep the AI's parked draft while the patient is still on the pra-quem steps.

    `_apply_flow_result` writes `flow_draft` unconditionally, so any result that does not
    name it clears it - which is the point: the draft only waits for pra-quem (spec §4.3),
    and a result on any other step (the menu, the LLM, a list) ends that wait.
    """
    if result.flow_draft is None and result.flow_step in ATTENDEE_STEPS:
        result.flow_draft = getattr(conversation, "flow_draft", None)
    return result


def _carry_booking(conversation: Conversation, result: FlowRouterResult) -> FlowRouterResult:
    """The three carries, applied once per public entry (route, resume, hand-back)."""
    return _carry_draft(
        conversation, _carry_insurance(conversation, _carry_attendee(conversation, result))
    )
```

- [x] **Step 5: Persist, expire, reset and snapshot**

1. `src/secretaria/workers/shared/flow_runner.py`, em `_apply_flow_result`, logo depois de `conv.flow_attendee_name = result.flow_attendee_name`, inserir:

```python
                    conv.flow_draft = result.flow_draft
```

2. `src/secretaria/workers/shared/state_expiry.py`: em `_expire_stale_llm_state`, logo depois de `conversation.flow_attendee_name = None`, inserir

```python
    # The AI's parked draft belongs to the same abandoned booking (TASK-030 P2).
    conversation.flow_draft = None
```

e em `_expire_stale_attendee_step`, logo depois de `conversation.flow_attendee_name = None` (antes do `return True`), inserir

```python
    conversation.flow_draft = None
```

3. `src/secretaria/workers/turn_router.py`, no ramo `if answer == "no":` da pergunta "quer continuar?", logo depois de `conversation.flow_attendee_name = None`, inserir:

```python
            conversation.flow_draft = None
```

4. `src/secretaria/workers/orchestrator.py`, no `SimpleNamespace(...)` do `flow_snapshot`, logo depois de `flow_attendee_name=conversation.flow_attendee_name,`, inserir:

```python
                            flow_draft=conversation.flow_draft,
```

- [x] **Step 6: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_flow_draft_column.py tests/test_attendee_booking.py tests/test_llm_state_expiry.py tests/test_reactivation.py tests/test_flow_router.py -q`
Expected: PASS.

- [x] **Step 7: Prove the migration on a disposable Postgres (port 5433)**

```bash
docker run -d --rm --name task030-pg -e POSTGRES_USER=secretaria -e POSTGRES_PASSWORD=secretaria -e POSTGRES_DB=secretaria -p 5433:5432 postgres:16-alpine
docker exec task030-pg pg_isready -U secretaria      # repeat until it prints "accepting connections"
export MIGRATION_ENV='APP_ENV=test META_APP_SECRET=x META_VERIFY_TOKEN=x META_ACCESS_TOKEN=x META_PHONE_NUMBER_ID=1 OPENAI_API_KEY=x ENCRYPTION_KEY=gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w= DATABASE_URL=postgresql+asyncpg://secretaria:secretaria@127.0.0.1:5433/secretaria'
env $MIGRATION_ENV uv run alembic upgrade head
docker exec task030-pg psql -U secretaria -c '\d conversations' | grep flow_draft
env $MIGRATION_ENV uv run alembic downgrade -1
docker exec task030-pg psql -U secretaria -c '\d conversations' | grep -c flow_draft
env $MIGRATION_ENV uv run alembic upgrade head
docker stop task030-pg
```

Expected: o primeiro `grep` mostra ` flow_draft | json |` (nullable, sem default); o `grep -c` depois do downgrade imprime `0`; o segundo `upgrade head` termina sem erro. Se a porta 5433 já estiver ocupada pelo `brain-postgres`, use um banco descartável nele: `docker exec brain-postgres createdb -U brain task030_draft`, troque o `DATABASE_URL` por `postgresql+asyncpg://brain:brain@127.0.0.1:5433/task030_draft`, rode os mesmos comandos (com `psql -U brain -d task030_draft`) e termine com `docker exec brain-postgres dropdb -U brain task030_draft`. Cole a saída no relatório do worker.

- [x] **Step 8: Lint and commit**

```bash
uvx ruff format tests/test_flow_draft_column.py
uvx ruff check src/secretaria/models/conversation.py src/secretaria/services/flow_router.py src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/shared/state_expiry.py src/secretaria/workers/turn_router.py src/secretaria/workers/orchestrator.py tests/test_flow_draft_column.py tests/test_attendee_booking.py
for f in src/secretaria/models/conversation.py src/secretaria/services/flow_router.py src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/shared/state_expiry.py src/secretaria/workers/turn_router.py src/secretaria/workers/orchestrator.py tests/test_attendee_booking.py; do echo "$f $(uvx ruff format --diff $f 2>/dev/null | grep -c '^@@')"; done   # none may exceed its count before editing
git add migrations/versions/e7d3c1a9b5f2_conversation_flow_draft.py src/secretaria/models/conversation.py src/secretaria/services/flow_router.py src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/shared/state_expiry.py src/secretaria/workers/turn_router.py src/secretaria/workers/orchestrator.py tests/test_flow_draft_column.py tests/test_attendee_booking.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(flow): conversations.flow_draft parks the AI draft while pra-quem is answered

Additive nullable JSON column (migration e7d3c1a9b5f2, before both services). The router
carries it only on the three attendee steps; every other result clears it, and both
silence floors and the "Não" to "quer continuar?" drop it.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task A4: O resolvedor — pra quem, convênio, profissional e serviço

**Files:**
- Modify: `src/secretaria/services/booking_draft.py` (resolvedor, códigos, `DraftResolution`, `landing_step`)
- Modify: `src/secretaria/services/flow_router.py` (`booking_gate_scope`; `route` usa o escopo; `_clinic_service_catalog` e `_professionals_offering` aceitam o catálogo; `_handle_insurance` respeita o médico escolhido / o serviço já escolhido)
- Test: `tests/test_booking_draft_resolver.py`

**Interfaces:**
- Consumes: A2 (`BookingDraft`, `draft_record`, `FIELD_*`); A3 (`FlowRouterResult.flow_draft`); flow_router (`_DayPickerState`, `_attendee_question`, `_attendee_name_request`, `_enter_insurance`, `_enter_professional_list`, `_enter_professional_services`, `_start_booking`, `_ask_day`, `_match_service`, `_find_professional_by_id`, `_insurance_step_skip_reason`, `match_insurance_plan`, `_menu_bubbles`, `ATTENDEE_NEXT_BOOK`).
- Produces: `DROP_*`, `DROP_REASONS`, `FALLBACK_*`, `FALLBACK_REASONS`; `DraftResolution`; `landing_step(result) -> str`; `CalendarSource`; `resolve_booking_draft(...)` (assinatura em "Interfaces"; o pouso na etapa do dia nesta tarefa é o seletor de dias — A5 completa dia e horário); `flow_router.booking_gate_scope(gate)`; `flow_router._clinic_service_catalog(tenant, professionals, services=None)`; `flow_router._professionals_offering(tenant, professionals, service_name, services=None)`.

- [x] **Step 1: Write the failing tests**

Criar `tests/test_booking_draft_resolver.py`:

```python
"""resolve_booking_draft: where the AI's draft lands, item by item (TASK-030 P2, spec §4.2)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import datetime as dt  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.models import FlowState  # noqa: E402
from secretaria.services import booking_draft as bd, flow_router as fr  # noqa: E402
from secretaria.services.attendee import (  # noqa: E402
    ATTENDEE_NAME_REQUEST,
    ATTENDEE_QUESTION_BODY,
    ATTENDEE_SELF,
)
from secretaria.services.booking_draft import (  # noqa: E402
    BookingDraft,
    draft_from_record,
    resolve_booking_draft,
)
from secretaria.services.calendar import CalendarUnavailableError  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
NOW = dt.datetime(2026, 10, 5, 9, 0, tzinfo=TZ)  # a Monday
DAY = dt.date(2026, 10, 8)  # the Thursday after


def _tenant(**kw):
    base = dict(
        initial_flows={},
        appointment_types=[
            {"name": "Consulta", "duration_min": 30, "is_active": True, "sort_order": 0},
            {"name": "Retorno", "duration_min": 20, "is_active": True, "sort_order": 1},
        ],
        appointment_duration_min=30,
        business_hours={"thursday": [{"start": "08:00", "end": "12:00"}]},
        collect_insurance=False,
        insurance_mode=None,
        insurances=[],
        insurance_plans=None,
        insurance_accepted_by={},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _doctor(name, services, hours=None):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=None,
        about=None,
        context_doctor_message=None,
        appointment_types=[
            {"name": s, "duration_min": 30, "is_active": True, "sort_order": i}
            for i, s in enumerate(services)
        ],
        business_hours=(
            hours if hours is not None else {"thursday": [{"start": "08:00", "end": "12:00"}]}
        ),
    )


def _sole():
    """The single professional: inherits the clinic's services and hours (NULL columns)."""
    return SimpleNamespace(
        id=uuid4(), name="Dra. Única", specialty=None, about=None,
        context_doctor_message=None, appointment_types=None, business_hours=None,
    )


def _conv(**kw):
    base = dict(
        id=uuid4(),
        tenant_id=None,  # no tenant: the default gate answers "no holds" without a DB
        flow_state=FlowState.LLM,
        flow_step=None,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_managing_appointment_id=None,
        flow_attendee_name=ATTENDEE_SELF,
        flow_draft=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _Cal:
    """Free times per day; can be made unavailable."""

    def __init__(self, free=None, unavailable=False):
        self.tzinfo = TZ
        self.free = free if free is not None else {DAY: ["08:00", "08:30", "10:00"]}
        self.unavailable = unavailable
        self.slot_reads: list = []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        if self.unavailable:
            raise CalendarUnavailableError("down")
        return [dt.datetime(d.year, d.month, d.day, tzinfo=TZ) for d in sorted(self.free) if self.free[d]]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        if self.unavailable:
            raise CalendarUnavailableError("down")
        self.slot_reads.append((day.date(), max_slots))
        times = self.free.get(day.date(), [])[:max_slots]
        return [{"start": f"{day.date().isoformat()}T{t}", "end": "", "label": t} for t in times]


def _source(cal, asked=None):
    async def _get(professional):
        if asked is not None:
            asked.append(professional)
        return cal

    return _get


async def _resolve(draft, *, conv=None, tenant=None, pros=(), cal=None, asked=None, insurance=None):
    return await resolve_booking_draft(
        draft,
        conversation=conv if conv is not None else _conv(),
        tenant=tenant if tenant is not None else _tenant(),
        professional_rows=list(pros),
        service_catalog=[],
        tenant_insurance=insurance,
        calendar=_source(cal if cal is not None else _Cal(), asked),
        now=NOW,
    )


# --------------------------------------------------------------------------
# Pra quem
# --------------------------------------------------------------------------


async def test_unknown_pra_quem_asks_first_and_parks_the_rest():
    asked: list = []
    res = await _resolve(
        BookingDraft(service="consulta", day=DAY), conv=_conv(flow_attendee_name=None), asked=asked
    )
    assert res.landing_step == fr.STEP_AWAITING_ATTENDEE_CHOICE
    assert res.result.bubbles[0].body == ATTENDEE_QUESTION_BODY
    # The marker keeps its job; the service waits in the draft, canonical.
    assert res.result.flow_selected_type == fr.ATTENDEE_NEXT_BOOK
    parked = draft_from_record(res.result.flow_draft, now=NOW)
    assert (parked.service, parked.day) == ("Consulta", DAY)
    assert res.accepted == ("service", "day")
    assert asked == []  # no agenda is built to ask a question


async def test_other_with_a_service_asks_the_name_and_parks_the_service():
    res = await _resolve(
        BookingDraft(service="Consulta", attendee="other"), conv=_conv(flow_attendee_name=None)
    )
    assert res.landing_step == fr.STEP_AWAITING_ATTENDEE_NAME
    assert res.result.bubbles[0].body == ATTENDEE_NAME_REQUEST
    parked = draft_from_record(res.result.flow_draft, now=NOW)
    assert (parked.service, parked.attendee) == ("Consulta", "other")
    assert res.accepted == ("service", "for_whom")


async def test_other_with_an_authorized_name_already_recorded_goes_on():
    res = await _resolve(
        BookingDraft(service="Consulta", attendee="other"),
        conv=_conv(flow_attendee_name="Maria da Silva"),
    )
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.result.flow_attendee_name == "Maria da Silva"


async def test_me_overrides_a_recorded_third_party():
    res = await _resolve(
        BookingDraft(service="Consulta", attendee="self"),
        conv=_conv(flow_attendee_name="Maria da Silva"),
    )
    assert res.result.flow_attendee_name == ATTENDEE_SELF


# --------------------------------------------------------------------------
# Convênio
# --------------------------------------------------------------------------


def _collecting(**kw):
    return _tenant(collect_insurance=True, insurance_mode="shared", insurances=["Unimed"], **kw)


async def test_an_unknown_plan_is_dropped_and_the_convenio_is_asked():
    res = await _resolve(BookingDraft(service="Consulta", insurance="Plano X"), tenant=_collecting())
    assert res.landing_step == fr.STEP_AWAITING_INSURANCE
    assert res.dropped == {"insurance": "unmatched_plan"}
    assert res.result.flow_selected_type == "Consulta"  # rides to the day picker after it


async def test_a_known_plan_is_recorded_canonically():
    res = await _resolve(BookingDraft(service="Consulta", insurance="unimed"), tenant=_collecting())
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.result.flow_selected_insurance == "Unimed"


async def test_a_recorded_typed_convenio_is_kept_verbatim():
    """A typed "Outro convênio" is the patient's answer, not something to re-validate."""
    res = await _resolve(
        BookingDraft(service="Consulta"),
        conv=_conv(flow_selected_insurance="Amil Dental"),
        tenant=_collecting(),
    )
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.result.flow_selected_insurance == "Amil Dental"


@pytest.mark.parametrize("typed, recorded", [("Plano X", None), ("unimed", "Unimed")])
async def test_a_clinic_that_does_not_collect_never_asks_the_convenio(typed, recorded):
    tenant = _tenant(collect_insurance=True, insurance_mode=None, insurances=["Unimed"])
    res = await _resolve(BookingDraft(service="Consulta", insurance=typed), tenant=tenant)
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.result.flow_selected_insurance == recorded


# --------------------------------------------------------------------------
# Profissional e serviço
# --------------------------------------------------------------------------


async def test_a_doctor_who_does_not_offer_the_service_keeps_the_doctor():
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Retorno"])
    res = await _resolve(
        BookingDraft(service="Retorno", professional_id=ana.id), pros=[ana, beto]
    )
    assert res.landing_step == fr.STEP_AWAITING_SERVICE
    assert res.dropped == {"service": "not_offered_by_professional"}
    assert res.result.flow_selected_professional_id == ana.id
    assert res.accepted == ("professional",)


async def test_a_service_only_one_doctor_offers_picks_that_doctor():
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Retorno"])
    asked: list = []
    res = await _resolve(BookingDraft(service="retorno"), pros=[ana, beto], asked=asked)
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.result.flow_selected_professional_id == beto.id
    assert res.result.flow_selected_type == "Retorno"
    assert asked == [beto]


async def test_a_recorded_doctor_who_does_not_offer_the_new_service_gives_way():
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Retorno"])
    res = await _resolve(
        BookingDraft(service="Retorno"),
        conv=_conv(flow_selected_professional_id=ana.id),
        pros=[ana, beto],
    )
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.result.flow_selected_professional_id == beto.id
    assert res.dropped == {}  # the recorded doctor was not SUPPLIED, so nothing is "dropped"


async def test_a_service_two_doctors_offer_asks_the_doctor_and_keeps_the_service():
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Consulta"])
    res = await _resolve(BookingDraft(service="Consulta"), pros=[ana, beto])
    assert res.landing_step == fr.STEP_AWAITING_PROFESSIONAL
    assert res.result.flow_selected_type == "Consulta"
    assert res.result.flow_selected_professional_id is None


async def test_an_unknown_doctor_is_dropped_and_the_doctor_list_shown():
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Consulta"])
    res = await _resolve(BookingDraft(professional_id=uuid4()), pros=[ana, beto])
    assert res.landing_step == fr.STEP_AWAITING_PROFESSIONAL
    assert res.dropped == {"professional": "unknown_professional"}


async def test_a_service_not_in_the_catalog_is_dropped_and_the_service_list_shown():
    res = await _resolve(BookingDraft(service="Botox"))
    assert res.landing_step == fr.STEP_AWAITING_SERVICE
    assert res.dropped == {"service": "not_in_catalog"}
    assert res.fallback is None  # the menu is NOT where an invalid item lands


async def test_a_chosen_doctor_with_no_services_reaches_the_clinic_alert():
    ana, beto = _doctor("Dra. Ana", []), _doctor("Dr. Beto", ["Consulta"])
    res = await _resolve(BookingDraft(professional_id=ana.id), pros=[ana, beto])
    assert res.result.action == "professional_config_incomplete"
    assert res.landing_step == "config_incomplete"
    assert res.fallback == "professional_config_incomplete"
    assert res.accepted == ("professional",)


async def test_no_bookable_catalog_falls_back_to_the_plain_menu():
    res = await _resolve(BookingDraft(service="Consulta"), tenant=_tenant(appointment_types=[]))
    assert res.result.flow_state == FlowState.MENU
    assert [type(b).__name__ for b in res.result.bubbles] == ["MenuBubble"]
    assert (res.landing_step, res.fallback) == ("menu", "no_bookable_catalog")


async def test_another_tenants_roster_and_catalog_are_never_used():
    foreign = _doctor("Dr. De Outra Clínica", ["Ortodontia"])
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Consulta"])
    asked: list = []
    res = await _resolve(
        BookingDraft(service="Ortodontia", professional_id=foreign.id),
        pros=[ana, beto],
        asked=asked,
    )
    assert res.dropped == {"professional": "unknown_professional", "service": "not_in_catalog"}
    assert res.landing_step == fr.STEP_AWAITING_PROFESSIONAL
    assert res.result.flow_selected_professional_id is None
    assert asked == []


async def test_a_single_professional_is_implicit():
    asked: list = []
    res = await _resolve(BookingDraft(service="Consulta"), pros=[_sole()], asked=asked)
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.result.flow_selected_professional_id is None
    assert asked == [None]  # the tenant-level agenda, which is the sole doctor's


async def test_the_pra_quem_marker_is_never_read_as_a_service():
    res = await _resolve(BookingDraft(), conv=_conv(flow_selected_type=fr.ATTENDEE_NEXT_BOOK))
    assert res.landing_step == fr.STEP_AWAITING_SERVICE


def test_landing_step_names_the_step_or_the_pseudo_step():
    def r(**kw):
        return fr.FlowRouterResult(**kw)

    assert bd.landing_step(r(action="reply", flow_state=FlowState.SERVICE_CATALOG, flow_step="awaiting_day")) == "awaiting_day"
    assert bd.landing_step(r(action="reply", flow_state=FlowState.MENU)) == "menu"
    assert bd.landing_step(r(action="calendar_unavailable", flow_state=FlowState.SERVICE_CATALOG, flow_step="awaiting_day")) == "human_handover"
    assert bd.landing_step(r(action="professional_config_incomplete", flow_state=FlowState.IDLE)) == "config_incomplete"


# --------------------------------------------------------------------------
# Router support the resolver relies on
# --------------------------------------------------------------------------


def test_booking_gate_scope_publishes_and_always_resets():
    gate = object()
    assert fr._ACTIVE_GATE.get() is None
    with pytest.raises(RuntimeError):
        with fr.booking_gate_scope(gate):
            assert fr._ACTIVE_GATE.get() is gate
            raise RuntimeError("boom")
    assert fr._ACTIVE_GATE.get() is None


async def test_the_insurance_answer_continues_to_the_chosen_doctors_services():
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Retorno"])
    conv = _conv(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_INSURANCE,
        flow_selected_professional_id=ana.id,
    )
    res = await fr._handle_insurance(conv, _collecting(), "Unimed", professionals=[ana, beto])
    assert res.flow_step == fr.STEP_AWAITING_SERVICE
    assert res.flow_selected_professional_id == ana.id
    assert res.flow_selected_insurance == "Unimed"


async def test_the_insurance_answer_with_a_service_but_no_doctor_lists_the_doctors():
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Consulta"])
    conv = _conv(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_INSURANCE,
        flow_selected_type="Consulta",
    )
    res = await fr._handle_insurance(conv, _collecting(), "Unimed", professionals=[ana, beto])
    assert res.flow_step == fr.STEP_AWAITING_PROFESSIONAL
    assert res.flow_selected_type == "Consulta"
    assert res.flow_selected_insurance == "Unimed"
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_booking_draft_resolver.py -q`
Expected: erro de coleta — `ImportError: cannot import name 'resolve_booking_draft' from 'secretaria.services.booking_draft'`.

- [x] **Step 3: Router support**

Em `src/secretaria/services/flow_router.py`:

1. Imports stdlib: trocar

```python
import re
from contextvars import ContextVar
```

por

```python
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
```

2. Logo depois de `_hold_owner` (Task A1), inserir:

```python
@contextmanager
def booking_gate_scope(gate: BookingGate | None) -> Iterator[None]:
    """Publish `gate` on `_ACTIVE_GATE` for the block, and ALWAYS take it down again.

    `route()` runs inside one; so does the AI draft resolver (services/booking_draft.py),
    which runs OUTSIDE `route()` and would otherwise see a slot another conversation is
    holding as free (TASK-030 P2, spec §7).
    """
    token = _ACTIVE_GATE.set(gate)
    try:
        yield
    finally:
        _ACTIVE_GATE.reset(token)
```

3. Em `route`, trocar

```python
    token = _ACTIVE_GATE.set(gate)
    try:
        result = await _route(
```

por

```python
    with booking_gate_scope(gate):
        result = await _route(
```

e trocar as linhas finais

```python
        return _carry_booking(conversation, result)
    finally:
        _ACTIVE_GATE.reset(token)
```

por

```python
        return _carry_booking(conversation, result)
```

(os argumentos de `_route(...)` entre elas não mudam; mantenha a indentação de 8 espaços.)

4. Trocar a assinatura e a linha do laço de `_clinic_service_catalog`:

```python
def _clinic_service_catalog(tenant: Tenant, professionals: list) -> list[dict]:
```

por

```python
def _clinic_service_catalog(
    tenant: Tenant, professionals: list, services: list | None = None
) -> list[dict]:
```

e, no corpo, `for service in professional_appointment_types(professional, tenant):` por `for service in professional_appointment_types(professional, tenant, services):`. Acrescentar ao fim da docstring: `` `services` is the clinic's canonical catalog; omitted, the entries are read as stored (every pre-TASK-030 caller).``

5. Trocar `_professionals_offering` inteira por:

```python
def _professionals_offering(
    tenant: Tenant, professionals: list, service_name: str, services: list | None = None
) -> list:
    """The active professionals whose own catalog contains `service_name`."""
    return [
        professional
        for professional in professionals or []
        if canonical_service_name(
            professional_appointment_types(professional, tenant, services), service_name
        )
        is not None
    ]
```

6. Em `_handle_insurance`, trocar o bloco

```python
    result = None
    if not conversation.flow_selected_type:
        result = _start_booking(tenant, professionals, insurance=stored)
        if result.flow_state != FlowState.SERVICE_CATALOG:
```

por

```python
    result = None
    selected = _find_professional_by_id(professionals, _selected_professional_id(conversation))
    multi = _is_multi_professional(professionals)
    if not conversation.flow_selected_type and multi and selected is not None:
        # The doctor is already chosen (an AI draft landed here with the doctor in hand,
        # TASK-030): their own services next - never the doctor list again. A doctor with
        # nothing configured ends in the clinic alert, returned as-is.
        result = _enter_professional_services(selected, tenant)
    elif conversation.flow_selected_type and multi and selected is None:
        # The service is known but not the doctor (an AI draft whose service two or more
        # doctors offer): the doctor list with the service kept, so the tap lands on that
        # service's card (STEP_AWAITING_PROFESSIONAL with a stored type).
        result = _enter_professional_list(tenant, professionals or [], insurance=stored)
        result.flow_selected_type = conversation.flow_selected_type
    elif not conversation.flow_selected_type:
        result = _start_booking(tenant, professionals, insurance=stored)
        if result.flow_state != FlowState.SERVICE_CATALOG:
```

(o corpo do `if result.flow_state != FlowState.SERVICE_CATALOG:` — comentário e `result = None` — e o `if result is None:` seguinte ficam iguais.)

- [x] **Step 4: The resolver (pra quem → convênio → profissional → serviço → seletor de dias)**

Em `src/secretaria/services/booking_draft.py`:

1. Trocar o bloco de imports por:

```python
from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from secretaria.models import FlowState
from secretaria.services import flow_router as fr
from secretaria.services.attendee import ATTENDEE_SELF, real_attendee_name
from secretaria.services.booking_hold import BookingGate
from secretaria.services.booking_scope import sole_active_professional
from secretaria.services.flow_router import FlowRouterResult
from secretaria.services.tenant_config import (
    active_appointment_types,
    professional_appointment_types,
)

if TYPE_CHECKING:
    from secretaria.services.calendar import CalendarService
```

2. Ao fim do arquivo, acrescentar:

```python
# ---------------------------------------------------------------------------
# The resolver (spec §4.2): one place decides where the draft lands
# ---------------------------------------------------------------------------

# Why a supplied item did not survive. Same strings as workers/shared/handback_log.py
# DROP_* (the first three are P1's; P2b adds the rest to that vocabulary).
DROP_NOT_IN_CATALOG = "not_in_catalog"
DROP_UNKNOWN_PROFESSIONAL = "unknown_professional"
DROP_UNMATCHED_PLAN = "unmatched_plan"
DROP_NOT_OFFERED_BY_PROFESSIONAL = "not_offered_by_professional"
DROP_OUT_OF_WINDOW = "out_of_window"
DROP_DAY_UNAVAILABLE = "day_unavailable"
DROP_NO_FREE_SLOT = "no_free_slot"
DROP_MISSING_DAY = "missing_day"
DROP_REASONS = (
    DROP_NOT_IN_CATALOG,
    DROP_UNKNOWN_PROFESSIONAL,
    DROP_UNMATCHED_PLAN,
    DROP_NOT_OFFERED_BY_PROFESSIONAL,
    DROP_OUT_OF_WINDOW,
    DROP_DAY_UNAVAILABLE,
    DROP_NO_FREE_SLOT,
    DROP_MISSING_DAY,
)

# Why the landing is not a step of the booking (handback_log.py FALLBACK_*). The menu is
# reserved for the spec's three cases: no bookable catalog, an unreachable agenda (which
# becomes today's hand-off to a human) and a corrupt payload (the worker's own fallback).
FALLBACK_NO_BOOKABLE_CATALOG = "no_bookable_catalog"
FALLBACK_CALENDAR_UNAVAILABLE = "calendar_unavailable"
FALLBACK_PROFESSIONAL_CONFIG_INCOMPLETE = "professional_config_incomplete"
FALLBACK_NO_FREE_DAYS = "no_free_days"
FALLBACK_REASONS = (
    FALLBACK_NO_BOOKABLE_CATALOG,
    FALLBACK_CALENDAR_UNAVAILABLE,
    FALLBACK_PROFESSIONAL_CONFIG_INCOMPLETE,
    FALLBACK_NO_FREE_DAYS,
)

# The agenda for the professional the resolver settled on (None = the tenant-level agenda,
# which on a single-professional clinic already carries that professional's credentials).
# Called at most once, and only when the landing needs a calendar.
CalendarSource = Callable[[Any | None], Awaitable["CalendarService | None"]]


@dataclass
class DraftResolution:
    """Where a draft landed and what happened to each supplied item on the way."""

    result: FlowRouterResult
    landing_step: str
    accepted: tuple[str, ...] = ()
    dropped: dict[str, str] = field(default_factory=dict)
    fallback: str | None = None


def landing_step(result: FlowRouterResult) -> str:
    """The log name of where `result` leaves the patient.

    Same rule as workers/shared/handback_log.py::landing_of (that module is the worker
    side and services cannot import it): a calendar outage is "human_handover", an
    unbookable doctor is "config_incomplete", anything else is the flow step or, without
    one, the lower-cased flow state.
    """
    if result.action == "calendar_unavailable":
        return "human_handover"
    if result.action == "professional_config_incomplete":
        return "config_incomplete"
    return result.flow_step or result.flow_state.value.lower()


def _result_fallback(result: FlowRouterResult) -> str | None:
    if result.action == "calendar_unavailable":
        return FALLBACK_CALENDAR_UNAVAILABLE
    if result.action == "professional_config_incomplete":
        return FALLBACK_PROFESSIONAL_CONFIG_INCOMPLETE
    if result.flow_state is FlowState.MENU:
        # The only menu a booking builder returns past the catalog check: the day picker
        # found no free day at all in the window.
        return FALLBACK_NO_FREE_DAYS
    return None


def _recorded_service(conversation: Any) -> str | None:
    """The service the conversation already recorded, never the pra-quem entry marker."""
    value = getattr(conversation, "flow_selected_type", None)
    if not value or value in (fr.ATTENDEE_NEXT_BOOK, fr.ATTENDEE_NEXT_CATALOG):
        return None
    return str(value)


def _insurance_view(tenant: Any, tenant_insurance: Any | None) -> Any:
    """What `match_insurance_plan` reads: the FRESH convênio catalog when one is given."""
    if tenant_insurance is None:
        return tenant
    return SimpleNamespace(
        insurance_plans=list(tenant_insurance.plans),
        insurances=getattr(tenant, "insurances", None),
    )


@dataclass
class _Checked:
    """Every draft item after the catalog-level checks (no calendar read yet)."""

    multi: bool
    clinic_catalog: list[dict]
    professional: Any | None
    service: dict | None
    insurance: str | None
    attendee_name: str | None  # None = pra-quem still unknown (or "other" without a name)
    needs_attendee_name: bool
    dropped: dict[str, str]


def _check(
    draft: BookingDraft,
    *,
    conversation: Any,
    tenant: Any,
    professionals: list,
    service_catalog: list | None,
    tenant_insurance: Any | None,
) -> _Checked:
    dropped: dict[str, str] = {}
    multi = fr._is_multi_professional(professionals)
    clinic_catalog = (
        fr._clinic_service_catalog(tenant, professionals, service_catalog)
        if multi
        else active_appointment_types(tenant)
    )

    def own(professional: Any) -> list[dict]:
        return professional_appointment_types(professional, tenant, service_catalog)

    # Profissional: on a multi-doctor clinic, the draft's doctor (or the recorded one);
    # otherwise implicit - the sole professional, or nobody on a tenant without any.
    if multi:
        if draft.professional_id is not None:
            professional = fr._find_professional_by_id(professionals, draft.professional_id)
            if professional is None:
                dropped[FIELD_PROFESSIONAL] = DROP_UNKNOWN_PROFESSIONAL
        else:
            professional = fr._find_professional_by_id(
                professionals, fr._selected_professional_id(conversation)
            )
            if (
                professional is not None
                and draft.service is not None
                and fr._match_service(own(professional), draft.service) is None
            ):
                # The newer statement wins: the doctor recorded earlier does not offer what
                # the patient asks for now, so the service picks the doctor below.
                professional = None
    else:
        professional = sole_active_professional(professionals)
        if draft.professional_id is not None and (
            getattr(professional, "id", None) != draft.professional_id
        ):
            dropped[FIELD_PROFESSIONAL] = DROP_UNKNOWN_PROFESSIONAL

    # Serviço: canonical in the catalog the booking will use. A recorded service that no
    # longer resolves is silently ignored - only a SUPPLIED item can be "dropped".
    from_draft = draft.service is not None
    text = draft.service if from_draft else _recorded_service(conversation)
    service: dict | None = None
    if text is not None:
        if multi and professional is not None:
            service = fr._match_service(own(professional), text)
            if service is None and from_draft:
                dropped[FIELD_SERVICE] = (
                    DROP_NOT_OFFERED_BY_PROFESSIONAL
                    if fr._match_service(clinic_catalog, text) is not None
                    else DROP_NOT_IN_CATALOG
                )
        else:
            known = fr._match_service(clinic_catalog, text)
            if known is None:
                if from_draft:
                    dropped[FIELD_SERVICE] = DROP_NOT_IN_CATALOG
            elif multi:
                offering = fr._professionals_offering(
                    tenant, professionals, str(known.get("name", "")), service_catalog
                )
                if len(offering) == 1:
                    professional = offering[0]
                    service = fr._match_service(own(professional), text)
                else:
                    service = known  # two or more offer it: the doctor question comes first
            else:
                service = known

    # Convênio: the draft's text only counts when it names a real plan; a recorded answer
    # (a typed "Outro convênio" included) is the patient's own and is kept verbatim.
    if draft.insurance is not None:
        insurance = fr.match_insurance_plan(_insurance_view(tenant, tenant_insurance), draft.insurance)
        if insurance is None:
            dropped[FIELD_INSURANCE] = DROP_UNMATCHED_PLAN
    else:
        insurance = fr._selected_insurance(conversation)

    # Pra quem: "self" wins over a recorded third party; "other" needs an authorized name.
    recorded = fr._attendee_name(conversation)
    needs_attendee_name = False
    if draft.attendee == DRAFT_ATTENDEE_SELF:
        attendee_name: str | None = ATTENDEE_SELF
    elif draft.attendee == DRAFT_ATTENDEE_OTHER:
        attendee_name = real_attendee_name(recorded)
        needs_attendee_name = attendee_name is None
    else:
        attendee_name = recorded

    if draft.time is not None and draft.day is None:
        dropped[FIELD_TIME] = DROP_MISSING_DAY

    return _Checked(
        multi=multi,
        clinic_catalog=clinic_catalog,
        professional=professional,
        service=service,
        insurance=insurance,
        attendee_name=attendee_name,
        needs_attendee_name=needs_attendee_name,
        dropped=dropped,
    )


def _pending(draft: BookingDraft, checked: _Checked) -> BookingDraft:
    """What waits in `flow_draft` for the pra-quem answer: only the items that survived."""
    return BookingDraft(
        service=str(checked.service.get("name")) if checked.service else None,
        professional_id=(
            checked.professional.id if checked.multi and checked.professional is not None else None
        ),
        insurance=checked.insurance if draft.insurance is not None else None,
        attendee=draft.attendee,
        day=draft.day,
        time=draft.time if draft.day is not None else None,
    )


def _carry(result: FlowRouterResult, state: Any) -> FlowRouterResult:
    """Name every validated answer on `result`: `_apply_flow_result` clears unnamed fields.

    Only results that stay in the booking carry them; the attendee steps carry the
    convênio alone (the rest waits in `flow_draft`), and a dead end (menu, alert) carries
    nothing, so its booking ends there.
    """
    if result.flow_state is not FlowState.SERVICE_CATALOG:
        return result
    if result.flow_selected_insurance is None and result.flow_step != fr.STEP_AWAITING_INSURANCE:
        result.flow_selected_insurance = state.flow_selected_insurance
    if result.flow_step in fr.ATTENDEE_STEPS:
        return result
    if result.flow_selected_type is None:
        result.flow_selected_type = state.flow_selected_type
    if result.flow_selected_professional_id is None:
        result.flow_selected_professional_id = state.flow_selected_professional_id
    if result.flow_attendee_name is None:
        result.flow_attendee_name = state.flow_attendee_name
    return result


def _accepted(draft: BookingDraft, dropped: dict[str, str], names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(name for name in draft.supplied_fields() if name in names and name not in dropped)


def _booking_services(checked: _Checked, tenant: Any, service_catalog: list | None) -> list[dict]:
    if checked.multi and checked.professional is not None:
        return professional_appointment_types(checked.professional, tenant, service_catalog)
    return checked.clinic_catalog


async def resolve_booking_draft(
    draft: BookingDraft,
    *,
    conversation: Any,
    tenant: Any,
    professional_rows: list,
    service_catalog: list | None,
    tenant_insurance: Any | None,
    calendar: CalendarSource,
    now: dt.datetime,
) -> DraftResolution:
    """Walk the booking in the workflow's own order and stop at the first open question.

    pra quem -> convênio -> profissional -> serviço -> dia -> horário (spec §4.2). An
    invalid item is dropped ALONE (a reason code in `dropped`) and the workflow asks from
    that step; the menu is only for a clinic with nothing bookable. Pure: reads nothing
    from the database - the caller passes FRESH data (workers/shared/draft_resolution.py).

    `conversation` is a snapshot with `id`, `tenant_id` and the `flow_*` fields; `tenant`
    is the flow tenant snapshot (`_flow_tenant_snapshot`); `professional_rows` is the
    router-shaped ACTIVE roster (`_flow_professionals`); `now` must be timezone-aware.

    Runs inside a booking-gate scope so held slots count as busy: the caller's gate when
    one is published (`flow_router.booking_gate_scope`), else an unarmed gate for this
    conversation - a direct call never sees a reserved slot as free.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    gate = fr._ACTIVE_GATE.get()
    if gate is None:
        gate = BookingGate(
            tenant_id=getattr(conversation, "tenant_id", None),
            conversation_id=conversation.id,
            patient_id=None,
            external_id=None,
            armed=False,
        )
    with fr.booking_gate_scope(gate):
        return await _resolve(
            draft,
            conversation=conversation,
            tenant=tenant,
            professionals=list(professional_rows or []),
            service_catalog=service_catalog,
            tenant_insurance=tenant_insurance,
            calendar=calendar,
            now=now,
        )


async def _resolve(
    draft: BookingDraft,
    *,
    conversation: Any,
    tenant: Any,
    professionals: list,
    service_catalog: list | None,
    tenant_insurance: Any | None,
    calendar: CalendarSource,
    now: dt.datetime,
) -> DraftResolution:
    checked = _check(
        draft,
        conversation=conversation,
        tenant=tenant,
        professionals=professionals,
        service_catalog=service_catalog,
        tenant_insurance=tenant_insurance,
    )
    state = fr._DayPickerState(
        id=getattr(conversation, "id", None),
        flow_selected_type=str(checked.service.get("name", "")) if checked.service else None,
        flow_selected_professional_id=(
            checked.professional.id if checked.multi and checked.professional is not None else None
        ),
        flow_selected_insurance=checked.insurance,
        flow_attendee_name=checked.attendee_name,
    )

    def done(result: FlowRouterResult, *names: str, fallback: str | None = None) -> DraftResolution:
        result = _carry(result, state)
        return DraftResolution(
            result=result,
            landing_step=landing_step(result),
            accepted=_accepted(draft, checked.dropped, names),
            dropped=dict(checked.dropped),
            fallback=fallback or _result_fallback(result),
        )

    # A chosen doctor nobody can book (FEAT 41): the clinic hears about it, before any
    # question the patient could only answer into a dead end.
    if checked.multi and checked.professional is not None and not professional_appointment_types(
        checked.professional, tenant, service_catalog
    ):
        return done(fr._enter_professional_services(checked.professional, tenant), FIELD_PROFESSIONAL)
    # Nothing bookable at all: the plain menu, as every hand-back did before.
    if not checked.clinic_catalog:
        menu = FlowRouterResult(
            action="reply",
            bubbles=fr._menu_bubbles(tenant, professionals),
            flow_state=FlowState.MENU,
        )
        return done(menu, fallback=FALLBACK_NO_BOOKABLE_CATALOG)
    # 1. Pra quem - asked, never assumed. The rest waits in flow_draft.
    if checked.attendee_name is None:
        ask = fr._attendee_name_request if checked.needs_attendee_name else fr._attendee_question
        result = ask(fr.ATTENDEE_NEXT_BOOK)
        result.flow_draft = draft_record(_pending(draft, checked), saved_at=now)
        return done(result, *FIELD_NAMES)
    # 2. Convênio, when the clinic collects it.
    if fr._insurance_step_skip_reason(tenant) is None and checked.insurance is None:
        return done(
            fr._enter_insurance(tenant, state), FIELD_FOR_WHOM, FIELD_PROFESSIONAL, FIELD_SERVICE
        )
    # 3. Profissional (multi-doctor clinics; implicit otherwise).
    if checked.multi and checked.professional is None:
        result = fr._enter_professional_list(tenant, professionals, insurance=checked.insurance)
        return done(result, FIELD_FOR_WHOM, FIELD_INSURANCE, FIELD_SERVICE)
    # 4. Serviço.
    if checked.service is None:
        result = (
            fr._enter_professional_services(checked.professional, tenant)
            if checked.multi
            else fr._start_booking(tenant, professionals, insurance=checked.insurance)
        )
        return done(result, FIELD_FOR_WHOM, FIELD_INSURANCE, FIELD_PROFESSIONAL)
    # 5-6. Dia e horário.
    return await _land_day(
        draft,
        checked,
        state,
        tenant=tenant,
        professionals=professionals,
        service_catalog=service_catalog,
        calendar=calendar,
        now=now,
        done=done,
    )


async def _land_day(
    draft: BookingDraft,
    checked: _Checked,
    state: Any,
    *,
    tenant: Any,
    professionals: list,
    service_catalog: list | None,
    calendar: CalendarSource,
    now: dt.datetime,
    done: Callable[..., DraftResolution],
) -> DraftResolution:
    """The day picker. (Task A5 replaces this with the day and time validation.)"""
    services = _booking_services(checked, tenant, service_catalog)
    cal = await calendar(checked.professional if checked.multi else None)
    result = await fr._ask_day(state, tenant, cal, services, professionals)
    return done(result, FIELD_FOR_WHOM, FIELD_INSURANCE, FIELD_PROFESSIONAL, FIELD_SERVICE)
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_booking_draft_resolver.py tests/test_booking_draft.py tests/test_flow_router.py tests/test_flow_router_insurance.py tests/test_convenio_catalogo_flow.py tests/test_flow_router_multiprofessional.py tests/test_insurance_mode_matrix.py -q`
Expected: PASS (os 24 novos e os antigos de convênio/multi-médico).

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff format src/secretaria/services/booking_draft.py tests/test_booking_draft_resolver.py
uvx ruff check src/secretaria/services/booking_draft.py src/secretaria/services/flow_router.py tests/test_booking_draft_resolver.py
uvx ruff format --diff src/secretaria/services/flow_router.py | grep -c '^@@'   # must not increase
git add src/secretaria/services/booking_draft.py src/secretaria/services/flow_router.py tests/test_booking_draft_resolver.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(draft): the resolver lands an AI draft at the first open step

Pra-quem, convênio, doctor and service are checked in the workflow's own order against
the fresh roster and catalogs; an invalid item is dropped alone with a reason code and
the workflow asks from there. Unknown pra-quem is always asked, the rest parked in
flow_draft. Runs inside a booking-gate scope (booking_gate_scope, now shared with route).

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task A5: O resolvedor — dia, horário, reservas e o gancho da confirmação expressa

**Files:**
- Modify: `src/secretaria/services/booking_draft.py` (`_land_day` completo; `_express_confirmation`; frases de aviso)
- Modify: `src/secretaria/services/flow_router.py` (`_ask_day` ganha `prefix`)
- Modify: `tests/test_booking_draft_resolver.py`

**Interfaces:**
- Consumes: A1 (`free_slots_for_day`, `_hold_owner`), A4 (todo o resolvedor), flow_router (`_booking_professional`, `_booking_duration`, `_calendar_unavailable`, `_enter_slot_picker`, `_professional_config_incomplete`, `PROFESSIONAL_GAP_HOURS`, `DAY_PICKER_WINDOW_DAYS`, `BOOKING_DAY_BRANCH`, `BACK_TARGET_SERVICE`).
- Produces: `DRAFT_DAY_OUT_OF_WINDOW_PREFIX`, `DRAFT_DAY_UNAVAILABLE_PREFIX`; `async def _express_confirmation(*, state, tenant, professional, service, slot_start, duration_minutes, calendar, professionals) -> FlowRouterResult | None` (P2: sempre `None`); `fr._ask_day(..., *, prefix: str | None = None)`.

- [x] **Step 1: Write the failing tests**

Ao fim de `tests/test_booking_draft_resolver.py`, acrescentar:

```python
# --------------------------------------------------------------------------
# Dia e horário (Task A5)
# --------------------------------------------------------------------------


async def test_a_valid_day_and_time_lands_on_that_days_slot_list(monkeypatch):
    seen: list = []

    async def _hook(**kwargs):
        seen.append(kwargs)
        return None

    monkeypatch.setattr(bd, "_express_confirmation", _hook)
    res = await _resolve(BookingDraft(service="Consulta", day=DAY, time=dt.time(10, 0)))
    assert res.landing_step == fr.STEP_AWAITING_SLOT
    assert res.result.flow_selected_day == "2026-10-08"
    assert res.result.flow_selected_type == "Consulta"
    assert res.accepted == ("service", "day", "time")
    # P3's hook was offered the RE-DERIVED slot: the agenda's own 10:00, aware.
    (call,) = seen
    assert call["slot_start"] == dt.datetime(2026, 10, 8, 10, 0, tzinfo=TZ)
    assert call["service"]["name"] == "Consulta"


async def test_the_express_hook_result_wins_and_keeps_every_answer(monkeypatch):
    async def _hook(**kwargs):
        return fr.FlowRouterResult(
            action="reply",
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=fr.STEP_AWAITING_CONFIRMATION,
            flow_selected_day="2026-10-08",
            flow_selected_slot="2026-10-08T10:00",
        )

    monkeypatch.setattr(bd, "_express_confirmation", _hook)
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY, time=dt.time(10, 0)),
        conv=_conv(flow_attendee_name="Maria da Silva"),
    )
    assert res.landing_step == fr.STEP_AWAITING_CONFIRMATION
    assert res.result.flow_selected_type == "Consulta"
    assert res.result.flow_attendee_name == "Maria da Silva"


async def test_p2_has_no_express_confirmation_yet():
    assert await bd._express_confirmation(
        state=None, tenant=None, professional=None, service={}, slot_start=NOW,
        duration_minutes=30, calendar=None, professionals=[],
    ) is None


async def test_a_time_outside_business_hours_lands_on_that_days_slot_list():
    res = await _resolve(BookingDraft(service="Consulta", day=DAY, time=dt.time(18, 0)))
    assert res.landing_step == fr.STEP_AWAITING_SLOT
    assert res.result.flow_selected_day == "2026-10-08"
    assert res.dropped == {"time": "no_free_slot"}
    assert res.accepted == ("service", "day")


async def test_a_day_in_the_past_is_dropped():
    res = await _resolve(BookingDraft(service="Consulta", day=dt.date(2026, 10, 4), time=dt.time(10, 0)))
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.dropped == {"day": "out_of_window", "time": "missing_day"}
    assert res.result.bubbles[0].body.startswith(bd.DRAFT_DAY_OUT_OF_WINDOW_PREFIX)


async def test_a_day_beyond_the_window_is_dropped():
    beyond = NOW.date() + dt.timedelta(days=fr.DAY_PICKER_WINDOW_DAYS)
    res = await _resolve(BookingDraft(service="Consulta", day=beyond))
    assert res.dropped == {"day": "out_of_window"}


async def test_the_last_day_of_the_window_is_accepted():
    last = NOW.date() + dt.timedelta(days=fr.DAY_PICKER_WINDOW_DAYS - 1)
    res = await _resolve(BookingDraft(service="Consulta", day=last), cal=_Cal({last: ["09:00"]}))
    assert res.landing_step == fr.STEP_AWAITING_SLOT
    assert res.result.flow_selected_day == last.isoformat()


async def test_a_day_without_free_time_is_dropped():
    # Another day IS free, so the picker lists it (an agenda with no free day at all is
    # the picker's own "não encontrei horários" + menu, `no_free_days`).
    cal = _Cal({DAY: [], dt.date(2026, 10, 9): ["09:00"]})
    res = await _resolve(BookingDraft(service="Consulta", day=DAY), cal=cal)
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.dropped == {"day": "day_unavailable"}
    assert res.result.bubbles[0].body.startswith(bd.DRAFT_DAY_UNAVAILABLE_PREFIX)


async def test_a_time_without_a_day_is_ignored():
    res = await _resolve(BookingDraft(service="Consulta", time=dt.time(10, 0)))
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.dropped == {"time": "missing_day"}


class _HoldGate:
    armed = False

    def __init__(self, windows):
        self._windows = windows
        self.asked: list = []

    async def busy_windows(self, professional_id):
        self.asked.append(professional_id)
        return list(self._windows.get(professional_id, []))


_HELD_1000 = (dt.datetime(2026, 10, 8, 10, 0, tzinfo=TZ), dt.datetime(2026, 10, 8, 10, 30, tzinfo=TZ))


async def test_a_held_slot_is_busy_inside_the_callers_gate_scope():
    with fr.booking_gate_scope(_HoldGate({None: [_HELD_1000]})):
        res = await _resolve(BookingDraft(service="Consulta", day=DAY, time=dt.time(10, 0)))
    assert res.dropped == {"time": "no_free_slot"}


async def test_a_direct_call_without_a_gate_still_sees_holds(monkeypatch):
    from secretaria.services import booking_hold

    async def _held_windows(tenant_id, professional_id, *, exclude_conversation=None):
        return [_HELD_1000] if professional_id is None else []

    monkeypatch.setattr(booking_hold, "held_windows", _held_windows)
    assert fr._ACTIVE_GATE.get() is None
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY, time=dt.time(10, 0)),
        conv=_conv(tenant_id=uuid4()),
    )
    assert res.dropped == {"time": "no_free_slot"}
    assert fr._ACTIVE_GATE.get() is None  # the default gate is taken down again


async def test_a_sole_professionals_holds_count():
    sole = _sole()
    gate = _HoldGate({sole.id: [_HELD_1000]})
    with fr.booking_gate_scope(gate):
        res = await _resolve(
            BookingDraft(service="Consulta", day=DAY, time=dt.time(10, 0)), pros=[sole]
        )
    assert res.dropped == {"time": "no_free_slot"}
    # Asked by the resolver AND by the slot list it lands on - always under the sole id.
    assert set(gate.asked) == {sole.id}


async def test_an_unreachable_agenda_hands_over():
    res = await _resolve(BookingDraft(service="Consulta", day=DAY), cal=_Cal(unavailable=True))
    assert res.result.action == "calendar_unavailable"
    assert (res.landing_step, res.fallback) == ("human_handover", "calendar_unavailable")
    assert res.result.flow_selected_type == "Consulta"


async def test_no_agenda_at_all_hands_over():
    async def _none(_professional):
        return None

    res = await resolve_booking_draft(
        BookingDraft(service="Consulta", day=DAY),
        conversation=_conv(),
        tenant=_tenant(),
        professional_rows=[],
        service_catalog=[],
        tenant_insurance=None,
        calendar=_none,
        now=NOW,
    )
    assert res.fallback == "calendar_unavailable"


async def test_a_doctor_without_hours_reaches_the_clinic_alert():
    ana = _doctor("Dra. Ana", ["Consulta"], hours={})
    beto = _doctor("Dr. Beto", ["Consulta"])
    res = await _resolve(BookingDraft(service="Consulta", professional_id=ana.id, day=DAY), pros=[ana, beto])
    assert res.result.action == "professional_config_incomplete"
    assert res.result.professional_config_gap == "hours"


async def test_ask_day_without_a_prefix_is_unchanged():
    res = await fr._ask_day(_conv(), _tenant(), _Cal(), None, None)
    assert res.bubbles[0].body == fr.DAY_PICKER_BODY
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_booking_draft_resolver.py -q`
Expected: os novos FAIL (`AttributeError: module 'secretaria.services.booking_draft' has no attribute '_express_confirmation'` / landing `awaiting_day` onde se espera `awaiting_slot`); os da Task A4 continuam PASS.

- [x] **Step 3: `_ask_day` gains an optional prefix**

Em `src/secretaria/services/flow_router.py`, trocar a assinatura de `_ask_day`

```python
async def _ask_day(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    services: list[dict] | None = None,
    professionals: list | None = None,
) -> FlowRouterResult:
```

por

```python
async def _ask_day(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    services: list[dict] | None = None,
    professionals: list | None = None,
    *,
    prefix: str | None = None,
) -> FlowRouterResult:
```

e, no `return await enter_day_picker(...)` do fim da função, acrescentar o argumento `prefix=prefix,` depois de `professionals=professionals,`. Acrescentar ao fim da docstring: `` `prefix` (TASK-030: why the AI's day was not kept) prepends one short line; None renders exactly as before.``

- [x] **Step 4: Day, time, holds and the P3 hook**

Em `src/secretaria/services/booking_draft.py`:

1. Acrescentar aos imports de primeira parte (ordem do ruff):

```python
from secretaria.services.availability import free_slots_for_day
```

logo antes de `from secretaria.services.booking_hold import BookingGate`;

```python
from secretaria.services.calendar import CalendarUnavailableError
```

logo depois de `from secretaria.services.booking_scope import sole_active_professional`; e trocar o import de `tenant_config` por

```python
from secretaria.services.tenant_config import (
    active_appointment_types,
    professional_appointment_types,
    professional_business_hours,
)
```

(o `if TYPE_CHECKING: from secretaria.services.calendar import CalendarService` continua.)

2. Logo depois de `FALLBACK_REASONS = (...)`, inserir:

```python
# The one line the day picker opens with when the AI's day could not be kept, so the
# patient who said "quinta" knows why they are being asked again.
DRAFT_DAY_OUT_OF_WINDOW_PREFIX = "Não temos agenda aberta para esse dia."
DRAFT_DAY_UNAVAILABLE_PREFIX = "Esse dia não tem horário livre."
```

3. Trocar a função `_land_day` inteira por:

```python
async def _land_day(
    draft: BookingDraft,
    checked: _Checked,
    state: Any,
    *,
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
    A valid time is offered to `_express_confirmation` (P3); without it - in P2, always -
    the patient lands on that day's slot list. An invalid day lands on the day picker,
    an invalid time on the slot list of its day.
    """
    dropped = checked.dropped
    base = (FIELD_FOR_WHOM, FIELD_INSURANCE, FIELD_PROFESSIONAL, FIELD_SERVICE)
    services = _booking_services(checked, tenant, service_catalog)

    # The static "this doctor has no hours at all" check, before any calendar call - the
    # same one `_ask_day` makes for the button flow.
    owner = fr._booking_professional(state, professionals)
    if owner is not None and not professional_business_hours(owner, tenant):
        return done(fr._professional_config_incomplete(owner, fr.PROFESSIONAL_GAP_HOURS), *base)

    cal = await calendar(checked.professional if checked.multi else None)

    async def day_picker(prefix: str | None = None) -> DraftResolution:
        result = await fr._ask_day(state, tenant, cal, services, professionals, prefix=prefix)
        return done(result, *base)

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
    return done(result, *names)


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
) -> FlowRouterResult | None:
    """P3 HOOK - straight to the confirmation card when every item is valid. P2: None.

    Called only with pra-quem answered (`state.flow_attendee_name` is ATTENDEE_SELF or an
    authorized name), the service known, and `slot_start` re-derived from the agenda's
    free slots minus holds (aware, clinic timezone). `state` is the resolver's
    `_DayPickerState` (id, flow_selected_type, flow_selected_professional_id,
    flow_selected_insurance, flow_attendee_name); `professional` is the booking owner
    (`_booking_professional`), None on a tenant without professionals.

    A non-None result must be a SERVICE_CATALOG result at STEP_AWAITING_CONFIRMATION that
    names flow_selected_day and flow_selected_slot (naive ISO minutes, like `_handle_slot`);
    the resolver's `_carry` fills type, professional, convênio and attendee.
    """
    return None
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_booking_draft_resolver.py tests/test_flow_day_picker.py tests/test_flow_router.py tests/test_availability.py -q`
Expected: PASS (todos os do resolvedor, 40 com os parametrizados, e os seletores inalterados).

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff format src/secretaria/services/booking_draft.py tests/test_booking_draft_resolver.py
uvx ruff check src/secretaria/services/booking_draft.py src/secretaria/services/flow_router.py tests/test_booking_draft_resolver.py
uvx ruff format --diff src/secretaria/services/flow_router.py | grep -c '^@@'   # must not increase
git add src/secretaria/services/booking_draft.py src/secretaria/services/flow_router.py tests/test_booking_draft_resolver.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(draft): day and time are re-derived from the fresh agenda minus holds

The draft's day must sit in the booking window and have a free slot (holds subtracted,
even on a direct call: an unarmed default gate); its time must be one of that day's free
slots. Invalid day -> day picker with the reason line; invalid time -> that day's slot
list. A valid time is offered to _express_confirmation, P3's hook, which returns None in
P2 so the patient lands on the slot list.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

### Task A6: No worker — leituras frescas, o portão do turno e a continuação depois do "pra quem"

**Files:**
- Create: `src/secretaria/workers/shared/draft_resolution.py`
- Modify: `src/secretaria/workers/shared/greeting.py` (novo `_flow_professionals`)
- Modify: `src/secretaria/workers/orchestrator.py` (usa `_flow_professionals`)
- Modify: `src/secretaria/workers/shared/flow_runner.py` (portão via `_turn_booking_gate`; continuação)
- Modify: `src/secretaria/services/flow_router.py` (`_attendee_step` marca `resume_draft`)
- Test: `tests/test_booking_draft_continuation.py`

**Interfaces:**
- Consumes: A2 (`draft_from_record`, `DRAFT_ATTENDEE_*`), A3 (`flow_draft`, `resume_draft`), A4/A5 (`resolve_booking_draft`, `DraftResolution`), flow_router (`booking_gate_scope`), `llm_context._appointment_calendar`, `greeting._flow_tenant_snapshot`.
- Produces: `DraftContext`, `_load_draft_context`, `_turn_booking_gate`, `_draft_calendar_source`, `_resolve_draft`, `_resume_booking_draft` (assinaturas em "Interfaces"); `greeting._flow_professionals(professional_rows, services)`; eventos `booking_draft_resumed` (`conversation_id`, `tenant_id`, `landing_step`, `accepted`, `dropped`, `fallback`), `booking_draft_resume_skipped` (`reason`: `missing` | `expired_or_invalid`), `booking_draft_resume_failed` (`error_type`).

- [x] **Step 1: Write the failing tests**

Criar `tests/test_booking_draft_continuation.py`:

```python
"""The pra-quem answer resumes the AI draft parked in flow_draft (TASK-030 P2, spec §4.3)."""

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
from datetime import UTC, datetime, timedelta  # noqa: E402
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

from secretaria.config import Settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import ConsentEvent, Conversation, FlowState, Tenant  # noqa: E402
from secretaria.services import booking_hold as booking_hold_service  # noqa: E402
from secretaria.services.attendee import (  # noqa: E402
    ATTENDEE_SELF,
    CONSENT_KIND_THIRD_PARTY_BOOKING,
    LABEL_ATTENDEE_AUTH_CONFIRM,
    LABEL_ATTENDEE_SELF,
)
from secretaria.services.booking_draft import BookingDraft, draft_record  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    ATTENDEE_NEXT_BOOK,
    STEP_AWAITING_ATTENDEE_AUTH,
    STEP_AWAITING_ATTENDEE_CHOICE,
    STEP_AWAITING_SERVICE,
    STEP_AWAITING_SLOT,
)
from secretaria.services.greeting_template import CONSENT_BUTTON_LABEL  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import draft_resolution  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511988887777"
# Inside the 20-day window whatever the hour the suite runs at.
DAY = (datetime.now(TZ) + timedelta(days=3)).date()


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
    """The clinic's agenda: free times per day."""

    def __init__(self, free):
        self.tzinfo = TZ
        self._free = free

    async def list_available_days(self, start_day, days, slot_minutes=None):
        return [datetime(d.year, d.month, d.day, tzinfo=TZ) for d in sorted(self._free)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        times = self._free.get(day.date(), [])[:max_slots]
        return [{"start": f"{day.date().isoformat()}T{t}", "end": "", "label": t} for t in times]


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, db):
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
            tenant_id=str(tenant_id), status="active", active=True, secretaria_enabled=True,
            plan="bronze", secretaria_tier="basico", addons={}, limits={},
        )

    async def _agenda_for(session, tenant, target):
        return _Agenda({DAY: ["10:00", "10:40"]})

    monkeypatch.setattr(workers_ns, "resolve_patient_opening_state", _fake_resolve)
    monkeypatch.setattr(workers_ns, "get_waba_token", _fake_token)
    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_entitlements)
    monkeypatch.setattr(workers_ns, "_appointment_calendar", _agenda_for)
    return db


async def _seed_tenant(db) -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=PHONE_NUMBER_ID,
            is_active=True,
            clinic_description="Oftalmologia.",
            initial_flows={},
            appointment_types=[{"name": "Primeira Consulta", "duration_min": 40, "is_active": True}],
        )
        session.add(tenant)
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


async def _consents(db, tenant) -> int:
    async with db() as session:
        return await session.scalar(
            select(func.count())
            .select_from(ConsentEvent)
            .where(
                ConsentEvent.tenant_id == tenant.id,
                ConsentEvent.kind == CONSENT_KIND_THIRD_PARTY_BOOKING,
            )
        )


async def _park(db, tenant, *, step, attendee, draft, saved_at=None) -> None:
    conversation = await _conversation(db, tenant)
    async with db() as session:
        async with session.begin():
            row = await session.get(Conversation, conversation.id)
            row.flow_state = FlowState.SERVICE_CATALOG
            row.flow_step = step
            row.flow_selected_type = ATTENDEE_NEXT_BOOK
            row.flow_attendee_name = attendee
            row.flow_draft = draft_record(draft, saved_at=saved_at or datetime.now(UTC))


_FULL_OTHER = BookingDraft(
    service="Primeira Consulta", attendee="other", day=DAY, time=dt.time(10, 0)
)


async def test_authorizing_a_third_party_lands_the_parked_draft_on_its_slot_list(wired) -> None:
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    await _park(db, tenant, step=STEP_AWAITING_ATTENDEE_AUTH, attendee="Maria da Silva", draft=_FULL_OTHER)

    await _wa_turn(tenant, LABEL_ATTENDEE_AUTH_CONFIRM)

    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_SLOT
    assert conversation.flow_selected_type == "Primeira Consulta"
    assert conversation.flow_selected_day == DAY.isoformat()
    assert conversation.flow_attendee_name == "Maria da Silva"
    assert conversation.flow_draft is None  # consumed
    assert await _consents(db, tenant) == 1  # the authorization is still recorded, once
    kind, body = _WireClient.sends[-1]
    assert kind == "list"
    assert body.startswith(f"Horários livres em {DAY.strftime('%d/%m')}")


async def test_answering_pra_mim_overrides_a_parked_other(wired) -> None:
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    await _park(db, tenant, step=STEP_AWAITING_ATTENDEE_CHOICE, attendee=None, draft=_FULL_OTHER)

    await _wa_turn(tenant, LABEL_ATTENDEE_SELF)

    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_SLOT  # no name is asked
    assert conversation.flow_attendee_name == ATTENDEE_SELF
    assert await _consents(db, tenant) == 0


async def test_a_draft_older_than_30_minutes_is_not_resumed(wired, monkeypatch) -> None:
    log = _Log()
    monkeypatch.setattr(draft_resolution, "logger", log)
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    await _park(
        db, tenant, step=STEP_AWAITING_ATTENDEE_AUTH, attendee="Maria da Silva",
        draft=_FULL_OTHER, saved_at=datetime.now(UTC) - timedelta(minutes=31),
    )

    await _wa_turn(tenant, LABEL_ATTENDEE_AUTH_CONFIRM)

    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_SERVICE  # the plain next question
    assert conversation.flow_draft is None
    assert await _consents(db, tenant) == 1
    skipped = [f for e, f in log.events if e == "booking_draft_resume_skipped"]
    assert [f["reason"] for f in skipped] == ["expired_or_invalid"]


async def test_a_resolver_failure_falls_back_to_the_plain_next_question(wired, monkeypatch) -> None:
    log = _Log()
    monkeypatch.setattr(draft_resolution, "logger", log)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("resolver bug")

    monkeypatch.setattr(draft_resolution, "resolve_booking_draft", _boom)
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    await _park(db, tenant, step=STEP_AWAITING_ATTENDEE_AUTH, attendee="Maria da Silva", draft=_FULL_OTHER)

    await _wa_turn(tenant, LABEL_ATTENDEE_AUTH_CONFIRM)

    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_SERVICE
    assert conversation.flow_draft is None
    assert [f["error_type"] for e, f in log.events if e == "booking_draft_resume_failed"] == [
        "RuntimeError"
    ]


async def test_a_conversation_of_another_tenant_is_never_loaded(wired) -> None:
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    conversation = await _conversation(db, tenant)
    stranger = Tenant(id=uuid4(), clinic_name="Outra", phone_number_id="999", is_active=True)
    reply = tasks._ReplyContext(conversation_id=conversation.id, patient_ref=WA_ID, inbound_body="x")
    assert await draft_resolution._load_draft_context(reply, stranger) is None
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_booking_draft_continuation.py -q`
Expected: erro de coleta — `ImportError: cannot import name 'draft_resolution' from 'secretaria.workers.shared'`.

- [x] **Step 3: `_flow_professionals` in greeting.py, used by the orchestrator**

Em `src/secretaria/workers/shared/greeting.py`, logo antes de `def _flow_tenant_snapshot(`, inserir:

```python
def _flow_professionals(
    professional_rows: list[Professional], services: list | None
) -> list[SimpleNamespace]:
    """The router-shaped ACTIVE roster: plain snapshots, catalog already resolved.

    What `route()` receives as `professionals`. `appointment_types` resolves through the
    clinic catalog (one spelling per service); `None` is preserved as `None`, never
    flattened to `[]`, because that is the ONLY thing that makes
    `professional_appointment_types` fall back to the tenant's legacy list - a
    professional whose own list is `[]` offers nothing. `business_hours` is verbatim, NULL
    and all: `professional_business_hours` reads the NULL-versus-EMPTY distinction to tell
    "inherits the clinic's hours" from "has none at all".
    """
    return [
        SimpleNamespace(
            id=p.id,
            name=p.name,
            specialty=p.specialty,
            about=p.about,
            context_doctor_message=p.context_doctor_message,
            appointment_types=(
                resolve_entries(p.appointment_types, services)
                if p.appointment_types
                else p.appointment_types
            ),
            business_hours=p.business_hours,
        )
        for p in professional_rows
    ]
```

Em `src/secretaria/workers/orchestrator.py`, trocar o `flow_professionals = [SimpleNamespace(...) for p in professional_rows]` inteiro (do `flow_professionals = [` até o `]` que fecha a lista, comentários internos incluídos — eles foram para a docstring acima) por:

```python
                    flow_professionals = _flow_professionals(professional_rows, service_catalog)
```

e acrescentar `_flow_professionals,` ao import de `secretaria.workers.shared.greeting` do orchestrator (em ordem alfabética dentro do parêntese). Se `SimpleNamespace` deixar de ser usado no orchestrator, o `uvx ruff check` acusa `F401`: remova o import só nesse caso.

- [x] **Step 4: The worker module**

Criar `src/secretaria/workers/shared/draft_resolution.py`:

```python
"""Run the AI draft resolver from the worker: fresh reads, the turn's gate, the right agenda.

`services/booking_draft.py::resolve_booking_draft` decides where an AI booking draft lands
and reads nothing itself. This module is its worker half (TASK-030 P2):

  * `_load_draft_context` re-reads the conversation, the ACTIVE roster and the service and
    convênio catalogs in one short session - never the turn-start snapshot, which can be
    several tool calls old by the time a hand-back runs (spec §4.2);
  * `_turn_booking_gate` is the booking gate for THIS turn - the same one `_run_flow` hands
    to `route()` - so a resolver run outside `route()` still sees held slots as busy;
  * `_draft_calendar_source` builds the agenda lazily, for the professional the resolver
    settled on, and only if it reaches the day step;
  * `_resume_booking_draft` is the continuation: the tap that answers "Essa consulta é pra
    você?" re-runs the resolver over the parked draft (`Conversation.flow_draft`), because
    the world may have changed in the two minutes the patient took to answer.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Conversation, FlowState, Tenant
from secretaria.services.attendee import real_attendee_name
from secretaria.services.booking_draft import (
    DRAFT_ATTENDEE_OTHER,
    DRAFT_ATTENDEE_SELF,
    BookingDraft,
    CalendarSource,
    DraftResolution,
    draft_from_record,
    resolve_booking_draft,
)
from secretaria.services.booking_hold import BookingGate
from secretaria.services.booking_scope import BOOKING_TOPOLOGY_MULTI, booking_topology
from secretaria.services.calendar import CalendarService
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.flow_router import FlowRouterResult, booking_gate_scope
from secretaria.services.insurance_catalog import load_tenant_insurance
from secretaria.services.service_catalog import load_service_catalog
from secretaria.services.tenant_config import list_active_professionals
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.greeting import _flow_professionals, _flow_tenant_snapshot
from secretaria.workers.shared.llm_context import _appointment_calendar

logger = get_logger(__name__)


@dataclass
class DraftContext:
    """Everything the resolver reads, loaded FRESH for one hand-back."""

    conversation: SimpleNamespace
    professional_rows: list  # ORM rows: agendas are built from these
    professionals: list  # router-shaped (catalog resolved): what the resolver reads
    service_catalog: list
    tenant_insurance: Any
    tenant_snapshot: SimpleNamespace
    topology: str


def _conversation_flow_snapshot(conversation: Conversation) -> SimpleNamespace:
    return SimpleNamespace(
        id=conversation.id,
        tenant_id=conversation.tenant_id,
        patient_id=conversation.patient_id,
        flow_state=conversation.flow_state,
        flow_step=conversation.flow_step,
        flow_selected_type=conversation.flow_selected_type,
        flow_selected_day=conversation.flow_selected_day,
        flow_selected_slot=conversation.flow_selected_slot,
        flow_selected_professional_id=conversation.flow_selected_professional_id,
        flow_selected_insurance=conversation.flow_selected_insurance,
        flow_managing_appointment_id=conversation.flow_managing_appointment_id,
        flow_attendee_name=conversation.flow_attendee_name,
        flow_draft=conversation.flow_draft,
    )


async def _load_draft_context(reply: _ReplyContext, tenant: Tenant) -> DraftContext | None:
    """One short session of fresh reads, scoped to `tenant`. None = no such conversation.

    A conversation that belongs to another tenant is refused (count-only warning): every
    roster, catalog and agenda below is read for `tenant`, so mixing the two would book a
    patient against another clinic's data.
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
    return DraftContext(
        conversation=snapshot,
        professional_rows=professional_rows,
        professionals=_flow_professionals(professional_rows, service_catalog),
        service_catalog=service_catalog,
        tenant_insurance=tenant_insurance,
        tenant_snapshot=_flow_tenant_snapshot(
            tenant, professional_rows, service_catalog, tenant_insurance
        ),
        topology=booking_topology(professional_rows),
    )


def _turn_booking_gate(reply: _ReplyContext, tenant: Tenant | None) -> BookingGate:
    """The booking gate for THIS turn. Armed only on Brain-Message.

    Only then does "Confirmar" become a reservation plus a mailed code instead of an
    appointment (services/booking_hold.py). Built unarmed on WhatsApp rather than left as
    None because the hold LOOKUPS must still happen there: a slot a Portal visitor is
    holding has to be invisible to a WhatsApp patient too, or the reservation only half
    exists.

    `tenant` (already loaded by the caller), NOT `reply.tenant_id`: `_ReplyContext.tenant_id`
    is populated only on the identity legs and the degrade paths, and the ORDINARY turn -
    the one that books - leaves it None. Reading it disarmed the gate on exactly the path
    it exists for, SILENTLY (proved in production on 2026-09-21, pinned by
    tests/test_booking_code_gate.py::test_the_gate_arms_on_the_real_reply_path).
    """
    return BookingGate(
        tenant_id=tenant.id if tenant is not None else reply.tenant_id,
        conversation_id=reply.conversation_id,
        patient_id=None,
        external_id=reply.patient_ref,
        armed=reply.channel == CHANNEL_BRAIN_MESSAGE,
    )


def _draft_calendar_source(tenant: Tenant, ctx: DraftContext) -> CalendarSource:
    """The resolver's agenda, built only when it reaches the day step.

    A professional on a multi-professional clinic -> THAT professional's own agenda;
    otherwise the tenant-level one (on a single-professional clinic `load_tenant_config`
    has already resolved that professional's credentials into it). A professional who no
    longer resolves -> None, which makes the picker answer `calendar_unavailable` instead
    of listing days off whichever agenda happened to be at hand.
    """

    async def _calendar_for(professional: Any | None) -> CalendarService | None:
        target: Any = "tenant"
        if professional is not None and ctx.topology == BOOKING_TOPOLOGY_MULTI:
            target = next(
                (row for row in ctx.professional_rows if row.id == professional.id), None
            )
        async with async_session_factory() as session:
            return await _appointment_calendar(session, tenant, target)

    return _calendar_for


async def _resolve_draft(
    reply: _ReplyContext,
    tenant: Tenant,
    draft: BookingDraft,
    ctx: DraftContext,
    *,
    gate: BookingGate | None = None,
) -> DraftResolution:
    """`resolve_booking_draft` over `ctx`, inside this turn's booking gate."""
    with booking_gate_scope(gate if gate is not None else _turn_booking_gate(reply, tenant)):
        return await resolve_booking_draft(
            draft,
            conversation=ctx.conversation,
            tenant=ctx.tenant_snapshot,
            professional_rows=ctx.professionals,
            service_catalog=ctx.service_catalog,
            tenant_insurance=ctx.tenant_insurance,
            calendar=_draft_calendar_source(tenant, ctx),
            now=datetime.now(UTC),
        )


async def _resume_booking_draft(
    reply: _ReplyContext,
    tenant: Tenant,
    result: FlowRouterResult,
    *,
    gate: BookingGate | None = None,
) -> FlowRouterResult:
    """Pra-quem was just answered: land the parked AI draft instead of the next list.

    `result` is what `route()` computed for the answer (the plain button continuation);
    it is the fallback for a missing, expired or corrupt draft and for ANY resolver
    failure - the patient always gets the next question, never nothing. The patient's
    answer wins over the draft's `w` ("Sim, é pra mim" after the AI said "other" means
    the patient). The authorization of a third party stays recorded exactly as the plain
    path would have recorded it.
    """
    plain = replace(result, flow_draft=None, resume_draft=False)
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
        answered_other = real_attendee_name(result.flow_attendee_name) is not None
        draft = replace(
            draft, attendee=DRAFT_ATTENDEE_OTHER if answered_other else DRAFT_ATTENDEE_SELF
        )
        ctx.conversation.flow_attendee_name = result.flow_attendee_name
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
        landing_step=resolution.landing_step,
        accepted=list(resolution.accepted),
        dropped=dict(resolution.dropped),
        fallback=resolution.fallback,
    )
    return resumed
```

- [x] **Step 5: The router flags the answer, the runner continues**

1. `src/secretaria/services/flow_router.py`, logo antes de `def _attendee_step(`, inserir:

```python
def _has_draft(conversation: Conversation) -> bool:
    """Whether an AI draft is parked on the conversation (TASK-030 P2)."""
    return bool(getattr(conversation, "flow_draft", None))
```

Em `_attendee_step`, no ramo `LABEL_ATTENDEE_SELF`, trocar

```python
            if result.flow_state == FlowState.SERVICE_CATALOG:
                result.flow_attendee_name = ATTENDEE_SELF
            return result
```

por

```python
            if result.flow_state == FlowState.SERVICE_CATALOG:
                result.flow_attendee_name = ATTENDEE_SELF
                # A parked AI draft continues from here (workers/shared/draft_resolution.py).
                result.resume_draft = _has_draft(conversation)
            return result
```

e no ramo `LABEL_ATTENDEE_AUTH_CONFIRM`, trocar

```python
        if result.flow_state == FlowState.SERVICE_CATALOG:
            result.flow_attendee_name = name
            result.attendee_authorized = True
        return result
```

por

```python
        if result.flow_state == FlowState.SERVICE_CATALOG:
            result.flow_attendee_name = name
            result.attendee_authorized = True
            result.resume_draft = _has_draft(conversation)
        return result
```

2. `src/secretaria/workers/shared/flow_runner.py`:
   - acrescentar o import (ordem do ruff, depois do bloco `from secretaria.workers.shared.deposit import (...)`):

```python
from secretaria.workers.shared.draft_resolution import (
    _resume_booking_draft,
    _turn_booking_gate,
)
```

   - em `_run_flow`, trocar o bloco inteiro do portão — do comentário `# The booking gate for THIS turn. Armed only on Brain-Message, and only` até o `)` que fecha `gate = BookingGate(...)` — por:

```python
    # The booking gate for THIS turn (workers/shared/draft_resolution.py explains the
    # arming rule and the production bug it pins).
    gate = _turn_booking_gate(reply, tenant)
```

   - ainda em `_run_flow`, trocar

```python
    return await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )

async def _apply_flow_result(
```

por

```python
    if result.resume_draft and tenant is not None:
        # Pra-quem answered with an AI draft parked: the resolver lands it instead of the
        # list `result` would show (falls back to `result` on any failure).
        result = await _resume_booking_draft(reply, tenant, result, gate=gate)
    return await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )

async def _apply_flow_result(
```

   - remova os imports `BookingGate` (bloco `from secretaria.services.booking_hold import (BookingGate,)`) e `CHANNEL_BRAIN_MESSAGE` (bloco `from secretaria.services.channel_sender import (...)`) SOMENTE se `uvx ruff check src/secretaria/workers/shared/flow_runner.py` os acusar como não usados (`F401`).

- [x] **Step 6: Run the tests to verify they pass**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_booking_draft_continuation.py tests/test_attendee_booking.py tests/test_booking_code_gate.py tests/test_workers_layering.py tests/test_agent_menu_tools.py tests/test_handback_events.py -q`
Expected: PASS (os 5 novos; o portão continua armando no Portal; camadas respeitadas; os hand-backs do P1 inalterados).

- [ ] **Step 7: Lint and commit**

```bash
uvx ruff format src/secretaria/workers/shared/draft_resolution.py tests/test_booking_draft_continuation.py
uvx ruff check src/secretaria/workers/shared/draft_resolution.py src/secretaria/workers/shared/greeting.py src/secretaria/workers/orchestrator.py src/secretaria/workers/shared/flow_runner.py src/secretaria/services/flow_router.py tests/test_booking_draft_continuation.py
for f in src/secretaria/workers/shared/greeting.py src/secretaria/workers/orchestrator.py src/secretaria/workers/shared/flow_runner.py src/secretaria/services/flow_router.py; do echo "$f $(uvx ruff format --diff $f 2>/dev/null | grep -c '^@@')"; done   # none may exceed its count before editing
git add src/secretaria/workers/shared/draft_resolution.py src/secretaria/workers/shared/greeting.py src/secretaria/workers/orchestrator.py src/secretaria/workers/shared/flow_runner.py src/secretaria/services/flow_router.py tests/test_booking_draft_continuation.py
git diff --cached --stat
git commit -F - <<'EOF'
feat(draft): the pra-quem answer resumes the parked AI draft

The worker half of the resolver: fresh reads scoped to the tenant, this turn's booking
gate (extracted from _run_flow), the agenda built lazily for the professional the
resolver settles on. The tap that answers "Essa consulta é pra você?" re-runs the
resolver over flow_draft; an expired draft or any failure falls back to the plain next
question, and the third-party authorization is recorded exactly as before.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
```

---

## Cobertura da spec (P2a)

| Spec | Onde |
|---|---|
| §4.1 rascunho v2 (formato, v1 aceito, `w` nunca nome) | A2 (a ferramenta é P2b, Task B3) |
| §4.2 ordem pra quem → … → horário; só o item inválido cai; menu só nos 3 casos | A4, A5 |
| §4.2 roda no contexto do portão/reservas; relê do banco | A4 (`booking_gate_scope`, gate padrão), A6 (`_load_draft_context`) |
| §4.2 carrega todos os campos validados | A4 `_carry` |
| §4.3 `flow_draft` + migração antes de API e worker; apagado ao consumir, no menu e por tempo | A3, A6 |
| §4.4.1 horário re-derivado da agenda fresca menos reservas | A5 (`free_slots_for_day`); a confirmação expressa é do P3 (`_express_confirmation`) |
| §6 isolamento de tenant; reservas e portão; expiração; migração SQLite + Postgres; contrato do sentinel nos dois sentidos | A4, A5, A6; A2/A6; A3; A2 |
| §7 `flow_selected_type` sobrecarregado; reservas só dentro de `route()` | A3/A4 (`flow_draft`), A4/A5/A6 |

## Deploy e liberação

Não há deploy no P2a isolado: a coluna existe para o P2b. Quando o dono pedir o deploy do P2 inteiro, a ordem e a liberação estão em `2026-10-02-ia-p2b-handbacks-ferramenta-e-estado.md` §"Deploy e liberação" — **a migração `e7d3c1a9b5f2` primeiro**, depois API **e** worker juntos.
