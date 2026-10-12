# Liberar horário com sinal pago: manter, remarcar ou cancelar (TASK-052) — Design

**Data:** 2026-10-11, refeito em 2026-10-12 (novo desenho do dono) · **Estado:** para revisão · **Integração:**
`feature/out-of-mvp` (NUNCA `main`) · **Base:** `task/TASK-050-secretaria` @ `3650e10` (worktree
`C:\TECH\BRAIN-worktrees\TASK-052\secretarIA`, branch `task/TASK-052-liberar-sinal-pago`) · **Risco:** ALTO (dinheiro,
agenda, migração, e-mails programados, dois serviços com deploy separado) · **Produção:** NÃO AUTORIZADA.

> **O que mudou em 2026-10-12.** O desenho anterior ("Receber agora" / "Remarcar", item de atenção aberto 24 h depois
> de silêncio, nenhum e-mail novo à clínica, nenhum aviso no vencimento) foi **substituído** por decisão do dono. Ficam
> do desenho anterior: a tabela nova aditiva (uma oferta por consulta liberada), as transições por `UPDATE` guardado,
> a transferência do sinal ao remarcar, a corrida provada em Postgres descartável, o texto da agenda corrigido, a lista
> de dias que mostra dias com horários segurados por Pix não pago (sem dado de outro paciente) e o modelo pago da Meta
> fora da janela de 24 h.

## 1. Intenção (decisão do dono, 2026-10-12)

T = instante em que a clínica libera o horário de uma consulta cujo sinal Pix **já está pago**.

1. **Em T:** a agenda mostra o aviso de liberar (já existe; texto corrigido) e o paciente recebe a mensagem na conversa
   e um e-mail com LINK para a conversa.
2. **Na conversa:** "Devido à sua não confirmação da ida à consulta, a clínica liberou o seu horário para que você
   decida o que fazer", com os botões **Manter horário**, **Remarcar** e **Cancelar**.
   - **Manter horário:** o sistema verifica se a clínica já ocupou aquele horário. Livre → a consulta volta para o
     mesmo horário (com o mesmo sinal) e a clínica recebe um e-mail avisando que o paciente insiste no horário, com
     LINK para a conversa dele. Ocupado → o paciente é avisado e recebe de novo **Remarcar** / **Cancelar**.
   - **Remarcar:** fluxo já definido — mesmo médico e mesmo serviço; pergunta o novo dia e depois o horário; o sinal
     pago vai para a nova consulta sem nova cobrança.
   - **Cancelar:** segue o fluxo que já existe (só pergunta o motivo) e, no instante C do toque, a clínica recebe o
     e-mail A (devolução devida) e ficam programados os e-mails B e C.
3. **Prazos da devolução** (a clínica devolve pelo painel do Asaas; o sistema NUNCA devolve sozinho — D15):
   - **A:** no toque em Cancelar (instante C).
   - **B:** em `min(T + 6 h, 30 min antes do fechamento da clínica no dia de T)`.
   - **C:** em T + 24 h.
   - Paciente que **não responde** (ou toca Remarcar e não termina até B) = cancelamento no prazo B: o primeiro e-mail
     à clínica sai em B (não há A) e o C em T + 24 h.
   - Exemplo do dono: T = 14:00, paciente cancela às 15:00 → A às 15:00, B às 20:00 (ou 30 min antes de fechar, se
     vier antes), C às 14:00 do dia seguinte.
4. **Em T + 24 h, o paciente** recebe um e-mail dizendo que a clínica foi avisada para devolver o sinal de R$ X e
   pedindo que confira o banco, com LINK para a conversa, onde há uma mensagem com **Recebido** / **Não recebido**.
   - Recebido: encerra (fecha todas as possibilidades do pagamento).
   - Não recebido: a secretária responde que a clínica foi avisada de novo e manda mais um e-mail à clínica.
5. **Janela de 24 h do WhatsApp:** o código usa modelos (templates) como se estivessem aprovados, com o nome numa
   configuração claramente marcada; a aprovação na Meta é pendência.
6. **Tudo idempotente:** um envio por e-mail por liberação, mesmo com retry ou duas instâncias; o que está programado é
   descartado quando o paciente remarca, mantém, ou a clínica dá a devolução por feita; nenhum dado pessoal
   desnecessário em log.

**Sucesso** =
1. Liberar uma consulta com sinal pago abre UMA oferta, não decide nada sobre o dinheiro (o sinal continua pago,
   nenhum item de atenção ainda) e manda a mensagem com os três botões (WhatsApp dentro da janela; modelo pago fora
   dela com a autorização da clínica; Portal sempre) e o e-mail com link.
2. Manter com o horário livre recria a consulta no mesmo horário, com o mesmo sinal, e manda UM e-mail à clínica com o
   link da conversa; com o horário ocupado, nada muda e o paciente recebe Remarcar / Cancelar.
3. Cancelar (antes de B) abre UM item de devolução, manda A na hora e programa B e C; tocar de novo não repete nada.
4. Silêncio (ou Remarcar sem terminar) até B abre o mesmo item e manda o primeiro e-mail em B; C em T + 24 h.
5. Em T + 24 h, com a devolução ainda devida e não dada por feita, o paciente recebe o e-mail e a mensagem com
   Recebido / Não recebido; Recebido encerra; Não recebido manda mais um e-mail à clínica (no máximo um a cada 24 h).
6. Nenhum e-mail sai duas vezes; nenhum sai antes de T; nada vaza entre clínicas ou entre pacientes da mesma clínica.

## 2. Escopo

- secretarIA: tabelas novas (oferta e e-mails programados), cálculo dos prazos com o horário da clínica, abertura da
  oferta na liberação, mensagem com três botões (WhatsApp e Portal), os toques (Manter / Remarcar / Cancelar /
  Recebido / Não recebido), a recriação da consulta no Manter, a transferência do sinal no Remarcar, o prazo B e o
  envio dos e-mails pela varredura de pagamentos que já existe, o aviso de horários segurados na escolha de dia.
- Brain-Message-Frontend: só o texto do aviso de liberar com sinal pago no cartão da agenda.

## 3. Fora de escopo (de propósito)

- O sistema devolver dinheiro (D15) ou consultar o Asaas para descobrir a conta de origem do Pix (§10).
- Cancelamento pela clínica (`POST .../cancel`) e cancelamento pelo paciente de uma consulta viva: como hoje.
- Aprovar os modelos novos na Meta (pendência do dono; §5.11).
- A tela "Pagamentos que precisam de decisão" (F4): continua sendo onde a clínica registra a devolução; enquanto não
  existe, a agenda mostra "precisa de decisão" no cartão (já existe).
- O agente de IA transferir o sinal: só os caminhos determinísticos transferem.
- Pix pago DEPOIS da liberação (em voo): continua `paid_then_cancelled`, sem oferta.
- Link de conversa de WhatsApp no e-mail do paciente de WhatsApp: o sistema não guarda o número de exibição da clínica
  (§11, pergunta 3).

## 4. O que o código mostra hoje (base `3650e10`)

| Fato | Onde | Consequência |
|---|---|---|
| Liberar trata a liberação como cancelamento DO PACIENTE (pode reter o sinal pago) | `api/hub/calendar.py::release_appointment` chama `deposit_lifecycle.on_appointment_cancelled` sem `cancelled_by` | Com sinal pago passa a abrir a oferta (§5.1) |
| Liberar apaga o evento do Google e marca a consulta CANCELADA | `release_appointment` (`calendar.cancel_event`, `UPDATE ... CANCELLED`) | "Manter" não reabre a linha: cria consulta e evento novos no mesmo horário (§5.4) |
| Um id de evento apagado no Google nunca mais pode ser usado | `services/calendar.py::CalendarService.create_event` | O evento do Manter usa id próprio, determinístico por oferta (`relkeep<hex da oferta>`) |
| Verificar se o horário está livre: eventos do Google, reservas, consultas | `CalendarService.check_availability`, `booking_hold.live_hold_overlaps`, `booking_promotion.slot_conflict` (padrão) | O Manter checa os três (§5.4); `is_slot_free` NÃO serve (horário fixo 8–18) |
| Aviso ao paciente: job `send_cancellation_notice` (WhatsApp) ou mensagem + e-mail genérico (Portal) | `services/appointment_release.py`, `workers/whatsapp/notifications.py` | Os botões novos entram aqui (§5.3) |
| O e-mail do Portal leva o link de convite da clínica, que abre a conversa do paciente | `services/reminder_text.py::portal_conversation_link` | É o "link para a conversa" do paciente do Portal |
| Paciente de WhatsApp não recebe e-mail na liberação; `Patient.email` existe (opcional) | `appointment_release.py`, `models/patient.py` | E-mail novo para ele, sem link (§5.3, §11) |
| O console da clínica abre uma conversa por `?conversationId=` | Brain-Message-Frontend `lib/portal-routes.ts::conversationHref` | Link da clínica: `{BRAIN_MESSAGE_PORTAL_URL}/chat/?conversationId=<id>` |
| E-mail da clínica: `Tenant.contact_email`; envio SMTP que diz se saiu | `services/email.py::send_paid_cancellation_alert`, `services/payments/attention_alerts.py` | Mesmo padrão: reivindicar, enviar, devolver a reivindicação se não saiu |
| Horário da clínica: `Tenant.business_hours` = `{"monday": [{"start": "08:00", "end": "12:00"}, ...]}`; sem feriados; fuso em `Tenant.timezone` | `models/tenant.py`, `schemas/config.py` | Fechamento do dia = maior `end` das janelas daquele dia da semana (§5.2) |
| "Cancelar" do aviso já pergunta o motivo (`rebookno` → `enter_decline_reasons`) | `workers/shared/actions.py::_handle_action_button`, `services/flow_router.py` | O Cancelar novo continua nesse caminho |
| "Remarcar" do aviso = "Outro horário" (`rebooksame` → `enter_rebooking`) | `workers/shared/actions.py` | Reaproveitado, com a frase do sinal no topo |
| Devolução dada por feita: resolver o item com `refunded_in_provider_panel` (deposito vira `cancelado_reembolsado`) ou o Asaas avisar estorno (`provider_refund_seen_at` no item) | `services/payments/attention_view.py::resolve_item`, `services/payments/event_processor.py::_on_refunded` | É a "marca da clínica" que encerra os avisos (§5.12) |
| Varredura de pagamentos a cada 4–6 min | `workers/payments_cron.py::run_payment_sweeps` (`SWEEP_MINUTES`) | Prazo B e e-mails programados rodam nela; atraso máximo ≈ 6 min (§10) |
| A lista de dias ESCONDE dias cujos horários livres estão todos segurados | `services/flow_router.py::enter_day_picker` (`holds=`) | Desfeito (§5.10) |
| Uma cabeça Alembic: `e2b7c4a9d1f5` | `migrations/versions/` | Migração nova `b6d2e8f4a1c3` |

## 5. Desenho

### 5.1 A oferta (tabela `deposit_release_offers`, uma por consulta liberada)

Ao liberar (depois do `UPDATE ... CANCELLED` vencedor, na mesma transação), com o sinal da consulta PAGO,
`services/release_offer.py::open_offer` grava a oferta e **não** chama `on_appointment_cancelled`: nada é retido, nada é
devolvido, nenhum item ainda. Colunas de tempo: `released_at` (T), `decide_by_at` (B), `refund_check_at` (T + 24 h).
Também guarda `paid_notice_allowed` (a autorização de mensagem paga dada na liberação, reaproveitada em T + 24 h).

Estados (cada mudança é UM `UPDATE ... WHERE state IN (...)`; só um ganha):

| Estado | Significa | Sai para |
|---|---|---|
| `open` | aguardando a escolha | `rescheduling`, `kept`, `refund_due` |
| `rescheduling` | tocou Remarcar, escolhendo dia/horário | `rebooked`, `kept`, `refund_due` |
| `kept` (final) | Manter deu certo; o sinal está na consulta recriada (`new_appointment_id`) | — |
| `rebooked` (final) | a nova consulta levou o sinal (`new_appointment_id`) | — |
| `refund_due` | Cancelou (`refund_source = patient`, em C) ou passou de B (`timeout`); UM item `released_refund_due` aberto | `refund_closed` |
| `refund_closed` (final) | `closed_reason = patient_received` (Recebido) ou `clinic_settled` (a clínica deu por feita) | — |

O 409 `retention_ack_required` continua exigido antes de liberar e passa a dizer: "O paciente já pagou o sinal
(R$ X). Ao liberar o horário, ele poderá manter o horário, remarcar usando o mesmo sinal ou cancelar. Se ele cancelar ou
não responder até o prazo, a clínica receberá e-mails para devolver o sinal por inteiro pelo painel do provedor de
pagamento." `deposit_outcome` = `choice_offered` (409 e 200).

### 5.2 Os prazos e o horário da clínica (`services/release_deadlines.py`, puro)

- `decide_by(T)`: no fuso da clínica, `fechamento` = maior `end` das janelas de `business_hours` no dia da semana de T.
  - Se há `fechamento` e `fechamento − 30 min` é DEPOIS de T: B = `min(T + 6 h, fechamento − 30 min)`.
  - Senão (dia sem expediente, nenhum horário configurado, ou T já depois de `fechamento − 30 min`): B = T + 6 h.
  - Garantias: T ≤ B ≤ T + 6 h < T + 24 h. Nada sai antes de T; B nunca passa de C.
- `refund_check_at(T)` = T + 24 h (e-mail C e conferência do paciente).
- Por que T + 6 h quando a clínica já fechou (e não B = T): com B = T o paciente não teria tempo nenhum para decidir e a
  clínica receberia o aviso fechada, sem poder agir; com T + 6 h o paciente decide, e a clínica lê o aviso ao abrir. É a
  escolha que nunca encurta o direito do paciente e nunca adianta dinheiro (§11, pergunta 1).

### 5.3 A mensagem em T (texto exato) e os canais

Corpo (≤ 1024; se a clínica escreveu um recado longo, ele é cortado, nunca a explicação):

> Devido à sua não confirmação da ida à consulta, a clínica liberou o seu horário para que você decida o que fazer.
>
> Recado da clínica: «justificativa» *(só se a clínica escreveu)*
>
> Você já pagou o sinal desta consulta (R$ 60,00). Toque em *Manter horário* para ficar com o mesmo horário, em
> *Remarcar* para escolher outro dia e horário usando o mesmo sinal, sem nova cobrança, ou em *Cancelar* para cancelar
> a consulta e receber o sinal de volta. Se você não responder até 20:00 de 05/03, a consulta será cancelada e a
> clínica fará a devolução do sinal.

Se a consulta já tinha sido confirmada pelo paciente (a clínica liberou mesmo assim), a primeira frase vira "A clínica
liberou o seu horário para que você decida o que fazer." (a frase do dono seria falsa nesse caso).

Botões: `relkeep|<id liberada>` "Manter horário" (14), `relresched|<id>` "Remarcar" (8), `relcancel|<id>` "Cancelar" (8).

- **WhatsApp** (job `send_cancellation_notice`, MESMA assinatura): com oferta `open`, dentro da janela → cartão com os
  três botões; fora → modelo `RELEASE_OFFER_TEMPLATE_NAME` (um parâmetro = o corpo; três botões de resposta rápida com os
  mesmos ids), só com a autorização de mensagem paga; sem ela, nada sai (como hoje) e o prazo B cobre.
- **Portal:** cartão gravado com os três botões (`BrainMessageSender.send_buttons`) + o e-mail genérico de sempre
  ("a clínica escreveu", com o link da conversa). A família `rel*|` passa a ser AÇÃO no Portal quando o id apareceu num
  cartão recente da própria conversa.
- **E-mail do paciente de WhatsApp** (novo, só se `Patient.email`): "A clínica liberou o horário da sua consulta... você
  decide pelo WhatsApp da clínica (Manter / Remarcar / Cancelar) até «B»". Sem link (§3).

### 5.4 Manter horário (`services/release_keep.py::keep_slot`)

Recusado (texto e nada muda) quando: a oferta não é do paciente que tocou; está `kept` ("Sua consulta já está mantida
para «data hora»."), `rebooked`, `refund_due`/`refund_closed` ("Você já pediu o cancelamento..."/"O prazo para manter
terminou..."), ou o horário já começou ("Esse horário já passou.", com Remarcar / Cancelar).

Senão, no calendário DONO da consulta liberada (`booking_promotion.calendar_for_booking`):
1. Ocupado se: algum evento do Google sobrepõe `[início, fim)` (exceto o próprio evento determinístico do Manter, para o
   retry), ou uma reserva viva de outra conversa (`live_hold_overlaps`), ou uma consulta viva da mesma clínica e do mesmo
   profissional sobrepõe o horário. Ocupado → "Esse horário já foi ocupado pela clínica. Você pode remarcar para outro
   dia e horário, com o mesmo sinal, ou cancelar e receber o sinal de volta." + botões Remarcar / Cancelar. Nada muda.
2. Livre → cria o evento com id `relkeep<hex da oferta>` (determinístico: um retry não duplica) e, numa transação:
   `UPDATE` da oferta `open|rescheduling → kept` (e `decide_by_at > agora`), `INSERT` da consulta nova (mesmo
   profissional, serviço, convênio, pessoa atendida, início, fim; `SCHEDULED`), reaponta o sinal pago para ela, e
   programa o e-mail "o paciente insiste no horário" (§5.8). Se a oferta perdeu a corrida (o prazo B ou outro toque
   ganhou), apaga o evento criado e responde conforme o vencedor.
3. Depois do commit: lembretes da consulta nova (`reminder_hooks.after_appointment_booked`), resposta ao paciente
   ("Pronto! Seu horário de «data hora» está mantido. O sinal que você já pagou continua valendo para esta consulta.") e
   envio imediato do e-mail à clínica (a varredura é o retry).
4. Google fora do ar → "Não consegui confirmar o horário agora. Tente de novo em instantes." e nada muda.

### 5.5 Remarcar

`open → rescheduling` (no primeiro toque; o prazo continua sendo B, não muda) e entra no "Outro horário" (mesmo médico,
mesmo serviço; dia, depois horário) com a frase no topo: "Ótimo! O sinal que você já pagou vale para a nova consulta."
O portão do "antes" não pede Pix enquanto a oferta está em `rescheduling` dentro de B; a criação da consulta nova
(`flow_runner._apply_flow_result` e `booking_hold._promote_booking_hold` do Portal) chama `claim_carry_over`, que fecha
a oferta como `rebooked` e reaponta o sinal na MESMA transação; a confirmação ganha "O sinal que você já pagou vale para
esta consulta. Não há nova cobrança." `reschedule_count` não é lido nem incrementado. Remarcar depois de `refund_due`:
agendamento comum com "A devolução do seu sinal já foi pedida à clínica, então a nova consulta segue as regras de sinal
da clínica." Remarcar depois de `kept`/`rebooked`: "O sinal que você já pagou está valendo para a sua consulta."

### 5.6 Cancelar

Antes de B, de `open` ou `rescheduling` → `refund_due` (fonte `patient`, `refund_due_at` = C), abre UM item
`released_refund_due` (valor cheio), programa A (em C), B (em `decide_by_at`), C (em `refund_check_at`) e a conferência do
paciente (em `refund_check_at`); manda A na hora; responde "Combinado, sua consulta foi cancelada. A clínica foi avisada
e fará a devolução do seu sinal de R$ 60,00." e segue para a pergunta de motivo de sempre (`enter_decline_reasons`).
Cancelar de novo, ou depois de B: "A devolução do seu sinal já foi pedida à clínica." (nada novo). Cancelar depois de
`kept`/`rebooked`: "O sinal que você já pagou está valendo para a sua consulta. Para cancelar essa consulta, use as
opções da própria consulta." (não cancela a consulta nova por este botão).

### 5.7 Sem resposta (prazo B)

A varredura (`release_offer.expire_offers`) fecha toda oferta `open`/`rescheduling` com `decide_by_at ≤ agora` como
`refund_due` (fonte `timeout`, `refund_due_at` = agora), abre o item e programa B (já devido), C e a conferência; um
commit por oferta. Toque que chega depois de B mas antes da varredura acerta o prazo primeiro (mesma transição) e
responde como se a varredura já tivesse passado.

### 5.8 Os e-mails à clínica (para `Tenant.contact_email`)

Sem nome, telefone ou e-mail do paciente; com a data/hora da consulta liberada, o valor e o LINK da conversa no console
(`{BRAIN_MESSAGE_PORTAL_URL}/chat/?conversationId=<id>`; sem URL configurada, sem link). Assunto e corpo por tipo:

| Tipo (`kind`) | Quando | Diz |
|---|---|---|
| `clinic_refund_now` (A) | C | o paciente cancelou a consulta liberada de «data hora»; devolver R$ X pelo painel do Asaas; depois marcar como devolvido em "Pagamentos que precisam de decisão" |
| `clinic_refund_deadline` (B) | B | com A antes: "lembrete: a devolução de R$ X continua pendente"; sem A: "o paciente não respondeu até «B»; a consulta foi cancelada e R$ X deve ser devolvido" |
| `clinic_refund_day` (C) | T + 24 h | "já se passaram 24 horas da liberação; a devolução de R$ X continua pendente; o paciente está sendo perguntado se recebeu" |
| `clinic_refund_reminder` (n) | toque Não recebido | "o paciente informou que ainda não recebeu a devolução de R$ X" |
| `clinic_patient_keeps` | Manter deu certo | "o paciente quer manter o horário de «data hora»; a consulta voltou para a agenda com o sinal pago" |

### 5.9 A conferência do paciente (T + 24 h)

Programada junto com A/B/C (só existe se a oferta virou `refund_due`). Em `refund_check_at`, se a devolução ainda não
foi dada por feita: mensagem na conversa "A clínica foi avisada para devolver o seu sinal de R$ 60,00. Confira a sua
conta bancária (a devolução vai para a conta de onde saiu o Pix) e nos conte:" com **Recebido** (`relgot|<id>`) e **Não
recebido** (`relmiss|<id>`) — WhatsApp dentro da janela: cartão; fora: modelo `REFUND_CHECK_TEMPLATE_NAME`, só com
`paid_notice_allowed`; Portal: cartão gravado — e e-mail ao paciente (se tem e-mail) com o mesmo texto e o link da
conversa (Portal) ou "responda pelo WhatsApp da clínica" (WhatsApp).
- Recebido → `refund_closed` (`patient_received`), "Que bom! Obrigada por confirmar."; os e-mails ainda programados
  são descartados.
- Não recebido → "Avisamos a clínica novamente. Ela vai conferir a devolução do seu sinal." e UM `clinic_refund_reminder`
  (no máximo um a cada 24 h por oferta; dentro disso, só "A clínica já foi avisada novamente há pouco.").
- Toque antes da conferência ter sido enviada: "A clínica ainda está dentro do prazo para devolver o seu sinal." (nada
  muda). Toque depois de `refund_closed`: "Esse pagamento já está encerrado." (nada muda).

### 5.10 Dias com horários segurados (do desenho anterior, sem mudança)

A lista de dias volta a NÃO descontar reservas; ao escolher o dia, os horários segurados por OUTRA conversa ficam fora
da lista e o topo diz só QUAIS horários ("Os horários 08:00 e 08:30 deste dia estão reservados para outro paciente,
aguardando a confirmação ou o pagamento do sinal. Se o prazo terminar sem pagamento, eles voltam a ficar livres."); dia
todo segurado: texto próprio e só "Outro dia" (e voltar). Até 8 horários, depois "e mais N". Nunca nome, telefone ou
dado do outro paciente. Vale nos dois canais e nos dois ramos (agendar, remarcar).

### 5.11 Modelos da Meta (PENDÊNCIA)

Duas configurações novas em `config.py`, marcadas `PENDÊNCIA META`: `RELEASE_OFFER_TEMPLATE_NAME =
"liberacao_sinal_pago_v1"` (1 parâmetro, 3 botões de resposta rápida) e `REFUND_CHECK_TEMPLATE_NAME =
"devolucao_sinal_conferir_v1"` (1 parâmetro, 2 botões). O código os usa como se aprovados (decisão do dono): enquanto a
Meta não aprovar, o envio fora da janela falha, é registrado e os prazos/e-mails seguem cobrindo o caso. Textos para
submeter: §5.3 e §5.9.

### 5.12 Envio programado, idempotente (tabela `deposit_release_emails`)

Uma linha por envio: `(offer_id, kind, seq)` ÚNICO (`seq` = 0, exceto `clinic_refund_reminder` = 1, 2, ...), `due_at`,
`attempts`, `claimed_at`, `outcome`. Quem envia (`services/release_offer_dispatch.py`, chamado pela varredura e, para A,
Manter e Não recebido, logo depois do toque):
1. Reivindica com `UPDATE ... SET claimed_at = agora, outcome = 'sending' WHERE id = :id AND claimed_at IS NULL` e
   comita ANTES de enviar: duas instâncias nunca enviam a mesma linha.
2. Revalida no instante do envio: oferta `kept`/`rebooked`/`refund_closed`, ou devolução dada por feita (item da oferta
   resolvido, `provider_refund_seen_at` preenchido, ou sinal fora de `confirmado_pago`) → `outcome = 'skipped'` sem
   enviar (e, no último caso, a oferta vira `refund_closed`/`clinic_settled`). `clinic_patient_keeps` só exige `kept`.
3. Sem `contact_email` (clínica) ou sem canal nenhum (paciente): `skipped_no_contact`.
4. Envio que não saiu (SMTP fora, Meta recusou): devolve a reivindicação (`claimed_at = NULL`, `attempts + 1`); na 5ª
   falha, `given_up` + log de alarme. Envio que saiu: `sent`. Um envio interrompido no meio fica `sending` para sempre
   (no máximo uma vez; nunca duas).

Logs: ids, `kind`, `outcome`, `error_type`. Nunca e-mail, nome, telefone, link ou corpo.

## 6. Combinações (todas têm teste)

| Sequência | Resultado |
|---|---|
| Manter (livre) | `kept`; consulta recriada com o sinal; 1 e-mail `clinic_patient_keeps`; nenhum e-mail de devolução |
| Manter (ocupado) → Cancelar | continua `open` → `refund_due`; A agora, B, C |
| Manter → Manter | 2º: "já está mantida"; 1 consulta, 1 evento, 1 e-mail |
| Manter → Cancelar | "o sinal está valendo para a sua consulta... use as opções da própria consulta"; nada muda |
| Cancelar → Manter | "Você já pediu o cancelamento..."; nada muda |
| Cancelar → Cancelar | "A devolução já foi pedida"; 1 item, 1 A |
| Remarcar → nada até B | `refund_due` (`timeout`); B em B, C; sem A |
| Remarcar → Cancelar (antes de B) | `refund_due` (`patient`); A, B, C |
| Remarcar → consulta nova | `rebooked`; sinal transferido; nada programado; a varredura em B não faz nada |
| Remarcar → Manter (livre) | `kept` |
| Cancelar → Remarcar | agendamento comum com a frase "a devolução já foi pedida" |
| Silêncio | B (primeiro e-mail) e C; conferência em T + 24 h |
| Clínica ocupa o horário (agenda outra pessoa) e o paciente toca Manter | ocupado → Remarcar / Cancelar |
| Clínica dá a devolução por feita antes de B/C | os e-mails pendentes saem `skipped`; oferta `refund_closed` (`clinic_settled`); sem conferência |
| Asaas avisa estorno | idem (por `provider_refund_seen_at`) |
| Recebido | `refund_closed` (`patient_received`); pendentes `skipped` |
| Não recebido ×2 em menos de 24 h | 1 lembrete |
| Toque de outro paciente / outra clínica | "Não encontrei essa consulta."; nada muda |
| Duas instâncias da varredura | cada e-mail sai uma vez |
| Toque × varredura em B | um vence; 1 item; nunca sinal transferido E devolvido |

## 7. Erros e corridas

| Situação | O que acontece |
|---|---|
| Toque × varredura (B) | um `UPDATE` vence; o perdedor responde o estado do vencedor; 1 item (dedupe por sinal) |
| Manter × varredura | o evento do Google é criado antes do `UPDATE`; se o `UPDATE` perder, o evento é apagado |
| Consulta nova (Remarcar) × varredura | `claim_carry_over` e `expire_offers` disputam o mesmo `UPDATE` (provado em Postgres real, Task 12) |
| Duas instâncias enviando | `claimed_at IS NULL` no `UPDATE`: um vence (provado em Postgres real) |
| SMTP fora | a linha volta a ficar devida; até 5 tentativas |
| Google fora no Manter | nada muda; o paciente tenta de novo |
| Liberar duas vezes | a segunda é `409 not_live` (já existe); oferta única por consulta |
| API nova + worker velho | o worker velho manda o cartão velho (3 botões de remarcação) com o texto novo e não trata `rel*`; o prazo B e os e-mails rodam só no worker novo → o worker vai LOGO depois da API |

## 8. Testes (TDD; `BOT_ALLOWLIST_WA_IDS=""`)

Ver o plano: um teste nomeado por linha das §6/§7, mais os prazos (clínica aberta, fechando antes de T + 6 h, já
fechada, dia sem expediente, sem horário, fuso), os textos (≤ 1024, ≤ 20 por botão, nunca "devolvemos"), os canais
(WhatsApp dentro/fora da janela, Portal, e-mail) e o front (texto do aviso).

## 9. Dependências e deploy

- Base `task/TASK-050-secretaria` @ `3650e10`; integra em `feature/out-of-mvp` depois da TASK-050.
- Deploy (NÃO autorizado; skill frozen-contract-migration): migração `b6d2e8f4a1c3` (aditiva) → API → worker LOGO
  depois → Brain-Message-Frontend. Configurar `BRAIN_MESSAGE_PORTAL_URL` (já existe) para os links.
- Pendência externa: aprovação dos dois modelos na Meta (§5.11).

## 10. Decisões tomadas (padrão) — o dono pode trocar qualquer uma

- **Liberar com sinal pago nunca retém**, qualquer que seja a política de cancelamento (quem liberou foi a clínica).
- **B quando a clínica já fechou (ou não abre) no dia de T = T + 6 h** (§5.2), nunca antes de T.
- **"Fechamento da clínica" = horário da CLÍNICA** (`Tenant.business_hours`), não o do médico; sem horário configurado,
  B = T + 6 h.
- **Manter recria a consulta** (linha nova, evento novo com id determinístico), porque a linha liberada está cancelada e
  o id de evento apagado não pode ser reaproveitado no Google; o sinal é reapontado como no Remarcar.
- **"Ocupado" = evento no Google, reserva viva de outra conversa ou consulta viva do mesmo profissional.**
- **Manter conta como a confirmação do paciente**: a consulta recriada nasce com `confirmation_count = 1`, para a
  clínica não receber de novo o aviso de "não confirmou".
- **Clínica sem `contact_email`: o e-mail é marcado `skipped_no_contact` e não é reenviado depois** (evita uma rajada
  de avisos velhos quando o e-mail for cadastrado); o item de atenção e a agenda continuam mostrando a devolução.
- **Manter e Remarcar valem até B**; depois de B, só agendamento comum.
- **Cancelar depois de manter/remarcar não cancela a consulta nova por este botão** (a consulta nova tem as opções dela).
- **O item de atenção `released_refund_due` abre quando a devolução fica devida (C ou B)** e é onde a clínica dá a
  devolução por feita; dar por feita (ou o Asaas avisar estorno, ou a clínica resolver o item de outro jeito) encerra
  todos os avisos, inclusive a conferência do paciente.
- **E-mails pela varredura de pagamentos** (atraso de até ~6 min); A, Manter e Não recebido saem logo depois do toque.
- **Não citar a conta de origem do Pix**: o sistema não guarda dado do pagador e buscar no Asaas poria dado bancário no
  e-mail; o texto diz "a conta de onde saiu o Pix".
- **Não recebido: no máximo um e-mail à clínica a cada 24 h por oferta.**
- **Valor e data no e-mail da clínica, sem nome/telefone do paciente**: o link da conversa identifica quem é.
- **Botões levam o id da consulta LIBERADA** (`rel*|`): o tratador acha a oferta pela clínica da conversa.
- **Aviso de horários segurados vale para Pix E código de e-mail** (a tabela de reservas não separa).

## 11. Perguntas em aberto (para o dono)

1. Clínica já fechada em T (ex.: liberou às 19:00, fecha às 18:00): B = T + 6 h (adotado) ou B = T (devolução devida na
   hora, sem tempo para o paciente)? E um piso para o paciente decidir (ex.: T = 17:25, fecha 18:00 → B = 17:30)?
2. "Fechamento da clínica" quando cada médico tem o seu horário: usar o da clínica (adotado) ou o do médico da consulta?
3. Paciente de WhatsApp: o e-mail não tem link (não guardamos o número da clínica). Guardar o número de exibição para
   montar `wa.me`?
4. A clínica deu a devolução por feita antes de T + 24 h: o paciente ainda deve ser perguntado "Recebido / Não
   recebido"? (adotado: não, pois "a clínica quita" encerra tudo).
5. Feriados: não existem no sistema; um feriado num dia útil conta como aberto.

## 12. Não provado / bloqueios

Nada impede planejar. NÃO PROVADO até a execução: envio real pelo WhatsApp (cartão e modelos — os modelos nem estão
aprovados), cartão desenhado no Portal real com estes ids, SMTP real, produção.
