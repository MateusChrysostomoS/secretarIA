# "Digitando…" universal + rolagem da janela recente — Frontend (Brain-Message-Frontend) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Revisado em 2026-10-03 contra o código de `main` do Brain-Message-Frontend (HEAD `0c0d2dd`, árvore limpa).** Mudanças em relação à versão de 2026-10-01:
> 1. **Nova Task 0** (rolagem pela última mensagem + poda das cópias locais). O TASK-028 fez a API devolver as 50 mensagens MAIS NOVAS; sem a Task 0 a resposta nova de uma conversa com mais de 50 mensagens chega ao navegador e fica fora da vista. Substitui o prompt solto `z_prompts/PROMPT_PORTAL_SCROLL_E_COPIAS_LOCAIS_JANELA_RECENTE.md` (agora absorvido aqui).
> 2. A chave de rolagem da Task 4 passou de `[count, ready, Boolean(typing)]` para `[newest, ready, Boolean(typing)]` (a contagem fica em 50 numa janela deslizante).
> 3. `lastSender = messages[last].sender` **não compila**: `Message` inclui `kind: "system"`, que não tem `sender`. Novo helper `lastVoice` em `lib/typing.ts`.
> 4. `Perspective` já existe em `lib/messages.ts:42`; `lib/typing.ts` importa em vez de redefinir.
> 5. O token `--ink-muted` **não existe**; a hora da bolha usa `color: var(--ink); opacity: .6` (`console.css:169`). Os pontos usam `var(--ink)`.
> 6. O `Composer` limpa o campo em `setText((current) => (current === sentText ? "" : current))` (linha 108), não em `setText("")`. `clinicCall` é genérico (`clinicCall<T>`), então a chamada do batimento vai como `clinicCall<unknown>`.
>
> Conferido e **sem mudança**: contrato do backend (`typing`, `typing_by`, `accepts_typing` no corpo; `GET/POST /tenants/me/conversations/{id}/typing` com `{typing, by}`), `.sr-only` em `app/styles/base.css`, `.msg--first` em `console.css`, os padrões de include do vitest, e as âncoras de linha de `PortalConversation.tsx` (319-369, 475, 519, 628, 712, 729), `Thread.tsx` (68, 77-79, 197, 210), `ChatScreen.tsx` (161-195, 429) e `Composer.tsx` (47, 74, 181).

**Goal:** (a) A conversa do Portal com mais de 50 mensagens mostra a resposta nova sem o paciente rolar à mão. (b) Os três pontinhos "digitando…" aparecem, como no WhatsApp, sempre que a outra parte está digitando: no Portal do paciente (a automação ou a equipe da clínica digitando) e no console da clínica (a automação ou o paciente digitando); e o próprio teclado do paciente e da equipe avisa que está digitando. Vale para QUALQUER produto (hoje secretarIA e PreCheck; um produto novo não exige mudança aqui).

**Architecture:** Toda a regra mora em módulos puros e testáveis: `lib/messages.ts::scrollKey` (quando rolar), `lib/patient-portal.ts::afterSecretariaPoll` (poda das cópias locais) e `lib/typing.ts` (ler o campo do corpo, escolher voz e rótulo, decidir quando um batimento deve sair, esconder um indicador obsoleto). Um componente compartilhado `TypingBubble` desenha a bolha (mesma cor/rabinho da voz de quem digita). O `Composer` (já compartilhado por paciente e clínica) ganha um gancho `onTyping` com limite de 1 batimento a cada 3 s. O Portal lê `typing/typing_by/accepts_typing` do corpo que já consulta a cada 4 s e só manda batimento quando `accepts_typing` (um humano conduz); o console usa duas chamadas novas e opcionais (`getTyping`, `sendTyping`). Servidor sem o campo = nunca mostra e nunca quebra.

**Tech Stack:** Next.js (static export), TypeScript, CSS à mão com tokens, vitest (ambiente `node`, sem jsdom; componentes testados com `renderToStaticMarkup`; efeitos de React NÃO são executáveis em teste — a decisão vai para função pura).

**Spec:** Decisões do dono (2026-10-01): "tem que ser universal igual WhatsApp, qualquer interação que esteja digitando tem que aparecer; deixa isso fixo; se tiver outro produto, tem que funcionar para ele também"; "o paciente digitando só importa quando um humano da clínica está na conversa — com a automação conduzindo não precisa". Contrato do backend: `secretarIA/docs/superpowers/plans/2026-10-01-digitando-backend.md` (campos `typing`, `typing_by` ∈ `automation|staff`, `accepts_typing` na listagem do Portal; `POST /patient-access/threads/{product}/typing`; no console `GET`/`POST /tenants/me/conversations/{id}/typing`). A Task 0 vem de `secretarIA/docs/CHECKPOINT_portal_mensagens_recentes.md` ("Dependência do front").

## Global Constraints

- Gates: `.\node_modules\.bin\tsc.cmd --noEmit`, `npm test`, `npm run build` (nunca `npx tsc`; **nunca `npm run lint`** — ESLint não está instalado e o comando trava num prompt). Commit local permitido; push/deploy só com pedido explícito do dono.
- Workspace: o checkout principal do front está limpo em `main`, então dá para trabalhar nele. Se houver mais de um agente no repo ou trabalho não commitado, use worktree em `C:\TECH\BRAIN-worktrees\TASK-NNN\Brain-Message-Frontend`, branch `task/TASK-NNN-digitando-frontend`. **TASK-030 já está em uso** em `BRAIN/tasks/` e em `BRAIN-worktrees/`: confira o próximo número livre (provavelmente TASK-031) antes de criar.
- `app/styles/tokens.css` é o ÚNICO lugar com hex: componente e CSS usam só `var(--...)`.
- Nenhum `fetch` em componente: toda chamada passa por `ConsoleApi` (`lib/console-api.ts`) ou `lib/real/*`.
- Texto de UI em português (sentence case); código e comentários em inglês.
- Acessibilidade: o nome acessível é texto num `<span className="sr-only">`; pontos `aria-hidden`; sem `role="status"` (a lista já é `role="log" aria-live="polite"`). Respeitar `prefers-reduced-motion` com `animation: none` (a regra global só encurta a duração e deixaria os pontos congelados no meio).
- Regra de perspectiva do repo: a bolha de digitando é SEMPRE do outro lado (`msg--in`) e usa a voz de quem digita (`Sender` = `"paciente" | "clinica" | "secretaria" | "precheck"`, classe `msg--<voz>`). Nunca desenhar moldura de celular nem card.
- Universalidade: nenhum `if (product === "secretaria")` fora de `lib/typing.ts`. Produto desconhecido cai numa voz neutra (`clinica`) e no rótulo "A clínica está digitando"; produto novo ganha nome/voz acrescentando UMA entrada em `lib/typing.ts::PRODUCT_NAMES`/`PRODUCT_VOICE`.
- Batimento: no máximo 1 a cada 3 000 ms por conversa; nunca enquanto o campo está vazio; nunca com a aba escondida; parar sozinho ao enviar.
- Deploy: a Task 0 é **independente** e pode ir ao ar sozinha (só front; não depende do backend de "digitando"). As Tasks 1-5 só aparecem depois do backend (secretaria_api + worker) e do brain-api; servidor sem os campos = comportamento de hoje. Peça autorização do dono a cada etapa.

## Review Focus

- **Rolagem (Task 0):** janela cheia de 50 + resposta nova ⇒ a chave muda e o efeito dispara; poll repetido sem novidade ⇒ mesma chave (não puxa quem está lendo o histórico); mensagens ANTIGAS inseridas no começo não mudam a chave.
- **Poda (Task 0):** nunca derruba cópia `enviando` ou `falhou` (o "tentar de novo" precisa dela); só descarta o que TEM par no servidor; uma mensagem recém-enviada que o poll ainda não trouxe continua em `local`; toque em botão/lista (`echoOf`) poda do mesmo jeito.
- Campo ausente/`null`/de tipo errado no corpo: "não está digitando" (nunca indicador fantasma).
- O paciente nunca vê o próprio "digitando"; o console nunca vê "equipe digitando" de si mesmo (o backend já filtra; o front não tem como mostrar `staff` no console e ignora).
- "Paciente digitando" some quando a última mensagem da conversa já é do paciente (a mensagem chegou); divisor de sistema no fim da conversa NÃO conta como "última mensagem".
- Trocar de aba/produto/conversa: o indicador de uma não aparece na outra.
- Conversa PreCheck no console: sem `getTyping`/`sendTyping` (produto só de automação; o backend responde "não aplicado").
- Batimento só quando `accepts_typing` (Portal); falha de rede/401/404 em batimento ou leitura: silêncio, nunca banner nem queda do poll de mensagens.
- Reduced motion: pontos estáticos e legíveis; tema claro e escuro: contraste do ponto sobre cada voz.
- Rolagem: o indicador entra na rolagem para o fim como uma mensagem, sem pular a tela.

## File Structure

- Modify `lib/messages.ts` — `scrollKey` (Task 0). Create `lib/__tests__/scroll-key.test.ts`.
- Modify `lib/patient-portal.ts` — `afterSecretariaPoll` (Task 0); depois `PortalThreadState.typing` e ajustes em `afterPrecheckPoll/afterPrecheckPost` (Task 4). Create `lib/__tests__/portal-window-prune.test.ts`.
- Modify `components/patient/PatientMessageList.tsx` — chave de rolagem (Task 0), prop `typing` (Task 4).
- Modify `components/portal/PortalConversation.tsx` — poda no poll (Task 0); ler o corpo, mandar batimento, passar à lista e ao compositor (Task 4).
- Create `lib/typing.ts` — regra pura (tipos, `parseTyping`, `typingView`, `lastVoice`, `shouldBeat`, `beatOnKeystroke`, `hideStaleTyping`, `NO_TYPING`, `PRODUCT_NAMES`, `PRODUCT_VOICE`). Create `lib/__tests__/typing.test.ts`.
- Create `components/console/TypingBubble.tsx`; modify `components/console/console.css`. Create `components/console/__tests__/typing-bubble.test.tsx`.
- Modify `components/console/Composer.tsx` — gancho `onTyping`.
- Modify `lib/real/patient-access.ts` — `sendTypingBeat(tenantId, product)`.
- Modify `lib/console-api.ts`, `lib/real/console-api.real.ts`, `lib/mock/console-api.mock.ts` — `getTyping?`, `sendTyping?`. Create `lib/real/__tests__/console-typing.test.ts`.
- Modify `components/console/ChatScreen.tsx`, `components/console/Thread.tsx`.
- Modify `lib/__tests__/patient-portal.test.ts` (literais de `PortalThreadState`).
- `components/console/Thread.tsx` NÃO ganha chave de rolagem por id: o console lê a lista do hub, que não tem limite (CHECKPOINT do TASK-028 §8), então a contagem continua correta ali.

---

### Task 0: A resposta nova entra na vista (janela das 50 mais novas)

**Por que:** desde o TASK-028 a leitura do Portal (sem `since`, a cada 4 s) devolve uma **janela deslizante**: chega uma mensagem, a mais antiga sai, a contagem continua 50. Dois pontos do front foram escritos para uma lista que só crescia. Verificado no código de `main` em 2026-10-03: `PatientMessageList.tsx` rola só quando `[count, ready]` muda (linhas 55-57), e o ramo da secretarIA de `refresh` guarda `local: prior?.local ?? []` sem podar (`PortalConversation.tsx:334`).

**Files:**
- Modify: `lib/messages.ts` (junto de `readCursor`, linha ~58)
- Modify: `lib/patient-portal.ts` (junto de `afterPrecheckPoll`, linha ~307)
- Modify: `components/patient/PatientMessageList.tsx` (linhas 50-57)
- Modify: `components/portal/PortalConversation.tsx` (import perto da linha 91; `setState` das linhas 330-335)
- Test: `lib/__tests__/scroll-key.test.ts`, `lib/__tests__/portal-window-prune.test.ts`

**Interfaces:**
- Produces:
  - `scrollKey(messages: readonly Message[] | null): string | null` — o `id` da última linha (qualquer tipo, divisor de sistema incluso: ele é desenhado no fim e merece rolagem); `null` para lista `null` ou vazia.
  - `afterSecretariaPoll(prior: PortalThreadState | undefined, server: BubbleMessage[]): PortalThreadState` — `{ server, local: unconfirmedLocal(server, prior?.local ?? [], "secretaria"), loaded: true, sessionId: null }`.

- [ ] **Step 1: Write the failing tests**

Criar `lib/__tests__/scroll-key.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { scrollKey } from "@/lib/messages";
import type { Message } from "@/lib/types";

const row = (n: number): Message => ({
  kind: "message",
  id: `m${n}`,
  conversationId: "secretaria",
  sender: n % 2 ? "paciente" : "secretaria",
  sentAt: new Date(Date.UTC(2026, 9, 1, 12, n)).toISOString(),
  text: `t${n}`,
});
const run = (from: number, to: number): Message[] => Array.from({ length: to - from + 1 }, (_, i) => row(from + i));

describe("scrollKey", () => {
  it("changes on every poll of a sliding 50-message window although the count stays 50", () => {
    const polls = [run(11, 60), run(12, 61), run(13, 62)];
    expect(polls.map((p) => p.length)).toEqual([50, 50, 50]);
    expect(polls.map(scrollKey)).toEqual(["m60", "m61", "m62"]);
  });

  it("is the same for a poll that brought nothing new (a reader scrolled up is not pulled down)", () => {
    expect(scrollKey(run(11, 60))).toBe(scrollKey(run(11, 60)));
  });

  it("changes when a short conversation grows", () => {
    expect(scrollKey(run(1, 10))).not.toBe(scrollKey(run(1, 11)));
  });

  it("is null for a missing or empty list", () => {
    expect(scrollKey(null)).toBeNull();
    expect(scrollKey([])).toBeNull();
  });

  it("ignores OLDER rows inserted at the top (a future 'see earlier' must not yank the reader)", () => {
    expect(scrollKey(run(1, 60))).toBe(scrollKey(run(11, 60)));
  });

  it("counts a system divider at the end, since it is drawn at the end", () => {
    const withDivider: Message[] = [...run(1, 3), { kind: "system", id: "sys-1", conversationId: "secretaria", sentAt: "2026-10-01T13:00:00.000Z", text: "Conversa transferida" }];
    expect(scrollKey(withDivider)).toBe("sys-1");
  });
});
```

Criar `lib/__tests__/portal-window-prune.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { afterSecretariaPoll, mergeThread, type PortalThreadState } from "@/lib/patient-portal";
import type { BubbleMessage } from "@/lib/types";

const at = (seconds: number) => new Date(Date.UTC(2026, 10, 1, 12, 0, seconds)).toISOString();

const mine = (id: string, text: string, seconds: number, status: "enviando" | "entregue" | "falhou", echoOf?: string): BubbleMessage => ({
  kind: "message", id, conversationId: "secretaria", sender: "paciente", sentAt: at(seconds), text, status, ...(echoOf ? { echoOf } : {}),
});
const theirs = (id: string, sender: "paciente" | "secretaria", text: string, seconds: number): BubbleMessage => ({
  kind: "message", id, conversationId: "secretaria", sender, sentAt: at(seconds), text,
});
const state = (local: BubbleMessage[], server: BubbleMessage[] = []): PortalThreadState => ({ server, local, loaded: true, sessionId: null });
// 50 newer rows: the window has slid past everything the patient sent earlier.
const newer = Array.from({ length: 50 }, (_, i) => theirs(`n${i}`, i % 2 ? "paciente" : "secretaria", `novo ${i}`, 1_000 + i));

describe("afterSecretariaPoll", () => {
  it("drops the local copy once the server's copy is on screen", () => {
    const next = afterSecretariaPoll(state([mine("local-1", "oi", 0, "entregue")]), [theirs("srv-1", "paciente", "oi", 1)]);
    expect(next.local).toEqual([]);
    expect(next.loaded).toBe(true);
  });

  it("does not resurface an orphan bubble when 50+ newer rows push the server's copy out of the window", () => {
    const prior = state([mine("local-1", "oi", 0, "entregue")]);
    const confirmed = afterSecretariaPoll(prior, [theirs("srv-1", "paciente", "oi", 1)]);
    const slid = afterSecretariaPoll(confirmed, newer);
    expect(mergeThread(slid.server, slid.local, "secretaria").some((m) => m.id === "local-1")).toBe(false);
    // The control that proves the test bites: without the prune the orphan comes back.
    expect(mergeThread(newer, prior.local, "secretaria").some((m) => m.id === "local-1")).toBe(true);
  });

  it("keeps a send in flight and a failed send (retry needs it), even when the server has the same text", () => {
    const prior = state([mine("local-2", "a", 0, "enviando"), mine("local-3", "b", 0, "falhou")]);
    const next = afterSecretariaPoll(prior, [theirs("s-a", "paciente", "a", 1), theirs("s-b", "paciente", "b", 1)]);
    expect(next.local.map((m) => m.id)).toEqual(["local-2", "local-3"]);
  });

  it("keeps a delivered copy the poll has not brought yet", () => {
    const next = afterSecretariaPoll(state([mine("local-4", "oi", 0, "entregue")]), newer);
    expect(next.local.map((m) => m.id)).toEqual(["local-4"]);
  });

  it("prunes the copy of a tapped button/list row like any other (echoOf, text = the option's label)", () => {
    const next = afterSecretariaPoll(
      state([mine("local-5", "Agendar consulta", 0, "entregue", "card-1")]),
      [theirs("srv-5", "paciente", "Agendar consulta", 1)],
    );
    expect(next.local).toEqual([]);
  });
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `npm test -- lib/__tests__/scroll-key.test.ts lib/__tests__/portal-window-prune.test.ts`
Expected: FAIL — `scrollKey` e `afterSecretariaPoll` não existem.

- [ ] **Step 3: Implement the two pure functions**

`lib/messages.ts`, logo depois de `readCursor`:

```ts
// ── Scroll-to-newest key ──────────────────────────────────────────────────
//
// The patient's poll reads the NEWEST window of the thread (50 rows, secretarIA
// TASK-028): a new reply pushes the oldest row out, so the COUNT stays put and a
// count-keyed scroll never fires. The id of the last row moves on every new
// arrival, and does NOT move when older rows are added at the top.
export function scrollKey(messages: readonly Message[] | null): string | null {
  return messages && messages.length > 0 ? messages[messages.length - 1].id : null;
}
```

`lib/patient-portal.ts`, ao lado de `afterPrecheckPoll`:

```ts
// The secretarIA thread: the server speaks only for its newest window, so a
// local copy must leave `local` the moment a poll proves it. Left in memory it
// would resurface as an orphan bubble once 50+ newer rows push the server's copy
// out of the window. Sends in flight and failed sends are never dropped
// (`unconfirmedLocal` keeps them: "tentar de novo" needs the copy).
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

- [ ] **Step 4: Run to verify they pass**

Run: `npm test -- lib/__tests__/scroll-key.test.ts lib/__tests__/portal-window-prune.test.ts`
Expected: PASS (11 testes).

- [ ] **Step 5: Wire the screens**

`components/patient/PatientMessageList.tsx` — importar `scrollKey` de `@/lib/messages`; trocar as linhas 51-57 por:

```tsx
  const newest = scrollKey(messages);
  const ready = messages !== null && anchor !== null;

  // Newest message in view, as a chat does. Keyed by the id of the last row, not
  // by the count: the Portal reads a sliding window of 50, so a reply leaves the
  // count unchanged (lib/messages.ts::scrollKey).
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [newest, ready]);
```

(remover `const count = messages?.length ?? 0;`, que fica sem uso).

`components/portal/PortalConversation.tsx` — acrescentar `afterSecretariaPoll` ao import que já traz `mergeThread` (linha ~91) e trocar o `setState` do `refresh` (linhas 330-335) por:

```ts
        setState((prev) => {
          const prior = prev[id];
          return { ...prev, [id]: product === "precheck"
            ? afterPrecheckPoll(prior, server, sessionId)
            : afterSecretariaPoll(prior, server) };
        });
```

- [ ] **Step 6: Run gates**

Run: `.\node_modules\.bin\tsc.cmd --noEmit` ; `npm test` ; `npm run build`
Expected: sem erros; testes verdes (a base atual é de ~370 testes; confira que nenhum antigo quebrou).

- [ ] **Step 7: Prova no navegador (é comportamento visual: teste e build sozinhos não provam)**

Abrir a demo `/componentes/portal/` (tela real com brain-api falso, `lib/mock/*`) com uma conversa de mais de 50 mensagens cujo poll devolve a janela deslizante (se a demo não permitir isso, estender o brain-api falso dela com esse poll). Conferir: (a) uma resposta nova entra na vista SEM rolar à mão; (b) rolar para cima e deixar um poll vazio passar NÃO puxa a tela para baixo; (c) mandar uma mensagem e esperar 50+ mensagens novas com a aba aberta NÃO faz reaparecer a bolha órfã. Registrar prints.

- [ ] **Step 8: Commit (dois commits, para poder reverter um sem o outro)**

```bash
git add lib/messages.ts lib/__tests__/scroll-key.test.ts components/patient/PatientMessageList.tsx
git commit -m "fix(portal): scroll to the newest message by id, not by count"
git add lib/patient-portal.ts lib/__tests__/portal-window-prune.test.ts components/portal/PortalConversation.tsx
git commit -m "fix(portal): prune confirmed local copies on every secretarIA poll"
```

---

### Task 1: Regra pura `lib/typing.ts`

**Files:**
- Create: `lib/typing.ts`
- Test: `lib/__tests__/typing.test.ts`

**Interfaces:**
- Produces:
  - `type TypingBy = "automation" | "staff" | "patient"`
  - `type ParsedTyping = { by: TypingBy | null; accepts: boolean }`; `const NO_TYPING: ParsedTyping = { by: null, accepts: false }`
  - `parseTyping(payload: unknown): ParsedTyping` — `by` só é aceito se `typing === true` E `typing_by` é um de `automation|staff|patient`; qualquer outra coisa ⇒ `by: null`. Se `typing === true` mas `typing_by` ausente (produto que só traz o booleano), `by` vira `"automation"`. `accepts = payload.accepts_typing === true`.
  - `typingView(by, product: string, perspective: Perspective): { voice: Sender; label: string } | null`:
    - `null` ⇒ `null`.
    - `automation` ⇒ `voice = PRODUCT_VOICE[product] ?? "clinica"`, `label = "<nome> está digitando"` com `PRODUCT_NAMES[product]` (sem nome: `"A clínica está digitando"`).
    - `staff` ⇒ `voice "clinica"`, `"A clínica está digitando"`; só na perspectiva `paciente` (na `clinica` ⇒ `null`).
    - `patient` ⇒ `voice "paciente"`, `"O paciente está digitando"`; só na perspectiva `clinica` (na `paciente` ⇒ `null`).
  - `lastVoice(messages: readonly Message[] | null): Sender | null` — o `sender` da última linha que NÃO é divisor de sistema (`Message` inclui `kind: "system"`, sem `sender`).
  - `shouldBeat(lastBeatAt: number | null, now: number, hasText: boolean, intervalMs = 3000): boolean`.
  - `beatOnKeystroke(state: { last: number | null }, now: number, text: string, hidden: boolean, send: () => void): void`.
  - `hideStaleTyping(by: TypingBy | null, lastSender: Sender | null): TypingBy | null` — `patient` + última mensagem do `paciente` ⇒ `null`; resto inalterado.
  - `PRODUCT_NAMES: Record<string, string>` = `{ secretaria: "secretarIA", precheck: "PreCheck" }`; `PRODUCT_VOICE: Record<string, Sender>` = `{ secretaria: "secretaria", precheck: "precheck" }`.
- Consumes: `Message`, `Sender` de `lib/types.ts`; `Perspective` de `lib/messages.ts:42` (já existe, `"clinica" | "paciente"` — não redefinir).

- [ ] **Step 1: Write the failing tests**

Criar `lib/__tests__/typing.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { beatOnKeystroke, hideStaleTyping, lastVoice, NO_TYPING, parseTyping, shouldBeat, typingView } from "@/lib/typing";
import type { Message } from "@/lib/types";

describe("parseTyping", () => {
  it("reads the three fields of the contract", () => {
    expect(parseTyping({ typing: true, typing_by: "staff", accepts_typing: true })).toEqual({ by: "staff", accepts: true });
  });
  it("a bare boolean from an older product means the automation", () => {
    expect(parseTyping({ typing: true })).toEqual({ by: "automation", accepts: false });
  });
  it("never conjures an indicator from missing, null or malformed fields", () => {
    expect(parseTyping(null)).toEqual(NO_TYPING);
    expect(parseTyping({})).toEqual(NO_TYPING);
    expect(parseTyping({ typing: "true", typing_by: "staff" })).toEqual(NO_TYPING);
    expect(parseTyping({ typing: true, typing_by: "robot" })).toEqual(NO_TYPING);
    expect(parseTyping({ typing: false, typing_by: "staff" })).toEqual(NO_TYPING);
  });
  it("accepts_typing must be an explicit true", () => {
    expect(parseTyping({ accepts_typing: "yes" }).accepts).toBe(false);
  });
});

describe("typingView", () => {
  it("names the product that is answering, in its own voice", () => {
    expect(typingView("automation", "secretaria", "paciente")).toEqual({ voice: "secretaria", label: "secretarIA está digitando" });
    expect(typingView("automation", "precheck", "clinica")).toEqual({ voice: "precheck", label: "PreCheck está digitando" });
  });
  it("a product nobody registered still works, neutrally", () => {
    expect(typingView("automation", "futuro", "paciente")).toEqual({ voice: "clinica", label: "A clínica está digitando" });
  });
  it("shows staff only to the patient and the patient only to the clinic", () => {
    expect(typingView("staff", "secretaria", "paciente")).toEqual({ voice: "clinica", label: "A clínica está digitando" });
    expect(typingView("staff", "secretaria", "clinica")).toBeNull();
    expect(typingView("patient", "secretaria", "clinica")).toEqual({ voice: "paciente", label: "O paciente está digitando" });
    expect(typingView("patient", "secretaria", "paciente")).toBeNull();
    expect(typingView(null, "secretaria", "paciente")).toBeNull();
  });
});

describe("lastVoice", () => {
  const msg = (id: string, sender: "paciente" | "secretaria"): Message => ({
    kind: "message", id, conversationId: "c", sender, sentAt: "2026-10-01T12:00:00.000Z", text: id,
  });
  const divider: Message = { kind: "system", id: "sys", conversationId: "c", sentAt: "2026-10-01T12:01:00.000Z", text: "Conversa transferida" };
  it("is the voice of the last bubble, skipping a system divider at the end", () => {
    expect(lastVoice([msg("a", "secretaria"), msg("b", "paciente"), divider])).toBe("paciente");
  });
  it("is null for a missing list, an empty one, or one with only dividers", () => {
    expect(lastVoice(null)).toBeNull();
    expect(lastVoice([])).toBeNull();
    expect(lastVoice([divider])).toBeNull();
  });
});

describe("shouldBeat", () => {
  it("beats at most once per interval, never on an empty field", () => {
    expect(shouldBeat(null, 10_000, true)).toBe(true);
    expect(shouldBeat(9_000, 10_000, true)).toBe(false);
    expect(shouldBeat(7_000, 10_000, true)).toBe(true);
    expect(shouldBeat(null, 10_000, false)).toBe(false);
  });
});

describe("beatOnKeystroke", () => {
  it("sends once per interval, never for empty text, never from a hidden tab", () => {
    const state = { last: null as number | null };
    let beats = 0;
    const send = () => { beats += 1; };
    beatOnKeystroke(state, 1_000, "o", false, send);
    beatOnKeystroke(state, 2_000, "oi", false, send); // inside the 3 s window
    beatOnKeystroke(state, 4_500, "oi t", false, send);
    beatOnKeystroke(state, 9_000, "   ", false, send); // blank
    beatOnKeystroke(state, 12_000, "oi tudo", true, send); // hidden tab
    expect(beats).toBe(2);
  });
});

describe("hideStaleTyping", () => {
  it("drops a patient indicator once the patient's message is the newest", () => {
    expect(hideStaleTyping("patient", "paciente")).toBeNull();
    expect(hideStaleTyping("patient", "clinica")).toBe("patient");
    expect(hideStaleTyping("automation", "paciente")).toBe("automation");
    expect(hideStaleTyping("patient", null)).toBe("patient");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm test -- lib/__tests__/typing.test.ts`
Expected: FAIL — módulo `@/lib/typing` inexistente.

- [ ] **Step 3: Implement**

Criar `lib/typing.ts`:

```ts
// The "digitando…" rule, pure and product-agnostic. Everything the screens need to decide
// lives here so it is tested without a DOM: how to read the backend's fields, whose voice and
// which words to show, when a typing heartbeat may go out, and when an indicator is stale.
//
// Universality: no screen branches on a product name. A new product gets its display name and
// voice by ONE entry in the two tables below; an unregistered product still works, neutrally.
import type { Perspective } from "@/lib/messages";
import type { Message, Sender } from "@/lib/types";

export type TypingBy = "automation" | "staff" | "patient";
export type ParsedTyping = { by: TypingBy | null; accepts: boolean };

export const NO_TYPING: ParsedTyping = { by: null, accepts: false };

export const PRODUCT_NAMES: Record<string, string> = { secretaria: "secretarIA", precheck: "PreCheck" };
export const PRODUCT_VOICE: Record<string, Sender> = { secretaria: "secretaria", precheck: "precheck" };

const TYPERS: readonly string[] = ["automation", "staff", "patient"];

// Anything but an explicit `typing: true` is "nobody is typing": a missing or malformed field
// must never conjure a phantom indicator. A product that only sends the boolean (no `typing_by`)
// is, by the contract's history, its automation.
export function parseTyping(payload: unknown): ParsedTyping {
  if (typeof payload !== "object" || payload === null) return NO_TYPING;
  const body = payload as { typing?: unknown; typing_by?: unknown; accepts_typing?: unknown };
  const accepts = body.accepts_typing === true;
  if (body.typing !== true) return { by: null, accepts };
  if (body.typing_by === undefined || body.typing_by === null) return { by: "automation", accepts };
  if (typeof body.typing_by === "string" && TYPERS.includes(body.typing_by)) {
    return { by: body.typing_by as TypingBy, accepts };
  }
  return { by: null, accepts };
}

export function typingView(
  by: TypingBy | null,
  product: string,
  perspective: Perspective,
): { voice: Sender; label: string } | null {
  if (by === "automation") {
    const name = PRODUCT_NAMES[product];
    return { voice: PRODUCT_VOICE[product] ?? "clinica", label: name ? `${name} está digitando` : "A clínica está digitando" };
  }
  if (by === "staff" && perspective === "paciente") return { voice: "clinica", label: "A clínica está digitando" };
  if (by === "patient" && perspective === "clinica") return { voice: "paciente", label: "O paciente está digitando" };
  return null;
}

// The voice of the newest BUBBLE: a system divider ("Conversa transferida") has no sender and
// must not hide, or reveal, an indicator.
export function lastVoice(messages: readonly Message[] | null): Sender | null {
  if (!messages) return null;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message.kind !== "system") return message.sender;
  }
  return null;
}

// One heartbeat per interval while there is text in the box; the WhatsApp rhythm is ~3 s.
export function shouldBeat(lastBeatAt: number | null, now: number, hasText: boolean, intervalMs = 3000): boolean {
  if (!hasText) return false;
  return lastBeatAt === null || now - lastBeatAt >= intervalMs;
}

// Decides and records one heartbeat for a keystroke. `state.last` is the caller's ref.
export function beatOnKeystroke(
  state: { last: number | null },
  now: number,
  text: string,
  hidden: boolean,
  send: () => void,
): void {
  if (hidden || !shouldBeat(state.last, now, text.trim().length > 0)) return;
  state.last = now;
  send();
}

// The patient's indicator outlives their message by up to the server's TTL; once the newest
// message in the thread is theirs, the typing it announced has arrived.
export function hideStaleTyping(by: TypingBy | null, lastSender: Sender | null): TypingBy | null {
  return by === "patient" && lastSender === "paciente" ? null : by;
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `npm test -- lib/__tests__/typing.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/typing.ts lib/__tests__/typing.test.ts
git commit -m "feat(portal): product-agnostic typing rule"
```

---

### Task 2: Componente `TypingBubble`

**Files:**
- Create: `components/console/TypingBubble.tsx`
- Modify: `components/console/console.css`
- Test: `components/console/__tests__/typing-bubble.test.tsx`

**Interfaces:**
- Produces: `export function TypingBubble(props: { voice: Sender; label: string })`. Renderiza `<div className="msg-row"><article className="msg msg--{voice} msg--in msg--first typing"><div className="bubble typing-bubble">` com `<span className="sr-only">{label}</span>` e 3 `<span className="typing-dot" aria-hidden="true">`.

- [ ] **Step 1: Write the failing test**

Criar `components/console/__tests__/typing-bubble.test.tsx` (padrão de `message-bubble-long-text.test.tsx`):

```tsx
import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { TypingBubble } from "../TypingBubble";

describe("TypingBubble", () => {
  it("is an incoming bubble in the voice of whoever is typing, with a screen-reader name", () => {
    const html = renderToStaticMarkup(<TypingBubble voice="secretaria" label="secretarIA está digitando" />);
    expect(html).toContain("msg msg--secretaria msg--in msg--first typing");
    expect(html).toContain("secretarIA está digitando");
    expect(html.match(/typing-dot/g)?.length).toBe(3);
  });

  it("works for every voice, including the patient's, on the clinic's screen", () => {
    expect(renderToStaticMarkup(<TypingBubble voice="paciente" label="O paciente está digitando" />)).toContain("msg--paciente msg--in");
    expect(renderToStaticMarkup(<TypingBubble voice="clinica" label="A clínica está digitando" />)).toContain("msg--clinica msg--in");
    expect(renderToStaticMarkup(<TypingBubble voice="precheck" label="PreCheck está digitando" />)).toContain("msg--precheck msg--in");
  });

  it("hides the dots from assistive technology and does not claim a live role", () => {
    const html = renderToStaticMarkup(<TypingBubble voice="precheck" label="PreCheck está digitando" />);
    expect(html).toContain('aria-hidden="true"');
    expect(html).not.toContain('role="status"');
  });

  it("stops animating under prefers-reduced-motion (the global rule alone would freeze mid-bounce)", () => {
    const css = readFileSync(new URL("../console.css", import.meta.url), "utf8");
    const block = css.slice(css.indexOf("@media (prefers-reduced-motion: reduce)", css.indexOf(".typing-dot")));
    expect(block).toContain("animation: none");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm test -- components/console/__tests__/typing-bubble.test.tsx`
Expected: FAIL — módulo `../TypingBubble` inexistente.

- [ ] **Step 3: Implement**

Criar `components/console/TypingBubble.tsx`:

```tsx
// TypingBubble — "digitando…": the other side is composing a message. Drawn as an incoming
// bubble in the voice of whoever is typing (same tint and tail as MessageBubble's), at the end
// of the thread. Shared by the console and the patient portal, like MessageBubble.
//
// The accessible name is plain text in an `.sr-only` span, NOT a role="status": the thread
// list is already a `role="log" aria-live="polite"`, which announces what is added to it once.
import type { Sender } from "@/lib/types";
import "./console.css";

type Props = {
  voice: Sender;
  // e.g. "secretarIA está digitando" — from lib/typing.ts::typingView.
  label: string;
};

export function TypingBubble({ voice, label }: Props) {
  return (
    <div className="msg-row">
      <article className={`msg msg--${voice} msg--in msg--first typing`}>
        <div className="bubble typing-bubble">
          <span className="sr-only">{label}</span>
          <span className="typing-dot" aria-hidden="true" style={{ ["--i" as string]: 0 }} />
          <span className="typing-dot" aria-hidden="true" style={{ ["--i" as string]: 1 }} />
          <span className="typing-dot" aria-hidden="true" style={{ ["--i" as string]: 2 }} />
        </div>
      </article>
    </div>
  );
}
```

Acrescentar ao fim de `components/console/console.css` (a cor dos pontos segue a hora da bolha, `.bubble-time { color: var(--ink); opacity: .6 }` na linha 169 — `--ink-muted` NÃO existe; `.sr-only` já existe em `app/styles/base.css`):

```css
/* "digitando…" - three dots in the ink the bubble's own timestamp uses. */
.typing-bubble {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-height: 20px;
}
.typing-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ink);
  opacity: 0.45;
  animation: typingBounce 1.2s infinite ease-in-out;
  animation-delay: calc(var(--i, 0) * 0.15s);
}
@keyframes typingBounce {
  0%, 60%, 100% { opacity: 0.45; transform: none; }
  30% { opacity: 1; transform: translateY(-3px); }
}
@media (prefers-reduced-motion: reduce) {
  .typing-dot { animation: none; opacity: 0.7; }
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `npm test -- components/console/__tests__/typing-bubble.test.tsx`
Expected: PASS (4 testes).

- [ ] **Step 5: Commit**

```bash
git add components/console/TypingBubble.tsx components/console/console.css components/console/__tests__/typing-bubble.test.tsx
git commit -m "feat(portal): TypingBubble component"
```

---

### Task 3: O teclado avisa — gancho `onTyping` no `Composer`

**Files:**
- Modify: `components/console/Composer.tsx` (Props linha 47; desestruturação linha 65; estado linha 74; `submit` linha 108; `onChange` linha 181)

**Interfaces:**
- Consumes: `beatOnKeystroke` (Task 1).
- Produces: `Composer` ganha a prop opcional `onTyping?: () => void`, chamada pelo `onChange` do campo de texto no ritmo de `beatOnKeystroke` (1 por 3 s, nunca vazio, nunca aba escondida); zera o ritmo ao enviar.

- [ ] **Step 1: Implement**

(A regra de decisão já está testada na Task 1; o `Composer` só a liga. `renderToStaticMarkup` não executa handlers, então não há teste de componente para o `onChange`; a prova é o gate de tipos + a prova ao vivo da Task 6.)

Em `Composer.tsx`:

- importar `beatOnKeystroke` de `@/lib/typing`;
- em `Props` (linha 47-61), acrescentar `// Fired at most once per 3 s while there is text in the box (lib/typing.ts). onTyping?: () => void;` e incluir `onTyping` na desestruturação de `Composer({ … })` (linha 65-73);
- junto dos outros `useRef` (linha 78-81): `const beat = useRef<{ last: number | null }>({ last: null });`
- trocar o `onChange` do campo de texto (linha 181, hoje `onChange={(e) => setText(e.target.value)}`) por:

```tsx
          onChange={(e) => {
            setText(e.target.value);
            if (onTyping) beatOnKeystroke(beat.current, Date.now(), e.target.value, document.hidden, onTyping);
          }}
```

- em `submit()` (linha 106-109), logo depois de `setText((current) => (current === sentText ? "" : current));`, acrescentar `beat.current.last = null;` (o próximo texto digitado avisa de novo imediatamente).

- [ ] **Step 2: Run gates**

Run: `.\node_modules\.bin\tsc.cmd --noEmit` ; `npm test`
Expected: sem erros; testes verdes.

- [ ] **Step 3: Commit**

```bash
git add components/console/Composer.tsx
git commit -m "feat(portal): composer reports that someone is typing"
```

---

### Task 4: Portal do paciente — ler, mostrar e avisar

**Files:**
- Modify: `lib/patient-portal.ts` (`PortalThreadState` linha 296; `afterSecretariaPoll` da Task 0; `afterPrecheckPoll`/`afterPrecheckPost` linhas 307-350)
- Modify: `lib/real/patient-access.ts` (nova função junto de `markThreadRead`, linha ~655)
- Modify: `components/portal/PortalConversation.tsx` (`refresh()` linhas 319-369; literais das linhas 475 e 519; render linhas 712-740)
- Modify: `components/patient/PatientMessageList.tsx`
- Test: `lib/__tests__/patient-portal.test.ts`, `lib/__tests__/portal-window-prune.test.ts`

**Interfaces:**
- Consumes: `parseTyping`, `typingView`, `hideStaleTyping`, `lastVoice`, `NO_TYPING`, `ParsedTyping` (Task 1); `onTyping` do `Composer` (Task 3); `TypingBubble` (Task 2); `scrollKey`/`afterSecretariaPoll` (Task 0).
- Produces:
  - `PortalThreadState` ganha `typing: ParsedTyping`; todo literal `{ server: [], local: [], loaded: true, sessionId: null }` ganha `typing: NO_TYPING` (`patient-portal.ts` linha 330; `PortalConversation.tsx` linhas 475 e 519; os testes em `lib/__tests__/` — o compilador aponta cada um: `.\node_modules\.bin\tsc.cmd --noEmit`).
  - `afterSecretariaPoll(prior, server, typing: ParsedTyping)` e `afterPrecheckPoll(prior, server, sessionId, typing: ParsedTyping)` gravam `typing`; `afterPrecheckPost` preserva `prior?.typing ?? NO_TYPING` (e devolve `NO_TYPING` no ramo `reset`).
  - `export async function sendTypingBeat(tenantId: string, product: string): Promise<void>` em `lib/real/patient-access.ts`: `POST /patient-access/threads/${product}/typing` pelo mesmo cliente autenticado por clínica do `pollMessages`; qualquer erro é engolido (decoração).
  - `PatientMessageList` ganha `typing?: { voice: Sender; label: string } | null`.

- [ ] **Step 1: Write the failing tests**

Ajustar todo literal de `PortalThreadState` e as chamadas de `afterSecretariaPoll`/`afterPrecheckPoll` nos testes existentes e em `portal-window-prune.test.ts` (passar `NO_TYPING` como último argumento). Em `lib/__tests__/patient-portal.test.ts`, no bloco `durable PreCheck conversation`, acrescentar: (1) `afterPrecheckPoll(prior, server, …, { by: "staff", accepts: true })` devolve um estado cujo `typing` é exatamente esse objeto; (2) `afterPrecheckPost(prior, …)` com `prior.typing = { by: "automation", accepts: false }` devolve o mesmo `typing`. E (3) `afterSecretariaPoll(prior, server, { by: "staff", accepts: true }).typing` idem. E um teste de contrato do corpo:

```ts
import { parseTyping } from "@/lib/typing";

describe("a poll body carries the typing state for any product", () => {
  it("is read the same way for the secretaria and the precheck bodies", () => {
    expect(parseTyping({ data: [], has_more: false, typing: true, typing_by: "staff", accepts_typing: true })).toEqual({ by: "staff", accepts: true });
    expect(parseTyping({ items: [], state: "active", typing: true })).toEqual({ by: "automation", accepts: false });
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm test -- lib/__tests__/patient-portal.test.ts lib/__tests__/portal-window-prune.test.ts`
Expected: FAIL por tipo/assinatura (`typing` não existe em `PortalThreadState`).

- [ ] **Step 3: Implement the state and the call**

`lib/patient-portal.ts`: importar `NO_TYPING` e `type ParsedTyping` de `@/lib/typing`; `PortalThreadState` ganha `typing: ParsedTyping;`; `afterSecretariaPoll` e `afterPrecheckPoll` recebem um parâmetro final `typing: ParsedTyping` e o gravam; `afterPrecheckPost` mantém `typing: prior?.typing ?? NO_TYPING` (e `NO_TYPING` no `if (status === "reset")`).

`lib/real/patient-access.ts`, junto de `markThreadRead`:

```ts
// The "I am typing" heartbeat. Decoration: any failure (network, 401, an older brain-api
// without the route) is silent — it must never become an error banner or stop the poll.
export async function sendTypingBeat(tenantId: string, product: string): Promise<void> {
  try {
    await clinicCall<unknown>(tenantId, `/patient-access/threads/${encodeURIComponent(product)}/typing`, { method: "POST" });
  } catch {
    /* decoration */
  }
}
```

(`clinicCall<T>` é genérico, linha 610; é o mesmo helper que `pollMessages` e `markThreadRead` usam.)

- [ ] **Step 4: Wire the screen**

`PortalConversation.tsx`, em `refresh(product)`, guardar o estado de digitação do MESMO `relay.payload` que alimenta `fromTranscript` (linha 325) e passá-lo às duas funções do `setState` da Task 0:

```ts
        const typing = parseTyping(relay.payload);
        // ...
        setState((prev) => {
          const prior = prev[id];
          return { ...prev, [id]: product === "precheck"
            ? afterPrecheckPoll(prior, server, sessionId, typing)
            : afterSecretariaPoll(prior, server, typing) };
        });
```

Os dois literais de estado inicial (linhas 475 e 519) ganham `typing: NO_TYPING`.

No render, depois de `messages` ser calculado (linha 631-633) e antes dos retornos antecipados (valores simples, não hooks):

```tsx
const parsed = thread?.typing ?? NO_TYPING;
const view = typingView(hideStaleTyping(parsed.by, lastVoice(messages)), channel, "paciente");
```

(`messages` é a lista final já com os toques aplicados; `lastVoice` pula divisor de sistema; `channel` é o produto da aba aberta — a string, sem `if` por produto.) No `<PatientMessageList … />` (linha 712): `typing={view}`. No `<Composer … />` (linha 729): `onTyping={thread?.typing.accepts ? () => void sendTypingBeat(tenantId, channel) : undefined}`.

`PatientMessageList.tsx`: importar `TypingBubble` e `Sender`; prop `typing?: { voice: Sender; label: string } | null` (default `null`); a dependência do efeito de rolagem da Task 0 vira `[newest, ready, Boolean(typing)]`; imediatamente antes de `<div ref={bottomRef} />`:

```tsx
      {ready && typing && <TypingBubble voice={typing.voice} label={typing.label} />}
```

- [ ] **Step 5: Run gates**

Run: `npm test` ; `.\node_modules\.bin\tsc.cmd --noEmit` ; `npm run build`
Expected: tudo verde.

- [ ] **Step 6: Commit**

```bash
git add lib/patient-portal.ts lib/real/patient-access.ts components/portal/PortalConversation.tsx components/patient/PatientMessageList.tsx lib/__tests__/patient-portal.test.ts lib/__tests__/portal-window-prune.test.ts
git commit -m "feat(portal): typing indicator and heartbeat in the patient conversation"
```

---

### Task 5: Console da clínica — ler e avisar

**Files:**
- Modify: `lib/console-api.ts` (interface, perto de `listMessages`, linha 105)
- Modify: `lib/real/console-api.real.ts` (helper exportado + métodos no objeto retornado, depois de `listMessages`, linha 413)
- Modify: `lib/mock/console-api.mock.ts` (perto de `listMessages`, linha 249)
- Modify: `components/console/ChatScreen.tsx` (estado ao lado de `messages`, linha 96; reset na linha 162; poll linhas 168-191; `<Thread>` linha 429)
- Modify: `components/console/Thread.tsx` (Props linha 35; função linha 68; efeito de rolagem linhas 77-79; render antes da linha 197; `<Composer>` linha 210)
- Test: `lib/real/__tests__/console-typing.test.ts`

**Interfaces:**
- Produces: `ConsoleApi.getTyping?(conversationId: string): Promise<TypingBy | null>` e `ConsoleApi.sendTyping?(conversationId: string): Promise<void>` (ambos OPCIONAIS: uma API que não sabe não mostra nada). Real: `GET`/`POST /tenants/me/conversations/{id}/typing` via `hubFetch` (resposta `{typing, by}` / `{applied}`); conversa PreCheck (`isPrecheckConversationId`) ⇒ `null`/no-op sem rede; qualquer erro ⇒ `null`/silêncio. Mock: `null`/no-op.

- [ ] **Step 1: Write the failing test**

Criar `lib/real/__tests__/console-typing.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { readTypingBy } from "@/lib/real/console-api.real";

describe("readTypingBy", () => {
  it("returns who is typing only for the two values the clinic can see", async () => {
    expect(await readTypingBy(async () => ({ typing: true, by: "patient" }))).toBe("patient");
    expect(await readTypingBy(async () => ({ typing: true, by: "automation" }))).toBe("automation");
  });
  it("is null for staff (your own typing), unknown values, a false flag or a missing body", async () => {
    expect(await readTypingBy(async () => ({ typing: true, by: "staff" }))).toBeNull();
    expect(await readTypingBy(async () => ({ typing: true, by: "robot" }))).toBeNull();
    expect(await readTypingBy(async () => ({ typing: false, by: "patient" }))).toBeNull();
    expect(await readTypingBy(async () => ({}) as { typing: boolean; by?: string })).toBeNull();
  });
  it("swallows any failure: an indicator is never worth an error banner", async () => {
    expect(await readTypingBy(async () => { throw new Error("boom"); })).toBeNull();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm test -- lib/real/__tests__/console-typing.test.ts`
Expected: FAIL — `readTypingBy` não exportada.

- [ ] **Step 3: Implement the client**

`lib/real/console-api.real.ts` (importar `type TypingBy` de `@/lib/typing`):

```ts
// The "digitando" flag is decoration: any failure (network, 401, 404, an older hub without the
// route) reads as "nobody typing" and must never reach the error banner or stop the thread poll.
// `staff` is the clinic's OWN typing and is never shown back to it.
export async function readTypingBy(
  fetchFlag: () => Promise<{ typing: boolean; by?: string | null }>,
): Promise<TypingBy | null> {
  try {
    const body = await fetchFlag();
    if (body.typing !== true) return null;
    return body.by === "patient" || body.by === "automation" ? body.by : null;
  } catch {
    return null;
  }
}
```

e no objeto retornado, logo depois de `listMessages` (linha 413):

```ts
    getTyping: (conversationId: string) =>
      isPrecheckConversationId(conversationId)
        ? Promise.resolve(null)
        : readTypingBy(async () => {
            const s = await session();
            return hubFetch<{ typing: boolean; by?: string | null }>(s, `/tenants/me/conversations/${encodeURIComponent(conversationId)}/typing`);
          }),

    sendTyping: async (conversationId: string) => {
      if (isPrecheckConversationId(conversationId)) return;
      try {
        const s = await session();
        await hubFetch<{ applied: boolean }>(s, `/tenants/me/conversations/${encodeURIComponent(conversationId)}/typing`, { method: "POST" });
      } catch {
        /* decoration */
      }
    },
```

(Mesma forma de `sendMessage`, linha 444: `hubFetch<T>(s, path, { method: "POST", … })`; `isPrecheckConversationId` e `session()` já existem no arquivo.) Em `lib/console-api.ts` (perto da linha 105) acrescentar à interface, com comentário: `getTyping?(conversationId: string): Promise<TypingBy | null>;` e `sendTyping?(conversationId: string): Promise<void>;` (importar `type TypingBy`). No mock (perto da linha 249): `async getTyping() { return null; }, async sendTyping() {},`.

- [ ] **Step 4: Wire ChatScreen and Thread**

`ChatScreen.tsx`: `const [typing, setTyping] = useState<TypingBy | null>(null);` ao lado de `messages` (linha 96); `setTyping(null);` no reset do efeito (linha 162, junto de `setMessages(null)`). No `load` do poll (linhas 168-191), UMA vez depois do `if (first) {…} else {…}` e antes de `setOffline(false)`:

```ts
        // Decoration: `getTyping` never rejects (readTypingBy swallows), so it cannot fail the poll.
        const by = api.getTyping ? await api.getTyping(selectedId) : null;
        if (cancelled) return;
        setTyping(by);
```

No `<Thread … />` (linha 429-431) acrescentar `typing={typing}` e `onTypingBeat={api.sendTyping ? () => void api.sendTyping!(selectedId) : undefined}`.

`Thread.tsx`: `typing: TypingBy | null;` e `onTypingBeat?: () => void;` em `Props` (linha 35-57) e na desestruturação (linha 68); importar `TypingBubble`, `typingView`, `hideStaleTyping`, `lastVoice`, `type TypingBy`; o efeito de rolagem (linhas 77-79) vira `[count, conversation.id, Boolean(typing)]` (o console lê a lista inteira do hub, então a contagem continua correta ali); calcular e renderizar antes de `<div ref={bottomRef} />` (linha 197):

```tsx
  const view = typingView(hideStaleTyping(typing, lastVoice(messages)), conversation.product, "clinica");
  // ...
      {view && <TypingBubble voice={view.voice} label={view.label} />}
```

e no `<Composer … />` (linha 210-216): `onTyping={onTypingBeat}`. (`conversation.product` é `ConversationProduct` = `"secretaria" | "precheck"`; passa como string.)

- [ ] **Step 5: Run gates**

Run: `npm test` ; `.\node_modules\.bin\tsc.cmd --noEmit` ; `npm run build`
Expected: tudo verde.

- [ ] **Step 6: Commit**

```bash
git add lib/console-api.ts lib/real/console-api.real.ts lib/mock/console-api.mock.ts lib/real/__tests__/console-typing.test.ts components/console/ChatScreen.tsx components/console/Thread.tsx
git commit -m "feat(console): typing indicator and heartbeat in the clinic thread"
```

---

### Task 6: Documentação e prova ao vivo (depois do backend no ar e do deploy do front autorizado)

- [ ] **Step 1:** `docs/CHECKPOINT_digitando.md` no repo do front (o que entrou, o contrato lido, a regra "um produto novo = uma entrada em `lib/typing.ts`", decisões: sem `role="status"`, `animation: none`, ganchos opcionais, rolagem por id e poda no poll) e uma linha no `CLAUDE.md` > "Documentação".
- [ ] **Step 2:** Atualizar a seção "Dependência do front" e o passo 3 do §7 de `secretarIA/docs/CHECKPOINT_portal_mensagens_recentes.md` para o estado real (Task 0 feita/provada) e marcar `z_prompts/PROMPT_PORTAL_SCROLL_E_COPIAS_LOCAIS_JANELA_RECENTE.md` como "ABSORVIDO pela Task 0 do plano D".
- [ ] **Step 3 (Task 0, em produção):** conta de QA com conversa de mais de 50 mensagens (a mesma da Task 3 do plano de mensagens recentes): mandar uma mensagem que acione a LLM ⇒ a resposta nova entra na vista em ≤ ~10 s **sem rolar à mão**; F5 mantém a mensagem enviada; sem bolha órfã.
- [ ] **Step 4 (digitando):** prova com o agent-browser (duas sessões: paciente no Portal e clínica no console logado): (a) o paciente envia uma pergunta que acione a LLM ⇒ pontinhos da secretarIA aparecem no Portal em ≤ 4 s e somem com a resposta; o console mostra os mesmos; (b) a clínica assume a conversa e digita no console ⇒ o Portal mostra "A clínica está digitando" em ≤ 4 s; (c) com humano conduzindo, o paciente digita no Portal ⇒ o console mostra "O paciente está digitando" e some ≤ 6 s depois de parar; (d) com a automação conduzindo, o Portal NÃO manda batimento (`network requests` sem `/typing`); (e) `prefers-reduced-motion` emulado ⇒ pontos estáticos; tema claro e escuro.
- [ ] **Step 5:** Registrar prints e a lista de requisições no CHECKPOINT.
