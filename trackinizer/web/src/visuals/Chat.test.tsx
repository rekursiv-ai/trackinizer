import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { type ChatHead, getChatHead, listChats, sendChatLine } from "../api/chats";
import { ApiError } from "../api/client";
import { findRef, getDetail } from "../api/detail";
import type { Profile } from "../api/me";
import { listSessionParts, readSessionRecords, type SessionRecord } from "../api/sessions";
import type { WorkspaceState } from "../api/workspaces";
import { MetaContext, ProfileContext } from "../app/boot";
import { META } from "../detail/testing";
import { LiveContext } from "../live";
import type { LiveHub } from "../live/hub";
import type { LiveQuery } from "../live/serial";
import { startTrail } from "../router/trail";
import { Chat } from "./Chat";
import { ChatFeed, ChatFeedContext } from "./chatFeed";
import { OPENING } from "./chatRecords";
import { canvasActions } from "./testing";
import { WorkspaceActionsProvider } from "./workspaceActions";
import { AGREED } from "../api/testing";

vi.mock("../api/chats", async (original) => ({
  ...(await original<typeof import("../api/chats")>()),
  listChats: vi.fn(),
  getChatHead: vi.fn(),
  sendChatLine: vi.fn(),
}));
vi.mock("../api/sessions", () => ({ listSessionParts: vi.fn(), readSessionRecords: vi.fn() }));
vi.mock("../api/detail", () => ({ getDetail: vi.fn(), findRef: vi.fn() }));

const WORKSPACE_ID = "c5286865-67b6-4bd8-ab51-e06e10c326c5";
const STORED = `trackinizer.v2.chat.${WORKSPACE_ID}`;
const ME = "ada@example.com";
const GRACE = "grace@example.com";
const SCOUT = { kind: "shared", session_id: "kb-session", actor: "scout", cli: "sagent", status: "live" } as const;
const NO_HELPER = { kind: "local", session_id: null, actor: null, cli: null, status: "unavailable" } as const;
const workspace: WorkspaceState = {
  id: WORKSPACE_ID, revision: 3, focused_instance: null, partner: SCOUT, assistant: "scout", partner_choice: "shared",
  visuals: [{ id: "889ffcb2-cf44-43e7-9806-eb08428c6203", type: "trax.chat", version: 1,
    placement: "main", record_id: null, params: {} }],
};
const PROFILE: Profile = { user_id: "u1", email: ME, name: "Ada", role: "writer", last_login: null, visual_workspace_enabled: true, ...AGREED };
const CREATED = "2026-10-03T10:00:00.000000Z";

/** The records of the session each conversation is held in, as the fake server has them. */
const sessions = new Map<string, SessionRecord[]>();
/** The live layer's registrations, which a test drives by hand as the stream does. */
let registered: LiveQuery[] = [];

beforeEach(() => {
  OPENING.everyMs = 5;
  OPENING.giveUpMs = 150;
  vi.mocked(listChats).mockResolvedValue([]);
  vi.mocked(listSessionParts).mockImplementation(async (id) => {
    const held = sessions.get(id) ?? [];
    return [{ part: 0, name: "chat.jsonl", format: "sagent", records: held.length, metadata: {}, ir_id: "i" }];
  });
  vi.mocked(readSessionRecords).mockImplementation(async (id, { afterIdx, limit }) =>
    (sessions.get(id) ?? []).filter((record) => record.idx > afterIdx).slice(0, limit));
  vi.mocked(getChatHead).mockImplementation(async (id) => (sessions.has(`s-${id}`) ? head(id) : null));
});

let stopTrail = () => {};
afterEach(() => {
  stopTrail();
  stopTrail = () => {};
  cleanup();
  vi.restoreAllMocks();
  for (const mock of [getChatHead, listChats, sendChatLine, listSessionParts, readSessionRecords, getDetail, findRef]) vi.mocked(mock).mockReset();
  sessions.clear();
  registered = [];
  localStorage.clear();
  history.replaceState(null, "", "#/");
});

function head(conversation: string, account = ME, extra: Partial<ChatHead> = {}): ChatHead {
  return { conversation_id: conversation, session_id: `s-${conversation}`, title: "t", account, live: true,
    forks: 0, forked_from: null, forks_on_typing: false, ...extra };
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

type Actions = React.ComponentProps<typeof WorkspaceActionsProvider>["value"];

/** One shell's worth of state, kept across `again` renders as the app keeps it across Chat mounting again. */
function shell(
  state: WorkspaceState = workspace,
  options: { actions?: Partial<NonNullable<Actions>>; profile?: Partial<Profile>; queries?: { retry: number | false; retryDelay?: number } } = {},
) {
  const feed = new ChatFeed();
  const client = new QueryClient({ defaultOptions: { queries: options.queries ?? { retry: false } } });
  const value = canvasActions(options.actions);
  const live = { register: (query: LiveQuery) => {
    registered.push(query);
    return { push: () => {}, dispose: () => { registered = registered.filter((each) => each !== query); } };
  } } as unknown as LiveHub;
  const ui = (next: WorkspaceState) => <QueryClientProvider client={client}><MetaContext value={META}>
    <ProfileContext value={{ ...PROFILE, ...options.profile }}><LiveContext value={live}>
      <ChatFeedContext value={feed}><WorkspaceActionsProvider value={value}>
        <Chat instance={next.visuals[0]!} focused={false} workspace={next} onWorkspaceChanged={vi.fn()} />
      </WorkspaceActionsProvider></ChatFeedContext></LiveContext></ProfileContext></MetaContext></QueryClientProvider>;
  const view = render(ui(state));
  return { feed, value, client, view, again: (next: WorkspaceState = state) => view.rerender(ui(next)), mount: () => { view.unmount(); return render(ui(state)); } };
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

const open = (id: string) => localStorage.setItem(STORED, id);
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

test("the composer says chats are public to every user and cannot be deleted, and Chat offers no delete", () => {
  shell();
  expect(screen.getByText(/public to every user and cannot be deleted/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /delete|remove/i })).toBeNull();
});

test("a line shows pending, then the conversation's id at once, then its record once the assistant has opened the session", async () => {
  let finish: (value: { conversation_id: string; session_id: string | null }) => void = () => {};
  vi.mocked(sendChatLine).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const { client } = shell();

  history.replaceState(null, "", "#/graph");
  await type("Show me Issue#12");
  const pendingLine = await within(screen.getByRole("log")).findByText("Show me Issue#12");
  expect(pendingLine.closest("article")!.className).toContain("chat-line-pending");
  expect(screen.getByText("Sending…")).toBeTruthy();
  const [line, key] = vi.mocked(sendChatLine).mock.calls[0]!;
  expect([line, typeof key]).toEqual([{ workspaceId: WORKSPACE_ID, text: "Show me Issue#12", chatInstanceId: workspace.visuals[0]!.id,
    expectedRecordId: null, conversationId: null, page: "#/graph", trail: [] }, "string"]);

  // The receipt names the conversation; its session is not there yet.
  await act(async () => finish({ conversation_id: "c1", session_id: null }));
  await waitFor(() => expect(screen.getByText("Waiting for the assistant…")).toBeTruthy(), { interval: 1 });
  expect(localStorage.getItem(STORED)).toBe("c1");
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
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: "s-c1" });
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

test("a line the assistant never opens a session for is said plainly after a while, and can be sent again", async () => {
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c9", session_id: null });
  shell();
  await type("anyone there?");
  await screen.findByText("Waiting for the assistant…");
  expect((await screen.findByRole("alert", {}, { timeout: 2000 })).textContent).toContain("has not opened this chat");
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
    .mockResolvedValueOnce({ conversation_id: "c1", session_id: null });
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
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: null });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce(), { interval: 1 });
});

test("the box stays focused and editable while a message is sending, and what is typed meanwhile stays", async () => {
  let finish: (value: { conversation_id: string; session_id: string | null }) => void = () => {};
  vi.mocked(sendChatLine).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  shell();
  await type("first");
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  await screen.findByText("Sending…");
  expect(box.disabled).toBe(false);
  fireEvent.change(box, { target: { value: "second, typed meanwhile" } });
  await act(async () => finish({ conversation_id: "c1", session_id: null }));
  expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value).toBe("second, typed meanwhile");
});

test("a receipt that comes back after the user picked another conversation does not reopen the old one", async () => {
  let finish: (value: { conversation_id: string; session_id: string | null }) => void = () => {};
  vi.mocked(sendChatLine).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  shell();
  await type("first");
  await screen.findByText("Sending…");
  fireEvent.click(screen.getByRole("button", { name: "Clear chat" }));
  await act(async () => finish({ conversation_id: "c1", session_id: null }));
  expect(localStorage.getItem(STORED)).toBeNull();
  expect(within(screen.getByRole("log")).queryByText("first")).toBeNull();
});

test("Clear chat starts a new chat and keeps the old one, which History opens again", async () => {
  open("c1");
  hold("c1", said(0, ME, "old question"), answered(1, "old answer"));
  vi.mocked(listChats).mockResolvedValue([
    { conversation_id: "c1", session_id: "s-c1", title: "old question", account: ME, modified: CREATED },
  ]);
  shell();
  expect(await screen.findByText("old answer")).toBeTruthy();
  // One control: there is no separate New chat, and nothing is deleted.
  expect(screen.queryByRole("button", { name: "New chat" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Clear" })).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Clear chat" }));
  expect(screen.queryByText("old answer")).toBeNull();
  expect(localStorage.getItem(STORED)).toBeNull();
  expect(screen.getByRole("button", { name: "Clear chat" })).toHaveProperty("disabled", true);

  fireEvent.click(screen.getByRole("button", { name: "History" }));
  fireEvent.click(await screen.findByRole("menuitemradio", { name: /old question/ }));
  expect(await screen.findByText("old answer")).toBeTruthy();
  expect(localStorage.getItem(STORED)).toBe("c1");
});

test("History lists the chats the user started or posted in with their age and starter, and picking one opens it and closes the menu", async () => {
  vi.mocked(listChats).mockResolvedValue([
    { conversation_id: "c9", session_id: "s-c9", title: "What changed last week", account: GRACE,
      modified: new Date(Date.now() - 3 * 3_600_000).toISOString() },
  ]);
  hold("c9", said(0, GRACE, "What changed last week"), answered(1, "Last week we merged it"));
  vi.mocked(getChatHead).mockResolvedValue(head("c9", GRACE));
  shell();
  expect(screen.queryByRole("menu", { name: "History" })).toBeNull();
  const button = screen.getByRole("button", { name: "History" });
  expect(button.getAttribute("aria-haspopup")).toBe("menu");
  fireEvent.click(button);
  const menu = screen.getByRole("menu", { name: "History" });
  const entry = await within(menu).findByRole("menuitemradio", { name: /What changed last week/ });
  expect(entry.textContent).toContain("3h ago");
  expect(entry.textContent).toContain(GRACE);
  expect(entry.getAttribute("aria-checked")).toBe("false");
  fireEvent.click(entry);
  expect(await screen.findByText("Last week we merged it")).toBeTruthy();
  expect(getChatHead).toHaveBeenCalledWith("c9", expect.any(Object));
  expect(screen.queryByRole("menu", { name: "History" })).toBeNull();
  expect(localStorage.getItem(STORED)).toBe("c9");
  fireEvent.click(button);
  expect((await screen.findByRole("menuitemradio", { name: /What changed last week/ })).getAttribute("aria-checked")).toBe("true");
});

test("an empty History says so", async () => {
  shell();
  fireEvent.click(screen.getByRole("button", { name: "History" }));
  expect(await screen.findByText("No conversations yet.")).toBeTruthy();
});

test("Escape closes an open menu", () => {
  shell();
  fireEvent.click(screen.getByRole("button", { name: "History" }));
  expect(screen.getByRole("menu", { name: "History" })).toBeTruthy();
  fireEvent.keyDown(screen.getByRole("menu", { name: "History" }), { key: "Escape" });
  expect(screen.queryByRole("menu", { name: "History" })).toBeNull();
});

test("Chat mounting again keeps the open conversation, with storage refused too", async () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: "s-c1" });
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
  const { feed } = shell();
  expect(screen.getByText("Say something to scout.")).toBeTruthy();
  act(() => feed.continueIn("sess-1"));
  expect(await screen.findByText("an answer there")).toBeTruthy();
  expect(localStorage.getItem(STORED)).toBe("c7");
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
  expect(localStorage.getItem(STORED)).toBeNull();
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
  const feed = new ChatFeed();
  feed.continueIn("sess-1");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MetaContext value={META}><ProfileContext value={PROFILE}>
    <ChatFeedContext value={feed}><WorkspaceActionsProvider value={canvasActions()}>
      <Chat instance={workspace.visuals[0]!} focused={false} workspace={workspace} onWorkspaceChanged={vi.fn()} />
    </WorkspaceActionsProvider></ChatFeedContext></ProfileContext></MetaContext></QueryClientProvider>);
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
  const { value } = shell();
  const issue = await screen.findByRole("link", { name: /Issue#12/ });
  expect(issue.getAttribute("href")).toBe("#/ref/Issue/12");
  const lookup = screen.getByRole("link", { name: /61d3a095/ });
  expect(lookup.getAttribute("href")).toBe("#/lookup/61d3a095-c7f1-4d27-a4c4-a5b1c218a31e");
  expect(fireEvent.click(lookup)).toBe(true);
  expect(value.operate).not.toHaveBeenCalled();
  expect(sendChatLine).not.toHaveBeenCalled();
});

test("a Chat about a record links to it and has a control that clears the context and leaves Chat where it is", async () => {
  vi.mocked(getDetail).mockResolvedValue({
    self: { id: "record", kind: "Issue", seq: 42, title: "A useful issue" }, edges: {}, backlinks: {}, changes: [],
  } as never);
  const about = { ...workspace, visuals: [{ ...workspace.visuals[0]!, placement: "floating" as const, record_id: "record" }] };
  const { value } = shell(about);
  const context = await screen.findByRole("link", { name: "Issue#42 A useful issue" });
  expect(context.getAttribute("href")).toBe("#/lookup/record");
  fireEvent.click(screen.getByRole("button", { name: "Clear context" }));
  expect(value.operate).toHaveBeenCalledWith({ kind: "show", visual_type: "trax.chat", record_id: null });
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
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: null });
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

test("with no Chat context pinned, Chat names the record on screen and follows the page", async () => {
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

test("the record on screen never replaces the record Chat is pinned to", async () => {
  knowRecords();
  vi.mocked(getDetail).mockImplementation(async (id: string) => ({
    self: id === "record" ? { id, kind: "Issue", seq: 42, title: "A useful issue" } : { id, kind: "Experiment", seq: 407, title: `Run of ${id}` },
    edges: {}, backlinks: {}, changes: [],
  }) as never);
  history.replaceState(null, "", `#/lookup/${LOOKED_UP}`);
  const about = { ...workspace, visuals: [{ ...workspace.visuals[0]!, placement: "floating" as const, record_id: "record" }] };
  shell(about);
  expect(await screen.findByRole("link", { name: "Issue#42 A useful issue" })).toBeTruthy();
  expect(screen.queryByLabelText("Screen context")).toBeNull();
  go("#/ref/Experiment/407");
  expect(screen.queryByLabelText("Screen context")).toBeNull();
  expect(screen.getByLabelText("Record context")).toBeTruthy();
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

test("Chat shows no write error of its own: the canvas shows it once", () => {
  shell(workspace, { actions: { writeError: "Could not update the canvas." } });
  expect(screen.queryByText("Could not update the canvas.")).toBeNull();
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
  vi.mocked(listChats).mockRejectedValueOnce(new ApiError(503, "unavailable")).mockResolvedValueOnce([
    { conversation_id: "c1", session_id: "s-c1", title: "Retried history", account: ME, modified: CREATED }]);
  shell(workspace, { queries: { retry: 1, retryDelay: 0 } });
  expect(await screen.findByText("second try")).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "History" }));
  expect(await screen.findByRole("menuitemradio", { name: /Retried history/ })).toBeTruthy();
});

const TALK = [said(0, ME, "q1"), answered(1, "a1"), said(2, GRACE, "q2"), answered(3, "a2")];
const forkButton = (line: string) => within(screen.getByText(line).closest("article")!).getByRole("button", { name: "Fork from here" });

test("typing in a chat of my own organisation joins it, and sends no fork", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: "s-c1" });
  shell();
  await screen.findByText("a2");
  expect(screen.queryByText(/outside your organisation/)).toBeNull();
  await type("and so?");
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce(), { interval: 1 });
  expect(vi.mocked(sendChatLine).mock.calls[0]![0]).toMatchObject({ conversationId: "c1" });
  expect(vi.mocked(sendChatLine).mock.calls[0]![0]).not.toHaveProperty("fork");
});

test("Fork from here on an earlier line makes the next message a fork after it, and the reader lands in the fork", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "f1", session_id: null });
  shell();
  await screen.findByText("a2");
  fireEvent.click(forkButton("a1"));
  expect((await screen.findByText(/Your next message starts a fork of this chat after/)).textContent).toContain("“a1”");
  await type("is a1 wrong?");
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce(), { interval: 1 });
  const [line, key] = vi.mocked(sendChatLine).mock.calls[0]!;
  expect(line).toMatchObject({ conversationId: null, fork: { sessionId: "s-c1", part: 0, idx: 1 } });
  expect(typeof key).toBe("string");
  // The fork is open now, and the banner has done its work.
  await waitFor(() => expect(localStorage.getItem(STORED)).toBe("f1"), { interval: 1 });
  expect(screen.queryByText(/Your next message starts a fork/)).toBeNull();
  // The line shows in the fork while the assistant opens it, and not in the original.
  expect(await within(screen.getByRole("log")).findByText("is a1 wrong?")).toBeTruthy();
  expect(screen.queryByText("a2")).toBeNull();
});

test("my line in a fork shows until the fork holds one more of mine than it opened with, not than the original has", async () => {
  open("c1");
  hold("c1", said(0, ME, "q1"), answered(1, "a1"), said(2, ME, "q2"), answered(3, "a2"));
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "f1", session_id: null });
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
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "c1", session_id: "s-c1" });
  shell();
  await screen.findByText("a2");
  fireEvent.click(forkButton("q2"));
  fireEvent.click(await screen.findByRole("button", { name: "Cancel fork" }));
  expect(screen.queryByText(/Your next message starts a fork/)).toBeNull();
  await type("still here");
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce(), { interval: 1 });
  expect(vi.mocked(sendChatLine).mock.calls[0]![0]).toMatchObject({ conversationId: "c1" });
  expect(vi.mocked(sendChatLine).mock.calls[0]![0]).not.toHaveProperty("fork");
});

test("typing in a chat started outside my organisation forks it at its latest line, and I land in the fork", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", GRACE, { forks_on_typing: true }));
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "f1", session_id: null });
  shell();
  await screen.findByText("a2");
  expect(screen.getByText(/outside your organisation/)).toBeTruthy();
  await type("a question of my own");
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce(), { interval: 1 });
  expect(vi.mocked(sendChatLine).mock.calls[0]![0]).toMatchObject({ conversationId: null, fork: { sessionId: "s-c1", part: 0, idx: 3 } });
  await waitFor(() => expect(localStorage.getItem(STORED)).toBe("f1"), { interval: 1 });
});

test("Fork from here in a chat outside my organisation forks after the line picked, not the latest", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(getChatHead).mockResolvedValue(head("c1", GRACE, { forks_on_typing: true }));
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: "f1", session_id: null });
  shell();
  await screen.findByText("a2");
  fireEvent.click(forkButton("q1"));
  await type("from the first line");
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledOnce(), { interval: 1 });
  expect(vi.mocked(sendChatLine).mock.calls[0]![0]).toMatchObject({ fork: { part: 0, idx: 0 } });
});

test("a conversation cannot be typed into before its head says whose it is", async () => {
  open("c1");
  hold("c1", ...TALK);
  let release: (value: ChatHead) => void = () => {};
  vi.mocked(getChatHead).mockReturnValue(new Promise((resolve) => { release = resolve; }));
  shell();
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  expect(box.disabled).toBe(true);
  await act(async () => release(head("c1", GRACE, { forks_on_typing: true })));
  await screen.findByText("a2");
  await waitFor(() => expect(box.disabled).toBe(false), { interval: 1 });
  expect(sendChatLine).not.toHaveBeenCalled();
});

test("a post the server refuses as outside the chat's organisation is sent again as a fork at its latest line", async () => {
  open("c1");
  hold("c1", ...TALK);
  vi.mocked(sendChatLine).mockRejectedValueOnce(new ApiError(403, "fork it")).mockResolvedValue({ conversation_id: "f1", session_id: null });
  shell();
  await screen.findByText("a2");
  await type("a question of my own");
  await waitFor(() => expect(sendChatLine).toHaveBeenCalledTimes(2), { interval: 1 });
  const [[first, firstKey], [second, secondKey]] = vi.mocked(sendChatLine).mock.calls as [[object, string], [object, string]];
  expect(first).toMatchObject({ conversationId: "c1" });
  expect(first).not.toHaveProperty("fork");
  expect(second).toMatchObject({ conversationId: null, fork: { sessionId: "s-c1", part: 0, idx: 3 } });
  expect(secondKey).not.toBe(firstKey);
  await waitFor(() => expect(localStorage.getItem(STORED)).toBe("f1"), { interval: 1 });
});

test("a refused post in a chat with no stored line to fork after says why and is not sent again", async () => {
  open("c1");
  hold("c1");
  vi.mocked(getChatHead).mockResolvedValue(head("c1", GRACE));
  vi.mocked(sendChatLine).mockRejectedValue(new ApiError(403, "started outside your organisation"));
  shell();
  await waitFor(() => expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).disabled).toBe(false), { interval: 1 });
  await type("hello");
  expect((await screen.findByRole("alert")).textContent).toContain("started outside your organisation");
  expect(sendChatLine).toHaveBeenCalledOnce();
});

test("a chat says how often it was forked, and a fork says what it was forked from and opens it", async () => {
  open("f1");
  hold("f1", said(0, ME, "q1"), answered(1, "a1"), said(2, ME, "mine"));
  hold("c0", answered(0, "the original"));
  vi.mocked(getChatHead).mockImplementation(async (id) => (id === "f1" ? head("f1", ME, { forked_from: "c0", forks: 1 }) : head(id)));
  shell();
  expect(await screen.findByText("Forked once.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "another chat" }));
  expect(await screen.findByText("the original")).toBeTruthy();
  expect(localStorage.getItem(STORED)).toBe("c0");
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
  expect(screen.getAllByRole("button", { name: "Fork from here" })).toHaveLength(4);
  await type("on its way");
  const pending = (await within(screen.getByRole("log")).findByText("on its way")).closest("article")!;
  expect(within(pending).queryByRole("button", { name: "Fork from here" })).toBeNull();
  view.unmount();
  shell(workspace, { profile: { role: "viewer" } });
  await screen.findByText("a2");
  expect(screen.queryByRole("button", { name: "Fork from here" })).toBeNull();
});
