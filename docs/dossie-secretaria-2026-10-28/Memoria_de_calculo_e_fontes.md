# SecretarIA — memória de cálculo e fontes

Data: 28/10/2026. Base: main@c9b6ba2d767756a88429496f066406c7ada79c8a. Documento principal: SecretarIA_Dossie.html e SecretarIA_Dossie.pdf.

## Como ler

O HTML é autocontido e inclui simulador offline. O PDF registra as premissas centrais e o inventário de APIs.
A análise descreve o código local; não comprova versão implantada ou configurações reais. Nenhum segredo ou dado de paciente foi consultado.

## Modelo econômico

Haiku 5.5, API padrão, faixa até 100.000 tokens de prompt por requisição: US$ 0,10/M de entrada e US$ 0,50/M de saída.
Acima do limiar: US$ 0,50/M e US$ 2,50/M. Cache de 5 min: gravação US$ 0,125/M; leitura US$ 0,01/M na faixa menor.
Tarifas verificadas em 28/10/2026. Câmbio hipotético: R$ 5,50/US$, sem tributos/spread. Sem desconto de cache nos cenários.

`LLM por consulta = (p/q) * k * câmbio * (I * preço_in + O * preço_out) / 1.000.000`

p = conversas com IA / conversas iniciadas; q = consultas novas / conversas iniciadas; k = requisições de modelo por conversa com IA, acumulando todos os turnos.
I/O = tokens médios por requisição, incluindo prompt, histórico, ferramentas, resultados e saída faturável/thinking quando aplicável.
Modelo simplificado: até uma nova consulta por conversa. Custos de conversas sem consulta são atribuídos às consultas concluídas na coorte.

| Cenário | p | q | k | I | O | R$/consulta | R$/1.000 consultas |
|---|---:|---:|---:|---:|---:|---:|---:|
| Leve | 15% | 75% | 2 | 4000 | 500 | 0.0014300 | 1.4300 |
| Central | 35% | 60% | 3 | 8000 | 1000 | 0.0125125 | 12.5125 |
| Intensivo | 65% | 40% | 6 | 16000 | 2000 | 0.1394250 | 139.4250 |

Central exato: R$ 0,0125125/consulta. Com reserva hipotética de 30%: R$ 0,01626625.
Em 1.000 consultas: R$ 12,5125 ou R$ 16,26625 com reserva. Em 10.000: R$ 125,125 ou R$ 162,6625 com reserva.
Um paciente que use apenas o fluxo guiado pode consumir zero tokens de chat; isso não zera custo de canal ou infraestrutura.
Uma conversa com IA central custa R$ 0,02145 antes de atribuir abandonos/conversão. Uma consulta exclusivamente guiada custa R$ 0 de LLM conversacional.

## Não incluído na estimativa LLM

Hospedagem API e worker, Postgres, Redis, observabilidade, suporte, Meta/mensageria, e-mail, áudio OpenAI/Groq, R2, tarifas de pagamento, contexto administrativo, PreCheck e tributos.
Ausência de valor contratual não significa custo zero. A reserva de 30% não é garantia de teto.

## O que medir para conhecer a média

Conversas iniciadas; taxa de ativação; chamadas em agente/ajuda/extração; input normal, cache criação/leitura, output, erros/retries faturados; novas consultas deduplicadas; comparecimento.
Conciliar o uso de todas as rotas com a conta do fornecedor. Evitar contar thinking duas vezes se já incluído em output.
`llm_turn_trace` é um ponto de partida, não um livro contábil completo: saídas por ferramentas, erros e chamadas fora do agente precisam de cobertura.

## Referências externas

- https://platform.claude.com/docs/en/about-claude/pricing
- https://platform.claude.com/docs/en/models/haiku-5-5/overview
- https://whatsappbusiness.com/products/platform-pricing/

## Fontes locais e escopo

- **S01 · Entrada e implantação**: src/secretaria/main.py; src/secretaria/api/webhook.py; src/secretaria/workers/arq_worker.py. API, webhook, fila, worker e rotinas agendadas.
- **S02 · Jornada e decisão**: src/secretaria/services/flow_router.py; src/secretaria/workers/turn_router.py; src/secretaria/workers/orchestrator.py. Fluxo guiado, delegação à IA e efeitos do turno.
- **S03 · Modelo e tokens**: src/secretaria/config.py; src/secretaria/ai/chat_model.py; src/secretaria/ai/graph.py; src/secretaria/ai/trace.py; src/secretaria/ai/scoped_help.py. Haiku 5.5, seleção por chave, cache, histórico e rastros de uso.
- **S04 · Dados, agenda e privacidade**: src/secretaria/models/; src/secretaria/services/calendar.py; src/secretaria/services/local_calendar.py; src/secretaria/ai/pii.py; src/secretaria/services/pii_pseudonymization.py. Estado, escolha de backend e pseudonimização.
- **S05 · Integrações**: src/secretaria/services/media_storage.py; src/secretaria/services/email.py; src/secretaria/services/precheck.py; src/secretaria/workers/whatsapp/audio.py; src/secretaria/services/payments/. Anexos, e-mail, áudio, PreCheck e sinal.
- **S06 · Contexto operacional**: ../PRODUCTS.md; AGENTS.md; CLAUDE.md; docs/CHECKPOINT_llm_provider_claude.md; docs/CHECKPOINT_sinal_padrao.md. Mapa entre produtos e histórico de decisões. Checkpoints podem descrever estados anteriores ao HEAD atual.

Inventário: 107 operações HTTP explícitas dos routers registrados em main.py; exclui /docs, /redoc e /openapi.json automáticos. Fonte de cada operação em inventario-apis.json.
Graphify STALE (grafo de commit anterior), usado somente para navegação histórica, fatos confirmados no código.
Os checkpoints têm registros históricos de deploy que podem estar defasados. O modelo no código é Haiku 5.5; runtime não inspecionado.
Git preexistente preservado: CLAUDE.md modificado e AGENTS.md não rastreado. Este gerador só cria arquivos nesta pasta de documentação. Sem commit, push, deploy ou alteração de produção.
