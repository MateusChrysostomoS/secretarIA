# E-mail fixo ao médico após confirmar Alterar Dados

## Estado

Pedido do dono em 2026-10-07: reenviar aviso ao médico quando o paciente confirmar a alteração,
com a frase “A consulta de {{NM_DO_PACIENTE}} mudou para: {{Mudanças}}” e os dados completos.
**Commitado e validado para integração**, sem LLM. O dono autorizou posteriormente os merges
e pushes. Entrega funcional no commit `6de2878`; main `8b4a5ee` incorporada sem conflitos
em `a132f3a`. Suíte da candidata: **4411 passed, 49 skipped, 17 warnings**, 227,84 s.
Publicação Git autorizada, deploy/SQL remoto/envio de e-mail real não executados. A entrega
anterior de R6 foi preservada; este complemento não muda migrações nem contratos HTTP.

Validação antes da integração: **4410 passed, 49 skipped, 17 warnings**, nenhuma falha, em 174,90 s.
Os 21 casos novos usam SQLite real e o template real, substituindo apenas e-mail, identidade
Brain e transporte Redis por doubles. Baseline escopado: 78 passed; integração intermediária:
126 passed. Ruff dos nove arquivos Python desta rodada e `git diff --check` limpos.

## O médico recebe

Assunto: **Consulta alterada — DD/MM/AAAA às HH:MM**.

O texto é um template fixo. A lista “mudou para” inclui somente os campos que realmente
mudaram, com seus valores atualizados. Em seguida vêm todos os dados abaixo, mesmo os que
permaneceram iguais:

```text
Olá, {{NM_DO_MEDICO}}!

A consulta de {{NM_DO_PACIENTE}} mudou para:
• Data: {{NOVA_DATA}}                [se mudou]
• Horário: {{NOVO_HORARIO}}          [se mudou]
• Serviço: {{NOVO_SERVICO}}          [se mudou]
• Médico: {{NOVO_MEDICO}}            [se mudou]
• Convênio: {{NOVO_CONVENIO}}        [se mudou]
• Paciente: {{NOVO_PACIENTE}}        [se mudou]

Dados atualizados da consulta:
Clínica: {{NM_DA_CLINICA}}
Médico: {{NM_DO_MEDICO}}
Paciente: {{NM_DO_PACIENTE}}
Agendado por: {{NM_DE_QUEM_AGENDOU}}
Serviço: {{SERVICO}}
Convênio: {{CONVENIO}}
Data: {{DATA}}
Horário: {{HORARIO}}
Término: {{HORARIO_FINAL}}
Unidade: {{UNIDADE}}                [quando cadastrada]
Endereço: {{ENDERECO_DA_UNIDADE}}    [quando cadastrado]

Ver na agenda: {{LINK_DA_AGENDA}}           [quando configurado]
Ver no Google Agenda: {{LINK_DO_EVENTO}}    [quando disponível]

— Equipe SecretarIA
```

As marcações entre colchetes explicam o modelo nesta documentação, não saem no e-mail.
`NM_DO_PACIENTE` é quem será atendido; para terceiros, “Agendado por” identifica a pessoa
que marcou. Convênio vazio aparece como “não informado”. Datas/horários usam o fuso da clínica.
Não vão telefone, e-mail do paciente, valores ou texto clínico: mantém-se a minimização
dos avisos profissionais existentes. O médico consulta contatos e outros detalhes na agenda.

## Onde e quando

- `services/email.py`: template `appointment_changed_professional`, literal em português.
- `services/appointment_edit.py::appointment_email_version`: fingerprint opaco dos dados
  da consulta; não muda ao replanejar lembretes/resetar confirmações.
- `workers/shared/appointment_edit_apply.py`: calcula mudanças reais comparando a consulta
  antes/depois; prepara identificação única da ocorrência, sem alterar a consulta original
  antes da confirmação.
- `workers/shared/flow_runner.py`: enfileira o aviso somente após a edição gravada. Cancelar
  o rascunho, navegar no menu, falha de gravação ou aplicação recusada não envia aviso.
- `workers/shared/appointment_edit_notification.py`: job `send_professional_edit_notification`,
  registrado em `workers/arq_worker.py`. Resolve o e-mail do médico atual pelo brain-api,
  confirma clínica/paciente/profissional/unidade com escopo de tenant e não invoca IA.

O job leva somente referências, fingerprint e nomes dos campos alterados; não leva nomes,
convênio ou destinatários na fila. O template é preenchido com o estado gravado no banco.
Uma troca de médico direciona ao novo médico; o aviso de nova marcação continua separado.
Não se executam novamente os plugins gerais de pós-agendamento/Pix/PreCheck.

## Proteções de envio

- Identificação e claim próprios por ocorrência em `ProcessedEvent`, separados de
  `profnotif:<appointment_id>`: repetir o mesmo job não duplica um envio confirmado;
  uma segunda alteração legítima pode gerar outro aviso.
- SMTP ou consulta de identidade temporariamente indisponíveis usam retry limitado a cinco
  tentativas, com intervalo de cinco minutos e validade de uma hora. E-mail desabilitado,
  profissional sem endereço/fora da clínica e outras recusas permanentes não repetem.
- Consulta cancelada/passada ou que já tenha outros dados não recebe aviso da versão antiga.
- Depois das consultas externas, a consulta e todos os dados exibidos são lidos novamente.
  Um `FOR UPDATE` da consulta durante o envio ordena alterações/cancelamentos concorrentes
  em PostgreSQL. Não se conserva o retrato antigo capturado antes da busca do endereço.
- Falha de fila/e-mail não desfaz nem rejeita a consulta já atualizada. Logs desta rodada
  contêm somente ids, motivos/tipos e contagens.

## Revisão e limites

Revisão independente em `C:\TECH\BRAIN\tasks\TASK-032\results\r6-email-review.md`.
Um Important, corrida durante as buscas externas, foi reproduzido por cinco testes que
falharam antes da correção e passaram depois. Também comprovadas atualização dos nomes
exibidos e entrega ao novo médico. O relatório anterior à correção foi preservado.

Um Minor adiado: fingerprint de conteúdo não é uma sequência cronológica. Se os dados
mudam e retornam exatamente ao conteúdo anterior (A→B→A), um aviso antigo com os mesmos
dados atuais ainda pode ser enviado, além do aviso novo; cada ocorrência envia no máximo
uma vez. Supressão cronológica estrita exige uma revisão durável/outbox, fora desta rodada.

Outros limites mantidos do envio existente: não há outbox para recuperar automaticamente
um processo que morra entre commit e enqueue; aceitação SMTP seguida de interrupção também
não fornece garantia distribuída de entrega exatamente uma vez. O modo é best-effort,
com retry das falhas transitórias previstas. Avisar o médico anterior sobre cancelamento
por troca de médico é outro aviso, não solicitado nesta rodada.

Lock de linha ainda precisa de prova em PostgreSQL descartável (Docker indisponível nesta
execução); SMTP/brain-api reais não foram acionados. Exige destinatário profissional conhecido
no Brain, SMTP habilitado/configurado e worker atualizado. Commit/merge/push foram autorizados;
deploy continua sem autorização. A ordem de migração/API+worker da R6 original permanece.

## Correções encontradas pela validação ampla

Registro do novo job passou de 13 para 14 funções; os dois testes de identidade/registro
foram atualizados mantendo a lista completa. A suíte também encontrou a expectativa antiga
“Outro” na checagem do documento Meta, deixada pela atualização da documentação da R6 depois
da suíte anterior: agora verifica o rótulo atual “Alterar Dados”. Nenhum teste foi removido
para obter a validação verde.

Snapshots anteriores ao complemento, logs e decisões:
`.superpowers/sdd/r6-email-followup/` no worktree. Graphify da candidata reconstruído por AST;
proveniência registrada em `a132f3a`. O commit posterior do próprio índice muda HEAD sem mudar
o código; conferir freshness antes de usar o grafo como evidência primária.
