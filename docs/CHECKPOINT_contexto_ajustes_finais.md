# CHECKPOINT — Ajustes finais da tela /contexto (TASK-090) — lado secretarIA

Spec: `C:\TECH\BRAIN\tasks\TASK-090\SPEC.md` · Plano: `C:\TECH\BRAIN\tasks\TASK-090\PLAN.md` · Registro: `C:\TECH\BRAIN\tasks\TASK-090\TASK.md` · Prova no navegador: `C:\TECH\BRAIN\tasks\TASK-090\evidence\REPORT.md` · Par no front: `Brain-Message-Frontend/docs/CHECKPOINT_contexto_ajustes_finais.md`.

## Estado (2026-11-22)

**Só LOCAL.** Branch `task/TASK-090-secretaria` (worktree `C:\TECH\BRAIN-worktrees\TASK-090\secretarIA`), base `origin/main` = `db86422`; código final em `d2ba325` (+ 1 commit só de documentação por cima). **NÃO foi mesclado em `main`, NÃO houve push, NÃO foi deployado.** Deploy e push só a pedido do dono. brain-api não foi tocado (entitlements/cobrança de unidades ficam fora do escopo).

## Deploy — ORDEM PARA O DONO (migrações à mão, nunca no boot)

A cadeia tem 1 head só. Três revisões **aditivas** e uma **destrutiva** (o DROP) que tem de ser a última:

1. `alembic upgrade a3c9e5d7f1b4` (a partir do head de `origin/main`, `f9a1d7c4e2b6`). Aditivas e idempotentes:
   - `e1a7c3b9d5f2` — colunas das partes do endereço em `units` (`cep`, `street`, `number`, `complement`, `neighborhood`, `city`, `state`).
   - `f2b8d4c6e0a3` — backfill da unidade "Sede" (só clínica com `tenants.address` e **zero** unidades) + **zera `tenants.paid_notices_auto_approved` para `false`**.
   - `a3c9e5d7f1b4` — tabela `insurance_plan_order` (ordem única da clínica no modo "cada profissional").
2. Deploy da **secretarIA API**.
3. Deploy do **worker** (API e worker são dois serviços; deployar só a API não move o worker).
4. Deploy do **front** (Brain-Message-Frontend; o `nginx.conf` ganhou `/api/cep/` e o resolver DNS embutido do Docker `127.0.0.11`).
5. **POR ÚLTIMO** `alembic upgrade head` = `b4d0f6e8a2c5`, que faz `DROP` de `custom_payment_note` em `tenant_insurance_plans` e `professional_insurance_plans`.

**Nunca** rode `alembic upgrade head` antes de o código novo estar no ar: o código antigo ainda seleciona a coluna e quebraria (`UndefinedColumn`). O docstring da revisão `b4d0f6e8a2c5` repete essa ordem. O DROP é **irreversível**: textos de pagamento já salvos se perdem (decisão do dono); o `downgrade` recria as colunas vazias.

Efeito esperado em produção ao ligar: clínica com convênio cadastrado e "Coletar convênio" desligado passa a ser perguntada; clínica de teste com aviso pago "ligado" volta a ser "desligado". Pré-lançamento — só afeta clínicas de teste.

## O que entrou onde (por item do spec)

| Item | Onde (âncoras estáveis) |
|---|---|
| Unidades sempre disponíveis (§4.7) | `api/hub/units.py` e `api/hub/professional_units.py` sem o 403 `multi_unit_not_entitled`, sem o 409 `unit_limit_reached`, sem o 503 de falha de entitlement. `services/entitlements_client.py`: `CORE_ADDON_IDS = {"multi_unit"}` (ativo para qualquer clínica ativa; a chave segue em `ADDON_IDS`, espelho do brain-api). Condição "≥1 unidade ativa": `PluginSpec.requires_active_units` (`plugins/base.py`), `registry.agent_tools_for(summary, has_active_units=...)`, `services.professional_units.has_active_units`, lido uma vez por turno em `workers/orchestrator.py`. Clínica sem unidade fica com o toolset/prompt idênticos ao legado (testes `test_ai_toolset_v2`, `test_agent_tool_enforcement`). |
| Endereço estruturado da unidade (C1) | `services/unit_address.py` (`UnitAddressParts`, `format_unit_address`, `BR_STATE_NAMES`, `BR_STATE_PREPOSITIONS`); migração `e1a7c3b9d5f2`. `address` de entrada é ignorado; o de saída é derivado no servidor. |
| Saudação por região | `services/clinic_region.py` (`region_of_units`, `load_clinic_region`) + `greeting_template.py` (`render_greeting`, `clinic_description_budget`, `_names_place`); chamadas em `workers/shared/greeting.py::greeting_place`, `workers/portal/open.py`, `workers/turn_router.py`, `workers/shared/debug_reset.py`, `workers/whatsapp/remove_context.py`, `workers/shared/context_ingestion.py`, `services/hub_configuration.py` (prévia e orçamento). Sem unidade com cidade, cai no `tenant_city(tenant.address)` legado. |
| Backfill "Sede" + zerar avisos pagos | migração `f2b8d4c6e0a3` (idempotente; separa o número do fim de `line` quando consegue; cria a Sede mesmo incompleta; limita cada parte ao tamanho da coluna). |
| "Coletar convênio" imutável (§4.2) | `services/flow_router.py::_insurance_step_skip_reason` (só `NO_MODE` e `EMPTY_CATALOG`; `INSURANCE_SKIP_DISABLED` saiu); `workers/shared/greeting.py` e `services/hub_configuration.py` deixaram de depender do campo. Coluna `tenants.collect_insurance` **fica** (rollback seguro); a API aceita e ignora o campo; a leitura devolve a constante `collect_insurance=True`. |
| Sem texto de pagamento do convênio "Outro" (§4.1) | `models/insurance.py`, `schemas/insurance.py`, `api/hub/insurance.py`, `services/insurance_catalog.py` sem `custom_payment_note`; bloco "Sobre o pagamento do convênio" removido de `services/flow_router.py` (confirmação) e `services/payments/deposit_lifecycle.py` (`_insurance_payment_note` apagada) e do plumbing `workers/shared/deposit_request.py`/`deposit_turn.py`; `services/patient_insurance_overview.py` mantém a forma do fio mas devolve `plans` sempre vazio. Coluna removida só pela migração `b4d0f6e8a2c5`. |
| Motivo novo de chamada humana (C4) | `ai/tools.py::HANDOFF_REASONS` (+ `insurance_payment_question`, transfere direto, sem oferta Sim/Não); `services/handoff_notification.py::_REASON_TEXT`; regra no prompt v1 (`ai/prompts.py::INSURANCE_PAYMENT_HANDOFF_RULE`, `INSURANCE_PAYMENT_HANDOFF`) e no bloco RECURSOS (v1 e v2). |
| Excluir serviço (C2) | `api/hub/services.py` (`DELETE /tenants/me/services/{id}`, `require_clinic_manager`) + `services/service_catalog.py::delete_service_everywhere` (limpa `Professional.appointment_types` e `tenants.appointment_types` casando por `service_id` OU nome normalizado via `match_service`; apaga explicitamente `professional_service_unit`, `..._terms`, exceções da matriz; reempacota `sort_order`; convênios intocados; histórico de consultas intacto porque guarda só o texto). |
| Ordem única da clínica (C3) | `models/insurance_plan_order.py`, `services/insurance_plan_order.py` (`apply_subset_order`, `save_subset_order`, `clinic_order_sort_key`; chave `catalog:<uuid>` / `custom:<uuid>`), `services/insurance_catalog.py::_load_independent_insurance` (lista do paciente respeita a ordem), rota `PUT /tenants/me/professionals/{id}/insurance-acceptance/plan-order` em `api/hub/insurance_acceptance.py`. Modos `shared`/`clinic_with_exceptions` seguem no `PUT /tenants/me/insurance-plans/order` existente. |
| Lembretes/avisos pagos (§4.4, lado servidor) | `services/hub_configuration.py::REVOKE_ONLY_TENANT_FIELDS` + `tenant_scalar_updates`: um Salvar só consegue **desligar** `paid_notices_auto_approved`; um `true` vindo do cliente é ignorado (200). A leitura (`TenantConfigRead`, `services/staff_patient_message.py`) continua honrando um `true` armazenado. |
| Completude "address" | `services/context_completeness.py` e `api/hub/context_completeness.py`: "endereço" fica satisfeito por unidade ativa com rua+cidade OU `tenants.address` legado; dicas "Complete o endereço da unidade." / "Cadastre uma unidade com pelo menos a rua e a cidade." |

## Decisões

**Do dono (SPEC §3, fechadas):** pergunta do convênio imutável; texto de pagamento do "Outro" removido inclusive do banco e a secretária chama uma pessoa por e-mail; "Não perguntar novamente" desabilitado; cidade → estado → "o Brasil" na saudação; excluir serviço de verdade só para gestor, sem apagar o convênio; uma só ordem de convênios para a clínica; unidade não é add-on e sem limite; endereço da unidade estruturado, CEP primeiro, País fixo Brasil.

**Do autor (SPEC §7 e escolhas de construção; o dono pode vetar):**
1. Avisos pagos salvos como `true` viram `false` na migração (e o servidor só aceita desligar; ver "Lembretes" acima).
2. Unidade "Sede" criada do endereço antigo, mesmo incompleta, marcada para completar.
3. Consciência de unidade mantém a condição "tem ≥1 unidade ativa"; só caiu o gate de entitlement.
4. 422 aceito para o front antigo na janela de deploy; entradas desconhecidas (`custom_payment_note`, `collect_insurance`, `address` de unidade) são ignoradas, não rejeitadas.
5. Excluir serviço casa por id OU nome normalizado e limpa também a lista legada do tenant.
6. Ordem única da clínica: reordenar o subconjunto que um médico vê preserva a posição dos planos que ele não vê; plano nunca posicionado segue alfabético depois dos posicionados.
7. Preposições dos estados: `BR_STATE_PREPOSITIONS` (27 UFs) — cidade "em", estado "em/no/na" conforme o nome ("na Bahia", "no Paraná", "em São Paulo"), país "no Brasil".
8. Nome do lugar já citado na descrição é detectado por palavra inteira (`_names_place`); para uma única palavra acentuada ("Pará") exige o acento.
9. Teto do prompt v2 subiu para 12.700 caracteres (B6); a regra curta da chamada humana não entrou na linha de `request_human_handoff` do v2 (vem do bloco RECURSOS e da descrição da tool).
10. Rota de ordem do médico aceita qualquer papel da equipe, como as demais rotas da matriz.

## Contratos como foram implementados (formas reais)

- **C1 `/tenants/me/units`**: `UnitCreate`/`UnitUpdate` com `cep` (máscara aceita; guarda 8 dígitos), `street`, `number`, `complement`, `neighborhood`, `city`, `state` (27 UFs). `UnitRead` devolve as 7 partes + `address` derivado (`"{rua}, {número}[ - {complemento}], {bairro}, {cidade} - {UF}, {CEP 00000-000}"`) + `address_incomplete`. Criar, ou PATCH que mande qualquer parte, exige CEP, rua, número, cidade e UF válidos, senão **422** `{"detail": {"code": "unit_address_invalid", "message": str, "fields": [...]}}` — `fields` pode incluir `complement` e `neighborhood` (partes longas demais). PATCH só de `name`/`is_active` não valida (unidade legada continua editável).
- **C2**: `DELETE /tenants/me/services/{id}` → **204**; 403 se não gestor; **404** com `detail` em texto simples `"service_not_found"` (também para id malformado, outra clínica ou corrida já apagada).
- **C3**: `PUT .../professionals/{professional_id}/insurance-acceptance/plan-order`, corpo `{"plan_ids": [str, ...]}` (subconjunto permitido) → **200** com a matriz do profissional na nova ordem; **404** `detail` texto `"professional_not_found"` / `"plan_not_found"` (inclui id que não é UUID; nada é gravado); **409** `detail` objeto `{"code": "insurance_mode_not_applicable", "message", "mode"}` fora do modo `independent`.
- **C4**: `request_human_handoff(reason="insurance_payment_question")`; e-mail `human_handoff_alert` com o texto "O paciente perguntou sobre a regra de pagamento do convênio." e nada do que o paciente digitou.
- **C5**: a API aceita e ignora `collect_insurance` e `custom_payment_note`; `address` da clínica segue legado (a coluna fica).

## O que foi validado

- **Suíte completa do secretarIA na branch integrada**: ver relatório do integrador: 2 falhas antigas conhecidas / 10533 passed @5f25c3d (`BOT_ALLOWLIST_WA_IDS=""`, sem chave de IA; as 2 falhas são `tests/test_service_change_agenda.py` e também falham em `db86422`). A onda de correções (`5f25c3d..d2ba325`) foi coberta por 518 testes focados verdes; uma reexecução da suíte completa sobre `d2ba325` estava em andamento ao fechar este checkpoint, sem resultado ainda — confirmar antes de mesclar.
- **Migrações em Postgres 18 descartável**: cadeia completa vazia → `upgrade head` → `downgrade -1` → `upgrade head`; dados semeados (clínica com endereço legado e zero unidades, clínica sem endereço, planos "Outro" com nota) → `a3c9e5d7f1b4` cria a Sede, zera o aviso pago, mantém as notas → app funciona nessa revisão → `b4d0f6e8a2c5` derruba a coluna sem `UndefinedColumn` com API e worker no ar.
- **Prova no navegador** (clínica local sintética; 9 PASS, 1 PARTIAL): resumo e links em `C:\TECH\BRAIN\tasks\TASK-090\evidence\REPORT.md`. Foi feita em `5f25c3d`; as correções finais não repetiram a prova visual (só testes + `nginx -t` + proxy real).
- Lint: `ruff check` limpo nos arquivos tocados, exceto um E501 antigo em `schemas/insurance_acceptance.py` (linha 1). Nunca `ruff format .`.

## O que NÃO foi provado

- Que o **modelo real** escolhe a tool de chamada humana (e não explica a regra do convênio): a prova usou um stub no lugar da decisão do modelo; depois dela tudo é código do produto. Rodar o eval com o modelo antes do deploy.
- Produção, WhatsApp real (sem credencial) e Easypanel: nada tocado.
- `tests/postgres/*` e `tests/llm_eval/*` não foram executados (a suíte os pula).
- "Remarcar com aviso pago" não foi exercitado no navegador.

## Pendências conhecidas (follow-ups)

- Remover juntas, em BMF + secretarIA + brain-api, a folha morta do convênio no Portal do paciente e os campos `has_clinic_notes` / `custom_note` (hoje constantes vazios mantidos só para a janela de deploy).
- O prompt v1 repete a regra da chamada humana 3 vezes (passo 3, estado da conversa e RECURSOS); enxugar.
- A rota de ordem do médico aceita qualquer papel da equipe, como as outras rotas da matriz (decisão adiada).
- O `secretarIA-frontend` antigo, ao criar unidade só com texto, recebe 422 (janela de deploy; fora do escopo).
- Descrição da clínica no limite de caracteres pode fazer um Salvar falhar depois que uma unidade nova alarga a região da saudação (ex.: "em Campinas" → "no Brasil"); no envio, `_fit_clinic_description` corta e nunca passa de 1024.
- Duas unidades com a mesma cidade em UFs diferentes contam como uma cidade; unidade legada com cidade e sem UF ao lado de outra cidade dá "no Brasil".
