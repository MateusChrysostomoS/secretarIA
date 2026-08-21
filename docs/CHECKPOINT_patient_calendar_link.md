# CHECKPOINT — Link de agenda pro paciente + gatilho opcional da LLM pro fluxo guiado

> Estado: **BUILT + suíte verde, NÃO commitado, NÃO deployado** (2026-08-20).
> Escopo: **só `secretarIA`** — nada em `brain-api`, `brain-frontend`, `PreCheck`.
> **Sem migração.** Todos os dados já existiam em `Appointment`/`Conversation`/`Tenant`.

Origem: `z_prompts/PROMPT_LINK_CALENDAR_TRIGGER_LLM_EMAIL_MEDICO.md`. Duas features numa
rodada só porque compartilham o mesmo miolo (`ai/tools.py`, `ai/graph.py`,
`services/flow_router.py`, `workers/tasks.py`). O terceiro item do prompt (link do Google
no e-mail do médico) ficou em `docs/CHECKPOINT_professional_booking_email.md`, que é o
dono daquela feature.

Suíte: **1712 passed + 1 falha pré-existente** (`uv run python -m pytest`), `ruff` com as 7
falhas pré-existentes de sempre e **nenhuma nova**. 43 testes novos.

> A falha conhecida `test_human_backup_plugin.py::test_on_inbound_inside_hours_returns_false`
> apareceu nesta rodada porque ela rodou às 01:25 UTC — a faixa 00:00-03:00 em que o
> fixture monta `business_hours` em UTC e o plugin resolve o dia em `America/Sao_Paulo`.
> **Provada pré-existente**: `git stash` das mudanças e a mesma execução no HEAD limpo
> falha idêntico. Nenhum arquivo dela é tocado aqui.

---

## Parte 1 — O link que o paciente recebe

### O defeito

`CalendarService.create_event` manda o evento pro Google sem `attendees` e sem
`sendUpdates` — de propósito: o produto tem o WhatsApp do paciente, não o e-mail dele, e
convidar alguém transformaria a agenda privada da clínica numa agenda compartilhada. A
consequência era que o `htmlLink` que o Google devolve é o link **privado** do evento **na
agenda da clínica**: abre certo para quem já tem acesso a ela, e dá erro de permissão para
qualquer outra pessoa.

Esse `htmlLink` era exatamente o que ia pro paciente. `ai/tools.py::create_event` o
devolvia pra LLM e `ai/prompts.py` mandava incluir "o link" no balão de recapitulação. O
fluxo determinístico (`_handle_confirmation`) **não** mandava link nenhum — não por
conserto, por omissão.

### A correção — `services/calendar.py::build_patient_calendar_link`

Um helper **puro** (nenhuma chamada de rede, nenhum `CalendarService` necessário) que monta
a URL **pública de template** do Google Calendar:

```
https://calendar.google.com/calendar/render?action=TEMPLATE&text=...&dates=...&details=...
```

Ela não aponta pra evento nenhum: leva o compromisso na própria querystring e abre a tela
"criar evento" já preenchida na conta Google de **quem clicar**. Sem convite, sem attendee,
sem e-mail do paciente, sem uma segunda chamada à API do Google — é formatação de string
sobre dados que os dois fluxos já têm em mãos.

**Decisão: UTC com sufixo `Z`, nunca hora local + `&ctz=`.** O Google aceita as duas
formas, mas a local só está certa enquanto o `ctz` for honrado — se esse parâmetro cair ou
for ignorado, os mesmos dígitos passam a ser lidos no fuso de **quem abriu**, movendo a
consulta em horas inteiras para qualquer paciente cuja conta Google não esteja no fuso da
clínica. Um instante em `Z` não tem como ser mal lido, e o Google continua exibindo no
fuso do próprio paciente — que é para isso que serve um calendário.

**Decisão: `tz` obrigatório para datetime naive.** Aware ignora o parâmetro; naive sem `tz`
**levanta `ValueError`** em vez de assumir UTC e emitir um link errado pelo offset inteiro
da clínica. Os dois call sites passam `tz=<calendar>.tzinfo` e datetimes já aware, então o
caminho de erro não é alcançável em produção — é contrato documentado, não guarda quente.

**Decisão: sem `location`.** O endereço da clínica (`Tenant.address`) não está em
`TenantRuntimeConfig`, então o caminho da LLM não teria como preenchê-lo; e para um tenant
com o addon `multi_unit` o endereço verdadeiro é o **da unidade**, não o da clínica. Um
endereço plausível-mas-errado dentro da agenda pessoal do paciente é pior que nenhum.

### Os dois links agora coexistem, e não são intercambiáveis

| link | o que abre | quem recebe |
|---|---|---|
| `event["htmlLink"]` (privado) | o evento **na agenda da clínica** | o médico — hub (`api/hub/calendar.py`) e e-mail de nova consulta |
| `build_patient_calendar_link(...)` (público) | tela "criar evento" pré-preenchida | o **paciente**, nos dois fluxos |

`Appointment.google_event_link` **continua guardando o `htmlLink` privado**, inalterado. O
link público é calculado na hora de montar a mensagem — não virou coluna, não virou
migração, e não criou uma segunda fonte de verdade pro mesmo evento.

### Onde entrou

- **As TRÊS tools de agendamento da LLM** — chave **nova** no dict de retorno,
  `patient_calendar_link`, ao lado do `htmlLink` que continua existindo. Chave separada de
  propósito: são coisas diferentes com usos diferentes, e colapsar as duas é literalmente
  o bug.

  | tool | módulo | quem a recebe |
  |---|---|---|
  | `create_event` | `ai/tools.py` | clínica de um profissional (ou topologia desconhecida) |
  | `create_event_for_professional` | `plugins/multi_professional.py` | clínica multi-profissional |
  | `create_event_at_unit` | `plugins/multi_unit.py` | clínica com `multi_unit` e sem `multi_professional` |

  > **Corrigido na revisão adversarial desta rodada.** A primeira versão só tocou a tool
  > base. Como `ai/graph.py::base_tools_for` **retira** as tools de nível de tenant numa
  > clínica multi-profissional, `create_event_for_professional` é a ÚNICA tool de
  > agendamento que aquela clínica recebe — e a instrução nova do prompt ("mande o
  > `patient_calendar_link`, NUNCA o `htmlLink`; se ele não vier, mande sem link") teria
  > deixado justamente as clínicas que pagam pelo addon **sem link nenhum**. Ou seja: a
  > correção da Task 1 teria virado uma regressão pra elas. As três tools devolvem o campo
  > agora, cada uma com um teste próprio.
- **`ai/prompts.py`** (seção "3) MENSAGEM FINAL pós-agendamento") — a instrução do balão de
  recapitulação passou a nomear o campo: copie o `patient_calendar_link` inteiro, **nunca**
  o `htmlLink`, e sem link nenhum se ele não vier. A regra de cima ("NUNCA inclua o ID do
  evento") continua intacta.
- **`services/flow_router.py::_handle_confirmation`** — o link entra na **mesma bolha** da
  confirmação, atrás de uma linha rotulada `Adicionar à sua agenda:`. Um balão, não dois:
  o paciente fica com uma mensagem única para printar ou encaminhar, e custa um envio de
  WhatsApp em vez de dois.

---

## Parte 2 — `start_guided_booking`: a LLM devolvendo o agendamento aos botões

### O que é

Uma tool **opcional**, acionada por iniciativa da própria LLM, que entrega o agendamento ao
fluxo determinístico: a conversa livre resolveu **qual serviço**, e daí em diante quem
conduz dia → horário → confirmação → `create_event` é o `flow_router`. Não é passo
obrigatório: continuar perguntando dia/hora em texto e chamar `create_event` no fim segue
válido, e o prompt diz isso explicitamente (com o contraexemplo: se o paciente já pediu um
dia específico, `list_free_slots` é melhor).

Preenche uma lacuna que só existia em clínica de **um** profissional. Em clínica
multi-profissional o seam de volta pro fluxo já existia e é completo
(`select_professional_and_continue` → saudação + serviços do médico escolhido).

### Mecanismo — o quarto uso do MESMO padrão, não um quinto padrão

| tool | exceção | sentinel | handler | reentrada |
|---|---|---|---|---|
| `show_main_menu` | `ShowMainMenuRequested` | `SHOW_MAIN_MENU_SENTINEL` | `_handle_show_main_menu` | menu |
| `select_professional_and_continue` | `SelectProfessionalRequested` | `SELECT_PROFESSIONAL_SENTINEL_PREFIX` | `_handle_select_professional` | `_enter_professional_services` |
| `manage_existing_appointment` | `ManageAppointmentRequested` | `MANAGE_APPOINTMENT_SENTINEL_PREFIX` | `_handle_manage_appointment` | `enter_manage_action` |
| **`start_guided_booking`** | **`GuidedBookingRequested`** | **`START_GUIDED_BOOKING_SENTINEL_PREFIX`** | **`_handle_start_guided_booking`** | **`enter_guided_booking`** |

O sentinel carrega o **nome canônico do serviço** (vazio quando a clínica não tem
catálogo), pela mesma razão que o de profissional carrega um id: é a única coisa que a
conversa livre resolveu e que o fluxo de botões teria que perguntar de novo.

### Decisão 1 — ele NÃO pula direto pro seletor de dias

O pedido literal foi "começaria pela mensagem do fluxo de escolher os dias disponíveis".
Fazer isso incondicionalmente quebraria uma invariante que o resto do fluxo respeita:
`collect_insurance` é uma configuração **da clínica inteira**, que o hub salvou de
propósito, e `_insurance_step_skip_reason` é o único lugar que decide se ela se aplica.
Pular o convênio faria a reserva perder uma informação que a clínica pede em **todo outro**
agendamento, só porque este paciente entrou pela LLM — e isso ficaria invisível até uma
recepcionista notar um convênio em branco.

Então `enter_guided_booking` reproduz exatamente os dois desfechos do toque em
"Sim, agendar" (`_catalog_step`, ramo `STEP_AWAITING_SERVICE_CONFIRM`), na mesma ordem e
com o mesmo log `insurance_step_skipped`: **convênio quando a clínica coleta, seletor de
dias quando não**. No caso comum (clínica que não coleta) o comportamento é o pedido.

### Decisão 2 — a tool é negada a tenant multi-profissional

**Três** travas, mesma forma que as tools de calendário já usam:

1. **Não entra no tool set** — `workers/tasks.py::_flow_handback_tools` retira a tool numa
   topologia `multi` (`manage_existing_appointment`, que não abre picker nenhum, continua).
2. **Recusa dentro da tool** — `_blocked_tenant_level("start_guided_booking")`, que devolve
   a mensagem recuperável apontando pra `show_main_menu`.
3. **Recusa no handler**, contra o roster **relido** — `_handle_start_guided_booking`
   manda pro menu se a clínica se revelar multi.

   > **A trava 3 saiu da revisão adversarial.** As duas primeiras julgam pela topologia
   > que *aquele turno* carregou, e `booking_topology(None)` — o roster cujo carregamento
   > **falhou** — devolve `unknown`, não `multi`. Numa clínica que de fato tem 2+ médicos,
   > uma leitura de roster que falha entregava a tool, e o picker sairia da agenda da
   > clínica: dias que médico nenhum necessariamente tem livre. O handler já relê o roster
   > por outro motivo, então a checagem é de graça.

Motivo: o que a tool **abre** é o seletor de dias em nível de tenant, cuja disponibilidade
sairia da agenda da clínica e não da do médico escolhido. Oferecer a um paciente dias que
nenhum médico tem livre é a mesma classe de erro de agendar na agenda errada, um passo
antes.

### Decisão 3 — `enter_guided_booking` é entrada PÚBLICA no `flow_router`, e não muta nada

A decisão convênio-ou-dia é roteamento de conversa, então mora em `services/flow_router.py`
e não no worker (regra de camadas do `CLAUDE.md`: o worker orquestra, não decide). Ela
segue o formato de `enter_manage_action` — recebe o estado como argumentos simples porque o
chamador não tem mais uma `Conversation` viva — e aplica o serviço escolhido a um
`_DayPickerState`, o carregador que o repo **já** criou exatamente para "entradas que não
têm linha de conversa" (a docstring dele já citava sentinel hand-back). Nada aqui muta uma
linha: `_apply_flow_result` persiste o que o `FlowRouterResult` carregar, como em qualquer
outra transição.

### Decisão 4 — o handler relê tudo, e a agenda certa vem da máquina que já existia

`_handle_start_guided_booking` relê tenant/roster/conversa numa sessão própria (mesma razão
declarada por `_handle_manage_appointment`: a LLM pode ter gasto vários turnos de tool-call
desde o que este turno pré-carregou).

O que ele passa pro `enter_guided_booking` é o **`_flow_tenant_snapshot`**, o mesmo objeto
tenant-shaped que o `route()` sempre recebe — **nunca** a linha ORM crua.

> **Também da revisão adversarial.** A primeira versão passava o `Tenant` e montava o
> catálogo à mão. Só que `_flow_tenant_snapshot` faz duas coisas que importam: resolve o
> catálogo canônico da clínica **e**, numa clínica com um profissional ativo, substitui
> pelos serviços **daquele profissional**. Com a linha crua, uma clínica que guarda tudo no
> `Professional` (a coluna legada `tenants.appointment_types` vazia — exatamente a forma
> que o `docs/CHECKPOINT_booking_ownership.md` chama de "o tenant que quebrou") não achava
> serviço nenhum, caía no `tenant.appointment_duration_min` e oferecia dias fatiados na
> duração errada. O teste
> `test_handle_start_guided_booking_slots_on_the_sole_doctors_own_duration` fixa isso com
> 50 min contra um default de 30, e falha se a linha crua voltar.

A agenda do seletor sai de
`_appointment_calendar_target` + `_appointment_calendar`, a mesma dupla que o fluxo de
gerenciamento usa: profissional selecionado quando ainda resolve, calendário do tenant
quando nunca houve escolha (numa clínica de um profissional só, `load_tenant_config` já
resolveu as credenciais **dele** ali dentro), e `None` quando a seleção não resolve mais —
o que faz o picker responder `calendar_unavailable` em vez de listar dias de qualquer
agenda à mão.

---

## Testes

| arquivo | o que cobre |
|---|---|
| `tests/test_calendar_patient_link.py` **(novo, 12 testes)** | o helper puro: endpoint TEMPLATE, fuso != UTC, naive+`tz`, aware ignora `tz`, naive sem `tz` levanta, consulta que atravessa a meia-noite (UTC), acento/espaço codificados, e um `summary` com `&`/`=` que **não** consegue forjar parâmetro |
| `tests/test_flow_router.py` | o balão de confirmação carrega o link público, com asserção na querystring — e o `htmlLink` privado continua sendo persistido e **não** aparece na mensagem |
| `tests/test_booking_owner_persistence.py` **(+2)** | `create_event` devolve os **dois** links, diferentes entre si; `description` chega em `details`; o privado é o que vai pra linha do `Appointment` |
| `tests/test_multi_professional_plugin.py` **(+1)** | `create_event_for_professional` também devolve o `patient_calendar_link` — a tool que a clínica multi-profissional realmente recebe |
| `tests/test_multi_unit_plugin.py` **(+1)** | idem para `create_event_at_unit`, e que ele **não** carrega `location` (nem o endereço da unidade) |
| `tests/test_agent_menu_tools.py` **(+23 testes)** | a tool (canonicalização, serviço inexistente, omissão ambígua, clínica sem catálogo, fail-closed em `multi`), o sentinel no `run_agent`, o gate `_flow_handback_tools` por topologia, e o handler (dia / convênio primeiro / convênio já respondido sobrevive / sem tipo / sem tenant / sem agenda → handover / **duração do profissional único** / **clínica multi mandada pro menu**) |

O teste que mais importa dessa lista é
`test_the_next_tap_after_the_handback_actually_advances`: cair no `flow_step` certo é só
metade do contrato — a linha também precisa **carregar** tudo que o passo seguinte lê.
Como `_apply_flow_result` escreve todo campo `flow_*` incondicionalmente, um
`FlowRouterResult` que esquecesse um deles o **apagaria**, e o toque seguinte cairia
silenciosamente na LLM. Esse teste toca uma linha de dia de verdade, tirada da própria
bolha que o hand-back acabou de produzir, e exige que o fluxo avance pro seletor de
horário com o serviço intacto.

O caso multi-profissional ficou nas **três** travas
(`test_start_guided_booking_fails_closed_on_a_multi_professional_turn`,
`test_a_multi_professional_tenant_is_not` e
`test_handle_start_guided_booking_turns_a_multi_doctor_clinic_away`) em vez de em
`tests/test_flow_router_multiprofessional.py`: como a decisão foi **negar** a tool nessa
topologia, não há comportamento de `flow_router` para testar lá — o fluxo nunca é entrado.

**Os dois testes de guarda foram provados não-vacuosos** desligando cada guarda no código e
confirmando a falha, e o teste do link do fluxo determinístico idem. Vale registrar porque
uma asserção de duração passa de graça quando o serviço tem a mesma duração do default da
clínica — foi por isso que a primeira versão do teste (30 contra 30) não pegou o bug do
snapshot, e a versão final usa 50 contra 30.

---

## Pendências

- **Commit + deploy.** Nada commitado. Isto toca `workers/tasks.py` **e**
  `services/flow_router.py`: vale a regra do `README.md`/`CLAUDE.md` — o push exige deploy
  do **`secretaria-worker`**, não só do `secretaria_api`. Deployar só a API entrega
  literalmente nada desta rodada.
- **Sem migração e sem env nova.** Nada a configurar no EasyPanel para estas duas partes.
  (O `DOCTOR_AGENDA_URL` da Parte 3 é pendência do outro checkpoint.)
- **Teste ao vivo do link** numa conta Google que **não** tenha acesso à agenda da clínica
  — é o único jeito de provar o conserto de ponta a ponta. Um `htmlLink` também "abre" pra
  quem está logado na conta da clínica, que é justamente por que o bug passou despercebido.
- **Sem UI no hub** para a Parte 2. A tool é decisão da LLM, não configuração da clínica;
  se algum dia virar liga/desliga por tenant, o lugar é `initial_flows`, junto de
  `flows_enabled`.
