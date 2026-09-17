# CHECKPOINT — e-mail + OTP inline no Brain-Message

Data: 2026-09-17  
Prompt: `z_prompts/PROMPT_BRAIN_MESSAGE_SECRETARIA_EMAIL_OTP_INLINE.md` (onda 2)  
Estado: **BUILT localmente, UNCOMMITTED, NÃO DEPLOYADO**

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
