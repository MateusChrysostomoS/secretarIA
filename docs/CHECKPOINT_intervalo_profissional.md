# Checkpoint: intervalo entre consultas do profissional (TASK-088 §B)

Estado: regra do núcleo e chamadores implementados (secretarIA, local, sem push/deploy). O campo na tela do
portal é feito à parte.

## Regra

`Professional.buffer_minutes` = N (0 a 120, NULL/0 = sem intervalo). Um horário só é oferecido e só é
confirmado se deixa N minutos livres antes e depois de cada **consulta** do profissional. É folga ao redor
de consulta existente: a duração do serviço, o horário mostrado e o evento gravado na agenda não mudam, e a
agenda manual da clínica nunca é limitada.

Fronteira: folga de exatamente N minutos é aceita; menos que N tira o horário (vale antes e depois).

## O que conta como consulta (e o que não conta)

Conta:

- linha de `appointments` viva (`LIVE_APPOINTMENT_STATUSES`, e não suspensa por troca aguardando Pix:
  `occupies_slot_clause`) **com paciente**, que não seja `appointment_type = "Bloqueado"`, do profissional da
  agenda (ou, na agenda própria da clínica, as linhas sem profissional) **ou de qualquer irmão dele na agenda
  compartilhada** (mesma pessoa em outras clínicas, TASK-083: a folga é da pessoa);
- janelas seguradas (`holds`) que o chamador passa (Pix em andamento de outra conversa).

Não conta (não engorda):

- bloqueios da clínica (almoço, férias, dia bloqueado: linha `Bloqueado`, sem paciente);
- eventos de dia inteiro ou transparentes (os dois backends já os ignoram como ocupação);
- qualquer evento sem linha em `appointments` (digitado direto no Google, evento local que ninguém marcou).

Por quê: o calendário só devolve `{id, summary, start, end}` e a janela de um irmão chega sem tipo, então a
única fonte que separa consulta de bloqueio é a linha de `appointments`. Decisão do desenhista (item 2):
quando não dá para distinguir, escolhe-se o lado que oferece **menos** folga indevida. Efeito conhecido: um
compromisso real digitado direto no Google não ganha folga.

## Onde está o código

- `services/availability.py`: `booking_buffer_minutes` (clamp 0..120), `padded`,
  `read_consultation_windows`, `free_slots_for_day` / `available_day_starts` / `days_with_free_slots`
  (parâmetros `buffer_minutes` e `consultations`), `buffer_taken_at_confirm` (reconferência na
  confirmação/remarcação, fail-open, com `ignore_event_id`).
- `services/availability_windows.py`: `scan_free_windows(buffer_minutes=)`.
- `services/agenda.py`: `consultation_windows` (a consulta ao banco) e `read_consultation_windows`
  (sessão própria curta, como `shared_busy_provider`).
- `services/calendar.py`: `CalendarService.consultation_windows` (a costura que `LocalCalendarService`
  herda). Um calendário sem essa costura significa "nenhuma consulta conhecida": nada é engordado.

## Custo de leitura

Com N = 0 nada muda e nenhuma leitura extra acontece. Com N > 0: as janelas de consulta são lidas **uma vez**
para o período inteiro (lista de dias, varredura da IA) e só os dias que uma janela engordada toca são relidos
horário a horário; nunca uma leitura por dia. Reconferência: uma leitura curta ao redor do horário.

## Falha na leitura (ofertas)

Se a leitura das consultas falhar num caminho que **oferece** horários (lista de dias, lista de horários,
ferramenta de disponibilidade da IA, lista de horários das ferramentas v1), o horário é oferecido **sem a
folga** e um aviso é registrado (`booking_buffer_offer_read_failed`, só o tipo do erro): fail-open, como a
reconferência. A checagem de ocupação do próprio calendário continua impedindo sobreposição, e a
reconferência na confirmação pergunta de novo (`services/availability.py::offer_consultation_windows`).

## Caminhos ligados (TASK-088 Task 5)

A folga é sempre a do profissional dono da agenda sendo lida/escrita (nunca a seleção velha da conversa) e o
agendamento que está sendo movido nunca engorda a si mesmo. Recusa na confirmação = a mesma resposta de
"horário ocupado entre a lista e o toque" (mesma mensagem, mesma re-oferta do dia).

- Lista de dias e de horários do fluxo (`flow_router.enter_day_picker`, `_enter_slot_picker`, `_ask_day`),
  nas três ramificações (agendar, remarcar, "Alterar dados") e no "Outro horário" (`_days_for`).
- Rascunho da IA (`booking_draft._land_day`), remarcação pedida pela IA (`manage_request._land_new_day`).
- Ferramenta de disponibilidade da IA (`ai/availability_tool.py`) e as ferramentas v1
  (`list_free_slots`, `list_free_slots_for_professional`, e a reconferência de `create_event`,
  `create_event_for_professional`, `create_event_at_unit`).
- Reconferência na confirmação: agendar ("Confirmar" e a re-checagem após sinal dispensado), remarcar,
  "Outro horário", promoção do código/Pix (`workers/shared/booking_hold.py::_held_slot_unavailable`),
  "Alterar dados" (`appointment_edit_flow._slot_free`, `_availability`, `_apply_confirm`) e troca de serviço
  paga (`service_change.sync_agenda_event(buffer_for_patient=True)`, só quando a troca é do paciente).
- Nunca na agenda da clínica: `api/hub/*`, `services/staff_appointment_edit.py` e a troca de origem `hub`.

## Pendências

- campo na tela do portal;
- `old_slot.place_new_event_after_payment` (evento criado depois do Pix da diferença) não reconfere a folga:
  a janela foi validada e segurada por um hold de diferença no pedido.
