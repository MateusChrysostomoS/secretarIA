# CHECKPOINT — Catálogo canônico de convênios + aceitação por profissional + convênio primeiro no fluxo

TASK-006, parte 1/2 (secretarIA backend). Parte 2 = `secretarIA-frontend`
(`z_prompts/PROMPT_CONVENIO_CATALOGO_ACEITACAO_2_SECRETARIA_FRONTEND.md`), que consome o contrato
da seção 4. Estado: **BUILT, commit local na branch `task/TASK-006-secretaria` (worktree
`C:\TECH\BRAIN-worktrees\TASK-006\secretarIA`), NÃO pushado, NÃO deployado.** A branch está em
cima de `task/TASK-007-secretaria` ("Essa consulta é pra você?"), que também não foi mesclada —
as duas vão juntas, TASK-007 primeiro.

## 1. Decisões de produto (do dono — não reabrir)

1. **Catálogo global**, compartilhado por todos os tenants; a clínica escolhe dentro dele (select),
   nunca digita nome. Substitui o array livre `Tenant.insurances`.
2. **Aceitação por profissional é subconjunto da clínica.** Imposto em duas camadas: validação no
   hub (422) e FK composta no Postgres.
3. **Ordem nova do fluxo determinístico:** pra-quem (TASK-007) → **convênio → profissional (TODOS,
   com marca "✅ Aceita seu convênio" nos que aceitam) → serviço → data → horário.** Nunca esconde
   profissional; só marca.
4. **Sinal Pix × convênio (2026-09-25):** flag `charge_deposit` **por clínica e por plano**
   (default `true` = comportamento de hoje), só em `tenant_insurance_plans`.

## 2. Schema (migração `c9f4e2a7b815_insurance_catalog`, revises `b8e3c5a1d702`)

| Tabela / coluna | O quê |
|---|---|
| `insurance_catalog` | global: `id` (uuid5 do slug — igual em todo ambiente), `slug` único, `name`, `normalized_name` único, `mechanism` (CHECK: `reembolso`\|`coparticipacao`\|`desconto_direto`\|`variavel_por_plano`\|`desconhecido`), `note`, `is_active`, timestamps |
| `tenant_insurance_plans` | `(tenant_id, catalog_id)` único; **`charge_deposit` BOOLEAN NOT NULL DEFAULT true** |
| `professional_insurance_plans` | `(professional_id, catalog_id)` único; **FK composta `(tenant_id, catalog_id)` → `tenant_insurance_plans` ON DELETE CASCADE** (tirar o plano da clínica tira de todo médico) |
| `appointments.insurance_plan_id` | FK nullable → `insurance_catalog.id` ON DELETE SET NULL; gravada 1× na criação da consulta (`resolve_tenant_plan_id`), lida pela guarda do sinal por id |

`Tenant.insurances` (strings) **continua existindo** neste ciclo (ver seção 6).

## 3. Seed do catálogo e proveniência (pesquisa web, acesso 2026-09-26)

Semente inicial: `docs/REFERENCIA_PAGAMENTO_CONVENIOS_2025_2026.md` (dono, 2026-09-25). Refinada com
pesquisa web por operadora. **É metadado ("como essa operadora tipicamente se relaciona com o
pagamento da consulta"), não preço, não regra, não promessa contratual da secretarIA** — nada no
produto filtra ou bloqueia por esses campos. Quando a fonte varia por linha/regional/contrato, o
mecanismo é `variavel_por_plano`; sem fonte clara, `desconhecido`.

| slug | nome | mecanismo | fontes (todas acessadas em 2026-09-26) |
|---|---|---|---|
| alice | Alice | variavel_por_plano | https://blog.alice.com.br/gestora-de-saude/reembolso-plano-saude-como-funciona-alice/ ; https://www.carmelseguros.com.br/duvidas-4366913-plano-saude-alice-oferece-reembolso-consultas.html |
| amil | Amil | variavel_por_plano | https://amigaosaude.com.br/blog/reembolso-amil-black-entenda-os-niveis-r1-r2-e-r3-para-livre-escolha/ ; https://amilplanos.com.br/reembolso-amil/ |
| assim-saude | Assim Saúde | variavel_por_plano | https://saude.zelas.com.br/plano-de-saude/assim-saude ; https://assimplanodesaude.com.br/plano-assim-exclusivo.html |
| bradesco-saude | Bradesco Saúde | variavel_por_plano | https://www.bradescoseguros.com.br/wcm/connect/32785753-dd20-455f-a896-b8556a265e5e/A_F_PDF_Manual+do+Reembolso_260526.pdf ; https://www.bradescosaudeempresas.com.br/blog/reembolso-especifico-vs-completo-bradesco-saude |
| care-plus | Care Plus | variavel_por_plano | https://www.planodesaudecareplus.com.br/duvidas-5556-como-funciona-reembolso-plano-care-plus.html ; https://www.careplus.com.br/a-careplus/perguntas-frequentes |
| cassi | CASSI | variavel_por_plano | https://www.cassi.com.br/index.php?option=com_content&view=article&id=705:livre-escolha-13902237096151&catid=51&Itemid=754&uf=DF ; https://www.cassi.com.br/noticias/quando-cabe-solicitar-ressarcimento-a-cassi-e-como-proceder/ |
| golden-cross | Golden Cross | variavel_por_plano | https://saudebemviver.com.br/guia-reembolso-golden-cross-descubra-os-percentuais/ ; https://saudemaislazer.com.br/reembolso-golden-cross-descubra-o-percentual-atualizado/ |
| hapvida | Hapvida | variavel_por_plano | https://www.gndi.com.br/noticias/veja-as-mudancas-na-coparticipacao-gndi ; https://www.gndi.com.br/beneficiario |
| notredame-intermedica | NotreDame Intermédica | variavel_por_plano | https://www.gndi.com.br/noticias/veja-as-mudancas-na-coparticipacao-gndi ; https://www.gndi.com.br/beneficiario |
| omint | Omint | variavel_por_plano | https://www.omint.com.br/planos-de-saude/planos/planos-omint-premium/ ; https://www.saudeseguros.com/reembolso-omint-como-funciona-quanto-recebe-e-quando-vale-a-pena/ |
| porto-seguro-saude | Porto Seguro Saúde | variavel_por_plano | https://www.portoseguro.com.br/consulta-de-clientes/tabela-reembolso-saude ; https://webplansaude.com/tabela-reembolso-linha-porto-saude |
| prevent-senior | Prevent Senior | **desconhecido** | https://www.reclameaqui.com.br/prevent-senior/pedido-de-reembolso_c-YWAbJqJAJwqx3o/ ; https://convenionow.com/planos-de-saude/prevent-senior/reembolso (fontes conflitam, sem fonte oficial) |
| sulamerica | SulAmérica | variavel_por_plano | https://www.sulamerica.com.br/reembolso/ ; https://www.lifebis.com.br/post/sulamerica-reembolso-tabela-livre-escolha |
| unimed | Unimed | variavel_por_plano | https://www.unimed.coop.br/site/web/riodejaneiro/coparticipacao ; https://www.unimed.coop.br/site/web/riodejaneiro/pedido-reembolso |

Notas curtas de cada linha: `services/insurance_catalog.py::CATALOG_SEED` (cópia congelada na
migração). Hapvida e NotreDame Intermédica são o mesmo grupo mas marcas que o paciente conhece
separadamente — duas linhas, mesma fonte. **Limitações:** a maioria das fontes é site da própria
operadora ou de corretora/blog especializado; tabelas ANS/CREMERJ/APM não foram abertas por
operadora; Prevent Senior sem confirmação oficial. Nenhum valor numérico foi transcrito para o
catálogo. Adicionar operadora = nova migração de dados (o catálogo é global; não há tela para isso).

## 4. Contrato para a parte 2 (hub frontend) — `api/hub/insurance.py`

Auth: a mesma do hub (`get_current_tenant`). Nunca gated por entitlement. Corpos de entrada com
`extra="forbid"` (chave desconhecida → 422).

| Método | Rota | Corpo | Resposta |
|---|---|---|---|
| GET | `/tenants/me/insurance-catalog` | — | `[{id, slug, name, mechanism, note}]` (ativos, por nome) |
| GET | `/tenants/me/insurance-plans` | — | `[{catalog_id, name, charge_deposit}]` |
| PUT | `/tenants/me/insurance-plans` | `{plans: [{catalog_id, charge_deposit?=true}]}` (≤50; **substitui o conjunto inteiro**) | igual ao GET |
| GET | `/tenants/me/professionals/{id}/insurance-plans` | — | `{professional_id, selectable: [{catalog_id, name, charge_deposit}], accepted_catalog_ids: [str]}` |
| PUT | `/tenants/me/professionals/{id}/insurance-plans` | `{catalog_ids: [str]}` (≤50; substitui) | igual ao GET |

Erros 422 (`detail = {code, message, catalog_ids}`): `invalid_catalog_ids` (não é UUID),
`unknown_catalog_ids` (fora do catálogo ativo), `plans_not_enabled_by_clinic` (médico tentando
aceitar plano que a clínica não habilitou — **rejeita, não normaliza**: a UI só oferece
`selectable`, então isso é tela velha ou request forjado e o save deve falhar visivelmente). 404
para profissional de outro tenant/inexistente.

Semântica que a UI precisa respeitar:
- Remover um plano da clínica **remove de todos os médicos** (cascade). Avisar antes de salvar.
- Plano **adicionado pela UI nova** entra com **nenhum** médico aceitando (decisão 2: é escolha do
  médico). Plano adicionado pelo **caminho legado** (seção 6) entra aceito por todos os médicos
  ativos — é o que "a clínica aceita X" significava antes.
- `charge_deposit=false` = "não cobra sinal Pix para esse convênio nesta clínica".
- `collect_insurance` continua no `PUT /tenants/me/configuration` / `/config` como hoje.
- **OBRIGATÓRIO na parte 2: parar de enviar `insurances` no `PUT /tenants/me/config` /
  `/configuration`.** O front atual manda esse campo em TODO save (inclusive edição só de Pix), e
  qualquer `insurances` no payload passa por `sync_legacy_insurances`, que re-deriva o conjunto de
  planos a partir das strings: com um CSV carregado antes de um `PUT /insurance-plans` na mesma
  tela, ele apaga o plano recém-adicionado, ou re-adiciona um antigo concedendo-o a todos os
  médicos (achado do Reviewer, M3). A tela nova edita planos SÓ pelos endpoints desta seção.
- `charge_deposit=false` vale para consultas criadas pelo fluxo de botões (WhatsApp) e pela
  promoção de reserva do Portal. Consulta marcada pelo agente LLM ou manualmente no hub
  (`api/hub/calendar.py`) não grava `insurance_plan_id` e segue a política do tenant. A copy da UI
  deve dizer isso.

## 5. Fluxo (`services/flow_router.py`)

- Pra-quem → `_booking_continuation`: convênio primeiro (`_enter_insurance(tenant)`), se a clínica
  coleta (`collect_insurance` + ao menos 1 plano) E a entrada realmente abre agendamento (não pergunta
  convênio na frente de "não há serviços").
- Resposta (`_handle_insurance`): sem serviço escolhido ainda (caso normal) → `_start_booking(...,
  insurance=)` = lista de TODOS os médicos, marcados via `_professional_rows` (descrição
  `"✅ Aceita seu convênio · <especialidade>"`; corpo ganha `"✅ = aceita <plano>"`); clínica de 1
  médico → lista de serviços. Com serviço já escolhido (conversa parada no meio quando isto subiu,
  ou hand-back da LLM) → dia.
- "Particular" / "Outro convênio" / clínica sem ids (snapshot legado) → sem marca, todos listados.
- "Escolher serviço" (menu multi-médico) agora abre a MESMA sequência de "Escolher médico" — a
  ordem do dono aposenta o serviço-primeiro. Exceção preservada: clínica sem serviço algum continua
  recebendo "não há serviços" + menu. Handlers de `STEP_AWAITING_CATALOG_SERVICE` /
  `STEP_AWAITING_SERVICE_PROFESSIONAL` ficam só para conversas paradas nesses passos no deploy;
  a lista desse último agora mostra todos os médicos, marcados.
- **Clínica com 1 profissional: a tela de profissional continua pulada** (decisão de UX desta
  implementação: lista de um item só acrescenta um toque).
- `_carry_insurance`/`_carry_booking` levam `flow_selected_insurance` adiante em todo resultado que
  continua no agendamento (antes ele só existia depois da escolha do serviço).
- "Sim, agendar" com convênio ainda sem resposta e clínica coletando → pergunta ali (rede de
  segurança para conversas antigas e para o hand-back `select_professional_and_continue`).
- `enter_guided_booking` (hand-back da LLM, só clínica de 1 médico): convênio já respondido → dia;
  não respondido e coletado → convênio (serviço preservado) → dia.
- Snapshot do tenant (`workers/tasks.py::_flow_tenant_snapshot`) ganhou `insurance_plans`
  (`[{"id","name"}]`) e `insurance_accepted_by` (`{prof_id: frozenset(catalog_id)}`), carregados 1×
  por turno (`load_tenant_insurance`). Chamadores que passam `Tenant` cru caem nas strings legadas:
  mesmos nomes, sem marca.
- Nenhuma mudança em `ChannelSender`: marca e corpo são texto de lista, iguais nos dois canais.

## 6. Migração de dados e transição (skill `frozen-contract-migration`)

- **Aditiva.** O backfill casa cada string de `tenants.insurances` com o catálogo (mesmo `normalize`
  do app: trim, colapsa espaço, tira acento, casefold). Casou → `tenant_insurance_plans` + aceito
  por todo profissional **ativo** da clínica. Não casou → **não apaga**; fica em
  `tenants.insurances` e sai no log `insurance_migration_unmatched tenant_id=… unmatched_count=N`.
- SQL para o operador listar candidatos a reclassificação:

  ```sql
  SELECT t.id, t.clinic_name, s.value AS legacy_string
    FROM tenants t, json_array_elements_text(t.insurances) s(value)
   WHERE t.insurances IS NOT NULL
     AND NOT EXISTS (
       SELECT 1 FROM insurance_catalog c
        WHERE c.normalized_name = lower(trim(regexp_replace(s.value, '\s+', ' ', 'g')))
     );
  ```
  (aproximação sem remoção de acento — a comparação exata do app é `normalize`; use o resultado
  como lista de candidatos e resolva via `PUT /tenants/me/insurance-plans`.)
- **String legada sem par no catálogo continua sendo OFERECIDA ao paciente** durante a transição
  (`load_tenant_insurance` a acrescenta depois dos planos do catálogo, com `id=None`): sem isso, uma
  clínica com "GEAP"/"Cabesp" perderia a pergunta de convênio em silêncio (achado do Tester). Sem id
  ela nunca marca médico nem vira `insurance_plan_id`. Uma string que nomeia plano do catálogo que a
  clínica REMOVEU não volta como opção (`_mirror_legacy` só preserva o que não casa com nenhuma
  entrada do catálogo).
- **Caminho legado vivo até a parte 2:** um `PUT /tenants/me/configuration`/`/config` com
  `insurances` (strings) passa por `sync_legacy_insurances` — casa no catálogo, habilita/remove
  planos, concede plano novo a todos os médicos ativos, mantém as strings exatamente como digitadas.
  O caminho novo espelha nomes canônicos em `tenants.insurances` (mantendo strings não casadas).
- **Ordem de deploy:** migração `c9f4e2a7b815` (depois da `b8e3c5a1d702` da TASK-007) → API e worker
  em qualquer ordem (ambos mapeiam `Appointment`; coluna nova nullable, tabelas novas que o código
  velho ignora). O fluxo novo vive no **worker** — deployar só a API não muda a conversa.
- **Remover `tenants.insurances`** = migração futura, só depois de (a) a parte 2 ler apenas o
  catálogo e (b) API **e** worker rodarem código que não mapeia a coluna (provar pelos dois
  `/build` `source_fingerprint`). Até lá, `sync_legacy_insurances` e `_mirror_legacy` ficam.

## 7. Sinal Pix (`services/payments/deposit_lifecycle.py`)

Guarda nova logo após `pix_deposit_enabled`: `_insurance_charges_deposit` lê
`appointment.insurance_plan_id` **por id** → linha `tenant_insurance_plans` do tenant com
`charge_deposit=false` → `None`, log `pix_deposit_skipped_insurance_no_charge`
(`DEPOSIT_SKIP_INSURANCE_NO_CHARGE = "insurance_no_charge"`). Sem id (Particular, Outro convênio,
sem resposta, consultas antigas) ou plano que a clínica não aceita mais → política do tenant, igual
antes. `insurance_plan_id` é gravado nos dois pontos que criam `Appointment` no worker (confirmação
direta e promoção de `booking_hold` do Portal); o agente LLM não grava convênio (como antes).

## 8. Validação (2026-09-26)

- Baseline antes de começar (mesmo worktree, HEAD = TASK-007): **2434 passed, 2 failed**
  (`test_action_buttons.py::test_apptresched_*` — credenciais do Google Calendar ausentes no
  worktree, pré-existente); `ruff check .` com 8 erros pré-existentes.
- Postgres 18 descartável (initdb em diretório temporário, porta 5544): `alembic upgrade` do zero até
  `b8e3c5a1d702`, dados sintéticos legados, `upgrade head`, `downgrade -1`, `upgrade head`. Provado:
  14 linhas de catálogo; strings com caixa/acento/espaço diferentes casaram; string sem par ficou em
  `tenants.insurances` e foi logada; médico inativo não recebeu plano; FK composta recusou médico
  aceitando plano não habilitado; CHECK recusou mecanismo inválido; cascade removeu plano de todos os
  médicos; serviço ORM (`load_tenant_insurance`, `resolve_tenant_plan_id`, `set_*`) rodou contra o
  Postgres.
- Testes novos (Tester independente + correções do Reviewer): `tests/test_convenio_catalogo_flow.py`
  (roteador puro: sintoma "todos listados, os que aceitam marcados, nenhum escondido" pelas duas
  entradas do menu, tabela 32 combinações, Particular/Outro, ordem até o dia, hand-back da LLM,
  testes que provam que as asserções mordem) e `tests/test_convenio_catalogo_db.py` (SQLite + HTTP:
  subconjunto 422, cascade, sync legado, guarda do sinal, `resolve_tenant_plan_id`, snapshot do
  worker → roteador). **84 testes.**
- Final: `uv run python -m pytest` → **2518 passed, 2 failed** (as MESMAS 2 da baseline, mesma
  causa); `uv run ruff check .` → **limpo** (os 8 pré-existentes foram corrigidos: quebra de linha
  em 5, `# noqa: UP042` nos 3 enums `(str, Enum)` — trocar por `StrEnum` mudaria `str(member)`).
- Revisão independente: `tasks/TASK-006/REVIEW.md` (raiz de BRAIN) — 3 MEDIUM + 2 LOW, todos
  tratados (código + teste que morde, ou regra explícita no contrato da seção 4).

## 9. Pendências (TASK-006)

- Parte 2 (hub frontend) — consome a seção 4 **mais** a seção 10 abaixo (TASK-008 substitui parte
  do contrato da seção 4 para o convênio da clínica/profissional — o catálogo global em si e
  `GET /insurance-catalog` não mudaram).
- Rótulo do botão "Escolher serviço" no menu multi-médico: hoje leva ao mesmo fluxo de "Escolher
  médico". Decisão de copy para o dono (renomear/remover) — não mudei o menu.
- Migração que remove `tenants.insurances` (seção 6).
- Deploy: não autorizado.

---

# TASK-008 — Modos de aceitação + catálogo extensível + convênio "Outro"

Branch `task/TASK-008-convenio-integracao` (worktree `C:\TECH\BRAIN-worktrees\TASK-008\secretarIA`),
em cima do commit `7ed2028` (TASK-006, que já inclui TASK-007). **BUILT, commit local, NÃO
pushado, NÃO deployado.** Ver `tasks/TASK-008/SPEC.md` (raiz de BRAIN) para o pedido completo e
`tasks/TASK-008/results/implementer-be.md` para o relatório de entrega.

## 10.1 Schema (migração `d4f8a2c6e913`, revises `c9f4e2a7b815`)

Aditiva, sem valor atribuído a `insurance_mode` para tenant nenhum (nem os já migrados pelo
TASK-006) — decisão explícita do dono, não reabrir.

| Tabela / coluna | O quê |
|---|---|
| `tenants.insurance_mode` | nullable, `CHECK IN ('shared','clinic_with_exceptions','independent')`. `NULL` = não escolhido; enquanto `NULL`, o passo de convênio no chat é pulado (`INSURANCE_SKIP_NO_MODE`, mesmo caminho de catálogo vazio). |
| `insurance_catalog.aliases` | `JSON NOT NULL DEFAULT '[]'` — apelidos extra que também casam com a entrada (`services/insurance_catalog.py::catalog_match_keys`). |
| `insurance_catalog_unmatched` | NOVA. `id`, `tenant_id` (FK cascade), `raw_text`, `normalized_text` (chave de dedupe/match, `UNIQUE(tenant_id, normalized_text)`), `first_seen_at`, `resolved_at` (nullable), `resolved_insurance_catalog_id` (nullable FK). Backfillada na própria migração TASK-008, **re-derivando** de `tenants.insurances` contra o catálogo atual (superset do que a migração TASK-006 apenas logou). |
| `tenant_insurance_plans.catalog_id` | agora nullable; colunas novas `custom_name`, `custom_payment_note`; `CHECK` "exatamente um entre catalog_id/custom_name". |
| `professional_insurance_plans` | **redesenhada** — a FK composta do TASK-006 (`(tenant_id, catalog_id)` → clínica) foi removida: ela impunha "o médico só aceita o que a clínica aceita" de forma incondicional, o que quebra o modo `independent` (médico escolhe do catálogo global, sem precisar que a clínica tenha habilitado). Agora tem TRÊS formas mutuamente exclusivas (`CHECK` soma = 1): `tenant_plan_id` (FK → `tenant_insurance_plans.id`, modos `shared`/`clinic_with_exceptions`, ainda com CASCADE), `catalog_id` (FK simples → `insurance_catalog.id`, modo `independent`), `custom_name`/`custom_payment_note` (Outro do próprio médico). Ganhou `charge_deposit` (só relevante para as duas formas do modo `independent`). |
| `professionals.insurance_plans_customized` | `BOOLEAN NOT NULL DEFAULT false` — distingue "nunca mexeu" (herda tudo, `clinic_with_exceptions`) de "salvou vazio de propósito" (aceita nada); zero linhas em `professional_insurance_plans` não bastava para isso. |
| `appointments.insurance_plan_id` | **FK re-apontada**: antes → `insurance_catalog.id`, agora → `tenant_insurance_plans.id` (a linha da clínica é o único identificador que serve tanto para catálogo quanto para "Outro" da clínica). Migração re-popula por `(tenant_id, catalog_id antigo)`. |
| `appointments.insurance_professional_plan_id` | NOVA, FK → `professional_insurance_plans.id`, usada só quando `insurance_mode == "independent"` (não há linha de clínica para apontar nesse modo). |

Testada em Postgres 18 descartável local (initdb + `pg_ctl` em porta 5544, destruído ao final):
`upgrade` do zero até `c9f4e2a7b815` (TASK-006) com 2 tenants sintéticos (`insurances` com
strings casáveis e não-casáveis) → `upgrade` até `d4f8a2c6e913` — confirmado: nenhum tenant
recebeu `insurance_mode`; `insurance_catalog_unmatched` populada com "GEAP"/"Cabesp"; as duas
`CHECK` constraints (`tenant_insurance_plans`, `professional_insurance_plans`) recusam
both-null e both-set (testado com INSERT direto); `create_catalog_entry` +
`reconcile_unmatched_insurances` (via serviço, contra o Postgres real) resolveram "GEAP" e
deixaram "Cabesp" pendente; `create_tenant_custom_plan` gravou e apareceu em
`load_tenant_insurance`. `downgrade -1` → `upgrade head`: **limpo quando não há dado de
`independent`/custom** (ciclo completo provado); com uma linha `custom_name` presente, o
downgrade falha na hora de tornar `catalog_id NOT NULL` de novo — **documentado no próprio
docstring da migração** como limitação aceita (downgrade é conveniência de dev/teste, nunca
rodado em produção com dado real; a mesma transação falha inteira e reverte sozinha, sem deixar
estado parcial — confirmado via `alembic current` após a falha).

## 10.2 Serviços (`services/insurance_catalog.py`)

- `insurance_mode_configured(tenant)` → `services/tenant_config.py` (usado pelo hub para bloquear
  seções e pelo flow_router para decidir o skip).
- `load_tenant_insurance` fica mode-aware: `independent` monta a união dos
  `professional_insurance_plans` de profissionais ATIVOS (`_load_independent_insurance`);
  `shared`/`clinic_with_exceptions`/`None` mantêm o caminho do TASK-006 (planos da clínica +
  fallback de strings legadas não casadas).
- `set_professional_clinic_plans` (renomeou `set_professional_plans`) — modo
  `clinic_with_exceptions`: substitui o subconjunto por **id da linha da clínica**
  (`tenant_insurance_plans.id`), não mais por catalog_id — necessário porque uma linha "Outro" da
  clínica não tem catalog_id. Sempre marca `insurance_plans_customized = True`, mesmo com lista
  vazia. Rejeita id fora do conjunto da clínica (`NotClinicPlans`).
- `set_professional_independent_plans` — modo `independent`: substitui as escolhas por
  **catalog_id direto** (sem checar contra a clínica). Rejeita catalog_id fora do catálogo ativo
  (`UnknownCatalogIds`).
- `create_tenant_custom_plan` / `create_professional_custom_plan` — convênio "Outro" da clínica /
  do profissional (`independent`).
- `update_tenant_plan_charge_deposit` — toggle de `charge_deposit` por linha (catálogo OU custom),
  já que `set_tenant_plans` (PUT em lote) só mexe nas linhas com `catalog_id` (nunca nas custom).
- `create_catalog_entry` (admin) + `reconcile_unmatched_insurances` + `record_unmatched` —
  catálogo extensível (seção 10.4).
- `resolve_booking_plan_ids(session, tenant_id, insurance, professional_id=None)` — substitui
  `resolve_tenant_plan_id` (mantida como wrapper fino, sem `professional_id`, para quem não tem
  esse contexto). Retorna `(tenant_plan_id, professional_plan_id)`: no modo `independent` resolve
  contra as linhas do PRÓPRIO profissional que fez a reserva; nos demais, contra as linhas da
  clínica — exatamente um dos dois preenchido, ou nenhum se não casou.

## 10.3 `flow_router.py`

- `_insurance_step_skip_reason` ganhou `INSURANCE_SKIP_NO_MODE` (checada depois de
  `collect_insurance`, antes de "catálogo vazio").
- `_accepts_plan` (usada por `_professional_rows`, chamada tanto pelo caminho de botão quanto por
  `enter_guided_booking` — **um helper só**, como pedido no SPEC §4.2): `shared` → todo profissional
  ativo marcado, sem olhar `professional_insurance_plans`; `clinic_with_exceptions` → linha própria
  se `insurance_plans_customized`, senão herda tudo (default do dono); `independent` → só linha
  própria decide, nunca herda; `None` (modo não escolhido, ou snapshot legado sem o atributo) —
  preserva o default ORIGINAL do TASK-006 (nunca aceita por omissão), decisão que **não foi
  reaberta**, só passou a valer apenas para esse caso.
- `_handle_confirmation` (confirmação do agendamento) agora anexa a `custom_payment_note` do plano
  escolhido (se houver) à mensagem de confirmação, prefixada com "💳 Sobre o pagamento do
  convênio:".
- `enter_guided_booking` não precisou de nenhuma mudança de lógica: já chamava
  `_insurance_step_skip_reason`/`_enter_insurance` — os MESMOS pontos que o caminho de botão usa —
  então herdou o comportamento novo automaticamente. Teste dedicado:
  `test_insurance_mode_matrix.py::test_enter_guided_booking_skips_when_mode_is_none`.

## 10.4 `services/payments/deposit_lifecycle.py`

- `_insurance_charges_deposit` passa a checar `appointment.insurance_professional_plan_id`
  PRIMEIRO (modo `independent`, lê `ProfessionalInsurancePlan.charge_deposit`); cai para
  `insurance_plan_id` (agora `TenantInsurancePlan.id`, não mais `catalog_id`) como antes.
- `_deposit_request_text` ganhou parâmetro `payment_note` — repete a explicação de pagamento do
  convênio "Outro" junto do pedido de sinal Pix quando `charge_deposit=true`, buscada por
  `_insurance_payment_note` (nova função, lê `custom_payment_note` de qualquer um dos dois lados).

## 10.5 Contrato do hub — `api/hub/insurance.py` (substitui parte da seção 4)

Auth igual à seção 4 (`get_current_tenant`), nunca gated por entitlement, corpos com
`extra="forbid"`.

| Método | Rota | Corpo | Resposta / erros |
|---|---|---|---|
| GET | `/tenants/me/insurance-mode` | — | `{mode: str \| null}` |
| PUT | `/tenants/me/insurance-mode` | `{mode}` | `{mode}`; 422 `invalid_insurance_mode` |
| GET | `/tenants/me/insurance-catalog` | — | igual à seção 4 (inalterado) |
| GET | `/tenants/me/insurance-plans` | — | `[{id, catalog_id\|null, name, is_custom, custom_payment_note\|null, charge_deposit}]` |
| PUT | `/tenants/me/insurance-plans` | `{plans: [{catalog_id, charge_deposit?=true}]}` (≤50) | mesma forma do GET; **só mexe nas linhas com catalog_id** — nunca nas "Outro" |
| POST | `/tenants/me/insurance-plans/custom` | `{custom_name, custom_payment_note, charge_deposit?=true}` | 201, forma do GET; 409 `insurance_mode_not_applicable` se `independent`/sem modo |
| PATCH | `/tenants/me/insurance-plans/{plan_id}` | `{charge_deposit}` | forma do GET; 404 se o id não é da clínica |
| GET | `/tenants/me/professionals/{id}/insurance-plans` | — | `{professional_id, mode, selectable, accepted_plan_ids, inherits_clinic}` — ver semântica abaixo |
| PUT | `/tenants/me/professionals/{id}/insurance-plans` | `{plan_ids}` (clinic_with_exceptions) OU `{catalog_ids}` (independent) | mesma forma do GET; 422 `wrong_field_for_mode` se enviar o campo errado para o modo atual |
| POST | `/tenants/me/professionals/{id}/insurance-plans/custom` | `{custom_name, custom_payment_note, charge_deposit?=true}` | 201, forma do GET; 409 se modo != `independent` |

**GET/PUT professional insurance-plans por modo:**
- `mode is None` ou `"shared"` → **409** `insurance_mode_not_applicable` (não há seção nesses
  casos — a UI nem deve chamar).
- `"clinic_with_exceptions"` → `selectable` = os planos da clínica (mesma forma do
  `GET /insurance-plans`, chave é `id`, não `catalog_id`); `accepted_plan_ids` ⊆ `selectable`;
  `inherits_clinic=true` quando o profissional nunca customizou (nesse caso
  `accepted_plan_ids == [todos os ids de selectable]`, para a UI já nascer com tudo marcado).
- `"independent"` → `selectable` = catálogo global inteiro + os próprios "Outro" do profissional
  (mesma forma do `GET /insurance-plans`, mas SEM ligação com a lista da clínica);
  `accepted_plan_ids` são os próprios catalog_ids + ids das próprias linhas custom;
  `inherits_clinic` sempre `false`.

**Erros 422 (`detail = {code, message, catalog_ids}`)** — nome do campo `catalog_ids` mantido do
TASK-006 por estabilidade de contrato, mas em `clinic_with_exceptions` os valores dentro dele são
**ids de linha da clínica** (`tenant_insurance_plans.id`), não catalog ids — divergência
deliberada, documentada aqui para o time de frontend não se confundir: `plans_not_enabled_by_clinic`
(médico tentando aceitar plano fora do conjunto da clínica), `unknown_catalog_ids` (id fora do
catálogo ativo, PUT da clínica ou modo independent), `invalid_catalog_ids` (não é UUID).

**Semântica que a UI precisa respeitar (adicional à seção 4):**
- Trocar de modo NÃO apaga dado do modo anterior — só para de ser lido. Não há tela de "limpar"
  dado órfão; ficou registrado como pendência (10.7).
- Remover um plano "Outro" da clínica: **não existe endpoint de delete** nesta rodada — só criar e
  alternar `charge_deposit`. Pendência (10.7).
- Um médico do modo `clinic_with_exceptions` que nunca mexeu aparece com tudo marcado
  (`inherits_clinic=true`); ao salvar QUALQUER seleção (mesmo idêntica ao herdado), ele passa a
  "customizado" e não volta a herdar sozinho.

## 10.6 Admin (SaaS-owner) — `api/admin/insurance.py` (NOVO)

Guard: `require_admin` (mesmo `X-Admin-Token` dos outros endpoints admin).

| Método | Rota | Corpo | Resposta |
|---|---|---|---|
| POST | `/admin/insurance-catalog` | `{name, aliases?=[], mechanism?="desconhecido", note?}` | 201 `{id, slug, name, aliases, mechanism, note, is_active, reconciled_count}`; 409 `duplicate_catalog_entry` se o nome normalizado já existe; 422 `invalid_mechanism` |
| GET | `/admin/insurance-catalog/unmatched` | — | `[{tenant_id, raw_text, first_seen_at}]` — só visibilidade, nunca altera nada |

`reconciled_count` é o número de tenants cujo texto legado casou com a entrada nova (ou um dos
`aliases`) e ganhou uma linha em `tenant_insurance_plans` na mesma transação da criação — **sem
deploy de código**, exatamente o pedido do dono (SPEC §3.2).

## 10.7 `PUT /tenants/me/config` — `insurances` removido

`insurances` saiu de `TenantConfigUpdate`/`TenantConfigRead` (`schemas/config.py`) e de
`TENANT_SCALAR_FIELDS`/`apply_tenant_config`/`tenant_read_model`
(`services/hub_configuration.py`) — `sync_legacy_insurances` (`services/insurance_catalog.py`)
ficou **órfã**, sem nenhum caminho HTTP que a chame; mantida (não apagada) só para eventual
tooling futuro, não reconectar sem reler por que foi removida.

Como `TenantConfigUpdate` nunca teve `extra="forbid"` (só o envelope `HubConfigurationUpdate`
tem — ver o comentário de FIX 34 no próprio arquivo), enviar `insurances` no PUT **não dá erro**:
o campo é silenciosamente ignorado, igual a `greeting_buttons`. Teste de regressão explícito nos
dois endpoints (`PUT /config` puro e via serviço): `tests/test_convenio_catalogo_db.py::test_hub_config_put_no_longer_accepts_insurances`,
`::test_http_put_config_with_insurances_field_is_ignored`, `tests/test_hub_config.py::test_put_insurances_field_is_silently_ignored`.

## 10.8 Decisões de implementação não 100% fechadas no SPEC (registradas aqui)

1. **`appointments.insurance_plan_id` mudou de alvo** (era `insurance_catalog.id`, agora
   `tenant_insurance_plans.id`) em vez de criar uma terceira coluna. Necessário porque uma linha
   "Outro" da clínica não tem catalog_id — sem isso, o convênio "Outro" nunca poderia ser
   registrado no agendamento nem alimentar a guarda do sinal. Migração re-popula por join;
   `resolve_tenant_plan_id` (nome antigo) virou wrapper de `resolve_booking_plan_ids`.
2. **`professional_insurance_plans` perdeu a FK composta** do TASK-006 — ver 10.1. O subconjunto
   de `clinic_with_exceptions` passa a ser 100% aplicação (a mesma garantia de UX que já existia
   via 422; o Postgres agora só garante "essa linha existe e pertence a uma clínica", não mais "é
   subconjunto").
3. **Nome do campo no erro 422** (`catalog_ids`) mantido por estabilidade, mesmo carregando ids de
   linha (não catalog ids) em `clinic_with_exceptions` — ver 10.5.
4. **Sem endpoint de exclusão** de convênio "Outro" (clínica ou profissional) — só criar e
   alternar `charge_deposit`. Fora do escopo explícito do SPEC (só citava "criar").
5. **Trocar de `insurance_mode` não limpa dado do modo anterior.** Decisão deliberada (menor
   risco de apagar histórico sem querer) — pendência de limpeza futura, não bloqueante.
6. **Catálogo de `independent` não tem paginação** — `GET .../insurance-plans` nesse modo devolve
   o catálogo global inteiro (14 linhas hoje); aceitável no tamanho atual, revisitar se o catálogo
   crescer muito via o endpoint admin.
7. **`professional_insurance_plans.tenant_plan_id` não tem tenant-scoping no banco** (achado do
   Tester, confirmado por INSERT direto): a FK aponta só para `tenant_insurance_plans.id`, sem
   checar que aquela linha pertence ao mesmo tenant do profissional — em teoria um bug de código
   poderia gravar a linha de convênio de OUTRA clínica. Nenhum caminho de código hoje faz isso (a
   API sempre resolve `tenant_plan_id` a partir do próprio tenant autenticado), mas é uma garantia
   que só existe em aplicação, não em banco. Registrado como risco latente de isolamento
   multi-tenant.
8. **Correção ao item de downgrade em 10.1**: o gatilho documentado ali ("falha com dado
   `independent`/custom") é mais estreito que a realidade (achado do Tester) — uma customização
   comum de `clinic_with_exceptions` (profissional que restringiu o próprio subconjunto) TAMBÉM
   deixa `catalog_id` NULL em alguma linha e quebra o mesmo `downgrade -1`, não só dado
   independente/custom. Não muda o risco (downgrade é conveniência de dev/teste, nunca roda contra
   dado real), só a precisão da nota.

## 10.9 Pendências (TASK-008)

- Frontend (`secretarIA-frontend`) — consome as seções 10.5/10.6, ainda não iniciado nesta rodada
  (é o Implementer-FE, tarefa separada em `tasks/TASK-008/TASK.md`).
- Endpoint de exclusão de convênio "Outro" (10.8.4).
- Limpeza de dado órfão ao trocar de modo (10.8.5).
- Migração que remove `tenants.insurances` — segue não feita (mesma pendência do TASK-006, agora
  também bloqueada por `insurance_catalog_unmatched` ainda referenciar textos legados).
- Deploy: **NÃO AUTORIZADO**, como sempre. Ver 10.10 antes de autorizar — tem uma janela de
  quebra real se a ordem não for respeitada.

## 10.10 Ordem de deploy obrigatória (achado do Reviewer, severidade HIGH — ler antes de autorizar)

Esta migração e este código **precisam subir junto com o TASK-006** (que ainda não foi deployado
nenhuma vez) — não é só "compatibilidade de leitura" como uma nota anterior sugeria, é também um
risco de ESCRITA:

- `appointments.insurance_plan_id` teve a FK re-apontada de `insurance_catalog.id` para
  `tenant_insurance_plans.id` (10.1). Quem escreve essa coluna é o **worker**
  (`src/secretaria/workers/tasks.py`), serviço deployado separadamente da API
  (`secretarIA/CLAUDE.md`, seção "Deploy — DOIS serviços").
- Se a migração rodar (banco já com a FK nova) ANTES do worker ser redeployado com o código
  novo, o worker antigo ainda resolve um valor no formato antigo (id de catálogo) para essa
  coluna — a inserção falha com `IntegrityError` de FK em **todo agendamento com convênio**
  durante essa janela, não é um caso raro.
- Regra de deploy, quando for autorizado (não agora): migração → API → worker, na mesma janela,
  sem intervalo onde o worker antigo processe um webhook com convênio contra o schema novo.
  Mesma disciplina que `docs/CHECKPOINT_pix_deposit.md` e outras migrações aditivas deste repo já
  exigem — nada novo em espécie, só reforçando porque aqui o efeito de pular a ordem é um
  `IntegrityError` visível, não um bug silencioso.


---

# TASK-014 — Convênio e sinal na agenda (leitura)

Branch `task/TASK-014-convenio-agenda` (worktree `C:\TECH\BRAIN-worktrees\TASK-014\secretarIA`), em
cima de `main`. **BUILT, commit local, NÃO pushado, NÃO deployado, SEM migração.** Spec:
`Brain-Message-Frontend/docs/superpowers/specs/2026-09-29-anamneses-compra-convenios-design.md` §8;
plano: `Brain-Message-Frontend/docs/superpowers/plans/2026-09-29-task-e-convenio-agenda.md`.

## 11.1 Contrato

`GET /tenants/me/calendar/events?start=&end=` (auth do hub, `get_current_tenant`) devolve, por evento,
TRÊS campos novos, todos opcionais e aditivos (consumidor que não os conhece os ignora):

| Campo | Tipo | Significado |
|---|---|---|
| `insurance` | `string \| null` | Rótulo que o paciente escolheu/digitou (`appointments.insurance`), sem espaços nas pontas. `null` quando vazio, em branco ou quando o evento não tem `Appointment` local (evento digitado direto no Google, bloqueio). |
| `insurance_plan` | `{id, name, charge_deposit} \| null` | A linha de plano a que o rótulo resolveu na criação da consulta. Exatamente 3 chaves. |
| `deposit` | `{status, amount_cents} \| null` | A linha de `pix_deposits` da consulta (1 por consulta). `status` é o VALOR de `PixDepositStatus`: `aguardando_sinal`, `confirmado_pago`, `cancelado_reembolsado`, `cancelado_retido`, `no_show_retido`, `expirado`. Exatamente 2 chaves. `null` = a consulta não tem linha de sinal. |

- `insurance_plan.id` é a linha usada: `tenant_insurance_plans.id` (modos `shared` /
  `clinic_with_exceptions`) ou, no modo `independent`, `professional_insurance_plans.id`.
- `insurance_plan.name` é o nome ATUAL da linha (nome do catálogo, ou `custom_name` do "Outro");
  o rótulo antigo continua em `insurance`.
- `charge_deposit` é a política do plano (`false` só quando a coluna é literalmente falsa). NÃO prova
  que um Pix foi cobrado: isso também depende do add-on Pix, de preço legível e de chave Asaas
  (`deposit_lifecycle.maybe_create_deposit`). O frontend redige o texto de acordo.
- `insurance_plan = null` quando: "Particular", "Outro convênio" digitado, consulta anterior ao
  catálogo, plano removido depois (FK `SET NULL`), ou id que não pertence à clínica autenticada.
- Nunca vão para o fio: `custom_payment_note`, `mechanism`, `note`, slug do catálogo; do sinal, `pix_copy_paste`, `asaas_payment_id`, `patient_id`, `refunded_amount_cents` e datas.
- `deposit.status` é o ESTADO DO DINHEIRO, não da consulta. Hoje (verificado em `main` 676814a): a consulta nasce `SCHEDULED` e o paciente já recebe "agendamento confirmado" na reserva (`flow_router.py:3471`); o sinal é criado depois, num hook best-effort (`maybe_create_deposit`); o pagamento só muda `pix_deposits.status` (`apply_asaas_event`), NÃO `Appointment.status`; a falta de pagamento só cancela a consulta quando o webhook do Asaas manda `PAYMENT_OVERDUE`/`PAYMENT_DELETED`. Quem consome NÃO pode ler `confirmado_pago` como "consulta confirmada".
- **Compatibilidade com a TASK F** (spec `Brain-Message-Frontend/docs/superpowers/specs/2026-09-29-task-f-sinal-pix-confirmacao-design.md`): `status` continua sendo o valor de `pix_deposits.status` (os seis valores não mudam; a F acrescenta o eixo `fulfillment_status` e `appointment_id` NULLABLE, e o eixo NÃO vai ao fio). A TASK-017 acrescenta a 3ª chave aditiva `needs_attention: bool` (spec F §10A.5) e atualiza o teste de "2 chaves"; consumidor deve ignorar chave que não conhece e ocultar `status` que não conhece. Cobrança sem consulta (hold do modo `before`) nunca aparece em `deposit`; vai para uma lista separada `awaiting_payment` da F. Sem estorno automático (D15): `cancelado_reembolsado` continua existindo em dados antigos e, depois da F, só é gravado quando a clínica marca o item como devolvido no painel do provedor.
- Símbolos protegidos contra a reestruturação da F1 (TASK-016): `deposit_lifecycle.AppointmentDepositView`, `deposit_lifecycle.load_deposit_views`, `insurance_catalog.AppointmentPlan`/`AppointmentPlanLookup`/`load_appointment_plans` e o teste `tests/test_calendar_events_insurance.py`.
- `deposit = null` cobre casos que o backend não distingue: sem add-on/`pix_deposit_enabled`, convênio com `charge_deposit=false`, sem chave Asaas, preço ilegível, falha do Asaas, agendamento manual pela agenda (não enfileira o hook) e evento só do Google. O consumidor não deve inventar explicação para o `null`.

## 11.2 Como resolve

Mesma precedência do guarda do sinal (`deposit_lifecycle._insurance_charges_deposit`):
`insurance_professional_plan_id` decide sozinho quando preenchido; senão `insurance_plan_id`. Uma
linha de profissional que aponta para linha da clínica (`tenant_plan_id`, sem nome próprio) não
resolve. Tudo em `services/insurance_catalog.py::load_appointment_plans`: no máximo 1 query em
`tenant_insurance_plans` e 1 em `professional_insurance_plans` por página de eventos, ambas com
`tenant_id`; zero query quando nenhum evento tem plano. O sinal vem de
`deposit_lifecycle.load_deposit_views`: 1 query em `pix_deposits` (só `appointment_id`, `status`,
`amount_cents`), com `tenant_id` no `WHERE`; zero query quando nenhum evento tem `Appointment` local.
Nenhuma função que crie, expire ou mude sinal/consulta foi editada. `AppointmentRead` (respostas de escrita)
não mudou.

## 11.3 Testes

`tests/test_calendar_events_insurance.py`: schema (8 chaves, plano com 3, sinal com 2), serviço de plano (catálogo,
"Outro", `independent`, id de outra clínica, precedência, zero query, 1 query por tabela), serviço de sinal (os 6
status reais, ausente, de outra clínica, zero query, `None` nos ids, 1 query, sem a coluna do Pix) e HTTP (os dois
caminhos, nota de pagamento nunca no fio, rótulo em branco, evento só-Google, id forjado, consulta de
outra clínica, plano removido/renomeado, os 6 status de sinal no fio, sem vazar Pix/Asaas, sinal de outra
clínica, página mista sem N+1). Os testes de `/events` anteriores
passam sem edição.

## 11.4 Deploy (NÃO autorizado)

Sem migração nova, mas a rota agora SELECIONA `insurance_plan_id` e `insurance_professional_plan_id` e LÊ
`pix_deposits`: um banco sem a migração `d4f8a2c6e913` (ou sem `b06ff85998bf`, a do sinal) responderia 500 em `/events`. Vale a ordem da seção 10.10
(migração → API → worker, junto com TASK-006/008). O worker não muda com esta tarefa. O
Brain-Message-Frontend novo é seguro contra a API antiga (campos ausentes = nenhuma linha).

## 11.5 Fora deste contrato (decisão do dono, 2026-09-29)

"A consulta deve ser marcada e confirmada apenas após o pagamento do sinal" NÃO está implementada e NÃO faz parte
desta tarefa: é a TASK F (spec aprovado `Brain-Message-Frontend/docs/superpowers/specs/2026-09-29-task-f-sinal-pix-confirmacao-design.md`,
TASK-016 a TASK-019; abordagem A: hold + cobrança, SEM status novo de consulta, modo `before` opt-in por clínica).
A evidência do que o código faz hoje está na seção "Fora deste plano" de
`Brain-Message-Frontend/docs/superpowers/plans/2026-09-29-task-e-convenio-agenda.md`. Quando a F for executada,
este contrato ganha `deposit.needs_attention` (aditivo) e uma lista separada `awaiting_payment`; `deposit.status` e
`amount_cents` continuam válidos. Itens de atenção (pagamento pago sem consulta) aparecem só na tela inicial do Chat,
nunca na agenda.

Validation (2026-09-29): 44 new tests passed; full suite 2577 passed, 2 failed (same Google Calendar credential baseline: 2533 passed, 2 failed). Ruff check clean. Lifecycle diff: 39 added, 0 removed. Graphify update/diagnose: 8351 nodes, 19927 edges, zero duplicates/dangling endpoints, one incidental self-loop.
