// THE DESIGN LIBRARY. Four primitives, one ink rule, no content: every answer
// surface the app draws is built out of these, so a new design costs a data shape
// and not a stylesheet. The model never writes any of this code — it fills a type
// (see review.tsx) and these components decide where ink and hue go.
//
// The rules the primitives carry, measured on the prototype they were ported from:
// white is what a scientist reads (prose, a mark's words); the neutral machine ink
// only locates (a path, a count); hue lives on a mark's glyph and never on its
// words, so a coloured thing on this pane always means a verdict about the shape.
import { Fragment, type ReactNode } from "react";
import "./sections.css";

/** What a mark says about the line beside it: kept, wrong, plain, the one demand, or —
 *  in a row's own gutter — the reading that is out of place. `flag` is the row-level
 *  one: a verdict about a measurement rather than about a sentence, which is why it
 *  takes a glyph of its own. The first line is the one demand and takes the arrow —
 *  the exact column, constant or test to write. The rest stand beside it. */
export type MarkKind = "fit" | "finding" | "note" | "demand" | "flag";

/** A line with a glyph on it. The glyph is the component's choice, never the model's. */
export type Mark = { readonly kind: MarkKind; readonly text: string };

/** The glyph each kind wears. `+` is a convention this change keeps, `−` the one it
 *  breaks, `·` a line that judges nothing, `→` the single thing to do, and `▲` the one
 *  reading in a measured run that is out of place. */
const GLYPH: { readonly [kind in MarkKind]: string } = { fit: "+", finding: "−", note: "·", demand: "→", flag: "▲" };

/** Text with its `backticked` tokens read as code: a tick is markup and never prints. */
export function ticks(text: string): readonly ReactNode[] {
  return text.split(/(`[^`]+`)/).filter(Boolean).map((part, index) =>
    part.length > 2 && part.startsWith("`") && part.endsWith("`")
      ? <code className="ui-tok" key={index}>{part.slice(1, -1)}</code>
      : <Fragment key={index}>{part}</Fragment>);
}

/** A section rule: the label, the hairline that carries the width, and the size at its end. */
export function Section({ label, size }: { readonly label: string; readonly size?: string }) {
  return (
    <p className="ui-sec">
      <b>{label}</b>
      <s aria-hidden="true" />
      {size ? <em>{size}</em> : null}
    </p>
  );
}

/** Prose: the one surface on a pane that is read, so it is white at answer size. */
export function Prose({ lead, span, children }: { readonly lead?: boolean; readonly span?: boolean; readonly children: ReactNode }) {
  return <p className={`ui-prose${lead ? " is-lead" : ""}${span ? " is-span" : ""}`}>{asText(children)}</p>;
}

/** The marked lines. Hue goes on the glyph, never on a word. The words go in their own
 *  box: the row is a flex one, so a loose token would take the glyph's gap with it. */
export function Marks({ items, span }: { readonly items: readonly Mark[]; readonly span?: boolean }) {
  return (
    <div className={`ui-marks${span ? " is-span" : ""}`}>
      {items.map((mark, index) => (
        <p className={`ui-mark is-${mark.kind}`} key={index}>
          <b aria-hidden="true">{GLYPH[mark.kind]}</b>
          <span>{ticks(mark.text)}</span>
        </p>
      ))}
    </div>
  );
}

/** One address, broken after a slash and nowhere else, so a long path stays typeable. If
 *  `link` is provided, each part becomes a clickable anchor that opens the file at that
 *  line; otherwise it's plain text. This lets compositions render actionable file paths
 *  without breaking the hosted-page sandbox constraint (DESIGN.md: link href sits dead). */
export function Path({ address, link }: { readonly address: string; readonly link?: string }) {
  const parts = address.split("/");
  return (
    <>
      {parts.map((part, index) => (
        <Fragment key={index}>
          {index === parts.length - 1 ? (
            link ? (
              <a href={link} className="ui-link" target="_blank" rel="noreferrer">
                {part}
              </a>
            ) : (
              part
            )
          ) : (
            <>
              {part}/
              <wbr />
            </>
          )}
        </Fragment>
      ))}
    </>
  );
}

/** The addresses under a sentence about them: paths, two to a row, in machine ink. */
export function Paths({ items }: { readonly items: readonly string[] }) {
  return (
    <div className="ui-paths">
      {items.map((path) => (
        <span key={path}><Path address={path} /></span>
      ))}
    </div>
  );
}

/** The prior, the one line under the sections that came out of somebody else's ledger. */
export function Footnote({ children }: { readonly children: ReactNode }) {
  return <p className="ui-note">{asText(children)}</p>;
}

/** A document heading, at one of three ranks.
 *
 *  `Section` is the machine register — a 10.5px uppercase mono rule with a measure at
 *  its end — and it is right on a pane of telemetry and wrong on anything meant to be
 *  read as a document, where it gives a page no hierarchy at all. These are the other
 *  register: real rank, set in the prose face, sized so a reader can see the shape of
 *  the page before reading a word of it. One `1` per surface; `2` opens a part; `3`
 *  opens a point inside one. */
export function Heading({ level, children }: { readonly level: 1 | 2 | 3; readonly children: ReactNode }) {
  const Tag = `h${level}` as const;
  return <Tag className={`ui-h ui-h${level}`}>{asText(children)}</Tag>;
}

/** A machine line: a row that is read for its figures, so the whole row takes the
 *  machine face and the reading ink. Prose is the wrong node for one of these — it
 *  sets numbers in the prose face, and the law holds that every figure is machine.
 *  The leading cell keeps its width so a column of these aligns down the page. */
export function MachineRow({ cells }: { readonly cells: readonly string[] }) {
  return (
    <p className="ui-mrow">
      {cells.map((cell, index) => (
        <span key={index}>{ticks(cell)}</span>
      ))}
    </p>
  );
}

/** A primitive's own words get the tick reading; a consumer's element stays as it is. */
const asText = (children: ReactNode): ReactNode =>
  typeof children === "string" ? ticks(children) : children;
