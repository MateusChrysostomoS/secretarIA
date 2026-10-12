# Liberar horário com sinal pago: devolver ou remarcar (TASK-052) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Quando a clínica libera uma consulta com sinal já pago, o paciente recebe "Receber agora" / "Remarcar": a devolução vira UM item de atenção para a clínica (nunca devolução automática), o silêncio por 24 horas abre o mesmo item, e "Remarcar" leva o sinal pago para a nova consulta; e a lista de dias mostra os dias com horários reservados por Pix ainda não pago, dizendo quais.

**Architecture:** Uma tabela nova (`deposit_release_offers`, uma oferta por consulta liberada) e um serviço puro de transições guardadas (`services/release_offer.py`: abrir, devolver, remarcar, transferir, expirar — cada uma um `UPDATE ... WHERE state IN (...)`). A liberação do hub abre a oferta no lugar do `on_appointment_cancelled`; o job de aviso do WhatsApp (mesma assinatura) e o aviso do Portal trocam os botões; os toques entram por `_handle_action_button` (WhatsApp e Portal); a transferência acontece dentro da transação que cria a nova consulta; o relógio de 24 h é um passo da varredura de pagamentos que já existe. A lista de dias volta a não descontar reservas, e a escolha do dia diz quais horários estão reservados.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Alembic, arq, pytest (`asyncio_mode = "auto"`, SQLite em memória; Postgres real opcional por `PAYMENTS_TEST_PG_URL`), ruff. Front: Next.js/TypeScript/vitest (só um texto).

**Spec:** `docs/superpowers/specs/2026-10-11-liberar-horario-com-sinal-pago-design.md` (este worktree). Regras-mãe que continuam valendo: D15 (a plataforma nunca devolve), `docs/CHECKPOINT_task050_main_into_out_of_mvp.md` (estrutura pós-merge dos workers), `tests/test_workers_layering.py` (camadas), skill `frozen-contract-migration` (API e worker com deploy separado).

**Estado de execução (leia antes de começar):**

- **Worktree (já criado pelo planejador):** `C:\TECH\BRAIN-worktrees\TASK-052\secretarIA`, branch `task/TASK-052-liberar-sinal-pago`, base `task/TASK-050-secretaria` @ `3650e10`. Integra em `feature/out-of-mvp` (NUNCA `main`) depois da TASK-050.
- **Todo o código deste plano foi aplicado e verificado na escrita** sobre uma cópia descartável de `3650e10`: linha de base **6103 passed, 79 skipped, 1 xfailed, 0 failed**; com as Tasks 1–8: **6139 passed, 79 skipped, 1 xfailed, 0 failed** (+36); com a Task 9 (Postgres, opcional) os 2 testes novos ficam `skipped` sem `PAYMENTS_TEST_PG_URL` e passaram 3 vezes seguidas num Postgres 18 descartável, junto com os testes de migração em Postgres que já existiam (19 passed: ida e volta da cabeça nova sem diferença de modelo); `ruff check` limpo em todo arquivo tocado. A Task 10 (front) foi aplicada numa cópia descartável do Brain-Message-Frontend: `tsc --noEmit` limpo, `lib/agenda` + `components/agenda` 181 testes verdes.
- **NÃO PROVADO na escrita:** envio real pelo WhatsApp (cartão e modelo), o cartão desenhado no Portal real, produção.
- **Produção:** NÃO AUTORIZADA. Sem push, sem deploy, sem migração em banco real, sem `.env`, sem Easypanel.

---

## Contratos tocados

| # | Onde | Antes | Depois |
|---|---|---|---|
| 1 | `POST /tenants/me/calendar/appointments/{id}/release` (hub) — 409 `retention_ack_required` | `deposit_outcome` = `retained`/`partial_refund_due`/`refund_due` conforme a política; texto de retenção | `deposit_outcome` = `refund_offered` sempre que o sinal está PAGO; texto da oferta (remarcar / devolver; 24 h) |
| 2 | o mesmo, 200 | sinal pago podia ficar `cancelado_retido` ou abrir item | sinal continua PAGO; abre uma oferta; `deposit_outcome = "refund_offered"`; nenhum item ainda |
| 3 | job arq `send_cancellation_notice(tenant_id, appointment_id, professional_name, justification, extra_notice, allow_paid)` | 3 botões de remarcação | **mesma assinatura**; com oferta aberta e dentro da janela: botões `relrefund|<id>` "Receber agora", `relresched|<id>` "Remarcar" |
| 4 | ids de botão (`schemas/webhook.py::_ACTION_BUTTON_PREFIXES`) | — | `relrefund|`, `relresched|` (id = consulta LIBERADA) |
| 5 | Portal: ids aceitos como ação (`workers/portal/inbound.py`) | só a família dos lembretes | + `relrefund`, `relresched` (só se um cartão recente da conversa os ofereceu) |
| 6 | `payment_attention_items.reason` | 10 motivos | + `released_refund_due` (sem e-mail novo) |
| 7 | Banco | cabeça `e2b7c4a9d1f5` | + `b6d2e8f4a1c3` (tabela `deposit_release_offers`; aditiva) |
| 8 | Brain-Message-Frontend `lib/agenda/release.ts` (texto do aviso) | "pode fazer a clínica reter o valor pago" | remarcar com o mesmo sinal ou devolver; 24 h; a clínica devolve |

## Global Constraints

- **Dinheiro (D15):** a plataforma NUNCA devolve sozinha; "devolver" = UM item `released_refund_due` (valor cheio) por sinal, para a clínica devolver no painel do provedor. Nenhuma frase promete devolução automática; a clínica "fará a devolução em até 24 horas".
- **Uma oferta por consulta liberada** (UNIQUE); cada transição é um `UPDATE` guardado (`WHERE state IN (...)`); prazo = 24 h da liberação, ou 24 h do PRIMEIRO toque em "Remarcar".
- **Transferência:** reaponta o sinal PAGO para a consulta nova, na MESMA transação que a cria; sem nova cobrança; `reschedule_count` não é lido nem incrementado.
- **Isolamento:** toda consulta usa a clínica da conversa (toque), da consulta (hub) ou da própria oferta (relógio); um toque só vale para o paciente da oferta; nada de payload decide a clínica.
- **Textos (pt-BR, exatos, §5 da spec):** botões "Receber agora" (13) e "Remarcar" (8) ≤ 20; corpo interativo ≤ 1024 cortando a justificativa, nunca a explicação; aviso de reservas só com horários (até 8, depois "e mais N"), nunca dado de outro paciente.
- **Camadas (`tests/test_workers_layering.py`):** `workers/shared/*` não importa `whatsapp/*`, `portal/*` nem `tasks`; serviços novos em `services/`.
- **Deploy (NÃO autorizado; skill frozen-contract-migration):** migração aditiva `b6d2e8f4a1c3` → API → worker logo depois → front. O job não muda de assinatura (worker velho + API nova = cartão velho com o texto novo, por minutos).
- **Gates:** `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest <arquivo> -q -p no:cacheprovider` (no Windows, `PYTHONPATH` = `src` do worktree com caminho `C:/...` quando rodar fora do `uv run` padrão); `uv run ruff check <arquivos tocados>`; **nunca** `ruff format .`. Front: `npx vitest run <arquivo>`, `npm run typecheck` (sem `npm run lint`).
- **Git:** nunca `git add -A`/`git add .`; commits em inglês terminando com `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` e `Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG`.

## Review Focus

1. **O relógio e um toque (ou o fechamento da nova consulta) ganham os dois** — dois itens de devolução, ou o sinal transferido E devolvido. → Task 2: `test_a_tap_that_loses_to_the_timer_opens_no_second_item`, `test_the_booking_commit_carries_the_paid_deposit_over_exactly_once`; Task 7: `test_a_booking_after_the_sweep_refunded_stands_on_its_own`; Task 9 (Postgres real): `test_taps_and_sweeps_on_one_offer_open_exactly_one_item`, `test_bookings_racing_the_sweep_carry_or_refund_never_both`.
2. **Liberar ainda retém o sinal pago pela política do paciente.** → Task 3: `test_a_paid_deposit_needs_the_acknowledgement_and_the_patient_gets_the_choice[2-total|2-partial|96-total]`, `test_with_the_acknowledgement_the_offer_opens_and_nothing_is_decided_yet`.
3. **Um toque de outro paciente/outra clínica mexe na oferta.** → Task 6: `test_another_patients_tap_is_an_unknown_appointment_and_changes_nothing`, `test_another_clinics_offer_is_never_reached`, `test_a_portal_id_never_shown_on_a_card_is_not_an_action`; Task 2: `test_a_deposit_of_another_clinic_never_gets_an_offer`, `test_another_clinic_or_a_closed_offer_carries_nothing`.
4. **A nova consulta cobra de novo (Pix no "antes", cobrança no "depois") apesar do sinal pago.** → Task 7: `test_the_before_gate_asks_no_new_pix_while_the_deposit_is_carried`, `test_the_new_booking_carries_the_paid_deposit_and_says_so`, `test_the_portal_booking_born_at_the_code_carries_it_too`.
5. **O aviso de horários reservados mostra dado de outro paciente, ou o dia todo reservado prende o paciente num laço.** → Task 5: `test_a_fully_held_day_is_offered_and_says_which_times_are_held`, `test_a_partly_held_day_lists_the_free_times_under_the_held_ones`.

---

## File Structure

**Criar:** `src/secretaria/models/deposit_release_offer.py` (a tabela e seus estados); `migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py`; `src/secretaria/services/release_offer.py` (transições, textos, botões, relógio); `src/secretaria/workers/shared/release_offer_actions.py` (os toques, neutro de canal); testes `tests/release_offer_support.py` (ajudante), `tests/test_deposit_release_offer_migration.py`, `tests/test_release_offer.py`, `tests/test_release_offer_buttons.py`, `tests/test_release_offer_carry_over.py`, `tests/test_release_offer_postgres.py`.

**Modificar:** `src/secretaria/models/{__init__,payment_attention_item}.py`; `services/payments/attention_view.py`; `api/hub/calendar.py` (liberar); `services/appointment_release.py` (Portal); `workers/whatsapp/notifications.py` (job); `schemas/webhook.py`, `workers/portal/inbound.py`, `workers/shared/actions.py` (toques); `services/flow_router.py` (lista de dias, aviso, prefixo do "Outro horário"); `services/availability.py` (`held_slots`); `services/booking_hold.py` (portão); `workers/shared/{flow_runner,booking_hold}.py` (transferência); `workers/payments_cron.py` (relógio); testes `tests/{test_deposit_before_migration,test_payment_attention,test_hub_release,test_cancellation_notice,test_availability,test_before_booking_flow,test_payment_cron_jobs}.py`; `docs/CHECKPOINT_task052_liberar_sinal_pago.md` (novo) + 1 linha no `CLAUDE.md`.

**Brain-Message-Frontend (Task 10, worktree próprio):** `lib/agenda/release.ts`, `lib/agenda/__tests__/release.test.ts`.

**Fora dos repositórios:** `C:\TECH\BRAIN\tasks\TASK-052\TASK.md` (já criado pelo planejador; atualize).

**Nunca modificar:** `.env`, `Dockerfile`, `workers/tasks.py` (fachada), brain-api, PreCheck.

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
git log --oneline -1
git grep -n "async def release_appointment" -- src/secretaria/api/hub/calendar.py
git grep -n "async def send_cancellation_notice" -- src/secretaria/workers/whatsapp/notifications.py
git grep -n "async def _handle_action_button" -- src/secretaria/workers/shared/actions.py
git grep -n "reserved = await _hold_windows" -- src/secretaria/services/flow_router.py
git grep -n 'revision: str = "e2b7c4a9d1f5"' -- migrations/versions
```

Expected: `git status` limpo (só os dois documentos desta tarefa, já commitados); topo = o commit dos documentos sobre `3650e10`; cada `git grep` acha linha(s) (a de `_hold_windows` acha DUAS: a da lista de dias e a da lista de horários). Se a base andou, aplique os diffs pelas âncoras (nomes de função), não pelos números de linha, e registre no `TASK.md`.

- [ ] **Step 2: Ambiente e linha de base**

```powershell
uv sync --frozen
$env:BOT_ALLOWLIST_WA_IDS = ""
uv run python -m pytest -q -p no:cacheprovider
```

Expected: **6103 passed, 79 skipped, 1 xfailed, 0 failed** (medido na escrita sobre `3650e10`). Anote o número real no `TASK.md`.

---

### Task 1: A tabela `deposit_release_offers` e a migração aditiva

**Files:**
- Create: `src/secretaria/models/deposit_release_offer.py`, `migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py`
- Modify: `src/secretaria/models/__init__.py`, `tests/test_deposit_before_migration.py` (a cabeça agora é a nova)
- Test: `tests/test_deposit_release_offer_migration.py` (novo)

**Interfaces:**
- Produces: `DepositReleaseOffer` (colunas `id, tenant_id, deposit_id, released_appointment_id (UNIQUE), patient_id, conversation_id, state, offered_at, deadline_at, closed_at, refund_source, rebooked_appointment_id, created_at`); constantes `STATE_OPEN="open"`, `STATE_RESCHEDULING="rescheduling"`, `STATE_REFUND="refund"`, `STATE_REBOOKED="rebooked"`, `OFFER_STATES`, `OPEN_OFFER_STATES`, `REFUND_SOURCE_PATIENT="patient"`, `REFUND_SOURCE_TIMEOUT="timeout"`; revisão `b6d2e8f4a1c3` (desce de `e2b7c4a9d1f5`).

- [ ] **Step 1: Escrever o teste que falha**

```python
"""Migration b6d2e8f4a1c3 - `deposit_release_offers` (TASK-052).

Cheapest layers first, like tests/test_deposit_before_migration.py: the chain stays a single
head right after the `before` timing columns; the offline SQL only CREATES (one table, its
indexes) and its downgrade only drops that table; the model declares exactly the same shape.
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


def test_upgrade_only_creates_the_new_table():
    sql = _offline("up").upper()
    assert "CREATE TABLE DEPOSIT_RELEASE_OFFERS" in sql
    assert "ALTER TABLE" not in sql  # no existing table is touched
    assert "DROP " not in sql
    assert "UQ_DEPOSIT_RELEASE_OFFERS_RELEASED_APPOINTMENT_ID" in sql
    assert "IX_DEPOSIT_RELEASE_OFFERS_STATE_DEADLINE" in sql


def test_downgrade_drops_only_that_table():
    sql = _offline("down").upper()
    assert "DROP TABLE DEPOSIT_RELEASE_OFFERS" in sql
    assert "ALTER TABLE" not in sql


def test_the_model_declares_what_the_migration_creates():
    table = Base.metadata.tables["deposit_release_offers"]
    assert {c.name for c in table.columns} == {
        "id",
        "tenant_id",
        "deposit_id",
        "released_appointment_id",
        "patient_id",
        "conversation_id",
        "state",
        "offered_at",
        "deadline_at",
        "closed_at",
        "refund_source",
        "rebooked_appointment_id",
        "created_at",
    }
    for nullable in ("patient_id", "conversation_id", "closed_at", "refund_source"):
        assert table.c[nullable].nullable is True
    fk = {fk.parent.name: fk.ondelete for fk in table.foreign_keys}
    assert fk["tenant_id"] == "CASCADE" and fk["released_appointment_id"] == "CASCADE"
    assert fk["patient_id"] == "SET NULL" and fk["rebooked_appointment_id"] == "SET NULL"
    names = {c.name for c in table.constraints if c.name}
    assert "uq_deposit_release_offers_released_appointment_id" in names
```

E em `tests/test_deposit_before_migration.py` (a revisão dela deixa de ser a cabeça; a mais nova é que a fixa):

```diff
--- a/tests/test_deposit_before_migration.py
+++ b/tests/test_deposit_before_migration.py
@@ -48,7 +48,8 @@
 
 def test_the_revision_chains_after_the_provider_core_and_is_the_single_head():
     script = ScriptDirectory.from_config(_config())
-    assert script.get_heads() == [REVISION]
+    # TASK-052 chains `b6d2e8f4a1c3` after this one: the newest migration pins the head.
+    assert len(script.get_heads()) == 1
     assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD
 
 
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_deposit_release_offer_migration.py -q -p no:cacheprovider`
Expected: FAIL (a revisão `b6d2e8f4a1c3` e a tabela `deposit_release_offers` ainda não existem).

- [ ] **Step 3: Implementar**

`src/secretaria/models/deposit_release_offer.py`:

```python
"""DepositReleaseOffer - the choice a patient gets when the clinic frees a PAID slot (TASK-052).

When the clinic releases an appointment whose Pix deposit is already PAID
(`POST /appointments/{id}/release`), the platform does not refund by itself (decision D15)
and does not decide for the patient either: it opens ONE row here and asks the patient, with
two buttons, whether they want the deposit back ("Receber agora") or want to keep it for a new
day and time ("Remarcar"). No answer within 24 hours counts as "devolver".

Why a table and not columns on `pix_deposits`:
  * one deposit can be released more than once over its life (released, carried to a new
    appointment, that one released again): one offer PER RELEASED APPOINTMENT, unique;
  * the offer is a short-lived conversation state with its own clock (`deadline_at`) that the
    payment sweep scans by (state, deadline) - a narrow index here, not on the money table;
  * only code written for TASK-052 reads it, so the additive migration is safe to run before
    either service is deployed (skill frozen-contract-migration).

`state`:
    open          - the offer was sent; nothing chosen yet.
    rescheduling  - the patient tapped "Remarcar" and is picking a new day/time; the paid
                    deposit is reserved for the next booking this conversation commits.
    refund        - closed: an attention item asks the clinic to return the deposit
                    (`refund_source`: "patient" tapped "Receber agora", or "timeout").
    rebooked      - closed: the deposit now belongs to `rebooked_appointment_id`.

LGPD: ids, instants and the state only - never a name, phone, e-mail or message text.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base

STATE_OPEN = "open"
STATE_RESCHEDULING = "rescheduling"
STATE_REFUND = "refund"
STATE_REBOOKED = "rebooked"
OFFER_STATES = (STATE_OPEN, STATE_RESCHEDULING, STATE_REFUND, STATE_REBOOKED)
# The states a tap, the timer or a booking commit may still move.
OPEN_OFFER_STATES = (STATE_OPEN, STATE_RESCHEDULING)

REFUND_SOURCE_PATIENT = "patient"
REFUND_SOURCE_TIMEOUT = "timeout"


class DepositReleaseOffer(Base):
    __tablename__ = "deposit_release_offers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    deposit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pix_deposits.id", ondelete="CASCADE"), index=True
    )
    # One offer per released appointment: a second release of the same row is impossible
    # (it is CANCELLED), and a replayed open is a no-op by this key.
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
    offered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refund_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    rebooked_appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "released_appointment_id", name="uq_deposit_release_offers_released_appointment_id"
        ),
        Index("ix_deposit_release_offers_state_deadline", "state", "deadline_at"),
    )
```

`migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py`:

```python
"""deposit release offers: the patient's choice after the clinic frees a PAID slot

TASK-052 (docs/superpowers/specs/2026-10-11-liberar-horario-com-sinal-pago-design.md). Purely
ADDITIVE (skill frozen-contract-migration): ONE new table, nothing altered, nothing dropped. Only
code written for TASK-052 reads or writes it, so code that predates this revision (the API or the
worker, whichever is deployed first) keeps working untouched.

DEPLOY ORDER: this migration -> secretarIA API -> secretarIA worker RIGHT AFTER (the API opens
offers; the worker sends their buttons, handles the taps, carries the deposit over and closes the
expired offers) -> Brain-Message-Frontend (copy of the release card) last.

Revision ID: b6d2e8f4a1c3
Revises: e2b7c4a9d1f5
Create Date: 2026-10-11
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
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
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
            "patient_id",
            sa.Uuid(),
            sa.ForeignKey("patients.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("state", sa.String(16), nullable=False, server_default="open"),
        sa.Column("offered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refund_source", sa.String(16), nullable=True),
        sa.Column(
            "rebooked_appointment_id",
            sa.Uuid(),
            sa.ForeignKey("appointments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "released_appointment_id", name="uq_deposit_release_offers_released_appointment_id"
        ),
    )
    op.create_index(
        "ix_deposit_release_offers_tenant_id", "deposit_release_offers", ["tenant_id"]
    )
    op.create_index(
        "ix_deposit_release_offers_deposit_id", "deposit_release_offers", ["deposit_id"]
    )
    op.create_index(
        "ix_deposit_release_offers_conversation_id", "deposit_release_offers", ["conversation_id"]
    )
    op.create_index(
        "ix_deposit_release_offers_state_deadline",
        "deposit_release_offers",
        ["state", "deadline_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_deposit_release_offers_state_deadline", table_name="deposit_release_offers")
    op.drop_index("ix_deposit_release_offers_conversation_id", table_name="deposit_release_offers")
    op.drop_index("ix_deposit_release_offers_deposit_id", table_name="deposit_release_offers")
    op.drop_index("ix_deposit_release_offers_tenant_id", table_name="deposit_release_offers")
    op.drop_table("deposit_release_offers")
```

```diff
--- a/src/secretaria/models/__init__.py
+++ b/src/secretaria/models/__init__.py
@@ -18,6 +18,7 @@
 from secretaria.models.consent_event import ConsentEvent
 from secretaria.models.conversation import Conversation, FlowState, HandoverState
 from secretaria.models.conversation_pii_token_map import ConversationPiiTokenMap
+from secretaria.models.deposit_release_offer import DepositReleaseOffer
 from secretaria.models.insurance import (
     INSURANCE_MECHANISMS,
     INSURANCE_MODES,
@@ -56,6 +57,7 @@
     "ClinicPaymentAccount",
     "is_live_status",
     "ConsentEvent",
+    "DepositReleaseOffer",
     "Conversation",
     "ConversationPiiTokenMap",
     "FlowState",
```

- [ ] **Step 4: Rodar e ver passar**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_deposit_release_offer_migration.py tests/test_deposit_before_migration.py tests/test_payment_migration.py tests/test_flow_draft_column.py tests/test_migration_extra_reminder_day_time.py tests/test_migration_professional_edit_notices.py tests/test_migration_reminders_foundation.py tests/test_flow_replaces_column.py tests/test_flow_edit_draft_column.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/models/deposit_release_offer.py src/secretaria/models/__init__.py migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py tests/test_deposit_release_offer_migration.py tests/test_deposit_before_migration.py`
Expected: PASS (na escrita: 34 passed, 4 skipped — os de Postgres opcionais), ruff limpo.

- [ ] **Step 5 (opcional, recomendado): ida e volta num Postgres descartável**

Com um Postgres LOCAL descartável (ex.: `initdb` numa pasta temporária, porta livre), `$env:PAYMENTS_TEST_PG_URL = "postgresql+asyncpg://postgres@127.0.0.1:<porta>/postgres"` e rode `tests/test_deposit_before_migration.py tests/test_payment_migration.py`. Expected: PASS (o teste de ida e volta compara o banco migrado com os modelos: a tabela nova precisa bater). Desligue e apague o Postgres depois. Sem Postgres: registre "NÃO PROVADO em Postgres" no `TASK.md`.

- [ ] **Step 6: Commit**

```powershell
git add src/secretaria/models/deposit_release_offer.py src/secretaria/models/__init__.py migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py tests/test_deposit_release_offer_migration.py tests/test_deposit_before_migration.py
git commit -m "feat(payments): deposit_release_offers table and additive migration (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 2: O serviço da oferta (`services/release_offer.py`) e o motivo `released_refund_due`

**Files:**
- Create: `src/secretaria/services/release_offer.py`, `tests/release_offer_support.py`
- Modify: `src/secretaria/models/payment_attention_item.py` (motivo novo), `src/secretaria/services/payments/attention_view.py` (texto do motivo; devolução registrável), `tests/test_payment_attention.py` (a lista de motivos)
- Test: `tests/test_release_offer.py` (novo)

**Interfaces:**
- Consumes: Task 1; `attention.open_attention_item`, `attention.attention_dedupe_key`, `db_utils.insert_ignore`, `money.format_brl`, `staff_patient_message.conversation_id_for`, `core.whatsapp_limits.{MAX_INTERACTIVE_BODY_CHARS, truncate_plain}`.
- Produces: `OFFER_HOURS = 24`; `ACTION_REFUND = "relrefund"`, `ACTION_RESCHEDULE = "relresched"`, `RELEASE_OFFER_ACTIONS`; `LABEL_REFUND = "Receber agora"`, `LABEL_RESCHEDULE = "Remarcar"`; `DEPOSIT_OUTCOME_OFFERED = "refund_offered"`; `REASON_CARRIED_OVER = "carried_over"`; textos `TAP_REFUND_TEXT` (com `{amount}`), `TAP_REFUND_ALREADY_TEXT`, `TAP_ALREADY_REBOOKED_TEXT`, `RESCHEDULE_PREFIX`, `RESCHEDULE_AFTER_REFUND_PREFIX`, `CARRIED_OVER_NOTE`; `offer_buttons(released_appointment_id) -> list[tuple[str, str]]`; `offer_text(deposit) -> str`; `fit_body(head, offer, limit=1024) -> str`; `warning_text(deposit) -> str`; `async open_offer(session, *, tenant, appointment, deposit, now=None) -> DepositReleaseOffer | None`; `async find_offer(session, *, tenant_id, released_appointment_id)`; `async choose_refund(session, *, offer, source="patient", now=None) -> bool`; `async choose_reschedule(session, *, offer, conversation_id, now=None) -> bool`; `async carry_over_pending(session, *, tenant_id, conversation_id, now=None) -> bool`; `async claim_carry_over(session, *, tenant_id, conversation_id, new_appointment, now=None) -> PixDeposit | None`; `async expire_offers(session, *, now=None, limit=50) -> int` (comita); `deadline_passed(offer, now=None) -> bool`; `REASON_RELEASED_REFUND_DUE = "released_refund_due"` (em `models/payment_attention_item.py`). Nada aqui comita, exceto `expire_offers`.

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

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    Patient,
    PixDeposit,
    PixDepositStatus,
    Tenant,
)
from tests.before_support import make_world

RELEASED_START = datetime(2030, 3, 4, 13, 0, tzinfo=UTC)


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
    **tenant_kw,
) -> Released:
    """A clinic whose appointment was just RELEASED (status CANCELLED) with a deposit."""
    world = await make_world(session, channel=channel, timing=timing, **tenant_kw)
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
    """A fresh SCHEDULED appointment of the same patient (what the rebooking commit creates)."""
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
```

`tests/test_release_offer.py`:

```python
"""services/release_offer.py - the offer a patient gets when the clinic frees a PAID slot.

TASK-052.

The money rule is decision D15: the platform NEVER refunds; "devolver" = ONE attention item
(`released_refund_due`) for the clinic to return the deposit in the provider's panel. Every
transition is one guarded UPDATE, so a tap, the 24-hour timer and a booking commit never both win.
"""

from datetime import UTC, datetime, timedelta
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
from tests.payments_support import make_sessionmaker
from tests.release_offer_support import make_released, new_appointment

NOW = datetime(2030, 3, 2, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


async def _offer(db, **kw):
    async with db() as session:
        released = await make_released(session, **kw)
        offer = await release_offer.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=NOW,
        )
        await session.commit()
    return released, offer


async def _items(db):
    async with db() as session:
        return list(await session.scalars(select(PaymentAttentionItem)))


async def _reload(db, model, row_id):
    async with db() as session:
        return await session.get(model, row_id)


def test_the_card_fits_whatsapp_and_names_the_two_choices():
    buttons = release_offer.offer_buttons(uuid4())
    assert [label for _id, label in buttons] == ["Receber agora", "Remarcar"]
    for button_id, label in buttons:
        assert len(label) <= MAX_BUTTON_LABEL_CHARS
        assert len(button_id) <= MAX_BUTTON_ID_CHARS
    assert buttons[0][0].startswith("relrefund|") and buttons[1][0].startswith("relresched|")


def test_the_offer_text_never_says_the_platform_refunds_and_survives_a_long_justification():
    deposit = PixDeposit(amount_cents=6000)
    offer = release_offer.offer_text(deposit)
    assert "R$ 60,00" in offer and "Remarcar" in offer and "Receber agora" in offer
    assert "a clínica fará a devolução" in offer
    for promise in ("será estornado", "devolvemos", "estorno automático", "reembolsamos"):
        assert promise not in offer
    body = release_offer.fit_body("x" * 2000, offer)
    assert len(body) <= MAX_INTERACTIVE_BODY_CHARS
    assert body.endswith(offer)  # the justification is cut, never the explanation of the buttons


async def test_opening_is_idempotent_and_only_for_a_paid_deposit(db):
    released, offer = await _offer(db)
    assert offer.state == "open" and offer.conversation_id == released.conversation.id
    assert release_offer.deadline_passed(offer, NOW + timedelta(hours=23)) is False
    assert release_offer.deadline_passed(offer, NOW + timedelta(hours=24)) is True
    async with db() as session:
        again = await release_offer.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=NOW + timedelta(minutes=5),
        )
        await session.commit()
        count = await session.scalar(select(func.count()).select_from(DepositReleaseOffer))
    assert again.id == offer.id and count == 1

    _released, nothing = await _offer(db, deposit_status=PixDepositStatus.AWAITING)
    assert nothing is None


async def test_a_deposit_of_another_clinic_never_gets_an_offer(db):
    released, _offer_row = await _offer(db)
    other, _ = await _offer(db)
    async with db() as session:
        crossed = await release_offer.open_offer(
            session, tenant=other.tenant, appointment=released.appointment, deposit=released.deposit
        )
    assert crossed is None


async def test_receber_agora_opens_one_full_refund_item_and_a_second_tap_changes_nothing(db):
    released, offer = await _offer(db)
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.choose_refund(session, offer=row, now=NOW) is True
        await session.commit()
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.choose_refund(session, offer=row, now=NOW) is False
        await session.commit()
    (item,) = await _items(db)
    assert item.reason == "released_refund_due" and item.status == "open"
    assert item.suggested_refund_cents == 6000 and item.deposit_id == released.deposit.id
    assert item.tenant_id == released.tenant_id
    closed = await _reload(db, DepositReleaseOffer, offer.id)
    assert (closed.state, closed.refund_source) == ("refund", "patient")
    deposit = await _reload(db, PixDeposit, released.deposit.id)
    assert deposit.status is PixDepositStatus.PAID  # never refunded by us (D15)
    assert deposit.fulfillment_status == FULFILLMENT_NEEDS_ATTENTION


async def test_remarcar_moves_the_deadline_once_and_reserves_the_deposit(db):
    released, offer = await _offer(db)
    tap = NOW + timedelta(hours=20)
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.choose_reschedule(
            session, offer=row, conversation_id=released.conversation.id, now=tap
        )
        await session.commit()
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.choose_reschedule(
            session,
            offer=row,
            conversation_id=released.conversation.id,
            now=tap + timedelta(hours=1),
        )
        await session.commit()
        assert await release_offer.carry_over_pending(
            session, tenant_id=released.tenant_id, conversation_id=released.conversation.id, now=tap
        )
    moved = await _reload(db, DepositReleaseOffer, offer.id)
    assert moved.state == "rescheduling"
    assert release_offer.deadline_passed(moved, tap + timedelta(hours=23, minutes=59)) is False
    assert release_offer.deadline_passed(moved, tap + timedelta(hours=24)) is True


async def test_remarcar_after_the_refund_or_after_the_deadline_is_refused(db):
    released, offer = await _offer(db)
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        late = NOW + timedelta(hours=25)
        assert not await release_offer.choose_reschedule(
            session, offer=row, conversation_id=released.conversation.id, now=late
        )
        await release_offer.choose_refund(session, offer=row, now=NOW)
        await session.commit()
        assert not await release_offer.choose_reschedule(
            session, offer=row, conversation_id=released.conversation.id, now=NOW
        )


async def test_the_booking_commit_carries_the_paid_deposit_over_exactly_once(db):
    released, offer = await _offer(db)
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        await release_offer.choose_reschedule(
            session, offer=row, conversation_id=released.conversation.id, now=NOW
        )
        await session.commit()
    async with db() as session:
        fresh = await new_appointment(session, released, start=datetime(2030, 3, 9, 14, tzinfo=UTC))
        carried = await release_offer.claim_carry_over(
            session,
            tenant_id=released.tenant_id,
            conversation_id=released.conversation.id,
            new_appointment=fresh,
            now=NOW + timedelta(hours=1),
        )
        await session.commit()
    assert carried is not None and carried.appointment_id == fresh.id
    assert carried.status is PixDepositStatus.PAID and carried.reschedule_count == 0
    done = await _reload(db, DepositReleaseOffer, offer.id)
    assert (done.state, done.rebooked_appointment_id) == ("rebooked", fresh.id)
    assert await _items(db) == []  # nothing to refund: the money is in use

    async with db() as session:
        second = await new_appointment(
            session, released, start=datetime(2030, 3, 10, 14, tzinfo=UTC)
        )
        assert (
            await release_offer.claim_carry_over(
                session,
                tenant_id=released.tenant_id,
                conversation_id=released.conversation.id,
                new_appointment=second,
                now=NOW + timedelta(hours=2),
            )
            is None
        )
        await session.commit()
        assert not await release_offer.carry_over_pending(
            session, tenant_id=released.tenant_id, conversation_id=released.conversation.id
        )


async def test_another_clinic_or_a_closed_offer_carries_nothing(db):
    released, offer = await _offer(db)
    other, _ = await _offer(db)
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        await release_offer.choose_reschedule(
            session, offer=row, conversation_id=released.conversation.id, now=NOW
        )
        await session.commit()
    async with db() as session:
        foreign = await new_appointment(session, other, start=datetime(2030, 3, 9, 14, tzinfo=UTC))
        assert (
            await release_offer.claim_carry_over(
                session,
                tenant_id=other.tenant_id,
                conversation_id=released.conversation.id,
                new_appointment=foreign,
                now=NOW,
            )
            is None
        )
    deposit = await _reload(db, PixDeposit, released.deposit.id)
    assert deposit.appointment_id == released.appointment.id


async def test_the_timer_refunds_open_and_rescheduling_offers_once_and_spares_the_rest(db):
    _a, open_offer = await _offer(db)
    b, rescheduling = await _offer(db)
    _c, fresh = await _offer(db)
    async with db() as session:
        row = await session.get(DepositReleaseOffer, rescheduling.id)
        await release_offer.choose_reschedule(
            session, offer=row, conversation_id=b.conversation.id, now=NOW
        )
        fresh_row = await session.get(DepositReleaseOffer, fresh.id)
        fresh_row.deadline_at = NOW + timedelta(hours=40)
        await session.commit()
    async with db() as session:
        assert await release_offer.expire_offers(session, now=NOW + timedelta(hours=25)) == 2
    async with db() as session:
        assert await release_offer.expire_offers(session, now=NOW + timedelta(hours=26)) == 0
    states = {
        o.id: (o.state, o.refund_source)
        for o in [
            await _reload(db, DepositReleaseOffer, open_offer.id),
            await _reload(db, DepositReleaseOffer, rescheduling.id),
            await _reload(db, DepositReleaseOffer, fresh.id),
        ]
    }
    assert states[open_offer.id] == ("refund", "timeout")
    assert states[rescheduling.id] == ("refund", "timeout")
    assert states[fresh.id] == ("open", None)
    assert len(await _items(db)) == 2


async def test_a_tap_that_loses_to_the_timer_opens_no_second_item(db):
    _released, offer = await _offer(db)
    async with db() as session:
        await release_offer.expire_offers(session, now=NOW + timedelta(hours=25))
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.choose_refund(session, offer=row, now=NOW) is False
        await session.commit()
    assert len(await _items(db)) == 1
```

```diff
--- a/tests/test_payment_attention.py
+++ b/tests/test_payment_attention.py
@@ -70,6 +70,7 @@
         "reference_mismatch",
         "promotion_failed",
         "refunded_externally",
+        "released_refund_due",
     }
 
 
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_release_offer.py tests/test_payment_attention.py -q -p no:cacheprovider`
Expected: FAIL (`ImportError: cannot import name 'release_offer'`; e `released_refund_due` ausente de `ATTENTION_REASONS`).

- [ ] **Step 3: Implementar**

`src/secretaria/services/release_offer.py`:

```python
"""The paid deposit of a slot the CLINIC released: refund or carry it over (TASK-052).

Owner decision 2026-10-11 (spec docs/superpowers/specs/2026-10-11-liberar-horario-com-sinal-
pago-design.md). When the clinic releases an appointment whose Pix deposit is PAID, the
platform never returns money by itself (decision D15) and does not decide for the patient:

* `open_offer` (hub release, same transaction) writes ONE `DepositReleaseOffer` per released
  appointment and the patient gets two buttons: "Receber agora" / "Remarcar".
* "Receber agora" -> `choose_refund`: the offer closes as `refund` and ONE attention item
  (`released_refund_due`, the full amount) asks the clinic to return the deposit in the
  provider's own panel. The patient is told the clinic does it within 24 hours.
* "Remarcar" -> `choose_reschedule`: `rescheduling`, the deadline moves to 24 hours after the
  tap (once), and the patient picks a new day, then a new time ("Outro horário": same
  professional, same service). The booking gate asks for no new deposit
  (`carry_over_pending`), and the booking commit calls `claim_carry_over`, which closes the
  offer as `rebooked` and re-points the PAID deposit to the new appointment in the SAME
  transaction. `reschedule_count` is not read nor written: this is not the patient's
  reschedule, the clinic freed the slot.
* no answer -> `expire_offers` (payment sweep) closes every offer past its deadline as `refund`
  (source `timeout`) with the same item.

Concurrency: every transition is ONE guarded UPDATE (`WHERE state IN (...)`), so a tap and the
timer, two taps, or a tap and a booking commit never both win; the attention item is deduped
per deposit (`attention.attention_dedupe_key`), so a deposit is never asked back twice.
Isolation: every query carries the tenant id the caller resolved (the conversation's, the
appointment's or the offer's own), never one taken from a payload. Never logs a name, phone,
e-mail or message text. Nothing here commits unless the docstring says so.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS, truncate_plain
from secretaria.models import Appointment, Patient, PixDeposit, PixDepositStatus, Tenant
from secretaria.models.deposit_release_offer import (
    OPEN_OFFER_STATES,
    REFUND_SOURCE_PATIENT,
    REFUND_SOURCE_TIMEOUT,
    STATE_OPEN,
    STATE_REBOOKED,
    STATE_REFUND,
    STATE_RESCHEDULING,
    DepositReleaseOffer,
)
from secretaria.models.payment_attention_item import REASON_RELEASED_REFUND_DUE
from secretaria.services.payments import attention
from secretaria.services.payments.db_utils import insert_ignore
from secretaria.services.payments.money import format_brl
from secretaria.services.staff_patient_message import conversation_id_for

logger = get_logger(__name__)

OFFER_HOURS = 24

# Button ids: "<action>|<released appointment id>" - the family schemas/webhook.py decodes
# (`_ACTION_BUTTON_PREFIXES`) and the Portal accepts when a recent card offered it.
ACTION_REFUND = "relrefund"
ACTION_RESCHEDULE = "relresched"
RELEASE_OFFER_ACTIONS: tuple[str, ...] = (ACTION_REFUND, ACTION_RESCHEDULE)
# <= 20 characters each (core/whatsapp_limits.py MAX_BUTTON_LABEL_CHARS).
LABEL_REFUND = "Receber agora"
LABEL_RESCHEDULE = "Remarcar"

# The value `deposit_outcome` carries in the hub's release answer when an offer was opened.
DEPOSIT_OUTCOME_OFFERED = "refund_offered"
# The `before` booking gate's reason for asking no deposit: this conversation carries a paid one.
REASON_CARRIED_OVER = "carried_over"

# --- patient copy (pt-BR; never says the PLATFORM refunds - the clinic does, D15) -------------

TAP_REFUND_TEXT = (
    "Combinado! Avisamos a clínica, que fará a devolução do seu sinal de {amount} em até "
    "24 horas. Se quiser marcar outra consulta, é só me escrever."
)
TAP_REFUND_ALREADY_TEXT = (
    "A devolução do seu sinal já foi pedida à clínica e será feita em até 24 horas."
)
TAP_ALREADY_REBOOKED_TEXT = "O sinal que você já pagou está valendo para a sua nova consulta."
RESCHEDULE_PREFIX = "Ótimo! O sinal que você já pagou vale para a nova consulta."
RESCHEDULE_AFTER_REFUND_PREFIX = (
    "A devolução do seu sinal já foi pedida à clínica, então a nova consulta segue as regras "
    "de sinal da clínica."
)
CARRIED_OVER_NOTE = "O sinal que você já pagou vale para esta consulta. Não há nova cobrança."
OFFER_EXPIRED_AT_BOOKING_NOTE = (
    "O prazo de 24 horas para usar o sinal pago terminou; a clínica fará a devolução dele."
)


def offer_buttons(released_appointment_id) -> list[tuple[str, str]]:
    """The two (id, label) pairs of the offer card, in the order the patient reads them."""
    return [
        (f"{ACTION_REFUND}|{released_appointment_id}", LABEL_REFUND),
        (f"{ACTION_RESCHEDULE}|{released_appointment_id}", LABEL_RESCHEDULE),
    ]


def offer_text(deposit: PixDeposit) -> str:
    """The block the release notice carries instead of the rebooking invitation."""
    return (
        f"Você já pagou o sinal desta consulta ({format_brl(deposit.amount_cents)}). "
        "Toque em *Remarcar* para escolher um novo dia e horário: o sinal pago vale para a nova "
        "consulta, sem nova cobrança. Se preferir, toque em *Receber agora* e a clínica fará a "
        "devolução do sinal em até 24 horas. Se você não responder em até 24 horas, a clínica "
        "também fará a devolução."
    )


def fit_body(head: str, offer: str, limit: int = MAX_INTERACTIVE_BODY_CHARS) -> str:
    """`head` + a blank line + `offer`, inside `limit`, cutting the HEAD (the clinic's
    justification) and never the offer: the offer is what explains the two buttons."""
    separator = "\n\n"
    room = limit - len(offer) - len(separator)
    if room <= 0:
        return truncate_plain(offer, limit)
    return f"{truncate_plain(head, room)}{separator}{offer}"


def warning_text(deposit: PixDeposit) -> str:
    """What the CLINIC reads before releasing a paid slot (the 409 `retention_ack_required`)."""
    return (
        f"O paciente já pagou o sinal ({format_brl(deposit.amount_cents)}). Ao liberar o horário, "
        "ele poderá remarcar usando o mesmo sinal ou pedir a devolução. Se ele não responder em "
        "24 horas, a clínica deve devolver o valor por inteiro pelo painel do provedor de "
        "pagamento."
    )


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def open_offer(
    session: AsyncSession,
    *,
    tenant: Tenant,
    appointment: Appointment,
    deposit: PixDeposit,
    now: datetime | None = None,
) -> DepositReleaseOffer | None:
    """Open the offer for a released appointment whose deposit is PAID; idempotent.

    Returns the offer (new, or the one a replay already wrote), or None when there is nothing
    to offer (no PAID deposit of THIS clinic on THIS appointment). Does not commit.
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
            "offered_at": now,
            "deadline_at": now + timedelta(hours=OFFER_HOURS),
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


async def _open_refund_item(
    session: AsyncSession, offer: DepositReleaseOffer, released: Appointment | None
) -> None:
    deposit = await session.get(PixDeposit, offer.deposit_id)
    if deposit is None or deposit.tenant_id != offer.tenant_id:
        logger.error("deposit_release_offer_deposit_missing", offer_id=str(offer.id))
        return
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


async def choose_refund(
    session: AsyncSession,
    *,
    offer: DepositReleaseOffer,
    source: str = REFUND_SOURCE_PATIENT,
    now: datetime | None = None,
) -> bool:
    """Close the offer as `refund` and open the clinic's item. True when THIS call did it.

    A loser (the offer already refunded or rebooked, by a tap or the timer) changes nothing.
    Does not commit.
    """
    now = _now(now)
    claimed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.state.in_(OPEN_OFFER_STATES),
        )
        .values(state=STATE_REFUND, refund_source=source, closed_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return False
    released = await session.get(Appointment, offer.released_appointment_id)
    await _open_refund_item(session, offer, released)
    logger.info(
        "deposit_release_offer_refund",
        tenant_id=str(offer.tenant_id),
        offer_id=str(offer.id),
        source=source,
    )
    return True


async def choose_reschedule(
    session: AsyncSession,
    *,
    offer: DepositReleaseOffer,
    conversation_id: UUID,
    now: datetime | None = None,
) -> bool:
    """`open` -> `rescheduling`, deadline 24 hours from now (only the FIRST tap moves it).

    True when the offer is (now or already) `rescheduling` for this conversation; False when it
    is closed (refunded or rebooked) or expired. Does not commit.
    """
    now = _now(now)
    moved = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.state == STATE_OPEN,
            DepositReleaseOffer.deadline_at > now,
        )
        .values(
            state=STATE_RESCHEDULING,
            deadline_at=now + timedelta(hours=OFFER_HOURS),
            conversation_id=conversation_id,
        )
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
        select(DepositReleaseOffer.state).where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.state == STATE_RESCHEDULING,
            DepositReleaseOffer.deadline_at > now,
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
            DepositReleaseOffer.deadline_at > _now(now),
        )
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
    """Move the paid deposit to `new_appointment` (flushed, same tenant) and close the offer.

    Called INSIDE the booking's own transaction. Returns the deposit when this booking carried
    it; None when there was nothing to carry (no offer in `rescheduling`, the deadline passed,
    or the timer/another tap won). Does not commit.
    """
    if new_appointment.tenant_id != tenant_id:
        return None
    now = _now(now)
    offer = await session.scalar(
        select(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.tenant_id == tenant_id,
            DepositReleaseOffer.conversation_id == conversation_id,
            DepositReleaseOffer.state == STATE_RESCHEDULING,
            DepositReleaseOffer.deadline_at > now,
        )
        .order_by(DepositReleaseOffer.offered_at.desc())
        .limit(1)
    )
    if offer is None:
        return None
    claimed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.state == STATE_RESCHEDULING,
            DepositReleaseOffer.deadline_at > now,
        )
        .values(
            state=STATE_REBOOKED,
            closed_at=now,
            rebooked_appointment_id=new_appointment.id,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return None
    moved = await session.execute(
        update(PixDeposit)
        .where(
            PixDeposit.id == offer.deposit_id,
            PixDeposit.tenant_id == tenant_id,
            PixDeposit.status == PixDepositStatus.PAID,
            PixDeposit.appointment_id == offer.released_appointment_id,
        )
        .values(appointment_id=new_appointment.id)
        .execution_options(synchronize_session=False)
    )
    if moved.rowcount != 1:
        # The money moved under us (resolved by the clinic, say): put the offer back to what it
        # was and let the booking stand on its own; nothing is half-carried.
        await session.execute(
            update(DepositReleaseOffer)
            .where(DepositReleaseOffer.id == offer.id)
            .values(state=STATE_RESCHEDULING, closed_at=None, rebooked_appointment_id=None)
            .execution_options(synchronize_session=False)
        )
        logger.warning("deposit_release_offer_carry_lost_deposit", offer_id=str(offer.id))
        return None
    logger.info(
        "deposit_release_offer_rebooked",
        tenant_id=str(tenant_id),
        offer_id=str(offer.id),
        appointment_id=str(new_appointment.id),
    )
    return await session.get(PixDeposit, offer.deposit_id, populate_existing=True)


async def expire_offers(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 50
) -> int:
    """The timer: every offer past its deadline becomes `refund` (source `timeout`) with its item.

    One commit per offer; one failure is logged and never stops the batch. Returns how many
    offers THIS tick closed. Commits.
    """
    now = _now(now)
    due = (
        await session.scalars(
            select(DepositReleaseOffer.id)
            .where(
                DepositReleaseOffer.state.in_(OPEN_OFFER_STATES),
                DepositReleaseOffer.deadline_at <= now,
            )
            .order_by(DepositReleaseOffer.deadline_at)
            .limit(limit)
        )
    ).all()
    closed = 0
    for offer_id in due:
        try:
            offer = await session.get(DepositReleaseOffer, offer_id)
            if offer is not None and await choose_refund(
                session, offer=offer, source=REFUND_SOURCE_TIMEOUT, now=now
            ):
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


def deadline_passed(offer: DepositReleaseOffer, now: datetime | None = None) -> bool:
    return _as_utc(offer.deadline_at) <= _now(now)
```

```diff
--- a/src/secretaria/models/payment_attention_item.py
+++ b/src/secretaria/models/payment_attention_item.py
@@ -35,6 +35,9 @@
 REASON_REFERENCE_MISMATCH = "reference_mismatch"
 REASON_PROMOTION_FAILED = "promotion_failed"
 REASON_REFUNDED_EXTERNALLY = "refunded_externally"
+# TASK-052: the clinic released a PAID slot and the patient asked for the money back (or did not
+# answer within 24 hours) - the clinic returns the full deposit in the provider's panel.
+REASON_RELEASED_REFUND_DUE = "released_refund_due"
 
 ATTENTION_REASONS = frozenset(
     {
@@ -48,6 +51,7 @@
         REASON_REFERENCE_MISMATCH,
         REASON_PROMOTION_FAILED,
         REASON_REFUNDED_EXTERNALLY,
+        REASON_RELEASED_REFUND_DUE,
     }
 )
 
```

```diff
--- a/src/secretaria/services/payments/attention_view.py
+++ b/src/secretaria/services/payments/attention_view.py
@@ -29,6 +29,7 @@
     REASON_PAID_THEN_CANCELLED,
     REASON_PROMOTION_FAILED,
     REASON_REFUND_DUE_POLICY,
+    REASON_RELEASED_REFUND_DUE,
     RESOLUTION_ACTIONS,
     PaymentAttentionItem,
 )
@@ -59,6 +60,7 @@
     "reference_mismatch": "Pagamento com referência diferente da esperada.",
     "promotion_failed": "Pago, mas não conseguimos confirmar a consulta.",
     "refunded_externally": "O provedor informa que este pagamento foi estornado.",
+    "released_refund_due": "Horário liberado pela clínica: devolver o sinal ao paciente.",
 }
 
 # Resolving with "refunded in the provider's panel" closes the money story of the deposit only
@@ -69,6 +71,7 @@
         REASON_PAID_THEN_CANCELLED,
         REASON_REFUND_DUE_POLICY,
         REASON_PROMOTION_FAILED,
+        REASON_RELEASED_REFUND_DUE,
     }
 )
 
```

- [ ] **Step 4: Rodar e ver passar**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_release_offer.py tests/test_payment_attention.py tests/test_payment_attention_hub.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/services/release_offer.py src/secretaria/models/payment_attention_item.py src/secretaria/services/payments/attention_view.py tests/release_offer_support.py tests/test_release_offer.py tests/test_payment_attention.py`
Expected: PASS (11 novos; o parametrizado de `test_payment_attention_hub.py` ganha um caso: o motivo novo NÃO manda e-mail), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/services/release_offer.py src/secretaria/models/payment_attention_item.py src/secretaria/services/payments/attention_view.py tests/release_offer_support.py tests/test_release_offer.py tests/test_payment_attention.py
git commit -m "feat(payments): release offer service - guarded refund/reschedule/carry/expire transitions, one refund item per deposit (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 3: Liberar abre a oferta (hub) e o Portal recebe o cartão com os dois botões

**Files:**
- Modify: `src/secretaria/api/hub/calendar.py` (`release_appointment`), `src/secretaria/services/appointment_release.py` (`notify_released_patient`, `_notify_portal`)
- Test: `tests/test_hub_release.py` (os 4 testes de sinal pago viram 2 — um parametrizado em 3 casos — e um teste novo do Portal)

**Interfaces:**
- Consumes: Task 2 (`open_offer`, `offer_text`, `offer_buttons`, `warning_text`, `fit_body`, `DEPOSIT_OUTCOME_OFFERED`).
- Produces: 409 `retention_ack_required` com `deposit_outcome = "refund_offered"`; 200 com `deposit_outcome = "refund_offered"` e uma oferta aberta; `notify_released_patient(..., offer_buttons: list[tuple[str, str]] | None = None)`; o enfileiramento do job NÃO muda (7 argumentos).

- [ ] **Step 1: Escrever os testes**

```diff
--- a/tests/test_hub_release.py
+++ b/tests/test_hub_release.py
@@ -16,7 +16,9 @@
     Appointment,
     AppointmentReminder,
     AppointmentStatus,
+    DepositReleaseOffer,
     FlowState,
+    PaymentAttentionItem,
     PixDeposit,
     PixDepositStatus,
     Tenant,
@@ -460,24 +462,40 @@
     return world
 
 
-async def test_a_paid_deposit_inside_the_retention_window_needs_the_acknowledgement(
-    client: AsyncClient, db, acting
+async def _offers(db) -> list[DepositReleaseOffer]:
+    async with db() as session:
+        return list(await session.scalars(select(DepositReleaseOffer)))
+
+
+async def _attention_items(db) -> list[PaymentAttentionItem]:
+    async with db() as session:
+        return list(await session.scalars(select(PaymentAttentionItem)))
+
+
+@pytest.mark.parametrize(
+    ("hours_ahead", "policy"), [(2, "total"), (2, "partial"), (96, "total")]
+)
+async def test_a_paid_deposit_needs_the_acknowledgement_and_the_patient_gets_the_choice(
+    client: AsyncClient, db, acting, hours_ahead, policy
 ):
-    world = await _paid_world(db, acting, hours_ahead=2)
+    """TASK-052: a clinic release never retains a paid deposit, whatever the patient
+    cancellation policy says - the patient chooses (or, silent for 24 h, gets it back)."""
+    world = await _paid_world(db, acting, hours_ahead=hours_ahead, policy=policy)
 
     response = await _release(client, world.appointment.id)
 
     assert response.status_code == 409
     detail = response.json()["detail"]
     assert detail["code"] == "retention_ack_required"
-    assert detail["deposit_outcome"] == "retained" and detail["amount_cents"] == 10000
-    assert "100,00" in detail["message"] and "fica com o sinal" in detail["message"]
+    assert detail["deposit_outcome"] == "refund_offered" and detail["amount_cents"] == 10000
+    assert "100,00" in detail["message"] and "remarcar" in detail["message"]
+    assert "24 horas" in detail["message"] and "por inteiro" in detail["message"]
     assert (await _appointment(db, world.appointment.id)).status == AppointmentStatus.SCHEDULED
     assert _FakeCalendar.deleted == []  # Google was not touched
-    assert await _deposit_status(db, world.appointment.id) == PixDepositStatus.PAID
+    assert await _offers(db) == []
 
 
-async def test_with_the_acknowledgement_the_clinic_keeps_the_deposit(
+async def test_with_the_acknowledgement_the_offer_opens_and_nothing_is_decided_yet(
     client: AsyncClient, db, acting
 ):
     world = await _paid_world(db, acting, hours_ahead=2)
@@ -485,36 +503,18 @@
     response = await _release(client, world.appointment.id, acknowledge_retention=True)
 
     assert response.status_code == 200
-    assert response.json()["deposit_outcome"] == "retained"
-    assert await _deposit_status(db, world.appointment.id) == PixDepositStatus.CANCELLED_RETAINED
+    assert response.json()["deposit_outcome"] == "refund_offered"
+    # Nothing was refunded nor retained (D15): the deposit stays PAID, no item yet.
+    assert await _deposit_status(db, world.appointment.id) == PixDepositStatus.PAID
+    assert await _attention_items(db) == []
+    [offer] = await _offers(db)
+    assert offer.state == "open" and offer.released_appointment_id == world.appointment.id
+    assert offer.tenant_id == world.tenant.id and offer.conversation_id == world.conversation.id
     assert len(_FakeCalendar.deleted) == 1
 
-
-async def test_a_partial_policy_is_announced_as_such(client: AsyncClient, db, acting):
-    world = await _paid_world(db, acting, hours_ahead=2, policy="partial")
-
-    detail = (await _release(client, world.appointment.id)).json()["detail"]
-
-    # Decision D15 (TASK-017, ported by TASK-050): the platform never refunds; the clinic does.
-    assert detail["deposit_outcome"] == "partial_refund_due"
-    assert "50,00" in detail["message"] and "painel" in detail["message"]
-
-
-async def test_even_a_full_refund_asks_first_and_then_leaves_it_to_the_clinic(
-    client: AsyncClient, db, acting
-):
-    world = await _paid_world(db, acting, hours_ahead=96)
-
-    refused = await _release(client, world.appointment.id)
-    accepted = await _release(client, world.appointment.id, acknowledge_retention=True)
-
-    assert refused.status_code == 409
-    assert refused.json()["detail"]["deposit_outcome"] == "refund_due"
-    assert "por inteiro" in refused.json()["detail"]["message"]
-    assert accepted.status_code == 200 and accepted.json()["deposit_outcome"] == "refund_due"
-    # Nothing was refunded by the platform (D15): the deposit stays PAID and the clinic gets an
-    # attention item with the amount to return in the provider's panel.
-    assert await _deposit_status(db, world.appointment.id) == PixDepositStatus.PAID
+    again = await _release(client, world.appointment.id, acknowledge_retention=True)
+    assert again.status_code == 409 and again.json()["detail"]["code"] == "not_live"
+    assert len(await _offers(db)) == 1  # a second release of the same slot changes nothing
 
 
 async def test_an_unpaid_charge_needs_no_acknowledgement(client: AsyncClient, db, acting):
@@ -693,7 +693,31 @@
 
     await _release(client, world.appointment.id, acknowledge_retention=True)
 
-    assert "retido" in pool.calls[0][5]
+    # Same job, same arguments (an older worker keeps working); the offer text rides along.
+    assert pool.calls[0][0] == "send_cancellation_notice" and len(pool.calls[0]) == 7
+    assert "Receber agora" in pool.calls[0][5] and "Remarcar" in pool.calls[0][5]
+    assert "retido" not in pool.calls[0][5]
+
+
+async def test_a_portal_patient_with_a_paid_deposit_gets_the_two_buttons(  # noqa: F811
+    client: AsyncClient, db, acting, portal_mail
+):
+    world = await _paid_world(
+        db, acting, hours_ahead=2, channel=CHANNEL_BRAIN_MESSAGE, email="paciente@x.com"
+    )
+
+    response = await _release(client, world.appointment.id, acknowledge_retention=True)
+
+    assert response.json()["patient_notice"] == "portal_chat_email"
+    [row] = await outbound_messages(db, world.conversation.id)
+    assert "desmarcou a sua consulta" in row.body
+    assert appointment_release.PORTAL_REBOOK_LINE not in row.body
+    options = [o["id"] for o in row.interactive["options"]]
+    assert options == [
+        f"relrefund|{world.appointment.id}",
+        f"relresched|{world.appointment.id}",
+    ]
+    assert [o["title"] for o in row.interactive["options"]] == ["Receber agora", "Remarcar"]
 
 
 async def test_a_portal_patient_gets_a_chat_message_and_an_email_nudge(  # noqa: F811
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_hub_release.py -q -p no:cacheprovider`
Expected: FAIL nos casos novos (`deposit_outcome` `retained`/`partial_refund_due`/`refund_due` em vez de `refund_offered`; nenhuma oferta; o Portal recebe texto, sem `interactive`).

- [ ] **Step 3: Implementar**

```diff
--- a/src/secretaria/api/hub/calendar.py
+++ b/src/secretaria/api/hub/calendar.py
@@ -61,6 +61,7 @@
     booking_hold,
     cancellation_notice,
     clinic_action_notice,
+    release_offer,
     reminder_hooks,
     reminder_schedule,
     staff_appointment_edit,
@@ -936,22 +937,21 @@
             ),
         )
 
-    # Money first, Google second: a release whose deposit was PAID moves money
-    # (retained / partially or fully refunded), so the clinic must have read the
-    # consequence. Raised before anything is touched.
+    # Money first, Google second: a release whose deposit was PAID hands the patient a choice
+    # (TASK-052: "Receber agora" / "Remarcar"; no answer in 24 h = the clinic returns it), so
+    # the clinic must have read the consequence. Raised before anything is touched.
     deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appt.id)
     if (
         deposit is not None
         and deposit.status is PixDepositStatus.PAID
         and not body.acknowledge_retention
     ):
-        outcome = deposit_lifecycle.preview_cancellation_outcome(tenant, deposit, appt)
         raise HTTPException(
             status.HTTP_409_CONFLICT,
             _detail(
                 "retention_ack_required",
-                deposit_lifecycle.release_warning_text(outcome, tenant, deposit),
-                deposit_outcome=outcome,
+                release_offer.warning_text(deposit),
+                deposit_outcome=release_offer.DEPOSIT_OUTCOME_OFFERED,
                 amount_cents=deposit.amount_cents,
             ),
         )
@@ -1015,16 +1015,30 @@
     )
     await reminder_schedule.cancel_reminders(session, appt.id, reason="released")
     professional_name = await _professional_name(session, tenant, appt)
-    deposit_outcome = await deposit_lifecycle.on_appointment_cancelled(
-        session, tenant=tenant, appointment=appt, waba_token=None
-    )
     deposit_notice: str | None = None
-    if deposit_outcome is not None:
-        resolved = await deposit_lifecycle.get_deposit_for_appointment(session, appt.id)
-        if resolved is not None:
-            deposit_notice = deposit_lifecycle.cancellation_notice(
-                deposit_outcome, tenant, resolved
-            )
+    offer_buttons: list[tuple[str, str]] | None = None
+    paid = await deposit_lifecycle.get_deposit_for_appointment(session, appt.id)
+    if paid is not None and paid.status is PixDepositStatus.PAID:
+        # TASK-052: the clinic freed a PAID slot. Nothing is decided for the patient and the
+        # platform never refunds (D15): ONE offer, answered by two buttons or by the 24-hour
+        # sweep (services/release_offer.py). No attention item yet.
+        offer = await release_offer.open_offer(
+            session, tenant=tenant, appointment=appt, deposit=paid, now=now
+        )
+        deposit_outcome = release_offer.DEPOSIT_OUTCOME_OFFERED if offer is not None else None
+        if offer is not None:
+            deposit_notice = release_offer.offer_text(paid)
+            offer_buttons = release_offer.offer_buttons(appt.id)
+    else:
+        deposit_outcome = await deposit_lifecycle.on_appointment_cancelled(
+            session, tenant=tenant, appointment=appt, waba_token=None
+        )
+        if deposit_outcome is not None:
+            resolved = await deposit_lifecycle.get_deposit_for_appointment(session, appt.id)
+            if resolved is not None:
+                deposit_notice = deposit_lifecycle.cancellation_notice(
+                    deposit_outcome, tenant, resolved
+                )
     patient = await session.scalar(
         select(Patient).where(Patient.id == appt.patient_id, Patient.tenant_id == tenant.id)
     )
@@ -1043,6 +1057,7 @@
             tenant, body.notify_outside_window
         ),
         arq_pool=getattr(request.app.state, "arq_pool", None),
+        offer_buttons=offer_buttons,
     )
     await session.refresh(appt)
 
```

```diff
--- a/src/secretaria/services/appointment_release.py
+++ b/src/secretaria/services/appointment_release.py
@@ -26,7 +26,7 @@
 
 from secretaria.core.logging import get_logger
 from secretaria.models import Appointment, MessageSender, Patient, Tenant
-from secretaria.services import cancellation_notice
+from secretaria.services import cancellation_notice, release_offer
 from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, BrainMessageSender
 from secretaria.services.staff_patient_message import (  # noqa: F401 - NOTICE_* re-exported
     EMAIL_SENT,
@@ -62,8 +62,15 @@
     allow_paid: bool,
     arq_pool,
     now: datetime | None = None,
+    offer_buttons: list[tuple[str, str]] | None = None,
 ) -> str:
-    """Tell the patient; returns one `NOTICE_*` code. Never raises."""
+    """Tell the patient; returns one `NOTICE_*` code. Never raises.
+
+    `offer_buttons` (TASK-052): the paid deposit's "Receber agora" / "Remarcar" card. On the
+    Portal it replaces the plain "write to me" line; on WhatsApp the job reads the open offer
+    itself (`workers/whatsapp/notifications.py::send_cancellation_notice`), so the queued
+    arguments do not change and an older worker keeps working.
+    """
     # Read once, up front: a rollback inside the Portal path expires the ORM rows,
     # and the error logging below must not trigger a lazy load.
     appointment_id = appointment.id
@@ -73,7 +80,14 @@
             return NOTICE_NO_CHANNEL
         if patient.channel == CHANNEL_BRAIN_MESSAGE:
             return await _notify_portal(
-                session, tenant, appointment, patient, professional_name, reason, deposit_notice
+                session,
+                tenant,
+                appointment,
+                patient,
+                professional_name,
+                reason,
+                deposit_notice,
+                offer_buttons=offer_buttons,
             )
 
         to = patient.wa_id or appointment.phone
@@ -121,21 +135,29 @@
     professional_name: str | None,
     reason: str | None,
     deposit_notice: str | None,
+    *,
+    offer_buttons: list[tuple[str, str]] | None = None,
 ) -> str:
     appointment_id = appointment.id  # before a rollback can expire the row
     conversation_id = await conversation_id_for(session, tenant.id, appointment, patient)
     if conversation_id is None:
         return NOTICE_NO_CHANNEL
-    text = cancellation_notice.join_blocks(
-        cancellation_notice.build_cancellation_text(professional_name, reason),
-        deposit_notice,
-        PORTAL_REBOOK_LINE,
-    )
+    head = cancellation_notice.build_cancellation_text(professional_name, reason)
     try:
         sender = BrainMessageSender(
             conversation_id=conversation_id, session=session, author=MessageSender.HUMAN
         )
-        await sender.send_text_message(to=patient.external_id or "", body=text)
+        if offer_buttons and deposit_notice:
+            # TASK-052: the two buttons ARE the way back; the card is recorded with its options,
+            # which is what lets the Portal accept the tap (workers/portal/inbound.py).
+            await sender.send_buttons(
+                to=patient.external_id or "",
+                body=release_offer.fit_body(head, deposit_notice),
+                buttons=offer_buttons,
+            )
+        else:
+            text = cancellation_notice.join_blocks(head, deposit_notice, PORTAL_REBOOK_LINE)
+            await sender.send_text_message(to=patient.external_id or "", body=text)
         await session.commit()
     except Exception as exc:
         await session.rollback()
```

- [ ] **Step 4: Rodar e ver passar**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_hub_release.py tests/test_deposit_release_preview.py tests/test_hub_cancel_portal_notice.py tests/test_hub_calendar_money.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/api/hub/calendar.py src/secretaria/services/appointment_release.py tests/test_hub_release.py`
Expected: PASS (`test_hub_release.py`: 37 — eram 36), ruff limpo. `preview_cancellation_outcome`/`release_warning_text` continuam existindo (usados pelo cancelamento e pelos seus testes).

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/api/hub/calendar.py src/secretaria/services/appointment_release.py tests/test_hub_release.py
git commit -m "feat(calendar): releasing a paid slot opens the patient's refund-or-reschedule offer instead of retaining (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 4: O job do WhatsApp manda "Receber agora" / "Remarcar" (mesma assinatura)

**Files:**
- Modify: `src/secretaria/workers/whatsapp/notifications.py` (`send_cancellation_notice`)
- Test: `tests/test_cancellation_notice.py` (2 testes novos)

**Interfaces:**
- Consumes: Task 2 (`find_offer`, `offer_buttons`, `fit_body`, `offer_text`), `OPEN_OFFER_STATES` (Task 1).
- Produces: dentro da janela e com oferta aberta + `extra_notice`: `send_buttons(body=fit_body(cabeçalho, extra_notice), buttons=offer_buttons(id))`; fora da janela: o modelo aprovado de sempre (`rebook_payloads`).

- [ ] **Step 1: Escrever os testes**

```diff
--- a/tests/test_cancellation_notice.py
+++ b/tests/test_cancellation_notice.py
@@ -49,10 +49,15 @@
     MessageDirection,
     MessageSender,
     Patient,
+    PixDeposit,
+    PixDepositStatus,
     Professional,
     Tenant,
 )
-from secretaria.services import cancellation_notice as notice  # noqa: E402
+from secretaria.services import (
+    cancellation_notice as notice,  # noqa: E402
+    release_offer,  # noqa: E402
+)
 from secretaria.services.tenant_config import set_google_refresh_token  # noqa: E402
 from secretaria.workers import tasks  # noqa: E402
 
@@ -794,6 +799,60 @@
     assert "retido pela clínica" in wa.buttons[0]["body"]
 
 
+async def _open_paid_offer(db, appt) -> None:
+    async with db() as session:
+        row = await session.get(Appointment, appt.id)
+        row.status = AppointmentStatus.CANCELLED
+        deposit = PixDeposit(
+            id=uuid4(),
+            tenant_id=row.tenant_id,
+            appointment_id=row.id,
+            patient_id=row.patient_id,
+            asaas_payment_id=f"pay_{uuid4().hex[:8]}",
+            amount_cents=6000,
+            percent_applied=30,
+            status=PixDepositStatus.PAID,
+        )
+        session.add(deposit)
+        await session.flush()
+        tenant_row = await session.get(Tenant, row.tenant_id)
+        await release_offer.open_offer(session, tenant=tenant_row, appointment=row, deposit=deposit)
+        await session.commit()
+
+
+async def test_a_released_paid_slot_carries_the_choice_instead_of_the_rebooking_buttons(
+    db, tenant, wa, metered
+):
+    """TASK-052: same job, same arguments; the open offer decides the card."""
+    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=2))
+    await _open_paid_offer(db, appt)
+    offer_text = release_offer.offer_text(PixDeposit(amount_cents=6000))
+
+    await _run(appt, justification="x" * 1500, extra=offer_text)
+
+    [sent] = wa.buttons
+    assert [bid for bid, _ in sent["buttons"]] == [
+        f"relrefund|{appt.id}",
+        f"relresched|{appt.id}",
+    ]
+    assert sent["body"].endswith(offer_text) and len(sent["body"]) <= 1024
+    assert "Quer remarcar?" not in sent["body"]
+
+
+async def test_outside_the_window_the_paid_template_keeps_its_rebooking_payloads(
+    db, tenant, wa, metered
+):
+    """No approved template carries the two new buttons; the rebook taps are mapped to the
+    offer by the worker (workers/shared/actions.py), and the 24-hour sweep covers silence."""
+    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=30))
+    await _open_paid_offer(db, appt)
+
+    await _run(appt, extra="offer", allow_paid=True)
+
+    [sent] = wa.templates
+    assert sent["button_payloads"] == notice.rebook_payloads(appt.id)
+
+
 async def test_an_appointment_from_another_tenant_is_never_notified(db, tenant, wa, metered):
     """Isolation guard on the job itself, not just on the endpoint."""
     appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=2))
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_cancellation_notice.py -q -p no:cacheprovider`
Expected: FAIL em `test_a_released_paid_slot_carries_the_choice_instead_of_the_rebooking_buttons` (os botões ainda são `rebooksame|…`).

- [ ] **Step 3: Implementar**

```diff
--- a/src/secretaria/workers/whatsapp/notifications.py
+++ b/src/secretaria/workers/whatsapp/notifications.py
@@ -13,7 +13,8 @@
     Patient,
     Tenant,
 )
-from secretaria.services import cancellation_notice
+from secretaria.models.deposit_release_offer import OPEN_OFFER_STATES
+from secretaria.services import cancellation_notice, release_offer
 from secretaria.services.email import (
     send_cancellation_escalation_alert,
 )
@@ -137,6 +138,13 @@
                 )
         to = to or appointment.phone
         waba_token = await get_waba_token(session, tenant.id)
+        # TASK-052: a slot the clinic released with a PAID deposit carries the patient's
+        # choice ("Receber agora" / "Remarcar") instead of the rebooking buttons. Read here,
+        # by the job, so the API's queued arguments never changed (old worker = old card).
+        offer = await release_offer.find_offer(
+            session, tenant_id=tenant.id, released_appointment_id=appointment.id
+        )
+        offer_open = offer is not None and offer.state in OPEN_OFFER_STATES
 
     if not to:
         logger.info(
@@ -163,14 +171,19 @@
         )
         return
 
-    body = cancellation_notice.build_cancellation_text(professional_name, justification)
-    if extra_notice:
-        body = cancellation_notice.join_blocks(body, extra_notice)
+    head = cancellation_notice.build_cancellation_text(professional_name, justification)
+    body = cancellation_notice.join_blocks(head, extra_notice) if extra_notice else head
 
     client = WhatsAppClient.for_tenant(tenant, waba_token)
     settings = get_settings()
     try:
-        if inside:
+        if inside and offer_open and extra_notice:
+            await client.send_buttons(
+                to=to,
+                body=release_offer.fit_body(head, extra_notice),
+                buttons=release_offer.offer_buttons(appointment_id),
+            )
+        elif inside:
             await client.send_buttons(
                 to=to,
                 body=cancellation_notice.join_blocks(
```

- [ ] **Step 4: Rodar e ver passar**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_cancellation_notice.py tests/test_workers_layering.py tests/test_build_identity.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/workers/whatsapp/notifications.py tests/test_cancellation_notice.py`
Expected: PASS (`test_cancellation_notice.py`: 37 — eram 35), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/workers/whatsapp/notifications.py tests/test_cancellation_notice.py
git commit -m "feat(whatsapp): the release notice of a paid slot carries Receber agora / Remarcar, same job arguments (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 5: A lista de dias mostra dias com horários reservados, e a escolha do dia diz quais

**Files:**
- Modify: `src/secretaria/services/availability.py` (`held_slots` + nota do módulo), `src/secretaria/services/flow_router.py` (`enter_day_picker` sem `holds=`; `_enter_slot_picker` com o aviso; `HELD_SLOTS_NOTICE`, `ALL_HELD_NOTICE`, `HELD_TIMES_SHOWN`, `held_times_text`)
- Test: `tests/test_before_booking_flow.py` (o teste do dia escondido vira o do dia mostrado), `tests/test_availability.py` (2 novos)

**Interfaces:**
- Produces: `availability.held_slots(slots, holds, *, duration_minutes, tz) -> list[dict]`; `flow_router.held_times_text(labels: list[str]) -> str`; `HELD_SLOTS_NOTICE`/`ALL_HELD_NOTICE` (com `{times}`). O dia todo reservado responde a lista "Ver opções" só com "Outro dia" (e voltar), no `slot_step` do ramo.

- [ ] **Step 1: Escrever os testes**

```diff
--- a/tests/test_before_booking_flow.py
+++ b/tests/test_before_booking_flow.py
@@ -303,7 +303,9 @@
     assert "está confirmado" in result.bubbles[0].body
 
 
-async def test_the_day_picker_treats_held_windows_as_busy_time(db, env):
+async def test_a_fully_held_day_is_offered_and_says_which_times_are_held(db, env):
+    """TASK-052 (owner decision 2026-10-11) reverts the TASK-050 port that hid it: the day is
+    offered (an unpaid Pix may expire), and picking it lists the held TIMES - never who."""
     from secretaria.models import Patient
 
     async with db() as session:
@@ -359,9 +361,22 @@
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

```diff
--- a/tests/test_availability.py
+++ b/tests/test_availability.py
@@ -206,6 +206,30 @@
     assert [f["hidden"] for e, f in log.events if e == "flow_slots_hidden_by_hold"] == [1]
 
 
+async def test_a_partly_held_day_lists_the_free_times_under_the_held_ones():
+    """TASK-052: the held times are named above the list (times only), the rest is tappable."""
+    res = await fr.route(
+        _at_day_step(),
+        _tenant(),
+        _FakeCalendar(slots=_SLOTS),
+        _DAY_TAP,
+        gate=_Gate({None: [_HELD_0800]}),
+    )
+    (bubble,) = res.bubbles
+    assert bubble.body == (
+        fr.HELD_SLOTS_NOTICE.format(times="08:00") + "\n\nHorários livres em 05/10:"
+    )
+    assert [row[0] for row in bubble.rows][:1] == ["slot|2026-10-05T08:40"]
+
+
+def test_held_times_are_listed_plainly_and_capped():
+    assert fr.held_times_text(["08:00"]) == "08:00"
+    assert fr.held_times_text(["08:00", "08:30"]) == "08:00 e 08:30"
+    assert fr.held_times_text(["08:00", "08:30", "09:00"]) == "08:00, 08:30 e 09:00"
+    many = [f"{h:02d}:00" for h in range(8, 19)]
+    assert fr.held_times_text(many).endswith("e mais 3")
+
+
 async def test_a_sole_professionals_hold_is_looked_up_by_their_id():
     """Holds are PLACED with the sole professional as owner (`resolve_booking_owner_id`);
     the picker used to look them up under None and never saw them."""
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_before_booking_flow.py tests/test_availability.py -q -p no:cacheprovider`
Expected: FAIL (`flow_state == MENU` com `NO_AVAILABLE_DAYS_MESSAGE`; `held_times_text`/`HELD_SLOTS_NOTICE` inexistentes).

- [ ] **Step 3: Implementar**

```diff
--- a/src/secretaria/services/availability.py
+++ b/src/secretaria/services/availability.py
@@ -13,9 +13,11 @@
 Nothing here returns an event - only instants. That is the contract the AI tool relies
 on: no title, attendee or event id ever leaves the calendar layer through this module.
 
-The day picker passes no holds, on purpose: it has always listed every day with free
-Google time and let the slot step hide the held slots, and it must keep rendering byte
-for byte (tests/test_availability.py). The AI readers pass the holds.
+The day picker passes no holds, on purpose: it lists every day with free Google time and
+lets the slot step hide the held slots AND say which times are held (`held_slots`), so a
+patient can see that a day may free up when an unpaid Pix expires (TASK-052, owner decision
+2026-10-11, reverting the TASK-050 port that hid fully held days). The AI readers pass the
+holds.
 """
 
 from __future__ import annotations
@@ -76,6 +78,21 @@
     return kept
 
 
+def held_slots(
+    slots: Sequence[dict],
+    holds: Sequence[Window],
+    *,
+    duration_minutes: int,
+    tz: tzinfo | None,
+) -> list[dict]:
+    """The complement of `without_holds`: the slots another conversation is holding."""
+    if not holds:
+        return []
+    free = without_holds(slots, holds, duration_minutes=duration_minutes, tz=tz)
+    kept = {id(slot) for slot in free}
+    return [slot for slot in slots if id(slot) not in kept]
+
+
 async def free_slots_for_day(
     calendar: CalendarService,
     *,
```

Em `src/secretaria/services/flow_router.py` (hunks da lista de dias e de horários; o hunk do `enter_rebooking` é da Task 6):

```diff
--- a/src/secretaria/services/flow_router.py
+++ b/src/secretaria/services/flow_router.py
@@ -75,6 +75,7 @@
 )
 from secretaria.services.availability import (
     available_day_starts,
+    held_slots,
     slot_scan_limit,
     without_holds,
 )
@@ -468,6 +469,31 @@
 # The day the patient picked filled up between the two taps (someone else
 # booked it). Deterministic answer: the same picker, refreshed.
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
 NO_AVAILABLE_DAYS_MESSAGE = (
     "Não encontrei horários livres nos próximos dias. "
     "Escolha uma opção abaixo e a gente te ajuda."
@@ -3384,12 +3410,9 @@
     step = step or branch.day_step
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
         start = datetime.now(calendar.tzinfo)
         if anchor is not None:
@@ -3400,7 +3423,6 @@
             start=start,
             window_days=DAY_PICKER_WINDOW_DAYS,
             duration_minutes=duration_minutes,
-            holds=reserved,
         )
         if anchor is not None:
             # The days NEAREST the current one come first (ties: the earlier day), so
@@ -3502,8 +3524,12 @@
     # both branches (first booking and reschedule) - a reservation half the
     # surfaces ignore is not a reservation. The patient's own hold is excluded
     # upstream, so their own choice stays visible to them.
+    held: list[dict] = []
     if reserved:
         before = len(slots)
+        held = held_slots(
+            slots, reserved, duration_minutes=duration_minutes, tz=calendar.tzinfo
+        )
         slots = without_holds(
             slots, reserved, duration_minutes=duration_minutes, tz=calendar.tzinfo
         )
@@ -3516,6 +3542,32 @@
         # Reserve the final two rows for the day and service navigation controls.
         slots = slots[:SLOT_PICKER_MAX_SLOTS]
 
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
             conversation,
@@ -3541,7 +3593,12 @@
         action="reply",
         bubbles=[
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

- [ ] **Step 4: Rodar e ver passar**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_before_booking_flow.py tests/test_availability.py tests/test_rebooking_flow.py tests/test_booking_gate_deposit.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/services/availability.py src/secretaria/services/flow_router.py tests/test_before_booking_flow.py tests/test_availability.py`
Expected: PASS (`test_availability.py`: 11 — eram 9), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/services/availability.py src/secretaria/services/flow_router.py tests/test_before_booking_flow.py tests/test_availability.py
git commit -m "feat(flow): offer days whose free times are held and name the held times when the day is picked (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 6: Os toques "Receber agora" / "Remarcar" (WhatsApp e Portal)

**Files:**
- Create: `src/secretaria/workers/shared/release_offer_actions.py`
- Modify: `src/secretaria/schemas/webhook.py` (`_ACTION_BUTTON_PREFIXES`), `src/secretaria/workers/portal/inbound.py` (ação aceita), `src/secretaria/workers/shared/actions.py` (chama os toques antes de tudo; prefixo no "Outro horário"/"Outro médico"), `src/secretaria/services/flow_router.py` (`enter_rebooking`: o ramo "mesmo médico" passa o `prefix`)
- Test: `tests/test_release_offer_buttons.py` (novo)

**Interfaces:**
- Consumes: Task 2 (`find_offer`, `choose_refund`, `choose_reschedule`, `deadline_passed`, textos, `RELEASE_OFFER_ACTIONS`); `_reply_sender`, `get_waba_token`, `async_session_factory`.
- Produces: `release_offer_actions.REBOOK_ACTIONS = ("rebooksame", "rebookother", "rebookno")`; `async handle_release_offer_tap(reply, action, appointment_id: UUID) -> tuple[str, str | None] | None` (None = respondido; senão `(ação a seguir, frase do topo)`).

- [ ] **Step 1: Escrever o teste que falha**

```python
"""The patient's taps on a slot the clinic released with a PAID deposit (TASK-052).

WhatsApp and the Portal reach the same handler (workers/shared/release_offer_actions.py through
`_handle_action_button`). "Receber agora" opens ONE `released_refund_due` item and says the
CLINIC returns the money within 24 hours (D15: the platform never refunds); "Remarcar" opens the
day list of "Outro horário" with the line saying the paid deposit carries over. A tap on someone
else's offer, or another clinic's, is answered like an unknown appointment and changes nothing.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    FlowState,
    Patient,
    PaymentAttentionItem,
    PixDeposit,
)
from secretaria.models.deposit_release_offer import DepositReleaseOffer
from secretaria.services import release_offer
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, BrainMessageSender
from secretaria.services.flow_router import STEP_DECLINE_REASON
from secretaria.workers import tasks
from secretaria.workers.orchestrator import _send_bot_reply
from secretaria.workers.portal.inbound import _persist_brain_message_inbound
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import consent, sent, wire
from tests._reminders_v2 import outbound_messages, seed_paid_deposit, seed_world


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


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
        await release_offer.open_offer(
            session, tenant=tenant, appointment=appointment, deposit=deposit
        )
        await session.commit()
    return world


def _reply(world, conversation_id=None, channel=None):
    kwargs = {} if channel is None else {"channel": channel}
    return tasks._ReplyContext(
        conversation_id=conversation_id or world.conversation.id,
        patient_ref=world.patient.external_id or world.patient.wa_id,
        inbound_body="",
        **kwargs,
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


def _texts() -> list[str]:
    return [entry[2] for entry in sent() if entry[0] == "text"]


async def test_receber_agora_asks_the_clinic_once_and_says_so(db):  # noqa: F811
    world = await _released(db)

    await tasks._handle_action_button(_reply(world), "relrefund", str(world.appointment.id))
    await tasks._handle_action_button(_reply(world), "relrefund", str(world.appointment.id))

    first, second = _texts()
    assert "Avisamos a clínica" in first and "R$ 100,00" in first
    assert "em até 24 horas" in first
    assert second == release_offer.TAP_REFUND_ALREADY_TEXT
    [item] = await _items(db)
    assert item.reason == "released_refund_due" and item.suggested_refund_cents == 10000
    offer = await _offer(db, world)
    assert (offer.state, offer.refund_source) == ("refund", "patient")


async def test_remarcar_opens_the_day_list_with_the_carry_over_line(db):  # noqa: F811
    world = await _released(db)

    await tasks._handle_action_button(_reply(world), "relresched", str(world.appointment.id))

    kind, _to, body, _label, rows, _section = sent()[-1]
    assert kind == "list" and body.startswith(release_offer.RESCHEDULE_PREFIX)
    assert rows[0][0].startswith("day|")
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
    assert conversation.flow_state == FlowState.SERVICE_CATALOG
    assert conversation.flow_selected_type == "Consulta"
    assert (await _offer(db, world)).state == "rescheduling"
    assert await _items(db) == []


async def test_remarcar_after_the_refund_is_a_plain_rebooking_and_says_why(db):  # noqa: F811
    world = await _released(db)
    await tasks._handle_action_button(_reply(world), "relrefund", str(world.appointment.id))

    await tasks._handle_action_button(_reply(world), "relresched", str(world.appointment.id))

    kind, _to, body, *_ = sent()[-1]
    assert kind == "list" and body.startswith(release_offer.RESCHEDULE_AFTER_REFUND_PREFIX)
    assert (await _offer(db, world)).state == "refund"


async def test_a_tap_past_the_deadline_settles_the_refund_before_answering(db):  # noqa: F811
    world = await _released(db)
    async with db() as session:
        offer = await session.scalar(select(DepositReleaseOffer))
        offer.deadline_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    await tasks._handle_action_button(_reply(world), "relresched", str(world.appointment.id))

    offer = await _offer(db, world)
    assert (offer.state, offer.refund_source) == ("refund", "timeout")
    assert len(await _items(db)) == 1


async def test_the_rebook_buttons_of_the_paid_template_answer_the_offer(db):  # noqa: F811
    world = await _released(db)

    await tasks._handle_action_button(_reply(world), "rebooksame", str(world.appointment.id))
    assert (await _offer(db, world)).state == "rescheduling"

    await tasks._handle_action_button(_reply(world), "rebookno", str(world.appointment.id))
    assert (await _offer(db, world)).state == "refund"
    assert len(await _items(db)) == 1
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
    assert conversation.flow_step == STEP_DECLINE_REASON  # and the "why" list still follows


async def test_another_patients_tap_is_an_unknown_appointment_and_changes_nothing(db):  # noqa: F811
    world = await _released(db)
    async with db() as session:
        intruder = Patient(id=uuid4(), tenant_id=world.tenant.id, wa_id="5511900000009")
        session.add(intruder)
        await session.flush()
        theirs = Conversation(id=uuid4(), tenant_id=world.tenant.id, patient_id=intruder.id)
        session.add(theirs)
        await session.commit()

    await tasks._handle_action_button(
        _reply(world, conversation_id=theirs.id), "relrefund", str(world.appointment.id)
    )

    assert _texts()[-1] == "Não encontrei essa consulta."
    assert (await _offer(db, world)).state == "open" and await _items(db) == []


async def test_another_clinics_offer_is_never_reached(db):  # noqa: F811
    world = await _released(db)
    other = await seed_world(db, phone_number_id="pnid-2", wa_id="5511900000002")

    await tasks._handle_action_button(
        _reply(other, conversation_id=other.conversation.id), "relrefund", str(world.appointment.id)
    )

    assert (await _offer(db, world)).state == "open" and await _items(db) == []


async def test_a_portal_tap_on_the_recorded_card_is_the_same_action(db):  # noqa: F811
    world = await _released(db, channel=CHANNEL_BRAIN_MESSAGE)
    await consent(db, world)
    async with db() as session:
        sender = BrainMessageSender(conversation_id=world.conversation.id, session=session)
        await sender.send_buttons(
            to=world.patient.external_id,
            body="O médico desmarcou a sua consulta!",
            buttons=release_offer.offer_buttons(world.appointment.id),
        )
        await session.commit()

    reply = await _persist_brain_message_inbound(
        tenant_id=world.tenant.id,
        external_id=world.patient.external_id,
        text="Receber agora",
        interactive_reply_id=f"relrefund|{world.appointment.id}",
    )
    await _send_bot_reply(reply)

    assert reply.action_button == ("relrefund", str(world.appointment.id))
    assert (await _offer(db, world)).state == "refund"
    bodies = [m.body for m in await outbound_messages(db, world.conversation.id)]
    assert any("Avisamos a clínica" in body for body in bodies)


async def test_a_portal_id_never_shown_on_a_card_is_not_an_action(db):  # noqa: F811
    world = await _released(db, channel=CHANNEL_BRAIN_MESSAGE)
    await consent(db, world)

    reply = await _persist_brain_message_inbound(
        tenant_id=world.tenant.id,
        external_id=world.patient.external_id,
        text="Receber agora",
        interactive_reply_id=f"relrefund|{world.appointment.id}",
    )

    assert reply.action_button is None
    assert (await _offer(db, world)).state == "open"
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_release_offer_buttons.py -q -p no:cacheprovider`
Expected: FAIL (`relrefund|`/`relresched|` ainda não são ids de ação: o toque não muda a oferta nem responde o texto da devolução; no Portal o id não vira ação).

- [ ] **Step 3: Implementar**

`src/secretaria/workers/shared/release_offer_actions.py`:

```python
"""release_offer_actions - the patient's taps on a released PAID slot (TASK-052).

Channel-neutral (WhatsApp and Portal reach it through `actions._handle_action_button`, which
calls it before anything else for these ids). Two families arrive here:

* the offer card itself, "relrefund|<released appointment>" ("Receber agora") and
  "relresched|<released appointment>" ("Remarcar");
* the rebooking buttons of the cancellation notice ("rebooksame|" / "rebookother|" /
  "rebookno|") when the released appointment has an OPEN offer - that is the card a patient
  outside WhatsApp's 24-hour window gets (the approved template carries only those), so "Outro
  horário"/"Outro médico" mean "Remarcar" and "Cancelar" means "Receber agora".

Returns None when the tap was fully answered here, or `(action, prefix)` for the caller to go
on with: "rebooksame"/"rebookother"/"rebookno" plus the line to put above the day list. A
tap whose offer is missing, belongs to another patient or another clinic is answered exactly
like an unknown appointment - nothing is revealed and nothing changes.
"""

from datetime import UTC, datetime
from uuid import UUID

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Conversation, PixDeposit, Tenant
from secretaria.models.deposit_release_offer import (
    REFUND_SOURCE_TIMEOUT,
    STATE_OPEN,
    STATE_REBOOKED,
    STATE_REFUND,
    DepositReleaseOffer,
)
from secretaria.services import release_offer
from secretaria.services.payments.money import format_brl
from secretaria.services.tenant_config import get_waba_token
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.sender import _reply_sender

logger = get_logger(__name__)

REBOOK_ACTIONS: tuple[str, ...] = ("rebooksame", "rebookother", "rebookno")
_NOT_FOUND_TEXT = "Não encontrei essa consulta."


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
        offer = await release_offer.find_offer(
            session, tenant_id=tenant.id, released_appointment_id=appointment_id
        )
        if offer is None or offer.patient_id != conversation.patient_id:
            if action in REBOOK_ACTIONS:
                return action, None  # no offer: today's rebooking, untouched
            client = _reply_sender(reply, tenant, await get_waba_token(session, tenant.id))
            if client is not None:
                await client.send_text_message(to=reply.patient_ref, body=_NOT_FOUND_TEXT)
            return None

        now = datetime.now(UTC)
        if offer.state == STATE_OPEN and release_offer.deadline_passed(offer, now):
            # The sweep has not run yet: settle it now, exactly as the sweep would.
            await release_offer.choose_refund(
                session, offer=offer, source=REFUND_SOURCE_TIMEOUT, now=now
            )
            await session.commit()
            await session.refresh(offer)

        if action in (release_offer.ACTION_REFUND, "rebookno"):
            won = await release_offer.choose_refund(session, offer=offer, now=now)
            await session.commit()
            await session.refresh(offer)
            deposit = await session.get(PixDeposit, offer.deposit_id)
            if won:
                text = release_offer.TAP_REFUND_TEXT.format(
                    amount=format_brl(deposit.amount_cents if deposit else 0)
                )
            elif offer.state == STATE_REBOOKED:
                text = release_offer.TAP_ALREADY_REBOOKED_TEXT
            else:
                text = release_offer.TAP_REFUND_ALREADY_TEXT
            client = _reply_sender(reply, tenant, await get_waba_token(session, tenant.id))
            if client is not None:
                await client.send_text_message(to=reply.patient_ref, body=text)
            logger.info(
                "deposit_release_offer_tap",
                tenant_id=str(tenant.id),
                offer_id=str(offer.id),
                choice="refund",
                won=won,
            )
            return ("rebookno", None) if action == "rebookno" else None

        # "Remarcar" (or "Outro horário"/"Outro médico" on an offer).
        follow = "rebooksame" if action == release_offer.ACTION_RESCHEDULE else action
        moving = await release_offer.choose_reschedule(
            session, offer=offer, conversation_id=conversation.id, now=now
        )
        await session.commit()
        logger.info(
            "deposit_release_offer_tap",
            tenant_id=str(tenant.id),
            offer_id=str(offer.id),
            choice="reschedule",
            won=moving,
        )
        if moving:
            return follow, release_offer.RESCHEDULE_PREFIX
        current = await session.get(DepositReleaseOffer, offer.id, populate_existing=True)
        if current is not None and current.state == STATE_REBOOKED:
            client = _reply_sender(reply, tenant, await get_waba_token(session, tenant.id))
            if client is not None:
                await client.send_text_message(
                    to=reply.patient_ref, body=release_offer.TAP_ALREADY_REBOOKED_TEXT
                )
            return None
        if current is not None and current.state == STATE_REFUND:
            return follow, release_offer.RESCHEDULE_AFTER_REFUND_PREFIX
        return follow, None
```

```diff
--- a/src/secretaria/schemas/webhook.py
+++ b/src/secretaria/schemas/webhook.py
@@ -704,6 +704,11 @@
     "remgiveup|",
     "remgiveupyes|",
     "remkeep|",
+    # TASK-052: the choice on a slot the clinic released with a PAID deposit. The trailing id
+    # is the RELEASED appointment's; workers/shared/release_offer_actions.py honours it only
+    # for the patient of the offer, in the tapping conversation's clinic.
+    "relrefund|",
+    "relresched|",
 )
 
 
```

```diff
--- a/src/secretaria/workers/portal/inbound.py
+++ b/src/secretaria/workers/portal/inbound.py
@@ -22,6 +22,7 @@
 from secretaria.services.channel_sender import (
     CHANNEL_BRAIN_MESSAGE,
 )
+from secretaria.services.release_offer import RELEASE_OFFER_ACTIONS
 from secretaria.services.reminder_text import REMINDER_ROW_ACTIONS
 from secretaria.workers.orchestrator import (
     _send_bot_reply,
@@ -243,7 +244,11 @@
                 # Older action ids keep today's title routing on this channel.
                 action_button = None
                 decoded = decode_action_id(interactive_reply_id) if interactive_reply_id else None
-                if decoded is not None and decoded[0] in REMINDER_ROW_ACTIONS:
+                if decoded is not None and (
+                    decoded[0] in REMINDER_ROW_ACTIONS or decoded[0] in RELEASE_OFFER_ACTIONS
+                ):
+                    # TASK-052: "Receber agora" / "Remarcar" of a released PAID slot is an
+                    # ACTION on this channel too (the card was recorded with its options).
                     action_button = decoded
 
                 return await _route_inbound_turn(
```

```diff
--- a/src/secretaria/workers/shared/actions.py
+++ b/src/secretaria/workers/shared/actions.py
@@ -44,6 +44,7 @@
     load_upcoming_appointments,
 )
 from secretaria.services.payments import deposit_lifecycle
+from secretaria.services.release_offer import RELEASE_OFFER_ACTIONS
 from secretaria.services.reminder_text import REMINDER_ROW_ACTIONS
 from secretaria.services.service_catalog import (
     load_service_catalog,
@@ -73,6 +74,10 @@
     _appointment_calendar,
     _appointment_calendar_target,
 )
+from secretaria.workers.shared.release_offer_actions import (
+    REBOOK_ACTIONS,
+    handle_release_offer_tap,
+)
 from secretaria.workers.shared.reminder_actions import (
     CONTINUE_CANCEL_AND_ASK_WHY,
     CONTINUE_EDIT,
@@ -233,6 +238,17 @@
         action, appointment_id = continuation
         appt_uuid = UUID(appointment_id)
 
+    # TASK-052: a slot the clinic released with a PAID deposit. "Receber agora" is answered
+    # there; "Remarcar" (and a rebook tap on an open offer) comes back as "rebooksame"/
+    # "rebookother" with the line for the day list, and the paid deposit is reserved for the
+    # booking that tap ends in (services/release_offer.py).
+    offer_prefix: str | None = None
+    if action in RELEASE_OFFER_ACTIONS or action in REBOOK_ACTIONS:
+        offer_tap = await handle_release_offer_tap(reply, action, appt_uuid)
+        if offer_tap is None:
+            return
+        action, offer_prefix = offer_tap
+
     # Set only on the "enter the reschedule sub-flow" path (apptresched,
     # under the limit) - handled AFTER this session closes, mirroring
     # _handle_manage_appointment's own short-read-session-then-handoff shape.
@@ -431,18 +447,19 @@
             same = action == "rebooksame"
             service_name = appointment.appointment_type
             candidates: list = []
-            prefix = None
+            prefix = offer_prefix
             if same:
                 target_id = appointment.professional_id
             else:
                 services = await load_service_catalog(session, tenant.id)
-                candidates, prefix = rebooking_candidates(
+                candidates, fallback_line = rebooking_candidates(
                     tenant,
                     professionals,
                     cancelled_professional_id=appointment.professional_id,
                     service_name=service_name,
                     services=services,
                 )
+                prefix = " ".join(line for line in (offer_prefix, fallback_line) if line) or None
                 # Only a single candidate fixes the agenda in advance; with a
                 # list the patient still picks, and their own calendar is
                 # resolved when they do.
```

Em `src/secretaria/services/flow_router.py` (`enter_rebooking`, ramo "mesmo médico"):

```diff
--- a/src/secretaria/services/flow_router.py
+++ b/src/secretaria/services/flow_router.py
@@ -5109,6 +5166,8 @@
             duration_minutes=duration,
             branch=BOOKING_DAY_BRANCH,
             professionals=professionals,
+            # TASK-052: "Remarcar" on a released PAID slot says the deposit carries over.
+            prefix=prefix,
         )
         # enter_day_picker knows nothing about who or what is being booked, and
         # these fields are written unconditionally by the caller — so a result
```

- [ ] **Step 4: Rodar e ver passar**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_release_offer_buttons.py tests/test_action_buttons.py tests/test_rebooking_flow.py tests/test_reminder_r3_portal.py tests/test_brain_message_interactive_tap.py tests/test_workers_layering.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/workers/shared/release_offer_actions.py src/secretaria/workers/shared/actions.py src/secretaria/schemas/webhook.py src/secretaria/workers/portal/inbound.py src/secretaria/services/flow_router.py tests/test_release_offer_buttons.py`
Expected: PASS (9 novos; os de botões, remarcação, Portal e camadas seguem verdes), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/workers/shared/release_offer_actions.py src/secretaria/workers/shared/actions.py src/secretaria/schemas/webhook.py src/secretaria/workers/portal/inbound.py src/secretaria/services/flow_router.py tests/test_release_offer_buttons.py
git commit -m "feat(flow): Receber agora opens the clinic's refund item, Remarcar enters day-then-time with the paid deposit reserved (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 7: A nova consulta leva o sinal pago (fluxo, promoção do Portal e portão do "antes")

**Files:**
- Modify: `src/secretaria/workers/shared/flow_runner.py` (`_apply_flow_result`), `src/secretaria/workers/shared/booking_hold.py` (`_promote_booking_hold`), `src/secretaria/services/booking_hold.py` (`BookingGate._deposit_quote`)
- Test: `tests/test_release_offer_carry_over.py` (novo)

**Interfaces:**
- Consumes: Task 2 (`claim_carry_over`, `carry_over_pending`, `CARRIED_OVER_NOTE`, `REASON_CARRIED_OVER`), `deposit_gate.DepositQuote`.
- Produces: a consulta criada numa conversa com oferta em `rescheduling` (dentro do prazo) recebe o sinal PAGO na mesma transação, e a última bolha (ou a confirmação do Portal) ganha `CARRIED_OVER_NOTE`; o portão do "antes" responde `no_deposit`/`carried_over` para essa conversa.

- [ ] **Step 1: Escrever o teste que falha**

```python
"""The booking that ends a "Remarcar" carries the released slot's PAID deposit (TASK-052).

The deposit moves to the new appointment in the booking's own transaction, without a new charge
and without touching `reschedule_count`; the `before` gate asks for no Pix while the offer is in
`rescheduling`; a booking made after the 24-hour sweep refunded the offer stands on its own.
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
    assert (offer.state, offer.rebooked_appointment_id) == ("rebooked", new.id)
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


async def test_a_booking_after_the_sweep_refunded_stands_on_its_own(db):  # noqa: F811
    world = await _rescheduling(db)
    async with db() as session:
        await release_offer.expire_offers(session, now=datetime.now(UTC) + timedelta(hours=25))

    await _confirm(world, _booking(_future(6)))

    new, deposit, offer, items = await _state(db)
    assert new is not None and deposit.appointment_id == world.appointment.id
    assert offer.state == "refund" and len(items) == 1
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

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_release_offer_carry_over.py -q -p no:cacheprovider`
Expected: FAIL (o sinal continua na consulta liberada; o portão do "antes" responde `blocked`/`no_credential`).

- [ ] **Step 3: Implementar**

```diff
--- a/src/secretaria/workers/shared/flow_runner.py
+++ b/src/secretaria/workers/shared/flow_runner.py
@@ -16,7 +16,7 @@
     Tenant,
 )
 from secretaria.plugins.post_booking import enqueue_post_booking_hooks
-from secretaria.services import reminder_hooks
+from secretaria.services import release_offer, reminder_hooks
 from secretaria.services.appointment_edit import EditDraft
 from secretaria.services.appointment_replacement import (
     ReplacedAppointment,
@@ -223,6 +223,8 @@
     persisted = True
     booked_appointment: Appointment | None = None
     cancellation_note: str | None = None
+    # TASK-052: the line that says a released slot's paid deposit now covers this booking.
+    carried_note: str | None = None
     # TASK-032 R3: the appointment a "Marcar outra" booking replaced, if any.
     replaced: ReplacedAppointment | None = None
     applied_edit: AppliedEdit | None = None
@@ -317,6 +319,17 @@
                             **result.appointment,
                         )
                         session.add(booked_appointment)
+                        # TASK-052: "Remarcar" on a slot the clinic released with a PAID deposit -
+                        # the deposit moves to THIS booking in this transaction (no new charge;
+                        # a booking that loses to the 24-hour sweep simply stands on its own).
+                        await session.flush()
+                        if await release_offer.claim_carry_over(
+                            session,
+                            tenant_id=conv.tenant_id,
+                            conversation_id=conv.id,
+                            new_appointment=booked_appointment,
+                        ):
+                            carried_note = release_offer.CARRIED_OVER_NOTE
                         # TASK-032 R3 ("Marcar outra consulta"): the original is
                         # cancelled HERE - same transaction as the new row, and
                         # only now that the new booking is confirmed. A failed
@@ -614,6 +627,8 @@
         if result.bubbles:
             if cancellation_note:
                 result.bubbles[-1].body = f"{result.bubbles[-1].body}\n\n{cancellation_note}"
+            if carried_note and persisted:
+                result.bubbles[-1].body = f"{result.bubbles[-1].body}\n\n{carried_note}"
             if replaced is not None and persisted and tenant is not None:
                 notice = _replaced_notice(tenant, replaced)
                 result.bubbles[-1].body = f"{result.bubbles[-1].body}\n\n{notice}"
```

```diff
--- a/src/secretaria/workers/shared/booking_hold.py
+++ b/src/secretaria/workers/shared/booking_hold.py
@@ -26,7 +26,7 @@
     Tenant,
 )
 from secretaria.plugins.post_booking import enqueue_post_booking_hooks
-from secretaria.services import reminder_hooks
+from secretaria.services import release_offer, reminder_hooks
 from secretaria.services.appointment_replacement import (
     ReplacedAppointment,
     cancel_replaced_appointment,
@@ -292,6 +292,7 @@
     )
     # TASK-032 R3: the appointment a "Marcar outra" booking replaced, if any.
     replaced: ReplacedAppointment | None = None
+    carried = None  # TASK-052: the paid deposit this booking carried over, if any
     try:
         async with async_session_factory() as session:
             async with session.begin():
@@ -302,6 +303,15 @@
                     session, tenant.id, held.insurance, held.professional_id
                 )
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
                 # The Portal booking is confirmed HERE (the code gate), so the
                 # original goes in this same transaction (spec §4.3).
                 conversation = await session.get(Conversation, reply.conversation_id)
@@ -366,6 +376,8 @@
     )
     if replaced is not None:
         confirmation = f"{confirmation}\n\n{_replaced_notice(tenant, replaced)}"
+    if carried is not None:
+        confirmation = f"{confirmation}\n\n{release_offer.CARRIED_OVER_NOTE}"
     return confirmation
 
 async def _release_hold(hold_id) -> None:
```

```diff
--- a/src/secretaria/services/booking_hold.py
+++ b/src/secretaria/services/booking_hold.py
@@ -696,6 +696,16 @@
                         )
                     )
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
                     session,
                     tenant,
```

- [ ] **Step 4: Rodar e ver passar**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_release_offer_carry_over.py tests/test_appointment_replacement_wiring.py tests/test_booking_gate_deposit.py tests/test_booking_code_gate.py tests/test_before_booking_flow.py tests/test_deposit_after_mode.py tests/test_workers_layering.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/shared/booking_hold.py src/secretaria/services/booking_hold.py tests/test_release_offer_carry_over.py`
Expected: PASS (5 novos), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/shared/booking_hold.py src/secretaria/services/booking_hold.py tests/test_release_offer_carry_over.py
git commit -m "feat(payments): the booking that ends a Remarcar carries the released slot's paid deposit, no new charge (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 8: O relógio de 24 horas na varredura de pagamentos

**Files:**
- Modify: `src/secretaria/workers/payments_cron.py` (`run_payment_sweeps`)
- Test: `tests/test_payment_cron_jobs.py` (1 novo)

**Interfaces:**
- Consumes: Task 2 (`expire_offers`).
- Produces: `run_payment_sweeps` fecha as ofertas vencidas (sessão própria) e loga `release_offers=<n>` em `payment_sweep_tick`. Nenhum cron novo (`test_build_identity.py` não muda).

- [ ] **Step 1: Escrever o teste**

```diff
--- a/tests/test_payment_cron_jobs.py
+++ b/tests/test_payment_cron_jobs.py
@@ -528,6 +528,35 @@
         assert name in names
 
 
+async def test_the_sweep_cron_also_closes_the_released_slots_nobody_answered(db, monkeypatch):
+    """TASK-052: the 24-hour timer of a released PAID slot rides the existing sweep."""
+    from secretaria.models import PaymentAttentionItem
+    from secretaria.models.deposit_release_offer import DepositReleaseOffer
+    from secretaria.services import release_offer
+    from tests.release_offer_support import make_released
+
+    monkeypatch.setattr(payments_cron, "async_session_factory", db)
+    async with db() as session:
+        released = await make_released(session)
+        offer = await release_offer.open_offer(
+            session,
+            tenant=released.tenant,
+            appointment=released.appointment,
+            deposit=released.deposit,
+            now=datetime.now(UTC) - timedelta(hours=25),
+        )
+        await session.commit()
+
+    await payments_cron.run_payment_sweeps({"redis": FakeRedis()})
+    await payments_cron.run_payment_sweeps({"redis": FakeRedis()})
+
+    async with db() as session:
+        closed = await session.get(DepositReleaseOffer, offer.id)
+        items = list(await session.scalars(select(PaymentAttentionItem)))
+    assert (closed.state, closed.refund_source) == ("refund", "timeout")
+    assert [item.reason for item in items] == ["released_refund_due"]
+
+
 async def test_the_expiry_cron_wrapper_runs_the_job_with_its_own_session(db, monkeypatch):
     monkeypatch.setattr(payments_cron, "async_session_factory", db)
     fake = _provider(monkeypatch, state=OPEN)
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_payment_cron_jobs.py -q -p no:cacheprovider`
Expected: FAIL (a oferta vencida continua `open`, nenhum item).

- [ ] **Step 3: Implementar**

```diff
--- a/src/secretaria/workers/payments_cron.py
+++ b/src/secretaria/workers/payments_cron.py
@@ -15,7 +15,10 @@
 # Imported for their side effects: `asaas_connection` registers the daily Asaas key recheck
 # into `jobs.DAILY_STEPS`; `deposit_wait` registers the `before` expiry hook that frees the
 # hold and tells the patient into `jobs.EXPIRY_HOOKS`.
-from secretaria.services import deposit_wait  # noqa: F401
+from secretaria.services import (
+    deposit_wait,  # noqa: F401
+    release_offer,
+)
 from secretaria.services.payments import (
     asaas_connection,  # noqa: F401
     attention_alerts,
@@ -63,8 +66,19 @@
             session, now=now, enqueue=enqueue_for(ctx)
         )
         notices = await jobs.recover_paid_notices(session, now=now)
-    if events or fulfillments or notices:
-        logger.info("payment_sweep_tick", events=events, fulfillments=fulfillments, notices=notices)
+    # TASK-052: a slot the clinic released with a PAID deposit, and no answer from the patient
+    # within 24 hours -> the clinic is asked to return it (one item per deposit, idempotent).
+    # Its own session, so a failure here never touches the three sweeps above.
+    async with async_session_factory() as session:
+        offers = await release_offer.expire_offers(session, now=now)
+    if events or fulfillments or notices or offers:
+        logger.info(
+            "payment_sweep_tick",
+            events=events,
+            fulfillments=fulfillments,
+            notices=notices,
+            release_offers=offers,
+        )
 
 
 async def run_attention_alerts(ctx: dict) -> None:
```

- [ ] **Step 4: Rodar e ver passar**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_payment_cron_jobs.py tests/test_build_identity.py -q -p no:cacheprovider ; uv run ruff check src/secretaria/workers/payments_cron.py tests/test_payment_cron_jobs.py`
Expected: PASS (`test_payment_cron_jobs.py`: 22 — eram 21), ruff limpo.

- [ ] **Step 5: Commit**

```powershell
git add src/secretaria/workers/payments_cron.py tests/test_payment_cron_jobs.py
git commit -m "feat(payments): the payment sweep closes unanswered release offers after 24 hours (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 9: As corridas em Postgres real (opcional, mas recomendada)

**Files:**
- Create: `tests/test_release_offer_postgres.py`

**Interfaces:**
- Consumes: Task 2; `tests/release_offer_support.py`.
- Produces: nenhum código de produção. Sem `PAYMENTS_TEST_PG_URL`, os 2 testes ficam `skipped`.

- [ ] **Step 1: Escrever o teste**

```python
"""Races SQLite cannot prove for the released-slot offer (TASK-052), on a REAL Postgres.

Opt-in: `PAYMENTS_TEST_PG_URL=postgresql+asyncpg://user@host:port/postgres` (a throwaway server;
each test creates and drops its own database). Proven under concurrent connections:

* N "Receber agora" taps + N runs of the 24-hour sweep on the SAME offer -> the offer closes
  once and the clinic gets exactly ONE attention item;
* N booking commits racing the sweep on an offer in `rescheduling` -> either the deposit moved
  to exactly one new appointment (and no item exists) or it was refunded once (and it stayed).
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from secretaria.core.database import Base
from secretaria.models import PaymentAttentionItem, PixDeposit
from secretaria.models.deposit_release_offer import DepositReleaseOffer
from secretaria.services import release_offer
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


async def _offer(maker, *, rescheduling: bool):
    now = datetime.now(UTC) - timedelta(hours=25)  # already past its deadline for the sweep
    async with maker() as session:
        released = await make_released(session)
        offer = await release_offer.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=now,
        )
        await session.commit()
    if rescheduling:
        async with maker() as session:
            row = await session.get(DepositReleaseOffer, offer.id)
            row.state = "rescheduling"
            row.conversation_id = released.conversation.id
            row.deadline_at = datetime.now(UTC) + timedelta(milliseconds=300)
            await session.commit()
    return released, offer


async def test_taps_and_sweeps_on_one_offer_open_exactly_one_item(maker):
    _released, offer = await _offer(maker, rescheduling=False)

    async def tap():
        async with maker() as session:
            row = await session.get(DepositReleaseOffer, offer.id)
            won = await release_offer.choose_refund(session, offer=row)
            await session.commit()
            return won

    async def sweep():
        async with maker() as session:
            return await release_offer.expire_offers(session)

    results = await asyncio.gather(
        *[tap() for _ in range(WORKERS)], *[sweep() for _ in range(WORKERS)]
    )

    taps, sweeps = results[:WORKERS], results[WORKERS:]
    assert sum(1 for won in taps if won) + sum(sweeps) == 1
    async with maker() as session:
        items = await session.scalar(select(func.count()).select_from(PaymentAttentionItem))
        state = (await session.get(DepositReleaseOffer, offer.id)).state
    assert items == 1 and state == "refund"


async def test_bookings_racing_the_sweep_carry_or_refund_never_both(maker):
    released, offer = await _offer(maker, rescheduling=True)

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
        await asyncio.sleep(0.3)
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
        assert state == "refund" and items == 1
        assert deposit.appointment_id == released.appointment.id
```

- [ ] **Step 2: Rodar contra um Postgres LOCAL descartável**

Run: `$env:PAYMENTS_TEST_PG_URL = "postgresql+asyncpg://postgres@127.0.0.1:<porta>/postgres"; BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_release_offer_postgres.py -q -p no:cacheprovider` (3 vezes seguidas)
Expected: PASS (2) nas três (na escrita: Postgres 18.4 local, 3 × 2 passed). Sem Postgres: `2 skipped` e "NÃO PROVADO em Postgres" no `TASK.md`. Nunca aponte para um banco que não seja descartável.

- [ ] **Step 3: Commit**

```powershell
git add tests/test_release_offer_postgres.py
git commit -m "test(payments): real-Postgres races for release offers - one refund item, carry or refund never both (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 10: Brain-Message-Frontend — o texto do aviso de liberar com sinal pago

O cartão "Liberar horário" da agenda diz hoje que liberar "pode fazer a clínica reter o valor pago" — falso depois das Tasks 3–8. Só texto; o pedido (`acknowledge_retention`) não muda de nome.

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

```diff
--- a/lib/agenda/__tests__/release.test.ts
+++ b/lib/agenda/__tests__/release.test.ts
@@ -12,6 +12,18 @@
   acknowledged: false, forceAck: false, confirmedSeen: false, notifyOutsideWindow: false, ...over,
 });
 
+describe("the paid-release warning (secretarIA TASK-052)", () => {
+  it("names the patient's two choices and the 24-hour return, and never says the clinic keeps it", () => {
+    expect(RELEASE_COPY.ackBody).toContain("remarcar usando o mesmo sinal");
+    expect(RELEASE_COPY.ackBody).toContain("24 horas");
+    expect(RELEASE_COPY.ackBody).toContain("a clínica deve devolver");
+    for (const text of [RELEASE_COPY.ackBody, RELEASE_COPY.ackLabel]) {
+      expect(text).not.toMatch(/retid|reter/i);
+      expect(text).not.toMatch(/(será|serão) (estornad|reembolsad|devolvid)/i);
+    }
+  });
+});
+
 describe("releaseNeedsAck / buildReleaseBody", () => {
   it("a PAID deposit needs the acknowledgement; no deposit / pending / refunded do not", () => {
     expect(releaseNeedsAck(paid)).toBe(true);
```

Run: `npx vitest run lib/agenda/__tests__/release.test.ts`
Expected: FAIL (`expected "Liberar o horário agora, dentro da janela…" to contain "remarcar usando o mesmo sinal"`).

- [ ] **Step 3: Implementar**

```diff
--- a/lib/agenda/release.ts
+++ b/lib/agenda/release.ts
@@ -1,12 +1,15 @@
 // lib/agenda/release.ts — the rules behind "Liberar horário" and "Enviar mensagem"
 // (TASK-032 R5). Pure, so the Pix-retention gate is testable without a DOM.
 //
-// The gate: releasing an appointment whose Pix deposit is PAID can make the clinic
-// retain the money (secretarIA services/payments/deposit_lifecycle.py). The clinic
-// must acknowledge that before any request leaves; `buildReleaseBody` returns null
-// until it did, and the backend refuses a release without it (409
-// retention_ack_required), which makes the step mandatory (`forceAck`) even when
-// the read did not show the deposit.
+// The gate: releasing an appointment whose Pix deposit is PAID hands the patient a
+// choice (secretarIA TASK-052, services/release_offer.py): reschedule with the same
+// deposit, or get it back - and no answer in 24 hours means the clinic returns it,
+// in the payment provider's panel (the platform never refunds by itself, D15). The
+// clinic must acknowledge that before any request leaves; `buildReleaseBody` returns
+// null until it did, and the backend refuses a release without it (409
+// retention_ack_required), which makes the step mandatory (`forceAck`) even when the
+// read did not show the deposit. The request field keeps its old name
+// (`acknowledge_retention`): renaming it is a contract change for nothing.
 
 import type { Appt } from "./types";
 import { fmtTime } from "./types";
@@ -20,8 +23,8 @@
   noticeTitle: "Aviso ao paciente",
   ackTitle: "Este paciente já pagou o sinal",
   ackBody:
-    "Liberar o horário agora, dentro da janela de reembolso, pode fazer a clínica reter o valor pago. Confirme que você entende antes de continuar.",
-  ackLabel: "Entendo que o sinal pago pode ser retido",
+    "Ao liberar o horário, o paciente recebe duas opções: remarcar usando o mesmo sinal, sem nova cobrança, ou pedir a devolução. Se ele não responder em 24 horas, a clínica deve devolver o sinal por inteiro pelo painel do provedor de pagamento.",
+  ackLabel: "Entendo que o sinal pago pode ter de ser devolvido",
   confirm: "Liberar horário",
   confirmAnyway: "Liberar mesmo assim",
   confirmPaidNotice: "Liberar e enviar aviso pago",
```

- [ ] **Step 4: Rodar e ver passar**

Run: `npx vitest run lib/agenda components/agenda ; npm run typecheck`
Expected: PASS (na escrita: 12 arquivos / 181 testes), typecheck limpo.

- [ ] **Step 5: Commit**

```powershell
git add lib/agenda/release.ts lib/agenda/__tests__/release.test.ts
git commit -m "fix(agenda): the paid-release warning describes the patient's refund-or-reschedule choice, not a retention (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

---

### Task 11: Gates, CHECKPOINT e registro

**Files:**
- Create: `docs/CHECKPOINT_task052_liberar_sinal_pago.md`
- Modify: `CLAUDE.md` (1 linha de ponteiro, na lista de CHECKPOINTs), `C:\TECH\BRAIN\tasks\TASK-052\TASK.md` (fora do repositório)

- [ ] **Step 1: Suíte inteira e lint de tudo o que foi tocado**

Run: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest -q -p no:cacheprovider ; uv run ruff check src/secretaria/models/deposit_release_offer.py src/secretaria/models/__init__.py src/secretaria/models/payment_attention_item.py migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py src/secretaria/services/release_offer.py src/secretaria/services/payments/attention_view.py src/secretaria/api/hub/calendar.py src/secretaria/services/appointment_release.py src/secretaria/workers/whatsapp/notifications.py src/secretaria/services/availability.py src/secretaria/services/flow_router.py src/secretaria/workers/shared/release_offer_actions.py src/secretaria/workers/shared/actions.py src/secretaria/schemas/webhook.py src/secretaria/workers/portal/inbound.py src/secretaria/workers/shared/flow_runner.py src/secretaria/workers/shared/booking_hold.py src/secretaria/services/booking_hold.py src/secretaria/workers/payments_cron.py tests/release_offer_support.py tests/test_deposit_release_offer_migration.py tests/test_release_offer.py tests/test_release_offer_buttons.py tests/test_release_offer_carry_over.py tests/test_release_offer_postgres.py tests/test_deposit_before_migration.py tests/test_payment_attention.py tests/test_hub_release.py tests/test_cancellation_notice.py tests/test_availability.py tests/test_before_booking_flow.py tests/test_payment_cron_jobs.py`
Expected: linha de base da Task 0 + 36 testes, 0 falhas (na escrita: **6139 passed, 81 skipped, 1 xfailed, 0 failed** — os 2 de Postgres somam aos `skipped`), ruff limpo.

- [ ] **Step 2: Criar o CHECKPOINT**

````markdown
# CHECKPOINT — Liberar horário com sinal pago: devolver ou remarcar (TASK-052)

**Estado:** construído e commitado em `task/TASK-052-liberar-sinal-pago` (worktree
`C:\TECH\BRAIN-worktrees\TASK-052\secretarIA`), último commit «SHA»; front em `task/TASK-052-aviso-liberar` «SHA».
**Não pushado, não deployado.** Integra em `feature/out-of-mvp` (nunca `main`), depois da TASK-050.
Spec: `docs/superpowers/specs/2026-10-11-liberar-horario-com-sinal-pago-design.md`.
Plano: `docs/superpowers/plans/2026-10-11-task-052-liberar-horario-sinal-pago.md`.

## 1. O que é
Liberar (`POST .../appointments/{id}/release`) uma consulta com sinal PAGO abre uma oferta (`deposit_release_offers`)
e manda ao paciente "Receber agora" / "Remarcar" (WhatsApp na janela de 24 h; Portal sempre). "Receber agora" abre UM
item `released_refund_due` (valor cheio) para a clínica devolver no painel; sem resposta em 24 h a varredura de
pagamentos abre o mesmo item. "Remarcar" = "Outro horário" (dia, depois horário); a nova consulta recebe o sinal pago
na mesma transação, sem nova cobrança. A lista de dias mostra dias com horários reservados e diz quais.

## 2. Onde está cada coisa
| Camada | Arquivo |
|---|---|
| Tabela + migração | `models/deposit_release_offer.py`, `migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py` |
| Regras | `services/release_offer.py` |
| Liberar / Portal | `api/hub/calendar.py::release_appointment`, `services/appointment_release.py` |
| WhatsApp | `workers/whatsapp/notifications.py::send_cancellation_notice` |
| Toques | `workers/shared/release_offer_actions.py`, `workers/shared/actions.py`, `schemas/webhook.py`, `workers/portal/inbound.py` |
| Transferência | `workers/shared/flow_runner.py::_apply_flow_result`, `workers/shared/booking_hold.py::_promote_booking_hold`, `services/booking_hold.py::BookingGate._deposit_quote` |
| Relógio | `workers/payments_cron.py::run_payment_sweeps` |
| Dias reservados | `services/flow_router.py::_enter_slot_picker`, `services/availability.py::held_slots` |

## 3. Decisões
As "Decisões tomadas (padrão)" da spec §9 (o dono pode trocar qualquer uma).

## 4. Validação
Suíte «N» passed / «S» skipped / 1 xfailed / 0 failed (linha de base «B»); ruff limpo; Postgres real: «PROVADO / NÃO PROVADO».
**NÃO PROVADO:** envio real pelo WhatsApp, cartão no Portal real, produção.

## 5. Deploy (NÃO autorizado)
Migração `b6d2e8f4a1c3` → API → worker logo depois → Brain-Message-Frontend.
````

- [ ] **Step 3: Ponteiro no `CLAUDE.md`**

Na lista de CHECKPOINTs do `CLAUDE.md` do secretarIA:

```markdown
- `docs/CHECKPOINT_task052_liberar_sinal_pago.md` — liberar horário com sinal pago: "Receber agora" / "Remarcar", devolução só por item de atenção, 24 h automático, sinal transferido ao remarcar, dias com horários reservados (2026-10-11, TASK-052); **construído, NÃO deployado**.
```

- [ ] **Step 4: Atualizar o `TASK.md`**

SHAs finais (secretarIA e front), contagens do Step 1, Postgres PROVADO/NÃO PROVADO, `Deployment: NOT AUTHORIZED`.

- [ ] **Step 5: Commit**

```powershell
git add docs/CHECKPOINT_task052_liberar_sinal_pago.md CLAUDE.md
git commit -m "docs: checkpoint for releasing a paid slot - refund or reschedule (TASK-052)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01J2kYPbf3cmf5WwjFSAq5zG"
```

Merge em `feature/out-of-mvp` (depois da TASK-050) é do Integrator; push e deploy só com pedido explícito do dono.

---

## Self-review

**1. Cobertura da spec → tasks.**

| Spec | Onde |
|---|---|
| §1 Intenção e os 6 critérios de sucesso | 1 → Tasks 1–4; 2 → Tasks 2, 6; 3 → Tasks 2, 8; 4 → Tasks 6, 7; 5 → Task 5; 6 → Tasks 2, 6, 7, 9 |
| §2 Escopo (secretarIA + texto do front) | Tasks 1–9; Task 10 |
| §3 Fora de escopo (cancelamento, modelo novo, e-mail, F4, IA, Pix em voo) | não tocados; Task 4 mantém o modelo aprovado fora da janela; Task 3 mantém o Pix em voo |
| §4 Fatos do código | Task 0 (âncoras), Tasks 3, 5, 6, 7 |
| §5.1 A oferta | Tasks 1, 2, 3 |
| §5.2 A mensagem e o limite de 1024 | Tasks 2 (`offer_text`, `fit_body`), 3, 4 |
| §5.3 WhatsApp (mesma assinatura; modelo fora da janela; botões do modelo = oferta) | Tasks 4, 6 |
| §5.4 Portal | Tasks 3, 6 |
| §5.5 Os toques e seus textos | Task 6 |
| §5.6 Transferência | Task 7 (e Task 2 para as transições) |
| §5.7 Relógio | Tasks 2, 8 |
| §5.8 Dias com horários reservados | Task 5 |
| §6 Erros e corridas | Review Focus 1–4; Tasks 2, 6, 7, 9 |
| §7 Testes | Tasks 1–10 |
| §8 Dependências e deploy | "Estado de execução", Global Constraints, Task 11 |
| §9 Decisões tomadas (padrão) | cada uma implementada na task citada na spec; nenhuma pendente de código |
| §10 Bloqueios / não provado | "Estado de execução", Task 11 |

**2. Varredura de placeholders.** Nenhum "TBD"/"TODO"/"similar à Task N": todo bloco é o conteúdo exato de um arquivo ou diff aplicado e verificado na escrita sobre `3650e10` (front sobre `feature/out-of-mvp`). Os `«»` do CHECKPOINT são medições declaradas; `<porta>` é a de um Postgres descartável escolhido na hora.

**3. Consistência de nomes.** `relrefund`/`relresched` são os mesmos em `services/release_offer.py`, `schemas/webhook.py`, `workers/portal/inbound.py`, nos botões do hub/job e nos testes; `released_refund_due` é o mesmo no modelo, no texto da atenção e nos testes; `refund_offered` é o mesmo no hub e nos testes; `OPEN_OFFER_STATES` é o mesmo no modelo, no serviço e no job; `claim_carry_over`/`carry_over_pending` são os mesmos no fluxo, na promoção do Portal e no portão.

**4. Review Focus.** Os 5 itens têm teste nomeado na task dona (todos passaram na escrita; os de Postgres num Postgres 18 local).
