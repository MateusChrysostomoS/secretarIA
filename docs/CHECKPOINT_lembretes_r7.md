# CHECKPOINT — Lembretes R7: ações da clínica na agenda avisam o paciente (TASK-044)

Spec: `docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md`.
Plano: `docs/superpowers/plans/2026-10-09-lembretes-r7-avisos-acoes-clinica.md`.

## Estado

- Passe final I1–I3: código `fcf9c86146e42c2fd54febd5756e38a6f3548a16`; **4913 passed, 68 skipped, 28 warnings in 229.32s (0:03:49)**, lint limpo. Estado local; adjudicação de integração é do coordenador.

- Branch `task/TASK-044-lembretes-r7-avisos` (worktree `C:\TECH\BRAIN-worktrees\TASK-044\secretarIA`), commits locais: 

```text
4f12339 feat(reminders): configurable extra lead replanned on change (TASK-044)
c5f5b1e feat(hub): enforce own agenda on appointment actions and creation (TASK-044)
7de9af6 feat(hub): events scoped by viewer with own filter and edit prefill (TASK-044)
1cbf380 feat(hub): agenda viewer from brain-api scope and GET viewer (TASK-044)
43643a2 fix(notices): preserve long edit details across Portal and WhatsApp (TASK-044)
9c4b25e feat(hub): cancel tells Portal patients in chat (TASK-044)
f64f6e4 feat(hub): POST edit tells the patient what changed (TASK-044)
4766ded feat(hub): clinic edit service validates and moves Google fail-closed (TASK-044)
8eedc47 refactor(edit): one row writer shared by Alterar Dados and hub edit (TASK-044)
10a73ee feat(hub): Compareceu sends the post-consult message once (TASK-044)
8a9d951 feat(hub): staff confirmation tells the patient with reminder buttons (TASK-044)
2bec01b fix(hub): refuse closed outcome corrections per binding R7 spec (TASK-044)
adb40c1 feat(hub): PATCH status guards - no_show_before_start, no resurrection (TASK-044)
cce0f6f feat(reminders): staff_confirm/staff_edit rows - tap targets that never warn or plan (TASK-044)
f5e02c3 feat(hub): R4 release/message honour the clinic paid-notice approval (TASK-044)
c982fc8 feat(reminders): one channel-aware sender for clinic-action notices (TASK-044)
3495201 feat(hub): expose paid_notices_auto_approved on the clinic configuration (TASK-044)
5c9ad52 feat(reminders): R7 columns - paid notice auto-approval and post-consult marker (TASK-044)
```.
- Suíte completa: 4892 passed, 68 skipped, 28 warnings in 594.33s (0:09:54). Falhas pré-existentes (provadas em `origin/main`): nenhuma (baseline independente: 4669 passed, 68 skipped, 21 warnings em 618.18s sobre `2e6dd91`).
- **Não mesclado, não pushado, não deployado. Nenhum modelo da Meta criado ou submetido. Migração não aplicada em banco real.**

## O que entrou e onde (âncoras estáveis)

- Config da clínica: `Tenant.paid_notices_auto_approved` (migração `d8e3a5c1f7b2`), lido/gravado em `schemas/config.py::TenantConfigRead/TenantConfigUpdate` e `services/hub_configuration.py::TENANT_SCALAR_FIELDS` → `GET /tenants/me/config`, `PUT /tenants/me/configuration`, `PUT /tenants/me/config`.
- Envio único dos avisos de ação: `services/staff_patient_message.py::send_clinic_notice` (+ `NoticeResult`, `paid_notice_authorised`, vocabulário `NOTICE_*`, agora definido aqui e reexportado por `appointment_release`).
- Cartões da clínica: `models/appointment_reminder.py` (`staff_confirm`, `staff_edit`, `REMINDER_KINDS_UNPLANNED`), `services/reminder_schedule.py::ensure_staff_notice_row/current_staff_notice_row/retire_staff_notice_row`; reconcile (`services/reminder_hooks.py::reconcile_missing_reminders`) e aviso à clínica (`workers/confirmation_warnings.py::due_candidates`) os ignoram. O toque é o fluxo R6 sem mudança (`workers/shared/reminder_actions.py::handle_reminder_button`).
- Textos e regras dos avisos: `services/clinic_action_notice.py` (`notify_staff_confirmation`, `notify_attended`, `notify_staff_edit`, `buttons_allowed`).
- PATCH status: `services/appointment_status.py::staff_transition` + `api/hub/calendar.py::update_appointment_status`.
- Pós-consulta: `Appointment.post_consult_notified_at` (mesma migração); `services/patient_context.py::find_post_consult_followup` ignora consulta já avisada.
- Editar/Remarcar: `POST …/edit` → `services/staff_appointment_edit.py::apply_staff_edit` → `services/appointment_edit_write.py::write_appointment_edit` (o mesmo gravador do "Alterar Dados" R6) + outbox R6 (`record_professional_edit`, `enqueue_professional_edit_notification`) + `reminder_hooks.after_appointment_rescheduled`.
- Cancelar: `services/appointment_release.py::notify_cancelled_patient` (Portal no chat + e-mail; WhatsApp = o job de sempre).
- R4: liberar e mensagem usam `paid_notice_authorised`; liberar devolve `whatsapp_link`.
- Quem vê a agenda (spec §5.A): `core/subscription.py::SubscriptionClaim` (`professional_id`, `agenda_scope` da introspecção do brain-api), `services/agenda_visibility.py` (`AgendaViewer`, `viewer_from_claim` — regra de rollout que fecha por segurança), `api/hub/deps.py::get_agenda_viewer`, `GET /calendar/viewer`; `api/hub/calendar.py::list_events` (filtro + `mine`), `_get_appointment(..., viewer)` em todas as rotas por id; `_creation_professional` em `POST /appointments` e `POST /blocks` (médico restrito cria só na própria agenda: sem médico = ele, outro médico = 403 `professional_not_allowed`, sem médico próprio = 422 `no_own_agenda`; recepção pode nomear um médico ativo da clínica) e `professional_not_allowed` no Editar/Remarcar.
- Dados do Editar na agenda (spec §5.C): `schemas/calendar.py::CalendarEventRead` (`professional_id`, `professional_name`, `service`, `attendee_name`, `phone`, `patient_channel`).
- Lembrete extra (spec §5.B): `schemas/config.py` (`reminders_v2_enabled` só leitura, `reminder_extra_lead_minutes` 1500..20160), `services/hub_configuration.py::apply_tenant_config` → `services/reminder_schedule.py::replan_custom_reminders`.
  - **Substituído no R9 (2026-10-10):** o lembrete extra passou a ser "N dias antes, às HH:MM" — ver `docs/CHECKPOINT_lembretes_r9.md`.

## Contrato para o R5

Desvio obrigatório da tabela planejada: `attended` ↔ `no_show` é recusado com `409 not_live` por a consulta estar encerrada; repetir o MESMO resultado continua no-op.

O contrato exato (campos, códigos, status) está na seção "Produces for R5 — exact wire shapes" do plano; o R5 deve copiá-la, não reinterpretá-la.

## Decisões onde a spec silenciou

As 22 decisões estão na seção "Decisions taken where the spec is silent" do plano (configuração única, linhas `staff_*`, um aviso de confirmação por horário, botões só quando funcionam, tabela de transições ajustada à spec (encerrada não muda de resultado), marcador da pós-consulta, nome = `attendee_name`, telefone com DDI, encaixe com `allow_overlap`, e-mail ao médico só data/hora/médico, cancelamento WhatsApp idêntico ao de hoje, `/message` e `/reschedule` inalterados, envio síncrono na API; e, da segunda rodada: papel decidido ao vivo pelo brain-api, regra de rollout, eventos sem consulta e consultas sem médico ocultos ao médico restrito, médico restrito cria consulta/bloqueio só na própria agenda e não passa a consulta adiante (decisão do dono de 2026-10-09), "Só os meus" é filtro e não permissão, limites 1500..20160 do lembrete extra, reprogramação só dos extras pendentes já planejados, dados do Editar na mesma consulta).

## Modelos da Meta (documentados, NÃO criados)

Fora das 24 h, até a Meta aprovar modelos com botões, confirmar/alterar vão pelo modelo pago de uma variável `REMINDER_TEMPLATE_NAME`, sem botões. Os modelos propostos estão em `docs/LEMBRETES_MODELOS_META.md` (seção R7).

## Deploy (quando o dono autorizar)

1. `alembic upgrade head` (imagem nova; adiciona as três colunas, aditivo).
2. brain-api com `agenda_scope` (`brain-api/docs/superpowers/plans/2026-10-09-hub-token-papel-profissional.md`). Sem ele, a regra de rollout vale: quem está ligado a um médico vê só o próprio (gestor-médico sem seletor) — é o sintoma de ordem trocada.
3. `secretaria_api` **e** `secretaria-worker` juntos (`GET /build` → `deploy_parity=match`): o worker lê o reconcile novo, o filtro dos avisos à clínica, o follow-up da pós-consulta e o gravador do "Alterar Dados".
4. Front (R5 atualizado) depois da API.

## Pendências

- O plano R5 (`docs/superpowers/plans/2026-10-03-lembretes-r5-front-agenda.md`) já consome este contrato (atualizado em 2026-10-09); se algo do contrato mudou na execução, atualizar o R5 antes de executá-lo.
- Modelos da Meta da seção R7 (decisão e submissão do dono).
- Prova real em clínica de teste (Portal e WhatsApp, dentro e fora das 24 h) após deploy autorizado.
- **Próximo plano (não escrito, decidido pelo dono em 2026-10-09) — console de conversas** (spec §5.D): o médico vê só as conversas dos próprios pacientes; o gestor (também quando é médico) alterna "Todos" / "Só os meus"; a secretária vê todas. Hoje `/tenants/me/conversations` (e as listas `GET /doctor/appointments` / `GET /doctor/patients` do brain-api) mostram a clínica inteira a qualquer médico; o R7 não muda isso.
- O front (R5) precisa dos três estados de criação do médico restrito: médico travado nele, 403 `professional_not_allowed`, 422 `no_own_agenda` (contrato no plano).
- Consultas que vivem na agenda Google própria de um profissional continuam fora da agenda do hub (a agenda lê só a da clínica — comportamento de hoje, mantido por decisão do dono).


## Validação e desvios da execução

- Base real `2e6dd91dd7931ab5a0d45cda68ff94e8e008135d` (a referência `5e66c9d` do plano estava antiga). Toda execução ocorreu nesta worktree isolada; main e brain-api não foram editados.
- `uvx ruff check .`: limpo antes e depois. Formatação global já era divergente no baseline (158 arquivos); nenhuma formatação global feita.
- As etapas 1–16 tiveram RED→GREEN; comandos/resultados por etapa e todos os Rulings ficam em `.superpowers/sdd/2026-10-09-lembretes-r7-avisos-acoes-clinica/progress.md`, preservado para revisão independente.
- Três testes de correção de resultado permitido do plano foram substituídos conforme a spec: `test_closed_outcomes_cannot_correct_each_other_and_repeat_as_a_no_op`, `test_attended_cannot_be_corrected_to_no_show_after_the_start`, `test_marked_twice_or_refused_correction_it_is_sent_once`. Dois casos adicionais provam ausência de dinheiro/aviso na recusa.
- Duas injeções de falha do R6 mudaram apenas o alvo do monkeypatch para o gravador extraído; os cenários de rollback/compensação permanecem.
- Concorrência de campos no Editar: todos os dados editáveis são comparados sob lock; perder a corrida devolve `409 appointment_changed` e a compensação Google restaura os dados persistidos do vencedor. Quatro testes RED→GREEN cobrem convênio, atendido, telefone e serviço.
- Aviso longo: o corpo neutro mantém TODAS as mudanças e o rodapé atual. Portal recebe texto completo e cartão curto; WhatsApp gratuito recebe texto em partes de até 4096 e cartão curto de três botões, com os mesmos ids. Quatro testes de sender/corpo e integração Portal cobrem fidelidade e recusa do cartão.
- O modelo pago mantém o orçamento existente de 900 caracteres, somente no transporte WhatsApp. Dados persistidos, identidades e ids nunca são cortados. Mensagens pagas excepcionalmente longas podem perder o fim; modelo maior exige decisão/submissão da Meta.
- Se o texto completo saiu no WhatsApp e o cartão falha, `notice_failed` é devolvido; repetir pode repetir o texto de detalhes. Nenhuma aprovação da Meta foi feita.
- Introspecção positiva preserva cache pré-existente (padrão60s); mudança do papel pode demorar esse TTL. Autoridade é o brain-api, que lê o usuário atual. Revisão independente precisa pesar esse atraso contra a exigência de papel atual.
- Pré-requisito brain-api verificado somente por leitura em `7f3331d` (66 testes focados no ledger): contrato aditivo pronto, revisão/checkpoint finais ainda em outra sessão; integração e publicação não foram feitas.
- Migração aditiva provada por testes SQLite upgrade/downgrade/defaults; banco Postgres real não usado.

## Registro das decisões (ordem de execução)

- Ruling: Execute Tasks 1–12 while producer brain-api finishes; check readiness before Task 13 — authorized by coordinator; costs no integration until producer ready.
- Ruling: No Claude coauthor attribution — implementation by Codex, use no fake coauthor; cost: deviates from plan commit footer only.
- Ruling: Keep ledger/worktree for final independent root review — authorized handoff overrides skill deletion; cost: scratch artifacts retained locally.
- Ruling: Use baseline site-packages via absolute PYTHONPATH — worktree venv unavailable/App Control; cost: dependencies match local main environment, not fresh install.
- Task 3 RED: 15 failures for missing sender/vocabulary; GREEN verification logged. Ruling: generated notification bodies use existing R4 one-line template budget/truncate_plain as planned; frontend validation outside R7 scope, full before/after data remains stored — cost if wrong: exceptionally long paid notices may lose tail text.
- Task 6 RED: missing StaffTransitionRefused import; GREEN69 passed. Ruling: retain plan explicit attended↔no_show correction and repeat no-op while prohibiting cancelled resurrection — plan explains correcting staff misclicks, improves clinic operability; cost if wrong: endpoint allows an outcome correction omitted from spec closed-button matrix. Final reviewer must weigh binding spec.
- Task 6 Ruling SUPERSEDES previous correction allowance: coordinator requires binding spec closed outcomes immutable; attended↔no_show now409 not_live, repeated same outcome no-op, cancel existing behavior retained — cost: misclick correction requires future explicit action. Correction RED4 failures→GREEN71 passed; commit 2bec01b7546cdb8f1dc093601bb66f90b3324449
- Task 8 RED5 missing sender/marker failures, 1 unchanged followup passed; replaced plan corrected-back acceptance with refusal plus original notice remains once (Task6 binding Ruling). Baseline independently confirmed by root:4669 passed,68 skipped,21 warnings on2e6dd91 in618.18s.
- Task 9 Ruling: extraction moves resolve_booking_plan_ids seam; two unchanged R6 fault-injection tests patch removed worker alias — retarget monkeypatch to shared writer preserving failure scenario and compensation assertions; cost if wrong: fault injection misses writer, prevented by meaningful DB rollback assertions. First GREEN attempt2 failed122 passed (only old monkeypatch location).
- Task10 RED missing module→GREEN22 plan tests; additional concurrent-field RED4→GREEN26. Ruling: compare all editable row fields under lock, restore committed winner details on Google compensation — plan only compared event/start and could lose simultaneous service/insurance/attendee/contact edits; cost: extra read on failed writes, Google remains best-effort compensation. Coordinator explicitly confirmed409 behavior.
- Task12 RED6 missing patient_notice failures→GREEN94 passed; existing cancel enqueue assertions preserved. Producer7f3331d additive contract committed,66focused tests; final docs/review pending. Ruling: proceed consumer13 tests against committed producer contract while its independent final validation finishes — coordinator authorized, no integration/deploy; cost: producer review may later require consumer adjustment.
- Task11 fidelity Ruling: full channel-neutral edit body never truncated; channel sender emits complete detail text then concise unchanged-ID card when >1024, free WhatsApp text chunks≤4096, Portal detail unlimited — binding all-field/current-footer requirement overrides plan global cap; cost: extra message(s), partial WhatsApp detail may have left if card subsequently fails; failure code truthful and paid900 wire cap unchanged. RED3 failures→GREEN59 passed, long Portal multi-field route included; commit 43643a23f0cf8e8ed5c184999d5a2ae618d2661d
- Task13 RED module missing→GREEN146 passed; current linked professional checked tenant-scope; no producer writes. Ruling: existing positive introspection cache TTL retained per plan — whole auth seam remains consistent and bounded; cost if wrong: current mutable role changes apply after configured TTL, final review retained the bounded cache; coordinator adjudication follows below.

## Revisão final

Revisão independente concluída em `C:/TECH/BRAIN/tasks/TASK-044/REVIEW_R7.md`: três achados Important (I1–I3). Corrigidos no passe único abaixo, com regressões RED→GREEN; adjudicação final é do coordenador, sem repetir a revisão.


## Passe único das correções finais (I1–I3)

Base do passe: `edd56f3`. Código final: `fcf9c86146e42c2fd54febd5756e38a6f3548a16`.

```text
fcf9c86 fix(calendar): refuse explicit origin without a proven event owner (TASK-044)
46f79a5 fix(calendar): preserve physical Google origin independently of doctor visibility (TASK-044)
3370214 fix(reminders): atomically claim confirmation and guard current status (TASK-044)
```

Suíte completa após todas as correções: **4913 passed, 68 skipped, 28 warnings in 229.32s (0:03:49)**. `uvx ruff check .`: zero achados (`fix-pass-ruff.json = []`).
A rodada anterior de 4892 passed/68 skipped continua sendo evidência da versão anterior. Uma primeira tentativa de validação deste passe foi interrompida em 26%, sem falhas, quando o coordenador pediu a cobertura adicional de origem profissional sem dono; a única rodada COMPLETA do passe é a registrada acima.

- I1: a decisão de criar/reutilizar cartão é feita sob lock da consulta. Conflito real no índice único é recuperado em savepoint; `created=False` não envia um segundo aviso de confirmação. Duas sessões independentes reproduziram ambos os interleavings: RED2→GREEN.
- I2: PATCH recarrega e bloqueia a consulta antes de validar, atribuir ou aplicar efeito financeiro; o viewer atual é rechecado após o lock. Fechamento concorrente e remarcação para o futuro são recusados sem aviso/dinheiro. O cartão revalida estado vivo e uma versão de horário, médico, atendido e identidade antes de usar seu corpo. RED5 + RED1 (troca de médico após montar texto)→GREEN.
- I3: **terceira coluna aditiva** `appointments.google_calendar_source`, nullable (`clinic`/`professional`, NULL=regra legada). Ela foi acrescentada à revisão R7 `d8e3a5c1f7b2`, que permanece NÃO aplicada em banco real. Criar consulta/bloqueio pelo hub continua gravando evento na agenda da clínica e registra origem `clinic`; visibilidade continua pelo médico. Editar/liberar, ações do paciente e R6 usam a origem física. Troca efetiva de calendário muda a origem no gravador compartilhado; trocar só o médico numa mesma agenda conserva a origem. Metadado interno e opcional no snapshot/rascunho: rascunhos antigos continuam válidos e os schemas públicos do R5 não ganharam campos.
- Eventos reais por agenda no double comprovam criar→editar→liberar, mover médico apagando o evento original da clínica, edição/movimento R6 de consulta criada pelo hub, bloco e fallback NULL. Origem explicitamente profissional sem médico (FK removida) ou origem não-NULL desconhecida recusa com `calendar_unresolved`/worker sem agenda, preservando evento e estado. RED4 + RED4→GREEN.
- Rollback perde também os metadados de origem: código antigo volta à inferência pelo médico. A distinção entre evento da clínica e agenda própria some. Ordem permanece **DDL das TRÊS colunas → brain-api → API+worker → front**; nenhuma etapa operacional foi executada.
- Pré-requisito brain-api verificado por leitura em `2450502`: checkpoint `docs/CHECKPOINT_hub_token_agenda_scope.md`, produtor `7f3331d`/`cf0833f`, suíte 1391 passed/3 skipped/39 warnings (baseline1355),66 testes focados e revisão READY informados na sessão produtora. Não houve integração, escrita no produtor nem deploy.
- Minor M1 adiado: cópias originais do plano e spec continuam deliberadamente untracked; não foram modificadas nem incluídas nos commits do passe. Os três arquivos duráveis gerados de Graphify permanecem locais/uncommitted, sem caches ou snapshots no commit.
- Relatório do passe: `C:/TECH/BRAIN/tasks/TASK-044/results/r7-fix-pass.md`. Os demais recortes "Declined to judge" do revisor ficam preservados como planejado; decisões finais e custos são do coordenador. Não foi feito novo passe de revisão.


## Adjudicação final do coordenador

**READY_WITH_DEFERRED_M1.** I1–I3 foram corrigidos e verificados por reproduções RED→GREEN e suíte final de 4913 passed/68 skipped/0 failed. O relatório independente original avaliou edd56f3; o coordenador examinou o delta final e as provas. Nenhuma segunda revisão foi solicitada. Branch e worktree permanecem locais, sem integração ou publicação.

Todos os recortes considerados pelo revisor foram decididos explicitamente, com os seguintes custos:

| Ordem | Decisão | Motivo | Custo/limite aceito |
|---|---|---|---|
| 1 | Manter cache positivo de até 60 s | Contrato e cache de autenticação existentes, explicitamente preservados pelo plano; produtor lê o cadastro atual | Uma alteração de papel pode levar o TTL configurado para valer no consumidor |
| 2 | Manter limite de 900 caracteres somente no modelo pago | Fallback de uma variável autorizado; novo modelo/custo não faz parte desta execução | Aviso pago excepcionalmente longo pode perder o final; Portal e mensagens gratuitas mantêm os detalhes |
| 3 | Manter `notice_failed` para envio parcial de detalhes antes de falha do cartão | Vocabulário de resposta não tem entrega parcial; não ocultar a falha nem criar novo contrato | Nova tentativa pode repetir detalhes; partes já enviadas podem não constar do histórico persistido após rollback |
| 4 | Manter repetição de Compareceu como no-op, sem retry automático do aviso pelo PATCH | Escolha explícita do plano, preview antes da ação e recuperação pelo follow-up | Aviso não entregue exige mensagem manual ou abertura posterior do paciente; se R5 oferecer retry pelo PATCH, esse contrato precisará mudar |
| 5 | Manter recusa de replay de edição/cancelamento já aplicado | Operações existentes retornam nothing_changed/already-cancelled; não criar nova ação de reenvio | Não há reenvio automático desses avisos; o operador usa mensagem/link disponível |
| 6 | Manter envio síncrono com claim antes do envio | Arquitetura autorizada; recuperação com lease/outbox seria trabalho novo | Uma queda do processo nessa janela pode impedir o aviso; não prometer entrega exatamente uma vez |
| 7 | Não ampliar compensação cross-calendar além da correção de origem I3 | Não foi demonstrada corrupção do evento vencedor; o evento antigo pode já ter sido removido | A compensação pode falhar e deixar necessidade de limpeza manual quando outra operação também falhou |
| 8 | Manter limpeza do evento antigo como best-effort | Comportamento do R6 preservado; nova fila de reconciliação fora do plano | Falha de limpeza pode deixar evento duplicado até intervenção |
| 9 | Manter leitura do hub somente na agenda Google da clínica | Decisão vinculante do dono | Consultas em agendas Google próprias continuam fora da lista do hub |
| 10 | Não refatorar todos os eventos/caminhos Google legados de cancelamento e remarcação | Escopo e compatibilidade explícitos; I3 corrige os eventos novos e seus leitores pertinentes | Limitações anteriores para eventos em agendas próprias permanecem |
| 11 | Manter console/conversas e listas do brain-api fora do R7 | Spec §5.D exige plano separado | Médicos continuam com visão ampla nesses canais até a tarefa específica |
| 12 | Não redesenhar criação legada com referência de paciente nesta execução | Comportamento anterior; novos leitores e avisos verificam tenant | A criação legada ainda pode produzir uma associação inadequada; nenhuma nova consulta/aviso pode revelar dados de outra clínica |
| 13 | Manter parsing compatível de UUID profissional malformado como ausente | Produtor interno conhecido emite UUID válido e scope explícito; próprio scope own sem profissional vê nada | Uma resposta interna malformada sem scope pode acionar fallback legado; não representa fluxo normal do produtor atual |
| 14 | Não inventar revogação de acesso por profissional inativo | Política de usuário/identidade não definida por este plano | Inativar agenda não é equivalente a revogar a conta; acesso deve ser removido pela autoridade de identidade |
| 15 | Manter coerção de representações integrais para lead | Valor fracionário/range inválido é recusado; representação integral conserva a mesma janela | String ou número integral pode ser aceito no boundary, sem alterar o prazo configurado |
| 16 | Manter exceção legada de PATCH cancelled | Decisão explícita de compatibilidade no plano/ruling; correções attended/no_show continuam recusadas | Cancelamento ainda pode alterar um resultado encerrado pelo caminho legado |

**Rollback não é automaticamente simétrico:** o código antigo não lê a origem física das consultas novas criadas na agenda da clínica com médico vinculado, mesmo antes de remover a coluna. Validar essas consultas antes de qualquer reversão; não presumir que código antigo ou downgrade recuperam a agenda correta. Nenhuma reversão ou migração foi executada.

M1 permanece adiado: fontes de plano/spec fora do snapshot Git, preservadas localmente. Antes de compartilhar/integrar, incluir essas fontes ou fixar a referência durável. Os snapshots duráveis de Graphify foram atualizados localmente e não commitados; caches/artefatos temporários não entraram nos commits.
