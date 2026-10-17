import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { createStation, createWorkstream, listWorkstreams, moveStation, type Workstream } from "../api/workstreams";
import { hideStation as setStationHidden, listStations, pinStation, type StationSummary } from "../api/stations";
import type { ChatHead } from "../api/chats";
import { getInquiry } from "../api/inquiries";
import type { WorkspaceState } from "../api/workspaces";
import { hideStation, nameStation, openStation, STATIONS_KEY } from "../stations/actions";
import { showStation } from "../stations/useStation";
import { chatsIn } from "../stations/workstreamChats";
import { setChatHidden, setChatSide, useChatColumn } from "./chatColumnState";
import { useHoverOpen } from "./hoverOpen";
import { DeleteChat, PrivacySwitch, ShareChat } from "./ChatSharing";

type Props = {
  readonly workspace: WorkspaceState | null;
  readonly head: ChatHead | undefined;
  readonly onHide: () => Promise<void>;
  readonly onClear: () => Promise<void>;
  readonly onDeleted: (conversation: string) => void;
};

type HeaderMenu = "name" | "workstream" | "more" | "info" | "find";

/**
 * Chat naming, workstream placement, and its compact header controls. A click
 * on the name renames it in place; the pointer resting on it drops down the
 * workstream's chats and + for a new one, the list the top bar folds behind the
 * workstream's title (`chatsIn`). ⋯ holds the rest: pin or hide this chat, find
 * any chat (hidden ones too), clear, the panel, delete, info, privacy, sharing.
 */
export function ChatWorkstreamHeader({ workspace, head, onHide, onClear, onDeleted }: Props) {
  const queryClient = useQueryClient();
  const { side, hidden } = useChatColumn();
  const [menu, setMenu] = useState<HeaderMenu | null>(null);
  const [nameDraft, setNameDraft] = useState("");
  const [nameProblem, setNameProblem] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [workstreamQuery, setWorkstreamQuery] = useState("");
  const [dismissedSuggestion, setDismissedSuggestion] = useState(false);
  const nameOpen = menu === "name";
  const workstreamOpen = menu === "workstream";
  const moreOpen = menu === "more";
  const infoOpen = menu === "info";
  const findOpen = menu === "find";
  const root = useRef<HTMLDivElement>(null);
  const renameInput = useRef<HTMLInputElement>(null);
  const workstreamInput = useRef<HTMLInputElement>(null);
  const findInput = useRef<HTMLInputElement>(null);
  const namePick = useRef<HTMLDivElement>(null);
  const chatList = useHoverOpen(namePick, { enabled: !nameOpen });
  const listing = chatList.open && !nameOpen;
  const stations = useQuery({ queryKey: STATIONS_KEY, queryFn: ({ signal }) => listStations({ signal }), enabled: findOpen || listing, staleTime: 2_000 });
  const workstreams = useQuery({ queryKey: ["workstreams"], queryFn: ({ signal }) => listWorkstreams({}, { signal }),
    enabled: workstreamOpen || listing || workspace?.suggestion != null, staleTime: 2_000 });
  const madeFor = workspace?.session_id ?? null;
  const madeForSession = useQuery({ queryKey: ["inquiry", madeFor], queryFn: ({ signal }) => getInquiry(madeFor ?? "", { signal }),
    enabled: infoOpen && madeFor !== null, staleTime: 5_000 });
  const made = madeForSession.data;
  const madeCli = typeof made?.cli === "string" ? made.cli : "session";
  const madeActor = typeof made?.owner === "string" ? ` as ${made.owner}` : "";
  const rows = stations.data ?? [];
  const groups = workstreams.data ?? [];
  const currentGroup = groups.find((each) => each.id === workspace?.workstream_id)
    ?? groups.find((each) => each.scratch);
  const suggestion = workspace?.suggestion && !dismissedSuggestion ? workspace.suggestion : null;
  const shownName = workspace?.name && /^unnamed-workstream#\d+$/.test(workspace.name) ? "Scratch chat" : workspace?.name ?? "Chat";
  const listed = currentGroup ? chatsIn(rows, currentGroup, workspace) : [];
  const searchedRows = rows.filter((each) => each.name.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  const workstreamMatches = groups.filter((each) => !each.hidden && each.title.toLocaleLowerCase().includes(workstreamQuery.trim().toLocaleLowerCase()));

  useEffect(() => {
    if (nameOpen) renameInput.current?.focus();
  }, [nameOpen]);
  useEffect(() => {
    if (workstreamOpen) workstreamInput.current?.focus();
  }, [workstreamOpen]);
  useEffect(() => {
    if (findOpen) findInput.current?.focus();
  }, [findOpen]);
  useEffect(() => {
    if (!nameOpen && !workstreamOpen && !moreOpen && !infoOpen && !findOpen) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && root.current?.contains(event.target)) return;
      if (nameOpen) void saveName();
      else setMenu(null);
    };
    document.addEventListener("pointerdown", outside, true);
    return () => document.removeEventListener("pointerdown", outside, true);
  }, [menu, nameDraft]);

  function closeMenu(closing: HeaderMenu) {
    setMenu((current) => current === closing ? null : current);
  }

  async function saveName(): Promise<void> {
    if (!workspace) return;
    const name = nameDraft.trim();
    if (!name) return;
    try {
      await nameStation(queryClient, workspace.id, name);
      setNameProblem(null);
      closeMenu("name");
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) setNameProblem("You already have a chat with that name");
      else setNameProblem("Could not rename this chat.");
    }
  }

  async function openChat(station: StationSummary): Promise<void> {
    try {
      if (station.hidden) {
        const restored = await setStationHidden(station.id, false);
        showStation(queryClient, restored);
        void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
      } else await openStation(queryClient, station.id);
      closeMenu("find");
    } catch {
      setNameProblem("Could not open this chat.");
    }
  }

  async function switchTo(id: string): Promise<void> {
    chatList.set(false);
    try {
      await openStation(queryClient, id);
    } catch {
      setNameProblem("Could not open this chat.");
    }
  }

  /** Make a chat in this workstream and open it with its name ready to type, as the top bar's + does. */
  async function newChat(): Promise<void> {
    chatList.set(false);
    try {
      const state = await createStation(currentGroup?.id);
      showStation(queryClient, state);
      void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
      setNameDraft(state.name);
      setMenu("name");
    } catch {
      setNameProblem("Could not make a chat.");
    }
  }

  async function pinChat(): Promise<void> {
    if (!workspace) return;
    const state = await pinStation(workspace.id, !workspace.pinned);
    queryClient.setQueryData(["workspace", workspace.id], state);
    void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
  }

  async function hideChat(): Promise<void> {
    if (!workspace) return;
    await hideStation(queryClient, workspace.id);
    void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
  }

  async function placeIn(group: Workstream): Promise<void> {
    if (!workspace) return;
    const state = await moveStation(workspace.id, group.id);
    showStation(queryClient, state);
    void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
    closeMenu("workstream");
    setDismissedSuggestion(true);
  }

  async function chooseWorkstream(): Promise<void> {
    const query = workstreamQuery.trim();
    const exact = query
      ? workstreamMatches.find((each) => each.title.toLocaleLowerCase() === query.toLocaleLowerCase())
      : workstreamMatches[0];
    if (exact) await placeIn(exact);
    else if (query) {
      const created = await createWorkstream(query);
      void queryClient.invalidateQueries({ queryKey: ["workstreams"] });
      await placeIn(created);
    }
  }

  async function followSuggestion(): Promise<void> {
    if (!suggestion) return;
    const available = await queryClient.fetchQuery({ queryKey: ["workstreams"],
      queryFn: ({ signal }) => listWorkstreams({}, { signal }), staleTime: 0 });
    const target = available.find((each) => each.id === suggestion.workstream_id);
    if (target) await placeIn(target);
    else {
      setWorkstreamQuery(suggestion.title);
      setMenu("workstream");
    }
  }

  const onDelete = () => {
    if (!head) return;
    onDeleted(head.conversation_id);
    void onHide();
  };

  return <header className="chat-head" ref={root}>
    <div className="chat-partner" role="group" aria-label="Chat partner">
      <span className="chat-partner-context" aria-hidden="true">
        {workspace && <><strong>Chat</strong><span className="chat-partner-note">{workspace.partner?.kind === "shared"
          ? workspace.partner.actor ?? "assistant"
          : workspace.partner?.actor ? `your local helper (${workspace.partner.actor})` : "your local helper"}</span></>}
        {head?.audience === "staff" && <span>Staff only</span>}
        {head?.audience === "private" && <span>Private</span>}
      </span>
      <div className="chat-name-pick" ref={namePick}>
        {nameOpen ? <input ref={renameInput} className="chat-name-input" aria-label="Chat name" value={nameDraft}
          onChange={(event) => { setNameDraft(event.target.value); setNameProblem(null); }}
          onKeyDown={(event) => {
            if (event.nativeEvent.isComposing) return;
            if (event.key === "Enter") { event.preventDefault(); void saveName(); }
            if (event.key === "Escape") { event.preventDefault(); setMenu(null); setNameProblem(null); }
          }} />
          : <button type="button" className="chat-name-button" title="Rename this chat" aria-label={shownName}
            onClick={() => { setNameDraft(workspace?.name ?? ""); setNameProblem(null); setMenu("name"); }}>{shownName}</button>}
        {nameProblem && <p className="station-problem chat-name-problem" role="status">{nameProblem}</p>}
        {listing && <div className="station-menu chat-name-menu" role="dialog" aria-label={`Chats in ${currentGroup?.title ?? "Scratch"}`}>
          <ul className="station-list">
            {listed.map((chat) => <li key={chat.id}>
              <button type="button" className="station-option" aria-current={chat.id === workspace?.id ? "true" : undefined}
                onClick={() => void (chat.id === workspace?.id ? chatList.set(false) : switchTo(chat.id))}>{chat.name}</button>
            </li>)}
          </ul>
          <button type="button" className="station-option" onClick={() => void newChat()}>+ New chat</button>
        </div>}
      </div>
    </div>
    <div className="chat-workstream-pick">
      {workstreamOpen ? <div className="chat-workstream-menu">
        <input ref={workstreamInput} aria-label="Workstream" placeholder="Move to workstream…" value={workstreamQuery}
          onChange={(event) => setWorkstreamQuery(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); void chooseWorkstream(); } if (event.key === "Escape") setMenu(null); }} />
        <ul className="station-list" aria-label="Workstreams">
          {workstreamMatches.map((group) => <li key={group.id}><button type="button" className="station-option" onClick={() => void placeIn(group)}>{group.title}</button></li>)}
          {workstreamQuery.trim() && !workstreamMatches.some((each) => each.title.toLocaleLowerCase() === workstreamQuery.trim().toLocaleLowerCase())
            && <li><button type="button" className="station-option" onClick={() => void chooseWorkstream()}>Create “{workstreamQuery.trim()}”</button></li>}
        </ul>
      </div>
        : <button type="button" className="chat-workstream-chip hover-only" aria-label={`Workstream: ${currentGroup?.title ?? "Scratch"}`}
          onClick={() => { setWorkstreamQuery(""); setMenu("workstream"); }}>{currentGroup?.title ?? "Scratch"} ▾</button>}
      {suggestion && <span className="chat-suggestion-chip">
        <button type="button" aria-label={`Move to suggested workstream ${suggestion.title}`} onClick={() => void followSuggestion()}>{suggestion.title}?</button>
        <button type="button" aria-label="Dismiss workstream suggestion" onClick={() => setDismissedSuggestion(true)}>×</button>
      </span>}
    </div>
    <div className="chat-head-actions" role="toolbar" aria-label="Chat controls">
      <button className="btn ghost hover-only" type="button" aria-label={`Move Chat to the ${side === "left" ? "right" : "left"}`}
        title="Dock on the other side" onClick={() => setChatSide(side === "left" ? "right" : "left")}>{side === "left" ? "▷" : "◁"}</button>
      <button className="btn ghost hover-only" type="button" aria-label="More chat actions" aria-expanded={moreOpen} onClick={() => setMenu(moreOpen ? null : "more")}>⋯</button>
      {moreOpen && <div className="chat-more-menu" aria-label="Chat actions">
        <button className="chat-menu-item" type="button" title="New chat, same canvas" onClick={() => { setMenu(null); void onClear(); }}>Clear chat</button>
        {workspace && <button className="chat-menu-item" type="button" onClick={() => { setMenu(null); void pinChat(); }}>{workspace.pinned ? "Unpin chat" : "Pin chat"}</button>}
        {workspace && <button className="chat-menu-item" type="button" title="Hide this chat from the bar; Find a chat brings it back" onClick={() => { setMenu(null); void hideChat(); }}>Hide chat</button>}
        <button className="chat-menu-item" type="button" onClick={() => { setSearch(""); setMenu("find"); }}>Find a chat…</button>
        {hidden
          ? <button className="chat-menu-item" type="button" aria-label="Pin Chat beside the page" title="Pin Chat beside the page" onClick={() => { setMenu(null); setChatHidden(false); }}>Pin panel</button>
          : <button className="chat-menu-item" type="button" aria-label="Hide Chat" title="Hide the chat panel" onClick={() => { setMenu(null); setChatHidden(true); }}>Hide panel</button>}
        {head?.may_delete && <DeleteChat head={head} onDeleted={onDelete} />}
        <button className="chat-menu-item" type="button" onClick={() => setMenu("info")}>Info</button>
        {head?.may_hide && <PrivacySwitch head={head} />}
        {head?.may_share && <ShareChat head={head} />}
      </div>}
      {findOpen && <div className="chat-more-menu chat-find-menu" role="dialog" aria-label="Find a chat">
        <input ref={findInput} className="chat-name-search" aria-label="Search chats" placeholder="Search all chats…" value={search}
          onChange={(event) => setSearch(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && searchedRows[0]) void openChat(searchedRows[0]);
            if (event.key === "Escape") setMenu(null);
          }} />
        {search && <ul className="station-list" aria-label="Search results">
          {searchedRows.map((station) => <li key={station.id}>
            <button type="button" className="station-option" onClick={() => void openChat(station)}>{station.name}{station.hidden ? " · hidden" : ""}</button>
          </li>)}
          {searchedRows.length === 0 && <li className="muted">No matches.</li>}
        </ul>}
      </div>}
      {infoOpen && <div className="chat-info-menu" role="dialog" aria-label="Chat info">
        <button type="button" className="btn ghost" onClick={() => setMenu(null)}>Close</button>
        <strong>{shownName}</strong>
        <span>Chat ID: {workspace?.id ?? "—"}</span>
        <span>Conversation: {head?.conversation_id ?? "—"}</span>
        <span>Session: {head?.session_id ?? "—"}</span>
        {madeFor !== null && <span>Made for: <a href={`#/lookup/${madeFor}`}>{`${madeCli}${madeActor}`}</a>
          {made ? (made.ended ? " · ended" : " · live") : ""}</span>}
      </div>}
    </div>
  </header>;
}
