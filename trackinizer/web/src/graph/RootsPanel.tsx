import { type KeyboardEvent, useEffect, useId, useRef, useState } from "react";
import { dateTime, useMinuteClock } from "../detail/time";
import { shortAge } from "../lists/Row";
import { Icon } from "../ui/icons";
import { KindIcon } from "../ui/kinds";
import type { RootGroup, Roots } from "./roots";
import "./roots.css";

/**
 * The graph's roots in a list to scan, one row each: the root's kind glyph,
 * `Kind#seq` and title, how many nodes are under it, and how long ago the
 * newest of them was made. The nodes under no root are the last row.
 *
 * Recent sorts the roots by their newest node, Size by how many nodes they
 * hold. Typing in the box filters them by title or `Kind#seq`. j and k, or the
 * arrows, mark a row; Enter, or a click, selects it and calls `onSelect` with
 * its group's key (`RootGroup.key`), and the list takes the keys, the filter
 * cleared. A letter typed on the list goes to the box; other keys, such as Esc,
 * are the view's.
 *
 * `compact`, as beside an open Peek, it narrows to a strip of the rows' glyphs,
 * each named by its root, without the box and the sort; its keys still work.
 * `id` is the panel's, for the button that shows and hides it.
 */
export function RootsPanel({
  id: panelId,
  roots,
  onSelect,
  compact = false,
}: {
  id?: string;
  roots: Roots;
  onSelect: (key: string) => void;
  compact?: boolean;
}) {
  const [order, setOrder] = useState<"recent" | "size">("recent");
  const [query, setQuery] = useState("");
  const [marked, setMarked] = useState<string | null>(null);
  const filter = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const id = useId();
  const now = useMinuteClock();
  const shown = sorted(roots.groups, order).filter((group) => matches(group, query));
  const at = shown.findIndex((group) => group.key === marked);
  useEffect(() => {
    if (at >= 0) document.getElementById(`${id}-${at}`)?.scrollIntoView({ block: "nearest" });
  }, [id, at]);

  const pick = (group: RootGroup | undefined) => {
    if (!group) return;
    setMarked(group.key);
    // A pick opens Peek, which narrows the panel and takes the box away: a
    // filter kept would hide roots with no box to say so, and the keys would
    // fall to the page.
    setQuery("");
    list.current?.focus();
    onSelect(group.key);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    const typing = event.target === filter.current;
    // The sort's buttons take their own Enter.
    if (!typing && event.target !== list.current) return;
    if (event.nativeEvent.isComposing || event.metaKey || event.ctrlKey || event.altKey) return;
    const step = { ArrowDown: 1, ArrowUp: -1, ...(typing ? {} : { j: 1, k: -1 }) }[event.key];
    if (step !== undefined && shown.length > 0) {
      setMarked(shown[Math.min(Math.max(at + step, 0), shown.length - 1)]!.key);
    } else if (event.key === "Enter") {
      pick(shown[Math.max(at, 0)]);
    } else if (event.key === "Escape" && typing && query !== "") {
      setQuery("");
    } else if (!compact && !typing && event.key.length === 1 && event.key !== " ") {
      // `/` only moves to the box, as it does to the graph's search.
      if (event.key !== "/") setQuery((typed) => typed + event.key);
      filter.current?.focus();
    } else {
      return;
    }
    event.preventDefault();
  };

  const count = roots.groups.filter((group) => group.root).length;
  return (
    <aside id={panelId} className={compact ? "roots is-compact" : "roots"} aria-label="Roots" onKeyDown={onKeyDown}>
      {compact ? null : (
        <>
          <div className="roots-head">
            <span className="roots-count">{count === 1 ? "1 root" : `${count} roots`}</span>
            <span className="spacer" />
            <div className="view-seg" role="group" aria-label="Sort roots">
              {SORTS.map(([value, label]) => (
                <button key={value} type="button" aria-pressed={order === value} onClick={() => setOrder(value)}>
                  {label}
                </button>
              ))}
            </div>
          </div>
          <div className="menu-search">
            <Icon name="search" size={14} />
            <input
              ref={filter}
              type="search"
              aria-label="Filter roots"
              aria-controls={id}
              placeholder="Filter roots"
              autoComplete="off"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>
        </>
      )}
      <ul
        ref={list}
        className="roots-list"
        role="listbox"
        id={id}
        aria-label="Roots"
        tabIndex={0}
        aria-activedescendant={at >= 0 ? `${id}-${at}` : undefined}
      >
        {shown.map((group, index) => (
          <li
            key={group.key}
            id={`${id}-${index}`}
            role="option"
            aria-selected={index === at}
            className={index === at ? "roots-row is-focused" : "roots-row"}
            aria-label={compact ? name(group) : undefined}
            title={compact ? name(group) : undefined}
            onClick={() => pick(group)}
          >
            {group.root ? <KindIcon kind={group.root.kind} size={14} /> : <Icon name="layers" size={14} />}
            {compact ? null : (
              <>
                <span className="roots-ref">{group.root ? `${group.root.kind}#${group.root.seq}` : ""}</span>
                <span className="roots-title">{group.root ? group.root.title || "(untitled)" : "Unrooted"}</span>
                <span className="roots-size" title={[...group.kinds].map(([kind, n]) => `${n} ${kind}`).join(", ")}>
                  {group.members.length}
                </span>
                <span className="roots-age" title={`Newest ${dateTime(group.newest)}`}>
                  {shortAge(group.newest, now)}
                </span>
              </>
            )}
          </li>
        ))}
      </ul>
      {shown.length === 0 && <p className="roots-none">{roots.groups.length === 0 ? "No roots" : "No matches"}</p>}
    </aside>
  );
}

/** A group's name: its root's `Kind#seq` and title, or Unrooted. */
function name({ root }: RootGroup): string {
  return root ? `${root.kind}#${root.seq} ${root.title || "(untitled)"}` : "Unrooted";
}

/** The groups in `order`, those under no root last whatever it is; `findRoots` gives them in Recent's. */
function sorted(groups: readonly RootGroup[], order: "recent" | "size"): readonly RootGroup[] {
  if (order === "recent") return groups;
  return [...groups.filter((group) => group.root).toSorted((a, b) => b.members.length - a.members.length), ...groups.filter((group) => !group.root)];
}

/** Whether `query` is in the group's `Kind#seq` and title, in any case; blank matches all. */
function matches({ root }: RootGroup, query: string): boolean {
  const text = root ? `${root.kind}#${root.seq} ${root.title}` : "Unrooted";
  return text.toLowerCase().includes(query.trim().toLowerCase());
}

const SORTS = [
  ["recent", "Recent"],
  ["size", "Size"],
] as const;
