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
ilegível para a tela). Recusas: `422 {"detail":{"code","message"}}` com `deposit_disabled|addon_required|
entitlement_unresolvable|account_not_ready|provider_unavailable|open_deposits`; prazo fora de 30–120 = `422` do Pydantic.

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
(sem laço de perguntas). O cliente criado sem CPF é apagado em melhor esforço.
