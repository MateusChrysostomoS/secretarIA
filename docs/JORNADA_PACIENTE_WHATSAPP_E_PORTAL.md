# Jornada do paciente — WhatsApp e Portal, em linguagem leiga

Este arquivo descreve como o paciente **vê e vive** cada fluxo determinístico da secretarIA,
sem citar código, função ou arquivo. É a referência de produto para decidir o que ainda falta
implementar. Cada bloco traz uma marca de status:

- **(no ar)** — já acontece hoje, em produção, no canal indicado.
- **(construído, não no ar)** — o código existe (numa branch ou fica pendente de deploy/merge),
  mas o paciente ainda não vê isso na vida real.
- **(não existe ainda)** — não há nenhum código para isso; é só intenção de produto.

Datado em 2026-09-20, corrigido em 2026-09-23 (pergunta do nome marcada como no ar; seção de
convênio adicionada — faltava por completo). Este arquivo descreve o estado real verificado na
data da última correção — releia antes de confiar nele para decisões novas, porque cada rodada de
trabalho muda esse mapa.

Atualização pontual em 2026-09-30 (TASK-021): os blocos abaixo estão **construídos, não no ar**; não houve deploy nem prova ao vivo desta rodada.

---

## 1. Primeiro contato

### No WhatsApp (no ar)
A pessoa manda "oi" pro número da clínica. A secretarIA se apresenta com o nome e uma frase
curta sobre a clínica, pede o aceite dos termos (sem isso a conversa não avança pra nada) e
mostra o menu principal: **Agendar** ou **Outro**. Quem já tem consulta marcada vê, no lugar
disso, um resumo da consulta e atalhos pra remarcar/cancelar.

### No Portal (no ar, hoje; a versão "ideal" descrita abaixo está construída numa branch, aguardando aprovação para entrar no ar)
Hoje: o paciente abre um link da clínica e cai numa tela de login (e-mail + senha/código) antes
de conseguir conversar.

Versão já construída (falta só decisão de merge/deploy): não existe mais tela de login. O
link abre direto na conversa, a secretarIA já fala primeiro (sem o paciente precisar escrever
nada), pede o e-mail dentro da própria conversa, e só no fim — depois de a pessoa já poder
conversar e até marcar uma consulta — pede o código de 6 dígitos que confirma a conta.

O aviso do código vem com uma mensagem de 3 botões:
- **⬅️ Voltar** — desiste, volta pro menu.
- **↩️ Reenviar Código** — manda outro código pro mesmo e-mail.
- **📩 Mudar e-mail** — volta pra digitar um e-mail diferente.

A mensagem sempre deixa explícito pra qual endereço o código foi (mascarado, nunca o e-mail
inteiro: `a***a@gmail.com`), porque a pessoa pode ter digitado errado e precisa saber pra qual
caixa olhar.

### Pergunta do nome (no ar, desde 2026-09-21)
A secretarIA pergunta explicitamente "Prazer! Qual o seu nome?" nos dois canais — no WhatsApp,
sempre no primeiro contato (o `profile.name` do Meta não é usado, por não ser confiável); no
Portal, só no ramo em que o e-mail é de fato novo (entre o e-mail e o LGPD — quem já tem conta
pula direto pro pedido de código). Ver `docs/CHECKPOINT_abertura_pergunta_nome.md`.

---

## 2. Escolher serviço, profissional, unidade, convênio, data e horário (mesma lógica nos dois canais)

> **Ordem nova (construído, não no ar — 2026-11-21, branch local `task/TASK-088`; pedido do dono em
> 2026-11-20).** O paciente resolve primeiro o que ele precisa: o **serviço**. Depois vem quem
> faz, depois o lugar, e o **convênio é a última escolha** antes do dia — porque só dá para saber
> se o convênio vale depois de saber serviço, profissional e unidade. Isto substitui a ordem
> "convênio primeiro" de setembro (ver a seção de convênio abaixo). Detalhes técnicos em
> `docs/CHECKPOINT_ordem_agendar_servico_primeiro.md`; os passos 1 a 5 abaixo já estão na ordem nova.

1. Escolhe o **serviço** (só o nome; nenhum valor aparece nesta tela). Se a clínica tem mais de um
   profissional, a lista traz os serviços de toda a clínica. Se não sabe qual escolher, pode contar
   o que está sentindo/precisando e a secretarIA sugere — sempre dentro dos serviços reais da
   clínica, nunca inventa um nome nem fala preço.
2. Se mais de um profissional faz aquele serviço, escolhe entre **só os que fazem** (nome +
   especialidade). Clínica de um profissional só: esse passo some sozinho. O menu não tem mais a
   porta "Escolher profissional": fica "Escolher serviço".
3. Se o profissional atende em mais de uma unidade, escolhe a **unidade**; se o serviço só é feito
   em uma delas, a secretarIA avisa "feito **apenas na unidade X**" (com o "apenas na unidade X" em negrito) e pede um sim/não.
4. Se a clínica pergunta convênio, aí vem o **convênio**: a lista mostra **só os planos aceitos**
   para aquele serviço, com aquele profissional, naquela unidade (mais "Particular"; "Outro
   convênio" só aparece quando há ao menos um plano aceito). Se a unidade escolhida não aceita
   convênio para esse atendimento, a secretarIA avisa. Quando outra unidade oferece o mesmo serviço
   com aquele profissional e aceita convênio, o paciente pode usar **"Unidades c/ convênio"** para
   ver as opções e os planos, ou **"Seguir particular"** na unidade que escolheu. Se nenhuma unidade
   alternativa aceitar convênio, a secretarIA explica que o atendimento será particular e mostra os
   dias. Quem já escolheu Particular mantém a escolha ao voltar ou trocar serviço, profissional ou unidade.
5. Escolhe a data. **No WhatsApp**, por causa do limite de botões do WhatsApp, é em duas telas:
   primeiro o mês, depois o dia daquele mês. **No Portal, hoje**, é a mesma lógica — não existe
   ainda um calendário visual no Portal; a ideia de um calendário de verdade (ver seção 8) é só
   proposta de produto.
6. Escolhe o horário livre daquele dia. Se o profissional configurou um **intervalo entre consultas**,
   só aparecem horários que deixam essa folga em volta das outras consultas dele.
7. Na confirmação final aparece o **valor**, e só ali: quem escolheu Particular vê o valor do
   serviço; quem escolheu convênio não vê valor; e, se a clínica cobra sinal, o paciente vê o valor
   do sinal e o da consulta inteira. Não existe mais o cartão "Deseja agendar esse serviço?" no meio
   do caminho (ele mostrava o valor e a descrição do serviço); a descrição longa deixou de aparecer
   nos botões.
8. Em qualquer uma dessas telas, o botão de voltar leva exatamente 1 passo atrás — nunca pula
   direto pro início: do dia volta para o convênio ("⬅️ Trocar convênio") quando ele foi perguntado,
   senão para a unidade ou o serviço.

### "Essa consulta é pra você?" (construído, não no ar — 2026-09-25, branch `task/TASK-007-secretaria`)
É a PRIMEIRA pergunta de qualquer agendamento, antes do serviço, do profissional e do convênio,
nos dois canais. Aparece ao tocar "Agendar", "Escolher serviço" (um "Escolher médico" digitado
também funciona) ou, na clínica de um profissional só, "Serviços e Custo". Dois botões: **✅ Sim, é pra mim**, e o agendamento
segue exatamente como antes; ou **👤 Pra outra pessoa** (ex.: um filho marcando pra mãe). No
segundo caso a secretarIA pergunta o nome completo de quem vai ser atendido e mostra a frase de
autorização com o nome já preenchido: "Ao informar os dados de **[nome]**, você confirma que tem
autorização para compartilhar essas informações com a clínica." A pessoa precisa tocar
**✅ Confirmar** para seguir; **⬅️ Cancelar** volta para a pergunta "é pra você?". O aceite fica
registrado para auditoria. Daí em diante a agenda da clínica, a confirmação, o lembrete e o
e-mail ao profissional usam o nome de quem vai ser atendido. Só o nome é pedido (nada de CPF,
telefone ou data de nascimento do terceiro). Pelo chat livre a secretarIA não marca consulta para
outra pessoa: ela devolve ao menu para passar por essa pergunta.

### “Outro” e “Não sei” (construído, não no ar — MVP Portal, 2026-09-30)
Ao tocar “Outro” ou não saber qual profissional/serviço escolher, a conversa livre recebe o contexto e preserva as escolhas já feitas. Orienta dentro dos serviços reais e devolve escolhas ao agendamento, que continua conferindo cada etapa. O toque em “Outro” no menu responde sempre a mesma pergunta fixa, “O que te traz à clínica?”, sem usar a IA; a conversa livre começa na resposta da pessoa (construído 2026-10-06, TASK-037, não no ar — ver `docs/CHECKPOINT_outro_pergunta_fixa.md`); perguntas comuns não pedem transferência automaticamente. Desde a TASK-038 (construído 2026-10-07, não no ar — `docs/CHECKPOINT_ia_atende_como_pessoa.md`): uma pergunta feita no meio de uma etapa (convênio, escolha do dia) é respondida e a lista continua valendo; quando a IA leva a pessoa de volta aos botões, a explicação dela aparece junto; o “Não sei” abre com uma pergunta simples (primeira consulta, retorno…); e chamar alguém da equipe é sempre o último recurso — quando a secretária não consegue ajudar, ela pergunta “Quer que eu chame nosso atendente humano? (Pode demorar alguns minutos)” com os botões Sim/Não, e só chama a equipe se a pessoa tocar Sim; se tocar Não, a conversa continua com a secretária, sem perder o que já foi escolhido. Pedido explícito de pessoa permite atendimento humano com aviso à clínica. O add-on de atendimento humano do WhatsApp mantém seu comportamento existente.

A secretarIA pode informar o preço cadastrado quando o paciente pergunta na conversa livre. Para quem usa convênio,
ela explica que a clínica confirma cobertura, coparticipação e eventual cobrança.

### Evento e confirmação no Portal (construído, não no ar — 2026-09-30)
A consulta cria evento com serviço, convênio e, quando autorizado, quem será atendido. A agenda da clínica recebe lembretes; no Portal, paciente com e-mail disponível recebe confirmação e convite para sua própria agenda. Falha de envio ou convite não cancela a consulta. Confirmação operacional à clínica identifica o agendamento sem expor dados pessoais do paciente.

### Convênio (no ar, quando a clínica ativa — mesma lógica nos dois canais)
> **Substituído (TASK-088, construído, NÃO deployado, 2026-11-21):** a ordem de setembro
> (convênio logo depois de "pra quem", marcas "✅ Aceita seu convênio" na lista de profissionais) foi
> trocada pelo dono em 2026-11-20: o convênio agora é a **última** escolha antes do dia, filtrado
> pelo serviço, profissional e unidade já escolhidos (ver a seção 2 acima); a lista de profissionais
> não tem mais marcas. Os planos continuam vindo do catálogo global. Ver
> `docs/CHECKPOINT_ordem_agendar_servico_primeiro.md` e `docs/CHECKPOINT_convenio_catalogo.md`.
> (TASK-006, construído, NÃO deployado, era assim: o convênio era a PRIMEIRA pergunta depois de
> "pra quem", antes do profissional; a lista de profissionais mostrava todos e marcava
> "✅ Aceita seu convênio" nos que aceitam o plano escolhido.) No MVP Portal (TASK-021), contexto e evento também carregam o convênio escolhido: **construído, não no ar**, sem verificação nova de elegibilidade. O texto abaixo descreve o que está no ar hoje.

Depois de confirmar o serviço, se a clínica ligou "perguntar convênio" e cadastrou pelo menos um
plano aceito, a secretarIA pergunta "Você vai usar convênio?" com uma lista: os planos da clínica
+ "Particular" + "Outro convênio" (que abre uma pergunta de texto livre com o nome do plano). A
resposta é **só informativa** — carimbada no agendamento pra a recepção ver, mas nunca filtra
médico, serviço, preço ou horário disponível; hoje não existe verificação de elegibilidade nem
preço diferente por convênio. Vale pra clínica com um médico só ou vários — não é recurso
multi-profissional. **(Adicionado a este mapa em 2026-09-23 — já estava no ar e não constava aqui;
ver `flow_router.py::_enter_insurance`/`_handle_insurance`.)**

### Pagamento (sinal pelo Pix) — (no ar, quando a clínica ativa)
Cada clínica decide se cobra sinal pra confirmar a consulta, e se é 100% do valor ou uma
porcentagem. Quando ativado, a mensagem final pede o Pix antes de confirmar; sem isso ligado,
a consulta confirma na hora.

---

## 3. Se a pessoa escreve de novo antes da consulta (no ar)

Se já existe uma consulta marcada, a secretarIA não trata como contato do zero: a primeira
coisa que ela faz é lembrar a consulta (profissional, serviço, dia, hora) e qualquer orientação
de preparo (jejum, exames, documentos) — só depois oferece remarcar, cancelar ou outro assunto.

---

## 4. Lembretes automáticos

### No ar
Um dia antes e duas horas antes da consulta, a secretarIA manda um lembrete por conta própria,
com a consulta + o preparo necessário, e três botões pra agir ali mesmo: confirmar, reagendar
ou cancelar.

### Não existe ainda
Se os dois lembretes ficarem sem resposta, a ideia é a secretarIA avisar a clínica por e-mail,
sugerindo contato direto — hoje esse aviso só existe para uma situação bem diferente (a agenda
do médico ficar inacessível), não para paciente que não responde.

---

## 5. Depois da consulta

### Só a configuração existe (não existe ainda o envio de verdade)
A clínica já pode cadastrar um texto de "mensagem pós-consulta" e um material de apoio pra
secretarIA usar se o paciente perguntar algo depois — mas isso nunca é mandado
automaticamente hoje. A ideia de produto é: quando a consulta termina, a secretarIA manda essa
mensagem perguntando como foi, e — quando fizer sentido clinicamente — já aproveita pra
oferecer deixar o retorno reservado. Configurável por clínica, porque mandar mensagem fora da
janela grátis de 24h do WhatsApp tem custo.

---

## 6. Quando a pessoa volta depois de já ter sido atendida (no ar)

Diferente do lembrete (a secretarIA quem chama) — aqui é a pessoa que escreve de novo, dias ou
semanas depois. A saudação já reconhece que ela foi atendida antes, sem perguntar de novo o que
já se sabe, e ajuda a marcar uma consulta nova pelo mesmo caminho da seção 2.

---

## 7. Remarcar ou cancelar, a qualquer momento (no ar)

A pessoa pode remarcar ou cancelar a consulta marcada a qualquer momento, inclusive direto
pelos botões do lembrete. Como a secretarIA já sabe qual é a consulta, não repete pergunta de
profissional/serviço — só pede a nova data (remarcar) ou a confirmação (cancelar).

---

## 8. Calendário dinâmico (não existe ainda em canal nenhum)

Ideia de produto: em vez da sequência mês → dia → horário, a mensagem da secretarIA traria um
botão que abre um card maior — do jeito que uma lista de botões abre hoje — mostrando a agenda
inteira do profissional, do dia de hoje pra frente, com os horários ocupados já aparecendo
visualmente bloqueados (sem revelar o que está marcado ali, só que está indisponível). A pessoa
clica no horário livre que quer e confirma na mesma tela. Vale tanto pro WhatsApp (como
substituto da sequência mês/dia/horário) quanto pro Portal (como o calendário de verdade que a
seção 2 menciona). Nada disso foi desenhado tecnicamente ainda — é só a descrição de como
deveria parecer pro paciente.

---

## 9. Específico do Portal — os 3 links e o roteamento automático

**(construído, não no ar — mesma branch da seção 1)**

A clínica tem uma tela de configuração (atrás de uma engrenagem) com 3 links pra distribuir:

| Link | O que abre | Mostra a escolha entre secretarIA/PreCheck? |
|---|---|---|
| Clínica (geral) | secretarIA (ou o único produto que a clínica tiver) | Sim, se a clínica tiver os dois produtos |
| secretarIA | Direto na secretarIA | Não — link pensado pra quem não conhece o PreCheck |
| PreCheck | Direto no PreCheck | Sim, se a clínica tiver os dois produtos |

Quando um paciente **totalmente novo daquela clínica** (nunca conversou com ela antes, mesmo
que já tenha conta no Portal de outra clínica) tem uma consulta marcada, a secretarIA já sabe
mandar a mensagem de abertura sozinha (seção 1). A parte que falta fechar é a mesma consulta
acionar a conversa de apresentação do PreCheck automaticamente — hoje isso está resolvido no
papel (não precisa mexer no repositório do PreCheck nem imitar o que o n8n faz lá: dá pra
reaproveitar uma chamada que o brain-api já faz para abrir uma conversa vazia do PreCheck sem
fabricar uma mensagem falsa do paciente), mas ainda não foi escrito.

---

## Princípios que valem em qualquer canal

- **Voltar é sempre 1 passo atrás**, nunca pula uma etapa, nunca mostra dois botões de voltar
  ao mesmo tempo.
- **O que é sempre igual**: a ordem serviço → profissional → unidade → convênio → data → horário
  (construído, não no ar — era profissional → serviço até a TASK-088); o lembrete de 1 dia e de 2
  horas antes; o botão de voltar.
- **O que cada clínica ajusta**: texto de boas-vindas, cobrar sinal ou não (e quanto), orientação
  de pré-consulta por serviço, mensagem pós-consulta ligada ou desligada.
- **WhatsApp e Portal contam a mesma história** — o que muda é só a tela (lista de botões vs.
  card/calendário), nunca a ordem das perguntas nem as regras de negócio.
