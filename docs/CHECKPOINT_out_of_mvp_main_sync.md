# Atualização de feature/out-of-mvp — TASK-066

Em 2026-10-21, incorporada a main local `59f260a` na feature que partiu de `d42175c`, por pedido explícito de `git merge main`. Sem push/deploy e sem migração executada em banco.

Conciliação preserva os gates de papel de pagamento e a introspecção fresca de integrantes da main. `get_current_tenant` guarda o resultado na requisição para o escopo de agenda reutilizá-lo sem prolongar acesso revogado. O provisionamento mantém tanto conexão Asaas quanto capacidade de profissionais. O worker registra 18 funções e 14 crons; a lista combina os jobs financeiros com a entrega de primeiro acesso. Os links de consultas liberadas usam `BRAIN_MESSAGE_PUBLIC_URL`, acompanhando a padronização de URLs da main.

Validação focada: 77 testes de papéis financeiros, ações de consulta, identidade do build e envio das ofertas passaram; ruff passou nos arquivos conciliados e Alembic tem um único head (`b6d2e8f4a1c3`). Suíte completa final: **6.279 passed, 82 skipped, 1 xfailed, 0 failed**, 62 warnings, em 20 minutos. Os skips não constituem prova de PostgreSQL nem de integrações reais; o xfail da qualificação das sondas Asaas continua pendente por desenho.

A configuração local preexistente do worktree interferiu na primeira rodada. A validação final desabilita apenas no processo de testes o carregamento de `.env` (`Settings.model_config['env_file'] = None`), sem alterar nem expor o arquivo. Dois casos investigados também passaram em checkout destacado do commit anterior.

Continuam pendentes as telas financeiras, qualificação real das sondas/contrato Asaas, modelos Meta e prova completa dos canais; Mercado Pago não consta do registro de provedores. O backend de fontes permanece em `task/TASK-026-context-sources`, ausente desta feature. Ao integrá-lo, conciliar sua migração `a1c4e7b9d2f3` com a cadeia Pix, pois ambas saem de `a4c7e2f9b1d3`. A leitura de integrantes para o prazo de devolução ainda falta, embora a gestão da equipe tenha vindo na main.

Relatório transversal e prioridades: `C:/TECH/BRAIN/tasks/TASK-066/ANALISE.md`. Os grafos preexistentes não foram usados como prova da branch nem reconstruídos nesta auditoria. Produção não foi consultada.
