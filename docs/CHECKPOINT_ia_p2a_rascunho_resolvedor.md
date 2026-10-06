# TASK-030 — IA entra em qualquer etapa: P2a

Atualizado em 2026-10-05. **P2a A1–A6 implementado e validado localmente.** A1–A3 já estavam
commitados no início da retomada (`7a62e47`, `10d4844`, `33597f9`); A4–A6 e as correções da
revisão ficam **sem novo commit**, no worktree `C:\TECH\BRAIN-worktrees\TASK-030\secretarIA`,
branch `task/TASK-030-ia-entra-em-qualquer-etapa`. Nada desta retomada foi mesclado, enviado
ou implantado. P2b e os demais planos continuam pendentes.

Plano executado: `superpowers/plans/2026-10-02-ia-p2a-rascunho-coluna-e-resolvedor.md`.
Spec: `superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md`, §4.1–4.3, §6–7.

## O que está construído

- `services/availability.py`: leitura de horários livres menos reservas, compartilhada pelos
  seletores e pelo resolvedor; nenhum título, participante ou id de evento sai por essa leitura.
- `services/booking_draft.py::BookingDraft`: payload v2 e leitura compatível com v1; registro
  estacionado com UTC e prazo de 30 minutos, sem nome de terceiro.
- `models/conversation.py::Conversation.flow_draft` e migração `e7d3c1a9b5f2`: JSON nullable,
  sem default; `FlowRouterResult.flow_draft` persistido/limpo por `_apply_flow_result` e levado
  somente nas etapas de pra quem. Menu e pisos de silêncio limpam o rascunho.
- `services/booking_draft.py::resolve_booking_draft`: valida pra quem, convênio, profissional,
  serviço, dia e horário; descarta apenas o campo inválido e carrega as respostas validadas.
  Horário é rederivado da agenda atual, dentro de `booking_gate_scope`, inclusive em chamada direta.
- `workers/shared/draft_resolution.py`: conversa, tenant, profissionais e catálogos relidos;
  agenda construída somente quando necessária, para o profissional resolvido; continuação após
  resposta de pra quem, com fallback ao fluxo comum se rascunho expirou ou houve falha.
- `_flow_professionals` centraliza o snapshot usado pelo orquestrador e pela continuação,
  preservando a distinção entre configuração NULL herdada e configuração explicitamente vazia.

O resolvedor ainda não está ligado às ferramentas da IA: isso é P2b. O gancho
`_express_confirmation` retorna `None` no P2a; pedido completo pousa na lista de horários.
A retenção através de perguntas de convênio, médico e serviço fica no P3.

## Revisão independente e correções

A revisão do conjunto A1–A6 encontrou quatro problemas importantes presentes também nas
premissas/código proposto pelo plano. Todos foram reproduzidos por testes antes da correção:

| Achado | Correção | Prova |
|---|---|---|
| Nome capturado em `awaiting_attendee_auth` era tratado como autorizado | Reapresentar o cartão de autorização e estacionar o rascunho. A continuação legítima sobrepõe estado/etapa do resultado determinístico antes de resolver novamente. | `test_a_captured_name_still_waiting_for_authorization_cannot_skip_it` (attendee other e omitido), mais continuação com consentimento registrado uma vez |
| Os oito primeiros horários eram limitados antes de descontar reservas | Em agenda com reservas, ler posições suficientes, descontar reservas e depois limitar a oito horários exibidos; manter ambas as saídas de navegação e caminho sem reservas. | `test_reserved_early_slots_do_not_hide_later_free_times`, `test_the_picker_limits_free_slots_after_subtracting_holds` |
| Tenant do início do turno contaminava a leitura fresca | Reler o tenant na sessão de contexto e carregá-lo em `DraftContext.tenant`; a agenda usa esse mesmo objeto fresco. | `test_draft_context_reloads_clinic_settings_instead_of_using_the_turn_snapshot`, `test_lazy_draft_calendar_uses_the_reloaded_clinic_settings` |
| 96 posições não cobriam serviços menores que 15 minutos | `slot_scan_limit` expande a varredura pela duração; 96 continua como piso/default publicado, até 1.440 posições para serviço de um minuto. | `test_a_short_service_can_accept_a_free_time_beyond_the_first_96_slots`, `test_a_short_service_keeps_a_day_with_only_late_free_slots_after_holds` |

Decisão final: **READY para o escopo local de P2a**, com os quatro achados cobertos por
RED→GREEN e suíte completa verde. A revisão independente foi única; a correção foi validada
pelos testes, sem segunda rodada de revisão.

## Validação

Testes sempre pelo Git Bash, com `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest`:

- Baseline anterior a P2a, registrado pela execução A1: **3.107 passed, 0 failed**.
- Retomada de A3: HEAD `33597f9`; relatório anterior: **3.149 passed, 10 skipped**.
- A4: comando focado do plano, **247 passed**. Trabalho parcial preservado; teste de ausência
  do resolvedor conferido numa cópia isolada de A3, sem desfazer os arquivos existentes.
- A5: **11 failed / 29 passed** antes da implementação → **184 passed** no comando do plano.
- A6: erro de importação do módulo ausente → **211 passed** no comando do plano.
- Correções da revisão: oito novos casos falharam antes das correções; suíte focada consolidada
  **197 passed**, incluindo autorização, contexto fresco e disponibilidade de serviços curtos.
- Suíte completa final: **3.202 passed, 10 skipped, 14 warnings**, em 160,78 s. Os avisos são
  de depreciação em FastAPI/uso de status 422 e configuração legada de caminho do Alembic.
- `uvx ruff check src/secretaria tests/test_booking_draft_resolver.py
  tests/test_booking_draft_continuation.py tests/test_availability.py`: clean.
- Format dos arquivos criados pelo plano: clean. Hunks de format em arquivos antigos,
  antes→depois: flow_router 22→22; greeting 12→11; orchestrator 2→2; flow_runner 2→2.
- `git diff --check`: clean; nenhum arquivo antigo aparece como reescrito inteiro.

A primeira suíte completa da retomada terminou em **8 failed, 3.186 passed, 10 skipped**:
os oito casos `test_each_entry_point_imports_alone` tiveram subprocesso Python encerrado no
Windows com código `0xC0000142` e stderr vazio. A repetição imediata de `test_workers_layering.py`
teve **15 passed**, e a suíte completa final também passou esses oito casos. Não houve
traceback de importação nem alteração de código para contornar essa ocorrência transitória.

## Migração comprovada em SQLite e PostgreSQL

A3 havia comprovado SQLite e deixado PostgreSQL pendente por Docker indisponível. Nesta
retomada, PostgreSQL 16 local descartável comprovou a cadeia completa de migrations:

1. `upgrade head`: `flow_draft` tipo `json`, nullable `YES`, sem default.
2. `downgrade -1`: coluna ausente, revisão `e5a1c9d3b7f2`.
3. Novo `upgrade head`: revisão `e7d3c1a9b5f2`, coluna restaurada corretamente.

Container exclusivo removido no fim. Porta local dinâmica ligada apenas a 127.0.0.1 para
evitar interferência com qualquer banco existente. Nenhum SQL remoto ou produção acessado.
O parent real da migração é `e5a1c9d3b7f2`, corrigindo o parent desatualizado do plano e
preservando um único head Alembic.

Quando houver pedido de implantação do P2 inteiro: migração primeiro, API e worker juntos.
Rollback: código antigo nos dois serviços antes de remover a coluna. **Deployment: NOT AUTHORIZED.**

## Decisões e pendências para a continuação

- Preservado o worktree existente e os arquivos parciais de A4. A prova RED contra A3 usou
  cópia em scratch; não é um registro original de test-first dessa sessão interrompida.
- Sem commit novo por instrução atual do dono; ledger e provas em
  `.superpowers/sdd/2026-10-02-ia-p2a-rascunho-coluna-e-resolvedor/` permanecem necessários.
- Próximo plano: P2b, **não iniciado nesta retomada**. O P2a sozinho não habilita ferramenta,
  interruptor ou prompt; custo do adiamento: a melhoria ampla ainda não está disponível ao paciente.
- P3 continua dono da confirmação expressa e da retenção em outras perguntas; custo do
  adiamento: pedido completo ainda recebe lista de horários e pode precisar redizer dia/horário.
- **Adaptar os trechos de substituição do P3 ao código final**, especialmente `_load_draft_context`
  (preservar a releitura de tenant e preencher `DraftContext.tenant`) e `_resume_booking_draft`
  (preservar a sobreposição de estado/etapa aprovados). Não transcrever trechos antigos que
  removeriam essas correções; os novos testes de regressão devem continuar verdes.
- Menores já registrados antes desta retomada permanecem: teste de fronteira exata de 30 min
  e timestamp futuro; agenda sem fuso (`_touches_day`); atenção ao potencial ciclo se
  `flow_router` vier a importar `booking_draft`; comentário de state_expiry não enumera
  explicitamente a limpeza de `flow_draft`. Nenhum caminho operacional para timestamp futuro
  ou agenda sem fuso foi demonstrado pela revisão. Não alterados nesta execução.
- Graphify estava INVALID: execução do CLI bloqueada por política local. Código e testes
  foram a evidência; grafo não foi reconstruído.
