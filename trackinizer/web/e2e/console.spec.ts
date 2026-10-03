import type { APIRequestContext, Page } from "@playwright/test";
import { expect, failedResource, test } from "./fixtures";

// The console (#/console): every session's captured records as one live feed,
// narrowed by a view that saves itself, and a line that messages agents. The test
// stands in for each session's `trax run`: a drain that returns at once
// (`GET .../inbound?wait_sec=0`) marks the session polled and reads what reached
// it. Other spec files write to the same feed, so each test names its own agents,
// one name family per test, and picks that family.

const TAG = crypto.randomUUID().slice(0, 8);

/** Start a session for agent `actor` in `rooms`; its id and file, with one captured turn. */
async function startSession(request: APIRequestContext, actor: string, rooms: string[], first: string) {
  const started = await request.post("/api/sessions/start", { data: { cli: "claude", title: `${actor} console`, actor, rooms } });
  expect(started.ok(), await started.text()).toBe(true);
  const { id } = await started.json();
  const file = { name: "console.jsonl", irId: crypto.randomUUID() };
  await capture(request, id, file, 0, [{ kind: "UserMessage", payload: { content: first }, text: first }]);
  return { id: id as string, file };
}

/** Append `records` from record `from` on, as the session's capture does. */
async function capture(
  request: APIRequestContext,
  id: string,
  file: { name: string; irId: string },
  from: number,
  records: readonly { kind: string; payload: object; text?: string }[],
) {
  const manifest = { name: file.name, metadata: {}, ir_id: file.irId, format: "claude", records: from + records.length };
  const data = { name: file.name, manifest, records: records.map((record, k) => ({ idx: from + k, ...record })) };
  const appended = await request.post(`/api/sessions/${id}/records`, { data });
  expect(appended.ok(), await appended.text()).toBe(true);
}

/** Take what is queued for the session, as its `trax run` does; the first take marks it polled. */
async function drain(request: APIRequestContext, id: string): Promise<string[]> {
  const drained = await request.get(`/api/sessions/${id}/inbound?wait_sec=0`);
  expect(drained.ok(), await drained.text()).toBe(true);
  return (await drained.json()).messages.map((message: { text: string }) => message.text);
}

/** Open the console once the live stream is connected. */
async function openConsole(page: Page) {
  const subscribed = page.waitForResponse((response) => response.url().includes("/api/web/subscribe"));
  await page.goto("/app/#/console");
  await subscribed;
}

/** Pick name family `family` (its agents, named `<family>-<x>`) in the agent facet, grouped by family. */
async function pickFamily(page: Page, family: string) {
  const agents = page.getByRole("region", { name: "Agents" });
  await agents.getByRole("combobox", { name: "Group agents by" }).selectOption("family");
  await agents.getByRole("checkbox", { name: `${family}-*`, exact: true }).check();
}

const lines = (page: Page) => page.locator(".console-line");

test("a view picks a family of agents and a level, follows them live, moves by the minimap, and a reload opens it again", async ({ page }) => {
  const family = `fam-${TAG}`;
  const room = `ops-${TAG}`;
  const a = await startSession(page.request, `${family}-a`, [room], "Fix the flaky test.");
  const b = await startSession(page.request, `${family}-b`, [room], "Review the fix.");
  await startSession(page.request, `solo-${TAG}`, [], "Not in the family.");
  await capture(page.request, a.id, a.file, 1, [
    { kind: "ToolCall", payload: { call_id: "c1", name: "Bash", arguments: { command: "pytest -x" } } },
    { kind: "TokenUsage", payload: { info: { input_tokens: 3 } } },
  ]);
  await openConsole(page);
  await pickFamily(page, family);
  // Messages: the two people's messages, not the call, the token count or the other agent.
  await expect(lines(page)).toHaveCount(2);
  await expect(lines(page).filter({ hasText: "Fix the flaky test." })).toContainText(`${family}-a[${room}]User`);
  await expect(page.getByText("Not in the family.")).toHaveCount(0);

  const levels = page.getByRole("group", { name: "Level" });
  await levels.getByRole("button", { name: /^\+ Calls/ }).click();
  await expect(lines(page)).toHaveCount(3);
  await expect(lines(page).locator(".tool-arg")).toHaveText("pytest -x");
  await levels.getByRole("button", { name: /^All/ }).click();
  await expect(lines(page)).toHaveCount(4);
  await levels.getByRole("button", { name: /^\+ Calls/ }).click();
  await expect(lines(page)).toHaveCount(3);

  // A record captured now shows without a reload.
  await capture(page.request, b.id, b.file, 1, [{ kind: "AssistantMessage", payload: { content: "**Fixed.**" }, text: "**Fixed.**" }]);
  await expect(lines(page)).toHaveCount(4);
  await expect(lines(page).last().locator("strong")).toHaveText("Fixed.");

  // A click on the minimap, an hour back at its left edge, moves the feed there: a window from that time on.
  const read = page.waitForRequest((request) => request.url().includes("/api/web/feed?") && new URL(request.url()).searchParams.has("since"));
  await page.locator(".minimap-band").click({ position: { x: 4, y: 10 } });
  const since = Date.parse(new URL((await read).url()).searchParams.get("since")!);
  expect(Math.abs(Date.now() - 3_600_000 - since)).toBeLessThan(120_000);
  await expect(page.locator(".console-mark")).toHaveText("History");
  await expect(lines(page)).toHaveCount(4);

  const views = page.getByRole("region", { name: "Views" });
  await views.getByRole("button", { name: "Rename Untitled view" }).click();
  await views.getByRole("textbox", { name: "View name" }).fill(`Family ${TAG}`);
  await views.getByRole("textbox", { name: "View name" }).press("Enter");
  await page.reload();
  await expect(views.getByRole("button", { name: `Family ${TAG}`, exact: true })).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("button", { name: `Clear ${family}-*` })).toBeVisible();
  await expect(levels.getByRole("button", { name: /^\+ Calls/ })).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".console-mark")).toHaveText("History");
  await expect(lines(page)).toHaveCount(4);
});

test("a line goes to the view's agents, each once with a retry, @ lists them, and @agent and @* still work", async ({ page, allowErrors }) => {
  const family = `send-${TAG}`;
  const room = `ops-${TAG}`;
  const a = await startSession(page.request, `${family}-a`, [room], "Ready.");
  const b = await startSession(page.request, `${family}-b`, [], "Ready too.");
  expect([await drain(page.request, a.id), await drain(page.request, b.id)]).toEqual([[], []]);
  const keys: { [actor: string]: string[] } = {};
  let lost = false;
  // The first send to b reaches the server, which queues it, but its answer never reaches the page.
  await page.route("**/api/messages", async (route) => {
    const { actor } = route.request().postDataJSON();
    (keys[actor] ??= []).push(route.request().headers()["idempotency-key"] ?? "");
    if (actor !== `${family}-b` || lost) return route.continue();
    lost = true;
    await route.fetch();
    return route.abort();
  });
  // The browser's own line for the request whose answer was cut.
  allowErrors(failedResource("/api/messages", "ERR_FAILED"));
  await openConsole(page);
  await pickFamily(page, family);
  await expect(lines(page)).toHaveCount(2);
  const to = page.getByRole("group", { name: "To" });
  await expect(to.getByRole("listitem")).toHaveCount(2);
  for (const chip of [`@${family}-a:${room}`, `@${family}-b`]) await expect(to.getByText(chip, { exact: true })).toBeVisible();
  // By its label: while it lists agents, the box is a combobox.
  const box = page.getByLabel("Message", { exact: true });
  await box.fill(`@${family}`);
  // Both agents' lines show, b's the latest; a, in one room, goes bare.
  await expect(page.getByRole("listbox", { name: "Agents" }).getByRole("option")).toHaveText([`@${family}-b`, `@${family}-a`]);
  await expect(page.getByRole("combobox", { name: "Message" })).toHaveAttribute("aria-expanded", "true");
  await box.press("ArrowDown");
  await box.press("Tab");
  await expect(box).toHaveValue(`@${family}-a `);
  await expect(page.getByRole("listbox", { name: "Agents" })).toBeHidden();
  await box.fill("rerun the suite");
  await box.press("Enter");
  await expect(page.getByRole("alert")).toContainText(`Not sent to @${family}-b`);
  await page.getByRole("button", { name: "Retry message" }).click();
  await expect(page.locator(".composer-receipt")).toHaveText("Sent to 2 sessions");
  const [ka, kb] = [keys[`${family}-a`]!, keys[`${family}-b`]!];
  expect([ka.length, new Set(ka).size, kb.length, new Set(kb).size]).toEqual([2, 1, 2, 1]);
  expect([await drain(page.request, a.id), await drain(page.request, b.id)]).toEqual([["rerun the suite"], ["rerun the suite"]]);

  await box.fill(`@${family}-a:${room} only you`);
  await box.press("Enter");
  await expect(page.locator(".composer-receipt")).toHaveText("Sent to 1 session");
  await box.fill("@* stop and summarize");
  await box.press("Enter");
  await expect(page.locator(".composer-receipt")).toHaveText("Sent to 2 sessions");
  expect([await drain(page.request, a.id), await drain(page.request, b.id)]).toEqual([["only you", "stop and summarize"], ["stop and summarize"]]);

  await box.fill(`@nobody-${TAG} hello`);
  await box.press("Enter");
  await expect(page.locator(".composer-receipt")).toHaveText(`Sent to 0 sessions; no live session for @nobody-${TAG}`);
});

test("a window of history reads that window and follows nothing", async ({ page }) => {
  const family = `hist-${TAG}`;
  await startSession(page.request, `${family}-a`, [], "Before the window closed.");
  await openConsole(page);
  await pickFamily(page, family);
  await expect(lines(page)).toHaveCount(1);
  const range = page.getByRole("group", { name: "Range" });
  const local = new Date(Date.now() - 60_000 - new Date().getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
  await range.getByLabel("From").fill(local);
  await range.getByRole("button", { name: "Apply" }).click();
  await expect(page.locator(".console-mark")).toHaveText("History");
  await expect(lines(page)).toHaveCount(1);
});
