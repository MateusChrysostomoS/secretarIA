# TASK-041 — prova real da edição no Portal, 09/10/2026

Resultado: **FAIL para a jornada de texto livre e a escolha de outro médico**. Não aprovar a edição real com base nos testes simulados anteriores.

## Ambiente e identidade

- Clínica Chrysostomo For Eyes; conta de teste indicada pelo dono, autenticada pelo próprio dono com código no Portal. E-mail e código omitidos deste registro.
- Sessão anterior pertencia a outra conta e não foi usada para editar. Contexto separado `task041-patient-qa`; verificação de titularidade pela autoridade de identidade retornou match=true.
- Uma consulta ativa encontrada em 19/10/2026 às 15:20 America/Sao_Paulo, status rescheduled.
- API e worker: fingerprint `a096527cadef`, deploy_parity match. Fingerprint calculado pelos bytes Git de `4545933` coincide; portanto a correção publicada foi efetivamente testada.
- Chrome DevTools MCP, cliques/compositor/lista reais. Sem API de ingress artificial, mock de IA ou calendário. Leitura HTTP autenticada independente para titularidade e consulta; nenhum SQL remoto, Environment ou deploy.

## Roteiro observado — 03:46–03:52 UTC (00:46–00:52 local)

| Entrada real | Resultado observado | Veredito |
| --- | --- | --- |
| Alterar Dados no cartão de 19/10 15:20 | Cinco escolhas da edição | PASS |
| Mudar data pelo botão | Qual o novo dia da consulta? | PASS |
| na verdade gostaria de mudar o serviço | Repete Qual o novo dia da consulta?, sem serviços | FAIL |
| desculpa, eu quero mudar o médico | Repete a pergunta de data, sem médicos | FAIL |
| Mudar médico digitado exatamente como a escolha | Abre Qual médico você quer? | PASS de controle determinístico; não prova IA |
| para o doutor Rafael Teixeira, mantendo o horário | Repete a lista de médicos, sem cartão final | FAIL |
| Ver médicos → selecionar outro médico oferecido/livre → Responder | Repete lista de médicos | FAIL |
| mesmo dia, quero seguir com o mesmo horário e não quero mudar o convênio, por hoje é só | Repete lista de médicos, sem confirmação de mudanças | FAIL |
| /menu para encerrar a prova | Como posso te ajudar?; Agendar / Outro | PASS de limpeza não destrutiva |

A lista mostrava os dois profissionais livres em 19/10 15:20. O médico escolhido veio de uma opção realmente oferecida. Request do browser continha interactive_reply_id com prefixo prof; a mensagem inbound persistida também continha esse prefixo. Portanto não atribuir o resultado a uma escolha inventada ou ausência do identificador no envio do navegador.

As respostas novas não sugeriram cancelar a consulta nem voltaram à remarcação antiga. Isso é uma observação limitada às mensagens desta prova; o objetivo de interpretar o texto livre e chegar ao cartão final não foi cumprido. O cartão final não apareceu, logo confirmação e aviso ao médico NÃO foram exercitados.

## Preservação e limites

Nenhum Confirmar, cancelamento de consulta, alteração de agenda/cadastro, envio de aviso ao médico ou deploy foi executado. O único e-mail necessário ao acesso foi o código de autenticação, solicitado pelo fluxo normal para a conta indicada.

Leitura independente após /menu: consulta continua rescheduled, início 19/10/2026 18:20Z (15:20 local). Comparação antes/depois dos campos expostos pela projeção HTTP: id, início, fim, status e serviço **idênticos**. Essa projeção não expõe médico/convênio; não inventar verificação desses campos por ela nem prova de leitura direta do Google Calendar.

A etapa precisa ser diagnosticada antes de aprovar o fluxo real. Ainda não foi comprovado por que a IA não aplicou as propostas nem por que a escolha estruturada repetiu o seletor. Os logs de serviço não estão expostos nesta sessão; nenhum contorno foi usado. Não inferir o interruptor v1/v2 a partir da aparência da lista ou de registros históricos.

## Evidências sanitizadas

Em `C:/TECH/BRAIN/tasks/TASK-041/results/`:
- `transcript-real-2026-10-09.json`: 18 mensagens de teste, iniciando em Alterar Dados e terminando em /menu; sem histórico anterior, e-mail ou código.
- `live-fixture-check.json`: identificador da consulta, hash da projeção inicial, data e status; sem identidade pessoal.
- `live-appointment-after.json`: comparação independente e campos realmente expostos.

Os 4530 testes da integração e a revisão READY continuam sendo provas locais. A prova real agora foi executada parcialmente e **falhou**; o gate não permanece apenas BLOCKED por falta de sessão. Nenhuma alteração de código, commit ou push foi feita nesta rodada.
