# CHECKPOINT — pergunta de nome na abertura (WhatsApp + Portal) e ramo "e-mail já cadastrado" (2026-09-21)

Origem: `TECH/BRAIN/z_prompts/PROMPT_BRAIN_MESSAGE_ABERTURA_EMAIL_NOME_2_SECRETARIA.md` (parte 2/2
do item 2 da Prioridade 1 de `PLANO_PORTAL_COMO_WHATSAPP.md`). Consome o contrato da parte 1:
`brain-api/docs/CHECKPOINT_portal_email_ja_cadastrado.md` §3 (commit `b4ec488` no brain-api).

**Estado: IMPLEMENTADO E VALIDADO LOCALMENTE, NÃO COMMITADO, NÃO DEPLOYADO.** Sem migração
(`flow_state` é `VARCHAR(32)` sem CHECK; `AWAITING_NAME` tem 13 caracteres). Mexe em
`workers/tasks.py` → exige deploy do **`secretaria-worker`** (regra do `CLAUDE.md`), além da API.
Ordem de deploy com o brain-api é livre: a leitura dos campos novos é best-effort (§5).

## §1 — Causa raiz (confirmada no código antes de programar)

- Nenhum canal **perguntava** o nome. `Patient.name` só era preenchido de lado: pelo
  `contact.profile.name` do Meta em `_persist_inbound_message` (criação do `Patient` e o
  `elif patient_name and not patient.name`), e pelo regex oportunista
  `extract_patient_name` ("meu nome é…"), aplicado em `_route_inbound_turn` só quando
  `patient.name` está vazio. Não existia nenhuma pergunta de nome em `flow_router.py` — a
  "pergunta tardia no agendamento" citada no plano-mãe não existe; esta é a primeira.
- A pseudonimização **já cobria** o campo: `services/pii_pseudonymization.py::load_pseudonymizer`
  registra `Patient.name` como `PACIENTE` (`add_identifier`) em todo turno, e
  `ai/graph.py::run_agent` aplica `scrub_messages` ao histórico inteiro vindo do banco antes de
  `invoke_agent`. Por isso a resposta é gravada **só** em `Patient.name` — nenhum caminho novo
  de mascaramento foi criado (skill nova `pii-field-capture-pseudonymization`).
- `claim_email` lia só o status code; o 200 do brain-api passou a trazer
  `account_exists`/`email_masked` e ninguém os lia.

## §2 — A sequência nova

```
WhatsApp:  saudação -> "Prazer! Qual é o seu nome?" -> (resposta) -> LGPD
Portal:    saudação -> e-mail -> [account_exists=false] "✅ E-mail anotado! Prazer!..." -> (resposta) -> LGPD
                              -> [account_exists=true ] card do código (máscara) -> código -> conta verificada -> menu
```

Pontos de decisão por canal (um em cada entrada, padrão `channel-aware-dispatch`):
`workers/tasks.py::_asks_name_at_first_contact` (WhatsApp) e
`workers/tasks.py::_continue_after_email_claim` (Portal). A leitura da resposta é neutra em
canal: o bloco `AWAITING_NAME` em `_route_inbound_turn`, acima do bloco de identidade do Portal e
do portão de LGPD. Nenhuma referência a e-mail entra no ramo do WhatsApp.

## §3 — Cópia exata das mensagens (`services/patient_name.py`)

- `NAME_REQUEST_MESSAGE` (WhatsApp e re-pergunta após "Sim"):
  `"Prazer! 😊 Qual é o seu *nome*?\n\nAssim eu sei como te chamar durante o atendimento.\n\n✍️ É só digitar aqui."`
- `NAME_REQUEST_AFTER_EMAIL_MESSAGE` (Portal, e-mail novo): `"✅ E-mail anotado!\n\n"` + a mesma.
  Decisão de copy: o Portal ganha a confirmação do e-mail porque o claim é silencioso e nada mais
  confirma o endereço; nunca ecoa o endereço (um typo pode ser o e-mail real de outra pessoa).
- `NAME_INVALID_MESSAGE`: `"Hmm, não consegui entender o seu nome. 😕\n\n✍️ Pode digitar só o seu nome? Assim, por exemplo: Maria Silva"`
- `NAME_PAUSED_MESSAGE` ("Não" ao "quer continuar?"): mesmo texto de `EMAIL_PAUSED_MESSAGE`.
- Nenhuma mensagem ecoa o nome (nem parte dele): o identificador é registrado inteiro, então
  "Obrigado, Maria!" para "Maria Silva" chegaria cru à LLM.

Ramo "e-mail já cadastrado" (`services/pending_identity.py`):
`existing_account_code_body(mask)` = `"📧 Seu e-mail já está no nosso sistema! 🙌"` +
`"🔐 Para confirmar que é você, enviei um *código de 6 dígitos* para {máscara}."` + link do
provedor quando reconhecido + `"✍️ Digite o código aqui."`, no card de 3 botões já existente.
Sem máscara válida: "para o seu e-mail". Nunca menciona consulta (não existe nenhuma).
`ACCOUNT_CODE_GIVE_UP_MESSAGE` para becos sem saída, seguido do LGPD.

## §4 — Regra de validação do nome (`parse_patient_name`)

Trim + colapsa espaços; recusa qualquer "?" ou "," (pergunta ou lista nunca é nome); remove UMA
introdução no começo ("meu nome é", "me chamo", "pode me chamar de", "sou (o|a)"; com
"oi/olá/opa" opcional antes); cada palavra só letras (hífen e apóstrofo internos permitidos) —
recusa dígitos, emoji, e-mail, "/menu"; recusa se QUALQUER palavra for de uma lista de não-nomes
(saudações, "sim/não", "quero", "agendar", "consulta", pronomes e verbos de resposta como
"você/tenho/pode/ser/atendem"…); até 6 palavras, e sem introdução no máximo 4 palavras que não
sejam partícula; 2 a 80 caracteres; primeira palavra não é partícula nem letra única; Title-Case
com partículas minúsculas ("maria DA silva" → "Maria da Silva"). Motivo do rigor (achado 1 do
Reviewer): aceitar "Vocês atendem Unimed" apagaria o `profile.name` real no WhatsApp. Se falhar, cai no
`extract_patient_name` existente ("meu nome é Ana, tudo bem?" → "Ana").

Resposta inválida → re-pergunta UMA vez (`flow_step = "name_reasked"`); segunda inválida →
segue para o LGPD sem nome (no WhatsApp o `profile.name` fica como fallback). A resposta
válida **sobrescreve** o `profile.name` (decisão 1 do dono); um `profile.name` posterior nunca
sobrescreve o nome digitado (guarda `not patient.name` pré-existente).

Quem é perguntado: WhatsApp — sempre num `Patient` criado por esta mensagem; num `Patient` que
já existia (primeiro contato de novo só porque o histórico foi apagado) só se ainda não houver
nome. Portal — sempre no ramo "e-mail novo" antes do consentimento (o nome que o frontend manda
em `patient_name` também é tratado como não confiável, simétrico à decisão do WhatsApp). Paciente
que já consentiu nunca é perguntado (ex.: "📩 Mudar e-mail" no card pós-agendamento mantém o
comportamento anterior, sem pergunta de nome).

## §5 — Contrato consumido (brain-api parte 1) e compatibilidade

`claim_email` agora devolve `ClaimResult(outcome, account_exists=False, email_masked=None)` (mesmo
estilo de `RequestCodeResult`). `account_exists` só é True se o JSON trouxer o booleano `true`;
qualquer ausência/tipo errado/corpo ilegível = "brain-api antigo" = `False` = comportamento de
paciente novo. `email_masked` só é aceito com `account_exists` e se passar em
`masked_email_or_none`; um endereço cru vindo por engano é descartado. Log: `account_exists` e
`has_email_mask`, nunca a máscara.

## §6 — Decisão: o ramo "e-mail já cadastrado" REAPROVEITA `AWAITING_EMAIL_CODE`/`verify_code`

Sem estado novo. O parsing do código, o card de 3 botões, os ids e o `verify_code` são os mesmos;
o que muda é só a copy e o destino de sucesso/fracasso, decididos por **consentimento**
(`patient_owes_consent`, lido junto com o `Patient` em `_send_bot_reply`): uma espera de código
antes do LGPD só pode ser este ramo (agendar exige consentimento, então todo hold/gate
pós-agendamento é de paciente que já consentiu). Não se inventou segundo mecanismo de login: a
verificação é a mesma do brain-api; sucesso → `_handle_pre_consent_identity` (re-probe →
`VERIFIED` → espelha o consentimento da conta como `account_terms_verified` → menu), exatamente o
caminho de quem já chega verificado (`cross-tenant-account-linking` intacta). Pré-consentimento:
"Voltar" → LGPD (não menu); reenvio → mesma copy; becos → `ACCOUNT_CODE_GIVE_UP_MESSAGE` + LGPD;
"Não" no "quer continuar?" → `EMAIL_PAUSED_MESSAGE` (nunca "sua consulta continua marcada").
Se o `request_code` falhar logo após o claim, segue direto para o LGPD — ainda sem perguntar o
nome a quem é conhecido.

## §7 — TTL de silêncio nos dois canais

`AWAITING_NAME` entrou em `_expire_stale_pending_identity_state` (mesmo teto
`pending_identity_ttl_minutes` = 60 min, sem gate de config do tenant). Ao expirar, o bloco
`AWAITING_NAME` arma o mesmo "quer continuar?" (`_pending_identity_reactivation_offer`, que ganhou
`channel=` e `pre_consent=`), com o prefixo "Seu atendimento ficou pausado antes da etapa de
privacidade.". "Sim" → re-pergunta o nome; "Não" → pausa; a mensagem seguinte encontra o lembrete
de LGPD. A resposta que chega junto com a expiração NÃO é aceita como nome (vira a oferta).
Qualquer resposta que não seja Sim/Não **consome** a oferta (achado 2 do Reviewer — não pode virar
laço cujo único saída é um botão): se for um nome válido, é gravado e segue o LGPD; senão cai no
lembrete de LGPD. No WhatsApp o estado `AWAITING_NAME` só é gravado em `_send_bot_reply`, depois
do gate de entitlement e imediatamente antes de enviar a pergunta (achado 4): um turno que não
envia nada não deixa a próxima mensagem ser lida como nome.

## §7.1 — PII: respostas que NÃO viraram `Patient.name` (achado 3 do Reviewer)

O pseudonymizer só mascara o que está registrado. Uma resposta recusada ("Ana, tudo bem?" antes
do re-ask; "Beatriz 😊" duas vezes no Portal) contém um nome que ninguém registrou. Por isso
`ai/graph.py::_load_history` substitui, **só na visão da LLM**, toda linha do paciente que segue
uma linha do bot reconhecida por `services/patient_name.py::is_name_question` (pergunta, pergunta
pós-e-mail, re-ask, prefixo da oferta de pausa pré-consentimento) por
`NAME_ANSWER_LLM_PLACEHOLDER`. Sem coluna nova, sem migração; o console da equipe continua vendo o
texto real. Não se registrou cada palavra como identificador (colidiria com nome de médico —
CHECKPOINT_pseudonimizacao §4).

## §8 — Validação (comandos e resultados reais)

- `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest -q -p no:cacheprovider tests` →
  **2377 passed** (baseline antes: 2331; +46 em `tests/test_patient_name_step.py`).
- Testes antigos que mudaram de expectativa **de propósito** (a abertura ganhou um passo):
  `test_lgpd_consent_gate.py`, `test_bot_allowlist.py::test_greeting_uses_tenant_whatsapp_credentials`,
  `test_brain_message_pipeline.py` (paridade: `send_consent_notice`/`send_name_request` agora
  diferem por canal, e são afirmados), `test_brain_message_email_otp_inline.py` (fakes de
  `claim_email` → `ClaimResult`; resposta de nome inserida depois do e-mail).
- Prova de que mordem: neutralizar `_asks_name_at_first_contact` → 6 falhas; trocar o
  `add_identifier("PACIENTE", …)` de `load_pseudonymizer` por `pass` → o teste de mascaramento
  falha (o histórico chega com "João da Silva" cru); neutralizar a redação em `_load_history` →
  3 falhas. Restaurados.
- Revisão independente (Reviewer, sessão de 2026-09-21): CHANGES_REQUIRED com 3 MAJOR + 1 MINOR
  + 1 NIT; todos corrigidos (§4, §7, §7.1; `channel` virou kw obrigatório em
  `_pending_identity_reactivation_offer`). Resíduo pré-existente registrado: a oferta de pausa do
  passo de **e-mail** do Portal mantém o formato só-Sim/Não (não introduzido aqui).
- Mascaramento provado por
  `test_patient_name_step.py::test_the_captured_name_reaches_the_llm_only_as_a_pacient_token`:
  fluxo real do WhatsApp, nome digitado em minúsculas, conversa movida para `FlowState.LLM`,
  `run_agent` carregando o histórico do banco; só `invoke_agent` é espionado. A menção livre
  ao nome no turno LLM chega como `[PACIENTE_xxxx]`; a própria resposta do passo chega como o
  placeholder. (Em SQLite o `created_at` tem resolução de segundo, então o helper do teste
  re-carimba as linhas pela ordem de inserção antes; no Postgres isso não é necessário.)
- `uv run ruff check .` → 8 erros, **os mesmos 8 do HEAD** (E501/UP042 em arquivos não tocados);
  `ruff format --check .` → 64 arquivos, igual ao HEAD. Arquivos novos formatados; nenhum
  reformat em massa (ver memória `secretaria-make-lint-red-at-head`).

## §9 — Pendências / limites conhecidos

- Não commitado, não deployado. Deploy: `secretaria_api` + `secretaria-worker` juntos.
- Nomes com menos de 3 caracteres ("Zé") são aceitos mas não mascarados (`MIN_TERM_LEN` do
  `pseudonymize-core`); espaços duplos no texto cru dependem do pin do `pseudonymize-core`
  (CHECKPOINT_pseudonimizacao §4.1.1).
- Resíduos aceitos pelo Reviewer (READY, não bloqueiam): (a) só a PRIMEIRA mensagem após a
  pergunta é redigida — "Ana" e "Souza" em duas bolhas antes da resposta do bot deixam "Souza"
  cru para a LLM; (b) se a pergunta sair da janela de histórico da LLM e a resposta não, a
  resposta não é redigida; (c) no WhatsApp o estado é gravado logo antes do envio — se o envio
  pela Graph API falhar, a espera dura até o teto de 60 min (a oferta não entra mais em laço).
- Escopo de tenant de `account_exists` segue em aberto do lado do brain-api (§4 do checkpoint
  dele) — esta parte só consome o booleano.
