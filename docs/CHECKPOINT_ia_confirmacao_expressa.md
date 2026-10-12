# CHECKPOINT — confirmação expressa, retomada do rascunho e gerenciamento v2 (TASK-030 P3)

**Estado atual:** P3 Tasks 1–9 concluídas e commitadas. Tasks 1–5 já publicadas em `6faf720`/`9e72ca5`; Tasks 6–9 em `244ead1`, com a main remota de lembretes R2 incorporada em `9ac99a6` e suíte conjunta aprovada. Integração/publicação autorizadas pelo dono em 2026-10-06; registro dos SHAs e verificação remota em `C:/TECH/BRAIN/tasks/TASK-030/results/p3-integration-codex.md`. Deploy pelo dono pendente; interruptor desligado por padrão e liberação completa depende de P4/P5. Nenhuma consulta de produção alterada pelo agente.

**Planos:** `docs/superpowers/plans/2026-10-02-ia-p2b-handbacks-ferramenta-e-estado.md` e `docs/superpowers/plans/2026-10-02-ia-p3-confirmacao-expressa.md`. **Spec:** `docs/superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md`, §4.1–4.5, §4.7, §4.11 e §7 (pré-checagem da remarcação).

## Correções

- A ferramenta v2 transporta serviço, profissional, convênio, para quem, dia e horário. O resolvedor confere cada item com dados atuais da clínica e disponibilidade da agenda, descontando reservas. Item inválido volta à pergunta correspondente.
- O rascunho conserva dia e horário enquanto o paciente responde para quem, convênio, profissional ou serviço. A resposta mais recente prevalece; o restante é revalidado. O rascunho expira em 30 minutos e é descartado no menu ou na ajuda “Não sei”.
- Dados completos e válidos chegam à mensagem de detalhes e ao cartão “Confirmar”. Nada é agendado pelo pouso da IA. O toque usa o mesmo caminho existente: evento e consulta no WhatsApp; reserva e confirmação por código no Portal.
- Os textos de gerenciamento e de confirmação de cancelamento exibem o início no fuso IANA da clínica. Por exemplo, `2026-10-16T17:40:00Z` aparece como **16/10/2026 às 14:40** em São Paulo. O instante persistido e o identificador ISO das opções permanecem iguais.

## Liberação e limites

A ferramenta v2, a confirmação expressa e a retomada nas perguntas de convênio/profissional/serviço seguem atrás de `initial_flows.ai_draft_v2`, desligado por padrão. A correção dos textos de horário funciona com o interruptor ligado ou desligado. Nenhuma configuração de produção foi alterada.

O plano recomenda liberar a v2 após P4/P5 e a conclusão do P3. O gerenciamento v2 (Tasks 6–8) foi concluído localmente na continuação abaixo; P4 e P5 permanecem pendentes. A publicação prepara o deploy conjunto de API e worker; só o reteste posterior comprova a correção em produção.

Não há migração nova. A coluna `flow_draft` usa a migração já aplicada `e7d3c1a9b5f2`. Um deploy futuro deve atualizar API e worker juntos e conferir a paridade de `/build`.

O comportamento existente do WhatsApp continua sem nova consulta de disponibilidade no toque “Confirmar”; o Portal verifica reservas. A mensagem de detalhes não é logada, pois pode conter nome de terceiro e convênio. O endereço só aparece quando existe rua cadastrada e não há unidade ativa.

## Validação

Suíte completa após as correções da revisão: **3442 passed, 10 skipped, 15 warnings in 431.43s (0:07:11)**. Ruff limpo nos arquivos Python modificados/novos; `git diff --check` sem erros. Comparação do fonte via AST confirma que `_handle_confirmation`, `_manage_cancel` e `_manage_reschedule` não mudaram; nenhuma migração nova.

Testes de regressão foram observados falhando antes das implementações e passando depois. Cobertura: ferramenta v1/v2 e cache, dados atuais da clínica, privacidade, respostas de convênio/médico/serviço, validade do rascunho, reservas, criação somente após confirmação, Portal/WhatsApp e fuso através do contexto real do worker.

Revisão independente concluída em uma passada; correções importantes verificadas com RED→GREEN e esta suíte completa. Os limites existentes e as decisões de escopo constam no relatório da revisão e no ledger.

## Âncoras

- `ai/tools.py::set_booking_draft_v2`, `ai/graph.py::_tool_cache_key`: seis campos e separação de agentes por variante.
- `services/booking_draft.py::_land_day`, `_express_confirmation`, `details_already_shown`: validação do dia/horário e confirmação.
- `services/booking_details.py`: detalhes e limites de texto.
- `services/flow_router.py::DRAFT_WAIT_STEPS`, `_resume_parked_draft`, `_confirmation_card`, `_appointment_local_start`: perguntas, cartão e fuso horário.
- `workers/shared/draft_resolution.py::_load_draft_context`, `_fold_answer`, `_resume_booking_draft`: dados atuais e retomada.

## Integração para a próxima sessão de testes — 2026-10-06

Commit de código/testes: `6faf72093f21eef2b67aa56c031d44e753bebefe`. A `main` recebeu fast-forward sem conflitos. O manifesto comprovou os oito documentos locais preexistentes preservados byte a byte e 402 arquivos de código/testes/configuração com conteúdo Git idêntico ao validado. A validação completa da `main` será registrada no relatório de integração da TASK-030 antes da publicação. Nenhum deploy, migração adicional, alteração remota de configuração ou cancelamento da consulta de teste executado pelo agente.

## Conclusão do P3 — 2026-10-06

Com `initial_flows.ai_draft_v2 = true`, `manage_existing_appointment_v2` aceita ação, referência da consulta (`AAAA-MM-DD HH:MM` no fuso da clínica) e novo dia/horário. `ManageRequest` preserva o sentinel antigo quando só há ação. A referência aparece no contexto apenas com o interruptor ligado, e as variantes v1/v2 não compartilham agente compilado.

`resolve_manage_request` usa somente consultas futuras do paciente desta clínica. Cancelar termina no cartão Sim/Não; remarcar chega ao seletor, lista ou cartão Confirmar/Cancelar conforme os dados válidos. O horário vem da agenda do dono da consulta, descontadas as reservas. A IA não executa cancelamento ou remarcação. Referência desconhecida ou ambígua exige escolha, mesmo quando há só uma consulta.

O worker relê clínica, interruptor, limite, consultas e roster ativo; recusa conversa de outra clínica. `_reschedule_limit_hit` roda antes de construir/consultar agenda, sem incrementar o contador. A recusa usa o envio existente de manter/cancelar. `_RESCHEDULE_PRECHECK_STEPS` passa a cobrir `manage_slot` e `manage_confirm` em todas as clínicas. Com o interruptor desligado, a entrada de gerenciamento e as opções antigas continuam preservadas.

### Correção da revisão: consultas simultâneas

As listas v2 usam `slot|appointment:<uuid>` como identificador interno do toque, resolvido só na lista atual do paciente; a IA continua recebendo referência temporal, sem UUID. Serviço e profissional aparecem na descrição da opção, respeitando 72 caracteres. Selecionar a segunda consulta mantém aquele alvo, mesmo com reordenação da lista. Identificador obsoleto/estrangeiro não seleciona nada.

Se os textos disponíveis continuarem indistinguíveis, o resultado usa o handover existente, sem consultar agenda nem cancelar/mover consulta. Não se amplia o contexto da IA com nomes de terceiros. A regra vale também para pedido v2 só com ação. As opções antigas de botões não mudam.

### Observabilidade e âncoras adicionais

`conversation_handback_entered` continua único por handback, só com nomes de campos/códigos/booleanos. Novos códigos: `unknown_appointment`, `appointment_not_chosen`, `reschedule_limit`, `ambiguous_appointment`; este último termina em `human_handover`. Nunca se registra referência, dia/horário, payload ou texto dos detalhes.

- `services/manage_request.py::ManageRequest`, `manage_target`, `resolve_manage_request`, `_specific_pick_result`.
- `services/flow_router.py::_manage_confirm_result`, `MANAGE_APPOINTMENT_PAYLOAD_PREFIX`, `_find_appt_by_iso`.
- `ai/tools.py::manage_existing_appointment_v2`, `ManageAppointmentRequested.request`; `ai/graph.py::run_agent`.
- `workers/shared/llm_context.py::_appointment_context_text`, `_flow_handback_tools`.
- `workers/shared/deposit.py::_reschedule_limit_hit`, `_at_reschedule_limit`, `_RESCHEDULE_PRECHECK_STEPS`.
- `workers/shared/sentinels.py::_handle_manage_appointment`; `workers/shared/handback_log.py::landing_of`.

### Validação da conclusão

Baseline `9e72ca5`: **3442 passed, 10 skipped, 15 warnings in 365.56s**. Suítes previstas: Task 6 **212 passed**, Task 7 **286 passed**, Task 8 **258 passed**. Antes da correção da revisão: **3505 passed, 10 skipped, 15 warnings in 330.73s**.

Uma revisão independente read-only (`gpt-6-astra`) encontrou um Important, nenhum Critical ou Minor. Falha reproduzida com **9 failed / 17 passed**; correção RED→GREEN, suíte relacionada **236 passed**, integração focada **268 passed**. Resultado completo após a correção: **3515 passed, 10 skipped, 15 warnings in 326.26s (0:05:26)**. Mesmos 10 testes ignorados e 15 avisos de depreciação do baseline, sem regressões.

Ruff limpo nos quinze arquivos Python afetados. `git diff --check` limpo. Comparação do fonte via AST comprova `_handle_confirmation`, `_manage_cancel` e `_manage_reschedule` intactos; nenhuma migração nova. Contagens de hunks de formatação dos arquivos antigos não aumentaram. Testes por Git Bash com `BOT_ALLOWLIST_WA_IDS` vazio; Ruff via `uv run ruff` conforme ferramenta disponível.

SQLite descarta offsets: o teste da remarcação confere o instante aware realmente enviado ao Calendar e, separadamente, o valor sem offset persistido pelo caminho existente. Esta execução não certifica PostgreSQL nem produção. Graphify foi atualizado por AST e diagnosticado, excluindo cópias temporárias de `.superpowers/` via `.graphifyignore`; checkout com código uncommitted permanece STALE por definição da política.

### Pendências de liberação e limites preservados

P4 (ferramentas cegas e privacidade em todas as tools) e P5 (prompt e avaliações reais) seguem fora deste P3. Não ligar o interruptor como rollout completo antes deles. Nenhuma migração nova; API e worker precisam subir juntos quando o dono autorizar, com paridade de `/build`.

Preservados: corrida existente do WhatsApp ao Confirmar e remarcação sem releitura final; confirmação concorrente/recuperação Calendar–banco não auditadas; lacuna legada de autorização de terceiros com flag OFF; fallback legado de sentinel malformado com par interno inconsistente. São limites existentes/separados, não garantias certificadas por este P3. Deploy, SQL remoto, configuração e consultas reais ficaram intocados.

Relatório da conclusão e decisões: `C:/TECH/BRAIN/tasks/TASK-030/results/p3-completion-codex.md`. Worktree/branch TASK-030 preservados, mudanças locais revisáveis e scratch ledger mantido até integração autorizada.

## Integração/publicação autorizadas — 2026-10-06

O dono solicitou merges e push para realizar o deploy. Commit P3 `244ead1`; merge da main remota `e23cbe4` em `9ac99a6`, preservando os lembretes R2 e os hooks existentes; sem conflitos de código. Suíte completa do código combinado: **3668 passed, 10 skipped, 15 warnings in 437.86s (0:07:17)**. Ruff dos quinze Python do P3 e diff-check limpos. O índice Graphify foi reconstruído para o código combinado e versionado em `6c45912`; seu SHA de geração é o merge de código, anterior ao commit de índice/documentação.

Os documentos locais da main foram protegidos antes da integração; suas alterações não fazem parte desta publicação. Fonte/testes/dependências da main devem ser idênticos aos blobs validados, conforme manifesto do relatório de integração. Worktree TASK-030 e scratch mantidos. A publicação prepara o deploy conjunto de API e worker pelo dono; não é prova de deploy, migração, ativação ou comportamento real. P4/P5 permanecem pendentes.

## Reteste após deploy informado pelo dono — 2026-10-06

API/worker comprovados pelo fingerprint `a069f15823c2`, equivalente às fontes Python de `01f2af7`, paridade `match`. Na Chrysostomo For Eyes, agendamento e remarcação por seleção chegaram ao cartão; desistências preservaram a consulta existente de 16/10 às 15:20, inclusive após reload. Gerenciamento/cancelamento exibiram horário local correto nesta sessão. Não houve nova confirmação, remarcação efetiva ou cancelamento efetivo.

Percurso em linguagem natural ainda não aprovado: dados completos repetiram atendido/dia; `14:40` digitado voltou ao dia, enquanto o botão funcionou. Interruptor v2 não consultado/alterado, então esta rodada não certifica a confirmação expressa nem o gerenciamento v2. P4/P5 e teste com ativação explícita seguem pendentes. Prova, limites e observações de automação/aba após reload em `docs/TESTES_REAIS_p3_posdeploy_2026-10-06.md`. Sem deploy pelo agente, SQL remoto, mudança de configuração, código, commit ou push nesta prova.

## Correção da captura no caminho atual — 2026-10-07

Correção aprovada após reteste real: a ferramenta atual passa dia/horário/atendido, usa data fresca no fuso da clínica e preserva preferências até o cartão, sem depender da liberação completa v2. Médico ambíguo/removido exige escolha explícita. Implementação e validação locais, sem nova publicação/deploy/ativação. Detalhes e limites em `docs/CHECKPOINT_ia_captura_datas.md`; esta correção supersede a preservação anterior da captura de três campos, mantendo o restante do rollout P4/P5 separado.

## Captura corrigida integrada — 2026-10-07

Captura completa e datas locais/relativas em `8a1aaf9`, combinadas com TASK-037 em `83dc2a4`. Suíte conjunta **3709 passed, 22 skipped, 15 warnings in 164.86s (0:02:44)**, lint/diff limpos. Merge/push autorizados; deploy pelo dono pendente, API e worker juntos. Sem migração/configuração/SQL remoto/consulta real nesta integração. Registro de publicação em `tasks/TASK-030/results/booking-date-integration-codex.md`; detalhes em `docs/CHECKPOINT_ia_captura_datas.md`.
