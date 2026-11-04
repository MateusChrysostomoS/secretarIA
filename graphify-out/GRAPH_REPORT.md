# Graph Report - wt-cal-sec  (2026-11-04)

## Corpus Check
- 938 files · ~1,498,177 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 19441 nodes · 53093 edges · 972 communities (780 shown, 192 thin omitted)
- Extraction: 83% EXTRACTED · 17% INFERRED · 0% AMBIGUOUS · INFERRED: 9244 edges (avg confidence: 0.73)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `61f00756`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- test_onboarding_cron.py
- _outbound
- test_reminders_plugin.py
- test_hub_professionals.py
- _tenant
- test_action_buttons.py
- test_professional_notification.py
- test_hub_oauth.py
- tenants.py
- BaseModel
- test_rebooking_flow.py
- test_webhook_history_sync.py
- _send_bot_reply
- test_hub_calendar_owner_agenda.py
- test_reactivation.py
- seed_world
- test_remove_context_command.py
- test_precheck_handoff.py
- tenant_config.py
- test_deposit_lifecycle.py
- test_hub_calendar_confirmation.py
- test_patient_name_step.py
- BrainMessageSender
- test_internal_provisioning.py
- test_brain_onboarding.py
- onboarding_cron.py
- test_email_transactional.py
- AsaasClient
- secretary_system_prompt
- test_typing_indicator.py
- test_internal.py
- _make_conversation
- test_hub_release.py
- test_hub_calendar_money.py
- test_booking_notifications.py
- test_hub_config.py
- test_booking_gate_deposit_fallback.py
- Patient
- agenda
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
- _seed
- Deliver `body` to `patient` over WhatsApp — the only channel a     Conversation
- test_internal_asaas_connection.py
- Appointment
- test_agent_tool_enforcement.py
- test_internal_privacy.py
- BookingHold
- FailureReason
- test_precheck_handoff_plugin.py
- test_audio_transcription.py
- test_interactive_bubble_record.py
- test_agent_menu_tools.py
- HandoverState
- route
- load_tenant_config
- test_ai_tools_cancel_money.py
- test_precheck_handoff_tool.py
- PROMPT 2 — state-driven greetings, buttons, manage sub-flow, Outro handoff
- PaymentProvider
- extract_inbound_body
- test_confirm_recheck_slot.py
- professional_notification.py
- test_pii_pseudonymization.py
- professionals.py
- attachment_http.py
- test_asaas_account_status.py
- test_build_identity.py
- test_professional_edit_notification.py
- CHECKPOINT — Google Calendar integration modes (per_professional / shared_account)
- SecretarIA
- Conversation
- test_flow_day_picker.py
- LocalCalendarService
- CHECKPOINT — Limites de texto do WhatsApp (constante única + corte marcado + trava no hub)
- CHECKPOINT — Onboarding & multi-professional configuration
- build_patient_calendar_link
- insurance_catalog.py
- test_convenio_catalogo_db.py
- run_agent
- test_booking_owner_persistence.py
- test_hub_calendar_health.py
- format_brl
- test_inbound_tap_display_vs_routing.py
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
- test_debug_reset_menu3.py
- service_change.py
- test_ai_v2_blindness.py
- hub_configuration.py
- calendar.py
- media_storage.py
- CHECKPOINT — WhatsApp Coexistence onboarding (Tasks 4 + 5)
- test_pix_deposit_plugin.py
- load_simulation_inputs
- test_webhook.py
- CHECKPOINT — Post-consult tenant config fields
- add_warnable_row
- insurance.py
- test_booking_code_gate.py
- CHECKPOINT — Inbound WhatsApp audio transcription
- CHECKPOINT — Pix deposit (sinal) add-on + appointment payment lifecycle
- config.py
- test_get_availability_tool.py
- test_menu_command.py
- test_deposit_breaker.py
- test_bot_allowlist.py
- b06ff85998bf_pix_deposit_asaas.py
- f06e85b476f2_onboarding_tenant_professional_config_.py
- test_handover_echoes.py
- test_flow_router_insurance.py
- payments_cron.py
- build_identity
- test_turn_safety_net.py
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
- reminder_engine.py
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
- DepositReleaseOffer
- inbound_routing_text
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
- db
- Return free [start, end) slots on `day` within business hours.          Walks th
- SCOPES = calendar.events
- test_brain_message_open.py
- services/entitlements_client.py
- Decide the next deterministic step for this inbound turn.      `patient_name` is
- PrecheckClient
- services/tenant_config.py (TenantRuntimeConfig)
- AppointmentReminder
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
- reload_appointment
- HMAC signature tests
- Webhook endpoint tests
- TypedDict
- FakeProvider
- internal_provisioning.py
- _seed
- sent
- test_human_backup_plugin.py
- test_hub_conversations.py
- Parte 1 — `/menu` deixou de ser destrutivo (PROMPT_FIX_18)
- 3. O que entrou onde
- CHECKPOINT — Seletor de dia/horário reaproveitável + entrada determinística
- CHECKPOINT — e-mail + OTP inline no Brain-Message
- test_reminder_v2_hub_wiring.py
- AgendaViewer
- Professional
- analytics_advanced
- test_emoji_decoration.py
- BookingDraftRequested
- request_precheck_handoff
- CHECKPOINT — Anexos no Portal (Brain-Message), parte 2: secretarIA (armazenamento, envio e recebimento)
- test_admin.py
- BookingDraft
- CHECKPOINT — QA ao vivo do canal Brain-Message em produção (2026-09-09)
- CHECKPOINT — PreCheck oferecido automaticamente logo depois do agendamento
- secretary_system_prompt_v2
- CHECKPOINT — Catálogo canônico de serviços da clínica (FEAT 35)
- test_hub_single_doctor_setup.py
- context
- CHECKPOINT — Estado da mensagem (enviado / entregue / lido / falhou), parte 1: secretarIA
- test_hub_config_pix.py
- get_session
- CHECKPOINT — `RESCHEDULED` é status VIVO (taxonomia unificada)
- test_handback_events.py
- _resolve_unit_or_error
- CHECKPOINT — Fluxos determinísticos incondicionais (fim do gate `initial_flows.enabled`)
- _FakeCalendarService
- staff_appointment_edit.py
- PluginSpec
- attachments.py
- get_conversation
- TenantWhatsAppCredentialMissing
- HandoffOutcome
- test_prompts_task072.py
- test_patient_channel_identity.py
- test_post_booking_plugin.py
- test_reminder_v2_text.py
- test_professional_null_vs_empty.py
- _on_inbound
- Parte 2 — `start_guided_booking`: a LLM devolvendo o agendamento aos botões
- test_booking_min_notice.py
- Jornada do paciente — WhatsApp e Portal, em linguagem leiga
- TASK-030 P2a — testes após deploy
- CHECKPOINT — Moldura de saudação + aviso de LGPD (2026-08-31)
- CHECKPOINT — FIX 32: a chave de idempotência devolvida para um retry inexistente
- CHECKPOINT — E-mail ao profissional quando uma consulta é marcada
- test_tasks_helpers.py
- CHECKPOINT — alerta de profissional com configuração incompleta (FEAT 41 + FIX 34)
- FakeRedis
- Achados e delimitação da causa
- CHECKPOINT — CORS em formato JSON derrubou todo o portal do médico
- resolve_booking_day
- test_handoff_notification.py
- test_portal_context_opening.py
- NormalizedPaymentEvent
- appointment_change_notifications.py
- test_appointment_edit_flow_fields.py
- CHECKPOINT — botões e listas como dado: `messages.interactive` (2026-09-11)
- test_cancel_event_ownership.py
- test_onboarding_cadence.py
- _should_inject_post_consult_knowledge
- test_multi_professional_plugin.py
- ChannelSender
- CHECKPOINT — toque real no Portal do paciente (canal Brain-Message) (2026-09-12)
- _ctx
- Enum
- _FakeWhatsAppClient
- test_main_menu_manage_entry.py
- appointment_edit.py
- booking_notifications.py
- test_extra_reminder.py
- Identidade de paciente por canal — migração aditiva
- test_whatsapp_limits.py
- test_brain_message_pipeline.py
- CHECKPOINT — mensagens do console de staff (hub/conversations)
- test_calendar_sync.py
- BookingGate
- payer_data.py
- PostBookingContext
- test_r7_calendar_origin.py
- CHECKPOINT — toque em lista: título pra exibir, id pra rotear (2026-09-11)
- FakeCalendar
- availability_tool.py
- fetch_professional_emails
- conftest.py
- TASK-030 — captura completa e datas em linguagem natural
- summarize_turn
- CHECKPOINT — o código de 6 dígitos vira portão antes da consulta existir
- b4c2e8f1a9d3_drop_professional_email.py
- d2a5b9c7e3f1_patient_lgpd_accepted_at.py
- e3b7c1d5a9f2_drop_tenant_greeting_message.py
- test_manage_request.py
- minimal_event_payload
- reminder_text.py
- test_set_booking_draft_v2.py
- test_availability_windows_scan.py
- load_pseudonymizer
- canonical_service_name
- 4c8e2a7f1b93_message_interactive.py
- 9d3b7e1f5a2c_message_interactive_reply_id.py
- test_before_fulfillment.py
- CHECKPOINT — Portal: a conversa mostra as mensagens MAIS RECENTES (TASK-028)
- seed_paid_deposit
- test_brain_patients.py
- confirmation_warnings.py
- Lembretes R7 — Ações da clínica na agenda avisam o paciente (API) Implementation Plan
- check_deploy_parity
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
- _card_notice
- contact_email
- google_calendar_id
- initial_flows
- GoogleScopeInsufficientError
- message_status.py
- menu_label
- reactivation
- is_active
- language
- test_reminder_v2_delivery.py
- allowlisted
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
- test_ai_tool_output_allowlist.py
- CHECKPOINT — conta ativa do Portal abre clínica nova: sem e-mail/código; nome vem da conta ou é perguntado uma vez (2026-09-24)
- CHECKPOINT — Agendar para outra pessoa ("Essa consulta é pra você?")
- CHECKPOINT — OTP / reset link leaking through arq's native job log
- graph.py
- pending_identity.py
- _seed
- apply_appointment_edit
- staff_transition
- CHECKPOINT — Onda 4: jornada sem gate ponta a ponta (E2E, sem PreCheck)
- test_entitlements_client.py
- test_bot_reply_gating.py
- d4f8a2c6e913_insurance_mode_catalog_ext_custom.py
- Digitando — backend universal (TASK-033)
- test_ia_atende_como_pessoa.py
- A patient, identified by their WhatsApp id (wa_id) within a tenant.
- Lacunas encontradas
- test_brain_message_email_otp_inline.py
- secretaria
- File Structure
- Return the human-readable text body of an inbound message.      Handles text m
- Persist one outbound message. Returns the empty send response.          `{}` o
- c1d4a8e6f2b0_message_interactive_null_backfill.py
- Referência — como convênios de saúde pagam a consulta no Brasil (2025/2026)
- AsaasProvider
- outbound_messages
- CHECKPOINT — A IA atende como pessoa; humano por último (TASK-038)
- test_analytics_bi_plugin.py
- File Structure
- test_text_extract.py
- Portal: visita descartada ao entrar na conta — TASK-034
- context_catalog_proposal.py
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
- test_chat_model.py
- test_asaas_probes.py
- availability.py
- Review Focus
- File Structure
- test_release_offer.py
- parse_patient_name
- db
- Lembrete com "Alterar Dados" Implementation Plan
- Tenant
- test_patient_context.py
- __init__.py
- logging.py
- __init__.py
- __init__.py
- guard_reply
- test_availability_windows.py
- Lembrete com "Alterar Dados" — Design
- test_ai_toolset_v2_locks.py
- Part B — Brain-Message-Frontend (after R5 is merged)
- CHECKPOINT — Lembretes R2: motor de lembretes (TASK-032)
- test_payer_data_flow.py
- test_multi_unit_plugin.py
- CHECKPOINT_tenant_patient_cleanup.md
- test_cors_origins_parsing.py
- test_hub_conversations_my_patients.py
- test_portal_booking_owner_calendar.py
- test_hub_config_extra_reminder.py
- Modelos do WhatsApp para os lembretes (TASK-032 R2)
- _reminders_v2.py
- test_payment_attention_hub.py
- R9 — PreCheck out of the console + extra reminder "N dias antes, às HH:MM" Implementation Plan
- receive_webhook
- test_before_bypass_paths.py
- test_cancellation_notice.py
- booking_draft.py
- TASK-032 R6 — lembrete com Alterar Dados
- TASK-037/038 — testes reais após deploy (2026-10-07)
- test_deposit_release_preview.py
- _connect_calendar
- test_simulation_api.py
- test_service_guides_multipro.py
- test_arq_log_secret_redaction.py
- ManageRequest
- FakeCalendar
- appointment_edit_flow.py
- CHECKPOINT — Lembretes R7: ações da clínica na agenda avisam o paciente (TASK-044)
- TenantConfigUpdate
- test_workers_ns_patching.py
- Service
- test_hub_deps.py
- .get
- test_service_change_agenda.py
- _first_decision
- ContextExtraction
- test_prompts_clinic_facts.py
- test_appointment_edit_pure.py
- test_asaas_connection.py
- TASK-032 — verificação após deploy, 08/10/2026
- test_reminder_opening_decision.py
- test_hub_edit_appointment.py
- test_probe_charge.py
- test_deposit_failure_notice.py
- Primeira mensagem de quem tem consulta — mesmo lembrete, mesmos fluxos
- make_event
- test_hub_payment_accounts.py
- CalendarUnavailableError
- Entry was pre-checked and allowed it, but a race left the deposit     already at
- The body a day-row tap produces (schemas.webhook.extract_inbound_body).
- The body a prof-row tap produces (see schemas.webhook.extract_inbound_body).
- as_utc
- CHECKPOINT — IA v2: prompt e regras (TASK-030 P5)
- E-mail fixo ao médico após confirmar Alterar Dados
- ProviderError
- Ações da clínica na agenda avisam o paciente — design (2026-10-09)
- ReminderJob
- test_booking_draft_continuation.py
- test_booking_draft_resume_questions.py
- Subagendas no convite e em “também atende” — TASK-054
- test_payment_failure_reason.py
- test_appointment_change_notifications.py
- test_clinic_context_backend.py
- CHECKPOINT — Lembretes R3: abertura do chat e caminho do Cancelar (TASK-032)
- test_booking_draft.py
- test_payment_cron_jobs.py
- TASK-030 — prova real da captura e das datas após deploy
- TASK-030 — reteste após deploy do P4, 2026-10-07
- datetime
- test_flow_replaces_column.py
- test_appointment_edit_write.py
- test_calendar_unavailable_mapping.py
- db
- _FakeWhatsAppClient
- test_availability.py
- a7e2c9d4f1b6_conversation_flow_replaces_appointment.py
- test_handback_log.py
- resolve_opening_message
- test_service_change_cancel.py
- test_service_change_webhook.py
- _seed_tenant
- _WhatsApp
- c9f4e2a7b815_insurance_catalog.py
- verify_subscription_token
- test_confirmation_warning_email.py
- test_manage_v2_handback.py
- TenantRuntimeConfig
- _Pool
- decide_reminder_opening
- _run_hand_back_with_intro
- _FakeWhatsApp
- TASK-041 — edição pela IA sem perder a consulta
- test_deposit_wait_turns.py
- test_flow_day_calendar.py
- test_calendar_event_body.py
- get_settings
- build_conversation_state
- CHECKPOINT — Lembretes R4: avisos, liberar horário e mensagem (TASK-032)
- clinic_facts.py
- FastAPI
- test_safe_fetch.py
- Lembretes R2 — Motor de lembretes Implementation Plan
- 4. Desenho
- _FakeAsyncClient
- test_hub_config_paid_notices.py
- test_release_offer_buttons.py
- test_hub_staff_message.py
- CHECKPOINT — "Meus pacientes" no console (TASK-046 R8, parte A)
- Retenção de visitas vazias + espera de código de conta conhecida — TASK-042 (lado secretarIA)
- TASK-041 — prova real da edição no Portal, 09/10/2026
- Exception
- CHECKPOINT — remediação lembretes R6 (e-mail ao médico + entrada pelo link)
- test_deposit_after_mode.py
- make_sessionmaker
- Lembretes R4 — Avisos por e-mail, estado vermelho, liberar horário e mensagem ao paciente (API) Implementation Plan
- test_release_carry_check.py
- _FakeArqPool
- Release R5 / R7 / R8 — 2026-10-10 (TASK-047)
- Para o P5 (o que a IA passa a ver — texto e nomes exatos)
- Lembretes R3 — Primeira mensagem no chat e caminho do Cancelar Implementation Plan
- test_migration_professional_edit_notices.py
- test_a_returning_portal_patient_gets_the_resume_offer_on_the_portal
- PaymentStatus
- _apply_deposit_awareness
- test_draft_wait_questions.py
- _audit
- WhatsAppClient
- ContextSource
- _log
- test_payment_settings.py
- chat_model.py
- TextBubble
- File Structure
- File Structure
- Interfaces
- Lembretes R1 — Fundação Implementation Plan
- build
- _Response
- EntitlementSummary
- Settings
- receive_asaas_webhook
- test_appointment_edit_paid_service.py
- test_payment_secret_hygiene.py
- AppointmentDepositView
- Lembretes R5 — Front da agenda (cores, ✓/✓✓, gaveta com ações por horário, avisos ao paciente, Editar/Remarcar, legenda) e configuração "Lembretes e avisos" Implementation Plan
- Lembretes, primeira mensagem e estado de confirmação da consulta — Design
- test_payment_settings_roles.py
- test_context_sources_api.py
- payment_accounts.py
- _ExplodingWhatsAppClient
- FakeAsaasClient
- delete_tenant
- test_context_catalog_proposal.py
- IA entra em qualquer etapa — P2a: disponibilidade, rascunho v2, `flow_draft` e o resolvedor — Implementation Plan
- test_release_offer_dispatch.py
- test_deposit_gate.py
- PaymentAttentionItem
- test_deposit_cancel_no_refund.py
- _report
- _FakeCalendarService
- make_released
- test_payment_account_charge_health.py
- 5. Desenho
- safe_fetch.py
- test_migration_native_agenda.py
- b7e2c4f9a1d6_native_agenda_calendar_events.py
- OutputAllowlist
- fetch_has_secretary
- test_payment_migration.py
- db
- test_payment_webhook_handler.py
- test_both_answers_are_typed_in_the_published_openapi
- IA entra em qualquer etapa — P2b: hand-backs pelo resolvedor, "pra quem" sempre perguntado, ferramenta v2, interruptor e estado da conversa — Implementation Plan
- SubscriptionClaim
- booking_details_text
- test_pix_single_message.py
- handback_log.py
- test_payment_notify.py
- test_release_keep.py
- release_offers.py
- deposit_wait.py
- _card_state
- DepositQuote
- File Structure
- asaas_sandbox_proof.py
- test_express_confirmation_worker.py
- asaas_connection.py
- Antes de começar (uma vez)
- _Model
- adjustment_queries.py
- ProviderContract
- make_world
- FakeGoogle
- deliver
- context_extraction.py
- manage_request.py
- PaymentAccount
- test_before_booking_flow.py
- evaluate_deposit_readiness
- test_confirmation_card.py
- test_payment_tasks.py
- CHECKPOINT — Contexto da clínica que a LLM enxerga (TASK-025, parte backend)
- Lembretes R1 — Fundação (TASK-032)
- test_deposit_off_without_account.py
- test_service_change_policy.py
- test_booking_gate_deposit.py
- _FakeAsaas
- test_payment_attention.py
- test_deposit_adjustment_model.py
- test_release_offer_carry_over.py
- Review Focus
- test_flow_draft_column.py
- CHECKPOINT — IA entra em qualquer etapa: rascunho v2, `flow_draft` e o resolvedor (TASK-030 P2)
- CHECKPOINT — Portal: a primeira mensagem chega ao ENTRAR, escolhida pelo contexto (TASK-035)
- history_item_is_final
- test_before_interplay.py
- _Agenda
- test_patient_insurance_overview.py
- ClinicPaymentAccount
- ProbeResponse
- next_retry_time
- settings.py
- test_payment_models.py
- resolve_attention_item
- _FakeRedis
- insert_ignore
- AST
- test_migration_reminders_foundation.py
- _post_booking
- brain_onboarding.py
- accept_keep
- test_payment_accounts.py
- quote_deposit
- release_carry_check.py
- release_deadlines.py
- ingest_context_source
- test_asaas_sandbox_proof_script.py
- CHECKPOINT — Contexto da clínica por fontes (TASK-026)
- CHECKPOINT — Sinal Pix ANTES de confirmar: hold, promoção por pagamento, itens de atenção (TASK-017, fase F2 da TASK F)
- CHECKPOINT — Núcleo de provedores de pagamento (TASK-016, fase F1 da TASK F)
- get_payment_settings
- handle_release_offer_tap
- test_appointment_replacement_service.py
- test_staff_notice_rows.py
- test_payment_attention_roles.py
- test_release_deadlines.py
- CHECKPOINT — Liberar horário com sinal pago: manter, remarcar ou cancelar (TASK-052)
- create_insurance_catalog_entry
- _alert_one
- _flow_tenant_snapshot
- ask_after_booking
- test_asaas_sandbox_e2e_script.py
- CHECKPOINT — Lembretes R9: lembrete extra "N dias antes, às HH:MM" (TASK-048)
- Troca de serviço com sinal pago — diferença do sinal (design)
- main
- patient_insurance_overview
- test_service_change_postgres.py
- CLAUDE.md
- env.py
- CHECKPOINT — Simulação do fluxo "Agendar" (TASK-027)
- CHECKPOINT — TASK-050: `main` trazida para `feature/out-of-mvp` (Pix portado para os workers divididos)
- _WorkersNamespace
- CHECKPOINT — confirmação expressa, retomada do rascunho e gerenciamento v2 (TASK-030 P3)
- TASK-030 — IA entra em qualquer etapa: P2a
- test_appointment_replacement_router.py
- test_workers_layering.py
- test_deposit_release_offer_migration.py
- test_payment_arch.py
- extract_action_button
- 9. Troca de serviço com diferença do sinal (TASK-067, 2026-10-26 — LOCAL: não commitada, não publicada, não deployada)
- FakeRedis
- is_clinic_clinician
- simulation.py
- CHECKPOINT — convênios sem corte e antecedência mínima por médico (2026-10-27)
- CHECKPOINT — Chat vendor switch: OpenAI or Claude (TASK-069)
- OwnedCalendar
- c8a5d3f1b7e2_payment_provider_core.py
- test_edit_day_picker.py
- CHECKPOINT — TASK-072: nenhum horário fica guardado; troca de e-mail mantém o agendamento
- Recorder
- _StubCalendarService
- _open_paid_offer
- full_extraction
- _notify_portal
- Code conventions & project structure
- b6d2e8f4a1c3_deposit_release_offers.py
- send_payment_alert
- test_deploy_parity_cron_publishes_and_compares
- _FakeWhatsApp
- test_migration_min_booking_notice.py
- test_payer_document.py
- test_payer_email.py
- Atualização de feature/out-of-mvp — TASK-066
- fetch_patient_email_result
- FakeAsaasClient
- test_arq_cron_minutes.py
- _FakeCalendar
- "Outra data": calendário de dias livres (2026-11-04)
- .request
- .supplied_fields
- CHECKPOINT — TASK-073: respostas sigilosas mascaradas + e-mail no contexto da paciente
- SecretarIA — memória de cálculo e fontes
- CHECKPOINT — Sinal (Pix) é recurso padrão da SecretarIA, não add-on
- "voltar" sends the recurring-patient message — Implementation Plan
- test_context_source_model.py
- Medições do Asaas real (TASK-071, Plano 4)
- TASK-072 (contexto da IA) - nome/e-mail, perguntas em duas partes, recursos ligados/desligados
- CHECKPOINT — "voltar" envia a mensagem para paciente recorrente (TASK-076)
- panel.py
- get_context_completeness
- HookSpy
- require_professional_capacity
- test_the_right_code_activates_the_account_and_ends_the_wait
- test_redis_failure_never_propagates
- datetime
- check_scopes.py
- _FakeRedis
- load_reminder_content
- _FakeArqPool
- validar_dossie.py
- answer_masks.py
- reconcile_appointment_reminders
- test_an_accepted_verdict_needs_every_required_group_granted
- test_dispatch_bubbles_sends_nothing_without_a_tenant

## God Nodes (most connected - your core abstractions)
1. `Tenant` - 1003 edges
2. `Appointment` - 504 edges
3. `Conversation` - 393 edges
4. `Patient` - 330 edges
5. `db()` - 316 edges
6. `seed_world()` - 314 edges
7. `PixDeposit` - 306 edges
8. `Professional` - 228 edges
9. `route()` - 189 edges
10. `FlowRouterResult` - 185 edges

## Surprising Connections (you probably didn't know these)
- `upgrade()` --calls--> `encrypt()`  [INFERRED]
  migrations/versions/d7e8f9a0b1c2_encrypt_waba_token.py → src/secretaria/core/crypto.py
- `test_the_card_body_never_exceeds_the_interactive_limit()` --indirect_call--> `ButtonBubble`  [INFERRED]
  tests/test_confirmation_card.py → src/secretaria/ai/formatter.py
- `test_confirm_block_needs_no_limit_because_its_labels_are_fixed()` --calls--> `ButtonBubble`  [INFERRED]
  tests/test_prompts.py → src/secretaria/ai/formatter.py
- `test_a_handback_message_gets_whatsapp_bold_too()` --calls--> `handback_message()`  [INFERRED]
  tests/test_formatter.py → src/secretaria/ai/tools.py
- `test_manage_existing_appointment_tool_raises_canonical_action()` --indirect_call--> `ManageAppointmentRequested`  [INFERRED]
  tests/test_agent_menu_tools.py → src/secretaria/ai/tools.py

## Import Cycles
- None detected.

## Communities (972 total, 192 thin omitted)

### Community 0 - "test_onboarding_cron.py"
Cohesion: 0.05
Nodes (125): _FakeSession, _make_config(), _make_tenant(), _override_session(), AsyncClient, MonkeyPatch, Tests for the admin fleet endpoints (GET /admin/tenants + calendar health).  T, Minimal AsyncSession stand-in for the delete path.      `tenant` is what `get` (+117 more)

### Community 1 - "_outbound"
Cohesion: 0.11
Nodes (32): _bm_turn(), _flow_state(), _outbound(), One inbound on the Brain-Message channel, decided AND delivered., Every bot message this tenant's conversation has sent, in order., CHECKLIST: greeting without LGPD -> e-mail question -> LGPD -> normal flow., A portal-only clinic can converse without a live WhatsApp number.      `Tenant, No address claimed, no consent sent, still waiting. The re-ask is the turn. (+24 more)

### Community 2 - "test_reminders_plugin.py"
Cohesion: 0.12
Nodes (49): db(), _deposit_button_ids(), _entitled_fake(), _FailFirstTemplateClient, _fake_get_waba_token(), _fakes(), _FakeWhatsAppClient, _ledger_count() (+41 more)

### Community 3 - "test_hub_professionals.py"
Cohesion: 0.10
Nodes (73): _allow_multi_professional(), _connect_clinic(), db(), _entitled_fake(), _FakeCalendarServiceFactory, _FakeSecondaryCalendar, limit_enforced(), _never_called_fake() (+65 more)

### Community 4 - "_tenant"
Cohesion: 0.05
Nodes (119): enter_booking(), Deterministic entry for a direct "Agendar" tap: "Essa consulta é pra você?"., test_a_clear_request_is_not_the_generic_menu(), test_route_from_idle_drops_a_stale_marker(), test_the_idle_menu_is_the_generic_menu(), test_without_an_anchor_the_picker_is_unchanged(), test_the_draft_is_dropped_by_anything_that_leaves_the_attendee_steps(), _appt_window() (+111 more)

### Community 5 - "test_action_buttons.py"
Cohesion: 0.11
Nodes (42): _async_return(), db(), _fake_get_waba_token(), _FakeAsaasClient, _fakes(), _greeting_button_msg(), datetime, MonkeyPatch (+34 more)

### Community 6 - "test_professional_notification.py"
Cohesion: 0.05
Nodes (77): Run `post_booking` hooks for every plugin this tenant is entitled to.      Unl, run_post_booking(), _claimed(), _ctx(), db(), log(), _LogRecorder, _make_rows() (+69 more)

### Community 7 - "test_hub_oauth.py"
Cohesion: 0.18
Nodes (26): db(), _extract_state(), _fake_exchange(), professional(), AsyncClient, MonkeyPatch, Tests for api/hub/oauth.py — Google Calendar OAuth start/callback/disconnect, i, Reconnecting is how an existing tenant upgrades a token minted before     calen (+18 more)

### Community 8 - "tenants.py"
Cohesion: 0.11
Nodes (32): delete_tenant(), _derived_health(), list_tenants(), _list_tenants_page(), _probe_config(), _probe_page(), AsyncSession, description (+24 more)

### Community 9 - "BaseModel"
Cohesion: 0.04
Nodes (98): BaseModel, AppointmentStatus, _enum_values(), MessageDirection, MessageSender, Enum, Message model - a single message inside a conversation., Relative to the clinic's WhatsApp number. (+90 more)

### Community 10 - "test_rebooking_flow.py"
Cohesion: 0.12
Nodes (38): _bodies(), _Calendar, _conversation(), _professional(), Rebooking after the DOCTOR cancelled, and the reason when the patient won't., Drive the pair exactly as workers/tasks.py does: resolve the candidates     fir, The patient chose both a moment ago; asking again would be the flow     forgett, The cancelled booking's service is 45 min, the tenant default 30. A scan     at (+30 more)

### Community 11 - "test_webhook_history_sync.py"
Cohesion: 0.12
Nodes (34): WebhookValue, _CapturingLogger, db(), _history_value(), MonkeyPatch, Tests for the `history` / `smb_app_state_sync` webhook fields (contract v1 §10):, Records every log call (event name + args/kwargs) instead of printing.      Us, Every captured call flattened to one string, for a substring check. (+26 more)

### Community 12 - "_send_bot_reply"
Cohesion: 0.11
Nodes (23): begin_turn(), end_turn(), fallback_allowed(), hold_intro(), _key(), note_send(), The patient never goes unanswered - and the guarantee cannot be turned into a we, Open a fresh send ledger for one inbound turn; reset it with `end_turn`. (+15 more)

### Community 13 - "test_hub_calendar_owner_agenda.py"
Cohesion: 0.05
Nodes (93): _Client, _Events, _Executable, _free(), datetime, CalendarService.update_event_details / is_slot_free (TASK-032 R6, spec §5.6)., _service(), test_a_busy_event_blocks_unless_it_is_the_one_being_edited() (+85 more)

### Community 14 - "test_reactivation.py"
Cohesion: 0.03
Nodes (128): FlowState, Which deterministic (zero-LLM) flow the conversation is currently in.      IDL, classify_yes_no(), llm_state_ttl_minutes(), pending_identity_ttl_minutes(), True when this tenant offers a 'welcome back / continue?' prompt.      A confi, True when the returning "quer continuar?" prompt may be offered.      Universa, Minutes of silence after which a returning patient is offered to resume. (+120 more)

### Community 15 - "seed_world"
Cohesion: 0.07
Nodes (101): give_up_confirm_buttons(), The give-up confirmation: Sim, cancelar / Manter consulta., _format_appointment_when(), datetime, Render an appointment start for greeting copy, in the tenant's timezone., add_reminder(), get_reminder(), datetime (+93 more)

### Community 16 - "test_remove_context_command.py"
Cohesion: 0.11
Nodes (35): AnalyticsEvent, One minimal, non-personal business event for a tenant., _count(), db(), _FakeWhatsAppClient, MonkeyPatch, Tests for `/dangerously-remove-context` — the destructive reset (PROMPT_FIX_18)., The rehearsal must be message-for-message what a newcomer receives.      This (+27 more)

### Community 18 - "test_precheck_handoff.py"
Cohesion: 0.08
Nodes (59): HandoffResult, The outcome of one `request_precheck_handoff` call., _fresh_settings(), _install_fake_client(), log(), _LogRecorder, _posted_body(), MonkeyPatch (+51 more)

### Community 19 - "tenant_config.py"
Cohesion: 0.04
Nodes (110): encrypt(), Encrypt `plaintext`, returning a urlsafe-base64 ciphertext string., ProfessionalCredentials, At most one row per professional, holding its own encrypted Calendar refresh tok, One row per tenant holding its encrypted Google Calendar refresh token., TenantCredentials, build_clinic_features(), Which optional features are ON for the clinic (TASK-072), or None without a tena (+102 more)

### Community 20 - "test_deposit_lifecycle.py"
Cohesion: 0.12
Nodes (42): db(), _FailingWhatsAppClient, _FakeWhatsAppClient, _install_fake_client(), _install_fake_whatsapp(), AsyncSession, datetime, MonkeyPatch (+34 more)

### Community 21 - "test_hub_calendar_confirmation.py"
Cohesion: 0.25
Nodes (30): _booked(), _connect(), _events(), _patch(), AsyncClient, Hub agenda API: confirmation state on GET /events and PATCH status (TASK-032 R1,, Appointment 6 days out (real clock for the PATCH tests) with its 3 rows., R7 (spec 2026-10-09 §3): confirmed/attended on a cancelled booking is 409 not_li (+22 more)

### Community 22 - "test_patient_name_step.py"
Cohesion: 0.08
Nodes (73): _age_conversation(), _bm_turn(), _conversation(), db(), _llm_history(), _outbound(), _patient(), MonkeyPatch (+65 more)

### Community 23 - "BrainMessageSender"
Cohesion: 0.08
Nodes (48): BrainMessageSender, AsyncSession, UUID, Deliver a reply to a Brain-Message patient by recording it.      There is no n, Add the row; a bot turn also starts the bot's clock on the conversation., Refuse, loudly, and write nothing.          A WhatsApp template is a body appr, api(), db() (+40 more)

### Community 24 - "test_internal_provisioning.py"
Cohesion: 0.10
Nodes (48): db(), _FakeArqPool, AsyncClient, MonkeyPatch, Tests for the internal onboarding/provisioning API (`/internal/*`, contract v1 §, Records every enqueue_job call; installed on app.state.arq_pool., httpx's `.get()` shortcut has no `json=` parameter - only pass it for     the m, Both consumers use this internal endpoint; an existing unprepared row is repaire (+40 more)

### Community 25 - "test_brain_onboarding.py"
Cohesion: 0.12
Nodes (30): _configured_settings(), _FakeAsyncClient, _FakeResponse, _install_fake_client(), Exception, MonkeyPatch, Tests for services/brain_onboarding.py — the onboarding-cron -> brain-api clien, Corrections round "Task 2": once brain-api ships the three test-window     fiel (+22 more)

### Community 26 - "onboarding_cron.py"
Cohesion: 0.16
Nodes (24): _month_bounds_utc(), _process_config_reminder(), _process_retry_and_closing(), _process_test_window(), Counter, datetime, ZoneInfo, Onboarding lifecycle crons (cross-service contract v1 §11): retry nudges, D+30 (+16 more)

### Community 27 - "test_email_transactional.py"
Cohesion: 0.06
Nodes (48): dict, Render `template` with `variables` and send it. NEVER raises.      Returns Fal, Email the clinic when a patient could NOT be told their consultation was cancell, `str.format_map` companion that leaves an unknown `{placeholder}` intact     in, Render `template` with `variables` and send it. NEVER raises.      Same work a, _SafeDict, send_cancellation_escalation_alert(), send_transactional_email_message() (+40 more)

### Community 28 - "AsaasClient"
Cohesion: 0.10
Nodes (33): AsaasClient, Async wrapper around the Asaas v3 REST API, scoped to ONE tenant's own API key., POST /customers -> the new (or Asaas-resolved) customer id.          `cpf_cnpj, POST /payments (billingType=PIX). `due_date` ("YYYY-MM-DD") is the         call, GET /payments/{id}/pixQrCode -> {payload, encodedImage, expirationDate, ...}., GET /myAccount/status -> the four onboarding states (`general`, `documentation`,, GET /payments/{id} -> {id, status, value, externalReference, deleted, ...}., DELETE /payments/{id} — voids an unpaid (AWAITING) charge. (+25 more)

### Community 29 - "secretary_system_prompt"
Cohesion: 0.08
Nodes (57): _format_appointment_context(), Render the "CONSULTAS MARCADAS DESTE PACIENTE" block, or "" when unset.      `, Render the full system prompt for a specific tenant.      `name_tool`: whether, secretary_system_prompt(), SystemMessage, Opt-in: the real model transfers the user's stated date and attendee.  Only th, test_real_model_keeps_selected_day_when_answering_only_the_time(), test_real_model_transfers_the_stated_fields() (+49 more)

### Community 30 - "test_typing_indicator.py"
Cohesion: 0.08
Nodes (39): AsyncFakeRedis, typing_pool(), _BrokenRedis, client(), _conversation_id(), db(), datetime, The "digitando" flag: three typers, two viewers, never a source of errors. (+31 more)

### Community 31 - "test_internal.py"
Cohesion: 0.12
Nodes (29): _clear_override(), _FakeResult, _FakeSession, _make_appointment(), _make_patient(), _override_session(), AsyncClient, MonkeyPatch (+21 more)

### Community 32 - "_make_conversation"
Cohesion: 0.19
Nodes (22): _assert_llm_never_called(), _make_conversation(), MonkeyPatch, A turn to feed `_send_bot_reply`.      The default body is unmatched free text, A pre-deploy free-text button's numeric id (schemas/webhook.py::     extract_gr, Defensive: an unresolvable tenant (conversation/tenant row vanished)     sends, It used to build a bare `WhatsAppClient()` and answer from the global env     s, Fails closed like every other outbound: an unentitled tenant gets no     messag (+14 more)

### Community 33 - "test_hub_release.py"
Cohesion: 0.07
Nodes (79): _appointment(), _approve_paid_notices(), _attention_items(), _connect(), _deposit_status(), _FakeArqPool, _FakeCalendar, _freeze_release_clock() (+71 more)

### Community 34 - "test_hub_calendar_money.py"
Cohesion: 0.14
Nodes (22): _connect_calendar(), db(), _FakeArqPool, _FakeCalendarService, AsyncClient, datetime, Tests for api/hub/calendar.py's Pix-deposit money hooks (PROMPT S3 section 4, i, The deposit notice still rides along with the cancellation — it just     rides (+14 more)

### Community 35 - "test_booking_notifications.py"
Cohesion: 0.10
Nodes (33): _ctx(), _persist_plan(), Hook post_booking: confirmações ao paciente e à clínica (MVP Portal, Task 7B)., Live 2026-10-05: "Nova consulta marcada" opened with "Agendamento: <uuid>"., Live 2026-10-19: "Particular" read "Convênio informado; consulte o agendamento"., test_a_private_booking_is_named_particular_in_the_emails(), test_absent_insurance_omits_insurance_line(), test_another_clinics_doctor_is_never_named() (+25 more)

### Community 36 - "test_hub_config.py"
Cohesion: 0.08
Nodes (41): db(), AsyncClient, Tests for api/hub/config.py — PUT /tenants/me/config, focused on the contract v, The greeting's buttons are now a fixed, product-defined set     (workers/tasks., The tenant fixture has phone_number_id=None and no Calendar connected -     a p, A completely unrelated config field (no address/insurance at all)     must also, PUT no longer accepts this field at all: an incoming `greeting_buttons`     is, The greeting is ALWAYS sent with the fixed action buttons attached (no     more (+33 more)

### Community 37 - "test_booking_gate_deposit_fallback.py"
Cohesion: 0.07
Nodes (80): FakeRedis, DepositWaived, _promote_waived(), Promote the code-phase hold of a booking whose deposit was DROPPED, then tell th, `_payment_after_code`'s answer when the deposit was DROPPED for a technical reas, _account(), _asked_for_the_cpf(), _assert_human_offer() (+72 more)

### Community 38 - "Patient"
Cohesion: 0.02
Nodes (172): DeclarativeBase, DefaultExecutionContext, Best-effort patient WhatsApp id (phone) for the current turn.      Mirrors `_p, Grava o NOME do paciente desta conversa quando o bloco DADOS DO PACIENTE diz que, _resolve_patient_phone(), save_patient_name(), execute(), preview() (+164 more)

### Community 39 - "agenda"
Cohesion: 0.06
Nodes (80): CalendarSlotTakenError, UUID, An EXCLUSIVE write was refused: something busy already overlaps [start, end)., THE agenda-backend selection point: Google when a credential covers it, else loc, CalendarService for professional X under tenant config Y (contract v1 §10)., agenda(), datetime, UUID (+72 more)

### Community 40 - "test_message_delivery_status.py"
Cohesion: 0.11
Nodes (48): MessageStatus, The delivery status a bubble draws, derived from the row's timestamps.      Fa, status_of(), api(), db(), _deliver_receipt(), _ExplodingWhatsAppClient, _meta_receipt() (+40 more)

### Community 41 - "test_attendee_booking.py"
Cohesion: 0.06
Nodes (73): authorization_body(), is_attendee_name_question(), parse_attendee_name(), Booking for someone else: "Essa consulta é pra você?" (both channels).  Until, The authorization sentence with the WHOLE stored name interpolated., The attendee's name from a typed answer, or None when it is not a name.      S, Whether an outbound row asked for the attendee's name (prefix match).      Pre, _book() (+65 more)

### Community 42 - "reminders.py"
Cohesion: 0.16
Nodes (23): _as_utc(), _claim_reminder(), _emit_reminder_usage(), _last_inbound_at(), _meta_language_code(), _process_lead_window(), datetime, timedelta (+15 more)

### Community 44 - "SecretarIA"
Cohesion: 0.09
Nodes (22): 1. Install dependencies, 2. Create your .env, 3. Start Postgres + Redis, 4. Apply database migrations, 5. (optional) Seed a development tenant, 6. Run the API, 7. Run the worker (in a second terminal), Architecture (+14 more)

### Community 45 - "PixDeposit"
Cohesion: 0.12
Nodes (36): PixDeposit, One Pix deposit (sinal) charge tied to exactly one appointment., ProcessedAsaasEvent, One row per Asaas event id already handled.      The unique constraint on `eve, db(), _FakeArqPool, _FakeCalendarService, _fakes() (+28 more)

### Community 46 - "test_professional_config_gap_alert.py"
Cohesion: 0.06
Nodes (60): _confirm_service(), _conversation(), db(), _FakeCalendar, _FakeRedis, log(), _LogRecorder, _no_whatsapp() (+52 more)

### Community 47 - "tools.py"
Cohesion: 0.04
Nodes (108): _blocked_by_required_deposit(), _blocked_by_toolset_v2(), _blocked_tenant_level(), _busy_without_foreign_details(), _calendar_for_calendar_id(), _calendar_for_professional(), cancel_event(), _canonical_appointment_type() (+100 more)

### Community 48 - "calendar.py"
Cohesion: 0.04
Nodes (135): Row, _agenda_owners(), _agenda_sources(), agenda_viewer(), _AgendaSource, _appointment_read(), _calendar_for_professional(), _calendar_unavailable() (+127 more)

### Community 49 - "setup_world"
Cohesion: 0.09
Nodes (70): acting(), FakeArqPool, install_pool(), patch_status(), datetime, MonkeyPatch, r7(), Wiring shared by the TASK-032 R7 hub tests (status, confirm, attended, edit, can (+62 more)

### Community 50 - "resolve_professional_calendar"
Cohesion: 0.16
Nodes (26): _professional_credential(), Which refresh token to use for ONE professional's Calendar operations.      TH, Resolve ONE professional's own config into a ready CalendarService.      THE s, resolve_professional_calendar(), db(), _fake_calendar(), _FakeCalendarService, MonkeyPatch (+18 more)

### Community 51 - "_seed"
Cohesion: 0.09
Nodes (58): _codes(), db(), What stands between a clinic and a working Pix deposit (TASK-071, Plan 4, task 3, A clinic that switched the deposit off is not told about its Asaas account or pr, Measured on the real Asaas (2026-10-30): the smallest Pix charge is R$ 5,00., Nothing pending is NOT proof: green only after a real charge worked., A success recorded with the OLD key is not proof for the new one., Failure Monday, success Tuesday, new key Wednesday: nothing has failed with the (+50 more)

### Community 53 - "test_internal_asaas_connection.py"
Cohesion: 0.25
Nodes (18): _asaas(), db(), _override_session(), AsyncClient, MonkeyPatch, Tests for POST /internal/tenants/{tenant_id}/asaas-connection.  Mirrors tests/, _seed_tenant(), test_a_badly_shaped_key_or_token_is_refused_without_echoing_it() (+10 more)

### Community 54 - "Appointment"
Cohesion: 0.08
Nodes (51): Appointment, Platform-side record of a clinic appointment.      Created whenever the platfo, apply_asaas_event(), _as_utc(), at_reschedule_limit(), before_wait_card_text(), _cancellation_already_handled(), _cancellation_money_line() (+43 more)

### Community 55 - "test_agent_tool_enforcement.py"
Cohesion: 0.09
Nodes (27): _config(), _ExplodingCalendar, _multi_turn(), _names(), The agent's capabilities are enforced by the tool set, not by the prompt.  A m, Stand-in for the compiled LangGraph: records its tools, never calls an LLM., The safe alternative replaces the unsafe one — it does not sit beside it., No entitlement means no booking tool at all — never a broader one. (+19 more)

### Community 56 - "test_internal_privacy.py"
Cohesion: 0.30
Nodes (15): db(), _headers(), _override_session(), AsyncClient, MonkeyPatch, Tests for api/internal_privacy.py — LGPD export/erase endpoints — and the conse, _seed_full_subject(), test_erase_counts_and_anonymizes_appointment_not_deletes() (+7 more)

### Community 57 - "BookingHold"
Cohesion: 0.09
Nodes (51): BookingHold, One slot reserved for one conversation, until `expires_at`., appointment_stands_in(), _aware(), conversation_has_live_charge(), extend_to_payment(), held_windows(), HeldSlot (+43 more)

### Community 58 - "FailureReason"
Cohesion: 0.08
Nodes (47): breaker_reason(), _counter_key(), _doctor_address(), _email_links(), failure_reason_for(), NoticeOutcome, NoticeResult, _notify() (+39 more)

### Community 59 - "test_precheck_handoff_plugin.py"
Cohesion: 0.05
Nodes (79): _claimed(), _ctx(), db(), _Handoff, _make_portal_rows(), _make_rows(), _portal_bodies(), Tests for plugins/precheck_handoff.py — "your pre-consult is waiting".  Same i (+71 more)

### Community 60 - "test_audio_transcription.py"
Cohesion: 0.10
Nodes (40): iter_audio_messages(), Yield one minimal dict per inbound WhatsApp voice-note audio message.      Mir, db(), _FakeArqPool, MonkeyPatch, Tests for inbound WhatsApp AUDIO transcription.  Covers `iter_audio_messages`, A coexistence echo of the business's OWN outbound audio is never transcribed., Guarantee a fresh Settings rebuild for whichever test runs next.      Belt-and (+32 more)

### Community 61 - "test_interactive_bubble_record.py"
Cohesion: 0.09
Nodes (32): insurance_payment_description(), interactive_list_record(), What `send_list` puts on the patient's screen, as data.      The list card aft, test_a_card_without_days_is_byte_for_byte_what_it_was(), test_the_card_records_its_days_apart_from_the_rows(), test_payment_description(), _add(), db() (+24 more)

### Community 62 - "test_agent_menu_tools.py"
Cohesion: 0.07
Nodes (76): _config_with_selected_professional(), Overlay an explicitly-selected professional onto the runtime config.      `loa, _agent_context(), _booking_context(), db(), _handback_names(), Tests for the LLM handoff round (PROMPT 3, multi-doctor flow).  Covers the two, The clinic shape this repo calls "the tenant that broke": the legacy     `tenan (+68 more)

### Community 63 - "HandoverState"
Cohesion: 0.03
Nodes (107): refusal(), _extract_wam_id(), _get_conversation(), get_message_media(), get_typing(), _interactive_read(), _last_message_at(), list_conversations() (+99 more)

### Community 64 - "route"
Cohesion: 0.09
Nodes (76): Route one turn with `gate` in scope for every listing/booking branch.      The, Decide the next deterministic step for this inbound turn.      `patient_name`, route(), _agendar_tap(), _conv_at_service_confirm(), _conversation(), _FakeCalendar, _professionals() (+68 more)

### Community 65 - "load_tenant_config"
Cohesion: 0.09
Nodes (53): can_activate_professional_aware(), load_tenant_config(), professional_completeness(), Resolve a tenant row into the fully-decrypted runtime config.      The agent w, Encrypt and upsert the professional's own Calendar refresh token. Caller commits, Per-active-professional completeness + the partial-activation verdict.      `h, Professional-aware activation gate (contract endpoints 3/5).      Requires `pr, set_professional_google_refresh_token() (+45 more)

### Community 66 - "test_ai_tools_cancel_money.py"
Cohesion: 0.21
Nodes (14): _agent_context(), db(), _FakeCalendarService, _fakes(), datetime, MonkeyPatch, Tests for ai/tools.py's Pix-deposit money hook on the LLM cancel path (PROMPT S, Dev-script scaffolding (no tenant context) never crashes and never     issues a (+6 more)

### Community 67 - "test_precheck_handoff_tool.py"
Cohesion: 0.33
Nodes (16): _agent_context(), db(), _fakes(), MonkeyPatch, Tests for ai/tools.py:iniciar_pre_consulta — the precheck hand-off agent tool., Tenant context set but no conversation (or no patient on it) -> can't     resol, _seed_tenant_patient_conversation(), _set_precheck_number() (+8 more)

### Community 68 - "PROMPT 2 — state-driven greetings, buttons, manage sub-flow, Outro handoff"
Cohesion: 0.12
Nodes (15): Agent surface, Buttons by state, CHECKPOINT — Context-aware opening + read-only patient-appointments tool, Core rule, Greeting — HAS_UPCOMING(_SOON) final copy, Manage sub-flow — deterministic actions on the existing appointment, Outro → LLM handoff (appointment-aware agent mode), Pendências (+7 more)

### Community 69 - "PaymentProvider"
Cohesion: 0.06
Nodes (38): KeyError, _cleanup(), document_digits(), _is_a_patients_customer(), ProbeResult, AsyncSession, date, UUID (+30 more)

### Community 70 - "extract_inbound_body"
Cohesion: 0.11
Nodes (40): extract_greeting_button(), extract_inbound_body(), extract_inbound_reply_id(), _interactive_reply(), The tapped reply button or list row, whichever this message carries., Return the human-readable text of an inbound message.      What the patient ty, Return the raw id of the control an inbound message tapped, or None.      The, Decode a tap on the greeting's id, when it carries the "greeting|" prefix. (+32 more)

### Community 71 - "test_confirm_recheck_slot.py"
Cohesion: 0.09
Nodes (50): _Agenda, _agent_turn(), _appointment(), _at(), _confirming(), _confirming_move(), db(), _event() (+42 more)

### Community 72 - "professional_notification.py"
Cohesion: 0.14
Nodes (27): _as_utc(), _claim(), _deliver(), _ledger_key(), _load_professional(), _local_when(), _patient_line_name(), _post_booking() (+19 more)

### Community 73 - "test_pii_pseudonymization.py"
Cohesion: 0.08
Nodes (41): _busy_via_tool(), _CalendarWithBusy, db(), _fake_tool(), _patch_session_factory(), MonkeyPatch, The scrub/re-hydrate step between this repo and OpenAI.  Covers the two direct, The spy stands exactly where the OpenAI request is built. (+33 more)

### Community 74 - "professionals.py"
Cohesion: 0.08
Nodes (45): _count_active(), create_professional(), create_professional_calendar(), create_professional_calendars(), _get_professional(), _list_item(), list_professionals(), AsyncSession (+37 more)

### Community 75 - "attachment_http.py"
Cohesion: 0.08
Nodes (35): ASGIMessage, FormData, M, MultiPartException, Receive, RequestValidationError, Scope, Send (+27 more)

### Community 76 - "test_asaas_account_status.py"
Cohesion: 0.08
Nodes (44): OnboardingState, build_checklist(), build_checklist_items(), checklist_view(), ChecklistItem, ChecklistOut, _clicked(), AsyncSession (+36 more)

### Community 77 - "test_build_identity.py"
Cohesion: 0.13
Nodes (22): configured_secrets(), make_identity(), MonkeyPatch, Build identity and the deploy-divergence alarm (FIX_01 §5.1/§5.2).  The failur, A full sha from a pipeline and a short one from a panel are the same commit., The normal case today: nothing passes BUILD_SHA, parity still provable., Same sha label, different code — any disagreement wins., No pool, no peer — reported as unknown, never as agreement. (+14 more)

### Community 78 - "test_professional_edit_notification.py"
Cohesion: 0.10
Nodes (49): _apply(), _edit(), _reply(), test_a_cancelled_appointment_is_never_edited(), test_a_doctor_change_creates_the_new_event_then_deletes_the_old_one_after_commit(), test_a_time_change_counts_against_the_pix_reschedule_limit(), test_service_convenio_and_patient_are_written_to_the_row(), test_exhausted_pix_limit_cannot_commit_a_time_change() (+41 more)

### Community 79 - "CHECKPOINT — Google Calendar integration modes (per_professional / shared_account)"
Cohesion: 0.07
Nodes (27): 10. "Conectado" que não agenda — token da clínica recusado pelo Google (2026-09-12), 1. Mode field, 2. OAuth scopes, 3. Secondary calendar creation, 4. Routing rule (event create/move/cancel) — the credential × calendar_id edge case, 5. Scope-insufficient 403 → structured reconnect signal, 6. Mode endpoint, 7. Professionals roster payload (+19 more)

### Community 80 - "SecretarIA"
Cohesion: 0.14
Nodes (14): Auxiliary scripts (Fase A scaffolding), `CORS_ALLOW_ORIGINS` aceita DOIS formatos — e o valor "certo" já derrubou o portal, Deploy — a API e o worker são DOIS serviços (regra obrigatória), Documentação, graphify, Implementation status, Per-tenant config (what each clinic can configure), Product vision (+6 more)

### Community 81 - "Conversation"
Cohesion: 0.02
Nodes (301): Conversation, A patient <-> clinic conversation and its handover state., True only when the agenda about to be WRITTEN proves [start, end) is already tak, slot_taken_at_confirm(), _accepts_plan(), _appointment_local_start(), _appt_duration_minutes(), _appt_row_label() (+293 more)

### Community 82 - "test_flow_day_picker.py"
Cohesion: 0.10
Nodes (55): _appt(), _Calendar, _calendar_service(), _control_tap(), _conversation(), _day_tap(), SimpleNamespace, The reusable day/time branch (PROMPT_FEAT_32 part B).  Two layers, mirroring h (+47 more)

### Community 84 - "LocalCalendarService"
Cohesion: 0.05
Nodes (57): Lock, CalendarEvent, One timed event on one secretarIA agenda (the clinic's or one doctor's)., CalendarEventNotFoundError, The secretarIA agenda holds no live event with this id - its 404/410.      `is, _advisory_key(), _advisory_lock(), _agenda_key() (+49 more)

### Community 85 - "CHECKPOINT — Limites de texto do WhatsApp (constante única + corte marcado + trava no hub)"
Cohesion: 0.07
Nodes (27): §1 — Por que NÃO é corte por fronteira de palavra, §2 — O que entrou onde, §3 — O que NÃO mudou (de propósito), §4 — Segunda rodada (2026-08-22): os outros campos, e a própria LLM, §5 — Terceira rodada (2026-08-30): emoji nos botões e nas listas (FEAT 44), A armadilha maior: o matcher, não o render, A decisão, A LLM agora sabe o número (+19 more)

### Community 86 - "CHECKPOINT — Onboarding & multi-professional configuration"
Cohesion: 0.17
Nodes (11): CHECKPOINT — Onboarding & multi-professional configuration, Crons (`workers/onboarding_cron.py`, registered in `workers/arq_worker.py`), Email (`services/email.py`), Honesty notes, Hub additions, Internal provisioning surface (`api/internal_provisioning.py` + `services/provisioning.py`), Migrations added this round (chain), Per-professional agent runtime (+3 more)

### Community 87 - "build_patient_calendar_link"
Cohesion: 0.13
Nodes (26): build_patient_calendar_link(), The "adicionar à minha agenda" link to send the PATIENT. Pure, no I/O.      NO, _params(), Unit tests for `services/calendar.py::build_patient_calendar_link` (pure, no net, The digits in the URL are UTC, never the clinic's wall clock, and never     acc, Both booking paths hand over aware values; this is the contract that     keeps, tz localizes naive values only — it must never re-stamp an aware one., No knowable instant. Raising beats emitting a link that is quietly wrong     by (+18 more)

### Community 88 - "insurance_catalog.py"
Cohesion: 0.06
Nodes (80): InsuranceCatalog, InsuranceCatalogUnmatched, ProfessionalInsurancePlan, Convênio (health-insurance plan) catalog: global list + per-clinic + per-doctor., One canonical convênio operator, shared by every tenant., A convênio this clinic accepts: a catalog entry, or the clinic's own "Outro"., What ONE doctor accepts. Shape depends on `Tenant.insurance_mode` — see     the, A clinic's legacy convênio text that has never matched a catalog entry.      T (+72 more)

### Community 89 - "test_convenio_catalogo_db.py"
Cohesion: 0.08
Nodes (62): NotClinicPlans, professional_plan_ids(), A doctor (clinic_with_exceptions) was asked to accept plans the clinic     has, Back-compat alias of `professional_accepted_tenant_plan_ids` (pre-TASK-008 name), Replace the clinic's CATALOG-BACKED plan set with `plans` = [(catalog_id,     c, `clinic_with_exceptions`: replace the clinic-plan subset THIS doctor accepts., set_professional_clinic_plans(), set_tenant_plans() (+54 more)

### Community 90 - "run_agent"
Cohesion: 0.08
Nodes (34): _body_digest(), _invoke_agent_with_retry(), _load_history(), _log_trace_content(), BaseMessage, UUID, `sentinel`, prefixed by the intro envelope when the agent left words for the pat, (intro, rest) - the inverse of `with_handback_intro`; (None, reply) without one. (+26 more)

### Community 91 - "test_booking_owner_persistence.py"
Cohesion: 0.16
Nodes (34): _add_appointment(), _agent_context(), db(), _FakeCalendar, _persist_flow_appointment(), Every booking persists its effective owner and a canonical service.  The tenan, Records created events; never touches Google., Exactly what ai/graph.py::run_agent sets before invoking the agent. (+26 more)

### Community 92 - "test_hub_calendar_health.py"
Cohesion: 0.22
Nodes (22): _connect_clinic(), db(), AsyncClient, Tests for GET /tenants/me/calendar/health (api/hub/oauth.py) and the service be, The production state: shared_account, clinic token rejected by Google., A 5xx or a network blip passes on its own; asking the clinic to reconnect     o, load_tenant_config resolves a single-professional clinic's credential     THROU, In shared_account every booking uses the clinic's token     (_professional_cred (+14 more)

### Community 93 - "format_brl"
Cohesion: 0.08
Nodes (31): The pt-BR warning the CLINIC reads before freeing a paid slot (not the patient n, release_warning_text(), format_brl(), parse_brl_to_cents(), BRL free-text money parsing/formatting — no currency library, no network.  `ap, Parse free-text BRL into integer cents. Returns None, never raises.      None, Render integer cents as pt-BR currency text, e.g. 125050 -> "R$ 1.250,50"., adjustment_not_applied_text() (+23 more)

### Community 94 - "test_inbound_tap_display_vs_routing.py"
Cohesion: 0.08
Nodes (35): _Calendar, _conversation_snapshot(), db(), _inbound_row(), MonkeyPatch, SimpleNamespace, A tapped row's id is routing data, never display text - end to end.  Found liv, Stands in for WhatsAppClient: records every send, reaches nothing. (+27 more)

### Community 95 - "test_subscription.py"
Cohesion: 0.11
Nodes (30): _clear_cache_and_settings(), _FakeAsyncClient, _FakeResponse, _install_fake_client(), Exception, MonkeyPatch, Tests for core/subscription.py — the doctor-hub token verification seam.  `ver, The request carries X-Internal-Api-Key and the token in the JSON body. (+22 more)

### Community 96 - "test_hub_configuration.py"
Cohesion: 0.09
Nodes (43): db(), other_tenant_professional(), professional(), AsyncClient, Tests for PUT /tenants/me/configuration — the transactional tenant + profession, Re-read from a FRESH session — the only honest way to ask what committed., The PUT response is built by the same readers the GETs use, so a client     can, PUT is idempotent: a double click, or a retry after a timed-out     response, m (+35 more)

### Community 97 - "test_convenio_catalogo_flow.py"
Cohesion: 0.15
Nodes (47): _advance(), _assert_full_list_with_marks(), _conversation(), _doctor(), _doctor_rows(), _FakeCalendar, _marked_ids(), _prof_row() (+39 more)

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
Cohesion: 0.10
Nodes (47): max_length, min_length, _appointment_dto(), _arq_pool_or_503(), _attachment_gate(), _brain_message_conversation_started(), brain_message_inbound(), brain_message_insurance_overview() (+39 more)

### Community 103 - "CHECKPOINT — Multi-doctor WhatsApp workflow (deterministic doctor selection)"
Cohesion: 0.22
Nodes (8): CHECKPOINT — Multi-doctor WhatsApp workflow (deterministic doctor selection), Decisions made in-session (not pinned by the prompt doc), Deterministic flow (`services/flow_router.py`), LLM handoff (`ai/`, `plugins/multi_professional.py`, `workers/tasks.py`), Open follow-ups, Schema (migration `b3d9f2a1c8e7`, revises `f06e85b476f2`), Shared professional resolution (`services/tenant_config.py`), Testing

### Community 104 - "CHECKPOINT — Plugin architecture (entitlement-gated capabilities)"
Cohesion: 0.22
Nodes (8): CHECKPOINT — Plugin architecture (entitlement-gated capabilities), Honesty notes (sandbox vs production), LGPD pieces that landed with this round, Migrations added this round (chain), Testes determinísticos de horário (2026-10-02), The core (tier `basico`) — was already real, now gated, The plugins, The registry (`src/secretaria/plugins/`)

### Community 105 - "cancellation_notice.py"
Cohesion: 0.11
Nodes (23): _as_utc(), build_cancellation_text(), is_inside_window(), join_blocks(), last_inbound_at(), meta_language_code(), datetime, UUID (+15 more)

### Community 106 - "test_debug_reset_menu3.py"
Cohesion: 0.13
Nodes (30): is_debug_reset_command(), True for `/menu3` alone - surrounding whitespace and letter case ignored., _bm_turn(), _count(), db(), _expected_opening(), MonkeyPatch, UUID (+22 more)

### Community 107 - "service_change.py"
Cohesion: 0.06
Nodes (89): AdjustmentHook, LookupError, DepositAdjustment, adjustment_applied_text(), adjustment_expired_text(), AdjustmentNotice, after_paid_move(), agenda_summary() (+81 more)

### Community 108 - "test_ai_v2_blindness.py"
Cohesion: 0.15
Nodes (24): _appointment(), _at(), _config(), _day(), db(), _decoy_calendar(), _google_event(), _leaks() (+16 more)

### Community 109 - "hub_configuration.py"
Cohesion: 0.09
Nodes (30): ActivationBlocked, _apply_extra_reminder(), apply_professional_config(), apply_tenant_config(), check_appointment_type_service_ids(), check_payment_settings(), check_tenant_activation(), ClinicDescriptionTooLong (+22 more)

### Community 110 - "calendar.py"
Cohesion: 0.18
Nodes (10): is_event_not_found(), is_local_event_id(), _patient_link_stamp(), BaseException, ZoneInfo, Google Calendar integration for the clinic - and the agenda-backend selection po, Whether `event_id` names an event on the secretarIA's own agenda. Pure., Whether `exc` is Google saying "no such event on THIS calendar".      Not an o (+2 more)

### Community 111 - "media_storage.py"
Cohesion: 0.09
Nodes (29): _bucket(), delete_object(), delete_object_required(), is_configured(), MediaObjectMissing, MediaStorageUnavailable, MediaStream, new_object_key() (+21 more)

### Community 112 - "CHECKPOINT — WhatsApp Coexistence onboarding (Tasks 4 + 5)"
Cohesion: 0.22
Nodes (8): CHECKPOINT — WhatsApp Coexistence onboarding (Tasks 4 + 5), Coverage verification (read, not assumed), External dependencies (not part of this round, pending before a live test), Pending, Status, Task 4 — `BOT_ALLOWLIST_WA_IDS` (test-window allowlist), Task 5 — webhook docstring, Tests

### Community 115 - "test_pix_deposit_plugin.py"
Cohesion: 0.13
Nodes (25): agent_tools_for(), enabled_plugins(), The plugin registry: id -> PluginSpec, filtered per tenant by entitlement.  St, Whether `spec`'s hooks/tools run for this tenant.      Two cases:      - `en, PluginSpecs this tenant is currently entitled to.      A disabled plugin (tena, Flatten `agent_tools` across every plugin this tenant is entitled to., _spec_enabled(), db() (+17 more)

### Community 116 - "load_simulation_inputs"
Cohesion: 0.07
Nodes (56): _conversation(), initial_state(), _copy_of(), load_simulation_inputs(), overlay_professional(), overlay_tenant(), Any, AsyncSession (+48 more)

### Community 117 - "test_webhook.py"
Cohesion: 0.39
Nodes (7): AsyncClient, Tests for the WhatsApp webhook endpoints.  These exercise only paths that need, test_get_handshake_returns_challenge(), test_get_handshake_wrong_token_returns_403(), test_health_endpoint(), test_post_invalid_signature_returns_403(), test_post_missing_signature_returns_403()

### Community 118 - "CHECKPOINT — Post-consult tenant config fields"
Cohesion: 0.29
Nodes (6): CHECKPOINT — Post-consult tenant config fields, Pendências, Qualifying turns (`workers/tasks.py`), Testing, The injection seam (`post_consult_knowledge` only), What was built

### Community 119 - "add_warnable_row"
Cohesion: 0.12
Nodes (39): add_warnable_row(), datetime, UUID, Rows and clinic setup shared by the TASK-032 R4 warning tests.  Builds the row, set_contact_email(), The warning tick: e-mail content, once-only, and the failure modes (TASK-032 R4,, test_a_booking_for_someone_else_names_the_attendee_and_the_holder(), test_a_burst_after_an_outage_sends_one_email_for_the_appointment() (+31 more)

### Community 120 - "insurance.py"
Cohesion: 0.09
Nodes (48): _bad_ids(), get_insurance_catalog(), get_insurance_mode(), get_professional_insurance_plans(), get_tenant_insurance_plans(), _independent_accepted(), _independent_selectable(), _mode_not_applicable() (+40 more)

### Community 121 - "test_booking_code_gate.py"
Cohesion: 0.07
Nodes (83): test_booking_hold_fallback_notifies_handoff(), _appointments(), _async(), _book(), _conversation_snapshot(), db(), _expire(), _FakeCalendar (+75 more)

### Community 122 - "CHECKPOINT — Inbound WhatsApp audio transcription"
Cohesion: 0.33
Nodes (5): CHECKPOINT — Inbound WhatsApp audio transcription, Config, Key design points, Pendências, What was built

### Community 123 - "CHECKPOINT — Pix deposit (sinal) add-on + appointment payment lifecycle"
Cohesion: 0.33
Nodes (5): CHECKPOINT — Pix deposit (sinal) add-on + appointment payment lifecycle, External pendências (do NOT fabricate — nothing works end-to-end until these), Flags / known gaps (deliberate, pending product decisions), State machine (`PixDeposit.status`, SEPARATE from `AppointmentStatus`), Where everything lives

### Community 124 - "config.py"
Cohesion: 0.04
Nodes (56): main(), _parse_args(), Namespace, Apply a JSON config file onto a tenant row (the doctor-hub columns).  This is, Return a list of human-readable warnings (does not block the write)., # NOTE: "greeting_buttons" is intentionally NOT in _ALLOWED_FIELDS anymore, _validate(), main() (+48 more)

### Community 125 - "test_get_availability_tool.py"
Cohesion: 0.08
Nodes (64): _Agenda, _ask(), _CalendarPerId, _canary_event(), _Clinic, _config(), db(), _doctor() (+56 more)

### Community 126 - "test_menu_command.py"
Cohesion: 0.11
Nodes (43): _clear_patient_name(), _count(), db(), _FakeWhatsAppClient, MonkeyPatch, Tests for the `/menu` (non-destructive) command family — PROMPT_FIX_18.  `/men, The whole point of the rename: no `/menu`-ish word reaches the wipe., Records every send; installed in place of the real client. (+35 more)

### Community 127 - "test_deposit_breaker.py"
Cohesion: 0.13
Nodes (31): breaker_open(), Whether the deposit step must be skipped for the account `row` at `now`: its hea, _account(), _asked_for_cpf(), Calendar, _confirm(), db(), _decide() (+23 more)

### Community 128 - "test_bot_allowlist.py"
Cohesion: 0.20
Nodes (21): _count(), db(), _echo_value(), MonkeyPatch, Tests for the Coexistence test-window allowlist (`BOT_ALLOWLIST_WA_IDS`).  Cov, Ordering fix (PROMPT_FIX_21): the allowlist runs BEFORE the     tenant-active g, The reorder must not silence the legitimate case., A redelivery of an already-dropped event hits the pre-existing     idempotency (+13 more)

### Community 131 - "test_handover_echoes.py"
Cohesion: 0.17
Nodes (28): extract_echo_body(), Return the history body for a `smb_message_echoes` (Coexistence) event.      E, db(), _echo_value(), _load_conversation(), _load_message(), _msg(), MonkeyPatch (+20 more)

### Community 132 - "test_flow_router_insurance.py"
Cohesion: 0.12
Nodes (36): _confirm_service(), _conversation(), _FakeCalendar, _plan_titles(), The convênio list pages instead of cutting plans (Instituto Gicovate, 2026-10-27, The routing text the router reads when the patient taps the row titled `title`., _tap(), test_every_plan_appears_exactly_once_and_no_page_exceeds_the_row_cap() (+28 more)

### Community 133 - "payments_cron.py"
Cohesion: 0.12
Nodes (17): arq cron wrappers of the payment jobs (services/payments/jobs.py).  Minutes ar, E-mail the clinic ONCE per item when a patient cancelled a paid consultation (an, run_attention_alerts(), run_payment_expiry(), run_payment_reconciliation(), run_payment_sweeps(), delete_provider_customer_job(), enqueue_for() (+9 more)

### Community 148 - "build_identity"
Cohesion: 0.15
Nodes (15): alembic_head(), build_identity(), Path, Normalise a commit sha to its short form so both sides compare equal.      A p, First directory containing `migrations/versions`, or None.      Checked from t, Head revision of the migration scripts shipped in THIS image.      This is the, Hash of the `secretaria` sources this process actually imported.      The poin, This process's identity. Computed once, then cached for its lifetime. (+7 more)

### Community 149 - "test_turn_safety_net.py"
Cohesion: 0.18
Nodes (15): _entitled(), _FakeRedis, The patient is never left in silence - and the net cannot be abused., _reply(), test_a_looping_sender_gets_at_most_three_apologies(), test_a_pipeline_that_sent_then_crashed_is_not_apologised_to(), test_a_turn_that_sent_something_gets_no_extra_apology(), test_apology_fails_closed_when_entitlement_cannot_be_read() (+7 more)

### Community 150 - "oauth.py"
Cohesion: 0.10
Nodes (31): RedirectResponse, calendar_disconnect(), calendar_health(), _enqueue_local_sync(), oauth_callback(), oauth_start(), _owned_professional(), _portal_redirect() (+23 more)

### Community 156 - "arq_worker.py"
Cohesion: 0.14
Nodes (15): on_shutdown(), on_startup(), arq worker entry point.  Start the worker with:     arq secretaria.workers.ar, Run once when the worker process stops., arq worker configuration.      arq reads these as plain class attributes, so `, Job names arq will accept in `enqueue_job`, in registration order.      Logged, Cron job names, in registration order. Same purpose as the above., Run once when the worker process starts. (+7 more)

### Community 184 - "reminder_engine.py"
Cohesion: 0.10
Nodes (44): _as_utc(), _chat_copy_exists(), claim_reminder(), _close(), due_reminder_ids(), _emit_usage(), _fail(), _finish() (+36 more)

### Community 220 - "test_resolve_tenant.py"
Cohesion: 0.16
Nodes (23): _all_tenants(), db(), MonkeyPatch, workers/tasks.py:_resolve_tenant - nullable phone_number_id + autoprovision gate, A NULL-phone tenant (mid-onboarding) sits in the DB - never adopted., Even an event with no phone_number_id at all must not match a NULL row., Regression guard: this exact call used to silently auto-create a tenant., No metadata.phone_number_id on the event at all - still dropped, not adopted. (+15 more)

### Community 222 - "test_ai_v2_conversations.py"
Cohesion: 0.05
Nodes (108): admits_missing_information(), asks_for_a_name(), booking_claims(), capability_promises(), emergency_guidance(), _hits(), invented_address(), invented_price() (+100 more)

### Community 227 - "scoped_help.py"
Cohesion: 0.10
Nodes (39): _get_decision_model(), _normalize(), _professional_help_prompt(), _professional_options_block(), Any, BaseMessage, UUID, Scoped LLM helper nodes for the deterministic flow's "Não sei" rows.  Two narr (+31 more)

### Community 240 - "DepositReleaseOffer"
Cohesion: 0.07
Nodes (89): DepositReleaseOffer, _as_utc(), _buttons(), carry_check_is_fresh(), _carry_not_holding(), carry_over_pending(), _carrying(), carrying_offer() (+81 more)

### Community 241 - "inbound_routing_text"
Cohesion: 0.15
Nodes (35): inbound_routing_text(), The text the flow router and the agent read for one inbound message.      Reco, The part of a data-carrying row id after its prefix, or None., _row_payload(), load_tenant_insurance(), ONE read per turn of everything the flow needs (see TenantInsurance).      Mod, _book_to_insurance_or_professional(), _count() (+27 more)

### Community 247 - "test_brain_message_recent_page.py"
Cohesion: 0.20
Nodes (22): _add_rows(), db(), _minute(), datetime, The Portal transcript shows the NEWEST messages, not the oldest 50 (LACUNAS L1)., One outbound bot row per stamp, body `m1..mN`, id `UUID(int=N)`, in the given or, Append `(kind, body, at)` rows: kind `bot` is outbound/bot, `marker` is the inbo, `count` outbound bot rows, body `m0..m{count-1}`, one minute apart, oldest first (+14 more)

### Community 248 - "test_calendar_events_insurance.py"
Cohesion: 0.10
Nodes (60): load_deposit_views(), The deposit state of a whole page of appointments in ONE query.      Read-only, _appointment(), _connect_calendar(), db(), _deposit(), _events(), other_tenant() (+52 more)

### Community 250 - "CHECKPOINT — Pseudonimização de PII antes do modelo de IA"
Cohesion: 0.12
Nodes (16): 10. Skill: nenhuma criada, e por quê, 1. O que mudou, em uma frase, 2. Onde o código está, 3.1 `check_availability` parou de expor evento alheio (2026-09-07, decisão do usuário), 3. A DECISÃO sobre o escopo das ferramentas do loop ReAct, §4.1.1 — o espaçamento foi corrigido no pacote, NÃO neste repo, 4. Duas divergências deliberadas do prompt, 5. Ordem: o mapa é salvo DEPOIS do invoke, no `finally` (+8 more)

### Community 253 - "render_greeting"
Cohesion: 0.06
Nodes (50): clinic_description_budget(), greeting_preview_template(), _identification(), _lower_first(), The product-defined first-contact greeting frame, and the LGPD consent notice., Lower-case the first letter so the description continues the sentence     ("..., The tail of "Sou a SecretarIA, assistente virtual <tail>".      "do consultóri, Render the frame for one tenant. Pure; never raises on missing input.      An (+42 more)

### Community 255 - "test_lgpd_consent_gate.py"
Cohesion: 0.17
Nodes (28): _consent_events(), db(), _inbound(), _open(), _patient(), Tests for the LGPD consent gate — the sequenced first conversation.  The shape, The opening up to the LGPD notice: first message, then the name answer.      S, Sending the notice is not consent. Only the tap is. (+20 more)

### Community 256 - "scrub_messages"
Cohesion: 0.21
Nodes (12): Any, BaseMessage, Pseudonymizer, LangChain-facing half of the pseudonymization step.  `services/pii_pseudonymiz, Wrap a tool sequence for the agent. Safe to call once per agent build.      Th, Un-tokenize the bot's reply, and shout if anything is left over.      A token, Mask every message's content, preserving its TYPE.      `model_copy` rather th, One tool, guarded on both sides. Returns the tool itself if unwrappable. (+4 more)

### Community 261 - "db"
Cohesion: 0.08
Nodes (72): CustomReplan, What `replan_custom_reminders` did (counts only - logged, never shown)., db(), make_appointment(), make_patient(), _make_tenant(), other_tenant(), datetime (+64 more)

### Community 264 - "test_brain_message_open.py"
Cohesion: 0.07
Nodes (67): _bodies(), _claim_held(), _consent_kinds(), _conversation(), db(), _menu_body(), _NoIdentityWrites, _open() (+59 more)

### Community 273 - "AppointmentReminder"
Cohesion: 0.06
Nodes (83): AppointmentReminder, A reminder of one appointment version (see the module docstring)., _arm(), _as_utc(), cancel_reminders(), _check_staff_kind(), current_staff_notice_row(), display_state() (+75 more)

### Community 274 - "test_appointment_edit_flow_menu.py"
Cohesion: 0.13
Nodes (31): enter_edit_menu(), The entry from the reminder's "Alterar Dados": a fresh draft + the 5-option menu, slots(), test_slots_helper_is_importable_for_the_next_task(), _asked_draft(), _labels(), The edit flow: menu, date, time and the complete confirmation (TASK-032 R6, spec, 2026-10-26: the service may change with a paid sinal (the difference is settled (+23 more)

### Community 276 - "test_agent_capability_cache.py"
Cohesion: 0.09
Nodes (22): manage_existing_appointment(), Aciona o fluxo de remarcação/cancelamento de uma consulta JÁ MARCADA     deste, _clean_agent_cache_and_fakes(), _empty_history(), _fake_tool_a(), _fake_tool_b(), _FakeCompiledAgent, MonkeyPatch (+14 more)

### Community 284 - "TASK-008 — Modos de aceitação + catálogo extensível + convênio "Outro""
Cohesion: 0.07
Nodes (28): 10.10 Ordem de deploy obrigatória (achado do Reviewer, severidade HIGH — ler antes de autorizar), 10.1 Schema (migração `d4f8a2c6e913`, revises `c9f4e2a7b815`), 10.2 Serviços (`services/insurance_catalog.py`), 10.3 `flow_router.py`, 10.4 `services/payments/deposit_lifecycle.py`, 10.5 Contrato do hub — `api/hub/insurance.py` (substitui parte da seção 4), 10.6 Admin (SaaS-owner) — `api/admin/insurance.py` (NOVO), 10.7 `PUT /tenants/me/config` — `insurances` removido (+20 more)

### Community 287 - "test_list_patient_appointments_tool.py"
Cohesion: 0.25
Nodes (13): _agent_context(), db(), _patch_session_factory(), datetime, MonkeyPatch, Tests for ai/tools.py:list_patient_appointments — the read-only agent tool.  M, Tenant + patient + conversation, with a mix of appointments:      future SCHED, _seed_with_appointments() (+5 more)

### Community 288 - "test_ai_toolset_v2.py"
Cohesion: 0.07
Nodes (44): _blocked(), cancel_event_v2(), _clinic_zone(), create_event_v2(), _own_upcoming(), _parse_start(), date, time (+36 more)

### Community 289 - "reload_appointment"
Cohesion: 0.15
Nodes (51): A refusal the hub maps to HTTP: `status_code`, machine `code`, Portuguese `messa, StaffEditError, reload_appointment(), _adjustments(), _busy_between(), calendar_for(), _dearer_but_shorter(), _edit() (+43 more)

### Community 293 - "FakeProvider"
Cohesion: 0.03
Nodes (64): ChargeState, CreatedCharge, new_charge(), UUID, Shared test helpers for the `before` timing (TASK-017). Not a test module.  `W, World, FakeProvider, A `PaymentProvider` with scripted answers, registered with     `monkeypatch.set (+56 more)

### Community 294 - "internal_provisioning.py"
Cohesion: 0.09
Nodes (32): activate(), attach_professional(), config_status(), notifications_email(), provision_tenant(), AsyncSession, Request, _TENANT_PATH (+24 more)

### Community 296 - "_seed"
Cohesion: 0.18
Nodes (25): db(), _flow_state(), _inbound(), MonkeyPatch, timedelta, Tests for the universal floor on how long a conversation stays in LLM mode.  `, The bug: nothing used to bound this cohort's stay in LLM mode.      The releas, Mid-conversation stickiness is the point of FlowState.LLM — keep it. (+17 more)

### Community 297 - "sent"
Cohesion: 0.09
Nodes (72): menu_label(), The question shown above the menu buttons., (id, label) pairs of a reminder: Confirmar / Cancelar / Alterar Dados., reminder_buttons(), age_conversation(), chat_rows(), _patient_ref(), datetime (+64 more)

### Community 298 - "test_human_backup_plugin.py"
Cohesion: 0.16
Nodes (24): InboundContext, What an `on_inbound` hook needs to decide + act on one inbound turn.      Hook, Run `on_inbound` hooks for every plugin this tenant is entitled to.      Stops, run_on_inbound(), _ctx(), db(), _fakes(), _FakeWhatsAppClient (+16 more)

### Community 299 - "test_hub_conversations.py"
Cohesion: 0.06
Nodes (90): _bot(), db(), _ExplodingHandoverManager, _FailingBrainMessageSender, _FailingWhatsAppClient, _FakeWhatsAppClient, _get_conversation_row(), _get_message_rows() (+82 more)

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
Cohesion: 0.19
Nodes (15): _appointment(), _FakeCalendarService, _patient(), AsyncClient, datetime, Hub calendar endpoints call the reminder hooks (TASK-032 R2). Harness copied fro, _switch(), tenant() (+7 more)

### Community 306 - "AgendaViewer"
Cohesion: 0.11
Nodes (46): AgendaViewer, UUID, Who may see which appointments in the hub agenda (TASK-044 R7, spec 2026-10-09 §, The hub session's agenda visibility.      `professional_id` is a Professional, True = only the appointments of `professional_id` exist for this viewer., A clinic-wide viewer who is also a professional: the "Todos / Só os meus" switch, Whether an appointment owned by `professional_id` exists for this viewer., brain-api's answer -> the viewer. `agenda_scope` None = brain-api did not say. (+38 more)

### Community 307 - "Professional"
Cohesion: 0.05
Nodes (95): apply_groups(), collect_groups(), _Group, look_alike_pairs(), main(), _print_report(), UUID, Populate the canonical service catalog from the existing JSON entries.  DELIVE (+87 more)

### Community 308 - "analytics_advanced"
Cohesion: 0.16
Nodes (16): analytics_advanced(), analytics_summary(), _as_aware_utc(), AsyncSession, datetime, Doctor hub — analytics endpoints.  - GET /tenants/me/analytics/summary  (basic, Normalize a stored `created_at` to an aware UTC datetime.      `AnalyticsEvent, The last `months` "YYYY-MM" keys ending at `now`, oldest first (dense). (+8 more)

### Community 309 - "test_emoji_decoration.py"
Cohesion: 0.12
Nodes (12): _label_match_body(), True when `body` equals `label` or its 20-char button truncation.      Normali, FEAT 44 - which surfaces wear an emoji, and that decorating broke no matcher., _services(), _tenant(), test_a_service_name_that_is_already_truncated_stays_bare(), test_a_tap_on_a_decorated_service_row_still_resolves_to_the_catalog_entry(), test_both_service_catalogs_render_a_name_identically() (+4 more)

### Community 310 - "BookingDraftRequested"
Cohesion: 0.05
Nodes (59): BookingDraftRequested, manage_existing_appointment_v2(), ManageAppointmentRequested, Raised by `manage_existing_appointment`: the patient wants to reschedule     or, Entrega o agendamento ao fluxo guiado com TUDO o que o paciente já disse nesta, Leva o paciente ao fluxo de remarcar ou cancelar uma consulta JÁ MARCADA dele. O, Raised by `set_booking_draft` (v1 and v2): the agent resolved what the patient w, set_booking_draft_v2() (+51 more)

### Community 311 - "request_precheck_handoff"
Cohesion: 0.16
Nodes (17): iniciar_pre_consulta(), Inicia a pré-consulta (anamnese) do paciente desta clínica, encaminhando-o, _context_field(), _log_outcome(), _phone_hash(), UUID, Precheck hand-off — asks brain-api (the mesh hub) to pre-seed a PreCheck conver, Short, non-reversible fingerprint of a phone number, safe to log. (+9 more)

### Community 312 - "CHECKPOINT — Anexos no Portal (Brain-Message), parte 2: secretarIA (armazenamento, envio e recebimento)"
Cohesion: 0.10
Nodes (20): 1. Pré-requisito e causa reconfirmada, 2. O que entrou (âncoras estáveis), 3.1 `POST /internal/brain-message/inbound` — variante multipart (brain-api → secretarIA), 3.2 Registro gravado (`messages.attachment`), 3.3 Listagens, 3.4 Mídia (bytes em stream, nunca URL), 3.5 Staff envia arquivo, 3.6 Recusas (corpo `{"detail": {"code", "message"}}`, + `max_bytes` no 413) (+12 more)

### Community 313 - "test_admin.py"
Cohesion: 0.23
Nodes (15): _fake_wipe(), AsyncClient, MonkeyPatch, Tests for the admin reset endpoint and the X-Admin-Token guard., Replace the destructive coroutine with a recorder.      Tests must never reach, test_current_token_still_accepted_when_previous_is_configured(), test_default_wipe_keeps_tenants(), test_endpoint_is_in_openapi_schema() (+7 more)

### Community 314 - "BookingDraft"
Cohesion: 0.06
Nodes (95): BookingDraft, draft_from_record(), The parked draft, or None when absent, corrupt or older than the TTL.      `no, What the AI says the patient asked for. Every field optional; None = not said., Regression: the current (flag-OFF) handback must carry day and who, too., test_ambiguous_doctor_correction_never_reuses_the_previous_doctor(), test_doctor_removed_before_fresh_resolution_is_not_replaced_implicitly(), test_full_explicit_request_reaches_confirmation_even_with_rollout_off() (+87 more)

### Community 315 - "CHECKPOINT — QA ao vivo do canal Brain-Message em produção (2026-09-09)"
Cohesion: 0.18
Nodes (10): 1. `patient_access_otp` ausente em `_TEMPLATES` — commit `f07ec05`, 2. Staff não conseguia responder no canal Brain-Message — commit `5b8bfdf`, 3. O que ainda NÃO foi provado ponta a ponta, 4. Deploy — os DOIS serviços, 5. Verificado ao vivo, sem mudança necessária, 6. Arquivos tocados, 7. Verificação ao vivo PÓS-DEPLOY (2026-09-09, ~15:20-15:31 UTC), 8. PreCheck pelo portal do paciente (fora deste repo) — GRANT aplicado, reteste ao vivo pendente (+2 more)

### Community 316 - "CHECKPOINT — PreCheck oferecido automaticamente logo depois do agendamento"
Cohesion: 0.13
Nodes (14): A ordem de deploy é a parte perigosa, CHECKPOINT — PreCheck oferecido automaticamente logo depois do agendamento, Deploy desta perna, `entitlement_keys=()` é deliberado, Fatia P1 (contexto do agendamento) — IMPLEMENTADA (FEAT 39, 2026-08-26), Idempotência — e por que não há job de retry, LGPD, O hook (+6 more)

### Community 317 - "secretary_system_prompt_v2"
Cohesion: 0.11
Nodes (46): _format_safety_rules(), Render the unconditional "REGRAS INEGOCIÁVEIS..." safety/tone block.      Hard, Render the v2 system prompt for one turn of `config`'s clinic.      `tool_name, secretary_system_prompt_v2(), Direct unit test of the pure helper: every non-negotiable rule from     the cor, test_format_safety_rules_contains_all_required_elements(), _config(), _fixed_clinic_day() (+38 more)

### Community 318 - "CHECKPOINT — Catálogo canônico de serviços da clínica (FEAT 35)"
Cohesion: 0.11
Nodes (18): 1. O problema, 2. A decisão de modelagem, 3. O que entrou (entrega 1), 4. Contrato para a UI (entrega 2 — frontends), 5. Testes, 6. Pendências, 7.1 O bug que travava tudo: `service_id` era descartado em silêncio, 7.2 Um `service_id` que não é da clínica agora é recusado (+10 more)

### Community 319 - "test_hub_single_doctor_setup.py"
Cohesion: 0.10
Nodes (39): _add_professional(), _configure_like_the_console(), _conversation(), db(), doctor(), _FakeEvents, _FakeGoogle, AsyncClient (+31 more)

### Community 320 - "context"
Cohesion: 0.13
Nodes (65): appointment(), context(), conversation(), draft_for(), EditCalendar, original_appointment(), professionals(), Shared fakes for the TASK-032 R6 edit-flow tests (router level, no database). (+57 more)

### Community 321 - "CHECKPOINT — Estado da mensagem (enviado / entregue / lido / falhou), parte 1: secretarIA"
Cohesion: 0.17
Nodes (11): 1. Causa (reconfirmada) — três pontos de descarte, não dois, 2. O que mudou, 3. Decisões (as do prompt, mais as tomadas aqui), 4.1 Campos novos (aditivos, com default) em cada mensagem, 4.2 Poll — **MUDANÇA DE SEMÂNTICA DE `since`**, 4.3 Rotas de leitura, 4. Contrato para as partes 2 (brain-api) e 3 (frontend), 5. Deploy (a migração vai PRIMEIRO) (+3 more)

### Community 322 - "test_hub_config_pix.py"
Cohesion: 0.23
Nodes (15): db(), AsyncClient, Tests for the six Pix-deposit policy fields on GET/PUT /tenants/me/config (api/, tenant(), test_asaas_connected_flips_true_after_key_set(), test_difference_refund_min_days_accepts_the_boundaries(), test_difference_refund_min_days_defaults_to_two(), test_get_config_exposes_six_fields_with_defaults() (+7 more)

### Community 323 - "get_session"
Cohesion: 0.06
Nodes (55): _claim_if_authenticated(), The verified claim of a caller that sent credentials, else None.      Delibera, get_current_tenant(), Authenticate the request and return the caller's Tenant row.      401 for a mi, asaas_connection(), get_session(), AsyncSession, FastAPI dependency that yields a database session. (+47 more)

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

### Community 329 - "staff_appointment_edit.py"
Cohesion: 0.09
Nodes (51): CalendarFor, SlotGuard, calendar_professional_id(), CalendarOriginUnresolved, Physical event origin, independent of the doctor who may see an appointment., None means the clinic calendar; NULL source preserves legacy doctor inference., Explicit provenance cannot identify a calendar; never guess the clinic., Any (+43 more)

### Community 330 - "PluginSpec"
Cohesion: 0.13
Nodes (25): PluginSpec, PluginSpec: the shape of one optional, entitlement-gated capability., One optional capability, gated by ANY of a set of entitlement keys.      id: t, Add (or replace) a plugin in the registry, keyed by `spec.id`., register(), _clean_registry(), _fake_ehr_tool(), _fake_pix_tool() (+17 more)

### Community 332 - "attachments.py"
Cohesion: 0.10
Nodes (28): attachment_body(), attachment_placeholder(), attachment_record(), AttachmentKind, AttachmentRefused, check_attachment(), _clean(), content_disposition() (+20 more)

### Community 333 - "get_conversation"
Cohesion: 0.04
Nodes (114): _log_discarded_attachment(), process_brain_message_inbound(), UUID, inbound - split out of workers/tasks.py (TASK-023)., Report a file whose turn this job dropped before writing its row.      The API, arq job: run one Brain-Message turn end to end.      `attachment` (optional, s, process_brain_message_enter(), arq job: a known patient entered a conversation that already has history. (+106 more)

### Community 334 - "TenantWhatsAppCredentialMissing"
Cohesion: 0.08
Nodes (37): A tenant-scoped send was attempted without that tenant's credentials.      Car, Build a client for a tenant. The DECRYPTED token is injected by the caller., The single-tenant `META_*` env scaffold, requested BY NAME.          Developme, TenantWhatsAppCredentialMissing, The inverted invariant (PROMPT_FIX_21).      This test used to assert the oppo, test_missing_tenant_token_fails_closed_not_global_scaffold(), _ExplodingHttpClient, _FakeResponse (+29 more)

### Community 335 - "HandoffOutcome"
Cohesion: 0.07
Nodes (32): AppointmentEditRequested, GuidedBookingRequested, HumanHandoffOfferRequested, HumanHandoffRequested, propose_appointment_edit(), Volta a conversa para o menu inicial de botões da clínica. Use quando o     pac, Raised by the `show_main_menu` tool: the patient wants the button menu back., Raised by `request_human_handoff`: the agent asks for a person.      Same exce (+24 more)

### Community 336 - "test_prompts_task072.py"
Cohesion: 0.14
Nodes (32): build_patient_identity(), Which identity data we hold for the patient of this turn (TASK-072).      Bool, PatientIdentityStatus, Which identity data of THIS patient we already hold (TASK-072).      Booleans, _both(), _config(), db(), _fixed_clinic_day() (+24 more)

### Community 337 - "test_patient_channel_identity.py"
Cohesion: 0.18
Nodes (20): db(), _inbound(), MonkeyPatch, Patient identity by channel - the additive half.  This pins the property that, The untouched call site produces a complete channel-addressable row., Identity by wa_id still resolves to ONE row, not a new one per message.      T, Tenant isolation survives the new key: the same phone, two clinics., A patient with no phone number gets a row - the case that was impossible. (+12 more)

### Community 338 - "test_post_booking_plugin.py"
Cohesion: 0.17
Nodes (18): _ctx(), db(), _empty_registry(), _fake_get_waba_token(), _fakes(), _make_appointment(), MonkeyPatch, Tests for the post_booking seam: plugins/base.py's PostBookingContext, registry (+10 more)

### Community 339 - "test_reminder_v2_text.py"
Cohesion: 0.06
Nodes (30): decode_action_id(), Decode one "<action>|<uuid>" id or payload, whatever carried it.      The chan, The cancel-path button ids (TASK-032 R3, spec §4.3)., test_a_longer_id_never_decodes_as_its_shorter_sibling(), test_the_cancel_path_ids_decode(), The reminder card's buttons and ids (TASK-032 R6, spec §5.1)., test_legacy_ids_from_cards_still_on_screen_still_decode(), test_remedit_decodes_like_the_other_row_ids() (+22 more)

### Community 341 - "test_professional_null_vs_empty.py"
Cohesion: 0.05
Nodes (83): BoundLogger, get_logger(), Return a bound structlog logger., _completeness_item(), _filter_active_hours(), _filter_active_types(), professional_appointment_types(), professional_business_hours() (+75 more)

### Community 342 - "_on_inbound"
Cohesion: 0.36
Nodes (7): dtime, _is_outside_business_hours(), _on_inbound(), _parse_hhmm(), datetime, human_backup_24_7 plugin: hand inbound messages to a human outside business hour, True when `now` falls outside every configured window for today.      Empty/un

### Community 343 - "Parte 2 — `start_guided_booking`: a LLM devolvendo o agendamento aos botões"
Cohesion: 0.12
Nodes (15): A correção — `services/calendar.py::build_patient_calendar_link`, CHECKPOINT — Link de agenda pro paciente + gatilho opcional da LLM pro fluxo guiado, Decisão 1 — ele NÃO pula direto pro seletor de dias, Decisão 2 — a tool é negada a tenant multi-profissional, Decisão 3 — `enter_guided_booking` é entrada PÚBLICA no `flow_router`, e não muta nada, Decisão 4 — o handler relê tudo, e a agenda certa vem da máquina que já existia, Mecanismo — o quarto uso do MESMO padrão, não um quinto padrão, O defeito (+7 more)

### Community 344 - "test_booking_min_notice.py"
Cohesion: 0.11
Nodes (21): _Agenda, _confirm(), _days(), default_12h(), _doctor(), The doctor's minimum notice for a NEW booking (Instituto Gicovate, 2026-10-27)., The production default: 12h when the doctor never configured the notice., test_an_unconfigured_doctor_gets_the_12h_default() (+13 more)

### Community 345 - "Jornada do paciente — WhatsApp e Portal, em linguagem leiga"
Cohesion: 0.09
Nodes (22): 1. Primeiro contato, 2. Escolher profissional, serviço, data e horário (mesma lógica nos dois canais), 3. Se a pessoa escreve de novo antes da consulta (no ar), 4. Lembretes automáticos, 5. Depois da consulta, 6. Quando a pessoa volta depois de já ter sido atendida (no ar), 7. Remarcar ou cancelar, a qualquer momento (no ar), 8. Calendário dinâmico (não existe ainda em canal nenhum) (+14 more)

### Community 346 - "TASK-030 P2a — testes após deploy"
Cohesion: 0.20
Nodes (9): Correção da primeira medição, Finding F1 — indisponibilidade da agenda, Pendências registradas na primeira execução (histórico), Primeira execução — método e limites, Primeira execução — verificações reais (histórico), Resultado, Reteste após correção informada pelo dono — 2026-10-06, Reteste concluído — 2026-10-06, 11:48–12:01 BRT (+1 more)

### Community 347 - "CHECKPOINT — Moldura de saudação + aviso de LGPD (2026-08-31)"
Cohesion: 0.15
Nodes (13): A quarta armadilha: decorar o rótulo do botão, A regressão do ensaio (`/dangerously-remove-context`) — 2026-09-01, A sequência exata, As três armadilhas que custaram tempo (não repita), CHECKPOINT — Moldura de saudação + aviso de LGPD (2026-08-31), `greeting_message` foi REMOVIDA (migração `e3b7c1d5a9f2`), O atributo que guarda o aceite, O gate bloqueia — mas é um FATO, não um `FlowState` (+5 more)

### Community 348 - "CHECKPOINT — FIX 32: a chave de idempotência devolvida para um retry inexistente"
Cohesion: 0.18
Nodes (10): 1. `services/email.py` — desligado não é falha, 2. `workers/tasks.py::send_cancellation_notice` — o retry passou a existir, 3. `plugins/professional_notification.py` — o reenvio virou um job, A premissa do prompt estava errada, e isso mudou a solução, CHECKPOINT — FIX 32: a chave de idempotência devolvida para um retry inexistente, Invariantes preservadas, O defeito, O que mudou (+2 more)

### Community 349 - "CHECKPOINT — E-mail ao profissional quando uma consulta é marcada"
Cohesion: 0.18
Nodes (10): A decisão: perguntar, não copiar, CHECKPOINT — E-mail ao profissional quando uma consulta é marcada, Contenção, `FIX_02` — pré-requisito já satisfeito, Idempotência — e por que não houve migração, O bloqueio real, e por que não era o que parecia, O hook, O link da agenda — correção de premissa (+2 more)

### Community 350 - "test_tasks_helpers.py"
Cohesion: 0.07
Nodes (43): _appointment_context_text(), _manage_owner_calendar_target(), UUID, Whether THIS turn's system prompt should carry the appointment context block., Render the per-turn "consultas marcadas" block for the LLM prompt.      Pure f, Whose calendar this turn's manage action acts on (pure, no I/O).      Two turn, _should_inject_appointment_context(), Substitute the `{{name}}` placeholder and tidy the spacing.      When the name (+35 more)

### Community 351 - "CHECKPOINT — alerta de profissional com configuração incompleta (FEAT 41 + FIX 34)"
Cohesion: 0.17
Nodes (11): 1. O problema: duas falhas que pareciam a mesma, 2. A separação, 3. Debounce — por que uma chave nova, 4. O endereço do médico — o brain-api é dono (corrigido pelo FIX 34), 5. Frontend (`secretarIA-frontend`) — nada a fazer, 6. Para o FEAT 42 (banner) — o sinal JÁ existe, sem endpoint novo, 7. Testes, 8. Efeito colateral encontrado durante a implementação (importante) (+3 more)

### Community 352 - "FakeRedis"
Cohesion: 0.05
Nodes (55): FakeRedis, Shared test helpers for the payment provider core (TASK-016).  Not a test modu, The few Redis calls the payments code makes (SET NX EX, INCR, EXPIRE, GET, DEL)., _ask(), _booked(), _conversation(), db(), _deposits() (+47 more)

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
Cohesion: 0.12
Nodes (24): is_known_template(), Whether `template` is an id this module can actually render.      Exists so th, test_templates_registered(), The guard `api/internal_provisioning.py` uses to refuse an unrenderable id, test_is_known_template_answers_for_the_enqueue_side(), E-mail de passagem para humano: destinatários, dedupe, retentativa (MVP Portal,, Live 2026-10-05: the mail read "Clínica: <uuid> Conversa: <uuid>"., _render() (+16 more)

### Community 357 - "test_portal_context_opening.py"
Cohesion: 0.13
Nodes (49): _appointment(), db(), _enter(), _message(), _outbound(), datetime, MonkeyPatch, timedelta (+41 more)

### Community 358 - "NormalizedPaymentEvent"
Cohesion: 0.14
Nodes (38): _adjustment_paid_late(), apply_normalized_event(), _claim_paid(), EventResult, expected_reference(), find_deposit(), _finish(), _on_adjustment_event() (+30 more)

### Community 359 - "appointment_change_notifications.py"
Cohesion: 0.13
Nodes (26): _change_ref(), _claim(), enqueue_appointment_change_notification(), _iso(), _ledger_key(), _notify(), _notify_all(), notify_appointment_change() (+18 more)

### Community 360 - "test_appointment_edit_flow_fields.py"
Cohesion: 0.19
Nodes (26): _doctor_tap(), _draft_of(), The edit flow: service, doctor, convênio and patient (TASK-032 R6, spec §5.4)., Paid sinal + reschedule limit reached: the time cannot move, so a service that d, _rows(), _step(), test_a_busy_doctor_continues_to_day_and_time(), test_a_doctor_who_does_not_offer_the_service_lists_their_services() (+18 more)

### Community 361 - "CHECKPOINT — botões e listas como dado: `messages.interactive` (2026-09-11)"
Cohesion: 0.22
Nodes (8): §1 — Causa raiz (confirmada no código), §2 — O que mudou, §3 — Decisões tomadas sem perguntar, §4 — Testes, §5 — Prova ao vivo (local, não produção), §6 — Deploy (NÃO FEITO) — ordem obrigatória, §7 — Pendências, CHECKPOINT — botões e listas como dado: `messages.interactive` (2026-09-11)

### Community 362 - "test_cancel_event_ownership.py"
Cohesion: 0.12
Nodes (24): _Calendar, db(), _Log, MonkeyPatch, The legacy `cancel_event` cancels only THIS patient's own event (TASK-030 P4, Ta, Tenant A: our patient (evt-own) and another patient (evt-other-patient).     Te, The check is per conversation, not a blanket refusal., Ownership is tenant AND patient: pointing the turn at the event's own tenant doe (+16 more)

### Community 365 - "test_onboarding_cadence.py"
Cohesion: 0.14
Nodes (27): config_reminder_due(), Whether a config reminder is due at `now`.      Never sent yet (`last_sent is, _at(), Exhaustive tests for workers/onboarding_cadence.py — the pure cadence/ due-date, D+58's next biweekly point would be D+72, which exceeds the 60-day cap., A cap that lands exactly on a checkpoint is inclusive (<=), but the     default, Config reminders keep recurring indefinitely - no D+60-style cutoff., The checkpoint that just fired must never be returned as 'next'. (+19 more)

### Community 366 - "_should_inject_post_consult_knowledge"
Cohesion: 0.17
Nodes (16): Whether THIS turn's system prompt should carry post_consult_knowledge.      Pu, _should_inject_post_consult_knowledge(), Unit tests for workers/tasks.py::_should_inject_post_consult_knowledge - the pu, Nothing to inject regardless of how "qualifying" the turn otherwise is., Conversation already in full-LLM ("Outro"/deviated) mode., The deterministic router just delegated THIS turn (e.g. the "Outro" tap)., opening_state unresolved (e.g. patient_id unknown), ordinary IDLE turn., RETURNING_NO_APPOINTMENT / HAS_UPCOMING(_SOON) are not post-consult states. (+8 more)

### Community 368 - "test_multi_professional_plugin.py"
Cohesion: 0.24
Nodes (25): _agent_context(), db(), Tests for plugins/multi_professional.py — professional-aware booking tools.  I, The professional's OWN refresh token wins over the tenant's own., On a multi-professional tenant this is the ONLY booking tool the agent     gets, _seed_tenant_and_professionals(), _summary(), _tenant_config() (+17 more)

### Community 369 - "ChannelSender"
Cohesion: 0.06
Nodes (25): Protocol, CheckedAttachment, An upload that passed `check_attachment`: its REAL kind, a safe name, its real s, ChannelSender, MediaChannelSender, BinaryIO, Outbound dispatch, one implementation per channel the clinic can be reached on., What `workers/tasks.py` needs of a channel in order to answer a patient. (+17 more)

### Community 370 - "CHECKPOINT — toque real no Portal do paciente (canal Brain-Message) (2026-09-12)"
Cohesion: 0.22
Nodes (8): §1 — O que mudou, §2 — Validação do id (entrada não confiável), §3 — Decisões tomadas sem perguntar, §4 — Testes, §5 — Deploy (NÃO FEITO) — ordem obrigatória entre repos, §6 — Pendências, §7 — Prova ao vivo LOCAL (2026-09-12, depois do §6), CHECKPOINT — toque real no Portal do paciente (canal Brain-Message) (2026-09-12)

### Community 371 - "_ctx"
Cohesion: 0.19
Nodes (11): IClinicProvider, iClinic EHR provider — STUB.  TODO: real iClinic API integration (OAuth/API-ke, STUB `EhrProvider` for iClinic — logs and returns a fake record id., _ctx(), Tests for plugins/ehr.py (provider selection) and services/ehr/iclinic.py (the s, plugins/ehr.py itself does not try/except — registry.run_post_booking does., test_configured_provider_is_invoked(), test_iclinic_stub_returns_deterministic_fake_id_and_logs() (+3 more)

### Community 372 - "Enum"
Cohesion: 0.13
Nodes (21): Enum, PixDeposit model - the sinal (deposit) payment lifecycle for a booked appointmen, activate_tenant(), ConfigStatus, connect_asaas(), connect_whatsapp(), ConnectOutcome, create_or_attach_professional() (+13 more)

### Community 374 - "test_main_menu_manage_entry.py"
Cohesion: 0.15
Nodes (24): is_main_menu(), main_menu_buttons(), True for a card that IS the main menu (`main_menu_buttons()` as drawn).      M, The buttons RENDERED under the main menu: [🗓️ Agendar, Outro], for every clinic., db(), MonkeyPatch, The main menu offers "Gerenciar consulta" to a patient with something booked., The router's own menu (`_menu_bubbles`) is what the seam personalises. (+16 more)

### Community 375 - "appointment_edit.py"
Cohesion: 0.12
Nodes (14): asks_to_edit_appointment(), build_edit_recap(), edit_menu_body(), edit_more_body(), _local_naive(), datetime, tzinfo, The pure pieces of the "Alterar Dados" flow (TASK-032 R6).  Spec: docs/superpo (+6 more)

### Community 376 - "booking_notifications.py"
Cohesion: 0.17
Nodes (20): agenda_line(), _claim(), _insurance_line(), invite_line(), _invite_patient(), _key(), _post_booking(), professional_line() (+12 more)

### Community 378 - "test_extra_reminder.py"
Cohesion: 0.10
Nodes (32): clinic_zone(), custom_due_at(), from_legacy_lead(), is_valid_send_time(), legacy_lead_minutes(), parse_send_time(), datetime, time (+24 more)

### Community 379 - "Identidade de paciente por canal — migração aditiva"
Cohesion: 0.15
Nodes (12): Assumem não-nulo e QUEBRAM com paciente sem telefone, Corrigidos, Decisão 1 — a constraint antiga foi MANTIDA, não substituída, Decisão 2 — `external_id` é NULLABLE, embora `NOT NULL` fosse o invariante melhor, Decisão 3 — o preenchimento automático é default de coluna, não mudança no call site, Identidade de paciente por canal — migração aditiva, Inventário de `.wa_id` — para o próximo prompt (pipeline), Já toleram `None` (nada a fazer) (+4 more)

### Community 380 - "test_whatsapp_limits.py"
Cohesion: 0.08
Nodes (43): decorate_and_truncate(), decorate_if_fits(), decorated_text_budget(), WhatsApp Cloud API display limits, and the ONE way we cut text down to them., Cut a reply-button title to `limit`, UNMARKED - see why below.      Buttons de, Prefix `text` only when the WHOLE of it still fits inside `limit`.      The co, How many characters of TEXT still fit beside `emoji` inside `limit`.      The, Always prefix; `text` absorbs the cost by being cut that much harder.      The (+35 more)

### Community 381 - "test_brain_message_pipeline.py"
Cohesion: 0.07
Nodes (44): api(), _bm_turn(), _call(), db(), _ExplodingWhatsAppClient, _FakeArqPool, MonkeyPatch, The second channel: one pipeline, two ways in.  What these tests are actually (+36 more)

### Community 383 - "CHECKPOINT — mensagens do console de staff (hub/conversations)"
Cohesion: 0.17
Nodes (11): 1. O que entrou, 2. Ordem de operações do POST — send-then-persist, 3. Decisões tomadas sem perguntar ao usuário (pedido explícito dele — argumento de cada uma), 4. O que NÃO mudou (fora de escopo, conforme o prompt), 5. `PIPELINE_CANAL` já tinha rodado quando cheguei aqui?, 6. Testes, 7. Arquivos tocados, 8.1 Interação com `PROMPT_BRAIN_MESSAGE_SECRETARIA_IDENTIDADE_PACIENTE.md` (rodou em paralelo) (+3 more)

### Community 384 - "test_calendar_sync.py"
Cohesion: 0.08
Nodes (45): GoogleTokenRevokedError, Google refused the stored refresh token itself (`invalid_grant`).      A SUBCL, google_id_for(), _is_conflict(), _owner_calendars(), push_future_local_events(), _push_one(), BaseException (+37 more)

### Community 385 - "BookingGate"
Cohesion: 0.11
Nodes (18): BookingGate, GateDecision, The provider call that needed the data has been made: forget all of it (D7)., Whether this conversation's hold still backs a payable Pix (what `place_hold` re, A deposit is due. Portal first proves the address (decision D12: never charge an, What the router should do with the slot the patient just confirmed.      "comm, Turns "Confirmar" into a hold + a mailed code, for a visitor who owes one., Whether `decide` may have something to say on this channel: the deposit gate (+10 more)

### Community 386 - "payer_data.py"
Cohesion: 0.05
Nodes (60): ask_text(), _aware(), discard(), exhausted_text(), expired_text(), extract_value(), _fold(), invalid_text() (+52 more)

### Community 387 - "PostBookingContext"
Cohesion: 0.14
Nodes (18): _post_booking(), analytics_bi plugin: record a minimal booking event (post_booking).  Entitleme, PostBookingContext, What a `post_booking` hook needs to react to one freshly booked appointment., _post_booking(), ehr plugin: push a freshly booked appointment to the tenant's EHR (post_booking), _claim(), _ledger_key() (+10 more)

### Community 388 - "test_r7_calendar_origin.py"
Cohesion: 0.20
Nodes (16): A start in the future, on a whole minute (the agenda never has seconds)., soon(), _created(), _legacy_hub_row(), Real event ownership regressions for hub-created doctor-owned appointments.  O, A doctor's hub row as written before 2026-10-19: clinic agenda, "clinic" origin., test_hub_creation_without_a_doctor_stays_on_the_clinic_agenda(), test_hub_doctor_creation_edit_and_release_use_the_doctors_own_event() (+8 more)

### Community 389 - "CHECKPOINT — toque em lista: título pra exibir, id pra rotear (2026-09-11)"
Cohesion: 0.29
Nodes (6): §1 — Causa raiz (confirmada no código), §2 — O que mudou, §3 — Testes, §4 — Deploy (NÃO FEITO) — ordem obrigatória, §5 — Pendências, CHECKPOINT — toque em lista: título pra exibir, id pra rotear (2026-09-11)

### Community 391 - "FakeCalendar"
Cohesion: 0.22
Nodes (11): _apply(), _demo(), FakeCalendar, main(), SimpleNamespace, Terminal harness for the deterministic flow router (cancel/reschedule).  Unlik, Mirror the worker's _apply_flow_result against in-memory state., Scripted run of the full cancel + reschedule flows (dynamic dates). (+3 more)

### Community 392 - "availability_tool.py"
Cohesion: 0.11
Nodes (27): _clinic_today(), _entry_minutes(), _fmt(), get_availability(), _names(), _note(), _pick_professional(), Any (+19 more)

### Community 393 - "fetch_professional_emails"
Cohesion: 0.14
Nodes (23): fetch_professional_emails(), UUID, secretarIA -> brain-api client for professional CONTACT data (their email).  =, `{professional_id: email}` for one tenant's linked professionals.      Keys ar, _configured(), MonkeyPatch, Request, Tests for services/brain_professionals.py — the professional-email lookup.  Mi (+15 more)

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
Cohesion: 0.17
Nodes (12): 1. O que passou a valer, 2.1 `BookingGate` é **fail-open**, e isso não é descuido, 2.2 Por que o gate viaja num `ContextVar`, 2.3 O que acontece com o card do TASK-003 e com o handoff do TASK-004, 2.4 Bug 2 — sair da espera passou a depender de existir reserva, 2.5 Bug 1 — a LLM não pode anunciar o que nenhuma tool fez, 2. Peças, 3. Prova (rodada 2026-09-20) (+4 more)

### Community 401 - "test_manage_request.py"
Cohesion: 0.13
Nodes (21): _optional_text(), parse_appointment_ref(), The bare v1 action or the v2 JSON. ValueError on anything malformed., "AAAA-MM-DD HH:MM" ("T" also accepted) as a NAIVE clinic-local datetime., _appt(), _Log, datetime, Manage request v2: wire format, the appointment reference and the tool (TASK-030 (+13 more)

### Community 402 - "minimal_event_payload"
Cohesion: 0.13
Nodes (27): minimal_event_payload(), _minimal_message(), _minimal_status(), One inbound message / echo reduced to the fields the worker reads.      `keep_, One delivery receipt reduced to what `apply_whatsapp_statuses` reads.      Kep, Reduce a raw Meta webhook body to ONLY what the arq worker reads.      The job, _FakeArqPool, _flat() (+19 more)

### Community 403 - "reminder_text.py"
Cohesion: 0.17
Nodes (26): build_reminder_body(), _clinic_tz(), _cut(), _doctor(), flatten_requirements(), local_start(), _one_line(), datetime (+18 more)

### Community 404 - "test_set_booking_draft_v2.py"
Cohesion: 0.12
Nodes (10): clinic(), _Log, _PinnedClock, set_booking_draft v2: six fields, formats only, never a name (TASK-030 P2)., Owner, 2026-10-07: "quarta às 10" said on a Wednesday at 11h is next Wednesday -, test_the_clinic_switch_picks_the_draft_tool(), test_v2_an_unresolvable_doctor_is_dropped_not_an_error(), test_v2_carries_every_field_raw_for_the_resolver() (+2 more)

### Community 405 - "test_availability_windows_scan.py"
Cohesion: 0.12
Nodes (24): _Calendar, _GoogleEvents, _GoogleService, _hhmm(), _many_windows(), date, The scan behind get_availability: windows from the real free-time code (TASK-030, `per_day` separate windows a day: one free 30-minute slot every two hours from 0 (+16 more)

### Community 407 - "load_pseudonymizer"
Cohesion: 0.23
Nodes (13): _attendee_names(), current_pseudonymizer(), load_pseudonymizer(), persist_pseudonymizer(), Pseudonymizer, UUID, DB layer of the PII pseudonymization step: load, carry and persist the map.  T, Pin a just-captured attendee name into this conversation's token map.      Cal (+5 more)

### Community 408 - "canonical_service_name"
Cohesion: 0.09
Nodes (38): canonical_service_name(), _norm(), Any, UUID, Booking scope: WHO owns a booking, WHICH service it is — one rule, everywhere., Name of one catalog entry, whichever shape it travels in.      The effective c, Every non-empty name in `services`, in catalog order., The catalog's OWN spelling of the service `candidate` refers to, or None. (+30 more)

### Community 411 - "test_before_fulfillment.py"
Cohesion: 0.15
Nodes (37): _apply_paid_event(), db(), _expiring_charge(), _fulfill(), _paid_charge(), The promotion of a paid reservation (services/payments/fulfillment.py, booking_p, A real `before` charge, then marked paid the way the event core does it., Google holds OUR event (id = deposit hex) but the appointment row was never writ (+29 more)

### Community 412 - "CHECKPOINT — Portal: a conversa mostra as mensagens MAIS RECENTES (TASK-028)"
Cohesion: 0.17
Nodes (11): 1. Causa (L1), 2. O que mudou e onde, 3. O contrato (rota `GET /internal/brain-message/conversations/{external_id}/messages`), 4. Ordem de deploy, 5. Como desfazer, 6. Validação (evidência do implementador da Task 1; nada foi rerodado nesta rodada de documentação), 7. Prova ao vivo — PENDENTE, 8. Não afeta o WhatsApp — por quê (+3 more)

### Community 413 - "seed_paid_deposit"
Cohesion: 0.17
Nodes (29): add_appointment(), seed_paid_deposit(), test_edit_guards_read_the_deposit_and_the_limit(), _deposit_and_tenant(), _future(), datetime, Direct Cancelar and legacy edit cards (TASK-032 R3 compatibility after R6).  T, _tap() (+21 more)

### Community 414 - "test_brain_patients.py"
Cohesion: 0.17
Nodes (17): _load(), _printed(), The Asaas measurement script must be incapable of echoing a key, a document or A, _response(), test_emit_refuses_any_free_text_field(), test_error_text_is_reduced_to_true_false_flags(), test_onboarding_states_are_fixed_words_only(), test_sandbox_key_targets_the_sandbox_host() (+9 more)

### Community 416 - "confirmation_warnings.py"
Cohesion: 0.14
Nodes (25): _agenda_link(), _appointment_conditions(), claim_warning(), _clinic_day(), due_candidates(), _notify_clinic(), _patient_label(), process_confirmation_warnings() (+17 more)

### Community 418 - "Lembretes R7 — Ações da clínica na agenda avisam o paciente (API) Implementation Plan"
Cohesion: 0.08
Nodes (24): Consumes (verified against `origin/main` 5e66c9d), Decisions taken where the spec is silent (for the owner), File Structure, Global Constraints, Lembretes R7 — Ações da clínica na agenda avisam o paciente (API) Implementation Plan, Produces for R5 (front) — exact wire shapes, Review Focus, Task 10: The clinic's edit — validate, move Google fail-closed, write the row (+16 more)

### Community 419 - "check_deploy_parity"
Cohesion: 0.15
Nodes (17): BuildIdentity, check_deploy_parity(), compare_build(), publish_build_identity(), Any, ParityVerdict, Build identity of the running process — the deploy-parity proof (FIX_01 §5.1/§5., Are these two processes running the same code?      Both `build_sha` and `sour (+9 more)

### Community 429 - "CalendarService"
Cohesion: 0.07
Nodes (31): CalendarService, datetime, timedelta, Whether anything on THIS agenda already overlaps [start, end).          Only t, Invite `email` to an existing event; Google notifies them. Idempotent., Delete an event by id. 404/410 are treated as success (idempotent).          R, The event as Google holds it, or None when THIS calendar has no such id (404/410, Whether `event_id` is a secretarIA-agenda id THIS Google agenda must route. (+23 more)

### Community 444 - "Pipeline channel-neutro + endpoints internos do Brain-Message"
Cohesion: 0.18
Nodes (10): A bomba-relógio que este round desarmou, A pergunta da §1 do prompt — respondida, Inventário de `.wa_id` do prompt anterior — o que foi de fato ajustado aqui, Limitação conhecida do endpoint de leitura, O que entrou, Pendências, Pipeline channel-neutro + endpoints internos do Brain-Message, Provas (+2 more)

### Community 447 - "CHECKPOINT — IA entra em qualquer etapa: `get_availability`, ferramentas de agenda cegas e o conjunto v2 (TASK-030 P4)"
Cohesion: 0.13
Nodes (14): 10. WhatsApp e Portal: implementação compartilhada e prova local, 11. Validação, revisão e limites da prova, 12. Integra??o com os ajustes de atendimento da main (2026-10-07), 13. Deploy comprovado e reteste real — 2026-10-07, 1. O que muda, 2. Onde está cada peça (âncoras), 3. O que a IA recebe, 4. As paredes da cegueira (+6 more)

### Community 448 - "CHECKPOINT — pergunta de nome na abertura (WhatsApp + Portal) e ramo "e-mail já cadastrado" (2026-09-21)"
Cohesion: 0.17
Nodes (11): §1 — Causa raiz (confirmada no código antes de programar), §2 — A sequência nova, §3 — Cópia exata das mensagens (`services/patient_name.py`), §4 — Regra de validação do nome (`parse_patient_name`), §5 — Contrato consumido (brain-api parte 1) e compatibilidade, §6 — Decisão: o ramo "e-mail já cadastrado" REAPROVEITA `AWAITING_EMAIL_CODE`/`verify_code`, §7.1 — PII: respostas que NÃO viraram `Patient.name` (achado 3 do Reviewer), §7 — TTL de silêncio nos dois canais (+3 more)

### Community 453 - "_card_notice"
Cohesion: 0.14
Nodes (28): NoticeResult, buttons_allowed(), _card_notice(), confirm_text(), _confirmation_version(), _doctor(), edit_text(), notify_attended() (+20 more)

### Community 457 - "GoogleScopeInsufficientError"
Cohesion: 0.13
Nodes (21): GoogleScopeInsufficientError, HttpError, _raise_if_scope_insufficient(), _raise_if_unavailable(), Translate an outage/auth HttpError into CalendarUnavailableError.      Returns, A 403 caused by a token that predates a scope this call needs.      Raised by, Translate a scope-insufficient 403 into GoogleScopeInsufficientError.      MUS, _http_error() (+13 more)

### Community 458 - "message_status.py"
Cohesion: 0.17
Nodes (19): apply_whatsapp_statuses(), failure_reason(), mark_read(), meta_timestamp(), _outbound_ids(), AsyncSession, datetime, UUID (+11 more)

### Community 464 - "test_reminder_v2_delivery.py"
Cohesion: 0.20
Nodes (23): deliver_reminder(), Send one reminder. Never raises: every failure is an outcome., _job(), mail(), services/reminder_delivery.py — one reminder, the patient's own channel (TASK-03, Portal e-mail: brain-api answers an address; the mailer records the send., test_a_crash_reports_the_patients_own_channel(), test_a_meta_outage_is_a_retryable_failure() (+15 more)

### Community 465 - "allowlisted"
Cohesion: 0.16
Nodes (23): allowlisted(), _clean_plain(), _clean_records(), _log_name(), Any, What a v2 tool may hand the model: a declared allowlist of output keys (TASK-030, A key as it may appear in a log line: a plain identifier, else "<other>"., A declared key that is not a record list: scalars or a list of scalars only. (+15 more)

### Community 490 - "CHECKPOINT — Portal: a automação fala primeiro, o aviso do código vira card, e o PreCheck sai pelo Portal"
Cohesion: 0.22
Nodes (8): 1. `POST /internal/brain-message/open` — a conversa nasce sem inbound, 2. O aviso do código vira card de 3 botões, com o e-mail mascarado, 3. Handoff do PreCheck a partir do Portal, CHECKPOINT — Portal: a automação fala primeiro, o aviso do código vira card, e o PreCheck sai pelo Portal, O invariante que MUDOU (e está registrado nos dois módulos), O que NÃO foi provado, Ordem de deploy (nada disto foi executado), Validação

### Community 491 - "_flow_handback_tools"
Cohesion: 0.22
Nodes (19): _flow_handback_tools(), This turn's `extra_tools`: the plugin set + the flow hand-back tools.      `se, _config(), _mentioned(), _pinned_day(), MonkeyPatch, Which system prompt a turn gets (TASK-030 P5): ai/graph.py::_turn_system_prompt., Every tool name a turn can be built with, v1 or v2 - read off the real objects. (+11 more)

### Community 492 - "test_ai_tool_output_allowlist.py"
Cohesion: 0.13
Nodes (16): _leaky_list_units(), _Log, _menu(), The v2 output allowlist (TASK-030 P4): every v2 tool answers only the keys it de, Stand-in with a declared name that leaks an undeclared key., A sync tool would bypass the filter., A multi clinic with no extras has the same three scope-free NAMES on v1 and v2., _summary() (+8 more)

### Community 493 - "CHECKPOINT — conta ativa do Portal abre clínica nova: sem e-mail/código; nome vem da conta ou é perguntado uma vez (2026-09-24)"
Cohesion: 0.22
Nodes (8): §1 — Ponto de entrada (confirmado no código), §2 — Decisões, §3 — Testes, §4 — Validação (comandos e resultados reais, 2026-09-24, rodada 2), §5 — Achados laterais, §6 — Deploy e pendências, §7 — Nome do paciente na saudação inicial (Portal) — 2026-09-25, CHECKPOINT — conta ativa do Portal abre clínica nova: sem e-mail/código; nome vem da conta ou é perguntado uma vez (2026-09-24)

### Community 494 - "CHECKPOINT — Agendar para outra pessoa ("Essa consulta é pra você?")"
Cohesion: 0.17
Nodes (11): CHECKPOINT — Agendar para outra pessoa ("Essa consulta é pra você?"), Coordenação, Correção 2026-10-01 — "Essa consulta é pra você?" repetida após o retorno da IA, LLM, O que o paciente vê, Onde o nome vive, PII — por que nenhum código novo de mascaramento, Quem usa o nome (+3 more)

### Community 495 - "CHECKPOINT — OTP / reset link leaking through arq's native job log"
Cohesion: 0.25
Nodes (7): 1. Causa raiz (reconfirmada na versão instalada, `arq==0.28.0`), 2. A correção, 3. Testes (`tests/test_arq_log_secret_redaction.py`), 4. Outros loggers de terceiro (passo 3 do prompt), 5. Pendências, 6. Revisão de segurança (2026-09-21), CHECKPOINT — OTP / reset link leaking through arq's native job log

### Community 496 - "graph.py"
Cohesion: 0.10
Nodes (26): main(), _print_tool_calls(), BaseMessage, Terminal agent loop for SecretarIA (Fase A smoke test).  Uses the SAME LangGra, Surface tool invocations so the dev can see what the LLM decided., base_tools_for(), build_agent(), effective_tools() (+18 more)

### Community 497 - "pending_identity.py"
Cohesion: 0.06
Nodes (50): booking_gate_body(), _booking_gate_details(), booking_gate_reprompt_body(), cancel_pending_code(), claim_email(), code_notice_body(), existing_account_code_body(), IdentityState (+42 more)

### Community 498 - "_seed"
Cohesion: 0.14
Nodes (21): One appointment, optionally owned by a professional and linked to a     patient, The patient has NOT been told. That is the doctor's choice (they were     shown, No conversation at all — a free-form send would just be rejected., arq retries jobs. Inside the window a duplicate is inbox noise; outside     it, A claim is a lock, not a receipt — but giving the lock back is only half     of, Outside the 24h window every send is billed, so the retry path is the one     p, The message already went out. A metering blip must not reach the retry     path, Count-only observability: ids and reasons, never patient content.      `capsys (+13 more)

### Community 499 - "apply_appointment_edit"
Cohesion: 0.11
Nodes (24): appointment_email_version(), Opaque revision of email-relevant booking data; never export their values to a j, AsyncSession, UUID, Write an edit onto the SAME appointment row (TASK-032 R6, shared with R7).  Tw, The appointment row as an `EditDraft` in the clinic's zone (current == original), Apply `edit` (R6's dict shape) to the caller-locked row; returns what changed., row_draft() (+16 more)

### Community 500 - "staff_transition"
Cohesion: 0.20
Nodes (12): datetime, UUID, Appointment status transitions — the one observability seam (PROMPT_FIX_16)., True = apply `target`; False = a repeated attended/no_show (a no-op). Pure., The PATCH would contradict the appointment's state (spec 2026-10-09 §1/§3)., staff_transition(), StaffTransitionRefused, test_a_cancelled_appointment_never_comes_back() (+4 more)

### Community 501 - "CHECKPOINT — Onda 4: jornada sem gate ponta a ponta (E2E, sem PreCheck)"
Cohesion: 0.29
Nodes (6): Achados — divergências entre CHECKPOINT e realidade (uma linha cada, sem investigação de causa), CHECKPOINT — Onda 4: jornada sem gate ponta a ponta (E2E, sem PreCheck), Comandos de teste rodados (repetíveis), Matriz de cenários — resultado, Pendências para uma sessão futura, Veredito dos Achados 10 e 11 (fechamento 2026-09-24)

### Community 502 - "test_entitlements_client.py"
Cohesion: 0.10
Nodes (26): is_entitled(), Whether `summary`'s tenant is entitled to `key`.      `key` is either an addon, _body_for(), _configured_settings(), _FakeAsyncClient, _FakeRedis, _FakeResponse, _install_fake_client() (+18 more)

### Community 503 - "test_bot_reply_gating.py"
Cohesion: 0.19
Nodes (21): _days_from_now(), db(), _flows_of(), datetime, Tests for the entitlement gate + per-tenant WhatsApp client wiring inside worke, A sessionmaker bound to a shared in-memory sqlite DB (StaticPool keeps     the, Owner, 2026-10-06: the "Outro" tap is answered by the flow with the fixed     ", Replaces the old `..._when_flows_disabled` case, whose premise is gone.      F (+13 more)

### Community 505 - "d4f8a2c6e913_insurance_mode_catalog_ext_custom.py"
Cohesion: 0.25
Nodes (10): _backfill_unmatched_insurance_strings(), downgrade(), _normalize(), insurance mode + extensible catalog + convênio "Outro"  TASK-008 (docs/CHECKPO, Re-target `appointments.insurance_plan_id` at `tenant_insurance_plans.id`., # NOTE: rows created under independent/custom modes (catalog_id, Best-effort reverse of `_repoint_insurance_plan_id` for dev/test only.      Ma, _repoint_insurance_plan_id() (+2 more)

### Community 506 - "Digitando — backend universal (TASK-033)"
Cohesion: 0.29
Nodes (6): Contrato e ciclo de vida, Decisões e limites, Deploy e limites da prova local, Digitando — backend universal (TASK-033), Estado, Validação

### Community 507 - "test_ia_atende_como_pessoa.py"
Cohesion: 0.09
Nodes (22): _offer_pending(), TASK-038 (owner, 2026-10-07): the secretary serves like a person, a human comes, T10/T11: a question is not a failed date - and "ter" in a sentence is not Tuesda, Review of TASK-038: the handover writes the flow fields BEFORE the staff alert r, Owner, 2026-10-07: "Não" continues the free conversation - no menu, and a     b, The offer goes out on WhatsApp as reply buttons with ids menu|0 / menu|1     (w, Owner, 2026-10-29: the deposit flow's offer (the time was not booked) asks its o, T4 of the live tests: "qual o endereço?" used to become the convênio. (+14 more)

### Community 528 - "Lacunas encontradas"
Cohesion: 0.15
Nodes (12): Comportamentos que funcionaram (não regredir), L1 — Conversa com mais de 50 mensagens congela no Portal  (ALTA, causa raiz provada), L2 — Duas conversas para o mesmo paciente após verificar o código  (ALTA), L3 — A LLM promete o que não pode fazer  (MÉDIA), L4 — Perguntas factuais comuns não têm onde buscar resposta  (MÉDIA), L5 — Vazamento de rótulo de botão como se fosse serviço/opção  (BAIXA), L6 — Sem limite de entrada no Portal  (MÉDIA, abuso/custo), L7 — Sem indicação de "digitando"/progresso  (BAIXA) (+4 more)

### Community 529 - "test_brain_message_email_otp_inline.py"
Cohesion: 0.10
Nodes (19): parse_email(), The address in `body`, normalized — or None if it does not look like one., The two inline identity steps, and the WhatsApp path proving it never sees them., 20 code units is the cap `interactive_buttons_record` applies.      Pinned bec, No mask must never become an invented address, or an empty slot., Additive field, additive behaviour: absent is SENT without a mask., A sentence CONTAINING an address is not an answer to "qual é o seu e-mail?"., A None is what lets a patient say something else instead of the code. (+11 more)

### Community 531 - "File Structure"
Cohesion: 0.15
Nodes (12): "Digitando…" universal — Backend (secretarIA + brain-api) — Implementation Plan, File Structure, Global Constraints, Pendências (registradas, fora do escopo), Review Focus, Task 1: Serviço do indicador (Redis, fail-open, por quem digita), Task 2: A automação liga e desliga a marca (worker), Task 3: A listagem do Portal informa `typing`, `typing_by`, `accepts_typing` (+4 more)

### Community 548 - "Referência — como convênios de saúde pagam a consulta no Brasil (2025/2026)"
Cohesion: 0.22
Nodes (8): Caveats — ler antes de usar qualquer valor como definitivo no seed do catálogo, Por que isso importa para o produto (não é só contexto de fundo), Referência — como convênios de saúde pagam a consulta no Brasil (2025/2026), Tabela 1 — modelo de remuneração por operadora (para classificar cada linha do catálogo), Tabela 2 — honorário por consulta (prestador credenciado), amostra RJ (CREMERJ, 19/09/2025), Tabela 3 — reembolso ao paciente por categoria de plano (livre escolha), Tabela 4 — coparticipação (não é sinal, é cobrança própria do convênio — informativo), TL;DR da pesquisa

### Community 549 - "AsaasProvider"
Cohesion: 0.13
Nodes (50): AsaasError, AsaasProvider, A non-2xx response from Asaas. Carries the status code + an optional     short, _account(), _install(), _payment(), FakeAsaasClient, _qr_calls() (+42 more)

### Community 551 - "outbound_messages"
Cohesion: 0.08
Nodes (42): outbound_messages(), world(), mail(), _notice(), MonkeyPatch, send_clinic_notice: every clinic-action notice on the patient's own channel (TAS, test_a_refusing_channel_is_notice_failed_and_records_nothing(), test_long_notice_preserves_all_details_and_uses_a_short_button_card() (+34 more)

### Community 552 - "CHECKPOINT — A IA atende como pessoa; humano por último (TASK-038)"
Cohesion: 0.18
Nodes (10): 1. Texto livre que não é resposta da etapa → uma rodada da IA, na MESMA etapa, 2. A fala da IA acompanha a devolução aos botões, 3. "Não sei" mais humano, 4. Ordem de prioridade no prompt, 5. Oferta de atendente com Sim/Não (pedido do dono, 2026-10-07), 6. Ajustes depois dos testes reais (dono, 2026-10-07), Canais: WhatsApp e Portal (conferido em 2026-10-07, a pedido do dono), CHECKPOINT — A IA atende como pessoa; humano por último (TASK-038) (+2 more)

### Community 553 - "test_analytics_bi_plugin.py"
Cohesion: 0.26
Nodes (20): db(), _entitled_fake(), _patch_session_factory(), AsyncClient, MonkeyPatch, Tests for plugins/analytics_bi.py (post_booking hook) and the doctor-hub GET /t, _seed_event(), _seed_event_full() (+12 more)

### Community 554 - "File Structure"
Cohesion: 0.17
Nodes (11): "Digitando…" universal + rolagem da janela recente — Frontend (Brain-Message-Frontend) — Implementation Plan, File Structure, Global Constraints, Review Focus, Task 0: A resposta nova entra na vista (janela das 50 mais novas), Task 1: Regra pura `lib/typing.ts`, Task 2: Componente `TypingBubble`, Task 3: O teclado avisa — gancho `onTyping` no `Composer` (+3 more)

### Community 555 - "test_text_extract.py"
Cohesion: 0.08
Nodes (46): HTMLParser, clean_text(), _content_bytes(), detect_kind(), docx_to_text(), extract_upload_text(), ExtractionError, html_to_text() (+38 more)

### Community 556 - "Portal: visita descartada ao entrar na conta — TASK-034"
Cohesion: 0.25
Nodes (7): Comportamento e contrato, Estado, Ordem de deploy (ainda não autorizado), Portal: visita descartada ao entrar na conta — TASK-034, Prova em produção — pendente, Revisão independente e correções, Validação local

### Community 557 - "context_catalog_proposal.py"
Cohesion: 0.15
Nodes (31): build_catalog_proposal(), _build_insurance(), CatalogProposal, _clean(), clean_model_notes(), _clock_in(), ExtractedHours, fold() (+23 more)

### Community 558 - "CHECKPOINT — observabilidade da LLM e "paciente nunca sem resposta" (2026-10-01)"
Cohesion: 0.20
Nodes (9): 1. Rastreio do turno da LLM (`ai/trace.py`, `ai/graph.py`), 2. Rede de segurança (`services/turn_safety_net.py`, `workers/tasks.py::_send_bot_reply`), 3. Orçamento de tempo, 4. Hand-backs da IA (TASK-030, plano P1) — `workers/shared/handback_log.py`, CHECKPOINT — observabilidade da LLM e "paciente nunca sem resposta" (2026-10-01), Como investigar um caso (roteiro), O que entrou, Pendências / não feito (+1 more)

### Community 559 - "availability_windows.py"
Cohesion: 0.16
Nodes (16): DayRange, FreeWindow, _parse_day(), date, datetime, Window, Free time as WINDOWS: the only shape of an agenda the AI may be told (TASK-030 P, One continuous stretch of free time on one day, in the clinic's wall clock. (+8 more)

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

### Community 569 - "test_chat_model.py"
Cohesion: 0.17
Nodes (13): The chat vendor (ai/chat_model.py): Claude with an Anthropic key, OpenAI only wi, _Shape, test_a_leftover_llm_provider_setting_cannot_pull_the_chat_back_to_openai(), test_an_anthropic_key_gets_claude_with_its_own_budget_and_refusal_fallback(), test_big_schemas_can_opt_out_of_claudes_constrained_output(), test_claude_caches_the_system_prompt_and_openai_gets_plain_text(), test_claude_without_its_key_falls_back_to_openai_and_says_so(), test_effort_is_passed_and_haiku_gets_no_fallback() (+5 more)

### Community 570 - "test_asaas_probes.py"
Cohesion: 0.12
Nodes (35): _client(), _path(), FakeAsaasClient, The permission-probe engine (services/payments/asaas_probes.py), spec section 12, A key with exactly the three required groups; `overrides` maps a group to a repl, _refusal_flag(), _run(), _settings_with_flag() (+27 more)

### Community 571 - "availability.py"
Cohesion: 0.16
Nodes (26): at_or_after(), available_day_starts(), booking_notice_floor(), booking_notice_hours(), days_with_free_slots(), free_slots_for_day(), held_slots(), date (+18 more)

### Community 573 - "Review Focus"
Cohesion: 0.18
Nodes (10): Desenho (sem estado novo), "digitando…" no PreCheck (Portal) — Plan (reescrito em 2026-10-08, executável), Global Constraints, O que mudou desde o plano original (2026-10-01) — por isso este plano foi reescrito, Review Focus, Task 0: Autorização e baseline, Task 1: Função pura de decisão (TDD), Task 2: Leitura da última linha (única SQL nova) (+2 more)

### Community 574 - "File Structure"
Cohesion: 0.25
Nodes (7): File Structure, Global Constraints, Portal mostra as mensagens mais recentes — Implementation Plan, Review Focus, Task 1: Listagem devolve as mensagens mais recentes, Task 2: Contrato documentado, Task 3: Prova ao vivo (depois do deploy autorizado)

### Community 575 - "test_release_offer.py"
Cohesion: 0.15
Nodes (50): new_appointment(), datetime, A fresh SCHEDULED appointment of the same patient (what a rebooking/keep commit, SQLite reads timestamps back naive; every instant here is UTC., utc(), _call(), _carry(), db() (+42 more)

### Community 576 - "parse_patient_name"
Cohesion: 0.18
Nodes (12): is_name_question(), _label_key(), parse_patient_name(), The explicit "qual é o seu nome?" step, on BOTH channels.  Until this module e, A button label or an answer reduced to its letters, for equality only.      Ca, Title-case one word, keeping hyphenated and apostrophe'd parts apart., The name in a direct answer to "qual é o seu nome?", normalized — or None., Whether an outbound row asked the patient for their name.      Pure. `ai/graph (+4 more)

### Community 577 - "db"
Cohesion: 0.19
Nodes (20): _booked(), _code_notice_row(), _ctx(), db(), _patient(), CHECKLIST: appointment committed with no proven address -> the code notice., An arq retry of run_post_booking_hooks must not mail a second code., Silence is the failure mode: never promise a code that was not mailed.      Th (+12 more)

### Community 578 - "Lembrete com "Alterar Dados" Implementation Plan"
Cohesion: 0.11
Nodes (17): Deploy e liberação, File Structure, Global Constraints, Interfaces → Produces (later tasks rely on these exact names), Lembrete com "Alterar Dados" Implementation Plan, Review Focus, Self-review (done while writing; kept for the reviewer), Task 10: Checkpoint, Meta template doc, and the validation (+9 more)

### Community 579 - "Tenant"
Cohesion: 0.02
Nodes (255): A clinic using SecretarIA.      The system is multi-tenant in the data model., Tenant, enqueue_post_booking_hooks(), UUID, post_booking dispatch: react to a freshly committed appointment, off the hot pat, Fire-and-forget enqueue of `run_post_booking_hooks`. Never raises.      `redis, arq job: run every entitled plugin's `post_booking` hook for one appointment., run_post_booking_hooks() (+247 more)

### Community 581 - "test_patient_context.py"
Cohesion: 0.07
Nodes (53): PatientOpeningContext, Resolved opening state + the data that produced it.      Buckets are evaluated, _add_appointment(), db(), _FakeArqPool, _greeting_tenant(), _patch_session_factory(), datetime (+45 more)

### Community 583 - "logging.py"
Cohesion: 0.10
Nodes (19): LogRecord, Create a development tenant from environment settings.  Usage:     uv run pyt, Insert a single development tenant if one does not already exist., seed(), ArqJobArgsRedactionFilter, install_arq_log_redaction(), Structured logging setup using structlog.  JSON output in non-dev environments, Scrub a free-text log message the same way `redact_secrets` scrubs keys. (+11 more)

### Community 586 - "guard_reply"
Cohesion: 0.16
Nodes (13): _Claim, guard_reply(), _p(), Pattern, The LLM may not announce an action it did not perform.  ## The bug that produc, The action `text` claims without evidence, or None if it claims nothing., `text`, or the honest replacement when it claims an unproven action.      The, unbacked_claim() (+5 more)

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

### Community 592 - "test_payer_data_flow.py"
Cohesion: 0.12
Nodes (53): _charge_requests(), _conversation(), db(), _decide(), _deposits(), _everything_persisted(), _free(), _gate() (+45 more)

### Community 593 - "test_multi_unit_plugin.py"
Cohesion: 0.21
Nodes (15): _agent_context(), db(), _FakeCalendarService, _fakes(), MonkeyPatch, Tests for plugins/multi_unit.py — unit-aware booking tools.  Same in-memory-sq, The sole booking tool for a tenant with multi_unit and no     multi_professiona, _seed_tenant_and_units() (+7 more)

### Community 595 - "test_cors_origins_parsing.py"
Cohesion: 0.18
Nodes (16): _origins(), CORS_ALLOW_ORIGINS parsing — the format the mesh actually ships.  The failure, The exact string that was live in EasyPanel when the portal broke., The actual regression: an origin carrying `["` or `"]` matches nothing., The old format stays valid — this is additive, nothing to migrate., A typo must reject everyone, never crash the process building Settings., Same origins, either spelling — the property the mesh relies on., test_blank_values_yield_no_origins() (+8 more)

### Community 596 - "test_hub_conversations_my_patients.py"
Cohesion: 0.32
Nodes (18): _appointment(), _conversation(), db(), _ids(), _professional(), AsyncClient, UUID, GET /tenants/me/conversations?mine=true - "Meus pacientes" (TASK-046 R8, spec 20 (+10 more)

### Community 597 - "test_portal_booking_owner_calendar.py"
Cohesion: 0.19
Nodes (19): _book_via_portal_gate(), db(), FakeGoogle, google(), _outbound(), _portal(), A gated Portal booking lives on the OWNING doctor's calendar (live bug, 2026-10-, The steps production ran: hold placed, code typed, hold promoted. (+11 more)

### Community 598 - "test_hub_config_extra_reminder.py"
Cohesion: 0.22
Nodes (21): _custom_due(), _planned_custom_due(), AsyncClient, datetime, MonkeyPatch, The extra reminder on the hub configuration (TASK-048 R9, spec §6.2; R7 §5.B rep, _set(), _stored() (+13 more)

### Community 599 - "Modelos do WhatsApp para os lembretes (TASK-032 R2)"
Cohesion: 0.22
Nodes (8): 1. Modelo novo com 3 botões — `lembrete_consulta_v2`, 2. Modelos que já existem e continuam em uso, 3. Configuração e ativação (quem faz: o dono, no painel; nenhuma automação mexe em variáveis), `devolucao_sinal_conferir_v1`, `liberacao_sinal_pago_v1`, Modelos do WhatsApp para os lembretes (TASK-032 R2), R7 — avisos das ações da clínica (2026-10-09) — PROPOSTA, não submetida, TASK-052 — horário liberado com sinal pago (PENDÊNCIA META, 2026-10-12)

### Community 600 - "_reminders_v2.py"
Cohesion: 0.05
Nodes (38): FakeCalendar, Owner-calendar double: records deletes and creations, every day is open., entitled(), fake_waba_token(), FakeWhatsAppClient, Seeds and fakes shared by the TASK-032 R2 reminder-engine tests.  The in-memor, Records every send; installed in place of `WhatsAppClient`.      `failing_temp, test_failure_of_long_notice_card_is_reported_truthfully() (+30 more)

### Community 601 - "test_payment_attention_hub.py"
Cohesion: 0.13
Nodes (42): Calendar, _clinic_with_mail(), db(), _item(), Mailbox, _payment_hold(), AsyncClient, The clinic's side of attention items and the agenda reads (spec sections 6.5, 10 (+34 more)

### Community 602 - "R9 — PreCheck out of the console + extra reminder "N dias antes, às HH:MM" Implementation Plan"
Cohesion: 0.10
Nodes (19): Decisions taken where the spec is silent, File Structure, Global Constraints, Part A — secretarIA, Part B — Brain-Message-Frontend, Produces for Part B — exact wire shapes, R9 — PreCheck out of the console + extra reminder "N dias antes, às HH:MM" Implementation Plan, Review Focus (+11 more)

### Community 603 - "receive_webhook"
Cohesion: 0.11
Nodes (23): _filter_new_event_ids(), Request, WhatsApp webhook endpoints.  GET  /webhook  - Meta verification handshake (ech, Meta verification handshake.      Meta calls this once when the webhook is con, Return the subset of `event_ids` not yet in `processed_events`.      Fail-open, Receive a webhook event.      GOLDEN RULE: return 200 in well under 5 seconds., receive_webhook(), verify_webhook() (+15 more)

### Community 604 - "test_before_bypass_paths.py"
Cohesion: 0.10
Nodes (36): _a_booked_appointment(), _agent(), _appointments(), _body(), Calendar, db(), Entitlement, _hold_for_another_conversation() (+28 more)

### Community 605 - "test_cancellation_notice.py"
Cohesion: 0.11
Nodes (15): datetime, Doctor-initiated cancellation: the patient is ALWAYS told, and told properly., The safe direction: a free-form send would just be rejected by Meta., Replaced by `justification`, not kept beside it — two fields with     confusabl, A cancellation notice landing hours later reaches somebody who has     already, Even with no clinic email on file, the abandonment carries a stable alarm     f, Cancelling with no reason is a supported path, not a degraded one — it     must, send_buttons truncates past 20 SILENTLY, so an over-long label would     reach (+7 more)

### Community 606 - "booking_draft.py"
Cohesion: 0.08
Nodes (41): The third party's name, or None for "pra mim" / not asked (never "")., real_attendee_name(), _accepted(), _booking_details(), _booking_services(), _carry(), _check(), _Checked (+33 more)

### Community 607 - "TASK-032 R6 — lembrete com Alterar Dados"
Cohesion: 0.18
Nodes (10): Decisões e adaptações, Estado, O que entrou onde, Pendências antes da liberação, progress.md, Registro completo das decisões durante a execução, Regras fixadas e testes, TASK-032 R6 — lembrete com Alterar Dados (+2 more)

### Community 608 - "TASK-037/038 — testes reais após deploy (2026-10-07)"
Cohesion: 0.40
Nodes (4): Pontos a melhorar, Resultados, Reteste dos ajustes do dono (c11f4ad), 12:49–12:54 BRT, TASK-037/038 — testes reais após deploy (2026-10-07)

### Community 609 - "test_deposit_release_preview.py"
Cohesion: 0.31
Nodes (13): _deposit(), _predicted_and_actual(), Preview of what a cancellation does to a paid deposit (TASK-032 R4, spec 4.4)., _setup(), _tenant(), test_a_clinic_cancellation_preview_is_always_refund_due(), test_an_unknown_outcome_still_warns(), test_an_unpaid_charge_is_voided() (+5 more)

### Community 610 - "_connect_calendar"
Cohesion: 0.24
Nodes (16): _connect_calendar(), _events(), AsyncClient, Typed straight into Google Calendar — the UI must keep cancel disabled     rath, Isolation invariant: a shared or mis-configured Google account must not     han, A month of a busy clinic is hundreds of events; the N+1 shape would put     tha, The whole point of the change: a blank box used to mean silence., test_another_tenants_event_id_never_resolves() (+8 more)

### Community 611 - "test_simulation_api.py"
Cohesion: 0.22
Nodes (15): db(), AsyncClient, POST /tenants/me/simulation/turn: faithful, validated and side-effect free (TASK, _slots(), tenant(), test_a_professional_of_another_clinic_is_a_404(), test_a_simulation_writes_and_enqueues_nothing(), test_a_tampered_state_is_refused() (+7 more)

### Community 612 - "test_service_guides_multipro.py"
Cohesion: 0.14
Nodes (36): _clinic(), _direct(), _guide(), maker(), _nothing_about_the_service(), Service orientations in a clinic with 2+ active professionals (TASK-025 producti, The "Tipos de consulta disponíveis" part of the prompt, up to the next blank lin, The prompt the SAME config renders when it carries no `service_guides`. (+28 more)

### Community 613 - "test_arq_log_secret_redaction.py"
Cohesion: 0.15
Nodes (15): _emit_arq_job_start_line(), arq's NATIVE job log must not carry the OTP, the reset link or the e-mail.  `a, arq truncates the args string at 80 chars — a long address pushes the OTP     a, repr(bytes) is `b'…'` — the prefix must not stop the quoted value from matching., Emit EXACTLY the line arq's `Worker.run_job` emits, with arq's own     logger a, Drift guard: if a future arq changes how it logs job args, fail here so     the, Stand-in for the job module's structlog logger. structlog caches loggers     on, _RecordingLogger (+7 more)

### Community 614 - "ManageRequest"
Cohesion: 0.14
Nodes (29): ManageRequest, What the AI says about an existing appointment. Only `action` is required., _Agenda, _appt(), _Gate, _local(), date, datetime (+21 more)

### Community 615 - "FakeCalendar"
Cohesion: 0.14
Nodes (16): FakeCalendar, Google Calendar double for the hub route: one instance per agenda label., _by_id(), _clinic(), _events(), _google(), AsyncClient, GET /events: who sees what, "só os meus" and the Editar pre-fill (TASK-044 R7, § (+8 more)

### Community 616 - "appointment_edit_flow.py"
Cohesion: 0.08
Nodes (68): EditContext, EditDraft, _after_slot_affecting_change(), apply_ai_edit(), _apply_confirm(), _attendee_question_result(), _attendee_step(), _availability() (+60 more)

### Community 617 - "CHECKPOINT — Lembretes R7: ações da clínica na agenda avisam o paciente (TASK-044)"
Cohesion: 0.14
Nodes (13): Adjudicação final do coordenador, CHECKPOINT — Lembretes R7: ações da clínica na agenda avisam o paciente (TASK-044), Contrato para o R5, Decisões onde a spec silenciou, Deploy (quando o dono autorizar), Estado, Modelos da Meta (documentados, NÃO criados), O que entrou e onde (âncoras estáveis) (+5 more)

### Community 618 - "TenantConfigUpdate"
Cohesion: 0.08
Nodes (19): AppointmentType, clean_requirements(), _parse_hhmm(), Any, time, Request/response schemas for the doctor-hub config endpoints.  All domain *sha, Reject unknown weekday keys and overlapping windows within a day.      A weekd, Clinic's physical address (contract v1 §10). Every field optional —     a clini (+11 more)

### Community 619 - "test_workers_ns_patching.py"
Cohesion: 0.29
Nodes (8): MonkeyPatch, `workers_ns` must patch a name in every split module that has it, and undo it (T, Every split module owns a distinct `logger`. Undoing a patch must not collapse, test_nested_patches_of_one_name_unwind_in_order(), test_setattr_is_undone_in_every_module(), test_setattr_reaches_every_module_that_has_the_name(), test_undo_gives_each_module_its_own_original_back(), test_unknown_name_is_refused()

### Community 620 - "Service"
Cohesion: 0.07
Nodes (49): create_service(), _duplicate_error(), list_services(), _offering(), AsyncSession, HTTPException, Doctor hub — the clinic's CANONICAL service catalog.  GET   /tenants/me/servic, The clinic's whole catalog, retired services included.      Retired rows are r (+41 more)

### Community 621 - "test_hub_deps.py"
Cohesion: 0.27
Nodes (10): _FakeSession, _make_tenant(), MonkeyPatch, Tests for api/hub/deps.py:get_current_tenant — the doctor-hub auth gate.  Toke, A minimal session stub: session.get(Tenant, id) -> canned tenant or None., test_active_claim_resolves_the_claimed_tenant(), test_active_claim_with_unknown_tenant_is_404(), test_inactive_claim_is_401() (+2 more)

### Community 622 - ".get"
Cohesion: 0.27
Nodes (8): AsyncClient, `/health` is a frozen contract: a load balancer may match on this body.      B, A silent worker must never read as agreement.      Under ASGITransport the lif, The endpoint that proves the deploy must not become the leak., test_build_endpoint_exposes_no_secret(), test_build_endpoint_proves_what_this_process_runs(), test_build_endpoint_reports_unknown_not_parity_without_a_peer(), test_health_body_is_unchanged()

### Community 623 - "test_service_change_agenda.py"
Cohesion: 0.09
Nodes (34): AgendaCalendar, _applied(), _deliver(), FakeRedis, _items(), _naive(), The agenda side of a service change on a PAID sinal (services/payments/service_c, Records arq enqueues (the A7 "Consulta remarcada" job). (+26 more)

### Community 624 - "_first_decision"
Cohesion: 0.25
Nodes (17): BaseMessage, The visible text of a model message, stripped; thinking and tool blocks never co, reply_text(), The production vendor's chat model (Claude, else OpenAI without a key), producti, real_chat_model(), _config(), _first_decision(), Bateria opt-in: o modelo REAL escolhe a ferramenta certa dado o estado (MVP Port (+9 more)

### Community 625 - "ContextExtraction"
Cohesion: 0.11
Nodes (46): build_draft_messages(), build_messages(), ContextExtraction, extract_context(), neutralise_fences(), What the model returns. Everything optional: 'not in the material' is a valid an, The fence must be impossible to forge from inside the material., Drop every item whose evidence is not literally in the source, or does not carry (+38 more)

### Community 626 - "test_prompts_clinic_facts.py"
Cohesion: 0.13
Nodes (25): _block(), _config(), _FixedDateTime, _frozen_today(), MonkeyPatch, The "SOBRE A CLÍNICA" prompt block and the service-guidance marker (TASK-025)., The TRUE block: from the heading's own prefix ("\\n\\n================ ") to the, Clinic-context isolation, with the approved clinic-local calendar reference. (+17 more)

### Community 627 - "test_appointment_edit_pure.py"
Cohesion: 0.22
Nodes (11): _appt(), The pure pieces of "Alterar Dados" (TASK-032 R6, spec §5.2/§5.5)., test_a_draft_starts_equal_to_the_appointment_in_clinic_time(), test_a_long_recap_drops_orientations_before_it_breaks_the_card_limit(), test_changed_names_each_field_once_in_a_fixed_order(), test_json_round_trip_and_garbage_is_none(), test_the_final_recap_can_report_the_applied_update(), test_the_original_never_moves_and_the_draft_is_immutable() (+3 more)

### Community 628 - "test_asaas_connection.py"
Cohesion: 0.09
Nodes (58): downgrade(), upgrade(), decrypt(), EncryptionError, _fernet(), Symmetric encryption for tenant secrets at rest (Fernet / AES-128-CBC + HMAC)., Raised when encryption/decryption cannot be performed., Build the process-wide Fernet from settings.ENCRYPTION_KEY.      Cached so we (+50 more)

### Community 629 - "TASK-032 — verificação após deploy, 08/10/2026"
Cohesion: 0.14
Nodes (13): Continuação real necessária, Destinatário de paciente informado pelo dono — 19:33Z, Falha reproduzida, Limpeza comprovada, Prova em produção, Reenvio da simulação solicitado pelo dono — 18:56Z, Resultado e limite, Reteste com o link do dono e consulta temporária (+5 more)

### Community 630 - "test_reminder_opening_decision.py"
Cohesion: 0.20
Nodes (20): _decide(), When the chat opens with the appointment reminder (TASK-032 R3, spec §4.3)., A reminder row of the CURRENT version, with any fields overridden., _row(), test_a_button_less_cron_reminder_does_not_block_the_opening(), test_a_chat_card_already_confirmed_is_shown_again_and_can_be_confirmed_again(), test_a_chat_row_shown_inside_the_gap_is_not_shown_again(), test_a_past_or_cancelled_appointment_never_opens_the_chat() (+12 more)

### Community 631 - "test_hub_edit_appointment.py"
Cohesion: 0.20
Nodes (37): sent(), _adjustments(), _iso(), _make_paid(), _paid_world(), _post(), AsyncClient, POST /appointments/{id}/edit - Editar/Remarcar tells the patient (TASK-032 R7 §1 (+29 more)

### Community 632 - "test_probe_charge.py"
Cohesion: 0.22
Nodes (22): _created(), db(), provider(), The clinic's "Testar cobrança": a real Pix charge that is voided right away (TAS, If Asaas hands back an existing customer (same document), it is not ours to dele, The charge was created but Asaas gave no Pix code (no Pix key): reason + stage r, _row(), _run() (+14 more)

### Community 633 - "test_deposit_failure_notice.py"
Cohesion: 0.16
Nodes (23): SimpleNamespace, _account_row(), Env, _link_settings(), _notify(), E-mail to the clinic (and the doctor) when a deposit could not be charged (TASK-, test_concurrent_failures_never_exceed_cap(), test_counter_key_is_salted_with_the_tenant() (+15 more)

### Community 646 - "Primeira mensagem de quem tem consulta — mesmo lembrete, mesmos fluxos"
Cohesion: 0.22
Nodes (8): Estado, O que o paciente vê, Onde foi unificado, Primeira mensagem de quem tem consulta — mesmo lembrete, mesmos fluxos, Regras mantidas, Revisão e evidências, Seguimento 2026-10-27 A — cartão em toda entrada, até 3 confirmações, Seguimento 2026-10-27 — pedido em texto livre abre "Alterar Dados" (LOCAL, sem commit)

### Community 647 - "make_event"
Cohesion: 0.27
Nodes (42): PaymentWebhookEvent, make_event(), A normalised webhook event with the defaults every payment test shares., db(), _deposit_now(), _items(), _paid_state(), _process() (+34 more)

### Community 648 - "test_hub_payment_accounts.py"
Cohesion: 0.10
Nodes (32): _listed(), _put(), AsyncClient, Hub endpoints of the payment connection (api/hub/payment_accounts.py), spec sect, While ASAAS_REFUSE_EXCESS_PERMISSIONS is off the key is accepted and the screen, Undo the fixture's stub: the REAL probe suite runs against the fake Asaas client, The G1 fix end to end: with BOTH flags at their defaults (off), a key that can d, Connect THIS clinic, optionally record a charge failure / success, return its it (+24 more)

### Community 649 - "CalendarUnavailableError"
Cohesion: 0.09
Nodes (7): CalendarUnavailableError, _is_invalid_grant(), Any, RefreshError, Raised when Google Calendar cannot be reached or refused our credentials., Whether a refresh failure is Google's `invalid_grant`.      google-auth raises, One events.list page (blocking): the timed, busy events and the next page's toke

### Community 676 - "as_utc"
Cohesion: 0.13
Nodes (27): as_utc(), find_post_consult_followup(), has_upcoming_appointment(), _load_recent_past_appointments(), load_upcoming_appointments(), AsyncSession, datetime, timedelta (+19 more)

### Community 677 - "CHECKPOINT — IA v2: prompt e regras (TASK-030 P5)"
Cohesion: 0.20
Nodes (9): 1. O que já existia e foi preservado, 2. A lacuna corrigida, 3. Compatibilidade e orçamento medidos, 4. Validação e revisão, 5. Avaliações reais e limites, 6. Liberação — somente dono/operador, 7. Registro, 8. Integração autorizada — 2026-10-07 (+1 more)

### Community 678 - "E-mail fixo ao médico após confirmar Alterar Dados"
Cohesion: 0.25
Nodes (7): Correções encontradas pela validação ampla, E-mail fixo ao médico após confirmar Alterar Dados, Estado, O médico recebe, Onde e quando, Proteções de envio, Revisão e limites

### Community 679 - "ProviderError"
Cohesion: 0.12
Nodes (30): ProviderError, A provider call failed. `code` is symbolic; `retryable` says a retry may succeed, _collect(), _deposit(), _expire(), _held(), The payment provider down around a service change on a PAID sinal (follow-up B,, Follow-up review I3: the clinic is never told to collect for a cancelled consult (+22 more)

### Community 680 - "Ações da clínica na agenda avisam o paciente — design (2026-10-09)"
Cohesion: 0.12
Nodes (15): 1. Botões da consulta na agenda (R5), 2. Toda ação avisa o paciente, no canal dele, 3. Contrato (backend → front), 4. Limites deliberados, 5. Decisões do dono de 2026-10-09 (segunda rodada): quem vê o quê, lembrete extra, dados do Editar, 6.1 Conversas do PreCheck nunca aparecem no console, 6.2 Lembrete extra: "N dias antes, às HH:MM", no fuso da clínica, 6.3 Confirmações do dono (2026-10-10, depois do plano escrito) (+7 more)

### Community 681 - "ReminderJob"
Cohesion: 0.15
Nodes (23): _buttons(), _deliver_portal(), _deliver_whatsapp(), DeliveryOutcome, _error_code(), _portal_email(), Exception, Deliver ONE appointment reminder on the patient's own channel (TASK-032 R2). (+15 more)

### Community 682 - "test_booking_draft_continuation.py"
Cohesion: 0.17
Nodes (21): _Agenda, _consents(), _conversation(), db(), _Log, _onboard(), _park(), MonkeyPatch (+13 more)

### Community 683 - "test_booking_draft_resume_questions.py"
Cohesion: 0.17
Nodes (26): _off(), An enriched request must survive the flag-OFF worker and subsequent answers., test_ambiguous_doctor_answer_preserves_preferences_until_an_explicit_choice(), test_flag_off_worker_does_not_lose_the_day_while_asking_the_doctor(), test_flag_off_worker_preserves_explicit_day_and_self(), _Agenda, _conversation(), db() (+18 more)

### Community 684 - "Subagendas no convite e em “também atende” — TASK-054"
Cohesion: 0.40
Nodes (4): Comportamento, Estado, Subagendas no convite e em “também atende” — TASK-054, Validação

### Community 685 - "test_payment_failure_reason.py"
Cohesion: 0.11
Nodes (17): _classify_text(), _extract_code_hint(), _mentions_missing_document(), Thin async client for the Asaas v3 API (Pix deposit charges).  There is NO ref, One read-only permission probe (`limit=1`, empty body, 5 s timeout)., Best-effort short error-code hint. Reads ONLY `errors[0].code` (a short     sym, True when an error's free text asks for the customer's CPF/CNPJ. The text is onl, Fixed keyword for what an error's free text complains about, or None. The text i (+9 more)

### Community 686 - "test_appointment_change_notifications.py"
Cohesion: 0.15
Nodes (24): _Mailer, datetime, A7: the PATIENT cancelled or moved - the clinic, the doctor and the Portal patie, The job's collaborators: DB, SMTP, brain-api, entitlements, the agenda URL., The doctor already gets "Consulta alterada" from the edit outbox for this move., A staff cancel/edit already tells the patient (TASK-044); announcing it here too, Records every send; `fail` names templates that fail transiently ONCE., _rows() (+16 more)

### Community 687 - "test_clinic_context_backend.py"
Cohesion: 0.10
Nodes (43): _Spec, _booking_items(), Completeness, CompletenessItem, compute_completeness(), _filled(), _guidance_gap(), _has_address() (+35 more)

### Community 688 - "CHECKPOINT — Lembretes R3: abertura do chat e caminho do Cancelar (TASK-032)"
Cohesion: 0.25
Nodes (7): 1. Estado, 2. O que entrou onde, 3. Rótulos dos botões (pedido do dono, 2026-10-07), 4. Regras fixadas (Review Focus do plano → teste que prende), 5. Decisões, 6. Pendências, CHECKPOINT — Lembretes R3: abertura do chat e caminho do Cancelar (TASK-032)

### Community 689 - "test_booking_draft.py"
Cohesion: 0.24
Nodes (9): test_run_agent_maps_a_v2_booking_draft_to_its_sentinel(), The AI booking draft v2: wire format, v1 compatibility and the parked record (TA, test_a_corrupt_payload_raises_value_error(), test_a_missing_or_corrupt_record_is_none(), test_blank_strings_are_absent_and_long_ones_are_capped(), test_draft_record_refuses_a_naive_timestamp(), test_supplied_fields_names_only_what_is_present_in_order(), test_the_v1_payload_still_parses_with_unknown_for_whom() (+1 more)

### Community 690 - "test_payment_cron_jobs.py"
Cohesion: 0.18
Nodes (34): An `enqueue(name, argument)` callable (what the jobs take) and the list it appen, recording_enqueue(), _appointment(), db(), _deposit(), _events(), _provider(), The recurring payment jobs (services/payments/jobs.py, workers/payments_cron.py) (+26 more)

### Community 691 - "TASK-030 — prova real da captura e das datas após deploy"
Cohesion: 0.33
Nodes (5): Casos, Identidade implantada, Limites e preservação, Resultado, TASK-030 — prova real da captura e das datas após deploy

### Community 692 - "TASK-030 — reteste após deploy do P4, 2026-10-07"
Cohesion: 0.33
Nodes (5): Casos, Identidade implantada, Limites e preservação, Resultado, TASK-030 — reteste após deploy do P4, 2026-10-07

### Community 693 - "datetime"
Cohesion: 0.04
Nodes (29): datetime, AnalyticsEvent model - minimal, non-personal usage events (analytics_bi addon)., One planned (or shown) reminder of one appointment - TASK-032, spec 4.1.  The, BookingHold - the slot a Brain-Message visitor CHOSE while they prove their addr, CalendarEvent - one event on the secretarIA's OWN agenda (no Google account need, ClinicPaymentAccount - one row per (tenant, payment provider): the clinic's own, ConsentEvent model - LGPD consent/legal-basis audit trail (groundwork).  One r, Per-conversation PII token map - the re-identification key for one thread.  Mi (+21 more)

### Community 694 - "test_flow_replaces_column.py"
Cohesion: 0.47
Nodes (4): _migration(), conversations.flow_replaces_appointment_id (TASK-032 R3, spec §4.3 "Marcar outra, test_the_migration_adds_and_drops_a_nullable_column_on_sqlite(), test_the_migration_is_the_single_head_on_top_of_the_previous_one()

### Community 695 - "test_appointment_edit_write.py"
Cohesion: 0.38
Nodes (7): _edit(), The shared row writer of an appointment edit (TASK-032 R6 extraction, used by R7, test_a_new_time_moves_the_row_and_logs_a_reschedule(), test_convenio_and_attendee_are_written_and_reported(), test_the_event_link_changes_only_with_the_calendar(), test_without_a_new_time_the_status_and_start_stay(), _write()

### Community 696 - "test_calendar_unavailable_mapping.py"
Cohesion: 0.21
Nodes (20): _fake_calendars_service(), BaseException, MonkeyPatch, RefreshError, Detection-layer contract: every "Google Calendar is broken" mode funnels into a, A minimal fake of the googleapiclient `.calendars().insert(...).execute()`, Regression guard: a 403 without a scope marker still falls through to     the p, Minimal Settings duck-type: enough for CalendarService.__init__ + _build_service (+12 more)

### Community 697 - "db"
Cohesion: 0.16
Nodes (43): _adjustments(), _answer(), _appointment(), db(), _deposit(), _edit(), _expire(), _items() (+35 more)

### Community 698 - "_FakeWhatsAppClient"
Cohesion: 0.22
Nodes (4): _fake_get_waba_token(), _fakes(), _FakeWhatsAppClient, Records constructed instances + sends; installed in place of the real client.

### Community 699 - "test_availability.py"
Cohesion: 0.12
Nodes (22): _at_day_step(), _Calendar, _Gate, _held(), _Log, date, datetime, Free time on one agenda minus held slots: one definition for every reader (TASK- (+14 more)

### Community 701 - "test_handback_log.py"
Cohesion: 0.20
Nodes (12): log(), _LogRecorder, _only_event(), MonkeyPatch, The hand-back event: one shape, a closed vocabulary, no values (TASK-030 P1)., Records every structlog call (`caplog` is empty for structlog in this suite)., test_a_value_outside_the_vocabulary_is_logged_as_other_never_as_itself(), test_landing_of_names_the_step_or_the_pseudo_step() (+4 more)

### Community 702 - "resolve_opening_message"
Cohesion: 0.05
Nodes (60): Hard cut with no mark, for fields nothing ever matches against.      Row descr, truncate_plain(), The city out of `Tenant.address` (a JSON dict), or "" when not collected., tenant_city(), PatientOpeningState, The five opening states, in first-match order (see the resolver)., ChannelPolicy, policy_for() (+52 more)

### Community 703 - "test_service_change_cancel.py"
Cohesion: 0.14
Nodes (38): _answer(), _awaiting(), _by_reason(), _cancel(), _choose_cancel(), _decrease_asked(), _no_money_out(), _paid_difference() (+30 more)

### Community 704 - "test_service_change_webhook.py"
Cohesion: 0.22
Nodes (37): _adjustment(), _appointment(), _awaiting(), _items(), _move_row(), _notices(), _paid(), _post_commit() (+29 more)

### Community 706 - "_seed_tenant"
Cohesion: 0.18
Nodes (11): The Portal's `patient_ref` is an id, not a phone, and is not stored as one., CHECKLIST (WhatsApp non-regression): the whole card leg is Portal-only.      A, CHECKLIST: the WhatsApp channel never gains an e-mail step.      Greeting then, A WhatsApp conversation carrying one of the new states is not intercepted., The clinic's patient context shows the address as soon as it is saved (owner, 20, _seed_tenant(), test_a_portal_booking_records_no_phone_number(), test_the_claimed_email_is_saved_on_the_patient_for_the_console() (+3 more)

### Community 707 - "_WhatsApp"
Cohesion: 0.11
Nodes (10): log(), _LogRecorder, _patch_session_factory(), precheck_number(), MonkeyPatch, Configure the platform PreCheck number for every test in this file.      `get_, Stands in for `WhatsAppClient`, as both the class and the client.      `for_te, Autouse so no test can reach the real Cloud API by forgetting to ask. (+2 more)

### Community 708 - "c9f4e2a7b815_insurance_catalog.py"
Cohesion: 0.43
Nodes (6): _backfill_legacy_insurances(), _created_at(), _normalize(), Column, insurance_catalog: global convênio catalog + per-clinic and per-doctor plans, upgrade()

### Community 710 - "verify_subscription_token"
Cohesion: 0.27
Nodes (11): _cache_get(), _cache_key(), _cache_put(), _optional_uuid(), _parse_role_claims(), UUID, Subscription-token verification — the doctor-hub auth seam.  =================, A UUID from the answer, or None for anything absent or malformed (never raises). (+3 more)

### Community 711 - "test_confirmation_warning_email.py"
Cohesion: 0.20
Nodes (3): MonkeyPatch, The clinic warning e-mail and the Portal nudge template (TASK-032 R4, spec 4.4)., smtp()

### Community 712 - "test_manage_v2_handback.py"
Cohesion: 0.30
Nodes (23): db(), _events(), _local(), _manage(), date, datetime, manage_existing_appointment v2 through the worker (TASK-030 P3, spec §4.5).  T, _ref() (+15 more)

### Community 713 - "TenantRuntimeConfig"
Cohesion: 0.05
Nodes (51): CalendarCredentialStatus, _address_line(), _clinic_fact_lines(), _format_answering_rules(), _format_appointment_types(), _format_business_hours(), _format_clinic_facts(), _format_clinic_features() (+43 more)

### Community 714 - "_Pool"
Cohesion: 0.13
Nodes (20): cancel_replaced_appointment(), AsyncSession, datetime, UUID, Cancel the appointment a "Marcar outra consulta" booking replaces (TASK-032 R3)., What the after-commit step and the patient's confirmation need to know., Cancel the replaced appointment inside the caller's transaction.      Returns, ReplacedAppointment (+12 more)

### Community 715 - "decide_reminder_opening"
Cohesion: 0.21
Nodes (16): _current_version_rows(), decide_reminder_opening(), ensure_chat_reminder(), entry_card_retired(), nearest_live_appointment(), OpeningDecision, AsyncSession, datetime (+8 more)

### Community 716 - "_run_hand_back_with_intro"
Cohesion: 0.13
Nodes (11): _fallback(), Owner, 2026-10-07: the patient who asked something reads the answer first,, Review of TASK-038: a hand-back that ends up sending no card must still get the, _run_hand_back_with_intro(), test_hand_back_words_are_sent_before_the_card(), test_hand_back_words_claiming_an_unproven_action_are_dropped(), test_hand_back_words_never_go_out_alone(), _FakeSession (+3 more)

### Community 717 - "_FakeWhatsApp"
Cohesion: 0.22
Nodes (5): _FakeWhatsApp, metered(), MonkeyPatch, Records sends. `for_tenant` hands back the instance the test holds., wa()

### Community 718 - "TASK-041 — edição pela IA sem perder a consulta"
Cohesion: 0.25
Nodes (7): Causa confirmada e limite da investigação, Comandos e baseline, Desenho implementado (âncoras estáveis), Efeito para o paciente e para a clínica, Evidência RED → GREEN e revisão, TASK-041 — edição pela IA sem perder a consulta, Transcript e checklist da prova

### Community 719 - "test_deposit_wait_turns.py"
Cohesion: 0.12
Nodes (44): _assert_back_on_the_day_picker(), _code_held_portal(), _conversation(), _DayCalendar, db(), _deposit(), _handle(), _inbound() (+36 more)

### Community 720 - "test_flow_day_calendar.py"
Cohesion: 0.15
Nodes (27): calendar_day_row_id(), The row id a day tapped on a card's calendar comes back as (page 0: the short li, offered_reply_ids(), Every option id the given `Message.interactive` blobs put on screen.      Pure, _Calendar, _calendar_screen(), _conversation(), The "Outra data" screen (owner, 2026-11-04): a date far ahead, picked on a calen (+19 more)

### Community 721 - "test_calendar_event_body.py"
Cohesion: 0.43
Nodes (6): _fake_service(), Corpo do evento: descrição com convênio e lembretes explícitos (MVP Portal, Task, test_create_event_sends_explicit_reminders(), test_create_event_success_log_omits_patient_title(), test_custom_reminders_override_defaults(), test_flow_confirmation_includes_insurance_in_event()

### Community 722 - "get_settings"
Cohesion: 0.05
Nodes (65): _admin_token_scheme, _internal_key_scheme, main(), Generate a Google Calendar refresh token for Fase A (single tenant).  One-shot, Security, FastAPI dependency: 403 unless `X-Admin-Token` matches the env token.      Use, require_admin(), Security (+57 more)

### Community 723 - "build_conversation_state"
Cohesion: 0.12
Nodes (27): build_conversation_state(), _doctor_label(), _is_internal_marker(), _pra_quem_line(), Turn-scoped "ESTADO DA CONVERSA" block for the agent's system prompt.  The LLM, `flow_selected_type` holds `__attendee_next_*__` during the pra-quem steps: an e, "Dr. Fulano (Cardiologia)" - the specialty lets the model tell, and say, when no, The state block, or None when there is no conversation snapshot. (+19 more)

### Community 724 - "CHECKPOINT — Lembretes R4: avisos, liberar horário e mensagem (TASK-032)"
Cohesion: 0.29
Nodes (6): CHECKPOINT — Lembretes R4: avisos, liberar horário e mensagem (TASK-032), Decisões tomadas sem consulta, Desvios na implementação (registro), O que entrou onde, Pendências, Regras que não são óbvias

### Community 725 - "clinic_facts.py"
Cohesion: 0.29
Nodes (4): _blank_to_none(), _drop_blank_items(), FaqItem, Bounded, optional facts about a clinic that the LLM may quote (TASK-025).  Liv

### Community 726 - "FastAPI"
Cohesion: 0.09
Nodes (42): FastAPI, _FakeBrainApi, install_fake_brain_api(), make_hub_token(), MonkeyPatch, UUID, Test helpers that stand in for brain-api's hub-token introspection (TASK-020)., An opaque hub token whose introspection body carries the given role claims. (+34 more)

### Community 727 - "test_safe_fetch.py"
Cohesion: 0.14
Nodes (42): MockTransport, Resolver, fetch_public_page(), FetchError, AsyncBaseTransport, The URL (or a redirect target) must not be fetched. `code` is stable for the UI/, The page could not be fetched (network, status, size, type). `code` is stable., UnsafeUrlError (+34 more)

### Community 728 - "Lembretes R2 — Motor de lembretes Implementation Plan"
Cohesion: 0.09
Nodes (22): Consumes from R1 (verified against `docs/superpowers/plans/2026-10-03-lembretes-r1-fundacao.md`), Deploy e liberação (only on the owner's explicit request, each step separately), File Structure, Global Constraints, Lembretes R2 — Motor de lembretes Implementation Plan, Produces for R3 / R4 / R5, Review Focus, Self-review (done while writing; kept for the executor) (+14 more)

### Community 729 - "4. Desenho"
Cohesion: 0.09
Nodes (22): 10. Referências, 1. Objetivo e intenção, 2. Decisões do dono (2026-10-02), 3. Estado atual (fatos do mapa que sustentam o design), 4.10 Prompt e regras, 4.11 Liberação, 4.1 Rascunho v2 (o que a IA pode preencher), 4.2 O resolvedor (um só lugar que decide onde o paciente cai) (+14 more)

### Community 730 - "_FakeAsyncClient"
Cohesion: 0.20
Nodes (4): _FakeAsyncClient, _FakeResponse, Exception, Records init kwargs + posted requests; returns a canned response or raises.

### Community 731 - "test_hub_config_paid_notices.py"
Cohesion: 0.38
Nodes (9): clinic(), AsyncClient, The clinic's standing authorisation for paid notices on the hub config (TASK-032, _stored(), test_a_save_that_omits_it_leaves_it_untouched(), test_get_config_exposes_the_flag_off_by_default(), test_null_is_refused_and_nothing_changes(), test_the_configuration_save_r5_uses_turns_it_on_and_echoes_it() (+1 more)

### Community 732 - "test_release_offer_buttons.py"
Cohesion: 0.16
Nodes (40): offer_card_after_start(), datetime, UUID, The Portal's one-time card after a released PAID slot's start (TASK-052).  Por, Portal entry after S with the offer still unanswered: ONE card with Remarcar / C, _ago(), _items(), _last_buttons() (+32 more)

### Community 733 - "test_hub_staff_message.py"
Cohesion: 0.29
Nodes (20): mail(), _message(), AsyncClient, MonkeyPatch, POST /appointments/{id}/message (TASK-032 R4, spec 4.4)., _recent(), test_a_failing_portal_write_is_a_502_and_records_nothing(), test_a_portal_patient_with_email_is_nudged() (+12 more)

### Community 734 - "CHECKPOINT — "Meus pacientes" no console (TASK-046 R8, parte A)"
Cohesion: 0.33
Nodes (5): CHECKPOINT — "Meus pacientes" no console (TASK-046 R8, parte A), Deploy (não executado), Estado, Fora do escopo, O que mudou

### Community 735 - "Retenção de visitas vazias + espera de código de conta conhecida — TASK-042 (lado secretarIA)"
Cohesion: 0.33
Nodes (5): 1. Descarte de visita vazia (API), 2. Espera de código de conta conhecida, antes do consentimento (worker), Estado, Retenção de visitas vazias + espera de código de conta conhecida — TASK-042 (lado secretarIA), Validação

### Community 736 - "TASK-041 — prova real da edição no Portal, 09/10/2026"
Cohesion: 0.33
Nodes (5): Ambiente e identidade, Evidências sanitizadas, Preservação e limites, Roteiro observado — 03:46–03:52 UTC (00:46–00:52 local), TASK-041 — prova real da edição no Portal, 09/10/2026

### Community 737 - "Exception"
Cohesion: 0.07
Nodes (49): Exception, BookingSnapshot, calendar_for_booking(), confirmation_text(), event_id_for(), promote(), Promotion, AsyncSession (+41 more)

### Community 738 - "CHECKPOINT — remediação lembretes R6 (e-mail ao médico + entrada pelo link)"
Cohesion: 0.40
Nodes (4): Causas tratadas, CHECKPOINT — remediação lembretes R6 (e-mail ao médico + entrada pelo link), Ordem de publicação (obrigatória), Pendências da revisão (results/remediation-review.md)

### Community 739 - "test_deposit_after_mode.py"
Cohesion: 0.32
Nodes (15): _create(), db(), _provider(), `maybe_create_deposit` behind the provider abstraction (spec sections 5.1, 6.1,, _seed(), test_a_charge_without_a_copy_paste_is_kept_because_it_is_real(), test_a_clinic_without_an_account_is_a_silent_skip(), test_a_payer_document_requirement_is_a_clean_skip_until_f2_collects_it() (+7 more)

### Community 740 - "make_sessionmaker"
Cohesion: 0.04
Nodes (71): PatientPaymentCustomer, FakeArqPool, make_sessionmaker(), A fresh in-memory SQLite database with every table; returns (engine, sessionmake, An arq pool: Redis calls plus a recorded `enqueue_job`., Calendar, _config(), db() (+63 more)

### Community 741 - "Lembretes R4 — Avisos por e-mail, estado vermelho, liberar horário e mensagem ao paciente (API) Implementation Plan"
Cohesion: 0.10
Nodes (20): Consumes (verified against the already-written plans), Decisions taken where the spec is silent (flag to the owner/Orchestrator), Deploy e liberação, File Structure, Global Constraints, Lembretes R4 — Avisos por e-mail, estado vermelho, liberar horário e mensagem ao paciente (API) Implementation Plan, Produces for R5 (front) — exact wire shapes, Review Focus (+12 more)

### Community 742 - "test_release_carry_check.py"
Cohesion: 0.19
Nodes (30): _book(), db(), _kinds(), _provider(), The provider check at the "Remarcar" confirmation (TASK-052, owner 2026-10-12)., _rescheduling(), _row(), test_a_deposit_of_another_clinic_is_never_read_with_this_clinics_key() (+22 more)

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

### Community 750 - "test_a_returning_portal_patient_gets_the_resume_offer_on_the_portal"
Cohesion: 0.33
Nodes (6): _age_conversation(), Sim" re-sends the code card - without "Sua consulta já está confirmada!"., Push every message `hours` into the past: `last_activity_at` is     max(Message, The generic "quer continuar?" leaves by Brain-Message, not by WhatsApp.      L, test_a_returning_portal_patient_gets_the_resume_offer_on_the_portal(), test_resuming_a_code_wait_without_a_booking_does_not_confirm_one()

### Community 751 - "PaymentStatus"
Cohesion: 0.12
Nodes (37): DailyStep, ExpiryHook, _charge_status(), daily_maintenance(), DailyReport, expire_due_adjustments(), expire_due_charges(), _expire_one() (+29 more)

### Community 752 - "_apply_deposit_awareness"
Cohesion: 0.16
Nodes (18): _apply_deposit_awareness(), _at_reschedule_limit(), _cancel_warning_line(), _hours_until_start(), _pix_retention_warning_line(), datetime, deposit - split out of workers/tasks.py (TASK-023)., The money sentence a patient must read BEFORE cancelling a PAID deposit, or None (+10 more)

### Community 753 - "test_draft_wait_questions.py"
Cohesion: 0.29
Nodes (12): _at(), _collecting(), _doctor_snapshot(), The AI draft waits on the convênio, doctor and service questions too (TASK-030 P, test_answer_preserves_terminal_or_failed_booking_continuation(), test_free_text_on_a_question_keeps_the_draft_for_the_llm(), test_outro_convenio_keeps_waiting_for_the_typed_name(), test_the_convenio_answer_asks_the_worker_to_resume() (+4 more)

### Community 754 - "_audit"
Cohesion: 0.33
Nodes (8): _audit(), _classify(), Counts, main(), READ-ONLY audit: NULL versus EMPTY on every professional's own config.  `Profe, Pure tallies. Nothing here can hold an id, a name or a config value., Per-tenant counts, numbered positionally so no tenant id is ever printed., TenantBucket

### Community 755 - "WhatsAppClient"
Cohesion: 0.05
Nodes (54): EmailOutcome, WHY a transactional send did or did not happen.      The bool `send_transactio, True iff trying the exact same send again could plausibly succeed., _clinic_notice_portal(), _clinic_notice_whatsapp(), conversation_id_for(), DeliveryFailedError, NoChannelError (+46 more)

### Community 756 - "ContextSource"
Cohesion: 0.19
Nodes (25): _arq_pool_or_503(), _create(), create_source(), create_source_from_file(), delete_source(), _detail(), _enforce_quota(), _file_error() (+17 more)

### Community 757 - "_log"
Cohesion: 0.12
Nodes (26): DepositReleaseEmail, A slot the clinic released with a Pix deposit, the patient's choice, and what it, dispatch_due(), _dispatch_one(), DispatchReport, _finish(), _give_back(), _held_text() (+18 more)

### Community 758 - "test_payment_settings.py"
Cohesion: 0.20
Nodes (33): entitlement_summary(), _code(), db(), Entitlement, _put(), AsyncClient, The deposit settings a clinic edits in `/contexto` (spec sections 6.4, 12.1, 12., The deposit is a core SecretarIA capability since 2026-10-28: switched on + a co (+25 more)

### Community 760 - "chat_model.py"
Cohesion: 0.13
Nodes (21): BaseChatModel, build_chat_model(), build_structured_model(), model_name(), provider(), Any, Which vendor's chat model answers: Anthropic's Claude Haiku, or OpenAI only with, A LangChain chat model for the configured vendor.      `openai_max_tokens` is (+13 more)

### Community 761 - "TextBubble"
Cohesion: 0.03
Nodes (144): _print_bubbles(), ButtonBubble, _clean(), _finalise(), parse(), _parse_slot_rows(), _pop_preceding_text_for(), Bubble (+136 more)

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

### Community 766 - "build"
Cohesion: 0.22
Nodes (8): build(), health(), Any, Request, Liveness probe and build identity.  `/health` is the LIVENESS probe and its re, Liveness probe. Intentionally does not touch Postgres or Redis., Which code is running here, and does the worker agree? (FIX_01 §5.1/§5.2), test_thinking_stays_the_models_default_unless_switched_off()

### Community 767 - "_Response"
Cohesion: 0.16
Nodes (15): base_url_for(), _code(), emit(), _json_field(), keyword_flags(), main(), onboarding_states(), Measure what the REAL Asaas answers, with the clinic owner's own account (TASK-0 (+7 more)

### Community 768 - "EntitlementSummary"
Cohesion: 0.17
Nodes (20): EntitlementSummary, _fetch_entitlements(), _fresh_key(), get_entitlements(), UUID, Entitlement summary reader — the plugin/tier gating seam.  ===================, Direct (uncached) fetch from brain-api. None on any ambiguity (fail closed)., Redis-cached entitlement read. See module docstring for the cache contract. (+12 more)

### Community 769 - "Settings"
Cohesion: 0.04
Nodes (48): BaseSettings, UUID, Strongly-typed settings. Values come from the environment or `.env`.      Real, Parse BOT_ALLOWLIST_WA_IDS into a set of digits-only wa_ids.          Meta alw, Parse DEBUG_RESET_TENANT_IDS into a set of tenant UUIDs.          Quotes and b, Parse CORS_ALLOW_ORIGINS into a clean list of origins.          Accepts a JSON, Settings, CalendarHealthStatus (+40 more)

### Community 770 - "receive_asaas_webhook"
Cohesion: 0.29
Nodes (7): _extract_event_fields(), JSONResponse, Request, Asaas payment webhook endpoint.  POST /webhooks/asaas - receives Pix payment l, Tolerant extraction of (event_id, event_type, payment_id) from an Asaas     web, Fast-ACK Asaas webhook receiver. Auth + processing happen in the worker., receive_asaas_webhook()

### Community 771 - "test_appointment_edit_paid_service.py"
Cohesion: 0.20
Nodes (32): ProfessionalEditNotice, _adjustments(), _asked(), _card_for(), _confirm(), _count(), _edit(), The patient changes the SERVICE of a consultation whose sinal is PAID (plan 2026 (+24 more)

### Community 772 - "test_payment_secret_hygiene.py"
Cohesion: 0.13
Nodes (11): EventDict, Blank secret-bearing keys AND personal-data/content keys before rendering., redact_secrets(), No secret and no payer data ever leaves through a log, a job argument, a respons, test_a_payment_account_never_renders_its_secrets_when_a_caller_logs_it(), test_an_asaas_key_inside_any_string_value_is_redacted_even_under_an_innocent_field(), test_payment_personal_and_secret_keys_are_blanked(), test_redactor_blanks_secret_bearing_keys() (+3 more)

### Community 773 - "AppointmentDepositView"
Cohesion: 0.19
Nodes (9): _deposit_read(), The state of the money for the agenda (status VALUE + amount), or None., CalendarDepositRead, The Pix deposit (sinal) of an appointment, as the agenda shows it.      Delibe, AppointmentDepositView, What the agenda may know about an appointment's deposit: the state of the     m, _FakeCalendarService, No Google call is ever attempted; `next_events` is what /events sees. (+1 more)

### Community 774 - "Lembretes R5 — Front da agenda (cores, ✓/✓✓, gaveta com ações por horário, avisos ao paciente, Editar/Remarcar, legenda) e configuração "Lembretes e avisos" Implementation Plan"
Cohesion: 0.09
Nodes (22): Change log 2026-10-09 (owner decisions — spec `2026-10-09-acoes-clinica-avisos-paciente-design.md`), Change log 2026-10-09, second round (owner decisions — spec §5 and the "create on his own agenda" decision; R7 "Produces for R5" as updated the same day), Consumes — backend contract (exact shapes, and what is assumed), Decisions taken where the spec is silent, Deploy and live proof (not part of the tasks), File Structure, Global Constraints, Lembretes R5 — Front da agenda (cores, ✓/✓✓, gaveta com ações por horário, avisos ao paciente, Editar/Remarcar, legenda) e configuração "Lembretes e avisos" Implementation Plan (+14 more)

### Community 775 - "Lembretes, primeira mensagem e estado de confirmação da consulta — Design"
Cohesion: 0.12
Nodes (16): 10. Referências, 1. Objetivo e intenção, 2. Decisões do dono (2026-10-03), 3. Estado atual (fatos do mapa que sustentam o design), 4.1 Cronograma de lembretes (dados), 4.2 O motor de lembretes, 4.3 A mensagem como primeira mensagem do chat, 4.4 Avisos à clínica, estado vermelho e ações na agenda (+8 more)

### Community 776 - "test_payment_settings_roles.py"
Cohesion: 0.41
Nodes (14): _bearer(), db(), _put(), AsyncClient, Only a clinic MANAGER may write the payment configuration (owner answer 12, TASK, _stored(), test_a_doctor_who_is_not_a_manager_cannot_either(), test_a_manager_saves_the_payment_fields() (+6 more)

### Community 777 - "test_context_sources_api.py"
Cohesion: 0.11
Nodes (17): db(), _docx_bytes(), _limits(), Hub routes for context sources (TASK-026): create (url/text/file), list, detail,, tenant(), test_a_docx_upload_is_read_in_the_request_and_queued(), test_a_row_stuck_in_flight_for_long_stops_counting(), test_an_over_quota_upload_is_refused_before_the_file_is_parsed() (+9 more)

### Community 778 - "payment_accounts.py"
Cohesion: 0.09
Nodes (46): ChecklistOut, _checklist_out(), _detail(), disconnect_asaas(), get_asaas_checklist(), _guard(), _labels(), list_payment_accounts() (+38 more)

### Community 779 - "_ExplodingWhatsAppClient"
Cohesion: 0.50
Nodes (3): _ExplodingWhatsAppClient, Any use at all fails the test. Nothing on this path may reach Meta., _wire()

### Community 780 - "FakeAsaasClient"
Cohesion: 0.17
Nodes (3): FakeAsaasClient, Exception, Records every call; every failure is injectable per method name via `errors`.

### Community 781 - "delete_tenant"
Cohesion: 0.14
Nodes (17): main(), _parse_args(), Namespace, Wipe dynamic data from the database for clean testing.  By default truncates t, _count(), delete_tenant(), DeleteTenantOutcome, DeleteTenantResult (+9 more)

### Community 782 - "test_context_catalog_proposal.py"
Cohesion: 0.20
Nodes (28): _by_name(), _good(), _proposal(), test_a_day_the_quote_does_not_name_is_dropped_from_the_week(), test_a_doctor_the_clinic_already_has_is_matched_not_duplicated(), test_a_doctor_whose_quote_is_not_in_the_source_is_dropped(), test_a_duration_the_text_does_not_say_falls_back_and_says_so(), test_a_made_up_service_is_dropped() (+20 more)

### Community 783 - "IA entra em qualquer etapa — P2a: disponibilidade, rascunho v2, `flow_draft` e o resolvedor — Implementation Plan"
Cohesion: 0.12
Nodes (15): Cobertura da spec (P2a), Decisões deste plano (mudar só se o dono discordar), Deploy e liberação, Estado de execução — 2026-10-05, File Structure, Global Constraints, IA entra em qualquer etapa — P2a: disponibilidade, rascunho v2, `flow_draft` e o resolvedor — Implementation Plan, Interfaces (o que P2b, P3, P4 e P5 consomem — nomes exatos) (+7 more)

### Community 784 - "test_release_offer_dispatch.py"
Cohesion: 0.28
Nodes (27): emails(), _call(), _clinic(), db(), _dispatch(), _inbound(), _mark(), _opened() (+19 more)

### Community 785 - "test_deposit_gate.py"
Cohesion: 0.22
Nodes (24): db(), entitlement(), _now(), _quote(), `quote_deposit` - the decision the `before` flow makes before it reserves a slot, Legitimate `no_deposit` is decided before the operational checks: a clinic whose, Measured on the real Asaas (2026-10-30): a Pix charge under R$ 5,00 is refused., The gate reads no entitlement any more; kept as a named fixture so every case be (+16 more)

### Community 786 - "PaymentAttentionItem"
Cohesion: 0.08
Nodes (47): PaymentAttentionItem, attention_dedupe_key(), AttentionOutcome, has_open_item(), mark_fulfillment(), mark_refund_seen(), open_attention_item(), AsyncSession (+39 more)

### Community 787 - "test_deposit_cancel_no_refund.py"
Cohesion: 0.27
Nodes (27): _adjustment(), _cancel(), db(), _install(), _items(), FakeAsaasClient, Cancelling a consultation with a deposit NEVER returns money (decision D15, spec, _seed() (+19 more)

### Community 788 - "_report"
Cohesion: 0.43
Nodes (6): main(), UUID, READ-ONLY report: future appointments saved without a professional owner.  Boo, _report(), MonkeyPatch, _wire()

### Community 790 - "make_released"
Cohesion: 0.14
Nodes (33): make_released(), UUID, Shared helpers for the TASK-052 tests (a released appointment with a deposit). N, A clinic whose appointment was just RELEASED (status CANCELLED) with a deposit., Released, maker(), _offer(), Races SQLite cannot prove for the released-slot offer (TASK-052), on a REAL Post (+25 more)

### Community 791 - "test_payment_account_charge_health.py"
Cohesion: 0.18
Nodes (14): _account(), _config(), db(), _offline(), Charge health of a clinic payment account (TASK-071, Plan 3, task 1).  The acc, The daily recheck (and a reconnection) refreshes `last_verified_at`; it used to, test_a_connection_check_after_the_failure_does_not_clear_failing(), test_failure_after_success_is_failing() (+6 more)

### Community 792 - "5. Desenho"
Cohesion: 0.07
Nodes (26): 10. Decisões tomadas (padrão), 11. Perguntas em aberto, 12. Não provado / bloqueios, 1. Intenção, 2. Escopo, 3. Fora de escopo, 4. O que o código mostra hoje (base `3650e10`), 5.10 Contrato para a aba (TASK-054) (+18 more)

### Community 793 - "safe_fetch.py"
Cohesion: 0.14
Nodes (17): _content_codings(), _decode(), FetchedPage, _host_header(), _ip_literal(), is_public_ip(), _netloc(), Fetch a public web page for context ingestion without being turned against our n (+9 more)

### Community 794 - "test_migration_native_agenda.py"
Cohesion: 0.60
Nodes (4): _migration(), calendar_events - the native agenda's table (2026-10-20): additive, one new head, test_the_migration_is_in_the_single_heads_history_on_top_of_r9(), test_upgrade_creates_the_table_the_model_maps_and_downgrade_drops_it()

### Community 797 - "fetch_has_secretary"
Cohesion: 0.15
Nodes (25): fetch_has_secretary(), UUID, secretarIA -> brain-api: does this clinic have a WORKING secretary? (TASK-052 Ta, True/False from brain-api's live answer; None when it cannot be known (see modul, _unknown(), _configured(), MonkeyPatch, Request (+17 more)

### Community 798 - "test_payment_migration.py"
Cohesion: 0.14
Nodes (23): Session, _config(), _offline(), Migration e2b7c4a9d1f5 - the `before` timing columns (TASK-017, spec sections 6., test_downgrade_sql_drops_only_what_this_revision_added(), test_postgres_round_trip_leaves_no_model_drift(), test_the_revision_chains_after_the_provider_core_and_is_the_single_head(), test_upgrade_sql_is_purely_additive() (+15 more)

### Community 799 - "db"
Cohesion: 0.22
Nodes (9): db(), It is read before the doctor has decided anything — it must not cancel,     and, Past the budget the patient still has not been told, and nobody is     reading, Isolation guard on the job itself, not just on the endpoint., tenant(), test_an_appointment_from_another_tenant_is_never_notified(), test_cancelled_appointment_stays_cancelled(), test_preview_never_mutates_anything() (+1 more)

### Community 800 - "test_payment_webhook_handler.py"
Cohesion: 0.23
Nodes (19): _headers(), AsyncClient, POST /webhooks/{provider}/{webhook_slug}: the persist-first per-clinic payment w, _rows(), test_a_database_failure_is_503_so_the_provider_retries(), test_a_forged_token_is_401_with_no_body_no_row_and_no_job(), test_a_queue_outage_still_answers_200_because_the_event_is_already_durable(), test_a_slug_never_authenticates_for_another_provider() (+11 more)

### Community 802 - "IA entra em qualquer etapa — P2b: hand-backs pelo resolvedor, "pra quem" sempre perguntado, ferramenta v2, interruptor e estado da conversa — Implementation Plan"
Cohesion: 0.15
Nodes (12): Cobertura da spec (P2b), Decisões deste plano (mudar só se o dono discordar), Deploy e liberação, File Structure, Global Constraints, IA entra em qualquer etapa — P2b: hand-backs pelo resolvedor, "pra quem" sempre perguntado, ferramenta v2, interruptor e estado da conversa — Implementation Plan, Review Focus, Task B1: `set_booking_draft` pelo resolvedor — "pra quem" sempre perguntado, o interruptor e as correções do `selection_only` (+4 more)

### Community 803 - "SubscriptionClaim"
Cohesion: 0.07
Nodes (46): get_config(), AsyncSession, Doctor hub — tenant configuration endpoints (authenticated).  GET  /tenants/me, LEGACY single-scope save. Superseded by PUT /tenants/me/configuration.      Be, Save tenant config and one professional's config in ONE transaction.      Orde, 403 `forbidden_role` / `role_claim_missing` when `patch` touches a payment field, _require_manager_for_payment_fields(), update_config() (+38 more)

### Community 804 - "booking_details_text"
Cohesion: 0.14
Nodes (24): _assemble(), booking_details_text(), _clean(), clinic_address_line(), _identity_lines(), price_text(), Any, The booking details a patient reads before confirming an AI-landed booking (TASK (+16 more)

### Community 805 - "test_pix_single_message.py"
Cohesion: 0.13
Nodes (12): before_request_text(), The Pix request (ONE message): what it is for, how much, until when and, when gi, pix_code_block(), pix_code_tail(), The Pix "copia e cola" as part of ONE patient message (2026-10-27).  A Pix req, The code as the fenced block both channels understand., The closing lines of a Pix message (label + fenced code); empty when there is no, difference_pix_text() (+4 more)

### Community 806 - "handback_log.py"
Cohesion: 0.23
Nodes (12): _dropped(), _id(), landing_of(), log_handback_entered(), _names(), UUID, One structured event per AI hand-back: where did the patient land?  The agent, `value` when it is in the closed vocabulary, "other" when it is not, None for No (+4 more)

### Community 807 - "test_payment_notify.py"
Cohesion: 0.17
Nodes (19): _copy_lines(), db(), FakeWhatsApp, Channel-neutral delivery of the money messages (services/payments/notify.py, spe, Before this task `_send_deposit_request` built a WhatsApp client for `wa_id=None, Review fix I6: a blocked deposit hands the chat to a human (no clinic e-mail, ow, Mirrors `WhatsAppClient`: the CALLER records the outbound row (persists_outbound, _rows() (+11 more)

### Community 808 - "test_release_keep.py"
Cohesion: 0.19
Nodes (21): _accept(), _Calendar, db(), services/release_keep.py - the CLINIC accepts "Manter horário" (TASK-052, owner, Session 07: `accept_keep` read the offer, the released row and the holds, then c, The clinic re-occupied the freed slot with another patient., _requested(), _state() (+13 more)

### Community 809 - "release_offers.py"
Cohesion: 0.29
Nodes (20): accept_offer_keep(), _answer(), _body(), _detail(), _dispatch_now(), _held(), list_release_offers(), mark_offer_refunded() (+12 more)

### Community 810 - "deposit_wait.py"
Cohesion: 0.13
Nodes (24): Tap, _aware(), check_paid(), classify_tap(), _clear_selection(), _conversation_of(), current_deposit(), _key() (+16 more)

### Community 811 - "_card_state"
Cohesion: 0.14
Nodes (18): _bm_tap(), _card_state(), _last_card(), One TAP on the Portal, through the real validation the channel applies.      G, The `Message.interactive` of the newest outbound row, as stored., A Portal conversation parked on a REAL code-notice card.      Built through th, CHECKLIST (§3 + §4): the body names the masked inbox, the card has 3 buttons., CHECKLIST (§4): `identity_resend` -> request_code again, same card. (+10 more)

### Community 812 - "DepositQuote"
Cohesion: 0.10
Nodes (31): LiveChargeHoldError, This conversation's own hold is the reservation behind a Pix that can still be p, abandon_charge(), begin_payment(), ChargeStartError, _create_and_record(), _create_with_one_retry(), _discard_customer_created_here() (+23 more)

### Community 813 - "File Structure"
Cohesion: 0.08
Nodes (23): Contratos tocados, File Structure, Global Constraints, Liberar horário com sinal pago: manter (por opção da clínica), remarcar ou cancelar (TASK-052) Implementation Plan, Review Focus, Self-review, Task 0: Preparação e linha de base, Task 10: Confirmar o "Remarcar" consulta o Asaas e só então a nova consulta leva o sinal pago (fluxo, promoção do Portal e portão do "antes") (+15 more)

### Community 814 - "asaas_sandbox_proof.py"
Cohesion: 0.27
Nodes (14): _call(), _charge_lifecycle(), _code(), emit(), _json(), main(), _paid_charge(), Asaas SANDBOX proof for TASK-016 (F1). Automatic-or-skip; never runs in CI. (+6 more)

### Community 815 - "test_express_confirmation_worker.py"
Cohesion: 0.06
Nodes (71): _count_active(), create_unit(), _get_unit(), list_units(), AsyncSession, UUID, Doctor hub — units CRUD (multi_unit addon).  GET   /tenants/me/units      - li, Gate an activate/create-active mutation. Raises HTTPException on failure. (+63 more)

### Community 816 - "asaas_connection.py"
Cohesion: 0.17
Nodes (23): connect_asaas_account(), ConnectionResult, disconnect_asaas_account(), _is_our_url(), _mark_connected(), AsyncSession, datetime, Connect a clinic's Asaas account from an API key the clinic pastes ONCE (spec 12 (+15 more)

### Community 817 - "Antes de começar (uma vez)"
Cohesion: 0.17
Nodes (11): Antes de começar (uma vez), Fechamento (fora das tasks), File Structure, Global Constraints, Portal: rolar pela última mensagem e podar cópias locais (P6) — Implementation Plan, Review Focus, Task 1: Demo `?janela=1` — conversa que serve só as 50 mais novas, e a prova do defeito no navegador, Task 2: O poll da secretarIA poda as cópias locais (+3 more)

### Community 818 - "_Model"
Cohesion: 0.25
Nodes (21): db(), _Model, The ingestion job: success, every failure path, idempotency and what it must NOT, _reload(), _source(), test_a_cancelled_job_still_closes_the_row_and_erases_the_input(), test_a_failing_draft_step_never_fails_the_analysis(), test_a_message_the_material_does_not_cover_is_drafted_for_the_doctor_to_review() (+13 more)

### Community 819 - "adjustment_queries.py"
Cohesion: 0.13
Nodes (22): AdjustmentTotals, collect_item_key(), held_cents_hint(), held_total_cents(), load_adjustment_totals(), open_collect_adjustments(), AsyncSession, datetime (+14 more)

### Community 820 - "ProviderContract"
Cohesion: 0.12
Nodes (7): ProviderContract, The behaviour EVERY `PaymentProvider` must show (spec section 16: "a provider is, An AUTHENTIC webhook (headers, raw body, query) of `kind`:         paid | expir, The same headers with the authenticity proof made wrong., Make the provider's network say the charge is `state`: open|paid|expired|refunde, Make every provider call answer HTTP `status`., Make cancelling answer `outcome`: ok | already_gone | fails.

### Community 821 - "make_world"
Cohesion: 0.20
Nodes (45): make_world(), Every log call of every `secretaria.*` module while the block runs, as dicts., recorded_logs(), asaas(), _begin(), _charge_failed_logs(), db(), _deposits() (+37 more)

### Community 822 - "FakeGoogle"
Cohesion: 0.18
Nodes (15): FakeGoogle, _http_error(), HttpError, CalendarService additions of TASK-017: an idempotent `create_event(event_id=...), `events().insert/get/list(...).execute()` with scripted outcomes., _Request, _service(), test_a_409_on_an_id_of_a_deleted_event_is_not_success() (+7 more)

### Community 823 - "deliver"
Cohesion: 0.11
Nodes (22): Outgoing, interactive_history_body(), Render an interactive card as the single line of text history keeps.      The, Persist one outbound message. Returns the send response.          `interactive, Buttons, conversation_id_for(), deliver(), DeliveryError (+14 more)

### Community 824 - "context_extraction.py"
Cohesion: 0.13
Nodes (47): ClinicFacts, ExtractedInsurancePlan, ExtractedProfessional, ExtractedService, ProposedCatalogService, ProposedInsurance, ProposedNote, ProposedProfessional (+39 more)

### Community 825 - "manage_request.py"
Cohesion: 0.19
Nodes (21): appointment_ref(), find_appointment(), _land_new_day(), manage_target(), ManageResolution, _owner_id(), Any, datetime (+13 more)

### Community 826 - "PaymentAccount"
Cohesion: 0.11
Nodes (20): _cents(), client_for(), _paid_moment(), _parse_dt(), _provider_error(), datetime, Asaas behind the `PaymentProvider` contract (services/payments/provider.py)., `_parse_dt` for a payment date: a bare 'YYYY-MM-DD' carries no time, so it would (+12 more)

### Community 827 - "test_before_booking_flow.py"
Cohesion: 0.18
Nodes (20): _confirm(), _conversation(), db(), SimpleNamespace, The `before` booking flow end to end at the confirmation turn (spec sections 5.2, TASK-071 Plan 3 (was: "...frees the slot and calls a human" with BEFORE_BLOCKED_, TASK-071 Plan 3, ruling R6 (was: "...makes no booking, holds nothing and hands o, TASK-052 (owner decision 2026-10-11) reverts the TASK-050 port that hid it: the (+12 more)

### Community 828 - "evaluate_deposit_readiness"
Cohesion: 0.19
Nodes (16): Badge, _active_professionals(), badge_for(), _deposit_cents(), evaluate_deposit_readiness(), _incomplete_text(), AsyncSession, UUID (+8 more)

### Community 829 - "test_confirmation_card.py"
Cohesion: 0.32
Nodes (10): _at_slot(), The confirmation card names the doctor on switched-on clinics (TASK-030 P3, spec, _switched(), test_a_clinic_without_professionals_has_no_doctor_line(), test_a_single_professional_clinic_names_its_only_doctor(), test_the_card_body_never_exceeds_the_interactive_limit(), test_the_doctor_comes_before_the_patient_line(), test_the_resumed_card_reads_like_the_tapped_one() (+2 more)

### Community 830 - "test_payment_tasks.py"
Cohesion: 0.13
Nodes (29): _client_ip(), _ip_mode(), AsyncSession, Request, Per-clinic payment webhook: POST /webhooks/{provider}/{webhook_slug}.  The cli, The address our proxy saw: the LAST `X-Forwarded-For` hop (the proxy appends the, receive_payment_webhook(), event_to_dict() (+21 more)

### Community 831 - "CHECKPOINT — Contexto da clínica que a LLM enxerga (TASK-025, parte backend)"
Cohesion: 0.17
Nodes (11): 10. Rodada de 2026-10-07 (direto na `main`, local, sem push), 1. O que mudou para o paciente, 2. Onde está cada peça (âncoras estáveis), 3. Contrato do hub (aditivo, congelado em SPEC §3 — o frontend implementa contra ele), 4. O bloco "SOBRE A CLÍNICA" e o orçamento, 5. Validação (2026-10-04), 6. Ordem de deploy (histórica) e reversão, 7. Pendências (+3 more)

### Community 832 - "Lembretes R1 — Fundação (TASK-032)"
Cohesion: 0.09
Nodes (20): Ajustes de execução, Correções da revisão independente (prevalecem sobre o exemplo de reutilização do plano), Estado, Follow-up: deploy informado pelo dono e testes reais, Integração em `main` (follow-up autorizado), Lembretes R1 — Fundação (TASK-032), O que entrou onde, Ordem de liberação (não autorizada ao agente) (+12 more)

### Community 833 - "test_deposit_off_without_account.py"
Cohesion: 0.28
Nodes (15): db(), _decide(), _gate(), AsyncClient, The deposit can be switched OFF without a working Asaas account (TASK-053, owner, TASK-071 Plan 3, ruling R6 (was "...keeps a before clinic fail-closed"): a broke, One clinic whose Asaas account is `account` ("none" = there is no account row at, _rows() (+7 more)

### Community 834 - "test_service_change_policy.py"
Cohesion: 0.15
Nodes (22): ChangeDecision, decide_service_change(), The pure rule of a service change on an appointment with a PAID sinal.  Spec:, _decide(), decide_service_change: the pure rule of a service change on a PAID appointment (, test_a_non_positive_paid_deposit_or_percent_cannot_be_priced(), test_cheaper_service_exactly_at_the_floor_asks(), test_cheaper_service_exactly_min_days_away_still_asks() (+14 more)

### Community 835 - "test_booking_gate_deposit.py"
Cohesion: 0.22
Nodes (28): db(), _decide(), _deposits(), _gate(), _holds(), _no_sleep(), `BookingGate.decide` with a deposit (services/booking_hold.py, spec sections 4.2, TASK-071 Plan 3, ruling R6 (was: "...blocks the booking"): a technical failure. (+20 more)

### Community 837 - "test_payment_attention.py"
Cohesion: 0.28
Nodes (16): _count(), db(), _deposit(), open_attention_item: the ONLY writer of `payment_attention_items` (spec section, _tenant(), test_a_deposit_of_another_tenant_is_refused_and_nothing_is_written(), test_an_orphan_item_needs_no_deposit(), test_an_unknown_reason_is_a_programming_error() (+8 more)

### Community 838 - "test_deposit_adjustment_model.py"
Cohesion: 0.27
Nodes (16): _adjustment(), _config(), db(), _deposit(), _offline(), DepositAdjustment (service change with a paid deposit, 2026-10-26 spec): the tab, test_a_deposit_can_have_more_than_one_adjustment(), test_creates_an_adjustment_that_hangs_on_the_deposit() (+8 more)

### Community 839 - "test_release_offer_carry_over.py"
Cohesion: 0.37
Nodes (16): _booking(), _confirm(), _future(), _on_before(), datetime, The booking that ends a "Remarcar" carries the released slot's PAID deposit (TAS, _rescheduling(), _state() (+8 more)

### Community 840 - "Review Focus"
Cohesion: 0.12
Nodes (16): Global Constraints, Paralelismo, Review Focus, Task 10: Documentação e registro, Task 11: Prova ponta a ponta no navegador, Task 12–15: Sessões do pacote (após 1–11, sequenciais), Task 1: Config `pix_difference_refund_min_days` (secretarIA), Task 2: Campo no painel Pix (Brain-Message-Frontend) (+8 more)

### Community 841 - "test_flow_draft_column.py"
Cohesion: 0.31
Nodes (9): _migration(), _parked(), Conversation.flow_draft: the AI draft parked while pra-quem is answered (TASK-03, _stale(), test_the_attendee_step_floor_drops_the_draft(), test_the_draft_rides_every_attendee_step(), test_the_llm_floor_drops_the_draft(), test_the_migration_adds_and_drops_a_nullable_json_column_on_sqlite() (+1 more)

### Community 842 - "CHECKPOINT — IA entra em qualquer etapa: rascunho v2, `flow_draft` e o resolvedor (TASK-030 P2)"
Cohesion: 0.20
Nodes (9): 1. O que muda para o paciente, 2. Onde está cada peça (âncoras), 3. Interruptor por clínica, 4. Observabilidade, 5. Decisões e limites conhecidos, 6. Pendências, 7. Deploy (quando o dono pedir), 8. Validação local desta correção (+1 more)

### Community 843 - "CHECKPOINT — Portal: a primeira mensagem chega ao ENTRAR, escolhida pelo contexto (TASK-035)"
Cohesion: 0.20
Nodes (9): CHECKPOINT — Portal: a primeira mensagem chega ao ENTRAR, escolhida pelo contexto (TASK-035), Mudança que afeta o WhatsApp (decisão do dono pendente), O que entrou, O que NÃO foi provado, Ordem de deploy (nada disto foi executado), Por que o paciente só via algo depois de escrever, Qual mensagem (primeira regra que casar), Quando a entrada fica em silêncio (`_brain_message_entry_decision`) (+1 more)

### Community 844 - "history_item_is_final"
Cohesion: 0.19
Nodes (16): history_item_is_final(), One chunk of a WhatsApp Coexistence chat-history sync (`history` field)., True when this `history` chunk signals the sync is FULLY complete.      Three, WebhookHistoryItem, _history_item(), COMPLETE_CHUNK means "this chunk is done, more are coming" - not final., Unknown-variant tolerance: a numeric 100% is enough even without a     literal, A non-numeric `progress` (unknown variant) must never raise. (+8 more)

### Community 845 - "test_before_interplay.py"
Cohesion: 0.25
Nodes (12): _capture_usage(), db(), _fulfill(), _pay(), How a `before` reservation coexists with what already reads appointments (spec s, The Review Focus race, sequentially: the Pix window ends and the slot is release, _reserve(), test_an_expired_or_abandoned_reservation_never_reaches_metering() (+4 more)

### Community 846 - "_Agenda"
Cohesion: 0.12
Nodes (6): _Agenda, _FakeWhatsAppClient, _LogRecorder, MonkeyPatch, The owner's agenda: 14:00 and 14:30 free on NEW_DAY. Records writes and reads., wired()

### Community 847 - "test_patient_insurance_overview.py"
Cohesion: 0.27
Nodes (16): client(), db(), _get(), What a PATIENT may see of the clinic's convenios (TASK-053, owner decision 2026-, A free-text convenio from before the catalog: the booking carries no plan id, so, Shared mode: Unimed (charges) + the clinic's own "GEAP" (no deposit, with a note, _row(), _shared_clinic() (+8 more)

### Community 848 - "ClinicPaymentAccount"
Cohesion: 0.11
Nodes (33): ChargeHealth, ClinicPaymentAccount, The clinic's connection to ONE payment provider., charge_health(), charge_health_view(), ChargeHealthView, get_account_by_slug(), get_account_row() (+25 more)

### Community 849 - "ProbeResponse"
Cohesion: 0.18
Nodes (19): ProbeResponse, classify(), decide(), decide_informational(), effective_qualified_groups(), _probe_with_retries(), ProbeKind, ProbeSpec (+11 more)

### Community 850 - "next_retry_time"
Cohesion: 0.20
Nodes (14): days_elapsed(), _next_checkpoint_day(), next_retry_time(), datetime, Pure cadence/due-date math for the onboarding nudge cron (contract v1 §11/§12)., Whether `now_local`'s wall-clock time falls inside `window`     ("HH:MM-HH:MM",, Fractional days between `anchor` and `now` (may be negative).      Both must b, Smallest checkpoint (day-offset from anchor) STRICTLY greater than     `elapsed (+6 more)

### Community 851 - "settings.py"
Cohesion: 0.19
Nodes (14): before_blockers(), PaymentSettingsInvalid, provider_choices(), provider_switch_blockers(), ProviderChoice, AsyncSession, UUID, Business rules of the clinic's payment settings (spec sections 6.4, 12.1, 12.2). (+6 more)

### Community 852 - "test_payment_models.py"
Cohesion: 0.29
Nodes (14): db(), AsyncSession, Schema contract of the payment provider core (TASK-016, spec sections 6.1-6.5)., _tenant(), test_account_defaults(), test_account_is_unique_per_tenant_and_provider_and_slug_is_unique(), test_attention_item_dedupe_key_is_unique_per_tenant(), test_attention_item_defaults() (+6 more)

### Community 853 - "resolve_attention_item"
Cohesion: 0.13
Nodes (19): RuntimeError, _detail(), _item_out(), list_attention_items(), AsyncSession, JSONResponse, Request, Doctor hub - "Pagamentos que precisam de decisão" (spec sections 6.5 and 10A.2). (+11 more)

### Community 854 - "_FakeRedis"
Cohesion: 0.15
Nodes (8): _is_rate_limited(), rate_limit - split out of workers/tasks.py (TASK-023)., Sliding-window inbound rate limit per wa_id, backed by the arq Redis pool., _FakeRedis, Minimal async Redis stub covering the commands _is_rate_limited uses., test_rate_limit_allows_under_cap_then_silences(), test_rate_limit_disabled_without_redis(), test_rate_limit_is_per_sender()

### Community 855 - "insert_ignore"
Cohesion: 0.33
Nodes (6): insert_ignore(), Any, AsyncSession, UUID, Small dialect-aware DB helpers shared by the payments code., `INSERT ... ON CONFLICT (index_elements) DO NOTHING RETURNING id`.      Return

### Community 856 - "AST"
Cohesion: 0.21
Nodes (9): AST, inventory(), literal(), Gera o dossiê offline, seu inventário de rotas e a edição PDF. Sem acesso à apli, _is_channel(), Counter, Ratchet: neutral worker code must not branch on the channel (TASK-024).  Scans, _scan() (+1 more)

### Community 857 - "test_migration_reminders_foundation.py"
Cohesion: 0.42
Nodes (8): Engine, _load(), The R1 migration: additive, reversible, in sync with the model (TASK-032).  Th, _run(), _stub_engine(), test_migrated_table_has_exactly_the_model_columns(), test_there_is_one_head_and_reminder_foundation_is_in_its_history(), test_upgrade_adds_columns_and_table_then_downgrade_removes_them()

### Community 858 - "_post_booking"
Cohesion: 0.28
Nodes (12): _claim(), _conversation_id(), _ledger_key(), _post_booking(), UUID, CORE post_booking hook: ask a Brain-Message visitor for the code, after booking., Give the ledger row back: the code notice did NOT go out. Best-effort., This patient's conversation row, which IS the Brain-Message address.      `Bra (+4 more)

### Community 859 - "brain_onboarding.py"
Cohesion: 0.26
Nodes (12): _item_from_dict(), list_onboarding_tenants(), OnboardingTenant, _parse_dt(), post_onboarding_event(), datetime, UUID, Onboarding cron -> brain-api client (cross-service contract v1 §4 endpoints 7/8) (+4 more)

### Community 860 - "accept_keep"
Cohesion: 0.31
Nodes (13): accept_keep(), _consultation_overlaps(), _end_read(), keep_event_id(), KeepResult, _kept(), AsyncSession, datetime (+5 more)

### Community 861 - "test_payment_accounts.py"
Cohesion: 0.36
Nodes (13): db(), Payment accounts: the single decrypt point, dual-read of the legacy Asaas column, _tenant(), test_a_legacy_only_tenant_can_charge_after_but_never_before(), test_customer_id_roundtrip_legacy_fallback_and_tenant_isolation(), test_get_or_create_is_idempotent_and_slugs_are_unique_opaque_uuids(), test_has_asaas_api_key_is_true_for_an_account_row_alone(), test_legacy_asaas_columns_are_read_when_there_is_no_account_row() (+5 more)

### Community 862 - "quote_deposit"
Cohesion: 0.18
Nodes (14): before_gate_active(), clamp_ttl(), AsyncSession, UUID, quote_deposit(), quote_with_retries(), The `before` gate: does THIS booking need a Pix deposit first? (spec sections 5., Whether bookings of this clinic must go through the deposit gate at all. (+6 more)

### Community 863 - "release_carry_check.py"
Cohesion: 0.27
Nodes (12): _account(), _ask_provider(), check_offer_deposit(), ensure_checked(), datetime, UUID, Before a "Remarcar" booking reuses a released slot's PAID deposit, read the char, Read-only provider check of an offer's deposit, for the clinic's acceptance of " (+4 more)

### Community 864 - "release_deadlines.py"
Cohesion: 0.29
Nodes (12): closing_time(), decide_by(), expires_at(), _hhmm(), date, datetime, time, ZoneInfo (+4 more)

### Community 865 - "ingest_context_source"
Cohesion: 0.29
Nodes (12): _claim(), _draft_model(), _extraction_model(), _finish(), ingest_context_source(), _load_inputs(), UUID, arq job: read the source, extract a verified proposal, store it, erase the input (+4 more)

### Community 866 - "test_asaas_sandbox_proof_script.py"
Cohesion: 0.28
Nodes (12): _load(), The sandbox proof script must be incapable of echoing a key, a body or a documen, test_a_production_key_in_the_environment_is_a_skip_and_is_never_used(), test_a_scoped_key_that_is_not_sandbox_is_dropped_not_used(), test_accepts_a_sandbox_key_shape(), test_both_keys_enable_the_full_qualification_run(), test_emit_prints_only_status_and_symbolic_code_never_the_body(), test_emit_rejects_free_text_extras() (+4 more)

### Community 867 - "CHECKPOINT — Contexto da clínica por fontes (TASK-026)"
Cohesion: 0.17
Nodes (12): Catálogo: profissionais, serviços e convênios (TASK-067, 2026-10-23, pedido do dono), CHECKPOINT — Contexto da clínica por fontes (TASK-026), Como funciona, Contrato (aditivo, `api/hub/context_sources.py`), Cotas e limites, Defesas de memória e CPU (revisão de segurança, 2026-10-11), Deploy e reversão, `error_code` (o que o gestor lê está em `lib/contexto/context-sources.ts` do front) (+4 more)

### Community 868 - "CHECKPOINT — Sinal Pix ANTES de confirmar: hold, promoção por pagamento, itens de atenção (TASK-017, fase F2 da TASK F)"
Cohesion: 0.17
Nodes (11): 1. O que entrou e onde, 2. Contrato congelado ao fim da F2 (a F4 constrói sobre isto), 3. Decisões tomadas sem perguntar (com o porquê), 4. Ordem de deploy (regra `frozen-contract-migration`), 5. O que NÃO foi feito (de propósito), 6. Prova, 7. Pendências e riscos conhecidos, 8. Pendências não bloqueantes (ação do dono depois do merge) (+3 more)

### Community 869 - "CHECKPOINT — Núcleo de provedores de pagamento (TASK-016, fase F1 da TASK F)"
Cohesion: 0.14
Nodes (14): 1. O que entrou e onde, 2. Decisões tomadas sem perguntar (com o porquê), 3. Contrato congelado ao fim da F1 (F2/F3/F4 constroem sobre isto), 4. Ordem de deploy (regra `frozen-contract-migration`), 5. O que NÃO foi feito (de propósito), 6. Prova, 7. Pendências e riscos conhecidos, 8.1 Contrato congelado para a sessão 03 (tela) (+6 more)

### Community 870 - "get_payment_settings"
Cohesion: 0.23
Nodes (10): _asaas_environment(), _blockers(), get_payment_settings(), AsyncSession, Doctor hub - the read side of the deposit settings (spec sections 12.1 and 12.2), Which Asaas the backend talks to, from ASAAS_BASE_URL (the same test the key-for, PaymentSettingsOut, ProviderChoiceOut (+2 more)

### Community 871 - "handle_release_offer_tap"
Cohesion: 0.39
Nodes (11): _Answer, _closed_answer(), handle_release_offer_tap(), _on_cancel(), _on_keep(), _on_not_received(), _on_received(), _on_reschedule() (+3 more)

### Community 872 - "test_appointment_replacement_service.py"
Cohesion: 0.36
Nodes (12): _future(), _new_booking(), datetime, cancel_replaced_appointment: guards, money hook, transition (TASK-032 R3, spec §, _replace(), test_a_cancelled_or_past_original_is_left_alone(), test_a_failing_money_hook_still_cancels_the_original(), test_a_live_future_original_is_cancelled() (+4 more)

### Community 873 - "test_staff_notice_rows.py"
Cohesion: 0.28
Nodes (11): _ensure(), The rows behind the clinic's confirm/edit cards are tap targets and nothing else, _rows(), test_a_new_start_or_a_retired_row_gets_a_fresh_row(), test_a_staff_row_is_born_sent_without_a_clinic_warning(), test_a_staff_row_never_turns_the_agenda_red_nor_blocks_the_opening(), test_kinds_are_validated_and_independent(), test_the_engine_never_picks_a_staff_row() (+3 more)

### Community 874 - "test_payment_attention_roles.py"
Cohesion: 0.45
Nodes (11): _bearer(), db(), AsyncClient, Who may resolve an attention item (owner answer 12, TASK-020).  `resolve` depe, _status(), test_a_manager_and_a_doctor_resolve_an_item(), test_a_secretary_can_still_read_the_items(), test_a_secretary_cannot_resolve_an_item_and_it_stays_open() (+3 more)

### Community 875 - "test_release_deadlines.py"
Cohesion: 0.27
Nodes (9): _hours(), services/release_deadlines.py - when a released PAID slot's refund falls due (TA, test_a_day_without_hours_or_a_clinic_without_hours_gets_six_hours(), test_an_early_closing_comes_first(), test_b_is_never_before_t_nor_after_e(), test_released_after_the_closing_margin_gets_six_hours_never_less_than_t(), test_the_clinic_zone_decides_the_day_and_the_closing(), test_the_closing_is_the_latest_end_of_the_day() (+1 more)

### Community 876 - "CHECKPOINT — Liberar horário com sinal pago: manter, remarcar ou cancelar (TASK-052)"
Cohesion: 0.18
Nodes (10): 1. O que é, 2. Onde está cada coisa, 3. Decisões e perguntas, 4. Validação, 5. Deploy (NÃO autorizado), 6. Revisão final e decisões de execução (2026-10-12), 7. A aba existe: "Consultas liberadas" no Brain-Message-Frontend (TASK-067 sessão 05, 2026-10-26 — LOCAL, não commitado), 8. Task 16 ligada: "clínica com secretária" usa o horário da clínica (TASK-067 sessão 06, 2026-10-26 — LOCAL, não commitado) (+2 more)

### Community 877 - "create_insurance_catalog_entry"
Cohesion: 0.20
Nodes (10): create_insurance_catalog_entry(), list_unmatched_insurance_strings(), AsyncSession, Admin (SaaS-owner) — extend the global convênio catalog without a code migration, Every legacy convênio string still waiting for a matching catalog entry., InsuranceCatalogAdminCreate, InsuranceCatalogAdminRead, InsuranceCatalogUnmatchedRead (+2 more)

### Community 878 - "_alert_one"
Cohesion: 0.35
Nodes (10): alert_new_items(), _alert_one(), AlertReport, _is_mine(), AsyncSession, datetime, UUID, E-mail the clinic when a patient cancels a consultation whose deposit was alread (+2 more)

### Community 879 - "_flow_tenant_snapshot"
Cohesion: 0.16
Nodes (17): _conversation_snapshot(), _flow_professional(), _flow_tenant_snapshot(), What workers/tasks.py::_flow_tenant_snapshot builds for this clinic., The plain snapshot the worker passes to route(professionals=...)., Zero active professionals: nothing to attribute the booking to., A tapped, truncated or differently-cased type is canonicalised., test_flow_booking_carries_the_single_professional_as_owner() (+9 more)

### Community 880 - "ask_after_booking"
Cohesion: 0.36
Nodes (9): ask_after_booking(), _leave(), AsyncSession, Kind, UUID, `after` timing: the consultation already exists; ask for what the provider needs, The answer was stored: create and send the Pix of the consultation that was wait, Ask the patient for `kind` and arm the wait. True when the question went out; Fa (+1 more)

### Community 881 - "test_asaas_sandbox_e2e_script.py"
Cohesion: 0.36
Nodes (9): _load(), The sandbox end-to-end script must refuse anything that is not a throwaway sandb, test_accepts_a_local_throwaway_database(), test_main_without_keys_skips_before_touching_the_database_or_the_network(), test_missing_or_non_sandbox_keys_are_a_skip_not_a_failure(), test_refuses_a_database_that_is_not_local(), test_refuses_a_non_sandbox_key(), test_refuses_the_production_asaas_host() (+1 more)

### Community 882 - "CHECKPOINT — Lembretes R9: lembrete extra "N dias antes, às HH:MM" (TASK-048)"
Cohesion: 0.22
Nodes (9): CHECKPOINT — Lembretes R9: lembrete extra "N dias antes, às HH:MM" (TASK-048), Decisões, Deploy (quando o dono autorizar), Estado, Integração autorizada (2026-10-10), O que entrou onde, Pendências, Revisao final (+1 more)

### Community 883 - "Troca de serviço com sinal pago — diferença do sinal (design)"
Cohesion: 0.22
Nodes (8): Antiobjetivos, Decisões minhas (a conferir), Modelo de dados (secretarIA), Pacote completo, Pontos de código, Por quê, Regras (decisão do dono, 2026-10-26), Troca de serviço com sinal pago — diferença do sinal (design)

### Community 884 - "main"
Cohesion: 0.33
Nodes (7): main(), End-to-end proof of the `after` timing against the Asaas SANDBOX (TASK-016, Task, (default key, scoped key, skip reason). A missing or non-sandbox key is a SKIP;, require_local_database(), require_sandbox_base_url(), resolve_keys(), step()

### Community 885 - "patient_insurance_overview"
Cohesion: 0.36
Nodes (8): _charge_flags(), _deposit_rule(), patient_insurance_overview(), AsyncSession, UUID, What a PATIENT may see of the clinic's convenios (TASK-053, owner decision 2026-, Plan id (the same id space as `load_tenant_insurance`'s plans) -> the deposit fl, The patient-facing view of one clinic's convenios, or None for an unknown clinic

### Community 886 - "test_service_change_postgres.py"
Cohesion: 0.39
Nodes (8): _awaiting(), maker(), Concurrency proofs of the service-change difference that SQLite cannot give (TAS, Cancel transaction: the appointment row first (the guarded UPDATE of every cance, `base._seed`'s rows and values, with the tenant flushed on its own first: Postgr, _seed_pg(), test_a_difference_paid_while_its_consultation_is_cancelled_never_deadlocks(), test_many_workers_apply_one_paid_difference_exactly_once()

### Community 887 - "CLAUDE.md"
Cohesion: 0.14
Nodes (7): TASK-054 — Boas-vindas cifradas e profissionais, TASK-055 — Base pública única do Brain-Message, Abordagem proposta, Aviso ao médico e entrada por lembrete — especificação proposta, Comportamento exigido, Fatos e limites, Objetivo do dono

### Community 888 - "env.py"
Cohesion: 0.29
Nodes (7): Connection, _do_run_migrations(), Alembic environment - async (asyncpg) configuration., Run migrations in 'offline' mode (emit SQL, no live DB connection)., Run migrations in 'online' mode using an async engine., run_migrations_offline(), run_migrations_online()

### Community 889 - "CHECKPOINT — Simulação do fluxo "Agendar" (TASK-027)"
Cohesion: 0.25
Nodes (7): CHECKPOINT — Simulação do fluxo "Agendar" (TASK-027), Como funciona, Contrato, Garantias e testes, O que NÃO é simulado, Pendências, Reverter

### Community 890 - "CHECKPOINT — TASK-050: `main` trazida para `feature/out-of-mvp` (Pix portado para os workers divididos)"
Cohesion: 0.25
Nodes (7): 1. O que o merge precisou além de juntar texto, 2. Decisões tomadas no merge (revisar), 3. Validação, 3a. Correções da revisão (`tasks/TASK-050/REVIEW.md`, I-1 e I-2), 4. Não provado, 5. Pré-condições e decisões conscientes (fecha m-4 e m-5 da revisão), CHECKPOINT — TASK-050: `main` trazida para `feature/out-of-mvp` (Pix portado para os workers divididos)

### Community 891 - "_WorkersNamespace"
Cohesion: 0.32
Nodes (4): ModuleType, Fan-out proxy. Per-module originals are remembered, because the value pytest's, _split_modules(), _WorkersNamespace

### Community 892 - "CHECKPOINT — confirmação expressa, retomada do rascunho e gerenciamento v2 (TASK-030 P3)"
Cohesion: 0.12
Nodes (15): Captura corrigida integrada — 2026-10-07, CHECKPOINT — confirmação expressa, retomada do rascunho e gerenciamento v2 (TASK-030 P3), Conclusão do P3 — 2026-10-06, Correção da captura no caminho atual — 2026-10-07, Correção da revisão: consultas simultâneas, Correções, Integração para a próxima sessão de testes — 2026-10-06, Integração/publicação autorizadas — 2026-10-06 (+7 more)

### Community 893 - "TASK-030 — IA entra em qualquer etapa: P2a"
Cohesion: 0.29
Nodes (6): Decisões e pendências para a continuação, Migração comprovada em SQLite e PostgreSQL, O que está construído, Revisão independente e correções, TASK-030 — IA entra em qualquer etapa: P2a, Validação

### Community 894 - "test_appointment_replacement_router.py"
Cohesion: 0.26
Nodes (11): _in_booking(), The router keeps "Marcar outra" only inside the booking (TASK-032 R3, spec §4.3), test_a_booked_result_never_carries_the_marker(), test_a_booking_step_keeps_the_marker(), test_a_marker_left_on_an_idle_conversation_never_reaches_a_new_booking(), test_a_marker_the_result_names_is_kept_as_named(), test_cancelar_on_the_card_keeps_the_marker_for_the_retry(), test_leaving_the_booking_drops_the_marker() (+3 more)

### Community 895 - "test_workers_layering.py"
Cohesion: 0.23
Nodes (9): _imported_workers_modules(), _modules(), Path, Layer rules for the split of workers/tasks.py (TASK-023).    shared/*     neut, `secretaria.workers.<first>` segments imported by a file., A cycle only shows for some import orders; a fresh interpreter per entry point, test_channel_packages_respect_the_layers(), test_composition_modules_never_import_the_facade() (+1 more)

### Community 896 - "test_deposit_release_offer_migration.py"
Cohesion: 0.39
Nodes (6): _config(), _offline(), Migration b6d2e8f4a1c3 - release offers and their scheduled sends (TASK-052)., test_downgrade_drops_only_those_tables(), test_the_revision_is_the_single_head_right_after_the_before_timing(), test_upgrade_only_creates_the_two_new_tables()

### Community 897 - "test_payment_arch.py"
Cohesion: 0.29
Nodes (7): _names_and_strings(), Path, Architecture guards of the payment core: rules that must hold for EVERY future c, A Cobrancas-only key may (NOT VERIFIED) be able to split a charge to another wal, test_no_charge_body_is_ever_built_with_a_split(), test_no_payment_source_calls_or_defines_a_refund(), test_the_asaas_client_and_provider_expose_no_refund_method()

### Community 898 - "extract_action_button"
Cohesion: 0.24
Nodes (11): extract_action_button(), Decode a reminder action-button reply into (action, appointment_id).      Two, _interactive_msg(), _template_button_msg(), test_extract_action_button_both_carriers_agree(), test_extract_action_button_malformed_uuid_returns_none(), test_extract_action_button_no_button_at_all_returns_none(), test_extract_action_button_unknown_prefix_returns_none() (+3 more)

### Community 899 - "9. Troca de serviço com diferença do sinal (TASK-067, 2026-10-26 — LOCAL: não commitada, não publicada, não deployada)"
Cohesion: 0.29
Nodes (7): 9.1 Regras (decisão do dono, 2026-10-26), 9.2 Dados e configuração, 9.3 Fluxo e módulos, 9.4 Ordem de deploy (quando o dono autorizar — nada foi deployado), 9.5 Riscos conhecidos / adiados (reauditados na TASK-067 sessão 07, 2026-10-26 — LOCAL), 9.6 Prova e estado, 9. Troca de serviço com diferença do sinal (TASK-067, 2026-10-26 — LOCAL: não commitada, não publicada, não deployada)

### Community 900 - "FakeRedis"
Cohesion: 0.22
Nodes (8): ServiceRole, Last identity `service` announced, or None. Never raises.      The result is f, read_build_identity(), FakeRedis, A reader that forwards whatever it finds is one poisoned key from a leak., `set`/`get` in a dict — the only two calls build_info makes., test_read_build_identity_drops_unexpected_fields(), test_read_build_identity_survives_garbage()

### Community 901 - "is_clinic_clinician"
Cohesion: 0.33
Nodes (6): is_clinic_clinician(), is_clinic_manager(), Role policy for hub callers (TASK-020) — pure, framework-agnostic.  The role r, True for `manager`, or for a `doctor` flagged owner/manager. Never for `secretar, True for `manager` and `doctor`; never for `secretary` or an unknown/absent role, test_role_policy_truth_table()

### Community 902 - "simulation.py"
Cohesion: 0.24
Nodes (9): AsyncSession, POST /tenants/me/simulation/turn - one patient turn of the booking flow, simulat, What the manager is editing and has NOT saved. Same shape as the Save envelope., The conversation position. Every field is validated: the client is not trusted., simulation_turn(), SimulationDraft, SimulationState, SimulationTurnIn (+1 more)

### Community 903 - "CHECKPOINT — convênios sem corte e antecedência mínima por médico (2026-10-27)"
Cohesion: 0.33
Nodes (5): 1. Lista de convênios paginada, 2. Antecedência mínima para agendar (por médico), 3. Deploy (ordem obrigatória), 4. Não provado, CHECKPOINT — convênios sem corte e antecedência mínima por médico (2026-10-27)

### Community 904 - "CHECKPOINT — Chat vendor switch: OpenAI or Claude (TASK-069)"
Cohesion: 0.33
Nodes (5): CHECKPOINT — Chat vendor switch: OpenAI or Claude (TASK-069), Haiku 5.5 como padrão (2026-10-22, pedido do dono), O que entrou, Pendências, Prova (2026-10-22, chaves locais, nunca exibidas)

### Community 906 - "c8a5d3f1b7e2_payment_provider_core.py"
Cohesion: 0.40
Nodes (4): _backfill(), payment provider core: accounts, webhook ledger, customers, attention items  T, Copy the Asaas data of the old columns into the new tables. Idempotent.      U, upgrade()

### Community 907 - "test_edit_day_picker.py"
Cohesion: 0.31
Nodes (8): _carrier(), _in_edit(), The day picker's anchor, the edit branch and the draft carry (TASK-032 R6, spec, test_a_far_anchor_moves_the_scan_window_to_it(), test_an_ai_detour_inside_the_edit_keeps_the_draft(), test_the_draft_is_carried_only_while_the_edit_continues(), test_the_edit_branch_carries_the_appointment_and_the_draft(), test_with_an_anchor_the_nearest_days_come_first_and_are_shown_in_order()

### Community 908 - "CHECKPOINT — TASK-072: nenhum horário fica guardado; troca de e-mail mantém o agendamento"
Cohesion: 0.22
Nodes (6): 1. Decisões do dono (vinculantes), 2. O que mudou, 3. Deploy (quando autorizado), 4. Testes, 5. Riscos / pendências, CHECKPOINT — TASK-072: nenhum horário fica guardado; troca de e-mail mantém o agendamento

### Community 909 - "Recorder"
Cohesion: 0.25
Nodes (4): The exact 2026-08-16 shape: worker on an older commit than the API., Captures structlog calls (house pattern, see test_appointment_status_taxonomy)., Recorder, test_deploy_parity_alarms_on_divergence()

### Community 911 - "_open_paid_offer"
Cohesion: 0.47
Nodes (6): _offer_text(), _open_paid_offer(), TASK-052: same job, same arguments; the open offer decides the card., test_a_released_paid_slot_carries_the_choices_and_the_body_as_is(), test_an_offer_already_answered_sends_nothing(), test_outside_the_window_the_offer_goes_as_the_pending_meta_template()

### Community 912 - "full_extraction"
Cohesion: 0.60
Nodes (5): full_extraction(), _proposal(), What a complete, correct model answer for SOURCE looks like., test_each_service_goes_to_the_doctors_the_text_names(), test_every_service_of_the_text_reaches_the_proposal_with_its_terms()

### Community 913 - "_notify_portal"
Cohesion: 0.39
Nodes (8): notify_cancelled_patient(), _notify_portal(), notify_released_patient(), AsyncSession, datetime, Telling a patient the clinic freed their slot (TASK-032 R4, spec 4.4).  Two ch, The patient notice of POST /cancel (TASK-032 R7 §2). Never raises.      * Port, Tell the patient; returns one `NOTICE_*` code. Never raises.      `offer_butto

### Community 914 - "Code conventions & project structure"
Cohesion: 0.40
Nodes (5): Code conventions & project structure, Folder granularity — group by domain, not "one folder per file", General, Layering (where code belongs), Onde vive o código do Portal e do WhatsApp (workers)

### Community 915 - "b6d2e8f4a1c3_deposit_release_offers.py"
Cohesion: 0.60
Nodes (3): Column, _ts(), upgrade()

### Community 916 - "send_payment_alert"
Cohesion: 0.50
Nodes (4): UUID, Clinic payment email gate: only a patient's cancellation of a paid deposit quali, Attempt the one allowed email at most once per attention item. Never raises., send_payment_alert()

### Community 917 - "test_deploy_parity_cron_publishes_and_compares"
Cohesion: 0.29
Nodes (6): check_deploy_parity_cron(), ParityVerdict, arq cron: the recurring deploy-divergence alarm (FIX_01 §5.2).  `on_startup` i, Re-announce this worker's build identity and alarm if the API differs.      Re, The recurring alarm: a divergence introduced AFTER startup still fires., test_deploy_parity_cron_publishes_and_compares()

### Community 918 - "_FakeWhatsApp"
Cohesion: 0.40
Nodes (3): _fakes(), _FakeWhatsApp, MonkeyPatch

### Community 919 - "test_migration_min_booking_notice.py"
Cohesion: 0.60
Nodes (4): _migration(), professionals.min_booking_notice_hours (2026-10-27): additive, nullable, one new, test_the_migration_is_in_the_single_head_history_on_top_of_the_native_agenda(), test_upgrade_adds_the_nullable_column_the_model_maps_and_downgrade_drops_it()

### Community 922 - "Atualização de feature/out-of-mvp — TASK-066"
Cohesion: 0.50
Nodes (3): Atualização de feature/out-of-mvp — TASK-066, Continuação: main remota e agenda nativa, Primeira rodada: main local

### Community 923 - "fetch_patient_email_result"
Cohesion: 0.33
Nodes (8): fetch_patient_email(), fetch_patient_email_result(), PatientEmailResult, UUID, Read a Portal patient's booking contact from the identity authority.  The call, Availability refers to a valid authority answer, including email=null., Compatibility interface; callers needing authority use the result variant., Fetch one clinic-scoped contact; external_id is the Portal UUID string.

### Community 935 - "_FakeCalendar"
Cohesion: 0.28
Nodes (5): fake_google(), _FakeCalendar, _FakeCalendarFactory, MonkeyPatch, Stands in for CalendarService. Answers each probe by the refresh token it     w

### Community 936 - ""Outra data": calendário de dias livres (2026-11-04)"
Cohesion: 0.25
Nodes (7): Contrato, Decisões sem consulta, Deploy, Limite conhecido, O que mudou, "Outra data": calendário de dias livres (2026-11-04), Validação

### Community 939 - "CHECKPOINT — TASK-073: respostas sigilosas mascaradas + e-mail no contexto da paciente"
Cohesion: 0.25
Nodes (7): Brain-Message-Frontend (mesma tarefa), CHECKPOINT — TASK-073: respostas sigilosas mascaradas + e-mail no contexto da paciente, Limites conhecidos, O problema, O que mudou, Ordem de deploy, Verificação

### Community 940 - "SecretarIA — memória de cálculo e fontes"
Cohesion: 0.25
Nodes (7): Como ler, Fontes locais e escopo, Modelo econômico, Não incluído na estimativa LLM, O que medir para conhecer a média, Referências externas, SecretarIA — memória de cálculo e fontes

### Community 941 - "CHECKPOINT — Sinal (Pix) é recurso padrão da SecretarIA, não add-on"
Cohesion: 0.29
Nodes (6): CHECKPOINT — Sinal (Pix) é recurso padrão da SecretarIA, não add-on, Deploy, Fora deste repo (pendente, decisão comercial), O que mudou no código (secretarIA), Por que mudou, Regra vigente

### Community 942 - ""voltar" sends the recurring-patient message — Implementation Plan"
Cohesion: 0.29
Nodes (6): Global Constraints, Review Focus, Self-review, Task 1: `voltar` renders the recurring message as the menu card body, Task 2: Whole-suite validation and checkpoint, "voltar" sends the recurring-patient message — Implementation Plan

### Community 943 - "test_context_source_model.py"
Cohesion: 0.38
Nodes (4): db(), The `context_sources` table: registered on the metadata, defaults, cascade, one, test_a_new_source_starts_pending_with_a_proposal_slot(), test_deleting_the_clinic_deletes_its_sources()

### Community 944 - "Medições do Asaas real (TASK-071, Plano 4)"
Cohesion: 0.33
Nodes (5): Como medir, Conclusões, Medições do Asaas real (TASK-071, Plano 4), Pendente, Resultado — rodado pelo dono em 2026-10-30, conta real (produção)

### Community 945 - "TASK-072 (contexto da IA) - nome/e-mail, perguntas em duas partes, recursos ligados/desligados"
Cohesion: 0.33
Nodes (5): O que entrou, Pendencias / pontos de atencao, Prompt v1 mudou de bytes, TASK-072 (contexto da IA) - nome/e-mail, perguntas em duas partes, recursos ligados/desligados, Testes

### Community 946 - "CHECKPOINT — "voltar" envia a mensagem para paciente recorrente (TASK-076)"
Cohesion: 0.33
Nodes (5): CHECKPOINT — "voltar" envia a mensagem para paciente recorrente (TASK-076), Deploy / pendências, O que mudou (visão do paciente), Onde (âncoras estáveis), Validação

### Community 947 - "panel.py"
Cohesion: 0.47
Nodes (5): Administrative endpoints (currently: data wipe).  Every route here is guarded, Reset the database. Requires `confirm: true` to actually run., reset_data(), ResetRequest, ResetResponse

### Community 948 - "get_context_completeness"
Cohesion: 0.47
Nodes (5): CompletenessItemRead, CompletenessRead, get_context_completeness(), AsyncSession, GET /tenants/me/context-completeness - what is still missing in the clinic's con

### Community 952 - "HookSpy"
Cohesion: 0.33
Nodes (4): HookSpy, Stands in for services/reminder_hooks.py's three hooks and records the calls., spy(), spy()

### Community 953 - "require_professional_capacity"
Cohesion: 0.50
Nodes (3): ProfessionalCapacityError, The same capacity check for hub and interactive identity provisioning., require_professional_capacity()

### Community 954 - "test_the_right_code_activates_the_account_and_ends_the_wait"
Cohesion: 0.40
Nodes (5): _inbound(), Non-regression: the buttons are an addition, not a replacement., CHECKLIST: the correct code confirms the account., test_the_right_code_activates_the_account_and_ends_the_wait(), test_typing_the_code_still_works_with_the_card_on_screen()

### Community 955 - "test_redis_failure_never_propagates"
Cohesion: 0.40
Nodes (4): BrokenRedis, Observability must never be able to take a service down at startup., Every call raises — the "Redis went away mid-flight" shape., test_redis_failure_never_propagates()

### Community 956 - "datetime"
Cohesion: 0.40
Nodes (5): datetime, Brazil abolished DST in 2019: the UTC offset must be identical across     the y, Passing tz-aware datetimes in America/Sao_Paulo vs UTC must yield the     exact, test_america_sao_paulo_has_no_dst_offset_shift(), test_next_retry_time_identical_across_utc_and_local_tz_labels()

### Community 957 - "check_scopes.py"
Cohesion: 0.67
Nodes (3): main(), Diagnose Fase A auth: what scopes does our refresh token actually carry?  A re, _short()

### Community 965 - "load_reminder_content"
Cohesion: 0.67
Nodes (3): load_reminder_content(), AsyncSession, Resolve doctor, service requirements and attendee for one appointment.      Sa

## Knowledge Gaps
- **1435 isolated node(s):** `secretaria`, `_Claim`, `Product vision`, `The "Eye Company" scaffold is gone — keep it that way`, `Per-tenant config (what each clinic can configure)` (+1430 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **192 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Tenant` connect `Tenant` to `test_onboarding_cron.py`, `_outbound`, `test_reminders_plugin.py`, `test_hub_professionals.py`, `_tenant`, `test_action_buttons.py`, `test_professional_notification.py`, `test_hub_oauth.py`, `tenants.py`, `test_webhook_history_sync.py`, `test_hub_calendar_owner_agenda.py`, `test_reactivation.py`, `seed_world`, `test_remove_context_command.py`, `tenant_config.py`, `test_deposit_lifecycle.py`, `test_hub_calendar_confirmation.py`, `test_patient_name_step.py`, `BrainMessageSender`, `test_internal_provisioning.py`, `onboarding_cron.py`, `test_typing_indicator.py`, `_make_conversation`, `test_hub_release.py`, `test_hub_calendar_money.py`, `test_booking_notifications.py`, `test_hub_config.py`, `test_booking_gate_deposit_fallback.py`, `Patient`, `agenda`, `outbound_messages`, `test_analytics_bi_plugin.py`, `reminders.py`, `test_attendee_booking.py`, `test_message_delivery_status.py`, `PixDeposit`, `test_professional_config_gap_alert.py`, `tools.py`, `calendar.py`, `setup_world`, `reminder_hooks.py`, `resolve_professional_calendar`, `test_clinic_facts_hub.py`, `_seed`, `Appointment`, `test_internal_asaas_connection.py`, `test_internal_privacy.py`, `BookingHold`, `FailureReason`, `test_precheck_handoff_plugin.py`, `test_audio_transcription.py`, `test_interactive_bubble_record.py`, `test_agent_menu_tools.py`, `HandoverState`, `route`, `load_tenant_config`, `test_ai_tools_cancel_money.py`, `db`, `test_precheck_handoff_tool.py`, `PaymentProvider`, `test_patient_context.py`, `logging.py`, `professional_notification.py`, `test_confirm_recheck_slot.py`, `professionals.py`, `test_pii_pseudonymization.py`, `test_asaas_account_status.py`, `test_professional_edit_notification.py`, `Conversation`, `test_multi_unit_plugin.py`, `test_hub_conversations_my_patients.py`, `test_portal_booking_owner_calendar.py`, `test_hub_config_extra_reminder.py`, `insurance_catalog.py`, `test_convenio_catalogo_db.py`, `_reminders_v2.py`, `test_booking_owner_persistence.py`, `test_hub_calendar_health.py`, `format_brl`, `test_inbound_tap_display_vs_routing.py`, `test_hub_configuration.py`, `test_deposit_release_preview.py`, `_connect_calendar`, `test_brain_message_attachments.py`, `is_live_status`, `test_service_guides_multipro.py`, `test_simulation_api.py`, `test_debug_reset_menu3.py`, `service_change.py`, `Service`, `hub_configuration.py`, `test_ai_v2_blindness.py`, `test_hub_deps.py`, `test_service_change_agenda.py`, `test_pix_deposit_plugin.py`, `load_simulation_inputs`, `test_asaas_connection.py`, `test_reminder_opening_decision.py`, `add_warnable_row`, `insurance.py`, `test_booking_code_gate.py`, `test_deposit_failure_notice.py`, `test_hub_edit_appointment.py`, `config.py`, `test_get_availability_tool.py`, `test_menu_command.py`, `test_probe_charge.py`, `test_bot_allowlist.py`, `test_handover_echoes.py`, `payments_cron.py`, `make_event`, `oauth.py`, `as_utc`, `ProviderError`, `test_booking_draft_continuation.py`, `test_booking_draft_resume_questions.py`, `test_appointment_change_notifications.py`, `test_payment_cron_jobs.py`, `datetime`, `reminder_engine.py`, `db`, `resolve_opening_message`, `test_service_change_cancel.py`, `test_service_change_webhook.py`, `_seed_tenant`, `test_manage_v2_handback.py`, `_Pool`, `decide_reminder_opening`, `FastAPI`, `test_hub_config_paid_notices.py`, `test_resolve_tenant.py`, `test_hub_staff_message.py`, `Exception`, `test_deposit_after_mode.py`, `make_sessionmaker`, `test_release_carry_check.py`, `PaymentStatus`, `DepositReleaseOffer`, `_apply_deposit_awareness`, `_audit`, `WhatsAppClient`, `ContextSource`, `_log`, `inbound_routing_text`, `test_brain_message_recent_page.py`, `test_calendar_events_insurance.py`, `TextBubble`, `test_payment_settings.py`, `render_greeting`, `test_lgpd_consent_gate.py`, `test_appointment_edit_paid_service.py`, `db`, `test_brain_message_open.py`, `test_context_sources_api.py`, `payment_accounts.py`, `test_payment_settings_roles.py`, `delete_tenant`, `AppointmentReminder`, `test_deposit_gate.py`, `test_deposit_cancel_no_refund.py`, `_report`, `PaymentAttentionItem`, `make_released`, `test_payment_account_charge_health.py`, `test_payment_migration.py`, `db`, `test_list_patient_appointments_tool.py`, `reload_appointment`, `SubscriptionClaim`, `internal_provisioning.py`, `test_payment_notify.py`, `_seed`, `release_offers.py`, `deposit_wait.py`, `sent`, `DepositQuote`, `_card_state`, `test_hub_conversations.py`, `test_express_confirmation_worker.py`, `asaas_connection.py`, `test_human_backup_plugin.py`, `_Model`, `Professional`, `analytics_advanced`, `adjustment_queries.py`, `make_world`, `deliver`, `BookingDraftRequested`, `test_reminder_v2_hub_wiring.py`, `evaluate_deposit_readiness`, `test_payment_tasks.py`, `test_hub_single_doctor_setup.py`, `test_deposit_off_without_account.py`, `test_hub_config_pix.py`, `get_session`, `test_handback_events.py`, `test_deposit_adjustment_model.py`, `test_payment_attention.py`, `staff_appointment_edit.py`, `get_conversation`, `TenantWhatsAppCredentialMissing`, `test_patient_insurance_overview.py`, `test_prompts_task072.py`, `test_patient_channel_identity.py`, `test_post_booking_plugin.py`, `settings.py`, `test_payment_models.py`, `resolve_attention_item`, `test_professional_null_vs_empty.py`, `test_reminder_v2_text.py`, `accept_keep`, `test_payment_accounts.py`, `quote_deposit`, `FakeRedis`, `ingest_context_source`, `test_portal_context_opening.py`, `get_payment_settings`, `appointment_change_notifications.py`, `NormalizedPaymentEvent`, `handle_release_offer_tap`, `test_appointment_replacement_service.py`, `test_cancel_event_ownership.py`, `test_staff_notice_rows.py`, `_alert_one`, `ask_after_booking`, `test_multi_professional_plugin.py`, `_ctx`, `main`, `patient_insurance_overview`, `Enum`, `test_main_menu_manage_entry.py`, `test_brain_message_pipeline.py`, `test_calendar_sync.py`, `test_r7_calendar_origin.py`, `simulation.py`, `_open_paid_offer`, `_notify_portal`, `send_payment_alert`, `seed_paid_deposit`, `confirmation_warnings.py`, `test_context_source_model.py`, `get_context_completeness`, `test_the_right_code_activates_the_account_and_ends_the_wait`, `_card_notice`, `load_reminder_content`, `message_status.py`, `test_release_keep.py`, `_flow_handback_tools`, `apply_appointment_edit`, `test_bot_reply_gating.py`?**
  _High betweenness centrality (0.236) - this node is a cross-community bridge._
- **Why does `Base` connect `Patient` to `test_reminders_plugin.py`, `test_hub_professionals.py`, `test_action_buttons.py`, `test_professional_notification.py`, `BaseModel`, `test_webhook_history_sync.py`, `test_reactivation.py`, `test_remove_context_command.py`, `tenant_config.py`, `test_deposit_lifecycle.py`, `BrainMessageSender`, `test_internal_provisioning.py`, `test_typing_indicator.py`, `test_hub_calendar_money.py`, `test_message_delivery_status.py`, `test_attendee_booking.py`, `PixDeposit`, `test_professional_config_gap_alert.py`, `resolve_professional_calendar`, `Appointment`, `_WireClient`, `BookingHold`, `test_precheck_handoff_plugin.py`, `test_audio_transcription.py`, `test_interactive_bubble_record.py`, `HandoverState`, `test_ai_tools_cancel_money.py`, `Tenant`, `test_patient_context.py`, `test_confirm_recheck_slot.py`, `test_pii_pseudonymization.py`, `test_professional_edit_notification.py`, `Conversation`, `test_multi_unit_plugin.py`, `LocalCalendarService`, `test_portal_booking_owner_calendar.py`, `insurance_catalog.py`, `test_convenio_catalogo_db.py`, `test_booking_owner_persistence.py`, `test_inbound_tap_display_vs_routing.py`, `test_brain_message_attachments.py`, `test_debug_reset_menu3.py`, `service_change.py`, `Service`, `test_booking_code_gate.py`, `config.py`, `test_get_availability_tool.py`, `test_menu_command.py`, `make_event`, `test_booking_draft_continuation.py`, `test_booking_draft_resume_questions.py`, `datetime`, `_FakeWhatsAppClient`, `_WhatsApp`, `TenantRuntimeConfig`, `_FakeWhatsApp`, `make_sessionmaker`, `_FakeArqPool`, `DepositReleaseOffer`, `ContextSource`, `_log`, `test_calendar_events_insurance.py`, `TextBubble`, `Settings`, `test_appointment_edit_paid_service.py`, `AppointmentDepositView`, `test_brain_message_open.py`, `_ExplodingWhatsAppClient`, `FakeAsaasClient`, `AppointmentReminder`, `PaymentAttentionItem`, `FakeProvider`, `test_human_backup_plugin.py`, `test_hub_conversations.py`, `test_express_confirmation_worker.py`, `_Model`, `Professional`, `BookingDraftRequested`, `context_extraction.py`, `test_hub_single_doctor_setup.py`, `_FakeAsaas`, `test_handback_events.py`, `_FakeCalendarService`, `get_conversation`, `_Agenda`, `HandoffOutcome`, `ClinicPaymentAccount`, `FakeRedis`, `test_cancel_event_ownership.py`, `ChannelSender`, `_FakeWhatsAppClient`, `test_brain_message_pipeline.py`, `_StubCalendarService`, `_FakeWhatsApp`, `FakeAsaasClient`, `_FakeCalendar`, `_FakeRedis`, `_FakeArqPool`, `pending_identity.py`?**
  _High betweenness centrality (0.050) - this node is a cross-community bridge._
- **Why does `Appointment` connect `Appointment` to `test_onboarding_cron.py`, `test_reminders_plugin.py`, `test_action_buttons.py`, `test_professional_notification.py`, `test_hub_calendar_owner_agenda.py`, `test_reactivation.py`, `seed_world`, `test_remove_context_command.py`, `test_deposit_lifecycle.py`, `test_hub_calendar_confirmation.py`, `test_internal.py`, `test_hub_release.py`, `test_hub_calendar_money.py`, `test_booking_gate_deposit_fallback.py`, `Patient`, `outbound_messages`, `agenda`, `test_analytics_bi_plugin.py`, `reminders.py`, `test_attendee_booking.py`, `PixDeposit`, `tools.py`, `calendar.py`, `setup_world`, `reminder_hooks.py`, `resolve_professional_calendar`, `test_internal_privacy.py`, `test_precheck_handoff_plugin.py`, `test_agent_menu_tools.py`, `test_release_offer.py`, `db`, `test_ai_tools_cancel_money.py`, `Tenant`, `test_patient_context.py`, `test_confirm_recheck_slot.py`, `professional_notification.py`, `test_pii_pseudonymization.py`, `test_professional_edit_notification.py`, `test_multi_unit_plugin.py`, `test_hub_conversations_my_patients.py`, `test_portal_booking_owner_calendar.py`, `test_hub_config_extra_reminder.py`, `_reminders_v2.py`, `test_convenio_catalogo_db.py`, `test_payment_attention_hub.py`, `test_booking_owner_persistence.py`, `test_before_bypass_paths.py`, `test_deposit_release_preview.py`, `_connect_calendar`, `is_live_status`, `internal.py`, `FakeCalendar`, `test_debug_reset_menu3.py`, `service_change.py`, `test_ai_v2_blindness.py`, `test_service_change_agenda.py`, `test_pix_deposit_plugin.py`, `test_reminder_opening_decision.py`, `add_warnable_row`, `test_hub_edit_appointment.py`, `test_booking_code_gate.py`, `config.py`, `test_menu_command.py`, `test_deposit_breaker.py`, `make_event`, `as_utc`, `test_booking_draft_resume_questions.py`, `test_appointment_change_notifications.py`, `test_payment_cron_jobs.py`, `test_appointment_edit_write.py`, `reminder_engine.py`, `db`, `resolve_opening_message`, `test_service_change_cancel.py`, `test_service_change_webhook.py`, `_seed_tenant`, `test_manage_v2_handback.py`, `_Pool`, `decide_reminder_opening`, `test_deposit_wait_turns.py`, `test_release_offer_buttons.py`, `test_hub_staff_message.py`, `Exception`, `test_deposit_after_mode.py`, `make_sessionmaker`, `test_release_carry_check.py`, `PaymentStatus`, `DepositReleaseOffer`, `_apply_deposit_awareness`, `inbound_routing_text`, `WhatsAppClient`, `_log`, `test_calendar_events_insurance.py`, `TextBubble`, `db`, `delete_tenant`, `AppointmentReminder`, `PaymentAttentionItem`, `test_deposit_cancel_no_refund.py`, `_report`, `make_released`, `test_payment_migration.py`, `db`, `test_list_patient_appointments_tool.py`, `reload_appointment`, `FakeProvider`, `test_payment_notify.py`, `test_release_keep.py`, `release_offers.py`, `sent`, `_card_state`, `test_express_confirmation_worker.py`, `test_reminder_v2_hub_wiring.py`, `AgendaViewer`, `Professional`, `test_before_booking_flow.py`, `test_payment_tasks.py`, `context`, `test_deposit_off_without_account.py`, `test_handback_events.py`, `test_release_offer_carry_over.py`, `staff_appointment_edit.py`, `get_conversation`, `test_before_interplay.py`, `test_post_booking_plugin.py`, `test_reminder_v2_text.py`, `test_professional_null_vs_empty.py`, `accept_keep`, `FakeRedis`, `test_portal_context_opening.py`, `NormalizedPaymentEvent`, `appointment_change_notifications.py`, `handle_release_offer_tap`, `test_appointment_replacement_service.py`, `test_cancel_event_ownership.py`, `test_staff_notice_rows.py`, `ask_after_booking`, `test_multi_professional_plugin.py`, `_ctx`, `test_main_menu_manage_entry.py`, `test_service_change_postgres.py`, `test_calendar_sync.py`, `test_r7_calendar_origin.py`, `_open_paid_offer`, `_notify_portal`, `load_pseudonymizer`, `test_before_fulfillment.py`, `seed_paid_deposit`, `confirmation_warnings.py`, `_card_notice`, `load_reminder_content`, `_seed`, `apply_appointment_edit`, `test_bot_reply_gating.py`?**
  _High betweenness centrality (0.044) - this node is a cross-community bridge._
- **Are the 411 inferred relationships involving `Tenant` (e.g. with `main()` and `main()`) actually correct?**
  _`Tenant` has 411 INFERRED edges - model-reasoned connections that need verification._
- **Are the 381 inferred relationships involving `Appointment` (e.g. with `_report()` and `_mark_appointment_cancelled()`) actually correct?**
  _`Appointment` has 381 INFERRED edges - model-reasoned connections that need verification._
- **Are the 276 inferred relationships involving `Conversation` (e.g. with `_own_upcoming()` and `_conversation_attendee_name()`) actually correct?**
  _`Conversation` has 276 INFERRED edges - model-reasoned connections that need verification._
- **Are the 264 inferred relationships involving `Patient` (e.g. with `main()` and `_persist_appointment()`) actually correct?**
  _`Patient` has 264 INFERRED edges - model-reasoned connections that need verification._