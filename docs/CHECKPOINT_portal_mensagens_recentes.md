# CHECKPOINT — Portal: a conversa mostra as mensagens MAIS RECENTES (TASK-028)

Plano: `docs/superpowers/plans/2026-10-01-portal-mensagens-recentes.md`. Origem: `docs/LACUNAS_PORTAL_2026-10-01.md` §L1
(teste ao vivo de 2026-10-01). Executado em 2026-10-02.

**Estado: local + commitado, não deployado, não provado em produção.** Código: `8c68831` no branch
`task/TASK-028-portal-recentes` (base `916587e`), não mesclado em `main`, não pushado. Contrato escrito no brain-api:
`433998e` no mesmo branch (base `2768469`), `docs/PORTAL_MESSAGING_API.md` §4.1. A prova ao vivo está **PENDENTE** (§7) —
deploy não autorizado.

## 1. Causa (L1)

`GET /internal/brain-message/conversations/{external_id}/messages` sem `since` ordenava por `created_at` crescente com
`LIMIT 50`: devolvia as 50 mensagens MAIS ANTIGAS. O Portal nunca manda `since` — a cada poll relê a conversa inteira
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

Nada mudou no brain-api (código) nem no Brain-Message-Frontend. O brain-api repassa o corpo sem alterar:
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

Só `secretaria_api` — a rota mora na API; o worker não foi tocado. Sem migração. O `GET /build` pode mostrar
`deploy_parity: divergent` porque o worker não mudou: é esperado, anotar. Não há ordem a respeitar com o brain-api nem
com o frontend (o campo novo é aditivo e o comportamento sem `since` já é o que o Portal usa).

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

1. Deployar `secretaria_api`. Conferir `GET /build`; `deploy_parity: divergent` é esperado (o worker não mudou).
2. Abrir o Portal com o agent-browser na conta de QA que tem a conversa longa (o código de acesso chega por e-mail).
   Mandar uma mensagem e esperar a resposta.
3. Esperado: a resposta nova aparece em até ~10 s; recarregar a página (F5) mantém a mensagem enviada.
4. Registrar aqui os prints/saída e a data. Só então este checkpoint pode dizer "provado em produção".

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

## 9. Pendências (menores, adiadas da revisão)

- Aviso de depreciação `status.HTTP_422_UNPROCESSABLE_ENTITY`: o plano mandou usar a constante e ela é a de todas as
  rotas (18 usos em 8 arquivos). Trocar tudo de uma vez, em mudança própria.
- Lacunas de cobertura de teste: paciente sem conversa; caminhada de 3 páginas por `before` com `has_more` verdadeiro
  na do meio; empate atravessando a fronteira numa página de `before`.
- O Portal ainda não usa `before`: se um dia mostrar "ver mensagens anteriores", deve devolver `created_at` literal
  (§3) e tratar páginas maiores que `limit`.
