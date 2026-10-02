# Lacunas do Portal + secretarIA — teste ao vivo (2026-10-01)

Teste como paciente real no Portal de produção (clínica "Chrysostomo For Eyes", link `?convite=…&produto=secretaria`),
com o worker já rodando `llm_turn_*` / `turn_*` (CHECKPOINT_llm_observabilidade_rede_de_seguranca.md).
Duas jornadas: (A) conta existente (e-mail já cadastrado, conversa antiga longa); (B) visitante novo
(`+qa1`, conversa curta). Nada foi alterado em código nesta rodada — só observação.

## O que a rede de segurança cobre no Portal (verificado no código)

`process_brain_message_inbound` e `process_brain_message_open` chamam `_send_bot_reply`, que agora é o wrapper
com o contador de envios (`BrainMessageSender._record` conta cada bolha). Portanto o Portal TEM a rede.
Não disparou em produção nesta rodada porque nenhum turno falhou (nenhum `turn_failed`/`turn_unanswered`
visto) — **ela ainda não foi exercitada ao vivo**, só por teste unitário.

O que ela NÃO cobre (e foi justamente onde o paciente ficou sem resposta visível):
1. Resposta enviada e gravada, mas **não exibida** pela tela (L1 abaixo).
2. Falha ANTES do worker (brain-api/secretaria_api não enfileirar): o front mostra a bolha do paciente e
   o POST responde `{"status":"queued"}` mesmo assim; nenhum worker roda, logo nenhuma rede roda. Não provado
   nesta rodada, é lacuna estrutural.
3. Tenant sem entitlement (silêncio por desenho) e sender sem credencial.
4. Latência: nenhum "digitando…"; respostas de LLM levaram de ~5 s a ~45 s sem sinal visual.

## Lacunas encontradas

### L1 — Conversa com mais de 50 mensagens congela no Portal  (ALTA, causa raiz provada)
- `GET /internal/brain-message/conversations/{id}/messages` sem `since` devolve as **50 mensagens MAIS ANTIGAS**
  (`order_by created_at` + `limit 50`, `api/internal.py::list_brain_message_messages`). O brain-api rebusca a
  thread inteira a cada poll (os GETs ao secretaria_api não levam `since`; log: `brain_message_messages_listed
  count=50` constante).
- Efeito: a conta com histórico longo vê só o começo da conversa; respostas novas existem no banco
  (`worker_bot_reply_sent`, `brain_message_outbound_recorded`) e **nunca aparecem**; as mensagens que o paciente
  acabou de enviar ficam só no estado local do navegador e **somem ao recarregar**. É exatamente o relato
  "algumas mensagens sumirem após recarregar" e "ficou sem resposta".
- Correção sugerida: primeira carga = as N MAIS RECENTES (ordem cronológica) + paginação para trás
  (`before`); e o brain-api/front usarem `since` no poll.

### L2 — Duas conversas para o mesmo paciente após verificar o código  (ALTA)
- A visita (`pending`) conversa na conversa A; ao verificar o código a tela passa para a conversa B da conta
  (histórico antigo). Log: o menu de "conta verificada" foi renderizado na conversa A
  (`conversation_menu_rendered source=verified_account`), mas a tela mostra a B. As mensagens do e-mail/código
  ficam na A e "somem" da vista; o paciente nunca vê o menu.
- Em conta existente, o paciente cai numa conversa antiga parada num passo qualquer (ex.: "Não sei" de
  serviço de 17/set), sem nenhuma mensagem de boas-vindas/retomada.

### L3 — A LLM promete o que não pode fazer  (MÉDIA)
- "Quer que eu **consulte o valor** para particular ou para qual convênio?" (não há ferramenta de preço).
- "Diga sua cidade que eu te trago o **link da previsão**" (fora de assunto + capacidade inexistente).
- "Quer que eu **deixe isso pronto para você mais tarde**?" (após urgência; sem ferramenta de lembrete).
- O prompt tem a regra 5 (não afirmar ação sem ferramenta) mas não proíbe PROMETER capacidade inexistente.

### L4 — Perguntas factuais comuns não têm onde buscar resposta  (MÉDIA)
- "Qual o endereço e tem estacionamento?" → a LLM devolveu o menu genérico ("Como posso te ajudar?"), sem
  responder nem dizer que não sabe. A clínica não tem endereço/estacionamento/preparo estruturados no contexto
  (já previsto em plano; sem eles a LLM não tem o que dizer).
- Preço: o catálogo tem preço (ex.: "Cirurgia de Catarata R$120") mas a resposta de "quanto custa uma consulta"
  não o usou e citou "Consulta (40 minutos)" sem valor.

### L5 — Vazamento de rótulo de botão como se fosse serviço/opção  (BAIXA)
- "Qual serviço prefere: Cirurgia de Catarata ou **'Não sei'**?" e "(opções: Cirurgia de Catarata, Não sei)":
  a LLM trata o botão "Não sei" como um serviço. Também oferece Catarata a quem veio por dor/vista cansada.

### L6 — Sem limite de entrada no Portal  (MÉDIA, abuso/custo)
- 8 mensagens em sequência rápida → 8 turnos processados, nenhum limitado. O limitador
  (`_is_rate_limited`) só existe no webhook do WhatsApp. Cada mensagem pode custar uma chamada ao modelo.
  A apologia da rede de segurança tem teto (3/10 min), mas o fluxo normal não.

### L7 — Sem indicação de "digitando"/progresso  (BAIXA)
- Entre o envio e a resposta da LLM a tela fica parada (5–45 s observados); o paciente tende a reenviar.

### Comportamentos que funcionaram (não regredir)
- Urgência ("dor forte no olho"): orienta pronto-socorro/192 de imediato.
- Visitante novo: e-mail → nome → LGPD → menu; texto digitado no portão de LGPD reapresenta o aviso.
- "Não sei" no seletor de médico: resposta imediata (determinística); `menu` volta ao início.
- Fluxo guiado "pra mim / convênio / médico" com listas e botões responde instantaneamente.
- Traço `llm_turn_trace verdict=ok` nos turnos de teste; nenhum `truncated` observado.

## Pendências de decisão (do dono)
1. Aceitar corrigir L1 no secretaria_api + brain-api + front (cross-repo, deploy: API → brain-api → front)?
2. L2: ao verificar o código, a conversa da visita deve ser fundida/redirecionada para a da conta, ou a conta
   deve abrir a conversa onde o menu foi renderizado?
3. Limite de entrada do Portal (L6): onde (brain-api por paciente/min?) e qual teto.
4. Prompt: proibir prometer capacidade inexistente (L3/L5) e dar à LLM os fatos que faltam (L4).
