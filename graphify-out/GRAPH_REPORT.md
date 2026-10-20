# Graph Report - wt-sec-menu3  (2026-10-20)

## Corpus Check
- 694 files · ~1,151,950 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 14239 nodes · 36811 edges · 825 communities (628 shown, 197 thin omitted)
- Extraction: 83% EXTRACTED · 17% INFERRED · 0% AMBIGUOUS · INFERRED: 6362 edges (avg confidence: 0.74)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `e216873e`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- test_onboarding_cron.py
- _outbound
- test_reminders_plugin.py
- test_hub_professionals.py
- route
- test_action_buttons.py
- test_professional_notification.py
- test_hub_oauth.py
- tenants.py
- sent
- test_rebooking_flow.py
- test_webhook_history_sync.py
- Tenant
- test_hub_calendar_owner_agenda.py
- test_reactivation.py
- seed_world
- test_remove_context_command.py
- test_precheck_handoff.py
- tenant_config.py
- test_deposit_lifecycle.py
- test_hub_calendar_confirmation.py
- test_patient_name_step.py
- test_brain_message_interactive_tap.py
- set_google_refresh_token
- test_brain_onboarding.py
- onboarding_cron.py
- get_settings
- AsaasClient
- secretary_system_prompt
- test_typing_indicator.py
- test_internal.py
- test_bot_reply_gating.py
- test_hub_release.py
- test_hub_calendar_money.py
- test_booking_notifications.py
- test_hub_config.py
- test_express_confirmation_worker.py
- Patient
- Base
- test_message_delivery_status.py
- test_attendee_booking.py
- reminders.py
- SecretarIA
- PixDeposit
- test_professional_config_gap_alert.py
- tools.py
- calendar.py
- setup_world
- resolve_professional_calendar
- test_reminder_schedule.py
- Deliver `body` to `patient` over WhatsApp — the only channel a     Conversation
- test_internal_asaas_connection.py
- Appointment
- test_agent_tool_enforcement.py
- ConsentEvent
- BookingHold
- test_a_known_account_whose_name_brain_api_sends_reads_the_frame_then_the_menu
- test_precheck_handoff_plugin.py
- ProcessedEvent
- test_interactive_bubble_record.py
- test_agent_menu_tools.py
- conversations.py
- test_flow_router_multiprofessional.py
- load_tenant_config
- test_ai_tools_cancel_money.py
- test_precheck_handoff_tool.py
- PROMPT 2 — state-driven greetings, buttons, manage sub-flow, Outro handoff
- test_agent_retry.py
- extract_inbound_body
- test_confirm_recheck_slot.py
- professional_notification.py
- test_pii_pseudonymization.py
- professionals.py
- attachment_http.py
- cleanup_patients
- test_build_identity.py
- test_appointment_edit_apply.py
- CHECKPOINT — Google Calendar integration modes (per_professional / shared_account)
- SecretarIA
- Conversation
- test_flow_day_picker.py
- ReminderJob
- CHECKPOINT — Limites de texto do WhatsApp (constante única + corte marcado + trava no hub)
- CHECKPOINT — Onboarding & multi-professional configuration
- build_patient_calendar_link
- insurance_catalog.py
- test_convenio_catalogo_db.py
- run_agent
- test_booking_owner_persistence.py
- test_hub_calendar_health.py
- format_brl
- test_admin_tenants.py
- test_subscription.py
- test_hub_configuration.py
- test_convenio_catalogo_flow.py
- CHECKPOINT — Trio [Agendar][Gerenciar consulta][Outro] + LLM escopada em médico/serviço
- test_brain_message_attachments.py
- is_live_status
- CHECKPOINT — Fixed greeting buttons (product principle: LLM is the last resort)
- internal.py
- CHECKPOINT — Multi-doctor WhatsApp workflow (deterministic doctor selection)
- CHECKPOINT — Plugin architecture (entitlement-gated capabilities)
- cancellation_notice.py
- test_hub_staff_message.py
- Exception
- test_ai_v2_blindness.py
- hub_configuration.py
- test_debug_reset_menu3.py
- media_storage.py
- CHECKPOINT — WhatsApp Coexistence onboarding (Tasks 4 + 5)
- test_pix_deposit_plugin.py
- load_simulation_inputs
- test_webhook.py
- CHECKPOINT — Post-consult tenant config fields
- _reminders_v2.py
- insurance.py
- test_booking_code_gate.py
- CHECKPOINT — Inbound WhatsApp audio transcription
- CHECKPOINT — Pix deposit (sinal) add-on + appointment payment lifecycle
- config.py
- test_get_availability_tool.py
- test_menu_command.py
- test_flow_cancel_money.py
- test_bot_allowlist.py
- b06ff85998bf_pix_deposit_asaas.py
- f06e85b476f2_onboarding_tenant_professional_config_.py
- test_handover_echoes.py
- test_flow_router_insurance.py
- payments_tasks.py
- Professional
- reminder_schedule.py
- oauth.py
- __init__.py
- __init__.py
- __init__.py
- __init__.py
- __init__.py
- arq_worker.py
- __init__.py
- __init__.py
- Audio idempotency (ProcessedEvent claim)
- iter_audio_messages parser
- LGPD logging discipline (audio)
- OPENAI_SECRETARIA_MODEL vs OPENAI_TRANSCRIPT_MODEL split
- No audio persistence invariant
- transcribe_audio_message arq job
- transcription-core library
- WhatsApp Cloud API Coexistence model
- docs/ as source of truth
- Encryption at rest (Fernet)
- Eye Company placeholder rule
- Pure decision function (flow_router.route)
- Folder granularity (group by domain)
- Layering convention (api/workers/services/ai/models/core)
- SecretarIA (project instructions)
- secretary_system_prompt(tenant)
- Per-tenant config model
- postgres compose service
- redis compose service
- Bot/human handover state
- HMAC-SHA256 webhook signature verification
- Multi-tenant routing by phone_number_id
- Initial schema downgrade
- Initial schema upgrade
- models (Appointment, Conversation, Patient, Message, Tenant)
- AppointmentReminder
- analytics_bi plugin
- CHECKPOINT — Plugin architecture
- ehr plugin (provider seam)
- Entitlement gate on bot path
- Ferro core (gated end-to-end loop)
- human_backup_24_7 plugin
- LGPD privacy endpoints (export/erase)
- Migration chain (d7e8f9a0b1c2 → a4b5c6d7e8f9)
- multi_professional plugin
- multi_unit plugin
- pix_whatsapp plugin
- Plugin registry (PluginSpec / registry.py)
- reminders plugin
- Shared reply path (_persist_inbound_message / _send_bot_reply)
- Idempotency ledger pattern
- API component (secretaria.main:app)
- Easypanel deploy (same image, two services)
- Meta webhook (messages + smb_message_echoes)
- PreCheck hand-off via brain-api
- Worker component (arq WorkerSettings)
- extract_echo_body
- extract_inbound_body
- iter_audio_messages
- iter_event_ids
- appointment_duration_min
- _calendar_ctx (ContextVar)
- _conversation_id_ctx (ContextVar)
- Lista até `max_slots` horários livres de 30 minutos em `day`, dentro do     horá
- _redis_ctx (ContextVar)
- _tenant_config_ctx (ContextVar)
- _tenant_id_ctx (ContextVar)
- Health router
- test_resolve_tenant.py
- test_ai_v2_conversations.py
- api/internal.py (require_internal_api_key)
- scoped_help.py
- async_session_factory
- async engine
- core/subscription.py (brain-api hub verification)
- The result of validating a subscription token.      tenant_id: which tenant the
- Validate a bearer token and return its subscription claim, or None.      MVP imp
- MessageDirection enum
- MessageSender enum
- test_ia_atende_como_pessoa.py
- load_tenant_insurance
- code:sh (make install            # or: uv sync)
- plugins/multi_professional.py
- plugins/multi_unit.py
- plugins/post_booking.py
- test_brain_message_recent_page.py
- test_calendar_events_insurance.py
- CHECKPOINT — Pseudonimização de PII antes do modelo de IA
- render_greeting
- test_lgpd_consent_gate.py
- scrub_messages
- CalendarService._build_service
- CalendarService.cancel_event
- CalendarService.check_availability
- CalendarService.create_event
- make_appointment
- Return free [start, end) slots on `day` within business hours.          Walks th
- SCOPES = calendar.events
- test_brain_message_open.py
- services/entitlements_client.py
- Decide the next deterministic step for this inbound turn.      `patient_name` is
- PrecheckClient
- services/tenant_config.py (TenantRuntimeConfig)
- WebhookValue
- test_appointment_edit_flow_menu.py
- test_agent_capability_cache.py
- client fixture (ASGITransport)
- A Tenant ORM instance with a plaintext access_token to catch leaks.
- test_audio_transcription
- TASK-008 — Modos de aceitação + catálogo extensível + convênio "Outro"
- test_handover_echoes
- test_hub_conversations
- test_list_patient_appointments_tool.py
- test_ai_toolset_v2.py
- test_staff_appointment_edit.py
- HMAC signature tests
- Webhook endpoint tests
- TypedDict
- EntitlementSummary
- WhatsAppClient
- _seed
- resolve_opening_message
- test_human_backup_plugin.py
- test_hub_conversations.py
- Parte 1 — `/menu` deixou de ser destrutivo (PROMPT_FIX_18)
- 3. O que entrou onde
- CHECKPOINT — Seletor de dia/horário reaproveitável + entrada determinística
- CHECKPOINT — e-mail + OTP inline no Brain-Message
- test_reminder_v2_hub_wiring.py
- AgendaViewer
- _persist_brain_message_inbound
- analytics_advanced
- test_emoji_decoration.py
- test_blind_staging_tools.py
- request_precheck_handoff
- CHECKPOINT — Anexos no Portal (Brain-Message), parte 2: secretarIA (armazenamento, envio e recebimento)
- test_admin.py
- BookingDraft
- CHECKPOINT — QA ao vivo do canal Brain-Message em produção (2026-09-09)
- CHECKPOINT — PreCheck oferecido automaticamente logo depois do agendamento
- secretary_system_prompt_v2
- CHECKPOINT — Catálogo canônico de serviços da clínica (FEAT 35)
- test_calendar_edit_methods.py
- draft_for
- CHECKPOINT — Estado da mensagem (enviado / entregue / lido / falhou), parte 1: secretarIA
- test_hub_config_pix.py
- get_current_tenant
- CHECKPOINT — `RESCHEDULED` é status VIVO (taxonomia unificada)
- test_handback_events.py
- _resolve_unit_or_error
- CHECKPOINT — Fluxos determinísticos incondicionais (fim do gate `initial_flows.enabled`)
- _FakeWhatsApp
- staff_appointment_edit.py
- PluginSpec
- attachments.py
- get_conversation
- test_waba_fail_closed.py
- test_arq_log_secret_redaction.py
- test_r7_calendar_origin.py
- test_patient_channel_identity.py
- test_post_booking_plugin.py
- test_reminder_v2_text.py
- test_professional_null_vs_empty.py
- env.py
- Parte 2 — `start_guided_booking`: a LLM devolvendo o agendamento aos botões
- _rows
- Jornada do paciente — WhatsApp e Portal, em linguagem leiga
- TASK-030 P2a — testes após deploy
- CHECKPOINT — Moldura de saudação + aviso de LGPD (2026-08-31)
- CHECKPOINT — FIX 32: a chave de idempotência devolvida para um retry inexistente
- CHECKPOINT — E-mail ao profissional quando uma consulta é marcada
- test_tasks_helpers.py
- CHECKPOINT — alerta de profissional com configuração incompleta (FEAT 41 + FIX 34)
- test_professional_calendar_wire.py
- Achados e delimitação da causa
- CHECKPOINT — CORS em formato JSON derrubou todo o portal do médico
- resolve_booking_day
- test_handoff_notification.py
- normalize
- reload_appointment
- appointment_change_notifications.py
- test_appointment_edit_flow_fields.py
- CHECKPOINT — botões e listas como dado: `messages.interactive` (2026-09-11)
- test_cancel_event_ownership.py
- test_onboarding_cadence.py
- _should_inject_post_consult_knowledge
- as_utc
- BrainMessageSender
- CHECKPOINT — toque real no Portal do paciente (canal Brain-Message) (2026-09-12)
- _ctx
- Enum
- _FakeWhatsAppClient
- test_main_menu_manage_entry.py
- EditDraft
- fetch_patient_email_result
- test_extra_reminder.py
- Identidade de paciente por canal — migração aditiva
- test_whatsapp_limits.py
- test_brain_message_pipeline.py
- CHECKPOINT — mensagens do console de staff (hub/conversations)
- _WorkersNamespace
- .calendar_for
- minimal_event_payload
- PostBookingContext
- delete_tenant
- CHECKPOINT — toque em lista: título pra exibir, id pra rotear (2026-09-11)
- test_multi_professional_plugin.py
- availability_tool.py
- fetch_professional_emails
- conftest.py
- TASK-030 — captura completa e datas em linguagem natural
- summarize_turn
- CHECKPOINT — o código de 6 dígitos vira portão antes da consulta existir
- b4c2e8f1a9d3_drop_professional_email.py
- d2a5b9c7e3f1_patient_lgpd_accepted_at.py
- e3b7c1d5a9f2_drop_tenant_greeting_message.py
- manage_request.py
- test_turn_safety_net.py
- reminder_text.py
- BookingDraftRequested
- test_availability_windows_scan.py
- load_pseudonymizer
- canonical_service_name
- 4c8e2a7f1b93_message_interactive.py
- 9d3b7e1f5a2c_message_interactive_reply_id.py
- panel.py
- CHECKPOINT — Portal: a conversa mostra as mensagens MAIS RECENTES (TASK-028)
- test_reminder_v2_hooks.py
- test_brain_patients.py
- confirmation_warnings.py
- Lembretes R7 — Ações da clínica na agenda avisam o paciente (API) Implementation Plan
- build_identity
- code:python (# models/appointment.py)
- code:block1 (PYTHONPATH="src;.venv/Lib/site-packages" \)
- code:block1 (event_type = "context_removed")
- code:block1 (https://calendar.google.com/calendar/render?action=TEMPLATE&)
- code:block1 (channel_null | external_id_null | total_rows          -> 0 |)
- code:block1 (professional_config:alert:{tenant_id}:{professional_id}:{gap)
- code:block2 (fetch_professional_emails(tenant_id) -> dict[str, str] | Non)
- CalendarService
- code:block1 (grep -rn "ChatOpenAI(" src --include=*.py     # 2 resultados)
- O que o guard NÃO resolve (e não finja que resolve)
- code:block1 (GET    /tenants/me/services                -> ServiceRead[])
- code:block1 ("Consulta de rotina adulto"    -> fronteira de palavra -> "C)
- Parse CORS_ALLOW_ORIGINS into a clean list of origins.          Trailing slash
- code:block2 ("Consulta de rotina adul…"  /  "Consulta de rotina infa…")
- appointment_types
- business_hours
- friday
- monday
- saturday
- Pipeline channel-neutro + endpoints internos do Brain-Message
- thursday
- CHECKPOINT — IA entra em qualquer etapa: `get_availability`, ferramentas de agenda cegas e o conjunto v2 (TASK-030 P4)
- CHECKPOINT — pergunta de nome na abertura (WhatsApp + Portal) e ramo "e-mail já cadastrado" (2026-09-21)
- tuesday
- wednesday
- clinic_description
- clinic_name
- test_reminder_confirmation.py
- contact_email
- google_calendar_id
- initial_flows
- test_calendar_unavailable_mapping.py
- message_status.py
- menu_label
- reactivation
- is_active
- language
- test_reminder_v2_delivery.py
- test_ai_tool_output_allowlist.py
- persona_notes
- buttons
- continue_prompt
- enabled
- gap_minutes
- 5e1f9a3c7d20_message_attachment.py
- returning_greeting_message
- timezone
- code:sh (make makemigration m="describe the change"   # autogenerate )
- code:block11 (uvicorn secretaria.main:app --host 0.0.0.0 --port 8000)
- code:block12 (arq secretaria.workers.arq_worker.WorkerSettings)
- code:block13 (DATABASE_URL=postgresql+asyncpg://USER:PASS@secretaria-postg)
- code:sh (curl -s https://<api-host>/build)
- code:json ({)
- code:block16 (src/secretaria/)
- code:sh (cp .env.example .env)
- code:sh (make up                 # or: docker compose up -d)
- code:sh (make migrate            # or: uv run alembic upgrade head)
- code:sh (make seed               # or: uv run python scripts/seed_dev)
- code:sh (make dev                # or: uv run uvicorn secretaria.main)
- code:sh (make worker             # or: uv run arq secretaria.workers.)
- code:sh (curl "http://localhost:8000/webhook?hub.mode=subscribe&hub.v)
- code:sh (make test               # or: uv run pytest)
- 8b4d2f6e1a37_message_delivery_status.py
- CHECKPOINT — Portal: a automação fala primeiro, o aviso do código vira card, e o PreCheck sai pelo Portal
- _flow_handback_tools
- db
- CHECKPOINT — conta ativa do Portal abre clínica nova: sem e-mail/código; nome vem da conta ou é perguntado uma vez (2026-09-24)
- CHECKPOINT — Agendar para outra pessoa ("Essa consulta é pra você?")
- CHECKPOINT — OTP / reset link leaking through arq's native job log
- _r7_support.py
- _post_booking
- _connect_calendar
- _handle_remove_context_command
- BrokenRedis
- CHECKPOINT — Onda 4: jornada sem gate ponta a ponta (E2E, sem PreCheck)
- history_item_is_final
- test_reminder_r3_cancel_path.py
- d4f8a2c6e913_insurance_mode_catalog_ext_custom.py
- Digitando — backend universal (TASK-033)
- _run_hand_back_with_intro
- A patient, identified by their WhatsApp id (wa_id) within a tenant.
- Lacunas encontradas
- test_brain_message_email_otp_inline.py
- secretaria
- File Structure
- Return the human-readable text body of an inbound message.      Handles text m
- Persist one outbound message. Returns the empty send response.          `{}` o
- c1d4a8e6f2b0_message_interactive_null_backfill.py
- Referência — como convênios de saúde pagam a consulta no Brasil (2025/2026)
- test_workers_layering.py
- outbound_messages
- CHECKPOINT — A IA atende como pessoa; humano por último (TASK-038)
- CalendarUnavailableError
- File Structure
- c9f4e2a7b815_insurance_catalog.py
- Portal: visita descartada ao entrar na conta — TASK-034
- test_cancellation_notice.py
- CHECKPOINT — observabilidade da LLM e "paciente nunca sem resposta" (2026-10-01)
- availability_windows.py
- Índice de execução — Portal: mensagens, visita fundida, "digitando" (2026-10-01)
- test_clinic_facts_hub.py
- reminder_hooks.py
- File Structure
- Checkpoint — MVP da secretarIA no Portal
- CHECKPOINT — divisão de `workers/tasks.py` (TASK-023)
- _WireClient
- CHECKPOINT — "Outro" responde a pergunta fixa; testes reais dos outros pontos de entrada da IA (TASK-037)
- TASK-030 — reteste do P3 publicado
- test_analytics_bi_plugin.py
- test_express_confirmation.py
- free_slots_for_day
- Review Focus
- File Structure
- _run_send_bot_reply_capturing_run_agent
- parse_patient_name
- db
- Lembrete com "Alterar Dados" Implementation Plan
- decode_action_id
- PatientOpeningContext
- __init__.py
- logging.py
- __init__.py
- __init__.py
- _seed
- test_availability_windows.py
- Lembrete com "Alterar Dados" — Design
- test_ai_toolset_v2_locks.py
- Part B — Brain-Message-Frontend (after R5 is merged)
- CHECKPOINT — Lembretes R2: motor de lembretes (TASK-032)
- guard_reply
- test_multi_unit_plugin.py
- CHECKPOINT_tenant_patient_cleanup.md
- test_cors_origins_parsing.py
- test_hub_conversations_my_patients.py
- _booking_context
- test_hub_config_extra_reminder.py
- Modelos do WhatsApp para os lembretes (TASK-032 R2)
- main
- _load_snapshot
- R9 — PreCheck out of the console + extra reminder "N dias antes, às HH:MM" Implementation Plan
- verify_meta_signature
- backfill_service_catalog.py
- test_patient_context.py
- booking_draft.py
- TASK-032 R6 — lembrete com Alterar Dados
- TASK-037/038 — testes reais após deploy (2026-10-07)
- test_deposit_release_preview.py
- test_appointment_edit_pure.py
- test_simulation_api.py
- test_service_guides_multipro.py
- _resolve
- ManageRequest
- FakeCalendar
- appointment_edit_flow.py
- CHECKPOINT — Lembretes R7: ações da clínica na agenda avisam o paciente (TASK-044)
- config.py
- test_workers_ns_patching.py
- handoff_notification.py
- FastAPI
- Settings
- _GoogleEvents
- _first_decision
- build
- test_patient_email_pii.py
- calendar.py
- _Agenda
- TASK-032 — verificação após deploy, 08/10/2026
- AppointmentType
- test_hub_edit_appointment.py
- test_portal_context_opening.py
- test_appointment_replacement_router.py
- Primeira mensagem de quem tem consulta — mesmo lembrete, mesmos fluxos
- test_hub_deps.py
- ClinicFacts
- staff_transition
- Entry was pre-checked and allowed it, but a race left the deposit     already at
- The body a day-row tap produces (schemas.webhook.extract_inbound_body).
- The body a prof-row tap produces (see schemas.webhook.extract_inbound_body).
- _seed_tenant
- CHECKPOINT — IA v2: prompt e regras (TASK-030 P5)
- E-mail fixo ao médico após confirmar Alterar Dados
- _FakeCalendarService
- Ações da clínica na agenda avisam o paciente — design (2026-10-09)
- _CapturingLogger
- test_booking_draft_continuation.py
- test_booking_draft_resume_questions.py
- Subagendas no convite e em “também atende” — TASK-054
- staff_welcome.py
- test_appointment_change_notifications.py
- test_clinic_context_backend.py
- CHECKPOINT — Lembretes R3: abertura do chat e caminho do Cancelar (TASK-032)
- draft_from_record
- test_reminder_v2_wiring.py
- TASK-030 — prova real da captura e das datas após deploy
- TASK-030 — reteste após deploy do P4, 2026-10-07
- datetime
- test_flow_replaces_column.py
- internal_provisioning.py
- extract_action_button
- viewer_from_claim
- test_hub_events_visibility.py
- test_availability.py
- a7e2c9d4f1b6_conversation_flow_replaces_appointment.py
- test_handback_log.py
- _route_inbound_turn
- brain_onboarding.py
- pending_identity.py
- get_agenda_viewer
- _Handoff
- encrypt
- simulation.py
- test_confirmation_warning_email.py
- test_manage_v2_handback.py
- TenantRuntimeConfig
- test_manage_request.py
- decide_reminder_opening
- strip_decoration
- TenantConfigUpdate
- TASK-041 — edição pela IA sem perder a consulta
- FakeAsaasClient
- apply_appointment_edit
- MonkeyPatch
- context_completeness.py
- build_conversation_state
- CHECKPOINT — Lembretes R4: avisos, liberar horário e mensagem (TASK-032)
- next_retry_time
- _FakeWhatsAppClient
- turn_safety_net.py
- Lembretes R2 — Motor de lembretes Implementation Plan
- 4. Desenho
- _FakeAsyncClient
- _Queue
- _CalendarPerId
- _notify_portal
- CHECKPOINT — "Meus pacientes" no console (TASK-046 R8, parte A)
- Retenção de visitas vazias + espera de código de conta conhecida — TASK-042 (lado secretarIA)
- TASK-041 — prova real da edição no Portal, 09/10/2026
- test_reminder_r3_continuations.py
- CHECKPOINT — remediação lembretes R6 (e-mail ao médico + entrada pelo link)
- test_back_leaves_for_the_menu_without_touching_the_appointment
- test_scoped_help.py
- Lembretes R4 — Avisos por e-mail, estado vermelho, liberar horário e mensagem ao paciente (API) Implementation Plan
- receive_webhook
- _FakeArqPool
- Release R5 / R7 / R8 — 2026-10-10 (TASK-047)
- Para o P5 (o que a IA passa a ver — texto e nomes exatos)
- Lembretes R3 — Primeira mensagem no chat e caminho do Cancelar Implementation Plan
- test_migration_professional_edit_notices.py
- _FakeWhatsAppClient
- _on_inbound
- _apply_deposit_awareness
- test_draft_wait_questions.py
- setup_logging
- test_edit_day_picker.py
- AST
- _CalendarWithBusy
- _ExplodingWhatsAppClient
- _FakeCalendarService
- SlotsBubble
- File Structure
- File Structure
- Interfaces
- Lembretes R1 — Fundação Implementation Plan
- run_post_booking_hooks
- datetime
- _handback_names
- _WhatsApp
- test_a_returning_portal_patient_gets_the_resume_offer_on_the_portal
- _LogRecorder
- test_agent.py
- _FakeCalendarService
- Lembretes R5 — Front da agenda (cores, ✓/✓✓, gaveta com ações por horário, avisos ao paciente, Editar/Remarcar, legenda) e configuração "Lembretes e avisos" Implementation Plan
- Lembretes, primeira mensagem e estado de confirmação da consulta — Design
- FakeCalendar
- _FakeWhatsAppClient
- _FakeAsaasClient
- get_context_completeness
- _FailingWhatsAppClient
- _State
- _Mailer
- IA entra em qualquer etapa — P2a: disponibilidade, rascunho v2, `flow_draft` e o resolvedor — Implementation Plan
- test_context_fields_are_keyword_only
- .supplied_fields
- test_resend_asks_for_a_new_code_and_re_emits_the_same_card
- test_express_confirmation_needs_the_clinic_switch
- _FakeCalendarService
- _fake_tool
- _config
- _report
- _RecordingLogger
- main
- .resolve
- parse_code
- _post_booking
- parse_email
- brain_professionals.py
- _FakeArqPool
- test_both_answers_are_typed_in_the_published_openapi
- IA entra em qualquer etapa — P2b: hand-backs pelo resolvedor, "pra quem" sempre perguntado, ferramenta v2, interruptor e estado da conversa — Implementation Plan
- TenantConfigRead
- booking_details_text
- .request
- handback_log.py
- test_worker_registry_is_complete
- test_the_retry_gives_up_and_tells_the_clinic
- .__init__
- test_the_fast_ack_now_carries_receipts_without_the_phone
- test_external_id_is_keyword_only
- Antes de começar (uma vez)
- test_confirmation_card.py
- CHECKPOINT — Contexto da clínica que a LLM enxerga (TASK-025, parte backend)
- Lembretes R1 — Fundação (TASK-032)
- test_flow_draft_column.py
- CHECKPOINT — IA entra em qualquer etapa: rascunho v2, `flow_draft` e o resolvedor (TASK-030 P2)
- CHECKPOINT — Portal: a primeira mensagem chega ao ENTRAR, escolhida pelo contexto (TASK-035)
- test_migration_reminders_foundation.py
- CHECKPOINT — confirmação expressa, retomada do rascunho e gerenciamento v2 (TASK-030 P3)
- TASK-030 — IA entra em qualquer etapa: P2a

## God Nodes (most connected - your core abstractions)
1. `Tenant` - 751 edges
2. `Appointment` - 358 edges
3. `Conversation` - 334 edges
4. `seed_world()` - 298 edges
5. `Patient` - 265 edges
6. `db()` - 230 edges
7. `Professional` - 200 edges
8. `route()` - 188 edges
9. `FlowRouterResult` - 166 edges
10. `Base` - 159 edges

## Surprising Connections (you probably didn't know these)
- `downgrade()` --calls--> `decrypt()`  [INFERRED]
  migrations/versions/d7e8f9a0b1c2_encrypt_waba_token.py → src/secretaria/core/crypto.py
- `main()` --calls--> `get_settings()`  [INFERRED]
  scripts/gcal_auth.py → src/secretaria/config.py
- `test_the_card_body_never_exceeds_the_interactive_limit()` --indirect_call--> `ButtonBubble`  [INFERRED]
  tests/test_confirmation_card.py → src/secretaria/ai/formatter.py
- `test_confirm_block_needs_no_limit_because_its_labels_are_fixed()` --calls--> `ButtonBubble`  [INFERRED]
  tests/test_prompts.py → src/secretaria/ai/formatter.py
- `test_show_main_menu_tool_raises()` --indirect_call--> `ShowMainMenuRequested`  [INFERRED]
  tests/test_agent_menu_tools.py → src/secretaria/ai/tools.py

## Import Cycles
- None detected.

## Communities (825 total, 197 thin omitted)

### Community 0 - "test_onboarding_cron.py"
Cohesion: 0.11
Nodes (78): _capture_email(), _capture_events(), _capture_usage(), db(), _fake_list(), _fakes(), _item(), _make_tenant() (+70 more)

### Community 1 - "_outbound"
Cohesion: 0.12
Nodes (32): _bm_turn(), _flow_state(), _outbound(), Sim" re-sends the code card - without "Sua consulta já está confirmada!"., One inbound on the Brain-Message channel, decided AND delivered., Every bot message this tenant's conversation has sent, in order., CHECKLIST: greeting without LGPD -> e-mail question -> LGPD -> normal flow., A portal-only clinic can converse without a live WhatsApp number.      `Tenant (+24 more)

### Community 2 - "test_reminders_plugin.py"
Cohesion: 0.12
Nodes (49): db(), _deposit_button_ids(), _entitled_fake(), _FailFirstTemplateClient, _fake_get_waba_token(), _fakes(), _FakeWhatsAppClient, _ledger_count() (+41 more)

### Community 3 - "test_hub_professionals.py"
Cohesion: 0.11
Nodes (67): _allow_multi_professional(), _connect_clinic(), db(), _entitled_fake(), _FakeCalendarServiceFactory, _FakeSecondaryCalendar, _never_called_fake(), _patch_calendar_service() (+59 more)

### Community 4 - "route"
Cohesion: 0.06
Nodes (113): Route one turn with `gate` in scope for every listing/booking branch.      The, Decide the next deterministic step for this inbound turn.      `patient_name`, route(), test_a_clear_request_is_not_the_generic_menu(), test_route_from_idle_drops_a_stale_marker(), test_the_idle_menu_is_the_generic_menu(), test_flow_confirmation_includes_insurance_in_event(), test_without_an_anchor_the_picker_is_unchanged() (+105 more)

### Community 5 - "test_action_buttons.py"
Cohesion: 0.14
Nodes (37): _async_return(), db(), _greeting_button_msg(), datetime, Tests for the reminder action-button routing (PROMPT S3 section 3).  Two layer, tenant + patient + conversation + one appointment (no google_event_id     -> _e, total retention + inside window -> CANCELLED_RETAINED needs no PSP     call at, Minimal calendar for the reminder-button reschedule hand-off.      `_handle_ac (+29 more)

### Community 6 - "test_professional_notification.py"
Cohesion: 0.07
Nodes (68): _claimed(), _ctx(), db(), log(), _make_rows(), _patch_agenda_url(), _patch_lookup(), _patch_session_factory() (+60 more)

### Community 7 - "test_hub_oauth.py"
Cohesion: 0.18
Nodes (26): db(), _extract_state(), _fake_exchange(), professional(), AsyncClient, MonkeyPatch, Tests for api/hub/oauth.py — Google Calendar OAuth start/callback/disconnect, i, Reconnecting is how an existing tenant upgrades a token minted before     calen (+18 more)

### Community 8 - "tenants.py"
Cohesion: 0.10
Nodes (31): delete_tenant(), _derived_health(), list_tenants(), _list_tenants_page(), _probe_config(), _probe_page(), AsyncSession, description (+23 more)

### Community 9 - "sent"
Cohesion: 0.12
Nodes (52): menu_label(), The question shown above the menu buttons., (id, label) pairs of a reminder: Confirmar / Cancelar / Alterar Dados., reminder_buttons(), age_conversation(), chat_rows(), Everything the WhatsApp double sent, in order., One inbound turn: route inside a transaction, then (by default) answer it. (+44 more)

### Community 10 - "test_rebooking_flow.py"
Cohesion: 0.12
Nodes (38): _bodies(), _Calendar, _conversation(), _professional(), Rebooking after the DOCTOR cancelled, and the reason when the patient won't., Drive the pair exactly as workers/tasks.py does: resolve the candidates     fir, The patient chose both a moment ago; asking again would be the flow     forgett, The cancelled booking's service is 45 min, the tenant default 30. A scan     at (+30 more)

### Community 11 - "test_webhook_history_sync.py"
Cohesion: 0.18
Nodes (30): db(), _history_value(), MonkeyPatch, Tests for the `history` / `smb_app_state_sync` webhook fields (contract v1 §10):, A single-chunk sync (or an immediate decline) can skip in_progress     entirely, The FIELD itself arriving (even with an empty body) is already the     signal t, A `contact` sub-object carrying a real name/phone must never appear in     any, Same LGPD guarantee for `history` - message bodies inside `threads`     must ne (+22 more)

### Community 12 - "Tenant"
Cohesion: 0.02
Nodes (197): A clinic using SecretarIA.      The system is multi-tenant in the data model., Tenant, booking_topology(), Name this tenant's booking topology from its ACTIVE professionals roster., Does this sender write the outbound `Message` row itself?      Read through `g, sender_persists_outbound(), ai_draft_v2_enabled(), booking_gate_scope() (+189 more)

### Community 13 - "test_hub_calendar_owner_agenda.py"
Cohesion: 0.09
Nodes (68): _appointment(), _appointment_count(), _at(), _cancel(), clinic(), _events(), _FakeGoogle, google() (+60 more)

### Community 14 - "test_reactivation.py"
Cohesion: 0.05
Nodes (88): classify_yes_no(), llm_state_ttl_minutes(), _professionals_offering(), The question appended to the returning greeting (e.g. 'Quer continuar?')., The [yes, no] reply-button labels for the continue prompt (max 3)., Classify a continue-prompt answer as "yes", "no", or "other".      Matched aga, The active professionals whose own catalog contains `service_name`., `_resume_bubbles` with attendee and convênio carried forward (`_carry_booking`). (+80 more)

### Community 15 - "seed_world"
Cohesion: 0.10
Nodes (73): add_reminder(), get_reminder(), datetime, UUID, One clinic, one patient, one conversation, one appointment., A planned row, as R1's `schedule_reminders` would have written it., seed_world(), test_a_time_change_updates_the_same_row_and_replans_the_reminders() (+65 more)

### Community 16 - "test_remove_context_command.py"
Cohesion: 0.15
Nodes (32): AnalyticsEvent, AnalyticsEvent model - minimal, non-personal usage events (analytics_bi addon)., One minimal, non-personal business event for a tenant., _count(), db(), MonkeyPatch, Tests for `/dangerously-remove-context` — the destructive reset (PROMPT_FIX_18)., The rehearsal must be message-for-message what a newcomer receives.      This (+24 more)

### Community 18 - "test_precheck_handoff.py"
Cohesion: 0.09
Nodes (55): HandoffResult, The outcome of one `request_precheck_handoff` call., _fresh_settings(), _install_fake_client(), log(), _LogRecorder, _posted_body(), MonkeyPatch (+47 more)

### Community 19 - "tenant_config.py"
Cohesion: 0.06
Nodes (79): decrypt(), Decrypt a ciphertext produced by `encrypt`. Raises EncryptionError if     the t, ProfessionalCredentials, At most one row per professional, holding its own encrypted Calendar refresh tok, One row per tenant holding its encrypted Google Calendar refresh token., TenantCredentials, calendar_credential_health(), CalendarCredentialHealth (+71 more)

### Community 20 - "test_deposit_lifecycle.py"
Cohesion: 0.18
Nodes (36): db(), _install_fake_client(), _install_fake_whatsapp(), AsyncSession, datetime, MonkeyPatch, Tests for services/payments/deposit_lifecycle.py — the Pix deposit money brain., Tenant + patient + one appointment, all in the SAME open session. (+28 more)

### Community 21 - "test_hub_calendar_confirmation.py"
Cohesion: 0.22
Nodes (31): Invite `email` to an existing event; Google notifies them. Idempotent., _booked(), _connect(), _events(), _patch(), AsyncClient, Hub agenda API: confirmation state on GET /events and PATCH status (TASK-032 R1,, Appointment 6 days out (real clock for the PATCH tests) with its 3 rows. (+23 more)

### Community 22 - "test_patient_name_step.py"
Cohesion: 0.08
Nodes (73): _age_conversation(), _bm_turn(), _conversation(), db(), _llm_history(), _outbound(), _patient(), MonkeyPatch (+65 more)

### Community 23 - "test_brain_message_interactive_tap.py"
Cohesion: 0.09
Nodes (41): api(), db(), _ExplodingWhatsAppClient, _FakeArqPool, _inbound_row(), _offer_doctor(), _post_inbound(), MonkeyPatch (+33 more)

### Community 24 - "set_google_refresh_token"
Cohesion: 0.10
Nodes (50): Encrypt and upsert the Calendar refresh token. Caller commits., set_google_refresh_token(), db(), _FakeArqPool, AsyncClient, MonkeyPatch, Tests for the internal onboarding/provisioning API (`/internal/*`, contract v1 §, Records every enqueue_job call; installed on app.state.arq_pool. (+42 more)

### Community 25 - "test_brain_onboarding.py"
Cohesion: 0.12
Nodes (30): _configured_settings(), _FakeAsyncClient, _FakeResponse, _install_fake_client(), Exception, MonkeyPatch, Tests for services/brain_onboarding.py — the onboarding-cron -> brain-api clien, Corrections round "Task 2": once brain-api ships the three test-window     fiel (+22 more)

### Community 26 - "onboarding_cron.py"
Cohesion: 0.16
Nodes (24): _month_bounds_utc(), _process_config_reminder(), _process_retry_and_closing(), _process_test_window(), Counter, datetime, ZoneInfo, Onboarding lifecycle crons (cross-service contract v1 §11): retry nudges, D+30 (+16 more)

### Community 27 - "get_settings"
Cohesion: 0.04
Nodes (77): dict, get_settings(), Return a cached Settings instance (read once per process)., EmailTemplate, Outbound email — operational alerts + onboarding transactional templates.  Use, Email the clinic owner when the human_backup_24_7 addon engages     (an inbound, Email the clinic when a patient could NOT be told their consultation was cancell, Email the clinic that a reminded patient has not confirmed (TASK-032 R4). (+69 more)

### Community 28 - "AsaasClient"
Cohesion: 0.06
Nodes (53): AsaasClient, AsaasError, _extract_code_hint(), Response, Thin async client for the Asaas v3 API (Pix deposit charges).  Each tenant sup, POST /payments (billingType=PIX). `due_date` ("YYYY-MM-DD") is the         call, GET /payments/{id}/pixQrCode -> {payload, encodedImage, expirationDate, ...}., POST /payments/{id}/refund. `value_cents=None` omits "value" entirely         ( (+45 more)

### Community 29 - "secretary_system_prompt"
Cohesion: 0.06
Nodes (77): base_tools_for(), The base tools a turn on `topology` may use (services/booking_scope.py)., _format_appointment_context(), Render the "CONSULTAS MARCADAS DESTE PACIENTE" block, or "" when unset.      `, Render the full system prompt for a specific tenant., secretary_system_prompt(), Opt-in: the real model transfers the user's stated date and attendee.  Only th, test_real_model_keeps_selected_day_when_answering_only_the_time() (+69 more)

### Community 30 - "test_typing_indicator.py"
Cohesion: 0.09
Nodes (39): AsyncFakeRedis, typing_pool(), _BrokenRedis, client(), _conversation_id(), db(), datetime, The "digitando" flag: three typers, two viewers, never a source of errors. (+31 more)

### Community 31 - "test_internal.py"
Cohesion: 0.12
Nodes (29): _clear_override(), _FakeResult, _FakeSession, _make_appointment(), _make_patient(), _override_session(), AsyncClient, MonkeyPatch (+21 more)

### Community 32 - "test_bot_reply_gating.py"
Cohesion: 0.16
Nodes (31): _assert_llm_never_called(), _flows_of(), _greeting_button_reply(), _make_conversation(), MonkeyPatch, Tests for the entitlement gate + per-tenant WhatsApp client wiring inside worke, A turn to feed `_send_bot_reply`.      The default body is unmatched free text, A pre-deploy free-text button's numeric id (schemas/webhook.py::     extract_gr (+23 more)

### Community 33 - "test_hub_release.py"
Cohesion: 0.11
Nodes (56): _appointment(), _approve_paid_notices(), _connect(), _deposit_status(), _FakeArqPool, _install_pool(), _paid_world(), _policy() (+48 more)

### Community 34 - "test_hub_calendar_money.py"
Cohesion: 0.29
Nodes (19): _connect_calendar(), db(), AsyncClient, datetime, Tests for api/hub/calendar.py's Pix-deposit money hooks (PROMPT S3 section 4, i, The deposit notice still rides along with the cancellation — it just     rides, PROMPT_FIX_16: the hub and the patient-facing readers must agree.      A docto, _seed_appointment() (+11 more)

### Community 35 - "test_booking_notifications.py"
Cohesion: 0.10
Nodes (34): _ctx(), _persist_plan(), Hook post_booking: confirmações ao paciente e à clínica (MVP Portal, Task 7B)., Live 2026-10-05: "Nova consulta marcada" opened with "Agendamento: <uuid>"., Live 2026-10-19: "Particular" read "Convênio informado; consulte o agendamento"., test_a_private_booking_is_named_particular_in_the_emails(), test_absent_insurance_omits_insurance_line(), test_another_clinics_doctor_is_never_named() (+26 more)

### Community 36 - "test_hub_config.py"
Cohesion: 0.08
Nodes (41): db(), AsyncClient, Tests for api/hub/config.py — PUT /tenants/me/config, focused on the contract v, The greeting's buttons are now a fixed, product-defined set     (workers/tasks., The tenant fixture has phone_number_id=None and no Calendar connected -     a p, A completely unrelated config field (no address/insurance at all)     must also, PUT no longer accepts this field at all: an incoming `greeting_buttons`     is, The greeting is ALWAYS sent with the fixed action buttons attached (no     more (+33 more)

### Community 37 - "test_express_confirmation_worker.py"
Cohesion: 0.14
Nodes (28): _Agenda, _confirm(), _count(), db(), _events(), _land(), _LogRecorder, _other_portal_conversation() (+20 more)

### Community 38 - "Patient"
Cohesion: 0.13
Nodes (48): ConversationPiiTokenMap, `{token: valor real}` for one conversation, grown turn by turn., Message, One WhatsApp message, inbound or outbound., Patient, A patient, identified by (tenant, channel, external_id).      Historically tha, discard_empty_visit(), discard_visit() (+40 more)

### Community 39 - "Base"
Cohesion: 0.03
Nodes (133): BaseModel, DeclarativeBase, _interactive_read(), _ListSignals, What the console list needs beyond the conversation row (see ConversationRead)., The row's `interactive` blob in its wire shape, or None (tolerant)., Base, Declarative base shared by every ORM model. (+125 more)

### Community 40 - "test_message_delivery_status.py"
Cohesion: 0.16
Nodes (39): MessageStatus, The delivery status a bubble draws, derived from the row's timestamps.      Fa, status_of(), db(), _deliver_receipt(), _meta_receipt(), datetime, Delivery state (enviado / entregue / lido / falhou), part 1 of 3 (secretarIA). (+31 more)

### Community 41 - "test_attendee_booking.py"
Cohesion: 0.05
Nodes (76): authorization_body(), is_attendee_name_question(), parse_attendee_name(), Booking for someone else: "Essa consulta é pra você?" (both channels).  Until, The authorization sentence with the WHOLE stored name interpolated., The attendee's name from a typed answer, or None when it is not a name.      S, Whether an outbound row asked for the attendee's name (prefix match).      Pre, _book() (+68 more)

### Community 42 - "reminders.py"
Cohesion: 0.16
Nodes (23): _as_utc(), _claim_reminder(), _emit_reminder_usage(), _last_inbound_at(), _meta_language_code(), _process_lead_window(), datetime, timedelta (+15 more)

### Community 44 - "SecretarIA"
Cohesion: 0.09
Nodes (22): 1. Install dependencies, 2. Create your .env, 3. Start Postgres + Redis, 4. Apply database migrations, 5. (optional) Seed a development tenant, 6. Run the API, 7. Run the worker (in a second terminal), Architecture (+14 more)

### Community 45 - "PixDeposit"
Cohesion: 0.11
Nodes (34): PixDeposit, One Pix deposit (sinal) charge tied to exactly one appointment., ProcessedAsaasEvent, One row per Asaas event id already handled.      The unique constraint on `eve, db(), _FakeArqPool, _FakeCalendarService, _fakes() (+26 more)

### Community 46 - "test_professional_config_gap_alert.py"
Cohesion: 0.06
Nodes (62): _confirm_service(), _conversation(), db(), _FakeCalendar, _FakeRedis, log(), _LogRecorder, _Lookup (+54 more)

### Community 47 - "tools.py"
Cohesion: 0.04
Nodes (102): _blocked_by_toolset_v2(), _blocked_tenant_level(), _busy_without_foreign_details(), _calendar_for_calendar_id(), _calendar_for_professional(), cancel_event(), _canonical_appointment_type(), check_availability() (+94 more)

### Community 48 - "calendar.py"
Cohesion: 0.04
Nodes (120): Row, _agenda_sources(), agenda_viewer(), _AgendaSource, _appointment_read(), _calendar_for_professional(), _calendar_unavailable(), cancel_appointment() (+112 more)

### Community 49 - "setup_world"
Cohesion: 0.13
Nodes (49): patch_status(), MonkeyPatch, r7(), `seed_world` with a future start and a recent inbound by default, acting as its, sent(), setup_world(), staff_rows(), _just_now() (+41 more)

### Community 50 - "resolve_professional_calendar"
Cohesion: 0.14
Nodes (27): CalendarService for professional X under tenant config Y (contract v1 §10)., _professional_credential(), Which refresh token to use for ONE professional's Calendar operations.      TH, Resolve ONE professional's own config into a ready CalendarService.      THE s, resolve_professional_calendar(), db(), _fake_calendar(), _FakeCalendarService (+19 more)

### Community 51 - "test_reminder_schedule.py"
Cohesion: 0.14
Nodes (32): cancel_reminders(), Cancel every not-yet-sent row of the appointment; returns how many.      Rows, Zero the confirmation counter (a moved booking is unconfirmed again)., The appointment moved: retire the old rows, zero the counter, recreate.      C, reschedule_reminders(), reset_confirmation(), _all_rows(), _mark_sent() (+24 more)

### Community 53 - "test_internal_asaas_connection.py"
Cohesion: 0.27
Nodes (14): db(), _override_session(), AsyncClient, MonkeyPatch, Tests for POST /internal/tenants/{tenant_id}/asaas-connection.  Mirrors tests/, _seed_tenant(), test_missing_key_is_unauthorized(), test_reconnect_upserts_same_credentials_row() (+6 more)

### Community 54 - "Appointment"
Cohesion: 0.10
Nodes (44): Appointment, Platform-side record of a clinic appointment.      Created whenever the platfo, apply_asaas_event(), _as_utc(), _asaas_client_for(), _deposit_request_text(), get_deposit_for_appointment(), _insurance_charges_deposit() (+36 more)

### Community 55 - "test_agent_tool_enforcement.py"
Cohesion: 0.09
Nodes (27): _config(), _ExplodingCalendar, _multi_turn(), _names(), The agent's capabilities are enforced by the tool set, not by the prompt.  A m, Stand-in for the compiled LangGraph: records its tools, never calls an LLM., The safe alternative replaces the unsafe one — it does not sit beside it., No entitlement means no booking tool at all — never a broader one. (+19 more)

### Community 56 - "ConsentEvent"
Cohesion: 0.15
Nodes (25): erase_subject(), export_subject(), _hash_wa_id(), AsyncSession, _TENANT_PATH, LGPD privacy endpoints — internal service-to-service (`/internal/privacy/*`)., SHA-256 hex digest of a wa_id — logged instead of the raw value, ever., ConsentEvent (+17 more)

### Community 57 - "BookingHold"
Cohesion: 0.06
Nodes (57): BookingHold, One slot reserved for one conversation, until `expires_at`., appointment_stands_in(), _aware(), BookingGate, GateDecision, held_windows(), HeldSlot (+49 more)

### Community 58 - "test_a_known_account_whose_name_brain_api_sends_reads_the_frame_then_the_menu"
Cohesion: 0.17
Nodes (17): _consent_kinds(), _menu_body(), _NoIdentityWrites, _patient(), MonkeyPatch, The e-mail/code legs. A verified account must never reach any of them., Every name handed to brain-api (`report_name`), and what it answers., The menu bubble as a Brain-Message transcript stores it (flattened card). (+9 more)

### Community 59 - "test_precheck_handoff_plugin.py"
Cohesion: 0.07
Nodes (66): _claimed(), _ctx(), db(), _make_portal_rows(), _make_rows(), _portal_bodies(), Tests for plugins/precheck_handoff.py — "your pre-consult is waiting".  Same i, Is the ledger key for this appointment currently held? (+58 more)

### Community 60 - "ProcessedEvent"
Cohesion: 0.10
Nodes (42): ProcessedEvent, One row per Meta event id already handled.      The unique constraint on `even, iter_audio_messages(), Yield one minimal dict per inbound WhatsApp voice-note audio message.      Mir, db(), _FakeArqPool, MonkeyPatch, Tests for inbound WhatsApp AUDIO transcription.  Covers `iter_audio_messages` (+34 more)

### Community 61 - "test_interactive_bubble_record.py"
Cohesion: 0.13
Nodes (24): _add(), db(), _payload_capturing_client(), AsyncClient, datetime, MonkeyPatch, messages.interactive - an interactive send's structure, recorded beside its hist, The portal draws real controls now (PROMPT_BRAIN_MESSAGE_PORTAL_INTERACTIVE_TAP) (+16 more)

### Community 62 - "test_agent_menu_tools.py"
Cohesion: 0.11
Nodes (53): _agent_context(), db(), Tests for the LLM handoff round (PROMPT 3, multi-doctor flow).  Covers the two, The clinic shape this repo calls "the tenant that broke": the legacy     `tenan, Second lock on the topology.      `_flow_handback_tools` withholds the tool fr, The `_seed` clinic reduced to ONE active professional (a SOLE tenant)., The common case: the clinic does not collect convênio, so the hand-back     lan, The literal ask was "open the day list". Doing that unconditionally     would d (+45 more)

### Community 63 - "conversations.py"
Cohesion: 0.08
Nodes (50): ColumnElement, refusal(), _extract_wam_id(), _get_conversation(), get_message_media(), get_typing(), _last_message_at(), list_conversations() (+42 more)

### Community 64 - "test_flow_router_multiprofessional.py"
Cohesion: 0.09
Nodes (71): _agendar_tap(), _conv_at_service_confirm(), _conversation(), _FakeCalendar, _professionals(), Tests for the deterministic multi-doctor branch of the flow router.  Mirrors t, TASK-035: the drawn "🗓️ Agendar" starts the usual flow ("Essa consulta é pra voc, Matched before the multi-doctor trio dispatch, so it works there too. (+63 more)

### Community 65 - "load_tenant_config"
Cohesion: 0.10
Nodes (52): can_activate_professional_aware(), load_tenant_config(), professional_completeness(), Encrypt and upsert the professional's own Calendar refresh token. Caller commits, Per-active-professional completeness + the partial-activation verdict.      `h, Professional-aware activation gate (contract endpoints 3/5).      Requires `pr, Resolve a tenant row into the fully-decrypted runtime config.      The agent w, set_professional_google_refresh_token() (+44 more)

### Community 66 - "test_ai_tools_cancel_money.py"
Cohesion: 0.21
Nodes (14): _agent_context(), db(), _FakeCalendarService, _fakes(), datetime, MonkeyPatch, Tests for ai/tools.py's Pix-deposit money hook on the LLM cancel path (PROMPT S, Dev-script scaffolding (no tenant context) never crashes and never     issues a (+6 more)

### Community 67 - "test_precheck_handoff_tool.py"
Cohesion: 0.33
Nodes (16): _agent_context(), db(), _fakes(), MonkeyPatch, Tests for ai/tools.py:iniciar_pre_consulta — the precheck hand-off agent tool., Tenant context set but no conversation (or no patient on it) -> can't     resol, _seed_tenant_patient_conversation(), _set_precheck_number() (+8 more)

### Community 68 - "PROMPT 2 — state-driven greetings, buttons, manage sub-flow, Outro handoff"
Cohesion: 0.12
Nodes (15): Agent surface, Buttons by state, CHECKPOINT — Context-aware opening + read-only patient-appointments tool, Core rule, Greeting — HAS_UPCOMING(_SOON) final copy, Manage sub-flow — deterministic actions on the existing appointment, Outro → LLM handoff (appointment-aware agent mode), Pendências (+7 more)

### Community 69 - "test_agent_retry.py"
Cohesion: 0.21
Nodes (15): LogCaptureFixture, _FakeError, _noop_sleep(), MonkeyPatch, Tests for the transient-network retry inside run_agent.  Verifies that a singl, A CalendarUnavailableError from a tool must surface the degradation     sentine, Skip the 1-second backoff so the test suite stays fast., Stand-in for a non-transient runtime error (e.g. a bug in the agent). (+7 more)

### Community 70 - "extract_inbound_body"
Cohesion: 0.11
Nodes (40): extract_greeting_button(), extract_inbound_body(), extract_inbound_reply_id(), _interactive_reply(), The tapped reply button or list row, whichever this message carries., Return the human-readable text of an inbound message.      What the patient ty, Return the raw id of the control an inbound message tapped, or None.      The, Decode a tap on the greeting's id, when it carries the "greeting|" prefix. (+32 more)

### Community 71 - "test_confirm_recheck_slot.py"
Cohesion: 0.09
Nodes (48): _Agenda, _agent_turn(), _appointment(), _at(), _confirming(), _confirming_move(), db(), _event() (+40 more)

### Community 72 - "professional_notification.py"
Cohesion: 0.14
Nodes (27): _as_utc(), _claim(), _deliver(), _ledger_key(), _load_professional(), _local_when(), _patient_line_name(), _post_booking() (+19 more)

### Community 73 - "test_pii_pseudonymization.py"
Cohesion: 0.12
Nodes (29): _busy_via_tool(), db(), _patch_session_factory(), MonkeyPatch, The scrub/re-hydrate step between this repo and OpenAI.  Covers the two direct, The spy stands exactly where the OpenAI request is built., The clinic's own prior replies quote the patient back by name., The reason the map is persisted at all.      Two turns, days apart. If the sec (+21 more)

### Community 74 - "professionals.py"
Cohesion: 0.07
Nodes (45): _count_active(), create_professional(), create_professional_calendar(), create_professional_calendars(), _get_professional(), _list_item(), list_professionals(), AsyncSession (+37 more)

### Community 75 - "attachment_http.py"
Cohesion: 0.07
Nodes (39): ASGIMessage, FormData, M, MultiPartException, Receive, RequestValidationError, Scope, Send (+31 more)

### Community 76 - "cleanup_patients"
Cohesion: 0.17
Nodes (18): execute(), preview(), AsyncSession, UUID, Brain orchestrated cleanup, authenticated with the existing mesh key., CleanupRequest, CleanupResult, Counts-only contract for clinic-scoped patient cleanup. (+10 more)

### Community 77 - "test_build_identity.py"
Cohesion: 0.07
Nodes (49): BuildIdentity, check_deploy_parity(), compare_build(), publish_build_identity(), Any, ParityVerdict, Build identity of the running process — the deploy-parity proof (FIX_01 §5.1/§5., Are these two processes running the same code?      Both `build_sha` and `sour (+41 more)

### Community 78 - "test_appointment_edit_apply.py"
Cohesion: 0.09
Nodes (52): test_alterar_dados_enqueues_a_move_only_when_the_time_changed(), _apply(), _edit(), _edit_world(), Confirming the edit: calendar, the same row, reminders, money (TASK-032 R6, spec, _reply(), _tap(), test_a_cancelled_appointment_is_never_edited() (+44 more)

### Community 79 - "CHECKPOINT — Google Calendar integration modes (per_professional / shared_account)"
Cohesion: 0.07
Nodes (27): 10. "Conectado" que não agenda — token da clínica recusado pelo Google (2026-09-12), 1. Mode field, 2. OAuth scopes, 3. Secondary calendar creation, 4. Routing rule (event create/move/cancel) — the credential × calendar_id edge case, 5. Scope-insufficient 403 → structured reconnect signal, 6. Mode endpoint, 7. Professionals roster payload (+19 more)

### Community 80 - "SecretarIA"
Cohesion: 0.04
Nodes (42): Auxiliary scripts (Fase A scaffolding), Code conventions & project structure, `CORS_ALLOW_ORIGINS` aceita DOIS formatos — e o valor "certo" já derrubou o portal, Deploy — a API e o worker são DOIS serviços (regra obrigatória), Documentação, Folder granularity — group by domain, not "one folder per file", General, graphify (+34 more)

### Community 81 - "Conversation"
Cohesion: 0.02
Nodes (264): TextBubble, Conversation, A patient <-> clinic conversation and its handover state., Open a booking landing past the service card with the details message., _with_details(), _appointment_local_start(), _appt_duration_minutes(), _appt_row_label() (+256 more)

### Community 82 - "test_flow_day_picker.py"
Cohesion: 0.08
Nodes (58): _appt(), _Calendar, _calendar_service(), _control_tap(), _conversation(), _day_tap(), _FakeGoogleEvents, _FakeGoogleService (+50 more)

### Community 84 - "ReminderJob"
Cohesion: 0.20
Nodes (19): _buttons(), _deliver_portal(), _deliver_whatsapp(), DeliveryOutcome, _error_code(), _portal_email(), Exception, Deliver ONE appointment reminder on the patient's own channel (TASK-032 R2). (+11 more)

### Community 85 - "CHECKPOINT — Limites de texto do WhatsApp (constante única + corte marcado + trava no hub)"
Cohesion: 0.07
Nodes (27): §1 — Por que NÃO é corte por fronteira de palavra, §2 — O que entrou onde, §3 — O que NÃO mudou (de propósito), §4 — Segunda rodada (2026-08-22): os outros campos, e a própria LLM, §5 — Terceira rodada (2026-08-30): emoji nos botões e nas listas (FEAT 44), A armadilha maior: o matcher, não o render, A decisão, A LLM agora sabe o número (+19 more)

### Community 86 - "CHECKPOINT — Onboarding & multi-professional configuration"
Cohesion: 0.17
Nodes (11): CHECKPOINT — Onboarding & multi-professional configuration, Crons (`workers/onboarding_cron.py`, registered in `workers/arq_worker.py`), Email (`services/email.py`), Honesty notes, Hub additions, Internal provisioning surface (`api/internal_provisioning.py` + `services/provisioning.py`), Migrations added this round (chain), Per-professional agent runtime (+3 more)

### Community 87 - "build_patient_calendar_link"
Cohesion: 0.10
Nodes (30): build_patient_calendar_link(), _patient_link_stamp(), ZoneInfo, One half of the `dates=` pair, e.g. `20260529T170000Z`., The "adicionar à minha agenda" link to send the PATIENT. Pure, no I/O.      NO, The clinic-local timezone this service interprets naive times in., _params(), Unit tests for `services/calendar.py::build_patient_calendar_link` (pure, no net (+22 more)

### Community 88 - "insurance_catalog.py"
Cohesion: 0.08
Nodes (63): InsuranceCatalog, InsuranceCatalogUnmatched, ProfessionalInsurancePlan, Convênio (health-insurance plan) catalog: global list + per-clinic + per-doctor., One canonical convênio operator, shared by every tenant., A convênio this clinic accepts: a catalog entry, or the clinic's own "Outro"., What ONE doctor accepts. Shape depends on `Tenant.insurance_mode` — see     the, A clinic's legacy convênio text that has never matched a catalog entry.      T (+55 more)

### Community 89 - "test_convenio_catalogo_db.py"
Cohesion: 0.08
Nodes (62): _mirror_legacy(), NotClinicPlans, professional_plan_ids(), A doctor (clinic_with_exceptions) was asked to accept plans the clinic     has, Back-compat alias of `professional_accepted_tenant_plan_ids` (pre-TASK-008 name), Keep `Tenant.insurances` readable by a hub frontend that predates the catalog., Replace the clinic's CATALOG-BACKED plan set with `plans` = [(catalog_id,     c, `clinic_with_exceptions`: replace the clinic-plan subset THIS doctor accepts. (+54 more)

### Community 90 - "run_agent"
Cohesion: 0.10
Nodes (37): _body_digest(), build_agent(), effective_tools(), invoke_agent(), _invoke_agent_with_retry(), _kept_on_v2(), _load_history(), _log_agent_trace() (+29 more)

### Community 91 - "test_booking_owner_persistence.py"
Cohesion: 0.06
Nodes (60): _add_appointment(), _agent_context(), _conversation_snapshot(), db(), _FakeAsaas, _FakeCalendar, _FakeRedis, _fakes() (+52 more)

### Community 92 - "test_hub_calendar_health.py"
Cohesion: 0.13
Nodes (29): GoogleTokenRevokedError, Google refused the stored refresh token itself (`invalid_grant`).      A SUBCL, _connect_clinic(), db(), fake_google(), _FakeCalendar, _FakeCalendarFactory, AsyncClient (+21 more)

### Community 93 - "format_brl"
Cohesion: 0.13
Nodes (20): cancellation_notice(), The honest pt-BR line to append after resolving a cancellation's money     outc, The pt-BR warning the CLINIC reads before freeing a paid slot (not the patient n, release_warning_text(), format_brl(), parse_brl_to_cents(), BRL free-text money parsing/formatting — no currency library, no network.  `ap, Parse free-text BRL into integer cents. Returns None, never raises.      None (+12 more)

### Community 94 - "test_admin_tenants.py"
Cohesion: 0.15
Nodes (27): _FakeSession, _make_config(), _make_tenant(), _override_session(), AsyncClient, MonkeyPatch, Tests for the admin fleet endpoints (GET /admin/tenants + calendar health).  T, Minimal AsyncSession stand-in for the delete path.      `tenant` is what `get` (+19 more)

### Community 95 - "test_subscription.py"
Cohesion: 0.09
Nodes (40): The result of validating a subscription token against brain-api.      tenant_i, SubscriptionClaim, Who is looking at the agenda: brain-api's answer -> the hub viewer (TASK-044 R7,, test_a_professional_of_another_clinic_is_dropped_never_trusted(), test_a_receptionist_sees_the_clinic_without_toggle(), test_an_older_brain_api_restricts_a_doctors_token(), test_no_live_session_for_this_clinic_is_401(), test_the_dependency_reads_brain_apis_answer() (+32 more)

### Community 96 - "test_hub_configuration.py"
Cohesion: 0.09
Nodes (43): db(), other_tenant_professional(), professional(), AsyncClient, Tests for PUT /tenants/me/configuration — the transactional tenant + profession, Re-read from a FRESH session — the only honest way to ask what committed., The PUT response is built by the same readers the GETs use, so a client     can, PUT is idempotent: a double click, or a retry after a timed-out     response, m (+35 more)

### Community 97 - "test_convenio_catalogo_flow.py"
Cohesion: 0.06
Nodes (86): inbound_routing_text(), The text the flow router and the agent read for one inbound message.      Reco, The part of a data-carrying row id after its prefix, or None., _row_payload(), _advance(), _assert_full_list_with_marks(), _conversation(), _doctor() (+78 more)

### Community 98 - "CHECKPOINT — Trio [Agendar][Gerenciar consulta][Outro] + LLM escopada em médico/serviço"
Cohesion: 0.22
Nodes (8): 1. Greeting trio consolidated (workers/tasks.py + services/flow_router.py), 2. Scoped-help LLM nodes ("Não sei" on both catalog lists), 3. Frontend (brain-frontend, separate repo), CHECKPOINT — Trio [Agendar][Gerenciar consulta][Outro] + LLM escopada em médico/serviço, Decisões e pendências, Investigation findings (the brief's "investigar antes"), Testes (34 new / updated across the two repos' concerns), What was built

### Community 99 - "test_brain_message_attachments.py"
Cohesion: 0.14
Nodes (45): api(), db(), _ExplodingWhatsAppClient, _pdf(), _png(), _post_file(), MonkeyPatch, Brain-Message attachments, part 2 of 3 (secretarIA): storage, and both direction (+37 more)

### Community 100 - "is_live_status"
Cohesion: 0.08
Nodes (44): is_live_status(), Appointment model - links a Google Calendar event to a patient + phone.  ## St, True when the booking is still going to happen (see the taxonomy above)., db(), datetime, MonkeyPatch, RESCHEDULED is a LIVE status — one taxonomy for writers and readers.  PROMPT_F, Every member is classified exactly once.      The guard that matters when a st (+36 more)

### Community 101 - "CHECKPOINT — Fixed greeting buttons (product principle: LLM is the last resort)"
Cohesion: 0.14
Nodes (13): 1. The fixed set: payload ids, labels, routes, 1. The `greeting|N` payload: full path, and how it actually diverged from the brief's working assumption, 2. Making it work for `flows_enabled(tenant) == False` too (the actual hard part), 2. `reactivation_choice_buttons(tenant)` — confirmed already deterministic, 3. Legacy `greeting|<número>` handling, 4. Hub config contract (backend is now the source of truth for the frontend agent), 5. `tenant.greeting_buttons` (the DB column), Achados fora de escopo (registrados, não corrigidos) (+5 more)

### Community 102 - "internal.py"
Cohesion: 0.08
Nodes (51): _internal_key_scheme, max_length, min_length, _appointment_dto(), _arq_pool_or_503(), _attachment_gate(), _brain_message_conversation_started(), brain_message_inbound() (+43 more)

### Community 103 - "CHECKPOINT — Multi-doctor WhatsApp workflow (deterministic doctor selection)"
Cohesion: 0.22
Nodes (8): CHECKPOINT — Multi-doctor WhatsApp workflow (deterministic doctor selection), Decisions made in-session (not pinned by the prompt doc), Deterministic flow (`services/flow_router.py`), LLM handoff (`ai/`, `plugins/multi_professional.py`, `workers/tasks.py`), Open follow-ups, Schema (migration `b3d9f2a1c8e7`, revises `f06e85b476f2`), Shared professional resolution (`services/tenant_config.py`), Testing

### Community 104 - "CHECKPOINT — Plugin architecture (entitlement-gated capabilities)"
Cohesion: 0.22
Nodes (8): CHECKPOINT — Plugin architecture (entitlement-gated capabilities), Honesty notes (sandbox vs production), LGPD pieces that landed with this round, Migrations added this round (chain), Testes determinísticos de horário (2026-10-02), The core (tier `basico`) — was already real, now gated, The plugins, The registry (`src/secretaria/plugins/`)

### Community 105 - "cancellation_notice.py"
Cohesion: 0.11
Nodes (23): _as_utc(), build_cancellation_text(), is_inside_window(), join_blocks(), last_inbound_at(), meta_language_code(), datetime, UUID (+15 more)

### Community 106 - "test_hub_staff_message.py"
Cohesion: 0.29
Nodes (20): mail(), _message(), AsyncClient, MonkeyPatch, POST /appointments/{id}/message (TASK-032 R4, spec 4.4)., _recent(), test_a_failing_portal_write_is_a_502_and_records_nothing(), test_a_portal_patient_with_email_is_nudged() (+12 more)

### Community 107 - "Exception"
Cohesion: 0.09
Nodes (26): Exception, AppointmentEditRequested, GuidedBookingRequested, HumanHandoffOfferRequested, HumanHandoffRequested, propose_appointment_edit(), Raised by the `show_main_menu` tool: the patient wants the button menu back., Raised by `request_human_handoff`: the agent asks for a person.      Same exce (+18 more)

### Community 108 - "test_ai_v2_blindness.py"
Cohesion: 0.15
Nodes (24): _appointment(), _at(), _config(), _day(), db(), _decoy_calendar(), _google_event(), _leaks() (+16 more)

### Community 109 - "hub_configuration.py"
Cohesion: 0.09
Nodes (27): ActivationBlocked, _apply_extra_reminder(), apply_professional_config(), apply_tenant_config(), check_appointment_type_service_ids(), check_tenant_activation(), ClinicDescriptionTooLong, config_source_fields() (+19 more)

### Community 110 - "test_debug_reset_menu3.py"
Cohesion: 0.15
Nodes (26): _bm_turn(), _count(), db(), _expected_opening(), MonkeyPatch, UUID, `/menu3` - the test-only reset that puts one patient back at the LGPD notice.  W, The transcript a reset leaves on the Portal: frame, then the LGPD notice. (+18 more)

### Community 111 - "media_storage.py"
Cohesion: 0.09
Nodes (29): AsyncBaseTransport, _bucket(), delete_object(), delete_object_required(), is_configured(), MediaObjectMissing, MediaStorageUnavailable, MediaStream (+21 more)

### Community 112 - "CHECKPOINT — WhatsApp Coexistence onboarding (Tasks 4 + 5)"
Cohesion: 0.22
Nodes (8): CHECKPOINT — WhatsApp Coexistence onboarding (Tasks 4 + 5), Coverage verification (read, not assumed), External dependencies (not part of this round, pending before a live test), Pending, Status, Task 4 — `BOT_ALLOWLIST_WA_IDS` (test-window allowlist), Task 5 — webhook docstring, Tests

### Community 115 - "test_pix_deposit_plugin.py"
Cohesion: 0.10
Nodes (30): agent_tools_for(), enabled_plugins(), The plugin registry: id -> PluginSpec, filtered per tenant by entitlement.  St, Whether `spec`'s hooks/tools run for this tenant.      Two cases:      - `en, PluginSpecs this tenant is currently entitled to.      A disabled plugin (tena, Flatten `agent_tools` across every plugin this tenant is entitled to., Run `post_booking` hooks for every plugin this tenant is entitled to.      Unl, run_post_booking() (+22 more)

### Community 116 - "load_simulation_inputs"
Cohesion: 0.07
Nodes (56): _conversation(), initial_state(), _copy_of(), load_simulation_inputs(), overlay_professional(), overlay_tenant(), Any, AsyncSession (+48 more)

### Community 117 - "test_webhook.py"
Cohesion: 0.39
Nodes (7): AsyncClient, Tests for the WhatsApp webhook endpoints.  These exercise only paths that need, test_get_handshake_returns_challenge(), test_get_handshake_wrong_token_returns_403(), test_health_endpoint(), test_post_invalid_signature_returns_403(), test_post_missing_signature_returns_403()

### Community 118 - "CHECKPOINT — Post-consult tenant config fields"
Cohesion: 0.29
Nodes (6): CHECKPOINT — Post-consult tenant config fields, Pendências, Qualifying turns (`workers/tasks.py`), Testing, The injection seam (`post_consult_knowledge` only), What was built

### Community 119 - "_reminders_v2.py"
Cohesion: 0.10
Nodes (44): add_warnable_row(), datetime, UUID, Rows and clinic setup shared by the TASK-032 R4 warning tests.  Builds the row, set_contact_email(), Seeds and fakes shared by the TASK-032 R2 reminder-engine tests.  The in-memor, World, alert() (+36 more)

### Community 120 - "insurance.py"
Cohesion: 0.07
Nodes (59): create_insurance_catalog_entry(), list_unmatched_insurance_strings(), AsyncSession, Every legacy convênio string still waiting for a matching catalog entry., _bad_ids(), get_insurance_catalog(), get_insurance_mode(), get_professional_insurance_plans() (+51 more)

### Community 121 - "test_booking_code_gate.py"
Cohesion: 0.10
Nodes (57): booking_gate_body(), The gate notice: the owner's sentence, the inbox, the link, the hold.      Pur, test_booking_hold_fallback_notifies_handoff(), _appointments(), _async(), _book(), _conversation_snapshot(), db() (+49 more)

### Community 122 - "CHECKPOINT — Inbound WhatsApp audio transcription"
Cohesion: 0.33
Nodes (5): CHECKPOINT — Inbound WhatsApp audio transcription, Config, Key design points, Pendências, What was built

### Community 123 - "CHECKPOINT — Pix deposit (sinal) add-on + appointment payment lifecycle"
Cohesion: 0.33
Nodes (5): CHECKPOINT — Pix deposit (sinal) add-on + appointment payment lifecycle, External pendências (do NOT fabricate — nothing works end-to-end until these), Flags / known gaps (deliberate, pending product decisions), State machine (`PixDeposit.status`, SEPARATE from `AppointmentStatus`), Where everything lives

### Community 124 - "config.py"
Cohesion: 0.04
Nodes (51): Dev/test: store an INVALID Google Calendar refresh token for a tenant so the ne, main(), Diagnose Fase A auth: what scopes does our refresh token actually carry?  A re, _short(), main(), Generate a Google Calendar refresh token for Fase A (single tenant).  One-shot, Dev shortcut: store the Fase A GOOGLE_REFRESH_TOKEN as the tenant's encrypted C, Create a development tenant from environment settings.  Usage:     uv run pyt (+43 more)

### Community 125 - "test_get_availability_tool.py"
Cohesion: 0.12
Nodes (55): _Agenda, _ask(), _canary_event(), _config(), db(), _doctor(), _hold(), get_availability: free WINDOWS only, never an event (TASK-030 P4, spec §4.6, cri (+47 more)

### Community 126 - "test_menu_command.py"
Cohesion: 0.16
Nodes (28): _count(), db(), MonkeyPatch, Tests for the `/menu` (non-destructive) command family — PROMPT_FIX_18.  `/men, The whole point of the rename: no `/menu`-ish word reaches the wipe., One inbound text message, in the exact shape the worker receives., A tenant + patient + conversation mid-flow, with a booking and a deposit., Row counts for everything `/menu` must never touch. (+20 more)

### Community 127 - "test_flow_cancel_money.py"
Cohesion: 0.21
Nodes (22): db(), _fakes(), _FakeWhatsAppClient, datetime, MonkeyPatch, Tests for the deterministic manage flow's Pix-deposit money hooks (PROMPT S3 se, Entry was pre-checked and allowed it, but a race left the deposit     already a, _reply_ctx() (+14 more)

### Community 128 - "test_bot_allowlist.py"
Cohesion: 0.20
Nodes (21): _count(), db(), _echo_value(), MonkeyPatch, Tests for the Coexistence test-window allowlist (`BOT_ALLOWLIST_WA_IDS`).  Cov, Ordering fix (PROMPT_FIX_21): the allowlist runs BEFORE the     tenant-active g, The reorder must not silence the legitimate case., A redelivery of an already-dropped event hits the pre-existing     idempotency (+13 more)

### Community 131 - "test_handover_echoes.py"
Cohesion: 0.17
Nodes (28): extract_echo_body(), Return the history body for a `smb_message_echoes` (Coexistence) event.      E, db(), _echo_value(), _load_conversation(), _load_message(), _msg(), MonkeyPatch (+20 more)

### Community 132 - "test_flow_router_insurance.py"
Cohesion: 0.16
Nodes (27): _confirm_service(), _conversation(), _FakeCalendar, _professionals(), Tests for the convênio step across every professional topology.  `collect_insu, Tap "Sim" on the service-detail card — the step right before convênio., A clinic-wide setting produces a clinic-wide step, whatever the roster., Toggle off OR an empty plan catalog skips the step — on every topology. (+19 more)

### Community 133 - "payments_tasks.py"
Cohesion: 0.50
Nodes (3): process_asaas_event(), arq job(s) for the Pix deposit (sinal) payment lifecycle.  Kept in its own mod, arq job: apply one Asaas webhook event. Enqueued by POST /webhooks/asaas.

### Community 148 - "Professional"
Cohesion: 0.08
Nodes (71): apply_groups(), Create the catalog rows and stamp `service_id`. Idempotent.      Re-running fi, Professional, A bookable professional (doctor, therapist, ...) belonging to a tenant., professionals_offering(), Overlay the canonical catalog onto per-professional JSON entries.      Returns, Which of `professionals` actually offer `service`. Order preserved.      `serv, resolve_entries() (+63 more)

### Community 149 - "reminder_schedule.py"
Cohesion: 0.13
Nodes (34): _arm(), _as_utc(), _check_staff_kind(), current_staff_notice_row(), ensure_staff_notice_row(), _lock_appointment(), _move_pending_custom(), pending_custom_reminders_need_replan() (+26 more)

### Community 150 - "oauth.py"
Cohesion: 0.11
Nodes (28): RedirectResponse, calendar_disconnect(), calendar_health(), oauth_callback(), oauth_start(), _owned_professional(), _portal_redirect(), professional_calendar_disconnect() (+20 more)

### Community 156 - "arq_worker.py"
Cohesion: 0.14
Nodes (15): on_shutdown(), on_startup(), arq worker entry point.  Start the worker with:     arq secretaria.workers.ar, Run once when the worker process stops., arq worker configuration.      arq reads these as plain class attributes, so `, Job names arq will accept in `enqueue_job`, in registration order.      Logged, Cron job names, in registration order. Same purpose as the above., Run once when the worker process starts. (+7 more)

### Community 184 - "AppointmentReminder"
Cohesion: 0.10
Nodes (48): AppointmentReminder, A reminder of one appointment version (see the module docstring)., _as_utc(), _chat_copy_exists(), claim_reminder(), _close(), due_reminder_ids(), _emit_usage() (+40 more)

### Community 220 - "test_resolve_tenant.py"
Cohesion: 0.15
Nodes (24): _all_tenants(), db(), MonkeyPatch, workers/tasks.py:_resolve_tenant - nullable phone_number_id + autoprovision gate, A NULL-phone tenant (mid-onboarding) sits in the DB - never adopted., Even an event with no phone_number_id at all must not match a NULL row., Regression guard: this exact call used to silently auto-create a tenant., No metadata.phone_number_id on the event at all - still dropped, not adopted. (+16 more)

### Community 222 - "test_ai_v2_conversations.py"
Cohesion: 0.05
Nodes (108): admits_missing_information(), asks_for_a_name(), booking_claims(), capability_promises(), emergency_guidance(), _hits(), invented_address(), invented_price() (+100 more)

### Community 227 - "scoped_help.py"
Cohesion: 0.11
Nodes (28): _get_decision_model(), _normalize(), _professional_options_block(), Any, BaseMessage, UUID, Scoped LLM helper nodes for the deterministic flow's "Não sei" rows.  Two narr, Build (once) the structured-output model both nodes share.      Same knobs as (+20 more)

### Community 240 - "test_ia_atende_como_pessoa.py"
Cohesion: 0.08
Nodes (28): `sentinel`, prefixed by the intro envelope when the agent left words for the pat, (intro, rest) - the inverse of `with_handback_intro`; (None, reply) without one., split_handback_intro(), with_handback_intro(), _offer_pending(), TASK-038 (owner, 2026-10-07): the secretary serves like a person, a human comes, T10/T11: a question is not a failed date - and "ter" in a sentence is not Tuesda, Review of TASK-038: the handover writes the flow fields BEFORE the staff alert r (+20 more)

### Community 241 - "load_tenant_insurance"
Cohesion: 0.18
Nodes (31): load_tenant_insurance(), ONE read per turn of everything the flow needs (see TenantInsurance).      Mod, _book_to_insurance_or_professional(), _count(), db(), _flow_snapshot(), _plan_id(), _professional() (+23 more)

### Community 247 - "test_brain_message_recent_page.py"
Cohesion: 0.23
Nodes (18): db(), datetime, The Portal transcript shows the NEWEST messages, not the oldest 50 (LACUNAS L1)., One outbound bot row per stamp, body `m1..mN`, id `UUID(int=N)`, in the given or, `count` outbound bot rows, body `m0..m{count-1}`, one minute apart, oldest first, _seed(), _seed_stamped(), test_a_tie_group_at_the_very_start_of_the_conversation_ends_the_paging() (+10 more)

### Community 248 - "test_calendar_events_insurance.py"
Cohesion: 0.08
Nodes (72): PixDepositStatus, AppointmentPlan, load_appointment_plans(), _plan_from_row(), One convênio row as the agenda shows it: an identity, a name, a flag.      Nev, Resolve every plan id of a page of appointments in at most TWO queries.      O, AppointmentDepositView, load_deposit_views() (+64 more)

### Community 250 - "CHECKPOINT — Pseudonimização de PII antes do modelo de IA"
Cohesion: 0.12
Nodes (16): 10. Skill: nenhuma criada, e por quê, 1. O que mudou, em uma frase, 2. Onde o código está, 3.1 `check_availability` parou de expor evento alheio (2026-09-07, decisão do usuário), 3. A DECISÃO sobre o escopo das ferramentas do loop ReAct, §4.1.1 — o espaçamento foi corrigido no pacote, NÃO neste repo, 4. Duas divergências deliberadas do prompt, 5. Ordem: o mapa é salvo DEPOIS do invoke, no `finally` (+8 more)

### Community 253 - "render_greeting"
Cohesion: 0.08
Nodes (37): clinic_description_budget(), greeting_preview_template(), The product-defined first-contact greeting frame, and the LGPD consent notice., Render the frame for one tenant. Pure; never raises on missing input.      An, How many characters of description still fit for THIS clinic.      The greetin, The frame for this clinic with `PREVIEW_PLACEHOLDER` in the slot.      Byte-fo, render_greeting(), Tests for services/greeting_template.py — the product greeting frame.  Two thi (+29 more)

### Community 255 - "test_lgpd_consent_gate.py"
Cohesion: 0.17
Nodes (28): _consent_events(), db(), _inbound(), _open(), _patient(), Tests for the LGPD consent gate — the sequenced first conversation.  The shape, The opening up to the LGPD notice: first message, then the name answer.      S, Sending the notice is not consent. Only the tap is. (+20 more)

### Community 256 - "scrub_messages"
Cohesion: 0.21
Nodes (12): Any, BaseMessage, Pseudonymizer, LangChain-facing half of the pseudonymization step.  `services/pii_pseudonymiz, Wrap a tool sequence for the agent. Safe to call once per agent build.      Th, Un-tokenize the bot's reply, and shout if anything is left over.      A token, Mask every message's content, preserving its TYPE.      `model_copy` rather th, One tool, guarded on both sides. Returns the tool itself if unwrappable. (+4 more)

### Community 261 - "make_appointment"
Cohesion: 0.14
Nodes (34): CustomReplan, What `replan_custom_reminders` did (counts only - logged, never shown)., make_appointment(), make_patient(), _make_tenant(), other_tenant(), datetime, Clinic with the feature ON and an extra reminder 5 days before at 09:00 (São Pau (+26 more)

### Community 264 - "test_brain_message_open.py"
Cohesion: 0.09
Nodes (50): _bodies(), _claim_held(), _conversation(), db(), _open(), `POST /internal/brain-message/open` — the automation speaks first.  Origin: TA, One open, decided AND delivered — what the arq job does end to end., 202 `queued`, and the job carries exactly what the worker needs. (+42 more)

### Community 273 - "WebhookValue"
Cohesion: 0.18
Nodes (5): WebhookValue, _FakeWhatsAppClient, Captures what the CALL SITE passes to the logger.      Deliberately upstream o, _RecordingLogger, test_logs_carry_no_phone_number()

### Community 274 - "test_appointment_edit_flow_menu.py"
Cohesion: 0.14
Nodes (31): enter_edit_menu(), The entry from the reminder's "Alterar Dados": a fresh draft + the 5-option menu, original_appointment(), professionals(), Shared fakes for the TASK-032 R6 edit-flow tests (router level, no database)., The original appointment of a supplied draft, including its stable id., slots(), tenant() (+23 more)

### Community 276 - "test_agent_capability_cache.py"
Cohesion: 0.11
Nodes (18): _clean_agent_cache_and_fakes(), _empty_history(), _fake_tool_a(), _fake_tool_b(), _FakeCompiledAgent, MonkeyPatch, Tests for the capability-keyed agent cache in ai/graph.py.  Replaces the old p, v1 and v2 share the model-facing NAME `manage_existing_appointment` (TASK-030 P3 (+10 more)

### Community 284 - "TASK-008 — Modos de aceitação + catálogo extensível + convênio "Outro""
Cohesion: 0.07
Nodes (27): 10.10 Ordem de deploy obrigatória (achado do Reviewer, severidade HIGH — ler antes de autorizar), 10.1 Schema (migração `d4f8a2c6e913`, revises `c9f4e2a7b815`), 10.2 Serviços (`services/insurance_catalog.py`), 10.3 `flow_router.py`, 10.4 `services/payments/deposit_lifecycle.py`, 10.5 Contrato do hub — `api/hub/insurance.py` (substitui parte da seção 4), 10.6 Admin (SaaS-owner) — `api/admin/insurance.py` (NOVO), 10.7 `PUT /tenants/me/config` — `insurances` removido (+19 more)

### Community 287 - "test_list_patient_appointments_tool.py"
Cohesion: 0.25
Nodes (13): _agent_context(), db(), _patch_session_factory(), datetime, MonkeyPatch, Tests for ai/tools.py:list_patient_appointments — the read-only agent tool.  M, Tenant + patient + conversation, with a mix of appointments:      future SCHED, _seed_with_appointments() (+5 more)

### Community 288 - "test_ai_toolset_v2.py"
Cohesion: 0.07
Nodes (44): _blocked(), cancel_event_v2(), _clinic_zone(), create_event_v2(), _own_upcoming(), _parse_start(), date, time (+36 more)

### Community 289 - "test_staff_appointment_edit.py"
Cohesion: 0.21
Nodes (26): A refusal the hub maps to HTTP: `status_code`, machine `code`, Portuguese `messa, StaffEditError, calendar_for(), _edit(), _labels(), _notices(), datetime, The clinic's Editar/Remarcar service (TASK-032 R7, spec 2026-10-09 §1/§3/§4). (+18 more)

### Community 293 - "EntitlementSummary"
Cohesion: 0.06
Nodes (49): EntitlementSummary, _fetch_entitlements(), _fresh_key(), get_entitlements(), is_entitled(), UUID, Entitlement summary reader — the plugin/tier gating seam.  ===================, Whether `summary`'s tenant is entitled to `key`.      `key` is either an addon (+41 more)

### Community 294 - "WhatsAppClient"
Cohesion: 0.05
Nodes (58): EmailOutcome, WHY a transactional send did or did not happen.      The bool `send_transactio, True iff trying the exact same send again could plausibly succeed., portal_conversation_link(), Where the reminder e-mail sends a Portal patient: the clinic's invite link., _clinic_notice_portal(), _clinic_notice_whatsapp(), conversation_id_for() (+50 more)

### Community 296 - "_seed"
Cohesion: 0.18
Nodes (25): db(), _flow_state(), _inbound(), MonkeyPatch, timedelta, Tests for the universal floor on how long a conversation stays in LLM mode.  `, The bug: nothing used to bound this cohort's stay in LLM mode.      The releas, Mid-conversation stickiness is the point of FlowState.LLM — keep it. (+17 more)

### Community 297 - "resolve_opening_message"
Cohesion: 0.07
Nodes (40): Hard cut with no mark, for fields nothing ever matches against.      Row descr, truncate_plain(), _accepts_plan(), pending_identity_ttl_minutes(), _professional_row_title(), _professional_rows(), Silence budget for AWAITING_EMAIL / AWAITING_EMAIL_CODE / AWAITING_NAME, in minu, A `prof|` row's visible title: "🥼 <name>", truncated to absorb it.      Unlike (+32 more)

### Community 298 - "test_human_backup_plugin.py"
Cohesion: 0.16
Nodes (24): InboundContext, What an `on_inbound` hook needs to decide + act on one inbound turn.      Hook, Run `on_inbound` hooks for every plugin this tenant is entitled to.      Stops, run_on_inbound(), _ctx(), db(), _fakes(), _FakeWhatsAppClient (+16 more)

### Community 299 - "test_hub_conversations.py"
Cohesion: 0.08
Nodes (80): _bot(), db(), _FakeWhatsAppClient, _get_conversation_row(), _get_message_rows(), AsyncClient, datetime, MonkeyPatch (+72 more)

### Community 300 - "Parte 1 — `/menu` deixou de ser destrutivo (PROMPT_FIX_18)"
Cohesion: 0.09
Nodes (21): Arquivos, Auditoria, Callers migrados (helper `workers/tasks.py::_tenant_client`), Caminho do `/menu`, CHECKPOINT — `/menu` rename + WABA fail-closed + LGPD logging, Gates do comando destrutivo, Job ARQ mínimo, O que era (+13 more)

### Community 301 - "3. O que entrou onde"
Cohesion: 0.10
Nodes (19): 1. O problema (um sintoma, três causas), 1a. O fluxo determinístico perdia o dono, 1b. A tool base gravava o título do Google como serviço, 1c. O agente multi-profissional ainda tinha as tools erradas na mão, 2. A decisão, 3. O que entrou onde, 4. Observabilidade (tudo sanitizado — só ids e enums), 5. Testes (+11 more)

### Community 303 - "CHECKPOINT — Seletor de dia/horário reaproveitável + entrada determinística"
Cohesion: 0.11
Nodes (18): Apresentação do médico: dobrada, não duplicada, Cartão de menu multi-médico (`flow_router.py::menu_buttons_for`), Cartão de saudação (`workers/tasks.py::_greeting_buttons_for`), CHECKPOINT — Seletor de dia/horário reaproveitável + entrada determinística, Clínica de 1 profissional — nada mudou, Efeito colateral: a agenda certa na entrada da remarcação, Falha de calendário nunca vira LLM, O que existe agora (+10 more)

### Community 304 - "CHECKPOINT — e-mail + OTP inline no Brain-Message"
Cohesion: 0.12
Nodes (15): Arquivos principais, CHECKPOINT — e-mail + OTP inline no Brain-Message, Conta já verificada, Contrato consumido do brain-api, E2E na clínica QA (sessão isolada, `fetch` same-origin, navegador visível), Estados e saídas limitadas, Limpeza e pendências, Não-regressões (§4) — por teste nomeado, 18 passed na revisão deployada (+7 more)

### Community 305 - "test_reminder_v2_hub_wiring.py"
Cohesion: 0.13
Nodes (21): HookSpy, Stands in for services/reminder_hooks.py's three hooks and records the calls., _appointment(), _FakeArqPool, _FakeCalendarService, _override(), _patient(), AsyncClient (+13 more)

### Community 306 - "AgendaViewer"
Cohesion: 0.19
Nodes (31): AgendaViewer, The hub session's agenda visibility.      `professional_id` is a Professional, True = only the appointments of `professional_id` exist for this viewer., A clinic-wide viewer who is also a professional: the "Todos / Só os meus" switch, Choose who is looking at the hub agenda in a test (TASK-044 R7).  tests/confte, view_as(), add_professional(), AsyncClient (+23 more)

### Community 307 - "_persist_brain_message_inbound"
Cohesion: 0.21
Nodes (13): _log_discarded_attachment(), offered_reply_ids(), _persist_brain_message_inbound(), process_brain_message_inbound(), AsyncSession, UUID, inbound - split out of workers/tasks.py (TASK-023)., Report a file whose turn this job dropped before writing its row.      The API (+5 more)

### Community 308 - "analytics_advanced"
Cohesion: 0.16
Nodes (16): analytics_advanced(), analytics_summary(), _as_aware_utc(), AsyncSession, datetime, Doctor hub — analytics endpoints.  - GET /tenants/me/analytics/summary  (basic, Normalize a stored `created_at` to an aware UTC datetime.      `AnalyticsEvent, The last `months` "YYYY-MM" keys ending at `now`, oldest first (dense). (+8 more)

### Community 309 - "test_emoji_decoration.py"
Cohesion: 0.15
Nodes (9): FEAT 44 - which surfaces wear an emoji, and that decorating broke no matcher., _services(), _tenant(), test_a_service_name_that_is_already_truncated_stays_bare(), test_a_tap_on_a_decorated_service_row_still_resolves_to_the_catalog_entry(), test_both_service_catalogs_render_a_name_identically(), test_default_reactivation_buttons_are_decorated_but_a_tenants_own_are_not(), test_tapping_a_decorated_service_row_advances_the_flow() (+1 more)

### Community 310 - "test_blind_staging_tools.py"
Cohesion: 0.09
Nodes (42): manage_existing_appointment_v2(), ManageAppointmentRequested, Raised by `manage_existing_appointment`: the patient wants to reschedule     or, Leva o paciente ao fluxo de remarcar ou cancelar uma consulta JÁ MARCADA dele. O, _config(), db(), _ExplodingCalendar, _Log (+34 more)

### Community 311 - "request_precheck_handoff"
Cohesion: 0.18
Nodes (15): _context_field(), _log_outcome(), _phone_hash(), UUID, Precheck hand-off — asks brain-api (the mesh hub) to pre-seed a PreCheck conver, Short, non-reversible fingerprint of a phone number, safe to log., One optional context field, normalized: blank and absent are the same thing., How a log line names the patient on whichever channel this call is for.      O (+7 more)

### Community 312 - "CHECKPOINT — Anexos no Portal (Brain-Message), parte 2: secretarIA (armazenamento, envio e recebimento)"
Cohesion: 0.10
Nodes (20): 1. Pré-requisito e causa reconfirmada, 2. O que entrou (âncoras estáveis), 3.1 `POST /internal/brain-message/inbound` — variante multipart (brain-api → secretarIA), 3.2 Registro gravado (`messages.attachment`), 3.3 Listagens, 3.4 Mídia (bytes em stream, nunca URL), 3.5 Staff envia arquivo, 3.6 Recusas (corpo `{"detail": {"code", "message"}}`, + `max_bytes` no 413) (+12 more)

### Community 313 - "test_admin.py"
Cohesion: 0.23
Nodes (15): _fake_wipe(), AsyncClient, MonkeyPatch, Tests for the admin reset endpoint and the X-Admin-Token guard., Replace the destructive coroutine with a recorder.      Tests must never reach, test_current_token_still_accepted_when_previous_is_configured(), test_default_wipe_keeps_tenants(), test_endpoint_is_in_openapi_schema() (+7 more)

### Community 314 - "BookingDraft"
Cohesion: 0.09
Nodes (66): BookingDraft, What the AI says the patient asked for. Every field optional; None = not said., Regression: the current (flag-OFF) handback must carry day and who, too., test_ambiguous_doctor_correction_never_reuses_the_previous_doctor(), test_doctor_removed_before_fresh_resolution_is_not_replaced_implicitly(), test_full_explicit_request_reaches_confirmation_even_with_rollout_off(), test_known_self_and_day_ask_only_the_time_with_rollout_off(), test_missing_service_preserves_day_on_flag_off_question() (+58 more)

### Community 315 - "CHECKPOINT — QA ao vivo do canal Brain-Message em produção (2026-09-09)"
Cohesion: 0.18
Nodes (10): 1. `patient_access_otp` ausente em `_TEMPLATES` — commit `f07ec05`, 2. Staff não conseguia responder no canal Brain-Message — commit `5b8bfdf`, 3. O que ainda NÃO foi provado ponta a ponta, 4. Deploy — os DOIS serviços, 5. Verificado ao vivo, sem mudança necessária, 6. Arquivos tocados, 7. Verificação ao vivo PÓS-DEPLOY (2026-09-09, ~15:20-15:31 UTC), 8. PreCheck pelo portal do paciente (fora deste repo) — GRANT aplicado, reteste ao vivo pendente (+2 more)

### Community 316 - "CHECKPOINT — PreCheck oferecido automaticamente logo depois do agendamento"
Cohesion: 0.13
Nodes (14): A ordem de deploy é a parte perigosa, CHECKPOINT — PreCheck oferecido automaticamente logo depois do agendamento, Deploy desta perna, `entitlement_keys=()` é deliberado, Fatia P1 (contexto do agendamento) — IMPLEMENTADA (FEAT 39, 2026-08-26), Idempotência — e por que não há job de retry, LGPD, O hook (+6 more)

### Community 317 - "secretary_system_prompt_v2"
Cohesion: 0.11
Nodes (45): _format_safety_rules(), Render the unconditional "REGRAS INEGOCIÁVEIS..." safety/tone block.      Hard, Render the v2 system prompt for one turn of `config`'s clinic.      `tool_name, secretary_system_prompt_v2(), Direct unit test of the pure helper: every non-negotiable rule from     the cor, test_format_safety_rules_contains_all_required_elements(), _config(), _fixed_clinic_day() (+37 more)

### Community 318 - "CHECKPOINT — Catálogo canônico de serviços da clínica (FEAT 35)"
Cohesion: 0.11
Nodes (18): 1. O problema, 2. A decisão de modelagem, 3. O que entrou (entrega 1), 4. Contrato para a UI (entrega 2 — frontends), 5. Testes, 6. Pendências, 7.1 O bug que travava tudo: `service_id` era descartado em silêncio, 7.2 Um `service_id` que não é da clínica agora é recusado (+10 more)

### Community 319 - "test_calendar_edit_methods.py"
Cohesion: 0.11
Nodes (24): _Client, _Events, _Executable, _free(), datetime, CalendarService.update_event_details / is_slot_free (TASK-032 R6, spec §5.6)., _service(), test_a_busy_event_blocks_unless_it_is_the_one_being_edited() (+16 more)

### Community 320 - "draft_for"
Cohesion: 0.17
Nodes (42): appointment(), context(), conversation(), draft_for(), EditCalendar, The router suite's fake agenda plus the two methods the edit flow needs., ai(), carry() (+34 more)

### Community 321 - "CHECKPOINT — Estado da mensagem (enviado / entregue / lido / falhou), parte 1: secretarIA"
Cohesion: 0.17
Nodes (11): 1. Causa (reconfirmada) — três pontos de descarte, não dois, 2. O que mudou, 3. Decisões (as do prompt, mais as tomadas aqui), 4.1 Campos novos (aditivos, com default) em cada mensagem, 4.2 Poll — **MUDANÇA DE SEMÂNTICA DE `since`**, 4.3 Rotas de leitura, 4. Contrato para as partes 2 (brain-api) e 3 (frontend), 5. Deploy (a migração vai PRIMEIRO) (+3 more)

### Community 322 - "test_hub_config_pix.py"
Cohesion: 0.25
Nodes (13): db(), _override(), AsyncClient, Tests for the six Pix-deposit policy fields on GET/PUT /tenants/me/config (api/, tenant(), test_asaas_connected_flips_true_after_key_set(), test_get_config_exposes_six_fields_with_defaults(), test_put_accepts_boundary_values() (+5 more)

### Community 323 - "get_current_tenant"
Cohesion: 0.09
Nodes (35): get_current_tenant(), Authenticate the request and return the caller's Tenant row.      401 for a mi, get_session(), AsyncSession, FastAPI dependency that yields a database session., _override(), _override(), MonkeyPatch (+27 more)

### Community 324 - "CHECKPOINT — `RESCHEDULED` é status VIVO (taxonomia unificada)"
Cohesion: 0.22
Nodes (8): A constante compartilhada, A decisão, Arquivos, CHECKPOINT — `RESCHEDULED` é status VIVO (taxonomia unificada), O bug, Observabilidade, Pendências, Sem migração, sem backfill

### Community 325 - "test_handback_events.py"
Cohesion: 0.08
Nodes (70): _captured_bubbles(), db(), _events(), log(), _LogRecorder, _patch_session_factory(), MonkeyPatch, Every AI hand-back writes exactly ONE `conversation_handback_entered` event. (+62 more)

### Community 326 - "_resolve_unit_or_error"
Cohesion: 0.31
Nodes (9): _active_units(), list_units(), UUID, multi_unit plugin: unit (physical location) awareness for booking.  Entitlemen, Active units for `tenant_id`, ordered by name. Never raises., Resolve an active unit by name for `tenant_id`.      Returns (unit, None) on s, Lista as unidades/locais ativos da clínica, com nome e endereço. Use     para i, _resolve_unit_or_error() (+1 more)

### Community 327 - "CHECKPOINT — Fluxos determinísticos incondicionais (fim do gate `initial_flows.enabled`)"
Cohesion: 0.29
Nodes (6): A decisão, CHECKPOINT — Fluxos determinísticos incondicionais (fim do gate `initial_flows.enabled`), Código morto que ESTE round deliberadamente não removeu, O problema, O que mudou, Pendências

### Community 328 - "_FakeWhatsApp"
Cohesion: 0.22
Nodes (5): _FakeWhatsApp, metered(), MonkeyPatch, Records sends. `for_tenant` hands back the instance the test holds., wa()

### Community 329 - "staff_appointment_edit.py"
Cohesion: 0.09
Nodes (44): CalendarFor, calendar_professional_id(), CalendarOriginUnresolved, Physical event origin, independent of the doctor who may see an appointment., None means the clinic calendar; NULL source preserves legacy doctor inference., Explicit provenance cannot identify a calendar; never guess the clinic., build_event_description(), The event's description: what a doctor needs to recognise the booking.      Th (+36 more)

### Community 330 - "PluginSpec"
Cohesion: 0.15
Nodes (24): PluginSpec, One optional capability, gated by ANY of a set of entitlement keys.      id: t, Add (or replace) a plugin in the registry, keyed by `spec.id`., register(), _clean_registry(), _fake_ehr_tool(), _fake_pix_tool(), Tests for plugins/base.py + plugins/registry.py — the plugin foundation. (+16 more)

### Community 332 - "attachments.py"
Cohesion: 0.12
Nodes (22): attachment_body(), attachment_placeholder(), AttachmentKind, AttachmentRefused, check_attachment(), _clean(), content_disposition(), extension_conflicts() (+14 more)

### Community 333 - "get_conversation"
Cohesion: 0.05
Nodes (84): process_brain_message_enter(), arq job: a known patient entered a conversation that already has history., Patch a worker-level name in every module the split put it in (TASK-023).  Bef, consent(), get_conversation(), _patient_ref(), datetime, Wiring shared by the TASK-032 R3 tests: patches, a whole-turn driver, ageing. (+76 more)

### Community 334 - "test_waba_fail_closed.py"
Cohesion: 0.06
Nodes (45): EventDict, RuntimeError, Blank secret-bearing keys AND personal-data/content keys before rendering., redact_secrets(), Build a client for a tenant. The DECRYPTED token is injected by the caller., The single-tenant `META_*` env scaffold, requested BY NAME.          Developme, A tenant-scoped send was attempted without that tenant's credentials.      Car, TenantWhatsAppCredentialMissing (+37 more)

### Community 335 - "test_arq_log_secret_redaction.py"
Cohesion: 0.21
Nodes (13): _emit_arq_job_start_line(), arq's NATIVE job log must not carry the OTP, the reset link or the e-mail.  `a, arq truncates the args string at 80 chars — a long address pushes the OTP     a, repr(bytes) is `b'…'` — the prefix must not stop the quoted value from matching., Emit EXACTLY the line arq's `Worker.run_job` emits, with arq's own     logger a, Drift guard: if a future arq changes how it logs job args, fail here so     the, _run_real_job_capturing(), test_a_value_cut_by_arqs_80_char_truncation_is_still_redacted() (+5 more)

### Community 336 - "test_r7_calendar_origin.py"
Cohesion: 0.22
Nodes (15): A start in the future, on a whole minute (the agenda never has seconds)., soon(), _created(), _legacy_hub_row(), Real event ownership regressions for hub-created doctor-owned appointments.  O, A doctor's hub row as written before 2026-10-19: clinic agenda, "clinic" origin., test_hub_creation_without_a_doctor_stays_on_the_clinic_agenda(), test_hub_doctor_creation_edit_and_release_use_the_doctors_own_event() (+7 more)

### Community 337 - "test_patient_channel_identity.py"
Cohesion: 0.18
Nodes (20): db(), _inbound(), MonkeyPatch, Patient identity by channel - the additive half.  This pins the property that, The untouched call site produces a complete channel-addressable row., Identity by wa_id still resolves to ONE row, not a new one per message.      T, Tenant isolation survives the new key: the same phone, two clinics., A patient with no phone number gets a row - the case that was impossible. (+12 more)

### Community 338 - "test_post_booking_plugin.py"
Cohesion: 0.17
Nodes (18): _ctx(), db(), _empty_registry(), _fake_get_waba_token(), _fakes(), _make_appointment(), MonkeyPatch, Tests for the post_booking seam: plugins/base.py's PostBookingContext, registry (+10 more)

### Community 339 - "test_reminder_v2_text.py"
Cohesion: 0.12
Nodes (22): _content(), _interactive(), _quick_reply(), services/reminder_text.py — the words of the reminder (TASK-032 R2, spec §4.2)., test_a_booking_for_someone_else_names_the_attendee(), test_a_naive_start_is_read_as_utc(), test_an_unknown_timezone_falls_back_to_sao_paulo(), test_another_clinic_timezone_is_honoured() (+14 more)

### Community 341 - "test_professional_null_vs_empty.py"
Cohesion: 0.06
Nodes (69): _filter_active_hours(), _filter_active_types(), professional_appointment_types(), professional_business_hours(), professional_inherits_appointment_types(), professional_inherits_business_hours(), Shared filter behind `active_appointment_types` / `professional_appointment_type, Shared filter behind `active_business_hours` / `professional_business_hours`. (+61 more)

### Community 342 - "env.py"
Cohesion: 0.29
Nodes (7): Connection, _do_run_migrations(), Alembic environment - async (asyncpg) configuration., Run migrations in 'offline' mode (emit SQL, no live DB connection)., Run migrations in 'online' mode using an async engine., run_migrations_offline(), run_migrations_online()

### Community 343 - "Parte 2 — `start_guided_booking`: a LLM devolvendo o agendamento aos botões"
Cohesion: 0.12
Nodes (15): A correção — `services/calendar.py::build_patient_calendar_link`, CHECKPOINT — Link de agenda pro paciente + gatilho opcional da LLM pro fluxo guiado, Decisão 1 — ele NÃO pula direto pro seletor de dias, Decisão 2 — a tool é negada a tenant multi-profissional, Decisão 3 — `enter_guided_booking` é entrada PÚBLICA no `flow_router`, e não muta nada, Decisão 4 — o handler relê tudo, e a agenda certa vem da máquina que já existia, Mecanismo — o quarto uso do MESMO padrão, não um quinto padrão, O defeito (+7 more)

### Community 344 - "_rows"
Cohesion: 0.19
Nodes (17): datetime, The doctor already gets "Consulta alterada" from the edit outbox for this move., _rows(), _run(), _run_move(), test_a_cancellation_mails_the_clinic_the_doctor_and_the_portal_patient(), test_a_cancellation_of_a_row_that_is_live_again_is_not_announced(), test_a_doctor_lookup_outage_is_retried_and_the_budget_ends_it() (+9 more)

### Community 345 - "Jornada do paciente — WhatsApp e Portal, em linguagem leiga"
Cohesion: 0.09
Nodes (22): 1. Primeiro contato, 2. Escolher profissional, serviço, data e horário (mesma lógica nos dois canais), 3. Se a pessoa escreve de novo antes da consulta (no ar), 4. Lembretes automáticos, 5. Depois da consulta, 6. Quando a pessoa volta depois de já ter sido atendida (no ar), 7. Remarcar ou cancelar, a qualquer momento (no ar), 8. Calendário dinâmico (não existe ainda em canal nenhum) (+14 more)

### Community 346 - "TASK-030 P2a — testes após deploy"
Cohesion: 0.20
Nodes (9): Correção da primeira medição, Finding F1 — indisponibilidade da agenda, Pendências registradas na primeira execução (histórico), Primeira execução — método e limites, Primeira execução — verificações reais (histórico), Resultado, Reteste após correção informada pelo dono — 2026-10-06, Reteste concluído — 2026-10-06, 11:48–12:01 BRT (+1 more)

### Community 347 - "CHECKPOINT — Moldura de saudação + aviso de LGPD (2026-08-31)"
Cohesion: 0.14
Nodes (13): A quarta armadilha: decorar o rótulo do botão, A regressão do ensaio (`/dangerously-remove-context`) — 2026-09-01, A sequência exata, As três armadilhas que custaram tempo (não repita), CHECKPOINT — Moldura de saudação + aviso de LGPD (2026-08-31), `greeting_message` foi REMOVIDA (migração `e3b7c1d5a9f2`), O atributo que guarda o aceite, O gate bloqueia — mas é um FATO, não um `FlowState` (+5 more)

### Community 348 - "CHECKPOINT — FIX 32: a chave de idempotência devolvida para um retry inexistente"
Cohesion: 0.18
Nodes (10): 1. `services/email.py` — desligado não é falha, 2. `workers/tasks.py::send_cancellation_notice` — o retry passou a existir, 3. `plugins/professional_notification.py` — o reenvio virou um job, A premissa do prompt estava errada, e isso mudou a solução, CHECKPOINT — FIX 32: a chave de idempotência devolvida para um retry inexistente, Invariantes preservadas, O defeito, O que mudou (+2 more)

### Community 349 - "CHECKPOINT — E-mail ao profissional quando uma consulta é marcada"
Cohesion: 0.18
Nodes (10): A decisão: perguntar, não copiar, CHECKPOINT — E-mail ao profissional quando uma consulta é marcada, Contenção, `FIX_02` — pré-requisito já satisfeito, Idempotência — e por que não houve migração, O bloqueio real, e por que não era o que parecia, O hook, O link da agenda — correção de premissa (+2 more)

### Community 350 - "test_tasks_helpers.py"
Cohesion: 0.05
Nodes (52): An active appointment type as the LLM should see it., What the CLINIC says a service needs from the patient (jejum, exames, preparo)., RuntimeAppointmentType, RuntimeServiceGuide, _appointment_context_text(), _manage_owner_calendar_target(), UUID, Whether THIS turn's system prompt should carry the appointment context block. (+44 more)

### Community 351 - "CHECKPOINT — alerta de profissional com configuração incompleta (FEAT 41 + FIX 34)"
Cohesion: 0.17
Nodes (11): 1. O problema: duas falhas que pareciam a mesma, 2. A separação, 3. Debounce — por que uma chave nova, 4. O endereço do médico — o brain-api é dono (corrigido pelo FIX 34), 5. Frontend (`secretarIA-frontend`) — nada a fazer, 6. Para o FEAT 42 (banner) — o sinal JÁ existe, sem endpoint novo, 7. Testes, 8. Efeito colateral encontrado durante a implementação (importante) (+3 more)

### Community 352 - "test_professional_calendar_wire.py"
Cohesion: 0.16
Nodes (24): professional_calendar_source(), WHOSE Calendar credential covers this professional: the professional's own,, api_tenant(), db(), _make(), AsyncClient, The professional Calendar STATUS contract — and what must never ride on it.  W, The state that made the old UI lie: available, but not the doctor's own. (+16 more)

### Community 353 - "Achados e delimitação da causa"
Cohesion: 0.20
Nodes (9): Achados e delimitação da causa, Casos exercitados, Evidências, segurança e limites, L1 — data/hora ficam na conversa, mas não são transferidas, L2 — pergunta livre no seletor não retorna à LLM, L3 — cartão de gerenciamento exibe UTC como hora local, L4 — respostas de FAQ não concluídas, Resultado (+1 more)

### Community 354 - "CHECKPOINT — CORS em formato JSON derrubou todo o portal do médico"
Cohesion: 0.20
Nodes (9): As 4 pistas do prompt — todas descartadas, Causa raiz, CHECKPOINT — CORS em formato JSON derrubou todo o portal do médico, Correção, Lição, Por que derrubou a tela inteira, e não só os profissionais, Por que ninguém viu, Prova ao vivo (2026-09-12, conta real) (+1 more)

### Community 355 - "resolve_booking_day"
Cohesion: 0.17
Nodes (19): clinic_today(), date_context(), date, datetime, time, Resolve date expressions against the clinic's clock, never the server's day., True when `day` is the clinic's today and `at` (a time or "HH:MM") is already pa, Canonical date or ValueError for an invalid/ambiguous expression.      A date (+11 more)

### Community 356 - "test_handoff_notification.py"
Cohesion: 0.11
Nodes (25): is_known_template(), Whether `template` is an id this module can actually render.      Exists so th, test_templates_registered(), The guard `api/internal_provisioning.py` uses to refuse an unrenderable id, test_is_known_template_answers_for_the_enqueue_side(), E-mail de passagem para humano: destinatários, dedupe, retentativa (MVP Portal,, Live 2026-10-05: the mail read "Clínica: <uuid> Conversa: <uuid>"., _render() (+17 more)

### Community 357 - "normalize"
Cohesion: 0.08
Nodes (52): create_service(), _duplicate_error(), list_services(), _offering(), AsyncSession, HTTPException, Doctor hub — the clinic's CANONICAL service catalog.  GET   /tenants/me/servic, The clinic's whole catalog, retired services included.      Retired rows are r (+44 more)

### Community 358 - "reload_appointment"
Cohesion: 0.15
Nodes (39): _format_appointment_when(), datetime, Render an appointment start for greeting copy, in the tenant's timezone., reload_appointment(), _future(), _new_booking(), datetime, cancel_replaced_appointment: guards, money hook, transition (TASK-032 R3, spec § (+31 more)

### Community 359 - "appointment_change_notifications.py"
Cohesion: 0.08
Nodes (44): _change_ref(), _claim(), enqueue_appointment_change_notification(), _iso(), _ledger_key(), _notify(), _notify_all(), notify_appointment_change() (+36 more)

### Community 360 - "test_appointment_edit_flow_fields.py"
Cohesion: 0.23
Nodes (21): _doctor_tap(), _draft_of(), The edit flow: service, doctor, convênio and patient (TASK-032 R6, spec §5.4)., _rows(), _step(), test_a_busy_doctor_continues_to_day_and_time(), test_a_doctor_who_does_not_offer_the_service_lists_their_services(), test_a_free_doctor_goes_to_the_confirmation() (+13 more)

### Community 361 - "CHECKPOINT — botões e listas como dado: `messages.interactive` (2026-09-11)"
Cohesion: 0.22
Nodes (8): §1 — Causa raiz (confirmada no código), §2 — O que mudou, §3 — Decisões tomadas sem perguntar, §4 — Testes, §5 — Prova ao vivo (local, não produção), §6 — Deploy (NÃO FEITO) — ordem obrigatória, §7 — Pendências, CHECKPOINT — botões e listas como dado: `messages.interactive` (2026-09-11)

### Community 362 - "test_cancel_event_ownership.py"
Cohesion: 0.11
Nodes (26): _Calendar, db(), _Log, _patient_with_event(), MonkeyPatch, The legacy `cancel_event` cancels only THIS patient's own event (TASK-030 P4, Ta, Tenant A: our patient (evt-own) and another patient (evt-other-patient).     Te, The check is per conversation, not a blanket refusal. (+18 more)

### Community 365 - "test_onboarding_cadence.py"
Cohesion: 0.14
Nodes (27): config_reminder_due(), Whether a config reminder is due at `now`.      Never sent yet (`last_sent is, _at(), Exhaustive tests for workers/onboarding_cadence.py — the pure cadence/ due-date, D+58's next biweekly point would be D+72, which exceeds the 60-day cap., A cap that lands exactly on a checkpoint is inclusive (<=), but the     default, Config reminders keep recurring indefinitely - no D+60-style cutoff., The checkpoint that just fired must never be returned as 'next'. (+19 more)

### Community 366 - "_should_inject_post_consult_knowledge"
Cohesion: 0.14
Nodes (19): PatientOpeningState, The five opening states, in first-match order (see the resolver)., Whether THIS turn's system prompt should carry post_consult_knowledge.      Pu, _should_inject_post_consult_knowledge(), str, Unit tests for workers/tasks.py::_should_inject_post_consult_knowledge - the pu, Nothing to inject regardless of how "qualifying" the turn otherwise is., Conversation already in full-LLM ("Outro"/deviated) mode. (+11 more)

### Community 368 - "as_utc"
Cohesion: 0.13
Nodes (25): list_patient_appointments(), Lista as consultas FUTURAS já marcadas DESTE paciente nesta clínica.     Use pa, cancel_replaced_appointment(), AsyncSession, datetime, UUID, Cancel the appointment a "Marcar outra consulta" booking replaces (TASK-032 R3)., Cancel the replaced appointment inside the caller's transaction.      Returns (+17 more)

### Community 369 - "BrainMessageSender"
Cohesion: 0.05
Nodes (39): Protocol, attachment_record(), CheckedAttachment, An upload that passed `check_attachment`: its REAL kind, a safe name, its real s, The JSON blob `messages.attachment` stores for an upload that passed the checks., BrainMessageSender, ChannelSender, interactive_history_body() (+31 more)

### Community 370 - "CHECKPOINT — toque real no Portal do paciente (canal Brain-Message) (2026-09-12)"
Cohesion: 0.22
Nodes (8): §1 — O que mudou, §2 — Validação do id (entrada não confiável), §3 — Decisões tomadas sem perguntar, §4 — Testes, §5 — Deploy (NÃO FEITO) — ordem obrigatória entre repos, §6 — Pendências, §7 — Prova ao vivo LOCAL (2026-09-12, depois do §6), CHECKPOINT — toque real no Portal do paciente (canal Brain-Message) (2026-09-12)

### Community 371 - "_ctx"
Cohesion: 0.19
Nodes (11): IClinicProvider, iClinic EHR provider — STUB.  TODO: real iClinic API integration (OAuth/API-ke, STUB `EhrProvider` for iClinic — logs and returns a fake record id., _ctx(), Tests for plugins/ehr.py (provider selection) and services/ehr/iclinic.py (the s, plugins/ehr.py itself does not try/except — registry.run_post_booking does., test_configured_provider_is_invoked(), test_iclinic_stub_returns_deterministic_fake_id_and_logs() (+3 more)

### Community 372 - "Enum"
Cohesion: 0.15
Nodes (20): Enum, activate_tenant(), ConfigStatus, connect_asaas(), connect_whatsapp(), ConnectOutcome, create_or_attach_professional(), get_config_status() (+12 more)

### Community 373 - "_FakeWhatsAppClient"
Cohesion: 0.20
Nodes (4): _fake_get_waba_token(), _fakes(), _FakeWhatsAppClient, MonkeyPatch

### Community 374 - "test_main_menu_manage_entry.py"
Cohesion: 0.13
Nodes (26): is_main_menu(), main_menu_buttons(), The buttons RENDERED under the main menu: [🗓️ Agendar, Outro], for every clinic., True for a card that IS the main menu (`main_menu_buttons()` as drawn).      M, db(), MonkeyPatch, The main menu offers "Gerenciar consulta" to a patient with something booked., The router's own menu (`_menu_bubbles`) is what the seam personalises. (+18 more)

### Community 375 - "EditDraft"
Cohesion: 0.08
Nodes (19): FakeCalendar, build_edit_recap(), EditDraft, _local_naive(), datetime, tzinfo, The pure pieces of the "Alterar Dados" flow (TASK-032 R6).  Spec: docs/superpo, `value` in the clinic zone as a naive ISO minute ("2026-10-16T15:20"). (+11 more)

### Community 376 - "fetch_patient_email_result"
Cohesion: 0.33
Nodes (8): fetch_patient_email(), fetch_patient_email_result(), PatientEmailResult, UUID, Read a Portal patient's booking contact from the identity authority.  The call, Availability refers to a valid authority answer, including email=null., Compatibility interface; callers needing authority use the result variant., Fetch one clinic-scoped contact; external_id is the Portal UUID string.

### Community 378 - "test_extra_reminder.py"
Cohesion: 0.10
Nodes (32): clinic_zone(), custom_due_at(), from_legacy_lead(), is_valid_send_time(), legacy_lead_minutes(), parse_send_time(), datetime, time (+24 more)

### Community 379 - "Identidade de paciente por canal — migração aditiva"
Cohesion: 0.15
Nodes (12): Assumem não-nulo e QUEBRAM com paciente sem telefone, Corrigidos, Decisão 1 — a constraint antiga foi MANTIDA, não substituída, Decisão 2 — `external_id` é NULLABLE, embora `NOT NULL` fosse o invariante melhor, Decisão 3 — o preenchimento automático é default de coluna, não mudança no call site, Identidade de paciente por canal — migração aditiva, Inventário de `.wa_id` — para o próximo prompt (pipeline), Já toleram `None` (nada a fazer) (+4 more)

### Community 380 - "test_whatsapp_limits.py"
Cohesion: 0.10
Nodes (36): decorate_and_truncate(), decorate_if_fits(), decorated_text_budget(), WhatsApp Cloud API display limits, and the ONE way we cut text down to them., Cut a reply-button title to `limit`, UNMARKED - see why below.      Buttons de, Prefix `text` only when the WHOLE of it still fits inside `limit`.      The co, How many characters of TEXT still fit beside `emoji` inside `limit`.      The, Always prefix; `text` absorbs the cost by being cut that much harder.      The (+28 more)

### Community 381 - "test_brain_message_pipeline.py"
Cohesion: 0.10
Nodes (36): _bm_turn(), _call(), db(), MonkeyPatch, The second channel: one pipeline, two ways in.  What these tests are actually, The same first contact decides the same thing on either channel.      Every fi, Not just the greeting: a mid-conversation `/menu` decides identically., Row-for-row, column-for-column, after the extraction.      The patient keyed o (+28 more)

### Community 383 - "CHECKPOINT — mensagens do console de staff (hub/conversations)"
Cohesion: 0.17
Nodes (11): 1. O que entrou, 2. Ordem de operações do POST — send-then-persist, 3. Decisões tomadas sem perguntar ao usuário (pedido explícito dele — argumento de cada uma), 4. O que NÃO mudou (fora de escopo, conforme o prompt), 5. `PIPELINE_CANAL` já tinha rodado quando cheguei aqui?, 6. Testes, 7. Arquivos tocados, 8.1 Interação com `PROMPT_BRAIN_MESSAGE_SECRETARIA_IDENTIDADE_PACIENTE.md` (rodou em paralelo) (+3 more)

### Community 384 - "_WorkersNamespace"
Cohesion: 0.32
Nodes (4): ModuleType, Fan-out proxy. Per-module originals are remembered, because the value pytest's, _split_modules(), _WorkersNamespace

### Community 386 - "minimal_event_payload"
Cohesion: 0.15
Nodes (26): minimal_event_payload(), _minimal_message(), _minimal_status(), One inbound message / echo reduced to the fields the worker reads.      `keep_, One delivery receipt reduced to what `apply_whatsapp_statuses` reads.      Kep, Reduce a raw Meta webhook body to ONLY what the arq worker reads.      The job, _flat(), _messages_payload() (+18 more)

### Community 387 - "PostBookingContext"
Cohesion: 0.13
Nodes (19): PostBookingContext, PluginSpec: the shape of one optional, entitlement-gated capability., What a `post_booking` hook needs to react to one freshly booked appointment., _post_booking(), ehr plugin: push a freshly booked appointment to the tenant's EHR (post_booking), _post_booking(), pix_deposit plugin: ask for a Pix deposit right after a booking (post_booking)., _claim() (+11 more)

### Community 388 - "delete_tenant"
Cohesion: 0.24
Nodes (11): _count(), delete_tenant(), DeleteTenantOutcome, DeleteTenantResult, AsyncSession, UUID, Destructive admin operations shared by the CLI script and the API.  Single sou, Result of `delete_tenant` — the API layer maps this to a status code. (+3 more)

### Community 389 - "CHECKPOINT — toque em lista: título pra exibir, id pra rotear (2026-09-11)"
Cohesion: 0.29
Nodes (6): §1 — Causa raiz (confirmada no código), §2 — O que mudou, §3 — Testes, §4 — Deploy (NÃO FEITO) — ordem obrigatória, §5 — Pendências, CHECKPOINT — toque em lista: título pra exibir, id pra rotear (2026-09-11)

### Community 391 - "test_multi_professional_plugin.py"
Cohesion: 0.07
Nodes (70): _count_active(), create_unit(), _get_unit(), list_units(), AsyncSession, UUID, Doctor hub — units CRUD (multi_unit addon).  GET   /tenants/me/units      - li, Gate an activate/create-active mutation. Raises HTTPException on failure. (+62 more)

### Community 392 - "availability_tool.py"
Cohesion: 0.12
Nodes (25): _clinic_today(), _entry_minutes(), _fmt(), get_availability(), _names(), _note(), _pick_professional(), Any (+17 more)

### Community 393 - "fetch_professional_emails"
Cohesion: 0.17
Nodes (21): fetch_professional_emails(), `{professional_id: email}` for one tenant's linked professionals.      Keys ar, _configured(), MonkeyPatch, Request, Tests for services/brain_professionals.py — the professional-email lookup.  Mi, Count-only observability: the tenant id and how many are linked, never     who, Base URL / key set, so each test isolates the failure mode it is about. (+13 more)

### Community 394 - "conftest.py"
Cohesion: 0.20
Nodes (9): client(), _clinic_wide_agenda_viewer(), _no_turn_apology(), AsyncClient, MonkeyPatch, Shared pytest fixtures and deterministic test environment., Keep the "patient never unanswered" net out of tests that fake the sender., An httpx AsyncClient bound to the FastAPI app via ASGITransport.      The app (+1 more)

### Community 395 - "TASK-030 — captura completa e datas em linguagem natural"
Cohesion: 0.18
Nodes (10): Decisões e limites, Estado, Integração/publicação autorizadas — 2026-10-07, Movimento concorrente da main, Problema e comportamento novo, Prova real após deploy — 2026-10-07, Revisão: escolha de médico, TASK-030 — captura completa e datas em linguagem natural (+2 more)

### Community 396 - "summarize_turn"
Cohesion: 0.14
Nodes (25): diagnose(), AIMessage, Any, BaseMessage, Observability for one agent turn: WHAT the model did, never what anyone said., One stable word for WHY a turn produced no usable text, or "ok".      Chosen t, Reduce the messages a turn ADDED (not the history it received) to a summary., _reasoning_tokens() (+17 more)

### Community 397 - "CHECKPOINT — o código de 6 dígitos vira portão antes da consulta existir"
Cohesion: 0.15
Nodes (12): 1. O que passou a valer, 2.1 `BookingGate` é **fail-open**, e isso não é descuido, 2.2 Por que o gate viaja num `ContextVar`, 2.3 O que acontece com o card do TASK-003 e com o handoff do TASK-004, 2.4 Bug 2 — sair da espera passou a depender de existir reserva, 2.5 Bug 1 — a LLM não pode anunciar o que nenhuma tool fez, 2. Peças, 3. Prova (rodada 2026-09-20) (+4 more)

### Community 401 - "manage_request.py"
Cohesion: 0.19
Nodes (21): appointment_ref(), find_appointment(), _land_new_day(), manage_target(), ManageResolution, _owner_id(), Any, datetime (+13 more)

### Community 402 - "test_turn_safety_net.py"
Cohesion: 0.12
Nodes (19): _entitled(), _FakeRedis, _FakeSession, MonkeyPatch, The patient is never left in silence - and the net cannot be abused., _reply(), spy(), test_a_looping_sender_gets_at_most_three_apologies() (+11 more)

### Community 403 - "reminder_text.py"
Cohesion: 0.07
Nodes (55): buttons_allowed(), _card_notice(), confirm_text(), _confirmation_version(), _doctor(), edit_text(), notify_attended(), notify_staff_confirmation() (+47 more)

### Community 404 - "BookingDraftRequested"
Cohesion: 0.06
Nodes (26): BookingDraftRequested, Raised by `set_booking_draft` (v1 and v2): the agent resolved what the patient w, test_current_tool_accepts_clinic_local_date_expressions(), test_flag_off_tool_carries_the_patient_and_the_requested_day(), test_unambiguous_first_name_keeps_the_chosen_doctor(), test_unknown_who_remains_unknown_when_no_new_fields_are_given(), test_the_draft_tool_caps_the_message(), _config() (+18 more)

### Community 405 - "test_availability_windows_scan.py"
Cohesion: 0.10
Nodes (27): _Calendar, _GoogleEvents, _GoogleService, _hhmm(), _many_windows(), date, The scan behind get_availability: windows from the real free-time code (TASK-030, `per_day` separate windows a day: one free 30-minute slot every two hours from 0 (+19 more)

### Community 407 - "load_pseudonymizer"
Cohesion: 0.23
Nodes (13): _attendee_names(), current_pseudonymizer(), load_pseudonymizer(), persist_pseudonymizer(), Pseudonymizer, UUID, DB layer of the PII pseudonymization step: load, carry and persist the map.  T, Pin a just-captured attendee name into this conversation's token map.      Cal (+5 more)

### Community 408 - "canonical_service_name"
Cohesion: 0.10
Nodes (36): canonical_service_name(), Any, UUID, Booking scope: WHO owns a booking, WHICH service it is — one rule, everywhere., Name of one catalog entry, whichever shape it travels in.      The effective c, Every non-empty name in `services`, in catalog order., The catalog's OWN spelling of the service `candidate` refers to, or None., The ONE active professional, or None when there is not exactly one.      "Exac (+28 more)

### Community 411 - "panel.py"
Cohesion: 0.14
Nodes (15): _admin_token_scheme, main(), _parse_args(), Namespace, Wipe dynamic data from the database for clean testing.  By default truncates t, Security, Administrative endpoints (currently: data wipe).  Every route here is guarded, Reset the database. Requires `confirm: true` to actually run. (+7 more)

### Community 412 - "CHECKPOINT — Portal: a conversa mostra as mensagens MAIS RECENTES (TASK-028)"
Cohesion: 0.17
Nodes (11): 1. Causa (L1), 2. O que mudou e onde, 3. O contrato (rota `GET /internal/brain-message/conversations/{external_id}/messages`), 4. Ordem de deploy, 5. Como desfazer, 6. Validação (evidência do implementador da Task 1; nada foi rerodado nesta rodada de documentação), 7. Prova ao vivo — PENDENTE, 8. Não afeta o WhatsApp — por quê (+3 more)

### Community 413 - "test_reminder_v2_hooks.py"
Cohesion: 0.21
Nodes (12): datetime, services/reminder_hooks.py — the schedule follows the appointment (TASK-032 R2)., _rows(), test_a_booking_in_a_clinic_with_the_switch_off_plans_nothing(), test_a_booking_plans_the_day_and_hour_reminders(), test_a_hook_never_raises(), test_a_reschedule_retires_old_rows_and_plans_new_ones(), test_a_reschedule_with_the_switch_off_still_zeroes_the_confirmation() (+4 more)

### Community 414 - "test_brain_patients.py"
Cohesion: 0.31
Nodes (9): _mock(), Patient contact lookup fails softly and keeps contact data out of logs., test_invalid_json_is_none(), test_network_error_is_none_without_logging_exception_text(), test_non_200_is_none(), test_posts_tenant_and_uuid_handle_with_internal_auth(), test_result_distinguishes_authority_from_unavailable(), test_unconfigured_does_not_make_a_request() (+1 more)

### Community 416 - "confirmation_warnings.py"
Cohesion: 0.14
Nodes (25): _agenda_link(), _appointment_conditions(), claim_warning(), _clinic_day(), due_candidates(), _notify_clinic(), _patient_label(), process_confirmation_warnings() (+17 more)

### Community 418 - "Lembretes R7 — Ações da clínica na agenda avisam o paciente (API) Implementation Plan"
Cohesion: 0.08
Nodes (24): Consumes (verified against `origin/main` 5e66c9d), Decisions taken where the spec is silent (for the owner), File Structure, Global Constraints, Lembretes R7 — Ações da clínica na agenda avisam o paciente (API) Implementation Plan, Produces for R5 (front) — exact wire shapes, Review Focus, Task 10: The clinic's edit — validate, move Google fail-closed, write the row (+16 more)

### Community 419 - "build_identity"
Cohesion: 0.12
Nodes (18): ServiceRole, alembic_head(), build_identity(), Path, Normalise a commit sha to its short form so both sides compare equal.      A p, First directory containing `migrations/versions`, or None.      Checked from t, Head revision of the migration scripts shipped in THIS image.      This is the, Hash of the `secretaria` sources this process actually imported.      The poin (+10 more)

### Community 429 - "CalendarService"
Cohesion: 0.07
Nodes (36): CalendarService, datetime, timedelta, Async wrapper around the (sync) Google Calendar v3 API., Build a CalendarService using per-tenant credentials.          The OAuth app (, The slot length this agenda walks when no service is named.          The tenan, Return events overlapping [start, end) on the clinic calendar.          Each i, Every event overlapping [start, end) - the hub agenda's read.          Same it (+28 more)

### Community 444 - "Pipeline channel-neutro + endpoints internos do Brain-Message"
Cohesion: 0.18
Nodes (10): A bomba-relógio que este round desarmou, A pergunta da §1 do prompt — respondida, Inventário de `.wa_id` do prompt anterior — o que foi de fato ajustado aqui, Limitação conhecida do endpoint de leitura, O que entrou, Pendências, Pipeline channel-neutro + endpoints internos do Brain-Message, Provas (+2 more)

### Community 447 - "CHECKPOINT — IA entra em qualquer etapa: `get_availability`, ferramentas de agenda cegas e o conjunto v2 (TASK-030 P4)"
Cohesion: 0.13
Nodes (14): 10. WhatsApp e Portal: implementação compartilhada e prova local, 11. Validação, revisão e limites da prova, 12. Integra??o com os ajustes de atendimento da main (2026-10-07), 13. Deploy comprovado e reteste real — 2026-10-07, 1. O que muda, 2. Onde está cada peça (âncoras), 3. O que a IA recebe, 4. As paredes da cegueira (+6 more)

### Community 448 - "CHECKPOINT — pergunta de nome na abertura (WhatsApp + Portal) e ramo "e-mail já cadastrado" (2026-09-21)"
Cohesion: 0.17
Nodes (11): §1 — Causa raiz (confirmada no código antes de programar), §2 — A sequência nova, §3 — Cópia exata das mensagens (`services/patient_name.py`), §4 — Regra de validação do nome (`parse_patient_name`), §5 — Contrato consumido (brain-api parte 1) e compatibilidade, §6 — Decisão: o ramo "e-mail já cadastrado" REAPROVEITA `AWAITING_EMAIL_CODE`/`verify_code`, §7.1 — PII: respostas que NÃO viraram `Patient.name` (achado 3 do Reviewer), §7 — TTL de silêncio nos dois canais (+3 more)

### Community 453 - "test_reminder_confirmation.py"
Cohesion: 0.21
Nodes (26): display_state(), The state the agenda shows: unconfirmed / confirmed / confirmed_twice / attentio, _appt(), _appt_row(), _booked(), _confirm(), register_confirmation + display_state (TASK-032 R1, spec 4.1 'Regra de contagem', An appointment 6 days out with its 3 reminder rows; returns (appt, {kind: id}). (+18 more)

### Community 457 - "test_calendar_unavailable_mapping.py"
Cohesion: 0.14
Nodes (30): _fake_calendars_service(), _http_error(), BaseException, HttpError, MonkeyPatch, RefreshError, SimpleNamespace, Detection-layer contract: every "Google Calendar is broken" mode funnels into a (+22 more)

### Community 458 - "message_status.py"
Cohesion: 0.16
Nodes (21): One delivery receipt (`value.statuses[]`, webhook field `messages`).      `id`, WebhookStatus, apply_whatsapp_statuses(), failure_reason(), mark_read(), meta_timestamp(), _outbound_ids(), AsyncSession (+13 more)

### Community 464 - "test_reminder_v2_delivery.py"
Cohesion: 0.18
Nodes (25): deliver_reminder(), Send one reminder. Never raises: every failure is an outcome., Sentence + flattened requirements in one line (single-variable templates)., single_line_text(), _job(), mail(), services/reminder_delivery.py — one reminder, the patient's own channel (TASK-03, Portal e-mail: brain-api answers an address; the mailer records the send. (+17 more)

### Community 465 - "test_ai_tool_output_allowlist.py"
Cohesion: 0.07
Nodes (41): allowlisted(), _clean_plain(), _clean_records(), _log_name(), OutputAllowlist, Any, What a v2 tool may hand the model: a declared allowlist of output keys (TASK-030, A key as it may appear in a log line: a plain identifier, else "<other>". (+33 more)

### Community 490 - "CHECKPOINT — Portal: a automação fala primeiro, o aviso do código vira card, e o PreCheck sai pelo Portal"
Cohesion: 0.22
Nodes (8): 1. `POST /internal/brain-message/open` — a conversa nasce sem inbound, 2. O aviso do código vira card de 3 botões, com o e-mail mascarado, 3. Handoff do PreCheck a partir do Portal, CHECKPOINT — Portal: a automação fala primeiro, o aviso do código vira card, e o PreCheck sai pelo Portal, O invariante que MUDOU (e está registrado nos dois módulos), O que NÃO foi provado, Ordem de deploy (nada disto foi executado), Validação

### Community 491 - "_flow_handback_tools"
Cohesion: 0.20
Nodes (20): _flow_handback_tools(), This turn's `extra_tools`: the plugin set + the flow hand-back tools.      `se, test_i3_edit_agent_has_no_booking_cancel_or_navigation_tools(), _config(), _mentioned(), _pinned_day(), MonkeyPatch, Which system prompt a turn gets (TASK-030 P5): ai/graph.py::_turn_system_prompt. (+12 more)

### Community 492 - "db"
Cohesion: 0.05
Nodes (65): db(), Shared in-memory fixtures for the reminder foundation tests (TASK-032 R1).  Im, _edit(), The shared row writer of an appointment edit (TASK-032 R6 extraction, used by R7, test_a_new_time_moves_the_row_and_logs_a_reschedule(), test_convenio_and_attendee_are_written_and_reported(), test_the_event_link_changes_only_with_the_calendar(), test_without_a_new_time_the_status_and_start_stay() (+57 more)

### Community 493 - "CHECKPOINT — conta ativa do Portal abre clínica nova: sem e-mail/código; nome vem da conta ou é perguntado uma vez (2026-09-24)"
Cohesion: 0.22
Nodes (8): §1 — Ponto de entrada (confirmado no código), §2 — Decisões, §3 — Testes, §4 — Validação (comandos e resultados reais, 2026-09-24, rodada 2), §5 — Achados laterais, §6 — Deploy e pendências, §7 — Nome do paciente na saudação inicial (Portal) — 2026-09-25, CHECKPOINT — conta ativa do Portal abre clínica nova: sem e-mail/código; nome vem da conta ou é perguntado uma vez (2026-09-24)

### Community 494 - "CHECKPOINT — Agendar para outra pessoa ("Essa consulta é pra você?")"
Cohesion: 0.17
Nodes (11): CHECKPOINT — Agendar para outra pessoa ("Essa consulta é pra você?"), Coordenação, Correção 2026-10-01 — "Essa consulta é pra você?" repetida após o retorno da IA, LLM, O que o paciente vê, Onde o nome vive, PII — por que nenhum código novo de mascaramento, Quem usa o nome (+3 more)

### Community 495 - "CHECKPOINT — OTP / reset link leaking through arq's native job log"
Cohesion: 0.25
Nodes (7): 1. Causa raiz (reconfirmada na versão instalada, `arq==0.28.0`), 2. A correção, 3. Testes (`tests/test_arq_log_secret_redaction.py`), 4. Outros loggers de terceiro (passo 3 do prompt), 5. Pendências, 6. Revisão de segurança (2026-09-21), CHECKPOINT — OTP / reset link leaking through arq's native job log

### Community 496 - "_r7_support.py"
Cohesion: 0.26
Nodes (18): acting(), FakeArqPool, install_pool(), datetime, UUID, Wiring shared by the TASK-032 R7 hub tests (status, confirm, attended, edit, can, recent(), set_appointment() (+10 more)

### Community 497 - "_post_booking"
Cohesion: 0.28
Nodes (12): _claim(), _conversation_id(), _ledger_key(), _post_booking(), UUID, CORE post_booking hook: ask a Brain-Message visitor for the code, after booking., Give the ledger row back: the code notice did NOT go out. Best-effort., This patient's conversation row, which IS the Brain-Message address.      `Bra (+4 more)

### Community 498 - "_connect_calendar"
Cohesion: 0.16
Nodes (23): _connect_calendar(), db(), _events(), AsyncClient, Typed straight into Google Calendar — the UI must keep cancel disabled     rath, Isolation invariant: a shared or mis-configured Google account must not     han, A month of a busy clinic is hundreds of events; the N+1 shape would put     tha, The whole point of the change: a blank box used to mean silence. (+15 more)

### Community 499 - "_handle_remove_context_command"
Cohesion: 0.05
Nodes (62): Last `size` DIGITS of a phone/wa_id — the only form allowed in a log.      Ret, wa_suffix(), begin_turn(), end_turn(), Open a fresh send ledger for one inbound turn; reset it with `end_turn`., Answer one inbound turn - and guarantee the patient is never left in silence., _send_bot_reply(), _open_brain_message_conversation() (+54 more)

### Community 500 - "BrokenRedis"
Cohesion: 0.19
Nodes (10): BrokenRedis, AsyncClient, `/health` is a frozen contract: a load balancer may match on this body.      B, A silent worker must never read as agreement.      Under ASGITransport the lif, The endpoint that proves the deploy must not become the leak., Every call raises — the "Redis went away mid-flight" shape., test_build_endpoint_exposes_no_secret(), test_build_endpoint_proves_what_this_process_runs() (+2 more)

### Community 501 - "CHECKPOINT — Onda 4: jornada sem gate ponta a ponta (E2E, sem PreCheck)"
Cohesion: 0.29
Nodes (6): Achados — divergências entre CHECKPOINT e realidade (uma linha cada, sem investigação de causa), CHECKPOINT — Onda 4: jornada sem gate ponta a ponta (E2E, sem PreCheck), Comandos de teste rodados (repetíveis), Matriz de cenários — resultado, Pendências para uma sessão futura, Veredito dos Achados 10 e 11 (fechamento 2026-09-24)

### Community 502 - "history_item_is_final"
Cohesion: 0.22
Nodes (14): history_item_is_final(), True when this `history` chunk signals the sync is FULLY complete.      Three, _history_item(), COMPLETE_CHUNK means "this chunk is done, more are coming" - not final., Unknown-variant tolerance: a numeric 100% is enough even without a     literal, A non-numeric `progress` (unknown variant) must never raise., test_final_case_insensitive_phase(), test_final_false_when_no_metadata_no_errors() (+6 more)

### Community 503 - "test_reminder_r3_cancel_path.py"
Cohesion: 0.24
Nodes (22): give_up_confirm_buttons(), The give-up confirmation: Sim, cancelar / Manter consulta., add_appointment(), seed_paid_deposit(), _deposit_and_tenant(), _future(), datetime, Direct Cancelar and legacy edit cards (TASK-032 R3 compatibility after R6).  T (+14 more)

### Community 505 - "d4f8a2c6e913_insurance_mode_catalog_ext_custom.py"
Cohesion: 0.25
Nodes (10): _backfill_unmatched_insurance_strings(), downgrade(), _normalize(), insurance mode + extensible catalog + convênio "Outro"  TASK-008 (docs/CHECKPO, Re-target `appointments.insurance_plan_id` at `tenant_insurance_plans.id`., # NOTE: rows created under independent/custom modes (catalog_id, Best-effort reverse of `_repoint_insurance_plan_id` for dev/test only.      Ma, _repoint_insurance_plan_id() (+2 more)

### Community 506 - "Digitando — backend universal (TASK-033)"
Cohesion: 0.29
Nodes (6): Contrato e ciclo de vida, Decisões e limites, Deploy e limites da prova local, Digitando — backend universal (TASK-033), Estado, Validação

### Community 507 - "_run_hand_back_with_intro"
Cohesion: 0.29
Nodes (7): _fallback(), Owner, 2026-10-07: the patient who asked something reads the answer first,, Review of TASK-038: a hand-back that ends up sending no card must still get the, _run_hand_back_with_intro(), test_hand_back_words_are_sent_before_the_card(), test_hand_back_words_claiming_an_unproven_action_are_dropped(), test_hand_back_words_never_go_out_alone()

### Community 528 - "Lacunas encontradas"
Cohesion: 0.15
Nodes (12): Comportamentos que funcionaram (não regredir), L1 — Conversa com mais de 50 mensagens congela no Portal  (ALTA, causa raiz provada), L2 — Duas conversas para o mesmo paciente após verificar o código  (ALTA), L3 — A LLM promete o que não pode fazer  (MÉDIA), L4 — Perguntas factuais comuns não têm onde buscar resposta  (MÉDIA), L5 — Vazamento de rótulo de botão como se fosse serviço/opção  (BAIXA), L6 — Sem limite de entrada no Portal  (MÉDIA, abuso/custo), L7 — Sem indicação de "digitando"/progresso  (BAIXA) (+4 more)

### Community 529 - "test_brain_message_email_otp_inline.py"
Cohesion: 0.12
Nodes (18): _inbound(), The two inline identity steps, and the WhatsApp path proving it never sees them., 20 code units is the cap `interactive_buttons_record` applies.      Pinned bec, No mask must never become an invented address, or an empty slot., Additive field, additive behaviour: absent is SENT without a mask., Non-regression: the buttons are an addition, not a replacement., Consumer contract: exact paths, key header and minimal typed payloads., CHECKLIST: the correct code confirms the account. (+10 more)

### Community 531 - "File Structure"
Cohesion: 0.15
Nodes (12): "Digitando…" universal — Backend (secretarIA + brain-api) — Implementation Plan, File Structure, Global Constraints, Pendências (registradas, fora do escopo), Review Focus, Task 1: Serviço do indicador (Redis, fail-open, por quem digita), Task 2: A automação liga e desliga a marca (worker), Task 3: A listagem do Portal informa `typing`, `typing_by`, `accepts_typing` (+4 more)

### Community 548 - "Referência — como convênios de saúde pagam a consulta no Brasil (2025/2026)"
Cohesion: 0.22
Nodes (8): Caveats — ler antes de usar qualquer valor como definitivo no seed do catálogo, Por que isso importa para o produto (não é só contexto de fundo), Referência — como convênios de saúde pagam a consulta no Brasil (2025/2026), Tabela 1 — modelo de remuneração por operadora (para classificar cada linha do catálogo), Tabela 2 — honorário por consulta (prestador credenciado), amostra RJ (CREMERJ, 19/09/2025), Tabela 3 — reembolso ao paciente por categoria de plano (livre escolha), Tabela 4 — coparticipação (não é sinal, é cobrança própria do convênio — informativo), TL;DR da pesquisa

### Community 549 - "test_workers_layering.py"
Cohesion: 0.23
Nodes (9): _imported_workers_modules(), _modules(), Path, Layer rules for the split of workers/tasks.py (TASK-023).    shared/*     neut, `secretaria.workers.<first>` segments imported by a file., A cycle only shows for some import orders; a fresh interpreter per entry point, test_channel_packages_respect_the_layers(), test_composition_modules_never_import_the_facade() (+1 more)

### Community 551 - "outbound_messages"
Cohesion: 0.05
Nodes (47): FakeCalendar, Owner-calendar double: records deletes and creations, every day is open., fake_waba_token(), FakeWhatsAppClient, outbound_messages(), Records every send; installed in place of `WhatsAppClient`.      `failing_temp, mail(), _notice() (+39 more)

### Community 552 - "CHECKPOINT — A IA atende como pessoa; humano por último (TASK-038)"
Cohesion: 0.18
Nodes (10): 1. Texto livre que não é resposta da etapa → uma rodada da IA, na MESMA etapa, 2. A fala da IA acompanha a devolução aos botões, 3. "Não sei" mais humano, 4. Ordem de prioridade no prompt, 5. Oferta de atendente com Sim/Não (pedido do dono, 2026-10-07), 6. Ajustes depois dos testes reais (dono, 2026-10-07), Canais: WhatsApp e Portal (conferido em 2026-10-07, a pedido do dono), CHECKPOINT — A IA atende como pessoa; humano por último (TASK-038) (+2 more)

### Community 553 - "CalendarUnavailableError"
Cohesion: 0.09
Nodes (9): CalendarUnavailableError, Any, HttpError, _raise_if_unavailable(), Raised when Google Calendar cannot be reached or refused our credentials., Translate an outage/auth HttpError into CalendarUnavailableError.      Returns, One events.list page (blocking): the timed, busy events and the next page's toke, test_client_error_status_is_not_an_outage() (+1 more)

### Community 554 - "File Structure"
Cohesion: 0.17
Nodes (11): "Digitando…" universal + rolagem da janela recente — Frontend (Brain-Message-Frontend) — Implementation Plan, File Structure, Global Constraints, Review Focus, Task 0: A resposta nova entra na vista (janela das 50 mais novas), Task 1: Regra pura `lib/typing.ts`, Task 2: Componente `TypingBubble`, Task 3: O teclado avisa — gancho `onTyping` no `Composer` (+3 more)

### Community 555 - "c9f4e2a7b815_insurance_catalog.py"
Cohesion: 0.43
Nodes (6): Column, _backfill_legacy_insurances(), _created_at(), _normalize(), insurance_catalog: global convênio catalog + per-clinic and per-doctor plans, upgrade()

### Community 556 - "Portal: visita descartada ao entrar na conta — TASK-034"
Cohesion: 0.25
Nodes (7): Comportamento e contrato, Estado, Ordem de deploy (ainda não autorizado), Portal: visita descartada ao entrar na conta — TASK-034, Prova em produção — pendente, Revisão independente e correções, Validação local

### Community 557 - "test_cancellation_notice.py"
Cohesion: 0.10
Nodes (16): _FakeArqPool, datetime, Doctor-initiated cancellation: the patient is ALWAYS told, and told properly., The safe direction: a free-form send would just be rejected by Meta., Replaced by `justification`, not kept beside it — two fields with     confusabl, A cancellation notice landing hours later reaches somebody who has     already, Even with no clinic email on file, the abandonment carries a stable alarm     f, Cancelling with no reason is a supported path, not a degraded one — it     must (+8 more)

### Community 558 - "CHECKPOINT — observabilidade da LLM e "paciente nunca sem resposta" (2026-10-01)"
Cohesion: 0.20
Nodes (9): 1. Rastreio do turno da LLM (`ai/trace.py`, `ai/graph.py`), 2. Rede de segurança (`services/turn_safety_net.py`, `workers/tasks.py::_send_bot_reply`), 3. Orçamento de tempo, 4. Hand-backs da IA (TASK-030, plano P1) — `workers/shared/handback_log.py`, CHECKPOINT — observabilidade da LLM e "paciente nunca sem resposta" (2026-10-01), Como investigar um caso (roteiro), O que entrou, Pendências / não feito (+1 more)

### Community 559 - "availability_windows.py"
Cohesion: 0.14
Nodes (18): DayRange, FreeWindow, _parse_day(), date, datetime, Window, Free time as WINDOWS: the only shape of an agenda the AI may be told (TASK-030 P, One continuous stretch of free time on one day, in the clinic's wall clock. (+10 more)

### Community 560 - "Índice de execução — Portal: mensagens, visita fundida, "digitando" (2026-10-01)"
Cohesion: 0.20
Nodes (9): Decisões do dono incorporadas (2026-10-01), Dependências, Fora do escopo desta rodada (registrado), Mapa de paralelismo (Dispatching Parallel Agents), Ordem de merge e de deploy, Os planos, Papéis (mínimo suficiente — `AI_WORKFLOW.md`), Validação por repo (conforme `PRODUCTS.md`) (+1 more)

### Community 561 - "test_clinic_facts_hub.py"
Cohesion: 0.24
Nodes (16): db(), _put(), AsyncClient, Hub routes for the clinic facts and the completeness checklist (TASK-025).  Sa, PUT tenant fields through either hub route; the frontend saves through /configur, tenant(), test_a_save_without_the_field_leaves_the_facts_untouched(), test_completeness_follows_what_was_saved() (+8 more)

### Community 562 - "reminder_hooks.py"
Cohesion: 0.23
Nodes (14): after_appointment_booked(), after_appointment_closed(), after_appointment_rescheduled(), enabled_for(), _load(), datetime, UUID, Keep an appointment's reminder rows in step with the appointment (TASK-032 R2). (+6 more)

### Community 563 - "File Structure"
Cohesion: 0.20
Nodes (9): File Structure, Global Constraints, Review Focus, Task 1: secretarIA — apagar uma visita (`discard_visit`), Task 2: secretarIA — rota de fusão + job do worker, Task 3: brain-api — avisar a secretarIA depois de completar a visita, Task 4: Front — zerar o estado local quando a identidade troca, Task 5: Documentação e prova ao vivo (depois do deploy autorizado) (+1 more)

### Community 564 - "Checkpoint — MVP da secretarIA no Portal"
Cohesion: 0.25
Nodes (7): Checkpoint — MVP da secretarIA no Portal, E-mail e PII, Escopo, Implantação e ordem, O que entrou onde, QA e correções locais de 2026-10-01 — TASK-022, Validação e limites

### Community 565 - "CHECKPOINT — divisão de `workers/tasks.py` (TASK-023)"
Cohesion: 0.22
Nodes (8): 1. Objetivo e o que não mudou, 2. Camadas, 3. Mapa nome → módulo, 4. Como provar, 5. Como desfazer, 6. Pendências, 7. Fase 2 (TASK-024) — política de canal, CHECKPOINT — divisão de `workers/tasks.py` (TASK-023)

### Community 566 - "_WireClient"
Cohesion: 0.20
Nodes (5): MonkeyPatch, Records send_text_message / send_buttons; installed for the tests below., wire(), _wire_db(), _WireClient

### Community 567 - "CHECKPOINT — "Outro" responde a pergunta fixa; testes reais dos outros pontos de entrada da IA (TASK-037)"
Cohesion: 0.33
Nodes (5): 1. O que mudou (código), 2. Testes reais (produção, clínica de teste, Portal) — versão no ar 01f2af7, 3. Mapa: texto livre em cada etapa (leitura de código, main 01f2af7), 4. O que deve ser feito (recomendação — os itens 1, 2, 3 e 5 foram EXECUTADOS na TASK-038, ver `docs/CHECKPOINT_ia_atende_como_pessoa.md`; o item 4 aguarda decisão do dono), CHECKPOINT — "Outro" responde a pergunta fixa; testes reais dos outros pontos de entrada da IA (TASK-037)

### Community 568 - "TASK-030 — reteste do P3 publicado"
Cohesion: 0.33
Nodes (5): Casos executados, Identidade do deploy, Limitações e observações, Resultado, TASK-030 — reteste do P3 publicado

### Community 569 - "test_analytics_bi_plugin.py"
Cohesion: 0.26
Nodes (20): db(), _entitled_fake(), _patch_session_factory(), AsyncClient, MonkeyPatch, Tests for plugins/analytics_bi.py (post_booking hook) and the doctor-hub GET /t, _seed_event(), _seed_event_full() (+12 more)

### Community 570 - "test_express_confirmation.py"
Cohesion: 0.21
Nodes (19): The single professional: inherits the clinic's services and hours (NULL columns), _sole(), _source(), _kinds(), _on(), Express confirmation: a complete AI draft lands on the details and the card (TAS, skill date-derived-ui-labels: the label the patient reads and the slot `Confirma, _resolve_at() (+11 more)

### Community 571 - "free_slots_for_day"
Cohesion: 0.20
Nodes (20): available_day_starts(), days_with_free_slots(), free_slots_for_day(), date, datetime, tzinfo, Window, Free time on one agenda, minus the slots other conversations are holding.  The (+12 more)

### Community 573 - "Review Focus"
Cohesion: 0.18
Nodes (10): Desenho (sem estado novo), "digitando…" no PreCheck (Portal) — Plan (reescrito em 2026-10-08, executável), Global Constraints, O que mudou desde o plano original (2026-10-01) — por isso este plano foi reescrito, Review Focus, Task 0: Autorização e baseline, Task 1: Função pura de decisão (TDD), Task 2: Leitura da última linha (única SQL nova) (+2 more)

### Community 574 - "File Structure"
Cohesion: 0.25
Nodes (7): File Structure, Global Constraints, Portal mostra as mensagens mais recentes — Implementation Plan, Review Focus, Task 1: Listagem devolve as mensagens mais recentes, Task 2: Contrato documentado, Task 3: Prova ao vivo (depois do deploy autorizado)

### Community 575 - "_run_send_bot_reply_capturing_run_agent"
Cohesion: 0.23
Nodes (14): _days_from_now(), db(), datetime, A sessionmaker bound to a shared in-memory sqlite DB (StaticPool keeps     the, Owner, 2026-10-06: the "Outro" tap is answered by the flow with the fixed     ", Replaces the old `..._when_flows_disabled` case, whose premise is gone.      F, action="handover": flow fields persisted, the escalation message sent,     and, _run_send_bot_reply_capturing_run_agent() (+6 more)

### Community 576 - "parse_patient_name"
Cohesion: 0.18
Nodes (12): is_name_question(), _label_key(), parse_patient_name(), The explicit "qual é o seu nome?" step, on BOTH channels.  Until this module e, A button label or an answer reduced to its letters, for equality only.      Ca, Title-case one word, keeping hyphenated and apostrophe'd parts apart., The name in a direct answer to "qual é o seu nome?", normalized — or None., Whether an outbound row asked the patient for their name.      Pure. `ai/graph (+4 more)

### Community 577 - "db"
Cohesion: 0.20
Nodes (20): _booked(), _card_state(), _code_notice_row(), _ctx(), db(), _patient(), CHECKLIST: appointment committed with no proven address -> the code notice., An arq retry of run_post_booking_hooks must not mail a second code. (+12 more)

### Community 578 - "Lembrete com "Alterar Dados" Implementation Plan"
Cohesion: 0.11
Nodes (17): Deploy e liberação, File Structure, Global Constraints, Interfaces → Produces (later tasks rely on these exact names), Lembrete com "Alterar Dados" Implementation Plan, Review Focus, Self-review (done while writing; kept for the reviewer), Task 10: Checkpoint, Meta template doc, and the validation (+9 more)

### Community 579 - "decode_action_id"
Cohesion: 0.10
Nodes (11): decode_action_id(), Decode one "<action>|<uuid>" id or payload, whatever carried it.      The chan, The cancel-path button ids (TASK-032 R3, spec §4.3)., test_a_longer_id_never_decodes_as_its_shorter_sibling(), test_the_cancel_path_ids_decode(), The reminder card's buttons and ids (TASK-032 R6, spec §5.1)., test_legacy_ids_from_cards_still_on_screen_still_decode(), test_remedit_decodes_like_the_other_row_ids() (+3 more)

### Community 581 - "PatientOpeningContext"
Cohesion: 0.13
Nodes (21): PatientOpeningContext, Resolved opening state + the data that produced it.      Buckets are evaluated, _greeting_tenant(), datetime, One future appointment: full detail (when/doctor/service/price/description/, 2+ future appointments: nearest gets the detail block, the others get ONE     b, No catalog match (stale/renamed service) and no doctor: the detail block     st, HAS_UPCOMING(_SOON) + flows enabled: [Remarcar] [Cancelar] [Outro] —     unchan (+13 more)

### Community 583 - "logging.py"
Cohesion: 0.14
Nodes (14): BoundLogger, LogRecord, ArqJobArgsRedactionFilter, get_logger(), install_arq_log_redaction(), Structured logging setup using structlog.  JSON output in non-dev environments, Scrub a free-text log message the same way `redact_secrets` scrubs keys., Redact job arguments that arq's own logger prints in clear text.      `arq.wor (+6 more)

### Community 586 - "_seed"
Cohesion: 0.14
Nodes (21): One appointment, optionally owned by a professional and linked to a     patient, The patient has NOT been told. That is the doctor's choice (they were     shown, No conversation at all — a free-form send would just be rejected., arq retries jobs. Inside the window a duplicate is inbox noise; outside     it, A claim is a lock, not a receipt — but giving the lock back is only half     of, Outside the 24h window every send is billed, so the retry path is the one     p, The message already went out. A metering blip must not reach the retry     path, Count-only observability: ids and reasons, never patient content.      `capsys (+13 more)

### Community 587 - "test_availability_windows.py"
Cohesion: 0.21
Nodes (15): datetime, _range(), Free time as windows: the range rules and the grouping (TASK-030 P4, spec §4.6)., _slot(), test_a_365_day_range_is_clamped_to_14_days_and_says_so(), test_a_gap_starts_a_new_window(), test_a_range_across_a_month_and_year_boundary_counts_real_days(), test_an_empty_range_is_today_through_the_largest_allowed_span() (+7 more)

### Community 588 - "Lembrete com "Alterar Dados" — Design"
Cohesion: 0.11
Nodes (17): 10. Riscos conhecidos, 1. Objetivo e intenção, 2. Decisões do dono (2026-10-07), 3. Premissas minhas (a confirmar na revisão), 4. O que existe hoje e será reaproveitado, 5.1 O lembrete e a abertura do chat, 5.2 O rascunho de mudança, 5.3 Os menus (+9 more)

### Community 589 - "test_ai_toolset_v2_locks.py"
Cohesion: 0.15
Nodes (10): _ExplodingCalendar, _Log, The v2 toolset's second lock: each withheld or legacy tool refuses by itself (TA, The lock is armed only by the v2 context: with it off the tool reaches its own l, A tool added tomorrow that reads or writes the agenda must be put on the withhel, Any use is a bug: a refused tool must never reach the calendar., test_a_withheld_or_legacy_tool_refuses_on_a_v2_turn_without_touching_the_calendar(), test_every_calendar_touching_tool_is_classified() (+2 more)

### Community 590 - "Part B — Brain-Message-Frontend (after R5 is merged)"
Cohesion: 0.12
Nodes (16): Decisions taken where the spec is silent, File Structure, Global Constraints, Lembretes R8 — "Meus pacientes" no console e escolha do médico na agenda Implementation Plan, Part A — secretarIA, Part B — Brain-Message-Frontend (after R5 is merged), Produces for Part B — exact wire shapes, Review Focus (+8 more)

### Community 591 - "CHECKPOINT — Lembretes R2: motor de lembretes (TASK-032)"
Cohesion: 0.29
Nodes (6): CHECKPOINT — Lembretes R2: motor de lembretes (TASK-032), Decisões tomadas sem consulta, Estado, O que entrou onde, Pendências, Validação (2026-10-06, `BOT_ALLOWLIST_WA_IDS=""`)

### Community 592 - "guard_reply"
Cohesion: 0.16
Nodes (13): _Claim, guard_reply(), _p(), Pattern, The LLM may not announce an action it did not perform.  ## The bug that produc, The action `text` claims without evidence, or None if it claims nothing., `text`, or the honest replacement when it claims an unproven action.      The, unbacked_claim() (+5 more)

### Community 593 - "test_multi_unit_plugin.py"
Cohesion: 0.21
Nodes (15): _agent_context(), db(), _FakeCalendarService, _fakes(), MonkeyPatch, Tests for plugins/multi_unit.py — unit-aware booking tools.  Same in-memory-sq, The sole booking tool for a tenant with multi_unit and no     multi_professiona, _seed_tenant_and_units() (+7 more)

### Community 595 - "test_cors_origins_parsing.py"
Cohesion: 0.18
Nodes (16): _origins(), CORS_ALLOW_ORIGINS parsing — the format the mesh actually ships.  The failure, The exact string that was live in EasyPanel when the portal broke., The actual regression: an origin carrying `["` or `"]` matches nothing., The old format stays valid — this is additive, nothing to migrate., A typo must reject everyone, never crash the process building Settings., Same origins, either spelling — the property the mesh relies on., test_blank_values_yield_no_origins() (+8 more)

### Community 596 - "test_hub_conversations_my_patients.py"
Cohesion: 0.32
Nodes (18): _appointment(), _conversation(), db(), _ids(), _professional(), AsyncClient, UUID, GET /tenants/me/conversations?mine=true - "Meus pacientes" (TASK-046 R8, spec 20 (+10 more)

### Community 597 - "_booking_context"
Cohesion: 0.12
Nodes (18): _config_with_selected_professional(), Overlay an explicitly-selected professional onto the runtime config.      `loa, _booking_context(), Agent context with a catalog to validate `appointment_type` against., The service is proven against the catalog BEFORE the hand-back: it lands     on, Recoverable, not raised: the model can correct itself in the same turn     inst, A clinic with nothing to prove a service against books typeless, the     same h, Second lock. The day picker it opens would read availability off the     CLINIC (+10 more)

### Community 598 - "test_hub_config_extra_reminder.py"
Cohesion: 0.22
Nodes (21): _custom_due(), _planned_custom_due(), AsyncClient, datetime, MonkeyPatch, The extra reminder on the hub configuration (TASK-048 R9, spec §6.2; R7 §5.B rep, _set(), _stored() (+13 more)

### Community 599 - "Modelos do WhatsApp para os lembretes (TASK-032 R2)"
Cohesion: 0.33
Nodes (5): 1. Modelo novo com 3 botões — `lembrete_consulta_v2`, 2. Modelos que já existem e continuam em uso, 3. Configuração e ativação (quem faz: o dono, no painel; nenhuma automação mexe em variáveis), Modelos do WhatsApp para os lembretes (TASK-032 R2), R7 — avisos das ações da clínica (2026-10-09) — PROPOSTA, não submetida

### Community 600 - "main"
Cohesion: 0.14
Nodes (15): main(), _parse_args(), Namespace, Apply a JSON config file onto a tenant row (the doctor-hub columns).  This is, Return a list of human-readable warnings (does not block the write)., # NOTE: "greeting_buttons" is intentionally NOT in _ALLOWED_FIELDS anymore, _validate(), menu_buttons() (+7 more)

### Community 601 - "_load_snapshot"
Cohesion: 0.22
Nodes (13): ProfessionalEditNotice, dispatch_pending_professional_edits(), enqueue_professional_edit_notification(), _finish_notice(), _load_snapshot(), A deterministic doctor email for a committed edit, with references-only ARQ args, Recover committed intents after lost enqueue/crash; bounded lease and batch., Fresh tenant-scoped recipient and data, optionally serialized with appointment e (+5 more)

### Community 602 - "R9 — PreCheck out of the console + extra reminder "N dias antes, às HH:MM" Implementation Plan"
Cohesion: 0.10
Nodes (19): Decisions taken where the spec is silent, File Structure, Global Constraints, Part A — secretarIA, Part B — Brain-Message-Frontend, Produces for Part B — exact wire shapes, R9 — PreCheck out of the console + extra reminder "N dias antes, às HH:MM" Implementation Plan, Review Focus (+11 more)

### Community 603 - "verify_meta_signature"
Cohesion: 0.23
Nodes (13): Security helpers - Meta webhook HMAC-SHA256 signature validation.  Meta signs, Return True if `signature_header` is a valid HMAC of `raw_body`.      Args:, verify_meta_signature(), Tests for the Meta webhook HMAC-SHA256 signature validation., Build a valid `X-Hub-Signature-256` header value for `body`., _sign(), test_empty_app_secret_is_rejected(), test_invalid_signature_is_rejected() (+5 more)

### Community 604 - "backfill_service_catalog.py"
Cohesion: 0.20
Nodes (13): collect_groups(), _Group, look_alike_pairs(), main(), _print_report(), UUID, Populate the canonical service catalog from the existing JSON entries.  DELIVE, Distinct services whose names resemble each other. NEVER merged.      Advisory (+5 more)

### Community 605 - "test_patient_context.py"
Cohesion: 0.14
Nodes (14): _FakeArqPool, _patch_session_factory(), MonkeyPatch, Tests for services/patient_context.py — the opening-state resolver + the shared, First trim stage: only the description is dropped — brief lines and     orienta, Maximally trimmed: description gone, brief lines capped with the     '… e mais, One source of truth: the manage-flow trigger uses the exact same query     func, Records every enqueue_job call; installed on app.state.arq_pool. (+6 more)

### Community 606 - "booking_draft.py"
Cohesion: 0.09
Nodes (39): The third party's name, or None for "pra mim" / not asked (never "")., real_attendee_name(), _accepted(), _booking_details(), _booking_services(), _carry(), _check(), _Checked (+31 more)

### Community 607 - "TASK-032 R6 — lembrete com Alterar Dados"
Cohesion: 0.18
Nodes (10): Decisões e adaptações, Estado, O que entrou onde, Pendências antes da liberação, progress.md, Registro completo das decisões durante a execução, Regras fixadas e testes, TASK-032 R6 — lembrete com Alterar Dados (+2 more)

### Community 608 - "TASK-037/038 — testes reais após deploy (2026-10-07)"
Cohesion: 0.40
Nodes (4): Pontos a melhorar, Resultados, Reteste dos ajustes do dono (c11f4ad), 12:49–12:54 BRT, TASK-037/038 — testes reais após deploy (2026-10-07)

### Community 609 - "test_deposit_release_preview.py"
Cohesion: 0.27
Nodes (14): _deposit(), _fake_refund(), _predicted_and_actual(), MonkeyPatch, Preview of what a cancellation does to a paid deposit (TASK-032 R4, spec 4.4)., _setup(), _tenant(), test_an_unknown_outcome_still_warns() (+6 more)

### Community 610 - "test_appointment_edit_pure.py"
Cohesion: 0.22
Nodes (11): _appt(), The pure pieces of "Alterar Dados" (TASK-032 R6, spec §5.2/§5.5)., test_a_draft_starts_equal_to_the_appointment_in_clinic_time(), test_a_long_recap_drops_orientations_before_it_breaks_the_card_limit(), test_changed_names_each_field_once_in_a_fixed_order(), test_json_round_trip_and_garbage_is_none(), test_the_final_recap_can_report_the_applied_update(), test_the_original_never_moves_and_the_draft_is_immutable() (+3 more)

### Community 611 - "test_simulation_api.py"
Cohesion: 0.22
Nodes (15): db(), AsyncClient, POST /tenants/me/simulation/turn: faithful, validated and side-effect free (TASK, _slots(), tenant(), test_a_professional_of_another_clinic_is_a_404(), test_a_simulation_writes_and_enqueues_nothing(), test_a_tampered_state_is_refused() (+7 more)

### Community 612 - "test_service_guides_multipro.py"
Cohesion: 0.13
Nodes (38): session(), _clinic(), _direct(), _guide(), maker(), _nothing_about_the_service(), Service orientations in a clinic with 2+ active professionals (TASK-025 producti, The "Tipos de consulta disponíveis" part of the prompt, up to the next blank lin (+30 more)

### Community 613 - "_resolve"
Cohesion: 0.22
Nodes (18): _add_appointment(), db(), Beyond the 48h soon window, and CONFIRMED counts as upcoming too., A SCHEDULED row 10 days in the past (beyond the 48h lookback) must NOT     read, A cancelled future row never triggers HAS_UPCOMING*, and a cancelled     past r, The owning professional's OWN catalog wins (matched casefold), and a     deacti, No owning professional: the tenant's own catalog resolves the nearest     servi, _resolve() (+10 more)

### Community 614 - "ManageRequest"
Cohesion: 0.14
Nodes (29): ManageRequest, What the AI says about an existing appointment. Only `action` is required., _Agenda, _appt(), _Gate, _local(), date, datetime (+21 more)

### Community 616 - "appointment_edit_flow.py"
Cohesion: 0.09
Nodes (64): EditContext, _after_slot_affecting_change(), apply_ai_edit(), _apply_confirm(), _attendee_question_result(), _attendee_step(), _availability(), _begin_date() (+56 more)

### Community 617 - "CHECKPOINT — Lembretes R7: ações da clínica na agenda avisam o paciente (TASK-044)"
Cohesion: 0.14
Nodes (13): Adjudicação final do coordenador, CHECKPOINT — Lembretes R7: ações da clínica na agenda avisam o paciente (TASK-044), Contrato para o R5, Decisões onde a spec silenciou, Deploy (quando o dono autorizar), Estado, Modelos da Meta (documentados, NÃO criados), O que entrou e onde (âncoras estáveis) (+5 more)

### Community 618 - "config.py"
Cohesion: 0.20
Nodes (10): _parse_hhmm(), time, Request/response schemas for the doctor-hub config endpoints.  All domain *sha, Reject unknown weekday keys and overlapping windows within a day., Clinic's physical address (contract v1 §10). Every field optional —     a clini, Parse an "HH:MM" string into a time, raising ValueError if out of range., A single availability window within a day (local clinic time)., TenantAddress (+2 more)

### Community 619 - "test_workers_ns_patching.py"
Cohesion: 0.29
Nodes (8): MonkeyPatch, `workers_ns` must patch a name in every split module that has it, and undo it (T, Every split module owns a distinct `logger`. Undoing a patch must not collapse, test_nested_patches_of_one_name_unwind_in_order(), test_setattr_is_undone_in_every_module(), test_setattr_reaches_every_module_that_has_the_name(), test_undo_gives_each_module_its_own_original_back(), test_unknown_name_is_refused()

### Community 620 - "handoff_notification.py"
Cohesion: 0.21
Nodes (16): activate_human_handoff(), _active_key(), _claim(), _clinic_name(), _console_line(), _current_occurrence(), _ledger_key(), notify_human_handoff() (+8 more)

### Community 621 - "FastAPI"
Cohesion: 0.15
Nodes (14): FastAPI, Admin (SaaS-owner) — extend the global convênio catalog without a code migration, _extract_event_fields(), JSONResponse, Request, Asaas payment webhook endpoint.  POST /webhooks/asaas - receives Pix payment l, Tolerant extraction of (event_id, event_type, payment_id) from an Asaas     web, Fast-ACK Asaas webhook receiver. Auth + processing happen in the worker. (+6 more)

### Community 622 - "Settings"
Cohesion: 0.08
Nodes (14): BaseSettings, UUID, Strongly-typed settings. Values come from the environment or `.env`.      Real, Parse BOT_ALLOWLIST_WA_IDS into a set of digits-only wa_ids.          Meta alw, Parse DEBUG_RESET_TENANT_IDS into a set of tenant UUIDs.          Quotes and b, Parse CORS_ALLOW_ORIGINS into a clean list of origins.          Accepts a JSON, Settings, UUID (+6 more)

### Community 624 - "_first_decision"
Cohesion: 0.24
Nodes (16): Volta a conversa para o menu inicial de botões da clínica. Use quando o     pac, show_main_menu(), _config(), _first_decision(), Bateria opt-in: o modelo REAL escolhe a ferramenta certa dado o estado (MVP Port, test_bare_button_tap_gets_an_open_question_not_a_human(), test_emergency_after_outro_gets_immediate_safety_guidance(), test_explicit_request_for_a_person_hands_off() (+8 more)

### Community 625 - "build"
Cohesion: 0.25
Nodes (7): build(), health(), Any, Request, Liveness probe and build identity.  `/health` is the LIVENESS probe and its re, Liveness probe. Intentionally does not touch Postgres or Redis., Which code is running here, and does the worker agree? (FIX_01 §5.1/§5.2)

### Community 626 - "test_patient_email_pii.py"
Cohesion: 0.54
Nodes (7): db(), _history_seen_by_the_model(), Patient e-mail is masked in real DB history before the model boundary., _seed_portal_conversation(), test_a_different_spelling_of_the_stored_email_is_masked_too(), test_an_email_the_patient_typed_is_masked_even_before_it_is_stored(), test_email_reaches_the_llm_only_as_a_token()

### Community 627 - "calendar.py"
Cohesion: 0.16
Nodes (13): GoogleScopeInsufficientError, is_event_not_found(), _is_invalid_grant(), BaseException, RefreshError, _raise_if_scope_insufficient(), Google Calendar integration for the clinic.  Fase A (single tenant): credentia, Whether `exc` is Google saying "no such event on THIS calendar".      Not an o (+5 more)

### Community 628 - "_Agenda"
Cohesion: 0.12
Nodes (6): _Agenda, _FakeWhatsAppClient, _LogRecorder, MonkeyPatch, The owner's agenda: 14:00 and 14:30 free on NEW_DAY. Records writes and reads., wired()

### Community 629 - "TASK-032 — verificação após deploy, 08/10/2026"
Cohesion: 0.14
Nodes (13): Continuação real necessária, Destinatário de paciente informado pelo dono — 19:33Z, Falha reproduzida, Limpeza comprovada, Prova em produção, Reenvio da simulação solicitado pelo dono — 18:56Z, Resultado e limite, Reteste com o link do dono e consulta temporária (+5 more)

### Community 630 - "AppointmentType"
Cohesion: 0.17
Nodes (9): AppointmentType, clean_requirements(), Trim items, drop blanks, and cap count/length.      ONE rule, shared by every, A bookable reason for a consult, with its own duration.      `service_id` is w, Request/response schemas for the doctor-hub canonical service catalog.  The cl, POST /tenants/me/services.      `name` is the clinic's canonical spelling. The, PATCH /tenants/me/services/{id}. Every field optional (partial update).      R, ServiceCreate (+1 more)

### Community 631 - "test_hub_edit_appointment.py"
Cohesion: 0.26
Nodes (22): The patient taps a button of a card (WhatsApp; a Portal tap is the same handler), set_tenant(), tap(), _iso(), _post(), AsyncClient, POST /appointments/{id}/edit - Editar/Remarcar tells the patient (TASK-032 R7 §1, Google refused the clinic's grant (invalid_grant): 409 `google_reconnect_require (+14 more)

### Community 632 - "test_portal_context_opening.py"
Cohesion: 0.10
Nodes (54): FlowState, _handover_values(), Enum, Conversation model - one ongoing thread between a patient and a clinic., Which deterministic (zero-LLM) flow the conversation is currently in.      IDL, _appointment(), db(), _enter() (+46 more)

### Community 633 - "test_appointment_replacement_router.py"
Cohesion: 0.26
Nodes (11): _in_booking(), The router keeps "Marcar outra" only inside the booking (TASK-032 R3, spec §4.3), test_a_booked_result_never_carries_the_marker(), test_a_booking_step_keeps_the_marker(), test_a_marker_left_on_an_idle_conversation_never_reaches_a_new_booking(), test_a_marker_the_result_names_is_kept_as_named(), test_cancelar_on_the_card_keeps_the_marker_for_the_retry(), test_leaving_the_booking_drops_the_marker() (+3 more)

### Community 646 - "Primeira mensagem de quem tem consulta — mesmo lembrete, mesmos fluxos"
Cohesion: 0.29
Nodes (6): Estado, O que o paciente vê, Onde foi unificado, Primeira mensagem de quem tem consulta — mesmo lembrete, mesmos fluxos, Regras mantidas, Revisão e evidências

### Community 647 - "test_hub_deps.py"
Cohesion: 0.27
Nodes (10): _FakeSession, _make_tenant(), MonkeyPatch, Tests for api/hub/deps.py:get_current_tenant — the doctor-hub auth gate.  Toke, A minimal session stub: session.get(Tenant, id) -> canned tenant or None., test_active_claim_resolves_the_claimed_tenant(), test_active_claim_with_unknown_tenant_is_404(), test_inactive_claim_is_401() (+2 more)

### Community 648 - "ClinicFacts"
Cohesion: 0.20
Nodes (9): _blank_to_none(), ClinicFacts, _drop_blank_items(), FaqItem, Bounded, optional facts about a clinic that the LLM may quote (TASK-025).  Liv, test_blank_text_becomes_none_and_blank_list_items_are_dropped(), test_every_field_is_optional(), test_exactly_at_the_limits_is_accepted() (+1 more)

### Community 649 - "staff_transition"
Cohesion: 0.20
Nodes (12): datetime, UUID, Appointment status transitions — the one observability seam (PROMPT_FIX_16)., True = apply `target`; False = a repeated attended/no_show (a no-op). Pure., The PATCH would contradict the appointment's state (spec 2026-10-09 §1/§3)., staff_transition(), StaffTransitionRefused, test_a_cancelled_appointment_never_comes_back() (+4 more)

### Community 676 - "_seed_tenant"
Cohesion: 0.15
Nodes (13): The Portal's `patient_ref` is an id, not a phone, and is not stored as one., CHECKLIST (WhatsApp non-regression): the whole card leg is Portal-only.      A, CHECKLIST: a verified account enters the normal menu immediately., If we cannot ask, consent is the thing that must still happen., CHECKLIST: the WhatsApp channel never gains an e-mail step.      Greeting then, A WhatsApp conversation carrying one of the new states is not intercepted., _seed_tenant(), test_a_portal_booking_records_no_phone_number() (+5 more)

### Community 677 - "CHECKPOINT — IA v2: prompt e regras (TASK-030 P5)"
Cohesion: 0.20
Nodes (9): 1. O que já existia e foi preservado, 2. A lacuna corrigida, 3. Compatibilidade e orçamento medidos, 4. Validação e revisão, 5. Avaliações reais e limites, 6. Liberação — somente dono/operador, 7. Registro, 8. Integração autorizada — 2026-10-07 (+1 more)

### Community 678 - "E-mail fixo ao médico após confirmar Alterar Dados"
Cohesion: 0.25
Nodes (7): Correções encontradas pela validação ampla, E-mail fixo ao médico após confirmar Alterar Dados, Estado, O médico recebe, Onde e quando, Proteções de envio, Revisão e limites

### Community 680 - "Ações da clínica na agenda avisam o paciente — design (2026-10-09)"
Cohesion: 0.12
Nodes (15): 1. Botões da consulta na agenda (R5), 2. Toda ação avisa o paciente, no canal dele, 3. Contrato (backend → front), 4. Limites deliberados, 5. Decisões do dono de 2026-10-09 (segunda rodada): quem vê o quê, lembrete extra, dados do Editar, 6.1 Conversas do PreCheck nunca aparecem no console, 6.2 Lembrete extra: "N dias antes, às HH:MM", no fuso da clínica, 6.3 Confirmações do dono (2026-10-10, depois do plano escrito) (+7 more)

### Community 681 - "_CapturingLogger"
Cohesion: 0.31
Nodes (3): _CapturingLogger, Records every log call (event name + args/kwargs) instead of printing.      Us, Every captured call flattened to one string, for a substring check.

### Community 682 - "test_booking_draft_continuation.py"
Cohesion: 0.16
Nodes (21): _Agenda, _consents(), _conversation(), db(), _Log, _onboard(), _park(), MonkeyPatch (+13 more)

### Community 683 - "test_booking_draft_resume_questions.py"
Cohesion: 0.17
Nodes (26): _off(), An enriched request must survive the flag-OFF worker and subsequent answers., test_ambiguous_doctor_answer_preserves_preferences_until_an_explicit_choice(), test_flag_off_worker_does_not_lose_the_day_while_asking_the_doctor(), test_flag_off_worker_preserves_explicit_day_and_self(), _Agenda, _conversation(), db() (+18 more)

### Community 684 - "Subagendas no convite e em “também atende” — TASK-054"
Cohesion: 0.40
Nodes (4): Comportamento, Estado, Subagendas no convite e em “também atende” — TASK-054, Validação

### Community 685 - "staff_welcome.py"
Cohesion: 0.24
Nodes (8): UUID, Live first-access delivery checks; no credentials returned or logged., staff_delivery_request(), open_staff_welcome(), Passwords and recipient data persist only as an encrypted, expiring job envelope, seal_staff_welcome(), StaffWelcomeEnvelope, send_staff_welcome_email()

### Community 686 - "test_appointment_change_notifications.py"
Cohesion: 0.17
Nodes (17): What the after-commit step and the patient's confirmation need to know., ReplacedAppointment, _change_jobs(), _Pool, A7: the PATIENT cancelled or moved - the clinic, the doctor and the Portal patie, A staff cancel/edit already tells the patient (TASK-044); announcing it here too, The arq pool: records every enqueue., _reply() (+9 more)

### Community 687 - "test_clinic_context_backend.py"
Cohesion: 0.27
Nodes (17): compute_completeness(), SimpleNamespace, ClinicFacts schema, `get_service_info` tool and the completeness checklist (TASK, _status(), _svc(), _tenant(), test_a_faq_that_is_not_a_list_neither_counts_nor_breaks(), test_a_fully_filled_clinic_scores_one_hundred_with_no_hints() (+9 more)

### Community 688 - "CHECKPOINT — Lembretes R3: abertura do chat e caminho do Cancelar (TASK-032)"
Cohesion: 0.25
Nodes (7): 1. Estado, 2. O que entrou onde, 3. Rótulos dos botões (pedido do dono, 2026-10-07), 4. Regras fixadas (Review Focus do plano → teste que prende), 5. Decisões, 6. Pendências, CHECKPOINT — Lembretes R3: abertura do chat e caminho do Cancelar (TASK-032)

### Community 689 - "draft_from_record"
Cohesion: 0.13
Nodes (19): draft_from_record(), Parse the six keys (unknown keys ignored). ValueError on anything malformed., The parked draft, or None when absent, corrupt or older than the TTL.      `no, _text(), test_run_agent_maps_a_v2_booking_draft_to_its_sentinel(), The AI booking draft v2: wire format, v1 compatibility and the parked record (TA, test_a_corrupt_payload_raises_value_error(), test_a_missing_or_corrupt_record_is_none() (+11 more)

### Community 690 - "test_reminder_v2_wiring.py"
Cohesion: 0.25
Nodes (13): _appointment_id(), _booking(), _flow(), Every booking / move / cancel path calls the reminder hooks (TASK-032 R2, spec §, _reply(), _tenant(), test_a_cancel_button_cancels_the_reminders(), test_a_cancel_button_with_the_switch_off_touches_nothing() (+5 more)

### Community 691 - "TASK-030 — prova real da captura e das datas após deploy"
Cohesion: 0.33
Nodes (5): Casos, Identidade implantada, Limites e preservação, Resultado, TASK-030 — prova real da captura e das datas após deploy

### Community 692 - "TASK-030 — reteste após deploy do P4, 2026-10-07"
Cohesion: 0.33
Nodes (5): Casos, Identidade implantada, Limites e preservação, Resultado, TASK-030 — reteste após deploy do P4, 2026-10-07

### Community 693 - "datetime"
Cohesion: 0.04
Nodes (30): datetime, DefaultExecutionContext, One planned (or shown) reminder of one appointment - TASK-032, spec 4.1.  The, BookingHold - the slot a Brain-Message visitor holds while they prove their addr, ConsentEvent model - LGPD consent/legal-basis audit trail (groundwork).  One r, Per-conversation PII token map - the re-identification key for one thread.  Mi, _external_id_default(), Patient model - a person messaging a clinic on one of its channels. (+22 more)

### Community 694 - "test_flow_replaces_column.py"
Cohesion: 0.47
Nodes (4): _migration(), conversations.flow_replaces_appointment_id (TASK-032 R3, spec §4.3 "Marcar outra, test_the_migration_adds_and_drops_a_nullable_column_on_sqlite(), test_the_migration_is_the_single_head_on_top_of_the_previous_one()

### Community 695 - "internal_provisioning.py"
Cohesion: 0.09
Nodes (35): activate(), asaas_connection(), attach_professional(), config_status(), internal_professional_roster(), notifications_email(), provision_tenant(), AsyncSession (+27 more)

### Community 696 - "extract_action_button"
Cohesion: 0.36
Nodes (8): extract_action_button(), Decode a reminder action-button reply into (action, appointment_id).      Two, _interactive_msg(), _template_button_msg(), test_extract_action_button_both_carriers_agree(), test_extract_action_button_malformed_uuid_returns_none(), test_extract_action_button_no_button_at_all_returns_none(), test_extract_action_button_unknown_prefix_returns_none()

### Community 697 - "viewer_from_claim"
Cohesion: 0.29
Nodes (6): UUID, Who may see which appointments in the hub agenda (TASK-044 R7, spec 2026-10-09 §, Whether an appointment owned by `professional_id` exists for this viewer., brain-api's answer -> the viewer. `agenda_scope` None = brain-api did not say., viewer_from_claim(), test_brain_apis_answer_becomes_the_viewer()

### Community 698 - "test_hub_events_visibility.py"
Cohesion: 0.34
Nodes (14): _by_id(), _clinic(), _events(), _google(), AsyncClient, GET /events: who sees what, "só os meus" and the Editar pre-fill (TASK-044 R7, §, The clinic's Google calendar returns these events (titles carry patient names)., Dra. Ana's WhatsApp patient (evt-ana), Dr. Beto's Portal patient (evt-beto), an (+6 more)

### Community 699 - "test_availability.py"
Cohesion: 0.13
Nodes (20): _at_day_step(), _Calendar, _Gate, _held(), _Log, date, datetime, Free time on one agenda minus held slots: one definition for every reader (TASK- (+12 more)

### Community 701 - "test_handback_log.py"
Cohesion: 0.20
Nodes (12): log(), _LogRecorder, _only_event(), MonkeyPatch, The hand-back event: one shape, a closed vocabulary, no values (TASK-030 P1)., Records every structlog call (`caplog` is empty for structlog in this suite)., test_a_value_outside_the_vocabulary_is_logged_as_other_never_as_itself(), test_landing_of_names_the_step_or_the_pseudo_step() (+4 more)

### Community 702 - "_route_inbound_turn"
Cohesion: 0.04
Nodes (64): ChannelPolicy, policy_for(), _portal_asks_name(), Any, Channel policy: what differs between the channels, as data (TASK-024).  Before, The policy of `channel`; WhatsApp's for anything unknown or None., _whatsapp_asks_name(), debug_reset_allowed() (+56 more)

### Community 703 - "brain_onboarding.py"
Cohesion: 0.26
Nodes (12): _item_from_dict(), list_onboarding_tenants(), OnboardingTenant, _parse_dt(), post_onboarding_event(), datetime, UUID, Onboarding cron -> brain-api client (cross-service contract v1 §4 endpoints 7/8) (+4 more)

### Community 704 - "pending_identity.py"
Cohesion: 0.04
Nodes (72): CalendarHealthStatus, Per-tenant Google Calendar reachability, as the admin table should show it., _booking_gate_details(), booking_gate_reprompt_body(), cancel_pending_code(), claim_email(), ClaimOutcome, ClaimResult (+64 more)

### Community 706 - "get_agenda_viewer"
Cohesion: 0.14
Nodes (18): _bearer_token(), get_agenda_viewer(), AsyncSession, Request, Shared FastAPI dependencies for the doctor hub.  `get_current_tenant` is the s, Extract the token from an Authorization header, tolerating a missing     'Beare, Load the claimed tenant, or the only tenant when the claim has no id.      A r, Who is looking at the agenda (TASK-044 R7, spec 2026-10-09 §5.A).      Uses th (+10 more)

### Community 707 - "_Handoff"
Cohesion: 0.08
Nodes (20): _Handoff, log(), _LogRecorder, _patch_session_factory(), precheck_number(), MonkeyPatch, Configure the platform PreCheck number for every test in this file.      `get_, Stands in for `request_precheck_handoff`: records asks, replays one outcome. (+12 more)

### Community 708 - "encrypt"
Cohesion: 0.22
Nodes (9): downgrade(), upgrade(), encrypt(), EncryptionError, _fernet(), Symmetric encryption for tenant secrets at rest (Fernet / AES-128-CBC + HMAC)., Raised when encryption/decryption cannot be performed., Build the process-wide Fernet from settings.ENCRYPTION_KEY.      Cached so we (+1 more)

### Community 710 - "simulation.py"
Cohesion: 0.22
Nodes (10): AsyncSession, POST /tenants/me/simulation/turn - one patient turn of the booking flow, simulat, What the manager is editing and has NOT saved. Same shape as the Save envelope., The conversation position. Every field is validated: the client is not trusted., simulation_turn(), SimulationDraft, SimulationState, SimulationTurnIn (+2 more)

### Community 711 - "test_confirmation_warning_email.py"
Cohesion: 0.20
Nodes (3): MonkeyPatch, The clinic warning e-mail and the Portal nudge template (TASK-032 R4, spec 4.4)., smtp()

### Community 712 - "test_manage_v2_handback.py"
Cohesion: 0.30
Nodes (23): db(), _events(), _local(), _manage(), date, datetime, manage_existing_appointment v2 through the worker (TASK-030 P3, spec §4.5).  T, _ref() (+15 more)

### Community 713 - "TenantRuntimeConfig"
Cohesion: 0.06
Nodes (42): CalendarCredentialStatus, _address_line(), _clinic_fact_lines(), _format_appointment_types(), _format_business_hours(), _format_clinic_facts(), _format_conversation_state(), _format_post_consult_knowledge() (+34 more)

### Community 714 - "test_manage_request.py"
Cohesion: 0.13
Nodes (21): _optional_text(), parse_appointment_ref(), The bare v1 action or the v2 JSON. ValueError on anything malformed., "AAAA-MM-DD HH:MM" ("T" also accepted) as a NAIVE clinic-local datetime., _appt(), _Log, datetime, Manage request v2: wire format, the appointment reference and the tool (TASK-030 (+13 more)

### Community 715 - "decide_reminder_opening"
Cohesion: 0.25
Nodes (14): _current_version_rows(), decide_reminder_opening(), ensure_chat_reminder(), nearest_live_appointment(), OpeningDecision, AsyncSession, datetime, UUID (+6 more)

### Community 716 - "strip_decoration"
Cohesion: 0.18
Nodes (11): Undo one leading `decorate*` prefix - the matcher's half of the pair.      Cal, strip_decoration(), _norm(), The catalog-resolution key, decoration-insensitive.      A `svc|` row now rend, _label_match_body(), True when `body` equals `label` or its 20-char button truncation.      Normali, test_the_greeting_cards_matcher_in_the_worker_accepts_all_three_forms(), test_strip_decoration_is_the_inverse_of_decorate() (+3 more)

### Community 717 - "TenantConfigUpdate"
Cohesion: 0.18
Nodes (5): Any, PUT body. Every field is optional — only provided fields are updated.      `bu, Light shape check for the deterministic-flow config blob., The greeting always ships with fixed action buttons attached.          Since t, TenantConfigUpdate

### Community 718 - "TASK-041 — edição pela IA sem perder a consulta"
Cohesion: 0.25
Nodes (7): Causa confirmada e limite da investigação, Comandos e baseline, Desenho implementado (âncoras estáveis), Efeito para o paciente e para a clínica, Evidência RED → GREEN e revisão, TASK-041 — edição pela IA sem perder a consulta, Transcript e checklist da prova

### Community 720 - "apply_appointment_edit"
Cohesion: 0.12
Nodes (18): appointment_email_version(), Opaque revision of email-relevant booking data; never export their values to a j, Store an edit notice in the caller's appointment transaction, never commit here., record_professional_edit(), AppliedEdit, apply_appointment_edit(), compensate_appointment_edit(), _finish_edit() (+10 more)

### Community 721 - "MonkeyPatch"
Cohesion: 0.18
Nodes (11): _captured_bubbles(), _patch_session_factory(), MonkeyPatch, Replace the worker's calendar-resolution seam; no Google, no credentials., _stub_calendar(), test_run_agent_maps_a_v2_manage_request_to_its_sentinel(), test_run_agent_maps_manage_appointment_to_sentinel(), test_run_agent_maps_select_professional_to_sentinel() (+3 more)

### Community 722 - "context_completeness.py"
Cohesion: 0.38
Nodes (9): Completeness, CompletenessItem, _filled(), _guidance_gap(), _has_address(), _has_guidance(), _has_hours(), Any (+1 more)

### Community 723 - "build_conversation_state"
Cohesion: 0.09
Nodes (37): build_conversation_state(), _doctor_label(), _is_internal_marker(), _pra_quem_line(), Turn-scoped "ESTADO DA CONVERSA" block for the agent's system prompt.  The LLM, `flow_selected_type` holds `__attendee_next_*__` during the pra-quem steps: an e, "Dr. Fulano (Cardiologia)" - the specialty lets the model tell, and say, when no, The state block, or None when there is no conversation snapshot. (+29 more)

### Community 724 - "CHECKPOINT — Lembretes R4: avisos, liberar horário e mensagem (TASK-032)"
Cohesion: 0.29
Nodes (6): CHECKPOINT — Lembretes R4: avisos, liberar horário e mensagem (TASK-032), Decisões tomadas sem consulta, Desvios na implementação (registro), O que entrou onde, Pendências, Regras que não são óbvias

### Community 725 - "next_retry_time"
Cohesion: 0.20
Nodes (14): days_elapsed(), _next_checkpoint_day(), next_retry_time(), datetime, Pure cadence/due-date math for the onboarding nudge cron (contract v1 §11/§12)., Whether `now_local`'s wall-clock time falls inside `window`     ("HH:MM-HH:MM",, Fractional days between `anchor` and `now` (may be negative).      Both must b, Smallest checkpoint (day-offset from anchor) STRICTLY greater than     `elapsed (+6 more)

### Community 727 - "turn_safety_net.py"
Cohesion: 0.22
Nodes (9): fallback_allowed(), hold_intro(), _key(), note_send(), The patient never goes unanswered - and the guarantee cannot be turned into a we, Hold words to go out in front of this turn's next card. No-op outside a turn., Record that one message really left. No-op outside a turn (reminders, staff)., Whether this conversation may receive another safety-net apology now.      Fai (+1 more)

### Community 728 - "Lembretes R2 — Motor de lembretes Implementation Plan"
Cohesion: 0.09
Nodes (22): Consumes from R1 (verified against `docs/superpowers/plans/2026-10-03-lembretes-r1-fundacao.md`), Deploy e liberação (only on the owner's explicit request, each step separately), File Structure, Global Constraints, Lembretes R2 — Motor de lembretes Implementation Plan, Produces for R3 / R4 / R5, Review Focus, Self-review (done while writing; kept for the executor) (+14 more)

### Community 729 - "4. Desenho"
Cohesion: 0.09
Nodes (22): 10. Referências, 1. Objetivo e intenção, 2. Decisões do dono (2026-10-02), 3. Estado atual (fatos do mapa que sustentam o design), 4.10 Prompt e regras, 4.11 Liberação, 4.1 Rascunho v2 (o que a IA pode preencher), 4.2 O resolvedor (um só lugar que decide onde o paciente cai) (+14 more)

### Community 730 - "_FakeAsyncClient"
Cohesion: 0.20
Nodes (4): _FakeAsyncClient, _FakeResponse, Exception, Records init kwargs + posted requests; returns a canned response or raises.

### Community 731 - "_Queue"
Cohesion: 0.20
Nodes (7): api(), _ExplodingWhatsAppClient, MonkeyPatch, _Queue, The real app on the in-memory DB, a recording queue, and a hub token whose, Stands in for both the arq pool (API) and ctx["redis"] (worker)., _wire()

### Community 732 - "_CalendarPerId"
Cohesion: 0.47
Nodes (3): _CalendarPerId, Stands in for `CalendarService` in ai/tools.py: each calendar id has its own age, real_roster()

### Community 733 - "_notify_portal"
Cohesion: 0.39
Nodes (8): notify_cancelled_patient(), _notify_portal(), notify_released_patient(), AsyncSession, datetime, Telling a patient the clinic freed their slot (TASK-032 R4, spec 4.4).  Two ch, The patient notice of POST /cancel (TASK-032 R7 §2). Never raises.      * Port, Tell the patient; returns one `NOTICE_*` code. Never raises.

### Community 734 - "CHECKPOINT — "Meus pacientes" no console (TASK-046 R8, parte A)"
Cohesion: 0.33
Nodes (5): CHECKPOINT — "Meus pacientes" no console (TASK-046 R8, parte A), Deploy (não executado), Estado, Fora do escopo, O que mudou

### Community 735 - "Retenção de visitas vazias + espera de código de conta conhecida — TASK-042 (lado secretarIA)"
Cohesion: 0.33
Nodes (5): 1. Descarte de visita vazia (API), 2. Espera de código de conta conhecida, antes do consentimento (worker), Estado, Retenção de visitas vazias + espera de código de conta conhecida — TASK-042 (lado secretarIA), Validação

### Community 736 - "TASK-041 — prova real da edição no Portal, 09/10/2026"
Cohesion: 0.33
Nodes (5): Ambiente e identidade, Evidências sanitizadas, Preservação e limites, Roteiro observado — 03:46–03:52 UTC (00:46–00:52 local), TASK-041 — prova real da edição no Portal, 09/10/2026

### Community 737 - "test_reminder_r3_continuations.py"
Cohesion: 0.42
Nodes (8): calendar(), _future(), datetime, Direct cancellation and legacy 'Agendar Outra' edit compatibility after R6., _tap(), test_legacy_book_another_edits_the_existing_booking_instead(), test_legacy_book_another_with_paid_deposit_hides_price_changes(), test_yes_cancel_cancels_closes_the_reminders_and_asks_why()

### Community 738 - "CHECKPOINT — remediação lembretes R6 (e-mail ao médico + entrada pelo link)"
Cohesion: 0.40
Nodes (4): Causas tratadas, CHECKPOINT — remediação lembretes R6 (e-mail ao médico + entrada pelo link), Ordem de publicação (obrigatória), Pendências da revisão (results/remediation-review.md)

### Community 739 - "test_back_leaves_for_the_menu_without_touching_the_appointment"
Cohesion: 0.20
Nodes (10): _bm_tap(), One TAP on the Portal, through the real validation the channel applies.      G, Never park a patient on a prompt for a code that was not sent., CHECKLIST (§4): `identity_change_email` -> AWAITING_EMAIL + the ask.      And, CHECKLIST (§4): `identity_back` -> IDLE/MENU and the menu is shown., The tap id is the patient's browser's word for it, not Meta's.      A conversa, test_a_forged_card_id_is_ignored_and_routed_as_text(), test_a_resend_that_cannot_be_mailed_frees_the_conversation() (+2 more)

### Community 740 - "test_scoped_help.py"
Cohesion: 0.33
Nodes (11): _professional_help_prompt(), _service_help_prompt(), test_nao_sei_uses_catalog_then_administrative_choice(), _professionals(), Tests for the scoped-help LLM nodes (ai/scoped_help.py) - no network.  The rou, Two nodes, two prompts - never one reused question for both scopes., _services(), test_helpers_restrict_clarification_to_administrative_catalog_choices() (+3 more)

### Community 741 - "Lembretes R4 — Avisos por e-mail, estado vermelho, liberar horário e mensagem ao paciente (API) Implementation Plan"
Cohesion: 0.10
Nodes (20): Consumes (verified against the already-written plans), Decisions taken where the spec is silent (flag to the owner/Orchestrator), Deploy e liberação, File Structure, Global Constraints, Lembretes R4 — Avisos por e-mail, estado vermelho, liberar horário e mensagem ao paciente (API) Implementation Plan, Produces for R5 (front) — exact wire shapes, Review Focus (+12 more)

### Community 742 - "receive_webhook"
Cohesion: 0.21
Nodes (11): _filter_new_event_ids(), Request, Response, WhatsApp webhook endpoints.  GET  /webhook  - Meta verification handshake (ech, Meta verification handshake.      Meta calls this once when the webhook is con, Return the subset of `event_ids` not yet in `processed_events`.      Fail-open, Receive a webhook event.      GOLDEN RULE: return 200 in well under 5 seconds., receive_webhook() (+3 more)

### Community 743 - "_FakeArqPool"
Cohesion: 0.33
Nodes (4): api(), _FakeArqPool, Records what the endpoint enqueued, so a test can run it as the worker would., The real ASGI app, on the in-memory DB, with a recording queue.

### Community 746 - "Release R5 / R7 / R8 — 2026-10-10 (TASK-047)"
Cohesion: 0.40
Nodes (4): Deploy manual, Prova em produção pendente, Release R5 / R7 / R8 — 2026-10-10 (TASK-047), Validação do candidato

### Community 747 - "Para o P5 (o que a IA passa a ver — texto e nomes exatos)"
Cohesion: 0.10
Nodes (19): Cobertura da spec (P4), Decisões deste plano (mudar só se o dono discordar), Deploy e liberação, File Structure, Global Constraints, IA entra em qualquer etapa — P4: `get_availability` (só horários livres), ferramentas de agenda cegas e o conjunto v2 — Implementation Plan, Interfaces, Para o P5 (o que a IA passa a ver — texto e nomes exatos) (+11 more)

### Community 748 - "Lembretes R3 — Primeira mensagem no chat e caminho do Cancelar Implementation Plan"
Cohesion: 0.10
Nodes (19): Deploy e liberação, File Structure, Global Constraints, Interfaces → Produces (R4/R5 and later tasks rely on these exact names), Lembretes R3 — Primeira mensagem no chat e caminho do Cancelar Implementation Plan, Review Focus, Self-review (done while writing; kept for the reviewer), Task 10: The opening in the turn — choose it, send it, answer the message, suppress the menu (+11 more)

### Community 750 - "_FakeWhatsAppClient"
Cohesion: 0.22
Nodes (4): _fake_get_waba_token(), _fakes(), _FakeWhatsAppClient, Records constructed instances + sends; installed in place of the real client.

### Community 751 - "_on_inbound"
Cohesion: 0.36
Nodes (7): dtime, _is_outside_business_hours(), _on_inbound(), _parse_hhmm(), datetime, human_backup_24_7 plugin: hand inbound messages to a human outside business hour, True when `now` falls outside every configured window for today.      Empty/un

### Community 752 - "_apply_deposit_awareness"
Cohesion: 0.17
Nodes (16): _apply_deposit_awareness(), _at_reschedule_limit(), _hours_until_start(), _pix_retention_warning_line(), datetime, deposit - split out of workers/tasks.py (TASK-023)., The keep-or-cancel message sent once an appointment's deposit has hit     `pix_, Fold Pix-deposit awareness into a manage-flow `FlowRouterResult`     BEFORE it (+8 more)

### Community 753 - "test_draft_wait_questions.py"
Cohesion: 0.29
Nodes (12): _at(), _collecting(), _doctor_snapshot(), The AI draft waits on the convênio, doctor and service questions too (TASK-030 P, test_answer_preserves_terminal_or_failed_booking_continuation(), test_free_text_on_a_question_keeps_the_draft_for_the_llm(), test_outro_convenio_keeps_waiting_for_the_typed_name(), test_the_convenio_answer_asks_the_worker_to_resume() (+4 more)

### Community 754 - "setup_logging"
Cohesion: 0.17
Nodes (14): _audit(), _classify(), Counts, main(), READ-ONLY audit: NULL versus EMPTY on every professional's own config.  `Profe, Pure tallies. Nothing here can hold an id, a name or a config value., Per-tenant counts, numbered positionally so no tenant id is ever printed., TenantBucket (+6 more)

### Community 755 - "test_edit_day_picker.py"
Cohesion: 0.31
Nodes (8): _carrier(), _in_edit(), The day picker's anchor, the edit branch and the draft carry (TASK-032 R6, spec, test_a_far_anchor_moves_the_scan_window_to_it(), test_an_ai_detour_inside_the_edit_keeps_the_draft(), test_the_draft_is_carried_only_while_the_edit_continues(), test_the_edit_branch_carries_the_appointment_and_the_draft(), test_with_an_anchor_the_nearest_days_come_first_and_are_shown_in_order()

### Community 756 - "AST"
Cohesion: 0.43
Nodes (6): AST, _is_channel(), Counter, Ratchet: neutral worker code must not branch on the channel (TASK-024).  Scans, _scan(), test_no_new_channel_branches_in_neutral_code()

### Community 757 - "_CalendarWithBusy"
Cohesion: 0.33
Nodes (4): _CalendarWithBusy, check_availability hands back real Google Calendar event titles., Devolve a forma crua do Google: id + summary + intervalo., test_tool_results_are_masked_before_re_entering_the_model()

### Community 758 - "_ExplodingWhatsAppClient"
Cohesion: 0.50
Nodes (3): _ExplodingWhatsAppClient, Any use at all fails the test. Nothing on this path may reach Meta., _wire()

### Community 761 - "SlotsBubble"
Cohesion: 0.06
Nodes (59): _print_bubbles(), ButtonBubble, _clean(), _finalise(), parse(), _parse_slot_rows(), _pop_preceding_text_for(), Bubble (+51 more)

### Community 762 - "File Structure"
Cohesion: 0.11
Nodes (17): Cobertura da spec, Decisões deste plano (mudar só se o dono discordar), Deploy e liberação, File Structure, Global Constraints, P1 — Registro de hand-backs Implementation Plan, Review Focus, Task 1: `handback_log` — o evento, o vocabulário e `landing_of` (+9 more)

### Community 763 - "File Structure"
Cohesion: 0.11
Nodes (17): Cobertura da spec (P3), Decisões deste plano (mudar só se o dono discordar), Deploy e liberação, File Structure, Global Constraints, IA entra em qualquer etapa — P3: confirmação expressa, rascunho que espera qualquer pergunta e consultas já marcadas — Implementation Plan, Interfaces (o que P4/P5 consomem — nomes exatos), Review Focus (+9 more)

### Community 764 - "Interfaces"
Cohesion: 0.11
Nodes (17): Cobertura da spec (P5), Decisões deste plano (mudar só se o dono discordar), Deploy e liberação, File Structure, Global Constraints, IA entra em qualquer etapa — P5: prompt e regras novos + avaliações com IA real — Implementation Plan, Interfaces, Review Focus (+9 more)

### Community 765 - "Lembretes R1 — Fundação Implementation Plan"
Cohesion: 0.11
Nodes (17): Decisions taken where the spec is silent (flag to the owner/Orchestrator), Deploy e liberação, File Structure, Global Constraints, Interfaces → Produces (R2-R5 depend on these exact names), Lembretes R1 — Fundação Implementation Plan, Review Focus, Spec coverage (self-review) (+9 more)

### Community 766 - "run_post_booking_hooks"
Cohesion: 0.38
Nodes (6): enqueue_post_booking_hooks(), UUID, post_booking dispatch: react to a freshly committed appointment, off the hot pat, Fire-and-forget enqueue of `run_post_booking_hooks`. Never raises.      `redis, arq job: run every entitled plugin's `post_booking` hook for one appointment., run_post_booking_hooks()

### Community 767 - "datetime"
Cohesion: 0.40
Nodes (5): datetime, Brazil abolished DST in 2019: the UTC offset must be identical across     the y, Passing tz-aware datetimes in America/Sao_Paulo vs UTC must yield the     exact, test_america_sao_paulo_has_no_dst_offset_shift(), test_next_retry_time_identical_across_utc_and_local_tz_labels()

### Community 768 - "_handback_names"
Cohesion: 0.29
Nodes (7): _handback_names(), First lock, and the reason the flow never has to guess an agenda: on     these, test_a_multi_professional_tenant_is_not(), test_a_tenant_level_tenant_is_offered_the_guided_booking_tool(), test_booking_draft_is_a_protocol_sentinel_and_tool_on_every_topology(), test_human_handoff_is_protocol_sentinel_and_available_tool(), test_plugin_tools_are_preserved_alongside_the_handbacks()

### Community 769 - "_WhatsApp"
Cohesion: 0.29
Nodes (3): Stands in for `WhatsAppClient`, as both the class and the client.      `for_te, Autouse so no test can reach the real Cloud API by forgetting to ask., _WhatsApp

### Community 770 - "test_a_returning_portal_patient_gets_the_resume_offer_on_the_portal"
Cohesion: 0.50
Nodes (4): _age_conversation(), Push every message `hours` into the past: `last_activity_at` is     max(Message, The generic "quer continuar?" leaves by Brain-Message, not by WhatsApp.      L, test_a_returning_portal_patient_gets_the_resume_offer_on_the_portal()

### Community 771 - "_LogRecorder"
Cohesion: 0.29
Nodes (3): _LogRecorder, Stands in for the module's structlog logger, recording every call.      Neithe, Everything that was logged, flattened — for leak assertions.

### Community 772 - "test_agent.py"
Cohesion: 0.40
Nodes (5): main(), _print_tool_calls(), BaseMessage, Terminal agent loop for SecretarIA (Fase A smoke test).  Uses the SAME LangGra, Surface tool invocations so the dev can see what the LLM decided.

### Community 774 - "Lembretes R5 — Front da agenda (cores, ✓/✓✓, gaveta com ações por horário, avisos ao paciente, Editar/Remarcar, legenda) e configuração "Lembretes e avisos" Implementation Plan"
Cohesion: 0.09
Nodes (22): Change log 2026-10-09 (owner decisions — spec `2026-10-09-acoes-clinica-avisos-paciente-design.md`), Change log 2026-10-09, second round (owner decisions — spec §5 and the "create on his own agenda" decision; R7 "Produces for R5" as updated the same day), Consumes — backend contract (exact shapes, and what is assumed), Decisions taken where the spec is silent, Deploy and live proof (not part of the tasks), File Structure, Global Constraints, Lembretes R5 — Front da agenda (cores, ✓/✓✓, gaveta com ações por horário, avisos ao paciente, Editar/Remarcar, legenda) e configuração "Lembretes e avisos" Implementation Plan (+14 more)

### Community 775 - "Lembretes, primeira mensagem e estado de confirmação da consulta — Design"
Cohesion: 0.12
Nodes (16): 10. Referências, 1. Objetivo e intenção, 2. Decisões do dono (2026-10-03), 3. Estado atual (fatos do mapa que sustentam o design), 4.1 Cronograma de lembretes (dados), 4.2 O motor de lembretes, 4.3 A mensagem como primeira mensagem do chat, 4.4 Avisos à clínica, estado vermelho e ações na agenda (+8 more)

### Community 776 - "FakeCalendar"
Cohesion: 0.22
Nodes (11): _apply(), _demo(), FakeCalendar, main(), SimpleNamespace, Terminal harness for the deterministic flow router (cancel/reschedule).  Unlik, Mirror the worker's _apply_flow_result against in-memory state., Scripted run of the full cancel + reschedule flows (dynamic dates). (+3 more)

### Community 779 - "get_context_completeness"
Cohesion: 0.47
Nodes (5): CompletenessItemRead, CompletenessRead, get_context_completeness(), AsyncSession, GET /tenants/me/context-completeness - what is still missing in the clinic's con

### Community 782 - "_Mailer"
Cohesion: 0.33
Nodes (4): _Mailer, The job's collaborators: DB, SMTP, brain-api, entitlements, the agenda URL., Records every send; `fail` names templates that fail transiently ONCE., world()

### Community 783 - "IA entra em qualquer etapa — P2a: disponibilidade, rascunho v2, `flow_draft` e o resolvedor — Implementation Plan"
Cohesion: 0.12
Nodes (15): Cobertura da spec (P2a), Decisões deste plano (mudar só se o dono discordar), Deploy e liberação, Estado de execução — 2026-10-05, File Structure, Global Constraints, IA entra em qualquer etapa — P2a: disponibilidade, rascunho v2, `flow_draft` e o resolvedor — Implementation Plan, Interfaces (o que P2b, P3, P4 e P5 consomem — nomes exatos) (+7 more)

### Community 787 - "test_resend_asks_for_a_new_code_and_re_emits_the_same_card"
Cohesion: 0.33
Nodes (6): _last_card(), The `Message.interactive` of the newest outbound row, as stored., CHECKLIST (§3 + §4): the body names the masked inbox, the card has 3 buttons., CHECKLIST (§4): `identity_resend` -> request_code again, same card., test_resend_asks_for_a_new_code_and_re_emits_the_same_card(), test_the_code_notice_is_a_card_with_three_ids_and_the_mask()

### Community 790 - "_fake_tool"
Cohesion: 0.33
Nodes (6): _fake_tool(), A stand-in for create_event: records what actually reached it., The doctor's real calendar must never receive a token.      create_event's own, Dev terminal / direct callers behave exactly as before this feature., test_tool_arguments_are_rehydrated_before_they_leave(), test_tools_are_untouched_outside_a_pseudonymized_turn()

### Community 791 - "_config"
Cohesion: 0.60
Nodes (6): _call(), _config(), test_tool_lists_the_valid_names_when_the_service_is_unknown(), test_tool_never_sees_another_tenants_catalog(), test_tool_returns_the_guidance_of_the_named_service(), _type()

### Community 792 - "_report"
Cohesion: 0.70
Nodes (4): main(), UUID, READ-ONLY report: future appointments saved without a professional owner.  Boo, _report()

### Community 794 - "main"
Cohesion: 0.67
Nodes (3): _load_config(), main(), Terminal chat with the agent DRESSED by a real tenant's hub config.  Unlike sc

### Community 795 - ".resolve"
Cohesion: 0.50
Nodes (3): The plan one appointment points at, or None.          Same precedence as `depo, test_unit_booking_adds_insurance_and_keeps_extra_description(), calendars()

### Community 796 - "parse_code"
Cohesion: 0.50
Nodes (4): parse_code(), The 6-digit code in `body`, or None if the message is not one.      Pure. A No, A None is what lets a patient say something else instead of the code., test_parse_code_accepts_six_digits_and_nothing_else()

### Community 798 - "parse_email"
Cohesion: 0.50
Nodes (4): parse_email(), The address in `body`, normalized — or None if it does not look like one., A sentence CONTAINING an address is not an answer to "qual é o seu e-mail?"., test_parse_email_accepts_an_address_and_only_an_address()

### Community 802 - "IA entra em qualquer etapa — P2b: hand-backs pelo resolvedor, "pra quem" sempre perguntado, ferramenta v2, interruptor e estado da conversa — Implementation Plan"
Cohesion: 0.15
Nodes (12): Cobertura da spec (P2b), Decisões deste plano (mudar só se o dono discordar), Deploy e liberação, File Structure, Global Constraints, IA entra em qualquer etapa — P2b: hand-backs pelo resolvedor, "pra quem" sempre perguntado, ferramenta v2, interruptor e estado da conversa — Implementation Plan, Review Focus, Task B1: `set_booking_draft` pelo resolvedor — "pra quem" sempre perguntado, o interruptor e as correções do `selection_only` (+4 more)

### Community 803 - "TenantConfigRead"
Cohesion: 0.13
Nodes (15): get_config(), AsyncSession, Doctor hub — tenant configuration endpoints (authenticated).  GET  /tenants/me, Save tenant config and one professional's config in ONE transaction.      Orde, LEGACY single-scope save. Superseded by PUT /tenants/me/configuration.      Be, update_config(), update_configuration(), GET/PUT response. Never includes secrets — only a `calendar_connected` flag. (+7 more)

### Community 804 - "booking_details_text"
Cohesion: 0.13
Nodes (26): _assemble(), booking_details_text(), _clean(), clinic_address_line(), _identity_lines(), price_text(), Any, The booking details a patient reads before confirming an AI-landed booking (TASK (+18 more)

### Community 806 - "handback_log.py"
Cohesion: 0.23
Nodes (12): _dropped(), _id(), landing_of(), log_handback_entered(), _names(), UUID, One structured event per AI hand-back: where did the patient land?  The agent, `value` when it is in the closed vocabulary, "other" when it is not, None for No (+4 more)

### Community 817 - "Antes de começar (uma vez)"
Cohesion: 0.17
Nodes (11): Antes de começar (uma vez), Fechamento (fora das tasks), File Structure, Global Constraints, Portal: rolar pela última mensagem e podar cópias locais (P6) — Implementation Plan, Review Focus, Task 1: Demo `?janela=1` — conversa que serve só as 50 mais novas, e a prova do defeito no navegador, Task 2: O poll da secretarIA poda as cópias locais (+3 more)

### Community 829 - "test_confirmation_card.py"
Cohesion: 0.32
Nodes (10): _at_slot(), The confirmation card names the doctor on switched-on clinics (TASK-030 P3, spec, _switched(), test_a_clinic_without_professionals_has_no_doctor_line(), test_a_single_professional_clinic_names_its_only_doctor(), test_the_card_body_never_exceeds_the_interactive_limit(), test_the_doctor_comes_before_the_patient_line(), test_the_resumed_card_reads_like_the_tapped_one() (+2 more)

### Community 831 - "CHECKPOINT — Contexto da clínica que a LLM enxerga (TASK-025, parte backend)"
Cohesion: 0.17
Nodes (11): 10. Rodada de 2026-10-07 (direto na `main`, local, sem push), 1. O que mudou para o paciente, 2. Onde está cada peça (âncoras estáveis), 3. Contrato do hub (aditivo, congelado em SPEC §3 — o frontend implementa contra ele), 4. O bloco "SOBRE A CLÍNICA" e o orçamento, 5. Validação (2026-10-04), 6. Ordem de deploy (histórica) e reversão, 7. Pendências (+3 more)

### Community 832 - "Lembretes R1 — Fundação (TASK-032)"
Cohesion: 0.09
Nodes (20): Ajustes de execução, Correções da revisão independente (prevalecem sobre o exemplo de reutilização do plano), Estado, Follow-up: deploy informado pelo dono e testes reais, Integração em `main` (follow-up autorizado), Lembretes R1 — Fundação (TASK-032), O que entrou onde, Ordem de liberação (não autorizada ao agente) (+12 more)

### Community 841 - "test_flow_draft_column.py"
Cohesion: 0.31
Nodes (9): _migration(), _parked(), Conversation.flow_draft: the AI draft parked while pra-quem is answered (TASK-03, _stale(), test_the_attendee_step_floor_drops_the_draft(), test_the_draft_rides_every_attendee_step(), test_the_llm_floor_drops_the_draft(), test_the_migration_adds_and_drops_a_nullable_json_column_on_sqlite() (+1 more)

### Community 842 - "CHECKPOINT — IA entra em qualquer etapa: rascunho v2, `flow_draft` e o resolvedor (TASK-030 P2)"
Cohesion: 0.20
Nodes (9): 1. O que muda para o paciente, 2. Onde está cada peça (âncoras), 3. Interruptor por clínica, 4. Observabilidade, 5. Decisões e limites conhecidos, 6. Pendências, 7. Deploy (quando o dono pedir), 8. Validação local desta correção (+1 more)

### Community 843 - "CHECKPOINT — Portal: a primeira mensagem chega ao ENTRAR, escolhida pelo contexto (TASK-035)"
Cohesion: 0.20
Nodes (9): CHECKPOINT — Portal: a primeira mensagem chega ao ENTRAR, escolhida pelo contexto (TASK-035), Mudança que afeta o WhatsApp (decisão do dono pendente), O que entrou, O que NÃO foi provado, Ordem de deploy (nada disto foi executado), Por que o paciente só via algo depois de escrever, Qual mensagem (primeira regra que casar), Quando a entrada fica em silêncio (`_brain_message_entry_decision`) (+1 more)

### Community 857 - "test_migration_reminders_foundation.py"
Cohesion: 0.42
Nodes (8): Engine, _load(), The R1 migration: additive, reversible, in sync with the model (TASK-032).  Th, _run(), _stub_engine(), test_migrated_table_has_exactly_the_model_columns(), test_there_is_one_head_and_reminder_foundation_is_in_its_history(), test_upgrade_adds_columns_and_table_then_downgrade_removes_them()

### Community 892 - "CHECKPOINT — confirmação expressa, retomada do rascunho e gerenciamento v2 (TASK-030 P3)"
Cohesion: 0.12
Nodes (15): Captura corrigida integrada — 2026-10-07, CHECKPOINT — confirmação expressa, retomada do rascunho e gerenciamento v2 (TASK-030 P3), Conclusão do P3 — 2026-10-06, Correção da captura no caminho atual — 2026-10-07, Correção da revisão: consultas simultâneas, Correções, Integração para a próxima sessão de testes — 2026-10-06, Integração/publicação autorizadas — 2026-10-06 (+7 more)

### Community 893 - "TASK-030 — IA entra em qualquer etapa: P2a"
Cohesion: 0.29
Nodes (6): Decisões e pendências para a continuação, Migração comprovada em SQLite e PostgreSQL, O que está construído, Revisão independente e correções, TASK-030 — IA entra em qualquer etapa: P2a, Validação

## Knowledge Gaps
- **1258 isolated node(s):** `secretaria`, `_Claim`, `Product vision`, `The "Eye Company" scaffold is gone — keep it that way`, `Per-tenant config (what each clinic can configure)` (+1253 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **197 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Tenant` connect `Tenant` to `test_onboarding_cron.py`, `_outbound`, `test_reminders_plugin.py`, `test_hub_professionals.py`, `route`, `test_action_buttons.py`, `test_professional_notification.py`, `test_hub_oauth.py`, `tenants.py`, `sent`, `test_webhook_history_sync.py`, `test_hub_calendar_owner_agenda.py`, `test_reactivation.py`, `seed_world`, `test_remove_context_command.py`, `test_brain_message_email_otp_inline.py`, `tenant_config.py`, `test_deposit_lifecycle.py`, `test_hub_calendar_confirmation.py`, `test_patient_name_step.py`, `test_brain_message_interactive_tap.py`, `set_google_refresh_token`, `onboarding_cron.py`, `AsaasClient`, `test_typing_indicator.py`, `test_bot_reply_gating.py`, `test_hub_release.py`, `test_hub_calendar_money.py`, `test_booking_notifications.py`, `test_hub_config.py`, `test_express_confirmation_worker.py`, `Patient`, `Base`, `outbound_messages`, `test_attendee_booking.py`, `reminders.py`, `test_message_delivery_status.py`, `PixDeposit`, `test_professional_config_gap_alert.py`, `tools.py`, `calendar.py`, `test_clinic_facts_hub.py`, `reminder_hooks.py`, `test_reminder_schedule.py`, `resolve_professional_calendar`, `setup_world`, `Appointment`, `test_internal_asaas_connection.py`, `ConsentEvent`, `BookingHold`, `test_analytics_bi_plugin.py`, `test_a_known_account_whose_name_brain_api_sends_reads_the_frame_then_the_menu`, `ProcessedEvent`, `test_interactive_bubble_record.py`, `test_agent_menu_tools.py`, `conversations.py`, `_run_send_bot_reply_capturing_run_agent`, `load_tenant_config`, `test_ai_tools_cancel_money.py`, `db`, `test_precheck_handoff_plugin.py`, `PatientOpeningContext`, `test_precheck_handoff_tool.py`, `test_confirm_recheck_slot.py`, `professional_notification.py`, `test_pii_pseudonymization.py`, `professionals.py`, `cleanup_patients`, `test_appointment_edit_apply.py`, `Conversation`, `test_multi_unit_plugin.py`, `test_hub_conversations_my_patients.py`, `test_hub_config_extra_reminder.py`, `main`, `insurance_catalog.py`, `test_convenio_catalogo_db.py`, `_load_snapshot`, `backfill_service_catalog.py`, `format_brl`, `test_admin_tenants.py`, `test_booking_owner_persistence.py`, `test_hub_calendar_health.py`, `test_deposit_release_preview.py`, `test_hub_configuration.py`, `test_brain_message_attachments.py`, `is_live_status`, `test_convenio_catalogo_flow.py`, `_resolve`, `test_service_guides_multipro.py`, `test_simulation_api.py`, `test_hub_staff_message.py`, `test_ai_v2_blindness.py`, `hub_configuration.py`, `test_debug_reset_menu3.py`, `test_patient_email_pii.py`, `test_pix_deposit_plugin.py`, `load_simulation_inputs`, `test_hub_edit_appointment.py`, `_reminders_v2.py`, `insurance.py`, `test_booking_code_gate.py`, `test_portal_context_opening.py`, `config.py`, `test_get_availability_tool.py`, `test_menu_command.py`, `test_flow_cancel_money.py`, `test_bot_allowlist.py`, `test_handover_echoes.py`, `test_hub_deps.py`, `Professional`, `reminder_schedule.py`, `oauth.py`, `_seed_tenant`, `test_booking_draft_continuation.py`, `test_booking_draft_resume_questions.py`, `test_reminder_v2_wiring.py`, `datetime`, `internal_provisioning.py`, `AppointmentReminder`, `_route_inbound_turn`, `get_agenda_viewer`, `simulation.py`, `test_manage_v2_handback.py`, `decide_reminder_opening`, `apply_appointment_edit`, `test_resolve_tenant.py`, `_notify_portal`, `test_back_leaves_for_the_menu_without_touching_the_appointment`, `_apply_deposit_awareness`, `load_tenant_insurance`, `setup_logging`, `test_brain_message_recent_page.py`, `test_calendar_events_insurance.py`, `run_post_booking_hooks`, `test_lgpd_consent_gate.py`, `make_appointment`, `test_brain_message_open.py`, `get_context_completeness`, `test_resend_asks_for_a_new_code_and_re_emits_the_same_card`, `_report`, `main`, `test_list_patient_appointments_tool.py`, `test_staff_appointment_edit.py`, `TenantConfigRead`, `WhatsAppClient`, `test_the_retry_gives_up_and_tells_the_clinic`, `resolve_opening_message`, `test_human_backup_plugin.py`, `test_hub_conversations.py`, `_seed`, `test_reminder_v2_hub_wiring.py`, `_persist_brain_message_inbound`, `analytics_advanced`, `test_blind_staging_tools.py`, `test_hub_config_pix.py`, `get_current_tenant`, `test_handback_events.py`, `staff_appointment_edit.py`, `get_conversation`, `test_waba_fail_closed.py`, `test_r7_calendar_origin.py`, `test_patient_channel_identity.py`, `test_post_booking_plugin.py`, `test_professional_null_vs_empty.py`, `_rows`, `test_professional_calendar_wire.py`, `normalize`, `reload_appointment`, `appointment_change_notifications.py`, `test_cancel_event_ownership.py`, `as_utc`, `_ctx`, `Enum`, `test_main_menu_manage_entry.py`, `test_brain_message_pipeline.py`, `PostBookingContext`, `delete_tenant`, `test_multi_professional_plugin.py`, `reminder_text.py`, `confirmation_warnings.py`, `CalendarService`, `test_reminder_confirmation.py`, `message_status.py`, `test_patient_context.py`, `_flow_handback_tools`, `db`, `_connect_calendar`, `_handle_remove_context_command`, `test_reminder_r3_cancel_path.py`?**
  _High betweenness centrality (0.237) - this node is a cross-community bridge._
- **Why does `Base` connect `Base` to `test_reminders_plugin.py`, `test_hub_professionals.py`, `test_action_buttons.py`, `Tenant`, `test_remove_context_command.py`, `tenant_config.py`, `test_brain_message_interactive_tap.py`, `set_google_refresh_token`, `AsaasClient`, `test_typing_indicator.py`, `test_express_confirmation_worker.py`, `Patient`, `test_attendee_booking.py`, `PixDeposit`, `test_cancellation_notice.py`, `test_professional_config_gap_alert.py`, `resolve_professional_calendar`, `Appointment`, `_WireClient`, `ConsentEvent`, `BookingHold`, `test_a_known_account_whose_name_brain_api_sends_reads_the_frame_then_the_menu`, `ProcessedEvent`, `test_interactive_bubble_record.py`, `test_ai_tools_cancel_money.py`, `test_confirm_recheck_slot.py`, `test_appointment_edit_apply.py`, `Conversation`, `test_multi_unit_plugin.py`, `insurance_catalog.py`, `_load_snapshot`, `test_convenio_catalogo_db.py`, `test_booking_owner_persistence.py`, `test_hub_calendar_health.py`, `test_patient_context.py`, `test_convenio_catalogo_flow.py`, `test_brain_message_attachments.py`, `Exception`, `Settings`, `test_debug_reset_menu3.py`, `_GoogleEvents`, `_Agenda`, `test_portal_context_opening.py`, `test_booking_code_gate.py`, `config.py`, `test_get_availability_tool.py`, `test_flow_cancel_money.py`, `Professional`, `_FakeCalendarService`, `_CapturingLogger`, `test_booking_draft_continuation.py`, `test_booking_draft_resume_questions.py`, `datetime`, `AppointmentReminder`, `pending_identity.py`, `_Handoff`, `TenantRuntimeConfig`, `FakeAsaasClient`, `_FakeWhatsAppClient`, `_Queue`, `_CalendarPerId`, `_FakeArqPool`, `_FakeWhatsAppClient`, `_CalendarWithBusy`, `_ExplodingWhatsAppClient`, `_FakeCalendarService`, `test_calendar_events_insurance.py`, `_WhatsApp`, `_LogRecorder`, `_FakeCalendarService`, `_FakeWhatsAppClient`, `_FakeAsaasClient`, `_FailingWhatsAppClient`, `_State`, `WebhookValue`, `EntitlementSummary`, `test_human_backup_plugin.py`, `test_hub_conversations.py`, `test_blind_staging_tools.py`, `get_current_tenant`, `test_handback_events.py`, `_FakeWhatsApp`, `get_conversation`, `test_tasks_helpers.py`, `normalize`, `test_cancel_event_ownership.py`, `BrainMessageSender`, `_FakeWhatsAppClient`, `test_multi_professional_plugin.py`, `test_availability_windows_scan.py`?**
  _High betweenness centrality (0.062) - this node is a cross-community bridge._
- **Why does `Appointment` connect `Appointment` to `test_onboarding_cron.py`, `test_reminders_plugin.py`, `test_action_buttons.py`, `test_professional_notification.py`, `sent`, `Tenant`, `test_hub_calendar_owner_agenda.py`, `seed_world`, `test_remove_context_command.py`, `test_deposit_lifecycle.py`, `test_hub_calendar_confirmation.py`, `AsaasClient`, `test_internal.py`, `test_hub_release.py`, `test_hub_calendar_money.py`, `test_express_confirmation_worker.py`, `Patient`, `Base`, `outbound_messages`, `test_attendee_booking.py`, `reminders.py`, `PixDeposit`, `tools.py`, `calendar.py`, `setup_world`, `reminder_hooks.py`, `test_reminder_schedule.py`, `resolve_professional_calendar`, `ConsentEvent`, `test_analytics_bi_plugin.py`, `test_precheck_handoff_plugin.py`, `test_agent_menu_tools.py`, `_run_send_bot_reply_capturing_run_agent`, `db`, `test_ai_tools_cancel_money.py`, `test_confirm_recheck_slot.py`, `professional_notification.py`, `test_pii_pseudonymization.py`, `_seed`, `cleanup_patients`, `test_appointment_edit_apply.py`, `test_multi_unit_plugin.py`, `test_hub_conversations_my_patients.py`, `test_hub_config_extra_reminder.py`, `_load_snapshot`, `test_convenio_catalogo_db.py`, `test_booking_owner_persistence.py`, `test_deposit_release_preview.py`, `is_live_status`, `_resolve`, `internal.py`, `test_hub_staff_message.py`, `test_ai_v2_blindness.py`, `test_debug_reset_menu3.py`, `test_pix_deposit_plugin.py`, `_reminders_v2.py`, `test_portal_context_opening.py`, `test_booking_code_gate.py`, `config.py`, `test_menu_command.py`, `test_flow_cancel_money.py`, `Professional`, `reminder_schedule.py`, `_seed_tenant`, `test_booking_draft_resume_questions.py`, `AppointmentReminder`, `test_hub_events_visibility.py`, `test_manage_v2_handback.py`, `decide_reminder_opening`, `apply_appointment_edit`, `_notify_portal`, `test_back_leaves_for_the_menu_without_touching_the_appointment`, `_apply_deposit_awareness`, `load_tenant_insurance`, `test_calendar_events_insurance.py`, `run_post_booking_hooks`, `make_appointment`, `_report`, `test_list_patient_appointments_tool.py`, `test_staff_appointment_edit.py`, `WhatsAppClient`, `resolve_opening_message`, `test_reminder_v2_hub_wiring.py`, `AgendaViewer`, `test_blind_staging_tools.py`, `test_handback_events.py`, `staff_appointment_edit.py`, `get_conversation`, `test_r7_calendar_origin.py`, `test_post_booking_plugin.py`, `test_professional_null_vs_empty.py`, `_rows`, `reload_appointment`, `appointment_change_notifications.py`, `test_cancel_event_ownership.py`, `as_utc`, `_ctx`, `test_main_menu_manage_entry.py`, `EditDraft`, `PostBookingContext`, `delete_tenant`, `test_multi_professional_plugin.py`, `reminder_text.py`, `load_pseudonymizer`, `test_reminder_v2_hooks.py`, `confirmation_warnings.py`, `CalendarService`, `test_reminder_confirmation.py`, `db`, `_r7_support.py`, `_connect_calendar`, `_handle_remove_context_command`, `test_reminder_r3_cancel_path.py`?**
  _High betweenness centrality (0.038) - this node is a cross-community bridge._
- **Are the 295 inferred relationships involving `Tenant` (e.g. with `main()` and `_audit()`) actually correct?**
  _`Tenant` has 295 INFERRED edges - model-reasoned connections that need verification._
- **Are the 264 inferred relationships involving `Appointment` (e.g. with `_report()` and `_mark_appointment_cancelled()`) actually correct?**
  _`Appointment` has 264 INFERRED edges - model-reasoned connections that need verification._
- **Are the 224 inferred relationships involving `Conversation` (e.g. with `_own_upcoming()` and `_conversation_attendee_name()`) actually correct?**
  _`Conversation` has 224 INFERRED edges - model-reasoned connections that need verification._
- **Are the 4 inferred relationships involving `seed_world()` (e.g. with `Conversation` and `Patient`) actually correct?**
  _`seed_world()` has 4 INFERRED edges - model-reasoned connections that need verification._