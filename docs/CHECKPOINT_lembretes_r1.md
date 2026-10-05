# Lembretes R1 — Fundação (TASK-032)

## Estado

**Follow-up de 2026-10-05:** o dono autorizou commit, integração em `main` e push. Deploy e SQL remoto não autorizados ao agente. Os SHAs e o resultado da publicação ficam no registro TASK-032 da workspace.

Implementado no worktree `C:\TECH\BRAIN-worktrees\TASK-032\secretarIA`, branch `task/TASK-032-lembretes-e-confirmacao`, base `9278716`. Fundação commitada em `9784450`; integrada à base atual da `main` (`583c8dc`) em `8bdc5db`, com o ajuste de migração descrito abaixo. R1 não envia mensagens nem ativa clínicas.

Migração `b8d3f1a6c2e5` validada em SQLite descartável; NÃO aplicada a bancos persistentes/remotos. **Postgres run NOT done:** Docker instalado, porém daemon indisponível (pipe dockerDesktopLinuxEngine ausente). Validar em Postgres descartável antes de deployar.

## O que entrou onde

| Área | Conteúdo |
|---|---|
| `models/appointment_reminder.py` | Tabela `appointment_reminders`, vocabulários, índices de envio/aviso, `invalidated_at` e unicidade parcial de linhas ativas por consulta/tipo/horário |
| `models/appointment.py` | `confirmation_count`, `first_confirmed_at`, `last_confirmed_at` |
| `models/tenant.py` | `reminders_v2_enabled` (false), `reminder_extra_lead_minutes` (nullable) |
| `migrations/versions/b8d3f1a6c2e5_appointment_reminders_foundation.py` | Migração aditiva sobre `e5a1c9d3b7f2`; head único após integração com a `main` |
| `services/reminder_schedule.py` | `schedule_reminders`, `cancel_reminders`, `reschedule_reminders`, `register_confirmation`, `reset_confirmation`, `display_state`, `ReminderMismatchError` |
| `schemas/calendar.py`, `api/hub/calendar.py` | GET `/events`: `status`, `confirmation_count`, `display_state`, `attention`, `reminders`; resumo da versão atual, datas UTC; `AppointmentRead.confirmation_count`; PATCH confirmado conta uma vez, scheduled zera, terminal cancela pendências |

O serviço não commita: o chamador mantém a transação. A agenda carrega os lembretes em uma consulta por página com escopo de tenant. Campos antigos preservados; eventos sem consulta local têm campos novos nulos.

## Regras fixadas

Decisões do plano, transcritas literalmente:

1. **No reason column.** The spec has no field for why a row was cancelled; `cancel_reminders(reason=...)` only logs it (id + reason code, no PII).
2. **`reminder_id=None` counts only at 0.** Staff "mark as confirmed" (and any prompt without a row) is idempotent and never reaches 2; two confirmations need two distinct reminder rows.
3. **Stale taps are ignored, not errors.** A cancelled row or a row of an older start returns the current count unchanged; a row of another clinic/appointment raises `ReminderMismatchError` (callers answer generically).
4. **`display_state` for terminal statuses is `unconfirmed`.** The four values of the binding contract are kept; clients colour terminal states by `status`.
5. **PATCH side effects beyond `confirmed`:** `scheduled` zeroes the counter (keeps "confirmed iff count >= 1"); terminal statuses cancel pending rows. PATCH to `rescheduled` only sets the status (the hub `reschedule` endpoint, wired in R2, is what recreates rows).
6. **Row `channel` defaults to `whatsapp`** at creation; R2 sets the real channel (email for Portal) at send time. `with_prompt` is computed at creation and must be recomputed at send time.
7. **No DB check constraint on the counter or vocabularies** (SQLite cannot `ADD CONSTRAINT`, and the vocabularies will grow in R2-R4); the cap lives in the service and is tested.
8. **Hub create/reschedule/cancel endpoints are NOT wired here** (R2). Spec section 9 does not list `api/hub/calendar.py` among R2's files, but R2 must edit it for those three endpoints, and R4 edits it too: sequence R2 before R4 on that file.
9. `AppointmentRead.confirmation_count` is added (not requested by the spec) so the front can update the chip from the PATCH response without refetching the page.

## Ajustes de execução

- Primeira entrega: commits/atribuições do plano omitidos por instrução explícita do dono. No follow-up, commit, integração em `main` e push foram autorizados.
- Primeira entrega: suíte completa não executada conforme o plano; validação proporcional nas sete suítes nomeadas. A validação adicional da integração está registrada abaixo.
- O teste antigo de depósito que exigia ausência de `status` passou a verificar `scheduled` e contador 0: pagamento não confirma presença.
- Imports e supressões de fixtures ajustados para lint; nenhuma formatação ampla em arquivos existentes.
- Graphify INVALID por bloqueio do executável pelo App Control; evidência obtida do código e testes.

### Correções da revisão independente (prevalecem sobre o exemplo de reutilização do plano)

- **Identidade de geração:** `invalidated_at` é nullable e aditivo na nova tabela; ao cancelar/remarcar, linhas antigas são invalidadas. Pendentes/sending ficam cancelled; sent/failed conservam status e timestamps como histórico. A unicidade `uq_appointment_reminders_version` agora é um índice único parcial WHERE `invalidated_at IS NULL`. Mesmo horário ou A→B→A sempre produz IDs novos, incluindo os tipos já enviados. Isso resolve o conflito do plano: não seria possível manter enviados, recriar cronograma e recusar botões antigos reutilizando os mesmos IDs.
- **Contrato R2–R5:** só reivindicar/enviar/avisar linhas com `invalidated_at is None`; revalidar também esse campo após o envio. Confirmação ignora linhas invalidadas, e API/display excluem avisos/história invalidados. Assinaturas públicas permanecem iguais. Não confiar apenas em igualdade de `appointment_start_at`.
- **Concorrência:** `_lock_appointment` lê o estado atual com FOR UPDATE e `populate_existing`, mantendo apenas edições explícitas do chamador; contador/status limpos não vêm de uma leitura ORM antiga. Ordem de bloqueio: consulta antes dos lembretes, mantida até o chamador terminar a transação. Usado na criação, remarcação e confirmação. O lembrete confirmado também é recarregado/bloqueado para deduplicação.
- **Tenant:** cancelamento resolve o tenant da consulta e inclui esse filtro em ambos os UPDATEs; remarcação recusa tenant divergente antes de alterar qualquer dado.
- **Minor adiado:** ainda há um único `chat` ativo por consulta/horário. R3 deve permitir linhas distintas de mensagens de abertura antes de implementar a segunda abertura elegível. Não foi incluído no passe de correções R1.

## Pendências para R2–R5

- R2: chamar `schedule_reminders` nas criações pelo fluxo, ferramenta da IA, promoção de reserva do Portal e POST `/appointments` do hub.
- R2: chamar `reschedule_reminders` nas remarcações pelo fluxo, botão e POST `/reschedule` do hub.
- R2: chamar `cancel_reminders` nos cancelamentos pelo fluxo, botão, POST `/cancel` do hub e varredura de sinal expirado.
- R2: cron de envio, reivindicação/reenvio, canal real, recomputar `with_prompt` no envio; backfill das consultas futuras; revalidar linhas `sending` após enviar.
- R2–R5: respeitar `invalidated_at` e a ordem de bloqueio consulta→lembrete, conforme o contrato acima; exemplos antigos dos planos que reutilizem IDs na remarcação precisam ser adaptados.
- R3: primeira mensagem no chat, novas ações e caminho do Cancelar; respeitar a sequência após TASK-030.
- R4: avisos por e-mail, liberar horário e enviar mensagem; sequenciar alterações de `api/hub/calendar.py` após R2.
- R5: agenda com cores, ✓/✓✓, linha do tempo e ações; configuração do lembrete extra.

## Verificação

Baseline: `tests/test_hub_calendar_money.py tests/test_calendar_events_insurance.py`: **53 passed**.

Git Bash:

```bash
BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_appointment_reminder_model.py tests/test_migration_reminders_foundation.py tests/test_reminder_schedule.py tests/test_reminder_confirmation.py tests/test_hub_calendar_confirmation.py tests/test_hub_calendar_money.py tests/test_calendar_events_insurance.py -q
```

Resultado inicial integrado: **116 passed**. Resultado final após revisão/correções: **127 passed**, 1 aviso de depreciação da configuração Alembic (`path_separator`, configuração anterior não modificada). Testes escritos e executados em RED antes de cada implementação.

Revisão independente somente leitura: `r1_final_review` (gpt-6-astra), três findings Important e um Minor. Findings registrados antes da correção em `C:\TECH\BRAIN\tasks\TASK-032\REVIEW.md`. Um único passe de correções, sem re-review; 9 reproduções de serviço + 1 reprodução API falharam primeiro pelos efeitos descritos, depois passaram. Cobertura nova: leitores ORM antigos (confirmações distintas/repetidas, cancelamento/remarcação/reset), tenant divergente em pending/sent, recusa de remarcação estrangeira sem mutações, mesmo horário/A→B→A com histórico enviado e estado correto no GET.

SQLite demonstra recarga/deduplicação e preservação de estado. Não demonstra bloqueio concorrente real do Postgres (FOR UPDATE é ignorado em SQLite); essa prova continua pendente com a migração descartável em Postgres.

`uvx ruff check` em todos os arquivos Python criados/modificados: sem erros. `git diff --check`: sem erros. Arquivos existentes têm mudanças pontuais.

## Integração em `main` (follow-up autorizado)

- Fundação: `9784450`; incorporação da `main` em `583c8dc`: merge `8bdc5db`.
- A `main` trouxe a migração `e5a1c9d3b7f2` (clinic facts). O teste de head único falhou com dois heads antes do ajuste; a nova migração R1 passou a depender dela, mantendo o head `b8d3f1a6c2e5`. Nenhuma migração existente foi alterada.
- Sete suítes do plano na versão integrada: **127 passed**, um aviso anterior do Alembic. Ruff da versão fixada pelo repositório: sem erros, após quebrar duas assertions longas em linhas menores.
- As alterações preexistentes da `main` em `docs/CHECKPOINT_portal_mensagens_recentes.md` e `docs/superpowers/plans/2026-10-01-digitando-frontend.md` não entram nos commits R1; integridade verificada por SHA256.
- Suíte completa adicional na versão integrada: **3181 passed, 10 skipped, 14 warnings**, nenhuma falha (450,87s). Avisos de depreciação FastAPI/HTTP 422 e configuração Alembic; nenhum ajuste fora do escopo foi necessário. O SHA final da `main` e a confirmação do push ficam no registro TASK-032 da workspace.

## Ordem de liberação (não autorizada ao agente)

Migração primeiro; API e worker em seguida; interruptor desligado até R2 e prova na clínica de teste; front depois. Não executar downgrade em banco usado por imagem nova. Deployment: **NOT AUTHORIZED**.
