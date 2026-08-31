# CHECKPOINT — Moldura de saudação + aviso de LGPD (2026-08-31)

> Estado: **BUILT, uncommitted, NÃO deployado.** Migração `c1f4a8b6d2e9` **não rodada**.
> Backend: 1963 testes verdes (`pytest -q`), `ruff check`/`format` limpos nos arquivos tocados.
> Frontend (`secretarIA-frontend`): `tsc --noEmit` limpo, 474 testes, `npm run build` ok.

## O que mudou, em uma frase

A primeira mensagem que um paciente recebe deixou de ser texto livre da clínica e virou uma
**moldura fixa de produto** com **um** slot para a clínica preencher — e, logo em seguida, o
paciente recebe uma **segunda mensagem** com os Termos/Política e um botão `✅ Concordo`.

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

## O consentimento NÃO bloqueia — e isso é deliberado

O PréCheck trava o questionário até o `Concordo`. Aqui não. A skill `conversation-flow-state`
documenta o invariante: *todo estado não-`IDLE` precisa de uma saída que não dependa de o paciente
escolhê-la*. Um `AWAITING_CONSENT` bloqueante teria como única saída o paciente tocar um botão —
a mesma forma que já estacionou conversas para sempre antes (ver `_expire_stale_llm_state`), só
que aqui trancaria o paciente para fora do agendamento **permanentemente**.

Então: as duas mensagens vão, o tap grava um `ConsentEvent(kind="terms_accepted")` com base legal
de consentimento, e quem não toca continua sendo atendido. O aviso é entregue e a aceitação
explícita é auditável quando acontece. **Se o produto quiser o gate bloqueante do PréCheck, ele
precisa vir com uma saída por tempo — não basta adicionar o estado.**

## `greeting_message` ficou órfã

Nada mais lê ou escreve a coluna; ela saiu de `TENANT_SCALAR_FIELDS`, do `TenantConfigUpdate` e do
`TenantConfigRead`. **Não** foi reaproveitada como slot de descrição de propósito: ela guarda
saudações INTEIRAS hoje, e renderizar "Olá! Sou a secretária do Dr. X…" dentro de uma moldura que
já abre exatamente assim é a duplicação ambígua que esta rodada existe para eliminar. Mantida
(não dropada) para uma migração de limpeza futura — mesmo tratamento de `greeting_buttons`.

## Por que o preview é servido pelo backend

O hub recebe `greeting_preview_template`: a moldura real desta clínica com o slot marcado por
`{{descricao}}`. O frontend faz **um** `split` e interpola o que está sendo digitado. A alternativa
— reescrever 800+ chars de copy em TypeScript — garantiria que preview e mensagem real divergissem
na primeira edição de qualquer lado. `test_preview_template_reconstitutes_the_real_message` prova
byte a byte que o que a clínica aprova é o que o paciente recebe.

## Pendências

1. **Rodar a migração `c1f4a8b6d2e9` ANTES de qualquer deploy.** Coluna nullable, então é segura
   de rodar primeiro e os dois serviços podem subir em qualquer ordem depois.
2. **Deploy dos DOIS serviços** (`secretaria_api` e `secretaria-worker`) — a mudança toca
   `workers/`, e a regra do `CLAUDE.md` se aplica: provar com `GET /build` / `deploy_parity`.
3. **Rebuild da imagem do `secretarIA-frontend`** (static export).
4. **Link dos Termos** aponta para um Google Doc em modo `/edit`. Trocar por `/view` antes de
   mandar a pacientes — `/edit` sugere permissão de escrita, e se o compartilhamento estiver
   aberto alguém pode alterar o texto que o paciente está aceitando. Mesmo problema no PréCheck.
5. **`legal_basis`** do `terms_accepted` diz "consentimento (art. 7º, I)". O `TODO_LAWYER` de
   `models/consent_event.py` continua valendo — confirmar com advogado.
6. Clínicas existentes têm `clinic_description` NULL: mandam a moldura sem descrição (renderiza
   limpo, sem buraco). Vale um aviso no hub para preencherem.
