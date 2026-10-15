import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createPortal } from "react-dom";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type FormEvent, type KeyboardEvent, type MouseEvent, type PointerEvent, type ReactNode } from "react";
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
import { useStation } from "../stations/useStation";
import { CrashBoundary } from "../app/CrashBoundary";
import { useWorkbenchMounted, useWorkbenchSlot } from "../app/workbenchSlot";
import { parseHash } from "../router/route";
import { useBrowserState } from "../state/store";
import { type Size, dropLegacySizes, fitSizes, heldWidth, legacyLayout, MIN_COLUMN, MIN_FLOAT, readLegacySizes, toFraction } from "./canvasSizes";
import { followColumns } from "./chatColumnState";
import { CLICK_SLOP, EDGES, finishGesture, moveGesture, rememberedTile, resizeRect, slideDivider, snapZone, startGesture, TEAR_SLOP, withTile, type Edge, type TileMemory, type Gesture, type Rect, type Zone } from "./floatingTile";
import { orderVisuals, SIDES } from "./layout";
import { holds, layoutOf, useLayout, type Layout } from "./stationLayout";
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
  const latestWorkspace = useRef(workspace);
  latestWorkspace.current = workspace;
  const legacyPreview = import.meta.env.DEV && station.error instanceof ApiError && station.error.status === 404;
  const [previewSelected, setPreviewSelected] = useState<string[] | null>(null);
  const controlsSlot = useWorkbenchSlot();
  useWorkbenchMounted();
  const [artifactLink, setArtifactLink] = useState("");
  const [artifactLinkError, setArtifactLinkError] = useState<string | null>(null);
  const [expandedMobileFloat, setExpandedMobileFloat] = useState<string | null>(null);
  // Browse stays under the stage: moving DOM nodes would reset iframe documents
  // and focus even when React keeps their components mounted.
  const browseRef = useRef<HTMLDivElement>(null);
  const browseSlotRef = useRef<HTMLDivElement>(null);
  // How the station's tiles and columns were left, as fractions of the stage,
  // and the stage they are fitted to. The station keeps the layout; the stage is
  // measured as it resizes, and nothing is written for a fit.
  const [layout, changeLayout] = useLayout();
  const [browser, updateBrowser] = useBrowserState();
  const [stage, setStage] = useState<Size>({ width: 0, height: 0 });
  const stageRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<TileDrag | null>(null);
  // What a drag draws over the stage: where the tile would dock, and, for a
  // docked tile, a card that follows the pointer in its place.
  const snapRef = useRef<HTMLDivElement>(null);
  const ghostRef = useRef<HTMLDivElement>(null);
  // The drags that set the layout's sizes: a column's edge, a floating window's
  // edge or corner, the divider between two docked tiles.
  const resizeRef = useRef<ColumnResize | null>(null);
  const windowRef = useRef<WindowResize | null>(null);
  const dividerRef = useRef<DividerDrag | null>(null);
  const sizes = fitSizes(layout, stage);
  const [writeError, setWriteError] = useState<string | null>(null);
  const active = workspace?.visuals.map((visual) => visual.type)
    ?? previewSelected ?? [catalog.data?.default_visual ?? "trax.browse"];
  const visible: WorkspaceState["visuals"] = workspace?.visuals ?? (active.length ? active : ["trax.browse"]).map((type) => ({
    id: type,
    type,
    version: catalog.data?.visuals.find((visual) => visual.type === type)?.version ?? 1,
    // The page stands in the strip of main visuals from the first render, before
    // the catalog or the canvas has arrived, so it never moves from the side.
    placement: type === "trax.browse" || catalog.data?.visuals.find((visual) => visual.type === type)?.default_size === "wide" ? "main" : "side",
    record_id: null,
    params: {},
  }));
  const visualTypes = useMemo(() => new Set(catalog.data?.visuals.map((visual) => visual.type)), [catalog.data]);
  const panes = orderVisuals(visible, workspace?.focused_instance ?? null);
  const browse = panes.find((pane) => pane.type === PAGE);
  /** Whether `pane` is drawn floating over the stage. */
  const floats = (pane: (typeof panes)[number]) => pane.placement === "floating";
  // Three columns, the left, the page's and the right, take the room Chat stands in.
  const threeColumns = SIDES.every(({ placement }) => panes.some((pane) => pane.placement === placement && !floats(pane)))
    && panes.some((pane) => (pane.placement ?? "main") === "main");
  useEffect(() => followColumns(threeColumns), [threeColumns]);
  /** Whether floating `pane` is folded to its bar, as it was left. */
  const folded = (pane: (typeof panes)[number]) => rememberedTile(layout, pane.type, stage).collapsed;
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

  // What decides a floating tile's size and place; the tiles are held inside the
  // stage again when it changes, or when the stage itself does. Each is held from
  // the place the layout means it to stand at, never from where an earlier hold
  // left it, so a tile a narrow stage pushed in returns when the stage grows back.
  const latestLayout = useRef<{ layout: Layout; rects: Record<string, { readonly left: number; readonly top: number } | null> }>({ layout, rects: {} });
  latestLayout.current = { layout, rects: {} };
  const floatingLayout = panes.filter(floats).map((pane) => {
    latestLayout.current.rects[pane.id] = pane.floating_rect ?? null;
    return [pane.id, layout.tiles?.[pane.type]?.place ?? null, pane.floating_rect ?? null, folded(pane)];
  });
  const floatingLayoutKey = JSON.stringify(floatingLayout);
  useLayoutEffect(() => {
    const stage = stageRef.current;
    if (!stage || floatingLayoutKey === "[]") return;
    const hold = () => {
      // A stage with no size is not shown; holding tiles inside it would pin them to its corner.
      if (stage.clientWidth === 0 || stage.clientHeight === 0) return;
      // On a phone the tile is a header in the flow, where a left or top only shifts it.
      if (window.innerWidth <= NARROW_VIEWPORT) return;
      for (const tile of stage.querySelectorAll<HTMLElement>(".visual-tile-floating")) {
        const { layout: now, rects } = latestLayout.current;
        const place = rememberedTile(now, tile.dataset.visualType ?? "", { width: stage.clientWidth, height: stage.clientHeight }).place
          ?? rects[tile.dataset.visualInstance ?? ""];
        if (!place) continue;
        tile.style.left = `${Math.min(Math.max(0, stage.clientWidth - tile.offsetWidth), Math.max(0, place.left))}px`;
        tile.style.top = `${Math.min(Math.max(0, stage.clientHeight - tile.offsetHeight), Math.max(0, place.top))}px`;
      }
    };
    hold();
    addEventListener("resize", hold);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(hold);
    observer?.observe(stage);
    return () => {
      removeEventListener("resize", hold);
      observer?.disconnect();
    };
  }, [floatingLayoutKey, stage.width, stage.height]);

  function acceptWorkspace(state: WorkspaceState) {
    cacheWorkspace(queryClient, state);
    if (latestWorkspace.current?.id === state.id) latestWorkspace.current = newerWorkspace(latestWorkspace.current, state);
  }

  useEffect(() => {
    if (!workspaceId) return;
    const timer = setTimeout(preloadRenderers, 0);
    return () => clearTimeout(timer);
  }, [workspaceId]);

  // The page, once closed, comes back with the user's own navigation: a link, an app
  // route, a changed hash. The server shows it again for an agent's.
  const pageClosed = workspace !== undefined && !workspace.visuals.some((visual) => visual.type === PAGE);
  const reopening = useRef(false);
  useEffect(() => {
    if (!pageClosed) return;
    const reopen = () => {
      if (reopening.current || latestWorkspace.current?.visuals.some((visual) => visual.type === PAGE)) return;
      reopening.current = true;
      void write({ kind: "show", visual_type: PAGE, placement: "main" }).finally(() => { reopening.current = false; });
    };
    addEventListener("hashchange", reopen);
    addEventListener("popstate", reopen);
    return () => {
      removeEventListener("hashchange", reopen);
      removeEventListener("popstate", reopen);
    };
  }, [pageClosed]);

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

  async function write(operation: WorkspaceOperation): Promise<boolean> {
    const id = latestWorkspace.current?.id;
    if (!id) return false;
    try {
      await change.mutateAsync({ id, operation });
      return true;
    } catch {
      return false;
    }
  }

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

  /** Remember how the floating tile of visual type `type` was left, in the station's layout. */
  function rememberTile(type: string, change: Partial<TileMemory>) {
    const measured = { width: stageRef.current?.clientWidth ?? 0, height: stageRef.current?.clientHeight ?? 0 };
    changeLayout((now) => withTile(now, type, change, measured) ?? {});
  }

  /** Where `tile` is drawn in the stage, which the stage's hold may have moved from the place last saved. */
  function drawnPlace(stage: HTMLElement, tile: HTMLElement) {
    const stageRect = stage.getBoundingClientRect();
    const tileRect = tile.getBoundingClientRect();
    return { left: tileRect.left - stageRect.left, top: tileRect.top - stageRect.top };
  }

  /**
   * Stand `pane` in the column at one side of the page, or in the main strip
   * beside it, at once: a floating tile docks there, a docked one moves.
   */
  function dockTile(pane: Pane, zone: Zone) {
    if (pane.placement !== zone) operate({ kind: "place", instance_id: pane.id, placement: zone });
  }

  /** Float docked `pane` over the stage at `place`. */
  function floatTile(pane: Pane, place: { readonly left: number; readonly top: number }) {
    rememberTile(pane.type, { place });
    operate({ kind: "place", instance_id: pane.id, placement: "floating" });
  }

  function focusTile(pane: Pane) {
    if (workspace?.focused_instance !== pane.id) operate({ kind: "focus", instance_id: pane.id });
  }

  function foldFloatingPane(type: string, collapsed: boolean) {
    rememberTile(type, { collapsed });
  }

  function moveFloatingPane(paneId: string, type: string, left: number, top: number) {
    const stage = stageRef.current;
    const tile = stage?.querySelector<HTMLElement>(`[data-visual-instance="${paneId}"]`);
    if (!stage || !tile) return;
    const maxLeft = Math.max(0, stage.clientWidth - tile.offsetWidth);
    const maxTop = Math.max(0, stage.clientHeight - tile.offsetHeight);
    rememberTile(type, {
      place: { left: Math.min(maxLeft, Math.max(0, left)), top: Math.min(maxTop, Math.max(0, top)) },
    });
  }

  /**
   * A press on a tile's top bar or its move handle. Pressing a control on the
   * bar starts nothing, and nor does a press on a phone, where the tiles stack
   * in one column. A floating tile follows the pointer itself; a docked one
   * stays where it stands, since its strip would clip it, and a card follows
   * the pointer in its place once the press has gone far enough to be a drag.
   */
  function startTileDrag(event: PointerEvent<HTMLElement>, pane: Pane, floating: boolean) {
    const stage = stageRef.current;
    const tile = event.currentTarget.closest<HTMLElement>(".visual-tile");
    const target = event.target;
    if (!stage || !tile || !(target instanceof Element)) return;
    // A drag whose tile has left the page never gets its release; it is not a drag in progress.
    if (dragRef.current?.tile.isConnected) return;
    if (event.button !== 0 || window.innerWidth <= NARROW_VIEWPORT) return;
    const byHandle = target.closest(".visual-tile-drag-handle") !== null;
    if (!byHandle && target.closest("button, select, input, textarea, a") !== null) return;
    const position = drawnPlace(stage, tile);
    // The bounds are measured once: read on every move, they lay the page out
    // again each time.
    dragRef.current = {
      pane, pointerId: event.pointerId, tile, byHandle, floating, zone: null,
      stage: { left: stage.getBoundingClientRect().left, top: stage.getBoundingClientRect().top, width: stage.clientWidth },
      title: event.currentTarget.querySelector("span")?.textContent ?? "",
      gesture: startGesture({
        x: event.clientX, y: event.clientY, left: position.left, top: position.top,
        maxLeft: Math.max(0, stage.clientWidth - tile.offsetWidth),
        maxTop: Math.max(0, stage.clientHeight - tile.offsetHeight),
      }),
    };
    if (floating) tile.style.willChange = "transform";
    target.setPointerCapture(event.pointerId);
  }

  /**
   * Each move only transforms the tile, or the card that stands for a docked
   * one, and marks where it would dock: rendering the canvas per move redrew
   * every visual in it. The canvas takes the outcome once, on release.
   */
  function updateTileDrag(event: PointerEvent<HTMLElement>) {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    moveGesture(drag.gesture, event.clientX, event.clientY);
    const at = { x: event.clientX - drag.stage.left, y: event.clientY - drag.stage.top };
    if (drag.floating) {
      drag.tile.style.transform = `translate(${drag.gesture.dx}px, ${drag.gesture.dy}px)`;
    } else if (drag.gesture.peak > TEAR_SLOP && ghostRef.current) {
      drag.tile.classList.add("visual-tile-lifted");
      ghostRef.current.textContent = drag.title;
      Object.assign(ghostRef.current.style, { display: "block", transform: `translate(${at.x - GHOST_GRIP.x}px, ${at.y - GHOST_GRIP.y}px)` });
    }
    // The move handle only moves a floating tile: it never docks it.
    const dragging = drag.gesture.peak > (drag.floating ? CLICK_SLOP : TEAR_SLOP) && !drag.byHandle;
    drag.zone = dragging ? snapZone(at.x, at.y, drag.stage.width) : null;
    if (snapRef.current) snapRef.current.dataset.zone = drag.zone ?? "";
  }

  /** Take away what a drag drew over the stage. */
  function endDragMarks(drag: TileDrag) {
    drag.tile.style.willChange = "";
    drag.tile.classList.remove("visual-tile-lifted");
    if (ghostRef.current) ghostRef.current.style.display = "none";
    if (snapRef.current) snapRef.current.dataset.zone = "";
  }

  /**
   * The release of a press. One that stayed within the slop is a click, which
   * focuses a tile pressed on its bar. A drag dropped at an edge of the stage
   * docks the tile there; dropped anywhere else it leaves a floating tile
   * where the pointer took it, and floats a docked one there.
   */
  function stopTileDrag(event: PointerEvent<HTMLElement>) {
    const drag = dragRef.current;
    if (drag?.pointerId !== event.pointerId) return;
    dragRef.current = null;
    endDragMarks(drag);
    const outcome = finishGesture(drag.gesture, drag.floating ? CLICK_SLOP : TEAR_SLOP);
    if (outcome.kind === "click") {
      drag.tile.style.transform = "";
      if (!drag.byHandle) focusTile(drag.pane);
    } else if (drag.zone) {
      drag.tile.style.transform = "";
      dockTile(drag.pane, drag.zone);
    } else if (drag.floating) {
      // Placed before the transform goes, so no frame shows the tile back where it started.
      Object.assign(drag.tile.style, { left: `${outcome.place.left}px`, top: `${outcome.place.top}px`, right: "auto", transform: "" });
      rememberTile(drag.pane.type, { place: outcome.place });
    } else {
      // Under the pointer as the card was; the stage's hold keeps the tile inside it.
      floatTile(drag.pane, {
        left: Math.max(0, event.clientX - drag.stage.left - GHOST_GRIP.x),
        top: Math.max(0, event.clientY - drag.stage.top - GHOST_GRIP.y),
      });
    }
  }

  /** The browser took the pointer (a touch became a scroll), or the captured bar left the page: the tile goes back. */
  function cancelTileDrag(event: PointerEvent<HTMLElement>) {
    const drag = dragRef.current;
    if (drag?.pointerId !== event.pointerId) return;
    dragRef.current = null;
    endDragMarks(drag);
    drag.tile.style.transform = "";
  }

  /** The column at `side` of the page, as the stage holds it now. */
  function columnAt(side: Side): HTMLElement | null {
    return stageRef.current?.querySelector<HTMLElement>(side === "left" ? ".visual-side-column-left" : ".visual-side-column:not(.visual-side-column-left)") ?? null;
  }

  /** Keep a column's width as a fraction of the stage, in the station's layout. */
  function keepColumnWidth(side: Side, width: number) {
    const fraction = toFraction(width, stageRef.current?.clientWidth ?? 0);
    if (fraction !== null) changeLayout((now) => ({ columns: { ...now.columns, [side]: fraction } }));
  }

  /** A press on an edge or a corner of a floating window: the sides it names follow the pointer, inside the stage. */
  function startWindowResize(event: PointerEvent<HTMLElement>, pane: Pane, edge: Edge) {
    const stage = stageRef.current;
    const tile = event.currentTarget.closest<HTMLElement>(".visual-tile");
    if (!stage || !tile || event.button !== 0) return;
    const rect = { ...drawnPlace(stage, tile), width: tile.offsetWidth, height: tile.offsetHeight };
    windowRef.current = {
      pane, edge, pointerId: event.pointerId, tile, x: event.clientX, y: event.clientY, start: rect, rect,
      bounds: { width: stage.clientWidth, height: stage.clientHeight },
    };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
    event.stopPropagation();
  }

  /** As a tile's drag, each move only sets the window's own box; the canvas takes it once, on release. */
  function updateWindowResize(event: PointerEvent<HTMLElement>) {
    const resize = windowRef.current;
    if (!resize || resize.pointerId !== event.pointerId) return;
    resize.rect = resizeRect(resize.start, resize.edge, event.clientX - resize.x, event.clientY - resize.y, MIN_FLOAT, resize.bounds);
    const { left, top, width, height } = resize.rect;
    Object.assign(resize.tile.style, { left: `${left}px`, top: `${top}px`, right: "auto", width: `${width}px`, height: `${height}px` });
  }

  function stopWindowResize(event: PointerEvent<HTMLElement>) {
    const resize = windowRef.current;
    if (resize?.pointerId !== event.pointerId) return;
    windowRef.current = null;
    const { left, top, width, height } = resize.rect;
    rememberTile(resize.pane.type, { place: { left, top } });
    const stageWidth = stageRef.current?.clientWidth ?? 0;
    const stageHeight = stageRef.current?.clientHeight ?? 0;
    const [wide, high] = [toFraction(width, stageWidth), toFraction(height, stageHeight)];
    if (wide !== null && high !== null) changeLayout((now) => ({ floating: { ...now.floating, [resize.pane.type]: { width: wide, height: high } } }));
  }

  /**
   * A press on the divider between two docked tiles: across the strip (`x`) or
   * down the column (`y`). Every tile in the flow of that strip is measured
   * once, so the two beside the divider can trade room while the rest hold.
   */
  function startDivider(event: PointerEvent<HTMLElement>, axis: "x" | "y") {
    const strip = event.currentTarget.parentElement;
    if (!strip || event.button !== 0) return;
    const tiles = [...strip.children].filter((child): child is HTMLElement =>
      child instanceof HTMLElement && child.classList.contains("visual-tile") && !child.classList.contains("visual-tile-floating"));
    let before = event.currentTarget.previousElementSibling;
    while (before && !tiles.includes(before as HTMLElement)) before = before.previousElementSibling;
    const at = tiles.indexOf(before as HTMLElement);
    if (at < 0 || at + 1 >= tiles.length) return;
    const px = (tile: HTMLElement, ...names: ("minWidth" | "minHeight" | "borderLeftWidth" | "borderRightWidth" | "borderTopWidth" | "borderBottomWidth")[]) =>
      names.reduce((sum, name) => sum + (Number.parseFloat(getComputedStyle(tile)[name]) || 0), 0);
    // As laid out, fractions included: whole pixels would not add up to the strip, and the shares would drift by one.
    const measured = tiles.map((tile) => tile.getBoundingClientRect()[axis === "x" ? "width" : "height"]);
    dividerRef.current = {
      axis, pointerId: event.pointerId, tiles, at, start: axis === "x" ? event.clientX : event.clientY,
      measured, now: measured,
      least: [px(tiles[at]!, axis === "x" ? "minWidth" : "minHeight"), px(tiles[at + 1]!, axis === "x" ? "minWidth" : "minHeight")],
      // The rule a tile draws between itself and its neighbour is not room the strip shares out.
      ruled: tiles.map((tile) => (axis === "x" ? px(tile, "borderLeftWidth", "borderRightWidth") : px(tile, "borderTopWidth", "borderBottomWidth"))),
    };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  }

  /** Each move only sets the tiles' shares of the strip, as their sizes in pixels; the canvas takes them once, on release. */
  function updateDivider(event: PointerEvent<HTMLElement>) {
    const drag = dividerRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const delta = (drag.axis === "x" ? event.clientX : event.clientY) - drag.start;
    const [a, b] = slideDivider(drag.measured[drag.at]!, drag.measured[drag.at + 1]!, delta, drag.least[0], drag.least[1]);
    drag.now = drag.measured.map((size, index) => (index === drag.at ? a : index === drag.at + 1 ? b : size));
    for (const [index, tile] of drag.tiles.entries()) {
      tile.style.setProperty("--tile-share", String(drag.now[index]! - drag.ruled[index]!));
      tile.style.setProperty("--tile-basis", "0px");
    }
  }

  function stopDivider(event: PointerEvent<HTMLElement>) {
    const drag = dividerRef.current;
    if (drag?.pointerId !== event.pointerId) return;
    dividerRef.current = null;
    // Kept as a share of the strip, an even one being 1, so a window of another size divides the same way.
    const room = drag.now.map((size, index) => size - drag.ruled[index]!);
    const even = room.reduce((sum, size) => sum + size, 0) / room.length || 1;
    const shares = Object.fromEntries(drag.tiles.map((tile, index) => [tile.dataset.visualType ?? "", Math.round((room[index]! / even) * 10_000) / 10_000]));
    changeLayout((now) => ({ share: { ...now.share, ...shares } }));
  }

  /** A press on the edge between a column and the page: the column takes the width the pointer drags that edge to. */
  function startColumnResize(event: PointerEvent<HTMLElement>, side: Side) {
    const stage = stageRef.current;
    const column = columnAt(side);
    if (!stage || !column || event.button !== 0) return;
    resizeRef.current = { side, pointerId: event.pointerId, column, stage: stage.getBoundingClientRect(), width: column.offsetWidth };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  }

  /** As a tile's drag, each move only sets the column's width; the canvas takes it once, on release. */
  function updateColumnResize(event: PointerEvent<HTMLElement>) {
    const resize = resizeRef.current;
    if (!resize || resize.pointerId !== event.pointerId) return;
    const dragged = resize.side === "left" ? event.clientX - resize.stage.left : resize.stage.right - event.clientX;
    resize.width = heldWidth(dragged, resize.stage.width);
    resize.column.style.setProperty("--column-width", `${resize.width}px`);
  }

  function stopColumnResize(event: PointerEvent<HTMLElement>) {
    const resize = resizeRef.current;
    if (resize?.pointerId !== event.pointerId) return;
    resizeRef.current = null;
    keepColumnWidth(resize.side, resize.width);
  }

  /** The arrow keys move the edge 16 px, 48 with Shift, toward the side the arrow points. */
  function resizeWithKeyboard(event: KeyboardEvent<HTMLElement>, side: Side) {
    const toward = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
    const stage = stageRef.current;
    const column = columnAt(side);
    if (!toward || !stage || !column) return;
    event.preventDefault();
    const wider = side === "left" ? toward : -toward;
    keepColumnWidth(side, heldWidth(column.offsetWidth + wider * (event.shiftKey ? 48 : 16), stage.clientWidth));
  }

  function moveWithKeyboard(event: KeyboardEvent<HTMLButtonElement>, paneId: string, type: string) {
    const directions = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] } as const;
    const direction = directions[event.key as keyof typeof directions];
    if (!direction) return;
    event.preventDefault();
    const stage = stageRef.current;
    const tile = event.currentTarget.closest<HTMLElement>(".visual-tile");
    if (!stage || !tile) return;
    const position = drawnPlace(stage, tile);
    const step = event.shiftKey ? 48 : 16;
    moveFloatingPane(paneId, type, position.left + direction[0] * step, position.top + direction[1] * step);
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

  const renderTile = (pane: (typeof panes)[number], index: number) => {
    const floating = floats(pane);
    const isPage = pane.type === "trax.browse";
    const title = catalog.data?.visuals.find((visual) => visual.type === pane.type)?.title ?? pane.type;
    const collapsed = floating && folded(pane);
    const at = rememberedTile(layout, pane.type, stage).place ?? pane.floating_rect ?? null;
    const size = sizes.floating[pane.type] ?? pane.floating_rect;
    const share = sizes.share[pane.type];
    return (
    <div className={`visual-tile visual-tile-${floating ? "floating" : pane.placement ?? "main"}${pane.type === "trax.browse" ? " visual-tile-browse" : ""}${workspace?.focused_instance === pane.id ? " visual-tile-focused" : ""}${expandedMobileFloat === pane.id ? " visual-tile-mobile-expanded" : ""}${collapsed ? " visual-tile-collapsed" : ""}`}
      key={pane.type === "trax.browse" ? pane.type : pane.id}
      ref={isPage ? browseRef : undefined}
      data-visual-instance={pane.id}
      data-visual-type={pane.type}
      style={floating ? {
        top: `${at?.top ?? 54 + index * 24}px`,
        ...(at ? { left: `${at.left}px`, right: "auto" } : {}),
        ...(size ? { width: `${size.width}px`, height: `${size.height}px` } : {}),
        zIndex: 10 + index,
      } : isPage ? {
        position: "fixed", left: 0, top: 0, right: "auto", width: "100%", height: "100%", clipPath: "inset(0px)",
      } : share ? { "--tile-share": share, "--tile-basis": "0px" } as CSSProperties : undefined}>
      {/* A floating window resizes by any edge or corner, as a desktop's does; folded to its bar it has no size to give. */}
      {floating && !collapsed && EDGES.map((edge) => <div key={edge} className={`visual-window-edge visual-window-edge-${edge}`} aria-hidden="true"
        onPointerDown={(event) => startWindowResize(event, pane, edge)} onPointerMove={updateWindowResize}
        onPointerUp={stopWindowResize} onPointerCancel={stopWindowResize} />)}
      {/* A press on the bar focuses the tile; a drag floats it, or docks it at the edge it is dropped on. The page stays the page: docked, its bar only focuses. */}
      {workspace && (panes.length > 1 || floating) && <div
        className={`visual-tile-toolbar${floating || !isPage ? " visual-tile-toolbar-draggable" : ""}`}
        {...(floating || !isPage ? {
          onPointerDown: (event: PointerEvent<HTMLElement>) => startTileDrag(event, pane, floating),
          onPointerMove: updateTileDrag, onPointerUp: stopTileDrag, onPointerCancel: cancelTileDrag,
          onLostPointerCapture: cancelTileDrag,
        } : { onClick: (event: MouseEvent<HTMLElement>) => { if (!(event.target instanceof Element && event.target.closest("button"))) focusTile(pane); } })}>
        {floating && <button className="visual-tile-drag-handle" type="button"
          aria-label={`Move ${title}`}
          title="Drag to move; use arrow keys to move"
          onKeyDown={(event) => moveWithKeyboard(event, pane.id, pane.type)}>⠿</button>}
        <span>{title}</span>
        {floating && <button className="visual-tile-fold" type="button" aria-expanded={!collapsed}
          aria-label={`${collapsed ? "Expand" : "Collapse"} ${title}`}
          title={collapsed ? "Show the whole tile" : "Fold the tile to its bar"}
          onClick={() => foldFloatingPane(pane.type, !collapsed)}>{collapsed ? "▸" : "▾"}</button>}
        {!isPage && SIDES.map((side) => <button key={side.placement} type="button"
          aria-label={`Dock ${title} at the ${side.name}`} title={`Dock at the ${side.name} of the page`}
          disabled={change.isPending || (!floating && pane.placement === side.placement)}
          onClick={() => dockTile(pane, side.placement)}>{side.glyph}</button>)}
        {floating && <button className="visual-mobile-tab-toggle" type="button"
          aria-expanded={expandedMobileFloat === pane.id}
          onClick={() => setExpandedMobileFloat(expandedMobileFloat === pane.id ? null : pane.id)}>
          {expandedMobileFloat === pane.id ? "Collapse" : "Expand"}
        </button>}
        <button type="button" title="Dismiss visual" disabled={change.isPending}
          onClick={() => operate({ kind: "hide", instance_id: pane.id })}>×</button>
      </div>}
      <VisualPane instance={pane} workspace={workspace ?? null} onWorkspaceChanged={acceptWorkspace}
        focused={workspace?.focused_instance === pane.id}>
        {isPage && <div className="visual-browse-content">
          <CrashBoundary reload={() => window.location.reload()} resetKey={window.location.hash}
            frame={(crash) => <div className="view">{crash}</div>}>{children}</CrashBoundary>
        </div>}
      </VisualPane>
    </div>
    );
  };

  /**
   * The tiles standing at `placement`, with a divider between each two that
   * share its flow: across the main strip (`x`) or down a column (`y`). A
   * floating tile keeps its place among them and takes none of the flow.
   */
  const strip = (placement: "main" | Side, axis: "x" | "y") => {
    let flowing = 0;
    return panes.flatMap((pane, index) => {
      if ((pane.placement ?? "main") !== placement) return [];
      const share = sizes.share[pane.type];
      const tile = pane.type === PAGE ? <div key="browse-slot" ref={browseSlotRef} className="visual-tile visual-browse-slot"
        data-visual-type={PAGE}
        style={share ? { "--tile-share": share, "--tile-basis": "0px" } as CSSProperties : undefined} /> : renderTile(pane, index);
      if (floats(pane)) return [tile];
      flowing += 1;
      return flowing === 1 ? [tile] : [
        <div key={`divider-${pane.type === "trax.browse" ? pane.type : pane.id}`} className={`visual-divider visual-divider-${axis}`}
          role="separator" aria-orientation={axis === "x" ? "vertical" : "horizontal"} title="Drag to resize"
          onPointerDown={(event) => startDivider(event, axis)} onPointerMove={updateDivider}
          onPointerUp={stopDivider} onPointerCancel={stopDivider} />,
        tile,
      ];
    });
  };

  /** The column at `side` and, while it holds a tile in the flow, the edge between it and the page that resizes it. */
  const sideColumn = (side: Side) => {
    if (!panes.some((pane) => pane.placement === side)) return null;
    const held = panes.some((pane) => pane.placement === side && !floats(pane));
    const width = sizes[side];
    const column = (
      <div key="column" className={`visual-side-column${side === "left" ? " visual-side-column-left" : ""}${held ? "" : " visual-side-column-vacant"}`}
        style={held && width ? { "--column-width": `${width}px` } as CSSProperties : undefined}>
        {strip(side, "y")}
      </div>
    );
    const name = SIDES.find((each) => each.placement === side)!.name;
    const edge = held && (
      <div key="edge" className="visual-column-edge" role="separator" aria-orientation="vertical" tabIndex={0}
        aria-label={`Resize the ${name} column`} aria-valuemin={MIN_COLUMN} aria-valuenow={width ?? DEFAULT_COLUMN}
        title="Drag to resize; use the arrow keys to resize"
        onPointerDown={(event) => startColumnResize(event, side)} onPointerMove={updateColumnResize}
        onPointerUp={stopColumnResize} onPointerCancel={stopColumnResize}
        onKeyDown={(event) => resizeWithKeyboard(event, side)} />
    );
    return side === "left" ? [column, edge] : [edge, column];
  };

  return (
    <WorkspaceActionsProvider value={workspace ? {
      busy: change.isPending,
      writeError,
      operate,
      visualTypes,
    } : null}>
    <div className="visual-canvas">
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
      <div ref={stageRef} className={`visual-stage${panes.length > 1 ? " visual-stage-split" : ""}`}>
        {/* The page and the other main visuals share a strip that scrolls inside itself when they outgrow it; the visuals docked at a side stand in one column there; floating ones lie over all three. */}
        {sideColumn("left")}
        {panes.some((pane) => (pane.placement ?? "main") === "main") && (
          <div className="visual-main-strip">
            {strip("main", "x")}
          </div>
        )}
        {sideColumn("side")}
        {panes.map((pane, index) => pane.placement === "floating" && pane.type !== PAGE ? renderTile(pane, index) : null)}
        {browse && renderTile(browse, panes.indexOf(browse))}
        <div ref={snapRef} className="visual-snap" aria-hidden="true" />
        <div ref={ghostRef} className="visual-drag-ghost" aria-hidden="true" />
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

/** A visual as the canvas draws it: what the drag and dock functions are handed. */
type Pane = { readonly id: string; readonly type: string; readonly placement?: "main" | "left" | "side" | "floating" };

type Side = (typeof SIDES)[number]["placement"];

/**
 * A tile being pressed on by its bar: the tile, where the stage stood then, and
 * the gesture (the pointer's start, the tile's place then, how far it may go,
 * and how far it has moved). A floating tile takes the gesture as a transform
 * until release; a docked one stays, and a card follows the pointer instead.
 */
type TileDrag = {
  readonly pane: Pane;
  readonly pointerId: number;
  readonly tile: HTMLElement;
  /** Pressed on the move handle, which only moves the tile: it neither focuses nor docks it. */
  readonly byHandle: boolean;
  /** Whether the tile floated when pressed. */
  readonly floating: boolean;
  /** Where the stage stood when pressed, and how wide it was. */
  readonly stage: { readonly left: number; readonly top: number; readonly width: number };
  /** The tile's name, which the card of a docked tile says. */
  readonly title: string;
  readonly gesture: Gesture;
  /** Where the tile would dock if dropped now. */
  zone: Zone | null;
};

/** A column's edge being dragged: the column, where the stage stood then, and the width it has now. */
type ColumnResize = {
  readonly side: Side;
  readonly pointerId: number;
  readonly column: HTMLElement;
  readonly stage: DOMRect;
  width: number;
};

/** A floating window being resized by an edge or a corner: where the press began, the window's box then and now, and the stage it must stay in. */
type WindowResize = {
  readonly pane: Pane;
  readonly edge: Edge;
  readonly pointerId: number;
  readonly tile: HTMLElement;
  readonly x: number;
  readonly y: number;
  readonly start: Rect;
  readonly bounds: { readonly width: number; readonly height: number };
  rect: Rect;
};

/** The divider between two docked tiles being dragged: the strip's tiles in the flow, their sizes at the press and now, and the least each neighbour takes. */
type DividerDrag = {
  readonly axis: "x" | "y";
  readonly pointerId: number;
  readonly tiles: readonly HTMLElement[];
  /** The tile before the divider, in `tiles`. */
  readonly at: number;
  readonly start: number;
  readonly measured: readonly number[];
  readonly least: readonly [number, number];
  /** How much of each tile's size is its own rule, which it keeps whatever its share. */
  readonly ruled: readonly number[];
  now: readonly number[];
};

/** The page: the catalog's default visual, which a canvas opens with and a navigation moves. */
const PAGE = "trax.browse";

/** Where on the card that stands for a docked tile the pointer holds it, and so where the floating tile lands under it. */
const GHOST_GRIP = { x: 60, y: 16 };

/** What a column never resized is told to assistive tech: the 360 px its CSS gives it at most. */
const DEFAULT_COLUMN = 360;

/** The canvas stacks its tiles in one column at this width and below (see canvas.css), floating ones included. */
const NARROW_VIEWPORT = 900;
