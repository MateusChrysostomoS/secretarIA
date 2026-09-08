# CHECKPOINT — mensagens do console de staff (hub/conversations)

**Estado:** BUILT, suíte completa verde (2014 passed, 0 failures). Uncommitted.
**Origem:** `z_prompts/PROMPT_BRAIN_MESSAGE_SECRETARIA_CONSOLE_STAFF.md` (prompt 4 de ~10 do
plano Brain-Message, gerado 2026-09-07). Independente dos outros 9 — não precisou de
migração de canal nem de conceito novo de identidade de paciente.
**Prompt:** `TECH/BRAIN/z_prompts/PROMPT_BRAIN_MESSAGE_SECRETARIA_CONSOLE_STAFF.md`

---

## 1. O que entrou

Dois endpoints novos em `api/hub/conversations.py`, mesmo router de
`GET /tenants/me/conversations` e `POST .../handover`:

- **`GET /tenants/me/conversations/{id}/messages`** — thread completa, mais antiga primeiro,
  escopada pelo tenant do JWT. Reaproveita `_get_conversation` (mesmo 404 "Conversation not
  found" tanto pra ID malformado quanto pra conversa de outro tenant — nunca vaza existência).
- **`POST /tenants/me/conversations/{id}/messages`** — staff envia uma mensagem. Persiste
  como `Message(direction=OUTBOUND, sender=MessageSender.HUMAN)` — a MESMA forma que
  `workers/tasks.py::_persist_human_echo` já usa pro echo do Coexistence — e ENTREGA de fato
  via `WhatsAppClient.for_tenant(...).send_text_message(...)` antes de persistir. Chama
  `HandoverManager.set_human_active` incondicionalmente, igual ao echo, pra nunca divergir de
  comportamento entre os dois caminhos (app do WhatsApp vs. console).

Zero mudança de schema/model, zero migração — só router + schemas Pydantic (`MessageRead`,
`MessageSend` em `schemas/conversation.py`) + testes.

## 2. Ordem de operações do POST — send-then-persist

Envia primeiro, persiste depois. Se o envio falhar (`TenantWhatsAppCredentialMissing` ou
`httpx.HTTPError`), a resposta é 502 e NADA é persistido — nem `Message`, nem handover. Isso é
o INVERSO do padrão do worker (`_send_bot_reply`: *"the message is already delivered; a
failure to record it must not crash the turn"* — lá o send já aconteceu antes e é
fire-and-forget, então uma falha ao *persistir* é só logada). Aqui é o oposto porque é uma
chamada HTTP síncrona com um humano esperando resposta: uma falha de *entrega* precisa ser
honesta com quem clicou "enviar", não silenciosamente engolida.

## 3. Decisões tomadas sem perguntar ao usuário (pedido explícito dele — argumento de cada uma)

**3.1 — Sem paginação em `GET .../messages`.** `list_conversations`, no mesmo arquivo, já
estabelece o precedente: *"No pagination — deliberately minimal for the dashboard"*. Não
existe hoje nenhum endpoint paginado em `api/hub/` pra copiar um padrão. Paginação seria
convenção nova pra um caso que a experiência de console provavelmente nunca estressa; se um
dia precisar, é aditivo (cursor por `created_at`) e não quebra o contrato atual.

**3.2 — Sem campo/dispatch de canal.** `PROMPT_BRAIN_MESSAGE_SECRETARIA_PIPELINE_CANAL.md`
(que introduziria `Conversation.channel`) **não rodou** — confirmado no `CLAUDE.md` deste
repo ("PENDENTES — nenhum executado ainda") e por grep: não existe `channel` em
`models/conversation.py` nem em nenhum lugar do código de mensageria (os únicos hits de
"channel" no repo são o Google Calendar push-notification channel — assunto sem relação
nenhuma). Não dá pra ramificar em cima de um campo que não existe. Em vez de simular uma
abstração de canal só pra ter uma abstração, o envio ficou isolado numa função pequena
(`_send_via_whatsapp`) — o seam que `PIPELINE_CANAL`, quando rodar, envolve com um
`if conversation.channel == ...`. Construir o dispatch agora seria abstração prematura sobre
um valor que só existe amanhã.

**3.3 — Status 502 (não 503/409/422) pra falha de entrega.** Duas causas colapsam no mesmo
código: credencial ausente (`TenantWhatsAppCredentialMissing`) e falha real de rede/API
(`httpx.HTTPError`). As duas são "não conseguimos entregar através do canal upstream" —
semântica de gateway. 503 ficou fora porque, neste caso, não é indisponibilidade de propósito
de uma funcionalidade nossa (esse é o sentido que o guard de `clinic_flows` usa no handoff do
PreCheck) — é uma falha ao falar com a Graph API da Meta.

**3.4 — `wam_id` extraído localmente, não importado.** `_extract_message_id`
(`services/whatsapp.py`) e `_extract_sent_wam_id` (`workers/tasks.py`) já são a MESMA função
de 4 linhas duplicada duas vezes no repo — ambas privadas (prefixo `_`) aos seus módulos.
Segui o mesmo precedente: uma terceira cópia local (`_extract_wam_id` em `conversations.py`)
em vez de importar um nome privado de outra camada — o que também inverteria a direção
`api/` → `services/`/`workers/` só pra economizar 4 linhas.

**3.5 — Sem status 201 pra criação.** `POST .../handover` (já existente, mesmo router)
devolve 200 com `response_model`. Segui a mesma convenção pro `POST .../messages` — dentro do
mesmo arquivo, consistência pesou mais que pureza REST.

## 4. O que NÃO mudou (fora de escopo, conforme o prompt)

- Nenhum endpoint `/internal/brain-message/*` (outro prompt).
- Nenhuma mudança em `models/patient.py` nem migração nova.
- Nada em `Brain-Message-Frontend` — consumidor futuro é
  `PROMPT_FABLE_BRAIN_MESSAGE_CONSOLE_REAL.md`.

## 5. `PIPELINE_CANAL` já tinha rodado quando cheguei aqui?

**Não.** Confirmado tanto pelo `CLAUDE.md` deste repo (lista as três — IDENTIDADE_PACIENTE,
CONSOLE_STAFF, PIPELINE_CANAL — como "PENDENTES — nenhum executado ainda") quanto por grep no
código. Não havia nenhuma abstração de "sender"/canal pra reaproveitar — ver §3.2 pro que isso
implicou no desenho do envio.

## 6. Testes

11 testes novos em `tests/test_hub_conversations.py` (antes: 12; agora: 23, todos verdes):

- GET: thread completa em ordem + vazia + 404 conversa de outro tenant + 404 UUID aleatório +
  401 sem token.
- POST: persiste como HUMAN + entrega de fato via `WhatsAppClient` (fake que grava
  `to`/`body`, prova a chamada real de entrega, não só a persistência) + assume o handover;
  404 conversa de outro tenant SEM nunca chamar o client (isolamento checado antes do envio);
  401 sem token; 422 corpo vazio; 502 com `_FailingWhatsAppClient` provando que uma falha de
  entrega não persiste nada; 502 SEM nenhum mock (tenant real sem `waba_token` em
  `tenant_credentials`, provando o fail-closed do PROMPT_FIX_21 nesse caminho novo também).

`ruff check` / `ruff format --check` limpos nos 3 arquivos tocados. Suíte completa do repo:
**2014 passed, 0 failures, 9 warnings pré-existentes (deprecation do FastAPI, não relacionados)**.

## 7. Arquivos tocados

- `src/secretaria/api/hub/conversations.py`
- `src/secretaria/schemas/conversation.py`
- `tests/test_hub_conversations.py`
- `docs/CHECKPOINT_console_staff_messages.md` (este arquivo)
- `CLAUDE.md` (ponteiro cruzado atualizado — ver §8)

Nada commitado.

## 8.1 Interação com `PROMPT_BRAIN_MESSAGE_SECRETARIA_IDENTIDADE_PACIENTE.md` (rodou em paralelo)

Enquanto esta sessão trabalhava, outra sessão executou `..._IDENTIDADE_PACIENTE.md` na MESMA
working tree (`models/patient.py`, migração `c7e1a4b9d0f3`, `docs/CHECKPOINT_patient_channel_identity.md`)
— `wa_id` virou nullable. O inventário deles (§"Inventário de `.wa_id`") já nomeia
`_send_via_whatsapp()` deste arquivo (e `_read_model()`, pré-existente) como pontos que
assumem `wa_id` não-nulo e QUEBRARIAM com um paciente sem telefone — e decide, deliberadamente,
NÃO corrigir nenhum dos dois ainda, porque nenhuma linha `channel='brain_message'` existe até
`PIPELINE_CANAL` ir ao ar. Concordo com essa decisão e não adicionei nenhum guard ad hoc aqui:
os dois sessions convergiram pro mesmo raciocínio de forma independente, e um patch avulso
agora colidiria com o que quer que `PIPELINE_CANAL` construa como dispatch real. A suíte
completa que rodei (2014 passed) já inclui os 7 testes novos deles
(`tests/test_patient_channel_identity.py`) — as duas mudanças foram verificadas juntas, no
mesmo estado atual do working tree, não em snapshots separados.

## 8. Pendências

- Commit (não pedido nesta sessão).
- Deploy (API — este router não é tocado pelo worker, então não exige redeploy do
  `secretaria-worker` pela regra de `CLAUDE.md`).
- `PROMPT_BRAIN_MESSAGE_SECRETARIA_IDENTIDADE_PACIENTE.md` e `..._PIPELINE_CANAL.md`
  continuam pendentes, não executados nesta sessão (fora de escopo deste prompt).
- Consumidor real (`PROMPT_FABLE_BRAIN_MESSAGE_CONSOLE_REAL.md`, no
  `Brain-Message-Frontend`) ainda não escrito.
