// A SURFACE NOBODY PRE-DESIGNED.
//
// The four templates — the blocks flow, the review register, the stage, the opened
// page — each cost a renderer and a stylesheet, which means every new kind of answer
// costs a release. This is the other half: the agent names catalog nodes and the order
// they read in, and the app assembles them. No new component, no new CSS, no template
// per use case.
//
// What that buys is narrow and worth being precise about. The agent gains: which
// nodes, how many, their order, what each holds. The agent still cannot reach: ink,
// face, spacing, glyph, hue, elevation — every one of those belongs to the primitives
// and to the theme. So a composed surface is as on-law as a template is, because it is
// made of the same parts, and `ui/law.test.ts` is already the proof of those parts.
//
// The trade against a template: a template can say things a list of sections cannot —
// the blocks flow routes wires between cards by measuring where they landed, and no
// ordered composition expresses that. Reach for a template when your own reading of
// the relations is the meaning; reach for this when the items are. One node here is the
// exception and is drawn for that case: `graph` puts the store's relations on the page
// inside a composition, which is why it is the node an answer picks when the shape of
// what lies around a record is the finding, not a sentence about it.
import type { CSSProperties, ReactNode } from "react";
import { useContext, useEffect, useMemo, useRef, useState } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { getGraphFocus, type FocusNode } from "../api/graph";
import { MetaContext, useMeta } from "../app/boot";
import { currentTopic, sameTopic, setTopic, topicOf } from "../graph/topic";
import { detailQueries } from "../detail/queries";
import type { Detail } from "../api/detail";
import { Block } from "./block";
import { Footnote, Heading, MachineRow, Marks, Paths, Prose, Section, ticks, type Mark } from "./sections";
import { FigureView, type Figure } from "./figure";
import { RegisterView } from "./register";
import { Flow, type FlowBlock, type FlowRelation } from "./flow";
import { CodeView, type Code } from "./code";
import { ImageView, type ImageItem } from "./image";
import { IllustrationView, type IllustrationItem } from "./illustration";
import { DrawingView, type Drawing } from "./drawing";
import { EmbedView } from "./embed";
import { Modal } from "./frame";
import "./compose.css";
import "../visuals/Knowledge.css";

/** One item of a composition: a catalog node, and what that node holds. */
export type Item =
  /** A document heading at one of three ranks. The node for an answer that reads as
   *  a document: one `1`, a `2` per part, a `3` per point. Pick this over `section`
   *  whenever the surface is prose rather than telemetry — a page of section rules
   *  has no hierarchy a reader can see. */
  | { readonly node: "heading"; readonly level: 1 | 2 | 3; readonly text: string }
  /** A section rule: a label, the hairline carrying the width, and the measure at its
   *  end, in the machine register. The node for a measurement pane — a sampled run,
   *  a register of shapes — where the measure at the end is the point. */
  | { readonly node: "section"; readonly label: string; readonly size?: string }
  /** Prose. Two to four sentences; `backticked` tokens read as code. Measured at ~75
   *  characters by default (the app's figure, not yours). Set `span` for a lead under
   *  full-width chrome — a narrow paragraph under a wide header reads as unfinished. */
  | { readonly node: "prose"; readonly text: string; readonly lead?: boolean; readonly span?: boolean }
  /** A machine line: cells read for their figures, in the machine face, spaced into a
   *  column by the first one. The node for a measurement whose parts are all figures —
   *  never `prose`, which sets a number in the prose face. When the row needs a gutter
   *  at its right, or a label in a different register from its values, compose a `row`
   *  instead: this is the shorthand for the common case. */
  | { readonly node: "cells"; readonly cells: readonly string[] }
  /** Marked lines. The glyph is the verdict and the only coloured thing. */
  | { readonly node: "marks"; readonly items: readonly Mark[]; readonly span?: boolean }
  /** Addresses under a sentence about them, two to a row. */
  | { readonly node: "paths"; readonly items: readonly string[] }
  /** The prior, the one line out of somebody else's ledger. */
  | { readonly node: "footnote"; readonly text: string }
  /** A figure: a chart, a series, a table, an equation. */
  | ({ readonly node: "figure" } & { readonly figure: Figure })
  /** A file on stage, highlighted and numbered, with annotations pinned to their own
   *  line. Never `prose` — no gutter, no line numbers. */
  | ({ readonly node: "code" } & Code)
  /** A real figure, fetched, never drawn or guessed. Not Rich — this is display, not
   *  composing text; Rich costs an editor you do not need to show one picture. */
  | ({ readonly node: "image" } & ImageItem)
  /** A video that dramatizes a mechanism, with the prompt that made it printed on the
   *  surface. Pick this when what a reader must see is what something LOOKS like rather
   *  than what it reaches — a number belongs in `figure`, and this node is labelled
   *  illustrative precisely so a video never reads as a measurement. */
  | ({ readonly node: "illustration" } & IllustrationItem)
  /** Geometry the agent drew itself, when no catalog shape is the right shape, and the
   *  shape for a simple directly labelled value: hand-write the marks at coordinates
   *  taken from the records' own numbers, and name every axis and unit inside the SVG.
   *  Reach for a library node (`figure`, `series`) only when its scales, legends or real
   *  sampling materially improve the result — never to decorate a number that reads in
   *  one line. */
  | ({ readonly node: "drawing" } & Drawing)
  /** A page you wrote and published, hosted live in the answer: the shape of a thing, a
   *  running simulation, anything whose geometry this catalog does not hold — and nothing
   *  else. The host grants no new permission, so the page inherits no ink and can fetch
   *  nothing. `shows` says what it shows — a hosted page nobody can describe is a black box
   *  — and `height` is the lane it draws in. `A page you write yourself`, below, carries the
   *  contract: what the sandbox forbids, the inks to copy, what a first draft must get right. */
  | { readonly node: "embed"; readonly id: string; readonly title: string; readonly shows: string; readonly height?: number }
  /** A quoted page: the address it was opened at, an optional centred title/byline,
   *  then any items — a `prose` quote, a `figure` table, an `image` — all re-themed
   *  to the page's own light colours, because a printed paper does not take the
   *  reader's dark mode. Pick this to hold anything reproduced FROM a source rather
   *  than written about it; nest the source's own table inside it rather than beside
   *  it, so a reader sees the page and not a description of the page. */
  | {
      readonly node: "plate";
      readonly bar: string;
      readonly title?: string;
      readonly byline?: string;
      readonly items: readonly Item[];
    }
  /** A bordered container holding a composition of its own: a knowledge block, a
   *  commit in a replay, a quoted plate. General, not a template — the box is a
   *  hairline and a step of surface, and what goes in it is whatever you compose.
   *  `lane` is its width in `ch` when a tier of them must come out even. */
  | { readonly node: "card"; readonly items: readonly Item[]; readonly lane?: number; readonly slot?: string }
  /** A tier: cards or items laid across the lane, centred, wrapping when they exceed
   *  it. The geometry a flow of blocks hangs in — one up, three down is two tiers. */
  | { readonly node: "tier"; readonly items: readonly Item[] }
  /** A flow: carded blocks in tiers and the relations between them, routed by
   *  React Flow. The statement an ordered list of sections cannot make is "THIS block
   *  belongs to THAT one, in this word", so a relation is named here rather than laid
   *  out. You give each block a tier and each relation its own word; the routing, the
   *  arrowhead and the pan are the library's, and the ink is the theme's. */
  | {
      readonly node: "flow";
      readonly blocks: readonly { readonly slot: string; readonly tier: number; readonly items: readonly Item[] }[];
      readonly relations: readonly { readonly from: string; readonly to: string; readonly label: string; readonly open?: boolean }[];
      readonly height?: number;
    }
  /** A hairline. Opens or closes a set of rows; the only rule the agent may place,
   *  because a rule is structure and not decoration. */
  | { readonly node: "rule" }
  /** A row with up to three slots: `lead` keeps its width and is what the column is
   *  scanned by, `fill` takes the rest, `trail` is a gutter at the far right that
   *  aligns down the surface. This is the geometry behind a label/values/marker row
   *  and behind a head with its actions on the right — one node, not two templates.
   *  It is also the ranked list: a `section` over N of these, ordered by what each would
   *  settle — the lead is what the reader scans to choose, the fill the claim, the trail
   *  what it costs. There is no leaderboard node to ask for because this is it. */
  | {
      readonly node: "row";
      readonly lead?: readonly Item[];
      readonly fill?: readonly Item[];
      readonly trail?: readonly Item[];
      /** The lead's width in `ch`, when a column of rows must align on one. */
      readonly lead_ch?: number;
      /** The trail's width in `ch`, so every gutter mark meets the same edge. */
      readonly trail_ch?: number;
      /** Pitch: `tight` for a scanned column, `open` for a sampled run that is read
       *  down. A run of rows at prose leading becomes a block of digits. */
      readonly pitch?: "tight" | "open";
    }
  /** A line of text at one of the surface's three registers. `chrome` is the 10.5px
   *  uppercase mono that labels and locates; `machine` is a value read off an
   *  instrument, white, in the machine face; `read` is the one paragraph a surface is
   *  read for, at reading size. Pick `chrome` for anything that is not itself an
   *  answer. */
  | {
      readonly node: "text";
      readonly register: "chrome" | "machine" | "read";
      readonly text: string;
      /** Whether the block takes the whole lane instead of the reading measure. A
       *  paragraph is measured by default, because ~75 characters is where a reader
       *  starts losing the line coming back from the right edge — that figure is the
       *  app's and not yours. Whether this block is a column someone reads or a band
       *  across the pane IS yours: set it when the block is a banner, a summary line
       *  under a full-width run, or anything whose job is to span what it describes. */
      readonly span?: boolean;
    }
  /** A chip: a boxed mono control — a lens offering another reading, a filter. `on`
   *  is the one already open. */
  | { readonly node: "chip"; readonly text: string; readonly on?: boolean }
  /** A line to type into, so a surface ends in a question rather than a full stop.
   *  `can` states what it could still be asked. */
  | { readonly node: "field"; readonly placeholder: string; readonly can?: string }
  /** A strip or a list longer than its lane. It scrolls on its own axis and never
   *  drops its tail — which is the whole reason a long run of rows is this and not
   *  a repeated `marks`. Each row is itself a composition, so a sampled row can
   *  carry its own marks. */
  | {
      readonly node: "register";
      readonly axis: "x" | "y";
      readonly extent: number;
      readonly gap?: number;
      readonly label: string;
      readonly rows: readonly (readonly Item[])[];
      /** The strip's own height, required on `axis: "x"` — a horizontal register's
       *  items are positioned absolutely, so nothing else gives the lane a height and
       *  it draws nothing without one. A card of two or three lines needs ~220–280. */
      readonly height?: number;
    }
  /** The knowledge graph, opened at one record and drawn as the fold: every record
   *  within `hops` as a block in a tier, and one labelled, arrowed wire per stored edge
   *  between two of them. The node for an answer whose weight is
   *  in the relations — and the only composition node whose edges are not yours: you name
   *  a record, the store says what bears on it, with the valence a `flow` relation would
   *  have to assert. Pick by whose reading the reader is doing: `flow` states YOURS, tier
   *  by tier and word by word; `graph` hands over the territory to find their own way in. */
  | {
      readonly node: "graph";
      /** The record the window centres on, by id. */
      readonly record: string;
      /** How far the window lights: 1 to 3 edges, default 2. */
      readonly hops?: 1 | 2 | 3;
      /** A block to open the formation panel on, by record id: the claim whose own
       *  sources and wires the reader should see unfolded without clicking for it. */
      readonly open?: string;
      readonly height?: number;
    }
  /** A file in context: upstream supply flow, syntax-highlighted code lines with pinned
   *  outcome run cards, matrix schematic, and next question exposed. Pick this over `code`
   *  when explaining how code paths route different configurations or kernels. */
  | {
      readonly node: "code_context";
      readonly super_header?: string;
      readonly file: string;
      readonly commit?: string;
      readonly feed_flow?: {
        readonly input: { readonly label: string; readonly note: string };
        readonly relation1: string;
        readonly guard: { readonly label: string; readonly note: string };
        readonly relation2: string;
        readonly output: { readonly label: string; readonly note: string };
      };
      readonly code: {
        readonly path?: string;
        readonly start_line: number;
        readonly lit_line?: number;
        readonly source: string;
        readonly run_groups?: readonly {
          readonly label: string;
          readonly target_line: number;
          readonly chips: readonly string[];
          readonly note: string;
          readonly tag?: string;
        }[];
      };
      readonly callout?: string;
      readonly matrix?: {
        readonly label: string;
        readonly grid: readonly (readonly string[])[];
        readonly caption: string;
        readonly finding_title: string;
        readonly finding_body: string;
      };
      readonly next_question?: {
        readonly label: string;
        readonly text: string;
        readonly action: string;
      };
    }
  /** A behavioral review with risk accordions: before/after routing table, pipeline stages,
   *  diff lines, and settlement proposals. Pick this over `prose` when reviewing a PR, commit,
   *  or behavioral change where code routing shifted without proving measured performance. */
  | {
      readonly node: "review_accordion";
      readonly super_header?: string;
      readonly title: string;
      readonly subtitle?: string;
      readonly meta_left?: string;
      readonly meta_right?: string;
      readonly items: readonly {
        readonly num: string;
        readonly title: string;
        readonly badge: string;
        readonly open?: boolean;
        readonly prose?: string;
        readonly pipeline?: readonly { readonly name: string; readonly desc: string }[];
        readonly pipeline_caption?: string;
        readonly routing_table?: readonly {
          readonly tokens: string;
          readonly before: string;
          readonly after: string;
          readonly configs: string;
        }[];
        readonly added_lines?: { readonly count: number; readonly file: string };
        readonly action_label?: string;
      }[];
      readonly settlement?: {
        readonly title: string;
        readonly status?: string;
        readonly branch_a: { readonly label: string; readonly name: string; readonly detail: string };
        readonly branch_b: { readonly label: string; readonly name: string; readonly detail: string };
        readonly note: string;
        readonly caption?: string;
        readonly action?: string;
      };
    }
  /** A horizontal pipeline of transformation stages connected by arrows. Pick this over
   *  `flow` when displaying a simple sequential dataflow or configuration pass. */
  | {
      readonly node: "pipeline";
      readonly steps: readonly { readonly title: string; readonly note: string }[];
      readonly caption?: string;
    }
  /** A 2D matrix or block mask schematic showing pairwise retention vs drop. Pick this
   *  over `figure` when analyzing sparsity grids, block masks, or attention patterns. */
  | {
      readonly node: "matrix_schematic";
      readonly label: string;
      readonly grid: readonly (readonly string[])[];
      readonly caption: string;
      readonly finding_title: string;
      readonly finding_body: string;
    }
  /** An experimental settlement proposal card contrasting two concrete branches. Pick
   *  this over `card` when proposing an A/B control experiment to settle an open hypothesis. */
  | {
      readonly node: "settlement";
      readonly title: string;
      readonly status?: string;
      readonly branch_a: { readonly label: string; readonly name: string; readonly detail: string };
      readonly branch_b: { readonly label: string; readonly name: string; readonly detail: string };
      readonly note: string;
      readonly caption?: string;
      readonly action?: string;
    };

/** A composed answer: the question, and the items that answer it. */
export type ComposedFlow = {
  readonly title: string;
  readonly prompt: string;
  /** The answer's own ceiling, set before it is composed: how many of each node may
   *  draw, keyed by the node names used. Counted off the render and checked, not
   *  trusted — LAW.md's noise budget says what it is for. */
  readonly budget?: Readonly<Record<string, number>>;
  /** The items, in the order they read. This is the composition — there is no
   *  second grammar, because a composed surface reads down the page. */
  readonly compose: readonly Item[];
};

/** A fetched answer is a composition when it says so. Checked before the templates,
 *  since a composition may legitimately also carry a `figure`. */
export function isComposed(flow: unknown): flow is ComposedFlow {
  return typeof flow === "object" && flow !== null && Array.isArray((flow as ComposedFlow).compose);
}

/** One item, as the node it names. */
/** Every item is wrapped so it can be pointed at: the node's family and the item's own
 *  composed JSON ride on the element. That is what lets a reader's question arrive with
 *  the data the agent emitted rather than a screenshot of it. Serialisation is capped —
 *  a cloud of ten thousand points is not a useful thing to hand back, and a surface
 *  that stalls while stringifying it has spent the whole budget on one verb. */
/** How much composed JSON one node may carry, which is a question's whole budget. */
const ITEM_BUDGET = 4000;

/**
 * The item with its long arrays sampled: every label, axis and series name kept, each
 * long run of values cut to a head and a count of the rest.
 *
 * A figure's data is the part that does not fit, and it is the part a question least
 * often needs — "what is this showing", "which arm is the control" and "why is this one
 * flat" are all answered from the labels and a few points. Keeping the shape and losing
 * the bulk answers them; losing the whole item answers none.
 */
function sampled(value: unknown, keep: number): unknown {
  if (Array.isArray(value)) {
    const head = value.slice(0, keep).map((each) => sampled(each, keep));
    return value.length > keep ? [...head, `…${value.length - keep} more`] : head;
  }
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(Object.entries(value).map(([field, v]) => [field, sampled(v, keep)]));
  }
  if (typeof value === "string" && value.length > 300) return `${value.slice(0, 300)}…`;
  return value;
}

function pointable(item: Item): Record<string, string> {
  let payload: string;
  try {
    payload = JSON.stringify(item);
    // Capped, because a cloud of ten thousand points is not a useful thing to hand back
    // and a surface that stalls stringifying it has spent the whole budget on one verb.
    // It used to be capped by dropping the item, which told a reader who pointed at a
    // figure and asked what it showed that "the JSON was not sent — point at a smaller
    // node": the surface blaming them for its own limit. It is sampled instead, coarser
    // each pass, and only a value that will not fit even at two points per series falls
    // back to naming its fields, which is still something to answer from.
    for (const keep of [12, 5, 2]) {
      if (payload.length <= ITEM_BUDGET) break;
      payload = JSON.stringify(sampled(item, keep));
    }
    if (payload.length > ITEM_BUDGET) payload = JSON.stringify({ node: item.node, fields: Object.keys(item) });
  } catch { payload = JSON.stringify({ node: item.node }); }
  return { "data-node": item.node, "data-item": payload };
}

function ItemView({ item }: { readonly item: Item }): ReactNode {
  return <div {...pointable(item)}>{ItemBody({ item })}</div>;
}

function ItemBody({ item }: { readonly item: Item }): ReactNode {
  switch (item.node) {
    case "heading":
      return <Heading level={item.level}>{item.text}</Heading>;
    case "section":
      return <Section label={item.label} size={item.size} />;
    case "prose":
      return <Prose lead={item.lead} span={item.span}>{item.text}</Prose>;
    case "code":
      return <CodeView {...item} />;
    case "image":
      return <ImageView {...item} />;
    case "illustration":
      return <IllustrationView {...item} />;
    case "drawing":
      return <DrawingView {...item} />;
    case "embed":
      return <EmbedView {...item} />;
    case "plate":
      return (
        <div className="ui-plate">
          <p className="ui-plate-bar"><span>{item.bar}</span></p>
          <div className="ui-plate-body">
            {item.title ? <p className="ui-plate-title">{item.title}</p> : null}
            {item.byline ? <p className="ui-plate-byline">{item.byline}</p> : null}
            {item.items.map((one, at) => <ItemView item={one} key={at} />)}
          </div>
        </div>
      );
    case "cells":
      return <MachineRow cells={item.cells} />;
    case "marks":
      return <Marks items={item.items} span={item.span} />;
    case "paths":
      return <Paths items={item.items} />;
    case "footnote":
      return <Footnote>{item.text}</Footnote>;
    case "figure":
      return <FigureView figure={item.figure} />;
    case "flow":
      return (
        <Flow
          height={item.height}
          relations={item.relations}
          blocks={item.blocks.map((block) => ({
            slot: block.slot,
            tier: block.tier,
            body: (
              <div className="ui-card" data-slot={block.slot}>
                <p className="ui-card-slot">{block.slot}</p>
                {block.items.map((one, at) => <ItemView item={one} key={at} />)}
              </div>
            ),
          }))}
        />
      );
    case "card":
      return (
        <div className="ui-card" data-slot={item.slot} style={item.lane === undefined ? undefined : { maxWidth: `${item.lane}ch` } as CSSProperties}>
          {item.slot ? <p className="ui-card-slot">{item.slot}</p> : null}
          {item.items.map((one, at) => <ItemView item={one} key={at} />)}
        </div>
      );
    case "tier":
      return <div className="ui-tier">{item.items.map((one, at) => <ItemView item={one} key={at} />)}</div>;
    case "rule":
      return <hr className="ui-rule" />;
    case "text":
      return <p className={`ui-t is-${item.register}${item.span ? " is-span" : ""}`}>{ticks(item.text)}</p>;
    case "chip":
      return <span className={`ui-chip${item.on ? " is-on" : ""}`}>{item.text}</span>;
    case "field":
      return (
        <div className="ui-field">
          <div className="ui-field-box">
            <span aria-hidden="true">&gt;</span>
            <input type="text" placeholder={item.placeholder} aria-label={item.placeholder} autoComplete="off" />
          </div>
          {item.can ? <p className="ui-t is-chrome">{`this pane can answer: ${item.can}`}</p> : null}
        </div>
      );
    case "row":
      return (
        <div className={`ui-arow is-${item.pitch ?? "tight"}`}
          style={{
            ...(item.lead_ch === undefined ? {} : { "--lead": `${item.lead_ch}ch` }),
            ...(item.trail_ch === undefined ? {} : { "--trail": `${item.trail_ch}ch` }),
          } as CSSProperties}>
          {item.lead ? <div className="ui-arow-lead">{item.lead.map((one, at) => <ItemView item={one} key={at} />)}</div> : null}
          {item.fill ? <div className="ui-arow-fill">{item.fill.map((one, at) => <ItemView item={one} key={at} />)}</div> : null}
          {item.trail ? <div className="ui-arow-trail">{item.trail.map((one, at) => <ItemView item={one} key={at} />)}</div> : null}
        </div>
      );
    case "register":
      return (
        <RegisterView
          count={item.rows.length}
          axis={item.axis}
          extent={item.extent}
          gap={item.gap}
          label={item.label}
          height={item.height}
          item={(at) => <Row items={item.rows[at] ?? []} />}
        />
      );
    case "graph":
      return <GraphPane record={item.record} hops={item.hops} open={item.open} height={item.height} />;
    case "code_context":
      return <CodeContextView item={item} />;
    case "review_accordion":
      return <ReviewAccordionView item={item} />;
    case "pipeline":
      return <PipelineView item={item} />;
    case "matrix_schematic":
      return <MatrixSchematicView item={item} />;
    case "settlement":
      return <SettlementView item={item} />;
    default:
      // A node the agent named and this app does not render is said out loud. An
      // answer that silently loses an item is an answer whose author believes it
      // shipped — the same rule FigureView keeps.
      return <Footnote>{`This item was not drawn: ${(item as { readonly node: string }).node} is not a node in the catalog.`}</Footnote>;
  }
}

/** A register's row: its own little composition, laid along the row. */
function Row({ items }: { readonly items: readonly Item[] }) {
  return (
    <div className="ui-row">
      {items.map((item, at) => <ItemView item={item} key={at} />)}
    </div>
  );
}

function renderSyntax(line: string): ReactNode {
  const parts = line.split(/(\b(?:def|if|not|return|import|from)\b|#.*$|\b[a-zA-Z_]\w*(?=\()|\b\d+\b)/g);
  return parts.map((part, idx) => {
    if (!part) return null;
    if (/^(?:def|if|not|return|import|from)$/.test(part)) return <span key={idx} className="tok-kw">{part}</span>;
    if (part.startsWith("#")) return <span key={idx} className="tok-comment">{part}</span>;
    if (/^\d+$/.test(part)) return <span key={idx} className="tok-num">{part}</span>;
    return part;
  });
}

function CodeContextView({ item }: { readonly item: Extract<Item, { readonly node: "code_context" }> }) {
  const [activeLine, setActiveLine] = useState<number>(item.code.lit_line ?? 111);
  const [viewMode, setViewMode] = useState<"context" | "source">("context");

  const lines = useMemo(() => {
    return item.code.source.split("\n").map((text, idx) => ({
      num: item.code.start_line + idx,
      text,
    }));
  }, [item.code.source, item.code.start_line]);

  const handleAsk = (question: string) => {
    window.dispatchEvent(new CustomEvent("trackinizer:chat-ask", { detail: question }));
  };

  return (
    <div className="cc-root">
      <div className="cc-subnav">
        <div className="cc-subnav-path">
          <button type="button" className="cc-nav-btn" aria-label="Back">←</button>
          <button type="button" className="cc-nav-btn" aria-label="Forward">→</button>
          <span className="cc-path-crumb">
            <span className="cc-path-dir">{item.file.split("/")[0]}</span>
            <span className="cc-path-slash">/</span>
            <span className="cc-path-file">{item.file.split("/")[1] || item.file}</span>
          </span>
        </div>
        <div className="cc-subnav-action">
          <button type="button" className="cc-btn ghost">All files ▾</button>
        </div>
      </div>

      <div className="cc-head-row">
        <div>
          {item.super_header && <p className="cc-super">{item.super_header}</p>}
          <h1 className="cc-title">{item.file.split("/").pop()}</h1>
          {item.commit && <p className="cc-commit">{item.commit}</p>}
        </div>
        <div className="cc-pill-toggle">
          <button
            type="button"
            className={viewMode === "context" ? "is-active" : ""}
            onClick={() => setViewMode("context")}
          >
            Research context
          </button>
          <button
            type="button"
            className={viewMode === "source" ? "is-active" : ""}
            onClick={() => setViewMode("source")}
          >
            Source
          </button>
        </div>
      </div>

      {item.feed_flow && (
        <section className="cc-section">
          <h2 className="cc-sec-label">READ THE FILE WITH WHAT FEEDS IT-AND WHAT IS MISSING</h2>
          <div className="cc-feed-flow">
            <div className="cc-feed-card">
              <span className="cc-feed-title">{item.feed_flow.input.label}</span>
              <span className="cc-feed-note">{item.feed_flow.input.note}</span>
            </div>
            <div className="cc-feed-wire">
              <span className="cc-feed-wire-label">{item.feed_flow.relation1}</span>
              <span className="cc-feed-wire-arrow">→</span>
            </div>
            <div className="cc-feed-card is-guard">
              <span className="cc-feed-title">{item.feed_flow.guard.label}</span>
              <span className="cc-feed-note">{item.feed_flow.guard.note}</span>
            </div>
            <div className="cc-feed-wire is-dashed">
              <span className="cc-feed-wire-label">{item.feed_flow.relation2}</span>
              <span className="cc-feed-wire-arrow">→</span>
            </div>
            <div className="cc-feed-card is-missing">
              <span className="cc-feed-title">{item.feed_flow.output.label}</span>
              <span className="cc-feed-note">{item.feed_flow.output.note}</span>
            </div>
          </div>
        </section>
      )}

      <section className="cc-section">
        <h2 className="cc-sec-label">WHERE THE SIX CONFIGURATIONS GO</h2>
        <div className="cc-code-split">
          <div className="cc-code-pane">
            <div className="cc-code-head">
              <span>{item.file}</span>
              <span>{item.code.start_line}-{item.code.start_line + lines.length - 1} · computed reach</span>
            </div>
            <div className="cc-code-body">
              {lines.map((line) => {
                const isLit = line.num === activeLine;
                return (
                  <div
                    key={line.num}
                    className={`cc-code-line ${isLit ? "is-lit" : ""}`}
                    onClick={() => setActiveLine(line.num)}
                  >
                    <span className="cc-line-num">{line.num}</span>
                    <span className="cc-line-code">{renderSyntax(line.text)}</span>
                  </div>
                );
              })}
            </div>
          </div>

          {item.code.run_groups && (
            <div className="cc-pinned-cards">
              {item.code.run_groups.map((group, idx) => {
                const isMatch = activeLine === group.target_line;
                return (
                  <div
                    key={idx}
                    className={`cc-run-card ${isMatch ? "is-active" : ""}`}
                    onClick={() => setActiveLine(group.target_line)}
                  >
                    <div className="cc-run-head">
                      <span className="cc-run-label">{group.label}</span>
                      <span className="cc-run-target">→ line {group.target_line}</span>
                    </div>
                    <div className="cc-run-chips">
                      {group.chips.map((chip, cIdx) => (
                        <span key={cIdx} className="cc-run-chip">{chip}</span>
                      ))}
                    </div>
                    <div className="cc-run-foot">
                      <span>{group.note}</span>
                      {group.tag && <span className="cc-run-tag">{group.tag}</span>}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {item.callout && (
          <div className="cc-callout">
            <span className="cc-callout-icon">ⓘ</span>
            <span>{item.callout}</span>
          </div>
        )}
      </section>

      {item.matrix && (
        <section className="cc-section cc-matrix-section">
          <div className="cc-matrix-box">
            <span className="cc-matrix-label">{item.matrix.label}</span>
            <div className="cc-matrix-grid">
              {item.matrix.grid.map((row, rIdx) => (
                <div key={rIdx} className="cc-matrix-row">
                  {row.map((cell, cIdx) => (
                    <div key={cIdx} className="cc-matrix-cell">{cell}</div>
                  ))}
                </div>
              ))}
            </div>
            <span className="cc-matrix-caption">{item.matrix.caption}</span>
          </div>
          <div className="cc-matrix-finding">
            <h3 className="cc-finding-title">{item.matrix.finding_title}</h3>
            {item.matrix.finding_body.split("\n\n").map((para, pIdx) => (
              <p key={pIdx} className="cc-finding-p">{para}</p>
            ))}
          </div>
        </section>
      )}

      {item.next_question && (
        <div className="cc-next-question">
          <div>
            <p className="cc-nq-label">{item.next_question.label}</p>
            <p className="cc-nq-text">{item.next_question.text}</p>
          </div>
          <button
            type="button"
            className="cc-nq-btn"
            onClick={() => handleAsk(item.next_question!.text)}
          >
            {item.next_question.action}
          </button>
        </div>
      )}
    </div>
  );
}

function ReviewAccordionView({ item }: { readonly item: Extract<Item, { readonly node: "review_accordion" }> }) {
  const [openItems, setOpenItems] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(item.items.map((it, idx) => [it.num, it.open ?? idx === 0]))
  );
  const [diffOpen, setDiffOpen] = useState(false);

  const toggleItem = (num: string) => {
    setOpenItems((prev) => ({ ...prev, [num]: !prev[num] }));
  };

  const handleAsk = (q: string) => {
    window.dispatchEvent(new CustomEvent("trackinizer:chat-ask", { detail: q }));
  };

  return (
    <div className="ra-root">
      <div className="ra-subnav">
        <div className="ra-subnav-path">
          <button type="button" className="cc-nav-btn" aria-label="Back">←</button>
          <button type="button" className="cc-nav-btn" aria-label="Forward">→</button>
          <span className="ra-path-crumb">350M sweep / 4f2a9c1</span>
        </div>
        <button type="button" className="ra-btn-search">🔍 Find file</button>
      </div>

      <div className="ra-head">
        {item.super_header && <p className="ra-super">{item.super_header}</p>}
        <h1 className="ra-title">{item.title}</h1>
        {item.subtitle && <p className="ra-subtitle">{item.subtitle}</p>}
        <div className="ra-meta">
          <span>{item.meta_left ?? "Source excerpts + 6 configurations"}</span>
          <span className="ra-meta-right">ⓘ {item.meta_right ?? "Execution not recorded"}</span>
        </div>
      </div>

      <div className="ra-accordions">
        {item.items.map((entry) => {
          const isOpen = !!openItems[entry.num];
          return (
            <div key={entry.num} className={`ra-item ${isOpen ? "is-open" : ""}`}>
              <div className="ra-item-head" onClick={() => toggleItem(entry.num)}>
                <span className="ra-item-num">{entry.num}</span>
                <span className="ra-item-title">{entry.title}</span>
                <span className="ra-item-badge">
                  {entry.badge} {isOpen ? "▾" : ">"}
                </span>
              </div>
              {isOpen && (
                <div className="ra-item-body">
                  {entry.prose && <p className="ra-item-prose">{entry.prose}</p>}

                  {entry.pipeline && (
                    <div className="ra-pipeline-wrap">
                      <div className="ra-pipeline">
                        {entry.pipeline.map((step, sIdx) => (
                          <div key={sIdx} className="ra-pipe-stage">
                            {sIdx > 0 && <span className="ra-pipe-arrow">→</span>}
                            <div className="ra-pipe-card">
                              <span className="ra-pipe-name">{step.name}</span>
                              <span className="ra-pipe-desc">{step.desc}</span>
                            </div>
                          </div>
                        ))}
                      </div>
                      {entry.pipeline_caption && <p className="ra-pipe-caption">{entry.pipeline_caption}</p>}
                    </div>
                  )}

                  {entry.routing_table && (
                    <div className="ra-table-wrap">
                      <table className="ra-table">
                        <tbody>
                          {entry.routing_table.map((row, rIdx) => (
                            <tr key={rIdx}>
                              <td className="ra-td-tokens">{row.tokens}</td>
                              <td className="ra-td-before">{row.before}</td>
                              <td className="ra-td-arrow">before → <span className="ra-after-val">{row.after}</span></td>
                              <td className="ra-td-configs">{row.configs}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}

                  {entry.added_lines && (
                    <div className="ra-diff-wrap">
                      <button
                        type="button"
                        className="ra-diff-btn"
                        onClick={() => setDiffOpen(!diffOpen)}
                      >
                        {diffOpen ? "▼" : "▶"} {entry.added_lines.count} added lines · {entry.added_lines.file}
                      </button>
                      {diffOpen && (
                        <div className="ra-diff-content">
                          <pre>
                            <code>
{`+    if T < cfg.block_len:
+        return dense(q, k, v)
+    blk = triton.cdiv(T, cfg.block_len)
+    return block_sparse(q, k, v, blk)`}
                            </code>
                          </pre>
                        </div>
                      )}
                      {entry.action_label && (
                        <p className="ra-action-link">{entry.action_label}</p>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {item.settlement && (
        <section className="ra-settlement-sec">
          <div className="ra-settlement-head">
            <h2 className="ra-settlement-title">{item.settlement.title}</h2>
            {item.settlement.status && <span className="ra-settlement-status">{item.settlement.status}</span>}
          </div>
          <div className="ra-settlement-box">
            <div className="ra-settlement-split">
              <div className="ra-branch">
                <span className="ra-branch-badge">{item.settlement.branch_a.label}</span>
                <div className="ra-branch-content">
                  <span className="ra-branch-name">{item.settlement.branch_a.name}</span>
                  <span className="ra-branch-detail">{item.settlement.branch_a.detail}</span>
                </div>
              </div>
              <div className="ra-branch">
                <span className="ra-branch-badge">{item.settlement.branch_b.label}</span>
                <div className="ra-branch-content">
                  <span className="ra-branch-name">{item.settlement.branch_b.name}</span>
                  <span className="ra-branch-detail">{item.settlement.branch_b.detail}</span>
                </div>
              </div>
            </div>
            <p className="ra-settlement-note">{item.settlement.note}</p>
          </div>
          <div className="ra-settlement-foot">
            {item.settlement.caption && <p className="ra-settlement-caption">{item.settlement.caption}</p>}
            <button
              type="button"
              className="ra-settlement-btn"
              onClick={() => handleAsk("Draft the experiment")}
            >
              {item.settlement.action ?? "Draft the experiment →"}
            </button>
          </div>
        </section>
      )}
    </div>
  );
}

function PipelineView({ item }: { readonly item: Extract<Item, { readonly node: "pipeline" }> }) {
  return (
    <div className="ra-pipeline-wrap">
      <div className="ra-pipeline">
        {item.steps.map((step, idx) => (
          <div key={idx} className="ra-pipe-stage">
            {idx > 0 && <span className="ra-pipe-arrow">→</span>}
            <div className="ra-pipe-card">
              <span className="ra-pipe-name">{step.title}</span>
              <span className="ra-pipe-desc">{step.note}</span>
            </div>
          </div>
        ))}
      </div>
      {item.caption && <p className="ra-pipe-caption">{item.caption}</p>}
    </div>
  );
}

function MatrixSchematicView({ item }: { readonly item: Extract<Item, { readonly node: "matrix_schematic" }> }) {
  return (
    <div className="cc-matrix-section">
      <div className="cc-matrix-box">
        <span className="cc-matrix-label">{item.label}</span>
        <div className="cc-matrix-grid">
          {item.grid.map((row, rIdx) => (
            <div key={rIdx} className="cc-matrix-row">
              {row.map((cell, cIdx) => (
                <div key={cIdx} className="cc-matrix-cell">{cell}</div>
              ))}
            </div>
          ))}
        </div>
        <span className="cc-matrix-caption">{item.caption}</span>
      </div>
      <div className="cc-matrix-finding">
        <h3 className="cc-finding-title">{item.finding_title}</h3>
        {item.finding_body.split("\n\n").map((para, pIdx) => (
          <p key={pIdx} className="cc-finding-p">{para}</p>
        ))}
      </div>
    </div>
  );
}

function SettlementView({ item }: { readonly item: Extract<Item, { readonly node: "settlement" }> }) {
  return (
    <div className="ra-settlement-sec">
      <div className="ra-settlement-head">
        <h2 className="ra-settlement-title">{item.title}</h2>
        {item.status && <span className="ra-settlement-status">{item.status}</span>}
      </div>
      <div className="ra-settlement-box">
        <div className="ra-settlement-split">
          <div className="ra-branch">
            <span className="ra-branch-badge">{item.branch_a.label}</span>
            <div className="ra-branch-content">
              <span className="ra-branch-name">{item.branch_a.name}</span>
              <span className="ra-branch-detail">{item.branch_a.detail}</span>
            </div>
          </div>
          <div className="ra-branch">
            <span className="ra-branch-badge">{item.branch_b.label}</span>
            <div className="ra-branch-content">
              <span className="ra-branch-name">{item.branch_b.name}</span>
              <span className="ra-branch-detail">{item.branch_b.detail}</span>
            </div>
          </div>
        </div>
        <p className="ra-settlement-note">{item.note}</p>
      </div>
      <div className="ra-settlement-foot">
        {item.caption && <p className="ra-settlement-caption">{item.caption}</p>}
        <button
          type="button"
          className="ra-settlement-btn"
          onClick={() => window.dispatchEvent(new CustomEvent("trackinizer:chat-ask", { detail: "Draft the experiment" }))}
        >
          {item.action ?? "Draft the experiment →"}
        </button>
      </div>
    </div>
  );
}

/** The canvas's own context window, loaded as the chunk the canvas already uses. */

/**
 * The knowledge graph, opened at one record inside an answer.
 *
 * This is the one composition item whose content the agent does not author: the record
 * is named here and every node, wire and valence comes from the store on the read the
 * window makes.
 *
 * It draws through React Flow, and it draws EVERY stored edge. Both of those are
 * corrections of what stood here before, and the reasons are worth keeping:
 *
 *  - The fold this replaced lays blocks in tiers and routes its own right-angled wires,
 *    but it only draws a wire between blocks on ADJACENT rows or on the SAME row, and
 *    keeps one wire per pair. On a real neighbourhood that is most of the graph gone:
 *    measured on a twelve-record answer, the store held 19 edges and the fold drew 7 —
 *    including dropping one of two edges between the same pair where a run both
 *    `proves` at +0.7 and `favors` at -0.5, which is exactly the disagreement a reader
 *    needs to see. A relation the store holds and the picture omits is worse than no
 *    picture, because the reader believes they have seen the whole of it.
 *  - Positions here are a starting layout, not the truth. React Flow lets the reader
 *    drag a block and pan the field, which is what a territory is for; the fold's
 *    geometry was fixed, so a reader who could not read an overlap had no recourse.
 */
function plainText(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s{0,3}>\s?/gm, "")
    .replace(/^\s{0,3}[-*+]\s+/gm, "")
    .replace(/^\s{0,3}\d+\.\s+/gm, "")
    .replace(/\*\*([^*]+)\*\*|__([^_]+)__/g, "$1$2")
    .replace(/(?<![\w*])\*([^*\n]+)\*(?![\w*])/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * Generate a node's description from its formation relationships — the same data
 * the "How it was formed" modal shows, condensed into a card-visible summary.
 *
 * Priority:
 * 1. If the DB record has a stored description/body, use it as the lead sentence.
 * 2. ALWAYS append the formation lineage from the edges: which records bear on this
 *    claim, what kind of edge connects them, and with what valence.
 *
 * This way every node is self-describing: a reader sees what formed the knowledge
 * without having to click through to the modal.
 */
function ownWords(detail: Detail | undefined): string | null {
  if (!detail?.self) return null;
  for (const field of ["body", "content", "summary", "description", "statement", "hypothesis", "method", "question", "answer"]) {
    const value = (detail.self as Record<string, unknown>)[field];
    if (typeof value === "string" && value.trim()) {
      const cleaned = plainText(value);
      if (cleaned.length > 160) {
        const cut = cleaned.slice(0, 160);
        const lastSpace = cut.lastIndexOf(" ");
        return (lastSpace > 0 ? cut.slice(0, lastSpace) : cut) + "…";
      }
      return cleaned;
    }
  }
  return null;
}

function extractClaimDescription(
  detail: Detail | undefined,
  claim: FocusNode,
  held: readonly { readonly from: string; readonly kind: string; readonly valence: number | null }[],
): string {
  const parts: string[] = [];

  // 1. Lead sentence from the DB record's own words, if any.
  const lead = ownWords(detail);
  if (lead) parts.push(lead);

  // 2. Formation lineage from the edges bearing on this claim — the same data
  //    the onclick modal renders, but condensed: name each source, its edge kind,
  //    and its valence sign so the reader sees what formed this knowledge at a glance.
  if (held.length > 0) {
    const forIt = held.filter((one) => (one.valence ?? 0) > 0);
    const against = held.filter((one) => (one.valence ?? 0) < 0);

    // Name up to 3 source records, each once, with the relations it files. Listing
    // raw edges repeated the same record for every relation it holds — "Issue#639
    // narrows · Issue#639 produced_by" spent two of three slots on one record.
    const grouped = new Map<string, string[]>();
    for (const ev of held) {
      const sign = ev.valence === null ? "" : ev.valence > 0 ? ` +${ev.valence}` : ` ${ev.valence}`;
      grouped.set(ev.from, [...(grouped.get(ev.from) ?? []), `${ev.kind}${sign}`]);
    }
    const named = [...grouped.entries()].slice(0, 3).map(([from, kinds]) => `${from} ${kinds.join("/")}`);
    const rest = grouped.size > 3 ? `, and ${grouped.size - 3} more` : "";
    const formation = named.join(" · ") + rest;

    // Append a formation summary line
    if (parts.length > 0) {
      // Already have a DB description, add formation as a second line
      parts.push(`Formed by: ${formation}`);
    } else {
      // No DB description — the formation IS the description.
      //
      // Count by what the edges actually say. Most relations (narrows, requires,
      // produced_by) carry no valence at all, and reporting those as
      // "0 supporting, 0 refuting" told the reader nothing true about a claim that
      // several records plainly point into.
      const plain = held.length - forIt.length - against.length;
      const tallied = [
        forIt.length > 0 ? `${forIt.length} for` : "",
        against.length > 0 ? `${against.length} against` : "",
        plain > 0 ? `${plain} related` : "",
      ].filter(Boolean).join(" · ");
      // Count the records, not the edges. Two records can file four relations between
      // them, and reporting that as "136 records point in" on a block whose whole
      // neighbourhood holds 120 records states a number that cannot be true.
      const sources = new Set(held.map((one) => one.from)).size;
      parts.push(`${claim.kind} · ${sources} record${sources === 1 ? "" : "s"} point${sources === 1 ? "s" : ""} in over ${held.length} relation${held.length === 1 ? "" : "s"} — ${tallied}. ${formation}.`);
    }
  } else if (parts.length === 0) {
    parts.push("No stored edges reach this record yet — the title is the whole claim.");
  }

  return parts.join(" — ");
}

/**
 * A graph node in the composed canvas: an inquiry's neighbourhood rendered via
 * React Flow, with interactive claim blocks, evidence counts, and formation lines.
 */
function GraphPane({ record, hops, open, height }: {
  readonly record: string;
  readonly hops?: 1 | 2 | 3;
  readonly open?: string;
  readonly height?: number;
}) {
  const meta = useMeta();
  const read = useQuery({
    queryKey: ["graph", "compose", record, hops ?? 2],
    queryFn: ({ signal }) => getGraphFocus({ focus: record, hops: hops ?? 2, limit: 120 }, { signal }),
  });
  const graph = read.data;
  const [pickedBlock, setPickedBlock] = useState<string | null>(null);
  const [pickedWire, setPickedWire] = useState<number | null>(null);
  /** `open` names a record whose formation panel should already be unfolded. The type
   *  and the brief both promised the agent this; nothing read it, so an answer that
   *  asked for a claim to arrive open silently arrived closed. Honoured once per
   *  record so the reader can still close it. */
  const unfolded = useRef<string | null>(null);
  useEffect(() => {
    if (!open || !graph || unfolded.current === open) return;
    const want = graph.nodes.find((node) => node.id === open || `${node.kind}#${node.seq}` === open);
    if (!want) return;
    unfolded.current = open;
    setPickedBlock(want.id);
  }, [open, graph]);

  const CLAIM_KINDS = useMemo(() => new Set(["Belief", "Issue"]), []);

  /** How many knowledge blocks the fold will draw. */
  const CLAIM_LIMIT = 8;
  /** A record pointed into by more than this is a knowledge hub in its own right and
   *  keeps its own block; at or under it, it is formation detail and gets absorbed. */
  const ABSORB_MAX_INDEGREE = 1;
  /** How many records one block may fold in. Without a cap the focus — which by
   *  construction has the highest in-degree in the window — absorbs nearly everything
   *  that points at it (61 of 120 records for Issue#573), which is not a distilled
   *  block but a dumping ground. Past the cap a record is simply left undrawn. */
  const FOLD_MAX = 6;
  /**
   * A record kind that must never be absorbed into another block's members.
   * Beliefs and Issues are claims: folding a claim into another block hides its
   * formation modal (the `open` prop then names a node with no card) and erases
   * the wires between claims, leaving a one-block zero-wire fold. Experiments,
   * Artifacts and Papers are formation detail and fold as before.
   */
  const NO_FOLD_KINDS = useMemo(() => new Set(["Belief", "Issue"]), []);

  /**
   * Which records become blocks, and which are absorbed into them.
   *
   * A block is a unit of knowledge, not a row of the store. Selecting by kind made the
   * fold unreadable the moment a neighbourhood was mostly one kind — Issue#573 reaches
   * 116 Issues, so "claims are Beliefs and Issues" selected clutter and distilled
   * nothing. Selection here is structural instead: a record that other records point
   * into is a hub and gets a block; a record that only points outward is the formation
   * behind some hub and is folded into it as a summary line rather than drawn.
   *
   * So a node's kind no longer decides anything. What decides is whether the record
   * carries knowledge others lean on, or is the working that produced it.
   */
  const { seeds, memberOf, absorbedInto } = useMemo(() => {
    const empty = { seeds: [] as FocusNode[], memberOf: new Map<string, FocusNode[]>(), absorbedInto: new Map<string, string>() };
    if (!graph) return empty;

    const byId = new Map(graph.nodes.map((node) => [node.id, node]));
    const incoming = new Map<string, typeof graph.edges>();
    for (const edge of graph.edges) {
      if (!byId.has(edge.from_id) || !byId.has(edge.to_id)) continue;
      incoming.set(edge.to_id, [...(incoming.get(edge.to_id) ?? []), edge]);
    }
    const inDegree = (id: string) => incoming.get(id)?.length ?? 0;

    // The focus leads; after it, the most-leaned-on records, nearest first.
    const order = [...graph.nodes].sort((a, b) => {
      if (a.id === record) return -1;
      if (b.id === record) return 1;
      if ((a.hops ?? 0) !== (b.hops ?? 0)) return (a.hops ?? 0) - (b.hops ?? 0);
      if (inDegree(b.id) !== inDegree(a.id)) return inDegree(b.id) - inDegree(a.id);
      return a.id.localeCompare(b.id); // a stable fold across reads
    });

    const picked: FocusNode[] = [];
    const members = new Map<string, FocusNode[]>();
    const owner = new Map<string, string>();

    for (const node of order) {
      if (picked.length >= CLAIM_LIMIT) break;
      if (owner.has(node.id)) continue; // already folded into a block
      picked.push(node);
      members.set(node.id, []);
      for (const edge of incoming.get(node.id) ?? []) {
        if ((members.get(node.id) ?? []).length >= FOLD_MAX) break;
        const source = byId.get(edge.from_id);
        if (!source || source.id === node.id) continue;
        if (source.id === record || owner.has(source.id)) continue;
        if (picked.some((seed) => seed.id === source.id)) continue; // a block already
        if (NO_FOLD_KINDS.has(source.kind)) continue; // a claim keeps its own block
        if (inDegree(source.id) > ABSORB_MAX_INDEGREE) continue; // a hub of its own
        owner.set(source.id, node.id);
        members.set(node.id, [...(members.get(node.id) ?? []), source]);
      }
    }
    return { seeds: picked, memberOf: members, absorbedInto: owner };
  }, [graph, record, NO_FOLD_KINDS]);

  const claimNodes = seeds;

  const claimQueries = useQueries({
    queries: claimNodes.map((node) => ({
      ...detailQueries.detail(node.id),
      staleTime: 60_000,
    })),
  });

  const detailMap = useMemo(() => {
    const map = new Map<string, Detail>();
    claimNodes.forEach((node, i) => {
      const q = claimQueries[i];
      if (q?.data) map.set(node.id, q.data);
    });
    return map;
  }, [claimNodes, claimQueries]);

  type Evidence = { readonly from: string; readonly kind: string; readonly valence: number | null };
  type WireInfo = {
    readonly fromNode: FocusNode;
    readonly toNode: FocusNode;
    readonly label: string;
    readonly open?: boolean;
    readonly why?: string;
  };

  const distilled = useMemo(() => {
    if (!graph) return null;
    const byId = new Map(graph.nodes.map((node) => [node.id, node]));
    const claims = claimNodes;

    // What bears on each claim, from the edges the store holds.
    //
    // Every incoming edge counts, whatever its source kind. A claim-to-claim edge is
    // drawn as a wire AND is evidence: those are two different jobs, not a choice.
    // Excluding them here is what made a card read "No stored edges reach this record
    // yet" while wires from other claims were visibly landing on it — in a neighbourhood
    // that is mostly Issues (Issue#573: 278 of 282 edges are claim-to-claim) that
    // fallback printed on nearly every card and the graph contradicted itself.
    const bears = new Map<string, Evidence[]>();
    const viaRun = new Map<string, string[]>(); // run id → the claims it bears on
    // A block stands for itself and for every record folded into it, so an edge landing
    // on an absorbed record is evidence for the block that absorbed it.
    const blockOf = (id: string) => (claims.some((claim) => claim.id === id) ? id : absorbedInto.get(id));
    for (const edge of graph.edges) {
      const source = byId.get(edge.from_id);
      if (!source) continue;
      const target = blockOf(edge.to_id);
      if (!target) continue;
      if (blockOf(edge.from_id) === target) continue; // inside one block, already its own summary
      bears.set(target, [...(bears.get(target) ?? []), {
        from: `${source.kind}#${source.seq}`, kind: edge.edge_kind, valence: edge.valence ?? null,
      }]);
      // "Both rest on the same run" stays a run-only inference: a claim that narrows two
      // others does not make them stand or fall together, and in a claim-dense
      // neighbourhood it would draw a wire for every pair.
      if (!CLAIM_KINDS.has(source.kind)) {
        viaRun.set(edge.from_id, [...(viaRun.get(edge.from_id) ?? []), target]);
      }
    }

    const ROW_MAX = 3;
    const seen = new Map<number, number>();
    const base = new Map<number, number>();
    let next = 0;
    for (const hop of [...new Set(claims.map((claim) => claim.hops))].sort((a, b) => a - b)) {
      base.set(hop, next);
      next += Math.ceil(claims.filter((claim) => claim.hops === hop).length / ROW_MAX);
    }

    const claimDetails = new Map<string, { readonly node: FocusNode; readonly evidence: readonly Evidence[]; readonly detail?: Detail }>();

    const blocks: FlowBlock[] = claims.map((claim, index) => {
      const at = seen.get(claim.hops) ?? 0;
      seen.set(claim.hops, at + 1);
      const held = bears.get(claim.id) ?? [];
      const detail = detailMap.get(claim.id);
      const slotLabel = `K#${index + 1}`;
      claimDetails.set(claim.id, { node: claim, evidence: held, detail });

      // The knowledge the block absorbed, said in the folded records' own words. This
      // is the point of folding: the reader sees what those records knew without them
      // each taking a node, and the block stops being a bare title with a count.
      const folded = memberOf.get(claim.id) ?? [];
      // A block whose only relations are to records it folded in has no *external*
      // evidence, but saying "no stored edges reach this record" of a block that
      // absorbed several is false twice over — the edges exist, and the block is
      // showing them as its own lines.
      const desc = held.length === 0 && folded.length > 0
        ? `${claim.kind} · ${folded.length} record${folded.length === 1 ? "" : "s"} folded in, their relations internal to this block.`
        : extractClaimDescription(detail, claim, held);
      const foldedLines = folded.slice(0, 3).map((one) => `${one.kind}#${one.seq} · ${one.title || "(untitled)"}`);
      if (folded.length > 3) foldedLines.push(`and ${folded.length - 3} more folded in`);

      return {
        slot: claim.id,
        tier: (base.get(claim.hops) ?? 0) + Math.floor(at / ROW_MAX),
        body: (
          <Block
            className="knowledge-card"
            slot={slotLabel}
            claim={claim.title || "(untitled)"}
            lines={[desc, ...foldedLines]}
            foot={`${claim.kind}#${claim.seq} · ${folded.length > 0 ? `${folded.length + 1} records folded` : "1 record"}`}
            status={claim.status}
          />
        ),
      };
    });

    const wires: FlowRelation[] = [];
    const wireDetails: WireInfo[] = [];
    const said = new Set<string>();
    const word = (kind: string) => (meta.edges[kind]?.forward ?? kind).replace(/_/g, " ");

    // Wires between blocks, distilled the same way the blocks are.
    //
    // A block stands for its folded records, so every stored edge between one block's
    // records and another's is the same relation restated. Drawing them one by one put
    // a dozen parallel arrows between the same two cards and hid the flow the fold
    // exists to show. They collapse into one wire per pair per relation, carrying the
    // count and the strongest valence, and the modal names every edge underneath it.
    type Pooled = {
      readonly from: string;
      readonly to: string;
      readonly kind: string;
      valence: number | null;
      count: number;
      readonly under: string[];
    };
    const pooled = new Map<string, Pooled>();
    for (const edge of graph.edges) {
      const from = blockOf(edge.from_id);
      const to = blockOf(edge.to_id);
      if (!from || !to || from === to) continue;
      const key = `${from}>${to}:${edge.edge_kind}`;
      const source = byId.get(edge.from_id);
      const target = byId.get(edge.to_id);
      const under = source && target
        ? `${source.kind}#${source.seq} → ${target.kind}#${target.seq}${typeof edge.valence === "number" ? ` (${edge.valence > 0 ? "+" : ""}${edge.valence})` : ""}`
        : null;
      const had = pooled.get(key);
      if (had) {
        had.count += 1;
        if (typeof edge.valence === "number" && (had.valence === null || Math.abs(edge.valence) > Math.abs(had.valence))) {
          had.valence = edge.valence;
        }
        if (under && had.under.length < 8) had.under.push(under);
        continue;
      }
      pooled.set(key, {
        from, to, kind: edge.edge_kind,
        valence: typeof edge.valence === "number" ? edge.valence : null,
        count: 1,
        under: under ? [under] : [],
      });
    }

    for (const [key, wire] of pooled) {
      said.add(key);
      const relation = word(wire.kind);
      const strength = wire.valence !== null ? ` ${wire.valence > 0 ? "+" : ""}${wire.valence}` : "";
      const many = wire.count > 1 ? ` ×${wire.count}` : "";
      const label = `${relation}${strength}${many}`;
      const isOpen = wire.valence !== null && wire.valence < 0;
      wires.push({ from: wire.from, to: wire.to, label, open: isOpen });
      const fromNode = byId.get(wire.from);
      const toNode = byId.get(wire.to);
      if (fromNode && toNode) {
        wireDetails.push({
          fromNode,
          toNode,
          label,
          open: isOpen,
          why: wire.count === 1
            ? `Filed in the store: ${fromNode.kind}#${fromNode.seq} ${relation} ${toNode.kind}#${toNode.seq}.`
            : `${wire.count} stored ${relation} edges between these two blocks and the records folded into them, distilled into one wire: ${wire.under.join(" · ")}${wire.count > wire.under.length ? " · …" : ""}.`,
        });
      }
    }

    // Derived: two claims resting on the same run stand or fall together.
    for (const [run, onto] of viaRun) {
      const source = byId.get(run);
      for (let a = 0; a < onto.length; a += 1) {
        for (let b = a + 1; b < onto.length; b += 1) {
          const key = [onto[a], onto[b]].sort().join("~");
          if (said.has(key)) continue;
          said.add(key);
          const label = `both rest on ${source ? `${source.kind}#${source.seq}` : "one run"}`;
          wires.push({ from: onto[a]!, to: onto[b]!, label, open: true });
          const fromNode = byId.get(onto[a]!);
          const toNode = byId.get(onto[b]!);
          if (fromNode && toNode) {
            wireDetails.push({
              fromNode,
              toNode,
              label,
              open: true,
              why: `Both claims rest on shared evidence from ${source ? `${source.kind}#${source.seq} (${source.title})` : "a shared run"}. Because they share the same experimental grounding, their validity is connected.`,
            });
          }
        }
      }
    }
    return { blocks, wires, wireDetails, claimDetails, records: graph.nodes.length };
  }, [graph, record, meta, CLAIM_KINDS, detailMap, claimNodes, memberOf, absorbedInto]);

  const blocks = distilled?.blocks ?? [];
  const relations = distilled?.wires ?? [];
  const pickedClaim = pickedBlock && distilled ? distilled.claimDetails.get(pickedBlock) : null;
  const pickedWireDetail = pickedWire !== null && distilled ? distilled.wireDetails[pickedWire] : null;

  return (
    <div className="ui-graph">
      {read.isError ? <p className="ui-t is-chrome">Could not read what lies around the record.</p> : null}
      {!graph ? <p className="ui-t is-chrome">Reading what lies around the record.</p> : null}
      {graph && blocks.length === 0 ? <p className="ui-t is-chrome">Nothing in the graph reaches this record yet.</p> : null}
      {graph && blocks.length > 0 ? (
        <Flow
          blocks={blocks}
          relations={relations}
          height={height ?? 420}
          onNodeClick={(id) => { setPickedWire(null); setPickedBlock(id); }}
          onEdgeClick={(idx) => { setPickedBlock(null); setPickedWire(idx); }}
        />
      ) : null}
      {graph && blocks.length > 0 ? (
        <p className="ui-t is-chrome">{`${blocks.length} ${blocks.length === 1 ? "claim" : "claims"} distilled from ${distilled?.records ?? 0} records · ${relations.length} ${relations.length === 1 ? "wire" : "wires"} · click any block or wire to see formation`}</p>
      ) : null}

      {pickedClaim ? (
        <Modal
          cls="knowledge-modal"
          kicker={`${pickedClaim.node.kind}#${pickedClaim.node.seq}`}
          note={(() => {
            // Sources are records, not edges — "136 sources" counted one record twice
            // for every pair of relations it files.
            const n = new Set(pickedClaim.evidence.map((e) => e.from)).size;
            return `${pickedClaim.node.status} · ${n} ${n === 1 ? "source" : "sources"}`;
          })()}
          title={pickedClaim.node.title || "(untitled)"}
          foot={
            <button
              type="button"
              className="btn ghost"
              onClick={() => {
                const claimText = `${pickedClaim.node.kind}#${pickedClaim.node.seq} — ${pickedClaim.node.title}`;
                setPickedBlock(null);
                window.dispatchEvent(new CustomEvent("trackinizer:chat-ask", { detail: `${claimText}: how was this formed, and which records bear on it?` }));
              }}
            >
              ask about this
            </button>
          }
          onClose={() => setPickedBlock(null)}
        >
          <p className="knowledge-sec">How it was formed</p>
          <div className="knowledge-spine">
            <div className="knowledge-me">
              <p className="knowledge-src-h">
                <span className="mono">{`${pickedClaim.node.kind}#${pickedClaim.node.seq}`}</span>
              </p>
              <p className="knowledge-src-line">{pickedClaim.node.title}</p>
              {/* The record's own words, if it has any. Passing no evidence here is
                  deliberate — the sources are listed below — but it used to mean a
                  record with no stored description printed "No stored edges reach this
                  record yet" directly above a list of the edges that reach it. */}
              {ownWords(pickedClaim.detail) ? (
                <p className="knowledge-src-note" style={{ marginTop: 6, color: "var(--ink-machine)", fontSize: "11.5px", lineHeight: "1.5" }}>
                  {ownWords(pickedClaim.detail)}
                </p>
              ) : null}
            </div>
            {pickedClaim.evidence.length > 0 ? (
              /* One entry per RECORD, not per edge. A record filing both `narrows` and
                 `produced_by` was two rows saying the same name, so 68 records became a
                 136-row scroll with every name printed twice. Capped, because a panel
                 longer than the screen is a dump, not a formation. */
              (() => {
                const byRecord = new Map<string, { kind: string; valence: number | null }[]>();
                for (const ev of pickedClaim.evidence) {
                  byRecord.set(ev.from, [...(byRecord.get(ev.from) ?? []), { kind: ev.kind, valence: ev.valence }]);
                }
                const all = [...byRecord.entries()];
                const shown = all.slice(0, 12);
                return <>
                  <ol className="knowledge-sources">
                    {shown.map(([from, rels]) => (
                      <li key={from}>
                        <p className="knowledge-src-h">
                          <span className="mono">{from}</span>
                        </p>
                        <p className="knowledge-src-line">
                          {rels.map((r) => `${r.kind}${r.valence !== null ? ` (${r.valence > 0 ? "+" : ""}${r.valence})` : ""}`).join(" · ")}
                        </p>
                      </li>
                    ))}
                  </ol>
                  {all.length > shown.length ? (
                    <p className="knowledge-none">{`and ${all.length - shown.length} more records point in — open the record to read them all.`}</p>
                  ) : null}
                </>;
              })()
            ) : (
              <p className="knowledge-none">No records in this neighborhood point into this claim yet.</p>
            )}
          </div>
        </Modal>
      ) : null}

      {pickedWireDetail ? (
        <Modal
          cls="knowledge-modal"
          kicker={`${pickedWireDetail.fromNode.kind}#${pickedWireDetail.fromNode.seq} → ${pickedWireDetail.toNode.kind}#${pickedWireDetail.toNode.seq}`}
          note="a distilled wire"
          title={`${pickedWireDetail.fromNode.title} — ${pickedWireDetail.label}: ${pickedWireDetail.toNode.title}`}
          foot={
            <button
              type="button"
              className="btn ghost"
              onClick={() => {
                const fromLabel = `${pickedWireDetail.fromNode.kind}#${pickedWireDetail.fromNode.seq}`;
                const toLabel = `${pickedWireDetail.toNode.kind}#${pickedWireDetail.toNode.seq}`;
                setPickedWire(null);
                window.dispatchEvent(new CustomEvent("trackinizer:chat-ask", { detail: `Why is ${fromLabel} ${pickedWireDetail.label} ${toLabel}? ${pickedWireDetail.why ?? ""}` }));
              }}
            >
              ask about this
            </button>
          }
          onClose={() => setPickedWire(null)}
        >
          <p className="knowledge-sec">What it starts at</p>
          <p className="knowledge-src-line">
            <b>{`${pickedWireDetail.fromNode.kind}#${pickedWireDetail.fromNode.seq}`}:</b> {pickedWireDetail.fromNode.title}
          </p>
          <p className="knowledge-sec">What it points at</p>
          <p className="knowledge-src-line">
            <b>{`${pickedWireDetail.toNode.kind}#${pickedWireDetail.toNode.seq}`}:</b> {pickedWireDetail.toNode.title}
          </p>
          <p className="knowledge-sec">How this wire was distilled</p>
          <p className="knowledge-src-line">
            {pickedWireDetail.why ?? `The distiller drew this wire: ${pickedWireDetail.label}.`}
          </p>
        </Modal>
      ) : null}
    </div>
  );
}

/**
 * A composed surface.
 *
 * The items read down the page in the order given, against the left rule, because
 * that is the one arrangement the law states for everything that is not a named
 * family: a reader scans the left edge for the next thing.
 */
export function ComposedView({ flow }: { readonly flow: ComposedFlow }) {
  const kinds = useContext(MetaContext)?.kinds ?? NO_KINDS;
  const words = useMemo(() => wordsOf(flow), [flow]);
  // What the reader is reading about, for the rail's Graph to open on (`graph/topic`).
  // Cleared on the way out only when it is still ours: the canvas can draw a second
  // composition before this one is taken down, and that one's subject is the live one.
  useEffect(() => {
    const mine = topicOf(words, kinds);
    setTopic(mine);
    return () => {
      if (sameTopic(currentTopic(), mine)) setTopic(null);
    };
  }, [words, kinds]);
  return (
    <section className="ui-composed" aria-label={flow.title}>
      <p className="ui-composed-h">
        <b>{flow.title}</b>
        <em>{flow.prompt}</em>
      </p>
      <div className="ui-composed-b">
        {flow.compose.map((item, at) => <ItemView item={item} key={at} />)}
      </div>
    </section>
  );
}

/** One array, so a composition drawn without a `MetaContext` does not re-run on every render. */
const NO_KINDS: readonly string[] = [];

/**
 * The words of `flow` a ref can be read out of: its title and prompt, and the text and
 * notes its items carry. Not the whole item — a figure's axis labels and a table's cells
 * are data the agent put there, and a `#` in one of them names no row.
 */
function wordsOf(flow: ComposedFlow): string {
  const said = flow.compose.flatMap((item) => {
    const held = item as { readonly text?: unknown; readonly note?: unknown; readonly label?: unknown };
    return [held.text, held.note, held.label].filter((words) => typeof words === "string");
  });
  return [flow.title, flow.prompt, ...said].join("\n\n");
}
