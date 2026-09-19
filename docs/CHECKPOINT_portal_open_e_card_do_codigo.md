# CHECKPOINT — Portal: a automação fala primeiro, o aviso do código vira card, e o PreCheck sai pelo Portal

Data: 2026-09-19
Origem: `tasks/TASK-003/TASK.md` (BRAIN), contratos §2, §3, §4 e §5.2 — perna da **secretarIA** de
uma tarefa de 3 repos (Brain-Message-Frontend, brain-api, secretarIA).
Estado: **BUILT, commitado só na branch `task/TASK-003-secretaria`. NÃO deployado, NÃO pushado.**

Continuação direta de `docs/CHECKPOINT_secretaria_email_otp_inline.md` (onda 2, já em produção).
Nada daquela ordem foi reescrito — as três peças abaixo só acrescentam gatilhos e saídas.

## 1. `POST /internal/brain-message/open` — a conversa nasce sem inbound

**O que existia.** `Conversation` tinha um único construtor em `src/`
(`workers/tasks.py::_get_or_create_conversation`) e um único caminho até ele,
`_route_inbound_turn`, alcançado apenas por `POST /internal/brain-message/inbound`. Consequência:
paciente novo que abre o link do Portal caía num chat vazio até digitar alguma coisa.

**O que entrou.** Uma segunda porta, e **só** a porta — a saudação continua sendo a mesma:

| Camada | Âncora | Papel |
|---|---|---|
| HTTP | `api/internal.py::brain_message_open` + `_brain_message_conversation_started` | 202 `queued` / 200 `exists`, ack-fast, enfileira e responde |
| Schema | `schemas/internal.py::BrainMessageOpen` | `tenant_id`, `external_id` (1..64), `patient_name`; `extra="forbid"` |
| Job | `workers/tasks.py::process_brain_message_open` | reserva no ledger, abre, envia, devolve a chave se nada saiu |
| Núcleo | `workers/tasks.py::_open_brain_message_conversation` | paciente + `ConsentEvent` + conversa, **sem nenhuma `Message` inbound** |
| Compartilhado | `workers/tasks.py::_first_contact_reply` | extraído de `_route_inbound_turn`; **uma** saudação para as duas portas |
| Registro | `workers/arq_worker.py` | `process_brain_message_open` na lista de `functions` |

**Idempotência em duas camadas, porque uma não basta.** A rota lê se a conversa já carrega
qualquer mensagem (barato, e é a resposta certa para quem volta); o job reserva
`brain_message_open:<tenant_id>:<external_id>` no `ProcessedEvent`. As duas chamadas concorrentes
de um F5 passam pela leitura; só uma insere a chave. A chave é **devolvida** quando nada chegou ao
paciente (`_conversation_has_outbound` como pós-condição), senão uma saudação perdida num erro de
envio transformaria o chat vazio em permanente.

**A regra que não pode ser afrouxada:** nenhuma `Message` com `direction="inbound"` é criada aqui.
O paciente não escreveu nada, e uma bolha fabricada mentiria ao mesmo tempo para o console do
paciente, para o console da clínica e para o histórico que a LLM relê.

Decisões tomadas sem perguntar:
- `extra="forbid"` no corpo novo (mesmo argumento de `BrainMessageInboundForm`: rota nova, nenhum
  consumidor antigo, e deriva de contrato vira 422 legível em vez de campo engolido). **Custo:** um
  campo extra do brain-api derruba o gatilho em silêncio — está listado em "Ordem de deploy".
- `Tenant.is_active` continua **não** consultado neste canal, exatamente como em
  `_persist_brain_message_inbound` (é o gate de go-live do WhatsApp; ver `b2f3055`).
- Se a conversa tem 0 mensagens mas o paciente já aceitou LGPD — estado que nenhum caminho produz
  hoje — o turno é tratado como primeiro contato mesmo assim. O aviso de consentimento é
  idempotente e, neste canal, quem decide o que sai é o probe do brain-api.

## 2. O aviso do código vira card de 3 botões, com o e-mail mascarado

`CODE_NOTICE_MESSAGE` era texto puro e não dizia para qual endereço o código tinha ido.

- `services/pending_identity.py::request_code` passa a devolver `RequestCodeResult(outcome,
  email_masked)` — campo **opcional** da resposta 200 (contrato §3). Ausente ou malformado não
  rebaixa o desfecho: o código FOI enviado; só a metade cosmética faltou.
- `masked_email_or_none` valida a máscara **na entrada** (`^[^@\s]*\*\*\*[^@\s]*@[^@\s]+$`, ≤ 254).
  Isso é propriedade de segurança, não arrumação: `email_masked` é o único campo deste contrato que
  vai verbatim para o transcript, e um brain-api que um dia mande o endereço cru não pode gravá-lo
  em `messages` através daqui. Recusa → `code_notice_body` cai na redação que não nomeia inbox
  nenhuma (nunca num palpite).
- `CODE_NOTICE_BUTTONS` (ordem do dono): `identity_back` "⬅️ Voltar" · `identity_resend`
  "↩️ Reenviar código" · `identity_change_email` "📩 Mudar e-mail". Os três títulos cabem no teto de
  20 unidades de código que `interactive_buttons_record` aplica — há teste fixando isso, porque o
  seletor de variação de ⬅️/↩️ e o 📩 astral custam uma unidade a mais do que parecem.
- Emissão em três lugares, um único formato: `plugins/pending_identity.py` (pós-agendamento, via
  `BrainMessageSender.send_buttons`), e `workers/tasks.py::_send_code_notice` (retomada e reenvio),
  que usa o `_send_buttons_reply` novo — gêmeo de `_send_plain_reply`. `_send_consent_notice`
  **não** foi refatorado de propósito: os nomes dos três eventos de log dele são carregados em
  produção e não cabem no esquema `f"{event}_..."`.

Toque nos botões: `_route_inbound_turn`, ramo `AWAITING_EMAIL_CODE`, **antes** do `parse_code` —
roteia por `identity_action_or_none(interactive_reply_id)`, nunca pelo rótulo. O id já chega
revalidado contra os cards que esta conversa ofereceu (`_validated_brain_message_reply_id`), então
um id forjado já é `None` aqui.

| Botão | Estado escrito onde | Efeito |
|---|---|---|
| `identity_resend` | fica em `AWAITING_EMAIL_CODE`; reescrito em `_handle_identity_card_action` só quando o brain-api confirma | novo desafio + o mesmo card, com a mesma máscara; falha → `IDLE` + `CODE_GIVE_UP_MESSAGE` |
| `identity_change_email` | `AWAITING_EMAIL`, dentro da transação do inbound | `EMAIL_REQUEST_MESSAGE`; o próximo endereço válido faz um claim novo (o brain-api sobrescreve o claim da mesma visita) |
| `identity_back` | `IDLE` na transação; `_handle_show_main_menu` depois grava `MENU` | menu; a oferta de reativação existente continua valendo, e a consulta não é tocada |

Não mudou: digitar os 6 dígitos continua verificando, e a redação para `[código oculto]` em
`Message.body` continua valendo. Nenhum e-mail cru entra no transcript — só a máscara.

## 3. Handoff do PreCheck a partir do Portal

Antes, `plugins/precheck_handoff.py` saía em `no_patient_phone` para todo paciente
`brain_message` (`Patient.wa_id` é NULL ali por desenho) e entregava um link `wa.me` por
`WhatsAppClient`.

- `services/precheck.py::request_precheck_handoff` aceita `phone_number` **XOR** `external_id`
  (keyword-only; `phone_number` continua posicional, então nenhum call site antigo se mexe).
  Nenhum ou os dois → WARNING + `UNAVAILABLE` **antes** de gastar a requisição, porque o contrato
  desta função é nunca levantar no chamador.
- O corpo de uma chamada WhatsApp é **byte-idêntico** ao de ontem — nem uma chave nula a mais —, o
  que é o que torna este lado seguro de subir sozinho. Há teste fixando isso.
- **422 ganhou ramo próprio:** `precheck_handoff_body_rejected` com `subject_field`, ainda mapeando
  para `UNAVAILABLE`. Para o paciente é a mesma silêncio; para quem lê log, é a diferença entre
  "ordem de deploy errada" e "rede caiu".
- `plugins/precheck_handoff.py` resolve o canal **primeiro** e ele decide tudo depois: qual handle
  nomeia o paciente, quais pré-condições valem (o número da plataforma só interessa ao ramo que
  monta o `wa.me`) e quem entrega. Portal → `BrainMessageSender` na conversa do paciente, com
  `_PORTAL_MESSAGE` (sem `wa.me`, sem "outro número"). Ledger `precheck:<appointment_id>` inalterado
  e cobrindo os dois canais: um envio por agendamento.
- Log de sucesso ganhou `channel`, o único fato que um leitor não recuperaria sozinho.

### O invariante que MUDOU (e está registrado nos dois módulos)

`plugins/pending_identity.py` documentava que os dois hooks `post_booking` eram **disjuntos por
canal** — `precheck_handoff` exigia `wa_id`, este exige `brain_message`. Isso deixou de ser
verdade: num agendamento do Portal **os dois** falam agora, um pedindo o código e o outro
oferecendo a pré-consulta. É intencional, são ofertas diferentes com ledgers diferentes, e
`run_post_booking` não tem short-circuit — nenhum suprime o outro. A ordem de registro
(alfabética) põe o card do código primeiro, que é a ordem desejada. O aviso da pré-consulta **não**
mexe em `flow_state`, então os próximos 6 dígitos continuam sendo lidos como código.

A disjunção que continua valendo é a das ENTREGAS do handoff: WhatsApp pelo caminho antigo, Portal
pelo novo, nunca os dois no mesmo agendamento — garantido estruturalmente porque um paciente tem um
canal só.

## Ordem de deploy (nada disto foi executado)

```
1. brain-api  — PrecheckHandoffIn aceitando external_id  (SENÃO o handoff do Portal 422)
2. brain-api  — email_masked em pending-otp/request      (opcional: sem ele, o card sai sem máscara)
3. brain-api  — o gatilho que chama /internal/brain-message/open
4. secretaria_api E secretaria-worker, JUNTOS
```

Por que os dois serviços: a rota nova vive na **API**, o job novo (`process_brain_message_open`),
o card e o ramo do handoff vivem no **worker**. Subir só a API faz a rota responder 202 e o job
nunca rodar ("unknown job"); subir só o worker deixa a rota inexistente. Não há migração nesta
rodada — `FlowState` não ganhou membro novo e nenhuma coluna mudou.

Subir a secretarIA **antes** do brain-api é seguro para o WhatsApp (corpo idêntico) e inerte para o
Portal: o handoff 422 vira "indisponível" com log claro, e o gatilho do `open` simplesmente não é
chamado por ninguém.

## Validação

```
uv run pytest -q     ->  2293 passed, 2 failed
uv run ruff check .  ->  8 errors
uv run ruff format --check .  ->  61 files would be reformatted
```

As 2 falhas e os 8 erros de lint são **pré-existentes no HEAD `9cc9b7a`**, medidos antes de
qualquer edição (2230 passed / 2 failed; os mesmos 8 erros; as mesmas 61 files). As duas falhas são
`tests/test_action_buttons.py` pedindo credenciais reais do Google Calendar, que esta worktree não
tem (`.env` ausente por desenho). `ruff format .` **não** foi rodado (memória
`secretaria-make-lint-red-at-head`); os dois arquivos que minhas edições sujaram foram formatados
individualmente, e o diff de formatação de `workers/tasks.py` foi conferido hunk a hunk como
idêntico ao do HEAD.

## O que NÃO foi provado

- Nada rodou contra o brain-api real: os três contratos novos (`open`, `email_masked`,
  `external_id`) são exercitados só contra `httpx.MockTransport`.
- O card nunca foi visto num navegador nesta rodada. O frontend já renderiza e toca
  `interactive` (`docs/CHECKPOINT_portal_interactive_tap.md`), mas estes três ids são novos.
- A fixture roda em SQLite, que não impõe largura de `VARCHAR` — a chave do ledger foi calculada
  (≤ 120 de 128) em vez de provada por um INSERT recusado.
- Nenhum E2E do fluxo completo link → saudação → e-mail → consulta → card → botão.
