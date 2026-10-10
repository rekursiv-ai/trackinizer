import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type FormEvent, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import { ApiError } from "../api/client";
import { findRef } from "../api/detail";
import { newUuid } from "../api/idempotency";
import {
  createWorkspacePreset,
  listWorkspacePresets,
  openWorkspacePreset,
  type FloatingRect,
  type WorkspacePreset,
} from "../api/presets";
import { getVisualCatalog, type VisualDescription } from "../api/visuals";
import {
  applyWorkspaceOperation,
  createDefaultWorkspace,
  getWorkspace,
  type WorkspaceOperation,
  type WorkspaceState,
} from "../api/workspaces";
import { acceptWorkspace as cacheWorkspace, newerWorkspace } from "../app/canvasStream";
import { CrashBoundary } from "../app/CrashBoundary";
import { parseHash } from "../router/route";
import { useBrowserState } from "../state/store";
import type { TileMemory } from "../state/value";
import { type PanelSpec, usePanel } from "../ui/panel";
import { useChatFeed } from "./chatFeed";
import { type CanvasSizes, heldWidth, MIN_COLUMN, MIN_FLOAT, readCanvasSizes, writeCanvasSizes } from "./canvasSizes";
import { CLICK_SLOP, EDGES, finishGesture, moveGesture, rememberedTile, resizeRect, slideDivider, snapZone, startGesture, TEAR_SLOP, withoutPlaces, withTile, type Edge, type Gesture, type Rect, type Zone } from "./floatingTile";
import { useHoverOpen } from "./hoverOpen";
import { chatHome, orderVisuals, SIDES } from "./layout";
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
  const created = useQuery({
    queryKey: ["workspace", "default"],
    queryFn: ({ signal }) => createDefaultWorkspace({ signal }),
    staleTime: Infinity,
    retry: false,
  });
  const workspaceId = created.data?.id;
  // The canvas is what the shell's stream last said (its opening frame is the
  // first state) and what this canvas's own writes returned. This query holds
  // that cache entry and reads only when asked to, after a refused write.
  const remote = useQuery({
    queryKey: ["workspace", workspaceId],
    queryFn: async ({ signal }) => {
      const incoming = await getWorkspace(workspaceId!, { signal });
      return newerWorkspace(queryClient.getQueryData<WorkspaceState>(["workspace", workspaceId]), incoming);
    },
    enabled: false,
    staleTime: Infinity,
    retry: false,
  });
  const workspace = remote.data ? newerWorkspace(created.data, remote.data) : created.data;
  const latestWorkspace = useRef(workspace);
  latestWorkspace.current = workspace;
  const legacyPreview = import.meta.env.DEV && created.error instanceof ApiError && created.error.status === 404;
  const [previewSelected, setPreviewSelected] = useState<string[] | null>(null);
  const configure = usePanel(CONFIGURE);
  const [artifactLink, setArtifactLink] = useState("");
  const [artifactLinkError, setArtifactLinkError] = useState<string | null>(null);
  const syncedPresetWorkspace = useRef<{ readonly id: string; readonly revision: number; readonly instructions: string | null; readonly recordId: string | null } | null>(null);
  const instructionsDirty = useRef(false);
  const recordIdDirty = useRef(false);
  const saveAttempt = useRef<{ readonly signature: string; readonly key: string } | null>(null);
  const openAttempt = useRef<{ readonly presetId: string; readonly workspaceId: string; readonly revision: number; readonly key: string } | null>(null);
  const [presetName, setPresetName] = useState("");
  const [presetInstructions, setPresetInstructions] = useState("");
  const [continuationRecordId, setContinuationRecordId] = useState("");
  const [presetError, setPresetError] = useState<string | null>(null);
  const [presetStatus, setPresetStatus] = useState<string | null>(null);
  const [expandedMobileFloat, setExpandedMobileFloat] = useState<string | null>(null);
  // Where floating tiles stand and whether they are folded, as this session left
  // them: a tile by its instance id, a fold by its visual type. The browser's
  // state remembers both across loads; these hold when it cannot store.
  const [floatingPositions, setFloatingPositions] = useState<Record<string, { readonly left: number; readonly top: number }>>({});
  const [foldedTypes, setFoldedTypes] = useState<Record<string, boolean>>({});
  const [browser, updateBrowser] = useBrowserState();
  const stageRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<TileDrag | null>(null);
  // What a drag draws over the stage: where the tile would dock, and, for a
  // docked tile, a card that follows the pointer in its place.
  const snapRef = useRef<HTMLDivElement>(null);
  const ghostRef = useRef<HTMLDivElement>(null);
  // The sizes dragged in this browser, and the drags that set them: a column's
  // edge, a floating window's edge or corner, the divider between two docked tiles.
  const resizeRef = useRef<ColumnResize | null>(null);
  const windowRef = useRef<WindowResize | null>(null);
  const dividerRef = useRef<DividerDrag | null>(null);
  const [sizes, setSizes] = useState<CanvasSizes>(readCanvasSizes);
  // Chat the user dragged out of its column stays open under the pointer that dropped it.
  const tornOff = useRef(false);
  // Chat's home is a panel beside the page. While the assistant shows something
  // it stands aside: the same tile floats over the page, folded to its bar unless
  // the pointer or the keyboard is in it. None of that is the canvas's state.
  const { feed, state: chatFeed } = useChatFeed();
  const chatAside = chatFeed.aside > 0;
  const chatTile = useRef<HTMLDivElement>(null);
  const chatOpen = useHoverOpen(chatTile);
  const [writeError, setWriteError] = useState<string | null>(null);
  const presets = useQuery({
    queryKey: ["workspace-presets", workspaceId],
    queryFn: ({ signal }) => listWorkspacePresets({ signal }),
    enabled: !configure.collapsed && !!workspaceId,
    retry: false,
  });
  const active = workspace?.visuals.map((visual) => visual.type)
    ?? previewSelected ?? [catalog.data?.default_visual ?? "trax.browse"];
  const visible: WorkspaceState["visuals"] = workspace?.visuals ?? (active.length ? active : ["trax.chat"]).map((type) => ({
    id: type,
    type,
    version: catalog.data?.visuals.find((visual) => visual.type === type)?.version ?? 1,
    // The page stands in the strip of main visuals from the first render, before
    // the catalog or the canvas has arrived, so it never moves from the side.
    placement: type === "trax.browse" || catalog.data?.visuals.find((visual) => visual.type === type)?.default_size === "wide" ? "main" : "side",
    record_id: null,
    params: {},
  }));
  const chat = catalog.data?.visuals.find((visual) => visual.type === "trax.chat");
  const visualTypes = useMemo(() => new Set(catalog.data?.visuals.map((visual) => visual.type)), [catalog.data]);
  const panes = orderVisuals((visible.length ? visible : [{
    id: "chat-disconnected", type: "trax.chat", version: chat?.version ?? 1,
    placement: "main" as const, record_id: null, params: {},
  }]).map((visual) => visual.type === "trax.chat" ? { ...visual, placement: chatHome(visual.placement) } : visual),
  workspace?.focused_instance ?? null);
  /** Whether `pane` is drawn floating over the stage: placed so, or Chat standing aside. */
  const floats = (pane: (typeof panes)[number]) => pane.placement === "floating" || (pane.type === "trax.chat" && chatAside);
  /** Whether floating `pane` is folded to its bar: Chat by where the pointer and the keyboard are, any other as it was left. */
  const folded = (pane: (typeof panes)[number]) => pane.type === "trax.chat"
    ? !chatOpen.open : foldedTypes[pane.type] ?? rememberedTile(browser, pane.type).collapsed;
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
  const focusedVisual = workspace?.visuals.find((visual) => visual.id === workspace.focused_instance);
  const chatRecordId = focusedVisual?.type === "trax.artifact" && focusedVisual.record_id
    ? focusedVisual.record_id : currentRecordId;

  // What decides a floating tile's size and place; the tiles are held inside the
  // stage again when it changes, or when the stage itself does. Each is held from
  // the place it is meant to stand at, never from where an earlier hold left it, so
  // a tile a narrow stage pushed in returns when the stage grows back.
  const intendedPlaces: Record<string, { readonly left: number; readonly top: number } | null> = {};
  const floatingLayout = panes.filter(floats).map((pane) => {
    const place = floatingPositions[pane.id] ?? rememberedTile(browser, pane.type).place ?? pane.floating_rect ?? null;
    intendedPlaces[pane.id] = place && { left: place.left, top: place.top };
    return [pane.id, intendedPlaces[pane.id], folded(pane)];
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
        const place = intendedPlaces[tile.dataset.visualInstance ?? ""];
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
  }, [floatingLayoutKey]);

  // Each time the assistant shows something, Chat folds out of its way at once,
  // from under the pointer and the keyboard too.
  useEffect(() => {
    if (chatFeed.aside === 0) return;
    if (tornOff.current) chatOpen.enter();
    else chatOpen.set(false);
    tornOff.current = false;
    setExpandedMobileFloat(null);
  }, [chatFeed.aside]);

  useEffect(() => {
    if (!workspace) return;
    const previous = syncedPresetWorkspace.current;
    if (previous?.id === workspace.id && previous.revision === workspace.revision
      && previous.instructions === (workspace.agent_instructions ?? null)
      && previous.recordId === (workspace.continuation_record_id ?? null)) return;
    if (previous?.id !== workspace.id) {
      instructionsDirty.current = false;
      recordIdDirty.current = false;
    }
    if (!instructionsDirty.current) setPresetInstructions(workspace.agent_instructions ?? "");
    if (!recordIdDirty.current) setContinuationRecordId(workspace.continuation_record_id ?? "");
    syncedPresetWorkspace.current = {
      id: workspace.id, revision: workspace.revision,
      instructions: workspace.agent_instructions ?? null,
      recordId: workspace.continuation_record_id ?? null,
    };
  }, [workspace]);

  function acceptWorkspace(state: WorkspaceState) {
    latestWorkspace.current = newerWorkspace(latestWorkspace.current, state);
    cacheWorkspace(queryClient, state);
  }

  useEffect(() => {
    if (!workspaceId) return;
    const timer = setTimeout(preloadRenderers, 0);
    return () => clearTimeout(timer);
  }, [workspaceId]);

  const change = useMutation({
    scope: { id: `workspace:${workspaceId ?? "default"}` },
    mutationFn: async (operation: WorkspaceOperation) => {
      const current = latestWorkspace.current;
      if (!current) throw new Error("The workspace is not ready.");
      return applyWorkspaceOperation(current.id, current.revision, operation, newUuid());
    },
    onMutate: () => setWriteError(null),
    onSuccess: acceptWorkspace,
    onError: () => {
      setWriteError("Could not update the canvas. Its revision may have changed; try again.");
      void remote.refetch();
    },
  });

  const savePreset = useMutation({
    mutationFn: async () => {
      const current = latestWorkspace.current;
      if (!current) throw new Error("The workspace is not ready.");
      const payload = {
        name: presetName.trim(),
        agentInstructions: presetInstructions.trim() || null,
        continuationRecordId: continuationRecordId.trim() || currentRecordId || null,
        floatingRects: readFloatingRects(current, stageRef.current),
      };
      const signature = JSON.stringify([current.id, current.revision, payload]);
      const key = saveAttempt.current?.signature === signature ? saveAttempt.current.key : newUuid();
      saveAttempt.current = { signature, key };
      return createWorkspacePreset(current.id, current.revision, payload, key);
    },
    onMutate: () => {
      setPresetError(null);
      setPresetStatus(null);
    },
    onSuccess: async (preset) => {
      saveAttempt.current = null;
      instructionsDirty.current = false;
      recordIdDirty.current = false;
      setPresetName("");
      setPresetStatus(`Saved “${preset.name}”.`);
      await queryClient.invalidateQueries({ queryKey: ["workspace-presets", workspaceId] });
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) saveAttempt.current = null;
      setPresetError(`Could not save this canvas. ${errorText(error)}`);
    },
  });

  const openPreset = useMutation({
    scope: { id: `workspace:${workspaceId ?? "default"}` },
    mutationFn: async (preset: WorkspacePreset) => {
      const current = latestWorkspace.current;
      if (!current) throw new Error("The workspace is not ready.");
      const previous = openAttempt.current;
      const attempt = previous?.presetId === preset.id && previous.workspaceId === current.id
        ? previous
        : { presetId: preset.id, workspaceId: current.id, revision: current.revision, key: newUuid() };
      openAttempt.current = attempt;
      return { state: await openWorkspacePreset(preset.id, attempt.workspaceId, attempt.revision, attempt.key), preset };
    },
    onMutate: () => {
      setPresetError(null);
      setPresetStatus(null);
    },
    onSuccess: ({ state, preset }) => {
      openAttempt.current = null;
      instructionsDirty.current = false;
      recordIdDirty.current = false;
      acceptWorkspace(state);
      setPresetInstructions(state.agent_instructions ?? "");
      setContinuationRecordId(state.continuation_record_id ?? "");
      setFloatingPositions({});
      keepSizes((previous) => ({ ...previous, floating: {} }));
      // A saved view's places and sizes show over the ones dragged before it.
      try {
        updateBrowser(withoutPlaces);
      } catch {
        // Storage is off, full or holds a value this build cannot read; the session already forgot them.
      }
      setPresetStatus(`Opened “${preset.name}”.`);
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) openAttempt.current = null;
      setPresetError(`Could not open this saved view. ${errorText(error)}`);
      void remote.refetch();
    },
  });

  async function write(operation: WorkspaceOperation): Promise<boolean> {
    if (!latestWorkspace.current) return false;
    try {
      await change.mutateAsync(operation);
      return true;
    } catch {
      return false;
    }
  }

  /**
   * Show Chat docked beside the page, about the record the page shows if any.
   *
   * A Chat already about that record, or about none on a page without one, is
   * only focused; one about another record moves to this page's. A new Chat
   * takes the side, where the server places it.
   */
  function showChat() {
    feed.dock();
    const existing = workspace?.visuals.find((visual) => visual.type === "trax.chat");
    operate(existing && (existing.record_id ?? null) === chatRecordId
      ? { kind: "focus", instance_id: existing.id }
      : { kind: "show", visual_type: "trax.chat", record_id: chatRecordId });
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

  /** Remember how the floating tile of visual type `type` was left; a browser that cannot store keeps it for the session only. */
  function rememberTile(type: string, change: Partial<TileMemory>) {
    try {
      updateBrowser((state) => withTile(state, type, change));
    } catch {
      // Storage is off, full or holds a value this build cannot read.
    }
  }

  /** Where `tile` is drawn in the stage, which the stage's hold may have moved from the place last saved. */
  function drawnPlace(stage: HTMLElement, tile: HTMLElement) {
    const stageRect = stage.getBoundingClientRect();
    const tileRect = tile.getBoundingClientRect();
    return { left: tileRect.left - stageRect.left, top: tileRect.top - stageRect.top };
  }

  function placeFloatingPane(paneId: string, type: string, place: { readonly left: number; readonly top: number }) {
    setFloatingPositions((previous) => ({ ...previous, [paneId]: place }));
    rememberTile(type, { place });
  }

  /**
   * Stand `pane` in the column at one side of the page, or in the main strip
   * beside it, at once: a floating tile docks there, a docked one moves, and
   * Chat standing aside comes back.
   */
  function dockTile(pane: Pane, zone: Zone) {
    if (pane.type === "trax.chat") feed.dock();
    if (pane.placement !== zone) operate({ kind: "place", instance_id: pane.id, placement: zone });
  }

  /**
   * Float docked `pane` over the stage at `place`. Chat floats by standing
   * aside, which is this tab's state and folds when the pointer leaves it; any
   * other visual by its placement.
   */
  function floatTile(pane: Pane, place: { readonly left: number; readonly top: number }) {
    placeFloatingPane(pane.id, pane.type, place);
    if (pane.type === "trax.chat") {
      tornOff.current = true;
      feed.stepAside();
    } else {
      operate({ kind: "place", instance_id: pane.id, placement: "floating" });
    }
  }

  function focusTile(pane: Pane) {
    if (workspace?.focused_instance !== pane.id) operate({ kind: "focus", instance_id: pane.id });
  }

  function foldFloatingPane(type: string, collapsed: boolean) {
    // Chat folds on its own; how it was last left is not remembered.
    if (type === "trax.chat") return chatOpen.set(!collapsed);
    setFoldedTypes((previous) => ({ ...previous, [type]: collapsed }));
    rememberTile(type, { collapsed });
  }

  function moveFloatingPane(paneId: string, type: string, left: number, top: number) {
    const stage = stageRef.current;
    const tile = stage?.querySelector<HTMLElement>(`[data-visual-instance="${paneId}"]`);
    if (!stage || !tile) return;
    const maxLeft = Math.max(0, stage.clientWidth - tile.offsetWidth);
    const maxTop = Math.max(0, stage.clientHeight - tile.offsetHeight);
    placeFloatingPane(paneId, type, {
      left: Math.min(maxLeft, Math.max(0, left)),
      top: Math.min(maxTop, Math.max(0, top)),
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
   * every visual in it, Chat's whole conversation included (6 ms a move with
   * 120 lines, against 3.3 ms so). The canvas takes the outcome once, on release.
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
      placeFloatingPane(drag.pane.id, drag.pane.type, outcome.place);
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

  /** Keep a size the user dragged, for this page and in this browser. */
  function keepSizes(change: (previous: CanvasSizes) => CanvasSizes) {
    setSizes((previous) => {
      const next = change(previous);
      writeCanvasSizes(next);
      return next;
    });
  }

  function keepColumnWidth(side: Side, width: number) {
    keepSizes((previous) => ({ ...previous, [side]: width }));
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
    placeFloatingPane(resize.pane.id, resize.pane.type, { left, top });
    keepSizes((previous) => ({ ...previous, floating: { ...previous.floating, [resize.pane.type]: { width, height } } }));
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
    keepSizes((previous) => ({ ...previous, share: { ...previous.share, ...shares } }));
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

  function saveCurrentCanvas(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (presetName.trim()) savePreset.mutate();
  }

  function openSavedPreset(preset: WorkspacePreset) {
    openPreset.mutate(preset);
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
          ...(descriptor?.requires.includes("record") && currentRecordId ? { record_id: currentRecordId } : {}),
        };
      void operate(operation);
    } else if (legacyPreview) {
      setPreviewSelected(active.includes(type) ? active.filter((item) => item !== type) : [...active, type]);
    }
  }

  const renderTile = (pane: (typeof panes)[number], index: number) => {
    const floating = floats(pane);
    const isChat = pane.type === "trax.chat";
    const isPage = pane.type === "trax.browse";
    const aside = floating && isChat;
    const title = catalog.data?.visuals.find((visual) => visual.type === pane.type)?.title ?? pane.type;
    const collapsed = floating && folded(pane);
    const at = floatingPositions[pane.id] ?? rememberedTile(browser, pane.type).place ?? pane.floating_rect ?? null;
    const size = sizes.floating[pane.type] ?? pane.floating_rect;
    const share = sizes.share[pane.type];
    return (
    // Browse is keyed by its type, which a canvas has once: its id is the
    // type until the server's canvas arrives, then a UUID, and a new key
    // would remount the view inside it, which reads its data again.
    // Chat keeps its place among its home's tiles while it stands aside, so going
    // aside and docking never mount it again: its draft and its scroll stay.
    <div className={`visual-tile visual-tile-${floating ? "floating" : pane.placement ?? "main"}${aside ? " visual-tile-aside" : ""}${pane.type === "trax.browse" ? " visual-tile-browse" : ""}${workspace?.focused_instance === pane.id ? " visual-tile-focused" : ""}${expandedMobileFloat === pane.id ? " visual-tile-mobile-expanded" : ""}${collapsed ? " visual-tile-collapsed" : ""}`}
      key={pane.type === "trax.browse" ? pane.type : pane.id}
      data-visual-instance={pane.id}
      data-visual-type={pane.type}
      ref={isChat ? chatTile : undefined}
      {...(aside ? chatOpen.handlers : {})}
      style={floating ? {
        top: `${at?.top ?? 54 + index * 24}px`,
        ...(at ? { left: `${at.left}px`, right: "auto" } : {}),
        ...(size ? { width: `${size.width}px`, height: `${size.height}px` } : {}),
        // Chat aside lies over every other floating tile, whichever strip it is drawn in.
        zIndex: 10 + (aside ? panes.length : index),
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
        } : { onClick: () => focusTile(pane) })}>
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
        {!isPage && <button type="button" title="Dismiss visual" disabled={change.isPending}
          onClick={() => operate({ kind: "hide", instance_id: pane.id })}>×</button>}
      </div>}
      <VisualPane instance={pane} workspace={workspace ?? null} onWorkspaceChanged={acceptWorkspace}
        focused={workspace?.focused_instance === pane.id}>
        {/* A crash in the page is the app's crash screen, here, and clears when the page moves. */}
        {pane.type === "trax.browse"
          ? <CrashBoundary reload={() => window.location.reload()} resetKey={window.location.hash}
            frame={(crash) => <div className="view">{crash}</div>}>{children}</CrashBoundary>
          : null}
      </VisualPane>
    </div>
    );
  };

  /**
   * The tiles standing at `placement`, with a divider between each two that
   * share its flow: across the main strip (`x`) or down a column (`y`). Chat
   * standing aside keeps its place among them and takes none of the flow.
   */
  const strip = (placement: "main" | Side, axis: "x" | "y") => {
    let flowing = 0;
    return panes.flatMap((pane, index) => {
      if ((pane.placement ?? "main") !== placement) return [];
      const tile = renderTile(pane, index);
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
      <div className="visual-toolbar">
        <span className="visual-toolbar-label">Canvas</span>
        <div className="visual-toolbar-actions" role="toolbar" aria-label="Canvas controls">
          <button className="btn ghost" type="button" disabled={!workspace || change.isPending} onClick={showChat}
            title={chatRecordId ? "Chat about this record" : "Chat"}>Chat</button>
          <button className="btn ghost" type="button" aria-expanded={!configure.collapsed} aria-controls="visual-configure"
            onClick={configure.toggle}>Configure</button>
        </div>
      </div>
      {writeError && <p className="visual-write-error" role="alert">{writeError}</p>}
      {!configure.collapsed && (
        <aside className="visual-configure" id="visual-configure" aria-label="Configure visuals">
          <div className="visual-configure-head"><h2>Visuals</h2><button className="btn ghost" type="button" onClick={() => configure.setCollapsed(true)}>Done</button></div>
          {catalog.isPending && <p className="muted">Loading available visuals…</p>}
          {catalog.isError && <p role="alert">Could not load visuals. <button className="btn" type="button" onClick={() => void catalog.refetch()}>Retry</button></p>}
          {created.isError && !legacyPreview && <p role="alert">Could not open your canvas. <button className="btn" type="button" onClick={() => void created.refetch()}>Retry</button></p>}
          {catalog.data?.visuals.map((visual) => (
            <VisualOption key={visual.type} visual={visual} checked={active.includes(visual.type)}
              disabled={(!workspace && !legacyPreview) || change.isPending || visual.type === "trax.browse"
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
          <section className="visual-presets" aria-label="Saved views">
            <h3>Saved views</h3>
            <form onSubmit={saveCurrentCanvas}>
              <label>View name<input aria-label="Saved view name" maxLength={120} required value={presetName}
                onChange={(event) => setPresetName(event.target.value)} /></label>
              <label>Agent instructions <span>(optional)</span><textarea aria-label="Agent instructions"
                maxLength={8192} rows={3} value={presetInstructions}
                onChange={(event) => { instructionsDirty.current = true; setPresetInstructions(event.target.value); }} /></label>
              <label>Continuation record ID <span>(optional)</span><input aria-label="Continuation record ID"
                value={continuationRecordId} placeholder={currentRecordId ?? "Record UUID"}
                onChange={(event) => { recordIdDirty.current = true; setContinuationRecordId(event.target.value); }} /></label>
              <button className="btn" type="submit" disabled={!workspace || !presetName.trim() || savePreset.isPending}>
                {savePreset.isPending ? "Saving…" : "Save canvas"}
              </button>
            </form>
            {presets.isPending && <p className="muted">Loading saved views…</p>}
            {presets.isError && <p role="alert">Could not load saved views. <button className="btn" type="button"
              onClick={() => void presets.refetch()}>Retry</button></p>}
            {presets.data && presets.data.length === 0 && <p className="muted">No saved views yet.</p>}
            {presets.data && presets.data.length > 0 && <ul>
              {presets.data.map((preset) => <li key={preset.id}>
                <button className="btn ghost" type="button" disabled={!workspace || openPreset.isPending}
                  aria-label={`Open ${preset.name}`} onClick={() => openSavedPreset(preset)}>
                  Open {preset.name}
                </button>
                {preset.continuation_record_id && <small>Continue from {preset.continuation_record_id}</small>}
              </li>)}
            </ul>}
            {presetError && <p role="alert">{presetError}</p>}
            {presetStatus && <p className="visual-preset-status" role="status">{presetStatus}</p>}
          </section>
        </aside>
      )}
      <div ref={stageRef} className={`visual-stage${panes.length > 1 ? " visual-stage-split" : ""}`}>
        {/* The page and the other main visuals share a strip that scrolls inside itself when they outgrow it; the visuals docked at a side stand in one column there, so Chat's header never leaves the screen; floating ones lie over all three, as Chat does from inside its strip while it stands aside. */}
        {sideColumn("left")}
        {panes.some((pane) => (pane.placement ?? "main") === "main") && (
          <div className="visual-main-strip">
            {strip("main", "x")}
          </div>
        )}
        {sideColumn("side")}
        {panes.map((pane, index) => pane.placement === "floating" ? renderTile(pane, index) : null)}
        <div ref={snapRef} className="visual-snap" aria-hidden="true" />
        <div ref={ghostRef} className="visual-drag-ghost" aria-hidden="true" />
      </div>
    </div>
    </WorkspaceActionsProvider>
  );
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

function readFloatingRects(
  state: WorkspaceState,
  stage: HTMLDivElement | null,
): Readonly<Record<string, FloatingRect>> {
  if (!stage) return {};
  const stageRect = stage.getBoundingClientRect();
  return Object.fromEntries(state.visuals.flatMap((visual) => {
    // Chat floats only while it stands aside, which no saved view holds.
    if (visual.placement !== "floating" || visual.type === "trax.chat") return [];
    const tile = stage.querySelector<HTMLElement>(`[data-visual-instance="${visual.id}"]`);
    if (!tile) return visual.floating_rect ? [[visual.id, visual.floating_rect]] : [];
    const rect = tile.getBoundingClientRect();
    const left = Math.max(0, Math.round(rect.left - stageRect.left));
    const top = Math.max(0, Math.round(rect.top - stageRect.top));
    const width = Math.min(2_000, Math.max(240, Math.round(rect.width)));
    const height = Math.min(2_000, Math.max(180, Math.round(rect.height)));
    return [[visual.id, { left, top, width, height } satisfies FloatingRect]];
  }));
}

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : "Please try again.";
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

/** Where on the card that stands for a docked tile the pointer holds it, and so where the floating tile lands under it. */
const GHOST_GRIP = { x: 60, y: 16 };

/** What a column never resized is told to assistive tech: the 360 px its CSS gives it at most. */
const DEFAULT_COLUMN = 360;

/** The canvas stacks its tiles in one column at this width and below (see canvas.css), floating ones included. */
const NARROW_VIEWPORT = 900;

/**
 * The Configure visuals panel: opened on demand by the toolbar's Configure,
 * which stays to open it again, and kept open or not for the tab.
 */
const CONFIGURE: PanelSpec = { id: "visuals.configure", name: "visual settings", side: "right", startsCollapsed: true };
