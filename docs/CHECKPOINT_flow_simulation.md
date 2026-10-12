# CHECKPOINT — Simulação do fluxo "Agendar" (TASK-027)

Estado: backend implementado e testado na branch `task/TASK-027-flow-simulation` (commit local, sem push, sem deploy). Frontend: Parte B da TASK-027 (repo Brain-Message-Frontend).

## Contrato

`POST /tenants/me/simulation/turn` (`api/hub/simulation.py`), autenticado por `get_current_tenant`, **sem estado**: a posição na conversa (`state`) vai e volta com o navegador.

- Pedido (tudo opcional, `extra="forbid"`): `draft` (`tenant` = `TenantConfigUpdate`, `professional_id`, `professional` = `ProfessionalConfigUpdate`), `state`, `tap {id,title}`, `text` (≤500). `state` ausente = começar no **Agendar**; `tap` vence `text`.
- Resposta: `bubbles` (`text` | `buttons` | `list`, mesmo formato dos registros interativos que o Brain-Message-Frontend converte), `state`, `finished`, `note`.
- Erros: `422` (rascunho inválido ou estado adulterado), `404` (profissional de outra clínica).

## Como funciona

- `workers/shared/flow_simulation.py` — motor: chama o `route()` real com um calendário falso (`SimCalendar`) que herda a aritmética real de horários; ninguém está ocupado; as escritas são no-ops registrados. Nunca usa `_apply_flow_result`.
- `workers/shared/flow_simulation_input.py` — monta a entrada como o orquestrador monta e sobrepõe o rascunho em cópias em memória (`SimpleNamespace`); nunca altera objetos ORM.

## Garantias e testes

| Garantia | Teste |
|---|---|
| Nenhum `commit`/`add`, nenhuma linha nova | `tests/test_flow_simulation_input.py::test_nothing_is_ever_committed_or_added`, `tests/test_simulation_api.py` |
| Nenhum job na fila, nenhum envio, nenhum LLM | `tests/test_simulation_api.py` |
| Rascunho muda o que é oferecido; cadastro não muda | `tests/test_flow_simulation_input.py`, `tests/test_simulation_api.py` |
| Rascunho inválido recusado como o Salvar recusaria | `tests/test_simulation_api.py` |
| Estado adulterado / profissional alheio recusados | `tests/test_simulation_api.py` |
| `shared/` sem tocar canais | `tests/test_workers_layering.py` |

A garantia de "zero commit" foi provada por mutação: acrescentar `await session.commit()` em `flow_simulation_input.py` faz `test_nothing_is_ever_committed_or_added` falhar.

## O que NÃO é simulado

IA livre, passar para humano, cancelar/remarcar (exige consultas existentes), identidade/OTP/LGPD, aparência do WhatsApp. Esses pontos viram `note` na resposta.

## Pendências

- Teste real com banco descartável (PLAN A2): contar `appointments`/`messages` antes e depois e conferir que o Google Calendar não recebeu evento. Não executado.

## Reverter

Remover `simulation` do import e o `include_router(simulation.router)` de `main.py`; os módulos novos ficam inertes.
