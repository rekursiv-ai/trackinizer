import type { Locator, Page } from "@playwright/test";
import { resetCanvas } from "./canvasState";
import { expect, streamOpened, test } from "./fixtures";

// The default path: the canvas is on, with Chat beside the page.
test.use({ canvas: true });

// Chat in its two styles over the same conversation: a user line and an answer
// with each markdown shape the Terminal style colours (docs/retro_ui.md). The
// test stands in for the assistant by writing the conversation's session
// records, as auto-highlight.spec.ts does.

const ANSWER = [
  "# Heading one", "", "## Heading two", "", "### Heading three", "",
  "Some `inline code`, a [link](https://example.com/page) and https://example.com/bare.", "",
  "```py", "def f(x):", "    return \"a\" + str(x)  # note", "```", "",
  "3. third", "4. fourth", "", "> quoted words", "",
  "| name | n |", "|---|---|", "| alpha | 1 |",
].join("\n");

test.beforeEach(async ({ request }) => {
  await resetCanvas(request);
});

async function openConversation(page: Page, style: "default" | "terminal"): Promise<string> {
  const { request } = page;
  const { email } = await (await request.get("/api/me/profile")).json();
  const started = await request.post("/api/sessions/start", { data: { cli: "claude", title: "E2E chat style" } });
  expect(started.ok(), await started.text()).toBe(true);
  const { id: session } = await started.json();
  const file = { name: "chat.jsonl", irId: crypto.randomUUID() };
  const turns = [{ kind: "AgentToAgentMessage", content: "Show me everything." }, { kind: "AssistantMessage", content: ANSWER }];
  const records = turns.map(({ kind, content }, idx) => ({ idx, kind, payload: { content, sender: email }, text: content }));
  const manifest = { name: file.name, metadata: {}, ir_id: file.irId, format: "claude", records: records.length };
  const appended = await request.post(`/api/sessions/${session}/records`, { data: { name: file.name, manifest, records } });
  expect(appended.ok(), await appended.text()).toBe(true);
  const conversation = crypto.randomUUID();
  await page.route(`**/api/chats/${conversation}`, (route) => route.fulfill({ json: {
    conversation_id: conversation, session_id: session, title: "E2E", account: email, live: true, forks: 0, forked_from: null, forks_on_typing: false, shared_with: [], may_share: false, may_delete: false,
  } }));
  // A new station for that chat, which is then the one changed last, so the tab opens on it.
  expect((await request.post(`/api/workspaces/for-chat/${conversation}`)).ok()).toBe(true);
  await page.addInitScript((choice) => localStorage.setItem("trackinizer.chat.style", choice!), style);
  return email;
}

async function open(page: Page, style: "default" | "terminal"): Promise<Locator> {
  const email = await openConversation(page, style);
  await page.setViewportSize({ width: 1400, height: 2400 });
  const subscribed = streamOpened(page);
  await page.goto("/app/#/list/Issue");
  await subscribed;
  const panel = page.locator(".chat-panel");
  await expect(panel.getByText("Heading three")).toBeVisible();
  expect(email).toContain("@");
  return panel;
}

const colour = (locator: Locator, property = "color") => locator.evaluate((element, p) => getComputedStyle(element).getPropertyValue(p), property);

test("the Terminal style draws Chat as the GNOME terminal profile, from the palette", async ({ page }) => {
  // A second station, older than the chat's, for the menu's highlight to move to.
  expect((await page.request.post("/api/workspaces/new")).ok()).toBe(true);
  const panel = await open(page, "terminal");
  expect(await colour(panel)).toBe("rgb(0, 255, 0)");
  expect(await colour(panel, "background-color")).toBe("rgb(0, 0, 0)");
  expect(await colour(panel, "font-size")).toBe("13px");
  expect(await colour(panel, "font-family")).toContain("DejaVu Sans Mono");
  // Chat's width is 80 characters of the terminal's font, so the column that sets it uses that font.
  const column = page.locator(".chat-column");
  expect(await colour(column, "font-size")).toBe("13px");
  expect(await colour(column, "font-family")).toContain("DejaVu Sans Mono");
  expect(await colour(panel.locator(".md h3"), "text-decoration-line")).toBe("underline");
  expect(await colour(panel.locator(".md h3"), "text-align")).toBe("left");
  expect(await colour(panel.locator(".md h4"))).toBe("rgb(163, 71, 186)");
  expect(await colour(panel.locator(".md h5"))).toBe("rgb(192, 97, 203)");
  expect(await colour(panel.locator(".md p code"))).toBe("rgb(51, 199, 222)");
  expect(await colour(panel.locator(".md p code"), "background-color")).toBe("rgb(23, 20, 33)");
  expect(await colour(panel.locator(".md pre code"))).toBe("rgb(42, 161, 179)");
  expect(await colour(panel.locator(".md pre"), "background-color")).toBe("rgb(23, 20, 33)");
  expect(await colour(panel.locator(".md blockquote"))).toBe("rgb(163, 71, 186)");
  expect(await colour(panel.locator(".md a", { hasText: "link" }))).toBe("rgb(42, 123, 222)");
  expect(await colour(panel.locator(".md td").first(), "border-top-color")).toBe("rgb(42, 161, 179)");
  expect(await panel.locator(".md ol").getAttribute("start")).toBe("3");
  expect(await colour(panel.locator(".chat-line-body").first().locator(".chat-prompt > strong").first())).toBe("rgb(51, 209, 122)");
  // The handle and host in bold bright green, the station in bold bright yellow, as a shell shows its git branch.
  await expect(panel.locator(".chat-prompt")).toHaveText(/^no-auth@127\.0\.0\.1:\[unnamed-workstream#\d+\]\$ $/);
  expect(await colour(panel.locator(".chat-prompt-station").first())).toBe("rgb(233, 173, 12)");
  expect(await colour(panel.locator(".chat-prompt-station").first(), "font-weight")).toBe("700");
  expect(await colour(panel.locator(".md pre .hljs-keyword").first())).toBe("rgb(246, 97, 81)");
  expect(await colour(panel.locator(".md-copy").first(), "display")).toBe("none");
  expect(await colour(panel.locator(".chat-line-assistant"), "border-radius")).toBe("0px");
  // The station menu lies over the conversation, so it keeps the terminal's black, and the open station reads in yellow.
  await panel.locator(".station-name").click();
  const menu = panel.locator(".station-menu");
  expect(await colour(menu, "background-color")).toBe("rgb(0, 0, 0)");
  // The menu opens on the open station: highlighted, it reads black on green.
  const current = menu.locator(".station-option[aria-current=\"true\"]");
  expect([await colour(current), await colour(current, "background-color")]).toEqual(["rgb(0, 0, 0)", "rgb(0, 255, 0)"]);
  // With the highlight moved off it, the open station reads in yellow.
  await page.keyboard.press("ArrowDown");
  await expect(current).not.toHaveClass(/station-option-active/);
  expect(await colour(current)).toBe("rgb(233, 173, 12)");
});

test("the Default style draws Chat as it always has: no prompt, no terminal colours", async ({ page }) => {
  const panel = await open(page, "default");
  await expect(panel.locator(".chat-prompt")).toHaveCount(0);
  expect(await colour(page.locator(".chat-column"), "font-size")).toBe("13px");
  expect(await colour(panel, "background-color")).not.toBe("rgb(0, 0, 0)");
  expect(await colour(panel.locator(".chat-line-assistant"), "border-radius")).not.toBe("0px");
});
