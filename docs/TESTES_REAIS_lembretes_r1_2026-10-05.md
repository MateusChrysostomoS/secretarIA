# Testes reais R1 — 2026-10-05

## Resultado

**Última execução, 2026-10-06 (agenda autenticada): PASS nos casos executados de mensagem → persistência → resposta → reload e nos contratos R1 da agenda/contador/transições.** Consulta de teste, passada, cancelada e sem depósito usada temporariamente; status cancelled/contador 0 restaurados ao final. Limites restantes: envio novo R2, duas confirmações por lembretes distintos, aviso, invalidação de cronogramas com linhas existentes e concorrência controlada não foram exercitados ao vivo. Histórico das execuções anteriores abaixo.

**Reteste de 2026-10-06:** persistência recuperada; resposta automática ainda não comprovada. Ambas as mensagens do reteste foram gravadas e a pergunta permaneceu após reload. Nenhuma resposta nova em 138s. A agenda continuou exigindo sessão de equipe. A seção abaixo preserva a primeira execução; detalhes do reteste ao final.

**NÃO APROVADO como validação funcional completa.** O dono informou o deploy. A identidade dos serviços foi comprovada, mas duas mensagens de teste aceitas pelo portal não retornaram no histórico nem produziram resposta durante a observação. O acesso de equipe à agenda estava indisponível nesta sessão.

Clínica autorizada: Chrysostomo For Eyes. Navegação real pelo Brain-Message publicado, em uma aba própria. Sem SQL remoto, leitura de Environment, deploy adicional ou alteração de consultas existentes. Nenhuma credencial, código, e-mail ou conteúdo de paciente foi registrado neste documento.

## Evidências

| Verificação | Resultado |
|---|---|
| API `/health` pelo proxy publicado | HTTP 200, `status=ok`; prova apenas liveness |
| API `/build` | HTTP 200; `source_fingerprint=81d7bd8766ec`, `alembic_head=b8d3f1a6c2e5` |
| Worker anunciado | Mesmo fingerprint e head; `deploy_parity=match`; anúncio `2026-10-05T23:07:00.175131+00:00` |
| Código publicado | Fingerprint calculado dos blobs Git de `c90df58` com os bytes LF do checkout Linux: `81d7bd8766ec`, igual aos dois serviços |
| Portal do paciente | Sessão existente restaurada; clínica correta e histórico carregados; leitura de threads/mensagens HTTP 200 |
| Separação paciente/equipe | Token do paciente recusado pelo endpoint de emissão de hub-token: HTTP 401; refresh de equipe também HTTP 401 |
| Consulta ao horário, enviada pela caixa do portal | HTTP 200 no relay; bolha apareceu localmente; não retornou no histórico consultado depois |
| Segunda reprodução, saudação neutra pela caixa | HTTP 200 no relay com `payload.status=queued`; nenhuma resposta nova dentro do wait de 25s e nas leituras seguintes |
| Leitura final, 23:24:04 UTC | HTTP 200; 50 mensagens, `has_more=false`; última mensagem retornada ainda `21:02:33.529414Z`; nenhuma mensagem após o início das sondas; 24 bolhas do bot |
| Reload, 23:24:15 UTC | Clínica e histórico carregados; mesma contagem de 24 bolhas do bot; pergunta de teste desapareceu; nenhum alerta visível |

O head de `/build` vem dos scripts embarcados na imagem; **não comprova o stamp nem o schema efetivo do banco**. A prova descartável em Postgres continua pendente.

## Interpretação e limites

- Existe falha observável entre o ACK da fila e a devolução de mensagens persistidas ao portal. Ainda não é possível distinguir falha do job antes da persistência de uma falha de leitura/roteamento. A ausência de alerta no portal mascara o problema.
- Não atribuir a causa à R1 ou a migração faltante sem evidência do banco/logs. A paridade dos serviços elimina divergência de código API/worker como explicação desta execução.
- Não foi possível verificar GET da agenda com os campos R1, confirmação manual repetida (contador permanece 1), reset para scheduled, transição terminal ou isolamento transacional real no Postgres: requerem sessão de equipe e dados de consulta descartáveis.
- O link fornecido abre o portal do paciente, não uma sessão de equipe. O console em aba separada mostrou o formulário de login. Solicitação de login de equipe feita ao dono; nenhuma senha solicitada pelo chat.
- Gmail abriu na tela de login nesta superfície; não houve acesso autenticado às caixas postais, leitura de códigos ou necessidade de OTP para a sessão de paciente já existente.
- O MCP Chrome falhou por profile lock; o runtime CUA não tinha browser disponível. Fallback pelo skill agent-browser, sessão nomeada `task032-r1-live`, CDP existente 9334, aba fixada. As abas originais foram preservadas. Hooks temporários/tokens em memória removidos pelo reload.
- R1 não contém o envio novo de lembretes nem o transporte dos botões R2/R3. Esses comportamentos não foram marcados como aprovados.

## Próximos passos para fechar a prova

1. Acessar a agenda com uma conta de equipe da clínica autorizada e uma consulta de teste identificada como descartável.
2. Verificar o schema efetivo e os erros dos jobs em logs sanitizados pelo operador/ferramentas allowlisted; não abrir Environment. Ferramentas de logs Easypanel não estavam expostas nesta sessão.
3. Repetir a prova mensagem → histórico → resposta após resolver o bloqueio de processamento/leitura.
4. Executar os casos R1 da agenda e registrar os resultados reais; ainda não há aprovação de funcionamento completo.

## Reteste autorizado — 2026-10-06

O dono pediu repetir o teste. Mesma clínica, mesma sessão de paciente, duas mensagens neutras enviadas pela caixa do portal. Nenhuma consulta alterada, nenhum SQL remoto/deploy/configuração de serviço executado.

| Verificação | Evidência |
|---|---|
| Paridade publicada | `/build` HTTP 200; API/worker `source_fingerprint=43086a2439e5`, head da imagem `e7d3c1a9b5f2`, paridade match; worker anunciado às `03:07:00.272825+00:00` |
| Pergunta sobre horário | Relay HTTP 200, queued às `03:16:50.406804Z`; linha patient retornada às `03:16:50.550644Z` |
| Saudação neutra | Relay HTTP 200, queued às `03:17:37.181136Z`; linha patient retornada às `03:17:37.638284Z` |
| Reload do portal | Clínica e histórico carregados; a pergunta e a saudação permanecem visíveis, sem alertas |
| Leitura final | HTTP 200, relay `03:19:08.476758Z`; ambas as mensagens persistidas; zero linhas bot posteriores às sondas; typing false; último bot às `02:55:56.278208Z` |
| Acesso de equipe | `/auth/refresh` HTTP 401 na aba do console; casos de agenda continuam pendentes |

**Conclusão atual:** a falha de desaparecimento das mensagens não se repetiu; persistência e reload passaram. Resposta automática não observada após 138s desde a primeira sonda. Não concluir causa (worker, política de atendimento ou roteamento) sem logs/estado autorizado; não aprovar contador/agenda R1 com essa prova parcial. Não houve acesso a Gmail/OTP neste reteste.

O relógio do browser estava aproximadamente um minuto à frente do relay. Comparações de mensagens usam `Date.parse` e timestamps do servidor, evitando tanto a diferença de relógio quanto comparação lexical de timestamps com frações diferentes. Uma observação inicial ocorreu antes de o histórico terminar de carregar; não foi usada como prova de nova resposta. O número de linhas/bot pode mudar com paginação; a evidência usa as datas das mensagens efetivamente retornadas.

## Terceira execução — 2026-10-06, agenda autenticada

Autorização: dono pediu repetir os testes e informou autenticação da agenda/correções. Sessão própria anexada aos navegadores reiniciados, preservando as abas originais. Refresh de equipe com o header CSRF padrão `X-Brain-Client: web`: HTTP 200; identidade da clínica conferida antes de emitir hub-token (HTTP 200, role doctor). Uma sonda inicial sem esse header respondeu 403 e foi corrigida no harness, sem finding no produto.

API e worker mantêm paridade `43086a2439e5`, head embarcado `e7d3c1a9b5f2`; worker anunciado às `15:07:00.528975+00:00`. Não inferir a revisão aplicada a partir desse head. Leituras e escritas autenticadas abaixo provaram o funcionamento das superfícies R1 contra a persistência real.

### Mensagem e resposta

Sonda neutra enviada pela caixa do portal da Chrysostomo For Eyes. Baseline de IDs da página obtido antes do envio; nenhum payload de identidade exportado. Mensagem patient persistida às `15:26:19.871300Z`; duas novas linhas bot às `15:26:28.919768Z` e `15:26:28.925025Z` (cerca de 9s). Relay do POST respondeu HTTP 200; leitura HTTP 200. Ambas as respostas foram verificadas na API e no DOM por IDs/timestamps. Após reload, a mensagem e as duas respostas permanecem visíveis, clínica correta, zero alertas. **PASS**. A espera genérica do CLI expirou em algumas chamadas apesar do resultado presente; a evidência vem das leituras explícitas e do DOM final, não do exit code dessas esperas.

### Agenda e confirmação

GET `/tenants/me/calendar/events` de outubro: HTTP 200, 33 eventos, uma consulta local. Os cinco campos R1 estavam tipados corretamente na consulta local; todos nulos nos eventos sem consulta local. Não registramos summaries/IDs dos outros eventos. A tela `/agenda/` abriu sem pedir login e renderizou o calendário, sem alertas.

Consulta alvo: único evento local com nome explicitamente de teste; horário já passado, status cancelled, contador 0, sem depósito e sem lembretes. Verificação dessas condições antes de qualquer PATCH. Nenhum evento/consulta criado ou excluído; nenhuma chamada que envie notificação, altere Google Calendar, cobre ou estorne valor. Apenas PATCH de status, que não altera Google Calendar. A restauração ficou no bloco finally.

| Caso | PATCH/GET reais | Resultado |
|---|---|---|
| Voltar para agendada | scheduled → count 0 | PASS |
| Primeira confirmação staff | confirmed → count 1; GET confirmed/display confirmed/attention false | PASS |
| Repetir confirmação staff | confirmed novamente → count 1; GET permanece confirmed/1 | PASS: não duplicou nem chegou a 2 |
| Reset | scheduled → count 0; GET unconfirmed/attention false | PASS |
| Terminal | attended → count 0; GET unconfirmed/attention false | PASS |
| Restauração final | scheduled seguido de cancelled; GET cancelled/0/unconfirmed/attention false | PASS: status/contador originais restaurados |
| ID inexistente | PATCH UUID nulo → 404 | PASS, nenhuma consulta alterada |
| Status inválido | PATCH valor inválido na consulta teste → 422 | PASS, nenhuma transição aplicada |

Todos os PATCH válidos e GET de verificação responderam HTTP 200. A primeira/última confirmação são zeradas pelo reset antes da restauração; `updated_at` e os registros de transição refletem este teste, como esperado. A consulta final continua cancelada. Não prometer ausência de mutação: ocorreram transições reais autorizadas, restritas à consulta de teste.

**Limites:** com zero lembretes na consulta alvo, este teste não demonstra cancelamento/invalidação de linhas de cronograma. Não houve teste de segunda confirmação por lembretes distintos, atenção após aviso, remarcação A→B→A com enviados, corrida entre transações ou isolamento por uma segunda clínica. Nenhum SQL remoto, deploy, mudança de configuração, acesso ao Environment ou nova liberação de feature. R2/R3 ainda necessários para envio/transportes novos. Relatório e checkpoint locais, sem commit/push nesta etapa.
