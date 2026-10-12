# CHECKPOINT — Lembretes R3: abertura do chat e caminho do Cancelar (TASK-032)

Plano: `docs/superpowers/plans/2026-10-03-lembretes-r3-primeira-mensagem-e-cancelar.md`.
Spec: `docs/superpowers/specs/2026-10-03-lembretes-e-confirmacao-design.md` §4.3.

## 1. Estado

- **Local, commitado** na branch `task/TASK-032-r3-abertura-cancelar` (worktree
  `C:\TECH\BRAIN-worktrees\TASK-032\secretarIA-r3`, partindo de `main` f63458b). **NÃO pushado, NÃO deployado.**
- Commits das Tasks 1–11: `307403e` (coluna), `270bd26` (roteador), `22234fa` + `eb18c8f` (marcador),
  `59bbb54` (serviço de substituição), `aab4e14` (consumo na reserva), `72b9421` (ids e rótulos),
  `14f4f7d` + `39cccc6` (caminho do Cancelar), `44f7eba` (continuações), `f1630a7` (decisão da abertura),
  `103e23f` (abertura no turno), `718a0f7` (Portal).
- Migração `a7e2c9d4f1b6` (`conversations.flow_replaces_appointment_id`), `down_revision = e7d3c1a9b5f2`
  (TASK-030 P2a, que está sobre a de R1 `b8d3f1a6c2e5`) — a única head encontrada na Task 1.
- **Postgres run NOT done**: o Docker não estava rodando nesta máquina; a migração foi provada só no
  SQLite (teste `test_flow_replaces_column.py`). Rodar o passo da Task 1 (subir Postgres, `upgrade`,
  `downgrade -1`, `upgrade`) antes de qualquer deploy.
- Validação focada: 92 testes novos do R3 e 335 testes de regressão (R2, roteador, reativação, reserva,
  Portal, camadas) verdes. Suíte completa: ver `TASK.md`/relatório do Integrator.

## 2. O que entrou onde

- **Abertura do chat:** `services/reminder_opening.py::decide_reminder_opening` (decisão, só leitura) e
  `ensure_chat_reminder` (uma linha `chat` por versão da consulta); `workers/turn_router.py`, entre os dois pisos
  de silêncio e a oferta de reativação; `workers/shared/reminder_opening.py::_send_reminder_opening` (envio, a
  partir de `_send_bot_reply_inner`, depois do portão de entitlement); a supressão do menu genérico em `_run_flow`
  (`flow_router.is_generic_menu_result`) e no retorno `show_main_menu` do agente.
- **Caminho do Cancelar:** `workers/shared/reminder_actions.py` (cartão de três opções, confirmação de
  "Cancelar Consulta", manter, Outro → IA, linha "Você também tem consulta em …"); as continuações rodam em
  `workers/shared/actions.py::_handle_action_button` (Remarcar com limite do Pix; cancelar e perguntar o motivo;
  agendar outra).
- **"Agendar Outra" (marcar outra):** `conversations.flow_replaces_appointment_id`,
  `flow_router._carry_replacement` (o marcador só vive dentro da reserva), `services/appointment_replacement.py`
  (cancela a original na MESMA transação da nova, com as regras do Pix), `workers/shared/replacement.py`
  (depois do commit: evento do Google da original e linhas de lembrete), consumo em `_apply_flow_result` e em
  `_promote_booking_hold` (Portal).
- **Portal:** `workers/portal/inbound.py` decodifica os ids `rem*` só depois de provar que o id estava num cartão
  recente da própria conversa.
- **Ids e rótulos:** `services/reminder_text.py` (`remresched`, `remnew`, `remgiveup`, `remgiveupyes`, `remkeep`)
  e `schemas/webhook.py::_ACTION_BUTTON_PREFIXES`.
- **WhatsApp e Portal:** nada é exclusivo de um canal — todo envio passa por `_send_buttons_reply` /
  `_reply_sender`. Os testes de turno rodam no WhatsApp e há um teste do mesmo cartão no Portal.

## 3. Rótulos dos botões (pedido do dono, 2026-10-07)

| Passo | Rótulo no botão | Observação |
|---|---|---|
| Remarcar esta consulta | **Remarcar Consulta** (17) | |
| Marcar outra consulta | **Agendar Outra** (13) | O dono pediu "Agendar Outra Consulta" (22 caracteres). O botão de resposta do WhatsApp aceita no máximo 20 (`MAX_BUTTON_LABEL_CHARS`; a Meta recusa mais que isso), então o botão diz "Agendar Outra" e o corpo do cartão diz "agendar outra consulta" por extenso. |
| Não vou mais | **Cancelar Consulta** (17) | |
| Confirmação | Sim, cancelar / Manter consulta | inalterados |

Só o cartão novo do caminho do Cancelar mudou. O menu antigo de gerenciar consulta (`LABEL_RESCHEDULE =
"Remarcar"` em `flow_router.py`) continua como estava.

## 4. Regras fixadas (Review Focus do plano → teste que prende)

1. Sem consentimento → só o pedido de consentimento: `test_consent_not_yet_given_gets_only_the_consent_prompt`.
2. Duas consultas futuras → o cartão é da mais próxima e a resposta cita a outra:
   `test_the_nearest_of_two_appointments_opens_the_chat`, `test_two_appointments_open_with_the_nearest_and_the_answer_names_the_other`.
3. Consulta passada ou cancelada → sem abertura; toque em cartão velho → "não está mais ativa":
   `test_a_past_or_cancelled_appointment_never_opens_the_chat`, `test_a_cancelled_appointment_gets_the_normal_menu`.
4. Já confirmou 2 vezes → sem pedido: `test_confirmed_twice_never_asks_again`, `test_confirmed_twice_gets_the_normal_menu`.
5. Pedido claro junto da abertura ("Agendar") → cartão e depois a pergunta: `test_a_clear_request_is_answered_right_after_the_card`,
   `test_a_clear_request_is_not_the_generic_menu`.
6. Toque no Portal com id de lembrete de outro paciente → descartado, nada contado:
   `test_a_portal_tap_with_another_patients_reminder_id_counts_nothing`.
7. "Agendar Outra" abandonada → a original continua: `test_a_marker_left_on_an_idle_conversation_never_reaches_a_new_booking`,
   `test_apply_clears_the_marker_when_the_result_leaves_the_booking` e os testes de pisos, "Não" e espera do código.
8. Nova reserva confirmada → original cancelada na mesma transação, evento do Google apagado, lembretes fechados:
   `test_confirming_the_new_booking_cancels_the_original_in_the_same_turn`, `test_the_portal_promotion_replaces_the_original_too`.
9. Original com Pix pago → aviso de retenção dentro da janela; falha no dinheiro nunca desfaz a reserva:
   `test_the_money_hook_runs_and_its_notice_is_returned`, `test_a_failing_money_hook_still_cancels_the_original`,
   `test_give_up_inside_the_refund_window_warns_about_the_deposit_first`, `test_book_another_inside_the_refund_window_warns_about_the_deposit`.
10. Abertura duplicada em 6 h → sem segundo cartão nem segunda linha: `test_a_chat_row_shown_inside_the_gap_is_not_shown_again`,
    `test_a_second_message_right_after_the_card_gets_no_second_card`.
11. A linha `chat` divide o contador com o cron; a mesma mensagem nunca conta duas vezes:
    `test_an_unanswered_cron_card_that_is_the_last_activity_is_not_repeated`, `test_chat_and_cron_confirmations_share_the_counter`,
    `test_the_same_chat_card_reshown_later_counts_once`.
12. Toques do caminho do Cancelar de outro paciente ou de versão antiga → "Não encontrei essa consulta." / horário antigo:
    `test_cancel_path_taps_are_checked_against_the_patient_and_the_version`.

## 5. Decisões

- (a) Uma linha `chat` por versão da consulta, reexibida, nunca duplicada.
- (b) A abertura não sai quando um lembrete do cron com botões, sem resposta, é a última atividade.
- (c) A linha "Você também tem consulta em …" só nas respostas ao cartão do chat (Confirmar/Outro).
- (d) Rótulos de botão com no máximo 20 caracteres; a redação longa vai no corpo do cartão (ver seção 3).
- (e) O Portal só decodifica a família `rem*`; os ids antigos seguem pelo título.
- (f) Reserva feita sozinha pela IA (`_persist_appointment`) nunca cancela a original, só apaga o marcador.
- (g) "Outro" coloca a conversa em modo IA pelo `_apply_flow_result`.
- (h) `reminder_actions.py` manteve duas proteções que a revisão final do R2 tinha acrescentado e que o texto do
  plano (mais antigo) perderia: linha de lembrete aposentada responde "não está mais ativa", e a confirmação só é
  anunciada se foi de fato registrada.
- (i) O teste do R2 que fixava os prefixos do decodificador no trio passou a comparar com a união
  (`REMINDER_ROW_ACTIONS`); o teste `test_the_row_actions_are_exactly_the_rem_prefixes_the_decoder_knows` prende o pareamento exato.
- (j) O teste de cabeça única da migração da P2a (`test_flow_draft_column.py`) deixou de fixar a head, porque uma
  revisão nova sempre fica por cima.

## 6. Pendências

- "Abre o chat" no Portal para conversa já existente não dispara abertura (a rota `open` responde `exists` antes
  de qualquer job; coberto pela mensagem que o R2 grava quando um lembrete sai e pela primeira mensagem escrita).
- A abertura substitui a oferta "quer continuar?" de quem estava no meio de uma marcação (a mensagem segue o
  estado guardado).
- O evento do Google da original não é apagado quando o profissional dono foi desativado (logado como
  `appointment_replacement_calendar_missing`).
- `workers/shared/draft_resolution.py` monta o próprio retrato da conversa e não leva o marcador; só importa com a
  IA v2 (`ai_draft_v2`) ligada e dentro de um "Agendar Outra" — hoje desligada. Revisar antes da P5.
- A Postgres run (seção 1) e o teste ao vivo no WhatsApp (clínica de teste, interruptor ligado só nela).
- TASK-030 P5 (prompt/filtro da IA ainda proíbem falar de lembretes); R4 (avisos e liberar horário) e R5 (front).
- Deploy: banco primeiro (`alembic upgrade head`), depois `secretaria_api` e `secretaria-worker` juntos
  (`GET /build` com paridade `match`). Nada foi pushado nem deployado.

- R6 (2026-10-07): o caminho do Cancelar e o Agendar Outra foram substituídos por Alterar Dados; ver docs/CHECKPOINT_lembretes_r6.md (local, validado, sem commit/deploy).
