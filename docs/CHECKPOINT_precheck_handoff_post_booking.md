# CHECKPOINT — PreCheck oferecido automaticamente logo depois do agendamento

> Feature checkpoint (padrão do `CLAUDE.md`). Esta é a **perna da secretarIA** de uma feature de 3
> repos — `brain-api` e `PreCheck` **não foram tocados** nesta rodada (ver "Fatia P1", abaixo).
> Data: 2026-08-25. Estado: **BUILT, verde, UNCOMMITTED, NÃO DEPLOYADO.**
> Origem: `z_prompts/debug_secretaria_producao/PROMPT_FEAT_36_PRECHECK_HANDOFF_POST_BOOKING.md`.

## O que existia, e o que faltava

A ponte para o PreCheck já estava inteira e é antiga: `services/precheck.py::request_precheck_handoff`
pede à brain-api (o hub) que o PreCheck pré-semeie a sessão do paciente, e falha fechado em
`UNAVAILABLE` em qualquer ambiguidade.

O que faltava era **o gatilho**. O único disparo era a tool `ai/tools.py::iniciar_pre_consulta`,
chamada quando a **LLM** decide, no meio da conversa, que é a hora. Quem agenda pelo **fluxo
determinístico** nunca fala com uma LLM — a confirmação (`services/flow_router.py`, "Pronto! Seu
agendamento está confirmado.") termina no link do Google Agenda e acaba ali. Para esse paciente a
pré-consulta simplesmente não existia.

## O hook

`plugins/precheck_handoff.py` — um `post_booking` a mais no registry, no molde de
`plugins/professional_notification.py`. Nada em `flow_router.py` foi tocado: os dois caminhos de
booking já convergem no mesmo job arq (`plugins/post_booking.py::run_post_booking_hooks`), então
um plugin alcança os dois de graça.

Ordem da execução, e o motivo de cada passo estar onde está:

1. checagens locais grátis (número da plataforma configurado, existe paciente, existe `wa_id`) —
   **antes** da reserva, para um no-op estrutural não queimar e devolver chave;
2. reserva no ledger (`ProcessedEvent`, chave `precheck:<appointment_id>`) — **antes** da chamada à
   brain-api, para duas varreduras concorrentes não semearem e mandarem duas vezes;
3. `request_precheck_handoff`;
4. mensagem, **somente** se o desfecho for `SEEDED` ou `ALREADY_ACTIVE`;
5. em qualquer caminho que termine sem mensagem, a chave é devolvida.

## `entitlement_keys=()` é deliberado

O portão de verdade — "esta clínica pode usar o PreCheck?" — já mora na brain-api
(`api/internal.py`: `ent.status in ACTIVE_STATUSES and ent.products.precheck`) e volta como
`NOT_ENTITLED`. Um segundo portão local só poderia divergir do primeiro. O `()` continua
significando "enquanto a assinatura estiver ativa" (`registry._spec_enabled`), que é o único portão
que este módulo mantém.

Nota de campo: `tenants.precheck_enabled` **existe** no modelo da secretarIA, mas hoje só é lido
pela fleet view do admin (`api/admin/tenants.py` → `schemas/admin.py`). Não tem consumidor de
runtime — não é, e não virou, um gate.

## Silêncio é o modo de falha

Nada além de `SEEDED`/`ALREADY_ACTIVE` produz mensagem. A tool pode se explicar porque **foi
perguntada**; o hook não — ele dispara sozinho depois de um agendamento, e um paciente que não
esperava uma segunda mensagem não pode receber um pedido de desculpas por uma que não veio.
`NOT_ENTITLED`, `NO_CLINIC`, `CONFLICT` e `UNAVAILABLE` terminam iguais: sem mensagem, uma linha de
log.

## Idempotência — e por que não há job de retry

A reserva no `ProcessedEvent` é o que impede um retry do job arq de entregar o mesmo link duas
vezes. O `seed_handoff_session` já é idempotente **do lado do PreCheck** (a segunda chamada responde
`already_active`), mas o **envio** do WhatsApp não é.

Diferente do `professional_notification` (FIX 32), aqui **não** há job de retry próprio: o e-mail ao
médico é um compromisso que não pode evaporar num soluço de SMTP; um convite de pré-consulta é uma
oferta — a clínica continua com o paciente, a consulta continua marcada, e a tool antiga continua
alcançável. Falhou: devolve a chave, loga em WARNING, para. A chave é devolvida justamente porque
**existe** algo que reexecuta isto: o arq retentando o `run_post_booking_hooks` em volta.

## Ordem em relação à confirmação: prática, não garantida

A confirmação é enviada inline pelo turno do booking; o hook roda no job arq que aquele turno
enfileirou, que ainda precisa ser pego, carregar linhas e chamar a brain-api. Na prática chega
depois. O arq não **promete** isso — e a afirmação honesta é que duas bolhas na ordem trocada
seriam estranhas e inofensivas, não que a ordem esteja garantida. A ordem de registro também põe o
`pix_deposit` na frente (`plugins/__init__.py`): quem cobra sinal pede dinheiro antes de pedir o
questionário.

## LGPD

Nem telefone, nem nome, nem corpo da mensagem em log — só ids e uma string de desfecho.
`services/precheck.py` já loga o telefone como hash; este módulo simplesmente nunca tem motivo para
citar um. No caminho de falha do envio o log carrega `error_type`, nunca a exceção crua: um corpo de
erro da Meta devolve o número do destinatário e o texto da mensagem para dentro do log.

Há teste que **falha** se o telefone completo, o nome do paciente ou a URL vazarem para o corpo ou
para o log, em todos os desfechos.

## Testes

`tests/test_precheck_handoff_plugin.py` — 20 funções, 27 casos. Suíte cheia: **1798 passed**
(baseline antes desta rodada: 1771; a única falha, `test_human_backup_plugin.py::test_on_inbound_inside_hours_returns_false`,
é o flake conhecido de UTC entre 00:00 e 03:00, pré-existente).

Sem migração: `processed_events` e o modelo já existem.

## Pendências (nada disso é código)

1. **O GRANT do `precheckv2`** — `PreCheck/docs/migration_precheckv2_handoff_grant.sql` **não foi
   rodado**. Sem ele o `INSERT` em `sessions` falha, a brain-api devolve 502, a secretarIA lê
   `UNAVAILABLE` e o hook **não manda nada, em silêncio** — exatamente para o caso comum (paciente
   sem sessão ativa). É o bloqueio nº 1 desta feature.
   Procedimento seguro: `z_prompts/debug_secretaria_producao/PROMPT_INVESTIGATE_03_PRECHECK_PRODUCTION_GRANT.md`.
2. **Entitlement do tenant** na brain-api: `status` ativo **E** `products.precheck`.
3. **`Clinic.brain_tenant_id`** setado do lado do PreCheck, senão `404 no_clinic_for_tenant`.
4. **Envs**: `PRECHECK_WHATSAPP_NUMBER`, `BRAIN_API_BASE_URL`, `INTERNAL_API_KEY` (secretarIA);
   `PRECHECK_BASE_URL`, `PRECHECK_INTERNAL_TOKEN` (brain-api).
5. **Deploy dos DOIS serviços.** Este hook roda no fluxo pós-booking, ou seja **no worker**. Um
   worker atrasado faz a feature parecer quebrada sem ter bug nenhum — ver a seção de deploy do
   `CLAUDE.md`. Em 2026-08-25 o `GET /build` ao vivo respondia `deploy_parity: divergent`.

## Fatia P1 (contexto do paciente) — investigada, NÃO implementada

Proposta escrita em `z_prompts/debug_secretaria_producao/PROPOSTA_FEAT_36_P1_CONTEXTO_PACIENTE.md`,
aguardando decisão. Resumo do achado: o nome **não é** coluna de `sessions` — é a resposta da
pergunta nº 1, hardcoded no n8n, atrás do portão de LGPD. Levar o nome exige coluna nova em
`precheckv2` (DDL manual, sem Alembic), edição de condutor n8n e mudança de contrato nos 3 repos —
sendo que `PrecheckHandoffIn` da brain-api é `extra="forbid"`, então a ordem de deploy é requisito.
