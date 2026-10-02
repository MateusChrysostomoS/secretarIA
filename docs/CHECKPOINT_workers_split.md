# CHECKPOINT — divisão de `workers/tasks.py` (TASK-023)

Estado: construído e commitado no branch `task/TASK-023-split-workers`; **não** integrado em `main`, **não** deployado.

## 1. Objetivo e o que não mudou

`workers/tasks.py` (8.788 linhas, 165 nomes) foi dividido por responsabilidade e por canal. Só movimentação: corpo,
assinatura, nome, mensagem e nível de log de cada função são idênticos (provado por comparação de AST). `tasks.py`
virou fachada de reexports; `arq_worker.py` e os testes continuam importando dele. Nomes dos jobs do arq (11 jobs,
5 crons) inalterados. `services/flow_router.py` e `services/channel_sender.py` não se moveram.

## 2. Camadas

| Pasta | O que é | Pode importar |
|---|---|---|
| `workers/shared/` | neutro de canal | só `shared/` |
| `workers/whatsapp/` | código só do WhatsApp | `shared/` (+ composição em `inbound`/`audio`) — nunca `portal/` |
| `workers/portal/` | código só do Portal | `shared/` (+ composição em `inbound`/`open`) — nunca `whatsapp/` |
| `workers/turn_router.py`, `workers/orchestrator.py` | composição, ainda com `if channel == ...` (`orchestrator` usa `portal/attachments` e `portal/identity`) | tudo |

Ordem: `shared` ← `whatsapp`, `portal` ← `turn_router`, `orchestrator` ← (`tasks`, `arq_worker`). Nada dentro de
`workers/` importa a fachada `tasks`. Ajustes forçados pela análise de ciclos: `_as_utc` em `shared/text`,
`_RESCHEDULE_PRECHECK_STEPS` em `shared/deposit`, `_extract_sent_wam_id` em `shared/sender`, `_log_booking_scope` em
`shared/booking_hold`.

## 3. Mapa nome → módulo

| Módulo | Nomes |
|---|---|
| `workers/shared/text.py` | `SERVICE_UNAVAILABLE_MESSAGE`, `CALENDAR_UNAVAILABLE_MESSAGE`, `AUDIO_UNINTELLIGIBLE_MESSAGE`, `GREETING_REQUIREMENTS_HEADER`, `GREETING_BRIEF_HEADER`, `GREETING_ACTION_HINT`, `GREETING_DETAIL_MAX_CHARS`, `GREETING_BRIEF_KEEP`, `GREETING_REQUIREMENTS_KEEP`, `JUST_HAD_CONSULT_NEUTRAL_LINE`, `JUST_HAD_CONSULT_ATTENDED_LINE`, `_MENU_COMMANDS`, `is_menu_command`, `_is_consent_acceptance`, `REMOVE_CONTEXT_COMMAND`, `is_remove_context_command`, `_NAME_PATTERNS`, `_NAME_STOPWORDS`, `extract_patient_name`, `_render_greeting_template`, `_as_utc` |
| `workers/shared/context.py` | `_ReactivationDirective`, `_ReplyContext` |
| `workers/shared/sender.py` | `_reply_sender`, `_record_outbound`, `_tenant_client`, `_send_simple_text`, `_extract_sent_wam_id` |
| `workers/shared/db.py` | `_event_already_processed`, `_conversation_message_count`, `_get_or_create_conversation` |
| `workers/shared/greeting.py` | `_menu_vocabulary`, `_asks_name_at_first_contact`, `_first_contact_reply`, `_select_greeting`, `_fit_clinic_description`, `_greeting_buttons_for`, `_flow_tenant_snapshot`, `_format_appointment_when`, `_UpcomingGreetingData`, `_appointment_doctor_name`, `_compose_upcoming_greeting_body`, `_adapt_greeting_has_upcoming`, `_adapt_greeting_to_state`, `_load_upcoming_greeting_data` |
| `workers/shared/state_expiry.py` | `_expire_stale_pending_identity_state`, `_pending_identity_reactivation_offer`, `_reactivation_offer`, `_expire_stale_llm_state`, `_expire_stale_attendee_step`, `_write_flow_state` |
| `workers/shared/llm_context.py` | `_llm_activation_reason`, `_should_inject_post_consult_knowledge`, `_should_inject_appointment_context`, `_appointment_context_text`, `_label_match_body`, `_flow_handback_tools`, `_flow_turn_calendar`, `_appointment_calendar_target`, `_appointment_calendar`, `_manage_owner_calendar_target` |
| `workers/shared/bubbles.py` | `_bubble_buttons`, `_slots_rows`, `_send_bubble`, `_bubble_history_body`, `_bubble_interactive` |
| `workers/shared/dispatch.py` | `_dispatch_bubbles`, `_GREETING_ACTION_IDS`, `_GREETING_LLM_ESCAPE_SUFFIX`, `_send_greeting`, `_send_consent_notice`, `_send_plain_reply`, `_send_buttons_reply` |
| `workers/shared/handover.py` | `_handle_human_handoff`, `_set_conversation_human_active`, `_handle_calendar_unavailable`, `_handle_professional_config_incomplete` |
| `workers/shared/booking_hold.py` | `_log_booking_scope`, `_is_agent_sentinel`, `_hold_minutes_left`, `_tenant_tzinfo`, `_hold_when`, `_send_booking_gate_notice`, `_promote_booking_hold`, `_release_hold`, `_patient_display_name`, `_PROMOTE_FAILED_MESSAGE` |
| `workers/shared/deposit.py` | `_RESCHEDULE_PRECHECK_STEPS`, `_pix_retention_warning_line`, `_hours_until_start`, `_send_reschedule_limit_buttons`, `_apply_deposit_awareness` |
| `workers/shared/flow_runner.py` | `_run_flow`, `_apply_flow_result` |
| `workers/shared/actions.py` | `_APPOINTMENT_NOT_FOUND_TEXT`, `_calendar_for_appointment`, `_execute_appointment_cancel`, `_handle_action_button`, `_GREETING_ACTION_UNAVAILABLE_TEXT`, `_GREETING_ACTION_UNAVAILABLE_DEFAULT`, `_handle_greeting_button_unavailable` |
| `workers/shared/sentinels.py` | `_handle_show_main_menu`, `_handle_select_professional`, `_handle_manage_appointment`, `_handle_start_guided_booking`, `_handle_set_booking_draft` |
| `workers/shared/degrade.py` | `_handle_service_unavailable` |
| `workers/shared/jobs.py` | `send_transactional_email`, `check_handover_timeouts`, `_claim_event`, `_release_event` |
| `workers/whatsapp/inbound.py` | `process_webhook_event`, `_STATUS_RETRY_DELAY`, `_apply_statuses`, `_handle_message_statuses`, `process_message_statuses`, `_handle_patient_messages`, `_persist_inbound_message` |
| `workers/whatsapp/rate_limit.py` | `_is_rate_limited` |
| `workers/whatsapp/db.py` | `_mark_connected`, `_resolve_tenant`, `_get_or_create_patient` |
| `workers/whatsapp/audio.py` | `_transcription_config`, `_mark_audio_event_processed`, `transcribe_audio_message` |
| `workers/whatsapp/coexistence.py` | `_handle_human_echoes`, `_persist_human_echo`, `_mark_mode_resolved`, `_handle_history`, `_handle_smb_app_state_sync` |
| `workers/whatsapp/remove_context.py` | `REMOVE_CONTEXT_PRESERVED_MESSAGE`, `_handle_remove_context_command` |
| `workers/whatsapp/notifications.py` | `send_patient_notification`, `CANCEL_NOTICE_MAX_TRIES`, `CANCEL_NOTICE_RETRY_DEFER_S`, `CANCEL_NOTICE_VALIDITY_S`, `send_cancellation_notice`, `_cancellation_retry_decision`, `_escalate_cancellation_failure`, `_emit_cancellation_usage` |
| `workers/portal/inbound.py` | `BRAIN_MESSAGE_TAP_WINDOW`, `offered_reply_ids`, `_validated_brain_message_reply_id`, `_log_discarded_attachment`, `_persist_brain_message_inbound`, `process_brain_message_inbound` |
| `workers/portal/open.py` | `_open_ledger_key`, `_portal_conversation_has`, `_open_brain_message_conversation`, `process_brain_message_open` |
| `workers/portal/identity.py` | `_send_code_notice`, `_continue_after_email_claim`, `_account_code_dead_end`, `_finish_account_code_before_consent`, `_handle_identity_card_action`, `_record_verified_account_consent`, `_conversation_patient_has_name`, `_adopt_account_name`, `_handle_pre_consent_identity` |
| `workers/portal/attachments.py` | `ATTACHMENT_RECEIVED_MESSAGE`, `_handle_attachment_received` |
| `workers/turn_router.py` | `_route_inbound_turn` |
| `workers/orchestrator.py` | `_send_turn_fallback`, `_send_bot_reply`, `_send_bot_reply_inner` |

## 4. Como provar

- `split_tasks.py verify` (em `tasks/TASK-023/tools/` do workspace BRAIN) compara `ast.dump` de cada nó movido com o
  original: `verify: OK - every moved node is identical`.
- `tests/test_workers_layering.py` — regras de camada e import isolado de 7 pontos de entrada.
- `tests/test_workers_ns_patching.py` — o helper `tests/_patching.py::workers_ns`. Os testes que antes faziam
  `monkeypatch.setattr(tasks, "X", v)` agora usam `workers_ns`, que aplica o patch no módulo onde `X` mora e desfaz
  devolvendo a cada módulo o seu original. Reexportar sozinho não basta: o código movido procura `X` no módulo novo.
- Logs: quem loga é o logger do módulo novo; `tests/test_bot_reply_gating.py` patcha `orchestrator.logger`.
- Nomes do arq: `registered_function_names()` (11) e `registered_cron_names()` (5).

## 5. Como desfazer

`git revert` do merge da TASK-023. A fachada garante que nada fora de `workers/` importa os módulos novos.

## 6. Pendências

- Fase 2: política de canal substituindo os `if channel ==` em `turn_router`/`orchestrator` e em
  `plugins/precheck_handoff.py::_post_booking`.
- Deploy não feito; exigiria o `secretaria-worker` (de preferência junto com a API).
