# Liberar horário com sinal pago: devolver ou remarcar (TASK-052) — Design

**Data:** 2026-10-11 · **Estado:** decisões do dono (D-B, 2026-10-11) aplicadas; para revisão · **Integração:**
`feature/out-of-mvp` (NUNCA `main`) · **Base:** `task/TASK-050-secretaria` @ `3650e10` (worktree
`C:\TECH\BRAIN-worktrees\TASK-052\secretarIA`, branch `task/TASK-052-liberar-sinal-pago`) · **Risco:** ALTO (dinheiro,
fluxo de agendamento, migração, dois serviços com deploy separado) · **Produção:** NÃO AUTORIZADA.

## 1. Intenção e critério de sucesso

Dito pelo dono (resumo fiel): quando a clínica **libera** uma consulta cujo sinal **já foi pago**, o paciente recebe a explicação
de sempre (a clínica liberou o horário) e fica sabendo que o sinal será devolvido pela clínica se ele não responder em 24 horas.
A mensagem traz **dois botões**: "Receber agora" e "Remarcar", e explica que, ao remarcar, o sinal pago vale para o novo horário.
"Receber agora" avisa a clínica na hora para devolver; sem resposta em 24 horas, o mesmo aviso abre sozinho. "Remarcar" segue o
caminho de remarcar perguntando primeiro o NOVO DIA e depois o NOVO HORÁRIO, sem nova cobrança. E a lista de dias passa a
mostrar os dias cujos horários livres estão reservados por Pix ainda não pago de outros pacientes, com um aviso dos horários
reservados (sem dado de ninguém) que podem ficar livres se o Pix expirar.

Regra do dinheiro (D15, confirmada): **o sistema nunca devolve sozinho.** "Devolver" = abrir UM item de atenção
"devolução devida" para a clínica devolver pelo painel do Asaas.

**Sucesso** =
1. Liberar uma consulta com sinal pago abre uma "oferta" e manda ao paciente (WhatsApp dentro da janela de 24 h, ou Portal) o
   aviso com "Receber agora" / "Remarcar"; nenhum item de atenção é aberto ainda; o sinal continua pago.
2. "Receber agora" abre exatamente UM item `released_refund_due` (valor cheio) e responde "a clínica fará a devolução em até
   24 horas"; tocar de novo não abre outro.
3. Sem resposta em 24 horas, a varredura de pagamentos abre o mesmo item (uma vez, idempotente).
4. "Remarcar" abre a lista de dias (mesmo médico, mesmo serviço; depois os horários) com a frase de que o sinal pago vale; ao
   confirmar, a nova consulta nasce com o sinal pago transferido, sem nova cobrança e sem contar como remarcação do paciente.
5. Um dia cujo horário livre está reservado por outro paciente aparece na lista; ao escolhê-lo, o paciente vê quais horários
   estão reservados e que podem liberar; nenhum dado do outro paciente aparece.
6. Nada vaza entre clínicas nem entre pacientes da mesma clínica; toques e o relógio nunca ganham os dois.

## 2. Escopo

- secretarIA: abertura da oferta na liberação (`POST /tenants/me/calendar/appointments/{id}/release`), mensagem com dois
  botões (WhatsApp e Portal), tratamento dos toques, transferência do sinal no fechamento do novo agendamento, relógio de 24 h
  na varredura de pagamentos, aviso de horários reservados na escolha de dia/horário, uma migração aditiva (tabela nova).
- Brain-Message-Frontend: só o texto do aviso de liberar com sinal pago no cartão da agenda (hoje diz "pode ser retido").

## 3. Fora de escopo (de propósito)

- Cancelamento pela clínica (`POST .../cancel`) e cancelamento pelo paciente: continuam como hoje (o cancelamento pela clínica
  já abre "devolver por inteiro" na hora).
- Um modelo (template) novo do WhatsApp com os dois botões (exige aprovação na Meta; não autorizado). Fora da janela de 24 h vale
  o modelo de cancelamento já aprovado (§5.3).
- E-mail novo para a clínica: a lista de motivos que geram e-mail (`EMAIL_REASONS`) é decisão de produto e não muda (§9).
- A tela "Pagamentos que precisam de decisão" (F4) não existe ainda; até lá, a agenda mostra "Pagamento pago, precisa de
  decisão" no cartão da consulta (já existe, `lib/agenda/insurance.ts::DEPOSIT_ATTENTION_TEXT`).
- Carregar o sinal pelo caminho do agente de IA (`ai/tools.py`): só os caminhos determinísticos fecham a transferência (§9).
- Pagamento que chega DEPOIS da liberação (Pix em voo): continua abrindo `paid_then_cancelled` como hoje; não vira oferta (§9).

## 4. O que o código mostra hoje (base `3650e10`)

| Fato | Onde | Consequência |
|---|---|---|
| Liberar trata a liberação como cancelamento DO PACIENTE: com sinal pago dentro da janela e retenção total, o sinal fica **retido** | `api/hub/calendar.py::release_appointment` chama `deposit_lifecycle.on_appointment_cancelled` sem `cancelled_by` | Contradiz a regra do dono (liberar = devolver ou remarcar). Muda (§5.1) |
| O aviso ao paciente vai pelo job `send_cancellation_notice` (WhatsApp; três botões `rebooksame/rebookother/rebookno`) ou por texto simples no Portal | `services/appointment_release.py`, `workers/whatsapp/notifications.py` | Os botões novos entram aqui (§5.2, §5.3) |
| A consulta liberada fica CANCELADA; "remarcar" pelo `apptresched` lê só consultas futuras vivas | `workers/shared/actions.py`, `services/patient_context.py::load_upcoming_appointments` | "Remarcar" não pode reaproveitar o `enter_manage_action`; reaproveita o "Outro horário" (`enter_rebooking(same_professional=True)`), que já pergunta dia e depois horário (§5.5) |
| `pix_deposits.appointment_id` é UNIQUE e "nunca é reapontado" (a remarcação move a MESMA consulta) | `models/pix_deposit.py` | A transferência reaponta o sinal para a consulta nova, na mesma transação (§5.6) |
| Itens de atenção: deduplicados por `(tenant_id, dedupe_key)`; motivo `String(32)` dentro de `ATTENTION_REASONS` | `models/payment_attention_item.py`, `services/payments/attention.py` | Motivo novo `released_refund_due`, um item por sinal |
| A lista de dias ESCONDE dias cujos horários livres estão todos reservados (decisão do port da TASK-050) | `services/flow_router.py::enter_day_picker` (`holds=`) | Desfeito por decisão do dono (§5.8) |
| Botões no WhatsApp: título ≤ 20, até 3 botões, corpo interativo ≤ 1024; id ≤ 256 | `core/whatsapp_limits.py` | "Receber agora" (13) e "Remarcar" (8) cabem; o corpo corta a justificativa, nunca a explicação dos botões (§5.2) |
| Portal só aceita como AÇÃO um id que apareceu num cartão recente da própria conversa e da família de lembretes | `workers/portal/inbound.py` | A família nova entra na lista aceita (§5.4) |
| Uma cabeça Alembic: `e2b7c4a9d1f5` | `migrations/versions/` | Migração nova `b6d2e8f4a1c3` depois dela |

## 5. Desenho

### 5.1 A oferta (tabela nova `deposit_release_offers`, uma por consulta liberada)

Ao liberar (depois do `UPDATE ... CANCELLED` vencedor, na mesma transação): se o sinal da consulta está PAGO,
`services/release_offer.py::open_offer` grava a oferta (`state = open`, `deadline_at = agora + 24 h`, conversa do paciente) e
**não** chama `on_appointment_cancelled` (nem retém, nem abre item). `deposit_outcome` da resposta passa a ser
`refund_offered`. O aviso de 409 `retention_ack_required` (antes de liberar) continua exigido e passa a dizer: "O paciente já
pagou o sinal (R$ X). Ao liberar o horário, ele poderá remarcar usando o mesmo sinal ou pedir a devolução. Se ele não responder
em 24 horas, a clínica deve devolver o valor por inteiro pelo painel do provedor de pagamento." Sinal NÃO pago (aguardando):
como hoje (anula a cobrança ou "pagamento em voo").

Estados: `open` → `rescheduling` (tocou "Remarcar") → `rebooked` (nova consulta fechada, sinal transferido) **ou** `refund`
(tocou "Receber agora", ou o prazo venceu; `refund_source` = `patient` | `timeout`). Cada mudança é UM `UPDATE` com `WHERE
state IN (...)`: só um ganha.

### 5.2 A mensagem (texto exato)

Depois do texto de sempre ("O médico X desmarcou a sua consulta!" + a justificativa da clínica, ou a padrão "a consulta não foi
confirmada a tempo e o horário foi liberado."):

> Você já pagou o sinal desta consulta (R$ 60,00). Toque em *Remarcar* para escolher um novo dia e horário: o sinal pago vale
> para a nova consulta, sem nova cobrança. Se preferir, toque em *Receber agora* e a clínica fará a devolução do sinal em até
> 24 horas. Se você não responder em até 24 horas, a clínica também fará a devolução.

Botões: `relrefund|<id da consulta liberada>` "Receber agora" e `relresched|<id>` "Remarcar". Corpo ≤ 1024: se a justificativa
for longa, ela é cortada; o bloco acima nunca (`release_offer.fit_body`).

### 5.3 WhatsApp

O job `send_cancellation_notice` NÃO muda de assinatura (a API continua enfileirando os mesmos 7 argumentos): ele mesmo lê se
há oferta aberta para a consulta. Dentro da janela de 24 h: o cartão com os dois botões. Fora da janela: como hoje, só com a
autorização de mensagem paga, vai o modelo aprovado de cancelamento (botões "Outro horário"/"Outro médico"/"Cancelar"); com
oferta aberta, "Outro horário"/"Outro médico" valem como "Remarcar" e "Cancelar" vale como "Receber agora" (e depois segue a
pergunta de motivo de sempre). Sem autorização, nada sai (como hoje) e o relógio de 24 h cobre o silêncio.

### 5.4 Portal

O aviso vira um cartão com os dois botões (gravado com as opções, `BrainMessageSender.send_buttons`), mais o e-mail genérico de
"a clínica escreveu" de sempre. A família `relrefund|` / `relresched|` passa a ser AÇÃO no Portal quando o id apareceu num
cartão recente da própria conversa (`workers/portal/inbound.py`); fora disso, é texto comum.

### 5.5 Os toques (`workers/shared/release_offer_actions.py`, chamado primeiro por `_handle_action_button`)

- **Receber agora:** fecha como `refund` (fonte `patient`), abre o item `released_refund_due` (valor cheio, sugerido = valor
  pago) e responde: "Combinado! Avisamos a clínica, que fará a devolução do seu sinal de R$ 60,00 em até 24 horas. Se quiser
  marcar outra consulta, é só me escrever." Tocar de novo: "A devolução do seu sinal já foi pedida à clínica e será feita em
  até 24 horas." Se já remarcou: "O sinal que você já pagou está valendo para a sua nova consulta."
- **Remarcar:** `open` → `rescheduling`, o prazo passa a ser 24 h depois do toque (só no primeiro toque), e entra no "Outro
  horário" (mesmo médico, mesmo serviço; a pessoa atendida, se era terceiro, continua a mesma) com a frase no topo da lista de
  dias: "Ótimo! O sinal que você já pagou vale para a nova consulta." Se a devolução já foi pedida: segue um agendamento comum
  com a frase "A devolução do seu sinal já foi pedida à clínica, então a nova consulta segue as regras de sinal da clínica."
- **Prazo vencido sem a varredura ter passado:** o toque fecha como a varredura fecharia (fonte `timeout`) antes de responder.
- **Isolamento:** a oferta é procurada pela clínica da conversa que tocou e só vale para o paciente dessa conversa; senão
  "Não encontrei essa consulta." e nada muda.

### 5.6 Transferência do sinal (no fechamento da nova consulta)

- "Antes de confirmar" ligado: o portão de agendamento (`BookingGate._deposit_quote`) não pede Pix enquanto a conversa tem
  oferta em `rescheduling` dentro do prazo (`REASON_CARRIED_OVER`).
- No `INSERT` da consulta nova (`workers/shared/flow_runner.py::_apply_flow_result`, e na promoção da reserva do Portal
  `workers/shared/booking_hold.py::_promote_booking_hold`): `release_offer.claim_carry_over` fecha a oferta como `rebooked` e
  reaponta o sinal PAGO para a consulta nova, na mesma transação. A confirmação ganha: "O sinal que você já pagou vale para esta
  consulta. Não há nova cobrança." O gancho do "depois" (`maybe_create_deposit`) vê que a consulta já tem sinal e não cobra.
- `reschedule_count` não é lido nem incrementado: quem liberou foi a clínica.
- Se a varredura ganhou antes do fechamento: a consulta nasce sozinha (no "depois", o Pix normal é pedido; no "antes", o portão
  pede o Pix normal), e o item de devolução fica.

### 5.7 O relógio de 24 horas

Um passo novo na varredura de pagamentos que já existe (`workers/payments_cron.py::run_payment_sweeps`, a cada 4–6 min):
`release_offer.expire_offers` fecha como `refund` (fonte `timeout`) toda oferta `open`/`rescheduling` com prazo vencido e abre o
item; um commit por oferta, uma falha não para as outras. Não manda mensagem ao paciente (o aviso original já disse o que
acontece sem resposta). Nenhum cron novo, nenhuma mudança na contagem de crons.

### 5.8 Dias com horários reservados

- A lista de dias volta a NÃO descontar reservas (como na `main`): um dia com horário livre no Google aparece mesmo que todos
  estejam reservados por outra conversa.
- Ao escolher o dia: os horários reservados por OUTRA conversa ficam fora da lista de horários (como hoje) e o topo diz quais
  são: "Os horários 08:00 e 08:30 deste dia estão reservados para outro paciente, aguardando a confirmação ou o pagamento do
  sinal. Se o prazo terminar sem pagamento, eles voltam a ficar livres." Se TODOS estão reservados: "Todos os horários livres
  deste dia (08:00) estão reservados para outro paciente, aguardando a confirmação ou o pagamento do sinal. Se o prazo terminar
  sem pagamento, eles voltam a ficar livres: tente de novo mais tarde ou escolha outro dia." com só "Outro dia" (e voltar).
- Só horários (até 8, depois "e mais N"); nunca nome, telefone ou qualquer dado do outro paciente. Vale nos dois canais e nos
  dois ramos (agendar e remarcar), porque é o mesmo código.

## 6. Erros e corridas

| Situação | O que acontece |
|---|---|
| Toque "Receber agora" × relógio | um `UPDATE` ganha; um único item (dedupe por sinal); o perdedor responde o texto de "já pedida" |
| Toque "Remarcar" × relógio | se o relógio ganhou, o toque segue um agendamento comum com a frase de devolução já pedida |
| Fechamento da nova consulta × relógio | um ganha: ou o sinal muda de consulta e não há item, ou fica e há item (provado em Postgres real, opcional) |
| Duas liberações da mesma consulta | a segunda é `409 not_live` (já existe); uma oferta só por consulta (UNIQUE) |
| A consulta nova (com o sinal transferido) é liberada de novo | nova oferta para ESSA consulta; o item de devolução continua no máximo um por sinal |
| Pix pago depois da liberação (em voo) | como hoje: `paid_then_cancelled`, sem oferta |
| Envio do aviso falha / fila fora | como hoje (`notice_failed`, `queue_unavailable`); a oferta fica e o relógio cobre |
| Paciente fora da janela do WhatsApp, sem autorização paga | nada sai (como hoje); a clínica vê o link para escrever à mão; o relógio cobre |
| API nova + worker velho (janela de deploy) | o worker velho manda o cartão velho (3 botões) com o texto novo e não fecha ofertas; por isso o worker vai logo depois da API |

## 7. Testes (TDD; rodar em cópia descartável com `BOT_ALLOWLIST_WA_IDS=""`)

- Migração: cabeça única, só cria a tabela, modelo igual à migração; ida-e-volta em Postgres real (opcional, `PAYMENTS_TEST_PG_URL`).
- Serviço: abrir é idempotente e só com sinal pago da mesma clínica; devolução abre UM item com valor cheio; remarcar move o
  prazo uma vez; transferência uma vez só, nunca entre clínicas; relógio fecha `open`/`rescheduling` vencidas e poupa o resto;
  toque perdendo para o relógio não abre segundo item; corpo ≤ 1024 sem cortar a explicação.
- Hub: o 409 traz `refund_offered` qualquer que seja a política de retenção; com o aceite, a oferta abre e nada é decidido; o
  job recebe os mesmos 7 argumentos; Portal recebe o cartão com os dois ids.
- Job: cartão com os dois botões dentro da janela; modelo aprovado fora dela.
- Toques: devolução, repetição, remarcar (lista de dias com a frase), remarcar depois da devolução, prazo vencido, botões do
  modelo, outro paciente, outra clínica, Portal (id em cartão recente vale; id nunca mostrado não vale).
- Transferência: WhatsApp e Portal (promoção da reserva), sem toque nada transfere, depois do relógio nada transfere, o portão
  do "antes" não pede Pix.
- Relógio: a varredura fecha e abre um item, duas vezes = um item.
- Dias: dia todo reservado aparece e, ao escolher, diz os horários sem dado de ninguém; dia parcialmente reservado lista os
  livres com o aviso; formatação dos horários.
- Front: o texto do aviso de liberar não diz "retido" e cita remarcar e 24 horas.

## 8. Dependências e deploy

- Base: `task/TASK-050-secretaria` @ `3650e10`. Integra em `feature/out-of-mvp` (nunca `main`), depois da TASK-050.
- Independente da TASK-051 (não tocam os mesmos arquivos de backend; no front, a TASK-051 não toca `lib/agenda/release.ts`).
- Deploy (NÃO autorizado; skill frozen-contract-migration): migração `b6d2e8f4a1c3` (aditiva) → API → worker logo em seguida
  (a API abre ofertas; o worker manda os botões, trata toques, transfere o sinal e fecha as vencidas) → front por último.

## 9. Decisões tomadas (padrão) — o dono pode trocar qualquer uma

- **Liberar com sinal pago = devolver por inteiro ou remarcar, independente da política de retenção**, porque quem liberou foi
  a clínica (igual ao cancelamento pela clínica, que já devolve por inteiro). Antes, liberar dentro da janela podia RETER o sinal.
- **"Remarcar" = "Outro horário"** (mesmo médico e serviço; dia, depois horário), porque a consulta liberada está cancelada e o
  fluxo de remarcar da conversa só vê consultas vivas; não reabrimos a consulta cancelada.
- **Tocar "Remarcar" dá 24 horas a partir do toque (uma vez)**, porque quem toca no fim do prazo precisa de tempo para escolher.
- **Transferir = reapontar o sinal para a consulta nova**, sem nova cobrança, sem acerto de diferença de preço, sem contar como
  remarcação do paciente.
- **Motivo novo `released_refund_due`** ("Horário liberado pela clínica: devolver o sinal ao paciente."), sem e-mail novo
  (`EMAIL_REASONS` é decisão de produto); a clínica vê "precisa de decisão" na agenda até a tela F4 existir.
- **O relógio não manda mensagem ao paciente**, porque o aviso original já disse que sem resposta a clínica devolve, e fora da
  janela do WhatsApp a mensagem livre nem chegaria.
- **Fora da janela de 24 h do WhatsApp, os botões do modelo aprovado ("Outro horário"/"Outro médico"/"Cancelar") respondem a
  oferta**, porque não há modelo aprovado com os dois botões novos (criar um exige a Meta).
- **Botões levam o id da consulta liberada** (família `relrefund|`/`relresched|`), porque o tratador já carrega a consulta pela
  clínica da conversa (isolamento pronto) e a oferta é única por consulta.
- **O aviso de horários reservados vale para reserva de Pix E de código de e-mail** (a tabela de reservas não separa no aviso),
  com texto que cobre os dois ("aguardando a confirmação ou o pagamento do sinal").
- **Só os caminhos determinísticos transferem o sinal** (fluxo e promoção do Portal), não o agente de IA, porque o "Remarcar" é
  determinístico; se o paciente agendar pela IA, a consulta segue as regras normais (no "antes", a trava da IA já bloqueia).
- **Pix pago depois da liberação não vira oferta** (continua `paid_then_cancelled`), porque a decisão de devolver ou remarcar foi
  pedida para o sinal JÁ pago no momento da liberação.

## 10. Bloqueios

Nenhum que impeça planejar. Itens NÃO PROVADOS até a execução: envio real pelo WhatsApp (cartão e modelo), tela do Portal
desenhando o cartão gravado (o front já desenha cartões de botões gravados; não testado com estes ids), e qualquer coisa em
produção.
