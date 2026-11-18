import { useQuery } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import {
  type MouseEvent,
  type RefObject,
  Suspense,
  useEffect,
  useEffectEvent,
  useId,
  useLayoutEffect,
  lazy,
  useMemo,
  useRef,
  useState,
} from "react";
import type { Graph, GraphNode } from "../api/graph";
import { type Meta, useMeta } from "../app/boot";
import { useHighlighted } from "../app/highlights";
import { isStrings, useTabState } from "../app/tabState";
import { useCommands } from "../commands/registry";
import "../lists/lists.css";
import { LiveFailureBar } from "../live";
import { type FocusRef, formatRoute, type Hops, parseHash, type Route } from "../router/route";
import { useRouter } from "../router/router";
import { ReadFailure } from "../ui/failure";
import { Icon } from "../ui/icons";
import { panelCommand, usePanel } from "../ui/panel";
import { PEEK, Peek } from "../ui/Peek";
import { EmptyState } from "../ui/view";
import { ASK_ABOVE, AskToDraw, FilterChips, FocusRow, type Hidden, Key, LimitMenu, Tooltip } from "./controls";
import "./graph.css";
import { hopCounts, isWithin, reach } from "./hops";
import { graphQuery, useLiveGraph } from "./live";
import { type DrawNode, GraphModel, type Lens, wires } from "./model";
import { useThemePalette } from "./palette";
import { BRIEFING, type SignalKey, Briefing, BriefingShow, Timeline, binsOf } from "./protoChrome";
import { type Covered, type CreateRenderer, type Renderer } from "./renderer";
import { canvasRenderer } from "./canvasRenderer";
import { replay } from "./replay";
import { islandHomes } from "./islands";
import { findRoots } from "./roots";
import { hopsName, MAX_ROWS, type MatchGroup, RESULTS, SearchBox, type SearchKey, SearchResults, searchNodes } from "./search";
import { ThreadBriefing } from "./ThreadBriefing";
import { useWorkspaceActions } from "../visuals/workspaceActions";

/** The graph route, as the hash holds it. */
type Place = Extract<Route, { name: "graph" }>;

/**
 * The record a selection opens. Its own chunk: it reads `/api/web/get` and the
 * field tables with it, none of which the canvas's first paint needs.
 */
const GraphRecord = lazy(() => import("./GraphRecord").then((made) => ({ default: made.GraphRecord })));

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
export function GraphView({ createRenderer = canvasRenderer }: { createRenderer?: CreateRenderer }) {
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
  const briefing = usePanel(BRIEFING);
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
  /** The record a Peek row is held on: the canvas rings and names it while it is. */
  const [peer, setPeer] = useState<GraphNode | null>(null);
  /** A flag from the briefing's `needs attention`: the beliefs it counts, lit and framed. */
  const [signal, setSignal] = useState<{ key: SignalKey; ids: readonly string[] } | null>(null);
  const [query, setQuery] = useState("");
  const [marked, setMarked] = useState(-1);
  const byId = useMemo(() => new Map(nodes.map((row) => [row.id, row])), [nodes]);
  const selected = selectedId === null ? undefined : byId.get(selectedId);
  const replayer = useReplay(() => setSelectedId(null));
  /** The timeline's bars and the date the view stands at, from the graph's own dates. */
  const timelineBins = useMemo(() => (data ? binsOf(data) : NO_BINS), [data]);
  const byAge = useMemo(() => (data ? [...data.nodes].sort((a, b) => Date.parse(a.created) - Date.parse(b.created)) : NO_NODES), [data]);
  const replayDate = useMemo(() => {
    if (byAge.length === 0) return "";
    const at = byAge[Math.min(byAge.length - 1, Math.round(replayer.position * (byAge.length - 1)))];
    return new Date(at.created).toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }, [byAge, replayer.position]);
  /**
   * The records the timeline's place draws: the oldest `position` of the graph
   * by `created`, the newest `ARRIVING` of them still coming up while it plays.
   * The graph itself is never re-sent — a replay shows more of it, which is what
   * keeps the islands where they settled, forming rather than re-laying out —
   * and one rule serves playing and scrubbing both, as the design's `cut` did.
   * A scrub's place holds still, so it draws every record behind it outright:
   * the ramp is for the newest few arriving under a moving place, not a wash
   * over the last of a field whose records are all long made.
   */
  const shown = useMemo(() => {
    if (byAge.length === 0 || replayer.position >= 1) return null;
    const cut = Math.max(1, Math.min(byAge.length, Math.round(replayer.position * byAge.length)));
    const map = new Map<string, number>();
    for (let at = 0; at < cut; at++) {
      const age = cut - at;
      map.set(byAge[at]!.id, replayer.running && age < ARRIVING ? age / ARRIVING : 1);
    }
    return map;
  }, [byAge, replayer.position, replayer.running]);
  /**
   * Draw the graph as it stood at `fraction` of its life, as the timeline's
   * scrub asks: the same showing a replay does, stopped where the pointer is.
   */
  const scrubTo = useEffectEvent((fraction: number) => {
    if (!data) return;
    replayer.stop();
    replayer.at(fraction);
  });
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
  /**
   * Opened on a focus the loaded graph does not hold, reach once for the next size up.
   *
   * A focus lights within the newest `limit` records, and on a store of thousands the
   * record just read about is rarely among the newest thousand — so the link landed on
   * "not among the nodes loaded" while the row was one size setting away. One step, once
   * per focus, so a record that really is absent settles on the honest answer.
   */
  const widened = useRef<string | null>(null);
  useEffect(() => {
    if (!data || focus === null || focusNode) return;
    const asked = refKey(focus.ref);
    const wider = LIMIT_STEPS.find((size) => size > view.limit && size <= ASK_ABOVE);
    if (wider === undefined || widened.current === asked) return;
    widened.current = asked;
    setView((state) => ({ ...state, limit: wider }));
  }, [data, focus, focusNode, view.limit]);
  const reached = useMemo(
    () => (data && focusNode ? reach(data, focusNode.id, { skipEdges: shownBy.skipEdges, keep: shownBy.keep }) : null),
    [data, focusNode, shownBy],
  );
  // A focus holds the light; without one, a selection does, or a hover.
  const lightId = focusNode ? null : (selected?.id ?? hovered?.node.id ?? null);
  const lit = useMemo(() => {
    if (focus && reached) return new Map([...reached].filter(([, far]) => isWithin(far, focus.hops)));
    // A flag holds the light over a selection: the reader asked which records, and
    // the answer is the whole set, not one of them.
    if (signal) return new Map(signal.ids.map((id) => [id, 0]));
    if (!data || lightId === null) return null;
    return new Map([...reach(data, lightId, { skipEdges: NO_STRINGS, keep: shownBy.keep })].filter(([, far]) => far <= 1));
  }, [focus, reached, signal, data, lightId, shownBy]);
  const only = focusNode !== undefined && view.only;
  const drawnNodes = useMemo(
    () => nodes.filter((row) => shownBy.keep(row) && !(only && !lit?.has(row.id))),
    [nodes, shownBy, only, lit],
  );
  const matches = useMemo(() => searchNodes(drawnNodes, query), [drawnNodes, query]);
  const highlighted = useHighlighted();
  /** The graph's wires as the canvas holds them: the server's edges less each `produced_by`
      that only companions a `narrows` or a `requires` between the same two Issues. */
  const wiresDrawn = useMemo(() => (data === undefined ? [] : wires(data)), [data]);
  /**
   * What the tools count, as the design's `setCut` writes it: while the timeline
   * stands partway through the graph's life, the cut's own records and only the
   * wires whose both ends are among them. So the number climbs with the sweep
   * instead of claiming the whole field while Replay holds records back.
   */
  const fieldCount = useMemo(() => {
    if (shown === null) return [nodes.length, wiresDrawn.length];
    let wiresMade = 0;
    for (const row of wiresDrawn) if (shown.has(row.from_id) && shown.has(row.to_id)) wiresMade++;
    return [shown.size, wiresMade];
  }, [shown, nodes.length, wiresDrawn]);
  const groups = matchGroups(matches, focusNode && focus && lit ? { node: focusNode, hops: focus.hops, lit } : null);
  const rows = groups.flatMap((group) => group.rows.slice(0, MAX_ROWS));
  const markedAt = Math.min(marked, rows.length - 1);
  const resultsId = useId();
  const keyId = useId();
  const searching = query.trim() !== "";
  const grouped = place.grouped ?? true;
  const roots = useMemo(() => (grouped && data ? findRoots(data) : null), [grouped, data]);
  /** What a focus, a selection or a hover lights names: its busiest nodes, capped, so a ring keeps readable. */
  const litLabels = useMemo(() => {
    if (lit === null) return [];
    return model.nodes
      .filter((drawn) => !drawn.hidden && lit.has(drawn.id))
      .sort((a, b) => b.degree - a.degree)
      .slice(0, LABELLED_MAX)
      .map((drawn) => drawn.id);
  }, [lit, model, data]);
  const lens = useMemo<Lens>(
    () => ({
      hiddenKinds: shownBy.kinds,
      hiddenStatuses: shownBy.statuses,
      hiddenOwners: shownBy.owners,
      lit,
      skipEdges: focusNode ? shownBy.skipEdges : NO_STRINGS,
      only,
      strong: new Set([focusNode?.id, selected?.id, peer?.id].filter((id) => id !== undefined)),
      highlighted,
      matches: new Set([...matches.map((row) => row.id), ...(signal?.ids ?? [])]),
      labelled: new Set([...(roots?.groups.flatMap((group) => (group.root ? [group.key] : [])) ?? []), ...litLabels, ...(peer ? [peer.id] : [])]),
      shown,
    }),
    [shownBy, lit, focusNode, only, selected, peer, highlighted, matches, roots, litLabels, signal, shown],
  );

  const focusOn = (row: GraphNode) => go({ focus: { ref: { kind: row.kind, seq: row.seq }, hops: focus?.hops ?? 1 } });
  /** A `needs attention` row picked: its beliefs hold the light and the frame. The
   *  same row again hands the canvas back, as clearing a focus does. */
  const showSignal = (key: SignalKey, ids: readonly string[]) => {
    if (signal?.key === key) setSignal(null);
    else {
      setSignal({ key, ids });
      setFraming(new Set(ids));
    }
  };
  // Double-click: one animated step straight into the node — not the wheel's
  // five turns. It selects and centres it in the visible column as it goes.
  const onDoubleClick = useEffectEvent((node: DrawNode) => {
    setSelectedId(node.id);
    renderer?.centre(node);
  });
  // An island's name on the canvas: pick its root, as the roots list's row would.
  const onIsland = useEffectEvent((key: string) => frameRoot(key));
  useEffect(() => {
    const canvas = host.current!;
    const made = createRenderer(
      canvas,
      {
        hover: (node) => setHovered(node ? { node, ...made.screenOf(node) } : null),
        click: (node) => setSelectedId(node?.id ?? null),
        doubleClick: (node) => onDoubleClick(node),
        island: (key) => onIsland(key),
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
  // canvas, and the roots list, the search results or the briefing how wide it
  // is: frame again once it has rendered.
  const panels = `${view.key} ${view.roots} ${peek.collapsed} ${results.collapsed} ${briefing.collapsed}`;
  const panelsShown = useRef(panels);
  useLayoutEffect(() => {
    if (panelsShown.current === panels) return;
    panelsShown.current = panels;
    renderer?.refit();
  }, [panels, renderer]);
  // The field of islands: one compact ball per line of inquiry, as many as the
  // inquiry has, none of them a giant — see islands.ts. Ungrouped, the hash's
  // group=none, the dust stands alone: no homes, no names.
  const islands = useMemo(() => (grouped && data ? islandHomes(data) : null), [grouped, data]);
  const homes = islands?.homes ?? null;
  useEffect(() => renderer?.group(homes ? (node) => homes.get(node.id) : null), [renderer, homes]);
  // Each island's name while grouped: its root's title, in the theme's inks. The
  // canvas draws them only while zoomed out far enough that root labels alone
  // would not carry, and a click on one picks the root.
  const islandNames = useMemo(() => {
    if (!roots || !data) return null;
    // An island may be a whole inquiry or one sub-inquiry of a deep one, so any
    // record's title can crown it.
    const names = new Map<string, string>(data.nodes.map((node) => [node.id, node.title || "(untitled)"]));
    for (const group of roots.groups) if (group.root) names.set(group.key, group.root.title || "(untitled)");
    // ...but not a record that heads nothing: a ball of records no one built on
    // would otherwise wear one of their titles as if it were the inquiry.
    for (const key of islands?.headless ?? []) names.delete(key);
    return names;
  }, [roots, data, islands]);
  useEffect(() => {
    if (!renderer) return;
    renderer.nameIslands(
      grouped && islandNames ? (key) => islandNames.get(key) : null,
      { title: palette.halo, count: palette.fallback, outline: palette.background },
    );
  }, [renderer, grouped, islandNames, palette]);
  // A selected node the graph no longer has stays unselected should it come back (R3-03).
  useEffect(() => {
    if (selectedId !== null && data && !selected) setSelectedId(null);
  }, [selectedId, selected, data]);
  useDebugHook(model, renderer, host, selected?.id ?? null, focusNode?.id ?? null, homes);

  const select = (row: GraphNode) => {
    setSelectedId(row.id);
    setCentring(row.id);
    setPeer(null);
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
  // The canvas's own light — the field dimmed, the wires drawn out, the ring and
  // the name — follows a selection made anywhere, not only one the pointer made.
  useEffect(() => {
    renderer?.select(selected ? (model.find(selected.id) ?? null) : null);
  }, [renderer, model, selected]);
  // The selection is shared with the canvas, and one operation crosses either
  // way: a pick here is the `show trax.record` an agent writes, and an agent's
  // `show` arrives as this view's own selection. `wrote` is what this tab last
  // sent, so the answer to its own write cannot move the view under the pointer.
  // The selection is shared with the canvas: a pick here is the `show trax.record`
  // an agent would write, so the record the reader is on and the record the canvas
  // holds are the same one. `wrote` is what this tab last sent, so a repeat pick
  // does not rewrite the canvas.
  //
  // One way only. Reading the canvas back -- an agent's `show` arriving as this
  // view's selection, and its record taken away ending one -- wants the station's
  // visuals, and a route is not a visual: the canvas hands its state to the panes
  // inside it, not to the page behind them. Taking the pane down on a cleared
  // selection wants the same thing, an instance id, so the pane keeps its last
  // record rather than emptying.
  const canvas = useWorkspaceActions();
  const wrote = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (!canvas?.visualTypes.has(RECORD_VISUAL)) return;
    // Nothing selected and nothing sent yet: the graph has no claim on the canvas,
    // and a record already on it is whoever put it there's to keep.
    if (selectedId === null && wrote.current === undefined) return;
    if (selectedId === wrote.current) return;
    wrote.current = selectedId;
    // A selection moves the record, not the pane, which stays where it was put.
    if (selectedId !== null) canvas.operate({ kind: "show", visual_type: RECORD_VISUAL, record_id: selectedId });
  }, [canvas, selectedId]);
  /**
   * A root picked in the roots list or by its island's name: its briefing takes
   * the roots list's place, Peek opens on the root, and its island is framed
   * beside Peek. The island is the nodes laid out by it (`home`), not all under
   * it: a node under several roots sits by the nearest, in another island.
   */
  const frameRoot = (key: string) => {
    const group = roots?.groups.find((found) => found.key === key);
    if (!group || !roots) return;
    // Selecting the root opens Peek on it, and its briefing heads that; the nodes
    // under no root have no root to select, so their pick only frames them.
    setSelectedId(group.root?.id ?? null);
    setFraming(new Set([...roots.home].flatMap(([id, home]) => (home === key ? [id] : []))));
  };
  // The cluster the selection tops, if it tops one: its briefing heads Peek, so
  // that reading a thread and reading a record are the same act, and the roots
  // list keeps its place while they are read.
  const threadGroup = selected === undefined ? undefined : roots?.groups.find((found) => found.root?.id === selected.id);
  // The thread the selection belongs to, whatever it is: its root names the
  // Peek's crumb, the way the design's breadcrumb did.
  const ownGroup = selected === undefined || threadGroup ? undefined : roots?.groups.find((found) => roots.home.get(selected.id) === found.key);
  const setLimit = (limit: number) => {
    // A Replay holds answers back; a new limit's answer draws at once.
    replayer.stop();
    setConsent(null);
    setView((state) => ({ ...state, limit }));
  };
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
    ...(data && !searching ? [panelCommand(briefing)] : []),
    ...(searching ? [panelCommand(results)] : []),
    ...(selected
      ? [
          panelCommand(peek),
          { id: "graph.clear", title: "Clear the selection", keys: ["Escape"], run: () => setSelectedId(null) },
          { id: "graph.focus", title: "Focus the graph on the selection", keys: ["."], section: "Graph", run: () => focusOn(selected) },
        ]
      : focus
        ? [{ id: "graph.unfocus", title: "Clear the graph's focus", keys: ["Escape"], section: "Graph", run: () => go({ focus: undefined }) }]
        : signal
          ? [{ id: "graph.unsignal", title: "Clear the flagged beliefs", keys: ["Escape"], section: "Graph", run: () => setSignal(null) }]
          : []),
  ]);
  const followLink = usePeekLinks(meta, drawnNodes, selected, select);

  const hidden: Hidden = { kinds: view.hiddenKinds, statuses: view.hiddenStatuses, owners: view.hiddenOwners };
  const litEdges = data && lit ? data.edges.filter((row) => lit.has(row.from_id) && lit.has(row.to_id) && !lens.skipEdges.has(row.edge_kind)) : [];
  const drawnIds = new Set(drawnNodes.map((row) => row.id));
  // What the field holds: the wires a reader can actually follow, so the count in the
  // tools and the key's agree with the canvas rather than the server's row count.
  const drawnEdges = wiresDrawn.filter((row) => drawnIds.has(row.from_id) && drawnIds.has(row.to_id));
  const keyShown = view.key && !asking && nodes.length > 0;
  const present = {
    kinds: meta.kinds.filter((kind) => nodes.some((node) => node.kind === kind)),
    statuses: (meta.enums.status ?? []).filter((status) => nodes.some((node) => node.status === status)),
    owners: [...new Set(nodes.flatMap((node) => (node.kind !== "AgentSession" && node.owner !== undefined ? [node.owner] : [])))].sort(),
  };
  /** The matches list, which the search box controls, shows. */
  const listed = searching && !results.collapsed;
  // The design's field has no header of its own: the canvas takes the viewport to
  // its edges (styles.css, `.graph-bleed`). The wordmark and the way back to the
  // navigation are the shell's top bar and rail, which stand over every route, so
  // the proto's tool row carries only what belongs to the graph.
  return (
    <div className="view graph">
      {/* Every view is named by a level-one heading; the design's top bar has no
          title to hang it on, so it names the page without showing it. */}
      <h1 className="sr-only">Graph</h1>
      <div className="view-tools proto-topbar">
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
        <div className="spacer" />
        <span className="proto-stat num">
          {data === undefined ? "loading…" : `${counted(fieldCount[0], "node")}, ${counted(fieldCount[1], "edge")}`}
        </span>
        <LimitMenu limit={view.limit} onLimit={setLimit} />
        <button type="button" className="btn ghost proto-fit" title="Fit the graph (f)" onClick={() => renderer?.fit()}>
          fit
        </button>
      </div>
      <FilterChips
        hidden={hidden}
        present={present}
        onRemove={(field) =>
          setView((state) => ({
            ...state,
            ...(field === "kinds" ? { hiddenKinds: [] } : field === "statuses" ? { hiddenStatuses: [] } : { hiddenOwners: [] }),
          }))
        }
        onClear={() => setView((state) => ({ ...state, hiddenKinds: [], hiddenStatuses: [], hiddenOwners: [] }))}
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
            onPick={(row, at) => {
              // The click marks its own row, so the list names what it opened: the
              // keys step on from there.
              setMarked(at);
              select(row);
            }}
            onFocus={focusOn}
            panel={results}
          />
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
          {/* One panel, as the prototype's #side stack is: the briefing holds the
              corner until a record is picked, and the record's own panel takes it --
              a thread's root reads as that cluster, so drawing the briefing beside it
              would list the same rows on both sides of the field. The crumb names the
              way back. Only a search takes the corner besides the briefing, and only a
              search needs it. Hid, the briefing slides out and leaves its own pill, so
              the canvas has the width. */}
          {data && !searching && !(selected && !peek.collapsed) ? (
            <>
              <Briefing
                graph={data}
                palette={palette}
                panel={briefing}
                roots={roots}
                onPick={select}
                onSignal={showSignal}
                signal={signal?.key ?? null}
              />
              {briefing.collapsed ? <BriefingShow panel={briefing} /> : null}
            </>
          ) : null}
          <div className="proto-legend">
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
                hiddenEdges={view.skipEdges}
                onHiddenEdges={(skipEdges) => setView((state) => ({ ...state, skipEdges }))}
              />
            ) : null}
            <button
              type="button"
              className="proto-legend-pill"
              aria-pressed={view.key}
              aria-controls={keyShown ? keyId : undefined}
              onClick={() => setView((state) => ({ ...state, key: !state.key }))}
            >
              legend <span>{view.key ? "▾" : "▴"}</span>
            </button>
          </div>
          {data ? (
            <Timeline
              bins={timelineBins}
              position={replayer.position}
              playing={replayer.running}
              date={replayDate}
              grown={shown === null ? null : shown.size}
              onScrub={(fraction) => scrubTo(fraction)}
              onToggle={() => {
                if (replayer.running) replayer.stop();
                else if (data && !asking) replayer.start(data);
              }}
            />
          ) : null}
          {hovered ? (
            <Tooltip {...hovered} far={focus ? reached?.get(hovered.node.id) : undefined} hint="Click opens Peek · double-click focuses here" />
          ) : null}
          <AnimatePresence>
            {selected ? (
              // Its own box, so the capture sees Peek's links before they navigate; Peek places itself.
              <motion.div
                key="peek"
                className="graph-peek"
                onClickCapture={followLink}
                initial={{ opacity: 0, x: 24 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 24 }}
                transition={{ type: "spring", stiffness: 420, damping: 36 }}
              >
                <Peek
                  row={selected}
                  panel={peek}
                  onClose={() => setSelectedId(null)}
                  // What the panel covers is what a fit frames around: once the drag
                  // lets go, the field is framed again in the room the panel left it.
                  onResizeEnd={() => renderer?.refit()}
                  crumbs={
                    threadGroup ? (
                      // The stack's home crumb, as the prototype's #sBack is: pressing it
                      // leaves the record, and the briefing takes its corner back.
                      <button type="button" className="peek-crumb mono" title="Back to the briefing" onClick={() => setSelectedId(null)}>
                        briefing
                      </button>
                    ) : ownGroup?.root ? (
                      <button type="button" className="peek-crumb mono" title={ownGroup.root.title} onClick={() => select(ownGroup.root!)}>
                        briefing / {ownGroup.root.title || "(untitled)"}
                      </button>
                    ) : null
                  }
                  body={
                    // A thread's root reads as its cluster; every other record reads as itself: one view, never both.
                    data === undefined ? null : threadGroup ? (
                      <ThreadBriefing group={threadGroup} graph={{ nodes, edges: data.edges }} palette={palette} onSelectNode={select} />
                    ) : (
                      <Suspense>
                        <GraphRecord
                          node={selected}
                          graph={{ nodes, edges: data.edges }}
                          palette={palette}
                          onSelectNode={select}
                          onHoverPeer={setPeer}
                        />
                      </Suspense>
                    )
                  }
                  foot={
                    <>
                      <button
                        type="button"
                        className="btn ghost mono"
                        onClick={() => void navigator.clipboard?.writeText(`${selected.kind}#${selected.seq}`)}
                      >
                        copy ref
                      </button>
                      <button type="button" className="btn ghost mono" onClick={() => focusOn(selected)}>
                        isolate
                      </button>
                      <a className="btn ghost mono" href={formatRoute({ name: "ref", kind: selected.kind, seq: selected.seq })}>
                        Open full page
                      </a>
                    </>
                  }
                />
              </motion.div>
            ) : null}
          </AnimatePresence>
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
    const owners = new Set(view.hiddenOwners);
    return {
      kinds,
      statuses,
      owners,
      skipEdges: new Set(view.skipEdges),
      keep: (row: GraphNode) => !kinds.has(row.kind) && !statuses.has(row.status) && !owners.has(row.owner ?? ""),
    };
  }, [view.hiddenKinds, view.hiddenStatuses, view.hiddenOwners, view.skipEdges]);
}

/**
 * What covers the canvas `host` now, as far in as each panel reaches from its own
 * side: the briefing on the left, the record, the key and the zoom buttons on the
 * right. The key is a column on the right, above the zoom buttons, since most
 * windows give a canvas wider than tall, which frames a graph by its height: a
 * strip on its right costs that frame less than a band across its foot. Measuring
 * live rects rather than assuming a side is what keeps the frame honest while the
 * legend folds away for an open record (graph.css) — a panel with no width covers
 * nothing.
 */
function coveredOf(host: HTMLElement): Covered {
  const canvas = host.getBoundingClientRect();
  const middle = (canvas.left + canvas.right) / 2;
  let left = 0;
  let right = 0;
  for (const selector of [".graph-peek > :not([hidden])", ".proto-briefing", ".graph-key", ".graph-zoom"]) {
    const at = host.parentElement!.querySelector(selector)?.getBoundingClientRect();
    if (!at || at.width === 0) continue;
    // A briefing slid past the viewport's edge covers nothing, mid-slide or settled:
    // its class says so rather than a rect the transition is still moving.
    if (selector === ".proto-briefing" && host.parentElement!.querySelector(`${selector}.is-hidden`)) continue;
    if ((at.left + at.right) / 2 < middle) left = Math.max(left, at.right - canvas.left);
    else right = Math.max(right, canvas.right - at.left);
  }
  return { left, right };
}

/** The node a focus names, if the graph has it. */
function findFocus(nodes: readonly GraphNode[], ref: FocusRef): GraphNode | undefined {
  return nodes.find((row) => ("id" in ref ? row.id === ref.id : row.kind === ref.kind && row.seq === ref.seq));
}

/** The sizes a focus that is not loaded yet may widen the view to, smallest first. */
const LIMIT_STEPS: readonly number[] = [1_000, 5_000];

/** One text per focus, so widening is tried once for each and not once per render. */
function refKey(ref: FocusRef): string {
  return "id" in ref ? ref.id : `${ref.kind}#${ref.seq}`;
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
 * Replay, restartable. While it runs the view shows the graph up to where the
 * sweep has reached, and answers wait; `onStart` clears what the view had picked.
 */
function useReplay(onStart: () => void) {
  const [running, setRunning] = useState(false);
  /** How far through the graph's life the view stands, 0 to 1; 1 when not replaying. */
  const [position, setPosition] = useState(1);
  const stop = useRef(() => {});
  useEffect(() => () => stop.current(), []);
  return {
    running,
    position,
    at: (fraction: number) => setPosition(fraction),
    stop: () => {
      stop.current();
      setRunning(false);
      setPosition(1);
    },
    start: (graph: Graph) => {
      stop.current();
      onStart();
      setRunning(true);
      // Back to the first record at once: the sweep's first frame comes a frame
      // later, and the field must not show whole in between.
      setPosition(0);
      stop.current = replay(graph, {
        // The graph is not re-sent: the whole of it is already drawn, and the
        // place moves over it, so the islands stay where they settled and fill.
        step: setPosition,
        done: () => {
          setRunning(false);
          setPosition(1);
        },
      });
    },
  };
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
  homes: ReadonlyMap<string, string> | null,
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
          const { id, kind, seq, title, status, owner, hidden, x, y } = node;
          return { id, kind, seq, title, status, owner, hidden, x, y, r: node.look.radius, home: homes?.get(id) ?? null, screen: screenOf(node) };
        }),
        links: model.links.map(({ from, to, kind, valence, asserted, actor, hidden }) => ({ from, to, kind, valence, asserted, actor, hidden })),
        selected,
        focus,
      };
    });
    return () => void Reflect.deleteProperty(debug, "graph");
  }, [model, renderer, host, selected, focus, homes]);
}

/** A count and its noun, agreeing: "1 node", "9,041 edges". */
const counted = (value: number, noun: string) => `${value.toLocaleString("en-US")} ${noun}${value === 1 ? "" : "s"}`;

/** `values` with `value` added, or taken out if it was there. */
function toggled(values: readonly string[], value: string): string[] {
  return values.includes(value) ? values.filter((other) => other !== value) : [...values, value];
}

/** What the view keeps for the tab. */
type ViewState = {
  readonly limit: number;
  readonly hiddenKinds: readonly string[];
  readonly hiddenStatuses: readonly string[];
  readonly hiddenOwners: readonly string[];
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
  return { limit: 1000, hiddenKinds: [], hiddenStatuses: [], hiddenOwners: [], skipEdges: [], only: false, key: false, roots: false };
}

/** The view's state kept for the tab, or null for anything this build cannot use. */
function readViewState(saved: unknown): ViewState | null {
  if (typeof saved !== "object" || saved === null) return null;
  const { limit, hiddenKinds, hiddenStatuses, skipEdges, only, key, roots } = saved as { [field: string]: unknown };
  // Tabs kept before the Owner filter have no hiddenOwners: they start unfiltered by person.
  const hiddenOwners = (saved as { [field: string]: unknown }).hiddenOwners ?? [];
  return Number.isSafeInteger(limit) &&
    (limit as number) >= 1 &&
    isStrings(hiddenKinds) &&
    isStrings(hiddenStatuses) &&
    isStrings(hiddenOwners) &&
    isStrings(skipEdges) &&
    typeof only === "boolean" &&
    typeof key === "boolean" &&
    typeof roots === "boolean"
    ? { limit: limit as number, hiddenKinds, hiddenStatuses, hiddenOwners, skipEdges, only, key, roots }
    : null;
}

/** The canvas visual that holds one record: what a selection here shows there. */
const RECORD_VISUAL = "trax.record";
const STATE_KEY = "trackinizer.v2.graph";
/** The most nodes of a lit neighbourhood that name themselves under it. */
const LABELLED_MAX = 24;
/** How many of the newest records a replay brings up rather than just turning on: the design's 24. */
const ARRIVING = 24;
const NO_NODES: readonly GraphNode[] = [];
const NO_BINS: readonly number[] = [];
const NO_STRINGS: ReadonlySet<string> = new Set();
const NO_COUNTS = { 1: 0, 2: 0, 3: 0, all: 0 } as const;
const EVERY_NODE = () => true;
