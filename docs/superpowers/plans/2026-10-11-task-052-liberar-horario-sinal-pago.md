# Liberar horário com sinal pago: manter (por opção da clínica), remarcar ou cancelar (TASK-052) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Quando a clínica libera uma consulta com sinal Pix já pago, o paciente escolhe **Manter horário** (um PEDIDO à clínica, que aceita ou recusa em 24 h), **Remarcar** (a qualquer momento; leva o sinal se a clínica ainda não marcou devolvido) ou **Cancelar** (avisa a clínica na hora com o passo a passo do Asaas); o silêncio avisa a clínica em B e vira "Cancelado por falta de resposta" em T + 24 h; quando a clínica marca devolvido o paciente responde Recebido / Não recebido — e a TASK-052 expõe tudo isso, com o escopo de agenda de cada papel, para a aba da TASK-054. O sistema nunca devolve dinheiro (D15).

**Architecture:** Duas tabelas aditivas — `deposit_release_offers` (uma por consulta liberada com sinal; eixos `decision` e `refund_status`; cada transição um `UPDATE ... WHERE` guardado) e `deposit_release_emails` (um envio por linha, reivindicado antes de enviar). Regras em `services/` (`release_deadlines`, `release_offer`, `release_keep`, `release_offer_dispatch`, `brain_staff`); toques por `workers/shared/actions.py::_handle_action_button` via `workers/shared/release_offer_actions.py`; B, E, K e envios na varredura de pagamentos; transferência na transação que cria a consulta nova; quatro rotas novas no hub com o escopo da agenda.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Alembic, arq, httpx (brain-api), pytest (`asyncio_mode = "auto"`, SQLite em memória; Postgres real opcional por `PAYMENTS_TEST_PG_URL`), ruff, SMTP. Front: Next.js/TypeScript/vitest (só um texto).

**Spec:** `docs/superpowers/specs/2026-10-11-liberar-horario-com-sinal-pago-design.md` (3ª revisão do dono, 2026-10-12; este worktree). Regras-mãe: D15, `docs/CHECKPOINT_task050_main_into_out_of_mvp.md`, `tests/test_workers_layering.py`, skill `frozen-contract-migration`.

**Estado de execução (leia antes de começar):**

- **Worktree:** `C:\TECH\BRAIN-worktrees\TASK-052\secretarIA`, branch `task/TASK-052-liberar-sinal-pago`, base `task/TASK-050-secretaria` @ `3650e10` (+ só commits de documentos). Integra em `feature/out-of-mvp` (NUNCA `main`) depois da TASK-050.
- **Este plano NÃO foi aplicado numa cópia descartável.** Âncoras conferidas lendo o código de `3650e10`; os blocos são o conteúdo pretendido. Se um trecho não aplicar, aplique pela âncora (nome de função/constante), nunca pelo número de linha, e registre no `TASK.md`. Veja cada teste falhar ANTES de implementar.
- **Dependência externa:** `GET /internal/tenants/{id}/staff-roles` do brain-api (o dono está construindo). Sem ele, o fechamento usado é o do médico (fallback testado).
- **NÃO PROVADO:** WhatsApp real (modelos não aprovados), SMTP real, cartão no Portal real, produção, o endpoint de papéis do brain-api. A tela da aba é da TASK-054.
- **Produção:** NÃO AUTORIZADA. Sem push, deploy, migração em banco real, `.env`, Easypanel.

---

## Contratos tocados

| # | Onde | Antes | Depois |
|---|---|---|---|
| 1 | `POST /tenants/me/calendar/appointments/{id}/release` — 409 `retention_ack_required` | `deposit_outcome` da política de retenção | `deposit_outcome = "choice_offered"` com sinal PAGO; texto da escolha |
| 2 | o mesmo, 200 | sinal pago podia ficar retido ou abrir item | oferta aberta; sinal PAGO; sinal não pago: como hoje + registro `not_offered` |
| 3 | job `send_cancellation_notice(...)` (7 argumentos) | 3 botões de remarcação | **mesma assinatura**; botões da oferta (sem Manter depois de S); fora da janela o modelo de 3 botões |
| 4 | `schemas/webhook.py::_ACTION_BUTTON_PREFIXES` | — | `relkeep|`, `relresched|`, `relcancel|`, `relgot|`, `relmiss|` (id = consulta LIBERADA) |
| 5 | Portal: ids aceitos como ação | família dos lembretes | + os 5 `rel*` (só de cartão recente) |
| 6 | `payment_attention_items.reason` / `resolve_item` | — | + `released_refund_due`; resolver o item atualiza a oferta |
| 7 | Hub NOVO (TASK-054) | — | `GET /tenants/me/release-offers`, `POST .../{id}/refunded`, `POST .../{id}/keep/accept`, `POST .../{id}/keep/refuse` — todos os papéis, escopo da agenda |
| 8 | brain-api (DEPENDÊNCIA) | — | `GET /internal/tenants/{id}/staff-roles` → `{"roles": [...]}` (secretarIA só lê; fallback = médico) |
| 9 | Banco | cabeça `e2b7c4a9d1f5` | + `b6d2e8f4a1c3` (só tabelas novas) |
| 10 | `config.py` | — | `RELEASE_OFFER_TEMPLATE_NAME`, `REFUND_CHECK_TEMPLATE_NAME` (PENDÊNCIA META) |
| 11 | Avisos | — | 7 tipos à clínica (e-mail com link do console); ao paciente: mensagens na conversa; e-mail só no Portal |
| 12 | Brain-Message-Frontend `lib/agenda/release.ts` | "pode fazer a clínica reter o valor pago" | manter (a clínica decide) / remarcar / cancelar; avisos para devolver |

## Global Constraints

- **Dinheiro (D15):** o sistema NUNCA estorna nem chama o provedor; "devolver" = UM item `released_refund_due` por sinal + avisos à clínica, que estorna no Asaas.
- **Instantes:** T; S; F = fechamento − 30 min no dia de T (clínica se o brain-api diz que há secretária; senão o do médico; sem médico, a clínica); B = `min(T + 6 h, F)` se F > T, senão T + 6 h; E = T + 24 h; K = toque em Manter + 24 h. Nada sai antes de T.
- **Manter** só pede: T..S (também depois de B), devolução não marcada; abre `keep_requested` até K; pausa B/E e descarta os avisos de devolução que vencerem; aceite (só com horário livre) recria e desfaz a devolução; recusa ou K → devolução devida, aviso novo + lembrete.
- **Remarcar** a qualquer momento enquanto a oferta não está encerrada (`kept`, `rebooked`, `no_answer_cancelled`, `not_offered` encerram): leva o sinal se a devolução não foi marcada; ao confirmar, desfaz a devolução e manda `clinic_rebooked_deposit`.
- **Cancelar** sempre avisa a clínica na hora (exceto devolução já marcada). **E** sem decisão final → `no_answer_cancelled` (terminal, não reabre).
- **Lembrete de E**: não sai só com devolução `received`/`clinic_decided` ou decisão `keep_requested`/`kept`/`rebooked`.
- **Escopo:** todos os papéis; lista e ações com `get_agenda_viewer` + `mine` (regra de `api/hub/calendar.py::list_events`); fora do escopo = 404.
- **Paciente:** WhatsApp NUNCA recebe e-mail; Portal: cartão + e-mail com link (liberação e conferência). Texto sem banco ("a conta de onde saiu o Pix").
- **Um envio por linha** (`(offer_id, kind, seq)` UNIQUE), reivindicado antes de enviar, revalidado, até 5 tentativas. **Logs:** ids, `kind`, `outcome`, `error_type`.
- **Textos:** botões ≤ 20; corpo ≤ 1024 cortando só o recado da clínica; e-mails sem nome/telefone/e-mail do paciente.
- **Modelos Meta:** 3 botões reaproveitado (também depois de S); conferência com 2 botões; como se aprovados.
- **Camadas:** `workers/shared/*` não importa `whatsapp/*`, `portal/*` nem `tasks`.
- **Deploy (NÃO autorizado):** migração → API → worker LOGO depois → front.
- **Gates:** `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest <arquivos> -q -p no:cacheprovider`; `uv run ruff check <arquivos>`; nunca `ruff format .`. Front: `npx vitest run`, `npm run typecheck`.
- **Git:** nunca `git add -A`/`git add .`; commits em inglês terminando com `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` e `Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG`.

## Review Focus

1. **Dinheiro em dois lugares** — sinal transferido E devolução ainda pedida, ou consulta mantida E devolução pedida. → Task 3: `test_a_confirmed_remarcar_carries_the_deposit_cancels_the_refund_and_tells_the_clinic`, `test_the_clinic_accepting_moves_the_deposit_and_closes_the_refund`; Task 10: `test_a_booking_after_b_still_carries_and_undoes_the_refund`; Task 13: `test_bookings_racing_the_sweep_carry_exactly_once_and_leave_no_refund_open`.
2. **Um aviso sai duas vezes** (retry, duas instâncias, cliques/marcações repetidos). → Task 5: `test_each_scheduled_email_goes_out_once_even_dispatched_twice`; Task 9: `test_manter_twice_tells_the_clinic_once`; Task 11: `test_marking_refunded_is_idempotent_and_asks_the_patient_once`; Task 13: `test_concurrent_dispatchers_send_one_email`.
3. **Aviso de devolução enquanto a clínica decide "Manter", ou lembrete de E errado.** → Task 5: `test_manter_tells_the_clinic_with_the_link_and_pauses_the_refund_notices`, `test_the_day_reminder_skips_only_when_marked_and_received`, `test_the_day_reminder_still_goes_when_marked_but_unanswered`.
4. **Alguém age fora do seu escopo** (médico na oferta de outro; outra clínica). → Task 11: `test_scope_follows_the_agenda`, `test_another_clinics_offer_is_a_404_and_a_bad_body_is_never_echoed`; Task 9: `test_another_patients_tap_is_an_unknown_appointment_and_changes_nothing`.
5. **A oferta reabre depois de T + 24 h, ou "aceitar manter" põe o paciente num horário ocupado.** → Task 9: `test_any_tap_after_e_is_answered_and_reopens_nothing`; Task 4: `test_a_consultation_the_clinic_booked_there_means_taken`, `test_another_conversations_hold_means_taken`.

---

## File Structure

**Criar (secretarIA):** `src/secretaria/models/deposit_release_offer.py`; `migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py`; `src/secretaria/services/{release_deadlines,release_offer,release_keep,release_offer_dispatch,brain_staff}.py`; `src/secretaria/workers/shared/release_offer_actions.py`; `src/secretaria/api/hub/release_offers.py`; `src/secretaria/schemas/release_offers.py`; testes `tests/release_offer_support.py`, `tests/test_deposit_release_offer_migration.py`, `tests/test_release_deadlines.py`, `tests/test_release_offer.py`, `tests/test_release_keep.py`, `tests/test_release_offer_dispatch.py`, `tests/test_brain_staff.py`, `tests/test_release_offer_buttons.py`, `tests/test_release_offer_carry_over.py`, `tests/test_release_offers_hub.py`, `tests/test_release_offer_postgres.py`; `docs/CHECKPOINT_task052_liberar_sinal_pago.md`.

**Modificar (secretarIA):** `src/secretaria/models/{__init__,payment_attention_item}.py`; `src/secretaria/config.py`; `src/secretaria/main.py`; `src/secretaria/services/email.py`; `src/secretaria/services/payments/attention_view.py`; `src/secretaria/api/hub/calendar.py`; `src/secretaria/services/appointment_release.py`; `src/secretaria/workers/whatsapp/notifications.py`; `src/secretaria/schemas/webhook.py`; `src/secretaria/workers/portal/{inbound,open}.py`; `src/secretaria/workers/shared/actions.py`; `src/secretaria/services/{flow_router,availability,booking_hold}.py`; `src/secretaria/workers/shared/{flow_runner,booking_hold}.py`; `src/secretaria/workers/payments_cron.py`; testes `tests/{test_deposit_before_migration,test_payment_attention,test_hub_release,test_cancellation_notice,test_availability,test_before_booking_flow,test_payment_cron_jobs}.py`; `docs/LEMBRETES_MODELOS_META.md`; `CLAUDE.md` (1 linha).

**Brain-Message-Frontend (Task 14):** `lib/agenda/release.ts`, `lib/agenda/__tests__/release.test.ts`.

**Fora dos repositórios:** `C:\TECH\BRAIN\tasks\TASK-052\TASK.md`.

**Nunca modificar:** `.env`, `Dockerfile`, `workers/tasks.py` (fachada), `workers/arq_worker.py`, brain-api (a dependência é do dono), PreCheck.

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
git grep -n "async def resolve_item" -- src/secretaria/services/payments/attention_view.py
git grep -n "async def process_brain_message_enter" -- src/secretaria/workers/portal/open.py
git grep -n "app.include_router(payment_attention.router)" -- src/secretaria/main.py
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

### Task 1: Tabelas `deposit_release_offers` / `deposit_release_emails` (migração aditiva)

**Files:**
- Create: `src/secretaria/models/deposit_release_offer.py`, `migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py`
- Modify: `src/secretaria/models/__init__.py`, `tests/test_deposit_before_migration.py`
- Test: `tests/test_deposit_release_offer_migration.py` (novo)

**Interfaces:**
- Produces: `DepositReleaseOffer` (colunas `id, tenant_id, deposit_id, released_appointment_id (UNIQUE), patient_id, conversation_id, deposit_paid, decision, refund_status, refund_source, released_at, slot_start_at, decide_by_at, expires_at, paid_notice_allowed, first_contact_at, refund_due_at, refund_marked_at, patient_answered_at, last_reminder_at, after_start_card_at, keep_requested_at, keep_deadline_at, keep_outcome, keep_resolved_at, keep_resolved_by, new_appointment_id, closed_at, created_at`); `DepositReleaseEmail` (`id, tenant_id, offer_id, kind, seq, due_at, attempts, claimed_at, outcome, created_at`; UNIQUE `(offer_id, kind, seq)`). Nenhuma coluna em tabela existente ("tem secretária" vem do brain-api, Task 6).
- Constantes (em `models/deposit_release_offer.py`): decisões `DECISION_NONE="none"`, `DECISION_RESCHEDULING="rescheduling"`, `DECISION_KEEP_REQUESTED="keep_requested"`, `DECISION_KEPT="kept"`, `DECISION_KEEP_DECLINED="keep_declined"`, `DECISION_REBOOKED="rebooked"`, `DECISION_CANCELLED="cancelled"`, `DECISION_NO_ANSWER="no_answer_cancelled"`, `DECISION_NOT_OFFERED="not_offered"`; grupos `SILENT_DECISIONS=(none, rescheduling)` (B e E valem), `KEEP_FROM=(none, rescheduling)`, `RESCHEDULE_FROM=(none, rescheduling, cancelled, keep_requested, keep_declined)`, `CANCEL_FROM=(none, rescheduling, keep_requested, keep_declined)`; `KEEP_OUTCOME_ACCEPTED="accepted"`, `KEEP_OUTCOME_REFUSED="refused"`, `KEEP_OUTCOME_TIMEOUT="timeout"`; `REFUND_SOURCE_KEEP_REFUSED="keep_refused"`, `REFUND_SOURCE_KEEP_TIMEOUT="keep_timeout"`; `REFUND_CARRYABLE=(not_due, due)`; devolução `REFUND_NOT_DUE="not_due"`, `REFUND_DUE="due"`, `REFUND_MARKED="marked"`, `REFUND_RECEIVED="received"`, `REFUND_NOT_RECEIVED="not_received"`, `REFUND_CLINIC_DECIDED="clinic_decided"`, `REFUND_ANSWERABLE=(marked, not_received)`; `REFUND_SOURCE_PATIENT="patient"`, `REFUND_SOURCE_TIMEOUT="timeout"`; envios `EMAIL_REFUND_CANCEL="clinic_refund_cancel"`, `EMAIL_REFUND_NOANSWER="clinic_refund_noanswer"`, `EMAIL_REFUND_DAY="clinic_refund_day"`, `EMAIL_REFUND_REMINDER="clinic_refund_reminder"`, `EMAIL_KEEP_REQUEST="clinic_keep_request"`, `EMAIL_KEEP_DECLINED="clinic_keep_declined"`, `EMAIL_REBOOKED_DEPOSIT="clinic_rebooked_deposit"`, `EMAIL_PATIENT_KEEP_ACCEPTED="patient_keep_accepted"`, `EMAIL_PATIENT_KEEP_DECLINED="patient_keep_declined"`, `EMAIL_PATIENT_CHECK="patient_check"`, `REFUND_NOTICE_KINDS` (os 5 avisos de devolução); resultados `OUTCOME_SENDING="sending"`, `OUTCOME_SENT="sent"`, `OUTCOME_SKIPPED="skipped"`, `OUTCOME_NO_CONTACT="skipped_no_contact"`, `OUTCOME_GIVEN_UP="given_up"`. Revisão `b6d2e8f4a1c3` (desce de `e2b7c4a9d1f5`).

- [ ] **Step 1: Escrever o teste que falha**

`tests/test_deposit_release_offer_migration.py`:

```python
"""Migration b6d2e8f4a1c3 - release offers and their scheduled sends (TASK-052).

Cheapest layers first, like tests/test_deposit_before_migration.py: a single head right after the
`before` timing columns; the offline SQL only creates two tables (no ALTER, no DROP), its
downgrade only drops them; the models declare the same shape.
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
    for name in (
        "UQ_DEPOSIT_RELEASE_OFFERS_RELEASED_APPOINTMENT_ID",
        "UQ_DEPOSIT_RELEASE_EMAILS_OFFER_KIND_SEQ",
        "IX_DEPOSIT_RELEASE_OFFERS_DECISION_DECIDE_BY",
        "IX_DEPOSIT_RELEASE_OFFERS_DECISION_EXPIRES",
        "IX_DEPOSIT_RELEASE_OFFERS_DECISION_KEEP_DEADLINE",
        "IX_DEPOSIT_RELEASE_EMAILS_DUE",
    ):
        assert name in sql


def test_downgrade_drops_only_those_tables():
    sql = _offline("down").upper()
    assert "DROP TABLE DEPOSIT_RELEASE_EMAILS" in sql
    assert "DROP TABLE DEPOSIT_RELEASE_OFFERS" in sql
    assert "ALTER TABLE" not in sql


def test_the_models_declare_what_the_migration_creates():
    offers = Base.metadata.tables["deposit_release_offers"]
    assert {c.name for c in offers.columns} == {
        "id", "tenant_id", "deposit_id", "released_appointment_id", "patient_id",
        "conversation_id", "deposit_paid", "decision", "refund_status", "refund_source",
        "released_at", "slot_start_at", "decide_by_at", "expires_at", "paid_notice_allowed",
        "first_contact_at", "refund_due_at", "refund_marked_at", "patient_answered_at",
        "last_reminder_at", "after_start_card_at", "keep_requested_at", "keep_deadline_at",
        "keep_outcome", "keep_resolved_at", "keep_resolved_by", "new_appointment_id", "closed_at",
        "created_at",
    }  # fmt: skip
    fk = {fk.parent.name: fk.ondelete for fk in offers.foreign_keys}
    assert fk["tenant_id"] == "CASCADE" and fk["released_appointment_id"] == "CASCADE"
    assert fk["patient_id"] == "SET NULL" and fk["new_appointment_id"] == "SET NULL"

    mails = Base.metadata.tables["deposit_release_emails"]
    assert {c.name for c in mails.columns} == {
        "id", "tenant_id", "offer_id", "kind", "seq", "due_at", "attempts", "claimed_at",
        "outcome", "created_at",
    }  # fmt: skip
    assert {fk.parent.name: fk.ondelete for fk in mails.foreign_keys}["offer_id"] == "CASCADE"
    assert "has_secretary" not in Base.metadata.tables["tenants"].c  # no manual field
```

Em `tests/test_deposit_before_migration.py`, no teste `test_the_revision_chains_after_the_provider_core_and_is_the_single_head`, troque `assert script.get_heads() == [REVISION]` por:

```python
    # TASK-052 chains `b6d2e8f4a1c3` after this one: the newest migration pins the head.
    assert len(script.get_heads()) == 1
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_deposit_release_offer_migration.py -q -p no:cacheprovider`
Expected: FAIL (a revisão e as tabelas ainda não existem).

- [ ] **Step 3: Implementar**

`src/secretaria/models/deposit_release_offer.py`:

```python
"""A slot the clinic released with a Pix deposit, the patient's choice, and what it schedules.

TASK-052, owner decisions 2026-10-12 (docs/superpowers/specs/2026-10-11-liberar-horario-com-
sinal-pago-design.md). The platform never returns money (decision D15) and does not decide for
the patient. One `DepositReleaseOffer` per released appointment that had a deposit, with two
independent axes:

`decision` - what the patient (and, for "Manter", the clinic) did:
    none / rescheduling      - still open ("Remarcar" tapped but not concluded = rescheduling);
    keep_requested           - "Manter horário" tapped: the CLINIC decides within 24 h of the tap
                               (`keep_deadline_at`); no B/E while it is open;
    kept                     - final: the clinic accepted; the deposit moved to `new_appointment_id`;
    keep_declined            - the clinic refused, or did not decide in 24 h (`keep_outcome`); the
                               refund is due; the patient may still reschedule or cancel;
    rebooked                 - final: a "Remarcar" booking carried the deposit;
    cancelled                - "Cancelar" tapped (the patient may still reschedule);
    no_answer_cancelled      - final: 24 h after the release without a final choice
                               ("Cancelado por falta de resposta"); never reopened;
    not_offered              - the deposit was NOT paid at the release: a record for the clinic's
                               list only, no buttons, no deadlines.

`refund_status` - the money:
    not_due -> due (cancel, or B with no answer) -> marked (the clinic says it refunded in the
    provider's panel) -> received / not_received (the patient's answer); due -> clinic_decided
    (the clinic resolved the attention item another way).

Every send is one `DepositReleaseEmail` row, claimed before it is sent, so a retry or a second
worker never sends it twice. LGPD: ids, instants, states and kinds only.
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

DECISION_NONE = "none"
DECISION_RESCHEDULING = "rescheduling"
DECISION_KEEP_REQUESTED = "keep_requested"
DECISION_KEPT = "kept"
DECISION_KEEP_DECLINED = "keep_declined"
DECISION_REBOOKED = "rebooked"
DECISION_CANCELLED = "cancelled"
DECISION_NO_ANSWER = "no_answer_cancelled"
DECISION_NOT_OFFERED = "not_offered"
# B and E apply only to these (no answer yet, or an unfinished "Remarcar").
SILENT_DECISIONS = (DECISION_NONE, DECISION_RESCHEDULING)
KEEP_FROM = (DECISION_NONE, DECISION_RESCHEDULING)
RESCHEDULE_FROM = (
    DECISION_NONE,
    DECISION_RESCHEDULING,
    DECISION_CANCELLED,
    DECISION_KEEP_REQUESTED,
    DECISION_KEEP_DECLINED,
)
CANCEL_FROM = (
    DECISION_NONE,
    DECISION_RESCHEDULING,
    DECISION_KEEP_REQUESTED,
    DECISION_KEEP_DECLINED,
)

KEEP_OUTCOME_ACCEPTED = "accepted"
KEEP_OUTCOME_REFUSED = "refused"
KEEP_OUTCOME_TIMEOUT = "timeout"

REFUND_NOT_DUE = "not_due"
REFUND_DUE = "due"
REFUND_MARKED = "marked"
REFUND_RECEIVED = "received"
REFUND_NOT_RECEIVED = "not_received"
REFUND_CLINIC_DECIDED = "clinic_decided"
# The states in which the patient's "Recebido" / "Não recebido" mean something.
REFUND_ANSWERABLE = (REFUND_MARKED, REFUND_NOT_RECEIVED)
# The clinic has not said it refunded: a "Remarcar" booking still carries the deposit.
REFUND_CARRYABLE = (REFUND_NOT_DUE, REFUND_DUE)

REFUND_SOURCE_PATIENT = "patient"
REFUND_SOURCE_TIMEOUT = "timeout"
REFUND_SOURCE_KEEP_REFUSED = "keep_refused"
REFUND_SOURCE_KEEP_TIMEOUT = "keep_timeout"

# `DepositReleaseEmail.kind` (<= 24 characters).
EMAIL_REFUND_CANCEL = "clinic_refund_cancel"  # at the "Cancelar" tap, always right away
EMAIL_REFUND_NOANSWER = "clinic_refund_noanswer"  # at B, nobody answered
EMAIL_REFUND_DAY = "clinic_refund_day"  # at E = T + 24 h
EMAIL_REFUND_REMINDER = "clinic_refund_reminder"  # "Não recebido" (seq 1, 2, ...)
EMAIL_KEEP_REQUEST = "clinic_keep_request"  # "Manter horário" tapped: the clinic decides
EMAIL_KEEP_DECLINED = "clinic_keep_declined"  # refused / no decision in 24 h: refund due
EMAIL_REBOOKED_DEPOSIT = "clinic_rebooked_deposit"  # "REMARCADA levando o sinal"
EMAIL_PATIENT_KEEP_ACCEPTED = "patient_keep_accepted"
EMAIL_PATIENT_KEEP_DECLINED = "patient_keep_declined"
EMAIL_PATIENT_CHECK = "patient_check"  # "Recebido / Não recebido", when the clinic marks refunded
# The clinic's refund notices (paused while a keep request is open; cancelled by a carry-over).
REFUND_NOTICE_KINDS = (
    EMAIL_REFUND_CANCEL,
    EMAIL_REFUND_NOANSWER,
    EMAIL_REFUND_DAY,
    EMAIL_REFUND_REMINDER,
    EMAIL_KEEP_DECLINED,
)

OUTCOME_SENDING = "sending"  # claimed; a send interrupted mid-way stays here (at most once)
OUTCOME_SENT = "sent"
OUTCOME_SKIPPED = "skipped"
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
    deposit_paid: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    decision: Mapped[str] = mapped_column(
        String(24), default=DECISION_NONE, server_default=DECISION_NONE
    )
    refund_status: Mapped[str] = mapped_column(
        String(16), default=REFUND_NOT_DUE, server_default=REFUND_NOT_DUE
    )
    refund_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    released_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # T
    slot_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # S
    decide_by_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # B
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # E = T + 24 h
    # The clinic's "may send a billed WhatsApp template" at release time, reused for the check.
    paid_notice_allowed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    first_contact_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refund_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refund_marked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    patient_answered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_reminder_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    after_start_card_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    keep_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    keep_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # K = the "Manter" tap + 24 h
    keep_outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    keep_resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    keep_resolved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    new_appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "released_appointment_id", name="uq_deposit_release_offers_released_appointment_id"
        ),
        Index("ix_deposit_release_offers_decision_decide_by", "decision", "decide_by_at"),
        Index("ix_deposit_release_offers_decision_expires", "decision", "expires_at"),
        Index("ix_deposit_release_offers_decision_keep_deadline", "decision", "keep_deadline_at"),
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
"""deposit release offers and their scheduled sends

TASK-052 (docs/superpowers/specs/2026-10-11-liberar-horario-com-sinal-pago-design.md). Purely
ADDITIVE (skill frozen-contract-migration): two new tables; no existing table is touched. Code
that predates this revision never reads them.

DEPLOY ORDER: this migration -> secretarIA API -> secretarIA worker RIGHT AFTER -> Brain-Message-
Frontend (copy of the release card) last.

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


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


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
        sa.Column("deposit_paid", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("decision", sa.String(24), nullable=False, server_default="none"),
        sa.Column("refund_status", sa.String(16), nullable=False, server_default="not_due"),
        sa.Column("refund_source", sa.String(16), nullable=True),
        _ts("released_at", nullable=False),
        _ts("slot_start_at"),
        _ts("decide_by_at", nullable=False),
        _ts("expires_at", nullable=False),
        sa.Column("paid_notice_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        _ts("first_contact_at"),
        _ts("refund_due_at"),
        _ts("refund_marked_at"),
        _ts("patient_answered_at"),
        _ts("last_reminder_at"),
        _ts("after_start_card_at"),
        _ts("keep_requested_at"),
        _ts("keep_deadline_at"),
        sa.Column("keep_outcome", sa.String(16), nullable=True),
        _ts("keep_resolved_at"),
        sa.Column("keep_resolved_by", sa.String(120), nullable=True),
        sa.Column(
            "new_appointment_id",
            sa.Uuid(),
            sa.ForeignKey("appointments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _ts("closed_at"),
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
        "ix_deposit_release_offers_decision_decide_by",
        "deposit_release_offers",
        ["decision", "decide_by_at"],
    )
    op.create_index(
        "ix_deposit_release_offers_decision_expires",
        "deposit_release_offers",
        ["decision", "expires_at"],
    )
    op.create_index(
        "ix_deposit_release_offers_decision_keep_deadline",
        "deposit_release_offers",
        ["decision", "keep_deadline_at"],
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
        _ts("due_at", nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        _ts("claimed_at"),
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
    for name in (
        "ix_deposit_release_offers_decision_keep_deadline",
        "ix_deposit_release_offers_decision_expires",
        "ix_deposit_release_offers_decision_decide_by",
        "ix_deposit_release_offers_conversation_id",
        "ix_deposit_release_offers_deposit_id",
        "ix_deposit_release_offers_tenant_id",
    ):
        op.drop_index(name, table_name="deposit_release_offers")
    op.drop_table("deposit_release_offers")
```

Em `src/secretaria/models/__init__.py`: depois de `from secretaria.models.conversation_pii_token_map import ConversationPiiTokenMap`, adicione `from secretaria.models.deposit_release_offer import DepositReleaseEmail, DepositReleaseOffer`; na lista `__all__`, depois de `"ConsentEvent",`, adicione `"DepositReleaseEmail",` e `"DepositReleaseOffer",`.

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_deposit_release_offer_migration.py tests/test_deposit_before_migration.py tests/test_payment_migration.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/models/deposit_release_offer.py src/secretaria/models/__init__.py migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py tests/test_deposit_release_offer_migration.py tests/test_deposit_before_migration.py`
Expected: PASS (os de Postgres opcionais `skipped`), ruff limpo.

- [ ] **Step 5 (opcional, recomendado): ida e volta num Postgres descartável**

Com um Postgres LOCAL descartável: `$env:PAYMENTS_TEST_PG_URL = "postgresql+asyncpg://postgres@127.0.0.1:<porta>/postgres"` e rode `tests/test_deposit_before_migration.py tests/test_payment_migration.py` (compara banco migrado × modelos). Desligue e apague depois. Sem Postgres: "NÃO PROVADO em Postgres" no `TASK.md`.

- [ ] **Step 6: Commit**

```powershell
git add src/secretaria/models/deposit_release_offer.py src/secretaria/models/__init__.py migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py tests/test_deposit_release_offer_migration.py tests/test_deposit_before_migration.py
git commit -m "feat(payments): release offer and scheduled send tables, additive migration (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 2: Os prazos B e E com o horário de fechamento (`services/release_deadlines.py`)

**Files:**
- Create: `src/secretaria/services/release_deadlines.py`
- Test: `tests/test_release_deadlines.py` (novo)

**Interfaces:**
- Produces: `DECIDE_HOURS = 6`, `CHECK_HOURS = 24`, `CLOSING_MARGIN = timedelta(minutes=30)`; `closing_time(business_hours: dict | None, day: date) -> time | None`; `decide_by(*, released_at: datetime, business_hours: dict | None, timezone: str | None) -> datetime` (UTC); `expires_at(released_at: datetime) -> datetime` (UTC). Puro: sem banco, sem relógio.

- [ ] **Step 1: Escrever o teste que falha**

`tests/test_release_deadlines.py`:

```python
"""services/release_deadlines.py - when a released PAID slot's refund falls due (TASK-052).

B = min(T + 6 h, 30 min before the clinic closes on T's day), in the clinic's zone; when the
clinic does not close AFTER T that day (closed, no hours, or T already past closing - 30 min),
B = T + 6 h. E = T + 24 h. Never before T.
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


def test_a_naive_instant_is_read_as_utc_and_e_is_one_day_later():
    naive = T.replace(tzinfo=None)
    assert rd.decide_by(released_at=naive, business_hours=None, timezone=SP) == T + timedelta(hours=6)
    assert rd.expires_at(naive) == T + timedelta(hours=24)


def test_b_is_never_before_t_nor_after_e():
    for end in ("09:00", "14:20", "14:31", "18:00", "23:59"):
        b = rd.decide_by(released_at=T, business_hours=_hours(end), timezone=SP)
        assert T <= b < rd.expires_at(T)
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_deadlines.py -q -p no:cacheprovider`
Expected: FAIL (`ImportError: cannot import name 'release_deadlines'`).

- [ ] **Step 3: Implementar**

`src/secretaria/services/release_deadlines.py`:

```python
"""The deadlines of a released PAID slot (TASK-052, owner decisions 2026-10-12). Pure.

T = the release instant. B (`decide_by`) = min(T + 6 h, F), F = 30 minutes before closing on T's
local day: the patient who has not answered by B makes the clinic return the deposit. E
(`expires_at`) = T + 24 h: an offer still undecided then is "cancelled for lack of answer".

`business_hours` is chosen by the CALLER (services/release_offer.py): the clinic's hours when it
has a secretary, else the hours of the consultation's doctor. Shape `{"monday": [{"start":
"08:00", "end": "12:00"}, ...]}` (schemas/config.py); closing = the latest `end` of T's weekday,
in `Tenant.timezone`. There is no holiday calendar in the product.

When the clinic does not close AFTER T that day (a day without windows, a clinic without hours,
or T already past closing - 30 min), B = T + 6 h: the patient keeps the time to decide and the
clinic reads the notice when it opens. Owner rule "never before T, B at least T" holds: T <= B
<= T + 6 h < E.
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


def expires_at(released_at: datetime) -> datetime:
    return _utc(released_at) + timedelta(hours=CHECK_HOURS)
```

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_deadlines.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/services/release_deadlines.py tests/test_release_deadlines.py`
Expected: PASS (9), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/services/release_deadlines.py tests/test_release_deadlines.py
git commit -m "feat(payments): decision deadlines of a released paid slot from the closing time (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 3: O serviço da oferta (`services/release_offer.py`), o motivo `released_refund_due` e o gancho de "devolvido"

**Files:**
- Create: `src/secretaria/services/release_offer.py`, `tests/release_offer_support.py`
- Modify: `src/secretaria/models/payment_attention_item.py` (motivo novo), `src/secretaria/services/payments/attention_view.py` (texto do motivo; devolução registrável; gancho em `resolve_item`), `tests/test_payment_attention.py`
- Test: `tests/test_release_offer.py` (novo)

**Interfaces:**
- Consumes: Task 1 (modelos/constantes), Task 2 (`decide_by`, `expires_at`); `attention.open_attention_item`, `attention.attention_dedupe_key`, `db_utils.insert_ignore`, `money.format_brl`, `staff_patient_message.conversation_id_for`, `tenant_config.professional_business_hours`, `core.whatsapp_limits.{MAX_INTERACTIVE_BODY_CHARS, truncate_plain}`, `attention_view.resolve_item`.
- Produces (`secretaria.services.release_offer`):
  - ações `ACTION_KEEP="relkeep"`, `ACTION_RESCHEDULE="relresched"`, `ACTION_CANCEL="relcancel"`, `ACTION_RECEIVED="relgot"`, `ACTION_NOT_RECEIVED="relmiss"`, `OFFER_ACTIONS`, `RELEASE_OFFER_ACTIONS`; `LABEL_*`; `DEPOSIT_OUTCOME_OFFERED="choice_offered"`; `REASON_CARRIED_OVER="carried_over"`; `REMINDER_INTERVAL`, `KEEP_WINDOW` (24 h); `REFUNDED_ACTION="refunded_in_provider_panel"`;
  - resultados: `KEEP_REQUESTED_NOW`, `KEEP_ALREADY_REQUESTED`, `KEEP_AFTER_START`, `KEEP_AFTER_REFUND`, `KEEP_DECLINED_BEFORE`, `KEEP_CLOSED`; `CANCEL_NOTIFIED`, `CANCEL_AFTER_REFUND`, `CANCEL_CLOSED`; `RESCHEDULE_CARRYING`, `RESCHEDULE_NEW_CHARGE`, `RESCHEDULE_NO_ANSWER`, `RESCHEDULE_CLOSED`;
  - textos (spec §5.3–§5.9): `OFFER_HEAD`, `OFFER_HEAD_CONFIRMED`, `CLINIC_NOTE_PREFIX`, `OFFER_DETAIL`, `OFFER_DETAIL_NO_KEEP`, `KEEP_REQUESTED_TEXT` (`{when}`), `KEEP_ALREADY_REQUESTED_TEXT`, `AFTER_START_TEXT`, `KEEP_AFTER_REFUND_TEXT`, `KEEP_DECLINED_TAP_TEXT`, `KEEP_ACCEPTED_TEXT` (`{when}`), `KEEP_DECLINED_TEXT` (`{when}`), `KEEP_UNAVAILABLE_TEXT`, `TAP_ALREADY_KEPT_TEXT` (`{when}`), `TAP_ALREADY_REBOOKED_TEXT`, `TAP_KEEP_AFTER_CANCEL_TEXT`, `CANCEL_DONE_TEXT` (`{amount}`), `CANCEL_AFTER_MARKED_TEXT`, `TAP_REFUND_ALREADY_TEXT`, `TAP_CANCEL_AFTER_KEEP_TEXT`, `OFFER_CLOSED_NO_ANSWER_TEXT`, `RESCHEDULE_PREFIX`, `RESCHEDULE_NEW_CHARGE_PREFIX`, `CARRIED_OVER_NOTE`, `CHECK_TEXT` (`{amount}`), `RECEIVED_TEXT`, `NOT_RECEIVED_TEXT`, `NOT_RECEIVED_RECENT_TEXT`, `CHECK_TOO_EARLY_TEXT`, `CHECK_CLOSED_TEXT`, `NOT_FOUND_TEXT`;
  - `offer_buttons(id, *, keep=True)`, `keep_fallback_buttons(id)`, `check_buttons(id)`; `when_text`, `slot_text`; `offer_body(*, deposit, decide_by_at, timezone, justification, confirmed, keep=True)`; `template_text`; `warning_text`; `console_conversation_link`; `keep_allowed(offer, now=None) -> bool`; `patient_decision(offer) -> str`;
  - `async open_offer(session, *, tenant, appointment, deposit, now=None, paid_notice_allowed=False, has_secretary=None)`; `async find_offer(...)`; `async record_contact(session, *, offer_id, now)`; `async request_keep(session, *, offer, now=None) -> str`; `async claim_keep(session, *, offer, new_appointment, resolved_by, now=None) -> bool`; `async decline_keep(session, *, offer, outcome, resolved_by=None, now=None) -> bool`; `async choose_cancel(...) -> str`; `async choose_reschedule(session, *, offer, conversation_id, now=None) -> str`; `async settle_if_expired(...)` (B); `async close_if_no_answer(...)` (E); `async expire_offers(session, *, now=None, limit=50) -> int` (B, E e K; comita); `async carry_over_pending(...)`; `async claim_carry_over(...) -> PixDeposit | None`; `async record_received(...)`; `async request_reminder(...)`; `async on_refund_recorded(session, *, tenant_id, item, now)`; `async mark_refunded(session, *, tenant_id, offer_id, note, resolved_by_name, now=None) -> str` (`marked`/`already`/`not_due`/`keep_pending`/`not_found`; comita); `async skip_kinds(session, offer_id, kinds, now)`.
  - Nada comita, exceto `expire_offers` e `mark_refunded`.

- [ ] **Step 1: Escrever os testes que falham**

`tests/release_offer_support.py` (ajudante, não é teste):

```python
"""Shared helpers for the TASK-052 tests (a released appointment with a deposit). Not a test
module: one clinic, one patient, one conversation, the released (CANCELLED) appointment and its
deposit, on the in-memory SQLite database of tests/payments_support.py.
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
    Professional,
    Tenant,
)
from secretaria.models.deposit_release_offer import DepositReleaseEmail
from tests.before_support import make_world

# T: Tuesday 2030-03-05, 14:00 in Sao Paulo. S: the released consultation, Friday 08/03, 10:00.
T = datetime(2030, 3, 5, 17, 0, tzinfo=UTC)
RELEASED_START = datetime(2030, 3, 8, 13, 0, tzinfo=UTC)
# Closes at 22:00 on Tuesdays: F = 21:30, B = min(T + 6 h, F) = 20:00 local = T + 6 h.
OPEN_LATE = {"tuesday": [{"start": "08:00", "end": "22:00"}]}
# Closes at 18:00 on Tuesdays: F = 17:30 local = 20:30 UTC = T + 3 h 30 min.
CLOSES_AT_SIX = {"tuesday": [{"start": "08:00", "end": "18:00"}]}


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
    professional_hours: dict | None = None,
    start: datetime = RELEASED_START,
    **tenant_kw,
) -> Released:
    """A clinic whose appointment was just RELEASED (status CANCELLED) with a deposit.

    `professional_hours` given: the appointment belongs to a doctor with those hours."""
    tenant_kw.setdefault("contact_email", "clinica@exemplo.com")
    world = await make_world(
        session,
        channel=channel,
        timing=timing,
        business_hours=OPEN_LATE if business_hours is None else business_hours,
        **tenant_kw,
    )
    professional_id = None
    if professional_hours is not None:
        professional = Professional(
            id=uuid4(), tenant_id=world.tenant_id, name="Dra. Ana", business_hours=professional_hours
        )
        session.add(professional)
        await session.flush()
        professional_id = professional.id
    appointment = Appointment(
        id=uuid4(),
        tenant_id=world.tenant_id,
        patient_id=world.patient.id,
        conversation_id=world.conversation.id,
        google_event_id=f"evt-{uuid4().hex[:8]}",
        appointment_type="Consulta",
        phone=world.patient.wa_id,
        professional_id=professional_id,
        status=AppointmentStatus.CANCELLED,
        start_at=start,
        end_at=start + timedelta(minutes=30),
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
"""services/release_offer.py - a slot the clinic released with a PAID deposit (TASK-052).

Owner decisions 2026-10-12 (3rd round). D15: the platform NEVER refunds. "Manter" is a request to
the CLINIC (24 h to decide); "Remarcar" carries the deposit until the clinic says it refunded;
"Cancelar" always notifies the clinic at once. Every transition is one guarded UPDATE.
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
from secretaria.models.pix_deposit import FULFILLMENT_NEEDS_ATTENTION, FULFILLMENT_RESOLVED
from secretaria.services import release_offer as ro
from secretaria.services.payments import attention_view
from tests.payments_support import make_sessionmaker
from tests.release_offer_support import (
    CLOSES_AT_SIX,
    RELEASED_START,
    T,
    emails,
    make_released,
    new_appointment,
    utc,
)

B = T + timedelta(hours=6)
E = T + timedelta(hours=24)
SP = "America/Sao_Paulo"


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


async def _offer(db, *, now=T, has_secretary=None, **kw):
    async with db() as session:
        released = await make_released(session, **kw)
        offer = await ro.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=now,
            paid_notice_allowed=True,
            has_secretary=has_secretary,
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
        return [(e.kind, e.seq, utc(e.due_at), e.outcome) for e in await emails(session, offer_id)]


async def _kinds(db, offer_id, *, pending_only=False) -> list[str]:
    return [k for k, _s, _d, out in await _schedule(db, offer_id) if not pending_only or out is None]


async def _call(db, fn, offer_id, at, **kw):
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer_id)
        result = await fn(session, offer=row, now=at, **kw)
        await session.commit()
    return result


async def _sweep(db, at) -> int:
    async with db() as session:
        return await ro.expire_offers(session, now=at)


async def _mark(db, released, offer_id, at=None):
    async with db() as session:
        return await ro.mark_refunded(
            session,
            tenant_id=released.tenant_id,
            offer_id=offer_id,
            note="Estornado no painel do Asaas",
            resolved_by_name="Ana",
            now=at or T + timedelta(hours=3),
        )


async def _carry(db, released, at, *, days=4):
    async with db() as session:
        fresh = await new_appointment(session, released, start=RELEASED_START + timedelta(days=days))
        carried = await ro.claim_carry_over(
            session,
            tenant_id=released.tenant_id,
            conversation_id=released.conversation.id,
            new_appointment=fresh,
            now=at,
        )
        await session.commit()
    return fresh, carried


# --- copy and buttons ----------------------------------------------------------------------------


def test_the_cards_fit_whatsapp_and_name_the_choices():
    rid = uuid4()
    for buttons, labels in (
        (ro.offer_buttons(rid), ["Manter horário", "Remarcar", "Cancelar"]),
        (ro.offer_buttons(rid, keep=False), ["Remarcar", "Cancelar"]),
        (ro.keep_fallback_buttons(rid), ["Remarcar", "Cancelar"]),
        (ro.check_buttons(rid), ["Recebido", "Não recebido"]),
    ):
        assert [label for _id, label in buttons] == labels
        for button_id, label in buttons:
            assert len(label) <= MAX_BUTTON_LABEL_CHARS and len(button_id) <= MAX_BUTTON_ID_CHARS
            assert button_id.split("|")[0] in ro.RELEASE_OFFER_ACTIONS
            assert button_id.endswith(f"|{rid}")


def test_the_texts_say_the_clinic_decides_and_never_name_a_bank():
    deposit = PixDeposit(amount_cents=6000)
    body = ro.offer_body(
        deposit=deposit, decide_by_at=B, timezone=SP, justification=None, confirmed=False
    )
    assert body.startswith(ro.OFFER_HEAD) and "R$ 60,00" in body and "até 20:00 de 05/03" in body
    assert "*Manter horário* para pedir à clínica o mesmo horário" in body
    no_keep = ro.offer_body(
        deposit=deposit, decide_by_at=B, timezone=SP, justification=None, confirmed=False, keep=False
    )
    assert "Manter" not in no_keep
    long = ro.offer_body(
        deposit=deposit, decide_by_at=B, timezone=SP, justification="x" * 3000, confirmed=True
    )
    assert len(long) <= MAX_INTERACTIVE_BODY_CHARS and long.startswith(ro.OFFER_HEAD_CONFIRMED)
    assert "\n" not in ro.template_text(long)
    assert "decide em até 24 horas" in ro.KEEP_REQUESTED_TEXT
    assert ro.AFTER_START_TEXT.startswith("O horário da consulta já passou")
    for text in (body, ro.CHECK_TEXT, ro.KEEP_DECLINED_TEXT, ro.CANCEL_DONE_TEXT):
        assert "banco" not in text.lower()
        for promise in ("devolvemos", "estorno automático", "reembolsamos"):
            assert promise not in text
    assert "conta de onde saiu o Pix" in ro.CHECK_TEXT


# --- opening -------------------------------------------------------------------------------------


async def test_opening_is_idempotent_and_sets_the_instants(db):
    released, offer = await _offer(db)
    assert (offer.decision, offer.refund_status, offer.deposit_paid) == ("none", "not_due", True)
    assert (utc(offer.released_at), utc(offer.decide_by_at), utc(offer.expires_at)) == (T, B, E)
    assert utc(offer.slot_start_at) == RELEASED_START
    async with db() as session:
        again = await ro.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=T + timedelta(minutes=5),
        )
        await session.commit()
        count = await session.scalar(select(func.count()).select_from(DepositReleaseOffer))
    assert again.id == offer.id and count == 1 and await _items(db) == []

    _other, unpaid = await _offer(db, deposit_status=PixDepositStatus.AWAITING)
    assert (unpaid.deposit_paid, unpaid.decision) == (False, "not_offered")
    assert await _sweep(db, E + timedelta(days=3)) == 0  # nothing ever fires on it


async def test_the_closing_is_the_clinics_with_a_secretary_else_the_doctors(db):
    # The doctor closes at 18:00 (F = T + 3 h 30); the clinic at 22:00 (B = T + 6 h).
    _a, doctors = await _offer(db, professional_hours=CLOSES_AT_SIX, has_secretary=False)
    _b, unknown = await _offer(db, professional_hours=CLOSES_AT_SIX, has_secretary=None)
    _c, clinics = await _offer(db, professional_hours=CLOSES_AT_SIX, has_secretary=True)
    _d, no_doctor = await _offer(db, business_hours=CLOSES_AT_SIX)
    assert utc(doctors.decide_by_at) == T + timedelta(hours=3, minutes=30)
    assert utc(unknown.decide_by_at) == T + timedelta(hours=3, minutes=30)  # brain-api silent
    assert utc(clinics.decide_by_at) == B
    assert utc(no_doctor.decide_by_at) == T + timedelta(hours=3, minutes=30)


async def test_a_deposit_of_another_clinic_never_gets_an_offer(db):
    released, _ = await _offer(db)
    other, _ = await _offer(db)
    async with db() as session:
        crossed = await ro.open_offer(
            session, tenant=other.tenant, appointment=released.appointment, deposit=released.deposit
        )
    assert crossed is None


# --- Manter: a request to the clinic -------------------------------------------------------------


async def test_manter_asks_the_clinic_once_and_recreates_nothing(db):
    _released, offer = await _offer(db)
    at = T + timedelta(hours=1)

    assert await _call(db, ro.request_keep, offer.id, at) == ro.KEEP_REQUESTED_NOW
    assert await _call(db, ro.request_keep, offer.id, at) == ro.KEEP_ALREADY_REQUESTED

    row = await _row(db, offer.id)
    assert row.decision == "keep_requested" and utc(row.keep_deadline_at) == at + timedelta(hours=24)
    assert utc(row.first_contact_at) == at and row.new_appointment_id is None
    assert await _kinds(db, offer.id) == ["clinic_keep_request"]


async def test_manter_after_b_is_allowed_and_pauses_b_and_e(db):
    _released, offer = await _offer(db)
    await _sweep(db, B)  # nobody answered: the refund fell due at B

    assert await _call(db, ro.request_keep, offer.id, B + timedelta(hours=1)) == ro.KEEP_REQUESTED_NOW

    assert await _sweep(db, E) == 0  # an open negotiation is not "no answer"
    assert (await _row(db, offer.id)).decision == "keep_requested"


async def test_manter_after_s_or_after_the_refund_was_marked_is_answered_not_recorded(db):
    _a, soon = await _offer(db, start=T + timedelta(hours=2))
    assert await _call(db, ro.request_keep, soon.id, T + timedelta(hours=3)) == ro.KEEP_AFTER_START

    released, offer = await _offer(db)
    await _sweep(db, B)
    await _mark(db, released, offer.id, at=B + timedelta(minutes=5))
    assert await _call(db, ro.request_keep, offer.id, B + timedelta(hours=1)) == ro.KEEP_AFTER_REFUND
    assert (await _row(db, offer.id)).decision == "none"


async def test_the_clinic_refusing_makes_the_refund_due_and_tells_both(db):
    _released, offer = await _offer(db)
    await _call(db, ro.request_keep, offer.id, T + timedelta(hours=1))
    at = T + timedelta(hours=2)

    assert await _call(db, ro.decline_keep, offer.id, at, outcome="refused", resolved_by="Ana")
    assert not await _call(db, ro.decline_keep, offer.id, at, outcome="refused", resolved_by="Ana")

    row = await _row(db, offer.id)
    assert (row.decision, row.keep_outcome, row.keep_resolved_by) == ("keep_declined", "refused", "Ana")
    assert (row.refund_status, row.refund_source) == ("due", "keep_refused")
    assert len(await _items(db)) == 1
    schedule = await _schedule(db, offer.id)
    assert ("clinic_keep_declined", 0, at, None) in schedule
    assert ("patient_keep_declined", 0, at, None) in schedule
    assert ("clinic_refund_day", 1, at + timedelta(hours=24), None) in schedule


async def test_no_clinic_decision_within_24h_of_the_tap_counts_as_a_refusal(db):
    _released, offer = await _offer(db)
    tap = T + timedelta(hours=1)
    await _call(db, ro.request_keep, offer.id, tap)

    assert await _sweep(db, tap + timedelta(hours=23, minutes=59)) == 0
    assert await _sweep(db, tap + timedelta(hours=24)) == 1

    row = await _row(db, offer.id)
    assert (row.decision, row.keep_outcome, row.refund_source) == (
        "keep_declined",
        "timeout",
        "keep_timeout",
    )


async def test_the_clinic_accepting_moves_the_deposit_and_closes_the_refund(db):
    released, offer = await _offer(db)
    await _sweep(db, B)  # the refund was due (item open, notices scheduled)...
    await _call(db, ro.request_keep, offer.id, B + timedelta(minutes=10))
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        again = await new_appointment(session, released, start=RELEASED_START)
        assert await ro.claim_keep(
            session, offer=row, new_appointment=again, resolved_by="Ana", now=B + timedelta(hours=1)
        )
        await session.commit()

    kept = await _row(db, offer.id)
    assert (kept.decision, kept.keep_outcome, kept.new_appointment_id) == ("kept", "accepted", again.id)
    assert kept.refund_status == "not_due"  # ...and is not any more: the money is in use
    deposit = await _deposit(db, released.deposit.id)
    assert deposit.appointment_id == again.id and deposit.status is PixDepositStatus.PAID
    assert deposit.fulfillment_status == FULFILLMENT_RESOLVED
    (item,) = await _items(db)
    assert (item.status, item.resolution_action) == ("resolved", "rebooked")
    # The refund notices of B/E are dropped; what is left is the request and the patient's answer.
    assert await _kinds(db, offer.id, pending_only=True) == [
        "clinic_keep_request",
        "patient_keep_accepted",
    ]


async def test_accepting_after_the_patient_moved_on_is_refused(db):
    released, offer = await _offer(db)
    await _call(db, ro.request_keep, offer.id, T + timedelta(hours=1))
    await _call(db, ro.choose_reschedule, offer.id, T + timedelta(hours=2),
                conversation_id=released.conversation.id)  # fmt: skip
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        late = await new_appointment(session, released, start=RELEASED_START)
        assert not await ro.claim_keep(
            session, offer=row, new_appointment=late, resolved_by="Ana", now=T + timedelta(hours=3)
        )


# --- Cancelar ------------------------------------------------------------------------------------


async def test_cancel_always_notifies_the_clinic_now(db):
    _a, before_s = await _offer(db)
    _b, after_s = await _offer(db, start=T + timedelta(hours=2))
    _c, after_b = await _offer(db, now=T - timedelta(hours=7))  # its B already passed
    await _sweep(db, T)
    for offer, at in ((before_s, T + timedelta(hours=1)), (after_s, T + timedelta(hours=3)),
                      (after_b, T + timedelta(hours=1))):  # fmt: skip
        assert await _call(db, ro.choose_cancel, offer.id, at) == ro.CANCEL_NOTIFIED
        assert "clinic_refund_cancel" in await _kinds(db, offer.id)
        row = await _row(db, offer.id)
        assert (row.decision, row.refund_status) == ("cancelled", "due")
    assert len(await _items(db)) == 3  # one per deposit, never two


async def test_cancel_after_the_clinic_refused_manter_still_notifies(db):
    _released, offer = await _offer(db)
    await _call(db, ro.request_keep, offer.id, T + timedelta(hours=1))
    await _call(db, ro.decline_keep, offer.id, T + timedelta(hours=2), outcome="refused")

    assert await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=3)) == ro.CANCEL_NOTIFIED
    assert len(await _items(db)) == 1


async def test_cancel_after_marked_twice_after_kept_or_after_e(db):
    released, marked = await _offer(db)
    await _sweep(db, B)
    await _mark(db, released, marked.id, at=B + timedelta(minutes=5))
    assert await _call(db, ro.choose_cancel, marked.id, B + timedelta(hours=1)) == ro.CANCEL_AFTER_REFUND
    assert "clinic_refund_cancel" not in await _kinds(db, marked.id)

    _x, twice = await _offer(db)
    assert await _call(db, ro.choose_cancel, twice.id, T + timedelta(hours=1)) == ro.CANCEL_NOTIFIED
    assert await _call(db, ro.choose_cancel, twice.id, T + timedelta(hours=2)) == ro.CANCEL_CLOSED

    _y, silent = await _offer(db)
    await _sweep(db, E)
    assert await _call(db, ro.choose_cancel, silent.id, E + timedelta(minutes=1)) == ro.CANCEL_CLOSED
    assert (await _row(db, silent.id)).decision == "no_answer_cancelled"


# --- B and E -------------------------------------------------------------------------------------


async def test_b_then_e_without_any_contact(db):
    _released, offer = await _offer(db)

    assert await _sweep(db, B) == 1
    row = await _row(db, offer.id)
    assert (row.decision, row.refund_status, row.refund_source) == ("none", "due", "timeout")
    assert await _kinds(db, offer.id) == ["clinic_refund_noanswer", "clinic_refund_day"]

    assert await _sweep(db, E) == 1
    row = await _row(db, offer.id)
    assert row.decision == "no_answer_cancelled" and row.first_contact_at is None
    assert ro.patient_decision(row) == "cancelled_no_answer"
    assert await _sweep(db, E + timedelta(hours=1)) == 0


async def test_an_unfinished_remarcar_is_cancelled_for_lack_of_answer_at_e(db):
    released, offer = await _offer(db)
    tap = T + timedelta(hours=1)
    async with db() as session:
        await ro.record_contact(session, offer_id=offer.id, now=tap)
        await session.commit()
    assert await _call(db, ro.choose_reschedule, offer.id, tap,
                       conversation_id=released.conversation.id) == ro.RESCHEDULE_CARRYING  # fmt: skip

    await _sweep(db, E)  # one late tick: B and E together

    row = await _row(db, offer.id)
    assert (row.decision, row.refund_status) == ("no_answer_cancelled", "due")
    assert utc(row.first_contact_at) == tap


# --- Remarcar ------------------------------------------------------------------------------------


async def test_remarcar_carries_until_the_clinic_marks_refunded(db):
    released, offer = await _offer(db)
    await _sweep(db, B)  # after B: still carries
    resched = ro.choose_reschedule
    conv = released.conversation.id
    assert await _call(db, resched, offer.id, B + timedelta(hours=1), conversation_id=conv) == (
        ro.RESCHEDULE_CARRYING
    )
    await _mark(db, released, offer.id, at=B + timedelta(hours=2))
    assert await _call(db, resched, offer.id, B + timedelta(hours=3), conversation_id=conv) == (
        ro.RESCHEDULE_NEW_CHARGE
    )
    async with db() as session:
        assert not await ro.carry_over_pending(session, tenant_id=released.tenant_id, conversation_id=conv)


async def test_remarcar_after_cancel_or_after_a_refusal_still_carries(db):
    released, cancelled = await _offer(db)
    await _call(db, ro.choose_cancel, cancelled.id, T + timedelta(hours=1))
    assert await _call(db, ro.choose_reschedule, cancelled.id, T + timedelta(hours=2),
                       conversation_id=released.conversation.id) == ro.RESCHEDULE_CARRYING  # fmt: skip

    other, declined = await _offer(db)
    await _call(db, ro.request_keep, declined.id, T + timedelta(hours=1))
    await _call(db, ro.decline_keep, declined.id, T + timedelta(hours=2), outcome="refused")
    assert await _call(db, ro.choose_reschedule, declined.id, T + timedelta(hours=3),
                       conversation_id=other.conversation.id) == ro.RESCHEDULE_CARRYING  # fmt: skip


async def test_remarcar_after_e_or_after_kept_reopens_nothing(db):
    released, offer = await _offer(db)
    await _sweep(db, E)
    assert await _call(db, ro.choose_reschedule, offer.id, E + timedelta(hours=1),
                       conversation_id=released.conversation.id) == ro.RESCHEDULE_NO_ANSWER  # fmt: skip
    assert (await _row(db, offer.id)).decision == "no_answer_cancelled"


async def test_a_confirmed_remarcar_carries_the_deposit_cancels_the_refund_and_tells_the_clinic(db):
    released, offer = await _offer(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))  # refund due, notices on
    await _call(db, ro.choose_reschedule, offer.id, T + timedelta(hours=2),
                conversation_id=released.conversation.id)  # fmt: skip

    fresh, carried = await _carry(db, released, T + timedelta(hours=3))
    _again, second = await _carry(db, released, T + timedelta(hours=4), days=5)

    assert carried is not None and carried.appointment_id == fresh.id and second is None
    assert carried.reschedule_count == 0
    row = await _row(db, offer.id)
    assert (row.decision, row.refund_status, row.new_appointment_id) == ("rebooked", "not_due", fresh.id)
    (item,) = await _items(db)
    assert (item.status, item.resolution_action) == ("resolved", "rebooked")
    assert await _kinds(db, offer.id, pending_only=True) == ["clinic_rebooked_deposit"]
    assert ro.patient_decision(row) == "rescheduled"


async def test_another_clinic_carries_nothing(db):
    released, offer = await _offer(db)
    other, _ = await _offer(db)
    await _call(db, ro.choose_reschedule, offer.id, T, conversation_id=released.conversation.id)
    async with db() as session:
        foreign = await new_appointment(session, other, start=RELEASED_START + timedelta(days=4))
        assert (
            await ro.claim_carry_over(
                session,
                tenant_id=other.tenant_id,
                conversation_id=released.conversation.id,
                new_appointment=foreign,
                now=T,
            )
            is None
        )
    assert (await _deposit(db, released.deposit.id)).appointment_id == released.appointment.id


async def test_a_tap_that_loses_to_the_sweep_opens_no_second_item(db):
    _released, offer = await _offer(db)
    async with db() as session:
        stale = await session.get(DepositReleaseOffer, offer.id)
    await _sweep(db, B)
    async with db() as session:
        assert not await ro.settle_if_expired(session, offer=stale, now=B)
    assert len(await _items(db)) == 1


# --- devolvido / Recebido / Não recebido -------------------------------------------------------


async def test_marking_refunded_rules(db):
    released, offer = await _offer(db)
    assert await _mark(db, released, offer.id) == "not_due"  # the patient may still choose
    await _call(db, ro.request_keep, offer.id, T + timedelta(hours=1))
    await _sweep(db, B)  # paused: nothing falls due during the negotiation
    assert await _mark(db, released, offer.id) == "keep_pending"

    other, due = await _offer(db)
    await _sweep(db, B)
    await _call(db, ro.request_keep, due.id, B + timedelta(minutes=1))  # after B: refund was due
    assert await _mark(db, other, due.id) == "keep_pending"  # decide "Manter" first
    await _call(db, ro.decline_keep, due.id, B + timedelta(hours=1), outcome="refused")
    assert await _mark(db, other, due.id, at=B + timedelta(hours=2)) == "marked"
    assert await _mark(db, other, due.id, at=B + timedelta(hours=3)) == "already"
    assert [k for k in await _kinds(db, due.id) if k == "patient_check"] == ["patient_check"]
    assert (await _deposit(db, other.deposit.id)).status is PixDepositStatus.CANCELLED_REFUNDED

    async with db() as session:
        outcome = await ro.mark_refunded(
            session, tenant_id=released.tenant_id, offer_id=due.id, note="Estornado no painel",
            resolved_by_name="Ana",
        )  # fmt: skip
    assert outcome == "not_found"  # another clinic's offer


async def test_resolving_the_item_another_way_ends_every_notice(db):
    released, offer = await _offer(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    (item,) = await _items(db)
    async with db() as session:
        await attention_view.resolve_item(
            session,
            released.tenant_id,
            item.id,
            action="kept_as_credit",
            note="Crédito para a próxima consulta",
            resolved_by_name="Ana",
        )
    assert (await _row(db, offer.id)).refund_status == "clinic_decided"
    assert await _kinds(db, offer.id, pending_only=True) == []
    assert (await _deposit(db, released.deposit.id)).fulfillment_status != FULFILLMENT_NEEDS_ATTENTION


async def test_received_and_not_received(db):
    released, offer = await _offer(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    assert await _call(db, ro.record_received, offer.id, T + timedelta(hours=2)) == "too_early"
    await _mark(db, released, offer.id)

    assert await _call(db, ro.request_reminder, offer.id, T + timedelta(hours=4)) == "sent"
    assert await _call(db, ro.request_reminder, offer.id, T + timedelta(hours=8)) == "recent"
    assert await _call(db, ro.request_reminder, offer.id, T + timedelta(hours=29)) == "sent"
    assert await _call(db, ro.record_received, offer.id, T + timedelta(hours=30)) == "closed"
    assert await _call(db, ro.record_received, offer.id, T + timedelta(hours=31)) == "already_closed"

    reminders = [(k, s) for k, s, _d, _o in await _schedule(db, offer.id) if k == "clinic_refund_reminder"]
    assert reminders == [("clinic_refund_reminder", 1), ("clinic_refund_reminder", 2)]
    assert await _kinds(db, offer.id, pending_only=True) == []  # received: the E reminder never goes


def test_patient_decision_maps_every_state_for_the_clinic_list():
    offer = DepositReleaseOffer(decision="none", refund_status="not_due")
    cases = {
        ("none", "not_due"): "no_answer",
        ("rescheduling", "due"): "no_answer",
        ("keep_requested", "not_due"): "keep_requested",
        ("kept", "not_due"): "kept",
        ("keep_declined", "due"): "keep_declined",
        ("rebooked", "not_due"): "rescheduled",
        ("cancelled", "due"): "cancelled",
        ("no_answer_cancelled", "due"): "cancelled_no_answer",
        ("cancelled", "received"): "received",
        ("no_answer_cancelled", "not_received"): "not_received",
        ("not_offered", "not_due"): "not_offered",
    }
    for (decision, refund), expected in cases.items():
        offer.decision, offer.refund_status = decision, refund
        assert ro.patient_decision(offer) == expected
```

Em `tests/test_payment_attention.py`, no conjunto de motivos esperado, adicione `"released_refund_due",` depois de `"refunded_externally",`.

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_offer.py tests/test_payment_attention.py -q -p no:cacheprovider`
Expected: FAIL (`ImportError: cannot import name 'release_offer'`; motivo ausente de `ATTENTION_REASONS`).

- [ ] **Step 3: Implementar**

Em `src/secretaria/models/payment_attention_item.py`, depois de `REASON_REFUNDED_EXTERNALLY = "refunded_externally"`:

```python
# TASK-052: the clinic released a PAID slot and the refund fell due (cancelled, no answer by B or
# in 24 h, or the clinic refused "Manter") - the clinic returns the deposit in the provider's panel.
REASON_RELEASED_REFUND_DUE = "released_refund_due"
```

e em `ATTENTION_REASONS`, depois de `REASON_REFUNDED_EXTERNALLY,`, adicione `REASON_RELEASED_REFUND_DUE,`.

Em `src/secretaria/services/payments/attention_view.py`:
1. No import de `models.payment_attention_item`, adicione `REASON_RELEASED_REFUND_DUE,` (depois de `REASON_REFUND_DUE_POLICY,`).
2. No dicionário de textos por motivo, depois da linha de `"refunded_externally"`:

```python
    "released_refund_due": "Horário liberado pela clínica: devolver o sinal ao paciente.",
```

3. Em `_REFUND_RECORDING_REASONS`, depois de `REASON_PROMOTION_FAILED,`, adicione `REASON_RELEASED_REFUND_DUE,`.
4. Em `resolve_item`, troque

```python
    await _close_deposit_story(session, tenant_id, item, action)
    await session.commit()
    return "resolved"
```

por

```python
    await _close_deposit_story(session, tenant_id, item, action)
    # TASK-052: a released PAID slot's refund item. "Refunded in the provider's panel" asks the
    # patient "Recebido / Não recebido"; any other decision ends the clinic's notices. Lazy
    # import: services/release_offer.py imports this package.
    from secretaria.services import release_offer

    await release_offer.on_refund_recorded(session, tenant_id=tenant_id, item=item, now=now)
    await session.commit()
    return "resolved"
```

`src/secretaria/services/release_offer.py`:

```python
"""A slot the CLINIC released with a PAID deposit (TASK-052, owner decisions 2026-10-12, 3rd round).

Instants: T release, S consultation start, B = min(T + 6 h, closing - 30 min), E = T + 24 h,
K = the "Manter" tap + 24 h. The platform never returns money (decision D15).

* `open_offer` (hub release): ONE `DepositReleaseOffer` per released appointment with a deposit;
  closing = the clinic's when it has a secretary (brain-api roles, services/brain_staff.py), else
  the doctor's.
* "Manter horário" (`request_keep`, T..S, also after B) asks the CLINIC: `keep_requested` for 24 h
  (K). The clinic accepts (services/release_keep.py -> `claim_keep`) or refuses
  (`decline_keep`); no decision by K is a refusal. B and E do not apply while it is open and the
  refund notices that fall due meanwhile are dropped (the clinic itself is deciding).
* "Remarcar" (`choose_reschedule`) at any time while the offer is open: the deposit carries over
  unless the clinic already said it refunded; the confirmed booking (`claim_carry_over`) closes the
  refund (item, notices) and tells the clinic "REMARCADA levando o sinal".
* "Cancelar" (`choose_cancel`) always tells the clinic at once (unless it already refunded).
* No answer: B -> refund due + notice; E -> "Cancelado por falta de resposta" (terminal).
* The clinic marks "devolvido" (`mark_refunded`, or resolving the item -> `on_refund_recorded`):
  the patient answers "Recebido / Não recebido".

Every transition is ONE guarded UPDATE; the item is deduped per deposit; every send is unique per
(offer, kind, seq); every query carries the tenant id the caller resolved. Never logs a name,
phone, e-mail or text. Nothing here commits unless the docstring says so.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS, truncate_plain
from secretaria.models import (
    Appointment,
    Patient,
    PixDeposit,
    PixDepositStatus,
    Professional,
    Tenant,
)
from secretaria.models.deposit_release_offer import (
    CANCEL_FROM,
    DECISION_CANCELLED,
    DECISION_KEEP_DECLINED,
    DECISION_KEEP_REQUESTED,
    DECISION_KEPT,
    DECISION_NO_ANSWER,
    DECISION_NONE,
    DECISION_NOT_OFFERED,
    DECISION_REBOOKED,
    DECISION_RESCHEDULING,
    EMAIL_KEEP_DECLINED,
    EMAIL_KEEP_REQUEST,
    EMAIL_PATIENT_CHECK,
    EMAIL_PATIENT_KEEP_ACCEPTED,
    EMAIL_PATIENT_KEEP_DECLINED,
    EMAIL_REBOOKED_DEPOSIT,
    EMAIL_REFUND_CANCEL,
    EMAIL_REFUND_DAY,
    EMAIL_REFUND_NOANSWER,
    EMAIL_REFUND_REMINDER,
    KEEP_FROM,
    KEEP_OUTCOME_ACCEPTED,
    KEEP_OUTCOME_REFUSED,
    KEEP_OUTCOME_TIMEOUT,
    OUTCOME_SKIPPED,
    REFUND_ANSWERABLE,
    REFUND_CARRYABLE,
    REFUND_CLINIC_DECIDED,
    REFUND_DUE,
    REFUND_MARKED,
    REFUND_NOT_DUE,
    REFUND_NOT_RECEIVED,
    REFUND_NOTICE_KINDS,
    REFUND_RECEIVED,
    REFUND_SOURCE_KEEP_REFUSED,
    REFUND_SOURCE_KEEP_TIMEOUT,
    REFUND_SOURCE_PATIENT,
    REFUND_SOURCE_TIMEOUT,
    RESCHEDULE_FROM,
    SILENT_DECISIONS,
    DepositReleaseEmail,
    DepositReleaseOffer,
)
from secretaria.models.payment_attention_item import (
    ATTENTION_OPEN,
    ATTENTION_RESOLVED,
    REASON_RELEASED_REFUND_DUE,
    PaymentAttentionItem,
)
from secretaria.models.pix_deposit import FULFILLMENT_NEEDS_ATTENTION, FULFILLMENT_RESOLVED
from secretaria.services import release_deadlines
from secretaria.services.payments import attention
from secretaria.services.payments.db_utils import insert_ignore
from secretaria.services.payments.money import format_brl
from secretaria.services.staff_patient_message import conversation_id_for
from secretaria.services.tenant_config import professional_business_hours

logger = get_logger(__name__)

DEFAULT_TIMEZONE = "America/Sao_Paulo"
REMINDER_INTERVAL = timedelta(hours=24)
KEEP_WINDOW = timedelta(hours=24)
REFUNDED_ACTION = "refunded_in_provider_panel"
CARRIED_RESOLUTION_NOTE = "Remarcada pelo paciente levando o sinal (TASK-052)."
KEPT_RESOLUTION_NOTE = "Horário mantido pela clínica com o mesmo sinal (TASK-052)."
SYSTEM_NAME = "SecretarIA"

ACTION_KEEP = "relkeep"
ACTION_RESCHEDULE = "relresched"
ACTION_CANCEL = "relcancel"
ACTION_RECEIVED = "relgot"
ACTION_NOT_RECEIVED = "relmiss"
OFFER_ACTIONS: tuple[str, ...] = (ACTION_KEEP, ACTION_RESCHEDULE, ACTION_CANCEL)
RELEASE_OFFER_ACTIONS: tuple[str, ...] = (*OFFER_ACTIONS, ACTION_RECEIVED, ACTION_NOT_RECEIVED)
LABEL_KEEP = "Manter horário"
LABEL_RESCHEDULE = "Remarcar"
LABEL_CANCEL = "Cancelar"
LABEL_RECEIVED = "Recebido"
LABEL_NOT_RECEIVED = "Não recebido"

DEPOSIT_OUTCOME_OFFERED = "choice_offered"
REASON_CARRIED_OVER = "carried_over"

KEEP_REQUESTED_NOW = "requested"
KEEP_ALREADY_REQUESTED = "already_requested"
KEEP_AFTER_START = "past"
KEEP_AFTER_REFUND = "refunded"
KEEP_DECLINED_BEFORE = "declined"
KEEP_CLOSED = "closed"
CANCEL_NOTIFIED = "notified"
CANCEL_AFTER_REFUND = "after_refund"
CANCEL_CLOSED = "closed"
RESCHEDULE_CARRYING = "carrying"
RESCHEDULE_NEW_CHARGE = "new_charge"
RESCHEDULE_NO_ANSWER = "no_answer"
RESCHEDULE_CLOSED = "closed"

# --- patient copy (pt-BR; the CLINIC returns the money, never "we" - D15; no bank named) -------

OFFER_HEAD = (
    "Devido à sua não confirmação da ida à consulta, a clínica liberou o seu horário para que "
    "você decida o que fazer."
)
OFFER_HEAD_CONFIRMED = "A clínica liberou o seu horário para que você decida o que fazer."
CLINIC_NOTE_PREFIX = "Recado da clínica: "
OFFER_DETAIL = (
    "Você já pagou o sinal desta consulta ({amount}). Toque em *Manter horário* para pedir à "
    "clínica o mesmo horário, em *Remarcar* para escolher outro dia e horário usando o mesmo "
    "sinal, sem nova cobrança, ou em *Cancelar* para cancelar a consulta e receber o sinal de "
    "volta. Se você não responder até {deadline}, a clínica será avisada para devolver o seu sinal."
)
OFFER_DETAIL_NO_KEEP = (
    "Você já pagou o sinal desta consulta ({amount}). Toque em *Remarcar* para escolher outro dia "
    "e horário usando o mesmo sinal, sem nova cobrança, ou em *Cancelar* para cancelar a consulta "
    "e receber o sinal de volta. Se você não responder até {deadline}, a clínica será avisada para "
    "devolver o seu sinal."
)
KEEP_REQUESTED_TEXT = (
    "Avisamos a clínica que você quer manter o horário de {when}. A clínica vai verificar se o "
    "horário ainda está livre e decide em até 24 horas; a resposta chega por aqui."
)
KEEP_ALREADY_REQUESTED_TEXT = (
    "A clínica já foi avisada de que você quer manter o horário e vai responder por aqui."
)
AFTER_START_TEXT = (
    "O horário da consulta já passou. Você pode remarcar para outro dia e horário ou cancelar a "
    "consulta."
)
KEEP_AFTER_REFUND_TEXT = (
    "A clínica já informou que devolveu o seu sinal. Você pode remarcar (com novo pagamento do "
    "sinal) ou cancelar."
)
KEEP_DECLINED_TAP_TEXT = (
    "A clínica não pôde manter este horário. Você pode remarcar para outro dia e horário ou "
    "cancelar a consulta."
)
KEEP_ACCEPTED_TEXT = (
    "A clínica confirmou: seu horário de {when} está mantido. O sinal que você já pagou continua "
    "valendo para esta consulta."
)
KEEP_DECLINED_TEXT = (
    "A clínica não conseguiu manter o horário de {when}. Você pode remarcar para outro dia e "
    "horário, usando o mesmo sinal, ou cancelar e receber o sinal de volta."
)
KEEP_UNAVAILABLE_TEXT = "Não consegui confirmar o horário agora. Tente de novo em instantes."
TAP_ALREADY_KEPT_TEXT = "Sua consulta já está mantida para {when}."
TAP_ALREADY_REBOOKED_TEXT = "O sinal que você já pagou está valendo para a sua consulta."
TAP_KEEP_AFTER_CANCEL_TEXT = (
    "Você já pediu o cancelamento desta consulta. Para marcar de novo, toque em Remarcar."
)
CANCEL_DONE_TEXT = (
    "Combinado, sua consulta foi cancelada. A clínica foi avisada e fará a devolução do seu sinal "
    "de {amount}."
)
CANCEL_AFTER_MARKED_TEXT = (
    "Sua consulta foi cancelada. A clínica já informou que devolveu o seu sinal."
)
TAP_REFUND_ALREADY_TEXT = "A devolução do seu sinal já foi pedida à clínica."
TAP_CANCEL_AFTER_KEEP_TEXT = (
    "O sinal que você já pagou está valendo para a sua consulta. Para cancelar essa consulta, use "
    "as opções da própria consulta."
)
OFFER_CLOSED_NO_ANSWER_TEXT = (
    "O prazo de 24 horas para responder terminou e a consulta foi cancelada por falta de "
    "resposta. A clínica foi avisada para devolver o seu sinal. Para marcar uma nova consulta, é "
    "só me escrever."
)
RESCHEDULE_PREFIX = "Ótimo! O sinal que você já pagou vale para a nova consulta."
RESCHEDULE_NEW_CHARGE_PREFIX = (
    "A clínica já informou que devolveu o seu sinal, então a nova consulta segue as regras de "
    "sinal da clínica."
)
CARRIED_OVER_NOTE = "O sinal que você já pagou vale para esta consulta. Não há nova cobrança."
CHECK_TEXT = (
    "A clínica informou que devolveu o seu sinal de {amount}. Confira a sua conta (a devolução "
    "vai para a conta de onde saiu o Pix) e nos conte:"
)
RECEIVED_TEXT = "Que bom! Obrigada por confirmar."
NOT_RECEIVED_TEXT = "Avisamos a clínica novamente. Ela vai conferir a devolução do seu sinal."
NOT_RECEIVED_RECENT_TEXT = "A clínica já foi avisada novamente há pouco."
CHECK_TOO_EARLY_TEXT = "A clínica ainda não informou a devolução do seu sinal."
CHECK_CLOSED_TEXT = "Esse pagamento já está encerrado."
NOT_FOUND_TEXT = "Não encontrei essa consulta."


def _buttons(released_appointment_id, *pairs: tuple[str, str]) -> list[tuple[str, str]]:
    return [(f"{action}|{released_appointment_id}", label) for action, label in pairs]


def offer_buttons(released_appointment_id, *, keep: bool = True) -> list[tuple[str, str]]:
    pairs = [(ACTION_RESCHEDULE, LABEL_RESCHEDULE), (ACTION_CANCEL, LABEL_CANCEL)]
    if keep:
        pairs.insert(0, (ACTION_KEEP, LABEL_KEEP))
    return _buttons(released_appointment_id, *pairs)


def keep_fallback_buttons(released_appointment_id) -> list[tuple[str, str]]:
    """Remarcar / Cancelar: after S, after a refusal, or when the refund was already marked."""
    return offer_buttons(released_appointment_id, keep=False)


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
    """"20:00 de 05/03" in the clinic's zone (a deadline)."""
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
    keep: bool = True,
    limit: int = MAX_INTERACTIVE_BODY_CHARS,
) -> str:
    """The release notice of a PAID slot. Only the clinic's note is cut to fit."""
    head = OFFER_HEAD_CONFIRMED if confirmed else OFFER_HEAD
    detail = (OFFER_DETAIL if keep else OFFER_DETAIL_NO_KEEP).format(
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
    """What the CLINIC reads before releasing a paid slot (409 `retention_ack_required`)."""
    return (
        f"O paciente já pagou o sinal ({format_brl(deposit.amount_cents)}). Ao liberar o horário, "
        "ele poderá pedir para manter o horário (a clínica decide), remarcar usando o mesmo sinal "
        "ou cancelar. Se ele cancelar ou não responder até o prazo, a clínica receberá avisos "
        "para devolver o sinal por inteiro pelo painel do provedor de pagamento."
    )


def console_conversation_link(conversation_id: UUID | None) -> str | None:
    """The clinic console's thread (Brain-Message-Frontend `conversationHref`), or None."""
    base = (get_settings().BRAIN_MESSAGE_PORTAL_URL or "").strip().rstrip("/")
    if not base or conversation_id is None:
        return None
    return f"{base}/chat/?conversationId={conversation_id}"


def keep_allowed(offer: DepositReleaseOffer, now: datetime | None = None) -> bool:
    """Whether "Manter horário" may still be offered/asked: before S, refund not marked."""
    now = _now(now)
    return (
        offer.deposit_paid
        and offer.decision in KEEP_FROM
        and offer.refund_status in REFUND_CARRYABLE
        and (offer.slot_start_at is None or _as_utc(offer.slot_start_at) > now)
    )


def patient_decision(offer: DepositReleaseOffer) -> str:
    """The patient's side, as the clinic list (TASK-054) shows it."""
    if offer.decision == DECISION_NOT_OFFERED:
        return "not_offered"
    if offer.refund_status == REFUND_RECEIVED:
        return "received"
    if offer.refund_status == REFUND_NOT_RECEIVED:
        return "not_received"
    return {
        DECISION_KEEP_REQUESTED: "keep_requested",
        DECISION_KEPT: "kept",
        DECISION_KEEP_DECLINED: "keep_declined",
        DECISION_REBOOKED: "rescheduled",
        DECISION_CANCELLED: "cancelled",
        DECISION_NO_ANSWER: "cancelled_no_answer",
    }.get(offer.decision, "no_answer")


# --- opening -----------------------------------------------------------------------------------


async def _closing_hours(
    session: AsyncSession, tenant: Tenant, appointment: Appointment, has_secretary: bool | None
):
    """The clinic's hours when it has a secretary; otherwise the consultation's doctor's."""
    if has_secretary or appointment.professional_id is None:
        return tenant.business_hours
    professional = await session.get(Professional, appointment.professional_id)
    if professional is None or professional.tenant_id != tenant.id:
        return tenant.business_hours
    return professional_business_hours(professional, tenant)


async def open_offer(
    session: AsyncSession,
    *,
    tenant: Tenant,
    appointment: Appointment,
    deposit: PixDeposit,
    now: datetime | None = None,
    paid_notice_allowed: bool = False,
    has_secretary: bool | None = None,
) -> DepositReleaseOffer | None:
    """Record the release of an appointment with a deposit of THIS clinic; idempotent.

    PAID: an open offer with T, S, B and E. Anything else: `not_offered` (list only). None when the
    deposit or the appointment is not this clinic's. `has_secretary` comes from brain-api
    (services/brain_staff.py); None = unknown = the doctor's closing time. Does not commit.
    """
    if (
        deposit.tenant_id != tenant.id
        or appointment.tenant_id != tenant.id
        or deposit.appointment_id != appointment.id
    ):
        return None
    now = _now(now)
    paid = deposit.status is PixDepositStatus.PAID
    patient = (
        await session.get(Patient, appointment.patient_id) if appointment.patient_id else None
    )
    if patient is not None and patient.tenant_id != tenant.id:
        patient = None
    conversation_id = await conversation_id_for(session, tenant.id, appointment, patient)
    decide_by = (
        release_deadlines.decide_by(
            released_at=now,
            business_hours=await _closing_hours(session, tenant, appointment, has_secretary),
            timezone=tenant.timezone,
        )
        if paid
        else now
    )
    await insert_ignore(
        session,
        DepositReleaseOffer,
        {
            "tenant_id": tenant.id,
            "deposit_id": deposit.id,
            "released_appointment_id": appointment.id,
            "patient_id": appointment.patient_id,
            "conversation_id": conversation_id,
            "deposit_paid": paid,
            "decision": DECISION_NONE if paid else DECISION_NOT_OFFERED,
            "refund_status": REFUND_NOT_DUE,
            "released_at": now,
            "slot_start_at": appointment.start_at,
            "decide_by_at": decide_by,
            "expires_at": release_deadlines.expires_at(now) if paid else now,
            "paid_notice_allowed": bool(paid_notice_allowed),
        },
        index_elements=["released_appointment_id"],
    )
    offer = await find_offer(session, tenant_id=tenant.id, released_appointment_id=appointment.id)
    if offer is not None:
        logger.info(
            "deposit_release_offer_opened", tenant_id=str(tenant.id), offer_id=str(offer.id), paid=paid
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


async def record_contact(session: AsyncSession, *, offer_id: UUID, now: datetime) -> None:
    """The patient tapped a button of the offer (Manter, Remarcar or Cancelar): "contato"."""
    await session.execute(
        update(DepositReleaseOffer)
        .where(DepositReleaseOffer.id == offer_id, DepositReleaseOffer.first_contact_at.is_(None))
        .values(first_contact_at=now)
        .execution_options(synchronize_session=False)
    )


# --- scheduling and the refund item ------------------------------------------------------------


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


async def skip_kinds(
    session: AsyncSession, offer_id: UUID, kinds: tuple[str, ...] | None, now: datetime
) -> None:
    """Every unclaimed send of the offer (of `kinds`, or all when None) will never go out."""
    query = update(DepositReleaseEmail).where(
        DepositReleaseEmail.offer_id == offer_id, DepositReleaseEmail.claimed_at.is_(None)
    )
    if kinds is not None:
        query = query.where(DepositReleaseEmail.kind.in_(kinds))
    await session.execute(
        query.values(claimed_at=now, outcome=OUTCOME_SKIPPED).execution_options(
            synchronize_session=False
        )
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


async def _close_refund_item(
    session: AsyncSession, offer: DepositReleaseOffer, now: datetime, note: str
) -> None:
    """The money is in use again (kept / carried over): the open refund item is closed as
    "rebooked" and the deposit is no longer flagged. Never touches the deposit's status."""
    await session.execute(
        update(PaymentAttentionItem)
        .where(
            PaymentAttentionItem.tenant_id == offer.tenant_id,
            PaymentAttentionItem.deposit_id == offer.deposit_id,
            PaymentAttentionItem.reason == REASON_RELEASED_REFUND_DUE,
            PaymentAttentionItem.status == ATTENTION_OPEN,
        )
        .values(
            status=ATTENTION_RESOLVED,
            resolved_at=now,
            resolution_action="rebooked",
            resolution_note=note,
            resolved_by_name=SYSTEM_NAME,
        )
        .execution_options(synchronize_session=False)
    )
    still_open = await session.scalar(
        select(func.count())
        .select_from(PaymentAttentionItem)
        .where(
            PaymentAttentionItem.tenant_id == offer.tenant_id,
            PaymentAttentionItem.deposit_id == offer.deposit_id,
            PaymentAttentionItem.status == ATTENTION_OPEN,
        )
    )
    if not still_open:
        await session.execute(
            update(PixDeposit)
            .where(
                PixDeposit.id == offer.deposit_id,
                PixDeposit.fulfillment_status == FULFILLMENT_NEEDS_ATTENTION,
            )
            .values(fulfillment_status=FULFILLMENT_RESOLVED)
            .execution_options(synchronize_session=False)
        )


def _reminder_at(offer: DepositReleaseOffer, now: datetime) -> datetime:
    """E, or 24 h from now when E already passed (a cancel after a refused "Manter")."""
    expires = _as_utc(offer.expires_at)
    return expires if expires > now else now + REMINDER_INTERVAL


async def _make_due(
    session: AsyncSession, offer: DepositReleaseOffer, *, now: datetime, source: str, extra=()
) -> bool:
    """`not_due` -> `due` (guarded); opens the item; B's notice for silence; the reminder."""
    moved = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.refund_status == REFUND_NOT_DUE,
            *extra,
        )
        .values(refund_status=REFUND_DUE, refund_source=source, refund_due_at=now)
        .execution_options(synchronize_session=False)
    )
    if moved.rowcount != 1:
        return False
    await _open_refund_item(session, offer)
    if source == REFUND_SOURCE_TIMEOUT:
        await _schedule(session, offer, EMAIL_REFUND_NOANSWER, offer.decide_by_at)
        await _schedule(session, offer, EMAIL_REFUND_DAY, offer.expires_at)
    elif source == REFUND_SOURCE_PATIENT:
        await _schedule(session, offer, EMAIL_REFUND_DAY, _reminder_at(offer, now))
    logger.info(
        "deposit_release_offer_refund_due",
        tenant_id=str(offer.tenant_id),
        offer_id=str(offer.id),
        source=source,
    )
    return True


async def _reload(session: AsyncSession, offer_id: UUID) -> DepositReleaseOffer:
    return await session.get(DepositReleaseOffer, offer_id, populate_existing=True)


# --- Manter: a request to the clinic -----------------------------------------------------------


async def request_keep(
    session: AsyncSession, *, offer: DepositReleaseOffer, now: datetime | None = None
) -> str:
    """"Manter horário" (T..S, also after B): ask the clinic; 24 h for it to decide. Does not
    commit. Outcomes: KEEP_REQUESTED_NOW / KEEP_ALREADY_REQUESTED / KEEP_AFTER_START /
    KEEP_AFTER_REFUND / KEEP_DECLINED_BEFORE / KEEP_CLOSED."""
    now = _now(now)
    asked = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.deposit_paid.is_(True),
            DepositReleaseOffer.decision.in_(KEEP_FROM),
            DepositReleaseOffer.refund_status.in_(REFUND_CARRYABLE),
            or_(
                DepositReleaseOffer.slot_start_at.is_(None),
                DepositReleaseOffer.slot_start_at > now,
            ),
        )
        .values(
            decision=DECISION_KEEP_REQUESTED,
            keep_requested_at=now,
            keep_deadline_at=now + KEEP_WINDOW,
            patient_answered_at=now,
            first_contact_at=func.coalesce(DepositReleaseOffer.first_contact_at, now),
        )
        .execution_options(synchronize_session=False)
    )
    if asked.rowcount == 1:
        await _schedule(session, offer, EMAIL_KEEP_REQUEST, now)
        logger.info("deposit_release_offer_keep_requested", offer_id=str(offer.id))
        return KEEP_REQUESTED_NOW
    current = await _reload(session, offer.id)
    if current.decision == DECISION_KEEP_REQUESTED:
        return KEEP_ALREADY_REQUESTED
    if current.decision == DECISION_KEEP_DECLINED:
        return KEEP_DECLINED_BEFORE
    if current.decision in KEEP_FROM and current.refund_status not in REFUND_CARRYABLE:
        return KEEP_AFTER_REFUND
    if current.decision in KEEP_FROM:
        return KEEP_AFTER_START
    return KEEP_CLOSED


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


async def _undo(session: AsyncSession, offer_id: UUID, decision: str, refund_status: str) -> None:
    await session.execute(
        update(DepositReleaseOffer)
        .where(DepositReleaseOffer.id == offer_id)
        .values(
            decision=decision,
            refund_status=refund_status,
            closed_at=None,
            new_appointment_id=None,
            keep_outcome=None,
            keep_resolved_at=None,
            keep_resolved_by=None,
        )
        .execution_options(synchronize_session=False)
    )


async def claim_keep(
    session: AsyncSession,
    *,
    offer: DepositReleaseOffer,
    new_appointment: Appointment,
    resolved_by: str | None,
    now: datetime | None = None,
) -> bool:
    """The clinic ACCEPTED "Manter" (inside the transaction that inserted `new_appointment`):
    `kept`, the deposit moves, a due refund is undone (item closed, notices dropped), the patient
    is told. Does not commit."""
    if new_appointment.tenant_id != offer.tenant_id:
        return False
    now = _now(now)
    previous_refund = offer.refund_status
    claimed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.decision == DECISION_KEEP_REQUESTED,
            DepositReleaseOffer.refund_status.in_(REFUND_CARRYABLE),
            or_(
                DepositReleaseOffer.slot_start_at.is_(None),
                DepositReleaseOffer.slot_start_at > now,
            ),
        )
        .values(
            decision=DECISION_KEPT,
            keep_outcome=KEEP_OUTCOME_ACCEPTED,
            keep_resolved_at=now,
            keep_resolved_by=resolved_by,
            new_appointment_id=new_appointment.id,
            closed_at=now,
            refund_status=REFUND_NOT_DUE,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return False
    if not await _move_deposit(session, offer, new_appointment.id):
        await _undo(session, offer.id, DECISION_KEEP_REQUESTED, previous_refund)
        logger.warning("deposit_release_offer_keep_lost_deposit", offer_id=str(offer.id))
        return False
    await _close_refund_item(session, offer, now, KEPT_RESOLUTION_NOTE)
    await skip_kinds(session, offer.id, REFUND_NOTICE_KINDS, now)
    await _schedule(session, offer, EMAIL_PATIENT_KEEP_ACCEPTED, now)
    logger.info("deposit_release_offer_kept", offer_id=str(offer.id))
    return True


async def decline_keep(
    session: AsyncSession,
    *,
    offer: DepositReleaseOffer,
    outcome: str,
    resolved_by: str | None = None,
    now: datetime | None = None,
) -> bool:
    """The clinic REFUSED "Manter" (`outcome="refused"`) or K passed (`"timeout"`): the refund
    falls due, the clinic gets the refund steps now (+ a reminder in 24 h), the patient gets
    Remarcar / Cancelar. True when THIS call did it. Does not commit."""
    now = _now(now)
    timed_out = outcome == KEEP_OUTCOME_TIMEOUT
    guard = [
        DepositReleaseOffer.id == offer.id,
        DepositReleaseOffer.tenant_id == offer.tenant_id,
        DepositReleaseOffer.decision == DECISION_KEEP_REQUESTED,
    ]
    if timed_out:
        guard.append(DepositReleaseOffer.keep_deadline_at <= now)
    declined = await session.execute(
        update(DepositReleaseOffer)
        .where(*guard)
        .values(
            decision=DECISION_KEEP_DECLINED,
            keep_outcome=KEEP_OUTCOME_TIMEOUT if timed_out else KEEP_OUTCOME_REFUSED,
            keep_resolved_at=now,
            keep_resolved_by=resolved_by,
        )
        .execution_options(synchronize_session=False)
    )
    if declined.rowcount != 1:
        return False
    source = REFUND_SOURCE_KEEP_TIMEOUT if timed_out else REFUND_SOURCE_KEEP_REFUSED
    await _make_due(session, offer, now=now, source=source)
    current = await _reload(session, offer.id)
    if current.refund_status == REFUND_DUE:
        await _schedule(session, current, EMAIL_KEEP_DECLINED, now)
        await _schedule(session, current, EMAIL_REFUND_DAY, now + REMINDER_INTERVAL, seq=1)
    await _schedule(session, current, EMAIL_PATIENT_KEEP_DECLINED, now)
    logger.info("deposit_release_offer_keep_declined", offer_id=str(offer.id), outcome=outcome)
    return True


# --- Cancelar / Remarcar -----------------------------------------------------------------------


async def choose_cancel(
    session: AsyncSession, *, offer: DepositReleaseOffer, now: datetime | None = None
) -> str:
    """"Cancelar": the refund falls due and the clinic is told RIGHT AWAY - unless it already said
    it refunded. Does not commit."""
    now = _now(now)
    claimed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.deposit_paid.is_(True),
            DepositReleaseOffer.decision.in_(CANCEL_FROM),
        )
        .values(
            decision=DECISION_CANCELLED,
            patient_answered_at=now,
            closed_at=now,
            first_contact_at=func.coalesce(DepositReleaseOffer.first_contact_at, now),
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return CANCEL_CLOSED
    await _make_due(session, offer, now=now, source=REFUND_SOURCE_PATIENT)
    current = await _reload(session, offer.id)
    if current.refund_status == REFUND_DUE:
        await _schedule(session, current, EMAIL_REFUND_CANCEL, now)
        await _schedule(session, current, EMAIL_REFUND_DAY, _reminder_at(current, now))
        return CANCEL_NOTIFIED
    return CANCEL_AFTER_REFUND


async def choose_reschedule(
    session: AsyncSession,
    *,
    offer: DepositReleaseOffer,
    conversation_id: UUID,
    now: datetime | None = None,
) -> str:
    """"Remarcar" at any time while the offer is open. CARRYING (the next booking carries the
    deposit), NEW_CHARGE (the clinic already said it refunded), NO_ANSWER (closed at E, never
    reopened) or CLOSED (kept / rebooked / not offered). Does not commit."""
    now = _now(now)
    current = await _reload(session, offer.id)
    if current is None or current.tenant_id != offer.tenant_id or not current.deposit_paid:
        return RESCHEDULE_CLOSED
    if current.decision == DECISION_NO_ANSWER:
        return RESCHEDULE_NO_ANSWER
    if current.decision not in RESCHEDULE_FROM:
        return RESCHEDULE_CLOSED
    if current.refund_status not in REFUND_CARRYABLE:
        return RESCHEDULE_NEW_CHARGE
    await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.decision.in_(RESCHEDULE_FROM),
            DepositReleaseOffer.refund_status.in_(REFUND_CARRYABLE),
        )
        .values(
            decision=DECISION_RESCHEDULING,
            conversation_id=conversation_id,
            first_contact_at=func.coalesce(DepositReleaseOffer.first_contact_at, now),
        )
        .execution_options(synchronize_session=False)
    )
    after = await _reload(session, offer.id)
    if after.decision == DECISION_RESCHEDULING and after.refund_status in REFUND_CARRYABLE:
        return RESCHEDULE_CARRYING
    return RESCHEDULE_NEW_CHARGE if after.decision in RESCHEDULE_FROM else RESCHEDULE_CLOSED


def _carrying(tenant_id: UUID, conversation_id: UUID) -> tuple:
    return (
        DepositReleaseOffer.tenant_id == tenant_id,
        DepositReleaseOffer.conversation_id == conversation_id,
        DepositReleaseOffer.deposit_paid.is_(True),
        DepositReleaseOffer.decision == DECISION_RESCHEDULING,
        DepositReleaseOffer.refund_status.in_(REFUND_CARRYABLE),
    )


async def carry_over_pending(
    session: AsyncSession, *, tenant_id: UUID, conversation_id: UUID, now: datetime | None = None
) -> bool:
    """Whether this conversation's next booking carries a paid deposit (no new charge)."""
    found = await session.scalar(
        select(DepositReleaseOffer.id).where(*_carrying(tenant_id, conversation_id))
    )
    return found is not None


async def claim_carry_over(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    new_appointment: Appointment,
    now: datetime | None = None,
) -> PixDeposit | None:
    """Inside the booking's transaction: move the paid deposit to `new_appointment`, close the
    offer as `rebooked`, undo a due refund (item closed, notices dropped) and tell the clinic
    "REMARCADA levando o sinal". None when nothing is carried. Does not commit."""
    if new_appointment.tenant_id != tenant_id:
        return None
    now = _now(now)
    offer = await session.scalar(
        select(DepositReleaseOffer)
        .where(*_carrying(tenant_id, conversation_id))
        .order_by(DepositReleaseOffer.released_at.desc())
        .limit(1)
    )
    if offer is None:
        return None
    previous_refund = offer.refund_status
    claimed = await session.execute(
        update(DepositReleaseOffer)
        .where(DepositReleaseOffer.id == offer.id, *_carrying(tenant_id, conversation_id))
        .values(
            decision=DECISION_REBOOKED,
            closed_at=now,
            patient_answered_at=now,
            new_appointment_id=new_appointment.id,
            refund_status=REFUND_NOT_DUE,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return None
    if not await _move_deposit(session, offer, new_appointment.id):
        await _undo(session, offer.id, DECISION_RESCHEDULING, previous_refund)
        logger.warning("deposit_release_offer_carry_lost_deposit", offer_id=str(offer.id))
        return None
    await _close_refund_item(session, offer, now, CARRIED_RESOLUTION_NOTE)
    await skip_kinds(session, offer.id, REFUND_NOTICE_KINDS, now)
    await _schedule(session, offer, EMAIL_REBOOKED_DEPOSIT, now)
    logger.info("deposit_release_offer_rebooked", tenant_id=str(tenant_id), offer_id=str(offer.id))
    return await session.get(PixDeposit, offer.deposit_id, populate_existing=True)


# --- B, E, K -----------------------------------------------------------------------------------


async def settle_if_expired(
    session: AsyncSession, *, offer: DepositReleaseOffer, now: datetime | None = None
) -> bool:
    """B passed with no answer (or an unfinished "Remarcar"): refund due (`timeout`)."""
    now = _now(now)
    return await _make_due(
        session,
        offer,
        now=now,
        source=REFUND_SOURCE_TIMEOUT,
        extra=(
            DepositReleaseOffer.decision.in_(SILENT_DECISIONS),
            DepositReleaseOffer.deposit_paid.is_(True),
            DepositReleaseOffer.decide_by_at <= now,
        ),
    )


async def close_if_no_answer(
    session: AsyncSession, *, offer: DepositReleaseOffer, now: datetime | None = None
) -> bool:
    """E passed without a final choice: "Cancelado por falta de resposta" (terminal); the refund
    is due as on "Cancelar" (settled here too if B was never swept). Does not commit."""
    now = _now(now)
    closed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.decision.in_(SILENT_DECISIONS),
            DepositReleaseOffer.deposit_paid.is_(True),
            DepositReleaseOffer.expires_at <= now,
        )
        .values(decision=DECISION_NO_ANSWER, closed_at=now)
        .execution_options(synchronize_session=False)
    )
    if closed.rowcount != 1:
        return False
    await _make_due(session, offer, now=now, source=REFUND_SOURCE_TIMEOUT)
    logger.info("deposit_release_offer_no_answer", offer_id=str(offer.id))
    return True


async def expire_offers(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 50
) -> int:
    """The sweep: B, E and K. One commit per offer; one failure never stops the batch. Returns
    how many transitions THIS tick made. Commits."""
    now = _now(now)
    paid = DepositReleaseOffer.deposit_paid.is_(True)
    silent = DepositReleaseOffer.decision.in_(SILENT_DECISIONS)
    queries = (
        select(DepositReleaseOffer.id).where(
            paid,
            silent,
            DepositReleaseOffer.refund_status == REFUND_NOT_DUE,
            DepositReleaseOffer.decide_by_at <= now,
        ),
        select(DepositReleaseOffer.id).where(paid, silent, DepositReleaseOffer.expires_at <= now),
        select(DepositReleaseOffer.id).where(
            paid,
            DepositReleaseOffer.decision == DECISION_KEEP_REQUESTED,
            DepositReleaseOffer.keep_deadline_at <= now,
        ),
    )
    ids: list[UUID] = []
    for query in queries:
        ids.extend((await session.scalars(query.limit(limit))).all())
    changed = 0
    for offer_id in dict.fromkeys(ids):
        try:
            offer = await session.get(DepositReleaseOffer, offer_id)
            if offer is not None:
                changed += int(await settle_if_expired(session, offer=offer, now=now))
                changed += int(await close_if_no_answer(session, offer=offer, now=now))
                changed += int(
                    await decline_keep(session, offer=offer, outcome=KEEP_OUTCOME_TIMEOUT, now=now)
                )
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - one offer never stops the batch
            await session.rollback()
            logger.warning(
                "deposit_release_offer_expire_failed",
                offer_id=str(offer_id),
                error_type=type(exc).__name__,
            )
    return changed


# --- devolvido / Recebido / Não recebido -------------------------------------------------------


async def record_received(
    session: AsyncSession, *, offer: DepositReleaseOffer, now: datetime | None = None
) -> str:
    """"Recebido": `closed`, `already_closed`, `too_early` (not marked yet) or `not_refund`."""
    now = _now(now)
    closed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.refund_status.in_(REFUND_ANSWERABLE),
        )
        .values(refund_status=REFUND_RECEIVED, patient_answered_at=now)
        .execution_options(synchronize_session=False)
    )
    if closed.rowcount == 1:
        await skip_kinds(session, offer.id, None, now)
        return "closed"
    current = await _reload(session, offer.id)
    if current is not None and current.refund_status in (REFUND_RECEIVED, REFUND_CLINIC_DECIDED):
        return "already_closed"
    if current is not None and current.refund_status == REFUND_DUE:
        return "too_early"
    return "not_refund"


async def request_reminder(
    session: AsyncSession, *, offer: DepositReleaseOffer, now: datetime | None = None
) -> str:
    """"Não recebido": `sent`, `recent` (< 24 h), `too_early`, `closed` or `not_refund`."""
    now = _now(now)
    claimed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.refund_status.in_(REFUND_ANSWERABLE),
            or_(
                DepositReleaseOffer.last_reminder_at.is_(None),
                DepositReleaseOffer.last_reminder_at <= now - REMINDER_INTERVAL,
            ),
        )
        .values(refund_status=REFUND_NOT_RECEIVED, last_reminder_at=now, patient_answered_at=now)
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
        return "sent"
    current = await _reload(session, offer.id)
    if current is None:
        return "not_refund"
    if current.refund_status in (REFUND_RECEIVED, REFUND_CLINIC_DECIDED):
        return "closed"
    if current.refund_status == REFUND_DUE:
        return "too_early"
    if current.refund_status in REFUND_ANSWERABLE:
        return "recent"
    return "not_refund"


async def on_refund_recorded(
    session: AsyncSession, *, tenant_id: UUID, item: PaymentAttentionItem, now: datetime | None
) -> UUID | None:
    """Called by `attention_view.resolve_item` for every resolution. For THIS clinic's
    `released_refund_due` item: "refunded in the panel" -> `marked` + the patient's check now (an
    open "Manter" request ends as refused); any other decision -> `clinic_decided`, nothing pending
    goes out. Does not commit."""
    if item.reason != REASON_RELEASED_REFUND_DUE or item.deposit_id is None:
        return None
    now = _now(now)
    offer = await session.scalar(
        select(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.tenant_id == tenant_id,
            DepositReleaseOffer.deposit_id == item.deposit_id,
            DepositReleaseOffer.refund_status == REFUND_DUE,
        )
        .order_by(DepositReleaseOffer.released_at.desc())
        .limit(1)
    )
    if offer is None:
        return None
    refunded = item.resolution_action == REFUNDED_ACTION
    moved = await session.execute(
        update(DepositReleaseOffer)
        .where(DepositReleaseOffer.id == offer.id, DepositReleaseOffer.refund_status == REFUND_DUE)
        .values(
            refund_status=REFUND_MARKED if refunded else REFUND_CLINIC_DECIDED,
            refund_marked_at=now if refunded else None,
        )
        .execution_options(synchronize_session=False)
    )
    if moved.rowcount != 1:
        return None
    await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.decision == DECISION_KEEP_REQUESTED,
        )
        .values(
            decision=DECISION_KEEP_DECLINED,
            keep_outcome=KEEP_OUTCOME_REFUSED,
            keep_resolved_at=now,
            keep_resolved_by=item.resolved_by_name,
        )
        .execution_options(synchronize_session=False)
    )
    if refunded:
        await _schedule(session, offer, EMAIL_PATIENT_CHECK, now)
    else:
        await skip_kinds(session, offer.id, None, now)
    logger.info("deposit_release_offer_refund_recorded", offer_id=str(offer.id), refunded=refunded)
    return offer.id


async def mark_refunded(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    offer_id: UUID,
    note: str,
    resolved_by_name: str,
    now: datetime | None = None,
) -> str:
    """The clinic says it refunded in the Asaas panel (TASK-054 list). Idempotent.

    `marked` / `already` (marked or answered before: nothing re-sent) / `not_due` / `keep_pending`
    (decide "Manter" first) / `not_found` (no such offer in THIS clinic). Goes through
    `attention_view.resolve_item`, so the deposit is recorded as refunded exactly as in the
    payments screen. Commits.
    """
    from secretaria.services.payments import attention_view  # lazy: it calls back into here

    now = _now(now)
    offer = await session.scalar(
        select(DepositReleaseOffer).where(
            DepositReleaseOffer.id == offer_id, DepositReleaseOffer.tenant_id == tenant_id
        )
    )
    if offer is None:
        return "not_found"
    if offer.refund_status in (REFUND_MARKED, REFUND_RECEIVED, REFUND_NOT_RECEIVED):
        return "already"
    if offer.decision == DECISION_KEEP_REQUESTED:
        return "keep_pending"
    if offer.refund_status != REFUND_DUE:
        return "not_due"
    item_id = await session.scalar(
        select(PaymentAttentionItem.id)
        .where(
            PaymentAttentionItem.tenant_id == tenant_id,
            PaymentAttentionItem.deposit_id == offer.deposit_id,
            PaymentAttentionItem.reason == REASON_RELEASED_REFUND_DUE,
            PaymentAttentionItem.status == ATTENTION_OPEN,
        )
        .limit(1)
    )
    if item_id is not None:
        await attention_view.resolve_item(
            session,
            tenant_id,
            item_id,
            action=REFUNDED_ACTION,
            note=note,
            resolved_by_name=resolved_by_name,
            now=now,
        )
    current = await _reload(session, offer_id)
    if current.refund_status == REFUND_DUE:
        # No open item (resolved elsewhere a moment ago, or never opened): record it here.
        moved = await session.execute(
            update(DepositReleaseOffer)
            .where(DepositReleaseOffer.id == offer_id, DepositReleaseOffer.refund_status == REFUND_DUE)
            .values(refund_status=REFUND_MARKED, refund_marked_at=now)
            .execution_options(synchronize_session=False)
        )
        if moved.rowcount == 1:
            await _schedule(session, current, EMAIL_PATIENT_CHECK, now)
        await session.commit()
        current = await _reload(session, offer_id)
    if current.refund_status == REFUND_MARKED:
        return "marked"
    if current.refund_status in (REFUND_RECEIVED, REFUND_NOT_RECEIVED):
        return "already"
    return "not_due"
```

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_offer.py tests/test_payment_attention.py tests/test_payment_attention_hub.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/services/release_offer.py src/secretaria/models/payment_attention_item.py src/secretaria/services/payments/attention_view.py tests/release_offer_support.py tests/test_release_offer.py tests/test_payment_attention.py`
Expected: PASS (`test_release_offer.py`: 27), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/services/release_offer.py src/secretaria/models/payment_attention_item.py src/secretaria/services/payments/attention_view.py tests/release_offer_support.py tests/test_release_offer.py tests/test_payment_attention.py
git commit -m "feat(payments): release offer service - keep request to the clinic, reschedule any time, cancel, B/24h/K deadlines, refunded marking (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 4: A clínica aceita "Manter" — verificar o horário e recriar a consulta (`services/release_keep.py`)

**Files:**
- Create: `src/secretaria/services/release_keep.py`
- Test: `tests/test_release_keep.py` (novo)

**Interfaces:**
- Consumes: Task 3 (`request_keep`, `claim_keep`, `choose_cancel`, `choose_reschedule`); `booking_promotion.calendar_for_booking(session, tenant, professional_id)`; `CalendarService.check_availability(start, end)`, `.create_event(start, end, summary, description="", reminders=None, *, event_id=None)`, `.cancel_event(event_id)`; `booking_hold.live_hold_overlaps(session, tenant_id, start_at, end_at, *, professional_id="any", exclude_charge_id=None)`; `LIVE_APPOINTMENT_STATUSES`.
- Produces: `ACCEPT_KEPT="kept"`, `ACCEPT_ALREADY="already"`, `ACCEPT_TAKEN="slot_taken"`, `ACCEPT_PAST="slot_past"`, `ACCEPT_UNAVAILABLE="calendar_unavailable"`, `ACCEPT_NOT_REQUESTED="not_requested"`, `ACCEPT_REFUNDED="refund_already_marked"`; `@dataclass(frozen=True) KeepResult(outcome: str, appointment: Appointment | None = None)`; `keep_event_id(offer_id) -> str`; `async accept_keep(session, *, tenant, offer, resolved_by, now=None) -> KeepResult` — comita quando aceita; rollback e apaga o evento quando perde a corrida. Os códigos são os 409/503 do contrato da aba (Task 11).

- [ ] **Step 1: Escrever os testes que falham**

`tests/test_release_keep.py`:

```python
"""services/release_keep.py - the CLINIC accepts "Manter horário" (TASK-052, owner 2026-10-12).

Only on an open request (`keep_requested`), before S, with the refund not marked. TAKEN when Google
has someone else's event there, another conversation holds it, or the clinic booked another live
consultation of the same professional over it. Accepted: a NEW confirmed consultation with a
deterministic event id, the PAID deposit re-pointed, the patient told - in one transaction.
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
from secretaria.services import booking_promotion, release_keep as rk, release_offer as ro
from tests.payments_support import make_sessionmaker
from tests.release_offer_support import RELEASED_START, T, emails, make_released, utc

TAP = T + timedelta(hours=1)
NOW = T + timedelta(hours=2)


class _Calendar:
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


async def _requested(db, *, ask=True, **kw):
    async with db() as session:
        released = await make_released(session, **kw)
        offer = await ro.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=T,
        )
        if ask:
            await ro.request_keep(session, offer=offer, now=TAP)
        await session.commit()
    return released, offer


async def _accept(db, released, offer_id, now=NOW):
    async with db() as session:
        offer = await session.get(DepositReleaseOffer, offer_id)
        tenant = await session.get(Tenant, released.tenant_id)
        return await rk.accept_keep(session, tenant=tenant, offer=offer, resolved_by="Ana", now=now)


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


async def test_accepting_a_free_slot_recreates_the_consultation_with_the_same_deposit(db, calendar):
    released, offer = await _requested(db)

    result = await _accept(db, released, offer.id)

    assert result.outcome == rk.ACCEPT_KEPT
    kept, live, deposit, kinds = await _state(db, offer.id)
    (again,) = live
    assert again.id == result.appointment.id and utc(again.start_at) == RELEASED_START
    assert again.patient_id == released.patient.id and again.confirmation_count == 1
    assert again.google_event_id == rk.keep_event_id(offer.id)
    assert (kept.decision, kept.keep_resolved_by) == ("kept", "Ana")
    assert deposit.appointment_id == again.id
    assert kinds == ["clinic_keep_request", "patient_keep_accepted"]


async def test_accepting_twice_is_the_same_answer(db, calendar):
    released, offer = await _requested(db)
    first = await _accept(db, released, offer.id)

    second = await _accept(db, released, offer.id, now=NOW + timedelta(minutes=5))

    assert second.outcome == rk.ACCEPT_ALREADY and second.appointment.id == first.appointment.id
    assert len(calendar.created) == 1


async def test_a_retry_finding_its_own_event_is_not_a_conflict(db, calendar):
    released, offer = await _requested(db)
    calendar.events = [{"id": rk.keep_event_id(offer.id)}]

    assert (await _accept(db, released, offer.id)).outcome == rk.ACCEPT_KEPT


async def test_an_event_in_google_means_taken_and_nothing_changes(db, calendar):
    released, offer = await _requested(db)
    calendar.events = [{"id": "someone-else"}]

    assert (await _accept(db, released, offer.id)).outcome == rk.ACCEPT_TAKEN

    still, live, deposit, _kinds = await _state(db, offer.id)
    assert still.decision == "keep_requested" and live == [] and calendar.created == []
    assert deposit.appointment_id == released.appointment.id


async def test_another_conversations_hold_means_taken(db, calendar):
    released, offer = await _requested(db)
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

    assert (await _accept(db, released, offer.id)).outcome == rk.ACCEPT_TAKEN


async def test_a_consultation_the_clinic_booked_there_means_taken(db, calendar):
    """The clinic re-occupied the freed slot with another patient."""
    released, offer = await _requested(db)
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

    assert (await _accept(db, released, offer.id)).outcome == rk.ACCEPT_TAKEN


async def test_accepting_after_the_start_is_past(db, calendar):
    released, offer = await _requested(db, start=T + timedelta(hours=1, minutes=30))

    assert (await _accept(db, released, offer.id)).outcome == rk.ACCEPT_PAST
    assert calendar.created == []


async def test_accepting_without_an_open_request_is_refused(db, calendar):
    released, never = await _requested(db, ask=False)
    assert (await _accept(db, released, never.id)).outcome == rk.ACCEPT_NOT_REQUESTED

    other, moved_on = await _requested(db)
    async with db() as session:
        row = await session.get(DepositReleaseOffer, moved_on.id)
        await ro.choose_reschedule(
            session, offer=row, conversation_id=other.conversation.id, now=TAP + timedelta(minutes=5)
        )
        await session.commit()
    assert (await _accept(db, other, moved_on.id)).outcome == rk.ACCEPT_NOT_REQUESTED
    assert calendar.created == []


async def test_google_down_changes_nothing(db, calendar):
    released, offer = await _requested(db)
    calendar.fail = True

    assert (await _accept(db, released, offer.id)).outcome == rk.ACCEPT_UNAVAILABLE
    still, live, _deposit, _kinds = await _state(db, offer.id)
    assert still.decision == "keep_requested" and live == []


async def test_losing_the_claim_deletes_the_event_it_created(db, calendar):
    released, offer = await _requested(db)
    async with db() as session:
        stale = await session.get(DepositReleaseOffer, offer.id)  # read while still requested
    async with db() as session:  # meanwhile the patient cancels
        row = await session.get(DepositReleaseOffer, offer.id)
        await ro.choose_cancel(session, offer=row, now=NOW)
        await session.commit()

    async with db() as session:
        tenant = await session.get(Tenant, released.tenant_id)
        result = await rk.accept_keep(session, tenant=tenant, offer=stale, resolved_by="Ana", now=NOW)

    assert result.outcome == rk.ACCEPT_NOT_REQUESTED
    assert calendar.cancelled == [rk.keep_event_id(offer.id)]
    still, live, deposit, _kinds = await _state(db, offer.id)
    assert still.decision == "cancelled" and live == []
    assert deposit.appointment_id == released.appointment.id
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_keep.py -q -p no:cacheprovider`
Expected: FAIL (`ImportError: cannot import name 'release_keep'`).

- [ ] **Step 3: Implementar**

`src/secretaria/services/release_keep.py`:

```python
"""release_keep - the CLINIC accepts "Manter horário" on a released PAID slot (TASK-052).

Owner decision 2026-10-12 (3rd round): the patient's "Manter" only ASKS; the clinic decides in
the TASK-054 list. Accepting (only on an open `keep_requested`, before S, refund not marked)
re-creates: the released row is CANCELLED and its Google event deleted, and Google never lets a
deleted event id be used again (services/calendar.py::CalendarService.create_event), so a NEW
consultation on the same slot gets its own deterministic event id (`keep_event_id`) and the PAID
deposit is re-pointed (`release_offer.claim_keep`) in ONE transaction with the offer's UPDATE.

TAKEN (nothing changes; the clinic may refuse or try later) when, on the OWNING calendar, Google
has someone else's event over the slot, another conversation holds it, or the clinic has another
live consultation of the same professional (or of the clinic, without a professional) over it.
`CalendarService.is_slot_free` is NOT used: it hard-codes an 8-18 window.

Order: checks -> create the event -> INSERT + claim. Losing the claim rolls back and deletes the
event. The re-created row counts as confirmed (`confirmation_count = 1`). Never logs PII.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, AppointmentStatus, Patient, Tenant
from secretaria.models.appointment import LIVE_APPOINTMENT_STATUSES
from secretaria.models.deposit_release_offer import (
    DECISION_KEEP_REQUESTED,
    DECISION_KEPT,
    REFUND_CARRYABLE,
    DepositReleaseOffer,
)
from secretaria.services import booking_hold, booking_promotion, release_offer

logger = get_logger(__name__)

ACCEPT_KEPT = "kept"
ACCEPT_ALREADY = "already"
ACCEPT_TAKEN = "slot_taken"
ACCEPT_PAST = "slot_past"
ACCEPT_UNAVAILABLE = "calendar_unavailable"
ACCEPT_NOT_REQUESTED = "not_requested"
ACCEPT_REFUNDED = "refund_already_marked"


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


async def _kept(session: AsyncSession, offer_id: UUID) -> KeepResult:
    offer = await session.get(DepositReleaseOffer, offer_id, populate_existing=True)
    if offer is not None and offer.decision == DECISION_KEPT and offer.new_appointment_id:
        return KeepResult(ACCEPT_ALREADY, await session.get(Appointment, offer.new_appointment_id))
    return KeepResult(ACCEPT_NOT_REQUESTED)


async def accept_keep(
    session: AsyncSession,
    *,
    tenant: Tenant,
    offer: DepositReleaseOffer,
    resolved_by: str | None,
    now: datetime | None = None,
) -> KeepResult:
    now = now or datetime.now(UTC)
    offer_id = offer.id
    if offer.tenant_id != tenant.id:
        return KeepResult(ACCEPT_NOT_REQUESTED)
    if offer.decision != DECISION_KEEP_REQUESTED:
        return await _kept(session, offer_id)
    if offer.refund_status not in REFUND_CARRYABLE:
        return KeepResult(ACCEPT_REFUNDED)
    released = await session.get(Appointment, offer.released_appointment_id)
    if released is None or released.tenant_id != tenant.id or released.start_at is None:
        return KeepResult(ACCEPT_NOT_REQUESTED)
    start, end = _utc(released.start_at), _utc(released.end_at or released.start_at)
    if start <= now:
        return KeepResult(ACCEPT_PAST)

    calendar = await booking_promotion.calendar_for_booking(
        session, tenant, released.professional_id
    )
    if calendar is None:
        return KeepResult(ACCEPT_UNAVAILABLE)
    own_event = keep_event_id(offer_id)
    try:
        events = await calendar.check_availability(start, end)
    except Exception as exc:  # noqa: BLE001 - Google down: nothing changes, the clinic retries
        logger.warning(
            "release_keep_calendar_failed", offer_id=str(offer_id), error_type=type(exc).__name__
        )
        return KeepResult(ACCEPT_UNAVAILABLE)
    if any(str(event.get("id")) != own_event for event in events):
        return KeepResult(ACCEPT_TAKEN)
    if await booking_hold.live_hold_overlaps(
        session, tenant.id, start, end, professional_id=released.professional_id
    ):
        return KeepResult(ACCEPT_TAKEN)
    if await _consultation_overlaps(session, released, start, end):
        return KeepResult(ACCEPT_TAKEN)

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
        return KeepResult(ACCEPT_UNAVAILABLE)

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
        session, offer=offer, new_appointment=appointment, resolved_by=resolved_by, now=now
    ):
        await session.rollback()
        try:
            await calendar.cancel_event(own_event)
        except Exception as exc:  # noqa: BLE001 - logged; the event is ours and empty
            logger.error(
                "release_keep_orphan_event", offer_id=str(offer_id), error_type=type(exc).__name__
            )
        return await _kept(session, offer_id)
    await session.commit()
    logger.info("release_keep_accepted", offer_id=str(offer_id), appointment_id=str(appointment.id))
    return KeepResult(ACCEPT_KEPT, appointment)
```

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_keep.py tests/test_release_offer.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/services/release_keep.py tests/test_release_keep.py`
Expected: PASS (`test_release_keep.py`: 10), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/services/release_keep.py tests/test_release_keep.py
git commit -m "feat(calendar): the clinic accepts a keep request - re-create the released consultation when the slot is free (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 5: Os envios programados — avisos à clínica, mensagens ao paciente e modelos Meta (`services/release_offer_dispatch.py`)

**Files:**
- Create: `src/secretaria/services/release_offer_dispatch.py`
- Modify: `src/secretaria/services/email.py` (textos + envios novos), `src/secretaria/config.py` (dois nomes de modelo, PENDÊNCIA META)
- Test: `tests/test_release_offer_dispatch.py` (novo)

**Interfaces:**
- Consumes: Tasks 1 e 3; `email._send_sync(to, subject, body)`; `cancellation_notice.{last_inbound_at, is_inside_window, meta_language_code}`; `BrainMessageSender.send_buttons/send_text_message`; `WhatsAppClient.for_tenant(...).send_buttons/send_text_message/send_template`; `reminder_text.portal_conversation_link(tenant_id)`; `emit_usage_event(...)`.
- Produces:
  - `config.Settings.RELEASE_OFFER_TEMPLATE_NAME = "liberacao_sinal_pago_v1"` (3 botões; também para as mensagens de Manter fora da janela), `REFUND_CHECK_TEMPLATE_NAME = "devolucao_sinal_conferir_v1"`.
  - `services/email.py`: `asaas_refund_steps(*, amount_text, paid_text)`; `release_refund_clinic_email(*, clinic_name, kind, no_answer_closed, amount_text, paid_text, slot_text, deadline_text, conversation_link)`; `release_keep_request_clinic_email(*, clinic_name, slot_text, decide_by_text, conversation_link)`; `release_rebooked_clinic_email(*, clinic_name, amount_text, old_slot_text, new_slot_text, conversation_link)`; `refund_check_patient_email(*, clinic_name, amount_text, conversation_link)` (cada um `-> (subject, body)`); envios `send_release_refund_clinic_email`, `send_release_keep_request_clinic_email`, `send_release_rebooked_clinic_email`, `send_refund_check_patient_email` (`-> bool`).
  - `services/release_offer_dispatch.py`: `still_relevant(kind, offer) -> bool` (puro, spec §5.8/§5.9); `MAX_ATTEMPTS = 5`; `DispatchReport(sent, skipped, no_contact, retried, given_up, lost)`; `async dispatch_due(session, *, now=None, offer_id=None, limit=50) -> DispatchReport` (comita por linha). Paciente de WhatsApp NUNCA recebe e-mail.

- [ ] **Step 1: Escrever os testes que falham**

`tests/test_release_offer_dispatch.py`:

```python
"""services/release_offer_dispatch.py - what a released PAID slot scheduled, sent once (TASK-052).

Each row is claimed (and committed) BEFORE its send, re-checked at send time, retried when nothing
left and given up after 5 tries. Clinic notices carry the amount, the slot, the console link (and
the Asaas steps when a refund is due) - never the patient's name, phone or e-mail. A WhatsApp
patient never gets an e-mail; a Portal patient gets the card and, for the refund check, an e-mail.
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.config import get_settings
from secretaria.models import Message, MessageDirection, MessageSender, Patient, PaymentAttentionItem
from secretaria.models.deposit_release_offer import DepositReleaseOffer
from secretaria.services import email, release_offer as ro, release_offer_dispatch
from secretaria.services.payments import attention_view
from tests._reminders_v2 import FakeWhatsAppClient, fake_waba_token
from tests.payments_support import make_sessionmaker
from tests.release_offer_support import RELEASED_START, T, emails, make_released, new_appointment

B = T + timedelta(hours=6)
E = T + timedelta(hours=24)
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


async def _opened(db, *, channel="whatsapp", patient_email=None, paid=True, **kw):
    async with db() as session:
        released = await make_released(session, channel=channel, **kw)
        if patient_email:
            patient = await session.get(Patient, released.patient.id)
            patient.email = patient_email
        offer = await ro.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=T,
            paid_notice_allowed=paid,
        )
        await session.commit()
    return released, offer


async def _call(db, fn, offer_id, at, **kw):
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer_id)
        result = await fn(session, offer=row, now=at, **kw)
        await session.commit()
    return result


async def _mark(db, released, offer_id, at=T + timedelta(hours=3)):
    async with db() as session:
        await ro.mark_refunded(
            session,
            tenant_id=released.tenant_id,
            offer_id=offer_id,
            note="Estornado no painel do Asaas",
            resolved_by_name="Ana",
            now=at,
        )


async def _dispatch(db, now, **kw):
    async with db() as session:
        return await release_offer_dispatch.dispatch_due(session, now=now, **kw)


async def _sweep(db, at):
    async with db() as session:
        await ro.expire_offers(session, now=at)


async def _outcomes(db, offer_id) -> dict:
    async with db() as session:
        return {(e.kind, e.seq): e.outcome for e in await emails(session, offer_id)}


def _clinic(mailbox) -> list[dict]:
    return [m for m in mailbox if m["to"] == CLINIC]


async def _inbound(db, released):
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


async def test_cancel_tells_the_clinic_now_with_the_asaas_steps_and_no_patient_data(db, mailbox):
    released, offer = await _opened(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))

    assert (await _dispatch(db, T + timedelta(hours=1))).sent == 1

    (mail,) = mailbox
    assert mail["to"] == CLINIC and "Devolver sinal" in mail["subject"]
    body = mail["body"]
    assert "R$ 60,00" in body and "08/03 às 10:00" in body and "paga em 01/03" in body
    assert f"https://portal.exemplo/chat/?conversationId={released.conversation.id}" in body
    for step in ("Cobranças", "estornar o valor total", "Não é preciso digitar chave Pix",
                 "saldo insuficiente", "comprovante"):  # fmt: skip
        assert step in body
    assert "Maria" not in body and released.patient.wa_id not in body


async def test_silence_tells_the_clinic_at_b_and_says_overdue_at_e(db, mailbox):
    _released, offer = await _opened(db)
    await _sweep(db, B)
    await _dispatch(db, B)
    assert "não respondeu até 20:00 de 05/03" in mailbox[-1]["body"]

    await _sweep(db, E)
    await _dispatch(db, E)

    assert "prazo vencido" in mailbox[-1]["subject"].lower()
    assert "Devolva agora" in mailbox[-1]["body"]


async def test_manter_tells_the_clinic_with_the_link_and_pauses_the_refund_notices(db, mailbox):
    released, offer = await _opened(db)
    await _sweep(db, B)  # refund due; B's notice and E's reminder scheduled
    await _call(db, ro.request_keep, offer.id, B - timedelta(minutes=30))  # tapped just before

    await _dispatch(db, E + timedelta(minutes=1))

    (mail,) = _clinic(mailbox)
    assert "manter o horário" in mail["subject"]
    assert "08/03 às 10:00" in mail["body"] and "conversationId=" in mail["body"]
    outcomes = await _outcomes(db, offer.id)
    assert outcomes[("clinic_refund_noanswer", 0)] == "skipped"
    assert outcomes[("clinic_refund_day", 0)] == "skipped"


async def test_a_refusal_sends_new_refund_steps_and_the_patient_gets_remarcar_or_cancelar(
    db, mailbox
):
    released, offer = await _opened(db)
    await _inbound(db, released)
    await _call(db, ro.request_keep, offer.id, T + timedelta(hours=1))
    await _call(db, ro.decline_keep, offer.id, T + timedelta(hours=2), outcome="refused")

    await _dispatch(db, T + timedelta(hours=2))

    # The request notice is dropped (the clinic already decided); the refusal carries the steps.
    (declined,) = _clinic(mailbox)
    assert "não foi mantido" in declined["subject"] and "estornar o valor total" in declined["body"]
    (card,) = FakeWhatsAppClient.all_sent()
    assert card[0] == "buttons" and card[2].startswith("A clínica não conseguiu manter")
    assert [label for _id, label in card[3]] == ["Remarcar", "Cancelar"]


async def test_an_accepted_keep_tells_the_patient_on_whatsapp_never_by_email(db, mailbox):
    released, offer = await _opened(db, patient_email=PATIENT)
    await _inbound(db, released)
    await _call(db, ro.request_keep, offer.id, T + timedelta(hours=1))
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        again = await new_appointment(session, released, start=RELEASED_START)
        await ro.claim_keep(
            session, offer=row, new_appointment=again, resolved_by="Ana", now=T + timedelta(hours=2)
        )
        await session.commit()

    await _dispatch(db, T + timedelta(hours=2))

    texts = [s for s in FakeWhatsAppClient.all_sent() if s[0] == "text"]
    assert texts and texts[0][2].startswith("A clínica confirmou: seu horário de 08/03 às 10:00")
    assert [m for m in mailbox if m["to"] == PATIENT] == []


async def test_a_confirmed_remarcar_sends_the_rebooked_email_and_cancels_refund_notices(db, mailbox):
    released, offer = await _opened(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))  # cancel notice due now
    await _call(db, ro.choose_reschedule, offer.id, T + timedelta(hours=2),
                conversation_id=released.conversation.id)  # fmt: skip
    async with db() as session:
        fresh = await new_appointment(session, released, start=RELEASED_START + timedelta(days=4))
        await ro.claim_carry_over(
            session,
            tenant_id=released.tenant_id,
            conversation_id=released.conversation.id,
            new_appointment=fresh,
            now=T + timedelta(hours=3),
        )
        await session.commit()

    await _dispatch(db, E + timedelta(hours=1))

    (mail,) = _clinic(mailbox)
    assert "REMARCADA levando o sinal" in mail["subject"]
    assert "não há reembolso a fazer" in mail["body"]
    assert "08/03 às 10:00" in mail["body"] and "12/03 às 10:00" in mail["body"]
    outcomes = await _outcomes(db, offer.id)
    assert outcomes[("clinic_refund_cancel", 0)] == "skipped"
    assert outcomes[("clinic_refund_day", 0)] == "skipped"


async def test_the_day_reminder_skips_only_when_marked_and_received(db, mailbox):
    released, offer = await _opened(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    await _dispatch(db, T + timedelta(hours=1))  # the cancel notice
    await _mark(db, released, offer.id)
    await _call(db, ro.record_received, offer.id, T + timedelta(hours=5))

    await _dispatch(db, E)

    assert len(_clinic(mailbox)) == 1
    assert (await _outcomes(db, offer.id))[("clinic_refund_day", 0)] == "skipped"


async def test_the_day_reminder_still_goes_when_marked_but_unanswered(db, mailbox):
    released, offer = await _opened(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    await _mark(db, released, offer.id)

    await _dispatch(db, E)

    assert (await _outcomes(db, offer.id))[("clinic_refund_day", 0)] == "sent"


async def test_the_day_reminder_still_goes_after_not_received(db, mailbox):
    released, offer = await _opened(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    await _mark(db, released, offer.id)
    await _call(db, ro.request_reminder, offer.id, T + timedelta(hours=5))

    await _dispatch(db, E)

    outcomes = await _outcomes(db, offer.id)
    assert outcomes[("clinic_refund_reminder", 1)] == "sent"
    assert outcomes[("clinic_refund_day", 0)] == "sent"


async def test_a_clinic_decision_other_than_refund_stops_every_notice(db, mailbox):
    released, offer = await _opened(db)
    await _sweep(db, B)
    async with db() as session:
        item = await session.scalar(select(PaymentAttentionItem))
    async with db() as session:
        await attention_view.resolve_item(
            session,
            released.tenant_id,
            item.id,
            action="kept_as_credit",
            note="Crédito para a próxima consulta",
            resolved_by_name="Ana",
        )

    assert (await _dispatch(db, E)).sent == 0 and mailbox == []


async def test_each_scheduled_email_goes_out_once_even_dispatched_twice(db, mailbox):
    _released, offer = await _opened(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))

    await _dispatch(db, T + timedelta(hours=1))
    await _dispatch(db, T + timedelta(hours=1, minutes=5))
    assert len(mailbox) == 1

    async with db() as session:  # another worker already claimed the E reminder
        for row in await emails(session, offer.id):
            if row.kind == "clinic_refund_day":
                row.claimed_at = E
                row.outcome = "sending"
        await session.commit()
    await _dispatch(db, E)
    assert len(mailbox) == 1


async def test_a_send_that_did_not_leave_is_retried_then_given_up(db, mailbox, monkeypatch):
    _released, offer = await _opened(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))

    def _refuse(to_email, subject, body):
        raise OSError("smtp down")

    monkeypatch.setattr(email, "_send_sync", _refuse)
    reports = [await _dispatch(db, T + timedelta(hours=1, minutes=m)) for m in range(5)]

    assert [r.retried for r in reports] == [1, 1, 1, 1, 0] and reports[-1].given_up == 1
    async with db() as session:
        (row,) = [e for e in await emails(session, offer.id) if e.kind == "clinic_refund_cancel"]
    assert (row.outcome, row.attempts) == ("given_up", 5)


async def test_a_clinic_without_contact_email_is_skipped_not_retried(db, mailbox):
    _released, offer = await _opened(db, contact_email=None)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))

    first = await _dispatch(db, T + timedelta(hours=1))
    again = await _dispatch(db, T + timedelta(hours=1, minutes=5))

    assert (first.no_contact, again.no_contact, mailbox) == (1, 0, [])


async def test_marking_refunded_asks_a_portal_patient_by_card_and_email_link(db, mailbox):
    released, offer = await _opened(db, channel="brain_message", patient_email=PATIENT)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    await _mark(db, released, offer.id)

    await _dispatch(db, T + timedelta(hours=3), offer_id=offer.id)

    (patient_mail,) = [m for m in mailbox if m["to"] == PATIENT]
    assert "R$ 60,00" in patient_mail["body"] and "conta de onde saiu o Pix" in patient_mail["body"]
    assert "banco" not in patient_mail["body"].lower()
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


async def test_outside_the_window_whatsapp_uses_the_pending_templates_or_nothing(db, mailbox):
    released, offer = await _opened(db, patient_email=PATIENT)  # paid permission given
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    await _mark(db, released, offer.id)
    await _dispatch(db, T + timedelta(hours=3), offer_id=offer.id)
    (template,) = FakeWhatsAppClient.all_sent()
    assert template[0] == "template" and template[2] == get_settings().REFUND_CHECK_TEMPLATE_NAME
    assert "\n" not in template[4][0]
    assert [m for m in mailbox if m["to"] == PATIENT] == []  # WhatsApp: never an e-mail

    FakeWhatsAppClient.reset()
    other, declined = await _opened(db)
    await _call(db, ro.request_keep, declined.id, T + timedelta(hours=1))
    await _call(db, ro.decline_keep, declined.id, T + timedelta(hours=2), outcome="refused")
    await _dispatch(db, T + timedelta(hours=2), offer_id=declined.id)
    (template,) = FakeWhatsAppClient.all_sent()
    assert template[2] == get_settings().RELEASE_OFFER_TEMPLATE_NAME  # the 3-button one, reused

    FakeWhatsAppClient.reset()
    _x, silent = await _opened(db, paid=False)
    await _call(db, ro.request_keep, silent.id, T + timedelta(hours=1))
    await _call(db, ro.decline_keep, silent.id, T + timedelta(hours=2), outcome="refused")
    report = await _dispatch(db, T + timedelta(hours=2), offer_id=silent.id)
    assert FakeWhatsAppClient.all_sent() == [] and report.no_contact == 1


def test_still_relevant_is_the_spec_table():
    offer = DepositReleaseOffer(deposit_paid=True, decision="keep_requested", refund_status="due")
    assert not release_offer_dispatch.still_relevant("clinic_refund_noanswer", offer)  # paused
    assert not release_offer_dispatch.still_relevant("clinic_refund_day", offer)
    assert release_offer_dispatch.still_relevant("clinic_keep_request", offer)
    offer.decision, offer.refund_status = "no_answer_cancelled", "marked"
    assert release_offer_dispatch.still_relevant("clinic_refund_day", offer)
    offer.refund_status = "received"
    assert not release_offer_dispatch.still_relevant("clinic_refund_day", offer)
    offer.decision, offer.refund_status = "rebooked", "not_due"
    assert release_offer_dispatch.still_relevant("clinic_rebooked_deposit", offer)
    assert not release_offer_dispatch.still_relevant("clinic_refund_cancel", offer)
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_offer_dispatch.py -q -p no:cacheprovider`
Expected: FAIL (`ImportError: cannot import name 'release_offer_dispatch'`).

- [ ] **Step 3: Implementar**

Em `src/secretaria/config.py`, logo depois de `REMINDER_V2_BATCH_SIZE: int = 200`:

```python
    # --- Released PAID slot (TASK-052) ---------------------------------- PENDÊNCIA META ---
    # Utility templates NOT YET APPROVED by Meta. Owner decision 2026-10-12: the code uses them as
    # if approved; once Meta approves them on each WABA, put the approved names here. Texts to
    # submit: docs/LEMBRETES_MODELOS_META.md, section "TASK-052". While unapproved, a send
    # outside the 24 h window fails and is logged; the clinic's notices still go.
    # 1 body parameter + 3 quick replies (Manter horário / Remarcar / Cancelar); reused for every
    # out-of-window message of the offer (owner: "reaproveite o modelo de 3 botões").
    RELEASE_OFFER_TEMPLATE_NAME: str = "liberacao_sinal_pago_v1"
    # 1 body parameter + 2 quick replies (Recebido / Não recebido).
    REFUND_CHECK_TEMPLATE_NAME: str = "devolucao_sinal_conferir_v1"
```

No FIM de `src/secretaria/services/email.py`:

```python
# --- TASK-052: a slot the clinic released with a PAID deposit ----------------------------------
# Builders are pure; senders are SMTP-only, never raise, and return True only when the server
# accepted the message (services/release_offer_dispatch.py gives a claim back otherwise). Bodies
# carry the amount, the slot and links - never the patient's name, phone or e-mail; logs carry
# only the event name. The platform never refunds (D15): the steps tell the CLINIC how to.


def asaas_refund_steps(*, amount_text: str, paid_text: str) -> str:
    paid = f", paga em {paid_text}" if paid_text else ""
    return (
        "Como devolver pelo Asaas:\n"
        f"1. No painel do Asaas, abra Cobranças e encontre a cobrança Pix deste sinal "
        f"({amount_text}{paid}).\n"
        "2. Abra a cobrança e escolha estornar o valor total. Não é preciso digitar chave Pix: o "
        "estorno devolve o dinheiro para a mesma conta de onde o paciente pagou.\n"
        "3. Atenção: o estorno usa o saldo da sua conta no Asaas. Logo depois do pagamento o "
        "valor pode ainda não estar disponível (saldo insuficiente); nesse caso, aguarde a "
        "liberação e tente de novo.\n"
        "4. Guarde o comprovante do estorno.\n"
        "5. Marque a devolução como feita na aba de consultas liberadas. O paciente será "
        "perguntado se recebeu."
    )


def _release_link_line(link: str | None) -> str:
    return f"\n\nConversa com o paciente: {link}" if link else ""


def release_refund_clinic_email(
    *,
    clinic_name: str,
    kind: str,
    no_answer_closed: bool,
    amount_text: str,
    paid_text: str,
    slot_text: str,
    deadline_text: str,
    conversation_link: str | None,
) -> tuple[str, str]:
    """(subject, body) of the clinic's refund notices."""
    pending = f"[SecretarIA] Devolver sinal de {amount_text} — {clinic_name}"
    if kind == "clinic_refund_cancel":
        subject = pending
        lead = (
            f"O paciente da consulta de {slot_text}, cujo horário a clínica liberou, cancelou a "
            f"consulta. O sinal de {amount_text} precisa ser devolvido."
        )
    elif kind == "clinic_refund_noanswer":
        subject = pending
        lead = (
            f"O paciente da consulta de {slot_text}, cujo horário a clínica liberou, não respondeu "
            f"até {deadline_text}. O sinal de {amount_text} precisa ser devolvido."
        )
    elif kind == "clinic_keep_declined":
        subject = f"[SecretarIA] Horário não foi mantido: devolver sinal de {amount_text} — {clinic_name}"
        lead = (
            f"O pedido do paciente para manter o horário de {slot_text} foi recusado ou ficou sem "
            f"decisão da clínica em 24 horas. O sinal de {amount_text} precisa ser devolvido, a "
            "menos que o paciente remarque."
        )
    elif kind == "clinic_refund_day" and no_answer_closed:
        subject = f"[SecretarIA] Prazo vencido: devolva o sinal de {amount_text} — {clinic_name}"
        lead = (
            f"Prazo vencido: 24 horas depois da liberação do horário da consulta de {slot_text}, "
            "o paciente não respondeu e a consulta foi cancelada por falta de resposta. "
            f"Devolva agora o sinal de {amount_text}."
        )
    elif kind == "clinic_refund_day":
        subject = f"[SecretarIA] Lembrete: devolução de sinal pendente — {clinic_name}"
        lead = (
            f"Lembrete da consulta de {slot_text}: confira se o sinal de {amount_text} foi "
            "devolvido e se a devolução foi marcada como feita."
        )
    else:  # clinic_refund_reminder
        subject = f"[SecretarIA] Paciente não recebeu a devolução do sinal — {clinic_name}"
        lead = (
            f"O paciente da consulta de {slot_text} informou que ainda não recebeu a devolução do "
            f"sinal de {amount_text}. Confira o estorno no painel do Asaas."
        )
    body = (
        f"Olá,\n\n{lead}\n\n{asaas_refund_steps(amount_text=amount_text, paid_text=paid_text)}"
        f"{_release_link_line(conversation_link)}\n\n— Equipe SecretarIA"
    )
    return subject, body


def release_keep_request_clinic_email(
    *, clinic_name: str, slot_text: str, decide_by_text: str, conversation_link: str | None
) -> tuple[str, str]:
    subject = f"[SecretarIA] Paciente quer manter o horário de {slot_text} — {clinic_name}"
    body = (
        f"Olá,\n\nO paciente da consulta de {slot_text}, cujo horário a clínica liberou, pediu "
        "para manter o mesmo horário. A decisão é da clínica: na aba de consultas liberadas, "
        "aceite (a consulta volta para a agenda com o sinal já pago, se o horário estiver livre) "
        f"ou recuse (o sinal deverá ser devolvido). Decida até {decide_by_text}; sem decisão, o "
        "pedido é tratado como recusado."
        f"{_release_link_line(conversation_link)}\n\n— Equipe SecretarIA"
    )
    return subject, body


def release_rebooked_clinic_email(
    *,
    clinic_name: str,
    amount_text: str,
    old_slot_text: str,
    new_slot_text: str,
    conversation_link: str | None,
) -> tuple[str, str]:
    subject = f"[SecretarIA] Nova consulta marcada — REMARCADA levando o sinal — {clinic_name}"
    body = (
        f"Olá,\n\nO paciente da consulta de {old_slot_text}, cujo horário a clínica liberou, "
        f"remarcou para {new_slot_text}. O sinal de {amount_text} já pago foi transferido para a "
        "nova consulta: não há reembolso a fazer. Se a clínica já tinha estornado esse sinal no "
        "Asaas e ainda não marcou como devolvido, confira com o paciente."
        f"{_release_link_line(conversation_link)}\n\n— Equipe SecretarIA"
    )
    return subject, body


def refund_check_patient_email(
    *, clinic_name: str, amount_text: str, conversation_link: str | None
) -> tuple[str, str]:
    """To a PORTAL patient only (a WhatsApp patient is asked on WhatsApp)."""
    subject = f"{clinic_name}: confira a devolução do seu sinal"
    where = (
        f"Abra a conversa e toque em Recebido ou Não recebido: {conversation_link}"
        if conversation_link
        else "Abra a conversa com a clínica e toque em Recebido ou Não recebido."
    )
    body = (
        f"Olá,\n\nA clínica {clinic_name} informou que devolveu o seu sinal de {amount_text}. "
        "Confira a sua conta: a devolução vai para a conta de onde saiu o Pix."
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


async def send_release_refund_clinic_email(to_email: str, **fields) -> bool:
    subject, body = release_refund_clinic_email(**fields)
    return await _send_release_mail(to_email, subject, body, "release_refund_clinic_email")


async def send_release_keep_request_clinic_email(to_email: str, **fields) -> bool:
    subject, body = release_keep_request_clinic_email(**fields)
    return await _send_release_mail(to_email, subject, body, "release_keep_request_email")


async def send_release_rebooked_clinic_email(to_email: str, **fields) -> bool:
    subject, body = release_rebooked_clinic_email(**fields)
    return await _send_release_mail(to_email, subject, body, "release_rebooked_clinic_email")


async def send_refund_check_patient_email(to_email: str, **fields) -> bool:
    subject, body = refund_check_patient_email(**fields)
    return await _send_release_mail(to_email, subject, body, "refund_check_patient_email")
```

`src/secretaria/services/release_offer_dispatch.py`:

```python
"""Sending what a released PAID slot scheduled (TASK-052, owner decisions 2026-10-12).

Every `deposit_release_emails` row is ONE send (spec §5.13): CLAIMED (`claimed_at IS NULL`) and
committed BEFORE the send; RE-CHECKED at send time by `still_relevant` (spec §5.8/§5.9 - refund
notices are paused while a "Manter" request is open and dropped once the money is in use again);
SENT (clinic notices to `Tenant.contact_email`; patient messages on the conversation's channel -
Portal card, plus an e-mail with the link only for the refund check; WhatsApp card inside the
24 h window, the PENDING-META template outside it with `paid_notice_allowed`, NEVER an e-mail);
"nothing left" gives the claim back, the 5th failure is `given_up`. Logs ids/kind/outcome only.
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
    DECISION_CANCELLED,
    DECISION_KEEP_DECLINED,
    DECISION_KEEP_REQUESTED,
    DECISION_KEPT,
    DECISION_NO_ANSWER,
    DECISION_REBOOKED,
    EMAIL_KEEP_DECLINED,
    EMAIL_KEEP_REQUEST,
    EMAIL_PATIENT_CHECK,
    EMAIL_PATIENT_KEEP_ACCEPTED,
    EMAIL_PATIENT_KEEP_DECLINED,
    EMAIL_REBOOKED_DEPOSIT,
    EMAIL_REFUND_CANCEL,
    EMAIL_REFUND_DAY,
    EMAIL_REFUND_NOANSWER,
    EMAIL_REFUND_REMINDER,
    OUTCOME_GIVEN_UP,
    OUTCOME_NO_CONTACT,
    OUTCOME_SENDING,
    OUTCOME_SENT,
    OUTCOME_SKIPPED,
    REFUND_DUE,
    REFUND_MARKED,
    REFUND_NOT_RECEIVED,
    SILENT_DECISIONS,
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
PATIENT_KINDS = (EMAIL_PATIENT_CHECK, EMAIL_PATIENT_KEEP_ACCEPTED, EMAIL_PATIENT_KEEP_DECLINED)


@dataclass
class DispatchReport:
    sent: int = 0
    skipped: int = 0
    no_contact: int = 0
    retried: int = 0
    given_up: int = 0
    lost: int = 0


def still_relevant(kind: str, offer: DepositReleaseOffer) -> bool:
    """The send-time rule of spec §5.8/§5.9 (pure)."""
    if not offer.deposit_paid:
        return False
    decision, refund = offer.decision, offer.refund_status
    rules = {
        EMAIL_KEEP_REQUEST: decision == DECISION_KEEP_REQUESTED,
        EMAIL_PATIENT_KEEP_ACCEPTED: decision == DECISION_KEPT,
        EMAIL_PATIENT_KEEP_DECLINED: decision == DECISION_KEEP_DECLINED,
        EMAIL_KEEP_DECLINED: decision == DECISION_KEEP_DECLINED and refund == REFUND_DUE,
        EMAIL_REBOOKED_DEPOSIT: decision == DECISION_REBOOKED,
        EMAIL_PATIENT_CHECK: refund == REFUND_MARKED,
        EMAIL_REFUND_CANCEL: decision == DECISION_CANCELLED and refund == REFUND_DUE,
        EMAIL_REFUND_NOANSWER: refund == REFUND_DUE
        and decision in (*SILENT_DECISIONS, DECISION_NO_ANSWER),
        EMAIL_REFUND_DAY: refund in (REFUND_DUE, REFUND_MARKED, REFUND_NOT_RECEIVED)
        and decision not in (DECISION_KEEP_REQUESTED, DECISION_KEPT, DECISION_REBOOKED),
        EMAIL_REFUND_REMINDER: refund == REFUND_NOT_RECEIVED,
    }
    return rules.get(kind, False)


async def dispatch_due(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    offer_id: UUID | None = None,
    limit: int = BATCH_SIZE,
) -> DispatchReport:
    """Send every unclaimed row due by `now` (only `offer_id`'s when given). Commits per row."""
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
    if offer is None or tenant is None or offer.tenant_id != tenant.id or not still_relevant(
        kind, offer
    ):
        await _finish(session, email_id, OUTCOME_SKIPPED)
        report.skipped += 1
        _log(email_id, offer_id, kind, OUTCOME_SKIPPED)
        return

    if kind in PATIENT_KINDS:
        delivered = await _send_patient(session, tenant, offer, kind)
    else:
        delivered = await _send_clinic(session, tenant, offer, kind)

    if delivered is None:
        outcome = OUTCOME_NO_CONTACT
        await _finish(session, email_id, outcome)
        report.no_contact += 1
    elif delivered:
        outcome = OUTCOME_SENT
        await _finish(session, email_id, outcome)
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
        "release_offer_send",
        email_id=str(email_id),
        offer_id=str(offer_id),
        kind=kind,
        outcome=outcome,
    )


async def _finish(session: AsyncSession, email_id: UUID, outcome: str) -> None:
    await session.execute(
        update(DepositReleaseEmail)
        .where(DepositReleaseEmail.id == email_id)
        .values(outcome=outcome)
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
    tz = tenant.timezone
    amount = format_brl(deposit.amount_cents) if deposit is not None else ""
    slot = (
        release_offer.slot_text(released.start_at, tz)
        if released is not None and released.start_at is not None
        else ""
    )
    link = release_offer.console_conversation_link(offer.conversation_id)
    if kind == EMAIL_KEEP_REQUEST:
        return await email.send_release_keep_request_clinic_email(
            to,
            clinic_name=tenant.clinic_name,
            slot_text=slot,
            decide_by_text=release_offer.when_text(offer.keep_deadline_at, tz),
            conversation_link=link,
        )
    if kind == EMAIL_REBOOKED_DEPOSIT:
        fresh = (
            await session.get(Appointment, offer.new_appointment_id)
            if offer.new_appointment_id
            else None
        )
        return await email.send_release_rebooked_clinic_email(
            to,
            clinic_name=tenant.clinic_name,
            amount_text=amount,
            old_slot_text=slot,
            new_slot_text=release_offer.slot_text(fresh.start_at, tz)
            if fresh is not None and fresh.start_at is not None
            else "",
            conversation_link=link,
        )
    paid = (
        deposit.paid_at.strftime("%d/%m")
        if deposit is not None and deposit.paid_at is not None
        else ""
    )
    return await email.send_release_refund_clinic_email(
        to,
        clinic_name=tenant.clinic_name,
        kind=kind,
        no_answer_closed=offer.decision == DECISION_NO_ANSWER,
        amount_text=amount,
        paid_text=paid,
        slot_text=slot,
        deadline_text=release_offer.when_text(offer.decide_by_at, tz),
        conversation_link=link,
    )


async def _patient_message(
    session: AsyncSession, tenant: Tenant, offer: DepositReleaseOffer, kind: str
) -> tuple[str, list[tuple[str, str]] | None, str, str]:
    """(text, buttons or None, out-of-window template name, amount) of a patient message."""
    released_id = offer.released_appointment_id
    deposit = await session.get(PixDeposit, offer.deposit_id)
    amount = format_brl(deposit.amount_cents) if deposit is not None else ""
    if kind == EMAIL_PATIENT_CHECK:
        return (
            release_offer.CHECK_TEXT.format(amount=amount),
            release_offer.check_buttons(released_id),
            get_settings().REFUND_CHECK_TEMPLATE_NAME,
            amount,
        )
    released = await session.get(Appointment, released_id)
    when = (
        release_offer.slot_text(released.start_at, tenant.timezone)
        if released is not None and released.start_at is not None
        else ""
    )
    template = get_settings().RELEASE_OFFER_TEMPLATE_NAME
    if kind == EMAIL_PATIENT_KEEP_ACCEPTED:
        return release_offer.KEEP_ACCEPTED_TEXT.format(when=when), None, template, amount
    return (
        release_offer.KEEP_DECLINED_TEXT.format(when=when),
        release_offer.keep_fallback_buttons(released_id),
        template,
        amount,
    )


async def _send_patient(
    session: AsyncSession, tenant: Tenant, offer: DepositReleaseOffer, kind: str
) -> bool | None:
    patient = await session.get(Patient, offer.patient_id) if offer.patient_id else None
    if patient is None or patient.tenant_id != tenant.id:
        return None
    text, buttons, template, amount = await _patient_message(session, tenant, offer, kind)
    # Everything read up front: the Portal branch commits, and nothing below may lazy-load.
    clinic_name, tenant_id, conversation_id = tenant.clinic_name, tenant.id, offer.conversation_id
    if patient.channel == CHANNEL_BRAIN_MESSAGE:
        patient_email = (patient.email or "").strip()
        external_id = patient.external_id or ""
        delivered = False
        if conversation_id is not None:
            delivered = await _on_portal(session, conversation_id, external_id, text, buttons)
        if kind == EMAIL_PATIENT_CHECK and patient_email:
            if await email.send_refund_check_patient_email(
                patient_email,
                clinic_name=clinic_name,
                amount_text=amount,
                conversation_link=portal_conversation_link(tenant_id),
            ):
                delivered = True
        if conversation_id is None and not (kind == EMAIL_PATIENT_CHECK and patient_email):
            return None
        return delivered
    if not patient.wa_id:
        return None
    # WhatsApp: a message only - never an e-mail (owner decision 2026-10-12).
    return await _on_whatsapp(session, tenant, offer, patient, text, buttons, template, kind)


async def _on_portal(
    session: AsyncSession, conversation_id: UUID, to: str, text: str, buttons
) -> bool:
    try:
        sender = BrainMessageSender(conversation_id=conversation_id, session=session)
        if buttons:
            await sender.send_buttons(to=to, body=text, buttons=buttons)
        else:
            await sender.send_text_message(to=to, body=text)
        await session.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        logger.warning("release_offer_portal_send_failed", error_type=type(exc).__name__)
        return False


async def _on_whatsapp(
    session: AsyncSession,
    tenant: Tenant,
    offer: DepositReleaseOffer,
    patient: Patient,
    text: str,
    buttons,
    template: str,
    kind: str,
) -> bool | None:
    """True sent, False failed (retry), None not allowed (outside the window, no permission)."""
    last = await cancellation_notice.last_inbound_at(session, tenant.id, patient.id)
    inside = cancellation_notice.is_inside_window(last)
    if not inside and not offer.paid_notice_allowed:
        return None
    token = await get_waba_token(session, tenant.id)
    offer_id = offer.id
    try:
        client = WhatsAppClient.for_tenant(tenant, token)
        if inside and buttons:
            await client.send_buttons(to=patient.wa_id, body=text, buttons=buttons)
        elif inside:
            await client.send_text_message(to=patient.wa_id, body=text)
        else:
            payload_buttons = (
                release_offer.check_buttons(offer.released_appointment_id)
                if kind == EMAIL_PATIENT_CHECK
                else release_offer.offer_buttons(offer.released_appointment_id)
            )
            await client.send_template(
                to=patient.wa_id,
                template=template,
                lang=cancellation_notice.meta_language_code(tenant.language),
                variables=[release_offer.template_text(text)],
                button_payloads=[button_id for button_id, _label in payload_buttons],
            )
    except Exception as exc:  # noqa: BLE001 - e.g. the template is not approved yet
        logger.warning(
            "release_offer_whatsapp_send_failed", offer_id=str(offer_id), error_type=type(exc).__name__
        )
        return False
    if not inside:
        try:
            await emit_usage_event(
                tenant_id=str(tenant.id),
                feature="reminders",
                amount=1,
                event_id=f"release:{kind}:{offer_id}",
            )
        except Exception as exc:  # noqa: BLE001 - fail-open, never retried
            logger.warning("usage_emit_failed", error_type=type(exc).__name__)
    return True
```

> Detalhe deliberado: fora da janela, as mensagens de Manter vão no modelo de 3 botões (o dono pediu para reaproveitá-lo); por isso os `button_payloads` são os 3 da oferta (`offer_buttons`) — tocar "Manter horário" de novo depois de uma recusa responde `KEEP_DECLINED_TAP_TEXT` com Remarcar / Cancelar (Task 9).

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_offer_dispatch.py tests/test_release_offer.py tests/test_email_transactional.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/services/release_offer_dispatch.py src/secretaria/services/email.py src/secretaria/config.py tests/test_release_offer_dispatch.py`
Expected: PASS (`test_release_offer_dispatch.py`: 16), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/services/release_offer_dispatch.py src/secretaria/services/email.py src/secretaria/config.py tests/test_release_offer_dispatch.py
git commit -m "feat(payments): released-slot notices - keep request, refund steps, rebooked-with-deposit, patient messages - sent once (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 6: Liberar abre a oferta (hub, com o fechamento lido do brain-api) e o Portal recebe o cartão

**Files:**
- Create: `src/secretaria/services/brain_staff.py`, `tests/test_brain_staff.py`
- Modify: `src/secretaria/api/hub/calendar.py` (`release_appointment`), `src/secretaria/services/appointment_release.py` (`notify_released_patient`, `_notify_portal`)
- Test: `tests/test_hub_release.py`, `tests/test_brain_staff.py`

**Interfaces:**
- Consumes: Task 3 (`open_offer(..., has_secretary=)`, `offer_body`, `offer_buttons`, `keep_allowed`, `warning_text`, `DEPOSIT_OUTCOME_OFFERED`); o padrão de cliente de `services/brain_professionals.py` (`BRAIN_API_BASE_URL`, `INTERNAL_API_KEY`, `X-Internal-Api-Key`, `BRAIN_API_TIMEOUT_SECONDS`).
- Produces (novo): `services/brain_staff.py::fetch_has_secretary(tenant_id) -> bool | None` — `GET {BRAIN_API_BASE_URL}/internal/tenants/{tenant_id}/staff-roles` → `{"roles": [str, ...]}`; `True`/`False` pela presença de `"secretary"`; `None` em qualquer ambiguidade (sem configuração, rede, ≠ 200 — inclusive 404 enquanto o brain-api não tiver o endpoint —, JSON ou formato inválidos). **Dependência externa:** o endpoint é do brain-api (o dono está construindo a adição de integrantes por papel); até existir, `None` = horário do médico.
- Produces: 409 `retention_ack_required` com `deposit_outcome = "choice_offered"`; 200 com `deposit_outcome = "choice_offered"` e UMA oferta aberta; sinal NÃO pago: o fluxo de hoje (`on_appointment_cancelled`) + registro `not_offered`; `notify_released_patient(..., offer_buttons: list[tuple[str, str]] | None = None)`; o enfileiramento do job NÃO muda (7 argumentos; o 6º é o corpo inteiro da oferta). Paciente de WhatsApp: nenhum e-mail.

- [ ] **Step 1: Escrever os testes**

Em `tests/test_hub_release.py`:

1. Acrescente aos imports:

```python
from secretaria.models import DepositReleaseOffer, PaymentAttentionItem
from secretaria.services import release_offer
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
    """TASK-052: a clinic release never retains a paid deposit, whatever the cancellation policy."""
    world = await _paid_world(db, acting, hours_ahead=hours_ahead, policy=policy)

    response = await _release(client, world.appointment.id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "retention_ack_required"
    assert detail["deposit_outcome"] == "choice_offered" and detail["amount_cents"] == 10000
    for words in ("100,00", "manter o horário", "remarcar", "avisos", "por inteiro"):
        assert words in detail["message"]
    assert "retid" not in detail["message"]
    assert (await _appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED
    assert _FakeCalendar.deleted == []
    assert await _offers(db) == []


async def test_with_the_acknowledgement_the_offer_opens_and_nothing_is_decided_yet(
    client: AsyncClient, db, acting
):
    world = await _paid_world(db, acting, hours_ahead=30)

    response = await _release(client, world.appointment.id, acknowledge_retention=True)

    assert response.status_code == 200
    assert response.json()["deposit_outcome"] == "choice_offered"
    assert await _deposit_status(db, world.appointment.id) == PixDepositStatus.PAID  # D15
    assert await _attention_items(db) == []
    [offer] = await _offers(db)
    assert (offer.decision, offer.refund_status, offer.deposit_paid) == ("none", "not_due", True)
    assert offer.released_appointment_id == world.appointment.id
    assert offer.tenant_id == world.tenant.id and offer.conversation_id == world.conversation.id
    released_at = utc(offer.released_at)
    # This clinic configured no business hours: B = T + 6 h (spec §5.2).
    assert utc(offer.decide_by_at) == released_at + timedelta(hours=6)
    assert utc(offer.expires_at) == released_at + timedelta(hours=24)
    assert utc(offer.slot_start_at) == utc(world.start_at)
    assert len(_FakeCalendar.deleted) == 1

    again = await _release(client, world.appointment.id, acknowledge_retention=True)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "not_live"
    assert len(await _offers(db)) == 1
```

3. No teste `test_an_unpaid_charge_needs_no_acknowledgement`, acrescente ao fim:

```python
    # TASK-052: the release is recorded for the clinic's list, without buttons or deadlines.
    [offer] = await _offers(db)
    assert (offer.deposit_paid, offer.decision) == (False, "not_offered")
```

4. Substitua o teste `test_the_deposit_sentence_rides_along_in_the_notice` (o que termina em `assert "retido" in pool.calls[0][5]`) por:

```python
async def test_the_offer_rides_in_the_same_job_arguments(client: AsyncClient, db, acting):
    pool = _FakeArqPool()
    _install_pool(pool)
    world = await _paid_world(db, acting, hours_ahead=30, last_inbound_at=_recent(1))

    await _release(
        client, world.appointment.id, acknowledge_retention=True, justification="Agenda cheia"
    )

    (call,) = pool.calls
    assert call[0] == "send_cancellation_notice" and len(call) == 7  # an older worker still works
    body = call[5]
    assert body.startswith(release_offer.OFFER_HEAD)
    assert "Recado da clínica: Agenda cheia" in body and "*Manter horário*" in body
    assert "retido" not in body


async def test_a_portal_patient_with_a_paid_deposit_gets_the_three_buttons_and_the_email_link(
    client: AsyncClient, db, acting, portal_mail
):
    world = await _paid_world(
        db, acting, hours_ahead=30, channel=CHANNEL_BRAIN_MESSAGE, email="paciente@x.com"
    )

    response = await _release(client, world.appointment.id, acknowledge_retention=True)

    assert response.json()["patient_notice"] == "portal_chat_email"
    assert portal_mail  # the existing "a clínica escreveu" e-mail, with the conversation link
    [row] = await outbound_messages(db, world.conversation.id)
    assert row.body.startswith(release_offer.OFFER_HEAD)
    assert appointment_release.PORTAL_REBOOK_LINE not in row.body
    assert [o["id"] for o in row.interactive["options"]] == [
        f"relkeep|{world.appointment.id}",
        f"relresched|{world.appointment.id}",
        f"relcancel|{world.appointment.id}",
    ]


async def test_a_released_slot_that_already_started_offers_no_manter(
    client: AsyncClient, db, acting, portal_mail
):
    world = await _paid_world(
        db, acting, hours_ahead=-1, channel=CHANNEL_BRAIN_MESSAGE, email="paciente@x.com"
    )

    await _release(client, world.appointment.id, acknowledge_retention=True)

    [row] = await outbound_messages(db, world.conversation.id)
    assert [o["title"] for o in row.interactive["options"]] == ["Remarcar", "Cancelar"]
    assert "Manter horário" not in row.body


async def test_a_whatsapp_patient_gets_no_email_at_the_release(
    client: AsyncClient, db, acting, monkeypatch
):
    from secretaria.config import get_settings
    from secretaria.services import email as email_module

    sent: list[tuple] = []
    monkeypatch.setattr(
        email_module, "_send_sync", lambda to, subject, body: sent.append((to, subject))
    )
    monkeypatch.setattr(get_settings(), "SMTP_HOST", "smtp.exemplo", raising=False)
    world = await _paid_world(
        db, acting, hours_ahead=30, last_inbound_at=_recent(30), email="maria@x.com"
    )

    response = await _release(client, world.appointment.id, acknowledge_retention=True)

    assert response.json()["patient_notice"] == "whatsapp_outside_window"
    assert sent == []  # owner decision 2026-10-12: WhatsApp gets messages, never e-mails
```

> `hours_ahead=-1` libera uma consulta que começou há 1 h; se `_setup` recusar consultas no passado, use a menor antecedência que ele aceita e confira que o texto continua sem "Manter horário" mudando `start_at` direto no banco antes de liberar.

`tests/test_brain_staff.py`:

```python
"""services/brain_staff.py - does the clinic have a secretary? (TASK-052, owner 2026-10-12).

brain-api owns identity and roles; secretarIA asks, per release, and never stores a copy. Any
ambiguity is `None` (the caller then uses the doctor's closing time).
"""

import httpx
import pytest

from secretaria.config import get_settings
from secretaria.services import brain_staff

TENANT = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "BRAIN_API_BASE_URL", "https://brain.exemplo", raising=False)
    monkeypatch.setattr(get_settings(), "INTERNAL_API_KEY", "k", raising=False)


def _answer(monkeypatch, handler):
    real = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(brain_staff.httpx, "AsyncClient", _client)


async def test_a_secretary_among_the_roles_is_true_and_none_is_false(monkeypatch, configured):
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("X-Internal-Api-Key")))
        return httpx.Response(200, json={"roles": ["manager", "doctor", "secretary"]})

    _answer(monkeypatch, handler)
    assert await brain_staff.fetch_has_secretary(TENANT) is True
    assert seen == [(f"/internal/tenants/{TENANT}/staff-roles", "k")]

    _answer(monkeypatch, lambda request: httpx.Response(200, json={"roles": ["doctor"]}))
    assert await brain_staff.fetch_has_secretary(TENANT) is False


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"detail": "Not Found"}),  # brain-api without the endpoint yet
        httpx.Response(500),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"items": []}),
    ],
)
async def test_every_ambiguity_is_unknown(monkeypatch, configured, response):
    _answer(monkeypatch, lambda request: response)
    assert await brain_staff.fetch_has_secretary(TENANT) is None


async def test_unconfigured_or_unreachable_is_unknown(monkeypatch):
    monkeypatch.setattr(get_settings(), "BRAIN_API_BASE_URL", "", raising=False)
    assert await brain_staff.fetch_has_secretary(TENANT) is None

    monkeypatch.setattr(get_settings(), "BRAIN_API_BASE_URL", "https://brain.exemplo", raising=False)
    monkeypatch.setattr(get_settings(), "INTERNAL_API_KEY", "k", raising=False)

    def boom(request):
        raise httpx.ConnectError("down")

    _answer(monkeypatch, boom)
    assert await brain_staff.fetch_has_secretary(TENANT) is None
```

Em `tests/test_hub_release.py`, acrescente também:

```python
async def test_the_b_deadline_uses_the_clinic_closing_when_brain_api_says_secretary(
    client: AsyncClient, db, acting, monkeypatch
):
    from secretaria.services import brain_staff

    asked: list = []

    async def _has_secretary(tenant_id):
        asked.append(tenant_id)
        return True

    monkeypatch.setattr(brain_staff, "fetch_has_secretary", _has_secretary)
    world = await _paid_world(db, acting, hours_ahead=30)

    await _release(client, world.appointment.id, acknowledge_retention=True)

    assert asked == [world.tenant.id]
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_hub_release.py -q -p no:cacheprovider`
Expected: FAIL nos casos novos (`deposit_outcome` ainda da política; nenhuma oferta; Portal recebe texto sem `interactive`).

- [ ] **Step 3: Implementar**

`src/secretaria/services/brain_staff.py`:

```python
"""secretarIA -> brain-api: does this clinic have a SECRETARY? (TASK-052, owner 2026-10-12).

brain-api is the single writer of identity and roles (`users.role`: manager | doctor | secretary,
brain-api models/user.py `ROLE_SECRETARY`). A role reaches secretarIA only in the hub token of the
person ACTING (core/hub_roles.py) - that says who clicks, not who the clinic has - so this module
ASKS, per release, and never stores a copy (same rule as services/brain_professionals.py).

Contract (brain-api, api/internal.py - being built by the owner together with "add members by
role" in the Contexto/professionals screen):
  - GET {BRAIN_API_BASE_URL}/internal/tenants/{tenant_id}/staff-roles
    -> 200 {"roles": ["manager", "doctor", "secretary", ...]}  (the distinct roles of the
       clinic's ACTIVE members)

Returns True/False, or None on ANY ambiguity (unconfigured, network, non-200 - including the 404
of a brain-api that does not have the endpoint yet -, bad JSON/shape). None means "unknown" and
the caller (services/release_offer.py) uses the DOCTOR's closing time. Never logs PII.
"""

from uuid import UUID

import httpx

from secretaria.config import get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)

ROLE_SECRETARY = "secretary"


async def fetch_has_secretary(tenant_id: UUID) -> bool | None:
    settings = get_settings()
    if not settings.BRAIN_API_BASE_URL or not settings.INTERNAL_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(
            base_url=settings.BRAIN_API_BASE_URL,
            headers={"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
            timeout=settings.BRAIN_API_TIMEOUT_SECONDS,
        ) as client:
            response = await client.get(f"/internal/tenants/{tenant_id}/staff-roles")
    except httpx.HTTPError as exc:
        logger.warning(
            "staff_roles_fetch_failed", reason="network_error", error_type=type(exc).__name__,
            tenant_id=str(tenant_id),
        )  # fmt: skip
        return None
    if response.status_code != 200:
        logger.info(
            "staff_roles_fetch_failed",
            reason="non_200_status",
            status_code=response.status_code,
            tenant_id=str(tenant_id),
        )
        return None
    try:
        body = response.json()
    except ValueError:
        logger.warning("staff_roles_fetch_failed", reason="invalid_json", tenant_id=str(tenant_id))
        return None
    roles = body.get("roles") if isinstance(body, dict) else None
    if not isinstance(roles, list):
        logger.warning("staff_roles_fetch_failed", reason="bad_shape", tenant_id=str(tenant_id))
        return None
    return ROLE_SECRETARY in roles
```

Em `src/secretaria/api/hub/calendar.py`:

1. No bloco `from secretaria.services import (...)`, adicione `brain_staff,` (em ordem alfabética) e `release_offer,` (depois de `clinic_action_notice,`).

2. Substitua o bloco que começa em `# Money first, Google second: a release whose deposit was PAID moves money` e termina no `raise HTTPException(...)` do `retention_ack_required` por:

```python
    # Money first, Google second: a release whose deposit was PAID hands the patient a choice
    # (TASK-052: "Manter horário" / "Remarcar" / "Cancelar"; cancelling or silence makes the
    # clinic return it), so the clinic must have read the consequence. Raised before anything
    # is touched.
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

3. Substitua o trecho de `deposit_outcome = await deposit_lifecycle.on_appointment_cancelled(` até o fim da chamada `patient_notice = await appointment_release.notify_released_patient(...)` por:

```python
    allow_paid = staff_patient_message.paid_notice_authorised(tenant, body.notify_outside_window)
    deposit_notice: str | None = None
    offer_buttons: list[tuple[str, str]] | None = None
    paid = await deposit_lifecycle.get_deposit_for_appointment(session, appt.id)
    if paid is not None and paid.status is PixDepositStatus.PAID:
        # TASK-052: the clinic freed a PAID slot. Nothing is decided about the money and the
        # platform never refunds (D15): ONE offer, answered by the patient or by the deadlines
        # (services/release_offer.py). No attention item yet.
        offer = await release_offer.open_offer(
            session,
            tenant=tenant,
            appointment=appt,
            deposit=paid,
            now=now,
            paid_notice_allowed=allow_paid,
            # Closing time: the clinic's with a secretary, else the doctor's (brain-api roles;
            # None while brain-api cannot say - services/brain_staff.py).
            has_secretary=await brain_staff.fetch_has_secretary(tenant.id),
        )
        deposit_outcome = release_offer.DEPOSIT_OUTCOME_OFFERED
        keep = offer is not None and release_offer.keep_allowed(offer, now)
        deposit_notice = release_offer.offer_body(
            deposit=paid,
            decide_by_at=offer.decide_by_at if offer is not None else now,
            timezone=tenant.timezone,
            justification=body.justification,
            confirmed=appt.confirmation_count >= 1,
            keep=keep,
        )
        offer_buttons = release_offer.offer_buttons(appt.id, keep=keep)
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
                # TASK-052: an unpaid deposit's release is recorded for the clinic's list only.
                await release_offer.open_offer(
                    session, tenant=tenant, appointment=appt, deposit=resolved, now=now
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
    )
```

(O restante — `await session.refresh(appt)`, o log e o retorno — fica igual.)

Em `src/secretaria/services/appointment_release.py`:

1. Em `notify_released_patient`, acrescente o parâmetro `offer_buttons: list[tuple[str, str]] | None = None,` depois de `now: datetime | None = None,`; acrescente à docstring:

```text
    `offer_buttons` (TASK-052): the choice of a PAID slot. Portal: the card carries the buttons
    and its body is `deposit_notice` (the whole offer text), plus the usual e-mail with the
    conversation link. WhatsApp: the job reads the open offer itself (same queued arguments);
    no e-mail is ever sent to a WhatsApp patient (owner decision 2026-10-12).
```

   e troque a chamada `return await _notify_portal(session, tenant, appointment, patient, professional_name, reason, deposit_notice)` por:

```python
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
```

2. Substitua a função `_notify_portal` inteira por:

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
            # TASK-052: the buttons ARE the way forward; the card is recorded with its options,
            # which is what lets the Portal accept the tap (workers/portal/inbound.py).
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

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_brain_staff.py tests/test_hub_release.py tests/test_deposit_release_preview.py tests/test_hub_cancel_portal_notice.py tests/test_hub_calendar_money.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/services/brain_staff.py src/secretaria/api/hub/calendar.py src/secretaria/services/appointment_release.py tests/test_brain_staff.py tests/test_hub_release.py`
Expected: PASS, ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/services/brain_staff.py src/secretaria/api/hub/calendar.py src/secretaria/services/appointment_release.py tests/test_brain_staff.py tests/test_hub_release.py
git commit -m "feat(calendar): releasing a paid slot opens the patient's offer; closing time from brain-api roles (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 7: O job do WhatsApp manda os botões da oferta (mesma assinatura; modelo pago fora da janela)

**Files:**
- Modify: `src/secretaria/workers/whatsapp/notifications.py` (`send_cancellation_notice`)
- Test: `tests/test_cancellation_notice.py` (3 testes novos)

**Interfaces:**
- Consumes: Task 3 (`find_offer`, `offer_buttons`, `keep_allowed`, `template_text`, `offer_body`), Task 5 (`settings.RELEASE_OFFER_TEMPLATE_NAME`), `SILENT_DECISIONS` (Task 1).
- Produces: com oferta ainda sem resposta e `extra_notice`: dentro da janela `send_buttons(body=extra_notice, buttons=offer_buttons(id, keep=keep_allowed(offer)))`; fora, com `allow_paid`, `send_template(RELEASE_OFFER_TEMPLATE_NAME, variables=[template_text(extra_notice)], button_payloads=[ids])`; oferta já respondida: nada sai.

- [ ] **Step 1: Escrever os testes**

Em `tests/test_cancellation_notice.py`:

1. No bloco `from secretaria.models import (...)` adicione `PixDeposit,` e `PixDepositStatus,`; troque `from secretaria.services import cancellation_notice as notice  # noqa: E402` por:

```python
from secretaria.models.deposit_release_offer import DepositReleaseOffer  # noqa: E402
from secretaria.services import (  # noqa: E402
    cancellation_notice as notice,
    release_offer,
)
```

e, se `get_settings` não estiver importado no arquivo, acrescente `from secretaria.config import get_settings  # noqa: E402`.

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


async def test_a_released_paid_slot_carries_the_choices_and_the_body_as_is(
    db, tenant, wa, metered
):
    """TASK-052: same job, same arguments; the open offer decides the card."""
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=2))
    await _open_paid_offer(db, appt)
    body = _offer_text()

    await _run(appt, justification="x" * 1500, extra=body)

    [sent] = wa.buttons
    ids = [bid for bid, _ in sent["buttons"]]
    assert ids[-2:] == [f"relresched|{appt.id}", f"relcancel|{appt.id}"]
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
    assert sent["button_payloads"][-1] == f"relcancel|{appt.id}"


async def test_an_offer_already_answered_sends_nothing(db, tenant, wa, metered):
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=2))
    await _open_paid_offer(db, appt)
    async with db() as session:
        offer = await session.scalar(select(DepositReleaseOffer))
        offer.decision = "kept"
        await session.commit()

    await _run(appt, extra=_offer_text())

    assert wa.buttons == [] and wa.templates == []
```

(`select`, `uuid4`, `datetime`, `UTC`, `timedelta`, `Appointment`, `AppointmentStatus`, `Tenant` já são usados no arquivo; se algum faltar, importe-o junto dos demais.)

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_cancellation_notice.py -q -p no:cacheprovider`
Expected: FAIL nos 3 novos.

- [ ] **Step 3: Implementar**

Em `src/secretaria/workers/whatsapp/notifications.py`:

1. Troque `from secretaria.services import cancellation_notice` por:

```python
from secretaria.models.deposit_release_offer import SILENT_DECISIONS
from secretaria.services import cancellation_notice, release_offer
```

2. Dentro do `async with async_session_factory() as session:` de `send_cancellation_notice`, depois de `waba_token = await get_waba_token(session, tenant.id)`:

```python
        # TASK-052: a slot the clinic released with a PAID deposit carries the patient's choice and
        # `extra_notice` IS the whole text. Read here, by the job, so the API's queued arguments
        # never changed (old worker = old card).
        offer = await release_offer.find_offer(
            session, tenant_id=tenant.id, released_appointment_id=appointment.id
        )
        offer_decision = offer.decision if offer is not None and offer.deposit_paid else None
        offer_keep = offer is not None and release_offer.keep_allowed(offer)
```

3. Logo ANTES de `if not await _claim_event(key):`:

```python
    if offer_decision is not None and offer_decision not in SILENT_DECISIONS:
        logger.info(
            "cancellation_notice_skipped", reason="offer_answered", appointment_id=appointment_id
        )
        return
```

4. No `try:` do envio, troque a primeira linha `if inside:` por:

```python
        if offer_decision is not None and extra_notice:
            choices = release_offer.offer_buttons(appointment_id, keep=offer_keep)
            if inside:
                await client.send_buttons(to=to, body=extra_notice, buttons=choices)
            else:
                # PENDÊNCIA META: RELEASE_OFFER_TEMPLATE_NAME is not approved yet (config.py);
                # until it is, this send fails and is retried/escalated like any other.
                await client.send_template(
                    to=to,
                    template=settings.RELEASE_OFFER_TEMPLATE_NAME,
                    lang=cancellation_notice.meta_language_code(tenant.language),
                    variables=[release_offer.template_text(extra_notice)],
                    button_payloads=[button_id for button_id, _label in choices],
                )
        elif inside:
```

(o corpo do antigo `if inside:` e o `else:` seguem iguais.)

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_cancellation_notice.py tests/test_workers_layering.py tests/test_build_identity.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/workers/whatsapp/notifications.py tests/test_cancellation_notice.py`
Expected: PASS, ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/workers/whatsapp/notifications.py tests/test_cancellation_notice.py
git commit -m "feat(whatsapp): the release notice of a paid slot carries the offer's buttons, same job arguments (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
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

### Task 9: Os toques (Manter / Remarcar / Cancelar / Recebido / Não recebido) e o cartão de 2 botões no Portal depois de S

**Files:**
- Create: `src/secretaria/workers/shared/release_offer_actions.py`
- Modify: `src/secretaria/schemas/webhook.py` (`_ACTION_BUTTON_PREFIXES`), `src/secretaria/workers/portal/inbound.py` (ação aceita), `src/secretaria/workers/portal/open.py` (`process_brain_message_enter`: cartão depois de S), `src/secretaria/workers/shared/actions.py` (chama os toques antes de tudo; frase do topo no "Outro horário"), `src/secretaria/services/flow_router.py` (`enter_rebooking`: o ramo "mesmo médico" passa o `prefix`)
- Test: `tests/test_release_offer_buttons.py` (novo)

**Interfaces:**
- Consumes: Task 3 (`request_keep`, `choose_reschedule`, `choose_cancel`, `record_received`, `request_reminder`, `settle_if_expired`, `close_if_no_answer`, `decline_keep`, `record_contact`, textos e resultados), Task 5 (`dispatch_due`); `_reply_sender(reply, tenant, waba_token)`, `get_waba_token`, `async_session_factory`, `BrainMessageSender`.
- Produces: `async handle_release_offer_tap(reply, action, appointment_id: UUID) -> tuple[str, str | None] | None` (None = respondido; `("rebooksame", prefixo)` entra no "Outro horário"; `("rebookno", None)` entra na pergunta de motivo); `async offer_card_after_start(tenant_id: UUID, external_id: str, *, now=None) -> bool`.

- [ ] **Step 1: Escrever o teste que falha**

`tests/test_release_offer_buttons.py`:

```python
"""The patient's taps on a slot the clinic released with a PAID deposit (TASK-052).

WhatsApp and the Portal reach the same handler (workers/shared/release_offer_actions.py through
`_handle_action_button`). One test per row of the spec's combination table (§6) that a tap can
reach. "Manter" only ASKS the clinic; "Remarcar" carries the deposit until the clinic marks it
refunded; "Cancelar" always tells the clinic at once. A tap on someone else's offer changes nothing.
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
from secretaria.services import email, release_offer as ro
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, BrainMessageSender
from secretaria.services.flow_router import STEP_DECLINE_REASON
from secretaria.workers import tasks
from secretaria.workers.orchestrator import _send_bot_reply
from secretaria.workers.portal.inbound import _persist_brain_message_inbound
from secretaria.workers.shared.release_offer_actions import offer_card_after_start
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import consent, sent, wire
from tests._reminders_v2 import outbound_messages, seed_paid_deposit, seed_world


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


@pytest.fixture
def mailbox(monkeypatch) -> list[dict]:
    box: list[dict] = []
    monkeypatch.setattr(
        email, "_send_sync", lambda to, subject, body: box.append({"to": to, "subject": subject})
    )
    monkeypatch.setattr(get_settings(), "SMTP_HOST", "smtp.exemplo", raising=False)
    return box


async def _released(db, *, starts_in=timedelta(days=2), **kwargs):  # noqa: F811
    """A world whose appointment the clinic released with a PAID deposit, offer open."""
    world = await seed_world(db, start_at=datetime.now(UTC) + starts_in, **kwargs)
    await seed_paid_deposit(db, world)
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.status = AppointmentStatus.CANCELLED
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == appointment.id)
        )
        tenant = await session.get(type(world.tenant), world.tenant.id)
        tenant.contact_email = "clinica@exemplo.com"
        await ro.open_offer(session, tenant=tenant, appointment=appointment, deposit=deposit)
        await session.commit()
    return world


async def _move(db, **fields):  # noqa: F811
    """Shift the offer (e.g. pretend B, S or E already passed, or the clinic decided)."""
    async with db() as session:
        offer = await session.scalar(select(DepositReleaseOffer))
        for name, value in fields.items():
            setattr(offer, name, value)
        await session.commit()


def _ago(minutes: int = 1) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


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


async def _mark(db, world):  # noqa: F811
    async with db() as session:
        offer = await session.scalar(select(DepositReleaseOffer))
        return await ro.mark_refunded(
            session,
            tenant_id=world.tenant.id,
            offer_id=offer.id,
            note="Estornado no painel do Asaas",
            resolved_by_name="Ana",
        )


def _texts() -> list[str]:
    return [entry[2] for entry in sent() if entry[0] in ("text", "buttons")]


def _last_buttons() -> list[str]:
    kind, _to, _body, buttons = [s for s in sent() if s[0] == "buttons"][-1]
    return [label for _id, label in buttons]


# --- Manter: the clinic decides ------------------------------------------------------------------


async def test_manter_before_s_asks_the_clinic_and_recreates_nothing(db, mailbox):  # noqa: F811
    world = await _released(db)

    await _tap(world, "relkeep")

    assert _texts()[-1].startswith("Avisamos a clínica que você quer manter o horário de")
    offer = await _offer(db, world)
    assert offer.decision == "keep_requested" and offer.new_appointment_id is None
    assert offer.first_contact_at is not None and offer.keep_deadline_at is not None
    assert await _sends(db) == {("clinic_keep_request", 0): "sent"}  # right after the tap
    assert "manter o horário" in mailbox[0]["subject"]
    async with db() as session:
        live = await session.scalar(
            select(Appointment).where(Appointment.status == AppointmentStatus.SCHEDULED)
        )
    assert live is None


async def test_manter_after_b_is_still_asked(db, mailbox):  # noqa: F811
    world = await _released(db)
    await _move(db, decide_by_at=_ago())

    await _tap(world, "relkeep")

    offer = await _offer(db, world)
    assert offer.decision == "keep_requested" and offer.refund_status == "due"  # B ran first
    assert _texts()[-1].startswith("Avisamos a clínica")


async def test_manter_after_s_on_whatsapp_answers_with_remarcar_and_cancelar_only(db):  # noqa: F811
    world = await _released(db, starts_in=timedelta(hours=1))
    await _move(db, slot_start_at=_ago(5))

    await _tap(world, "relkeep")

    assert _texts()[-1] == ro.AFTER_START_TEXT
    assert _last_buttons() == ["Remarcar", "Cancelar"]
    assert (await _offer(db, world)).decision == "none"


async def test_manter_twice_tells_the_clinic_once(db, mailbox):  # noqa: F811
    world = await _released(db)

    await _tap(world, "relkeep")
    await _tap(world, "relkeep")

    assert _texts()[-1] == ro.KEEP_ALREADY_REQUESTED_TEXT and len(mailbox) == 1


async def test_manter_after_the_refund_was_marked_or_after_a_refusal(db, mailbox):  # noqa: F811
    world = await _released(db)
    await _move(db, decide_by_at=_ago())
    await _tap(world, "relgot")  # settles B (refund due) and answers "not yet"
    assert await _mark(db, world) == "marked"

    await _tap(world, "relkeep")
    assert _texts()[-1] == ro.KEEP_AFTER_REFUND_TEXT and _last_buttons() == ["Remarcar", "Cancelar"]

    refused = await _released(db, phone_number_id="pnid-8", wa_id="5511900000088")
    await _tap(refused, "relkeep")
    async with db() as session:
        row = await session.scalar(
            select(DepositReleaseOffer).where(
                DepositReleaseOffer.released_appointment_id == refused.appointment.id
            )
        )
        await ro.decline_keep(session, offer=row, outcome="refused", resolved_by="Ana")
        await session.commit()
    await _tap(refused, "relkeep")
    assert _texts()[-1] == ro.KEEP_DECLINED_TAP_TEXT


async def test_manter_after_cancelar_is_refused(db, mailbox):  # noqa: F811
    world = await _released(db)

    await _tap(world, "relcancel")
    await _tap(world, "relkeep")

    assert _texts()[-1] == ro.TAP_KEEP_AFTER_CANCEL_TEXT
    assert (await _offer(db, world)).decision == "cancelled"


# --- Remarcar ------------------------------------------------------------------------------------


@pytest.mark.parametrize("moment", ["before_b", "after_s", "after_b", "after_cancel"])
async def test_remarcar_carries_the_deposit_whenever_the_refund_is_not_marked(  # noqa: F811
    db, mailbox, moment
):
    starts_in = timedelta(hours=1) if moment == "after_s" else timedelta(days=2)
    world = await _released(db, starts_in=starts_in)
    if moment == "after_s":
        await _move(db, slot_start_at=_ago(5))
    if moment == "after_b":
        await _move(db, decide_by_at=_ago())
    if moment == "after_cancel":
        await _tap(world, "relcancel")

    await _tap(world, "relresched")

    kind, _to, body, _label, rows, _section = sent()[-1]
    assert kind == "list" and body.startswith(ro.RESCHEDULE_PREFIX)
    assert rows[0][0].startswith("day|")
    assert (await _offer(db, world)).decision == "rescheduling"
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
    assert conversation.flow_state == FlowState.SERVICE_CATALOG


async def test_remarcar_after_the_refund_was_marked_charges_again(db, mailbox):  # noqa: F811
    world = await _released(db)
    await _move(db, decide_by_at=_ago())
    await _tap(world, "relgot")
    await _mark(db, world)

    await _tap(world, "relresched")

    assert sent()[-1][2].startswith(ro.RESCHEDULE_NEW_CHARGE_PREFIX)


# --- Cancelar ------------------------------------------------------------------------------------


@pytest.mark.parametrize("moment", ["before_s", "after_s", "after_b"])
async def test_cancelar_always_tells_the_clinic_now_and_asks_why(db, mailbox, moment):  # noqa: F811
    starts_in = timedelta(hours=1) if moment == "after_s" else timedelta(days=2)
    world = await _released(db, starts_in=starts_in)
    if moment == "after_s":
        await _move(db, slot_start_at=_ago(5))
    if moment == "after_b":
        await _move(db, decide_by_at=_ago())

    await _tap(world, "relcancel")

    assert any("sua consulta foi cancelada" in text and "R$ 100,00" in text for text in _texts())
    offer = await _offer(db, world)
    assert (offer.decision, offer.refund_status) == ("cancelled", "due")
    assert (await _sends(db))[("clinic_refund_cancel", 0)] == "sent"  # right after the tap
    assert len(await _items(db)) == 1
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
    assert conversation.flow_step == STEP_DECLINE_REASON


async def test_cancelar_after_marked_refunded_sends_nothing_new(db, mailbox):  # noqa: F811
    world = await _released(db)
    await _move(db, decide_by_at=_ago())
    await _tap(world, "relgot")
    await _mark(db, world)

    await _tap(world, "relcancel")

    assert ro.CANCEL_AFTER_MARKED_TEXT in _texts()
    assert ("clinic_refund_cancel", 0) not in await _sends(db)


async def test_cancelar_twice_or_after_a_kept_slot(db, mailbox):  # noqa: F811
    world = await _released(db)
    await _tap(world, "relcancel")
    await _tap(world, "relcancel")
    assert _texts()[-1] == ro.TAP_REFUND_ALREADY_TEXT and len(mailbox) == 1

    kept = await _released(db, phone_number_id="pnid-7", wa_id="5511900000077")
    async with db() as session:
        row = await session.scalar(
            select(DepositReleaseOffer).where(
                DepositReleaseOffer.released_appointment_id == kept.appointment.id
            )
        )
        row.decision = "kept"
        await session.commit()
    await _tap(kept, "relcancel")
    assert _texts()[-1] == ro.TAP_CANCEL_AFTER_KEEP_TEXT


# --- T + 24 h ------------------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["relkeep", "relresched", "relcancel"])
async def test_any_tap_after_e_is_answered_and_reopens_nothing(db, mailbox, action):  # noqa: F811
    world = await _released(db)
    await _move(db, decide_by_at=_ago(60 * 18), expires_at=_ago())

    await _tap(world, action)

    assert _texts()[-1] == ro.OFFER_CLOSED_NO_ANSWER_TEXT
    offer = await _offer(db, world)
    assert (offer.decision, offer.refund_status) == ("no_answer_cancelled", "due")
    assert len(await _items(db)) == 1


# --- Recebido / Não recebido --------------------------------------------------------------------


async def test_recebido_and_nao_recebido_follow_the_clinics_marking(db, mailbox):  # noqa: F811
    world = await _released(db)
    await _tap(world, "relgot")
    assert _texts()[-1] == ro.CHECK_TOO_EARLY_TEXT

    await _tap(world, "relcancel")
    assert await _mark(db, world) == "marked"

    await _tap(world, "relmiss")
    assert _texts()[-1] == ro.NOT_RECEIVED_TEXT
    await _tap(world, "relmiss")
    assert _texts()[-1] == ro.NOT_RECEIVED_RECENT_TEXT
    sends = await _sends(db)
    assert sends[("clinic_refund_reminder", 1)] == "sent"
    assert ("clinic_refund_reminder", 2) not in sends

    await _tap(world, "relgot")
    assert _texts()[-1] == ro.RECEIVED_TEXT
    await _tap(world, "relgot")
    assert _texts()[-1] == ro.CHECK_CLOSED_TEXT


# --- isolation and the Portal --------------------------------------------------------------------


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

    assert _texts()[-1] == ro.NOT_FOUND_TEXT
    offer = await _offer(db, world)
    assert offer.decision == "none" and offer.first_contact_at is None


async def test_another_clinics_offer_is_never_reached(db):  # noqa: F811
    world = await _released(db)
    other = await seed_world(db, phone_number_id="pnid-2", wa_id="5511900000002")

    await tasks._handle_action_button(
        _reply(other, conversation_id=other.conversation.id), "relcancel", str(world.appointment.id)
    )

    assert (await _offer(db, world)).decision == "none" and await _items(db) == []


async def test_a_portal_tap_on_the_recorded_card_is_the_same_action(db, mailbox):  # noqa: F811
    world = await _released(db, channel=CHANNEL_BRAIN_MESSAGE)
    await consent(db, world)
    async with db() as session:
        sender = BrainMessageSender(conversation_id=world.conversation.id, session=session)
        await sender.send_buttons(
            to=world.patient.external_id,
            body=ro.OFFER_HEAD,
            buttons=ro.offer_buttons(world.appointment.id),
        )
        await session.commit()

    reply = await _persist_brain_message_inbound(
        tenant_id=world.tenant.id,
        external_id=world.patient.external_id,
        text="Manter horário",
        interactive_reply_id=f"relkeep|{world.appointment.id}",
    )
    await _send_bot_reply(reply)

    assert reply.action_button == ("relkeep", str(world.appointment.id))
    assert (await _offer(db, world)).decision == "keep_requested"
    bodies = [m.body for m in await outbound_messages(db, world.conversation.id)]
    assert any(body.startswith("Avisamos a clínica") for body in bodies)


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
    assert (await _offer(db, world)).decision == "none"


async def test_opening_the_portal_after_s_shows_remarcar_and_cancelar_once(db):  # noqa: F811
    world = await _released(db, channel=CHANNEL_BRAIN_MESSAGE, starts_in=timedelta(hours=1))
    await _move(db, slot_start_at=_ago(5))

    assert await offer_card_after_start(world.tenant.id, world.patient.external_id)
    assert not await offer_card_after_start(world.tenant.id, world.patient.external_id)

    [card] = await outbound_messages(db, world.conversation.id)
    assert card.body == ro.AFTER_START_TEXT
    assert [o["title"] for o in card.interactive["options"]] == ["Remarcar", "Cancelar"]


async def test_opening_the_portal_before_s_sends_nothing_extra(db):  # noqa: F811
    world = await _released(db, channel=CHANNEL_BRAIN_MESSAGE)

    assert not await offer_card_after_start(world.tenant.id, world.patient.external_id)
    assert await outbound_messages(db, world.conversation.id) == []
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_offer_buttons.py -q -p no:cacheprovider`
Expected: FAIL (`ImportError` de `release_offer_actions`; depois, `rel*|` ainda não são ações).

- [ ] **Step 3: Implementar**

`src/secretaria/workers/shared/release_offer_actions.py`:

```python
"""release_offer_actions - the patient's taps on a released PAID slot (TASK-052).

Owner decisions 2026-10-12 (3rd round). Channel-neutral: WhatsApp and the Portal reach
`handle_release_offer_tap` through `actions._handle_action_button`, which calls it first for the
`rel*|<released appointment id>` family. Before answering it applies the deadlines that may have
passed since the last sweep (B, E, and the clinic's 24 h to decide "Manter") and records the
patient's first contact.

* "Manter horário" only ASKS the clinic (`request_keep`): the clinic accepts or refuses in the
  TASK-054 list. After S it answers "O horário da consulta já passou" with Remarcar / Cancelar
  (a NEW WhatsApp message; the Portal also shows that card when the patient opens the
  conversation after S - `offer_card_after_start`).
* "Remarcar" at any time while the offer is open: carries the deposit, or charges again when the
  clinic already marked it refunded. After E ("cancelada por falta de resposta") nothing reopens.
* "Cancelar" always tells the clinic at once (unless it already marked the refund).
* "Recebido" / "Não recebido" after the clinic marked the refund.

Returns None when answered here, or `(action, prefix)` for the caller: ("rebooksame", line above
the day list) enters "Outro horário"; ("rebookno", None) asks why. A tap whose offer is missing,
belongs to another patient or another clinic is answered like an unknown appointment.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Conversation, Patient, PixDeposit, Tenant
from secretaria.models.deposit_release_offer import (
    DECISION_CANCELLED,
    DECISION_KEPT,
    DECISION_NO_ANSWER,
    DECISION_NOT_OFFERED,
    KEEP_FROM,
    KEEP_OUTCOME_TIMEOUT,
    DepositReleaseOffer,
)
from secretaria.services import release_offer as ro, release_offer_dispatch
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, BrainMessageSender
from secretaria.services.payments.money import format_brl
from secretaria.services.tenant_config import get_waba_token
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.sender import _reply_sender

logger = get_logger(__name__)

_TERMINAL = (DECISION_KEPT, "rebooked", DECISION_NO_ANSWER, DECISION_NOT_OFFERED)


@dataclass
class _Answer:
    text: str | None = None
    buttons: list[tuple[str, str]] | None = None
    follow: tuple[str, str | None] | None = None
    dispatch: bool = False


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
        timezone, tenant_id, conversation_id = tenant.timezone, tenant.id, conversation.id
        client = _reply_sender(reply, tenant, await get_waba_token(session, tenant.id))
        offer = await ro.find_offer(
            session, tenant_id=tenant_id, released_appointment_id=appointment_id
        )
        if (
            offer is None
            or not offer.deposit_paid
            or offer.patient_id is None
            or offer.patient_id != conversation.patient_id
        ):
            if client is not None:
                await client.send_text_message(to=reply.patient_ref, body=ro.NOT_FOUND_TEXT)
            return None
        offer_id = offer.id
        now = datetime.now(UTC)
        settled = await ro.settle_if_expired(session, offer=offer, now=now)
        settled = await ro.close_if_no_answer(session, offer=offer, now=now) or settled
        settled = (
            await ro.decline_keep(session, offer=offer, outcome=KEEP_OUTCOME_TIMEOUT, now=now)
            or settled
        )
        offer = await session.get(DepositReleaseOffer, offer_id, populate_existing=True)
        if action in ro.OFFER_ACTIONS and offer.decision not in _TERMINAL:
            await ro.record_contact(session, offer_id=offer_id, now=now)
        await session.commit()

        if action == ro.ACTION_KEEP:
            answer = await _on_keep(session, offer, now, timezone)
        elif action == ro.ACTION_RESCHEDULE:
            answer = await _on_reschedule(session, offer, conversation_id, now)
        elif action == ro.ACTION_CANCEL:
            answer = await _on_cancel(session, offer, now)
        elif action == ro.ACTION_RECEIVED:
            answer = await _on_received(session, offer, now)
        else:
            answer = await _on_not_received(session, offer, now)
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
    if answer.dispatch:
        try:
            async with async_session_factory() as session:
                await release_offer_dispatch.dispatch_due(session, offer_id=offer_id)
        except Exception as exc:  # noqa: BLE001 - the sweep retries what did not go now
            logger.warning("deposit_release_offer_dispatch_failed", error_type=type(exc).__name__)
    return answer.follow


def _closed_answer(offer: DepositReleaseOffer, action: str) -> _Answer:
    """Kept / rebooked / cancelled-for-no-answer: what each button says."""
    if offer.decision == DECISION_NO_ANSWER:
        return _Answer(text=ro.OFFER_CLOSED_NO_ANSWER_TEXT)
    if action == ro.ACTION_CANCEL:
        return _Answer(text=ro.TAP_CANCEL_AFTER_KEEP_TEXT)
    return _Answer(text=ro.TAP_ALREADY_REBOOKED_TEXT)


async def _on_keep(session, offer, now, timezone) -> _Answer:
    fallback = ro.keep_fallback_buttons(offer.released_appointment_id)
    released = await session.get(Appointment, offer.released_appointment_id)
    when = (
        ro.slot_text(released.start_at, timezone)
        if released is not None and released.start_at is not None
        else ""
    )
    outcome = await ro.request_keep(session, offer=offer, now=now)
    await session.commit()
    if outcome == ro.KEEP_REQUESTED_NOW:
        return _Answer(text=ro.KEEP_REQUESTED_TEXT.format(when=when), dispatch=True)
    if outcome == ro.KEEP_ALREADY_REQUESTED:
        return _Answer(text=ro.KEEP_ALREADY_REQUESTED_TEXT)
    if outcome == ro.KEEP_AFTER_START:
        return _Answer(text=ro.AFTER_START_TEXT, buttons=fallback)
    if outcome == ro.KEEP_AFTER_REFUND:
        return _Answer(text=ro.KEEP_AFTER_REFUND_TEXT, buttons=fallback)
    if outcome == ro.KEEP_DECLINED_BEFORE:
        return _Answer(text=ro.KEEP_DECLINED_TAP_TEXT, buttons=fallback)
    current = await session.get(DepositReleaseOffer, offer.id, populate_existing=True)
    if current.decision == DECISION_CANCELLED:
        return _Answer(text=ro.TAP_KEEP_AFTER_CANCEL_TEXT)
    if current.decision == DECISION_KEPT:
        return _Answer(text=ro.TAP_ALREADY_KEPT_TEXT.format(when=when))
    return _closed_answer(current, ro.ACTION_KEEP)


async def _on_reschedule(session, offer, conversation_id, now) -> _Answer:
    outcome = await ro.choose_reschedule(
        session, offer=offer, conversation_id=conversation_id, now=now
    )
    await session.commit()
    if outcome == ro.RESCHEDULE_CARRYING:
        return _Answer(follow=("rebooksame", ro.RESCHEDULE_PREFIX))
    if outcome == ro.RESCHEDULE_NEW_CHARGE:
        return _Answer(follow=("rebooksame", ro.RESCHEDULE_NEW_CHARGE_PREFIX))
    current = await session.get(DepositReleaseOffer, offer.id, populate_existing=True)
    return _closed_answer(current, ro.ACTION_RESCHEDULE)


async def _on_cancel(session, offer, now) -> _Answer:
    offer_id, deposit_id = offer.id, offer.deposit_id
    outcome = await ro.choose_cancel(session, offer=offer, now=now)
    await session.commit()
    if outcome == ro.CANCEL_NOTIFIED:
        deposit = await session.get(PixDeposit, deposit_id)
        amount = format_brl(deposit.amount_cents) if deposit is not None else ""
        return _Answer(
            text=ro.CANCEL_DONE_TEXT.format(amount=amount),
            follow=("rebookno", None),
            dispatch=True,
        )
    if outcome == ro.CANCEL_AFTER_REFUND:
        return _Answer(text=ro.CANCEL_AFTER_MARKED_TEXT, follow=("rebookno", None))
    current = await session.get(DepositReleaseOffer, offer_id, populate_existing=True)
    if current.decision == DECISION_CANCELLED:
        return _Answer(text=ro.TAP_REFUND_ALREADY_TEXT)
    return _closed_answer(current, ro.ACTION_CANCEL)


async def _on_received(session, offer, now) -> _Answer:
    outcome = await ro.record_received(session, offer=offer, now=now)
    await session.commit()
    if outcome == "closed":
        return _Answer(text=ro.RECEIVED_TEXT)
    if outcome == "already_closed":
        return _Answer(text=ro.CHECK_CLOSED_TEXT)
    return _Answer(text=ro.CHECK_TOO_EARLY_TEXT)


async def _on_not_received(session, offer, now) -> _Answer:
    outcome = await ro.request_reminder(session, offer=offer, now=now)
    await session.commit()
    if outcome == "sent":
        return _Answer(text=ro.NOT_RECEIVED_TEXT, dispatch=True)
    if outcome == "recent":
        return _Answer(text=ro.NOT_RECEIVED_RECENT_TEXT)
    if outcome == "closed":
        return _Answer(text=ro.CHECK_CLOSED_TEXT)
    return _Answer(text=ro.CHECK_TOO_EARLY_TEXT)


async def offer_card_after_start(
    tenant_id: UUID, external_id: str, *, now: datetime | None = None
) -> bool:
    """Portal entry after S with the offer still unanswered: ONE card with Remarcar / Cancelar."""
    now = now or datetime.now(UTC)
    async with async_session_factory() as session:
        patient = await session.scalar(
            select(Patient).where(
                Patient.tenant_id == tenant_id,
                Patient.channel == CHANNEL_BRAIN_MESSAGE,
                Patient.external_id == external_id,
            )
        )
        if patient is None:
            return False
        offer = await session.scalar(
            select(DepositReleaseOffer)
            .where(
                DepositReleaseOffer.tenant_id == tenant_id,
                DepositReleaseOffer.patient_id == patient.id,
                DepositReleaseOffer.deposit_paid.is_(True),
                DepositReleaseOffer.decision.in_(KEEP_FROM),
                DepositReleaseOffer.slot_start_at <= now,
                DepositReleaseOffer.expires_at > now,
                DepositReleaseOffer.after_start_card_at.is_(None),
            )
            .order_by(DepositReleaseOffer.released_at.desc())
            .limit(1)
        )
        if offer is None or offer.conversation_id is None:
            return False
        conversation_id, released_id = offer.conversation_id, offer.released_appointment_id
        claimed = await session.execute(
            update(DepositReleaseOffer)
            .where(
                DepositReleaseOffer.id == offer.id,
                DepositReleaseOffer.after_start_card_at.is_(None),
            )
            .values(after_start_card_at=now)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        if claimed.rowcount != 1:
            return False
        sender = BrainMessageSender(conversation_id=conversation_id, session=session)
        await sender.send_buttons(
            to=external_id,
            body=ro.AFTER_START_TEXT,
            buttons=ro.keep_fallback_buttons(released_id),
        )
        await session.commit()
    logger.info("deposit_release_offer_after_start_card", tenant_id=str(tenant_id))
    return True
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
1. Antes de `from secretaria.services.reminder_text import REMINDER_ROW_ACTIONS`, adicione `from secretaria.services.release_offer import RELEASE_OFFER_ACTIONS`.
2. Troque `if decoded is not None and decoded[0] in REMINDER_ROW_ACTIONS:` por:

```python
                if decoded is not None and (
                    decoded[0] in REMINDER_ROW_ACTIONS or decoded[0] in RELEASE_OFFER_ACTIONS
                ):
                    # TASK-052: the choices on a released PAID slot are ACTIONS on this channel
                    # too (the card was recorded with its options, so the id was validated above).
```

Em `src/secretaria/workers/portal/open.py`:
1. Adicione `from secretaria.workers.shared.release_offer_actions import offer_card_after_start` junto dos outros imports `secretaria.workers.shared.*`.
2. Em `process_brain_message_enter`, logo depois de `tenant_uuid = UUID(tenant_id)`:

```python
    # TASK-052: after the released consultation's start, opening the conversation shows only
    # "Remarcar" / "Cancelar" (once per offer); nothing else is said on this entry.
    if await offer_card_after_start(tenant_uuid, external_id):
        return
```

Em `src/secretaria/workers/shared/actions.py`:
1. Depois de `from secretaria.services.payments import deposit_lifecycle`, adicione `from secretaria.services.release_offer import RELEASE_OFFER_ACTIONS`; entre o bloco `from secretaria.workers.shared.llm_context import (...)` e `from secretaria.workers.shared.reminder_actions import (...)`, adicione `from secretaria.workers.shared.release_offer_actions import handle_release_offer_tap`.
2. Logo depois do bloco `if action in REMINDER_ROW_ACTIONS: ...` (que termina em `appt_uuid = UUID(appointment_id)`):

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

Em `src/secretaria/services/flow_router.py`, em `enter_rebooking`, no ramo "mesmo médico" (a chamada `await enter_day_picker(...)` com `branch=BOOKING_DAY_BRANCH,` e `professionals=professionals,`), acrescente:

```python
            # TASK-052: "Remarcar" on a released PAID slot says what happens to the deposit.
            prefix=prefix,
```

(`enter_rebooking` e `enter_day_picker` já aceitam `prefix`.)

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_offer_buttons.py tests/test_action_buttons.py tests/test_rebooking_flow.py tests/test_reminder_r3_portal.py tests/test_brain_message_interactive_tap.py tests/test_reminder_v2_text.py tests/test_reminder_r3_ids.py tests/test_workers_layering.py tests/test_brain_message_open.py tests/test_portal_context_opening.py tests/test_reminder_link_entry.py tests/test_first_message_reminder.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/workers/shared/release_offer_actions.py src/secretaria/workers/shared/actions.py src/secretaria/schemas/webhook.py src/secretaria/workers/portal/inbound.py src/secretaria/workers/portal/open.py src/secretaria/services/flow_router.py tests/test_release_offer_buttons.py`
Expected: PASS (26 casos novos, contando os parametrizados), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/workers/shared/release_offer_actions.py src/secretaria/workers/shared/actions.py src/secretaria/schemas/webhook.py src/secretaria/workers/portal/inbound.py src/secretaria/workers/portal/open.py src/secretaria/services/flow_router.py tests/test_release_offer_buttons.py
git commit -m "feat(flow): keep request, reschedule any time, cancel and refund-check taps on a released paid slot; Portal card after the start (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 10: A nova consulta do "Remarcar" leva o sinal pago (fluxo, promoção do Portal e portão do "antes")

**Files:**
- Modify: `src/secretaria/workers/shared/flow_runner.py` (`_apply_flow_result`), `src/secretaria/workers/shared/booking_hold.py` (`_promote_booking_hold`), `src/secretaria/services/booking_hold.py` (`BookingGate._deposit_quote`)
- Test: `tests/test_release_offer_carry_over.py` (novo)

**Interfaces:**
- Consumes: Task 3 (`claim_carry_over`, `carry_over_pending`, `CARRIED_OVER_NOTE`, `REASON_CARRIED_OVER`, `expire_offers`, `mark_refunded`), `deposit_gate.DepositQuote`.
- Produces: a consulta criada numa conversa com oferta em `rescheduling` e devolução ainda NÃO marcada (antes OU depois de B) recebe o sinal PAGO na mesma transação; a devolução devida é desfeita (item fechado como "remarcado", avisos de devolução descartados) e a clínica recebe `clinic_rebooked_deposit`; a última bolha (ou a confirmação do Portal) ganha `CARRIED_OVER_NOTE`; o portão do "antes" responde `DepositQuote("no_deposit", "carried_over")`. Com a devolução marcada, nada é transferido (nova cobrança).

- [ ] **Step 1: Escrever o teste que falha**

`tests/test_release_offer_carry_over.py`:

```python
"""The booking that ends a "Remarcar" carries the released slot's PAID deposit (TASK-052).

The deposit moves to the new appointment in the booking's own transaction, without a new charge
and without touching `reschedule_count`; the `before` gate asks for no Pix while the offer is in
`rescheduling` and the refund is not marked - before OR after B; once the clinic marked the
refund, the booking stands on its own (new charge).
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
from secretaria.models.deposit_release_offer import DepositReleaseEmail, DepositReleaseOffer
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
    assert (offer.decision, offer.new_appointment_id) == ("rebooked", new.id)
    assert items == []
    [text] = [item[2] for item in sent() if item[0] == "text"]
    assert text == f"{CONFIRMED}\n\n{release_offer.CARRIED_OVER_NOTE}"


async def test_without_the_remarcar_tap_nothing_is_carried(db):  # noqa: F811
    world = await _rescheduling(db, tap=False)

    await _confirm(world, _booking(_future(6)))

    new, deposit, offer, _items = await _state(db)
    assert new is not None and deposit.appointment_id == world.appointment.id
    assert offer.decision == "none"
    assert [item[2] for item in sent() if item[0] == "text"] == [CONFIRMED]


async def test_a_booking_after_b_still_carries_and_undoes_the_refund(db):  # noqa: F811
    world = await _rescheduling(db)
    async with db() as session:  # B passed meanwhile: the refund fell due, the clinic was told
        await release_offer.expire_offers(session, now=datetime.now(UTC) + timedelta(hours=7))

    await _confirm(world, _booking(_future(6)))

    new, deposit, offer, items = await _state(db)
    assert deposit.appointment_id == new.id and deposit.status is PixDepositStatus.PAID
    assert (offer.decision, offer.refund_status) == ("rebooked", "not_due")
    (item,) = items
    assert (item.status, item.resolution_action) == ("resolved", "rebooked")
    async with db() as session:
        pending = [
            row.kind
            for row in await session.scalars(
                select(DepositReleaseEmail).where(DepositReleaseEmail.claimed_at.is_(None))
            )
        ]
    assert pending == ["clinic_rebooked_deposit"]  # "REMARCADA levando o sinal", nothing else


async def test_a_booking_after_the_clinic_marked_refunded_charges_again(db):  # noqa: F811
    world = await _rescheduling(db)
    async with db() as session:
        await release_offer.expire_offers(session, now=datetime.now(UTC) + timedelta(hours=7))
    async with db() as session:
        offer = await session.scalar(select(DepositReleaseOffer))
        assert (
            await release_offer.mark_refunded(
                session,
                tenant_id=world.tenant.id,
                offer_id=offer.id,
                note="Estornado no painel do Asaas",
                resolved_by_name="Ana",
            )
            == "marked"
        )

    await _confirm(world, _booking(_future(6)))

    new, deposit, offer, _items = await _state(db)
    assert new is not None and deposit.appointment_id == world.appointment.id
    assert deposit.status is PixDepositStatus.CANCELLED_REFUNDED
    assert offer.refund_status == "marked"
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

(Diffs do plano anterior, verificados sobre `3650e10`; o estado da oferta (`decision`, `refund_status`) fica todo dentro de `claim_carry_over`.)

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
+                        # the deposit moves to THIS booking in this transaction (no new charge)
+                        # unless the clinic already marked it refunded.
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
Expected: PASS (6 novos), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/shared/booking_hold.py src/secretaria/services/booking_hold.py tests/test_release_offer_carry_over.py
git commit -m "feat(payments): the booking that ends a Remarcar carries the released slot's paid deposit, no new charge (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 11: O contrato da aba (TASK-054): listar, marcar devolvido, aceitar / recusar "Manter"

**Files:**
- Create: `src/secretaria/schemas/release_offers.py`, `src/secretaria/api/hub/release_offers.py`
- Modify: `src/secretaria/main.py` (registrar o router)
- Test: `tests/test_release_offers_hub.py` (novo)

**Interfaces:**
- Consumes: Task 3 (`patient_decision`, `mark_refunded`, `decline_keep`, constantes), Task 4 (`accept_keep`, `ACCEPT_*`), Task 5 (`dispatch_due`); `reminder_hooks.after_appointment_booked`; `get_current_tenant`, `get_agenda_viewer` (`api/hub/deps.py`) e `AgendaViewer` (`services/agenda_visibility.py`) — a MESMA regra de escopo de `api/hub/calendar.py::list_events` (`mine`, `no_own_agenda`); `attention_view.check_resolution_input`, `ResolveRefused`.
- Produces (spec §5.10). Todas as rotas: qualquer papel da clínica (secretária, gestor, médico), filtradas pela clínica do token e pelo escopo de agenda — quem só vê os seus nunca vê nem age sobre a oferta de outro médico (404); `mine=true` sem profissional → 422 `no_own_agenda`.
  - `GET /tenants/me/release-offers?pending_only=false&mine=false&limit=100` → `{"items": [ReleaseOfferOut]}` (mais novos primeiro).
  - `POST /tenants/me/release-offers/{id}/refunded` `{"resolved_by_name", "note"?}` → 200 / 409 `refund_not_due` | `keep_pending` / 404 / 422.
  - `POST /tenants/me/release-offers/{id}/keep/accept` `{"resolved_by_name"}` → 200 / 409 `not_requested` | `slot_taken` | `slot_past` | `refund_already_marked` / 503 `calendar_unavailable` / 404 / 422.
  - `POST /tenants/me/release-offers/{id}/keep/refuse` `{"resolved_by_name"}` → 200 (agora ou antes) / 409 `not_requested` / 404 / 422.
  - `ReleaseOfferOut`: `id, released_appointment_id, new_appointment_id, conversation_id, patient_display_name, professional_id, service_label, slot_start, slot_end, amount_cents, deposit_paid_at_release, deposit_status, refund_status, refund_source, decision, patient_decision, keep_deadline_at, released_at, decide_by_at, expires_at, first_contact_at, refund_due_at, refund_marked_at, patient_answered_at, can_mark_refunded, can_decide_keep`.

- [ ] **Step 1: Escrever o teste que falha**

`tests/test_release_offers_hub.py`:

```python
"""The hub contract the TASK-054 list consumes (TASK-052, spec §5.10, owner 2026-10-12).

Every role may list, mark "devolvido" and accept/refuse "Manter" - but only on what it already sees
in the agenda (`get_agenda_viewer`, the rule of GET /calendar/events): a doctor restricted to his
own never reaches another doctor's offer. Marking and deciding are idempotent and tenant-scoped.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from secretaria.api.hub.deps import get_agenda_viewer, get_current_tenant
from secretaria.core.database import get_session
from secretaria.core.subscription import AGENDA_SCOPE_CLINIC, AGENDA_SCOPE_OWN
from secretaria.models import Appointment, AppointmentStatus, PixDeposit, Tenant
from secretaria.models.deposit_release_offer import DepositReleaseEmail, DepositReleaseOffer
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services import booking_promotion, release_offer as ro, reminder_hooks
from secretaria.services.agenda_visibility import CLINIC_WIDE, AgendaViewer
from tests.payments_support import make_sessionmaker
from tests.release_offer_support import CLOSES_AT_SIX, T, make_released

OFFERS = "/tenants/me/release-offers"
BODY = {"resolved_by_name": "Ana"}


class _FreeCalendar:
    async def check_availability(self, start, end, max_results=50):
        return []

    async def create_event(
        self, start, end, summary, description="", reminders=None, *, event_id=None
    ):
        return {"id": event_id, "htmlLink": "https://calendar.google.com/x"}

    async def cancel_event(self, event_id):
        return None


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _cleanup(monkeypatch):
    async def _for_booking(session, tenant, professional_id):
        return _FreeCalendar()

    async def _booked(appointment_id, *, now=None):
        return 0

    monkeypatch.setattr(booking_promotion, "calendar_for_booking", _for_booking)
    monkeypatch.setattr(reminder_hooks, "after_appointment_booked", _booked)
    yield
    from secretaria.main import app

    for dependency in (get_session, get_current_tenant, get_agenda_viewer):
        app.dependency_overrides.pop(dependency, None)


def _wire(db, tenant_id, viewer: AgendaViewer = CLINIC_WIDE):
    """A logged-in member of `tenant_id` who sees what `viewer` sees (any role)."""
    from fastapi import Depends

    from secretaria.main import app

    async def _session():
        async with db() as session:
            yield session

    async def _tenant(session=Depends(get_session)):
        return await session.get(Tenant, tenant_id)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = _tenant
    app.dependency_overrides[get_agenda_viewer] = lambda: viewer


async def _opened(db, *, now=T, **kw):
    async with db() as session:
        released = await make_released(session, **kw)
        offer = await ro.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=now,
        )
        await session.commit()
    return released, offer


async def _do(db, fn, offer_id, at, **kw):
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer_id)
        await fn(session, offer=row, now=at, **kw)
        await session.commit()


async def test_the_list_shows_what_the_tab_needs_and_never_another_clinic(client: AsyncClient, db):
    released, offer = await _opened(db)
    await _do(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    _wire(db, released.tenant_id)
    _other, foreign = await _opened(db)

    items = (await client.get(OFFERS)).json()["items"]

    assert [i["id"] for i in items] == [str(offer.id)]
    (item,) = items
    assert (item["patient_decision"], item["refund_status"]) == ("cancelled", "due")
    assert item["deposit_status"] == "confirmado_pago" and item["amount_cents"] == 6000
    assert item["can_mark_refunded"] is True and item["can_decide_keep"] is False
    assert item["patient_display_name"] == "Maria"
    assert str(foreign.id) not in str(items)


async def test_unpaid_releases_are_listed_and_pending_only_filters(client: AsyncClient, db):
    released, paid = await _opened(db)
    _wire(db, released.tenant_id)
    async with db() as session:  # an unpaid release of the same clinic, an hour later
        appointment = Appointment(
            id=uuid4(),
            tenant_id=released.tenant_id,
            patient_id=released.patient.id,
            conversation_id=released.conversation.id,
            google_event_id="evt-unpaid",
            appointment_type="Consulta",
            status=AppointmentStatus.CANCELLED,
            start_at=released.appointment.start_at + timedelta(days=1),
            end_at=released.appointment.end_at + timedelta(days=1),
        )
        session.add(appointment)
        await session.flush()
        unpaid = PixDeposit(
            id=uuid4(),
            tenant_id=released.tenant_id,
            appointment_id=appointment.id,
            patient_id=released.patient.id,
            asaas_payment_id="pay_unpaid",
            amount_cents=6000,
            percent_applied=30,
            status=PixDepositStatus.AWAITING,
        )
        session.add(unpaid)
        tenant = await session.get(Tenant, released.tenant_id)
        await ro.open_offer(
            session, tenant=tenant, appointment=appointment, deposit=unpaid, now=T + timedelta(hours=1)
        )
        await session.commit()

    everything = (await client.get(OFFERS)).json()["items"]
    pending = (await client.get(OFFERS, params={"pending_only": "true"})).json()["items"]

    assert [i["patient_decision"] for i in everything] == ["not_offered", "no_answer"]
    assert [i["id"] for i in pending] == [str(paid.id)]


async def test_scope_follows_the_agenda(client: AsyncClient, db):
    released, mine = await _opened(db, professional_hours=CLOSES_AT_SIX)
    doctor = released.appointment.professional_id
    async with db() as session:  # another doctor's release in the same clinic
        tenant = await session.get(Tenant, released.tenant_id)
    _wire(db, released.tenant_id, viewer=AgendaViewer(scope=AGENDA_SCOPE_OWN, professional_id=doctor))
    assert [i["id"] for i in (await client.get(OFFERS)).json()["items"]] == [str(mine.id)]

    # A plain doctor never acts on another doctor's offer.
    _wire(db, released.tenant_id, viewer=AgendaViewer(scope=AGENDA_SCOPE_OWN, professional_id=uuid4()))
    assert (await client.get(OFFERS)).json()["items"] == []
    await _do(db, ro.choose_cancel, mine.id, T + timedelta(hours=1))
    response = await client.post(f"{OFFERS}/{mine.id}/refunded", json=BODY)
    assert response.status_code == 404

    # A manager who is also a doctor narrows with mine=true; without a professional it is 422.
    _wire(db, tenant.id, viewer=AgendaViewer(scope=AGENDA_SCOPE_CLINIC, professional_id=doctor))
    assert len((await client.get(OFFERS, params={"mine": "true"})).json()["items"]) == 1
    _wire(db, tenant.id, viewer=CLINIC_WIDE)  # a secretary: the whole clinic, no "mine"
    assert (await client.get(OFFERS, params={"mine": "true"})).status_code == 422


async def test_marking_refunded_is_idempotent_and_asks_the_patient_once(client: AsyncClient, db):
    released, offer = await _opened(db)
    await _do(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    _wire(db, released.tenant_id)  # any role - the secretary included

    first = await client.post(f"{OFFERS}/{offer.id}/refunded", json=BODY)
    second = await client.post(f"{OFFERS}/{offer.id}/refunded", json=BODY)

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json()["refund_status"] == "marked" == second.json()["refund_status"]
    async with db() as session:
        checks = list(
            await session.scalars(
                select(DepositReleaseEmail).where(DepositReleaseEmail.kind == "patient_check")
            )
        )
    assert len(checks) == 1


async def test_marking_is_refused_before_the_refund_is_due_or_while_keep_is_pending(
    client: AsyncClient, db
):
    released, offer = await _opened(db)
    _wire(db, released.tenant_id)
    response = await client.post(f"{OFFERS}/{offer.id}/refunded", json=BODY)
    assert (response.status_code, response.json()) == (409, {"detail": {"code": "refund_not_due"}})

    await _do(db, ro.request_keep, offer.id, T + timedelta(hours=1))
    response = await client.post(f"{OFFERS}/{offer.id}/refunded", json=BODY)
    assert response.status_code == 409  # keep_pending, or refund_not_due while nothing is due


async def test_accepting_keep_recreates_the_consultation_and_twice_is_the_same(
    client: AsyncClient, db
):
    released, offer = await _opened(db)
    await _do(db, ro.request_keep, offer.id, T + timedelta(hours=1))
    _wire(db, released.tenant_id)

    first = await client.post(f"{OFFERS}/{offer.id}/keep/accept", json=BODY)
    second = await client.post(f"{OFFERS}/{offer.id}/keep/accept", json=BODY)

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json()["decision"] == "kept" and first.json()["new_appointment_id"]
    assert first.json()["new_appointment_id"] == second.json()["new_appointment_id"]


async def test_accepting_without_a_request_or_refusing_twice(client: AsyncClient, db):
    released, offer = await _opened(db)
    _wire(db, released.tenant_id)
    response = await client.post(f"{OFFERS}/{offer.id}/keep/accept", json=BODY)
    assert (response.status_code, response.json()) == (409, {"detail": {"code": "not_requested"}})

    await _do(db, ro.request_keep, offer.id, T + timedelta(hours=1))
    first = await client.post(f"{OFFERS}/{offer.id}/keep/refuse", json=BODY)
    second = await client.post(f"{OFFERS}/{offer.id}/keep/refuse", json=BODY)
    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json()["decision"] == "keep_declined"
    assert first.json()["refund_status"] == "due" and first.json()["can_mark_refunded"] is True


async def test_another_clinics_offer_is_a_404_and_a_bad_body_is_never_echoed(
    client: AsyncClient, db
):
    released, offer = await _opened(db)
    await _do(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    other, _ = await _opened(db)
    _wire(db, other.tenant_id)
    for path in ("refunded", "keep/accept", "keep/refuse"):
        assert (await client.post(f"{OFFERS}/{offer.id}/{path}", json=BODY)).status_code == 404
    assert (await client.post(f"{OFFERS}/not-a-uuid/refunded", json=BODY)).status_code == 404

    _wire(db, released.tenant_id)
    response = await client.post(
        f"{OFFERS}/{offer.id}/refunded",
        json={"resolved_by_name": "Ana", "note": "Devolvido ao CPF 123.456.789-09 hoje"},
    )
    assert (response.status_code, response.json()) == (
        422,
        {"detail": {"code": "note_has_personal_data"}},
    )
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_offers_hub.py -q -p no:cacheprovider`
Expected: FAIL (404 em `/tenants/me/release-offers`).

- [ ] **Step 3: Implementar**

`src/secretaria/schemas/release_offers.py`:

```python
"""Wire shapes of the released-consultations list (TASK-052 -> TASK-054). A whitelist: the patient
is a display name; never a phone, an e-mail, a charge id or a payer."""

from datetime import datetime

from pydantic import BaseModel


class ReleaseOfferOut(BaseModel):
    id: str
    released_appointment_id: str
    new_appointment_id: str | None
    conversation_id: str | None
    patient_display_name: str | None
    professional_id: str | None
    service_label: str | None
    slot_start: datetime | None
    slot_end: datetime | None
    amount_cents: int
    deposit_paid_at_release: bool
    deposit_status: str  # the deposit's CURRENT status (PixDepositStatus value)
    refund_status: str  # not_due | due | marked | received | not_received | clinic_decided
    refund_source: str | None  # patient | timeout | keep_refused | keep_timeout
    # none | rescheduling | keep_requested | kept | keep_declined | rebooked | cancelled |
    # no_answer_cancelled | not_offered
    decision: str
    # no_answer | keep_requested | kept | keep_declined | rescheduled | cancelled |
    # cancelled_no_answer | received | not_received | not_offered
    patient_decision: str
    keep_deadline_at: datetime | None
    released_at: datetime
    decide_by_at: datetime
    expires_at: datetime
    first_contact_at: datetime | None
    refund_due_at: datetime | None
    refund_marked_at: datetime | None
    patient_answered_at: datetime | None
    can_mark_refunded: bool
    can_decide_keep: bool


class ReleaseOfferListOut(BaseModel):
    items: list[ReleaseOfferOut]
```

`src/secretaria/api/hub/release_offers.py`:

```python
"""Doctor hub - released consultations with a deposit (TASK-052; the screen is TASK-054).

    GET  /tenants/me/release-offers?pending_only=false&mine=false&limit=100
    POST /tenants/me/release-offers/{offer_id}/refunded
    POST /tenants/me/release-offers/{offer_id}/keep/accept
    POST /tenants/me/release-offers/{offer_id}/keep/refuse

Who (owner decision 2026-10-12): EVERY clinic role - secretary, manager, doctor - but only on what
it already sees in the agenda. The scope is the agenda's own (`get_agenda_viewer` ->
`services/agenda_visibility.py::AgendaViewer`, computed by brain-api `services/hub_scope.py`:
secretary and manager = the whole clinic, a plain doctor = his own; a manager who is also a doctor
narrows with `mine=true`, exactly like GET /calendar/events in api/hub/calendar.py). An offer
outside that scope, or of another clinic, is a 404 (never a 403). Bodies are parsed BY HAND (a
validation error would echo the note). Nothing here moves money or calls a provider (D15):
"refunded" records what the CLINIC did in the Asaas panel; accept/refuse decide the patient's
"Manter" request. Repeating any action is a 200 with the current state.
"""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_agenda_viewer, get_current_tenant
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Patient, PixDeposit, Tenant
from secretaria.models.deposit_release_offer import (
    DECISION_KEEP_DECLINED,
    DECISION_KEEP_REQUESTED,
    KEEP_OUTCOME_REFUSED,
    REFUND_DUE,
    REFUND_NOT_RECEIVED,
    SILENT_DECISIONS,
    DepositReleaseOffer,
)
from secretaria.schemas.release_offers import ReleaseOfferListOut, ReleaseOfferOut
from secretaria.services import release_keep, release_offer, release_offer_dispatch, reminder_hooks
from secretaria.services.agenda_visibility import AgendaViewer
from secretaria.services.payments import attention_view
from secretaria.services.payments.attention_view import NAME_MAX_CHARS, ResolveRefused

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me/release-offers", tags=["hub-release-offers"])

DEFAULT_LIMIT = 100
MAX_LIMIT = 200
MAX_BODY_BYTES = 4096
DEFAULT_NOTE = "Devolução registrada na lista de consultas liberadas."


def _detail(code: str, status_code: int) -> JSONResponse:
    return JSONResponse({"detail": {"code": code}}, status_code=status_code)


async def _rows(
    session: AsyncSession,
    tenant_id: UUID,
    viewer: AgendaViewer,
    *,
    own_only: bool = False,
    pending_only: bool = False,
    limit: int = DEFAULT_LIMIT,
    offer_id: UUID | None = None,
) -> list:
    narrowed = viewer.restricted or own_only
    if narrowed and viewer.professional_id is None:
        return []  # an "own" viewer without a professional reaches nothing
    query = (
        select(DepositReleaseOffer, Appointment, PixDeposit, Patient.name)
        .join(Appointment, Appointment.id == DepositReleaseOffer.released_appointment_id)
        .join(PixDeposit, PixDeposit.id == DepositReleaseOffer.deposit_id)
        .outerjoin(Patient, Patient.id == DepositReleaseOffer.patient_id)
        .where(
            DepositReleaseOffer.tenant_id == tenant_id,
            Appointment.tenant_id == tenant_id,
            PixDeposit.tenant_id == tenant_id,
        )
    )
    if narrowed:
        query = query.where(Appointment.professional_id == viewer.professional_id)
    if offer_id is not None:
        query = query.where(DepositReleaseOffer.id == offer_id)
    if pending_only:
        query = query.where(
            or_(
                DepositReleaseOffer.refund_status.in_((REFUND_DUE, REFUND_NOT_RECEIVED)),
                DepositReleaseOffer.decision.in_((*SILENT_DECISIONS, DECISION_KEEP_REQUESTED))
                & DepositReleaseOffer.deposit_paid.is_(True),
            )
        )
    query = query.order_by(DepositReleaseOffer.released_at.desc()).limit(limit)
    return (await session.execute(query)).all()


def _out(row) -> ReleaseOfferOut:
    offer, appointment, deposit, patient_name = row
    status = deposit.status.value if hasattr(deposit.status, "value") else str(deposit.status)
    return ReleaseOfferOut(
        id=str(offer.id),
        released_appointment_id=str(offer.released_appointment_id),
        new_appointment_id=str(offer.new_appointment_id) if offer.new_appointment_id else None,
        conversation_id=str(offer.conversation_id) if offer.conversation_id else None,
        patient_display_name=(patient_name or "").strip() or None,
        professional_id=str(appointment.professional_id) if appointment.professional_id else None,
        service_label=appointment.appointment_type,
        slot_start=appointment.start_at,
        slot_end=appointment.end_at,
        amount_cents=deposit.amount_cents,
        deposit_paid_at_release=offer.deposit_paid,
        deposit_status=status,
        refund_status=offer.refund_status,
        refund_source=offer.refund_source,
        decision=offer.decision,
        patient_decision=release_offer.patient_decision(offer),
        keep_deadline_at=offer.keep_deadline_at,
        released_at=offer.released_at,
        decide_by_at=offer.decide_by_at,
        expires_at=offer.expires_at,
        first_contact_at=offer.first_contact_at,
        refund_due_at=offer.refund_due_at,
        refund_marked_at=offer.refund_marked_at,
        patient_answered_at=offer.patient_answered_at,
        can_mark_refunded=offer.deposit_paid
        and offer.refund_status == REFUND_DUE
        and offer.decision != DECISION_KEEP_REQUESTED,
        can_decide_keep=offer.decision == DECISION_KEEP_REQUESTED,
    )


async def _body(request: Request) -> dict | None:
    raw = await request.body()
    if not raw:
        return {}
    if len(raw) > MAX_BODY_BYTES:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _name(data: dict) -> str | None:
    value = data.get("resolved_by_name")
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > NAME_MAX_CHARS:
        return None
    return value.strip()


async def _visible(session, tenant: Tenant, viewer: AgendaViewer, offer_id: str):
    """(offer uuid, row) when the offer exists in this clinic AND in this viewer's agenda."""
    try:
        offer_uuid = UUID(offer_id)
    except ValueError:
        return None, None
    rows = await _rows(session, tenant.id, viewer, offer_id=offer_uuid)
    return offer_uuid, (rows[0] if rows else None)


async def _answer(session, tenant: Tenant, viewer: AgendaViewer, offer_uuid: UUID) -> JSONResponse:
    (row,) = await _rows(session, tenant.id, viewer, offer_id=offer_uuid)
    return JSONResponse(_out(row).model_dump(mode="json"), status_code=200)


async def _dispatch_now(session, offer_uuid: UUID) -> None:
    try:  # what the action made due now (the patient's messages); the sweep is the retry
        await release_offer_dispatch.dispatch_due(session, offer_id=offer_uuid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("release_offer_dispatch_failed", error_type=type(exc).__name__)


@router.get("", response_model=ReleaseOfferListOut)
async def list_release_offers(
    pending_only: bool = Query(default=False),
    mine: bool = Query(default=False),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    tenant: Tenant = Depends(get_current_tenant),
    viewer: AgendaViewer = Depends(get_agenda_viewer),
    session: AsyncSession = Depends(get_session),
):
    if mine and not viewer.restricted and viewer.professional_id is None:
        return _detail("no_own_agenda", 422)  # same answer as GET /calendar/events?mine=true
    rows = await _rows(
        session, tenant.id, viewer, own_only=mine, pending_only=pending_only, limit=limit
    )
    return ReleaseOfferListOut(items=[_out(row) for row in rows])


@router.post("/{offer_id}/refunded")
async def mark_offer_refunded(
    offer_id: str,
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    viewer: AgendaViewer = Depends(get_agenda_viewer),
    session: AsyncSession = Depends(get_session),
):
    data = await _body(request)
    if data is None:
        return _detail("invalid_body", 422)
    try:
        _action, note, name = attention_view.check_resolution_input(
            release_offer.REFUNDED_ACTION,
            data.get("note") or DEFAULT_NOTE,
            data.get("resolved_by_name"),
        )
    except ResolveRefused as refused:
        return _detail(refused.code, 422)
    offer_uuid, row = await _visible(session, tenant, viewer, offer_id)
    if row is None:
        return _detail("not_found", 404)
    outcome = await release_offer.mark_refunded(
        session, tenant_id=tenant.id, offer_id=offer_uuid, note=note, resolved_by_name=name
    )
    if outcome in ("not_due", "keep_pending"):
        return _detail("refund_not_due" if outcome == "not_due" else "keep_pending", 409)
    if outcome == "not_found":
        return _detail("not_found", 404)
    if outcome == "marked":
        await _dispatch_now(session, offer_uuid)
    logger.info("release_offer_marked_refunded", outcome=outcome)
    return await _answer(session, tenant, viewer, offer_uuid)


@router.post("/{offer_id}/keep/accept")
async def accept_offer_keep(
    offer_id: str,
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    viewer: AgendaViewer = Depends(get_agenda_viewer),
    session: AsyncSession = Depends(get_session),
):
    data = await _body(request)
    name = _name(data or {})
    if data is None or name is None:
        return _detail("resolved_by_required", 422)
    offer_uuid, row = await _visible(session, tenant, viewer, offer_id)
    if row is None:
        return _detail("not_found", 404)
    offer = row[0]
    result = await release_keep.accept_keep(session, tenant=tenant, offer=offer, resolved_by=name)
    if result.outcome == release_keep.ACCEPT_UNAVAILABLE:
        return _detail(result.outcome, 503)
    if result.outcome not in (release_keep.ACCEPT_KEPT, release_keep.ACCEPT_ALREADY):
        return _detail(result.outcome, 409)
    if result.outcome == release_keep.ACCEPT_KEPT:
        await reminder_hooks.after_appointment_booked(result.appointment.id)
        await _dispatch_now(session, offer_uuid)
    return await _answer(session, tenant, viewer, offer_uuid)


@router.post("/{offer_id}/keep/refuse")
async def refuse_offer_keep(
    offer_id: str,
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    viewer: AgendaViewer = Depends(get_agenda_viewer),
    session: AsyncSession = Depends(get_session),
):
    data = await _body(request)
    name = _name(data or {})
    if data is None or name is None:
        return _detail("resolved_by_required", 422)
    offer_uuid, row = await _visible(session, tenant, viewer, offer_id)
    if row is None:
        return _detail("not_found", 404)
    offer = row[0]
    if offer.decision == DECISION_KEEP_DECLINED and offer.keep_outcome == KEEP_OUTCOME_REFUSED:
        return await _answer(session, tenant, viewer, offer_uuid)  # refused before: same answer
    if not await release_offer.decline_keep(
        session, offer=offer, outcome=KEEP_OUTCOME_REFUSED, resolved_by=name
    ):
        await session.rollback()
        return _detail("not_requested", 409)
    await session.commit()
    await _dispatch_now(session, offer_uuid)
    return await _answer(session, tenant, viewer, offer_uuid)
```

Em `src/secretaria/main.py`: no bloco de imports dos routers do hub (o que tem `payment_attention,`), adicione `release_offers,`; logo depois de `app.include_router(payment_attention.router)`, adicione `app.include_router(release_offers.router)`.

(`NAME_MAX_CHARS` já é o limite que `check_resolution_input` usa em `attention_view.py`.)

- [ ] **Step 4: Rodar e ver passar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_release_offers_hub.py tests/test_payment_attention_hub.py tests/test_payment_attention_roles.py tests/test_agenda_viewer.py tests/test_hub_conversations_my_patients.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/schemas/release_offers.py src/secretaria/api/hub/release_offers.py src/secretaria/main.py tests/test_release_offers_hub.py`
Expected: PASS (8), ruff limpo. (`test_agenda_viewer.py` e `test_hub_conversations_my_patients.py` provam que o escopo da agenda não mudou.)

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/schemas/release_offers.py src/secretaria/api/hub/release_offers.py src/secretaria/main.py tests/test_release_offers_hub.py
git commit -m "feat(hub): released-consultations contract for TASK-054 - list, mark refunded, accept/refuse keep, agenda scope for every role (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 12: B, T + 24 h, o prazo da clínica para "Manter" (K) e os envios na varredura de pagamentos

**Files:**
- Modify: `src/secretaria/workers/payments_cron.py` (`run_payment_sweeps`)
- Test: `tests/test_payment_cron_jobs.py` (1 novo)

**Interfaces:**
- Consumes: Task 3 (`expire_offers`), Task 5 (`dispatch_due`).
- Produces: `run_payment_sweeps` aplica B, E e K e envia o que está devido (cada um em sessão própria) e loga `release_offers=<n>`, `release_sends=<n>` em `payment_sweep_tick`. Nenhum cron novo (`test_build_identity.py` não muda). Atraso máximo ≈ 6 min (`SWEEP_MINUTES`).

- [ ] **Step 1: Escrever o teste**

Em `tests/test_payment_cron_jobs.py`, antes de `test_the_expiry_cron_wrapper_runs_the_job_with_its_own_session`:

```python
async def test_the_sweep_cron_runs_b_and_the_24h_cancellation_and_sends_once(db, monkeypatch):
    """TASK-052: B, E ("cancelado por falta de resposta") and the notices ride the sweep."""
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
            now=datetime.now(UTC) - timedelta(hours=25),  # B and E both passed, nobody answered
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
    assert (closed.decision, closed.refund_status) == ("no_answer_cancelled", "due")
    assert [item.reason for item in items] == ["released_refund_due"]
    assert sends == {"clinic_refund_noanswer": "sent", "clinic_refund_day": "sent"}
    assert len(mailbox) == 2 and any("Prazo vencido" in subject for subject in mailbox)


async def test_the_sweep_cron_closes_a_keep_request_the_clinic_never_decided(db, monkeypatch):
    """TASK-052: no clinic decision 24 h after the "Manter" tap = refused; the clinic is told."""
    from secretaria.config import get_settings
    from secretaria.models.deposit_release_offer import DepositReleaseEmail, DepositReleaseOffer
    from secretaria.services import email, release_offer
    from tests.release_offer_support import make_released

    monkeypatch.setattr(payments_cron, "async_session_factory", db)
    mailbox: list[str] = []
    monkeypatch.setattr(email, "_send_sync", lambda to, subject, body: mailbox.append(subject))
    monkeypatch.setattr(get_settings(), "SMTP_HOST", "smtp.exemplo", raising=False)
    now = datetime.now(UTC)
    async with db() as session:
        released = await make_released(session, business_hours={}, start=now + timedelta(days=3))
        offer = await release_offer.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=now - timedelta(hours=26),
        )
        await release_offer.request_keep(session, offer=offer, now=now - timedelta(hours=25))
        await session.commit()

    await payments_cron.run_payment_sweeps({"redis": FakeRedis()})

    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        sends = {
            (e.kind, e.seq): e.outcome for e in await session.scalars(select(DepositReleaseEmail))
        }
    assert (row.decision, row.keep_outcome, row.refund_status) == (
        "keep_declined",
        "timeout",
        "due",
    )
    assert sends[("clinic_keep_declined", 0)] == "sent"
    assert any("não foi mantido" in subject for subject in mailbox)
```

(`FakeRedis`, `select`, `datetime`, `UTC`, `timedelta` e `payments_cron` já são usados neste arquivo.)

- [ ] **Step 2: Rodar e ver falhar**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest tests/test_payment_cron_jobs.py -q -p no:cacheprovider`
Expected: FAIL (a oferta continua `none`/`not_due`, nenhum item, nenhum aviso).

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
+    # TASK-052: a slot the clinic released with a PAID deposit - B (refund due when nobody
+    # answered), E (24 h: "cancelado por falta de resposta"), K (no clinic decision on a
+    # "Manter" request in 24 h = refused), then whatever is due (the clinic's
+    # notices, the patient's refund check). Own sessions: a failure here never touches the sweeps
+    # above.
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
git commit -m "feat(payments): the payment sweep runs the B, 24h and keep-decision deadlines and the released-slot notices (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 13: As corridas em Postgres real (opcional, mas recomendada)

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

* N "Cancelar" taps + N runs of the B sweep on ONE offer -> exactly one tap cancels, the refund
  falls due once: ONE attention item, ONE cancel notice, whatever the sweep did;
* N booking commits racing the sweep on an offer in `rescheduling` -> the deposit moves to exactly
  one new appointment and no refund item is left open (a refund B made due is undone);
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
            outcome = await release_offer.choose_cancel(session, offer=row)
            await session.commit()
            return outcome == release_offer.CANCEL_NOTIFIED

    async def sweep():
        await asyncio.sleep(1.5)
        async with maker() as session:
            return await release_offer.expire_offers(session)

    results = await asyncio.gather(*[tap() for _ in range(WORKERS)], *[sweep() for _ in range(WORKERS)])

    taps = results[:WORKERS]
    assert sum(1 for won in taps if won) == 1  # one "Cancelar" wins; the sweep may or may not
    async with maker() as session:
        items = await session.scalar(select(func.count()).select_from(PaymentAttentionItem))
        row = await session.get(DepositReleaseOffer, offer.id)
        cancel_rows = await session.scalar(
            select(func.count())
            .select_from(DepositReleaseEmail)
            .where(DepositReleaseEmail.kind == "clinic_refund_cancel")
        )
    assert items == 1 and cancel_rows == 1
    assert (row.decision, row.refund_status) == ("cancelled", "due")


async def test_bookings_racing_the_sweep_carry_exactly_once_and_leave_no_refund_open(maker):
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

    # Owner rule 2026-10-12: "Remarcar" carries the deposit even after B (the refund that B made
    # due is undone) - so exactly ONE booking carries, whatever the sweep did, and no refund item
    # is left open for money that is in use.
    assert sum(1 for won in results[:WORKERS] if won) == 1
    async with maker() as session:
        open_items = await session.scalar(
            select(func.count())
            .select_from(PaymentAttentionItem)
            .where(PaymentAttentionItem.status == "open")
        )
        deposit = await session.get(PixDeposit, released.deposit.id)
        row = await session.get(DepositReleaseOffer, offer.id)
    assert (row.decision, row.refund_status) == ("rebooked", "not_due") and open_items == 0
    assert deposit.appointment_id == row.new_appointment_id


async def test_concurrent_dispatchers_send_one_email(maker, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(email, "_send_sync", lambda to, subject, body: sent.append(subject))
    monkeypatch.setattr(get_settings(), "SMTP_HOST", "smtp.exemplo", raising=False)
    _released, offer = await _offer(maker, released_hours_ago=1)
    async with maker() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.choose_cancel(session, offer=row) == release_offer.CANCEL_NOTIFIED
        await session.commit()

    async def dispatch():
        async with maker() as session:
            return (await release_offer_dispatch.dispatch_due(session)).sent

    results = await asyncio.gather(*[dispatch() for _ in range(WORKERS)])

    assert sum(results) == 1 and len(sent) == 1  # only the cancel notice was due
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

### Task 14: Brain-Message-Frontend — o texto do aviso de liberar com sinal pago

O cartão "Liberar horário" da agenda diz hoje que liberar "pode fazer a clínica reter o valor pago" — falso depois das Tasks 3–12. Só texto; o pedido (`acknowledge_retention`) não muda de nome.

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
  it("names the patient's three choices and the refund notices, and never says the clinic keeps it", () => {
    expect(RELEASE_COPY.ackBody).toContain("pedir para manter o horário (a clínica decide)");
    expect(RELEASE_COPY.ackBody).toContain("remarcar usando o mesmo sinal");
    expect(RELEASE_COPY.ackBody).toContain("cancelar");
    expect(RELEASE_COPY.ackBody).toContain("avisos para devolver");
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
// choice (secretarIA TASK-052, services/release_offer.py): ask to keep the slot (the clinic decides), reschedule
// with the same deposit, or cancel. Cancelling - or no answer by the deadline - makes
// the clinic receive notices to return the deposit in the payment provider's panel
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
    "Ao liberar o horário, o paciente poderá pedir para manter o horário (a clínica decide), remarcar usando o mesmo sinal, sem nova cobrança, ou cancelar. Se ele cancelar ou não responder até o prazo, a clínica recebe avisos para devolver o sinal por inteiro pelo painel do provedor de pagamento.",
  ackLabel: "Entendo que o sinal pago pode ter de ser devolvido",
```

- [ ] **Step 4: Rodar e ver passar**

Run: `npx vitest run lib/agenda components/agenda ; npm run typecheck`
Expected: PASS, typecheck limpo.

- [ ] **Step 5: Commit**

```powershell
git add lib/agenda/release.ts lib/agenda/__tests__/release.test.ts
git commit -m "fix(agenda): the paid-release warning describes keep/reschedule/cancel and the refund notices, not a retention (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 15: Gates, modelos Meta documentados, CHECKPOINT e registro

**Files:**
- Create: `docs/CHECKPOINT_task052_liberar_sinal_pago.md`
- Modify: `docs/LEMBRETES_MODELOS_META.md` (seção TASK-052), `CLAUDE.md` (1 linha), `C:\TECH\BRAIN\tasks\TASK-052\TASK.md` (fora do repositório)

- [ ] **Step 1: Suíte inteira e lint de tudo o que foi tocado**

Run: `$env:BOT_ALLOWLIST_WA_IDS = ""; uv run python -m pytest -q -p no:cacheprovider ; uv run ruff check src/secretaria/models/deposit_release_offer.py src/secretaria/models/__init__.py src/secretaria/models/payment_attention_item.py migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py src/secretaria/config.py src/secretaria/main.py src/secretaria/services/email.py src/secretaria/services/release_deadlines.py src/secretaria/services/release_offer.py src/secretaria/services/release_keep.py src/secretaria/services/release_offer_dispatch.py src/secretaria/services/brain_staff.py src/secretaria/services/payments/attention_view.py src/secretaria/api/hub/calendar.py src/secretaria/api/hub/release_offers.py src/secretaria/schemas/release_offers.py src/secretaria/services/appointment_release.py src/secretaria/workers/whatsapp/notifications.py src/secretaria/services/availability.py src/secretaria/services/flow_router.py src/secretaria/workers/shared/release_offer_actions.py src/secretaria/workers/shared/actions.py src/secretaria/schemas/webhook.py src/secretaria/workers/portal/inbound.py src/secretaria/workers/portal/open.py src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/shared/booking_hold.py src/secretaria/services/booking_hold.py src/secretaria/workers/payments_cron.py tests/release_offer_support.py tests/test_deposit_release_offer_migration.py tests/test_release_deadlines.py tests/test_release_offer.py tests/test_release_keep.py tests/test_release_offer_dispatch.py tests/test_brain_staff.py tests/test_release_offer_buttons.py tests/test_release_offer_carry_over.py tests/test_release_offers_hub.py tests/test_release_offer_postgres.py tests/test_deposit_before_migration.py tests/test_payment_attention.py tests/test_hub_release.py tests/test_cancellation_notice.py tests/test_availability.py tests/test_before_booking_flow.py tests/test_payment_cron_jobs.py`
Expected: linha de base da Task 0 + os testes novos, **0 failed**; ruff limpo. Falha que também falha na linha de base é pré-existente (registre); qualquer outra é regressão (corrija antes de seguir).

- [ ] **Step 2: Documentar os dois modelos pendentes na Meta**

No fim de `docs/LEMBRETES_MODELOS_META.md`:

````markdown
## TASK-052 — horário liberado com sinal pago (PENDÊNCIA META, 2026-10-12)

Decisão do dono: o código já usa os dois modelos como se aprovados (`config.py`
`RELEASE_OFFER_TEMPLATE_NAME`, `REFUND_CHECK_TEMPLATE_NAME`). Enquanto a Meta não aprovar, o envio
fora da janela de 24 h falha e fica registrado; os avisos à clínica seguem. Categoria: Utilidade.
Idioma: pt_BR. Um parâmetro no corpo, sem quebras de linha.

### `liberacao_sinal_pago_v1`

Corpo: `{{1}}`. Botões de resposta rápida: "Manter horário", "Remarcar", "Cancelar". Decisão do dono
(2026-10-12): este modelo é reaproveitado em toda mensagem da oferta fora da janela (liberação, resposta
da clínica ao "Manter"), inclusive depois do início da consulta — "Manter horário" tocado depois do
início responde com uma mensagem nova só com "Remarcar" e "Cancelar".
Exemplo de `{{1}}`: "Devido à sua não confirmação da ida à consulta, a clínica liberou o seu horário para que você decida o que fazer. Você já pagou o sinal desta consulta (R$ 60,00). Toque em *Manter horário* para ficar com o mesmo horário, em *Remarcar* para escolher outro dia e horário usando o mesmo sinal, sem nova cobrança, ou em *Cancelar* para cancelar a consulta e receber o sinal de volta. Se você não responder até 20:00 de 05/03, a clínica será avisada para devolver o seu sinal."

### `devolucao_sinal_conferir_v1`

Corpo: `{{1}}`. Botões de resposta rápida: "Recebido", "Não recebido".
Exemplo de `{{1}}`: "A clínica informou que devolveu o seu sinal de R$ 60,00. Confira a sua conta (a devolução vai para a conta de onde saiu o Pix) e nos conte:"
````

- [ ] **Step 3: Criar o CHECKPOINT**

`docs/CHECKPOINT_task052_liberar_sinal_pago.md` (as medições `«…»` vêm do Step 1 e da Task 13):

````markdown
# CHECKPOINT — Liberar horário com sinal pago: manter, remarcar ou cancelar (TASK-052)

**Estado:** construído e commitado em `task/TASK-052-liberar-sinal-pago` (worktree
`C:\TECH\BRAIN-worktrees\TASK-052\secretarIA`), último commit «SHA»; front em `task/TASK-052-aviso-liberar` «SHA».
**Não pushado, não deployado.** Integra em `feature/out-of-mvp` (nunca `main`), depois da TASK-050.
Spec: `docs/superpowers/specs/2026-10-11-liberar-horario-com-sinal-pago-design.md`. Plano:
`docs/superpowers/plans/2026-10-11-task-052-liberar-horario-sinal-pago.md`.

## 1. O que é
Liberar uma consulta com sinal PAGO abre uma oferta. Manter horário (até o início da consulta) é um PEDIDO à clínica,
que aceita (a consulta volta, se o horário estiver livre) ou recusa em até 24 h do toque; sem decisão = recusa.
Remarcar a qualquer momento leva o sinal se a clínica ainda não marcou devolvido (e a clínica recebe "REMARCADA levando
o sinal"); senão cobra de novo. Cancelar avisa a clínica NA HORA com o passo a passo do estorno no Asaas. Sem resposta:
aviso em B = min(T + 6 h, fechamento − 30 min; fechamento da clínica se o brain-api diz que há secretária, senão o do
médico); em T + 24 h "Cancelado por falta de resposta" e "prazo vencido, devolva agora". A clínica (qualquer papel, no
escopo da agenda) marca "devolvido" e o paciente responde Recebido / Não recebido. Portal: cartão + e-mail com link;
WhatsApp: só mensagem. O sistema nunca estorna (D15).

## 2. Onde está cada coisa
| Camada | Arquivo |
|---|---|
| Tabelas + migração + `tenants.has_secretary` | `models/deposit_release_offer.py`, `models/tenant.py`, `migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py` |
| Prazos | `services/release_deadlines.py` |
| Regras e textos | `services/release_offer.py` |
| Aceitar "Manter" | `services/release_keep.py` |
| Papéis (brain-api) | `services/brain_staff.py` (depende de `GET /internal/tenants/{id}/staff-roles`) |
| Envios | `services/release_offer_dispatch.py`, `services/email.py`, `config.py` |
| Liberar / Portal | `api/hub/calendar.py::release_appointment`, `services/appointment_release.py` |
| Aba (TASK-054) | `api/hub/release_offers.py` (lista, devolvido, aceitar/recusar Manter; escopo da agenda), `schemas/release_offers.py`; gancho em `services/payments/attention_view.py::resolve_item` |
| WhatsApp | `workers/whatsapp/notifications.py::send_cancellation_notice` |
| Toques | `workers/shared/release_offer_actions.py`, `workers/shared/actions.py`, `schemas/webhook.py`, `workers/portal/inbound.py`, `workers/portal/open.py` |
| Transferência | `workers/shared/flow_runner.py`, `workers/shared/booking_hold.py`, `services/booking_hold.py` |
| B, T + 24 h, K, envios | `workers/payments_cron.py::run_payment_sweeps` |
| Dias segurados | `services/flow_router.py::_enter_slot_picker`, `services/availability.py::held_slots` |

## 3. Decisões e perguntas
Decisões padrão: spec §10. Perguntas ao dono: spec §11. Modelos Meta: `docs/LEMBRETES_MODELOS_META.md` (TASK-052).

## 4. Validação
Suíte «N» passed / «S» skipped / «X» xfailed / 0 failed (linha de base «B»); ruff limpo; Postgres real: «PROVADO / NÃO PROVADO».
**NÃO PROVADO:** WhatsApp real (modelos não aprovados), SMTP real, cartão no Portal real, produção.

## 5. Deploy (NÃO autorizado)
Migração `b6d2e8f4a1c3` → API → worker LOGO depois → Brain-Message-Frontend. `BRAIN_MESSAGE_PORTAL_URL` na API e no worker.
````

- [ ] **Step 4: Ponteiro no `CLAUDE.md`**

Na seção "## Documentação" do `CLAUDE.md` do secretarIA, como primeiro parágrafo:

```markdown
`docs/CHECKPOINT_task052_liberar_sinal_pago.md` — TASK-052: liberar horário com sinal pago dá ao paciente Manter (pedido; a clínica aceita/recusa em 24 h) / Remarcar (a qualquer momento; leva o sinal se não marcado devolvido) / Cancelar (avisa a clínica na hora com o passo a passo do Asaas); sem resposta, aviso em B e "cancelado por falta de resposta" em T+24h; contrato da aba da TASK-054 (todos os papéis, escopo da agenda); depende de `staff-roles` do brain-api (fallback: horário do médico); modelos Meta PENDENTES; integra em `feature/out-of-mvp`; **construído, NÃO deployado**.
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
| §1 Intenção e critérios 1–6 | 1 → Tasks 3, 6, 7; 2 → Tasks 3, 4, 9, 11; 3 → Tasks 3, 5, 9; 4 → Tasks 3, 5, 10; 5 → Tasks 3, 5, 12; 6 → Tasks 2, 3, 5, 9, 11, 13 |
| §2 / §3 | Tasks 1–14 / não tocados (sem estorno, sem tela, sem endpoint do brain-api, sem aprovação Meta) |
| §5.1 Oferta e transições | Tasks 1, 3, 4 |
| §5.2 Prazos e "tem secretária" (brain-api + fallback) | Tasks 2, 3, 6 |
| §5.3 Mensagem em T, canais | Tasks 3, 6, 7 |
| §5.4 Manter por opção da clínica (pausa, aceitar, recusar, K, Portal depois de S) | Tasks 3, 4, 5, 9, 11, 12 |
| §5.5 Remarcar a qualquer momento; nova cobrança se marcado; e-mail "REMARCADA" | Tasks 3, 5, 9, 10 |
| §5.6 Cancelar | Tasks 3, 5, 9 |
| §5.7 B, E, K | Tasks 3, 9, 12 |
| §5.8 Avisos à clínica | Task 5 |
| §5.9 Avisos ao paciente; marcar devolvido / Recebido / Não recebido | Tasks 3, 5, 9, 11 |
| §5.10 Contrato da aba (escopo da agenda, todos os papéis) | Task 11 |
| §5.11 Dias segurados | Task 8 |
| §5.12 Modelos Meta | Tasks 5, 7, 15 |
| §5.13 Envio idempotente | Tasks 1, 5, 13 |
| §6 Combinações | Tasks 3, 4, 5, 9, 10, 11 (um teste por linha) |
| §7 Corridas | Review Focus; Tasks 3, 4, 10, 13 |
| §9 Dependências e deploy | Estado de execução, Global Constraints, Tasks 6, 15 |

**2. Varredura de placeholders.** Nenhum "TBD"/"TODO"/"similar à Task N". Os `«»` do CHECKPOINT são medições; `<porta>` é a de um Postgres descartável. O plano NÃO foi aplicado numa cópia descartável (dito no topo); as notas de "se o texto diferir" (Task 6) dizem o que ajustar.

**3. Consistência de nomes.** `rel*` iguais em `release_offer.py`, `webhook.py`, `inbound.py`, hub, job e testes; decisões/devolução/kinds vêm todos de `models/deposit_release_offer.py`; `keep_allowed` (hub, job), `request_keep`/`claim_keep`/`decline_keep`/`accept_keep` (serviço, hub, toques, varredura), `choose_reschedule` → `RESCHEDULE_*` (toques), `still_relevant` (despacho), `patient_decision` (aba) e `fetch_has_secretary` (hub release) com as mesmas assinaturas em todas as tasks.

**4. Review Focus.** Os 5 itens têm teste nomeado na task dona.
