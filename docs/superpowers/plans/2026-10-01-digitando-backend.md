# "Digitando…" universal — Backend (secretarIA + brain-api) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** O indicador "digitando…" funciona como no WhatsApp, em todas as direções, e vale para qualquer produto presente ou futuro: (1) a automação (secretarIA, PreCheck, qualquer outro) digitando → paciente e clínica veem; (2) a equipe da clínica digitando no console → o paciente vê; (3) o paciente digitando → a clínica vê, mas SOMENTE quando um humano está conduzindo a conversa (com a automação conduzindo, ninguém lê o "paciente digitando", então não se processa nada).

**Architecture:** Um contrato único, independente de produto, dono é o brain-api (`docs/PORTAL_MESSAGING_API.md`): o corpo de leitura de qualquer produto pode trazer `typing` (bool), `typing_by` (`"automation"|"staff"|null`) e `accepts_typing` (bool: "o cliente deve me mandar batimentos de digitação"); e o cliente avisa que está digitando com `POST /patient-access/threads/{product}/typing`. Na secretarIA a marca é uma chave de Redis por conversa e por quem digita (`brain_message:typing:<conversation_id>:<by>`) com validade curta; tudo falha aberto. Produto novo = implementar os mesmos campos na sua listagem e registrar a sua rota de batimento no brain-api (um dicionário); o front não muda.

**Tech Stack:** Python 3.12, FastAPI, arq/Redis, pytest. **Estrutura de workers pós-TASK-023** (`workers/{shared,whatsapp,portal}/` + `orchestrator.py`/`turn_router.py`; `workers/tasks.py` é só fachada): o wrapper do turno é `workers/orchestrator.py::_send_bot_reply`; testes que trocam nomes de worker usam `from tests._patching import workers_ns` + `monkeypatch.setattr(workers_ns, "NOME", valor)` (nunca `setattr(tasks, ...)`).

**Spec:** Decisões do dono (2026-10-01): "tem que ser universal igual WhatsApp, qualquer interação que esteja digitando tem que aparecer; deixa isso fixo; se tiver outro produto, tem que funcionar para ele também"; "se demandar processamento para o paciente digitando, pode optar por não processar quando a secretária estiver ligada (PreCheck também): só a automação mexe na conversa, não precisa dessa interação".

## Global Constraints

- Testes: `$env:BOT_ALLOWLIST_WA_IDS=""; uv run python -m pytest <arquivos> -q`; nunca `ruff format .`; não reformatar `orchestrator.py` inteiro.
- Só o canal `brain_message`. Conversa de WhatsApp: nenhum indicador (não há onde mostrar).
- Falha aberta e silenciosa: toda chamada ao Redis em `try/except`; o indicador nunca derruba, atrasa ou falha um turno ou uma rota.
- Contrato aditivo: campos novos com default (`typing=False`, `typing_by=None`, `accepts_typing=False`).
- "Paciente digitando" só é gravado quando um humano conduz a conversa (`handover_state == HUMAN_ACTIVE`); com a automação conduzindo a rota responde `applied: false` e NÃO escreve no Redis.
- TTL: automação 90 s (um turno pode demorar); humano 6 s (batimento do cliente a cada 3 s).
- Worker + API são serviços separados; este plano toca `workers/` ⇒ deploy dos DOIS. Sem migração. Commit local permitido; push/deploy só com pedido explícito do dono.
- Nenhum texto de paciente em log (só tipo de erro).
- Este plano e `2026-10-01-portal-mensagens-recentes.md` alteram a MESMA rota de listagem (`list_brain_message_messages`): executar o de mensagens recentes primeiro (mesma branch).

## Review Focus

- Turno que lança exceção: a marca da automação é apagada. Worker morto: expira em 90 s.
- Redis fora do ar / `arq_pool` ausente / pool de teste sem `exists`: resposta normal, `typing=false`.
- "Paciente digitando" com a automação conduzindo: não grava nada.
- Clínica A nunca vê a digitação da clínica B; a rota do console é escopada por tenant (404 para conversa alheia); a rota do Portal usa o escopo da SESSÃO (nunca ids do corpo; o corpo é fechado).
- O paciente nunca recebe `typing_by == "patient"` (não vê a si mesmo); o console nunca recebe `"staff"` (a própria digitação de quem lê; ver Pendências).
- Produto sem rota de batimento registrada: `applied: false` sem rede (como `mark_read` faz para PreCheck).
- Batimento é idempotente e barato; sem limitador próprio (mesma decisão da leitura/`mark_read`), o cliente limita a 1 por 3 s.

## File Structure

secretarIA
- Create `src/secretaria/services/typing_indicator.py`
- Modify `src/secretaria/workers/orchestrator.py` (`_send_bot_reply`)
- Modify `src/secretaria/schemas/internal.py` (`BrainMessageMessageList`; novos `BrainMessageTyping`, `TypingAck`)
- Modify `src/secretaria/api/internal.py` (listagem + `POST /internal/brain-message/typing`)
- Modify `src/secretaria/schemas/conversation.py` (`TypingRead`, `TypingBeat`)
- Modify `src/secretaria/api/hub/conversations.py` (`GET`/`POST /{id}/typing`; limpar a marca da equipe ao enviar)
- Create `tests/test_typing_indicator.py`

brain-api
- Modify `src/brain_api/services/message_switchboard.py` (`TYPING_PRODUCTS`, `send_typing`)
- Modify `src/brain_api/api/portal/patient_access.py` (`POST /threads/{product}/typing`) e o arquivo de schemas do portal (`PatientTypingIn`)
- Create `tests/test_patient_typing.py`
- Modify `docs/PORTAL_MESSAGING_API.md` (seção "Typing — qualquer produto")

---

### Task 1: Serviço do indicador (Redis, fail-open, por quem digita)

**Files:**
- Create: `src/secretaria/services/typing_indicator.py`
- Test: `tests/test_typing_indicator.py`

**Interfaces:**
- Produces:
  - `TypingBy = Literal["automation", "staff", "patient"]`
  - `TYPING_TTL_SECONDS = {"automation": 90, "staff": 6, "patient": 6}`
  - `typing_key(conversation_id, by) -> str` → `"brain_message:typing:<id>:<by>"`
  - `async mark_typing(redis, conversation_id, by: TypingBy) -> None`
  - `async clear_typing(redis, conversation_id, by: TypingBy) -> None`
  - `async typing_by(redis, conversation_id, *, viewer: Literal["patient", "staff"]) -> TypingBy | None` — o paciente enxerga `automation` e `staff` (nessa prioridade); a clínica enxerga `automation` e `patient`.
  Todas aceitam `redis=None` e engolem qualquer exceção (log só do tipo do erro).

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_typing_indicator.py`:

```python
"""The "digitando" flag: three typers, two viewers, never a source of errors."""

import os
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from secretaria.services import typing_indicator as ti  # noqa: E402


class _Redis:
    def __init__(self) -> None:
        self.data: dict[str, tuple[str, int | None]] = {}

    async def setex(self, key, ttl, value):  # noqa: ANN001
        self.data[key] = (value, ttl)

    async def delete(self, key):  # noqa: ANN001
        self.data.pop(key, None)

    async def exists(self, key):  # noqa: ANN001
        return 1 if key in self.data else 0


class _BrokenRedis:
    async def setex(self, *a, **k):  # noqa: ANN002, ANN003
        raise ConnectionError("redis down")

    async def delete(self, *a, **k):  # noqa: ANN002, ANN003
        raise ConnectionError("redis down")

    async def exists(self, *a, **k):  # noqa: ANN002, ANN003
        raise ConnectionError("redis down")


async def test_the_patient_sees_the_automation_and_the_staff_but_never_themselves() -> None:
    redis, cid = _Redis(), uuid4()
    assert await ti.typing_by(redis, cid, viewer="patient") is None
    await ti.mark_typing(redis, cid, "patient")
    assert await ti.typing_by(redis, cid, viewer="patient") is None  # not their own typing
    await ti.mark_typing(redis, cid, "staff")
    assert await ti.typing_by(redis, cid, viewer="patient") == "staff"
    await ti.mark_typing(redis, cid, "automation")
    assert await ti.typing_by(redis, cid, viewer="patient") == "automation"  # priority


async def test_the_clinic_sees_the_automation_and_the_patient_never_staff() -> None:
    redis, cid = _Redis(), uuid4()
    await ti.mark_typing(redis, cid, "staff")
    assert await ti.typing_by(redis, cid, viewer="staff") is None
    await ti.mark_typing(redis, cid, "patient")
    assert await ti.typing_by(redis, cid, viewer="staff") == "patient"
    await ti.mark_typing(redis, cid, "automation")
    assert await ti.typing_by(redis, cid, viewer="staff") == "automation"


async def test_each_typer_has_its_own_ttl_and_clear() -> None:
    redis, cid = _Redis(), uuid4()
    await ti.mark_typing(redis, cid, "automation")
    await ti.mark_typing(redis, cid, "staff")
    assert redis.data[ti.typing_key(cid, "automation")][1] == 90
    assert redis.data[ti.typing_key(cid, "staff")][1] == 6
    await ti.clear_typing(redis, cid, "automation")
    assert await ti.typing_by(redis, cid, viewer="patient") == "staff"


async def test_a_missing_redis_is_just_not_typing() -> None:
    cid = uuid4()
    await ti.mark_typing(None, cid, "automation")
    await ti.clear_typing(None, cid, "automation")
    assert await ti.typing_by(None, cid, viewer="patient") is None


async def test_a_broken_redis_never_raises() -> None:
    redis, cid = _BrokenRedis(), uuid4()
    await ti.mark_typing(redis, cid, "patient")
    await ti.clear_typing(redis, cid, "patient")
    assert await ti.typing_by(redis, cid, viewer="staff") is None


async def test_a_pool_without_exists_is_tolerated() -> None:
    """The test double used by older suites only has `enqueue_job`."""

    class _OnlyEnqueue:
        async def enqueue_job(self, *a, **k):  # noqa: ANN002, ANN003
            return None

    assert await ti.typing_by(_OnlyEnqueue(), uuid4(), viewer="patient") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `$env:BOT_ALLOWLIST_WA_IDS=""; uv run python -m pytest tests/test_typing_indicator.py -q`
Expected: FAIL — `ModuleNotFoundError: secretaria.services.typing_indicator`.

- [ ] **Step 3: Implement**

Criar `src/secretaria/services/typing_indicator.py`:

```python
"""The "digitando…" flag, the way WhatsApp has it: whoever is typing, the other side sees it.

Three typers: the clinic's AUTOMATION (a turn is running - seconds to most of a minute), the
clinic's STAFF (a person typing in the console) and the PATIENT. Each is one Redis key per
conversation with a short TTL - ephemeral by nature, and a dead worker or a closed tab simply
expires instead of leaving "digitando" stuck.

Who sees whom: the patient sees the automation and the staff; the clinic sees the automation
and the patient. Nobody sees their own typing.

The patient's typing is only worth recording while a HUMAN conducts the conversation (the
callers check the handover state): with the automation conducting, no one reads it, so no
work is done for it.

EVERYTHING here fails OPEN and quiet. A flag that can break a turn or a request is worse
than no flag: Redis missing, erroring, or a test double without `exists` all read as "not
typing". Only the exception TYPE is logged - never a conversation's content.
"""

from __future__ import annotations

from typing import Literal

from secretaria.core.logging import get_logger

logger = get_logger(__name__)

TypingBy = Literal["automation", "staff", "patient"]
Viewer = Literal["patient", "staff"]

# Automation: longer than a normal turn (the whole-turn budget is LLM_TURN_TIMEOUT_SECONDS).
# Humans: the client re-sends every ~3 s while a key is being typed, so 6 s rides out one lost beat.
TYPING_TTL_SECONDS: dict[str, int] = {"automation": 90, "staff": 6, "patient": 6}

# What each viewer can see, in priority order.
_VISIBLE_TO: dict[str, tuple[TypingBy, ...]] = {
    "patient": ("automation", "staff"),
    "staff": ("automation", "patient"),
}


def typing_key(conversation_id, by: str) -> str:
    return f"brain_message:typing:{conversation_id}:{by}"


async def mark_typing(redis, conversation_id, by: TypingBy) -> None:
    if redis is None:
        return
    try:
        await redis.setex(typing_key(conversation_id, by), TYPING_TTL_SECONDS[by], "1")
    except Exception as exc:
        logger.warning(
            "typing_indicator_redis_failed", op="mark", by=by, error_type=type(exc).__name__
        )


async def clear_typing(redis, conversation_id, by: TypingBy) -> None:
    if redis is None:
        return
    try:
        await redis.delete(typing_key(conversation_id, by))
    except Exception as exc:
        logger.warning(
            "typing_indicator_redis_failed", op="clear", by=by, error_type=type(exc).__name__
        )


async def typing_by(redis, conversation_id, *, viewer: Viewer) -> TypingBy | None:
    """Who the `viewer` should see typing right now, or None."""
    if redis is None:
        return None
    for by in _VISIBLE_TO[viewer]:
        try:
            if await redis.exists(typing_key(conversation_id, by)):
                return by
        except Exception as exc:
            logger.warning(
                "typing_indicator_redis_failed", op="read", by=by, error_type=type(exc).__name__
            )
            return None
    return None
```

- [ ] **Step 4: Run to verify it passes**

Run: `$env:BOT_ALLOWLIST_WA_IDS=""; uv run python -m pytest tests/test_typing_indicator.py -q`
Expected: PASS (6 testes).

- [ ] **Step 5: Lint and commit**

```bash
uvx ruff check src/secretaria/services/typing_indicator.py tests/test_typing_indicator.py
uvx ruff format src/secretaria/services/typing_indicator.py tests/test_typing_indicator.py
git add src/secretaria/services/typing_indicator.py tests/test_typing_indicator.py
git commit -m "feat(portal): universal typing indicator flag service"
```

---

### Task 2: A automação liga e desliga a marca (worker)

**Files:**
- Modify: `src/secretaria/workers/orchestrator.py` (`_send_bot_reply`, ~linha 235; import do serviço)
- Test: `tests/test_typing_indicator.py` (acrescentar)

**Interfaces:**
- Consumes: `mark_typing`, `clear_typing` (Task 1); `CHANNEL_BRAIN_MESSAGE`, `begin_turn`, `end_turn` já disponíveis no módulo (ver os imports do topo do arquivo; `CHANNEL_BRAIN_MESSAGE` vem de `secretaria.services.channel_sender`).
- Produces: durante `_send_bot_reply_inner` de um turno `brain_message` com `conversation_id`, `typing_by(redis, cid, viewer="patient") == "automation"`; ao terminar (ou levantar), `None`.

- [ ] **Step 1: Write the failing tests**

Acrescentar a `tests/test_typing_indicator.py`:

```python
from tests._patching import workers_ns  # noqa: E402

from secretaria.services.turn_safety_net import note_send  # noqa: E402
from secretaria.workers import orchestrator, tasks  # noqa: E402


def _reply(channel: str):
    return tasks._ReplyContext(
        channel=channel,
        conversation_id=uuid4(),
        patient_ref="ref",
        inbound_body="oi",
        tenant_id=uuid4(),
    )


async def test_automation_flag_is_on_during_a_portal_turn_and_off_after(monkeypatch) -> None:
    redis = _Redis()
    reply = _reply("brain_message")
    seen: list[str | None] = []

    async def _inner(r, redis=None):  # noqa: ANN001
        seen.append(await ti.typing_by(redis, r.conversation_id, viewer="patient"))
        note_send()

    monkeypatch.setattr(workers_ns, "_send_bot_reply_inner", _inner)
    await orchestrator._send_bot_reply(reply, redis=redis)
    assert seen == ["automation"]
    assert await ti.typing_by(redis, reply.conversation_id, viewer="patient") is None


async def test_the_flag_is_cleared_even_when_the_turn_raises(monkeypatch) -> None:
    redis = _Redis()
    reply = _reply("brain_message")

    async def _boom(r, redis=None):  # noqa: ANN001
        raise RuntimeError("downstream exploded")

    monkeypatch.setattr(workers_ns, "_send_bot_reply_inner", _boom)
    await orchestrator._send_bot_reply(reply, redis=redis)  # the net absorbs it
    assert await ti.typing_by(redis, reply.conversation_id, viewer="patient") is None


async def test_a_whatsapp_turn_never_sets_the_flag(monkeypatch) -> None:
    redis = _Redis()
    reply = _reply("whatsapp")
    seen: list[str | None] = []

    async def _inner(r, redis=None):  # noqa: ANN001
        seen.append(await ti.typing_by(redis, r.conversation_id, viewer="patient"))
        note_send()

    monkeypatch.setattr(workers_ns, "_send_bot_reply_inner", _inner)
    await orchestrator._send_bot_reply(reply, redis=redis)
    assert seen == [None]
```

(Se `tasks._ReplyContext` não for reexportado pela fachada, importar de onde `orchestrator.py` o importa: `grep -n "_ReplyContext" src/secretaria/workers/orchestrator.py`.)

- [ ] **Step 2: Run to verify it fails**

Run: `$env:BOT_ALLOWLIST_WA_IDS=""; uv run python -m pytest tests/test_typing_indicator.py -q`
Expected: FAIL — `seen == [None]` no turno do Portal.

- [ ] **Step 3: Implement**

Em `orchestrator.py`, importar `from secretaria.services.typing_indicator import clear_typing, mark_typing` (bloco `secretaria.services.*`, ordem alfabética) e, em `_send_bot_reply`, trocar o bloco que começa em `token = begin_turn()` até `end_turn(token)` por:

```python
    token = begin_turn()
    typing_on = reply.channel == CHANNEL_BRAIN_MESSAGE and reply.conversation_id is not None
    if typing_on:
        await mark_typing(redis, reply.conversation_id, "automation")
    cause = "silent_return"
    try:
        try:
            await _send_bot_reply_inner(reply, redis=redis)
        except Exception as exc:
            cause = "exception"
            logger.error(
                "turn_failed",
                error_type=type(exc).__name__,
                conversation_id=str(reply.conversation_id),
                exc_info=True,
            )
        sent = sends_in_turn()
    finally:
        end_turn(token)
        if typing_on:
            await clear_typing(redis, reply.conversation_id, "automation")
```

O restante (`if sent > 0 or reply.conversation_id is None: return`, `turn_unanswered`, `_send_turn_fallback`) não muda.

- [ ] **Step 4: Run to verify it passes, with the neighbours**

Run: `$env:BOT_ALLOWLIST_WA_IDS=""; uv run python -m pytest tests/test_typing_indicator.py tests/test_turn_safety_net.py tests/test_brain_message_open.py tests/test_brain_message_pipeline.py tests/test_workers_layering.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uvx ruff check src/secretaria/workers/orchestrator.py tests/test_typing_indicator.py
git add src/secretaria/workers/orchestrator.py tests/test_typing_indicator.py
git commit -m "feat(portal): worker raises and clears the automation typing flag"
```

---

### Task 3: A listagem do Portal informa `typing`, `typing_by`, `accepts_typing`

**Files:**
- Modify: `src/secretaria/schemas/internal.py` (`BrainMessageMessageList`)
- Modify: `src/secretaria/api/internal.py` (`list_brain_message_messages`)
- Test: `tests/test_typing_indicator.py` (acrescentar)

**Interfaces:**
- Produces: `GET .../conversations/{external_id}/messages` → `{"data": [...], "has_more": bool, "typing": bool, "typing_by": "automation"|"staff"|null, "accepts_typing": bool}`. `typing_by = typing_by(pool, conversation_id, viewer="patient")`; `typing = typing_by is not None`; `accepts_typing = (conversation.handover_state == HUMAN_ACTIVE)`. Todo retorno antecipado: `typing=false`, `typing_by=null`, `accepts_typing=false`.

- [ ] **Step 1: Write the failing tests**

Copiar para o arquivo as definições `db`, `client`, `_seed`, `_url`, `KEY`, `EXTERNAL_ID`, `BASE` de `tests/test_brain_message_recent_page.py` e acrescentar:

```python
from sqlalchemy import select, update  # noqa: E402

from secretaria.models import Conversation, HandoverState  # noqa: E402


async def _conversation_id(db):
    async with db() as session:
        return await session.scalar(select(Conversation.id))


async def test_the_listing_reports_who_is_typing(client, db) -> None:
    from secretaria.main import app

    tenant = await _seed(db, 2)
    pool = _Redis()
    app.state.arq_pool = pool
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert (body["typing"], body["typing_by"]) == (False, None)

    cid = await _conversation_id(db)
    await ti.mark_typing(pool, cid, "staff")
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert (body["typing"], body["typing_by"]) == (True, "staff")

    await ti.mark_typing(pool, cid, "automation")
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert body["typing_by"] == "automation"


async def test_the_patient_never_sees_their_own_typing(client, db) -> None:
    from secretaria.main import app

    tenant = await _seed(db, 1)
    pool = _Redis()
    app.state.arq_pool = pool
    await ti.mark_typing(pool, await _conversation_id(db), "patient")
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert (body["typing"], body["typing_by"]) == (False, None)


async def test_accepts_typing_only_while_a_human_conducts(client, db) -> None:
    tenant = await _seed(db, 1)
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert body["accepts_typing"] is False  # automation conducting

    async with db() as session:
        await session.execute(update(Conversation).values(handover_state=HandoverState.HUMAN_ACTIVE))
        await session.commit()
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert body["accepts_typing"] is True


async def test_the_listing_survives_a_pool_without_redis_methods(client, db) -> None:
    from secretaria.main import app

    class _OnlyEnqueue:
        async def enqueue_job(self, *a, **k):  # noqa: ANN002, ANN003
            return None

    tenant = await _seed(db, 1)
    app.state.arq_pool = _OnlyEnqueue()
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert (body["typing"], body["typing_by"]) == (False, None)
```

(`HandoverState` é exportado por `secretaria.models`? Confirmar com `grep -n "HandoverState" src/secretaria/models/__init__.py src/secretaria/services/handover.py` e ajustar o import do teste e do código.)

- [ ] **Step 2: Run to verify it fails**

Run: `$env:BOT_ALLOWLIST_WA_IDS=""; uv run python -m pytest tests/test_typing_indicator.py -q`
Expected: FAIL — `KeyError: 'typing'`.

- [ ] **Step 3: Implement**

`schemas/internal.py`, em `BrainMessageMessageList` (depois de `has_more`):

```python
    # The typing contract (brain-api docs/PORTAL_MESSAGING_API.md, "Typing - any product"): all
    # three are additive and defaulted, so an older reader keeps working.
    typing: bool = False
    # Who the READER should see typing: the clinic's automation or its staff. Never the reader.
    typing_by: Literal["automation", "staff"] | None = None
    # True when the client should send typing heartbeats (a human conducts this conversation).
    accepts_typing: bool = False
```

(`Literal` precisa estar importado nesse arquivo. Se um campo chamado `typing` conflitar com `from typing import ...` dentro do corpo da classe pydantic, trocar o import por `import typing as _t` e anotar `_t.Literal[...]`; o campo continua se chamando `typing` no JSON.)

`api/internal.py`: importar `from secretaria.services.typing_indicator import typing_by as typing_by_for` e `HandoverState`. Em `list_brain_message_messages`:

1. adicionar `request: Request` à assinatura (antes de `session`);
2. trocar a busca da conversa por:

```python
    found = (
        await session.execute(
            select(Conversation.id, Conversation.handover_state).where(
                Conversation.tenant_id == tenant_id,
                Conversation.patient_id == patient.id,
            )
        )
    ).one_or_none()
    if found is None:
        return BrainMessageMessageList(data=[])
    conversation_id, handover_state = found
```

3. no `return` final:

```python
    pool = getattr(request.app.state, "arq_pool", None)
    typer = await typing_by_for(pool, conversation_id, viewer="patient")
    return BrainMessageMessageList(
        data=[...],  # lista existente, inalterada
        has_more=has_more,
        typing=typer is not None,
        typing_by=typer,
        accepts_typing=handover_state == HandoverState.HUMAN_ACTIVE,
    )
```

- [ ] **Step 4: Run tests and neighbours**

Run: `$env:BOT_ALLOWLIST_WA_IDS=""; uv run python -m pytest tests/test_typing_indicator.py tests/test_brain_message_recent_page.py tests/test_brain_message_pipeline.py tests/test_message_delivery_status.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uvx ruff check src/secretaria/api/internal.py src/secretaria/schemas/internal.py
git add src/secretaria/api/internal.py src/secretaria/schemas/internal.py tests/test_typing_indicator.py
git commit -m "feat(portal): transcript listing reports typing, typing_by and accepts_typing"
```

---

### Task 4: O paciente digitando — só quando um humano conduz

**Files:**
- Modify: `src/secretaria/schemas/internal.py` (`BrainMessageTyping`, `TypingAck`)
- Modify: `src/secretaria/api/internal.py` (rota nova, perto de `mark_brain_message_read`)
- Test: `tests/test_typing_indicator.py` (acrescentar)

**Interfaces:**
- Produces: `POST /internal/brain-message/typing` body `{"tenant_id": UUID, "external_id": str(1..64)}` (fechado, `extra="forbid"`) → `{"applied": bool}`. `applied=false` e NADA gravado quando: paciente/conversa desconhecidos, ou a automação conduz (`handover_state != HUMAN_ACTIVE`). Só com humano conduzindo: `mark_typing(pool, conversation_id, "patient")` e `applied=true`. Escopo de três elos (tenant, canal `brain_message`, external_id). Exige `X-Internal-Api-Key`.

- [ ] **Step 1: Write the failing tests**

```python
TYPING_URL = "/internal/brain-message/typing"


async def test_patient_typing_is_not_processed_while_the_automation_conducts(client, db) -> None:
    from secretaria.main import app

    tenant = await _seed(db, 1)
    pool = _Redis()
    app.state.arq_pool = pool
    body = {"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID}
    resp = await client.post(TYPING_URL, json=body, headers=KEY)
    assert resp.status_code == 200
    assert resp.json() == {"applied": False}
    assert pool.data == {}  # nothing written: nobody would read it


async def test_patient_typing_is_recorded_when_a_human_conducts(client, db) -> None:
    from secretaria.main import app

    tenant = await _seed(db, 1)
    pool = _Redis()
    app.state.arq_pool = pool
    async with db() as session:
        await session.execute(update(Conversation).values(handover_state=HandoverState.HUMAN_ACTIVE))
        await session.commit()
    resp = await client.post(
        TYPING_URL, json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID}, headers=KEY
    )
    assert resp.json() == {"applied": True}
    assert await ti.typing_by(pool, await _conversation_id(db), viewer="staff") == "patient"


async def test_patient_typing_for_an_unknown_patient_is_a_quiet_no(client, db) -> None:
    tenant = await _seed(db, 1)
    resp = await client.post(
        TYPING_URL, json={"tenant_id": str(tenant.id), "external_id": "nobody"}, headers=KEY
    )
    assert resp.status_code == 200
    assert resp.json() == {"applied": False}


async def test_patient_typing_requires_the_internal_key_and_a_closed_body(client, db) -> None:
    tenant = await _seed(db, 1)
    body = {"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID}
    assert (await client.post(TYPING_URL, json=body)).status_code in (401, 403)
    assert (await client.post(TYPING_URL, json={**body, "by": "staff"}, headers=KEY)).status_code == 422
```

- [ ] **Step 2: Run to verify it fails** — 404 na rota.

- [ ] **Step 3: Implement**

`schemas/internal.py`:

```python
class BrainMessageTyping(BaseModel):
    """The PATIENT is typing (a heartbeat). Scope is the session's; nothing else is accepted."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    external_id: str = Field(min_length=1, max_length=64)


class TypingAck(BaseModel):
    applied: bool
```

`api/internal.py` (importar os dois schemas, `mark_typing`, `HandoverState`):

```python
@router.post(
    "/brain-message/typing",
    response_model=TypingAck,
    summary="The patient is typing - recorded only while a human conducts (internal)",
    description=(
        "A heartbeat from the patient's client. Recorded ONLY when a human conducts the "
        "conversation (the clinic's console is the only reader); with the automation "
        "conducting nothing is written and `applied` is false. Unknown patient: `applied: "
        "false`, never 404. Requires the X-Internal-Api-Key header."
    ),
    responses=_INTERNAL_RESPONSES,
)
async def patient_typing(
    payload: BrainMessageTyping,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TypingAck:
    found = (
        await session.execute(
            select(Conversation.id, Conversation.handover_state)
            .join(Patient, Patient.id == Conversation.patient_id)
            .where(
                Conversation.tenant_id == payload.tenant_id,
                Patient.tenant_id == payload.tenant_id,
                Patient.channel == CHANNEL_BRAIN_MESSAGE,
                Patient.external_id == payload.external_id,
            )
        )
    ).one_or_none()
    if found is None:
        return TypingAck(applied=False)
    conversation_id, handover_state = found
    if handover_state != HandoverState.HUMAN_ACTIVE:
        return TypingAck(applied=False)
    await mark_typing(getattr(request.app.state, "arq_pool", None), conversation_id, "patient")
    return TypingAck(applied=True)
```

- [ ] **Step 4: Run** `$env:BOT_ALLOWLIST_WA_IDS=""; uv run python -m pytest tests/test_typing_indicator.py tests/test_brain_message_pipeline.py -q` — Expected: PASS.

- [ ] **Step 5: Commit** `git add -u; git commit -m "feat(portal): record the patient typing only while a human conducts"`.

---

### Task 5: Console da clínica — ler e emitir

**Files:**
- Modify: `src/secretaria/schemas/conversation.py` (`TypingRead`, `TypingBeat`)
- Modify: `src/secretaria/api/hub/conversations.py` (duas rotas depois de `list_messages`, ~linha 327; limpar a marca da equipe em `send_message`; atualizar o docstring do módulo)
- Test: `tests/test_hub_conversations.py` (acrescentar)

**Interfaces:**
- Produces:
  - `GET /tenants/me/conversations/{conversation_id}/typing` → `{"typing": bool, "by": "automation"|"patient"|null}` (`viewer="staff"`).
  - `POST /tenants/me/conversations/{conversation_id}/typing` → `{"applied": bool}`: grava `staff` só se o paciente é do canal `brain_message`; WhatsApp ⇒ `applied: false`.
  - `send_message` apaga a marca `staff` da conversa depois de gravar a mensagem.
  Todas autenticadas como o resto do hub (`get_current_tenant`); 404 para conversa de outro tenant (`_get_conversation(session, tenant, conversation_id: str)`).

- [ ] **Step 1: Write the failing tests** (ler `tests/test_hub_conversations.py`; reutilizar o cliente autenticado e os helpers de seed; `pool` = o `_Redis` de `test_typing_indicator.py` instalado em `app.state.arq_pool`)

```python
async def test_hub_reads_who_is_typing(hub_client, my_conversation, pool) -> None:
    url = f"/tenants/me/conversations/{my_conversation.id}/typing"
    assert (await hub_client.get(url)).json() == {"typing": False, "by": None}
    await ti.mark_typing(pool, my_conversation.id, "patient")
    assert (await hub_client.get(url)).json() == {"typing": True, "by": "patient"}
    await ti.mark_typing(pool, my_conversation.id, "automation")
    assert (await hub_client.get(url)).json()["by"] == "automation"


async def test_hub_never_shows_staff_their_own_typing(hub_client, my_conversation, pool) -> None:
    await ti.mark_typing(pool, my_conversation.id, "staff")
    resp = await hub_client.get(f"/tenants/me/conversations/{my_conversation.id}/typing")
    assert resp.json() == {"typing": False, "by": None}


async def test_staff_typing_beat_is_recorded_for_a_portal_patient(hub_client, my_portal_conversation, pool) -> None:
    resp = await hub_client.post(f"/tenants/me/conversations/{my_portal_conversation.id}/typing")
    assert resp.json() == {"applied": True}
    assert await ti.typing_by(pool, my_portal_conversation.id, viewer="patient") == "staff"


async def test_staff_typing_beat_for_a_whatsapp_patient_is_a_quiet_no(hub_client, my_whatsapp_conversation, pool) -> None:
    resp = await hub_client.post(f"/tenants/me/conversations/{my_whatsapp_conversation.id}/typing")
    assert resp.json() == {"applied": False}
    assert pool.data == {}


async def test_typing_of_another_tenants_conversation_is_404(hub_client, other_conversation) -> None:
    assert (await hub_client.get(f"/tenants/me/conversations/{other_conversation.id}/typing")).status_code == 404
    assert (await hub_client.post(f"/tenants/me/conversations/{other_conversation.id}/typing")).status_code == 404
```

(Os nomes de fixture são locais: o engenheiro os cria no topo do arquivo de teste a partir das fixtures/seeds que ele já tem — cliente autenticado do tenant atuante, uma conversa `brain_message`, uma `whatsapp`, uma de outro tenant, e o pool falso. O comportamento exigido é o dos cinco testes.)

- [ ] **Step 2: Run to verify it fails** — 404/405 nas rotas.

- [ ] **Step 3: Implement**

`schemas/conversation.py`:

```python
class TypingRead(BaseModel):
    """Who the clinic's console should see typing right now."""

    typing: bool
    by: Literal["automation", "patient"] | None = None


class TypingBeat(BaseModel):
    applied: bool
```

`api/hub/conversations.py` (importar `TypingRead`, `TypingBeat`, `typing_by`, `mark_typing`, `clear_typing`, `CHANNEL_BRAIN_MESSAGE`):

```python
@router.get("/{conversation_id}/typing", response_model=TypingRead)
async def get_typing(
    conversation_id: str,
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> TypingRead:
    """A cheap poll beside the transcript (which is a bare list, with no room for a flag)."""
    conversation = await _get_conversation(session, tenant, conversation_id)
    pool = getattr(request.app.state, "arq_pool", None)
    by = await typing_by(pool, conversation.id, viewer="staff")
    return TypingRead(typing=by is not None, by=by)


@router.post("/{conversation_id}/typing", response_model=TypingBeat)
async def staff_typing(
    conversation_id: str,
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> TypingBeat:
    """A heartbeat while a person types in the console: the Portal patient sees "digitando".

    Only a Brain-Message patient has a screen to show it on; a WhatsApp conversation answers
    `applied: false` and nothing is written.
    """
    conversation = await _get_conversation(session, tenant, conversation_id)
    patient = await session.get(Patient, conversation.patient_id)
    if patient is None or patient.channel != CHANNEL_BRAIN_MESSAGE:
        return TypingBeat(applied=False)
    await mark_typing(getattr(request.app.state, "arq_pool", None), conversation.id, "staff")
    return TypingBeat(applied=True)
```

Em `send_message`, logo depois de a mensagem ser gravada e comitada com sucesso: `await clear_typing(getattr(request.app.state, "arq_pool", None), conversation.id, "staff")` (a função já recebe `request: Request` para ler o corpo; se não receber, adicionar o parâmetro).

- [ ] **Step 4: Run** `$env:BOT_ALLOWLIST_WA_IDS=""; uv run python -m pytest tests/test_hub_conversations.py tests/test_typing_indicator.py -q` — Expected: PASS.

- [ ] **Step 5: Commit** `git add -u; git commit -m "feat(console): typing read and heartbeat routes"`.

---

### Task 6: brain-api — batimento do paciente e contrato "qualquer produto"

**Files:**
- Modify: `src/brain_api/services/message_switchboard.py` (junto de `mark_read`, ~linha 518)
- Modify: `src/brain_api/api/portal/patient_access.py` (rota nova depois de `mark_thread_read`, ~linha 1343) e `src/brain_api/schemas/portal/patient_access.py` (`PatientTypingIn`)
- Create: `tests/test_patient_typing.py`
- Modify: `docs/PORTAL_MESSAGING_API.md`

**Interfaces:**
- Produces:
  - `TYPING_PRODUCTS: dict[str, str]` — produto → caminho interno do batimento; hoje `{PRODUCT_SECRETARIA: "/internal/brain-message/typing"}`. **Produto novo = uma linha aqui.**
  - `async def send_typing(product: str, *, tenant_id: UUID, patient_ref: str) -> dict` → relay de `{"applied": bool}`; produto fora do dicionário (PreCheck hoje) ⇒ `{"applied": False}` SEM rede.
  - `POST /patient-access/threads/{product}/typing` (`PatientTypingIn` fechado) → `RelayOut` com `payload={"applied": bool}`. Escopo pela SESSÃO (`patient.tenant_id`, `patient.id`), `require_product` antes de qualquer rede, `await session.close()` antes do salto.

- [ ] **Step 1: Write the failing tests**

Criar `tests/test_patient_typing.py` (copiar `_configure_mesh`, `_spy_transport`, `_login`, `_bearer` de `tests/test_patient_access.py`, mesmos nomes):

```python
async def test_the_patient_beat_is_relayed_with_the_sessions_scope(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, payload={"applied": True})
    body = (await _login(client, sessionmaker, seed.both)).json()

    resp = await client.post(
        "/patient-access/threads/secretaria/typing", headers=_bearer(body["access_token"])
    )
    assert resp.status_code == 200
    assert resp.json()["payload"] == {"applied": True}
    assert calls[0]["url"] == "/internal/brain-message/typing"
    assert calls[0]["json"] == {"tenant_id": str(seed.both), "external_id": body["patient_ref"]}


async def test_a_product_without_a_typing_route_answers_without_the_network(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, payload={"applied": True})
    body = (await _login(client, sessionmaker, seed.both)).json()

    resp = await client.post(
        "/patient-access/threads/precheck/typing", headers=_bearer(body["access_token"])
    )
    assert resp.status_code == 200
    assert resp.json()["payload"] == {"applied": False}
    assert calls == []


async def test_the_beat_refuses_ids_in_the_body_and_needs_a_session(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    _spy_transport(monkeypatch, payload={"applied": True})
    body = (await _login(client, sessionmaker, seed.both)).json()
    headers = _bearer(body["access_token"])
    resp = await client.post(
        "/patient-access/threads/secretaria/typing", json={"tenant_id": str(seed.both)}, headers=headers
    )
    assert resp.status_code == 422
    assert (await client.post("/patient-access/threads/secretaria/typing")).status_code == 401


async def test_poll_relays_the_typing_fields_untouched(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    _spy_transport(
        monkeypatch,
        payload={"data": [], "has_more": False, "typing": True, "typing_by": "staff", "accepts_typing": True},
    )
    body = (await _login(client, sessionmaker, seed.both)).json()
    resp = await client.get(
        "/patient-access/threads/secretaria/messages", headers=_bearer(body["access_token"])
    )
    payload = resp.json()["payload"]
    assert (payload["typing"], payload["typing_by"], payload["accepts_typing"]) == (True, "staff", True)
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_patient_typing.py -q`; Expected: FAIL (404/405; o último teste já passa se o relay não filtra — se falhar, deixar os 3 campos passarem em `list_messages`).

- [ ] **Step 3: Implement the switchboard**

Em `message_switchboard.py`, depois de `mark_read`:

```python
# --- Typing (2026-10-01): the patient's "I am typing" heartbeat ----------------------------

# product -> the product's internal heartbeat route. A product that is conducted ONLY by its
# automation (PreCheck today) has no entry: nobody there reads "the patient is typing", so
# nothing is relayed. A NEW product joins by adding ONE line here and implementing the same
# route + the `typing` fields in its transcript (docs/PORTAL_MESSAGING_API.md, "Typing").
TYPING_PRODUCTS: dict[str, str] = {PRODUCT_SECRETARIA: "/internal/brain-message/typing"}


async def send_typing(product: str, *, tenant_id: UUID, patient_ref: str) -> dict[str, Any]:
    """Relay the patient's typing heartbeat to `product`, or say "not applied" locally.

    Same shape as `mark_read`: a product with no route answers `{"applied": False}` here with
    no network call, so the portal may beat for every thread it shows. The body is built field
    by field (the product's model is `extra="forbid"`) from the SESSION's scope.
    """
    path = TYPING_PRODUCTS.get(product)
    if path is None:
        return {"applied": False}
    body = {"tenant_id": str(tenant_id), "external_id": patient_ref}
    return await _call(product, "POST", path, json=body)
```

- [ ] **Step 4: Implement the route**

Em `schemas/portal/patient_access.py`, junto de `PatientReadMarkIn`:

```python
class PatientTypingIn(BaseModel):
    """The typing heartbeat carries NOTHING: scope comes from the session, never the body."""

    model_config = ConfigDict(extra="forbid")
```

Em `api/portal/patient_access.py`, depois de `mark_thread_read` (importar `PatientTypingIn`):

```python
@router.post(
    "/threads/{product}/typing",
    response_model=RelayOut,
    summary="The patient is typing (heartbeat)",
    description=(
        "Send every ~3 s while the patient types, and only when the transcript said "
        "`accepts_typing`. Relayed to the product, which records it only while a human "
        "conducts the conversation. `payload.applied` says whether it was recorded."
    ),
    responses={
        401: {"description": "Missing or invalid patient session."},
        403: {"description": "The clinic does not offer that product on this channel."},
        422: {"description": "A body field was sent."},
        502: {"description": "The product backend failed or is misconfigured."},
        503: {"description": "That product's leg of the mesh is unconfigured or degraded."},
    },
)
async def patient_typing(
    product: str = Path(description="secretaria | precheck | any product the clinic offers."),
    payload: PatientTypingIn | None = None,
    patient: MessagePatient = Depends(get_thread_patient),
    session: AsyncSession = Depends(get_session),
) -> RelayOut:
    ent = await resolve_entitlement(session, patient.tenant_id)
    message_switchboard.require_product(ent, product)
    await session.close()  # release the pooled connection before the upstream hop
    result = await message_switchboard.send_typing(
        product, tenant_id=patient.tenant_id, patient_ref=str(patient.id)
    )
    return RelayOut(product=product, payload=result, at=datetime.now(UTC))
```

(Corpo opcional: sem corpo ⇒ 200; corpo com qualquer campo ⇒ 422 por `extra="forbid"` — o teste `…refuses_ids_in_the_body…` cobre os dois lados.)

- [ ] **Step 5: Document the contract**

Em `docs/PORTAL_MESSAGING_API.md`, nova seção "Typing — o contrato de digitação, qualquer produto": campos de leitura `typing`/`typing_by`/`accepts_typing` (significado, quem vê quem, nunca o próprio), o batimento `POST /patient-access/threads/{product}/typing` (a cada ~3 s, só se `accepts_typing`), a regra "paciente digitando só é registrado quando um humano conduz", e o passo-a-passo para um produto novo (1: campos na listagem; 2: rota interna de batimento; 3: uma linha em `TYPING_PRODUCTS`; o front não muda).

- [ ] **Step 6: Run and commit**

Run: `uv run pytest tests/test_patient_typing.py tests/test_patient_access.py tests/test_patient_read_receipts.py -q` — Expected: PASS. Depois:

```bash
make lint
git add src/brain_api tests/test_patient_typing.py docs/PORTAL_MESSAGING_API.md
git commit -m "feat(portal): typing heartbeat relay and the any-product typing contract"
```

---

### Task 7: Checkpoint e prova ao vivo (depois do deploy autorizado)

- [ ] **Step 1:** `docs/CHECKPOINT_digitando_backend.md` (estado, chaves/TTL, rotas novas, regra "paciente digitando só com humano", ordem de deploy: **secretaria_api + secretaria-worker → brain-api**) + ponteiro no `CLAUDE.md` de cada repo.
- [ ] **Step 2:** Prova (agent-browser, visitante `+qa` novo): (a) pergunta livre que acione a LLM ⇒ listagem mostra `typing:true, typing_by:"automation"` durante o turno e `false` depois; (b) com a conversa em modo humano (assumir pelo console), o Portal mostra `accepts_typing:true`, um batimento do paciente vira "paciente digitando" no console e some ≤ 6 s depois; (c) a equipe digitando no console aparece no Portal; (d) com a automação conduzindo, o batimento responde `applied:false` e nada é gravado.
- [ ] **Step 3:** Se o dono autorizar, interromper o worker no meio de um turno de teste e confirmar que a marca da automação expira em ≤ 90 s.

## Pendências (registradas, fora do escopo)

- Console com várias pessoas da equipe: ver "outra pessoa da equipe digitando" exigiria identificar quem digita (a marca `staff` hoje é por conversa).
- PreCheck e produtos futuros: ver `2026-10-01-digitando-precheck.md` (a automação do PreCheck digitando) e a seção "produto novo" do contrato.
