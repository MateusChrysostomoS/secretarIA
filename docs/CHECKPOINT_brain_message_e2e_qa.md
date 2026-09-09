# CHECKPOINT — QA ao vivo do canal Brain-Message em produção (2026-09-09)

**Estado:** dois bugs deste repo CORRIGIDOS, commitados e pushados. **Não deployados** — e o
deploy é dos DOIS serviços (ver §4).
**Origem:** `z_prompts/PROMPT_BRAIN_MESSAGE_E2E_QA_PRODUCAO.md` — teste de ponta a ponta pelo
navegador, contra produção, do canal Brain-Message (portal do paciente + console de staff).
**Tenant de teste:** "Chrysostomo For Eyes" (`9c4fa6a5-ffdb-4adb-9fe1-9036270f1246`).

Este checkpoint cobre o que MUDOU neste repo. O que foi só verificado (sem mudança) está em §5.

---

## 1. `patient_access_otp` ausente em `_TEMPLATES` — commit `f07ec05`

**Sintoma:** o paciente pedia o código no `/conversa`, a tela avançava normalmente para "Digite
o código", e o e-mail nunca chegava. Sem erro em lugar nenhum.

**Causa:** `brain-api` (`api/patient_access.py::_OTP_EMAIL_TEMPLATE`) manda o nome
`patient_access_otp` para `POST /internal/notifications/email`. `services/email.py::_TEMPLATES`
não tinha essa chave. O caminho inteiro é surdo a isso: `api/internal_provisioning.py::
notifications_email` não valida o nome, só enfileira no arq e responde 200; quem resolve o
template é o **worker**, que faz `_TEMPLATES.get(...)`, acha `None`, loga
`transactional_email_unknown_template` em WARNING e para. O chamador já foi embora com o 200 na
mão.

**Correção, em duas partes** — registrar o template sozinho deixaria o próximo serviço irmão
redescobrir o mesmo silêncio:

1. `patient_access_otp` registrado (`{code}`, `{ttl_minutes}`). Deliberadamente **não** nomeia a
   clínica: `request-otp` responde igual exista ou não a clínica, então nomeá-la no corpo do
   e-mail vazaria na caixa de entrada exatamente o que o endpoint se recusa a vazar na resposta.
2. `notifications_email` agora resolve o template no ENFILEIRAMENTO (`email.is_known_template`) e
   responde **422** para um id que este serviço não sabe renderizar. Um template desconhecido é
   defeito do chamador que nenhum retry conserta; responder 200 promete um e-mail que ninguém
   manda.

Testes: `tests/test_email_transactional.py`, `tests/test_internal_provisioning.py` (63 passed).

## 2. Staff não conseguia responder no canal Brain-Message — commit `5b8bfdf`

**Reproduzido ao vivo** antes de qualquer correção: console → conversa de um paciente
`brain_message` → enviar → `POST /tenants/me/conversations/{id}/messages` respondeu **502** e a
tela mostrou "A mensagem não foi enviada. Tente novamente."

**Causa:** `_send_via_whatsapp` montava um `WhatsAppClient` incondicionalmente e chamava
`send_text_message(to=patient.wa_id)`. Paciente `brain_message` tem `wa_id=None` por desenho
(migração `c7e1a4b9d0f3`) → `"to": null` → Graph API 400 → `httpx.HTTPStatusError` → o próprio
`except httpx.HTTPError` deste router vira 502 "Failed to deliver message via WhatsApp". Mensagem
exata sobre o canal errado.

> Nota histórica: a §8.1 de `CHECKPOINT_console_staff_messages.md` já tinha PREVISTO este ponto e
> decidido, conscientemente, não corrigir enquanto nenhuma linha `channel='brain_message'`
> existisse. A decisão estava certa na época; o que mudou é que agora existem linhas assim em
> produção. Uma sessão anterior previu 500 lendo o código — o status real é 502, porque o handler
> já captura `httpx.HTTPError`.

**Correção:** `_send_via_whatsapp` virou `_deliver_to_patient`, despachando por `Patient.channel`
como `workers/tasks.py::_reply_sender` já faz no caminho automático. Em Brain-Message não há
perna de rede: o console do paciente faz poll de
`GET /internal/brain-message/conversations/{external_id}/messages`, que devolve TODA linha da
conversa **sem filtrar por sender** — então a linha que este endpoint já grava **é** a entrega.
Retorna `{}`, que `_extract_wam_id` percorre até `None`, correto para uma mensagem que a Meta
nunca carregou.

**Divergência deliberada do prompt gerado** (`..._CONSOLE_SEND_CHANNEL_DISPATCH.md`), que propunha
passar por `channel_sender.BrainMessageSender`: aquela classe fixa `sender=BOT` e carimba
`conversation.last_bot_message_at`. Usá-la aqui faria a resposta HUMANA do staff aparecer na
transcrição do paciente como se fosse a secretarIA, e ligaria o relógio do bot num turno humano.
Além disso ela declara `persists_outbound`, e este endpoint precisa gravar e **retornar** a
própria linha — sairiam duas. Seria trocar um 502 visível por uma troca de autoria silenciosa.

Teste de regressão: `test_send_message_to_brain_message_patient_persists_without_touching_whatsapp`
— afirma que a resposta persiste como HUMAN com `wam_id=None`, que o handover ainda vira, e
sobretudo que **nenhum** `WhatsAppClient` chega a ser construído. Verificado que ele FALHA contra
o código antigo (não é teste vazio). Suíte completa: **2045 passed, 0 failures**.

## 3. O que ainda NÃO foi provado ponta a ponta

O login do paciente depende do e-mail do §1, e o código só existe como hash
(`services/patient_access.py::issue_otp` devolve o código cru só para o chamador enviar) — não há
como lê-lo sem a caixa de entrada. Logo, **tudo abaixo está bloqueado no deploy do §4**:

- paciente entra no `/conversa` com o código recebido;
- paciente manda mensagem e o bot responde de verdade pelo pipeline assíncrono;
- staff assume a conversa **enviando** (o caminho corrigido no §2 exercido ao vivo);
- questionário do PreCheck pela aba do portal;
- logout / F5 exigindo código novo.

## 4. Deploy — os DOIS serviços

`_TEMPLATES` é lido pelo **worker**; `notifications_email` e `api/hub/conversations.py` são lidos
pela **API**. Pela regra de `CLAUDE.md` (dois serviços EasyPanel, sem auto-deploy):

- **`secretaria-worker`** → sem ele o código do OTP continua sumindo em silêncio, idêntico a antes.
- **`secretaria_api`** → 422 de template desconhecido + despacho por canal na resposta do staff.

Como conferir sem abrir o EasyPanel: `GET /build`. No momento desta sessão a produção respondia
`source_fingerprint: 419ebec97e53`, `alembic_head: aeeeb64360f5`, `deploy_parity: "match"` — o
deploy terá acontecido quando o `source_fingerprint` mudar, e `deploy_parity` precisa continuar
`match` depois (nunca `unknown`).

## 5. Verificado ao vivo, sem mudança necessária

- **Migração `0018_patient_access` em produção** (o 500 do `request-otp` de ontem): `POST
  /api/brain/patient-access/request-otp` responde **200** com o corpo neutro. Segue corrigido.
- **Rate limit do OTP:** os dois baldes documentados em `api/patient_access.py` funcionam como
  escrito — 3 pedidos/min por endereço e 5/min por IP; a 4ª chamada no mesmo e-mail deu 429, e um
  e-mail novo no mesmo minuto também deu 429 pelo balde de IP. Não é bug: são limites distintos e
  deliberados.
- **Entrada malformada:** `tenant_id` não-UUID e e-mail inválido dão 422 do Pydantic, sem tocar o
  banco (os limitadores rodam antes).
- **Handover pelos dois sentidos** (`POST .../handover`): 200 em BOT→HUMAN e em HUMAN→BOT, com
  barra de confirmação ("Assumir esta conversa?" / "Devolver") antes de cada troca.
- **Não-regressão do WhatsApp:** as conversas de WhatsApp já existentes continuam listadas no
  console junto com a de Brain-Message, na mesma lista.

## 6. Arquivos tocados

- `src/secretaria/services/email.py`, `src/secretaria/api/internal_provisioning.py`
- `src/secretaria/api/hub/conversations.py`
- `tests/test_email_transactional.py`, `tests/test_internal_provisioning.py`,
  `tests/test_hub_conversations.py`
- `docs/CHECKPOINT_brain_message_e2e_qa.md` (este arquivo)

Bugs achados no `Brain-Message-Frontend` na mesma rodada (fora deste repo): botão "Receber código"
inerte com código de clínica inválido, e cabeçalho da thread dizendo "IA conduzindo" ao lado de um
switch dizendo "Recepção conduz" depois do takeover. Ver
`Brain-Message-Frontend/docs/CHECKPOINT_portal_paciente.md` e `..._console_real.md`.
