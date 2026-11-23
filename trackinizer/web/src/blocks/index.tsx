// The knowledge flow view, `#/blocks`: a prompt's answer rendered in the fold's
// own design — the same DOM, the same stylesheet — with the distiller's blocks
// in the card grid, the wires between them, and the formation modal on click.
//
// The design is fixed and the content is not: the distiller (a model, or the
// CLI's agent) decides the blocks, their claims, provenance and flow for the
// prompt that was asked. The flow itself is fetched, never baked in:
// `blocks-latest.json` is the distiller's latest answer, written beside the app
// the way a build artifact is.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { isSolid } from "./model";
import type { FlowEdge, KnowledgeBlock, KnowledgeFlow } from "./model";
import { Block } from "../ui/block";
import { ComposedView, isComposed, type ComposedFlow } from "../ui/compose";
import { Askable, type Asked } from "../ui/ask";
import { FigureView } from "../ui/figure";
import { Modal } from "../ui/frame";
// The fold's own stylesheet: the blocks view speaks the fold's classes, so the
// design here is the design there, rule for rule.
import "../visuals/Knowledge.css";
import "./fit.css";
import { setChatHidden } from "../visuals/chatColumnState";
import { getGraph } from "../api/graph";
import { listStations } from "../api/stations";
import { getWorkspace } from "../api/workspaces";
import { useCurrentStation } from "../stations/store";
import { askTheChat } from "./askChat";
import { distillFlow } from "./distill";
import { loadSessionFiles, registerSessionFiles, type SessionFile } from "./sessionFiles";
import { CodeView } from "../ui/code";

/** One edge as it is drawn: its path through the stage, and where its label sits. */
type DrawnEdge = { readonly edge: FlowEdge; readonly path: string; readonly x: number; readonly y: number };

/** A card's place in the stage, measured from the card the row already laid out. */
type Rect = { readonly x: number; readonly y: number; readonly w: number; readonly h: number };

/** The session's code files: the strip above the stage, and the whole-program reader. */
function SessionFiles() {
  const [files, setFiles] = useState<SessionFile[]>(() => loadSessionFiles());
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => {
    const read = () => setFiles(loadSessionFiles());
    read();
    window.addEventListener("blocks:files", read);
    return () => window.removeEventListener("blocks:files", read);
  }, []);
  const file = open ? files.find((f) => f.path === open) : null;
  return (
    <>
      {files.length > 0 && (
        <div className="kb-files" role="tablist" aria-label="Session code files">
          <span className="kb-files-label">session files</span>
          {files.map((f) => (
            <button key={f.path} role="tab" aria-selected={open === f.path} className="kb-file-tab"
              title={`open ${f.path}, whole file`} onClick={() => setOpen(f.path)}>
              {f.path}
            </button>
          ))}
        </div>
      )}
      {file && (
        <Modal cls="knowledge-modal" title={file.path}
          note={`${file.text.split("\n").length} lines \u00b7 the whole program, for the session`}
          onClose={() => setOpen(null)}>
          <CodeView path={file.path} text={file.text} language={file.language ?? ""} />
        </Modal>
      )}
    </>
  );
}

/** The glyph each status wears on its slot, as the fold's cards do. */
const GLYPHS: { readonly [status: string]: string } = { settled: "✓", open: "◔", awaited: "▲" };

/** The canvas visual's entry: the renderer registry hands the instance and the
 *  workspace; the flow itself is the fetched artifact, so neither is read. */
export function BlocksVisual() {
  return <BlocksView />;
}

/** A question asked of a record: `#/blocks?focus=<uuid>` distills the fold into a flow. */
function FocusFlow({ focus, hops }: { readonly focus: string; readonly hops: 1 | 2 | 3 }) {
  const [state, setState] = useState<{ readonly flow?: KnowledgeFlow; readonly error?: string }>({});
  useEffect(() => {
    let live = true;
    Promise.all([
      getGraph(4000),
      // the fold's window can outrun its limit and drop the asked record; its own row is the anchor regardless
      fetch(`/api/inquiries/${encodeURIComponent(focus)}`).then((r) => (r.ok ? r.json() : null)).catch(() => null),
    ])
      .then(async ([graph, row]) => {
        if (!live) return;
        const self = row && row.id ? row : null;
        const asked = self ? { id: self.id, kind: self.kind, seq: self.seq, title: self.title, status: self.status, created: self.created, hops: 0 } : undefined;
        const first = distillFlow(graph, focus, hops, asked);
        const rows = await Promise.all(first.ids.map((id) =>
          fetch(`/api/inquiries/${encodeURIComponent(id)}`).then((r) => (r.ok ? r.json() : null)).catch(() => null),
        ));
        const descById = new Map(first.ids.map((id, k) => [id, rows[k] && rows[k].description ? String(rows[k].description) : ""]));
        const out = distillFlow(graph, focus, hops, asked, descById);
        registerSessionFiles(out.flow.files);
        setState({ flow: out.flow });
      })
      .catch((error: Error) => { if (live) setState({ error: error.message }); });
    return () => { live = false; };
  }, [focus, hops]);
  if (state.error) {
    return (
      <div className="kb-with-files">
        <SessionFiles />
        <section className="knowledge" aria-label="What we know">
          <p className="knowledge-none" style={{ padding: 20 }}>The fold did not distill — {state.error}.</p>
        </section>
      </div>
    );
  }
  if (!state.flow) {
    return (
      <div className="kb-with-files">
        <SessionFiles />
        <section className="knowledge" aria-label="What we know">
          <p className="knowledge-none" style={{ padding: 20 }} aria-busy="true">Distilling the fold for {focus.slice(0, 8)}…</p>
        </section>
      </div>
    );
  }
  const composedFlow: ComposedFlow = {
    title: state.flow.title || `Knowledge Graph · ${focus.slice(0, 8)}`,
    prompt: state.flow.prompt || `What the graph claims of this record and its neighborhood`,
    compose: [
      {
        node: "heading",
        level: 1,
        text: state.flow.title || `Distilled Knowledge Graph`,
      },
      {
        node: "graph",
        record: focus,
        hops,
        height: 520,
      },
    ],
  };
  return (
    <div className="kb-with-files">
      <SessionFiles />
      <section className="knowledge" aria-label="What we know">
        <div className="knowledge-body">
          <ComposedView flow={composedFlow} />
        </div>
      </section>
    </div>
  );
}

/**
 * The ask bar's answering side: a question about a node goes to the station's own chat
 * and the reply comes back to the bar, under the question, where it was asked.
 *
 * The station is read when the question is asked rather than subscribed to, because the
 * bar is idle almost all the time and a canvas does not need to re-render when a chat it
 * is not using changes. A station with no conversation yet starts one, which is the same
 * thread the chat column then shows — the canvas never opens a second channel.
 */
/**
 * The station a short question off the canvas is asked on, when one is standing.
 *
 * The station that composed the page is answered by the composer, and a composer is one
 * turn at a time: measured, a two-word follow-up sat on `thinking…` for minutes behind a
 * turn that was reading PNGs and rewriting the surface. A reader asking what a number
 * means is not asking for the page to be rebuilt. So a station named this one answers
 * the canvas instead, if the lab is running an agent on it; nothing here creates it,
 * because a station with no agent on it answers nothing, and falling back to the
 * composer is slow but certain.
 */
const ASKER = "canvas questions";

/**
 * What the pointed-at node sits in: the page's title, and one line per node around it.
 *
 * A question sent with the node alone is a question with no page. "Explain this" on one
 * figure came back as an essay reciting the figure's own numbers, because the agent had
 * the numbers and nothing to say they were an intervention arm in an argument three nodes
 * above. The surrounding items are the argument; their labels are what a reader has in
 * their head when they point at one of them, so they are what rides along.
 */
function pageAround(flow: ComposedFlow, pointedAt: number): string {
  const lines = flow.compose.map((item, index) => {
    const named = item as { node: string; text?: string; label?: string; caption?: string; title?: string; figure?: { label?: string } };
    const says = named.title ?? named.label ?? named.caption ?? named.figure?.label ?? named.text ?? "";
    return `${index === pointedAt ? "→" : " "} ${named.node}: ${says.slice(0, 110)}`;
  });
  return [`The page is titled "${flow.title}". Its nodes, in order, with the one asked about marked:`, ...lines].join("\n");
}

function useCanvasAsk(flow: ComposedFlow | null): (asked: Asked) => void | Promise<string> {
  const station = useCurrentStation();
  return useCallback(async (asked: Asked) => {
    if (!station.id) throw new Error("this tab is not on a station yet");
    const asker = (await listStations()).find((s) => s.name === ASKER && !s.hidden) ?? null;
    const workspace = await getWorkspace(asker?.id ?? station.id);
    const page = typeof location !== "undefined" && location.hash.startsWith("#/") ? location.hash : null;
    // The node's own composed JSON rides along: the agent answers from the item it drew,
    // which is the whole reason this beats sending a screenshot.
    //
    // And the question states its own terms. The agent on the other end is placed by
    // `trax run`, whose system prompt is the canvas composer's — read the brief, read the
    // graph, recompose the page — so a two-word follow-up was answered by a hundred tool
    // calls and a rebuilt surface while the reader watched a spinner. A system prompt
    // appended under that one did not hold; the turn itself does, because it is the last
    // thing said and the most specific. The ceiling is what the reader can see: a box on
    // the page, which fits a paragraph, not a composition.
    const about = JSON.stringify(asked.about.item ?? null);
    const where = flow ? pageAround(flow, flow.compose.findIndex((item) => item === asked.about.item)) : null;
    const reply = await askTheChat(
      { workspaceId: asker?.id ?? station.id, conversationId: workspace.conversation_id ?? null, page },
      [
        asked.question,
        "",
        "Answer from what is below and this question alone. Do not run commands, read files,",
        "query the graph, take screenshots, or recompose the canvas — the answer is going in a",
        "small box on the page the reader is looking at, and they are watching it spin.",
        "At most three sentences. Answer the question that was asked and stop: do not recap the",
        "node, do not list what it does not say, do not explain what you cannot answer unless",
        "that is the whole answer. Lead with the number if there is one. Colours, fonts and",
        "layout are the app's and are in no JSON — if asked about one, say which series or row",
        "is meant and answer about that.",
        ...(where ? ["", where] : []),
        "",
        `[the ${asked.about.node} they pointed at, as it was composed]`,
        about.slice(0, 4000),
      ].join("\n"),
    );
    return reply.text;
  }, [station.id, flow]);
}

export function BlocksView() {
  const focusMatch = typeof location !== "undefined"
    ? location.hash.match(/^#\/blocks\?focus=([^&]+)(?:&hops=([123]))?/)
    : null;
  const focus = focusMatch ? decodeURIComponent(focusMatch[1]) : null;
  (window as unknown as { __blocksDebug?: unknown }).__blocksDebug = { hash: typeof location !== "undefined" ? location.hash : "", focus, matched: !!focusMatch };
  const hops = (focusMatch && focusMatch[2] ? Number(focusMatch[2]) : 2) as 1 | 2 | 3;
  const [state, setState] = useState<{ readonly flow?: KnowledgeFlow; readonly error?: string }>({});
  const askOnCanvas = useCanvasAsk(isComposed(state.flow) ? state.flow : null);
  useEffect(() => {
    if (focus) return;
    let live = true;
    let known = "";
    // The answer file is rewritten by the agent as it works; the tab polls it so a
    // new or updated answer lands without a refresh, and a failed poll never
    // blanks an answer already on screen.
    const read = () =>
      fetch(`blocks-latest.json?v=${Date.now()}`, { cache: "no-store" })
        .then((r) => {
          if (!r.ok) throw new Error(`the distiller answered ${r.status}`);
          return r.json() as Promise<KnowledgeFlow>;
        })
        .then((flow) => {
          if (!live) return;
          const body = JSON.stringify(flow);
          if (body === known) return;
          known = body;
          registerSessionFiles(flow.files);
          setState({ flow });
        })
        .catch((error: Error) => {
          if (live && !known) setState({ error: error.message });
        });
    read();
    const timer = window.setInterval(read, 4000);
    const onFocus = () => read();
    window.addEventListener("focus", onFocus);
    return () => {
      live = false;
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [focus]);
  const strip = <SessionFiles />;
  if (focus) return <FocusFlow focus={focus} hops={hops} />;
  if (state.error) {
    return (
      <div className="kb-with-files">
        {strip}
        <section className="knowledge" aria-label="What we know">
          <p className="knowledge-none" style={{ padding: 20 }}>No distilled answer yet — {state.error}. Ask the agent a question and its answer lands here.</p>
        </section>
      </div>
    );
  }
  if (!state.flow) {
    return (
      <div className="kb-with-files">
        {strip}
        <section className="knowledge" aria-label="What we know">
          <p className="knowledge-none" style={{ padding: 20 }} aria-busy="true">Distilling the latest answer…</p>
        </section>
      </div>
    );
  }
  // Two open forms answer a question here: a composition, which is the catalog's own
  // tree assembled by the answer, and the blocks flow, which is the fold's. The three
  // fixed templates this route used to dispatch — a stage, a review, a paper — are
  // deleted. Each was a question answered in advance, and each was built out of the
  // same catalog nodes a composition reaches for directly.
  // The canvas answers back: point at any item and ask about THAT item. The question
  // leaves with the node's own composed JSON attached, which is the whole reason this
  // beats sending a screenshot. Until a live chat channel exists it is logged, which
  // is enough to prove the handle works end to end.
  if (isComposed(state.flow)) return (
    // The composed answer scrolls in the fold's own body: the tile clips (overflow hidden),
    // so without this wrapper a composition taller than the pane is unreachable.
    <div className="kb-with-files">
      {strip}
      <section className="knowledge" aria-label="What we know">
        <div className="knowledge-body">
          <Askable onAsk={askOnCanvas}>
            <ComposedView flow={state.flow} />
          </Askable>
        </div>
      </section>
    </div>
  );
  const flow = state.flow as Partial<KnowledgeFlow>;
  if (Array.isArray(flow.blocks) && Array.isArray(flow.edges)) {
    return (
      <div className="kb-with-files">
        {strip}
        <KnowledgeFlowView flow={state.flow as KnowledgeFlow} />
      </div>
    );
  }
  // A fetched answer in a shape this app no longer renders is the reader's own
  // mistake to see, not one to guess at: a blank pane reads as a broken fetch.
  return (
    <div className="kb-with-files">
      {strip}
      <section className="knowledge" aria-label="What we know">
        <p className="knowledge-none" style={{ padding: 20 }}>
          {`This answer is not a composition — it declares ${Object.keys(state.flow).length} top-level fields and no "compose" array. `}
          {`The fields it names: ${Object.keys(state.flow).join(", ")}.`}
          {" The catalog is the only design this route renders; the node list and the envelope are in DESIGN.md."}
        </p>
      </section>
    </div>
  );
}

/** The flow itself: the anchor's claim top centre, what it rests on fanned below,
 *  and the wires across the gutters. */
export function KnowledgeFlowView({ flow }: { readonly flow: KnowledgeFlow }) {
  const rows = useRows(flow);
  const [opened, setOpened] = useState<string | null>(null);
  const [openedWire, setOpenedWire] = useState<FlowEdge | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [edges, setEdges] = useState<readonly DrawnEdge[]>([]);
  const [host, setHost] = useState<{ readonly w: number; readonly h: number }>({ w: 0, h: 0 });
  const stage = useRef<HTMLDivElement>(null);
  const cards = useRef(new Map<string, HTMLElement>());

  const measure = useCallback(() => {
    const box = stage.current?.getBoundingClientRect();
    if (!box) return;
    setHost({ w: Math.round(box.width), h: Math.round(box.height) });
    // The distiller's composition decides how many stand in a row, so the card
    // width follows it: the widest row's cards share what the stage gives them,
    // down to the floor where a claim stops reading. The design's 316 stands
    // whenever the row is alone in its width.
    const widest = Math.max(1, ...rows.map((row) => row.length));
    const cardW = Math.max(210, Math.min(316, Math.floor((box.width - 80 - 44 * (widest - 1)) / widest)));
    stage.current?.style.setProperty("--kb-card-w", `${cardW}px`);
    const rectOf = (id: string): Rect | null => {
      const card = cards.current.get(id);
      if (!card) return null;
      const placed = card.getBoundingClientRect();
      return { x: placed.left - box.left, y: placed.top - box.top, w: placed.width, h: placed.height };
    };
    setEdges(drawWires(flow, rows, rectOf));
  }, [flow, rows]);

  useLayoutEffect(() => {
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const watcher = new ResizeObserver(() => measure());
    if (stage.current) watcher.observe(stage.current);
    void document.fonts?.ready.then(() => measure());
    return () => watcher.disconnect();
  }, [measure]);

  /** The blocks the hovered one flows to or from, itself included. */
  const lit = useMemo(() => {
    if (hovered === null) return null;
    const lit = new Set([hovered]);
    for (const { from, to } of flow.edges) {
      if (from === hovered) lit.add(to);
      if (to === hovered) lit.add(from);
    }
    return lit;
  }, [flow, hovered]);

  const anchor = flow.anchor ?? rows[0]?.[0]?.id;
  const sources = new Set((flow.blocks ?? []).flatMap((block) => (block.chain ?? []).map((row) => row.ref))).size;
  return (
    <section className="knowledge" aria-label="What we know">
      <div className="kb-main">
        <header className="knowledge-h">
          <span className="knowledge-lead">
            <span className="knowledge-of mono">{anchor ?? "distilled"}</span>
            <span className="knowledge-title">{flow.title}</span>
          </span>
          <span className="knowledge-count mono">{`${flow.blocks.length} blocks · ${flow.edges.length} wires`}</span>
        </header>
        <div className="knowledge-body" ref={stage}>
          <svg className="knowledge-wires" width={host.w} height={host.h}>
            <defs>
              <marker id="knowledge-head" viewBox="0 0 8 8" refX="8" refY="4" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto">
                <path className="knowledge-head" d="M 0 0 L 8 4 L 0 8 Z" />
              </marker>
            </defs>
            {edges.map(({ edge, path }) => {
              const touching = hovered !== null && (edge.from === hovered || edge.to === hovered);
              return (
                <g key={`${edge.from}-${edge.to}`}>
                  <path
                    d={path}
                    markerEnd="url(#knowledge-head)"
                    className={["knowledge-run", !isSolid(edge.kind) ? "is-open" : "", lit && !touching ? "kb-faint" : ""].filter(Boolean).join(" ")}
                  />
                  {/* The wire answers a click the way a block does: a wide
                      invisible stroke over the run is the target. */}
                  <path d={path} className="kb-wire-hit" onClick={() => setOpenedWire(edge)} />
                </g>
              );
            })}
            {edges.map(({ edge, x, y }) => {
              const touching = hovered !== null && (edge.from === hovered || edge.to === hovered);
              return (
                <text
                  key={`${edge.from}-${edge.to}-word`}
                  x={x}
                  y={y}
                  onClick={() => setOpenedWire(edge)}
                  className={["knowledge-said", lit && !touching ? "kb-faint" : ""].filter(Boolean).join(" ")}
                >
                  {edge.kind}
                </text>
              );
            })}
          </svg>
          <div className="knowledge-tiers">
            {rows.map((row, index) => (
              <div className="knowledge-tier" key={index}>
                {row.map((block) => (
                  <Block
                    key={block.id}
                    className="knowledge-card"
                    status={block.status}
                    dim={lit !== null && !lit.has(block.id)}
                    ref={(node) => {
                      if (node === null) cards.current.delete(block.id);
                      else cards.current.set(block.id, node);
                    }}
                    selected={opened === block.id}
                    onSelect={() => setOpened(block.id)}
                    onMouseEnter={() => setHovered(block.id)}
                    onMouseLeave={() => setHovered(null)}
                    slot={
                      <>
                        {block.id}
                        <i className="kb-glyph" aria-hidden="true">{GLYPHS[block.status] ?? "◔"}</i>
                        <KindDots block={block} />
                      </>
                    }
                    claim={block.title}
                    lines={[block.body]}
                    foot={block.provenance}
                  />
                ))}
              </div>
            ))}
          </div>
        </div>
        <footer className="knowledge-machine mono">
          {`${flow.blocks.length} blocks · ${flow.edges.length} wires · distilled from ${sources} records`}
        </footer>
      </div>
      {opened ? <Formation block={flow.blocks.find((one) => one.id === opened) ?? null} onClose={() => setOpened(null)} /> : null}
      {openedWire ? (
        <WireFormation flow={flow} edge={openedWire} onClose={() => setOpenedWire(null)} />
      ) : null}
    </section>
  );
}

/** Open dependencies run dashed, the fold's own rule for wants. */
/** Send the reader's question to the station's Chat: the column shows, the box
 *  fills and focuses, and the send stays the reader's own keystroke. */
function askChat(question: string): void {
  setChatHidden(false);
  window.dispatchEvent(new CustomEvent("trackinizer:chat-ask", { detail: question }));
}

/** The ask field inside a formation or wire modal: click "ask about this", type right
 *  here, Enter sends the question with the block's or wire's context already attached. */
function ModalAsk({ seed, onDone }: { readonly seed: string; readonly onDone?: () => void }) {
  const field = useRef<HTMLInputElement>(null);
  const [value, setValue] = useState("");
  useEffect(() => {
    const focusNow = () => field.current?.focus();
    requestAnimationFrame(focusNow);
    const retry = setTimeout(focusNow, 60);
    return () => clearTimeout(retry);
  }, []);
  const send = () => {
    const rest = value.trim();
    if (!rest) return;
    askChat(`${seed} \u2014 ${rest}`);
    setValue("");
    if (onDone) onDone();
  };
  return (
    <form className="kb-modal-ask" onSubmit={(e) => { e.preventDefault(); send(); }}>
      <input ref={field} value={value} placeholder="type your question\u2026" aria-label="ask about this"
        onChange={(e) => setValue(e.target.value)} />
      <button type="submit" className="btn ghost">send</button>
    </form>
  );
}
/** The kinds behind a block, as tiny dots under the slot, in the kinds' hues. */
function KindDots({ block }: { readonly block: KnowledgeBlock }) {
  const kinds = [...new Set((block.chain ?? []).map((row) => row.ref.split("#")[0] ?? ""))].filter(Boolean);
  if (kinds.length === 0) return null;
  return (
    <span className="kb-kinds" aria-hidden="true">
      {kinds.map((kind) => (
        <i key={kind} style={{ background: `var(--g-kind-${kind}, var(--g-fallback))` }} />
      ))}
    </span>
  );
}

/** The rows the flow lands in: the anchor top centre, then each row of what the
 * one above rests on — the graph distance from the anchor, not the JSON's order. */
function useRows(flow: KnowledgeFlow): readonly (readonly KnowledgeBlock[])[] {
  return useMemo(() => {
    // The distiller's own composition, when it declared one: its rows, its order,
    // verbatim — this is how a flow says "one at top, three below" and is drawn
    // exactly so. Only named blocks land; the rest fall back to the derivation.
    if (flow.rows && flow.rows.length > 0) {
      const byId = new Map(flow.blocks.map((block) => [block.id, block]));
      const placed = new Set(flow.rows.flat());
      const rows = flow.rows
        .map((row) => row.map((id) => byId.get(id)).filter((block) => block !== undefined))
        .filter((row) => row.length > 0);
      const rest = flow.blocks.filter((block) => !placed.has(block.id));
      return rest.length > 0 ? [...rows, rest] : rows;
    }
    const neighbours = new Map(flow.blocks.map((block) => [block.id, [] as string[]]));
    for (const { from, to } of flow.edges) {
      neighbours.get(from)?.push(to);
      neighbours.get(to)?.push(from);
    }
    const anchor =
      (flow.anchor && flow.blocks.find((block) => block.id === flow.anchor)?.id) ??
      [...flow.blocks].sort((a, b) => (neighbours.get(b.id)?.length ?? 0) - (neighbours.get(a.id)?.length ?? 0) || a.id.localeCompare(b.id))[0]?.id;
    if (!anchor) return [flow.blocks];
    const seen = new Map<string, number>([[anchor, 0]]);
    let frontier = [anchor];
    while (frontier.length > 0) {
      const next: string[] = [];
      for (const id of frontier) {
        for (const other of neighbours.get(id) ?? []) {
          if (seen.has(other)) continue;
          seen.set(other, (seen.get(id) ?? 0) + 1);
          next.push(other);
        }
      }
      frontier = next;
    }
    const rows: KnowledgeBlock[][] = [];
    for (const block of flow.blocks) {
      const at = seen.get(block.id) ?? 0;
      (rows[at] ??= []).push(block);
    }
    return rows.filter((row) => row !== undefined);
  }, [flow]);
}

/**
 * The wires: one right-angle run per edge, the claim above and its support below,
 * the run leaving the claim's bottom edge, turning in the gutter between the rows
 * and entering the support's top — the arrow pointing down, the way the design
 * reads. A claim with several records under it spreads its departures along its
 * bottom edge, and a support held up by several claims spreads its landings along
 * its top, so no two wires share a point on a card.
 */
function drawWires(flow: KnowledgeFlow, rows: readonly (readonly KnowledgeBlock[])[], rectOf: (id: string) => Rect | null): DrawnEdge[] {
  const rowOf = new Map<string, number>();
  rows.forEach((row, index) => {
    for (const block of row) rowOf.set(block.id, index);
  });
  const bottoms = new Map<string, string[]>();
  const tops = new Map<string, string[]>();
  for (const edge of flow.edges) {
    const upper = (rowOf.get(edge.from) ?? 0) <= (rowOf.get(edge.to) ?? 0) ? edge.from : edge.to;
    const lower = upper === edge.from ? edge.to : edge.from;
    bottoms.set(upper, [...(bottoms.get(upper) ?? []), lower]);
    tops.set(lower, [...(tops.get(lower) ?? []), upper]);
  }
  const rowBottom = (index: number, floor: number): number => {
    let bottom = floor;
    for (const block of rows[index] ?? []) {
      const one = rectOf(block.id);
      if (one) bottom = Math.max(bottom, one.y + one.h);
    }
    return bottom;
  };
  const runs: DrawnEdge[] = [];
  for (const edge of flow.edges) {
    const upper = (rowOf.get(edge.from) ?? 0) <= (rowOf.get(edge.to) ?? 0) ? edge.from : edge.to;
    const lower = upper === edge.from ? edge.to : edge.from;
    const claim = rectOf(upper);
    const support = rectOf(lower);
    if (!claim || !support) continue;
    const at = rowOf.get(lower) ?? 0;
    const lo = at < 1 ? claim.y + claim.h : Math.max(claim.y + claim.h, rowBottom(at - 1, claim.y + claim.h));
    if (support.y - lo <= 8) continue;
    const turn = Math.round((lo + support.y) / 2);
    const under = bottoms.get(upper) ?? [];
    const over = tops.get(lower) ?? [];
    const fromX = claim.x + (claim.w * (under.indexOf(lower) + 1)) / (under.length + 1);
    const toX = support.x + (support.w * (over.indexOf(upper) + 1)) / (over.length + 1);
    runs.push({
      edge,
      path: `M ${fromX} ${claim.y + claim.h} V ${turn} H ${toX} V ${support.y}`,
      x: (fromX + toX) / 2,
      y: turn,
    });
  }
  return runs;
}

/** One opened wire: the two blocks it joins, and how the distillation decided
 *  to draw it — the word on the wire, opened like a block is. */
function WireFormation({ flow, edge, onClose }: { readonly flow: KnowledgeFlow; readonly edge: FlowEdge; readonly onClose: () => void }) {
  const [asking, setAsking] = useState(false);
  const from = flow.blocks.find((block) => block.id === edge.from);
  const to = flow.blocks.find((block) => block.id === edge.to);
  if (!from || !to) return null;
  return (
    <Modal
      cls="knowledge-modal"
      kicker={`${edge.from} — ${edge.kind} → ${edge.to}`}
      note="a distilled wire"
      title={`${from.title} — ${edge.kind}: ${to.title}`}
      foot={asking ? (
        <ModalAsk seed={`Why is ${edge.from} ${edge.kind} ${edge.to}? ${edge.why ?? ""}`} />
      ) : (
        <button type="button" className="btn ghost" onClick={() => setAsking(true)}>
          ask about this
        </button>
      )}
      onClose={onClose}>
      <p className="knowledge-sec">What it starts at</p>
      <p className="knowledge-src-line">{from.title}. {from.body}</p>
      <p className="knowledge-sec">What it points at</p>
      <p className="knowledge-src-line">{to.title}. {to.body}</p>
      <p className="knowledge-sec">How this wire was distilled</p>
      <p className="knowledge-src-line">
        {edge.why ?? "The distiller drew this wire from the records behind each block — neither block alone says the connection."}
      </p>
    </Modal>
  );
}

/** One opened block: the fold's formation panel — the claim whole, its own line
 * in the boxed head of the spine, then the chain of records that formed it. */
function Formation({ block, onClose }: { readonly block: KnowledgeBlock | null; readonly onClose: () => void }) {
  const [asking, setAsking] = useState(false);
  if (!block) return null;
  const say = { settled: "settled by records", open: "still open", awaited: "awaiting results" }[block.status] ?? "still open";
  const records = new Set((block.chain ?? []).map((row) => row.ref)).size;
  return (
    <Modal
      cls="knowledge-modal"
      kicker={block.id}
      note={`${say} · ${records} ${records === 1 ? "record" : "records"}`}
      title={block.title}
      foot={asking ? (
        <ModalAsk seed={`${block.id} — ${block.title}: how was this distilled, and which records carry it?`} />
      ) : (
        <button type="button" className="btn ghost" onClick={() => setAsking(true)}>
          ask about this
        </button>
      )}
      onClose={onClose}>
      <p className="knowledge-sec">How it was formed</p>
      <div className="knowledge-spine">
        <div className="knowledge-me">
          <p className="knowledge-src-h"><span className="mono">{block.id}</span></p>
          <p className="knowledge-src-line">{block.body}</p>
        </div>
        {block.chain && block.chain.length > 0 ? (
          <ol className="knowledge-sources">
            {block.chain.map((row, index) => (
              <li key={index}>
                <p className="knowledge-src-h">
                  <span className="mono">{row.ref}</span>
                </p>
                <p className="knowledge-src-line">{row.note}</p>
                <p className="knowledge-src-note">{row.meta}</p>
              </li>
            ))}
          </ol>
        ) : (
          <p className="knowledge-rest">The distiller drew this block from the prompt's records without a chain — its provenance line names them.</p>
        )}
      </div>
      {/* The figure those records are a measurement of. It sits under the chain, not
          over it: the records are what the claim stands on, and the figure is one way
          of reading them — so a reader meets the provenance first. */}
      {block.figure ? <FigureView figure={block.figure} /> : null}
    </Modal>
  );
}
