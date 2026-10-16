import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type DragEvent, useEffect, useMemo, useState } from "react";
import { createWorkstream, createStation, listWorkstreams, setWorkstreamOrder, updateWorkstream, workstreamInfo, type Workstream, type WorkstreamInfo } from "../api/workstreams";
import type { StationSummary } from "../api/stations";
import { openStation, stationsQuery } from "../stations/actions";
import { showStation, useStation } from "../stations/useStation";
import { Icon } from "../ui/icons";

const WORKSTREAMS_KEY = ["workstreams"] as const;
const workstreamsQuery = { queryKey: WORKSTREAMS_KEY, queryFn: () => listWorkstreams({ hidden: true }), staleTime: 2_000 } as const;

/** Workstreams across the top bar; their chats remain in their own station state. */
export function WorkstreamBar() {
  const queryClient = useQueryClient();
  const { data: workstreams = [] } = useQuery(workstreamsQuery);
  const { data: stations = [] } = useQuery(stationsQuery);
  const current = useStation().data;
  const [menu, setMenu] = useState<{ readonly id: string; readonly x: number; readonly y: number } | null>(null);
  const [infoId, setInfoId] = useState<string | null>(null);
  const [draggedId, setDraggedId] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const info = useQuery({
    queryKey: ["workstream-info", infoId], queryFn: () => workstreamInfo(infoId!), enabled: infoId !== null, staleTime: 30_000,
  });
  const ordered = useMemo(() => [
    ...workstreams.filter((each) => !each.hidden && each.pinned),
    ...workstreams.filter((each) => !each.hidden && !each.pinned),
  ], [workstreams]);
  const pinnedCount = ordered.findIndex((each) => !each.pinned);
  const boundary = pinnedCount < 0 ? ordered.length : pinnedCount;

  useEffect(() => {
    if (!menu && !infoId) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Element && !event.target.closest(".workstream-pop")) {
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
      queryClient.invalidateQueries({ queryKey: WORKSTREAMS_KEY }),
      queryClient.invalidateQueries({ queryKey: stationsQuery.queryKey }),
    ]);
  }

  async function pick(stream: Workstream): Promise<void> {
    const inStream = stations.filter((each) => belongs(each, stream) && !each.hidden);
    const pinned = inStream.filter((each) => each.pinned).sort(byRecent)[0];
    const chosen = pinned ?? [...inStream].sort(byRecent)[0];
    try {
      setProblem(null);
      if (chosen) await openStation(queryClient, chosen.id);
      else showStation(queryClient, await createStation(stream.id));
      void refresh();
    } catch {
      setProblem("Could not open that workstream.");
    }
  }

  async function patch(id: string, change: Parameters<typeof updateWorkstream>[1]): Promise<void> {
    try {
      setProblem(null);
      await updateWorkstream(id, change);
      void refresh();
    } catch {
      setProblem("Could not update that workstream.");
    }
  }

  async function persistPosition(id: string, placed: readonly Workstream[], pinned: boolean): Promise<void> {
    try {
      setProblem(null);
      await Promise.all([
        updateWorkstream(id, { pinned }),
        setWorkstreamOrder(placed.map((each) => each.id)),
      ]);
      void refresh();
    } catch {
      setProblem("Could not reorder workstreams.");
      void refresh();
    }
  }

  function move(id: string, step: -1 | 1): void {
    const at = ordered.findIndex((each) => each.id === id);
    const neighbour = ordered[at + step];
    if (!neighbour) return;
    const placed = placeRelative(workstreams, id, neighbour.id, step > 0);
    void persistPosition(id, placed, neighbour.pinned);
    setMenu(null);
  }

  function dragStart(event: DragEvent<HTMLButtonElement>, id: string): void {
    setDraggedId(id);
    event.dataTransfer?.setData("text/plain", id);
    if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
  }

  function drop(event: DragEvent<HTMLButtonElement>, target: Workstream): void {
    event.preventDefault();
    const id = event.dataTransfer?.getData("text/plain") || draggedId;
    if (!id || id === target.id) return;
    const after = event.clientX > event.currentTarget.getBoundingClientRect().left + event.currentTarget.getBoundingClientRect().width / 2;
    void persistPosition(id, placeRelative(workstreams, id, target.id, after), target.pinned);
    setDraggedId(null);
  }

  async function create(): Promise<void> {
    const title = window.prompt("New workstream name");
    if (!title?.trim()) return;
    try {
      await createWorkstream(title.trim());
      await refresh();
      setProblem(null);
    } catch {
      setProblem("Could not create that workstream.");
    }
  }

  function rename(stream: Workstream): void {
    setMenu(null);
    const title = window.prompt("Workstream name", stream.title);
    if (title?.trim()) void patch(stream.id, { title: title.trim() });
  }

  async function openInfo(stream: Workstream): Promise<void> {
    setMenu(null);
    setInfoId(stream.id);
    try {
      await queryClient.fetchQuery({ queryKey: ["workstream-info", stream.id], queryFn: () => workstreamInfo(stream.id), staleTime: 30_000 });
    } catch {
      setProblem("Could not load workstream info.");
    }
  }

  return (
    <div className="workstream-bar-wrap">
      <nav className="workstream-bar" aria-label="Workstreams">
        {ordered.map((stream, index) => (
          <span key={stream.id} className="workstream-slot">
            {index === boundary && boundary > 0 && boundary < ordered.length && <span className="workstream-divider" aria-hidden="true" />}
          <button type="button" className={`workstream-title${current && belongs(current, stream) ? " is-current" : ""}${draggedId === stream.id ? " is-dragged" : ""}`}
              aria-label={stream.title} title={stream.title} draggable
              onClick={() => void pick(stream)}
              onContextMenu={(event) => { event.preventDefault(); setMenu({ id: stream.id, x: event.clientX, y: event.clientY }); }}
              onDragStart={(event) => dragStart(event, stream.id)} onDragOver={(event) => event.preventDefault()} onDrop={(event) => drop(event, stream)}
              onDragEnd={() => setDraggedId(null)}>
              {stream.pinned && <span className="workstream-pin" aria-hidden="true">📌</span>}
              <span className="workstream-title-text">{stream.title}</span>
            </button>
          </span>
        ))}
      </nav>
      <button type="button" className="workstream-new" aria-label="New workstream" title="New workstream" onClick={() => void create()}>
        <Icon name="plus" size={13} /><span>Workstream</span>
      </button>
      {menu && workstreams.find((each) => each.id === menu.id) && (() => {
        const stream = workstreams.find((each) => each.id === menu.id)!;
        const at = ordered.findIndex((each) => each.id === stream.id);
        return <div className="topbar-pop workstream-pop workstream-menu" role="menu" aria-label={`${stream.title} actions`}
          style={{ left: menu.x, top: menu.y }}>
          <button type="button" role="menuitem" className="topbar-item" onClick={() => void openInfo(stream)}>Info</button>
          <button type="button" role="menuitem" className="topbar-item" onClick={() => { setMenu(null); void patch(stream.id, { pinned: !stream.pinned }); }}>{stream.pinned ? "Unpin" : "Pin"}</button>
          <button type="button" role="menuitem" className="topbar-item" disabled={at === 0} onClick={() => move(stream.id, -1)}>Move left</button>
          <button type="button" role="menuitem" className="topbar-item" disabled={at < 0 || at === ordered.length - 1} onClick={() => move(stream.id, 1)}>Move right</button>
          <button type="button" role="menuitem" className="topbar-item" onClick={() => rename(stream)}>Rename</button>
          {!stream.scratch && <button type="button" role="menuitem" className="topbar-item" onClick={() => { setMenu(null); void patch(stream.id, { hidden: true }); }}>Hide</button>}
        </div>;
      })()}
      {infoId && <WorkstreamInfoDrawer info={info.data} loading={info.isPending} onClose={() => setInfoId(null)} />}
      {problem && <p className="workstream-problem" role="alert">{problem}</p>}
    </div>
  );
}

function WorkstreamInfoDrawer({
  info, loading, onClose,
}: {
  readonly info: WorkstreamInfo | undefined;
  readonly loading: boolean;
  readonly onClose: () => void;
}) {
  if (!info) return loading ? <aside className="workstream-drawer workstream-pop" role="dialog" aria-label="Workstream info">Loading…</aside> : null;
  return (
    <aside className="workstream-drawer workstream-pop" role="dialog" aria-label={`${info.title} info`}>
      <div className="workstream-drawer-head"><h2>{info.title}</h2><button type="button" aria-label="Close info" onClick={onClose}><Icon name="x" /></button></div>
      <a href={`#/lookup/${info.id}`}>Issue {info.seq}</a>
      <h3>Chats</h3>
      {info.stations.length ? <ul>{info.stations.map((each) => <li key={each.id}>
        <span>{each.name}{each.hidden ? " · hidden" : ""}</span>
        {each.session_ids.map((session) => <span className="workstream-session" key={session}>{session}</span>)}
      </li>)}</ul> : <p>No chats in this workstream.</p>}
    </aside>
  );
}

function belongs(station: Pick<StationSummary, "workstream_id"> | { readonly workstream_id?: string | null }, stream: Workstream): boolean {
  return stream.scratch ? station.workstream_id == null : station.workstream_id === stream.id;
}

function byRecent(left: StationSummary, right: StationSummary): number {
  return right.modified_at.localeCompare(left.modified_at) || left.id.localeCompare(right.id);
}

function placeRelative(all: readonly Workstream[], id: string, targetId: string, after: boolean): Workstream[] {
  const moving = all.find((each) => each.id === id);
  if (!moving || id === targetId) return [...all];
  const remaining = all.filter((each) => each.id !== id);
  const targetAt = remaining.findIndex((each) => each.id === targetId);
  if (targetAt < 0) return [...all];
  remaining.splice(targetAt + Number(after), 0, moving);
  return remaining;
}
