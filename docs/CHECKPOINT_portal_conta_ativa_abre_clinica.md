# CHECKPOINT — conta ativa do Portal abre clínica nova: sem e-mail/código; nome vem da conta ou é perguntado uma vez (2026-09-24)

Parte 2/3 de `z_prompts/PLANO_BRAIN_MESSAGE_ENTRAR_TAMBEM_SESSAO_ATIVA.md` (raiz de BRAIN). O
contrato vem da parte 1: `brain-api/docs/CHECKPOINT_portal_sessao_ativa_pula_pendente.md` §3 e §8
(§8 = o nome, lado brain-api).

**Estado: BUILT, não commitado, não deployado.** Duas rodadas no mesmo dia:

1. **Rodada 1 (manhã):** só testes — o ramo `verified` do `open` já existia (§1). A decisão 3
   original ("o nome não é perguntado nem copiado") foi **revertida pelo dono** na mesma sessão.
2. **Rodada 2 (tarde):** o dono — *"É necessário que de algum jeito a clínica tenha o dado do nome
   para tanto colocar no evento da consulta, quanto em algumas mensagens do fluxo"* — escolheu as
   "camadas 1 + 2" (§2): o nome mora na conta (brain-api) e vem no `open`; se a conta não tem
   nenhum, é perguntado uma vez. Código de produção nos dois repos.

## §1 — Ponto de entrada (confirmado no código)

A parte 1 não criou campo novo no `POST /internal/brain-message/open`. O sinal "esta pessoa já é
uma conta" é o probe que já existia: para a identidade que `add_clinic` cria,
`POST /internal/brain-message/pending-identity` responde `verified`. `BrainMessageOpen` já tinha
`patient_name` opcional, gravado em `Patient.name` por `_open_brain_message_conversation`.

```
process_brain_message_open
  └─ _open_brain_message_conversation      → Patient/Conversation, sem inbound; Patient.name = patient_name do open
       └─ _first_contact_reply             → frame; probe_pending_identity=True (só Portal)
  └─ _send_bot_reply
       ├─ _send_greeting                   → mensagem 1: frame da clínica
       └─ _handle_pre_consent_identity     → probe_identity(...)
            ├─ PENDING_UNCLAIMED → AWAITING_EMAIL + pergunta de e-mail            (visitante anônimo, hoje)
            ├─ VERIFIED → _record_verified_account_consent (account_terms_verified)
            │     ├─ Patient.name presente → _handle_show_main_menu(source="verified_account")  → menu
            │     └─ sem nome (NOVO)       → AWAITING_NAME + NAME_REQUEST_MESSAGE
            │                                 └─ resposta → name_captured → report_name + menu (source="name_captured")
            └─ resto → aviso LGPD de hoje
```

O caminho "visita anônima → e-mail já cadastrado digitado → código" também converge aqui
(`_finish_account_code_before_consent` → `_handle_pre_consent_identity`): o nome que o brain-api
agora devolve na verificação (`VerifyResult.patient_name`) é gravado antes
(`_adopt_account_name`), então essa pessoa também só é perguntada se a conta não tem nome.

## §2 — Decisões

1. **Nenhum estado novo, nenhum campo novo no `open`.** Reusa `AWAITING_NAME` (que já tem saída por
   tempo e o "quer continuar?") e `_handle_show_main_menu`.
2. **Saudação = o frame de produto da clínica, sem alteração** ("👋 Olá! Bem-vindo(a) à `<clínica>`!").
   Não o `returning_greeting_message` ("que bom te ver de novo"): seria falso numa clínica nunca
   visitada e não carrega os avisos obrigatórios do primeiro contato.
3. ~~Nome não é perguntado nem copiado~~ — **revertida pelo dono (rodada 2).** A clínica precisa do
   nome: título do evento da agenda (`flow_router.py`, `f"{service_type} - {patient_name}"`),
   e-mail ao profissional (`professional_notification.py`), handoff do PreCheck, console, cliente do
   Pix. Agora:
   - **Camada 1 (a conta tem nome):** o brain-api manda no `open` (e na verificação do código); a
     secretarIA grava e vai direto ao menu. Nunca pergunta.
   - **Camada 2 (a conta não tem nome — ex.: contas antigas):** frame → "Qual é o seu nome?" → menu.
     Nunca e-mail, nunca código, nunca LGPD (o consentimento é espelhado da conta ANTES da pergunta).
   - O vínculo entre clínicas é só o `account_id` do brain-api (`cross-tenant-account-linking`); a
     secretarIA nunca procura nome por atributo.
4. **`name_captured` com consentimento já dado → menu** (antes ia sempre ao aviso LGPD). Só é
   alcançável pelo ramo novo: todo outro caminho que entra em `AWAITING_NAME` é pré-consentimento.
5. **`report_name` (Portal apenas, best effort, depois do commit):** todo nome capturado no Portal
   vai ao brain-api (`POST /internal/brain-message/patient-name`), inclusive o do fluxo de e-mail
   novo — é o que alimenta a camada 1 para a próxima clínica. Falha = só uma pergunta futura.
   WhatsApp nunca reporta.
6. **`_adopt_account_name` nunca sobrescreve** um nome digitado nesta clínica.
7. **Leitura do nome que falha = "não pergunte"** (`_conversation_patient_has_name`): o nome é
   cortesia, não pode prender uma conta verificada numa pergunta.
8. **Pseudonimização:** o nome continua só em `Patient.name`, que `load_pseudonymizer` registra
   como `PACIENTE` — o nome vindo do brain-api é mascarado como qualquer outro.

## §3 — Testes

`tests/test_brain_message_open.py`, seção "2b":

- `..._whose_name_brain_api_sends_reads_the_frame_then_the_menu` — o sintoma: `[frame, menu]`,
  `MENU`, `Patient.name` = o do open, e-mail/código trocados por funções que explodem, nada reportado.
- `..._without_a_name_is_asked_it_once_and_nothing_else` — `[frame, pergunta do nome]`,
  `AWAITING_NAME`, consentimento já espelhado.
- `test_the_name_answer_opens_the_menu_and_reaches_brain_api` — resposta → menu, nome gravado e
  reportado uma vez, sem novo probe.
- `test_two_unreadable_answers_still_reach_the_menu` — nunca preso na pergunta.
- `test_a_name_brain_api_did_not_take_does_not_hold_the_patient` — `report_name` falhando.
- `test_no_e_mail_or_consent_question_is_ever_sent_to_a_known_account[com|sem nome]`.
- `test_reopening_the_clinic_as_a_known_account_changes_nothing[com|sem nome]` — F5.
- `test_a_known_accounts_first_tap_on_the_menu_is_served_not_gated`.
- Regressão: `test_the_ordinary_open_is_unchanged_message_by_message` e
  `test_an_undecided_probe_still_falls_back_to_the_consent_notice[unknown|unavailable]`.

`tests/test_patient_name_step.py`:

- `test_portal_known_email_skips_the_name_and_asks_the_code` — agora com a verificação devolvendo
  o nome da conta: menu direto, `Patient.name` gravado, nada reportado de volta.
- `test_portal_known_email_whose_account_has_no_name_is_asked_it_after_the_code` (novo) — código →
  pergunta do nome → menu, sem LGPD.
- `test_portal_new_email_asks_the_name_before_the_lgpd` — + o nome é reportado.
- `test_whatsapp_first_contact_asks_the_name_even_with_a_profile_name` — + WhatsApp nunca reporta.

**Mutações** (arquivo restaurado do backup, conferido com `cmp`):
- rodada 1: `VERIFIED` tratado como fallback → 5 testes do ramo verificado falham;
- rodada 2: pergunta do nome desligada → 6 testes falham (os 5 "sem nome" do open + o do código).

## §4 — Validação (comandos e resultados reais, 2026-09-24, rodada 2)

- Suíte completa `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest -q` → **2399 passed, 0 falhas**,
  e nenhuma tentativa de rede do `report_name` (as suítes que não o mockavam passaram a mockar).
- `uv run ruff check` nos arquivos tocados → limpo; `ruff format --check` nos arquivos que estavam
  formatados no HEAD → limpo (`workers/tasks.py` já não estava formatado no HEAD; só os meus trechos
  foram conferidos). `ruff check .` do repo: 8 erros pré-existentes em arquivos não tocados.
- **Transcript local** (script avulso no scratchpad da sessão, não versionado; job real
  `process_brain_message_open` 2× — a 2ª é o F5 — e inbound real, SQLite em memória, probe
  respondendo o que a parte 1 responde, `report_name` gravando em vez de chamar a rede).
  `scripts/test_agent.py` não serve aqui: só o LLM, nunca passa pela abertura.

```
A) CONTA VERIFICADA que já deu o nome em outra clínica — open com patient_name='Maria Silva'
[1] SECRETARIA: 👋 Olá! Bem-vindo(a) à Clinica Nova Unidade Teste! [...]
[2] SECRETARIA: Como posso te ajudar? (opções: Serviços e Custo, Remarcar/Cancelar, Outro)
-> MENU | Patient.name='Maria Silva' | nada reportado

B) CONTA VERIFICADA sem nome em lugar nenhum — open com patient_name=None
[1] SECRETARIA: 👋 Olá! Bem-vindo(a) à Clinica Nova Unidade Teste! [...]
[2] SECRETARIA: Prazer! 😊 Qual é o seu *nome*? [...]
[3] PACIENTE  : ana souza
[4] SECRETARIA: Como posso te ajudar? (opções: Serviços e Custo, Remarcar/Cancelar, Outro)
-> MENU | Patient.name='Ana Souza' | reportado ao brain-api: ['Ana Souza']

C) VISITANTE ANÔNIMO — caminho de hoje, intocado
[1] SECRETARIA: 👋 Olá! Bem-vindo(a) à Clinica Nova Unidade Teste! [...]
[2] SECRETARIA: 📧 Antes de continuarmos, qual é o seu *e-mail*? [...]
-> AWAITING_EMAIL
```

## §5 — Achados laterais

1. **"Remarcar/Cancelar" é o menu padrão.** `flow_router.py::DEFAULT_MENU_BUTTONS` é fixo, não
   depende de agendamento — derruba a leitura do Achado 10 de `CHECKPOINT_jornada_sem_gate_e2e.md`
   ("sugere interação prévia").
2. **CORRIGIDO (rodada 3, mesma data, pedido do dono):** `parse_patient_name` aceitava rótulos
   do menu como nome — `"Serviços e Custo"` e `"Outro"` viravam o nome do paciente, nos dois canais
   desde 2026-09-20; com o nome indo para a conta, o erro passaria a ser herdado pelas próximas
   clínicas. Duas camadas:
   - `_NOT_A_NAME` ganhou o vocabulário do menu/fluxo (outro, serviços, custo, escolher, médico,
     particular, convênio, exame…) — cobre toda clínica com o menu padrão;
   - `parse_patient_name(body, not_names=...)`: a resposta INTEIRA igual a um rótulo (ignorando
     caixa, acento e emoji) é recusada; `workers/tasks.py::_menu_vocabulary(tenant)` passa os dois
     menus + o rótulo de gerenciar da clínica, cobrindo botões customizados em `initial_flows`.
   Recusa = a re-pergunta que já existia ("Hmm, não consegui entender o seu nome"), e a segunda
   recusa segue sem nome (nunca prende). Nomes reais que só CONTÊM uma dessas palavras continuam
   aceitos ("Custódio Lima", "Outília Souza"). Testes em `tests/test_patient_name_step.py`
   (parametrizados + `test_a_clinics_custom_menu_label_is_never_a_name` + um ponta a ponta por
   canal); mutação: sem o vocabulário, 8 falham; sem a checagem de rótulo, 2 falham. Suíte completa
   **2411 passed, 0 falhas**.

## §6 — Deploy e pendências

Ordem: **`alembic upgrade head` (0024, brain-api) → brain-api → secretarIA API + worker** (os dois
serviços — o worker é quem roda o `open` e o turno do nome). Compatibilidade nos dois sentidos:

- secretarIA nova com brain-api antigo: `report_name` recebe 404/erro → só loga; o `open` vem sem
  nome → pergunta uma vez. Nada quebra.
- brain-api novo com secretarIA antiga: `patient_name` no `open` já era aceito e gravado;
  `patient_name` extra na resposta de verificação é ignorado (só o status é lido); a rota
  `patient-name` só não é chamada.

Pendências: não commitado, não deployado; prova ao vivo depende da parte 3 (frontend) mandar
`X-Brain-Client: web`.
