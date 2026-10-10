# Release R5 / R7 / R8 — 2026-10-10 (TASK-047)

R7 já estava em main remota (`cb526d5`). R8 foi integrado sobre essa base em `3845c2b`, sem conflitos. O dono autorizou publicação em main nesta sessão; deploy e migração são manuais, ainda pendentes. Este registro de integração sucede os estados históricos dos checkpoints R7/R8.

## Validação do candidato

- Suíte completa: **4927 passed, 68 skipped, 30 warnings, zero failures**, 416.17 s.
- Ruff: passou. Alembic: cabeça única `d8e3a5c1f7b2`.
- Fonte de produção esperada (bytes Git/LF): **`3e5cde4aebb6`**. A API e o worker devem reportar esse fingerprint com `deploy_parity=match`.
- O código não foi alterado na integração. A documentação R7/R8 veio da branch R8, incluindo as decisões posteriores do dono. Graphify no início estava INVALID e não foi usado como prova primária.

## Deploy manual

Preparar a imagem nova da secretarIA e executar nela `/app/.venv/bin/alembic upgrade head` em execução pontual antes da troca dos processos. Não usar a imagem antiga, que não contém R7. Em seguida: brain-api main (`4ce0e97`) → **secretaria_api e secretaria-worker** na mesma versão → Brain-Message-Frontend main com R5 e R8.

`/build` mostra a cabeça dos scripts da imagem, não a revisão aplicada no banco. Conferir o sucesso da migração separadamente. O agente não realizou SQL remoto, deploy, acesso a Environment ou alterações no Easypanel.

## Prova em produção pendente

Antes do deploy, Chrome mostrou API e worker em `aa11e3be7642`, com cabeça de imagem `c2d5f8a1e4b6` e paridade match: ambos ainda antigos. Demonstração posterior deve usar clínica e pacientes de teste; verificar avisos Portal/WhatsApp, agenda por papel, ações por horário, mensagem pós-consulta única, lembrete extra e Meus pacientes.

Modelos novos Meta permanecem pendentes; fora da janela gratuita pode haver fallback pago sem botões. Nenhuma feature fora do MVP foi integrada por esta release. Rollback mantém a ressalva de origem física dos eventos documentada no checkpoint R7.
