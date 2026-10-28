# CHECKPOINT — Sinal (Pix) é recurso padrão da SecretarIA, não add-on

**Decisão do dono, 2026-10-28.** O pagamento do sinal de uma consulta passa a fazer parte da
SecretarIA para qualquer clínica. Deixa de ser o add-on `pix_deposit` do plano.

## Por que mudou

Teste em produção (2026-10-28): clínica com conta Asaas de produção conectada no Portal, sinal
ligado e contexto preenchido — e o agendamento terminava sem pedir o sinal. Causa: três pontos do
código ainda exigiam o add-on `pix_deposit` ativo no brain-api, e a clínica não o tinha:

1. `plugins/pix_deposit.py` — o hook pós-agendamento (momento `after`) era registrado com
   `entitlement_keys=("pix_deposit",)`; sem o add-on o registro nem chamava o hook.
2. `services/payments/deposit_gate.py::quote_deposit` — no momento `before`, sem o add-on a
   resposta era `no_deposit` (motivo `no_entitlement`), e o agendamento era confirmado sem sinal.
3. `services/payments/settings.py::before_blockers` — a tela recusava "cobrar antes de confirmar"
   com `addon_required` / `entitlement_unresolvable`.

## Regra vigente

O sinal é cobrado quando, e só quando, a própria clínica preencheu os requisitos:

| Requisito | Onde | Se faltar |
|---|---|---|
| Sinal ligado (`tenants.pix_deposit_enabled`) e percentual | Contexto → Pagamento | consulta normal, sem sinal |
| Conta Asaas conectada e validada | Contexto → Pagamento | `after`: sem sinal (log); `before`: o agendamento é bloqueado, e a tela não deixa escolher `before` (`account_not_ready`) |
| Serviço com preço legível (`appointment_types[].price`) | Contexto → Serviços | sem sinal ("sem preço, sem sinal", regra antiga mantida) |
| Convênio que cobra sinal (quando há convênio) | Contexto → Convênios | sem sinal |
| Assinatura da SecretarIA ativa | brain-api | nenhum plugin roda (vale para tudo, não só o sinal) |

Nenhum ponto lê mais o add-on `pix_deposit`. Consequência boa: o momento `before` não depende mais
de o brain-api responder — o motivo `entitlement_unresolvable` deixou de existir.

## O que mudou no código (secretarIA)

- `plugins/pix_deposit.py` — `entitlement_keys=()` (capacidade CORE, mesma semântica de
  `reminders`/`professional_notification`: respeita só `summary.active`).
- `services/payments/deposit_gate.py` — removidos `is_addon_active`, `ADDON_KEY`,
  `REASON_NO_ENTITLEMENT`, `REASON_ENTITLEMENT_UNRESOLVABLE`; `before_gate_active` = momento
  `before` + sinal ligado.
- `services/payments/settings.py` — removidos `BLOCKER_ADDON_REQUIRED` e
  `BLOCKER_ENTITLEMENT_UNRESOLVABLE`. Códigos de recusa vigentes: `deposit_disabled |
  account_not_ready | provider_unavailable | open_deposits`.
- `services/entitlements_client.py::ADDON_IDS` mantém `pix_deposit`: o brain-api ainda devolve a
  chave e `is_entitled` recusa chave desconhecida. Ela só não é mais consultada.
- Testes ajustados para a regra nova (clínica sem o add-on É cobrada; assinatura inativa não).

## Fora deste repo (pendente, decisão comercial)

- **Retirado da venda (2026-10-28, autorizado pelo dono)**, branch `feat/sinal-fora-da-venda`
  em brain-api e brain-frontend: `catalog.AddonDef.sellable=False` para `pix_deposit` (a chave
  continua no conjunto de add-ons); o checkout descarta, o PATCH de add-ons do cadastro recusa
  e o `checkout-config` responde indisponível; o cadastro não mostra mais o cartão "Sinal via
  Pix". Fora do código, pendente: arquivar o preço no Stripe e decidir sobre assinaturas que já
  tenham o item (continuam cobradas até alguém removê-lo). Não tirar `pix_deposit` do
  `STRIPE_PRICE_MAP` enquanto houver assinatura com o item: o webhook ainda o reconhece por ele.
- **Brain-Message-Frontend** ainda tem texto para `addon_required`/`entitlement_unresolvable`; fica
  inofensivo (o backend não envia mais esses códigos) e pode ser limpo depois.

## Deploy

Sem migração. Deploy da API **e** do worker (o hook pós-agendamento roda no worker; o momento
`before` decide no fluxo, também no worker; a tela de configuração é a API).
