# CHECKPOINT — Seletor de dia/horário reaproveitável + entrada determinística

> Estado: **BUILT + suíte verde, NÃO commitado, NÃO deployado** (2026-08-17).
> Escopo: `secretarIA` (backend). Nenhuma mudança de frontend, nenhuma migração.

`PROMPT_FEAT_32`. Continuação direta de `CHECKPOINT_trio_gerenciar_scoped_help.md` e
`CHECKPOINT_multi_doctor_flow.md`, sob o mesmo princípio de produto: **a LLM é o último
recurso, nunca o padrão** — e onde ela ainda entra, entra escopada, ancorada e contada.

Esta rodada fecha a **maior fonte isolada de vazamento para a LLM que restava** no fluxo
determinístico (a pergunta de dia em texto livre) e elimina dois becos sem saída na
entrada (um botão que só sabia dizer "você não tem consulta" e um botão que parecia
determinístico mas era a porta da LLM).

Suíte: **1511 passed** (`.venv/Scripts/python.exe -m pytest -q`). `ruff check .` continua
com as **mesmas 8 falhas pré-existentes** (3× `UP042` de enums + 5× `E501` em arquivos que
esta rodada não tocou) — nenhuma nova, nenhuma mascarada.

## Pré-requisito: FIX_16 primeiro (e por quê)

A Parte A tira `Gerenciar consulta` do cartão de saudação de quem **não tem consulta
futura**. Antes do `PROMPT_FIX_16` (commit `4df2460`, já em `main`), um paciente que tinha
**remarcado** aparecia para o sistema como "sem consulta futura" — então tirar o botão
antes do FIX_16 teria deixado exatamente esses pacientes sem nenhuma porta para gerenciar
a consulta deles.

Regressão que fixa a ordem: `tests/test_appointment_status_taxonomy.py::
test_rescheduled_booking_still_gets_the_manage_trio` — uma consulta `RESCHEDULED` produz
`[Remarcar, Cancelar, Outro]`, não `[Agendar, Outro]`.

## Restrições duras da WhatsApp Cloud API (o que o desenho tem de respeitar)

Já codificadas em `ai/formatter.py` — **não são negociáveis**:

| Constante | Valor | Consequência no desenho |
|---|---|---|
| botões de resposta | **3** | nenhum cartão passa de 3 botões |
| `MAX_LIST_ROWS` | **10** | o seletor de dia pagina de 8 em 8 + 2 linhas de controle |
| `MAX_BUBBLES_PER_TURN` | 4 | a saída "sem disponibilidade" usa 2 bolhas |
| `MAX_LIST_BODY_CHARS` | 1024 | o cabeçalho do médico é cortado nesse teto |

> **Correção de premissa registrada:** a ideia original previa "17 dias úteis + Outro" =
> 18 opções numa lista. **Isso não existe na API.** O que entregou a mesma intenção — o
> paciente escolhe o dia sem digitar — foi paginação de 8 dias dentro do teto de 10.

## Parte A — entrada determinística

### Cartão de saudação (`workers/tasks.py::_greeting_buttons_for`)

| Estado do paciente | Antes | Agora |
|---|---|---|
| Sem consulta futura | `[Agendar, Gerenciar consulta, Outro]` | **`[Agendar, Outro]`** |
| Com consulta futura (`HAS_UPCOMING`/`_SOON`) | `[Remarcar, Cancelar, Outro]` | inalterado |
| Sem greeting | `[]` | inalterado |

`Gerenciar consulta` saiu **só do cartão**: para quem nunca marcou nada ele só podia
chegar no beco `_enter_manage` → *"Você não tem nenhuma consulta agendada no momento."*, e
quem **tem** consulta já era servido pelo trio Remarcar/Cancelar. `LABEL_MANAGE_APPOINTMENT`
continua sendo entrada de primeira classe do `route()` (thread antiga com o botão ainda
tappável; digitar o texto continua funcionando) e continua em `_GREETING_ACTION_IDS`.

### Cartão de menu multi-médico (`flow_router.py::menu_buttons_for`)

| Antes | Agora |
|---|---|
| `[Escolher médico, Procurar médico, Outro]` | **`[Escolher médico, Escolher serviço, Outro]`** |

`Procurar médico` enviava um opener fixo e jogava a conversa em `FlowState.LLM` — um botão
que **parecia** determinístico e era a porta da LLM. `BTN_FIND_PROFESSIONAL` e
`FIND_PROFESSIONAL_OPENER` foram **removidos**.

`Escolher serviço` é a imagem espelhada de `Escolher médico`:

1. `_enter_clinic_service_catalog` monta o catálogo **unificado** da clínica — união dos
   `professional_appointment_types(p, tenant)` de todos os profissionais ativos,
   deduplicada por nome canônico, ordenada por `sort_order`/nome. Isolamento por tenant é
   estrutural: a união vem da roster ativa DESTE tenant e todo fallback é para o
   `appointment_types` DESTE tenant.
2. `_handle_catalog_service` filtra quem oferece o serviço escolhido:
   - **exatamente 1** → cartão de detalhe daquele serviço, já com
     `flow_selected_professional_id` e `flow_selected_type` gravados (os dois ramos
     convergem um passo depois);
   - **2+** → `STEP_AWAITING_SERVICE_PROFESSIONAL`, a lista filtrada de médicos; o toque
     cai direto no cartão de detalhe, **não** de volta numa lista de serviços que o
     paciente já respondeu;
   - **nenhum** → mensagem fixa + menu (guarda defensiva: com uma roster consistente é
     inalcançável, já que a união é construída a partir dela mesma).
3. Nenhum passo aqui chama a LLM.

**Desvio consciente do prompt (§4.3 item 2):** o caso "exatamente 1 profissional" vai para
o cartão de confirmação do serviço, **não** direto para o ramo de dia/horário. Motivo
técnico duro: `route()` recebe o `calendar` **já resolvido** pelo worker a partir do
`flow_selected_professional_id` da conversa (`_flow_turn_calendar`). Um profissional
escolhido no meio do turno ainda não está na conversa, então pular para o seletor de dia no
mesmo turno o montaria na agenda **do tenant**, não na do médico. Passando pelo cartão de
detalhe (o mesmo "Deseja agendar esse serviço?" que todo o produto já usa), o turno
seguinte já enxerga a seleção e resolve a agenda certa. Nenhum turno extra em relação ao
fluxo médico-primeiro.

**Sem linha `Não sei` no catálogo unificado**, de propósito: o hand-back do nó de ajuda
escopada reentra em `_enter_service_detail`, que pressupõe o profissional já conhecido —
verdade depois de `Escolher médico`, falso aqui. Ligar a ajuda nesse ramo pede um par
próprio de steps; a linha foi omitida em vez de embarcada apontando para a reentrada errada.

### Clínica de 1 profissional — nada mudou

`Agendar` continua indo **direto** para a lista de serviços. Nenhum cartão intermediário
foi introduzido: com um médico só, o garfo "médico ou serviço" teria um botão real e seria
um turno morto. Não-regressão: `tests/test_flow_router.py::
test_single_professional_agendar_adds_no_intermediate_card`.

### Apresentação do médico: dobrada, não duplicada

`_enter_professional_services` mandava a apresentação como **bolha separada** antes da
lista de serviços — uma mensagem WhatsApp inteira por agendamento. Agora ela é o
**cabeçalho do próprio cartão de serviços** (`_service_list_bubble(header=...)`, via
`_professional_card_header`: nome + `specialty` + `about`, cortado em `MAX_LIST_BODY_CHARS`).
Mesma informação, mesma ordem, **uma mensagem a menos**. `context_doctor_message` continua
proibido aqui (é texto de persona interno — ver `ai/prompts.py`).

## Parte B — o ramo reaproveitável de dia/horário

### O que existia

`_ask_day` perguntava em texto livre (*"Para quando você gostaria? (ex: amanhã, sexta,
12/06)"*) e `_handle_day` tinha **dois** `delegate_llm`: um quando `_parse_day` não
entendia, outro quando `calendar is None`. Os dois eram silenciosos e ilimitados.

### O que existe agora

Uma implementação, dois descritores (`DayBranch`): `BOOKING_DAY_BRANCH` e
`MANAGE_DAY_BRANCH`. Só mudam as coordenadas de flow-state e a cópia — o orçamento de
linhas, a paginação, a escalada de texto livre e o contrato de calendário são idênticos por
construção, não por duas cópias mantidas em sincronia na mão.

**Passo 1 — `enter_day_picker`** (`SlotsBubble`, ≤ 10 linhas):

- **UMA** chamada `CalendarService.list_available_days(start_day, days, slot_minutes)` para
  a janela inteira. O método é novo e faz **um** `events.list` (`DAY_SCAN_MAX_EVENTS=2500`,
  contra o `DEFAULT_MAX_EVENTS=50` de sempre — uma janela de 20 dias estoura 50 fácil, e uma
  lista de ocupados truncada anunciaria dias que na verdade estão cheios); o passeio por dia
  depois é aritmética pura sobre essa lista. `check_availability` ganhou `max_results`
  opcional (default 50 — todo chamador existente inalterado).
- Cruzamento com `business_hours` e a duração do serviço já vive em `_windows_for_day` /
  `_walk_free_slots` (extraídos de `list_free_slots`, comportamento idêntico).
- Até **8 dias com disponibilidade** por página. Rótulo `Seg, 18/08` (≤ 24 chars).
- Linha 9: `Ver mais dias` quando há mais na janela. Linha 10: `Voltar` quando
  `back_target` está definido — substituída por `Outro` quando o escape já está sendo
  oferecido. Máximo em qualquer combinação: **10**.
- Janela padrão **20 dias corridos** (`DAY_PICKER_WINDOW_DAYS`), que cobre com folga os "17
  dias úteis" pretendidos. A janela é de busca; quem limita a tela é a paginação.
- Zero dias livres na janela → texto fixo + menu (`FlowState.MENU`), nunca uma lista vazia.

**Passo 2 — `_enter_slot_picker`** (`SlotsBubble`, ≤ 10 linhas): `list_free_slots(...,
max_slots=8)` + `Escolher outro dia` (volta ao Passo 1, **mesma página**) + `Voltar` quando
houver `back_target`. Um dia que ficou sem horário entre os dois toques (alguém pegou o
último) re-renderiza o seletor com prefixo — não um texto pedindo para tentar outro dia.

**Paginação sem coluna nova.** O cursor viaja no **id da linha**, não no banco:

| id da linha | corpo que chega | significado |
|---|---|---|
| `day\|2026-08-18\|2` | `Seg, 18/08 (2026-08-18\|2)` | dia + página em que foi listado |
| `daymore\|3` | `Ver mais dias (3)` | próxima página |
| `dayagain\|2` | `Escolher outro dia (2)` | volta ao seletor, mesma página |
| `dayback\|service` | `Voltar (service)` | destino do voltar |
| `dayescape\|0` | `Outro` | escape (prefixo FORA da família, chega como rótulo puro) |

`schemas/webhook.py::extract_inbound_body` ganhou `_PAYLOAD_ROW_PREFIXES`, que
table-driveniza os `slot|`/`prof|` que já existiam e acrescenta a família do seletor —
saída byte-idêntica para os dois antigos. `_control_match` (em `flow_router.py`) compara os
rótulos de controle ignorando o sufixo `(payload)`; um `Voltar` **digitado** também casa.

### Texto livre: continua atalho, deixou de ser vazamento

`_parse_day` segue honrando "amanhã"/"12/06". O que mudou é o **não entendi**:

| tentativa | step resultante | o que o paciente vê |
|---|---|---|
| 1ª | `awaiting_day_retry` | mesmo seletor, prefixado *"Não entendi a data."* |
| 2ª | `awaiting_day_escape` | mesmo seletor + linha `Outro` + dica de escape |
| 3ª+ | `awaiting_day_escape` | idem (limitado, não em loop) |

Mesmo padrão de máquina-de-steps dos nós `Não sei` (`*_FINAL`): o limite vive no state
machine, não num prompt. A **única** saída para a LLM neste step é o toque em `Outro` na
renderização de escape — e ela sai logada como `flow_step=awaiting_day_escape`, distinguível
de um vazamento silencioso em `awaiting_day`. O espelho existe no ramo de remarcação
(`manage_day_retry` / `manage_day_escape`).

### Falha de calendário nunca vira LLM

`calendar is None` e `CalendarUnavailableError` produzem `action="calendar_unavailable"`
(`_calendar_unavailable`) nos dois ramos e no `resume_bubbles`. Agenda ausente não é uma
lacuna de compreensão que o modelo possa preencher: qualquer coisa que ele dissesse sobre
disponibilidade seria inventada.

### Reaproveitamento

| fluxo | entra por | `back_target` |
|---|---|---|
| primeiro agendamento | `_ask_day` (depois de serviço/convênio) | `service` |
| remarcação pelo paciente | `_begin_reschedule` (consolidado — `_manage_handle_day` deixou de existir) | nenhum |
| rebooking após cancelamento do médico (`PROMPT_FEAT_34`, **ainda não construído**) | `enter_day_picker(back_target=BACK_TARGET_PROFESSIONAL)` | `professional` |

O terceiro fluxo ainda não existe; o que já existe e está testado é o **mecanismo** que ele
vai usar (`_handle_day_back` com destino `professional`, preservando serviço e convênio).

### Efeito colateral: a agenda certa na entrada da remarcação

`_begin_reschedule` agora abre o seletor **no mesmo turno**, então a agenda dona da consulta
precisa estar resolvida **antes** do `route()` (o router não faz I/O de banco):

- `_manage_owner_calendar_target` ganhou o turno de **entrada** de um toque direto em
  `Remarcar` (ainda IDLE/MENU, exatamente 1 consulta futura → dono inequívoco; com 2+ o
  router mostra lista e não precisa de agenda);
- `_run_flow` ganhou `manage_calendar_owned`: quando um dono FOI identificado mas a
  construção da agenda falhou, o `None` **permanece** e o fluxo degrada honestamente — nunca
  cai de volta na agenda do tenant, que remarcaria no calendário errado;
- `enter_manage_action` virou `async` e recebe `calendar`; os dois chamadores sem conversa
  (botão de lembrete `apptresched`, hand-back `manage_existing_appointment`) resolvem a
  agenda via `_appointment_calendar` / `_appointment_calendar_target`.

O pre-check de Pix (`_apply_deposit_awareness`) passou a reconhecer os steps
`manage_day_retry`/`_escape` (`_RESCHEDULE_PRECHECK_STEPS`), senão um alvo bloqueado poderia
passar pelo portão errando a data uma vez.

## Observabilidade (§8 do prompt)

Reusa o evento `llm_activated` (`workers/tasks.py::_llm_activation_reason`). Depois do
rollout, a contagem de `reason=router_delegated` com `flow_step=awaiting_day` deve cair para
**aproximadamente zero**; o que sobrar deve aparecer como `awaiting_day_escape`. O router
também emite `flow_day_not_understood` (count-only: `step`/`next_step`, nunca o corpo da
mensagem) para dimensionar quanta gente ainda digita data em vez de tocar.

**Se não cair, o desenho não resolveu o vazamento** — reinvestigar antes de seguir.

## Testes

Novo: `tests/test_flow_day_picker.py` (74 casos, duas camadas — `list_available_days`
contra um cliente Google falso, e o seletor contra um calendário falso). Cobre o teto de 10
linhas em toda combinação de `dias × back_target × step`, dia sem disponibilidade ausente da
lista, paginação ida-e-volta, `Voltar` preservando serviço/profissional/convênio, texto livre
compreendido e não compreendido, os dois modos de falha de calendário, **uma única leitura de
disponibilidade por montagem do seletor**, e a igualdade estrutural entre os ramos.

Atualizados: `test_flow_router.py`, `test_flow_router_multiprofessional.py`,
`test_flow_router_insurance.py`, `test_reactivation.py`, `test_action_buttons.py`,
`test_agent_menu_tools.py`, `test_patient_context.py`, `test_appointment_status_taxonomy.py`,
`test_webhook_parsing.py`.

## Pendências

- **Commit + deploy.** Nada foi commitado. E `flow_router.py`/`tasks.py` são os dois
  arquivos que a regra de deploy do `README.md`/`CLAUDE.md` cobre: **todo push que os toca
  exige deploy do `secretaria-worker`, não só do `secretaria_api`**.
- **Verificar o log** `llm_activated` alguns dias depois do rollout (§8 acima).
- **`awaiting_catalog_service` ainda delega** em texto livre não resolvido, exatamente como
  o `awaiting_service` de sempre. É contado pelo `llm_activated`, não é silencioso — mas é o
  próximo nó a receber tratamento determinístico.
- **Ajuda escopada no ramo serviço-primeiro** (a linha `Não sei` omitida acima).
- `PROMPT_FEAT_34` (rebooking após cancelamento do médico) é quem fecha o terceiro
  consumidor do ramo.
