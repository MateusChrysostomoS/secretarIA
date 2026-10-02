# Visita do Portal descartada ao entrar na conta — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Quando uma pessoa que JÁ tem conta confirma o código numa visita nova, a conversa de cadastro dessa visita é jogada fora (não preservada) e ela cai na conversa que já tinha, com o menu — sem nova apresentação. A apresentação da secretária aparece uma única vez na vida da pessoa naquela clínica. Nunca sobra uma conversa por cadastro.

**Architecture:** O brain-api já decide a fusão: ao completar uma visita cujo e-mail já tinha identidade na clínica, grava `pending.superseded_by = <identidade antiga>`. Hoje a secretarIA não é avisada, então a conversa da visita fica órfã (e o menu "conta verificada" é escrito nela, onde ninguém vê). Passa a existir: (1) na secretarIA, uma rota interna `POST /internal/brain-message/visits/merge` que enfileira um job do worker; o job apaga a visita (paciente, conversa, mensagens, mapa de pseudônimos, reservas) e escreve o menu na conversa antiga — ou, se a identidade antiga nunca teve conversa nessa clínica, faz a abertura normal (primeira vez na vida = apresentação); (2) no brain-api, uma chamada fire-and-forget a essa rota depois de `POST /pending/complete` quando `superseded_by` existe; (3) no front, a tela zera o estado local da conversa quando a promoção troca de identidade. Caso sem identidade antiga (primeira vez): a conversa da visita continua sendo a da conta (mesmo `MessagePatient.id`) — nada muda.

**Tech Stack:** secretarIA: Python 3.12/FastAPI/arq/SQLAlchemy async/pytest. brain-api: FastAPI/httpx/pytest. Front: Next.js static export, TypeScript, vitest.

**Spec:** `secretarIA/docs/LACUNAS_PORTAL_2026-10-01.md` §L2 + decisão do dono (2026-10-01): "aparecer apenas a mensagem de apresentação da secretária uma vez, logo a primeira vez; nas outras vezes que ele for cadastrar no portal pelo chat, essa interação de cadastro seja jogada fora e não preservada, sem criar uma conversa para cada cadastro."

## Global Constraints

- secretarIA: testes com `$env:BOT_ALLOWLIST_WA_IDS=""; uv run python -m pytest <arquivos> -q`; nunca `ruff format .`.
- brain-api: `uv run pytest <arquivos>` (SQLite em memória; sem Docker); lint `make lint` só nos arquivos tocados.
- Front: `.\node_modules\.bin\tsc.cmd --noEmit`, `npm test`, `npm run build` (nunca `npx tsc`; sem ESLint). Texto de UI em português; código/comentários em inglês; nada de `fetch` em componentes.
- Contrato aditivo e fail-soft: a chamada do brain-api NUNCA levanta e nunca atrasa a resposta (background task); 404 de uma secretarIA que ainda não tem a rota = comportamento de hoje.
- Segurança LGPD: o descarte apaga conversa/mensagens/mapa de pseudônimos/reservas da VISITA, mas NÃO apaga `consent_events` nem `processed_events` (trilha de auditoria / idempotência). Decisão do dono (2026-10-01): NÃO há proteção extra por "visita com consulta marcada" — o desenho do fluxo já garante que uma visita que se funde numa conta existente nunca marcou consulta (o código é pedido ANTES do aceite e do agendamento); a primeira conversa da pessoa (apresentação, e-mail, LGPD e o que ela fizer depois) é preservada porque vira a conversa da conta, e logins seguintes mostram essa conversa.
- Estrutura de workers pós-TASK-023: o job novo vive em `src/secretaria/workers/portal/merge.py` (pode importar `shared/` e `portal/`, nunca `whatsapp/` — `tests/test_workers_layering.py` vigia), é reexportado pela fachada `workers/tasks.py` e registrado em `workers/arq_worker.py`. Testes que trocam nomes de worker usam `from tests._patching import workers_ns` + `monkeypatch.setattr(workers_ns, "NOME", valor)`, nunca `setattr(tasks, ...)`.
- Escopo de três elos em toda busca: `tenant_id` + `channel == brain_message` + `external_id`. A visita e a conta têm de ser da MESMA clínica.
- Worker e API são serviços separados: este plano toca `workers/` ⇒ deploy dos DOIS na secretarIA. Ordem: secretarIA (API + worker) → brain-api → frontend. Commit local permitido; push/deploy só com pedido explícito do dono.
- Branch: `main` (fase MVP). Cada repo é uma raiz Git independente; um commit por repo.

## Review Focus

- Visita SEM identidade antiga (primeira vez): `superseded_by` vazio ⇒ nenhuma chamada de fusão, a conversa continua. Teste pinando "não chama".
- Fusão pedida duas vezes (retry/duplo clique): idempotente (ledger), sem apagar a conversa antiga por engano.
- `visit_external_id == into_external_id`: recusar (nunca apagar a própria conta).
- Visita e conta de clínicas diferentes ou paciente que não é `brain_message` (WhatsApp com mesmo texto): inalcançável.
- Conta antiga sem conversa nessa clínica: faz a abertura normal (apresentação), não deixa a tela vazia.
- Clínica sem entitlement: não escreve menu (silêncio por desenho), mas o descarte acontece.
- Front: a identidade trocou ⇒ estado local zerado; identidade igual ⇒ não pisca.

## File Structure

secretarIA (`C:\TECH\BRAIN\secretarIA`)
- Create `src/secretaria/services/visit_merge.py` — `discard_visit` (apaga a visita, só DB).
- Modify `src/secretaria/schemas/internal.py` — `BrainMessageVisitMerge`.
- Modify `src/secretaria/api/internal.py` — rota `POST /internal/brain-message/visits/merge`.
- Create `src/secretaria/workers/portal/merge.py` — job `merge_brain_message_visit`.
- Modify `src/secretaria/workers/tasks.py` — só reexportar o job (fachada).
- Modify `src/secretaria/workers/arq_worker.py` — registrar o job.
- Create `tests/test_visit_merge.py`.

brain-api (`C:\TECH\BRAIN\brain-api`)
- Modify `src/brain_api/services/message_switchboard.py` — `merge_visit`.
- Modify `src/brain_api/api/portal/patient_access.py:1815` — `complete_pending` dispara a fusão.
- Create `tests/test_patient_visit_merge.py`.
- Modify `docs/PORTAL_MESSAGING_API.md`.

Front (`C:\TECH\BRAIN\Brain-Message-Frontend`)
- Modify `lib/real/patient-access.ts:403` — `completePendingVisit` informa `switched`.
- Modify `components/portal/PortalConversation.tsx:390` — zera `state` quando `switched`.
- Create `lib/__tests__/patient-promotion.test.ts`.

---

### Task 1: secretarIA — apagar uma visita (`discard_visit`)

**Files:**
- Create: `src/secretaria/services/visit_merge.py`
- Test: `tests/test_visit_merge.py`

**Interfaces:**
- Produces: `async def discard_visit(session: AsyncSession, tenant_id: UUID, visit_external_id: str) -> VisitDiscard` onde `VisitDiscard` é `@dataclass(frozen=True)` com `status: Literal["discarded","absent"]` e `messages: int`. NÃO faz `commit` (o chamador comita). Apaga, nesta ordem, filhos explícitos (SQLite dos testes não impõe CASCADE): `Message`, `ConversationPiiTokenMap`, `BookingHold`, `Conversation`, `Patient`.

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_visit_merge.py` (esta suíte redefine `db` por arquivo):

```python
"""A visit merged into an existing account is thrown away (LACUNAS L2)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest_asyncio  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    ConsentEvent,
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)
from secretaria.services.visit_merge import discard_visit  # noqa: E402

VISIT = "visit-aaaa"
ACCOUNT = "account-bbbb"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


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


async def _tenant(db) -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id="1234567890",
            is_active=True,
            clinic_description="x",
            initial_flows={},
        )
        session.add(tenant)
        await session.commit()
        return tenant


async def _portal_patient(db, tenant, external_id, *, messages=2, channel="brain_message"):
    async with db() as session:
        patient = Patient(
            tenant_id=tenant.id,
            channel=channel,
            external_id=external_id,
            wa_id=None if channel == "brain_message" else "5511900000000",
            name="Maria",
            lgpd_accepted_at=NOW,
        )
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        for i in range(messages):
            session.add(
                Message(
                    conversation_id=conversation.id,
                    direction=MessageDirection.OUTBOUND,
                    sender=MessageSender.BOT,
                    body=f"m{i}",
                    created_at=NOW + timedelta(minutes=i),
                    updated_at=NOW + timedelta(minutes=i),
                )
            )
        session.add(ConsentEvent(tenant_id=tenant.id, wa_id=external_id, kind="first_contact_service"))
        await session.commit()
        return patient, conversation


async def _count(db, model) -> int:
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def test_discard_removes_the_visit_and_keeps_the_audit_trail(db) -> None:
    tenant = await _tenant(db)
    await _portal_patient(db, tenant, VISIT, messages=3)
    await _portal_patient(db, tenant, ACCOUNT, messages=5)
    async with db() as session:
        result = await discard_visit(session, tenant.id, VISIT)
        await session.commit()
    assert result.status == "discarded"
    assert result.messages == 3
    assert await _count(db, Patient) == 1  # only the account's patient is left
    assert await _count(db, Conversation) == 1
    assert await _count(db, Message) == 5  # the account's history is untouched
    assert await _count(db, ConsentEvent) == 2  # audit trail is never deleted


async def test_discard_of_an_unknown_visit_is_a_quiet_no_op(db) -> None:
    tenant = await _tenant(db)
    async with db() as session:
        result = await discard_visit(session, tenant.id, "nobody")
    assert result.status == "absent"


async def test_discard_never_reaches_a_whatsapp_patient_with_the_same_string(db) -> None:
    tenant = await _tenant(db)
    await _portal_patient(db, tenant, VISIT, channel="whatsapp")
    async with db() as session:
        result = await discard_visit(session, tenant.id, VISIT)
    assert result.status == "absent"
    assert await _count(db, Patient) == 1


async def test_discard_is_scoped_to_the_tenant(db) -> None:
    mine = await _tenant(db)
    other = await _tenant(db)
    await _portal_patient(db, other, VISIT)
    async with db() as session:
        result = await discard_visit(session, mine.id, VISIT)
    assert result.status == "absent"
    assert await _count(db, Patient) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `$env:BOT_ALLOWLIST_WA_IDS=""; uv run python -m pytest tests/test_visit_merge.py -q`
Expected: FAIL — `ModuleNotFoundError: secretaria.services.visit_merge`. (Se `ConsentEvent(...)` reclamar de coluna obrigatória, abrir `src/secretaria/models/consent_event.py` e ajustar SÓ o construtor do teste; a assinatura de `discard_visit` não muda.)

- [ ] **Step 3: Implement**

Criar `src/secretaria/services/visit_merge.py`:

```python
"""Throw away a Portal VISIT that was merged into an existing account.

A visit is the conversation a person has BEFORE the clinic knows who they are (greeting,
e-mail, code). When the address turns out to belong to an account that already has its own
conversation at this clinic, brain-api keeps the old identity and the visit's conversation
is left orphaned: nobody can reach it, and every signup used to leave one behind.

What is deleted is the conversation's own data: messages, the pseudonym map, booking holds,
the conversation and the patient row. What is NOT deleted: `consent_events` (keyed by the
visit's string handle, an audit trail on purpose) and `processed_events` (idempotence ledger).
No appointment guard (owner's decision, 2026-10-01): a visit that merges into an existing
account has by construction never booked - the code is asked BEFORE consent and booking.

The function does not commit: the caller owns the transaction. Children are deleted
explicitly because SQLite (tests) does not enforce ON DELETE CASCADE.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Conversation, Message, Patient
from secretaria.models.booking_hold import BookingHold
from secretaria.models.conversation_pii_token_map import ConversationPiiTokenMap
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE

logger = get_logger(__name__)


@dataclass(frozen=True)
class VisitDiscard:
    status: Literal["discarded", "absent"]
    messages: int = 0


async def discard_visit(
    session: AsyncSession, tenant_id: UUID, visit_external_id: str
) -> VisitDiscard:
    patient = await session.scalar(
        select(Patient).where(
            Patient.tenant_id == tenant_id,
            Patient.channel == CHANNEL_BRAIN_MESSAGE,
            Patient.external_id == visit_external_id,
        )
    )
    if patient is None:
        return VisitDiscard(status="absent")

    conversation_ids = list(
        await session.scalars(select(Conversation.id).where(Conversation.patient_id == patient.id))
    )
    message_count = 0
    if conversation_ids:
        message_count = (
            await session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id.in_(conversation_ids))
            )
            or 0
        )
        for child in (Message, ConversationPiiTokenMap, BookingHold):
            await session.execute(delete(child).where(child.conversation_id.in_(conversation_ids)))
        await session.execute(delete(Conversation).where(Conversation.id.in_(conversation_ids)))
    await session.execute(delete(Patient).where(Patient.id == patient.id))
    logger.info("visit_discarded", tenant_id=str(tenant_id), messages=message_count)
    return VisitDiscard(status="discarded", messages=message_count)
```

- [ ] **Step 4: Run to verify it passes**

Run: `$env:BOT_ALLOWLIST_WA_IDS=""; uv run python -m pytest tests/test_visit_merge.py -q`
Expected: PASS (4 testes).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff check src/secretaria/services/visit_merge.py tests/test_visit_merge.py
uvx ruff format src/secretaria/services/visit_merge.py tests/test_visit_merge.py
git add src/secretaria/services/visit_merge.py tests/test_visit_merge.py
git commit -m "feat(portal): discard a visit that was merged into an account"
```

---

### Task 2: secretarIA — rota de fusão + job do worker

**Files:**
- Modify: `src/secretaria/schemas/internal.py` (final do arquivo)
- Modify: `src/secretaria/api/internal.py` (importar o schema; nova rota depois de `mark_brain_message_read`)
- Create: `src/secretaria/workers/portal/merge.py` (o job novo)
- Modify: `src/secretaria/workers/tasks.py` (reexportar o job na fachada, junto dos outros `from secretaria.workers.portal... import`)
- Modify: `src/secretaria/workers/arq_worker.py:102` (registrar)
- Test: `tests/test_visit_merge.py` (acrescentar)

**Interfaces:**
- Consumes: `discard_visit` (Task 1); `_claim_event` (`workers/shared/jobs.py`), `_handle_show_main_menu` (`workers/shared/sentinels.py`), `process_brain_message_open` (`workers/portal/open.py`), `_ReplyContext` (confirmar onde mora: `grep -rn "class _ReplyContext" src/secretaria/workers`), `get_entitlements`, `list_active_professionals`, `CHANNEL_BRAIN_MESSAGE`, `async_session_factory` (ver como `portal/open.py` os importa e copiar os mesmos imports).
- Produces: `POST /internal/brain-message/visits/merge` body `{"tenant_id": UUID, "visit_external_id": str(1..64), "into_external_id": str(1..64)}` → `202 {"status":"queued"}`; `422` se `visit_external_id == into_external_id`; exige `X-Internal-Api-Key`. Job arq `merge_brain_message_visit(ctx, tenant_id: str, visit_external_id: str, into_external_id: str) -> None`.

- [ ] **Step 1: Write the failing tests**

Acrescentar ao fim de `tests/test_visit_merge.py`:

```python
import httpx  # noqa: E402
import pytest  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from tests._patching import workers_ns  # noqa: E402

KEY = {"X-Internal-Api-Key": "test-internal-key"}
MERGE_URL = "/internal/brain-message/visits/merge"


class _Queue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple, dict]] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.jobs.append((name, args, kwargs))


@pytest_asyncio.fixture
async def api(db):
    from secretaria.config import get_settings
    from secretaria.core.database import get_session

    get_settings.cache_clear()
    from secretaria.main import app

    async def _override_session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    pool = _Queue()
    app.state.arq_pool = pool
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield SimpleNamespace(client=client, pool=pool)
    app.dependency_overrides.clear()


async def test_merge_route_requires_the_internal_key(api) -> None:
    body = {"tenant_id": str(uuid4()), "visit_external_id": VISIT, "into_external_id": ACCOUNT}
    assert (await api.client.post(MERGE_URL, json=body)).status_code in (401, 403)


async def test_merge_route_enqueues_one_job(api) -> None:
    tenant_id = str(uuid4())
    body = {"tenant_id": tenant_id, "visit_external_id": VISIT, "into_external_id": ACCOUNT}
    resp = await api.client.post(MERGE_URL, json=body, headers=KEY)
    assert resp.status_code == 202
    assert resp.json() == {"status": "queued"}
    assert api.pool.jobs == [("merge_brain_message_visit", (tenant_id, VISIT, ACCOUNT), {})]


async def test_merge_route_refuses_merging_a_handle_into_itself(api) -> None:
    body = {"tenant_id": str(uuid4()), "visit_external_id": VISIT, "into_external_id": VISIT}
    resp = await api.client.post(MERGE_URL, json=body, headers=KEY)
    assert resp.status_code == 422
    assert api.pool.jobs == []


@pytest.fixture
def _worker(monkeypatch, db):
    monkeypatch.setattr(workers_ns, "async_session_factory", db)

    async def _entitled(tenant_id, redis):
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

    async def _no_ledger(key):
        return True

    monkeypatch.setattr(workers_ns, "get_entitlements", _entitled)
    monkeypatch.setattr(workers_ns, "_claim_event", _no_ledger)


async def test_job_discards_the_visit_and_shows_the_menu_in_the_account(db, _worker) -> None:
    tenant = await _tenant(db)
    await _portal_patient(db, tenant, VISIT, messages=3)
    _, account_conv = await _portal_patient(db, tenant, ACCOUNT, messages=5)

    await tasks.merge_brain_message_visit({"redis": None}, str(tenant.id), VISIT, ACCOUNT)

    async with db() as session:
        visit = await session.scalar(select(Patient).where(Patient.external_id == VISIT))
        assert visit is None
        rows = list(
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == account_conv.id)
                .order_by(Message.created_at)
            )
        )
    # The old history is intact and exactly ONE new bot row (the menu) was added - no greeting.
    assert len(rows) == 6
    assert rows[-1].sender == MessageSender.BOT
    assert rows[-1].interactive is not None


async def test_job_opens_the_account_when_it_never_had_a_conversation(db, _worker, monkeypatch) -> None:
    tenant = await _tenant(db)
    await _portal_patient(db, tenant, VISIT, messages=3)
    opened: list[tuple] = []

    async def _open(ctx, tenant_id, external_id, patient_name=None):
        opened.append((tenant_id, external_id))

    monkeypatch.setattr(workers_ns, "process_brain_message_open", _open)
    await tasks.merge_brain_message_visit({"redis": None}, str(tenant.id), VISIT, ACCOUNT)
    assert opened == [(str(tenant.id), ACCOUNT)]
    async with db() as session:
        assert await session.scalar(select(Patient).where(Patient.external_id == VISIT)) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `$env:BOT_ALLOWLIST_WA_IDS=""; uv run python -m pytest tests/test_visit_merge.py -q`
Expected: FAIL — 404 na rota e `AttributeError: merge_brain_message_visit`.

- [ ] **Step 3: Schema**

No fim de `src/secretaria/schemas/internal.py`:

```python
class BrainMessageVisitMerge(BaseModel):
    """brain-api merged a Portal visit into an existing account: drop the visit's chat."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    visit_external_id: str = Field(min_length=1, max_length=64)
    into_external_id: str = Field(min_length=1, max_length=64)
```

(`ConfigDict`, `Field`, `UUID` já são importados nesse arquivo; se algum não for, adicionar ao import existente.)

- [ ] **Step 4: Route**

Em `src/secretaria/api/internal.py`: adicionar `BrainMessageVisitMerge` ao import de `secretaria.schemas.internal` e, depois de `mark_brain_message_read`:

```python
@router.post(
    "/brain-message/visits/merge",
    response_model=BrainMessageAck,
    status_code=status.HTTP_202_ACCEPTED,
    summary="A Portal visit was merged into an existing account (internal)",
    description=(
        "brain-api calls this after a visit's code proved an address that already had an "
        "identity at the clinic. The visit's own conversation is discarded and the account's "
        "conversation gets the menu (or, if the account never had one, the normal opening). "
        "Idempotent; returns 202 immediately, the work runs in the worker. Requires the "
        "X-Internal-Api-Key header."
    ),
    responses={**_INTERNAL_RESPONSES, 422: {"description": "Visit and account are the same."}},
)
async def merge_brain_message_visit_route(
    payload: BrainMessageVisitMerge, request: Request
) -> BrainMessageAck:
    if payload.visit_external_id == payload.into_external_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A visit cannot be merged into itself.",
        )
    pool = _arq_pool_or_503(request)
    await pool.enqueue_job(
        "merge_brain_message_visit",
        str(payload.tenant_id),
        payload.visit_external_id,
        payload.into_external_id,
    )
    logger.info("brain_message_visit_merge_queued", tenant_id=str(payload.tenant_id))
    return BrainMessageAck(status="queued")
```

(Conferir em `brain_message_open` o nome exato do helper e a forma de construir `BrainMessageAck` — copiar o mesmo padrão usado lá para o `202`.)

- [ ] **Step 5: Worker job**

Criar `src/secretaria/workers/portal/merge.py` com o cabeçalho (copiar de `workers/portal/open.py` os imports de `UUID`, `select`, `Tenant`, `Patient`, `Conversation`, `async_session_factory`, `get_entitlements`, `list_active_professionals`, `CHANNEL_BRAIN_MESSAGE`, `get_logger`/`logger`, `_ReplyContext`, `_claim_event`, `_handle_show_main_menu`, `process_brain_message_open`, e acrescentar `from secretaria.services.visit_merge import discard_visit`) e a função:

```python
async def merge_brain_message_visit(
    ctx: dict,
    tenant_id: str,
    visit_external_id: str,
    into_external_id: str,
) -> None:
    """arq job: a Portal visit was merged into an existing account - throw the visit away.

    The visit's conversation (greeting, e-mail, code) is deleted; it is NOT carried over, by
    the owner's decision: the opening shows once, the first time ever. The account's own
    conversation then gets the main menu so a patient who returns mid-flow is never left
    staring at a stale step. An account that never had a conversation here gets the normal
    opening instead (that IS its first time).

    Idempotent through a ledger key, like the open job. The discard is committed BEFORE
    anything is sent, so a send failure can never resurrect the visit.
    """
    tenant_uuid = UUID(tenant_id)
    key = f"brain_message_merge:{tenant_id}:{visit_external_id}"
    if not await _claim_event(key):
        logger.info("brain_message_merge_already_claimed", tenant_id=tenant_id)
        return

    async with async_session_factory() as session:
        discarded = await discard_visit(session, tenant_uuid, visit_external_id)
        await session.commit()
    logger.info("brain_message_merge_discarded", tenant_id=tenant_id, status=discarded.status)

    async with async_session_factory() as session:
        account = await session.scalar(
            select(Patient).where(
                Patient.tenant_id == tenant_uuid,
                Patient.channel == CHANNEL_BRAIN_MESSAGE,
                Patient.external_id == into_external_id,
            )
        )
        conversation = (
            await session.scalar(
                select(Conversation).where(
                    Conversation.tenant_id == tenant_uuid, Conversation.patient_id == account.id
                )
            )
            if account is not None
            else None
        )
        tenant = await session.get(Tenant, tenant_uuid)
        professionals = (
            await list_active_professionals(session, tenant_uuid) if tenant is not None else []
        )

    if tenant is None:
        return
    if conversation is None:
        # The account's identity never had a conversation here: this IS the first time.
        await process_brain_message_open(ctx, tenant_id, into_external_id)
        return

    summary = await get_entitlements(tenant_uuid, ctx.get("redis"))
    if summary is None or not (summary.active and summary.secretaria_enabled):
        logger.warning(
            "bot_reply_suppressed_unentitled",
            tenant_id=tenant_id,
            entitlement_unknown=summary is None,
            conversation_id=str(conversation.id),
        )
        return
    reply = _ReplyContext(
        channel=CHANNEL_BRAIN_MESSAGE,
        conversation_id=conversation.id,
        patient_ref=into_external_id,
        inbound_body="",
        tenant_id=tenant_uuid,
    )
    await _handle_show_main_menu(
        reply,
        tenant,
        professionals,
        into_external_id,
        redis=ctx.get("redis"),
        source="merged_visit",
    )
```

- [ ] **Step 6: Re-export and register the job**

Em `src/secretaria/workers/tasks.py` (fachada), acrescentar `from secretaria.workers.portal.merge import merge_brain_message_visit` ao lado dos reexports de `portal.open`. Em `src/secretaria/workers/arq_worker.py`, importar `merge_brain_message_visit` junto dos outros jobs (de `tasks`, como os demais) e adicionar na lista `functions` logo abaixo de `process_brain_message_open,`:

```python
        # A Portal visit merged into an existing account: discard it, menu in the account.
        merge_brain_message_visit,
```

- [ ] **Step 7: Run tests and neighbours**

Run: `$env:BOT_ALLOWLIST_WA_IDS=""; uv run python -m pytest tests/test_visit_merge.py tests/test_brain_message_open.py tests/test_brain_message_pipeline.py tests/test_turn_safety_net.py tests/test_workers_layering.py -q`
Expected: PASS. Se `test_job_discards_the_visit_and_shows_the_menu_in_the_account` falhar porque `_apply_flow_result` exige algo que o `_ReplyContext` mínimo não traz, ler `_apply_flow_result` e `_handle_pre_consent_identity` (que chama `_handle_show_main_menu` com `source="verified_account"`) e replicar os mesmos argumentos — o contrato do teste (1 linha nova, do bot, com `interactive`) não muda.

- [ ] **Step 8: Lint and commit**

```bash
uvx ruff check src/secretaria tests/test_visit_merge.py
uvx ruff format src/secretaria/workers/portal/merge.py src/secretaria/schemas/internal.py src/secretaria/api/internal.py src/secretaria/workers/arq_worker.py tests/test_visit_merge.py
git add -u; git add src/secretaria/workers/portal/merge.py tests/test_visit_merge.py
git commit -m "feat(portal): merge-visit route and worker job"
```

(Não rodar `ruff format` na fachada `tasks.py`: só reexports, e o repo tem lint vermelho histórico; não reformatar arquivos grandes inteiros.)

---

### Task 3: brain-api — avisar a secretarIA depois de completar a visita

**Files:**
- Modify: `src/brain_api/services/message_switchboard.py` (depois de `open_conversation`, ~linha 453)
- Modify: `src/brain_api/api/portal/patient_access.py:1815-1840`
- Create: `tests/test_patient_visit_merge.py`
- Modify: `docs/PORTAL_MESSAGING_API.md`

**Interfaces:**
- Produces: `async def merge_visit(*, tenant_id: UUID, visit_ref: str, into_ref: str) -> str` (nunca levanta; devolve `MERGE_QUEUED | MERGE_FAILED | MERGE_UNCONFIGURED`). `complete_pending` agenda `merge_visit` como background task SOMENTE quando a visita foi substituída (`superseded_by` ≠ `patient_id`).

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_patient_visit_merge.py`. Copiar o cabeçalho de imports, `_configure_mesh`, `_spy_transport`, `_internal_key` e os helpers `_open/_claim/_prove/_identity_call/_seed_identity` de `tests/test_patient_pending_session.py` (mesmos nomes; este repo os repete por arquivo) e acrescentar:

```python
async def test_completing_a_superseded_visit_tells_secretaria_to_merge(pclient, monkeypatch):
    """A twin identity exists at the clinic => the visit's chat is discarded upstream."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})
    # Arrange: reuse the scenario of test_a_clinic_that_already_knows_the_address_keeps_its_own_identity
    # (tests/test_patient_pending_session.py:519-548): an existing identity for the address, then a
    # fresh visit that claims and proves the same address.
    visit_ref, old_ref, headers = await _visit_with_twin(client, sessionmaker, seed, monkeypatch)

    resp = await client.post("/patient-access/pending/complete", headers=headers)
    assert resp.status_code == 200

    merges = [c for c in calls if c["url"] == "/internal/brain-message/visits/merge"]
    assert len(merges) == 1
    assert merges[0]["json"] == {
        "tenant_id": str(seed.both),
        "visit_external_id": visit_ref,
        "into_external_id": old_ref,
    }


async def test_completing_a_first_time_visit_does_not_merge_anything(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})
    headers = await _verified_visit_without_twin(client, sessionmaker, seed, monkeypatch)

    resp = await client.post("/patient-access/pending/complete", headers=headers)
    assert resp.status_code == 200
    assert [c for c in calls if c["url"].endswith("/visits/merge")] == []


async def test_a_secretaria_that_refuses_the_merge_never_breaks_the_login(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    _spy_transport(monkeypatch, status_code=404, payload={"detail": "nope"})
    _, _, headers = await _visit_with_twin(client, sessionmaker, seed, monkeypatch)
    resp = await client.post("/patient-access/pending/complete", headers=headers)
    assert resp.status_code == 200
```

`_visit_with_twin` e `_verified_visit_without_twin` são helpers a escrever neste arquivo reaproveitando EXATAMENTE os passos dos testes `test_a_clinic_that_already_knows_the_address_keeps_its_own_identity` (519-548) e do teste de visita sem gêmeo (`test_an_address_that_already_has_an_account_gains_this_clinic` e vizinhos) — cada um devolve `(visit_ref, old_ref, headers_com_bearer_da_visita)` / `headers`. Se o helper do teste original já devolver isso, importar em vez de duplicar.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_patient_visit_merge.py -q`
Expected: FAIL — nenhuma chamada a `/visits/merge`.

- [ ] **Step 3: `merge_visit` in the switchboard**

Em `src/brain_api/services/message_switchboard.py`, depois de `open_conversation`:

```python
_MERGE_PATH = "/internal/brain-message/visits/merge"
MERGE_QUEUED = "queued"
MERGE_UNCONFIGURED = "unconfigured"
MERGE_FAILED = "failed"


async def merge_visit(*, tenant_id: UUID, visit_ref: str, into_ref: str) -> str:
    """Tell secretarIA to throw a merged visit's conversation away — FIRE AND FORGET.

    Same contract as `open_conversation`: it never raises, because it runs after the response
    to `POST /pending/complete` has been written and there is nobody to show an error to. A
    secretarIA that has not got the route yet answers 404 — the visit's chat is then simply
    left orphaned, exactly today's behaviour. Handles are ids, never logged.
    """
    try:
        base, headers = _upstream(PRODUCT_SECRETARIA)
    except HTTPException:
        logger.warning("brain_message_merge_unconfigured", tenant_id=str(tenant_id))
        return MERGE_UNCONFIGURED
    body = {
        "tenant_id": str(tenant_id),
        "visit_external_id": visit_ref,
        "into_external_id": into_ref,
    }
    try:
        async with httpx.AsyncClient(
            base_url=base, timeout=get_settings().SECRETARIA_TIMEOUT_SECONDS
        ) as client:
            resp = await client.request("POST", _MERGE_PATH, headers=headers, json=body)
    except Exception as exc:  # noqa: BLE001 - "never raises" must hold for EVERY exception
        logger.error(
            "brain_message_merge_unexpected_error",
            tenant_id=str(tenant_id),
            error=type(exc).__name__,
        )
        return MERGE_FAILED
    if resp.status_code == status.HTTP_202_ACCEPTED:
        logger.info("brain_message_merge_queued", tenant_id=str(tenant_id))
        return MERGE_QUEUED
    logger.warning(
        "brain_message_merge_refused", tenant_id=str(tenant_id), upstream_status=resp.status_code
    )
    return MERGE_FAILED
```

- [ ] **Step 4: Trigger from `complete_pending`**

Em `src/brain_api/api/portal/patient_access.py`, trocar a assinatura e o corpo de `complete_pending`:

```python
async def complete_pending(
    response: Response,
    background_tasks: BackgroundTasks,
    visit: tuple[MessagePatient, MessagePendingSession] = Depends(get_current_pending),
    session: AsyncSession = Depends(get_session),
) -> PatientAccountOut:
    """(docstring existente, mais:)

    When the address already had an identity at this clinic (`superseded_by`), the visit's own
    conversation upstream is now orphaned: secretarIA is told to discard it and to show the
    menu in the account's conversation instead (owner's decision, 2026-10-01: the opening shows
    once, the first time ever). Handles are read BEFORE the exchange commits.
    """
    _, pending = visit
    visit_ref = str(pending.patient_id)
    into_ref = str(pending.superseded_by) if pending.superseded_by else None
    tenant_id = pending.tenant_id
    completed = await patient_access.complete_pending_identity(session, pending)
    if completed is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "pending_verification_incomplete")
    account, canonical, raw_session, session_id = completed
    set_patient_session_cookie(response, raw_session)
    clear_patient_pending_cookie(response)
    if into_ref is not None and into_ref != visit_ref:
        background_tasks.add_task(
            message_switchboard.merge_visit,
            tenant_id=tenant_id,
            visit_ref=visit_ref,
            into_ref=into_ref,
        )
    return await _account_body(
        session,
        account,
        session_id,
        canonical,
        invited_tenant_id=canonical.tenant_id,
    )
```

(`BackgroundTasks` e `message_switchboard` já são importados no arquivo — `_greet_if_secretaria` os usa.)

- [ ] **Step 5: Run tests and neighbours**

Run: `uv run pytest tests/test_patient_visit_merge.py tests/test_patient_pending_session.py tests/test_patient_access.py -q`
Expected: PASS.

- [ ] **Step 6: Document and commit**

Em `docs/PORTAL_MESSAGING_API.md`, na parte da visita: "Ao completar uma visita cujo e-mail já tinha identidade na clínica, o brain-api chama `POST {secretarIA}/internal/brain-message/visits/merge` (fire-and-forget) e a secretarIA descarta a conversa da visita." Depois:

```bash
make lint
git add src/brain_api/services/message_switchboard.py src/brain_api/api/portal/patient_access.py tests/test_patient_visit_merge.py docs/PORTAL_MESSAGING_API.md
git commit -m "feat(portal): tell secretarIA to discard a visit merged into an account"
```

---

### Task 4: Front — zerar o estado local quando a identidade troca

**Files:**
- Modify: `lib/real/patient-access.ts:403-426`
- Modify: `components/portal/PortalConversation.tsx:390-394`
- Test: `lib/__tests__/patient-promotion.test.ts`

**Interfaces:**
- Produces: `export type PromotedClinic = ClinicRef & { switched: boolean }`; `completePendingVisit(): Promise<PromotedClinic>` onde `switched = session.patientRef !== visit.patientRef`. Função pura `export function promotionSwitched(visitRef: string, accountRef: string): boolean` (testável).

- [ ] **Step 1: Write the failing test**

Criar `lib/__tests__/patient-promotion.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { promotionSwitched } from "@/lib/real/patient-access";

describe("promotionSwitched", () => {
  it("is true when the account's conversation is another one than the visit's", () => {
    expect(promotionSwitched("visit-1", "account-1")).toBe(true);
  });
  it("is false when the visit's own identity became the account's", () => {
    expect(promotionSwitched("same", "same")).toBe(false);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm test -- lib/__tests__/patient-promotion.test.ts`
Expected: FAIL — `promotionSwitched` não exportada.

- [ ] **Step 3: Implement**

Em `lib/real/patient-access.ts`:

```ts
export type PromotedClinic = ClinicRef & {
  // True when the account's conversation is a DIFFERENT one than the visit's: the visit's
  // chat was thrown away upstream, so nothing the page held locally belongs to it any more.
  switched: boolean;
};

export function promotionSwitched(visitRef: string, accountRef: string): boolean {
  return visitRef !== accountRef;
}
```

e em `completePendingVisit`: mudar a assinatura para `Promise<PromotedClinic>` e o `return refOf(session);` final para:

```ts
  return { ...refOf(session), switched: promotionSwitched(visit.patientRef, session.patientRef) };
```

Em `components/portal/PortalConversation.tsx`, no `askPending`, depois de `const account = await completePendingVisit();`/`promoted = true;`/`if (!mounted.current) return;`:

```ts
      // The visit's chat was discarded upstream when the account already had its own: drop
      // everything held locally (the e-mail and code bubbles included) so the next poll
      // paints the account's conversation, not a mix of the two.
      if (account.switched) setState({});
```

- [ ] **Step 4: Run gates**

Run: `npm test` ; `.\node_modules\.bin\tsc.cmd --noEmit` ; `npm run build`
Expected: tudo verde (os testes que montam `completePendingVisit` em `patient-account-flow.test.ts` continuam passando: o campo novo é aditivo).

- [ ] **Step 5: Commit**

```bash
git add lib/real/patient-access.ts components/portal/PortalConversation.tsx lib/__tests__/patient-promotion.test.ts
git commit -m "fix(portal): drop local chat state when the visit is merged into the account"
```

---

### Task 5: Documentação e prova ao vivo (depois do deploy autorizado)

- [ ] **Step 1:** `docs/CHECKPOINT_portal_visita_fundida.md` na secretarIA (estado, contrato da rota, ordem de deploy: **secretaria_api + secretaria-worker → brain-api → frontend**, sem migração) e ponteiro de 1 linha no `CLAUDE.md` de cada repo tocado.
- [ ] **Step 2:** Prova (agent-browser, sessão nova SEM cookies): abrir o link da clínica → digitar o e-mail da conta de QA → ler o código na caixa que envia os e-mails → confirmar. Esperado: a tela mostra a conversa antiga com UMA bolha nova (o menu) e nenhuma nova apresentação; ao recarregar (F5) nada some.
- [ ] **Step 3:** Prova do descarte: nos logs do worker (`get_service_logs`, só leitura) aparecer `brain_message_merge_discarded status=discarded` e `visit_discarded`; repetir o cadastro 2 vezes e conferir que a conta continua com UMA conversa só.
- [ ] **Step 4:** Registrar prints e os eventos no CHECKPOINT.
