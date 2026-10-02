# "digitando…" no PreCheck (Portal) — Plan (CONDICIONADO)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A aba PreCheck do Portal também mostra "PreCheck está digitando…" enquanto o condutor Python do PreCheck prepara a próxima pergunta.

**Status: dentro do escopo (decisão do dono, 2026-10-01: o indicador é universal, "igual WhatsApp", e "se tiver outro produto tem que funcionar para ele também"), mas começa pela Task 0.** O dono também decidiu que o PreCheck NÃO precisa processar "paciente digitando": só a automação mexe na conversa, então ninguém lê essa interação ali — por isso o PreCheck NÃO entra em `TYPING_PRODUCTS` do brain-api e responde `applied: false` sem rede. O que falta aqui é só a AUTOMAÇÃO do PreCheck digitando. Ressalvas que continuam valendo: `PreCheck/CLAUDE.md` e a memória `feedback-precheck-whatsapp-producao-intocavel` mandam manter qualquer mudança mínima e avisar ANTES de tocar nos caminhos de produção; e este plano foi escrito sem explorar o código do PreCheck (só a secretarIA, o brain-api e o front foram lidos). A Task 0 é o que falta para virar um plano executável: ela só é feita quando o plano de backend da secretarIA (contrato) já estiver no ar.

**Architecture (proposta):** O mesmo contrato do plano `2026-10-01-digitando-backend.md`: o corpo de `GET /internal/brain-message/sessions/{session_ref}/messages` do PreCheck ganha `typing: bool` e `typing_by: "automation" | null` (aditivos, default `false`/`null`; `accepts_typing` fica `false` — nenhum humano conduz o PreCheck). O condutor Python do Portal (`PreCheck/app/services/brain_message/conductor.py`, só Portal; nenhum nó do n8n o chama) marca a sessão como "processando" no começo do turno e limpa no fim, com TTL de 90 s. O brain-api repassa o corpo sem mudar (`RelayOut` aceita campos extras) e o front já lê os campos (`parseTyping`, plano do front, com o nome e a voz do PreCheck em `lib/typing.ts`).

**Tech Stack:** PreCheck: Python/FastAPI + persistência própria; testes `pytest` (exige `pythonpath = .` no `pytest.ini`).

**Spec:** Pedido do dono (2026-10-01): "digitando… tanto para o paciente quanto para os nossos produtos". Contrato em `2026-10-01-digitando-backend.md`; front em `2026-10-01-digitando-frontend.md`.

## Global Constraints

- NUNCA tocar os caminhos do WhatsApp em produção do PreCheck (regra crítica de `PreCheck/CLAUDE.md`); só `app/services/brain_message/` e a rota `/internal/brain-message/*`.
- Mudança mínima e aditiva; nenhuma chamada nova ao n8n; sem migração se a marca puder viver em memória/Redis com TTL (decidir na Task 0).
- Falha aberta: erro na marca nunca falha o turno; `typing=false` quando não souber.
- Push/deploy só com pedido explícito do dono, a cada ocasião.

## Review Focus

- A marca nunca fica presa (TTL ≤ 90 s, limpa em exceção).
- Sessão de outro paciente/clínica nunca recebe `typing=true`.
- O ramo WhatsApp do PreCheck não muda um byte.

---

### Task 0: Explorar e confirmar (obrigatória antes de qualquer código)

- [ ] **Step 1:** Ler `PreCheck/CLAUDE.md` e confirmar que a mudança planejada toca SÓ `app/services/brain_message/` e a rota `/internal/brain-message/sessions/*/messages` (nada do WhatsApp, nada do n8n). Se qualquer passo exigir tocar um caminho protegido, parar e avisar o dono antes.
- [ ] **Step 2:** Ler `PreCheck/CLAUDE.md`, `PreCheck/app/services/brain_message/{store.py,agent_client.py,conductor.py}` e a rota `GET /internal/brain-message/sessions/{session_ref}/messages`; registrar: (a) onde o turno começa e termina; (b) se existe Redis/cache no PreCheck (senão usar um dicionário em memória com expiração — o PreCheck roda um processo por réplica; confirmar quantas); (c) o schema de resposta e o teste que o cobre.
- [ ] **Step 3:** Reescrever as Tasks 1-3 abaixo com código real (mesmo molde das Tasks 1-3 do plano do backend da secretarIA: serviço de marca com `mark/clear/is` fail-open → ligar/desligar no turno → campo `typing` na listagem), commitando o plano atualizado antes de executar.

### Task 1 (rascunho a refinar na Task 0): serviço de marca no condutor
### Task 2 (rascunho): ligar/desligar em volta do turno do condutor (incluindo exceção)
### Task 3 (rascunho): `typing` na listagem + teste de escopo por sessão + documentação em `brain-api/docs/PORTAL_MESSAGING_API.md`
