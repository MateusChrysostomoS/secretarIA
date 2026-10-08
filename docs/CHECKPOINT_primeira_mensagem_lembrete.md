# Primeira mensagem de quem tem consulta — mesmo lembrete, mesmos fluxos

## Estado

Seguimento do dono em 2026-10-08: substituir a mensagem “Olá / Vi aqui que você já tem uma
consulta...” pelo cartão de lembrete e seus mesmos caminhos, incluindo a primeira mensagem
no WhatsApp. Implementado e validado sobre `main@b9d5cd1`, preservando R6 e o e-mail fixo ao médico.
Integração/publicação Git seguem a autorização da rodada; deploy, SQL remoto e mensagens reais
não foram executados. Os planos futuros R4/R5 permanecem no outro checkout, fora deste ajuste.

Validação: **4427 passed, 49 skipped, 17 warnings**, nenhuma falha, 370,83 s.
Casos novos RED→GREEN: primeira entrada WhatsApp/Portal, cartão idêntico e ids de lembrete,
confirmar/cancelar/alterar, confirmação limitada a dois, entrada superseded, cancelamento entre
composição/envio, isolamento de clínica, privacidade de terceiro e toque rápido após entrega.
Suíte escopada final: **102 passed**; Ruff e diff check limpos.

## O que o paciente vê

Nas clínicas com o novo lembrete habilitado, a abertura elegível com consulta futura usa
**o mesmo construtor e o mesmo cartão** do lembrete, sem a frase “Vi aqui...” ou um segundo
menu de abertura:

```text
LEMBRE-SE: Sua consulta com {{NM_DO_MEDICO}} está marcada para o dia {{DATA}}
às {{HORARIO}} para {{SERVICO}}.

Orientações para a consulta:
• {{ORIENTACAO_DO_SERVICO}}

[Confirmar] [Cancelar] [Alterar Dados]
```

O primeiro parágrafo é uma única linha no envio; a quebra acima apenas apresenta o modelo.
Sem orientações cadastradas, o bloco não aparece. Para terceiro, o sujeito é “A consulta de
{{NOME_DO_ATENDIDO}}”. Sem profissional identificável, usa a equipe da clínica. Dados são
lidos novamente no envio e datas/horários usam o fuso da clínica; limites são os do lembrete.
Nenhuma LLM compõe essa abertura.

- Confirmar: mesma confirmação/deduplicação e menu recorrente Agendar / Outro.
- Cancelar: mesma pergunta de certeza, cancelamento/Pix e motivo após Sim, cancelar.
- Alterar Dados: mesmo menu/rascunho R6, sem alterar a consulta antes do Confirmar final;
  o e-mail fixo ao médico continua disparado depois da alteração salva.

## Onde foi unificado

- `workers/shared/opening.py`: `OpeningKind.UPCOMING` deixa de usar o texto provisório na
  clínica habilitada; usa `load_reminder_content` / `build_reminder_body` e identifica a
  consulta para o envio comum. Contexto e paciente são validados na mesma clínica.
- `workers/shared/reminder_opening.py`: preparação/envio comum da linha `chat` e dos ids
  `remconfirm|...`, `remcancel|...`, `remedit|...` por `_send_buttons_reply`, nos dois canais.
- `workers/turn_router.py`: primeira saudação de paciente já consentido com consulta escolhe
  esse mesmo cartão, em vez de a saudação antiga tomar sua frente. WhatsApp e inbound Portal
  compartilham esse percurso.
- `workers/shared/context.py`: marcador interno da abertura explícita; não há schema HTTP,
  coluna ou migration nova.
- `services/pii_pseudonymization.py`: nome do terceiro em consulta feita pela clínica
  (`conversation_id=NULL`) é identificado pelo paciente **e** tenant antes de copiar o histórico
  para o modelo; reservas continuam escopadas à conversa/tenant. A mensagem para o paciente
  continua com o nome real; o histórico que chega à IA permanece mascarado.

Portal: entrada de conversa conhecida, conta verificada e retorno do código usam a mesma
abertura contextual. O primeiro acesso de desconhecido continua com identificação/privacidade
antes de mostrar informações médicas. WhatsApp: quando o paciente já consentido inicia a
conversa, recebe o cartão de lembrete como abertura, com os mesmos ids e ações.

## Regras mantidas

Interruptor de lembretes OFF conserva o comportamento anterior. Sem consulta futura ou após
consulta, menu/pergunta de pós-consulta continuam como antes. Identificação, consentimento,
handover humano e entitlement continuam precedendo a abertura; nada disso foi pulado.

Uma abertura **explícita** de gestão permite os três botões mesmo se a presença já foi
confirmada duas vezes; o escritor de confirmação continua limitado a dois e o mesmo cartão
não conta de novo. Regras automáticas/cron de não repetir pedidos de confirmação permanecem.

MENU é persistido **antes** da entrega, sem uma segunda bolha de menu; um toque que chega
assim que o cartão aparece não é apagado por um reset posterior. A guarda `still_current`
é verificada novamente **depois** de preparar linha/texto, evitando que uma entrada antiga
apareça depois de um toque válido do paciente em outro cartão.

## Revisão e evidências

Revisão independente: `C:\TECH\BRAIN\tasks\TASK-032\results\first-message-review.md`.
Um Important reproduzido por entrada/inbound reais com rede fake: preparar o lembrete enquanto
chega um `remedit` válido produzia menu de edição seguido por cartão antigo. Teste falhou antes
da correção e passou depois; a entrada superseded agora retorna `None` e não entrega o cartão.
Relatório anterior à correção preservado. Não houve nova rodada de revisão após testes do fix.

Minor adiado: prosa de alguns módulos antigos de testes ainda descreve o lembrete como
substituição futura. Sem efeito sobre o comportamento; aqueles testes ainda cobrem o modo OFF.

Limitações herdadas, registradas pela revisão e não ampliadas nesta tarefa: snapshots de
interruptor/handover/consentimento entre decisão e transporte; erro de envio engolido por
renderizadores pode deixar claim de entrada ocupado; a recuperação desse erro exige ajuste
de transporte/ledger separado. Não foi apresentada como regressão nova. Abertura desconhecida
do Portal conserva frame/identidade; detalhes internos R6/email e rollout já têm checkpoints
próprios. SMTP/Meta/produção não foram usados como prova nesta rodada.

Logs, snapshots, decisões e testes: `.superpowers/sdd/r6-first-message/` no worktree de
integração. Graphify atualizado por AST: 12468 nós / 31014 arestas, sem duplicatas/endpoints
soltos, um self-loop incidental. Conferir freshness antes de confiar nele após commits.

Liberação continua exigindo as mesmas condições R6: banco com a migração anterior e API/worker
atualizados juntos. Este ajuste acrescenta somente código e testes, sem migration.
