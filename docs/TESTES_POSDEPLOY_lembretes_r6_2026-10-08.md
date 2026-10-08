# TASK-032 — verificação após deploy, 08/10/2026

**Atualização 18:26Z: HTTP 500 anterior superado no reteste. Cartão inicial,
botões e menus R6 comprovados no Portal; confirmação de presença e Manter consulta
preservam a consulta. Edição completa NÃO APROVADA:** Particular e a seleção de
serviço, no cenário abaixo, desviaram para oferta humana em vez de resumo.
Ambas as consultas temporárias canceladas; janelas Google vazias e estados
cancelled conferidos. Sem nova mudança de código/deploy pelo agente.

## Resultado e limite

Pedido do dono: testar a entrega já publicada. **Versão de produção comprovada;
647 testes automatizados relacionados passaram. Jornada funcional real completa
ainda PENDENTE**, por falta de identificação da conta/consulta de teste e do
destinatário de teste para receber o aviso profissional. Solicitação dessas
informações enviada ao dono durante a execução.

Não houve deploy, SQL remoto, mudança de configuração, envio de mensagem pelo
compositor, confirmação/cancelamento/edição de consulta ou envio de e-mail pelo
agente nesta rodada. Não houve commit/push. Resultados automatizados são locais,
com banco descartável e transportes simulados; não comprovam entrega SMTP, Meta
ou mutação no Google Calendar real.

## Prova em produção

Chrome DevTools MCP, origem pública do Brain-Message. Em 2026-10-08T12:00:36Z,
GETs públicos via proxy /api/secretaria:

| Checagem | Resultado |
|---|---|
| /health | HTTP 200, status ok |
| /build | HTTP 200, deploy_parity match |
| Fingerprint API e worker | ambos a8eaeb7bdb0a |
| Conteúdo de 2240d7f | fingerprint Git a8eaeb7bdb0a, 215 fontes Python |
| Migração embarcada | b1c4e7a2d9f3 em ambos |
| /openapi.json | HTTP 200, rotas health/build presentes |
| Portal | sessão existente abriu a clínica; secretarIA selecionada e compositor disponível |

Fingerprint de Git calculado com caminhos relativos ordenados e bytes de git show,
pelo algoritmo de core/build_info.py::source_fingerprint, evitando divergência
CRLF do checkout Windows. main e origin/main locais em
2240d7f5ea361cfc02eda6262ca84515e13ea33a.

/build informa a migração **embarcada**, não o stamp aplicado ao banco.
Nenhum stamp remoto foi consultado. A primeira mensagem nova não foi exercitada:
a conversa aberta já tinha histórico e uma continuação anterior pendente.
Não interpretar ausência de cartão novo nessa sessão como regressão.
Nenhum nome, e-mail, código de convite, token ou histórico pessoal foi registrado.

## Testes automatizados

Execução em C:/TECH/BRAIN/secretarIA, commit 2240d7f, uv run --frozen pytest -q.

Primeiro grupo: **307 passed**, 91,34 s, sem falhas/avisos:

- test_first_message_reminder.py
- test_appointment_edit_apply.py, test_appointment_edit_flow_fields.py,
  test_appointment_edit_flow_menu.py, test_appointment_edit_pure.py,
  test_appointment_edit_safety.py
- test_professional_edit_notification.py
- test_reminder_r6_buttons.py, test_reminder_r6_actions.py
- test_reminder_r3_cancel_path.py, test_reminder_r3_continuations.py,
  test_reminder_r3_ids.py, test_reminder_r3_portal.py
- test_reminder_confirmation.py, test_reminder_opening_turn.py,
  test_reminder_opening_decision.py, test_portal_context_opening.py
- test_pii_pseudonymization.py, test_build_identity.py, test_workers_layering.py

Segundo grupo: **340 passed**, 27,96 s, sem falhas, um aviso de depreciação
Alembic por path_separator ausente:

- test_calendar_edit_methods.py, test_edit_day_picker.py,
  test_flow_edit_draft_column.py, test_wide_menus.py,
  test_brain_message_interactive_tap.py, test_llm_context.py
- test_reminder_v2_buttons.py, test_reminder_v2_delivery.py,
  test_reminder_v2_engine.py, test_reminder_v2_text.py
- test_flow_router.py, test_flow_router_multiprofessional.py,
  test_flow_router_insurance.py, test_flow_router_llm_draft.py,
  test_flow_router_scoped_help_llm.py

Inclui confirmação e descarte sem escrita antecipada, alteração efetiva em banco
de teste, compensação de agenda, conflito concorrente, médico/serviço/convênio,
restrições Pix, botões Portal/lista WhatsApp, primeira abertura idêntica,
privacidade/tenant, expiração, migração SQLite upgrade/downgrade, e-mail literal
com dados atualizados, duplicação/retry e supressão de avisos obsoletos.

Ruff nos 17 arquivos principais de implementação/testes: All checks passed.
uv run --frozen alembic heads: b1c4e7a2d9f3 (head).
git diff --check limpo. Nenhuma mudança de código foi necessária.

## Continuação real necessária

Na conta/consulta identificadas como teste: primeira abertura elegível;
Confirmar e menu recorrente; Cancelar até a pergunta de certeza sem apagar
consulta real; Alterar Dados com cinco opções, Alterar Mais Dados e descarte;
alteração final na consulta de teste, conferência independente do aviso recebido
pelo médico de teste e restauração do agendamento. WhatsApp requer número de
teste identificado. Não declarar esses cenários aprovados pelos testes locais.

## Seguimento — simulação de e-mail com link autorizada

O dono autorizou posteriormente envio de simulação ao endereço que forneceu e
especificou que o objetivo é abrir a conversa pelo e-mail e ver o cartão inicial
com Confirmar / Cancelar / Alterar Dados. O envio desta etapa usa, portanto,
`appointment_reminder_patient`, que já contém o link. O aviso ao médico
`appointment_changed_professional` não foi enviado nesta rodada.

Em **2026-10-08T16:29:41Z**, uma única chamada ao endpoint de aplicação
`/internal/notifications/email` respondeu **HTTP 200, queued=true**. Utilizado
o template real publicado, texto gerado por `build_reminder_body` com dados
fictícios claramente marcados SIMULAÇÃO e link público de convite para a clínica
da sessão atual. Nenhuma consulta fictícia foi criada para corresponder ao texto;
o corpo diz explicitamente que nenhuma consulta foi marcada. Não foram alterados
cadastro de profissional, destinatários de outros avisos, configuração ou agenda.
Autenticação legítima da aplicação, credencial somente em memória; não houve
acesso ao Easypanel, ambiente remoto ou execução em container.

**queued=true prova aceite/enfileiramento, não prova entrega na caixa de entrada.**
Confirmação de recebimento solicitada ao dono; SMTP do worker/inbox não observados.
Prévia local conferida: marca de simulação, frase canônica, link de conversa,
chamada de confirmação/cancelamento, ausência de placeholders pendentes e indicação
de que nenhuma consulta real foi marcada.

O link exato enviado abriu o Portal, selecionou secretarIA, disponibilizou o
compositor e não apresentou alertas. A identidade da sessão foi comparada em memória
ao endereço autorizado, sem registrar a identidade: **é outra conta e tem zero
consultas futuras ativas**. Por isso não é uma prova dos três botões, nem uma
regressão da abertura. Solicitado ao dono que abra o link na conta de teste com
consulta marcada. Não se gerou cartão falso para aparentar uma validação real.

Artefatos sanitizados em `C:/TECH/BRAIN/tasks/TASK-032/evidence/r6-email-simulation-2026-10-08/`:
`email-preview.txt` e `production-enqueue-result.json`. Execução explícita e sem
retry automático por `tasks/TASK-032/simulate_production_edit_email.py`.

## Reteste com o link do dono e consulta temporária

O dono forneceu o link da mesma clínica e autorizou prosseguir. A conta no
navegador continuou a mesma, sem consulta futura. Para concretizar o cenário,
criada pela API normal do hub uma consulta temporária marcada `[TESTE R6]`, em
16/10/2030, 15:20–16:00 America/Sao_Paulo, para o paciente dessa sessão. Nenhum
evento ocupava a janela antes da criação. Data distante evita crons de lembrete
durante a prova; fixture sem telefone, sem depósito e sem profissional vinculado,
sem aviso de marcação. Não se alterou qualquer consulta anterior da clínica.

Criação HTTP 201, status scheduled. Registro de cleanup em `live-fixture.json`.
Reabrir o link continuou exibindo somente o histórico anterior, sem cartão novo.
Investigação leu apenas projeções/contagens/flags sanitizadas, sem guardar PII.

### Falha reproduzida

Com o mesmo hub token legítimo da sessão de equipe e o tenant correto:

| Operação | Resultado |
|---|---|
| Configuração e consultas do tenant | HTTP 200 |
| Histórico interno do paciente, projeção Conversation.id/handover_state | HTTP 200; último registro ainda de 07/10 |
| GET /tenants/me/conversations | HTTP 500, text/plain, reproduzido |
| GET /tenants/me/conversations/not-a-uuid/messages | HTTP 404, JSON (guarda anterior à consulta ORM) |
| GET /tenants/me/conversations/00000000-0000-0000-0000-000000000000/messages | HTTP 500, text/plain (deveria ser 404) |
| GET /build, 2026-10-08T17:01:28Z | HTTP 200; API/worker match; fingerprint a8eaeb7bdb0a; head embarcada b1c4e7a2d9f3 |

`hub/conversations.py::_get_conversation` consulta a entidade inteira mesmo
para um UUID inexistente; `internal.py::list_brain_message_messages` seleciona
somente id/handover_state da conversa. A diferença isola a falha no caminho de
consulta da entidade completa, antes da resolução de dados de uma conversa
específica. Afeta também as decisões de abertura do worker que carregam essa
entidade. **Hipótese a conferir: schema do banco sem migração(s) exigida(s) pelo
código novo, incluindo flow_edit_draft.** Sem log/erro SQL ou stamp remoto, não
atribuir definitivamente a coluna ausente; HTTP 500 genérico não prova essa causa.

Solicitado ao dono executar manualmente `uv run alembic current` no ambiente
da API e informar somente a revisão (esperada b1c4e7a2d9f3). /build mostra a head
dos scripts da imagem, não a versão aplicada. Ferramentas de logs de serviço
allowlisted não estão expostas nesta sessão; nenhuma configuração/variável de
Easypanel, exec em container ou SQL remoto foi usado para contornar esse limite.

### Limpeza comprovada

Cancelamento da fixture pela API normal do hub respondeu HTTP 200, cancelled.
GET dos eventos Google na janela de teste respondeu HTTP 200, lista vazia.
Leitura interna independente comprovou depois cancelled e ausência de telefone.
Registro cancelado mantido para auditoria; sem purgar dados/histórico da conta.
Sem notificação de cancelamento por telefone, alteração de config, deploy,
migração, commit ou push. Confirmação de presença, rascunhos de edição,
cancelamento pelo cartão e aviso após edição continuam **não exercitados** em
produção, pois o servidor não consegue carregar as conversas completas.

## Reteste solicitado pelo dono — 18:06–18:26Z

Leitura de conversas voltou a funcionar: lista HTTP 200 (81 conversas), UUID
inexistente HTTP 404, id malformado HTTP 404. /build continua a8eaeb7bdb0a,
API/worker match, head embarcada b1c4e7a2d9f3. O agente não aplicou migração;
causa/correção do 500 anterior não foram confirmadas por logs/stamp remoto.

Nova fixture, mesmo cenário de consulta manual da equipe, sem professional_id,
sem appointment_type explícito (o produto usa Consulta), sem depósito/telefone,
16/10/2030 15:20–16:00. Slot previamente vazio, criação HTTP 201. Novo registro
de cleanup em live-fixture-retry.json. Nenhuma consulta anterior foi editada.

| Caso real | Resultado |
|---|---|
| Abrir/recarregar o link do e-mail com consulta futura | PASS: LEMBRE-SE canônico, 16/10/2030 15:20, equipe da clínica/Consulta; frase antiga ausente |
| Três controles reais do cartão inicial | PASS: Confirmar / Cancelar / Alterar Dados, botões visíveis e ids rem* registrados |
| Alterar Dados pela interface | PASS: cinco botões Mudar data / Mudar horário / Mudar serviço / Mudar médico / Outro |
| Outro na edição | PASS: Mudar convênio / Mudar paciente / Voltar |
| Mudar convênio, abrir lista | PASS: catálogo, Particular e Outro convênio disponíveis |
| Selecionar Particular e Responder pela interface | FAIL: seleção ins\|particular persistida; duas respostas, última oferta de atendente Sim/Não; nenhum resumo/Alterar Mais Dados |
| Mudar médico, abrir lista | PASS: dois profissionais, marcação de disponibilidade; médico vinculado ao destinatário autorizado identificado, selecionado pela interface |
| Trocar de médico quando o serviço atual não é oferecido | PASS parcial: pediu serviço do novo médico; não se aplicou alteração antecipada |
| Selecionar serviço oferecido e Responder pela interface | FAIL: svc\|Cirurgia de Catarata persistido; respostas de texto + oferta humana Sim/Não; sem resumo/Alterar Mais Dados |
| Confirmar presença | PASS: contador 1, status confirmed; menu Agendar / Outro exibido; evento ainda na mesma janela |
| Cancelar | PASS até a pergunta: Sim, cancelar / Manter consulta |
| Manter consulta pela interface | PASS preservação: consulta continuou confirmed e com data original; a resposta prevista é texto de manutenção, não menu obrigatório |
| Encerrar fixture pelo hub e limpar rascunho | PASS: cancelled, evento Google ausente; /menu pelo compositor retornou Agendar / Outro |

Não afirmar cancelamento final pelo cartão, edição final, Alterar Mais Dados,
troca de paciente, seleções de data/horário, entrega ao médico após edição ou
WhatsApp real aprovados. A jornada final de edição foi impedida pelos desvios
observados. Nenhum novo aviso ao médico foi disparado: não houve edição salva.

As escolhas de lista foram feitas nos controles reais do Portal. Algumas retomadas
de cartão e a confirmação de presença foram enviadas pela API interna legítima da
aplicação, usando título/id de opções realmente registradas nesta conversa e chave
de deduplicação de QA; não simularam confirmação direta no banco/Redis. Diferenciar
essa prova do worker/registro/Portal renderizado de um teste exclusivamente por
cliques. O limite de validade do Portal é de dez cartões recentes; tentativas
tardias de reutilizar o cartão inicial após muitos passos não contam como falha
do menu de horário/data. Foram interrompidas e a sessão restaurada a /menu.

Evidência sanitizada live-retry-results.json correlaciona cada escolha Particular/
serviço aos seus dois outbound subsequentes, com recap=false, has_edit_more=false
e human_offer=true no segundo. Causa desses desvios **ainda não diagnosticada**;
não atribuir a erro de parser, banco ou indisponibilidade Google sem prova adicional.
Como controle local, a rota pura com consulta sem médico/serviço explícitos e
Particular produz edit_confirm corretamente; ela não reproduziu o desvio do worker
real. Isso não aprova o comportamento em produção.

Limpeza pela API do hub HTTP 200 e cancelled; nova leitura Google HTTP 200 e zero
eventos na janela; leitura interna independente confirmou cancelled/data original.
Fixture sem telefone: sem aviso de cancelamento por WhatsApp. Consultas temporárias
ficam canceladas para auditoria, sem purgar histórico. Sem commit/push, configuração
ou deploy. As 647 aprovações automatizadas anteriores continuam sendo evidência
local; não foram repetidas nem usadas para suprimir os dois FAILs reais.

## Reenvio da simulação solicitado pelo dono — 18:56Z

Uma nova chamada explícita em 2026-10-08T18:56:46Z, para o mesmo destinatário
autorizado, respondeu HTTP 200, queued=true. Mesmo template de lembrete, conteúdo
fictício e link da conversa. Recibo inicial preservado em
production-enqueue-first-send.json; recibo novo em production-enqueue-result.json.
Sem criar/reabrir consulta de teste, alterar cadastro/configuração ou executar
deploy. Aceite da fila confirmado; entrega no inbox ainda não observada pelo agente.

## Teste do dono e novo envio — 19:07Z

O dono relatou ter visto Mudar data e a pergunta condicional sobre também mudar
o horário; após confirmar, não recebeu o novo e-mail ao médico. **Relato do dono,
sem verificação independente da gravação, fila, destinatário ou SMTP pelo agente.**
Não concluir ainda que o job não enfileirou ou que a alteração não foi salva.
O diagnóstico geral foi explicitamente deixado para depois dos outros testes.

Conforme solicitado, reenviada uma única simulação de lembrete com o mesmo link
em 2026-10-08T19:07:18Z: HTTP 200, queued=true, destinatário autorizado anterior.
Recibo do reenvio anterior preservado em production-enqueue-20261008_185646.json.
Sem criar/cancelar/editar consulta, reenfileirar aviso ao médico ou alterar qualquer
configuração nesta rodada. A simulação de lembrete e o aviso automático ao médico
depois de uma alteração confirmada são eventos distintos.

## Destinatário de paciente informado pelo dono — 19:33Z

O dono solicitou simulação para outro endereço, mesma clínica, consulta de
14/10/2026 11:00. Uma única consulta ativa nessa janela foi localizada pela API
interna e vinculada ao endereço autorizado pela autoridade de identidade,
sempre com escopo de tenant. Início 14:00Z, término 14:40Z, status scheduled.
Não foi criada consulta adicional. Datas/serviço do lembrete vêm da consulta
existente; a autoridade confirmou o destinatário novamente antes do envio.

O endereço informado pertence à **mesma identidade/conversa de Portal usada
nos testes anteriores**, não a um histórico novo. Em 19:22Z havia atividade
recente na conversa. Reabrir o link durante esta execução não produziu cartão
novo de 14/10 11:00. Não alterar/reiniciar o histórico para aparentar entrada nova.
Para o teste de entrada por outra sessão, orientar abertura em janela anônima e
autenticação normal com o endereço informado; preservar a verificação por código,
sem pedir senha/OTP no chat. Entrada recente e fluxo em andamento têm guardas de
silêncio; reenviar o e-mail genérico não enfileira o job de criação de cartão.

Simulação única de lembrete com link em 2026-10-08T19:33:03Z respondeu HTTP 200,
queued=true. Corpo verificado com 14/10/2026 11:00, link da clínica, sem placeholders
ou paciente fictício. Modelo fixo, sem LLM; identificação do profissional não
inferida da história: remetida a referência genérica à equipe da clínica. Esse
envio não é prova de execução do cron de lembretes nem do aviso profissional após
edição. Sem alteração de consulta, cadastro, configuração, migração ou deploy.
Entrega na caixa de entrada depende de confirmação do dono.

Artefatos separados em
`tasks/TASK-032/evidence/r6-email-simulation-2026-10-08-second-patient/`.
O helper recebeu modo explícito de consulta existente, com validação de destinatário,
status e data, para não reutilizar o exemplo anterior de 16/10 15:20. Nenhum ajuste
de código do produto foi feito nesta rodada. Diagnóstico geral continua adiado
conforme solicitação do dono.
