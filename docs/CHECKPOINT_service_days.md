# CHECKPOINT — Dias da semana por serviço + dia da semana nas listas do paciente (TASK-087)

Estado (2026-11-19): **pronto, revisado (T11: READY) e validado, só LOCAL. Commitado na branch `task/TASK-087-dias-do-servico` (worktree
`BRAIN-worktrees/TASK-087/secretarIA`, base `c7423f0` = branch `task/TASK-083-multi-unidades` com 085+086); NÃO foi
para `main`, NÃO houve push, NÃO foi deployado.** Deploy, merge e push só a pedido do dono (`Deployment: NOT
AUTHORIZED`). Registro central: `../tasks/TASK-087/` (raiz de BRAIN: `SPEC.md` = autoridade, `PLAN.md`,
`results/`, `evidencias/`). Par: `Brain-Message-Frontend` (chips na tela /contexto; commits `7f35778` e `7749bbc` na
branch homônima da worktree TASK-087). Graphify **não** foi reconstruído na branch (mesmo motivo da TASK-083: o
grafo versionado só geraria conflito de merge); fica STALE até um `update` depois do merge.

## Por quê

O médico quer que um serviço seu só possa ser marcado **pelos pacientes** em certos dias da semana (ex.: "Cirurgia"
só às terças). No /contexto, em cada serviço oferecido, aparecem os dias em que ele atende (todos marcados) e ele
desmarca os que não valem. Junto, o dono pediu que toda lista de datas do paciente traga o dia da semana abreviado
("Seg, 24/11").

## Decisões vigentes (SPEC §2 e §9 — não reabrir)

- **D1** dias escolhidos por unidade (médico sem unidade: uma fileira). **D2** guardamos os dias **bloqueados**, não os
  liberados (dia novo que o médico abre entra marcado para os serviços existentes). **D3** a regra vale só para o
  **paciente**; a recepção não é bloqueada. **D4** rótulo abreviado "Seg, 24/11"; em frase, minúsculo "seg, 24/11".
  **D5** mesmo molde do preço/duração por unidade da TASK-085 (base no serviço do médico + exceção na mesma linha).
- **R1** unidade ainda não escolhida (IA, "qualquer profissional", oferta de colega): o dia entra se o serviço pode
  ser feito nele em pelo menos uma das unidades do médico (só some o dia bloqueado em TODAS); escolhida a unidade,
  filtra pela dela; na confirmação, dia bloqueado na unidade final é recusado. **R2** médico que herda a lista da
  clínica segue o mesmo caminho do preço ao salvar. **R3** dias próprios saem/ficam junto com o preço próprio quando
  o serviço é desligado ou a unidade sai. **R4** a regra só é checada quando muda dia, horário, serviço, médico ou
  unidade; trocar convênio/dados/pagamento não re-checa, e consulta já marcada em dia agora bloqueado continua
  valendo. **R5** serviço ainda desconhecido: sem filtro até fixar o serviço (a confirmação cobre o resto). **R6** o
  filtro é um parâmetro explícito do calendário (não apagar chaves das horas: horas vazias caem no padrão 08–18 e
  reabririam tudo). **R7** unidade cujos dias de atendimento ficam todos bloqueados sem chegar a 7: aceito em
  silêncio no servidor; a tela impede. **R8** falha ao ler os dias na consulta de disponibilidade da IA cai para a
  lista BASE (como `_unit_minutes`, TASK-085 D7): aceito porque todo `create_event*` relê os dias e RECUSA em falha
  (fail-closed).

## Modelo de dados

- **Base (médico × serviço):** chave opcional `blocked_weekdays` na entrada de `Professional.appointment_types`
  (lista de `monday…sunday`, sem repetição, gravada na ordem da semana). Ausente/`[]` = nada bloqueado — nenhuma
  clínica existente precisa de migração de dados.
- **Exceção por unidade:** coluna nova `professional_service_unit_terms.blocked_weekdays` (JSON, nullable) em
  `models/professional_service_unit_terms.py`. `NULL` = herda a base; `[]` = próprio, nada bloqueado nesta unidade
  (mesmo que a base bloqueie); lista = próprio, estes dias bloqueados.
- **CHECK `ck_psut_price_or_duration`** (mesmo nome): `price IS NOT NULL OR duration_min IS NOT NULL OR
  blocked_weekdays IS NOT NULL` (uma linha só com dias é válida).
- **Migração `c8e2a4f6b1d3_service_unit_blocked_weekdays`** (`down_revision = f1c4b8d2a6e3`, head da 085/086):
  `batch_alter_table` adiciona a coluna e troca o CHECK (compatível com o SQLite dos testes). O downgrade apaga as
  linhas só-com-dias, recria o CHECK antigo e remove a coluna.
- **Resolução** em `services/service_terms.py`: `WEEKDAY_KEYS`, `UnitTerms.blocked_weekdays`,
  `blocked_weekdays_for(entry, terms_by_unit, unit_id)` (exceção da unidade, se não NULL, > base; sem unidade = base),
  `blocked_weekdays_everywhere(...)` (interseção dos bloqueios das unidades do serviço = R1) e
  `blocked_weekdays_at(session, tenant_id, professional_id, entry, unit_id)` (leitura filtrada pelo tenant, usada
  pela IA). Chaves desconhecidas são ignoradas na leitura.

## O ponto único da regra

`services/calendar.py::CalendarService.with_blocked_weekdays(blocked)` devolve uma cópia (como `with_unit`) cujo
`_windows_for_day` responde `[]` no dia bloqueado, checado **antes** do padrão 08–18. Como lista de dias, lista de
horários e `is_slot_free` saem todos de `_windows_for_day`, os três caem juntos. O conjunto **substitui** o anterior
(não acumula); `LocalCalendarService.mirroring` o copia.

Helpers que aplicam/consultam o filtro (nenhum caminho do paciente reimplementa a regra):

- `services/unit_booking.py`: `service_calendar(calendar, blocked)` (sem bloqueio = o mesmo objeto), `blocked_on`,
  `weekday_plural_pt`, `weekday_list_pt` (dias com artigo: "às segundas e sextas", "às sextas e aos sábados"),
  `service_day_refusal(serviço, médico, dias)` — a frase "<serviço> com <médico> não está disponível às segundas.
  Posso ver outro dia?" (revisão P6: concorda com qualquer nome de serviço).
- `services/flow_units.py`: `blocked_weekdays_of(professional, entry, unit_id)` (sem unidade = o bloqueado em
  todas + o dia em que toda unidade que ABRE nele o bloqueia, R1/revisão P2; sem entrada = nada, R5). A agenda do
  paciente com unidade + dias é montada só em `flow_router._service_scoped` (substitui o conjunto anterior).
- `services/service_terms.py`: os resolvedores acima.

## Onde cada caminho do paciente aplica

- **Fluxo de botões** (`services/flow_router.py`): `_service_scoped` / `_booking_calendar` (agenda da unidade + dias
  do serviço) alimentam `_ask_day`, o passo de catálogo, os passos de unidade, a retomada (`_resume_bubbles`) e a
  reoferta do Portal (`reoffer_taken_booking_slot`). `_service_day_check` + `_handle_day_step(refuse_day=...)`
  recusam data digitada ou toque em lista velha. `_handle_confirmation` recusa dia bloqueado na unidade final ANTES
  do prazo mínimo, da releitura e do gate (nada de evento, reserva ou Pix). `booking_draft` e
  `manage_request._land_new_day` (IA entregando o rascunho/novo dia) descartam o dia e devolvem seletor.
- **Remarcação:** `_appointment_unit_calendar` + `_appointment_day_check` (dias do serviço da consulta, na unidade
  dela; várias unidades e nenhuma gravada = R1). Um bloqueio só em `_manage_reschedule`, antes do desvio para
  `_manage_rebook`: cobre os dois e nada é movido nem criado. Botão de lembrete/da consulta herda.
- **Oferta de colega** (`enter_rebooking`, "Outro horário"): `_rebooking_calendar` (agenda do colega sem os dias
  bloqueados, R1).
- **"Alterar dados"** (`services/appointment_edit_flow.py`): `_blocked_days` + `_calendar(ctx, draft, professionals,
  tenant)` é o ponto único do fluxo (lista de dias/horários, "Mudar data/horário", livre-após-trocar,
  dia proposto pela IA); `_doctor_list` mostra "Ocupado" para médico que não faz o serviço no dia; `_service_step`
  não confirma direto quando o dia já marcado é bloqueado para o novo serviço; `_apply_confirm` é a defesa final
  (volta ao seletor, não escreve na agenda). R4: trocar só convênio/paciente não checa o dia.
- **IA** (`ai/`): `availability_tool._unit_blocked_days` (cada unidade varrida com `service_calendar(...)`; sem
  serviço nomeado, sem filtro — R5; falha de leitura cai para a base — R8); `unit_scope.service_day_refusal_for` e
  `service_entry_of`; `tools._service_day_refusal` chamada em `create_event`, e nos plugins
  `multi_professional.create_event_for_professional` e `multi_unit.create_event_at_unit` (depois do horário local,
  ANTES de qualquer escrita; falha de leitura recusa com `SERVICE_DAYS_LOOKUP_FAILED_ERROR`, revisão A4).
  A ferramenta de disponibilidade lê as exceções UMA vez por chamada e só varre as unidades onde o médico faz o
  serviço (marcas da 083; revisão A5). `services/llm_context.py` diz por serviço (e por unidade) "(não às segundas e
  sextas)" / "(não às sextas e aos sábados)" — só quando há bloqueio; sem bloqueio o texto é idêntico ao de antes.
- **Entradas que montam o retrato do médico fora do `route()`** (revisão P1): botões de remarcar após cancelamento
  do médico e "Remarcar" do lembrete (`workers/shared/actions.py::_router_roster`) e as entradas de agendamento
  guiado/rascunho legado (`workers/shared/sentinels.py`, `ctx.professionals`) usam o retrato com unidades, marcas e
  exceções por unidade — sem isso a lista de dias usava só a base.

## O que fica FORA da regra (D3)

Agenda da recepção (`api/hub/calendar.py`, `staff_appointment_edit`) — provado no navegador: a recepção marca num dia
bloqueado (HTTP 201), sem aviso. Escritas cuja janela já foi escolhida: `service_change`, promoção de Pix, hold,
sync de calendário, `release_*` (elas herdam um horário que já passou pela regra; as escritas `create_event`/
`update_event` não olham a janela). Também fora: bloqueio por horário, por data (feriado) e consultas já marcadas num
dia depois bloqueado.

## Contrato de fio do hub (back × front conferido na Integração 2: sem divergência)

- `PUT /tenants/me/configuration` e `PUT /tenants/me/professionals/{id}/config` aceitam `blocked_weekdays` na entrada
  (`ProfessionalAppointmentType`, base) e em cada item de `unit_terms` (`UnitTermsItem`: válido com preço, duração
  ou dias). `unit_terms` presente continua **substituindo** as exceções do serviço. O front manda a exceção da
  unidade inteira (preço, duração e dias juntos); unidade que herda tudo não gera linha. `ServiceOffer` (criar
  serviço no catálogo, `schemas/service.py`) também aceita `blocked_weekdays`.
- `apply_to_all` agora é **por assunto** (`services/hub_unit_terms.py::apply_unit_terms`): zera só preço/duração
  próprios (ativas e inativas; apaga as linhas que ficam sem nada) e preserva os dias; pode vir junto com
  `unit_terms` só-dias. A recusa antiga de `apply_to_all` + `unit_terms` saiu. Desde a revisão (F1) o Portal manda
  `unit_terms` SEMPRE junto com `apply_to_all` (mesmo `[]`), para que dias próprios que a tela já tirou não fiquem no
  servidor.
- **"Ausente = mantém" para os dias da base** (revisão D1, `hub_unit_terms.keep_stored_blocked_weekdays`, chamado em
  `hub_configuration.apply_professional_config`): entrada que chega SEM a chave `blocked_weekdays` (tela
  `/configuracao` do secretarIA-frontend, aba antiga do Portal) herda os dias guardados da mesma entrada (por
  `service_id`, senão por nome); `null` explícito limpa (`[]`). Sem isso, salvar só um preço por outra tela apagava os
  bloqueios.
- A coluna usa `JSON(none_as_null=True)` (revisão D2): `None` em Python vira NULL de SQL (o CHECK e o "herda a base"
  dependem disso), nunca JSON `null`. Sem migração.
- **422 `blocked_weekdays_invalid`** (`{code, message}`, mesmo formato das recusas de `unit_terms`;
  `check_blocked_weekdays`): chave desconhecida, repetida ou os 7 dias. "Não sobra dia em que o médico atende" não é
  validado no servidor (R7); é impedido na tela.
- Leitura: `entries_with_terms` devolve `blocked_weekdays` em cada item (`null` = herda a base). A tela trata `null`
  da base como "nada bloqueado".

## Dia da semana nas listas do paciente (D4)

Um helper só, `services/booking_dates.py::short_day_label` ("Seg, 24/11") e `short_day_phrase` ("seg, 24/11"), usados
por: título dos horários ("Horários livres em seg, 24/11:" / "Novos horários em ..."), `_appt_row_label` (só dia e
hora; o serviço vai na descrição da linha — revisão P4), "Livre/Ocupado
seg, dd/mm hh:mm" do "Alterar dados", notas de intervalo da IA ("de seg, 05/10 a seg, 12/10"), `FreeWindow.payload()`
(ganhou `weekday`) e a allowlist `ai/tool_output.py` (mantém `weekday` em `windows[]`; sem isso a IA nunca o veria).
Os botões de dia já eram assim. Limite do WhatsApp (24 caracteres) respeitado. Confirmações, lembretes e e-mails não
mudaram. Seis testes antigos tiveram só a asserção atualizada para os novos rótulos (`test_availability_windows*`,
`test_manage_timezone`).

## Validação

- **Revisão final (T11), código em `dd8be65`:** a suíte completa foi interrompida aos 68% (6953 testes) pelo próprio
  Claude Code por falta de memória na máquina (não por falha) — até ali, só 3 falhas, todas do mesmo teste
  (`test_manage_timezone::test_multiple_appointments_local_labels_keep_original_iso_ids`, que fixava o título antigo
  cortado "… Consul…" — justamente o defeito da revisão P4; expectativa atualizada, 21/21). Os 32% restantes foram
  cobertos em lotes curtos onde a T11 mexeu: 576 (lembretes, botões do worker, remarcação, camadas do worker,
  sinal/remarcar) + 972 (unidades, exceções por unidade, "Alterar dados", rótulos, WhatsApp, prompts, todos os
  `test_service_days_*`) + 47 (os 4 arquivos de sinal que chamam os botões do worker ou a tabela de exceções) = 1595
  passed, 0 falhas. Não re-rodados na T11: ~84 arquivos de áreas que a T11 não toca (ofertas de vaga, webhook,
  segurança, assinatura…; conferido por busca) — passaram na suíte completa da T10 (`d9f81c6`). `ruff check` limpo
  nos 46 `.py` alterados desde `c7423f0`; `alembic heads` = só `c8e2a4f6b1d3`.
- **Suíte completa do secretarIA no HEAD da branch (`d9f81c6`, `BOT_ALLOWLIST_WA_IDS=""`):** 9981 passed, 131 skipped,
  1 xfailed, **2 failed** — as 2 antigas aceitas de `tests/test_service_change_agenda.py` (flake de horário UTC, ver
  memória `secretaria-human-backup-utc-flake`). Nenhuma regressão nova. `ruff check` limpo nos 41 `.py` alterados
  desde `c7423f0` (nunca `ruff format .`).
- Nota de ordem de teste: `test_get_availability_tool::test_the_agent_builds_the_tool_behind_the_guard` e três de
  `test_ai_tool_output_allowlist` (`ANTHROPIC_API_KEY missing`) falham só quando certos arquivos rodam juntos em lote;
  idêntico na base e passam na suíte completa/sozinhos — não é regressão.
- Testes novos: `test_service_days_terms`, `test_migration_service_days`, `test_hub_service_days`,
  `test_service_days_calendar`, `test_service_days_flow`, `test_service_days_edit_flow`, `test_service_days_ai`,
  `test_weekday_labels`. Mutação confirmada na regra central (tirar a checagem de `_windows_for_day` derruba 8) e na
  defesa de confirmação do "Alterar dados".
- Front (par): `npm test` 267 arquivos / 4171 testes, `npm run typecheck` e `npm run build` verdes (Integração 2).

## Prova no navegador (2026-11-19, stack local própria da TASK-087, Portal + agent-browser)

Screenshots em `../tasks/TASK-087/evidencias/` (raiz de BRAIN). Nenhum defeito encontrado.

- **/contexto:** serviço oferecido mostra chips só dos dias atendidos (seg–sex); desmarcar segunda em "Retorno",
  salvar e recarregar mantém desmarcado. Médica com 2 unidades: "Consulta" na Unidade Centro com sexta desmarcada →
  selo "dias próprios", preço próprio R$ 250 / 40 min intacto (confirmado no GET do hub). O último chip fica travado
  com a dica "Para não oferecer este serviço, desligue “Oferecer”."
- **Portal do paciente:** Retorno → lista de dias sem segunda (Sex 20/11, Ter 24/11 …); títulos "Horários livres em
  ter, 24/11:"; calendário "Outra data" com a segunda desligada; data digitada 23/11 recusada com "Retorno com Dra.
  Multi Teste não é feito às segundas. Posso ver outro dia?" (texto da época; desde a revisão P6: "… não está
  disponível às segundas …"). Consulta na Sede mostra segunda e sexta; Consulta na
  Unidade Centro não mostra sexta; agendamento de Consulta na segunda 23/11 10:00 confirmado.
- **Agenda da recepção:** marcou Retorno numa segunda (HTTP 201), sem recusa (D3).
- Não testado: WhatsApp real (Portal é o único ambiente liberado, `CHECKPOINT_ambiente_secretaria.md`), nem IA com
  provedor real de LLM (só testes automatizados).

## Pendências e riscos conhecidos

1. **R8 aceito** (ver acima): em falha de leitura, a disponibilidade da IA pode mostrar um dia bloqueado só por
   unidade (recusado ao marcar) ou esconder um dia que a unidade libera. Sem task nova.
2. **Mescla futura com a branch 083:** esta branch parte de `c7423f0`. Se a revisão da 085/086 (onda 5, na worktree
   TASK-083) criar migração nova, a `c8e2a4f6b1d3` ficará com o head errado e exigirá uma migração-ponte/`merge` de
   heads (padrão já usado na TASK-071 Plano 3). Conferir `alembic heads` antes de mesclar.
3. `flow_router._service_days_calendar` lê o atributo privado `CalendarService._blocked_weekdays` para saber se há um
   conjunto a limpar ao re-escopar; renomear lá exige ajustar aqui (o filtro em si continua).
4. `create_event` (médico único) agora lê o `owner` sempre — uma consulta a mais por agendamento da IA.
5. Clínica multi-médico sem serviço fixado: `create_event_at_unit` não tem `owner` e não checa (a ferramenta já é
   bloqueada nessa topologia).
6. Dias próprios por unidade continuam ligados à linha de exceção da 085: o médico que salva "Todas as unidades" no
   preço **não** perde os dias próprios (por assunto) — comportamento novo para quem já usava a 085.
7. **Remarcação acha o serviço pelo NOME guardado na consulta** (revisão P3, aceito): a consulta não guarda
   `service_id`; se o serviço for renomeado depois de marcado, a remarcação daquela consulta não acha a entrada e não
   filtra os dias (a recepção pode remarcar; nada quebra). Corrigir exige `service_id` na consulta — fora do escopo.
8. **Dias "para todas as unidades" não alcançam unidade DESATIVADA** (revisão D3, aceito): o Portal manda os dias já
   resolvidos por unidade ativa; uma unidade desativada guarda os dias próprios que tinha e volta com eles se for
   reativada (o preço em "Todas" alcança as desativadas). Raro; o médico revê os dias ao reativar.
9. A recusa na confirmação volta com `BACK_TARGET_SERVICE` mesmo quando a unidade veio de lista (revisão P8, aceito:
   copia o ramo do prazo mínimo que já existia).

## Revisão final (T11, 2026-11-19)

Revisão independente em 4 frentes (caminhos do paciente, IA + "Alterar dados", dados/migração/isolamento, tela) —
achados e decisão em `../tasks/TASK-087/REVIEW.md` (raiz de BRAIN). Corrigidos aqui: **P1** (retrato sem unidades em
lembrete/remarcar/legado), **P2** (R1 com unidades que abrem em dias diferentes: `blocked_weekdays_of` agora soma o dia
em que toda unidade que ABRE nele o bloqueia), **D1** (ausente = mantém), **A1** ("Alterar dados" explica a recusa:
`_day_check`, `_refused_day_picker`, `_time_entry`), **A2/A3** (R4: `_rechecks_day` — escolher de novo o mesmo
serviço/médico não força novo dia; o próprio médico da consulta não aparece "Ocupado"), **P4** (lista "suas
consultas" no WhatsApp: título só dia e hora, serviço na descrição), **P5** ("Livre seg, 24/11"), **P6** (texto da
recusa), **P7** (`flow_units.service_scoped_calendar` removido; `flow_router._service_scoped` é o único), **A4**, **A5**,
**A6**, **D2**, **D4** (`_nearest_price` ignora linhas só-de-dias). Aceitos sem mudança: P3, P8, D3 (acima).
Re-revisão das correções: **RR1** (pedido de remarcar da IA com o modo `ai_draft_v2` ligado também usa o retrato com
unidades — `sentinels._handle_manage_appointment`), **RR2** ("ausente = mantém" só casa por nome entrada SEM
`service_id`), **RR3** ("Alterar dados": escolher de novo o próprio médico/unidade checa a consulta como marcada, R4 —
`appointment_edit_flow._calendar(service_days=...)`). Aceito: **RR4** (o histórico que a IA lê da lista "suas
consultas" mostra só dia e hora; o serviço foi para a descrição da linha).

## Deploy (não autorizado)

Ordem: **migração `c8e2a4f6b1d3` → API E worker do secretarIA (dois serviços separados) → Brain-Message-Frontend
(Portal)**. Antes do front, o servidor já tolera chips ausentes (campo opcional, nada a migrar nas clínicas
existentes).

## Commits da branch (`c7423f0..HEAD`)

```
dd8be65 fix(service-days): re-review - AI v2 reschedule roster, D1 name fallback, R4 own doctor   (T11)
e7d272a fix(service-days): P6 refusal agrees with any service name and mixed weekday lists        (T11)
368cefe merge: TASK-087 T11 fixes (patient flows, edit flow) into dias-do-servico
0f0e55e fix(service-days): A1 explain blocked days in "Alterar dados", A2/A3 R4 ..., P4/P5 list labels (T11)
90ade47 fix(service-days): P1 unit-aware roster on button/AI entries, P2 R1 by opening units, P7 ...  (T11)
fd90b14 merge: TASK-087 T11 fixes (data, hub, AI) into dias-do-servico
86b6381 fix(service-days): D2 ..., D4 ..., A4 ..., A5/D5 ..., A6 docstrings                          (T11)
24d4777 fix(service-days): D1 keep stored base blocked_weekdays when a PUT omits the key; F1 tests   (T11)
2393c0f docs(service-days): checkpoint + pointer                                                     (T10)
d9f81c6 merge: TASK-087 T6 into dias-do-servico
8df12d1 merge: TASK-087 T5 into dias-do-servico
333e602 feat(service-days): button flow, reschedule and rebooking respect service days   (T4)
bbb4122 feat(service-days): AI tools and context respect service days                    (T6)
f596c04 feat(service-days): appointment edit flow respects service days                  (T5)
aaadb7e merge: TASK-087 T3 (calendar filter) into dias-do-servico
6fa51ab test(service-days): align 6 old tests with T7 weekday labels
a80495a feat(service-days): calendar filter for blocked weekdays + helpers               (T3)
e6c358c feat(service-days): hub writes and reads blocked weekdays (base + per unit)      (T2)
b0e5f2b merge: TASK-087 T7 (weekday labels) into dias-do-servico
53acd43 feat(service-days): blocked weekdays per professional x service x unit (data + resolver)  (T1)
1ad9870 feat(dates): weekday next to every patient-facing date list                      (T7)
```
