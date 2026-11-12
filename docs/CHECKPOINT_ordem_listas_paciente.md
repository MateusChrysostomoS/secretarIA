# CHECKPOINT — Ordem dos serviços na conversa do paciente (TASK-081)

Estado (2026-11-11): commitado em `main` e enviado ao GitHub; NÃO deployado (deploy pelo dono). Registro central: `../tasks/TASK-081/TASK.md` (raiz de BRAIN). A ordem dos CONVÊNIOS é a de `7e2d0a3` (`tenant_insurance_plans.sort_order`, `PUT /tenants/me/insurance-plans/order`, já no ar) — esta tarefa não mexe nela.

## O que entrou

- **A ordem dos serviços é da clínica.** `service_catalog.resolve_entries` carimba o `sort_order` do CATÁLOGO em cada entrada ligada. O worker resolve todo médico por ela antes do `route()` (`workers/shared/greeting.py::_flow_professionals` / `_flow_tenant_snapshot`; a simulação idem), então a lista do paciente segue uma ordem só, seja qual for a ordem salva de cada médico.
- `PUT /tenants/me/services/order` `{service_ids}`: grava a ordem (os não listados mantêm a ordem relativa depois). Id de outra clínica → 404, repetido → 422, nada gravado.
- Serviço criado sem `sort_order` vai para o fim (antes nascia com 0 e pulava para o topo).

## Comportamento que muda no deploy

Clínica que nunca ordenou: os serviços passam a seguir o catálogo (`sort_order`, depois nome). Serviços antigos têm `sort_order` 0 → ordem alfabética até a clínica arrastar os cartões (antes cada médico tinha a sua ordem salva). Entradas sem linha no catálogo mantêm o `sort_order` próprio.

## Limite que a tela espelha

Lista de serviços **de um médico** (`_service_list_bubble`): até `MAX_CATALOG_OPTION_ROWS` = 9 + "Não sei", sem página.

## Deploy

Sem migração. `secretaria_api` + `secretaria-worker` juntos → depois o Brain-Message-Frontend.

## Testes

`tests/test_list_order_task081.py` (inclui o caminho real do worker; mutação provada: sem o carimbo do `sort_order` 2 testes caem). Asserção de ordem atualizada em `tests/test_service_guides_multipro.py`.
