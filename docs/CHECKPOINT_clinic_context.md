# CHECKPOINT — Contexto da clínica que a LLM enxerga (TASK-025, parte backend)

Spec: `C:\TECH\BRAIN\tasks\TASK-025\SPEC.md` (BRAIN, fora deste repo). Plano: `tasks/TASK-025/` (BRAIN). Executado em 2026-10-04.

**Estado: local + commitado, NÃO mesclado em `main`, NÃO pushado, NÃO deployado, migração NÃO aplicada em banco real.**
Branch `task/TASK-025-clinic-context` (base `main@1b3b7a6`). Commits: `4a0edd4` (arquivo de referência do prompt),
`527155f` (migração `e5a1c9d3b7f2`), `95a88c5` (código + testes). A parte de frontend (fatia `clinicFacts` na tela `/contexto`
do Brain-Message-Frontend) é a Parte B do mesmo plano e fica em outro repo.

## 1. O que mudou para o paciente

Quando a LLM livre é quem responde ("onde fica?", "tem estacionamento?", "preciso de jejum?"), ela passa a ver o que a clínica
cadastrou: endereço e fatos da clínica no prompt, e as orientações de cada serviço sob demanda. Se a clínica não cadastrou, o
prompt manda a LLM dizer que vai confirmar com a equipe em vez de inventar. O gestor ganha uma lista determinística do que
ainda falta preencher.

**Efeito no deploy:** o prompt de um tenant SEM endereço, SEM `clinic_facts` e sem serviço com orientações fica byte a byte igual
ao de antes (prova: `tests/golden/system_prompt_default.txt`). Um tenant que já tem endereço passa a ter o bloco "SOBRE A
CLÍNICA" assim que o código novo rodar — é o objetivo, mas é uma mudança de prompt em produção.

## 2. Onde está cada peça (âncoras estáveis)

| Peça | Onde |
|---|---|
| Coluna `tenants.clinic_facts` (JSON, nullable, sem default) | `models/tenant.py::Tenant.clinic_facts`; migração `migrations/versions/e5a1c9d3b7f2_tenant_clinic_facts.py` (`down_revision = c3a9e5f1d7b2`) |
| Esquema com limites no servidor | `schemas/clinic_facts.py::ClinicFacts`, `FaqItem` |
| Entrada/saída do hub | `schemas/config.py::TenantConfigUpdate.clinic_facts`, `TenantConfigRead.clinic_facts`; `services/hub_configuration.py::TENANT_SCALAR_FIELDS` e `tenant_read_model` (o envelope `/configuration` herda o campo) |
| Dados disponíveis para o prompt | `services/tenant_config.py::TenantRuntimeConfig.address` e `.clinic_facts`, preenchidos em `load_tenant_config` |
| Bloco do prompt | `ai/prompts.py::_format_clinic_facts`, `_clinic_fact_lines`, `CLINIC_FACTS_BUDGET`; inserido em `secretary_system_prompt` entre o contexto profissional e o conhecimento pós-consulta |
| Marcador de orientações no serviço | `ai/prompts.py::_format_appointment_types` |
| Tool somente-leitura | `ai/tools.py::get_service_info`, registrada em `ai/graph.py::_SCOPE_FREE_TOOLS` |
| Completude | `services/context_completeness.py::compute_completeness`; rota `api/hub/context_completeness.py` (`GET /tenants/me/context-completeness`), incluída em `main.py::create_app` |

## 3. Contrato do hub (aditivo, congelado em SPEC §3 — o frontend implementa contra ele)

`PUT /tenants/me/configuration` — o objeto `tenant` aceita `clinic_facts` (opcional; `null` apaga; ausente não altera; um
objeto SUBSTITUI o anterior inteiro, não faz merge):

```json
{ "tenant": { "clinic_facts": {
    "parking": "string|null", "how_to_arrive": "string|null",
    "payment_methods": ["string"], "cancellation_policy": "string|null",
    "documents_to_bring": ["string"], "accessibility": "string|null",
    "faq": [{"question": "string", "answer": "string"}], "notes": "string|null" } } }
```

`GET /tenants/me/configuration` (e o legado `GET /tenants/me/config`) devolvem o mesmo objeto em `tenant.clinic_facts` (`null`
se nunca salvo).

Limites (rejeitados com 422 no servidor): `parking` ≤300; `how_to_arrive` ≤400; `payment_methods` ≤10 itens de ≤60;
`cancellation_policy` ≤500; `documents_to_bring` ≤10 itens de ≤120; `accessibility` ≤300; `faq` ≤15 itens
`{question ≤150, answer ≤400}`; `notes` ≤1000. Texto é aparado; texto em branco vira `null`; itens em branco das listas são
descartados.

`GET /tenants/me/context-completeness` (auth do hub, tenant do token):

```json
{ "score": 62,
  "items": [ { "key": "parking_or_arrival", "label": "Estacionamento ou como chegar",
               "status": "done|missing|optional", "hint": "string|null",
               "section": "address|facts|services|hours|insurance|messages|post_consult" } ] }
```

14 itens: 6 obrigatórios (peso 1: endereço com rua+cidade, horários, ≥1 serviço ativo, estacionamento ou como chegar, formas de
pagamento, política de cancelamento) e 8 recomendados (peso 0,5: descrição da clínica, mensagem para quem volta, mensagem
pós-consulta, orientações pós-consulta, documentos a levar, ≥3 itens de FAQ, orientações em cada serviço, modo de convênio
definido). `missing` = obrigatório ausente; `optional` = recomendado ausente; `done` tem `hint = null`. `score` = 0–100
arredondado. Determinística, sem LLM, sem gate de entitlement.

## 4. O bloco "SOBRE A CLÍNICA" e o orçamento

- Orçamento total de **1.800 caracteres** (`CLINIC_FACTS_BUDGET`), contando título e fecho. O limite máximo de cada campo vem de
  `ClinicFacts`, então o pior caso é conhecido.
- Ordem de prioridade: endereço (de `Tenant.address`, não duplicado) → como chegar → estacionamento → pagamento → cancelamento →
  documentos → acessibilidade → FAQ → notas. Estourou? A lista termina no primeiro item que não cabe (FAQ primeiro, depois
  notas); nunca se corta um item pela metade, exceto quando UM único item sozinho é maior que o orçamento inteiro (cortado e
  terminado com `…`, nunca descartado).
- Sem nenhum fato ⇒ bloco ausente (prompt inalterado).
- Fecho fixo, nunca cortado: "Se a informação pedida não estiver acima nem vier de get_service_info, não invente: diga que vai
  confirmar com a equipe."
- Serviços: `requirements` e `long_description` NÃO entram no prompt. A linha do serviço ganha o sufixo ` (há orientações: use
  get_service_info)` só quando o serviço tem um dos dois.
- `get_service_info(service_name)`: lê SÓ o catálogo efetivo da conversa em curso (`_tenant_config_ctx` via
  `_effective_service_catalog` — o catálogo do profissional quando há um único profissional ativo, senão o do tenant) e casa o
  nome com `_match_by_name`. Não consulta o banco: o isolamento entre clínicas vem do contexto, não de filtro. Nome desconhecido
  devolve um erro que lista os serviços da clínica. Devolve `servico`, `duracao_min`, `preco`, `descricao`,
  `descricao_completa`, `orientacoes`.
- Os fatos entram no prompt sem pseudonimização (informação comercial pública, como os demais campos do tenant). Texto vindo de
  fontes externas (TASK-026) sempre passa por revisão do gestor antes de virar `clinic_facts`; esta tarefa só lê o que o gestor
  salvou. Quebras de linha internas dos textos livres não são normalizadas.

## 5. Validação (2026-10-04)

- 48 testes novos (`tests/test_prompts_clinic_facts.py`, `tests/test_clinic_context_backend.py`, `tests/test_clinic_facts_hub.py`):
  prompt de tenant vazio idêntico ao de referência, orçamento (aviso nunca cortado, endereço nunca cortado antes do FAQ),
  `get_service_info` com duas configurações (sem vazamento entre clínicas), `PUT` (ausente preserva / `null` limpa / objeto
  substitui / acima do limite é 422), completude e mensagens prontas.
- TDD: os testes novos falharam antes do código (11 falhas + 2 erros de coleta na base `1b3b7a6`) e passaram depois (48).
- Suíte completa: **2903 passed, 10 skipped, 0 falhas** (base `1b3b7a6`: 2855 passed, 10 skipped) — exatamente +48.
- Dois testes antigos fixavam o conjunto de tools; só a constante esperada ganhou `get_service_info`
  (`tests/test_agent_tool_enforcement.py::_SCOPE_FREE_TOOLS`, `tests/test_agent_capability_cache.py::test_build_agent_base_tools_unchanged`).
- `ruff check src tests`: limpo. Cabeça única do alembic: `e5a1c9d3b7f2`. SQL offline da migração conferido
  (`ALTER TABLE tenants ADD COLUMN clinic_facts JSON` / `DROP COLUMN`).
- **Não feito:** migração aplicada em banco real (sem Postgres descartável disponível; o SQLite dos testes cria o schema por
  `create_all`); prova ao vivo com a LLM respondendo com os fatos; Parte B (frontend).

## 6. Ordem de deploy e reversão

Deploy (regra `frozen-contract-migration`): **migração `e5a1c9d3b7f2` primeiro** (aditiva, coluna nullable sem default), depois
**API e worker** (deploys separados — o worker lê o modelo e não pode rodar a versão nova antes da coluna existir). Entre a
migração e o código novo nada muda para o paciente.

Reversão: primeiro voltar API e worker ao código anterior (`git revert` dos commits `95a88c5` e `527155f`, e deploy dos dois);
só depois `alembic downgrade -1` (derruba a coluna — o código novo a mapeia, então não derrube a coluna com ele no ar).

## 7. Pendências

- Ingestão dos fatos por site/documento/texto: TASK-026 (esta tarefa não escreve `clinic_facts` por conta própria).
- Parte B: fatia `clinicFacts` e cartão "O que falta?" na tela `/contexto` do Brain-Message-Frontend (contrato da §3).
- Aplicar a migração em um Postgres descartável antes do deploy real.
- Merge em `main` e push/deploy: exigem pedido explícito do dono.
