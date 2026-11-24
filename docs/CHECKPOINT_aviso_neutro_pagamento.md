# CHECKPOINT — aviso neutro ao paciente quando a clínica está sem pagamento (TASK-093, Plano 2)

Estado: **LOCAL**, branch `task/TASK-093-aviso-pagamento`, base `db864222e28b4a4e05ae51b301c2cde74d97bb63`.
Sem push, sem deploy (`Deployment: NOT AUTHORIZED`).

## Comportamento

- Clínica sem pagamento em dia (`not (active and secretaria_enabled)`) e o **paciente escreve** → a secretária não conduz a conversa e
  envia **um** aviso fixo: "No momento esta clínica não está atendendo por aqui. Para falar com a clínica, entre em contato por telefone."
- No máximo **1 por dia por paciente**, dia contado no fuso da clínica (`tenant.timezone`, padrão `America/Sao_Paulo`).
- Paciente não escreve → nada é enviado. O aviso é só resposta a mensagem recebida.
- Silêncio quando: estado de pagamento ilegível (`summary is None`), clínica em dia, Redis ausente/com falha, sem remetente.
  Exceção deliberada: no toque em botão de lembrete, pagamento ilegível mantém o comportamento antigo (a ação roda), porque esse ramo nunca teve a trava.
- Não alterados (teste proíbe o import): `portal/open.py`, `portal/merge.py`, `whatsapp/remove_context.py`, `reminder_engine.py`,
  `appointment_edit_notification.py`, `plugins/reminders.py`, `api/hub/conversations.py`, `services/staff_patient_message.py`.

## Onde entrou

| Peça | Local |
|---|---|
| Texto, regra, dia da clínica, chave | `services/payment_notice.py` (`PAYMENT_NOTICE_TEXT`, `should_notify`, `clinic_today`, `seconds_until_clinic_midnight`, `notice_key`) |
| Envio 1x/dia (`SET NX`) | `workers/shared/payment_notice.py::send_payment_notice_once` |
| Ligação | `workers/orchestrator.py` (turno normal + `_notify_unpaid_button_tap` para toque em botão de lembrete), `workers/portal/attachments.py`, `workers/shared/degrade.py` |
| Testes | `tests/test_payment_notice.py`, `test_payment_notice_send.py`, `test_payment_notice_wiring.py` |

## Controle diário

Chave Redis `payment_notice:{tenant_id}:{patient_ref}:{AAAA-MM-DD}`, valor `1`, `SET NX EX`, validade até a meia-noite da clínica (mínimo 60 s).
No WhatsApp `patient_ref` é o telefone do paciente: **decisão do dono (2026-11-24): guardar o número** na chave até a meia-noite.
No Portal é o `external_id`. Falha de envio gasta o aviso do dia (o controle é gravado antes de enviar).

## Validação

- Suíte completa (`BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest -p no:cacheprovider`, serial): **10387 passed, 131 skipped, 1 xfailed, 2 failed** (33 min).
- As 2 falhas (`tests/test_service_change_agenda.py::test_a_paid_difference_that_also_moved_the_time_replans_and_tells_once` e
  `::test_a_clinic_initiated_paid_move_replans_but_sends_no_patient_change_notice`) **também falham na base `db86422`** → antigas, não regressão.
- `make lint` não rodado (já vermelho no HEAD).
- Não provado: entrega real pelo Portal/WhatsApp (testes usam remetente falso e SQLite).

## Deploy

API **e** worker (o código toca `workers/`). Sem migração, sem variável nova. Ordem da TASK: brain-api → secretarIA → Portal.
