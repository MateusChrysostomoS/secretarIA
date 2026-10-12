# CHECKPOINT — TASK-050: `main` trazida para `feature/out-of-mvp` (Pix portado para os workers divididos)

Estado: merge commitado SÓ no branch `task/TASK-050-secretaria` (worktree
`C:\TECH\BRAIN-worktrees\TASK-050\secretarIA`). `feature/out-of-mvp` NÃO foi movido; nada foi
pushado nem deployado; nenhuma migração rodou em banco real.

Bases: `feature/out-of-mvp` = `4e16d6c`, `main` = `d37aa11`, merge-base `64ce192` (213 commits de
main, 22 do Pix).

## 1. O que o merge precisou além de juntar texto

O Pix (TASK-016/017/020) editou ~740 linhas do `workers/tasks.py` antigo; a main dividiu esse
arquivo em pacote (TASK-023, `docs/CHECKPOINT_workers_split.md`). O `tasks.py` da main (fachada)
foi mantido e cada trecho do Pix foi movido para o módulo dono:

| Trecho do Pix | Onde ficou |
|---|---|
| campos `deposit_wait_*` / `payer_data_*` do `_ReplyContext` | `workers/shared/context.py` |
| redação da resposta de CPF/e-mail; turnos `AWAITING_PAYER_DATA` / `AWAITING_DEPOSIT` | `workers/turn_router.py::_route_inbound_turn` (logo após `/menu`) |
| Pix depois do código verificado; despacho de `deposit_wait_action` / `payer_data_*` | `workers/orchestrator.py::_send_bot_reply_inner` |
| `_send_deposit_request_notice`, `_abort_deposit_request`, `_void_waiting_deposit`, `_arq_enqueue`, `_retry_buttons` | `workers/shared/deposit_request.py` (novo) |
| `_handle_deposit_wait`, `_handle_payer_data`, `_payment_after_code` e auxiliares | `workers/shared/deposit_turn.py` (novo) |
| `deposit_request` em `_apply_flow_result`; `redis` no `BookingGate` | `workers/shared/flow_runner.py`, `draft_resolution.py::_turn_booking_gate(redis=)` |
| hold em fase `payment` não é promovido pelo código | `workers/shared/booking_hold.py::_promote_booking_hold` |
| `/menu` anula o Pix aberto e esquece o dado do pagador | `workers/shared/sentinels.py::_handle_show_main_menu` |

Os dois módulos novos são neutros de canal e respeitam `tests/test_workers_layering.py`
(`deposit_request` não importa `flow_runner`/`sentinels`, por isso os dois podem importá-lo).
A fachada reexporta os nomes novos. Testes do Pix que faziam `monkeypatch.setattr(tasks, ...)`
passaram a usar `tests/_patching.py::workers_ns`.

## 2. Decisões tomadas no merge (revisar)

- **Liberar horário (R4) x "a plataforma nunca devolve" (D15).** A main previa "estornado ao
  paciente"; no Pix nada é devolvido pela plataforma. `preview_cancellation_outcome` e
  `release_warning_text` (`services/payments/deposit_lifecycle.py`) agora espelham o D15:
  `refund_due` / `partial_refund_due` / `retained` / `voided`, e o aviso diz que a CLÍNICA devolve
  pelo painel do provedor. Liberar continua com a regra de prazo do paciente (decisão da main);
  o PATCH de status para `cancelled` e o cancelamento pelo hub usam `cancelled_by=clinic`
  (sempre `refund_due`), como no Pix.
- **Lista de dias com reservas.** A main não descontava reservas na lista de dias; com Pix uma
  reserva dura 30-120 min, então `enter_day_picker` passa `holds=` para
  `services/availability.py::available_day_starts` (sem reservas o resultado é idêntico ao da main).
- **Token do hub (brain-api).** A introspecção devolve os papéis da TASK-020 (`role`,
  `is_owner`, `is_manager`) e o `agenda_scope` da TASK-044; `SubscriptionClaim` carrega os dois.
- **Migrações.** `c8a5d3f1b7e2` passou a descender de `a4c7e2f9b1d3` (cabeça da main); conteúdo
  inalterado. Uma cabeça só: `e2b7c4a9d1f5`.

## 3. Validação

- Suíte completa: **6103 passed, 79 skipped, 1 xfailed, 0 failed** (6183 coletados).
- Postgres 18 descartável (initdb, porta 5544, apagado no fim): `alembic upgrade head` do zero,
  `downgrade a4c7e2f9b1d3`, `upgrade head` OK; testes opt-in com `PAYMENTS_TEST_PG_URL`
  (migração + concorrência): 22 passed.
- `ruff check` nos arquivos alterados: limpo.

## 3a. Correções da revisão (`tasks/TASK-050/REVIEW.md`, I-1 e I-2)

- **I-1 — `/edit` respeita o horário de quem está pagando o sinal (D4).** A porta
  `POST /appointments/{id}/edit` (R7, vinda da main) não consultava as reservas. Agora
  `services/staff_appointment_edit.py::apply_staff_edit` recebe `slot_guard` e o chama antes do
  Google sempre que horário ou médico mudam; a rota do hub passa a MESMA checagem de criar e
  `/reschedule` (`api/hub/calendar.py::_refuse_if_slot_is_held`) → 409
  `slot_held_awaiting_payment`. Decisão tomada: o **encaixe (`allow_overlap`) NÃO passa por cima**
  de uma reserva com Pix (encaixe sobrepõe consulta marcada, nunca horário sendo pago) — confirmar
  com o dono. Testes em `tests/test_before_bypass_paths.py` (a lista de portas): mover para o
  horário reservado é 409 com e sem encaixe, sem tocar Google nem a linha; mover para o horário
  vizinho funciona.
- **I-2 — os encaixes do Pix no orquestrador agora têm teste de ponta a ponta.**
  `tests/test_pix_turn_dispatch_e2e.py` passa pelo caminho real (`_persist_inbound_message` →
  `_send_bot_reply_inner`): os três toques do cartão de espera, CPF inválido e opt-out, e o CPF
  válido que completa o "Confirmar" (o Redis do turno chega ao portão e o provedor recebe o CPF).
  Prova de mordida (cada encaixe desligado na cópia de trabalho, depois restaurado): despacho de
  `deposit_wait_action` → 3 falham; despacho de `payer_data_action` → 2 falham; guarda da resposta
  (`payer_data_answer`) → 1 falha; `_turn_booking_gate(redis=None)` → 1 falha. Restaurado: 6 verdes.
- Comentários: `/menu` voltou para cima do bloco dele em `orchestrator.py` (o merge o deixou sobre
  o despacho do Pix); docstrings de `deposit_lifecycle` e `release_appointment` não falam mais em
  estorno automático nem em `_asaas_client_for`.
- Os Minor m-1, m-3, m-4, m-5 da revisão continuam abertos.
- Suíte completa depois das correções: **6112 passed, 79 skipped, 1 xfailed, 0 failed**
  (6103 + 9 testes novos).

## 4. Não provado

Nada foi testado em produção ou com o Asaas real; o fluxo completo Pix + Portal + lembretes da
main não foi exercitado de ponta a ponta fora dos testes.

## 5. Pré-condições e decisões conscientes (fecha m-4 e m-5 da revisão)

- **Migração reencadeada (m-5).** `c8a5d3f1b7e2.down_revision` passou de `d4f8a2c6e913` para
  `a4c7e2f9b1d3`. Isso só é seguro porque a linha do Pix (`e2b7c4a9d1f5`, antes do merge) nunca foi
  aplicada em banco algum (nem prod, nem staging, nem dev compartilhado). Um banco que já tivesse
  `e2b7c4a9d1f5` se acharia "na cabeça" sem as 8 migrações da `main`. **Antes de qualquer deploy
  desta linha, confirmar que nenhum banco tem `e2b7c4a9d1f5` na tabela `alembic_version`.**
- **Papel no token x escopo ao vivo (m-4).** `role`/`is_owner`/`is_manager` vêm do token do hub
  (TASK-020); `agenda_scope` é lido ao vivo (decisão do dono, 2026-10-09). Um gestor rebaixado
  mantém os poderes de gestor (chave Pix, ajustes de pagamento) até o token expirar. É o desenho
  da TASK-020, não regressão do merge; fica registrado como decisão consciente e documentado em
  `brain-api/CONTRACTS.md` §12.2. Alinhar com o `agenda_scope` é tarefa futura.
