import type { APIRequestContext, Page } from "@playwright/test";
import { resetCanvas } from "./canvasState";
import { expect, streamOpened, test } from "./fixtures";

// The console inside a station: a named one starts on the station's agents, an unnamed
// one on every session, and one control switches it; a line sent to an agent is told to the station's chat
// once, as a relay; and the chat draws that relay line as its own. The server here
// has no assistant, so what the chat would answer is out of reach: the relay request
// is intercepted, and the relay line is seeded as the chat's session record.

test.use({ canvas: true });

const TAG = crypto.randomUUID().slice(0, 8);

/** The station each test starts in: new, so unnamed. */
let station = "";

test.beforeEach(async ({ request }) => {
  station = await resetCanvas(request);
});

/** Start a session for agent `actor`, captured once, and polled as its `trax run` would poll it; its id. */
async function startAgent(request: APIRequestContext, actor: string, first: string): Promise<string> {
  const started = await request.post("/api/sessions/start", { data: { cli: "claude", title: `${actor} console`, actor } });
  expect(started.ok(), await started.text()).toBe(true);
  const { id } = await started.json();
  await capture(request, id, [{ kind: "UserMessage", payload: { content: first }, text: first }]);
  expect((await request.get(`/api/sessions/${id}/inbound?wait_sec=0`)).ok()).toBe(true);
  return id as string;
}

async function capture(request: APIRequestContext, id: string, records: readonly { kind: string; payload: object; text?: string }[], name = "console.jsonl") {
  const manifest = { name, metadata: {}, ir_id: crypto.randomUUID(), format: "claude", records: records.length };
  const appended = await request.post(`/api/sessions/${id}/records`, { data: { name, manifest, records: records.map((record, idx) => ({ idx, ...record })) } });
  expect(appended.ok(), await appended.text()).toBe(true);
}

/** Give the station a chat of the user's own (its head says typing joins it), so the console may tell it. */
async function bindOwnChat(page: Page, request: APIRequestContext): Promise<void> {
  const { email } = await (await request.get("/api/me/profile")).json();
  const started = await request.post("/api/sessions/start", { data: { cli: "claude", title: "console chat" } });
  expect(started.ok(), await started.text()).toBe(true);
  const { id: session } = await started.json();
  const conversation = crypto.randomUUID();
  await page.route(`**/api/chats/${conversation}`, (route) => route.fulfill({ json: {
    conversation_id: conversation, session_id: session, title: "E2E", account: email, live: true, forks: 0, forked_from: null,
    forks_on_typing: false, audience: "open", may_hide: false, shared_with: [], may_share: false, may_delete: false,
  } }));
  expect((await request.post(`/api/workspaces/for-chat/${conversation}`)).ok()).toBe(true);
}

async function openConsole(page: Page): Promise<void> {
  const subscribed = streamOpened(page);
  await page.goto("/app/#/console");
  await subscribed;
  await expect(page.getByRole("group", { name: "Scope" })).toBeVisible();
}

/** Every feed read the page makes, by its `station` parameter (null for none). */
function watchFeed(page: Page): (string | null)[] {
  const reads: (string | null)[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/web/feed") reads.push(url.searchParams.get("station"));
  });
  return reads;
}

test("in a named station the console opens on the station's agents, and All sessions widens it and This station narrows it again", async ({ page, request }) => {
  const actor = `scope-${TAG}`;
  await startAgent(request, actor, "Agent outside the station.");
  const renamed = await request.patch(`/api/workspaces/${station}`, { data: { name: `scope-${TAG}` } });
  expect(renamed.ok(), await renamed.text()).toBe(true);
  const reads = watchFeed(page);
  await openConsole(page);
  const scope = page.getByRole("group", { name: "Scope" });
  await expect(scope.getByRole("button", { name: "This station" })).toHaveAttribute("aria-pressed", "true");
  // Never read unscoped first: every read before the widening carries the station.
  expect(reads.every((each) => each === station)).toBe(true);
  // The agent was not started by the station's chat, so its line is not there.
  await expect(page.locator(".console-line", { hasText: "Agent outside the station." })).toHaveCount(0);

  // A live refetch may still go out scoped while the click lands: from the first read in the new scope on, every read is in it.
  reads.length = 0;
  await scope.getByRole("button", { name: "All sessions" }).click();
  await expect(page.locator(".console-line", { hasText: "Agent outside the station." })).toBeVisible();
  expect(reads).toContain(null);
  expect(reads.slice(reads.indexOf(null)).every((each) => each === null), JSON.stringify(reads)).toBe(true);

  reads.length = 0;
  await scope.getByRole("button", { name: "This station" }).click();
  await expect(page.locator(".console-line", { hasText: "Agent outside the station." })).toHaveCount(0);
  expect(reads).toContain(station);
  expect(reads.slice(reads.indexOf(station)).every((each) => each === station), JSON.stringify(reads)).toBe(true);
});

test("in an unnamed station the console opens on every session, as before stations", async ({ page, request }) => {
  await startAgent(request, `unnamed-${TAG}`, "Agent of no station.");
  const reads = watchFeed(page);
  await openConsole(page);
  const scope = page.getByRole("group", { name: "Scope" });
  await expect(scope.getByRole("button", { name: "All sessions" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".console-line", { hasText: "Agent of no station." })).toBeVisible();
  expect(reads.length).toBeGreaterThan(0);
  expect(reads.every((each) => each === null)).toBe(true);
});

test("a line to an agent goes to it, then the station's chat is told once, under one key, with the agent that took it", async ({ page, request }) => {
  const actor = `relay-${TAG}`;
  await startAgent(request, actor, "Waiting for work.");
  await bindOwnChat(page, request);
  const relays: { key: string | undefined; body: unknown }[] = [];
  await page.route("**/api/workspaces/*/relay", async (route) => {
    relays.push({ key: route.request().headers()["idempotency-key"], body: route.request().postDataJSON() });
    await route.fulfill({ json: { delivered: true } });
  });
  await openConsole(page);
  await page.getByRole("group", { name: "Scope" }).getByRole("button", { name: "All sessions" }).click();
  await expect(page.locator(".console-line", { hasText: "Waiting for work." })).toBeVisible();
  await page.getByRole("textbox", { name: "Message" }).last().fill(`@${actor} rerun the suite`);
  await page.getByRole("textbox", { name: "Message" }).last().press("Enter");
  await expect(page.locator(".composer-receipt")).toHaveText("Sent to 1 session");
  expect(relays).toHaveLength(1);
  expect(relays[0]!.body).toEqual({ targets: [{ actor, room: null }], text: "rerun the suite" });
  expect(relays[0]!.key).toMatch(/^[0-9a-f-]{36}$/);
});

test("a chat that was not told is said quietly, and the console's box names its target", async ({ page, request }) => {
  const actor = `quiet-${TAG}`;
  await startAgent(request, actor, "Standing by.");
  await bindOwnChat(page, request);
  await page.route("**/api/workspaces/*/relay", (route) => route.fulfill({ json: { delivered: false } }));
  await openConsole(page);
  await page.getByRole("group", { name: "Scope" }).getByRole("button", { name: "All sessions" }).click();
  const agents = page.getByRole("region", { name: "Agents" });
  await agents.getByRole("combobox", { name: "Group agents by" }).selectOption("family");
  await agents.getByRole("checkbox", { name: `quiet-*`, exact: true }).check();
  await expect(page.getByRole("textbox", { name: "Message" }).last()).toHaveAttribute("placeholder", new RegExp(`^Message ${actor}…`));
  await page.getByRole("textbox", { name: "Message" }).last().fill("hello");
  await page.getByRole("textbox", { name: "Message" }).last().press("Enter");
  await expect(page.locator(".composer-receipt")).toHaveText("Sent to 1 session; Chat not told");
});

test(`the chat draws a relay line as its own`, async ({ page, request }) => {
  const { email } = await (await request.get("/api/me/profile")).json();
  const started = await request.post("/api/sessions/start", { data: { cli: "claude", title: "E2E relay chat" } });
  const { id: session } = await started.json();
  const relay = "→ lead-2, tiles-a:ops: rerun the suite";
  await capture(request, session, [
    { kind: "AgentToAgentMessage", payload: { sender: email, content: "Hello." }, text: "Hello." },
    { kind: "AssistantMessage", payload: { content: "Hi." }, text: "Hi." },
    { kind: "AgentToAgentMessage", payload: { sender: email, content: relay }, text: relay },
  ], "chat.jsonl");
  const conversation = crypto.randomUUID();
  await page.route(`**/api/chats/${conversation}`, (route) => route.fulfill({ json: {
    conversation_id: conversation, session_id: session, title: "E2E", account: email, live: true, forks: 0, forked_from: null, forks_on_typing: false, audience: "open", may_hide: false, shared_with: [], may_share: false, may_delete: false,
  } }));
  expect((await request.post(`/api/workspaces/for-chat/${conversation}`)).ok()).toBe(true);
  await page.setViewportSize({ width: 1400, height: 900 });
  const subscribed = streamOpened(page);
  await page.goto("/app/#/list/Issue");
  await subscribed;
  const line = page.locator(".chat-line-relay");
  await expect(line).toHaveText("you → lead-2, tiles-a:ops: rerun the suite");
  await expect(page.locator(".chat-panel .chat-line-user")).toHaveCount(1);
});
