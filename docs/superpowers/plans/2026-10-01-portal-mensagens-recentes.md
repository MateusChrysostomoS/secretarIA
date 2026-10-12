# Portal mostra as mensagens mais recentes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Uma conversa do Portal com mais de 50 mensagens passa a mostrar as 50 MAIS RECENTES (hoje mostra as 50 mais antigas e congela), com um cursor `before` para voltar no tempo.

**Architecture:** Só a secretarIA muda. A listagem interna `GET /internal/brain-message/conversations/{external_id}/messages`, quando chamada SEM `since` (primeira carga e todo poll do Portal, que não usa `since`), devolve as `limit` mais recentes em ordem cronológica, mais `has_more`; um `before` opcional pagina para trás. O ramo com `since` (poll incremental) fica byte a byte igual. O brain-api repassa o corpo sem tocar (`RelayOut` aceita campos extras), então não há mudança de código lá — só a documentação do contrato. O front não muda.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, pytest (SQLite em memória), ruff.

**Spec:** `secretarIA/docs/LACUNAS_PORTAL_2026-10-01.md` §L1 (causa raiz provada ao vivo: `brain_message_messages_listed count=50` constante numa conta com histórico longo).

## Global Constraints

- Rodar testes com `cd C:\TECH\BRAIN\secretarIA` + `$env:BOT_ALLOWLIST_WA_IDS=""; uv run python -m pytest <arquivo> -q` (nunca `pytest` solto; nunca `ruff format .` — só nos arquivos tocados).
- Contrato aditivo: campo novo de resposta com default (`has_more: bool = False`), parâmetro novo opcional (`before`). Chamadores antigos continuam válidos (skill `frozen-contract-migration`).
- O escopo de três elos (tenant, canal `brain_message`, `external_id`) NÃO muda; um paciente WhatsApp com o mesmo texto continua inalcançável.
- Texto de UI em português; código e comentários em inglês.
- Deploy: só `secretaria_api` (a rota vive na API). O worker não é tocado. Commit local é permitido; push e deploy só com pedido explícito do dono.

## Review Focus

- Exatamente `limit` mensagens, `limit + 1` mensagens e 0 mensagens (`has_more` falso/verdadeiro/falso).
- Mensagens com o mesmo `created_at` (SQLite tem resolução de 1 s): ordem determinística por `id`, sem perder nem duplicar linha entre páginas.
- `since` e `before` juntos: 422 (sem escolha silenciosa).
- Paciente/conversa desconhecidos: `data=[]`, `has_more=false` (nunca 404).
- O ramo `since` continua devolvendo mensagem cujo status mudou depois (testes existentes de `test_message_delivery_status.py`).

## File Structure

- Modify `src/secretaria/schemas/internal.py:213` — `BrainMessageMessageList` ganha `has_more`.
- Modify `src/secretaria/api/internal.py:638-762` — `list_brain_message_messages`: parâmetro `before`, primeira carga = mais recentes.
- Create `tests/test_brain_message_recent_page.py` — testes novos (fixtures próprias: este repo redefine `db` em cada arquivo).
- Modify `brain-api/docs/PORTAL_MESSAGING_API.md` — registrar `has_more`/`before` (cross-repo, só texto).

---

### Task 1: Listagem devolve as mensagens mais recentes

**Files:**
- Modify: `src/secretaria/schemas/internal.py:213-216`
- Modify: `src/secretaria/api/internal.py:651-762`
- Test: `tests/test_brain_message_recent_page.py`

**Interfaces:**
- Produces: `GET .../messages?tenant_id=<uuid>[&since=<iso>][&before=<iso>][&limit=<1..200>]` → `{"data": [BrainMessageMessage...], "has_more": bool}`. Sem `since`: `data` = as `limit` mais recentes, ordem cronológica crescente (`created_at`, `id`); `has_more` = existem mais antigas. Com `before`: só `created_at < before`. Com `since`: comportamento atual, `has_more` sempre `false`. `since` + `before` → 422.

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_brain_message_recent_page.py`:

```python
"""The Portal transcript shows the NEWEST messages, not the oldest 50 (LACUNAS L1)."""

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

import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)

KEY = {"X-Internal-Api-Key": "test-internal-key"}
EXTERNAL_ID = "bm-recent-001"
BASE = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


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


@pytest_asyncio.fixture
async def client(db):
    from secretaria.config import get_settings
    from secretaria.core.database import get_session

    get_settings.cache_clear()
    from secretaria.main import app

    async def _override_session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()


async def _seed(db, count: int, *, channel: str = "brain_message") -> Tenant:
    """`count` outbound bot rows, body `m0..m{count-1}`, one minute apart, oldest first."""
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
        patient = Patient(
            tenant_id=tenant.id,
            channel=channel,
            external_id=EXTERNAL_ID,
            wa_id=None if channel == "brain_message" else "5511900000000",
            name="Maria",
            lgpd_accepted_at=BASE,
        )
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        for i in range(count):
            at = BASE + timedelta(minutes=i)
            session.add(
                Message(
                    conversation_id=conversation.id,
                    direction=MessageDirection.OUTBOUND,
                    sender=MessageSender.BOT,
                    body=f"m{i}",
                    created_at=at,
                    updated_at=at,
                )
            )
        await session.commit()
        return tenant


def _url() -> str:
    return f"/internal/brain-message/conversations/{EXTERNAL_ID}/messages"


async def test_first_load_returns_the_newest_fifty_oldest_first(client, db) -> None:
    tenant = await _seed(db, 60)
    resp = await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)
    assert resp.status_code == 200
    body = resp.json()
    bodies = [m["body"] for m in body["data"]]
    assert bodies == [f"m{i}" for i in range(10, 60)]
    assert body["has_more"] is True


async def test_exactly_the_page_size_has_no_more(client, db) -> None:
    tenant = await _seed(db, 50)
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert [m["body"] for m in body["data"]] == [f"m{i}" for i in range(50)]
    assert body["has_more"] is False


async def test_one_over_the_page_size_has_more(client, db) -> None:
    tenant = await _seed(db, 51)
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert body["data"][0]["body"] == "m1"
    assert body["data"][-1]["body"] == "m50"
    assert body["has_more"] is True


async def test_before_pages_backwards_without_overlap(client, db) -> None:
    tenant = await _seed(db, 60)
    first = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    oldest_seen = first["data"][0]["created_at"]
    older = (
        await client.get(
            _url(), params={"tenant_id": str(tenant.id), "before": oldest_seen}, headers=KEY
        )
    ).json()
    assert [m["body"] for m in older["data"]] == [f"m{i}" for i in range(10)]
    assert older["has_more"] is False


async def test_empty_conversation_is_empty_not_an_error(client, db) -> None:
    tenant = await _seed(db, 0)
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert body == {"data": [], "has_more": False}


async def test_since_and_before_together_are_refused(client, db) -> None:
    tenant = await _seed(db, 3)
    resp = await client.get(
        _url(),
        params={
            "tenant_id": str(tenant.id),
            "since": BASE.isoformat(),
            "before": BASE.isoformat(),
        },
        headers=KEY,
    )
    assert resp.status_code == 422


async def test_since_branch_keeps_its_old_behaviour(client, db) -> None:
    tenant = await _seed(db, 5)
    cursor = (BASE + timedelta(minutes=2)).isoformat()
    body = (
        await client.get(_url(), params={"tenant_id": str(tenant.id), "since": cursor}, headers=KEY)
    ).json()
    assert [m["body"] for m in body["data"]] == ["m3", "m4"]
    assert body["has_more"] is False


async def test_a_whatsapp_patient_with_the_same_string_is_unreachable(client, db) -> None:
    tenant = await _seed(db, 3, channel="whatsapp")
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert body == {"data": [], "has_more": False}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd C:\TECH\BRAIN\secretarIA; $env:BOT_ALLOWLIST_WA_IDS=""; uv run python -m pytest tests/test_brain_message_recent_page.py -q`
Expected: FAIL — `KeyError: 'has_more'` e a primeira carga devolvendo `m0..m49`.

- [ ] **Step 3: Add the schema field**

Em `src/secretaria/schemas/internal.py`, na classe `BrainMessageMessageList`:

```python
class BrainMessageMessageList(BaseModel):
    data: list[BrainMessageMessage]
    # True when older messages exist beyond this page (only meaningful without `since`).
    # Additive and defaulted: callers that never read it keep working.
    has_more: bool = False
```

- [ ] **Step 4: Rewrite the query in the route**

Em `src/secretaria/api/internal.py`:

1. Na assinatura de `list_brain_message_messages`, depois do parâmetro `since`, adicionar:

```python
    before: Annotated[
        datetime | None,
        Query(
            description=(
                "Page backwards: only messages created strictly before this instant. "
                "Only valid without `since`."
            )
        ),
    ] = None,
```

2. No início do corpo da função (antes da busca do `Patient`), adicionar:

```python
    if since is not None and before is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="`since` and `before` cannot be combined.",
        )
```

3. Substituir o bloco que vai de `stmt = select(Message).where(Message.conversation_id == conversation_id)` até o fim do `rows.extend(...)` por:

```python
    stmt = select(Message).where(Message.conversation_id == conversation_id)
    has_more = False
    if since is None:
        # The first load and every Portal poll: the NEWEST `limit` rows, returned oldest
        # first. Ordering ascending with a LIMIT used to hand back the oldest page and
        # freeze any conversation past `limit` messages (LACUNAS_PORTAL L1). `limit + 1`
        # rows are read only to learn whether older ones exist.
        if before is not None:
            stmt = stmt.where(Message.created_at < before)
        newest_first = list(
            (
                await session.execute(
                    stmt.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        has_more = len(newest_first) > limit
        rows = list(reversed(newest_first[:limit]))
    else:
        # The poll: everything that CHANGED since the cursor - new rows and rows whose
        # status moved (`updated_at`, bumped by every write). Ordered by the cursor column.
        stmt = stmt.where(Message.updated_at > since).order_by(Message.updated_at, Message.id)
        rows = list((await session.execute(stmt.limit(limit))).scalars().all())
        if len(rows) == limit:
            # A page never ends INSIDE a group of equal `updated_at`: one read mark stamps
            # every row it touches with the same now(), and the caller's next cursor is the
            # last `updated_at` it received - with a strict `>`, the rest of a group cut by
            # `limit` would never be served again. So the page is completed with the rest
            # of the last row's group (it may exceed `limit` by that group).
            # Column-to-column, not the loaded value re-bound: a datetime round-trip need not
            # compare equal to what the database stored.
            seen = {row.id for row in rows}
            last_updated_at = (
                select(Message.updated_at).where(Message.id == rows[-1].id).scalar_subquery()
            )
            tail = await session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.updated_at == last_updated_at,
                )
                .order_by(Message.id)
            )
            rows.extend(row for row in tail.all() if row.id not in seen)
```

4. No `return BrainMessageMessageList(data=[...])` final, trocar para `return BrainMessageMessageList(data=[...], has_more=has_more)` (manter a lista de `BrainMessageMessage(...)` exatamente como está).

- [ ] **Step 5: Run the new and the neighbouring tests**

Run: `$env:BOT_ALLOWLIST_WA_IDS=""; uv run python -m pytest tests/test_brain_message_recent_page.py tests/test_brain_message_pipeline.py tests/test_message_delivery_status.py tests/test_brain_message_attachments.py tests/test_brain_message_interactive_tap.py -q`
Expected: PASS. Se algum teste antigo assumia "primeira carga = mais antigas", ele estava com ≤ 50 linhas e continua igual (a ordem final é crescente).

- [ ] **Step 6: Lint and commit**

```bash
uvx ruff check src/secretaria/api/internal.py src/secretaria/schemas/internal.py tests/test_brain_message_recent_page.py
uvx ruff format src/secretaria/api/internal.py src/secretaria/schemas/internal.py tests/test_brain_message_recent_page.py
git add src/secretaria/api/internal.py src/secretaria/schemas/internal.py tests/test_brain_message_recent_page.py
git commit -m "fix(portal): transcript shows the newest messages, not the oldest 50"
```

---

### Task 2: Contrato documentado

**Files:**
- Modify: `C:\TECH\BRAIN\brain-api\docs\PORTAL_MESSAGING_API.md` (seção do `GET .../threads/{product}/messages`)
- Create: `docs/CHECKPOINT_portal_mensagens_recentes.md` (este repo)

- [ ] **Step 1:** Em `PORTAL_MESSAGING_API.md`, na seção da leitura de mensagens, acrescentar: "O corpo da secretarIA agora traz `has_more` (bool). Sem `since`, `payload.data` são as 50 mais RECENTES em ordem cronológica; o brain-api repassa o corpo sem alterar. `before` (ISO) existe na perna interna para paginar para trás; o Portal ainda não o usa."
- [ ] **Step 2:** Criar o CHECKPOINT (estado, o que mudou, ordem de deploy: só `secretaria_api`; sem migração) e uma linha de ponteiro em `CLAUDE.md` > "Documentação".
- [ ] **Step 3:** Commit `docs(portal): record newest-first transcript contract`.

---

### Task 3: Prova ao vivo (depois do deploy autorizado)

**Files:** nenhum (verificação).

- [ ] **Step 1:** Com autorização do dono, deployar `secretaria_api`. Conferir `GET /build`: `deploy_parity` pode ficar `divergent` (o worker não mudou) — esperado e anotar.
- [ ] **Step 2:** Abrir o Portal com o agent-browser na conta de QA que tem a conversa longa (`mateuscsiqueiraw7d@gmail.com`; o código de acesso é lido na caixa que envia os e-mails). Mandar uma mensagem; esperar a resposta.
- [ ] **Step 3:** Esperado: a resposta nova aparece em até ~10 s; recarregar a página (F5) mantém a mensagem enviada. Registrar prints/saída no CHECKPOINT.
