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

## 9. Pendências

- Parte 2 (hub frontend) — consome a seção 4.
- Rótulo do botão "Escolher serviço" no menu multi-médico: hoje leva ao mesmo fluxo de "Escolher
  médico". Decisão de copy para o dono (renomear/remover) — não mudei o menu.
- Migração que remove `tenants.insurances` (seção 6).
- Deploy: não autorizado.
