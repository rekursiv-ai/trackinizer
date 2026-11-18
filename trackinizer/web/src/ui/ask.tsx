// POINT AT IT AND ASK.
//
// The canvas is read-only on purpose: a surface you can edit is a surface that drifts
// from the record it was composed against, and a notebook you can mutate cell-by-cell
// stops being one whole thing. So the reader gets exactly one verb instead — point at
// any part of an answer and ask about THAT part.
//
// This is the other half of the canvas. The canvas let the agent show more than text;
// this lets the reader show more than a screenshot: the question arrives with the
// actual node that was pointed at, its own composed JSON, not a crop of pixels the
// agent has to squint at.
//
// One verb, not a toolbar. No edit, no move, no delete, no comment threads — those are
// four decisions a reader has to make before they can ask a question, and the question
// is the point.
import { useCallback, useContext, useEffect, useRef, useState } from "react";
import { MetaContext } from "../app/boot";
import { Markdown } from "../markdown/Markdown";
import "./ask.css";

/** What the reader pointed at: the node, and the composed item behind it. */
export type Pointed = {
  /** The node's family — `scene`, `ranked`, `figure`. Tells the agent what it is. */
  readonly node: string;
  /** The item exactly as it was composed. The agent gets the data it emitted, not a
   *  description of it, so "why is this arm unpriced" can be answered from the arm. */
  readonly item: unknown;
  /** Where on screen, so the asker's own view can be reconstructed if it matters. */
  readonly at: { readonly x: number; readonly y: number; readonly w: number; readonly h: number };
};

export type Asked = { readonly about: Pointed; readonly question: string };

/**
 * Wrap a composed surface to make every item pointable.
 *
 * Works by delegation off one listener rather than a handler per node: a composition
 * can hold hundreds of items, and a surface that attaches hundreds of listeners to
 * enable one verb has spent more than the verb is worth.
 */
export function Askable({ children, onAsk }: {
  readonly children: React.ReactNode;
  /** Answer in place by returning the reply; return nothing to leave it to the store. */
  readonly onAsk: (asked: Asked) => void | Promise<string>;
}) {
  const host = useRef<HTMLDivElement>(null);
  // Read off the context rather than `useMeta`, which throws without one: the bar is a
  // catalog component and has to render on a surface mounted without the app's boot.
  const kinds = useContext(MetaContext)?.kinds ?? [];
  const [pointed, setPointed] = useState<Pointed | null>(null);
  const [question, setQuestion] = useState("");
  const field = useRef<HTMLInputElement>(null);

  const click = useCallback((event: React.MouseEvent) => {
    // The bar and the answer sit INSIDE the surface they point at, so their own clicks
    // arrive here too — including the one the browser synthesises on the submit button
    // when the reader presses Enter. Reading that as "clicked off the node" put the bar
    // away before the form's own submit event could fire, so Enter threw the question
    // away and left the page as it was. This is the first thing the handler rules out.
    if ((event.target as HTMLElement).closest(".ui-ask, .ui-ask-answer, .ui-ask-waiting")) return;
    const target = (event.target as HTMLElement).closest<HTMLElement>("[data-node]");
    if (!target || !host.current) { setPointed(null); return; }
    // A control inside a node keeps its own click: pointing at a surface must never
    // steal the surface's own affordance, or the one verb breaks the others.
    if ((event.target as HTMLElement).closest("button, a, input")) return;
    const box = target.getBoundingClientRect();
    const frame = host.current.getBoundingClientRect();
    let item: unknown = null;
    try { item = JSON.parse(target.dataset.item ?? "null"); } catch { item = null; }
    setPointed({
      node: target.dataset.node ?? "item",
      item,
      at: { x: box.left - frame.left, y: box.top - frame.top, w: box.width, h: box.height },
    });
    setQuestion("");
  }, []);

  useEffect(() => {
    if (!pointed) return;
    const focusNow = () => field.current?.focus();
    requestAnimationFrame(focusNow);
    const retry = setTimeout(focusNow, 60);
    return () => clearTimeout(retry);
  }, [pointed]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") { setPointed(null); setExchange(null); } };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);

  // The exchange carries its own `at`, and is drawn whether or not the node is still
  // pointed at. An answer tied to the pointer disappears the moment the reader clicks
  // anywhere — including on the canvas they asked about, to look at the thing again —
  // and an agent that takes a minute to answer is then answering into nothing.
  const [exchange, setExchange] = useState<{ q: string; a: string | null; id: number; live: boolean; at: Pointed["at"] } | null>(null);
  // Two ways an answer arrives. `onAsk` may return the reply, which is the live path —
  // the caller owns the channel, so the bar does not need to know there is a chat behind
  // it. Where it returns nothing the question lands in the session store under `id` and
  // the bar polls for a reply written back beside it, which is how a surface with no
  // channel of its own still answers.
  const send = () => {
    if (!pointed || question.trim() === "") return;
    const q = question.trim();
    const id = Date.now();
    const answering = onAsk({ about: pointed, question: q });
    const live = typeof answering?.then === "function";
    if (!live) {
      try {
        localStorage.setItem(`blocks:ask:${id}:q`, JSON.stringify({ q, node: pointed.node, item: pointed.item }));
      } catch { /* a blocked store loses this one exchange, not the bar */ }
    }
    setExchange({ q, a: null, id, live, at: pointed.at });
    setQuestion("");
    if (live) {
      void answering.then(
        // Only the exchange still on screen takes the answer: a reader who asked again
        // while this was in flight is owed the reply to the question they can see.
        (text) => setExchange((now) => (now && now.id === id ? { ...now, a: text } : now)),
        (error: Error) => setExchange((now) => (now && now.id === id ? { ...now, a: `Not answered: ${error.message}.` } : now)),
      );
    }
  };
  useEffect(() => {
    if (!exchange || exchange.a !== null || exchange.live) return;
    const poll = setInterval(() => {
      const a = localStorage.getItem(`blocks:ask:${exchange.id}:a`);
      if (a === null) return;
      let text = a;
      try { const o = JSON.parse(a); if (o && typeof o === "object" && o.answer) text = String(o.answer); } catch { /* plain string */ }
      setExchange({ ...exchange, a: text });
      clearInterval(poll);
    }, 600);
    return () => clearInterval(poll);
  }, [exchange]);

  return (
    <div className="ui-askable" ref={host} onClick={click}>
      {children}
      {pointed ? (
        <>
          {/* The ring is the whole selection UI: it says what was pointed at and
              nothing else. No handles — there is nothing to resize. */}
          <div className="ui-ask-ring" style={{
            left: `${pointed.at.x}px`, top: `${pointed.at.y}px`,
            width: `${pointed.at.w}px`, height: `${pointed.at.h}px`,
          }} />
          <form className="ui-ask" style={{
            left: `${pointed.at.x}px`, top: `${pointed.at.y + pointed.at.h + 8}px`,
          }} onSubmit={(e) => { e.preventDefault(); send(); }}>
            <input ref={field} value={question} placeholder="ask this"
              onChange={(e) => setQuestion(e.target.value)} aria-label={`ask about this ${pointed.node}`} />
            <button type="submit" disabled={question.trim() === ""}>ask</button>
          </form>
        </>
      ) : null}
      {exchange ? (
        <div className={exchange.a === null ? "ui-ask-waiting" : "ui-ask-answer"} style={{
          left: `${exchange.at.x}px`, top: `${exchange.at.y + exchange.at.h + (pointed ? 44 : 8)}px`,
        }}>
          {/* The box covers the page it is about, so it has to be possible to put away
              without asking something else. Escape does it too; this is the one a reader
              finds without being told. */}
          <button type="button" className="ui-ask-close" aria-label="close this answer"
            onClick={() => { setExchange(null); setPointed(null); }}>×</button>
          <div className="ui-ask-q machine">{exchange.q}</div>
          {/* The agent answers in markdown — bold, bullets, a table of the two curves —
              so the box renders markdown. Printing it as plain text put `**` and `|` on
              screen and made a table unreadable. Images off: this is an agent's words,
              and no reply should make the browser fetch a URL. */}
          {exchange.a === null
            ? <div className="ui-ask-thinking machine">thinking…</div>
            : <Markdown source={exchange.a} kinds={kinds} className="md ui-ask-a" images={false} />}
        </div>
      ) : null}
    </div>
  );
}
