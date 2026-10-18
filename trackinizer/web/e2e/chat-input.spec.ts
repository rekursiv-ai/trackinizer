import type { Locator, Page } from "@playwright/test";
import { exposeEventSources, resetCanvas } from "./canvasState";
import { expect, streamOpened, test } from "./fixtures";

test.use({ canvas: true });

type Station = { id: string; name: string };

test.beforeEach(async ({ page, request }) => {
  await resetCanvas(request);
  await page.addInitScript(exposeEventSources);
});

async function conversation(page: Page): Promise<Station> {
  const { request } = page;
  const { email } = await (await request.get("/api/me/profile")).json();
  const started = await request.post("/api/sessions/start", { data: { cli: "claude", title: "E2E chat input" } });
  expect(started.ok(), await started.text()).toBe(true);
  const { id: session } = await started.json();
  const turns = [
    { kind: "AgentToAgentMessage", content: "My first question", sender: email },
    { kind: "AssistantMessage", content: "First answer", sender: email },
    { kind: "AgentToAgentMessage", content: "Someone else's question", sender: "other@example.com" },
    { kind: "AgentToAgentMessage", content: "My latest question", sender: email },
    { kind: "AssistantMessage", content: "Latest answer", sender: email },
  ];
  const records = turns.map(({ kind, content, sender }, idx) => ({ idx, kind, payload: { content, sender }, text: content }));
  const name = "chat.jsonl";
  const manifest = { name, metadata: {}, ir_id: crypto.randomUUID(), format: "claude", records: records.length };
  const appended = await request.post(`/api/sessions/${session}/records`, { data: { name, manifest, records } });
  expect(appended.ok(), await appended.text()).toBe(true);
  const id = crypto.randomUUID();
  await page.route(`**/api/chats/${id}`, (route) => route.fulfill({ json: {
    conversation_id: id, session_id: session, title: "E2E", account: email, live: true, forks: 0,
    forked_from: null, forks_on_typing: false, audience: "open", may_hide: false,
    shared_with: [], may_share: false, may_delete: false,
  } }));
  const station = await request.post(`/api/workspaces/for-chat/${id}`);
  expect(station.ok(), await station.text()).toBe(true);
  return await station.json() as Station;
}

async function enableInput(page: Page, station: Station): Promise<Locator> {
  const response = await page.request.get(`/api/workspaces/${station.id}`);
  expect(response.ok(), await response.text()).toBe(true);
  const state = await response.json();
  // The harness has no assistant. Only its availability is supplied; records, drafts and stations use the server.
  await expect.poll(() => page.evaluate((id) => (window as unknown as { heldEventSources: EventSource[] }).heldEventSources.some((source) => source.url.includes(`/workspaces/${id}/events`)), state.id)).toBe(true);
  const input = page.locator(".chat-panel textarea");
  // The station's own stream sends its state, assistant-less, once as it connects. That frame may land after ours and
  // put the input off again, so ours goes again until the input is on, which it stays once the stream's has come.
  await expect.poll(async () => {
    await page.evaluate((state) => {
      const sources = (window as unknown as { heldEventSources: EventSource[] }).heldEventSources;
      const stream = sources.findLast((source) => source.url.includes(`/workspaces/${state.id}/events`));
      if (!stream) throw new Error(`workspace stream missing: ${state.id}`);
      stream.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "workspace", t: Date.now(), state: {
        ...state, partner: { session_id: null, actor: "input-test", cli: "claude", status: "live", kind: "shared" },
      } }) }));
    }, state);
    return input.isEnabled();
  }).toBe(true);
  return input;
}

async function open(page: Page, station: Station): Promise<Locator> {
  const subscribed = streamOpened(page);
  await page.goto("/app/#/list/Issue");
  await subscribed;
  await expect(page.locator(".chat-panel").getByText("Latest answer", { exact: true })).toBeVisible();
  return enableInput(page, station);
}

async function switchChat(page: Page, name: string): Promise<void> {
  await page.locator(".chat-name-button").click();
  const dialog = page.getByRole("dialog", { name: "Chats" });
  const search = dialog.getByRole("textbox", { name: "Search chats" });
  await search.fill(name);
  await dialog.getByRole("button", { name, exact: true }).last().click();
}

async function caret(input: Locator, start: number, end = start): Promise<void> {
  await input.focus();
  await input.evaluate((element: HTMLTextAreaElement, [start, end]) => element.setSelectionRange(start!, end!), [start, end]);
}

test("history recalls only my messages and restores the unfinished draft", async ({ page }) => {
  const input = await open(page, await conversation(page));
  await input.fill("Unfinished draft");
  await caret(input, 0);
  await input.press("ArrowUp");
  await expect(input).toHaveValue("My latest question");
  await input.press("ArrowUp");
  await expect(input).toHaveValue("My first question");
  await input.press("ArrowUp");
  await expect(input).toHaveValue("My first question");
  await caret(input, "My first question".length);
  await input.press("ArrowDown");
  await expect(input).toHaveValue("My latest question");
  await input.press("ArrowDown");
  await expect(input).toHaveValue("Unfinished draft");
  await input.press("ArrowDown");
  await expect(input).toHaveValue("Unfinished draft");
});

test("multiline movement, selections and modified arrows do not recall history", async ({ page }) => {
  const input = await open(page, await conversation(page));
  const text = "first line\nsecond line";
  await input.fill(text);
  await caret(input, text.length);
  await input.press("ArrowUp");
  await expect(input).toHaveValue(text);
  await expect.poll(() => input.evaluate((element: HTMLTextAreaElement) => element.selectionStart)).toBeLessThan(text.indexOf("\n") + 1);
  await caret(input, 0);
  await input.press("ArrowDown");
  await expect(input).toHaveValue(text);
  await expect.poll(() => input.evaluate((element: HTMLTextAreaElement) => element.selectionStart)).toBeGreaterThan(text.indexOf("\n"));
  for (const modifier of ["Shift", "Alt", "Control", "Meta"]) {
    await caret(input, 0);
    await input.press(`${modifier}+ArrowUp`);
    await expect(input).toHaveValue(text);
  }
  await caret(input, 0, 5);
  await input.press("ArrowUp");
  await expect(input).toHaveValue(text);
  await caret(input, text.length);
  await input.press("Shift+Enter");
  await expect(input).toHaveValue(`${text}\n`);
});

test("record completion takes arrows and Enter before history or sending", async ({ page, request }) => {
  const tag = `input-${crypto.randomUUID()}`;
  for (const suffix of ["alpha", "beta"]) {
    const made = await request.post("/api/inquiries/issue", { data: { title: `${tag}-${suffix}`, idempotency_key: crypto.randomUUID() } });
    expect(made.ok(), await made.text()).toBe(true);
  }
  const input = await open(page, await conversation(page));
  let sends = 0;
  page.on("request", (request) => { if (request.method() === "POST" && request.url().includes("/api/chats")) sends += 1; });
  await input.fill(`@${tag}`);
  const list = page.getByRole("listbox", { name: "Records" });
  await expect(list.getByRole("option")).toHaveCount(2);
  const first = await list.getByRole("option").nth(0).getAttribute("title");
  const second = await list.getByRole("option").nth(1).getAttribute("title");
  await input.press("ArrowDown");
  await expect(list.getByRole("option").nth(1)).toHaveAttribute("aria-selected", "true");
  await expect(input).toHaveValue(`@${tag}`);
  await input.press("ArrowUp");
  await expect(list.getByRole("option").nth(0)).toHaveAttribute("aria-selected", "true");
  await input.press("Enter");
  await expect(input).toHaveValue(`${first} `);
  await expect(list).toHaveCount(0);
  expect(sends).toBe(0);
  await input.fill(`@${tag}`);
  await expect(list.getByRole("option")).toHaveCount(2);
  await input.press("ArrowDown");
  await input.press("Tab");
  await expect(input).toHaveValue(`${second} `);
  await expect(input).toBeFocused();
  expect(sends).toBe(0);
});

test("record kind prefixes match case-insensitively without opening their list first", async ({ page, request }) => {
  const made = await request.post("/api/inquiries/issue", { data: { title: "Agenda", idempotency_key: crypto.randomUUID() } });
  expect(made.ok(), await made.text()).toBe(true);
  const input = await open(page, await conversation(page));
  const list = page.getByRole("listbox", { name: "Records" });
  const refs: string[][] = [];
  for (const query of ["@Ag", "@ag", "@AG"]) {
    await input.fill(query);
    await expect(list.getByRole("option").filter({ has: page.locator(".ref-t", { hasText: /^AgentSession#/ }) }).first()).toBeVisible();
    refs.push(await list.getByRole("option").evaluateAll((options) => options.map((option) => option.getAttribute("title")!)));
  }
  expect(refs[1]).toEqual(refs[0]);
  expect(refs[2]).toEqual(refs[0]);
  expect(refs[0]!.some((ref) => ref.startsWith("Issue#"))).toBe(true);
  await input.fill("@AgentSession");
  await expect(list.getByRole("option").first()).toBeVisible();
  const fullKindRefs = await list.getByRole("option").evaluateAll((options) => options.map((option) => option.getAttribute("title")!));
  expect(fullKindRefs.every((ref) => ref.startsWith("AgentSession#"))).toBe(true);
});

test("completion follows the caret horizontally and onto the next line", async ({ page, request }) => {
  const made = await request.post("/api/inquiries/issue", { data: { title: "Caret suggestion", idempotency_key: crypto.randomUUID() } });
  expect(made.ok(), await made.text()).toBe(true);
  const input = await open(page, await conversation(page));
  await input.fill("@");
  const list = page.getByRole("listbox", { name: "Records" });
  await expect(list).toBeVisible();
  const first = await list.boundingBox();
  expect(first).not.toBeNull();
  await input.fill("hello @");
  await expect.poll(async () => (await list.boundingBox())!.x).toBeGreaterThan(first!.x + 10);
  const inline = await list.boundingBox();
  await input.fill("hello\n@");
  await expect.poll(async () => (await list.boundingBox())!.y).toBeGreaterThan(inline!.y + 5);
  await expect.poll(async () => (await list.boundingBox())!.x).toBeLessThan(inline!.x - 10);
});

test("the list opens at the caret inside the box's border and padding", async ({ page }) => {
  const input = await open(page, await conversation(page));
  await input.evaluate((element: HTMLTextAreaElement) => { element.style.border = "10px solid"; });
  await input.fill("@");
  const list = page.getByRole("listbox", { name: "Records" });
  await expect(list).toBeVisible();
  const [box, menu] = [(await input.boundingBox())!, (await list.boundingBox())!];
  const inset = await input.evaluate((element: HTMLTextAreaElement) => element.clientLeft + parseFloat(getComputedStyle(element).paddingLeft));
  // The caret after "@" is past the border, the padding and the "@" itself.
  expect(menu.x - box.x).toBeGreaterThan(inset);
  expect(menu.x - box.x).toBeLessThan(inset + 20);
  const bottom = menu.y + menu.height - box.y;
  await input.evaluate((element: HTMLTextAreaElement) => {
    element.style.borderTopWidth = "20px";
    window.dispatchEvent(new Event("resize"));
  });
  await expect.poll(async () => {
    const box = (await input.boundingBox())!;
    const menu = (await list.boundingBox())!;
    return menu.y + menu.height - box.y;
  }).toBeCloseTo(bottom + 10, 1);
});

test("a caret moved by script moves the list with it", async ({ page }) => {
  const input = await open(page, await conversation(page));
  const list = page.getByRole("listbox", { name: "Records" });
  await input.fill("@ and then some more words @");
  await expect(list).toBeVisible();
  const far = (await list.boundingBox())!.x;
  await input.evaluate((element: HTMLTextAreaElement) => element.setSelectionRange(1, 1));
  await expect.poll(async () => (await list.boundingBox())?.x ?? far).toBeLessThan(far - 50);
});

test("an edited recall goes back to the unfinished draft on Down", async ({ page }) => {
  const input = await open(page, await conversation(page));
  await input.fill("Unfinished draft");
  await caret(input, 0);
  await input.press("ArrowUp");
  await expect(input).toHaveValue("My latest question");
  await caret(input, "My latest question".length);
  await input.pressSequentially(" edited");
  await expect(input).toHaveValue("My latest question edited");
  await input.press("ArrowDown");
  await expect(input).toHaveValue("Unfinished draft");
});

test("station drafts remain independent across switching and reload", async ({ page, request }) => {
  const first = await conversation(page);
  const second = await conversation(page);
  const input = await open(page, second);
  await input.fill("Second station draft");
  await switchChat(page, first.name);
  await enableInput(page, first);
  await expect(input).toHaveValue("");
  await input.fill("First station draft\nStill unfinished");
  await switchChat(page, second.name);
  await expect(input).toHaveValue("Second station draft");
  await expect.poll(async () => {
    const { preferences } = await (await request.get("/api/me/profile")).json();
    return [preferences.drafts[`chat:${first.id}`]?.text, preferences.drafts[`chat:${second.id}`]?.text];
  }).toEqual(["First station draft\nStill unfinished", "Second station draft"]);
  await page.reload();
  await expect(input).toHaveValue("Second station draft");
  await switchChat(page, first.name);
  await expect(input).toHaveValue("First station draft\nStill unfinished");
});
