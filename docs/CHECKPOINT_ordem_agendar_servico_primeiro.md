# CHECKPOINT — Nova ordem do Agendar: serviço → médico → unidade → convênio → dia (TASK-088)

Estado (2026-11-21): **construído e revisado, só LOCAL. Commitado na branch `task/TASK-088` (worktree
`BRAIN-worktrees/TASK-088/secretarIA`, base `7ae1d4c` = `feature/multi-unidades-integrada`); NÃO foi para `main`, NÃO houve
push, NÃO foi deployado.** Merge, push e deploy só a pedido do dono (`Deployment: NOT AUTHORIZED`). Registro central:
`../tasks/TASK-088/` (raiz de BRAIN: `SPEC.md` = autoridade, `PLAN.md`, `results/`). O mesmo plano traz o intervalo entre
consultas ([checkpoint](CHECKPOINT_intervalo_profissional.md)), o destaque "apenas na unidade X" e a tela de Contexto
(Brain-Message-Frontend, registrado à parte). Graphify **não** foi reconstruído na branch; fica STALE até um `update`
depois do merge.

Este documento descreve o que o **código faz no HEAD** da branch. Onde um relatório de tarefa e o código divergiram, vale
o código.

## Por quê (decisão do dono, 2026-11-20)

O paciente resolve o problema dele primeiro: precisa de um **serviço**; quem faz e onde vêm depois; o convênio é a última
escolha, porque só dá para saber se ele vale depois de conhecer serviço, médico e unidade. **Isto substitui a ordem de
2026-09-23** (convênio primeiro, que filtrava tudo — ver o aviso no topo de
[CHECKPOINT_convenio_catalogo.md](CHECKPOINT_convenio_catalogo.md)). Troca direta, sem chave por clínica para voltar à
ordem antiga (abordagem 1 da SPEC).

## A ordem nova (WhatsApp e Portal, o mesmo roteador)

`pra quem → serviço → médico → unidade → convênio → dia → horário → confirmação`.

- **Menu:** "Escolher médico" saiu do menu multi-médico; sobra "Escolher serviço" e "Outro" (`menu_buttons_for`). Um
  "Escolher médico" digitado, ou um toque num cartão antigo, **continua reconhecido** (`_menu_choice_multi`,
  `BTN_CHOOSE_PROFESSIONAL`) e abre o mesmo agendamento serviço-primeiro; `greeting._menu_vocabulary` mantém o rótulo para
  nunca ser lido como nome de paciente.
- **Entrada** (`_booking_entry` / `_booking_continuation`): depois do pra-quem abre o **catálogo unificado de serviços**
  (`_enter_clinic_service_catalog`, passo `awaiting_catalog_service`) numa clínica de vários médicos, ou a lista de serviços
  do médico único (`awaiting_service`). Clínica sem serviço algum: "não há serviços" + menu, como sempre.
- **Serviço → médico:** o serviço escolhido leva à lista **só dos médicos que fazem aquele serviço**
  (`_enter_service_professional_list`, `awaiting_service_professional`); com um só médico que faça, ele já é o médico. Clínica
  de um médico só: o passo do médico some e o médico nunca é gravado na conversa.
- **Sem cartão do serviço:** o toque no serviço segue direto (`_continue_with_service`, que monta um carrinho
  `_DayPickerState` com o serviço novo e chama `_ask_day`). Ver "Cartão removido".
- **`_ask_day`** é o ponto único dos passos seguintes, na ordem: horário de atendimento do médico (alerta de configuração se
  não tem) → **unidade** (lista se 2+; Sim/Não "O serviço X feito por Y é feito *apenas na unidade Z*, em W. Você confirma
  essa unidade?" quando o médico tem 2+ unidades mas o serviço só é feito em uma; implícita se há uma; "0 unidades" como antes) → **convênio**
  (`_insurance_gate`) → dia. Quem chega aos dias (toque, IA, hand-back, conversa retomada) passa por aqui, então nenhum
  caminho pula a unidade nem o convênio.
- **Convênio só onde a clínica coleta** (`_insurance_step_skip_reason`); não coletando, vai direto ao dia como antes.
- Os dados gravados não mudaram (`flow_selected_*`); os passos mantêm os nomes.

## O filtro do convênio: (serviço, médico, unidade)

`plans_accepted_for` / `insurance_plans_for_booking` (públicas; o Portal usa a mesma função ao reconstruir a lista):

- um convênio vale na combinação quando o médico o aceita no modo da clínica (`_accepts_plan`, `insurance_mode`) **e** a
  matriz TASK-086 não recusa a célula (`is_accepted`);
- unidade ainda desconhecida: "aceito em alguma unidade" (`refuses_everywhere`); médico sem vínculo com unidade: a fatia
  "sem unidade"; sem serviço, só a lista do próprio médico julga;
- A lista de convênios inclui **Particular** e **"Outro convênio"** quando há pelo menos um plano aceito.
  Com zero planos aceitos, o fluxo decide entre oferecer outras unidades e seguir como Particular com aviso.
- escopo para o Portal: `{service, professional_id}` + `unit_id`, e `unit_marks` só quando a clínica tem alguma célula
  recusada (clínica sem exceções = escopo de sempre).

O convênio **não estreita mais as unidades** no fluxo de agendar: a unidade vem antes, e o convênio é julgado **naquela**
unidade.

## Unidade sem convênios aceitos

Quando a unidade escolhida aceita zero planos, `_insurance_gate` confere as demais unidades disponíveis para o mesmo
serviço com o mesmo médico. Se alguma aceita convênio, o passo `awaiting_no_plan_at_unit` informa a situação e mostra
"Unidades c/ convênio" e "Seguir particular".

- "Unidades c/ convênio" mostra as unidades elegíveis e os planos aceitos em cada uma. A lista tem páginas quando
  necessário e o resumo dos planos recebe reticências se ultrapassar o limite de 72 caracteres do WhatsApp. O paciente
  que escolhe outra unidade recebe a lista de convênios filtrada por ela.
- "Seguir particular" abre os dias na unidade original e grava uma escolha explícita. A opção também aparece na
  lista de unidades com convênio.
- Quando nenhuma outra unidade é elegível, o agendamento abre os dias com Particular automático após o aviso
  "Para esse serviço nessa unidade, o atendimento é particular." Se a unidade for implícita, o aviso é
  "Para esse serviço, o atendimento é particular."

## Convênio que já chegou antes da pergunta

Rascunho da IA, remarcação, conversa parada no desenho antigo, hand-back. O convênio é conferido na combinação; **nunca
vira Particular em silêncio**:

- Particular escolhido e plano aceito são mantidos. Plano digitado fora do catálogo também é mantido onde há uma
  aceitação cadastrada ou onde o paciente não escolheu unidade. Se a unidade escolhida tem zero planos, o aviso
  genérico descreve a situação sem afirmar que aquele plano digitado foi recusado individualmente.
- plano que a combinação **recusa** e há outros aceitos: a lista já filtrada, com a linha "Nessa unidade o convênio que você
  informou não é aceito para esse serviço. Escolha uma opção:" (`INSURANCE_REFUSED_LIST_UNIT`; sem unidade:
  `INSURANCE_REFUSED_LIST`) — a resposta recusada é descartada;
- plano recusado e **nenhum outro aceito naquela unidade**: "Nessa unidade esse convênio não é aceito para o serviço."
  Quando houver outra unidade elegível, segue o cartão de escolha acima. Quando não houver, segue como Particular
  automático com o aviso correspondente (`INSURANCE_REFUSED_PARTICULAR_UNIT` ou `INSURANCE_REFUSED_PARTICULAR`).
- A pergunta em si não mudou: "Você vai usar convênio? Escolha uma opção:". Log só com contagens/booleanos
  (`insurance_step_presented`, `insurance_step_particular_direct`), nunca o nome do convênio.

## Particular escolhido e automático

A decisão do dono de 2026-11-21 substitui R30. A conversa guarda `flow_insurance_auto` junto com a resposta. Atribuição
automática grava `True` e resposta do paciente grava `False`. Uma conversa anterior à migração com a marca vazia mantém
o Particular registrado.

- Particular escolhido acompanha o paciente durante o agendamento quando ele volta ou troca serviço, médico ou unidade.
  A linha "Trocar convênio" abre a lista a pedido do paciente e mantém a escolha até ele indicar outra resposta.
- Particular automático vale para a combinação que o originou. Ao voltar ou mudar a combinação, `particular_survives`
  e o roteador permitem conferir de novo os convênios. A inclusão ou remoção de uma unidade também pode mudar a combinação.
- O simulador, as retomadas do rascunho e as esperas pelo código e pelo Pix carregam a marca da conversa.
  Uma pausa no código permite retomar a mesma reserva. Ao concluir ou encerrar essa espera, a próxima marcação pode
  perguntar seu próprio convênio.

## Linhas de voltar (R18)

- **Lista de convênios** (última linha, também fixa no Portal): "⬅️ Outra unidade" (`dayback|unit`) quando o paciente
  escolheu numa lista de unidades; senão "⬅️ Outro serviço" (`dayback|service`). A lista é paginada com `reserved=1` para
  nunca passar de 10 linhas.
- **Seletor de dia e de horário** (e as reofertas de `_handle_confirmation`): "⬅️ **Trocar convênio**"
  (`dayback|insurance`, `BACK_TARGET_INSURANCE`, 18 caracteres) quando a clínica coleta convênio e a combinação aceita ao
  menos um; senão "⬅️ Outra unidade" / "⬅️ Outro serviço" como antes (`_day_back_target`, que usa a mesma `_accepted_plans`
  do gate — a pergunta e o caminho de volta não divergem). O rótulo é "Trocar convênio" e **não** "Outro convênio" de
  propósito: o roteador compara rótulos sem o emoji e esse é o nome da linha "Outro convênio" da lista. Ele também coincide,
  sem a seta, com o botão do cartão de "convênio não aceito" (`LABEL_PLAN_CHANGE_INSURANCE`); os dois nunca aparecem no
  mesmo passo e ambos significam "perguntar o convênio de novo".
- Tocar na linha de voltar da lista chama `_handle_day_back` e **não** retoma rascunho da IA (voltaria à mesma pergunta).
  A escolha explícita de Particular acompanha a volta. As demais respostas seguem as regras de aceitação e retomada.
- Numa clínica de vários médicos, "⬅️ Outro serviço" do agendamento reabre o **catálogo da clínica** (médico e serviço
  limpos); a re-marcação após cancelamento da clínica tem o próprio alvo (`BACK_TARGET_DOCTOR_SERVICES`, "doctorservice",
  mesmo rótulo), que reabre a lista de serviços do médico candidato como sempre foi.
- Uma linha "Trocar convênio" velha (a combinação já não aceita nada) leva um passo mais para trás em vez de desenhar uma
  lista só com Particular (`_day_back_result`).

## Rascunho da IA e hand-back

O resolvedor anda **na mesma ordem dos botões**, pelos mesmos pontos únicos (`flow_router._ask_day`, `_insurance_gate`),
e para na primeira pergunta aberta: `pra quem → serviço → médico → unidade → convênio → dia → horário`.

- Serviço: multi-médico = a lista do médico que a IA nomeou, senão o catálogo; médico único = a lista de serviços dele.
- Médico: só quando há mais de um, ou quando a IA nomeou um médico que não resolveu
  (`_enter_service_professional_list`, só quem faz o serviço); nunca implícito depois de um médico descartado.
- `_land_day` estaciona o rascunho ao perguntar a unidade, mostrar a lista de convênios ou oferecer a troca de unidade
  por falta de cobertura. Se a combinação só permitir Particular, explica a escolha automática antes de continuar.
  A resolução de data e horário vem depois dessas etapas.
- **Estacionamento:** `DRAFT_WAIT_STEPS` ganhou `awaiting_catalog_service`, `awaiting_service_professional`, `awaiting_unit`
  e `awaiting_unit_confirm`. Ganchos de retomada em `_catalog_step`: resposta do catálogo, do médico-do-serviço, escolha da
  unidade (e o "Sim") e resposta do convênio; `_resume_parked_draft` também não retoma quando o resultado fica no mesmo passo
  de uma página "Ver mais". Linhas de voltar, "Não" e texto livre **nunca** retomam. `draft_resolution._fold_answer` dobra a
  resposta do catálogo em serviço (+ médico), a do médico-do-serviço em médico, e copia a unidade escolhida (sem copiar o
  Particular automático do resultado — o resolvedor decide de novo, com a linha).
- **Hand-back** (`enter_guided_booking`, clínica de um médico): sempre `_ask_day` — unidade (2+) → convênio → dias. Convênio
  conhecido: mantido se aceito, perguntado de novo com linha se não. Um "Particular" guardado só sobrevive por
  `particular_survives` (`recorded=` o retrato da conversa, `named_now=`); sem serviço (clínica sem catálogo) não se toca no
  Particular, e quem julga é o gate com a lista do próprio médico.
- **Pouso legado** (chave da IA v2 desligada, `workers/shared/sentinels.py`): rascunho só com seleção em clínica de vários
  médicos → catálogo da clínica, ou os médicos do serviço já escolhido; serviço sem médico → lista de médicos do serviço
  (antes: menu / `missing_professional`). O Particular dito agora é mantido; o resto passa por `particular_survives`.
- **Devoluções da IA (revisão final):** `ai/tools.py::_slot_taken_hand_back` (horário tomado/dentro do intervalo na hora de
  gravar) devolve o rascunho **sem** convênio — o resolvedor lê o guardado com `named_now=False`, então um Particular
  automático não vira "dito agora". `workers/shared/sentinels.py::_handle_select_professional` (escolher médico, chave v2
  desligada) abre a lista de serviços do médico e por isso aplica `particular_survives(..., opens_choice=True)`.

## Agenda do médico escolhido no mesmo toque (re-execução no worker)

Sem o cartão do serviço, um toque que **escolhe o médico** (catálogo com um só médico que faz o serviço, médico da lista
"quem faz este serviço") vai direto à lista de dias **no mesmo turno**. O worker resolve a agenda do turno **antes** do
roteador, pelo médico já gravado (`workers/shared/llm_context.py::_flow_turn_calendar`): sem médico gravado, é a agenda da
clínica — horários, ocupados, dono dos holds e do intervalo errados.

- O roteador continua puro: `_ask_day` marca no resultado `FlowRouterResult.agenda_owner_id` (nunca gravado) quando chegou a
  `enter_day_picker` — dias, calendário, "sem dias" ou "agenda indisponível".
- `flow_router.calendar_owner_changed(conversation, result)`: dono marcado, diferente do médico gravado, e o turno não começou
  num passo de ajuda com IA ("Não sei"). Então `workers/shared/flow_runner.py::_run_flow` resolve a agenda daquele médico como
  o próximo turno resolveria (`resolve_professional_calendar`, linha ativa da própria clínica) e roda `route()` **uma vez**
  de novo com o mesmo retrato, mensagem e portão (log `flow_turn_rerun_on_picked_doctor_calendar`, só ids). Nunca duas
  chamadas de IA no mesmo turno; turnos de gerenciar consulta não trocam de agenda; rascunho estacionado é retomado depois da
  re-execução. Falha ao resolver a agenda, ou a re-execução levantar erro (`worker_picked_doctor_rerun_failed`, tipo do erro
  + ids) → roda com agenda nenhuma = "agenda indisponível" (atendente), nunca a IA no lugar.
- O simulador da configuração (`workers/shared/flow_simulation.py::simulate_turn`) faz o mesmo com `calendar_for(médico)`.
- Custo: o primeiro `route()` lê a agenda da clínica à toa nesses toques (latência, sem efeito colateral).

## Remarcação (R19, `appointment_edit_flow.py`)

Trocar médico, serviço ou unidade **com o convênio da consulta inalterado e recusado pela célula nova**: o lugar escolhido
fica; havendo planos aceitos, aparece a lista filtrada do agendamento (`_enter_insurance(with_back=False)`, corpo
`INSURANCE_REFUSED_LIST[_UNIT]`; o escopo do Portal é o do agendamento, então `list_options` reconstrói idêntico); nenhum
aceito → o rascunho vira Particular e o cartão traz `INSURANCE_REFUSED_PARTICULAR[_UNIT]` por cima. Listas de unidade da
remarcação **não** são mais estreitadas pelo convênio. Um convênio que o paciente **escolhe** na remarcação e é recusado
mantém o "recusa e reverte" de antes; a checagem velha da matriz no "Confirmar" e a recusa na hora de aplicar não mudaram.
`appointment_edit.EDIT_INSURANCE_NOT_ACCEPTED` e `EDIT_ROW_INSURANCE_REFUSED` seguem em uso.

## Cartão do serviço removido

Sumiu "Deseja agendar esse serviço?" (e o valor que ele mostrava). O toque no serviço — lista do médico, catálogo, médicos do
serviço, escolha da ajuda "Não sei" — vai direto ao próximo passo. A **descrição longa** do serviço deixou de aparecer no
fluxo de botões (R25): a confirmação final é um cartão Confirmar/Cancelar de no máximo 1024 caracteres e leva as linhas de
dinheiro; a descrição pode ter 2000. O caminho da IA segue mandando a mensagem de detalhes (descrição + o que levar), agora
**sem preço** ("Serviço: Consulta", não "— R$250").

## Valor só na confirmação final (R23–R26, R38)

- **Meio do fluxo não diz dinheiro nenhum:** linhas de serviço sem preço; a linha da unidade (D3, depois do toque na unidade)
  diz **só a duração** e só quando a unidade leva outro tempo que o base ("Consulta na Unidade Centro: 40 min",
  `UNIT_TERMS_LINE`, uma vez por escolha); a mensagem de detalhes da IA sem preço; o nó de ajuda "Não sei" recebe os serviços
  com `price: None` e o bloco de opções (`ai/scoped_help.py::_service_options_block`) é só nome + descrição, sem "valor
  depende da unidade" e sem leitura de preço no banco. (`PRICE_DEPENDS_ON_UNIT` segue só na lista de serviços do agente.)
- **Cartão de confirmação final** (recapitulação como sempre + linhas no fim, `with_money_lines`: o dinheiro nunca é cortado,
  a recapitulação é; bloco separado por linha em branco; segunda passada não muda nada):

  | Caso | Linhas no fim do cartão |
  |---|---|
  | Particular (ou clínica sem passo de convênio) | `Valor: R$ 200,00` (`booking_price_line`: preço na **unidade escolhida**, `_priced_entry` + `service_price_text`) |
  | Convênio, sem sinal | nenhuma |
  | Sinal ligado e **cobrado**, antes ou depois do atendimento, Particular **ou convênio** (D-R12: convênio nunca isenta o sinal) | `Sinal (Pix): R$ 60,00` + `Valor da consulta: R$ 200,00` (substituem "Valor") |
  | Sinal ligado mas **não cobrado** (preço abaixo do piso de R$ 5,00 do Asaas, sem conta/conta não verificada, sem preço) | como sem sinal |
  | Serviço sem preço legível ("a combinar", 0) | nenhuma linha |
  | Leitura do sinal falha | só a linha do roteador (ou nenhuma); aviso com id e tipo do erro |

- **Quem calcula o sinal:** `workers/shared/deposit.py::_with_booking_money`, chamada logo no início de
  `_apply_deposit_awareness` (o ponto único por onde passa toda confirmação nova: Portal e WhatsApp, toque no horário, cartão
  expresso da IA, cartão retomado) — o roteador não faz I/O de banco. Valor = `deposit_lifecycle.booking_sinal_preview`
  (`requote_quote` + as proteções de "dá para criar a cobrança"), a **mesma aritmética** que o portão `before` e o gancho
  `after` usam (teste de paridade com `quote_deposit` e `requote_amount_cents`); nunca uma segunda cópia da conta. O **dono**
  do agendamento é resolvido pela regra do hold/da linha (`resolve_booking_owner_id` sobre o elenco ativo), **não** pelo
  médico selecionado do roteador (que é `None` em clínica de um médico) — senão o preço sairia do catálogo legado e o preço
  próprio da unidade seria ignorado.
- Só para **novo agendamento** em `awaiting_confirmation` sem consulta gerenciada (cartão de remarcação/edição não é isto).
  Repasse de sinal pago pendente (`release_offer.carry_over_pending`, consultado depois da prévia): sem linhas de sinal; o
  cartão fica com a linha "Valor" do roteador.
- Limite conhecido: a ajuda "Não sei" gera uma frase livre do modelo; sem preço nos dados, ela não tem número para citar, e as
  regras dela (`ai/scoped_help.py::_SCOPED_RULES`) dizem "não informe valores; o valor aparece na confirmação final" — mas um
  texto do modelo não é garantia formal.
- A conversa livre pode informar o preço cadastrado quando o paciente pergunta. Os dois prompts orientam a IA a informar
  que a clínica confirma a cobertura, a coparticipação e a cobrança conforme o convênio. A IA pode repetir condições
  já informadas pela clínica e deve evitar prometer cobertura ou valor.

## Conversas paradas no deploy (compatibilidade)

Nomes de passos e dados não mudaram; nenhum passo antigo perdeu o tratador.

- `awaiting_service_confirm` (o cartão que sumiu): "Sim, agendar" → `_ask_day`; "Outro serviço" → lista de serviços; texto
  livre como antes. Retomada por lembrete (`_resume_bubbles`) **não redesenha o cartão**: segue por `_ask_day`.
- `awaiting_insurance` com serviço vazio (a antiga primeira pergunta): a resposta é guardada e julgada depois, e o agendamento
  segue pela entrada nova (catálogo / lista do médico único); clínica de vários médicos sem serviço → "não há serviços" +
  menu, nunca um dia sem tipo.
- `awaiting_insurance` com médico já escolhido: os serviços dele; com serviço sem médico: os médicos daquele serviço.
- `awaiting_professional` (lista de médicos antiga) e `awaiting_catalog_service` / `awaiting_service_professional` continuam
  tratados.
- `awaiting_plan_refused` (o cartão de "convênio não aceito") **continua existindo**: `_enter_plan_refused` ainda é alcançado
  por uma linha de serviço recusada com convênio conhecido e pela guarda final quando o passo do convênio está desligado.
  Uma conversa parada ali que toca "Trocar convênio" volta a perguntar o convênio, com a linha.
- Guarda final no "Confirmar" (`_booking_insurance_refusal`, assíncrona, recebe o `calendar`): matriz mudou durante o
  agendamento → nada é agendado, aviso, e a pergunta volta por `_ask_day` (lista do que é aceito agora, ou Particular + linha),
  **mantendo a unidade**; dia e horário se perguntam de novo. Pós-código/Pix
  (`workers/shared/booking_hold.py::_insurance_refused_reoffer`): mesma ordem; o texto "Vamos escolher o convênio de novo."
  só aparece na frente de uma lista; sem conversa para reconstruir, a mensagem curta já não promete uma pergunta que não vem
  (`_INSURANCE_REFUSED_NO_REOFFER_MESSAGE` usa só o começo do aviso).
- Como a guarda final e as perguntas reabertas passam por `_ask_day`, um médico **sem horário** configurado recebe ali o
  alerta de configuração (correto em produção).

## O que foi removido (código sem chamador na ordem nova)

Cada item foi conferido por grep em `src` e `tests` antes de sair; o que ainda tem chamador ficou (abaixo).

- `flow_router`: `_service_detail_text`, `_enter_service_detail`, `_service_card_unit_id` (o cartão do serviço); a linha da
  unidade com preço (`UNIT_TERMS_LINE` antiga e o parâmetro `price` de `_unit_terms_text`, hoje `UNIT_TERMS_LINE` só de
  duração); a variante com convênio do Sim/Não de unidade (`convenio=` de `_enter_unit_choice`/`_enter_unit_confirm`); o
  parâmetro `professional` sem uso de `_handle_service_help`.
- `flow_units`: `UNIT_CONFIRM_PLAN_BODY`, `UNIT_CONFIRM_PLAN_BODY_NO_ADDRESS` (+ exports) e o `convenio=` de
  `unit_confirm_body` — "Pelo convênio X ... apenas na unidade Y" não existe mais (a unidade vem antes do convênio).
- `booking_draft`: `_plan_refused`, `DRAFT_PLAN_NOT_ACCEPTED_PREFIX` (o gate substituiu o aviso A8 da TASK-086).
- `appointment_edit_flow`: `_units_taking_plan`, `_doctor_units_taking_plan`; `appointment_edit`: `EDIT_INSURANCE_NOT_AT_UNITS`.
- Testes que só testavam isso: os do cartão do serviço (`test_booking_details`, `test_flow_router`), o do texto "Pelo convênio"
  (`test_t086_flow_matrix`); `test_unit_terms_t8_review` passou a afirmar a linha de unidade sem dinheiro.

**Ficou, de propósito** (ainda alcançável, em especial por conversa parada): `STEP_AWAITING_SERVICE_CONFIRM` + tratador +
`LABEL_BOOK_SERVICE` / `LABEL_OTHER_SERVICE`; `STEP_AWAITING_PLAN_REFUSED`, `_enter_plan_refused`, `_handle_plan_refused`;
`_plan_kwargs` e o `by_plan` de `UnitChoice` (usados por `_no_unit_alternatives`); `_priced_entry` (agora do
`booking_price_line`); `DETAILS_SEEN_STEPS`, `llm_context` e os ramos de `_handle_insurance` para conversa sem serviço.

Textos históricos que ainda citam nomes removidos (`CHECKPOINT_day_picker_entry_buttons.md`,
`CHECKPOINT_gicovate_feedback_2026-11-09.md`, `CHECKPOINT_trio_gerenciar_scoped_help.md`, `docs/superpowers/`) são registro
datado e **não** foram reescritos.

## Deploy (NÃO autorizado)

1. Aplicar as migrações **antes** do código. A cabeça **`f9a1d7c4e2b6`** adiciona `conversations.flow_insurance_auto` e
   sucede `d4b7a9c2e6f1` (`professionals.buffer_minutes`).
2. **API e worker da secretarIA juntos:** `flow_router.py` e o caminho de confirmação (`workers/shared/deposit.py`) mudaram, e
   são dois serviços de deploy separados — só a API não move o worker.
3. Brain-Message-Frontend (campo do intervalo, Contexto) segue o registro do próprio repo; a ordem nova do fluxo em si não
   exige front novo (o Portal usa o mesmo roteador e `list_options`).
4. Provas no navegador/WhatsApp real ficam para antes do merge (clínica de teste Chrysostomo For Eyes).

## Limites conhecidos

- A oferta de unidades com convênio faz parte do agendamento inicial. Na recuperação de uma reserva cujo convênio
  deixou de ser aceito durante a verificação por código, o Portal reabre a escolha de unidade antes de conferir o convênio.
- **P5 (re-marcação após cancelamento da clínica):** o alvo de voltar é recalculado a cada turno em `_catalog_step`; depois de
  um toque de dia, a lista de horários mostra `dayback|service` / `dayback|insurance` em vez de `dayback|doctorservice`.
  Corrigir pede um marcador gravado na conversa (não há coluna).
- **`carry_over_pending`** é lido depois da prévia do sinal; repasse pendente deixa o cartão só com a linha "Valor".
- Descrição longa do serviço não aparece mais nos botões (R25).
- O intervalo entre consultas tem os próprios limites em [CHECKPOINT_intervalo_profissional.md](CHECKPOINT_intervalo_profissional.md).
- A extração da IA do Contexto continua sem preencher unidades, horários de funcionamento nem o intervalo (SPEC, "Extração
  da IA").

## Testes

Arquivos novos: `tests/test_booking_order_service_first.py` (ordem, filtro, Particular direto, convênio conhecido, voltar),
`tests/test_booking_order_compat.py` (rascunho da IA, hand-back, legado, remarcação, conversas paradas, `particular_survives`),
`tests/test_service_no_detail_card_price_at_end.py` (sem cartão, valor no fim, sinal, ajuda sem preço, paridade com o portão de
  sinal). A2 acrescenta `tests/test_particular_choice_sticks.py`, `tests/test_migration_conversation_insurance_auto.py` e
  `tests/test_units_with_insurance.py`. Rodar com `BOT_ALLOWLIST_WA_IDS=""` e
  `uv run python -m pytest ... -q -p no:cacheprovider`. O `Settings` é
congelado no primeiro import (`core/database.py`); um arquivo coletado primeiro **sem** `OPENAI_API_KEY` no bloco de ambiente
do topo o congela sem provedor de IA e faz testes de IA coletados depois falharem com "ANTHROPIC_API_KEY missing" — por isso
`test_booking_buffer.py` e `test_booking_buffer_paths.py` fazem `os.environ.setdefault("OPENAI_API_KEY", ...)` como os demais.
