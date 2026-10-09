import { mkdir } from "node:fs/promises";
import type { APIRequestContext, Page } from "@playwright/test";
import { resetCanvas } from "./canvasState";
import { expect, streamOpened, test } from "./fixtures";

// The default path: the canvas is on, with Chat beside the page.
test.use({ canvas: true });

// An answer in Chat lights up the rows it cites at once, through the store the
// agent's Highlight tool feeds. The test stands in for the assistant: it writes
// the conversation's session records as the assistant's capture does, and the
// page learns of them from the canvas stream, as it learns of a real answer.

const SHOTS = "/opt/scratch/artifacts/trax-hosted/ui/autohighlight";
const SWITCH = "Highlight rows the assistant mentions";
const GLOW_BUDGET_MS = 200;

type Row = { readonly id: string; readonly seq: number; readonly title: string };

async function createIssue(request: APIRequestContext, title: string): Promise<Row> {
  const created = await request.post("/api/inquiries/issue", { data: { title, idempotency_key: crypto.randomUUID() } });
  expect(created.ok(), await created.text()).toBe(true);
  const { id } = await created.json();
  const { self } = await (await request.get(`/api/web/get/${id}`)).json();
  return { id, seq: self.seq, title };
}

/** A conversation the signed-in user started, held in a session with `lines`; Chat opens it on load. */
async function openConversation(page: Page, lines: readonly { kind: string; content: string }[]) {
  const { request } = page;
  const { email } = await (await request.get("/api/me/profile")).json();
  const started = await request.post("/api/sessions/start", { data: { cli: "claude", title: "E2E highlight chat" } });
  expect(started.ok(), await started.text()).toBe(true);
  const { id: session } = await started.json();
  const file = { name: "chat.jsonl", irId: crypto.randomUUID() };
  const capture = async (from: number, turns: readonly { kind: string; content: string }[]) => {
    const records = turns.map(({ kind, content }, k) => ({ idx: from + k, kind, payload: { content, sender: email }, text: content }));
    const manifest = { name: file.name, metadata: {}, ir_id: file.irId, format: "claude", records: from + turns.length };
    const appended = await request.post(`/api/sessions/${session}/records`, { data: { name: file.name, manifest, records } });
    expect(appended.ok(), await appended.text()).toBe(true);
  };
  await capture(0, lines);
  const conversation = crypto.randomUUID();
  // The assistant opens a conversation's session; the server here has none, so its head is the test's.
  await page.route(`**/api/chats/${conversation}`, (route) => route.fulfill({ json: {
    conversation_id: conversation, session_id: session, title: "E2E", account: email, live: true, forks: 0, forked_from: null, forks_on_typing: false,
  } }));
  const workspace = await (await request.post("/api/workspaces")).json();
  await page.addInitScript(([key, value]) => localStorage.setItem(key!, value!), [`trackinizer.v2.chat.${workspace.id}`, conversation]);
  return { email, answer: (content: string, at: number) => capture(at, [{ kind: "AssistantMessage", content }]) };
}

/** Watch the page for the answer's line and for the rows' glow; read both times back with `lit`. */
async function watchGlow(page: Page, rows: number) {
  await page.evaluate((count) => {
    const times = { answer: 0, glow: 0, painted: 0 };
    Object.assign(window, { glowTimes: times });
    new MutationObserver(() => {
      const now = performance.now();
      if (!times.answer && document.querySelector(".chat-line-assistant")) times.answer = now;
      if (!times.glow && document.querySelectorAll(".row.is-highlighted").length >= count) {
        times.glow = now;
        requestAnimationFrame(() => { times.painted = performance.now(); });
      }
    }).observe(document.body, { subtree: true, childList: true, attributes: true, characterData: true });
  }, rows);
  return () => page.evaluate(() => (window as unknown as { glowTimes: { answer: number; glow: number; painted: number } }).glowTimes);
}

/** Record the title of every row that glows from now on; read the record back with the result. */
async function watchGlowed(page: Page) {
  await page.evaluate(() => {
    const seen = new Set<string>();
    Object.assign(window, { glowed: seen });
    new MutationObserver(() => {
      for (const row of document.querySelectorAll(".row.is-highlighted")) seen.add(row.textContent ?? "");
    }).observe(document.body, { subtree: true, childList: true, attributes: true });
  });
  return async () => {
    const texts = await page.evaluate(() => [...(window as unknown as { glowed: Set<string> }).glowed]);
    return texts.map((text) => /Quiet \w+ [0-9a-f]{8}/.exec(text)?.[0] ?? text);
  };
}

test.beforeEach(async ({ request }) => {
  await resetCanvas(request);
});

test("an answer citing two rows lights them up within the budget", async ({ page, request }, info) => {
  const stamp = crypto.randomUUID().slice(0, 8);
  const first = await createIssue(request, `Highlight first ${stamp}`);
  const second = await createIssue(request, `Highlight second ${stamp}`);
  const other = await createIssue(request, `Highlight other ${stamp}`);
  const { answer } = await openConversation(page, [{ kind: "AgentToAgentMessage", content: "Which are open?" }]);
  const subscribed = streamOpened(page);
  await page.goto("/app/#/list/Issue");
  await subscribed;
  await expect(page.getByText("Which are open?")).toBeVisible();
  await expect(page.locator("a.row", { hasText: other.title })).toBeVisible();
  expect(await page.locator(".row.is-highlighted").count()).toBe(0);

  const lit = await watchGlow(page, 2);
  await answer(`Both are open: Issue#${first.seq} and Issue#${second.seq}.`, 1);

  await expect(page.locator("a.row.is-highlighted")).toHaveCount(2);
  await expect(page.locator("a.row.is-highlighted", { hasText: first.title })).toBeVisible();
  await expect(page.locator("a.row.is-highlighted", { hasText: second.title })).toBeVisible();
  await expect(page.locator("a.row", { hasText: other.title })).not.toHaveClass(/is-highlighted/);
  await expect(page.getByRole("img", { name: "Pointed out" })).toHaveCount(2);
  await expect.poll(async () => (await lit()).painted).toBeGreaterThan(0);
  const times = await lit();
  const domMs = times.glow - times.answer;
  const paintedMs = times.painted - times.answer;
  info.annotations.push({ type: "answer to glow", description: `${domMs.toFixed(1)} ms in the DOM, ${paintedMs.toFixed(1)} ms painted` });
  console.log(`answer to glow: ${domMs.toFixed(1)} ms in the DOM, ${paintedMs.toFixed(1)} ms painted`);
  expect(times.answer, "the answer line was seen").toBeGreaterThan(0);
  expect(paintedMs, "answer appearing to rows painted as glowing").toBeLessThan(GLOW_BUDGET_MS);

  await mkdir(SHOTS, { recursive: true });
  for (const theme of ["light", "dark"] as const) {
    await page.evaluate((shown) => { document.documentElement.dataset.theme = shown; }, theme);
    await page.screenshot({ path: `${SHOTS}/chat-highlight-${theme}.png` });
  }
});

test("with the switch off in Settings an answer lights nothing", async ({ page, request }) => {
  const stamp = crypto.randomUUID().slice(0, 8);
  const first = await createIssue(request, `Quiet first ${stamp}`);
  const second = await createIssue(request, `Quiet second ${stamp}`);
  const third = await createIssue(request, `Quiet third ${stamp}`);
  const { answer } = await openConversation(page, [{ kind: "AgentToAgentMessage", content: "Which are open?" }]);
  const subscribed = streamOpened(page);
  await page.goto("/app/#/settings");
  await subscribed;
  const toggle = page.getByRole("checkbox", { name: SWITCH });
  await expect(toggle).toBeChecked();
  await toggle.uncheck();
  await expect(toggle).not.toBeChecked();
  await mkdir(SHOTS, { recursive: true });
  await toggle.scrollIntoViewIfNeeded();
  await page.locator(".scroll").evaluate((element) => element.scrollBy(0, 200));
  for (const theme of ["light", "dark"] as const) {
    await page.evaluate((shown) => { document.documentElement.dataset.theme = shown; }, theme);
    await page.screenshot({ path: `${SHOTS}/settings-switch-${theme}.png` });
  }

  await page.goto("/app/#/list/Issue");
  await expect(page.getByText("Which are open?")).toBeVisible();
  await expect(page.locator("a.row", { hasText: first.title })).toBeVisible();
  const glowed = await watchGlowed(page);
  await answer(`Both are open: Issue#${first.seq} and Issue#${second.seq}.`, 1);
  await expect(page.locator(".chat-line-assistant")).toContainText("Both are open");
  // Positive control, in place of a wait for a late glow to land: with the switch on again, an
  // answer citing a third row lights it, and every glow seen since the first answer is that one.
  await page.goto("/app/#/settings");
  await page.getByRole("checkbox", { name: SWITCH }).check();
  await page.goto("/app/#/list/Issue");
  await answer(`And Issue#${third.seq}.`, 2);
  await expect(page.locator("a.row.is-highlighted")).toHaveCount(1);
  await expect(page.locator("a.row.is-highlighted", { hasText: third.title })).toBeVisible();
  expect(await glowed()).toEqual([third.title]);
});

test("the choice of the switch is the browser's and survives a reload", async ({ page }) => {
  await page.goto("/app/#/settings");
  const toggle = page.getByRole("checkbox", { name: SWITCH });
  await expect(toggle).toBeChecked();
  await toggle.uncheck();
  await page.reload();
  await expect(page.getByRole("checkbox", { name: SWITCH })).not.toBeChecked();
});
