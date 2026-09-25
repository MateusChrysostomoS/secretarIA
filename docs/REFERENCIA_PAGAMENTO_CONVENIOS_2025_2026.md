# Referência — como convênios de saúde pagam a consulta no Brasil (2025/2026)

Pesquisa fornecida pelo dono em 2026-09-25, usada como semente do catálogo canônico de convênios
pedido em `z_prompts/PROMPT_CONVENIO_CATALOGO_ACEITACAO_1_SECRETARIA.md`. **Não é fonte primária**
— é uma compilação de tabelas públicas (CREMERJ, APM, materiais oficiais de operadoras, ANS,
corretoras) datada de set/2025 a jul/2026; releia os caveats no fim antes de tratar qualquer valor
como definitivo, e trate valores por região/operadora como ponto de partida para o catálogo, não
como preço final imutável.

## Por que isso importa para o produto (não é só contexto de fundo)

Existem dois modelos de relação financeira entre paciente, clínica e operadora, e eles implicam
comportamentos DIFERENTES do sinal via Pix (`pix_deposit_percent` em `Tenant`, ver
`docs/CHECKPOINT_pix_deposit.md`):

1. **Credenciamento direto** — a clínica fatura a operadora pela consulta (TISS/TUSS); o paciente
   não paga nada adiantado à clínica (só eventual coparticipação, cobrada por regra própria do
   plano, não pelo fluxo de sinal deste produto). Cobrar um sinal via Pix nesse modelo não faz
   sentido de negócio e pode até conflitar com a regra do convênio.
2. **Reembolso / livre escolha** — o paciente paga a consulta (particular ou via sinal) e depois
   pede reembolso à operadora por conta própria; aqui o sinal continua fazendo sentido, igual ao
   fluxo particular de hoje.

**Decisão do dono (2026-09-25, resposta a `AskUserQuestion`):** não existe uma regra fixa
"credenciado nunca cobra sinal" — cada convênio aceito por uma clínica ganha uma flag própria e
configurável (cobra sinal / não cobra), porque a mesma operadora pode ser credenciamento direto
numa clínica e reembolso em outra, e a clínica é quem sabe sua relação real com cada plano. Ver a
seção de schema em `PROMPT_CONVENIO_CATALOGO_ACEITACAO_1_SECRETARIA.md` para onde essa flag mora.

## TL;DR da pesquisa

- **Honorário por consulta (prestador credenciado):** R$ 110–150 na maioria das operadoras
  grandes; RJ tipicamente R$ 113,50–144 (CREMERJ, set/2025); SP R$ 122–148,53 (APM, dez/2025), com
  exceções (Omint até R$ 291,98 na linha Premium; NotreDame/Hapvida R$ 80–420 conforme contrato;
  Care Plus R$ 116,71–183,89). Diferenciação por especialidade é rara — a consulta costuma ser um
  valor único por operadora, não por especialidade.
- **Reembolso (paciente, livre escolha):** múltiplo da tabela própria da operadora (THSM/CRS no
  Bradesco, TASA na SulAmérica, tabela Amil). Varia de ~R$ 110 (planos de entrada) a R$ 1.000–1.100
  (linhas top: SulAmérica Prestige, Amil Black II, Bradesco Premium). Planos de entrada (Direto,
  Efetivo, Amil Fácil/Bronze, Hapvida) praticamente não reembolsam consulta eletiva.
- **Coparticipação:** costuma ser valor fixo por evento (Unimeds, ex. R$ 33–90 por consulta) ou
  percentual com teto (seguradoras, 30–40%, sem teto regulatório geral hoje — a RN 433/2018 que
  fixava 40% foi revogada; proposta de novo teto de 30% estava em Câmara Técnica da ANS em 2026,
  ainda não é norma).
- **Faturamento:** padrão TISS vigente de maio/jul-2026 (ANS); CBHPM 2022 é referência ética, não
  obrigação de pagamento (UCO R$ 29,80 de out/2025 a set/2026, operadoras usam edições antigas com
  deflator de 8-17%); glosa inicial hospitalar 14-17%, glosa final ~2% (a maior parte é recuperável
  via recurso).

## Tabela 1 — modelo de remuneração por operadora (para classificar cada linha do catálogo)

| Operadora | Modelo com o médico/clínica | Reembolso ao paciente |
|---|---|---|
| Unimed (cooperativas regionais) | Cooperativa — médico é cooperado/sócio; hospitais/clínicas credenciados | Limitado, depende do produto |
| Bradesco Saúde / Mediservice | Rede referenciada, fee-for-service (THSM) | Sim — "Específico" (só consultas) ou "Completo", múltiplos 1x-20x |
| SulAmérica (Rede D'Or) | Rede referenciada, fee-for-service (TASA) + trilha "cuidado coordenado" | Sim, exceto linha Direto (só urgência) |
| Amil (Fácil/Bronze → Black/One Health) | Credenciamento + rede própria parcial | Linhas Ouro+, Selecionada, Black |
| Hapvida NotreDame Intermédica | Verticalizada (hospitais/clínicas/labs próprios) + rede credenciada complementar | Praticamente inexistente nos planos de massa |
| Porto Seguro Saúde | Rede referenciada, fee-for-service | Sim, conforme categoria |
| Omint, Care Plus | Premium, rede seletiva | Reembolso é o diferencial principal |
| Golden Cross, Assim, Caberj | Rede credenciada regional (RJ) | Limitado |
| Prevent Senior, Trasmontano, Alice | Verticalização forte / atenção primária coordenada | Sem dados públicos |

## Tabela 2 — honorário por consulta (prestador credenciado), amostra RJ (CREMERJ, 19/09/2025)

| Operadora | Consulta | Vigência |
|---|---|---|
| Saúde Petrobras | R$ 144,00 | 01/10/2025 |
| FAPES (BNDES) | R$ 140,00 | 01/12/2024 |
| FioSaúde | R$ 138,00 | 01/11/2024 |
| SulAmérica | R$ 135,39 | 01/09/2025 |
| Real Grandeza (Furnas) | R$ 134,00 | 01/10/2024 |
| CASSI | R$ 133,00 (PS R$ 112) | 01/04/2025 |
| Porto Seguro | R$ 132,00 | 01/08/2024 |
| Bradesco/Mediservice | R$ 131,50 | 15/01/2025 |
| CAURJ | R$ 128,00 | 01/07/2024 |
| CABERJ | R$ 127,12 | 01/04/2025 |
| PASA | R$ 125,43 (PS R$ 120,13) | 01/08/2025 |
| Amil (Amil, Lincx, One Health) | R$ 119,50 | 01/11/2025 |
| Assim | R$ 114,76 | 01/09/2025 |
| Postal Saúde | R$ 113,66 | 01/10/2024 |
| Dix/Medial (Amil Fácil/Next) | R$ 113,50 | 01/11/2025 |
| Golden Cross (Vision Med) | R$ 115,28 | 01/01/2024 (pode estar desatualizado) |

Amostra SP (APM, 05/12/2025): Omint R$ 291,98 (Premium)/R$ 139,47 (Skill); Bradesco R$ 140,00
(2026); Porto Seguro R$ 139,15; Amil R$ 138,00; SulAmérica R$ 137,81 (geral)/R$ 158,13 (cuidado
coordenado); Unimed Fesp R$ 122,00 eletiva; NotreDame/Hapvida R$ 80–420 conforme contrato.

## Tabela 3 — reembolso ao paciente por categoria de plano (livre escolha)

**SulAmérica** (grade 1-10): Direto sem reembolso eletivo → Clássico R$ 110,25 → Especial 100 RC
R$ 150,30 → Especial 100 R1 R$ 180,00 → Especial Mais R$ 330,75 → Executivo R1/R2/R3 R$
500/660/880 → Prestige R$ 1.000,00.

**Amil Black** (ex-One Health, 17/09/2026): Black I R$ 456/504/600 (R1/R2/R3); Black II
R$ 704/800/1.104. Bronze/Prata/Fácil sem reembolso; Ouro+ já inclui.

**Bradesco Saúde**: reembolso = CRS × valor CRS × múltiplo do plano (THSM). Efetivo/Flex/Ideal:
1x. Nacional: 1x-3x. Nacional Plus: 4x-8x. Premium: 6x-10x (até 20x no segmento 200+ vidas).

Resumo: só planos com múltiplo ≥4x (~R$ 450+) cobrem uma consulta particular típica de
especialista em SP; planos de entrada com "reembolso" devolvem ~R$ 110.

## Tabela 4 — coparticipação (não é sinal, é cobrança própria do convênio — informativo)

| Operadora | Consulta eletiva | Fonte |
|---|---|---|
| Unimed Costa Oeste (PR) | R$ 33 (30%) / R$ 55 (50%); especialidades R$ 54/R$ 90 | Oficial, set/2026 |
| Unimed VTRP (RS) | R$ 58 fixo | Oficial |
| SulAmérica "Completa 30%" | Direto R$ 30,98 → Prestige R$ 175,54 (30% com teto) | Corretora — valores simulativos, conflitam entre fontes |
| Hapvida | Tabelas regionais; SP/BH ~72% maior que N/NE | Corretora, set/2026 |

Regra geral ANS hoje: sem teto percentual fixo (RN 433/2018 revogada); proposta de teto de 30% em
discussão na Câmara Técnica 2026, ainda não é norma. Exceção vigente: internação psiquiátrica, só
após 30 dias/ano, limitada a 50% (RN 465/2021 art. 19-II).

## Caveats — ler antes de usar qualquer valor como definitivo no seed do catálogo

- Valores do CREMERJ/APM são autoinformados pelas operadoras às entidades de classe — são os mais
  "oficiais" disponíveis, mas contratos individuais de uma clínica específica podem divergir.
- Dados de reembolso da Amil Black e tetos de coparticipação de SulAmérica/Hapvida vêm de
  corretoras (podem estar desatualizados/simplificados) — confirmar na apólice real quando possível.
- Sem dados públicos 2025/26 para Central Nacional Unimed, Unimed-Rio/Ferj, Unimed-BH, Prevent
  Senior, Trasmontano, Alice — nem para Norte/Centro-Oeste/maior parte do Nordeste.
- A proposta de teto de coparticipação de 30% (ANS, 2026) **não é norma vigente ainda**.
- Nenhum valor aqui deve ser usado para FILTRAR ou BLOQUEAR um convênio no catálogo — o catálogo é
  sobre quais planos existem e como cada um tipicamente se relaciona com pagamento (credenciamento
  vs. reembolso vs. coparticipação), não uma tabela de preço fixo que o produto deva impor.
