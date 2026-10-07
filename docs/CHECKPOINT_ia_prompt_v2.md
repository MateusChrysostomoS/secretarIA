# CHECKPOINT — IA v2: prompt e regras (TASK-030 P5)

Estado em 2026-10-07: **implementado e validado localmente**, sem commit, merge, push ou deploy.
Worktree `C:/TECH/BRAIN-worktrees/TASK-030/secretarIA`, branch
`task/TASK-030-ia-entra-em-qualquer-etapa`, base `1c1ce2c`. Nenhuma clínica foi ativada
ou configuração de produção consultada/alterada nesta execução. Deployment **NOT AUTHORIZED**.

Plano original: `docs/superpowers/plans/2026-10-02-ia-p5-prompt-e-regras.md`.
O dono autorizou adaptar o plano às capacidades atuais e melhorar as lacunas em vez de
reimplementar o agendamento. A Task 9 permanece exclusivamente do dono/operador.

## 1. O que já existia e foi preservado

- Rascunho com serviço, profissional, convênio, atendido, dia e horário; validação fresca,
  retomada após perguntas intermediárias e chegada ao cartão de confirmação (P2/P3 e captura).
- Leitura apenas de horários livres, ferramentas cegas que preparam cartões de criar/cancelar,
  isolamento de paciente/clínica e proteção de saída (P4). Não foram reimplementadas.
- Captura atual de datas no fuso da clínica e referência de 20 dias, incluindo próxima semana,
  horário já passado e datas ambíguas. A disponibilidade continua limitada a 14 dias por leitura.
- Fatos cadastrados da clínica, endereço, preparo e orientações do catálogo, inclusive de
  serviços disponíveis apenas no catálogo de uma clínica com vários profissionais (TASK-025).
- Resposta humana em `message` antes dos botões, dúvida sem reiniciar o agendamento e oferta
  de atendente com Sim/Não (TASK-038).
- Quando a clínica não oferece profissional/serviço adequado, a IA explica a diferença,
  pergunta se o paciente quer agendar mesmo assim e espera a resposta antes do handback.

## 2. A lacuna corrigida

`ai/graph.py::_turn_system_prompt` escolhe o prompt em cada chamada do modelo pelo mesmo
`_ai_toolset_v2_ctx` que determina as ferramentas. Antes, um turno v2 ainda recebia o prompt v1.
`_turn_tool_names` usa `effective_tools`, a mesma composição do agente. O log opcional de conteúdo
também usa essa escolha; nenhum log novo de conteúdo foi habilitado.

`ai/prompts_v2.py::secretary_system_prompt_v2` ensina o modelo a usar tudo que o paciente já
disse; não pedir nome de terceiro; não listar opções dos botões em texto; consultar horários
quando a ferramenta existe; apresentar no máximo três ofertas com o respectivo dia; reconhecer
informações ausentes; usar as capacidades reais de preparo/fatos e oferta humana; nunca declarar
uma nova consulta marcada, reservada, confirmada ou cancelada antes do toque do paciente.

Os nomes `start_guided_booking` e `select_professional_and_continue` são retirados somente do
conjunto v2 (`ai/tools.py::AI_TOOLSET_V2_RETIRED`). O rascunho já leva serviço e profissional.
Continuam no v1. A descrição de `set_booking_draft_v2` foi atualizada sem alterar o comportamento
nem os sete argumentos atuais, inclusive `message`.

As regras de segurança são reaproveitadas verbatim. O texto fixo não nomeia canal. O estado da
conversa tem limite de 6.000 caracteres com aviso de corte. O módulo importa somente `ai/prompts`
e biblioteca padrão em runtime; o tipo da configuração é importado apenas em TYPE_CHECKING.

## 3. Compatibilidade e orçamento medidos

`src/secretaria/ai/prompts.py`, `scripts/test_agent.py` e a avaliação v1
`tests/llm_eval/test_portal_mvp_conversations.py` ficaram intocados.
`tests/test_prompts.py` fixa os bytes do **v1 atual em 1c1ce2c**, com `now` explícito em 08/10/2026.
O pin original do plano era anterior a TASK-025/038 e usava `prompts.date`, que já não existe;
o dono autorizou atualizar essa referência, sem editar o prompt de produção.

Clínica mínima/conjunto completo testado após ajustes com modelo real: **v1 13.667; v2 11.881
caracteres** (−13,1%). Com profissional, pós-consulta, consultas e estado: **v1 19.318;
v2 13.455** (−30,3%). Teto v2 mínimo testado: 12.000. Os valores e teto antigos do plano não descrevem as capacidades
atuais preservadas. Fatos/estado dinâmicos têm seus próprios limites.

## 4. Validação e revisão

- Baseline de agendamento existente: **332 passed**.
- Validação dirigida final: **376 passed, 49 skipped**.
- Suíte completa inicial: 4151 passed/49 skipped; após testes reais e correções,
  **4165 passed, 49 skipped, 15 warnings**, 370,44 s.
- Ruff em todos os Python do escopo e `tests/llm_eval/`: clean; `git diff --check`: clean.
- Baseline de hunks de formatação preservado nos existentes: tools 7, graph 2,
  test_set_booking_draft_v2 1, test_prompts 0. Somente arquivos novos e o teste permitido
  de ferramentas v2 receberam formatação integral.
- Revisão independente somente leitura: quatro Important, nenhum Critical/Minor.
  Os quatro foram corrigidos com reproduções RED→GREEN e suíte completa verde.

Correções da revisão: preservar acordo do paciente quando a clínica não atende à necessidade;
rejeitar silêncio/resposta irrelevante nas avaliações; examinar nomes de terceiros, listas e
idioma na fala; melhorar detecção de confirmação/promessa/preço e orientação afirmativa de
emergência; validar ofertas por **dia e horário**, contando ofertas em dias diferentes.

## 5. Avaliações reais e limites

`tests/llm_eval/test_ai_v2_conversations.py`: 27 casos coletados (com parametrizações), opt-in.
Incluem os pedidos originais, ferramentas cegas, retorno de disponibilidade/erro, preparo,
fatos cadastrados/ausentes, datas atuais, idioma, injeção e especialidade não disponível.
Os verificadores ficam em `tests/llm_eval/eval_checks.py`, provados também por respostas
adversariais determinísticas em `tests/test_llm_eval_checks.py`.

O texto avaliado inclui conteúdo do modelo **e** `message` das ferramentas: a introdução ao
cartão também chega ao paciente. Há checks duros em todas as rodadas e brandos em pelo menos
80%. Modelo/limite de tokens são os de produção. As ferramentas não são executadas nesses casos.

O dono posteriormente autorizou testes reais. **27 casos com cinco repetições foram exercitados
com gpt-5-mini**. Última rodada completa: 25 passed/2 failed; os dois eram verificadores de
preço/leitura e inglês de emergência, corrigidos RED→GREEN e repetidos em subset de preço e
urgência pt/en: 3 passed. Todos os casos têm sua última verificação aprovada, mas não foi um
único run final 27/27. Os skips da suíte offline continuam deliberados.

Jornada contínua de nove passos com modelo real, clínica fictícia completa, ferramenta real de
preparo e resolvedor real passou; agenda é fixture. Chrome no link fornecido pelo dono comprovou
pedido/correção/desistência e consulta original preservada **no código publicado, sem P5**.
Relatório `C:/TECH/BRAIN/tasks/TASK-030/results/p5-real-patient-codex.md` distingue as provas.
Não houve confirmação de consulta ou chamada ao atendente, deploy, ativação ou alteração de
configuração. Houve somente as mensagens QA autorizadas no Portal.

Os testes reais motivaram reforçar intros sem opções/perguntas/anúncio de cartão, recusa de
“deixar pronto para depois”, ofertas pontuais de horários e fala direta no caso de urgência.
A descrição de `ai/staging_tools.py::cancel_event_v2` agora explicita o formato sem `(ref ...)`;
o parser e a proteção de dono continuam iguais. O preço foi avaliado também depois de executar
`get_service_info`, quando essa leitura foi a primeira decisão legítima do modelo.

Verificadores lexicais não provam toda paráfrase possível. Uma imprecisão de redação (“Sim”
antes de informar garagem gratuita em resposta a “é pago?”) ficou registrada, sem efeito em
agendamento. O piloto publicado v2 e WhatsApp QA continuam necessários; o guarda de saída
de produção para afirmações de agendamento permanece fora do P5.

## 6. Liberação — somente dono/operador

1. Autorizar integração/publicação; deployar P1–P5 com a migração prévia dos planos anteriores.
   P5 não tem migração. API e worker juntos; comprovar a identidade do código e paridade `match`.
2. Rodar as avaliações com chave só na sessão do terminal Git Bash, nunca em arquivo/log:
   `RUN_LLM_EVAL=1 LLM_EVAL_RUNS=5 BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/llm_eval -v`.
   Todo `test_ai_v2_conversations.py` deve passar. Remover a chave da sessão ao terminar.
3. Só então o dono/operador decide ativar `initial_flows.ai_draft_v2` para uma clínica.
   Não foi executado por agente. Desativar o interruptor faz o próximo turno voltar ao v1.
4. Provar Portal e WhatsApp QA: consulta para terceiro; pedido completo; pergunta de
   disponibilidade; corrigir só dia/horário; cancelar sem confirmar; fatos cadastrados;
   preparo; pedido impossível; especialidade ausente; confirmação somente por toque.
5. Acompanhar os eventos de capacidades, handback, disponibilidade, bloqueio de ferramenta e
   saída declarada, sem copiar PII. Uso de ferramenta retirada/bloqueio de saída exige investigação
   e desligar o piloto. Nenhuma consulta real é criada/cancelada só pela fala da IA.

## 7. Registro

Relatório consolidado: `C:/TECH/BRAIN/tasks/TASK-030/results/p5-completion-codex.md`.
Revisão: `C:/TECH/BRAIN/tasks/TASK-030/results/p5-review-codex.md`.
Ledger/logs preservados em `.superpowers/sdd/2026-10-02-ia-p5-prompt-e-regras/`
porque não houve commits. Branch e worktree mantidos para integração futura autorizada.
