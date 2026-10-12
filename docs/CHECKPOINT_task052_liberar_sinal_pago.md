# CHECKPOINT — Liberar horário com sinal pago: manter, remarcar ou cancelar (TASK-052)

**Estado:** construído e commitado em `task/TASK-052-liberar-sinal-pago` (worktree
`C:\TECH\BRAIN-worktrees\TASK-052\secretarIA`), último commit de código `9c0db0d` (os commits de documentação vêm depois); front em
`task/TASK-052-aviso-liberar` `b0a10c0` (worktree `C:\TECH\BRAIN-worktrees\TASK-052\Brain-Message-Frontend`).
**Não pushado, não deployado.** Integra em `feature/out-of-mvp` (nunca `main`), depois da TASK-050.
Spec: `docs/superpowers/specs/2026-10-11-liberar-horario-com-sinal-pago-design.md`. Plano:
`docs/superpowers/plans/2026-10-11-task-052-liberar-horario-sinal-pago.md`.

## 1. O que é
Liberar uma consulta com sinal PAGO abre uma oferta. Manter horário (até o início da consulta) é um PEDIDO à clínica,
que aceita (a consulta volta, se o horário estiver livre) ou recusa em até 24 h do toque; sem decisão = recusa.
Remarcar a qualquer momento: ao CONFIRMAR, o sistema LÊ a cobrança no Asaas (nunca estorna). Paga e sem estorno → leva o
sinal (a clínica recebe "REMARCADA levando o sinal" e "marcar devolvido" fica travado); estornada/em estorno ou já marcada
devolvida → sinal novo; Asaas sem resposta/erro → sinal novo por segurança (o paciente pode pagar duas vezes; a clínica
recebe "DEVOLVA o sinal anterior"). Cancelar avisa a clínica NA HORA com o passo a passo do estorno no Asaas. Sem resposta:
aviso em B = min(T + 6 h, fechamento − 30 min; fechamento da clínica se o brain-api diz que há secretária, senão o do
médico — por enquanto sempre o do médico, Task 16 pendente); em T + 24 h "Cancelado por falta de resposta" e "prazo vencido, devolva agora". A clínica (qualquer papel, no
escopo da agenda) marca "devolvido" e o paciente responde Recebido / Não recebido. Portal: cartão + e-mail com link;
WhatsApp: só mensagem. O sistema nunca estorna (D15).

## 2. Onde está cada coisa
| Camada | Arquivo |
|---|---|
| Tabelas + migração | `models/deposit_release_offer.py`, `migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py` |
| Prazos | `services/release_deadlines.py` |
| Regras e textos | `services/release_offer.py` |
| Aceitar "Manter" | `services/release_keep.py` |
| Papéis (brain-api) | PENDENTE (Task 16): B usa o horário do médico até a tela de integrantes existir |
| Envios | `services/release_offer_dispatch.py`, `services/email.py`, `config.py` |
| Liberar / Portal | `api/hub/calendar.py::release_appointment`, `services/appointment_release.py` |
| Aba (TASK-054) | `api/hub/release_offers.py` (lista, devolvido, aceitar/recusar Manter; escopo da agenda), `schemas/release_offers.py`; gancho em `services/payments/attention_view.py::resolve_item` |
| WhatsApp | `workers/whatsapp/notifications.py::send_cancellation_notice` |
| Toques | `workers/shared/release_offer_actions.py`, `workers/shared/actions.py`, `schemas/webhook.py`, `workers/portal/inbound.py`, `workers/portal/open.py`, `workers/portal/release_card.py` (cartão do Portal depois de S; fora de `workers/shared/` porque `tests/test_channel_branches.py` proíbe comparar o canal em código neutro) |
| Transferência | `services/release_carry_check.py` (leitura no Asaas na confirmação; fail-safe = sinal novo), `services/payments/asaas_provider.py::get_charge` (`refunds`), `workers/shared/flow_runner.py`, `workers/shared/booking_hold.py`, `services/booking_hold.py` |
| B, T + 24 h, K, envios | `workers/payments_cron.py::run_payment_sweeps` |
| Dias segurados | `services/flow_router.py::_enter_slot_picker`, `services/availability.py::held_slots` |

## 3. Decisões e perguntas
**Reverte uma decisão da TASK-050 (D2):** o seletor de dias volta a OFERECER o dia em que todos os horários livres estão segurados por outro paciente (aguardando Pix); ao escolher o dia o paciente vê quais HORÁRIOS estão segurados (nunca por quem) e que podem voltar a ficar livres. É a decisão do dono de 2026-10-11 que o plano da TASK-052 incorpora.

Decisões padrão: spec §10. Perguntas ao dono: spec §11. Modelos Meta: `docs/LEMBRETES_MODELOS_META.md` (TASK-052).

## 4. Validação
Suíte completa (com Postgres descartável ligado): **6270 passed, 68 skipped, 1 xfailed, 0 failed**. Referência da TASK-050: 6115 passed / 79 skipped / 1 xfailed (a linha de base da Task 0 rodou em paralelo com edições e foi descartada; o Postgres só tira 11 skips). Ruff limpo nos arquivos tocados. Postgres real (18 local descartável): **PROVADO** — 3 corridas × 3 rodadas, e a migração `b6d2e8f4a1c3` sobe/desce/sobe sem divergência com os modelos. Front: vitest `lib/agenda` + `components/agenda` 497 passed, typecheck limpo.
**NÃO PROVADO:** WhatsApp real (modelos não aprovados), SMTP real, cartão no Portal real, produção, a leitura da cobrança no Asaas real (status de estorno e lista `refunds`; só o sandbox prova o formato — qualquer formato inesperado cobra de novo).

## 5. Deploy (NÃO autorizado)
Migração `b6d2e8f4a1c3` → API → worker LOGO depois → Brain-Message-Frontend. `BRAIN_MESSAGE_PORTAL_URL` na API e no worker.
