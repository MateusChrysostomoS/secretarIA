# CHECKPOINT — Contexto da clínica que a LLM enxerga (TASK-025, parte backend)

Spec: `C:\TECH\BRAIN\tasks\TASK-025\SPEC.md` (BRAIN, fora deste repo). Plano: `tasks/TASK-025/` (BRAIN). Executado em 2026-10-04.

**Estado (atualizado em 2026-10-05): mesclado em `main` como `f7a903e` e pushado em 2026-10-05; deployado pelo dono no mesmo dia. A versão no ar (`main@f7a903e`) tinha a lacuna das orientações por serviço em clínicas com 2+ profissionais, descrita e corrigida na §9.** A migração `e5a1c9d3b7f2` só foi exercitada em SQLite durante o desenvolvimento (não havia Postgres descartável); a ordem de deploy está na §6.
Branch de origem `task/TASK-025-clinic-context` (base `main@1b3b7a6`, HEAD `c029714`). Commits: `4a0edd4` (arquivo de referência do
prompt), `527155f` (migração `e5a1c9d3b7f2`), `95a88c5` (código + testes), `93702ad` (este documento), `b20de90` (leitores
tolerantes a `clinic_facts` malformado + higiene dos testes), `c029714` (correções de docs). Suíte completa: 2948 passed,
10 skipped, 0 falhas (base `1b3b7a6`: 2855); `ruff` limpo; prompt de referência byte a byte idêntico para um tenant sem
endereço, sem fatos e sem orientações de serviço. Prova no navegador em 2026-10-05 contra os routers reais do hub DESTE
backend (SQLite em memória) + o export estático do frontend — não contra o ambiente deployado. A parte de frontend (fatia
`clinicFacts` na tela `/contexto` do Brain-Message-Frontend) é a Parte B do mesmo plano e fica em outro repo.

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
| Orientações da clínica inteira (§9) | `services/tenant_config.py::TenantRuntimeConfig.service_guides`, `RuntimeServiceGuide`, `runtime_service_guides`; linha no prompt em `ai/prompts.py::_format_service_guides`; casamento de nomes em `services/service_catalog.py::find_by_name` e `missing_from` |
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

`GET /tenants/me/config` devolve `clinic_facts` no topo; a resposta do `PUT /tenants/me/configuration` o devolve em
`tenant.clinic_facts`; `null` se nunca salvo. (`GET /tenants/me/configuration` não existe: só o `PUT`.) Campos de lista não
aceitam `null` (422), e o GET devolve o objeto exatamente como foi salvo (só as chaves enviadas, sem preencher padrões).

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
- `get_service_info(service_name)`: lê o catálogo efetivo da conversa em curso (`_tenant_config_ctx` via
  `_effective_service_catalog` — o catálogo do profissional quando há um único profissional ativo, senão o do tenant) e, desde
  2026-10-05, também `service_guides` (§9). Casa o nome com `_match_by_name` e, se não achar, ignorando acento e espaço interno.
  Não consulta o banco: o isolamento entre clínicas vem do contexto, não de filtro. Nome desconhecido devolve um erro que lista
  os serviços da clínica (a união das duas fontes, sem repetir). Devolve `servico`, `duracao_min`, `preco`, `descricao`,
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
- Suíte completa na primeira rodada (HEAD `95a88c5`): **2903 passed, 10 skipped, 0 falhas** (base `1b3b7a6`: 2855 passed,
  10 skipped) — exatamente +48. Após a revisão final (`b20de90`, HEAD `c029714`): **2948 passed, 10 skipped, 0 falhas**.
- Dois testes antigos fixavam o conjunto de tools; só a constante esperada ganhou `get_service_info`
  (`tests/test_agent_tool_enforcement.py::_SCOPE_FREE_TOOLS`, `tests/test_agent_capability_cache.py::test_build_agent_base_tools_unchanged`).
- `ruff check src tests`: limpo. Cabeça única do alembic: `e5a1c9d3b7f2`. SQL offline da migração conferido
  (`ALTER TABLE tenants ADD COLUMN clinic_facts JSON` / `DROP COLUMN`).
- Prova no navegador (2026-10-05): export estático do frontend falando com os routers reais do hub deste backend (SQLite em
  memória, não o ambiente deployado). Cobre o ciclo de salvar/limpar `clinic_facts` e a rota de completude; detalhes no
  checkpoint do frontend (`docs/CHECKPOINT_contexto_clinic_facts.md`, Brain-Message-Frontend).
- **Não feito:** migração aplicada em banco real (sem Postgres descartável disponível; o SQLite dos testes cria o schema por
  `create_all`); prova ao vivo com a LLM respondendo com os fatos.

## 6. Ordem de deploy e reversão

Deploy (regra `frozen-contract-migration`): **migração `e5a1c9d3b7f2` primeiro** (aditiva, coluna nullable sem default), depois
**API e worker** (deploys separados — o worker lê o modelo e não pode rodar a versão nova antes da coluna existir). Entre a
migração e o código novo nada muda para o paciente.

Cuidados antes de rodar a migração:

- Confira em `pg_stat_activity` se há sessões `idle in transaction` e rode a migração com um `lock_timeout` curto:
  `ALTER TABLE ... ADD COLUMN` pega por um instante um lock exclusivo em `tenants`, a tabela mais quente, e toda leitura de
  Tenant fica na fila atrás dele (foi o que travou a migração do incidente de 2026-09-27, presa atrás de conexões zumbi
  `idle in transaction`).
- A ordem do README ("rode `alembic upgrade head` depois de cada deploy") está ERRADA para esta mudança: código novo antes da
  migração faz toda leitura de Tenant levantar `UndefinedColumn`. Se os serviços fazem deploy automático no push para `main`,
  aplique a migração em produção ANTES do push — o que exige autorização explícita do dono, porque mexe no banco de produção.

Reversão:

1. Volte API e worker ao código anterior: redeploy do commit/imagem anterior a `95a88c5` (ex.: `1b3b7a6`) nos dois serviços.
   Se for por `git revert`, reverta `c029714` (docs, opcional), `b20de90` e `95a88c5`, nessa ordem (o mais novo primeiro; é o
   `95a88c5` que mapeia a coluna), e NUNCA `527155f` (a migração), para o script da migração continuar no repositório e o
   `alembic upgrade head` seguinte não quebrar.
2. Normalmente pare aí: o código antigo ignora a coluna nullable e os fatos já salvos ficam preservados.
3. Só se for preciso derrubar a coluna (apaga todos os `clinic_facts` salvos), com API e worker já no código antigo e a partir
   de um checkout que ainda tenha `migrations/versions/e5a1c9d3b7f2_tenant_clinic_facts.py`:
   `alembic downgrade c3a9e5f1d7b2` (revisão explícita; `-1` é relativo e erra se houver migração posterior).

## 7. Pendências

- Ingestão dos fatos por site/documento/texto: TASK-026 (esta tarefa não escreve `clinic_facts` por conta própria).
- Parte B: fatia `clinicFacts` e cartão "O que falta?" na tela `/contexto` do Brain-Message-Frontend (contrato da §3) — construída
  e validada no branch `task/TASK-025-clinic-context-ui`, não mesclada; vai por último no deploy (ver o checkpoint dela).
- Aplicar a migração em um Postgres descartável antes do deploy real.
- Merge em `main` (`f7a903e`), push e deploy: feitos em 2026-10-05 (deploy pelo dono). Fica pendente o deploy da correção da §9.

## 8. Limites conhecidos

Conhecidos e aceitos nesta entrega (nenhum é regressão; só aparecem com dado fora do esquema ou por desenho):

- `GET /tenants/me/config` e as respostas do `PUT` falham na validação de resposta (500) se `clinic_facts` ou `address` estiver
  gravado como não-dict, porque `TenantConfigRead.clinic_facts` é tipado `dict | None`. Só se chega nisso escrevendo fora do
  esquema — vira relevante quando a TASK-026 adicionar um segundo escritor.
- Os fatos escalares (`parking`, `how_to_arrive`, `cancellation_policy`, `accessibility`, `notes`) não têm guarda de tipo no
  prompt: um valor que não seja texto é impresso como seu `repr`.
- O checklist (`compute_completeness`) conta `payment_methods` como feito mesmo que o valor gravado seja inutilizável (por
  exemplo, uma string).
- Os máximos do esquema (>12.000 caracteres somados) são maiores que o bloco do prompt (1.800) e nada avisa o gestor do que foi
  cortado; o frontend mostra um aviso de uma linha. Um medidor usado/orçamento exigiria um endpoint novo.
- Um tenant que já tem endereço passa a receber o bloco "SOBRE A CLÍNICA" no deploy (previsto na SPEC §5), sem feature flag. O
  fecho promete "vai confirmar com a equipe" e nada acompanha essa promessa depois (fora de escopo, SPEC "A").

## 9. Correção (2026-10-05): orientações por serviço em clínicas com 2+ profissionais

**Sintoma.** Numa clínica com 2 ou mais profissionais ativos, a LLM respondia "no registro não há orientação" para um serviço
que tinha orientações cadastradas (ex.: "Jejum de 8 horas") e descrição completa. Com exatamente 1 profissional ativo funcionava.
Confirmado em produção e reproduzido localmente. A versão deployada em 2026-10-05 (`main@f7a903e`) tinha essa lacuna.

**Causa raiz.** `get_service_info` e o sufixo " (há orientações: use get_service_info)" do prompt liam só
`TenantRuntimeConfig.appointment_types`. `load_tenant_config` preenche isso com a lista do TENANT e só troca pela lista do
profissional quando há EXATAMENTE UM profissional ativo. O hub grava um serviço novo apenas no catálogo canônico (`services`) e
na lista própria de cada profissional, nunca na lista do tenant. Com 2+ profissionais, um serviço que só existe no catálogo
ficava fora do catálogo efetivo, embora `requirements` e `long_description` estivessem salvos no catálogo.

**Correção (aditiva, sem migração).**

- `TenantRuntimeConfig.service_guides` (lista de `RuntimeServiceGuide`, com padrão vazio — todo construtor antigo continua
  válido), preenchida em `load_tenant_config` a partir do catálogo que ele JÁ carrega (`load_service_catalog(session, tenant.id)`,
  então o isolamento entre clínicas é o mesmo; nenhuma consulta nova): só serviços ATIVOS com `requirements` ou
  `long_description`. As orientações são da clínica inteira, não de um profissional.
- `get_service_info` procura o nome primeiro no catálogo efetivo (resultado idêntico ao de antes) e depois em `service_guides`.
  Serviço só dos guias devolve as mesmas chaves, com `duracao_min` e `preco` vazios (variam por profissional). O erro de
  "serviço desconhecido" mantém o texto e lista a união das duas fontes, sem repetir. O casamento de nome mantém a regra antiga
  (sem diferenciar maiúsculas) e, se não achar, também ignora acento e espaço interno, como a identidade do catálogo
  (`service_catalog.normalize`).
- Prompt: uma linha, "- Serviços com orientações (use get_service_info): X, Y", logo abaixo dos tipos de consulta, só com os
  guias que NÃO estão na lista de tipos (comparação por nome normalizado). Com 1 profissional ou com a lista completa, o prompt
  é byte a byte o de antes (`tests/golden/system_prompt_default.txt` intocado). `_format_appointment_types` e o orçamento de
  1.800 caracteres do bloco "SOBRE A CLÍNICA" não mudaram.

**Deploy da correção.** API + worker (deploys separados; ambos montam o prompt e rodam a ferramenta), **sem migração**: nenhuma
coluna nova, só leitura do catálogo que já existia.

**Validação.** `tests/test_service_guides_multipro.py` (24 testes, escritos antes do código; 23 falharam antes da correção e os 24 passam depois): 2 profissionais com
serviço só no catálogo, 1 profissional (sem mudança), 2 profissionais com o serviço também na lista do tenant (sem linha
duplicada), serviço inativo ou sem orientação (fora dos guias), isolamento entre duas clínicas, erro com a união dos nomes, nome
com maiúsculas/acento, clínica sem nenhum profissional. Suíte completa: 2992 passed, 10 skipped, 0 falhas (base `f7a903e`: 2968).

**Limites conhecidos.** A linha do prompt e a ferramenta cobrem todo serviço ATIVO do catálogo que tenha orientações, mesmo que
nenhum profissional o ofereça no momento; a lista "Tipos de consulta disponíveis" continua sendo a fonte do que se agenda.
Duração e preço de um serviço só do catálogo vêm vazios na ferramenta (a descrição da ferramenta manda a LLM confirmar com a
equipe em vez de inventar).
