# CHECKPOINT — correções do rascunho da IA e do horário da agenda (TASK-030)

**Estado:** código commitado em `6faf720` e integrado na `main` em 2026-10-06, após autorização do dono. Deploy pelo dono e nova prova em produção pendentes. P2b e P3 Tasks 1–5; Tasks 6–8 do P3 permanecem pendentes. A consulta de teste de 16/10/2026 às 14:40 (America/Sao_Paulo) permanece como estava.

**Planos:** `docs/superpowers/plans/2026-10-02-ia-p2b-handbacks-ferramenta-e-estado.md` e `docs/superpowers/plans/2026-10-02-ia-p3-confirmacao-expressa.md`. **Spec:** `docs/superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md`, §4.1–4.4, §4.7 e §4.11.

## Correções

- A ferramenta v2 transporta serviço, profissional, convênio, para quem, dia e horário. O resolvedor confere cada item com dados atuais da clínica e disponibilidade da agenda, descontando reservas. Item inválido volta à pergunta correspondente.
- O rascunho conserva dia e horário enquanto o paciente responde para quem, convênio, profissional ou serviço. A resposta mais recente prevalece; o restante é revalidado. O rascunho expira em 30 minutos e é descartado no menu ou na ajuda “Não sei”.
- Dados completos e válidos chegam à mensagem de detalhes e ao cartão “Confirmar”. Nada é agendado pelo pouso da IA. O toque usa o mesmo caminho existente: evento e consulta no WhatsApp; reserva e confirmação por código no Portal.
- Os textos de gerenciamento e de confirmação de cancelamento exibem o início no fuso IANA da clínica. Por exemplo, `2026-10-16T17:40:00Z` aparece como **16/10/2026 às 14:40** em São Paulo. O instante persistido e o identificador ISO das opções permanecem iguais.

## Liberação e limites

A ferramenta v2, a confirmação expressa e a retomada nas perguntas de convênio/profissional/serviço seguem atrás de `initial_flows.ai_draft_v2`, desligado por padrão. A correção dos textos de horário funciona com o interruptor ligado ou desligado. Nenhuma configuração de produção foi alterada.

O plano recomenda liberar a v2 após P4/P5 e a conclusão do P3. Esta correção não implementa o novo gerenciamento v2 (Tasks 6–8), P4 ou P5. A publicação prepara o deploy conjunto de API e worker; só o reteste posterior comprova a correção em produção.

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
