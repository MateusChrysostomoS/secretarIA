# Portal: visita descartada ao entrar na conta — TASK-034

## Estado

Implementação local em worktrees `C:/TECH/BRAIN-worktrees/TASK-034/`, branch
`task/TASK-034-portal-visita` em cada repositório. Sem merge, push, deploy ou SQL remoto.
Um commit local por repositório, conforme o plano; SHAs no registro da TASK-034.
Fonte: plano `docs/superpowers/plans/2026-10-01-portal-visita-fundida-na-conta.md`,
L2 de `docs/LACUNAS_PORTAL_2026-10-01.md` e decisão do dono de 2026-10-01.

## Comportamento e contrato

- `services/visit_merge.py::discard_visit` apaga somente a identidade da visita no escopo
  `tenant_id + channel=brain_message + external_id`. Remove mensagens, mapa de pseudônimos,
  reservas, conversa e paciente. O chamador controla o commit. Mantém `consent_events` e
  `processed_events`, além de todo o histórico da conta.
- `POST /internal/brain-message/visits/merge` exige `X-Internal-Api-Key`, aceita
  `{tenant_id: UUID, visit_external_id: str(1..64), into_external_id: str(1..64)}` e responde
  `202 {"status":"queued"}`. Campos extras/identidades iguais são recusados com 422;
  fila ausente é 503. Sem migração de banco.
- `workers/portal/merge.py::merge_brain_message_visit` usa ledger por clínica/visita,
  comita o descarte antes de enviar e apresenta apenas o menu na conversa existente da
  conta. Se a conta nunca teve conversa, usa a abertura normal. Sem entitlement válido,
  o descarte ocorre e o bot fica em silêncio. O job também recusa fusão consigo mesmo.
- `brain-api` agenda o aviso após `pending/complete` somente quando `superseded_by`
  aponta para identidade diferente. A falha do aviso não interrompe o login; 404 durante
  rollout mantém o comportamento anterior. Primeira visita não dispara fusão.
- `Brain-Message-Frontend` recebe `switched` no resultado da promoção e limpa o estado
  local da conversa somente se a identidade mudou. `PortalChatScreen` altera a geração
  da key da conversa: a instância da visita é desmontada, os uploads são abortados e
  seus polls atrasados não podem repopular a instância da conta. Identidade igual
  conserva a instância e o rascunho do paciente.

## Revisão independente e correções

A revisão final apontou dois achados Important, reproduzidos antes das correções:

1. Claim do ledger consumido antes de falha transitória: agora falhas de claim,
   descarte, abertura e menu solicitam `arq.Retry`; claims incompletos são liberados.
   Um menu não enviado também libera o claim. A abertura é confirmada por mensagem
   outbound persistida; conversa vazia após falha ainda é primeira abertura.
   `_handle_show_main_menu` retorna o resultado de envio; outros consumidores podem
   continuar ignorando o retorno. Clínica sem entitlement permanece em silêncio.
2. Poll antigo restaurando a visita: remount condicional aposenta toda a instância
   anterior, incluindo estados de erros/reveal/anexos. Identidade igual não remonta.

Nenhum achado Critical ou Minor; nenhum comportamento excluído da avaliação.
Correções verificadas em uma única rodada, sem segunda revisão por agente.

## Validação local

Testes novos executados antes da implementação: ausência de módulo, rota/job, chamada
de fusão e função de comparação foram observadas em RED. Depois passaram em GREEN.

- SecretarIA: 101 testes da mudança e vizinhos passaram. Cobertura adicional de
  auditoria, reservas, mapa de pseudônimos, rollback, ledger real, entitlement e
  autodescarte. Registro do job e log de startup: 37 testes focados passaram.
- Brain-api: 98 testes de promoção/acesso passaram; 5 testes finais incluem callback,
  primeira visita, recusa HTTP, transporte inválido e configuração ausente.
- Frontend: 1.504 testes passaram; TypeScript e build passaram.
- Ruff dos arquivos Python afetados passou. A fachada de reexports não foi reformatada.
- Resultado consolidado das suítes completas e revisão: ver `C:/TECH/BRAIN/tasks/TASK-034/TASK.md`.

Validação final da correção: secretarIA **2.875 passed, 10 skipped** (20 testes de fusão);
brain-api **1.255 passed, 3 skipped**, mais os 5 testes de fusão executados novamente
após ampliar a cobertura de erro/configuração; frontend **1.504 passed**, TypeScript e
build verdes. Ruff amplo de `src/secretaria` e Ruff dos arquivos tocados no brain-api
verdes. Avisos de depreciação do status 422 seguem o padrão existente do projeto.
Graphify atualizado via AST nos três repositórios; zero duplicatas ou arestas pendentes.
A provenance aponta para o commit-base do rebuild, portanto não afirmar FRESH após commit.

Prova Chrome local contra build estático, com fixture `tasks/TASK-034/qa-promotion.js`:
antes da correção, liberar um poll da visita após promoção substituiu `ACCOUNT_HISTORY`
por `VISIT_ONLY`; depois, o histórico da conta permaneceu intacto. Na identidade igual,
`DRAFT_PRESERVE` permaneceu no composer antes e depois da promoção/poll atrasado.
Capturas `race-red.png`, `race-green.png`, `same-identity-green.png` em `tasks/TASK-034/`.
Esta prova local não substitui a prova de produção pendente abaixo.

## Ordem de deploy (ainda não autorizado)

1. SecretarIA **API e worker**, ambos na mesma revisão.
2. Brain-api.
3. Brain-Message-Frontend.

Nenhuma migração. A ausência temporária da rota não bloqueia o login.

## Prova em produção — pendente

Executar somente depois de deploy autorizado, com conta de QA e navegador sem cookies:

1. Abrir link da clínica; confirmar e-mail e código dentro do chat.
2. Conferir histórico antigo com um menu novo e nenhuma apresentação nova; F5 preserva.
3. Repetir duas vezes; confirmar uma conversa na conta e descarte das visitas.
4. Verificar `brain_message_merge_discarded status=discarded` e `visit_discarded` no worker.
5. Registrar evidências sem e-mail, código, tokens ou outros dados pessoais.

Nenhuma prova em produção foi executada nesta tarefa.
