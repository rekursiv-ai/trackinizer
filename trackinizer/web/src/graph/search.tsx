import { type KeyboardEvent, type ReactNode, type RefObject, useEffect } from "react";
import type { GraphNode } from "../api/graph";
import { formatRoute } from "../router/route";
import { StatusGlyph } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { KindIcon } from "../ui/kinds";
import { type Panel, type PanelSpec, PanelStrip, PanelToggle } from "../ui/panel";

/** The search results as a panel: on the graph's left, and `[` collapses or expands it. */
export const RESULTS: PanelSpec = { id: "graph.results", name: "search results", side: "left", keys: ["["] };

/** What a key in the search box asks for. */
export type SearchKey = "next" | "previous" | "pick" | "everywhere" | "leave";

/**
 * The search box in the graph's tools: the app's search field, with its icon
 * and its key. ↑ and ↓ move through the matches, Enter picks the marked one
 * (the first when none is), Shift+Enter searches every inquiry, and Escape
 * empties the box and leaves it; `onKey` hears each.
 */
export function SearchBox({
  query,
  onQuery,
  onKey,
  inputRef,
  listId,
  activeId,
}: {
  query: string;
  onQuery: (text: string) => void;
  onKey: (key: SearchKey) => void;
  inputRef: RefObject<HTMLInputElement | null>;
  /** The matches' listbox, while it shows. */
  listId: string | null;
  /** The marked match's element. */
  activeId: string | undefined;
}) {
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    const key = KEYS[event.key === "Enter" && event.shiftKey ? "Shift+Enter" : event.key];
    if (!key) return;
    event.preventDefault();
    if (key === "leave") event.currentTarget.blur();
    onKey(key);
  };
  return (
    <div className="graph-search">
      <Icon name="search" size={14} />
      <input
        ref={inputRef}
        role="combobox"
        aria-label="Search the graph"
        aria-expanded={listId !== null}
        aria-controls={listId ?? undefined}
        aria-autocomplete="list"
        aria-activedescendant={activeId}
        placeholder="Search the graph"
        autoComplete="off"
        value={query}
        onChange={(event) => onQuery(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <kbd>{query ? "esc" : "/"}</kbd>
    </div>
  );
}

/** One group of matches: those within a focus's hops, those elsewhere, or all of them (no name). */
export type MatchGroup = { readonly name: string | null; readonly rows: readonly GraphNode[] };

/**
 * The matches, in the left column: their count and the button that collapses
 * them (`panel`), then each group under its heading, each match a list's row
 * (kind, status, `#seq`, the title with the query marked, and its hops from the
 * focus). A click picks a match and a double-click focuses on it. The foot
 * searches every inquiry for the query on the search page. Collapsed, a strip
 * with the count.
 *
 * Each group lists its first `MAX_ROWS`. The rows are numbered across the
 * groups, `marked` the one the keys marked, which scrolls into view; each row's
 * element id is `<id>-<number>`.
 */
export function SearchResults({
  id,
  query,
  total,
  groups,
  marked,
  hopsOf,
  onPick,
  onFocus,
  panel,
}: {
  id: string;
  query: string;
  total: number;
  groups: readonly MatchGroup[];
  marked: number;
  hopsOf: (row: GraphNode) => number | undefined;
  onPick: (row: GraphNode) => void;
  onFocus: (row: GraphNode) => void;
  panel: Panel;
}) {
  useEffect(() => {
    if (marked >= 0) document.getElementById(`${id}-${marked}`)?.scrollIntoView({ block: "nearest" });
  }, [id, marked]);
  let number = 0;
  const row = (match: GraphNode) => {
    const at = number++;
    const far = hopsOf(match);
    return (
      <div
        key={match.id}
        id={`${id}-${at}`}
        role="option"
        aria-selected={at === marked}
        className={at === marked ? "row is-focused" : "row"}
        // The search box keeps the keyboard, so typing goes on after a click.
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => onPick(match)}
        onDoubleClick={() => onFocus(match)}
      >
        <KindIcon kind={match.kind} size={14} />
        <StatusGlyph status={match.status} />
        <span className="row-ref">#{match.seq}</span>
        <span className="row-title">{marks(match.title || "(untitled)", query)}</span>
        {far === undefined ? null : <span className="row-meta">{far === 0 ? "focus" : hopsName(far)}</span>}
      </div>
    );
  };
  const text = query.trim();
  const counted = total === 1 ? "1 match" : `${total.toLocaleString("en-US")} matches`;
  if (panel.collapsed) {
    return (
      <PanelStrip panel={panel} id={`${id}-panel`} label="Search results">
        <span className="panel-strip-text">{counted}</span>
      </PanelStrip>
    );
  }
  return (
    <aside id={`${id}-panel`} className="graph-panel" aria-label="Search results">
      <div className="graph-panel-h">
        <span>
          <b>{counted}</b> for “{text}”
        </span>
        <PanelToggle panel={panel} controls={`${id}-panel`} />
      </div>
      <div className="scroll" role="listbox" id={id} aria-label="Matches">
        {total === 0 ? <p className="list-note">No matches among the nodes drawn</p> : null}
        {groups.map((group) =>
          group.name === null ? (
            [...group.rows.slice(0, MAX_ROWS).map(row), <More key="more" rows={group.rows} />]
          ) : (
            <div key={group.name} role="group" aria-label={group.name}>
              <div className="group-h" aria-hidden="true">
                {group.name}
                <span className="count">{group.rows.length.toLocaleString("en-US")}</span>
              </div>
              {group.rows.slice(0, MAX_ROWS).map(row)}
              <More rows={group.rows} />
            </div>
          ),
        )}
      </div>
      <a className="graph-panel-foot" href={formatRoute({ name: "search", q: text })}>
        <Icon name="search" size={14} />
        <span className="row-title">Search every inquiry for “{text}”</span>
        <kbd>⇧</kbd>
        <kbd>↵</kbd>
      </a>
    </aside>
  );
}

/** Past `MAX_ROWS`, how many matches the list leaves out. */
function More({ rows }: { rows: readonly GraphNode[] }) {
  return rows.length > MAX_ROWS ? <p className="list-note">{(rows.length - MAX_ROWS).toLocaleString("en-US")} more: type more to narrow them</p> : null;
}

/** The most matches a group lists; more would make every keystroke slow on a big graph. */
export const MAX_ROWS = 200;

/** `1 hop`, `2 hops`. */
export function hopsName(far: number): string {
  return far === 1 ? "1 hop" : `${far} hops`;
}

/**
 * The inquiries among `nodes` that `query` finds, newest first, as v1's graph
 * search matched them: `#42` or bare digits match a seq exactly, of any kind;
 * other text matches a substring of the title or the kind, in any case. Blank
 * text finds nothing.
 */
export function searchNodes<Node extends GraphNode>(nodes: readonly Node[], query: string): Node[] {
  const text = query.trim().toLowerCase();
  if (!text) return [];
  const digits = text.replace(/^#/, "");
  const matches = /^\d+$/.test(digits)
    ? (found: Node) => String(found.seq) === digits
    : (found: Node) => found.title.toLowerCase().includes(text) || found.kind.toLowerCase().includes(text);
  // The server sends nodes oldest first.
  return nodes.filter(matches).toReversed();
}

/** `title` with the first stretch of it that `query` matches marked. */
function marks(title: string, query: string): ReactNode {
  const text = query.trim().toLowerCase();
  const at = text ? title.toLowerCase().indexOf(text) : -1;
  if (at < 0) return title;
  return (
    <>
      {title.slice(0, at)}
      <mark>{title.slice(at, at + text.length)}</mark>
      {title.slice(at + text.length)}
    </>
  );
}

const KEYS: { readonly [key: string]: SearchKey } = {
  ArrowDown: "next",
  ArrowUp: "previous",
  Enter: "pick",
  "Shift+Enter": "everywhere",
  Escape: "leave",
};
