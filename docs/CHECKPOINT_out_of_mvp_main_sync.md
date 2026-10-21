# Atualização de feature/out-of-mvp — TASK-066

## Continuação: main remota e agenda nativa

O dono pediu pull e novo merge após a rodada abaixo. Main foi atualizada para `fc94a45`. O merge preserva reservas Pix e incorpora agenda do profissional, releitura de choque na confirmação, agenda nativa e seus avisos/jobs. O registro combinado tem 20 funções e 14 crons.

Nova revisão de merge `d6f0a8c2e4b7` une as histórias Pix e agenda nativa, sem alterar revisões antigas ou executar SQL. Promoção após pagamento e aceitação de Manter passam a funcionar também na agenda nativa: id local determinístico, criação exclusiva e recuperação idempotente do evento numa repetição. Regressões reproduzidas antes do ajuste e provas locais após ele; detalhes em `C:/TECH/BRAIN/tasks/TASK-066/PULL_MAIN.md`.

Validação ampla desta continuação: 6.521 passed, 82 skipped, 1 xfailed, 3 failed (63 warnings, 20m59s). As três falhas eram adaptações de testes aos contratos da main: argumento `professionals` removido em duas chamadas e calendário falso sem `list_events` em outra. Corrigidas somente as fixtures/chamadas dos testes. Reexecução final `--lf`: **3 passed**, sem falhas restantes no conjunto que havia falhado; arquivos relacionados também repetidos: **108 passed** e **8 passed**. Teste adicional de pagamento até confirmação na agenda nativa: **1 passed**. A suíte inteira não foi repetida após essas adaptações exclusivas de testes. Ruff dos arquivos conciliados passou; head único `d6f0a8c2e4b7`. Números abaixo pertencem à primeira rodada. Sem push/deploy/migração em banco.

## Primeira rodada: main local

Em 2026-10-21, incorporada a main local `59f260a` na feature que partiu de `d42175c`, por pedido explícito de `git merge main`. Sem push/deploy e sem migração executada em banco.

Conciliação preserva os gates de papel de pagamento e a introspecção fresca de integrantes da main. `get_current_tenant` guarda o resultado na requisição para o escopo de agenda reutilizá-lo sem prolongar acesso revogado. O provisionamento mantém tanto conexão Asaas quanto capacidade de profissionais. O worker registra 18 funções e 14 crons; a lista combina os jobs financeiros com a entrega de primeiro acesso. Os links de consultas liberadas usam `BRAIN_MESSAGE_PUBLIC_URL`, acompanhando a padronização de URLs da main.

Validação focada: 77 testes de papéis financeiros, ações de consulta, identidade do build e envio das ofertas passaram; ruff passou nos arquivos conciliados e Alembic tem um único head (`b6d2e8f4a1c3`). Suíte completa final: **6.279 passed, 82 skipped, 1 xfailed, 0 failed**, 62 warnings, em 20 minutos. Os skips não constituem prova de PostgreSQL nem de integrações reais; o xfail da qualificação das sondas Asaas continua pendente por desenho.

A configuração local preexistente do worktree interferiu na primeira rodada. A validação final desabilita apenas no processo de testes o carregamento de `.env` (`Settings.model_config['env_file'] = None`), sem alterar nem expor o arquivo. Dois casos investigados também passaram em checkout destacado do commit anterior.

Continuam pendentes as telas financeiras, qualificação real das sondas/contrato Asaas, modelos Meta e prova completa dos canais; Mercado Pago não consta do registro de provedores. O backend de fontes permanece em `task/TASK-026-context-sources`, ausente desta feature. Ao integrá-lo, conciliar sua migração `a1c4e7b9d2f3` com a cadeia Pix, pois ambas saem de `a4c7e2f9b1d3`. A leitura de integrantes para o prazo de devolução ainda falta, embora a gestão da equipe tenha vindo na main.

Relatório transversal e prioridades: `C:/TECH/BRAIN/tasks/TASK-066/ANALISE.md`. Os grafos preexistentes não foram usados como prova da branch nem reconstruídos nesta auditoria. Produção não foi consultada.
