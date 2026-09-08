# CHECKPOINT — Pseudonimização de PII antes do modelo de IA

**Estado:** BUILT, 1993 testes verdes (baseline do HEAD era 1980), **uncommitted**,
**migração NÃO rodada em nenhum banco**, **não deployado**.
**Data:** 2026-09-07. Prompt de origem: `z_prompts/PROMPT_PSEUDONYMIZE_SECRETARIA_ADOPTION.md`.

## 1. O que mudou, em uma frase

Texto escrito por paciente não sai mais deste repositório em claro para a OpenAI: ele
passa por `pseudonymize-core` (mesmo motor que o PreCheck usa) na ida e volta ao normal
na volta, com o mapa de tokens guardado por conversa.

Antes, `ai/graph.py::run_agent` remontava o histórico do banco e mandava direto.

## 2. Onde o código está

| Camada | Arquivo | Papel |
| --- | --- | --- |
| pacote | `pseudonymize-core` v0.1.0 (`pyproject.toml`) | motor puro: sem DB, sem I/O, sem log |
| model | `models/conversation_pii_token_map.py` | `{token: valor real}` por `conversation_id` |
| migração | `migrations/versions/a1b2c3d4e5f6_*.py` | cria a tabela; encadeia em `e3b7c1d5a9f2` |
| services | `services/pii_pseudonymization.py` | load/persist do mapa + a ContextVar do turno |
| ai | `ai/pii.py` | `scrub_messages` + o wrapper do limite de ferramenta |
| ai | `ai/graph.py` | scrub do histórico, rehydrate do reply, persist no `finally` |
| ai | `ai/scoped_help.py` | scrub do histórico, rehydrate do `clarify.question` |
| testes | `tests/test_pii_pseudonymization.py` | 13 testes |

`workers/tasks.py` **não mudou**: `run_agent` continua devolvendo `str`, já re-hidratada.

## 3. A DECISÃO sobre o escopo das ferramentas do loop ReAct

O prompt deixou isto em aberto de propósito e mandou não decidir sozinho. **Decidido com
o usuário em 2026-09-07: o escopo CRESCEU para cobrir o limite da ferramenta.** O motivo é
que a pergunta original ("os retornos de ferramenta carregam PII de terceiro?") tinha
resposta **não** — mas ao investigar apareceu um segundo problema, maior, na direção
oposta:

**(a) Entrada — sim, há PII de terceiro.** `check_availability` está em `_BASE_TOOLS`
(`ai/graph.py`) e devolve `busy[].summary`: títulos reais de eventos do Google Calendar
**da clínica**, ou seja, nome de outros pacientes. O docstring de
`services/calendar.py::check_availability` afirma isso explicitamente — "surfaces real
summaries to the LLM, which it can quote back to the patient". Não é acidente, é desenho.

**(b) Saída — e esta é a decisiva.** Argumentos de ferramenta saem para sistemas externos
reais. O docstring de `ai/tools.py::create_event` instrui o modelo:
`summary: Título do evento no Google Agenda, ex: 'Consulta - João Silva'`. O modelo tira
esse nome do histórico. **Mascarar o nome do paciente sem re-hidratar o argumento faria a
agenda real do médico encher de `Consulta - [PACIENTE_a1b2]`** — não é vazamento, é
corrupção, e não é corner case: é o caminho instruído. Mesma exposição em
`create_event_for_professional` e `create_event_at_unit`.

Isso amarra as duas pontas: **mascarar o nome do paciente EXIGE re-hidratar argumento de
ferramenta.** Por isso o guard é simétrico e mora no limite, não em três ferramentas
individuais — assim toda ferramenta atual, todo plugin entitled
(`plugins/registry.py::agent_tools_for`) e toda ferramenta futura ficam cobertos por
construção.

Implementação: `ai/pii.py::wrap_tools_with_pseudonymizer`, instalado em
`ai/graph.py::build_agent` logo depois da consulta ao cache. Cada ferramenta é envolvida
via `tool.model_copy(update={"coroutine": ...})`, o que preserva `name`/`description`/
`args_schema` — por isso a chave do cache de agente (um `frozenset` de NOMES, calculada
sobre as originais) continua descrevendo o agente exatamente. O `Pseudonymizer` do turno
viaja numa ContextVar (`services/pii_pseudonymization.py::_pseudonymizer_ctx`): o LangGraph
COPIA o contexto para o tool node, então um valor setado antes é visível lá, e a mutação do
mapa volta — o objeto é o mesmo, só o nome é rebindado.

### O que o guard NÃO resolve — e por que isso virou outra correção

Escrudar o retorno de uma ferramenta mascara **padrões** (telefone/CPF/e-mail) e valores
**já conhecidos** do mapa — inclusive o nome deste paciente voltando do Google Calendar.
Ele **não** mascara o nome de um terceiro desconhecido: o motor detecta nome por consulta
ao mapa, não por inferência, então `Consulta - Maria Silva` na agenda da clínica chegaria
ao modelo como está.

Pseudonimização não tinha como fechar isso. **Fechado na fonte — ver §3.1.**


## 3.1 `check_availability` parou de expor evento alheio (2026-09-07, decisão do usuário)

**Decisão:** a ferramenta só entrega `summary` e `id` reais quando o evento **É do próprio
paciente da conversa**. Para qualquer outro, devolve **apenas o intervalo ocupado**.

Antes, `ai/tools.py::check_availability` repassava a lista crua do Google — `{id, summary,
start, end}` — e `ai/prompts.py` ainda mandava a agente *"mencione brevemente o conflito"*.
Numa agenda compartilhada, o título de um evento que não é deste paciente é quase sempre o
NOME DE OUTRO PACIENTE. **Não era acidente: era o desenho pedindo o vazamento.**

**Como o "é do próprio paciente" é decidido:** pela tabela `appointments`, que guarda
`google_event_id` nos DOIS caminhos de agendamento — o do agente
(`ai/tools.py::_persist_appointment`) e o do fluxo de botões (`workers/tasks.py`). **Nunca**
pelo título do evento, que é texto livre digitado por qualquer pessoa com acesso à agenda.

**Falha FECHADA.** Sem contexto de paciente (scripts de dev) ou com o banco fora do ar,
`_own_google_event_ids` devolve conjunto vazio — ou seja, esconde TUDO. O erro barato é a
agente dizer "ocupado" sem detalhe; o caro é contar a um paciente o nome de outro. Há teste
fixando isso (`test_without_patient_context_everything_is_hidden`).

**O `id` do Google some junto, de propósito.** Além de não servir para nada aqui, é o
argumento que `cancel_event` aceita — um id de terceiro na mão do modelo fica a uma
alucinação de distância de cancelar a consulta de outra pessoa. Efeito colateral desejado,
não escopo extra: `list_patient_appointments` já omitia ids exatamente por isso.

**Forma nova do payload** (`{"busy": [...]}`):

| Caso | O que a ferramenta devolve |
| --- | --- |
| evento de terceiro | `{"start", "end", "do_paciente": false}` |
| evento do próprio paciente | tudo o que vinha antes + `"do_paciente": true` |

**Onde:** `ai/tools.py::check_availability` (não em `services/calendar.py`) — o hub do médico
(`api/hub/calendar.py`) mostra a agenda **para a própria clínica** e continua vendo os
títulos, que é legítimo. A regra é sobre o que a AGENTE pode citar, e por isso mora em `ai/`.

**Texto do prompt ajustado junto** (`ai/prompts.py`): o *"mencione brevemente o conflito"*
virou *"diga só que aquele horário não está disponível"*, com a explicação de `do_paciente`.
Ferramenta e instrução mudaram na mesma rodada — mudar só uma deixaria o modelo tentando
narrar um detalhe que não recebe mais.

**Validado:** os fakes de `check_availability` em toda a suíte (`test_admin_tenants.py`,
`test_agent_tool_enforcement.py`, `test_hub_calendar_money.py`) devolvem `[]`, então
**nenhum teste dependia do summary de terceiro passar adiante** — verificado por varredura,
não presumido. Três testes novos cobrem os três caminhos.

**Custo assumido:** uma consulta indexada a mais ao banco por chamada de
`check_availability`, dentro do loop ReAct. Só roda quando a lista de ocupados não é vazia.

## 4. Duas divergências deliberadas do prompt

**4.1 — `add_identifier`, não `add_person_name`, para o nome do paciente.**
O PreCheck usa `add_person_name`, que registra cada PARTE do nome apontando para o mesmo
token. Certo lá (um laudo, uma pessoa). **Errado aqui:** esta conversa cita outras pessoas
— os profissionais da clínica. Um paciente "Ana Clara Souza" faria uma linha do histórico
sobre "Dra. Ana Paula" virar `[PACIENTE_x] Paula`, que re-hidrata como
"Ana Clara Souza Paula" **na mensagem enviada ao paciente**. Casamento por nome completo
exato abre mão do ganho fraco (primeiro nome solto) para não corromper o nome de um médico.

O nome da CLÍNICA também não é registrado: não é dado pessoal, e
`ai/prompts.py::secretary_system_prompt` o renderiza sem máscara — tokenizá-lo só no
histórico deixaria o modelo lendo duas clínicas diferentes.

#### LIMITAÇÃO CONHECIDA: o que `add_identifier` cobre, medido

Não é cobertura de 100%, e o preço está aqui em vez de escondido. Medido rodando o motor
contra `Patient.name = "João da Silva"`:

| Forma que o paciente escreve | Resultado |
| --- | --- |
| `João da Silva` (idêntico) | mascara |
| `JOÃO DA SILVA` (caixa alta) | mascara |
| `Joao da Silva` (sem acento) | mascara |
| `joao DA silva` (caixa + acento misturados) | mascara |
| `João da Silva Souza` (sobrenome extra) | mascara o prefixo, `Souza` fica |
| `João` (só primeiro nome) | **VAZA** |
| `Silva` (só sobrenome) | **VAZA** |
| `João Silva` (sem a partícula `da`) | **VAZA** |
| `João  da  Silva` (espaço duplo) | **VAZA neste pin** — corrigido a montante, ver §4.1.1 |
| `Joãozinho da Silvano` | não mascara — correto, `` funcionando |

São duas categorias diferentes, e só a primeira é o preço da decisão acima:

1. **Nome parcial (primeiro nome, sobrenome, partícula omitida) — o trade deliberado.**
   `add_person_name` cobriria, e é exatamente por isso que não foi usado.
2. **Espaçamento não-canônico (`João  da  Silva`) — NÃO é o trade, é fragilidade do
   casamento exato.** Já resolvido, mas A MONTANTE — ver §4.1.1.

#### §4.1.1 — o espaçamento foi corrigido no pacote, NÃO neste repo

`pseudonymize-core::_accent_insensitive_pattern` passou a juntar as palavras do termo com
`(?:\s+)` em vez de espaço literal, então `João  da  Silva` volta a casar. **A correção é
do pacote e ficou lá** (13/13 testes verdes no repositório dele, uncommitted no momento
desta escrita).

**Este repo NÃO reimplementou nada, e nem deve.** O pin em `pyproject.toml` continua em
`pseudonymize-core @ tag v0.1.0`, que é o código SEM a correção — logo **a linha da tabela
acima ainda descreve o comportamento real da secretarIA hoje**. O que fecha isso aqui é uma
única mudança, e ela depende de você:

> quando a tag nova do `pseudonymize-core` for cortada, **suba o pin** em
> `pyproject.toml` (`[tool.uv.sources]`, linha do `pseudonymize-core`) e rode `uv sync`.

Duplicar a lógica de normalização deste lado seria a armadilha exata que motivou extrair o
pacote: dois motores divergindo em silêncio, com PreCheck e secretarIA mascarando diferente.

#### Por que o falso positivo do `add_identifier` é seguro, e o do `add_person_name` não

Um `Patient.name` curto (o nome de perfil do WhatsApp costuma ser só "Ana") casa com o
primeiro nome de um profissional. Medido, sobre `"A Dra. Ana Paula atende terça.
Confirmo pra você, Ana?"`:

- **`add_identifier("PACIENTE", "Ana")`** → scrub vira `A Dra. [PACIENTE_x] Paula ...`, e o
  rehydrate devolve o texto **byte a byte igual ao original**. O valor registrado É o valor
  restaurado, então um falso positivo é round-trip-seguro: o custo é o modelo ver o
  primeiro nome de uma médica como token DENTRO do turno, nada corrompido chega ao paciente
  nem ao Google Calendar.
- **`add_person_name("PACIENTE", "Ana Clara Souza")`** → o mesmo falso positivo re-hidrata
  como `A Dra. Ana Clara Souza Paula ... Confirmo pra você, Ana Clara Souza?` — **corrompe,
  e na mensagem enviada ao paciente.**

Essa assimetria é o argumento real da §4.1, e é medida, não deduzida.

**4.2 — `scoped_help` precisa SIM de rehydrate, num ramo.**
O prompt levantou a hipótese de que a saída do `scoped_help` não precisaria de
re-hidratação. Metade certa, confirmado lendo `_normalize`:

- `pick.choice` — copiado pelo modelo do bloco de opções que vive no **system prompt**
  (nunca escrudado) e re-validado pelo router. **Não precisa.**
- `clarify.question` — **prosa livre do modelo, enviada literalmente ao paciente**:
  `services/flow_router.py` faz `TextBubble(body=outcome.question or "")` nas duas telas.
  Token ecoado ali é impresso na cara de uma pessoa. **Precisa, e foi feito.**

## 5. Ordem: o mapa é salvo DEPOIS do invoke, no `finally`

O mapa cresce em dois momentos — no scrub do histórico e **dentro** do loop ReAct, quando
o wrapper escruda um retorno de ferramenta. Por isso o `persist` vem depois do invoke.
E vem no `finally`, não no caminho feliz: quando um dos sentinelas
(`ShowMainMenuRequested`, `CalendarUnavailableError`, ...) dispara, o mapa já cresceu, e
re-emitir esses tokens no turno seguinte faria o modelo ler uma pessoa como duas.

`load_pseudonymizer` e `persist_pseudonymizer` são **best-effort e nunca levantam**: falha
de leitura degrada para um mapa novo (ainda mascara — só perde reuso de token), falha de
escrita custa a estabilidade do turno seguinte. Nenhuma das duas pode causar MAIS PII
saindo, só menos reuso. Há teste cobrindo isso.

## 6. Ciclo de vida do mapa — diferente do PreCheck, de propósito

O PreCheck **apaga** a linha na re-hidratação bem-sucedida, porque lá o mapa é uma segunda
cópia, que só existiria ali, do vínculo nome-sessão. **Aqui não é:** `messages.body` já
guarda as palavras do paciente em claro pela vida da conversa. Apagar não protegeria nada e
quebraria a estabilidade de token entre turnos. A linha morre com a conversa —
`ON DELETE CASCADE`, o que também faz `/dangerously-remove-context` (que apaga o `Patient`,
cascateando para `conversations`) levar a chave de re-identificação junto, sem fiação extra.

## 7. O que continua fora de escopo

- **Áudio.** `workers/tasks.py::transcribe_audio_message` manda o áudio do paciente para a
  OpenAI. Não dá para pseudonimizar voz com um motor de texto. Caminho de IA conhecido e
  não coberto.
- **`scripts/chat_tenant.py`.** Chama `invoke_agent` direto com histórico em memória.
  Terminal de dev, não caminho de paciente.
- **`check_availability` devolvendo nome de terceiro** — ver secao 3.
- Isto é **pseudonimização, não anonimização**. Dado pseudonimizado continua sendo dado
  pessoal (LGPD art. 13 §4º) — nós guardamos a chave. É medida de segurança/minimização
  (art. 46); **não** dispensa o mecanismo de transferência internacional (art. 33).

## 8. Como provar que fechou (varredura, não memória)

```
grep -rn "ChatOpenAI(" src --include=*.py     # 2 resultados: graph.py, scoped_help.py
grep -rn "\.ainvoke(" src --include=*.py      # 2 resultados: os mesmos dois
grep -rn "invoke_agent" src scripts --include=*.py
```
`invoke_agent` só é alcançado por `_invoke_agent_with_retry` <- `run_agent` (escrudado); a
única outra chamada é `scripts/chat_tenant.py`. `run_agent` tem **um** chamador em
produção: `workers/tasks.py`.

## 9. PENDÊNCIAS — nada disto foi feito

1. **Rodar a migração.** Validada offline (`alembic upgrade e3b7c1d5a9f2:a1b2c3d4e5f6 --sql`
   renderiza DDL Postgres válido; `alembic heads` mostra head único `a1b2c3d4e5f6`).
   **Nenhum banco foi tocado.**
2. **Deploy.** A migração ADICIONA tabela, então é segura de rodar PRIMEIRO — o inverso da
   `e3b7c1d5a9f2` logo abaixo dela, que é um DROP. Mas o código que LÊ a tabela contra um
   banco sem ela derruba todo turno de LLM. Ordem: migração -> **os DOIS serviços**.
   `ai/graph.py` roda dentro do `secretaria-worker`, e `secretaria_api`/`secretaria-worker`
   são deploys independentes que já divergiram em produção (2026-08-16). Provar com
   `GET /build` (`deploy_parity`), não com "eu dei push".
3. **Commit.** Nada foi commitado.
4. **Subir o pin do `pseudonymize-core` (§4.1.1).** A correção de espaçamento já está no
   pacote, mas o pin daqui é `v0.1.0`, que não a tem — então o espaço duplo **ainda vaza em
   produção**. Ação: cortar a tag nova lá, atualizar `[tool.uv.sources]` em `pyproject.toml`,
   `uv sync`, rodar a suíte. Nada a implementar neste repo.

## 10. Skill: nenhuma criada, e por quê

Verificado: nenhuma skill existente cobre "adicionar uma etapa de scrub/rehydrate a um
pipeline de LLM já existente". A `brain-shared-python-library` é sobre **empacotar** código
numa biblioteca (foi o prompt irmão, não este); a `tenant-secrets-encryption` é sobre
segredo de tenant **em repouso**, não sobre dado de paciente em trânsito para fora.

Mesmo assim **não** criei skill nova: a armadilha genérica real desta rodada — *num
pipeline agêntico o limite da FERRAMENTA é um terceiro caminho de dados que o desenho
ingênuo "escruda a entrada, re-hidrata a saída" não vê, e argumento saindo para sistema
externo é corrupção, não vazamento* — está registrada concretamente na secao 3 acima, que
é onde uma sessão trabalhando neste repo vai olhar. Uma skill seria a terceira cópia do
mesmo parágrafo sem um repositório novo onde aplicá-la.

**O gatilho para valer a pena:** um TERCEIRO serviço adotar `pseudonymize-core` num
caminho com ferramentas (o candidato é o `Brain-Message-Backend`). Aí o padrão terá três
instâncias e a skill se paga.
