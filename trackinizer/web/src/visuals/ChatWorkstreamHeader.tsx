import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { createStation as createStationInWorkstream, createWorkstream, listWorkstreams, moveStation, type Workstream } from "../api/workstreams";
import { hideStation as setStationHidden, listStations, pinStation, type StationSummary } from "../api/stations";
import type { ChatHead } from "../api/chats";
import type { WorkspaceState } from "../api/workspaces";
import { hideStation, nameStation, openStation, STATIONS_KEY } from "../stations/actions";
import { showStation } from "../stations/useStation";
import { setChatHidden, setChatSide, useChatColumn } from "./chatColumnState";
import { DeleteChat, PrivacySwitch, ShareChat } from "./ChatSharing";

type Props = {
  readonly workspace: WorkspaceState | null;
  readonly head: ChatHead | undefined;
  readonly onHide: () => Promise<void>;
  readonly onClear: () => Promise<void>;
  readonly onDeleted: (conversation: string) => void;
};

/** Chat naming, switching, workstream placement, and its compact header controls. */
export function ChatWorkstreamHeader({ workspace, head, onHide, onClear, onDeleted }: Props) {
  const queryClient = useQueryClient();
  const { side, hidden } = useChatColumn();
  const [nameOpen, setNameOpen] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [nameProblem, setNameProblem] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [workstreamOpen, setWorkstreamOpen] = useState(false);
  const [workstreamQuery, setWorkstreamQuery] = useState("");
  const [moreOpen, setMoreOpen] = useState(false);
  const [infoOpen, setInfoOpen] = useState(false);
  const [dismissedSuggestion, setDismissedSuggestion] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const renameInput = useRef<HTMLInputElement>(null);
  const workstreamInput = useRef<HTMLInputElement>(null);
  const stations = useQuery({ queryKey: STATIONS_KEY, queryFn: ({ signal }) => listStations({ signal }), enabled: nameOpen, staleTime: 2_000 });
  const workstreams = useQuery({ queryKey: ["workstreams"], queryFn: ({ signal }) => listWorkstreams({}, { signal }),
    enabled: workstreamOpen || workspace?.suggestion != null, staleTime: 2_000 });
  const rows = stations.data ?? [];
  const groups = workstreams.data ?? [];
  const currentGroup = groups.find((each) => each.id === workspace?.workstream_id)
    ?? groups.find((each) => each.scratch);
  const currentGroupId = currentGroup?.id;
  const suggestion = workspace?.suggestion && !dismissedSuggestion ? workspace.suggestion : null;
  const shownName = workspace?.name && /^unnamed-workstream#\d+$/.test(workspace.name) ? "Scratch chat" : workspace?.name ?? "Chat";
  const inGroup = rows.filter((each) => (workspace?.workstream_id ? each.workstream_id === workspace.workstream_id : !each.workstream_id) && !each.hidden);
  const recent = inGroup.toSorted((a, b) => (b.modified_at ?? "").localeCompare(a.modified_at ?? ""));
  const chatRows = [...recent.filter((each) => each.pinned), ...recent.filter((each) => !each.pinned).slice(0, 5)];
  const searchedRows = rows.filter((each) => each.name.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  const workstreamMatches = groups.filter((each) => !each.hidden && each.title.toLocaleLowerCase().includes(workstreamQuery.trim().toLocaleLowerCase()));

  useEffect(() => {
    if (nameOpen) renameInput.current?.focus();
  }, [nameOpen]);
  useEffect(() => {
    if (workstreamOpen) workstreamInput.current?.focus();
  }, [workstreamOpen]);
  useEffect(() => {
    if (!nameOpen && !workstreamOpen && !moreOpen && !infoOpen) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && root.current?.contains(event.target)) return;
      if (nameOpen) void saveName();
      setWorkstreamOpen(false);
      setMoreOpen(false);
      setInfoOpen(false);
    };
    document.addEventListener("pointerdown", outside, true);
    return () => document.removeEventListener("pointerdown", outside, true);
  }, [infoOpen, moreOpen, nameOpen, workstreamOpen, nameDraft]);

  async function saveName(): Promise<void> {
    if (!workspace) return;
    const name = nameDraft.trim();
    if (!name) return;
    try {
      await nameStation(queryClient, workspace.id, name);
      setNameProblem(null);
      setNameOpen(false);
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
      setNameOpen(false);
    } catch {
      setNameProblem("Could not open this chat.");
    }
  }

  async function pinChat(station: StationSummary): Promise<void> {
    const state = await pinStation(station.id, !station.pinned);
    if (station.id === workspace?.id) queryClient.setQueryData(["workspace", station.id], state);
    void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
  }

  async function hideChat(station: StationSummary): Promise<void> {
    await hideStation(queryClient, station.id);
    void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
  }

  async function createChat(): Promise<void> {
    if (!workspace) return;
    const state = await createStationInWorkstream(currentGroupId);
    showStation(queryClient, state);
    void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
    setNameOpen(false);
  }

  async function placeIn(group: Workstream): Promise<void> {
    if (!workspace) return;
    const state = await moveStation(workspace.id, group.id);
    showStation(queryClient, state);
    void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
    setWorkstreamOpen(false);
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
      setWorkstreamOpen(true);
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
      <div className="chat-name-pick">
        {nameOpen ? <input ref={renameInput} className="chat-name-input" aria-label="Chat name" value={nameDraft}
          onChange={(event) => { setNameDraft(event.target.value); setNameProblem(null); }}
          onKeyDown={(event) => {
            if (event.key === "Enter") { event.preventDefault(); void saveName(); }
            if (event.key === "Escape") { event.preventDefault(); setNameOpen(false); setNameProblem(null); }
            if (event.key === "ArrowDown") { event.preventDefault(); (root.current?.querySelector<HTMLInputElement>(".chat-name-search"))?.focus(); }
          }} />
          : <button type="button" className="chat-name-button" title="Rename, switch chats or search" aria-label={shownName}
            onClick={() => { setNameDraft(workspace?.name ?? ""); setNameProblem(null); setNameOpen(true); }}>{shownName}<span className="chat-name-caret hover-only">▾</span></button>}
        {nameOpen && <div className="station-menu chat-name-menu" role="dialog" aria-label="Chats">
          {nameProblem && <p className="station-problem" role="status">{nameProblem}</p>}
          <p className="chat-menu-label">Chats in {currentGroup?.title ?? "Scratch"}</p>
          <ul className="station-list" aria-label="Chats in this workstream">
            {chatRows.map((station) => <li key={station.id} className="chat-name-row">
              <button type="button" className="station-option" aria-current={station.id === workspace?.id} onClick={() => void openChat(station)}>{station.name}</button>
              <span className="chat-name-row-actions">
                <button type="button" className="btn ghost" aria-label={`${station.pinned ? "Unpin" : "Pin"} ${station.name}`} onClick={() => void pinChat(station)}>{station.pinned ? "📌" : "Pin"}</button>
                <button type="button" className="btn ghost" aria-label={`Hide ${station.name}`} onClick={() => void hideChat(station)}>Hide</button>
              </span>
            </li>)}
            {chatRows.length === 0 && <li className="muted">No chats yet.</li>}
          </ul>
          <button type="button" className="station-option" onClick={() => void createChat()}>+ New chat</button>
          <div className="chat-name-search">
            <input className="chat-name-search" aria-label="Search chats" placeholder="Search all chats…" value={search}
              onChange={(event) => setSearch(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Enter" && searchedRows[0]) void openChat(searchedRows[0]); }} />
            {search && <ul className="station-list" aria-label="Search results">
              {searchedRows.map((station) => <li key={station.id}>
                <button type="button" className="station-option" onClick={() => void openChat(station)}>{station.name}{station.hidden ? " · hidden" : ""}</button>
              </li>)}
              {searchedRows.length === 0 && <li className="muted">No matches.</li>}
            </ul>}
          </div>
        </div>}
      </div>
    </div>
    <div className="chat-workstream-pick">
      {workstreamOpen ? <div className="chat-workstream-menu">
        <input ref={workstreamInput} aria-label="Workstream" placeholder="Move to workstream…" value={workstreamQuery}
          onChange={(event) => setWorkstreamQuery(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); void chooseWorkstream(); } if (event.key === "Escape") setWorkstreamOpen(false); }} />
        <ul className="station-list" aria-label="Workstreams">
          {workstreamMatches.map((group) => <li key={group.id}><button type="button" className="station-option" onClick={() => void placeIn(group)}>{group.title}</button></li>)}
          {workstreamQuery.trim() && !workstreamMatches.some((each) => each.title.toLocaleLowerCase() === workstreamQuery.trim().toLocaleLowerCase())
            && <li><button type="button" className="station-option" onClick={() => void chooseWorkstream()}>Create “{workstreamQuery.trim()}”</button></li>}
        </ul>
      </div>
        : <button type="button" className="chat-workstream-chip hover-only" aria-label={`Workstream: ${currentGroup?.title ?? "Scratch"}`}
          onClick={() => { setWorkstreamQuery(""); setWorkstreamOpen(true); }}>{currentGroup?.title ?? "Scratch"} ▾</button>}
      {suggestion && <span className="chat-suggestion-chip">
        <button type="button" aria-label={`Move to suggested workstream ${suggestion.title}`} onClick={() => void followSuggestion()}>{suggestion.title}?</button>
        <button type="button" aria-label="Dismiss workstream suggestion" onClick={() => setDismissedSuggestion(true)}>×</button>
      </span>}
    </div>
    <div className="chat-head-actions" role="toolbar" aria-label="Chat controls">
      {workspace && <button className={`btn ghost hover-only${workspace.pinned ? " on" : ""}`} type="button" aria-label={workspace.pinned ? "Unpin chat" : "Pin chat"}
        onClick={() => void pinStation(workspace.id, !workspace.pinned).then((state) => {
          queryClient.setQueryData(["workspace", workspace.id], state);
          return queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
        })}>{workspace.pinned ? "📌" : "Pin"}</button>}
      {hidden
        ? <button className="btn ghost hover-only" type="button" aria-label="Pin Chat beside the page" title="Pin Chat beside the page" onClick={() => setChatHidden(false)}>Pin panel</button>
        : <button className="btn ghost hover-only" type="button" aria-label="Hide Chat" title="Hide the chat panel" onClick={() => setChatHidden(true)}>Hide panel</button>}
      <button className="btn ghost hover-only" type="button" aria-label={`Move Chat to the ${side === "left" ? "right" : "left"}`}
        title="Dock on the other side" onClick={() => setChatSide(side === "left" ? "right" : "left")}>{side === "left" ? "▷" : "◁"}</button>
      <button className="btn ghost hover-only" type="button" aria-label="More chat actions" aria-expanded={moreOpen} onClick={() => setMoreOpen(!moreOpen)}>⋯</button>
      {moreOpen && <div className="chat-more-menu" aria-label="Chat actions">
        <button type="button" title="New chat, same canvas" onClick={() => { setMoreOpen(false); void onClear(); }}>Clear chat</button>
        <button type="button" onClick={() => { setMoreOpen(false); void onHide(); }}>Hide from bar</button>
        {head?.may_delete && <DeleteChat head={head} onDeleted={onDelete} />}
        <button type="button" onClick={() => { setMoreOpen(false); setInfoOpen(true); }}>Info</button>
        {head?.may_hide && <PrivacySwitch head={head} />}
        {head?.may_share && <ShareChat head={head} />}
      </div>}
      {infoOpen && <div className="chat-info-menu" role="dialog" aria-label="Chat info">
        <button type="button" className="btn ghost" onClick={() => setInfoOpen(false)}>Close</button>
        <strong>{shownName}</strong>
        <span>Chat ID: {workspace?.id ?? "—"}</span>
        <span>Conversation: {head?.conversation_id ?? "—"}</span>
        <span>Session: {head?.session_id ?? "—"}</span>
      </div>}
    </div>
  </header>;
}
