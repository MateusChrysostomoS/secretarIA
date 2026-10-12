# Retenção de visitas vazias + espera de código de conta conhecida — TASK-042 (lado secretarIA)

Registro da tarefa: `BRAIN/tasks/TASK-042/`. Lado brain-api (o job, a regra completa, o risco LGPD):
`brain-api/docs/CHECKPOINT_retencao_visitas_portal.md`.

## Estado

Implementado e validado localmente na branch `task/TASK-042-retencao-visitas` (worktree
`BRAIN-worktrees/TASK-042/secretarIA`). Commit local (SHA no `TASK.md`). Sem migração. Não
mesclado, não pushado, não deployado. Deploy exige **API e worker** juntos.

## 1. Descarte de visita vazia (API)

- `services/visit_merge.py::discard_empty_visit` — escopo `tenant_id + channel=brain_message +
  external_id`. Recusa (`not_empty`) se houver qualquer mensagem do paciente, qualquer consulta
  (por `patient_id` ou pela conversa — agendamento para terceiro) ou reserva viva. Senão reutiliza
  `discard_visit` da TASK-034 (mensagens do bot, mapa de pseudônimos, reservas, conversa, ficha;
  mantém `consent_events` e `processed_events`).
- `POST /internal/brain-message/visits/discard` (`api/internal.py`) — síncrona, `X-Internal-Api-Key`,
  corpo `{tenant_id, external_id}` com `extra="forbid"`. `200 {"status":"discarded"|"absent"}`,
  `409 visit_not_empty`. Idempotente. Precisa estar no ar ANTES do brain-api que a chama.

## 2. Espera de código de conta conhecida, antes do consentimento (worker)

Incidente de 2026-10-09: uma mensagem que não era código derrubava a espera e o paciente que já
tinha conta caía no aviso de LGPD de visitante novo.

- `workers/portal/identity_gate.py::run_identity_gate` — em `AWAITING_EMAIL_CODE`, sem reserva e com
  `lgpd_accepted_at` vazio, a mensagem que não é código devolve `existing_account_code_reprompt`.
- `workers/orchestrator.py::_send_bot_reply` — reenvia o cartão de dois botões
  (`EXISTING_ACCOUNT_CODE_BUTTONS`, "Reenviar código"/"Mudar e-mail") e mantém a espera. A saída
  continua por tempo (`pending_identity_ttl_minutes`) ou pelo botão "Mudar e-mail".
- A espera depois da consulta (com reserva, ou depois do consentimento) ficou exatamente igual.
- Log novo `conversation_pending_code_unrecognized`: só `kind` (text/interactive/empty), `length`
  e `pre_consent`, nunca o texto.

## Validação

Ver `BRAIN/tasks/TASK-042/TASK.md`. Testes: `tests/test_visit_retention_discard.py`,
`tests/test_patient_name_step.py::test_portal_known_email_non_code_message_repeats_the_card_not_the_lgpd`
(falha no código antigo), `tests/test_task042_tester.py` (Tester independente).
