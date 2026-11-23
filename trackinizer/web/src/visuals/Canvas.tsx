import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createPortal } from "react-dom";
import { useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ApiError } from "../api/client";
import { findRef } from "../api/detail";
import { newUuid } from "../api/idempotency";
import { getVisualCatalog, type VisualDescription } from "../api/visuals";
import {
  applyWorkspaceOperation,
  type WorkspaceOperation,
  type WorkspaceState,
} from "../api/workspaces";
import { acceptWorkspace as cacheWorkspace, newerWorkspace } from "../stations/cache";
import { setStation } from "../stations/store";
import { useStation } from "../stations/useStation";
import { CrashBoundary } from "../app/CrashBoundary";
import { useWorkbenchMounted, useWorkbenchSlot } from "../app/workbenchSlot";
import { usePreferences } from "../preferences";
import { parseHash } from "../router/route";
import { useHash } from "../router/trail";
import { useBrowserState } from "../state/store";
import { CaptureContext } from "../canvasCapture/stageRegistry";
import { MarkLayer } from "../canvasMarks/MarkLayer";
import { useTileBodies } from "../canvasMarks/tileBodies";
import { useBoxes } from "../canvasMarks/useBoxes";
import { CanvasBar } from "./CanvasBar";
import { forget, go, interrupt, settle, step, takeInterrupted, useCanvasHistory } from "./canvasHistory";
import { type Size, dropLegacySizes, legacyLayout, readLegacySizes } from "./canvasSizes";
import { HOME_ROUTE, homeSnapshot, movesThePage, PAGE, planRestore, type Snapshot, snapshotOf } from "./canvasSnapshot";
import { type CanvasTab, leftTab, MAX_TABS, neighbourOf, newTabName, openTabOf, sizesOf, tabSnapshot, tabsOf } from "./canvasTabs";
import { followColumns } from "./chatColumnState";
import { orderVisuals } from "./layout";
import { holds, layoutOf, useLayout } from "./stationLayout";
import { preloadRenderers, RENDERERS, VisualPane } from "./registry";
import { WorkspaceActionsProvider } from "./workspaceActions";
import "./canvas.css";

/** The route view inside a revisioned canvas shared with agent clients. */
export function Canvas({ children }: { readonly children: ReactNode }) {
  const queryClient = useQueryClient();
  const catalog = useQuery({
    queryKey: ["visuals", "catalog"],
    queryFn: ({ signal }) => getVisualCatalog({ signal }),
    staleTime: Infinity,
    retry: false,
  });
  // The canvas is what the shell's stream last said (its opening frame is the
  // first state) and what this canvas's own writes returned. A read happens
  // only when asked for, after a refused write.
  const station = useStation();
  const workspace = station.data;
  const workspaceId = workspace?.id;
  const boxes = useBoxes(workspace);
  const { bodies, bodyRef } = useTileBodies();
  const latestWorkspace = useRef(workspace);
  latestWorkspace.current = workspace;
  const legacyPreview = import.meta.env.DEV && station.error instanceof ApiError && station.error.status === 404;
  const [previewSelected, setPreviewSelected] = useState<string[] | null>(null);
  const controlsSlot = useWorkbenchSlot();
  useWorkbenchMounted();
  const [artifactLink, setArtifactLink] = useState("");
  const [artifactLinkError, setArtifactLinkError] = useState<string | null>(null);
  // Browse stays under the stage: moving DOM nodes would reset iframe documents
  // and focus even when React keeps their components mounted.
  const browseRef = useRef<HTMLDivElement>(null);
  const browseSlotRef = useRef<HTMLDivElement>(null);
  // How the station's tiles and columns were left, as fractions of the stage,
  // and the stage they are fitted to. The station keeps the layout; the stage is
  // measured as it resizes, and nothing is written for a fit.
  const [layout, changeLayout] = useLayout();
  // The open canvas tab, and what it shows: the steps Back and Forward walk are
  // its own, as a browser tab's history is. Chat's agent changes the station's
  // canvas, which is always the open tab.
  const home = usePreferences().home;
  const hash = useHash();
  const tabs = tabsOf(layout);
  const openTab = openTabOf(layout);
  const historyKey = workspaceId ? tabKey(workspaceId, openTab) : null;
  const snapshot = workspace ? snapshotOf(workspace.visuals, hash) : null;
  const history = useCanvasHistory(historyKey, snapshot);
  const [browser, updateBrowser] = useBrowserState();
  const [stage, setStage] = useState<Size>({ width: 0, height: 0 });
  const stageRef = useRef<HTMLDivElement>(null);
  // An agent may ask for a picture of this stage; the boxes are read off it when it does.
  const captures = useContext(CaptureContext);
  useEffect(() => {
    const stage = stageRef.current;
    if (!captures || !stage) return;
    captures.set({ stage });
    return () => captures.set(null);
  }, [captures]);
  const [writeError, setWriteError] = useState<string | null>(null);
  const active = workspace?.visuals.map((visual) => visual.type)
    ?? previewSelected ?? [catalog.data?.default_visual ?? "trax.browse"];
  const visible: WorkspaceState["visuals"] = workspace?.visuals ?? (active.length ? active : ["trax.browse"]).map((type) => {
    const descriptor = catalog.data?.visuals.find((visual) => visual.type === type);
    return {
      id: type,
      type,
      version: descriptor?.version ?? 1,
      // The page stands in the strip of main visuals from the first render, before
      // the catalog or the canvas has arrived, so it never moves from the side.
      // Every other visual takes the placement its catalog entry names, and failing
      // that the server's rule: a wide one is a page, a compact one a column.
      placement: type === PAGE ? "main" as const
        : descriptor?.default_placement ?? (descriptor?.default_size === "wide" ? "main" as const : "side" as const),
      record_id: null,
      params: {},
    };
  });
  const visualTypes = useMemo(() => new Set(catalog.data?.visuals.map((visual) => visual.type)), [catalog.data]);
  const panes = orderVisuals(visible, workspace?.focused_instance ?? null);
  const pageClosed = workspace !== undefined && !workspace.visuals.some((visual) => visual.type === PAGE);
  const browse = pageClosed ? null : (panes.find((pane) => pane.type === PAGE) ?? {
    id: PAGE,
    type: PAGE,
    version: catalog.data?.visuals.find((visual) => visual.type === PAGE)?.version ?? 1,
    placement: "main" as const,
    record_id: null,
    params: {},
  });
  /**
   * Whether the stage is showing the routed page instead of the visual over it.
   *
   * The rail navigates — Graph, Files, Runs, Evidence are routes — and a route draws only
   * in the page pane. A composition sits over that pane, and the control that moved one
   * aside was removed, leaving a destructive Clear as the only way back: every rail click
   * changed the address and nothing on screen. This is that control, restored as a toggle,
   * and it writes nothing to the station, so the composition is still there to come back to.
   */
  const [onPage, setOnPage] = useState(false);

  // Going to a page shows it: the rail's places are routes, and with the toggle alone a
  // click there changed the address under a composition that kept the stage. Only an
  // actual change counts, so a station still opens on whatever it last showed.
  const paged = movesThePage(hash);
  const cameFrom = useRef(hash);
  useEffect(() => {
    const before = cameFrom.current;
    cameFrom.current = hash;
    if (hash === before || !paged) return;
    setOnPage(true);
    // The page pane can be closed outright, and then a route has no surface to draw in.
    if (!latestWorkspace.current?.visuals.some((visual) => visual.type === PAGE)) {
      operate({ kind: "show", visual_type: PAGE, placement: "main" });
    }
  }, [hash, paged]);

  // A visual just put up takes the stage back: an answer composed while the reader is on
  // Graph would otherwise land behind the page and never be seen.
  const standing = visible.filter((visual) => visual.type !== PAGE).map((visual) => visual.id);
  const standingKey = standing.join(" ");
  const stood = useRef<readonly string[]>(standing);
  useEffect(() => {
    const before = stood.current;
    stood.current = standing;
    if (standing.some((id) => !before.includes(id))) setOnPage(false);
    // `standing` is this render's array; `standingKey` is what makes it a change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [standingKey]);

  /**
   * The one surface the canvas draws.
   *
   * The stage used to hold any number of tiles at once — a strip of main ones, a
   * column down each side, floating windows over all three, with dividers to resize
   * them and drop zones to dock them. That is a window manager, and a window manager
   * is a set of decisions a reader has to make before they can read anything: which
   * pane, how wide, which side. The canvas asks none of them now. It shows what was
   * shown last, whole, and showing something else replaces it.
   *
   * The page is the resting surface, so it draws whenever nothing has been put on top
   * of it; the focused instance wins when the agent or a click named one.
   */
  const shown = (onPage ? browse : null)
    ?? panes.find((pane) => pane.id === workspace?.focused_instance)
    ?? [...visible].reverse().map((visual) => panes.find((pane) => pane.id === visual.id)).find((pane) => pane && pane.type !== PAGE)
    ?? browse
    ?? panes[0];
  // Three columns, the left, the page's and the right, take the room Chat stands in.
  // Chat keeps its own width: the stage beside it is one surface now, never three
  // columns that had to be squeezed to make room.
  useEffect(() => followColumns(false), []);
  const route = parseHash(window.location.hash, []);
  const refRoute = /^#\/ref\/([^/?]+)\/(\d+)$/.exec(window.location.hash);
  const refKind = refRoute?.[1] ?? null;
  const refSeq = refRoute?.[2] ? Number(refRoute[2]) : null;
  const refId = useQuery({
    queryKey: ["workspace", "current-record", refKind, refSeq],
    queryFn: ({ signal }) => findRef(refKind!, refSeq!, { signal }),
    enabled: refKind !== null && refSeq !== null && Number.isSafeInteger(refSeq),
    staleTime: Infinity,
    retry: false,
  });
  // The record the page shows, from the address alone: the page is the route.
  const currentRecordId = route.name === "lookup" ? route.id : refKind ? refId.data ?? null : null;

  // The stage's size, which the layout's fractions are fitted to.
  useLayoutEffect(() => {
    const element = stageRef.current;
    if (!element) return;
    const measure = () => {
      setStage((previous) => previous.width === element.clientWidth && previous.height === element.clientHeight
        ? previous : { width: element.clientWidth, height: element.clientHeight });
      projectBrowse(element, browseRef.current, browseSlotRef.current);
    };
    measure();
    addEventListener("resize", measure);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(element);
    if (browseSlotRef.current) {
      observer?.observe(browseSlotRef.current);
      observer?.observe(browseSlotRef.current.parentElement!);
    }
    addEventListener("scroll", measure, true);
    return () => {
      removeEventListener("resize", measure);
      observer?.disconnect();
      removeEventListener("scroll", measure, true);
    };
  }, [browse?.placement, panes.length]);

  useLayoutEffect(() => {
    if (stageRef.current) projectBrowse(stageRef.current, browseRef.current, browseSlotRef.current);
  });

  // A browser that kept column widths, window sizes and places before the station
  // did: the first station it shows takes them, as fractions of its stage, unless
  // it already holds a layout of its own, and the browser forgets them either way.
  useEffect(() => {
    if (!workspaceId || stage.width === 0 || stage.height === 0) return;
    const forget = () => {
      dropLegacySizes("canvas");
      if (Object.keys(browser.ui.tiles).length) {
        try {
          updateBrowser((state) => ({ ...state, ui: { ...state.ui, tiles: {} } }));
        } catch {
          // Storage is off, full or holds a value this build cannot read.
        }
      }
    };
    if (!holds(layoutOf(workspace?.layout), "columns", "floating", "tiles", "share")) {
      const patch = legacyLayout(readLegacySizes(), browser.ui.tiles, stage);
      if (Object.keys(patch).length) {
        changeLayout(patch, { now: true, onSaved: forget });
        return;
      }
    }
    forget();
  }, [workspaceId, stage.width > 0 && stage.height > 0]);

  function acceptWorkspace(state: WorkspaceState) {
    cacheWorkspace(queryClient, state);
    if (latestWorkspace.current?.id === state.id) latestWorkspace.current = newerWorkspace(latestWorkspace.current, state);
  }

  useEffect(() => {
    if (!workspaceId) return;
    const timer = setTimeout(preloadRenderers, 0);
    return () => clearTimeout(timer);
  }, [workspaceId]);

  // The redesign's rule: the page, once closed, stays closed. The canvas is the

  const change = useMutation({
    // A station change resets the observer, preserving queued mutations' scopes.
    mutationKey: ["workspace", workspaceId, "operation"],
    scope: { id: `workspace:${workspaceId ?? "default"}` },
    mutationFn: async ({ id, operation }: { readonly id: string; readonly operation: WorkspaceOperation }) => {
      const current = queryClient.getQueryData<WorkspaceState>(["workspace", id]);
      if (!current || current.id !== id) throw new Error("The workspace is not ready.");
      return applyWorkspaceOperation(id, current.revision, operation, newUuid());
    },
    onMutate: () => setWriteError(null),
    onSuccess: acceptWorkspace,
    onError: (_error, { id }) => {
      if (latestWorkspace.current?.id !== id) return;
      setWriteError("Could not update the workbench. Its revision may have changed; try again.");
      void station.refetch();
    },
  });

  /** Apply `operation` to station `id`, the one on show unless named. */
  async function write(operation: WorkspaceOperation, id = latestWorkspace.current?.id): Promise<boolean> {
    if (!id) return false;
    try {
      await change.mutateAsync({ id, operation });
      return true;
    } catch {
      return false;
    }
  }

  /**
   * Show `target` on the canvas, a step tab `key` has already taken: its
   * visuals by the canvas's own operations, one after another, then its page.
   * The tab then stands where the canvas got to, there or short of it. Every
   * step is the station's it began on: one opened while they are on their way
   * (the top bar puts every station a click away) is not this tab's, so the
   * rest are dropped and its page left alone, and the restore is finished when
   * the station is on show again (`interrupt`).
   */
  async function restore(key: string, target: Snapshot): Promise<void> {
    const station = latestWorkspace.current?.id;
    const here = () => latestWorkspace.current?.id === station;
    for (const operation of planRestore(latestWorkspace.current?.visuals ?? [], target)) {
      if (!here() || !(await write(operation, station))) break;
    }
    if (!here()) {
      settle(key, target, null);
      if (station) interrupt(station, key, target);
      return;
    }
    if (target.page !== null && window.location.hash !== target.page) window.location.hash = target.page;
    settle(key, target, snapshotOf(latestWorkspace.current?.visuals ?? [], window.location.hash));
  }

  function stepBy(by: -1 | 1) {
    const target = historyKey ? step(historyKey, by) : null;
    if (historyKey && target) void restore(historyKey, target);
  }

  function goHome() {
    if (!historyKey) return;
    const target = homeSnapshot(home);
    go(historyKey, target);
    void restore(historyKey, target);
  }

  // Only the open tab is cleared: the other tabs keep what they show and their history.
  function clearCanvas() {
    if (!workspaceId || !historyKey) return;
    const target = { page: null, visuals: [] } as const;
    go(historyKey, target);
    void restore(historyKey, target);
  }

  /** The tabs with the open one as it is left now: what it shows, under a sheet the graph, and its sizes. */
  function leaving(): CanvasTab[] {
    const visuals = latestWorkspace.current?.visuals ?? [];
    const now = snapshotOf(visuals, window.location.hash) ?? snapshotOf(visuals, HOME_ROUTE)!;
    return tabs.map((tab) => (tab.id === openTab ? leftTab(tab, now, layout) : tab));
  }

  /** Open tab `id` of `kept`, the tabs the station keeps from now: its sizes at once, then what it shows. */
  function switchTab(id: string, kept: CanvasTab[]) {
    const tab = kept.find((each) => each.id === id);
    if (!workspaceId || !tab) return;
    changeLayout({ ...sizesOf(tab), tabs: kept, tab: id }, { now: true });
    const key = tabKey(workspaceId, id);
    const target = tabSnapshot(tab);
    go(key, target);
    void restore(key, target);
  }

  function addTab() {
    const target = homeSnapshot(home);
    const tab: CanvasTab = { id: newUuid(), name: newTabName(tabs), page: target.page, visuals: target.visuals.map((visual) => ({ ...visual, params: { ...visual.params } })) };
    switchTab(tab.id, [...leaving(), tab]);
  }

  function closeTab(id: string) {
    if (!workspaceId || tabs.length < 2) return;
    const kept = tabs.filter((tab) => tab.id !== id);
    if (id === openTab) switchTab(neighbourOf(tabs, id)!.id, kept);
    else changeLayout({ tabs: kept });
    forget(tabKey(workspaceId, id));
  }

  // A restore this station was left in the middle of: finish it as the station comes back, before
  // a tab switch or a step takes the half-restored canvas for the open tab's own. It moves the page,
  // so the switch that brought the station here is over.
  useEffect(() => {
    if (!workspaceId) return;
    const left = takeInterrupted(workspaceId);
    if (!left || left.key !== tabKey(workspaceId, openTab)) return;
    setStation(workspaceId, null);
    go(left.key, left.target);
    void restore(left.key, left.target);
  }, [workspaceId]);

  function openArtifact(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    let parsed: ReturnType<typeof parseHash>;
    try {
      parsed = parseHash(new URL(artifactLink.trim(), window.location.href).hash, []);
    } catch {
      setArtifactLinkError("Paste an Artifact link.");
      return;
    }
    if (parsed.name !== "lookup") {
      setArtifactLinkError("Paste an Artifact link.");
      return;
    }
    setArtifactLinkError(null);
    void write({ kind: "show", visual_type: "trax.artifact", placement: "main", record_id: parsed.id });
  }

  function operate(operation: WorkspaceOperation) {
    void write(operation);
  }

  function toggle(type: string) {
    if (workspace) {
      const existing = workspace.visuals.find((visual) => visual.type === type);
      if (type === "trax.artifact" && !existing) {
        document.querySelector<HTMLInputElement>("#visual-artifact-link")?.focus();
        return;
      }
      const descriptor = catalog.data?.visuals.find((visual) => visual.type === type);
      const operation: WorkspaceOperation = existing
        ? { kind: "hide", instance_id: existing.id }
        : {
          kind: "show", visual_type: type,
          ...(type === PAGE ? { placement: "main" as const } : {}),
          ...(descriptor?.requires.includes("record") && currentRecordId ? { record_id: currentRecordId } : {}),
        };
      void operate(operation);
    } else if (legacyPreview) {
      setPreviewSelected(active.includes(type) ? active.filter((item) => item !== type) : [...active, type]);
    }
  }

  /**
   * One surface on the stage. `tucked` draws it but takes it out of sight: the page is
   * always rendered, because unmounting a route throws away its scroll, its selection,
   * its focus and any iframe inside it — a visual shown on top of Browse must not
   * destroy what the reader had open underneath. An earlier cut of this replaced the
   * hidden page with an empty placeholder and did exactly that.
   */
  const renderTile = (pane: (typeof panes)[number], tucked = false) => {
    const isPage = pane.type === "trax.browse";
    return (
    <div className={`visual-tile visual-tile-only${pane.type === "trax.browse" ? " visual-tile-browse" : ""}`}
      key={pane.type === "trax.browse" ? pane.type : pane.id}
      ref={isPage ? browseRef : undefined}
      data-visual-instance={pane.id}
      data-visual-type={pane.type}
      style={tucked ? { display: "none" } : undefined}>
      {/* The surface wears no name bar of its own: the Canvas tab above is the header,
          and a visual is put away from the workbench's toggles, not a second chrome.
          The body fills the tile; the red boxes pinned to the tile stand in it. */}
      <div className="visual-tile-body" ref={bodyRef(pane.id)}>
        <VisualPane instance={pane} workspace={workspace ?? null} onWorkspaceChanged={acceptWorkspace}
          focused={workspace?.focused_instance === pane.id}>
          {isPage && <div className="visual-browse-content">
            <CrashBoundary reload={() => window.location.reload()} resetKey={window.location.hash}
              frame={(crash) => <div className="view">{crash}</div>}>{children}</CrashBoundary>
          </div>}
        </VisualPane>
      </div>
    </div>
    );
  };

  return (
    <WorkspaceActionsProvider value={workspace ? {
      busy: change.isPending,
      writeError,
      operate,
      visualTypes,
    } : null}>
    <div className="visual-canvas">
      {workspace && <CanvasBar onPage={onPage} showPage={(page) => {
        setOnPage(page);
        // The page pane can be closed outright, and then there is nothing to put the
        // visual down in front of — the routes have no surface at all. It is opened back
        // BESIDE what is on the stage, not over it: `goHome` would restore the home
        // snapshot and drop the composition the agent just built, which is a reader
        // losing their answer for clicking Graph.
        if (page && !browse) operate({ kind: "show", visual_type: PAGE, placement: "main" });
      }}
        tabs={tabs} open={openTab} back={history.back} forward={history.forward}
        moving={history.moving} home={home} canAdd={tabs.length < MAX_TABS} boxes={boxes}
        actions={{ back: () => stepBy(-1), forward: () => stepBy(1), home: goHome, clear: clearCanvas,
          open: (id) => { if (id !== openTab) switchTab(id, leaving()); }, add: addTab, close: closeTab,
          rename: (id, name) => changeLayout({ tabs: tabs.map((tab) => (tab.id === id ? { ...tab, name } : tab)) }) }} />}
      {writeError && <p className="visual-write-error" role="alert">{writeError}</p>}
      {controlsSlot && createPortal(
        <div className="workbench-controls" role="group" aria-label="Workbench visuals">
          {catalog.isPending && <p className="muted">Loading available visuals…</p>}
          {catalog.isError && <p role="alert">Could not load visuals. <button className="btn" type="button" onClick={() => void catalog.refetch()}>Retry</button></p>}
          {station.isError && !legacyPreview && <p role="alert">Could not open your workbench. <button className="btn" type="button" onClick={() => void station.refetch()}>Retry</button></p>}
          {catalog.data?.visuals.map((visual) => (
            <VisualOption key={visual.type} visual={visual} checked={active.includes(visual.type)}
              disabled={(!workspace && !legacyPreview) || change.isPending
                || (!active.includes(visual.type) && visual.requires.includes("record") && !currentRecordId)}
              missingRecord={visual.requires.includes("record") && !currentRecordId}
              onToggle={() => toggle(visual.type)} />
          ))}
          {catalog.data?.visuals.some((visual) => visual.type === "trax.artifact") &&
            <form className="visual-artifact-select" onSubmit={openArtifact}>
              <input id="visual-artifact-link" aria-label="Artifact link" value={artifactLink}
                placeholder="Paste an Artifact link"
                onChange={(event) => { setArtifactLink(event.target.value); setArtifactLinkError(null); }} />
              <button className="btn" type="submit" disabled={!workspace || !artifactLink.trim() || change.isPending}>
                Open Artifact
              </button>
              {artifactLinkError && <p role="alert">{artifactLinkError}</p>}
            </form>}
        </div>,
        controlsSlot,
      )}
      <div ref={stageRef} className="visual-stage">
        {/* One surface, filling the stage. The page keeps its own slot below whatever
            stands on top of it, because its DOM has to survive the round trip — a
            route that unmounts and remounts loses its scroll, its selection and its
            canvas — so it is hidden rather than removed.

            The redesign's resting state: a canvas with nothing on it is the black
            stage alone — no message, no prompt. A visual appears when it is asked
            for, by the person or the agent, and the stage waits in the meantime. */}
        {browse && renderTile(browse, shown !== browse)}
        {shown && shown !== browse && renderTile(shown)}
        {workspace && <MarkLayer boxes={boxes} bodies={bodies} />}
      </div>
    </div>
    </WorkspaceActionsProvider>
  );
}

/** Fit the fixed Browse tile to its docked flow slot without moving its DOM. */
function projectBrowse(stage: HTMLElement, tile: HTMLElement | null, slot: HTMLElement | null): void {
  if (!tile || !slot || tile.classList.contains("visual-tile-floating")) return;
  const origin = stage.getBoundingClientRect();
  const rect = slot.getBoundingClientRect();
  const clip = slot.parentElement!.getBoundingClientRect();
  const left = rect.left + slot.clientLeft;
  const top = rect.top + slot.clientTop;
  const width = slot.clientWidth;
  const height = slot.clientHeight;
  Object.assign(tile.style, {
    left: `${left}px`, top: `${top}px`,
    width: `${width}px`, height: `${height}px`,
    clipPath: `inset(${Math.max(0, Math.max(clip.top, origin.top) - top)}px ${Math.max(0, left + width - Math.min(clip.right, origin.right))}px ${Math.max(0, top + height - Math.min(clip.bottom, origin.bottom))}px ${Math.max(0, Math.max(clip.left, origin.left) - left)}px)`,
  });
}

function VisualOption({ visual, checked, disabled, missingRecord, onToggle }: {
  visual: VisualDescription;
  checked: boolean;
  disabled: boolean;
  missingRecord: boolean;
  onToggle: () => void;
}) {
  const renderer = RENDERERS[visual.type];
  const supported = renderer?.version === visual.version;
  return (
    <label className="visual-option">
      <input type="checkbox" checked={checked} disabled={disabled || (!checked && !supported)} onChange={onToggle} />
      <span><strong>{visual.title}</strong><small>{visual.description}</small>
        {missingRecord && <small>Open a record to enable this visual.</small>}
        {!supported && <small>Renderer unavailable for version {visual.version}</small>}
      </span>
    </label>
  );
}

/** Where tab `tab` of station `station` keeps its Back and Forward steps. */
function tabKey(station: string, tab: string): string {
  return `${station}/${tab}`;
}

