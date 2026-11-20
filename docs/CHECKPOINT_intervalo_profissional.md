# Checkpoint: intervalo entre consultas do profissional (TASK-088 §B)

Estado: regra do núcleo implementada (secretarIA, local, sem push/deploy). Os chamadores (fluxo guiado,
ferramenta de disponibilidade da IA, reconferência na confirmação, remarcação) são ligados à parte.

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

## Pendências (fora desta tarefa de núcleo)

- ligar `buffer_minutes` do profissional nos chamadores (fluxo, `booking_draft`, ferramenta da IA,
  confirmação e remarcação) e a tela do portal;
- o evento que está sendo remarcado não se engorda a si mesmo: passar `ignore_event_id` (o
  `google_event_id` da consulta) em `buffer_taken_at_confirm`.
