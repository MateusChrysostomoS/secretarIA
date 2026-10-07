# CHECKPOINT — Portal: a conversa mostra as mensagens MAIS RECENTES (TASK-028)

Plano: `docs/superpowers/plans/2026-10-01-portal-mensagens-recentes.md`. Origem: `docs/LACUNAS_PORTAL_2026-10-01.md` §L1
(teste ao vivo de 2026-10-01). Executado em 2026-10-02.

**Estado: local + commitado, não deployado, não provado em produção.** Código: `8c68831` no branch
`task/TASK-028-portal-recentes` (base `916587e`), não mesclado em `main`, não pushado. Contrato escrito no brain-api:
`433998e` no mesmo branch (base `2768469`), `docs/PORTAL_MESSAGING_API.md` §4.1. A prova ao vivo está **PENDENTE** (§7) —
deploy não autorizado.

**A prova em produção ainda não está completa de ponta a ponta:** o ajuste de rolagem/poda do Portal foi implementado e
provado localmente na Task 0 do plano D / TASK-031 (2026-10-03), mas não deployado — ver "Dependência do front".

## Dependência do front — implementada e provada localmente, deploy pendente

Task 0 do plano D / TASK-031 concluída no Brain-Message-Frontend em 2026-10-03, no worktree
`C:/TECH/BRAIN-worktrees/TASK-031/Brain-Message-Frontend`, branch `task/TASK-031-digitando-frontend`, **LOCAL / UNCOMMITTED**.
Nenhuma alteração de código deste checkpoint/backend foi necessária nesta etapa.

1. `lib/messages.ts::scrollKey` e `PatientMessageList` agora rolam pelo ID da última mensagem, mesmo quando a janela
   continua com 50 linhas. Poll sem novidade mantém a posição de quem está lendo acima.
2. `lib/patient-portal.ts::afterSecretariaPoll`, usado no poll de `PortalConversation`, poda ecos locais confirmados via
   `unconfirmedLocal`; pendentes, falhos e ainda não confirmados sobrevivem. Avançar a janela não ressuscita bolha órfã.

Testes RED/GREEN: 11/11 focados; typecheck, suíte e build da Task 0 verdes. Tester independente usou telas reais com
fixtures locais sintéticas: janela de 50, novo ID visível sem rolar à mão, eco ausente após 55 mensagens novas, poll
repetido mantendo scrollTop=0 e envio único depois de F5/reabrir. Evidências: [relatório](C:/TECH/BRAIN/tasks/TASK-031/results/tester.md),
[janela](C:/TECH/BRAIN/tasks/TASK-031/evidence/task0-slide.png), [poda](C:/TECH/BRAIN/tasks/TASK-031/evidence/task0-pruned.png).

Fonte de verdade do front: `docs/CHECKPOINT_digitando.md` naquele worktree. O prompt
`z_prompts/PROMPT_PORTAL_SCROLL_E_COPIAS_LOCAIS_JANELA_RECENTE.md` foi **ABSORVIDO pela Task 0 do plano D / TASK-031**;
não é trabalho pendente a executar de novo. Deploy do front e prova com backend/LLM real continuam **PENDENTES / NOT AUTHORIZED** (§7).

## 1. Causa (L1)

`GET /internal/brain-message/conversations/{external_id}/messages` sem `since` ordenava por `created_at` crescente com
`LIMIT 50`: devolvia as 50 mensagens MAIS ANTIGAS. O Portal nunca manda `since` — a cada poll pede a conversa de novo, sem cursor
(`Brain-Message-Frontend/lib/real/patient-access.ts::pollMessages`) — então qualquer conversa com mais de 50 mensagens
congelava: as respostas novas existiam no banco e nunca chegavam à tela, e o que o paciente acabava de enviar só
existia no estado local do navegador (sumia ao recarregar).

## 2. O que mudou e onde

| Onde | O quê |
|---|---|
| `src/secretaria/api/internal.py::list_brain_message_messages` | Sem `since`: lê `limit + 1` linhas em `created_at DESC, id DESC`, devolve as `limit` mais novas em ordem crescente (`created_at`, `id`) e preenche `has_more`. Novo parâmetro `before` (datetime). `since` + `before` → 422, checado antes de qualquer acesso ao banco. Completa o grupo de empate (§3). O ramo `since` não mudou |
| `src/secretaria/schemas/internal.py::BrainMessageMessageList` | `has_more: bool = False` (aditivo, com default: quem não lê o campo continua funcionando) |
| `tests/test_brain_message_recent_page.py` (novo) | 10 testes: as 50 mais novas em ordem; página cheia exata sem `has_more` e uma a mais com `has_more`; `before` sem sobreposição; conversa vazia; `since` + `before` recusado; `since` com o comportamento antigo; paciente de WhatsApp inalcançável pela mesma string; 2 de empate na fronteira da página |
| brain-api `docs/PORTAL_MESSAGING_API.md` | §4.1 (comportamento novo), §8.5 (a frase "relê a conversa inteira" foi corrigida), nota datada no cabeçalho e entrada no changelog |

Esta tarefa backend não mudou código do brain-api nem do Brain-Message-Frontend; o ajuste consumidor foi feito depois
na Task 0 / TASK-031, conforme "Dependência do front". O brain-api repassa o corpo sem alterar:
`message_switchboard.list_messages` devolve o corpo da secretarIA por `_project_attachments`, que só reescreve
`data[].attachment`, e `RelayOut` é um envelope permissivo — então `has_more` chega ao navegador em `payload.has_more`
sem mudança lá. O brain-api não envia `before` e o Portal ainda não o usa.

## 3. O contrato (rota `GET /internal/brain-message/conversations/{external_id}/messages`)

- **Sem `since`** (primeira carga e todo poll do Portal): `data` = as `limit` mensagens MAIS NOVAS (`limit` padrão 50,
  máximo 200), em ordem cronológica crescente (`created_at`, depois `id`). `has_more` = existem mensagens mais antigas.
- **`before`** (opcional, ISO 8601): só mensagens com `created_at` estritamente anterior. Não combina com `since`
  (422). É um cursor **opaco**: o cliente devolve exatamente a string `created_at` que recebeu. Reformatar (por
  exemplo passando por um `Date` do JavaScript) corta microssegundos para milissegundos e pode pular linhas mais
  antigas criadas no mesmo milissegundo. Datetime sem fuso é aceito como em `since` (sem 422 extra).
- **Empate: a página pode passar de `limit`.** Uma página nunca termina DENTRO de um grupo de mensagens com o mesmo
  `created_at` (uma transação grava todas as suas linhas com o mesmo `now()` e o cursor `before` é estrito). Se o corte
  cai dentro de um grupo, a página é estendida com o resto do grupo e `has_more` é recalculado depois da extensão
  (o grupo pode ter sido o mais antigo da conversa). Logo `len(data)` pode exceder `limit` pelo tamanho desse grupo —
  quem consome não pode assumir `len(data) <= limit`.
- **`since` inalterado:** linhas que MUDARAM depois do cursor, ordenadas por `updated_at`; `has_more` é sempre `false`
  ali. Paciente ou conversa desconhecidos → `{"data": [], "has_more": false}`, nunca 404. Escopo (tenant + canal
  `brain_message` + `external_id`) inalterado.

## 4. Ordem de deploy

A unidade de deploy é o HEAD de `main` com este branch já mesclado, não só esta mudança. O código desta tarefa mora
apenas na rota da API, mas `main` também carrega a divisão dos workers da TASK-023; por isso o worker precisa ser
redeployado junto. Deployar **`secretaria_api` E `secretaria-worker` juntos** (README da secretarIA, "Deploy both
services, or neither"; `docs/superpowers/plans/2026-10-01-INDEX-execucao.md`, "Ordem de merge e de deploy"). Sem
migração. `GET /build` deve mostrar `deploy_parity: match`; `divergent` NÃO é ruído esperado — quer dizer que um dos
dois serviços ficou para trás, e isso se corrige antes de seguir. Com o brain-api não há nada a deployar (só texto);
com o frontend não há ordem a respeitar (o campo novo é aditivo e o comportamento sem `since` já é o que o Portal usa),
mas o ajuste de rolagem do front ("Dependência do front") é necessário para o paciente ver a correção inteira.

## 5. Como desfazer

`git revert 8c68831`. Nada depende de `has_more`/`before` ainda (o brain-api só repassa e o Portal não os lê), então
reverter volta ao comportamento antigo (as 50 mais antigas) sem quebrar ninguém.

## 6. Validação (evidência do implementador da Task 1; nada foi rerodado nesta rodada de documentação)

- `tests/test_brain_message_recent_page.py`: 10/10. Ciclo RED→GREEN registrado; desligar o ramo de empate faz falhar
  exatamente os 2 testes de empate.
- Arquivos vizinhos (pipeline, status de entrega, anexos, toque interativo, console do hub): 147 passed.
- Suíte inteira no worktree: **2806 passed, 10 skipped, 2 failed**. Os 2 são
  `tests/test_action_buttons.py::test_apptresched_without_deposit_and_flows_enters_manage_flow` e
  `::test_apptresched_enters_manage_flow_on_an_unconfigured_tenant`. Falham só porque um worktree git novo não tem
  `.env` ("Google Calendar credentials missing", `CalendarService._build_service`): falham de forma idêntica na base
  `916587e` e passam (6/6 em `-k apptresched`) no checkout principal, que tem o `.env`. **Não são regressões** e não
  tocam a rota de mensagens.
- `ruff check` e `ruff format --check` limpos nos 3 arquivos. Não houve execução contra Postgres (a suíte usa SQLite):
  a prova com banco real é a do §7.

## 7. Prova ao vivo — PENDENTE

Não feita: depende de deploy, e deploy exige autorização explícita do dono a cada ocasião (`AI_WORKFLOW.md`). Passos do
plano (Task 3), a executar depois da autorização:

1. Deployar o HEAD de `main` já com este branch: `secretaria_api` E `secretaria-worker` juntos, sem migração (§4).
   Conferir `GET /build` → `deploy_parity: match`.
2. Abrir o Portal com o agent-browser na conta de QA que tem a conversa longa (o código de acesso chega por e-mail).
   Mandar uma mensagem e esperar a resposta.
3. Critério de aprovação: a resposta nova ENTRA NA VISTA SEM o testador rolar a tela à mão, e recarregar a página (F5)
   mantém a mensagem enviada, sem duplicação/bolha órfã. **Esta prova só vale depois do deploy autorizado do ajuste da
   Task 0 / TASK-031 do front** ("Dependência do front"), já implementado e provado localmente. A prova sintética local
   não fecha este passo: ainda é necessário confirmar o comportamento com a conta QA e resposta LLM em produção.
   Antes do front atualizado estar no ar, a API isolada pode ser provada parcialmente, mas não o resultado ao paciente.
4. Registrar aqui os prints/saída e a data. Só então este checkpoint pode dizer "provado em produção" — e, nesse
   momento, trocar "não deployado / não provado" pelo estado real em todos os lugares que o repetem: o cabeçalho e este
   §7; no brain-api, `docs/PORTAL_MESSAGING_API.md` (§4.1, bullet "State and deploy"; a entrada do changelog; a nota
   datada do cabeçalho); e a linha de ponteiro em `CLAUDE.md`.

Resultado: _(a preencher)_.

## 8. Não afeta o WhatsApp — por quê

L1 não existe no WhatsApp, e esta mudança não o toca (verificado no código em 2026-10-02):

- **Lista do console de staff** — `GET /tenants/me/conversations/{id}/messages` (`api/hub/conversations.py::list_messages`)
  lê TODAS as linhas da conversa em `created_at` crescente, sem limite: não há página que congele.
- **Histórico da LLM** — `ai/graph.py::_load_history` pega as `HISTORY_LIMIT` (30) mais RECENTES (`created_at` decrescente
  + limite, depois inverte). Já era "mais novas primeiro".
- **Webhook** — `api/webhook.py::receive_webhook` só LÊ a tabela de deduplicação (`_filter_new_event_ids`) e enfileira;
  sem fila responde 503 e uma falha ao enfileirar sobe como erro 5xx, nos dois casos a Meta reenvia. Nenhuma
  mensagem é descartada em silêncio por limite de página.
- **O paciente do WhatsApp** lê o próprio app do WhatsApp, não esta rota.

Adjacente, FORA desta tarefa:

- A lista do hub é ilimitada: para conversas gigantes é um assunto de desempenho, não de silêncio.
- As lacunas L3/L4/L5 de `docs/LACUNAS_PORTAL_2026-10-01.md` (LLM promete o que não pode, perguntas factuais sem fonte,
  rótulo de botão tratado como serviço) são neutras de canal — é a mesma LLM nos dois — e continuam registradas lá
  como decisões pendentes do dono.

## 9. Pendências e próximos passos

- **Limite que o paciente vê (dono decide; fora desta tarefa).** Mensagens MAIS ANTIGAS que as 50 mais novas deixam de
  ser alcançáveis pelo Portal: o brain-api não repassa `before` nem `limit` (`message_switchboard.list_messages` só
  repassa `tenant_id` e `since`) e o front não tem controle "ver anteriores". Ainda é estritamente melhor que antes (as
  respostas aparecem e a mensagem enviada sobrevive ao recarregar). Seguimento: o brain-api repassar `before`/`limit`
  (opacos, literais) + um controle no front.
- **Ajustes do front implementados/provados localmente** (rolagem pelo `id`; poda de cópias locais), Task 0 do plano D /
  TASK-031: ver "Dependência do front" e `Brain-Message-Frontend/docs/CHECKPOINT_digitando.md` no worktree TASK-031.
  Prompt `z_prompts/PROMPT_PORTAL_SCROLL_E_COPIAS_LOCAIS_JANELA_RECENTE.md` **ABSORVIDO**. Restam deploy autorizado e
  prova de produção (§7); mudanças frontend LOCAL / UNCOMMITTED.

Menores, adiadas da revisão:

- Aviso de depreciação `status.HTTP_422_UNPROCESSABLE_ENTITY`: o plano mandou usar a constante e ela é a de todas as
  rotas (18 usos em 8 arquivos). Trocar tudo de uma vez, em mudança própria.
- Lacunas de cobertura de teste: paciente sem conversa; caminhada de 3 páginas por `before` com `has_more` verdadeiro
  na do meio; empate atravessando a fronteira numa página de `before`.
- O Portal ainda não usa `before`: se um dia mostrar "ver mensagens anteriores", deve devolver `created_at` literal
  (§3) e tratar páginas maiores que `limit`.
