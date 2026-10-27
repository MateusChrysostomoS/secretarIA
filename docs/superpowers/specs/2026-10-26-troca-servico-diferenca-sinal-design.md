# Troca de serviço com sinal pago — diferença do sinal (design)

Data: 2026-10-26. Worktrees: `C:\TECH\BRAIN-worktrees\TASK-067\{secretarIA,brain-api,Brain-Message-Frontend}` (continuação da TASK-067; sem commit, sem push, sem deploy).
Fora de escopo: convênios (convênio não muda o que for cobrado aqui), Mercado Pago (sessão 09).

## Por quê
Hoje, com o sinal pago, o paciente NÃO pode trocar de serviço (`EditContext.paid_deposit` esconde a opção) e a equipe pode trocar sem nenhum tratamento do dinheiro. Falta uma regra para a diferença do sinal.

## Regras (decisão do dono, 2026-10-26)
1. **Mais caro:** novo Pix SÓ com a diferença do sinal (`sinal_novo − sinal_pago`). A consulta só muda de serviço depois que esse Pix for pago. Se não pagar no prazo, continua o serviço antigo. Vale também perto da consulta (o prazo mínimo NÃO bloqueia), desde que o novo serviço caiba no horário do médico. O paciente recebe o Pix copia-e-cola.
2. **Mais barato:** o serviço muda na hora; o sistema pergunta ao paciente se quer a diferença de volta. Sim → abre aviso para a clínica devolver (igual ao cancelamento, nunca devolve sozinho). Não → a diferença fica abatida no valor da consulta (só registro).
3. **Prazo mínimo (nova configuração da clínica):** `pix_difference_refund_min_days` (dias antes da consulta). Faltando menos dias que isso, ou diferença menor que **R$ 10,00 (fixo)**, no caso "mais barato" o sistema não pergunta e a clínica fica com a diferença (abatida no resto da consulta). No caso "mais caro" com diferença < R$ 10,00 a troca vale sem Pix (a clínica acerta na consulta).
4. **E-mail ao médico:** só avisa a mudança de serviço (já existe o aviso "Consulta alterada" via outbox). Pagamento de diferença NÃO gera e-mail extra ao médico.

## Decisões minhas (a conferir)
- `pix_difference_refund_min_days`: inteiro 0–60, padrão 2; 0 = sempre pergunta (respeitando o corte de R$ 10). Só gestor altera (grupo `PAYMENT_WRITE_FIELDS`). Campo no painel Pix de /contexto.
- Cálculo: sinal antigo = `deposit.amount_cents`; sinal novo = `round(preço_novo × deposit.percent_applied / 100)` (percentual do sinal pago, não o atual da clínica). Preço novo ilegível/zero → troca bloqueada com "fale com a clínica".
- Prazo do Pix da diferença = `deposit_hold_ttl_minutes` da clínica. Expirou/cancelado → ajuste `expired`, serviço antigo mantido, paciente avisado.
- Sem resposta do paciente à pergunta de devolução em 24 h → tratada como "não" (abatida); não abre aviso.
- Equipe (agenda) usa o MESMO motor: aumento gera Pix pendente enviado ao paciente; redução pergunta ao paciente.
- Troca que muda a duração segue o fluxo de horário atual; só conta no limite de reagendamento se o horário mudar.
- Cancelar com Pix de diferença aguardando: anula esse Pix. Cancelar depois de diferença paga: o aviso de devolução usa o total pago (sinal + diferenças pagas).

## Modelo de dados (secretarIA)
Nova tabela `deposit_adjustments` (migração; filha de `pix_deposits`; mantém `appointment_id` único):
`id, tenant_id, deposit_id, appointment_id, kind (increase|decrease), status (awaiting_payment|applied|expired|cancelled|asked|refund_requested|kept_credit|clinic_kept), old_service, new_service, pending_edit (JSON), diff_cents, provider_charge_id (único; nulo em decrease), expires_at, asked_at, resolved_at, created_at`.
Webhook: `event_processor.find_deposit` reconhece o `provider_charge_id` do ajuste; o pagamento valida referência/valor da DIFERENÇA, aplica o `pending_edit` na mesma transação, marca `applied`. Pagamento de ajuste já expirado/cancelado → item de atenção ORPHAN existente. `PixDeposit.amount_cents` NÃO muda.

## Pontos de código
- Liberar troca paga: `appointment_edit_flow._menu_labels/_more_result/edit_step/apply_ai_edit`, `appointment_edit_apply.py` (`money_changed`).
- Novo `services/payments/service_change.py`; reuso de `attention.open_attention_item` com razão nova `service_diff_refund_due` (≤32 chars) e texto em `attention_view.REASON_TEXT`.
- Cancelamento: `on_appointment_cancelled`/`_void_awaiting` somam/anulam ajustes.
- Config: `models/tenant.py`, `schemas/config.py`, `services/hub_configuration.py`, `api/hub/config.py`, migração (head `f3b9d1c7a5e2`), front `PixSection.tsx`, `hub-mapping.ts`, `types.ts`.
- Mensagens nos dois canais (WhatsApp botões fixos sim/não; Portal).

## Antiobjetivos
Nunca devolver dinheiro sozinho. Nunca cobrar diferença sem o paciente ver o valor. Não mudar o serviço antes do pagamento do aumento. Sem PII/chaves em logs. Não mexer em convênio nem no WhatsApp de produção do PreCheck.

## Pacote completo
Após a troca: sessões 05 (consultas liberadas/Manter/devolução), 06 (prazos da equipe), 07 (reauditoria), 08 (aceite integrado) conforme `z_prompts/`. Prova de tela com `agent-browser` (390 e 1280 px).
