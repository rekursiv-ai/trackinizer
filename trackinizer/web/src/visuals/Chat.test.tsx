import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { CHAT_DELETED, CHAT_FORK_CLOSED, CHAT_FORK_WIDER, CHAT_NOT_SHARED, CHAT_STAFF_ONLY, type ChatHead, type ChatPartial, type ChatPartialState, deleteChat, getChatHead,
  readChatPartial, sendChatLine, shareChat, unshareChat } from "../api/chats";
import { stationForChat } from "../api/stations";
import { ApiError, type CallOptions } from "../api/client";
import { findRef, getDetail } from "../api/detail";
import type { Profile } from "../api/me";
import { listSessionParts, readSessionRecords, SESSION_NOT_OWNER, SESSION_STAFF, type SessionRecord, setSessionAudience } from "../api/sessions";
import { getWorkspace, type WorkspaceState } from "../api/workspaces";
import { MetaContext, ProfileContext } from "../app/boot";
import { HighlightContext, HighlightStore } from "../app/highlights";
import { META } from "../detail/testing";
import { LiveContext } from "../live";
import type { LiveHub } from "../live/hub";
import type { LiveQuery } from "../live/serial";
import { startTrail } from "../router/trail";
import { chooseChatStyle } from "../chatStyle";
import { preferences, receivePreferences, sendWaiting } from "../preferences";
import { Chat } from "./Chat";
import { setStation, currentStation } from "../stations/store";
import { useStation } from "../stations/useStation";
import { currentChatColumn, setChatHidden, setChatSide } from "./chatColumnState";
import { ChatFeed, ChatFeedContext } from "./chatFeed";
import { FOLLOW_UP_ASK } from "./chatFollowUp";
import { OPENING } from "./chatRecords";
import { ToastProvider } from "../ui/toast";
import { AGREED } from "../api/testing";

vi.mock("../api/chats", async (original) => ({
  ...(await original<typeof import("../api/chats")>()),
  getChatHead: vi.fn(),
  readChatPartial: vi.fn(),
  sendChatLine: vi.fn(),
  shareChat: vi.fn(),
  unshareChat: vi.fn(),
  deleteChat: vi.fn(),
}));
vi.mock("../api/stations", () => ({ stationForChat: vi.fn() }));
vi.mock("../api/workspaces", () => ({ getWorkspace: vi.fn() }));
vi.mock("../api/sessions", async (original) => ({
  ...(await original<typeof import("../api/sessions")>()),
  listSessionParts: vi.fn(),
  readSessionRecords: vi.fn(),
  setSessionAudience: vi.fn(),
}));
vi.mock("../api/detail", () => ({ getDetail: vi.fn(), findRef: vi.fn() }));

const WORKSPACE_ID = "c5286865-67b6-4bd8-ab51-e06e10c326c5";
const ME = "ada@example.com";
const GRACE = "grace@example.com";
const SCOUT = { kind: "shared", session_id: "kb-session", actor: "scout", cli: "sagent", status: "live" } as const;
const NO_HELPER = { kind: "local", session_id: null, actor: null, cli: null, status: "unavailable" } as const;
const workspace: WorkspaceState = {
  id: WORKSPACE_ID, name: "unnamed-workstream#1", conversation_id: null, page: null, revision: 3, focused_instance: null, partner: SCOUT, assistant: "scout", partner_choice: "shared",
  visuals: [{ id: "889ffcb2-cf44-43e7-9806-eb08428c6203", type: "trax.browse", version: 1,
    placement: "main", record_id: null, params: {} }],
};
const PROFILE: Profile = { user_id: "u1", email: ME, name: "Ada", role: "writer", last_login: null, visual_workspace_enabled: true, ...AGREED };

/** The records of the session each conversation is held in, as the fake server has them. */
const sessions = new Map<string, SessionRecord[]>();
/** The live layer's registrations, which a test drives by hand as the stream does. */
let registered: LiveQuery[] = [];

beforeEach(() => {
  vi.mocked(getWorkspace).mockResolvedValue({ ...workspace, id: "w-fork", name: "unnamed-workstream#2", conversation_id: "f1" });
  OPENING.everyMs = 5;
  OPENING.giveUpMs = 150;
  // A cited row resolves to an id unless a test says otherwise, as the server's route answers.
  vi.mocked(findRef).mockImplementation(async (kind, seq) => `id-${kind}-${seq}`);
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
  for (const mock of [getChatHead, readChatPartial, sendChatLine, shareChat, unshareChat, deleteChat, stationForChat, getWorkspace, listSessionParts, readSessionRecords, setSessionAudience, getDetail, findRef]) {
    vi.mocked(mock).mockReset();
  }
  sessions.clear();
  registered = [];
  localStorage.clear();
  // A save still on its way (a flip of Chat's side, a style) would make the page ignore the next test's preferences.
  await sendWaiting();
  receivePreferences({ ...preferences(), highlight_mentions: true });
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
  const highlights = new HighlightStore();
  const client = new QueryClient({ defaultOptions: { queries: options.queries ?? { retry: false } } });
  const live = { register: (query: LiveQuery) => {
    registered.push(query);
    return { push: () => {}, dispose: () => { registered = registered.filter((each) => each !== query); } };
  } } as unknown as LiveHub;
  const held = (next: WorkspaceState) => (opened ? { ...next, conversation_id: opened } : next);
  setStation(state.id);
  client.setQueryData(["workspace", state.id], held(state));
  const ui = (pinned?: WorkspaceState) => <ToastProvider><QueryClientProvider client={client}><MetaContext value={META}>
    <ProfileContext value={{ ...PROFILE, ...options.profile }}><LiveContext value={live}>
      <ChatFeedContext value={feed}><HighlightContext value={highlights}>
        <Station pinned={pinned} />
      </HighlightContext></ChatFeedContext></LiveContext></ProfileContext></MetaContext></QueryClientProvider></ToastProvider>;
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
/** The words of each line, without its "Fork from here" button. */
const articles = () => screen.getAllByRole("article").map((line) => [...line.children].filter((child) => child.tagName !== "BUTTON")
  .map((child) => child.textContent).join(""));

test("scout is the partner with no pairing, and Chat is ready to send", () => {
  shell();
  expect(within(screen.getByLabelText("Chat partner")).getByText("scout")).toBeTruthy();
  expect(screen.getByText("Say something to scout.")).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveProperty("disabled", false);
  expect(getChatHead).not.toHaveBeenCalled();
});

test("a button in the header hides Chat, where Pin stands while it is hidden", () => {
  setChatHidden(false);
  shell();
  fireEvent.click(screen.getByRole("button", { name: "Hide Chat" }));
  expect(currentChatColumn().hidden).toBe(true);
  expect(screen.queryByRole("button", { name: "Hide Chat" })).toBeNull();
  expect(screen.getByRole("button", { name: "Pin Chat beside the page" })).toBeTruthy();
  setChatHidden(false);
});

test("a button in the header moves Chat to the other side, and says which way", () => {
  setChatSide("left");
  shell();
  fireEvent.click(screen.getByRole("button", { name: "Move Chat to the right" }));
  expect(currentChatColumn().side).toBe("right");
  fireEvent.click(screen.getByRole("button", { name: "Move Chat to the left" }));
  expect(currentChatColumn().side).toBe("left");
});

test("a Chat open over the page by its strip has a Pin that stands it beside the page, and a column has none", () => {
  setChatHidden(false);
  shell();
  expect(screen.queryByRole("button", { name: "Pin Chat beside the page" })).toBeNull();
  act(() => setChatHidden(true));
  fireEvent.click(screen.getByRole("button", { name: "Pin Chat beside the page" }));
  expect(currentChatColumn().hidden).toBe(false);
  expect(screen.queryByRole("button", { name: "Pin Chat beside the page" })).toBeNull();
});

test("Chat's header names the station, and has neither History nor Clear chat", () => {
  shell();
  expect(screen.getByRole("button", { name: "unnamed-workstream#1" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "History" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Clear chat" })).toBeNull();
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
  act(() => chooseChatStyle("terminal"));
  expect(mine.querySelector(".chat-prompt")).toBeNull();
  act(() => chooseChatStyle("default"));
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
  fireEvent.click(await screen.findByRole("button", { name: "Share" }));
  // A press inside it keeps it.
  fireEvent.pointerDown(screen.getByRole("textbox", { name: "Email to share with" }));
  expect(screen.getByRole("dialog", { name: "Share chat" })).toBeTruthy();
  const remove = screen.getByRole("button", { name: "Delete" });
  fireEvent.pointerDown(remove);
  fireEvent.click(remove);
  expect(screen.queryByRole("dialog", { name: "Share chat" })).toBeNull();
  expect(screen.getByRole("alertdialog", { name: "Delete this chat" })).toBeTruthy();
  fireEvent.pointerDown(document.body);
  expect(screen.queryByRole("alertdialog", { name: "Delete this chat" })).toBeNull();
});

test("a refused share says why in one toast line and keeps the address in the field", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", ME, { may_share: true }));
  vi.mocked(shareChat).mockRejectedValue(new ApiError(403, "Only the starter shares a chat"));
  shell();
  await screen.findByText("a2");
  fireEvent.click(await screen.findByRole("button", { name: "Share" }));
  const field = screen.getByRole("textbox", { name: "Email to share with" });
  fireEvent.change(field, { target: { value: "linus@example.com" } });
  fireEvent.click(screen.getByRole("button", { name: "Add" }));
  expect(await screen.findByText("Only the starter shares a chat")).toBeTruthy();
  expect((field as HTMLInputElement).value).toBe("linus@example.com");
});

test("Delete asks first, Cancel deletes nothing, and Delete chat leaves the station with an empty chat", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", ME, { may_delete: true }));
  vi.mocked(deleteChat).mockResolvedValue();
  const { client } = shell();
  await screen.findByText("a2");
  fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
  expect(screen.getByText("Delete this chat? Its forks keep their copies.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByText(/Its forks keep their copies/)).toBeNull();
  expect(deleteChat).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete chat" }));
  await waitFor(() => expect(screen.queryByText("a2")).toBeNull());
  expect(deleteChat).toHaveBeenCalledWith("c1");
  expect(client.getQueryData<WorkspaceState>(["workspace", WORKSPACE_ID])?.conversation_id).toBeNull();
  expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
});

test("a share refused with chat_not_starter is said in words", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", ME, { may_share: true }));
  vi.mocked(shareChat).mockRejectedValue(new ApiError(403, "403 Forbidden", "chat_not_starter"));
  shell();
  await screen.findByText("a2");
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
  fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
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
  fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete chat" }));
  expect(await screen.findByText("Someone else has posted in this chat")).toBeTruthy();
  expect(screen.getByText("a2")).toBeTruthy();
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

test("a viewer reads a chat but cannot post in it", async () => {
  open("c1");
  hold("c1", said(0, GRACE, "a teammate's line"), answered(1, "an answer"));
  shell(workspace, { profile: { role: "viewer" } });
  expect(await screen.findByText("an answer")).toBeTruthy();
  const box = screen.getByRole("textbox", { name: "Message" });
  expect(box).toHaveProperty("disabled", true);
  expect(box.getAttribute("placeholder")).toContain("read this chat but not post");
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

test("the composer refuses a blank or too long message before sending it", async () => {
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
  render(<QueryClientProvider client={client}><MetaContext value={META}><ProfileContext value={PROFILE}>
    <ChatFeedContext value={feed}>
      <Station />
    </ChatFeedContext></ProfileContext></MetaContext></QueryClientProvider>);
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

const SHARED_LINE = "Ask your admin to set up a shared Chat assistant, or set up a local one:";
const SETUP = `uv tool install trackinizer && trax profile url to ${location.origin} && trax profile token to <TOKEN>`;
const emptyPanel = () => document.querySelector(".chat-panel")!.textContent!;

test.each([
  ["no assistant is set up", { ...workspace, partner: null, assistant: null }],
  ["the assistant has no live session", { ...workspace, partner: { ...SCOUT, status: "unavailable" as const } }],
])("when %s, Chat says to ask an admin or set up a local helper, once, with Settings and both commands", (_, state) => {
  shell(state);
  expect(screen.getAllByText(SHARED_LINE)).toHaveLength(1);
  expect(screen.getByRole("link", { name: "Use a local helper in Settings" }).getAttribute("href")).toBe("#/settings");
  expect(screen.getByText(SETUP)).toBeTruthy();
  expect(screen.getByText("trax helper claude")).toBeTruthy();
  expect(screen.getAllByRole("button", { name: "Copy" })).toHaveLength(2);
  expect(screen.getByRole("link", { name: "Settings → API tokens" }).getAttribute("href")).toBe("#/settings");
  expect(emptyPanel()).not.toMatch(/No partner is available|This partner is unavailable|No assistant is set up/);
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("textbox", { name: "Message" }).getAttribute("placeholder")).toBe("Chat is off");
});

test("when the local helper is chosen but not running, Chat says to start it, with no Settings link", () => {
  shell({ ...workspace, partner: NO_HELPER, partner_choice: "local" });
  expect(screen.getAllByText("Start your local helper:")).toHaveLength(1);
  expect(screen.queryByText(SHARED_LINE)).toBeNull();
  expect(screen.queryByRole("link", { name: "Use a local helper in Settings" })).toBeNull();
  expect(screen.getByText(SETUP)).toBeTruthy();
  expect(screen.getByText("trax helper claude")).toBeTruthy();
  expect(within(screen.getByLabelText("Chat partner")).getByText("your local helper")).toBeTruthy();
});

test("the commands copy with their own origin", async () => {
  const written: string[] = [];
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async (text: string) => void written.push(text) } });
  shell({ ...workspace, partner: null, assistant: null });
  fireEvent.click(screen.getAllByRole("button", { name: "Copy" })[1]!);
  await waitFor(() => expect(written).toEqual(["trax helper claude"]));
  await screen.findByText("Copied");
  Reflect.deleteProperty(navigator, "clipboard");
});

test("a live partner shows none of the setup, and a live local helper is named as such", () => {
  shell();
  expect(emptyPanel()).not.toMatch(/Ask your admin|Start your local helper|trax helper|uv tool install/);
  expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
  cleanup();
  shell({ ...workspace, partner_choice: "local", partner: { kind: "local", session_id: "s", actor: "ada-run", cli: "claude", status: "live" } });
  expect(within(screen.getByLabelText("Chat partner")).getByText("your local helper (ada-run)")).toBeTruthy();
  expect(screen.getByText("Say something to ada-run.")).toBeTruthy();
  expect(emptyPanel()).not.toMatch(/Ask your admin|Start your local helper|trax helper|uv tool install/);
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
  vi.mocked(getDetail).mockResolvedValue({ self: { cli_session_id: "chat:c9", labels: ["science-chat"] } } as never);
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

test("in the Terminal style a user's line, mine or another's, follows a shell prompt, and an answer has none", async () => {
  open("c1");
  hold("c1", said(0, ME, "my question"), answered(1, "an answer"), said(2, GRACE, "and mine"));
  shell();
  await screen.findByText("an answer");
  expect(document.querySelector(".chat-prompt")).toBeNull();
  act(() => chooseChatStyle("terminal"));
  const prompts = Array.from(document.querySelectorAll(".chat-prompt"), (prompt) => prompt.parentElement!.textContent);
  expect(prompts).toEqual(["ada@localhost:[unnamed-workstream#1]$ my question", "grace@localhost:[unnamed-workstream#1]$ and mine"]);
  act(() => chooseChatStyle("default"));
  expect(document.querySelector(".chat-prompt")).toBeNull();
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
