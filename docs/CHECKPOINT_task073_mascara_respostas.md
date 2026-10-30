# CHECKPOINT — TASK-073: respostas sigilosas mascaradas + e-mail no contexto da paciente

Estado: **commitado na branch `task/TASK-073-mascara` (local), NÃO mesclado em `main`, NÃO
pushado, NÃO deployado.** Pedido do dono, 2026-10-30.

## O problema

Duas respostas do paciente são segredos, não conversa: o **código de 6 dígitos** do login (Portal) e o
**CPF / e-mail** que o provedor de pagamento pede para gerar o Pix. O worker nunca as grava como
digitadas; gravava um marcador de sistema (`[código oculto]` / `[dado oculto]`). Esse marcador
aparecia na conversa — para a clínica e para o paciente, que via uma bolha de sistema no lugar da
própria resposta. O Plano 1 da TASK-071 escondeu só o `[dado oculto]` do paciente.

## O que mudou

- `services/answer_masks.py` (novo): `CODE_MASK` = `••••••` e `PAYER_DATA_MASK` = `•••••••••••`.
  Bolinhas, nunca `*` (o texto formatado do canal lê `*...*` como negrito). Tamanho fixo, não copia o
  formato digitado (um CPF errado não mostra o erro).
- `workers/turn_router.py` grava `CODE_MASK` no lugar do código; `payer_data.REDACTED_BODY` passou a ser
  `PAYER_DATA_MASK`. O valor real continua só na memória do turno (e, para o CPF, no Redis cifrado por
  15 min, como antes).
- Saiu o filtro do Plano 1 (`_is_redacted_answer` em `api/internal.py` e
  `payer_data.is_redacted_placeholder`): **nenhuma linha é escondida de ninguém**. Clínica, paciente e
  histórico da IA leem a mesma linha mascarada, na ordem certa da conversa.
- O que a IA lê: a máscara. O código e o CPF reais nunca entram no histórico, então não dependem da
  pseudonimização (que casa CPF/e-mail/telefone por formato e **não tem regra para o código**).
- `Patient.email` passa a ser gravado quando o endereço é aceito (`workers/orchestrator.py::
  _remember_claimed_email`, nos dois ramos CLAIMED: primeiro cadastro e "Mudar e-mail"), e
  `ConversationRead.patient_email` (aditivo, `GET /tenants/me/conversations`) o entrega ao console.
  Antes só era gravado na primeira marcação (`booking_notifications._remember_email`, que segue
  mantendo a cópia atual). O brain-api continua sendo a autoridade de identidade.

## Brain-Message-Frontend (mesma tarefa)

- `lib/answer-masks.ts` repete as duas máscaras; `lib/patient-portal.ts::unconfirmedLocal` junta a
  bolha local (o que o paciente digitou) com a linha mascarada que volta do servidor, para o paciente
  ficar com UMA bolha. Toque em botão, arquivo e PreCheck nunca se juntam à máscara.
- `lib/real/mappers.ts` lê `patient_email` e o põe em `patient.email`; o `ContextPanel` já desenhava
  esse campo (linha do e-mail no contexto da paciente, ao clicar no nome na conversa).

## Ordem de deploy

Sem migração. secretarIA **API + worker juntos** (toca `workers/`); o frontend depois (o campo novo
é opcional, então o frontend velho continua funcionando com a API nova).

## Verificação

- secretarIA: ruff limpo nos arquivos tocados; testes afetados 236 + 10 passando (inclui o novo
  `test_the_claimed_email_is_saved_on_the_patient_for_the_console` e
  `test_list_carries_the_patients_email_for_the_console_context`).
- Frontend: `tsc` limpo, 3211 testes, `npm run build` verde.
- NÃO provado: Portal real no navegador (código digitado → uma bolha `••••••` na ordem certa, também
  após F5) e o contexto da paciente com o e-mail numa clínica real. Fazer na clínica de teste
  (Chrysostomo For Eyes) depois do deploy.

## Limites conhecidos

- Linhas antigas já gravadas com `[código oculto]` / `[dado oculto]` continuam como estão.
- Entre o envio e o retorno do servidor o paciente vê, na bolha local, o que digitou (uns segundos);
  depois ela vira a máscara. É o próprio paciente vendo a própria resposta.
- A bolha local do CPF só se junta à máscara se o servidor devolver a linha em até 5 min do envio.
