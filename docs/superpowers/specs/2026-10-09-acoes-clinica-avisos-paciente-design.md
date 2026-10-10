# Ações da clínica na agenda avisam o paciente — design (2026-10-09)

Decisões do dono em 2026-10-09 (sessão de testes reais do R4). Este documento é a autoridade para
dois planos: **R7** (backend secretarIA — `plans/2026-10-09-lembretes-r7-avisos-acoes-clinica.md`)
e a **atualização do R5** (front Brain-Message — `plans/2026-10-03-lembretes-r5-front-agenda.md`); as §5.D e
§5.E são a autoridade do **R8** (`plans/2026-10-09-lembretes-r8-filtro-meus-pacientes-e-seletor-medico.md`).

## 1. Botões da consulta na agenda (R5)

| Momento | Botões |
|---|---|
| Consulta viva, **antes** do horário (`start_at > agora`) | **Liberar horário**, **Confirmar**, **Compareceu**, **Editar/Remarcar**, **Cancelar consulta**, Enviar mensagem (R4) |
| Consulta viva, **depois** do horário de início | **Compareceu**, **Faltou**, **Editar/Remarcar** |
| Encerrada (cancelada / compareceu / faltou) | nenhum botão de ação |

- **Faltou não existe antes do horário.** O backend recusa (`409 no_show_before_start`) — a regra não
  pode depender só da tela.
- "Remarcar" e "Editar" viram **um único botão "Editar/Remarcar"**: numa só ação o médico muda
  data/horário, serviço, médico, convênio, nome do paciente/de quem é a consulta e telefone de contato.
- Cancelar continua como hoje: o médico escolhe justificativa e se avisa ou não (decisão dele).

## 2. Toda ação avisa o paciente, no canal dele

| Ação | Mensagem ao paciente |
|---|---|
| Liberar horário | já existe (R4) |
| Confirmar | "Seu médico confirmou sua consulta de <dia> às <hora> com <médico>. Você está ciente?" + botões **Confirmar / Cancelar / Alterar Dados** — o mesmo fluxo dos botões do lembrete (Confirmar conta mais uma confirmação; Cancelar e Alterar Dados seguem o R2/R3/R6) |
| Compareceu | mensagem de pós-consulta **na hora**: o texto que a clínica configurou (`post_consult_message`); sem texto configurado, um padrão perguntando como foi a consulta. Uma vez por consulta |
| Editar/Remarcar | o que mudou ("A clínica alterou sua consulta: …" com o antes → depois de cada campo alterado) + dia/hora/médico atuais + os mesmos 3 botões |
| Cancelar | já existe no WhatsApp; **paciente do Portal hoje não recebe nada** → passa a receber no chat (+ aviso por e-mail) |
| Faltou | nada |

**Portal:** sem janela de 24 h; a mensagem vai ao chat e um e-mail genérico avisa que há mensagem
(nunca com o conteúdo — mesma regra do R4).

**WhatsApp fora das 24 h:** só modelo pago da Meta. A clínica é **perguntada toda vez**, com o custo,
e tem a opção **"não perguntar novamente"** — que grava na clínica a autorização permanente para
mensagens pagas (configurável de volta em Configurações). Com a autorização gravada, o backend envia
sem perguntar. Sem autorização e sem o "sim" daquela vez, a ação acontece e a resposta diz
`whatsapp_outside_window` (a tela avisa que o paciente não foi avisado e oferece o link gratuito wa.me).
A regra vale também para os avisos do R4 (liberar e mensagem).

## 3. Contrato (backend → front)

- Campo novo da clínica `paid_notices_auto_approved: bool` (padrão `false`), lido e gravado pela
  configuração do hub que o R5 já usa.
- Toda ação que avisa aceita `notify_outside_window: bool` (padrão `false`); autorização efetiva =
  `notify_outside_window or paid_notices_auto_approved`.
- Toda resposta de ação que avisa traz `patient_notice` com o vocabulário do R4
  (`whatsapp_queued | whatsapp_sent | whatsapp_outside_window | portal_chat | portal_chat_email |
  no_channel | queue_unavailable | notice_failed`) e, para `whatsapp_outside_window`, `whatsapp_link`.
- A tela decide antes de agir com o `GET …/cancel-preview` existente (`inside_window`,
  `template_cost_brl`, `whatsapp_link`) somado a `paid_notices_auto_approved`.
- `PATCH …/status`: `no_show` antes do início → 409 `no_show_before_start`; `confirmed`/`attended` em
  consulta encerrada → 409 `not_live` (hoje uma consulta cancelada "ressuscita" com confirmed).
- Novo `POST …/appointments/{id}/edit` (Editar/Remarcar) com todos os campos opcionais; ao menos um
  precisa mudar. O `POST …/reschedule` antigo continua funcionando até o front migrar.

## 4. Limites deliberados

- Telefone de contato muda só o contato **da consulta**; não muda a identidade do paciente (número do
  WhatsApp / e-mail de login) — isso trocaria a conta.
- Botões fora das 24 h exigem modelo da Meta aprovado; enquanto não houver, o modelo pago de uma
  variável (`REMINDER_TEMPLATE_NAME`) leva o texto sem botões (padrão já usado no R2/R4).
- Nada é deployado, publicado ou submetido à Meta pelos planos.

## 5. Decisões do dono de 2026-10-09 (segunda rodada): quem vê o quê, lembrete extra, dados do Editar

Estas decisões entram no **R7** (Tasks 13–16) e num plano novo do **brain-api**
(`brain-api/docs/superpowers/plans/2026-10-09-hub-token-papel-profissional.md`); o **R5** consome o
contrato. Ordem de execução: brain-api → R7 → R5. Ordem de deploy: migração da secretarIA → brain-api →
secretarIA (API e worker juntos) → front. Nada é deployado pelos planos.

### A. Quem vê a agenda (regra aplicada no servidor, nunca só na tela)

| Pessoa | Vê |
|---|---|
| Dono/gestor da clínica, recepção (secretária) | as consultas de todos os médicos |
| Gestor que também é médico | escolhe "Todos" ou "Só os meus" |
| Qualquer outro médico | **só as próprias consultas** |

- Para o médico restrito, a consulta de outro médico **não existe**: a lista não a traz, e qualquer leitura
  ou ação pelo id dela responde o mesmo "não encontrada" de um id que não existe (não revela que existe).
- Eventos do Google sem consulta registrada e consultas sem médico definido também não aparecem para o
  médico restrito (o título de um evento do Google pode trazer o nome de outro paciente).
- O comportamento com o Google Calendar não muda (a agenda continua lendo a mesma agenda de hoje).
- Quem decide o papel é o **brain-api** (autoridade de identidade), a cada uso, pelo cadastro atual da
  pessoa — não por algo gravado no token. Ele passa a dizer "vê a clínica" ou "vê só o próprio".
- Enquanto o brain-api novo não estiver no ar: quem está ligado a um médico vê só as próprias consultas
  (fecha por segurança); quem não está ligado a médico continua vendo como hoje.
- Médico restrito **cria consulta e bloqueio, mas só na própria agenda** (decisão do dono, 2026-10-09): a tela
  já vem com ele escolhido e travado; se a tela não disser o médico, o servidor completa com ele; se pedir outro
  médico, o servidor recusa. Ele não passa a consulta dele para outro médico no Editar/Remarcar (pede à
  recepção). Quem vê a clínica toda pode marcar para qualquer médico ativo da clínica, ou sem médico, como hoje.

### B. Lembrete extra configurável pela clínica

- A configuração da clínica passa a mostrar se os lembretes estão ligados (só leitura — quem liga é o dono
  da plataforma) e o **lembrete extra**: quantos minutos antes da consulta, de 1500 (mais que o lembrete de
  1 dia) a 20160 (14 dias); vazio = sem lembrete extra.
- Mudar o valor **reprograma** os lembretes extras ainda não enviados das consultas futuras: os que já
  saíram ficam como histórico; os que ficariam no passado são cancelados (nunca enviados atrasados); as
  consultas ainda sem nenhum lembrete planejado ficam com a rotina que já as planeja.

### C. A agenda entrega o que o "Editar/Remarcar" precisa

Cada consulta da agenda passa a trazer: médico (id e nome), serviço, convênio, nome de quem vai ser
atendido, telefone de contato da consulta e o **canal do paciente** (WhatsApp ou Portal) — a tela para
de adivinhar. Respeita a regra A: o médico restrito nunca recebe dados de pacientes de outro médico.

### D. Console de conversas — todos veem tudo; "Meus pacientes" é só um filtro (decisão do dono, 2026-10-09, substitui a anterior)

Plano: **R8** (`plans/2026-10-09-lembretes-r8-filtro-meus-pacientes-e-seletor-medico.md`). Esta decisão
**substitui** a tabela "quem vê o quê no console" que estava antes nesta seção — ela não vale mais. O R7 e o
R5 continuam sem mexer no console.

- **Todo mundo da clínica vê todas as conversas** (dono/gestor, recepção e qualquer médico, inclusive o
  médico que na agenda só vê as próprias consultas). Não há filtro por papel no console.
- A única novidade é um botão de filtro **"Meus pacientes"**, que aparece só para quem é médico na clínica
  (a pessoa está ligada a um profissional da clínica). Ligado, ele mostra só as conversas dos pacientes que
  **já tiveram ou têm consulta com esse médico**. Desligado, a lista é a de sempre.
- É um **filtro, não uma permissão**: não esconde nada de ninguém. Quem desliga o botão (ou nunca o liga)
  vê a clínica inteira; quem não é médico nem vê o botão.
- O que conta como "já teve ou tem consulta" (decidido no plano R8, onde o dono não falou): consulta com
  esse médico marcada, confirmada, remarcada, em que o paciente compareceu ou em que faltou. **Consulta
  cancelada não conta** — ela não aconteceu nem vai acontecer. Bloqueio de horário não é paciente. Quem só
  conversou e nunca marcou nada fica fora do filtro (continua em "Todas").
- Conversas do PreCheck (questionário) não têm consulta ligada: com "Meus pacientes" ligado elas saem da
  lista; desligado, aparecem como hoje.

### E. Agenda — quem vê a clínica toda escolhe o médico ao criar (decisão do dono, 2026-10-09)

- Em **"Nova consulta"** e **"Bloquear horário"**, quem vê a clínica toda (gestor, recepção, gestor que também
  é médico) escolhe o médico numa lista dos médicos **ativos** da clínica. Também existe a opção
  **"Sem profissional definido"**, porque o servidor já aceita marcar sem médico (como hoje).
- Com mais de um médico na clínica, a tela **não escolhe sozinha**: a pessoa escolhe (um médico ou "sem
  profissional") antes de agendar — assim uma consulta não fica sem médico por descuido (consulta sem médico
  não aparece para o médico restrito). O gestor que também é médico já vem com ele mesmo escolhido; clínica
  com um só médico já vem com ele.
- O médico restrito continua travado nele mesmo (já construído no R5); para ele a lista nem aparece.
- O servidor continua sendo a autoridade (R7): médico de outra clínica, inativo ou inexistente é recusado;
  médico restrito pedindo outro médico é recusado.
