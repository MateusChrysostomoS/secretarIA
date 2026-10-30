# CHECKPOINT — TASK-072: nenhum horário fica guardado; troca de e-mail mantém o agendamento

**Data:** 2026-10-29 · **Origem:** bateria de testes do Portal (Instituto Gicovate) e decisões do
dono no mesmo dia (`BRAIN/tasks/TASK-072/TASK.md`, itens Imp 2 e Imp 4) · **Estado: LOCAL,
commitado em `task/TASK-072-fluxo` (base `main` 77b6017). Sem push, sem deploy. Exige migração
`d4f8a2c6e1b9` ANTES do deploy de API + worker.**

Atualiza `CHECKPOINT_secretaria_booking_code_gate.md` (a "reserva de 10 minutos" deixou de
existir como reserva) e `CHECKPOINT_greeting_frame_lgpd.md` (o gate de consentimento passou a
ler um booleano explícito).

## 1. Decisões do dono (vinculantes)

1. **Nenhum horário é guardado para um paciente por tempo.** O horário escolhido (e médico,
   serviço, convênio, "pra quem") acompanha o paciente pelo código, pelo reenvio e pela troca de
   e-mail, mas **não bloqueia** o horário para ninguém. A frase "Guardei o horário … por 10
   minutos" saiu de todo texto ao paciente.
2. **Dupla checagem ao gravar na agenda.** Se o horário foi ocupado enquanto o paciente digitava
   o código, o fluxo **não termina**: avisa e volta para "Para quando você gostaria? Escolha um
   dia:" mantendo convênio/serviço/médico/pra-quem. O paciente já verificado conclui sem novo
   código.
3. **`lgpd_accepted` explícito** em `patients`; depois de verdadeiro, o aviso LGPD nunca mais é
   enviado a esse paciente.
4. **"Mudar e-mail" preserva o agendamento**: e-mail novo → código no endereço novo → mesmo
   horário; sem LGPD de novo, sem pedir nome, sem voltar ao menu.
5. Pix/sinal (fase `payment`) **fora do escopo** — continua bloqueando como antes.

## 2. O que mudou

| Onde | Mudança |
|---|---|
| `services/booking_hold.py` | `BLOCKING_PHASE = "payment"`: `held_windows`, `live_hold_overlaps` e o teste de conflito de `place_hold` só consideram linhas de **Pix**. Linha da fase `code` é só "a escolha em trânsito". `HOLD_TTL_MINUTES` virou 60 (memória da escolha, não reserva). `refresh_code_hold` renova a linha a cada código novo. `extend_to_payment` agora faz o teste de conflito (serializado) antes de virar Pix — dois pacientes podem carregar o mesmo horário pelo código, mas só um vira Pix. |
| `workers/shared/booking_hold.py::_promote_booking_hold` | Não recusa mais por `expires_at`. Dupla checagem em `_held_slot_unavailable`: horário já começou, Pix de outra conversa no horário, ou evento na agenda onde o agendamento seria gravado (`slot_taken_at_confirm`, fail-open). Ocupado → `SlotTakenAtPromotion` com o seletor de dia. |
| `services/flow_router.py::_reoffer_taken_slot` | No ramo de **agendamento**, "ocupado na hora de gravar" (confirmação comum E promoção pós-código) volta ao seletor de dia (`enter_day_picker`, prefixo `SLOT_TAKEN_AT_CONFIRM_MESSAGE`). Remarcação mantém o comportamento anterior (horários do mesmo dia). |
| `services/pending_identity.py` | `_BOOKING_GATE_SLOT_LINE` ("📅 Horário escolhido: … Só falta confirmar o seu e-mail para finalizar o agendamento.") substitui a linha "Guardei…". `booking_gate_body`/`booking_gate_reprompt_body` não recebem mais minutos. |
| `workers/orchestrator.py` (ramo `pending_email_claim`, paciente já consentido) + `workers/portal/identity.py::_continue_after_email_change` | Era aqui o bug do "Ivo": o ramo mandava o aviso LGPD de novo e deixava a conversa IDLE. Agora pede o código ao endereço novo, renova a escolha e mostra o card do código com o mesmo horário. Reenvio (`identity_resend`) também renova. |
| `models/patient.py` + migração `d4f8a2c6e1b9` | Coluna `lgpd_accepted BOOLEAN NOT NULL DEFAULT false`, backfill `true` onde `lgpd_accepted_at` não é NULL. Todo leitor do gate (`turn_router`, `portal/identity_gate`, `portal/identity`, `portal/open`, `orchestrator`, `api/internal` anexos) lê o booleano. `lgpd_accepted_at` continua como auditoria ("quando"); um `@validates` mantém os dois coerentes para quem só carimba/limpa o timestamp (reset de debug). |

## 3. Deploy (quando autorizado)

1. `alembic upgrade head` (migração `d4f8a2c6e1b9`) com os dois serviços ainda antigos.
2. Deploy de `secretaria_api` **e** `secretaria-worker` juntos (`GET /build` parity).
3. Se algum paciente aceitou o LGPD entre 1 e 2, rodar de novo o `UPDATE … SET lgpd_accepted =
   true WHERE lgpd_accepted_at IS NOT NULL` (o código antigo só grava o timestamp).

## 4. Testes

`tests/test_booking_code_gate.py` (seções 2, 5-6 e 10) e `tests/test_confirm_recheck_slot.py`
(seções 2 e 4, ponta a ponta: código → ocupado → seletor de dia → dia → horário → confirmar,
sem segundo código).

## 5. Riscos / pendências

- Dois pacientes no mesmo horário ao mesmo tempo: quem gravar primeiro leva; o segundo volta ao
  seletor de dia depois de digitar o código (é o comportamento pedido).
- Clínica com sinal **antes** (`deposit_timing='before'`) no Portal: o código passa a não
  reservar; o conflito é checado quando a linha vira Pix (`extend_to_payment`). Comportamento do
  Pix em si inalterado.
- Agenda Google sem leitura (falha) continua fail-open na dupla checagem, como antes.
