# "Digitando…" universal — Frontend (Brain-Message-Frontend) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Os três pontinhos "digitando…" aparecem, como no WhatsApp, sempre que a outra parte está digitando: no Portal do paciente (a automação ou a equipe da clínica digitando) e no console da clínica (a automação ou o paciente digitando); e o próprio teclado do paciente e da equipe avisa que está digitando. Vale para QUALQUER produto (hoje secretarIA e PreCheck; um produto novo não exige mudança aqui).

**Architecture:** Toda a regra mora num módulo puro e testável, `lib/typing.ts` (ler o campo do corpo, escolher voz e rótulo, decidir quando um batimento deve sair, esconder um indicador obsoleto). Um componente compartilhado `TypingBubble` desenha a bolha (mesma cor/rabinho da voz de quem digita). O `Composer` (já compartilhado por paciente e clínica) ganha um gancho `onTyping` com limite de 1 batimento a cada 3 s. O Portal lê `typing/typing_by/accepts_typing` do corpo que já consulta a cada 4 s e só manda batimento quando `accepts_typing` (um humano conduz); o console usa duas chamadas novas e opcionais (`getTyping`, `sendTyping`). Servidor sem o campo = nunca mostra e nunca quebra.

**Tech Stack:** Next.js (static export), TypeScript, CSS à mão com tokens, vitest (ambiente `node`; componentes testados com `renderToStaticMarkup`).

**Spec:** Decisões do dono (2026-10-01): "tem que ser universal igual WhatsApp, qualquer interação que esteja digitando tem que aparecer; deixa isso fixo; se tiver outro produto, tem que funcionar para ele também"; "o paciente digitando só importa quando um humano da clínica está na conversa — com a automação conduzindo não precisa". Contrato do backend: `secretarIA/docs/superpowers/plans/2026-10-01-digitando-backend.md` (campos `typing`, `typing_by` ∈ `automation|staff`, `accepts_typing` na listagem do Portal; `POST /patient-access/threads/{product}/typing`; no console `GET`/`POST /tenants/me/conversations/{id}/typing`).

## Global Constraints

- Gates: `.\node_modules\.bin\tsc.cmd --noEmit`, `npm test`, `npm run build` (nunca `npx tsc`; sem ESLint). Branch `main`; commit local permitido; push/deploy só com pedido explícito do dono.
- `app/styles/tokens.css` é o ÚNICO lugar com hex: componente e CSS usam só `var(--...)`.
- Nenhum `fetch` em componente: toda chamada passa por `ConsoleApi` (`lib/console-api.ts`) ou `lib/real/*`.
- Texto de UI em português (sentence case); código e comentários em inglês.
- Acessibilidade: o nome acessível é texto num `<span className="sr-only">`; pontos `aria-hidden`; sem `role="status"` (a lista já é `role="log" aria-live="polite"`). Respeitar `prefers-reduced-motion` com `animation: none` (a regra global só encurta a duração e deixaria os pontos congelados no meio).
- Regra de perspectiva do repo: a bolha de digitando é SEMPRE do outro lado (`msg--in`) e usa a voz de quem digita (`Sender` = `"paciente" | "clinica" | "secretaria" | "precheck"`, classe `msg--<voz>`). Nunca desenhar moldura de celular nem card.
- Universalidade: nenhum `if (product === "secretaria")` fora de `lib/typing.ts`. Produto desconhecido cai numa voz neutra (`clinica`) e no rótulo "A clínica está digitando"; produto novo ganha nome/voz acrescentando UMA entrada em `lib/typing.ts::PRODUCT_NAMES`/`PRODUCT_VOICE`.
- Batimento: no máximo 1 a cada 3 000 ms por conversa; nunca enquanto o campo está vazio; nunca com a aba escondida; parar sozinho ao enviar.
- Deploy: depois do backend (secretaria_api + worker) e do brain-api. Servidor sem os campos = comportamento de hoje.

## Review Focus

- Campo ausente/`null`/de tipo errado no corpo: "não está digitando" (nunca indicador fantasma).
- O paciente nunca vê o próprio "digitando"; o console nunca vê "equipe digitando" de si mesmo (o backend já filtra; o front não tem como mostrar `staff` no console e ignora).
- "Paciente digitando" some quando a última mensagem da conversa já é do paciente (a mensagem chegou).
- Trocar de aba/produto/conversa: o indicador de uma não aparece na outra.
- Conversa PreCheck no console: sem `getTyping`/`sendTyping` (produto só de automação; o backend responde "não aplicado").
- Batimento só quando `accepts_typing` (Portal); falha de rede/401/404 em batimento ou leitura: silêncio, nunca banner nem queda do poll de mensagens.
- Reduced motion: pontos estáticos e legíveis; tema claro e escuro: contraste do ponto sobre cada voz.
- Rolagem: o indicador entra na rolagem para o fim como uma mensagem, sem pular a tela.

## File Structure

- Create `lib/typing.ts` — regra pura (tipos, `parseTyping`, `typingView`, `shouldBeat`, `beatOnKeystroke`, `hideStaleTyping`, `PRODUCT_NAMES`, `PRODUCT_VOICE`).
- Create `lib/__tests__/typing.test.ts`.
- Create `components/console/TypingBubble.tsx`; modify `components/console/console.css`.
- Create `components/console/__tests__/typing-bubble.test.tsx`.
- Modify `components/console/Composer.tsx` — gancho `onTyping`.
- Modify `lib/patient-portal.ts` — `PortalThreadState.typing`, ajustes em `afterPrecheckPoll/afterPrecheckPost`.
- Modify `lib/real/patient-access.ts` — `sendTypingBeat(tenantId, product)`.
- Modify `components/portal/PortalConversation.tsx` — ler o corpo, mandar batimento, passar à lista e ao compositor.
- Modify `components/patient/PatientMessageList.tsx` — prop `typing`.
- Modify `lib/console-api.ts`, `lib/real/console-api.real.ts`, `lib/mock/console-api.mock.ts` — `getTyping?`, `sendTyping?`.
- Modify `components/console/ChatScreen.tsx`, `components/console/Thread.tsx`.
- Modify `lib/__tests__/patient-portal.test.ts`.

---

### Task 1: Regra pura `lib/typing.ts`

**Files:**
- Create: `lib/typing.ts`
- Test: `lib/__tests__/typing.test.ts`

**Interfaces:**
- Produces:
  - `type TypingBy = "automation" | "staff" | "patient"`
  - `type ParsedTyping = { by: TypingBy | null; accepts: boolean }`
  - `type Perspective = "paciente" | "clinica"` (quem está olhando a tela)
  - `parseTyping(payload: unknown): ParsedTyping` — `by` só é aceito se `typing === true` E `typing_by` é um de `automation|staff|patient`; qualquer outra coisa ⇒ `by: null`. Se `typing === true` mas `typing_by` ausente (produto que só traz o booleano), `by` vira `"automation"`. `accepts = payload.accepts_typing === true`.
  - `typingView(by, product: string, perspective): { voice: Sender; label: string } | null`:
    - `null` ⇒ `null`.
    - `automation` ⇒ `voice = PRODUCT_VOICE[product] ?? "clinica"`, `label = "<nome> está digitando"` com `PRODUCT_NAMES[product]` (sem nome: `"A clínica está digitando"`).
    - `staff` ⇒ `voice "clinica"`, `"A clínica está digitando"`; só na perspectiva `paciente` (na `clinica` ⇒ `null`).
    - `patient` ⇒ `voice "paciente"`, `"O paciente está digitando"`; só na perspectiva `clinica` (na `paciente` ⇒ `null`).
  - `shouldBeat(lastBeatAt: number | null, now: number, hasText: boolean, intervalMs = 3000): boolean`.
  - `beatOnKeystroke(state: { last: number | null }, now: number, text: string, hidden: boolean, send: () => void): void`.
  - `hideStaleTyping(by: TypingBy | null, lastSender: Sender | null): TypingBy | null` — `patient` + última mensagem do `paciente` ⇒ `null`; resto inalterado.
  - `PRODUCT_NAMES: Record<string, string>` = `{ secretaria: "secretarIA", precheck: "PreCheck" }`; `PRODUCT_VOICE: Record<string, Sender>` = `{ secretaria: "secretaria", precheck: "precheck" }`.
- Consumes: `Sender` de `lib/types.ts:167`.

- [ ] **Step 1: Write the failing tests**

Criar `lib/__tests__/typing.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { beatOnKeystroke, hideStaleTyping, parseTyping, shouldBeat, typingView } from "@/lib/typing";

describe("parseTyping", () => {
  it("reads the three fields of the contract", () => {
    expect(parseTyping({ typing: true, typing_by: "staff", accepts_typing: true })).toEqual({ by: "staff", accepts: true });
  });
  it("a bare boolean from an older product means the automation", () => {
    expect(parseTyping({ typing: true })).toEqual({ by: "automation", accepts: false });
  });
  it("never conjures an indicator from missing, null or malformed fields", () => {
    expect(parseTyping(null)).toEqual({ by: null, accepts: false });
    expect(parseTyping({})).toEqual({ by: null, accepts: false });
    expect(parseTyping({ typing: "true", typing_by: "staff" })).toEqual({ by: null, accepts: false });
    expect(parseTyping({ typing: true, typing_by: "robot" })).toEqual({ by: null, accepts: false });
    expect(parseTyping({ typing: false, typing_by: "staff" })).toEqual({ by: null, accepts: false });
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
import type { Sender } from "@/lib/types";

export type TypingBy = "automation" | "staff" | "patient";
export type ParsedTyping = { by: TypingBy | null; accepts: boolean };
// Who is LOOKING at the screen.
export type Perspective = "paciente" | "clinica";

export const PRODUCT_NAMES: Record<string, string> = { secretaria: "secretarIA", precheck: "PreCheck" };
export const PRODUCT_VOICE: Record<string, Sender> = { secretaria: "secretaria", precheck: "precheck" };

const TYPERS: readonly string[] = ["automation", "staff", "patient"];

// Anything but an explicit `typing: true` is "nobody is typing": a missing or malformed field
// must never conjure a phantom indicator. A product that only sends the boolean (no `typing_by`)
// is, by the contract's history, its automation.
export function parseTyping(payload: unknown): ParsedTyping {
  if (typeof payload !== "object" || payload === null) return { by: null, accepts: false };
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

Acrescentar ao fim de `components/console/console.css` (antes, `grep -n "sr-only\|timestamp\|msg-meta" components/console/console.css app/styles/*.css` para achar o token de cor que a hora da bolha usa e a classe `.sr-only`; usar esse token no lugar de `var(--ink-muted)` abaixo; se `.sr-only` não existir fora do console, reaproveitar a classe equivalente do repo):

```css
/* "digitando…" - three dots in the colour the bubble's own timestamp uses. */
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
  background: var(--ink-muted);
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
- Modify: `components/console/Composer.tsx` (Props linha 47; estado perto da linha 74; `onChange` da linha 181)

**Interfaces:**
- Consumes: `beatOnKeystroke` (Task 1).
- Produces: `Composer` ganha a prop opcional `onTyping?: () => void`, chamada pelo `onChange` do campo de texto no ritmo de `beatOnKeystroke` (1 por 3 s, nunca vazio, nunca aba escondida); zera o ritmo ao enviar.

- [ ] **Step 1: Implement**

(A regra de decisão já está testada na Task 1; o `Composer` só a liga. `renderToStaticMarkup` não executa handlers, então não há teste de componente para o `onChange`; a prova é o gate de tipos + a prova ao vivo da Task 6.)

Em `Composer.tsx`: importar `beatOnKeystroke` de `@/lib/typing`; adicionar `onTyping?: () => void;` em `Props` (e na desestruturação de `Composer({ … })`); `const beat = useRef<{ last: number | null }>({ last: null });` junto dos outros `useState`/`useRef`; trocar o `onChange` do campo de texto (linha ~181) por:

```tsx
          onChange={(e) => {
            setText(e.target.value);
            if (onTyping) beatOnKeystroke(beat.current, Date.now(), e.target.value, document.hidden, onTyping);
          }}
```

e, no ponto em que o envio é concluído e o texto é limpo (`setText("")`), acrescentar `beat.current.last = null;`.

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
- Modify: `lib/patient-portal.ts` (`PortalThreadState` linha 296; `afterPrecheckPoll`/`afterPrecheckPost`)
- Modify: `lib/real/patient-access.ts` (nova função junto de `pollMessages`, linha ~643)
- Modify: `components/portal/PortalConversation.tsx` (`refresh()` ~319-369; literais de estado; render ~711-722; `Composer`)
- Modify: `components/patient/PatientMessageList.tsx`
- Test: `lib/__tests__/patient-portal.test.ts`

**Interfaces:**
- Consumes: `parseTyping`, `typingView`, `hideStaleTyping`, `ParsedTyping` (Task 1); `onTyping` do `Composer` (Task 3); `TypingBubble` (Task 2).
- Produces:
  - `PortalThreadState` ganha `typing: ParsedTyping`; todo literal `{ server: [], local: [], loaded: true, sessionId: null }` ganha `typing: { by: null, accepts: false }`.
  - `afterPrecheckPoll(prior, server, …, typing: ParsedTyping)` grava `typing`; `afterPrecheckPost` preserva `prior?.typing ?? { by: null, accepts: false }`.
  - `export async function sendTypingBeat(tenantId: string, product: string): Promise<void>` em `lib/real/patient-access.ts`: `POST /patient-access/threads/${product}/typing` pelo mesmo cliente autenticado por clínica do `pollMessages`; qualquer erro é engolido (decoração).
  - `PatientMessageList` ganha `typing?: { voice: Sender; label: string } | null`.

- [ ] **Step 1: Write the failing tests**

Em `lib/__tests__/patient-portal.test.ts`, ajustar todo literal de `PortalThreadState` (o compilador aponta cada um: `.\node_modules\.bin\tsc.cmd --noEmit`) e acrescentar, no bloco `durable PreCheck conversation`, dois testes: (1) `afterPrecheckPoll(prior, server, …, { by: "staff", accepts: true })` devolve um estado cujo `typing` é exatamente esse objeto; (2) `afterPrecheckPost(prior, …)` com `prior.typing = { by: "automation", accepts: false }` devolve o mesmo `typing`. E um teste de contrato do corpo:

```ts
import { parseTyping } from "@/lib/typing";

describe("a poll body carries the typing state for any product", () => {
  it("is read the same way for the secretaria and the precheck bodies", () => {
    expect(parseTyping({ data: [], typing: true, typing_by: "staff", accepts_typing: true })).toEqual({ by: "staff", accepts: true });
    expect(parseTyping({ items: [], state: "active", typing: true })).toEqual({ by: "automation", accepts: false });
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm test -- lib/__tests__/patient-portal.test.ts`
Expected: FAIL por tipo/assinatura (`typing` não existe em `PortalThreadState`).

- [ ] **Step 3: Implement the state and the call**

`lib/patient-portal.ts`: `PortalThreadState` ganha `typing: ParsedTyping;` (importar o tipo de `@/lib/typing`); `afterPrecheckPoll` recebe um parâmetro final `typing: ParsedTyping` e o grava; `afterPrecheckPost` mantém `prior?.typing ?? { by: null, accepts: false }`.

`lib/real/patient-access.ts`, junto de `pollMessages`:

```ts
// The "I am typing" heartbeat. Decoration: any failure (network, 401, an older brain-api
// without the route) is silent — it must never become an error banner or stop the poll.
export async function sendTypingBeat(tenantId: string, product: string): Promise<void> {
  try {
    await clinicCall(tenantId, `/patient-access/threads/${encodeURIComponent(product)}/typing`, { method: "POST" });
  } catch {
    /* decoration */
  }
}
```

(Usar o mesmo helper de chamada autenticada por clínica que `pollMessages` usa — conferir o nome real em `lib/real/patient-access.ts:643-646` e copiar a forma da chamada; o contrato exigido é o do comentário.)

- [ ] **Step 4: Wire the screen**

`PortalConversation.tsx`, em `refresh(product)`, guardar o estado de digitação do MESMO `relay.payload` que alimenta `fromTranscript`:

```ts
const typing = parseTyping(relay.payload);
// secretaria branch:
setState((prev) => ({ ...prev, [id]: { server, local: prev[id]?.local ?? [], loaded: true, sessionId: null, typing } }));
// precheck branch: pass `typing` as the new last argument of afterPrecheckPoll(...)
```

No render, onde `PatientMessageList` é montado:

```tsx
const parsed = thread?.typing ?? { by: null, accepts: false };
const lastSender = merged.length ? merged[merged.length - 1].sender : null;
const view = typingView(hideStaleTyping(parsed.by, lastSender), channel, "paciente");
// ...
<PatientMessageList … typing={view} />
```

(`merged` é o resultado de `mergeThread(...)` já calculado na linha ~628; `channel` é o produto da aba aberta — a string, sem `if` por produto.)

No `Composer` do portal: `onTyping={thread?.typing.accepts ? () => void sendTypingBeat(tenantId, channel) : undefined}`.

`PatientMessageList.tsx`: importar `TypingBubble` e `Sender`; prop `typing?: { voice: Sender; label: string } | null` (default `null`); `[count, ready, Boolean(typing)]` nas dependências do efeito de rolagem; imediatamente antes de `<div ref={bottomRef} />`:

```tsx
      {ready && typing && <TypingBubble voice={typing.voice} label={typing.label} />}
```

- [ ] **Step 5: Run gates**

Run: `npm test` ; `.\node_modules\.bin\tsc.cmd --noEmit` ; `npm run build`
Expected: tudo verde.

- [ ] **Step 6: Commit**

```bash
git add lib/patient-portal.ts lib/real/patient-access.ts components/portal/PortalConversation.tsx components/patient/PatientMessageList.tsx lib/__tests__/patient-portal.test.ts
git commit -m "feat(portal): typing indicator and heartbeat in the patient conversation"
```

---

### Task 5: Console da clínica — ler e avisar

**Files:**
- Modify: `lib/console-api.ts` (interface, perto de `listMessages`, linha ~105)
- Modify: `lib/real/console-api.real.ts` (helper exportado + métodos no objeto retornado, perto de `listMessages`)
- Modify: `lib/mock/console-api.mock.ts` (perto de `listMessages`, linha ~249)
- Modify: `components/console/ChatScreen.tsx` (estado ao lado de `messages`, linha 96; reset na linha 162; poll linhas 168-191)
- Modify: `components/console/Thread.tsx` (Props linha 35; render antes de `<div ref={bottomRef} />`; efeito de rolagem linha 77; `Composer`)
- Test: `lib/real/__tests__/console-typing.test.ts`

**Interfaces:**
- Produces: `ConsoleApi.getTyping?(conversationId: string): Promise<TypingBy | null>` e `ConsoleApi.sendTyping?(conversationId: string): Promise<void>` (ambos OPCIONAIS: uma API que não sabe não mostra nada). Real: `GET`/`POST /tenants/me/conversations/{id}/typing` via `hubFetch`; conversa PreCheck (`isPrecheckConversationId`) ⇒ `null`/no-op sem rede; qualquer erro ⇒ `null`/silêncio. Mock: `null`/no-op.

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

`lib/real/console-api.real.ts`:

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

e no objeto retornado, ao lado de `listMessages`:

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

(Ajustar a forma de passar `method: "POST"` ao `hubFetch` copiando uma chamada POST já existente no arquivo, p.ex. a de `sendMessage`/`setConversationMode`.) Importar `TypingBy` de `@/lib/typing`. Em `lib/console-api.ts` acrescentar à interface, com comentário: `getTyping?(conversationId: string): Promise<TypingBy | null>;` e `sendTyping?(conversationId: string): Promise<void>;`. No mock: `getTyping: async () => null, sendTyping: async () => {},`.

- [ ] **Step 4: Wire ChatScreen and Thread**

`ChatScreen.tsx`: `const [typing, setTyping] = useState<TypingBy | null>(null);` ao lado de `messages` (linha 96); `setTyping(null);` no reset do efeito (linha 162); nos dois ramos do `load`, depois de `setMessages(...)`:

```ts
          // Decoration: `getTyping` never rejects (readTypingBy swallows), so it cannot fail the poll.
          const by = api.getTyping ? await api.getTyping(selectedId) : null;
          if (cancelled) return;
          setTyping(by);
```

Passar ao `<Thread … />`: `typing={typing}` e `onTypingBeat={api.sendTyping ? () => void api.sendTyping!(selectedId) : undefined}` (só quando há conversa aberta).

`Thread.tsx`: `typing: TypingBy | null;` e `onTypingBeat?: () => void;` em `Props` e na desestruturação; `[count, conversation.id, Boolean(typing)]` no efeito de rolagem (linha 77); importar `TypingBubble`, `typingView`, `hideStaleTyping`; calcular e renderizar antes de `<div ref={bottomRef} />`:

```tsx
  const lastSender = messages && messages.length ? messages[messages.length - 1].sender : null;
  const view = typingView(hideStaleTyping(typing, lastSender), conversation.product, "clinica");
  // ...
      {view && <TypingBubble voice={view.voice} label={view.label} />}
```

e no `<Composer … />` da thread: `onTyping={onTypingBeat}`. (`conversation.product` é a string do produto da conversa, `"secretaria" | "precheck"` hoje.)

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

- [ ] **Step 1:** `docs/CHECKPOINT_digitando.md` no repo do front (o que entrou, o contrato lido, a regra "um produto novo = uma entrada em `lib/typing.ts`", decisões: sem `role="status"`, `animation: none`, ganchos opcionais) e uma linha no `CLAUDE.md` > "Documentação".
- [ ] **Step 2:** Prova com o agent-browser (duas sessões: paciente no Portal e clínica no console logado): (a) o paciente envia uma pergunta que acione a LLM ⇒ pontinhos da secretarIA aparecem no Portal em ≤ 4 s e somem com a resposta; o console mostra os mesmos; (b) a clínica assume a conversa e digita no console ⇒ o Portal mostra "A clínica está digitando" em ≤ 4 s; (c) com humano conduzindo, o paciente digita no Portal ⇒ o console mostra "O paciente está digitando" e some ≤ 6 s depois de parar; (d) com a automação conduzindo, o Portal NÃO manda batimento (`network requests` sem `/typing`); (e) `prefers-reduced-motion` emulado ⇒ pontos estáticos; tema claro e escuro.
- [ ] **Step 3:** Registrar prints e a lista de requisições no CHECKPOINT.
