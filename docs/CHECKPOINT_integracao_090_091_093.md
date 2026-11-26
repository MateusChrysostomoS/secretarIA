# CHECKPOINT — junção TASK-090 + TASK-091 + TASK-093 (secretarIA)

Estado: **LOCAL**, branch `feature/integracao-090-091-093` (sem upstream), sem push, sem deploy. Registro central:
`BRAIN/tasks/INTEGRACAO-090-091-093/TASK.md` (prova no navegador em `evidence/REPORT.md`).

## O que entrou (ordem, todos `--no-ff`, base `db86422`)

`b2c0bbf` merge 090 (`582e460`) → `63e11ea` merge 091 (`3d8c6d5`; 8 arquivos em conflito, união dos lados) →
`9493c8c` regras cruzadas 090×091 → `6fcabb0` merge 093 Plano 2 (`08b86c9`) → `6e9f391` merge 093 Plano 3
(`57706dd`) → `71d7a4c` migração de junção `ac6a0f8102de`.

## Regras da junção (onde as tarefas se encontram)

1. **Quem salva o quê — vale a regra mais restritiva** (pedido do dono, 2026-11-25). As rotas novas da 090 que eram
   cegas a papel ficaram com a trava da 091:
   - `PUT /tenants/me/insurance-plans/{plan_id}/active` ("Outro" da clínica liga/desliga) → `manager_only`;
   - `PUT /tenants/me/professionals/{id}/insurance-plans/{plan_id}/active` ("Outro" do médico) → `professional_writer`;
   - `PUT /tenants/me/professionals/{id}/insurance-acceptance/plan-order` → `manager_only` — grava a ordem ÚNICA de
     convênios da clínica, mesmo pela matriz do médico;
   - criar "Outro" sem regra deixou de ser de qualquer papel (a 091 fechou a rota; os testes da 090 foram ajustados).
   - A regra de pagamento continua: linha da clínica = gestor; linha do médico (modo independente) = gestor ou o
     próprio médico (C8).
   - Guarda estrutural nova: `tests/test_integracao_090_091_gates.py::test_every_hub_write_route_declares_a_role_gate`
     — toda rota de escrita sob `/tenants/me` declara uma trava de papel ou aparece na lista `DECIDED_ELSEWHERE` com o
     motivo.
2. **Quais médicos o paciente vê e "um médico × vários"** — `services/booking_scope.py::topology_roster`:
   - oferecidos (listas, toques, IA, simulador "Testar") = ativos menos o médico sem unidade utilizável (090) e menos o
     convidado que não está pronto (091, `booking_blocked`);
   - a forma da clínica (`booking_topology`, `sole_active_professional`, `flow_router._is_multi_professional`, o médico
     único de um agendamento / de consulta sem dono, `list_options`, `llm_context`, `appointment_edit_flow`) = todos os
     ativos menos o convidado não pronto (o escondido por unidade CONTA, como decidiu o dono na 090);
   - consulta já marcada acha o médico em `everyone_of` (todos, convidado incluído).
   - `workers/shared/patient_roster.py` e `workers/shared/flow_simulation_input.py` tiram o convidado não pronto da
     oferta (o simulador copiava só as colunas e perdia a marca da 091).
3. **Ordem das travas de resposta** (`workers/orchestrator.py`): pagamento primeiro (clínica sem pagamento → aviso
   neutro da 093, 1 por dia), unidade completa depois (clínica em dia sem unidade completa → silêncio da 090).
4. **Migração**: `ac6a0f8102de` é uma junção vazia de `c7e3a9f1d5b8` (cadeia da 090), `a3c7e1f9b5d2` (091) e
   `e7b3d1f9a5c2` (093 Plano 3), todos a partir de `f9a1d7c4e2b6`. `alembic heads` = 1.

## Validação

- Suíte completa: 10930 passed, 132 skipped, 1 xfailed, 6 failed — 2 antigas (`test_service_change_agenda`), 3 de
  relógio/ordem (passam isoladas) e 1 dependente de data que falha igual nas 4 branches de origem
  (`test_unit_terms_money::test_a2_the_booking_gate_quotes_at_the_holds_unit`). Nenhuma regressão nova.
- Testes novos da junção: `tests/test_integracao_090_091_gates.py`, `tests/test_integracao_090_091_roster.py`
  (vermelhos antes, verdes depois).
- Migração em Postgres 16.14 descartável (porta 5433): 86 revisões; zero → cabeça, cabeça → base, base → cabeça e o
  caminho do deploy `f9a1d7c4e2b6` → `ac6a0f8102de` (7 passos), sem erro.
- Lint: nada novo da junção (diferenças em relação à base vêm das branches de origem; ver TASK.md).

## Deploy (NÃO autorizado; ordem para quando o dono pedir)

1. brain-api (sem migração nova além da `0035_user_avatar` da 091: `alembic upgrade head` à mão antes do código).
2. secretarIA: `uv run python -m alembic upgrade head` **à mão, antes do código** — aplica, a partir de `f9a1d7c4e2b6`:
   `e1a7c3b9d5f2`, `f2b8d4c6e0a3`, `a3c9e5d7f1b4`, `c7e3a9f1d5b8`, `a3c7e1f9b5d2`, `e7b3d1f9a5c2`, `ac6a0f8102de`.
3. `secretaria_api` e depois `secretaria-worker` (os dois; a junção mexe em `workers/` e `flow_router.py`).
4. Portal (Brain-Message-Frontend). 5. Sites (brain-frontend, secretarIA-frontend — este só depois de conferir os
   endereços de convite/reativação, M1 da revisão da 093).
- **Impacto da 090:** no deploy, clínica sem unidade de endereço completo deixa de ser atendida pela secretária.

## Não provado / pendências da junção

- Respostas da IA (sem modelo local): pergunta Sim/Não do convênio sem regra.
- A trava "médico comum não arrasta a ordem" foi provada por teste e pela API (403), não na tela em modo independente.
- Portal: a tela "Sobre você" é decidida pelas claims do token; token velho de um gestor-médico pode mostrá-la até
  renovar (achado da onda 4 da 090; não corrigido aqui).
