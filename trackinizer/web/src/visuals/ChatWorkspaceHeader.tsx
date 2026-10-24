import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { createCanvas as createCanvasInWorkspace, createWorkspace, listWorkspaces, moveCanvas, type Workspace } from "../api/workspaces";
import { hideCanvas as setCanvasHidden, listCanvases, pinCanvas, type CanvasSummary } from "../api/canvasManagement";
import type { ChatHead } from "../api/chats";
import { getInquiry } from "../api/inquiries";
import type { CanvasPartner, CanvasState } from "../api/canvases";
import { hasAssistant, PROVIDERS, partnerLabel, type Provider, providerName, canvasProvider } from "../api/providers";
import { hideCanvas, nameCanvas, openCanvas, CANVASES_KEY } from "../canvases/actions";
import { showCanvas } from "../canvases/useCanvas";
import { beginCanvasNavigation } from "../canvases/store";
import { acceptCanvas } from "../canvases/cache";
import { routeUrl } from "../router/addressBar";
import { setChatHidden, setChatSide, useChatColumn } from "./chatColumnState";
import { CloseChat, DeleteChat, PrivacySwitch, ShareChat } from "./ChatSharing";
import { ProviderPicker, reasonText, useChatDefault, useProviderStatuses } from "./ProviderPicker";

type Props = {
  readonly canvas: CanvasState | null;
  readonly head: ChatHead | undefined;
  /** An agent waits on the person for an answer: the header shows a dot. */
  readonly waiting?: boolean;
  readonly onHide: () => Promise<void>;
  readonly onClear: () => Promise<void>;
  readonly onDeleted: (conversation: string) => void;
};

type HeaderMenu = "name" | "workspace" | "more" | "info" | "provider";

/** Chat naming, switching, workspace placement, the provider, and its compact header controls. */
export function ChatWorkspaceHeader({ canvas, head, waiting = false, onHide, onClear, onDeleted }: Props) {
  const queryClient = useQueryClient();
  const { side, hidden } = useChatColumn();
  const defaults = useChatDefault();
  const statuses = useProviderStatuses(canvas);
  const [menu, setMenu] = useState<HeaderMenu | null>(null);
  const [nameDraft, setNameDraft] = useState("");
  const [nameProblem, setNameProblem] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [workspaceQuery, setWorkspaceQuery] = useState("");
  const [dismissedSuggestion, setDismissedSuggestion] = useState(false);
  const nameOpen = menu === "name";
  const workspaceOpen = menu === "workspace";
  const moreOpen = menu === "more";
  const infoOpen = menu === "info";
  const providerOpen = menu === "provider";
  const provider = canvasProvider(canvas);
  const partner = canvas?.partner ?? null;
  const run = runInfo(provider, partner);
  // New chats are offered the default first, then the others, in the order the card lists them. A server with no
  // assistant offers none.
  const newChatOrder = [defaults.provider, ...PROVIDERS.filter((each) => each !== defaults.provider)]
    .filter((each) => each !== "assistant" || hasAssistant(canvas));
  const root = useRef<HTMLDivElement>(null);
  const renameInput = useRef<HTMLInputElement>(null);
  const workspaceInput = useRef<HTMLInputElement>(null);
  const canvases = useQuery({ queryKey: CANVASES_KEY, queryFn: ({ signal }) => listCanvases({ signal }), enabled: nameOpen, staleTime: 2_000 });
  const workspaces = useQuery({ queryKey: ["workspaces"], queryFn: ({ signal }) => listWorkspaces({}, { signal }),
    enabled: workspaceOpen || canvas?.suggestion != null, staleTime: 2_000 });
  const madeFor = canvas?.session_id ?? null;
  const madeForSession = useQuery({ queryKey: ["inquiry", madeFor], queryFn: ({ signal }) => getInquiry(madeFor ?? "", { signal }),
    enabled: infoOpen && madeFor !== null, staleTime: 5_000 });
  const made = madeForSession.data;
  const madeCli = typeof made?.cli === "string" ? made.cli : "session";
  const madeActor = typeof made?.owner === "string" ? ` as ${made.owner}` : "";
  const rows = canvases.data ?? [];
  const groups = workspaces.data ?? [];
  const currentGroup = groups.find((each) => each.id === canvas?.workspace_id)
    ?? groups.find((each) => each.scratch);
  const currentGroupId = currentGroup?.id;
  // Never offer the workspace the chat is already in. The server drops a suggestion naming the canvas's own id, but a
  // canvas with none sits in Scratch, and a title-only suggestion can repeat the current title.
  const offered = canvas?.suggestion;
  const here = offered?.workspace_id != null
    ? offered.workspace_id === currentGroupId
    : offered?.title.toLocaleLowerCase() === currentGroup?.title.toLocaleLowerCase();
  const suggestion = offered && !dismissedSuggestion && !here ? offered : null;
  const shownName = canvas?.name && /^unnamed-canvas#\d+$/.test(canvas.name) ? "Scratch chat" : canvas?.name ?? "Chat";
  const inGroup = rows.filter((each) => (canvas?.workspace_id ? each.workspace_id === canvas.workspace_id : !each.workspace_id) && !each.hidden);
  const recent = inGroup.toSorted((a, b) => (b.modified_at ?? "").localeCompare(a.modified_at ?? ""));
  const chatRows = [...recent.filter((each) => each.pinned), ...recent.filter((each) => !each.pinned).slice(0, 5)];
  const searchedRows = rows.filter((each) => each.name.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  const workspaceMatches = groups.filter((each) => !each.hidden && each.title.toLocaleLowerCase().includes(workspaceQuery.trim().toLocaleLowerCase()));

  useEffect(() => {
    if (nameOpen) renameInput.current?.focus();
  }, [nameOpen]);
  useEffect(() => {
    if (workspaceOpen) workspaceInput.current?.focus();
  }, [workspaceOpen]);
  useEffect(() => {
    if (!nameOpen && !workspaceOpen && !moreOpen && !infoOpen && !providerOpen) return;
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
    if (!canvas) return;
    const name = nameDraft.trim();
    if (!name) return;
    try {
      await nameCanvas(queryClient, canvas.id, name);
      setNameProblem(null);
      closeMenu("name");
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) setNameProblem("You already have a chat with that name");
      else setNameProblem("Could not rename this chat.");
    }
  }

  async function openChat(entry: CanvasSummary): Promise<void> {
    closeMenu("name");
    try {
      if (entry.hidden) {
        const current = beginCanvasNavigation();
        const restored = await setCanvasHidden(entry.id, false);
        acceptCanvas(queryClient, restored);
        if (current()) showCanvas(queryClient, restored);
        void queryClient.invalidateQueries({ queryKey: CANVASES_KEY });
      } else await openCanvas(queryClient, entry.id);
    } catch {
      setNameProblem("Could not open this chat.");
    }
  }

  async function pinChat(entry: CanvasSummary): Promise<void> {
    const state = await pinCanvas(entry.id, !entry.pinned);
    if (entry.id === canvas?.id) queryClient.setQueryData(["canvas", entry.id], state);
    void queryClient.invalidateQueries({ queryKey: CANVASES_KEY });
  }

  async function hideChat(entry: CanvasSummary): Promise<void> {
    await hideCanvas(queryClient, entry.id);
    void queryClient.invalidateQueries({ queryKey: CANVASES_KEY });
  }

  async function createChat(chosen: Provider): Promise<void> {
    if (!canvas) return;
    const current = beginCanvasNavigation();
    const state = await createCanvasInWorkspace(currentGroupId, chosen);
    acceptCanvas(queryClient, state);
    if (current()) showCanvas(queryClient, state);
    void queryClient.invalidateQueries({ queryKey: CANVASES_KEY });
    closeMenu("name");
  }

  async function placeIn(group: Workspace, current = beginCanvasNavigation()): Promise<void> {
    if (!canvas) return;
    const state = await moveCanvas(canvas.id, group.id);
    acceptCanvas(queryClient, state);
    if (current()) showCanvas(queryClient, state);
    void queryClient.invalidateQueries({ queryKey: CANVASES_KEY });
    closeMenu("workspace");
    setDismissedSuggestion(true);
  }

  async function chooseWorkspace(): Promise<void> {
    const query = workspaceQuery.trim();
    const exact = query
      ? workspaceMatches.find((each) => each.title.toLocaleLowerCase() === query.toLocaleLowerCase())
      : workspaceMatches[0];
    if (exact) await placeIn(exact);
    else if (query) {
      const current = beginCanvasNavigation();
      const created = await createWorkspace(query);
      void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      await placeIn(created, current);
    }
  }

  async function followSuggestion(): Promise<void> {
    if (!suggestion) return;
    const current = beginCanvasNavigation();
    const available = await queryClient.fetchQuery({ queryKey: ["workspaces"],
      queryFn: ({ signal }) => listWorkspaces({}, { signal }), staleTime: 0 });
    const target = available.find((each) => each.id === suggestion.workspace_id);
    if (target) await placeIn(target, current);
    else if (current()) {
      setWorkspaceQuery(suggestion.title);
      setMenu("workspace");
    }
  }

  const onDelete = () => {
    if (!head) return;
    onDeleted(head.conversation_id);
    void onHide();
  };

  return <header className="chat-head" ref={root}>
    {nameProblem && !nameOpen && <p className="canvas-problem" role="status">{nameProblem}</p>}
    <div className="chat-partner" role="group" aria-label="Chat partner">
      <span className="chat-partner-context" aria-hidden="true">
        {canvas && <strong>Chat</strong>}
        {head?.audience === "staff" && <span>Staff only</span>}
        {head?.audience === "private" && <span>Private</span>}
      </span>
      {waiting && <span className="chat-waiting" role="status" aria-label="Waiting for your answer" title="An agent is waiting for your answer" />}
      {canvas && <div className="chat-provider-pick">
        <button type="button" className="chat-provider-button" aria-label={`Chat provider: ${providerName(provider, canvas)}`} aria-expanded={providerOpen}
          title="Choose what this chat talks to" onClick={() => setMenu(providerOpen ? null : "provider")}>
          <span className={`provider-dot${partner?.status === "live" ? " live" : ""}`} aria-hidden="true" />
          <span className="chat-provider-label">{partnerLabel(partner, providerName(provider, canvas))}</span><span className="chat-name-caret">▾</span>
        </button>
        {providerOpen && <div className="canvas-menu chat-name-menu chat-provider-menu" role="dialog" aria-label="Chat provider card">
          <ProviderPicker canvas={canvas} />
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
            onClick={() => { setNameDraft(canvas?.name ?? ""); setNameProblem(null); setMenu("name"); }}>{shownName}<span className="chat-name-caret hover-only">▾</span></button>}
        {nameOpen && <div className="canvas-menu chat-name-menu" role="dialog" aria-label="Chats">
          {nameProblem && <p className="canvas-problem" role="status">{nameProblem}</p>}
          <p className="chat-menu-label">Chats in {currentGroup?.title ?? "Scratch"}</p>
          <ul className="canvas-list" aria-label="Chats in this workspace">
            {chatRows.map((entry) => <li key={entry.id} className="chat-name-row">
              <button type="button" className="canvas-option" aria-current={entry.id === canvas?.id} onClick={() => void openChat(entry)}>{entry.name}</button>
              <span className="chat-name-row-actions">
                <button type="button" className="btn ghost" aria-label={`${entry.pinned ? "Unpin" : "Pin"} ${entry.name}`} onClick={() => void pinChat(entry)}>{entry.pinned ? "📌" : "Pin"}</button>
                <button type="button" className="btn ghost" aria-label={`Hide ${entry.name}`} onClick={() => void hideChat(entry)}>Hide</button>
              </span>
            </li>)}
            {chatRows.length === 0 && <li className="muted">No chats yet.</li>}
          </ul>
          {newChatOrder.map((each) => <div key={each} className="provider-new">
            <button type="button" className="canvas-option" disabled={!statuses[each].ok} onClick={() => void createChat(each)}>
              + New chat · {providerName(each, canvas)}{each === defaults.provider && <span className="provider-mark"> ★ default</span>}</button>
            {!statuses[each].ok && <p className="muted provider-reason">{reasonText(statuses[each])}</p>}
          </div>)}
          <div className="chat-name-search">
            <input className="chat-name-search" aria-label="Search chats" placeholder="Search all chats…" value={search}
              onChange={(event) => setSearch(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Enter" && searchedRows[0]) void openChat(searchedRows[0]); }} />
            {search && <ul className="canvas-list" aria-label="Search results">
              {searchedRows.map((entry) => <li key={entry.id}>
                <button type="button" className="canvas-option" onClick={() => void openChat(entry)}>{entry.name}{entry.hidden ? " · hidden" : ""}</button>
              </li>)}
              {searchedRows.length === 0 && <li className="muted">No matches.</li>}
            </ul>}
          </div>
        </div>}
      </div>
    </div>
    <div className="chat-workspace-pick">
      {workspaceOpen && <div className="chat-workspace-menu">
        <p className="chat-menu-label">Move this chat to a workspace</p>
        <input ref={workspaceInput} aria-label="Workspace" placeholder="Move to workspace…" value={workspaceQuery}
          onChange={(event) => setWorkspaceQuery(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); void chooseWorkspace(); } if (event.key === "Escape") setMenu(null); }} />
        <ul className="canvas-list" aria-label="Workspaces">
          {workspaceMatches.map((group) => <li key={group.id}><button type="button" className="canvas-option" onClick={() => void placeIn(group)}>{group.title}</button></li>)}
          {workspaceQuery.trim() && !workspaceMatches.some((each) => each.title.toLocaleLowerCase() === workspaceQuery.trim().toLocaleLowerCase())
            && <li><button type="button" className="canvas-option" onClick={() => void chooseWorkspace()}>Create “{workspaceQuery.trim()}”</button></li>}
        </ul>
      </div>}
      {suggestion && <span className="chat-suggestion-chip">
        <button type="button" aria-label={`Move to suggested workspace ${suggestion.title}`} onClick={() => void followSuggestion()}>Move to {suggestion.title}?</button>
        <button type="button" aria-label="Dismiss workspace suggestion" onClick={() => setDismissedSuggestion(true)}>×</button>
      </span>}
    </div>
    <div className="chat-head-actions" role="toolbar" aria-label="Chat controls">
      <button className="btn ghost hover-only" type="button" aria-label={`Move Chat to the ${side === "left" ? "right" : "left"}`}
        title="Dock on the other side" onClick={() => setChatSide(side === "left" ? "right" : "left")}>{side === "left" ? "▷" : "◁"}</button>
      <button className="btn ghost hover-only" type="button" aria-label="More chat actions" aria-expanded={moreOpen} onClick={() => setMenu(moreOpen ? null : "more")}>⋯</button>
      {moreOpen && <div className="chat-more-menu" aria-label="Chat actions">
        <button className="chat-menu-item" type="button" title="New chat, same canvas" onClick={() => { setMenu(null); void onClear(); }}>Clear chat</button>
        <button className="chat-menu-item" type="button" title="Move this chat to a workspace"
          onClick={() => { setWorkspaceQuery(""); setMenu("workspace"); }}>Move to…</button>
        {hidden
          ? <button className="chat-menu-item" type="button" aria-label="Pin Chat beside the page" title="Pin Chat beside the page" onClick={() => { setMenu(null); setChatHidden(false); }}>Pin panel</button>
          : <button className="chat-menu-item" type="button" aria-label="Hide Chat" title="Hide the chat panel" onClick={() => { setMenu(null); setChatHidden(true); }}>Hide panel</button>}
        {canvas && head && <CloseChat head={head} onClose={() => hideCanvas(queryClient, canvas.id)} />}
        {head?.may_delete && <DeleteChat head={head} onDeleted={onDelete} />}
        <button className="chat-menu-item" type="button" onClick={() => setMenu("info")}>Info</button>
        {head?.may_hide && <PrivacySwitch head={head} />}
        {head?.may_share && <ShareChat head={head} />}
      </div>}
      {infoOpen && <div className="chat-info-menu" role="dialog" aria-label="Chat info">
        <button type="button" className="btn ghost" onClick={() => setMenu(null)}>Close</button>
        <strong>{shownName}</strong>
        <span>Chat ID: {canvas?.id ?? "—"}</span>
        <span>Conversation: {head?.conversation_id ?? "—"}</span>
        <span>Session: {head?.session_id ?? "—"}</span>
        {run && <div className="chat-info-run">
          <span>{run.text}</span>
        </div>}
        {madeFor !== null && <span>Made for: <a href={routeUrl(`#/lookup/${madeFor}`)}>{`${madeCli}${madeActor}`}</a>
          {made ? (made.ended ? " · ended" : " · live") : ""}</span>}
      </div>}
    </div>
  </header>;
}

/**
 * What the Info dialog says of a CLI chat's run: the detached run's name and folder, where it is one, else where it
 * runs. None for the assistant, which runs on the server.
 */
function runInfo(provider: Provider, partner: CanvasPartner | null): { readonly text: string } | null {
  if (provider === "assistant") return null;
  if (partner?.status !== "live") return { text: "No run: pick a machine in the provider card" };
  const machine = partner.machine ?? "your machine";
  if (partner.run_name) {
    return { text: `Detached run ${partner.run_name} on ${machine}${partner.cwd ? ` (in ${partner.cwd})` : ""}` };
  }
  return { text: `Runs in a terminal on ${machine}` };
}
