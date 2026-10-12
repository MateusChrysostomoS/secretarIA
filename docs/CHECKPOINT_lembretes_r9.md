# CHECKPOINT — Lembretes R9: lembrete extra "N dias antes, às HH:MM" (TASK-048)

Spec: `docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md` §6.2.
Plano: `docs/superpowers/plans/2026-10-10-lembretes-r9-sem-precheck-e-lembrete-dia-hora.md` (Parte A).

## Estado

- Branch `task/TASK-048-lembrete-dia-hora-api` (base: 75c81baf). Commits: b950a30 (A1), a608f90 (A2), 80706f5 (A3), ad4d8e9 + dca93c7 (A4); este checkpoint registra A5.
- **Integrado à `main` em 2026-10-10 para publicação Git autorizada pelo dono. Não deployado.** Migração não aplicada em banco real.
- Suíte: 4999 passed / 68 skipped / 0 failed; ruff limpo; head Alembic `a4c7e2f9b1d3`.

## O que entrou onde

- Regra única: `core/extra_reminder.py` (`custom_due_at`, `from_legacy_lead`, `is_valid_send_time`).
- Colunas: `tenants.reminder_extra_days_before`, `tenants.reminder_extra_send_time` (migração
  `a4c7e2f9b1d3`, converte os minutos antigos: dias arredondados para cima, 2..14, às 09:00).
- Planejamento: `services/reminder_schedule.py::schedule_reminders` (`_planned_dues`) e
  `replan_custom_reminders` usam `custom_due_at`.
- Configuração: `schemas/config.py` (par dias+hora, juntos; minutos antigos ainda aceitos) e
  `services/hub_configuration.py::apply_tenant_config` (`_apply_extra_reminder`; reprograma quando muda
  dia, hora ou fuso).

## Decisões

D1–D11 do plano ("Decisions taken where the spec is silent"): 2..14 dias; 06:00..22:00 de 15 em 15;
piso de 25 h; fuso inválido → UTC; coluna antiga mantida e espelhada (dias × 1440); minutos antigos
traduzidos mantendo a hora gravada; par vence o legado; mudança de fuso reprograma.

## Deploy (quando o dono autorizar)

1. `alembic upgrade head` a partir da imagem nova (serviços ainda antigos).
2. `secretaria_api` **e** `secretaria-worker` juntos (`GET /build` → paridade).
3. Front Brain-Message.

Rollback: código antigo nos dois serviços, depois `alembic downgrade` (o dia continua certo pela coluna
espelhada; a hora escolhida se perde).

## Pendências

- Remover a coluna `reminder_extra_lead_minutes` e o campo legado do contrato numa migração futura, depois
  que API, worker e front estiverem no R9 (code-first, depois o DROP).
- Lembretes extras já planejados antes do deploy mantêm o horário antigo até a clínica salvar a
  configuração de lembretes de novo.
- Prova real em clínica de teste após deploy autorizado.

## Validacao e ajustes de execucao

Su?te completa: 4999 passed, 68 skipped, 31 warnings em 287.66 s. Nenhuma falha nova ou baseline. `uvx ruff check src tests`: limpo; formatacao dos Python alterados: limpa; `python -m alembic heads`: a4c7e2f9b1d3. Migra??o testada em SQLite descartavel (upgrade/conversao/downgrade), nao em banco real.

- O novo par ignora o legado antes da validacao, inclusive legado invalido: regressao adicional de D11, 3 casos RED -> GREEN.
- As colunas antigas e novas seguem compativeis enquanto a tela antiga existir.
- Graphify atualizado via AST, diagnostico sem duplicatas nem endpoints ausentes (um self-loop incidental permitido pela politica).

## Revisao final

Revisao independente encontrou uma lacuna no exemplo do plano: um par migrado igual ao salvo nao reprogramava linhas R7 ainda pendentes. Corrigido em `services/reminder_schedule.py::pending_custom_reminders_need_replan` e `services/hub_configuration.py::apply_tenant_config`: ao salvar explicitamente o lembrete, divergencias pendentes sao reconciliadas pelo escritor com locks ja existente. Linhas ja alinhadas continuam sem reprogramacao, e historico enviado permanece. Duas regressoes HTTP/banco (par novo igual / eco legado) falharam antes e passaram depois; 42 testes focados e suite completa 4999 passed / 68 skipped / 0 failed. Custo: uma consulta de leitura em salvamentos explicitos inalterados, com a funcionalidade ligada.

## Integração autorizada (2026-10-10)

O dono autorizou os merges e pushes dos dois repositórios. `main` integrada por fast-forward, sem conflitos; o código integrado é idêntico ao HEAD da tarefa revisada. Validação repetida nesse mesmo commit: 4999 passed, 68 skipped, 31 warnings em 263.66 s; ruff limpo; head Alembic `a4c7e2f9b1d3`.

Deploy e migração real continuam sem autorização. A ordem operacional permanece: migração, API e worker juntos, depois front.
