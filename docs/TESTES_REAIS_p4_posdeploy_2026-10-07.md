# TASK-030 — reteste após deploy do P4, 2026-10-07

Rodada aproximadamente 13:23–13:31 BRT, Portal secretarIA da Chrysostomo For Eyes, sessão QA existente. Pedido explícito do dono após o deploy. Chrome DevTools MCP; nenhum deploy pelo agente, mudança de configuração, SQL remoto, commit/push ou confirmação de consulta nesta rodada.

## Resultado

**PASS nas regressões de cartões e desistência do modo atualmente ativo.** Pedido completo para Cirurgia de Catarata em 08/10/2026 às 14:40 chegou diretamente ao cartão com o profissional solicitado. O rascunho foi descartado. Remarcação por seletores chegou ao cartão de 19/10 às 14:40 e foi recusada. Cancelamento mostrou Sim/Não e responder Não manteve a consulta original. Gerenciamento antes, depois e após reload mostrou a única consulta futura de 16/10/2026 às 15:20. Nenhum Confirmar nem Sim de cancelamento foi acionado.

**Disponibilidade em linguagem natural não aprovada nesta rodada.** Pedido somente de horários para 08/10 respondeu "Consigo mostrar ... Quer ver as opções?" e voltou ao menu, sem listar horários. Remarcação com referência/data/hora completas repetiu o seletor de dia; digitar 14:40 depois de 19/10 também repetiu o dia. O seletor de botões resolveu até o cartão. A repetição de remarcação já consta na prova pós-P3; não atribuir à regressão P4.

**P4 v2 ainda não certificado ao vivo:** leitura somente do booleano `initial_flows.ai_draft_v2` pela resposta normal do hub retornou **false** em duas leituras HTTP 200. Nenhum interruptor alterado. O deploy instalou o código, mas esta clínica continua com as ferramentas legadas. P5 continua necessário antes de ativar o conjunto v2. Não afirmar que `get_availability`, as ferramentas cegas ou o filtro v2 foram exercitados pelo modelo neste reteste.

## Identidade implantada

- `/health`: ok. API e worker: fingerprint **95aa4c547749**, paridade **match**, no início e no fim.
- Conteúdo Python do publicado **1c1ce2cd5159d9f049d41011fdd145a0a14a6b80** confirmado pelo fingerprint: archive Git com LF, 205 fontes Python ordenadas, mesmo algoritmo de SHA-256 do serviço.
- `build_sha`/`built_at`: unknown. É atribuição por conteúdo Python, não prova de um SHA informado pelo painel. A main local recebeu depois `f63458b`, só docs/testes; não muda esse fingerprint nem permite distinguir esses dois commits pelo build público.
- Migração embarcada `e7d3c1a9b5f2`; stamp remoto não consultado. Sem acesso ao ambiente do Easypanel.

## Casos

| Caso | Resultado | Evidência |
|---|---|---|
| Identidade API/worker | Conteúdo P4 presente, paridade match | 00-build.json, 01-deploy-proof.json, 16-build-final.json |
| Gerenciar baseline | Consulta 16/10 às 15:20 | 03-cancel-preview.json, 14-original-preserved.json |
| Cancelar, depois Não | Consulta mantida, menu | 03-cancel-preview.json, 04-cancel-rejected.json |
| Outro | Pergunta fixa "O que te traz à clínica?" | Observação direta antes da mensagem de disponibilidade |
| Consultar somente disponibilidade | Ofereceu mostrar, retornou ao menu sem horários | 05-availability-reply.json |
| Pedido completo amanhã 14:40 | Cartão direto de 08/10 às 14:40 | 06-booking-card.json |
| Descartar marcação | Não confirmou; ofereceu outro horário/menu | 07-booking-discarded.json |
| Ler interruptor da clínica | v2 false, sem alteração | 08-v2-disabled.json |
| Remarcar por frase completa | Repetiu dia | 09-natural-reschedule.json |
| Digitar novo dia, depois 14:40 | Horários, mas hora livre retornou ao seletor de dia | 10-reschedule-slots.json, 11-reschedule-time-repeated.json |
| Selecionar 19/10 e 14:40 | Cartão de remarcação | 12-reschedule-card.json |
| Recusar remarcação | Menu e consulta original preservada | 13-reschedule-rejected.json, 14-original-preserved.json |
| Reload e gerenciar | Consulta original ainda 16/10 às 15:20 | 17-after-reload.json |

## Limites e preservação

Nenhuma consulta criada, remarcada ou cancelada pelos toques desta rodada. Verificação da consulta pelo gerenciamento do próprio produto, sem inspeção independente do Google ou SQL remoto. Sem OTP novo, terceiros, concorrência, prova ao vivo de eventos de outro paciente/tenant ou tentativa de exclusão de evento externo.

Observador transitório guardou somente método/path/status: **115 requisições, 27 POSTs, zero status de erro**, incluindo leituras e marcações de mensagens lidas. Nenhum header, cookie, token ou body de rede coletado. Observer restaurado/removido antes do reload; aba do hub usada para ler apenas o booleano foi fechada. Artefatos sanitizados da sessão QA, profissionais/links mascarados. Browser mantido no menu da secretarIA, composer vazio, sem rascunho confirmado. A URL sem seleção perdeu a clínica no reload; a clínica foi selecionada de novo e a aba secretarIA conferida antes de enviar.

Envio real pelo WhatsApp não certificado: número/conversa de teste solicitado ao dono, sem resposta até o encerramento desta rodada. Reuso estrutural dos caminhos já coberto pelos testes offline e pelo código comum; isso não prova transporte Meta nesta execução. Nenhuma mensagem enviada a paciente real/terceiro.

A suíte de 4009 testes da integração é evidência local anterior; não foi repetida porque o teste pós-deploy não mudou código. Registro posterior f63458b de teste WhatsApp pertence à outra frente e não foi tratado como execução deste agente.

Artefatos em `C:/TECH/BRAIN/tasks/TASK-030/evidence/p4-postdeploy-2026-10-07/`. Registro local desta rodada, sem commit/push.
