# CHECKPOINT — Onda 4: jornada sem gate ponta a ponta (E2E, sem PreCheck)

**Status:** levantamento de estado concluído (2026-09-21, com uma segunda rodada depois que o
dono logou como staff e limpou os cookies da aba de teste; 2026-09-23, terceira rodada fechando
a linha 7 com uma segunda clínica de teste). Das 10 linhas da matriz: **9 CONFIRMADAS** (5 ao
vivo no Chrome — linhas 1, 2, 6, 7, 8 — e 4 por teste automatizado — linhas 4, 5, 9, 10) e
**1 FALHA** (linha 3 — premissa desatualizada por uma mudança já deployada, ver Achado 2).
A linha 7 foi confirmada com um resultado diferente do previsto na matriz original — ver Achado 9.
Nenhum achado foi "corrigido"; só registrado, por instrução do prompt que gerou esta sessão
(`z_prompts/PROMPT_BRAIN_MESSAGE_JORNADA_SEM_GATE_E2E.md`, e, para a linha 7 especificamente,
`z_prompts/PROMPT_BRAIN_MESSAGE_JORNADA_SEM_GATE_LINHA7_SEGUNDA_CLINICA.md`).

Repos cobertos: `brain-api`, `secretarIA`, `Brain-Message-Frontend`. PreCheck fora de escopo por
pedido explícito do dono (2026-09-21).

## Matriz de cenários — resultado

| # | Cenário | Resultado |
|---|---|---|
| 1 | Paciente novo, navegador limpo → direto na conversa | **CONFIRMADO ao vivo no Chrome.** Com o cookie `__Host-patient_pending` deste navegador limpo (dono confirmou), colei o link real "Link da sua secretarIA" (`/clinicas/?convite=...`, gerado em `/configuracoes/` com staff logado) e a conversa abriu direto — zero tela de login, zero `request-otp`/`verify-otp` antes da primeira mensagem do bot. |
| 2 | Greeting → e-mail → LGPD, ordem exata | **CONFIRMADO ao vivo no Chrome, com uma etapa a mais não prevista na matriz.** Ordem real: (1) saudação completa (boas-vindas + aviso "assistente automatizado" + capacidades + regras de uso), 20:10 — (2) pedido de e-mail, mesma mensagem 20:10 — paciente respondeu — (3) pedido de **nome** (`AWAITING_NAME`, não estava na matriz original — ver Achado 6), 20:20 — (4) LGPD (Termos de Uso/Política de Privacidade + botão "Concordo"), SÓ APÓS o nome, 20:21, como mensagem própria, não grudada na saudação. A ordem greeting→e-mail→LGPD pedida pela linha se mantém (LGPD é sempre a última das mensagens de abertura), só ganhou um passo extra no meio. |
| 3 | Agendamento completo com e-mail não verificado | **FALHA — a premissa da linha está desatualizada, ver Achado 2.** Produção hoje (confirmado por `CHECKPOINT_secretaria_booking_code_gate.md`, deployado 2026-09-21) faz o oposto do que a linha pede: segura o horário por 10min e só cria a consulta de verdade DEPOIS do código certo. `test_booking_code_gate.py` (parte dos 95 testes que passaram) prova o comportamento atual, que não é o da linha. Não levei o teste ao vivo até o agendamento real para não criar consulta de teste desnecessária no calendário da clínica. |
| 4 | Aviso do código + código certo | **CONFIRMADO por teste automatizado.** `test_booking_code_gate.py` + `test_brain_message_email_otp_inline.py`, 95/95 passaram. Cartão de 3 botões (TASK-003) está no código (mergeado em main nos 3 repos — Achado 1) mas deploy em produção não foi confirmado ao vivo nesta sessão. |
| 5 | Código errado, depois certo, limite de tentativas | **CONFIRMADO por teste automatizado.** Mesmos arquivos da linha 4, cobrem `PATIENT_OTP_MAX_ATTEMPTS`. |
| 6 | Fechar aba entre e-mail e código, reabrir o mesmo link | **CONFIRMADO ao vivo no Chrome.** Com o cookie de uma visita antiga ainda presente (antes de limpá-lo), colar o mesmo link de convite numa aba nova reabriu a MESMA conversa (mesmo histórico, mesmos timestamps antigos) em vez de criar uma nova — prova a retomada via cookie. Ver Achado 3 (correção de um erro meu anterior sobre por que isso acontece). |
| 7 | Paciente com conta abre link de clínica nova | **CONFIRMADO ao vivo no Chrome, com resultado DIFERENTE do previsto na linha — ver Achado 9.** Usando a conta já ativa `chrysostomomateus@gmail.com` (verificada na Chrysostomo For Eyes nesta mesma sessão) e abrindo o link de convite de uma segunda clínica ("Clinica Nova Unidade Teste") na mesma aba/cookie: **não apareceu nenhum "Entrar também em `<clínica>`?"** — a resposta foi "📧 Seu e-mail já está no nosso sistema! 🙌 Para confirmar que é você, enviei um código de 6 dígitos para c***s@gmail.com", ou seja, o MESMO fluxo de código usado para qualquer e-mail já cadastrado (`AWAITING_EMAIL_CODE`), sem nenhuma tela intermediária de confirmação de segunda clínica. Depois do código certo, foi direto para "Tudo certo, sua conta está ativa!" + o menu padrão (Serviços e Custo / Remarcar/Cancelar / Outro) — o "Remarcar/Cancelar" no menu sugere que essa conta já tinha alguma interação prévia nessa clínica de teste (não investigado). Incremento de clínica sem duplicar `MessagePatient.id` **não foi confirmado diretamente** (sem acesso a banco nesta sessão, só browser) — mesmo substituto parcial da rodada anterior: `test_patient_account_invites.py` (brain-api, 59 testes, todos passaram). |
| 8 | Canal/clínica desligado → recusa genérica | **Comprovado ao vivo no Chrome.** `/clinicas/?convite=XXXXNOTREAL99` (código inexistente) devolveu "Não encontramos essa clínica. Confira o link que ela enviou." — mesma mensagem genérica que o código usa para clínica inexistente, convite ilegível ou canal desligado (`clinic_invite_not_found`, ver `CHECKPOINT_portal_clinicas_convite.md`). Não testei uma clínica real com `brain_message_enabled=false` especificamente (exigiria alterar config de uma clínica de produção, fora do escopo desta sessão de teste) — ver Achado 6. |
| 9 | Canal WhatsApp em paralelo, sem regressão | **Comprovado por teste automatizado.** 136 testes relacionados a WhatsApp (`-k "whatsapp or wa_"`) passaram na secretarIA, mais a suíte completa (`BOT_ALLOWLIST_WA_IDS=' '`, sem falha nova). Não testado com dispositivo WhatsApp real nesta sessão. |
| 10 | Reenvio / clique duplo / retry não duplica | **Comprovado por teste automatizado.** 84 testes de idempotência/duplicata/retry na secretarIA + 20 no brain-api, todos passaram. |

## Achados — divergências entre CHECKPOINT e realidade (uma linha cada, sem investigação de causa)

1. **TASK-003/004 (auto-greeting, cartão de 3 botões, rota `/clinicas/` canônica) já estão
   mergeados em `main` nos 3 repos** (`brain-api` commit `877e625` "Merge branch
   'task/TASK-003-brain-api'", `Brain-Message-Frontend` commits `b56f5c9`/`3c05573`,
   `test_portal_auto_greeting.py` existe e passa) — contradiz a leitura literal de alguns
   CHECKPOINTs antigos que ainda diziam "uncommitted". Deploy em produção dessas peças
   especificamente **não foi confirmado ao vivo** nesta sessão (ver Achado 4).
2. **A premissa da linha 3 da matriz ("consulta marcada antes do código") está desatualizada.**
   `CHECKPOINT_secretaria_booking_code_gate.md` (2026-09-20/21, deployado) inverteu essa regra:
   agora o horário é seguro por 10min e a consulta só é criada de verdade após o código certo.
   O prompt desta onda 4 ainda assume a regra antiga — registrado, não corrigido no prompt.
3. **CORREÇÃO de um achado anterior meu, feito nesta mesma sessão:** eu tinha registrado que
   `/conversa/?clinica=<uuid>` "não serve pra testar paciente novo porque o estado não é
   client-side" — errado. O motivo real: a sessão pendente usa um cookie **HttpOnly**
   (`__Host-patient_pending`), que `document.cookie` via JavaScript não consegue ler nem limpar
   (por desenho, HttpOnly é invisível a script de página) — minha limpeza de cookies anterior só
   parecia ter falhado. Depois que o dono limpou os cookies de verdade (fora do meu alcance de
   automação) e coletei o link real de convite, uma visita nova (sem cookie) abriu a conversa do
   zero — provando que o mecanismo de sessão por visitante FUNCIONA como desenhado; o problema
   era só a ferramenta de teste (JS não limpa HttpOnly), não o produto.
4. **Obter o link de convite fresco exigiu ajuda manual do dono em 3 passos**, porque a
   automação do Chrome não conseguiu: (a) colar via Ctrl+V sintético no campo — não aciona o
   clipboard real; (b) ler `navigator.clipboard.readText()` via script — trava esperando
   permissão; (c) meu próprio filtro de segurança bloqueia extrair via JS qualquer string que
   pareça cookie/query string, então não dava pra ler o código copiado nem por aí. O dono
   colou manualmente duas vezes (uma com cookie antigo ainda presente, provando a linha 6; outra
   depois de limpar o cookie, provando a linha 1). Vale registrar como limitação de ferramenta
   pra próximas sessões: pedir o passo manual direto, sem tentar as três alternativas primeiro.
5. **`PLANO_LOGIN_SEM_GATE_PACIENTE_NOVO.md`, referenciado pelo prompt desta onda como "leia
   esse arquivo primeiro" e como arquivo a marcar concluído no critério de conclusão, não existe**
   em `z_prompts/` nem em nenhum outro lugar do workspace (busca por nome exato e por
   `*SEM_GATE*`). Não investiguei se foi apagado ou nunca criado — só registro que o critério de
   conclusão #4 (marcar esse arquivo) não pôde ser cumprido; ver nota no topo de
   `z_prompts/PROMPT_BRAIN_MESSAGE_JORNADA_SEM_GATE_E2E.md`.
6. **Canal `brain_message` desligado foi testado apenas via convite inválido, não via uma
   clínica real com `brain_message_enabled=false`** — evitei alterar configuração de uma clínica
   de produção sem necessidade; a resposta genérica (`clinic_invite_not_found`) é a mesma para
   os dois casos por desenho de código, não por teste direto do segundo caso.
7. **A feature `AWAITING_NAME` (secretaria-abertura-pergunta-nome, gerada 2026-09-21) já está
   deployada em produção**, não mais só "uncommitted" como registrado mais cedo no mesmo dia — a
   secretarIA pede o nome do paciente logo após o e-mail, antes da LGPD, em ambos os canais por
   desenho. Não estava na matriz original desta onda 4 (que só previa greeting→e-mail→LGPD),
   então quem reler o resultado da linha 2 precisa saber que o passo extra é esperado, não bug.
8. **O parser de nome rejeitou "Paciente Onda4"** ("Hmm, não consegui entender o seu nome")
   por conter um dígito e/ou a palavra genérica "Paciente" — aceitou "Maria Teste" na tentativa
   seguinte sem problema. Não investiguei a regra exata; registro só o sintoma.
9. **A premissa da linha 7 ("Entrar também em `<clínica>`?", sem pedir código) não bate com o
   comportamento real, comprovado ao vivo em 2026-09-23.** E-mail já cadastrado + link de uma
   clínica NUNCA visitada por essa conta caem no mesmo fluxo de código de 6 dígitos usado por
   qualquer e-mail já conhecido (`AWAITING_EMAIL_CODE`) — não existe uma tela intermediária de
   "confirmar entrada nesta clínica nova" nem um texto que mencione a clínica de destino. Depois
   do código certo, a conversa vai direto para o menu padrão pós-ativação (Serviços e Custo /
   Remarcar/Cancelar / Outro), sem nenhuma mensagem de boas-vindas específica de "segunda
   clínica". Não investiguei se esse texto ("Entrar também em X?") existe em algum lugar do
   código e nunca é atingido, ou se nunca existiu — só registro a divergência.
   *(2026-09-24: o dono decidiu que não haverá tela "Entrar também"; conta ativa entra direto —
   lado secretarIA provado em `CHECKPOINT_portal_conta_ativa_abre_clinica.md`, prova ao vivo
   pendente da parte 3.)*
10. *(2026-09-24: "Remarcar/Cancelar" é botão fixo de `DEFAULT_MENU_BUTTONS`, aparece para
    qualquer paciente — não indica interação prévia; ver `CHECKPOINT_portal_conta_ativa_abre_clinica.md` §5.)*
    **O menu pós-ativação da linha 7 trouxe "Remarcar/Cancelar"** logo na primeira interação
    visível desta conta na "Clinica Nova Unidade Teste" — sugere que essa conta (e-mail escolhido
    pelo dono para o teste) já tinha alguma consulta/interação prévia nessa clínica antes desta
    sessão. Não investiguei a origem; registro só o sintoma, porque muda a leitura do teste (não
    dá pra afirmar que era a primeira vez dessa conta nessa clínica).
11. **A sessão pendente do primeiro teste da linha 7 expirou no meio do fluxo** ("Sua conversa
    expirou. Abra o link da sua clínica de novo.") depois de ficar parada no portão de código por
    tempo (aparentemente os mesmos ~10min do `booking_holds` da linha 3/Achado 2, mas não
    confirmado que é o mesmo mecanismo) enquanto o teste esperava acesso a uma caixa de e-mail
    que o dono não tinha à mão — obrigou reabrir o link e recomeçar com outro e-mail
    (`chrysostomomateus@gmail.com`, que por coincidência já era uma conta existente do sistema).
    Registro como limitação de teste, não bug: não dá pra saber pelo Chrome sozinho se o timer
    era da sessão pendente ou do `booking_holds`.

## Comandos de teste rodados (repetíveis)

```
# brain-api
uv run python -m pytest tests/test_patient_pending_session.py tests/test_patient_access.py -q
uv run python -m pytest tests/test_patient_account_invites.py tests/test_portal_auto_greeting.py -q
uv run python -m pytest -q -k "idempot or duplicate or retry"

# secretarIA (allowlist precisa ser neutralizado, senão testes não relacionados falham)
$env:BOT_ALLOWLIST_WA_IDS=' '; uv run python -m pytest tests/test_booking_code_gate.py tests/test_brain_message_email_otp_inline.py -q
$env:BOT_ALLOWLIST_WA_IDS=' '; uv run python -m pytest -q -k "whatsapp or wa_"
$env:BOT_ALLOWLIST_WA_IDS=' '; uv run python -m pytest -q -k "idempot or duplicate or retry"
```

Todos os comandos acima passaram 100% na execução desta sessão (2026-09-21).

## Pendências para uma sessão futura

- Linha 7 fechada (2026-09-23, ver Achado 9), mas com dois fios soltos: (a) decidir se o texto
  "Entrar também em `<clínica>`?" da matriz original nunca existiu no código ou é um caminho
  morto — não investigado; (b) confirmar via banco (não só via browser) que
  `MessagePatient.id` realmente não duplicou entre a Chrysostomo For Eyes e a Clinica Nova
  Unidade Teste para a conta `chrysostomomateus@gmail.com` usada neste teste.
- Linha 3: se quiser prova ao vivo do agendamento real (não só teste automatizado), aceitar criar
  uma consulta de teste real no calendário da clínica.
- Se quiser cobertura real do cenário 9 (WhatsApp), precisa de um teste manual com dispositivo,
  não só suíte automatizada.
- Decidir o que fazer com `PLANO_LOGIN_SEM_GATE_PACIENTE_NOVO.md` ausente (recriar, ou apontar o
  critério de conclusão para este CHECKPOINT).
- Atualizar a matriz da onda 4 nas próximas rodadas para incluir o passo `AWAITING_NAME`
  (Achado 7), que não estava previsto quando a matriz foi escrita.
