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

## 5. Oferta de atendente com Sim/Não (pedido do dono, 2026-10-07)

Quando a IA não tem o contexto para fazer ou responder o que o paciente pediu, ela não promete mais
"vou confirmar com a equipe" (teste real T12): chama `ai/tools.py::offer_human_handoff` (com `message`
opcional — "Não tenho o valor dessa cirurgia aqui.") e o fluxo envia o cartão fixo
`flow_router.HUMAN_OFFER_BODY`: "Não sou capaz de atender essa sua necessidade por aqui. Quer que eu
chame nosso atendente humano? (Pode demorar alguns minutos)" com **✅ Sim / ❌ Não**.

- Caminho: `HumanHandoffOfferRequested` → `ai/graph.py::HUMAN_HANDOFF_OFFER_SENTINEL` (com o envelope
  `__INTRO__:` quando há fala) → `workers/shared/sentinels.py::_handle_offer_human_handoff` →
  `flow_router.enter_human_offer`. A conversa espera em `FlowState.LLM` + `STEP_HUMAN_OFFER` (a expiração
  do modo LLM limita a espera); o rascunho do agendamento é preservado.
- `flow_router._handle_human_offer` (checado em `_route` antes do ramo LLM): **Sim** → `action="handover"`
  — o mesmo caminho da escalada do "Não sei": a equipe assume e a clínica é avisada (inclusive o médico
  já escolhido: o resultado preserva os campos da conversa, como o pedido direto), depois sai "Vou te
  conectar com alguém da nossa equipe…"; **Não** → "Tudo bem! Se quiser, me conta de outro jeito o que
  você precisa, ou escolha uma opção:" com o menu; **qualquer outro texto** → a oferta cai e a IA responde.
- `request_human_handoff`: pedido explícito de pessoa e assunto clínico continuam indo direto para a
  equipe; o motivo "could_not_help" agora pergunta antes (vira a oferta).
- Prompt: item 3 do "COMO ATENDER", bloco de estado, rodapé dos fatos da clínica e `get_service_info`
  apontam para `offer_human_handoff`. Isso resolve a pendência anterior ("vou confirmar com a equipe"
  sem mecanismo).
- Testes: `tests/test_ia_atende_como_pessoa.py` §5 e
  `tests/test_bot_reply_gating.py::test_the_human_offer_card_then_yes_hands_over` (cartão → Sim → humano
  no banco). Desligar cada peça faz o teste correspondente falhar. Revisão independente: 1 achado médio
  (médico escolhido apagado antes do aviso no "Sim") corrigido com teste; sem evento de hand-back próprio
  para a oferta (só log `human_offer_presented`/`human_offer_answered`) — aceito.

## Integração com a `main` de 2026-10-07

Outra sessão publicou em `main` a captura de dia/horário/"pra quem" pela IA (8a1aaf9, `set_booking_draft`
v1 com `for_whom`/`day`/`time`). A junção manteve as duas coisas: a ferramenta v1 tem os campos dela e o
`message` desta tarefa (repassado à v2), e a exceção carrega `professional_unresolved` e `intro`.

## Validação e estado

Testes novos em `tests/test_ia_atende_como_pessoa.py` + testes do worker em
`tests/test_bot_reply_gating.py` (fala antes do cartão; fala com afirmação não comprovada descartada;
fala nunca sozinha quando a devolução não envia cartão). Cada correção foi desligada uma a uma e o teste
correspondente falhou. Suíte: 3717 passed, 0 failed (base 3671). Revisão independente: 2 achados médios
(dia abreviado com horário; fala enviada antes podia anular o pedido de desculpas de segurança) e 4
menores, todos tratados. Deploy: **API e worker juntos**, sem migração (o envelope `__INTRO__:` só é
produzido e lido pela mesma versão). Prova ao vivo no Portal pendente (depois do deploy pelo dono).
