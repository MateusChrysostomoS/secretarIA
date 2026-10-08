# TASK-032 R6 — lembrete com Alterar Dados

## Estado

Seguimento de 2026-10-08: primeira mensagem contextual e primeiro contato WhatsApp elegível
unificados com o cartão/ações do lembrete. Validação atual: 4427 passed, 49 skipped, 17 warnings;
ver `docs/CHECKPOINT_primeira_mensagem_lembrete.md`. Sem nova migration/deploy.

Complemento autorizado depois desta entrega: e-mail fixo ao médico ao confirmar alteração,
sem LLM, com mudanças e dados completos. Validação conjunta atual: 4410 passed, 49 skipped,
17 warnings; ver `docs/CHECKPOINT_email_alteracao_consulta.md`.

**Integração autorizada posteriormente pelo dono:** entrega funcional commitada em `6de2878`,
main `8b4a5ee` incorporada em `a132f3a`, sem conflitos. Candidata validada com **4411 passed,
49 skipped, 17 warnings** em 227,84 s. Commit/merge/push autorizados; deploy e SQL remoto não.
Os dois planos futuros R4/R5 em edição no checkout original ficaram fora do commit e
permaneceram no lugar. Índice Graphify da candidata reconstruído e diagnosticado.

Implementação original no worktree `C:\TECH\BRAIN-worktrees\TASK-032\secretarIA-r6`, branch `task/TASK-032-r6-alterar-dados`, base `9ef32396caeea1611c7fe2b74ca37017635cee95`. Naquela rodada os passos de commit/publicação ficaram pendentes; o pedido posterior autorizou a integração descrita acima. A versão de produção não foi alterada pelo agente.

Migração aditiva `b1c4e7a2d9f3`, acima de `a7e2c9d4f1b6`: uma coluna JSON nullable em `conversations`. Head único, metadados e upgrade/downgrade SQLite comprovados; nenhuma migração anterior reescrita. **Postgres run NOT done:** `docker info` encontrou o pipe do Docker Desktop Linux engine ausente. Não foi usado banco externo.

Validação final: **4388 passed, 49 skipped, 17 warnings**, em 292,86 s (4 min 52 s), nenhuma falha. Ruff nos 49 arquivos Python afetados limpo, `git diff --check` limpo. A primeira suíte ampla encontrou uma regressão nova no contexto da IA (`test_every_workflow_step_has_a_label`); as 15 etapas novas foram descritas e o comportamento protegido sem inserir nomes de pacientes ou valores de convênio no system prompt. Avisos existentes de depreciação FastAPI/Alembic permanecem.

Revisão independente antes das correções: `C:\TECH\BRAIN\tasks\TASK-032\results\r6-review.md`. Nove achados Important corrigidos em um passe, com testes RED→GREEN; o relatório original foi preservado. Evidências completas e decisões: diretório `.superpowers/sdd/2026-10-07-lembretes-alterar-dados/` no worktree. Mantido porque não há commits que preservem esse histórico.

Graphify atualizado via AST, sem LLM; consultas encontram os módulos de edição e aplicação, diagnóstico sem duplicatas nem endpoints soltos. Proveniência da candidata registrada em `a132f3a`. O commit posterior do índice muda HEAD sem mudar código; conferir freshness antes de confiar no grafo.

## O que entrou onde

- `services/reminder_text.py` e `schemas/webhook.py`: Confirmar / Cancelar / Alterar Dados e `remedit`; ids antigos continuam decodificando.
- `workers/shared/reminder_actions.py`: confirmação seguida do menu recorrente; Cancelar pede confirmação diretamente e a continuação cancela e pergunta o motivo. Com o interruptor OFF os cartões de lembrete antigos não abrem a edição.
- `services/appointment_edit.py`, `appointment_edit_flow.py`: menu de alteração, data próxima da consulta, horário, serviços do médico atual, médicos com disponibilidade, convênio e paciente. Uma confirmação completa fecha cada escolha; Alterar Mais Dados mantém o mesmo rascunho.
- `models/conversation.py`, `workers/shared/state_expiry.py`, `turn_router.py`, `orchestrator.py`, `flow_runner.py`, `ai/tools.py`: `EDIT_BOOKING`, coluna `flow_edit_draft`, persistência, snapshots e limpeza/expiração universal. A consulta não muda enquanto há somente um rascunho.
- `services/calendar.py`: alteração de título/descrição/janela, disponibilidade e identidade física da agenda (o alias primary não confunde contas distintas). Mesmo médico ou agenda física compartilhada preserva o evento; agenda diferente cria primeiro.
- `workers/shared/appointment_edit_support.py` e `appointment_edit_apply.py`: contexto de agendas/Pix, aplicação na mesma linha com escopo clínica/paciente, status futuro, comparação de origem e locks; limites revalidados; limpeza da agenda anterior após commit.
- `workers/shared/bubbles.py`, `dispatch.py`, `services/channel_sender.py`, `whatsapp.py`: cinco opções viram botões reais no Portal e lista no WhatsApp, com registro compatível com o que foi enviado.
- `services/patient_context.py`, `services/llm_context.py` e `pii_pseudonymization.py`: leitura de convênio/atendido, contexto seguro da etapa de edição e mascaramento persistente dos nomes copiados da consulta, inclusive depois de descartar o rascunho.

## Regras fixadas e testes

| Regra | Prova local |
|---|---|
| Rascunho e Cancelar final mantêm a consulta | `test_the_draft_never_touches_the_appointment_before_confirm`, `test_cancel_on_the_final_card_keeps_the_appointment` |
| Falha de agenda mantém o rascunho | `test_a_calendar_failure_changes_nothing_and_keeps_the_draft` |
| Falha no banco não anuncia sucesso e desfaz o efeito na agenda | `test_database_failure_compensates_calendar_and_keeps_the_draft`, parametrizado patch/criação |
| Toque de outro paciente/versão não abre nem cancela | `test_new_ids_are_checked_against_the_patient_and_the_version`, `test_a_foreign_remedit_opens_nothing` |
| Revalidação final clínica/paciente/futuro | `test_worker_refuses_another_patient_in_the_same_tenant`, `test_worker_refuses_past_and_cancelled_appointments_without_success` |
| Alteração da clínica não é sobrescrita por rascunho antigo | `test_a_concurrently_changed_appointment_is_not_overwritten`, `test_a_clinic_change_after_calendar_write_wins_and_is_restored` |
| Pix esconde opções e revalida cartões estacionados | `test_a_paid_deposit_hides_service_doctor_and_insurance`, `test_new_pix_restrictions_block_confirmation_of_a_parked_draft`, `test_exhausted_pix_limit_cannot_commit_a_time_change` |
| Médico ocupado continua para dia/horário | `test_the_doctor_list_marks_free_and_busy`, `test_a_busy_doctor_continues_to_day_and_time` |
| Serviço mais longo é reavaliado; serviço incompatível não confirma | `test_a_longer_service_that_does_not_fit_asks_for_a_new_time`, `test_an_unsupported_service_cannot_be_confirmed_after_switching_doctor` |
| Troca de agenda cria antes e apaga depois do commit | `test_a_doctor_change_creates_the_new_event_then_deletes_the_old_one_after_commit` |
| Dois médicos na mesma agenda preservam o mesmo evento | `test_changing_doctor_on_a_shared_calendar_keeps_the_same_event` |
| Reservas adquiridas depois da lista bloqueiam Confirmar | `test_a_hold_acquired_after_the_picker_blocks_confirm` |
| Cinco opções são lista no WhatsApp e botões no Portal | `test_five_labels_become_a_list_on_a_three_button_channel`, `test_send_buttons_records_all_five_portal_options` |
| Cartões antigos continuam atendidos no caminho novo | `test_legacy_ids_still_resolve` e sete cenários R3 substituídos nos próprios arquivos |
| OFF impede entrada/aplicação do caminho novo | `test_switch_off_cannot_open_an_edit_from_a_legacy_reminder`, `test_switch_off_discards_an_edit_without_writing_calendar` |
| Paginação preserva a âncora da consulta | `test_next_day_page_keeps_the_appointment_anchor` |
| Nome copiado não chega à IA antes/depois do descarte | `test_an_existing_attendee_is_masked_during_edit_and_after_discard` |
| Pergunta livre mantém a etapa, o rascunho e contexto anônimo | `test_free_text_hands_the_turn_to_the_ai_and_keeps_the_draft`, `test_edit_detour_explains_the_parked_confirmation_without_identifiers` |

## Decisões e adaptações

D1–D9 implementadas: o terceiro botão é Alterar Dados; Cancelar direto com certeza/motivo; Confirmar traz menu recorrente; menu de cinco opções e segundo menu; listas/botões por canal; confirmação completa com três escolhas; data próxima da consulta e pergunta do horário; médico/convênio marcados; explicação no menu de mudança.

P1–P5 mantidas: médico ocupado continua a dia/horário; troca de agenda cria antes de apagar; mudança de dia/horário replaneja lembretes/zera confirmações, outras mudanças mantêm o contador; Pix limita dados que mudam valor e remarcações; texto livre usa a IA na mesma etapa.

P6: o rótulo fora da janela depende da versão aprovada na Meta. P7: a explicação aparece no menu de mudança, sem ampliar o texto padrão do lembrete; nenhuma preferência adicional foi presumida.

O rascunho usa a forma interna `appointment_id/current/original/stage` do plano, em vez do exemplo flat da spec, para comparar alterações e origem. Não muda contrato HTTP externo.

A revalidação e a compensação foram reforçadas além do snippet do plano: recusa de Pix ou conflito não é somente registrada em log seguida de commit. A transação recusa a edição, preserva o rascunho e tenta devolver o Google ao estado efetivamente commitado. Em troca de agenda, remove apenas o evento novo que não foi commitado. Não envia sucesso se a aplicação falhar. **Compensação remota é best-effort:** uma segunda falha do Google ao desfazer exige conciliação humana; não existe transação distribuída entre Postgres e Google Calendar. O evento anterior nunca é apagado nessa falha.

Datas persistidas em UTC; o Google e os textos usam o fuso da clínica. As reservas temporárias usam o mesmo gate do agendamento. Corrigidos fixtures que criavam UUIDs independentes para rascunho/consulta e a expectativa literal da segunda página; cobertura comportamental preservada.

Os sete casos R3 retirados provisoriamente pela frente de implementação foram substituídos nos arquivos originais por casos da jornada nova (Cancelar direto, remarcação legada/Pix, Agendar Outra legado/Pix), cumprindo a restrição global do plano. Testes dos cartões/menus antigos agora verificam a mudança desejada; não houve remoção para esconder falhas.

## Pendências antes da liberação

- Conferir na tela do Portal se cinco botões ficam adequados; o transporte/registro foi provado, a renderização real não.
- Validar migration e locks em PostgreSQL descartável; Docker indisponível nesta execução.
- Reenviar/aprovar o modelo Meta para trocar o rótulo fora da janela de 24 h; essa operação externa não foi executada.
- Verificar jornadas reais Google/WhatsApp/Portal e paridade API/worker depois da publicação autorizada. Nenhum dado real foi alterado nesta execução.
- A coluna `flow_replaces_appointment_id` e consumidores antigos permanecem para limpeza futura.
- Ao editar, o Google perde a linha de atribuição “Agendado pelo”; dados da consulta permanecem.
- A lista de médicos começa com a duração atual; seleção e confirmação conferem a duração do serviço do médico escolhido.
- O `ConsentEvent` de autorização de terceiro é gravado na confirmação da frase, mesmo se o rascunho for descartado depois.

Minor adiados pela revisão: nome proposto duplicado em `stage`; política de overflow se as informações essenciais de confirmação ultrapassarem 1024; normalização de timezone em `is_slot_free` para futuros consumidores que passem datetime aware fora do fuso da clínica (o caminho R6 passa o fuso correto).

Ordem de liberação: **banco primeiro**, depois **API e worker juntos**, com prova de paridade. A migração é inerte para código antigo. Rollback: código antigo nos dois serviços primeiro; só depois remover a coluna, aceitando perder unicamente rascunhos de alteração em andamento. **Deployment R6: NOT AUTHORIZED nesta sessão.**


## Registro completo das decisões durante a execução

Verificação adicional após a suíte ampla, sem mudar produção: os 18 testes de segurança passaram
em 2,64 s, incluindo a correção da clínica entre a chamada Google e o commit. Graphify final:
12409 nós / 30796 arestas, sem duplicatas/endpoints soltos; um self-loop incidental.

Decisão de encerramento: preservar branch, worktree e evidências locais. As instruções explícitas
do dono proíbem commit/publicação sem novo pedido, por isso não foi aberta uma confirmação para
integrar. Custo se errado: o dono deve autorizar explicitamente a etapa de integração/publicação.

As decisões abaixo conservam os registros de cada executor. A restauração dos sete cenários R3 descrita acima substitui as decisões provisórias de removê-los. Custos condicionais não afirmam falhas observadas.

### worker-progress-draft.md

- Task 1: Ruling: relax prior migration head assertion to one head plus membership in revision chain, as plan explicitly directs - R6 legitimately supersedes R3 - cost if wrong: acceptance of a later linear migration instead of pinning R3 as latest.
- Task 3: Ruling: omit absent test_interactive_bubbles.py as plan permits; scoped command has test_workers_layering.py + test_brain_message_interactive_tap.py - cost if wrong: missed coverage from a test file not present in baseline.
- Task 3: Ruling: replace old Portal truncation assertion with five stored ids/titles and unchanged history/direction checks - spec D5 explicitly requires five Portal buttons; literal assertions avoid reusing production builder - cost if wrong: Portal could show additional buttons despite legacy cap expectation (desired product change).
- Execution ruling: skip commit/stage and shared task-done ledger writes, preserving individual worker ledger per parent and user constraints; cost if wrong: local changes require later integration and scratch evidence retention.

### worker-progress-reminders.md

- Task 4-5: Ruling: no commits/staging and own ledger/logs only - explicit user/root policy overrides plan commit and shared ledger steps - cost if wrong: root must consolidate local diff.
- Task 4: Ruling: tests/test_reminder_v2_text.py: ["Confirmar", "Cancelar", "Outro"] - ["Confirmar", "Cancelar", "Alterar Dados"] - R6 changes the third button - cost if wrong: one card/payload assertion reflects the wrong contract.
- Task 4: Ruling: tests/test_reminder_r3_ids.py: rt.REMINDER_ACTIONS == ("remconfirm", "remcancel", "remother") - rt.REMINDER_ACTIONS == ("remconfirm", "remcancel", "remedit") - R6 changes the third button - cost if wrong: one card/payload assertion reflects the wrong contract.
- Task 4: Ruling: tests/test_reminder_r3_ids.py: rt.REMINDER_ROW_ACTIONS == rt.REMINDER_ACTIONS + rt.CANCEL_PATH_ACTIONS - rt.REMINDER_ROW_ACTIONS == rt.REMINDER_ACTIONS + rt.LEGACY_REMINDER_ACTIONS + rt.CANCEL_PATH_ACTIONS - R6 changes the third button - cost if wrong: one card/payload assertion reflects the wrong contract.
- Task 4: Ruling: tests/test_reminder_v2_engine.py: (opções: Confirmar, Cancelar, Outro) - (opções: Confirmar, Cancelar, Alterar Dados) - R6 changes the third button - cost if wrong: one card/payload assertion reflects the wrong contract.
- Task 4: Ruling: tests/test_reminder_v2_delivery.py: remother| - remedit| - R6 changes the third button - cost if wrong: one card/payload assertion reflects the wrong contract.
- Task 4: Ruling: tests/test_reminder_v2_delivery.py: "Outro" - "Alterar Dados" - R6 changes the third button - cost if wrong: one card/payload assertion reflects the wrong contract.
- Task 4: Ruling: tests/test_reminder_v2_delivery.py: (opções: Confirmar, Cancelar, Outro) - (opções: Confirmar, Cancelar, Alterar Dados) - R6 changes the third button - cost if wrong: one card/payload assertion reflects the wrong contract.
- Task 5: Ruling: test_the_cancel_card_is_the_three_way_choice removed - R6 replaces the three-way/booking path; Task7-9 cover edits - cost if wrong: old behavior loses coverage pending edit-flow tests
- Task 5: Ruling: test_every_new_label_fits_a_whatsapp_button keeps cancellation labels + LABEL_EDIT - removed labels no longer emitted - cost if wrong: missing coverage for an unexpected surviving label
- Task 5: Ruling: test_cancel_offers_the_three_way_card removed - R6 replaces the three-way/booking path; Task7-9 cover edits - cost if wrong: old behavior loses coverage pending edit-flow tests
- Task 5: Ruling: test_the_three_way_card_names_each_button_in_its_body removed - R6 replaces the three-way/booking path; Task7-9 cover edits - cost if wrong: old behavior loses coverage pending edit-flow tests
- Task 5: Ruling: test_remarcar_consulta_enters_the_reschedule_of_this_appointment removed - R6 replaces the three-way/booking path; Task7-9 cover edits - cost if wrong: old behavior loses coverage pending edit-flow tests
- Task 5: Ruling: test_remarcar_consulta_respects_the_pix_reschedule_limit removed - R6 replaces the three-way/booking path; Task7-9 cover edits - cost if wrong: old behavior loses coverage pending edit-flow tests
- Task 5: Ruling: cancel path ownership/version loop covers remgiveup/remgiveupyes/remkeep; remresched/remnew map to edits and R6 tests cover those - cost if wrong: missing legacy edit scope case
- Task 5: Ruling: test_book_another_opens_a_booking_that_replaces_this_one removed - R6 replaces the three-way/booking path; Task7-9 cover edits - cost if wrong: old behavior loses coverage pending edit-flow tests
- Task 5: Ruling: test_book_another_inside_the_refund_window_warns_about_the_deposit removed - R6 replaces the three-way/booking path; Task7-9 cover edits - cost if wrong: old behavior loses coverage pending edit-flow tests
- Task 5: Ruling: v2 cancel card expects direct confirmation question + two cancellation labels - D2 - cost if wrong: current cancel journey asserted incorrectly
- Task 5: Ruling: v2 Portal confirmation adds recurring menu after confirmation - D3 - cost if wrong: recurring menu would be hidden by legacy expectation
- Task 5: Ruling: Portal remcancel goes directly to remkeep and confirmation appends recurring menu - D2/D3 - cost if wrong: Portal journey would retain obsolete intermediate card
- Task 5: Ruling: Portal body expectations include persisted menu option footer - real BrainMessageClient records option titles in body - cost if wrong: tests could miss an incorrectly recorded menu footer.
- Task 5: Ruling: test_reminder_opening_turn.py::test_two_appointments_open_with_the_nearest_and_the_answer_names_the_other examines the confirmation before the appended menu - D3 - cost if wrong: confirmation position assertion would miss a misplaced message.

### progress.md

- Ruling: Keep all changes local and uncommitted; user AGENTS.md prohibits commits without explicit request. Retain scratch ledger because Git history will not contain the work. Cost: manual commit/integration remains.
- Ruling: Use the plan's nested current/original/stage draft rather than the spec's illustrative flat JSON, to compare original values and detect stale edits; internal nullable column has no external contract. Cost: flattening later would need an internal conversion.
- Task 7: Ruling: reuse the supplied draft's original appointment in test helpers, instead of generating an unrelated UUID; otherwise normal continuation is tested as a foreign/stale edit. Cost: fixtures need maintaining if draft representation changes.
- Task 8: Ruling: apply Task7's stable original-appointment fixture also to fields tests, including the no-services scenario. Cost: test fixtures follow draft shape.
- Task 9: Ruling: normalize persisted edit start/end to UTC before SQLite strips timezone, leaving Google clinic-local — whole real-turn test RED16:00 versus expected19:00 — cost if wrong: timestamp conversion would move the intended appointment.
- Task 9: Ruling: apply stable-original fixtures to Confirmar helper and preserve seeded insurance in worker edit fixture so Pix time-only tests do not silently edit price-related fields — cost if wrong: fixture may no longer represent an intended multi-field edit.
- Task 9: Ruling: patch test-only hold lookup in real-turn fixture to empty holds; live-hold correctness has separate synthetic gate tests — cost if wrong: real-turn test would miss a hold integration issue.
- Task 9: Ruling: update interim and legacy remother tests from full LLM mode to edit menu / same-step free-text delegation; legacy IDs remain accepted — cost if wrong: legacy card behavior expectation diverges.
- Final: Ruling: safety pagination's hand-derived second page uses day19, not11 (11 is already on first page); correct literal without deriving it through production — cost if wrong: test pins wrong page order.
- Final: Ruling: strengthen plan's worker contract with patient scope, original snapshot, row locks, refusal and Calendar compensation instead of the plan's 'log and commit Pix race' — authoritative spec requires unchanged original on failure — cost if wrong: a competing change is refused and the patient retries.
- Final: Ruling: distinguish professional change from physical calendar change — supported shared-agenda configuration would otherwise invent conflicts or delete its own event — cost if wrong: an unknown calendar identity conservatively retains create-before-delete.
- Final: Ruling: live Google/Meta/Asaas journey and parity are release checks, not authorized in local implementation — cost if wrong: local passing doubles may miss provider-specific behavior.
- Final: Ruling: actual five-button Portal visual smoke remains a pre-deploy check; local transport persists five options — cost if wrong: frontend may render them poorly.
- Final: Ruling: disposable Postgres smoke unavailable: docker info reports missing Docker Desktop Linux engine pipe; SQLite up/down and metadata head tests stand — cost if wrong: PostgreSQL-only lock/migration issue remains undetected.
- Final: Ruling: dormant replacement column/consumers left in place per spec — cost if wrong: legacy dead code persists until cleanup.
- Final: Ruling: Calendar's old 'Agendado pelo' description line is not restored during edit per plan — cost if wrong: clinic loses channel attribution in Google description, booking data remain accurate.
- Final: Ruling: doctor's list mark initially uses current appointment duration; selection and confirmation validate new service duration — cost if wrong: list may be conservatively marked until selection.
- Final: Ruling: Meta template submission/approval remains owner's external task; old positional label Outro keeps working — cost if wrong: outside-window label stays old until approved.
- Final: Ruling: the seven obsolete R3 cases are replaced in their original files, not discarded; updated Cancelar, legacy Remarcar/Pix and legacy Agendar Outra/Pix now assert the R6 effects. This supersedes worker removal rulings, preserving plan's global testing constraint. Cost if wrong: compatibility coverage represents the new rather than removed journey. Legacy replacement suite24passed,4.38s.
- Task 10: Ruling: extend services/llm_context.py and its behavioral test to describe parked edits without patient/insurance identifiers; the plan omitted this existing step contract. Cost if wrong: AI receives misleading edit context; no prompt/agent-tool redesign introduced.
