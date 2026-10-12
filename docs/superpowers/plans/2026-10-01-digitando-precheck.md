# "digitando…" no PreCheck (Portal) — Plan (reescrito em 2026-10-08, executável)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A aba PreCheck do Portal também mostra "PreCheck está digitando…" enquanto o PreCheck prepara a próxima resposta.

**Status: pronto para executar, mas SÓ com autorização do dono para tocar o PreCheck** (regra permanente de `PreCheck/CLAUDE.md` e memória `feedback-precheck-whatsapp-producao-intocavel`: avisar ANTES). A mudança fica inteira em `app/services/brain_message/` (`store.py` + um módulo novo), `app/schemas/brain_message.py` e na rota `GET /internal/brain-message/sessions/{session_ref}/messages` — superfície só do Portal, classificada como "seguro sem aviso" no topo do `PreCheck/CLAUDE.md`. Nenhum nó do n8n, nenhum caminho do WhatsApp, nenhuma migração.

## O que mudou desde o plano original (2026-10-01) — por isso este plano foi reescrito

A Task 0 (explorar) foi feita em 2026-10-08. Achados:

1. **O condutor Python NÃO é mais o motor do Portal.** Desde 25/09, `BRAIN_MESSAGE_N8N_CLINICS=*` em produção: o questionário do Portal roda no n8n (workflows só do Portal), e o `PortalRelay` (`app/services/brain_message/n8n_relay.py`) grava a fala do paciente, repassa ao n8n como se fosse um webhook do WhatsApp, e o que o n8n responde volta como uma linha em `brain_message_events`. Marcar "digitando" dentro do `conductor.py` (plano original) **não cobriria nada** em produção. O condutor só atende clínicas fora dessa lista, e ali o turno é síncrono (a resposta volta no mesmo pedido), então não há "digitando" a mostrar.
2. **Não há Redis no PreCheck** e o processo que grava a fala do paciente (`/inbound`) não é necessariamente o que lê (`GET …/messages`) nem o que grava a resposta do n8n (`canal.py`). Um dicionário em memória com TTL (alternativa do plano original) falharia com mais de um processo/réplica.
3. **O lado do brain-api e do front já está pronto:** `typing`, `typing_by`, `accepts_typing` passam intactos pelo relay (`brain-api/docs/CHECKPOINT_digitando_backend.md`); o front já conhece o produto (`lib/typing.ts` → `precheck: "PreCheck"`); `TYPING_PRODUCTS` do brain-api só lista a secretarIA e o PreCheck responde `applied: false` sem rede — decisão do dono, mantida. O que falta é só **a automação do PreCheck digitando**.

## Desenho (sem estado novo)

`typing` é **derivado do que já está no banco**, na hora da leitura:

> `typing = true` e `typing_by = "automation"` quando a **última linha** de `brain_message_events` da sessão é do **paciente** e foi gravada há **menos de 90 s**.

- Seguro com várias réplicas: o estado é o próprio histórico; nada a marcar, nada a limpar, nada que fique preso. A primeira linha do assistente (a resposta do n8n) apaga o indicador sozinha; se o n8n nunca responder, o indicador some em 90 s.
- Sem escrita no GET (a rota continua só lendo).
- `accepts_typing` fica `false` (ninguém humano conduz o PreCheck; decisão do dono).
- Limites aceitos e registrados: (a) a abertura (`/open`, texto sintético "Olá") não grava linha de paciente → sem indicador na abertura; (b) se o n8n mandar várias linhas em sequência, entre elas pode não haver indicador.

**Arquitetura de código:** `store.last_event_summary(v2, session_id, clinic_id)` (um `SELECT role, at … ORDER BY id DESC LIMIT 1` — o `store.py` é o único módulo do canal com SQL e há teste que garante isso) → função pura `automation_is_typing(last_role, last_at, now)` em módulo novo `typing_state.py` (sem SQL; nome escolhido para não sombrear o `typing` da biblioteca padrão) → a rota preenche três campos novos de `BrainMessageTranscriptResponse`.

**Stack:** PreCheck — Python/FastAPI, SQLAlchemy; testes `pytest` (exige `pythonpath = .` no `pytest.ini`), fixtures no estilo de `tests/test_brain_message_transcript_controls.py` (SQLite + shim de JSONB).

## Global Constraints

- NUNCA tocar os caminhos do WhatsApp em produção do PreCheck; só `app/services/brain_message/`, `app/schemas/brain_message.py` e a rota `/internal/brain-message/sessions/*/messages`.
- Mudança aditiva: o corpo atual da resposta não muda; os campos novos têm default (`false`/`null`/`false`) e a suíte existente passa **sem alteração**.
- Falha aberta: qualquer erro ao calcular `typing` devolve `typing=false` e nunca derruba a listagem.
- Sem migração, sem n8n, sem Redis. Push/deploy só com pedido explícito do dono, a cada ocasião. Deploy: só o `precheck-api`; brain-api e front não precisam mudar.
- Nunca copiar lógica de nó do n8n para código (memória `feedback-nunca-imitar-node-n8n-precheck`): aqui nada do n8n é reproduzido — só se lê o histórico.

## Review Focus

- O GET nunca escreve; a consulta é por `session_id` **e** `clinic_id` (nunca `typing=true` para sessão de outra clínica).
- Relógio: `at` pode vir `str` (SQLite) ou `datetime` (psycopg); naive = UTC. Instante no futuro (relógio torto) = sem indicador.
- O ramo WhatsApp e o condutor Python não mudam um byte; o corpo antigo da resposta é idêntico.

---

### Task 0: Autorização e baseline

- [ ] **Step 1:** pedir ao dono, em linguagem simples, a OK para mexer no PreCheck só no canal do Portal ("só a parte do Portal; o WhatsApp não é tocado; o n8n não muda"). Sem OK, parar aqui.
- [ ] **Step 2:** worktree `C:\TECH\BRAIN-worktrees\TASK-<N>\PreCheck` (branch `task/TASK-<N>-precheck-digitando`) a partir de `main` do PreCheck; baseline: `pytest tests/test_brain_message_*.py -q` (anote N). A suíte inteira do PreCheck tem falhas conhecidas no HEAD (memória `precheck-suite-vermelha-no-head`): compare com o HEAD, não com zero.
- [ ] **Step 3:** reconfirmar os fatos acima (podem ter mudado): `grep -n "brain_message_n8n_clinics" app/core/config.py` e `docs/CHECKPOINT_PORTAL_N8N_ADAPTER.md`. Se o Portal tiver voltado ao condutor Python, este plano muda (o turno do condutor é síncrono; nada a mostrar).

### Task 1: Função pura de decisão (TDD)

**Files:** Create `app/services/brain_message/typing_state.py`, `tests/test_brain_message_typing.py`.

- [ ] **Step 1: testes primeiro** (falham por importação):

```python
from datetime import datetime, timedelta, timezone

from app.services.brain_message.typing_state import AUTOMATION_TTL_S, automation_is_typing

NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)


def _ago(seconds: int) -> datetime:
    return NOW - timedelta(seconds=seconds)


def test_patient_line_just_written_means_typing():
    assert automation_is_typing("patient", _ago(2), NOW) is True


def test_an_assistant_line_last_means_not_typing():
    assert automation_is_typing("assistant", _ago(1), NOW) is False


def test_the_indicator_expires_when_the_flow_never_answers():
    assert automation_is_typing("patient", _ago(AUTOMATION_TTL_S + 1), NOW) is False
    assert automation_is_typing("patient", _ago(AUTOMATION_TTL_S - 1), NOW) is True


def test_no_events_and_unreadable_instants_fail_open_to_false():
    assert automation_is_typing(None, None, NOW) is False
    assert automation_is_typing("patient", "not a date", NOW) is False
    assert automation_is_typing("patient", None, NOW) is False


def test_naive_instants_are_read_as_utc_and_text_instants_are_parsed():
    naive = (NOW - timedelta(seconds=5)).replace(tzinfo=None)
    assert automation_is_typing("patient", naive, NOW) is True
    assert automation_is_typing("patient", _ago(5).isoformat(), NOW) is True


def test_a_future_instant_is_not_typing():  # clock skew must not pin the indicator on
    assert automation_is_typing("patient", NOW + timedelta(seconds=30), NOW) is False
```

- [ ] **Step 2: implementação mínima** (`typing_state.py`, sem SQL e sem importar `store`):

```python
"""Is the automation "typing" for this Brain-Message session? Derived, never stored.

The portal PreCheck is driven by n8n: the patient's line is recorded, handed to n8n, and n8n's
answer comes back as one more line in `brain_message_events`. Between the two the automation is
working -- so "the last line is the patient's and it is recent" IS the indicator. No mark to set,
no mark to clear, no process-local state (the writer and the reader of a turn may be different
processes), and nothing can stay stuck: the TTL ends it if n8n never answers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Same bound as the secretarIA automation mark (brain-api PORTAL_MESSAGING_API.md, "Typing").
AUTOMATION_TTL_S = 90


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def automation_is_typing(last_role: str | None, last_at: Any, now: datetime) -> bool:
    if last_role != "patient":
        return False
    at = _as_utc(last_at)
    if at is None:
        return False
    return 0 <= (now - at).total_seconds() < AUTOMATION_TTL_S
```

- [ ] **Step 3:** `pytest tests/test_brain_message_typing.py -q` → verdes; depois `pytest tests/test_brain_message_*.py -q` (o pacote inteiro).
- [ ] **Step 4:** commit (caminhos explícitos, nunca `git add -A`).

### Task 2: Leitura da última linha (única SQL nova)

**Files:** Modify `app/services/brain_message/store.py`; testes no mesmo arquivo de testes (ou irmão) com o fixture de SQLite de `test_brain_message_transcript_controls.py`.

- [ ] **Step 1: teste primeiro** — criar sessão, `append_line(role="patient")` e `append_line(role="assistant")` e provar: `last_event_summary` devolve o papel e o `at` da **última** linha; é por `session_id` **e** `clinic_id` (outra clínica → `None`); sem linhas → `None`.
- [ ] **Step 2:** acrescentar em `store.py`, perto de `has_assistant_line`:

```python
def last_event_summary(v2: Session, session_id: str, clinic_id: int) -> Optional[tuple[str, Any]]:
    """(role, at) of the newest line of one session and clinic, or None. Read-only."""
    raw = v2.execute(
        text("SELECT role, at FROM brain_message_events WHERE session_id = :sid "
             "AND clinic_id = :cid ORDER BY id DESC LIMIT 1"),
        {"sid": session_id, "cid": clinic_id},
    ).first()
    return (raw.role, raw.at) if raw is not None else None
```
- [ ] **Step 3:** o teste que garante "só o `store.py` tem SQL no canal" continua verde: `pytest tests/test_brain_message_*.py -q`. Commit.

### Task 3: Campos no contrato e na rota

**Files:** Modify `app/schemas/brain_message.py` (`BrainMessageTranscriptResponse`), `app/routers/internal.py` (rota `brain_message_messages`); testes.

- [ ] **Step 1: testes primeiro** (TestClient, como em `test_brain_message_transcript_controls.py`): (a) última linha do paciente, `at` recente → `typing is True`, `typing_by == "automation"`, `accepts_typing is False`; (b) última linha do assistente → `typing is False`, `typing_by is None`; (c) sessão inexistente ou de outra clínica → resposta vazia com `typing False` (a listagem vazia atual não muda); (d) o `GET` não escreve (contar linhas antes/depois); (e) os testes existentes passam sem alteração (corpo antigo idêntico); (f) se `store.last_event_summary` lançar exceção (monkeypatch), a listagem sai com `typing=False` e status 200.
- [ ] **Step 2:** `BrainMessageTranscriptResponse` ganha `typing: bool = False`, `typing_by: Optional[Literal["automation"]] = None`, `accepts_typing: bool = False`.
- [ ] **Step 3:** na rota, depois de montar `items` (ramo da sessão existente), calcular com falha aberta e devolver os três campos; os retornos antecipados (sem clínica / sem sessão) ficam como estão (defaults):

```python
typing = False
try:
    last = bm_store.last_event_summary(v2, session.id, clinic.id)
    typing = last is not None and automation_is_typing(
        last[0], last[1], datetime.now(timezone.utc))
except Exception:  # noqa: BLE001 - an indicator must never fail the poll
    logger.warning("internal.brain_message.typing_unavailable", extra={"clinic_id": clinic.id})
# ... BrainMessageTranscriptResponse(..., typing=typing, typing_by="automation" if typing else None)
```
- [ ] **Step 4:** `pytest tests/test_brain_message_*.py -q` sem regressão frente ao baseline do Task 0; lint só nos arquivos tocados (`uvx ruff check <arquivos>`, comparando com o HEAD — o PreCheck não tem config de lint persistida). Commit.

### Task 4: Documentação e prova

- [ ] `brain-api/docs/PORTAL_MESSAGING_API.md`, seção "Typing": registrar que o PreCheck passou a preencher `typing`/`typing_by` (automação) derivando do histórico, `accepts_typing=false`, TTL 90 s, e o limite da abertura. `PreCheck/docs/CHECKPOINT_PORTAL_N8N_ADAPTER.md`: uma linha de ponteiro. `PreCheck/CLAUDE.md` só se a regra local mandar.
- [ ] **Prova em ambiente real (Integrator, depois de deploy AUTORIZADO):** no Portal, na aba PreCheck, enviar uma resposta e ver "PreCheck está digitando…" entre o envio e a próxima pergunta, sumindo quando ela chega; conferir que a aba da secretarIA não mudou. Teste e build sozinhos não provam o visual.
- [ ] Entrega: sem tocar `main` dos checkouts; avisar o dono em linguagem simples ("o Portal passa a mostrar que o PreCheck está respondendo; o WhatsApp não muda"). Deploy só com pedido explícito, só o `precheck-api`.
