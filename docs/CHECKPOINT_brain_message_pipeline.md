# Pipeline channel-neutro + endpoints internos do Brain-Message

**Data:** 2026-09-08 · **Escopo:** worker + API interna. Nenhuma migração, nenhuma tela.
**Estado:** BUILT, suíte completa verde (**2039 passed**, baseline do HEAD era 2014),
**uncommitted e não deployado**.

## A pergunta da §1 do prompt — respondida

> Dentro de `_send_bot_reply`, a gravação da `Message` de saída acontece ANTES/independente
> da chamada à Graph API, ou só depois de a chamada ter sucesso?

**Só depois, e derivada dela.** Nos três lugares que gravavam saída, a linha só é escrita
após um envio bem-sucedido, e o `wam_id` vem da RESPOSTA do envio:

| onde | envia | grava | guarda |
|---|---|---|---|
| `_dispatch_bubbles` | `tasks.py:3684` `result = await _send_bubble(...)` | `tasks.py:3703` | `except: break` antes de gravar |
| `_send_greeting` | `tasks.py:3820/3822` | `tasks.py:3836` | `except: ... return` (`:3833`) |
| `_send_consent_notice` | `tasks.py:3988` | `tasks.py:4009` | `except: ... return` |

(Linhas do estado PRÉ-refatoração, para conferência contra o `git stash`.)

Consequência direta no desenho: **"enviar por `brain_message`" NÃO pôde ser "só persistir,
sem chamada de rede"** de forma implícita. Pior: a maioria dos envios (resposta de botão de
ação, `service_unavailable`, `calendar_unavailable`, degrade de botão de saudação) **não
grava linha nenhuma** — no WhatsApp isso é só um buraco no histórico da LLM, mas no
Brain-Message seria a mensagem inteira, porque sem perna de rede a linha É a entrega.

Por isso o sender do Brain-Message **grava ele mesmo**, em todo envio, e os três sites acima
pulam a gravação deles quando o sender já gravou. É a única assimetria real entre os canais,
e ela tem nome: `persists_outbound` (`services/channel_sender.py`).

## O que entrou

- **`services/channel_sender.py`** (novo) — `ChannelSender` (Protocol) com os **mesmos quatro
  métodos e as mesmas assinaturas** de `WhatsAppClient`. Foi de propósito: `WhatsAppClient`
  satisfaz o protocolo **estruturalmente**, então os ~60 call sites de envio em `tasks.py`
  ficaram **inalterados, byte a byte**. `BrainMessageSender` grava a `Message` e devolve `{}`.
  `sender_persists_outbound()` lê a flag por `getattr` com default **False** — o default é a
  parte que carrega peso: False é o comportamento pré-refatoração, então qualquer dublê de
  teste antigo continua correto sem saber que a flag existe.
- **`workers/tasks.py`** — `_persist_inbound_message` virou **wrapper WhatsApp** (dedupe por
  `wam_id`, `phone_number_id`→tenant, allowlist de `wa_id`, `wa_id`→paciente) + núcleo
  `_route_inbound_turn(session, *, tenant, patient, patient_ref, channel, ...)`. A extração é
  **textual, não reescrita**: a ordem de precedência e todos os comentários que explicam cada
  gate vieram junto. `_ReplyContext.patient_wa_id` → **`patient_ref`** + campo novo `channel`.
  `_tenant_client` ganhou o irmão `_reply_sender(reply, tenant, waba_token)` — o **único**
  lugar do repo que ramifica por canal. `_record_outbound` unifica as três cópias da gravação.
  Novos: `_persist_brain_message_inbound` + job arq `process_brain_message_inbound`.
- **`api/internal.py`** — `POST /internal/brain-message/inbound` (202 + enfileira; 503 se a
  fila não existe, nunca 202 com nada enfileirado) e
  `GET /internal/brain-message/conversations/{external_id}/messages?tenant_id=&since=&limit=`.
  Os dois atrás do `require_internal_api_key` **já existente**, no router que já o aplica.
- **`schemas/internal.py`** — `BrainMessageInbound`/`Ack`/`Message`/`MessageList`, e a
  correção abaixo.

## A bomba-relógio que este round desarmou

`InternalPatient.wa_id` era **`str`, não-opcional** — o item nº 1 do inventário de risco em
`CHECKPOINT_patient_channel_identity.md`. O primeiro paciente `brain_message` de um tenant
(que é exatamente o que este prompt passa a criar) faria
`GET /internal/tenants/{id}/patients` levantar `ValidationError` e devolver **500**,
derrubando a lista de pacientes do portal do médico inteira daquela clínica — não só a linha.
Agora é `str | None`, e o DTO também expõe `channel`/`external_id`. Coberto por
`test_a_phoneless_patient_does_not_break_the_list`.

## Inventário de `.wa_id` do prompt anterior — o que foi de fato ajustado aqui

Do bloco "Assumem não-nulo e QUEBRAM com paciente sem telefone" de
`CHECKPOINT_patient_channel_identity.md`:

| local | status |
|---|---|
| `schemas/internal.py` + `api/internal.py` (`InternalPatient.wa_id: str`) | **CORRIGIDO** (acima) |
| `_ReplyContext.patient_wa_id` | **CORRIGIDO** — virou `patient_ref`, 21 leituras + 14 construções, mais 9 arquivos de teste |
| `api/hub/conversations.py` → `_send_via_whatsapp()` | **NÃO tocado** — é o console de STAFF (clínica respondendo), superfície do prompt anterior, não deste. Continua correto: ainda não há caminho que faça a staff responder a um paciente `brain_message` por ali. **Lacuna conhecida**, é o próximo item natural |
| `api/hub/conversations.py` → `_read_model()` (`patient_wa_id=`) | **NÃO tocado**, mesmo motivo; é campo JSON do hub, homônimo mas não relacionado |
| `plugins/reminders.py` (5 envios + join sem `wa_id IS NOT NULL`) | **NÃO tocado — fora de escopo por desenho**, ver abaixo |
| `services/payments/deposit_lifecycle.py` | **NÃO tocado** — Asaas exige telefone real; um paciente sem telefone não pode ter cobrança Pix hoje. Lacuna registrada |

Três armadilhas de nome idêntico que o rename **não** podia pegar, e não pegou (todas
verificadas): `_persist_human_echo(patient_wa_id=...)` é parâmetro de outra função;
`plugins/base.py::InboundContext.patient_wa_id` é outro dataclass (o rename cego pegou os
dois — a suíte apontou, e foram revertidos); `tests/test_hub_conversations.py` usa
`patient_wa_id` como campo JSON do hub.

## Reminders/HSM — lacuna conhecida, deliberada

`plugins/reminders.py` **não foi estendido**. Um template HSM existe para reabrir a janela de
24h do WhatsApp, que no Brain-Message não existe — não há o que portar, há o que **decidir**.
E há um obstáculo concreto: `BrainMessageSender.send_template` **recusa e não grava nada**,
de propósito. O processo tem só o NOME do template e as variáveis posicionais; o corpo é
aprovado pela Meta e renderizado por ela. Inventar um corpo aqui poria no console do paciente
um texto que a clínica nunca aprovou. Lembrete proativo por Brain-Message é provavelmente
**mais simples** que no WhatsApp (sem restrição de janela), mas precisa de uma fonte de
mensagem própria — prompt futuro, não este.

## Limitação conhecida do endpoint de leitura

`Message.created_at` é `server_default=func.now()`, que no Postgres é o **início da
transação**. Linhas gravadas na mesma transação empatam, e o desempate é `Message.id`, um
UUID4 — ou seja, **ordem indefinida entre linhas do mesmo instante**. Na prática cada
mensagem aqui abre a própria transação, então no Postgres elas diferem; no SQLite dos testes
o `CURRENT_TIMESTAMP` tem granularidade de **1 segundo** e as três empatam sempre. Por isso o
teste ponta a ponta compara **conjunto**, não sequência: fingir ordem com um índice seria um
teste instável escondendo um limite real. A correção honesta é uma coluna sequencial
monotônica — migração, escopo de outro round.

## Provas

- **Suíte completa: 2039 passed, 0 failed** (`uv run python -m pytest -q`, 85s). Baseline do
  HEAD antes desta sessão: 2014. Nenhuma regressão no caminho WhatsApp.
- `ruff check` limpo nos 6 arquivos tocados.
- **Paridade** (`test_both_wrappers_reach_the_same_decision`): o mesmo primeiro contato pelos
  dois wrappers devolve `_ReplyContext` idêntico campo a campo, exceto `channel`,
  `patient_ref` e `conversation_id` (pessoas diferentes). O teste **conta** quantos campos
  comparou e falha se forem menos de 8 — uma paridade vazia é o jeito óbvio desse teste
  apodrecer.
- **Ponta a ponta sem Graph API**: `WhatsAppClient` é substituído por um dublê cujo
  `for_tenant`/`for_dev_scaffold` **levantam `AssertionError`**. Uma única tentativa de
  construir cliente WhatsApp reprova o teste. O POST devolve 202, o job roda como o arq
  rodaria, e a resposta do bot aparece no GET.
- **Escopo**: `tenant` errado com `external_id` certo → lista vazia; `external_id` de outro
  paciente → lista vazia; `tenant_id` ausente → 422 (é escopo obrigatório, não filtro).
- **Auth fail-closed** nos dois endpoints, parametrizado: sem chave → 401, chave errada →
  401, servidor sem `INTERNAL_API_KEY` → 403. Mais `test_auth_runs_before_the_queue`: um POST
  não autenticado **não enfileira nada**.

### Testes de mutação — executados, e um deles corrigiu o próprio teste

1. **`/menu` deixa de interceptar** no núcleo (`if False and is_menu_command(body)`):
   **3 falhas**, incluindo `test_parity_holds_through_the_menu_branch`. Restaurado. A ordem de
   precedência extraída está de fato presa.
2. **Escopo de tenant removido da busca de paciente** no endpoint de leitura:
   **passou mesmo assim** — e isso foi um achado, não um alívio. O escopo é defesa em
   profundidade (paciente E conversa), e o segundo elo segurou. Mutação refeita removendo
   **os dois** elos: aí sim `test_read_is_scoped_to_the_tenant` falhou. Restaurado.

## Pendências

1. **Commit + deploy dos DOIS serviços.** Isto toca `workers/` — a regra do `CLAUDE.md` vale:
   `secretaria-worker` **precisa** ser deployado, não só `secretaria_api`. O job novo
   `process_brain_message_inbound` só existe no worker; a API enfileira para um nome que um
   worker antigo **não conhece** e o 202 viraria mentira.
2. Não há migração nesta rodada; a de `channel`/`external_id` (`c7e1a4b9d0f3`) continua sendo
   pré-requisito **em produção**.
3. Console de staff (`api/hub/conversations.py`) ainda responde só por WhatsApp.
4. Reminders proativos por Brain-Message, e Pix para paciente sem telefone (acima).
