# Liberar horário com sinal pago: manter, remarcar ou cancelar (TASK-052) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Quando a clínica libera uma consulta com sinal Pix já pago, o paciente escolhe **Manter horário** / **Remarcar** / **Cancelar**; cancelar (ou não responder até o prazo B) faz a clínica receber e-mails programados (A no toque, B em `min(T+6h, fechamento−30min)`, C em T+24h) para devolver o sinal pelo painel do Asaas, e em T+24h o paciente confere a devolução com **Recebido** / **Não recebido** — tudo idempotente, sem o sistema nunca devolver dinheiro.

**Architecture:** Duas tabelas novas aditivas: `deposit_release_offers` (uma oferta por consulta liberada; cada transição é um `UPDATE ... WHERE state IN (...)` guardado) e `deposit_release_emails` (um envio por linha, `(offer_id, kind, seq)` único, reivindicado antes de enviar). Serviços puros/transacionais em `services/` (`release_deadlines`, `release_offer`, `release_keep`, `release_offer_dispatch`); os toques entram por `workers/shared/actions.py::_handle_action_button` (WhatsApp e Portal) através de `workers/shared/release_offer_actions.py`; o prazo B e os envios rodam na varredura de pagamentos que já existe; a transferência do sinal acontece dentro da transação que cria a consulta nova.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Alembic, arq, pytest (`asyncio_mode = "auto"`, SQLite em memória; Postgres real opcional por `PAYMENTS_TEST_PG_URL`), ruff, SMTP (`services/email.py`). Front: Next.js/TypeScript/vitest (só um texto).

**Spec:** `docs/superpowers/specs/2026-10-11-liberar-horario-com-sinal-pago-design.md` (refeita em 2026-10-12; este worktree). Regras-mãe: D15 (a plataforma nunca devolve), `docs/CHECKPOINT_task050_main_into_out_of_mvp.md`, `tests/test_workers_layering.py`, skill `frozen-contract-migration`.

**Estado de execução (leia antes de começar):**

- **Worktree:** `C:\TECH\BRAIN-worktrees\TASK-052\secretarIA`, branch `task/TASK-052-liberar-sinal-pago`, base `task/TASK-050-secretaria` @ `3650e10` (+ só commits de documentos). Integra em `feature/out-of-mvp` (NUNCA `main`) depois da TASK-050.
- **Este plano foi REESCRITO em 2026-10-12 e NÃO foi aplicado numa cópia descartável** (o plano anterior foi; o desenho mudou). As âncoras foram conferidas lendo o código de `3650e10`; os blocos de código são o conteúdo pretendido. Se um diff não aplicar, aplique pela âncora (nome de função/constante citada), nunca pelo número de linha, e registre no `TASK.md`. Rode cada teste e veja falhar ANTES de implementar.
- **NÃO PROVADO:** envio real pelo WhatsApp (cartão e os dois modelos novos, que nem estão aprovados na Meta), SMTP real, cartão no Portal real, produção.
- **Produção:** NÃO AUTORIZADA. Sem push, sem deploy, sem migração em banco real, sem `.env`, sem Easypanel.

---

## Contratos tocados

| # | Onde | Antes | Depois |
|---|---|---|---|
| 1 | `POST /tenants/me/calendar/appointments/{id}/release` — 409 `retention_ack_required` | `deposit_outcome` da política de retenção; texto de retenção | `deposit_outcome = "choice_offered"` sempre que o sinal está PAGO; texto da escolha (manter / remarcar / cancelar; e-mails de devolução) |
| 2 | o mesmo, 200 | sinal pago podia ficar `cancelado_retido` ou abrir item | sinal continua PAGO; abre uma oferta; `deposit_outcome = "choice_offered"`; nenhum item ainda |
| 3 | job arq `send_cancellation_notice(tenant_id, appointment_id, professional_name, justification, extra_notice, allow_paid)` | 3 botões de remarcação | **mesma assinatura**; com oferta em decisão: `relkeep|`/`relresched|`/`relcancel|` (cartão na janela; modelo `RELEASE_OFFER_TEMPLATE_NAME` fora) e o corpo é o próprio `extra_notice` |
| 4 | ids de botão (`schemas/webhook.py::_ACTION_BUTTON_PREFIXES`) | — | `relkeep|`, `relresched|`, `relcancel|`, `relgot|`, `relmiss|` (id = consulta LIBERADA) |
| 5 | Portal: ids aceitos como ação (`workers/portal/inbound.py`) | família dos lembretes | + os 5 `rel*` (só se um cartão recente da conversa os ofereceu) |
| 6 | `payment_attention_items.reason` | 10 motivos | + `released_refund_due` (registrável como devolvido no painel) |
| 7 | Banco | cabeça `e2b7c4a9d1f5` | + `b6d2e8f4a1c3` (tabelas `deposit_release_offers`, `deposit_release_emails`; aditiva) |
| 8 | `config.py` | — | `RELEASE_OFFER_TEMPLATE_NAME`, `REFUND_CHECK_TEMPLATE_NAME` (PENDÊNCIA META) |
| 9 | E-mails à clínica (`Tenant.contact_email`) e ao paciente (`Patient.email`) | — | 5 tipos à clínica, 2 ao paciente (§5.3, §5.8, §5.9 da spec) |
| 10 | Brain-Message-Frontend `lib/agenda/release.ts` (texto do aviso) | "pode fazer a clínica reter o valor pago" | manter / remarcar / cancelar; e-mails para devolver |

## Global Constraints

- **Dinheiro (D15):** a plataforma NUNCA devolve sozinha e nunca chama o provedor para devolver; "devolver" = UM item `released_refund_due` (valor cheio) por sinal + e-mails à clínica. Nenhuma frase diz que "devolvemos"/"reembolsamos"/"estorno automático".
- **Prazos (spec §5.2):** T = liberação; B = `min(T + 6 h, fechamento − 30 min)` no fuso da clínica (`Tenant.business_hours`, `Tenant.timezone`), e B = T + 6 h quando a clínica não fecha DEPOIS de T naquele dia (fechada, sem horário, ou T ≥ fechamento − 30 min); C = T + 24 h. Sempre T ≤ B < C. Nada é enviado antes de T.
- **Uma oferta por consulta liberada** (UNIQUE `released_appointment_id`); estados `open`, `rescheduling`, `kept`, `rebooked`, `refund_due`, `refund_closed`; toda transição é um `UPDATE` guardado; Manter/Remarcar/Cancelar só valem com `decide_by_at > agora`.
- **Um envio por linha** de `deposit_release_emails` (`(offer_id, kind, seq)` UNIQUE): reivindicado com `UPDATE ... WHERE claimed_at IS NULL` e comitado ANTES de enviar; revalidado no envio; "não saiu" devolve a reivindicação (até 5 tentativas).
- **Isolamento:** a clínica vem da conversa (toque), da consulta (hub) ou da própria linha (varredura); um toque só vale para o paciente da oferta; nada de payload decide a clínica.
- **Textos (pt-BR, exatos, spec §5.3–§5.9):** botões "Manter horário" (14), "Remarcar" (8), "Cancelar" (8), "Recebido" (8), "Não recebido" (12) ≤ 20; corpo interativo ≤ 1024 cortando só o recado da clínica; e-mails sem nome, telefone ou e-mail do paciente.
- **Logs:** ids, `kind`, `outcome`, `error_type`. Nunca e-mail, nome, telefone, link ou corpo.
- **Modelos Meta:** `RELEASE_OFFER_TEMPLATE_NAME = "liberacao_sinal_pago_v1"`, `REFUND_CHECK_TEMPLATE_NAME = "devolucao_sinal_conferir_v1"`, usados como se aprovados (PENDÊNCIA META); parâmetro de modelo sem quebras de linha.
- **Camadas (`tests/test_workers_layering.py`):** `workers/shared/*` não importa `whatsapp/*`, `portal/*` nem `tasks`; regras novas em `services/`.
- **Deploy (NÃO autorizado):** migração aditiva `b6d2e8f4a1c3` → API → worker LOGO depois → front. O job não muda de assinatura.
- **Gates:** `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest <arquivos> -q -p no:cacheprovider`; `uv run ruff check <arquivos tocados>`; **nunca** `ruff format .`. Front: `npx vitest run <arquivo>`, `npm run typecheck` (sem `npm run lint`).
- **Git:** nunca `git add -A`/`git add .`; commits em inglês terminando com `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` e `Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG`.

## Review Focus

1. **Um toque e a varredura do prazo B ganham os dois** — dois itens, sinal transferido E devolvido, ou consulta mantida E devolução pedida. → Task 3: `test_a_tap_that_loses_to_the_sweep_opens_no_second_item`, `test_keep_claim_moves_the_deposit_and_schedules_one_clinic_email`; Task 4: `test_losing_the_claim_deletes_the_event_it_created`; Task 12 (Postgres real): `test_cancel_taps_and_sweeps_open_exactly_one_item`, `test_bookings_racing_the_sweep_carry_or_refund_never_both`.
2. **Um e-mail sai duas vezes** (retry, duas instâncias, toques repetidos). → Task 3: `test_cancelling_twice_or_after_b_changes_nothing`; Task 5: `test_each_scheduled_email_goes_out_once_even_dispatched_twice`, `test_a_send_that_did_not_leave_is_retried_then_given_up`; Task 12: `test_concurrent_dispatchers_send_one_email`.
3. **Um aviso sai depois que o caso acabou** (manteve, remarcou, clínica deu por feita, Recebido). → Task 5: `test_once_the_clinic_settles_nothing_else_goes_out`, `test_a_kept_offer_sends_only_the_keep_email`; Task 3: `test_received_closes_the_story_and_skips_what_is_pending`.
4. **Manter devolve o paciente para um horário já ocupado** (evento no Google, reserva de outro paciente, consulta que a clínica marcou ali). → Task 4: `test_an_event_in_google_means_taken_and_nothing_changes`, `test_another_conversations_hold_means_taken`, `test_a_consultation_the_clinic_booked_there_means_taken`.
5. **O prazo B errado nas bordas do dia** (clínica fechada, depois de fechar, sem horário, fuso). → Task 2: todos os testes de `tests/test_release_deadlines.py`.

---

## File Structure

**Criar (secretarIA):**
- `src/secretaria/models/deposit_release_offer.py` — as duas tabelas e as constantes de estado/tipo/resultado.
- `migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py` — migração aditiva.
- `src/secretaria/services/release_deadlines.py` — prazos B e C (puro).
- `src/secretaria/services/release_offer.py` — textos, botões, transições guardadas, programação dos envios.
- `src/secretaria/services/release_keep.py` — "Manter horário" (calendário + recriação da consulta).
- `src/secretaria/services/release_offer_dispatch.py` — envio idempotente do que foi programado.
- `src/secretaria/workers/shared/release_offer_actions.py` — os toques, neutro de canal.
- Testes: `tests/release_offer_support.py` (ajudante), `tests/test_deposit_release_offer_migration.py`, `tests/test_release_deadlines.py`, `tests/test_release_offer.py`, `tests/test_release_keep.py`, `tests/test_release_offer_dispatch.py`, `tests/test_release_offer_buttons.py`, `tests/test_release_offer_carry_over.py`, `tests/test_release_offer_postgres.py`.
- `docs/CHECKPOINT_task052_liberar_sinal_pago.md`.

**Modificar (secretarIA):** `src/secretaria/models/{__init__,payment_attention_item}.py`; `src/secretaria/config.py`; `src/secretaria/services/email.py`; `src/secretaria/services/payments/attention_view.py`; `src/secretaria/api/hub/calendar.py` (`release_appointment`); `src/secretaria/services/appointment_release.py`; `src/secretaria/workers/whatsapp/notifications.py`; `src/secretaria/schemas/webhook.py`; `src/secretaria/workers/portal/inbound.py`; `src/secretaria/workers/shared/actions.py`; `src/secretaria/services/flow_router.py`; `src/secretaria/services/availability.py`; `src/secretaria/services/booking_hold.py`; `src/secretaria/workers/shared/{flow_runner,booking_hold}.py`; `src/secretaria/workers/payments_cron.py`; testes `tests/{test_deposit_before_migration,test_payment_attention,test_hub_release,test_cancellation_notice,test_availability,test_before_booking_flow,test_payment_cron_jobs}.py`; `docs/LEMBRETES_MODELOS_META.md`; `CLAUDE.md` (1 linha).

**Brain-Message-Frontend (Task 13, worktree próprio):** `lib/agenda/release.ts`, `lib/agenda/__tests__/release.test.ts`.

**Fora dos repositórios:** `C:\TECH\BRAIN\tasks\TASK-052\TASK.md`.

**Nunca modificar:** `.env`, `Dockerfile`, `workers/tasks.py` (fachada), `workers/arq_worker.py` (nenhum cron novo), brain-api, PreCheck.

---

### Task 0: Preparação e linha de base

**Files:**
- Modify (fora do repositório): `C:\TECH\BRAIN\tasks\TASK-052\TASK.md`

**Interfaces:**
- Produces: o ambiente e a linha de base (pré-existente × regressão).

- [ ] **Step 1: Conferir o worktree e as âncoras**

```powershell
Set-Location "C:\TECH\BRAIN-worktrees\TASK-052\secretarIA"
git status --short
git log --oneline -3
git grep -n "async def release_appointment" -- src/secretaria/api/hub/calendar.py
git grep -n "async def send_cancellation_notice" -- src/secretaria/workers/whatsapp/notifications.py
git grep -n "async def _handle_action_button" -- src/secretaria/workers/shared/actions.py
git grep -n "reserved = await _hold_windows" -- src/secretaria/services/flow_router.py
git grep -n 'revision: str = "e2b7c4a9d1f5"' -- migrations/versions
git grep -n "async def run_payment_sweeps" -- src/secretaria/workers/payments_cron.py
git grep -n "_REFUND_RECORDING_REASONS = " -- src/secretaria/services/payments/attention_view.py
```

Expected: `git status` limpo; topo = commits de documentos sobre `3650e10`; cada `git grep` acha a âncora (a de `_hold_windows` acha DUAS: lista de dias e lista de horários).

- [ ] **Step 2: Ambiente e linha de base**

```powershell
uv sync --frozen
$env:BOT_ALLOWLIST_WA_IDS = ""
uv run python -m pytest -q -p no:cacheprovider
```

Expected: 0 failed (medido no plano anterior sobre `3650e10`: **6103 passed, 79 skipped, 1 xfailed**). Anote o número real no `TASK.md` (seção Validation). Uma falha aqui é PRÉ-EXISTENTE: registre e não a conte como regressão.

---

### Task 1: As tabelas `deposit_release_offers` e `deposit_release_emails` (migração aditiva)

**Files:**
- Create: `src/secretaria/models/deposit_release_offer.py`, `migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py`
- Modify: `src/secretaria/models/__init__.py`, `tests/test_deposit_before_migration.py`
- Test: `tests/test_deposit_release_offer_migration.py` (novo)

**Interfaces:**
- Produces: `DepositReleaseOffer` (colunas `id, tenant_id, deposit_id, released_appointment_id (UNIQUE), patient_id, conversation_id, state, released_at, decide_by_at, refund_check_at, paid_notice_allowed, refund_source, refund_due_at, new_appointment_id, closed_at, closed_reason, patient_check_sent_at, last_reminder_at, created_at`); `DepositReleaseEmail` (colunas `id, tenant_id, offer_id, kind, seq, due_at, attempts, claimed_at, outcome, created_at`; UNIQUE `(offer_id, kind, seq)`); constantes `STATE_OPEN="open"`, `STATE_RESCHEDULING="rescheduling"`, `STATE_KEPT="kept"`, `STATE_REBOOKED="rebooked"`, `STATE_REFUND_DUE="refund_due"`, `STATE_REFUND_CLOSED="refund_closed"`, `OFFER_STATES`, `DECIDING_STATES`, `REFUND_SOURCE_PATIENT="patient"`, `REFUND_SOURCE_TIMEOUT="timeout"`, `CLOSED_PATIENT_RECEIVED="patient_received"`, `CLOSED_CLINIC_SETTLED="clinic_settled"`, `EMAIL_REFUND_NOW="clinic_refund_now"`, `EMAIL_REFUND_DEADLINE="clinic_refund_deadline"`, `EMAIL_REFUND_DAY="clinic_refund_day"`, `EMAIL_REFUND_REMINDER="clinic_refund_reminder"`, `EMAIL_PATIENT_KEEPS="clinic_patient_keeps"`, `EMAIL_PATIENT_CHECK="patient_check"`, `EMAIL_KINDS`, `OUTCOME_SENDING="sending"`, `OUTCOME_SENT="sent"`, `OUTCOME_SKIPPED="skipped"`, `OUTCOME_NO_CONTACT="skipped_no_contact"`, `OUTCOME_GIVEN_UP="given_up"`; revisão `b6d2e8f4a1c3` (desce de `e2b7c4a9d1f5`).

- [ ] **Step 1: Escrever o teste que falha**

`tests/test_deposit_release_offer_migration.py`:

```python
"""Migration b6d2e8f4a1c3 - `deposit_release_offers` + `deposit_release_emails` (TASK-052).

Cheapest layers first, like tests/test_deposit_before_migration.py: the chain stays a single
head right after the `before` timing columns; the offline SQL only CREATES (two tables, their
indexes) and its downgrade only drops them; the models declare exactly the same shape.
"""

import contextlib
import io
import pathlib

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from secretaria.core.database import Base

REPO = pathlib.Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "e2b7c4a9d1f5"
REVISION = "b6d2e8f4a1c3"


def _config() -> Config:
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    return cfg


def _offline(direction: str) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        if direction == "up":
            command.upgrade(_config(), f"{PREVIOUS_HEAD}:{REVISION}", sql=True)
        else:
            command.downgrade(_config(), f"{REVISION}:{PREVIOUS_HEAD}", sql=True)
    return out.getvalue()


def test_the_revision_is_the_single_head_right_after_the_before_timing():
    script = ScriptDirectory.from_config(_config())
    assert script.get_heads() == [REVISION]
    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD


def test_upgrade_only_creates_the_two_new_tables():
    sql = _offline("up").upper()
    assert "CREATE TABLE DEPOSIT_RELEASE_OFFERS" in sql
    assert "CREATE TABLE DEPOSIT_RELEASE_EMAILS" in sql
    assert "ALTER TABLE" not in sql  # no existing table is touched
    assert "DROP " not in sql
    assert "UQ_DEPOSIT_RELEASE_OFFERS_RELEASED_APPOINTMENT_ID" in sql
    assert "UQ_DEPOSIT_RELEASE_EMAILS_OFFER_KIND_SEQ" in sql
    assert "IX_DEPOSIT_RELEASE_OFFERS_STATE_DECIDE_BY" in sql
    assert "IX_DEPOSIT_RELEASE_EMAILS_DUE" in sql


def test_downgrade_drops_only_those_tables():
    sql = _offline("down").upper()
    assert "DROP TABLE DEPOSIT_RELEASE_EMAILS" in sql
    assert "DROP TABLE DEPOSIT_RELEASE_OFFERS" in sql
    assert "ALTER TABLE" not in sql


def test_the_models_declare_what_the_migration_creates():
    offers = Base.metadata.tables["deposit_release_offers"]
    assert {c.name for c in offers.columns} == {
        "id",
        "tenant_id",
        "deposit_id",
        "released_appointment_id",
        "patient_id",
        "conversation_id",
        "state",
        "released_at",
        "decide_by_at",
        "refund_check_at",
        "paid_notice_allowed",
        "refund_source",
        "refund_due_at",
        "new_appointment_id",
        "closed_at",
        "closed_reason",
        "patient_check_sent_at",
        "last_reminder_at",
        "created_at",
    }
    for nullable in (
        "patient_id",
        "conversation_id",
        "refund_source",
        "refund_due_at",
        "new_appointment_id",
        "closed_at",
        "closed_reason",
        "patient_check_sent_at",
        "last_reminder_at",
    ):
        assert offers.c[nullable].nullable is True
    fk = {fk.parent.name: fk.ondelete for fk in offers.foreign_keys}
    assert fk["tenant_id"] == "CASCADE" and fk["released_appointment_id"] == "CASCADE"
    assert fk["patient_id"] == "SET NULL" and fk["new_appointment_id"] == "SET NULL"

    mails = Base.metadata.tables["deposit_release_emails"]
    assert {c.name for c in mails.columns} == {
        "id",
        "tenant_id",
        "offer_id",
        "kind",
        "seq",
        "due_at",
        "attempts",
        "claimed_at",
        "outcome",
        "created_at",
    }
    assert {fk.parent.name: fk.ondelete for fk in mails.foreign_keys}["offer_id"] == "CASCADE"
    names = {c.name for c in mails.constraints if c.name}
    assert "uq_deposit_release_emails_offer_kind_seq" in names
```

Em `tests/test_deposit_before_migration.py`, no teste `test_the_revision_chains_after_the_provider_core_and_is_the_single_head`, troque a linha `assert script.get_heads() == [REVISION]` por:

```python
    # TASK-052 chains `b6d2e8f4a1c3` after this one: the newest migration pins the head.
    assert len(script.get_heads()) == 1
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_deposit_release_offer_migration.py -q -p no:cacheprovider`
Expected: FAIL (a revisão `b6d2e8f4a1c3` e as tabelas ainda não existem).

- [ ] **Step 3: Implementar**

`src/secretaria/models/deposit_release_offer.py`:

```python
"""The patient's choice after the clinic frees a PAID slot, and what it schedules (TASK-052).

Owner decision 2026-10-12 (docs/superpowers/specs/2026-10-11-liberar-horario-com-sinal-pago-
design.md). When the clinic releases an appointment whose Pix deposit is already PAID, the
platform never returns money by itself (decision D15) and does not decide for the patient: it
opens ONE `DepositReleaseOffer` and the patient chooses "Manter horário", "Remarcar" or
"Cancelar". Cancelling - or not answering by `decide_by_at` (B) - makes the refund DUE: the
clinic gets e-mails (A at the tap, B, C at `refund_check_at`) and, at C, the patient is asked
whether the money arrived. Each of those sends is one `DepositReleaseEmail` row, claimed before
it is sent, so a retry or a second worker never sends it twice.

`DepositReleaseOffer.state`:
    open           - the offer was sent; nothing chosen yet.
    rescheduling   - "Remarcar" tapped; the next booking of the conversation carries the deposit.
    kept           - final: "Manter horário" re-created the consultation (`new_appointment_id`).
    rebooked       - final: the new booking carries the deposit (`new_appointment_id`).
    refund_due     - the patient cancelled (`refund_source="patient"`) or B passed ("timeout");
                     ONE `released_refund_due` attention item is open for the clinic.
    refund_closed  - final: `closed_reason` "patient_received" (Recebido) or "clinic_settled".

LGPD: ids, instants, states and kinds only - never a name, phone, e-mail or message text.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base

STATE_OPEN = "open"
STATE_RESCHEDULING = "rescheduling"
STATE_KEPT = "kept"
STATE_REBOOKED = "rebooked"
STATE_REFUND_DUE = "refund_due"
STATE_REFUND_CLOSED = "refund_closed"
OFFER_STATES = (
    STATE_OPEN,
    STATE_RESCHEDULING,
    STATE_KEPT,
    STATE_REBOOKED,
    STATE_REFUND_DUE,
    STATE_REFUND_CLOSED,
)
# The states in which "Manter", "Remarcar", "Cancelar" and the B deadline may still act.
DECIDING_STATES = (STATE_OPEN, STATE_RESCHEDULING)

REFUND_SOURCE_PATIENT = "patient"
REFUND_SOURCE_TIMEOUT = "timeout"
CLOSED_PATIENT_RECEIVED = "patient_received"
CLOSED_CLINIC_SETTLED = "clinic_settled"

# `DepositReleaseEmail.kind` (<= 24 characters).
EMAIL_REFUND_NOW = "clinic_refund_now"  # A: at the "Cancelar" tap
EMAIL_REFUND_DEADLINE = "clinic_refund_deadline"  # B
EMAIL_REFUND_DAY = "clinic_refund_day"  # C: 24 h after the release
EMAIL_REFUND_REMINDER = "clinic_refund_reminder"  # "Não recebido" (seq 1, 2, ...)
EMAIL_PATIENT_KEEPS = "clinic_patient_keeps"  # "Manter horário" worked
EMAIL_PATIENT_CHECK = "patient_check"  # the patient's "Recebido / Não recebido" (C)
EMAIL_KINDS = (
    EMAIL_REFUND_NOW,
    EMAIL_REFUND_DEADLINE,
    EMAIL_REFUND_DAY,
    EMAIL_REFUND_REMINDER,
    EMAIL_PATIENT_KEEPS,
    EMAIL_PATIENT_CHECK,
)

OUTCOME_SENDING = "sending"  # claimed; a send interrupted mid-way stays here (at most once)
OUTCOME_SENT = "sent"
OUTCOME_SKIPPED = "skipped"  # no longer relevant when its time came
OUTCOME_NO_CONTACT = "skipped_no_contact"
OUTCOME_GIVEN_UP = "given_up"


class DepositReleaseOffer(Base):
    __tablename__ = "deposit_release_offers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    deposit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pix_deposits.id", ondelete="CASCADE"), index=True
    )
    released_appointment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE")
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("patients.id", ondelete="SET NULL"), nullable=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    state: Mapped[str] = mapped_column(String(16), default=STATE_OPEN, server_default=STATE_OPEN)
    released_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # T
    decide_by_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # B
    refund_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # C = T + 24 h
    # The clinic's "may send a billed WhatsApp template" at release time, reused at C.
    paid_notice_allowed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    refund_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    refund_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    new_appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    patient_check_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_reminder_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "released_appointment_id", name="uq_deposit_release_offers_released_appointment_id"
        ),
        Index("ix_deposit_release_offers_state_decide_by", "state", "decide_by_at"),
    )


class DepositReleaseEmail(Base):
    __tablename__ = "deposit_release_emails"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deposit_release_offers.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(24))
    seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("offer_id", "kind", "seq", name="uq_deposit_release_emails_offer_kind_seq"),
        Index("ix_deposit_release_emails_due", "claimed_at", "due_at"),
    )
```

`migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py`:

```python
"""deposit release offers and their scheduled e-mails (released PAID slot)

TASK-052 (docs/superpowers/specs/2026-10-11-liberar-horario-com-sinal-pago-design.md). Purely
ADDITIVE (skill frozen-contract-migration): TWO new tables, nothing altered, nothing dropped. Only
code written for TASK-052 reads or writes them, so code that predates this revision keeps working.

DEPLOY ORDER: this migration -> secretarIA API -> secretarIA worker RIGHT AFTER (the API opens
offers; the worker sends the buttons, handles the taps, runs the B deadline and the e-mails) ->
Brain-Message-Frontend (copy of the release card) last.

Revision ID: b6d2e8f4a1c3
Revises: e2b7c4a9d1f5
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6d2e8f4a1c3"
down_revision: str | None = "e2b7c4a9d1f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deposit_release_offers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "deposit_id",
            sa.Uuid(),
            sa.ForeignKey("pix_deposits.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "released_appointment_id",
            sa.Uuid(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "patient_id", sa.Uuid(), sa.ForeignKey("patients.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("state", sa.String(16), nullable=False, server_default="open"),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decide_by_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refund_check_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_notice_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("refund_source", sa.String(16), nullable=True),
        sa.Column("refund_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "new_appointment_id",
            sa.Uuid(),
            sa.ForeignKey("appointments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_reason", sa.String(24), nullable=True),
        sa.Column("patient_check_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reminder_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "released_appointment_id", name="uq_deposit_release_offers_released_appointment_id"
        ),
    )
    op.create_index("ix_deposit_release_offers_tenant_id", "deposit_release_offers", ["tenant_id"])
    op.create_index(
        "ix_deposit_release_offers_deposit_id", "deposit_release_offers", ["deposit_id"]
    )
    op.create_index(
        "ix_deposit_release_offers_conversation_id", "deposit_release_offers", ["conversation_id"]
    )
    op.create_index(
        "ix_deposit_release_offers_state_decide_by",
        "deposit_release_offers",
        ["state", "decide_by_at"],
    )
    op.create_table(
        "deposit_release_emails",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "offer_id",
            sa.Uuid(),
            sa.ForeignKey("deposit_release_offers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(24), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "offer_id", "kind", "seq", name="uq_deposit_release_emails_offer_kind_seq"
        ),
    )
    op.create_index("ix_deposit_release_emails_tenant_id", "deposit_release_emails", ["tenant_id"])
    op.create_index(
        "ix_deposit_release_emails_due", "deposit_release_emails", ["claimed_at", "due_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_deposit_release_emails_due", table_name="deposit_release_emails")
    op.drop_index("ix_deposit_release_emails_tenant_id", table_name="deposit_release_emails")
    op.drop_table("deposit_release_emails")
    op.drop_index("ix_deposit_release_offers_state_decide_by", table_name="deposit_release_offers")
    op.drop_index("ix_deposit_release_offers_conversation_id", table_name="deposit_release_offers")
    op.drop_index("ix_deposit_release_offers_deposit_id", table_name="deposit_release_offers")
    op.drop_index("ix_deposit_release_offers_tenant_id", table_name="deposit_release_offers")
    op.drop_table("deposit_release_offers")
```

Em `src/secretaria/models/__init__.py`: depois da linha `from secretaria.models.conversation_pii_token_map import ConversationPiiTokenMap`, adicione

```python
from secretaria.models.deposit_release_offer import DepositReleaseEmail, DepositReleaseOffer
```

e, na lista `__all__`, logo depois de `"ConsentEvent",`, adicione `"DepositReleaseEmail",` e `"DepositReleaseOffer",`.

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_deposit_release_offer_migration.py tests/test_deposit_before_migration.py tests/test_payment_migration.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/models/deposit_release_offer.py src/secretaria/models/__init__.py migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py tests/test_deposit_release_offer_migration.py tests/test_deposit_before_migration.py`
Expected: PASS (os de Postgres opcionais ficam `skipped`), ruff limpo.

- [ ] **Step 5 (opcional, recomendado): ida e volta num Postgres descartável**

Com um Postgres LOCAL descartável (`initdb` numa pasta temporária, porta livre): `$env:PAYMENTS_TEST_PG_URL = "postgresql+asyncpg://postgres@127.0.0.1:<porta>/postgres"` e rode `tests/test_deposit_before_migration.py tests/test_payment_migration.py`. Expected: PASS (o teste de ida e volta compara o banco migrado com os modelos). Desligue e apague o Postgres depois. Sem Postgres: "NÃO PROVADO em Postgres" no `TASK.md`.

- [ ] **Step 6: Commit**

```powershell
git add src/secretaria/models/deposit_release_offer.py src/secretaria/models/__init__.py migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py tests/test_deposit_release_offer_migration.py tests/test_deposit_before_migration.py
git commit -m "feat(payments): release offer and scheduled e-mail tables, additive migration (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 2: Os prazos B e C com o horário da clínica (`services/release_deadlines.py`)

**Files:**
- Create: `src/secretaria/services/release_deadlines.py`
- Test: `tests/test_release_deadlines.py` (novo)

**Interfaces:**
- Produces: `DECIDE_HOURS = 6`, `CHECK_HOURS = 24`, `CLOSING_MARGIN = timedelta(minutes=30)`; `closing_time(business_hours: dict | None, day: date) -> time | None`; `decide_by(*, released_at: datetime, business_hours: dict | None, timezone: str | None) -> datetime` (UTC); `refund_check_at(released_at: datetime) -> datetime` (UTC). Puro: sem banco, sem relógio.

- [ ] **Step 1: Escrever o teste que falha**

`tests/test_release_deadlines.py`:

```python
"""services/release_deadlines.py - when a released PAID slot's refund falls due (TASK-052).

B = min(T + 6 h, 30 min before the clinic closes on T's day), in the clinic's zone; when the
clinic does not close AFTER T that day (closed, no hours, or T already past closing - 30 min),
B = T + 6 h. C = T + 24 h. Never before T.
"""

from datetime import UTC, date, datetime, time, timedelta

from secretaria.services import release_deadlines as rd

SP = "America/Sao_Paulo"  # UTC-3 all year (no DST since 2019)
# Tuesday 2030-03-05, 14:00 in Sao Paulo.
T = datetime(2030, 3, 5, 17, 0, tzinfo=UTC)


def _hours(end: str, *, day: str = "tuesday") -> dict:
    return {day: [{"start": "08:00", "end": "12:00"}, {"start": "13:00", "end": end}]}


def test_the_owners_example_late_closing_gives_t_plus_six_hours():
    # Closes 22:00 -> 21:30 is later than 20:00 (T + 6 h): B = 20:00 local.
    assert rd.decide_by(released_at=T, business_hours=_hours("22:00"), timezone=SP) == T + timedelta(
        hours=6
    )


def test_an_early_closing_comes_first():
    # Closes 18:00 -> 17:30 local = 20:30 UTC, before T + 6 h.
    assert rd.decide_by(released_at=T, business_hours=_hours("18:00"), timezone=SP) == datetime(
        2030, 3, 5, 20, 30, tzinfo=UTC
    )


def test_released_after_the_closing_margin_gets_six_hours_never_less_than_t():
    late = datetime(2030, 3, 5, 20, 45, tzinfo=UTC)  # 17:45 local, closes 18:00
    assert rd.decide_by(released_at=late, business_hours=_hours("18:00"), timezone=SP) == late + (
        timedelta(hours=6)
    )


def test_a_day_without_hours_or_a_clinic_without_hours_gets_six_hours():
    assert rd.decide_by(
        released_at=T, business_hours=_hours("18:00", day="monday"), timezone=SP
    ) == T + timedelta(hours=6)
    assert rd.decide_by(released_at=T, business_hours={}, timezone=SP) == T + timedelta(hours=6)
    assert rd.decide_by(released_at=T, business_hours=None, timezone=SP) == T + timedelta(hours=6)


def test_the_clinic_zone_decides_the_day_and_the_closing():
    # 14:00 in Manaus (UTC-4) is 18:00 UTC; closes 15:00 local -> 14:30 local = 18:30 UTC.
    manaus_t = datetime(2030, 3, 5, 18, 0, tzinfo=UTC)
    assert rd.decide_by(
        released_at=manaus_t, business_hours=_hours("15:00"), timezone="America/Manaus"
    ) == datetime(2030, 3, 5, 18, 30, tzinfo=UTC)


def test_bad_entries_and_a_bad_zone_are_ignored_safely():
    messy = {"tuesday": [{"start": "08:00", "end": "25:99"}, {"start": "x"}, "nope"]}
    assert rd.closing_time(messy, date(2030, 3, 5)) is None
    assert rd.decide_by(released_at=T, business_hours=messy, timezone="Not/AZone") == T + timedelta(
        hours=6
    )


def test_the_closing_is_the_latest_end_of_the_day():
    assert rd.closing_time(_hours("19:30"), date(2030, 3, 5)) == time(19, 30)


def test_a_naive_instant_is_read_as_utc_and_c_is_one_day_later():
    naive = T.replace(tzinfo=None)
    assert rd.decide_by(released_at=naive, business_hours=None, timezone=SP) == T + timedelta(hours=6)
    assert rd.refund_check_at(naive) == T + timedelta(hours=24)


def test_b_is_never_before_t_nor_after_c():
    for end in ("09:00", "14:20", "14:31", "18:00", "23:59"):
        b = rd.decide_by(released_at=T, business_hours=_hours(end), timezone=SP)
        assert T <= b < rd.refund_check_at(T)
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_deadlines.py -q -p no:cacheprovider`
Expected: FAIL (`ImportError: cannot import name 'release_deadlines'`).

- [ ] **Step 3: Implementar**

`src/secretaria/services/release_deadlines.py`:

```python
"""When a released PAID slot's refund falls due (TASK-052, owner decision 2026-10-12). Pure.

T = the release instant. B (`decide_by`) = min(T + 6 h, 30 minutes before the clinic closes on
T's local day). C (`refund_check_at`) = T + 24 h.

The clinic's closing is the latest `end` among the windows of `Tenant.business_hours` for T's
weekday (shape `{"monday": [{"start": "08:00", "end": "12:00"}, ...]}`, schemas/config.py), in
`Tenant.timezone`. There is no holiday calendar in the product.

When the clinic does not close AFTER T that day (a day without windows, a clinic without hours,
or T already past closing - 30 min), B = T + 6 h: the patient keeps the time to decide and the
clinic reads the notice when it opens. Owner rule "never before T, B at least T" holds: T <= B
<= T + 6 h < C.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DECIDE_HOURS = 6
CHECK_HOURS = 24
CLOSING_MARGIN = timedelta(minutes=30)
DEFAULT_TIMEZONE = "America/Sao_Paulo"
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception:  # noqa: BLE001 - a bad zone never breaks the deadline
        return ZoneInfo(DEFAULT_TIMEZONE)


def _hhmm(value) -> time | None:
    if not isinstance(value, str):
        return None
    try:
        hours, minutes = value.split(":")
        return time(int(hours), int(minutes))
    except (ValueError, TypeError):
        return None


def closing_time(business_hours: dict | None, day: date) -> time | None:
    """The latest valid `end` of `day`'s weekday windows, or None when the clinic is closed."""
    if not isinstance(business_hours, dict):
        return None
    windows = business_hours.get(_WEEKDAYS[day.weekday()])
    if not isinstance(windows, list):
        return None
    ends = [_hhmm(window.get("end")) for window in windows if isinstance(window, dict)]
    valid = [end for end in ends if end is not None]
    return max(valid) if valid else None


def decide_by(
    *, released_at: datetime, business_hours: dict | None, timezone: str | None
) -> datetime:
    released = _utc(released_at)
    six_hours = released + timedelta(hours=DECIDE_HOURS)
    zone = _zone(timezone)
    local_day = released.astimezone(zone).date()
    closing = closing_time(business_hours, local_day)
    if closing is None:
        return six_hours
    cut = (datetime.combine(local_day, closing, tzinfo=zone) - CLOSING_MARGIN).astimezone(UTC)
    if cut <= released:
        return six_hours
    return min(six_hours, cut)


def refund_check_at(released_at: datetime) -> datetime:
    return _utc(released_at) + timedelta(hours=CHECK_HOURS)
```

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_deadlines.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/services/release_deadlines.py tests/test_release_deadlines.py`
Expected: PASS (9), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/services/release_deadlines.py tests/test_release_deadlines.py
git commit -m "feat(payments): refund deadlines of a released paid slot from the clinic's closing time (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 3: O serviço da oferta (`services/release_offer.py`) e o motivo `released_refund_due`

**Files:**
- Create: `src/secretaria/services/release_offer.py`, `tests/release_offer_support.py`
- Modify: `src/secretaria/models/payment_attention_item.py` (motivo novo), `src/secretaria/services/payments/attention_view.py` (texto do motivo; devolução registrável), `tests/test_payment_attention.py` (lista de motivos)
- Test: `tests/test_release_offer.py` (novo)

**Interfaces:**
- Consumes: Task 1 (modelos e constantes), Task 2 (`decide_by`, `refund_check_at`); `attention.open_attention_item`, `attention.attention_dedupe_key`, `db_utils.insert_ignore`, `money.format_brl`, `staff_patient_message.conversation_id_for`, `core.whatsapp_limits.{MAX_INTERACTIVE_BODY_CHARS, truncate_plain}`.
- Produces (todas em `secretaria.services.release_offer`):
  - ações `ACTION_KEEP="relkeep"`, `ACTION_RESCHEDULE="relresched"`, `ACTION_CANCEL="relcancel"`, `ACTION_RECEIVED="relgot"`, `ACTION_NOT_RECEIVED="relmiss"`, `RELEASE_OFFER_ACTIONS` (as 5); rótulos `LABEL_*`; `DEPOSIT_OUTCOME_OFFERED="choice_offered"`; `REASON_CARRIED_OVER="carried_over"`; `REMINDER_INTERVAL=timedelta(hours=24)`;
  - textos `OFFER_HEAD`, `OFFER_HEAD_CONFIRMED`, `CLINIC_NOTE_PREFIX`, `OFFER_DETAIL` (`{amount}`, `{deadline}`), `KEEP_DONE_TEXT` (`{when}`), `KEEP_TAKEN_TEXT`, `KEEP_PAST_TEXT`, `KEEP_UNAVAILABLE_TEXT`, `TAP_ALREADY_KEPT_TEXT` (`{when}`), `TAP_ALREADY_REBOOKED_TEXT`, `TAP_KEEP_AFTER_CANCEL_TEXT`, `TAP_KEEP_AFTER_DEADLINE_TEXT`, `CANCEL_DONE_TEXT` (`{amount}`), `TAP_REFUND_ALREADY_TEXT`, `TAP_CANCEL_AFTER_KEEP_TEXT`, `RESCHEDULE_PREFIX`, `RESCHEDULE_AFTER_REFUND_PREFIX`, `CARRIED_OVER_NOTE`, `CHECK_TEXT` (`{amount}`), `RECEIVED_TEXT`, `NOT_RECEIVED_TEXT`, `NOT_RECEIVED_RECENT_TEXT`, `CHECK_TOO_EARLY_TEXT`, `CHECK_CLOSED_TEXT`, `NOT_FOUND_TEXT`;
  - `offer_buttons(id)`, `keep_fallback_buttons(id)`, `check_buttons(id) -> list[tuple[str, str]]`; `when_text(dt, timezone) -> "HH:MM de DD/MM"`; `slot_text(dt, timezone) -> "DD/MM às HH:MM"`; `offer_body(*, deposit, decide_by_at, timezone, justification, confirmed) -> str`; `template_text(text) -> str`; `warning_text(deposit) -> str`; `console_conversation_link(conversation_id) -> str | None`;
  - `async open_offer(session, *, tenant, appointment, deposit, now=None, paid_notice_allowed=False) -> DepositReleaseOffer | None`; `async find_offer(session, *, tenant_id, released_appointment_id)`; `async choose_cancel(session, *, offer, now=None) -> bool`; `async settle_if_expired(session, *, offer, now=None) -> bool`; `async expire_offers(session, *, now=None, limit=50) -> int` (comita); `async choose_reschedule(session, *, offer, conversation_id, now=None) -> bool`; `async carry_over_pending(session, *, tenant_id, conversation_id, now=None) -> bool`; `async claim_carry_over(session, *, tenant_id, conversation_id, new_appointment, now=None) -> PixDeposit | None`; `async claim_keep(session, *, offer, new_appointment, now=None) -> bool`; `async record_received(session, *, offer, now=None) -> str` (`closed`/`already_closed`/`too_early`/`not_refund`); `async request_reminder(session, *, offer, now=None) -> str` (`sent`/`recent`/`too_early`/`closed`/`not_refund`); `async refund_settled(session, offer) -> bool`; `async close_settled(session, *, offer, now=None) -> bool`; `async skip_pending(session, offer_id, now) -> None`; `deadline_passed(offer, now=None) -> bool`.
  - `REASON_RELEASED_REFUND_DUE = "released_refund_due"` (em `models/payment_attention_item.py`).
  - Nada comita, exceto `expire_offers`.

- [ ] **Step 1: Escrever os testes que falham**

`tests/release_offer_support.py` (ajudante, não é teste):

```python
"""Shared helpers for the TASK-052 tests (a released appointment with a PAID deposit). Not a test
module. One clinic, one patient, one conversation, the released (CANCELLED) appointment and its
PAID deposit, on the in-memory SQLite database of tests/payments_support.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    Patient,
    PixDeposit,
    PixDepositStatus,
    Tenant,
)
from secretaria.models.deposit_release_offer import DepositReleaseEmail
from tests.before_support import make_world

# T: Tuesday 2030-03-05, 14:00 in Sao Paulo. The released consultation: Friday 08/03, 10:00.
T = datetime(2030, 3, 5, 17, 0, tzinfo=UTC)
RELEASED_START = datetime(2030, 3, 8, 13, 0, tzinfo=UTC)
# Closes at 22:00 on Tuesdays: B = min(T + 6 h, 21:30) = 20:00 local = T + 6 h (owner's example).
OPEN_LATE = {"tuesday": [{"start": "08:00", "end": "22:00"}]}


def utc(value: datetime | None) -> datetime | None:
    """SQLite reads timestamps back naive; every instant here is UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass
class Released:
    tenant: Tenant
    patient: Patient
    conversation: Conversation
    appointment: Appointment
    deposit: PixDeposit

    @property
    def tenant_id(self) -> UUID:
        return self.tenant.id


async def make_released(
    session,
    *,
    channel: str = "whatsapp",
    deposit_status: PixDepositStatus = PixDepositStatus.PAID,
    amount_cents: int = 6000,
    timing: str = "after",
    business_hours: dict | None = None,
    **tenant_kw,
) -> Released:
    """A clinic whose appointment was just RELEASED (status CANCELLED) with a deposit."""
    tenant_kw.setdefault("contact_email", "clinica@exemplo.com")
    world = await make_world(
        session,
        channel=channel,
        timing=timing,
        business_hours=OPEN_LATE if business_hours is None else business_hours,
        **tenant_kw,
    )
    appointment = Appointment(
        id=uuid4(),
        tenant_id=world.tenant_id,
        patient_id=world.patient.id,
        conversation_id=world.conversation.id,
        google_event_id=f"evt-{uuid4().hex[:8]}",
        appointment_type="Consulta",
        phone=world.patient.wa_id,
        status=AppointmentStatus.CANCELLED,
        start_at=RELEASED_START,
        end_at=RELEASED_START + timedelta(minutes=30),
    )
    session.add(appointment)
    await session.flush()
    deposit = PixDeposit(
        id=uuid4(),
        tenant_id=world.tenant_id,
        appointment_id=appointment.id,
        patient_id=world.patient.id,
        asaas_payment_id=f"pay_{uuid4().hex[:10]}",
        provider="asaas",
        provider_charge_id=None,
        amount_cents=amount_cents,
        percent_applied=30,
        status=deposit_status,
        paid_at=datetime(2030, 3, 1, 12, 0, tzinfo=UTC)
        if deposit_status is PixDepositStatus.PAID
        else None,
    )
    deposit.provider_charge_id = deposit.asaas_payment_id
    session.add(deposit)
    await session.commit()
    return Released(world.tenant, world.patient, world.conversation, appointment, deposit)


async def new_appointment(session, released: Released, *, start: datetime) -> Appointment:
    """A fresh SCHEDULED appointment of the same patient (what a rebooking/keep commit creates)."""
    appointment = Appointment(
        id=uuid4(),
        tenant_id=released.tenant_id,
        patient_id=released.patient.id,
        conversation_id=released.conversation.id,
        google_event_id=f"evt-{uuid4().hex[:8]}",
        appointment_type="Consulta",
        phone=released.patient.wa_id,
        status=AppointmentStatus.SCHEDULED,
        start_at=start,
        end_at=start + timedelta(minutes=30),
    )
    session.add(appointment)
    await session.flush()
    return appointment


async def emails(session, offer_id: UUID) -> list[DepositReleaseEmail]:
    return list(
        await session.scalars(
            select(DepositReleaseEmail)
            .where(DepositReleaseEmail.offer_id == offer_id)
            .order_by(DepositReleaseEmail.due_at, DepositReleaseEmail.kind, DepositReleaseEmail.seq)
        )
    )
```

`tests/test_release_offer.py`:

```python
"""services/release_offer.py - the offer a patient gets when the clinic frees a PAID slot.

TASK-052, owner decision 2026-10-12. Money rule D15: the platform NEVER refunds; "devolver" = ONE
`released_refund_due` attention item plus scheduled e-mails to the clinic. Every transition is
one guarded UPDATE, so a tap, the B deadline and a booking/keep commit never both win.
"""

from datetime import timedelta
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import func, select

from secretaria.core.whatsapp_limits import (
    MAX_BUTTON_ID_CHARS,
    MAX_BUTTON_LABEL_CHARS,
    MAX_INTERACTIVE_BODY_CHARS,
)
from secretaria.models import PaymentAttentionItem, PixDeposit, PixDepositStatus
from secretaria.models.deposit_release_offer import DepositReleaseOffer
from secretaria.models.pix_deposit import FULFILLMENT_NEEDS_ATTENTION
from secretaria.services import release_offer
from secretaria.services.payments import attention_view
from tests.payments_support import make_sessionmaker
from tests.release_offer_support import (
    RELEASED_START,
    T,
    emails,
    make_released,
    new_appointment,
    utc,
)

B = T + timedelta(hours=6)
C = T + timedelta(hours=24)
SP = "America/Sao_Paulo"


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


async def _offer(db, *, now=T, **kw):
    async with db() as session:
        released = await make_released(session, **kw)
        offer = await release_offer.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=now,
            paid_notice_allowed=True,
        )
        await session.commit()
    return released, offer


async def _row(db, offer_id) -> DepositReleaseOffer:
    async with db() as session:
        return await session.get(DepositReleaseOffer, offer_id)


async def _deposit(db, deposit_id) -> PixDeposit:
    async with db() as session:
        return await session.get(PixDeposit, deposit_id)


async def _items(db) -> list[PaymentAttentionItem]:
    async with db() as session:
        return list(await session.scalars(select(PaymentAttentionItem)))


async def _schedule(db, offer_id) -> list[tuple]:
    async with db() as session:
        return [(e.kind, e.seq, utc(e.due_at)) for e in await emails(session, offer_id)]


async def _cancel(db, offer_id, at):
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer_id)
        won = await release_offer.choose_cancel(session, offer=row, now=at)
        await session.commit()
    return won


async def _mark_check_sent(db, offer_id, at):
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer_id)
        row.patient_check_sent_at = at
        await session.commit()


def test_the_cards_fit_whatsapp_and_name_the_choices():
    rid = uuid4()
    for buttons, labels in (
        (release_offer.offer_buttons(rid), ["Manter horário", "Remarcar", "Cancelar"]),
        (release_offer.keep_fallback_buttons(rid), ["Remarcar", "Cancelar"]),
        (release_offer.check_buttons(rid), ["Recebido", "Não recebido"]),
    ):
        assert [label for _id, label in buttons] == labels
        for button_id, label in buttons:
            assert len(label) <= MAX_BUTTON_LABEL_CHARS
            assert len(button_id) <= MAX_BUTTON_ID_CHARS
            assert button_id.split("|")[0] in release_offer.RELEASE_OFFER_ACTIONS
            assert button_id.endswith(f"|{rid}")


def test_the_offer_body_is_the_owners_sentence_and_never_cuts_the_explanation():
    deposit = PixDeposit(amount_cents=6000)
    body = release_offer.offer_body(
        deposit=deposit, decide_by_at=B, timezone=SP, justification=None, confirmed=False
    )
    assert body.startswith(
        "Devido à sua não confirmação da ida à consulta, a clínica liberou o seu horário para que "
        "você decida o que fazer."
    )
    assert "R$ 60,00" in body and "até 20:00 de 05/03" in body
    for label in ("*Manter horário*", "*Remarcar*", "*Cancelar*"):
        assert label in body
    for promise in ("devolvemos", "estorno automático", "reembolsamos"):
        assert promise not in body
    detail = release_offer.OFFER_DETAIL.format(amount="R$ 60,00", deadline="20:00 de 05/03")
    long = release_offer.offer_body(
        deposit=deposit, decide_by_at=B, timezone=SP, justification="x" * 3000, confirmed=True
    )
    assert len(long) <= MAX_INTERACTIVE_BODY_CHARS
    assert long.startswith(release_offer.OFFER_HEAD_CONFIRMED)
    assert long.endswith(detail)
    assert f"{release_offer.CLINIC_NOTE_PREFIX}x" in long
    assert "\n" not in release_offer.template_text(long)


async def test_opening_is_idempotent_only_for_a_paid_deposit_and_sets_the_deadlines(db):
    released, offer = await _offer(db)
    assert offer.state == "open" and offer.conversation_id == released.conversation.id
    assert (utc(offer.released_at), utc(offer.decide_by_at), utc(offer.refund_check_at)) == (T, B, C)
    assert offer.paid_notice_allowed is True
    async with db() as session:
        again = await release_offer.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=T + timedelta(minutes=5),
        )
        await session.commit()
        count = await session.scalar(select(func.count()).select_from(DepositReleaseOffer))
    assert again.id == offer.id and count == 1

    _other, nothing = await _offer(db, deposit_status=PixDepositStatus.AWAITING)
    assert nothing is None
    assert await _items(db) == []  # opening decides nothing about the money


async def test_a_deposit_of_another_clinic_never_gets_an_offer(db):
    released, _offer_row = await _offer(db)
    other, _ = await _offer(db)
    async with db() as session:
        crossed = await release_offer.open_offer(
            session, tenant=other.tenant, appointment=released.appointment, deposit=released.deposit
        )
    assert crossed is None


async def test_cancel_opens_one_item_and_schedules_a_b_c_and_the_check(db):
    released, offer = await _offer(db)
    cancel_at = T + timedelta(hours=1)

    assert await _cancel(db, offer.id, cancel_at) is True

    closed = await _row(db, offer.id)
    assert (closed.state, closed.refund_source) == ("refund_due", "patient")
    assert utc(closed.refund_due_at) == cancel_at
    (item,) = await _items(db)
    assert item.reason == "released_refund_due" and item.suggested_refund_cents == 6000
    assert item.tenant_id == released.tenant_id and item.deposit_id == released.deposit.id
    assert await _schedule(db, offer.id) == [
        ("clinic_refund_now", 0, cancel_at),
        ("clinic_refund_deadline", 0, B),
        ("clinic_refund_day", 0, C),
        ("patient_check", 0, C),
    ]
    deposit = await _deposit(db, released.deposit.id)
    assert deposit.status is PixDepositStatus.PAID  # never refunded by us (D15)
    assert deposit.fulfillment_status == FULFILLMENT_NEEDS_ATTENTION


async def test_cancelling_twice_or_after_b_changes_nothing(db):
    _released, offer = await _offer(db)
    assert await _cancel(db, offer.id, B + timedelta(minutes=1)) is False  # past B: the sweep's
    assert await _cancel(db, offer.id, T + timedelta(hours=1)) is True
    assert await _cancel(db, offer.id, T + timedelta(hours=2)) is False
    assert len(await _items(db)) == 1
    assert len(await _schedule(db, offer.id)) == 4


async def test_the_sweep_at_b_cancels_silence_and_schedules_b_and_c_without_a(db):
    _a, silent = await _offer(db)
    _b, later = await _offer(db, now=T + timedelta(hours=3))  # its own B is later

    async with db() as session:
        assert await release_offer.expire_offers(session, now=B) == 1
    async with db() as session:
        assert await release_offer.expire_offers(session, now=B + timedelta(minutes=5)) == 0

    closed = await _row(db, silent.id)
    assert (closed.state, closed.refund_source) == ("refund_due", "timeout")
    assert [kind for kind, _seq, _due in await _schedule(db, silent.id)] == [
        "clinic_refund_deadline",
        "clinic_refund_day",
        "patient_check",
    ]
    assert (await _row(db, later.id)).state == "open"
    assert len(await _items(db)) == 1


async def test_remarcar_keeps_b_and_unfinished_it_is_cancelled_at_b(db):
    released, offer = await _offer(db)
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.choose_reschedule(
            session, offer=row, conversation_id=released.conversation.id, now=T + timedelta(hours=5)
        )
        await session.commit()
    moved = await _row(db, offer.id)
    assert moved.state == "rescheduling" and utc(moved.decide_by_at) == B  # B does not move

    async with db() as session:
        assert await release_offer.carry_over_pending(
            session,
            tenant_id=released.tenant_id,
            conversation_id=released.conversation.id,
            now=B - timedelta(minutes=1),
        )
        assert not await release_offer.carry_over_pending(
            session, tenant_id=released.tenant_id, conversation_id=released.conversation.id, now=B
        )
        assert await release_offer.expire_offers(session, now=B) == 1
    assert (await _row(db, offer.id)).refund_source == "timeout"
    assert "clinic_refund_now" not in [kind for kind, *_ in await _schedule(db, offer.id)]


async def test_remarcar_then_cancel_before_b_is_the_patients_cancel(db):
    released, offer = await _offer(db)
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        await release_offer.choose_reschedule(
            session, offer=row, conversation_id=released.conversation.id, now=T + timedelta(hours=1)
        )
        await session.commit()

    assert await _cancel(db, offer.id, T + timedelta(hours=2)) is True

    assert (await _row(db, offer.id)).refund_source == "patient"
    assert [kind for kind, *_ in await _schedule(db, offer.id)][0] == "clinic_refund_now"


async def test_the_booking_commit_carries_the_paid_deposit_over_exactly_once(db):
    released, offer = await _offer(db)
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        await release_offer.choose_reschedule(
            session, offer=row, conversation_id=released.conversation.id, now=T
        )
        await session.commit()
    async with db() as session:
        fresh = await new_appointment(session, released, start=RELEASED_START + timedelta(days=4))
        carried = await release_offer.claim_carry_over(
            session,
            tenant_id=released.tenant_id,
            conversation_id=released.conversation.id,
            new_appointment=fresh,
            now=T + timedelta(hours=1),
        )
        await session.commit()
    assert carried is not None and carried.appointment_id == fresh.id
    assert carried.status is PixDepositStatus.PAID and carried.reschedule_count == 0
    done = await _row(db, offer.id)
    assert (done.state, done.new_appointment_id) == ("rebooked", fresh.id)
    assert await _items(db) == [] and await _schedule(db, offer.id) == []

    async with db() as session:
        second = await new_appointment(session, released, start=RELEASED_START + timedelta(days=5))
        assert (
            await release_offer.claim_carry_over(
                session,
                tenant_id=released.tenant_id,
                conversation_id=released.conversation.id,
                new_appointment=second,
                now=T + timedelta(hours=2),
            )
            is None
        )
        assert await release_offer.expire_offers(session, now=B) == 0


async def test_another_clinic_or_a_closed_offer_carries_nothing(db):
    released, offer = await _offer(db)
    other, _ = await _offer(db)
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        await release_offer.choose_reschedule(
            session, offer=row, conversation_id=released.conversation.id, now=T
        )
        await session.commit()
    async with db() as session:
        foreign = await new_appointment(session, other, start=RELEASED_START + timedelta(days=4))
        assert (
            await release_offer.claim_carry_over(
                session,
                tenant_id=other.tenant_id,
                conversation_id=released.conversation.id,
                new_appointment=foreign,
                now=T,
            )
            is None
        )
    assert (await _deposit(db, released.deposit.id)).appointment_id == released.appointment.id


async def test_keep_claim_moves_the_deposit_and_schedules_one_clinic_email(db):
    released, offer = await _offer(db)
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        again = await new_appointment(session, released, start=RELEASED_START)
        assert await release_offer.claim_keep(
            session, offer=row, new_appointment=again, now=T + timedelta(hours=1)
        )
        await session.commit()

    kept = await _row(db, offer.id)
    assert (kept.state, kept.new_appointment_id) == ("kept", again.id)
    deposit = await _deposit(db, released.deposit.id)
    assert deposit.appointment_id == again.id and deposit.status is PixDepositStatus.PAID
    assert await _schedule(db, offer.id) == [("clinic_patient_keeps", 0, T + timedelta(hours=1))]
    assert await _items(db) == []
    async with db() as session:
        assert await release_offer.expire_offers(session, now=B) == 0
    assert await _cancel(db, offer.id, T + timedelta(hours=2)) is False  # kept stays kept


async def test_keep_after_cancel_or_after_b_is_refused(db):
    released, offer = await _offer(db)
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        late = await new_appointment(session, released, start=RELEASED_START)
        assert not await release_offer.claim_keep(session, offer=row, new_appointment=late, now=B)
        assert await release_offer.choose_cancel(session, offer=row, now=T + timedelta(hours=1))
        assert not await release_offer.claim_keep(
            session, offer=row, new_appointment=late, now=T + timedelta(hours=2)
        )
        await session.commit()
    assert (await _deposit(db, released.deposit.id)).appointment_id == released.appointment.id
    assert (await _row(db, offer.id)).state == "refund_due"


async def test_a_tap_that_loses_to_the_sweep_opens_no_second_item(db):
    _released, offer = await _offer(db)
    async with db() as session:
        await release_offer.expire_offers(session, now=B)

    assert await _cancel(db, offer.id, T + timedelta(hours=1)) is False

    assert len(await _items(db)) == 1


async def test_received_closes_the_story_and_skips_what_is_pending(db):
    _released, offer = await _offer(db)
    await _cancel(db, offer.id, T + timedelta(hours=1))
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.record_received(session, offer=row, now=T + timedelta(hours=2)) == (
            "too_early"
        )
    await _mark_check_sent(db, offer.id, C)

    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.record_received(session, offer=row, now=C + timedelta(hours=1)) == (
            "closed"
        )
        await session.commit()
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.record_received(session, offer=row, now=C + timedelta(hours=2)) == (
            "already_closed"
        )

    closed = await _row(db, offer.id)
    assert (closed.state, closed.closed_reason) == ("refund_closed", "patient_received")
    async with db() as session:
        assert {e.outcome for e in await emails(session, offer.id)} == {"skipped"}


async def test_not_received_asks_the_clinic_at_most_once_a_day(db):
    _released, offer = await _offer(db)
    await _cancel(db, offer.id, T + timedelta(hours=1))
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.request_reminder(session, offer=row, now=C) == "too_early"
    await _mark_check_sent(db, offer.id, C)

    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.request_reminder(
            session, offer=row, now=C + timedelta(hours=1)
        ) == "sent"
        await session.commit()
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.request_reminder(
            session, offer=row, now=C + timedelta(hours=5)
        ) == "recent"
        assert await release_offer.request_reminder(
            session, offer=row, now=C + timedelta(hours=26)
        ) == "sent"
        await session.commit()

    reminders = [(k, s) for k, s, _due in await _schedule(db, offer.id) if k == "clinic_refund_reminder"]
    assert reminders == [("clinic_refund_reminder", 1), ("clinic_refund_reminder", 2)]


async def test_the_clinic_or_the_provider_settling_the_refund_is_seen(db):
    _released, offer = await _offer(db)
    await _cancel(db, offer.id, T + timedelta(hours=1))
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert not await release_offer.refund_settled(session, row)
        item = await session.scalar(select(PaymentAttentionItem))
        item.provider_refund_seen_at = T + timedelta(hours=3)  # the Asaas refund webhook
        await session.commit()

    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.refund_settled(session, row)
        assert await release_offer.close_settled(session, offer=row, now=T + timedelta(hours=4))
        await session.commit()

    closed = await _row(db, offer.id)
    assert (closed.state, closed.closed_reason) == ("refund_closed", "clinic_settled")


async def test_resolving_the_item_as_refunded_in_the_panel_records_the_refund(db):
    released, offer = await _offer(db)
    await _cancel(db, offer.id, T + timedelta(hours=1))
    (item,) = await _items(db)

    async with db() as session:
        outcome = await attention_view.resolve_item(
            session,
            released.tenant_id,
            item.id,
            action="refunded_in_provider_panel",
            note="Devolvido no painel",
            resolved_by_name="Ana",
        )
    assert outcome == "resolved"
    assert (await _deposit(db, released.deposit.id)).status is PixDepositStatus.CANCELLED_REFUNDED
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.refund_settled(session, row)
```

Em `tests/test_payment_attention.py`, no conjunto de motivos esperado (o que termina em `"refunded_externally",`), adicione a linha `"released_refund_due",` logo depois de `"refunded_externally",`.

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_offer.py tests/test_payment_attention.py -q -p no:cacheprovider`
Expected: FAIL (`ImportError: cannot import name 'release_offer'`; `released_refund_due` ausente de `ATTENTION_REASONS`).

- [ ] **Step 3: Implementar**

Em `src/secretaria/models/payment_attention_item.py`, depois de `REASON_REFUNDED_EXTERNALLY = "refunded_externally"`:

```python
# TASK-052: the clinic released a PAID slot and the patient cancelled (or did not answer by the B
# deadline) - the clinic returns the full deposit in the provider's panel.
REASON_RELEASED_REFUND_DUE = "released_refund_due"
```

e, dentro de `ATTENTION_REASONS = frozenset({...})`, depois de `REASON_REFUNDED_EXTERNALLY,`, adicione `REASON_RELEASED_REFUND_DUE,`.

Em `src/secretaria/services/payments/attention_view.py`:
1. No bloco `from secretaria.models.payment_attention_item import (...)`, adicione `REASON_RELEASED_REFUND_DUE,` (ordem alfabética, depois de `REASON_REFUND_DUE_POLICY,`).
2. No dicionário de textos por motivo (o que tem `"refunded_externally": "O provedor informa que este pagamento foi estornado.",`), adicione depois dessa linha:

```python
    "released_refund_due": "Horário liberado pela clínica: devolver o sinal ao paciente.",
```

3. Em `_REFUND_RECORDING_REASONS = frozenset({...})`, depois de `REASON_PROMOTION_FAILED,`, adicione `REASON_RELEASED_REFUND_DUE,` (resolver com "devolvido no painel" registra a devolução no sinal).

`src/secretaria/services/release_offer.py`:

```python
"""The PAID deposit of a slot the CLINIC released: keep, reschedule or cancel (TASK-052).

Owner decision 2026-10-12 (docs/superpowers/specs/2026-10-11-liberar-horario-com-sinal-pago-
design.md). The platform never returns money (decision D15) and does not decide for the patient:

* `open_offer` (hub release, same transaction) writes ONE `DepositReleaseOffer` with the three
  instants T (`released_at`), B (`decide_by_at`, services/release_deadlines.py) and C
  (`refund_check_at` = T + 24 h). The patient gets "Manter horário" / "Remarcar" / "Cancelar".
* "Cancelar" (`choose_cancel`, before B) or silence until B (`settle_if_expired`, from the sweep
  `expire_offers` or from a late tap) makes the refund DUE: ONE `released_refund_due` attention
  item and the scheduled sends - A now (cancel only), B at B, C and the patient's check at C.
* "Remarcar" (`choose_reschedule`) reserves the deposit for the next booking of the conversation
  until B (`carry_over_pending` / `claim_carry_over`, same transaction as the booking).
* "Manter horário" is services/release_keep.py; its commit calls `claim_keep` here.
* At C the patient answers "Recebido" (`record_received`) or "Não recebido" (`request_reminder`,
  at most one clinic e-mail per 24 h). The clinic settling the refund (item resolved, provider
  refund seen, deposit no longer PAID) closes the story (`refund_settled` / `close_settled`).

Concurrency: every transition is ONE guarded UPDATE (`WHERE state IN (...)` plus the B deadline),
so a tap, the sweep and a booking never both win; the attention item is deduped per deposit and
every scheduled send is unique per (offer, kind, seq). Isolation: every query carries the tenant
id the caller resolved, never one from a payload. Never logs a name, phone, e-mail or text.
Nothing here commits unless the docstring says so.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS, truncate_plain
from secretaria.models import Appointment, Patient, PixDeposit, PixDepositStatus, Tenant
from secretaria.models.deposit_release_offer import (
    CLOSED_CLINIC_SETTLED,
    CLOSED_PATIENT_RECEIVED,
    DECIDING_STATES,
    EMAIL_PATIENT_CHECK,
    EMAIL_PATIENT_KEEPS,
    EMAIL_REFUND_DAY,
    EMAIL_REFUND_DEADLINE,
    EMAIL_REFUND_NOW,
    EMAIL_REFUND_REMINDER,
    OUTCOME_SKIPPED,
    REFUND_SOURCE_PATIENT,
    REFUND_SOURCE_TIMEOUT,
    STATE_KEPT,
    STATE_OPEN,
    STATE_REBOOKED,
    STATE_REFUND_CLOSED,
    STATE_REFUND_DUE,
    STATE_RESCHEDULING,
    DepositReleaseEmail,
    DepositReleaseOffer,
)
from secretaria.models.payment_attention_item import (
    ATTENTION_OPEN,
    REASON_RELEASED_REFUND_DUE,
    PaymentAttentionItem,
)
from secretaria.services import release_deadlines
from secretaria.services.payments import attention
from secretaria.services.payments.db_utils import insert_ignore
from secretaria.services.payments.money import format_brl
from secretaria.services.staff_patient_message import conversation_id_for

logger = get_logger(__name__)

DEFAULT_TIMEZONE = "America/Sao_Paulo"
REMINDER_INTERVAL = timedelta(hours=24)

# Button ids: "<action>|<RELEASED appointment id>" (schemas/webhook.py `_ACTION_BUTTON_PREFIXES`;
# the Portal accepts them only when a recent card of the conversation offered them).
ACTION_KEEP = "relkeep"
ACTION_RESCHEDULE = "relresched"
ACTION_CANCEL = "relcancel"
ACTION_RECEIVED = "relgot"
ACTION_NOT_RECEIVED = "relmiss"
RELEASE_OFFER_ACTIONS: tuple[str, ...] = (
    ACTION_KEEP,
    ACTION_RESCHEDULE,
    ACTION_CANCEL,
    ACTION_RECEIVED,
    ACTION_NOT_RECEIVED,
)
# <= 20 characters each (core/whatsapp_limits.py MAX_BUTTON_LABEL_CHARS).
LABEL_KEEP = "Manter horário"
LABEL_RESCHEDULE = "Remarcar"
LABEL_CANCEL = "Cancelar"
LABEL_RECEIVED = "Recebido"
LABEL_NOT_RECEIVED = "Não recebido"

# `deposit_outcome` of the hub's release answer (409 and 200) when the deposit is PAID.
DEPOSIT_OUTCOME_OFFERED = "choice_offered"
# The `before` booking gate's reason for asking no deposit: this conversation carries a paid one.
REASON_CARRIED_OVER = "carried_over"

# --- patient copy (pt-BR; the CLINIC returns the money, never "we", D15) ----------------------

OFFER_HEAD = (
    "Devido à sua não confirmação da ida à consulta, a clínica liberou o seu horário para que "
    "você decida o que fazer."
)
# The owner's sentence would be false when the patient HAD confirmed and the clinic released anyway.
OFFER_HEAD_CONFIRMED = "A clínica liberou o seu horário para que você decida o que fazer."
CLINIC_NOTE_PREFIX = "Recado da clínica: "
OFFER_DETAIL = (
    "Você já pagou o sinal desta consulta ({amount}). Toque em *Manter horário* para ficar com o "
    "mesmo horário, em *Remarcar* para escolher outro dia e horário usando o mesmo sinal, sem "
    "nova cobrança, ou em *Cancelar* para cancelar a consulta e receber o sinal de volta. Se você "
    "não responder até {deadline}, a consulta será cancelada e a clínica fará a devolução do sinal."
)
KEEP_DONE_TEXT = (
    "Pronto! Seu horário de {when} está mantido. O sinal que você já pagou continua valendo para "
    "esta consulta."
)
KEEP_TAKEN_TEXT = (
    "Esse horário já foi ocupado pela clínica. Você pode remarcar para outro dia e horário, com o "
    "mesmo sinal, ou cancelar e receber o sinal de volta."
)
KEEP_PAST_TEXT = (
    "Esse horário já passou. Você pode remarcar para outro dia e horário, com o mesmo sinal, ou "
    "cancelar e receber o sinal de volta."
)
KEEP_UNAVAILABLE_TEXT = "Não consegui confirmar o horário agora. Tente de novo em instantes."
TAP_ALREADY_KEPT_TEXT = "Sua consulta já está mantida para {when}."
TAP_ALREADY_REBOOKED_TEXT = "O sinal que você já pagou está valendo para a sua consulta."
TAP_KEEP_AFTER_CANCEL_TEXT = (
    "Você já pediu o cancelamento desta consulta e a clínica foi avisada para devolver o seu "
    "sinal. Para marcar de novo, toque em Remarcar."
)
TAP_KEEP_AFTER_DEADLINE_TEXT = (
    "O prazo para manter este horário terminou e a clínica foi avisada para devolver o seu sinal. "
    "Para marcar de novo, toque em Remarcar."
)
CANCEL_DONE_TEXT = (
    "Combinado, sua consulta foi cancelada. A clínica foi avisada e fará a devolução do seu sinal "
    "de {amount}."
)
TAP_REFUND_ALREADY_TEXT = "A devolução do seu sinal já foi pedida à clínica."
TAP_CANCEL_AFTER_KEEP_TEXT = (
    "O sinal que você já pagou está valendo para a sua consulta. Para cancelar essa consulta, use "
    "as opções da própria consulta."
)
RESCHEDULE_PREFIX = "Ótimo! O sinal que você já pagou vale para a nova consulta."
RESCHEDULE_AFTER_REFUND_PREFIX = (
    "A devolução do seu sinal já foi pedida à clínica, então a nova consulta segue as regras "
    "de sinal da clínica."
)
CARRIED_OVER_NOTE = "O sinal que você já pagou vale para esta consulta. Não há nova cobrança."
CHECK_TEXT = (
    "A clínica foi avisada para devolver o seu sinal de {amount}. Confira a sua conta bancária "
    "(a devolução vai para a conta de onde saiu o Pix) e nos conte:"
)
RECEIVED_TEXT = "Que bom! Obrigada por confirmar."
NOT_RECEIVED_TEXT = "Avisamos a clínica novamente. Ela vai conferir a devolução do seu sinal."
NOT_RECEIVED_RECENT_TEXT = "A clínica já foi avisada novamente há pouco."
CHECK_TOO_EARLY_TEXT = "A clínica ainda está dentro do prazo para devolver o seu sinal."
CHECK_CLOSED_TEXT = "Esse pagamento já está encerrado."
NOT_FOUND_TEXT = "Não encontrei essa consulta."


def _buttons(released_appointment_id, *pairs: tuple[str, str]) -> list[tuple[str, str]]:
    return [(f"{action}|{released_appointment_id}", label) for action, label in pairs]


def offer_buttons(released_appointment_id) -> list[tuple[str, str]]:
    return _buttons(
        released_appointment_id,
        (ACTION_KEEP, LABEL_KEEP),
        (ACTION_RESCHEDULE, LABEL_RESCHEDULE),
        (ACTION_CANCEL, LABEL_CANCEL),
    )


def keep_fallback_buttons(released_appointment_id) -> list[tuple[str, str]]:
    """What "Manter horário" offers when the slot is taken or already began."""
    return _buttons(
        released_appointment_id, (ACTION_RESCHEDULE, LABEL_RESCHEDULE), (ACTION_CANCEL, LABEL_CANCEL)
    )


def check_buttons(released_appointment_id) -> list[tuple[str, str]]:
    return _buttons(
        released_appointment_id,
        (ACTION_RECEIVED, LABEL_RECEIVED),
        (ACTION_NOT_RECEIVED, LABEL_NOT_RECEIVED),
    )


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _zone(timezone: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(timezone or DEFAULT_TIMEZONE)
    except Exception:  # noqa: BLE001
        return ZoneInfo(DEFAULT_TIMEZONE)


def when_text(value: datetime, timezone: str | None) -> str:
    """"20:00 de 05/03" in the clinic's zone (the B deadline)."""
    return _as_utc(value).astimezone(_zone(timezone)).strftime("%H:%M de %d/%m")


def slot_text(value: datetime, timezone: str | None) -> str:
    """"08/03 às 10:00" in the clinic's zone (a consultation)."""
    return _as_utc(value).astimezone(_zone(timezone)).strftime("%d/%m às %H:%M")


def offer_body(
    *,
    deposit: PixDeposit,
    decide_by_at: datetime,
    timezone: str | None,
    justification: str | None,
    confirmed: bool,
    limit: int = MAX_INTERACTIVE_BODY_CHARS,
) -> str:
    """The release notice of a PAID slot. The clinic's note is cut to fit, never the rest."""
    head = OFFER_HEAD_CONFIRMED if confirmed else OFFER_HEAD
    detail = OFFER_DETAIL.format(
        amount=format_brl(deposit.amount_cents), deadline=when_text(decide_by_at, timezone)
    )
    fixed = f"{head}\n\n{detail}"
    note = (justification or "").strip()
    room = limit - len(fixed) - len("\n\n") - len(CLINIC_NOTE_PREFIX)
    if not note or room <= 0:
        return truncate_plain(fixed, limit)
    return f"{head}\n\n{CLINIC_NOTE_PREFIX}{truncate_plain(note, room)}\n\n{detail}"


def template_text(text: str) -> str:
    """A WhatsApp template parameter may not carry line breaks: one line, single spaces."""
    return " ".join(text.split())


def warning_text(deposit: PixDeposit) -> str:
    """What the CLINIC reads before releasing a paid slot (the 409 `retention_ack_required`)."""
    return (
        f"O paciente já pagou o sinal ({format_brl(deposit.amount_cents)}). Ao liberar o horário, "
        "ele poderá manter o horário, remarcar usando o mesmo sinal ou cancelar. Se ele cancelar "
        "ou não responder até o prazo, a clínica receberá e-mails para devolver o sinal por "
        "inteiro pelo painel do provedor de pagamento."
    )


def console_conversation_link(conversation_id: UUID | None) -> str | None:
    """The clinic console's thread of this patient (Brain-Message-Frontend
    `lib/portal-routes.ts::conversationHref`), or None while the portal URL is unset."""
    base = (get_settings().BRAIN_MESSAGE_PORTAL_URL or "").strip().rstrip("/")
    if not base or conversation_id is None:
        return None
    return f"{base}/chat/?conversationId={conversation_id}"


def deadline_passed(offer: DepositReleaseOffer, now: datetime | None = None) -> bool:
    return _as_utc(offer.decide_by_at) <= _now(now)


# --- opening -----------------------------------------------------------------------------------


async def open_offer(
    session: AsyncSession,
    *,
    tenant: Tenant,
    appointment: Appointment,
    deposit: PixDeposit,
    now: datetime | None = None,
    paid_notice_allowed: bool = False,
) -> DepositReleaseOffer | None:
    """Open the offer for a released appointment whose deposit is PAID; idempotent.

    None when there is nothing to offer (no PAID deposit of THIS clinic on THIS appointment).
    Does not commit.
    """
    if (
        deposit.tenant_id != tenant.id
        or appointment.tenant_id != tenant.id
        or deposit.appointment_id != appointment.id
        or deposit.status is not PixDepositStatus.PAID
    ):
        return None
    now = _now(now)
    patient = (
        await session.get(Patient, appointment.patient_id) if appointment.patient_id else None
    )
    if patient is not None and patient.tenant_id != tenant.id:
        patient = None
    conversation_id = await conversation_id_for(session, tenant.id, appointment, patient)
    await insert_ignore(
        session,
        DepositReleaseOffer,
        {
            "tenant_id": tenant.id,
            "deposit_id": deposit.id,
            "released_appointment_id": appointment.id,
            "patient_id": appointment.patient_id,
            "conversation_id": conversation_id,
            "state": STATE_OPEN,
            "released_at": now,
            "decide_by_at": release_deadlines.decide_by(
                released_at=now, business_hours=tenant.business_hours, timezone=tenant.timezone
            ),
            "refund_check_at": release_deadlines.refund_check_at(now),
            "paid_notice_allowed": bool(paid_notice_allowed),
        },
        index_elements=["released_appointment_id"],
    )
    offer = await find_offer(session, tenant_id=tenant.id, released_appointment_id=appointment.id)
    if offer is not None:
        logger.info(
            "deposit_release_offer_opened",
            tenant_id=str(tenant.id),
            offer_id=str(offer.id),
            deposit_id=str(deposit.id),
        )
    return offer


async def find_offer(
    session: AsyncSession, *, tenant_id: UUID, released_appointment_id: UUID
) -> DepositReleaseOffer | None:
    return await session.scalar(
        select(DepositReleaseOffer).where(
            DepositReleaseOffer.tenant_id == tenant_id,
            DepositReleaseOffer.released_appointment_id == released_appointment_id,
        )
    )


# --- scheduling --------------------------------------------------------------------------------


async def _schedule(
    session: AsyncSession, offer: DepositReleaseOffer, kind: str, due_at: datetime, seq: int = 0
) -> None:
    await insert_ignore(
        session,
        DepositReleaseEmail,
        {
            "tenant_id": offer.tenant_id,
            "offer_id": offer.id,
            "kind": kind,
            "seq": seq,
            "due_at": _as_utc(due_at),
            "attempts": 0,
        },
        index_elements=["offer_id", "kind", "seq"],
    )


async def skip_pending(session: AsyncSession, offer_id: UUID, now: datetime) -> None:
    """Every send of the offer nobody claimed yet will never go out."""
    await session.execute(
        update(DepositReleaseEmail)
        .where(DepositReleaseEmail.offer_id == offer_id, DepositReleaseEmail.claimed_at.is_(None))
        .values(claimed_at=now, outcome=OUTCOME_SKIPPED)
        .execution_options(synchronize_session=False)
    )


async def _open_refund_item(session: AsyncSession, offer: DepositReleaseOffer) -> None:
    deposit = await session.get(PixDeposit, offer.deposit_id)
    if deposit is None or deposit.tenant_id != offer.tenant_id:
        logger.error("deposit_release_offer_deposit_missing", offer_id=str(offer.id))
        return
    released = await session.get(Appointment, offer.released_appointment_id)
    charge_ref = deposit.provider_charge_id or deposit.asaas_payment_id or str(deposit.id)
    await attention.open_attention_item(
        session,
        tenant_id=offer.tenant_id,
        reason=REASON_RELEASED_REFUND_DUE,
        dedupe_key=attention.attention_dedupe_key(
            deposit.provider, charge_ref, REASON_RELEASED_REFUND_DUE
        ),
        deposit=deposit,
        suggested_refund_cents=deposit.amount_cents,
        slot_start=released.start_at if released is not None else None,
        slot_end=released.end_at if released is not None else None,
        service_label=released.appointment_type if released is not None else None,
    )


async def _refund_became_due(
    session: AsyncSession, offer: DepositReleaseOffer, *, now: datetime, by_patient: bool
) -> None:
    await _open_refund_item(session, offer)
    if by_patient:
        await _schedule(session, offer, EMAIL_REFUND_NOW, now)  # A
    await _schedule(session, offer, EMAIL_REFUND_DEADLINE, offer.decide_by_at)  # B
    await _schedule(session, offer, EMAIL_REFUND_DAY, offer.refund_check_at)  # C
    await _schedule(session, offer, EMAIL_PATIENT_CHECK, offer.refund_check_at)


# --- the patient's choices ---------------------------------------------------------------------


async def choose_cancel(
    session: AsyncSession, *, offer: DepositReleaseOffer, now: datetime | None = None
) -> bool:
    """"Cancelar" before B: refund due now (A), plus B, C and the patient's check. True when THIS
    call did it; False when the offer is closed, kept, rebooked or past B. Does not commit."""
    now = _now(now)
    claimed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.state.in_(DECIDING_STATES),
            DepositReleaseOffer.decide_by_at > now,
        )
        .values(state=STATE_REFUND_DUE, refund_source=REFUND_SOURCE_PATIENT, refund_due_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return False
    await _refund_became_due(session, offer, now=now, by_patient=True)
    logger.info(
        "deposit_release_offer_refund_due",
        tenant_id=str(offer.tenant_id),
        offer_id=str(offer.id),
        source=REFUND_SOURCE_PATIENT,
    )
    return True


async def settle_if_expired(
    session: AsyncSession, *, offer: DepositReleaseOffer, now: datetime | None = None
) -> bool:
    """B passed with no final choice: refund due (source `timeout`), B already due, C and the
    check at C. True when THIS call did it. Does not commit."""
    now = _now(now)
    claimed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.state.in_(DECIDING_STATES),
            DepositReleaseOffer.decide_by_at <= now,
        )
        .values(state=STATE_REFUND_DUE, refund_source=REFUND_SOURCE_TIMEOUT, refund_due_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return False
    await _refund_became_due(session, offer, now=now, by_patient=False)
    logger.info(
        "deposit_release_offer_refund_due",
        tenant_id=str(offer.tenant_id),
        offer_id=str(offer.id),
        source=REFUND_SOURCE_TIMEOUT,
    )
    return True


async def expire_offers(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 50
) -> int:
    """The B deadline: every deciding offer past it becomes `refund_due`. One commit per offer;
    one failure never stops the batch. Returns how many THIS tick closed. Commits."""
    now = _now(now)
    due = (
        await session.scalars(
            select(DepositReleaseOffer.id)
            .where(
                DepositReleaseOffer.state.in_(DECIDING_STATES),
                DepositReleaseOffer.decide_by_at <= now,
            )
            .order_by(DepositReleaseOffer.decide_by_at)
            .limit(limit)
        )
    ).all()
    closed = 0
    for offer_id in due:
        try:
            offer = await session.get(DepositReleaseOffer, offer_id)
            if offer is not None and await settle_if_expired(session, offer=offer, now=now):
                closed += 1
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - one offer never stops the batch
            await session.rollback()
            logger.warning(
                "deposit_release_offer_expire_failed",
                offer_id=str(offer_id),
                error_type=type(exc).__name__,
            )
    return closed


async def choose_reschedule(
    session: AsyncSession,
    *,
    offer: DepositReleaseOffer,
    conversation_id: UUID,
    now: datetime | None = None,
) -> bool:
    """`open` -> `rescheduling` (B does not move). True when the offer is (now or already)
    `rescheduling` before B; False otherwise. Does not commit."""
    now = _now(now)
    moved = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.state == STATE_OPEN,
            DepositReleaseOffer.decide_by_at > now,
        )
        .values(state=STATE_RESCHEDULING, conversation_id=conversation_id)
        .execution_options(synchronize_session=False)
    )
    if moved.rowcount == 1:
        logger.info(
            "deposit_release_offer_rescheduling",
            tenant_id=str(offer.tenant_id),
            offer_id=str(offer.id),
        )
        return True
    current = await session.scalar(
        select(DepositReleaseOffer.id).where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.state == STATE_RESCHEDULING,
            DepositReleaseOffer.decide_by_at > now,
        )
    )
    return current is not None


async def carry_over_pending(
    session: AsyncSession, *, tenant_id: UUID, conversation_id: UUID, now: datetime | None = None
) -> bool:
    """Whether this conversation's next booking carries a paid deposit (no new charge)."""
    found = await session.scalar(
        select(DepositReleaseOffer.id).where(
            DepositReleaseOffer.tenant_id == tenant_id,
            DepositReleaseOffer.conversation_id == conversation_id,
            DepositReleaseOffer.state == STATE_RESCHEDULING,
            DepositReleaseOffer.decide_by_at > _now(now),
        )
    )
    return found is not None


async def _move_deposit(
    session: AsyncSession, offer: DepositReleaseOffer, new_appointment_id: UUID
) -> bool:
    moved = await session.execute(
        update(PixDeposit)
        .where(
            PixDeposit.id == offer.deposit_id,
            PixDeposit.tenant_id == offer.tenant_id,
            PixDeposit.status == PixDepositStatus.PAID,
            PixDeposit.appointment_id == offer.released_appointment_id,
        )
        .values(appointment_id=new_appointment_id)
        .execution_options(synchronize_session=False)
    )
    return moved.rowcount == 1


async def _undo_close(session: AsyncSession, offer_id: UUID, previous_state: str) -> None:
    await session.execute(
        update(DepositReleaseOffer)
        .where(DepositReleaseOffer.id == offer_id)
        .values(state=previous_state, closed_at=None, new_appointment_id=None)
        .execution_options(synchronize_session=False)
    )


async def claim_carry_over(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    new_appointment: Appointment,
    now: datetime | None = None,
) -> PixDeposit | None:
    """Move the paid deposit to `new_appointment` (flushed, same tenant) and close the offer as
    `rebooked`. Called INSIDE the booking's own transaction. None when there was nothing to carry
    (no offer in `rescheduling`, B passed, or the sweep/another tap won). Does not commit."""
    if new_appointment.tenant_id != tenant_id:
        return None
    now = _now(now)
    offer = await session.scalar(
        select(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.tenant_id == tenant_id,
            DepositReleaseOffer.conversation_id == conversation_id,
            DepositReleaseOffer.state == STATE_RESCHEDULING,
            DepositReleaseOffer.decide_by_at > now,
        )
        .order_by(DepositReleaseOffer.released_at.desc())
        .limit(1)
    )
    if offer is None:
        return None
    claimed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.state == STATE_RESCHEDULING,
            DepositReleaseOffer.decide_by_at > now,
        )
        .values(state=STATE_REBOOKED, closed_at=now, new_appointment_id=new_appointment.id)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return None
    if not await _move_deposit(session, offer, new_appointment.id):
        # The money moved under us (settled by the clinic, say): the booking stands on its own.
        await _undo_close(session, offer.id, STATE_RESCHEDULING)
        logger.warning("deposit_release_offer_carry_lost_deposit", offer_id=str(offer.id))
        return None
    logger.info(
        "deposit_release_offer_rebooked",
        tenant_id=str(tenant_id),
        offer_id=str(offer.id),
        appointment_id=str(new_appointment.id),
    )
    return await session.get(PixDeposit, offer.deposit_id, populate_existing=True)


async def claim_keep(
    session: AsyncSession,
    *,
    offer: DepositReleaseOffer,
    new_appointment: Appointment,
    now: datetime | None = None,
) -> bool:
    """"Manter horário" won: close the offer as `kept`, move the paid deposit to the re-created
    consultation and schedule the clinic's e-mail. Called INSIDE the transaction that inserted
    `new_appointment` (services/release_keep.py). Does not commit."""
    if new_appointment.tenant_id != offer.tenant_id:
        return False
    now = _now(now)
    previous_state = offer.state
    claimed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.state.in_(DECIDING_STATES),
            DepositReleaseOffer.decide_by_at > now,
        )
        .values(state=STATE_KEPT, closed_at=now, new_appointment_id=new_appointment.id)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return False
    if not await _move_deposit(session, offer, new_appointment.id):
        await _undo_close(session, offer.id, previous_state)
        logger.warning("deposit_release_offer_keep_lost_deposit", offer_id=str(offer.id))
        return False
    await _schedule(session, offer, EMAIL_PATIENT_KEEPS, now)
    logger.info(
        "deposit_release_offer_kept",
        tenant_id=str(offer.tenant_id),
        offer_id=str(offer.id),
        appointment_id=str(new_appointment.id),
    )
    return True


# --- after the refund fell due -----------------------------------------------------------------


async def record_received(
    session: AsyncSession, *, offer: DepositReleaseOffer, now: datetime | None = None
) -> str:
    """"Recebido": `closed`, `already_closed`, `too_early` (the check was not sent yet) or
    `not_refund` (kept/rebooked/still deciding). Does not commit."""
    now = _now(now)
    closed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.state == STATE_REFUND_DUE,
            DepositReleaseOffer.patient_check_sent_at.is_not(None),
        )
        .values(state=STATE_REFUND_CLOSED, closed_reason=CLOSED_PATIENT_RECEIVED, closed_at=now)
        .execution_options(synchronize_session=False)
    )
    if closed.rowcount == 1:
        await skip_pending(session, offer.id, now)
        logger.info("deposit_release_offer_received", offer_id=str(offer.id))
        return "closed"
    current = await session.get(DepositReleaseOffer, offer.id, populate_existing=True)
    if current is not None and current.state == STATE_REFUND_CLOSED:
        return "already_closed"
    if current is not None and current.state == STATE_REFUND_DUE:
        return "too_early"
    return "not_refund"


async def request_reminder(
    session: AsyncSession, *, offer: DepositReleaseOffer, now: datetime | None = None
) -> str:
    """"Não recebido": one more clinic e-mail, at most one per `REMINDER_INTERVAL`. Returns
    `sent`, `recent`, `too_early`, `closed` or `not_refund`. Does not commit."""
    now = _now(now)
    claimed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.state == STATE_REFUND_DUE,
            DepositReleaseOffer.patient_check_sent_at.is_not(None),
            or_(
                DepositReleaseOffer.last_reminder_at.is_(None),
                DepositReleaseOffer.last_reminder_at <= now - REMINDER_INTERVAL,
            ),
        )
        .values(last_reminder_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount == 1:
        last_seq = await session.scalar(
            select(func.coalesce(func.max(DepositReleaseEmail.seq), 0)).where(
                DepositReleaseEmail.offer_id == offer.id,
                DepositReleaseEmail.kind == EMAIL_REFUND_REMINDER,
            )
        )
        await _schedule(session, offer, EMAIL_REFUND_REMINDER, now, seq=int(last_seq or 0) + 1)
        logger.info("deposit_release_offer_not_received", offer_id=str(offer.id))
        return "sent"
    current = await session.get(DepositReleaseOffer, offer.id, populate_existing=True)
    if current is None or current.state not in (STATE_REFUND_DUE, STATE_REFUND_CLOSED):
        return "not_refund"
    if current.state == STATE_REFUND_CLOSED:
        return "closed"
    if current.patient_check_sent_at is None:
        return "too_early"
    return "recent"


async def refund_settled(session: AsyncSession, offer: DepositReleaseOffer) -> bool:
    """The clinic gave the refund as done: the deposit is no longer PAID, or this deposit's
    `released_refund_due` item was resolved, or the provider reported the refund."""
    deposit = await session.get(PixDeposit, offer.deposit_id, populate_existing=True)
    if deposit is None or deposit.status is not PixDepositStatus.PAID:
        return True
    item = await session.scalar(
        select(PaymentAttentionItem)
        .where(
            PaymentAttentionItem.tenant_id == offer.tenant_id,
            PaymentAttentionItem.deposit_id == deposit.id,
            PaymentAttentionItem.reason == REASON_RELEASED_REFUND_DUE,
        )
        .order_by(PaymentAttentionItem.detected_at.desc())
        .limit(1)
    )
    if item is None:
        return False
    return item.status != ATTENTION_OPEN or item.provider_refund_seen_at is not None


async def close_settled(
    session: AsyncSession, *, offer: DepositReleaseOffer, now: datetime | None = None
) -> bool:
    """`refund_due` -> `refund_closed` (`clinic_settled`); nothing pending goes out. Does not
    commit."""
    now = _now(now)
    closed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.state == STATE_REFUND_DUE,
        )
        .values(state=STATE_REFUND_CLOSED, closed_reason=CLOSED_CLINIC_SETTLED, closed_at=now)
        .execution_options(synchronize_session=False)
    )
    if closed.rowcount != 1:
        return False
    await skip_pending(session, offer.id, now)
    logger.info("deposit_release_offer_settled", offer_id=str(offer.id))
    return True
```

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_offer.py tests/test_payment_attention.py tests/test_payment_attention_hub.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/services/release_offer.py src/secretaria/models/payment_attention_item.py src/secretaria/services/payments/attention_view.py tests/release_offer_support.py tests/test_release_offer.py tests/test_payment_attention.py`
Expected: PASS (`test_release_offer.py`: 18; o parametrizado de `test_payment_attention_hub.py` ganha o motivo novo e ele NÃO manda o e-mail de `EMAIL_REASONS` — os e-mails desta tarefa são os de `deposit_release_emails`), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/services/release_offer.py src/secretaria/models/payment_attention_item.py src/secretaria/services/payments/attention_view.py tests/release_offer_support.py tests/test_release_offer.py tests/test_payment_attention.py
git commit -m "feat(payments): release offer service - keep/reschedule/cancel transitions, B deadline, scheduled sends, one refund item (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 4: "Manter horário" — verificar o horário e recriar a consulta (`services/release_keep.py`)

**Files:**
- Create: `src/secretaria/services/release_keep.py`
- Test: `tests/test_release_keep.py` (novo)

**Interfaces:**
- Consumes: Task 3 (`release_offer.claim_keep`, `release_offer.deadline_passed`, `choose_cancel` nos testes); `booking_promotion.calendar_for_booking(session, tenant, professional_id) -> CalendarService | None`; `CalendarService.check_availability(start, end) -> list[dict]`; `CalendarService.create_event(start, end, summary, description="", reminders=None, *, event_id=None) -> dict`; `CalendarService.cancel_event(event_id)`; `booking_hold.live_hold_overlaps(session, tenant_id, start_at, end_at, *, professional_id="any", exclude_charge_id=None) -> bool`; `LIVE_APPOINTMENT_STATUSES`.
- Produces: `KEEP_KEPT="kept"`, `KEEP_TAKEN="taken"`, `KEEP_PAST="past"`, `KEEP_CLOSED="closed"`, `KEEP_UNAVAILABLE="unavailable"`; `@dataclass(frozen=True) KeepResult(outcome: str, appointment: Appointment | None = None)`; `keep_event_id(offer_id: UUID) -> str` (`"relkeep" + hex`); `async keep_slot(session, *, tenant, offer, now=None) -> KeepResult` — COMITA quando mantém, faz rollback quando perde a corrida.

- [ ] **Step 1: Escrever os testes que falham**

`tests/test_release_keep.py`:

```python
"""services/release_keep.py - "Manter horário" on a slot the clinic released (TASK-052).

The slot counts as TAKEN when Google has someone else's event there, another conversation holds
it, or the clinic booked another live consultation of the same professional over it. Free: a new
consultation (same patient, service, professional, start, end) with a deterministic Google event
id, the PAID deposit re-pointed to it and ONE clinic e-mail scheduled - in one transaction.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    BookingHold,
    Conversation,
    Patient,
    PixDeposit,
    Tenant,
)
from secretaria.models.deposit_release_offer import DepositReleaseOffer
from secretaria.services import booking_promotion, release_keep, release_offer
from tests.payments_support import make_sessionmaker
from tests.release_offer_support import RELEASED_START, T, emails, make_released, utc

NOW = T + timedelta(hours=1)


class _Calendar:
    """The owning calendar: what Google has in the slot, and what we create/delete there."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.fail = False
        self.created: list[tuple] = []
        self.cancelled: list[str] = []

    async def check_availability(self, start, end, max_results=50):
        if self.fail:
            raise RuntimeError("google down")
        return list(self.events)

    async def create_event(
        self, start, end, summary, description="", reminders=None, *, event_id=None
    ):
        self.created.append((start, end, summary, event_id))
        return {"id": event_id, "htmlLink": f"https://calendar.google.com/{event_id}"}

    async def cancel_event(self, event_id):
        self.cancelled.append(event_id)


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


@pytest.fixture
def calendar(monkeypatch) -> _Calendar:
    owner = _Calendar()

    async def _for_booking(session, tenant, professional_id):
        return owner

    monkeypatch.setattr(booking_promotion, "calendar_for_booking", _for_booking)
    return owner


async def _opened(db):
    async with db() as session:
        released = await make_released(session)
        offer = await release_offer.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=T,
        )
        await session.commit()
    return released, offer


async def _keep(db, released, offer_id, now=NOW):
    async with db() as session:
        offer = await session.get(DepositReleaseOffer, offer_id)
        tenant = await session.get(Tenant, released.tenant_id)
        return await release_keep.keep_slot(session, tenant=tenant, offer=offer, now=now)


async def _state(db, offer_id):
    async with db() as session:
        offer = await session.get(DepositReleaseOffer, offer_id)
        live = list(
            await session.scalars(
                select(Appointment).where(Appointment.status == AppointmentStatus.SCHEDULED)
            )
        )
        deposit = await session.scalar(select(PixDeposit))
        kinds = [e.kind for e in await emails(session, offer_id)]
    return offer, live, deposit, kinds


async def test_a_free_slot_is_kept_with_the_same_deposit_and_one_clinic_email(db, calendar):
    released, offer = await _opened(db)

    result = await _keep(db, released, offer.id)

    assert result.outcome == release_keep.KEEP_KEPT
    kept, live, deposit, kinds = await _state(db, offer.id)
    (again,) = live
    assert again.id == result.appointment.id and again.id != released.appointment.id
    assert utc(again.start_at) == RELEASED_START and again.patient_id == released.patient.id
    assert again.appointment_type == "Consulta" and again.confirmation_count == 1
    assert again.google_event_id == release_keep.keep_event_id(offer.id)
    assert [created[3] for created in calendar.created] == [release_keep.keep_event_id(offer.id)]
    assert (kept.state, kept.new_appointment_id) == ("kept", again.id)
    assert deposit.appointment_id == again.id
    assert kinds == ["clinic_patient_keeps"]


async def test_a_retry_finding_its_own_event_is_not_a_conflict(db, calendar):
    released, offer = await _opened(db)
    calendar.events = [{"id": release_keep.keep_event_id(offer.id)}]

    assert (await _keep(db, released, offer.id)).outcome == release_keep.KEEP_KEPT


async def test_an_event_in_google_means_taken_and_nothing_changes(db, calendar):
    released, offer = await _opened(db)
    calendar.events = [{"id": "someone-else"}]

    assert (await _keep(db, released, offer.id)).outcome == release_keep.KEEP_TAKEN

    still, live, deposit, kinds = await _state(db, offer.id)
    assert still.state == "open" and live == [] and kinds == []
    assert deposit.appointment_id == released.appointment.id and calendar.created == []


async def test_another_conversations_hold_means_taken(db, calendar):
    released, offer = await _opened(db)
    async with db() as session:
        other = Patient(id=uuid4(), tenant_id=released.tenant_id, wa_id="5511900000009")
        session.add(other)
        await session.flush()
        theirs = Conversation(id=uuid4(), tenant_id=released.tenant_id, patient_id=other.id)
        session.add(theirs)
        await session.flush()
        session.add(
            BookingHold(
                tenant_id=released.tenant_id,
                conversation_id=theirs.id,
                patient_id=other.id,
                appointment_type="Consulta",
                start_at=RELEASED_START,
                end_at=RELEASED_START + timedelta(minutes=30),
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
        )
        await session.commit()

    assert (await _keep(db, released, offer.id)).outcome == release_keep.KEEP_TAKEN


async def test_a_consultation_the_clinic_booked_there_means_taken(db, calendar):
    released, offer = await _opened(db)
    async with db() as session:
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=released.tenant_id,
                google_event_id="evt-clinic",
                appointment_type="Retorno",
                status=AppointmentStatus.SCHEDULED,
                start_at=RELEASED_START + timedelta(minutes=15),
                end_at=RELEASED_START + timedelta(minutes=45),
            )
        )
        await session.commit()

    assert (await _keep(db, released, offer.id)).outcome == release_keep.KEEP_TAKEN


async def test_a_slot_that_already_started_is_past(db, calendar):
    released, offer = await _opened(db)
    async with db() as session:
        row = await session.get(Appointment, released.appointment.id)
        row.start_at = T + timedelta(minutes=30)
        row.end_at = T + timedelta(minutes=60)
        await session.commit()

    assert (await _keep(db, released, offer.id)).outcome == release_keep.KEEP_PAST
    assert calendar.created == []


async def test_google_down_changes_nothing(db, calendar):
    released, offer = await _opened(db)
    calendar.fail = True

    assert (await _keep(db, released, offer.id)).outcome == release_keep.KEEP_UNAVAILABLE

    still, live, _deposit, _kinds = await _state(db, offer.id)
    assert still.state == "open" and live == []


async def test_losing_the_claim_deletes_the_event_it_created(db, calendar):
    released, offer = await _opened(db)
    async with db() as session:
        stale = await session.get(DepositReleaseOffer, offer.id)  # read while still "open"
    async with db() as session:  # meanwhile the patient's other tap (or the sweep) wins
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.choose_cancel(session, offer=row, now=NOW)
        await session.commit()

    async with db() as session:
        tenant = await session.get(Tenant, released.tenant_id)
        result = await release_keep.keep_slot(session, tenant=tenant, offer=stale, now=NOW)

    assert result.outcome == release_keep.KEEP_CLOSED
    assert calendar.cancelled == [release_keep.keep_event_id(offer.id)]
    still, live, deposit, _kinds = await _state(db, offer.id)
    assert still.state == "refund_due" and live == []
    assert deposit.appointment_id == released.appointment.id


async def test_keep_after_cancel_is_closed_without_touching_google(db, calendar):
    released, offer = await _opened(db)
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        await release_offer.choose_cancel(session, offer=row, now=NOW)
        await session.commit()

    assert (await _keep(db, released, offer.id)).outcome == release_keep.KEEP_CLOSED
    assert calendar.created == [] and calendar.cancelled == []
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_keep.py -q -p no:cacheprovider`
Expected: FAIL (`ImportError: cannot import name 'release_keep'`).

- [ ] **Step 3: Implementar**

`src/secretaria/services/release_keep.py`:

```python
"""release_keep - "Manter horário" on a slot the clinic released with a PAID deposit (TASK-052).

The released row is CANCELLED and its Google event deleted (api/hub/calendar.py::
release_appointment), and Google never lets a deleted event id be used again
(services/calendar.py::CalendarService.create_event). So "Manter" re-creates: a NEW consultation
on the same slot, with a deterministic event id of its own (`keep_event_id`), and the PAID
deposit re-pointed to it (`release_offer.claim_keep`) - in ONE transaction with the offer's
guarded UPDATE.

The slot is TAKEN (nothing changes, the patient gets Remarcar / Cancelar) when, on the OWNING
calendar, Google has an event of someone else over it, another conversation holds it
(`booking_hold.live_hold_overlaps`), or the clinic has another live consultation of the same
professional over it. `CalendarService.is_slot_free` is NOT used: it hard-codes an 8-18 window.

Order: checks -> create the event -> INSERT + claim. A claim that loses the race (the B deadline
or another tap won meanwhile) rolls back and deletes the event it created. Insisting on the slot
is the patient's confirmation: the re-created row starts with `confirmation_count = 1`.
Never logs a name, phone or e-mail.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, AppointmentStatus, Patient, Tenant
from secretaria.models.appointment import LIVE_APPOINTMENT_STATUSES
from secretaria.models.deposit_release_offer import DECIDING_STATES, DepositReleaseOffer
from secretaria.services import booking_hold, booking_promotion, release_offer

logger = get_logger(__name__)

KEEP_KEPT = "kept"
KEEP_TAKEN = "taken"
KEEP_PAST = "past"
KEEP_CLOSED = "closed"
KEEP_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class KeepResult:
    outcome: str
    appointment: Appointment | None = None


def keep_event_id(offer_id: UUID) -> str:
    """Deterministic Google event id: base32hex alphabet (a-v, 0-9), 5..1024 characters."""
    return f"relkeep{offer_id.hex}"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _consultation_overlaps(
    session: AsyncSession, released: Appointment, start: datetime, end: datetime
) -> bool:
    same_professional = (
        Appointment.professional_id == released.professional_id
        if released.professional_id is not None
        else Appointment.professional_id.is_(None)
    )
    found = await session.scalar(
        select(Appointment.id)
        .where(
            Appointment.tenant_id == released.tenant_id,
            Appointment.id != released.id,
            Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
            Appointment.start_at < end,
            Appointment.end_at > start,
            same_professional,
        )
        .limit(1)
    )
    return found is not None


async def keep_slot(
    session: AsyncSession,
    *,
    tenant: Tenant,
    offer: DepositReleaseOffer,
    now: datetime | None = None,
) -> KeepResult:
    now = now or datetime.now(UTC)
    offer_id = offer.id
    if (
        offer.tenant_id != tenant.id
        or offer.state not in DECIDING_STATES
        or release_offer.deadline_passed(offer, now)
    ):
        return KeepResult(KEEP_CLOSED)
    released = await session.get(Appointment, offer.released_appointment_id)
    if (
        released is None
        or released.tenant_id != tenant.id
        or released.start_at is None
        or released.end_at is None
    ):
        return KeepResult(KEEP_CLOSED)
    start, end = _utc(released.start_at), _utc(released.end_at)
    if start <= now:
        return KeepResult(KEEP_PAST)

    calendar = await booking_promotion.calendar_for_booking(
        session, tenant, released.professional_id
    )
    if calendar is None:
        return KeepResult(KEEP_UNAVAILABLE)
    own_event = keep_event_id(offer_id)
    try:
        events = await calendar.check_availability(start, end)
    except Exception as exc:  # noqa: BLE001 - Google down: nothing changes, the patient retries
        logger.warning(
            "release_keep_calendar_failed", offer_id=str(offer_id), error_type=type(exc).__name__
        )
        return KeepResult(KEEP_UNAVAILABLE)
    if any(str(event.get("id")) != own_event for event in events):
        return KeepResult(KEEP_TAKEN)
    if await booking_hold.live_hold_overlaps(
        session, tenant.id, start, end, professional_id=released.professional_id
    ):
        return KeepResult(KEEP_TAKEN)
    if await _consultation_overlaps(session, released, start, end):
        return KeepResult(KEEP_TAKEN)

    patient = await session.get(Patient, released.patient_id) if released.patient_id else None
    display = released.attendee_name or (patient.name if patient is not None else None)
    service = released.appointment_type or "Consulta"
    try:
        event = await calendar.create_event(
            start=start,
            end=end,
            summary=f"{service} - {display}" if display else service,
            event_id=own_event,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "release_keep_create_failed", offer_id=str(offer_id), error_type=type(exc).__name__
        )
        return KeepResult(KEEP_UNAVAILABLE)

    appointment = Appointment(
        id=uuid4(),
        tenant_id=tenant.id,
        patient_id=released.patient_id,
        conversation_id=released.conversation_id,
        google_event_id=str(event.get("id") or own_event),
        google_calendar_source=released.google_calendar_source,
        google_event_link=event.get("htmlLink"),
        appointment_type=released.appointment_type,
        start_at=released.start_at,
        end_at=released.end_at,
        phone=released.phone,
        professional_id=released.professional_id,
        unit_id=released.unit_id,
        insurance=released.insurance,
        insurance_plan_id=released.insurance_plan_id,
        insurance_professional_plan_id=released.insurance_professional_plan_id,
        attendee_name=released.attendee_name,
        status=AppointmentStatus.SCHEDULED,
        confirmation_count=1,
        first_confirmed_at=now,
        last_confirmed_at=now,
    )
    session.add(appointment)
    await session.flush()
    if not await release_offer.claim_keep(
        session, offer=offer, new_appointment=appointment, now=now
    ):
        await session.rollback()
        try:
            await calendar.cancel_event(own_event)
        except Exception as exc:  # noqa: BLE001 - logged; the event is ours and empty
            logger.error(
                "release_keep_orphan_event", offer_id=str(offer_id), error_type=type(exc).__name__
            )
        return KeepResult(KEEP_CLOSED)
    await session.commit()
    logger.info(
        "release_keep_kept", offer_id=str(offer_id), appointment_id=str(appointment.id)
    )
    return KeepResult(KEEP_KEPT, appointment)
```

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_keep.py tests/test_release_offer.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/services/release_keep.py tests/test_release_keep.py`
Expected: PASS (`test_release_keep.py`: 9), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/services/release_keep.py tests/test_release_keep.py
git commit -m "feat(calendar): Manter horario re-creates the released consultation when the slot is still free (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 5: Os envios programados — e-mails, conferência do paciente e modelos Meta (`services/release_offer_dispatch.py`)

**Files:**
- Create: `src/secretaria/services/release_offer_dispatch.py`
- Modify: `src/secretaria/services/email.py` (textos + envios novos), `src/secretaria/config.py` (dois nomes de modelo, PENDÊNCIA META)
- Test: `tests/test_release_offer_dispatch.py` (novo)

**Interfaces:**
- Consumes: Task 1 (modelos/constantes), Task 3 (`release_offer.refund_settled`, `close_settled`, `when_text`, `slot_text`, `console_conversation_link`, `check_buttons`, `CHECK_TEXT`, `template_text`); `email._send_sync(to, subject, body)`; `cancellation_notice.{last_inbound_at, is_inside_window, meta_language_code}`; `BrainMessageSender.send_buttons(to, body, buttons)`; `WhatsAppClient.for_tenant(...).send_buttons/send_template`; `reminder_text.portal_conversation_link(tenant_id)`; `emit_usage_event(tenant_id=, feature=, amount=, event_id=)`.
- Produces:
  - `config.Settings.RELEASE_OFFER_TEMPLATE_NAME = "liberacao_sinal_pago_v1"`, `REFUND_CHECK_TEMPLATE_NAME = "devolucao_sinal_conferir_v1"`.
  - Em `services/email.py`: `release_refund_clinic_email(*, clinic_name, kind, first_notice, amount_text, slot_text, deadline_text, conversation_link) -> tuple[str, str]`; `release_keep_clinic_email(*, clinic_name, slot_text, conversation_link) -> tuple[str, str]`; `release_offer_patient_email(*, clinic_name, deadline_text) -> tuple[str, str]`; `refund_check_patient_email(*, clinic_name, amount_text, conversation_link) -> tuple[str, str]`; e os envios `async send_release_refund_clinic_email(to_email, **mesmos campos) -> bool`, `async send_release_keep_clinic_email(...) -> bool`, `async send_release_offer_patient_email(...) -> bool`, `async send_refund_check_patient_email(...) -> bool` (True só quando o SMTP aceitou).
  - Em `services/release_offer_dispatch.py`: `MAX_ATTEMPTS = 5`; `@dataclass DispatchReport(sent, skipped, no_contact, retried, given_up, lost)`; `async dispatch_due(session, *, now=None, offer_id=None, limit=50) -> DispatchReport` (comita por linha).

- [ ] **Step 1: Escrever os testes que falham**

`tests/test_release_offer_dispatch.py`:

```python
"""services/release_offer_dispatch.py - what a released PAID slot scheduled, sent once (TASK-052).

Each `deposit_release_emails` row is claimed (and committed) BEFORE its send, re-checked at send
time (kept/rebooked/closed/settled -> skipped), retried when nothing left the building and given
up after 5 tries. Clinic e-mails carry the amount, the slot and the console link - never the
patient's name, phone or e-mail.
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.config import get_settings
from secretaria.models import (
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    PaymentAttentionItem,
)
from secretaria.models.deposit_release_offer import DepositReleaseOffer
from secretaria.services import email, release_offer, release_offer_dispatch
from secretaria.services.payments import attention_view
from tests._reminders_v2 import FakeWhatsAppClient, fake_waba_token
from tests.payments_support import make_sessionmaker
from tests.release_offer_support import (
    RELEASED_START,
    T,
    emails,
    make_released,
    new_appointment,
    utc,
)

B = T + timedelta(hours=6)
C = T + timedelta(hours=24)
CLINIC = "clinica@exemplo.com"
PATIENT = "maria@exemplo.com"


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


@pytest.fixture
def mailbox(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    def _send(to_email, subject, body):
        sent.append({"to": to_email, "subject": subject, "body": body})

    monkeypatch.setattr(email, "_send_sync", _send)
    monkeypatch.setattr(get_settings(), "SMTP_HOST", "smtp.exemplo", raising=False)
    monkeypatch.setattr(
        get_settings(), "BRAIN_MESSAGE_PORTAL_URL", "https://portal.exemplo", raising=False
    )
    return sent


@pytest.fixture(autouse=True)
def whatsapp(monkeypatch):
    FakeWhatsAppClient.reset()
    monkeypatch.setattr(release_offer_dispatch, "WhatsAppClient", FakeWhatsAppClient)
    monkeypatch.setattr(release_offer_dispatch, "get_waba_token", fake_waba_token)

    async def _usage(**kwargs):
        return True

    monkeypatch.setattr(release_offer_dispatch, "emit_usage_event", _usage)


async def _cancelled(db, *, channel="whatsapp", patient_email=None, paid=True, **kw):
    async with db() as session:
        released = await make_released(session, channel=channel, **kw)
        if patient_email:
            patient = await session.get(Patient, released.patient.id)
            patient.email = patient_email
        offer = await release_offer.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=T,
            paid_notice_allowed=paid,
        )
        await release_offer.choose_cancel(session, offer=offer, now=T + timedelta(hours=1))
        await session.commit()
    return released, offer


async def _dispatch(db, now, **kw):
    async with db() as session:
        return await release_offer_dispatch.dispatch_due(session, now=now, **kw)


async def _outcomes(db, offer_id) -> dict:
    async with db() as session:
        return {e.kind: e.outcome for e in await emails(session, offer_id)}


async def _offer_row(db, offer_id) -> DepositReleaseOffer:
    async with db() as session:
        return await session.get(DepositReleaseOffer, offer_id)


async def test_a_cancel_sends_a_now_b_at_b_and_c_with_the_check_at_c(db, mailbox):
    _released, offer = await _cancelled(db)

    assert (await _dispatch(db, T + timedelta(hours=1))).sent == 1
    assert "Devolver sinal" in mailbox[-1]["subject"]
    assert (await _dispatch(db, B - timedelta(minutes=1))).sent == 0
    assert (await _dispatch(db, B)).sent == 1
    assert "Lembrete" in mailbox[-1]["subject"]
    assert (await _dispatch(db, C)).sent == 2  # C to the clinic + the patient's check

    assert set((await _outcomes(db, offer.id)).values()) == {"sent"}
    assert utc((await _offer_row(db, offer.id)).patient_check_sent_at) == C
    (template,) = FakeWhatsAppClient.all_sent()  # outside the window, with paid permission
    assert template[0] == "template" and template[2] == get_settings().REFUND_CHECK_TEMPLATE_NAME
    assert template[5] == [bid for bid, _ in release_offer.check_buttons(_released.appointment.id)]
    assert "\n" not in template[4][0]


async def test_the_clinic_email_has_amount_slot_and_console_link_and_no_patient_data(db, mailbox):
    released, _offer = await _cancelled(db)

    await _dispatch(db, T + timedelta(hours=1))

    (mail,) = mailbox
    assert mail["to"] == CLINIC
    assert "R$ 60,00" in mail["body"] and "08/03 às 10:00" in mail["body"]
    assert f"https://portal.exemplo/chat/?conversationId={released.conversation.id}" in mail["body"]
    assert "painel do Asaas" in mail["body"]
    assert "Maria" not in mail["body"] and released.patient.wa_id not in mail["body"]


async def test_silence_sends_b_as_the_first_notice(db, mailbox):
    async with db() as session:
        released = await make_released(session)
        await release_offer.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=T,
        )
        await session.commit()
    async with db() as session:
        await release_offer.expire_offers(session, now=B)

    await _dispatch(db, B)

    (mail,) = mailbox
    assert "não respondeu até 20:00 de 05/03" in mail["body"]


async def test_each_scheduled_email_goes_out_once_even_dispatched_twice(db, mailbox):
    _released, offer = await _cancelled(db)

    await _dispatch(db, T + timedelta(hours=1))
    await _dispatch(db, T + timedelta(hours=1, minutes=5))
    assert len(mailbox) == 1

    async with db() as session:  # another worker already claimed B
        for row in await emails(session, offer.id):
            if row.kind == "clinic_refund_deadline":
                row.claimed_at = B
                row.outcome = "sending"
        await session.commit()
    await _dispatch(db, B)
    assert len(mailbox) == 1


async def test_a_send_that_did_not_leave_is_retried_then_given_up(db, mailbox, monkeypatch):
    _released, offer = await _cancelled(db)

    def _refuse(to_email, subject, body):
        raise OSError("smtp down")

    monkeypatch.setattr(email, "_send_sync", _refuse)
    reports = [
        await _dispatch(db, T + timedelta(hours=1, minutes=minute)) for minute in range(5)
    ]

    assert [r.retried for r in reports] == [1, 1, 1, 1, 0] and reports[-1].given_up == 1
    async with db() as session:
        (row,) = [e for e in await emails(session, offer.id) if e.kind == "clinic_refund_now"]
    assert (row.outcome, row.attempts) == ("given_up", 5)


async def test_a_clinic_without_contact_email_is_skipped_not_retried(db, mailbox):
    _released, offer = await _cancelled(db, contact_email=None)

    report = await _dispatch(db, T + timedelta(hours=1))
    again = await _dispatch(db, T + timedelta(hours=1, minutes=5))

    assert (report.no_contact, again.no_contact, mailbox) == (1, 0, [])
    assert (await _outcomes(db, offer.id))["clinic_refund_now"] == "skipped_no_contact"


async def test_once_the_clinic_settles_nothing_else_goes_out(db, mailbox):
    released, offer = await _cancelled(db)
    await _dispatch(db, T + timedelta(hours=1))  # A went out
    async with db() as session:
        item = await session.scalar(select(PaymentAttentionItem))
    async with db() as session:
        await attention_view.resolve_item(
            session,
            released.tenant_id,
            item.id,
            action="refunded_in_provider_panel",
            note="",
            resolved_by_name="Ana",
        )

    report = await _dispatch(db, C)

    assert report.sent == 0 and len(mailbox) == 1 and FakeWhatsAppClient.all_sent() == []
    closed = await _offer_row(db, offer.id)
    assert (closed.state, closed.closed_reason) == ("refund_closed", "clinic_settled")
    outcomes = await _outcomes(db, offer.id)
    assert outcomes["clinic_refund_now"] == "sent"
    assert {outcomes[k] for k in ("clinic_refund_deadline", "clinic_refund_day", "patient_check")} == {
        "skipped"
    }


async def test_a_kept_offer_sends_only_the_keep_email(db, mailbox):
    async with db() as session:
        released = await make_released(session)
        offer = await release_offer.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=T,
        )
        again = await new_appointment(session, released, start=RELEASED_START)
        await release_offer.claim_keep(
            session, offer=offer, new_appointment=again, now=T + timedelta(hours=1)
        )
        await session.commit()

    report = await _dispatch(db, C + timedelta(hours=1))

    assert report.sent == 1
    (mail,) = mailbox
    assert "mantém o horário" in mail["subject"] and "08/03 às 10:00" in mail["body"]


async def test_the_portal_check_is_a_card_and_an_email_with_the_conversation_link(db, mailbox):
    released, offer = await _cancelled(db, channel="brain_message", patient_email=PATIENT)

    await _dispatch(db, C)

    (patient_mail,) = [m for m in mailbox if m["to"] == PATIENT]
    assert "R$ 60,00" in patient_mail["body"] and "conta de onde saiu o Pix" in patient_mail["body"]
    assert f"https://portal.exemplo/clinicas/?convite={released.tenant_id}" in patient_mail["body"]
    async with db() as session:
        card = await session.scalar(
            select(Message)
            .where(Message.conversation_id == released.conversation.id)
            .order_by(Message.created_at.desc())
            .limit(1)
        )
    assert [o["id"] for o in card.interactive["options"]] == [
        f"relgot|{released.appointment.id}",
        f"relmiss|{released.appointment.id}",
    ]
    assert (await _outcomes(db, offer.id))["patient_check"] == "sent"


async def test_the_whatsapp_check_inside_the_window_is_a_card(db, mailbox):
    released, _offer = await _cancelled(db)
    async with db() as session:
        session.add(
            Message(
                conversation_id=released.conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.PATIENT,
                body="oi",
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await session.commit()

    await _dispatch(db, C)

    (card,) = FakeWhatsAppClient.all_sent()
    assert card[0] == "buttons"
    assert [bid for bid, _ in card[3]] == [
        bid for bid, _ in release_offer.check_buttons(released.appointment.id)
    ]


async def test_outside_the_window_without_paid_permission_only_the_email_goes(db, mailbox):
    _released, offer = await _cancelled(db, paid=False, patient_email=PATIENT)

    await _dispatch(db, C)

    assert FakeWhatsAppClient.all_sent() == []
    assert [m for m in mailbox if m["to"] == PATIENT]
    assert (await _outcomes(db, offer.id))["patient_check"] == "sent"
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_offer_dispatch.py -q -p no:cacheprovider`
Expected: FAIL (`ImportError: cannot import name 'release_offer_dispatch'`).

- [ ] **Step 3: Implementar**

Em `src/secretaria/config.py`, logo depois da linha `REMINDER_V2_BATCH_SIZE: int = 200`:

```python
    # --- Released PAID slot (TASK-052) ---------------------------------- PENDÊNCIA META ---
    # Utility templates NOT YET APPROVED by Meta. Owner decision 2026-10-12: the code uses them as
    # if approved; once Meta approves them on each WABA, put the approved names here. Texts to
    # submit: docs/LEMBRETES_MODELOS_META.md, section "TASK-052". While unapproved, a send
    # outside the 24 h window fails, is logged, and the B/C e-mails still cover the patient.
    # 1 body parameter + 3 quick replies (Manter horário / Remarcar / Cancelar).
    RELEASE_OFFER_TEMPLATE_NAME: str = "liberacao_sinal_pago_v1"
    # 1 body parameter + 2 quick replies (Recebido / Não recebido).
    REFUND_CHECK_TEMPLATE_NAME: str = "devolucao_sinal_conferir_v1"
```

Em `src/secretaria/services/email.py`, adicione no FIM do arquivo:

```python
# --- TASK-052: a slot the clinic released with a PAID deposit ----------------------------------
# Builders are pure (tested on their own); senders are SMTP-only, never raise, and return True
# only when the server accepted the message, so services/release_offer_dispatch.py can give a
# claim back when nothing left. Bodies carry the amount, the slot and links - never the patient's
# name, phone or e-mail; logs carry only the event name.

_RELEASE_REFUND_HOW = (
    "Devolva o valor pelo painel do Asaas (o sistema não faz devoluções) e depois marque a "
    'devolução como feita em "Pagamentos que precisam de decisão". Assim estes avisos param.'
)


def _release_link_line(label: str, link: str | None) -> str:
    return f"\n\n{label}: {link}" if link else ""


def release_refund_clinic_email(
    *,
    clinic_name: str,
    kind: str,
    first_notice: bool,
    amount_text: str,
    slot_text: str,
    deadline_text: str,
    conversation_link: str | None,
) -> tuple[str, str]:
    """(subject, body) of the clinic's refund e-mails A / B / C / "não recebido"."""
    pending = f"[SecretarIA] Devolver sinal de {amount_text} — {clinic_name}"
    if kind == "clinic_refund_now":
        subject = pending
        lead = (
            f"O paciente da consulta de {slot_text}, cujo horário a clínica liberou, cancelou a "
            f"consulta. O sinal de {amount_text} precisa ser devolvido."
        )
    elif kind == "clinic_refund_deadline" and first_notice:
        subject = pending
        lead = (
            f"O paciente da consulta de {slot_text}, cujo horário a clínica liberou, não respondeu "
            f"até {deadline_text}. A consulta foi cancelada e o sinal de {amount_text} precisa ser "
            "devolvido."
        )
    elif kind == "clinic_refund_deadline":
        subject = f"[SecretarIA] Lembrete: devolução de sinal pendente — {clinic_name}"
        lead = (
            f"Lembrete: a devolução do sinal de {amount_text} da consulta de {slot_text} continua "
            "pendente."
        )
    elif kind == "clinic_refund_day":
        subject = f"[SecretarIA] Devolução de sinal ainda pendente (24 horas) — {clinic_name}"
        lead = (
            "Já se passaram 24 horas desde que a clínica liberou o horário da consulta de "
            f"{slot_text}. A devolução do sinal de {amount_text} continua pendente, e o paciente "
            "está sendo perguntado se recebeu."
        )
    else:  # clinic_refund_reminder
        subject = f"[SecretarIA] Paciente não recebeu a devolução do sinal — {clinic_name}"
        lead = (
            f"O paciente da consulta de {slot_text} informou que ainda não recebeu a devolução do "
            f"sinal de {amount_text}. Confira a devolução no painel do Asaas."
        )
    body = (
        f"Olá,\n\n{lead}\n\n{_RELEASE_REFUND_HOW}"
        f"{_release_link_line('Conversa com o paciente', conversation_link)}"
        "\n\n— Equipe SecretarIA"
    )
    return subject, body


def release_keep_clinic_email(
    *, clinic_name: str, slot_text: str, conversation_link: str | None
) -> tuple[str, str]:
    subject = f"[SecretarIA] Paciente mantém o horário de {slot_text} — {clinic_name}"
    body = (
        f"Olá,\n\nO paciente da consulta de {slot_text}, cujo horário a clínica liberou, pediu "
        "para manter o horário. O horário estava livre: a consulta voltou para a agenda e o sinal "
        "já pago continua valendo para ela."
        f"{_release_link_line('Conversa com o paciente', conversation_link)}"
        "\n\n— Equipe SecretarIA"
    )
    return subject, body


def release_offer_patient_email(*, clinic_name: str, deadline_text: str) -> tuple[str, str]:
    """At the release, to a WhatsApp patient with an e-mail (the Portal one gets the chat nudge)."""
    subject = f"{clinic_name}: o horário da sua consulta foi liberado"
    body = (
        f"Olá,\n\nA clínica {clinic_name} liberou o horário da sua consulta para que você decida o "
        "que fazer: manter o horário, remarcar usando o sinal que você já pagou, ou cancelar e "
        "receber o sinal de volta.\n\nResponda pela conversa com a clínica no WhatsApp até "
        f"{deadline_text}. Sem resposta, a consulta será cancelada e a clínica fará a devolução "
        f"do sinal.\n\n— {clinic_name}"
    )
    return subject, body


def refund_check_patient_email(
    *, clinic_name: str, amount_text: str, conversation_link: str | None
) -> tuple[str, str]:
    subject = f"{clinic_name}: confira a devolução do seu sinal"
    where = (
        f"Abra a conversa e toque em Recebido ou Não recebido: {conversation_link}"
        if conversation_link
        else "Responda pela conversa com a clínica no WhatsApp, tocando em Recebido ou Não recebido."
    )
    body = (
        f"Olá,\n\nA clínica {clinic_name} foi avisada para devolver o seu sinal de {amount_text}. "
        "Confira a sua conta bancária: a devolução vai para a conta de onde saiu o Pix."
        f"\n\n{where}\n\n— {clinic_name}"
    )
    return subject, body


async def _send_release_mail(to_email: str, subject: str, body: str, event: str) -> bool:
    if not get_settings().SMTP_HOST:
        return False
    try:
        await asyncio.to_thread(_send_sync, to_email, subject, body)
    except Exception as exc:
        logger.warning(f"{event}_failed", error_type=type(exc).__name__)
        return False
    logger.info(f"{event}_sent")
    return True


async def send_release_refund_clinic_email(
    to_email: str,
    *,
    clinic_name: str,
    kind: str,
    first_notice: bool,
    amount_text: str,
    slot_text: str,
    deadline_text: str,
    conversation_link: str | None,
) -> bool:
    subject, body = release_refund_clinic_email(
        clinic_name=clinic_name,
        kind=kind,
        first_notice=first_notice,
        amount_text=amount_text,
        slot_text=slot_text,
        deadline_text=deadline_text,
        conversation_link=conversation_link,
    )
    return await _send_release_mail(to_email, subject, body, "release_refund_clinic_email")


async def send_release_keep_clinic_email(
    to_email: str, *, clinic_name: str, slot_text: str, conversation_link: str | None
) -> bool:
    subject, body = release_keep_clinic_email(
        clinic_name=clinic_name, slot_text=slot_text, conversation_link=conversation_link
    )
    return await _send_release_mail(to_email, subject, body, "release_keep_clinic_email")


async def send_release_offer_patient_email(
    to_email: str, *, clinic_name: str, deadline_text: str
) -> bool:
    subject, body = release_offer_patient_email(clinic_name=clinic_name, deadline_text=deadline_text)
    return await _send_release_mail(to_email, subject, body, "release_offer_patient_email")


async def send_refund_check_patient_email(
    to_email: str, *, clinic_name: str, amount_text: str, conversation_link: str | None
) -> bool:
    subject, body = refund_check_patient_email(
        clinic_name=clinic_name, amount_text=amount_text, conversation_link=conversation_link
    )
    return await _send_release_mail(to_email, subject, body, "refund_check_patient_email")
```

`src/secretaria/services/release_offer_dispatch.py`:

```python
"""Sending what a released PAID slot scheduled (TASK-052, owner decision 2026-10-12).

Every `deposit_release_emails` row is ONE send (spec §5.12):

1. CLAIM: `UPDATE ... SET claimed_at = now, outcome = 'sending' WHERE id = :id AND claimed_at IS
   NULL`, committed BEFORE the send - two workers (or a tap and the sweep) never send one row.
2. RE-CHECK at send time: the clinic e-mail of a kept slot needs `kept`; everything else needs
   `refund_due` and a refund the clinic has NOT settled (item resolved, provider refund seen,
   deposit no longer PAID). Settled -> the offer becomes `refund_closed`/`clinic_settled` and
   nothing else goes out.
3. SEND: clinic e-mails to `Tenant.contact_email` (none -> `skipped_no_contact`, final); the
   patient's check to the conversation (Portal card; WhatsApp card inside the 24 h window, the
   PENDING-META template outside it only with `paid_notice_allowed`) and to `Patient.email`.
4. Nothing left -> the claim is given back (`attempts + 1`); the 5th failure is `given_up` with
   an alarm log. A send interrupted mid-way stays `sending`: at most once, never twice.

Called by the payment sweep (workers/payments_cron.py) and, for the sends due at a tap (A, the
keep e-mail, "não recebido"), right after the tap (workers/shared/release_offer_actions.py).
Logs ids, kind and outcome only.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Patient, PixDeposit, Tenant
from secretaria.models.deposit_release_offer import (
    EMAIL_PATIENT_CHECK,
    EMAIL_PATIENT_KEEPS,
    OUTCOME_GIVEN_UP,
    OUTCOME_NO_CONTACT,
    OUTCOME_SENDING,
    OUTCOME_SENT,
    OUTCOME_SKIPPED,
    REFUND_SOURCE_TIMEOUT,
    STATE_KEPT,
    STATE_REFUND_DUE,
    DepositReleaseEmail,
    DepositReleaseOffer,
)
from secretaria.services import cancellation_notice, email, release_offer
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, BrainMessageSender
from secretaria.services.payments.money import format_brl
from secretaria.services.reminder_text import portal_conversation_link
from secretaria.services.tenant_config import get_waba_token
from secretaria.services.usage_events import emit_usage_event
from secretaria.services.whatsapp import WhatsAppClient

logger = get_logger(__name__)

MAX_ATTEMPTS = 5
BATCH_SIZE = 50


@dataclass
class DispatchReport:
    sent: int = 0
    skipped: int = 0
    no_contact: int = 0
    retried: int = 0
    given_up: int = 0
    lost: int = 0


async def dispatch_due(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    offer_id: UUID | None = None,
    limit: int = BATCH_SIZE,
) -> DispatchReport:
    """Send every unclaimed row due by `now` (only `offer_id`'s when given). Commits per row;
    one row failing never stops the others."""
    now = now or datetime.now(UTC)
    report = DispatchReport()
    query = select(DepositReleaseEmail.id).where(
        DepositReleaseEmail.claimed_at.is_(None), DepositReleaseEmail.due_at <= now
    )
    if offer_id is not None:
        query = query.where(DepositReleaseEmail.offer_id == offer_id)
    due = (
        await session.scalars(
            query.order_by(DepositReleaseEmail.due_at, DepositReleaseEmail.kind).limit(limit)
        )
    ).all()
    for email_id in due:
        try:
            await _dispatch_one(session, email_id, now, report)
        except Exception as exc:  # noqa: BLE001 - one row never stops the batch
            await session.rollback()
            logger.warning(
                "release_offer_send_failed", email_id=str(email_id), error_type=type(exc).__name__
            )
    return report


async def _dispatch_one(
    session: AsyncSession, email_id: UUID, now: datetime, report: DispatchReport
) -> None:
    claimed = await session.execute(
        update(DepositReleaseEmail)
        .where(DepositReleaseEmail.id == email_id, DepositReleaseEmail.claimed_at.is_(None))
        .values(claimed_at=now, outcome=OUTCOME_SENDING)
        .execution_options(synchronize_session=False)
    )
    await session.commit()  # durable BEFORE the send: nobody else can take it now
    if claimed.rowcount != 1:
        report.lost += 1
        return
    row = await session.get(DepositReleaseEmail, email_id, populate_existing=True)
    kind, attempts, offer_id = row.kind, row.attempts, row.offer_id
    offer = await session.get(DepositReleaseOffer, offer_id, populate_existing=True)
    tenant = await session.get(Tenant, row.tenant_id)
    if (
        offer is None
        or tenant is None
        or offer.tenant_id != tenant.id
        or not await _still_relevant(session, kind, offer, now)
    ):
        await _finish(session, email_id, OUTCOME_SKIPPED)
        report.skipped += 1
        _log(email_id, offer_id, kind, OUTCOME_SKIPPED)
        return

    if kind == EMAIL_PATIENT_CHECK:
        delivered = await _send_patient_check(session, tenant, offer)
    else:
        delivered = await _send_clinic(session, tenant, offer, kind)

    if delivered is None:
        outcome = OUTCOME_NO_CONTACT
        await _finish(session, email_id, outcome)
        report.no_contact += 1
    elif delivered:
        outcome = OUTCOME_SENT
        await _finish(
            session,
            email_id,
            outcome,
            check_offer_id=offer_id if kind == EMAIL_PATIENT_CHECK else None,
            now=now,
        )
        report.sent += 1
    else:
        outcome = await _give_back(session, email_id, attempts)
        if outcome == OUTCOME_GIVEN_UP:
            report.given_up += 1
        else:
            report.retried += 1
    _log(email_id, offer_id, kind, outcome)


def _log(email_id: UUID, offer_id: UUID, kind: str, outcome: str) -> None:
    logger.info(
        "release_offer_send", email_id=str(email_id), offer_id=str(offer_id), kind=kind,
        outcome=outcome,
    )


async def _still_relevant(
    session: AsyncSession, kind: str, offer: DepositReleaseOffer, now: datetime
) -> bool:
    if kind == EMAIL_PATIENT_KEEPS:
        return offer.state == STATE_KEPT
    if offer.state != STATE_REFUND_DUE:
        return False
    if await release_offer.refund_settled(session, offer):
        await release_offer.close_settled(session, offer=offer, now=now)
        await session.commit()
        return False
    return True


async def _finish(
    session: AsyncSession,
    email_id: UUID,
    outcome: str,
    *,
    check_offer_id: UUID | None = None,
    now: datetime | None = None,
) -> None:
    await session.execute(
        update(DepositReleaseEmail)
        .where(DepositReleaseEmail.id == email_id)
        .values(outcome=outcome)
        .execution_options(synchronize_session=False)
    )
    if check_offer_id is not None:
        await session.execute(
            update(DepositReleaseOffer)
            .where(DepositReleaseOffer.id == check_offer_id)
            .values(patient_check_sent_at=now)
            .execution_options(synchronize_session=False)
        )
    await session.commit()


async def _give_back(session: AsyncSession, email_id: UUID, attempts: int) -> str:
    """Nothing left the building: retry later, or give up on the 5th failure."""
    tries = attempts + 1
    if tries >= MAX_ATTEMPTS:
        values = {"outcome": OUTCOME_GIVEN_UP, "attempts": tries}
        logger.error(
            "release_offer_send_given_up",
            alarm="release_offer_send_undelivered",
            email_id=str(email_id),
        )
    else:
        values = {"claimed_at": None, "outcome": None, "attempts": tries}
    await session.execute(
        update(DepositReleaseEmail)
        .where(DepositReleaseEmail.id == email_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return OUTCOME_GIVEN_UP if tries >= MAX_ATTEMPTS else "retry"


async def _send_clinic(
    session: AsyncSession, tenant: Tenant, offer: DepositReleaseOffer, kind: str
) -> bool | None:
    to = (tenant.contact_email or "").strip()
    if not to:
        return None
    deposit = await session.get(PixDeposit, offer.deposit_id)
    released = await session.get(Appointment, offer.released_appointment_id)
    amount = format_brl(deposit.amount_cents) if deposit is not None else ""
    slot = (
        release_offer.slot_text(released.start_at, tenant.timezone)
        if released is not None and released.start_at is not None
        else ""
    )
    link = release_offer.console_conversation_link(offer.conversation_id)
    if kind == EMAIL_PATIENT_KEEPS:
        return await email.send_release_keep_clinic_email(
            to, clinic_name=tenant.clinic_name, slot_text=slot, conversation_link=link
        )
    return await email.send_release_refund_clinic_email(
        to,
        clinic_name=tenant.clinic_name,
        kind=kind,
        first_notice=offer.refund_source == REFUND_SOURCE_TIMEOUT,
        amount_text=amount,
        slot_text=slot,
        deadline_text=release_offer.when_text(offer.decide_by_at, tenant.timezone),
        conversation_link=link,
    )


async def _send_patient_check(
    session: AsyncSession, tenant: Tenant, offer: DepositReleaseOffer
) -> bool | None:
    patient = await session.get(Patient, offer.patient_id) if offer.patient_id else None
    if patient is None or patient.tenant_id != tenant.id:
        return None
    deposit = await session.get(PixDeposit, offer.deposit_id)
    # Everything read up front: the Portal branch commits, and nothing below may lazy-load.
    amount = format_brl(deposit.amount_cents) if deposit is not None else ""
    text = release_offer.CHECK_TEXT.format(amount=amount)
    buttons = release_offer.check_buttons(offer.released_appointment_id)
    clinic_name, tenant_id = tenant.clinic_name, tenant.id
    patient_email = (patient.email or "").strip()
    on_portal = patient.channel == CHANNEL_BRAIN_MESSAGE
    conversation_id = offer.conversation_id
    external_id = patient.external_id or ""

    attempted = delivered = False
    if on_portal and conversation_id is not None:
        attempted = True
        delivered = await _check_on_portal(session, conversation_id, external_id, text, buttons)
    elif not on_portal and patient.wa_id:
        on_whatsapp = await _check_on_whatsapp(session, tenant, offer, patient, text, buttons)
        if on_whatsapp is not None:
            attempted = True
            delivered = on_whatsapp
    if patient_email:
        attempted = True
        link = portal_conversation_link(tenant_id) if on_portal else None
        if await email.send_refund_check_patient_email(
            patient_email, clinic_name=clinic_name, amount_text=amount, conversation_link=link
        ):
            delivered = True
    if not attempted:
        return None
    return delivered


async def _check_on_portal(
    session: AsyncSession, conversation_id: UUID, to: str, text: str, buttons
) -> bool:
    try:
        sender = BrainMessageSender(conversation_id=conversation_id, session=session)
        await sender.send_buttons(to=to, body=text, buttons=buttons)
        await session.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        logger.warning("release_offer_check_portal_failed", error_type=type(exc).__name__)
        return False


async def _check_on_whatsapp(
    session: AsyncSession,
    tenant: Tenant,
    offer: DepositReleaseOffer,
    patient: Patient,
    text: str,
    buttons,
) -> bool | None:
    """True sent, False failed, None not allowed (outside the window without paid permission)."""
    last = await cancellation_notice.last_inbound_at(session, tenant.id, patient.id)
    inside = cancellation_notice.is_inside_window(last)
    if not inside and not offer.paid_notice_allowed:
        return None
    token = await get_waba_token(session, tenant.id)
    offer_id = offer.id
    try:
        client = WhatsAppClient.for_tenant(tenant, token)
        if inside:
            await client.send_buttons(to=patient.wa_id, body=text, buttons=buttons)
        else:
            await client.send_template(
                to=patient.wa_id,
                template=get_settings().REFUND_CHECK_TEMPLATE_NAME,
                lang=cancellation_notice.meta_language_code(tenant.language),
                variables=[release_offer.template_text(text)],
                button_payloads=[button_id for button_id, _label in buttons],
            )
    except Exception as exc:  # noqa: BLE001 - e.g. the template is not approved yet
        logger.warning(
            "release_offer_check_whatsapp_failed",
            offer_id=str(offer_id),
            error_type=type(exc).__name__,
        )
        return False
    if not inside:
        await _meter(tenant.id, offer_id)
    return True


async def _meter(tenant_id: UUID, offer_id: UUID) -> None:
    """One billed template: best-effort meter tick (fail-open, never retried)."""
    try:
        await emit_usage_event(
            tenant_id=str(tenant_id),
            feature="reminders",
            amount=1,
            event_id=f"refundcheck:{offer_id}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("usage_emit_failed", error_type=type(exc).__name__)
```

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_offer_dispatch.py tests/test_release_offer.py tests/test_email_transactional.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/services/release_offer_dispatch.py src/secretaria/services/email.py src/secretaria/config.py tests/test_release_offer_dispatch.py`
Expected: PASS (`test_release_offer_dispatch.py`: 11; `test_email_transactional.py` inalterado), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/services/release_offer_dispatch.py src/secretaria/services/email.py src/secretaria/config.py tests/test_release_offer_dispatch.py
git commit -m "feat(payments): send the released-slot e-mails and the patient's refund check once, claimed before sending (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 6: Liberar abre a oferta (hub), Portal recebe o cartão de três botões e o paciente de WhatsApp recebe e-mail

**Files:**
- Modify: `src/secretaria/api/hub/calendar.py` (`release_appointment`), `src/secretaria/services/appointment_release.py` (`notify_released_patient`, `_notify_portal`)
- Test: `tests/test_hub_release.py`

**Interfaces:**
- Consumes: Task 3 (`open_offer`, `offer_body`, `offer_buttons`, `warning_text`, `when_text`, `DEPOSIT_OUTCOME_OFFERED`), Task 5 (`email.send_release_offer_patient_email`).
- Produces: 409 `retention_ack_required` com `deposit_outcome = "choice_offered"`; 200 com `deposit_outcome = "choice_offered"` e UMA oferta aberta; `notify_released_patient(..., offer_buttons: list[tuple[str, str]] | None = None, offer_deadline: str | None = None)`; o enfileiramento do job NÃO muda (7 argumentos; o 6º — `extra_notice` — é o corpo inteiro da oferta).

- [ ] **Step 1: Escrever os testes**

Em `tests/test_hub_release.py`:

1. Acrescente aos imports:

```python
from secretaria.config import get_settings
from secretaria.models import DepositReleaseOffer, PaymentAttentionItem
from secretaria.services import email as email_module, release_offer
from tests.release_offer_support import utc
```

2. Substitua os QUATRO testes `test_a_paid_deposit_inside_the_retention_window_needs_the_acknowledgement`, `test_with_the_acknowledgement_the_clinic_keeps_the_deposit`, `test_a_partial_policy_is_announced_as_such` e `test_even_a_full_refund_asks_first_and_then_leaves_it_to_the_clinic` por:

```python
async def _offers(db) -> list[DepositReleaseOffer]:
    async with db() as session:
        return list(await session.scalars(select(DepositReleaseOffer)))


async def _attention_items(db) -> list[PaymentAttentionItem]:
    async with db() as session:
        return list(await session.scalars(select(PaymentAttentionItem)))


@pytest.mark.parametrize(("hours_ahead", "policy"), [(2, "total"), (2, "partial"), (96, "total")])
async def test_a_paid_deposit_needs_the_acknowledgement_and_the_patient_gets_the_choice(
    client: AsyncClient, db, acting, hours_ahead, policy
):
    """TASK-052: a clinic release never retains a paid deposit, whatever the patient
    cancellation policy says - the patient keeps, reschedules or cancels."""
    world = await _paid_world(db, acting, hours_ahead=hours_ahead, policy=policy)

    response = await _release(client, world.appointment.id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "retention_ack_required"
    assert detail["deposit_outcome"] == "choice_offered" and detail["amount_cents"] == 10000
    for words in ("100,00", "manter o horário", "remarcar", "e-mails", "por inteiro"):
        assert words in detail["message"]
    assert "retid" not in detail["message"]
    assert (await _appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED
    assert _FakeCalendar.deleted == []  # Google was not touched
    assert await _offers(db) == []


async def test_with_the_acknowledgement_the_offer_opens_and_nothing_is_decided_yet(
    client: AsyncClient, db, acting
):
    world = await _paid_world(db, acting, hours_ahead=2)

    response = await _release(client, world.appointment.id, acknowledge_retention=True)

    assert response.status_code == 200
    assert response.json()["deposit_outcome"] == "choice_offered"
    # Nothing was refunded nor retained (D15): the deposit stays PAID, no item yet.
    assert await _deposit_status(db, world.appointment.id) == PixDepositStatus.PAID
    assert await _attention_items(db) == []
    [offer] = await _offers(db)
    assert offer.state == "open" and offer.released_appointment_id == world.appointment.id
    assert offer.tenant_id == world.tenant.id and offer.conversation_id == world.conversation.id
    released_at = utc(offer.released_at)
    # This clinic configured no business hours: B = T + 6 h (spec §5.2).
    assert utc(offer.decide_by_at) == released_at + timedelta(hours=6)
    assert utc(offer.refund_check_at) == released_at + timedelta(hours=24)
    assert offer.paid_notice_allowed is False
    assert len(_FakeCalendar.deleted) == 1

    again = await _release(client, world.appointment.id, acknowledge_retention=True)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "not_live"
    assert len(await _offers(db)) == 1  # a second release of the same slot changes nothing
```

3. Substitua o teste `test_the_deposit_sentence_rides_along_in_the_notice` (o que termina em `assert "retido" in pool.calls[0][5]`) por:

```python
async def test_the_offer_rides_in_the_same_job_arguments(client: AsyncClient, db, acting):
    pool = _FakeArqPool()
    _install_pool(pool)
    world = await _paid_world(db, acting, hours_ahead=2, last_inbound_at=_recent(1))

    await _release(
        client, world.appointment.id, acknowledge_retention=True, justification="Agenda cheia"
    )

    (call,) = pool.calls
    assert call[0] == "send_cancellation_notice" and len(call) == 7  # an older worker still works
    body = call[5]
    assert body.startswith(release_offer.OFFER_HEAD)
    assert "Recado da clínica: Agenda cheia" in body and "*Manter horário*" in body
    assert "retido" not in body


async def test_a_portal_patient_with_a_paid_deposit_gets_the_three_buttons(
    client: AsyncClient, db, acting, portal_mail
):
    world = await _paid_world(
        db, acting, hours_ahead=2, channel=CHANNEL_BRAIN_MESSAGE, email="paciente@x.com"
    )

    response = await _release(client, world.appointment.id, acknowledge_retention=True)

    assert response.json()["patient_notice"] == "portal_chat_email"
    [row] = await outbound_messages(db, world.conversation.id)
    assert row.body.startswith(release_offer.OFFER_HEAD)
    assert appointment_release.PORTAL_REBOOK_LINE not in row.body
    assert [o["id"] for o in row.interactive["options"]] == [
        f"relkeep|{world.appointment.id}",
        f"relresched|{world.appointment.id}",
        f"relcancel|{world.appointment.id}",
    ]
    assert [o["title"] for o in row.interactive["options"]] == [
        "Manter horário",
        "Remarcar",
        "Cancelar",
    ]


async def test_a_whatsapp_patient_with_an_email_is_told_by_email_too(
    client: AsyncClient, db, acting, monkeypatch
):
    sent: list[tuple] = []
    monkeypatch.setattr(
        email_module, "_send_sync", lambda to, subject, body: sent.append((to, subject, body))
    )
    monkeypatch.setattr(get_settings(), "SMTP_HOST", "smtp.exemplo", raising=False)
    world = await _paid_world(
        db, acting, hours_ahead=2, last_inbound_at=_recent(30), email="maria@x.com"
    )

    response = await _release(client, world.appointment.id, acknowledge_retention=True)

    # WhatsApp could not go (outside the window, no paid permission); the e-mail is free.
    assert response.json()["patient_notice"] == "whatsapp_outside_window"
    [(to, subject, body)] = sent
    assert to == "maria@x.com" and "horário da sua consulta foi liberado" in subject
    assert "manter o horário" in body and "Clínica Olhar" in body
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_hub_release.py -q -p no:cacheprovider`
Expected: FAIL nos casos novos (`deposit_outcome` ainda `retained`/`partial_refund_due`/`refund_due`; nenhuma oferta; Portal recebe texto sem `interactive`; nenhum e-mail).

- [ ] **Step 3: Implementar**

Em `src/secretaria/api/hub/calendar.py`:

1. No bloco `from secretaria.services import (...)`, adicione `release_offer,` em ordem alfabética (depois de `clinic_action_notice,`).

2. Substitua o bloco que começa em `# Money first, Google second: a release whose deposit was PAID moves money` e termina no `raise HTTPException(...)` do `retention_ack_required` por:

```python
    # Money first, Google second: a release whose deposit was PAID hands the patient a choice
    # (TASK-052: "Manter horário" / "Remarcar" / "Cancelar"; cancelling, or silence until the B
    # deadline, makes the clinic return it), so the clinic must have read the consequence. Raised
    # before anything is touched.
    deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appt.id)
    if (
        deposit is not None
        and deposit.status is PixDepositStatus.PAID
        and not body.acknowledge_retention
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            _detail(
                "retention_ack_required",
                release_offer.warning_text(deposit),
                deposit_outcome=release_offer.DEPOSIT_OUTCOME_OFFERED,
                amount_cents=deposit.amount_cents,
            ),
        )
```

3. Substitua o trecho que vai de `deposit_outcome = await deposit_lifecycle.on_appointment_cancelled(` até o fim da chamada `patient_notice = await appointment_release.notify_released_patient(...)` por:

```python
    allow_paid = staff_patient_message.paid_notice_authorised(tenant, body.notify_outside_window)
    deposit_notice: str | None = None
    offer_buttons: list[tuple[str, str]] | None = None
    offer_deadline: str | None = None
    offer = None
    paid = await deposit_lifecycle.get_deposit_for_appointment(session, appt.id)
    if paid is not None and paid.status is PixDepositStatus.PAID:
        # TASK-052: the clinic freed a PAID slot. Nothing is decided about the money and the
        # platform never refunds (D15): ONE offer, answered by the patient's three buttons or by
        # the B deadline (services/release_offer.py). No attention item yet.
        offer = await release_offer.open_offer(
            session,
            tenant=tenant,
            appointment=appt,
            deposit=paid,
            now=now,
            paid_notice_allowed=allow_paid,
        )
    if offer is not None:
        deposit_outcome = release_offer.DEPOSIT_OUTCOME_OFFERED
        deposit_notice = release_offer.offer_body(
            deposit=paid,
            decide_by_at=offer.decide_by_at,
            timezone=tenant.timezone,
            justification=body.justification,
            confirmed=appt.confirmation_count >= 1,
        )
        offer_buttons = release_offer.offer_buttons(appt.id)
        offer_deadline = release_offer.when_text(offer.decide_by_at, tenant.timezone)
    else:
        deposit_outcome = await deposit_lifecycle.on_appointment_cancelled(
            session, tenant=tenant, appointment=appt, waba_token=None
        )
        if deposit_outcome is not None:
            resolved = await deposit_lifecycle.get_deposit_for_appointment(session, appt.id)
            if resolved is not None:
                deposit_notice = deposit_lifecycle.cancellation_notice(
                    deposit_outcome, tenant, resolved
                )
    patient = await session.scalar(
        select(Patient).where(Patient.id == appt.patient_id, Patient.tenant_id == tenant.id)
    )
    await session.commit()

    # After the commit: the release stands whatever happens to the notice.
    patient_notice = await appointment_release.notify_released_patient(
        session,
        tenant,
        appt,
        patient,
        professional_name=professional_name,
        justification=body.justification,
        deposit_notice=deposit_notice,
        allow_paid=allow_paid,
        arq_pool=getattr(request.app.state, "arq_pool", None),
        offer_buttons=offer_buttons,
        offer_deadline=offer_deadline,
    )
```

(O restante — `await session.refresh(appt)`, o log `calendar_appointment_released` e o retorno — fica igual.)

Em `src/secretaria/services/appointment_release.py`:

1. Troque `from secretaria.services import cancellation_notice` por `from secretaria.services import cancellation_notice, email`.

2. Substitua a função `notify_released_patient` inteira por:

```python
async def notify_released_patient(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient | None,
    *,
    professional_name: str | None,
    justification: str | None,
    deposit_notice: str | None,
    allow_paid: bool,
    arq_pool,
    now: datetime | None = None,
    offer_buttons: list[tuple[str, str]] | None = None,
    offer_deadline: str | None = None,
) -> str:
    """Tell the patient; returns one `NOTICE_*` code. Never raises.

    `offer_buttons` / `offer_deadline` (TASK-052): the choice of a PAID slot. On the Portal the
    card carries the three buttons and its body is `deposit_notice` (the whole offer text). On
    WhatsApp the job reads the open offer itself (`workers/whatsapp/notifications.py::
    send_cancellation_notice`), so the queued arguments do not change; a patient with an e-mail
    also gets one - free, even outside the 24 h window.
    """
    # Read once, up front: a rollback inside the Portal path expires the ORM rows,
    # and the error logging below must not trigger a lazy load.
    appointment_id = appointment.id
    try:
        reason = (justification or "").strip() or RELEASE_JUSTIFICATION
        if patient is None or patient.tenant_id != tenant.id:
            return NOTICE_NO_CHANNEL
        if patient.channel == CHANNEL_BRAIN_MESSAGE:
            return await _notify_portal(
                session,
                tenant,
                appointment,
                patient,
                professional_name,
                reason,
                deposit_notice,
                offer_buttons=offer_buttons,
            )
        if offer_buttons and offer_deadline and patient.email:
            await _email_whatsapp_patient(tenant, patient.email, offer_deadline)

        to = patient.wa_id or appointment.phone
        if not to:
            return NOTICE_NO_CHANNEL
        last_inbound = await cancellation_notice.last_inbound_at(session, tenant.id, patient.id)
        if not cancellation_notice.is_inside_window(last_inbound, now=now) and not allow_paid:
            logger.info("release_notice_not_queued", reason="outside_window_not_authorised")
            return NOTICE_WHATSAPP_OUTSIDE_WINDOW
        if arq_pool is None:
            logger.warning("release_notice_not_queued", reason="no_queue")
            return NOTICE_QUEUE_UNAVAILABLE
        try:
            await arq_pool.enqueue_job(
                "send_cancellation_notice",
                str(tenant.id),
                str(appointment_id),
                professional_name,
                reason,
                deposit_notice,
                allow_paid,
            )
        except Exception as exc:
            logger.error(
                "release_notice_enqueue_failed",
                appointment_id=str(appointment_id),
                error_type=type(exc).__name__,
            )
            return NOTICE_QUEUE_UNAVAILABLE
        return NOTICE_WHATSAPP_QUEUED
    except Exception as exc:
        logger.error(
            "release_notice_failed",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )
        return NOTICE_FAILED


async def _email_whatsapp_patient(tenant: Tenant, to_email: str, deadline_text: str) -> None:
    """TASK-052: the release of a PAID slot, by e-mail, to a WhatsApp patient. Best effort."""
    try:
        await email.send_release_offer_patient_email(
            to_email, clinic_name=tenant.clinic_name, deadline_text=deadline_text
        )
    except Exception as exc:  # noqa: BLE001 - the release stands; the e-mail is a courtesy
        logger.warning("release_offer_patient_email_failed", error_type=type(exc).__name__)
```

3. Substitua a função `_notify_portal` inteira por:

```python
async def _notify_portal(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient,
    professional_name: str | None,
    reason: str | None,
    deposit_notice: str | None,
    *,
    offer_buttons: list[tuple[str, str]] | None = None,
) -> str:
    appointment_id = appointment.id  # before a rollback can expire the row
    conversation_id = await conversation_id_for(session, tenant.id, appointment, patient)
    if conversation_id is None:
        return NOTICE_NO_CHANNEL
    try:
        sender = BrainMessageSender(
            conversation_id=conversation_id, session=session, author=MessageSender.HUMAN
        )
        if offer_buttons and deposit_notice:
            # TASK-052: the three buttons ARE the way forward; the card is recorded with its
            # options, which is what lets the Portal accept the tap (workers/portal/inbound.py).
            await sender.send_buttons(
                to=patient.external_id or "", body=deposit_notice, buttons=offer_buttons
            )
        else:
            text = cancellation_notice.join_blocks(
                cancellation_notice.build_cancellation_text(professional_name, reason),
                deposit_notice,
                PORTAL_REBOOK_LINE,
            )
            await sender.send_text_message(to=patient.external_id or "", body=text)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.error(
            "release_portal_notice_failed",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )
        return NOTICE_FAILED
    nudge = await nudge_portal_patient(tenant, patient)
    return NOTICE_PORTAL_CHAT_EMAIL if nudge == EMAIL_SENT else NOTICE_PORTAL_CHAT
```

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_hub_release.py tests/test_deposit_release_preview.py tests/test_hub_cancel_portal_notice.py tests/test_hub_calendar_money.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/api/hub/calendar.py src/secretaria/services/appointment_release.py tests/test_hub_release.py`
Expected: PASS, ruff limpo. `preview_cancellation_outcome`/`release_warning_text` continuam existindo (o cancelamento pela clínica e os testes deles os usam).

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/api/hub/calendar.py src/secretaria/services/appointment_release.py tests/test_hub_release.py
git commit -m "feat(calendar): releasing a paid slot opens the patient's keep/reschedule/cancel offer instead of retaining (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 7: O job do WhatsApp manda os três botões (mesma assinatura; modelo pago fora da janela)

**Files:**
- Modify: `src/secretaria/workers/whatsapp/notifications.py` (`send_cancellation_notice`)
- Test: `tests/test_cancellation_notice.py` (3 testes novos)

**Interfaces:**
- Consumes: Task 3 (`find_offer`, `offer_buttons`, `template_text`, `offer_body`), Task 5 (`settings.RELEASE_OFFER_TEMPLATE_NAME`), `DECIDING_STATES` (Task 1).
- Produces: com oferta em decisão e `extra_notice`: dentro da janela `send_buttons(body=extra_notice, buttons=offer_buttons(id))`; fora, com `allow_paid`, `send_template(RELEASE_OFFER_TEMPLATE_NAME, variables=[template_text(extra_notice)], button_payloads=[3 ids])`; oferta já respondida: nada sai.

- [ ] **Step 1: Escrever os testes**

Em `tests/test_cancellation_notice.py`:

1. No bloco `from secretaria.models import (...)` adicione `PixDeposit,` e `PixDepositStatus,`; troque `from secretaria.services import cancellation_notice as notice  # noqa: E402` por:

```python
from secretaria.config import get_settings  # noqa: E402
from secretaria.models.deposit_release_offer import DepositReleaseOffer  # noqa: E402
from secretaria.services import (  # noqa: E402
    cancellation_notice as notice,
    release_offer,
)
```

(Se `get_settings` já estiver importado no arquivo, não duplique.)

2. Antes de `test_an_appointment_from_another_tenant_is_never_notified`, adicione:

```python
async def _open_paid_offer(db, appt) -> None:
    async with db() as session:
        row = await session.get(Appointment, appt.id)
        row.status = AppointmentStatus.CANCELLED
        deposit = PixDeposit(
            id=uuid4(),
            tenant_id=row.tenant_id,
            appointment_id=row.id,
            patient_id=row.patient_id,
            asaas_payment_id=f"pay_{uuid4().hex[:8]}",
            amount_cents=6000,
            percent_applied=30,
            status=PixDepositStatus.PAID,
        )
        session.add(deposit)
        await session.flush()
        tenant_row = await session.get(Tenant, row.tenant_id)
        await release_offer.open_offer(session, tenant=tenant_row, appointment=row, deposit=deposit)
        await session.commit()


def _offer_text() -> str:
    return release_offer.offer_body(
        deposit=PixDeposit(amount_cents=6000),
        decide_by_at=datetime.now(UTC) + timedelta(hours=6),
        timezone="America/Sao_Paulo",
        justification=None,
        confirmed=False,
    )


async def test_a_released_paid_slot_carries_the_three_choices_and_the_body_as_is(
    db, tenant, wa, metered
):
    """TASK-052: same job, same arguments; the open offer decides the card."""
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=2))
    await _open_paid_offer(db, appt)
    body = _offer_text()

    await _run(appt, justification="x" * 1500, extra=body)

    [sent] = wa.buttons
    assert [bid for bid, _ in sent["buttons"]] == [
        f"relkeep|{appt.id}",
        f"relresched|{appt.id}",
        f"relcancel|{appt.id}",
    ]
    assert sent["body"] == body and len(sent["body"]) <= 1024


async def test_outside_the_window_the_offer_goes_as_the_pending_meta_template(
    db, tenant, wa, metered
):
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=30))
    await _open_paid_offer(db, appt)
    body = _offer_text()

    await _run(appt, extra=body, allow_paid=True)

    [sent] = wa.templates
    assert sent["template"] == get_settings().RELEASE_OFFER_TEMPLATE_NAME
    assert sent["variables"] == [release_offer.template_text(body)]
    assert sent["button_payloads"] == [bid for bid, _ in release_offer.offer_buttons(appt.id)]


async def test_an_offer_already_answered_sends_nothing(db, tenant, wa, metered):
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=2))
    await _open_paid_offer(db, appt)
    async with db() as session:
        offer = await session.scalar(select(DepositReleaseOffer))
        offer.state = "kept"
        await session.commit()

    await _run(appt, extra=_offer_text())

    assert wa.buttons == [] and wa.templates == []
```

(`select`, `uuid4`, `datetime`, `UTC`, `timedelta`, `Appointment`, `AppointmentStatus`, `Tenant` já são usados no arquivo; se algum faltar, importe-o no topo junto dos demais.)

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_cancellation_notice.py -q -p no:cacheprovider`
Expected: FAIL nos 3 novos (botões ainda `rebooksame|…`; modelo ainda `CANCEL_TEMPLATE_NAME`; a oferta respondida ainda recebe o cartão).

- [ ] **Step 3: Implementar**

Em `src/secretaria/workers/whatsapp/notifications.py`:

1. Troque `from secretaria.services import cancellation_notice` por:

```python
from secretaria.models.deposit_release_offer import DECIDING_STATES
from secretaria.services import cancellation_notice, release_offer
```

2. Dentro do `async with async_session_factory() as session:` de `send_cancellation_notice`, logo depois de `waba_token = await get_waba_token(session, tenant.id)`, adicione:

```python
        # TASK-052: a slot the clinic released with a PAID deposit carries the patient's choice
        # ("Manter horário" / "Remarcar" / "Cancelar") and `extra_notice` IS the whole text. Read
        # here, by the job, so the API's queued arguments never changed (old worker = old card).
        offer = await release_offer.find_offer(
            session, tenant_id=tenant.id, released_appointment_id=appointment.id
        )
        offer_state = offer.state if offer is not None else None
```

3. Logo ANTES de `if not await _claim_event(key):`, adicione:

```python
    if offer_state is not None and offer_state not in DECIDING_STATES:
        # The patient already answered (e.g. on the Portal e-mail link): nothing to offer.
        logger.info(
            "cancellation_notice_skipped", reason="offer_answered", appointment_id=appointment_id
        )
        return
```

4. Dentro do `try:` do envio, troque a primeira linha `if inside:` (a do `client.send_buttons(... rebooking_invitation() ...)`) por:

```python
        if offer_state is not None and extra_notice:
            choices = release_offer.offer_buttons(appointment_id)
            if inside:
                await client.send_buttons(to=to, body=extra_notice, buttons=choices)
            else:
                # PENDÊNCIA META: RELEASE_OFFER_TEMPLATE_NAME is not approved yet (config.py);
                # until it is, this send fails, is retried/escalated like any other, and the B
                # deadline e-mails still cover the patient.
                await client.send_template(
                    to=to,
                    template=settings.RELEASE_OFFER_TEMPLATE_NAME,
                    lang=cancellation_notice.meta_language_code(tenant.language),
                    variables=[release_offer.template_text(extra_notice)],
                    button_payloads=[button_id for button_id, _label in choices],
                )
        elif inside:
```

(o corpo do antigo `if inside:` e o `else:` seguem iguais, agora como `elif inside:` / `else:`.)

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_cancellation_notice.py tests/test_workers_layering.py tests/test_build_identity.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/workers/whatsapp/notifications.py tests/test_cancellation_notice.py`
Expected: PASS (os 3 novos e todos os antigos), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/workers/whatsapp/notifications.py tests/test_cancellation_notice.py
git commit -m "feat(whatsapp): the release notice of a paid slot carries keep/reschedule/cancel, same job arguments (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 8: A lista de dias mostra dias com horários segurados, e a escolha do dia diz quais

(Sem mudança em relação ao desenho anterior; reproduzido aqui por completo.)

**Files:**
- Modify: `src/secretaria/services/availability.py` (`held_slots` + nota do módulo), `src/secretaria/services/flow_router.py` (`enter_day_picker` sem `holds=`; `_enter_slot_picker` com o aviso; `HELD_SLOTS_NOTICE`, `ALL_HELD_NOTICE`, `HELD_TIMES_SHOWN`, `held_times_text`)
- Test: `tests/test_before_booking_flow.py` (o teste do dia escondido vira o do dia mostrado), `tests/test_availability.py` (2 novos)

**Interfaces:**
- Produces: `availability.held_slots(slots, holds, *, duration_minutes, tz) -> list[dict]`; `flow_router.held_times_text(labels: list[str]) -> str`; `HELD_SLOTS_NOTICE`/`ALL_HELD_NOTICE` (com `{times}`). O dia todo segurado responde a lista "Ver opções" só com "Outro dia" (e voltar), no `slot_step` do ramo.

- [ ] **Step 1: Escrever os testes**

Em `tests/test_before_booking_flow.py`, o teste `test_the_day_picker_treats_held_windows_as_busy_time` passa a se chamar `test_a_fully_held_day_is_offered_and_says_which_times_are_held` e termina assim (o início — mundo, reserva de outro paciente em `held_day`/`held_local` e `_ACTIVE_GATE` — fica igual):

```diff
-async def test_the_day_picker_treats_held_windows_as_busy_time(db, env):
+async def test_a_fully_held_day_is_offered_and_says_which_times_are_held(db, env):
+    """TASK-052 (owner decision 2026-10-11) reverts the TASK-050 port that hid it: the day is
+    offered (an unpaid Pix may expire), and picking it lists the held TIMES - never who."""
     from secretaria.models import Patient
@@
         result = await fr.enter_day_picker(
             _snapshot(world.conversation), world.tenant, calendar, duration_minutes=30
         )
+        slot_step = await fr._enter_slot_picker(
+            _snapshot(world.conversation),
+            world.tenant,
+            calendar,
+            held_day,
+            duration_minutes=30,
+        )
     finally:
         fr._ACTIVE_GATE.reset(token)
-    # Ported onto main's availability module (TASK-050): the fully held day is not offered.
     assert len(calendar.day_kwargs) == 1
-    assert result.flow_state == FlowState.MENU
-    assert result.bubbles[0].body == fr.NO_AVAILABLE_DAYS_MESSAGE
+    assert result.flow_state != FlowState.MENU
+    day_rows = [row[0] for row in result.bubbles[0].rows]
+    assert f"day|{held_day.date().isoformat()}|0" in day_rows
+    (bubble,) = slot_step.bubbles
+    label = held_local.strftime("%H:%M")
+    assert bubble.body.startswith(f"Todos os horários livres deste dia ({label})")
+    assert "pagamento do sinal" in bubble.body and "voltam a ficar livres" in bubble.body
+    assert "5511888888888" not in bubble.body  # times only, never the other patient
+    assert [row[0] for row in bubble.rows] == ["dayagain|0"]  # nothing held is tappable
```

Em `tests/test_availability.py`, antes de `test_a_sole_professionals_hold_is_looked_up_by_their_id`:

```python
async def test_a_partly_held_day_lists_the_free_times_under_the_held_ones():
    """TASK-052: the held times are named above the list (times only), the rest is tappable."""
    res = await fr.route(
        _at_day_step(),
        _tenant(),
        _FakeCalendar(slots=_SLOTS),
        _DAY_TAP,
        gate=_Gate({None: [_HELD_0800]}),
    )
    (bubble,) = res.bubbles
    assert bubble.body == (
        fr.HELD_SLOTS_NOTICE.format(times="08:00") + "\n\nHorários livres em 05/10:"
    )
    assert [row[0] for row in bubble.rows][:1] == ["slot|2026-10-05T08:40"]


def test_held_times_are_listed_plainly_and_capped():
    assert fr.held_times_text(["08:00"]) == "08:00"
    assert fr.held_times_text(["08:00", "08:30"]) == "08:00 e 08:30"
    assert fr.held_times_text(["08:00", "08:30", "09:00"]) == "08:00, 08:30 e 09:00"
    many = [f"{h:02d}:00" for h in range(8, 19)]
    assert fr.held_times_text(many).endswith("e mais 3")
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_before_booking_flow.py tests/test_availability.py -q -p no:cacheprovider`
Expected: FAIL (`flow_state == MENU` com `NO_AVAILABLE_DAYS_MESSAGE`; `held_times_text`/`HELD_SLOTS_NOTICE` inexistentes).

- [ ] **Step 3: Implementar**

Em `src/secretaria/services/availability.py`, troque o parágrafo final da docstring do módulo (o que começa em "The day picker passes no holds, on purpose") por:

```text
The day picker passes no holds, on purpose: it lists every day with free Google time and
lets the slot step hide the held slots AND say which times are held (`held_slots`), so a
patient can see that a day may free up when an unpaid Pix expires (TASK-052, owner decision
2026-10-11, reverting the TASK-050 port that hid fully held days). The AI readers pass the
holds.
```

e, logo depois da função `without_holds`, adicione:

```python
def held_slots(
    slots: Sequence[dict],
    holds: Sequence[Window],
    *,
    duration_minutes: int,
    tz: tzinfo | None,
) -> list[dict]:
    """The complement of `without_holds`: the slots another conversation is holding."""
    if not holds:
        return []
    free = without_holds(slots, holds, duration_minutes=duration_minutes, tz=tz)
    kept = {id(slot) for slot in free}
    return [slot for slot in slots if id(slot) not in kept]
```

Em `src/secretaria/services/flow_router.py`:

```diff
@@ (import de services.availability)
 from secretaria.services.availability import (
     available_day_starts,
+    held_slots,
     slot_scan_limit,
     without_holds,
 )
@@ (logo depois de DAY_NO_LONGER_FREE_PREFIX)
 DAY_NO_LONGER_FREE_PREFIX = "Esse dia não tem mais horário livre."
+# TASK-052 (owner decision 2026-10-11): a day whose free times are held by other patients'
+# unfinished bookings (an unpaid Pix, or an e-mail code still pending) is offered, and picking it
+# says which times are held - never by whom - and that they may free up.
+HELD_SLOTS_NOTICE = (
+    "Os horários {times} deste dia estão reservados para outro paciente, aguardando a "
+    "confirmação ou o pagamento do sinal. Se o prazo terminar sem pagamento, eles voltam a "
+    "ficar livres."
+)
+ALL_HELD_NOTICE = (
+    "Todos os horários livres deste dia ({times}) estão reservados para outro paciente, "
+    "aguardando a confirmação ou o pagamento do sinal. Se o prazo terminar sem pagamento, eles "
+    "voltam a ficar livres: tente de novo mais tarde ou escolha outro dia."
+)
+HELD_TIMES_SHOWN = 8
+
+
+def held_times_text(labels: list[str]) -> str:
+    """"08:00", "08:00 e 08:30", "08:00, 08:30 e 09:00", "... e mais 3" - times only."""
+    shown = labels[:HELD_TIMES_SHOWN]
+    rest = len(labels) - len(shown)
+    if rest > 0:
+        return f"{', '.join(shown)} e mais {rest}"
+    if len(shown) <= 1:
+        return "".join(shown)
+    return f"{', '.join(shown[:-1])} e {shown[-1]}"
+
+
 NO_AVAILABLE_DAYS_MESSAGE = (
@@ (enter_day_picker)
     if calendar is None:
         return _calendar_unavailable(conversation, branch, step)
-    # feature/out-of-mvp (TASK-017, ported by TASK-050): a Pix keeps a slot HELD for 30-120
-    # minutes, so a day whose every free slot is held must not be offered - the patient would
-    # tap it and land straight back here. With no holds this renders exactly as on main
-    # (services/availability.py's module note); with holds only the days they touch are
-    # re-read slot by slot.
-    reserved = await _hold_windows(_hold_owner(conversation, professionals))
+    # TASK-052 (owner decision 2026-10-11): a day whose free slots are held by another
+    # conversation (an unpaid Pix keeps one for 30-120 minutes) IS offered, as on main; the
+    # slot step hides the held times and says which they are (`_enter_slot_picker`).
     try:
@@ (a chamada de available_day_starts dentro de enter_day_picker)
             duration_minutes=duration_minutes,
-            holds=reserved,
         )
@@ (_enter_slot_picker, no bloco "if reserved:")
+    held: list[dict] = []
     if reserved:
         before = len(slots)
+        held = held_slots(
+            slots, reserved, duration_minutes=duration_minutes, tz=calendar.tzinfo
+        )
         slots = without_holds(
             slots, reserved, duration_minutes=duration_minutes, tz=calendar.tzinfo
         )
@@ (_enter_slot_picker, logo antes de "if not slots:" que volta para enter_day_picker)
+    held_labels = [str(s.get("label") or "") for s in held if s.get("label")]
+    if not slots and held_labels:
+        # TASK-052: every free time of the day is held by someone else. The day stays offered
+        # (it may free up), so going back to the day list would loop: say why, offer the day
+        # list and the back control, nothing to tap on this day.
+        controls: list[tuple[str, str]] = [(f"{ROW_DAY_AGAIN_PREFIX}{page}", LABEL_ANOTHER_DAY)]
+        if back_target:
+            controls.append((f"{ROW_DAY_BACK_PREFIX}{back_target}", _day_back_label(back_target)))
+        return FlowRouterResult(
+            action="reply",
+            bubbles=[
+                SlotsBubble(
+                    body=ALL_HELD_NOTICE.format(times=held_times_text(held_labels))[
+                        :MAX_LIST_BODY_CHARS
+                    ],
+                    rows=controls[:MAX_LIST_ROWS],
+                    button_label="Ver opções",
+                    section_title="Opções",
+                )
+            ],
+            flow_state=branch.flow_state,
+            flow_step=branch.slot_step,
+            flow_selected_day=target.date().isoformat(),
+            **_day_branch_fields(conversation, branch),
+        )
+
     if not slots:
         return await enter_day_picker(
@@ (_enter_slot_picker, o corpo da SlotsBubble dos horários)
             SlotsBubble(
-                body=f"{branch.slot_body_prefix} {target.strftime('%d/%m')}:",
+                body=(
+                    f"{HELD_SLOTS_NOTICE.format(times=held_times_text(held_labels))}\n\n"
+                    if held_labels
+                    else ""
+                )
+                + f"{branch.slot_body_prefix} {target.strftime('%d/%m')}:",
                 rows=rows[:MAX_LIST_ROWS],
                 button_label="Ver horários",
                 section_title="Horários livres",
```

(Os nomes `ROW_DAY_AGAIN_PREFIX`, `LABEL_ANOTHER_DAY`, `ROW_DAY_BACK_PREFIX`, `_day_back_label`, `page`, `back_target`, `target`, `MAX_LIST_BODY_CHARS`, `MAX_LIST_ROWS`, `_day_branch_fields` são os que `_enter_slot_picker` já usa; este diff foi aplicado e verificado no plano anterior sobre `3650e10`.)

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_before_booking_flow.py tests/test_availability.py tests/test_rebooking_flow.py tests/test_booking_gate_deposit.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/services/availability.py src/secretaria/services/flow_router.py tests/test_before_booking_flow.py tests/test_availability.py`
Expected: PASS, ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/services/availability.py src/secretaria/services/flow_router.py tests/test_before_booking_flow.py tests/test_availability.py
git commit -m "feat(flow): offer days whose free times are held and name the held times when the day is picked (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 9: Os toques — Manter / Remarcar / Cancelar / Recebido / Não recebido (WhatsApp e Portal)

**Files:**
- Create: `src/secretaria/workers/shared/release_offer_actions.py`
- Modify: `src/secretaria/schemas/webhook.py` (`_ACTION_BUTTON_PREFIXES`), `src/secretaria/workers/portal/inbound.py` (ação aceita), `src/secretaria/workers/shared/actions.py` (chama os toques antes de tudo; frase do topo no "Outro horário"), `src/secretaria/services/flow_router.py` (`enter_rebooking`: o ramo "mesmo médico" passa o `prefix`)
- Test: `tests/test_release_offer_buttons.py` (novo)

**Interfaces:**
- Consumes: Task 3 (transições e textos), Task 4 (`keep_slot`, `KEEP_*`), Task 5 (`dispatch_due`); `reminder_hooks.after_appointment_booked(appointment_id)`; `_reply_sender(reply, tenant, waba_token)`, `get_waba_token`, `async_session_factory`.
- Produces: `async handle_release_offer_tap(reply, action, appointment_id: UUID) -> tuple[str, str | None] | None` — `None` = respondido aqui; senão `(ação a seguir, frase do topo)`: `("rebooksame", RESCHEDULE_PREFIX | RESCHEDULE_AFTER_REFUND_PREFIX)` entra no "Outro horário"; `("rebookno", None)` entra na pergunta de motivo.

- [ ] **Step 1: Escrever o teste que falha**

`tests/test_release_offer_buttons.py`:

```python
"""The patient's taps on a slot the clinic released with a PAID deposit (TASK-052).

WhatsApp and the Portal reach the same handler (workers/shared/release_offer_actions.py through
`_handle_action_button`). A tap on someone else's offer, or another clinic's, is answered like an
unknown appointment and changes nothing.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from secretaria.config import get_settings
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    FlowState,
    Patient,
    PaymentAttentionItem,
    PixDeposit,
)
from secretaria.models.deposit_release_offer import DepositReleaseEmail, DepositReleaseOffer
from secretaria.services import booking_promotion, email, reminder_hooks, release_offer
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, BrainMessageSender
from secretaria.services.flow_router import STEP_DECLINE_REASON
from secretaria.workers import tasks
from secretaria.workers.orchestrator import _send_bot_reply
from secretaria.workers.portal.inbound import _persist_brain_message_inbound
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import consent, sent, wire
from tests._reminders_v2 import outbound_messages, seed_paid_deposit, seed_world


class _OwnerCalendar:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.created: list[str] = []

    async def check_availability(self, start, end, max_results=50):
        return list(self.events)

    async def create_event(
        self, start, end, summary, description="", reminders=None, *, event_id=None
    ):
        self.created.append(event_id)
        return {"id": event_id, "htmlLink": "https://calendar.google.com/x"}

    async def cancel_event(self, event_id):
        return None


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


@pytest.fixture
def owner(monkeypatch) -> _OwnerCalendar:
    calendar = _OwnerCalendar()

    async def _for_booking(session, tenant, professional_id):
        return calendar

    monkeypatch.setattr(booking_promotion, "calendar_for_booking", _for_booking)
    return calendar


@pytest.fixture
def mailbox(monkeypatch) -> list[dict]:
    box: list[dict] = []
    monkeypatch.setattr(
        email, "_send_sync", lambda to, subject, body: box.append({"to": to, "subject": subject})
    )
    monkeypatch.setattr(get_settings(), "SMTP_HOST", "smtp.exemplo", raising=False)
    return box


@pytest.fixture
def booked(monkeypatch) -> list:
    calls: list = []

    async def _after(appointment_id, *, now=None):
        calls.append(appointment_id)
        return 0

    monkeypatch.setattr(reminder_hooks, "after_appointment_booked", _after)
    return calls


async def _released(db, **kwargs):  # noqa: F811
    """A world whose appointment the clinic released with a PAID deposit, offer open."""
    world = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=2), **kwargs)
    await seed_paid_deposit(db, world)
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.status = AppointmentStatus.CANCELLED
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == appointment.id)
        )
        tenant = await session.get(type(world.tenant), world.tenant.id)
        tenant.contact_email = "clinica@exemplo.com"
        await release_offer.open_offer(
            session, tenant=tenant, appointment=appointment, deposit=deposit
        )
        await session.commit()
    return world


def _reply(world, conversation_id=None):
    return tasks._ReplyContext(
        conversation_id=conversation_id or world.conversation.id,
        patient_ref=world.patient.external_id or world.patient.wa_id,
        inbound_body="",
    )


async def _tap(world, action, conversation_id=None):
    await tasks._handle_action_button(
        _reply(world, conversation_id), action, str(world.appointment.id)
    )


async def _offer(db, world) -> DepositReleaseOffer:  # noqa: F811
    async with db() as session:
        return await session.scalar(
            select(DepositReleaseOffer).where(
                DepositReleaseOffer.released_appointment_id == world.appointment.id
            )
        )


async def _items(db):  # noqa: F811
    async with db() as session:
        return list(await session.scalars(select(PaymentAttentionItem)))


async def _sends(db) -> dict:  # noqa: F811
    async with db() as session:
        return {
            (row.kind, row.seq): row.outcome
            for row in await session.scalars(select(DepositReleaseEmail))
        }


def _texts() -> list[str]:
    return [entry[2] for entry in sent() if entry[0] == "text"]


async def test_manter_on_a_free_slot_keeps_it_and_tells_the_clinic(db, owner, mailbox, booked):  # noqa: F811
    world = await _released(db)

    await _tap(world, "relkeep")

    assert _texts()[-1].startswith("Pronto! Seu horário de")
    offer = await _offer(db, world)
    assert offer.state == "kept" and booked == [offer.new_appointment_id]
    assert await _sends(db) == {("clinic_patient_keeps", 0): "sent"}  # sent right after the tap
    assert "mantém o horário" in mailbox[0]["subject"]


async def test_manter_on_an_occupied_slot_offers_remarcar_or_cancelar(db, owner, mailbox):  # noqa: F811
    world = await _released(db)
    owner.events = [{"id": "evt-another-patient"}]

    await _tap(world, "relkeep")

    kind, _to, body, buttons = sent()[-1]
    assert kind == "buttons" and body == release_offer.KEEP_TAKEN_TEXT
    assert [bid for bid, _ in buttons] == [
        f"relresched|{world.appointment.id}",
        f"relcancel|{world.appointment.id}",
    ]
    assert (await _offer(db, world)).state == "open" and mailbox == []


async def test_remarcar_opens_the_day_list_with_the_carry_over_line(db):  # noqa: F811
    world = await _released(db)

    await _tap(world, "relresched")

    kind, _to, body, _label, rows, _section = sent()[-1]
    assert kind == "list" and body.startswith(release_offer.RESCHEDULE_PREFIX)
    assert rows[0][0].startswith("day|")
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
    assert conversation.flow_state == FlowState.SERVICE_CATALOG
    assert conversation.flow_selected_type == "Consulta"
    assert (await _offer(db, world)).state == "rescheduling"
    assert await _items(db) == []


async def test_cancelar_tells_the_clinic_now_and_asks_why(db, mailbox):  # noqa: F811
    world = await _released(db)

    await _tap(world, "relcancel")

    assert any("sua consulta foi cancelada" in text and "R$ 100,00" in text for text in _texts())
    offer = await _offer(db, world)
    assert (offer.state, offer.refund_source) == ("refund_due", "patient")
    assert len(await _items(db)) == 1
    sends = await _sends(db)
    assert sends[("clinic_refund_now", 0)] == "sent"  # A, right after the tap
    assert sends[("clinic_refund_deadline", 0)] is None  # B waits for its time
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
    assert conversation.flow_step == STEP_DECLINE_REASON  # and the "why" list follows


async def test_cancelar_twice_says_it_was_already_asked(db, mailbox):  # noqa: F811
    world = await _released(db)

    await _tap(world, "relcancel")
    await _tap(world, "relcancel")

    assert _texts()[-1] == release_offer.TAP_REFUND_ALREADY_TEXT
    assert len(await _items(db)) == 1 and len(mailbox) == 1


async def test_manter_after_cancelar_is_refused(db, owner, mailbox):  # noqa: F811
    world = await _released(db)

    await _tap(world, "relcancel")
    await _tap(world, "relkeep")

    assert _texts()[-1] == release_offer.TAP_KEEP_AFTER_CANCEL_TEXT
    assert (await _offer(db, world)).state == "refund_due" and owner.created == []


async def test_cancelar_after_manter_does_not_cancel_the_kept_consultation(  # noqa: F811
    db, owner, mailbox, booked
):
    world = await _released(db)

    await _tap(world, "relkeep")
    await _tap(world, "relcancel")

    assert _texts()[-1] == release_offer.TAP_CANCEL_AFTER_KEEP_TEXT
    offer = await _offer(db, world)
    assert offer.state == "kept" and await _items(db) == []


async def test_a_tap_past_b_settles_the_deadline_first(db, owner, mailbox):  # noqa: F811
    world = await _released(db)
    async with db() as session:
        offer = await session.scalar(select(DepositReleaseOffer))
        offer.decide_by_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    await _tap(world, "relkeep")

    offer = await _offer(db, world)
    assert (offer.state, offer.refund_source) == ("refund_due", "timeout")
    assert _texts()[-1] == release_offer.TAP_KEEP_AFTER_DEADLINE_TEXT
    assert len(await _items(db)) == 1 and owner.created == []


async def test_recebido_and_nao_recebido_after_the_check(db, mailbox):  # noqa: F811
    world = await _released(db)
    await _tap(world, "relgot")
    assert _texts()[-1] == release_offer.CHECK_TOO_EARLY_TEXT  # nothing to confirm yet

    await _tap(world, "relcancel")
    async with db() as session:
        offer = await session.scalar(select(DepositReleaseOffer))
        offer.patient_check_sent_at = datetime.now(UTC)
        await session.commit()

    await _tap(world, "relmiss")
    assert _texts()[-1] == release_offer.NOT_RECEIVED_TEXT
    await _tap(world, "relmiss")
    assert _texts()[-1] == release_offer.NOT_RECEIVED_RECENT_TEXT
    assert (await _sends(db))[("clinic_refund_reminder", 1)] == "sent"
    assert ("clinic_refund_reminder", 2) not in await _sends(db)

    await _tap(world, "relgot")
    assert _texts()[-1] == release_offer.RECEIVED_TEXT
    offer = await _offer(db, world)
    assert (offer.state, offer.closed_reason) == ("refund_closed", "patient_received")
    await _tap(world, "relgot")
    assert _texts()[-1] == release_offer.CHECK_CLOSED_TEXT


async def test_another_patients_tap_is_an_unknown_appointment_and_changes_nothing(db):  # noqa: F811
    world = await _released(db)
    async with db() as session:
        intruder = Patient(id=uuid4(), tenant_id=world.tenant.id, wa_id="5511900000009")
        session.add(intruder)
        await session.flush()
        theirs = Conversation(id=uuid4(), tenant_id=world.tenant.id, patient_id=intruder.id)
        session.add(theirs)
        await session.commit()

    await _tap(world, "relcancel", conversation_id=theirs.id)

    assert _texts()[-1] == release_offer.NOT_FOUND_TEXT
    assert (await _offer(db, world)).state == "open" and await _items(db) == []


async def test_another_clinics_offer_is_never_reached(db):  # noqa: F811
    world = await _released(db)
    other = await seed_world(db, phone_number_id="pnid-2", wa_id="5511900000002")

    await tasks._handle_action_button(
        _reply(other, conversation_id=other.conversation.id), "relcancel", str(world.appointment.id)
    )

    assert (await _offer(db, world)).state == "open" and await _items(db) == []


async def test_a_portal_tap_on_the_recorded_card_is_the_same_action(db, mailbox):  # noqa: F811
    world = await _released(db, channel=CHANNEL_BRAIN_MESSAGE)
    await consent(db, world)
    async with db() as session:
        sender = BrainMessageSender(conversation_id=world.conversation.id, session=session)
        await sender.send_buttons(
            to=world.patient.external_id,
            body=release_offer.OFFER_HEAD,
            buttons=release_offer.offer_buttons(world.appointment.id),
        )
        await session.commit()

    reply = await _persist_brain_message_inbound(
        tenant_id=world.tenant.id,
        external_id=world.patient.external_id,
        text="Cancelar",
        interactive_reply_id=f"relcancel|{world.appointment.id}",
    )
    await _send_bot_reply(reply)

    assert reply.action_button == ("relcancel", str(world.appointment.id))
    assert (await _offer(db, world)).state == "refund_due"
    bodies = [m.body for m in await outbound_messages(db, world.conversation.id)]
    assert any("sua consulta foi cancelada" in body for body in bodies)


async def test_a_portal_id_never_shown_on_a_card_is_not_an_action(db):  # noqa: F811
    world = await _released(db, channel=CHANNEL_BRAIN_MESSAGE)
    await consent(db, world)

    reply = await _persist_brain_message_inbound(
        tenant_id=world.tenant.id,
        external_id=world.patient.external_id,
        text="Cancelar",
        interactive_reply_id=f"relcancel|{world.appointment.id}",
    )

    assert reply.action_button is None
    assert (await _offer(db, world)).state == "open"
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_offer_buttons.py -q -p no:cacheprovider`
Expected: FAIL (`rel*|` ainda não são ids de ação: o toque não muda a oferta nem responde).

- [ ] **Step 3: Implementar**

`src/secretaria/workers/shared/release_offer_actions.py`:

```python
"""release_offer_actions - the patient's taps on a released PAID slot (TASK-052).

Channel-neutral: WhatsApp and the Portal reach it through `actions._handle_action_button`, which
calls it first for the `rel*|<released appointment id>` family:

* "Manter horário" (`relkeep`) -> services/release_keep.py; free: kept + clinic e-mail; taken or
  past: Remarcar / Cancelar again.
* "Remarcar" (`relresched`) -> `rescheduling`, then the caller enters "Outro horário" with the
  line above the day list.
* "Cancelar" (`relcancel`) -> refund due, A e-mail right away, then the caller asks why.
* "Recebido" / "Não recebido" (`relgot` / `relmiss`) -> close, or one more clinic e-mail.

Before any of them a tap past the B deadline settles it exactly as the sweep would. A tap whose
offer is missing, belongs to another patient or another clinic is answered like an unknown
appointment - nothing is revealed and nothing changes. Returns None when answered here, or
`(action, prefix)` for the caller to continue with ("rebooksame" / "rebookno").
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Conversation, PixDeposit, Tenant
from secretaria.models.deposit_release_offer import (
    DECIDING_STATES,
    REFUND_SOURCE_TIMEOUT,
    STATE_KEPT,
    STATE_REBOOKED,
    STATE_REFUND_CLOSED,
    STATE_REFUND_DUE,
    DepositReleaseOffer,
)
from secretaria.services import release_keep, release_offer, release_offer_dispatch, reminder_hooks
from secretaria.services.payments.money import format_brl
from secretaria.services.tenant_config import get_waba_token
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.sender import _reply_sender

logger = get_logger(__name__)


@dataclass
class _Answer:
    text: str | None = None
    buttons: list[tuple[str, str]] | None = None
    follow: tuple[str, str | None] | None = None
    dispatch: bool = False
    booked: UUID | None = None


async def handle_release_offer_tap(
    reply: _ReplyContext, action: str, appointment_id: UUID
) -> tuple[str, str | None] | None:
    if reply.conversation_id is None:
        return None
    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        tenant = await session.get(Tenant, conversation.tenant_id) if conversation else None
        if tenant is None:
            return None
        # Plain values up front: "Manter" may roll the session back (release_keep), which
        # expires every loaded row, and nothing below may lazy-load.
        timezone, tenant_id = tenant.timezone, tenant.id
        conversation_id = conversation.id
        client = _reply_sender(reply, tenant, await get_waba_token(session, tenant.id))
        offer = await release_offer.find_offer(
            session, tenant_id=tenant.id, released_appointment_id=appointment_id
        )
        if offer is None or offer.patient_id is None or offer.patient_id != conversation.patient_id:
            if client is not None:
                await client.send_text_message(
                    to=reply.patient_ref, body=release_offer.NOT_FOUND_TEXT
                )
            return None
        offer_id = offer.id
        now = datetime.now(UTC)
        settled = await release_offer.settle_if_expired(session, offer=offer, now=now)
        if settled:
            await session.commit()
        offer = await session.get(DepositReleaseOffer, offer_id, populate_existing=True)

        if action == release_offer.ACTION_KEEP:
            answer = await _on_keep(session, tenant, offer, now, timezone)
        elif action == release_offer.ACTION_RESCHEDULE:
            answer = await _on_reschedule(session, offer, conversation_id, now, timezone)
        elif action == release_offer.ACTION_CANCEL:
            answer = await _on_cancel(session, offer, now, timezone)
        elif action == release_offer.ACTION_RECEIVED:
            answer = await _on_received(session, offer, now, timezone)
        else:
            answer = await _on_not_received(session, offer, now, timezone)
        answer.dispatch = answer.dispatch or settled

        if client is not None and answer.text:
            if answer.buttons:
                await client.send_buttons(
                    to=reply.patient_ref, body=answer.text, buttons=answer.buttons
                )
            else:
                await client.send_text_message(to=reply.patient_ref, body=answer.text)
        logger.info(
            "deposit_release_offer_tap",
            tenant_id=str(tenant_id),
            offer_id=str(offer_id),
            action=action,
        )

    # After the session closed (never two sessions open on one engine).
    if answer.booked is not None:
        await reminder_hooks.after_appointment_booked(answer.booked)
    if answer.dispatch:
        try:
            async with async_session_factory() as session:
                await release_offer_dispatch.dispatch_due(session, offer_id=offer_id)
        except Exception as exc:  # noqa: BLE001 - the sweep retries what did not go now
            logger.warning("deposit_release_offer_dispatch_failed", error_type=type(exc).__name__)
    return answer.follow


async def _amount(session, offer: DepositReleaseOffer) -> str:
    deposit = await session.get(PixDeposit, offer.deposit_id)
    return format_brl(deposit.amount_cents) if deposit is not None else ""


async def _state_answer(session, offer: DepositReleaseOffer, action: str, timezone) -> _Answer:
    """The answer when the offer is no longer deciding (kept, rebooked, refund due/closed)."""
    if offer.state == STATE_KEPT:
        if action == release_offer.ACTION_CANCEL:
            return _Answer(text=release_offer.TAP_CANCEL_AFTER_KEEP_TEXT)
        if action == release_offer.ACTION_KEEP:
            kept = (
                await session.get(Appointment, offer.new_appointment_id)
                if offer.new_appointment_id
                else None
            )
            when = (
                release_offer.slot_text(kept.start_at, timezone)
                if kept is not None and kept.start_at is not None
                else ""
            )
            return _Answer(text=release_offer.TAP_ALREADY_KEPT_TEXT.format(when=when))
        return _Answer(text=release_offer.TAP_ALREADY_REBOOKED_TEXT)
    if offer.state == STATE_REBOOKED:
        if action == release_offer.ACTION_CANCEL:
            return _Answer(text=release_offer.TAP_CANCEL_AFTER_KEEP_TEXT)
        return _Answer(text=release_offer.TAP_ALREADY_REBOOKED_TEXT)
    # refund_due / refund_closed
    if action == release_offer.ACTION_KEEP:
        return _Answer(
            text=release_offer.TAP_KEEP_AFTER_DEADLINE_TEXT
            if offer.refund_source == REFUND_SOURCE_TIMEOUT
            else release_offer.TAP_KEEP_AFTER_CANCEL_TEXT
        )
    if action == release_offer.ACTION_RESCHEDULE:
        return _Answer(follow=("rebooksame", release_offer.RESCHEDULE_AFTER_REFUND_PREFIX))
    return _Answer(text=release_offer.TAP_REFUND_ALREADY_TEXT)


async def _on_keep(session, tenant, offer, now, timezone) -> _Answer:
    offer_id, released_id = offer.id, offer.released_appointment_id
    if offer.state in DECIDING_STATES:
        result = await release_keep.keep_slot(session, tenant=tenant, offer=offer, now=now)
        if result.outcome == release_keep.KEEP_KEPT:
            when = release_offer.slot_text(result.appointment.start_at, timezone)
            return _Answer(
                text=release_offer.KEEP_DONE_TEXT.format(when=when),
                dispatch=True,
                booked=result.appointment.id,
            )
        if result.outcome in (release_keep.KEEP_TAKEN, release_keep.KEEP_PAST):
            text = (
                release_offer.KEEP_TAKEN_TEXT
                if result.outcome == release_keep.KEEP_TAKEN
                else release_offer.KEEP_PAST_TEXT
            )
            return _Answer(text=text, buttons=release_offer.keep_fallback_buttons(released_id))
        if result.outcome == release_keep.KEEP_UNAVAILABLE:
            return _Answer(text=release_offer.KEEP_UNAVAILABLE_TEXT)
        # KEEP_CLOSED: someone else won meanwhile (the sweep or another tap); the row may have
        # been expired by a rollback, so it is re-read by its id.
        offer = await session.get(DepositReleaseOffer, offer_id, populate_existing=True)
    return await _state_answer(session, offer, release_offer.ACTION_KEEP, timezone)


async def _on_reschedule(session, offer, conversation_id, now, timezone) -> _Answer:
    if await release_offer.choose_reschedule(
        session, offer=offer, conversation_id=conversation_id, now=now
    ):
        await session.commit()
        return _Answer(follow=("rebooksame", release_offer.RESCHEDULE_PREFIX))
    offer = await session.get(DepositReleaseOffer, offer.id, populate_existing=True)
    return await _state_answer(session, offer, release_offer.ACTION_RESCHEDULE, timezone)


async def _on_cancel(session, offer, now, timezone) -> _Answer:
    if await release_offer.choose_cancel(session, offer=offer, now=now):
        await session.commit()
        amount = await _amount(session, offer)
        return _Answer(
            text=release_offer.CANCEL_DONE_TEXT.format(amount=amount),
            follow=("rebookno", None),
            dispatch=True,
        )
    offer = await session.get(DepositReleaseOffer, offer.id, populate_existing=True)
    return await _state_answer(session, offer, release_offer.ACTION_CANCEL, timezone)


def _not_refund_text(offer: DepositReleaseOffer) -> str:
    if offer.state in (STATE_KEPT, STATE_REBOOKED):
        return release_offer.TAP_ALREADY_REBOOKED_TEXT
    return release_offer.CHECK_TOO_EARLY_TEXT


async def _on_received(session, offer, now, timezone) -> _Answer:
    outcome = await release_offer.record_received(session, offer=offer, now=now)
    await session.commit()
    if outcome == "closed":
        return _Answer(text=release_offer.RECEIVED_TEXT)
    if outcome == "already_closed":
        return _Answer(text=release_offer.CHECK_CLOSED_TEXT)
    if outcome == "too_early":
        return _Answer(text=release_offer.CHECK_TOO_EARLY_TEXT)
    current = await session.get(DepositReleaseOffer, offer.id, populate_existing=True)
    return _Answer(text=_not_refund_text(current))


async def _on_not_received(session, offer, now, timezone) -> _Answer:
    outcome = await release_offer.request_reminder(session, offer=offer, now=now)
    await session.commit()
    if outcome == "sent":
        return _Answer(text=release_offer.NOT_RECEIVED_TEXT, dispatch=True)
    if outcome == "recent":
        return _Answer(text=release_offer.NOT_RECEIVED_RECENT_TEXT)
    if outcome == "closed":
        return _Answer(text=release_offer.CHECK_CLOSED_TEXT)
    if outcome == "too_early":
        return _Answer(text=release_offer.CHECK_TOO_EARLY_TEXT)
    current = await session.get(DepositReleaseOffer, offer.id, populate_existing=True)
    if current is not None and current.state in (STATE_REFUND_DUE, STATE_REFUND_CLOSED):
        return _Answer(text=release_offer.CHECK_CLOSED_TEXT)
    return _Answer(text=_not_refund_text(current))
```

Em `src/secretaria/schemas/webhook.py`, dentro de `_ACTION_BUTTON_PREFIXES`, depois de `"remkeep|",`:

```python
    # TASK-052: the choice on a slot the clinic released with a PAID deposit, and the patient's
    # refund check. The trailing id is the RELEASED appointment's; workers/shared/
    # release_offer_actions.py honours it only for the patient of the offer, in the tapping
    # conversation's clinic.
    "relkeep|",
    "relresched|",
    "relcancel|",
    "relgot|",
    "relmiss|",
```

Em `src/secretaria/workers/portal/inbound.py`:

1. Depois de `from secretaria.services.reminder_text import REMINDER_ROW_ACTIONS`, adicione
   `from secretaria.services.release_offer import RELEASE_OFFER_ACTIONS` (na ordem alfabética dos imports, ANTES da linha de `reminder_text`).
2. Troque a condição `if decoded is not None and decoded[0] in REMINDER_ROW_ACTIONS:` por:

```python
                if decoded is not None and (
                    decoded[0] in REMINDER_ROW_ACTIONS or decoded[0] in RELEASE_OFFER_ACTIONS
                ):
                    # TASK-052: the choices on a released PAID slot are ACTIONS on this channel
                    # too (the card was recorded with its options, so the id was validated above).
```

Em `src/secretaria/workers/shared/actions.py`:

1. Depois de `from secretaria.services.payments import deposit_lifecycle`, adicione `from secretaria.services.release_offer import RELEASE_OFFER_ACTIONS`; e entre o bloco `from secretaria.workers.shared.llm_context import (...)` (o que traz `_appointment_calendar`) e `from secretaria.workers.shared.reminder_actions import (...)`, adicione `from secretaria.workers.shared.release_offer_actions import handle_release_offer_tap`.
2. Logo depois do bloco `if action in REMINDER_ROW_ACTIONS: ...` (o que termina em `appt_uuid = UUID(appointment_id)`), adicione:

```python
    # TASK-052: a slot the clinic released with a PAID deposit. Most taps are answered there;
    # "Remarcar" comes back as "rebooksame" with the line above the day list, "Cancelar" as
    # "rebookno" (the "why" list follows), both on the released appointment.
    offer_prefix: str | None = None
    if action in RELEASE_OFFER_ACTIONS:
        offer_tap = await handle_release_offer_tap(reply, action, appt_uuid)
        if offer_tap is None:
            return
        action, offer_prefix = offer_tap
```

3. No ramo `if action in ("rebooksame", "rebookother"):`, troque `prefix = None` por `prefix = offer_prefix`.

Em `src/secretaria/services/flow_router.py`, na função `enter_rebooking`, no ramo "mesmo médico" (a chamada `await enter_day_picker(...)` que passa `branch=BOOKING_DAY_BRANCH,` e `professionals=professionals,`), adicione o argumento:

```python
            # TASK-052: "Remarcar" on a released PAID slot says the deposit carries over.
            prefix=prefix,
```

(`enter_rebooking` já recebe `prefix`; hoje só o ramo "outro médico" o usa. Confira que `enter_day_picker` aceita `prefix=` — ele aceita, é o que o "Outro médico" usa.)

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_offer_buttons.py tests/test_action_buttons.py tests/test_rebooking_flow.py tests/test_reminder_r3_portal.py tests/test_brain_message_interactive_tap.py tests/test_reminder_v2_text.py tests/test_reminder_r3_ids.py tests/test_workers_layering.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/workers/shared/release_offer_actions.py src/secretaria/workers/shared/actions.py src/secretaria/schemas/webhook.py src/secretaria/workers/portal/inbound.py src/secretaria/services/flow_router.py tests/test_release_offer_buttons.py`
Expected: PASS (13 novos; botões, remarcação, Portal, ids dos lembretes e camadas seguem verdes), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/workers/shared/release_offer_actions.py src/secretaria/workers/shared/actions.py src/secretaria/schemas/webhook.py src/secretaria/workers/portal/inbound.py src/secretaria/services/flow_router.py tests/test_release_offer_buttons.py
git commit -m "feat(flow): keep/reschedule/cancel and received/not-received taps on a released paid slot (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 10: A nova consulta do "Remarcar" leva o sinal pago (fluxo, promoção do Portal e portão do "antes")

**Files:**
- Modify: `src/secretaria/workers/shared/flow_runner.py` (`_apply_flow_result`), `src/secretaria/workers/shared/booking_hold.py` (`_promote_booking_hold`), `src/secretaria/services/booking_hold.py` (`BookingGate._deposit_quote`)
- Test: `tests/test_release_offer_carry_over.py` (novo)

**Interfaces:**
- Consumes: Task 3 (`claim_carry_over`, `carry_over_pending`, `CARRIED_OVER_NOTE`, `REASON_CARRIED_OVER`, `settle_if_expired`/`expire_offers`), `deposit_gate.DepositQuote`.
- Produces: a consulta criada numa conversa com oferta em `rescheduling` (antes de B) recebe o sinal PAGO na mesma transação e a última bolha (ou a confirmação do Portal) ganha `CARRIED_OVER_NOTE`; o portão do "antes" responde `DepositQuote("no_deposit", "carried_over")` para essa conversa.

- [ ] **Step 1: Escrever o teste que falha**

`tests/test_release_offer_carry_over.py`:

```python
"""The booking that ends a "Remarcar" carries the released slot's PAID deposit (TASK-052).

The deposit moves to the new appointment in the booking's own transaction, without a new charge
and without touching `reschedule_count`; the `before` gate asks for no Pix while the offer is in
`rescheduling`; a booking made after the B deadline cancelled the offer stands on its own.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from secretaria.ai.formatter import TextBubble
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    BookingHold,
    FlowState,
    PaymentAttentionItem,
    PixDeposit,
    PixDepositStatus,
)
from secretaria.models.deposit_release_offer import DepositReleaseOffer
from secretaria.services import booking_hold as holds, release_offer
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.flow_router import FlowRouterResult
from secretaria.services.payments import deposit_gate, registry
from secretaria.workers import tasks
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import sent, wire
from tests._reminders_v2 import WA_ID, seed_paid_deposit, seed_world
from tests.before_support import entitlement_summary
from tests.payments_support import FakeProvider

CONFIRMED = "Pronto! Seu agendamento está confirmado."


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)
    monkeypatch.setattr(holds, "async_session_factory", db)


def _future(days: int) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


async def _rescheduling(db, *, tap: bool = True):  # noqa: F811
    world = await seed_world(db, start_at=_future(2), google_event_id="evt-released")
    await seed_paid_deposit(db, world)
    async with db() as session:
        released = await session.get(Appointment, world.appointment.id)
        released.status = AppointmentStatus.CANCELLED
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == released.id)
        )
        tenant = await session.get(type(world.tenant), world.tenant.id)
        offer = await release_offer.open_offer(
            session, tenant=tenant, appointment=released, deposit=deposit
        )
        if tap:
            await release_offer.choose_reschedule(
                session, offer=offer, conversation_id=world.conversation.id
            )
        await session.commit()
    return world


def _booking(start: datetime) -> FlowRouterResult:
    return FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=CONFIRMED)],
        flow_state=FlowState.IDLE,
        appointment={
            "google_event_id": "evt-new",
            "appointment_type": "Consulta",
            "start_at": start,
            "end_at": start + timedelta(minutes=30),
        },
    )


async def _confirm(world, result) -> None:
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="Confirmar"
    )
    await tasks._apply_flow_result(reply, result, WA_ID, tenant=world.tenant, waba_token="t")


async def _state(db):  # noqa: F811
    async with db() as session:
        new = await session.scalar(
            select(Appointment).where(Appointment.google_event_id == "evt-new")
        )
        deposit = await session.scalar(select(PixDeposit))
        offer = await session.scalar(select(DepositReleaseOffer))
        items = list(await session.scalars(select(PaymentAttentionItem)))
    return new, deposit, offer, items


async def test_the_new_booking_carries_the_paid_deposit_and_says_so(db):  # noqa: F811
    world = await _rescheduling(db)

    await _confirm(world, _booking(_future(6)))

    new, deposit, offer, items = await _state(db)
    assert new is not None and new.status == AppointmentStatus.SCHEDULED
    assert deposit.appointment_id == new.id and deposit.status is PixDepositStatus.PAID
    assert deposit.reschedule_count == 0  # the clinic freed the slot: not the patient's reschedule
    assert (offer.state, offer.new_appointment_id) == ("rebooked", new.id)
    assert items == []
    [text] = [item[2] for item in sent() if item[0] == "text"]
    assert text == f"{CONFIRMED}\n\n{release_offer.CARRIED_OVER_NOTE}"


async def test_without_the_remarcar_tap_nothing_is_carried(db):  # noqa: F811
    world = await _rescheduling(db, tap=False)

    await _confirm(world, _booking(_future(6)))

    new, deposit, offer, _items = await _state(db)
    assert new is not None and deposit.appointment_id == world.appointment.id
    assert offer.state == "open"
    assert [item[2] for item in sent() if item[0] == "text"] == [CONFIRMED]


async def test_a_booking_after_the_b_deadline_stands_on_its_own(db):  # noqa: F811
    world = await _rescheduling(db)
    async with db() as session:
        await release_offer.expire_offers(session, now=datetime.now(UTC) + timedelta(hours=7))

    await _confirm(world, _booking(_future(6)))

    new, deposit, offer, items = await _state(db)
    assert new is not None and deposit.appointment_id == world.appointment.id
    assert (offer.state, offer.refund_source) == ("refund_due", "timeout") and len(items) == 1
    assert [item[2] for item in sent() if item[0] == "text"] == [CONFIRMED]


async def test_the_before_gate_asks_no_new_pix_while_the_deposit_is_carried(db, monkeypatch):  # noqa: F811
    world = await _rescheduling(db)
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant.id)
        tenant.pix_deposit_enabled = True
        tenant.deposit_timing = "before"
        tenant.appointment_types = [
            {"name": "Consulta", "duration_min": 30, "is_active": True, "price": "R$ 200,00"}
        ]
        await session.commit()

    async def _entitled(tenant_id, redis):
        return entitlement_summary()

    monkeypatch.setattr(deposit_gate, "get_entitlements", _entitled)
    provider = FakeProvider()
    monkeypatch.setitem(registry._PROVIDERS, "asaas", provider)
    gate = holds.BookingGate(
        tenant_id=world.tenant.id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        external_id="",
        armed=False,
    )

    decision = await gate.decide(
        start_at=_future(6),
        end_at=_future(6) + timedelta(minutes=30),
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
    )

    assert decision.outcome == "commit"
    assert provider.called("create_pix_charge") == 0


async def test_the_portal_booking_born_at_the_code_carries_it_too(db):  # noqa: F811
    world = await seed_world(
        db, start_at=_future(2), channel=CHANNEL_BRAIN_MESSAGE, google_event_id="evt-released"
    )
    await seed_paid_deposit(db, world)
    start = _future(6)
    async with db() as session:
        released = await session.get(Appointment, world.appointment.id)
        released.status = AppointmentStatus.CANCELLED
        deposit = await session.scalar(select(PixDeposit))
        tenant = await session.get(type(world.tenant), world.tenant.id)
        offer = await release_offer.open_offer(
            session, tenant=tenant, appointment=released, deposit=deposit
        )
        await release_offer.choose_reschedule(
            session, offer=offer, conversation_id=world.conversation.id
        )
        session.add(
            BookingHold(
                tenant_id=world.tenant.id,
                conversation_id=world.conversation.id,
                patient_id=world.patient.id,
                appointment_type="Consulta",
                start_at=start,
                end_at=start + timedelta(minutes=30),
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        await session.commit()
    reply = tasks._ReplyContext(
        channel=CHANNEL_BRAIN_MESSAGE,
        conversation_id=world.conversation.id,
        patient_ref=world.patient.external_id,
        inbound_body="123456",
    )

    extra = await tasks._promote_booking_hold(
        reply, tenant=world.tenant, waba_token=None, professionals=[], redis=None
    )

    assert extra.endswith(release_offer.CARRIED_OVER_NOTE)
    async with db() as session:
        deposit = await session.scalar(select(PixDeposit))
        born = await session.scalar(
            select(Appointment).where(Appointment.id == deposit.appointment_id)
        )
    assert born.id != world.appointment.id and born.start_at is not None
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_offer_carry_over.py -q -p no:cacheprovider`
Expected: FAIL (o sinal continua na consulta liberada; o portão do "antes" responde `blocked`/`no_credential`).

- [ ] **Step 3: Implementar**

(Diffs do plano anterior, verificados sobre `3650e10`; só a coluna da oferta mudou de nome — `new_appointment_id` — e isso fica dentro de `claim_carry_over`.)

```diff
--- a/src/secretaria/workers/shared/flow_runner.py
+++ b/src/secretaria/workers/shared/flow_runner.py
@@ (imports)
-from secretaria.services import reminder_hooks
+from secretaria.services import release_offer, reminder_hooks
@@ (_apply_flow_result: junto das variáveis iniciais, depois de `cancellation_note: str | None = None`)
     cancellation_note: str | None = None
+    # TASK-052: the line that says a released slot's paid deposit now covers this booking.
+    carried_note: str | None = None
@@ (_apply_flow_result: logo depois de `session.add(booked_appointment)`)
                         session.add(booked_appointment)
+                        # TASK-052: "Remarcar" on a slot the clinic released with a PAID deposit -
+                        # the deposit moves to THIS booking in this transaction (no new charge;
+                        # a booking that loses to the B deadline simply stands on its own).
+                        await session.flush()
+                        if await release_offer.claim_carry_over(
+                            session,
+                            tenant_id=conv.tenant_id,
+                            conversation_id=conv.id,
+                            new_appointment=booked_appointment,
+                        ):
+                            carried_note = release_offer.CARRIED_OVER_NOTE
@@ (_apply_flow_result: onde a última bolha recebe `cancellation_note`)
             if cancellation_note:
                 result.bubbles[-1].body = f"{result.bubbles[-1].body}\n\n{cancellation_note}"
+            if carried_note and persisted:
+                result.bubbles[-1].body = f"{result.bubbles[-1].body}\n\n{carried_note}"
```

```diff
--- a/src/secretaria/workers/shared/booking_hold.py
+++ b/src/secretaria/workers/shared/booking_hold.py
@@ (imports)
-from secretaria.services import reminder_hooks
+from secretaria.services import release_offer, reminder_hooks
@@ (_promote_booking_hold: depois de `replaced: ReplacedAppointment | None = None`)
     replaced: ReplacedAppointment | None = None
+    carried = None  # TASK-052: the paid deposit this booking carried over, if any
@@ (_promote_booking_hold: logo depois de `session.add(appointment)` dentro de `async with session.begin():`)
                 session.add(appointment)
+                # TASK-052: a released PAID slot's deposit moves to this booking, in this
+                # transaction (the Portal booking is born here, after the code).
+                await session.flush()
+                carried = await release_offer.claim_carry_over(
+                    session,
+                    tenant_id=tenant.id,
+                    conversation_id=reply.conversation_id,
+                    new_appointment=appointment,
+                )
@@ (_promote_booking_hold: no fim, antes de `return confirmation`)
     if replaced is not None:
         confirmation = f"{confirmation}\n\n{_replaced_notice(tenant, replaced)}"
+    if carried is not None:
+        confirmation = f"{confirmation}\n\n{release_offer.CARRIED_OVER_NOTE}"
     return confirmation
```

```diff
--- a/src/secretaria/services/booking_hold.py
+++ b/src/secretaria/services/booking_hold.py
@@ (BookingGate._deposit_quote: logo depois de `self._patient_id = patient_id`, antes de `return await deposit_gate.quote_deposit(`)
                 self._patient_id = patient_id
+                # TASK-052: "Remarcar" on a slot the clinic released with a PAID deposit - the
+                # next booking of this conversation carries that deposit (no new charge).
+                from secretaria.services import release_offer
+
+                if self._conversation_id is not None and await release_offer.carry_over_pending(
+                    session, tenant_id=tenant.id, conversation_id=self._conversation_id
+                ):
+                    return deposit_gate.DepositQuote(
+                        "no_deposit", release_offer.REASON_CARRIED_OVER
+                    )
                 return await deposit_gate.quote_deposit(
```

(O import é local de propósito: `services/release_offer.py` não deve entrar no grafo de import de `services/booking_hold.py` na carga do módulo.)

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_offer_carry_over.py tests/test_appointment_replacement_wiring.py tests/test_booking_gate_deposit.py tests/test_booking_code_gate.py tests/test_before_booking_flow.py tests/test_deposit_after_mode.py tests/test_workers_layering.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/shared/booking_hold.py src/secretaria/services/booking_hold.py tests/test_release_offer_carry_over.py`
Expected: PASS (5 novos), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/shared/booking_hold.py src/secretaria/services/booking_hold.py tests/test_release_offer_carry_over.py
git commit -m "feat(payments): the booking that ends a Remarcar carries the released slot's paid deposit, no new charge (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 11: O prazo B e os envios na varredura de pagamentos

**Files:**
- Modify: `src/secretaria/workers/payments_cron.py` (`run_payment_sweeps`)
- Test: `tests/test_payment_cron_jobs.py` (1 novo)

**Interfaces:**
- Consumes: Task 3 (`expire_offers`), Task 5 (`dispatch_due`).
- Produces: `run_payment_sweeps` fecha as ofertas vencidas e envia o que está devido (cada um em sessão própria) e loga `release_offers=<n>`, `release_sends=<n>` em `payment_sweep_tick`. Nenhum cron novo (`test_build_identity.py` não muda).

- [ ] **Step 1: Escrever o teste**

Em `tests/test_payment_cron_jobs.py`, antes de `test_the_expiry_cron_wrapper_runs_the_job_with_its_own_session`:

```python
async def test_the_sweep_cron_cancels_unanswered_releases_and_sends_b(db, monkeypatch):
    """TASK-052: the B deadline and the scheduled e-mails ride the existing sweep."""
    from secretaria.config import get_settings
    from secretaria.models import PaymentAttentionItem
    from secretaria.models.deposit_release_offer import DepositReleaseEmail, DepositReleaseOffer
    from secretaria.services import email, release_offer
    from tests.release_offer_support import make_released

    monkeypatch.setattr(payments_cron, "async_session_factory", db)
    mailbox: list[str] = []
    monkeypatch.setattr(email, "_send_sync", lambda to, subject, body: mailbox.append(subject))
    monkeypatch.setattr(get_settings(), "SMTP_HOST", "smtp.exemplo", raising=False)
    async with db() as session:
        released = await make_released(session, business_hours={})
        offer = await release_offer.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=datetime.now(UTC) - timedelta(hours=7),  # B = T + 6 h already passed
        )
        await session.commit()

    await payments_cron.run_payment_sweeps({"redis": FakeRedis()})
    await payments_cron.run_payment_sweeps({"redis": FakeRedis()})

    async with db() as session:
        closed = await session.get(DepositReleaseOffer, offer.id)
        items = list(await session.scalars(select(PaymentAttentionItem)))
        sends = {
            row.kind: row.outcome for row in await session.scalars(select(DepositReleaseEmail))
        }
    assert (closed.state, closed.refund_source) == ("refund_due", "timeout")
    assert [item.reason for item in items] == ["released_refund_due"]
    assert sends["clinic_refund_deadline"] == "sent" and len(mailbox) == 1  # once, not twice
    assert sends["clinic_refund_day"] is None and sends["patient_check"] is None  # due at C
```

(`FakeRedis`, `select`, `datetime`, `UTC`, `timedelta` e `payments_cron` já são usados neste arquivo.)

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_payment_cron_jobs.py -q -p no:cacheprovider`
Expected: FAIL (a oferta vencida continua `open`, nenhum item, nenhum e-mail).

- [ ] **Step 3: Implementar**

```diff
--- a/src/secretaria/workers/payments_cron.py
+++ b/src/secretaria/workers/payments_cron.py
@@
-from secretaria.services import deposit_wait  # noqa: F401
+from secretaria.services import (
+    deposit_wait,  # noqa: F401
+    release_offer,
+    release_offer_dispatch,
+)
@@ async def run_payment_sweeps(ctx: dict) -> None:
         notices = await jobs.recover_paid_notices(session, now=now)
-    if events or fulfillments or notices:
-        logger.info("payment_sweep_tick", events=events, fulfillments=fulfillments, notices=notices)
+    # TASK-052: a slot the clinic released with a PAID deposit - the B deadline, then whatever
+    # is due (the clinic's refund e-mails A/B/C, the keep e-mail, the patient's check at C).
+    # Their own sessions, so a failure here never touches the three sweeps above.
+    async with async_session_factory() as session:
+        offers = await release_offer.expire_offers(session, now=now)
+    async with async_session_factory() as session:
+        sends = (await release_offer_dispatch.dispatch_due(session, now=now)).sent
+    if events or fulfillments or notices or offers or sends:
+        logger.info(
+            "payment_sweep_tick",
+            events=events,
+            fulfillments=fulfillments,
+            notices=notices,
+            release_offers=offers,
+            release_sends=sends,
+        )
```

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_payment_cron_jobs.py tests/test_build_identity.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/workers/payments_cron.py tests/test_payment_cron_jobs.py`
Expected: PASS, ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/workers/payments_cron.py tests/test_payment_cron_jobs.py
git commit -m "feat(payments): the payment sweep runs the B deadline and the released-slot sends (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 12: As corridas em Postgres real (opcional, mas recomendada)

**Files:**
- Create: `tests/test_release_offer_postgres.py`

**Interfaces:**
- Consumes: Tasks 3 e 5; `tests/release_offer_support.py`.
- Produces: nenhum código de produção. Sem `PAYMENTS_TEST_PG_URL`, os 3 testes ficam `skipped`.

- [ ] **Step 1: Escrever o teste**

```python
"""Races SQLite cannot prove for the released-slot offer (TASK-052), on a REAL Postgres.

Opt-in: `PAYMENTS_TEST_PG_URL=postgresql+asyncpg://user@host:port/postgres` (a throwaway server;
each test creates and drops its own database). Proven under concurrent connections:

* N "Cancelar" taps + N runs of the B sweep on ONE offer -> it becomes `refund_due` once and the
  clinic gets exactly ONE attention item and ONE set of scheduled sends;
* N booking commits racing the sweep on an offer in `rescheduling` -> either the deposit moved to
  exactly one new appointment (no item) or the refund fell due once (the deposit stayed);
* N dispatchers on ONE due e-mail -> exactly one SMTP send.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from secretaria.config import get_settings
from secretaria.core.database import Base
from secretaria.models import PaymentAttentionItem, PixDeposit
from secretaria.models.deposit_release_offer import DepositReleaseEmail, DepositReleaseOffer
from secretaria.services import email, release_offer, release_offer_dispatch
from tests.release_offer_support import make_released, new_appointment

PG_URL = os.environ.get("PAYMENTS_TEST_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="set PAYMENTS_TEST_PG_URL to a throwaway server")

WORKERS = 8


@pytest_asyncio.fixture
async def maker():
    name = f"t052_conc_{uuid4().hex[:10]}"
    admin = create_async_engine(PG_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    url = PG_URL.rpartition("/")[0] + f"/{name}"
    engine = create_async_engine(url, pool_size=WORKERS * 2 + 2)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()
    async with admin.connect() as conn:
        await conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await admin.dispose()


async def _offer(maker, *, released_hours_ago: float):
    async with maker() as session:
        released = await make_released(session, business_hours={})
        offer = await release_offer.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=datetime.now(UTC) - timedelta(hours=released_hours_ago),
        )
        await session.commit()
    return released, offer


async def test_cancel_taps_and_sweeps_open_exactly_one_item(maker):
    # Released 5 h 59 min 58.5 s ago: B is ~1.5 s ahead, so taps (before B) and sweeps (after B)
    # genuinely overlap around the deadline.
    _released, offer = await _offer(maker, released_hours_ago=6 - 1.5 / 3600)

    async def tap():
        async with maker() as session:
            row = await session.get(DepositReleaseOffer, offer.id)
            won = await release_offer.choose_cancel(session, offer=row)
            await session.commit()
            return won

    async def sweep():
        await asyncio.sleep(1.5)
        async with maker() as session:
            return await release_offer.expire_offers(session)

    results = await asyncio.gather(*[tap() for _ in range(WORKERS)], *[sweep() for _ in range(WORKERS)])

    taps, sweeps = results[:WORKERS], results[WORKERS:]
    assert sum(1 for won in taps if won) + sum(sweeps) == 1
    async with maker() as session:
        items = await session.scalar(select(func.count()).select_from(PaymentAttentionItem))
        state = (await session.get(DepositReleaseOffer, offer.id)).state
        deadline_rows = await session.scalar(
            select(func.count())
            .select_from(DepositReleaseEmail)
            .where(DepositReleaseEmail.kind == "clinic_refund_deadline")
        )
    assert items == 1 and state == "refund_due" and deadline_rows == 1


async def test_bookings_racing_the_sweep_carry_or_refund_never_both(maker):
    released, offer = await _offer(maker, released_hours_ago=6 - 1.5 / 3600)
    async with maker() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.choose_reschedule(
            session, offer=row, conversation_id=released.conversation.id
        )
        await session.commit()

    async def book(day: int):
        async with maker() as session:
            fresh = await new_appointment(
                session, released, start=datetime(2030, 4, day, 14, tzinfo=UTC)
            )
            carried = await release_offer.claim_carry_over(
                session,
                tenant_id=released.tenant_id,
                conversation_id=released.conversation.id,
                new_appointment=fresh,
            )
            await session.commit()
            return carried is not None

    async def sweep():
        await asyncio.sleep(1.5)
        async with maker() as session:
            return await release_offer.expire_offers(session)

    results = await asyncio.gather(*[book(day) for day in range(1, WORKERS + 1)], sweep())

    carried = sum(1 for won in results[:WORKERS] if won)
    refunded = results[WORKERS]
    assert carried + refunded == 1
    async with maker() as session:
        items = await session.scalar(select(func.count()).select_from(PaymentAttentionItem))
        deposit = await session.get(PixDeposit, released.deposit.id)
        state = (await session.get(DepositReleaseOffer, offer.id)).state
    if carried:
        assert state == "rebooked" and items == 0
        assert deposit.appointment_id != released.appointment.id
    else:
        assert state == "refund_due" and items == 1
        assert deposit.appointment_id == released.appointment.id


async def test_concurrent_dispatchers_send_one_email(maker, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(email, "_send_sync", lambda to, subject, body: sent.append(subject))
    monkeypatch.setattr(get_settings(), "SMTP_HOST", "smtp.exemplo", raising=False)
    _released, offer = await _offer(maker, released_hours_ago=1)
    async with maker() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.choose_cancel(session, offer=row)
        await session.commit()

    async def dispatch():
        async with maker() as session:
            return (await release_offer_dispatch.dispatch_due(session)).sent

    results = await asyncio.gather(*[dispatch() for _ in range(WORKERS)])

    assert sum(results) == 1 and len(sent) == 1  # only A was due
```

- [ ] **Step 2: Rodar contra um Postgres LOCAL descartável**

Run: `$env:PAYMENTS_TEST_PG_URL = "postgresql+asyncpg://postgres@127.0.0.1:<porta>/postgres"; $env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_offer_postgres.py -q -p no:cacheprovider` (3 vezes seguidas; `<porta>` é a do Postgres descartável que você subiu)
Expected: PASS (3) nas três. Sem Postgres: `3 skipped` e "NÃO PROVADO em Postgres" no `TASK.md`. Nunca aponte para um banco que não seja descartável.

- [ ] **Step 3: Commit**

```powershell
git add tests/test_release_offer_postgres.py
git commit -m "test(payments): real-Postgres races for release offers - one item, carry or refund, one send (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 13: Brain-Message-Frontend — o texto do aviso de liberar com sinal pago

O cartão "Liberar horário" da agenda diz hoje que liberar "pode fazer a clínica reter o valor pago" — falso depois das Tasks 3–11. Só texto; o pedido (`acknowledge_retention`) não muda de nome.

**Files:**
- Modify (outro repositório): `lib/agenda/release.ts`, `lib/agenda/__tests__/release.test.ts`

**Interfaces:**
- Produces: `RELEASE_COPY.ackBody` e `RELEASE_COPY.ackLabel` novos.

- [ ] **Step 1: Worktree próprio a partir de `feature/out-of-mvp`**

```powershell
git -C C:\TECH\BRAIN\Brain-Message-Frontend worktree add -b task/TASK-052-aviso-liberar "C:\TECH\BRAIN-worktrees\TASK-052\Brain-Message-Frontend" feature/out-of-mvp
Set-Location "C:\TECH\BRAIN-worktrees\TASK-052\Brain-Message-Frontend"
npm ci
```

- [ ] **Step 2: Escrever o teste que falha**

Em `lib/agenda/__tests__/release.test.ts`, antes de `describe("releaseNeedsAck / buildReleaseBody", ...)`:

```ts
describe("the paid-release warning (secretarIA TASK-052)", () => {
  it("names the patient's three choices and the refund e-mails, and never says the clinic keeps it", () => {
    expect(RELEASE_COPY.ackBody).toContain("manter o horário");
    expect(RELEASE_COPY.ackBody).toContain("remarcar usando o mesmo sinal");
    expect(RELEASE_COPY.ackBody).toContain("cancelar");
    expect(RELEASE_COPY.ackBody).toContain("e-mails para devolver");
    for (const text of [RELEASE_COPY.ackBody, RELEASE_COPY.ackLabel]) {
      expect(text).not.toMatch(/retid|reter/i);
      expect(text).not.toMatch(/(será|serão) (estornad|reembolsad|devolvid)/i);
    }
  });
});
```

Run: `npx vitest run lib/agenda/__tests__/release.test.ts`
Expected: FAIL (`expected "Liberar o horário agora, dentro da janela…" to contain "manter o horário"`).

- [ ] **Step 3: Implementar**

Em `lib/agenda/release.ts`, troque o comentário do topo (linhas que começam em `// The gate: releasing an appointment whose Pix deposit is PAID can make the clinic`) por:

```ts
// The gate: releasing an appointment whose Pix deposit is PAID hands the patient a
// choice (secretarIA TASK-052, services/release_offer.py): keep the slot, reschedule
// with the same deposit, or cancel. Cancelling - or no answer by the deadline - makes
// the clinic receive e-mails to return the deposit in the payment provider's panel
// (the platform never refunds by itself, D15). The clinic must acknowledge that before
// any request leaves; `buildReleaseBody` returns null until it did, and the backend
// refuses a release without it (409 retention_ack_required), which makes the step
// mandatory (`forceAck`) even when the read did not show the deposit. The request
// field keeps its old name (`acknowledge_retention`): renaming it is a contract change
// for nothing.
```

e, em `RELEASE_COPY`, troque `ackBody` e `ackLabel` por:

```ts
  ackBody:
    "Ao liberar o horário, o paciente poderá manter o horário, remarcar usando o mesmo sinal, sem nova cobrança, ou cancelar. Se ele cancelar ou não responder até o prazo, a clínica recebe e-mails para devolver o sinal por inteiro pelo painel do provedor de pagamento.",
  ackLabel: "Entendo que o sinal pago pode ter de ser devolvido",
```

- [ ] **Step 4: Rodar e ver passar**

Run: `npx vitest run lib/agenda components/agenda ; npm run typecheck`
Expected: PASS, typecheck limpo.

- [ ] **Step 5: Commit**

```powershell
git add lib/agenda/release.ts lib/agenda/__tests__/release.test.ts
git commit -m "fix(agenda): the paid-release warning describes keep/reschedule/cancel and the refund e-mails, not a retention (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 14: Gates, modelos Meta documentados, CHECKPOINT e registro

**Files:**
- Create: `docs/CHECKPOINT_task052_liberar_sinal_pago.md`
- Modify: `docs/LEMBRETES_MODELOS_META.md` (seção TASK-052), `CLAUDE.md` (1 linha de ponteiro), `C:\TECH\BRAIN\tasks\TASK-052\TASK.md` (fora do repositório)

- [ ] **Step 1: Suíte inteira e lint de tudo o que foi tocado**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest -q -p no:cacheprovider ; uv run ruff check src/secretaria/models/deposit_release_offer.py src/secretaria/models/__init__.py src/secretaria/models/payment_attention_item.py migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py src/secretaria/config.py src/secretaria/services/email.py src/secretaria/services/release_deadlines.py src/secretaria/services/release_offer.py src/secretaria/services/release_keep.py src/secretaria/services/release_offer_dispatch.py src/secretaria/services/payments/attention_view.py src/secretaria/api/hub/calendar.py src/secretaria/services/appointment_release.py src/secretaria/workers/whatsapp/notifications.py src/secretaria/services/availability.py src/secretaria/services/flow_router.py src/secretaria/workers/shared/release_offer_actions.py src/secretaria/workers/shared/actions.py src/secretaria/schemas/webhook.py src/secretaria/workers/portal/inbound.py src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/shared/booking_hold.py src/secretaria/services/booking_hold.py src/secretaria/workers/payments_cron.py tests/release_offer_support.py tests/test_deposit_release_offer_migration.py tests/test_release_deadlines.py tests/test_release_offer.py tests/test_release_keep.py tests/test_release_offer_dispatch.py tests/test_release_offer_buttons.py tests/test_release_offer_carry_over.py tests/test_release_offer_postgres.py tests/test_deposit_before_migration.py tests/test_payment_attention.py tests/test_hub_release.py tests/test_cancellation_notice.py tests/test_availability.py tests/test_before_booking_flow.py tests/test_payment_cron_jobs.py`
Expected: linha de base da Task 0 + os testes novos, **0 failed**; ruff limpo. Uma falha que também falha na linha de base é pré-existente (registre); qualquer outra é regressão (corrija antes de seguir).

- [ ] **Step 2: Documentar os dois modelos pendentes na Meta**

No fim de `docs/LEMBRETES_MODELOS_META.md`, acrescente:

````markdown
## TASK-052 — horário liberado com sinal pago (PENDÊNCIA META, 2026-10-12)

Decisão do dono: o código já usa os dois modelos como se aprovados (`config.py`
`RELEASE_OFFER_TEMPLATE_NAME`, `REFUND_CHECK_TEMPLATE_NAME`). Enquanto a Meta não aprovar, o envio
fora da janela de 24 h falha e fica registrado; os e-mails de devolução seguem cobrindo o caso.
Categoria: Utilidade. Idioma: pt_BR. Um parâmetro no corpo, sem quebras de linha.

### `liberacao_sinal_pago_v1`

Corpo: `{{1}}` (o texto da liberação: frase do dono, recado da clínica, valor do sinal e prazo).
Botões de resposta rápida: "Manter horário", "Remarcar", "Cancelar".
Exemplo de `{{1}}`: "Devido à sua não confirmação da ida à consulta, a clínica liberou o seu horário para que você decida o que fazer. Você já pagou o sinal desta consulta (R$ 60,00). Toque em *Manter horário* para ficar com o mesmo horário, em *Remarcar* para escolher outro dia e horário usando o mesmo sinal, sem nova cobrança, ou em *Cancelar* para cancelar a consulta e receber o sinal de volta. Se você não responder até 20:00 de 05/03, a consulta será cancelada e a clínica fará a devolução do sinal."

### `devolucao_sinal_conferir_v1`

Corpo: `{{1}}`. Botões de resposta rápida: "Recebido", "Não recebido".
Exemplo de `{{1}}`: "A clínica foi avisada para devolver o seu sinal de R$ 60,00. Confira a sua conta bancária (a devolução vai para a conta de onde saiu o Pix) e nos conte:"

Depois da aprovação em cada WABA: troque os nomes em `config.py` se a Meta exigir outro nome e
registre aqui a data.
````

- [ ] **Step 3: Criar o CHECKPOINT**

`docs/CHECKPOINT_task052_liberar_sinal_pago.md` (preencha as medições `«…»` com o que o Step 1 e a Task 12 mostraram; são medições, não decisões):

````markdown
# CHECKPOINT — Liberar horário com sinal pago: manter, remarcar ou cancelar (TASK-052)

**Estado:** construído e commitado em `task/TASK-052-liberar-sinal-pago` (worktree
`C:\TECH\BRAIN-worktrees\TASK-052\secretarIA`), último commit «SHA»; front em `task/TASK-052-aviso-liberar` «SHA».
**Não pushado, não deployado.** Integra em `feature/out-of-mvp` (nunca `main`), depois da TASK-050.
Spec: `docs/superpowers/specs/2026-10-11-liberar-horario-com-sinal-pago-design.md` (refeita em 2026-10-12).
Plano: `docs/superpowers/plans/2026-10-11-task-052-liberar-horario-sinal-pago.md`.

## 1. O que é
Liberar (`POST .../appointments/{id}/release`) uma consulta com sinal PAGO abre uma oferta (`deposit_release_offers`)
e manda ao paciente "Manter horário" / "Remarcar" / "Cancelar" (WhatsApp na janela; modelo pago fora dela, PENDÊNCIA
META; Portal sempre) e, ao paciente de WhatsApp com e-mail, um e-mail. Manter recria a consulta se o horário está
livre (e-mail à clínica); Remarcar leva o sinal à nova consulta; Cancelar (ou silêncio até B = min(T+6h, fechamento−30
min)) abre UM item `released_refund_due` e programa os e-mails à clínica A (no toque), B e C (T+24h) e a conferência
do paciente (Recebido / Não recebido) em T+24h. Tudo em `deposit_release_emails`, um envio por linha.

## 2. Onde está cada coisa
| Camada | Arquivo |
|---|---|
| Tabelas + migração | `models/deposit_release_offer.py`, `migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py` |
| Prazos | `services/release_deadlines.py` |
| Regras e textos | `services/release_offer.py` |
| Manter | `services/release_keep.py` |
| Envios (e-mails, conferência, modelos) | `services/release_offer_dispatch.py`, `services/email.py`, `config.py` |
| Liberar / Portal | `api/hub/calendar.py::release_appointment`, `services/appointment_release.py` |
| WhatsApp | `workers/whatsapp/notifications.py::send_cancellation_notice` |
| Toques | `workers/shared/release_offer_actions.py`, `workers/shared/actions.py`, `schemas/webhook.py`, `workers/portal/inbound.py` |
| Transferência | `workers/shared/flow_runner.py::_apply_flow_result`, `workers/shared/booking_hold.py::_promote_booking_hold`, `services/booking_hold.py::BookingGate._deposit_quote` |
| Prazo B e envios | `workers/payments_cron.py::run_payment_sweeps` |
| Dias segurados | `services/flow_router.py::_enter_slot_picker`, `services/availability.py::held_slots` |

## 3. Decisões e perguntas
Decisões padrão: spec §10. Perguntas ao dono: spec §11. Modelos Meta: `docs/LEMBRETES_MODELOS_META.md` (TASK-052).

## 4. Validação
Suíte «N» passed / «S» skipped / «X» xfailed / 0 failed (linha de base «B»); ruff limpo; Postgres real: «PROVADO / NÃO PROVADO».
**NÃO PROVADO:** envio real pelo WhatsApp (os modelos nem estão aprovados), SMTP real, cartão no Portal real, produção.

## 5. Deploy (NÃO autorizado)
Migração `b6d2e8f4a1c3` → API → worker LOGO depois → Brain-Message-Frontend. `BRAIN_MESSAGE_PORTAL_URL` precisa estar
configurado na API e no worker para os links dos e-mails.
````

- [ ] **Step 4: Ponteiro no `CLAUDE.md`**

Na seção "## Documentação" do `CLAUDE.md` do secretarIA, como primeiro parágrafo:

```markdown
`docs/CHECKPOINT_task052_liberar_sinal_pago.md` — TASK-052: liberar horário com sinal pago dá ao paciente Manter / Remarcar / Cancelar; cancelar ou não responder até B gera e-mails à clínica (A/B/C) e a conferência Recebido / Não recebido em T+24h; modelos Meta PENDENTES; integra em `feature/out-of-mvp`; **construído, NÃO deployado**.
```

- [ ] **Step 5: Atualizar o `TASK.md`**

SHAs finais (secretarIA e front), contagens do Step 1, Postgres PROVADO/NÃO PROVADO, modelos Meta pendentes, `Deployment: NOT AUTHORIZED`.

- [ ] **Step 6: Commit**

```powershell
git add docs/CHECKPOINT_task052_liberar_sinal_pago.md docs/LEMBRETES_MODELOS_META.md CLAUDE.md
git commit -m "docs: checkpoint and pending Meta templates for releasing a paid slot (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

Merge em `feature/out-of-mvp` (depois da TASK-050) é do Integrator; push e deploy só com pedido explícito do dono.

---

## Self-review

**1. Cobertura da spec → tasks.**

| Spec | Onde |
|---|---|
| §1 Intenção e os 6 critérios de sucesso | 1 → Tasks 3, 6, 7; 2 → Tasks 4, 9; 3 → Tasks 3, 5, 9; 4 → Tasks 3, 5, 11; 5 → Tasks 3, 5, 9; 6 → Tasks 2, 3, 5, 9, 12 |
| §2 Escopo (secretarIA + texto do front) | Tasks 1–12; Task 13 |
| §3 Fora de escopo | não tocados; Task 6 mantém `on_appointment_cancelled` para sinal não pago e Pix em voo |
| §4 Fatos do código | Task 0 (âncoras), Tasks 4, 5, 6, 7, 9 |
| §5.1 A oferta e os estados | Tasks 1, 3, 6 |
| §5.2 Prazos e horário da clínica | Task 2 (e Task 3 grava B e C) |
| §5.3 Mensagem em T e canais | Tasks 3 (`offer_body`), 5 (e-mail), 6 (hub, Portal, e-mail WhatsApp), 7 (job) |
| §5.4 Manter | Tasks 3 (`claim_keep`), 4, 9 |
| §5.5 Remarcar | Tasks 3, 9, 10 |
| §5.6 Cancelar | Tasks 3, 5, 9 |
| §5.7 Sem resposta (B) | Tasks 3 (`settle_if_expired`/`expire_offers`), 9 (toque tardio), 11 |
| §5.8 E-mails à clínica | Task 5 (textos e envio), Task 9 (envio logo após o toque) |
| §5.9 Conferência do paciente | Tasks 3 (`record_received`/`request_reminder`), 5 (envio), 9 (toques) |
| §5.10 Dias com horários segurados | Task 8 |
| §5.11 Modelos Meta | Tasks 5 (config), 7 (uso), 14 (documentação) |
| §5.12 Envio idempotente | Tasks 1, 5, 12 |
| §6 Combinações | Tasks 3, 4, 5, 9 (um teste por linha) |
| §7 Erros e corridas | Review Focus; Tasks 3, 4, 5, 10, 12 |
| §9 Deploy | "Estado de execução", Global Constraints, Task 14 |
| §10 Decisões / §11 Perguntas | implementadas como adotadas; perguntas registradas no CHECKPOINT e no `TASK.md` |

**2. Varredura de placeholders.** Nenhum "TBD"/"TODO"/"similar à Task N": cada bloco é o conteúdo de um arquivo, um trecho a substituir por âncora ou um diff. Os `«»` do CHECKPOINT são medições declaradas; `<porta>` é a de um Postgres descartável escolhido na hora. Diferente do plano anterior, este NÃO foi aplicado numa cópia descartável (está dito no topo).

**3. Consistência de nomes.** `relkeep`/`relresched`/`relcancel`/`relgot`/`relmiss` são os mesmos em `services/release_offer.py`, `schemas/webhook.py`, `workers/portal/inbound.py` (via `RELEASE_OFFER_ACTIONS`), hub/job e testes; `choice_offered` no hub e nos testes; `DECIDING_STATES` no modelo, no serviço, no Manter, no job e nos toques; `decide_by_at`/`refund_check_at`/`new_appointment_id` no modelo, na migração, no serviço, no despacho e nos testes; os `kind` (`clinic_refund_now`, `clinic_refund_deadline`, `clinic_refund_day`, `clinic_refund_reminder`, `clinic_patient_keeps`, `patient_check`) no modelo, no serviço, no texto dos e-mails e nos testes.

**4. Review Focus.** Os 5 itens têm teste nomeado na task dona.

