import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type CSSProperties, type KeyboardEvent, type ReactNode, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { chatRefusalMessage } from "../api/chatRefusals";
import { deleteChat } from "../api/chats";
import { ApiError } from "../api/client";
import type { StationSummary } from "../api/stations";
import type { WorkspaceState } from "../api/workspaces";
import { closeStation, nameStation, newStation, openStation, stationsQuery } from "../stations/actions";
import { requestTab, useCanvasTabs } from "../stations/canvasTabs";
import { inTabOrder } from "../stations/cycle";
import { StationMenu } from "../stations/StationMenu";
import { useStation } from "../stations/useStation";
import { Icon } from "../ui/icons";
import { useDismiss } from "./useDismiss";

/** A tab as a group shows it: its id and name. */
type TabName = { readonly id: string; readonly name: string };

/** A station as the bar shows it: its tabs, the open one, its chat (none is null), and whether it is the station on show. */
type Group = { readonly id: string; readonly name: string; readonly tabs: readonly TabName[]; readonly open: string; readonly current: boolean; readonly conversation: string | null };

/** A name being edited: a station's or a tab's, and what the field holds. */
type Naming = { readonly what: "station" | "tab"; readonly id: string; readonly value: string };

/** A station the delete confirm is open for: its id, name, and the chat (if any) that goes with it. */
type Pending = { readonly id: string; readonly name: string; readonly conversation: string | null };

/** How long the tabs stay out after the pointer leaves the bar, so a pass between groups does not fold them. */
export const FOLD_AFTER_MS = 140;

/** The tab a station has before a second is made, which its layout does not name (`canvasTabs.ts`). */
const FIRST: TabName = { id: "1", name: "Tab 1" };

/** How many group colours the theme has (`--group-1` to `--group-4`); the groups take them in turn. */
const COLOURS = 4;

/**
 * The user's stations along the top bar, as a browser's tab groups: each a
 * coloured title, its canvas tabs folded behind it. The pointer resting on a
 * group, or the keyboard in it, slides its tabs out; leaving folds them. A
 * click on a title opens its station and keeps its tabs out, and a second
 * click on the open station's folds them. Only the open station's tabs stay
 * out: a group left for another folds, however the station changed. Folded, the open station's title
 * names its open tab. A tab opens with its station; the open station's tabs
 * also rename (double click), close (×) and add (+). A double click on the open
 * station's title renames it. A × by any group deletes that workspace, its chat and
 * canvas with it, behind a confirm; the last one stays. + Station makes one, and the
 * chevron searches them.
 */
export function StationGroups() {
  const queryClient = useQueryClient();
  const listed = useQuery(stationsQuery).data;
  const station = useStation().data ?? null;
  const canvas = useCanvasTabs();
  // The group a click kept out; it stays out only while its station is the one on show.
  const [pinned, setPinned] = useState<string | null>(null);
  const [peek, setPeek] = useState<string | null>(null);
  const [naming, setNaming] = useState<Naming | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  // The station a delete confirm is open for, and whether its delete is on its way.
  const [pending, setPending] = useState<Pending | null>(null);
  const [deleting, setDeleting] = useState(false);
  const confirmRef = useRef<HTMLDivElement>(null);
  // The × a confirm opened from, so Esc gives focus back to it.
  const askedFrom = useRef<HTMLButtonElement | null>(null);
  const closeConfirm = useCallback(() => setPending(null), []);
  useDismiss(pending !== null, closeConfirm, confirmRef, askedFrom);
  const fold = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // What the field holds as the handlers read it: a blur that comes after Enter or Esc settled the name sees null.
  const live = useRef<Naming | null>(null);
  const strip = useRef<HTMLElement>(null);
  // The title or tab a settled name gives focus back to, once the field has gone.
  const [refocus, setRefocus] = useState<string | null>(null);
  useEffect(() => {
    if (!refocus) return;
    strip.current?.querySelector<HTMLElement>(`[data-name-of="${refocus}"]`)?.focus();
    setRefocus(null);
  }, [refocus]);
  if (!station) return null;
  const groups = groupsOf(listed ?? [], station, canvas);
  const expanded = pinned === station.id ? pinned : null;

  function hover(id: string | null) {
    clearTimeout(fold.current);
    if (id) setPeek(id);
    else fold.current = setTimeout(() => setPeek(null), FOLD_AFTER_MS);
  }

  async function open(id: string, tab?: string): Promise<void> {
    try {
      if (tab) requestTab({ station: id, tab });
      await openStation(queryClient, id);
      setProblem(null);
    } catch {
      requestTab(null);
      setProblem("Could not open that station.");
    }
  }

  function pressTitle(group: Group) {
    if (!group.current) {
      setPinned(group.id);
      void open(group.id);
    } else if (expanded === group.id) {
      setPinned(null);
      setPeek(null);
    } else setPinned(group.id);
  }

  function pressTab(group: Group, tab: TabName) {
    setPinned(group.id);
    if (group.current) canvas?.actions.open(tab.id);
    else void open(group.id, tab.id);
  }

  function edit(next: Naming | null) {
    live.current = next;
    setNaming(next);
    setProblem(null);
  }

  function stop(held: Naming) {
    edit(null);
    setRefocus(held.id);
  }

  async function keepName() {
    const held = live.current;
    if (!held) return;
    live.current = null;
    const name = held.value.trim();
    if (held.what === "tab") {
      const was = canvas?.tabs.find((tab) => tab.id === held.id)?.name;
      if (name && name !== was) canvas?.actions.rename(held.id, name.slice(0, MAX_TAB_NAME));
      stop(held);
      return;
    }
    if (!name || name === station?.name) {
      stop(held);
      return;
    }
    try {
      await nameStation(queryClient, held.id, name);
      stop(held);
    } catch (error) {
      live.current = held;
      setProblem(error instanceof ApiError && error.status === 409 ? "That name is taken" : "Could not rename the station.");
    }
  }

  function onNameKey(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      event.preventDefault();
      void keepName();
    } else if (event.key === "Escape" && live.current) {
      event.stopPropagation();
      stop(live.current);
    }
  }

  async function create() {
    try {
      await newStation(queryClient);
      setProblem(null);
    } catch {
      setProblem("Could not make a station.");
    }
  }

  function askDelete(group: Group, from: HTMLButtonElement) {
    askedFrom.current = from;
    setProblem(null);
    setPending({ id: group.id, name: group.name, conversation: group.conversation });
  }

  async function removeStation() {
    const target = pending;
    if (!target || deleting) return;
    setDeleting(true);
    // The server refuses to delete a station that still holds a chat, so its chat goes first; a refusal
    // (someone else has posted, not the starter) stops the delete with the reason Chat would show.
    if (target.conversation) {
      try {
        await deleteChat(target.conversation);
      } catch (error) {
        setDeleting(false);
        setPending(null);
        setProblem(chatRefusalMessage(error, "Could not delete this workspace's chat."));
        return;
      }
    }
    try {
      await closeStation(queryClient, target.id);
      setProblem(null);
    } catch {
      setProblem("Could not delete this workspace.");
    } finally {
      setDeleting(false);
      setPending(null);
    }
  }

  const nameField = (label: string, max: number) => naming && (
    <input className="station-group-name" aria-label={label} maxLength={max} value={naming.value} autoFocus
      onFocus={(event) => event.currentTarget.select()} onChange={(event) => edit({ ...naming, value: event.target.value })}
      onKeyDown={onNameKey} onBlur={() => void keepName()} />
  );

  return (
    <div className="station-groups">
      <nav ref={strip} className="station-groups-strip" aria-label="Stations" onPointerLeave={() => hover(null)}>
        {groups.map((group, index) => {
          const out = expanded === group.id || peek === group.id;
          const openTab = group.tabs.find((tab) => tab.id === group.open);
          const moving = group.current && (canvas?.moving ?? false);
          return (
            <div key={group.id} role="group" aria-label={group.name} style={{ "--group": `var(--group-${(index % COLOURS) + 1})` } as CSSProperties}
              className={`station-group${group.current ? " is-current" : ""}${out ? " is-open" : ""}${expanded === group.id ? " is-expanded" : ""}`}
              onPointerEnter={() => hover(group.id)}
              onFocus={() => hover(group.id)}
              onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) hover(null); }}>
              {naming?.what === "station" && naming.id === group.id ? nameField("Station name", MAX_STATION_NAME) : (
                <button type="button" className="station-group-title" aria-expanded={out} aria-current={group.current ? "true" : undefined}
                  title={`${group.name} · ${group.tabs.length} ${group.tabs.length === 1 ? "tab" : "tabs"}${group.current ? " (double-click to rename)" : ""}`}
                  onClick={() => pressTitle(group)}
                  data-name-of={group.id}
                  onDoubleClick={() => { if (group.current) edit({ what: "station", id: group.id, value: group.name }); }}>
                  <span className="station-group-dot" aria-hidden="true" />
                  <span className="station-group-label">{group.name}</span>
                  <span className="station-group-count" aria-hidden="true">{group.tabs.length}</span>
                  {group.current && !out && openTab && <span className="station-group-here">· {openTab.name}</span>}
                </button>
              )}
              <GroupTabs out={out}>
                {group.tabs.map((tab, at) => naming?.what === "tab" && naming.id === tab.id && group.current ? (
                  <span key={tab.id} className="station-tab-slot" style={{ "--i": at } as CSSProperties}>{nameField("Tab name", MAX_TAB_NAME)}</span>
                ) : (
                  <span key={tab.id} className="station-tab" style={{ "--i": at } as CSSProperties}
                    aria-current={group.current && tab.id === group.open ? "true" : undefined}>
                    <button type="button" className="station-tab-open" disabled={moving && tab.id !== group.open}
                      title={group.current ? `${tab.name} (double-click to rename)` : tab.name}
                      onClick={() => pressTab(group, tab)}
                      data-name-of={tab.id}
                      onDoubleClick={() => { if (group.current) edit({ what: "tab", id: tab.id, value: tab.name }); }}>{tab.name}</button>
                    {group.current && group.tabs.length > 1 && (
                      <button type="button" className="station-tab-close" aria-label={`Close ${tab.name}`} title="Close this tab"
                        disabled={moving} onClick={() => canvas?.actions.close(tab.id)}><Icon name="x" size={11} /></button>
                    )}
                  </span>
                ))}
                {group.current && (
                  <button type="button" className="station-tab-new" style={{ "--i": group.tabs.length } as CSSProperties}
                    aria-label={`New tab in ${group.name}`} title="New tab" disabled={moving || !canvas?.canAdd}
                    onClick={() => canvas?.actions.add()}><Icon name="plus" size={13} /></button>
                )}
              </GroupTabs>
              <button type="button" className="station-group-del" aria-label={`Delete ${group.name}`} title="Delete this workspace"
                aria-haspopup="dialog" aria-expanded={pending?.id === group.id}
                onClick={(event) => askDelete(group, event.currentTarget)}><Icon name="x" size={11} /></button>
            </div>
          );
        })}
      </nav>
      <button type="button" className="station-group-new" aria-label="New station" title="New station" onClick={() => void create()}>
        <Icon name="plus" size={12} />
        <span>Station</span>
      </button>
      <StationMenu station={station} />
      {pending && (
        <div className="station-delete" role="alertdialog" aria-label={`Delete ${pending.name}`} ref={confirmRef}>
          <p className="station-delete-note">{deletePrompt(groups.length <= 1, pending.conversation !== null)}</p>
          <div className="station-delete-actions">
            <button className="btn ghost" type="button" autoFocus disabled={deleting} onClick={closeConfirm}>Cancel</button>
            <button className="btn danger" type="button" disabled={deleting || (groups.length <= 1 && pending.conversation === null)}
              onClick={() => void removeStation()}>Delete</button>
          </div>
        </div>
      )}
      {problem && <p className="station-problem" role="alert">{problem}</p>}
    </div>
  );
}

/**
 * A group's tabs behind its title: their width springs open to what they need
 * and back to nothing, each tab a beat behind the one before (topbar.css).
 * Folded, they are inert, so Tab passes them by.
 */
function GroupTabs({ out, children }: { readonly out: boolean; readonly children: ReactNode }) {
  const outer = useRef<HTMLDivElement>(null);
  const inner = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (outer.current && inner.current) outer.current.style.width = out ? `${inner.current.scrollWidth}px` : "0px";
  });
  return (
    <div ref={outer} className="station-group-tabs" inert={!out}>
      <div ref={inner} className="station-group-tabs-inner">{children}</div>
    </div>
  );
}

/**
 * The groups: every station in the order they were made (by id when made
 * together), so opening one moves none. The one on show is always among them,
 * under its name now, with its tabs as its canvas holds them; another's tabs
 * are as the list last read them.
 */
function groupsOf(listed: readonly StationSummary[], current: WorkspaceState, canvas: ReturnType<typeof useCanvasTabs>): Group[] {
  const ordered = inTabOrder(listed);
  const all = ordered.some((each) => each.id === current.id) ? ordered : [...ordered, null];
  return all.map((each) => {
    if (each === null || each.id === current.id) {
      const held = canvas?.station === current.id ? canvas : null;
      const tabs = held ? held.tabs : current.layout?.tabs?.length ? current.layout.tabs : [FIRST];
      const open = held?.open ?? current.layout?.tab ?? tabs[0]!.id;
      return { id: current.id, name: current.name, tabs, open, current: true, conversation: current.conversation_id };
    }
    const tabs = each.tabs?.length ? each.tabs : [FIRST];
    return { id: each.id, name: each.name, tabs, open: each.tab ?? tabs[0]!.id, current: false, conversation: each.conversation_id };
  });
}

/**
 * What the delete confirm says. A station the user is not down to their last goes
 * entirely, chat and canvas; the last stays (closeStation keeps one open), so the
 * most its delete does is clear the chat it holds, and with no chat there is nothing
 * to do.
 */
function deletePrompt(onlyWorkspace: boolean, hasChat: boolean): string {
  if (!onlyWorkspace) return "Delete this workspace? Its chat and canvas go with it.";
  return hasChat
    ? "This is your only workspace, so it stays — deleting only clears its chat."
    : "This is your only workspace, so it stays. There is nothing to delete.";
}

/** The longest name a station takes (`server/visuals/workspaces.py`). */
const MAX_STATION_NAME = 120;

/** The longest name a tab keeps (`server/visuals/workspaces.py`). */
const MAX_TAB_NAME = 60;
