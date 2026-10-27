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
médico; desconhecido também usa o do médico — Task 16 ligada localmente na TASK-067 s06, §8); em T + 24 h "Cancelado por falta de resposta" e "prazo vencido, devolva agora". A clínica (qualquer papel, no
escopo da agenda) marca "devolvido" e o paciente responde Recebido / Não recebido. Portal: cartão + e-mail com link;
WhatsApp: só mensagem. O sistema nunca estorna (D15).

## 2. Onde está cada coisa
| Camada | Arquivo |
|---|---|
| Tabelas + migração | `models/deposit_release_offer.py`, `migrations/versions/b6d2e8f4a1c3_deposit_release_offers.py` |
| Prazos | `services/release_deadlines.py` |
| Regras e textos | `services/release_offer.py` |
| Aceitar "Manter" | `services/release_keep.py` |
| Papéis (brain-api) | `services/brain_staff.py::fetch_has_secretary` → brain-api `GET /internal/tenants/{id}/staff-roles` (Task 16, §8) |
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
Suíte completa (com Postgres descartável ligado): **6278 passed, 68 skipped, 1 xfailed, 0 failed** (depois da revisão final). Referência da TASK-050: 6115 passed / 79 skipped / 1 xfailed (a linha de base da Task 0 rodou em paralelo com edições e foi descartada; o Postgres só tira 11 skips). Ruff limpo nos arquivos tocados. Postgres real (18 local descartável): **PROVADO** — 3 corridas × 3 rodadas, e a migração `b6d2e8f4a1c3` sobe/desce/sobe sem divergência com os modelos. Front: vitest `lib/agenda` + `components/agenda` 497 passed, typecheck limpo.
**NÃO PROVADO:** WhatsApp real (modelos não aprovados), SMTP real, cartão no Portal real, produção, a leitura da cobrança no Asaas real (status de estorno e lista `refunds`; só o sandbox prova o formato — qualquer formato inesperado cobra de novo).

## 5. Deploy (NÃO autorizado)
Migração `b6d2e8f4a1c3` → API → worker LOGO depois → Brain-Message-Frontend. `BRAIN_MESSAGE_PORTAL_URL` na API e no worker.

## 6. Revisão final e decisões de execução (2026-10-12)
Revisão independente da branch inteira (Opus): 1 Crítico e 5 Importantes, todos corrigidos com teste que falhava antes
(`tests/test_release_review_fixes.py`): resposta "reaproveitável" do Asaas vale 15 min e depois é perguntada de novo;
Remarcar depois de T + 24 h não é fechado como "sem resposta"; resposta fresca segura a varredura e trava "marcar
devolvido"; um sinal liberado uma segunda vez abre o próprio item de devolução; aceitar "Manter" com a devolução já
devida lê o Asaas antes (409 `deposit_not_reusable`); texto do toque numa oferta fechada por carry recusado não diz
mais que o sinal vale.

Decisões que tomei sem o dono (cada uma com o custo se estiver errada):
- Rebase dos commits de documento sobre `feature/out-of-mvp` `5231683` (a TASK-050 terminou depois do plano). Custo: nenhum, o reflog volta.
- O teste-guarda de canal (`test_channel_branches.py`) falhou com o cartão do Portal em `workers/shared`; movi para `workers/portal/release_card.py` em vez de afrouxar o teste.
- Três asserções de teste do plano estavam erradas (contagem de varredura, campo do cartão do Portal, nome de arquivo); corrigi só o teste.
- NÃO implementado: abortar uma reserva já cotada "sem sinal" se a transferência falhar depois (corrida de vários eventos; as causas realistas agora são bloqueadas) e manter a oferta em "remarcando" até a reserva concluir (o fechamento no portão é o desenho aprovado).

Adiado (Minor): avisos à clínica viram `given_up` em ~5 varreduras se o SMTP estiver vazio/fora; mensagens de Manter fora da janela usam o modelo de 3 botões; texto "sem nova cobrança" promete demais diante do fail-safe; `accept_keep` chama o Google com transação de leitura aberta; campo `refunds` ausente conta como "sem estorno" (provar no sandbox do Asaas antes do deploy); botão "Manter" fica morto até a aba da TASK-054 existir (publicar junto ou esconder).

## 7. A aba existe: "Consultas liberadas" no Brain-Message-Frontend (TASK-067 sessão 05, 2026-10-26 — LOCAL, não commitado)
O pendente "botão Manter fica morto até a aba existir" foi fechado: a tela consome §5.10 sem mudar rotas, escopo nem códigos
(`Brain-Message-Frontend/docs/CHECKPOINT_brain_message_consultas_liberadas.md`). Única mudança aqui, aditiva: `ReleaseOfferOut.held_cents`
= o que a clínica SEGURA pelo sinal (sinal + diferenças de serviço pagas − diferenças já pedidas de volta; a fórmula de
`service_change.held_deposit_cents`, em lote por `adjustment_queries.load_adjustment_totals`, uma consulta por página) — o mesmo valor que
os textos e o item de devolução já usam. `amount_cents` segue sendo o sinal original. Teste: `tests/test_release_offers_hub_held.py` (3).
Deploy (NÃO autorizado): API antes do front (o front aceita backend sem `held_cents`, cai no `amount_cents`).

## 8. Task 16 ligada: "clínica com secretária" usa o horário da clínica (TASK-067 sessão 06, 2026-10-26 — LOCAL, não commitado)
Na liberação de consulta com sinal PAGO, `api/hub/calendar.py::release_appointment` pergunta ao brain-api, ao vivo e sem cache,
`services/brain_staff.py::fetch_has_secretary(tenant_id)` → `GET {BRAIN_API_BASE_URL}/internal/tenants/{id}/staff-roles`
(`X-Internal-Api-Key`, `BRAIN_API_TIMEOUT_SECONDS`) → `{"roles": [...]}`; o resultado vai a `release_offer.open_offer(has_secretary=)`.
- `True` (há `secretary` ativa) → fechamento da CLÍNICA; `False` → do MÉDICO da consulta; `None` (sem configuração, rede/timeout,
  ≠ 200 incl. 404 e 5xx, JSON ou formato inválidos) → do MÉDICO (regra aprovada). A liberação nunca falha por isso.
- Chamada só com sinal PAGO e só depois do 409 `retention_ack_required`, ANTES do UPDATE que trava a linha (nenhuma trava
  durante a rede). Sinal pago durante o delete do Google (corrida já aceita) fica com `None` = horário do médico.
- "Ativa" é decidido no brain-api (`services/team.active_staff_roles`): não revogada, sem primeiro acesso/convite/vínculo pendente,
  conta pessoal (se houver) não desativada e sem senha inicial pendente. O papel do token de quem libera NÃO é usado.
- Sem feriados, sem outra política de prazo; E (T + 24 h) e K não mudam. Logs só com tenant, motivo e status/tipo da exceção.
Testes: `tests/test_brain_staff.py` (29: True/False/None, 401/403/404/422/5xx, timeout/rede, JSON e formatos inválidos, sem cache,
logs sem chave/URL/corpo), `tests/test_hub_release.py` (`test_t16_*`, 8: relógio fixo, B por resposta, brain-api 503 de verdade
pelo cliente, não pergunta sem sinal pago nem antes do aceite, avisos de B uma só vez com varredura repetida),
`tests/test_release_offer.py` (True/False/None e fuso `America/Manaus`). brain-api: `tests/test_internal_staff_roles.py` (15).
Prova de contrato (scratchpad da sessão, dados sintéticos): brain-api REAL (uvicorn + SQLite) ↔ rota REAL de liberação da secretarIA,
relógio fixo em ter 05/03/2030 14:00 (SP), clínica fecha 22:00 e médico 18:00 → Alfa com secretária ativa B = 20:00 (T+6 h);
Beta (secretária revogada/pendente), Gama (conta desativada), clínica desconhecida (404) e brain-api desligado → B = 17:30 (T+3h30).
Deploy (NÃO autorizado): qualquer ordem é segura — secretarIA nova com brain-api antigo recebe 404 → `None` → horário do médico
(comportamento de hoje); brain-api novo sozinho não é chamado por ninguém. Exige `BRAIN_API_BASE_URL`/`INTERNAL_API_KEY` na API.

## 9. Reauditoria dos adiados (TASK-067 sessão 07, 2026-10-26 — LOCAL, não commitado)
Cada achado antigo (§6 "Adiado" e a lista da TASK-066) classificado pela evidência ATUAL; corrigidos com teste vermelho antes e verde depois.
- **CORRIGIDO** — texto "sem nova cobrança" x modo de segurança: a oferta (`OFFER_DETAIL`, `OFFER_DETAIL_NO_KEEP`), a recusa do Manter (`KEEP_DECLINED_TEXT`) e o aviso à clínica antes de liberar (`warning_text`) prometiam o mesmo sinal ANTES da conferência no Asaas. Agora dizem que a conferência é feita ao confirmar (e, para a clínica, que sem confirmação é cobrado um sinal novo). Só `CARRIED_OVER_NOTE` (reuso confirmado) diz "Não há nova cobrança". Teste: `tests/test_release_offer.py::test_before_the_check_no_text_promises_the_same_sinal_without_a_new_charge`. O exemplo de `{{1}}` em `docs/LEMBRETES_MODELOS_META.md` foi atualizado (o modelo da Meta só tem a variável; nada a reaprovar por isso).
- **CORRIGIDO** — `accept_keep` chamava a agenda (Google) com a transação de leitura aberta: `release_keep._end_read` encerra a transação só-leitura antes de `check_availability` e de `create_event_exclusive`; a gravação (INSERT + `claim_keep`) continua numa transação só. Não abre corrida: as conferências eram leituras sem trava (READ COMMITTED) e o `claim_keep` atômico decide. Agenda nativa (`loc_…`) usa sessão própria e segue provada (`test_accept_keep_works_on_the_merged_native_agenda`). Teste: `tests/test_release_keep.py::test_no_database_transaction_is_open_while_the_calendar_answers`. Ainda aberto: a leitura do Asaas em `release_carry_check.check_offer_deposit` (só quando a devolução já venceu) roda com a leitura do depósito aberta, limitada a `CHECK_TIMEOUT_SECONDS` = 4 s.
- **CORRIGIDO** (F1, visto na s05) — `jobs.recover_paid_notices` mandava "Sinal recebido! Sua consulta está garantida" para sinal PAGO de consulta cancelada/liberada/inexistente (o processador não avisa nesses casos e deixa o marcador nulo). Agora só consulta viva (`LIVE_APPOINTMENT_STATUSES`), conferida na consulta em lote e de novo antes do envio. Teste: `tests/test_payment_cron_jobs.py::test_paid_notice_recovery_never_tells_a_cancelled_or_missing_consultation_it_is_safe`.
- **JÁ RESOLVIDO** — "Manter mostrado sem tela operacional": a aba existe (§7, s05). Não há (e não se criou) chave para esconder o Manter; o que vale é publicar API+worker e front na MESMA janela. Se a API for antes, um pedido de Manter sem decisão vence em K = 24 h como recusa (`keep_declined/timeout`) e a devolução fica devida — seguro, mas o paciente perde a chance de manter.
- **JÁ RESOLVIDO (prova atual)** — reagendamento concorrente com reserva e promoção de evento: suíte Postgres descartável verde na s07 (`test_before_postgres_concurrency.py` 4, `test_release_offer_postgres.py` 3, `test_payment_postgres_concurrency.py` 3). A promoção usa id de evento determinístico (repetição reencontra o mesmo evento; 409 do Google = sucesso).
- **RISCO RESTANTE (documentado)** — evento órfão: (a) `release_keep` perde o `claim_keep` e o `cancel_event` falha → evento nosso, vazio, fica na agenda (log `release_keep_orphan_event`); (b) promoção que criou o evento e caiu antes do commit, e depois falha de vez (horário passou) → o evento com id do depósito fica. `booking_promotion.promote` também chama o Google com a leitura aberta (fora do escopo pedido).
- **RISCO RESTANTE** — ganchos depois do commit: avisos de pagamento "depois" = pelo menos uma vez (`recover_paid_notices`; queda entre envio e marcador pode duplicar); ganchos da diferença paga = no máximo uma vez (sem varredura). Nenhuma promessa de exactly-once.
- **DIAGNOSTICADO, sem mudança (política)** — avisos que viram `given_up`: `release_offer_dispatch._give_back` desiste na 5ª falha (`MAX_ATTEMPTS = 5`, uma por varredura de ~poucos minutos); com SMTP vazio/fora o envio retorna falha (não "sem contato"), então os avisos à clínica desistem em ~5 varreduras e só ficam o item na tela e o log `release_offer_send_given_up`. Mudar quantidade/prazo ou tratar "SMTP não configurado" como "sem contato" é decisão do dono.
- **DIAGNOSTICADO, decisão do dono vigente** — fora da janela de 24 h, "manter aceito" e "manter recusado" saem pelo modelo `RELEASE_OFFER_TEMPLATE_NAME` com os 3 botões da oferta (Manter/Remarcar/Cancelar). É o desenho aprovado em 2026-10-12 (`docs/LEMBRETES_MODELOS_META.md`: o modelo é reaproveitado em toda mensagem da oferta; tocar "Manter" fora de hora responde com Remarcar/Cancelar), não um erro técnico; os botões não combinam com o texto de "manter aceito". Modelo próprio = decisão + aprovação Meta pendentes. Sem permissão de aviso pago, nada sai (`no_contact`).
- **DEPENDE DE PROVA EXTERNA** — `refunds` ausente na leitura do Asaas conta como "sem estorno" (`asaas_provider._refunds`, `release_carry_check.verdict_from`): não alterado; só o sandbox/produção prova o formato.
