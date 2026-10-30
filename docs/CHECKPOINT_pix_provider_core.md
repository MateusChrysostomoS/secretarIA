# CHECKPOINT — Núcleo de provedores de pagamento (TASK-016, fase F1 da TASK F)

**Data:** 2026-09-30 · **Origem:** `Brain-Message-Frontend/docs/superpowers/specs/2026-09-29-task-f-sinal-pix-confirmacao-design.md`
(§17/§17A) + plano `.../plans/2026-09-29-task-f1-pix-nucleo-provedores-asaas.md` ·
**Estado: BUILT no worktree `C:\TECH\BRAIN-worktrees\TASK-016\secretarIA` (branch
`task/TASK-016-pix-nucleo`), commitado em 2026-09-30 (`57959e3`) e mesclado em `feature/out-of-mvp`
(`8fda1b1`, NÃO em `main`: decisão do dono), sem push ou deploy; migração
`c8a5d3f1b7e2` NÃO aplicada em produção. Provas ao vivo no sandbox Asaas: **NÃO PROVADAS
(puladas: `no_sandbox_key`)**. Os scripts recusaram chamadas de rede sem
`ASAAS_SANDBOX_API_KEY`.**

> **Continuação (TASK-017, F2):** o modo `before` foi construído em cima desta fundação — ver
> `docs/CHECKPOINT_pix_before_timing.md`. O que mudou em código desta fase: `fulfill_paid_charge` ganhou
> o promotor real (o ramo `promotion_failed` da §1 foi substituído), o teste de migração e o de
> `test_payment_tasks.py` que fixavam o estado interino foram atualizados de propósito, e o worker
> ganhou um cron (`run_attention_alerts`).


Esta fase é a fundação de "sinal Pix por clínica, Asaas + Mercado Pago, antes ou depois de confirmar".
Ela **não muda o comportamento visível de nenhuma clínica**: o modo `after` (confirma e depois
cobra) continua o padrão e é o único que existe; o modo `before` é a F2. O que muda por baixo:
tudo passa por uma interface de provedor, o webhook ficou por clínica e durável, e **nenhum
caminho do sistema devolve dinheiro** (decisão D15).

## 1. O que entrou e onde

| Peça | Arquivo |
|---|---|
| Interface `PaymentProvider` + tipos (`NormalizedPaymentEvent`, `ChargeState`, ...) | `services/payments/provider.py` |
| Registro de provedores | `services/payments/registry.py` |
| Asaas atrás da interface (`cpfCnpj`, `get_charge`, `cancel_charge` idempotente, sem estorno) | `services/payments/asaas_provider.py`, `asaas.py` |
| Contas por clínica (decrypt único em `tenant_config.get_payment_account`) | `models/clinic_payment_account.py`, `services/payments/accounts.py` |
| Webhook por clínica, persistir antes do 200 | `api/webhook_payments.py`, `models/payment_webhook_event.py`, `services/payments/webhook_payload.py` |
| Núcleo de eventos (pago uma vez, `get_charge` como autoridade, valor/referência) | `services/payments/event_processor.py` |
| Itens de atenção (`open_attention_item`) | `models/payment_attention_item.py`, `services/payments/attention.py` |
| Pós-pagamento (`fulfill_paid_charge`) | `services/payments/fulfillment.py` |
| Jobs: expiração, reconciliação, varreduras, diário | `services/payments/jobs.py`, `workers/payments_cron.py`, `workers/payments_tasks.py` |
| Conexão da chave Asaas + sondas de permissão (D14) | `services/payments/asaas_connection.py`, `asaas_probes.py` |
| Endpoints do hub | `api/hub/payment_accounts.py`, `schemas/payment_accounts.py`, `services/payments/rate_limit.py` |
| Cancelamento sem estorno | `services/payments/deposit_lifecycle.py` (`on_appointment_cancelled`, `cancellation_notice`) |
| LGPD (erasure/export) | `api/internal_privacy.py`, `schemas/privacy.py` |
| Migração (aditiva) | `migrations/versions/c8a5d3f1b7e2_payment_provider_core.py` |

## 2. Decisões tomadas sem perguntar (com o porquê)

1. **Webhook persist-first é exceção só de pagamentos.** A regra "fast-ACK sem DB" da skill
   `whatsapp-webhook-arq` protege o orçamento de 5 s da Meta; os PSPs dão muito mais folga e o
   checklist do dono (autenticar e deduplicar ANTES de processar) exige saber a clínica. O handler
   faz um SELECT, o `verify_webhook`, um INSERT e o enqueue só do id da linha. O handler da Meta não mudou.
2. **`PENDING` entrou em `PaymentStatus`** (o spec lista PAID/EXPIRED/REFUNDED/OTHER): `get_charge`
   precisa distinguir "aberta" de "outra coisa". Eventos nunca são `PENDING`.
3. **`NormalizedPaymentEvent` ganhou `external_reference` e `event_type`** (opcionais) e
   `verify_webhook` ganhou `query` opcional (o Mercado Pago assina com o `data.id` da URL).
4. **`payload` do evento guarda `{"normalized": ..., "raw": <minimizado>}`**: processar nunca
   reparseia payload de provedor, e o job de retenção pode anular `payload` sem perder o processamento.
5. **Linha migrada do Asaas nasce `connected` sem `last_verified_at`**: serve ao `after`, mas não
   ao `before` até o fluxo novo verificar (`accounts.load_charge_account(timing=...)`).
6. **A busca da cobrança aceita as duas colunas** (`provider_charge_id` OU `asaas_payment_id`) e a
   referência esperada cai para o id da consulta: linhas escritas pelo código antigo, no intervalo
   entre a migração e o deploy, continuam casando.
7. **Sondas de excesso só rodam depois de qualificadas** (`QUALIFIED_EXCESS_GROUPS`, vazio até a
   prova de sandbox, ou os 4 mínimos quando `ASAAS_PROBES_QUALIFIED=true`). Enquanto os 4 grupos mínimos
   não estiverem qualificados, a conexão recusa TUDO com `probes_not_qualified` (503): fail-closed.
   `test_release_gate_the_minimum_probes_are_qualified` é o portão (xfail até a prova).
8. **Cancelamento pelo staff** (`POST /appointments/{id}/cancel` e `PATCH .../status`) passou a
   devolver `deposit_outcome="refund_due"` (item `paid_then_cancelled`, valor total sugerido) em vez
   de `retained`/`refunded`: quem cancelou foi a clínica, não o paciente.
9. **Cancelar com a cobrança já paga no PSP nunca apaga a cobrança** (`payment_in_flight`).
10. **Chave colada**: corpo parseado à mão no hub (um erro de validação do Pydantic ecoaria a chave),
    limite de 10 tentativas / 10 min por clínica, fail-closed sem Redis.
11. **Papel imposto no backend (dono, 2026-09-29; TASK-020):** conectar, tentar de novo e desconectar exigem
    `require_clinic_manager`; a listagem de status exige `require_clinic_clinician`. Secretária → 403
    `forbidden_role`; token sem papel → 403 `role_claim_missing`. Revoga o antigo "só regra de UI (D10)".
12. **Flag `ASAAS_PROBES_QUALIFIED` (padrão desligado):** mantém a conexão fail-closed até a prova de sandbox
    ser registrada ou o dono ligar a flag; provas de sandbox são automáticas-ou-puladas por
    `ASAAS_SANDBOX_API_KEY`, nunca uma parada.
13. **Sem gate jurídico e CPF/e-mail sensíveis (dono, 2026-09-29):** a frase de consentimento LGPD está aprovada;
    `payer_document`/`payer_email` seguem transitórios na F1 (a F2 decide a persistência com pseudonimização).
14. **Segurança após revisão independente:** `OVERDUE` consulta o estado atual no provedor antes
    de cancelar; falha de consulta ou de cancelamento deixa a cobrança aberta para reconciliação.
    A seleção dos jobs gira por `last_payment_check_at`, evitando que cobranças antigas prendam
    as próximas. Um aviso de pagamento ao paciente perdido após o commit é recuperado pela
    varredura; uma queda após o envio e antes do marcador ainda pode repetir o aviso.
15. **Webhook em checagem diária:** `connected` exige `enabled`, `interrupted=false` e os eventos
    necessários. Como o Asaas só devolve `authToken` na criação, a checagem reaplica o token
    guardado cifrado e falha fechada se a atualização ou a releitura falhar.
16. **E-mail à clínica (R13):** nenhum problema operacional desta F1 envia e-mail. O helper só
    admite `patient_cancelled_paid` por item. A F2 adiciona `clinic_emailed_at` e o disparo
    durável para cancelamento pelo paciente com sinal pago (spec §6.5/§10A.4).

## 3. Contrato congelado ao fim da F1 (F2/F3/F4 constroem sobre isto)

Ver a tabela "Interfaces congeladas" no plano da TASK-016 (Task 24). Resumo: `PaymentProvider` (5
métodos), `NormalizedPaymentEvent`, `ChargeState`, `PaymentAccount`, tabelas/colunas da migração,
`open_attention_item`, `fulfill_paid_charge`, `EXPIRY_HOOKS`/`DAILY_STEPS`, `load_charge_account`, e os
endpoints `GET/PUT/POST/DELETE /tenants/me/payment-accounts/...` (respostas e códigos 403/422/429/503; papéis
`require_clinic_manager` nas três de escrita e `require_clinic_clinician` na listagem, conforme a TASK-020),
e `Settings.ASAAS_PROBES_QUALIFIED` (padrão `False`).
Mudança de valor de `deposit_outcome`: `refunded`/`partial_refund`/`refund_failed` deixaram de existir;
entram `refund_due`, `partial_refund_due`, `payment_in_flight`.

## 4. Ordem de deploy (regra `frozen-contract-migration`)

1. Migração `c8a5d3f1b7e2` (aditiva; `alembic upgrade head` pelo dono, com `alembic current` conferido por ele antes — o convênio já está em produção, o sinal Pix não tem uso).
2. `secretaria_api` E `secretaria-worker` no mesmo commit (`GET /build` → `deploy_parity=match`).
   Uma API nova com worker velho não perde evento (a linha é durável; o job novo não existe no worker
   velho, e a varredura do worker novo reenfileira); um worker novo com API velha só processa o legado.
3. O `brain-api` deve emitir as claims de papel da TASK-020 antes de ativar estas rotas; as
   alterações desse contrato estão locais em worktree separado. O frontend de pagamento é F4.
4. Variável nova (só o dono muda, no Easypanel): `PAYMENTS_WEBHOOK_BASE_URL` (https público da API).
   Sem ela nenhuma clínica passa de `incomplete` (`webhook_create_failed`). `ASAAS_WEBHOOK_IP_MODE`
   (padrão `alert`) só vira `enforce` depois de provar os IPs.

## 5. O que NÃO foi feito (de propósito)

Modo `before`, tela, Mercado Pago, e-mail de item de atenção, endpoints de itens (F2/F3/F4). Não há
coleta de CPF (D7 é da F2): se o Asaas exigir `cpfCnpj` (veredito C4 = B) o `after` de uma clínica
sem CPF cai em `payer_document_required` e nenhuma cobrança nasce (silencioso, como os demais skips).
Não mexi em `workers/tasks.py` (o aviso de cancelamento com a política continua com o texto atual).

## 6. Prova

- Local: suíte `pytest` com as duas falhas pré-existentes de Calendar explicitamente desmarcadas:
  **3094 passed, 6 skipped, 2 deselected, 1 xfailed** após a revisão; `ruff check` limpo
  nos arquivos tocados; Postgres real
  (`PAYMENTS_TEST_PG_URL`): migração sobe/desce/sobe sem drift de modelo, backfill com UUID, e
  concorrência (**10 passed**, pago uma vez, item deduplicado, webhook em rajada). Mutação provada: remover a guarda
  `status == 'aguardando_sinal'` do UPDATE atômico quebra o teste de concorrência.
- **Sandbox (automático-ou-pulado, sem parada):** C4 (`cpfCnpj`), matriz de qualificação das sondas,
  `DELETE` de cobrança não paga (e o segundo `DELETE`), `GET /webhooks` e `interrupted`, `split` numa
  chave só com Cobranças, e o E2E do `after` — **todos NÃO PROVADOS (pulado:
  `no_sandbox_key`)**, conforme `tasks/TASK-016/results/sandbox-proof.md` e `sandbox-e2e.md`.

## 7. Pendências e riscos conhecidos

- Rótulos reais da tela de permissões do Asaas e o caminho de menu do guia (spec §12.3A): conferir na tela.
- Merge de paciente (`workers/tasks.py`) não reaponta `patient_payment_customers` (o customer remoto
  fica órfão; a próxima cobrança cria outro). Sem impacto de dinheiro.
- `RESCHEDULED` não é cancelado pelo `OVERDUE` (só `SCHEDULED`): a clínica recebe a sinalização
  operacional na tela quando a F2/F4 expuserem os itens, sem novo e-mail.
- "Só gestor resolve" itens de atenção continua fora desta fase (não há endpoint de item na F1): a F4/F2 usam
  `require_clinic_manager`/`require_clinic_clinician` da TASK-020 quando os expuserem.
- Colunas Asaas antigas (`asaas_payment_id`, `asaas_customer_id`, `tenant_credentials.asaas_*`,
  `processed_asaas_events`) ficam até a fase "narrow" (migração futura, com `source_fingerprint` das duas aplicações).

## 8. TASK-053 Parte B (implementada na TASK-067 sessão 02, 2026-10-21 — LOCAL, não commitada, não publicada)

Plano: `Brain-Message-Frontend` worktree TASK-053, `docs/superpowers/plans/2026-10-11-task-053-pagamento-sinal-conexao-asaas-tela.md`
(Parte B, com as emendas de 2026-10-12). Sem migração; head Alembic continua `f3b9d1c7a5e2`.

- **B1 — `ASAAS_REFUSE_EXCESS_PERMISSIONS` (padrão `False`)**, separado de `ASAAS_PROBES_QUALIFIED`. Desligada:
  só os três grupos necessários (Clientes, Cobranças, Webhooks) decidem; os quatro grupos mínimos de excesso são
  sondados (GETs de leitura; nunca o `POST` candidato de estorno nem a segunda onda) e um concedido volta em
  `excess_groups`/`excess_labels` como INFORMAÇÃO (`asaas_probes.decide_informational`); sonda de excesso duvidosa ou
  proibida é ignorada; a qualificação não importa. Ligada: a recusa D14 de antes, exigindo a qualificação (sem ela,
  `503 probes_not_qualified` sem nenhuma chamada). Corrige a lacuna G1 (antes, TODA chave recebia `probes_not_qualified`).
  O xfail "RELEASE GATE" (`test_release_gate_the_minimum_probes_are_qualified`) continua: agora ele só gateia LIGAR a recusa.
- **B2** — `connect_asaas_account` e `recheck_asaas_account` guardam os grupos informativos de uma chave aceita;
  `_mark_connected` não os zera mais; `AsaasConnectionResultOut` ganhou `excess_groups`/`excess_labels` (aditivo,
  `extra="ignore"`). Uma chave aceita com extras e webhook falho fica `incomplete` com os grupos.
- **B3 (só testes, `tests/test_deposit_off_without_account.py`, 30)** — gestor desliga o sinal com conta ausente,
  revogada, `reauth_required`, `error` ou `incomplete`, nos dois momentos; os demais campos da clínica ficam iguais;
  desligado, o "antes" agenda sem reservar/cobrar (WhatsApp) ou só pede o código de e-mail (Portal) e o "depois" não
  cria cobrança; ligado com conta quebrada o "antes" continua recusando. Mutação em `quote_deposit`: 10 falhas / 20 verdes.
  Nenhum código de produto mudou. Papéis: secretária/médico não-gestor já recebem `403 forbidden_role` ao mandar
  `pix_deposit_enabled` (`tests/test_payment_settings_roles.py`).
- **B4** — `GET /payment-settings` ganhou `asaas_environment` (`sandbox`|`production`, de `ASAAS_BASE_URL`). Sem IP de saída.
- **B5** — `GET /internal/brain-message/insurance-overview?tenant_id=` (`services/patient_insurance_overview.py`,
  `schemas/internal.py::PatientInsuranceOverviewOut`, `extra="forbid"`): a lista que o fluxo oferece, texto fixo de
  pagamento, nota própria da clínica num "Outro", sinal `charged|not_charged|varies` espelhando
  `deposit_lifecycle._insurance_charges_deposit`; nunca id, profissional, nota do catálogo, dado de paciente. Rota do
  paciente no brain-api: `GET /patient-access/insurance-overview` (B6, `docs/PORTAL_MESSAGING_API.md` do brain-api).
- Risco latente (não tocado): `deposit_lifecycle._insurance_payment_note` lê `custom_payment_note` sem filtrar
  `catalog_id IS NULL`; hoje nada grava nota em linha do catálogo.
- **NÃO provado:** nenhuma chave real do Asaas foi usada; a conexão qualificada com o sandbox/produção continua
  pendente (`ASAAS_SANDBOX_API_KEY` do dono). Nenhuma cobrança, devolução, envio ou cancelamento real foi feito.
- Deploy (NÃO autorizado): sem migração; `secretaria_api` e `secretaria-worker` juntos (o recheck diário roda no worker
  e usa `asaas_probes`/`asaas_connection`) → brain-api (B6) → front.

### 8.1 Contrato congelado para a sessão 03 (tela)

Conexão — `PUT /tenants/me/payment-accounts/asaas/connection`, corpo EXATAMENTE `{"api_key": "<string>"}`. Ordem das
respostas: papel → limite de tentativas → corpo → formato → sondas → gravação.

```json
200 {"provider":"asaas","status":"connected","incomplete_reason":null,
     "capabilities":{"customers":true,"payments":true,"webhooks":true},
     "excess_groups":["TRANSFER"],"excess_labels":["Transferências"]}
200 {"provider":"asaas","status":"incomplete","incomplete_reason":"webhook_create_failed",
     "capabilities":{...},"excess_groups":[],"excess_labels":[]}
422 {"detail":{"code":"missing_permission","missing_groups":["WEBHOOK"],"excess_groups":[],
     "missing_labels":["Webhooks"],"excess_labels":[]}}
422 {"detail":{"code":"key_invalid"|"key_restricted","missing_groups":[],"excess_groups":[],"missing_labels":[],"excess_labels":[]}}
422 {"detail":{"code":"key_excess_permissions"|"key_permissions_mixed",...}}   (só com ASAAS_REFUSE_EXCESS_PERMISSIONS=true)
422 {"detail":{"code":"invalid_body"}}
429 {"detail":{"code":"rate_limited"}}
503 {"detail":{"code":"verification_unavailable"|"probes_not_qualified"|"rate_limiter_unavailable"}}
403 {"detail":{"code":"forbidden_role"|"role_claim_missing","message":"..."}}   (a tela usa só o code)
```

`probes_not_qualified` só aparece com a recusa LIGADA. Nenhuma resposta, recusa ou log carrega a chave (testes:
`test_flag_off_the_real_probes_connect_a_key_holding_extras`, `test_flag_off_a_timeout_is_inconclusive_and_nothing_carries_the_key`,
`test_a_malformed_body_is_422_and_the_response_never_echoes_what_was_sent`). `POST …/asaas/retry-webhook`: mesmas formas de 200,
`422 no_key`. `DELETE …/asaas`: `200` com `"status":"revoked"` | `404 not_connected`.

`GET /tenants/me/payment-accounts` → `[{provider, status, incomplete_reason, capabilities, excess_groups, excess_labels,
connected_at, last_verified_at, ready_for_before}]`. Estados: `pending|connected|incomplete|error|reauth_required|revoked`;
motivos: `key_expired_or_disabled|excess_permissions|missing_permission:<grupos>|webhook_create_failed|webhook_readback_failed|legacy_unverified`.

`GET /tenants/me/payment-settings` → `{payment_provider, deposit_timing, deposit_hold_ttl_minutes, ttl_min_minutes,
ttl_max_minutes, deposit_enabled, deposit_percent, providers:[{provider, available}], before_blockers:[{code,message}],
provider_switch_blockers:[{code,message}], asaas_environment:"sandbox"|"production"}`.

Configuração — `PUT /tenants/me/configuration` com `{"tenant": {…só os campos alterados…}}`; campos ausentes ficam como
estão. Desligar de emergência: `{"tenant":{"pix_deposit_enabled":false}}` → `200` com qualquer estado de conta (inclusive
ilegível para a tela). Recusas: `422 {"detail":{"code","message"}}` com `deposit_disabled|account_not_ready|
provider_unavailable|open_deposits` (`addon_required`/`entitlement_unresolvable` saíram em 2026-10-28: `CHECKPOINT_sinal_padrao.md`); prazo fora de 30–120 = `422` do Pydantic.

Paciente — `GET /patient-access/insurance-overview` (brain-api) → `200 {"product":"secretaria","payload":{"deposit":
{"enabled":true,"percent":30},"plans":[{"name":"GEAP","payment_text":"Pagamento: confirme as condições com a clínica.",
"clinic_note":"Pagamento na recepção.","deposit":"not_charged"},{"name":"Unimed","payment_text":"Varia por plano: …",
"clinic_note":null,"deposit":"charged"}]},"at":"…"}`; `401` sessão; `403` sem Secretaria no canal; `502/503` genéricos.

Matriz papel × ação (o backend manda; a tela só esconde):

| Ação | gestor (`manager`, ou médico `is_owner`/`is_manager`) | médico | secretária | sem claim de papel |
|---|---|---|---|---|
| `GET /payment-accounts` | sim | sim | `403 forbidden_role` | `403 role_claim_missing` |
| `PUT connection` / `POST retry-webhook` / `DELETE` | sim | `403 forbidden_role` | `403 forbidden_role` | `403 role_claim_missing` |
| `GET /payment-settings` | sim | sim | sim (sem gate de papel; lacuna G8) | sim |
| `PUT /configuration` com campo `pix_*`/`payment_*`/`deposit_*` (inclusive desligar) | sim | `403 forbidden_role` | `403 forbidden_role` | `403 role_claim_missing` |

Matriz estado da conta × sinal (decisão D-A 2 + emenda 2026-10-12):

| Conta | Sinal desligado | Sinal ligado |
|---|---|---|
| `connected` + verificada | gestor pode ligar/configurar | agenda com sinal (antes/depois) |
| ausente, `revoked`, `reauth_required`, `error`, `incomplete`, ilegível | agenda normal sem sinal | "antes": agendamento recusado (fail-closed); o único salvamento útil é desligar |

### §8.2 Prova no Asaas sandbox REAL (TASK-067 sessão 03b, 2026-10-22)

- O Asaas real recusa `POST /webhooks` sem `email` (400 "O parâmetro email deve ser informado"). A conexão passa a enviar `email` = `ASAAS_WEBHOOK_ALERT_EMAIL` (vazia = omitido; sem ela o Asaas real deixa a conta `incomplete`/`webhook_create_failed`). **Definir antes de conectar clínicas reais.**
- O webhook exige URL pública https (`PAYMENTS_WEBHOOK_BASE_URL`); `http://127.0.0.1` e `*.invalid` são recusadas.
- Provado com chave real de sandbox: conectar (sondas reais, excesso informativo), criar cobrança Pix, confirmar, conciliar, processar, repetir (sem efeito), cancelar (DELETE), desconectar (apaga o aviso no Asaas). Contrato JSON do §8.1 inalterado.
- NÃO provado: webhook recebido do Asaas real; recusa de excesso (D14) com chave restrita.

### §8.3 CPF e cobrança no Asaas (TASK-067, 2026-10-26 — fato do sandbox)

`POST /customers` aceita cliente SEM `cpfCnpj`; é `POST /payments` que responde 400 `invalid_object` ("é necessário preencher o CPF ou CNPJ do cliente"). O veredito C4
da F1 ficava incompleto: o provedor só traduzia o 400 do CLIENTE para `payer_document_required`. Agora `AsaasError.document_missing` (booleano) marca o 400 da COBRANÇA e
`create_pix_charge` o traduz para o mesmo código, que dispara a pergunta do CPF no chat (Portal e WhatsApp, modos `before` e `after`). Com documento já enviado vira `charge_failed`
(sem laço de perguntas). Todo cliente criado NESTA chamada é apagado em melhor esforço (depois de anular a cobrança) quando a cobrança falha ou é anulada; um cliente já guardado nunca é apagado, e uma exclusão que falha nunca substitui o erro original (vira só o log `asaas_customer_cleanup_failed`, com o tipo do erro).

### §8.4 Motivo de falha da cobrança, limpeza do cliente órfão e nova tentativa do QR (TASK-071 Plano 2, 2026-10-28)

Antes, uma cobrança Pix que falhava deixava só um código simbólico genérico (`charge_failed`) e ninguém sabia dizer por quê. Agora toda falha grava um **motivo fechado**, sem texto livre do Asaas.

- **Lista fechada de motivos:** `FailureReason` em `secretaria/services/payments/failure_reason.py` (`classify` só devolve membros dela; o que não reconhece é `unknown`; `user_text` dá a frase em português para a clínica). O `ProviderError` ganhou `reason`, `stage` (`config` / `customer` / `charge` / `qr` / `persist`) e `code_hint` (código simbólico curto do Asaas, nunca o texto dele); o `ChargeStartError` de `booking_charge` repassa os mesmos campos.
- **Log `payment_charge_failed`:** campos `tenant_id`, `stage`, `reason`, `status`, `code_hint`. Emitido **uma única vez**, em `begin_payment` (`_log_charge_failed`); **não** é emitido para a pergunta rotineira de CPF/e-mail (`_is_payer_data_question`), que não é falha.
- **`booking_gate_deposit_blocked` enriquecido:** `reason` agora guarda o motivo fechado e o valor simbólico antigo passou para `code`. **Quem buscava `reason=charge_failed` nos logs deve buscar `code=charge_failed`.** Ganhou também `stage` e `status` (None quando nenhuma cobrança foi tentada).
- **`pix_deposit_charge_failed`** (caminho do sinal no modo `after`, `maybe_create_deposit`): mantém o nome e o `code`; ganhou `reason`, `stage`, `status` e `code_hint` (None quando o provedor não classificou).
- **Nova tentativa do QR:** `_fetch_pix_qr` em `asaas_provider` tenta 2 vezes (quando a primeira lança erro ou responde sem `payload`), com pausa `ASAAS_QR_RETRY_DELAY_SECONDS` (opcional, padrão 1.5 s; **não exige configuração no painel**). Cada tentativa que falha loga `asaas_qr_failed` com `attempt`, `status`, `code_hint` e `error_type` (nunca corpo, id da cobrança nem dados do pagador). Sem QR depois da 2ª tentativa, o motivo é `qr_unavailable`.
- **Cliente órfão:** ver §8.3 (limpeza de todo cliente criado na chamada; `_discard_customer_created_here` em `booking_charge` e `_discard_customer` em `asaas_provider`).

**Pendências conhecidas (Plano 3/4), de propósito fora desta entrega:**
- a nova tentativa do QR também dispara em 4xx permanente (deveria só tentar de novo em erro transitório);
- a sessão do banco fica aberta durante as chamadas ao provedor (risco de conexão "idle in transaction");
- `no_api_key` (chave ausente) cai no texto de `api_key_invalid`, que não distingue "sem chave" de "chave inválida";
- o caminho de troca de serviço com aumento de preço (`service_change`) ainda pode deixar um cliente órfão no Asaas.

### §8.5 Prontidão do sinal, cadastro do Asaas, checklist e "Testar cobrança" (TASK-071 Plano 4, 2026-10-29)

O selo "Sinal funcionando" só acende com **prova** (uma cobrança real que deu certo); o resto é lista de pendências.

- **Cadastro do Asaas:** `AsaasClient.get_account_status()` (`GET /myAccount/status`) e `services/payments/onboarding.py` (`parse_account_status`, `read_onboarding`). Os 4 estados (`general`, `documentation`, `commercialInfo`, `bankAccountInfo`) ficam nas colunas `onboarding_general/_docs/_commercial/_bank` + `onboarding_checked_at` de `clinic_payment_accounts`, lidos ao conectar e na revisão diária. Valor fora dos 4 conhecidos = `UNKNOWN` (nunca aprovado); `403` (chave só com Clientes/Cobranças/Webhooks) ou corpo inesperado = "não legível" (campos `None`, conexão intacta). A coluna é `onboarding_docs` (e não `_documents`) porque o teste de higiene `test_payment_secret_hygiene` proíbe "document" em nome de coluna de pagamento.
- **Prova perdida ao trocar a chave:** `credentials_changed_at` (gravada em `connect_asaas_account`) faz `accounts.charge_health` ignorar um sucesso mais antigo que a chave atual (o selo volta a cinza até nova prova; uma falha anterior continua valendo).
- **Prontidão:** `services/payments/readiness.py` (`evaluate_deposit_readiness`, `badge_for`). Lê só linhas (sem chamar o Asaas). Códigos: `sinal_off`, `no_account`, `account_incomplete`, `no_prices`, `services_without_price`, `amount_below_minimum` (só existe com `MIN_PIX_CENTS`, hoje `None` até a medição), `onboarding_pending`, `onboarding_unreadable`, `last_charge_failing`, `never_tested`, mais `no_contact_email` e `professionals_without_email` (acrescentados porque o item 7 do checklist precisa deles). O preço usa o mesmo `parse_brl_to_cents` e o mesmo percentual do `deposit_gate.quote_deposit`.
- **Checklist (8 itens):** `services/payments/checklist.py` (`checklist_view`, `build_checklist`). O sistema preenche 1, 4–8 (e o 2 quando o cadastro é legível); o usuário marca `pix_key_registered` e, se o cadastro não é legível, `onboarding_done` (`MANUAL_ITEMS`, lista branca; gravados em `setup_checklist` com `{done, at, by}`; `by` é o papel do usuário, pois o token do Portal não traz id de usuário). Os cliques sobrevivem a trocar/reconectar a chave. `pix_key_registered` marcado vira `warning` enquanto a última falha for `qr_unavailable`/`pix_key_missing`.
- **Testar cobrança:** `services/payments/probe_charge.py` (`run_probe_charge`). Passa pelo mesmo `provider.create_pix_charge` do paciente (R$ 5,00, referência `probe-<uuid>`, cliente "Teste Brain - pode apagar" sem e-mail/telefone), apaga cobrança e cliente num `finally` (`cleaned=False` se a limpeza falhar), grava `record_charge_success/failure` e tem trava por clínica no Redis (`SET NX`, 60 s). O CPF/CNPJ digitado vive só na memória da chamada.
- **Endpoints** (`api/hub/payment_accounts.py`): `GET /tenants/me/payment-accounts/asaas/checklist` (gestor ou médico), `PUT .../asaas/checklist` (`{"item","done"}`, só gestor), `POST .../asaas/test-charge` (`{"document"}`, só gestor; `429 probe_in_progress`, `503` sem Redis). Corpos lidos à mão: um erro de validação nunca devolve o documento.
- **Migração** `a7c3e9d1f5b2` (cabeça única, depois de `b9e4d2a6c8f1`): só acrescenta 7 colunas anuláveis. Deploy: migração antes; API e worker da secretarIA juntos.
- **Medição do Asaas real:** `scripts/asaas_measure.py` e `docs/ASAAS_MEDICOES.md`. **PENDENTE**: o dono ainda não rodou; sem ela não há `MIN_PIX_CENTS` nem palavras-chave novas em `_classify_text`.

## 9. Troca de serviço com diferença do sinal (TASK-067, 2026-10-26 — LOCAL: não commitada, não publicada, não deployada)
Spec: `docs/superpowers/specs/2026-10-26-troca-servico-diferenca-sinal-design.md`; plano: `docs/superpowers/plans/2026-10-26-troca-servico-diferenca-sinal.md`. Antes, com o sinal pago o paciente não trocava de serviço e a equipe trocava sem tratar o dinheiro.

### 9.1 Regras (decisão do dono, 2026-10-26)
- Sinal novo = `round(preço_novo × PixDeposit.percent_applied / 100)` (percentual do sinal PAGO, não o atual da clínica); diferença = |novo − `amount_cents`|. Preço ilegível/zero ou sinal não pago -> bloqueado ("fale com a clínica").
- Corte fixo de R$ 10,00 (`service_change_policy.MIN_DIFFERENCE_CENTS`), não configurável.
- Mais caro, diferença >= R$ 10 (`increase_charge`): novo Pix SÓ da diferença; o serviço só muda DEPOIS de pago (`pending_edit` guardado no ajuste). Prazo do Pix = `deposit_hold_ttl_minutes`. Não é bloqueado por `pix_difference_refund_min_days` e vale perto da consulta, se o novo serviço couber no horário. Vencido/cancelado: ajuste `expired`, serviço antigo mantido, paciente avisado.
- Mais caro, diferença < R$ 10 (`increase_free`): troca vale sem Pix (acerto na consulta).
- Mais barato, diferença >= R$ 10 e dias até a consulta >= `pix_difference_refund_min_days` (`decrease_ask`): serviço muda na hora e o paciente é perguntado (botões fixos "Sim, quero de volta" / "Não, abater no valor", WhatsApp e Portal). Sim -> `refund_requested` + item de atenção `service_diff_refund_due` (a clínica devolve; o sistema NUNCA devolve sozinho). Não -> `kept_credit` (só registro). Sem resposta em 24 h = "não" (varredura).
- Mais barato fora disso (`decrease_keep`): `clinic_kept`, sem pergunta; a clínica fica com a diferença (abatida no resto da consulta).
- Equipe (agenda) usa o MESMO motor (`source=hub`): aumento -> Pix pendente enviado ao paciente; redução -> pergunta ao paciente. Mudança de horário feita pela clínica nunca conta no limite de reagendamento (`time_changed=False`).
- Aplicar o `pending_edit` só uma vez: UPDATE atômico `move_adjustment` exigindo o status de origem.

### 9.2 Dados e configuração
- Tabela `deposit_adjustments` (`models/deposit_adjustment.py`; migração `b3e8f1a6c2d9`): `id, tenant_id, deposit_id, appointment_id, kind (increase|decrease), status (awaiting_payment|applied|expired|cancelled|asked|refund_requested|kept_credit|clinic_kept), old_service, new_service, pending_edit (JSON), diff_cents, provider_charge_id (único; nulo em decrease), expires_at, asked_at, resolved_at, created_at`. `PixDeposit.amount_cents` NÃO muda; o total retido é calculado (`service_change.held_deposit_cents` = sinal + aumentos pagos − reduções com devolução pedida).
- Config `tenants.pix_difference_refund_min_days` (migração `a7d2e9c4b1f6`): inteiro 0–60, padrão 2, 0 = sempre pergunta (respeitando os R$ 10). Só gestor altera (grupo `PAYMENT_WRITE_FIELDS`; médico recebe 403). Passa por `models/tenant.py`, `schemas/config.py`, `services/hub_configuration.py`, `api/hub/config.py`.
- Cadeia alembic: `f3b9d1c7a5e2` -> `a7d2e9c4b1f6` -> `b3e8f1a6c2d9` (head único).

### 9.3 Fluxo e módulos
- `services/payments/service_change_policy.py`: regra pura (sem I/O). `services/payments/service_change.py`: orquestração (`request_service_change`, `answer_refund_question`, `expire_adjustments`, `apply_paid_adjustment`, `held_deposit_cents`, `find_adjustment`, `move_adjustment`, textos e botões). `services/payments/adjustment_queries.py`: total retido e `settle_open_adjustments`.
- Paciente: `appointment_edit_flow` (serviço liberado com sinal pago; médico/convênio continuam travados), `workers/shared/appointment_edit_apply._apply_paid_service_change`, `workers/shared/service_change_actions` (bolhas e toques `svcdiffyes|`/`svcdiffno|`; prefixos em `schemas/webhook`, roteados em `workers/shared/actions` e `workers/portal/inbound`), `services/flow_router.ActionButtonsBubble`.
- Equipe: `services/staff_appointment_edit.apply_staff_edit` (uma transação; bloqueio desfaz também o Google), `services/clinic_action_notice.notify_service_change`, resposta `AppointmentEditRead.service_change` em `api/hub/calendar.edit_appointment`.
- Webhook: `event_processor.apply_normalized_event` cai em `service_change.find_adjustment` quando `find_deposit` não acha; valida referência e valor da DIFERENÇA, aplica o `pending_edit` na mesma transação e marca `applied`; após o commit, hook `update_agenda_after_payment` (agenda, lembretes, A7 uma vez). Pagamento de ajuste já vencido/cancelado, ou a menor -> item de atenção ORPHAN existente. `jobs.expire_due_adjustments` roda no tick de `workers/payments_cron.run_payment_expiry` (varredura com `_SWEEP_OFFSET`).
- Cancelamento: `deposit_lifecycle.on_appointment_cancelled` chama `settle_open_adjustments` (anula na Asaas o Pix de diferença não pago; ajuste `asked` fecha a pergunta); o aviso de devolução usa o total retido (sinal + diferenças pagas). Mesma lógica em `release_offer*` (clínica libera horário pago) e nos textos de `workers/shared/deposit`.
- Atenção: razões `service_diff_refund_due` e `service_diff_agenda_pending` (`models/payment_attention_item`, texto em `attention_view.REASON_TEXT`; sem migração).
- E-mail ao médico: só o aviso "Consulta alterada" (`ProfessionalEditNotice`, campo `serviço`), exatamente 1 por troca aplicada; nenhum e-mail por Pix pendente, pagamento, recusa ou vencimento. Troca aguardando pagamento: 0 e-mails de serviço.

### 9.4 Ordem de deploy (quando o dono autorizar — nada foi deployado)
Migrações `a7d2e9c4b1f6` e `b3e8f1a6c2d9` ANTES de API e worker: toda leitura de depósito PAGO consulta `deposit_adjustments`. API e worker são deploys separados da secretarIA (deployar só a API não move o worker; varredura e toques vivem no worker). Front (Brain-Message-Frontend) por último.

### 9.5 Riscos conhecidos / adiados (reauditados na TASK-067 sessão 07, 2026-10-26 — LOCAL)
- **CORRIGIDO (s07)** — ordem de locks webhook x cancelamento: era um deadlock REAL no Postgres (reproduzido: `DeadlockDetectedError`). O cancelamento trava a consulta e depois o ajuste; o webhook travava o ajuste e depois a consulta. `event_processor._on_adjustment_paid` agora trava a CONSULTA (`SELECT … FOR UPDATE`, escopo do tenant) antes de mexer no ajuste, depois da chamada ao provedor (nenhuma trava durante a rede). Prova: `tests/test_service_change_postgres.py` (Postgres descartável, opt-in `PAYMENTS_TEST_PG_URL`): vermelho antes, verde 3×3 depois; o pagamento que chega durante o cancelamento vira ORPHAN, serviço intocado.
- **CORRIGIDO (s07)** — corpo do 409 `retention_ack_required` em `api/hub/calendar.py::release_appointment`: ganhou `held_cents` (aditivo, = `adjustment_queries.held_cents_hint`, o mesmo número da mensagem); `amount_cents` continua sendo o sinal. O front usa só a mensagem; nada muda lá. Teste: `tests/test_hub_release.py::test_the_acknowledgement_409_quotes_the_total_held_with_paid_differences`.
- **PROVADO (s07)** — aplicar-uma-vez sob concorrência agora também no Postgres: 8 webhooks simultâneos da mesma diferença → 1 `adjustment_applied`, 7 `adjustment_already_applied`, 1 aviso ao médico, 0 itens (`test_many_workers_apply_one_paid_difference_exactly_once`). Não há promessa de entrega exactly-once dos AVISOS (ver abaixo).
- **CORRIGIDO (seguimento B + revisão, 2026-10-27)** — void repetido: se o provedor estiver fora (ou o void falhar, ou a conta estiver indisponível) no cancelamento/liberação, o cancelamento segue e o ajuste vai a `cancelled_void_pending` (status novo, VARCHAR, sem migração; `adjustment_queries.settle_open_adjustments`). A varredura de pagamentos (`service_change._retry_pending_voids`, dentro de `expire_adjustments`, mesmo tick do `payments_cron`) tenta de novo a cada tick: `get_charge` primeiro (pago -> evento sintético pelo caminho do webhook, nunca apagado), senão anula e passa a `cancelled`. Para de vez (vai a `cancelled`, um log `service_change_void_retry_gave_up`) quando o provedor diz que a cobrança não existe (404), que já foi estornada (REFUNDED, nunca apagada), quando recusa o void para sempre (4xx não repetível) ou depois de `VOID_RETRY_WINDOW` (7 dias desde o cancelamento). Pagamento que chega com o void pendente ou depois de desistir = item ORPHAN. Risco restante: entre o cancelamento e o void efetivo o Pix continua pagável (o paciente foi avisado para não pagar).
- **NOVO (seguimento B + revisão I2/I3, 2026-10-27)** — provedor FORA ao criar o Pix da diferença (troca para serviço mais caro, paciente e equipe): só `ProviderError.retryable` (rede, 429, 5xx) ou timeout/erro de transporte (`service_change.provider_unavailable`) aplicam a troca na hora como no `increase_free` (decisão `increase_collect`, ajuste `collect_due` com o MESMO id enviado como referência, um aviso "Consulta alterada" ao médico) e abrem UM item `service_diff_collect_due` (valor = diferença, horário e serviço; sem `deposit_id`, sem data de pagamento; texto "…o provedor de pagamento estava indisponível… cobrar R$ X do paciente"; sem e-mail de alerta). Recusa permanente (401/403 chave, 400 validação como CPF ausente, cobrança sem código, erro desconhecido) continua `blocked/charge_failed` (paciente: "…fale com a clínica"; equipe: 502): a diferença nunca é dispensada em silêncio. O paciente lê que a diferença será acertada com a clínica (fluxo do paciente: bolha após o "Pronto"; equipe: `clinic_action_notice.notify_service_change`, status `applied_now`). A diferença NÃO entra no total retido nem em devolução. Regra única (I3): enquanto o item de cobrança estiver ABERTO, QUALQUER nova troca de serviço dessa consulta — inclusive voltar ao serviço antigo — é bloqueada (`collect_pending`; paciente: "A diferença do sinal da troca anterior ainda vai ser acertada com a clínica… fale com a clínica"; equipe: 409). Quando a clínica resolve o item, as trocas voltam (a próxima é calculada sobre o que foi pago por Pix — a clínica considera o que cobrou à parte). Cancelamento (e a devolução de horário liberado quando vence) fecha o item (`resolved`, ação `other`, nota "Consulta cancelada: não cobrar a diferença do sinal desta troca.", por "Sistema") e passa o ajuste a `cancelled`; a liberação que ainda pode ser mantida não fecha. Conta desconectada continua `blocked/no_payment_account`. Riscos: o item se resolve com "Outro" + nota (não há ação "cobrado"); remarcação com sinal levado (release carry-over) não fecha o item sozinha.
- **MANTIDO** — a cobrança da diferença é criada dentro da transação do chamador (trava do sinal aberta durante a chamada ao Asaas), antes do commit: falha de commit deixa cobrança órfã no Asaas que ninguém vê (o código Pix só sai depois do commit) e que vence. Separar em duas fases muda o desenho; não feito.
- **MANTIDO (depende de decisão)** — borda "sinal pago entre a leitura e o lock": na edição pela equipe/paciente, um sinal ainda `aguardando` faz a troca seguir sem dinheiro; se for pago no meio, o sinal pago corresponde ao serviço antigo. É o mesmo efeito de trocar o serviço com o sinal aguardando (o plano não cobre re-cotar sinal aguardando) — política a decidir pelo dono, não erro técnico isolado.
- **CORRIGIDO (seguimento C + revisão, 2026-10-27)** — diferença paga A MENOS: não aplica, ajuste `cancelled`, item `amount_under` da clínica (nunca devolução automática, nunca Pix novo do resto) e, depois do commit, UM aviso ao paciente com os números ("Recebemos R$ X no Pix da diferença, mas o valor necessário é R$ Y. Faltam R$ Z. Por isso, a troca … não foi feita. A clínica vai entrar em contato com você."; `service_change.adjustment_underpaid_text`) pelo mesmo gancho do I2 (`ADJUSTMENT_NOT_APPLIED_HOOKS`, `EventResult.not_applied_paid_cents`). Replays (mesmo evento, ou RECEIVED+CONFIRMED) não repetem o aviso. Sem valor pago conhecido (ausente/zero) não há aviso ao paciente (nunca "R$ 0,00"); só o item da clínica. Referência errada: continua só item `reference_mismatch` para a clínica, sem mensagem ao paciente (decisão do dono). Valor a mais: comportamento mantido (aplica + item `amount_over`). Não coberto: sinal ORIGINAL pago a menos (outro caminho, `_on_paid`) segue só com item da clínica.
- Ganchos depois do commit da diferença paga (agenda + aviso ao paciente, `run_applied_hooks`): no máximo uma vez — queda do worker entre o commit e o gancho perde a atualização da agenda e o aviso (não há varredura de recuperação); replay nunca duplica (`adjustment_already_applied` não roda ganchos). Documentado, não corrigido (exigiria marcador + varredura).
- Paciente fora da janela de 24 h do WhatsApp sem template pago: o texto do Pix NÃO é enviado; a clínica recebe o link wa.me; a diferença vence após o TTL e o serviço fica o antigo (seguro, mas decisão/risco do dono). Com template, a pergunta de devolução chega sem botões -> sem resposta em 24 h conta como "não".
- E-mails "remarcada": troca de horário cuja diferença é paga depois não dispara os e-mails de remarcação; se a agenda falhar ao "Confirmar", vai para atendimento humano.
- Rótulo "Sim, quero de volta" limitado pelos 20 caracteres do botão do WhatsApp.
- ~~Aplicar-uma-vez provado só em SQLite~~ — provado no Postgres na s07 (acima).
- Achados 1–6 da prova no navegador (Portal só chega a "Mudar serviço" pelo card do lembrete; texto do menu cita médico; total retido no drawer; troca pendente na agenda; aviso ao cancelar com Pix pendente; valor da diferença no cartão de confirmação) estão sendo tratados à parte — conferir o estado atual no código.

### 9.6 Prova e estado
Testes novos: `test_service_change_policy`, `test_service_change`, `test_service_change_webhook`, `test_service_change_agenda`, `test_service_change_cancel`, `test_appointment_edit_paid_service`, `test_deposit_adjustment_model`, +8 em `test_professional_edit_notification`. Suítes amplas verdes nos relatórios das tasks; ruff limpo nos arquivos novos. Navegador (`agent-browser`, 1280 e 390 px) com Asaas SANDBOX real: cenários a–h PASS (sessão 05c, `tasks/TASK-067/LEDGER.md`).
**NÃO PROVADO:** WhatsApp real, SMTP real, Asaas de produção, LLM real em pedido livre de troca, pagamento da diferença após vencer/cancelar ou a menor (só teste automático), "sem resposta em 24 h = não" (só teste).
**Estado:** tudo LOCAL — nada commitado, nada enviado ao remoto, nada deployado.
**Seguimento B/C + revisão I2/I3 (2026-10-27, LOCAL):** ver 9.5 (provedor fora — só falha repetível/timeout — ao criar o Pix da diferença; bloqueio de novas trocas com cobrança aberta e fechamento no cancelamento; void repetido com parada; aviso de diferença paga a menos só com valor conhecido). Testes: `tests/test_service_change_provider_down.py` (novo) + casos em `test_service_change_webhook`, `test_staff_appointment_edit`, `test_hub_edit_appointment`, `test_appointment_edit_paid_service`, `test_payment_attention(_hub)`; front `lib/payment-attention.ts` (rótulo de reserva do novo motivo).
**Final fix wave (2026-10-26, LOCAL):** ao pagar a diferença, `service_change.apply_paid_adjustment` grava só serviço, fim e início (este só se a própria troca o moveu); convênio, planos, para-quem, médico e evento vêm da linha ATUAL. Se a consulta foi remarcada antes do pagamento, a troca cai no horário atual com a nova duração quando ainda cabe (banco: outras consultas do mesmo agenda e reservas vivas); se não cabe, foi cancelada ou passou, vira item ORPHAN e o paciente recebe um aviso neutro (`ADJUSTMENT_NOT_APPLIED_HOOKS`, sem prometer devolução). Testes em `test_service_change_webhook`.
