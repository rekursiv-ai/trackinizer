import type { CSSProperties, ReactNode } from "react";
import type { InquiryRow } from "../api/inquiries";
import type { Selection } from "../bulk/selection";
import { useCommands } from "../commands/registry";
import { useAncestry } from "../lists/ancestry";
import { Row } from "../lists/Row";
import { formatRoute } from "../router/route";
import { ReadFailure } from "../ui/failure";
import { StatusGlyph } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { buildOutline, type Outline, type OutlineLine, type OutlineNode } from "./tree";
import "./outline.css";

/** An Issue list's rows as an outline, with what the list's keys and selection need. */
export type OutlineView = Outline & {
  /** The No parent group is open. */
  readonly orphansOpen: boolean;
  /** What j and k move through, in order: each line's own Issue, then the No parent rows. */
  readonly order: readonly OutlineNode[];
  /** The list's rows shown, in order, for selecting. */
  readonly rows: readonly InquiryRow[];
  /** Nothing to show yet: the ancestry's first read is on its way. */
  readonly pending: boolean;
  readonly error: Error | null;
  readonly retry: () => void;
};

/**
 * The outline of `rows`, the rows a list shows in its order, nested under their
 * `narrows` parents (LV1), its ancestry read and kept current while mounted;
 * null, reading nothing, when `rows` is. `collapsed` is the list's stored
 * collapse keys, as `onToggle` hands them over.
 */
export function useOutline(rows: readonly InquiryRow[] | null, collapsed: readonly string[]): OutlineView | null {
  const { ancestry, pending, error, retry } = useAncestry(rows && rows.map((row) => row.id));
  if (!rows) return null;
  const folded = new Set(collapsed.flatMap((key) => (key.startsWith(KEY_PREFIX) ? [key.slice(KEY_PREFIX.length)] : [])));
  const outline = buildOutline(rows, ancestry, folded);
  const orphansOpen = !collapsed.includes(NO_PARENT_KEY);
  const orphans = orphansOpen ? outline.orphans : [];
  return {
    ...outline,
    orphansOpen,
    order: [...outline.lines.map(lineNode), ...orphans.map(rowNode)],
    rows: [...outline.lines.flatMap((line) => lineNode(line).row ?? []), ...orphans],
    pending,
    error,
    retry,
  };
}

/** ← collapses the focused line and → expands it, while the list is an outline. */
export function useOutlineKeys(outline: OutlineView | null, focusedId: string | null, toggle: (key: string) => void): void {
  const fold = (expanded: boolean) => {
    const line = outline?.lines.find((candidate) => lineNode(candidate).id === focusedId);
    if (line?.parent && line.expanded === expanded) toggle(outlineKey(lineNode(line).id));
  };
  useCommands(
    outline
      ? [
          { id: "outline.collapse", title: "Collapse", keys: ["ArrowLeft"], run: () => fold(true) },
          { id: "outline.expand", title: "Expand", keys: ["ArrowRight"], run: () => fold(false) },
        ]
      : [],
  );
}

/**
 * The outline's lines in a list: rows nested under their parents, older
 * parents dimmed, each line with children folding by its chevron, and the No
 * parent group at the end.
 */
export function OutlineRows({
  outline,
  focusedId,
  now,
  onFocus,
  onToggle,
  selection,
}: {
  outline: OutlineView;
  focusedId: string | null;
  now: number;
  onFocus: (id: string) => void;
  onToggle: (key: string) => void;
  selection: Selection;
}) {
  const empty = outline.lines.length === 0 && outline.orphans.length === 0;
  if (outline.error && empty) return <ReadFailure error={outline.error} retry={outline.retry} />;
  if (outline.pending) return <p className="list-loading">Loading…</p>;
  const rowLine = (row: InquiryRow, depth: number, twisty: ReactNode, note: ReactNode) => (
    <div key={row.id} className="o-line" data-depth={depth} style={{ "--depth": depth } as CSSProperties}>
      {twisty}
      <Row
        row={row}
        mixed={false}
        focused={row.id === focusedId}
        now={now}
        onFocus={onFocus}
        selected={selection.ids.has(row.id)}
        onSelect={selection.enabled ? selection.select : undefined}
        note={note}
      />
    </div>
  );
  return (
    <>
      {outline.lines.length > 0 && (
        <section aria-label="Outline">
          {outline.lines.map((line) => {
            const node = lineNode(line);
            const toggle = () => {
              onToggle(outlineKey(node.id));
              onFocus(node.id);
            };
            const twisty = <Twisty line={line} onToggle={toggle} />;
            if (node.row) return rowLine(node.row, line.depth, twisty, lineNote(line));
            return (
              <div key={node.id} className="o-line" data-depth={line.depth} style={{ "--depth": line.depth } as CSSProperties}>
                {twisty}
                <Older line={line} focused={node.id === focusedId} onFocus={onFocus} />
              </div>
            );
          })}
        </section>
      )}
      {outline.orphans.length > 0 && (
        <section aria-label="No parent">
          <button
            type="button"
            className={outline.orphansOpen ? "group-h o-group" : "group-h o-group is-collapsed"}
            aria-expanded={outline.orphansOpen}
            onClick={() => onToggle(NO_PARENT_KEY)}
          >
            <Icon name="chevD" size={14} className="chev" />
            <span>No parent</span>
            <span className="count">{outline.orphans.length}</span>
          </button>
          {outline.orphansOpen && outline.orphans.map((row) => rowLine(row, 0, <span className="o-twisty" />, null))}
        </section>
      )}
    </>
  );
}

/** The chevron that folds a line with lines under it; a gap of its width on any other. */
function Twisty({ line, onToggle }: { line: OutlineLine; onToggle: () => void }) {
  if (!line.parent) return <span className="o-twisty" />;
  const { seq } = lineNode(line);
  return (
    <button
      type="button"
      className={line.expanded ? "o-twisty" : "o-twisty is-collapsed"}
      aria-expanded={line.expanded}
      aria-label={`${line.expanded ? "Collapse" : "Expand"} #${seq}`}
      onClick={onToggle}
    >
      <Icon name="chevD" size={14} className="chev" />
    </button>
  );
}

/** A line of parents outside the page, dimmed: one, or a compressed chain, opening the last. */
function Older({ line, focused, onFocus }: { line: OutlineLine; focused: boolean; onFocus: (id: string) => void }) {
  const node = lineNode(line);
  return (
    <a
      className={focused ? "o-older is-focused" : "o-older"}
      href={formatRoute({ name: "ref", kind: node.kind, seq: node.seq })}
      // Marked as a row, so the list scrolls it into view when focused.
      data-row={node.id}
      aria-current={focused ? "true" : undefined}
      onClick={() => onFocus(node.id)}
    >
      <StatusGlyph status={node.status} />
      <span className="row-ref">{line.nodes.map(({ seq }) => `#${seq}`).join(" › ")}</span>
      <span className="row-title">{line.nodes.map(({ title }) => title).join(" › ")}</span>
      <span className="o-tag" title="Not among the rows this list loaded">
        older
      </span>
      <span className="row-meta">{lineNote(line)}</span>
    </a>
  );
}

/** What a line says besides its Issue: its other parents, and, folded, how many rows it holds. */
function lineNote(line: OutlineLine): ReactNode {
  const also = line.alsoUnder.map(({ seq }) => `#${seq}`).join(", ");
  const folded = line.parent && !line.expanded;
  if (!also && !folded) return null;
  return (
    <>
      {also && <span className="o-also">also under {also}</span>}
      {folded && <span className="o-below">{line.below === 1 ? "1 issue" : `${line.below} issues`}</span>}
    </>
  );
}

/** The Issue a line ends with: the one it opens, and the one its children narrow. */
function lineNode(line: OutlineLine): OutlineNode {
  return line.nodes.at(-1)!;
}

/** The list's stored key for a collapsed line, by the Issue it ends with. */
function outlineKey(id: string): string {
  return `${KEY_PREFIX}${id}`;
}

function rowNode(row: InquiryRow): OutlineNode {
  return { id: row.id, kind: row.kind, seq: row.seq, title: row.title, status: row.status, row };
}

const KEY_PREFIX = "outline:";

/** The No parent group's collapse key; no Issue id is `none`. */
const NO_PARENT_KEY = `${KEY_PREFIX}none`;
