import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { createStation as createStationInWorkstream, createWorkstream, listWorkstreams, moveStation, type Workstream } from "../api/workstreams";
import { hideStation as setStationHidden, listStations, pinStation, type StationSummary } from "../api/stations";
import type { ChatHead } from "../api/chats";
import { getInquiry } from "../api/inquiries";
import type { WorkspacePartner, WorkspaceState } from "../api/workspaces";
import { hasAssistant, PROVIDERS, partnerLabel, type Provider, providerName, stationProvider } from "../api/providers";
import { Command } from "../settings/ChatPartner";
import { hideStation, nameStation, openStation, STATIONS_KEY } from "../stations/actions";
import { showStation } from "../stations/useStation";
import { setChatHidden, setChatSide, useChatColumn } from "./chatColumnState";
import { DeleteChat, PrivacySwitch, ShareChat } from "./ChatSharing";
import { ProviderPicker, reasonText, useChatDefault, useProviderStatuses } from "./ProviderPicker";

type Props = {
  readonly workspace: WorkspaceState | null;
  readonly head: ChatHead | undefined;
  /** An agent waits on the person for an answer: the header shows a dot. */
  readonly waiting?: boolean;
  readonly onHide: () => Promise<void>;
  readonly onClear: () => Promise<void>;
  readonly onDeleted: (conversation: string) => void;
};

type HeaderMenu = "name" | "workstream" | "more" | "info" | "provider";

/** Chat naming, switching, workstream placement, the provider, and its compact header controls. */
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
  const providerOpen = menu === "provider";
  const provider = stationProvider(workspace);
  const partner = workspace?.partner ?? null;
  const run = runInfo(provider, partner);
  // New chats are offered the default first, then the others, in the order the card lists them. A server with no
  // assistant offers none.
  const newChatOrder = [defaults.provider, ...PROVIDERS.filter((each) => each !== defaults.provider)]
    .filter((each) => each !== "assistant" || hasAssistant(workspace));
  const root = useRef<HTMLDivElement>(null);
  const renameInput = useRef<HTMLInputElement>(null);
  const workstreamInput = useRef<HTMLInputElement>(null);
  const stations = useQuery({ queryKey: STATIONS_KEY, queryFn: ({ signal }) => listStations({ signal }), enabled: nameOpen, staleTime: 2_000 });
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
    if (!nameOpen && !workstreamOpen && !moreOpen && !infoOpen && !providerOpen) return;
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
      closeMenu("name");
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
    const state = await createStationInWorkstream(currentGroupId, chosen);
    showStation(queryClient, state);
    void queryClient.invalidateQueries({ queryKey: STATIONS_KEY });
    closeMenu("name");
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
        {workspace && <strong>Chat</strong>}
        {head?.audience === "staff" && <span>Staff only</span>}
        {head?.audience === "private" && <span>Private</span>}
      </span>
      {waiting && <span className="chat-waiting" role="status" aria-label="Waiting for your answer" title="An agent is waiting for your answer" />}
      {workspace && <div className="chat-provider-pick">
        <button type="button" className="chat-provider-button" aria-label={`Chat provider: ${providerName(provider, workspace)}`} aria-expanded={providerOpen}
          title="Choose what this chat talks to" onClick={() => setMenu(providerOpen ? null : "provider")}>
          <span className={`provider-dot${partner?.status === "live" ? " live" : ""}`} aria-hidden="true" />
          <span className="chat-provider-label">{partnerLabel(partner, providerName(provider, workspace))}</span><span className="chat-name-caret">▾</span>
        </button>
        {providerOpen && <div className="station-menu chat-name-menu chat-provider-menu" role="dialog" aria-label="Chat provider card">
          <ProviderPicker station={workspace} />
        </div>}
      </div>}
      <div className="chat-name-pick">
        {nameOpen ? <input ref={renameInput} className="chat-name-input" aria-label="Chat name" value={nameDraft}
          onChange={(event) => { setNameDraft(event.target.value); setNameProblem(null); }}
          onKeyDown={(event) => {
            if (event.key === "Enter") { event.preventDefault(); void saveName(); }
            if (event.key === "Escape") { event.preventDefault(); setMenu(null); setNameProblem(null); }
            if (event.key === "ArrowDown") { event.preventDefault(); (root.current?.querySelector<HTMLInputElement>(".chat-name-search"))?.focus(); }
          }} />
          : <button type="button" className="chat-name-button" title="Rename, switch chats or search" aria-label={shownName}
            onClick={() => { setNameDraft(workspace?.name ?? ""); setNameProblem(null); setMenu("name"); }}>{shownName}<span className="chat-name-caret hover-only">▾</span></button>}
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
          {newChatOrder.map((each) => <div key={each} className="provider-new">
            <button type="button" className="station-option" disabled={!statuses[each].ok} onClick={() => void createChat(each)}>
              + New chat · {providerName(each, workspace)}{each === defaults.provider && <span className="provider-mark"> ★ default</span>}</button>
            {!statuses[each].ok && <p className="muted provider-reason">{reasonText(statuses[each])}</p>}
          </div>)}
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
        {hidden
          ? <button className="chat-menu-item" type="button" aria-label="Pin Chat beside the page" title="Pin Chat beside the page" onClick={() => { setMenu(null); setChatHidden(false); }}>Pin panel</button>
          : <button className="chat-menu-item" type="button" aria-label="Hide Chat" title="Hide the chat panel" onClick={() => { setMenu(null); setChatHidden(true); }}>Hide panel</button>}
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
