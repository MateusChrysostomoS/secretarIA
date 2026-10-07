# CHECKPOINT — "Outro" responde a pergunta fixa; testes reais dos outros pontos de entrada da IA (TASK-037)

Data: 2026-10-06. Branch `task/TASK-037-outro-pergunta-fixa` (base `main` 01f2af7), worktree
`C:\TECH\BRAIN-worktrees\TASK-037\secretarIA`. Registro da tarefa: `BRAIN/tasks/TASK-037/TASK.md`.

## 1. O que mudou (código)

Pedido do dono: tocar "Outro" no menu (inclusive na primeira mensagem do Portal para paciente já
cadastrado) não deve acionar a IA; deve só perguntar "O que te traz à clínica?" e a IA começa na
resposta do paciente, com essa pergunta no histórico como contexto.

- `services/flow_router.py`: constante `OTHER_OPENER` e helper `_enter_other`. Os três caminhos de
  "Outro" do menu — o rótulo `LABEL_OTHER` em `_route` (vale para os cartões [Agendar, Outro] e
  [Remarcar, Cancelar, Outro]), o 3º botão do menu de vários médicos (`_menu_choice_multi`) e o 3º
  botão configurado do menu de um médico (`_enter_menu_choice`) — devolvem `action="reply"` com a
  pergunta fixa e `flow_state=LLM` (mesmos campos de rascunho que `_delegate_llm_keeping_draft`
  preservava). O turno seguinte, já em LLM, chega ao modelo normalmente.
- Fica igual, de propósito: a linha "Outro" da escolha de dia depois de duas datas não entendidas
  (`dayescape|0`) continua passando direto para a IA (o paciente está no meio do agendamento); o
  "Outro" do cartão de lembrete (TASK-032) tem tratamento próprio.
- `services/llm_context.py::_pra_quem_line`: quando "pra quem" ainda não foi respondido, a ficha
  da IA agora diz "Não pergunte isso você: o fluxo guiado pergunta na hora de marcar." Essa linha
  era a origem do "Essa consulta é pra você?" como abertura. Comprovado ao vivo que o fluxo pergunta
  "pra quem" sozinho quando a IA devolve o agendamento (seção 2, T3).
- Vale para os dois canais (WhatsApp e Portal): o roteador é neutro de canal.
- Deploy: toca `services/flow_router.py` → exige **API e worker** (regra do `CLAUDE.md`). Sem migração.

Testes novos/alterados: `tests/test_flow_router.py` (pergunta fixa, 3º botão configurado, próximo
turno vai à IA), `tests/test_bot_reply_gating.py::test_outro_tap_sends_fixed_question_without_calling_the_llm`
(contra banco real: `run_agent` não é chamado, sai só a pergunta, estado gravado = LLM),
`tests/test_llm_context.py::test_an_unanswered_pra_quem_tells_the_model_not_to_ask_it`. Prova de que
mordem: com o ramo antigo restaurado, os 3 testes de "Outro" falham. Números da suíte em
`BRAIN/tasks/TASK-037/TASK.md`.

## 2. Testes reais (produção, clínica de teste, Portal) — versão no ar 01f2af7

2026-10-06, 23:26–23:42 BRT, Chrysostomo For Eyes, sessão de paciente de teste já autenticada.
Nada foi confirmado nem cancelado; a conversa terminou em `/menu`.

| # | Onde | O que o paciente fez | O que aconteceu |
|---|---|---|---|
| T1 | Menu | Tocou "Outro" (2×) | 1ª vez: a IA reenviou o próprio menu ("Como posso te ajudar?" + Agendar/Outro). 2ª vez: "O que te traz à clínica?". Com o relato do dono ("Essa consulta é pra você?"), são 3 aberturas diferentes para o mesmo toque. Corrigido na seção 1. |
| T2 | Conversa livre | "Estou enxergando embaçado de perto… o que recomendam?" | Acolheu, disse que não diagnostica, ofereceu consulta de avaliação. OK. |
| T3 | Conversa livre | "Sim, quero marcar" | O fluxo perguntou "Essa consulta é pra você?" sozinho. OK. |
| T4 | Pergunta de convênio | "Antes disso, qual o endereço da clínica?" | **Defeito:** o texto foi gravado como convênio e o fluxo pulou para "Com qual profissional…". Pergunta ignorada. Causa: `_handle_insurance` guarda qualquer texto (`stored = (matched or body.strip())[:120]`). |
| T5 | Lista de médicos → "Não sei" | Tocou "Não sei" | Abertura fixa robótica: "Posso explicar as opções de profissionais da clínica e como agendar. Sobre qual profissional ou etapa do agendamento você quer saber?" |
| T6 | Ajuda de médico | "Estou com a vista embaçada pra ler de perto, não sei qual médico é o certo" | **Defeito de experiência:** voltou a mesma lista de médicos, sem nenhuma explicação (a clínica de teste só lista Clínica Geral e Cardiologia nesse percurso). |
| T7 | Lista de serviços → "Não sei" | "Nunca vim aí… só queria uma consulta normal… quanto custa?" | Mesmo padrão: voltou a lista de serviços calada (só "Cirurgia de Catarata"), sem responder preço nem dizer que não há consulta comum com esse médico. |
| T8 | Lista de serviços (texto livre) | "Vocês atendem no sábado?" | Respondeu bem (horário real da clínica). Mas terminou com "Quer que eu verifique horários… Qual dia prefere?" |
| T9 | Lista de serviços (texto livre) | "Pode ser quinta de manhã" | **Promessa quebrada:** voltou a lista de serviços calada; a preferência se perdeu. |
| T10 | Escolha do dia | "Vocês têm estacionamento aí?" | **Defeito:** "Não entendi a data." Pergunta ignorada. |
| T11 | Escolha do dia | "Posso ter desconto se pagar à vista?" | **Defeito:** "Horários livres em 13/10" — a palavra "ter" foi lida como terça-feira. |
| T12 | Lista de horários (texto livre) | "Quanto custa essa cirurgia?" | Disse que não tem o valor e "posso confirmar com a equipe e te responder em seguida" — nada no sistema avisa a equipe nesse caso (promessa sem lastro). Re-perguntou o convênio. |

Nenhum teste levou a transferência para humano. Evidência: textos copiados da conversa nesta
sessão (sem links, sem dados pessoais além do primeiro nome fictício da conta de teste).
Observação lateral: a mensagem pós-consulta configurada pela clínica cita um médico fixo logo abaixo
de "Como foi a sua consulta… com <outro médico>?" — texto da clínica, não código.

Ferramenta: a aba do Chrome ficou "escondida" (o navegador não renderiza nem processa digitação);
mensagens enviadas pelo próprio formulário da página (`requestSubmit`) e leitura por recarga. Não
classificar demora de exibição desta rodada como defeito do Portal.

## 3. Mapa: texto livre em cada etapa (leitura de código, main 01f2af7)

- Uma rodada de IA ficando na mesma etapa: pra quem (escolha), autorização, médico, serviço,
  detalhe do serviço, horário, confirmação, todas as etapas de remarcar/cancelar exceto o dia.
- Vira conversa livre (LLM fixo): texto no menu; "Outro" na escolha de dia (depois de 2 erros);
  ajuda do "Não sei" que não decide.
- Nunca chega à IA: convênio (engole o texto — T4), dia ("Não entendi a data" duas vezes, depois
  linha "Outro"; leitura de dia da semana pega "ter/seg/qua…" dentro de frases — T11), nome de quem
  será atendido (repete o pedido sem limite), motivo de não remarcar (grava como motivo).
- Transferência direta para humano sem IA: só falha de agenda e o add-on de atendimento humano fora
  do horário — nunca por causa do texto do paciente.

## 4. O que deve ser feito (recomendação — os itens 1, 2, 3 e 5 foram EXECUTADOS na TASK-038, ver `docs/CHECKPOINT_ia_atende_como_pessoa.md`; o item 4 aguarda decisão do dono)

1. **Regra única para texto livre em qualquer etapa:** se é uma resposta válida da etapa, segue; se
   não é, a IA responde como pessoa e devolve a MESMA pergunta/botões, sem perder nada. Falta em
   convênio (T4 — também corrompe o dado gravado), dia (T10/T11), nome do atendido, motivo.
2. **A fala da IA tem que acompanhar a devolução ao fluxo.** Hoje, quando a IA devolve o paciente aos
   botões, o que ela ia dizer é descartado e o paciente só vê a lista de novo (T6, T7, T9).
3. **"Não sei" mais humano:** abertura do tipo "Sem problema! Me conta o que você está sentindo ou
   procurando que eu te ajudo a escolher" e, quando nenhuma opção serve, dizer isso com franqueza.
4. **Sem promessas sem lastro** ("vou confirmar com a equipe", "posso verificar horários") — ou
   existe um aviso real à recepção, ou a IA diz que não tem a informação e como obtê-la (decisão do dono).
5. **Ordem de prioridade escrita no prompt, para toda entrada da IA:** 1º atender a necessidade como
   uma secretária de verdade; 2º levar ao agendamento quando fizer sentido; 3º e último, chamar uma
   pessoa (pedido explícito, assunto que exige avaliação humana, ou depois de tentar de verdade). A
   parte "último recurso" já existe no prompt e na ferramenta de transferência; falta a 1ª parte.

Sobreposição: a TASK-030 tem P2b (rascunho com dia/horário), P4 e P5 (prompt novo com avaliações)
pendentes; os itens 2–5 cabem no P5 ou numa tarefa própria. O item 1 (convênio) é defeito e
independe deles.
