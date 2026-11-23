import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { createStation as createStationInWorkstream, createWorkstream, listWorkstreams, moveStation, type Workstream } from "../api/workstreams";
import { hideStation as setStationHidden, listStations, pinStation, type StationSummary } from "../api/stations";
import type { ChatHead } from "../api/chats";
import { getInquiry } from "../api/inquiries";
import type { WorkspacePartner, WorkspaceState } from "../api/workspaces";
import { hasAssistant, PROVIDERS, type Provider, providerName, stationProvider } from "../api/providers";
import { Command } from "../settings/ChatPartner";
import { hideStation, nameStation, openStation, STATIONS_KEY } from "../stations/actions";
import { showStation } from "../stations/useStation";
import { beginStationNavigation } from "../stations/store";
import { acceptWorkspace } from "../stations/cache";
import { setChatHidden, setChatSide, useChatColumn } from "./chatColumnState";
import { CloseChat, DeleteChat, PrivacySwitch, ShareChat } from "./ChatSharing";
import { reasonText, useChatDefault, useProviderStatuses } from "./ProviderPicker";
import { Icon } from "../ui/icons";

type Props = {
  readonly workspace: WorkspaceState | null;
  readonly head: ChatHead | undefined;
  /** An agent waits on the person for an answer: the header shows a dot. */
  readonly waiting?: boolean;
  readonly onHide: () => Promise<void>;
  readonly onClear: () => Promise<void>;
  readonly onDeleted: (conversation: string) => void;
};

type HeaderMenu = "name" | "workstream" | "more" | "info";

/**
 * What a chat is called on the screen. A chat nobody has named carries the id the server made
 * it under, `unnamed-workstream#4`, and that is not a name: it reads as "Scratch chat" wherever
 * it appears. The row and the picker both call this, so one chat has one name in both.
 */
function chatLabel(name: string | null | undefined): string | undefined {
  if (!name) return undefined;
  return /^unnamed-workstream#\d+$/.test(name) ? "Scratch chat" : name;
}

/**
 * The strip Chat's column opens with: which chats there are, a search of them, where this one
 * lives, and what you can do to it. What the chat *talks to* is not here — that is the row under
 * the box — so this row is only about the chats themselves.
 */
export function ChatWorkstreamHeader({ workspace, head, waiting = false, onHide, onClear, onDeleted }: Props) {
  const queryClient = useQueryClient();
  const { side, hidden } = useChatColumn();
  const defaults = useChatDefault();
  const statuses = useProviderStatuses(workspace);
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
  const provider = stationProvider(workspace);
  const partner = workspace?.partner ?? null;
  const run = runInfo(provider, partner);
  // New chats are offered the default first, then the others, in the order the card lists them. A server with no
  // assistant offers none.
  const newChatOrder = [defaults.provider, ...PROVIDERS.filter((each) => each !== defaults.provider)]
    .filter((each) => each !== "assistant" || hasAssistant(workspace));
  const root = useRef<HTMLElement>(null);
  const renameInput = useRef<HTMLInputElement>(null);
  const workstreamInput = useRef<HTMLInputElement>(null);
  const stations = useQuery({ queryKey: STATIONS_KEY, queryFn: ({ signal }) => listStations({ signal }), staleTime: 2_000 });
  const workstreams = useQuery({ queryKey: ["workstreams"], queryFn: ({ signal }) => listWorkstreams({}, { signal }),
    enabled: workstreamOpen || workspace?.suggestion != null, staleTime: 2_000 });
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
  const currentGroupId = currentGroup?.id;
  // Never offer the workstream the chat is already in. The server drops a suggestion naming the station's own id, but a
  // station with none sits in Scratch, and a title-only suggestion can repeat the current title.
  const offered = workspace?.suggestion;
  const here = offered?.workstream_id != null
    ? offered.workstream_id === currentGroupId
    : offered?.title.toLocaleLowerCase() === currentGroup?.title.toLocaleLowerCase();
  const suggestion = offered && !dismissedSuggestion && !here ? offered : null;
  const shownName = chatLabel(workspace?.name) ?? "Chat";
  const inGroup = rows.filter((each) => (workspace?.workstream_id ? each.workstream_id === workspace.workstream_id : !each.workstream_id) && !each.hidden);
  const recent = inGroup.toSorted((a, b) => (b.modified_at ?? "").localeCompare(a.modified_at ?? ""));
  const chatRows = [...recent.filter((each) => each.pinned), ...recent.filter((each) => !each.pinned).slice(0, 5)];
  const searchedRows = rows.filter((each) => each.name.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  // One list: this workstream's recent chats, or every chat the search matches while it is typed in.
  const shownRows = search.trim() ? searchedRows : chatRows;
  // Every visible chat on the server, which is what the picker's count is of — the rows above it
  // are this workstream's share.
  const allVisible = rows.filter((each) => !each.hidden);
  const workstreamMatches = groups.filter((each) => !each.hidden && each.title.toLocaleLowerCase().includes(workstreamQuery.trim().toLocaleLowerCase()));

  useEffect(() => {
    // The dialog opens on its search, which is what it is for; renaming is a step down the page.
    if (nameOpen) root.current?.querySelector<HTMLInputElement>(".chat-name-search-input")?.focus();
  }, [nameOpen]);
  useEffect(() => {
    if (workstreamOpen) workstreamInput.current?.focus();
  }, [workstreamOpen]);
  useEffect(() => {
    if (!nameOpen && !workstreamOpen && !moreOpen && !infoOpen) return;
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
    closeMenu("name");
    try {
      if (station.hidden) {
        const current = beginStationNavigation();
        const restored = await setStationHidden(station.id, false);
        acceptWorkspace(queryClient, restored);
        if (current()) showStation(queryClient, restored);
        void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
      } else await openStation(queryClient, station.id);
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

  async function createChat(chosen: Provider): Promise<void> {
    if (!workspace) return;
    const current = beginStationNavigation();
    const state = await createStationInWorkstream(currentGroupId, chosen);
    acceptWorkspace(queryClient, state);
    if (current()) showStation(queryClient, state);
    void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
    closeMenu("name");
  }

  async function placeIn(group: Workstream, current = beginStationNavigation()): Promise<void> {
    if (!workspace) return;
    const state = await moveStation(workspace.id, group.id);
    acceptWorkspace(queryClient, state);
    if (current()) showStation(queryClient, state);
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
      const current = beginStationNavigation();
      const created = await createWorkstream(query);
      void queryClient.invalidateQueries({ queryKey: ["workstreams"] });
      await placeIn(created, current);
    }
  }

  async function followSuggestion(): Promise<void> {
    if (!suggestion) return;
    const current = beginStationNavigation();
    const available = await queryClient.fetchQuery({ queryKey: ["workstreams"],
      queryFn: ({ signal }) => listWorkstreams({}, { signal }), staleTime: 0 });
    const target = available.find((each) => each.id === suggestion.workstream_id);
    if (target) await placeIn(target, current);
    else if (current()) {
      setWorkstreamQuery(suggestion.title);
      setMenu("workstream");
    }
  }

  const onDelete = () => {
    if (!head) return;
    onDeleted(head.conversation_id);
    void onHide();
  };

  /** A search box with nothing in it is a box; with something in it it is the picker, open. */
  function typeSearch(text: string): void {
    setSearch(text);
    if (text.trim()) setMenu("name");
    else closeMenu("name");
  }

  return <header className="chat-head" ref={root}>
    {nameProblem && !nameOpen && <p className="station-problem" role="status">{nameProblem}</p>}
    {/* One row, and one chat named in it: the chat you are in, its chevron, and the find at the
        far end. The chats beside it used to sit here as a strip, which put three names across the
        row before the first word of the conversation; they are all in the picker the chevron
        opens, with their search, so the row carries one name and the rest are a click away. */}
    <span className="chat-partner-context" aria-hidden="true">
      {head?.audience === "staff" && <span>Staff only</span>}
      {head?.audience === "private" && <span>Private</span>}
    </span>
    <span className="chat-mark" aria-hidden="true"><Icon name="chat" size={15} /></span>
    {waiting && <span className="chat-waiting" role="status" aria-label="Waiting for your answer" title="An agent is waiting for your answer" />}
    <div className="chat-sessions" role="group" aria-label="Chat sessions">
      <button type="button" className="chat-session on" aria-label={shownName} aria-current="true"
        title="Switch sessions or search"
        onClick={() => { setNameDraft(workspace?.name ?? ""); setNameProblem(null); setMenu(nameOpen ? null : "name"); }}>
        <span className="nm">{shownName}</span><span className="cv" aria-hidden="true">▾</span>
      </button>
    </div>
    {/* The one dock control. It was an arrow in the toolbar below and a labelled chip here
        at once: two buttons, one action, the same accessible name, so a click had no single
        target. The chip won, and it reads both ways rather than only out of the right dock. */}
    <button type="button" className="chat-head-icon-btn"
      aria-label={`Move Chat to the ${side === "left" ? "right" : "left"}`}
      title="Dock Chat on the other side" onClick={() => setChatSide(side === "left" ? "right" : "left")}>
      <span aria-hidden="true">{side === "left" ? "▷" : "◁"}</span>
    </button>
    <button type="button" className="chat-head-search-btn" aria-label="Search sessions" onClick={() => setMenu(nameOpen ? null : "name")}>
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <circle cx="11" cy="11" r="8" /><path d="M21 21l-4.35-4.35" />
      </svg>
    </button>
    <div className="chat-name-pick">
      {nameOpen && (
        /* The picker is the design's session pop-over, and it is also the only place a chat can be
           renamed, pinned, hidden or started on a chosen provider. Those four were lost when the
           pop-over was restyled and its rows were stand-ins; the rows are the workstream's own chats
           again, and every control is back inside the one dialog. */
        <div className="station-menu chat-name-menu session-popover" role="dialog" aria-label="Chats">
          {nameProblem && <p className="station-problem" role="status">{nameProblem}</p>}
          {/* Search leads, and it narrows the one list under it. It used to sit at the foot of the
              dialog under the New-chat rows, with a second results list of its own, so the field
              that decides what the list shows was below the list and the same chat could be drawn
              twice. One field, at the top, over one list. */}
          <div className="session-pop-search">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
              <circle cx="11" cy="11" r="8" /><path d="M21 21l-4.35-4.35" />
            </svg>
            <input className="chat-name-search-input session-search-input" aria-label="Search chats" placeholder="Search name or agent…"
              value={search}
              onChange={(event) => typeSearch(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && shownRows[0]) { event.preventDefault(); void openChat(shownRows[0]); }
                if (event.key === "Escape") { event.preventDefault(); setMenu(null); setSearch(""); }
              }} />
            <kbd>esc</kbd>
          </div>
          {/* No agent tabs here. A chat list read (`WorkspaceSummary`) carries no provider, so a
              tab per agent can only filter rows that were written in by hand; the tabs went rather
              than stand over real rows and do nothing. They belong here the day the summary says
              which provider a chat talks to. */}
          <p className="chat-menu-label">{search.trim() ? "Matching chats" : `Chats in ${currentGroup?.title ?? "Scratch"}`}</p>
          <ul className="session-pop-list station-list" aria-label="Chats in this workstream">
            {shownRows.map((station) => <li key={station.id} className={`session-pop-item chat-name-row${station.id === workspace?.id ? " is-current" : ""}`}>
              <button type="button" className="station-option" aria-current={station.id === workspace?.id}
                onClick={() => void openChat(station)}>{chatLabel(station.name)}{station.hidden ? " · hidden" : ""}</button>
              <span className="chat-name-row-actions">
                <button type="button" className="btn ghost" aria-label={`${station.pinned ? "Unpin" : "Pin"} ${station.name}`}
                  onClick={() => void pinChat(station)}>{station.pinned ? "📌" : "Pin"}</button>
                <button type="button" className="btn ghost" aria-label={`Hide ${station.name}`}
                  onClick={() => void hideChat(station)}>Hide</button>
              </span>
            </li>)}
            {shownRows.length === 0 && <li className="muted">{search.trim() ? "No chat by that name." : "No chats yet."}</li>}
          </ul>
          {newChatOrder.map((each) => <div key={each} className="provider-new">
            <button type="button" className="station-option" disabled={!statuses[each].ok} onClick={() => void createChat(each)}>
              + New chat · {providerName(each, workspace)}{each === defaults.provider && <span className="provider-mark"> ★ default</span>}</button>
            {!statuses[each].ok && <p className="muted provider-reason">{reasonText(statuses[each])}</p>}
          </div>)}
          {/* Renaming the open chat stays in this dialog — it is the only place that does it —
              but it is a different job from choosing one, so it sits under the list, named. */}
          <div className="chat-name-search">
            <input
              ref={renameInput}
              className="chat-name-input session-search-input"
              aria-label="Chat name"
              placeholder="Name this chat…"
              value={nameDraft}
              onChange={(event) => { setNameDraft(event.target.value); setNameProblem(null); }}
              onKeyDown={(event) => {
                if (event.key === "Enter") { event.preventDefault(); void saveName(); }
                if (event.key === "Escape") { event.preventDefault(); setMenu(null); setNameProblem(null); }
              }}
            />
          </div>
          <div className="session-pop-footer">
            <span>Chats in this workstream</span>
            <span>{shownRows.length} of {allVisible.length}</span>
          </div>
        </div>
      )}
    </div>
    <div className="chat-workstream-pick">
      {workstreamOpen && <div className="chat-workstream-menu">
        <p className="chat-menu-label">Move this chat to a workstream</p>
        <input ref={workstreamInput} aria-label="Workstream" placeholder="Move to workstream…" value={workstreamQuery}
          onChange={(event) => setWorkstreamQuery(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); void chooseWorkstream(); } if (event.key === "Escape") setMenu(null); }} />
        <ul className="station-list" aria-label="Workstreams">
          {workstreamMatches.map((group) => <li key={group.id}><button type="button" className="station-option" onClick={() => void placeIn(group)}>{group.title}</button></li>)}
          {workstreamQuery.trim() && !workstreamMatches.some((each) => each.title.toLocaleLowerCase() === workstreamQuery.trim().toLocaleLowerCase())
            && <li><button type="button" className="station-option" onClick={() => void chooseWorkstream()}>Create “{workstreamQuery.trim()}”</button></li>}
        </ul>
      </div>}
      {suggestion && <span className="chat-suggestion-chip">
        <button type="button" aria-label={`Move to suggested workstream ${suggestion.title}`} onClick={() => void followSuggestion()}>Move to {suggestion.title}?</button>
        <button type="button" aria-label="Dismiss workstream suggestion" onClick={() => setDismissedSuggestion(true)}>×</button>
      </span>}
    </div>
    <div className="chat-head-actions" role="toolbar" aria-label="Chat controls">
      <button className="btn ghost" type="button" aria-label="Graph" title="Open the graph"
        onClick={() => { window.location.hash = "#/graph"; }}>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <circle cx="8" cy="7" r="3" /><circle cx="16" cy="17" r="3" /><path d="M10 8l4 9" />
        </svg>
      </button>
      <button className="btn ghost" type="button" aria-label="More chat actions" aria-expanded={moreOpen} onClick={() => setMenu(moreOpen ? null : "more")}>⋯</button>
      {moreOpen && <div className="chat-more-menu" aria-label="Chat actions">
        <button className="chat-menu-item" type="button" title="New chat, same canvas" onClick={() => { setMenu(null); void onClear(); }}>Clear chat</button>
        <button className="chat-menu-item" type="button" title="Move this chat to a workstream"
          onClick={() => { setWorkstreamQuery(""); setMenu("workstream"); }}>Move to…</button>
        {hidden
          ? <button className="chat-menu-item" type="button" aria-label="Pin Chat beside the page" title="Pin Chat beside the page" onClick={() => { setMenu(null); setChatHidden(false); }}>Pin panel</button>
          : <button className="chat-menu-item" type="button" aria-label="Hide Chat" title="Hide the chat panel" onClick={() => { setMenu(null); setChatHidden(true); }}>Hide panel</button>}
        {workspace && head && <CloseChat head={head} onClose={() => hideStation(queryClient, workspace.id)} />}
        {head?.may_delete && <DeleteChat head={head} onDeleted={onDelete} />}
        <button className="chat-menu-item" type="button" onClick={() => setMenu("info")}>Info</button>
        {head?.may_hide && <PrivacySwitch head={head} />}
        {head?.may_share && <ShareChat head={head} />}
      </div>}
      {infoOpen && <div className="chat-info-menu" role="dialog" aria-label="Chat info">
        <button type="button" className="btn ghost" onClick={() => setMenu(null)}>Close</button>
        <strong>{shownName}</strong>
        <span>Chat ID: {workspace?.id ?? "—"}</span>
        <span>Conversation: {head?.conversation_id ?? "—"}</span>
        <span>Session: {head?.session_id ?? "—"}</span>
        {run && <div className="chat-info-run">
          <span>{run.text}</span>
          {run.command && <Command command={run.command} />}
        </div>}
        {madeFor !== null && <span>Made for: <a href={`#/lookup/${madeFor}`}>{`${madeCli}${madeActor}`}</a>
          {made ? (made.ended ? " · ended" : " · live") : ""}</span>}
      </div>}
    </div>
  </header>;
}

/**
 * What the Info dialog says of a CLI chat's run: the command that attaches to a trax run, where it is one, else where
 * it runs. None for the assistant, which runs on the server.
 */
function runInfo(provider: Provider, partner: WorkspacePartner | null): { readonly text: string; readonly command?: string } | null {
  if (provider === "assistant") return null;
  if (partner?.status !== "live") return { text: "No run: pick a machine in the provider card" };
  const machine = partner.machine ?? "your machine";
  if (partner.run_name) {
    return { text: `Attach on ${machine}${partner.cwd ? ` (in ${partner.cwd})` : ""}:`, command: `trax run attach ${partner.run_name}` };
  }
  return { text: `Runs in a terminal on ${machine}` };
}
