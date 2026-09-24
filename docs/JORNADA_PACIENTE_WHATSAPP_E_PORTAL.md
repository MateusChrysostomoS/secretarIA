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

## 2. Escolher profissional, serviço, data e horário (mesma lógica nos dois canais)

1. Se a clínica tem mais de um profissional, a pessoa escolhe entre eles (nome + especialidade).
   Se não sabe qual escolher, pode contar o que está sentindo/precisando e a secretarIA sugere —
   sempre dentro do time real da clínica, nunca inventa um nome.
2. Escolhe o serviço daquele profissional (nome + preço). Mesma ajuda por conversa livre, se não
   souber.
3. Escolhe a data. **No WhatsApp**, por causa do limite de botões do WhatsApp, é em duas telas:
   primeiro o mês, depois o dia daquele mês. **No Portal, hoje**, é a mesma lógica — não existe
   ainda um calendário visual no Portal; a ideia de um calendário de verdade (ver seção 8) é só
   proposta de produto.
4. Escolhe o horário livre daquele dia.
5. Em qualquer uma dessas telas, o botão **⬅️Voltar** sempre volta exatamente 1 passo — nunca
   pula direto pro início.

### Passo que falta (não existe ainda)
"Essa consulta é pra você?" — hoje a secretarIA sempre assume que quem está escrevendo é quem
vai ser atendido. Não existe pergunta nem fluxo pra marcar consulta pra outra pessoa (ex.: um
filho marcando pra a mãe), nem a frase de autorização que isso exigiria ("ao informar os dados
de [nome], você confirma que tem autorização para compartilhar essas informações com a
clínica"). É só proposta de produto.

### Convênio (no ar, quando a clínica ativa — mesma lógica nos dois canais)
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
- **O que é sempre igual**: a ordem profissional → serviço → data → horário; o lembrete de 1
  dia e de 2 horas antes; o botão de voltar.
- **O que cada clínica ajusta**: texto de boas-vindas, cobrar sinal ou não (e quanto), orientação
  de pré-consulta por serviço, mensagem pós-consulta ligada ou desligada.
- **WhatsApp e Portal contam a mesma história** — o que muda é só a tela (lista de botões vs.
  card/calendário), nunca a ordem das perguntas nem as regras de negócio.
