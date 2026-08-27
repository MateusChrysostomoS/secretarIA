# CHECKPOINT — PreCheck oferecido automaticamente logo depois do agendamento

> Feature checkpoint (padrão do `CLAUDE.md`). Perna da **secretarIA** de uma feature de 3 repos.
> Duas rodadas até aqui:
>
> - **P0, o gatilho automático** (2026-08-25) — commit `4c62021`. Origem:
>   `z_prompts/debug_secretaria_producao/PROMPT_FEAT_36_PRECHECK_HANDOFF_POST_BOOKING.md`.
> - **P1, o contexto do agendamento** (2026-08-26, FEAT 39) — nome do paciente + serviço
>   agendado passam a viajar no handoff. Estado: **BUILT, verde, UNCOMMITTED, NÃO DEPLOYADO.**
>   Ver "Fatia P1", no fim.

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

A FEAT 39 mexeu na premissa desse parágrafo sem mexer na conclusão: o módulo agora **envia** o
nome do paciente. Enviar e logar são atos diferentes, e só o primeiro mudou — nenhuma linha de
log ganhou o nome, nem aqui nem em `services/precheck.py`, inclusive nos caminhos de falha (que
carregam `error_type` / hash do telefone / status code e nada mais). A garantia deixou de valer
**por acidente** (o módulo não tinha o dado) e passou a valer **por teste**: a varredura de
vazamento agora cobre os seis desfechos e roda também no nível do serviço, sobre os quatro
caminhos de falha em que o log é mais tentador (exceção renderizada, JSON inválido, status
inesperado, erro de rede).

## Testes

`tests/test_precheck_handoff_plugin.py` — 25 funções, 35 casos (20/27 no P0).
`tests/test_precheck_handoff.py` — 24 funções, 34 casos, que é onde mora a forma do corpo HTTP.

Suíte cheia depois da FEAT 39: **1832 passed, 1 failed** — a falha é
`test_human_backup_plugin.py::test_on_inbound_inside_hours_returns_false`, o flake conhecido de
UTC entre 00:00 e 03:00 (a execução foi às 01:45 UTC), pré-existente e sem nenhuma referência a
`precheck`/`handoff` no arquivo.

Sem migração: `processed_events` e o modelo já existem.

## Pendências (nada disso é código)

1. ~~**O GRANT do `precheckv2`**~~ — **RESOLVIDO em 2026-08-26**: confirmado
   `SELECT`/`INSERT`/`UPDATE` em `precheckv2.sessions` para a role real lida de
   `PRECHECKV2_DATABASE_URL`. Era o bloqueio nº 1: sem ele o `INSERT` falhava, a brain-api
   devolvia 502, a secretarIA lia `UNAVAILABLE` e o hook não mandava nada **em silêncio** —
   exatamente no caso comum. Fica registrado porque é o modelo exato do modo de falha que a
   ordem de deploy da FEAT 39 existe para evitar.
2. **Entitlement do tenant** na brain-api: `status` ativo **E** `products.precheck`.
3. **`Clinic.brain_tenant_id`** setado do lado do PreCheck, senão `404 no_clinic_for_tenant`.
4. **Envs**: `PRECHECK_WHATSAPP_NUMBER`, `BRAIN_API_BASE_URL`, `INTERNAL_API_KEY` (secretarIA);
   `PRECHECK_BASE_URL`, `PRECHECK_INTERNAL_TOKEN` (brain-api).
5. **Deploy dos DOIS serviços.** Este hook roda no fluxo pós-booking, ou seja **no worker**. Um
   worker atrasado faz a feature parecer quebrada sem ter bug nenhum — ver a seção de deploy do
   `CLAUDE.md`. Em 2026-08-25 o `GET /build` ao vivo respondia `deploy_parity: divergent`.

## Fatia P1 (contexto do agendamento) — IMPLEMENTADA (FEAT 39, 2026-08-26)

A proposta original está em
`z_prompts/debug_secretaria_producao/PROPOSTA_FEAT_36_P1_CONTEXTO_PACIENTE.md`. O achado que a
motivou continua valendo: o nome **não é** coluna de `sessions` — é a resposta da pergunta nº 1,
hardcoded no n8n, atrás do portão de LGPD. Levar o nome exigiu coluna nova em `precheckv2` (DDL
manual, sem Alembic) e mudança de contrato nos 3 repos. Decisão do produto: nome **e** serviço
juntos, sem pedir confirmação, serviço **cru**.

### O que mudou nesta perna

`services/precheck.py::request_precheck_handoff` ganhou `patient_name` e `booked_service`, os
dois `str | None = None` e **keyword-only**. Keyword-only não é estilo: são dois `str | None`
adjacentes sem forma que os distinga, então uma troca posicional mandaria o nome do paciente
como o serviço agendado e nada em lugar nenhum levantaria.

Cada campo entra no corpo **só quando sobra texto depois do `.strip()`** — vazio e ausente
colapsam na mesma coisa, e a chave é **omitida**, nunca enviada como `null`. A consequência é o
ponto: uma chamada que não conhece nenhum dos dois (a tool `iniciar_pre_consulta`, um
agendamento sem tipo) produz **exatamente** o corpo de duas chaves de antes da FEAT 39. Há teste
fixando isso — `test_a_call_without_booking_context_sends_todays_exact_payload`.

`plugins/precheck_handoff.py::_post_booking` passa `ctx.patient.name` e
`ctx.appointment.appointment_type` **crus**, sem heurística. Não é preguiça: `Patient.name` quase
sempre é o nome de perfil do WhatsApp (`workers/tasks.py::_handle_patient_messages`, de
`contact.profile.name`), e não existe coluna hoje que separe isso de um nome digitado — qualquer
teste de "isso é um nome de verdade?" aqui só poderia chutar. `None` nos dois é o caso ordinário
(bloqueio de agenda, agendamento sem tipo), não um erro.

Limite de tamanho: os dois campos são `max_length=255` do lado da brain-api, e as duas colunas de
origem já cabem — `Patient.name` é `String(255)` e `Appointment.appointment_type` é `String(120)`.
Por isso **não** há truncamento nesta perna; se alguma dessas colunas crescer, este parágrafo
vira um bug.

### A ordem de deploy é a parte perigosa

`brain_api/src/brain_api/schemas/internal.py::PrecheckHandoffIn` é `extra="forbid"`. Um nome de
campo que ela ainda não conhece **não é ignorado**: 422 no corpo inteiro. E
`request_precheck_handoff` falha fechado em `UNAVAILABLE` para qualquer resposta que não seja
200/403/404/409 — ou seja, o 422 volta **indistinguível de uma queda**, e derrubaria o gatilho
automático do P0, que já está em produção e não usa nenhum dos dois campos novos. Ver a skill
`frozen-contract-migration`.

Ordem obrigatória: DDL → PreCheck (FEAT 37) → brain-api (FEAT 38) → **secretarIA (FEAT 39)**.

Verificado ao vivo em 2026-08-26, antes de liberar esta perna, pelo OpenAPI publicado de cada
serviço — não pelo código local:

- `https://precheckv2-precheck-api.cpux9k.easypanel.host/openapi.json` →
  `PrecheckHandoffRequest` tem `patient_name` e `booked_service`.
- `https://secretaria-brain-api.cpux9k.easypanel.host/openapi.json` → `PrecheckHandoffIn` tem os
  dois, com `additionalProperties: false` intacto (a mudança ampliou o conjunto de nomes
  conhecidos, não relaxou a validação).

### Deploy desta perna

O hook roda no **worker** (`plugins/post_booking.py::run_post_booking_hooks`). Vale a regra dos
dois serviços do `CLAUDE.md`: deployar só a `secretaria_api` não move este código. Confirme o
`secretaria-worker` à parte, e cheque `GET /build` → `deploy_parity`.
