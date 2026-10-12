# TASK-041 — edição pela IA sem perder a consulta

Data: 2026-10-08. Candidato local no branch `task/TASK-041-edicao-ia`, base `99b9769`.
Repositório único: secretarIA. Sem push, merge, deploy, SQL remoto ou alteração de produção.

## Efeito para o paciente e para a clínica

O paciente pode mudar de ideia durante Alterar Dados, manter o dia/horário/convênio e
chegar à confirmação somente das mudanças que escolheu. A consulta continua marcada;
nada é trocado na agenda antes de Confirmar. A clínica recebe um único aviso para o
médico que ficará responsável pela consulta confirmada.

## Causa confirmada e limite da investigação

Abrir Mudar data não apagava a data: `EditDraft.original` e `current` já permaneciam
intactos. O texto livre ia para uma IA com ferramentas do agendamento/remarcação antigos;
esses retornos saíam da edição e o descarte legítimo do rascunho na saída ocorria sem o
paciente querer sair. Reproduzido no worker com Portal de teste, SQLite, IA simulada e
calendário falso; os testes RED capturam o desvio.

Não foi possível confirmar o caminho exato da conversa de produção ou o interruptor
atual da Chrysostomo: `get_service_logs` não estava disponível, não havia sessão de
navegador/Portal e nenhuma fixture atual do dono estava confirmada. O código de
`ai_draft_v2_enabled` exige `initial_flows["ai_draft_v2"] is True`, com padrão desligado;
não inferimos a flag atual de registros históricos. Nenhuma Environment foi consultada.

## Desenho implementado (âncoras estáveis)

- `ai.tools.propose_appointment_edit` levanta `AppointmentEditRequested`: proposta estreita
  para o rascunho em andamento. Não recebe id de outra consulta nem nome de paciente.
  `attendee=self/other` reutiliza captura de nome e autorização existentes.
- `ai.graph.effective_tools` usa uma allowlist durante a edição nas versões v1 e v2:
  informações, disponibilidade somente leitura e atendimento humano continuam disponíveis.
  Agendamento, remarcação antiga, criação, cancelamento, pré-consulta e saída para menu
  ficam ausentes. `build_agent` mantém `_tool_cache_key` e adiciona o discriminador
  `#editing`; os contextos por turno são repassados e restaurados.
- `ai.graph._turn_system_prompt` acrescenta a regra de edição nos dois prompts: current
  prevalece, valores omitidos ficam como estão, manter não apaga, terminar mostra o cartão
  e nunca se oferece cancelar a consulta para trocar serviço/médico. O bloco estático
  não inclui dados do paciente; `flow_edit_draft` continua na pseudonimização existente.
- `appointment_edit_flow.apply_ai_edit` valida catálogo da clínica, médico único sem
  ambiguidade, compatibilidade médico/serviço em todos os turnos, formato de dia/horário,
  convênio, tipos de coleção e restrições Pix. Rejeita propostas conflitantes de manter
  e substituir o mesmo campo. Somente escolhas válidas substituem current.
- `edit_step` aceita outra entrada do menu em qualquer seletor sem apagar escolhas.
  `_after_slot_affecting_change` é reutilizada pela IA: horário livre leva ao cartão;
  ocupado leva a dias/horários da agenda do novo médico, mantendo o início anterior.
- `redisplay_edit` devolve a mesma etapa, preservando current/original/stage, em saída
  inválida, sentinela antiga, desconhecimento, falha da IA ou indisponibilidade.
- `workers.orchestrator._send_bot_reply_inner` intercepta a resposta enquanto editing,
  relê a conversa depois da IA e ignora propostas contra um rascunho/etapa mais recente.
  Falha que escape da entrada de `run_agent` também redesenha a edição; fora dela mantém
  o tratamento anterior. Logs desse erro têm somente tipo e identificador, nunca conteúdo.
  Resposta informativa segura precede o cartão; sugestão de cancelar e intros antigos
  não são enviadas. Atendimento humano segue a política existente.
- `_apply_confirm` e a persistência/aviso existentes continuam sendo a única aplicação
  da edição. A IA nunca confirma a proposta. `_expire_stale_edit_state` permanece intacta.

Não foi alterado qualquer botão, lista, rótulo, tela, contrato entre serviços ou migração.
As pendências R6 de clinic_link/reminder_link não foram corrigidas nem reclassificadas.

## Evidência RED → GREEN e revisão

Resultados sanitizados em `C:/TECH/BRAIN/tasks/TASK-041/results/`:

| Evidência | Resultado |
| --- | --- |
| red.txt, antes de qualquer correção | 29 failed, 7 passed |
| red-worker.txt, antes de alterar o worker | 7 failed |
| red-extra.txt, conflito manter/substituir | 1 failed, 54 passed |
| red-review.txt, achados independentes | 6 failed, 57 passed |
| red-agent-entrypoint.txt, exceção externa da IA | 1 failed, 10 deselected |
| targeted-ai.txt, 74 testes novos finais | 74 passed |
| targeted-final.txt, novos + entrada por lembrete | 87 passed |
| full-tests.txt, snapshot final após todas as correções | 4527 passed, 68 skipped, 18 warnings; pytest 0 |
| lint-check.txt / lint-exits.json | ruff check 0 |
| lint-format.txt / lint-exits.json | format --check 1: 138 arquivos históricos |

I1 (abrir sem apagar) e I7 (expiração) já estavam corretos e passaram no RED;
não inventamos falhas nesses controles. Os novos testes fixam I1–I8, incluindo matriz
5 entradas × 4 interrupções, ferramentas v1/v2/cache, respostas inseguras, calendário
ocupado, Pix, compatibilidade posterior, ambiguidade, erros e corrida de rascunho.
A jornada worker usa o ingresso real do Portal e cartões persistidos: antes de Confirmar
não grava a consulta; após Confirmar grava uma vez e avisa o médico atual uma vez.

A revisão independente está em `tasks/TASK-041/REVIEW.md`, na raiz BRAIN. Quatro achados
foram registrados ANTES das correções: ambiguidade de médico, compatibilidade em turno
posterior, membro inválido em keep e exceção externa da IA. Todos reproduzidos em RED e
corrigidos. A decisão final da revisão é registrada pelo reviewer, separada da prova live.

A execução completa anterior ao último catch apresentou uma expectativa antiga legítima
em `test_legacy_outro_keeps_the_edit_draft_while_free_text_goes_to_the_ai`: esperava a IA
como última mensagem. Agora a informação vem seguida do cartão da etapa. O teste foi
atualizado para verificar ambas as mensagens e a preservação; não é falha baseline.
A execução prévia interrompida e a execução pré-catch não são a prova final.

## Comandos e baseline

No PowerShell, `make` não está disponível no PATH. Foram usados os equivalentes do
Makefile, com os mesmos alvos e o módulo Python para evitar imports de outro checkout:

```powershell
$env:BOT_ALLOWLIST_WA_IDS=''
$env:OPENAI_API_KEY='test-openai-key' # somente valor fictício do teste
$env:UV_PROJECT_ENVIRONMENT='C:/TECH/BRAIN/secretarIA/.venv'
$env:PYTHONPATH='C:/TECH/BRAIN-worktrees/TASK-041/secretarIA/src'
uv run python -m pytest -q
uv run python -m ruff check .
uv run python -m ruff format --check .
```

A origem importada do pacote foi confirmada independentemente pelo principal.
`99b9769` foi exportado para um snapshot isolado, sem WIP: ruff check 0 e as MESMAS
138 falhas de formatação, comparadas por conjunto de arquivos em lint-comparison.json
(new_format_failures=[]). Nenhuma formatação ampla foi aplicada. O teste legado antigo
passou nesse baseline isolado; sua expectativa foi atualizada pela mudança solicitada.

Graphify inicial INVALID: código/testes foram a evidência primária. Foi executado
`graphify update .` (0.9.5, AST-only, sem LLM/API): 607 arquivos, 12617 nós, 31568 arestas.
Diagnóstico e query saíram 0; sem missing/dangling/duplicatas/colapsos, com um self-loop
incidental. HTML foi omitido pelo limite de 5000 nós. Saída em results/graphify; apenas
os três arquivos rastreados de grafo, comprovadamente limpos antes, foram restaurados
para não juntar artefatos gerados ao diff. O índice versionado continua histórico/INVALID.
Uma tentativa prévia de saída isolada falhou com opção não suportada, exit 2, sem mutação.

Skills aplicadas: conversation-flow-state, channel-aware-dispatch,
pii-field-capture-pseudonymization, TDD, systematic-debugging e verification-before-completion.
A skill genérica criada pelo principal via skill-creator foi aplicada e validada:
`C:/TECH/.claude/skills/guided-draft-ai-edit/SKILL.md`. Sem contrato cross-service,
frozen-contract-migration não foi necessária.

## Transcript e checklist da prova

`tasks/TASK-041/results/transcript-simulado.md` contém paciente → resposta/lista/cartão
produzidos pelo roteador, incluindo Mudar data → serviço → médico → manter horário →
cartão com **O que mudou: médico.** → Confirmar. Data futura e dados de fixture explícitos.
É transcript SIMULADO: não prova OpenAI real nem Portal de produção.

- PASS automatizado: trocar serviço/médico no meio da data, sem sair da edição.
- PASS automatizado: médico livre chega ao cartão só do médico; ocupado usa a agenda nova.
- PASS automatizado: manter dia/horário/convênio conserva current, inclusive mudanças anteriores.
- PASS automatizado: ferramentas antigas ausentes; texto sugerindo cancelar não chega ao paciente.
- PASS automatizado: abrir as cinco listas não altera current/original.
- PASS automatizado: I1–I8 e suíte final; lint sem novas falhas, baseline de formato registrado.
- **BLOCKED — prova real no Portal:** não há sessão disponível nem fixture atual confirmada;
  candidato não foi deployado e deploy não está autorizado. Não executar sobre consulta real.

Para liberar prova real, publicar apenas mediante pedido explícito e confirmar o paciente de
teste do dono. Uma mudança em workers exige secretaria-worker junto da API; nunca só API.
