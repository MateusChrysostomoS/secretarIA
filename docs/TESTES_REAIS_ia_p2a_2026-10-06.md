# TASK-030 P2a — testes após deploy

Execução iniciada em 2026-10-05, encerramento em 2026-10-06 (America/Sao_Paulo).
O dono informou deploy dos serviços e migração. Código publicado: `f91b84d`.

**Continuação posterior, autorizada até criar uma consulta:** ver
`TESTES_REAIS_llm_agendamento_2026-10-06.md`. Uma consulta mantida para 16/10 às 14:40;
memória da LLM PASS, transferência de data/hora incompleta e divergência UTC no
gerenciamento. O limite não destrutivo abaixo descreve apenas o smoke anterior.

## Resultado

**SMOKE NÃO DESTRUTIVO APROVADO ATÉ O RESUMO, após a correção informada pelo dono.**
API/worker correspondem ao código publicado; o Portal persiste mensagens e respostas.
Pra quem, autorização de terceiro, reload, convênio, profissional e serviço funcionaram.
No reteste de 2026-10-06 às 11:48–12:01 BRT, dias, mais dias, horários, retorno para outro dia,
persistência após reload e resumo também funcionaram. A falha de agenda da primeira execução,
registrada abaixo como F1, não se repetiu no percurso retestado.

**Confirmação final, OTP e criação da consulta NÃO executados.** O fluxo de criação completo
continua sem prova ao vivo nesta tarefa; o resumo foi cancelado e a conversa voltou ao menu.

Não atribuir a causa ao P2a ou à migração sem evidência adicional. A interface prova a falha
de acesso à agenda, mas não distingue autorização Google, configuração ou outra falha interna.

## Primeira execução — método e limites

- `agent-browser` da Vercel, sessão própria `task030-p2a-live`, CDP 9334, aba fixada.
- Abas próprias t8 (paciente QA) e t9 (console de equipe); abas anteriores preservadas.
  t8 encerrada ao concluir a prova, removendo o observer transitório; t9 mantida no login
  para o dono. O navegador compartilhado não foi fechado.
- Clínica de teste: Chrysostomo For Eyes, sessão QA existente. Nome de terceiro inteiramente
  sintético; autorização cancelada, depois escolhida consulta para o próprio paciente.
- Sem criar consulta, reserva, remarcar/cancelar consulta existente, solicitar OTP, SQL remoto,
  alteração de configuração, Environment, deploy adicional ou leitura de credenciais.
- A transferência para humano foi automática, causada pelo caminho de falha da agenda.
  O texto informa que a equipe foi avisada; entrega do aviso não foi verificada.
- Console de equipe mostrou login; não havia sessão de equipe para verificar conexão/configuração
  da agenda ou retomar a automação do chat QA. Login manual solicitado ao dono, sem pedir senha.
- Ferramentas de logs Easypanel não estão expostas nesta sessão; MCP local está desativado.
  Não usado painel bruto, execução em container ou workaround para obter configuração.

## Primeira execução — verificações reais (histórico)

| Caso | Resultado | Evidência |
|---|---|---|
| Liveness API | PASS: HTTP 200, `status=ok` | `build-health.json` |
| Paridade API/worker | PASS: `deploy_parity=match`; ambos `43086a2439e5` | `build-health.json` |
| Equivalência com o código publicado | PASS: fingerprint dos blobs Git LF de `f91b84d` = `43086a2439e5` | Cálculo local pelo mesmo algoritmo; `build_sha` da imagem é `unknown` |
| Head embarcado | PASS: único `e7d3c1a9b5f2` nos dois serviços | `build-health.json`; não é leitura do stamp do banco |
| Portal QA autenticado | PASS: clínica correta e histórico recuperado | Navegação real na t8 |
| `/menu` e Agendar | PASS: entradas e respostas persistidas; cartão “Essa consulta é pra você?” | `01`–`03` PNG; histórico posteriormente relido |
| Pra outra pessoa | PASS: pediu nome antes de prosseguir | `04-other-name-prompt.json/png` |
| Nome de teste | PASS: exibiu autorização explícita, sem ir direto ao serviço | `05-authorization-card.json/png` |
| Reload durante autorização | PASS: cartão e nome sintético permaneceram | `06-authorization-after-reload.json/png` |
| Cancelar autorização | PASS: voltou a “Essa consulta é pra você?” | `07-authorization-cancelled.json` |
| Consulta para si | PASS: prosseguiu ao seletor de convênio | `08-self-insurance.json` |
| Particular | PASS: prosseguiu aos profissionais | `09-professionals.json` |
| Primeiro profissional do catálogo | PASS: seletor de serviços correspondente | `10-services.json` |
| Serviço configurado | PASS: pediu confirmação do serviço | `11-service-confirmation.json` |
| Confirmação do serviço → agenda | **FAIL:** mensagem de dificuldade técnica de agenda; histórico `humanActive=true` | `13-calendar-unavailable.json/png` |
| Dias, horários, limite de oito e navegação | **BLOCKED** pela indisponibilidade da agenda | Não marcar como aprovados |
| Booking final, OTP e criação | **NOT RUN**; nenhuma consulta/reserva criada | Fora do smoke não destrutivo |
| Resolvedor novo/rascunho estacionado por IA | **NOT REACHABLE neste P2a:** ligação às ferramentas é P2b | Cobertura automatizada anterior; não confundir com prova ao vivo |

O caminho exercitado leu e gravou conversas pelo ORM novo, sem erro visível de coluna ausente.
Isso dá evidência funcional do schema usado no caminho, mas não substitui inspeção do stamp,
tipo/default da coluna ou prova de todos os dados da migração. Migração real foi informada pelo dono.

## Finding F1 — indisponibilidade da agenda

Severidade: **HIGH para o percurso testado**, porque impede escolher dia/horário. Blast radius
não determinado: só o primeiro profissional/serviço foi exercitado nesta sessão.

Estado atual: **sintoma resolvido no reteste do percurso, após correção informada pelo dono.**
Dias e horários passaram na execução de 2026-10-06 à tarde UTC. A causa e a alteração de
configuração feita pelo dono não foram inspecionadas. A reprodução abaixo é histórica.

Reprodução:

1. Abrir a clínica com a sessão QA e enviar `/menu`.
2. Tocar Agendar, escolher consulta para si.
3. Selecionar Particular e confirmar a resposta do seletor.
4. Selecionar o primeiro profissional e o serviço oferecido “Cirurgia de Catarata”.
5. Confirmar o serviço com “Sim”. Não tocar confirmação final de consulta.
6. Observar: “Estou com uma dificuldade técnica para acessar a agenda agora. Nossa equipe
   foi avisada e entrará em contato em breve.” Nenhuma lista de dias/horários é apresentada.
7. O GET autenticado do histórico retorna HTTP 200, as entradas/respostas estão persistidas,
   e `accepts_typing=true` indica que a conversa está em atendimento humano.

A imagem `13-calendar-unavailable.png` mostra só o cartão novo, sem histórico de paciente.
JSONs guardam apenas status, etapas, contagens e metadados; nomes de profissionais mascarados.
Não há HAR, cookies, tokens, e-mail ou payload clínico nos artefatos. Sem gravação de vídeo:
o recorder criaria outro contexto e capturaria histórico anterior antes de poder redigi-lo.

## Correção da primeira medição

A primeira sonda usava `created_at >= Date.now()` e retornou falso negativo de persistência.
O navegador/host de execução está aproximadamente 59 segundos à frente das datas do servidor:
na leitura independente, cliente `03:00:37 UTC` e HTTP Date `02:59:38 UTC`. A mensagem `/menu`
foi persistida com `02:48:03 UTC` enquanto o ACK foi observado pelo cliente em `02:49:02 UTC`.

O harness foi corrigido para comparar IDs contra o snapshot anterior, sem usar relógios distintos.
Após a correção, todas as etapas registradas retornaram novas entradas e respostas no histórico.
**Não existe finding de perda de mensagem nesta prova.** Essa medição não revisa nem invalida
o relatório R1 anterior, cuja execução e evidências são independentes.

## Pendências registradas na primeira execução (histórico)

1. Com sessão de equipe, verificar a conexão/permissão da agenda do profissional testado e os
   eventos sanitizados `worker_calendar_unavailable`/`calendar_*` pelos meios permitidos.
2. Determinar a causa antes de corrigir; nenhuma reconexão/configuração feita pelo agente.
3. Retomar a automação do chat QA após resolver a agenda e repetir dias/horários/navegação.
4. P2b segue pendente. Este deploy não habilita a nova entrada ampla da IA.

Evidências locais: `C:\TECH\BRAIN\tasks\TASK-030\evidence\p2a-postdeploy-2026-10-05\`.
Scripts transitórios de observação ficam apenas nessa pasta; não alteram o código da aplicação.

## Reteste após correção informada pelo dono — 2026-10-06

O dono informou que corrigiu a agenda. Nova sessão `task030-p2a-retest` do agent-browser,
na mesma sessão QA do portal, confirmou health HTTP 200 e paridade API/worker ainda `match`,
com fingerprint `43086a2439e5` e head embarcado `e7d3c1a9b5f2`.

Precondição às `03:15:06 UTC` do cliente: GET do histórico HTTP 200, `humanActive=true`.
A última mensagem continua sendo a falha anterior, sem novas mensagens enviadas nesta tentativa.
**Agenda após a correção ainda NÃO testada:** o atendimento humano impede iniciar outra execução
automatizada. Isso não reproduz a falha de calendário nem demonstra que a correção falhou.

As abas do console disponíveis exigem login. O dono informou que não consegue devolver o chat
à automação; solicitada sessão de equipe para o agente operar somente o chat QA. Nenhuma
mudança de configuração, SQL remoto ou chamada administrativa autenticada foi realizada.

Ao disponibilizar o login, constatado que o navegador QA anexado por CDP roda headless.
Foi aberta uma janela visível separada pelo agent-browser (`task030-p2a-console`) para
autenticação manual de equipe, sem copiar cookies ou credenciais. O navegador QA e suas
abas originais permaneceram preservados. O dono se dispôs a fazer login.

No código publicado há o controle do console “Devolver à secretarIA” e retorno automático por
inatividade: timeout padrão de 30 minutos, varredura a cada 15 minutos. O valor configurado
em produção não foi inspecionado; o código não permite garantir o horário efetivo do retorno.
`/menu` é processado depois do bloqueio por atendimento humano e não o remove.

Evidências sanitizadas desta tentativa:
`C:\TECH\BRAIN\tasks\TASK-030\evidence\p2a-postdeploy-2026-10-06-retest\`.

## Reteste concluído — 2026-10-06, 11:48–12:01 BRT

As sessões da madrugada foram encerradas antes desta retomada. A janela de equipe foi
recuperada e o dono fez login manualmente; nenhuma credencial foi solicitada ou exportada.
No console, o chat antigo foi identificado pela janela temporal e pelas mensagens sintéticas
de terceiro/F1. Já estava com “secretarIA conduz”; **nenhuma troca de modo foi realizada**.

Foi recuperada também a sessão do portal no perfil existente do navegador de testes,
`task030-p2a-portal-recovered`, via agent-browser. É **outra conversa autenticada da mesma
clínica**, com automação ativa, usada para este smoke. O fluxo exercitado foi consulta para si,
Particular, primeiro profissional do catálogo e Cirurgia de Catarata. Não extrapolar o
resultado para todos os profissionais, serviços ou canais.

| Caso | Resultado | Evidência |
|---|---|---|
| API/worker e health | PASS: health ok, fingerprint `43086a2439e5`, head embarcado `e7d3c1a9b5f2`, parity match | `build-health.json` |
| Agendar → pra quem → convênio → profissional → serviço | PASS, POST/GET HTTP 200 e novas entradas/respostas persistidas | `01`–`05` JSON |
| Confirmar serviço → dias | PASS, sem indisponibilidade de agenda ou atendimento humano | `06-days.json`, `06-days-dialog.png` |
| Ver mais dias | PASS: primeira página 06–15/10, segunda 16–23/10 | `07-more-days.json`, `07-more-days-dialog.png` |
| Selecionar 16/10 → horários | PASS: oito opções de 09:00 a 16:00 | `08-slots.json` |
| Reload e reabrir a clínica | PASS: mesma lista de oito horários distintos e mesmo dia | `09-slots-after-reload.json/png`, `09-slot-metrics.json` |
| Outro dia → 19/10 | PASS: voltou ao seletor e carregou oito horários para o novo dia | `10-other-day.json`, `11-slots-other-day.json` |
| Escolher 09:00 → resumo | PASS: serviço e 19/10 às 09:00 corretos, botões Confirmar/Cancelar | `12-summary.json`, `12-summary-metrics.json` |
| Cancelar somente o resumo e Menu principal | PASS: sem booking, retorno ao menu, automação ativa | `13-summary-cancelled.json`, `14-cleanup-menu.json` |
| Confirmação final, OTP, escrita na agenda | NOT RUN: limite do smoke não destrutivo | Nenhuma ação de confirmar executada |
| Outro serviço, outros profissionais/canais, concorrência/holds controlados | NOT RUN ao vivo | Não inferir dessa prova |

O resumo atual não mostra convênio: `insuranceMatches=false` é observação da interface,
não finding de perda do campo. `_recap_text` em `flow_router.py` exibe serviço, atendido
(somente terceiro) e data/hora; não exibe convênio nem profissional. Não houve inspeção
direta de `flow_selected_insurance` remoto.

A página geral `/clinicas/` volta ao seletor de clínicas no reload. Ao reabrir a mesma clínica,
o histórico e o seletor de horários reapareceram; não houve perda do estado do fluxo.
Tentativas de leitura antes de reabrir a clínica foram corrigidas no harness, sem finding
de regressão. A captura usa o seletor nativo `dialog[open]`, porque o elemento HTML dialog
não tem um atributo `role=dialog` explícito. Um timeout de snapshot do CLI foi contornado
com leitura DOM sanitizada e seletores CSS observados; não atribuído ao serviço.

As etapas usam diferenças de IDs, sem comparar timestamps do servidor com o relógio local.
O caminho permaneceu `humanActive=false`. Nenhum clique no Confirmar final; `_handle_slot`
somente gera o resumo, enquanto booking/hold está no handler de confirmação. Cancelar o
resumo não cancelou consultas existentes. Nenhum SQL remoto ou alteração de configuração.

Evidências: `C:\TECH\BRAIN\tasks\TASK-030\evidence\p2a-postdeploy-2026-10-06-agenda\`.
Screenshots recortados dos seletores, sem identidade do paciente; JSONs só de metadados,
opções de horários e verificações booleanas. Observer transitório removido ao encerrar.
Relatórios locais; nenhum novo commit, push ou deploy nesta etapa.
