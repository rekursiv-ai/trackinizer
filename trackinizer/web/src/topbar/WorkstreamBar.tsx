import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type CSSProperties, type DragEvent, type ReactNode, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { createWorkstream, createStation, listWorkstreams, setWorkstreamOrder, updateWorkstream, workstreamInfo, type Workstream, type WorkstreamInfo } from "../api/workstreams";
import { nameStation, openStation, stationsQuery } from "../stations/actions";
import { showStation, useStation } from "../stations/useStation";
import { belongs, byRecent, chatsIn } from "../stations/workstreamChats";
import { Icon } from "../ui/icons";

const WORKSTREAMS_KEY = ["workstreams"] as const;

/** How long the pointer rests on a workstream before its chats slide out: a pointer only passing over opens nothing. */
export const PEEK_AFTER_MS = 180;

/** How long peeked chats stay out after the pointer leaves the bar. */
export const FOLD_AFTER_MS = 300;

/** How many group colours the theme has (`--group-1` to `--group-4`); the workstreams take them in turn. */
const COLOURS = 4;

/** The longest name a chat takes (`server/visuals/workspaces.py`). */
const MAX_CHAT_NAME = 120;
const workstreamsQuery = { queryKey: WORKSTREAMS_KEY, queryFn: () => listWorkstreams({ hidden: true }), staleTime: 2_000 } as const;

/**
 * Workstreams across the top bar, each a coloured title with its chats folded
 * behind it. The pointer resting on a group, or the keyboard in it, slides its
 * chats out (`peekAt`); leaving the bar folds them. A click on another workstream's title opens it and keeps
 * its chats out; a click on the current one's keeps them out, and a second
 * folds them. Only the current workstream's chats stay out. Folded, the current
 * title names the chat on show. A chat opens on a click and renames on a double
 * click; + makes a chat in that workstream with its name ready to type.
 */
export function WorkstreamBar() {
  const queryClient = useQueryClient();
  const { data: workstreams = [] } = useQuery(workstreamsQuery);
  const { data: stations = [] } = useQuery(stationsQuery);
  const current = useStation().data;
  const [menu, setMenu] = useState<{ readonly id: string; readonly x: number; readonly y: number } | null>(null);
  const [infoId, setInfoId] = useState<string | null>(null);
  const [draggedId, setDraggedId] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [naming, setNaming] = useState<{ readonly id: string; readonly value: string } | null>(null);
  const [creating, setCreating] = useState(false);
  const [kept, setKept] = useState<string | null>(null);
  const [peeks, setPeeks] = useState<readonly string[]>([]);
  const [chatNaming, setChatNaming] = useState<{ readonly id: string; readonly value: string } | null>(null);
  const fold = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const dwell = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const picking = useRef(new Set<string>());
  useEffect(() => () => { clearTimeout(fold.current); clearTimeout(dwell.current); }, []);
  const info = useQuery({
    queryKey: ["workstream-info", infoId], queryFn: () => workstreamInfo(infoId!), enabled: infoId !== null, staleTime: 30_000,
  });
  const ordered = useMemo(() => workstreams.filter((each) => !each.hidden), [workstreams]);
  const currentStream = current ? ordered.find((stream) => belongs(current, stream)) : undefined;
  // A group a click kept out stays out only while the chat on show is in it.
  const keptOpen = kept !== null && kept === currentStream?.id ? kept : null;

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
      queryClient.invalidateQueries({ queryKey: ["workstream-info"] }),
    ]);
  }

  async function pick(stream: Workstream): Promise<void> {
    if (picking.current.has(stream.id)) return;
    picking.current.add(stream.id);
    const inStream = stations.filter((each) => belongs(each, stream) && !each.hidden);
    const pinned = inStream.filter((each) => each.pinned).sort(byRecent)[0];
    const chosen = pinned ?? [...inStream].sort(byRecent)[0];
    try {
      setProblem(null);
      if (chosen) await openStation(queryClient, chosen.id);
      else showStation(queryClient, await createStation(stream.id));
      await refresh();
    } catch {
      setProblem("Could not open that workstream.");
    } finally {
      picking.current.delete(stream.id);
    }
  }

  function hover(id: string | null): void {
    clearTimeout(fold.current);
    clearTimeout(dwell.current);
    if (id === null) fold.current = setTimeout(() => setPeeks([]), FOLD_AFTER_MS);
    else dwell.current = setTimeout(() => setPeeks((open) => peekAt(open, id, ordered)), PEEK_AFTER_MS);
  }

  function pressTitle(stream: Workstream): void {
    if (stream.id !== currentStream?.id) {
      setKept(stream.id);
      void pick(stream);
    } else if (keptOpen === stream.id) {
      setKept(null);
      setPeeks((open) => open.filter((id) => id !== stream.id));
    } else setKept(stream.id);
  }

  async function openChat(stream: Workstream, id: string): Promise<void> {
    setKept(stream.id);
    try {
      setProblem(null);
      await openStation(queryClient, id);
    } catch {
      setProblem("Could not open that chat.");
    }
  }

  async function newChatIn(stream: Workstream): Promise<void> {
    setKept(stream.id);
    try {
      setProblem(null);
      const state = await createStation(stream.id);
      showStation(queryClient, state);
      setChatNaming({ id: state.id, value: state.name });
      void refresh();
    } catch {
      setProblem("Could not make a chat.");
    }
  }

  async function keepChatName(): Promise<void> {
    if (!chatNaming) return;
    const name = chatNaming.value.trim();
    const was = stations.find((each) => each.id === chatNaming.id)?.name ?? (current?.id === chatNaming.id ? current.name : undefined);
    setChatNaming(null);
    if (!name || name === was) return;
    try {
      setProblem(null);
      await nameStation(queryClient, chatNaming.id, name);
    } catch (error) {
      setProblem(error instanceof ApiError && error.status === 409 ? "You already have a chat with that name" : "Could not rename that chat.");
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

  async function persistPosition(placed: readonly Workstream[]): Promise<void> {
    try {
      setProblem(null);
      await setWorkstreamOrder(placed.map((each) => each.id));
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
    void persistPosition(placed);
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
    void persistPosition(placeRelative(workstreams, id, target.id, after));
    setDraggedId(null);
  }

  async function create(): Promise<void> {
    if (creating) return;
    setCreating(true);
    setProblem(null);
    try {
      const names = new Set(workstreams.map((stream) => stream.title));
      let number = 1;
      while (names.has(`Workstream ${number}`)) number += 1;
      const stream = await createWorkstream(`Workstream ${number}`);
      await queryClient.cancelQueries({ queryKey: WORKSTREAMS_KEY });
      queryClient.setQueryData<Workstream[]>(WORKSTREAMS_KEY, (previous = []) => [...previous, stream]);
      setNaming({ id: stream.id, value: stream.title });
      setKept(stream.id);
      await pick(stream);
    } catch {
      setProblem("Could not create that workstream.");
    } finally {
      setCreating(false);
    }
  }

  function rename(stream: Workstream): void {
    setMenu(null);
    setNaming({ id: stream.id, value: stream.title });
  }

  function keepName(): void {
    if (!naming) return;
    const title = naming.value.trim();
    setNaming(null);
    if (title && title !== workstreams.find((stream) => stream.id === naming.id)?.title) {
      void patch(naming.id, { title });
    }
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
      <nav className="workstream-bar" aria-label="Workstreams" onPointerLeave={() => hover(null)}>
        {ordered.map((stream) => {
          const isCurrent = stream.id === currentStream?.id;
          const out = keptOpen === stream.id || peeks.includes(stream.id);
          const chats = chatsIn(stations, stream, current);
          return (
          <span key={stream.id} className="workstream-slot">
          <div role="group" aria-label={stream.title} className={`workstream-group${out ? " is-open" : ""}${isCurrent ? " is-current" : ""}`}
            style={{ "--group": `var(--group-${colourOf(stream.id)})` } as CSSProperties}
            onPointerEnter={() => hover(stream.id)} onFocus={() => hover(stream.id)}
            onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) hover(null); }}>
          {stream.id === naming?.id ? (
            <input className="workstream-title workstream-name" aria-label="Workstream name" maxLength={120}
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
          <button type="button" className={`workstream-title${isCurrent ? " is-current" : ""}${draggedId === stream.id ? " is-dragged" : ""}`}
              aria-label={stream.title} aria-expanded={out} title={`Workstream ${stream.title} (drag to move, double-click to rename)`} draggable
              onClick={() => pressTitle(stream)}
              onDoubleClick={() => rename(stream)}
              onContextMenu={(event) => { event.preventDefault(); setMenu({ id: stream.id, x: event.clientX, y: event.clientY }); }}
              onDragStart={(event) => dragStart(event, stream.id)} onDragOver={(event) => event.preventDefault()} onDrop={(event) => drop(event, stream)}
              onDragEnd={() => setDraggedId(null)}>
              <span className="workstream-mark" aria-hidden="true"><Icon name="layers" size={12} /></span>
              <span className="workstream-title-text">{stream.title}</span>
              {isCurrent && !out && current && <span className="workstream-here" aria-hidden="true">· {current.name}</span>}
            </button>
          )}
          <GroupChats out={out}>
            {chats.map((chat, at) => chat.id === chatNaming?.id ? (
              <span key={chat.id} className="workstream-chat-slot" style={{ "--i": at } as CSSProperties}>
                <input className="workstream-chat-name" aria-label="Chat name" maxLength={MAX_CHAT_NAME} value={chatNaming.value} autoFocus
                  onFocus={(event) => event.currentTarget.select()}
                  onChange={(event) => setChatNaming({ id: chat.id, value: event.target.value })}
                  onBlur={() => void keepChatName()} onKeyDown={(event) => {
                    if (event.nativeEvent.isComposing) return;
                    if (event.key === "Enter") { event.preventDefault(); void keepChatName(); }
                    else if (event.key === "Escape") { event.stopPropagation(); setChatNaming(null); }
                  }} />
              </span>
            ) : (
              <button key={chat.id} type="button" className="workstream-chat" style={{ "--i": at } as CSSProperties}
                aria-current={chat.id === current?.id ? "true" : undefined} title={`${chat.name} (double-click to rename)`}
                onClick={() => void openChat(stream, chat.id)}
                onDoubleClick={() => setChatNaming({ id: chat.id, value: chat.name })}>{chat.name}</button>
            ))}
            <button type="button" className="workstream-chat-new" style={{ "--i": chats.length } as CSSProperties}
              aria-label={`New chat in ${stream.title}`} title="New chat" onClick={() => void newChatIn(stream)}><Icon name="plus" size={13} /></button>
          </GroupChats>
          </div>
          </span>
          );
        })}
      </nav>
      <button type="button" className="workstream-new" aria-label="New workstream" title="New workstream" disabled={creating} onClick={() => void create()}>
        <Icon name="plus" size={13} /><span>Workstream</span>
      </button>
      {menu && workstreams.find((each) => each.id === menu.id) && (() => {
        const stream = workstreams.find((each) => each.id === menu.id)!;
        const at = ordered.findIndex((each) => each.id === stream.id);
        return <div className="topbar-pop workstream-pop workstream-menu" role="menu" aria-label={`${stream.title} actions`}
          style={{ left: menu.x, top: menu.y }}>
          <button type="button" role="menuitem" className="topbar-item" onClick={() => void openInfo(stream)}>Info</button>
          <button type="button" role="menuitem" className="topbar-item" disabled={at <= 0} onClick={() => move(stream.id, -1)}>Move left</button>
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

/**
 * A group's chats behind its title: their width springs open to what they need
 * and back to nothing, each chat a beat behind the one before (topbar.css).
 * Folded, they are inert, so Tab passes them by.
 */
function GroupChats({ out, children }: { readonly out: boolean; readonly children: ReactNode }) {
  const outer = useRef<HTMLDivElement>(null);
  const inner = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (outer.current && inner.current) outer.current.style.width = out ? `${inner.current.scrollWidth}px` : "0px";
  });
  return (
    <div ref={outer} className="workstream-chats" inert={!out}>
      <div ref={inner} className="workstream-chats-inner">{children}</div>
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
      <a href={`#/lookup/${info.id}`}>Workstream settings</a>
      <h3>Chats</h3>
      {info.stations.length ? <ul>{info.stations.map((each) => <li key={each.id}>
        <span>{each.name}{each.hidden ? " · hidden" : ""}</span>
        {each.session_ids.map((session) => <a className="workstream-session" href={`#/lookup/${session}`} key={session}>{session}</a>)}
      </li>)}</ul> : <p>No chats in this workstream.</p>}
    </aside>
  );
}

/** A workstream's colour, 1 to `COLOURS`, from its id alone: it keeps it wherever it moves. */
export function colourOf(id: string): number {
  let hash = 0;
  for (const char of id) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return (hash % COLOURS) + 1;
}

/**
 * The groups out once the pointer rests on `id`: it, and those to its left.
 * Folding a group left of the pointer would slide the title it rests on away
 * under it, onto another, which would open in turn; folding those to its right
 * moves nothing under it.
 */
export function peekAt(open: readonly string[], id: string, order: readonly { readonly id: string }[]): readonly string[] {
  const at = order.findIndex((stream) => stream.id === id);
  return [...open.filter((each) => each !== id && order.findIndex((stream) => stream.id === each) < at), id];
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
