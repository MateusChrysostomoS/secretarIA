# "Outra data": calendário de dias livres (2026-11-04)

Pedido do dono, testando o Portal: a lista de dias para em 20 dias, e o paciente pode querer uma data distante. Só os dias disponíveis pelas configurações do médico podem ser escolhidos.

## O que mudou

- Lista curta (`enter_day_picker`): a linha "Ver mais dias" deu lugar a "🗓️ Outra data" (`daycal|<página>`), sempre presente. O limite de 10 linhas continua valendo.
- "Outra data" abre `calendar_view`: uma leitura do Google para `DAY_CALENDAR_WINDOW_DAYS` (180 dias) a partir de hoje, com as mesmas regras da lista curta (horários do médico, agenda e antecedência mínima). Só é paga quando o paciente pede.
- Sem nenhum dia nos próximos 20, a tela abre sozinha com a busca longa, em vez de dizer que não há agenda. Sem dia em 180 dias, segue a mensagem de sempre e o menu.
- `SlotsBubble.calendar_days` leva todas as datas. O remetente que declara `SUPPORTS_CALENDAR` (só `BrainMessageSender`) grava `interactive.calendar.days`, fora de `options`: o limite de linhas, o histórico da IA e a lista do console não mudam.
- Toque no calendário: volta como uma linha de dia (`day|<iso>|0`). `offered_reply_ids` aceita só as datas do cartão, então uma data forjada segue recusada.
- WhatsApp: nada de calendário. "Outra data" continua pela lista, paginada com `daymore|<página>|cal`, que permanece na busca longa. `daymore|<n>` de cartões antigos continua funcionando.

## Contrato

`interactive.calendar` é opcional em `InteractiveRead` e vem só em cartão de lista. O brain-api repassa a mensagem sem filtrar, então não precisa mudar. O par do Portal está em `Brain-Message-Frontend/lib/day-calendar.ts`.

## Decisões sem consulta

- Janela de 180 dias: não existe configuração de horizonte máximo por médico. Mudar é trocar uma constante.
- No reagendamento o calendário abre a partir de hoje, não em torno da consulta atual.
- Depois de escolher uma data no calendário, "⬅️ Outro dia" volta para a lista curta, de onde "Outra data" reabre o calendário.

## Limite conhecido

Feriados continuam não bloqueados (pendência já registrada): um feriado sem evento na agenda aparece como disponível, na lista e no calendário.

## Deploy

Sem migração. API e worker juntos, depois o Portal. Portal antigo com backend novo: o cartão aparece como lista paginada normal, sem quebrar. Backend antigo com Portal novo: nada muda.

## Validação

Testes em `tests/test_flow_day_calendar.py`. Tela provada em navegador pelo demo do Portal (`/componentes/portal/`); o fluxo real em produção depende do deploy.

Suíte completa em 2026-11-04: 8 falhas em 7.728 testes. Três eram testes desatualizados por esta mudança e foram corrigidos; os arquivos relacionados passam depois da correção (a suíte inteira não foi repetida).

As outras cinco existem sem esta mudança e não foram corrigidas aqui:

- `test_service_change_agenda.py` (2): `test_service_change.NOW` é fixo em 2026-10-26, e a consulta remarcada já ficou no passado, então nenhum lembrete é planejado. Falham em qualquer rodada e passam com o planejador nessa data.
- `test_payment_attention_hub.py` (1) e `test_portal_context_opening.py` (2): `NOW` é calculado na importação e envelhece durante a rodada, que leva 30 minutos. Passam sozinhos e falham na `main` limpa com `NOW` 25 minutos atrás.
