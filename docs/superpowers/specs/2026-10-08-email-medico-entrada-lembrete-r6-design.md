# Aviso ao médico e entrada por lembrete — especificação proposta

Estado: PLANEJAMENTO, 08/10/2026. Não é correção aplicada nem aceite de produção.
Plano executável: `C:/TECH/BRAIN/z_prompts/PROMPT_CORRECAO_EMAIL_MEDICO_ENTRADA_LEMBRETE_R6.md`.

## Objetivo do dono

Ao confirmar uma alteração real da consulta, o médico recebe o aviso fixo com as
mudanças e os dados completos. Ao abrir o link recebido por e-mail e autenticar-se,
o paciente vê a consulta correta com Confirmar / Cancelar / Alterar Dados.
Validar todos os ramos dessa jornada, inclusive os desvios de convênio/serviço já
observados, antes de declarar a entrega pronta.

Clínica de teste: Chrysostomo For Eyes, tenant
`9c4fa6a5-ffdb-4adb-9fe1-9036270f1246`. Os endereços de teste autorizados estão no
plano; não são prova de função de médico, acesso à caixa postal ou identidade nova.

## Fatos e limites

O dono relata ausência do aviso profissional depois de confirmar mudança de data;
não foi rastreada independentemente uma ocorrência até a caixa postal. O agente
observou abertura inicial correta, menus e confirmação de presença, mas escolhas
reais de Particular e serviço foram seguidas de oferta humana, sem resumo.
Testes locais verdes não superam esses resultados.

Os reenvios anteriores chamaram o envio genérico de template. Não passaram pelo
cron completo que cria o cartão no chat e depois envia o lembrete ao paciente.
O link atual identifica a clínica, não uma ocorrência/versão de consulta.
Janela anônima cria outra sessão de navegador, não outra conversa no banco:
autenticar o mesmo endereço retoma a mesma identidade. O endereço Sinapse já era
o da conversa usada antes. O HTTP 500 de consulta de conversas foi superado no
reteste, sem prova independente da causa ou do stamp remoto.

Evidência histórica: `docs/TESTES_POSDEPLOY_lembretes_r6_2026-10-08.md`.
Não atribuir os sintomas a migração, SMTP, gate, parser ou outbox sem reproduzir
e localizar a quebra na execução atual.

## Comportamento exigido

1. Aviso profissional: uma edição confirmada, gravada e com mudanças reais gera
   uma ocorrência própria; nenhum aviso por navegação, descarte, ausência de
   mudança ou gravação recusada. Confirmação de presença não é confirmação de edição.
2. O modelo permanece literal, sem LLM: “A consulta de {{NM_DO_PACIENTE}} mudou
   para:” seguido somente das mudanças reais e depois paciente, responsável,
   médico, serviço, convênio, data, início/término e unidade/endereço/links disponíveis.
3. Destinatário vem do vínculo profissional autorizado pela autoridade de
   identidade. Troca de médico avisa o novo; não atribuir acesso por match de e-mail.
4. Falha de e-mail não desfaz a consulta já salva. Erros transitórios têm retry
   limitado e motivos observáveis; recusas permanentes são identificadas. Não
   alterar kill switches, entitlement ou regras de canal para esconder a causa.
5. Após abrir link de lembrete válido, com autenticação/consentimento concluídos
   e consulta futura ativa própria, o cartão deve ficar visível e utilizável sem
   o paciente precisar enviar “oi”, aguardar inatividade ou limpar histórico.
6. Links novos identificam contexto de lembrete com referência opaca; nenhum
   nome, e-mail, telefone, OTP ou dado clínico na URL. A referência não concede
   acesso: titularidade/tenant/versão são verificados no servidor após autenticar.
7. Link antigo só de clínica continua funcionando: seleção segura da consulta
   futura pertinente após login, sem inventar alvo quando houver ambiguidade.
   Link de outra pessoa/clínica não mostra nem altera dados alheios.
8. **Decisão confirmada pelo dono em 08/10/2026:** entrada explícita pelo lembrete
   mostra o cartão preservando rascunho em andamento. Exibir/recuperar cartão não
   pode resetar MENU nem apagar estado da edição. Retomar Alterar Dados da mesma
   consulta preserva as escolhas; mudar para outra consulta não perde o rascunho
   silenciosamente. Navegação comum mantém suas guardas de conversa recente.
9. Reabrir/recarregar não conta confirmação e não multiplica cartões, jobs ou
   e-mails. Cartão recuperado continua válido para toque; não revelar um cartão
   que o limite de dez cartões recentes já tornou inutilizável.
10. Handover humano e identidade/consentimento pendentes continuam protegidos.
    O plano precisa definir resposta/retomada visível para esses estados; não
    forçar automação a retomar a conversa humana nem contornar login.
11. Cancelada/passada/remarcada depois do envio não aceita ação da versão antiga;
    explicar a situação e oferecer o estado atual próprio quando apropriado.
12. R6 mantém rascunho sem escrita antecipada, resumo único com Confirmar /
    Cancelar / Alterar Mais Dados, perguntas condicionais de horário e menus
    completos, sem desvio para LLM em uma escolha válida de lista/botão.
13. Texto livre pode usar IA dentro da etapa conforme a regra existente;
    templates de abertura, resumo determinístico e aviso profissional não usam IA.
14. Google e banco precisam refletir a mesma consulta. Troca de agenda cria antes
    de remover a anterior; falhas não anunciam sucesso e preservam recuperação.
15. Preservar restrições Pix/entitlements e o modo OFF. Exercitar dinheiro em
    ambiente descartável, sem cobranças/reembolsos financeiros reais nesta QA.
16. Aceite envolve inbox do destinatário, cliques reais e estado final da agenda.
    ACK/queued/SMTP aceito, clique programático e teste mockado são provas de
    etapas específicas, não prova de recebimento ou de toda a jornada.

## Abordagem proposta

Investigar primeiro e criar reproduções RED do caminho completo do worker.
Corrigir a menor falha comprovada no aviso profissional; adotar persistência
durável/outbox apenas se perda entre commit/fila for demonstrada e necessária
ao contrato, com migração aditiva e recuperação/idempotência especificadas.

Para entrada, distinguir intenção explícita de link do retorno genérico. Propagar
um contexto opcional pela cadeia de link, autenticação, entrada e worker; verificar
esse contexto na SecretarIA, dona da consulta. Reutilizar o cartão/construtor e as
ações atuais, com política de apresentação que não sobrescreva fluxo ativo.
Decidir detalhes internos reversíveis com base no diagnóstico, sem reconstruir
a máquina de estados ou introduzir LLM nessa abertura.

O plano contém tarefas, arquivos, contratos propostos, exemplos de regressão,
matriz de cobertura, prova em PostgreSQL descartável, rollout e limpeza das fixtures.
Implementação, publicação e QA de produção são etapas futuras, não executadas
ao escrever esta especificação.
