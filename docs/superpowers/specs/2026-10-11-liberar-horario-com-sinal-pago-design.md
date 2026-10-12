# Liberar horário com sinal pago: manter, remarcar ou cancelar (TASK-052) — Design

**Data:** 2026-10-11; refeito em 2026-10-12 (2ª revisão do dono, mesmo dia) · **Estado:** para revisão ·
**Integração:** `feature/out-of-mvp` (NUNCA `main`) · **Base:** `task/TASK-050-secretaria` @ `3650e10` (worktree
`C:\TECH\BRAIN-worktrees\TASK-052\secretarIA`, branch `task/TASK-052-liberar-sinal-pago`) · **Risco:** ALTO (dinheiro,
agenda, migração, avisos programados, dois serviços com deploy separado) · **Produção:** NÃO AUTORIZADA.

> **Histórico.** 2026-10-11: desenho "Receber agora / Remarcar" (substituído). 2026-10-12 (1ª): Manter/Remarcar/Cancelar
> com e-mails A/B/C e conferência fixa em T + 24 h (substituído). **2026-10-12 (2ª, este documento):** WhatsApp sem
> e-mail; Cancelar avisa a clínica SEMPRE na hora; janelas T/S/F; lembrete em T + 24 h com regra de encerramento;
> Recebido / Não recebido disparado quando a CLÍNICA marca "devolvido"; estados e ação "marcar devolvido" expostos para
> a aba da TASK-054; ajuste do mesmo dia: em T + 24 h sem decisão final a oferta vira "Cancelado por falta de
> resposta" (terminal) e o sinal deve ser devolvido como no Cancelar. Continuam: tabela nova aditiva, transições por `UPDATE` guardado, idempotência dos envios, corridas
> provadas em Postgres descartável, texto da agenda corrigido, dias com horários segurados por Pix não pago (sem dado de
> outro paciente), modelos pagos da Meta como se aprovados, D15 (o sistema nunca devolve sozinho).

## 1. Intenção (decisões do dono, 2026-10-12)

Instantes: **T** = a clínica libera o horário; **S** = início da consulta liberada; **F** = fechamento − 30 min no dia
de T; **B** = `min(T + 6 h, F)`, nunca antes de T; **E** = T + 24 h.

1. **Em T** o paciente recebe, na conversa: "Devido à sua não confirmação da ida à consulta, a clínica liberou o seu
   horário para que você decida o que fazer", com **Manter horário**, **Remarcar**, **Cancelar**.
   - **Portal:** cartão na conversa + o e-mail de sempre ("a clínica escreveu") com LINK para a conversa.
   - **WhatsApp:** só a mensagem (cartão dentro da janela de 24 h; modelo pago fora dela). **Nenhum e-mail.**
2. **Manter horário** vale de T até S: verifica se a clínica já ocupou o horário; livre → a consulta volta, com o mesmo
   sinal, e a clínica recebe aviso (e-mail) com LINK para a conversa; ocupado → o paciente recebe Remarcar / Cancelar.
   Depois de S, ao abrir a conversa (Portal) o paciente vê só **Remarcar** e **Cancelar**.
3. **Remarcar:** mesmo médico e serviço; pergunta o dia e depois o horário; o sinal pago vai para a nova consulta, sem
   nova cobrança. Continua disponível depois de S.
4. **Cancelar:** segue a pergunta de motivo que já existe e, **na hora do toque, sempre** manda à clínica o aviso de
   devolução com o passo a passo no Asaas (estornar a própria cobrança, sem digitar chave: o dinheiro volta para a conta
   de origem; cuidado com saldo insuficiente logo após receber; guardar o comprovante). Continua disponível depois de S.
5. **Sem resposta** (ou Remarcar não concluído): aviso de devolução à clínica em B.
6. **Em E** (T + 24 h) sem decisão final, a oferta vira "Cancelado por falta de resposta" (terminal) e o sinal deve
   ser devolvido como no Cancelar. **Lembrete à clínica em E:** sai ("prazo vencido, devolva agora" nesse caso), reavaliado na hora do envio, EXCETO quando a clínica já marcou
   "devolvido" E o paciente já respondeu **Recebido** (tudo encerrado).
7. **Recebido / Não recebido:** perguntado ao paciente QUANDO A CLÍNICA MARCA "devolvido" (aba de consultas liberadas,
   TASK-054). Portal: cartão + e-mail com link; WhatsApp: mensagem. Recebido encerra tudo; Não recebido: "a clínica foi
   avisada de novo" e a clínica recebe novo aviso (no máximo 1 a cada 24 h por oferta). Não há mais envio fixo ao
   paciente em T + 24 h.
8. **Texto do reembolso ao paciente:** não cita banco; relembra o valor e diz "a conta de onde saiu o Pix".
9. **Fechamento:** clínica com secretária → horário da CLÍNICA; sem secretária → horário do MÉDICO da consulta.
10. **A TASK-052 guarda e expõe** os estados que a aba da TASK-054 lista (oferta paga/não paga, devolvida/pendente;
    decisão do paciente) e a ação "marcar devolvido". A tela é da TASK-054 (não planejada aqui).

**Sucesso** =
1. Liberar com sinal pago abre UMA oferta e não decide nada do dinheiro (sinal PAGO, nenhum item ainda); o paciente
   recebe os botões no canal dele; liberar com sinal não pago registra a liberação (para a aba) e segue como hoje.
2. Manter (antes de S, horário livre) recria a consulta com o mesmo sinal e avisa a clínica UMA vez com o link.
3. Cancelar abre UM item `released_refund_due` (se ainda não aberto) e manda o aviso à clínica na hora, uma vez.
4. Silêncio até B abre o mesmo item e avisa a clínica em B; o lembrete em E segue a regra do item 6.
5. Marcar "devolvido" (idempotente, só a própria clínica, gestor ou médico) pergunta ao paciente Recebido / Não
   recebido; Recebido encerra; Não recebido gera no máximo um aviso por 24 h.
6. Nenhum aviso sai duas vezes; nada sai antes de T; nada vaza entre clínicas ou entre pacientes da mesma clínica.

## 2. Escopo

- secretarIA: tabelas novas (oferta e envios programados) + coluna `tenants.has_secretary`; prazos; abertura na
  liberação; mensagens com botões (WhatsApp e Portal); os toques; Manter; transferência no Remarcar; prazo B, lembrete E
  e fechamento da decisão em E na varredura de pagamentos; endpoints para a aba (listar, marcar devolvido); aviso de
  horários segurados na escolha do dia; cartão "só Remarcar/Cancelar" ao abrir a conversa do Portal depois de S.
- Brain-Message-Frontend: só o texto do aviso de liberar com sinal pago no cartão da agenda.

## 3. Fora de escopo (de propósito)

- O sistema devolver dinheiro ou consultar o Asaas (D15). A tela/aba de consultas liberadas (TASK-054). Aprovar os
  modelos na Meta (pendência do dono). Como `has_secretary` é preenchido (§11). Cancelamento pela clínica e pelo
  paciente de consulta viva: como hoje. O agente de IA transferir o sinal. Pix pago depois da liberação: continua
  `paid_then_cancelled` (a oferta registrada como "não paga" mostra o status atual do sinal na aba).
- WhatsApp: não há evento "abriu a conversa"; o cartão antigo (3 botões) continua na tela e "Manter" depois de S
  responde com Remarcar / Cancelar.

## 4. O que o código mostra hoje (base `3650e10`)

| Fato | Onde | Consequência |
|---|---|---|
| Liberar trata como cancelamento DO PACIENTE (pode reter o sinal pago) | `api/hub/calendar.py::release_appointment` | Com sinal pago passa a abrir a oferta |
| Liberar apaga o evento do Google e cancela a linha; um id de evento apagado não pode ser reusado | `release_appointment`, `services/calendar.py::create_event` | Manter cria consulta e evento novos (id determinístico por oferta) |
| Horário livre: eventos do Google, reservas, consultas | `CalendarService.check_availability`, `booking_hold.live_hold_overlaps` | Manter checa os três; `is_slot_free` não serve (8–18 fixo) |
| Aviso ao paciente: job `send_cancellation_notice` (WhatsApp) ou mensagem + e-mail genérico com link (Portal) | `services/appointment_release.py`, `services/staff_patient_message.py::nudge_portal_patient`, `services/reminder_text.py::portal_conversation_link` | Botões novos entram aqui; Portal já tem o e-mail com link |
| O console da clínica abre uma conversa por `?conversationId=` | Brain-Message-Frontend `lib/portal-routes.ts::conversationHref` | Link no aviso à clínica: `{BRAIN_MESSAGE_PORTAL_URL}/chat/?conversationId=<id>` |
| E-mail da clínica: `Tenant.contact_email`; padrão reivindicar → enviar → devolver se não saiu | `services/payments/attention_alerts.py` | Mesmo padrão nos avisos novos |
| Horário: `Tenant.business_hours` e `Professional.business_hours` (NULL herda da clínica, `{}` fechado); fuso `Tenant.timezone`; sem feriados | `models/tenant.py`, `services/tenant_config.py::professional_business_hours` | F usa a clínica ou o médico (§5.2) |
| secretarIA não sabe se a clínica tem secretária (o papel só vem no token de quem chama) | `api/hub/deps.py`, brain-api | Coluna nova `tenants.has_secretary` (NULL = não sabido = médico) |
| Item de atenção resolvido com `refunded_in_provider_panel` grava `cancelado_reembolsado` no sinal; só gestor/médico resolve | `services/payments/attention_view.py::resolve_item`, `api/hub/payment_attention.py` | "Marcar devolvido" resolve o item da oferta pelo mesmo caminho |
| "Cancelar" do aviso pergunta o motivo (`rebookno` → `enter_decline_reasons`); "Outro horário" = `rebooksame` | `workers/shared/actions.py` | Reaproveitados |
| Ao entrar na conversa do Portal roda `process_brain_message_enter` | `workers/portal/open.py` | Gancho do cartão "só Remarcar/Cancelar" depois de S |
| Varredura de pagamentos a cada 4–6 min | `workers/payments_cron.py::run_payment_sweeps` | B, E e envios rodam nela (atraso ≤ ~6 min) |
| A lista de dias esconde dias com horários todos segurados | `services/flow_router.py::enter_day_picker` | Desfeito (§5.11) |
| Uma cabeça Alembic: `e2b7c4a9d1f5` | `migrations/versions/` | Migração nova `b6d2e8f4a1c3` |

## 5. Desenho

### 5.1 A oferta (`deposit_release_offers`, uma por consulta liberada COM sinal)

Dois eixos independentes, mais os instantes:

| Coluna | Valores |
|---|---|
| `deposit_paid` | o sinal estava pago em T? (`false` → `decision = not_offered`: só registro para a aba) |
| `decision` (paciente) | `none` → `rescheduling` → `rebooked`; `none`/`rescheduling` → `kept` / `cancelled`; `none`/`rescheduling` → `no_answer_cancelled` em E ("Cancelado por falta de resposta", terminal); `not_offered` |
| `first_contact_at` | primeiro toque do paciente num botão da oferta (Manter, Remarcar, Cancelar) — "contato"; só registro para a aba |
| `refund_status` (dinheiro) | `not_due` → `due` (Cancelar ou B) → `marked` (clínica marcou) → `received` / `not_received` (paciente); `due` → `clinic_decided` (clínica resolveu o item de outro jeito) |
| instantes | `released_at` T, `slot_start_at` S, `decide_by_at` B, `expires_at` E, `refund_due_at`, `refund_marked_at`, `patient_answered_at`, `last_reminder_at`, `after_start_card_at` |

Cada transição é UM `UPDATE ... WHERE <estado esperado>`; só um vence. O 409 `retention_ack_required` continua exigido e
diz: "O paciente já pagou o sinal (R$ X). Ao liberar o horário, ele poderá manter o horário, remarcar usando o mesmo
sinal ou cancelar. Se ele cancelar ou não responder até o prazo, a clínica receberá avisos para devolver o sinal por
inteiro pelo painel do provedor de pagamento." `deposit_outcome = choice_offered`.

### 5.2 Prazos (`services/release_deadlines.py`, puro; a escolha do horário em `release_offer`)

- **Qual horário:** `tenants.has_secretary = true` → `Tenant.business_hours`; senão (falso ou NULL) → o horário
  efetivo do médico da consulta (`professional_business_hours`, que herda da clínica quando o médico não tem o seu);
  consulta sem médico → horário da clínica.
- **F** = maior `end` das janelas do dia da semana de T, no fuso da clínica, − 30 min.
- **B** = `min(T + 6 h, F)` se F for DEPOIS de T; senão **T + 6 h**. Caso "clínica já fechada em T" (dia sem expediente,
  sem horário configurado, ou T depois de F): B = T + 6 h — o paciente mantém tempo para decidir e a clínica lê o aviso
  ao abrir; nada sai antes de T. Garantias: T ≤ B ≤ T + 6 h < E.
- **E** = T + 24 h (decisão do dono, igual ao desenho original): se em E a oferta continua sem decisão final, ela vira
  **terminal `no_answer_cancelled` ("Cancelado por falta de resposta")** e o sinal deve ser devolvido como no Cancelar
  (a clínica estorna no Asaas; o sistema NUNCA estorna — D15). Vale também para quem tocou e não concluiu: Manter que
  achou o horário ocupado/passado sem nova escolha, ou Remarcar iniciado e não concluído — em E, cancelado por falta de
  resposta. **Contato** = qualquer toque do paciente num botão da oferta (`first_contact_at`); texto livre na conversa
  NÃO conta. Remarcar e Cancelar ficam disponíveis até a oferta ser resolvida ou até E; depois de E a oferta está
  encerrada e não reabre.

### 5.3 A mensagem em T

> Devido à sua não confirmação da ida à consulta, a clínica liberou o seu horário para que você decida o que fazer.
>
> Recado da clínica: «justificativa» *(só se a clínica escreveu; é o que se corta para caber em 1024)*
>
> Você já pagou o sinal desta consulta (R$ 60,00). Toque em *Manter horário* para ficar com o mesmo horário, em
> *Remarcar* para escolher outro dia e horário usando o mesmo sinal, sem nova cobrança, ou em *Cancelar* para cancelar a
> consulta e receber o sinal de volta. Se você não responder até 20:00 de 05/03, a clínica será avisada para devolver o
> seu sinal.

Paciente que tinha confirmado: a 1ª frase vira "A clínica liberou o seu horário para que você decida o que fazer.".
Se S já passou em T: sem "Manter horário" (texto e botões). Botões: `relkeep|<id liberada>` "Manter horário",
`relresched|<id>` "Remarcar", `relcancel|<id>` "Cancelar". WhatsApp: cartão na janela; fora dela o modelo
`RELEASE_OFFER_TEMPLATE_NAME` (só com a autorização de mensagem paga; sem ela nada sai e B cobre). Portal: cartão + o
e-mail genérico com link. Paciente de WhatsApp NÃO recebe e-mail.

### 5.4 Manter horário (`services/release_keep.py`)

Vale com `decision ∈ {none, rescheduling}`, `refund_status = not_due`, agora < S e agora < B (sem resposta até B, a
clínica já foi avisada para devolver; §11-1). Ocupado = evento de outro no Google, reserva viva de outra conversa ou
consulta viva do mesmo médico (ou da clínica, se a consulta não tinha médico) sobre o horário. Livre → evento com id
`relkeep<hex da oferta>`, consulta nova (mesmo paciente, serviço, médico, convênio, pessoa atendida; confirmada), sinal
reapontado, aviso `clinic_patient_keeps` com link — numa transação com o `UPDATE` da oferta; perdendo a corrida, apaga
o evento criado. Respostas: livre "Pronto! Seu horário de «08/03 às 10:00» está mantido. O sinal que você já pagou
continua valendo para esta consulta."; ocupado "Esse horário já foi ocupado pela clínica. Você pode remarcar para outro
dia e horário ou cancelar a consulta." + Remarcar/Cancelar; depois de S "Esse horário já passou, então não dá mais para
mantê-lo. Você pode remarcar para outro dia e horário ou cancelar a consulta." + Remarcar/Cancelar.
**Portal depois de S:** ao entrar na conversa com a oferta aberta, o paciente recebe UMA vez esse cartão de 2 botões.

### 5.5 Remarcar

Com `decision = none`, `refund_status = not_due` e agora < B: vira `rescheduling` e entra no "Outro horário" com "Ótimo!
O sinal que você já pagou vale para a nova consulta."; a consulta nova recebe o sinal na mesma transação (`rebooked`), sem
nova cobrança e sem contar como remarcação do paciente; o portão do "antes" não pede Pix. Depois de B (devolução já
devida) ou de Cancelar: agendamento comum com "A devolução do seu sinal já foi pedida à clínica, então a nova consulta
segue as regras de sinal da clínica.". Depois de Manter/Remarcar concluído: "O sinal que você já pagou está valendo
para a sua consulta.".

### 5.6 Cancelar

Com `decision ∈ {none, rescheduling}` e agora < E: `decision = cancelled`; se `refund_status = not_due` → `due`
(`refund_source = patient`), abre o item e programa o lembrete E; se a devolução ainda não foi marcada, programa e envia
na hora `clinic_refund_cancel` (passo a passo do Asaas). Resposta: "Combinado, sua consulta foi cancelada. A clínica foi
avisada e fará a devolução do seu sinal de R$ 60,00." e segue a pergunta de motivo. Se a clínica já tinha marcado
devolvido: "Sua consulta foi cancelada. A clínica já informou que devolveu o seu sinal." (sem aviso novo). Repetido:
"A devolução do seu sinal já foi pedida à clínica.". Depois de Manter/Remarcar concluído: "O sinal que você já pagou está
valendo para a sua consulta. Para cancelar essa consulta, use as opções da própria consulta.". Depois de E (qualquer
botão da oferta: Manter, Remarcar ou Cancelar): "O prazo de 24 horas para responder terminou e a consulta foi
cancelada por falta de resposta. A clínica foi avisada para devolver o seu sinal. Para marcar uma nova consulta, é só me
escrever." — nada muda, nada reabre.

### 5.7 Sem resposta (B) e fim da decisão (E)

A varredura: (1) `decision ∈ {none, rescheduling}`, `refund_status = not_due`, B vencido → `refund_status = due`
(`timeout`), item, `clinic_refund_noanswer` (em B) e lembrete E; (2) `decision ∈ {none, rescheduling}`, E vencido →
`decision = no_answer_cancelled` (garante também a devolução devida, se por algum motivo ainda não estava). Um toque que
chega depois de B/E e antes da varredura aplica a mesma transição antes de responder.

### 5.8 Avisos à clínica (`Tenant.contact_email`)

Sem nome, telefone ou e-mail do paciente; com data/hora da consulta, valor e LINK da conversa no console. Os de
devolução trazem o passo a passo:

> Como devolver pelo Asaas: 1. No painel do Asaas, abra Cobranças e encontre a cobrança Pix deste sinal (R$ X, paga em
> DD/MM). 2. Abra a cobrança e escolha estornar o valor total. Não é preciso digitar chave Pix: o estorno devolve o
> dinheiro para a mesma conta de onde o paciente pagou. 3. Atenção: o estorno usa o saldo da sua conta no Asaas; logo
> depois do pagamento o valor pode ainda não estar disponível (saldo insuficiente) — nesse caso aguarde a liberação e
> tente de novo. 4. Guarde o comprovante do estorno. 5. Marque a devolução como feita na aba de consultas liberadas; o
> paciente será perguntado se recebeu.

| `kind` | Quando | Vale no envio se |
|---|---|---|
| `clinic_refund_cancel` | toque Cancelar | `refund_status = due` |
| `clinic_refund_noanswer` | B | `refund_status = due` e a decisão não é `cancelled` |
| `clinic_refund_day` | E | `refund_status ∈ {due, marked, not_received}` (não sai com `received` nem `clinic_decided`); com `decision = no_answer_cancelled` o texto é "prazo vencido (24 h sem resposta): devolva agora" |
| `clinic_refund_reminder` (seq n) | toque Não recebido | `refund_status = not_received` |
| `clinic_patient_keeps` | Manter deu certo | `decision = kept` |

### 5.9 Marcar devolvido e a conferência do paciente

Ação da clínica (§5.10): resolve o item da oferta com `refunded_in_provider_panel` (o sinal vira
`cancelado_reembolsado`), `refund_status = marked`, e programa/envia na hora `patient_check`: "A clínica informou que
devolveu o seu sinal de R$ 60,00. Confira a sua conta (a devolução vai para a conta de onde saiu o Pix) e nos conte:" com
**Recebido** (`relgot|<id>`) / **Não recebido** (`relmiss|<id>`). Portal: cartão + e-mail com link; WhatsApp: cartão na
janela, modelo `REFUND_CHECK_TEMPLATE_NAME` fora (com `paid_notice_allowed`). Resolver o item pela tela atual de
pagamentos com "devolvido no painel" tem o mesmo efeito; com outra decisão → `clinic_decided` (nada mais é enviado).
- Recebido (`marked`/`not_received` → `received`): "Que bom! Obrigada por confirmar." e nada pendente sai.
- Não recebido (`marked`/`not_received` → `not_received`): "Avisamos a clínica novamente. Ela vai conferir a devolução
  do seu sinal." + um `clinic_refund_reminder` (no máximo 1 a cada 24 h; dentro disso "A clínica já foi avisada
  novamente há pouco.").
- Antes da marcação: "A clínica ainda não informou a devolução do seu sinal."; depois de `received`: "Esse pagamento já
  está encerrado.".

### 5.10 Contrato mínimo para a aba da TASK-054 (hub)

- `GET /tenants/me/release-offers?pending_only=false&limit=100` — qualquer papel da clínica, filtrado pela visibilidade
  de agenda (`get_agenda_viewer`: quem só vê os seus vê só os seus). Itens (mais novos primeiro): `id`,
  `released_appointment_id`, `new_appointment_id`, `conversation_id`, `patient_display_name`, `professional_id`,
  `service_label`, `slot_start`, `slot_end`, `amount_cents`, `deposit_paid_at_release`, `deposit_status` (status ATUAL
  do sinal), `refund_status`, `refund_source`, `decision`, `patient_decision` (`no_answer` | `kept` | `rescheduled` |
  `cancelled` | `cancelled_no_answer` | `received` | `not_received` | `not_offered`), `first_contact_at`, `released_at`, `decide_by_at`, `expires_at`,
  `refund_due_at`, `refund_marked_at`, `patient_answered_at`, `can_mark_refunded`. `pending_only` = devolução `due` ou
  `not_received`, ou decisão em aberto.
- `POST /tenants/me/release-offers/{id}/refunded` com `{"resolved_by_name": "...", "note": "..."(opcional)}` — gestor ou
  médico (`require_clinic_clinician`; secretária 403, igual a resolver item), só a própria clínica (outra = 404),
  corpo lido à mão (nunca ecoa a nota). `refund_status = due` → marca e responde 200 com o item; já `marked`/`received`/
  `not_received` → 200 com o estado atual (idempotente, nada reenviado); `not_due`/`clinic_decided` → 409
  `{"detail": {"code": "refund_not_due"}}`.

### 5.11 Dias com horários segurados (sem mudança)

A lista de dias volta a NÃO descontar reservas; ao escolher o dia, os horários segurados por OUTRA conversa ficam fora e
o topo diz só quais ("Os horários 08:00 e 08:30 deste dia estão reservados para outro paciente, aguardando a confirmação
ou o pagamento do sinal. Se o prazo terminar sem pagamento, eles voltam a ficar livres."); dia todo segurado: texto
próprio e só "Outro dia". Até 8 horários, depois "e mais N". Nunca dado de outro paciente.

### 5.12 Modelos da Meta (PENDÊNCIA)

`RELEASE_OFFER_TEMPLATE_NAME = "liberacao_sinal_pago_v1"` (1 parâmetro, 3 botões) e `REFUND_CHECK_TEMPLATE_NAME =
"devolucao_sinal_conferir_v1"` (1 parâmetro, 2 botões), em `config.py`, marcados PENDÊNCIA META; usados como se
aprovados. Sem aprovação, o envio fora da janela falha e é registrado; os avisos à clínica seguem.

### 5.13 Envio idempotente (`deposit_release_emails`)

Uma linha por envio, `(offer_id, kind, seq)` ÚNICO. Quem envia reivindica (`UPDATE ... WHERE claimed_at IS NULL`) e
comita ANTES de enviar; revalida pela tabela do §5.8 (ou, para `patient_check`, `refund_status = marked`); sem contato →
`skipped_no_contact`; não saiu → devolve a reivindicação (até 5 tentativas, depois `given_up` com alarme); envio
interrompido fica `sending` (no máximo uma vez). Logs: ids, `kind`, `outcome`.

## 6. Combinações (todas com teste nomeado no plano)

| Sequência | Resultado |
|---|---|
| Manter antes de S, livre | `kept`; consulta recriada com o sinal; 1 `clinic_patient_keeps` |
| Manter, clínica reocupou o horário | nada muda; Remarcar/Cancelar |
| Manter depois de S | nada muda; Remarcar/Cancelar |
| Manter depois de B sem resposta | recusado ("o prazo para decidir terminou") |
| Manter depois de Cancelar / Manter repetido | recusado / "já está mantida" (1 consulta, 1 aviso) |
| Cancelar antes de S / depois de S | `cancelled`, `due`, 1 item, `clinic_refund_cancel` na hora, lembrete E |
| Cancelar depois de B (sem resposta antes) | `cancelled`; `clinic_refund_cancel` na hora (o de B já saiu); 1 item |
| Cancelar depois de a clínica marcar devolvido | `cancelled`; nenhum aviso novo |
| Cancelar repetido / depois de Manter / depois de E | "já pedida" / "use as opções da consulta" / "prazo terminou" |
| Remarcar antes de B e conclui | `rebooked`; sinal transferido; nada programado |
| Remarcar sem terminar até B | `due` (`timeout`); `clinic_refund_noanswer` em B |
| Remarcar depois de S (antes de B) | permitido, com o sinal |
| Remarcar depois de B ou de Cancelar | agendamento comum com a frase da devolução |
| Sem nenhum contato | B: aviso; E: `no_answer_cancelled` + aviso "prazo vencido, devolva agora" |
| Remarcar iniciado e não concluído até E | B: aviso; E: `no_answer_cancelled` (contato registrado) |
| Manter com horário ocupado e nenhuma nova escolha até E | E: `no_answer_cancelled` |
| Qualquer botão depois de E | "prazo terminou... cancelada por falta de resposta"; nada muda |
| Clínica marca devolvido com devolução devida | `marked`; paciente perguntado uma vez |
| Clínica marca devolvido antes de o paciente decidir | 409 `refund_not_due` |
| Clínica marca devolvido duas vezes | 200 nas duas; 1 pergunta ao paciente |
| Clínica marcou e paciente não respondeu até E | lembrete E sai |
| Clínica marcou e paciente respondeu Recebido antes de E | lembrete E NÃO sai |
| Não recebido ×2 em < 24 h | 1 aviso; o lembrete E continua valendo |
| Recebido antes de a clínica marcar | "a clínica ainda não informou"; nada muda |
| Clínica resolve o item com outra decisão | `clinic_decided`; nada mais sai |
| Toque de outro paciente / outra clínica / id nunca mostrado no Portal | "Não encontrei essa consulta." / nada muda |
| Duas instâncias da varredura | cada envio sai uma vez |

## 7. Erros e corridas

| Situação | O que acontece |
|---|---|
| Cancelar × varredura em B | o `UPDATE` de `refund_status` tem guarda; 1 item; o aviso de Cancelar sai de qualquer jeito (§5.6) |
| Manter × varredura | o evento é criado antes do `UPDATE`; perdendo, é apagado |
| Remarcar concluído × varredura | um `UPDATE` vence (provado em Postgres real) |
| Duas instâncias enviando | reivindicação com `claimed_at IS NULL` (provado em Postgres real) |
| SMTP fora / Meta recusa | a linha volta a ficar devida; até 5 tentativas |
| API nova + worker velho | worker velho manda o cartão velho e não trata `rel*`; B/E/envios só no worker novo → worker LOGO depois da API |

## 8. Testes

No plano: um teste nomeado por linha das §6/§7, mais prazos (clínica/médico, fechada, fuso), textos (≤ 1024, ≤ 20 por
botão, sem promessa de "devolvemos", sem banco), canais (WhatsApp sem e-mail; Portal com e-mail), contrato da aba e o
texto do front.

## 9. Dependências e deploy

Base `task/TASK-050-secretaria` @ `3650e10`; integra em `feature/out-of-mvp` depois da TASK-050. A TASK-054 consome
§5.10. Deploy (NÃO autorizado): migração `b6d2e8f4a1c3` → API → worker LOGO depois → front. `BRAIN_MESSAGE_PORTAL_URL`
configurado na API e no worker (links). Pendência externa: modelos na Meta.

## 10. Decisões tomadas (padrão) — o dono pode trocar qualquer uma

- Liberar com sinal pago nunca retém, qualquer que seja a política de cancelamento.
- `has_secretary` NULL (não sabido) conta como "sem secretária" (horário do médico).
- Clínica já fechada em T → B = T + 6 h.
- Manter vale até o MENOR entre S e B; Remarcar com o mesmo sinal vale até B; Cancelar até E.
- Em E, oferta sem decisão final (com ou sem contato) vira `no_answer_cancelled`; contato = toque num botão da oferta.
- Manter recria a consulta (linha e evento novos) e conta como confirmação do paciente.
- Cancelar depois de a clínica já marcar devolvido não gera aviso.
- Item resolvido com outra decisão (crédito, remarcado, outro) encerra os avisos (`clinic_decided`).
- O aviso do Asaas usa o saldo; o texto avisa do saldo insuficiente e pede o comprovante.
- Não recebido: no máximo um aviso à clínica a cada 24 h por oferta.
- Clínica sem `contact_email`: aviso `skipped_no_contact`, não reenviado depois.
- Avisos à clínica por e-mail (o "mensagem à clínica" do pedido = e-mail com link para o console).
- Liberação com sinal não pago fica registrada (`not_offered`) só para a aba listar.
- Marcar devolvido: gestor ou médico (mesma regra de resolver item).

## 11. Perguntas em aberto (para o dono)

1. Manter depois de B (sem resposta até B, mas antes de S): recusar (adotado) ou permitir e avisar a clínica para NÃO
   devolver?
2. Como o sistema sabe que a clínica tem secretária? (adotado: coluna `tenants.has_secretary`, ainda sem tela; NULL =
   médico). Preencher pelo brain-api (papéis) ou por uma opção no Contexto?
3. A secretária pode marcar "devolvido"? (adotado: não, igual a resolver item hoje)
4. Feriados não existem no sistema; um feriado num dia útil conta como aberto.

## 12. Não provado / bloqueios

Nada impede planejar. NÃO PROVADO até a execução: WhatsApp real (modelos nem aprovados), SMTP real, cartão no Portal
real, produção. A tela da aba é da TASK-054.
