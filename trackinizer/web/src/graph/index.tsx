import { useQuery } from "@tanstack/react-query";
import {
  type MouseEvent,
  type RefObject,
  useEffect,
  useEffectEvent,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { Graph, GraphNode } from "../api/graph";
import { type Meta, useMeta } from "../app/boot";
import { useHighlighted } from "../app/highlights";
import { SIDEBAR } from "../app/Sidebar";
import { isStrings, useTabState } from "../app/tabState";
import { useCommands } from "../commands/registry";
import "../lists/lists.css";
import { LiveFailureBar } from "../live";
import { type FocusRef, type Hops, parseHash, type Route } from "../router/route";
import { useRouter } from "../router/router";
import { ReadFailure } from "../ui/failure";
import { StatusGlyph } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { KindIcon } from "../ui/kinds";
import { panelCommand, usePanel } from "../ui/panel";
import { PEEK, Peek } from "../ui/Peek";
import { EmptyState, ViewHeader } from "../ui/view";
import { ASK_ABOVE, AskToDraw, FilterChips, FilterMenu, FocusRow, type Hidden, Key, LimitMenu, ReplayMenu, ZoomButtons } from "./controls";
import "./graph.css";
import { hopCounts, isWithin, reach } from "./hops";
import { graphQuery, useLiveGraph } from "./live";
import { type DrawNode, GraphModel, type Lens } from "./model";
import { useThemePalette } from "./palette";
import { type Covered, type CreateRenderer, forceGraphRenderer, type Renderer } from "./renderer";
import { replay } from "./replay";
import { findRoots } from "./roots";
import { RootsPanel } from "./RootsPanel";
import { hopsName, MAX_ROWS, type MatchGroup, RESULTS, SearchBox, type SearchKey, SearchResults, searchNodes } from "./search";

/** The graph route, as the hash holds it. */
type Place = Extract<Route, { name: "graph" }>;

/**
 * The graph, `#/graph` and the home view: the newest inquiries, each with the
 * older ones it links to, and the edges between them, drawn on a canvas by a
 * force layout, as the old UI's `/graph` was. Its tools are a list's: search,
 * Filter (kinds and statuses, shown as chips), the node limit (100, 1k, 5k,
 * All or any count; it asks before drawing over `ASK_ABOVE`), Replay, Group by
 * root and, while grouped, Roots list, and Key. A key and zoom buttons sit
 * over the canvas; every fit frames what they and Peek leave of it, and
 * showing or hiding the key or the roots list frames again. The limit, filters,
 * and whether the key and the roots list show are kept for the tab; the focus,
 * hops and grouping live in the hash. It opens grouped by root.
 *
 * Hovering a node shows what it is and lights its neighbourhood. A click, a
 * search pick or a Peek link to a drawn node selects it: Peek shows it and its
 * light holds. A double-click focuses on it: the focus row lights what lies
 * within 1, 2 or 3 hops or the whole connected part, over the edge kinds
 * Through names, and dims or hides the rest; opened on a focus, the view also
 * selects it, so Peek opens beside what it lights. Search lights its matches and
 * lists them, within the focus's hops and elsewhere. Grouped, each root's
 * subgraph gathers into an island, and the roots list frames one. Esc clears
 * the selection, then the focus. Peek and the search results collapse to a
 * strip at their side, kept for the tab; a collapsed Peek stays so as the
 * selection moves. Keys: `f` fits, `/` searches, `.` focuses on the selection,
 * `]` collapses or expands Peek, and `[` the search results or, while not
 * searching, shows or hides the roots list.
 *
 * `createRenderer` draws it; tests replace it, as jsdom has no canvas.
 */
export function GraphView({ createRenderer = forceGraphRenderer }: { createRenderer?: CreateRenderer }) {
  const meta = useMeta();
  const { route, navigate } = useRouter();
  // The create form opens over the graph under a route of its own; the graph keeps its place meanwhile.
  const [place, setPlace] = useState<Place>(route.name === "graph" ? route : { name: "graph" });
  if (route.name === "graph" && route !== place) setPlace(route);
  const go = (next: Partial<Omit<Place, "name">>, replace = false) =>
    navigate({ name: "graph", focus: place.focus, grouped: place.grouped, ...next }, { replace });
  const [view, setView] = useTabState(STATE_KEY, readViewState, initialState);
  const peek = usePanel(PEEK);
  const results = usePanel(RESULTS);
  const sidebar = usePanel(SIDEBAR);
  const graph = useQuery(graphQuery(view.limit));
  const failure = useLiveGraph(view.limit);
  const data = graph.data;
  const nodes = data?.nodes ?? NO_NODES;
  /** The limit whose graph over `ASK_ABOVE` nodes the user agreed to draw. */
  const [consent, setConsent] = useState<number | null>(null);
  const asking = nodes.length > ASK_ABOVE && consent !== view.limit;
  const palette = useThemePalette(meta);
  const [model] = useState(() => new GraphModel(palette));
  const host = useRef<HTMLDivElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const searchBox = useRef<HTMLInputElement>(null);
  const [renderer, setRenderer] = useState<Renderer | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  /** A node picked by search or a Peek link, to centre on once Peek shows it. */
  const [centring, setCentring] = useState<string | null>(null);
  /** The nodes of a root's island, to frame once Peek shows the root. */
  const [framing, setFraming] = useState<ReadonlySet<string> | null>(null);
  const [hovered, setHovered] = useState<{ node: DrawNode; x: number; y: number } | null>(null);
  const [query, setQuery] = useState("");
  const [marked, setMarked] = useState(-1);
  /** The roots list is to take the keyboard once it shows. */
  const [rootsWanted, setRootsWanted] = useState(false);
  const byId = useMemo(() => new Map(nodes.map((row) => [row.id, row])), [nodes]);
  const selected = selectedId === null ? undefined : byId.get(selectedId);
  const replayer = useReplay(model, renderer, () => setSelectedId(null));
  /** The limit whose graph was last drawn anew: a new limit's graph is framed once it settles. */
  const drawn = useRef<number | null>(null);

  const shownBy = useShown(view);
  const focus = place.focus ?? null;
  const focusNode = focus ? findFocus(nodes, focus.ref) : undefined;
  // Opened on a focus (Show in graph, a detail's preview, a link), the view
  // selects it once the graph has come: Peek opens on it in the same commit, so
  // the focus's frame below leaves Peek out. A focus moved here selects nothing.
  const [arriving, setArriving] = useState(focus !== null);
  if (arriving && data) {
    setArriving(false);
    if (focusNode) setSelectedId(focusNode.id);
  }
  const reached = useMemo(
    () => (data && focusNode ? reach(data, focusNode.id, { skipEdges: shownBy.skipEdges, keep: shownBy.keep }) : null),
    [data, focusNode, shownBy],
  );
  // A focus holds the light; without one, a selection does, or a hover.
  const lightId = focusNode ? null : (selected?.id ?? hovered?.node.id ?? null);
  const lit = useMemo(() => {
    if (focus && reached) return new Map([...reached].filter(([, far]) => isWithin(far, focus.hops)));
    if (!data || lightId === null) return null;
    return new Map([...reach(data, lightId, { skipEdges: NO_STRINGS, keep: shownBy.keep })].filter(([, far]) => far <= 1));
  }, [focus, reached, data, lightId, shownBy]);
  const only = focusNode !== undefined && view.only;
  const drawnNodes = useMemo(
    () => nodes.filter((row) => shownBy.keep(row) && !(only && !lit?.has(row.id))),
    [nodes, shownBy, only, lit],
  );
  const matches = useMemo(() => searchNodes(drawnNodes, query), [drawnNodes, query]);
  const highlighted = useHighlighted();
  const groups = matchGroups(matches, focusNode && focus && lit ? { node: focusNode, hops: focus.hops, lit } : null);
  const rows = groups.flatMap((group) => group.rows.slice(0, MAX_ROWS));
  const markedAt = Math.min(marked, rows.length - 1);
  const resultsId = useId();
  const keyId = useId();
  const rootsId = useId();
  const searching = query.trim() !== "";
  const grouped = place.grouped ?? true;
  const roots = useMemo(() => (grouped && data ? findRoots(data) : null), [grouped, data]);
  const lens = useMemo<Lens>(
    () => ({
      hiddenKinds: shownBy.kinds,
      hiddenStatuses: shownBy.statuses,
      lit,
      skipEdges: focusNode ? shownBy.skipEdges : NO_STRINGS,
      only,
      strong: new Set([focusNode?.id, selected?.id].filter((id) => id !== undefined)),
      highlighted,
      matches: new Set(matches.map((row) => row.id)),
      labelled: new Set(roots?.groups.flatMap((group) => (group.root ? [group.key] : [])) ?? []),
    }),
    [shownBy, lit, focusNode, only, selected, highlighted, matches, roots],
  );

  const focusOn = (row: GraphNode) => go({ focus: { ref: { kind: row.kind, seq: row.seq }, hops: focus?.hops ?? 1 } });
  const onDoubleClick = useEffectEvent((node: DrawNode) => focusOn(node));
  useEffect(() => {
    const canvas = host.current!;
    const made = createRenderer(
      canvas,
      {
        hover: (node) => setHovered(node ? { node, ...made.screenOf(node) } : null),
        click: (node) => setSelectedId(node?.id ?? null),
        doubleClick: (node) => onDoubleClick(node),
      },
      () => coveredOf(canvas),
    );
    setRenderer(made);
    return () => made.dispose();
  }, [createRenderer]);
  // A graph drawn anew (a new limit's, or one drawn on consent) is framed once
  // laid out: what a focus lights, or every node. Framed earlier, with nothing
  // drawn, a focus would be lost.
  const frameNew = useEffectEvent(() => (focusNode && lit ? (node: DrawNode) => lit.has(node.id) : EVERY_NODE));
  useEffect(() => {
    if (!renderer || !data || replayer.running || asking) return;
    if (model.apply(data)) {
      renderer.setData(model.nodes, model.links, drawn.current === view.limit ? null : frameNew());
      drawn.current = view.limit;
    } else {
      renderer.repaint();
    }
  }, [renderer, model, data, view.limit, replayer.running, asking]);
  useEffect(() => {
    model.show(palette, lens);
    renderer?.repaint();
  }, [model, renderer, palette, lens]);
  // A new focus, or new hops or edges to walk, frames what it lights; a re-read of the same graph leaves the view be.
  const frameFocus = useEffectEvent(() => {
    if (lit) renderer?.fit((node) => lit.has(node.id));
  });
  const focusKey = focus && focusNode ? `${focusNode.id} ${focus.hops} ${view.skipEdges.join(" ")}` : null;
  useEffect(() => {
    if (focusKey !== null) frameFocus();
  }, [focusKey, renderer]);
  // Showing or hiding the key, or collapsing Peek, changes what covers the
  // canvas, and the roots list, the search results or the app's sidebar how
  // wide it is: frame again once it has rendered.
  const panels = `${view.key} ${view.roots} ${peek.collapsed} ${results.collapsed} ${sidebar.collapsed}`;
  const panelsShown = useRef(panels);
  useLayoutEffect(() => {
    if (panelsShown.current === panels) return;
    panelsShown.current = panels;
    renderer?.refit();
  }, [panels, renderer]);
  // force-graph reads each node's group only as the force takes its nodes, so a new answer registers it again.
  useEffect(() => renderer?.group(roots ? (node) => roots.home.get(node.id) : null), [renderer, roots]);
  // A selected node the graph no longer has stays unselected should it come back (R3-03).
  useEffect(() => {
    if (selectedId !== null && data && !selected) setSelectedId(null);
  }, [selectedId, selected, data]);
  useEffect(() => {
    const list = body.current?.querySelector<HTMLElement>(".roots [role=listbox]");
    if (!rootsWanted || !list) return;
    list.focus();
    setRootsWanted(false);
  }, [rootsWanted, roots, searching]);
  useDebugHook(model, renderer, host, selected?.id ?? null, focusNode?.id ?? null);

  const select = (row: GraphNode) => {
    setSelectedId(row.id);
    setCentring(row.id);
  };
  // After Peek has rendered, so the node or island lands in what Peek leaves of the canvas.
  useLayoutEffect(() => {
    if (centring === null) return;
    const node = model.find(centring);
    if (node) renderer?.centre(node);
    setCentring(null);
  }, [centring, model, renderer]);
  useLayoutEffect(() => {
    if (framing === null) return;
    renderer?.fit((node) => framing.has(node.id));
    setFraming(null);
  }, [framing, renderer]);
  /**
   * A root picked in the roots list: Peek opens on it, and its island is framed
   * beside Peek. The island is the nodes laid out by it (`home`), not all under
   * it: a node under several roots sits by the nearest, in another island.
   */
  const frameRoot = (key: string) => {
    const group = roots?.groups.find((found) => found.key === key);
    if (!group || !roots) return;
    setSelectedId(group.root?.id ?? null);
    setFraming(new Set([...roots.home].flatMap(([id, home]) => (home === key ? [id] : []))));
  };
  const setLimit = (limit: number) => {
    // A Replay holds answers back; a new limit's answer draws at once.
    replayer.stop();
    setConsent(null);
    setView((state) => ({ ...state, limit }));
  };
  const toggleRoots = () => setView((state) => ({ ...state, roots: !state.roots }));
  const onSearchKey = (key: SearchKey) => {
    if (key === "next" || key === "previous") {
      const step = key === "next" ? 1 : -1;
      setMarked(Math.min(Math.max(markedAt + step, 0), rows.length - 1));
    } else if (key === "pick") {
      const row = rows[Math.max(0, markedAt)];
      if (row) select(row);
    } else if (key === "everywhere") {
      navigate({ name: "search", q: query.trim() });
    } else {
      setQuery("");
      setMarked(-1);
      setSelectedId(null);
    }
  };
  useCommands([
    { id: "graph.fit", title: "Fit the graph", keys: ["f"], section: "Graph", run: () => renderer?.fit() },
    { id: "graph.search", title: "Search the graph", keys: ["/"], section: "Graph", run: () => searchBox.current?.focus() },
    // The search results take the roots list's place, and its key.
    ...(searching
      ? [panelCommand(results)]
      : grouped
        ? [{ id: "graph.roots", title: "Show or hide the roots list", keys: ["["], section: "Graph", run: toggleRoots }]
        : []),
    ...(selected
      ? [
          panelCommand(peek),
          { id: "graph.clear", title: "Clear the selection", keys: ["Escape"], run: () => setSelectedId(null) },
          { id: "graph.focus", title: "Focus the graph on the selection", keys: ["."], section: "Graph", run: () => focusOn(selected) },
        ]
      : focus
        ? [{ id: "graph.unfocus", title: "Clear the graph's focus", keys: ["Escape"], section: "Graph", run: () => go({ focus: undefined }) }]
        : []),
  ]);
  const followLink = usePeekLinks(meta, drawnNodes, selected, select);

  const hidden: Hidden = { kinds: view.hiddenKinds, statuses: view.hiddenStatuses };
  const litEdges = data && lit ? data.edges.filter((row) => lit.has(row.from_id) && lit.has(row.to_id) && !lens.skipEdges.has(row.edge_kind)) : [];
  const drawnIds = new Set(drawnNodes.map((row) => row.id));
  const drawnEdges = (data?.edges ?? []).filter((row) => drawnIds.has(row.from_id) && drawnIds.has(row.to_id));
  const keyShown = view.key && !asking && nodes.length > 0;
  const present = {
    kinds: meta.kinds.filter((kind) => nodes.some((node) => node.kind === kind)),
    statuses: (meta.enums.status ?? []).filter((status) => nodes.some((node) => node.status === status)),
  };
  /** The matches list, which the search box controls, shows. */
  const listed = searching && !results.collapsed;
  const rootsShown = roots !== null && view.roots && !searching;
  return (
    <div className="view graph">
      <ViewHeader icon={<Icon name="graph" />} title="Graph" />
      <div className="view-tools">
        <SearchBox
          query={query}
          onQuery={(text) => {
            setQuery(text);
            setMarked(-1);
          }}
          onKey={onSearchKey}
          inputRef={searchBox}
          listId={listed ? resultsId : null}
          activeId={listed && markedAt >= 0 ? `${resultsId}-${markedAt}` : undefined}
        />
        <span className="graph-count muted num">{countText(data !== undefined, nodes.length, drawnNodes.length, searching ? matches.length : null)}</span>
        <div className="spacer" />
        <button
          type="button"
          className="btn ghost"
          aria-pressed={grouped}
          aria-label="Group by root"
          title="Gather each root's subgraph into an island, and list the roots"
          onClick={() => {
            setRootsWanted(!grouped && view.roots);
            go({ grouped: grouped ? false : undefined }, true);
          }}
        >
          <Icon name="orbit" size={14} />
          <span className="hide-sm">Group by root</span>
        </button>
        {grouped ? (
          <button
            type="button"
            className="btn ghost"
            aria-pressed={view.roots}
            aria-controls={rootsShown ? rootsId : undefined}
            aria-label="Roots list"
            title="Show or hide the list of roots ([)"
            onClick={toggleRoots}
          >
            <Icon name="list" size={14} />
            <span className="hide-sm">Roots list</span>
          </button>
        ) : null}
        <button
          type="button"
          className="btn ghost"
          aria-pressed={view.key}
          aria-controls={keyShown ? keyId : undefined}
          title="Show or hide the key to the canvas"
          onClick={() => setView((state) => ({ ...state, key: !state.key }))}
        >
          <Icon name="tag" size={14} />
          Key
        </button>
        <FilterMenu meta={meta} nodes={nodes} hidden={hidden} onToggle={(field, value) => setView((state) => toggleHidden(state, field, value))} />
        <LimitMenu limit={view.limit} onLimit={setLimit} />
        <ReplayMenu
          running={replayer.running}
          speed={replayer.speed}
          onSpeed={(speed) => {
            replayer.setSpeed(speed);
            // Over `ASK_ABOVE` nodes, Replay draws nothing until the question is answered.
            if (!replayer.running && data && !asking) replayer.start(data);
          }}
          onStop={replayer.stop}
        />
      </div>
      <FilterChips
        hidden={hidden}
        present={present}
        onRemove={(field) => setView((state) => ({ ...state, ...(field === "kinds" ? { hiddenKinds: [] } : { hiddenStatuses: [] }) }))}
        onClear={() => setView((state) => ({ ...state, hiddenKinds: [], hiddenStatuses: [] }))}
      />
      {focus ? (
        <FocusRow
          name={"id" in focus.ref ? focus.ref.id : `${focus.ref.kind}#${focus.ref.seq}`}
          node={focusNode ?? null}
          hops={focus.hops}
          counts={reached ? hopCounts(reached) : NO_COUNTS}
          lit={{ nodes: lit?.size ?? 0, edges: litEdges.length }}
          only={view.only}
          skipEdges={view.skipEdges}
          meta={meta}
          onHops={(hops) => go({ focus: { ...focus, hops } }, true)}
          onOnly={(next) => setView((state) => ({ ...state, only: next }))}
          onSkip={(edgeKind) => setView((state) => ({ ...state, skipEdges: toggled(state.skipEdges, edgeKind) }))}
          onClear={() => go({ focus: undefined })}
        />
      ) : null}
      <LiveFailureBar failure={failure} />
      <div className="graph-body" ref={body}>
        {searching ? (
          <SearchResults
            id={resultsId}
            query={query}
            total={matches.length}
            groups={groups}
            marked={markedAt}
            hopsOf={(row) => (focus ? reached?.get(row.id) : undefined)}
            onPick={select}
            onFocus={focusOn}
            panel={results}
          />
        ) : roots && view.roots ? (
          // Beside an open Peek, a strip, so the graph keeps at least half the width at 1,280 px.
          <RootsPanel id={rootsId} roots={roots} onSelect={frameRoot} compact={selected !== undefined && !peek.collapsed} />
        ) : null}
        <div className="graph-stage">
          <div className="graph-canvas" ref={host} aria-busy={!data} />
          {!data && graph.isError ? <ReadFailure error={graph.error} retry={() => void graph.refetch()} /> : null}
          {data && nodes.length === 0 ? (
            <div className="graph-ask">
              <EmptyState icon={<Icon name="graph" size={24} />} title="No inquiries yet">
                <p>Inquiries, and the edges between them, show here as they are made.</p>
              </EmptyState>
            </div>
          ) : null}
          {asking ? <AskToDraw count={nodes.length} onDraw={() => setConsent(view.limit)} onFewer={() => setLimit(ASK_ABOVE)} /> : null}
          {keyShown ? (
            <Key
              id={keyId}
              meta={meta}
              palette={palette}
              kinds={present.kinds}
              nodes={drawnNodes}
              edges={drawnEdges.map((row) => ({ kind: row.edge_kind, valence: row.valence }))}
              hiddenKinds={view.hiddenKinds}
              onHiddenKinds={(hiddenKinds) => setView((state) => ({ ...state, hiddenKinds }))}
            />
          ) : null}
          <ZoomButtons onZoom={(factor) => renderer?.zoomBy(factor)} onFit={() => renderer?.fit()} />
          {hovered ? <Tooltip {...hovered} far={focus ? reached?.get(hovered.node.id) : undefined} /> : null}
          {selected ? (
            // Its own box, so the capture sees Peek's links before they navigate; Peek places itself.
            <div className="graph-peek" onClickCapture={followLink}>
              <Peek row={selected} panel={peek} onClose={() => setSelectedId(null)} />
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

/** What the view shows of the graph by its kept state: what Filter hides, and the edge kinds a focus does not walk. */
function useShown(view: ViewState) {
  return useMemo(() => {
    const kinds = new Set(view.hiddenKinds);
    const statuses = new Set(view.hiddenStatuses);
    return {
      kinds,
      statuses,
      skipEdges: new Set(view.skipEdges),
      keep: (row: GraphNode) => !kinds.has(row.kind) && !statuses.has(row.status),
    };
  }, [view.hiddenKinds, view.hiddenStatuses, view.skipEdges]);
}

/**
 * What covers the canvas `host` now: a strip on its right, as far in as Peek,
 * the key or the zoom buttons reach. The key is a column on the right, above
 * the zoom buttons, since most windows give a canvas wider than tall, which
 * frames a graph by its height: a strip on its right costs that frame less than
 * a band across its foot.
 */
function coveredOf(host: HTMLElement): Covered {
  const canvas = host.getBoundingClientRect();
  const box = (selector: string) => host.parentElement!.querySelector(selector)?.getBoundingClientRect();
  return { right: Math.max(0, ...[box(".graph-peek > :not([hidden])"), box(".graph-key"), box(".graph-zoom")].map((at) => (at ? canvas.right - at.left : 0))) };
}

/** The node a focus names, if the graph has it. */
function findFocus(nodes: readonly GraphNode[], ref: FocusRef): GraphNode | undefined {
  return nodes.find((row) => ("id" in ref ? row.id === ref.id : row.kind === ref.kind && row.seq === ref.seq));
}

/** The matches as the results list them: within a focus's reach, then elsewhere; without a focus, all together. */
function matchGroups(
  matches: readonly GraphNode[],
  focus: { node: GraphNode; hops: Hops; lit: ReadonlyMap<string, number> } | null,
): MatchGroup[] {
  if (!focus) return [{ name: null, rows: matches }];
  const ref = `${focus.node.kind}#${focus.node.seq}`;
  const near = focus.hops === "all" ? `Connected to ${ref}` : `Within ${hopsName(focus.hops)} of ${ref}`;
  return [
    { name: near, rows: matches.filter((row) => focus.lit.has(row.id)) },
    { name: "Elsewhere in the graph", rows: matches.filter((row) => !focus.lit.has(row.id)) },
  ].filter((group) => group.rows.length > 0);
}

/** `1,039 nodes`, `915 of 1,039 nodes` while some are hidden, or `47 of 915 nodes match` while searching. */
function countText(loaded: boolean, total: number, drawn: number, matched: number | null): string {
  if (!loaded) return "Loading…";
  if (matched !== null) return `${matched.toLocaleString("en-US")} of ${plural(drawn, "node")} match`;
  return drawn === total ? plural(total, "node") : `${drawn.toLocaleString("en-US")} of ${plural(total, "node")}`;
}

/**
 * A click on a Peek link to a node the graph draws (`drawn`) selects it here
 * instead (v1's jump); any other link navigates, one to a node loaded but
 * hidden by a filter or Only these too.
 */
function usePeekLinks(meta: Meta, drawn: readonly GraphNode[], selected: GraphNode | undefined, select: (row: GraphNode) => void) {
  return (event: MouseEvent<HTMLElement>) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const href = event.target instanceof Element ? event.target.closest("a")?.getAttribute("href") : null;
    if (!href?.startsWith("#/")) return;
    const route = parseHash(href, meta.kinds);
    const target =
      route.name === "lookup"
        ? drawn.find((row) => row.id === route.id)
        : route.name === "ref"
          ? drawn.find((row) => row.kind === route.kind && row.seq === route.seq)
          : undefined;
    if (!target || target.id === selected?.id) return;
    event.preventDefault();
    select(target);
  };
}

/**
 * Replay, restartable, at a speed that may change while it runs. While it
 * runs the graph shows what it has grown, and answers wait; `onStart` clears
 * what the view had picked.
 */
function useReplay(model: GraphModel, renderer: Renderer | null, onStart: () => void) {
  const [running, setRunning] = useState(false);
  const [speed, setSpeedState] = useState(1);
  const speedNow = useRef(1);
  const stop = useRef(() => {});
  useEffect(() => () => stop.current(), []);
  return {
    running,
    speed,
    setSpeed: (next: number) => {
      speedNow.current = next;
      setSpeedState(next);
    },
    stop: () => {
      stop.current();
      setRunning(false);
    },
    start: (graph: Graph) => {
      if (!renderer) return;
      stop.current();
      onStart();
      setRunning(true);
      stop.current = replay(graph, {
        speed: () => speedNow.current,
        step: (grown) => {
          if (model.apply(grown)) renderer.setData(model.nodes, model.links, grown.nodes.length === graph.nodes.length ? EVERY_NODE : null);
          else renderer.repaint();
        },
        done: () => setRunning(false),
      });
    },
  };
}

/**
 * What a hovered node is, by it, as text (COLD-12): its kind and status as the
 * app draws them, its ref and, under a focus, its hops from it; its title; and
 * what a click and a double-click do.
 */
function Tooltip({ node, x, y, far }: { node: DrawNode; x: number; y: number; far: number | undefined }) {
  return (
    <div className="graph-tip" role="tooltip" style={{ left: x + TIP_OFFSET_PX, top: y + TIP_OFFSET_PX }}>
      <div className="graph-tip-ref">
        <KindIcon kind={node.kind} size={12} />
        <StatusGlyph status={node.status} size={12} />
        {node.kind}#{node.seq}
        {far ? ` · ${hopsName(far)}` : null}
      </div>
      <div>{node.title || "(untitled)"}</div>
      <div className="graph-tip-hint">Click opens Peek · double-click focuses here</div>
    </div>
  );
}

/**
 * `trackinizer.graph()` in the console, while the view is open: what is drawn,
 * each node with where it is in the window, what is selected and what is
 * focused, for checks that cannot read a canvas.
 */
function useDebugHook(
  model: GraphModel,
  renderer: Renderer | null,
  host: RefObject<HTMLElement | null>,
  selected: string | null,
  focus: string | null,
): void {
  useEffect(() => {
    const debug: unknown = Reflect.get(window, "trackinizer");
    if (typeof debug !== "object" || debug === null) return;
    Reflect.set(debug, "graph", () => {
      const box = host.current?.getBoundingClientRect();
      const screenOf = (node: DrawNode) => {
        const at = renderer?.screenOf(node);
        return at && box ? { x: box.left + at.x, y: box.top + at.y } : null;
      };
      return {
        nodes: model.nodes.map((node) => {
          const { id, kind, seq, title, status, hidden } = node;
          return { id, kind, seq, title, status, hidden, screen: screenOf(node) };
        }),
        links: model.links.map(({ from, to, kind, valence, hidden }) => ({ from, to, kind, valence, hidden })),
        selected,
        focus,
      };
    });
    return () => void Reflect.deleteProperty(debug, "graph");
  }, [model, renderer, host, selected, focus]);
}

function plural(count: number, noun: string): string {
  return `${count.toLocaleString("en-US")} ${noun}${count === 1 ? "" : "s"}`;
}

/** `values` with `value` added, or taken out if it was there. */
function toggled(values: readonly string[], value: string): string[] {
  return values.includes(value) ? values.filter((other) => other !== value) : [...values, value];
}

function toggleHidden(state: ViewState, field: keyof Hidden, value: string): ViewState {
  return field === "kinds"
    ? { ...state, hiddenKinds: toggled(state.hiddenKinds, value) }
    : { ...state, hiddenStatuses: toggled(state.hiddenStatuses, value) };
}

/** What the view keeps for the tab. */
type ViewState = {
  readonly limit: number;
  readonly hiddenKinds: readonly string[];
  readonly hiddenStatuses: readonly string[];
  /** Edge kinds a focus does not walk through. */
  readonly skipEdges: readonly string[];
  /** Only these: a focus hides the rest rather than dim it. */
  readonly only: boolean;
  /** The key shows. */
  readonly key: boolean;
  /** The roots list shows while grouped. */
  readonly roots: boolean;
};

/**
 * The state a tab starts with: the newest 1,000 inquiries, nothing hidden.
 * src/app/prefetch.ts starts this limit's read for a tab with no state kept.
 */
function initialState(): ViewState {
  return { limit: 1000, hiddenKinds: [], hiddenStatuses: [], skipEdges: [], only: false, key: true, roots: true };
}

/** The view's state kept for the tab, or null for anything this build cannot use. */
function readViewState(saved: unknown): ViewState | null {
  if (typeof saved !== "object" || saved === null) return null;
  const { limit, hiddenKinds, hiddenStatuses, skipEdges, only, key, roots } = saved as { [field: string]: unknown };
  return Number.isSafeInteger(limit) &&
    (limit as number) >= 1 &&
    isStrings(hiddenKinds) &&
    isStrings(hiddenStatuses) &&
    isStrings(skipEdges) &&
    typeof only === "boolean" &&
    typeof key === "boolean" &&
    typeof roots === "boolean"
    ? { limit: limit as number, hiddenKinds, hiddenStatuses, skipEdges, only, key, roots }
    : null;
}

const STATE_KEY = "trackinizer.v2.graph";
const TIP_OFFSET_PX = 12;
const NO_NODES: readonly GraphNode[] = [];
const NO_STRINGS: ReadonlySet<string> = new Set();
const NO_COUNTS = { 1: 0, 2: 0, 3: 0, all: 0 } as const;
const EVERY_NODE = () => true;
