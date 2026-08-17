# CHECKPOINT — `/menu` rename + WABA fail-closed + LGPD logging

Round que juntou dois prompts de correção do debug de produção:
**PROMPT_FIX_18** (renomear o reset destrutivo, devolver `/menu` ao menu,
resolver os órfãos) e **PROMPT_FIX_21** (saída WhatsApp fail-closed por tenant
e observabilidade LGPD).

**Status:** BUILT + validado localmente. `uv run python -m pytest -q` →
**1224 passed** (baseline antes do round: 1123). `uv run ruff check .` →
**8 erros PREEXISTENTES**, nenhum novo. **Sem migração.** NÃO commitado, NÃO
deployado — aguardando autorização.

---

## Parte 1 — `/menu` deixou de ser destrutivo (PROMPT_FIX_18)

### O que era

`/menu`, `/reset`, `/recomecar`, `/recomeçar`, `/inicio` e `/início` eram
reconhecidos em `workers/tasks.py::is_menu_command` e despachados **antes** da
persistência e de todos os gates do turno, direto para `_handle_menu_command`,
que APAGAVA `Patient` + `Conversation` + `Message` + `Appointment` do número e
recriava paciente/conversa para simular primeiro contato. Sem autenticação, sem
allowlist, sem handover, sem entitlement — e disparado por palavras que um
paciente digita naturalmente.

### O que é agora

| Comando | Comportamento |
|---|---|
| `/menu`, `/reset`, `/recomecar`, `/recomeçar`, `/inicio`, `/início` | **Não destrutivo.** Volta ao menu principal. |
| `/dangerously-remove-context` | **Destrutivo, preservado**, com tratamento de órfãos + auditoria. |

- `is_menu_command` continua igual (case-insensitive, trim) mas agora só marca
  `_ReplyContext.menu_requested`.
- `is_remove_context_command` faz **casamento exato**
  (`body == REMOVE_CONTEXT_COMMAND`): sem aliases, sem case folding, sem trim,
  sem prefixo. A string literal É o mecanismo de segurança.

### Caminho do `/menu`

`_handle_patient_messages` não desvia mais. O comando flui por
`_persist_inbound_message` como qualquer mensagem, e só é reconhecido **depois**
do dedupe → tenant resolvido → **allowlist** → tenant ativo → **handover**.
Em `_send_bot_reply` ele é tratado depois do **entitlement** e dos **plugins**
(`run_on_inbound`), reusando `_handle_show_main_menu` — o mesmo seam da tool
`show_main_menu` do agente, agora com um parâmetro `source`
(`"command"` | `"agent_tool"` | `"sentinel_fallback"`).

Só o estado transitório muda (`flow_state` → MENU, `flow_step`/seleções limpos
por `_apply_flow_result`, gate `reactivation_origin` consumido). Paciente,
histórico, consultas, mensagens, consentimento, Pix e eventos Google ficam
intactos, com os mesmos ids.

**Handover ativo:** `/menu` é **registrado e IGNORADO** — decisão explícita.
Nunca é uma forma de o paciente retomar o bot por trás do humano.

### Órfãos — decisão tomada

Das três opções do prompt, foi escolhida a **segunda**: o reset **não apaga
consultas**.

Motivo: `appointments.google_event_id` é `NOT NULL`, ou seja, **toda** consulta
espelha um evento vivo no Google Calendar. Apagar a linha faria o banco dizer
"não há consulta" enquanto o Google continua mostrando — o médico aparecendo
para um horário que o sistema esqueceu. As outras opções ou fazem um comando de
chat mutar o Google (não atômico; falha parcial deixa a agenda meio cancelada),
ou mantêm a divergência.

Além disso é a política que o repo já adota no caminho LGPD autorizado
(`api/internal_privacy.py::erase_subject`, que anonimiza consultas em vez de
apagar) — consistência vale mais que novidade.

Concretamente, `_handle_remove_context_command`:

- **Consultas:** PRESERVADAS e DESTACADAS — `patient_id` e `conversation_id`
  viram NULL por `UPDATE` explícito (não por cascade do FK, para Postgres e o
  SQLite dos testes se comportarem igual). `phone`, `status`,
  `start_at`/`end_at` e `google_event_id` ficam intocados: a reserva continua um
  registro completo e acionável, e banco e Google continuam concordando.
- **Pix:** `pix_deposits.appointment_id` é `ON DELETE CASCADE` — apagar a
  consulta levaria junto o registro de um sinal PAGO. Como a consulta
  sobrevive, o depósito sobrevive; só o ponteiro `patient_id` é limpo.
  Retenção/reembolso continuam governados pelo lifecycle do depósito.
- **ConsentEvent:** nunca apagado.
- **Google/Asaas:** nenhuma chamada externa. Testado com fake que explode se
  `CalendarService` for construído.
- O que sobrou é **dito ao remetente** (`REMOVE_CONTEXT_PRESERVED_MESSAGE`), e
  só quando há algo a dizer.

Apagar de verdade continua sendo exclusividade do processo de privacidade
autorizado (`DELETE /internal/privacy/tenants/{id}/subjects/{wa_id}`).

### Gates do comando destrutivo

Rate limit (em `_handle_patient_messages`), resolução de tenant, **allowlist**
e — só para o envio — **entitlement**: mesma política dos demais turnos. O wipe
em si não é bloqueado por entitlement (é o operador agindo sobre os dados do
próprio tenant, já commitado e auditado); o *envio* é, porque custa dinheiro.

`tenant.is_active` **deliberadamente não é exigido**: é um flag de prontidão de
produto ("a clínica terminou o setup?"), não uma fronteira de segurança, e o
comando existe justamente para resetar contexto de teste num tenant ainda em
configuração. Desvio consciente do invariante genérico, anotado aqui.

**O comando continua sem autenticação forte** — decisão explícita do usuário
(2026-08-16) de preservar a funcionalidade. O que o torna defensável: a string
literal impossível de digitar por acidente, o escopo restrito aos dados do
próprio remetente dentro do próprio tenant, a preservação de reservas/sinal, e
o registro de auditoria durável.

### Auditoria

Todo disparo grava uma linha durável e sanitizada em `analytics_events`
(**sem migração** — tabela existente):

```
event_type = "context_removed"
payload    = {conversation_id (a APAGADA), replacement_conversation_id,
              patients, conversations, messages,
              appointments_preserved, deposits_preserved}
created_at = server default now()
```

Nunca o telefone, nunca o conteúdo apagado. A linha é invisível para o hub:
`api/hub/analytics.py` filtra estritamente por
`event_type == "appointment_booked"`, e a exportação LGPD nunca toca essa
tabela por construção.

### Observabilidade

`conversation_menu_requested` (tenant/conversation/source) e
`conversation_menu_rendered` (+ `handover`, `rendered`).
`conversation_context_removed` em WARNING com os mesmos campos do payload de
auditoria. O log `worker_menu_patient_deleted`, que registrava o `wa_id`
completo, foi removido.

---

## Parte 2 — WABA fail-closed (PROMPT_FIX_21)

### `services/whatsapp.py`

- `WhatsAppClient.__init__` agora exige `phone_number_id` **e** `access_token`
  explícitos (keyword-only). Não existe mais `WhatsAppClient()`.
- `for_tenant(tenant, token)` valida os dois e levanta
  **`TenantWhatsAppCredentialMissing`** (com os NOMES dos campos faltantes,
  nunca valores) **antes de qualquer HTTP**. Emite `whatsapp_credential_missing`
  antes de levantar, então o evento sai mesmo quando o caller deixa a exceção
  propagar.
- `for_dev_scaffold()` é o único caminho para o env global `META_*`, e tem que
  ser pedido pelo nome. Um teste faz walk na AST de `workers/tasks.py` e falha
  se ele (ou uma construção sem credenciais) reaparecer lá.

### Callers migrados (helper `workers/tasks.py::_tenant_client`)

Fail-closed com log, nunca fallback: `_dispatch_bubbles` (retorna 0),
`_send_greeting`, `_handle_action_button`, `_handle_greeting_button_unavailable`,
`_handle_calendar_unavailable` (o handover ainda acontece),
`_apply_deposit_awareness`, `transcribe_audio_message` e o novo
`_handle_service_unavailable`. `_send_simple_text` passou a **exigir** um client.
`plugins/human_backup.py` construía o client fora do `try` — movido para dentro,
senão a exceção escaparia para o job arq e reentraria em loop.

### Ordem dos gates corrigida

**Allowlist agora roda ANTES do gate de tenant ativo.** O ramo de tenant inativo
devolve um contexto `service_unavailable`, que É uma mensagem de saída —
avaliá-lo primeiro deixava um número fora da allowlist arrancar um envio durante
a janela restrita de Coexistence, só falando com um tenant inativo.

### `service_unavailable` com o remetente certo

Esse caminho não tem conversa (nenhuma é criada para tenant inativo), então
construía um `WhatsAppClient()` global. Agora o `tenant_id` viaja no
`_ReplyContext` e `_handle_service_unavailable` resolve tenant + token, aplica o
**mesmo gate de entitlement** do resto e envia nas credenciais do próprio tenant
— ou não envia.

### Áudio

`transcribe_audio_message` usava `waba_token or settings.META_ACCESS_TOKEN`.
O token do tenant autentica **tanto** o download do media no Graph **quanto** a
resposta de esclarecimento, então a ausência agora aborta o job antes de
qualquer gasto de STT.

---

## Parte 3 — LGPD nos logs e na fila

### Removido na origem

| Onde | Antes | Agora |
|---|---|---|
| `whatsapp.py::_post` | `response=exc.response.text`, `to=<telefone>` | `to_suffix` (4 dígitos), `status_code`, `status_class`, `meta_error_code` (só os inteiros `code`/`error_subcode`) |
| `whatsapp.py` eventos | `whatsapp_message_sent` / `whatsapp_send_http_error` / `whatsapp_send_connection_error` | `whatsapp_send_attempt` / `whatsapp_send_result` (+ `outcome`) |
| `tasks.py` rate limit (texto e áudio) e `_send_simple_text` | `wa_id=` / `to=` completos | `wa_id_suffix` / `to_suffix` |
| `ai/graph.py` | `rejected_body=reply[:500]` | `rejected_len`, `rejected_sha256` (12 hex), `reason` (índice do padrão) |

`error.message` e `error.error_user_msg` da Meta são deliberadamente
descartados: eles ecoam o telefone do destinatário e o nosso próprio texto.

### Redactor central (defesa em profundidade)

`core/logging.py::redact_secrets` ganhou `_PII_KEYS`, casado **exatamente**
(nunca por substring) para os identificadores operacionais sobreviverem —
`phone_number_id` é id opaco da Meta, e `wa_id_suffix`/`to_suffix`/`wa_id_sha256`
são as formas reduzidas sancionadas. Novo helper `wa_suffix()`.

### Job ARQ mínimo

`api/webhook.py` enfileirava o corpo Meta **completo** no Redis.
`schemas/webhook.py::minimal_event_payload` reduz para só o que o worker lê,
**com os mesmos nomes de chave** — o worker segue parseando com o
`WebhookPayload` inalterado. Some do Redis:

- `statuses[].recipient_id` (telefone completo em todo recibo de entrega);
- `metadata.display_phone_number` (o número da própria clínica);
- `smb_app_state_sync[].contact` (`full_name` + `phone_number` da agenda);
- `history[].threads` (o backlog de conversas);
- `context` (mensagem citada), `timestamp`, `messaging_product`, `to` de inbound.

Preservados: os ids de mensagem (chaves de idempotência), `from`, `type`,
`text.body`, `interactive.{button_reply,list_reply}.{id,title}`, `audio.id`,
`button.{payload,text}`, contatos (`wa_id` + `profile.name`), o `to` dos
**echoes** (que é o paciente), e o **tamanho** das listas
`history`/`errors`/`state_sync`. Novo evento `webhook_job_enqueued` com
`payload_bytes` vs `source_bytes`. HMAC, ACK rápido e dedupe inalterados.

---

## Arquivos

**src:** `workers/tasks.py`, `services/whatsapp.py`, `core/logging.py`,
`schemas/webhook.py`, `api/webhook.py`, `ai/graph.py`, `ai/tools.py`
(docstring), `plugins/human_backup.py`.

**tests:** `test_menu_command.py` (reescrito), `test_remove_context_command.py`
(novo), `test_waba_fail_closed.py` (novo), `test_webhook_minimal_payload.py`
(novo), `test_waba_encryption.py`, `test_audio_transcription.py`,
`test_bot_allowlist.py`, `test_bot_reply_gating.py`.

Dois testes existentes consagravam o comportamento antigo e foram
**invertidos**: `test_waba_encryption.py` (fallback do token global) e
`test_audio_transcription.py` (payload completo no job).

## Pendências

1. **Commit/push/deploy** — nada foi commitado.
2. **API e worker são serviços separados no EasyPanel, com deploy manual e sem
   auto-deploy.** Praticamente tudo aqui vive em `workers/tasks.py` — deployar
   só a API não entrega nada disto.
3. **Antes do rollout:** identificar tenants que hoje dependem do fallback
   global (por comportamento/config status, **nunca** por valores de
   credencial) — depois desta mudança eles ficam mudos com
   `whatsapp_credential_missing` em vez de enviar pelo WABA errado. É o
   comportamento desejado, mas precisa ser sabido antes, não descoberto em
   produção.
4. **`services/email.py` ainda loga `to=<e-mail do dono da clínica>`**
   (`calendar_alert_email_sent`, `human_backup_alert_email_sent`). Fora do
   escopo desta rodada por instrução explícita — o arquivo estava intocável.
   Fica anotado como resíduo.
5. **`wam_id` continua logado inteiro** em ~10 pontos do worker. É a chave de
   idempotência que operação usa, e o prompt não a sinalizou; vale saber que o
   formato wamid da Meta embute o número do destinatário em base64.
6. Smoke test com fixture local e, se autorizado, só número descartável.
