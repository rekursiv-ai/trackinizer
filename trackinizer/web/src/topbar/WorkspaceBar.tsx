import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type DragEvent, useEffect, useMemo, useRef, useState } from "react";
import { createWorkspace, createCanvas, listWorkspaces, setWorkspaceOrder, updateWorkspace, workspaceInfo, type Workspace, type WorkspaceInfo } from "../api/workspaces";
import { waitingOf } from "../api/runState";
import type { CanvasSummary } from "../api/canvasManagement";
import { openCanvas, canvasesQuery } from "../canvases/actions";
import { showCanvas, useCanvas } from "../canvases/useCanvas";
import { beginCanvasNavigation } from "../canvases/store";
import { acceptCanvas } from "../canvases/cache";
import { routeUrl } from "../router/addressBar";
import { Icon } from "../ui/icons";

const WORKSPACES_KEY = ["workspaces"] as const;
const workspacesQuery = { queryKey: WORKSPACES_KEY, queryFn: () => listWorkspaces({ hidden: true }), staleTime: 2_000 } as const;

/** Workspaces across the top bar; their chats remain in their own canvas state. */
export function WorkspaceBar() {
  const queryClient = useQueryClient();
  const { data: workspaces = [] } = useQuery(workspacesQuery);
  const { data: canvases = [] } = useQuery(canvasesQuery);
  const current = useCanvas().data;
  const [menu, setMenu] = useState<{ readonly id: string; readonly x: number; readonly y: number } | null>(null);
  const [infoId, setInfoId] = useState<string | null>(null);
  const [draggedId, setDraggedId] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [naming, setNaming] = useState<{ readonly id: string; readonly value: string } | null>(null);
  const [creating, setCreating] = useState(false);
  const picking = useRef(new Map<string, { current: () => boolean }>());
  const info = useQuery({
    queryKey: ["workspace-info", infoId], queryFn: () => workspaceInfo(infoId!), enabled: infoId !== null, staleTime: 30_000,
  });
  const ordered = useMemo(() => [
    ...workspaces.filter((each) => !each.hidden && each.pinned),
    ...workspaces.filter((each) => !each.hidden && !each.pinned),
  ], [workspaces]);
  const pinnedCount = ordered.findIndex((each) => !each.pinned);
  const boundary = pinnedCount < 0 ? ordered.length : pinnedCount;

  useEffect(() => {
    if (!menu && !infoId) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Element && !event.target.closest(".workspace-pop")) {
        setMenu(null);
        setInfoId(null);
      }
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setMenu(null); setInfoId(null); }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [menu, infoId]);

  async function refresh(): Promise<void> {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: WORKSPACES_KEY }),
      queryClient.invalidateQueries({ queryKey: canvasesQuery.queryKey }),
      queryClient.invalidateQueries({ queryKey: ["workspace-info"] }),
    ]);
  }

  async function pick(stream: Workspace, current = beginCanvasNavigation()): Promise<void> {
    const pending = picking.current.get(stream.id);
    if (pending) { pending.current = current; return; }
    const intent = { current };
    picking.current.set(stream.id, intent);
    const inStream = canvases.filter((each) => belongs(each, stream) && !each.hidden);
    const pinned = inStream.filter((each) => each.pinned).sort(byRecent)[0];
    const chosen = pinned ?? [...inStream].sort(byRecent)[0];
    try {
      setProblem(null);
      if (chosen) await openCanvas(queryClient, chosen.id);
      else {
        const state = await createCanvas(stream.id);
        acceptCanvas(queryClient, state);
        if (intent.current()) showCanvas(queryClient, state);
      }
      await refresh();
    } catch {
      setProblem("Could not open that workspace.");
    } finally {
      picking.current.delete(stream.id);
    }
  }

  async function patch(id: string, change: Parameters<typeof updateWorkspace>[1]): Promise<void> {
    try {
      setProblem(null);
      await updateWorkspace(id, change);
      void refresh();
    } catch {
      setProblem("Could not update that workspace.");
    }
  }

  async function persistPosition(id: string, placed: readonly Workspace[], pinned: boolean): Promise<void> {
    try {
      setProblem(null);
      await Promise.all([
        updateWorkspace(id, { pinned }),
        setWorkspaceOrder(placed.map((each) => each.id)),
      ]);
      void refresh();
    } catch {
      setProblem("Could not reorder workspaces.");
      void refresh();
    }
  }

  function move(id: string, step: -1 | 1): void {
    const at = ordered.findIndex((each) => each.id === id);
    const neighbour = ordered[at + step];
    if (!neighbour || neighbour.scratch) return;
    const placed = placeRelative(workspaces, id, neighbour.id, step > 0);
    void persistPosition(id, placed, neighbour.pinned);
    setMenu(null);
  }

  function dragStart(event: DragEvent<HTMLButtonElement>, id: string): void {
    setDraggedId(id);
    event.dataTransfer?.setData("text/plain", id);
    if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
  }

  function drop(event: DragEvent<HTMLButtonElement>, target: Workspace): void {
    event.preventDefault();
    const id = event.dataTransfer?.getData("text/plain") || draggedId;
    if (!id || id === target.id || target.scratch || workspaces.find((stream) => stream.id === id)?.scratch) return;
    const after = event.clientX > event.currentTarget.getBoundingClientRect().left + event.currentTarget.getBoundingClientRect().width / 2;
    void persistPosition(id, placeRelative(workspaces, id, target.id, after), target.pinned);
    setDraggedId(null);
  }

  async function create(): Promise<void> {
    if (creating) return;
    const current = beginCanvasNavigation();
    setCreating(true);
    setProblem(null);
    try {
      const names = new Set(workspaces.map((stream) => stream.title));
      let number = 1;
      while (names.has(`Workspace ${number}`)) number += 1;
      const stream = await createWorkspace(`Workspace ${number}`);
      await queryClient.cancelQueries({ queryKey: WORKSPACES_KEY });
      queryClient.setQueryData<Workspace[]>(WORKSPACES_KEY, (previous = []) => [...previous, stream]);
      if (current()) {
        setNaming({ id: stream.id, value: stream.title });
        await pick(stream, current);
      }
    } catch {
      setProblem("Could not create that workspace.");
    } finally {
      setCreating(false);
    }
  }

  function rename(stream: Workspace): void {
    setMenu(null);
    setNaming({ id: stream.id, value: stream.title });
  }

  function keepName(): void {
    if (!naming) return;
    const title = naming.value.trim();
    setNaming(null);
    if (title && title !== workspaces.find((stream) => stream.id === naming.id)?.title) {
      void patch(naming.id, { title });
    }
  }

  async function openInfo(stream: Workspace): Promise<void> {
    setMenu(null);
    setInfoId(stream.id);
    try {
      await queryClient.fetchQuery({ queryKey: ["workspace-info", stream.id], queryFn: () => workspaceInfo(stream.id), staleTime: 30_000 });
    } catch {
      setProblem("Could not load workspace info.");
    }
  }

  return (
    <div className="workspace-bar-wrap">
      <nav className="workspace-bar" aria-label="Workspaces">
        {ordered.map((stream, index) => (
          <span key={stream.id} className="workspace-slot">
            {index === boundary && boundary > 0 && boundary < ordered.length && <span className="workspace-divider" aria-hidden="true" />}
          {stream.id === naming?.id ? (
            <input className="workspace-title workspace-name" aria-label="Workspace name" maxLength={120}
              value={naming.value} autoFocus onFocus={(event) => {
                event.currentTarget.select();
                event.currentTarget.scrollIntoView?.({ block: "nearest", inline: "nearest" });
              }}
              onChange={(event) => setNaming({ id: stream.id, value: event.target.value })}
              onBlur={keepName} onKeyDown={(event) => {
                if (event.nativeEvent.isComposing) return;
                if (event.key === "Enter") { event.preventDefault(); keepName(); }
                else if (event.key === "Escape") { event.stopPropagation(); setNaming(null); }
              }} />
          ) : (
          <button type="button" className={`workspace-title${current && belongs(current, stream) ? " is-current" : ""}${draggedId === stream.id ? " is-dragged" : ""}`}
              aria-label={stream.title} title={`${stream.title} (double-click to rename)`} draggable={!stream.scratch}
              onClick={() => void pick(stream)}
              onDoubleClick={() => rename(stream)}
              onContextMenu={(event) => { event.preventDefault(); setMenu({ id: stream.id, x: event.clientX, y: event.clientY }); }}
              onDragStart={(event) => dragStart(event, stream.id)} onDragOver={(event) => event.preventDefault()} onDrop={(event) => drop(event, stream)}
              onDragEnd={() => setDraggedId(null)}>
              {stream.pinned && <span className="workspace-pin" title="Pinned workspace" aria-hidden="true"><Icon name="bookmark" size={12} /></span>}
              <span className="workspace-title-text">{stream.title}</span>
              {canvases.some((each) => belongs(each, stream) && !each.hidden && waitingOf(each) === true)
                && <span className="workspace-waiting" role="img" aria-label="Waiting for your answer" title={`A chat in ${stream.title} is waiting for your answer`} />}
            </button>
          )}
          </span>
        ))}
      </nav>
      <button type="button" className="workspace-new" aria-label="New workspace" title="New workspace" disabled={creating} onClick={() => void create()}>
        <Icon name="plus" size={13} /><span>Workspace</span>
      </button>
      {menu && workspaces.find((each) => each.id === menu.id) && (() => {
        const stream = workspaces.find((each) => each.id === menu.id)!;
        const at = ordered.findIndex((each) => each.id === stream.id);
        return <div className="topbar-pop workspace-pop workspace-menu" role="menu" aria-label={`${stream.title} actions`}
          style={{ left: menu.x, top: menu.y }}>
          <button type="button" role="menuitem" className="topbar-item" onClick={() => void openInfo(stream)}>Info</button>
          {!stream.scratch && <>
            <button type="button" role="menuitem" className="topbar-item" onClick={() => { setMenu(null); void patch(stream.id, { pinned: !stream.pinned }); }}>{stream.pinned ? "Unpin" : "Pin"}</button>
            <button type="button" role="menuitem" className="topbar-item" disabled={at === 0 || ordered[at - 1]?.scratch} onClick={() => move(stream.id, -1)}>Move left</button>
            <button type="button" role="menuitem" className="topbar-item" disabled={at < 0 || at === ordered.length - 1} onClick={() => move(stream.id, 1)}>Move right</button>
          </>}
          <button type="button" role="menuitem" className="topbar-item" onClick={() => rename(stream)}>Rename</button>
          {!stream.scratch && <button type="button" role="menuitem" className="topbar-item" onClick={() => { setMenu(null); void patch(stream.id, { hidden: true }); }}>Hide</button>}
        </div>;
      })()}
      {infoId && <WorkspaceInfoDrawer info={info.data} loading={info.isPending} onClose={() => setInfoId(null)} />}
      {problem && <p className="workspace-problem" role="alert">{problem}</p>}
    </div>
  );
}

function WorkspaceInfoDrawer({
  info, loading, onClose,
}: {
  readonly info: WorkspaceInfo | undefined;
  readonly loading: boolean;
  readonly onClose: () => void;
}) {
  if (!info) return loading ? <aside className="workspace-drawer workspace-pop" role="dialog" aria-label="Workspace info">Loading…</aside> : null;
  return (
    <aside className="workspace-drawer workspace-pop" role="dialog" aria-label={`${info.title} info`}>
      <div className="workspace-drawer-head"><h2>{info.title}</h2><button type="button" aria-label="Close info" onClick={onClose}><Icon name="x" /></button></div>
      <a href={routeUrl(`#/lookup/${info.id}`)}>Issue {info.seq}</a>
      <a href={routeUrl(`#/lookup/${info.id}`)}>Workspace settings</a>
      <h3>Chats</h3>
      {info.canvases.length ? <ul>{info.canvases.map((each) => <li key={each.id}>
        <span>{each.name}{each.hidden ? " · hidden" : ""}</span>
        {each.session_ids.map((session) => <a className="workspace-session" href={routeUrl(`#/lookup/${session}`)} key={session}>{session}</a>)}
      </li>)}</ul> : <p>No chats in this workspace.</p>}
    </aside>
  );
}

function belongs(canvas: Pick<CanvasSummary, "workspace_id"> | { readonly workspace_id?: string | null }, stream: Workspace): boolean {
  return stream.scratch ? canvas.workspace_id == null : canvas.workspace_id === stream.id;
}

function byRecent(left: CanvasSummary, right: CanvasSummary): number {
  return right.modified_at.localeCompare(left.modified_at) || left.id.localeCompare(right.id);
}

function placeRelative(all: readonly Workspace[], id: string, targetId: string, after: boolean): Workspace[] {
  const moving = all.find((each) => each.id === id);
  if (!moving || id === targetId) return [...all];
  const remaining = all.filter((each) => each.id !== id);
  const targetAt = remaining.findIndex((each) => each.id === targetId);
  if (targetAt < 0) return [...all];
  remaining.splice(targetAt + Number(after), 0, moving);
  return remaining;
}
