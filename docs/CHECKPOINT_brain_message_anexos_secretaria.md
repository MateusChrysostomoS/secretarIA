# CHECKPOINT — Anexos no Portal (Brain-Message), parte 2: secretarIA (armazenamento, envio e recebimento)

**Estado (2026-09-18): BUILT — não commitado, não deployado. Deploy: NOT AUTHORIZED.**
Prompt: `z_prompts/PROMPT_BRAIN_MESSAGE_ANEXOS_SECRETARIA_2_SECRETARIA.md` (parte 2 de 3 da Onda 2
de `z_prompts/PLANO_PORTAL_API_MVP.md`). Base `main@9386f14`, checkout normal, 1 agente
(Implementer) + 2 revisores read-only. Contrato consumido:
`brain-api/docs/CHECKPOINT_brain_message_anexos.md` §4–§5 (parte 1, BUILT e não commitada).

## 1. Pré-requisito e causa reconfirmada

- **Pré-requisito (Onda 0) já estava no HEAD**: `9386f14` levou o console de staff para
  `_staff_sender` → `BrainMessageSender(author=HUMAN, session=...)`. Esta peça estende
  exatamente essa função — sem a Onda 0, o envio de staff a paciente do Portal nem passava pelo
  `ChannelSender`.
- **Causa confirmada no código**: `Message` sem coluna de anexo; `BrainMessageInbound` só JSON;
  nenhum boto3/R2 em `src/`. E, como no brain-api da parte 1, **`python-multipart` não estava
  instalado** — nenhuma rota do secretarIA conseguia ler `multipart/form-data`. Adicionados em
  `pyproject.toml` + `uv.lock`: `boto3>=1.36` (resolvido 1.43.97) e `python-multipart>=0.0.20`
  (resolvido 0.0.32, a mesma do brain-api). O Dockerfile usa `uv sync --frozen`, então a imagem pega.

## 2. O que entrou (âncoras estáveis)

| Arquivo | O quê |
|---|---|
| `core/attachments.py` (novo) | Cópia do contrato do brain-api: `MAX_ATTACHMENT_BYTES` (20 971 520), `sniff_kind`, `safe_filename`, `check_attachment`, `REFUSALS`/`AttachmentRefused` (+4 códigos só deste lado), `StoredAttachment`/`attachment_record`/`stored_attachment_or_none`, `attachment_body` (placeholder), `content_disposition`, `MEDIA_RESPONSE_HEADERS` |
| `services/media_storage.py` (novo) | Cliente R2 PRÓPRIO (boto3 lazy, SigV4, path-style, checksum `when_required`), `put_object`/`delete_object` em thread, `open_object` (presign local + stream httpx), `object_key_prefix`/`new_object_key`, `MediaStorageUnavailable`/`MediaObjectMissing` |
| `api/attachment_http.py` (novo) | `read_json_body` (422 idênticos aos do FastAPI), `read_attachment_form` (multipart com teto por `Content-Length` e em stream — `CappedReceive`), `checked_upload`, `stream_attachment`, `refusal` |
| `migrations/versions/5e1f9a3c7d20_message_attachment.py` (novo) | `messages.attachment JSON NULL` + índice parcial `ix_messages_attachment_created_at (created_at) WHERE attachment IS NOT NULL` |
| `models/message.py` | `Message.attachment` (`JSON(none_as_null=True)`) + o índice parcial em `__table_args__` |
| `schemas/conversation.py` | `AttachmentRead`, `attachment_read_or_none`, `MessageRead.attachment`, `MessageSendForm` |
| `schemas/internal.py` | `BrainMessageInboundForm` (`extra="forbid"`), `BrainMessageMessage.attachment` |
| `api/internal.py` | `brain_message_inbound` (JSON **ou** multipart), `_inbound_with_attachment`, `_attachment_gate`, `_enforce_attachment_quota`, `_inbound_attachment_bytes`, `get_brain_message_media` (nova), listagem com `attachment` |
| `api/hub/conversations.py` | `send_message` (JSON **ou** multipart), `_send_attachment`, `get_message_media` (nova), `_staff_sender` passa `media_key_prefix`, `MessageRead.attachment` |
| `services/channel_sender.py` | `MediaChannelSender` (sub-protocolo), `sender_sends_media`, `BrainMessageSender.send_media` + `media_key_prefix`, `_record(attachment=)` |
| `workers/tasks.py` | `process_brain_message_inbound(attachment=)` → `_persist_brain_message_inbound` → `_route_inbound_turn(attachment=)`; ramo do arquivo logo após o handover; `_ReplyContext.attachment_received`; `_handle_attachment_received`; `ATTACHMENT_RECEIVED_MESSAGE` |
| `config.py` | `ATTACHMENTS_R2_*` (4 + TTL), `ATTACHMENT_DAILY_BYTES_PER_PATIENT/_PER_TENANT` |
| `tests/test_brain_message_attachments.py` (novo) | 22 testes (§7) |

## 3. Contrato

### 3.1 `POST /internal/brain-message/inbound` — variante multipart (brain-api → secretarIA)

Exatamente o §4.1 da parte 1. Mesmo path da variante JSON, que continua idêntica (mesmo job, sem
argumento `attachment` — um worker antigo ainda roda). Campos de formulário: `tenant_id`,
`external_id`, `text` (legenda, opcional), `patient_name`, `interactive_reply_id` (opcionais);
parte `file`. Campo desconhecido → 422 `extra_forbidden` (estrito de propósito: encoding novo,
deriva vira erro claro). **`dedupe_id` não existe nesta variante** (o §4.1 não o manda; ver §4.13)
→ 422 em `["body", "dedupe_id"]`. Sucesso: `202 {"status": "queued"}`.

Ordem (de custo): chave interna (dependência do router) → fila disponível (503) → corpo lido
DENTRO do handler, com teto → arquivo revalidado pelo conteúdo → portão numa leitura curta
(consentimento, cota) → **conexão do pool devolvida** → upload ao R2 → enqueue (se falhar, o
objeto é apagado). O job leva só a referência (`attachment` = registro abaixo).

### 3.2 Registro gravado (`messages.attachment`)

`{"r2_object_key", "content_type", "size_bytes", "filename"}`. A chave é
`brain-message/<tenant_id>/<patient_id>/<uuid hex>` — nunca o nome do arquivo (PII) — e **não sai
deste serviço**: toda projeção a descarta.

### 3.3 Listagens

`GET /internal/brain-message/conversations/{external_id}/messages` e
`GET /tenants/me/conversations/{id}/messages` ganham, por mensagem,
`attachment: {"content_type", "size_bytes", "filename"} | null` (aditivo, padrão `null`). Leitura
tolerante: um blob fora do contrato (tipo fora dos 5, tamanho fora de (0, 20 MiB]) vira `null` +
log `message_attachment_invalid`, sem derrubar a thread.

### 3.4 Mídia (bytes em stream, nunca URL)

- `GET /internal/brain-message/media/{message_id}?tenant_id=&external_id=` (chave interna) — a
  checagem de dono vive AQUI, numa consulta só por (id, tenant, `channel=brain_message`,
  `external_id`).
- `GET /tenants/me/conversations/{conversation_id}/messages/{message_id}/media` (hub-token) —
  uma consulta por (id, conversa, tenant do token). O console de staff fala direto com a
  secretarIA (via proxy `/api/*` do Brain-Message-Frontend), nunca pelo brain-api.
- Ambas: `404 attachment_not_found` **com o mesmo corpo** para "não existe", "de outro
  paciente/clínica/conversa", "mensagem sem arquivo" e id malformado; `503
  attachment_storage_unavailable` para o R2. Headers: `Content-Type` = o tipo GRAVADO (um dos 5),
  `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'; sandbox`,
  `Cache-Control: private, no-store`, `Cross-Origin-Resource-Policy: same-origin`,
  `Content-Disposition` `inline` (imagem) / `attachment` (PDF) com nome genérico `anexo.<ext>`.

### 3.5 Staff envia arquivo

`POST /tenants/me/conversations/{id}/messages` com `multipart/form-data`: parte `file` + `body`
opcional (legenda). Só paciente `channel="brain_message"`; paciente WhatsApp → `422
attachment_unsupported_for_channel`, nada gravado. Despacho por `BrainMessageSender.send_media`
(upload → linha `HUMAN`/`OUTBOUND` na sessão do request → handover `HUMAN_ACTIVE` → um commit).

### 3.6 Recusas (corpo `{"detail": {"code", "message"}}`, + `max_bytes` no 413)

| `code` | HTTP | Onde | Quando |
|---|---|---|---|
| `attachment_too_large` | 413 | ambos | arquivo > 20 971 520 bytes ou corpo acima do teto |
| `attachment_type_unsupported` | 415 | ambos | conteúdo não é JPEG/PNG/WEBP/GIF/PDF |
| `attachment_type_mismatch` | 422 | ambos | nome promete outra família, ou extensão fora da lista |
| `attachment_empty` | 422 | ambos | 0 bytes |
| `attachment_malformed` | 422 | ambos | sem `file`, dois arquivos, campo repetido, multipart quebrado |
| `attachment_consent_required` | **409** | inbound | paciente desconhecido ou sem `lgpd_accepted_at` |
| `attachment_quota_exceeded` | **429** | inbound | cota diária persistida (paciente ou clínica) |
| `attachment_unsupported_for_channel` | 422 | staff | paciente WhatsApp |
| `attachment_storage_unavailable` | **503** | todos | R2 sem configuração ou fora do ar (retentável) |
| `attachment_not_found` | 404 | mídia | qualquer motivo |

**Aviso ao brain-api (pedido pela parte 1, §4.1):** `attachment_consent_required` (409) e
`attachment_quota_exceeded` (429) são novos e o brain-api hoje os converteria em `502
product_error`. Ele precisa passar a repassá-los (mesmo corpo). O 503 já é repassado como 503.

## 4. Decisões tomadas sem consulta (e como reverter)

1. **Upload na API, não no worker.** O job arq leva a referência; 20 MiB nunca passam pelo
   Redis. Consequência: **só `secretaria_api` precisa das credenciais R2** — o worker só grava a
   linha que recebe. Reverter exigiria bytes na fila (não recomendado).
2. **`send_media` é capacidade, não 5º membro do `ChannelSender`.** O prompt dizia
   "`ChannelSender.send_media`", mas `test_whatsapp_client_satisfies_the_sender_protocol` prova a
   premissa da costura (o `WhatsAppClient` satisfaz o protocolo estruturalmente); pôr
   `send_media` no protocolo base exigiria um método no `WhatsAppClient` (proibido pela decisão 5)
   ou quebraria essa premissa. Ficou `MediaChannelSender(ChannelSender, Protocol)` +
   `sender_sends_media` (mesmo idioma de `sender_persists_outbound`). Nenhum método de mídia foi
   criado no `WhatsAppClient` (`services/whatsapp.py` não tem nenhum hoje).
3. **`body` de um arquivo**: a legenda, ou **`"[anexo: <nome>]"`** sem legenda
   (`core/attachments.py::attachment_body`). Nunca vazio: é a linha de histórico da LLM
   (`ai/graph.py::_load_history` já pula conteúdo vazio, mas então a LLM nem saberia que chegou
   um arquivo), o texto que uma tela antiga exibe, e o registro neutro de canal. Uma tela que
   desenha o arquivo pode omitir o texto quando `body == "[anexo: " + attachment.filename + "]"`.
   Relidos: `_load_history` (usa `inbound_routing_text(body)`, aceita qualquer texto), o
   redator de OTP em `_route_inbound_turn` (só age em `AWAITING_EMAIL_CODE` com 6 dígitos) e
   `extract_patient_name` — nenhum quebra com o placeholder.
4. **O bot só confirma**: `"Recebi seu arquivo, a equipe da clínica vai conferir."`
   (`workers/tasks.py::ATTACHMENT_RECEIVED_MESSAGE`), com o mesmo portão de entitlement de
   `_handle_service_unavailable`. O ramo fica **depois do handover** (humano no comando → só
   grava, bot calado) e **antes do portão de identidade** — senão, em `AWAITING_EMAIL_CODE`, o
   placeholder seria lido como código errado e derrubaria o estado para `IDLE`. `flow_state`
   fica intocado. Limitação consciente: a LEGENDA de um arquivo não é roteada ao fluxo/LLM (o
   paciente digita de novo se quiser agendar).
5. **Arquivo antes do aceite LGPD → `409 attachment_consent_required`, nada gravado** (a
   recomendação do §4.1 da parte 1). Paciente desconhecido também: com upload aberto a visitante
   não verificado, a primeira mensagem de alguém novo é texto, nunca arquivo.
6. **Cota persistida** (obrigação do §4.1): soma, das próprias linhas, dos bytes que PACIENTES
   gravaram nas últimas 24 h (janela móvel), por paciente (padrão **100 MiB**) e por clínica
   (padrão **1 GiB**) — `ATTACHMENT_DAILY_BYTES_PER_PATIENT/_PER_TENANT`, `0` desliga. Upload de
   staff não conta. Conta o que está PERSISTIDO: uploads concorrentes podem passar da cota pelo
   que está em voo (limitado pelos 10/min por paciente e 20/min por clínica do brain-api e pelos
   segundos de um job). O índice parcial mantém a soma da clínica barata.
7. **`JSON(none_as_null=True)`** — diferente de `interactive`: mensagem sem arquivo é SQL NULL,
   nunca JSON `'null'`, porque o índice e a cota filtram `attachment IS NOT NULL` (provado em
   `test_text_without_a_file_travels_exactly_as_before`).
8. **Download por presign local + stream httpx**: a URL assinada (TTL 60 s,
   `ATTACHMENTS_R2_SIGNED_URL_TTL_SECONDS`) nunca sai do processo; o chamador recebe bytes.
9. **Conexão do pool nunca presa durante I/O lento** (achado HIGH da parte 1): a rota interna
   fecha a sessão antes do upload; a do staff faz `commit()` (sem nada pendente) antes de ler o
   corpo — `expire_on_commit=False` em `core/database.py` mantém as linhas carregadas usáveis; as
   rotas de mídia fecham a sessão antes do stream.
10. **Teto de 1 MiB no corpo JSON** das duas rotas que passaram a ler o corpo à mão (antes: sem
    teto). Nenhuma mensagem real chega perto (texto do WhatsApp: 4096 caracteres).
11. **Checksums do botocore em `when_required`**: o padrão novo (≥ 1.36) manda CRC/aws-chunked
    em todo upload, a baixa documentada dos S3-compatíveis.
12. **Sem pacote compartilhado ainda** (`brain-shared-python-library`): um consumidor não é
    biblioteca. Quando o PreCheck adaptar (2º consumidor), o candidato a extrair é o cliente
    boto3 (`media_storage.py`) + o sniffing (`core/attachments.py`) — mesmo padrão de
    `pseudonymize-core`/`transcription-core`.
13. **Sem `dedupe_id` no multipart** (achado MEDIUM da revisão de segurança). O arquivo é gravado
    ANTES do enqueue; um `dedupe_id` repetido faria o job descartar a mensagem com o arquivo já no
    bucket — órfão silencioso. O §4.1 da parte 1 nem manda esse campo, então a variante multipart
    o recusa (422). E o job, nos três caminhos em que descarta o turno antes de gravar a linha
    (duplicata, tenant sumiu, corrida de integridade), registra `brain_message_attachment_discarded`
    com `reason` e `r2_object_key` (só ids, sem nome) — o worker não tem credencial do R2 (§4.1),
    então avisa em vez de apagar.
14. **Commit do staff que falha leva o upload junto** (achado LOW): `_send_attachment` apaga o
    objeto (best effort) antes de repropagar. Caso residual aceito: um COMMIT que efetivou mas
    voltou como erro (conexão caiu depois do commit no servidor) deixa a linha apontando para um
    objeto apagado → a rota de mídia responde 404 — mais raro e mais visível que um órfão mudo.
15. **Download sempre libera a conexão com o R2** (achado MEDIUM da revisão FastAPI): no Starlette
    1.0.1 com ASGI ≥ 2.4, o cliente que cai no meio do download vira `ClientDisconnect` ANTES do
    `background` rodar. `api/attachment_http.py::_MediaResponse` fecha a conexão num `finally`
    (blindado com `anyio.CancelScope(shield=True)`) em qualquer saída; e `open_object` fecha o
    cliente httpx também quando é cancelado com o GET em voo (`except BaseException`).

## 5. Pendências do DONO (variáveis de ambiente — nunca em `.env` local)

No EasyPanel, **só no `secretaria_api`** (o worker não toca o bucket):

| Variável | Valor |
|---|---|
| `ATTACHMENTS_R2_ACCOUNT_ID` | id da conta Cloudflare |
| `ATTACHMENTS_R2_ACCESS_KEY_ID` | access key de um token R2 **novo**, escopo *Object Read & Write* só neste bucket |
| `ATTACHMENTS_R2_SECRET_ACCESS_KEY` | secret desse token |
| `ATTACHMENTS_R2_BUCKET` | bucket **novo** (não o do PreCheck), privado |
| `ATTACHMENTS_R2_SIGNED_URL_TTL_SECONDS` | opcional (padrão 60) |
| `ATTACHMENT_DAILY_BYTES_PER_PATIENT` / `_PER_TENANT` | opcionais (padrões 104857600 / 1073741824) |

Faltando qualquer uma das quatro primeiras: todo upload e download responde `503
attachment_storage_unavailable` (falha fechada, nada gravado); texto não é afetado.

## 6. Ordem de deploy (`frozen-contract-migration`)

1. `alembic upgrade head` (aplica `5e1f9a3c7d20`) com a imagem NOVA, enquanto os dois serviços
   rodam a antiga — coluna e índice são inertes para o código velho.
2. `secretaria-worker` **e** `secretaria_api` (regra do repo: nunca só um) — o worker **antes ou
   junto**, nunca depois: o worker novo aceita jobs sem `attachment`, mas um worker VELHO recebendo
   o kwarg `attachment` de uma API nova falha o job com `TypeError` (arquivo perdido e objeto
   órfão). Na prática só acontece se o brain-api (parte 1) já estiver repassando uploads.
3. brain-api (parte 1) — ou ele antes com `PATIENT_ATTACHMENTS_ENABLED=false`.
4. Frontend (parte 3).
Rollback: código velho nos dois serviços primeiro, depois `alembic downgrade 4c8e2a7f1b93`.

## 7. Provas

### 7.1 Migração — Postgres 18 descartável (`initdb` no scratchpad, porta 5439)

```
== 1. upgrade head (from empty)       ... Running upgrade 4c8e2a7f1b93 -> 5e1f9a3c7d20
json | CREATE INDEX ix_messages_attachment_created_at ON public.messages USING btree (created_at) WHERE (attachment IS NOT NULL) | head=5e1f9a3c7d20
== 2. downgrade 4c8e2a7f1b93
NO COLUMN | NO INDEX | head=4c8e2a7f1b93
== 3. upgrade head again
json | CREATE INDEX ix_messages_attachment_created_at ... WHERE (attachment IS NOT NULL) | head=5e1f9a3c7d20
== 4. EXPLAIN do predicado da cota: Bitmap Heap Scan, Recheck Cond (created_at >= ...) AND (attachment IS NOT NULL)
```

### 7.2 Testes novos — `tests/test_brain_message_attachments.py` → **26 passed** (22 + 4 das revisões)

| Item do checklist §6 | Teste(s) |
|---|---|
| paciente envia anexo → persistido, confirmação genérica, `body` nunca vazio | `test_a_patient_file_is_stored_persisted_and_only_acknowledged`, `test_the_caption_is_the_body_and_the_listing_shows_the_file_never_its_key`, `test_with_a_human_in_charge_the_file_is_recorded_and_the_bot_stays_quiet` |
| staff envia anexo → `BrainMessageSender.send_media` (não `WhatsAppClient`), persistido, no histórico | `test_a_staff_file_goes_out_through_send_media_and_lands_in_both_transcripts` (spy no `send_media`; `WhatsAppClient` explode se construído), `test_a_file_for_a_whatsapp_patient_is_refused_and_nothing_is_stored`, `test_sending_a_file_is_a_capability_whatsapp_does_not_grow` |
| as duas rotas de mídia só servem com escopo; resto → 404 igual | `test_internal_media_is_served_only_inside_the_owners_conversation` (outro paciente, outra clínica, id inexistente, id malformado, mensagem sem arquivo: o MESMO corpo 404; sem chave → 401), `test_staff_media_is_scoped_to_the_hub_tenant_and_the_conversation` |
| tipo real ≠ extensão / acima do limite (MESMO número) → recusa clara, sem 5xx, nada no R2 | `test_a_file_that_is_not_what_it_claims_is_refused_and_nothing_is_stored` ×4, `test_the_ceiling_is_brain_apis_twenty_mib_inclusive` (20 971 520 aceito, +1 → 413, corpo acima do teto → 413), `test_a_malformed_or_unauthenticated_upload_is_refused_before_storage`, `test_a_file_before_the_lgpd_terms_is_refused_and_nothing_is_stored`, `test_the_daily_quota_is_persisted_per_patient_and_per_clinic`, `test_storage_trouble_is_a_retryable_503_and_a_failed_enqueue_cleans_up`, `test_the_limits_are_brain_apis_byte_for_byte` |
| mensagem sem anexo não regride | `test_text_without_a_file_travels_exactly_as_before` + `test_staff_text_without_a_file_is_unchanged` + as suítes `test_brain_message_pipeline.py`/`test_hub_conversations.py` inteiras (§7.3) |
| nenhum log com bytes, nome com PII, credencial | `test_no_log_line_carries_the_file_name_its_bytes_or_the_storage_secret` (loggers dos 6 módulos gravados diretamente — o structlog do app cacheia loggers, invisíveis ao `capture_logs` — + stdlib em INFO + stdout/stderr; inclui o `media_storage` REAL falhando com um `ClientError` que repete o segredo) |
| correções das revisões (§4.13–§4.15) | `test_a_dropped_download_still_releases_the_storage_connection` (ASGI 2.4, `send` falha no 2º pedaço → `ClientDisconnect` e a conexão liberada), `test_a_fetch_cancelled_in_flight_closes_its_client`, `test_a_file_the_worker_cannot_record_is_reported_with_its_key` (duplicata e tenant sumido: aviso com a chave; só a 1ª linha grava), `test_a_failed_staff_commit_removes_the_uploaded_file`, e `dedupe_id` no multipart → 422 dentro de `test_a_malformed_or_unauthenticated_upload_is_refused_before_storage` |
| cliente R2 mockado (nunca um bucket real) | `test_storage_uses_its_own_bucket_and_streams_back_without_handing_out_a_url` (boto3 stub + `httpx.MockTransport`: bucket e endpoint próprios, presign com o TTL, 404 → `MediaObjectMissing`, 500/rede → `MediaStorageUnavailable`, sem configuração → falha fechada sem construir cliente) |

### 7.3 Suíte completa e ruff

- Rodadas direcionadas: `test_brain_message_attachments` + `test_brain_message_pipeline` +
  `test_hub_conversations` → 81 passed + a correção do teste de log (22/22 depois).
- **Suíte completa** — `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest -q` (código final):
  **2198 passed, 0 failed** (2 min 55 s) = baseline 2176 da Onda 0 + os 22 novos.
- `uv run ruff check` nos 11 arquivos tocados/novos de `src/` + o teste → **All checks passed**,
  exceto `config.py:260` (E501), linha PRÉ-EXISTENTE e não tocada (um dos 8 erros de baseline do
  repo). `ruff format` não foi rodado em arquivos existentes (regra do repo).

### 7.4 Revisões (subagents read-only) e o que foi feito com cada achado

- `ecc:security-reviewer`: **nenhum CRITICAL/HIGH**. Confirmou 404 uniforme e dono numa consulta
  só nas duas rotas de mídia, nada lido antes da autenticação (inclusive corpo *chunked*),
  conexão do pool devolvida antes do I/O lento, headers endurecidos e sniff ancorado no offset 0.
  MEDIUM — órfão no R2 quando o job descarta um turno com arquivo (`dedupe_id` repetido /
  corrida): **corrigido** (§4.13). LOW — commit do staff falhando depois do upload: **corrigido**
  (§4.14).
- `ecc:fastapi-reviewer`: **nenhuma regressão** nos caminhos JSON (conferido contra o código do
  FastAPI 0.136.1: `strict_content_type` já é o padrão, os 422 batem), modelo = migração, cadeia
  linear de migrações, `extra="forbid"` em subclasse correto no Pydantic v2, WhatsApp intocado.
  MEDIUM — conexão com o R2 dependendo do GC no cancelamento e na queda do cliente no meio do
  download: **corrigido** (§4.15). Observação aceita: teto novo de 1 MiB no corpo JSON (§4.10).

### 7.5 Resultados finais (código final, depois das correções das revisões)

- `tests/test_brain_message_attachments.py` → **26 passed**; com `test_brain_message_pipeline`,
  `test_hub_conversations` e `test_internal` (as suítes que dividem estes arquivos) → **102 passed**.
- **Suíte completa** — `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest -q`: **2202 passed, 0 failed**
  (3 min 00 s) = baseline 2176 da Onda 0 + os 26 novos. (A rodada do §7.3, antes das correções
  das revisões, deu 2198 = 2176 + 22.)
- `ruff check` nos arquivos tocados → limpo (o único achado, `config.py:260` E501, é idêntico no
  blob do HEAD — pré-existente, linha não tocada); `ruff format --check` nos 4 arquivos novos →
  formatados.

## 8. Riscos residuais e pendências

- **Órfãos no bucket**: os caminhos conhecidos apagam o objeto (enqueue falho, linha falha em
  `send_media`, commit do staff falho) ou avisam com a chave (job que descarta o turno:
  `brain_message_attachment_discarded`). Sobra o job que QUEBRA no meio (ex.: banco fora) — o arq
  não repete, a mensagem se perde como uma de texto se perderia hoje, e o objeto fica. Mitigação
  futura: varredura no prefixo `brain-message/` comparando com `messages.attachment`.
- **LGPD — exclusão**: `api/internal_privacy.py::erase_subject` procura o titular por `wa_id` e
  não alcança pacientes `brain_message` (`wa_id` NULL) — lacuna PRÉ-EXISTENTE. Quando alcançar,
  apagar também o prefixo `brain-message/<tenant>/<patient>/` (a chave foi desenhada para isso).
- Sem antivírus: todo arquivo é tratado como não confiável (servido com `nosniff` + CSP
  `sandbox`; PDF só como download). Opção futura.
- A legenda de um arquivo não é roteada (decisão 4).
- **Achado PRÉ-EXISTENTE (não corrigido, fora do escopo):** `messages.interactive` usa
  `JSON` com `none_as_null=False` (padrão), e `BrainMessageSender._record` passa
  `interactive=None` explícito em toda bolha de texto — isso grava a string JSON `'null'`, não
  SQL NULL (provado nesta sessão: `raw rows: [('x', 'null', None), ...]`, `interactive IS NOT
  NULL: 1`). Consequência: em `workers/tasks.py::_validated_brain_message_reply_id`, o filtro
  `Message.interactive.is_not(None)` não exclui texto, e o `limit(BRAIN_MESSAGE_TAP_WINDOW)` (10)
  vira "as 10 últimas mensagens de saída" em vez de "os 10 últimos cartões" — um cartão seguido de
  10+ bolhas de texto teria o toque recusado (roteado como texto). Correção sugerida (prompt
  próprio): filtrar também `Message.interactive != JSON.NULL` (ou `none_as_null=True` + backfill
  `'null'` → NULL). É exatamente por isso que `messages.attachment` nasceu com
  `none_as_null=True` (decisão 7).
- Prova ponta a ponta contra o R2 REAL só depois do deploy autorizado, com um arquivo de teste.
- Skill `attachment-upload-relay` (a parte 1 adiou para cá): agora há as DUAS implementações de
  referência (brain-api `core/attachments.py` + `api/patient_access.py`; secretarIA os 3 módulos
  novos). Não criada nesta sessão — fica como próximo passo via `skill-creator`.
