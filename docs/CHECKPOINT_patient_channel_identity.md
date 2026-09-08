# Identidade de paciente por canal — migração aditiva

**Revisão:** `c7e1a4b9d0f3` (down_revision `a1b2c3d4e5f6`) · **Data:** 2026-09-08
**Escopo:** só banco + modelo. Nenhum endpoint, worker ou plugin foi tocado.

## O que mudou

| | antes | depois |
|---|---|---|
| `patients.wa_id` | `NOT NULL` | **`NULL` permitido** |
| `patients.channel` | não existia | `VARCHAR(32) NOT NULL DEFAULT 'whatsapp'` |
| `patients.external_id` | não existia | `VARCHAR(64) NULL`, backfill `= wa_id` |
| unicidade | `uq_patients_tenant_wa_id` | **as duas**: a antiga + `uq_patients_tenant_channel_external_id` |

Identidade de paciente passa de `(tenant, wa_id)` para `(tenant, channel, external_id)`.
Para toda linha WhatsApp — passada e futura — `channel = 'whatsapp'` e
`external_id = wa_id`, então a chave nova é um **reetiquetamento** da antiga, não
uma chave diferente.

## Decisão 1 — a constraint antiga foi MANTIDA, não substituída

O prompt permitia substituir ou coexistir. Optei por **coexistir**, e não é
conservadorismo: substituir abriria um buraco real em produção.

`secretarIA` roda a API e o worker arq como **dois serviços EasyPanel com deploy
manual independente** (já divergiram em produção — ver memória
`secretaria-api-worker-deploy-parity`). Depois desta migração existe uma janela em
que o worker ainda roda o modelo pré-canal e insere paciente **sem `external_id`**.
`NULL` não prende constraint de unicidade (NULLs comparam distinto), então a
constraint nova **não** cobre essas linhas. Se a antiga tivesse sido derrubada,
nessa janela dois `Patient` para o mesmo telefone passariam sem barreira.

Custo de manter: zero para linhas WhatsApp (as duas constraints descrevem a mesma
tupla) e zero para linhas `brain_message` (`wa_id IS NULL` não colide — provado em
Postgres real e coberto por `test_two_wa_id_less_patients_do_not_collide`).

**Derrubar quando:** os dois serviços estiverem provados no código novo *e*
`SELECT count(*) FROM patients WHERE external_id IS NULL` = 0.

## Decisão 2 — `external_id` é NULLABLE, embora `NOT NULL` fosse o invariante melhor

`NOT NULL` seria mais forte e está errado hoje. Um `DEFAULT` no Postgres **não pode
referenciar outra coluna**, então não há como o banco derivar `external_id` de
`wa_id` sozinho. Com `NOT NULL`, todo `INSERT` vindo do worker ainda não
redeployado — que não mapeia a coluna — falharia, e o primeiro contato de **todas
as clínicas** cairia. Trocaríamos uma duplicata hipotética por uma queda certa.

Provado empiricamente: um `INSERT INTO patients (id, tenant_id, wa_id, name)` no
estilo do worker antigo entra sem erro depois da migração (grava
`channel='whatsapp'` pelo server_default e `external_id=NULL`).

**Apertar para `NOT NULL` na mesma rodada que derruba a constraint antiga**, depois
de um segundo backfill das linhas criadas nessa janela.

## Decisão 3 — o preenchimento automático é default de coluna, não mudança no call site

`external_id` usa um default sensível ao contexto (`_external_id_default`, em
`models/patient.py`) que espelha `wa_id`. Assim os dois call sites que constroem
`Patient` (`workers/tasks.py:669` e `:5069`) continuam passando apenas
`tenant_id`/`wa_id`/`name`, **byte a byte como antes**, e mesmo assim produzem
linha completa. A alternativa — editar os call sites — violaria o escopo deste
prompt e acoplaria a migração ao deploy do worker.

## Prova

Postgres 16 descartável, semeado com 5 pacientes **sob o schema antigo** (incluindo
o mesmo telefone em dois tenants), depois `alembic upgrade head`:

```
channel_null | external_id_null | total_rows          -> 0 | 0 | 5
external_id_diverge_de_wa_id | channel_nao_whatsapp   -> 0 | 0
dup_antiga_tenant_wa_id | dup_nova_tenant_channel_ext -> 0 | 0
```

Comportamento verificado no banco real: linha `brain_message` com `wa_id NULL`
entra; duas delas entram; `external_id` repetido no mesmo canal é **rejeitado**
pela constraint nova; `wa_id` repetido é **rejeitado** pela antiga; INSERT do
worker antigo entra. `downgrade` volta ao schema exato e **falha alto** se já
existir linha `brain_message` — recusa inventar telefone (rollback honesto é para
frente a partir daí).

Suíte: **2014 passed, 0 failed**. Os 7 testes novos foram provados por
neutralização (tirar o default quebra 3; tirar a constraint quebra 1).

## Inventário de `.wa_id` — para o próximo prompt (pipeline)

Referenciado por símbolo em `api/hub/conversations.py` de propósito: esse arquivo
estava sendo reescrito por outra sessão enquanto este doc era escrito (console
staff), então número de linha ali apodrece rápido.

Nenhum destes foi corrigido aqui, **de propósito**: não existe linha
`channel='brain_message'` até o pipeline entrar no ar, então todos continuam
corretos. Ordenado por risco.

### Assumem não-nulo e QUEBRAM com paciente sem telefone

| local | o quê |
|---|---|
| `schemas/internal.py:48` + `api/internal.py:122` | `InternalPatient.wa_id: str` **não-opcional**. `wa_id=None` vira `ValidationError` na resposta — o mais duro do repo |
| `api/hub/conversations.py` → `_send_via_whatsapp()` | `send_text_message(to=patient.wa_id)` — o próprio docstring diz ser "the one seam a future channel dispatch would branch on" |
| `plugins/reminders.py:214, 221, 239, 280, 285` | 5 envios `to=patient.wa_id`; o join em `:309` não filtra `wa_id IS NOT NULL` |
| `services/payments/deposit_lifecycle.py:194, 253` | envio e `create_customer(...)` no Asaas com o telefone |
| `api/hub/conversations.py` → `_read_model()` | `patient_wa_id=patient.wa_id` no schema de leitura |

### Já toleram `None` (nada a fazer)

| local | por quê |
|---|---|
| `plugins/precheck_handoff.py:211` | `(ctx.patient.wa_id or "").strip()` |
| `ai/tools.py:452, 942` | destino é `phone: str | None = None` |
| `api/hub/calendar.py:214` | mesma forma |
| `services/pii_pseudonymization.py:92` | `patient_phone` opcional |
| `services/payments/deposit_lifecycle.py:544, 581` | `... if patient is not None else None` |

### Não são `Patient.wa_id` (não confundir no grep)

`workers/tasks.py:516, 4792` são `contact.wa_id` do payload do webhook.
`api/internal_privacy.py:149, 226` são `ConsentEvent.wa_id`, coluna própria — e
`ConsentEvent` **não** ganhou canal: ele indexa por telefone e sobrevive à exclusão
do paciente por desenho (ver `models/consent_event.py`).

### Lookups por identidade — o coração do próximo prompt

`workers/tasks.py:664`, `:1713`, `:5065` e `api/internal_privacy.py:111, 185`
buscam `Patient.wa_id == wa_id`. São eles que precisam virar
`(channel, external_id)` para o Brain-Message achar o paciente certo.
