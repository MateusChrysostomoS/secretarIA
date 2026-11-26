# CHECKPOINT — Multi-unidades + agenda compartilhada por pessoa (TASK-083)

> **Superado em parte por TASK-090 (2026-11-22, local):** unidade deixou de ser add-on pago e de ter limite — saíram o `403 multi_unit_not_entitled`, o `409 unit_limit_reached` e o `503` de entitlement; o endereço da unidade agora é estruturado (CEP, rua, número, complemento, bairro, cidade, UF; `address` é derivado; `422 unit_address_invalid`) em vez do `unit_address_required`. Adendo 1 (2026-11-23): a condição passou a ser "clínica tem ≥1 unidade ativa com endereço completo" — liga as ferramentas de unidade e é exigida para a secretária responder; o paciente só vê unidades completas. Ver [CHECKPOINT_contexto_ajustes_finais.md](CHECKPOINT_contexto_ajustes_finais.md).

Estado (2026-11-13): **pronto e validado, só LOCAL. Commitado na branch `task/TASK-083-multi-unidades`
(worktree `BRAIN-worktrees/TASK-083/secretarIA`); NÃO foi para `main`, NÃO houve push, NÃO foi deployado.**
Deploy e push só a pedido do dono.
**Follow-up 2026-11-14 (serviço × médico × unidade, e-mail à clínica, botão "Sincronizar agenda" removido): ver a seção
"Serviço × médico × unidade" abaixo — também só LOCAL; migração nova `c6e1a9f4d2b7`.** Registro central: `../tasks/TASK-083/TASK.md` (raiz de BRAIN). Pares:
`brain-api` (1 rota interna, `docs/CHECKPOINT_agenda_multi_unidades.md` de lá) e `Brain-Message-Frontend`
(telas, `docs/CHECKPOINT_agenda_multi_unidades.md` de lá). Spec (autoridade):
`docs/superpowers/specs/2026-11-12-multi-unidades-agenda-compartilhada-design.md`; plano:
`docs/superpowers/plans/2026-11-12-multi-unidades-agenda.md`. Graphify **não** foi reconstruído na branch
(`graphify-out/` é versionado e grande — atualizar na branch só geraria conflito de merge): o grafo fica
STALE (referência histórica) até um `update` depois do merge.

**Junção final (2026-11-20, só LOCAL):** branch `feature/multi-unidades-integrada` (worktree
`BRAIN-worktrees/INTEGRACAO-MULTI-UNIDADES/secretarIA`) = esta branch (`c7423f0`, já com 085+086) + correções das
revisões finais 085 T8 / 086 T9 + TASK-087. Uma ponta de migração só (`c8e2a4f6b1d3`; nenhuma ponte nova foi
necessária). Decisão do dono na junção: na tela "Remarcar" depois do cancelamento pela clínica, os dias bloqueados
valem os da unidade onde a lista é lida (ver `CHECKPOINT_service_days.md`). Registro: `../tasks/INTEGRACAO-MULTI-UNIDADES/`.
Sem `main`, sem push, sem deploy.

**TASK-088 (2026-11-21, só LOCAL, branch `task/TASK-088`):** a ordem do Agendar passou a ser serviço → médico → **unidade** →
convênio → dia (o convênio vem DEPOIS da unidade e não estreita mais as unidades); o aviso "apenas na unidade X" ficou em negrito
(`*apenas na unidade X*`); o Sim/Não "Pelo convênio X ... apenas na unidade Y" foi removido. Ver
[CHECKPOINT_ordem_agendar_servico_primeiro.md](CHECKPOINT_ordem_agendar_servico_primeiro.md).

## Por quê

1. **Multi-unidades.** Médico que usa a secretarIA como assistente pessoal atende em mais de um lugar. Antes,
   `Unit` era só um endereço do tenant; nada ligava unidade a profissional, horário ou fluxo do paciente.
2. **Dupla reserva entre secretarIAs.** O mesmo médico pode ter a secretarIA pessoal (tenant P) e atender numa
   clínica que também usa (tenant C): são duas linhas `professionals` que nunca se enxergavam. Agora as duas
   linhas apontam para uma `Agenda` (a pessoa) e o ocupado de uma vale para a outra.

## O que entrou onde

### Modelos e migrações (aditivas; head final `b5d9e3f7a1c4`)

- `models/agenda.py::Agenda` — `person_key` UNIQUE (= `str(StaffAccount.id)` do brain-api).
- `models/professional_unit.py::ProfessionalUnit` — PK `(professional_id, unit_id)`; `business_hours` JSON NULL
  (NULL = herda), `is_active`, `sort_order`.
- Colunas novas: `professionals.agenda_id` (FK `ON DELETE SET NULL`, índice), `calendar_events.unit_id` e
  `booking_holds.unit_id` (sem FK: rótulo histórico), `conversations.flow_selected_unit_id` (FK `SET NULL`;
  no SQLite dos testes vai sem FK). `Appointment.unit_id` já existia. `CalendarEvent` ganhou o status
  `google_lease` (`EVENT_STATUS_GOOGLE_LEASE`, ver "Escrita no Google") — sem migração.
- Migrações (encadeadas, nessa ordem):
  - `e4a8c2f6b1d9_multi_unit_shared_agenda` — `agendas`, `professional_units`, `professionals.agenda_id`,
    `calendar_events.unit_id`, `booking_holds.unit_id`. Não mexe em dados.
  - `b5d9e3f7a1c4_conversation_flow_selected_unit` — `conversations.flow_selected_unit_id`. Existe porque o
    roteador do fluxo não guarda estado entre turnos: a unidade escolhida mora na conversa, como médico e convênio.

### Serviços

- `services/agenda.py` — o núcleo do ocupado compartilhado:
  - `BusyWindow(start, end)` (só dois campos, de propósito), `shared_busy`, `sibling_professional_ids`,
    `agenda_members`, `owned_events`, `professionals_with_siblings`, `shared_busy_provider`;
  - `agenda_lock_key`/`agenda_write_lock` — chave `agenda:<id>` (advisory lock no Postgres; no-op no SQLite);
  - `GOOGLE_WRITE_LEASE` (2 min).
  O "ocupado" de um irmão = agendamentos vivos, `calendar_events` locais ocupados (não repassados ao Google),
  holds na fase `payment` não expirados e leases Google frescos. Só janelas; nunca resumo, paciente, serviço,
  nome de clínica ou tenant (nem em log).
- `services/agenda_linking.py` — liga profissionais à `Agenda`: `ensure_agenda_for_professional`
  (idempotente; lock por conta; move quem estava em outra agenda), `sync_tenant_agendas` (1 chamada ao brain-api
  por tenant), `try_link_professional` (gatilho de criação/attach; engole qualquer erro) e
  `reconcile_all_tenants` (usado pelo script).
- `services/brain_professionals.py::fetch_professional_accounts` — cliente da rota nova do brain-api; devolve
  `None` em qualquer falha (não configurado, timeout, 5xx, 404/rota antiga, corpo inválido).
- `services/professional_units.py` — vínculos, horário por unidade (`unit_hours`), `available_unit_ids`,
  `replace_links`, `count_professionals_by_unit`.
- `services/unit_booking.py` — `UnitOption`, `unit_options_for`/`attach_unit_options` (uma leitura que alimenta o
  snapshot do roteador), `scoped_calendar`, `check_unit` (`UnitRefused`: `unit_not_found` 404,
  `unit_inactive` 422, `unit_not_linked` 422), `unit_location`/`unit_line` (a única grafia de "onde").
- `services/flow_units.py` — helpers puros do passo de unidade (`STEP_AWAITING_UNIT`, `unit_choice_needed`,
  `effective_unit`, `union_hours`, `appointment_calendar`, `unit_after_doctor_change`, `match_unit`).
- `ai/unit_scope.py` — o mesmo critério para as ferramentas da IA (`resolve_tool_unit`, `outside_unit_hours`).

### Calendário (leitura e escrita)

- `services/calendar.py::CalendarService`: `shared_busy_provider` opcional (injetado por `from_tenant_config`/
  `tenant_config.resolve_professional_calendar` só quando o profissional tem agenda COM irmão —
  `TenantRuntimeConfig.shared_agenda_professional_ids`), consumido em disponibilidade, `has_conflict`,
  `is_slot_free`, listas de horários, `slot_taken_at_confirm`, IA, promoção de hold e Pix.
  `with_unit(...)` (cópia escopada à unidade: horário da unidade, `unit_id` no evento local, `location` no
  Google), `own_agenda_view()` (visão só da própria agenda, para o hub), `overlaps_shared_busy` e
  `services/calendar.py::overlaps_other_agenda` (só sim/não).
- `supports_exclusive_write` substitui os `isinstance(calendar, LocalCalendarService)` que guardavam o caminho
  exclusivo (`flow_router`, `ai/tools`, `workers/shared/booking_hold`, `booking_promotion`, `release_keep`);
  helpers `local_calendar.create_event_exclusive`/`update_event_exclusive`/`update_event_details_exclusive`.
- `services/local_calendar.py` — agenda nativa: `agenda_write_lock(tenant, professional, shared=True)` pega
  também o lock da pessoa; `_overlap_exists` inclui o ocupado dos irmãos.
- `release_keep._consultation_overlaps` e `payments/service_change` passam a contar o ocupado dos irmãos.

#### Escrita no Google (`CalendarService.exclusive_write`) e ordem de locks

Só para quem compartilha agenda (os demais Google ficam byte a byte como antes). **Reserva primeiro, escreve
fora do lock** — nenhuma chamada de rede roda com transação ou lock aberto:

1. monta o cliente Google (refresh do token incluso) com timeout explícito
   (`Settings.GOOGLE_CALENDAR_EXCLUSIVE_TIMEOUT_SECONDS`, 20 s), fora de tudo;
2. `local_calendar.reserve_person_window`: transação curta sob o lock da pessoa (`person_agenda_write_lock`),
   varre leases vencidos, recusa se há irmão ocupado OU lease fresco (inclusive do próprio profissional),
   insere o **lease** — linha `calendar_events` com status `google_lease` (sem resumo/paciente) — e commita;
3. fora do lock: relê o calendário Google (`_own_overlap`) e roda o `write`;
4. tomado no Google ou falhou: `release_person_window` (melhor esforço; o vencimento de 2 min é o backstop);
   sucesso: o lease fica até vencer e `stamp_person_window` grava o id Google nele.

**Ordem de locks (regra de ouro):** escrita local exclusiva = lock do dono (`calendar_events:<tenant>:<prof>`)
**depois** o da pessoa (`agenda:<id>`), a mesma transação; o lock em-processo da pessoa só é solto depois do
commit. A reserva do Google pega **só** o da pessoa. Nunca chame escrita local exclusiva segurando
`person_agenda_write_lock` (pega dono e depois pessoa; o lock em-processo não é reentrante).

### Hub (contratos congelados)

- `api/hub/professional_units.py`:
  - `GET /tenants/me/professionals/{pid}/units` → `{"units":[{unit_id,name,address,is_active,linked,business_hours}]}`
    (todas as unidades ATIVAS do tenant; legível por qualquer membro da equipe — a secretária precisa ao marcar);
  - `PUT` (mesmo corpo/resposta; gestor ou o próprio médico via `deps.ensure_clinic_manager_or_own_professional`):
    corpo `{"links":[{unit_id,business_hours}]}` SUBSTITUI o conjunto de vínculos de unidades ATIVAS (vazio = sem
    vínculos); vínculos e horários de unidades desativadas ficam intactos e o `unit_id` de unidade desativada no
    corpo é ignorado; outra clínica/desconhecida = 404; `403 multi_unit_not_entitled` só quando o resultado tem
    mais de 1 unidade; `409 unit_limit_reached`; falha ao buscar entitlement = 503 (fecha); salvar duas vezes ao
    mesmo tempo = lock na linha do profissional e `409 professional_units_changed`, nunca 500.
- `api/hub/agendas.py`: `POST /tenants/me/agendas/sync` (gestor; corpo VAZIO — qualquer campo é 422, o hub nunca
  aceita `agenda_id`/`person_key`) → `{linked, shared}`; `GET /tenants/me/professionals/{pid}/agenda` →
  `{shared, other_tenants_count}` (só contagem, nunca nome de clínica).
- `api/hub/units.py`: `GET /tenants/me/units` ganha `professionals_count` (profissionais ativos com vínculo ativo
  àquela unidade; conta por unidade, independentemente de a unidade estar ativa).
- Agenda do hub (`/tenants/me/calendar`): cada item de `GET /events` ganha `unit_id`/`unit_name`/`unit_address`
  (null sem unidade; a agenda nativa não tem coluna `location` — o nome/endereço são resolvidos na leitura pelo
  `unit_id`); `POST /appointments` aceita `unit_id` opcional (`_creation_unit` → `check_unit`; ausente é sempre
  aceito — encaixe da equipe continua permissivo); `POST /appointments`, `/appointments/{id}/reschedule` e
  `/appointments/{id}/edit` respondem `conflicts_other_agenda: bool` (aviso, **não bloqueia**; usa
  `own_agenda_view()` para a checagem própria continuar bloqueando como antes).
- Reconciliação: `workers/agenda_cron.py::run_agenda_reconcile` (cron arq, ver abaixo) e
  `scripts/reconcile_agendas.py [--dry-run]`.

### Fluxo do paciente e IA

- Passo `awaiting_unit` (`flow_units.STEP_AWAITING_UNIT`, dentro do estado de catálogo de serviços; roteamento em
  `flow_router._enter_unit_list`/`_handle_unit_step`; linhas `unit|<uuid>`): com 2+ unidades reserváveis o paciente
  escolhe a unidade (nome + endereço curto, limites de texto do WhatsApp, fixo "Outra unidade"/"Outro serviço"); com
  1, pula e usa a unidade; sem vínculo, fluxo idêntico ao de antes. Dias e horários usam o horário da unidade
  escolhida ∩ ocupado da agenda inteira.
- **Onde `conversations.flow_selected_unit_id` é gravada e limpa:** gravada por `workers/shared/flow_runner.py`
  a partir do resultado do roteador; `flow_router._carry_unit` só a mantém nos passos depois da escolha (dia,
  horário, confirmação) para o MESMO profissional, e a derruba na lista de unidade, nas listas de serviço/médico e
  ao fim da reserva. Também é zerada em `workers/shared/state_expiry.py` (3 pontos de reset),
  `workers/shared/debug_reset.py`, `services/deposit_wait.py` (reserva terminada) e `ai/tools.py` (reserva feita pela
  IA). Hold e espera de sinal reconstruídos preservam a unidade (`BookingHold.unit_id`).
- `unit_id` gravado em `Appointment`/`CalendarEvent`/`BookingHold`; `location` (nome + endereço) em evento Google,
  no link "Adicionar à sua agenda" do paciente (`build_patient_calendar_link(location=...)`), na confirmação, nos
  e-mails (`plugins/booking_notifications.booking_unit_location`) e no lembrete em texto livre
  (`services/reminder_text.py`, linha "Local: ..."). `workers/shared/draft_resolution` deixou de zerar o
  endereço quando há unidade.
- **Descrição do evento e "Detalhes" do link do paciente (follow-up, dono 2026-11-13):** com unidade, a descrição
  do evento da clínica ganha `Profissional:`/`Unidade:`/`Endereço:` (`unit_booking.event_unit_fields`; sem unidade
  fica idêntica), inclusive após "Alterar Dados", edição pelo hub, troca de serviço e Pix pago (unidade ATUAL da
  consulta, `unit_booking.booking_unit_fields`; falha de leitura não bloqueia a agenda). O link do paciente agora
  leva `details` (`calendar.build_patient_link_details`: serviço, profissional, data e horário, unidade, endereço,
  convênio) nas confirmações do chat, do Pix e nos e-mails; a mensagem final mostra `Unidade:` e `Endereço:` em
  linhas próprias (`unit_booking.confirmation_unit_lines`).
- "Alterar Dados"/Remarcar (`services/appointment_edit_flow.py`) seguem a unidade da consulta; troca de médico
  mantém a unidade só se o novo médico a atende, senão a única unidade dele, senão limpa. Remarcar consulta sem
  unidade de médico que agora tem unidades usa a união dos horários das unidades (sem carimbar unidade).
  `staff_appointment_edit` limpa `unit_id` na troca de médico (a menos que o novo atenda a unidade).
- IA: `plugins/multi_unit.py` (`list_units`/`create_event_at_unit`) e `plugins/multi_professional.py` respeitam
  `professional_units`; `create_event` base falha FECHADO quando a unidade do médico único não pôde ser lida
  (nunca reserva sem unidade); disponibilidade e criação usam o horário da unidade.
- **Cron:** `workers/agenda_cron.py` (registrado em `workers/arq_worker.py`), a cada
  `AGENDA_RECONCILE_INTERVAL_MINUTES` (padrão 15), no máximo `AGENDA_RECONCILE_MAX_TENANTS_PER_RUN` tenants
  (padrão 25) por tick. Existe porque o brain-api só amarra `users.professional_id` DEPOIS do attach, então o gatilho
  de attach (`try_link_professional`, em `api/internal_provisioning.py`) quase sempre acha nada num convite real.

## Regras-chave

- `professionals.agenda_id` NULL = comportamento antigo, byte a byte: todo caminho novo vira no-op (e nem faz
  consulta extra).
- Horário por unidade: `business_hours` NULL **herda** o horário resolvido do profissional; `{}` é horário próprio
  **fechado** a semana toda e não herda nada (use `is None`, nunca truthiness; nunca `IS NULL` em SQL nesta coluna —
  JSON `null` vs SQL NULL). Unidade só com horário fechado não é reservável.
- O ocupado é sempre da agenda inteira (a pessoa está num lugar por vez); a unidade só muda o **horário oferecido**.
- Identidade só vem do brain-api (`account_id`); nunca por e-mail digitado ou nome; o hub nunca aceita `agenda_id`.
- **Liga:** só quando o brain-api respondeu e lista o profissional. **Desliga** (`agenda_id = NULL`): quando o
  brain-api RESPONDEU (200, lista vazia inclusive) e um profissional ligado deste tenant está ausente. Falha do
  brain-api (`None`: timeout, 5xx, 404/rota antiga, corpo ruim) nunca liga nem desliga nada.
- Convite pendente, membro revogado ou conta desativada **nunca** são ligados: a rota do brain-api só devolve
  membros que a pessoa aceitou e ainda tem (`membership_pending = false`, `access_revoked_at IS NULL`, conta não
  desativada). Sem isso, qualquer clínica poderia entrar na agenda de outro médico só digitando o e-mail dele.
- Erro de banco no ocupado compartilhado propaga (mesmo banco); só a chamada ao brain-api é tolerante.

## ORDEM DE DEPLOY (informativa; esta tarefa não deploya)

1. **Migrações do secretarIA primeiro** — `alembic upgrade head` da imagem NOVA, como one-off, com os dois serviços
   ainda no código antigo (as duas, `e4a8c2f6b1d9` e `b5d9e3f7a1c4`, são inertes para o código antigo; o ORM novo
   nomeia as colunas em toda leitura de `conversations`/`professionals`, então API+worker novos sem elas falham
   todo turno). Recomendado: `SET lock_timeout` curto na sessão da migração e conferir antes se há conexão
   `idle in transaction` (uma assim trava o `ALTER`; reiniciar não resolve — ver memória
   `secretaria-postgres-idle-in-transaction-leak`).
2. **brain-api** (rota `GET /internal/tenants/{id}/professional-accounts`) ANTES de ligar agendas — a ligação tolera
   a ausência (404 = nada muda), mas sem a rota nada é ligado.
3. **secretaria_api E secretaria-worker juntos** (`GET /build` deve dar paridade `match`; o worker carrega o cron e
   os fluxos).
4. **Brain-Message-Frontend** por último.
5. Rodar `uv run python scripts/reconcile_agendas.py --dry-run` e depois sem `--dry-run`, **uma vez** (o cron
   também alcança tudo sozinho, a cada 15 min, mas o script dá o resultado na hora).

Rollback: código antigo nos dois serviços, depois `alembic downgrade` (primeiro `b5d9e3f7a1c4`, depois
`e4a8c2f6b1d9`). Perde-se: vínculos de agenda, horários por unidade, `unit_id` nos eventos/holds e a unidade de uma
reserva em andamento (o paciente é perguntado de novo). Consultas/eventos/holds em si ficam. **O downgrade deixa para
trás as linhas `calendar_events` de status `google_lease`** (são linhas normais da tabela; expiram sozinhas, mas o
código antigo não as conhece — limpe `status = 'google_lease'` se ficar incômodo).

## Como reconciliar / operar

- Hub: `POST /tenants/me/agendas/sync` (gestor) continua no servidor, mas **o botão "Sincronizar agenda" saiu do Portal**
  (2026-11-14, ver "Serviço × médico × unidade"): uso só de suporte/ops (chamada direta); no dia a dia a ligação é
  automática (vínculo + cron de 15 min).
- Script: `uv run python scripts/reconcile_agendas.py [--dry-run]` — todos os tenants com profissionais, idempotente,
  um tenant com falha é registrado e pulado; imprime só contagens e ids de tenant (nenhum nome/conta/paciente).
- Cron: ver acima; log `agenda_reconcile_cron_tick` (contagens) e `agenda_sync_done` por tenant (`linked`, `shared`,
  `changed`, `brain_ok`).
- Agenda não liga? Confira, nesta ordem: a rota do brain-api existe (deploy), o profissional tem usuário vinculado e
  o membro ACEITOU o convite (pendente/revogado não liga), `BRAIN_API_BASE_URL` e `INTERNAL_API_KEY` configurados no
  secretarIA (sem eles o cliente devolve `None` e nada muda), e o log
  `agenda_sync_done ... brain_ok=False`.
- Prova de concorrência em Postgres real: `tests/postgres/README.md` (container descartável, `T083_PG_URL`; o teste
  recusa rodar fora de loopback/banco `t083*`).

## LIMITAÇÕES e pendências deferidas

Decisões do dono / pendências de produto:

- **Template Meta de lembrete fora da janela de 24h não traz a unidade.** O lembrete em texto livre (dentro da
  janela) mostra "Local: ...", mas o template aprovado não tem variável de unidade: precisa de um template novo
  aprovado pela Meta — **decisão do dono**, não feito.
- `POST`/`PATCH /tenants/me/units` não tem gate de papel no backend (usa `get_current_tenant`, como antes desta
  tarefa); o frontend só esconde a edição da secretária. **Decisão do dono**: gestor-apenas trancaria o médico-dono
  de um tenant de assistente pessoal (o caso de uso central). Quem tem acesso à equipe pode criar/desativar unidade.

Limites conhecidos (documentados, não bloqueiam):

- **Google:** eventos pessoais soltos do Google Calendar de OUTRO tenant não são lidos (exigiria usar o token de
  outro tenant). O outro tenant enxerga as consultas (`appointments`), bloqueios locais e leases do irmão. Para
  cobertura total, conectar o mesmo calendário nos dois (`references_same_calendar`) ou usar a agenda nativa.
- **Corrida de `place_hold` entre clínicas:** a janela de milissegundos entre dois holds na fase de pagamento de
  clínicas diferentes pode aceitar os dois; o desfecho é Pix pago e depois reembolso (risco aceito pela spec).
- **Lease liberada em desfecho desconhecido:** `exclusive_write` solta o lease em qualquer `BaseException`
  (inclusive timeout/cancelamento, em que o evento pode ter sido criado no Google). Pior caso: evento Google órfão
  na agenda do próprio médico; nunca dois confirmados. Não declarado em `pyproject`: `httplib2`/`google_auth_httplib2`
  são usados diretamente.
- **IA — reserva best-effort:** `ai/tools._persist_appointment` pode deixar evento Google órfão se a gravação falhar
  depois; e o caminho de reserva da IA (`booking_draft.effective_unit`) ainda cai em silêncio para a unidade que
  sobrou quando a escolhida some (no fluxo determinístico isso reabre a lista de dias com aviso — M1 corrigido só lá).
  Só `create_event` é fail-closed; a unidade-consciência das outras ferramentas de escrita da IA não foi verificada.
  Médico INATIVO alcançado pela `calendar_factory` da IA recebe a escrita Google simples (sem exclusividade).
- **Troca de médico em calendário compartilhado** não re-carimba o `unit_id` do evento local (a linha do
  agendamento é atualizada e o hub lê a consulta primeiro).
- **Cobertura de aviso no hub:** `POST /blocks` e um caminho de edição da equipe não trazem `conflicts_other_agenda`.
  O hub não mostra o ocupado de irmãos na listagem da agenda (aceito).
- "Alterar Dados" do paciente e a troca de médico pelo hub respeitam a unidade (ver acima), mas o hub NÃO tem campo
  para trocar a unidade de uma consulta já existente (`AppointmentEdit`/`AppointmentReschedule` não têm `unit_id`);
  e edição/remarcação pela equipe não revalidam a unidade da consulta contra o horário daquela unidade.
- **Outros leitores do horário próprio do médico ignoram o horário por unidade** (só relatório/mensagens):
  `tenant_config` (completude), `workers/shared/greeting.py`, `flow_simulation_input`, `context_completeness`,
  `release_offer`. Também `booking_draft` passa `BACK_TARGET_SERVICE` ao seletor de horários (linha "voltar"
  inconsistente quando há unidade).
- **Cron:** M4 não feito — linhas de tenants já ligados são re-consultadas a cada tick em que sorteadas (precisaria
  de coluna "última checagem" + migração). Com `AGENDA_RECONCILE_MAX_TENANTS_PER_RUN = 1` os tenants à espera de
  vínculo ficam sempre com a única vaga e os já ligados podem não ser re-checados (padrão 25: sem problema).
- **Linha malformada:** o cliente do brain-api descarta linhas malformadas; se uma lista não vazia chega sem
  nenhuma linha válida, os profissionais do tenant são desligados até a próxima sincronização (endurecer depois:
  lista não vazia com zero linhas válidas → `None`). O filtro da rota difere do de `active_staff_roles`
  (`requires_password_change`/`invite_token_hash`) — não é vetor entre clínicas. Desligar roda sem o lock da pessoa
  (o próximo tick corrige). `shared` em `_tenant_counts` e no GET da agenda diferem para irmãos do mesmo tenant.
  `sync_tenant_agendas` ainda faz uma chamada síncrona ao brain-api no attach (latência no convite).
- **Perfil de lock:** a conexão do pool fica presa enquanto espera o lock em-processo da agenda; leases do próprio
  médico bloqueiam outra reserva dele por até 2 min (lado seguro); segundo cliente Google por escrita exclusiva.
- Tempo de deslocamento entre unidades: fora (futuro `Agenda.travel_buffer_minutes`). Billing/Stripe: `multi_unit`
  e `limits["units"]` ficam como estão.

Falhas **pré-existentes** (não são regressão desta tarefa): `tests/test_service_change_agenda.py` ×2
(`test_a_paid_difference_that_also_moved_the_time_replans_and_tells_once`,
`test_a_clinic_initiated_paid_move_replans_but_sends_no_patient_change_notice`) — falham igual no `main` base
(`ea505ca`) e rodando o arquivo sozinho; `tests/test_ai_toolset_v2.py`/`test_get_availability_tool` falham só quando
rodam depois de suítes de plugin no mesmo processo (`ANTHROPIC_API_KEY` ausente), passam sozinhos; `make lint`
vermelho no HEAD (não rodar `ruff format .`); `alembic check` acusa drift em tabelas antigas (`booking_holds`,
`processed_asaas_events`, `professional_insurance_plans`, `tenant_credentials`).

## Serviço × médico × unidade (follow-up 2026-11-14)

Estado: **LOCAL** (commits na branch `task/TASK-083-multi-unidades`; fora de `main`, sem push, sem deploy). Spec:
`docs/superpowers/specs/2026-11-14-servico-por-unidade-design.md` (as "decisões minhas" 1–14 da §9 são requisitos);
plano: `docs/superpowers/plans/2026-11-14-servico-por-unidade.md`. Registro e prova:
`../tasks/TASK-083/results/servico-por-unidade.md` (capturas em `../tasks/TASK-083/evidencias/servico-por-unidade/`).

**O que mudou para o paciente.** Depois de escolher serviço e médico, o paciente só vê as unidades onde **aquela
combinação médico + serviço** é feita (WhatsApp e Portal usam o mesmo roteador; as ferramentas da IA e todo caminho de
escrita iniciado pelo paciente respeitam a regra):

- 2+ unidades permitidas: a lista de hoje, só com as permitidas.
- Médico que atende em 2+ unidades e o filtro deixa **1**: passo de confirmação com os botões fixos `✅ Sim` / `❌ Não`
  e o texto exato "O serviço {serviço} feito por {médico} é feito *apenas na unidade {unidade}*, em {endereço}. Você
  confirma essa unidade?" (desde a TASK-088 o trecho "apenas na unidade {unidade}" vai em negrito do WhatsApp, entre
  asteriscos). Sim → dias daquela unidade (`flow_selected_unit_id`); Não → lista de médicos **que fazem o
  serviço** (sem o médico recusado; clínica de um médico só, ou só um faz o serviço → lista de serviços).
- Médico com 1 unidade só, ou sem nenhuma marcação: fluxo de antes, byte a byte (sem pergunta).
- 0 unidades válidas (marcada(s) fechada(s)/desativada(s)): o serviço **não** some da lista (a clínica precisa ver o
  problema); o paciente recebe "Não consegui seguir com {serviço} com {médico} agora. Avisei a clínica." e volta à lista
  de médicos/serviços; a clínica recebe e-mail (abaixo).

**Dados.** Tabela `professional_service_units(professional_id, service_id, unit_id, created_at; PK das três; FK CASCADE)`,
migração **`c6e1a9f4d2b7`** (aditiva; `down_revision = b5d9e3f7a1c4`). Regra: sem linhas para (profissional, serviço) =
vale em todas as unidades do médico; com linhas = só nelas (∩ unidades reserváveis); "nenhuma" nunca é gravado; entrada
de serviço sem `service_id` = sem restrição e não aparece para marcar. Domínio em `services/professional_service_units.py`
(`read_marks_for`, `allowed_unit_ids`, `apply_marks`); escolha de unidade em `services/flow_units.py`
(`unit_choice`/`UnitChoice`, `units_for_service`); passo Sim/Não em `services/flow_router.py`.

**Hub (mesmas permissões de `professional_units`).** `GET/PUT /tenants/me/professionals/{pid}/units` agora leva
`services` (catálogo ativo com `service_id`) e `service_marks` (só os serviços restritos). O `PUT` é **uma chamada
atômica**: `links` + horários + `service_marks` (o estado desejado completo) numa transação; `service_marks: null` =
só vínculos (marcas intactas). Erros: **422** `service_without_unit` (desvincular a única unidade de um serviço
restrito; olha os VÍNCULOS, não o horário), 422 `unit_not_linked` (unidade da mesma clínica não vinculada ao médico),
**404** para unidade de outra clínica (igual a inexistente), 409 em `PUT`s simultâneos do mesmo profissional.
O encaixe da equipe no hub **só avisa**, não bloqueia; remarcar mantém a unidade da consulta.

**E-mail à clínica (0 unidades).** `services/no_unit_notice.py` + `services/email.py::send_no_unit_alert`: destino
`Tenant.contact_email`; link `{BRAIN_MESSAGE_PUBLIC_URL sem barra final}/contexto/?prof=<professional_id>`; no máximo
**1 por dia** por (clínica, médico, serviço) (contador Redis `no_unit_mail:...`, dia local da clínica, TTL 48 h,
compartilha `within_daily_cap` com o aviso de falha do sinal); sem dado de paciente, canal ou telefone. Sem e-mail
configurado, Redis fora ou SMTP falhando ⇒ só log sem PII (e o dia é liberado em falha de envio); o fluxo do paciente
nunca quebra. Precisa de `SMTP_HOST` etc. na **API e no worker** (o efeito roda no worker).

**Portal (`Brain-Message-Frontend`, `/contexto`).** Seção "Locais de atendimento" → **"Unidades"** (botão recolhível
**"Unidades ▾"**, recolhido por padrão, resumo "N unidades"); dentro de cada unidade ligada, sempre visível: horário
(geral/próprio) e **"Serviços feitos aqui"** (uma caixa por serviço ativo com `service_id`; todas marcadas = nada
gravado); não deixa desmarcar tudo (dica: desligue o serviço em "Serviços oferecidos"); salvar = **"Salvar unidades"**.
**Saiu o botão "Sincronizar agenda"** (e `onSync`/`api.sync`/estado/textos/testes); ficou o selo "Agenda
compartilhada". A rota `POST /tenants/me/agendas/sync` do servidor **permanece** (suporte/ops, sem UI) — quem liga as
agendas é o vínculo do profissional (`try_link_professional`) + o cron de 15 min do **worker**; por isso o worker é
obrigatório no deploy.

**ORDEM DE DEPLOY deste follow-up** (informativa; não deployado): **(1)** migração `c6e1a9f4d2b7` (`alembic upgrade head`
da imagem nova, com os serviços ainda no código antigo; é inerte para o código antigo) → **(2)** secretarIA **API E
worker** juntos (`GET /build` com paridade `match`; o worker roda o fluxo e envia o e-mail) → **(3)** Brain-Message-Frontend.
Nenhuma mudança em brain-api. Rollback: código antigo nos dois serviços, depois `alembic downgrade -1` (perde só as
marcas de serviço × unidade; o paciente volta a ver todas as unidades do médico).

**"Alterar Dados" (correção da revisão final, 2026-11-14).** A edição da consulta pelo paciente
(`services/appointment_edit_flow.py`) também respeita a regra — antes dava para trocar o serviço/médico e cair numa
unidade onde o serviço não é feito:
- Trocar o **médico**: a unidade segue o novo médico **para o serviço da consulta**
  (`flow_units.unit_after_doctor_change(..., service_id)`, filtrada como `units_for_service`); se o novo médico não faz o
  serviço em nenhuma unidade aberta, a troca é recusada com aviso simples (`EDIT_DOCTOR_SERVICE_NO_UNIT`) e o médico
  continua o mesmo.
- Trocar o **serviço**: a edição não tem passo de escolher unidade, então se o serviço novo não é feito na unidade da
  consulta a troca é **recusada** com aviso simples (`EDIT_SERVICE_NOT_AT_UNIT`) e o serviço antigo fica (variante
  "recusar com aviso"; oferecer as unidades válidas exigiria um passo novo — decisão do dono se quiser). Depois de uma
  troca de médico em que o médico não fazia o serviço, a unidade é recalculada para o serviço escolhido; sem unidade
  válida, a troca de médico é desfeita com o aviso.
- A proposta da IA dentro da edição (`apply_ai_edit`) segue a mesma regra.
- Remarcar (mesmo médico, mesmo serviço, mesma unidade) **nunca** é recusado, mesmo que o serviço tenha deixado de ser
  feito ali (§9.4) — `_is_original_booking`. Sem marcações/sem vínculos = comportamento de antes.
- Peça comum: `flow_units.unit_refused`.

**Também corrigido na revisão final:** caminho de rascunho da IA (`booking_draft._carry`) no caso 0 unidades não
mantém mais o médico recusado selecionado na lista de médicos (teste do caminho da IA); o `GET` do hub só devolve
`service_marks` dos serviços que o médico ainda oferece (o próximo salvamento apaga a marca velha).

**Antes do merge com Mercado Pago:** a branch `task/TASK-074-mp-provider` (`feature/mercado-pago-connection`) tem head
de migração `c4e8a1d6b3f9`, que também descende de `a7c3e9d1f5b2` (esta branch: `a7c3e9d1f5b2` → … → `b5d9e3f7a1c4` →
`c6e1a9f4d2b7`). Juntar as duas dá **duas heads**: será preciso uma revisão Alembic de merge (`alembic merge`) antes
do `upgrade head`.

**Pendências deferidas (Minor, nenhuma bloqueia):**
- **Aviso do hub dormente (follow-up aberto, decisão do dono):** o encaixe da equipe avisa
  (`unit_booking.check_unit` com recusa `unit_service_not_offered`) só quando recebe `service_id`, e **nenhuma tela de
  agenda do Portal envia** esse campo (`AppointmentCreate.service_id` dormente; o caminho de edição da equipe não avisa).
  Fica sem efeito até o Portal mandar o `service_id`.
- **Texto da confirmação sem endereço** (`flow_units.UNIT_CONFIRM_BODY_NO_ADDRESS`: a frase termina no nome da unidade
  quando ela não tem endereço) **aguarda o OK do dono**.
- Caminho de rascunho da IA (`booking_draft`): ao cair na lista de unidades / Sim-Não perde o dia/horário já
  capturado.
- `get_availability`/`list_free_slots` da IA não são limitados às unidades válidas (a escrita é bloqueada por
  `check_unit`); `create_event` sem guarda na leitura de `sole_professional_row`.
- `reoffer`/`_booking_back_target` derivam a lista de serviços de forma ligeiramente diferente de `_catalog_step`.
- E-mail à clínica parafraseia o motivo da spec §6.3; falha de SMTP prolongada custa até ~15 s por turno; import
  privado `_tenant_tz` de `deposit_lifecycle`.
- Front: dica do último serviço sem `aria-describedby`/`aria-controls` no recolhível; "Locais de atendimento" ainda em
  comentários de 5 arquivos fora de escopo.

## Follow-up do dono (2026-11-14)

Estado: **LOCAL** (branch `task/TASK-083-multi-unidades`, sem push/deploy). Plano:
`docs/superpowers/plans/2026-11-14-servico-por-unidade-followup.md`.

- **Endereço obrigatório.** Criar unidade sem endereço (vazio após `strip`) ou apagar o endereço de uma existente ⇒
  HTTP 422 `{"code": "unit_address_required", "message": "Informe o endereço da unidade."}` (`api/hub/units.py`).
  PATCH que não envia `address` continua valendo para unidade antiga sem endereço; no /contexto essas unidades aparecem
  sinalizadas ("Falta o endereço…") até alguém preencher. O fluxo do paciente mantém o texto sem endereço só como defesa.
- **"Alterar dados" com unidade** (`services/appointment_edit_flow.py`). (a) Serviço novo que não é feito na unidade
  da consulta ⇒ lista das unidades do médico onde ele é feito (`STEP_EDIT_UNIT`, mesmo com uma só; nenhuma ⇒ aviso e
  mantém o serviço). (b) Médico novo que não faz o serviço atual na unidade da consulta ⇒ os serviços dele **nessa**
  unidade, com a linha extra "Alterar unidade" (`edit|change_unit`) só quando ele faz o serviço atual em outra unidade;
  ela leva às unidades dele e depois aos serviços na unidade escolhida; médico que não atende ali ⇒ direto às unidades
  dele. Nada é gravado no rascunho até a escolha terminar (`stage.pending_doctor` / `pending_service` / `at_unit`);
  `_placement_problem` recusa no cartão uma combinação meio escolhida, um serviço fora da unidade ou uma unidade onde o
  médico não atende. O resumo mostra a unidade e o endereço dela; "unidade" entra em `changed()`. Unidade da lista que
  fechou entre um toque e outro ⇒ aviso e as unidades do médico de novo; sem unidade nenhuma ⇒
  `EDIT_DOCTOR_SERVICE_NO_UNIT`.
- **IA com as combinações.** O estado da conversa (`services/llm_context.py::build_conversation_state`) lista, por
  médico, as unidades reserváveis e os serviços feitos em cada uma (só em clínica com unidades), a unidade atual da
  consulta, o que está pendente na alteração e a unidade já escolhida numa marcação em andamento.
  `propose_appointment_edit` aceita `unit`; `apply_ai_edit` usa a unidade escolhida na lista aberta e leva proposta
  inválida aos mesmos passos (a)/(b). `list_units(professional_name=...)` dá só as unidades do médico (nenhuma ⇒
  lista vazia com nota, nunca todas as da clínica).
- **Sinal pago carrega a unidade.** `services/payments/service_change.py::serialize_edit` leva `unit_id` (e a unidade
  da linha em `expected`); `apply_paid_adjustment` grava a unidade nova só se a consulta ainda está na unidade do
  pedido (unidade trocada pela equipe nesse meio-tempo é mantida). Sinal pago no meio da edição volta médico, serviço,
  convênio **e unidade** aos originais.
- **E-mail ao médico** (`workers/shared/appointment_edit_notification.py`) lista "unidade" entre as mudanças.
- **Deploy:** o **worker sai junto com (ou antes de) a API** — o e-mail ao médico agora aceita o campo "unidade", e um
  worker antigo descarta (não envia) o e-mail de uma alteração que tenha esse campo. Sem migração nova neste follow-up.

## Testes

Arquivos novos (SQLite em memória, `BOT_ALLOWLIST_WA_IDS=""`): `test_agenda_characterization` (fixa o
comportamento sem agenda), `test_agenda_models`, `test_migration_multi_unit_agenda`, `test_flow_selected_unit_column`,
`test_agenda_service`, `test_agenda_linking`, `test_agenda_cron`, `test_brain_professional_accounts_client`,
`test_hub_agendas`, `test_agenda_shared_calendar`, `test_agenda_exclusive_google` (+ `_google_fake.py`),
`test_agenda_write_paths`, `test_agenda_hub_warning`, `test_professional_units`, `test_hub_calendar_units`,
`test_unit_booking*` (6 arquivos), `test_flow_router_units`, `test_appointment_edit_units`; mais `tests/postgres/`
(opt-in).

- **Suíte completa em lotes (HEAD `3b3a77e`, antes da onda final; `../tasks/TASK-083/results/suites.md`):**
  secretarIA **8016 passed, 98 skipped, 1 xfailed, 2 failed** (as 2 pré-existentes acima), 0 regressões; brain-api
  1664 passed, 0 failed; frontend 235 arquivos / 3493 testes, tsc e build (22 páginas) verdes.
- **Onda final (secretarIA HEAD `11bfcc9`, brain-api `88c47c8`, frontend `825c56b`):** subconjunto de agenda/unidades/
  fluxo/edição/calendário/hub + `tests/postgres` = 840 passed, 16 skipped (os de Postgres sem URL), 2 failed
  pré-existentes; brain-api `test_internal_professional_accounts` + convites + equipe = 26 passed; frontend 235
  arquivos / 3504 testes, tsc e build verdes. A rerodada completa nos HEADs finais é registrada em
  `../tasks/TASK-083/results/suites.md` (não verificada por este documento).
- **Postgres real** (container descartável `postgres:16`, `alembic upgrade head` → `downgrade -1` → `upgrade head`
  ok): `tests/postgres` = **14 passed, 2 skipped** (os 2 são o jitter opt-in). 20 rodadas × 8 escritores de duas
  clínicas na mesma agenda, backends local, Google falso e misto, nos modos `in_process` e `cross_process`: exatamente
  1 vencedor por janela; linking concorrente = 1 linha `Agenda`; controles negativos (sem os locks aparece mais de um
  vencedor) são permanentes.
- **Follow-up serviço × unidade (HEAD `a914c71`, 2026-11-14; `../tasks/TASK-083/results/servico-por-unidade.md`):**
  suíte secretarIA completa em lotes **8162 passed, 100 skipped, 1 xfailed, 3 failed** — as 3 falhas são idênticas no
  baseline `c9e05f8` (2× `test_service_change_agenda` UTC; `test_get_availability_tool` ordem-dependente, sem
  `ANTHROPIC_API_KEY`), 0 regressões; ruff limpo nos `.py` tocados; frontend 235 arquivos / 3531 testes, tsc e build
  verdes. Navegador real (agent-browser) + coletor SMTP local: /contexto (marcar, salvar, recarregar, última unidade
  bloqueada, desvincular recusado), chat do Portal (Sim/Não com o texto exato, lista de 2 unidades, 0 unidades ⇒ aviso
  + exatamente 1 e-mail por dia) — 13/13 verificações PASS; capturas em
  `../tasks/TASK-083/evidencias/servico-por-unidade/`.
- **E2E real** (`../tasks/TASK-083/results/validacao-e2e.md`, harness local descartável, 31+ capturas em
  `../tasks/TASK-083/evidencias/`): API 22/22, navegador 13/15 → as 2 falhas (UX baixa: listas de Unidades/Locais
  desatualizadas entre si; editor de horário próprio cortado em 375 px) foram corrigidas no frontend e reverificadas
  9/9; fluxo do paciente com 2 unidades 9/9 (pergunta a unidade, dias/horários só da unidade, endereço na confirmação,
  `unit_id` gravado; com 1 unidade não pergunta; o horário tomado em P some em C e volta ao cancelar).
