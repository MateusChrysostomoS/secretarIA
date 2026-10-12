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

## 4. Não provado

Nada foi testado em produção ou com o Asaas real; o fluxo completo Pix + Portal + lembretes da
main não foi exercitado de ponta a ponta fora dos testes.
