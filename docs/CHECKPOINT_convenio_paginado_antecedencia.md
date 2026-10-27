# CHECKPOINT — convênios sem corte e antecedência mínima por médico (2026-10-27)

Origem: primeiro cliente real (Instituto Gicovate), teste ao vivo no Portal. Branch
`feat/convenio-paginado-antecedencia` (secretarIA) + `feat/antecedencia-minima` (Brain-Message-Frontend).

## 1. Lista de convênios paginada

Antes: `MAX_INSURANCE_PLAN_ROWS = 8` cortava em silêncio. Com 9 planos, o 9º em ordem
alfabética (Unimed) nunca aparecia (`flow_insurance_list_truncated` no log).

Agora (`flow_router.insurance_page`): até 8 planos, uma página só (igual a antes). Com mais
de 8, cada página tem 7 planos + "Ver mais convênios" + Particular + Outro convênio = 10 linhas
(o teto do WhatsApp). O cursor da página viaja no toque (`insmore|<n>`, registrado em
`schemas/webhook.py::_PAYLOAD_ROW_PREFIXES`), igual a "Ver mais dias": nenhuma coluna nova.
Vale para o agendamento (`_enter_insurance` / `_handle_insurance`) e para "Alterar dados" da
consulta (`appointment_edit_flow._insurance_list` / `_insurance_step`).
Testes: `tests/test_flow_router_insurance_paging.py` (9 a 40 planos, nenhum plano perdido,
nenhuma página acima de 10 linhas, Unimed alcançável e gravada).

## 2. Antecedência mínima para agendar (por médico)

`professionals.min_booking_notice_hours` (INTEGER NULL; migração aditiva `c3d9e1f5a7b2`).
NULL = não configurado → vale o padrão da clínica, `BOOKING_NOTICE_DEFAULT_HOURS` (env do
secretarIA, **12 h**, decisão do dono 2026-10-27; vale para TODOS os profissionais existentes
assim que o deploy subir). 0 = sem limite (escolha explícita do médico). N = consulta NOVA
marcada pelo paciente pela secretarIA nunca começa antes de agora + N horas.
A suíte de testes roda com o padrão 0 (`tests/conftest.py`), porque as agendas falsas dos
testes antigos oferecem horários relativos ao relógio real; `tests/test_booking_min_notice.py`
liga os 12 h onde testa o padrão.

| Onde | O que faz |
|---|---|
| `services/availability.py::booking_notice_floor` | a regra (agora + N h, ou None) |
| `availability.free_slots_for_day` / `available_day_starts` (`not_before`) | definição única de "livre" filtra pelo piso |
| `flow_router.enter_day_picker` / `_enter_slot_picker` | dias e horários do agendamento só depois do piso |
| `flow_router._handle_confirmation` | última checagem no "Confirmar": dentro do piso, reabre os dias com "Os agendamentos precisam de pelo menos N horas de antecedência." |
| `booking_draft.py` (pedido por frase à IA) | dia pedido dentro do piso cai nos dias com a mesma frase |
| `ai/availability_tool.py` | `get_availability` não mostra horário dentro do piso e devolve `min_booking_notice_hours` |
| hub `PUT /tenants/me/professionals/{id}/config` e `PUT /tenants/me/configuration` | campo `min_booking_notice_hours` (0–720 ou null = padrão); `GET` devolve |

**Não limita** (decisão do dono, 2026-10-27): remarcar (`MANAGE_DAY_BRANCH`), "Alterar dados"
(`EDIT_DAY_BRANCH`), cancelar, e a agenda da clínica no console ("Nova consulta").

Frontend: Contexto → Profissionais → "Antecedência mínima para agendar (horas)", vazio = padrão
de 12 h, 0 = sem limite (`lib/contexto/hub-mapping.ts::toWireNoticeHours`). Um backend antigo ignora a chave.
Testes: `tests/test_booking_min_notice.py`, `tests/test_migration_min_booking_notice.py`,
`tests/test_hub_professionals.py` (round-trip e 422), `lib/contexto/__tests__/booking-notice.test.ts`.

## 3. Deploy (ordem obrigatória)

1. `alembic upgrade head` com a imagem NOVA (a antiga nunca lê a coluna).
2. `secretaria_api` E `secretaria-worker` juntos (`GET /build` → `deploy_parity: match`).
3. Brain-Message-Frontend.

Rollback: código antigo nos dois serviços, depois `alembic downgrade b7e2c4f9a1d6`.

## 4. Não provado

Produção (nada foi deployado nesta sessão). O cliente Instituto Gicovate precisa, depois do
deploy, de `min_booking_notice_hours = 24` no Dr. Carlos (tela ou API); as demais clínicas
passam a ter 12 h sem fazer nada.
