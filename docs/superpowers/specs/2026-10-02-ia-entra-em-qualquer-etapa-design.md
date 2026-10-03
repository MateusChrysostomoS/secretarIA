# A IA entra em qualquer etapa do fluxo — Design

**Data:** 2026-10-02 · **Task:** TASK-030 · **Status:** aguardando revisão do dono (spec escrita; planos só depois da aprovação)
**Origem:** conversa de 2026-10-02 depois da prova ao vivo da TASK-028 (Portal, "Chrysostomo For Eyes", paciente de QA).
**Mapa do código usado como base:** relatório de exploração desta data (fluxo, entradas, hand-backs, portões, estado da conversa, logs, testes) — resumido na §3; as funções citadas são âncoras estáveis, sem número de linha.

## 1. Objetivo e intenção

Hoje a IA da secretarIA só consegue devolver o paciente ao fluxo guiado por cinco "portas" fixas, e o rascunho que ela passa leva só serviço, médico e convênio. Quando o paciente diz algo que não cabe nessas portas ("quero marcar para outra pessoa", "quinta às 10h com o Dr. X pela Unimed"), a IA cai no menu principal e o paciente refaz o caminho.

**Queremos:** a IA leva o paciente para o ponto certo do fluxo, **em qualquer etapa**, usando o que o paciente já disse, **até o cartão de confirmação**, e o fluxo (automação) conduz o resto. A IA diz o que sabe; a automação decide onde o paciente cai, confere tudo contra os dados reais da clínica e escreve as mensagens. Ao chegar direto à confirmação, o paciente recebe tudo o que as etapas puladas teriam mostrado.

**Dito pelo dono:** "não há limite" para onde a IA pode entrar, desde que tenha as informações; para ir direto à confirmação, o paciente precisa receber tudo sobre a consulta (médico, serviço, o que levar etc.). A IA pode ver a agenda, mas **nunca** títulos/eventos — só os horários livres.
**Suposição (a corrigir se estiver errada):** o paciente continua tocando em **Confirmar**; a IA nunca cria nem confirma o agendamento por ele.

## 2. Decisões do dono (2026-10-02)

1. **Sem limite de profundidade:** a IA pode levar o paciente a qualquer etapa e até o cartão de confirmação, quando tiver as informações.
2. **Abordagem 1 — rascunho mais rico + um resolvedor.** Rejeitadas: uma ferramenta nova por porta; a IA nomear a etapa.
3. **"Pra quem" desconhecido → o fluxo pergunta** ("Essa consulta é pra você?"). Isso fecha o buraco de hoje (§3).
4. **A IA mantém leitura da agenda, só de horários livres**, por uma ferramenta própria. Perde as ferramentas de criar/cancelar evento e a que mostra intervalos ocupados.
5. **O detalhe da consulta mostra tudo o que existir:** preço, o que levar/preparo, descrição do serviço e endereço da clínica (este só quando a clínica tiver).
6. **Seis planos, nesta ordem:** log, rascunho+resolvedor, confirmação expressa, ferramenta de disponibilidade, prompt e regras, tela do Portal.

## 3. Estado atual (fatos do mapa que sustentam o design)

- O fluxo é a função pura `services/flow_router.py::route()`; cada resultado é gravado por `workers/shared/flow_runner.py::_apply_flow_result`, que **limpa todo campo `flow_*` que o resultado não nomeia** — toda entrada nova precisa carregar explicitamente tipo, médico, convênio, "pra quem" e dia.
- Ordem das etapas de agendamento (decisão do dono de 2026-09-23): pra quem → convênio → profissional → serviço → confirmação do serviço → dia → horário → confirmação.
- Os hand-backs (`workers/shared/sentinels.py`) **não passam por `route()`**: chamam funções `enter_*` e depois `_apply_flow_result`. Por isso um hand-back que cai no seletor de horário enxergaria horários reservados como livres (a lista de reservas vive num ContextVar preenchido só dentro de `route()`).
- O rascunho de hoje (`__BOOKING_DRAFT__`, JSON `t`/`p`/`i`) só leva serviço, médico e convênio; a IA nunca grava `flow_*`.
- **Buraco:** um rascunho com serviço chama `enter_guided_booking`, que nunca faz a pergunta "pra quem"; `flow_attendee_name = None` é lido como "para o próprio paciente". A única barreira para "marcar para minha mãe" é uma frase do prompt.
- **Porta dos fundos:** em tenants de um só médico a IA ainda tem `create_event`/`cancel_event` (e `check_availability`), que agendam direto no Google, fora do fluxo, do portão de código do Portal e das reservas.
- Durante as etapas de "pra quem", `flow_selected_type` guarda marcadores internos (`__attendee_next_book__`/`__attendee_next_catalog__`): um serviço do rascunho **não cabe ali** hoje; e a IA chega a ver esse marcador como "serviço já escolhido".
- Consentimento (LGPD), nome, e-mail e código **não são etapas do fluxo**: são portões em `workers/turn_router.py`/`workers/orchestrator.py` que rodam antes da IA e do fluxo.
- A confirmação de hoje mostra só serviço, "pra quem" e data/hora — sem médico, preço, descrição nem o que levar.
- Log: o ramo `selection_only` de `_handle_set_booking_draft` (o caso mais comum) e os hand-backs de médico e de gerenciar não registram a etapa em que o paciente caiu.

## 4. Desenho

### 4.1 Rascunho v2 (o que a IA pode preencher)

Ferramenta `set_booking_draft(service, professional, insurance, for_whom, day, time)`, todos opcionais:

| Campo | Valores | Observação |
|---|---|---|
| `service` | nome exato de um serviço do catálogo | como hoje |
| `professional` | nome de um profissional ativo | como hoje |
| `insurance` | convênio citado | como hoje; só vale se casar com um plano real |
| `for_whom` | `"me"`, `"other"` ou vazio | **nunca um nome**; vazio = desconhecido |
| `day` | `AAAA-MM-DD` no fuso da clínica | |
| `time` | `HH:MM` | sem `day`, é ignorado |

O sentinel (`BOOKING_DRAFT`) ganha as chaves `w` (`self`/`other`/null), `d` e `h`; `t`/`p`/`i` ficam. O sentinel nasce e é consumido no mesmo worker, no mesmo turno — não há contrato entre serviços nem mistura de versões. O parser aceita o payload antigo (sem `w` = desconhecido).

A IA **não** escreve nome de terceiro (a captura do nome é determinística, para o pseudonimizador registrar o valor antes de qualquer modelo vê-lo — skill `pii-field-capture-pseudonymization`), nem consentimento, nem confirma.

### 4.2 O resolvedor (um só lugar que decide onde o paciente cai)

`resolve_booking_draft` (novo, no lado do fluxo) recebe o rascunho e o estado fresco da conversa, percorre as etapas **na ordem do próprio fluxo** e para na primeira que ainda não tem resposta **válida**:

1. **Pra quem:** `for_whom` ou resposta já gravada (`flow_attendee_name is not None`). Senão → `awaiting_attendee_choice`. `other` sem nome gravado → `awaiting_attendee_name` (o fluxo pede o nome e mostra a frase de autorização). `me` → autoatendimento.
2. **Convênio:** se a clínica coleta (`_insurance_step_skip_reason` é None): `match_insurance_plan` ou o já gravado; senão → `awaiting_insurance`.
3. **Profissional** (topologia multi): `professional` no roster fresco; ausente com serviço dado e **exatamente um** profissional que o oferece → esse; senão → `awaiting_professional`. Topologia de um médico: implícito.
4. **Serviço:** nome canônico no catálogo fresco do profissional; senão → `awaiting_service`.
5. **Dia:** dentro da janela de agendamento, com horário de atendimento do profissional e ao menos um horário livre (reservas descontadas); senão → `awaiting_day` (seletor).
6. **Horário:** `time` precisa estar entre os horários livres de `day` na agenda do profissional (duração do serviço, reservas descontadas); senão → `awaiting_slot` daquele dia.
7. **Tudo válido** → cartão de confirmação (§4.4).

**Item inválido:** só ele é descartado (código de motivo no log) e o fluxo pergunta a partir dali — em vez de hoje, em que um item ruim derruba tudo para o menu. O menu principal fica só para: tenant sem catálogo agendável, agenda indisponível (vira a transferência para humano de hoje) ou payload corrompido.

**Regras de execução:** o resolvedor roda **dentro do contexto do portão/reservas** (o mesmo que `route()` prepara), relê tudo do banco (nada do snapshot do início do turno) e devolve um `FlowRouterResult` que carrega todos os campos validados explicitamente, gravado por `_apply_flow_result`.

### 4.3 Guardar o rascunho enquanto o paciente responde "pra quem"

Coluna nova e aditiva **`conversations.flow_draft` (JSON, NULL)** com o rascunho já validado (serviço, médico, convênio, dia, horário). É lida quando o paciente termina as etapas de nome/autorização: a continuação **roda o resolvedor de novo** sobre o rascunho (o mundo pode ter mudado em dois minutos) e segue. É apagada ao consumir, ao voltar ao menu e pela expiração de estado (todo estado precisa de saída por tempo — skill `conversation-flow-state`). Os marcadores de `flow_selected_type` continuam como estão no caminho de botões.
**Regra do orquestrador (2026-10-03, vinda do P2):** o rascunho guardado espera **qualquer** pergunta que o fluxo faça — convênio, médico e serviço também, não só "pra quem". Quando o paciente responde, o resolvedor roda de novo sobre o rascunho (validação fresca), de modo que "quinta às 10h com o Dr. X, pra mim" não perde o dia e o horário se o convênio for perguntado no meio. A implementação desse trecho fica no P3.
Migração: **antes** da API e do worker (ambos mapeiam `Conversation`; coluna nula e aditiva — skill `frozen-contract-migration`).

### 4.4 Confirmação expressa

Pré-condições: os seis itens válidos e "pra quem" respondido (`me`, ou `other` com a autorização já confirmada).
1. **Horário re-derivado:** o resolvedor pega os horários livres do dia na agenda do profissional (mesma função do seletor de horário), desconta reservas, e só aceita o pedido se o início bater. Nada que a IA mande vira horário sem passar por aqui.
2. **Duas mensagens** quando o paciente pulou etapas: (a) **detalhes** — médico (+especialidade), serviço com preço e descrição, "o que levar/preparo" (`requirements` do serviço), convênio, para quem e endereço da clínica **quando existir** — como mensagem de texto própria, cortada pela função única de corte (`core/whatsapp_limits`, skill `third-party-text-limits`); (b) **cartão de confirmação** com serviço, médico, data/hora e para quem, botões Confirmar/Cancelar. Quem passou pelas etapas normais já viu os detalhes: só vê o cartão (agora com o médico).
3. **Confirmar** segue o caminho de hoje sem alteração (`_handle_confirmation`): portão e código do Portal, reserva, evento no Google, linha de `Appointment`, hooks de depósito. Horário que sumiu no meio do caminho → tratamento "horário tomado" de hoje.

### 4.5 Consultas já marcadas

`manage_existing_appointment(action, appointment, day, time)`: a consulta é resolvida **entre as do próprio paciente**; o pouso segue a mesma regra (escolher → cartão de cancelar/remarcar → seletor de dia → horário). Cancelar sempre pára no cartão de confirmar cancelamento; remarcar só chega à confirmação depois da pré-checagem de depósito/limite que os botões já fazem (hoje `manage_slot`/`manage_confirm` ficam fora dessa pré-checagem).

### 4.6 Ferramentas da IA depois da mudança

- **Ficam:** `set_booking_draft` (v2), `show_main_menu`, `manage_existing_appointment` (v2), `request_human_handoff`, ferramentas de plugin que não agendam.
- **Entram no rascunho e saem como ferramentas separadas:** `select_professional_and_continue` e `start_guided_booking` (removidas no plano 5, depois que o prompt deixar de citá-las).
- **Nova — `get_availability(professional, service, day_from, day_to)`:** devolve **só janelas livres** `{day, start, end}` (agenda do profissional − reservas, duração do serviço, no máximo 14 dias e 30 janelas no total). Calculada pelo mesmo código dos seletores. **Nenhum campo de evento** (título, participantes, id, bloco ocupado) sai da camada de calendário. Ferramenta dentro do worker, não endpoint público; rota interna pode vir depois, se outro produto precisar.
- **Saem:** `check_availability`, `list_free_slots`, `create_event`, `create_event_for_professional`, `cancel_event` e similares (as funções continuam para uso interno do fluxo).
- A IA pode **mencionar** horários que recebeu da ferramenta; o agendamento em si só acontece pelo rascunho e pelo toque do paciente.

### 4.7 O que a IA enxerga ("ESTADO DA CONVERSA")

`services/llm_context.py::build_conversation_state` passa a: rotular as etapas que hoje caem em "no menu inicial" (pra quem, gerenciar, motivo de recusa, legadas); **nunca** mostrar marcadores internos (`__attendee_next_*__`); dizer se "pra quem" já foi respondido (`me`/`outra pessoa`/ainda não); mostrar o horário gravado, se houver; e informar a topologia e quais médicos oferecem quais serviços (como já faz para vários médicos). Valores de convênio continuam omitidos.

### 4.8 O que continua fechado (nenhuma entrada pode contornar)

LGPD, nome, e-mail e código do Portal (rodam antes da IA); nome de terceiro e frase de autorização; o toque em Confirmar; o portão de código e as reservas; o `ConsentEvent` de terceiro; os hooks de depósito/Pix; a checagem de agenda no momento de listar e de confirmar. Os estados `AWAITING_*` pertencem ao `turn_router` — o resolvedor nunca os escreve.

### 4.9 Observabilidade

Evento único `conversation_handback_entered` em **todo** hand-back (sucesso e fallback): `source_tool` (rascunho/menu/gerenciar/humano), `landing_step`, `supplied` (nomes dos campos), `accepted`, `dropped` (campo → código de motivo), `fallback` (motivo ou nulo), `topology`, `channel`. **Sem valores, sem PII.** Os eventos atuais (`conversation_booking_draft_entered`, `conversation_guided_booking_entered`, `conversation_menu_rendered`) continuam. `llm_turn_trace` ganha `tools_called` (só nomes, em ordem).

### 4.10 Prompt e regras

A IA recebe: preencha o rascunho com tudo o que o paciente já disse e não repita pergunta respondida; para "tem horário?" chame `get_availability` e, se o paciente quer marcar, preencha `day`/`time`; **nunca descreva em texto as opções do fluxo** (serviços, médicos, convênios) — devolva ao fluxo; **nunca prometa capacidade que não existe** (consultar valor, lembrete, previsão do tempo, "diga X que eu abro o menu" sem a porta existir); nunca diga que algo foi marcado/verificado; "para outra pessoa" → `for_whom="other"`, sem nome. Conversas reais de hoje viram testes (§6).

### 4.11 Liberação

- **Interruptor por tenant** (lido ao lado de `flows_enabled`), **desligado por padrão**, para o rascunho v2, o conjunto novo de ferramentas e o prompt novo. Ligar primeiro na "Chrysostomo For Eyes"; observar pelos logs; depois todos; depois remover o caminho antigo.
- **Fora do interruptor** (correção de segurança): "pra quem" desconhecido passa a **perguntar**, também nos hand-backs antigos. Muda o comportamento de todos os tenants (um toque a mais para quem só diz "quero cirurgia de catarata"); decisão do dono.
- **Ordem de deploy:** migração → API **e** worker juntos (README: "Deploy both services, or neither"; a paridade em `GET /build` deve voltar `match`).

## 5. Critérios de sucesso (verificáveis)

1. "Quero marcar pra minha mãe" (com ou sem serviço dito) → o fluxo pede o nome e mostra a frase de autorização; ninguém é agendado como o próprio paciente sem ter respondido "pra quem".
2. "Quinta às 10h com o Dr. X pela Unimed, pra mim", horário livre → mensagem de detalhes + cartão de confirmação; Confirmar agenda pelo caminho de hoje (portão do Portal incluído).
3. Mesmo pedido com horário ocupado → lista de horários daquele dia, não erro.
4. Médico/serviço/convênio inválido → o fluxo pergunta **a partir daquela etapa**, não o menu.
5. A IA nunca recebe título, participante ou id de evento: teste que serializa a saída de `get_availability` e falha se aparecer qualquer chave além de `day`/`start`/`end`.
6. Nenhum caminho da IA chega à confirmação sem horário re-derivado da agenda fresca menos reservas; o portão e as reservas valem.
7. 100% dos hand-backs geram `conversation_handback_entered`.
8. O toque em Confirmar continua sendo o único jeito de criar a consulta.

## 6. Testes

Unitários por pouso (cada etapa) e por item inválido; isolamento de tenant (roster/catálogo/agenda só do tenant); reservas e portão (o resolvedor chamado direto vê o horário reservado como ocupado); expiração de `flow_draft`; migração em SQLite e em Postgres descartável; contrato do sentinel nos dois sentidos (payload antigo e novo); teste de formato de `get_availability`; testes existentes de hand-back atualizados (`tests/test_agent_menu_tools.py`, `test_set_booking_draft.py`, `test_attendee_booking.py`, `test_llm_context.py`, `test_prompts.py`). **Avaliações com IA real** (`tests/llm_eval/`): "marcar pra minha mãe", "quinta às 10h com o Dr. X pela Unimed", "tem horário semana que vem?", "qual o endereço?", "ok, vamos prosseguir com a marcação" depois de médico já escolhido.

## 7. Riscos do mapa → quem resolve

| Risco | Plano |
|---|---|
| `flow_selected_type` sobrecarregado nas etapas de "pra quem" | P2 (`flow_draft`) |
| Rascunho com serviço pula "pra quem" | P2 |
| Reservas só visíveis dentro de `route()` | P2/P3 (resolvedor roda no contexto do portão) |
| `_handle_select_professional` usa snapshot velho; `_handle_start_guided_booking` não re-canoniza o serviço | P2 (o resolvedor revalida tudo) |
| `_is_agent_sentinel` não lista o sentinel de `start_guided_booking` | P2 |
| `selection_only` perde o alerta `professional_config_incomplete`; convênio "Outro" digitado é descartado | P2 (decidir e testar) |
| `manage_slot`/`manage_confirm` fora da pré-checagem de depósito | P3 |
| `create_event` & cia. como porta dos fundos | P4 |
| Defeitos do estado da conversa (marcador interno, rótulos faltando) | P2 |
| Hand-backs não registram a etapa | P1 |

## 8. Fora de escopo

A IA escrever nome, consentimento ou confirmar; cadastrar fatos da clínica (endereço/estacionamento — TASK-025/026); componente de calendário do Portal ou WhatsApp Flow; fusão de visita/conta (L2, plano B); "digitando" (planos C–E); deploy (cada um só com pedido do dono).

## 9. Divisão em planos

| # | Plano | Depende de | Arquivos principais (dono) |
|---|---|---|---|
| P1 | Registro de hand-backs (`conversation_handback_entered`, `tools_called`) | — | `workers/shared/sentinels.py`, `ai/graph.py` |
| P2 | Rascunho v2 + resolvedor + `flow_draft` + correção do "pra quem" + estado da conversa | P1 | `ai/tools.py`, `ai/graph.py`, `workers/shared/sentinels.py`, `services/flow_router.py`, `models/conversation.py` + migração, `services/llm_context.py` |
| P3 | Confirmação expressa (detalhes + cartão + horário re-derivado) e manage até a confirmação | P2 | `services/flow_router.py`, `workers/shared/flow_runner.py`, `workers/shared/deposit.py` |
| P4 | `get_availability` + retirada das ferramentas de calendário | P2 | `ai/tools.py`, `workers/shared/llm_context.py`, `services/calendar.py` |
| P5 | Prompt e regras + avaliações com IA real (ligar o interruptor por tenant é decisão do dono, fora do plano) | P2, P3, P4 | `ai/prompts.py`, `tests/llm_eval/` |
| P6 | Portal: rolar pela última mensagem e podar cópias locais | — (independente) | `Brain-Message-Frontend` (`PatientMessageList`, `PortalConversation`) |

Ordem: P1 → P2 → P3 → P4 → P5; P6 a qualquer momento. P2 e P3 tocam `flow_router.py` e P1/P2 tocam `sentinels.py`: **sequenciais**, mesmo worktree (regra "nunca dois agentes no mesmo arquivo").

## 10. Referências

`services/flow_router.py` (`route`, `enter_booking`, `_start_booking`, `_enter_professional_services`, `enter_guided_booking`, `_enter_slot_picker`, `_handle_confirmation`, `_recap_text`, `_service_detail_text`, `resume_bubbles`); `workers/shared/sentinels.py` (`_handle_set_booking_draft` e irmãos); `workers/shared/flow_runner.py::_apply_flow_result`; `workers/shared/llm_context.py::_flow_handback_tools`; `services/llm_context.py::build_conversation_state`; `ai/tools.py`, `ai/graph.py`, `ai/prompts.py`; `services/booking_hold.py`; `models/service.py` (`requirements`, `description`, `long_description`); `docs/CHECKPOINT_portal_mensagens_recentes.md` ("Dependência do front"); `docs/LACUNAS_PORTAL_2026-10-01.md` (L3–L5).
