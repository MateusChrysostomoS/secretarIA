# CHECKPOINT — o código de 6 dígitos vira portão antes da consulta existir

**Data:** 2026-09-20 · **Origem:** `z_prompts/PROMPT_BRAIN_MESSAGE_OTP_PORTAO_ANTES_DA_CONSULTA.md`
(gerado 17/09, emendado 20/09) · **Estado: COMMITADO e PUSHADO em `main` (`b0c43a9`, docs no
commit seguinte). Migração `a7d2f4b9c013` NÃO aplicada em banco nenhum. NÃO deployado.**

Este documento **supera**, para o canal `brain_message`, a decisão registrada em
`CHECKPOINT_secretaria_email_otp_inline.md` ("a consulta é marcada de verdade ANTES do código";
"o código não é trava do agendamento"). A prova de produção de 17/09 naquele arquivo continua
válida como histórico — não a reescreva. O que mudou é a decisão de produto, tomada pelo dono
depois de ver o efeito colateral em produção.

## 1. O que passou a valer

```
antes:  escolhe horário → CONSULTA CRIADA (Google + appointments) → pede código
agora:  escolhe horário → RESERVA (booking_holds, 10 min) → pede código
                        → código verificado → CONSULTA CRIADA (Google + appointments)
```

A **ordem é a feature**: a linha no Postgres vem primeiro e o evento no Google vem por último.
A onda anterior fazia o inverso e produziu órfãos (evento no Google, nada no banco — seção
"Limpeza e pendências" do checkpoint da onda 2). Uma reserva abandonada não deixa nada para trás:
ela para de ser lida no instante em que `expires_at` passa.

**Expiração é regra de leitura, não job.** Nenhum cron, nenhum worker, nenhum sweeper precisa
rodar para o horário liberar. Todo leitor filtra `expires_at > now()`. `place_hold` apaga as
linhas vencidas do tenant de passagem, só para a tabela não crescer — nunca por correção.

Os 10 minutos são **de propósito** os mesmos do desafio OTP do brain-api
(`brain-api/docs/CHECKPOINT_portal_sessao_pendente.md` §5.4). Um relógio só: um código não pode
sobreviver ao horário que ele confirma, nem o contrário.

## 2. Peças

| Arquivo | Papel |
|---|---|
| `models/booking_hold.py` (novo) | tabela `booking_holds` — a reserva. Sem PII: ids, uma janela, um rótulo de serviço. |
| `migrations/versions/a7d2f4b9c013_booking_holds.py` (novo) | aditiva e reversível; nada existente é tocado. |
| `services/booking_hold.py` (novo) | `place_hold`/`held_windows`/`live_hold`/`latest_hold`/`release` + a classe `BookingGate`. |
| `services/flow_router.py` | `_handle_confirmation` pergunta ao gate **antes** de `create_event`; `_enter_slot_picker` esconde janelas reservadas; `route()` publica o gate em `_ACTIVE_GATE`. |
| `workers/tasks.py` | monta o `BookingGate` por turno; `_send_booking_gate_notice`; `_promote_booking_hold`; o ramo de re-prompt; o filtro da LLM. |
| `services/pending_identity.py` | as mensagens do portão (`BOOKING_GATE_SENTENCE`, `booking_gate_body`, `provider_link`, …). |
| `services/sensitive_claim_guard.py` (novo) | a proteção estrutural do Bug 1. |

### 2.1 `BookingGate` é **fail-open**, e isso não é descuido

brain-api fora do ar, visita já verificada (`NOT_PENDING`), visita sem e-mail (`NO_EMAIL`), escrita
da reserva falhando — **todos** resolvem para `commit`: o paciente fica com o agendamento, exatamente
como antes do portão existir. Um portão fail-closed transformaria uma queda do brain-api numa
clínica que não consegue marcar consulta, que é uma falha bem pior do que um paciente não
verificado segurando um horário real. O log `booking_gate_stood_down` é o que diz que o portão não
correu naquele agendamento.

### 2.2 Por que o gate viaja num `ContextVar`

`_ACTIVE_GATE` em `flow_router.py`, setado e derrubado só por `route()`, sempre em `try/finally`.
Dois motivos, em ordem de peso: (a) **todo** caminho de listagem precisa enxergar reservas, não só
o de agendamento — um horário que um paciente do Portal está segurando também tem que sumir da
tela de remarcar, e esses dois caminhos não compartilham cadeia de chamada; passar argumento
significaria lembrar de cada um deles. (b) `contextvars` é por task no asyncio, então duas
conversas roteadas ao mesmo tempo no mesmo worker nunca veem o gate uma da outra — um global de
módulo seria o bug que isso evita.

### 2.3 O que acontece com o card do TASK-003 e com o handoff do TASK-004

O card de 3 botões (`identity_back`/`identity_resend`/`identity_change_email`) **não foi recriado**:
é o mesmo, com os mesmos ids, o mesmo ramo do roteador e a mesma revalidação. Só o **corpo** mudou,
porque o corpo antigo abre dizendo "Sua consulta já está confirmada!" e nesse ponto isso é falso.
As duas redações convivem e estão documentadas lado a lado em `services/pending_identity.py`.

O handoff do PreCheck (TASK-004) **volta a ser genuinamente pós-booking**: ele dispara de
`enqueue_post_booking_hooks`, que agora é chamado de `_promote_booking_hold` — o primeiro instante
em que a consulta existe. O hook `plugins/pending_identity.py` também roda aí e **se auto-pula**,
porque o brain-api responde `NOT_PENDING` para quem acabou de verificar. Sem card duplicado, sem
mecanismo paralelo, sem idempotência nova: o ledger `ProcessedEvent` continua sendo o mesmo.

### 2.4 Bug 2 — sair da espera passou a depender de existir reserva

| situação | comportamento |
|---|---|
| texto fora de formato **com** reserva viva | re-manda o card dizendo quantos minutos restam; estado continua `AWAITING_EMAIL_CODE` |
| texto fora de formato **sem** reserva | comportamento anterior, intacto: cai para `IDLE` e o turno é roteado normalmente |

A conversa continua com saída limitada no tempo (invariante da skill `conversation-flow-state`):
o botão "⬅️ Voltar" sai em um toque e o piso de silêncio (`_expire_stale_pending_identity_state`)
continua expirando o estado pelo relógio.

### 2.5 Bug 1 — a LLM não pode anunciar o que nenhuma tool fez

Duas camadas, de propósito:

1. **Regra 5** no bloco REGRAS INEGOCIÁVEIS de `ai/prompts.py` — hardcoded, não configurável
   por clínica, como as outras quatro.
2. **`services/sensitive_claim_guard.py`** — filtro de saída, aplicado uma única vez, logo depois
   de `run_agent` e antes de qualquer envio. Uma afirmação de ação sensível só passa se a ação
   estiver em `proven_actions`, o conjunto do que uma **tool** concluiu naquele turno. Hoje o
   agente não tem nenhuma tool de identidade ou pagamento, então o conjunto é vazio e toda
   afirmação é bloqueada. É uma **catraca sobre evidência**, não uma blocklist de palavras: no dia
   em que a tool existir, ela passa a própria chave e a frase volta a ser dizível sem ninguém
   editar regex.

Um prompt é um pedido, e o modo de falha em questão é justamente o modelo fazer o que não foi
pedido — por isso a regra vem acompanhada do filtro, que não é pedido.

## 3. Prova (rodada 2026-09-20)

```
$ uv run python -m pytest tests/test_booking_code_gate.py -q -p no:randomly
...........................                                              [100%]
27 passed in 2.76s

$ uv run python -m pytest -q -p no:randomly
1 failed, 2328 passed, 10 warnings in 85.97s
FAILED tests/test_human_backup_plugin.py::test_on_inbound_inside_hours_returns_false
```

A única falha é o flake de fuso já conhecido (`secretaria-human-backup-utc-flake`, falha entre
00-03h UTC). **Provado pré-existente**: com `git stash push -- src/secretaria` (HEAD limpo) ela
falha igual. Baseline do HEAD era 2301 passed; os 27 novos fecham em 2328.

`uvx ruff check src/` termina com 5 achados, todos em arquivos não tocados por esta rodada
(`config.py`, `api/hub/__init__.py`, `models/appointment.py`, `models/pix_deposit.py`,
`services/patient_context.py`) — lint vermelho no HEAD, já registrado.

## 4. Decisões tomadas aqui (implementação, não produto)

- **"Abrir e-mail" é link, não quarto botão.** O card tem teto de 3 botões e um botão de URL
  exigiria render novo no `Brain-Message-Frontend`, que o item 4 do prompt põe explicitamente fora
  de escopo. O provedor é derivado do **domínio da máscara** que o brain-api devolve
  (`a***a@gmail.com`) — o endereço real nunca entra neste serviço. Cobertos: Gmail, Outlook/Hotmail/
  Live/MSN, Yahoo, iCloud/me/mac, UOL/BOL, Terra, Proton, Zoho. Domínio desconhecido ⇒ **sem link**,
  nunca `https://<dominio>` adivinhado (seria string influenciada pelo paciente em posição
  clicável).
- **Conflito de reserva é checado em Python, não por UNIQUE.** `professional_id` é nullable e no
  Postgres NULLs são distintos — um UNIQUE pararia de proteger justamente os tenants de um
  profissional só, que são a maioria.
- **Reserva não bloqueia o dia inteiro**: o filtro age em `list_free_slots` (a janela), não em
  `list_available_days` (o dia).
- **`phone` fica NULL** na consulta promovida: paciente do Portal não tem telefone, e gravar o
  `patient_ref` de 36 chars em `VARCHAR(32)` é o defeito que `697c24a` corrigiu.

## 5. Pendências

1. **Logo do provedor no botão** — precisa do `Brain-Message-Frontend`, fora de escopo aqui.
2. **Remarcar ainda pode cair num horário reservado.** `_enter_slot_picker` esconde a janela, mas o
   caminho de reschedule não passa pelo gate e não faz checagem de conflito no confirm — se a
   reserva for promovida depois, há sobreposição. Fechar antes do deploy ou registrar como risco
   aceito.
3. **Skill nova** ("confirmação de ação sensível exige tool call no mesmo turno") — pedida no item
   1.3 do prompt, não criada.
4. **Migração `a7d2f4b9c013` não rodou em banco nenhum.** Aplicar ANTES da API e do worker.
5. **Deploy: os DOIS serviços.** Isto toca `workers/` e `services/flow_router.py` — a regra
   obrigatória do `CLAUDE.md` se aplica.
6. **Commitado e pushado em `main` (`b0c43a9`), NÃO deployado.** Commit e push não movem os
   serviços: `secretaria_api` e `secretaria-worker` são deploy manual e separado.

## 6. Prova em produção 2026-09-21 — e o defeito que ela achou

O deploy foi confirmado pelos logs do worker: reinício às `01:59:48Z` com
`source_fingerprint=b55739fb8cfe`, API atrás às `02:00:11Z`, e
`deploy_sha_parity ... 'match'` às `02:07:00Z`. Os dois serviços no código novo.

O E2E foi feito pelo Portal da clínica QA (`9c4fa6a5-…`, visita de teste do
TASK-004), percorrendo serviço → médico → dia → horário → **Confirmar**.

**O portão não engatou.** A conversa respondeu `"Pronto! Seu agendamento está
confirmado. ✅"` e só então o cartão antigo do código — o comportamento
pré-portão. O log do turno (`02:26:09Z`) mostra `calendar_event_created` e
`booking_owner_resolved`, e **nenhuma linha `booking_gate_*`**: nem
`booking_gate_held`, nem `booking_gate_stood_down`, nem
`booking_gate_hold_failed`.

Causa raiz: `_run_flow` montava o `BookingGate` com `tenant_id=reply.tenant_id`.
`_ReplyContext.tenant_id` é preenchido só nas pernas de identidade e nos
degrades; o turno ORDINÁRIO — o que agenda — o deixa `None` (o
`_ReplyContext` terminal de `_route_inbound_turn`). `BookingGate.__init__`
trata tenant ausente como "não armado", então `_handle_confirmation` pulava o
ramo do portão.

**O que tornou isso invisível é o que torna o teste novo obrigatório: um gate
desarmado não emite log nenhum.** Produção ficou idêntica ao build pré-portão, e
o único sintoma era uma consulta que não deveria existir ainda.

Corrigido lendo o `tenant` que o próprio `_run_flow` já recebe.
`test_the_gate_arms_on_the_real_reply_path` fixa a fiação (verificado: falha com
o bug reintroduzido, passa com o fix). Os 27 testes anteriores passavam porque
todos construíam o `BookingGate` à mão, já com tenant — a fiação nunca tinha
sido exercitada.

Resíduo desta prova, a limpar: consulta `ac107f06-a039-4983-a052-e3b71a11e8ff`
(Cirurgia de Catarata, Dr. Diogo Raposo, 30/09/2026 16:00, evento Google
`1urlf006pl0faafgslmt9iqpb8`), com e-mail de aviso já enviado ao profissional.
