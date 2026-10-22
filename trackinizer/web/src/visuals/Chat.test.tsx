import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { CHAT_DELETED, CHAT_FORK_CLOSED, CHAT_FORK_WIDER, CHAT_NOT_SHARED, CHAT_STAFF_ONLY, type ChatHead, type ChatPartial, type ChatPartialState, deleteChat, getChatHead,
  readChatPartial, reattachChat, sendChatLine, shareChat, unshareChat } from "../api/chats";
import { clearChat, hideStation as restoreStation, listStations, pinStation, renameStation, stationForChat } from "../api/stations";
import { createStation as createStationInWorkstream, createWorkstream, listWorkstreams, moveStation } from "../api/workstreams";
import { ApiError, type CallOptions } from "../api/client";
import { findRef, getDetail } from "../api/detail";
import type { Profile } from "../api/me";
import { listSessionParts, readSessionRecords, SESSION_NOT_OWNER, SESSION_STAFF, type SessionRecord, setSessionAudience } from "../api/sessions";
import { getWorkspace, type WorkspacePartner, type WorkspaceState } from "../api/workspaces";
import { MetaContext, ProfileContext } from "../app/boot";
import { HighlightContext, HighlightStore } from "../app/highlights";
import { META, uuid } from "../detail/testing";
import { LiveContext } from "../live";
import type { LiveHub } from "../live/hub";
import type { LiveQuery } from "../live/serial";
import { startTrail } from "../router/trail";
import { preferences, receivePreferences, sendWaiting } from "../preferences";
import { Chat } from "./Chat";
import { setStation, currentStation } from "../stations/store";
import { useStation } from "../stations/useStation";
import { currentChatColumn, setChatHidden, setChatSide } from "./chatColumnState";
import { ChatFeed, ChatFeedContext } from "./chatFeed";
import { FOLLOW_UP_ASK } from "./chatFollowUp";
import { OPENING } from "./chatRecords";
import { ToastProvider } from "../ui/toast";
import { AGREED, stubFetch } from "../api/testing";
import { Session, SessionContext } from "../app/session";
import goalCommand from "../../test/seams/goalCommand.json";
import livePartner from "../../test/seams/livePartner.json";
import trustAnswer from "../../test/seams/trustAnswer.json";
import trustCard from "../../test/seams/trustCard.json";

vi.mock("../api/chats", async (original) => ({
  ...(await original<typeof import("../api/chats")>()),
  getChatHead: vi.fn(),
  readChatPartial: vi.fn(),
  sendChatLine: vi.fn(),
  reattachChat: vi.fn(),
  shareChat: vi.fn(),
  unshareChat: vi.fn(),
  deleteChat: vi.fn(),
}));
vi.mock("../api/stations", async (original) => ({
  ...(await original<typeof import("../api/stations")>()),
  clearChat: vi.fn(), stationForChat: vi.fn(), hideStation: vi.fn(), createStation: vi.fn(), listStations: vi.fn(),
  pinStation: vi.fn(), renameStation: vi.fn(),
}));
vi.mock("../api/workstreams", () => ({
  createStation: vi.fn(), createWorkstream: vi.fn(), listWorkstreams: vi.fn(), moveStation: vi.fn(), updateWorkstream: vi.fn(),
}));
vi.mock("../api/workspaces", () => ({ getWorkspace: vi.fn() }));
vi.mock("../api/sessions", async (original) => ({
  ...(await original<typeof import("../api/sessions")>()),
  listSessionParts: vi.fn(),
  readSessionRecords: vi.fn(),
  setSessionAudience: vi.fn(),
}));
vi.mock("../api/terminals", () => ({ listTerminalMachines: vi.fn(async () => ({ machines: [], live: null })) }));
vi.mock("../api/detail", () => ({ getDetail: vi.fn(), findRef: vi.fn() }));

const WORKSPACE_ID = "c5286865-67b6-4bd8-ab51-e06e10c326c5";
const ME = "ada@example.com";
const GRACE = "grace@example.com";
const SCOUT = { kind: "shared", session_id: "kb-session", actor: "scout", cli: "sagent", status: "live" } as const;
const workspace: WorkspaceState = {
  id: WORKSPACE_ID, name: "unnamed-workstream#1", conversation_id: null, page: null, revision: 3, focused_instance: null, partner: SCOUT, assistant: "scout",
  visuals: [{ id: "889ffcb2-cf44-43e7-9806-eb08428c6203", type: "trax.browse", version: 1,
    placement: "main", record_id: null, params: {} }],
};
const PROFILE: Profile = { user_id: "u1", email: ME, name: "Ada", role: "writer", last_login: null, visual_workspace_enabled: true, ...AGREED };

/** The records of the session each conversation is held in, as the fake server has them. */
const sessions = new Map<string, SessionRecord[]>();
/** The live layer's registrations, which a test drives by hand as the stream does. */
let registered: LiveQuery[] = [];

beforeEach(() => {
  // jsdom has no ResizeObserver, which the composer's open list uses.
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  stubFetch(async (request) => request.method === "PATCH"
    ? Response.json({ ...preferences(), ...await request.json() }) : Response.json({}));
  vi.mocked(listStations).mockResolvedValue([]);
  vi.mocked(listWorkstreams).mockResolvedValue([]);
  vi.mocked(getWorkspace).mockResolvedValue({ ...workspace, id: "w-fork", name: "unnamed-workstream#2", conversation_id: "f1" });
  OPENING.everyMs = 5;
  OPENING.giveUpMs = 150;
  // A cited row resolves to an id unless a test says otherwise, as the server's route answers.
  vi.mocked(findRef).mockImplementation(async (kind, seq) => `id-${kind}-${seq}`);
  // A detail no test names is missing, as the server answers.
  vi.mocked(getDetail).mockRejectedValue(new ApiError(404, "not found"));
  vi.mocked(listSessionParts).mockImplementation(async (id) => {
    const held = sessions.get(id) ?? [];
    return [{ part: 0, name: "chat.jsonl", format: "sagent", records: held.length, metadata: {}, ir_id: "i" }];
  });
  vi.mocked(readSessionRecords).mockImplementation(async (id, { afterIdx, limit }) =>
    (sessions.get(id) ?? []).filter((record) => record.idx > afterIdx).slice(0, limit));
  vi.mocked(getChatHead).mockImplementation(async (id) => (sessions.has(`s-${id}`) ? head(id) : null));
  vi.mocked(readChatPartial).mockImplementation(heldPartial);
});

/** The reply the assistant is writing, as the server holds it for every chat; `write` changes it. */
let partial: ChatPartialState = { seq: 0, partial: null };
let wakePartial = () => {};

/** A read of the partial, which the server holds until its seq is not `after`. */
async function heldPartial(_session: string, after: number, { signal }: CallOptions = {}): Promise<ChatPartialState> {
  while (partial.seq === after) {
    await new Promise<void>((resolve, reject) => {
      wakePartial = resolve;
      signal?.addEventListener("abort", () => reject(signal.reason));
    });
  }
  return partial;
}

/** The assistant writes `next` as the reply under way; null clears it. */
async function write(next: ChatPartial | null) {
  await act(async () => {
    partial = { seq: partial.seq + 1, partial: next };
    wakePartial();
  });
}

let stopTrail = () => {};
/** What a fork's first line is answered with: a station of its own, holding the new conversation. */
const FORKED = { conversation_id: "f1", session_id: null, workspace_id: "w-fork" };
/** The conversation the station holds, set by `open`. */
let opened: string | null = null;
afterEach(async () => {
  opened = null;
  partial = { seq: 0, partial: null };
  setStation(null);
  vi.useRealTimers();
  stopTrail();
  stopTrail = () => {};
  cleanup();
  vi.restoreAllMocks();
  for (const mock of [getChatHead, readChatPartial, sendChatLine, reattachChat, shareChat, unshareChat, deleteChat, stationForChat, restoreStation, pinStation, listStations, listWorkstreams, moveStation, createWorkstream, createStationInWorkstream, clearChat, getWorkspace, listSessionParts, readSessionRecords, setSessionAudience, getDetail, findRef]) {
    vi.mocked(mock).mockReset();
  }
  sessions.clear();
  registered = [];
  localStorage.clear();
  sessionStorage.clear();
  // A save still on its way (a flip of Chat's side, a style) would make the page ignore the next test's preferences.
  await sendWaiting();
  receivePreferences({ ...preferences(), drafts: {}, highlight_mentions: true, chat_tool_calls: false });
  history.replaceState(null, "", "#/");
});

function head(conversation: string, account = ME, extra: Partial<ChatHead> = {}): ChatHead {
  return { conversation_id: conversation, session_id: `s-${conversation}`, title: "t", account, live: true, forks: 0, forked_from: null,
    forks_on_typing: false, audience: "open", may_hide: false, shared_with: [], may_share: false, may_delete: false, ...extra };
}
function record(idx: number, kind: string, payload: { [field: string]: unknown }): SessionRecord {
  return { idx, kind, payload, text: "", context_id: null, timestamp: null, model: null, ciphertext: null } as unknown as SessionRecord;
}
const said = (idx: number, sender: string, content: string) => record(idx, "AgentToAgentMessage", { sender, content });
const answered = (idx: number, content: string) => record(idx, "AssistantMessage", { content });
const called = (idx: number, name: string) => record(idx, "ToolCall", { name });

/** Hold a conversation's session, so the assistant has "opened" it. */
function hold(conversation: string, ...records: SessionRecord[]) {
  sessions.set(`s-${conversation}`, records);
}

/** The chat of the station this tab shows, as `ChatColumn` hands it: from the query cache the stream keeps. */
function Station({ pinned }: { readonly pinned?: WorkspaceState }) {
  const shown = useStation().data ?? null;
  return <Chat workspace={pinned ?? shown} />;
}

/** One shell's worth of state, kept across `again` renders as the app keeps it across Chat mounting again. */
function shell(
  state: WorkspaceState = workspace,
  options: { profile?: Partial<Profile>; queries?: { retry: number | false; retryDelay?: number } } = {},
) {
  const feed = new ChatFeed();
  const session = new Session(() => {});
  const highlights = new HighlightStore();
  const client = new QueryClient({ defaultOptions: { queries: options.queries ?? { retry: false } } });
  client.setQueryData(["stations"], []);
  client.setQueryData(["workstreams"], []);
  const live = { register: (query: LiveQuery) => {
    registered.push(query);
    return { push: () => {}, dispose: () => { registered = registered.filter((each) => each !== query); } };
  } } as unknown as LiveHub;
  const held = (next: WorkspaceState) => (opened ? { ...next, conversation_id: opened } : next);
  setStation(state.id);
  client.setQueryData(["workspace", state.id], held(state));
  const ui = (pinned?: WorkspaceState) => <SessionContext value={session}><ToastProvider><QueryClientProvider client={client}><MetaContext value={META}>
    <ProfileContext value={{ ...PROFILE, ...options.profile }}><LiveContext value={live}>
      <ChatFeedContext value={feed}><HighlightContext value={highlights}>
        <Station pinned={pinned} />
      </HighlightContext></ChatFeedContext></LiveContext></ProfileContext></MetaContext></QueryClientProvider></ToastProvider></SessionContext>;
  const view = render(ui());
  return {
    feed, client, view, highlights,
    // The same station in another state, as the stream would say it.
    again: (next: WorkspaceState = state) => view.rerender(ui(held(next))),
    mount: () => { view.unmount(); return render(ui()); },
  };
}

/** The stream says the session changed; the live layer hands that to whoever follows it. */
async function changed(session: string, gap = false) {
  await act(async () => {
    for (const query of registered) await query.update({ ids: new Set([session]), gap }, () => true);
  });
}

async function type(text: string) {
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
}

/** The station's chat is conversation `id` when the next `shell` renders. */
const open = (id: string) => { opened = id; };
/** The words of each line, without its "Fork from here" button or the name of the agent that wrote an answer. */
const articles = () => screen.getAllByRole("article").map((line) => [...line.children]
  .filter((child) => child.tagName !== "BUTTON" && !child.classList.contains("chat-line-agent"))
  .map((child) => child.textContent).join(""));
const openMore = () => fireEvent.click(screen.getByRole("button", { name: "More chat actions" }));

test("input history recalls only my questions, not replies, relays or another poster", async () => {
  open("c1");
  hold("c1", said(0, ME, "My first"), answered(1, "Reply"), said(2, GRACE, "Someone else"),
    said(3, ME, "My latest"), said(4, ME, "→ agent: relay"), said(5, "", "Unknown sender"));
  shell();
  await screen.findByText("Unknown sender");
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  fireEvent.keyDown(box, { key: "ArrowUp" });
  expect(box.value).toBe("My latest");
  fireEvent.keyDown(box, { key: "ArrowUp" });
  expect(box.value).toBe("My first");
});

test("@ record suggestions take the arrows and Enter before history and send; Shift, Alt and IME keys pass", async () => {
  open("c1");
  hold("c1", said(0, ME, "My first"));
  const { client } = shell();
  client.setQueryData(["inquiries", "list", [], "Issue", 50, 0], [
    { id: uuid(1), kind: "Issue", seq: 1, title: "Upload", status: "active", modified: "2026-09-21T00:00:00+00:00" },
    { id: uuid(2), kind: "Issue", seq: 2, title: "Retry", status: "active", modified: "2026-09-22T00:00:00+00:00" },
  ]);
  await screen.findByText("My first");
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  box.focus();
  fireEvent.change(box, { target: { value: "@" } });
  box.setSelectionRange(1, 1);
  fireEvent.keyUp(box, { key: "ArrowLeft" });
  expect(screen.getAllByRole("option").map((option) => option.title)).toEqual(["Issue#2", "Issue#1"]);
  // Shift+Up selects text natively: the open list does not take it, and history does not either.
  expect(fireEvent.keyDown(box, { key: "ArrowUp", shiftKey: true })).toBe(true);
  expect(box.value).toBe("@");
  expect(screen.getByRole("option", { selected: true }).title).toBe("Issue#2");
  expect(fireEvent.keyDown(box, { key: "ArrowUp" })).toBe(false);
  expect(box.value).toBe("@");
  expect(screen.getByRole("option", { selected: true }).title).toBe("Issue#1");
  fireEvent.keyDown(box, { key: "Enter", isComposing: true });
  fireEvent.keyDown(box, { key: "Enter", altKey: true });
  expect(box.value).toBe("@");
  fireEvent.keyDown(box, { key: "Enter" });
  expect(box.value).toBe("Issue#1 ");
  expect(sendChatLine).not.toHaveBeenCalled();
});

test("chat drafts follow their station across switches, partner changes and remounts", () => {
  const { again, mount } = shell();
  const box = () => screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(box(), { target: { value: "Draft A" } });
  again({ ...workspace, id: "station-b" });
  expect(box()).toHaveProperty("value", "");
  fireEvent.change(box(), { target: { value: "Draft B" } });
  again({ ...workspace, partner: { ...SCOUT, session_id: "restarted" } });
  expect(box()).toHaveProperty("value", "Draft A");
  mount();
  expect(box()).toHaveProperty("value", "Draft A");
});

test("a send finishing after a station switch clears only its original unchanged draft", async () => {
  let finish = (_value: { conversation_id: string; session_id: string | null; workspace_id: string }) => {};
  vi.mocked(sendChatLine).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const { again } = shell();
  await type("Sent from A");
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce());
  again({ ...workspace, id: "station-b" });
  const box = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(box, { target: { value: "Draft B" } });
  await act(async () => finish({ conversation_id: "c1", session_id: null, workspace_id: WORKSPACE_ID }));
  expect(box).toHaveProperty("value", "Draft B");
  again();
  expect(box).toHaveProperty("value", "");
});

test("a successfully delivered question is recallable before its transcript arrives", async () => {
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: null, workspace_id: WORKSPACE_ID });
  shell();
  await type("Just delivered");
  const box = screen.getByRole("textbox", { name: "Message" });
  await waitFor(() => expect(box).toHaveProperty("value", ""));
  await waitFor(() => expect(box).toHaveProperty("disabled", false));
  fireEvent.keyDown(box, { key: "ArrowUp" });
  expect(box).toHaveProperty("value", "Just delivered");
});

test("scout is the partner with no pairing, and Chat is ready to send", () => {
  shell();
  expect(within(screen.getByLabelText("Chat partner")).getByText("scout")).toBeTruthy();
  expect(screen.getByText("Say something to scout.")).toBeTruthy();
  expect(screen.getByLabelText("Message")).toHaveProperty("disabled", false);
  expect(getChatHead).not.toHaveBeenCalled();
});

test("the chat menu hides the panel without hiding its station", () => {
  setChatHidden(false);
  shell();
  expect(screen.queryByRole("button", { name: "Hide Chat" })).toBeNull();
  openMore();
  expect(screen.queryByRole("button", { name: "Hide from bar" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Hide Chat" }));
  expect(currentChatColumn().hidden).toBe(true);
  expect(currentStation().id).toBe(WORKSPACE_ID);
  expect(restoreStation).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Hide Chat" })).toBeNull();
  expect(screen.getByRole("button", { name: "More chat actions" }).getAttribute("aria-expanded")).toBe("false");
  openMore();
  expect(screen.getByRole("button", { name: "Pin Chat beside the page" })).toBeTruthy();
  setChatHidden(false);
});

test("the info pop-over names the session a station was made for, with its state and a link", async () => {
  stubFetch(async (request) => new URL(request.url).pathname === "/api/inquiries/s-made"
    ? Response.json({ id: "s-made", kind: "AgentSession", cli: "claude", owner: "Agent", ended: null }) : Response.json({}));
  shell({ ...workspace, session_id: "s-made" });
  openMore();
  fireEvent.click(screen.getByRole("button", { name: "Info" }));
  const dialog = screen.getByRole("dialog", { name: "Chat info" });
  await within(dialog).findByText("claude as Agent");
  expect(within(dialog).getByRole("link", { name: "claude as Agent" }).getAttribute("href")).toBe("#/lookup/s-made");
  expect(dialog.textContent).toContain("· live");
});

/** A claude run on gx10 that `trax run --detach --name chat-1a2b3c4d` holds, as the station reads it. */
const DETACHED: WorkspacePartner = { session_id: "s-chat", actor: null, cli: "claude", status: "live",
  label: "claude · gx10", provider: "claude", machine: "gx10", run_name: "chat-1a2b3c4d" };

test("the info pop-over of a claude run on a trax run gives the command that attaches to it, and copies it exactly", async () => {
  const writeText = vi.fn(async (_text: string) => {});
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  shell({ ...workspace, provider: "claude", partner: DETACHED });
  openMore();
  fireEvent.click(screen.getByRole("button", { name: "Info" }));
  const dialog = screen.getByRole("dialog", { name: "Chat info" });
  expect(within(dialog).getByText("Attach on gx10:")).toBeTruthy();
  fireEvent.click(within(dialog).getByRole("button", { name: "Copy" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith("trax run attach chat-1a2b3c4d"));
  vi.unstubAllGlobals();
});

test("the info pop-over names the folder a trax run attaches in, when the station reads it", () => {
  const inLoop: WorkspacePartner = { ...DETACHED, cwd: "~/project" };
  shell({ ...workspace, provider: "claude", partner: inLoop });
  openMore();
  fireEvent.click(screen.getByRole("button", { name: "Info" }));
  expect(within(screen.getByRole("dialog", { name: "Chat info" })).getByText("Attach on gx10 (in ~/project):")).toBeTruthy();
});

test("the info pop-over of a claude run in a terminal names its machine, with no command to attach", () => {
  const foreground: WorkspacePartner = { ...DETACHED, run_name: null };
  shell({ ...workspace, provider: "claude", partner: foreground });
  openMore();
  fireEvent.click(screen.getByRole("button", { name: "Info" }));
  const dialog = screen.getByRole("dialog", { name: "Chat info" });
  expect(within(dialog).getByText("Runs in a terminal on gx10")).toBeTruthy();
  expect(within(dialog).queryByRole("button", { name: "Copy" })).toBeNull();
});

test("the info pop-over says where a CLI with no run is to be run, and says nothing of a run for the assistant", () => {
  const offline: WorkspacePartner = { session_id: "s-chat", actor: "claude", cli: "claude", status: "offline",
    label: "claude: no run", provider: "claude", machine: null };
  shell({ ...workspace, provider: "claude", partner: offline });
  openMore();
  fireEvent.click(screen.getByRole("button", { name: "Info" }));
  expect(screen.getByRole("dialog", { name: "Chat info" }).textContent).toContain("No run: pick a machine in the provider card");
  cleanup();
  shell();
  openMore();
  fireEvent.click(screen.getByRole("button", { name: "Info" }));
  const text = screen.getByRole("dialog", { name: "Chat info" }).textContent ?? "";
  expect(text).not.toMatch(/No run|Attach|Runs in|trax run/);
});

test("the info pop-over says nothing of a session for a station that was not made for one", () => {
  shell();
  openMore();
  fireEvent.click(screen.getByRole("button", { name: "Info" }));
  expect(screen.getByRole("dialog", { name: "Chat info" }).textContent).not.toContain("Made for");
});

test("a button in the header moves Chat to the other side, and says which way", () => {
  setChatSide("left");
  shell();
  fireEvent.click(screen.getByRole("button", { name: "Move Chat to the right" }));
  expect(currentChatColumn().side).toBe("right");
  fireEvent.click(screen.getByRole("button", { name: "Move Chat to the left" }));
  expect(currentChatColumn().side).toBe("left");
});

test("a Chat open over the page can be pinned beside it from the menu", () => {
  setChatHidden(false);
  shell();
  expect(screen.queryByRole("button", { name: "Pin Chat beside the page" })).toBeNull();
  act(() => setChatHidden(true));
  openMore();
  fireEvent.click(screen.getByRole("button", { name: "Pin Chat beside the page" }));
  expect(currentChatColumn().hidden).toBe(false);
  expect(screen.queryByRole("button", { name: "Pin Chat beside the page" })).toBeNull();
});

test("Chat's header names the station and offers Clear chat without History", () => {
  shell();
  const partner = screen.getByRole("group", { name: "Chat partner" });
  expect(within(partner).getByRole("button", { name: "Scratch chat" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "History" })).toBeNull();
  openMore();
  expect(screen.getByRole("button", { name: "Clear chat" }).getAttribute("title")).toBe("New chat, same canvas");
});

test("renaming a chat to a name already in use keeps the field open and explains the conflict", async () => {
  const named = { ...workspace, name: "Original name" };
  vi.mocked(renameStation).mockRejectedValue(new ApiError(409, "Conflict"));
  shell(named);
  fireEvent.click(screen.getByRole("button", { name: "Original name" }));
  const field = screen.getByRole("textbox", { name: "Chat name" });
  fireEvent.change(field, { target: { value: "Existing name" } });
  await act(async () => fireEvent.keyDown(field, { key: "Enter" }));
  expect(screen.getByText("You already have a chat with that name")).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "Chat name" })).toHaveProperty("value", "Existing name");
  expect(renameStation).toHaveBeenCalledWith(WORKSPACE_ID, "Existing name");
});

test("clicking outside the chat name field saves the name", async () => {
  const named = { ...workspace, name: "Original name" };
  vi.mocked(renameStation).mockResolvedValue({ ...named, name: "Kept name" });
  shell(named);
  fireEvent.click(screen.getByRole("button", { name: "Original name" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Chat name" }), { target: { value: "Kept name" } });
  await act(async () => fireEvent.pointerDown(document.body));
  expect(renameStation).toHaveBeenCalledWith(WORKSPACE_ID, "Kept name");
  expect(await screen.findByRole("button", { name: "Kept name" })).toBeTruthy();
});

test("the chat title and workstream menus close each other in both directions", () => {
  shell();
  fireEvent.click(screen.getByRole("button", { name: "Scratch chat" }));
  expect(screen.getByRole("dialog", { name: "Chats" })).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Workstream: Scratch" }));
  expect(screen.queryByRole("dialog", { name: "Chats" })).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Workstream" }));

  fireEvent.click(screen.getByRole("button", { name: "Scratch chat" }));
  expect(screen.queryByRole("textbox", { name: "Workstream" })).toBeNull();
  expect(screen.getByRole("dialog", { name: "Chats" })).toBeTruthy();
  expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Chat name" }));
});

test("a chat name save finishing after switching menus leaves the workstream picker open", async () => {
  let finish: (state: WorkspaceState) => void = () => {};
  vi.mocked(renameStation).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  shell();
  fireEvent.click(screen.getByRole("button", { name: "Scratch chat" }));
  const field = screen.getByRole("textbox", { name: "Chat name" });
  fireEvent.change(field, { target: { value: "Kept name" } });
  fireEvent.keyDown(field, { key: "Enter" });
  fireEvent.click(screen.getByRole("button", { name: "Workstream: Scratch" }));
  expect(screen.queryByRole("dialog", { name: "Chats" })).toBeNull();

  await act(async () => finish({ ...workspace, name: "Kept name" }));
  expect(await screen.findByRole("button", { name: "Kept name" })).toBeTruthy();
  expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Workstream" }));
});

test("global chat search includes hidden chats and restores one when opened", async () => {
  const hidden = { id: "w-hidden", name: "Secret notes", conversation_id: null, page: null, created_at: "2026-10-02T00:00:00Z",
    modified_at: "2026-10-02T00:00:00Z", hidden: true, pinned: false, workstream_id: null };
  vi.mocked(restoreStation).mockResolvedValue({ ...workspace, ...hidden, id: hidden.id, hidden: false });
  const { client } = shell();
  client.setQueryData(["stations"], [hidden]);
  fireEvent.click(screen.getByRole("button", { name: "Scratch chat" }));
  const search = screen.getByRole("textbox", { name: "Search chats" });
  fireEvent.change(search, { target: { value: "Secret" } });
  expect(screen.getByRole("button", { name: "Secret notes · hidden" })).toBeTruthy();
  fireEvent.keyDown(search, { key: "Enter" });
  await waitFor(() => expect(currentStation().id).toBe("w-hidden"));
  expect(restoreStation).toHaveBeenCalledWith("w-hidden", false);
});

test("selecting a cached chat closes the picker before its refresh completes", async () => {
  let finish!: (value: WorkspaceState) => void;
  vi.mocked(getWorkspace).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  const { client } = shell();
  const next = { ...workspace, id: "cached-chat", name: "Cached chat" };
  client.setQueryData(["workspace", next.id], next);
  client.setQueryData(["stations"], [{ ...next, created_at: "2026-10-01T00:00:00Z", modified_at: "2026-10-01T00:00:00Z" }]);
  fireEvent.click(screen.getByRole("button", { name: "Scratch chat" }));
  fireEvent.click(within(screen.getByRole("list", { name: "Chats in this workstream" })).getByRole("button", { name: next.name }));
  expect(currentStation().id).toBe(next.id);
  expect(screen.queryByRole("dialog", { name: "Chats" })).toBeNull();
  await act(async () => finish(next));
});

test("the workstream chip moves this chat to the selected workstream", async () => {
  const destination = { id: "workstream-2", title: "Training", hidden: false, pinned: false, scratch: false, seq: 2 };
  vi.mocked(moveStation).mockResolvedValue({ ...workspace, workstream_id: destination.id });
  const { client } = shell();
  client.setQueryData(["workstreams"], [{ id: "scratch", title: "Scratch", hidden: false, pinned: false, scratch: true, seq: 1 }, destination]);
  fireEvent.click(screen.getByRole("button", { name: "Workstream: Scratch" }));
  fireEvent.click(screen.getByRole("button", { name: "Training" }));
  await waitFor(() => expect(moveStation).toHaveBeenCalledWith(WORKSPACE_ID, destination.id));
});

test("the chat menu lists pinned then recent visible chats and creates in the current workstream", async () => {
  const row = (id: string, name: string, modified_at: string, extra: { hidden?: boolean; pinned?: boolean } = {}) => ({
    id, name, conversation_id: null, page: null, created_at: modified_at, modified_at, hidden: extra.hidden ?? false,
    pinned: extra.pinned ?? false, workstream_id: null,
  });
  const scratch = { id: "scratch", title: "Scratch", hidden: false, pinned: false, scratch: true, seq: 1 };
  const next = { ...workspace, id: "new-chat", name: "unnamed-workstream#2" };
  vi.mocked(createStationInWorkstream).mockResolvedValue(next);
  const { client } = shell();
  client.setQueryData(["stations"], [
    row("current", "Current", "2026-10-01T00:00:00Z"), row("pinned", "Pinned", "2026-10-02T00:00:00Z", { pinned: true }),
    row("recent", "Recent", "2026-10-03T00:00:00Z"), row("hidden", "Hidden", "2026-10-04T00:00:00Z", { hidden: true }),
  ]);
  client.setQueryData(["workstreams"], [scratch]);
  fireEvent.click(screen.getByRole("button", { name: "Scratch chat" }));
  const chats = within(screen.getByRole("list", { name: "Chats in this workstream" }));
  expect(chats.getAllByRole("button").filter((button) => button.classList.contains("station-option"))
    .map((button) => button.textContent)).toEqual(["Pinned", "Recent", "Current"]);
  expect(chats.getByRole("button", { name: "Pin Recent" })).toBeTruthy();
  expect(chats.getByRole("button", { name: "Hide Recent" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /\+ New chat · scout/ }));
  await waitFor(() => expect(currentStation().id).toBe("new-chat"));
  expect(createStationInWorkstream).toHaveBeenCalledWith("scratch", "assistant");
});

test("the workstream picker creates a missing workstream and moves the chat", async () => {
  const scratch = { id: "scratch", title: "Scratch", hidden: false, pinned: false, scratch: true, seq: 1 };
  const created = { id: "new-workstream", title: "New work", hidden: false, pinned: false, scratch: false, seq: 2 };
  vi.mocked(createWorkstream).mockResolvedValue(created);
  vi.mocked(moveStation).mockResolvedValue({ ...workspace, workstream_id: created.id });
  const { client } = shell();
  client.setQueryData(["workstreams"], [scratch]);
  fireEvent.click(screen.getByRole("button", { name: "Workstream: Scratch" }));
  const picker = screen.getByRole("textbox", { name: "Workstream" });
  fireEvent.change(picker, { target: { value: "New work" } });
  fireEvent.keyDown(picker, { key: "Enter" });
  await waitFor(() => expect(moveStation).toHaveBeenCalledWith(WORKSPACE_ID, created.id));
  expect(createWorkstream).toHaveBeenCalledWith("New work");
});

test("the chat pin control stays in the name picker instead of the header", async () => {
  vi.mocked(pinStation).mockResolvedValue({ ...workspace, pinned: true });
  const { client } = shell();
  expect(screen.queryByRole("button", { name: "Pin chat" })).toBeNull();
  client.setQueryData(["stations"], [{ ...workspace, created_at: "2026-10-01T00:00:00Z" }]);
  fireEvent.click(screen.getByRole("button", { name: "Scratch chat" }));
  fireEvent.click(screen.getByRole("button", { name: `Pin ${workspace.name}` }));
  await waitFor(() => expect(pinStation).toHaveBeenCalledWith(WORKSPACE_ID, true));
});

test("clicking a workstream suggestion moves the chat there", async () => {
  const target = { id: "workstream-craftax", title: "Craftax", hidden: false, pinned: false, scratch: false, seq: 2 };
  const suggested = { ...workspace, suggestion: { title: target.title, workstream_id: target.id } };
  vi.mocked(listWorkstreams).mockResolvedValue([
    { id: "scratch", title: "Scratch", hidden: false, pinned: false, scratch: true, seq: 1 }, target,
  ]);
  vi.mocked(moveStation).mockResolvedValue({ ...workspace, workstream_id: target.id });
  const { view } = shell(suggested);
  fireEvent.click(screen.getByRole("button", { name: "Move to suggested workstream Craftax" }));
  await waitFor(() => expect(moveStation).toHaveBeenCalledWith(WORKSPACE_ID, target.id));
  view.unmount();
});

test("a chat in Scratch is not offered Scratch as a workstream suggestion", async () => {
  const scratch = { id: "scratch", title: "Scratch", hidden: false, pinned: false, scratch: true, seq: 1 };
  vi.mocked(listWorkstreams).mockResolvedValue([scratch]);
  const { client } = shell({ ...workspace, suggestion: { title: scratch.title, workstream_id: scratch.id } });
  await act(async () => client.setQueryData(["workstreams"], [scratch]));
  await waitFor(() => expect(screen.queryByRole("button", { name: "Move to suggested workstream Scratch" })).toBeNull());
});

test("a chat in a workstream is not offered that workstream by title alone", async () => {
  const charm = { id: "workstream-charm", title: "Charm ARC1/2", hidden: false, pinned: false, scratch: false, seq: 2 };
  vi.mocked(listWorkstreams).mockResolvedValue([charm]);
  const { client } = shell({ ...workspace, workstream_id: charm.id, suggestion: { title: "charm arc1/2", workstream_id: null } });
  await act(async () => client.setQueryData(["workstreams"], [charm]));
  await waitFor(() => expect(screen.queryByRole("button", { name: "Move to suggested workstream charm arc1/2" })).toBeNull());
});

test("a workstream suggestion can be dismissed for this view", () => {
  const suggested = { ...workspace, suggestion: { title: "Craftax", workstream_id: "workstream-craftax" } };
  shell(suggested);
  fireEvent.click(screen.getByRole("button", { name: "Dismiss workstream suggestion" }));
  expect(screen.queryByRole("button", { name: "Move to suggested workstream Craftax" })).toBeNull();
});

test("Chat shows the conversation its station holds, and none for a station without a chat", async () => {
  hold("c1", said(0, ME, "hello there"), answered(1, "in this station"));
  open("c1");
  const { again } = shell();
  expect(await screen.findByText("in this station")).toBeTruthy();
  opened = null;
  again({ ...workspace, conversation_id: null });
  expect(screen.queryByText("in this station")).toBeNull();
  expect(screen.getByText("Say something to scout.")).toBeTruthy();
});

test("a relay line reads as \"you → lead-2: text\", in its own style, with no fork and no prompt; another poster's names them", async () => {
  open("c1");
  hold("c1", said(0, ME, "q"), answered(1, "a"), said(2, ME, "→ lead-2, tiles-a:ops: rerun the suite"), said(3, GRACE, "→ lead-3: stop"));
  shell();
  const mine = (await screen.findByText(/rerun the suite/)).closest("article")!;
  expect(mine.textContent).toBe("you → lead-2, tiles-a:ops: rerun the suite");
  expect(mine.className).toContain("chat-line-relay");
  expect(mine.className).not.toContain("chat-line-user");
  expect(mine.className).not.toContain("chat-line-assistant");
  expect(within(mine).queryByRole("button", { name: "Fork from here" })).toBeNull();
  expect(screen.getByText(/stop/).closest("article")!.textContent).toBe("grace → lead-3: stop");
});

test("a prompt a claude run asked shows as a card, and Yes posts the decision as the chat's answer, not as a line of mine", async () => {
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID });
  open("c1");
  hold("c1", said(0, ME, "run the suite"), record(1, "SystemMessage", { extra: { $interaction: { id: "p1", type: "permission", tool: "Bash", input: { command: "make test" } } } }));
  shell();
  const prompt = (await screen.findByText("Claude wants to use Bash")).closest("article")!;
  fireEvent.click(within(prompt).getByRole("button", { name: "Yes" }));
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce());
  const [line] = vi.mocked(sendChatLine).mock.calls[0]!;
  expect(line).toMatchObject({ workspaceId: WORKSPACE_ID, conversationId: "c1", kind: "answer", text: "Yes", payload: { prompt_id: "p1", decision: "allow" } });
  expect(document.querySelector(".chat-line-pending")).toBeNull();
});

test("a folder trust a CLI asks shows as a card in Chat, and Trust posts the answer as the chat's answer", async () => {
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID });
  open("c1");
  hold("c1", said(0, ME, "run the suite"), record(1, "SystemMessage", { extra: { $interaction: { id: "t1", type: "trust", tool: "claude", input: { folder: "~/proj" },
    questions: null, plan: null, state: "pending", cli: "claude", folder: "~/proj", machine: "gx10", remembered: true } } }));
  shell();
  const card = (await screen.findByText("Trust this folder?")).closest("article")!;
  fireEvent.click(within(card).getByRole("button", { name: "Trust ~/proj" }));
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce());
  const [line] = vi.mocked(sendChatLine).mock.calls[0]!;
  expect(line).toMatchObject({ workspaceId: WORKSPACE_ID, conversationId: "c1", kind: "answer", text: "Trust ~/proj", payload: { prompt_id: "t1", decision: "allow" } });
});

test("an answer from someone other than the run's owner (403) shows the server's reason, and the card keeps its answer", async () => {
  vi.mocked(sendChatLine).mockRejectedValue(new ApiError(403, "Only ada@example.com can answer prompts from their machine.", "answer_not_owner"));
  open("c1");
  hold("c1", said(0, ME, "run the suite"), record(1, "SystemMessage", { extra: { $interaction: { id: "t1", type: "trust", tool: "claude", input: { folder: "~/proj" },
    questions: null, plan: null, state: "pending", cli: "claude", folder: "~/proj", machine: "gx10", remembered: true } } }));
  shell();
  const card = (await screen.findByText("Trust this folder?")).closest("article")!;
  fireEvent.click(within(card).getByRole("button", { name: "Trust ~/proj" }));
  expect((await screen.findByText("Only ada@example.com can answer prompts from their machine.")).className).toContain("chat-note");
  await waitFor(() => expect(within(card).getByRole("button", { name: "Trust ~/proj" })).toHaveProperty("disabled", false));
  expect(document.querySelector(".chat-line-pending")).toBeNull();
});

test("a /goal the run recorded is the sender's monospace line, its log's goal a note, and the strip above the box names the goal until it stops", async () => {
  open("c1");
  hold("c1",
    record(0, "SystemMessage", { subtype: "command", content: "/goal ship it", extra: { $command: { text: "/goal ship it", verb: "goal", args: "ship it", source: ME,
      room: "lab", state: "typed", reason: null } } }),
    record(1, "ContextState", { kind: "goal_status", content: null, extra: { attachment: { type: "goal_status", met: false, sentinel: true, condition: "ship it" } } }),
    record(2, "ContextState", { kind: "goal_status", content: null, extra: { attachment: { type: "goal_status", met: false, condition: "ship it", reason: "the tests have not run" } } }));
  shell();
  expect((await screen.findByText("Goal set: ship it")).closest("article")!.className).toContain("chat-line-note");
  const command = screen.getByText("/goal ship it").closest("article")!;
  expect(command.className).toContain("chat-line-user");
  expect(command.className).toContain("chat-line-command");
  expect(document.querySelector(".chat-goal")!.textContent).toBe("Goal: ship it · last check: the tests have not run · /goal clear stops it");
});

// The records `trax run` writes, the partner the server reads, and the answer the server
// queues: `test/seams/*.json`, which the Python producers' and consumers' tests hold too.
test("the trust card trax run writes renders, and Trust posts the answer the server queues and the run takes", async () => {
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID });
  open("c1");
  hold("c1", said(0, ME, "run the suite"), trustCard as unknown as SessionRecord);
  shell();
  const card = (await screen.findByText("Trust this folder?")).closest("article")!;
  expect(within(card).getByText("claude on gx10 is waiting to start in ~/proj.")).toBeTruthy();
  fireEvent.click(within(card).getByRole("button", { name: "Trust ~/proj" }));
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce());
  const [line] = vi.mocked(sendChatLine).mock.calls[0]!;
  expect({ kind: line.kind, text: line.text, payload: line.payload }).toEqual(trustAnswer);
});

test("the /goal command trax run records is its sender's command line", async () => {
  open("c1");
  hold("c1", said(0, ME, "go"), goalCommand as unknown as SessionRecord);
  shell();
  const command = (await screen.findByText("/goal ship it")).closest("article")!;
  expect(command.className).toContain("chat-line-command");
  expect(command.className).toContain("chat-line-user");
});

test("the info pop-over of the partner the server reads for a detached run gives its attach command", async () => {
  const writeText = vi.fn(async (_text: string) => {});
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  shell({ ...workspace, provider: "claude", partner: livePartner as WorkspacePartner });
  openMore();
  fireEvent.click(screen.getByRole("button", { name: "Info" }));
  const dialog = screen.getByRole("dialog", { name: "Chat info" });
  expect(within(dialog).getByText("Attach on gx10 (in ~/project):")).toBeTruthy();
  fireEvent.click(within(dialog).getByRole("button", { name: "Copy" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith("trax run attach chat-1a2b3c4d"));
  vi.unstubAllGlobals();
});

test("a command the run refuses (422) shows its reason and keeps the draft", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(sendChatLine).mockRejectedValue(new ApiError(422, "Chat cannot run /model: it opens a menu. Use the terminal: trax run attach <run>", "command_refused"));
  shell();
  await screen.findByText("a2");
  await type("/model opus");
  expect((await screen.findByRole("alert")).textContent).toContain("Chat cannot run /model: it opens a menu.");
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveProperty("value", "/model opus");
  expect(sendChatLine).toHaveBeenCalledOnce();
});

test("Chat asks its partner: the box reads Ask <partner>", () => {
  shell();
  expect(screen.getByRole("textbox", { name: "Message" }).getAttribute("placeholder")).toBe("Ask scout…");
});

test("the composer says who reads the chat, as the server's org mode lets them in", async () => {
  open("c1");
  hold("c1", ...TALK);
  const notice = () => document.querySelector(".chat-notice")!.textContent;
  const { view } = shell(workspace, { profile: { org_mode: "single" } });
  await screen.findByText("a2");
  expect(notice()).toBe("Read by everyone signed in to this server. Do not type secrets here.");
  expect(document.body.textContent).not.toMatch(/Public to read|forks it instead|cannot be deleted/);
  view.unmount();
  shell(workspace, { profile: { org_mode: "domain" } });
  await screen.findByText("a2");
  expect(notice()).toBe("Read by its starter, the people they share it with, and the starter's organisation. Do not type secrets here.");
  cleanup();
  shell(workspace, { profile: { org_mode: "individual" } });
  await screen.findByText("a2");
  expect(notice()).toBe("Read by its starter and the people they share it with. Do not type secrets here.");
  cleanup();
  // A server older than the org mode names none.
  shell();
  await screen.findByText("a2");
  expect(notice()).toMatch(/^Read by its starter, the people they share it with, and their organisation where this server lets it in\./);
});

test("the composer says a private chat is its starter's and their shares', and a staff-only one its staff readers'", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", ME, { audience: "private" }));
  const notice = () => document.querySelector(".chat-notice")!.textContent;
  shell(workspace, { profile: { org_mode: "single" } });
  await screen.findByText("a2");
  expect(notice()).toBe("Private: only its starter and the people they share it with read it. Do not type secrets here.");
  cleanup();
  vi.mocked(getChatHead).mockResolvedValue(head("c1", ME, { audience: "staff" }));
  shell(workspace, { profile: { org_mode: "single" } });
  await screen.findByText("a2");
  expect(notice()).toBe("Staff only: of everyone signed in to this server, only those who hold staff read it. Do not type secrets here.");
});

test("a chat I may neither share nor delete has neither button", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", GRACE));
  shell();
  await screen.findByText("a2");
  expect(screen.queryByRole("button", { name: "Share" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
});

test("Share lists who has the chat, adds an address, removes one, and reads the head again", async () => {
  open("c1");
  hold("c1", ...TALK);
  let shared: string[] = [GRACE];
  vi.mocked(getChatHead).mockImplementation(async () => head("c1", ME, { may_share: true, shared_with: shared }));
  vi.mocked(shareChat).mockImplementation(async (_, who) => { shared = [...shared, who]; });
  vi.mocked(unshareChat).mockImplementation(async (_, who) => { shared = shared.filter((each) => each !== who); });
  shell();
  await screen.findByText("a2");
  openMore();
  fireEvent.click(await screen.findByRole("button", { name: "Share" }));
  const panel = screen.getByRole("dialog", { name: "Share chat" });
  expect(within(panel).getByText(GRACE)).toBeTruthy();
  fireEvent.change(within(panel).getByRole("textbox", { name: "Email to share with" }), { target: { value: " Linus@Example.com " } });
  fireEvent.click(within(panel).getByRole("button", { name: "Add" }));
  await within(panel).findByText("Linus@Example.com");
  expect(shareChat).toHaveBeenCalledWith("c1", "Linus@Example.com");
  fireEvent.click(within(panel).getByRole("button", { name: `Remove ${GRACE}` }));
  await waitFor(() => expect(within(panel).queryByText(GRACE)).toBeNull());
  expect(unshareChat).toHaveBeenCalledWith("c1", GRACE);
});

test("a header pop-over closes on a press anywhere else, so Share and Delete never show together", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", ME, { may_share: true, may_delete: true }));
  shell();
  await screen.findByText("a2");
  openMore();
  fireEvent.click(await screen.findByRole("button", { name: "Share" }));
  // A press inside it keeps it.
  fireEvent.pointerDown(screen.getByRole("textbox", { name: "Email to share with" }));
  expect(screen.getByRole("dialog", { name: "Share chat" })).toBeTruthy();
  const remove = screen.getByRole("button", { name: "Delete chat forever" });
  fireEvent.pointerDown(remove);
  fireEvent.click(remove);
  expect(screen.queryByRole("dialog", { name: "Share chat" })).toBeNull();
  expect(screen.getByRole("alertdialog", { name: "Delete chat forever" })).toBeTruthy();
  fireEvent.pointerDown(document.body);
  expect(screen.queryByRole("alertdialog", { name: "Delete chat forever" })).toBeNull();
});

test("every ⋯ item wears the one menu style, Share, Delete and Public among them", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", ME, { may_share: true, may_delete: true, may_hide: true }));
  shell();
  await screen.findByText("a2");
  openMore();
  await screen.findByRole("button", { name: "Share" });
  const items = within(screen.getByLabelText("Chat actions")).getAllByRole("button");
  expect(items.map((item) => [item.textContent, item.className])).toEqual(
    ["Clear chat", "Hide panel", "Delete chat forever", "Info", "Public", "Share"].map((name) => [name, "chat-menu-item"]));
});

test("a refused share says why in one toast line and keeps the address in the field", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", ME, { may_share: true }));
  vi.mocked(shareChat).mockRejectedValue(new ApiError(403, "Only the starter shares a chat"));
  shell();
  await screen.findByText("a2");
  openMore();
  fireEvent.click(await screen.findByRole("button", { name: "Share" }));
  const field = screen.getByRole("textbox", { name: "Email to share with" });
  fireEvent.change(field, { target: { value: "linus@example.com" } });
  fireEvent.click(screen.getByRole("button", { name: "Add" }));
  expect(await screen.findByText("Only the starter shares a chat")).toBeTruthy();
  expect((field as HTMLInputElement).value).toBe("linus@example.com");
});

test("Clear chat opens the server's new workspace with a fresh idempotency key", async () => {
  const next = { ...workspace, id: "w-cleared", name: "unnamed-workstream#2", conversation_id: null };
  vi.mocked(clearChat).mockResolvedValue(next);
  const { client } = shell();
  openMore();
  fireEvent.click(await screen.findByRole("button", { name: "Clear chat" }));
  await waitFor(() => expect(currentStation().id).toBe("w-cleared"));
  expect(clearChat).toHaveBeenCalledWith(WORKSPACE_ID, expect.any(String));
  expect(client.getQueryData<WorkspaceState>(["workspace", "w-cleared"])).toEqual(next);
});

test("Delete chat forever asks first, Cancel does nothing, then deletes only the chat", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", ME, { may_delete: true }));
  vi.mocked(deleteChat).mockResolvedValue();
  const { client } = shell();
  await screen.findByText("a2");
  openMore();
  fireEvent.click(await screen.findByRole("button", { name: "Delete chat forever" }));
  expect(screen.getByRole("alertdialog", { name: "Delete chat forever" })).toBeTruthy();
  expect(screen.getByText("Delete this chat? Its forks keep their copies.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByText(/Its forks keep their copies/)).toBeNull();
  expect(deleteChat).not.toHaveBeenCalled();
  vi.mocked(restoreStation).mockResolvedValue({ ...workspace, conversation_id: null, hidden: true });
  fireEvent.click(screen.getByRole("button", { name: "Delete chat forever" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete chat" }));
  await waitFor(() => expect(screen.queryByText("a2")).toBeNull());
  expect(deleteChat).toHaveBeenCalledWith("c1");
  expect(restoreStation).toHaveBeenCalledWith(WORKSPACE_ID);
  expect(client.getQueryData<WorkspaceState>(["workspace", WORKSPACE_ID])?.conversation_id).toBeNull();
  expect(screen.queryByRole("button", { name: "Delete chat forever" })).toBeNull();
});

test("Delete chat forever hides the emptied station and opens the station beside it", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", ME, { may_delete: true }));
  vi.mocked(deleteChat).mockResolvedValue();
  vi.mocked(restoreStation).mockResolvedValue({ ...workspace, conversation_id: null, hidden: true });
  vi.mocked(getWorkspace).mockResolvedValue({ ...workspace, id: "w-beta", name: "beta", conversation_id: "c2" });
  const { client } = shell();
  client.setQueryData(["stations"], [
    { id: WORKSPACE_ID, name: "unnamed-workstream#1", conversation_id: "c1", page: null, created_at: "2026-10-01T10:00:00Z" },
    { id: "w-beta", name: "beta", conversation_id: "c2", page: null, created_at: "2026-10-02T10:00:00Z" },
  ]);
  await screen.findByText("a2");
  openMore();
  fireEvent.click(await screen.findByRole("button", { name: "Delete chat forever" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete chat" }));
  await waitFor(() => expect(currentStation().id).toBe("w-beta"));
  expect(deleteChat).toHaveBeenCalledWith("c1");
  expect(restoreStation).toHaveBeenCalledWith(WORKSPACE_ID);
  expect(client.getQueryData<{ id: string; hidden?: boolean; conversation_id: string | null }[]>(["stations"])
    ?.find((each) => each.id === WORKSPACE_ID)).toMatchObject({ hidden: true, conversation_id: null });
});

test("a share refused with chat_not_starter is said in words", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", ME, { may_share: true }));
  vi.mocked(shareChat).mockRejectedValue(new ApiError(403, "403 Forbidden", "chat_not_starter"));
  shell();
  await screen.findByText("a2");
  openMore();
  fireEvent.click(await screen.findByRole("button", { name: "Share" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Email to share with" }), { target: { value: "linus@example.com" } });
  fireEvent.click(screen.getByRole("button", { name: "Add" }));
  expect(await screen.findByText("Only the person who started this chat can do that.")).toBeTruthy();
});

test("a share of a staff-only chat with someone who holds no staff is refused in words", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", ME, { may_share: true, audience: "staff" }));
  vi.mocked(shareChat).mockRejectedValue(new ApiError(409, "409 Conflict", CHAT_STAFF_ONLY));
  shell();
  await screen.findByText("a2");
  openMore();
  fireEvent.click(await screen.findByRole("button", { name: "Share" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Email to share with" }), { target: { value: "linus@example.com" } });
  fireEvent.click(screen.getByRole("button", { name: "Add" }));
  expect(await screen.findByText("This chat is staff only, so it can be shared only with someone who holds staff.")).toBeTruthy();
});

test("a delete refused with chat_has_other_posters is said in words, and an unknown code keeps the server's detail", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", ME, { may_delete: true }));
  vi.mocked(deleteChat).mockRejectedValueOnce(new ApiError(403, "403 Forbidden", "chat_has_other_posters"))
    .mockRejectedValueOnce(new ApiError(403, "Locked by an admin", "locked"));
  shell();
  await screen.findByText("a2");
  openMore();
  fireEvent.click(await screen.findByRole("button", { name: "Delete chat forever" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete chat" }));
  expect(await screen.findByText("Others have posted in this chat, so only an admin can delete it.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Delete chat" }));
  expect(await screen.findByText("Locked by an admin")).toBeTruthy();
});

test("a workspace frame that drops the conversation leaves the station on an empty chat, and drops a fork picked in it", async () => {
  open("c1");
  hold("c1", ...TALK);
  const { client } = shell();
  await screen.findByText("a2");
  fireEvent.click(forkButton("a1"));
  expect(screen.getByText(/Your next message starts a fork/)).toBeTruthy();
  await act(async () => client.setQueryData(["workspace", WORKSPACE_ID], { ...workspace, conversation_id: null, revision: 9 }));
  await waitFor(() => expect(screen.queryByText("a2")).toBeNull());
  expect(screen.queryByText(/Your next message starts a fork/)).toBeNull();
});

test("a refused delete says why in a toast and keeps the chat", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", ME, { may_delete: true }));
  vi.mocked(deleteChat).mockRejectedValue(new ApiError(403, "Someone else has posted in this chat"));
  shell();
  await screen.findByText("a2");
  openMore();
  fireEvent.click(await screen.findByRole("button", { name: "Delete chat forever" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete chat" }));
  expect(await screen.findByText("Someone else has posted in this chat")).toBeTruthy();
  expect(screen.getByText("a2")).toBeTruthy();
});

test("an unconfirmed post survives Chat remounting without sending it again", async () => {
  hold("c1");
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID });
  const shown = shell({ ...workspace, conversation_id: "c1" });
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toHaveProperty("disabled", false));
  await type("continue");
  await screen.findByText("Waiting for the assistant…");

  shown.mount();

  expect(await within(screen.getByRole("log")).findByText("continue")).toBeTruthy();
  expect(sendChatLine).toHaveBeenCalledOnce();
});

test("a post with no captured acknowledgement stops claiming an indefinite wait", async () => {
  hold("c1");
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID });
  shell({ ...workspace, conversation_id: "c1" });
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toHaveProperty("disabled", false));
  await type("continue");

  expect(await screen.findByText(/Delivery is unconfirmed/)).toBeTruthy();
  expect(sendChatLine).toHaveBeenCalledOnce();
});

test("switching to an empty station preserves the previous chat's unconfirmed post", async () => {
  hold("c1");
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID });
  const original = { ...workspace, conversation_id: "c1" };
  const shown = shell(original);
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toHaveProperty("disabled", false));
  await type("continue");
  await screen.findByText("Waiting for the assistant…");
  shown.again({ ...workspace, id: "other-station", conversation_id: null });
  expect(within(screen.getByRole("log")).queryByText("continue")).toBeNull();
  shown.again(original);
  expect(await within(screen.getByRole("log")).findByText("continue")).toBeTruthy();
});

test("an in-flight first post belongs only to the station it was sent from", async () => {
  vi.mocked(sendChatLine).mockReturnValue(new Promise(() => {}));
  const shown = shell();
  await type("for station A");
  await screen.findByText("Sending…");
  shown.again({ ...workspace, id: "station-B" });
  expect(within(screen.getByRole("log")).queryByText("for station A")).toBeNull();
});

test("retrying a captured post after a lost receipt keeps its original confirmation baseline", async () => {
  open("c1");
  hold("c1", said(0, ME, "first"), answered(1, "answer"));
  vi.mocked(sendChatLine).mockRejectedValueOnce(new ApiError(0, "offline", "network"))
    .mockResolvedValueOnce({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID });
  const shown = shell();
  await screen.findByText("answer");
  await type("captured already");
  await screen.findByRole("button", { name: "Retry message" });
  hold("c1", said(0, ME, "first"), answered(1, "answer"), said(2, ME, "captured already"));
  await changed("s-c1");
  await within(screen.getByRole("log")).findByText("captured already");
  fireEvent.click(screen.getByRole("button", { name: "Retry message" }));
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByText("Sending…")).toBeNull());
  expect(within(screen.getByRole("log")).getAllByText("captured already")).toHaveLength(1);
  expect(shown.feed.snapshot().pending).toEqual([]);
});

test("a refused line shows its failure and keeps the original text", async () => {
  hold("c1", record(0, "SystemMessage", { extra: { $delivery: {
    text: "continue", source: ME, kind: "line", refused: true, reason: "run ended",
  } } }));
  shell({ ...workspace, conversation_id: "c1" });

  expect(await screen.findByText("continue")).toBeTruthy();
  expect(await screen.findByText(/Not delivered: run ended/)).toBeTruthy();
  expect(screen.queryByText("Waiting for the assistant…")).toBeNull();
});

test("a line shows pending, then the conversation's id at once, then its record once the assistant has opened the session", async () => {
  let finish: (value: { conversation_id: string; session_id: string | null; workspace_id: string }) => void = () => {};
  vi.mocked(sendChatLine).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const { client } = shell();

  history.replaceState(null, "", "#/graph");
  await type("Show me Issue#12");
  const pendingLine = await within(screen.getByRole("log")).findByText("Show me Issue#12");
  expect(pendingLine.closest("article")!.className).toContain("chat-line-pending");
  expect(screen.getByText("Sending…")).toBeTruthy();
  const [line, key] = vi.mocked(sendChatLine).mock.calls[0]!;
  expect([line, typeof key]).toEqual([{ workspaceId: WORKSPACE_ID, text: "Show me Issue#12", conversationId: null, page: "#/graph", trail: [] }, "string"]);

  // The receipt names the conversation; its session is not there yet.
  await act(async () => finish({ conversation_id: "c1", session_id: null, workspace_id: WORKSPACE_ID }));
  await waitFor(() => expect(screen.getByText("Waiting for the assistant…")).toBeTruthy(), { interval: 1 });
  // The first line binds the conversation to the station.
  expect(client.getQueryData<WorkspaceState>(["workspace", WORKSPACE_ID])?.conversation_id).toBe("c1");
  expect(screen.getAllByText("Show me Issue#12")).toHaveLength(1);

  // The assistant opens the session: the line is its record, and the pending one gives way.
  hold("c1", said(0, ME, "Show me Issue#12"), called(1, "ReadRecord"));
  await waitFor(() => expect(screen.getByText("Working: ReadRecord…")).toBeTruthy(), { interval: 1 });
  const log = within(screen.getByRole("log"));
  expect(log.getAllByText("Show me Issue#12")).toHaveLength(1);
  expect(log.getByText("Show me Issue#12").closest("article")!.className).not.toContain("pending");
  expect(screen.queryByText("Waiting for the assistant…")).toBeNull();

  hold("c1", said(0, ME, "Show me Issue#12"), called(1, "ReadRecord"), answered(2, "It is **open**."));
  await changed("s-c1");
  expect((await screen.findByText("open")).tagName).toBe("STRONG");
  expect(screen.queryByText(/Working/)).toBeNull();
  expect(client.getQueryData(["chat", "records", "s-c1"])).toBeTruthy();
});

test("the next line of a conversation names it", async () => {
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID });
  hold("c1", said(0, ME, "one"), answered(1, "an answer"));
  shell();
  await type("one");
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce(), { interval: 1 });
  await screen.findByText("an answer");
  await type("two");
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledTimes(2), { interval: 1 });
  expect(vi.mocked(sendChatLine).mock.calls[1]![0].conversationId).toBe("c1");
});

test("a line by someone else says who, and mine does not", async () => {
  open("c1");
  hold("c1", said(0, ME, "my question"), answered(1, "an answer"), said(2, GRACE, "and mine"));
  shell();
  const mine = (await screen.findByText("my question")).closest("article")!;
  const hers = screen.getByText("and mine").closest("article")!;
  expect(within(mine).queryByText(GRACE)).toBeNull();
  expect(within(mine).queryByText(ME)).toBeNull();
  expect(within(hers).getByText(GRACE)).toBeTruthy();
  expect(hers.className).toContain("chat-line-other");
});

test("a chat started by someone else says so", async () => {
  open("c1");
  hold("c1", said(0, GRACE, "hello"));
  vi.mocked(getChatHead).mockResolvedValue(head("c1", GRACE));
  shell();
  expect(await screen.findByText(`Started by ${GRACE}.`)).toBeTruthy();
});

test("the stream's change to the session reads what it gained, and another session's change reads nothing", async () => {
  open("c1");
  hold("c1", said(0, ME, "first"));
  shell();
  await screen.findByText("first");
  expect(registered).toHaveLength(1);
  const reads = vi.mocked(readSessionRecords).mock.calls.length;

  hold("c1", said(0, ME, "first"), answered(1, "second, from a teammate's turn"));
  await changed("s-other");
  expect(vi.mocked(readSessionRecords).mock.calls.length).toBe(reads);
  await changed("s-c1");
  expect(await screen.findByText("second, from a teammate's turn")).toBeTruthy();
  // Only what the session gained was asked for.
  expect(vi.mocked(readSessionRecords).mock.calls.at(-1)![1]).toMatchObject({ part: 0, afterIdx: 0 });
});

test("a gap in the stream reads what the session gained meanwhile", async () => {
  open("c1");
  hold("c1", said(0, ME, "first"));
  shell();
  await screen.findByText("first");
  hold("c1", said(0, ME, "first"), answered(1, "missed while the stream was down"));
  await changed("s-unrelated", true);
  expect(await screen.findByText("missed while the stream was down")).toBeTruthy();
});

test("Working… names the last tool call, and gives way to the partner's unavailability", async () => {
  open("c1");
  hold("c1", said(0, ME, "hi"));
  const { again } = shell();
  expect((await screen.findByText("Working…")).getAttribute("role")).toBe("status");
  hold("c1", said(0, ME, "hi"), called(1, "SearchRecords"));
  await changed("s-c1");
  expect(await screen.findByText("Working: SearchRecords…")).toBeTruthy();
  again({ ...workspace, partner: { ...SCOUT, status: "unavailable" } });
  expect(screen.queryByText(/Working/)).toBeNull();
  expect(screen.getByText("scout is unavailable.")).toBeTruthy();
  again();
  expect(screen.getByText("Working: SearchRecords…")).toBeTruthy();
});

test("a conversation whose last line is an answer shows no working line", async () => {
  open("c1");
  hold("c1", said(0, ME, "hi"), called(1, "SearchRecords"), answered(2, "done"));
  shell();
  await screen.findByText("done");
  expect(screen.queryByText(/Working/)).toBeNull();
});

test("the reply being written shows as its answer will, in place of the wait for the assistant", async () => {
  open("c1");
  hold("c1", said(0, ME, "q"), answered(1, "a"));
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID });
  shell();
  await screen.findByText("a");
  await type("and now?");
  await screen.findByText("Waiting for the assistant…");
  await write({ turn: "t1", text: "It is **open**.", done: false });
  const bold = await screen.findByText("open");
  expect(bold.tagName).toBe("STRONG");
  expect(bold.closest("article")!.className).toContain("chat-line-partial");
  expect(bold.closest("article")!.getAttribute("aria-busy")).toBe("true");
  expect(screen.queryByText("Waiting for the assistant…")).toBeNull();
  expect(readChatPartial).toHaveBeenCalledWith("s-c1", 0, expect.anything());
});

test("the stored answer takes the bubble's place, once, and a partial cleared for a tool call gives way to Working…", async () => {
  open("c1");
  hold("c1", said(0, ME, "hi"));
  shell();
  await screen.findByText("Working…");
  await write({ turn: "t1", text: "Let me look", done: false });
  await screen.findByText("Let me look");
  expect(screen.queryByText("Working…")).toBeNull();
  await write(null);
  await screen.findByText("Working…");
  expect(document.querySelector(".chat-line-partial")).toBeNull();
  await write({ turn: "t1", text: "It is open.", done: false });
  await write({ turn: "t1", text: "It is open.", done: true });
  await screen.findByText("It is open.");
  hold("c1", said(0, ME, "hi"), answered(1, "It is open."));
  await changed("s-c1");
  await waitFor(() => expect(screen.getByText("It is open.").closest("article")!.className).not.toContain("chat-line-partial"), { interval: 1 });
  expect(document.querySelector(".chat-line-partial")).toBeNull();
});

test("my next line's Sending… shows over the bubble, and an answer stored while that line waits takes the bubble's place, once", async () => {
  open("c1");
  hold("c1", said(0, ME, "first"));
  let finish: (value: { conversation_id: string; session_id: string | null; workspace_id: string }) => void = () => {};
  vi.mocked(sendChatLine).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  shell();
  await screen.findByText("Working…");
  await write({ turn: "t1", text: "The first", done: false });
  await screen.findByText("The first");
  await type("second");
  await screen.findByText("Sending…");
  expect(document.querySelector(".chat-line-partial")).toBeNull();
  await act(async () => finish({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID }));
  await screen.findByText("The first");
  expect(screen.queryByText("Waiting for the assistant…")).toBeNull();
  await write({ turn: "t1", text: "The first answer", done: true });
  expect(await screen.findByText("The first answer")).toBeTruthy();
  hold("c1", said(0, ME, "first"), answered(1, "The first answer"));
  await changed("s-c1");
  await screen.findByText("Waiting for the assistant…");
  expect(screen.getAllByText("The first answer")).toHaveLength(1);
  expect(document.querySelector(".chat-line-partial")).toBeNull();
});

test("the transcript follows the reply being written as it grows", async () => {
  open("c1");
  hold("c1", said(0, ME, "hi"));
  shell();
  await screen.findByText("Working…");
  const log = screen.getByRole("log", { name: "Messages" });
  let height = 1000;
  Object.defineProperty(log, "scrollHeight", { configurable: true, get: () => height });
  Object.defineProperty(log, "clientHeight", { configurable: true, value: 300 });
  await write({ turn: "t1", text: "One", done: false });
  await screen.findByText("One");
  expect(log.scrollTop).toBe(1000);
  height = 1300;
  await write({ turn: "t1", text: "One more line", done: false });
  await screen.findByText("One more line");
  expect(log.scrollTop).toBe(1300);
});

test("Chat reads no reply being written while its partner is gone, whose going hides the bubble", async () => {
  open("c1");
  hold("c1", said(0, ME, "hi"));
  const gone = { ...workspace, partner: { ...SCOUT, status: "unavailable" as const } };
  const { again } = shell(gone);
  await screen.findByText("scout is unavailable.");
  await act(() => Promise.resolve());
  expect(readChatPartial).not.toHaveBeenCalled();
  again(workspace);
  await write({ turn: "t1", text: "Half a reply", done: false });
  await screen.findByText("Half a reply");
  again(gone);
  expect(screen.getByText("scout is unavailable.")).toBeTruthy();
  expect(document.querySelector(".chat-line-partial")).toBeNull();
});

test("a finished turn's text still held raises no bubble under the next line", async () => {
  partial = { seq: 1, partial: { turn: "t0", text: "the earlier answer", done: true } };
  open("c1");
  hold("c1", said(0, ME, "q"), answered(1, "the earlier answer"), said(2, ME, "and then?"));
  shell();
  await screen.findByText("Working…");
  // The second read asks after the held seq: the first one's answer was taken.
  await waitFor(() => expect(readChatPartial).toHaveBeenCalledTimes(2), { interval: 1 });
  expect(document.querySelector(".chat-line-partial")).toBeNull();
  expect(screen.getByText("Working…")).toBeTruthy();
});

test("a line the assistant never opens a session for is said plainly after a while, and can be sent again", async () => {
  // The clock is the test's: the give-up time passes by advancing it, not by waiting.
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c9", session_id: null, workspace_id: WORKSPACE_ID });
  shell();
  await type("anyone there?");
  await screen.findByText("Waiting for the assistant…");
  expect(screen.queryByRole("alert")).toBeNull();
  await act(() => vi.advanceTimersByTimeAsync(OPENING.giveUpMs));
  expect(screen.getByRole("alert").textContent).toContain("has not opened this chat");
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveProperty("disabled", false);
});

test("a stored conversation with no session on this server says so, and reads nothing", async () => {
  open("c1");
  shell();
  expect((await screen.findByRole("status")).textContent).toContain("no session on this server yet");
  expect(listSessionParts).not.toHaveBeenCalled();
});

test.each([
  ["a live partner", workspace],
  ["a CLI chat with no run", { ...workspace, provider: "claude" as const, partner: { ...SCOUT, status: "offline" as const, provider: "claude" as const, label: "claude: no run" } }],
])("a viewer reads a chat with %s, and where the box would be is told they read it but do not post", async (_, state) => {
  open("c1");
  hold("c1", said(0, GRACE, "a teammate's line"), answered(1, "an answer"));
  shell(state, { profile: { role: "viewer" } });
  expect(await screen.findByText("an answer")).toBeTruthy();
  expect(screen.getByText("Viewers can read this chat but not post.")).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
  expect(sendChatLine).not.toHaveBeenCalled();
});

test("a failed send keeps the draft for a retry under one key, and shows no pending line", async () => {
  vi.mocked(sendChatLine).mockRejectedValueOnce(new ApiError(0, "offline", "network"))
    .mockResolvedValueOnce({ conversation_id: "c1", session_id: null, workspace_id: WORKSPACE_ID });
  shell();
  await type("hi");
  fireEvent.click(await screen.findByRole("button", { name: "Retry message" }));
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledTimes(2), { interval: 1 });
  expect(vi.mocked(sendChatLine).mock.calls[1]![1]).toBe(vi.mocked(sendChatLine).mock.calls[0]![1]);
  await waitFor(() => expect(screen.queryByText("Sending…")).toBeNull(), { interval: 1 });
});

test("recalling identical history after a failed send keeps retry identity and clears on success", async () => {
  open("c1");
  hold("c1", said(0, ME, "same"), answered(1, "answer"));
  vi.mocked(sendChatLine).mockRejectedValueOnce(new ApiError(0, "offline", "network"))
    .mockResolvedValueOnce({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID });
  shell();
  await screen.findByText("answer");
  await type("same");
  await screen.findByRole("button", { name: "Retry message" });
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  box.setSelectionRange(0, 0);
  fireEvent.keyDown(box, { key: "ArrowUp" });
  fireEvent.click(screen.getByRole("button", { name: "Retry message" }));
  await waitFor(() => expect(box.value).toBe(""));
  expect(vi.mocked(sendChatLine).mock.calls[1]![1]).toBe(vi.mocked(sendChatLine).mock.calls[0]![1]);
  expect(preferences().drafts[`chat:${WORKSPACE_ID}`]).toBeUndefined();
});

test("a refusal shows the server's reason and offers no retry, while a server fault does", async () => {
  vi.mocked(sendChatLine).mockRejectedValueOnce(new ApiError(409, "The assistant is not running"));
  shell();
  await type("hi");
  expect((await screen.findByRole("alert")).textContent).toContain("The assistant is not running");
  expect(screen.queryByRole("button", { name: "Retry message" })).toBeNull();
  expect(within(screen.getByRole("log")).queryByText("hi")).toBeNull();
  cleanup();
  vi.mocked(sendChatLine).mockRejectedValueOnce(new ApiError(503, "Service Unavailable"));
  shell();
  await type("hi");
  expect(await screen.findByRole("button", { name: "Retry message" })).toBeTruthy();
});

test("a message's length counts characters as the server does, so 16,384 emoji send and one more is refused", async () => {
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: null, workspace_id: WORKSPACE_ID });
  shell();
  const box = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(box, { target: { value: "\u{1F600}".repeat(16_385) } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect((await screen.findByRole("alert")).textContent).toContain("this one has 16,385");
  fireEvent.change(box, { target: { value: "\u{1F600}".repeat(16_384) } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce(), { interval: 1 });
});

test("the composer refuses a too long message before sending it", async () => {
  shell();
  const box = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(box, { target: { value: "x".repeat(16_385) } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect((await screen.findByRole("alert")).textContent).toContain("at most 16,384");
  expect(sendChatLine).not.toHaveBeenCalled();
  fireEvent.change(box, { target: { value: "x".repeat(16_384) } });
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: null, workspace_id: WORKSPACE_ID });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce(), { interval: 1 });
});

test("the box stays focused and editable while a message is sending, and what is typed meanwhile stays", async () => {
  let finish: (value: { conversation_id: string; session_id: string | null; workspace_id: string }) => void = () => {};
  vi.mocked(sendChatLine).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  shell();
  await type("first");
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  await screen.findByText("Sending…");
  expect(box.disabled).toBe(false);
  fireEvent.change(box, { target: { value: "second, typed meanwhile" } });
  await act(async () => finish({ conversation_id: "c1", session_id: null, workspace_id: WORKSPACE_ID }));
  expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value).toBe("second, typed meanwhile");
});

test("Chat mounting again keeps the station's conversation, which the station holds", async () => {
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID });
  hold("c1", said(0, ME, "hi"));
  const { mount } = shell();
  await type("hi");
  expect(await screen.findByText("hi")).toBeTruthy();
  await waitFor(() => expect(listSessionParts).toHaveBeenCalled(), { interval: 1 });
  mount();
  expect(await screen.findByText("hi")).toBeTruthy();
  expect(screen.queryByText("Say something to scout.")).toBeNull();
});

test("a request from outside Chat to continue a science chat's session opens its conversation", async () => {
  vi.mocked(getDetail).mockResolvedValue({
    self: { id: "sess-1", kind: "AgentSession", seq: 7, title: "t", labels: ["science-chat", "poster:ada@example.com"],
      cli_session_id: "chat:c7" },
    edges: {}, backlinks: {}, changes: [],
  } as never);
  hold("c7", said(0, GRACE, "from the Console"), answered(1, "an answer there"));
  vi.mocked(getChatHead).mockResolvedValue(head("c7", GRACE));
  vi.mocked(stationForChat).mockResolvedValue({ ...workspace, id: "w7", name: "unnamed-workstream#7", conversation_id: "c7" });
  const { feed } = shell();
  expect(screen.getByText("Say something to scout.")).toBeTruthy();
  act(() => feed.continueIn("sess-1"));
  expect(await screen.findByText("an answer there")).toBeTruthy();
  // The user's station for that chat is shown, the server having found or made it.
  expect(stationForChat).toHaveBeenCalledWith("c7");
  expect(currentStation().id).toBe("w7");
  expect(getDetail).toHaveBeenCalledWith("sess-1", expect.any(Object));
  expect(feed.snapshot().request).toBeNull();
});

test("a request to continue a session that is no science chat says so and opens nothing", async () => {
  vi.mocked(getDetail).mockResolvedValue({
    self: { id: "sess-2", kind: "AgentSession", seq: 8, title: "t", labels: ["slack-thread:x"], cli_session_id: "chat:spoof" },
    edges: {}, backlinks: {}, changes: [],
  } as never);
  const { feed } = shell();
  act(() => feed.continueIn("sess-2"));
  expect((await screen.findByRole("status")).textContent).toBe("That session is not a science chat.");
  expect(stationForChat).not.toHaveBeenCalled();
  expect(getChatHead).not.toHaveBeenCalled();
});

test("a delayed continuation lookup cannot replace the chat requested last", async () => {
  let first!: (value: Awaited<ReturnType<typeof getDetail>>) => void;
  vi.mocked(getDetail).mockReturnValueOnce(new Promise((resolve) => { first = resolve; }))
    .mockResolvedValueOnce({ self: { id: "second-session", kind: "AgentSession", seq: 2, title: "second", cli_session_id: "chat:c2", labels: ["science-chat"] },
      edges: {}, backlinks: {}, changes: [] } as never);
  vi.mocked(stationForChat).mockImplementation(async (id) => ({ ...workspace, id: `w-${id}`, conversation_id: id }));
  const { feed } = shell();
  act(() => feed.continueIn("first-session"));
  await waitFor(() => expect(getDetail).toHaveBeenCalledWith("first-session", expect.any(Object)));
  act(() => feed.continueIn("second-session"));
  await waitFor(() => expect(currentStation().id).toBe("w-c2"));
  await act(async () => first({ self: { id: "first-session", kind: "AgentSession", seq: 1, title: "first", cli_session_id: "chat:c1", labels: ["science-chat"] },
    edges: {}, backlinks: {}, changes: [] } as never));
  expect(currentStation().id).toBe("w-c2");
  expect(stationForChat).not.toHaveBeenCalledWith("c1");
});

test("a request whose session cannot be read says so", async () => {
  vi.mocked(getDetail).mockRejectedValue(new ApiError(404, "gone"));
  const { feed } = shell();
  act(() => feed.continueIn("sess-3"));
  expect((await screen.findByRole("status")).textContent).toBe("Could not open that chat.");
});

test("a request waits for Chat to mount: a Chat shown later takes it", async () => {
  vi.mocked(getDetail).mockResolvedValue({
    self: { id: "sess-1", kind: "AgentSession", seq: 7, title: "t", labels: ["science-chat"], cli_session_id: "chat:c7" },
    edges: {}, backlinks: {}, changes: [],
  } as never);
  hold("c7", answered(0, "waiting for you"));
  vi.mocked(getChatHead).mockResolvedValue(head("c7", GRACE));
  vi.mocked(stationForChat).mockResolvedValue({ ...workspace, id: "w7", name: "unnamed-workstream#7", conversation_id: "c7" });
  const feed = new ChatFeed();
  feed.continueIn("sess-1");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  setStation(workspace.id);
  client.setQueryData(["workspace", workspace.id], workspace);
  render(<SessionContext value={new Session(() => {})}><QueryClientProvider client={client}><MetaContext value={META}><ProfileContext value={PROFILE}>
    <ChatFeedContext value={feed}>
      <Station />
    </ChatFeedContext></ProfileContext></MetaContext></QueryClientProvider></SessionContext>);
  expect(await screen.findByText("waiting for you")).toBeTruthy();
});

test("a reload restores the conversation", async () => {
  open("c1");
  hold("c1", answered(0, "still here"));
  shell();
  expect(await screen.findByText("still here")).toBeTruthy();
});

test("every part of the session is read, in order", async () => {
  open("c1");
  hold("c1");
  vi.mocked(listSessionParts).mockResolvedValue([
    { part: 0, name: "a.jsonl", format: "sagent", records: 1, metadata: {}, ir_id: "i" },
    { part: 1, name: "b.jsonl", format: "sagent", records: 1, metadata: {}, ir_id: "j" },
  ]);
  vi.mocked(readSessionRecords).mockImplementation(async (_id, { part }) => [part === 0 ? said(0, ME, "before the restart") : answered(0, "after the restart")]);
  shell();
  await screen.findByText("after the restart");
  expect(articles()).toEqual(["before the restart", "after the restart"]);
});

test("each answer shows the agent whose part wrote it, not the provider the station has now", async () => {
  open("c1");
  hold("c1");
  vi.mocked(listSessionParts).mockResolvedValue([
    { part: 0, name: "a.jsonl", format: "sagent", records: 2, metadata: {}, ir_id: "i" },
    { part: 1, name: "b.jsonl", format: "claude", records: 1, metadata: {}, ir_id: "j" },
  ]);
  vi.mocked(readSessionRecords).mockImplementation(async (_id, { part }) => (part === 0 ? [said(0, ME, "a question"), answered(1, "from the assistant")] : [answered(0, "from claude")]));
  shell({ ...workspace, provider: "codex" });
  await screen.findByText("from claude");
  const agentOf = (text: string) => screen.getByText(text).closest("article")!.querySelector(".chat-line-agent")?.textContent ?? null;
  expect([agentOf("a question"), agentOf("from the assistant"), agentOf("from claude")]).toEqual([null, "scout", "claude"]);
});

const OFF_LINE = "Chat is off: the assistant is not connected.";
const emptyPanel = () => document.querySelector(".chat-panel")!.textContent!;

test.each([
  ["no assistant is set up", { ...workspace, partner: null, assistant: null }],
  ["the assistant has no live session", { ...workspace, partner: { ...SCOUT, status: "unavailable" as const } }],
])("when %s, Chat says it is off, once, and the box is disabled with no setup", (_, state) => {
  shell(state);
  expect(screen.getAllByText(OFF_LINE)).toHaveLength(1);
  expect(emptyPanel()).not.toMatch(/No partner is available|This partner is unavailable|No assistant is set up|Ask your admin|helper|uv tool install/);
  expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("textbox", { name: "Message" }).getAttribute("placeholder")).toBe("Chat is off");
});

test("a CLI chat with no live run says so, and asks for a machine", () => {
  shell({ ...workspace, provider: "claude", partner: { ...SCOUT, status: "offline" as const, provider: "claude", label: "claude: no run" } });
  expect(screen.getAllByText("Chat is off: no claude run holds this chat. Pick a machine.")).toHaveLength(1);
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveProperty("disabled", true);
  expect(emptyPanel()).not.toMatch(/Chat is off: the assistant/);
});

test("a live partner shows none of the off line, and the header names it", () => {
  shell({ ...workspace, partner: { ...SCOUT, actor: "ada-run" } });
  expect(within(screen.getByLabelText("Chat partner")).getByText("ada-run")).toBeTruthy();
  expect(screen.getByText("Say something to ada-run.")).toBeTruthy();
  expect(emptyPanel()).not.toMatch(/Chat is off|helper|uv tool install/);
  expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
});

test("a record link in an answer is a plain link that only sets the address, and writes nothing", async () => {
  open("c1");
  hold("c1", answered(0, "See Issue#12 and 61d3a095-c7f1-4d27-a4c4-a5b1c218a31e."));
  shell();
  const issue = await screen.findByRole("link", { name: /Issue#12/ });
  expect(issue.getAttribute("href")).toBe("#/ref/Issue/12");
  const lookup = screen.getByRole("link", { name: /61d3a095/ });
  expect(lookup.getAttribute("href")).toBe("#/lookup/61d3a095-c7f1-4d27-a4c4-a5b1c218a31e");
  expect(fireEvent.click(lookup)).toBe(true);
  expect(sendChatLine).not.toHaveBeenCalled();
});

const LOOKED_UP = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";

/** Move the page as the router's links do: the hash changes and the window says so. */
function go(hash: string) {
  act(() => {
    history.replaceState(null, "", hash);
    dispatchEvent(new HashChangeEvent("hashchange"));
  });
}

/** Name any record id as an Experiment, and any `Kind/seq` as found. */
function knowRecords() {
  vi.mocked(getDetail).mockImplementation(async (id: string) => ({
    self: { id, kind: "Experiment", seq: 407, title: `Run of ${id}` }, edges: {}, backlinks: {}, changes: [],
  }) as never);
  vi.mocked(findRef).mockResolvedValue("found");
}

async function sentFrom(hashes: readonly string[]) {
  history.replaceState(null, "", hashes[0]);
  stopTrail = startTrail();
  shell();
  for (const hash of hashes.slice(1)) go(hash);
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: null, workspace_id: WORKSPACE_ID });
  await type("where am I");
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledTimes(1), { interval: 1 });
  const { page, trail } = vi.mocked(sendChatLine).mock.calls[0]![0];
  return { page, trail };
}

test("a line carries the page it is sent from and the pages the user came through, oldest first", async () => {
  expect(await sentFrom(["#/list/Issue", "#/activity", "#/console"]))
    .toEqual({ page: "#/console", trail: ["#/list/Issue", "#/activity"] });
});

test("the trail a line carries holds at most 8 pages and never the current one", async () => {
  const hashes = Array.from({ length: 12 }, (_, index) => `#/ref/Issue/${index + 1}`);
  const { page, trail } = await sentFrom(hashes);
  expect(page).toBe("#/ref/Issue/12");
  expect(trail).toEqual(hashes.slice(3, 11));
});

test("a page the server would refuse is sent as null, with the pages before it as the trail", async () => {
  expect(await sentFrom(["#/graph", "#/settings", "#top"])).toEqual({ page: null, trail: ["#/graph", "#/settings"] });
});

test("Chat names the record on screen and follows the page", async () => {
  knowRecords();
  history.replaceState(null, "", "#/activity");
  shell();
  const chip = () => screen.queryByLabelText("Screen context");
  expect(chip()).toBeNull();

  go(`#/lookup/${LOOKED_UP}`);
  await waitFor(() => expect(chip()?.textContent).toBe(`On screen: Experiment#407 Run of ${LOOKED_UP}`));

  go("#/ref/Experiment/407");
  await waitFor(() => expect(chip()?.textContent).toBe("On screen: Experiment#407 Run of found"));
  expect(findRef).toHaveBeenCalledWith("Experiment", 407, expect.anything());

  go(`#/inquiry/${LOOKED_UP}`);
  await waitFor(() => expect(chip()?.textContent).toBe(`On screen: Experiment#407 Run of ${LOOKED_UP}`));

  go("#/activity");
  expect(chip()).toBeNull();
});

test("the transcript follows the newest row, the working line included, unless the reader scrolled up", async () => {
  open("c1");
  hold("c1", said(0, ME, "one"));
  shell();
  await screen.findByText("one");
  const log = screen.getByRole("log", { name: "Messages" });
  let height = 1000;
  Object.defineProperty(log, "scrollHeight", { configurable: true, get: () => height });
  Object.defineProperty(log, "clientHeight", { configurable: true, value: 300 });
  hold("c1", said(0, ME, "one"), called(1, "SearchRecords"));
  await changed("s-c1");
  await screen.findByText("Working: SearchRecords…");
  expect(log.scrollTop).toBe(1000);
  height = 1200;
  hold("c1", said(0, ME, "one"), called(1, "SearchRecords"), answered(2, "two"));
  await changed("s-c1");
  await screen.findByText("two");
  expect(log.scrollTop).toBe(1200);

  log.scrollTop = 100;
  fireEvent.scroll(log);
  height = 1500;
  hold("c1", said(0, ME, "one"), called(1, "SearchRecords"), answered(2, "two"), said(3, GRACE, "three"));
  await changed("s-c1");
  await screen.findByText("three");
  expect(log.scrollTop).toBe(100);
});

test("a read that failed offers Retry", async () => {
  open("c1");
  hold("c1", answered(0, "back"));
  vi.mocked(getChatHead).mockRejectedValueOnce(new ApiError(500, "boom")).mockResolvedValue(head("c1"));
  shell();
  fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
  expect(await screen.findByText("back")).toBeTruthy();
});

test("Chat's reads use the app's read retry: a read that fails once is read again without a click", async () => {
  open("c1");
  hold("c1", answered(0, "second try"));
  vi.mocked(getChatHead).mockRejectedValueOnce(new ApiError(503, "unavailable")).mockResolvedValue(head("c1"));
  shell(workspace, { queries: { retry: 1, retryDelay: 0 } });
  expect(await screen.findByText("second try")).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
});

const TALK = [said(0, ME, "q1"), answered(1, "a1"), said(2, GRACE, "q2"), answered(3, "a2")];
const forkButton = (line: string) => within(screen.getByText(line).closest("article")!).getByRole("button", { name: "Fork from here" });

test("typing in a chat I read joins it, and sends no fork", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID });
  shell();
  await screen.findByText("a2");
  expect(screen.queryByText(/not shared with you/)).toBeNull();
  await type("and so?");
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce(), { interval: 1 });
  expect(vi.mocked(sendChatLine).mock.calls[0]![0]).toMatchObject({ conversationId: "c1" });
  expect(vi.mocked(sendChatLine).mock.calls[0]![0]).not.toHaveProperty("fork");
});

test("Fork from here on an earlier line makes the next message a fork after it, and the reader lands in the fork", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(sendChatLine).mockResolvedValue(FORKED);
  shell();
  await screen.findByText("a2");
  // Only an answer forks: a line typed to the assistant has no button.
  expect(within(screen.getByText("q1").closest("article")!).queryByRole("button", { name: "Fork from here" })).toBeNull();
  fireEvent.click(forkButton("a1"));
  expect((await screen.findByText(/Your next message starts a fork of this chat after/)).textContent).toContain("“a1”");
  await type("is a1 wrong?");
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce(), { interval: 1 });
  const [line, key] = vi.mocked(sendChatLine).mock.calls[0]!;
  expect(line).toMatchObject({ conversationId: null, fork: { sessionId: "s-c1", part: 0, idx: 1 } });
  expect(typeof key).toBe("string");
  // The fork is open now, the banner has done its work, and the line sent has left the box.
  await waitFor(() => expect(currentStation().id).toBe("w-fork"), { interval: 1 });
  expect(screen.queryByText(/Your next message starts a fork/)).toBeNull();
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toHaveProperty("value", ""), { interval: 1 });
  // The line shows in the fork while the assistant opens it, and not in the original.
  expect(await within(screen.getByRole("log")).findByText("is a1 wrong?")).toBeTruthy();
  expect(screen.queryByText("a2")).toBeNull();
});

test("my line in a fork shows until the fork holds one more of mine than it opened with, not than the original has", async () => {
  open("c1");
  hold("c1", said(0, ME, "q1"), answered(1, "a1"), said(2, ME, "q2"), answered(3, "a2"));
  vi.mocked(sendChatLine).mockResolvedValue(FORKED);
  shell();
  await screen.findByText("a2");
  fireEvent.click(forkButton("a1"));
  await type("is a1 wrong?");
  const log = within(screen.getByRole("log"));
  await log.findByText("is a1 wrong?");
  // The fork opens with q1 and a1, so one of my lines is already there and the new one is still on its way.
  hold("f1", said(0, ME, "q1"), answered(1, "a1"));
  await waitFor(() => expect(articles()).toEqual(["q1", "a1", "is a1 wrong?"]), { interval: 1 });
  hold("f1", said(0, ME, "q1"), answered(1, "a1"), said(2, ME, "is a1 wrong?"));
  await changed("s-f1");
  await waitFor(() => expect(articles()).toEqual(["q1", "a1", "is a1 wrong?"]), { interval: 1 });
  expect(log.getAllByText("is a1 wrong?")).toHaveLength(1);
  expect(screen.getAllByRole("article")[2]!.className).not.toContain("pending");
});

test("Cancel fork goes back to posting into the chat", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID });
  shell();
  await screen.findByText("a2");
  fireEvent.click(forkButton("a1"));
  fireEvent.click(await screen.findByRole("button", { name: "Cancel fork" }));
  expect(screen.queryByText(/Your next message starts a fork/)).toBeNull();
  await type("still here");
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce(), { interval: 1 });
  expect(vi.mocked(sendChatLine).mock.calls[0]![0]).toMatchObject({ conversationId: "c1" });
  expect(vi.mocked(sendChatLine).mock.calls[0]![0]).not.toHaveProperty("fork");
});

/** Pick "Fork from here" on the first answer of the conversation `c1`, which `TALK` holds, for a station in `state`. */
async function pickFork(state: WorkspaceState) {
  open("c1");
  hold("c1", ...TALK);
  const view = shell(state);
  await screen.findByText("a2");
  fireEvent.click(forkButton("a1"));
  return view;
}

test("the fork banner says the fork runs on the assistant by its name", async () => {
  await pickFork(workspace);
  expect(screen.getByText("The fork runs on scout")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Cancel fork" })).toBeTruthy();
});

test("the fork banner says the fork runs on a CLI's live machine and folder", async () => {
  const inLoop: WorkspacePartner = { ...DETACHED, cwd: "~/project" };
  await pickFork({ ...workspace, provider: "claude", partner: inLoop });
  expect(screen.getByText("The fork runs on claude on gx10 in ~/project")).toBeTruthy();
});

test("the fork banner says a CLI whose run has ended gets its machine picked after the fork", async () => {
  const live: WorkspacePartner = { session_id: "s-chat", actor: "codex", cli: "codex", status: "live",
    label: "codex · gx10", provider: "codex", machine: "gx10" };
  const ended: WorkspacePartner = { ...live, status: "offline", label: "codex: no run", machine: null };
  const { again } = await pickFork({ ...workspace, provider: "codex", partner: live });
  again({ ...workspace, provider: "codex", partner: ended });
  expect(screen.getByText("The fork runs on codex (no run yet: pick a machine after)")).toBeTruthy();
});

test("a chat not shared with me says so, reads none of its lines and takes none of mine", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", GRACE, { forks_on_typing: true }));
  shell();
  expect((await screen.findByText(/This chat is not shared with you/)).textContent).toBe(
    "This chat is not shared with you, so you can neither read it nor post in it. Ask its starter to share it.");
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  expect(box.placeholder).toBe("This chat is not shared with you");
  expect(box.disabled).toBe(true);
  expect(listSessionParts).not.toHaveBeenCalled();
  expect(readSessionRecords).not.toHaveBeenCalled();
  expect(screen.queryByText("a2")).toBeNull();
  expect(screen.queryByText("Loading the conversation…")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("a staff-only chat says so in its header, offers no fork, and tells a reader without staff why it is closed", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", GRACE, { audience: "staff" }));
  const { view } = shell();
  await screen.findByText("a2");
  expect(within(document.querySelector<HTMLElement>(".chat-head")!).getByText("Staff only")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Fork from here" })).toBeNull();
  view.unmount();
  vi.mocked(getChatHead).mockResolvedValue(head("c1", GRACE, { audience: "staff", forks_on_typing: true }));
  shell();
  expect((await screen.findByText(/staff only and you hold no staff/)).textContent).toBe(
    "This chat is staff only and you hold no staff, so you can neither read it nor post in it.");
  expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).placeholder).toBe("This chat is staff only");
});

test("a private chat says so in its header and offers no fork; an open one offers it and has no mark", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", ME, { audience: "private" }));
  const { view } = shell();
  await screen.findByText("a2");
  expect(within(document.querySelector<HTMLElement>(".chat-head")!).getByText("Private")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Fork from here" })).toBeNull();
  view.unmount();
  vi.mocked(getChatHead).mockResolvedValue(head("c1", ME));
  shell();
  await screen.findByText("a2");
  expect(screen.getAllByRole("button", { name: "Fork from here" })).toHaveLength(2);
  expect(document.querySelector(".chat-badge")).toBeNull();
});

test("lines read before the chat closed to me go with it", async () => {
  open("c1");
  hold("c1", ...TALK);
  const { client } = shell();
  await screen.findByText("a2");
  vi.mocked(getChatHead).mockResolvedValue(head("c1", GRACE, { audience: "staff", forks_on_typing: true }));
  await act(() => client.invalidateQueries({ queryKey: ["chat", "head", "c1"] }));
  await screen.findByText(/staff only and you hold no staff/);
  expect(screen.queryByText("a2")).toBeNull();
});

test("the starter's switch says Public or Private and a click flips it; nobody else is offered it", async () => {
  open("c1");
  hold("c1", ...TALK);
  let audience = "open";
  vi.mocked(getChatHead).mockImplementation(async () => head("c1", ME, { may_hide: true, audience }));
  vi.mocked(setSessionAudience).mockImplementation(async (session, next) => {
    audience = next;
    return { session_id: session, audience: next, sessions: [session] };
  });
  const { view } = shell();
  await screen.findByText("a2");
  const controls = () => within(screen.getByRole("toolbar", { name: "Chat controls" }));
  openMore();
  fireEvent.click(controls().getByRole("button", { name: "Public chat: make it private" }));
  expect((await controls().findByRole("button", { name: "Private chat: make it public" })).textContent).toBe("Private");
  // The switch says it for the starter: no badge besides.
  expect(document.querySelector(".chat-badge")).toBeNull();
  fireEvent.click(controls().getByRole("button", { name: "Private chat: make it public" }));
  expect((await controls().findByRole("button", { name: "Public chat: make it private" })).textContent).toBe("Public");
  expect(vi.mocked(setSessionAudience).mock.calls.map(([session, next]) => [session, next])).toEqual([["s-c1", "private"], ["s-c1", "open"]]);
  view.unmount();
  vi.mocked(getChatHead).mockImplementation(async () => head("c1", GRACE));
  shell();
  await screen.findByText("a2");
  expect(screen.queryByRole("button", { name: /^(Public|Private) chat:/ })).toBeNull();
});

test("a refused flip says why in a toast, in words for the server's code, and leaves the chat as it was", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", ME, { may_hide: true }));
  vi.mocked(setSessionAudience)
    .mockRejectedValueOnce(new ApiError(403, "403 Forbidden", SESSION_NOT_OWNER))
    .mockRejectedValueOnce(new ApiError(409, "409 Conflict", SESSION_STAFF));
  shell();
  await screen.findByText("a2");
  openMore();
  fireEvent.click(screen.getByRole("button", { name: "Public chat: make it private" }));
  expect(await screen.findByText("Only the person who started this chat can make it public or private.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Public chat: make it private" }));
  expect(await screen.findByText("This chat is staff only, so it is never made public or private.")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Public chat: make it private" }).textContent).toBe("Public");
});

test("a conversation cannot be typed into before its head says whose it is", async () => {
  open("c1");
  hold("c1", ...TALK);
  let release: (value: ChatHead) => void = () => {};
  vi.mocked(getChatHead).mockReturnValue(new Promise((resolve) => { release = resolve; }));
  shell();
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  expect(box.disabled).toBe(true);
  await act(async () => release(head("c1", GRACE)));
  await screen.findByText("a2");
  await waitFor(() => expect(box.disabled).toBe(false), { interval: 1 });
  expect(sendChatLine).not.toHaveBeenCalled();
});

test("a post the server refuses as not shared with me is shown, never sent again as a fork", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(sendChatLine).mockRejectedValue(new ApiError(403, "This chat is not shared with you", CHAT_NOT_SHARED));
  shell();
  await screen.findByText("a2");
  await type("a question of my own");
  expect((await screen.findByRole("alert")).textContent).toContain("This chat is not shared with you");
  expect(sendChatLine).toHaveBeenCalledOnce();
  expect(vi.mocked(sendChatLine).mock.calls[0]![0]).not.toHaveProperty("fork");
  expect(currentStation().id).toBe(WORKSPACE_ID);
});

test.each([
  [CHAT_FORK_CLOSED, "This chat was hidden or made staff only, so it can no longer be forked."],
  [CHAT_FORK_WIDER, "This chat is shared with you, not with your organisation, which would read a fork of it; post in it instead."],
])("a fork the server refuses (%s) says why, drops the line it was to start from, keeps the draft, and reads the head again", async (code, why) => {
  open("c1");
  hold("c1", ...TALK);
  // The server names the refusal by its code alone.
  vi.mocked(sendChatLine).mockRejectedValue(new ApiError(409, "409 Conflict", code));
  shell();
  await screen.findByText("a2");
  fireEvent.click(forkButton("a1"));
  const heads = vi.mocked(getChatHead).mock.calls.length;
  await type("a branch");
  expect(await screen.findByText(why)).toBeTruthy();
  expect(screen.queryByText(/Your next message starts a fork/)).toBeNull();
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveProperty("value", "a branch");
  await waitFor(() => expect(vi.mocked(getChatHead).mock.calls.length).toBeGreaterThan(heads));
  expect(sendChatLine).toHaveBeenCalledOnce();
});

test("a 403 that is not the chat's refusal of the poster is shown, never sent as a fork", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(sendChatLine).mockRejectedValue(new ApiError(403, "Viewers cannot post", "role"));
  shell();
  await screen.findByText("a2");
  await type("a question");
  expect((await screen.findByRole("alert")).textContent).toContain("Viewers cannot post");
  expect(sendChatLine).toHaveBeenCalledOnce();
});

test("the box waits for the chat's lines, which a line sent counts its own among", async () => {
  open("c1");
  hold("c1", ...TALK);
  let release: (records: SessionRecord[]) => void = () => {};
  vi.mocked(readSessionRecords).mockImplementation(() => new Promise((resolve) => { release = resolve; }));
  shell();
  await waitFor(() => expect(getChatHead).toHaveBeenCalled());
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  await waitFor(() => expect(readSessionRecords).toHaveBeenCalled());
  expect(box.disabled).toBe(true);
  await act(async () => release(sessions.get("s-c1")!));
  await screen.findByText("a2");
  await waitFor(() => expect(box.disabled).toBe(false), { interval: 1 });
});

test("a post into a deleted chat lets it go and starts a new chat with the same line", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(sendChatLine).mockRejectedValueOnce(new ApiError(410, "This chat was deleted; start a new one", CHAT_DELETED))
    .mockResolvedValue({ conversation_id: "n1", session_id: null, workspace_id: WORKSPACE_ID });
  const { client } = shell();
  await screen.findByText("a2");
  await type("still there?");
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledTimes(2), { interval: 1 });
  const [[first], [second]] = vi.mocked(sendChatLine).mock.calls as [[object, string], [object, string]];
  expect(first).toMatchObject({ conversationId: "c1" });
  expect(second).toMatchObject({ text: "still there?", conversationId: null });
  expect(second).not.toHaveProperty("fork");
  await waitFor(() => expect(client.getQueryData<WorkspaceState>(["workspace", WORKSPACE_ID])?.conversation_id).toBe("n1"));
});

test("a fork of a deleted chat's line lets the chat go and starts a new chat with the line", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(sendChatLine).mockRejectedValueOnce(new ApiError(410, "This chat was deleted; start a new one", CHAT_DELETED))
    .mockResolvedValue({ conversation_id: "n1", session_id: null, workspace_id: WORKSPACE_ID });
  const { client } = shell();
  await screen.findByText("a2");
  fireEvent.click(forkButton("a1"));
  await type("a branch");
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledTimes(2), { interval: 1 });
  const [[first], [second]] = vi.mocked(sendChatLine).mock.calls as [[object, string], [object, string]];
  expect(first).toMatchObject({ conversationId: null, fork: { sessionId: "s-c1" } });
  expect(second).toMatchObject({ text: "a branch", conversationId: null });
  expect(second).not.toHaveProperty("fork");
  await waitFor(() => expect(client.getQueryData<WorkspaceState>(["workspace", WORKSPACE_ID])?.conversation_id).toBe("n1"));
  expect(screen.queryByText(/Your next message starts a fork/)).toBeNull();
});

test("opening a deleted chat by link says it was deleted", async () => {
  vi.mocked(getDetail).mockResolvedValue({ self: { cli_session_id: "chat:c9", labels: ["science-chat"] }, edges: {}, backlinks: {}, changes: [] } as never);
  vi.mocked(stationForChat).mockRejectedValue(new ApiError(410, "This chat was deleted; start a new one", CHAT_DELETED));
  const { feed } = shell();
  act(() => feed.continueIn("s9"));
  expect(await screen.findByText("This chat was deleted; start a new one")).toBeTruthy();
});

test("a head read that says the chat is deleted (410) leaves the station on an empty chat, with no error to retry", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockRejectedValue(new ApiError(410, "This chat was deleted; start a new one", CHAT_DELETED));
  const { client } = shell();
  await waitFor(() => expect(client.getQueryData<WorkspaceState>(["workspace", WORKSPACE_ID])?.conversation_id).toBeNull());
  expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  expect(screen.queryByText("a2")).toBeNull();
});

test("a refused post in a chat with no stored line says why and is not sent again", async () => {
  open("c1");
  hold("c1");
  vi.mocked(getChatHead).mockResolvedValue(head("c1", GRACE));
  vi.mocked(sendChatLine).mockRejectedValue(new ApiError(403, "403 Forbidden", CHAT_NOT_SHARED));
  shell();
  await waitFor(() => expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).disabled).toBe(false), { interval: 1 });
  await type("hello");
  expect((await screen.findByRole("alert")).textContent).toContain("403 Forbidden");
  expect(sendChatLine).toHaveBeenCalledOnce();
});

test("a chat says how often it was forked, and a fork says what it was forked from and opens it", async () => {
  open("f1");
  hold("f1", said(0, ME, "q1"), answered(1, "a1"), said(2, ME, "mine"));
  hold("c0", answered(0, "the original"));
  vi.mocked(getChatHead).mockImplementation(async (id) => (id === "f1" ? head("f1", ME, { forked_from: "c0", forks: 1 }) : head(id)));
  shell();
  expect(await screen.findByText("Forked once.")).toBeTruthy();
  vi.mocked(stationForChat).mockResolvedValue({ ...workspace, id: "w-c0", name: "unnamed-workstream#3", conversation_id: "c0" });
  fireEvent.click(screen.getByRole("button", { name: "another chat" }));
  expect(await screen.findByText("the original")).toBeTruthy();
  expect(stationForChat).toHaveBeenCalledWith("c0");
  expect(currentStation().id).toBe("w-c0");
  cleanup();
  vi.mocked(getChatHead).mockResolvedValue(head("c0", ME, { forks: 3 }));
  shell();
  expect(await screen.findByText("Forked 3 times.")).toBeTruthy();
});

test("a line still being sent, and a chat I cannot post in, offer no fork", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(sendChatLine).mockImplementation(() => new Promise(() => {}));
  const { view } = shell();
  await screen.findByText("a2");
  // One for each answer, a1 and a2.
  expect(screen.getAllByRole("button", { name: "Fork from here" })).toHaveLength(2);
  await type("on its way");
  const pending = (await within(screen.getByRole("log")).findByText("on its way")).closest("article")!;
  expect(within(pending).queryByRole("button", { name: "Fork from here" })).toBeNull();
  view.unmount();
  shell(workspace, { profile: { role: "viewer" } });
  await screen.findByText("a2");
  expect(screen.queryByRole("button", { name: "Fork from here" })).toBeNull();
});

// What an answer cites lights up on the canvas as it arrives, through the store the agent's Highlight feeds.

const idOf = (kind: string, seq: number) => `id-${kind}-${seq}`;
const marked = (highlights: HighlightStore) => [...highlights.snapshot()].toSorted();

/** Hold a conversation with one line of mine, show it, and return the shell; its answers come by `reply`. */
async function conversation() {
  open("c1");
  hold("c1", said(0, ME, "What is open?"));
  const view = shell();
  await screen.findByText("What is open?");
  const lines = [said(0, ME, "What is open?")];
  const reply = async (content: string) => {
    lines.push(answered(lines.length, content));
    hold("c1", ...lines);
    await changed("s-c1");
  };
  return { ...view, reply };
}

test("an answer that arrives lights up every row it cites, any kind, resolving each once", async () => {
  const { highlights, reply } = await conversation();
  await reply("Start with Belief#7 and Issue#12, then AgentSession#4 and Issue#12 again.");
  await waitFor(() => expect(marked(highlights)).toEqual([idOf("AgentSession", 4), idOf("Belief", 7), idOf("Issue", 12)]), { interval: 1 });
  expect(vi.mocked(findRef).mock.calls.map(([kind, seq]) => `${kind}#${seq}`).toSorted()).toEqual(["AgentSession#4", "Belief#7", "Issue#12"]);
});

test("a row named by its id lights up with no lookup", async () => {
  const { highlights, reply } = await conversation();
  await reply("It is 61D3A095-C7F1-4D27-A4C4-A5B1C218A31E.");
  await waitFor(() => expect(marked(highlights)).toEqual(["61d3a095-c7f1-4d27-a4c4-a5b1c218a31e"]), { interval: 1 });
  expect(findRef).not.toHaveBeenCalled();
});

test("the rows are looked up together, and one that does not resolve does not hold the others back", async () => {
  const { highlights, reply } = await conversation();
  const pending: ((id: string) => void)[] = [];
  vi.mocked(findRef).mockImplementation((kind, seq) => kind === "Belief"
    ? Promise.reject(new ApiError(404, "gone"))
    : new Promise((resolve) => pending.push((id) => resolve(id + seq))));
  await reply("Issue#1, Belief#2 and Issue#3");
  await waitFor(() => expect(pending).toHaveLength(2), { interval: 1 });
  expect(marked(highlights)).toEqual([]);
  act(() => pending.forEach((resolve) => resolve("row-")));
  await waitFor(() => expect(marked(highlights)).toEqual(["row-1", "row-3"]), { interval: 1 });
});

test("each row lights as its own lookup returns, so a slow one holds the rest up no longer", async () => {
  const { highlights, reply } = await conversation();
  let release = (_id: string) => {};
  vi.mocked(findRef).mockImplementation((kind, seq) => seq === 2
    ? new Promise((resolve) => { release = resolve; })
    : Promise.resolve(idOf(kind, seq)));
  await reply("Issue#1, Issue#2 and Issue#3");
  await waitFor(() => expect(marked(highlights)).toEqual([idOf("Issue", 1), idOf("Issue", 3)]), { interval: 1 });
  await act(async () => release("slow"));
  expect(marked(highlights)).toEqual([idOf("Issue", 1), idOf("Issue", 3), "slow"]);
});

test("a lookup that fails is not retried, as the app's read retries would hold the row dark for seconds", async () => {
  vi.mocked(findRef).mockImplementation(async (kind, seq) => {
    if (seq === 2) throw new ApiError(503, "busy");
    return idOf(kind, seq);
  });
  open("c1");
  hold("c1", said(0, ME, "What is open?"));
  const { highlights } = shell(workspace, { queries: { retry: 3, retryDelay: 1 } });
  await screen.findByText("What is open?");
  hold("c1", said(0, ME, "What is open?"), answered(1, "Issue#1 and Issue#2"));
  await changed("s-c1");
  await waitFor(() => expect(marked(highlights)).toEqual([idOf("Issue", 1)]), { interval: 1 });
  // Long enough for the retries the app would make at this delay.
  await new Promise((resolve) => setTimeout(resolve, 30));
  expect(vi.mocked(findRef).mock.calls.filter(([, seq]) => seq === 2)).toHaveLength(1);
});

test("an answer after the agent called Highlight leaves its marks alone, and the next turn's answer lights again", async () => {
  const { highlights } = await conversation();
  hold("c1", said(0, ME, "What is open?"), called(1, "Highlight"), answered(2, "Compare with Issue#12."));
  await changed("s-c1");
  await screen.findByText(/Compare with/);
  expect(findRef).not.toHaveBeenCalled();
  expect(marked(highlights)).toEqual([]);
  hold("c1", said(0, ME, "What is open?"), called(1, "Highlight"), answered(2, "Compare with Issue#12."),
    said(3, ME, "and?"), called(4, "SearchRecords"), answered(5, "See Issue#13."));
  await changed("s-c1");
  await waitFor(() => expect(marked(highlights)).toEqual([idOf("Issue", 13)]), { interval: 1 });
});

test("rows an answer quotes in code are not cited, and an answer that cites none leaves the marks as they were", async () => {
  const { highlights, reply } = await conversation();
  await reply("Issue#1 is the one.");
  await waitFor(() => expect(marked(highlights)).toEqual([idOf("Issue", 1)]), { interval: 1 });
  await reply("Write refs as `Issue#2`, quoted.");
  expect((await screen.findByText("Issue#2")).tagName).toBe("CODE");
  expect(marked(highlights)).toEqual([idOf("Issue", 1)]);
  expect(vi.mocked(findRef).mock.calls.map(([kind, seq]) => `${kind}#${seq}`)).toEqual(["Issue#1"]);
});

test("the newest answer replaces the marks, and a slower lookup for an older one does not overwrite it", async () => {
  const { highlights, reply } = await conversation();
  let release = (_id: string) => {};
  vi.mocked(findRef).mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
  await reply("First: Issue#1");
  await waitFor(() => expect(findRef).toHaveBeenCalledTimes(1), { interval: 1 });
  await reply("Then: Issue#2");
  await waitFor(() => expect(marked(highlights)).toEqual([idOf("Issue", 2)]), { interval: 1 });
  await act(async () => release("late"));
  expect(marked(highlights)).toEqual([idOf("Issue", 2)]);
});

test("a row seen before costs no second lookup", async () => {
  const { highlights, reply } = await conversation();
  await reply("Issue#5");
  await waitFor(() => expect(marked(highlights)).toEqual([idOf("Issue", 5)]), { interval: 1 });
  await reply("Issue#5 again, with Issue#6");
  await waitFor(() => expect(marked(highlights)).toEqual([idOf("Issue", 5), idOf("Issue", 6)]), { interval: 1 });
  expect(vi.mocked(findRef).mock.calls.map(([kind, seq]) => `${kind}#${seq}`)).toEqual(["Issue#5", "Issue#6"]);
});

test("answers already in a conversation when it opens light nothing: only a new one does", async () => {
  vi.mocked(findRef).mockImplementation(async (kind, seq) => idOf(kind, seq));
  open("c1");
  hold("c1", said(0, ME, "q"), answered(1, "Earlier: Issue#1"));
  const { highlights } = shell();
  await screen.findByText(/Earlier/);
  expect(marked(highlights)).toEqual([]);
  expect(findRef).not.toHaveBeenCalled();
  hold("c1", said(0, ME, "q"), answered(1, "Earlier: Issue#1"), said(2, ME, "q2"), answered(3, "Nothing to cite"));
  await changed("s-c1");
  await screen.findByText("Nothing to cite");
  expect(marked(highlights)).toEqual([]);
  hold("c1", said(0, ME, "q"), answered(1, "Earlier: Issue#1"), said(2, ME, "q2"), answered(3, "Nothing to cite"), said(4, ME, "q3"), answered(5, "Now: Issue#2"));
  await changed("s-c1");
  await waitFor(() => expect(marked(highlights)).toEqual([idOf("Issue", 2)]), { interval: 1 });
  expect(vi.mocked(findRef).mock.calls.map(([kind, seq]) => `${kind}#${seq}`)).toEqual(["Issue#2"]);
});

test("with the switch off an answer lights nothing, and turning it on lights only answers that come after", async () => {
  const { highlights, reply } = await conversation();
  act(() => receivePreferences({ ...preferences(), highlight_mentions: false }));
  await reply("Issue#1 and Issue#2");
  await screen.findByText(/Issue#1/);
  expect(marked(highlights)).toEqual([]);
  expect(findRef).not.toHaveBeenCalled();
  act(() => receivePreferences({ ...preferences(), highlight_mentions: true }));
  expect(marked(highlights)).toEqual([]);
  await reply("Issue#3");
  await waitFor(() => expect(marked(highlights)).toEqual([idOf("Issue", 3)]), { interval: 1 });
});

test("the switch is honoured while a lookup is under way", async () => {
  const { highlights, reply } = await conversation();
  let release = (_id: string) => {};
  vi.mocked(findRef).mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
  await reply("Issue#1");
  await waitFor(() => expect(findRef).toHaveBeenCalledTimes(1), { interval: 1 });
  act(() => receivePreferences({ ...preferences(), highlight_mentions: false }));
  await act(async () => release("row"));
  expect(marked(highlights)).toEqual([]);
});

test("a recap whose last answer proposes a next experiment offers to launch it, sending the canned ask", async () => {
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID });
  open("c1");
  hold("c1", said(0, ME, "how did it go?"), answered(1, "## Results\nThe climb held.\n\n## Next experiment\nRaise the effort to high."));
  shell();
  const launch = await screen.findByRole("button", { name: "Launch the follow-up" });
  fireEvent.click(launch);
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce(), { interval: 1 });
  expect(vi.mocked(sendChatLine).mock.calls[0]![0].text).toBe(FOLLOW_UP_ASK);
});

test("a refused follow-up says why and is not an unhandled rejection; it waits like the composer", async () => {
  vi.mocked(sendChatLine).mockRejectedValue(new ApiError(400, "Too many lines"));
  open("c1");
  hold("c1", said(0, ME, "how did it go?"), answered(1, "## Results\nok\n\n## Next experiment\nRaise it."));
  shell();
  fireEvent.click(await screen.findByRole("button", { name: "Launch the follow-up" }));
  expect(await screen.findByText("Too many lines")).toBeTruthy();
  expect(sendChatLine).toHaveBeenCalledOnce();
});

test("a follow-up whose answer was lost resends under its key, and leaves the typed draft alone", async () => {
  vi.mocked(sendChatLine).mockRejectedValueOnce(new ApiError(0, "offline", "network"))
    .mockResolvedValue({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID });
  open("c1");
  hold("c1", said(0, ME, "how did it go?"), answered(1, "## Results\nok\n\n## Next experiment\nRaise it."));
  shell();
  const box = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(box, { target: { value: "my own draft" } });
  fireEvent.click(await screen.findByRole("button", { name: "Launch the follow-up" }));
  await screen.findByText("Could not send this message. Retry to send the same draft safely.");
  fireEvent.click(await screen.findByRole("button", { name: "Launch the follow-up" }));
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledTimes(2));
  const [first, second] = vi.mocked(sendChatLine).mock.calls;
  expect(second![1]).toBe(first![1]);
  expect(box).toHaveProperty("value", "my own draft");
});

test("a follow-up whose answer was lost takes a fresh key once it goes to another target", async () => {
  vi.mocked(sendChatLine).mockRejectedValueOnce(new ApiError(0, "offline", "network"))
    .mockResolvedValue({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID });
  open("c1");
  hold("c1", said(0, ME, "how did it go?"), answered(1, "## Results\nok\n\n## Next experiment\nRaise it."));
  const { again } = shell();
  fireEvent.click(await screen.findByRole("button", { name: "Launch the follow-up" }));
  await screen.findByText("Could not send this message. Retry to send the same draft safely.");
  again({ ...workspace, partner: { ...SCOUT, session_id: "restarted" } });
  fireEvent.click(await screen.findByRole("button", { name: "Launch the follow-up" }));
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledTimes(2));
  const [first, second] = vi.mocked(sendChatLine).mock.calls;
  expect(second![1]).not.toBe(first![1]);
});

test("while a follow-up is on its way, Enter sends no second line, and the box stays editable", async () => {
  let finish = (_value: { conversation_id: string; session_id: string | null; workspace_id: string }) => {};
  vi.mocked(sendChatLine).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  open("c1");
  hold("c1", said(0, ME, "how did it go?"), answered(1, "## Results\nok\n\n## Next experiment\nRaise it."));
  shell();
  fireEvent.click(await screen.findByRole("button", { name: "Launch the follow-up" }));
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce());
  const box = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(box, { target: { value: "also this" } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(box).toHaveProperty("disabled", false);
  expect(sendChatLine).toHaveBeenCalledOnce();
  await act(async () => finish({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID }));
  expect(box).toHaveProperty("value", "also this");
});

test("a plain last answer offers no follow-up", async () => {
  open("c1");
  hold("c1", said(0, ME, "and then?"), answered(1, "The run plateaued; nothing new to try."));
  shell();
  await screen.findByText("The run plateaued; nothing new to try.");
  expect(screen.queryByRole("button", { name: "Launch the follow-up" })).toBeNull();
});

test("hovering a stored line shows when it was sent, in the reader's own time; a line still being sent shows none", async () => {
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID });
  open("c1");
  hold("c1", { ...said(0, ME, "my question"), timestamp: "2026-10-13T11:50:00Z" } as SessionRecord);
  shell();
  const line = (await screen.findByText("my question")).closest("article")!;
  expect(line.getAttribute("title")).toMatch(/^Sent .*2026/);
  await type("still sending");
  await screen.findByText("still sending");
  const pending = document.querySelector(".chat-line-pending");
  expect(pending).not.toBeNull();
  expect(pending!.getAttribute("title")).toBeNull();
});

test("the header names the provider the chat talks to, with its live label and dot", () => {
  shell({ ...workspace, provider: "claude", partner: { ...SCOUT, status: "live" as const, provider: "claude", label: "claude · gx10", machine: "gx10" } });
  const button = screen.getByRole("button", { name: "Chat provider: claude" });
  expect(button.textContent).toContain("claude · gx10");
  expect(button.querySelector(".provider-dot")?.classList.contains("live")).toBe(true);
});

test("+ New chat offers the three providers, the default first and starred, and a grayed one does nothing", async () => {
  vi.mocked(createStationInWorkstream).mockResolvedValue({ ...workspace, id: "new-chat", name: "unnamed-workstream#2" });
  const { client } = shell({ ...workspace, available: { claude: false },
    reasons: { claude: "Connect a machine with `trax machine connect` and install claude on it" } }, {
    // The preference a profile carries, which openapi.json does not list yet.
    profile: { preferences: { chat_provider: "codex" } as unknown as Profile["preferences"] },
  });
  client.setQueryData(["workstreams"], [{ id: "scratch", title: "Scratch", hidden: false, pinned: false, scratch: true, seq: 1 }]);
  fireEvent.click(screen.getByRole("button", { name: "Scratch chat" }));
  const rows = screen.getAllByRole("button", { name: /\+ New chat · / });
  expect(rows.map((each) => each.textContent)).toEqual(["+ New chat · codex ★ default", "+ New chat · scout", "+ New chat · claude"]);
  expect((rows[2] as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText(/install claude on it/)).toBeTruthy();
  fireEvent.click(rows[2]!);
  expect(createStationInWorkstream).not.toHaveBeenCalled();
  fireEvent.click(rows[0]!);
  await waitFor(() => expect(createStationInWorkstream).toHaveBeenCalledWith("scratch", "codex"));
});

test("a server with no assistant offers no assistant in + New chat", async () => {
  shell({ ...workspace, assistant: null }, { profile: { preferences: { chat_provider: "codex" } as unknown as Profile["preferences"] } });
  fireEvent.click(screen.getByRole("button", { name: "Scratch chat" }));
  expect(screen.getAllByRole("button", { name: /\+ New chat · / }).map((each) => each.textContent))
    .toEqual(["+ New chat · codex ★ default", "+ New chat · claude"]);
});

// Tool calls between the lines: the same conversation as each provider records it,
// with the Settings switch off and on.
const stampedAt = (at: SessionRecord, minute: number): SessionRecord => ({ ...at, timestamp: `2026-10-21T10:0${minute}:00Z` }) as SessionRecord;
const TOOL_TALKS: readonly (readonly [string, readonly SessionRecord[]])[] = [
  ["sagent", [said(0, ME, "list the home directory"),
    record(1, "ToolCall", { call_id: "kb1", name: "Bash", arguments: { command: "ls ~" } }),
    record(2, "ShellCommandResult", { call_id: "kb1", command: ["ls ~"], stdout: "notes\nsrc\n", extra: { is_error: false } }),
    answered(3, "Two entries.")]],
  ["claude", [record(0, "UserMessage", { content: "list the home directory", extra: { author: ME } }),
    record(1, "ToolCall", { call_id: "toolu_1", name: "Bash", arguments: { command: "ls ~", description: "List home" } }),
    record(2, "ShellCommandResult", { call_id: "toolu_1", command: ["ls ~"], stdout: "notes\nsrc\n", stderr: "", exit_code: 0 }),
    answered(3, "Two entries.")]],
  ["codex", [record(0, "UserMessage", { content: "list the home directory", extra: { author: ME } }),
    record(1, "ToolCall", { call_id: "call_1", name: "shell", arguments: { command: ["bash", "-lc", "ls ~"], workdir: "/home/ada" } }),
    record(2, "ShellCommandResult", { call_id: "call_1", command: ["bash", "-lc", "ls ~"], stdout: "notes\nsrc\n", stderr: "", exit_code: 0 }),
    answered(3, "Two entries.")]],
];
const cardsOn = () => act(() => receivePreferences({ ...preferences(), chat_tool_calls: true }));
const kindsShown = () => [...document.querySelectorAll(".chat-lines > article")].map((line) => /chat-line-(user|assistant|tool)\b/.exec(line.className)?.[1]);

test.each(TOOL_TALKS)("with the switch off, a %s conversation shows its lines and no tool card", async (_provider, records) => {
  open("c1");
  hold("c1", ...records);
  shell();
  await screen.findByText("Two entries.");
  expect(articles()).toEqual(["list the home directory", "Two entries."]);
  expect(document.querySelector(".chat-line-tool")).toBeNull();
  expect(screen.queryByText("Ran")).toBeNull();
});

test.each(TOOL_TALKS)("with the switch on, a %s conversation shows its call as a card between the lines, opening to its output", async (_provider, records) => {
  cardsOn();
  open("c1");
  hold("c1", ...records.map((record, minute) => stampedAt(record, minute)));
  shell();
  await screen.findByText("Two entries.");
  expect(kindsShown()).toEqual(["user", "tool", "assistant"]);
  const card = document.querySelector<HTMLElement>(".chat-line-tool")!;
  expect(card.querySelector(".tool-row .name")!.textContent).toBe("Ran");
  expect(card.querySelector(".tool-arg")!.textContent).toBe("ls ~");
  expect(card.querySelector(".chat-tool-state")!.textContent).toBe("done");
  expect(card.querySelector(".tool-out")).toBeNull();
  fireEvent.click(card.querySelector("summary")!);
  await waitFor(() => expect(card.querySelector(".tool-out")!.textContent).toBe("notes\nsrc\n"));
  // A card is no message: it has no fork button, and the working line does not stand
  // for it.
  expect(within(card).queryByRole("button", { name: "Fork from here" })).toBeNull();
});

test("turning the switch on shows the cards of the conversation open, and turning it off takes them away", async () => {
  open("c1");
  hold("c1", ...TOOL_TALKS[1]![1]);
  shell();
  await screen.findByText("Two entries.");
  expect(document.querySelector(".chat-line-tool")).toBeNull();
  cardsOn();
  expect(document.querySelectorAll(".chat-line-tool")).toHaveLength(1);
  act(() => receivePreferences({ ...preferences(), chat_tool_calls: false }));
  expect(document.querySelector(".chat-line-tool")).toBeNull();
});

test("a call still running shows as a running card, and a failed one as failed with its error open", async () => {
  cardsOn();
  open("c1");
  hold("c1", said(0, ME, "run both"),
    record(1, "ToolCall", { call_id: "a", name: "Bash", arguments: { command: "sleep 200" } }),
    record(2, "ToolCall", { call_id: "b", name: "Bash", arguments: { command: "ls /nope" } }),
    record(3, "ShellCommandResult", { call_id: "b", command: ["ls /nope"], stdout: "", stderr: "no such directory\n", exit_code: 2 }));
  shell();
  await waitFor(() => expect(document.querySelectorAll(".chat-line-tool")).toHaveLength(2));
  const [running, failed] = [...document.querySelectorAll<HTMLElement>(".chat-line-tool")];
  expect(running!.querySelector(".chat-tool-state")!.textContent).toBe("running");
  expect(failed!.querySelector(".chat-tool-state")!.textContent).toBe("failed");
  expect(failed!.querySelector(".tool-out")!.textContent).toBe("no such directory\n");
  // The agent is still working on the running call: its name stays on the working line.
  expect(screen.getByText("Working: Bash…")).toBeTruthy();
});

// The unified ask path: a card of the agent's own options, a dot while any waits, and a takeover banner with Reattach.
const ASK = { id: "a1", type: "permission", tool: "shell", input: {}, questions: null, plan: null, state: "pending", provider: "codex", kind: "command",
  title: "Run a command", tool_call_id: "", command: "make test", cwd: "/work", reason: "", diff: "", answerable_in_chat: true,
  options: [{ id: "turn", label: "Yes", kind: "allow_once" }, { id: "decline", label: "No", kind: "deny" }] };
const asking = (idx: number) => record(idx, "SystemMessage", { extra: { $interaction: ASK } });
const settled = (idx: number, body: { [field: string]: unknown }) => record(idx, "SystemMessage", { extra: { $interaction_resolved: { id: "a1", ...body } } });
const CODEX_RUN: WorkspacePartner = { ...(livePartner as WorkspacePartner), provider: "codex", cli: "codex", label: "codex · gx10" };
/** The station of a codex chat whose run is as `run` says, over the live one. */
function codexChat(run: Partial<WorkspacePartner> = {}): WorkspaceState {
  const partner: WorkspacePartner = { ...CODEX_RUN, ...run };
  return { ...workspace, provider: "codex", partner };
}

test("an ask with options shows the agent's buttons, and a click posts that option as the chat's answer", async () => {
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: "s-c1", workspace_id: WORKSPACE_ID });
  open("c1");
  hold("c1", said(0, ME, "run the suite"), asking(1));
  shell(codexChat());
  const card = (await screen.findByText("Codex wants to run a command")).closest("article")!;
  fireEvent.click(within(card).getByRole("button", { name: "No" }));
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce());
  const [line] = vi.mocked(sendChatLine).mock.calls[0]!;
  expect(line).toMatchObject({ kind: "answer", text: "No", payload: { prompt_id: "a1", option: "decline", message: "" } });
  expect(line.payload).not.toHaveProperty("decision");
});

test("the chat's header shows a dot while an ask waits, and drops it once the ask is resolved or released", async () => {
  open("c1");
  hold("c1", said(0, ME, "run the suite"), asking(1));
  const { client } = shell(codexChat());
  await screen.findByText("Codex wants to run a command");
  expect(screen.getByRole("status", { name: "Waiting for your answer" })).toBeTruthy();
  hold("c1", said(0, ME, "run the suite"), asking(1), settled(2, { option: "", by: "released", who: "", decision: "released" }));
  await act(async () => { await client.invalidateQueries(); });
  await changed("s-c1");
  await waitFor(() => expect(screen.queryByRole("status", { name: "Waiting for your answer" })).toBeNull());
});

test("the dot follows the server's word too: it shows with no open card in this log, and an open card shows it whatever the server last said", async () => {
  open("c1");
  hold("c1", said(0, ME, "hello"));
  shell(codexChat({ state: "requires_action" }));
  await screen.findByText("hello");
  expect(screen.getByRole("status", { name: "Waiting for your answer" })).toBeTruthy();
  cleanup();
  hold("c1", said(0, ME, "hello"));
  shell(codexChat({ state: "running" }));
  await screen.findByText("hello");
  expect(screen.queryByRole("status", { name: "Waiting for your answer" })).toBeNull();
  cleanup();
  hold("c1", said(0, ME, "run the suite"), asking(1));
  shell(codexChat({ state: "idle" }));
  await screen.findByText("Codex wants to run a command");
  expect(screen.getByRole("status", { name: "Waiting for your answer" })).toBeTruthy();
});

test("a chat taken over in a terminal says so in place of Chat is off, takes no line, and Reattach to Chat reopens its run", async () => {
  vi.mocked(reattachChat).mockResolvedValue(undefined);
  open("c1");
  hold("c1", said(0, ME, "run the suite"));
  const { client } = shell(codexChat({ status: "offline", label: "codex: no run", state: "taken_over" }));
  const invalidated = vi.spyOn(client, "invalidateQueries");
  expect(await screen.findByText(/^Taken over in a terminal/)).toBeTruthy();
  expect(screen.queryByText(/^Chat is off/)).toBeNull();
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" }).getAttribute("placeholder")).toBe("Taken over in a terminal"));
  fireEvent.click(screen.getByRole("button", { name: "Reattach to Chat" }));
  await waitFor(() => expect(reattachChat).toHaveBeenCalledOnce());
  expect(vi.mocked(reattachChat).mock.calls[0]).toEqual(["s-c1", expect.any(String)]);
  await waitFor(() => expect(invalidated).toHaveBeenCalledWith({ queryKey: ["workspace"] }));
});

test("a run taken over while its partner is still live takes no line, and its open card is answered in the terminal", async () => {
  open("c1");
  hold("c1", said(0, ME, "run the suite"), asking(1));
  shell(codexChat({ state: "taken_over" }));
  const card = (await screen.findByText("Codex wants to run a command")).closest("article")!;
  expect(within(card).getByText("Answer it in the terminal.")).toBeTruthy();
  for (const button of within(card).getAllByRole("button")) expect(button).toHaveProperty("disabled", true);
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("textbox", { name: "Message" }).getAttribute("placeholder")).toBe("Taken over in a terminal");
  // Only the terminal can answer the card now, so Chat does not say it waits on the person.
  expect(screen.queryByRole("status", { name: "Waiting for your answer" })).toBeNull();
});

test("a card left open in a chat whose run is gone shows no dot: nobody can answer it from here", async () => {
  open("c1");
  hold("c1", said(0, ME, "run the suite"), asking(1));
  shell(codexChat({ status: "offline", label: "codex: no run" }));
  await screen.findByText("Codex wants to run a command");
  expect(screen.queryByRole("status", { name: "Waiting for your answer" })).toBeNull();
});

test("a refused reattach shows the server's reason and the next press is a new request; a lost answer is repeated under its key", async () => {
  vi.mocked(reattachChat).mockRejectedValueOnce(new ApiError(409, "This chat's run is live.", "chat_run_live"))
    .mockRejectedValueOnce(new ApiError(0, "The server could not be reached", "network")).mockResolvedValue(undefined);
  open("c1");
  hold("c1", said(0, ME, "run the suite"));
  shell(codexChat({ status: "offline", state: "taken_over" }));
  const button = await screen.findByRole("button", { name: "Reattach to Chat" });
  fireEvent.click(button);
  expect((await screen.findByText("This chat's run is live.")).className).toContain("chat-note");
  await waitFor(() => expect(button).toHaveProperty("disabled", false));
  fireEvent.click(button);
  await waitFor(() => expect(reattachChat).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(button).toHaveProperty("disabled", false));
  fireEvent.click(button);
  await waitFor(() => expect(reattachChat).toHaveBeenCalledTimes(3));
  const keys = vi.mocked(reattachChat).mock.calls.map(([, key]) => key);
  expect([keys[0] === keys[1], keys[1] === keys[2]]).toEqual([false, true]);
});

test("a viewer who cannot post is not offered Reattach", async () => {
  open("c1");
  hold("c1", said(0, ME, "run the suite"));
  shell(codexChat({ status: "offline", state: "taken_over" }), { profile: { role: "viewer" } });
  expect(await screen.findByText(/^Taken over in a terminal/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Reattach to Chat" })).toBeNull();
});

test("with tool cards on, an ask that names its call sits flush under that call's card", async () => {
  cardsOn();
  open("c1");
  hold("c1", said(0, ME, "run the suite"), record(1, "ToolCall", { call_id: "item_1", name: "shell", arguments: { command: "make test" } }),
    record(2, "AssistantMessage", { content: "Waiting on you." }), record(3, "SystemMessage", { extra: { $interaction: { ...ASK, tool_call_id: "item_1" } } }));
  shell(codexChat());
  const card = (await screen.findByText("Codex wants to run a command")).closest("article")!;
  expect(card.className).toContain("prompt-card-attached");
  expect(card.previousElementSibling!.className).toContain("chat-line-tool");
});
