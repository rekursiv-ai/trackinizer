import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
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
import { type PanelSpec, usePanel } from "../ui/panel";
import { orderVisuals } from "./layout";
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
  const [floatingPositions, setFloatingPositions] = useState<Record<string, { readonly left: number; readonly top: number }>>({});
  const stageRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<FloatingDrag | null>(null);
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
  const panes = orderVisuals(visible.length ? visible : [{
    id: "chat-disconnected", type: "trax.chat", version: chat?.version ?? 1,
    placement: "main" as const, record_id: null, params: {},
  }], workspace?.focused_instance ?? null);
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
      setPresetStatus(`Opened “${preset.name}”.`);
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) openAttempt.current = null;
      setPresetError(`Could not open this saved view. ${errorText(error)}`);
      void remote.refetch();
    },
  });

  async function write(operation: WorkspaceOperation, expandRecordId?: string): Promise<boolean> {
    if (!latestWorkspace.current) return false;
    try {
      const state = await change.mutateAsync(operation);
      if (expandRecordId) {
        const chatPane = state.visuals.find((visual) =>
          visual.type === "trax.chat" && visual.record_id === expandRecordId && visual.placement === "floating");
        if (chatPane) setExpandedMobileFloat(chatPane.id);
      }
      return true;
    } catch {
      return false;
    }
  }

  function chatAbout(recordId: string) {
    void write({ kind: "show", visual_type: "trax.chat", placement: "floating", record_id: recordId }, recordId);
  }

  /** Show Chat as a floating window over the page, or focus it when it is already shown. */
  function showChat() {
    const existing = workspace?.visuals.find((visual) => visual.type === "trax.chat");
    operate(existing
      ? { kind: "focus", instance_id: existing.id }
      : { kind: "show", visual_type: "trax.chat", placement: "floating" });
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

  function moveFloatingPane(paneId: string, left: number, top: number) {
    const stage = stageRef.current;
    const tile = stage?.querySelector<HTMLElement>(`[data-visual-instance="${paneId}"]`);
    if (!stage || !tile) return;
    const maxLeft = Math.max(0, stage.clientWidth - tile.offsetWidth);
    const maxTop = Math.max(0, stage.clientHeight - tile.offsetHeight);
    setFloatingPositions((previous) => ({
      ...previous,
      [paneId]: {
        left: Math.min(maxLeft, Math.max(0, left)),
        top: Math.min(maxTop, Math.max(0, top)),
      },
    }));
  }

  function startFloatingDrag(event: PointerEvent<HTMLButtonElement>, paneId: string) {
    const stage = stageRef.current;
    const tile = event.currentTarget.closest<HTMLElement>(".visual-tile");
    if (!stage || !tile) return;
    const stageRect = stage.getBoundingClientRect();
    const tileRect = tile.getBoundingClientRect();
    const position = floatingPositions[paneId] ?? {
      left: tileRect.left - stageRect.left,
      top: tileRect.top - stageRect.top,
    };
    // The bounds are measured once: read on every move, they lay the page out
    // again each time.
    dragRef.current = {
      id: paneId, pointerId: event.pointerId, x: event.clientX, y: event.clientY,
      left: position.left, top: position.top, tile,
      maxLeft: Math.max(0, stage.clientWidth - tile.offsetWidth),
      maxTop: Math.max(0, stage.clientHeight - tile.offsetHeight),
      dx: 0, dy: 0,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  /**
   * Each move only transforms the tile: rendering the canvas per move redrew
   * every visual in it, Chat's whole conversation included (6 ms a move with
   * 120 lines, against 3.3 ms so). The canvas takes the place once, on release.
   */
  function updateFloatingDrag(event: PointerEvent<HTMLButtonElement>) {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    drag.dx = Math.min(drag.maxLeft, Math.max(0, drag.left + event.clientX - drag.x)) - drag.left;
    drag.dy = Math.min(drag.maxTop, Math.max(0, drag.top + event.clientY - drag.y)) - drag.top;
    drag.tile.style.transform = `translate(${drag.dx}px, ${drag.dy}px)`;
  }

  function stopFloatingDrag(event: PointerEvent<HTMLButtonElement>) {
    const drag = dragRef.current;
    if (drag?.pointerId !== event.pointerId) return;
    dragRef.current = null;
    const place = { left: drag.left + drag.dx, top: drag.top + drag.dy };
    // Placed before the transform goes, so no frame shows the tile back where it started.
    Object.assign(drag.tile.style, { left: `${place.left}px`, top: `${place.top}px`, right: "auto", transform: "" });
    setFloatingPositions((previous) => ({ ...previous, [drag.id]: place }));
  }

  function moveWithKeyboard(event: KeyboardEvent<HTMLButtonElement>, paneId: string) {
    const directions = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] } as const;
    const direction = directions[event.key as keyof typeof directions];
    if (!direction) return;
    event.preventDefault();
    const stage = stageRef.current;
    const tile = event.currentTarget.closest<HTMLElement>(".visual-tile");
    if (!stage || !tile) return;
    const rect = tile.getBoundingClientRect();
    const stageRect = stage.getBoundingClientRect();
    const position = floatingPositions[paneId] ?? { left: rect.left - stageRect.left, top: rect.top - stageRect.top };
    const step = event.shiftKey ? 48 : 16;
    moveFloatingPane(paneId, position.left + direction[0] * step, position.top + direction[1] * step);
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

  const renderTile = (pane: (typeof panes)[number], index: number) => (
    // Browse is keyed by its type, which a canvas has once: its id is the
    // type until the server's canvas arrives, then a UUID, and a new key
    // would remount the view inside it, which reads its data again.
    <div className={`visual-tile visual-tile-${pane.placement ?? "main"}${pane.type === "trax.browse" ? " visual-tile-browse" : ""}${workspace?.focused_instance === pane.id ? " visual-tile-focused" : ""}${expandedMobileFloat === pane.id ? " visual-tile-mobile-expanded" : ""}`}
      key={pane.type === "trax.browse" ? pane.type : pane.id}
      data-visual-instance={pane.id}
      style={pane.placement === "floating" ? {
        top: `${floatingPositions[pane.id]?.top ?? pane.floating_rect?.top ?? 54 + index * 24}px`,
        ...(floatingPositions[pane.id] || pane.floating_rect ? {
          left: `${floatingPositions[pane.id]?.left ?? pane.floating_rect?.left ?? 0}px`, right: "auto",
        } : {}),
        ...(pane.floating_rect ? {
          width: `${pane.floating_rect.width}px`, height: `${pane.floating_rect.height}px`,
        } : {}),
        zIndex: 10 + index,
      } : undefined}>
      {workspace && (panes.length > 1 || pane.placement === "floating") && <div className="visual-tile-toolbar">
        {pane.placement === "floating" && <button className="visual-tile-drag-handle" type="button"
          aria-label={`Move ${catalog.data?.visuals.find((visual) => visual.type === pane.type)?.title ?? pane.type}`}
          title="Drag to move; use arrow keys to move"
          onPointerDown={(event) => startFloatingDrag(event, pane.id)}
          onPointerMove={updateFloatingDrag} onPointerUp={stopFloatingDrag} onPointerCancel={stopFloatingDrag}
          onKeyDown={(event) => moveWithKeyboard(event, pane.id)}>⠿</button>}
        <span>{catalog.data?.visuals.find((visual) => visual.type === pane.type)?.title ?? pane.type}</span>
        {pane.placement === "floating" && <button className="visual-mobile-tab-toggle" type="button"
          aria-expanded={expandedMobileFloat === pane.id}
          onClick={() => setExpandedMobileFloat(expandedMobileFloat === pane.id ? null : pane.id)}>
          {expandedMobileFloat === pane.id ? "Collapse" : "Expand"}
        </button>}
        <button type="button" title="Focus visual" disabled={change.isPending}
          onClick={() => operate({ kind: "focus", instance_id: pane.id })}>Focus</button>
        <select aria-label={`Place ${pane.type}`} value={pane.placement ?? "main"} disabled={change.isPending}
          onChange={(event) => operate({ kind: "place", instance_id: pane.id, placement: event.target.value as "main" | "side" | "floating" })}>
          <option value="main">Main</option><option value="side">Side</option><option value="floating">Float</option>
        </select>
        {pane.type !== "trax.browse" && <button type="button" title="Dismiss visual" disabled={change.isPending}
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
          <button className="btn ghost" type="button" disabled={!workspace || change.isPending} onClick={showChat}>Chat</button>
          <button className="btn ghost" type="button"
            disabled={!workspace || !chatRecordId || change.isPending}
            onClick={() => {
              if (chatRecordId) chatAbout(chatRecordId);
            }}>Chat about this</button>
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
              <label htmlFor="visual-artifact-link">Artifact link</label>
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
        {/* The page and the other main visuals share a strip that scrolls inside itself when they outgrow it; the side visuals stand in one column beside it, so Chat's header never leaves the screen; floating ones lie over both. */}
        {panes.some((pane) => (pane.placement ?? "main") === "main") && (
          <div className="visual-main-strip">
            {panes.map((pane, index) => (pane.placement ?? "main") === "main" ? renderTile(pane, index) : null)}
          </div>
        )}
        {panes.some((pane) => pane.placement === "side") && (
          <div className="visual-side-column">
            {panes.map((pane, index) => pane.placement === "side" ? renderTile(pane, index) : null)}
          </div>
        )}
        {panes.map((pane, index) => pane.placement === "floating" ? renderTile(pane, index) : null)}
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
    if (visual.placement !== "floating") return [];
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

/**
 * A floating visual being dragged: the pointer's start, the tile's place then
 * and how far it may go, and how far it has moved (`dx`, `dy`), applied as a
 * transform until release.
 */
type FloatingDrag = {
  readonly id: string;
  readonly pointerId: number;
  readonly x: number;
  readonly y: number;
  readonly left: number;
  readonly top: number;
  readonly tile: HTMLElement;
  readonly maxLeft: number;
  readonly maxTop: number;
  dx: number;
  dy: number;
};

/**
 * The Configure visuals panel: opened on demand by the toolbar's Configure,
 * which stays to open it again, and kept open or not for the tab.
 */
const CONFIGURE: PanelSpec = { id: "visuals.configure", name: "visual settings", side: "right", startsCollapsed: true };
