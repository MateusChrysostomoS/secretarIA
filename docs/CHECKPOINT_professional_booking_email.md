# CHECKPOINT — E-mail ao profissional quando uma consulta é marcada

> Estado: **COMMITADO no `secretarIA` (`1029ce5`, "FEAT 33 & 34"), NÃO deployado**
> (status corrigido em 2026-08-20 — este cabeçalho ainda dizia "NÃO commitado", de
> 2026-08-17, e o `git log` do `plugins/professional_notification.py` desmente).
> O `calendar_line` adicionado em 2026-08-20 (ver "Os dois links" abaixo) é a única
> parte **ainda não commitada**. Estado dos outros três repos não reverificado.
> Escopo: `secretarIA` (backend), `brain-api` (endpoint interno), `brain-frontend` e
> `secretarIA-frontend` (indicação de profissional sem e-mail).
> **Sem migração** — ver "Idempotência" abaixo.

`PROMPT_FEAT_33`. Até esta rodada, um médico só descobria uma consulta nova abrindo a
agenda (ou pelo Google Calendar, quando a clínica tinha conectado uma). Nada empurrava.

Suítes: **secretarIA 1596 passed**, **brain-api 461 passed**, **brain-frontend 221**,
**secretarIA-frontend 212** — mais `tsc --noEmit` limpo nos dois frontends. `ruff` segue
com as falhas pré-existentes de sempre (8 no secretarIA, 13 no brain-api), nenhuma nova.

> Uma falha conhecida aparece se a suíte do secretarIA rodar entre 00:00 e 03:00 UTC:
> `test_human_backup_plugin.py::test_on_inbound_inside_hours_returns_false`. O fixture
> monta `business_hours` na chave do dia em **UTC** e o plugin resolve o dia em
> **America/Sao_Paulo** — nessa faixa os dois discordam. Pré-existente e sem relação com
> esta feature (nenhum arquivo dela é tocado aqui).

## O bloqueio real, e por que não era o que parecia

A suspeita inicial era "o formulário coleta o e-mail e não salva". **Falso.** O convite
(`InviteTeamMemberModal` → `POST /doctor/professionals/invites`) salva o e-mail de forma
durável em `users.email` do brain-api, ligado por `users.professional_id`. Nada se perdia.

O que faltava era mais estreito: `brain-api::secretaria_provisioning.create_professional`
manda `{"name", "specialty", "about"}` para o secretarIA e **não** manda o e-mail — e o
`Professional` do secretarIA não tem coluna para recebê-lo. Ou seja: o dado existia, só
não estava do lado que precisava dele na hora do booking.

## A decisão: perguntar, não copiar

**Não** criamos `Professional.email`. brain-api é o escritor único de identidade, e uma
segunda cópia não teria caminho de propagação: no dia em que o médico trocasse o e-mail
no brain-api, o secretarIA seguiria mandando para o endereço velho, em silêncio e para
sempre. Então o secretarIA **pergunta**, uma vez por agendamento.

- **brain-api** — `GET /internal/tenants/{tenant_id}/professional-emails`
  (`api/internal.py`), atrás do mesmo `X-Internal-Api-Key` de sempre. Em **lote** por
  tenant, de propósito: um endpoint por-id viraria N+1 no primeiro consumidor que
  precisasse de dois. Mesmo join que `GET /doctor/professionals` já faz para o
  `linked_user_email`. Contrato registrado em `CONTRACTS.md` §16.6.
- **secretarIA** — `services/brain_professionals.py`, irmão de
  `services/brain_onboarding.py` em tudo: cliente httpx curto, `BRAIN_API_BASE_URL` /
  `INTERNAL_API_KEY`, fail-soft em toda ambiguidade. `None` = "não deu para saber";
  `{}` = "o brain-api respondeu e ninguém está vinculado". Nunca loga endereço.

**Consequência aceita:** um `Professional` cadastrado **sem convite** não tem usuário
vinculado, logo não tem e-mail, e o hook é um no-op logado para ele. É honesto — e agora
**visível**: a tela de profissionais dos dois frontends passou a dizer, na própria linha,
`Sem e-mail vinculado — não recebe aviso de nova consulta`, no lugar do nada que
renderizava antes.

## O hook

`plugins/professional_notification.py`, no formato de `plugins/ehr.py`.

**CORE, não add-on** (`entitlement_keys=()`): avisar o médico é higiene de produto, não
item vendável.

> **Armadilha corrigida no caminho.** `entitlement_keys=()` **nunca** casava com o
> `any()` do registry — `any(())` é `False`, então a tupla vazia significava
> "permanentemente desligado", o oposto exato do que quem escreve `()` quer dizer.
> `plugins/reminders.py` também declara `()`, mas registra zero hooks e é disparado pelo
> próprio cron, então nunca bateu nisso — não era precedente utilizável, era coincidência.
> `registry._spec_enabled` agora lê tupla vazia como "core, ligado enquanto a assinatura
> estiver ativa". `summary.active` continua respeitado: clínica com assinatura vencida não
> segue mandando e-mail em nome da plataforma.

**Conteúdo** (template `appointment_booked_professional`, inserido no FIM de `_TEMPLATES`
para não mexer na posição do `password_reset`): paciente, serviço, data/hora **no
timezone do tenant**, convênio quando houver, e **dois** links de agenda.

### Os dois links — por que são diferentes (2026-08-20)

`agenda_line` e `calendar_line` respondem perguntas diferentes, então um não substitui o
outro. Ambos são opcionais e chegam **pré-renderizados** (string vazia quando não há o que
dizer), porque `EmailTemplate` é um par plano de `str.format_map`, sem condicional.

| linha | de onde vem | o que abre |
|---|---|---|
| `agenda_line` | `DOCTOR_AGENDA_URL` (env) | a tela de agenda **do produto** — do frontend que aquela instalação serve |
| `calendar_line` | `Appointment.google_event_link` | **o evento em si** no Google Calendar da clínica |

`calendar_line` é o `htmlLink` **privado** que o Google devolve no `create_event`. Aqui ele
é exatamente o link certo — o profissional **é dono** daquela agenda, então ele abre. Para
o **paciente** o mesmo link dá erro de permissão, e é por isso que o paciente recebe outra
coisa: a URL pública de template do Google
(`services/calendar.py::build_patient_calendar_link`, ver
`docs/CHECKPOINT_patient_calendar_link.md`). Os dois existem de propósito e **não** são
intercambiáveis.

`google_event_link` é NULL para uma consulta marcada sem calendário conectado e para linhas
anteriores à coluna — a linha simplesmente some, sem exceção e sem placeholder cru.

> Isto era a única pendência funcional registrada nesta feature e foi **feita** em
> 2026-08-20 (`plugins/professional_notification.py::_deliver` +
> `services/email.py::_TEMPLATES`, mais 3 testes em
> `tests/test_professional_notification.py`). Sem migração: a coluna já existia.

**Nunca**: telefone do paciente, preço, nada clínico. SMTP trafega em claro para uma
caixa que o produto não controla — o médico precisa reconhecer a consulta e abrir a
agenda, não guardar o contato do paciente. Dois testes falham se um telefone aparecer no
corpo **renderizado pelo template real** ou em qualquer linha de log.

### O link da agenda — correção de premissa

O prompt pedia `{FRONTEND_BASE_URL}/secretaria/agenda`. **Isso quebraria metade das
clínicas**: o `brain-frontend` serve a agenda em `/secretaria/agenda`, o
`secretarIA-frontend` em `/agenda`. Compor o caminho aqui produziria um 404 para quem
estiver no segundo.

Então a configuração nova é a **URL completa**: `DOCTOR_AGENDA_URL` (default vazio).
Vazio omite a linha do link — e-mail sem link é aceitável, e-mail com link quebrado não é.

## Idempotência — e por que não houve migração

`run_post_booking_hooks` é job arq e arq reexecuta jobs; um segundo "nova consulta
marcada" é ruído direto na caixa do médico. O prompt previa migração; **não foi
necessária**: `processed_events` já é o ledger durável de claim que o pipeline de webhook
e o `plugins/reminders.py` usam, com chave namespaced. Aqui a chave é
`profnotif:<appointment_id>`. Mesma solução, não uma paralela — que é exatamente o que o
`PROMPT_FIX_27` pede ("id determinístico e claim/ack idempotente").

**Uma diferença deliberada em relação ao `reminders`:** lá, "claimed" significa "feito,
nunca repetir" (lembrete perdido é melhor que duplicado). Aqui o claim é **só uma
tranca** — um envio que não aconteceu **devolve** o claim, senão uma queda transitória
viraria uma notificação perdida para sempre, e ligar o mailer depois encontraria todo
booking antigo já marcado como "enviado".

> **Corrigido depois, pelo `PROMPT_FIX_32` (2026-08-18):** devolver o claim **não era**
> um retry, e esta seção afirmava que era. Nada reexecuta um hook de `post_booking` —
> `registry.run_post_booking` contém as exceções de cada hook por contrato, então o hook
> não consegue nem pedir retry levantando, e o job externo termina com sucesso de
> qualquer jeito. Chave devolvida sem nada agendado atrás = e-mail perdido com uma linha
> de log. Hoje uma falha **transitória** enfileira o job próprio
> `retry_professional_notification` (que levanta `arq.Retry`, a única coisa que faz o arq
> reexecutar um job) e só **depois** devolve a chave. E `EMAIL_ENABLED=false` deixou de
> ser tratado como falha: `services/email.py::EmailOutcome` separa "desligado" de "SMTP
> caiu agora". Ver `docs/CHECKPOINT_fix32_retry_idempotencia.md`.

## Contenção

`registry.run_post_booking` já embrulha cada hook no próprio try/except. O teste força a
falha na **dependência real** do hook (o lookup no brain-api explode) e **reordena o
registry** para o hook que quebra rodar PRIMEIRO — sem isso a asserção passaria de graça,
já que `pix_deposit` é importado antes e rodaria de qualquer jeito.

## `FIX_02` — pré-requisito já satisfeito

O prompt marca dependência forte do `PROMPT_FIX_02` ("em clínica de um profissional só,
`Appointment.professional_id` fica NULL"). Isso **já foi resolvido** pela rodada de
booking ownership (`e1b6855`): `services/booking_scope.resolve_booking_owner_id` atribui o
único profissional ativo como dono quando não há seleção explícita. Sem isso o hook seria
um no-op justamente no caso mais comum; com isso, funciona. `professional_id is None`
segue sendo tratado (clínica com zero ou 2+ profissionais e nenhuma seleção).

## Pendências

- **Deploy** (o commit do slice do `secretarIA` já foi — `1029ce5`; falta commitar só o
  `calendar_line` de 2026-08-20). `plugins/` roda no **worker**: vale a regra do
  `README.md`/`CLAUDE.md` — todo push que toca `workers/` ou os plugins exige deploy do
  `secretaria-worker`, não só do `secretaria_api`. E o brain-api precisa subir **antes**,
  senão o lookup responde 404 e o hook vira no-op logado (fail-soft, sem quebrar nada).
- **EasyPanel** (nada de `.env` local): no serviço `secretaria_api` **e** no
  `secretaria-worker`, setar `DOCTOR_AGENDA_URL` com a URL completa da agenda do frontend
  que aquela instalação serve, e confirmar `EMAIL_ENABLED=true` + `SMTP_*` (hoje o default
  é `false`, então sem isso o hook é um no-op logado). `BRAIN_API_BASE_URL` e
  `INTERNAL_API_KEY` já existem — é o mesmo par de sempre.
- **Cadastro sem convite** continua sem e-mail por construção. Se isso incomodar na
  prática, o caminho é fazer o cadastro direto emitir convite, **não** duplicar a coluna.
