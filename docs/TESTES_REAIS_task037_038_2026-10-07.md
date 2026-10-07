# TASK-037/038 — testes reais após deploy (2026-10-07)

Rodada de 07/10/2026, 11:05–11:16 BRT, no Portal da clínica de teste (Chrysostomo For Eyes), sessão de
paciente de teste já autenticada. Deploy feito pelo dono.

**Identidade do deploy:** `/build` → API e worker com fingerprint `6f4fae0d2333`, `deploy_parity=match`,
migração `e7d3c1a9b5f2`. O mesmo algoritmo sobre `git archive 2455431 src/secretaria` dá `6f4fae0d2333`:
o que está no ar é a `main` `2455431` (TASK-037 + TASK-038 + captura de datas de outra sessão).

Ferramenta: claude-in-chrome com a aba "escondida" — envio pelo próprio formulário (`requestSubmit`),
toques por clique no DOM, leitura recarregando a conversa. Nenhuma consulta confirmada ou cancelada.

## Resultados

| # | O que o paciente fez | O que aconteceu | Veredito |
|---|---|---|---|
| 1 | Tocou "Outro" no menu | "O que te traz à clínica?" na hora | PASS |
| 2 | "Estou enxergando embaçado de perto…" | Fala da IA + cartão do convênio na sequência; não perguntou "pra quem" | PASS (ver A) |
| 3 | No convênio: "Antes disso, qual o endereço da clínica?" | Endereço real + "Quando quiser, é só escolher o convênio na lista acima"; nada gravado como convênio | PASS |
| 4 | No convênio: "não tenho" | Aceito; seguiu para a lista de médicos | PASS (gravação como Particular coberta por teste, não vista na tela) |
| 5 | "Não sei" na lista de médicos | "Sem problema, eu te ajudo a escolher! É uma primeira consulta, um retorno ou…" | PASS |
| 6 | "É uma primeira consulta, quero avaliar a vista…" | Fala ("Entendi — você quer uma consulta…") + lista de médicos | PARCIAL (ver B) |
| 7 | "Não sei" no serviço → "Tenho catarata… preciso operar" | Abertura nova; escolha veio com o porquê antes do cartão do serviço | PASS |
| 8 | Na escolha do dia: "Vocês têm estacionamento aí?" | "Não tenho informação sobre estacionamento na clínica." + cartão "Não sou capaz… Quer que eu chame nosso atendente humano? (Pode demorar alguns minutos)" ✅ Sim / ❌ Não | PASS |
| 9 | Tocou "❌ Não" | "Tudo bem! Se quiser, me conta de outro jeito…" + menu | PASS (ver C) |
| 10 | Na escolha do dia: "Posso ter desconto se pagar à vista?" | Não virou terça; IA disse não ter a informação + oferta de atendente | PASS |
| 11 | Com a oferta aberta: "Tudo bem, não precisa. Pode ser qua às 10" | Oferta caiu; IA levou aos horários de **hoje** (quarta 07/10) à tarde, dizendo "quarta (07/10) às 10:00" | PARCIAL (ver D) |
| 12 | Na lista de dias: "qua às 10" | Horários de **14/10** (quarta que vem) | PASS |
| 13 | Pergunta sem resposta ("bitcoin?") → oferta → "✅ Sim" | "Vou te conectar com alguém da nossa equipe…"; mensagem seguinte do paciente ficou sem resposta do robô (equipe assumiu) | PASS (ver E) |

## Pontos a melhorar

- **A.** A fala que acompanha o cartão às vezes anuncia o passo errado ("quer que eu mostre os dias
  disponíveis?" antes do cartão de convênio; "vou mostrar… os dias" antes da lista de médicos), apesar da
  instrução de não anunciar a próxima lista.
- **B.** Nenhum médico da clínica de teste é de olhos (Clínica Geral, Cardiologia) e a IA não disse isso
  com franqueza, como a instrução pede.
- **C.** A oferta aparece no meio de um agendamento; "Não" volta ao menu e o andamento se perde. Decisão
  do dono: voltar ao ponto em que estava, ou manter o menu.
- **D.** Inconsistência de datas: dito numa quarta depois das 10h, "qua às 10" virou **hoje** pela IA
  (horário já passado, sem aviso) e **quarta que vem** pela lista de dias. A leitura da IA vem da captura de
  datas da outra sessão (`services/booking_dates.py`), não da TASK-038.
- **E.** O aviso à clínica (e-mail) não foi confirmado: a leitura de logs do Easypanel devolve só as linhas
  mais antigas. O robô volta sozinho para essa conversa depois de 30 min sem mensagem da equipe
  (`HANDOVER_TIMEOUT_MINUTES`).
- A clínica de teste não tem estacionamento/descontos cadastrados nos fatos da clínica; por isso a oferta
  de atendente apareceu (comportamento esperado).

## Reteste dos ajustes do dono (c11f4ad), 12:49–12:54 BRT

Deploy: API e worker com fingerprint `78622db587aa` = `main` `c11f4ad`, `deploy_parity=match`. A conversa de
teste, entregue à equipe no teste 13, voltou sozinha ao robô após o tempo limite (respondeu `/menu`).

| # | O que o paciente fez | O que aconteceu | Veredito |
|---|---|---|---|
| R1 | "Outro" → "Estou enxergando embaçado de perto… avaliar a vista" | Fala + cartão do convênio, sem dizer que não há médico de olhos | Esperado com estes dados: o Dr. Diogo (Clínica Geral) oferece "Cirurgia de Catarata", serviço de olhos — para a IA, algo da clínica corresponde |
| R2 | "…uma mancha na pele, vocês têm dermatologista?" | "A clínica não tem dermatologista. Temos Clínica Geral (Dr. Diogo Raposo) e Cardiologia. Quer agendar uma Consulta com o Dr. Diogo…?" | PASS na franqueza; PARCIAL: mostrou o cartão do convênio sem esperar a resposta |
| R3 | "Vocês têm estacionamento aí?" → oferta → "❌ Não" | "Tudo bem! Pode continuar me contando o que você precisa que eu sigo te ajudando por aqui." — sem menu | PASS |
| R4 | Em seguida: "…com o Dr. Diogo pra mim, particular, quarta às 10" (quarta, 12h53) | IA seguiu a conversa; após escolher o serviço, horários de **14/10** (próxima quarta) | PASS |

Ainda abertos: a fala às vezes anuncia o passo errado (ponto A) e, no caso R2, a IA devolveu aos botões antes
da resposta do paciente apesar da instrução.

