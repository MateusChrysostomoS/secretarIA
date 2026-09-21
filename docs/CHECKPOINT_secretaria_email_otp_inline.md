# CHECKPOINT — e-mail + OTP inline no Brain-Message

> **EMENDA 2026-09-20 — a decisão "consulta antes do código" foi REVERTIDA pelo dono.**
> Para o canal `brain_message`, o código de 6 dígitos passou a ser um PORTÃO: a consulta só é
> criada (Google Calendar + `appointments`) depois do código verificado, e o horário fica
> reservado por 10 minutos enquanto isso. Ver `docs/CHECKPOINT_secretaria_booking_code_gate.md`.
> **Nada abaixo foi apagado**: a prova de produção de 17/09 continua sendo o registro de como o
> comportamento anterior funcionou e por que foi revertido. Onde este arquivo diz que o código é
> uma oferta e não uma trava, leia "era, até 2026-09-20".


Data: 2026-09-17  
Prompt: `z_prompts/PROMPT_BRAIN_MESSAGE_SECRETARIA_EMAIL_OTP_INLINE.md` (onda 2)  
Estado: **DEPLOYADO E PROVADO EM PRODUÇÃO** (2026-09-17) — ver a última seção

> **Continuação (TASK-003, 2026-09-19, BUILT e não deployado):** o aviso do código virou um card de
> 3 botões com o e-mail mascarado, a conversa passou a poder nascer sem inbound
> (`POST /internal/brain-message/open`) e o handoff do PreCheck ganhou um ramo do Portal — ver
> `docs/CHECKPOINT_portal_open_e_card_do_codigo.md`. A ordem fixada aqui não mudou.

## Resultado

O canal `brain_message` agora executa a ordem fechada pelo produto:

```
greeting sem LGPD
→ pergunta de e-mail
→ claim confirmado pelo brain-api
→ LGPD
→ fluxo de agendamento existente, sem alteração
→ consulta confirmada
→ código enviado por e-mail e solicitado no chat
→ conta ativada no código correto
```

O WhatsApp não entra em nenhuma dessas ramificações. Ele continua enviando greeting e LGPD na
ordem anterior, e o hook pós-agendamento retorna antes de rede, ledger ou envio.

## Contrato consumido do brain-api

A onda 1 originalmente publicava somente o claim do e-mail. A execução desta onda revelou que os
outros três endpoints imaginados pela implementação parcial não existiam. O contrato foi ampliado
primeiro no brain-api e está documentado em
`brain-api/docs/CHECKPOINT_portal_sessao_pendente.md` §12:

| Uso pela secretarIA | Endpoint interno | Corpo | Resultado consumido |
|---|---|---|---|
| decidir se pergunta e-mail | `POST /internal/brain-message/pending-identity` | `tenant_id`, `external_id` | `pending_unclaimed`, `pending_claimed`, `verified`, `unknown` |
| reivindicar endereço | `POST /internal/brain-message/pending-email` | `tenant_id`, `external_id`, `email` | `claimed` |
| enviar código após booking | `POST /internal/brain-message/pending-otp/request` | `tenant_id`, `external_id` | `sent`; `503` quando o e-mail não foi aceito |
| verificar resposta no chat | `POST /internal/brain-message/pending-otp/verify` | `tenant_id`, `external_id`, `code` | `verified` |

Todas usam `X-Internal-Api-Key`. Pedido e verificação não recebem e-mail; o brain-api lê o endereço
já reivindicado na visita. O código, o e-mail e credenciais nunca aparecem em logs. O OTP recebido
é salvo em `messages` como `[código oculto]`, embora o valor real continue disponível em memória
para a chamada de verificação.

Para a onda 3, o navegador usa `GET /patient-access/pending/status` para o estado tipado do composer
e `POST /patient-access/pending/complete` para trocar a visita já verificada por conta/cookie. A
secretarIA nunca transporta token de conta pelo transcript.

## Estados e saídas limitadas

Foram adicionados dois `FlowState` (a coluna já era `VARCHAR(32)`, sem enum nativo ou `CHECK`, então
não há migração neste repo):

- `AWAITING_EMAIL`: intercepta a próxima mensagem como endereço. Formato inválido re-pergunta;
  falha de claim mantém o estado e **não** avança para LGPD. A decisão de produto assumida é que o
  e-mail é bloqueante porque não foi autorizado um “pular por enquanto”.
- `AWAITING_EMAIL_CODE`: nasce somente depois de uma consulta já persistida. Seis dígitos são
  verificados; outro texto abandona o modo e segue no fluxo normal. Código errado mantém o estado;
  limite de tentativas e expiração continuam exclusivamente no brain-api.

Ambos têm teto de 60 minutos de silêncio (`pending_identity_ttl_minutes`). Ao estourar, o estado
ativo sai para `IDLE` e o mecanismo existente de reativação oferece “Sim/Não”:

- e-mail + “Sim”: volta a `AWAITING_EMAIL`; “Não”: pausa, sem liberar LGPD — uma mensagem futura
  consulta novamente o estado autoritativo do brain-api;
- código + “Sim”: pede um desafio novo e volta a `AWAITING_EMAIL_CODE`; “Não”: encerra a ativação
  local e afirma que a consulta continua marcada.

Nenhuma dessas saídas altera ou remove a consulta.

## Conta já verificada

`status=verified` pula tanto a pergunta de e-mail quanto a LGPD de conta. A secretarIA espelha esse
fato em `Patient.lgpd_accepted_at` e grava `ConsentEvent(kind="account_terms_verified")`, separado do
evento de clique local, antes de abrir o menu normal. Se o registro local falhar, o bypass falha
fechado e a LGPD é mostrada.

## Ponto exato pós-agendamento

O novo plugin core `plugins/pending_identity.py` usa o mesmo evento `post_booking` que já alimenta
`plugins/precheck_handoff.py`, mas permanece independente:

- ledger próprio: `ProcessedEvent("pending_identity:<appointment_id>")`;
- somente `Patient.channel == "brain_message"`;
- chama o pedido de OTP e só promete código quando recebe `sent`;
- grava `AWAITING_EMAIL_CODE` antes da mensagem, para a resposta imediata não cair no router normal;
- libera o ledger em falha para permitir retry do job.

O hook do PreCheck não foi alterado. `run_post_booking` continua executando todos os hooks sem
short-circuit; nos dados atuais eles são disjuntos por canal (PreCheck exige `wa_id`, identidade
inline exige `brain_message`). O teste de regressão executa os dois hooks sobre o mesmo tipo de
evento, uma vez por canal, e prova qual efeito permanece em cada caso.

## Arquivos principais

- `src/secretaria/services/pending_identity.py`: copy, parser de e-mail/código e cliente tipado.
- `src/secretaria/plugins/pending_identity.py`: trigger pós-booking e idempotência.
- `src/secretaria/models/conversation.py`: dois estados novos.
- `src/secretaria/services/flow_router.py`: TTL da identidade e proteção contra estado vazado.
- `src/secretaria/workers/tasks.py`: ordem e-mail/LGPD, interceptação do OTP, retomada e redaction.
- `tests/test_brain_message_email_otp_inline.py`: sintomas e não-regressões.

## Provas

```
uv run python -m pytest \
  tests/test_brain_message_email_otp_inline.py \
  tests/test_brain_message_pipeline.py -q
68 passed in 13.09s

uv run ruff check <8 arquivos Python tocados>
All checks passed!
```

O contrato produtor do brain-api também foi validado junto com o acesso existente:

```
uv run python -m pytest \
  tests/test_patient_pending_session.py \
  tests/test_patient_access.py -q
85 passed in 158.07s
```

Suíte completa, com a allowlist de Coexistence esvaziada somente no processo de teste (o `.env`
local contém números reais e, sem essa neutralização, 24 testes antigos são descartados antes do
código sob teste):

```
$env:BOT_ALLOWLIST_WA_IDS=' '; uv run python -m pytest -q
2163 passed, 10 warnings in 244.47s
```

Sem a neutralização, a primeira execução terminou em `24 failed, 2138 passed`: todas as falhas
registravam `worker_wa_id_not_allowlisted` ou eram efeitos diretos de o inbound ter sido descartado.
Não houve commit, push, deploy, migração remota nem escrita em produção.

## Ordem de rollout (não executada)

```
1. brain-api: alembic upgrade head (0022)
2. brain-api com os endpoints da §12
3. secretaria_api E secretaria-worker juntos
4. Brain-Message-Frontend (onda 3)
```

Subir a secretarIA antes do brain-api faria o probe cair no fallback de LGPD e impediria a ordem
nova; subir só a API sem o worker deixaria o comportamento conversacional antigo em produção.

---

## Rollout e prova em produção — 2026-09-17

Executado a partir de `z_prompts/PROMPT_BRAIN_MESSAGE_FECHAR_ONDA_2_ROLLOUT_E2E.md`.
Registro operacional completo em `tasks/TASK-002/TASK.md`.

### Revisões

| Commit | `source_fingerprint` (Linux/LF) | Papel |
|---|---|---|
| `2bda47b` | `8d7c6951e7fa` | onda 2 original |
| `b2f3055` | `4b6bd25a508d` | remove o gate `Tenant.is_active` do canal `brain_message` |
| `697c24a` | **`595b1f80df8c`** | corrige `appointments.phone` — **revisão em produção** |

O fingerprint é conferível localmente replicando `core/build_info.py` sobre os blobs do git (LF, não
a working tree Windows, que tem CRLF e produz outro hash):

```python
paths = sorted(p for p in git_ls_tree(rev, "src/secretaria") if p.endswith(".py"))
digest.update(relpath.encode()); digest.update(blob_bytes)
```

Produção em `/build`: API e worker ambos `595b1f80df8c`, `deploy_parity: match`,
`alembic_head: 4c8e2a7f1b93`.

### O defeito que o rollout expôs

`b2f3055` destravou o canal — `brain_message_bot_not_active` desapareceu, a `Conversation` passou a
ser criada e o worker a responder. O E2E então parou no `✅ Confirmar` do agendamento, com o
paciente recebendo `CALENDAR_UNAVAILABLE_MESSAGE` e a conversa indo para handover humano.

Não era o calendário. O evento **era criado no Google** e a gravação no banco é que falhava — o ramo
`tasks.py::_apply_flow_result` "`result.appointment is not None and not persisted`", que emite a
**mesma** mensagem ao paciente que `calendar_unavailable` e por isso é indistinguível de fora.
Causa: `patient_wa` cai para `reply.patient_ref` neste canal (`Patient.wa_id` é NULL por desenho),
e esse `patient_ref` é um UUID de 36 caracteres gravado em `appointments.phone`, que é
`VARCHAR(32)` desde `c4d8e2f1a5b6`. Postgres recusa, a transação inteira (estado do fluxo **e**
consulta) faz rollback, e o evento do Google fica órfão.

Pré-existente desde `d607eb5` (2026-06-09); esta onda apenas tornou o agendamento pelo Portal
alcançável pela primeira vez. O caminho do agente sempre esteve correto
(`ai/tools.py::_persist_appointment` usa `patient.wa_id`). Corrigido em `697c24a` resolvendo o
número de `Patient.wa_id` dentro da mesma transação. Todos os leitores de `Appointment.phone` já
toleravam NULL — dois deles já caíam de volta para `Patient.wa_id`.

A suíte não pegava porque os fixtures rodam em `sqlite+aiosqlite://`, que não impõe largura de
`VARCHAR`. Por isso `test_a_portal_booking_records_no_phone_number` asserta o **valor** gravado, não
que o INSERT passou.

### E2E na clínica QA (sessão isolada, `fetch` same-origin, navegador visível)

Visita nova criada depois do deploy de `697c24a`. Nenhum código, token, cookie ou endereço aparece
aqui; o OTP foi digitado pelo dono direto no navegador e nunca transitou pelo agente.

| # | Critério | Observado |
|---|---|---|
| 1 | visita `product="secretaria"` → `pending_unclaimed` | `200`, corpo só `{state}` |
| 2 | `GREETING_FRAME` sem LGPD + pergunta de e-mail | 3 mensagens, LGPD ausente |
| 3 | e-mail inválido re-pergunta, não libera LGPD | segue `pending_unclaimed` |
| 4 | e-mail válido → `pending_claimed`, só então LGPD | nessa ordem |
| 5 | aceite LGPD → agendamento normal | menu + fluxo determinístico |
| 6 | **consulta confirmada ANTES do código** | "Pronto! Seu agendamento está confirmado. ✅ … 30/09/2026 às 11:00", e só depois o aviso |
| 7 | aviso do código + `otp_sent` | `state=otp_sent` |
| 9 | código incorreto permite nova tentativa | "Esse código não confere…", `state` mantido |
| 10 | código correto ativa a conta | "✅ Tudo certo, sua conta está ativa!", `state=verified` |
| 11 | `POST /pending/complete` exatamente uma vez | 1ª `200` + cookie de conta; 2ª `401 Invalid or expired token` |
| 12 | conta ativa sem perder histórico nem trocar `patient_ref` | mesmo `patient_ref`; 26 mensagens antes e depois |
| 13 | transcript mostra `[código oculto]` | 2 ocorrências; varredura do histórico: nenhum grupo de 6 dígitos cru |
| 14 | F5/reabertura mantém estado e histórico | recarga real limpou a memória da página; `POST /patient-access/refresh` só com o cookie `__Host-` devolveu `200` e o mesmo `patient_ref`; 26 mensagens |
| 15 | `/build` em paridade no fingerprint novo | API e worker `595b1f80df8c`, `match` |

O passo 8 ("resposta fora do formato não é aceita como OTP") ficou coberto por teste em vez de
runtime: exercitá-lo ao vivo abandona `AWAITING_EMAIL_CODE` por desenho e descartaria o desafio
necessário para os passos 10-12. Cobertura: `test_parse_code_accepts_six_digits_and_nothing_else` e
`test_saying_something_else_leaves_the_code_state_immediately`, ambos verdes na revisão deployada.

### Não-regressões (§4) — por teste nomeado, 18 passed na revisão deployada

WhatsApp byte-a-byte inalterado; `Tenant.is_active` ainda respeitado no WhatsApp; WhatsApp não entra
na perna de identidade; PreCheck/handoff pós-agendamento independente; aviso de código no máximo uma
vez por consulta; TTL dos dois estados com retomada limitada sem tocar a consulta; falha do serviço
de e-mail não promete envio; contrato com o brain-api inalterado.

Suíte completa na revisão deployada, com `BOT_ALLOWLIST_WA_IDS` neutralizada: **2165 passed, 0
failed**. Sem a neutralização, as mesmas 24 falhas de sempre — confirmadas idênticas com e sem a
correção, logo pré-existentes de ambiente, não regressão.

### Limpeza e pendências

- A consulta QA de 30/09 foi cancelada pelo fluxo público de cancelamento, nunca por SQL.
- Ficaram **dois eventos órfãos** no Google Calendar da clínica QA, das tentativas feitas ANTES da
  correção (`Cirurgia de Catarata` 28/09/2026 15:20 e 29/09/2026 10:20). Não há consulta
  correspondente no banco, então o fluxo público não os alcança: precisam ser apagados na agenda.
  Duas conversas dessa clínica também ficaram em handover humano.
- **DEF-2, aberto, fora do escopo desta onda:** `_appt_summary` (`flow_router.py`) formata
  `start_at` cru do banco (UTC) com `strftime`, sem converter para o fuso da clínica, enquanto a
  confirmação do agendamento formata um datetime já no fuso do calendário. A mesma consulta foi
  anunciada ao paciente como "30/09/2026 às 11:00" no agendamento e "30/09/2026 às 14:00" na
  confirmação do cancelamento. Atinge também `_appt_row_label`, isto é, as linhas da lista de
  consultas do fluxo de gerenciar.
- **DEF-3, aberto:** `calendar_unavailable` e "gravou no Google mas não no banco" emitem a mesma
  mensagem ao paciente. São problemas distintos — um é transitório, o outro deixa um evento órfão —
  e a indistinguibilidade foi o que mais custou tempo neste diagnóstico.
