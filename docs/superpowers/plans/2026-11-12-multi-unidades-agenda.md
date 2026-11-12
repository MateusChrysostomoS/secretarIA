# Multi-unidades + Agenda compartilhada — plano de implementação (TASK-083)

Spec (autoridade): `docs/superpowers/specs/2026-11-12-multi-unidades-agenda-compartilhada-design.md` (secretarIA worktree).
Worktrees (branch `task/TASK-083-multi-unidades`, base `main`): `C:\TECH\BRAIN-worktrees\TASK-083\{secretarIA,brain-api,Brain-Message-Frontend}`.

## Global Constraints (valem para TODAS as tarefas)

- Spec §3: sem `agenda_id` o comportamento é idêntico ao de hoje. Todo caminho novo vira no-op nesse caso.
- Privacidade: o ocupado de irmãos é só `(start, end)`; nunca summary, paciente, serviço, nome de tenant/clínica — nem em log nem em resposta de API.
- Identidade só vem do brain-api (`account_id`); nada de ligar por e-mail/nome; hub nunca aceita `agenda_id`/`person_key` do cliente.
- secretarIA: camadas `api → workers → services/ai → models → core`; sem fatos de clínica hardcoded em prompts; env só por `config.py::Settings`; segredos nunca em log. Migração Alembic aditiva, head atual `d2f6b8a4c1e7` (um só head; confirmar com `uv run alembic heads`), estilo dos docstrings de `migrations/versions/*` (rationale + DEPLOY ORDER).
- Testes secretarIA: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest -q -p no:cacheprovider <arquivos>`; SQLite em memória (`tests/_local_agenda.py::wire_local_agenda`, fixture `db`). **Não rode a suíte inteira de uma vez** (já estourou memória): rode por grupos de arquivos relacionados; a suíte completa só na tarefa 11, em lotes. Falha pré-existente no HEAD (`make lint` vermelho no HEAD, `ruff format .` PROIBIDO) ≠ regressão — prove com `git stash`/HEAD antes de reportar.
- brain-api: `uv run pytest` (SQLite), `ruff check`; `.env` local NUNCA copiado; sem tokens/segredos em nenhum arquivo.
- Brain-Message-Frontend: gates `npm test`, `.\node_modules\.bin\tsc.cmd --noEmit` (nunca `npx tsc`), `npm run build`; NÃO rodar `npm run lint` (sem ESLint, trava); textos pt-BR sem jargão, código/comentários em inglês; tokens só em `app/styles/tokens.css`; sem `fetch` fora de `lib/real/*`.
- Git: commits locais frequentes na branch da tarefa; NUNCA push, deploy, `git add -A` (sessões paralelas — adicione arquivos pelo nome), nem `--no-verify`. Mensagens terminam com `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Segurança Easypanel (`EASYPANEL_CLI_SECURITY.md`): nenhuma ferramenta/CLI do Easypanel nesta tarefa. Nada de produção.
- Docs só depois de validar (tarefa 11). TDD: teste falhando primeiro quando o comportamento é novo.

## Contratos congelados (todas as tarefas leem estes valores literalmente)

- Tabelas/colunas: spec §5. `agendas.person_key` = `str(StaffAccount.id)`.
- brain-api: `GET /internal/tenants/{tenant_id}/professional-accounts` → `200 [{"professional_id": "<uuid>", "account_id": "<uuid>"}]`; 401/403 como `professional-emails`.
- Hub secretarIA (auth do hub, como `units.py`):
  - `GET /tenants/me/professionals/{pid}/units` → `{"units":[{"unit_id","name","address","is_active","linked":bool,"business_hours": <dict|null>}]}` (todas as unidades ativas do tenant, `linked` marca o vínculo).
  - `PUT /tenants/me/professionals/{pid}/units` body `{"links":[{"unit_id","business_hours":<dict|null>}]}` (substitui o conjunto; vazio = sem vínculos). Erros: 403 `multi_unit_not_entitled` (só quando passa de 1 unidade vinculada), 409 `unit_limit_reached`, 404 unidade de outro tenant.
  - `GET /tenants/me/professionals/{pid}/agenda` → `{"shared":bool,"other_tenants_count":int}`.
  - `POST /tenants/me/agendas/sync` → `{"linked":int,"shared":int}`.
  - `GET /tenants/me/units` ganha `professionals_count:int`.
  - Resposta de criação/edição de consulta pelo hub ganha `conflicts_other_agenda:bool` (default false).

### Task 1: Caracterização + migração + modelos

Escopo: secretarIA. (a) Escreva testes de caracterização que fixam o comportamento atual SEM agenda: dois profissionais do mesmo tenant não se bloqueiam; slots livres/`slot_taken_at_confirm`/`create_event_exclusive` local de um tenant ignoram eventos de outro tenant (`tests/test_agenda_characterization.py`). (b) Modelos `Agenda` (`models/agenda.py`), `ProfessionalUnit` (`models/professional_unit.py`), colunas `Professional.agenda_id`, `CalendarEvent.unit_id`, `BookingHold.unit_id`, exportados em `models/__init__.py` (spec §5). (c) Migração Alembic aditiva única (down_revision = head atual) com `upgrade`/`downgrade` e DEPLOY ORDER no docstring. (d) Testes de modelo/migração (cria via `Base.metadata.create_all`; e um teste que `alembic heads` retorna 1 head).
Aceite: testes novos verdes; os testes de caracterização passam também sobre o HEAD sem as mudanças de modelo (prove com `git stash`); `uv run alembic heads` = 1 head; `uv run alembic upgrade head --sql` gera SQL sem erro; ruff dos arquivos tocados limpo.

### Task 2: brain-api — rota `professional-accounts`

Escopo: brain-api, SÓ nesse repo (worktree `...\TASK-083\brain-api`). Adicione `GET /internal/tenants/{tenant_id}/professional-accounts` ao lado de `professional-emails` em `api/internal.py` (mesmo gate de chave interna, mesmo filtro: usuários do tenant com `professional_id` não nulo), retornando `[{professional_id, account_id}]` (contrato congelado). Schema em `schemas/internal.py`. Documente a rota na tabela de `CONTRACTS.md` §16.6 (e §6 se mencionar tabelas). Testes em SQLite seguindo `tests/` existentes de `professional-emails`: feliz, tenant sem profissionais (`[]`), 401/403 sem chave, nunca devolve e-mail/nome, tenant isolado (não vaza usuário de outro tenant).
Aceite: `uv run pytest` dos arquivos tocados + os de `internal` verdes; `ruff check` limpo nos arquivos tocados.

### Task 3: Serviço `agenda` (ocupado/lock) + ligação de agendas

Escopo: secretarIA. (a) `services/agenda.py`: `sibling_professional_ids`, `shared_busy` (appointments vivos + `calendar_events` locais ocupados + holds fase `payment` dos irmãos; janelas apenas), `agenda_write_lock` (chave `agenda:<id>`, no-op em SQLite, mesmo padrão de `local_calendar._advisory_lock`), tudo no-op/vazio sem `agenda_id`. (b) Cliente do brain-api `professional-accounts` em `services/brain_professionals.py` (mesmo estilo/erros dos clientes existentes). (c) `services/agenda_linking.py` (spec §6; tolerante a falha). (d) Gatilho em `api/internal_provisioning.py` (criação/attach), `POST /tenants/me/agendas/sync` e `GET /tenants/me/professionals/{pid}/agenda` em `api/hub/` (gestor para sync), `scripts/reconcile_agendas.py` (todos os tenants, idempotente, `--dry-run`). Testes: dois tenants/mesma conta ⇒ mesma agenda; contas diferentes ⇒ não liga; brain-api 500/timeout ⇒ sem erro e sem `agenda_id`; `shared_busy` só devolve janelas (assert no tipo) e ignora cancelados/code-phase/expirados; hub rejeita `agenda_id` no body; hub `GET .../agenda` não devolve nomes.
Aceite: testes novos verdes; nenhum arquivo existente mudou de comportamento sem agenda.

### Task 4: Leitura compartilhada em CalendarService + escrita local atômica

Escopo: secretarIA (`services/calendar.py`, `services/local_calendar.py`, `services/tenant_config.py`, `services/availability.py`). Spec §7 itens 1–2. `shared_busy_provider` opcional no `CalendarService` (injetado em `from_tenant_config`/`resolve_professional_calendar` quando `professional.agenda_id`), consumido em `check_availability`, `has_conflict`, `is_slot_free`, `list_free_slots`/`list_available_days` e `slot_taken_at_confirm`. `LocalCalendarService`: `agenda_write_lock` pela chave da agenda e `_overlap_exists` com `shared_busy`. Testes de duas clínicas (tenants P e C, mesma agenda, agenda local): slots de C somem quando P ocupa; `create_event_exclusive` de C falha com `CalendarSlotTakenError` quando P ocupa; cancelar em P libera em C; bloqueio local de P bloqueia C; hold `payment` de P bloqueia C, hold `code` não; agenda diferente não interfere; sem agenda nada muda (testes da Tarefa 1 continuam verdes).
Aceite: testes novos + caracterização verdes; rode também `tests/test_local_calendar*.py`, `tests/test_availability*.py`, `tests/test_booking_hold*.py`, `tests/test_calendar*.py`.

### Task 5: Escrita exclusiva também no Google + demais guardas + aviso no hub

Escopo: secretarIA. Spec §7 itens 3–5. `supports_exclusive_write` e `exclusive_write` genérico em `CalendarService` (Google) quando há `agenda_id` (lock da agenda + `shared_busy` + `check_availability` + cria); substitua os `isinstance(calendar, LocalCalendarService)` que guardam o caminho exclusivo (`local_calendar.py` ~600/613/621 e chamadores em `flow_router.py`, `ai/tools.py`, `workers/shared/booking_hold.py`, `booking_promotion.py`, `release_keep.py`) por checagem de capacidade, sem mudar comportamento sem `agenda_id`. `release_keep._consultation_overlaps` e `payments/service_change` incluem appointments dos irmãos. Hub (`api/hub/calendar.py`, `staff_appointment_edit.py`): `conflicts_other_agenda` na resposta, sem bloquear. Testes por caminho de escrita (fluxo, tools da IA, promoção de hold/Pix, release_keep, edição) com Google falso (fakes existentes) e agenda compartilhada: o segundo é recusado; sem agenda: igual a hoje.
Aceite: testes novos verdes e os arquivos de teste dos módulos tocados verdes (rode por arquivo).

### Task 6: Unidades por profissional — serviço, horários e API do hub

Escopo: secretarIA. `services/professional_units.py`: vínculos, resolução do horário por unidade (herda `business_hours` do profissional se NULL; spec §5), `available_unit_ids(professional)`; API hub `GET/PUT /tenants/me/professionals/{pid}/units` e `professionals_count` em `GET /tenants/me/units` (contratos congelados; gestor ou o próprio médico; mesmo entitlement/limite/fail-closed de `api/hub/units.py`); ao desativar uma unidade, vínculos ficam mas deixam de ser oferecidos. `schemas/`. Testes: entitlement, limite, 404 cross-tenant, substituição de conjunto, horário herdado vs próprio, desativar unidade, permissão (secretária não edita médico alheio, regra igual à de `professionals.py`).
Aceite: testes verdes + `tests/test_multi_unit_plugin.py` e testes de `units` seguem verdes.

### Task 7: Unidade no fluxo do paciente e na IA

Escopo: secretarIA (`workers/shared/flow_runner.py`/`flow_router.py`, `services/booking_details.py`, `workers/shared/draft_resolution.py`, `workers/shared/booking_hold.py`, `ai/tools.py`, `plugins/multi_unit.py`, `plugins/multi_professional.py`, lembretes). Spec §8: estado `AWAITING_UNIT` (siga o padrão do passo de profissional/convênio, com botões fixos e corte de texto do WhatsApp), pula com 1 unidade, sem vínculos = fluxo de hoje; slots pelo horário da unidade; `unit_id` em Appointment/CalendarEvent/BookingHold; `location` no evento; confirmação e lembrete mostram unidade+endereço; corrigir `draft_resolution` que zera o endereço quando há unidades (só quando não há unidade escolhida); `create_event_at_unit`/disponibilidade da IA respeitam `professional_units`; reagendar mantém a unidade. Testes de fluxo (WhatsApp e Portal): 0, 1 e 2 unidades; escolher unidade; endereço na confirmação; horário da unidade restringe slots; persistência de `unit_id`; arquitetura (`tests/test_workers_layering.py`, `tests/test_channel_branches.py`) segue verde.
Aceite: testes novos + `tests/test_flow*.py`, `tests/test_multi_doctor*.py`, `tests/test_workers_layering.py`, `tests/test_channel_branches.py` verdes (por arquivo).

### Task 8: Prova de concorrência real em Postgres

Escopo: secretarIA (só testes/scripts). Suba Postgres descartável em Docker (Docker Desktop pode estar parado — inicie `Docker Desktop` e aguarde; use porta livre, banco `t083`, credenciais sintéticas inline, NUNCA `.env`), aplique as migrações com `alembic upgrade head` e escreva `tests/postgres/test_agenda_concurrency.py` (marcado/pulado sem `T083_PG_URL`) que dispara N escritas simultâneas de duas clínicas na mesma agenda e prova exatamente 1 vencedor, para backend local e backend Google falso. Rode também `alembic upgrade head` → `downgrade -1` → `upgrade head`.
Aceite: saída dos comandos colada no relatório; 1 vencedor em 20 rodadas × 8 concorrentes; migração ida/volta/ida ok.

### Task 9: Brain-Message-Frontend — Unidades, Locais de atendimento, agenda compartilhada

Escopo: Brain-Message-Frontend (worktree). Spec §10. (a) Tipos e cliente real/mock para os contratos congelados (`lib/real/secretaria-hub.ts`, `lib/console-api.ts` + mock, `lib/contexto/types.ts`), sem `fetch` fora de `lib/real/*`. (b) `/contexto`: seção "Unidades" (`components/contexto/UnitsSection.tsx`: listar/criar/editar/ativar, via `/tenants/me/units`; ids em `SectionId` e `TOC_LABELS`, ordem em `CONTEXTO_SECTION_ORDER`), "Locais de atendimento" dentro de `ProfessionalRow` (marcar unidades; horário próprio por unidade reaproveitando `AvailabilitySection`/`WeekGrid`), selo "Agenda compartilhada" + botão "Sincronizar agenda" (`POST /tenants/me/agendas/sync`; o selo mostra só a contagem, nunca nomes). (c) `/agenda`: unidade/endereço no `Drawer`, seletor de unidade no `NewApptModal` quando o médico tem >1, aviso "Este horário já está ocupado em outra agenda desta pessoa" quando `conflicts_other_agenda`. Estados de carregando/erro/vazio; `multi_unit_not_entitled` e `unit_limit_reached` com texto claro em pt-BR. Testes vitest (`renderToStaticMarkup`) por componente + funções puras.
Aceite: `npm test`, `.\node_modules\.bin\tsc.cmd --noEmit` e `npm run build` verdes.

### Task 10: Validação ponta a ponta (API + navegador com agent-browser)

Escopo: nenhum código de produto (só scripts de validação sob `_local-test/` git-ignorado e relatório). Monte o ambiente local a partir de `C:\TECH\BRAIN-worktrees\TASK-081\_local-test` (copie o padrão `run.sh`/`gateway.py`/seeds, portas novas livres, Postgres Docker da Tarefa 8, tudo sintético, sem `.env`), com seed: 2 tenants (P pessoal com 2 unidades, C clínica), o mesmo `StaffAccount` como médico nos dois, entitlement `multi_unit`. (1) Via API: sincronizar agendas; paciente em P ocupa terça 10h; tentativa em C no mesmo horário é recusada e o horário some da lista de C; cancelar em P libera em C. (2) Via `agent-browser` (Vercel, v0.34 já instalado): login do gestor de P; cria unidade; vincula ao médico com horário próprio; abre `/agenda` e vê unidade/endereço; aviso de conflito ao encaixar manualmente em horário ocupado pelo irmão; capturas de tela em `tasks/TASK-083/evidencias/`. (3) Fluxo do paciente (Portal/chat) com 2 unidades: pergunta a unidade e a confirmação traz endereço.
Aceite: relatório `tasks/TASK-083/results/validacao-e2e.md` com cada passo, comando e resultado reais (PASS/FAIL); qualquer FAIL vira correção por resume do implementador responsável.

### Task 11: Documentação, suíte completa e fechamento

Escopo: os 3 repos. (a) Suíte completa do secretarIA em lotes (por diretório/prefixo de arquivo, com `-p no:cacheprovider`, `BOT_ALLOWLIST_WA_IDS=""`), brain-api (`uv run pytest`), Brain-Message-Frontend (`npm test`, tsc, build); separe falha pré-existente de regressão com evidência. (b) `docs/CHECKPOINT_agenda_multi_unidades.md` no secretarIA (estado, o que entrou onde, pendências, ordem de deploy, limitação do Google, como reconciliar) + 1 linha de ponteiro no `CLAUDE.md`/`AGENTS.md` do secretarIA; `brain-api/CONTRACTS.md` e CLAUDE/AGENTS pointer; checkpoint curto no Brain-Message-Frontend (+ ponteiro no `CLAUDE.md`). (c) `tasks/TASK-083/TASK.md` com SHAs base/final por repo, estado local×commitado×deployado (`Deployment: NOT AUTHORIZED`), resultados dos testes. (d) `graphify update .` nos repos com `graphify-out` FRESH (`python -m graphify`, não o .exe). (e) Revisão final de ramo completa.
Aceite: tudo acima presente e commitado localmente; nenhuma regressão não explicada.
