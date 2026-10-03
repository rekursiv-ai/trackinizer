import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import type { FeedEvent, FeedFacets } from "../api/sessions";
import { type Sent, stubFetch } from "../api/testing";
import { MetaContext, ProfileContext } from "../app/boot";
import { CommandRegistry, CommandRegistryContext, Shortcuts } from "../commands/registry";
import { META, PROFILE } from "../detail/testing";
import { clock, ConsoleView } from ".";
import { type Level, levelOf } from "./levels";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

const TILES_A = "00000000-0000-4000-8000-0000000000c1";
const CLAUDE = "00000000-0000-4000-8000-0000000000c2";
const TILES_B = "00000000-0000-4000-8000-0000000000c3";
const TILES_C = "00000000-0000-4000-8000-0000000000c4";

/** Each session's agent, rooms and CLI: tiles-a is in two rooms, claude in none. */
const SESSIONS: { [id: string]: { actor: string; rooms: string[]; cli: string } } = {
  [TILES_A]: { actor: "tiles-a", rooms: ["ops", "lab"], cli: "codex" },
  [CLAUDE]: { actor: "claude", rooms: [], cli: "claude" },
  [TILES_B]: { actor: "tiles-b", rooms: ["ops"], cli: "codex" },
  [TILES_C]: { actor: "tiles-c", rooms: ["ops"], cli: "codex" },
};

/** Record `seq` of a session, as the feed sends one. */
function said(seq: number, session: string, kind: string, message: { [field: string]: unknown }, text = ""): FeedEvent {
  return {
    session_id: session,
    ...SESSIONS[session]!,
    part: 0,
    seq,
    kind,
    created: `2026-10-01T10:00:0${seq}Z`,
    model: "m",
    message: { "py/object": `trackinizer.lib.agent.types.sessions.${kind}`, ...message },
    text,
  };
}

const EVENTS = [
  said(0, TILES_A, "UserMessage", { content: "Fix the flake." }, "Fix the flake."),
  said(1, CLAUDE, "ToolCall", { call_id: "c1", name: "Bash", arguments: { command: "pytest -x" } }),
  said(2, CLAUDE, "TokenUsage", { info: { input_tokens: 3 } }),
  said(3, TILES_A, "UncategorizedRecord", { kind: "event_msg/task_started", payload: { type: "task_started" } }),
  said(4, TILES_A, "AssistantMessage", { content: "**Done.**" }, "**Done.**"),
  said(5, TILES_B, "AssistantMessage", { content: "On it." }, "On it."),
  said(6, CLAUDE, "ShellCommandResult", { command: "pytest -x", stdout: "1 passed", stderr: "", exit_code: 0 }),
];

/** Session `session`'s agent as the facets count it, last heard from `minutes` before `now`. */
function facet(session: string, count: number, conversation: number, minutes: number, now: number, ended: string | null = null) {
  const last = new Date(now - minutes * 60_000).toISOString();
  return { ...SESSIONS[session]!, session_id: session, count, conversation, last, ended };
}

/**
 * The facets of a read's window, as of `now`: the agents heard from since its
 * `since`, or all of them with none. tiles-b has ended; tiles-c was last heard
 * from two hours ago.
 */
function facets(query: URLSearchParams, now: number): FeedFacets {
  const since = query.has("since") ? Date.parse(query.get("since")!) : -Infinity;
  const actors = [facet(TILES_A, 40, 3, 1, now), facet(CLAUDE, 20, 1, 2, now), facet(TILES_B, 10, 1, 30, now, new Date(now).toISOString()), facet(TILES_C, 5, 1, 120, now)];
  return {
    actors: actors.filter(({ last }) => Date.parse(last) >= since),
    rooms: [
      { room: "ops", count: 50, actors: ["tiles-a", "tiles-b"] },
      { room: "lab", count: 40, actors: ["tiles-a"] },
    ],
    kinds: [
      { kind: "UserMessage", count: 2 },
      { kind: "AssistantMessage", count: 4 },
      { kind: "ToolCall", count: 10 },
      { kind: "ShellCommandResult", count: 9 },
      { kind: "TokenUsage", count: 30 },
      { kind: "UncategorizedRecord", count: 15 },
    ],
  };
}

/** The facets under a read's picks: its agents, and their records by kind (tiles-a's alone, here). */
function scoped(query: URLSearchParams, now: number): FeedFacets {
  const pass = ({ actor, rooms, cli }: { actor: string; rooms: readonly string[]; cli: string | null }) =>
    (!query.has("actor") || query.getAll("actor").includes(actor)) &&
    (!query.has("room") || rooms.some((room) => query.getAll("room").includes(room))) &&
    (!query.has("cli") || query.getAll("cli").includes(cli ?? ""));
  return {
    ...facets(query, now),
    actors: facets(query, now).actors.filter(pass),
    kinds: [
      { kind: "UserMessage", count: 1 },
      { kind: "AssistantMessage", count: 2 },
      { kind: "ToolCall", count: 4 },
      { kind: "TokenUsage", count: 10 },
      { kind: "UncategorizedRecord", count: 3 },
    ],
  };
}

/** Whether `event` passes a feed read's filters: OR within each, AND across; `conversation`, what Messages shows. */
function passes(event: FeedEvent, query: URLSearchParams): boolean {
  const any = (name: string, values: readonly string[]) => !query.has(name) || values.some((value) => query.getAll(name).includes(value));
  const said = !query.has("conversation") || levelOf(event) === 1;
  return any("actor", [event.actor]) && any("room", event.rooms ?? []) && any("cli", [event.cli ?? ""]) && any("kind", [event.kind]) && said;
}

/**
 * Serve the feed of `events` (its first page, filtered as the server filters;
 * later pages are empty), the facets as of now, and each routed message with
 * `message`.
 */
function serve(
  message: (body: { actor: string; room: string | null }) => Response = () => Response.json({ delivered: ["s1"] }),
  events: readonly FeedEvent[] = EVENTS,
): Sent[] {
  const now = Date.now();
  return stubFetch(async (request) => {
    const url = new URL(request.url);
    if (url.pathname === "/api/web/feed") {
      const last = events.at(-1)!;
      return Response.json({
        events: url.searchParams.has("after_seq") ? [] : events.filter((event) => passes(event, url.searchParams)),
        next_after: { created: last.created, session_id: last.session_id, part: last.part, seq: last.seq },
      });
    }
    if (url.pathname === "/api/web/feed/facets") {
      return Response.json(["actor", "room", "cli"].some((name) => url.searchParams.has(name)) ? scoped(url.searchParams, now) : facets(url.searchParams, now));
    }
    if (url.pathname === "/api/web/feed/histogram") {
      return Response.json({ start: "2026-10-01T10:00:00Z", end: "2026-10-01T11:00:00Z", bucket_seconds: 3600, counts: [] });
    }
    if (url.pathname === "/api/messages") return message(await request.json());
    return Response.json({ detail: "not found" }, { status: 404 });
  });
}

function renderConsole(profile = PROFILE) {
  // jsdom lays nothing out: the minimap's band has no width, so it reads no bars.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MetaContext value={META}>
        <ProfileContext value={profile}>
          <CommandRegistryContext value={new CommandRegistry()}>
            <Shortcuts />
            <ConsoleView />
          </CommandRegistryContext>
        </ProfileContext>
      </MetaContext>
    </QueryClientProvider>,
  );
}

/** The console's lines, once `count` show. */
async function lines(count: number): Promise<HTMLElement[]> {
  return waitFor(() => {
    const shown = [...document.querySelectorAll<HTMLElement>(".console-line")];
    expect(shown).toHaveLength(count);
    return shown;
  });
}

/** Each feed read's query, a repeated parameter's values joined by commas. */
const feedReads = (sent: readonly Sent[]) =>
  sent
    .filter((request) => request.path === "/api/web/feed")
    .map((request) => {
      const query = new URLSearchParams(request.query);
      return Object.fromEntries([...new Set(query.keys())].map((name) => [name, query.getAll(name).join(",")]));
    });

const MESSAGES = "AgentToAgentMessage,AssistantMessage,ContextState,UserMessage";

/** Messages' read: only the conversation, no kind. */
const CONVERSATION = { conversation: "true" };

// The rail's sections and the level buttons are found by their labels. After each
// change jsdom works out every element's style again, and a query by role reads
// it for each candidate: 4-8 ms a call here, against under one by label.

/** One section of the rail. */
const rail = (name: string) => within(screen.getByLabelText(name, { selector: "section" }));

const levels = () => within(screen.getByLabelText("Level", { selector: "[role=group]" }));

/** The checkboxes of the agent facet, by name. */
const agentBoxes = () => rail("Agents").queryAllByRole("checkbox").map((box) => box.getAttribute("aria-label"));

test("the console opens on every agent's messages, live, drawn as the transcript draws them, each level with its count", async () => {
  const sent = serve();
  renderConsole();
  const [user, done, onIt] = await lines(3);
  expect(feedReads(sent)).toEqual([{ tail: "true", limit: "300", ...CONVERSATION }]);
  // Two lines: when and whose; then where and what.
  expect([...user!.querySelectorAll(".turn-h")].map((row) => row.textContent)).toEqual([`${clock(EVENTS[0]!.created)}tiles-a`, "[ops, lab]User"]);
  expect(clock(EVENTS[0]!.created)).toMatch(/^\d\d:\d\d:\d\d$/);
  expect(done!.querySelector("strong")!.textContent).toBe("Done.");
  expect(onIt!.textContent).toContain("On it.");
  await waitFor(() =>
    expect(levels().getAllByRole("button").map((button) => [button.textContent, button.getAttribute("aria-pressed")])).toEqual([
      ["Messages5", "true"],
      ["+ Calls15", "false"],
      ["+ Output24", "false"],
      ["All70", "false"],
    ]),
  );
});

test("the rail's button in the header, or [, collapses the rail and expands it again", async () => {
  serve();
  renderConsole();
  const user = userEvent.setup();
  const toggle = () => screen.getByRole("button", { name: /views and filters$/ });
  expect(toggle().closest(".view-h")).not.toBeNull();
  expect(document.getElementById(toggle().getAttribute("aria-controls")!)!.contains(rail("Views").getByRole("heading").closest("section"))).toBe(true);
  await user.click(toggle());
  expect(toggle().getAttribute("aria-label")).toBe("Expand views and filters");
  expect(toggle().getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByRole("region", { name: "Views" })).toBeNull();
  // user-event spells the [ key "[[".
  await user.keyboard("[[");
  expect(screen.getByRole("region", { name: "Views" })).toBeTruthy();
});

test("a line shows when its record happened, its own timestamp, not when the server stored it; one with none, when it was stored", async () => {
  // Stored in one burst, as capture stores records, but said minutes apart, the second before the first.
  serve(undefined, [
    { ...EVENTS[0]!, timestamp: "2026-10-01T09:41:07Z" },
    { ...EVENTS[4]!, timestamp: "2026-10-01T09:37:30Z" },
    EVENTS[5]!,
  ]);
  renderConsole();
  const times = (await lines(3)).map((line) => line.querySelector("time")!);
  // Still in the server's order, by when it stored them.
  expect(times.map((time) => [time.getAttribute("datetime"), time.textContent])).toEqual([
    ["2026-10-01T09:41:07Z", clock("2026-10-01T09:41:07Z")],
    ["2026-10-01T09:37:30Z", clock("2026-10-01T09:37:30Z")],
    [EVENTS[5]!.created, clock(EVENTS[5]!.created)],
  ]);
});

test("with agents picked, each level counts the view's own records, from facets read under its picks", async () => {
  const sent = serve();
  renderConsole();
  await lines(3);
  fireEvent.click(rail("Agents").getByRole("checkbox", { name: "tiles-a" }));
  await waitFor(() =>
    expect(levels().getAllByRole("button").map((button) => button.textContent)).toEqual(["Messages3", "+ Calls7", "+ Output27", "All40"]),
  );
  const reads = sent.filter((request) => request.path === "/api/web/feed/facets").map((request) => new URLSearchParams(request.query));
  expect(reads.map((query) => query.getAll("actor"))).toEqual([[], ["tiles-a"]]);
  // The agent facet still lists every agent.
  expect(agentBoxes()).toEqual(["tiles-a", "claude", "tiles-b"]);
});

test("a level reads the kinds it adds and shows them: + Calls the tool calls, + Output their results, All the bookkeeping", async () => {
  const sent = serve();
  renderConsole();
  await lines(3);
  fireEvent.click(levels().getByRole("button", { name: /^\+ Calls/ }));
  const [, call] = await lines(4);
  expect(call!.querySelector(".tool-arg")!.textContent).toBe("pytest -x");
  expect(feedReads(sent).at(-1)).toEqual({ tail: "true", limit: "300", kind: `${MESSAGES},ToolCall` });
  fireEvent.click(levels().getByRole("button", { name: /^\+ Output/ }));
  await lines(5);
  expect(feedReads(sent).at(-1)).toEqual({ tail: "true", limit: "300" });
  fireEvent.click(levels().getByRole("button", { name: /^All/ }));
  await lines(7);
  // + Output and All read the same records, so All reads nothing more.
  expect(feedReads(sent)).toHaveLength(3);
});

test("agents group by name family, and a family is picked as its pattern; the feed reads its agents", async () => {
  const sent = serve();
  renderConsole();
  await lines(3);
  const agents = rail("Agents");
  fireEvent.change(agents.getByRole("combobox", { name: "Group agents by" }), { target: { value: "family" } });
  expect(agentBoxes()).toEqual(["tiles-*", "tiles-a", "tiles-b", "claude (whole group)", "claude"]);
  fireEvent.click(agents.getByRole("checkbox", { name: "tiles-*" }));
  await waitFor(() => expect(feedReads(sent).at(-1)).toEqual({ tail: "true", limit: "300", actor: "tiles-a,tiles-b", ...CONVERSATION }));
  expect(agents.getByRole("checkbox", { name: "tiles-b" })).toHaveProperty("checked", true);
  fireEvent.click(agents.getByRole("button", { name: "Clear tiles-*" }));
  await waitFor(() => expect(feedReads(sent).at(-1)).toEqual({ tail: "true", limit: "300", ...CONVERSATION }));
});

test("agents are found by a fragment of their name or room, and a typed pattern is picked in one step", async () => {
  const sent = serve();
  renderConsole();
  await lines(3);
  const agents = rail("Agents");
  const find = agents.getByRole("searchbox", { name: "Find agents" });
  fireEvent.change(find, { target: { value: "LAB" } });
  expect(agentBoxes()).toEqual(["tiles-a"]);
  fireEvent.change(find, { target: { value: "tiles-*" } });
  fireEvent.click(agents.getByRole("button", { name: "Pick tiles-*" }));
  await waitFor(() => expect(feedReads(sent).at(-1)).toMatchObject({ actor: "tiles-a,tiles-b" }));
  fireEvent.change(find, { target: { value: "nobody-*" } });
  fireEvent.click(agents.getByRole("button", { name: "Pick nobody-*" }));
  fireEvent.click(agents.getByRole("button", { name: "Clear tiles-*" }));
  expect(await screen.findByText("No agent matches this view's picks.")).toBeTruthy();
  expect(document.querySelectorAll(".console-line")).toHaveLength(0);
  expect(levels().getAllByRole("button").map((button) => button.textContent)).toEqual(["Messages", "+ Calls", "+ Output", "All"]);
});

test("agents group by state, and Active in reads the facets over that much of the past: 15 m, 1 h, 24 h or 7 d, never all of it", async () => {
  const sent = serve();
  renderConsole();
  await lines(3);
  const agents = rail("Agents");
  fireEvent.change(agents.getByRole("combobox", { name: "Group agents by" }), { target: { value: "state" } });
  expect(agentBoxes()).toEqual(["Working (whole group)", "tiles-a", "claude", "Ended (whole group)", "tiles-b"]);
  const facetReads = () => sent.filter((request) => request.path === "/api/web/feed/facets").map((request) => new URLSearchParams(request.query));
  const minutesAgo = (query: URLSearchParams) => Math.round((Date.now() - Date.parse(query.get("since")!)) / 60_000);
  expect(facetReads().map(minutesAgo)).toEqual([60]);
  const active = within(agents.getByRole("group", { name: "Active in the last" }));
  // All would count the whole history: 8.8 s on 9 million records.
  expect(active.getAllByRole("button").map((button) => button.textContent)).toEqual(["15 m", "1 h", "24 h", "7 d"]);
  fireEvent.click(active.getByRole("button", { name: "15 m" }));
  await waitFor(() => expect(facetReads().map(minutesAgo)).toEqual([60, 15]));
  fireEvent.click(active.getByRole("button", { name: "7 d" }));
  await waitFor(() => expect(facetReads().map(minutesAgo)).toEqual([60, 15, 7 * 24 * 60]));
});

test("rooms and CLIs filter the feed, AND with each other", async () => {
  const sent = serve();
  renderConsole();
  await lines(3);
  fireEvent.click(rail("Rooms").getByRole("checkbox", { name: "lab" }));
  await waitFor(() => expect(feedReads(sent).at(-1)).toMatchObject({ room: "lab" }));
  await lines(2);
  fireEvent.click(rail("CLI").getByRole("checkbox", { name: "claude" }));
  await waitFor(() => expect(feedReads(sent).at(-1)).toMatchObject({ room: "lab", cli: "claude" }));
  expect(await screen.findByText("No records match this view.")).toBeTruthy();
});

/** The rail's views, in its order. */
const viewNames = () => rail("Views").queryAllByRole("listitem").map((item) => item.querySelector("button")!.textContent);

/** Store `views` as views kept from before, oldest first, each live and picking no agent, room or CLI. */
function keepViews(...views: { name: string; level: Level; rooms?: string[]; pinned?: boolean }[]) {
  const kept = views.map(({ name, level, rooms = [], pinned = false }, index) => ({
    id: `v${index}`,
    name,
    pinned,
    created: `2026-10-01T09:0${index}:00.000Z`,
    agents: [],
    rooms,
    clis: [],
    level,
    range: { live: true },
  }));
  localStorage.setItem(`trackinizer.v2.console.${PROFILE.email}`, JSON.stringify(kept));
}

test("a change saves the view: the rail lists it, Untitled, as the open one", async () => {
  serve();
  renderConsole();
  await lines(3);
  expect(viewNames()).toEqual([]);
  fireEvent.click(levels().getByRole("button", { name: /^All/ }));
  await lines(7);
  expect(viewNames()).toEqual(["Untitled view"]);
  expect(rail("Views").getByRole("button", { name: "Untitled view" }).getAttribute("aria-current")).toBe("page");
});

test("a saved view is renamed and pinned in the rail", async () => {
  serve();
  renderConsole();
  await lines(3);
  fireEvent.click(levels().getByRole("button", { name: /^All/ }));
  await lines(7);
  const views = rail("Views");
  fireEvent.click(views.getByRole("button", { name: "Rename Untitled view" }));
  const name = views.getByRole("textbox", { name: "View name" });
  fireEvent.change(name, { target: { value: "Everything" } });
  fireEvent.keyDown(name, { key: "Enter" });
  fireEvent.click(views.getByRole("button", { name: "Pin Everything" }));
  expect(views.getByRole("button", { name: "Unpin Everything" })).toBeTruthy();
  expect(viewNames()).toEqual(["Everything"]);
});

test("+ starts another view, listed after an older pinned one; a view picked in the rail opens, and a reload opens it again", async () => {
  serve();
  keepViews({ name: "Everything", level: 4, pinned: true });
  const { unmount } = renderConsole();
  await lines(7);
  // Found by their labels and text, as the rail's sections are.
  const views = rail("Views");
  fireEvent.click(views.getByText("New view", { selector: "button" }));
  await lines(3);
  fireEvent.click(rail("Rooms").getByLabelText("lab"));
  await lines(2);
  // Pinned views list first, then the rest, each newest first.
  expect(viewNames()).toEqual(["Everything", "Untitled view"]);
  fireEvent.click(views.getByText("Everything", { selector: "button" }));
  await lines(7);
  unmount();
  renderConsole();
  await lines(7);
  expect(levels().getByText(/^All/, { selector: "button" }).getAttribute("aria-pressed")).toBe("true");
});

test("Delete takes a view out of the rail and opens the one left", async () => {
  serve();
  keepViews({ name: "Everything", level: 4 }, { name: "Lab", level: 1, rooms: ["lab"] });
  renderConsole();
  // The newest view opens first.
  await lines(2);
  fireEvent.click(rail("Views").getByRole("button", { name: "Delete Lab" }));
  expect(viewNames()).toEqual(["Everything"]);
  await lines(7);
});

test("To follows the view's agents that have not ended; a line with no target goes to each, and a chip left out is not sent to", async () => {
  const sent = serve();
  renderConsole();
  await lines(3);
  expect(screen.queryByRole("group", { name: "To" })).toBeNull();
  const agents = rail("Agents");
  fireEvent.change(agents.getByRole("searchbox", { name: "Find agents" }), { target: { value: "tiles-*" } });
  fireEvent.click(agents.getByRole("button", { name: "Pick tiles-*" }));
  // Found by their labels and text, as the rail's sections are.
  const to = within(await screen.findByLabelText("To", { selector: "[role=group]" }));
  expect(to.getAllByRole("listitem").map((chip) => chip.textContent)).toEqual(["@tiles-a:ops", "@tiles-a:lab"]);
  const box = screen.getByLabelText("Message");
  const send = screen.getByText("Send message", { selector: "button" });
  fireEvent.change(box, { target: { value: "rerun the suite" } });
  fireEvent.click(send);
  await waitFor(() => expect(document.querySelector(".composer-receipt")?.textContent).toBe("Sent to 2 sessions"));
  fireEvent.click(to.getByLabelText("Leave out @tiles-a:lab"));
  fireEvent.change(box, { target: { value: "stop" } });
  fireEvent.click(send);
  await waitFor(() => expect(document.querySelector(".composer-receipt")?.textContent).toBe("Sent to 1 session"));
  expect(sent.filter((request) => request.path === "/api/messages").map((request) => request.body)).toEqual([
    { actor: "tiles-a", room: "ops", text: "rerun the suite" },
    { actor: "tiles-a", room: "lab", text: "rerun the suite" },
    { actor: "tiles-a", room: "ops", text: "stop" },
  ]);
});

test("a chip left out stays left out of its own view only, however views are switched", async () => {
  const sent = serve();
  renderConsole();
  await lines(3);
  // Every control here is found by its label or text, as the rail's sections are.
  const pick = (name: string) => fireEvent.click(rail("Agents").getByLabelText(name));
  const toGroup = () => screen.findByLabelText("To", { selector: "[role=group]" });
  const to = async () => within(await toGroup());
  const chips = async () => [...(await toGroup()).querySelectorAll("li")].map((chip) => chip.textContent);
  const views = rail("Views");
  pick("tiles-a");
  fireEvent.click(await views.findByLabelText("Rename Untitled view"));
  fireEvent.change(views.getByLabelText("View name"), { target: { value: "One" } });
  fireEvent.keyDown(views.getByLabelText("View name"), { key: "Enter" });
  pick("claude");
  fireEvent.click((await to()).getByLabelText("Leave out @claude"));
  fireEvent.click(views.getByText("New view", { selector: "button" }));
  await lines(3);
  pick("tiles-a");
  fireEvent.click((await to()).getByLabelText("Leave out @tiles-a:lab"));
  expect(await chips()).toEqual(["@tiles-a:ops"]);
  fireEvent.click(views.getByText("One", { selector: "button" }));
  await waitFor(async () => expect(await chips()).toEqual(["@tiles-a:ops", "@tiles-a:lab"]));
  fireEvent.change(screen.getByLabelText("Message"), { target: { value: "stop" } });
  fireEvent.click(screen.getByText("Send message", { selector: "button" }));
  await waitFor(() => expect(document.querySelector(".composer-receipt")?.textContent).toBe("Sent to 2 sessions"));
  expect(sent.filter((request) => request.path === "/api/messages").map((request) => request.body)).toEqual([
    { actor: "tiles-a", room: "ops", text: "stop" },
    { actor: "tiles-a", room: "lab", text: "stop" },
  ]);
});

test("the facets' window only finds agents: a narrower one keeps every agent the view picked, its records and its To chips", async () => {
  const sent = serve();
  renderConsole();
  await lines(3);
  const agents = rail("Agents");
  const active = within(agents.getByRole("group", { name: "Active in the last" }));
  fireEvent.click(active.getByRole("button", { name: "24 h" }));
  await waitFor(() => expect(agentBoxes()).toContain("tiles-c"));
  fireEvent.change(agents.getByRole("searchbox", { name: "Find agents" }), { target: { value: "tiles-*" } });
  fireEvent.click(agents.getByRole("button", { name: "Pick tiles-*" }));
  const chips = () => within(screen.getByRole("group", { name: "To" })).getAllByRole("listitem").map((chip) => chip.textContent);
  await waitFor(() => expect(chips()).toEqual(["@tiles-a:ops", "@tiles-a:lab", "@tiles-c:ops"]));
  await waitFor(() => expect(feedReads(sent).at(-1)).toMatchObject({ actor: "tiles-a,tiles-b,tiles-c" }));
  const reads = feedReads(sent).length;
  fireEvent.click(active.getByRole("button", { name: "1 h" }));
  await waitFor(() => expect(agentBoxes()).toEqual(["tiles-a", "claude", "tiles-b"]));
  expect(chips()).toEqual(["@tiles-a:ops", "@tiles-a:lab", "@tiles-c:ops"]);
  expect(feedReads(sent)).toHaveLength(reads);
});

test("live, with no stream batch, the facets' window still rolls on and Working turns Quiet", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  serve();
  renderConsole();
  await lines(3);
  const agents = rail("Agents");
  fireEvent.change(agents.getByRole("combobox", { name: "Group agents by" }), { target: { value: "state" } });
  expect(agentBoxes()).toEqual(["Working (whole group)", "tiles-a", "claude", "Ended (whole group)", "tiles-b"]);
  await act(() => vi.advanceTimersByTimeAsync(6 * 60_000));
  expect(agentBoxes()).toEqual(["Quiet (whole group)", "tiles-a", "claude", "Ended (whole group)", "tiles-b"]);
  fireEvent.click(within(agents.getByRole("group", { name: "Active in the last" })).getByRole("button", { name: "15 m" }));
  await waitFor(() => expect(agentBoxes()).toEqual(["Quiet (whole group)", "tiles-a", "claude"]));
  await act(() => vi.advanceTimersByTimeAsync(10 * 60_000));
  expect(agentBoxes()).toEqual([]);
});

test("a history range reads its window instead of the live tail, the facets count it, and Apply with no time says so", async () => {
  // A day after the window starts, well within the 7 days its facets may count.
  vi.useFakeTimers({ toFake: ["Date"], now: Date.parse("2026-10-02T12:00:00Z") });
  const sent = serve();
  renderConsole();
  await lines(3);
  expect(document.querySelector(".console-mark")!.textContent).toBe("● Live");
  const range = within(screen.getByRole("group", { name: "Range" }));
  fireEvent.click(range.getByRole("button", { name: "Apply" }));
  expect(range.getByRole("status").textContent).toBe("Set a from or to time first.");
  fireEvent.change(range.getByLabelText("From"), { target: { value: "2026-10-01T09:30" } });
  fireEvent.click(range.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(feedReads(sent)).toHaveLength(2));
  const since = new Date("2026-10-01T09:30").toISOString();
  expect(feedReads(sent)[1]).toEqual({ since, limit: "300", ...CONVERSATION });
  // The range's controls start afresh from the new range.
  const applied = within(screen.getByRole("group", { name: "Range" }));
  expect(applied.getByRole("button", { name: "Live" }).getAttribute("aria-pressed")).toBe("false");
  expect((applied.getByLabelText("From") as HTMLInputElement).value).toBe("2026-10-01T09:30");
  expect(document.querySelector(".console-mark")!.textContent).toBe("History");
  await waitFor(() => expect(sent.filter((request) => request.path === "/api/web/feed/facets").at(-1)!.query).toBe(`?since=${encodeURIComponent(since)}`));
});

test("a history range's facets count at most the 7 days before its end, now for an open one; its feed reads the window as it is", async () => {
  vi.useFakeTimers({ toFake: ["Date"], now: Date.parse("2026-10-02T12:00:00Z") });
  const sent = serve();
  const facetWindows = () =>
    sent.filter((request) => request.path === "/api/web/feed/facets").map((request) => new URLSearchParams(request.query)).map((query) => [query.get("since"), query.get("until")]);
  const until = "2026-10-01T10:00:00.000Z";
  // With no start, as Apply with only To sets: the 7 days before its end, not the whole history.
  const open = (since: string | null, end: string | null) => ({ id: "v", name: "Old", pinned: false, created: "2026-09-01T00:00:00.000Z", agents: [], rooms: [], clis: [], level: 1, range: { live: false, since, until: end } });
  localStorage.setItem(`trackinizer.v2.console.${PROFILE.email}`, JSON.stringify([open(null, until)]));
  const { unmount } = renderConsole();
  await lines(3);
  expect(feedReads(sent)).toEqual([{ until, limit: "300", ...CONVERSATION }]);
  await waitFor(() => expect(new Set(facetWindows().map(String))).toEqual(new Set([String(["2026-09-24T10:00:00.000Z", until])])));
  unmount();
  // A start a month back with no end: the 7 days before now.
  sent.length = 0;
  localStorage.setItem(`trackinizer.v2.console.${PROFILE.email}`, JSON.stringify([open("2026-09-01T00:00:00.000Z", null)]));
  renderConsole();
  await lines(3);
  expect(feedReads(sent)).toEqual([{ since: "2026-09-01T00:00:00.000Z", limit: "300", ...CONVERSATION }]);
  await waitFor(() => expect(new Set(facetWindows().map(String))).toEqual(new Set([String(["2026-09-25T12:00:00.000Z", null])])));
});

test("a view kept as a window of history opens on that window, its times in the range's boxes", async () => {
  const sent = serve();
  const since = "2026-10-01T09:30:00.000Z";
  const view = { id: "v", name: "Morning", pinned: false, created: since, agents: [], rooms: [], clis: [], level: 1, range: { live: false, since, until: null } };
  localStorage.setItem(`trackinizer.v2.console.${PROFILE.email}`, JSON.stringify([view]));
  renderConsole();
  await lines(3);
  expect(feedReads(sent)).toEqual([{ since, limit: "300", ...CONVERSATION }]);
  const range = within(screen.getByRole("group", { name: "Range" }));
  const local = new Date(new Date(since).getTime() - new Date(since).getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
  expect([range.getByLabelText("From"), range.getByLabelText("To")].map((box) => (box as HTMLInputElement).value)).toEqual([local, ""]);
});

test("a line goes to each target under its own key; Retry sends them again under the same keys; the receipt counts sessions", async () => {
  let refuse = true;
  const sent = serve(({ actor }) =>
    refuse && actor === "claude" ? Response.json({ detail: "statement timeout" }, { status: 500 }) : Response.json({ delivered: ["s1"] }),
  );
  renderConsole();
  await lines(3);
  const box = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(box, { target: { value: "@tiles-a:ops,@claude rerun the suite" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect((await screen.findByRole("alert")).textContent).toBe(
    "Not sent to @claude (statement timeout). Retry sends it again; no session gets a message twice.",
  );
  refuse = false;
  fireEvent.click(screen.getByRole("button", { name: "Retry message" }));
  await waitFor(() => expect(document.querySelector(".composer-receipt")?.textContent).toBe("Sent to 2 sessions"));
  const posts = sent.filter((request) => request.path === "/api/messages");
  const keyOf = (actor: string) => posts.filter((post) => (post.body as { actor: string }).actor === actor).map((post) => post.headers["idempotency-key"]);
  expect(posts.map((post) => post.body)).toEqual(
    expect.arrayContaining([
      { actor: "tiles-a", room: "ops", text: "rerun the suite" },
      { actor: "claude", room: null, text: "rerun the suite" },
    ]),
  );
  expect(posts).toHaveLength(4);
  const [tiles, claude] = [keyOf("tiles-a"), keyOf("claude")];
  expect([tiles.length, new Set(tiles).size, claude.length, new Set(claude).size]).toEqual([2, 1, 2, 1]);
  expect(tiles[0]).not.toBe(claude[0]);
});

test("a line with no target and no agents picked says why; @* sends to each agent-and-room pair shown", async () => {
  const sent = serve();
  renderConsole();
  await lines(3);
  const box = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(box, { target: { value: "stop" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect(screen.getByRole("alert").textContent).toBe("Pick agents for this view, or start with a target: @agent message");
  fireEvent.click(rail("Agents").getByRole("checkbox", { name: "tiles-a" }));
  await waitFor(() => expect(feedReads(sent).at(-1)).toMatchObject({ actor: "tiles-a" }));
  await lines(2);
  fireEvent.change(box, { target: { value: "@* stop" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(document.querySelector(".composer-receipt")?.textContent).toBe("Sent to 2 sessions"));
  expect(sent.filter((request) => request.path === "/api/messages").map((request) => request.body)).toEqual([
    { actor: "tiles-a", room: "ops", text: "stop" },
    { actor: "tiles-a", room: "lab", text: "stop" },
  ]);
});

test("Retry sends @* to the targets it first went to, under the same keys, whatever shows by then (CR-01)", async () => {
  let refuse = true;
  const sent = serve(({ room }) =>
    refuse && room === "lab" ? Response.json({ detail: "statement timeout" }, { status: 500 }) : Response.json({ delivered: ["s1"] }),
  );
  renderConsole();
  await lines(3);
  const agents = rail("Agents");
  fireEvent.click(agents.getByRole("checkbox", { name: "tiles-a" }));
  await lines(2);
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "@* stop" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect((await screen.findByRole("alert")).textContent).toBe(
    "Not sent to @tiles-a:lab (statement timeout). Retry sends it again; no session gets a message twice.",
  );
  // Every agent shows now, tiles-b too.
  fireEvent.click(agents.getByRole("checkbox", { name: "tiles-a" }));
  await lines(3);
  refuse = false;
  fireEvent.click(screen.getByRole("button", { name: "Retry message" }));
  await waitFor(() => expect(document.querySelector(".composer-receipt")?.textContent).toBe("Sent to 2 sessions"));
  const posts = sent.filter((request) => request.path === "/api/messages");
  const [ops, lab] = [{ actor: "tiles-a", room: "ops", text: "stop" }, { actor: "tiles-a", room: "lab", text: "stop" }];
  expect(posts.map((post) => post.body)).toEqual([ops, lab, ops, lab]);
  const keys = (room: string) => new Set(posts.filter((post) => (post.body as { room: string }).room === room).map((post) => post.headers["idempotency-key"]));
  expect([keys("ops").size, keys("lab").size]).toEqual([1, 1]);
});

test("@ lists the agents the view shows first, then the rest, then @*; Enter completes one, and the line goes there", async () => {
  const sent = serve();
  renderConsole();
  await lines(3);
  const box = screen.getByLabelText<HTMLTextAreaElement>("Message");
  const offered = () => [...document.querySelectorAll<HTMLElement>("[role=option]")].map((option) => option.title);
  box.focus();
  fireEvent.change(box, { target: { value: "@" } });
  // tiles-a's lines show, in two rooms; tiles-b's show too, but it has ended; claude is only in the facets.
  expect(offered()).toEqual(["@tiles-a:lab", "@tiles-a:ops", "@claude", "@*"]);
  fireEvent.keyDown(box, { key: "ArrowDown" });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(box.value).toBe("@tiles-a:ops ");
  fireEvent.change(box, { target: { value: "@tiles-a:ops rerun" } });
  fireEvent.keyDown(box, { key: "Enter" });
  await waitFor(() => expect(document.querySelector(".composer-receipt")?.textContent).toBe("Sent to 1 session"));
  expect(sent.filter((request) => request.path === "/api/messages").map((request) => request.body)).toEqual([{ actor: "tiles-a", room: "ops", text: "rerun" }]);
  // A view of claude alone ranks it first, though none of its lines show at Messages.
  fireEvent.click(rail("Agents").getByRole("checkbox", { name: "claude" }));
  await waitFor(() => expect(feedReads(sent).at(-1)).toMatchObject({ actor: "claude" }));
  fireEvent.change(box, { target: { value: "@" } });
  expect(offered()).toEqual(["@claude", "@tiles-a:lab", "@tiles-a:ops"]);
});

test("a click on a line's agent addresses the box to it, focused with the caret at its end; in the line's room when it is in several", async () => {
  serve();
  renderConsole();
  const [user, , onIt] = await lines(3);
  const box = screen.getByLabelText<HTMLTextAreaElement>("Message");
  // Found by label, as the rail's controls are.
  const agent = (line: HTMLElement, name: string) => within(line).getByLabelText(`Message ${name}`);
  fireEvent.click(agent(onIt!, "tiles-b"));
  expect([box.value, document.activeElement === box, box.selectionStart]).toEqual(["@tiles-b ", true, 9]);
  fireEvent.change(box, { target: { value: "@tiles-b rerun" } });
  fireEvent.click(agent(user!, "tiles-a"));
  expect([box.value, box.selectionStart]).toEqual(["@tiles-b,@tiles-a:ops rerun", 27]);
  fireEvent.click(agent(onIt!, "tiles-b"));
  expect(box.value).toBe("@tiles-b,@tiles-a:ops rerun");
});

test("a picked agent the facets do not count stays picked, so it can be cleared (CR-04)", async () => {
  const sent = serve();
  const view = { id: "v", name: "Gone", pinned: false, created: "2026-10-01T00:00:00Z", agents: ["gone-agent"], rooms: [], clis: [], level: 4, range: { live: true } };
  localStorage.setItem(`trackinizer.v2.console.${PROFILE.email}`, JSON.stringify([view]));
  renderConsole();
  expect(await screen.findByText("No records match this view.")).toBeTruthy();
  expect(feedReads(sent)).toEqual([{ tail: "true", limit: "300", actor: "gone-agent" }]);
  fireEvent.click(rail("Agents").getByRole("button", { name: "Clear gone-agent" }));
  await lines(7);
});

test("a viewer reads the console but gets no send box", async () => {
  serve();
  renderConsole({ ...PROFILE, role: "viewer" });
  const [user] = await lines(3);
  expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
  // Nor a line's agent to address it to.
  expect(user!.querySelector(".console-actor")!.tagName).toBe("B");
});
