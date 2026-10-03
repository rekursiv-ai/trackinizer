import { type MouseEvent, type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { type Meta, useMeta } from "../app/boot";
import { isPageCounts, isStrings, useTabState } from "../app/tabState";
import { BulkBar } from "../bulk/BulkBar";
import { ColumnsView } from "../columns";
import { type Selection, useSelection } from "../bulk/selection";
import { listCommands } from "../commands/list";
import { keyCaps, useCommands } from "../commands/registry";
import { useMinuteClock } from "../detail/time";
import { LiveRows, useLiveList } from "../live";
import { useSteadyLayout } from "../live/steady";
import { OutlineRows, useOutline, useOutlineKeys } from "../outline";
import {
  type ChoiceField,
  type Choice,
  compileQuery,
  isChoices,
  isListTab,
  type ListQuery,
  removeChoice,
  type Tab,
  toggleChoice,
  traxLine,
  withTab,
} from "../query/query";
import { whenIdle } from "../router/lazy";
import { useRouter } from "../router/router";
import { DetailView } from "../router/views";
import { useMe } from "../state/me";
import { StreamsRows, useStreams } from "../streams";
import { Bar } from "../ui/bars";
import { ReadFailure } from "../ui/failure";
import { Avatar, capitalize, JudgementGlyph, PRIORITY_NAMES, PriorityGlyph, StatusGlyph } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { KindIcon, kindLook } from "../ui/kinds";
import { panelCommand, usePanel } from "../ui/panel";
import { PEEK, Peek } from "../ui/Peek";
import { useCopy } from "../ui/toast";
import { EmptyState, ViewHeader } from "../ui/view";
import {
  defaultDisplay,
  type Group,
  type Grouping,
  groupings,
  groupRows,
  type Ordering,
  orderings,
  sortRows,
} from "./display";
import { FilterChips, FilterMenu } from "./filters";
import "./lists.css";
import { Menu } from "./Menu";
import { useListPages } from "./pages";
import { useDetailWarmer } from "./prefetch";
import { Row } from "./Row";
import { isStoredView, offeredViews, type StoredView, type View, ViewSwitch } from "./views";

/** One kind's list: `#/list/<kind>`. */
export function ListView({ kind }: { kind: string }) {
  const kinds = useMemo(() => [kind], [kind]);
  // Keyed, so another kind's list starts from its own state, not this one's.
  return (
    <InquiryList key={kind} id={kind} kinds={kinds} title={kindLook(kind).plural} icon={<KindIcon kind={kind} />} />
  );
}

/**
 * A list of inquiries of `kinds`, as the mock's Browse list: the Active, Closed
 * and All tabs, the Filter menu, the `trax` line, Group and Sort over loaded
 * rows, and Load more.
 *
 * Several kinds load 20 rows each, one kind 50. Its tab, filters, grouping,
 * pages and focused row last for this browser tab under `id`, so Back returns to
 * the list as it was.
 */
export function InquiryList({
  id,
  kinds,
  title,
  icon,
}: {
  id: string;
  kinds: readonly string[];
  title: string;
  icon: ReactNode;
}) {
  const meta = useMeta();
  const me = useMe();
  const { navigate, route } = useRouter();
  const [state, update] = useTabState(`trackinizer.v2.list.${id}`, readListState, () => initialState(kinds, meta));
  const views = offeredViews(kinds);
  // Columns lives in the hash, with the Issues selected column by column.
  const path = route.name === "list" && views.includes("columns") ? (route.columns ?? null) : null;
  const view: View = path ? "columns" : views.includes(state.view) ? state.view : "list";
  const query: ListQuery = useMemo(
    () => ({ kinds, tab: state.tab, choices: state.choices }),
    [kinds, state.tab, state.choices],
  );
  // Columns' first column is the list's roots: its tab and filters, of the Issues that narrow none.
  const roots = view === "columns";
  const request = useMemo(() => {
    const compiled = compileQuery(query, meta.fieldOwners, me);
    return roots ? { ...compiled, filters: [...compiled.filters, { field: "narrows", op: "isnull", value: "" } as const] } : compiled;
  }, [query, meta.fieldOwners, me, roots]);
  const mixed = kinds.length > 1;
  const pageSize = mixed ? 20 : 50;
  const loaded = useListPages(request, pageSize, state.pages);
  const scroller = useRef<HTMLDivElement>(null);
  const { live, rows } = useLiveList(request, pageSize, scroller, loaded.rows);
  const offeredGroupings = groupings(kinds, meta.fieldOwners);
  const offeredOrderings = orderings(kinds, meta.fieldOwners);
  const fallback = defaultDisplay(kinds, meta.fieldOwners);
  const grouping = offeredGroupings.includes(state.grouping) ? state.grouping : fallback.grouping;
  const ordering = offeredOrderings.includes(state.ordering) ? state.ordering : fallback.ordering;
  // A tab or filter the user picks lays the rows out afresh, even when its
  // cached pages hold the same rows, and again once those pages have loaded:
  // only the user's actions load pages, and live updates read apart from them.
  const layout = `${view} ${grouping} ${ordering} ${JSON.stringify(request)} ${loaded.loading.length > 0}`;
  // Streams and Outline are groupings of their own, over the rows in their steady
  // order; Streams take them newest first.
  const groups = useSteadyLayout(
    groupRows(sortRows(rows, view === "streams" ? "created" : ordering), view === "list" ? grouping : "none", meta),
    layout,
  );
  const laidOut = view === "list" ? null : (groups[0]?.rows ?? []);
  const outline = useOutline(view === "outline" ? laidOut : null, state.collapsed);
  const streams = useStreams(view === "streams" ? laidOut : null, state.collapsed);
  const collapsed = new Set(state.collapsed);
  // Columns select by their path, and leave the list's focus and selection alone.
  const visible =
    view === "columns"
      ? []
      : ((outline ?? streams)?.rows ?? groups.flatMap((group) => (collapsed.has(groupKey(grouping, group)) ? [] : group.rows)));
  // What j and k move through: the rows, and an outline's parents outside the page.
  const order: readonly { readonly id: string; readonly kind: string; readonly seq: number }[] =
    (outline ?? streams)?.order ?? visible;
  const focused = order.find((line) => line.id === state.focus) ?? order[0] ?? null;
  const selection = useSelection(
    visible.map((row) => row.id),
    focused && visible.some((row) => row.id === focused.id) ? focused.id : null,
  );
  const [peeking, setPeeking] = useState(false);
  const peek = usePanel(PEEK);
  const now = useMinuteClock();

  const setQuery = (next: ListQuery) => {
    selection.clear();
    update((s) => ({ ...s, tab: next.tab, choices: next.choices, pages: {}, focus: null }));
  };
  const focus = useCallback((rowId: string) => update((s) => ({ ...s, focus: rowId })), [update]);
  // A row the keys move to, or the pointer rests on, may be opened next: its
  // detail is read ahead. The row a list opens on is no such sign.
  const warm = useDetailWarmer();
  const move = (step: number) => {
    const index = order.findIndex((line) => line.id === focused?.id);
    const next = order[Math.min(order.length - 1, Math.max(0, index + step))];
    if (!next) return;
    focus(next.id);
    warm(next.id);
  };
  const toggle = (key: string) => update((s) => ({ ...s, collapsed: toggled(s.collapsed, key) }));
  const pickView = (next: View) => {
    if (next === "columns") return navigate({ name: "list", kind: kinds[0]!, columns: [] });
    update((s) => ({ ...s, view: next }));
    if (path) navigate({ name: "list", kind: kinds[0]! });
  };
  useCommands(
    // Columns bind j, k and Enter to their own path; the list's, mounted after, would win.
    view === "columns"
      ? []
      : [
          ...listCommands({
            next: () => move(1),
            previous: () => move(-1),
            open: () => focused && navigate({ name: "ref", kind: focused.kind, seq: focused.seq }),
            close: () => (peeking ? setPeeking(false) : selection.clear()),
            // Space shows the focused row's detail: it closes Peek only when Peek shows it.
            peek: () => {
              const closing = peeking && !peek.collapsed;
              if (!closing) peek.setCollapsed(false);
              setPeeking(!closing && focused !== null);
            },
          }),
          ...(peeking && focused ? [panelCommand(peek)] : []),
        ],
  );
  useOutlineKeys(outline, focused?.id ?? null, toggle);
  // Keyed on the id, not the row: a refetch hands over new row objects, and
  // scrolling then would pull the list away from where the user scrolled to.
  const focusedId = focused?.id ?? null;
  useEffect(() => {
    if (!focusedId) return;
    // In the next frame, which lays the rows out anyway: React runs this effect
    // in the task that rendered them, and scrolling there forced their layout
    // inside it, 13 of its 42 ms with the CPU slowed 4x.
    const frame = requestAnimationFrame(() =>
      scroller.current?.querySelector(`[data-row="${focusedId}"]`)?.scrollIntoView({ block: "nearest" }),
    );
    return () => cancelAnimationFrame(frame);
  }, [focusedId]);
  // Opening a row is what a list leads to, and the detail's code is a chunk of
  // its own: a first open that waited for it took 0.8 s, since React shows a
  // suspended view's fallback for at least 300 ms. So it loads once the rows
  // show and the browser is idle. A failed load is reported when
  // the detail renders.
  const shown = rows.length > 0;
  useEffect(() => {
    if (shown) return whenIdle(() => void DetailView.preload().catch(() => {}));
  }, [shown]);

  return (
    <div className="view">
      <ViewHeader icon={icon} title={title} />
      <div className="view-tools" onMouseDown={keepFocus}>
        <Tabs tab={state.tab} onChange={(tab) => setQuery(withTab(query, tab))} />
        <div className="spacer" />
        <FilterMenu query={query} rows={rows} onToggle={(f, v) => setQuery(toggleChoice(query, f, v))} />
        {view === "list" && (
          <Menu
            label="Group loaded rows by…"
            trigger={
              <button type="button" className="btn ghost" title="Group loaded rows">
                <Icon name="layers" size={14} />
                <span className="hide-sm">Group: {GROUPING_NAMES[grouping]}</span>
              </button>
            }
            options={offeredGroupings.map((g) => ({ value: g, label: GROUPING_NAMES[g], checked: g === grouping }))}
            onPick={(value) =>
              update((s) => ({ ...s, grouping: offeredGroupings.find((g) => g === value) ?? grouping, collapsed: [] }))
            }
          />
        )}
        {(view === "list" || view === "outline") && (
          <Menu
            label="Sort loaded rows by…"
            trigger={
              <button type="button" className="btn ghost" title="Sort loaded rows">
                <Icon name="sliders" size={14} />
                <span className="hide-sm">Sort: {ORDERING_NAMES[ordering]}</span>
              </button>
            }
            options={offeredOrderings.map((o) => ({ value: o, label: ORDERING_NAMES[o], checked: o === ordering }))}
            onPick={(value) => update((s) => ({ ...s, ordering: offeredOrderings.find((o) => o === value) ?? ordering }))}
          />
        )}
        <ViewSwitch offered={views} view={view} onChange={pickView} />
      </div>
      <FilterChips
        query={query}
        onRemove={(field: ChoiceField) => setQuery(removeChoice(query, field))}
        onClear={() => setQuery({ ...query, choices: [] })}
      />
      <LeftOut query={query} requested={request.kinds} />
      {request.kinds.length > 0 && <TraxLine line={traxLine(request)} />}
      {loaded.error && rows.length > 0 ? (
        <Bar kind="stale">
          Could not load all of {title}: {loaded.error.message}
          <button type="button" className="btn ghost" onClick={loaded.retry}>
            Retry
          </button>
        </Bar>
      ) : null}
      <div
        className="scroll"
        ref={scroller}
        aria-busy={loaded.loading.length > 0}
        onMouseDown={keepFocus}
        onMouseOver={(event) => warm((event.target as Element).closest("[data-row]")?.getAttribute("data-row") ?? null)}
        onMouseLeave={() => warm(null)}
      >
        <LiveRows
          live={live}
          scroller={scroller}
          reveal={(joined) => {
            const keys = new Set(groupRows(joined, grouping, meta).map((group) => groupKey(grouping, group)));
            update((s) => ({ ...s, collapsed: s.collapsed.filter((key) => !keys.has(key)) }));
          }}
        />
        {loaded.error && rows.length === 0 ? <ReadFailure error={loaded.error} retry={loaded.retry} /> : null}
        {request.kinds.length === 0 ? (
          <EmptyState icon={<Icon name="filter" size={24} />} title="Nothing to ask for">
            <p>No kind in this list has every filtered field.</p>
          </EmptyState>
        ) : loaded.pending ? (
          <p className="list-loading">Loading…</p>
        ) : rows.length === 0 ? (
          !loaded.error && (
            <EmptyState icon={icon} title="Nothing here">
              <p>No {title.toLowerCase()} match this tab and these filters.</p>
            </EmptyState>
          )
        ) : (
          <>
            {path ? (
              <ColumnsView kind={kinds[0]!} roots={rows} path={path} />
            ) : outline ? (
              <OutlineRows
                outline={outline}
                focusedId={focusedId}
                now={now}
                onFocus={focus}
                onToggle={toggle}
                selection={selection}
              />
            ) : streams ? (
              <StreamsRows
                view={streams}
                focusedId={focusedId}
                now={now}
                onFocus={focus}
                onToggle={toggle}
                selection={selection}
              />
            ) : (
              groups.map((group) => (
                <GroupRows
                  key={groupKey(grouping, group)}
                  grouping={grouping}
                  group={group}
                  collapsed={collapsed.has(groupKey(grouping, group))}
                  more={grouping === "kind" ? loaded.more.includes(group.value ?? "") : loaded.more.length > 0}
                  onToggle={() => toggle(groupKey(grouping, group))}
                  mixed={mixed}
                  focusedId={focusedId}
                  now={now}
                  onFocus={focus}
                  selection={selection}
                />
              ))
            )}
            <ListFoot
              selectable={selection.enabled && view !== "columns"}
              loaded={rows.length}
              more={loaded.more}
              loading={loaded.loading}
              mixed={mixed}
              view={view}
              onMore={(kind) => update((s) => ({ ...s, pages: { ...s.pages, [kind]: (s.pages[kind] ?? 1) + 1 } }))}
            />
          </>
        )}
      </div>
      {selection.enabled && (
        <BulkBar rows={rows.filter((row) => selection.ids.has(row.id))} onClear={selection.clear} />
      )}
      {peeking && focused && <Peek row={focused} panel={peek} onClose={() => setPeeking(false)} />}
    </div>
  );
}

/** A list's state in this browser tab. The query part is `tab` and `choices`. */
type ListState = {
  readonly tab: Tab;
  readonly choices: readonly Choice[];
  readonly view: StoredView;
  readonly grouping: Grouping;
  readonly ordering: Ordering;
  /** Pages loaded per kind; absent is one. */
  readonly pages: { readonly [kind: string]: number };
  /**
   * Sections toggled from how they start: collapsed groups, by `groupKey`, and
   * folded outline lines and streams shown whole, by the keys those views hand over.
   */
  readonly collapsed: readonly string[];
  readonly focus: string | null;
};

function initialState(kinds: readonly string[], meta: Meta): ListState {
  return {
    // Papers are reference material whose status rarely changes, so the mock
    // opens them on All.
    tab: kinds.length === 1 && kinds[0] === "Paper" ? "all" : "active",
    choices: [],
    view: "list",
    ...defaultDisplay(kinds, meta.fieldOwners),
    pages: {},
    collapsed: [],
    focus: null,
  };
}

/**
 * A stored list state, or null when it is not one this build can show. A
 * grouping, ordering or view the list does not offer is left to fall back when
 * shown.
 */
function readListState(saved: unknown): ListState | null {
  if (typeof saved !== "object" || saved === null) return null;
  // A state stored before lists had views is a List's: the tab keeps it across a deploy.
  const { tab, choices, view = "list", grouping, ordering, pages, collapsed, focus } = saved as { [field: string]: unknown };
  const valid =
    isListTab(tab) &&
    isChoices(choices) &&
    isStoredView(view) &&
    typeof grouping === "string" &&
    typeof ordering === "string" &&
    isPageCounts(pages) &&
    isStrings(collapsed) &&
    (focus === null || typeof focus === "string");
  return valid
    ? { tab, choices, view, grouping: grouping as Grouping, ordering: ordering as Ordering, pages, collapsed, focus }
    : null;
}

function Tabs({ tab, onChange }: { tab: Tab; onChange: (tab: Tab) => void }) {
  return (
    <nav className="tabs" aria-label="Status">
      {TABS.map(([value, name]) => (
        <button
          key={value}
          type="button"
          className="tab"
          aria-current={tab === value ? "page" : undefined}
          onClick={() => onChange(value)}
        >
          {name}
        </button>
      ))}
    </nav>
  );
}

/** Says which kinds a filter left out of the request (S8); only lists of several kinds have any. */
function LeftOut({ query, requested }: { query: ListQuery; requested: readonly string[] }) {
  const only = query.choices.find((choice) => choice.field === "kind")?.values;
  const left = query.kinds.filter((kind) => (!only || only.includes(kind)) && !requested.includes(kind));
  if (left.length === 0) return null;
  return (
    <p className="list-note">
      Not shown: {left.map((kind) => kindLook(kind).plural).join(", ")}. They lack a filtered field.
    </p>
  );
}

/** The same query as a `trax` command, with a copy button. */
function TraxLine({ line }: { line: string }) {
  const copy = useCopy();
  return (
    <div className="qline" onMouseDown={keepFocus}>
      <code title="The same query from the CLI">{line}</code>
      <button
        type="button"
        className="icon-btn"
        onClick={() => void copy(line, "Copied the trax command")}
        title="Copy as a trax command"
        aria-label="Copy as a trax command"
      >
        <Icon name="copy" size={13} />
      </button>
    </div>
  );
}

function GroupRows({
  grouping,
  group,
  collapsed,
  more,
  onToggle,
  mixed,
  focusedId,
  now,
  onFocus,
  selection,
}: {
  grouping: Grouping;
  group: Group;
  collapsed: boolean;
  /** Load more may find more of its rows: the count is of those loaded so far. */
  more: boolean;
  onToggle: () => void;
  mixed: boolean;
  focusedId: string | null;
  now: number;
  onFocus: (id: string) => void;
  selection: Selection;
}) {
  return (
    <section aria-label={grouping === "none" ? undefined : groupName(grouping, group.value)}>
      {grouping !== "none" && (
        <button
          type="button"
          className={collapsed ? "group-h is-collapsed" : "group-h"}
          aria-expanded={!collapsed}
          onClick={onToggle}
        >
          <Icon name="chevD" size={14} className="chev" />
          {groupIcon(grouping, group.value)}
          <span>{groupName(grouping, group.value)}</span>
          <span className="count" title={more ? "Loaded so far: Load more may find more" : undefined}>
            {group.rows.length}
            {more && "+"}
          </span>
        </button>
      )}
      {!collapsed &&
        group.rows.map((row) => (
          <Row
            key={row.id}
            row={row}
            mixed={mixed}
            focused={row.id === focusedId}
            now={now}
            onFocus={onFocus}
            selected={selection.ids.has(row.id)}
            onSelect={selection.enabled ? selection.select : undefined}
          />
        ))}
    </section>
  );
}

function ListFoot({
  selectable,
  loaded,
  more,
  loading,
  mixed,
  view,
  onMore,
}: {
  selectable: boolean;
  loaded: number;
  more: readonly string[];
  loading: readonly string[];
  mixed: boolean;
  view: View;
  onMore: (kind: string) => void;
}) {
  return (
    <div className="list-foot">
      {more.map((kind) => (
        <button key={kind} type="button" className="btn" disabled={loading.includes(kind)} onClick={() => onMore(kind)}>
          {loading.includes(kind) ? "Loading…" : mixed ? `Load more ${kindLook(kind).plural.toLowerCase()}` : "Load more"}
        </button>
      ))}
      <span>{loaded} loaded</span>
      <span className="spacer" />
      <span className="hide-sm">
        <kbd>j</kbd>
        <kbd>k</kbd> move
      </span>
      {(view === "outline" || view === "columns") && (
        <span className="hide-sm">
          <kbd>←</kbd>
          <kbd>→</kbd> {view === "outline" ? "fold" : "columns"}
        </span>
      )}
      <span className="hide-sm">
        <kbd>↵</kbd> open
      </span>
      {view !== "columns" && (
        <span className="hide-sm">
          <kbd>Space</kbd> peek
        </span>
      )}
      {selectable && (
        <span className="hide-sm">
          <kbd>x</kbd> select
        </span>
      )}
      <span className="hide-sm">
        <kbd>{keyCaps("$mod+k").join(" ")}</kbd> commands
      </span>
    </div>
  );
}

/**
 * Keep the focus where it was when the mouse presses a button among a list's
 * tools, its trax line or its rows. A button the mouse focused would take Enter
 * and press itself again (a tab, Load more, a group's heading), where Enter
 * should open the focused row (README TODO 1). Tab still focuses each button for
 * the keyboard, and there Enter presses it.
 */
function keepFocus(event: MouseEvent): void {
  if ((event.target as Element).closest("button")) event.preventDefault();
}

function groupKey(grouping: Grouping, group: Group): string {
  return `${grouping}:${group.value ?? ""}`;
}

function groupName(grouping: Grouping, value: string | null): string {
  switch (grouping) {
    case "none":
      return "All";
    case "kind":
      return kindLook(value ?? "").plural;
    case "owner":
      return value ?? "No owner";
    case "priority":
      return value === null ? "No priority" : PRIORITY_NAMES[Number(value)]!;
    case "status":
    case "judgement":
      return value === null ? `No ${grouping}` : capitalize(value);
  }
}

function groupIcon(grouping: Grouping, value: string | null): ReactNode {
  if (grouping === "priority") return <PriorityGlyph priority={value === null ? null : Number(value) * 10} />;
  if (value === null) return null;
  if (grouping === "kind") return <KindIcon kind={value} size={14} />;
  if (grouping === "status") return <StatusGlyph status={value} />;
  if (grouping === "judgement") return <JudgementGlyph judgement={value} />;
  if (grouping === "owner") return <Avatar actor={value} size={16} />;
  return null;
}

function toggled(values: readonly string[], value: string): string[] {
  return values.includes(value) ? values.filter((v) => v !== value) : [...values, value];
}

const TABS: readonly [Tab, string][] = [
  ["active", "Active"],
  ["closed", "Closed"],
  ["all", "All"],
];

const GROUPING_NAMES: { readonly [grouping in Grouping]: string } = {
  none: "No grouping",
  kind: "Kind",
  status: "Status",
  owner: "Owner",
  priority: "Priority",
  judgement: "Judgement",
};

const ORDERING_NAMES: { readonly [ordering in Ordering]: string } = {
  priority: "Priority",
  confidence: "Confidence",
  modified: "Last updated",
  created: "Created",
  seq: "Number",
};
