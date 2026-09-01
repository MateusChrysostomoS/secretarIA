# CHECKPOINT — Moldura de saudação + aviso de LGPD (2026-08-31)

> Estado: **BUILT, uncommitted, NÃO deployado.** Migrações `c1f4a8b6d2e9`, `d2a5b9c7e3f1` e
> `e3b7c1d5a9f2` **não rodadas** — e a última tem ordem de deploy INVERTIDA (ver Pendências).
> Backend: 1976 testes verdes (`pytest -q`), `ruff check`/`format` limpos nos arquivos tocados.
> Frontend (`secretarIA-frontend`): `tsc --noEmit` limpo, 484 testes, `npm run build` ok.

## O que mudou, em uma frase

A primeira mensagem que um paciente recebe deixou de ser texto livre da clínica e virou uma
**moldura fixa de produto** com **um** slot para a clínica preencher — e a conversa passou a ser
**sequenciada por um gate de LGPD**, espelhando o PreCheck.

## A sequência exata

| # | O que sai | Botões |
|---|---|---|
| 1 | Moldura (nome da clínica + descrição + obrigações) | **nenhum** — texto puro |
| 2 | Termos de Uso + Política de Privacidade | `[✅ Concordo]` |
| 3 | *(qualquer coisa que não seja aceite)* → reenvio dos termos | `[✅ Concordo]` |
| 4 | Após o aceite: "O que você precisa?" | `[🗓️ Agendar] [Outro]` |
| 5 | Fluxo determinístico normal | — |

A mensagem 1 sai **sem botões de propósito**: oferecer `[Agendar]` ali convidaria um toque que o
gate recusaria no instante seguinte, e colocaria duas mensagens interativas seguidas com funções
diferentes. A mensagem 4 é a **primeira** da conversa a carregar botões de ação.

## Por que a moldura não é opcional

`Tenant.greeting_message` era NULL para a maioria das clínicas. Para essas, `_select_greeting`
devolvia `None` e a LLM improvisava a abertura — sem dizer que é um assistente automatizado, sem
dizer que ali não se dá orientação médica, sem saída de emergência. Essas obrigações não podem
depender de a clínica ter preenchido um campo, exatamente como os botões da saudação deixaram de
depender disso em `CHECKPOINT_fixed_greeting_buttons.md`. É o mesmo movimento, pelo mesmo motivo.

## Onde está cada peça

| Camada | Arquivo | O que faz |
|---|---|---|
| Copy + regras | `services/greeting_template.py` | `GREETING_FRAME`, `render_greeting`, `clinic_description_budget`, `greeting_preview_template`, constantes de LGPD. Puro: sem DB, sem clock. |
| Coluna | `models/tenant.py` + migração `c1f4a8b6d2e9` | `tenants.clinic_description` (nullable). |
| Envio | `workers/tasks.py` | `_select_greeting` renderiza a moldura; `_send_consent_notice` manda a 2ª mensagem; `_is_consent_acceptance` trata o tap. |
| Contrato do hub | `schemas/config.py`, `services/hub_configuration.py` | PUT aceita `clinic_description`; GET devolve `greeting_preview_template` + `clinic_description_max`. |
| UI | `secretarIA-frontend` `MessagesSection.tsx::GreetingComposer` | Campo "Descrição da clínica" + preview ao vivo da mensagem inteira. |

## As três armadilhas que custaram tempo (não repita)

**1. O orçamento é apertado e EXATO.** A saudação sempre vai com botões, então a mensagem inteira
tem que caber em **1024** chars (corpo interativo), não em 4096. A moldura come ~843, sobrando
**~180** para a clínica — e esse número **encolhe conforme `clinic_name` cresce**, por isso
`clinic_description_budget` é função, nunca constante. `send_buttons` **não trunca**: um char a
mais é 400 da Meta, `_send_greeting` loga e segue, e o paciente não recebe **nada**.

**2. O bug de ±2 no orçamento.** A primeira versão media a moldura com o slot **vazio** — mas o
slot vazio faz `render_greeting` colapsar as duas linhas em branco ao redor. O orçamento voltava
2 chars generoso demais, o hub aceitava no limite e a mensagem saía com 1026. Hoje a medição usa
uma sonda de 1 caractere (`_BUDGET_PROBE`). Fixado em
`test_a_description_at_the_budget_renders_exactly_at_the_cap`.

**3. `"voltar"` não existia.** A moldura promete *"Errou? Digite **voltar**"*, mas `_MENU_COMMANDS`
só tinha variantes com barra (`/menu`, `/reset`, …) — que nenhum paciente digita. A promessa seria
copy morta. As variantes sem barra foram adicionadas; o casamento continua sendo do **corpo
inteiro**, então "quero voltar na segunda" segue roteando normal.
`test_the_voltar_promise_is_backed_by_a_real_command` quebra se alguém estreitar de novo sem
editar a copy.

## O gate bloqueia — mas é um FATO, não um `FlowState`

O gate recusa atendimento enquanto `Patient.lgpd_accepted_at` for NULL: nem `Agendar`, nem
`/menu`, nem `voltar` passam. Isso espelha o PreCheck, que responde qualquer coisa que não seja
aceite com "Reenviar LGPD".

A parte que importa para quem for mexer: **isso não é um `FlowState`**. A skill
`conversation-flow-state` proíbe estado não-`IDLE` cuja única saída seja o paciente escolher — foi
essa forma que estacionou conversas em modo LLM para sempre (`_expire_stale_llm_state`). Um
`AWAITING_CONSENT` teria exatamente esse formato. Um **fato sobre o sujeito** não tem esse risco:
`flow_state` não é tocado pelo gate, então o que a conversa estivesse fazendo retoma intacto no
instante em que o aceite chega. Se alguém for transformar isto num `FlowState`, precisa levar
junto uma saída por tempo.

Duas exceções deliberadas, ambas acima do gate na escada de `_persist_inbound_message`:

- **Handover humano vence.** Consentimento governa o que o **bot** pode fazer sozinho; uma
  secretária de carne e osso que assumiu a linha não é o bot. Ter isso invertido faria o produto
  impedir uma atendente real de falar com o paciente. Fixado em
  `test_human_handover_outranks_the_consent_gate`.
- **Botões de ação de lembrete passam.** Honrar um agendamento que o paciente já fez (confirmar,
  cancelar) não é tratamento novo a consentir.

## O atributo que guarda o aceite

| | PreCheck | secretarIA (agora) |
|---|---|---|
| Onde vive | `sessions.state`: `LGPD_PENDING` → `ACTIVE` | `patients.lgpd_accepted_at` (timestamptz) |
| Responde "quando aceitou?" | **não** | sim |
| Trilha de auditoria | nenhuma | `consent_events(kind="terms_accepted")` |
| Sobrevive à limpeza | `wf-limpeza-diaria` mexe em sessions | evento sobrevive ao wipe de contexto |
| Doc dos termos | `clinics.lgpd_doc_url` existe mas **ninguém lê** — a URL está hardcoded no nó do n8n | constante de produto em `greeting_template.py` |

São **dois** registros de propósito, com tempos de vida diferentes:
`/dangerously-remove-context` apaga a linha `Patient` (e com ela a coluna) mas **não** apaga
`consent_events` — então limpar o contexto replaya um primeiro contato de verdade, incluindo ser
perguntado de novo, enquanto o registro legal do que a pessoa já aceitou sobrevive.

## A quarta armadilha: decorar o rótulo do botão

`[🗓️ Agendar]` é seguro **só porque a palavra por baixo não mudou**. Todo matcher compara via
`flow_router._norm`, que roda `strip_decoration` nos **dois** lados, então "🗓️ Agendar", "Agendar"
e um "agendar" digitado compartilham a mesma chave (FEAT 44).

`🗓️ Agendar Consulta` **não** sairia de graça: `strip_decoration` devolveria "Agendar Consulta",
que não é `LABEL_BOOK` — seria preciso renomear a própria constante e arrastar junto todos os
matchers, o prompt da LLM e `_GREETING_ACTION_IDS`. **O tamanho nunca foi o limite**: são 19 dos
20 caracteres permitidos. Um efeito colateral já corrigido: `_send_greeting` fazia
`label in _GREETING_ACTION_IDS` com o rótulo cru, o que passaria a errar em silêncio e daria ao
toque um id posicional `reactivation|N`, custando ao tenant com flows desligados o degrade
determinístico — sem nada logado.

## `greeting_message` foi REMOVIDA (migração `e3b7c1d5a9f2`)

Primeiro ficou órfã, depois foi apagada. **Não** foi reaproveitada como slot de descrição de
propósito: ela guardava saudações INTEIRAS, e renderizar "Olá! Sou a secretária do Dr. X…" dentro
de uma moldura que já abre exatamente assim é a duplicação ambígua que esta rodada existe para
eliminar.

O que saiu junto:

- `Tenant.greeting_message` e `TenantRuntimeConfig.greeting_message` — este último era **escrito
  por dois lugares e lido por nenhum** (conferido por grep antes de apagar, não presumido).
- O fallback de reativação em `_reactivation_offer`, que reusava o "pitch de boas-vindas" para
  um tenant opted-in sem `returning_greeting_message`. A **feature ficou**; só a fonte seguiu o
  pitch para onde ele mora agora, `clinic_description` — e ficou mais leve, já que aquele slot é
  capado em ~180 chars contra os 1024 da coluna antiga.
- `scripts/apply_config.py` (allowlist) e o seed `clinica-psi-infantil.json`, cuja saudação de
  416 chars virou o pitch de ~80 que cabe na moldura.
- Os tipos mortos em `brain-frontend/lib/secretaria-hub.ts` (as telas da secretarIA saíram
  daquele repo em 2026-08-24, mas a declaração ficou).

`brain-api` tem **zero** referências ao campo — conferido, porque num mesh hub-and-spoke a
ausência num spoke costuma significar que o hub é o dono, não que ninguém usa.

Sobrou apenas
(não dropada) para uma migração de limpeza futura — mesmo tratamento de `greeting_buttons`.

## Por que o preview é servido pelo backend

O hub recebe `greeting_preview_template`: a moldura real desta clínica com o slot marcado por
`{{descricao}}`. O frontend faz **um** `split` e interpola o que está sendo digitado. A alternativa
— reescrever 800+ chars de copy em TypeScript — garantiria que preview e mensagem real divergissem
na primeira edição de qualquer lado. `test_preview_template_reconstitutes_the_real_message` prova
byte a byte que o que a clínica aprova é o que o paciente recebe.

## Pendências

1. **`c1f4a8b6d2e9` e `d2a5b9c7e3f1` ANTES de qualquer deploy.** Ambas só adicionam coluna
   nullable, então são seguras de rodar primeiro e os dois serviços podem subir em qualquer ordem
   depois. **Sem backfill de propósito:** todo paciente existente lê como "nunca aceitou" e é
   perguntado uma vez na próxima mensagem — backfillar um timestamp seria inventar um
   consentimento que não aconteceu.
2. **`e3b7c1d5a9f2` (o DROP) por ÚLTIMO, e a ordem é a INVERSA das outras duas.** O SQLAlchemy
   não faz `SELECT *`: emite a lista explícita de colunas do modelo. Um processo ainda no código
   antigo não perde só o campo — **toda** leitura de `tenants` levanta
   `column tenants.greeting_message does not exist`. Então: código nos **dois** serviços →
   provar por `source_fingerprint` no `/build` de cada um ("dei push" não é prova) → só então
   rodar. Depois do DROP, **rollback de código sozinho não volta atrás**: o caminho honesto é
   para a frente.
3. **Deploy dos DOIS serviços** (`secretaria_api` e `secretaria-worker`) — a mudança toca
   `workers/`, e a regra do `CLAUDE.md` se aplica: provar com `GET /build` / `deploy_parity`.
4. **Rebuild da imagem do `secretarIA-frontend`** (static export).
5. **Link dos Termos** aponta para um Google Doc em modo `/edit`. Trocar por `/view` antes de
   mandar a pacientes — `/edit` sugere permissão de escrita, e se o compartilhamento estiver
   aberto alguém pode alterar o texto que o paciente está aceitando. Mesmo problema no PréCheck.
6. **`legal_basis`** do `terms_accepted` diz "consentimento (art. 7º, I)". O `TODO_LAWYER` de
   `models/consent_event.py` continua valendo — confirmar com advogado.
7. Clínicas existentes têm `clinic_description` NULL: mandam a moldura sem descrição (renderiza
   limpo, sem buraco). Vale um aviso no hub para preencherem.
