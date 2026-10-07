# CHECKPOINT — A IA atende como pessoa; humano por último (TASK-038)

Data: 2026-10-07. Branch `task/TASK-038-ia-atende-como-pessoa` (base `main` 0ca17ac), worktree
`C:\TECH\BRAIN-worktrees\TASK-038\secretarIA`. Registro: `BRAIN/tasks/TASK-038/TASK.md`.
Origem: os 4 pontos de "O que deve ser feito" de `docs/CHECKPOINT_outro_pergunta_fixa.md` §4, que o dono
mandou executar. Vale para todas as clínicas e para os dois canais (sem interruptor).

## 1. Texto livre que não é resposta da etapa → uma rodada da IA, na MESMA etapa

`services/flow_router.py`, funções puras novas: `_reads_as_conversation` (tem "?", 7+ palavras, ou
começa com qual/quanto/como/onde/quando/vocês/"por que"/"o que"/"não sei"), `_says_no_insurance`,
`_looks_like_date_attempt`, `_bare_weekday_abbreviation`.

- **Convênio** (`_handle_insurance`): um nome de plano digitado continua gravado; "não tenho" / "não" /
  "sem convênio" / "nenhum" viram **Particular**; uma pergunta ou frase vai para a IA com
  `_preserve(..., "delegate_llm")` — a etapa e a lista continuam, nada é gravado como convênio.
  Antes: qualquer texto era gravado como convênio (teste real T4).
- **Escolha do dia** (`_handle_day_step`, agendar e remarcar): data lida → horários (igual); tentativa
  de data ilegível ("dia 35", "31/02") → "Não entendi a data" e a linha "Outro" depois de 2 erros (igual);
  qualquer outra coisa (pergunta, "sei lá", "qualquer dia") → uma rodada da IA na mesma etapa. Isso
  **reverte de propósito** a regra antiga "texto no dia nunca vai à IA" (testes antigos reescritos).
- **Dia da semana abreviado** (`_parse_day_manual`): "ter/seg/qua/qui/sex/sab/dom" só contam quando são
  a resposta (sozinhos, ou com palavras de apoio e de horário: "na sex", "qua às 10", "seg 14h", "sex de
  manhã", "ter ou qua"); dentro de frase não ("posso ter desconto" era terça — teste real T11). Nomes
  completos ("terça", "quinta-feira") seguem valendo em qualquer posição.
- **Mantidos de propósito:** o pedido do nome de quem será atendido continua só repetindo o pedido
  (o texto ali pode conter o nome de outra pessoa, que nunca vai para a IA — TASK-007); o motivo de não
  remarcar continua gravando o texto como motivo. As etapas de botão que já davam uma rodada da IA
  (médico, serviço, horário, confirmação, remarcar/cancelar) não mudaram.

## 2. A fala da IA acompanha a devolução aos botões

Antes, quando a IA devolvia o paciente aos botões, o que ela ia dizer era descartado e o paciente via
só a lista de novo (testes reais T6, T7, T9).

- `ai/tools.py`: as ferramentas de devolução ganharam o campo opcional `message` (`show_main_menu`,
  `manage_existing_appointment` v1/v2, `start_guided_booking`, `set_booking_draft` v1/v2) — e
  `plugins/multi_professional.py::select_professional_and_continue`. `handback_message` limpa e corta
  em `HANDBACK_MESSAGE_MAX` (600). As 5 exceções carregam `intro`.
- `ai/graph.py`: `with_handback_intro` põe `__INTRO__:<json>` + quebra de linha **na frente** do sinal;
  `split_handback_intro` desfaz. Sem mensagem, o sinal sai idêntico ao de antes. O texto vem dos
  argumentos da ferramenta, que a proteção de dados pessoais já devolve reidratados (`ai/pii.py`); o
  log só registra `has_intro`.
- `workers/orchestrator.py`: separa a fala antes de comparar os sinais; se o resto é um sinal, a fala
  passa pelo mesmo filtro de honestidade das respostas da IA (`guard_reply`) e fica **segurada**
  (`services/turn_safety_net.py::hold_intro`). `workers/shared/dispatch.py::_dispatch_bubbles` a põe na
  frente do primeiro lote que o turno envia — ou seja, ela sai **junto com o cartão**, nunca sozinha. Se
  a devolução acaba não enviando nada, a fala não sai, o turno fica sem resposta e o pedido de desculpas
  de segurança continua respondendo (`end_turn` descarta o que sobrou). Se o filtro a reescreveria, ela
  é descartada. `handback_message` também descarta a fala com marcador de pseudônimo não resolvido.
- As instruções pedem que a fala responda o que o paciente perguntou/contou **sem anunciar qual lista
  vem a seguir**: quem decide a próxima etapa é o fluxo (pode ser "pra quem", convênio…).
- "Não sei" (`ai/scoped_help.py`): a escolha ("pick") traz uma frase curta do porquê
  (`ScopedHelpOutcome.message`, reidratada como a pergunta de esclarecimento); `flow_router.py::
  _with_pick_message` a coloca antes do cartão escolhido, descartando-a se afirmar ação não comprovada.

## 3. "Não sei" mais humano

`PROFESSIONAL_HELP_OPENER` / `SERVICE_HELP_OPENER`: "Sem problema, eu te ajudo a escolher! É uma
primeira consulta, um retorno ou …?" — uma pergunta administrativa. Continuam **sem perguntar
sintomas** e sem o "me conta o que você precisa" que a TASK-022 tirou (puxava a IA para perguntas
clínicas); o teste dessa trava foi mantido.

## 4. Ordem de prioridade no prompt

`ai/prompts.py::_format_service_priorities` (bloco "COMO ATENDER", para toda clínica, logo após as
regras de segurança): 1º atender como secretária de verdade; 2º levar ao agendamento pelos botões,
sempre preenchendo `message` (e dizendo com franqueza quando nenhuma opção serve); 3º, por último,
chamar uma pessoa. Mais: "não ofereça o que não pode fazer neste turno". No bloco de estado: responder
a pergunta feita numa etapa com lista e convidar a escolher na lista que já está na conversa; usar
`message` ao reabrir as escolhas. Última regra reescrita ("responda com educação e traga a conversa de
volta…"). A foto do prompt padrão (`tests/golden/system_prompt_default.txt`) foi regerada: muda só o
bloco novo e essa última linha.

**Fora do escopo, decisão do dono pendente:** a regra "se a informação não estiver aqui, diga que vai
confirmar com a equipe" (`_CLINIC_FACTS_FOOTER`, TASK-025) continua; hoje nada avisa a equipe nesse
caso (teste real T12). Opções: criar um aviso real à recepção, ou a IA dizer que não tem a informação
e como obtê-la.

## Validação e estado

Testes novos em `tests/test_ia_atende_como_pessoa.py` + testes do worker em
`tests/test_bot_reply_gating.py` (fala antes do cartão; fala com afirmação não comprovada descartada;
fala nunca sozinha quando a devolução não envia cartão). Cada correção foi desligada uma a uma e o teste
correspondente falhou. Suíte: 3717 passed, 0 failed (base 3671). Revisão independente: 2 achados médios
(dia abreviado com horário; fala enviada antes podia anular o pedido de desculpas de segurança) e 4
menores, todos tratados. Deploy: **API e worker juntos**, sem migração (o envelope `__INTRO__:` só é
produzido e lido pela mesma versão). Prova ao vivo no Portal pendente (depois do deploy pelo dono).
