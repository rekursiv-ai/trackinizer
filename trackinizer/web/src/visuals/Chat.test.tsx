import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError } from "../api/client";
import { findRef } from "../api/detail";
import { type ChatMessage, getChat, listChats } from "../api/chats";
import { sendWorkspaceMessage, type WorkspaceState } from "../api/workspaces";
import { MetaContext } from "../app/boot";
import { META } from "../detail/testing";
import { startTrail } from "../router/trail";
import { appendLines } from "./chatCache";
import { ChatFeed, ChatFeedContext } from "./chatFeed";
import { Chat } from "./Chat";
import { canvasActions } from "./testing";
import { WorkspaceActionsProvider } from "./workspaceActions";

vi.mock("../api/chats", () => ({ listChats: vi.fn(), getChat: vi.fn() }));
vi.mock("../api/detail", () => ({
  getDetail: vi.fn(async (id: string) => ({
    self: id === "record" ? { id, kind: "Issue", seq: 42, title: "A useful issue" } : { id, kind: "Experiment", seq: 407, title: `Run of ${id}` },
    edges: {}, backlinks: {}, changes: [],
  })),
  findRef: vi.fn(async () => "found"),
}));
vi.mock("../api/workspaces", () => ({ sendWorkspaceMessage: vi.fn() }));

const WORKSPACE_ID = "c5286865-67b6-4bd8-ab51-e06e10c326c5";
const STORED = `trackinizer.v2.chat.${WORKSPACE_ID}`;
const SCOUT = { kind: "shared", session_id: "kb-session", actor: "scout", cli: "sagent", status: "live" } as const;
const NO_HELPER = { kind: "local", session_id: null, actor: null, cli: null, status: "unavailable" } as const;
const workspace: WorkspaceState = {
  id: WORKSPACE_ID, revision: 3, focused_instance: null, partner: SCOUT, assistant: "scout", partner_choice: "shared",
  visuals: [{ id: "889ffcb2-cf44-43e7-9806-eb08428c6203", type: "trax.chat", version: 1,
    placement: "main", record_id: null, params: {} }],
};

let stopTrail = () => {};
afterEach(() => {
  stopTrail();
  stopTrail = () => {};
  cleanup();
  vi.restoreAllMocks();
  for (const mock of [getChat, listChats, sendWorkspaceMessage]) vi.mocked(mock).mockReset();
  vi.mocked(listChats).mockResolvedValue([]);
  localStorage.clear();
  history.replaceState(null, "", "#/");
});

const CREATED = "2026-10-03T10:00:00.000000Z";
function message(seq: number, role: "user" | "assistant", text: string): ChatMessage {
  return { id: `message-${seq}`, seq, role, author: role === "user" ? "ada@example.com" : "scout", text, created: CREATED };
}
function thread(id: string, messages: ChatMessage[], earlier = false) {
  return { id, title: "t", partner_actor: "scout", partner_session_id: "kb-session", earlier, messages };
}
function receipt(conversation: string, stored: ChatMessage) {
  return { session_id: "kb-session", conversation_id: conversation, message: stored };
}

type Actions = React.ComponentProps<typeof WorkspaceActionsProvider>["value"];

/** One shell's worth of state, kept across `again` renders as the app keeps it across Chat mounting again. */
function shell(
  state: WorkspaceState = workspace,
  actions: Partial<NonNullable<Actions>> = {},
  queries: { retry: number | false; retryDelay?: number } = { retry: false },
) {
  const feed = new ChatFeed();
  const client = new QueryClient({ defaultOptions: { queries } });
  const value = canvasActions(actions);
  const ui = (next: WorkspaceState) => <QueryClientProvider client={client}><MetaContext value={META}>
    <ChatFeedContext value={feed}><WorkspaceActionsProvider value={value}>
      <Chat instance={next.visuals[0]!} focused={false} workspace={next} onWorkspaceChanged={vi.fn()} />
    </WorkspaceActionsProvider></ChatFeedContext></MetaContext></QueryClientProvider>;
  const view = render(ui(state));
  return { feed, value, client, view, again: (next: WorkspaceState = state) => view.rerender(ui(next)), mount: () => { view.unmount(); return render(ui(state)); } };
}

async function type(text: string) {
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
}

const open = (id: string) => localStorage.setItem(STORED, id);

test("scout is the partner with no pairing, and Chat is ready to send", () => {
  shell();
  expect(within(screen.getByLabelText("Chat partner")).getByText("scout")).toBeTruthy();
  expect(screen.getByText("Say something to scout.")).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveProperty("disabled", false);
  expect(getChat).not.toHaveBeenCalled();
});

test("a message shows pending, then stored, then delivered by its seq, with the status and answer", async () => {
  let finish: (value: ReturnType<typeof receipt>) => void = () => {};
  vi.mocked(sendWorkspaceMessage).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  vi.mocked(getChat).mockResolvedValue(thread("c1", [message(1, "user", "Show me Issue#12")]));
  const { feed, client } = shell();

  history.replaceState(null, "", "#/graph");
  await type("Show me Issue#12");
  expect(await screen.findByText("Show me Issue#12")).toBeTruthy();
  expect(screen.getByText("Sending…")).toBeTruthy();
  const [id, sent, key] = vi.mocked(sendWorkspaceMessage).mock.calls[0]!;
  expect([id, sent, typeof key]).toEqual([WORKSPACE_ID, { text: "Show me Issue#12", chatInstanceId: workspace.visuals[0]!.id,
    expectedRecordId: null, conversationId: null, page: "#/graph", trail: [] }, "string"]);

  await act(async () => finish(receipt("c1", message(1, "user", "Show me Issue#12"))));
  await waitFor(() => expect(screen.queryByText("Sending…")).toBeNull(), { interval: 1 });
  expect(screen.getAllByText("Show me Issue#12")).toHaveLength(1);
  expect(screen.queryByText("Delivered")).toBeNull();
  expect(localStorage.getItem(STORED)).toBe("c1");

  act(() => feed.drained("c1", 1));
  expect(screen.getByText("Delivered")).toBeTruthy();
  act(() => feed.setStatus("c1", "Looking it up"));
  expect(screen.getByText("Looking it up").className).toBe("chat-status");

  act(() => { appendLines(client, "c1", [message(2, "assistant", "It is **open**.")]); feed.messaged("c1"); });
  expect((await screen.findByText("open")).tagName).toBe("STRONG");
  expect(screen.queryByText("Looking it up")).toBeNull();
  expect(screen.queryByText("Delivered")).toBeNull();

  vi.mocked(sendWorkspaceMessage).mockResolvedValue(receipt("c1", message(3, "user", "thanks")));
  await type("thanks");
  await waitFor(() => expect(sendWorkspaceMessage).toHaveBeenCalledTimes(2), { interval: 1 });
  expect(vi.mocked(sendWorkspaceMessage).mock.calls[1]![1].conversationId).toBe("c1");
});

test("a conversation deleted elsewhere is left for a new chat with one plain line", async () => {
  open("c1");
  vi.mocked(getChat).mockResolvedValue(thread("c1", [message(1, "assistant", "soon gone")]));
  const { feed } = shell();
  await screen.findByText("soon gone");
  act(() => feed.forget("c1", true));
  expect((await screen.findByRole("status")).textContent).toBe("This conversation was deleted.");
  expect(screen.queryByText("soon gone")).toBeNull();
  expect(localStorage.getItem(STORED)).toBeNull();
});

test("Working… and a status give way to the partner's unavailability when it goes away", async () => {
  open("c1");
  vi.mocked(getChat).mockResolvedValue(thread("c1", [message(1, "user", "hi")]));
  const { feed, again } = shell();
  await screen.findByText("hi");
  act(() => feed.drained("c1", 1));
  expect(screen.getByText("Working…")).toBeTruthy();
  again({ ...workspace, partner: { ...SCOUT, status: "unavailable" } });
  expect(screen.queryByText("Working…")).toBeNull();
  expect(screen.getByText("scout is unavailable.")).toBeTruthy();
  act(() => feed.setStatus("c1", "Searching"));
  expect(screen.queryByText("Searching")).toBeNull();
  again();
  expect(screen.getByText("Searching")).toBeTruthy();
});

test("a send shows one receipt, the transcript's, and the composer none", async () => {
  vi.mocked(sendWorkspaceMessage).mockResolvedValue(receipt("c1", message(1, "user", "hi")));
  vi.mocked(getChat).mockResolvedValue(thread("c1", [message(1, "user", "hi")]));
  const { feed } = shell();
  await type("hi");
  await screen.findByText("hi");
  act(() => feed.drained("c1", 1));
  expect(screen.getByText("Delivered")).toBeTruthy();
  expect(screen.queryByText("Sent")).toBeNull();
  expect(document.querySelector(".composer-receipt")).toBeNull();
});

test("the first stream open while the thread is still being read adds no second read", async () => {
  open("c1");
  let read: (value: ReturnType<typeof thread>) => void = () => {};
  vi.mocked(getChat).mockImplementation(() => new Promise((resolve) => { read = resolve; }));
  const { feed } = shell();
  await waitFor(() => expect(getChat).toHaveBeenCalledTimes(1), { interval: 1 });
  act(() => feed.opened());
  await act(async () => read(thread("c1", [message(1, "assistant", "once")])));
  await screen.findByText("once");
  expect(getChat).toHaveBeenCalledTimes(1);
});

test("delivery is the partner's drained seq, not a clock: a message past it is not delivered", async () => {
  open("c1");
  vi.mocked(getChat).mockResolvedValue(thread("c1", [message(1, "user", "one"), message(2, "assistant", "a"), message(3, "user", "two")]));
  const { feed } = shell();
  await screen.findByText("two");
  act(() => feed.drained("c1", 2));
  expect(screen.queryByText("Delivered")).toBeNull();
  expect(screen.queryByText("Working…")).toBeNull();
  act(() => feed.drained("c1", 3));
  expect(screen.getByText("Delivered")).toBeTruthy();
});

test("a drained frame that comes before the receipt still marks the message delivered", async () => {
  vi.mocked(getChat).mockResolvedValue(thread("c1", []));
  const { feed } = shell();
  act(() => feed.drained("c1", 1));
  vi.mocked(sendWorkspaceMessage).mockResolvedValue(receipt("c1", message(1, "user", "hi")));
  await type("hi");
  expect(await screen.findByText("Delivered")).toBeTruthy();
});

test("after delivery every partner shows Working…, its status replaces it, and a clear or the answer ends it", async () => {
  open("c1");
  vi.mocked(getChat).mockResolvedValue(thread("c1", [message(1, "user", "hi")]));
  const { feed, client } = shell();
  await screen.findByText("hi");
  expect(screen.queryByText("Working…")).toBeNull();
  act(() => feed.drained("c1", 1));
  expect(screen.getByText("Working…").getAttribute("role")).toBe("status");
  act(() => feed.setStatus("c1", "Searching"));
  expect(screen.queryByText("Working…")).toBeNull();
  expect(screen.getByText("Searching")).toBeTruthy();
  act(() => feed.setStatus("c1", ""));
  expect(screen.queryByText("Working…")).toBeNull();
  expect(screen.queryByText("Searching")).toBeNull();
  act(() => feed.messaged("c1"));
  expect(screen.getByText("Working…")).toBeTruthy();
  act(() => { appendLines(client, "c1", [message(2, "assistant", "done")]); feed.messaged("c1"); });
  await screen.findByText("done");
  expect(screen.queryByText("Working…")).toBeNull();
});

test("a failed send keeps the draft for a retry under one key, and shows no pending line", async () => {
  vi.mocked(sendWorkspaceMessage).mockRejectedValueOnce(new ApiError(0, "offline", "network"))
    .mockResolvedValueOnce(receipt("c1", message(1, "user", "hi")));
  vi.mocked(getChat).mockResolvedValue(thread("c1", [message(1, "user", "hi")]));
  shell();
  await type("hi");
  fireEvent.click(await screen.findByRole("button", { name: "Retry message" }));
  await waitFor(() => expect(sendWorkspaceMessage).toHaveBeenCalledTimes(2), { interval: 1 });
  expect(vi.mocked(sendWorkspaceMessage).mock.calls[1]![2]).toBe(vi.mocked(sendWorkspaceMessage).mock.calls[0]![2]);
  await waitFor(() => expect(screen.queryByText("Sending…")).toBeNull(), { interval: 1 });
});

test("a refusal shows the server's reason and offers no retry, while a server fault does", async () => {
  vi.mocked(sendWorkspaceMessage).mockRejectedValueOnce(new ApiError(409, "That key was used for another message."));
  shell();
  await type("hi");
  expect((await screen.findByRole("alert")).textContent).toContain("That key was used for another message.");
  expect(screen.queryByRole("button", { name: "Retry message" })).toBeNull();
  cleanup();
  vi.mocked(sendWorkspaceMessage).mockRejectedValueOnce(new ApiError(503, "Service Unavailable"));
  shell();
  await type("hi");
  expect(await screen.findByRole("button", { name: "Retry message" })).toBeTruthy();
});

test("a send to a conversation that is gone starts a new chat and says so", async () => {
  open("c1");
  vi.mocked(getChat).mockResolvedValue(thread("c1", [message(1, "assistant", "old")]));
  vi.mocked(sendWorkspaceMessage).mockRejectedValue(new ApiError(404, "No such conversation"));
  shell();
  await screen.findByText("old");
  await type("hello?");
  expect((await screen.findByRole("status")).textContent).toContain("no longer exists");
  expect(localStorage.getItem(STORED)).toBeNull();
  expect(screen.queryByText("old")).toBeNull();
  expect(screen.queryByText("Sending…")).toBeNull();
  expect(screen.queryByRole("button", { name: "Retry message" })).toBeNull();
});

test("the composer refuses a blank or too long message before sending it", async () => {
  shell();
  const box = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(box, { target: { value: "x".repeat(16_385) } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect((await screen.findByRole("alert")).textContent).toContain("at most 16,384");
  expect(sendWorkspaceMessage).not.toHaveBeenCalled();
  fireEvent.change(box, { target: { value: "x".repeat(16_384) } });
  vi.mocked(sendWorkspaceMessage).mockResolvedValue(receipt("c1", message(1, "user", "x")));
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(sendWorkspaceMessage).toHaveBeenCalledOnce(), { interval: 1 });
});

test("the box stays focused and editable while a message is sending, and what is typed meanwhile stays", async () => {
  let finish: (value: ReturnType<typeof receipt>) => void = () => {};
  vi.mocked(sendWorkspaceMessage).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  vi.mocked(getChat).mockResolvedValue(thread("c1", [message(1, "user", "first")]));
  shell();
  await type("first");
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  await screen.findByText("Sending…");
  expect(box.disabled).toBe(false);
  fireEvent.change(box, { target: { value: "second, typed meanwhile" } });
  await act(async () => finish(receipt("c1", message(1, "user", "first"))));
  expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value).toBe("second, typed meanwhile");
});

test("a receipt that comes back after the user picked another conversation does not reopen the old one", async () => {
  let finish: (value: ReturnType<typeof receipt>) => void = () => {};
  vi.mocked(sendWorkspaceMessage).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  vi.mocked(getChat).mockResolvedValue(thread("c1", [message(1, "user", "first")]));
  const { client } = shell();
  await type("first");
  await screen.findByText("Sending…");
  fireEvent.click(screen.getByRole("button", { name: "Clear chat" }));
  await act(async () => finish(receipt("c1", message(1, "user", "first"))));
  expect(localStorage.getItem(STORED)).toBeNull();
  expect(within(screen.getByRole("log")).queryByText("first")).toBeNull();
  // The line is still stored in its own conversation's entry.
  expect(client.getQueryData(["chat", "c1"])).toMatchObject({ messages: [{ seq: 1 }] });
});

test("Clear chat starts a new chat and keeps the old one, which History opens again", async () => {
  open("c1");
  vi.mocked(getChat).mockImplementation(async (id) => id === "c1"
    ? thread("c1", [message(1, "user", "old question"), message(2, "assistant", "old answer")])
    : thread(id, [message(1, "user", "fresh")]));
  vi.mocked(listChats).mockResolvedValue([
    { id: "c1", title: "old question", partner_actor: "scout", workspace_id: WORKSPACE_ID, created: CREATED, modified: CREATED },
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
  vi.mocked(sendWorkspaceMessage).mockResolvedValue(receipt("c2", message(1, "user", "fresh")));
  await type("fresh");
  await waitFor(() => expect(localStorage.getItem(STORED)).toBe("c2"), { interval: 1 });
  expect(vi.mocked(sendWorkspaceMessage).mock.calls[0]![1].conversationId).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "History" }));
  fireEvent.click(await screen.findByRole("menuitemradio", { name: /old question/ }));
  expect(await screen.findByText("old answer")).toBeTruthy();
  expect(localStorage.getItem(STORED)).toBe("c1");
});


test("History is a menu of conversations with their age and partner, and picking one opens it and closes the menu", async () => {
  vi.mocked(listChats).mockResolvedValue([
    { id: "c9", title: "What changed last week", partner_actor: "scout", workspace_id: WORKSPACE_ID,
      created: CREATED, modified: new Date(Date.now() - 3 * 3_600_000).toISOString() },
  ]);
  vi.mocked(getChat).mockResolvedValue(thread("c9", [message(1, "assistant", "Last week we merged it")]));
  shell();
  expect(screen.queryByRole("menu", { name: "History" })).toBeNull();
  const button = screen.getByRole("button", { name: "History" });
  expect(button.getAttribute("aria-haspopup")).toBe("menu");
  fireEvent.click(button);
  const menu = screen.getByRole("menu", { name: "History" });
  const entry = await within(menu).findByRole("menuitemradio", { name: /What changed last week/ });
  expect(entry.textContent).toContain("3h ago");
  expect(entry.textContent).toContain("scout");
  expect(entry.getAttribute("aria-checked")).toBe("false");
  fireEvent.click(entry);
  expect(await screen.findByText("Last week we merged it")).toBeTruthy();
  expect(getChat).toHaveBeenCalledWith("c9", 0, expect.any(Object));
  expect(screen.queryByRole("menu", { name: "History" })).toBeNull();
  expect(localStorage.getItem(STORED)).toBe("c9");
  fireEvent.click(button);
  expect((await screen.findByRole("menuitemradio", { name: /What changed last week/ })).getAttribute("aria-checked")).toBe("true");
});

test("Escape closes an open menu", () => {
  shell();
  fireEvent.click(screen.getByRole("button", { name: "History" }));
  expect(screen.getByRole("menu", { name: "History" })).toBeTruthy();
  fireEvent.keyDown(screen.getByRole("menu", { name: "History" }), { key: "Escape" });
  expect(screen.queryByRole("menu", { name: "History" })).toBeNull();
});

test("a reload restores the conversation, and one deleted elsewhere becomes a new chat", async () => {
  open("c1");
  vi.mocked(getChat).mockResolvedValue(thread("c1", [message(1, "assistant", "still here")]));
  shell();
  expect(await screen.findByText("still here")).toBeTruthy();
  cleanup();

  vi.mocked(getChat).mockRejectedValue(new ApiError(404, "No such conversation"));
  shell();
  await waitFor(() => expect(localStorage.getItem(STORED)).toBeNull(), { interval: 1 });
  expect(await screen.findByText("Say something to scout.")).toBeTruthy();
});

test("Chat mounting again keeps the open conversation, with storage refused too", async () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
  vi.mocked(sendWorkspaceMessage).mockResolvedValue(receipt("c1", message(1, "user", "hi")));
  vi.mocked(getChat).mockResolvedValue(thread("c1", [message(1, "user", "hi")]));
  const { mount } = shell();
  await type("hi");
  expect(await screen.findByText("hi")).toBeTruthy();
  await waitFor(() => expect(getChat).toHaveBeenCalled(), { interval: 1 });
  mount();
  expect(await screen.findByText("hi")).toBeTruthy();
  expect(screen.queryByText("Say something to scout.")).toBeNull();
});

test("Earlier messages not shown appears when the server holds older ones", async () => {
  open("c1");
  vi.mocked(getChat).mockResolvedValue(thread("c1", [message(501, "assistant", "newest")], true));
  shell();
  await screen.findByText("newest");
  expect(screen.getByText("Earlier messages not shown.")).toBeTruthy();
});

test("a line pushed before the thread read lands is kept and merged with the read", async () => {
  open("c1");
  let read: (value: ReturnType<typeof thread>) => void = () => {};
  vi.mocked(getChat).mockImplementation(() => new Promise((resolve) => { read = resolve; }));
  const { client } = shell();
  act(() => appendLines(client, "c1", [message(3, "assistant", "pushed early")]));
  expect(await screen.findByText("pushed early")).toBeTruthy();
  await act(async () => read(thread("c1", [message(1, "user", "one"), message(2, "assistant", "two")])));
  await waitFor(() => expect(screen.getAllByRole("article").map((line) => line.textContent)).toEqual(["one", "two", "pushed early"]), { interval: 1 });
});

test("a trax helper and scout give the same panel, differing in the partner's name", async () => {
  const lines = [message(1, "user", "hello"), message(2, "assistant", "hi **there**")];
  open("c1");
  vi.mocked(getChat).mockResolvedValue(thread("c1", lines));
  shell();
  await screen.findByText("there");
  const panel = () => [".chat-head-actions", ".chat-lines", ".composer"]
    .map((selector) => document.querySelector(`.chat-panel ${selector}`)!.outerHTML.replaceAll(/scout|ada-run/g, "NAME").replaceAll(/_r_\w+_/g, "ID"));
  const scout = panel();
  cleanup();
  shell({ ...workspace, partner: { kind: "shared", session_id: "s", actor: "ada-run", cli: "trax-helper", status: "live" } });
  await screen.findByText("there");
  expect(panel()).toEqual(scout);
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
  vi.mocked(getChat).mockResolvedValue(thread("c1", [message(1, "assistant", "See Issue#12 and 61d3a095-c7f1-4d27-a4c4-a5b1c218a31e.")]));
  const { value } = shell();
  const issue = await screen.findByRole("link", { name: /Issue#12/ });
  expect(issue.getAttribute("href")).toBe("#/ref/Issue/12");
  const lookup = screen.getByRole("link", { name: /61d3a095/ });
  expect(lookup.getAttribute("href")).toBe("#/lookup/61d3a095-c7f1-4d27-a4c4-a5b1c218a31e");
  expect(fireEvent.click(lookup)).toBe(true);
  expect(value.operate).not.toHaveBeenCalled();
  expect(sendWorkspaceMessage).not.toHaveBeenCalled();
});

test("a Chat about a record links to it and has a control that clears the context and leaves Chat where it is", async () => {
  const about = { ...workspace, visuals: [{ ...workspace.visuals[0]!, placement: "floating" as const, record_id: "record" }] };
  const { value } = shell(about);
  const context = await screen.findByRole("link", { name: "Issue#42 A useful issue" });
  expect(context.getAttribute("href")).toBe("#/lookup/record");
  fireEvent.click(screen.getByRole("button", { name: "Clear context" }));
  expect(value.operate).toHaveBeenCalledWith({ kind: "show", visual_type: "trax.chat", record_id: null });
  expect(sendWorkspaceMessage).not.toHaveBeenCalled();
});

const LOOKED_UP = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";

/** Move the page as the router's links do: the hash changes and the window says so. */
function go(hash: string) {
  act(() => {
    history.replaceState(null, "", hash);
    dispatchEvent(new HashChangeEvent("hashchange"));
  });
}

async function sentFrom(hashes: readonly string[]) {
  history.replaceState(null, "", hashes[0]);
  stopTrail = startTrail();
  shell();
  for (const hash of hashes.slice(1)) go(hash);
  vi.mocked(sendWorkspaceMessage).mockResolvedValue(receipt("c1", message(1, "user", "where am I")));
  await type("where am I");
  await waitFor(() => expect(sendWorkspaceMessage).toHaveBeenCalledTimes(1), { interval: 1 });
  const { page, trail } = vi.mocked(sendWorkspaceMessage).mock.calls[0]![1];
  return { page, trail };
}

test("a message carries the page it is sent from and the pages the user came through, oldest first", async () => {
  expect(await sentFrom(["#/list/Issue", "#/activity", "#/console"]))
    .toEqual({ page: "#/console", trail: ["#/list/Issue", "#/activity"] });
});

test("the trail a message carries holds at most 8 pages and never the current one", async () => {
  const hashes = Array.from({ length: 12 }, (_, index) => `#/ref/Issue/${index + 1}`);
  const { page, trail } = await sentFrom(hashes);
  expect(page).toBe("#/ref/Issue/12");
  expect(trail).toEqual(hashes.slice(3, 11));
});

test("a page the server would refuse is sent as null, with the pages before it as the trail", async () => {
  expect(await sentFrom(["#/graph", "#/settings", "#top"])).toEqual({ page: null, trail: ["#/graph", "#/settings"] });
});

test("with no Chat context pinned, Chat names the record on screen and follows the page", async () => {
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
  history.replaceState(null, "", `#/lookup/${LOOKED_UP}`);
  const about = { ...workspace, visuals: [{ ...workspace.visuals[0]!, placement: "floating" as const, record_id: "record" }] };
  shell(about);
  expect(await screen.findByRole("link", { name: "Issue#42 A useful issue" })).toBeTruthy();
  expect(screen.queryByLabelText("Screen context")).toBeNull();
  go("#/ref/Experiment/407");
  expect(screen.queryByLabelText("Screen context")).toBeNull();
  expect(screen.getByLabelText("Record context")).toBeTruthy();
});

test("after the stream opens again Chat reads what was stored while it was down", async () => {
  open("c1");
  vi.mocked(getChat).mockResolvedValueOnce(thread("c1", [message(1, "user", "first")]));
  const { feed } = shell();
  await screen.findByText("first");
  vi.mocked(getChat).mockResolvedValueOnce(thread("c1", [message(2, "assistant", "missed while down")]));
  act(() => feed.opened());
  expect(await screen.findByText("missed while down")).toBeTruthy();
  expect(getChat).toHaveBeenLastCalledWith("c1", 1, expect.any(Object));
});

test("the transcript follows the newest row, the delivery and working rows included, unless the reader scrolled up", async () => {
  open("c1");
  vi.mocked(getChat).mockResolvedValue(thread("c1", [message(1, "user", "one")]));
  const { feed, client } = shell();
  await screen.findByText("one");
  const log = screen.getByRole("log", { name: "Messages" });
  let height = 1000;
  Object.defineProperty(log, "scrollHeight", { configurable: true, get: () => height });
  Object.defineProperty(log, "clientHeight", { configurable: true, value: 300 });
  act(() => feed.drained("c1", 1));
  expect(log.scrollTop).toBe(1000);
  height = 1100;
  act(() => feed.setStatus("c1", "Searching"));
  expect(log.scrollTop).toBe(1100);
  height = 1200;
  act(() => { appendLines(client, "c1", [message(2, "assistant", "two")]); feed.messaged("c1"); });
  await screen.findByText("two");
  expect(log.scrollTop).toBe(1200);

  log.scrollTop = 100;
  fireEvent.scroll(log);
  height = 1500;
  act(() => feed.setStatus("c1", "Again"));
  expect(log.scrollTop).toBe(100);
});

test("Chat shows no write error of its own: the canvas shows it once", () => {
  shell(workspace, { writeError: "Could not update the canvas." });
  expect(screen.queryByText("Could not update the canvas.")).toBeNull();
});

test("a read that failed offers Retry", async () => {
  open("c1");
  vi.mocked(getChat).mockRejectedValueOnce(new ApiError(500, "boom")).mockResolvedValueOnce(thread("c1", [message(1, "assistant", "back")]));
  shell();
  fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
  expect(await screen.findByText("back")).toBeTruthy();
});

test("Chat's reads use the app's read retry: a read that fails once is read again without a click", async () => {
  open("c1");
  vi.mocked(getChat).mockRejectedValueOnce(new ApiError(503, "unavailable")).mockResolvedValueOnce(thread("c1", [message(1, "assistant", "second try")]));
  vi.mocked(listChats).mockRejectedValueOnce(new ApiError(503, "unavailable")).mockResolvedValueOnce([
    { id: "c1", title: "Retried history", partner_actor: null, workspace_id: WORKSPACE_ID, created: CREATED, modified: CREATED }]);
  shell(workspace, {}, { retry: 1, retryDelay: 0 });
  expect(await screen.findByText("second try")).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "History" }));
  expect(await screen.findByRole("menuitemradio", { name: /Retried history/ })).toBeTruthy();
});
