# Troca de serviço com sinal pago — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Permitir trocar o serviço de uma consulta com sinal pago, tratando a diferença do sinal (Pix só da diferença no aumento; pergunta de devolução na redução), com novo prazo configurável; depois fechar as sessões 05–08 do pagamento.

**Architecture:** Nova tabela filha `deposit_adjustments` (o `pix_deposits.appointment_id` único permanece). Um módulo puro decide o resultado; um serviço orquestra cobrança/aviso; o webhook existente reconhece a cobrança do ajuste e aplica a edição pendente. O fluxo de edição (paciente e equipe) deixa de bloquear a troca paga e delega a esse serviço.

**Tech Stack:** Python/FastAPI/SQLAlchemy/Alembic/arq (secretarIA), Next.js/vitest (Brain-Message-Frontend), pytest, agent-browser.

**Spec:** `docs/superpowers/specs/2026-10-26-troca-servico-diferenca-sinal-design.md` (secretarIA worktree).

Worktrees (continuação TASK-067, nunca `git stash`/`git add -A`, sem commit/push/deploy):
`C:\TECH\BRAIN-worktrees\TASK-067\{secretarIA,brain-api,Brain-Message-Frontend}`. Testes secretarIA: `uv run python -m pytest` com `BOT_ALLOWLIST_WA_IDS=""`; nunca `ruff format .`; sem `npm run lint`.

## Global Constraints

- Diferença mínima fixa: **R$ 10,00** (`MIN_DIFFERENCE_CENTS = 1000`).
- Config nova: `pix_difference_refund_min_days`, int 0–60, padrão **2**, só gestor altera.
- Sinal novo = `round(preço_novo × deposit.percent_applied / 100)`; sinal antigo = `deposit.amount_cents`.
- Aumento: Pix só da diferença; serviço só muda após pago; prazo do Pix = `tenant.deposit_hold_ttl_minutes`; expirou → serviço antigo.
- Redução: serviço muda na hora; pergunta de devolução só se `dias_até_consulta >= min_days` E `diff >= 1000`; senão clínica fica com a diferença.
- Aumento perto da consulta continua permitido (min_days não bloqueia); aumento com `diff < 1000` troca sem Pix.
- Devolução nunca é automática: reutiliza `attention.open_attention_item` (razão `service_diff_refund_due`, ≤32 chars), `suggested_refund_cents = diff`.
- E-mail ao médico: só o aviso de mudança de serviço já existente; pagamento de diferença não gera e-mail.
- Pergunta sem resposta em 24 h = "não" (abatida, sem aviso à clínica).
- Sem PII/chaves em logs; copy em pt-BR; nunca prometer estorno automático.

## Review Focus

1. Pix da diferença pago DEPOIS de expirar/cancelar → item ORPHAN, nunca serviço mudado em silêncio (Task 5).
2. Webhook duplicado/reentrega do pagamento da diferença → aplica a edição uma única vez (Task 5).
3. Cancelar com Pix de diferença aguardando → anula o Pix e mantém uma só devolução coerente (Task 8).
4. Preço novo ilegível/zero ou convênio sem sinal → troca bloqueada com mensagem, nada gravado (Task 4).
5. Duas trocas seguidas com ajuste pendente → segunda recusada ("aguarde o pagamento anterior") (Task 5).

---

### Task 1: Config `pix_difference_refund_min_days` (secretarIA)

**Files:** Modify `src/secretaria/models/tenant.py` (junto a `pix_refund_window_hours`), `schemas/config.py` (`TenantConfigUpdate`, `TenantConfigRead`), `services/hub_configuration.py` (`TENANT_SCALAR_FIELDS` + leitura), `api/hub/config.py` (`PAYMENT_WRITE_FIELDS`); Create migração `migrations/versions/<12hex>_pix_difference_refund_min_days.py` (`down_revision="f3b9d1c7a5e2"`, confirmar com `alembic heads`); Test `tests/test_hub_config_pix.py`.
**Interfaces:** Produces: coluna `Tenant.pix_difference_refund_min_days: int` (default 2) e campo homônimo no JSON de config (leitura e escrita).

- [ ] Escrever testes: padrão = 2; gestor grava 5 e lê 5; valor 61 e -1 → 422; não-gestor → 403 no campo.
- [ ] Rodar e ver falhar.
- [ ] Implementar coluna (`Integer, server_default="2"`), schemas com `ge=0, le=60`, mapeamento e migração.
- [ ] Rodar `test_hub_config_pix.py`, `alembic heads` (um só head), `ruff check` nos arquivos.

### Task 2: Campo no painel Pix (Brain-Message-Frontend)

**Files:** Modify `lib/contexto/types.ts` (`PixDeposit`, `DEFAULT_PIX_DEPOSIT`), `lib/contexto/hub-mapping.ts` (leitura, escrita completa e diff-only), `lib/real/secretaria-hub.ts` (tipos), `components/contexto/PixSection.tsx`; Test `lib/contexto/__tests__/pix-save-scope.test.ts`, `components/contexto/__tests__/pix-section.test.tsx`.
**Interfaces:** Consumes: campo `pix_difference_refund_min_days` (Task 1). Produces: `PixDeposit.differenceRefundMinDays: number`.

- [ ] Testes: renderiza "Dias mínimos para devolver a diferença" com padrão 2; editar manda SÓ esse campo; secretária/médico não editam; texto explica o corte de R$ 10.
- [ ] Ver falhar, implementar, rodar `vitest` dos dois arquivos, `tsc.cmd --noEmit`.

### Task 3: Modelo `DepositAdjustment` + razão de atenção

**Files:** Create `src/secretaria/models/deposit_adjustment.py`, migração `<12hex>_deposit_adjustments.py` (encadeada após Task 1), Modify `models/__init__.py`, `models/payment_attention_item.py` (`ATTENTION_REASONS` += `service_diff_refund_due`), `services/payments/attention_view.py` (`REASON_TEXT`; NÃO entra em `_REFUND_RECORDING_REASONS`); Test `tests/test_deposit_adjustment_model.py`.
**Interfaces:** Produces: `DepositAdjustment` com campos da spec; constantes `KIND_INCREASE="increase"`, `KIND_DECREASE="decrease"`; `STATUS_*` (strings da spec); índice único em `provider_charge_id`.

- [ ] Teste: cria ajuste, único por `provider_charge_id`, FK `deposit_id`, tenant obrigatório; razão nova aceita por `open_attention_item`.
- [ ] Ver falhar, implementar, `alembic heads` único, testes de modelos de pagamento existentes verdes.

### Task 4: Decisão pura da troca

**Files:** Create `src/secretaria/services/payments/service_change_policy.py`; Test `tests/test_service_change_policy.py`.
**Interfaces:** Produces:
```python
MIN_DIFFERENCE_CENTS = 1000
@dataclass(frozen=True)
class ChangeDecision:
    action: Literal["no_change","increase_charge","increase_free","decrease_ask","decrease_keep","blocked"]
    diff_cents: int
    reason: str | None = None
def decide_service_change(*, paid_deposit_cents: int, percent_applied: int,
    new_price_cents: int | None, days_until: float, min_days: int) -> ChangeDecision
```
- [ ] Testes (tabela): igual→`no_change`; +R$ 25→`increase_charge` mesmo com `days_until=0`; +R$ 9,99→`increase_free`; −R$ 25 e dias≥min→`decrease_ask`; −R$ 25 e dias<min→`decrease_keep`; −R$ 9,99→`decrease_keep`; `new_price_cents` None/0→`blocked`; `min_days=0` pergunta sempre.
- [ ] Ver falhar, implementar, passar.

### Task 5: Orquestração + webhook + expiração + resposta

**Files:** Create `services/payments/service_change.py`; Modify `services/payments/event_processor.py` (`find_deposit`, `_on_paid`), `services/payments/jobs.py` (varredura de ajustes vencidos), `workers/shared/deposit.py` ou módulo de ações existente para a resposta sim/não; Test `tests/test_service_change.py`, `tests/test_service_change_webhook.py`.
**Interfaces:** Consumes: `decide_service_change`, `DepositAdjustment`, `attention.open_attention_item`, provider `create_pix_charge`. Produces:
```python
async def request_service_change(session, *, tenant, appointment, deposit, new_service: str,
    pending_edit: dict, now: datetime) -> ServiceChangeResult   # .kind: "applied_now"|"awaiting_payment"|"asked"|"blocked"; .pix_copy_paste: str|None; .diff_cents
async def answer_refund_question(session, *, adjustment_id, accept: bool, now) -> None
async def expire_adjustments(session, *, now) -> int
```
`awaiting_payment`: cria ajuste + cobrança (`external_reference=str(adjustment.id)`), NÃO grava a edição. `applied_now`/`decrease_*`: aplica o `pending_edit` com a mesma escrita de `write_appointment_edit` na transação do chamador. `decrease_ask`: ajuste `asked`. `answer_refund_question(accept=True)` abre item `service_diff_refund_due`; `False` → `kept_credit`.

- [ ] Testes: aumento cria ajuste+Pix e serviço antigo permanece; webhook pago aplica a edição UMA vez (reentrega ignorada); pago após expirar → ORPHAN e serviço antigo; valor ≠ diferença → recusado como no `_on_paid` atual; segunda troca com ajuste pendente → `blocked`; redução pergunta/abate conforme Task 4; resposta sim abre exatamente um item com `suggested_refund_cents=diff`; 24 h sem resposta → `kept_credit` sem item.
- [ ] Ver falhar, implementar, passar, regressão `test_payment_event_processor.py`, `test_deposit_lifecycle.py`.

### Task 6: Fluxo de edição do paciente (WhatsApp + Portal)

**Files:** Modify `services/appointment_edit_flow.py` (`_menu_labels`, `_more_result`, `edit_step` e reversão de cartão velho, `apply_ai_edit`), `services/appointment_edit.py` (`EditContext`/`EDIT_PIX_NOTICE`), `workers/shared/appointment_edit_apply.py` (regra `money_changed` chama o serviço da Task 5 para SERVIÇO; médico/convênio continuam bloqueados com sinal pago), `workers/shared/appointment_edit_support.py`; mensagens com valor da diferença e botões fixos "Sim, quero de volta"/"Não, abater"; Test estender `tests/test_appointment_edit_apply.py`, `test_appointment_edit_flow_menu.py`, `test_appointment_edit_flow_fields.py`.
**Interfaces:** Consumes: `request_service_change`, `answer_refund_question`.

- [ ] Testes: com sinal pago o menu mostra "Mudar serviço"; aumento mostra o valor e o Pix copia-e-cola e diz que o serviço só muda após o pagamento; redução aplica e pergunta; botões roteiam para `answer_refund_question`; médico/convênio continuam ocultos; IA não consegue trocar serviço pulando a regra.
- [ ] Ver falhar, implementar, passar suíte de edição inteira.

### Task 7: Edição pela equipe (agenda)

**Files:** Modify `services/staff_appointment_edit.py`, `api/hub/calendar.py` (handler `POST /appointments/{id}/edit`, resposta com `adjustment` quando houver); Test `tests/test_staff_appointment_edit.py`, `tests/test_hub_edit_appointment.py`.
- [ ] Testes: equipe troca para mais caro → 200 com `status: "awaiting_payment"`, paciente recebe o Pix, serviço antigo permanece; mais barato → aplicado e paciente recebe a pergunta; papéis/escopo inalterados.
- [ ] Ver falhar, implementar, passar. (Front: estado "aguardando pagamento da diferença" no drawer da agenda, com teste vitest.)

### Task 8: Cancelamento e lembretes com ajustes

**Files:** Modify `services/payments/deposit_lifecycle.py` (`_void_awaiting`, `on_appointment_cancelled`: anula Pix de diferença aguardando; devolução usa `amount_cents + diferenças pagas`), `workers/shared/deposit.py` (texto de aviso de cancelamento), `plugins/reminders.py` se citar valor; Test `tests/test_deposit_cancel_no_refund.py`, `tests/test_service_change_cancel.py`.
- [ ] Testes: cancelar com diferença aguardando anula o Pix; cancelar após aumento pago pede devolução do total; redução com pergunta pendente não duplica item; nenhum caminho devolve dinheiro.
- [ ] Ver falhar, implementar, passar.

### Task 9: E-mails e avisos

**Files:** Test `tests/test_professional_edit_notification.py` (estender).
- [ ] Teste: troca de serviço aplicada gera UM aviso "Consulta alterada" ao médico (campo Serviço); pagamento da diferença não gera e-mail novo; Pix da diferença aguardando não envia aviso ao médico.
- [ ] Ajustar só se o teste falhar.

### Task 10: Documentação e registro

**Files:** Modify `secretarIA/docs/CHECKPOINT_pix_provider_core.md` (seção: ajuste de sinal), `Brain-Message-Frontend/docs/CHECKPOINT_*` correspondente, `C:/TECH/BRAIN/tasks/TASK-067/LEDGER.md` (sessão "05c troca de serviço").
- [ ] Atualizar só depois de validar; separar local/commitado/publicado (tudo local).

### Task 11: Prova ponta a ponta no navegador

Ambiente `C:\TECH\BRAIN-worktrees\TASK-067\_local-test\` (run.sh, Asaas sandbox). Usar `agent-browser`.
- [ ] Cenários: (a) Alfa com sinal pago → troca para mais caro → Pix da diferença → pagamento no sandbox → webhook → serviço muda; (b) não pagar → expira → serviço antigo; (c) mais barato com prazo ok → pergunta → sim → item na Atenção da clínica; não → abatido; (d) mais barato perto do prazo → sem pergunta; (e) campo de dias no /contexto, 390 e 1280 px, claro/escuro; (f) cancelar com diferença pendente. Screenshots sanitizadas em `tasks/TASK-067/screens/s05c-*`.

### Task 12–15: Sessões do pacote (após 1–11, sequenciais)

- **12 = Sessão 05** `z_prompts/PROMPT_OUT_OF_MVP_05_CONSULTAS_LIBERADAS.md`; **13 = Sessão 06** `…_06_PRAZOS_EQUIPE.md`; **14 = Sessão 07** `…_07_ROBUSTEZ.md`; **15 = Sessão 08** `…_08_ACEITE_LOCAL.md`. Cada uma segue o próprio prompt (escopo, aceite, handoff no LEDGER, sem commit/push/deploy); a regra "sem subagentes" dos prompts é substituída aqui pelo pedido do dono de usar subagent-driven-development. A 08 re-roda a suíte completa dos 3 repos e a jornada da troca de serviço.

## Paralelismo

Wave 1 (paralelo, arquivos disjuntos): Tasks 1+3+4 (secretarIA) ∥ Task 2 (front). Wave 2: Task 5. Wave 3 (paralelo): 6 ∥ 7 ∥ 8. Wave 4: 9, 10, 11. Wave 5: sessões 05→08.
