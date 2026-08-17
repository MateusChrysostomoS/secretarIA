# CHECKPOINT — `RESCHEDULED` é status VIVO (taxonomia unificada)

PROMPT_FIX_16. Writers e readers discordavam sobre o que `RESCHEDULED`
significa; agora existe **uma** definição, compartilhada.

**Status:** BUILT + validado localmente. `uv run python -m pytest -q` →
**1281 passed**. `uv run ruff check .` → **8 erros PREEXISTENTES**, nenhum novo.
**Sem migração e sem backfill.** NÃO commitado, NÃO deployado.

---

## O bug

Os dois carriers de reagendamento **movem a mesma linha**: mesmo `id`, mesmo
`google_event_id`, mesmo `PixDeposit`, novo `start_at`/`end_at`, status
`RESCHEDULED`. Nenhum dos dois cria linha substituta nem aposenta a original —
`workers/tasks.py::_apply_flow_result` e
`api/hub/calendar.py::reschedule_appointment`.

Os readers liam `RESCHEDULED` como lápide:

| Reader | Filtro antigo | Efeito depois do 1º reagendamento |
|---|---|---|
| `patient_context.UPCOMING_STATUSES` | `SCHEDULED, CONFIRMED` | some do **gerenciamento**, do **greeting** (HAS_UPCOMING) e da tool **`list_patient_appointments`** |
| `patient_context._RECENT_PAST_EXCLUDED` | excluía `RESCHEDULED` | nunca conta como "acabou de consultar" |
| `reminders._REMINDABLE_STATUSES` | `SCHEDULED, CONFIRMED` | **para de receber lembrete** — justo quando ele mais importa |
| `tasks.py` botão `apptconfirm` | `(SCHEDULED, CONFIRMED)` | "essa consulta não está mais ativa" ao tentar confirmar |

Enquanto isso a metragem (`workers/onboarding_cron.py`) **não tem filtro de
status** e seguia cobrando pela consulta — e
`tests/test_onboarding_cron.py::test_tally_rescheduled_appointment_is_billable`
já documentava que "RESCHEDULED is NOT cancelled-like". Essa era a contradição:
o teste estava certo, os readers é que estavam errados.

## A decisão

**`RESCHEDULED` é LIVE** — a orientação preferida do prompt, e a única coerente
com os writers. A alternativa (voltar para `SCHEDULED` e guardar histórico em
campo próprio) exigiria migração + campo novo + migrar todos os carriers, sem
ganho: a informação "esta reserva já foi movida" é útil e o `PixDeposit` já
conta os movimentos em `reschedule_count`.

    LIVE      SCHEDULED · CONFIRMED · RESCHEDULED
    TERMINAL  CANCELLED · ATTENDED · NO_SHOW

A tabela de transições completa está no docstring de
`src/secretaria/models/appointment.py` (fonte canônica, junto do enum).

## A constante compartilhada

Para não voltar a divergir, ninguém escreve mais uma tupla de status inline:

```python
# models/appointment.py
LIVE_APPOINTMENT_STATUSES      # (SCHEDULED, CONFIRMED, RESCHEDULED)
TERMINAL_APPOINTMENT_STATUSES  # (CANCELLED, ATTENDED, NO_SHOW)
is_live_status(status) -> bool
```

Reexportadas por `models/__init__.py`. `UPCOMING_STATUSES` e
`_REMINDABLE_STATUSES` agora **são** `LIVE_APPOINTMENT_STATUSES` (identidade,
não cópia — há teste que verifica `is`). Um teste também prova que LIVE e
TERMINAL **particionam** o enum: um membro novo que ninguém classificar quebra
o build em vez de silenciosamente virar terminal.

## Observabilidade

Novo `src/secretaria/services/appointment_status.py`:
`log_status_transition(...)` emite `appointment_status_transition` com
`appointment_id`, `tenant_id`, `old_status`, `new_status`, `source`,
`idempotency_key` e `still_live`. Só ids internos e nomes de status — nunca
telefone, nome ou detalhe clínico (teste verifica o conjunto exato de campos).

Os quatro carriers passaram a emiti-lo:

| source | onde | idempotency key |
|---|---|---|
| `flow` | `_apply_flow_result` (cancel + reschedule) | `resched:{google_event_id}:{novo start ISO}` / `cancel:{google_event_id}` |
| `button` | `_handle_action_button` (apptconfirm / apptcancel) | `apptconfirm:{id}` / `apptcancel:{id}` |
| `hub` | `POST /cancel`, `POST /reschedule`, `PATCH /status` | `cancel:{id}` / `resched:{...}` / `status:{id}:{valor}` |
| `system` | expiração de sinal Pix (`apply_asaas_event`) | `asaas:{event_id}` |

`still_live=False` é o sinal contável de "saiu do conjunto vivo".

Efeito colateral bom: nos ramos de cancel/reschedule do flow o `SELECT` foi
**movido para antes** do `UPDATE` (para saber o status anterior) e a linha é
reaproveitada pelo money hook — **uma query a menos**, mesmo comportamento.

## Sem migração, sem backfill

Nada muda no schema: `appointments.status` continua o mesmo enum Postgres com
os mesmos seis valores. Nenhuma linha é reclassificada — linhas históricas em
`RESCHEDULED` simplesmente **voltam a ser lidas** como vivas, que é o que
sempre foram do ponto de vista dos writers. Nada de histórico se perde porque
nada é reescrito.

Consequência esperada no deploy: consultas passadas que ficaram paradas em
`RESCHEDULED` e nunca foram marcadas como atendidas voltam a aparecer no
lookback de "acabou de consultar" (janela de `POST_CONSULT_WINDOW_HOURS`, 48h
por padrão) — é o comportamento correto, mas é uma mudança visível.

## Arquivos

**src:** `models/appointment.py`, `models/__init__.py`,
`services/appointment_status.py` (novo), `services/patient_context.py`,
`plugins/reminders.py`, `workers/tasks.py`, `api/hub/calendar.py`,
`services/payments/deposit_lifecycle.py`.

**tests:** `test_appointment_status_taxonomy.py` (novo, 28),
`test_reminders_plugin.py` (+5), `test_hub_calendar_money.py` (+1).

**docs:** este arquivo; correções em `CHECKPOINT_pix_deposit.md` e
`CHECKPOINT_context_aware_opening.md`, que documentavam a semântica antiga.

## Pendências

1. **Commit/push/deploy** — nada foi commitado.
2. **Deploy: API e worker são serviços EasyPanel separados, com deploy manual.**
   Os readers estão espalhados: reminders e manage flow vivem no **worker**;
   o hub vive na **API**. Deployar só um lado deixa as duas metades com
   definições diferentes de "consulta futura" — exatamente o bug que este round
   resolve, só que entre processos. **Deployar os dois.**
3. A ordem segura, se for preciso escalonar: **readers primeiro** (esta mudança
   é toda de leitura — nenhum writer mudou de comportamento), depois o resto.
4. Monitorar após o deploy: contagem de `appointment_status_transition` por
   `source`/`new_status`, e o volume de reminders (deve **subir** um pouco, já
   que reservas reagendadas voltam a ser lembradas).
5. Não verificado nesta rodada: o painel do hub (`brain-frontend` /
   `secretarIA-frontend`) exibe `status` cru — vale conferir se "rescheduled"
   aparece de forma compreensível para o médico.
