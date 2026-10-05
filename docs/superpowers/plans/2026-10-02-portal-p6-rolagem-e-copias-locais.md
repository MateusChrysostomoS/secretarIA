# Portal: rolar pela última mensagem e podar cópias locais (P6) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Numa conversa do Portal com mais de 50 mensagens, a resposta nova (da secretarIA, do PreCheck ou da clínica) entra na vista sem o paciente rolar à mão, sem puxar para o fim quem está lendo mensagens antigas, e a cópia local de uma mensagem enviada nunca reaparece como bolha órfã no topo.

**Architecture:** Só o front muda (repo `Brain-Message-Frontend`). (1) A decisão de rolar sai do componente para um módulo puro, `lib/thread-scroll.ts`: a tela segue o FIM da conversa pelo `id` da última mensagem (não pela contagem, que fica em 50 quando a janela do servidor desliza) e só quando o paciente estava no fim, acabou de agir (enviou/tocou num cartão), trocou de conversa ou é a primeira exibição. (2) O poll da secretarIA passa a podar `local` com `unconfirmedLocal`, como o PreCheck já faz, numa função pura `afterSecretariaPoll`. (3) A demo `/componentes/portal/` ganha o cenário `?janela=1` (conversa de 60 mensagens que serve só as 50 mais novas e cresce) para reproduzir o defeito e provar a correção no navegador antes de qualquer deploy.

**Tech Stack:** Next.js 15 (export estático), React 19, TypeScript, CSS à mão, vitest (ambiente `node`; sem jsdom nem Testing Library — de propósito, ver `vitest.config.ts`).

**Spec:** não há spec próprio; este plano é a forma-plano de `z_prompts/PROMPT_PORTAL_SCROLL_E_COPIAS_LOCAIS_JANELA_RECENTE.md` (que ele substitui) e a P6 da §9 de `docs/superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md`. Origem do defeito, causa e contrato do servidor: `secretarIA/docs/CHECKPOINT_portal_mensagens_recentes.md` (seção "Dependência do front", §7 e §9). Leia a seção "Dependência do front" antes de começar.

**Avaliação (formato de `AI_WORKFLOW.md`):** Complexity: LOW–MEDIUM · Risk: MEDIUM (é a tela viva do paciente, mas sem contrato novo) · Repositories: Brain-Message-Frontend · Graphify: Brain-Message-Frontend STALE benigno (não usado) · 1 Implementer por task (sequenciais, mesmos arquivos) + 1 Reviewer fresco no fim · Parallelizable: NO.

## Global Constraints

- **Worktree novo obrigatório:** executar em `C:\TECH\BRAIN-worktrees\TASK-0NN\Brain-Message-Frontend`, branch `task/TASK-0NN-portal-rolagem` a partir de `main`. `TASK-0NN` = o próximo número livre (confira `C:\TECH\BRAIN\tasks\` e `C:\TECH\BRAIN-worktrees\`; em 2026-10-02 o último usado é `TASK-030`). Substitua `TASK-0NN` pelo número real em todo comando e texto abaixo. Nunca no checkout principal `C:\TECH\BRAIN\Brain-Message-Frontend`; nunca `git add -A` (liste os arquivos).
- **Portões do front (PowerShell):** `.\node_modules\.bin\tsc.cmd --noEmit`, `npm test`, `npm run build`. **NUNCA** `npm run lint` (ESLint não está instalado; trava num prompt interativo) e **nunca** `npx tsc` (pacote errado nesta máquina). `next dev`/`next build` só com o caminho começando em `C:` MAIÚSCULO.
- **Testes sem jsdom:** o vitest daqui roda em `node`, sem jsdom, sem Testing Library, sem `react-test-renderer`, e `renderToStaticMarkup` não executa efeitos nem handlers. Por isso a regra de rolagem é função pura sobre números e ids (testada no node) e o componente só a liga. Um teste de rolagem **não depende de layout**: a caixa de rolagem é fingida com três números (`scrollTop`, `scrollHeight`, `clientHeight` — num jsdom os três seriam 0 porque ele não faz layout) e o `scrollIntoView` é fingido por um contador injetado (`createTailScroller(scrollToBottom)`). Não instale jsdom nem Testing Library: mudar a infraestrutura de testes da família não é escopo daqui.
- **Rolagem instantânea, nunca suave:** o `scrollIntoView({ block: "end" })` existente fica como está (sem `behavior`). Nenhuma folha de estilo liga `scroll-behavior: smooth`. Logo não há movimento para `prefers-reduced-motion` cancelar (ver Review Focus).
- Texto de UI em português; código e comentários em inglês. Esta mudança **não cria texto de UI novo** (a demo só tem dados fictícios em português).
- Sem dependência nova, sem `fetch` novo em componente, sem mudança em contrato (secretarIA e brain-api não são tocados), sem mexer em `components/console/Thread.tsx` (o console lê a lista do hub, que não tem janela).
- **PreCheck:** o ramo do PreCheck (`afterPrecheckPoll`, `afterPrecheckPost`) e `deliver` ficam byte a byte iguais.
- Os arquivos do working tree são CRLF (`core.autocrlf=true`; `.gitattributes` fixa LF no repo). Ao editar, preserve o fim de linha do arquivo e confira com `git diff --stat` que só as linhas tocadas aparecem. Crie e edite arquivos com as ferramentas de edição, não com heredoc de shell (o Bash trunca heredoc grande).
- Commit local permitido; **push e deploy só com pedido explícito do dono**. Todo commit termina com `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Skills: `front-brain` (substitui a skill genérica `frontend` neste repo) e `superpowers:test-driven-development` (RED visto antes de cada GREEN).

## Review Focus

Entradas e condições que o plano exercita além do caminho feliz, da mais provável para a menos. Cada linha aponta a task cujo teste a fixa.

- **Resposta chega enquanto o paciente está rolado para cima lendo o histórico:** a tela NÃO se move; quando ele volta ao fim, a próxima resposta volta a ser seguida. → Task 3 (cenário "leitor de histórico") e Task 5 (prova no navegador).
- **Duas respostas seguidas:** no mesmo poll vira uma rolagem só; em polls separados cada uma é seguida, exceto se o paciente saiu do fim entre elas. → Task 3.
- **O próprio envio do paciente (e o toque num cartão):** sempre traz a bolha dele para a vista, mesmo se ele estava lendo o histórico; e a troca da cópia local pela do servidor logo depois não puxa quem voltou a ler. → Task 3 (regra) e Task 4 (ligação).
- **Conversa com menos de 50 mensagens:** comportamento de hoje — abre no fim e cada mensagem nova é seguida. → Task 3 (cenário "conversa curta") e o teste de contagem constante (50) da janela.
- **Poll que re-renderiza com dados idênticos:** nenhuma rolagem (mesmo `id` da última) e nenhuma mudança no estado podado (`afterSecretariaPoll` idempotente). → Task 2 e Task 3.
- **Aba do PreCheck (ramo diferente):** `afterPrecheckPoll`/`afterPrecheckPost` intactos (testes existentes continuam verdes); a lista é a MESMA para as duas abas, então trocar de aba abre a outra conversa no fim, mesmo se a primeira estava sendo lida (`threadKey`). → Task 3, Task 4 e Task 5.
- **`prefers-reduced-motion`:** a rolagem é instantânea, não há movimento a reduzir; um teste fixa que nenhuma folha liga `scroll-behavior: smooth` e que a lista não pede `behavior: "smooth"`. → Task 4.
- **Cópia local de uma mensagem entregue cujo par saiu da janela (bolha órfã):** não reaparece; cópias `enviando` e `falhou` NUNCA são podadas (o "tentar de novo" mora nelas); uma entregue que o servidor ainda não devolveu fica. → Task 2 e Task 5 (`/rajada`).
- **Duas mensagens idênticas seguidas ("sim", "sim"):** depois do poll que traz as duas, nenhuma se perde nem se duplica. Há um transitório inerente ao pareamento por texto de `unconfirmedLocal` (uma só ida e volta, só se as duas saírem em menos de 5 s e um poll periódico cruzar o segundo envio; o refresh do próprio envio chega logo depois); a poda não o cria, só o torna visível nesse caso. Aceito e registrado. → Task 2.

## File Structure

- Create `lib/thread-scroll.ts` — a regra pura de "quando seguir o fim da conversa" (`isNearBottom`, `tailKey`, `shouldScrollToBottom`, `createTailScroller`).
- Create `lib/__tests__/thread-scroll.test.ts`.
- Modify `lib/patient-portal.ts` — `afterSecretariaPoll`, irmã de `afterPrecheckPoll`.
- Modify `lib/__tests__/patient-portal.test.ts`.
- Modify `components/patient/PatientMessageList.tsx` — troca o efeito de `[count, ready]` pelo `scroller`; props `threadKey` e `scrollRequest`; `onScroll` na caixa.
- Create `components/patient/__tests__/message-list-scroll.test.tsx` — fumaça de renderização + guardas de ligação e de "rolagem instantânea".
- Modify `components/portal/PortalConversation.tsx` — o ramo da secretarIA de `refresh` usa `afterSecretariaPoll`; contador `ownSends`; `threadKey`/`scrollRequest` na lista; comentário de cabeçalho.
- Modify `lib/mock/portal-demo.ts`, `app/componentes/portal/PortalDemo.tsx`, `lib/__tests__/portal-demo.test.ts` — cenário `?janela=1`.
- Create `docs/CHECKPOINT_portal_scroll_janela_recente.md`; modify `CLAUDE.md` (uma linha de ponteiro).

## Antes de começar (uma vez)

- [ ] **Passo 1: conferir o estado do repo principal (somente leitura)**

Run: `git -C C:\TECH\BRAIN\Brain-Message-Frontend status --short`
Expected: vazio, `main` em `0c0d2dd` (conferido em 2026-10-02: nada não commitado; a nota de `PRODUCTS.md` sobre "trabalho de feature uncommitted" está velha). Se aparecer algo, **não toque**: é trabalho de outra sessão — o worktree abaixo parte de `main` e não o leva.

- [ ] **Passo 2: criar o worktree e instalar as dependências**

```powershell
git -C C:\TECH\BRAIN\Brain-Message-Frontend worktree add C:\TECH\BRAIN-worktrees\TASK-0NN\Brain-Message-Frontend -b task/TASK-0NN-portal-rolagem main
cd C:\TECH\BRAIN-worktrees\TASK-0NN\Brain-Message-Frontend
npm ci
```

(`node_modules` é ignorado pelo git, um worktree novo nasce sem ele.)

- [ ] **Passo 3: linha de base**

Run: `.\node_modules\.bin\tsc.cmd --noEmit` e `npm test`
Expected: `tsc` sem saída; vitest todo verde. Anote o total de testes que passam (`B`). Se algo já estiver vermelho aqui, pare e reporte: é falha pré-existente, não regressão deste plano.

---

### Task 1: Demo `?janela=1` — conversa que serve só as 50 mais novas, e a prova do defeito no navegador

Isto vem primeiro de propósito: é o banco de provas. Sem ele não dá para ver o defeito (RED no navegador) nem a correção (GREEN) sem produção.

**Files:**
- Modify: `lib/mock/portal-demo.ts` (tipo `PortalDemoScenario`, `parsePortalDemoScenario`, `answer`, `installPortalDemo`, e funções novas)
- Modify: `app/componentes/portal/PortalDemo.tsx`
- Test: `lib/__tests__/portal-demo.test.ts`

**Interfaces:**
- Produces:
  - `PortalDemoScenario.slidingWindow?: true`.
  - `parsePortalDemoScenario(clinics: string | null, onda1?: string | null, sliding?: string | null): PortalDemoScenario` — `slidingWindow: true` só para `sliding === "1"`; os outros cenários ficam byte a byte como eram (a chave nem existe).
  - Com `slidingWindow`, a thread da secretarIA da "Clínica Vida Leve" tem 60 linhas (`demo-vida-leve-w-1` … `-w-60`, alternando paciente e bot, `Pergunta n`/`Resposta n`) e o GET devolve `{ data: <as 50 últimas>, has_more: true }`. Um POST de texto grava a linha do paciente na hora e uma resposta `Resposta automática: <texto>` 2 s depois (a janela desliza, contagem continua 50). O texto `/rajada` faz chegarem 55 respostas `Rajada 1…55` de uma vez, 1,5 s depois (empurra a mensagem do próprio paciente para fora da janela com a aba aberta).
- Consumes: `MessageWire` (`lib/real/mappers.ts`), `VIDA_LEVE`, `MINUTE`, `iso` (já no arquivo).

- [ ] **Step 1: Write the failing tests**

Em `lib/__tests__/portal-demo.test.ts`, trocar a linha de import do vitest por:

```ts
import { afterEach, describe, expect, it, vi } from "vitest";
```

e acrescentar ao FIM do arquivo:

```ts
describe("parsePortalDemoScenario — ?janela=1", () => {
  it("turns the sliding-window thread on only for an explicit 1 and leaves every other scenario as it was", () => {
    expect(parsePortalDemoScenario("1", null, "1")).toEqual({ clinics: 1, slidingWindow: true });
    expect(parsePortalDemoScenario("1", null, "0")).toEqual({ clinics: 1 });
    expect(parsePortalDemoScenario("1", null, null)).toEqual({ clinics: 1 });
    expect(parsePortalDemoScenario(null)).toEqual({ clinics: 2 });
  });
});

// The production conversation that exposed the Portal's scroll (2026-10-02): the server answers with the
// newest 50 messages, so a reply makes the window SLIDE — the count stays 50 and the oldest message leaves.
describe("the sliding-window thread (?janela=1), through the real client", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  // Opens the demo with the scenario on and returns a poll of the secretarIA thread, already mapped.
  async function openSliding() {
    const scenario = { clinics: 1, slidingWindow: true } as const;
    uninstall = installPortalDemo(scenario);
    await signInPortalDemo(scenario);
    return async () =>
      fromTranscript("secretaria", (await pollMessages(vidaLeve.tenantId, "secretaria")).payload, portalConversationId("secretaria"));
  }

  it("serves the newest 50 of a 60-message thread", async () => {
    const poll = await openSliding();
    const first = await poll();
    expect(first).toHaveLength(50);
    expect(first[0].id).toBe("demo-vida-leve-w-11");
    expect(first.at(-1)?.id).toBe("demo-vida-leve-w-60");
  });

  it("a send and then its reply slide the window: the count stays 50, the last message changes", async () => {
    const poll = await openSliding();
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });

    await sendPatientMessage(vidaLeve.tenantId, "secretaria", "Qual o endereço?");
    const sent = await poll();
    expect(sent).toHaveLength(50);
    expect(sent.at(-1)?.text).toBe("Qual o endereço?");

    await vi.advanceTimersByTimeAsync(2_000);
    const answered = await poll();
    expect(answered).toHaveLength(50);
    expect(answered.at(-1)?.text).toBe("Resposta automática: Qual o endereço?");
    expect(answered[0].id).toBe("demo-vida-leve-w-13");
  });

  it("/rajada pushes the patient's own message out of the window", async () => {
    const poll = await openSliding();
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });

    await sendPatientMessage(vidaLeve.tenantId, "secretaria", "/rajada");
    expect((await poll()).at(-1)?.text).toBe("/rajada");

    await vi.advanceTimersByTimeAsync(1_500);
    const burst = await poll();
    expect(burst).toHaveLength(50);
    expect(burst.every((m) => m.sender === "secretaria")).toBe(true);
    expect(burst.some((m) => m.text === "/rajada")).toBe(false);
    expect(burst.at(-1)?.text).toBe("Rajada 55");
  });

  it("leaves the ordinary demo as it was: six rows, and a send adds nothing", async () => {
    const scenario = { clinics: 1 } as const;
    uninstall = installPortalDemo(scenario);
    await signInPortalDemo(scenario);
    const poll = async () =>
      fromTranscript("secretaria", (await pollMessages(vidaLeve.tenantId, "secretaria")).payload, portalConversationId("secretaria"));

    expect(await poll()).toHaveLength(6);
    await sendPatientMessage(vidaLeve.tenantId, "secretaria", "Oi");
    expect(await poll()).toHaveLength(6);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm test -- lib/__tests__/portal-demo.test.ts`
Expected: FAIL — o `parsePortalDemoScenario("1", null, "1")` volta `{ clinics: 1 }` e os testes da thread deslizante falham (`expected 6 to have a length of 50`). Os testes antigos do arquivo continuam verdes.

- [ ] **Step 3: Implement the scenario**

Em `lib/mock/portal-demo.ts`:

(a) No tipo `PortalDemoScenario`, depois do campo `onda1`, acrescentar:

```ts
  // The Vida Leve secretarIA thread is LONGER than the server's window: 60 rows, of which the newest 50
  // are served (as brain-api relays secretarIA's list since TASK-028). A send is stored at once and a
  // reply follows 2 s later, so the window SLIDES with the count unchanged — the shape of the
  // production conversation that froze the Portal's scroll (2026-10-02). Sending "/rajada" makes 55
  // replies land together, pushing the patient's own message out of the window while the tab is open.
  // `?janela=1`.
  slidingWindow?: true;
```

(b) Trocar `parsePortalDemoScenario` por:

```ts
// `?clinicas=0|1|2|3`, an optional synthetic `?onda1=` phase and `?janela=1`.
export function parsePortalDemoScenario(
  clinics: string | null, onda1?: string | null, sliding?: string | null,
): PortalDemoScenario {
  const phase = ["opening", "consent", "pending", "terminal", "new", "legacy"].includes(onda1 ?? "")
    ? onda1 as PortalDemoScenario["onda1"] : undefined;
  const count = clinics === "0" ? 0 : clinics === "1" ? 1 : clinics === "3" ? 3 : 2;
  return {
    clinics: count,
    ...(phase ? { onda1: phase } : {}),
    ...(sliding === "1" ? { slidingWindow: true as const } : {}),
  };
}
```

(c) Logo depois da função `secretariaRows` (antes de `precheckTranscript`), acrescentar:

```ts
// ── The long thread behind `?janela=1` ──────────────────────────────────────

const SLIDING_WINDOW = 50;
const SLIDING_BURST = "/rajada";

type SlidingThread = { rows: MessageWire[]; next: number };

function slidingRow(thread: SlidingThread, sender: "patient" | "bot", body: string, createdAt: number): MessageWire {
  const n = thread.next++;
  return {
    id: `${VIDA_LEVE.token}-w-${n}`,
    direction: sender === "patient" ? "inbound" : "outbound",
    sender,
    body,
    created_at: iso(createdAt),
  };
}

// 60 rows ending a minute before `t0`, alternating patient and bot: more than the window, so even the
// first poll already leaves the oldest ten out.
function slidingThread(t0: number): SlidingThread {
  const thread: SlidingThread = { rows: [], next: 1 };
  for (let n = 1; n <= 60; n += 1) {
    const patient = n % 2 === 1;
    thread.rows.push(
      slidingRow(thread, patient ? "patient" : "bot", patient ? `Pergunta ${n}` : `Resposta ${n}`, t0 - (61 - n) * MINUTE),
    );
  }
  return thread;
}

// What the patient sends is stored at once; the "AI" answers a poll later, into a thread that is already
// full. "/rajada" is the other production case: 55 replies together.
function growSlidingThread(thread: SlidingThread, text: string): void {
  thread.rows.push(slidingRow(thread, "patient", text, Date.now()));
  const burst = text === SLIDING_BURST;
  setTimeout(() => {
    const at = Date.now();
    const replies = burst ? 55 : 1;
    for (let i = 0; i < replies; i += 1) {
      thread.rows.push(
        slidingRow(thread, "bot", burst ? `Rajada ${i + 1}` : `Resposta automática: ${text}`, at + i),
      );
    }
  }, burst ? 1_500 : 2_000);
}
```

(d) Em `answer(...)`, acrescentar um sexto parâmetro e usá-lo. Trocar a assinatura:

```ts
function answer(
  scenario: PortalDemoScenario, path: string, init: RequestInit, t0: number,
  qa: { phase: Onda1Phase | null },
  sliding: SlidingThread | null,
): Response {
```

Trocar o bloco do GET:

```ts
    const payload = product === "precheck"
      ? (clinic === VIDA_LEVE && qa.phase ? onda1Transcript(qa.phase) : precheckTranscript(t0))
      : { data: secretariaRows(clinic, t0) };
```

por:

```ts
    const payload = product === "precheck"
      ? (clinic === VIDA_LEVE && qa.phase ? onda1Transcript(qa.phase) : precheckTranscript(t0))
      : sliding && clinic === VIDA_LEVE
        ? { data: sliding.rows.slice(-SLIDING_WINDOW), has_more: sliding.rows.length > SLIDING_WINDOW }
        : { data: secretariaRows(clinic, t0) };
```

E, logo antes do comentário `// A send is accepted and nothing answers back: the page keeps what the patient sent.`, inserir:

```ts
  if (product === "secretaria" && clinic === VIDA_LEVE && sliding) {
    const body = typeof init.body === "string" ? JSON.parse(init.body) as { text?: string } : {};
    growSlidingThread(sliding, (body.text ?? "").trim());
    return json(200, { product, payload: { status: "queued" }, at });
  }
```

(e) Em `installPortalDemo`, depois de `const qa = ...`, acrescentar `const sliding = scenario.slidingWindow ? slidingThread(t0) : null;` e passar `sliding` ao `answer`:

```ts
    return answer(scenario, input.slice(base.length - 1), init ?? {}, t0, qa, sliding);
```

Em `app/componentes/portal/PortalDemo.tsx`, trocar a leitura do cenário e o efeito:

```tsx
  const { clinics, onda1, slidingWindow } = parsePortalDemoScenario(
    params.get("clinicas"), params.get("onda1"), params.get("janela"),
  );
```

```tsx
    const scenario = { clinics, onda1, slidingWindow };
```

e a lista de dependências do efeito vira `[clinics, onda1, slidingWindow]`.

- [ ] **Step 4: Run to verify it passes**

Run: `npm test -- lib/__tests__/portal-demo.test.ts` e `.\node_modules\.bin\tsc.cmd --noEmit`
Expected: PASS (os 5 testes novos mais os antigos); `tsc` sem saída.

- [ ] **Step 5: Reproduzir o defeito no navegador (RED de verdade, no código ainda não corrigido)**

Em outro terminal (ou em background), a partir do worktree com `C:` maiúsculo: `npm run dev`. Espere `Ready` e abra `http://localhost:3000/componentes/portal/?clinicas=1&janela=1` com o agent-browser (skill `agent-browser`) ou o Chrome DevTools MCP, numa janela larga (≥ 1100 px). Clique em "Clínica Vida Leve" na lista. A sonda abaixo, rodada com `evaluate_script`, é a mesma de todo o plano:

```js
() => {
  const box = document.querySelector(".patient-panel .thread-scroll");
  const rows = [...box.querySelectorAll(".msg")];
  const last = rows[rows.length - 1];
  const b = box.getBoundingClientRect();
  const r = last.getBoundingClientRect();
  return {
    bubbles: rows.length,
    gapPx: Math.round(box.scrollHeight - box.scrollTop - box.clientHeight),
    scrollTop: Math.round(box.scrollTop),
    lastText: last.textContent.trim().slice(0, 60),
    lastInView: r.top >= b.top - 1 && r.bottom <= b.bottom + 1,
    orphan: rows.some((m) => m.textContent.includes("/rajada")),
  };
}
```

Roteiro (envio: digite no campo "Escrever mensagem para a clínica" e tecle Enter):

1. Aberta a conversa: sonda → `bubbles` 50, `lastText` começando em `Resposta 60`, `gapPx` ≤ 2, `lastInView` true.
2. Envie `Qual o endereço?`, espere 8 s, sonda. **Esperado AGORA (defeito):** `lastText` = `Resposta automática: Qual o endereço?` mas `lastInView` **false** e `gapPx` > 0 (a resposta está abaixo da dobra sem ninguém rolar).
3. Envie `/rajada`, espere 8 s, sonda. **Esperado AGORA (defeito):** `orphan` **true** (a bolha `/rajada` reaparece no topo).

Anote os valores medidos (vão para o CHECKPOINT na Task 5). Pare o `npm run dev`.

- [ ] **Step 6: Commit**

```powershell
git add lib/mock/portal-demo.ts app/componentes/portal/PortalDemo.tsx lib/__tests__/portal-demo.test.ts
git commit -m "test(portal): demo scenario with a thread longer than the server window (?janela=1)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: O poll da secretarIA poda as cópias locais

**Files:**
- Modify: `lib/patient-portal.ts` (nova função logo depois de `afterPrecheckPoll`)
- Modify: `components/portal/PortalConversation.tsx` (import, ramo da secretarIA em `refresh`, comentário de cabeçalho)
- Test: `lib/__tests__/patient-portal.test.ts`

**Interfaces:**
- Consumes: `unconfirmedLocal(server, local, product)` e `PortalThreadState` (já em `lib/patient-portal.ts`).
- Produces: `export function afterSecretariaPoll(prior: PortalThreadState | undefined, server: BubbleMessage[]): PortalThreadState` → `{ server, local: unconfirmedLocal(server, prior?.local ?? [], "secretaria"), loaded: true, sessionId: null }`.

Decisão registrada: poda por confirmação (`unconfirmedLocal`), como o PreCheck, em vez de "podar o que ficou mais velho que a janela". A alternativa evitaria o transitório de mensagens idênticas descrito no Review Focus, mas depende de o servidor sempre devolver a janela das mais novas (quebraria se o Portal um dia usar `since`) e divergiria do PreCheck; é a recomendação do CHECKPOINT da TASK-028.

- [ ] **Step 1: Write the failing tests**

Em `lib/__tests__/patient-portal.test.ts`: na lista de import de `"../patient-portal"` acrescentar `afterSecretariaPoll,` (depois de `afterPrecheckPost,`) e `type PortalThreadState,` (depois de `portalStatusLabel,`); e abaixo dos imports existentes acrescentar:

```ts
import type { MessageWire } from "../real/mappers";
```

Depois acrescentar ao FIM do arquivo:

```ts
// The secretarIA poll prunes the page's own copies. The server answers with a WINDOW — its newest 50
// messages — so a delivered send's server copy can slide out of it while the tab stays open; a local
// copy kept in memory would then resurface at the top as an orphan bubble.
describe("afterSecretariaPoll", () => {
  const BASE = Date.parse("2026-10-02T12:00:00Z");
  const at = (seconds: number) => new Date(BASE + seconds * 1000).toISOString();
  const row = (n: number, sender: "patient" | "bot", body: string): MessageWire => ({
    id: `m${n}`, direction: sender === "patient" ? "inbound" : "outbound", sender, body, created_at: at(n),
  });
  // Filler conversation: rows from..to, alternating voices, bodies "linha n" (never equal to a test's text).
  const talk = (from: number, to: number): MessageWire[] =>
    Array.from({ length: to - from + 1 }, (_, i) => row(from + i, (from + i) % 2 === 0 ? "patient" : "bot", `linha ${from + i}`));
  // What the server answers: the newest 50 rows, oldest first.
  const windowOf = (rows: MessageWire[]): BubbleMessage[] => fromSecretariaTranscript({ data: rows.slice(-50) }, SEC);
  const state = (server: BubbleMessage[], local: BubbleMessage[]): PortalThreadState =>
    ({ server, local, loaded: true, sessionId: null });

  it("drops a delivered send once the server has it, so it cannot come back as an orphan when the window slides past it", () => {
    const sent = patient("local-1", "pergunta", at(60), "entregue");
    const confirmed = [...talk(1, 59), row(60, "patient", "pergunta")];

    const polled = afterSecretariaPoll(state(windowOf(talk(1, 59)), [sent]), windowOf(confirmed));
    expect(polled.local).toEqual([]);
    expect(mergeThread(polled.server, polled.local).filter((m) => m.text === "pergunta")).toHaveLength(1);

    // 55 newer messages arrive while the tab stays open: "pergunta" leaves the window.
    const slid = windowOf([...confirmed, ...talk(61, 115)]);
    expect(slid.some((m) => m.text === "pergunta")).toBe(false);
    const later = afterSecretariaPoll(polled, slid);
    expect(mergeThread(later.server, later.local).some((m) => m.text === "pergunta")).toBe(false);

    // Control: this is the orphan the old code produced, because it kept `local` as it was.
    expect(mergeThread(slid, [sent]).some((m) => m.id === "local-1")).toBe(true);
  });

  it("never prunes a send in flight or a failed one (the retry lives on them), however old", () => {
    const inFlight = patient("local-2", "a caminho", at(1), "enviando");
    const failed = patient("local-3", "não foi", at(2), "falhou");
    const done = patient("local-4", "pergunta", at(60), "entregue");
    const server = windowOf([...talk(1, 59), row(60, "patient", "pergunta")]);

    const polled = afterSecretariaPoll(state(windowOf(talk(1, 59)), [inFlight, failed, done]), server);
    expect(polled.local.map((m) => m.id)).toEqual(["local-2", "local-3"]);
  });

  it("keeps a delivered send the server has not returned yet (the poll was taken before it was stored)", () => {
    const sent = patient("local-5", "ainda não chegou", at(61), "entregue");
    const server = windowOf(talk(1, 60));

    const polled = afterSecretariaPoll(state(server, [sent]), server);
    expect(polled.local.map((m) => m.id)).toEqual(["local-5"]);
    expect(mergeThread(polled.server, polled.local).at(-1)?.id).toBe("local-5");
  });

  it("prunes the copy of a card tap the same way: its text is the option's label", () => {
    const tap: BubbleMessage = {
      kind: "message", id: "local-6", conversationId: SEC, sender: "paciente", sentAt: at(60),
      status: "entregue", text: "Sexta, 14h30", echoOf: "m58",
    };
    const server = windowOf([...talk(1, 59), { ...row(60, "patient", "Sexta, 14h30"), interactive_reply_id: "horario-sexta" }]);

    expect(afterSecretariaPoll(state(windowOf(talk(1, 59)), [tap]), server).local).toEqual([]);
  });

  it("a poll with identical data changes nothing: same state, same merged thread", () => {
    const sent = patient("local-7", "ainda não chegou", at(61), "entregue");
    const first = afterSecretariaPoll(state(windowOf(talk(1, 60)), [sent]), windowOf(talk(1, 60)));
    const second = afterSecretariaPoll(first, windowOf(talk(1, 60)));

    expect(second).toEqual(first);
    expect(mergeThread(second.server, second.local).map((m) => m.id))
      .toEqual(mergeThread(first.server, first.local).map((m) => m.id));
  });

  it("two identical sends are each paired with their own stored line: none lost, none doubled", () => {
    const one = patient("local-8", "sim", at(60), "entregue");
    const two = patient("local-9", "sim", at(63), "entregue");
    const server = windowOf([...talk(1, 59), row(60, "patient", "sim"), row(63, "patient", "sim")]);

    const polled = afterSecretariaPoll(state(windowOf(talk(1, 59)), [one, two]), server);
    expect(polled.local).toEqual([]);
    expect(mergeThread(polled.server, polled.local).filter((m) => m.text === "sim")).toHaveLength(2);
  });

  it("with no prior state it starts empty and loaded, with no PreCheck session", () => {
    const server = windowOf(talk(1, 5));
    expect(afterSecretariaPoll(undefined, server)).toEqual({ server, local: [], loaded: true, sessionId: null });
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm test -- lib/__tests__/patient-portal.test.ts`
Expected: FAIL — os 7 testes novos com `TypeError: afterSecretariaPoll is not a function`; os testes antigos do arquivo continuam verdes.

- [ ] **Step 3: Implement**

Em `lib/patient-portal.ts`, logo depois da função `afterPrecheckPoll` (e antes de `afterPrecheckPost`), inserir:

```ts
// The secretarIA thread's poll, the sibling of `afterPrecheckPoll`. The server now answers with a
// WINDOW — its newest 50 messages, not the whole conversation — so a delivered send's server copy can
// slide out of it while the tab stays open. Keeping `local` as it was would then resurface that send at
// the top as an orphan bubble (`mergeThread` only hides a local copy that has a twin in the window), so
// `local` is pruned on EVERY poll: a copy the server confirmed goes (paired one-to-one by
// `unconfirmedLocal`). `enviando` and `falhou` sends are never pruned — the retry lives on them — and a
// delivered one the server has not returned yet stays.
export function afterSecretariaPoll(
  prior: PortalThreadState | undefined,
  server: BubbleMessage[],
): PortalThreadState {
  return {
    server,
    local: unconfirmedLocal(server, prior?.local ?? [], "secretaria"),
    loaded: true,
    sessionId: null,
  };
}
```

Em `components/portal/PortalConversation.tsx`:

(a) Na lista de import de `"@/lib/patient-portal"`, acrescentar `afterSecretariaPoll,` logo depois de `afterPrecheckPost,`.

(b) No `refresh`, trocar:

```ts
          return { ...prev, [id]: product === "precheck"
            ? afterPrecheckPoll(prior, server, sessionId)
            : { server, local: prior?.local ?? [], loaded: true, sessionId: null } };
```

por:

```ts
          return { ...prev, [id]: product === "precheck"
            ? afterPrecheckPoll(prior, server, sessionId)
            : afterSecretariaPoll(prior, server) };
```

(c) No comentário de cabeçalho, trocar o trecho:

```
// on screen is polled, every POLL_MS, and the timer dies with the screen. The
// full transcript is fetched each time and merged with what this page sent
// (lib/patient-portal.ts::mergeThread): a `since` cursor would save bytes but
```

por:

```
// on screen is polled, every POLL_MS, and the timer dies with the screen. The
// newest window of the transcript (secretarIA serves its last 50 messages, so a
// reply makes the window SLIDE) is fetched each time and merged with what this
// page sent (lib/patient-portal.ts::mergeThread), and every poll prunes the
// local copies the server confirmed (afterSecretariaPoll, like PreCheck's): a
// `since` cursor would save bytes but
```

(as linhas seguintes do parágrafo seguem como estão).

- [ ] **Step 4: Run to verify it passes**

Run: `npm test -- lib/__tests__/patient-portal.test.ts` e `.\node_modules\.bin\tsc.cmd --noEmit`
Expected: PASS (7 novos mais os antigos, inclusive toda a suíte de `afterPrecheckPoll`/`afterPrecheckPost`, que não mudou); `tsc` sem saída.

- [ ] **Step 5: Commit**

```powershell
git add lib/patient-portal.ts lib/__tests__/patient-portal.test.ts components/portal/PortalConversation.tsx
git commit -m "fix(portal): prune the local copies of sent messages on every secretarIA poll" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: A regra de rolagem pura — `lib/thread-scroll.ts`

**Files:**
- Create: `lib/thread-scroll.ts`
- Test: `lib/__tests__/thread-scroll.test.ts`

**Interfaces:**
- Produces (todos exportados de `lib/thread-scroll.ts`):
  - `const NEAR_BOTTOM_PX = 120`
  - `type ScrollMetrics = { scrollTop: number; scrollHeight: number; clientHeight: number }` (um `HTMLDivElement` satisfaz)
  - `isNearBottom(metrics: ScrollMetrics, thresholdPx?: number): boolean`
  - `tailKey(messages: readonly { readonly id: string }[] | null): string | null` — o `id` da última, `null` para lista nula ou vazia
  - `type TailSnapshot = { ready: boolean; threadKey: string; tail: string | null; request: number }`
  - `shouldScrollToBottom(prev: TailSnapshot | null, next: TailSnapshot, nearBottom: boolean): boolean`
  - `type TailScroller = { onScroll(metrics: ScrollMetrics): void; update(next: TailSnapshot): void }`
  - `createTailScroller(scrollToBottom: () => void): TailScroller`
- Consumes: nada.

A regra, escrita aqui uma vez (e no comentário do módulo): **segue o fim da conversa quando** (1) a lista pode ser mostrada pela primeira vez (estava carregando ou acabou de montar); (2) passou a mostrar OUTRA conversa (as abas de produto compartilham a mesma lista); (3) o paciente agiu (`request` mudou: enviou, tocou num cartão) — a bolha dele sempre vem para a vista; (4) a última mensagem mudou E o paciente estava no fim (a até `NEAR_BOTTOM_PX` do fim) na última vez que rolou. Caso contrário, fica onde o leitor está. "No fim" é medido a cada evento de rolagem, isto é, ANTES de o conteúdo novo existir: medir depois contaria a altura da própria bolha nova como distância, e uma resposta alta (texto longo chega expandido) pareceria "o paciente rolou para cima".

- [ ] **Step 1: Write the failing tests**

Criar `lib/__tests__/thread-scroll.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import {
  NEAR_BOTTOM_PX,
  createTailScroller,
  isNearBottom,
  shouldScrollToBottom,
  tailKey,
  type ScrollMetrics,
  type TailSnapshot,
} from "../thread-scroll";

// A scroll box as three numbers. jsdom would answer 0 for all three (it does no layout) and this repo's
// vitest runs in node anyway: the rule only reads scrollTop, scrollHeight and clientHeight, so the tests
// hand it those three, and a counter stands in for `scrollIntoView`.
const AT_END: ScrollMetrics = { scrollTop: 1400, scrollHeight: 2000, clientHeight: 600 }; // 0 px from the end
const READING: ScrollMetrics = { scrollTop: 600, scrollHeight: 2000, clientHeight: 600 }; // 800 px above the end

const snap = (over: Partial<TailSnapshot> = {}): TailSnapshot => ({
  ready: true,
  threadKey: "portal:secretaria",
  tail: "m1",
  request: 0,
  ...over,
});

function harness() {
  let scrolls = 0;
  const scroller = createTailScroller(() => {
    scrolls += 1;
  });
  return { scroller, scrolls: () => scrolls };
}

// A window of the conversation as the server serves it: ids m<from>..m<to>.
const ids = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => ({ id: `m${from + i}` }));

describe("isNearBottom", () => {
  it("is true within the threshold of the end, false beyond it", () => {
    expect(isNearBottom(AT_END)).toBe(true);
    expect(isNearBottom({ scrollTop: 2000 - 600 - NEAR_BOTTOM_PX, scrollHeight: 2000, clientHeight: 600 })).toBe(true);
    expect(isNearBottom({ scrollTop: 2000 - 600 - NEAR_BOTTOM_PX - 1, scrollHeight: 2000, clientHeight: 600 })).toBe(false);
    expect(isNearBottom(READING)).toBe(false);
  });

  it("holds for fractional pixels, rubber-band overscroll and a thread that does not fill the box", () => {
    expect(isNearBottom({ scrollTop: 1399.5, scrollHeight: 2000, clientHeight: 600 })).toBe(true);
    expect(isNearBottom({ scrollTop: 1412, scrollHeight: 2000, clientHeight: 600 })).toBe(true);
    expect(isNearBottom({ scrollTop: 0, scrollHeight: 300, clientHeight: 600 })).toBe(true);
  });
});

describe("tailKey", () => {
  it("is null for a list that is not there or is empty", () => {
    expect(tailKey(null)).toBeNull();
    expect(tailKey([])).toBeNull();
  });

  it("is the id of the last message, and older messages inserted at the start do not change it", () => {
    expect(tailKey(ids(11, 60))).toBe("m60");
    expect(tailKey([...ids(1, 5), ...ids(11, 60)])).toBe(tailKey(ids(11, 60)));
  });
});

describe("a full window sliding under the reader", () => {
  it("keeps the COUNT at 50 while the last id moves — why the count could not be the signal", () => {
    const polls = [ids(11, 60), ids(12, 61), ids(13, 62), ids(13, 62)];
    expect(polls.map((p) => p.length)).toEqual([50, 50, 50, 50]);
    expect(polls.map((p) => tailKey(p))).toEqual(["m60", "m61", "m62", "m62"]);
  });
});

describe("shouldScrollToBottom", () => {
  it("decides each case of the rule", () => {
    // not shown yet: never
    expect(shouldScrollToBottom(null, snap({ ready: false }), true)).toBe(false);
    expect(shouldScrollToBottom(snap(), snap({ ready: false, tail: "m2" }), true)).toBe(false);
    // 1. first time it can be shown (mounted, or it was loading), wherever the box happens to be
    expect(shouldScrollToBottom(null, snap(), false)).toBe(true);
    expect(shouldScrollToBottom(snap({ ready: false }), snap(), false)).toBe(true);
    // 2. another conversation in the same list
    expect(shouldScrollToBottom(snap(), snap({ threadKey: "portal:precheck" }), false)).toBe(true);
    // 3. the patient acted
    expect(shouldScrollToBottom(snap(), snap({ request: 1 }), false)).toBe(true);
    // same tail: a poll with identical data
    expect(shouldScrollToBottom(snap(), snap(), true)).toBe(false);
    // 4. the tail moved: only for a reader at the end
    expect(shouldScrollToBottom(snap(), snap({ tail: "m2" }), true)).toBe(true);
    expect(shouldScrollToBottom(snap(), snap({ tail: "m2" }), false)).toBe(false);
  });
});

describe("createTailScroller", () => {
  it("opens at the end once, and a poll with identical data does nothing", () => {
    const { scroller, scrolls } = harness();
    scroller.update(snap());
    expect(scrolls()).toBe(1);
    for (let i = 0; i < 3; i += 1) scroller.update(snap()); // a new object each poll, the same ids
    expect(scrolls()).toBe(1);
  });

  it("waits until the list can be shown", () => {
    const { scroller, scrolls } = harness();
    scroller.update(snap({ ready: false, tail: null }));
    scroller.update(snap({ ready: false, tail: "m1" })); // messages are there, the time anchor is not
    expect(scrolls()).toBe(0);
    scroller.update(snap({ ready: true, tail: "m1" }));
    expect(scrolls()).toBe(1);
  });

  it("follows every reply while the 50-message window slides under a reader at the end", () => {
    const { scroller, scrolls } = harness();
    scroller.onScroll(AT_END);
    scroller.update(snap({ tail: "m60" }));
    scroller.update(snap({ tail: "m61" }));
    scroller.update(snap({ tail: "m62" }));
    scroller.update(snap({ tail: "m63" }));
    expect(scrolls()).toBe(4);
  });

  it("does not pull a reader of older messages, and follows again once they are back at the end", () => {
    const { scroller, scrolls } = harness();
    scroller.update(snap({ tail: "m60" }));
    expect(scrolls()).toBe(1);

    scroller.onScroll(READING);
    scroller.update(snap({ tail: "m61" }));
    scroller.update(snap({ tail: "m62" }));
    expect(scrolls()).toBe(1);

    scroller.onScroll(AT_END);
    scroller.update(snap({ tail: "m63" }));
    expect(scrolls()).toBe(2);
  });

  it("two replies in one poll are one scroll; a reader who leaves between two replies is not pulled by the second", () => {
    const together = harness();
    together.scroller.update(snap({ tail: "m60" }));
    together.scroller.update(snap({ tail: "m62" })); // m61 and m62 arrived in the same poll
    expect(together.scrolls()).toBe(2);

    const apart = harness();
    apart.scroller.update(snap({ tail: "m60" }));
    apart.scroller.update(snap({ tail: "m61" }));
    expect(apart.scrolls()).toBe(2);
    apart.scroller.onScroll(READING);
    apart.scroller.update(snap({ tail: "m62" }));
    expect(apart.scrolls()).toBe(2);
  });

  it("the patient's own send always comes into view; the copy swap after it does not pull a reader who left", () => {
    const { scroller, scrolls } = harness();
    scroller.update(snap({ tail: "m60" }));
    scroller.onScroll(READING);

    scroller.update(snap({ tail: "local-1", request: 1 })); // own send, from history
    expect(scrolls()).toBe(2);
    scroller.update(snap({ tail: "m61", request: 1 })); // the server's copy replaces the local one: still at the end
    expect(scrolls()).toBe(3);

    scroller.onScroll(READING); // reads history again while the AI thinks
    scroller.update(snap({ tail: "m62", request: 1 })); // the reply lands
    expect(scrolls()).toBe(3);
  });

  it("a short conversation behaves as before: opens at the end and follows every new message", () => {
    const { scroller, scrolls } = harness(); // no scroll event ever: it fits the box
    scroller.update(snap({ tail: "m10" }));
    scroller.update(snap({ tail: "m11" }));
    expect(scrolls()).toBe(2);
  });

  it("another thread opens at its end whatever the first one's scroll was", () => {
    const { scroller, scrolls } = harness();
    scroller.update(snap({ tail: "m60" }));
    scroller.onScroll(READING);

    scroller.update(snap({ threadKey: "portal:precheck", tail: "p9" })); // the PreCheck tab
    expect(scrolls()).toBe(2);
    scroller.onScroll(READING);
    scroller.update(snap({ threadKey: "portal:secretaria", tail: "m60" })); // and back
    expect(scrolls()).toBe(3);
  });

  it("never scrolls while the list cannot be shown", () => {
    const { scroller, scrolls } = harness();
    scroller.update(snap({ ready: false }));
    scroller.update(snap({ ready: false, tail: "m2" }));
    scroller.update(snap({ ready: false, request: 1 }));
    expect(scrolls()).toBe(0);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm test -- lib/__tests__/thread-scroll.test.ts`
Expected: FAIL — `Failed to resolve import "../thread-scroll"` (módulo inexistente).

- [ ] **Step 3: Implement**

Criar `lib/thread-scroll.ts`:

```ts
// thread-scroll.ts — when the patient's conversation follows its newest message.
//
// The server answers every poll with a WINDOW: the newest 50 messages. When a reply lands it enters
// at the end and the oldest message leaves, so the COUNT stays 50 — the signal this screen used to
// scroll by (the number of messages) never moved, and the reply sat below the fold until the patient
// scrolled by hand (production, 2026-10-02). The signal is the id of the LAST message.
//
// Not every change of the last message may move the screen: someone reading older messages must not be
// pulled to the end each time a poll brings something new. The rule, in one place — follow the end when
//   1. the list can be shown for the first time (it was loading, or it just mounted);
//   2. it now shows ANOTHER thread (the product tabs share one list);
//   3. the patient acted (`request` moved: they sent, or tapped a card) — their own bubble always comes
//      into view, even from history;
//   4. the last message changed AND the patient was at the end (within NEAR_BOTTOM_PX of it) when they
//      last scrolled.
// Otherwise stay where the reader is.
//
// "At the end" is measured on each scroll event, i.e. BEFORE the new content exists. Measuring after the
// update would count the new bubble's own height as distance, and a tall reply (long texts arrive
// expanded) would read as "the patient scrolled up". A scroll this module asks for marks the reader as
// at the end at once, without waiting for the browser's scroll event.
//
// The scroll itself is instant on purpose (the caller never passes `behavior: "smooth"`): it is what the
// screen did before, a smooth one would fire intermediate scroll events that read as "left the end", and
// there is no motion for prefers-reduced-motion to cancel.
//
// Everything here is plain functions over numbers and ids, so it is tested in node without a DOM; the
// component only wires `onScroll` and an effect to it (components/patient/PatientMessageList.tsx).

// About one short bubble plus the gap between two. A reader at "the end" is rarely at exactly 0 px
// (fractional pixels, the thread's bottom padding); someone reading history is further up than this.
export const NEAR_BOTTOM_PX = 120;

export type ScrollMetrics = { scrollTop: number; scrollHeight: number; clientHeight: number };

export function isNearBottom(metrics: ScrollMetrics, thresholdPx: number = NEAR_BOTTOM_PX): boolean {
  return metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight <= thresholdPx;
}

// What is at the END of the thread. Inserting OLDER messages at the start (a future "ver anteriores")
// changes the count but not this, so it would not pull the patient to the end.
export function tailKey(messages: readonly { readonly id: string }[] | null): string | null {
  return messages && messages.length > 0 ? messages[messages.length - 1].id : null;
}

export type TailSnapshot = {
  // The list can be drawn (messages and the time anchor are there).
  ready: boolean;
  // Which conversation the list shows (the product tabs share one list).
  threadKey: string;
  // `tailKey` of the messages on screen.
  tail: string | null;
  // Bumped by the screen when the PATIENT acts (a typed send, a card tap).
  request: number;
};

export function shouldScrollToBottom(prev: TailSnapshot | null, next: TailSnapshot, nearBottom: boolean): boolean {
  if (!next.ready) return false;
  if (prev === null || !prev.ready) return true;
  if (next.threadKey !== prev.threadKey) return true;
  if (next.request !== prev.request) return true;
  if (next.tail === prev.tail) return false;
  return nearBottom;
}

export type TailScroller = {
  // From the scroll box's `scroll` event.
  onScroll(metrics: ScrollMetrics): void;
  // From an effect keyed on the snapshot's fields; `scrollToBottom` runs when the rule says follow.
  update(next: TailSnapshot): void;
};

export function createTailScroller(scrollToBottom: () => void): TailScroller {
  let prev: TailSnapshot | null = null;
  // A thread that fits the box has never fired a scroll event and is at its end.
  let nearBottom = true;
  return {
    onScroll(metrics) {
      nearBottom = isNearBottom(metrics);
    },
    update(next) {
      if (shouldScrollToBottom(prev, next, nearBottom)) {
        scrollToBottom();
        nearBottom = true;
      }
      prev = next;
    },
  };
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `npm test -- lib/__tests__/thread-scroll.test.ts` e `.\node_modules\.bin\tsc.cmd --noEmit`
Expected: PASS (15 testes); `tsc` sem saída.

- [ ] **Step 5: Commit**

```powershell
git add lib/thread-scroll.ts lib/__tests__/thread-scroll.test.ts
git commit -m "feat(portal): pure rule for when the conversation follows its newest message" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Ligar a regra na lista e na tela

**Files:**
- Modify: `components/patient/PatientMessageList.tsx` (arquivo inteiro, abaixo)
- Modify: `components/portal/PortalConversation.tsx`
- Test: `components/patient/__tests__/message-list-scroll.test.tsx` (novo)

**Interfaces:**
- Consumes: `createTailScroller`, `tailKey` (Task 3).
- Produces: `PatientMessageList` ganha as props opcionais `threadKey?: string` (padrão `""`) e `scrollRequest?: number` (padrão `0`) e deixa de depender da contagem. `PortalConversation` passa `threadKey={conversationId}` e `scrollRequest={ownSends}`; `ownSends` sobe a cada envio digitado e a cada toque num cartão.

- [ ] **Step 1: Write the failing test**

Criar `components/patient/__tests__/message-list-scroll.test.tsx`:

```tsx
import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { Anchor } from "@/lib/format-dates";
import type { Message } from "@/lib/types";
import { PatientMessageList } from "../PatientMessageList";

const anchor: Anchor = { now: new Date("2026-10-02T12:00:00Z"), timeZone: "America/Sao_Paulo" };
const read = (relative: string) => readFileSync(new URL(relative, import.meta.url), "utf8");

const thread: Message[] = [
  { kind: "message", id: "m1", conversationId: "portal:secretaria", sender: "secretaria", sentAt: "2026-10-02T11:00:00Z", text: "Bom dia, em que posso ajudar?" },
  { kind: "message", id: "m2", conversationId: "portal:secretaria", sender: "paciente", sentAt: "2026-10-02T11:01:00Z", text: "Quero marcar uma consulta", status: "entregue" },
];

// renderToStaticMarkup runs neither effects nor handlers, so what these tests can prove is that the list
// still draws with the new props, and (below) that the wiring and the "instant scroll" premise hold.
describe("PatientMessageList", () => {
  it("still draws the thread in order, with the scroll props", () => {
    const html = renderToStaticMarkup(
      <PatientMessageList messages={thread} anchor={anchor} patientName="" onRetry={() => {}} threadKey="portal:secretaria" scrollRequest={2} />,
    );
    expect(html).toContain('role="log"');
    expect(html.indexOf("Bom dia, em que posso ajudar?")).toBeGreaterThan(-1);
    expect(html.indexOf("Quero marcar uma consulta")).toBeGreaterThan(html.indexOf("Bom dia, em que posso ajudar?"));
  });

  it("still shows the loading state until the messages and the time anchor exist", () => {
    const html = renderToStaticMarkup(<PatientMessageList messages={null} anchor={null} patientName="" onRetry={() => {}} />);
    expect(html).toContain('aria-busy="true"');
  });
});

describe("the list follows the last message, not the count (wiring guard)", () => {
  const source = read("../PatientMessageList.tsx");

  it("scrolls through the shared rule in lib/thread-scroll.ts", () => {
    expect(source).toContain("createTailScroller");
    expect(source).toContain("tailKey(messages)");
    expect(source).toContain("scroller.update(");
    expect(source).toContain("scroller.onScroll(");
  });

  it("no longer keys its effect on the number of messages", () => {
    expect(source).not.toMatch(/\[count, ready\]/);
    expect(source).not.toContain("messages?.length");
  });
});

describe("the scroll is instant by design (prefers-reduced-motion has no motion to cancel)", () => {
  it("no stylesheet turns smooth scrolling on", () => {
    for (const file of [
      "../../../app/styles/base.css",
      "../../console/console.css",
      "../patient.css",
      "../../portal/portal.css",
    ]) {
      expect(read(file), file).not.toMatch(/scroll-behavior\s*:\s*smooth/);
    }
  });

  it("the list never asks for a smooth scroll", () => {
    expect(read("../PatientMessageList.tsx")).not.toMatch(/behavior\s*:\s*["']smooth["']/);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm test -- components/patient/__tests__/message-list-scroll.test.tsx`
Expected: FAIL — os dois testes do "wiring guard" (`createTailScroller` ausente; `[count, ready]` ainda presente). Os de renderização e os de "rolagem instantânea" passam (são guardas que já valem antes e devem continuar valendo depois).

- [ ] **Step 3: Implement the list**

Substituir `components/patient/PatientMessageList.tsx` inteiro por:

```tsx
"use client";

// PatientMessageList — the thread as the patient sees it: the SAME loop as the
// console's Thread.tsx (day divider from sameCivilDay, grouping from
// lib/messages.ts isContinuation, one MessageBubble per message), with
// `perspective="paciente"` and nothing else. Thread.tsx is not reused whole
// because its contract is the desk's (me, context, handover, starring, the
// "you take over" hint); what the two screens share is exactly this loop and
// these classes (console.css .thread-scroll / .msg-row / .day-divider).
//
// System dividers ("Atendimento assumido por…", "Agendamento confirmado…")
// are shown as the console shows them. Whether the patient should see every
// one of them is the backend's call (some are the desk's internal events).
//
// SCROLL. The server answers with a WINDOW (the newest 50 messages), so a
// reply slides it: the count stays 50 and the oldest message leaves. The list
// therefore follows the END of the thread by the id of its LAST message, not by
// how many there are, and only while the reader is at the end, has just acted,
// or has moved to another thread: lib/thread-scroll.ts holds the rule and
// explains each case. (The console's Thread.tsx keeps its own count-based
// effect: the hub's list has no window.)

import { useEffect, useRef, useState } from "react";

import { formatDayDivider, sameCivilDay, type Anchor } from "@/lib/format-dates";
import { isContinuation } from "@/lib/messages";
import { createTailScroller, tailKey } from "@/lib/thread-scroll";
import type { Message } from "@/lib/types";
import { MessageBubble } from "../console/MessageBubble";
import { Spinner } from "../primitives/Button";
import "../console/console.css";
import "./patient.css";

type Props = {
  // null while loading (either the thread or the time anchor).
  messages: Message[] | null;
  anchor: Anchor | null;
  patientName: string;
  onRetry: (messageId: string) => void;
  // Aborts an upload still in flight (the ✕ on the attachment card).
  onCancel?: (messageId: string) => void;
  // Reply buttons / list rows on the patient's own screen: may they tap, and
  // if not, why. Absent = read-only (the cards are drawn inert).
  canAnswer?: boolean;
  answerDisabledReason?: string | null;
  onAnswer?: (messageId: string, actionId: string) => Promise<void>;
  // Which conversation the list is showing (the product tabs share one list).
  // A new value opens the new thread at its end whatever the old one's scroll.
  threadKey?: string;
  // The screen bumps this when the PATIENT acts (a typed send, a card tap): their
  // own bubble is brought into view even if they were reading older messages.
  scrollRequest?: number;
};

export function PatientMessageList({
  messages,
  anchor,
  patientName,
  onRetry,
  onCancel,
  canAnswer = false,
  answerDisabledReason = null,
  onAnswer,
  threadKey = "",
  scrollRequest = 0,
}: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const ready = messages !== null && anchor !== null;
  // What is at the end of the thread: the only thing that says "something new
  // arrived" once the server's window is full.
  const tail = tailKey(messages);
  // One scroller for the life of the list; the instant `scrollIntoView` is what
  // this screen always did (never `behavior: "smooth"`).
  const [scroller] = useState(() => createTailScroller(() => bottomRef.current?.scrollIntoView({ block: "end" })));

  useEffect(() => {
    scroller.update({ ready, threadKey, tail, request: scrollRequest });
  }, [scroller, ready, threadKey, tail, scrollRequest]);

  return (
    <div
      className="thread-scroll scroll"
      role="log"
      aria-live="polite"
      aria-label="Mensagens"
      onScroll={(e) => scroller.onScroll(e.currentTarget)}
    >
      {!ready && (
        <div className="thread-state" aria-busy="true">
          <Spinner label="Carregando mensagens" />
        </div>
      )}
      {ready && messages.length === 0 && (
        <div className="thread-state"><p className="muted">Nenhuma mensagem ainda. Escreva para a clínica abaixo.</p></div>
      )}
      {ready && messages.map((m, i) => {
        const previous = i > 0 ? messages[i - 1] : null;
        const newDay = !previous || !sameCivilDay(previous.sentAt, m.sentAt, anchor.timeZone);
        const continuation = !newDay && isContinuation(previous, m, anchor.timeZone);
        return (
          <div key={m.id} className={continuation ? "msg-row msg-row--cont" : "msg-row"}>
            {newDay && (
              <div className="day-divider" role="separator" aria-label={formatDayDivider(m.sentAt, anchor)}>
                <span>{formatDayDivider(m.sentAt, anchor)}</span>
              </div>
            )}
            {m.kind === "system" ? (
              <div className="system-divider"><span>{m.text}</span></div>
            ) : (
              <MessageBubble
                message={m}
                patientName={patientName}
                anchor={anchor}
                continuation={continuation}
                perspective="paciente"
                onRetry={() => onRetry(m.id)}
                onCancel={onCancel ? () => onCancel(m.id) : undefined}
                canAnswer={canAnswer}
                answerDisabledReason={answerDisabledReason}
                onAnswer={onAnswer ? (actionId) => onAnswer(m.id, actionId) : undefined}
              />
            )}
          </div>
        );
      })}
      <div ref={bottomRef} />
    </div>
  );
}
```

- [ ] **Step 4: Implement the screen**

Em `components/portal/PortalConversation.tsx`:

(a) Depois de `const sequence = useRef(0);` acrescentar:

```ts
  // Bumped when the PATIENT acts (a typed send, a card tap): the list brings their own bubble into view
  // even when they were reading older messages (lib/thread-scroll.ts, rule 3).
  const [ownSends, setOwnSends] = useState(0);
```

(b) Em `send()`, trocar:

```ts
    if (preview) previews.current[id] = preview;
    patchLocal(message.conversationId, (local) => [...local, message]);
    await deliver(product, message);
```

por:

```ts
    if (preview) previews.current[id] = preview;
    patchLocal(message.conversationId, (local) => [...local, message]);
    setOwnSends((n) => n + 1);
    await deliver(product, message);
```

(c) Em `answer()`, trocar:

```ts
    setTapped((prev) => ({ ...prev, [messageId]: { actionId, chosenAt: sentAt } }));
    patchLocal(message.conversationId, (local) => [...local, message]);
    await deliver(product, message);
```

por:

```ts
    setTapped((prev) => ({ ...prev, [messageId]: { actionId, chosenAt: sentAt } }));
    patchLocal(message.conversationId, (local) => [...local, message]);
    setOwnSends((n) => n + 1);
    await deliver(product, message);
```

(`retry()` não muda: reenviar não cria bolha nova.)

(d) No JSX, trocar:

```tsx
          <PatientMessageList
            messages={messages}
            anchor={anchor}
            patientName=""
            onRetry={retry}
```

por:

```tsx
          <PatientMessageList
            messages={messages}
            anchor={anchor}
            patientName=""
            threadKey={conversationId}
            scrollRequest={ownSends}
            onRetry={retry}
```

- [ ] **Step 5: Run gates**

Run: `npm test -- components/patient/__tests__/message-list-scroll.test.tsx`
Expected: PASS (6 testes).

Run: `npm test` e `.\node_modules\.bin\tsc.cmd --noEmit` e `npm run build`
Expected: vitest todo verde (`B` + 33 testes novos: 5 da demo, 7 da poda, 15 da regra, 6 do componente); `tsc` sem saída; `next build` termina com exit 0.

- [ ] **Step 6: Commit**

```powershell
git add components/patient/PatientMessageList.tsx components/portal/PortalConversation.tsx components/patient/__tests__/message-list-scroll.test.tsx
git commit -m "fix(portal): follow the last message of the conversation, not the message count" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Prova no navegador (GREEN), documentação e revisão final

Comportamento visual/runtime não se prova só com teste e build (`AI_WORKFLOW.md`, "Definição de pronto").

**Files:**
- Create: `docs/CHECKPOINT_portal_scroll_janela_recente.md`
- Modify: `CLAUDE.md` (uma linha de ponteiro em "Documentação — manter em dia")

- [ ] **Step 1: Prova GREEN no navegador**

Mesmo ambiente do Task 1 Step 5 (`npm run dev` com `C:` maiúsculo; `http://localhost:3000/componentes/portal/?clinicas=1&janela=1`; mesma sonda). Registre cada medição.

1. Abrir "Clínica Vida Leve": `bubbles` 50, `lastText` `Resposta 60…`, `gapPx` ≤ 2, `lastInView` true.
2. **Resposta com a janela cheia:** enviar `Qual o endereço?`, esperar 8 s sem tocar na tela → `lastText` `Resposta automática: Qual o endereço?`, `lastInView` **true**, `gapPx` ≤ 2. (No Task 1 isso dava `lastInView` false.)
3. **Bolha órfã:** enviar `/rajada`, esperar 8 s → `orphan` **false**, `lastText` `Rajada 55`, `lastInView` true.
4. **Leitor de histórico não é puxado:** enviar `Mais uma pergunta`; meio segundo depois rodar `document.querySelector(".patient-panel .thread-scroll").scrollTop = 0`; esperar 7 s; sonda → `scrollTop` ≤ 2 (a tela não se moveu), `lastText` é a resposta (existe no DOM), `lastInView` false. Depois rolar de volta ao fim (`box.scrollTop = box.scrollHeight`), esperar 300 ms, enviar `Última` e esperar 8 s → `lastInView` true.
5. **Aba PreCheck (outro ramo):** clicar na aba "PreCheck" → abre no fim (`gapPx` ≤ 2, `lastInView` true); enviar `Maria` → a bolha própria fica na vista. Voltar à aba da secretarIA → abre no fim.
6. **Janela estreita:** redimensionar para 390 × 800 e repetir os passos 2 e 4 (com a lista de clínicas: clicar na clínica para abrir a conversa).
7. **Movimento reduzido:** emular `prefers-reduced-motion: reduce` e repetir o passo 2; confirmar `getComputedStyle(box).scrollBehavior === "auto"` (a rolagem é instantânea).
8. Console do navegador sem erros novos durante o roteiro.

Pare o `npm run dev`. Opcional: dois prints (antes = Task 1 Step 5; depois = passo 2) em `docs/evidence/` com os nomes `portal_scroll_antes.png` e `portal_scroll_depois.png`.

- [ ] **Step 2: Escrever o CHECKPOINT**

Criar `docs/CHECKPOINT_portal_scroll_janela_recente.md` com este conteúdo (substitua `TASK-0NN`, os SHAs e a tabela da §4 pelos valores reais medidos):

````markdown
# CHECKPOINT — Portal: rolar pela última mensagem e podar cópias locais (TASK-0NN)

Plano: `secretarIA/docs/superpowers/plans/2026-10-02-portal-p6-rolagem-e-copias-locais.md` (P6 do spec
`2026-10-02-ia-entra-em-qualquer-etapa-design.md`). Origem: `secretarIA/docs/CHECKPOINT_portal_mensagens_recentes.md`
("Dependência do front") e a prova ao vivo de 2026-10-02. Executado em 2026-10-02.

**Estado: local + commitado em `task/TASK-0NN-portal-rolagem` (base `0c0d2dd`), NÃO mesclado, NÃO pushado, NÃO
deployado, NÃO provado em produção.** Prova local (demo `?janela=1`) na §4.

## 1. O problema

Desde a TASK-028 o servidor devolve, sem `since`, as 50 mensagens MAIS NOVAS: uma janela que desliza. A tela do
Portal foi escrita para uma lista que só crescia, e quebrava de dois jeitos numa conversa com mais de 50:

1. **A resposta nova ficava abaixo da dobra.** `PatientMessageList` rolava para o fim num efeito sobre
   `[count, ready]`. Com a janela cheia a resposta entra, a mais antiga sai e a contagem continua 50: o efeito não
   dispara.
2. **A cópia local de uma mensagem enviada podia reaparecer como bolha órfã.** O ramo da secretarIA de
   `PortalConversation::refresh` guardava `local` sem podar; com a aba aberta e 50 ou mais mensagens novas, a cópia do
   servidor saía da janela, `unconfirmedLocal` deixava de achar o par e a cópia local voltava ao topo.

## 2. O que mudou

| Onde | O quê |
|---|---|
| `lib/thread-scroll.ts` (novo) | Regra pura: `isNearBottom`, `tailKey`, `shouldScrollToBottom`, `createTailScroller`. |
| `components/patient/PatientMessageList.tsx` | O efeito de `[count, ready]` virou `scroller.update({ ready, threadKey, tail, request })`; `onScroll` na caixa; props novas `threadKey`, `scrollRequest`. |
| `components/portal/PortalConversation.tsx` | `afterSecretariaPoll` no ramo da secretarIA; contador `ownSends` (envio digitado e toque em cartão); `threadKey={conversationId}`. |
| `lib/patient-portal.ts` | `afterSecretariaPoll`, irmã de `afterPrecheckPoll`: `local: unconfirmedLocal(server, prior.local, "secretaria")`. |
| `lib/mock/portal-demo.ts`, `app/componentes/portal/PortalDemo.tsx` | Cenário `?janela=1`: 60 mensagens, serve as 50 mais novas, resposta 2 s depois do envio; `/rajada` faz chegarem 55 respostas juntas. |

## 3. A regra e as decisões (sem perguntar ao dono)

A tela segue o fim da conversa quando (1) a lista pode ser mostrada pela primeira vez; (2) passou a mostrar outra
conversa (as abas de produto compartilham a lista); (3) o paciente agiu (enviou ou tocou num cartão); (4) a última
mensagem mudou E o paciente estava a até 120 px do fim na última vez que rolou. Senão, fica onde ele está.

- **Pelo id da última, não pela contagem** (e não por "a última é do paciente": o próprio envio é um pedido explícito
  da tela, `scrollRequest`, porque a cópia local e a do servidor têm ids diferentes e o fim muda sem ele ter agido).
- **"No fim" medido no evento de rolagem**, antes do conteúdo novo existir; medir depois contaria a altura da resposta
  nova como distância (texto longo chega expandido).
- **Rolagem instantânea**, como sempre foi: a suave dispararia eventos de rolagem intermediários que parecem "saiu do
  fim", e não há movimento para `prefers-reduced-motion` cancelar. Fixado por teste (nenhuma folha liga
  `scroll-behavior: smooth`).
- **Sem aviso "mensagem nova" para quem lê o histórico**: não foi pedido; é decisão de produto à parte.
- **Poda por confirmação** (`unconfirmedLocal`), como o PreCheck; alternativa descartada: podar o que ficou mais
  velho que a janela (depende de o servidor sempre devolver as mais novas e diverge do PreCheck). Transitório
  conhecido: duas mensagens idênticas em menos de 5 s com um poll cruzando o segundo envio podem esconder a segunda por
  uma ida e volta (o refresh do próprio envio a traz logo).
- **Testes sem jsdom** (convenção do repo): regra pura + caixa de rolagem fingida por três números + contador no lugar
  do `scrollIntoView`; a ligação do componente é provada pelo navegador (§4) e por guardas de código-fonte.
- O console (`components/console/Thread.tsx`) NÃO foi tocado: lê a lista do hub, que não tem janela.

## 4. Prova local (demo `/componentes/portal/?clinicas=1&janela=1`)

(Colar aqui a tabela com as medições do Task 1 Step 5 — antes — e do Task 5 Step 1 — depois: passo, `lastInView`,
`gapPx`, `scrollTop`, `orphan`.)

## 5. Prova em PRODUÇÃO — pendente

Depende do deploy do front (a pedido do dono) **e** da secretarIA da TASK-028 (`secretaria_api` + `secretaria-worker`
juntos): sem a segunda, uma conversa longa continua congelada nas 50 mais antigas, com ou sem este ajuste. Roteiro:
agent-browser numa conversa com mais de 50 mensagens, fazer uma pergunta livre à IA → a resposta entra na vista sem
rolar à mão; recarregar (F5) mantém tudo, inclusive a mensagem enviada.

## 6. Como desfazer

`git revert` dos commits do branch (a ordem não importa). Nada mais depende de `thread-scroll.ts`,
`afterSecretariaPoll` nem do cenário `?janela=1`.

## 7. Pendências e vizinhos

- Mensagens mais antigas que as 50 mais novas continuam inalcançáveis pelo Portal (sem "ver anteriores"; o brain-api
  não repassa `before`/`limit`): decisão do dono, ver o CHECKPOINT da secretarIA §9.
- Plano D (`2026-10-01-digitando-frontend.md`), Task 4: o efeito `[count, ready, Boolean(typing)]` não existe mais; o
  marcador de "digitando" entra na chave do fim (`const tail = typing ? \`${tailKey(messages)}#typing\` : tailKey(messages)`),
  e o ramo da secretarIA de `refresh` passa a `{ ...afterSecretariaPoll(prior, server), typing }`.
- Ao mesclar: atualizar a seção "Dependência do front" e o passo 3 do §7 de
  `secretarIA/docs/CHECKPOINT_portal_mensagens_recentes.md` para o estado real.
````

Em `CLAUDE.md`, na seção "Documentação — manter em dia", depois do item `docs/CHECKPOINT_brain_message_anamneses_paridade.md`, acrescentar:

```markdown
- `docs/CHECKPOINT_portal_scroll_janela_recente.md` — Portal, conversa longa (2026-10-02, TASK-0NN): o servidor devolve a
  janela das 50 mais novas (secretarIA TASK-028), então a tela segue a ÚLTIMA mensagem (não a contagem) e poda as cópias
  locais a cada poll. Regra pura em `lib/thread-scroll.ts` (só segue se o paciente estava no fim, acabou de agir ou trocou
  de conversa), `afterSecretariaPoll`, cenário `?janela=1` da demo `/componentes/portal/`. Commitado em branch, NÃO
  deployado; prova em produção pendente.
```

- [ ] **Step 3: Gates finais**

Run: `.\node_modules\.bin\tsc.cmd --noEmit` ; `npm test` ; `npm run build`
Expected: `tsc` sem saída; vitest todo verde (`B` + 33 testes novos: 5 da demo, 7 da poda, 15 da regra, 6 do componente); build com exit 0. Confira `git status --short`: só os arquivos deste plano.

- [ ] **Step 4: Commit**

```powershell
git add docs/CHECKPOINT_portal_scroll_janela_recente.md CLAUDE.md
git commit -m "docs(portal): checkpoint of the scroll and local-copy fixes for the 50-message window" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

(Se tirou prints, inclua `docs/evidence/portal_scroll_antes.png` e `docs/evidence/portal_scroll_depois.png` no `git add`.)

- [ ] **Step 5: Revisão final**

Um Reviewer fresco (`ecc:react-reviewer` ou `ecc:typescript-reviewer`) lê o diff do branch inteiro (`git diff main...HEAD`) com o Review Focus acima na mão: efeito sem `count`; ramo do PreCheck e `deliver` intactos; `ownSends` só sobe em envio digitado e toque; `threadKey` e `scrollRequest` com padrões que não mudam outro chamador; nada de `smooth`. Registre os findings antes de corrigir.

---

## Fechamento (fora das tasks)

- **Deploy nunca faz parte deste plano.** O front sobe sozinho (imagem Docker própria) quando o dono pedir; push e merge também só a pedido. Ordem para a prova ao vivo: secretarIA da TASK-028 (`secretaria_api` + `secretaria-worker` juntos) e depois este front; com o front só, uma conversa longa continua congelada.
- **A prova ao vivo depois do deploy:** Chrome/agent-browser numa conversa com mais de 50 mensagens (a conta de QA com a conversa longa; o código de acesso chega por e-mail): fazer à IA uma pergunta livre → a resposta aparece na vista sem rolar à mão; recarregar a página (F5) mantém tudo, inclusive a mensagem enviada. Só então o CHECKPOINT da secretarIA pode dizer "provado em produção" (§7 dele) e a seção "Dependência do front" sai.
- **Plano D (`digitando-frontend`)**: depois deste, adaptar a Task 4 dele como descrito no CHECKPOINT §7 deste plano; nunca os dois ao mesmo tempo nos mesmos arquivos.
- **Em linguagem do dono:** quando o paciente conversa há muito tempo, a resposta nova passa a aparecer sozinha na tela, sem ele precisar rolar; se ele estiver lendo mensagens antigas, a tela não pula; e uma mensagem que ele enviou nunca volta a aparecer lá em cima por engano. Nada muda para quem tem poucas mensagens.
