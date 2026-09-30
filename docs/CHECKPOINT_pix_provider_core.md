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
