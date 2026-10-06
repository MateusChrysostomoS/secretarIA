# CHECKPOINT — Lembretes R2: motor de lembretes (TASK-032)

Spec: `docs/superpowers/specs/2026-10-03-lembretes-e-confirmacao-design.md` §4.2.
Plano: `docs/superpowers/plans/2026-10-03-lembretes-r2-motor.md`. Base: R1 (`docs/CHECKPOINT_lembretes_r1.md`).

## Estado

- Local + commitado na branch `task/TASK-032-lembretes-e-confirmacao`. **Não pushado, não deployado.**
- Interruptor `Tenant.reminders_v2_enabled` desligado em todas as clínicas. Com ele desligado, as únicas mudanças de comportamento são: o cron antigo não tenta mais pacientes do Portal (não tinham `wa_id` e só geravam erro) e a remarcação zera o contador de confirmações (R1).
- Modelo `lembrete_consulta_v2` ainda **não submetido/aprovado** na Meta (folha: `docs/LEMBRETES_MODELOS_META.md`).

## O que entrou onde

| Peça | Onde |
|---|---|
| Texto único (frase da spec, requisitos, uma linha, 6 variáveis, botões, link do Portal) | `services/reminder_text.py` |
| Envio por canal (janela 24 h, modelo novo/Pix/simples com fallback, Portal chat + e-mail) | `services/reminder_delivery.py::deliver_reminder` |
| Cron de 1 min: seleção, reivindicação atômica, guardas, até 4 tentativas, falha de entrega, histórico, uso | `workers/reminder_engine.py::run_reminder_tick` |
| Ganchos depois do commit + reconciliação (backfill) a cada 10 min | `services/reminder_hooks.py` |
| Chamadas dos ganchos | fluxo (`_apply_flow_result`), Portal (`_promote_booking_hold`), IA (`_persist_appointment`, `_mark_appointment_cancelled`), botões (`_handle_action_button`), hub (`create_appointment`, `cancel_appointment`, `reschedule_appointment`); `PATCH status` é do R1 |
| Botões `remconfirm|`/`remcancel|`/`remother|` | decodificação `schemas/webhook.py::decode_action_id`; tratamento `workers/shared/reminder_actions.py::handle_reminder_button` |
| Cron antigo | `plugins/reminders.py`: pula clínicas com o interruptor ligado e pacientes sem WhatsApp |
| E-mail do Portal | `services/email.py` template `appointment_reminder_patient` |

## Decisões tomadas sem consulta

1. Ganchos rodam **depois** do commit, em transação própria (um erro de lembrete nunca desfaz um agendamento); a reconciliação de 10 min cura o que faltar e é o backfill ao ligar o interruptor.
2. Link do e-mail do Portal = `{BRAIN_MESSAGE_PORTAL_URL}/clinicas/?convite=<tenant_id>` montado aqui, sem chamar o brain-api (o brain-api já aceita o UUID da clínica como convite; nenhum contrato novo). Depende da variável `BRAIN_MESSAGE_PORTAL_URL` na API e no worker.
3. Consulta com sinal Pix pago mantém os 3 botões Confirmar/Reagendar/Cancelar; só o Confirmar passou para o id do lembrete (conta e é do paciente); Reagendar/Cancelar continuam com as regras do Pix.
4. Atraso máximo para enviar: lembrete extra 6 h, de 1 dia 3 h, de 1 hora 30 min; nunca depois do início da consulta.
5. Cancelar no lembrete mostra, por enquanto, Remarcar / Não vou mais (o R3 troca pelo cartão de três opções). Outro só convida a escrever (o R3 entrega à IA).
6. Varredura de expiração do sinal Pix não chama gancho (coordenação com Pix F2): as linhas dela são fechadas pela guarda do motor na hora de enviar.
7. Paciente do Portal sem e-mail: a mensagem fica na conversa e a linha vira `failed`/`no_email` com aviso de falha de entrega.
8. Linha presa em `sending` por mais de 10 minutos (worker morreu entre reivindicar e concluir) é recuperada pelo motor no início de cada tick (`reclaim_stuck_reminders`, `STUCK_SENDING_AFTER`): volta para `pending`; na 4ª tentativa vira `failed`/`engine_error` com aviso de falha de entrega.
9. Clínica com assinatura inativa: as linhas ficam `pending`, intocadas, enquanto estão dentro da janela de atraso; passada a janela são fechadas como `skipped`/`too_late` (para não encher o lote para sempre).
10. Entrega é "pelo menos uma vez". Se o envio deu certo e a contabilidade falha, a linha nunca volta para `pending`: o motor a marca `sent` numa sessão nova (resgate). Se isso também falhar, a linha fica `sending` e o recuperador a devolve a `pending` mais tarde — duplicata possível só nesse duplo erro (o resgate também não grava a cópia no histórico do WhatsApp).
11. O modelo simples de fallback dispara em qualquer erro de envio; uma falha ambígua (timeout/5xx depois de a Meta ter aceitado) pode, raramente, gerar um segundo lembrete.
12. Linhas de um horário antigo (gancho de remarcação perdido) nunca são enviadas: a guarda `stale_version` do motor as cancela. A reconciliação cura linhas que faltam, mas não aposenta as velhas, e o contador de confirmações não é zerado se o gancho de remarcação se perdeu.
13. O webhook de pagamento vencido/removido do Asaas que cancela uma consulta com sinal (`deposit_lifecycle`) não chama o gancho de fechamento (mesma isenção da varredura de expiração): as linhas são fechadas pela guarda `appointment_closed` na hora do envio.
14. Toque `rem*` em linha aposentada/cancelada não grava nada e responde `Essa consulta não está mais ativa.`; Confirmar só diz "Presença confirmada!" quando a linha ficou de fato registrada como confirmada.
15. Portal: a cópia no chat é decidida pelos dados, não pelo número da tentativa (`ReminderJob.chat_written`, `_chat_copy_exists`): com botões, mensagem enviada cujo cartão tem um id terminando em `|<id do lembrete>`; sem botões, mensagem enviada com o texto idêntico criada em/depois do `due_at` da linha. Assim uma 1ª tentativa que morreu antes do cartão não o perde, e uma nova tentativa nunca o duplica.

## Pendências

- Dono: submeter `lembrete_consulta_v2` à Meta; depois da aprovação ligar `REMINDER_V2_TEMPLATE_APPROVED=true` (API e worker).
- R3: decodificar no Portal os toques `rem*` (chamar `decode_action_id` + `handle_reminder_button`), cartão de três opções do Cancelar, Outro → IA, mensagem de abertura (`kind='chat'`).
- R4: avisos à clínica lendo `warn_due_at`/`warn_kind` — só linhas `sent`/`failed`; `failed` + `delivery_failed` = falha de entrega. Ler `warn_due_at` como está: o motor já o ajusta num envio atrasado (em `sent` com botões vale `max(prazo do R1, envio + atraso do R1)`, ou seja, hora +20 min, dia +2 h, extra +2 h contados do envio real; envio no horário mantém o valor do R1).
- TASK-030 P5: o prompt e o filtro da IA ainda proíbem falar de lembretes.
- `reminder_opt_out` continua sem tela para o paciente gravar (fora de escopo, spec §7).
- Dívida conhecida: a gravação da resposta em `remcancel`/`remother` é ler-modificar-gravar sem trava de linha.
- Dívida conhecida: a reconciliação processa no máximo 500 candidatas por tick, ordenadas pelo início da consulta.
- Dívida conhecida: a reconciliação não tem sinal de log para "bateu no limite".

## Validação (2026-10-06, `BOT_ALLOWLIST_WA_IDS=""`)

- Grupos do plano: 54 + 63 + 53 + 105 + 73 + 67 passed; `tests/test_workers_layering.py`: 15 passed.
- Suíte completa: 3325 passed, 10 skipped, 0 failed (7 min).
