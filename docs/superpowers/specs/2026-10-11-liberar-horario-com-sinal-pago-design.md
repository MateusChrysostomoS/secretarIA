# Liberar horário com sinal pago: manter (por opção da clínica), remarcar ou cancelar (TASK-052) — Design

**Data:** 2026-10-11; refeito em 2026-10-12 (3ª revisão do dono, mesmo dia) · **Estado:** para revisão ·
**Integração:** `feature/out-of-mvp` (NUNCA `main`) · **Base:** `task/TASK-050-secretaria` @ `3650e10` (worktree
`C:\TECH\BRAIN-worktrees\TASK-052\secretarIA`, branch `task/TASK-052-liberar-sinal-pago`) · **Risco:** ALTO (dinheiro,
agenda, migração, avisos programados, dois serviços com deploy separado) · **Produção:** NÃO
AUTORIZADA.

> **Histórico.** 2026-10-11 "Receber agora / Remarcar" → substituído. 2026-10-12 (1ª) e‑mails A/B/C → substituído.
> 2026-10-12 (2ª) Manter recriava a consulta sozinho, Remarcar com sinal só até B → substituído. **2026-10-12 (3ª, este
> documento):** Manter é POR OPÇÃO DA CLÍNICA (o toque avisa a clínica e abre uma negociação de 24 h do toque; a clínica
> aceita ou recusa na aba); fechamento = horário do MÉDICO da consulta (a regra "tem secretária" pelos papéis do
> brain-api ficou PENDENTE até a tela de integrantes do dono — decisão de 2026-10-12, Task 16 do plano); TODOS os papéis marcam
> "devolvido", no escopo de agenda de cada um; modelo Meta de 3 botões reaproveitado; Remarcar a qualquer momento
> (transfere o sinal se a clínica ainda não marcou devolvido; senão cobra de novo) com e-mail próprio à clínica.
> **Ajuste do dono (2026-10-12, resolve a §11-1):** ao CONFIRMAR o Remarcar o sistema LÊ a cobrança no Asaas; só
> transfere se ela está paga e sem estorno; estornada/em estorno ou Asaas sem resposta → sinal novo (fail-safe
> financeiro); com sinal reaproveitado, "marcar devolvido" fica travado (§5.5). Seguem
> valendo: Cancelar avisa a clínica NA HORA com o passo a passo do Asaas; B; T + 24 h "Cancelado por falta de resposta";
> lembrete de T + 24 h; Recebido / Não recebido quando a clínica marca devolvido; WhatsApp sem e-mail; texto sem banco;
> D15; idempotência; tabela nova aditiva; corridas em Postgres; dias segurados; texto da agenda.

## 1. Intenção

Instantes: **T** liberação; **S** início da consulta liberada; **F** fechamento − 30 min no dia de T; **B** =
`min(T + 6 h, F)` (nunca antes de T); **E** = T + 24 h; **K** = toque em Manter + 24 h (prazo da clínica).

1. **Em T**: "Devido à sua não confirmação da ida à consulta, a clínica liberou o seu horário para que você decida o que
   fazer" + **Manter horário** / **Remarcar** / **Cancelar**. Portal: cartão + o e-mail de sempre com LINK; WhatsApp: só
   mensagem (cartão na janela; fora dela o modelo pago de 3 botões).
2. **Manter horário** (de T até S, também depois de B): NÃO recria nada. Avisa a clínica (e-mail com link da conversa)
   e abre uma negociação de 24 h contadas do toque. Ao paciente: "Avisamos a clínica ... ela decide e responde por aqui".
   A clínica, na aba (TASK-054): **aceitar manter** (a consulta volta se o horário estiver livre, com o mesmo sinal) ou
   **recusar** (devolução devida). Sem decisão até K: "sem decisão da clínica" = recusa (devolução devida, aviso à
   clínica). Depois de S, Manter responde "O horário da consulta já passou" com só Remarcar / Cancelar (WhatsApp: nova
   mensagem; Portal: ao abrir a conversa o cartão mostra só esses dois).
3. **Remarcar** a qualquer momento enquanto a oferta não foi encerrada (antes ou depois de B e de S; depois de
   Cancelar; depois da recusa da clínica). Se a clínica JÁ marcou o sinal como devolvido → a nova consulta cobra de novo
   (novo Pix); senão, quando o paciente CONFIRMA a nova consulta, o sistema consulta o Asaas (§5.5): paga e sem estorno →
   o sinal pago é transferido sem nova cobrança e a clínica
   recebe um e-mail próprio ("nova consulta marcada — REMARCADA levando o sinal, não há reembolso a fazer") e os avisos
   de devolução pendentes da oferta são cancelados ("marcar devolvido" fica travado); estornada / em estorno / Asaas sem
   resposta → sinal novo e a clínica é avisada para devolver o anterior. **Oferta "Cancelada por falta de resposta" (E) continua encerrada:
   Remarcar depois disso não reabre e não transfere** (resposta clara; o paciente marca uma consulta nova normal).
4. **Cancelar**: pergunta de motivo de sempre + aviso de devolução à clínica NA HORA, sempre, com o passo a passo do
   Asaas (estornar a própria cobrança, sem digitar chave; saldo insuficiente logo após receber; guardar comprovante).
5. **Sem resposta** (ou Remarcar não concluído) até B: aviso de devolução em B. **Em E** sem decisão final: terminal
   "Cancelado por falta de resposta"; a clínica recebe "prazo vencido, devolva agora".
6. **Lembrete à clínica em E**: sai, reavaliado no envio, exceto quando a clínica já marcou devolvido E o paciente já
   respondeu Recebido.
7. **Recebido / Não recebido**: perguntado quando a clínica marca "devolvido" (Portal: cartão + e-mail com link;
   WhatsApp: mensagem). Recebido encerra; Não recebido: novo aviso à clínica (≤ 1 a cada 24 h).
8. **Marcar devolvido**, **aceitar/recusar manter** e a **lista**: TODOS os papéis (secretária, gestor, médico), cada um
   no escopo que já tem na agenda — secretária e gestor veem a clínica toda (gestor que também é médico tem "só as
   minhas"); médico comum só as suas; um médico não age sobre a oferta de outro.
9. **Fechamento (F)**: por enquanto, horário do médico da consulta (sem médico: o da clínica). "Clínica com secretária →
   horário da clínica" fica PENDENTE de dependência (tela de integrantes do dono no brain-api; §5.2, §11).
10. **Texto do reembolso ao paciente**: sem banco; valor + "a conta de onde saiu o Pix". Feriados: ficam para depois.

**Sucesso** =
1. Liberar com sinal pago abre UMA oferta sem decidir nada do dinheiro; não pago: registro `not_offered` (lista) + fluxo
   de hoje.
2. Manter avisa a clínica UMA vez e abre a negociação; aceitar recria a consulta (se livre) e avisa o paciente; recusar
   ou K vencido torna a devolução devida, avisa a clínica e o paciente (Remarcar / Cancelar).
3. Cancelar: UM item `released_refund_due`, aviso à clínica na hora, uma vez.
4. Remarcar: transfere só se a clínica não marcou "devolvido" E o Asaas, consultado na confirmação, diz paga e sem
   estorno; com transferência, e-mail "REMARCADA levando o sinal", avisos de devolução cancelados e "devolvido" travado
   (409); Asaas estornado/em estorno/sem resposta → sinal novo + "DEVOLVA o sinal anterior" à clínica.
5. Silêncio: aviso em B; "Cancelado por falta de resposta" em E; lembrete de E conforme a regra.
6. Nenhum aviso duas vezes; nada antes de T; nada entre clínicas nem fora do escopo de agenda de quem age.

## 2. Escopo

secretarIA: tabelas novas aditivas; prazos (horário do médico); abertura na liberação; mensagens
com botões; toques; negociação de Manter e as ações da clínica; transferência/cobrança no Remarcar; B, E, K e envios na
varredura de pagamentos; endpoints da aba; cartão depois de S no Portal; aviso de horários segurados. Brain-Message-
Frontend: só o texto do aviso de liberar.

## 3. Fora de escopo

O sistema estornar (D15) ou consultar o Asaas fora da confirmação do Remarcar (a única leitura, §5.5). A tela/aba (TASK-054). A leitura de "tem secretária" (endpoint do
brain-api + cliente no secretarIA) — PENDENTE de dependência, tarefa isolada no fim do plano (§5.2, §11). Aprovação Meta. Feriados. Cancelamento de consulta viva (como hoje). IA
transferir o sinal. Pix pago depois da liberação (`paid_then_cancelled`).

## 4. O que o código mostra hoje (base `3650e10`)

| Fato | Onde | Consequência |
|---|---|---|
| Liberar trata como cancelamento DO PACIENTE (pode reter) | `api/hub/calendar.py::release_appointment` | Com sinal pago abre a oferta |
| Liberar apaga o evento e cancela a linha; id de evento apagado não volta | `release_appointment`, `services/calendar.py::create_event` | "Aceitar manter" cria consulta e evento novos (id determinístico) |
| Horário livre: eventos do Google, reservas, consultas | `check_availability`, `booking_hold.live_hold_overlaps` | "Aceitar manter" checa os três |
| Papéis: `manager`, `doctor`, `secretary` existem no brain-api (`models/user.py::ROLE_SECRETARY`) e chegam ao secretarIA só no token de QUEM chama (`core/hub_roles.py`) | brain-api `api/internal.py` não tem leitura de "papéis da clínica" (`professional-emails` exclui secretárias) | PENDENTE: leitura futura `GET /internal/tenants/{id}/staff-roles` quando a tela de integrantes existir; até lá, nada é chamado e F usa o horário do médico |
| Escopo de agenda: `agenda_scope` no token (brain-api `services/hub_scope.py`: secretária e gestor = clínica; médico = os seus) → `AgendaViewer` | `api/hub/deps.py::get_agenda_viewer`, `services/agenda_visibility.py`, `api/hub/calendar.py::list_events` (`mine`, `no_own_agenda`) | A lista e as ações da aba usam a MESMA regra |
| Aviso ao paciente: job `send_cancellation_notice` (WhatsApp) ou mensagem + e-mail com link (Portal) | `services/appointment_release.py`, `services/staff_patient_message.py::nudge_portal_patient` | Botões novos entram aqui |
| Link do console: `?conversationId=` | Brain-Message-Frontend `lib/portal-routes.ts::conversationHref` | `{BRAIN_MESSAGE_PORTAL_URL}/chat/?conversationId=<id>` |
| Item resolvido com `refunded_in_provider_panel` grava `cancelado_reembolsado` | `services/payments/attention_view.py::resolve_item` | "Marcar devolvido" passa por aí |
| "Cancelar" pergunta o motivo; "Outro horário" = `rebooksame` | `workers/shared/actions.py` | Reaproveitados |
| Entrada no Portal: `process_brain_message_enter` | `workers/portal/open.py` | Cartão de 2 botões depois de S |
| Varredura de pagamentos a cada 4–6 min | `workers/payments_cron.py::run_payment_sweeps` | B, E, K, envios (atraso ≤ ~6 min) |
| A lista de dias esconde dias todo segurados | `services/flow_router.py::enter_day_picker` | Desfeito (§5.11) |
| Uma cabeça Alembic: `e2b7c4a9d1f5` | `migrations/versions/` | Migração nova `b6d2e8f4a1c3` (só tabelas novas) |

## 5. Desenho

### 5.1 A oferta (`deposit_release_offers`, uma por consulta liberada com sinal)

| Coluna | Valores |
|---|---|
| `decision` (paciente/negociação) | `none`, `rescheduling`, `keep_requested`, `kept`, `keep_declined`, `rebooked`, `cancelled`, `no_answer_cancelled`, `not_offered` |
| `refund_status` (dinheiro) | `not_due` → `due` → `marked` → `received` / `not_received`; `due` → `clinic_decided` |
| `refund_source` | `patient` (Cancelar), `timeout` (B/E), `keep_refused`, `keep_timeout`, `carry_refunded` / `carry_unknown` (checagem do Remarcar recusou) |
| `carry_check` | `reusable` / `refunded` / `unknown` — o que o Asaas disse na confirmação do Remarcar (gravado UMA vez; + `carry_checked_at`) |
| `deposit_reused_at` | instante em que um Remarcar reaproveitou o sinal; a partir daí "marcar devolvido" → 409 `deposit_reused` |
| `keep_outcome` | `accepted`, `refused`, `timeout` (+ `keep_requested_at`, `keep_deadline_at` = K, `keep_resolved_at`, `keep_resolved_by`) |
| instantes | `released_at` T, `slot_start_at` S, `decide_by_at` B, `expires_at` E, `first_contact_at`, `refund_due_at`, `refund_marked_at`, `patient_answered_at`, `last_reminder_at`, `after_start_card_at` |

Transições (cada uma um `UPDATE ... WHERE <estado esperado>`):

| De | Ação | Para |
|---|---|---|
| `none`/`rescheduling`, antes de S, devolução não marcada | Manter | `keep_requested` (K = agora + 24 h); aviso `clinic_keep_request` |
| `keep_requested` | clínica aceita (horário livre) | `kept`; consulta recriada; sinal reapontado; devolução volta a `not_due` (item fechado como "remarcado"); aviso ao paciente |
| `keep_requested` | clínica recusa / K vence | `keep_declined`; devolução `due` (`keep_refused`/`keep_timeout`); aviso `clinic_keep_declined` (passo a passo) + lembrete em +24 h; aviso ao paciente com Remarcar / Cancelar |
| `none`/`rescheduling`/`cancelled`/`keep_requested`/`keep_declined` | Remarcar | `rescheduling` (transfere se a devolução não foi marcada) |
| `rescheduling` | nova consulta confirmada, devolução não marcada | `rebooked`; sinal transferido; devolução `not_due`; item fechado; `clinic_rebooked_deposit`; avisos de devolução cancelados |
| `none`/`rescheduling`/`keep_requested`/`keep_declined` | Cancelar | `cancelled`; devolução `due` se não estava; aviso `clinic_refund_cancel` na hora (se não marcada) |
| `none`/`rescheduling` | B vencido | devolução `due` (`timeout`); `clinic_refund_noanswer` em B; lembrete em E |
| `none`/`rescheduling` | E vencido | `no_answer_cancelled` (terminal, sem reabrir) |

O 409 `retention_ack_required` continua e diz: "O paciente já pagou o sinal (R$ X). Ao liberar o horário, ele poderá
pedir para manter o horário (a clínica decide), remarcar usando o mesmo sinal ou cancelar. Se ele cancelar ou não
responder até o prazo, a clínica receberá avisos para devolver o sinal por inteiro pelo painel do provedor de pagamento."
`deposit_outcome = choice_offered`.

### 5.2 Prazos (horário do médico; "tem secretária" pendente)

- **Agora (decisão do dono, 2026-10-12):** F usa o horário de fechamento do **médico da consulta** (o efetivo, que
  herda da clínica quando o médico não tem horário; consulta sem médico → clínica). Nada é perguntado ao brain-api.
- **Pendente de dependência:** "clínica com secretária → horário da clínica". Espera a tela de integrantes do dono
  (brain-api). Interface esperada: `services/brain_staff.py::fetch_has_secretary(tenant_id) -> bool | None` lendo
  `GET {BRAIN_API_BASE_URL}/internal/tenants/{id}/staff-roles` → `{"roles": ["manager", "doctor", "secretary", ...]}`
  (mesmo cliente/chave de `services/brain_professionals.py`); qualquer falha → `None` → horário do médico. O papel do
  token de quem libera NÃO serve (diz quem age, não quem a clínica tem). Nenhum campo manual. `open_offer` já aceita
  `has_secretary` (hoje sempre `None`); ligar é a Task 16 do plano, que não bloqueia as demais.
- F = maior `end` do dia de T nesse horário, no fuso da clínica, − 30 min.
- B = `min(T + 6 h, F)` se F > T; senão T + 6 h (clínica fechada em T: dia sem expediente, sem horário, ou T depois de
  F — o paciente mantém tempo para decidir; nada antes de T). E = T + 24 h. K = toque em Manter + 24 h.
- **Premissas visíveis:** oferta sem decisão final fica aberta até E e vira `no_answer_cancelled`; "contato" = toque em
  Manter, Remarcar ou Cancelar (`first_contact_at`), texto livre não conta; uma negociação aberta (`keep_requested`) NÃO
  é "sem resposta" (o paciente respondeu) e não cai em E — termina em aceite, recusa ou K.

### 5.3 A mensagem em T

> Devido à sua não confirmação da ida à consulta, a clínica liberou o seu horário para que você decida o que fazer.
>
> Recado da clínica: «justificativa» *(só se houver; é o que se corta para caber em 1024)*
>
> Você já pagou o sinal desta consulta (R$ 60,00). Toque em *Manter horário* para pedir à clínica o mesmo horário, em
> *Remarcar* para escolher outro dia e horário usando o mesmo sinal, sem nova cobrança, ou em *Cancelar* para cancelar a
> consulta e receber o sinal de volta. Se você não responder até 20:00 de 05/03, a clínica será avisada para devolver o
> seu sinal.

Paciente que tinha confirmado: "A clínica liberou o seu horário para que você decida o que fazer.". Se S já passou em T:
sem Manter (texto e botões). WhatsApp: cartão na janela; fora dela o modelo `RELEASE_OFFER_TEMPLATE_NAME` (3 botões,
reaproveitado em toda mensagem fora da janela desta tarefa, exceto a conferência). Portal: cartão + e-mail com link.

### 5.4 Manter horário (negociação por opção da clínica)

- Toque válido: decisão `none`/`rescheduling`, devolução não marcada, antes de S (também depois de B). Resposta: "Avisamos
  a clínica que você quer manter o horário de 08/03 às 10:00. A clínica vai verificar se o horário ainda está livre e
  decide em até 24 horas; a resposta chega por aqui." Repetido: "A clínica já foi avisada de que você quer manter o
  horário e vai responder por aqui.". Depois de S: "O horário da consulta já passou. Você pode remarcar para outro dia e
  horário ou cancelar a consulta." + Remarcar / Cancelar. Devolução já marcada: "A clínica já informou que devolveu o seu
  sinal. Você pode remarcar (com novo pagamento do sinal) ou cancelar.".
- **Pausa dos avisos de devolução durante a negociação:** enquanto `keep_requested`, B e E não se aplicam e os avisos de
  devolução que vencerem (B, lembrete de E) são descartados — a própria clínica está decidindo; pedir que devolva seria
  contraditório. Se a negociação terminar em recusa ou K, um aviso novo (`clinic_keep_declined`, com o passo a passo) sai
  na hora e um lembrete 24 h depois.
- **Aceitar** (`release_keep.accept_keep`): só com `keep_requested`, antes de S e com a devolução não marcada. Horário
  ocupado → 409 `slot_taken` (a clínica recusa ou espera); S passou → 409 `slot_past`; Google fora → 503. Livre →
  evento `relkeep<hex>`, consulta nova confirmada (mesmo paciente, serviço, médico, convênio, pessoa atendida), sinal
  reapontado, devolução de volta a `not_due` com o item fechado ("remarcado"), avisos de devolução descartados, ao
  paciente "A clínica confirmou: seu horário de 08/03 às 10:00 está mantido. O sinal que você já pagou continua valendo
  para esta consulta.". Aceitar de novo → 200 (idempotente).
- **Recusar** / **K vencido**: `keep_declined`; devolução `due`; `clinic_keep_declined` + lembrete; ao paciente "A
  clínica não conseguiu manter o horário de 08/03 às 10:00. Você pode remarcar para outro dia e horário, usando o mesmo
  sinal, ou cancelar e receber o sinal de volta." + Remarcar / Cancelar. Recusar de novo → 200.
- Remarcar durante a negociação encerra o pedido de Manter (a clínica que aceitar depois recebe 409 `not_requested`).

### 5.5 Remarcar

Sempre que a oferta não está encerrada (`kept`, `rebooked`, `no_answer_cancelled`, `not_offered` encerram). Entra no
"Outro horário" (mesmo médico/serviço; dia, depois horário). Devolução não marcada → "Ótimo! Escolha o novo horário. Ao
confirmar, conferimos se o sinal que você já pagou pode valer para a nova consulta." Devolução marcada → "A clínica já
informou que devolveu o seu sinal, então a nova consulta segue as regras de sinal da clínica." (novo Pix, SEM consultar
o Asaas). Depois de E sem resposta: "O prazo de 24 horas para responder terminou e a consulta foi cancelada por falta de
resposta. ... Para marcar uma nova consulta, é só me escrever." (não reabre).

**Proteção da transferência (decisão do dono, 2026-10-12).** No momento em que o paciente CONFIRMA a nova consulta
(portão do "antes" ao escolher o horário; `flow_runner._apply_flow_result` ou promoção do Portal no "depois"), o sistema
LÊ a cobrança paga original no Asaas com a chave da própria clínica (`services/release_carry_check.py` →
`AsaasProvider.get_charge` → `GET /payments/{id}`; leitura apenas, nunca estorna — D15), FORA de transação e com espera
máxima de poucos segundos; o resultado fica gravado UMA vez na oferta (`carry_check`):

| O Asaas diz | Resultado |
|---|---|
| paga (`RECEIVED`/`CONFIRMED`) e nenhum estorno listado | `reusable`: a consulta nova leva o sinal na mesma transação — `rebooked`, devolução `not_due`, item fechado como "remarcado", avisos de devolução pendentes descartados, `deposit_reused_at` gravado ("marcar devolvido" → 409 `deposit_reused`; a aba mostra "Sinal reaproveitado — não devolver") e `clinic_rebooked_deposit` destacado à clínica ("REMARCADA levando o sinal"); o paciente lê "O sinal que você já pagou vale para esta consulta. Não há nova cobrança." |
| `REFUNDED` / `REFUND_REQUESTED` / `REFUND_IN_PROGRESS`, ou algum estorno listado (inclui parcial) | `refunded`: nada transferido; sinal NOVO pelas regras da clínica |
| fora do ar, timeout, erro HTTP/rede, status inesperado, formato ilegível, sem conta/sem id de cobrança, sinal de outra clínica | `unknown` = **FAIL-SAFE FINANCEIRO: cobrar de novo**. A remarcação não espera o Asaas |

Em `refunded`/`unknown`: a oferta fecha como remarcada com sinal novo (`rebooked`, sem `new_appointment_id`), o sinal
antigo vira devolução devida (`carry_refunded`/`carry_unknown`; item aberto) e a clínica recebe NA HORA
`clinic_carry_refused` ("DEVOLVA o sinal anterior" — em `unknown`: "não conseguimos confirmar com o Asaas; o paciente
pode ter pago duas vezes", com o passo a passo do estorno); o paciente lê "Não foi possível reaproveitar o sinal
anterior, então esta consulta terá um sinal novo. Se o sinal anterior ainda não voltou para você, a clínica vai
devolvê-lo." **Consequência aceita:** no `unknown` o paciente pode pagar duas vezes (o primeiro sinal continua pago) e a
clínica fica com um sinal extra a devolver. **Risco residual aceito pelo dono:** segundos entre a leitura no Asaas e um
estorno feito pela clínica nesse intervalo. Dois cliques de confirmar recebem a MESMA resposta (gravada uma vez) e só
uma consulta leva o sinal (`UPDATE` guardado). Campos do Asaas: o cliente existente já traduz `status`; a lista
`refunds` da cobrança é lida se vier — formato NÃO PROVADO até o sandbox (formato inesperado = `unknown`). Os textos da
oferta ao paciente (e o modelo Meta) continuam dizendo "usando o mesmo sinal, sem nova cobrança" — vale no caso normal;
quando a checagem recusa, a nota acima explica na hora.

### 5.6 Cancelar

De `none`/`rescheduling`/`keep_requested`/`keep_declined`: `cancelled`; devolução `due` se não estava (lembrete em E,
ou em agora + 24 h se E já passou); aviso `clinic_refund_cancel` na hora se a devolução não foi marcada. Respostas como
na revisão anterior ("Combinado, sua consulta foi cancelada. A clínica foi avisada e fará a devolução do seu sinal de
R$ 60,00." / já marcada: "... A clínica já informou que devolveu o seu sinal." / repetido / depois de Manter aceito /
depois de E). Segue a pergunta de motivo.

### 5.7 B, E, K (varredura)

B: `none`/`rescheduling`, devolução `not_due` → `due` (`timeout`), item, `clinic_refund_noanswer` (B), lembrete (E).
E: `none`/`rescheduling` → `no_answer_cancelled` (e devida, se ainda não). K: `keep_requested` vencido → como "recusar"
(`keep_timeout`). Um toque tardio aplica a mesma transição antes de responder.

### 5.8 Avisos à clínica (`Tenant.contact_email`; sem dado do paciente; com link da conversa)

| `kind` | Quando | Vale no envio se |
|---|---|---|
| `clinic_keep_request` | toque Manter | `keep_requested` |
| `clinic_keep_declined` | recusa / K | `keep_declined` e devolução `due` (passo a passo do Asaas) |
| `clinic_rebooked_deposit` | Remarcar confirmado com sinal transferido | `rebooked` ("REMARCADA levando o sinal: não há reembolso a fazer; NÃO devolva, o sinal está em uso; conferido no Asaas na remarcação") |
| `clinic_carry_refused` | checagem do Remarcar = `refunded`/`unknown` | devolução `due` ("DEVOLVA o sinal anterior"; motivo; em `unknown`, "o paciente pode ter pago duas vezes") |
| `clinic_refund_cancel` | toque Cancelar | `cancelled` e `due` |
| `clinic_refund_noanswer` | B | `due` e decisão `none`/`rescheduling`/`no_answer_cancelled` |
| `clinic_refund_day` (seq 0 em E; seq 1 após recusa/K) | E / +24 h | devolução `due`/`marked`/`not_received` e decisão fora de `keep_requested`/`kept`/`rebooked`; com `no_answer_cancelled`: "prazo vencido, devolva agora" |
| `clinic_refund_reminder` (seq n) | Não recebido | `not_received` |

Passo a passo do Asaas (em todos os de devolução): 1. abrir Cobranças e achar a cobrança Pix (R$ X, paga em DD/MM);
2. estornar o valor total — não é preciso chave Pix, volta para a conta de origem; 3. saldo insuficiente logo após
receber: aguardar a liberação e tentar de novo; 4. guardar o comprovante; 5. marcar como devolvido na aba (o paciente
será perguntado se recebeu).

### 5.9 Avisos ao paciente

Sempre na conversa; e-mail SÓ no Portal e só onde já existia (liberação) e na conferência. `patient_keep_accepted`,
`patient_keep_declined` (com Remarcar / Cancelar) e `patient_check` (Recebido / Não recebido). WhatsApp fora da janela:
modelo de 3 botões (`RELEASE_OFFER_TEMPLATE_NAME`) para os de Manter; `REFUND_CHECK_TEMPLATE_NAME` para a conferência;
só com `paid_notice_allowed`, senão nada sai. Conferência: "A clínica informou que devolveu o seu sinal de R$ 60,00.
Confira a sua conta (a devolução vai para a conta de onde saiu o Pix) e nos conte:".

### 5.10 Contrato para a aba (TASK-054)

Escopo de TODAS as rotas = o da agenda (`get_agenda_viewer` + `mine`, igual a `GET /calendar/events`): quem vê só os
seus não age sobre outro (404); `mine=true` sem profissional → 422 `no_own_agenda`. Todos os papéis.

- `GET /tenants/me/release-offers?pending_only=false&mine=false&limit=100` → `{"items": [...]}`: `id`,
  `released_appointment_id`, `new_appointment_id`, `conversation_id`, `patient_display_name`, `professional_id`,
  `service_label`, `slot_start`, `slot_end`, `amount_cents`, `deposit_paid_at_release`, `deposit_status` (atual),
  `refund_status`, `refund_source`, `decision`, `patient_decision` (`no_answer` | `keep_requested` | `kept` |
  `keep_declined` | `rescheduled` | `cancelled` | `cancelled_no_answer` | `received` | `not_received` | `not_offered`),
  `keep_deadline_at`, `released_at`, `decide_by_at`, `expires_at`, `first_contact_at`, `refund_due_at`,
  `refund_marked_at`, `patient_answered_at`, `can_mark_refunded`, `can_decide_keep`, `deposit_reused` (bool),
  `carry_check` (`reusable` | `refunded` | `unknown` | null), `state_note` ("Sinal reaproveitado — não devolver" |
  "Sinal novo cobrado na remarcação — devolva o sinal anterior" | null).
- `POST /tenants/me/release-offers/{id}/refunded` `{"resolved_by_name", "note"?}` → 200 (marcado agora ou antes);
  409 `refund_not_due` | `keep_pending` | `deposit_reused` (um Remarcar reaproveitou o sinal: não devolver); 404; 422
  (nota nunca ecoada).
- `POST /tenants/me/release-offers/{id}/keep/accept` `{"resolved_by_name"}` → 200 (aceito agora ou antes); 409
  `not_requested` | `slot_taken` | `slot_past` | `refund_already_marked`; 503 `calendar_unavailable`; 404.
- `POST /tenants/me/release-offers/{id}/keep/refuse` `{"resolved_by_name"}` → 200 (recusado agora ou antes); 409
  `not_requested`; 404.

### 5.11 Dias com horários segurados (sem mudança)

Dia com horários segurados por OUTRA conversa aparece; ao escolher, o topo diz só quais horários estão reservados
("aguardando a confirmação ou o pagamento do sinal; se o prazo terminar sem pagamento, eles voltam a ficar livres");
dia todo segurado: só "Outro dia". Nunca dado de outro paciente.

### 5.12 Modelos da Meta (PENDÊNCIA)

`RELEASE_OFFER_TEMPLATE_NAME = "liberacao_sinal_pago_v1"` (1 parâmetro, 3 botões — reaproveitado inclusive depois de S;
Manter tocado depois de S responde com 2 botões) e `REFUND_CHECK_TEMPLATE_NAME = "devolucao_sinal_conferir_v1"` (2
botões), usados como se aprovados.

### 5.13 Envio idempotente

Uma linha de `deposit_release_emails` por envio, `(offer_id, kind, seq)` ÚNICO; reivindicada (`claimed_at IS NULL`) e
comitada antes de enviar; revalidada (§5.8/§5.9); sem contato → `skipped_no_contact`; até 5 tentativas.

## 6. Combinações (todas com teste nomeado no plano)

| Sequência | Resultado |
|---|---|
| Manter antes de S | `keep_requested`; 1 `clinic_keep_request`; nada recriado |
| Manter depois de B (sem resposta antes) | permitido; avisos de devolução pendentes descartados durante a negociação |
| Manter depois de S | "o horário já passou" + Remarcar/Cancelar; nada muda |
| Manter repetido | "a clínica já foi avisada"; 1 aviso |
| Manter com devolução já marcada | "a clínica já devolveu"; Remarcar/Cancelar |
| Clínica aceita, horário livre | `kept`; consulta recriada; sinal reapontado; paciente avisado; aceitar de novo = 200 |
| Clínica aceita, horário reocupado | 409 `slot_taken`; nada muda |
| Clínica aceita depois de S | 409 `slot_past` |
| Clínica recusa | `keep_declined`; devida; `clinic_keep_declined`; paciente com Remarcar/Cancelar |
| Clínica não decide até K | idem, `keep_timeout` |
| Manter e depois Remarcar | pedido encerrado; aceitar depois = 409 `not_requested` |
| Remarcar antes/depois de B, depois de S, depois de Cancelar, depois da recusa (não marcada) | sinal transferido; ao confirmar `clinic_rebooked_deposit`; avisos de devolução cancelados; item fechado |
| Remarcar com devolução marcada (Asaas ainda diz paga) | nova cobrança (prefixo próprio), sem consultar o Asaas |
| Remarcar confirmado; Asaas diz paga e sem estorno | sinal transferido; "devolvido" travado (409 `deposit_reused`) |
| Remarcar confirmado; Asaas diz estornada / em estorno / estorno listado | sinal novo; devolução devida; `clinic_carry_refused` |
| Remarcar confirmado; Asaas fora do ar / timeout / erro / resposta inesperada | sinal novo (fail-safe); devolução devida; `clinic_carry_refused` ("pode ter pago duas vezes") |
| Marcar devolvido depois do sinal reaproveitado | 409 `deposit_reused`; nada muda |
| Remarcar sem concluir até B / até E | aviso em B / `no_answer_cancelled` em E |
| Remarcar depois de E sem resposta | encerrada; não reabre |
| Cancelar antes/depois de S, depois de B, depois da recusa | aviso na hora; 1 item |
| Cancelar depois de marcado / repetido / depois de aceito / depois de E | sem aviso / "já pedida" / "use as opções" / "prazo terminou" |
| Sem nenhum contato | B: aviso; E: `no_answer_cancelled` + "prazo vencido" |
| Lembrete E: marcado e Recebido / marcado sem resposta / Não recebido / negociação aberta | não sai / sai / sai / não sai |
| Clínica marca devolvido: devida / não devida / negociação aberta / duas vezes | pergunta ao paciente / 409 / 409 `keep_pending` / 200, 1 pergunta |
| Escopo: médico comum na oferta de outro médico; secretária; gestor com `mine` | 404 / vê tudo / só as suas |
| Toque de outro paciente / outra clínica / id nunca mostrado no Portal | nada muda |
| Duas instâncias da varredura | cada envio sai uma vez |

## 7. Erros e corridas

| Situação | O que acontece |
|---|---|
| Cancelar × B | guarda no `refund_status`; 1 item; aviso de Cancelar sai |
| Aceitar × Remarcar/Cancelar do paciente | um `UPDATE` vence; perdendo, o evento criado é apagado |
| Aceitar × K | idem |
| Remarcar confirmado × marcação de devolvido | um `UPDATE` vence (transfere com devolução `due` OU marca devolvido) — provado em Postgres real |
| Clínica estornou no Asaas mas não marcou, e o paciente remarcou | a leitura do Asaas na confirmação vê o estorno → sinal novo (§5.5) |
| Clínica estorna no Asaas nos segundos entre a leitura e a consulta nova | transfere; risco residual ACEITO pelo dono |
| Dois cliques de confirmar | mesma resposta (gravada uma vez), um só `UPDATE` transfere — provado em Postgres real |
| Consulta ao Asaas de uma clínica | só a chave e a cobrança da própria clínica (sinal de outra clínica = `unknown`, nenhuma chamada) |
| Duas instâncias enviando | reivindicação |
| API nova + worker velho | worker LOGO depois da API |

## 8. Testes

No plano: um teste nomeado por linha das §6/§7, prazos (horário do médico; sem médico = clínica), textos, canais
(WhatsApp sem e-mail), contrato da aba (escopo, idempotência) e o texto do front.

## 9. Dependências e deploy

Base `task/TASK-050-secretaria` @ `3650e10`; integra em `feature/out-of-mvp`. Dependências: TASK-054 (tela) consome
§5.10. PENDENTE (não bloqueia): papéis da clínica no brain-api (tela de integrantes do dono) → Task 16; até lá F usa o
horário do médico.
Deploy (NÃO autorizado): migração `b6d2e8f4a1c3` → API → worker LOGO depois → front. `BRAIN_MESSAGE_PORTAL_URL` já
configurado.

## 10. Decisões tomadas (padrão)

- Liberar com sinal pago nunca retém.
- Manter = pedido à clínica com 24 h para decidir (K); K vencido = recusa; contato via Manter conta como contato.
- Durante a negociação os avisos de devolução ficam pausados (descartados) e B/E não se aplicam; ao recusar/K, aviso novo
  na hora + lembrete em 24 h.
- Aceitar recria (linha e evento novos), conta como confirmação e fecha o item de devolução como "remarcado".
- Remarcar a qualquer momento enquanto a oferta não está encerrada; transfere só se a devolução não foi marcada E o
  Asaas, lido na confirmação, diz paga e sem estorno; qualquer dúvida = sinal novo (fail-safe financeiro).
- Cancelar depois de devolução marcada não gera aviso. Item resolvido de outro jeito → `clinic_decided`.
- Clínica fechada em T → B = T + 6 h. Fechamento = horário do médico (secretária: pendente).
- Clínica sem `contact_email` → `skipped_no_contact`, não reenviado.
- Avisos à clínica por e-mail com link para o console.
- Liberação com sinal não pago fica só registrada (`not_offered`).
- Não recebido: ≤ 1 aviso a cada 24 h.
- Marcar devolvido durante a negociação → 409 `keep_pending` (decidir o Manter primeiro).

## 11. Perguntas em aberto

1. RESOLVIDA (dono, 2026-10-12): ler o Asaas na confirmação do Remarcar; fail-safe = cobrar de novo (§5.5).
2. ADIADA (dono, 2026-10-12): "tem secretária" e o contrato `GET /internal/tenants/{id}/staff-roles` →
   `{"roles": [...]}` ficam para quando a tela de integrantes existir (Task 16, pendente de dependência).
3. Feriados: depois.

## 12. Não provado / bloqueios

Nada impede planejar. NÃO PROVADO: WhatsApp real (modelos não aprovados), SMTP real, Portal real, produção, a leitura
real da cobrança no Asaas (status de estorno e lista `refunds`, só o sandbox prova). Pendente
de dependência: a regra "tem secretária" (Task 16).
