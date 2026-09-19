# CHECKPOINT — Estado da mensagem (enviado / entregue / lido / falhou), parte 1: secretarIA

Prompt: `z_prompts/PROMPT_BRAIN_MESSAGE_STATUS_ENTREGA_1_SECRETARIA.md` (raiz de BRAIN), Onda 3 ordem 1
de `z_prompts/PLANO_PORTAL_API_MVP.md`. Executado em 2026-09-19.

**Estado: BUILT, UNCOMMITTED, migração `8b4d2f6e1a37` NÃO aplicada em banco nenhum, não deployado.**

## 1. Causa (reconfirmada) — três pontos de descarte, não dois

1. **O fast-ACK jogava fora o recibo antes de enfileirar.** Isso não estava no prompt:
   `schemas/webhook.py::minimal_event_payload` reduzia o evento `messages` a `metadata`,
   `contacts` e `messages[]`, e **`statuses[]` nunca chegava ao Redis**. Mesmo com um ramo
   no worker, o recibo não teria nada para ler.
2. **O worker não tinha ramo para `value.statuses`** (`workers/tasks.py::process_webhook_event`),
   que era um `list[dict]` sem tipo.
3. **`Message` não tinha coluna de estado nenhuma**, e o frontend fixa `"enviado"` (partes 2/3).

## 2. O que mudou

| Onde | O quê |
|---|---|
| `models/message.py::Message` | `delivered_at`, `read_at`, `failed_at` (timestamptz NULL), `failure_reason` (VARCHAR 255 NULL), `updated_at` (NOT NULL, `server_default`/`onupdate` now()), índice `(conversation_id, updated_at)` |
| `models/message.py::status_of` | função pura: `failed_at` → `"falhou"` > `read_at` → `"lido"` > `delivered_at` → `"entregue"` > `"enviado"`. Nenhuma 5ª coluna |
| `migrations/versions/8b4d2f6e1a37_message_delivery_status.py` | aditiva + **backfill** `updated_at = created_at` (todas) e `delivered_at = created_at` (linhas `brain_message`) |
| `schemas/webhook.py` | `WebhookStatus`/`WebhookStatusError` tipados; `_minimal_status` passa `statuses` pelo fast-ACK com só `id`/`status`/`timestamp`/`errors[{code,title}]`, **sem `recipient_id`** |
| `services/message_status.py` (novo) | `apply_whatsapp_statuses`, `read_cutoff`, `mark_read`, `meta_timestamp`, `failure_reason` |
| `workers/tasks.py` | `_handle_message_statuses` no ramo `field == "messages"`; job `process_message_statuses` (uma única segunda tentativa adiada, de 30s); inbound `brain_message` nasce com `delivered_at = func.now()` |
| `workers/arq_worker.py` | registra `process_message_statuses` |
| `services/channel_sender.py::BrainMessageSender._record` | `delivered_at = func.now()` no mesmo INSERT |
| `schemas/conversation.py` | `MessageRead` + `status`/`delivered_at`/`read_at`/`failure_reason`/`updated_at`; `MessagesReadMark`, `MessagesReadResult` |
| `schemas/internal.py` | `BrainMessageMessage` + `status`/`delivered_at`/`read_at`/`updated_at`; `BrainMessageReadMark` |
| `api/internal.py` | cursor `since` passa a comparar `updated_at`; rota `POST /internal/brain-message/messages/read` |
| `api/hub/conversations.py` | `MessageRead` com estado; rota `POST /tenants/me/conversations/{id}/messages/read` |

## 3. Decisões (as do prompt, mais as tomadas aqui)

- **WhatsApp — escrita condicional, nunca ler-modificar-gravar.** Cada tipo de recibo é um
  `UPDATE ... WHERE <coluna> IS NULL`. Isso deixa tudo idempotente (a reentrega do Meta não
  casa com nenhuma linha) e monotônico: `"read"` preenche também `delivered_at` via
  `COALESCE`, então um `"delivered"` atrasado já encontra a coluna ocupada e não faz nada.
  Os timestamps usam o horário do Meta (`meta_timestamp`: epoch → UTC com fuso). `"sent"`
  não faz nada.
- **Escopo do recibo:** o match é feito por `wam_id` + `direction = outbound` + tenant dono do
  `phone_number_id` receptor, mesmo com o wamid sendo único no Meta. Recibo sem
  `phone_number_id` gera `worker_statuses_without_phone_number_id` (warning) e é
  descartado.
- **Recibo que ultrapassa a própria linha** (decisão desta sessão): a linha outbound só é gravada
  depois que a Graph API responde (`_record_outbound`), numa transação própria. Recibos sem
  linha correspondente ganham **uma** segunda tentativa, `process_message_statuses`, adiada
  30s. O que ainda não casar depois disso é mensagem que este serviço não gravou; sai
  `whatsapp_status_unmatched` (info, só a contagem).
- **Brain-Message: entrega imediata.** `delivered_at = func.now()` vai no mesmo INSERT de
  `created_at` (`server_default now()`), então os dois valores são idênticos, sem segunda
  escrita. Vale para os dois sentidos: a mensagem do paciente que chega ao secretarIA e a
  da clínica ou do bot gravada pelo `BrainMessageSender`. O inbound WhatsApp continua com NULL.
- **Leitura manual num paciente WhatsApp** (o prompt deixou "recusar ou ignorar" em aberto):
  **ignorar, de forma explícita**. A rota do staff devolve `200 {"marked": 0, "applied": false}`
  e não mexe em nada. Escolhi isso para o console poder chamar a rota em toda conversa
  aberta sem precisar saber o canal. Um 409 viraria erro de console a cada conversa WhatsApp.
  A rota interna nem alcança paciente WhatsApp: ela filtra por `channel = brain_message`, e
  um paciente desconhecido também dá `applied: false`, nunca 404.
- **Cursor de leitura:** exatamente um entre `up_to_message_id` (resolvido para o
  `created_at` dela, só dentro da mesma conversa) e `up_to` (instante **com offset**; um
  naive dá 422, nunca é adivinhado; valor no futuro é limitado a now). Um id desconhecido dá
  `marked: 0, applied: true`. A marcação ignora linhas já lidas ou `failed`.
- **Poll (decisão 6):** com `since`, a rota interna filtra `updated_at > since` e ordena por
  `(updated_at, id)`. Sem `since` (primeira carga), continua `created_at` (ordem de
  transcrição). A listagem do hub não tem `since`: sempre devolve a thread inteira, já com o
  estado atual.
- **Backfill `updated_at = created_at`** (decisão desta sessão): deixar o `now()` da migração em
  todas as linhas antigas faria o primeiro poll pós-deploy (cursor anterior à migração)
  receber de novo até uma página inteira do histórico. O Portal de hoje *acrescenta* o
  que recebe, então isso duplicaria bolhas.

## 4. Contrato para as partes 2 (brain-api) e 3 (frontend)

### 4.1 Campos novos (aditivos, com default) em cada mensagem

`GET /internal/brain-message/conversations/{external_id}/messages` (`BrainMessageMessage`):

```json
{ "status": "enviado|entregue|lido|falhou",
  "delivered_at": "…" , "read_at": "…" , "updated_at": "…" }
```

(`delivered_at`/`read_at` podem ser null.) `GET /tenants/me/conversations/{id}/messages`
(`MessageRead`, console) traz os mesmos campos e mais `failure_reason` (`"<código Meta>: <título>"`
ou null).

Do ponto de vista de quem desenha a bolha: o tique só faz sentido na **própria** mensagem. A
paciente vê o estado das mensagens `inbound`, e o staff vê o das `outbound`. `"enviando"`
continua sendo um estado só do cliente.

### 4.2 Poll — **MUDANÇA DE SEMÂNTICA DE `since`**

`since` agora significa "linhas **alteradas** estritamente depois de": linhas novas **e**
linhas cujo estado mudou. A mesma mensagem pode voltar várias vezes. **O consumidor tem que
fazer upsert por `id`, nunca append.** O próximo cursor é o maior `updated_at` recebido (e
não mais `created_at`).
Até a parte 2 chamar a rota de leitura, o comportamento é idêntico ao de antes: as linhas
`brain_message` nascem com `updated_at == created_at` e ninguém mais as altera.
Corrida residual, que já existia com `created_at`: o `now()` do Postgres é o instante de
início da transação, então uma transação longa que commita depois de um poll pode ficar com
`updated_at` abaixo do cursor. O desenho atual não cobre isso. Se virar sintoma, o
consumidor pode reenviar o poll com uma sobreposição de alguns segundos (o upsert por `id`
torna isso inofensivo).

### 4.3 Rotas de leitura

- `POST /internal/brain-message/messages/read` (`X-Internal-Api-Key`, chamada pelo brain-api
  quando o Portal **mostra** a conversa ao paciente). Corpo, `extra="forbid"`:
  `{"tenant_id", "external_id", "up_to_message_id"}` ou `{"tenant_id", "external_id", "up_to"}`.
  Marca como lidas as mensagens **outbound** (da clínica ou do bot) até o cursor.
  Resposta `{"marked": int, "applied": bool}`. Erros: 401/403 (chave), 422 (cursor ausente,
  duplo ou naive).
- `POST /tenants/me/conversations/{id}/messages/read` (hub-token, chamada pelo console quando o
  staff **vê** a conversa). Corpo `{"up_to_message_id"}` ou `{"up_to"}`. Marca as mensagens
  **inbound** (da paciente). Numa conversa WhatsApp, devolve `applied: false` sem alterar
  nada. Conversa de outro tenant dá 404.

Ambas são idempotentes e baratas: podem ser chamadas a cada poll que traga algo novo com a
tela visível.

## 5. Deploy (a migração vai PRIMEIRO)

1. `alembic upgrade head` (one-off, a partir da imagem nova), com os dois serviços ainda no
   código antigo. As colunas são inertes para ele: `updated_at` tem `server_default`.
2. Deploy de **`secretaria-worker` E `secretaria_api`**. O worker grava os recibos, o job novo
   e o inbound `brain_message`. A API serve os campos, recebe as marcações de leitura e move
   o cursor.
3. Sem nenhuma variável de ambiente nova.

Rollback: código antigo nos dois primeiro, depois `alembic downgrade 5e1f9a3c7d20`.

## 6. Validação

- `tests/test_message_delivery_status.py`: 29 testes. **Mutação:** voltar o cursor para
  `created_at` e retirar `statuses` do fast-ACK derruba 7 deles, entre eles o do cenário
  "poll com `since` antigo".
- Suíte completa: **2232 passed** (baseline do HEAD `b844245`: 2202, + 30 novos).
  `uv run ruff check` limpo em todos os `.py` tocados.
- Três testes antigos foram **atualizados de propósito** (a mudança deles é o fix, não uma
  regressão): `test_build_identity.py` (registro do worker, que agora tem 10 jobs) e
  `test_webhook_minimal_payload.py` (que fixava `"statuses" not in value`; agora fixa o
  recibo reduzido, sem `recipient_id` nem `pricing`).
- Revisão (`ecc:silent-failure-hunter`): **1 achado HIGH, corrigido.** O `mark_read` em lote
  carimba o mesmo `updated_at` em N linhas. Com `limit < N`, a página cortava o grupo e o
  cursor `>` estrito perdia o resto para sempre. Agora a página nunca termina no meio de um
  grupo de `updated_at` igual (pode passar do `limit` pelo tamanho do grupo). Teste:
  `test_a_page_never_cuts_a_bulk_read_group`, que falhou antes do fix. A comparação é
  coluna-a-coluna (subquery): no SQLite o datetime relido não é igual, por valor, ao que o
  `func.now()` gravou. Achado LOW, aceito: recibo sem linha correspondente e sem `redis`
  (só acontece fora do worker real) é descartado com contagem no log, sem retry.
- **Não provado:** a migração em Postgres real (a suíte roda em SQLite, com `create_all`) e o
  recibo real do Meta em produção.

## 7. Pendências

- Aplicar a migração em Postgres descartável antes do deploy (backfill + `NOT NULL`).
- Parte 2 (brain-api): repassar os campos, fazer o switchboard chamar a rota de leitura do
  paciente e mudar o cursor do Portal para upsert por `id`.
- Parte 3 (frontend): trocar os 4 `status: "enviado" as const` de `lib/real/mappers.ts` por
  `status` do wire e chamar a rota de leitura do staff.
- PreCheck fora do escopo (decisão 7).
- Não há "confirmação de leitura" do lado da clínica **para o paciente WhatsApp** (chamada
  `mark as read` na Graph API): é outra funcionalidade, fora deste prompt.
