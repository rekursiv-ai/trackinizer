import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { resetCanvas } from "./canvasState";
import { expect, test } from "./fixtures";

// axe's WCAG 2.0 and 2.1 A and AA rules (the a11y spec's) on the default layout:
// the canvas, with Chat beside a list and a detail, with a conversation open and
// its History menu open, and with the partner picker open, at 1280 and 390 px,
// in both themes. The conversation and the sessions are served by the test, since
// no partner runs on the e2e server.

test.use({ canvas: true });

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const SIZES = [{ width: 1280, height: 1600 }, { width: 390, height: 2400 }] as const;

test.afterEach(async ({ request }) => {
  await resetCanvas(request);
});

async function audit(page: Page, theme: string, what: string) {
  await expect(page.locator("html"), "the theme applied").toHaveCSS("color-scheme", theme);
  const { violations } = await new AxeBuilder({ page }).withTags(WCAG).analyze();
  const found = violations.flatMap((violation) =>
    violation.nodes.map((node) => `${violation.id} at ${node.target.join(" ")}: ${node.failureSummary?.replace(/\s+/g, " ")}`),
  );
  expect.soft(found, `axe violations on ${what}`).toEqual([]);
}

for (const theme of ["dark", "light"] as const) {
  for (const size of SIZES) {
    test.describe(`${theme} theme, ${size.width} px`, () => {
      test.use({ viewport: size });
      test.beforeEach(async ({ page }) => {
        await page.addInitScript((choice) => {
          try {
            localStorage.setItem("trackinizer.theme", choice);
          } catch {
            // about:blank has no storage.
          }
        }, theme);
        await page.emulateMedia({ reducedMotion: "reduce" });
      });

      /** The conversation the test serves, and the one Issue the pages show. */
      async function seed(page: Page, request: import("@playwright/test").APIRequestContext) {
        const tag = String(crypto.getRandomValues(new Uint32Array(1))[0]);
        const issue = await (await request.post("/api/inquiries/issue", {
          data: { title: `Canvas a11y ${tag}`, idempotency_key: crypto.randomUUID() },
        })).json();
        const workspace = await (await request.post("/api/workspaces")).json();
        const chat = "0b1f6f3e-6c1e-4d3a-9a55-3a1c2f7d9a10";
        const created = "2026-10-03T10:00:00.000000Z";
        const session = "0b1f6f3e-6c1e-4d3a-9a55-3a1c2f7d9a30";
        const record = (idx: number, kind: string, payload: { [field: string]: unknown }) => ({
          idx, kind, payload, text: "", context_id: null, timestamp: created, model: null, ciphertext: null,
        });
        const records = [
          record(0, "AgentToAgentMessage", { sender: "ada@example.com", content: "What changed this week?" }),
          record(1, "AssistantMessage", { content: `Two things: **${tag}** and a [link](#/lookup/${issue.id}).\n\n- one\n- two` }),
        ];
        await page.route(`**/api/sessions/${session}/parts`, (route) => route.fulfill({ json: {
          parts: [{ part: 0, name: "chat.jsonl", format: "sagent", records: records.length, metadata: {}, ir_id: "ir" }],
        } }));
        await page.route(`**/api/sessions/${session}/records**`, (route) => route.fulfill({ json: { records } }));
        await page.route(`**/api/chats/${chat}`, (route) => route.fulfill({ json: {
          conversation_id: chat, session_id: session, title: "What changed", account: "ada@example.com", live: true,
          forks: 0, forked_from: null, forks_on_typing: false,
        } }));
        await page.route("**/api/chats", (route) => route.fulfill({ json: [
          { conversation_id: chat, session_id: session, title: "What changed", account: "ada@example.com", modified: created },
        ] }));
        await page.addInitScript(([key, id]) => {
          try {
            localStorage.setItem(key!, id!);
          } catch {
            // about:blank has no storage.
          }
        }, [`trackinizer.v2.chat.${workspace.id}`, chat]);
        return { issue: issue.id as string };
      }

      test("a list and a detail, with Chat beside them", async ({ page, request }) => {
        const { issue } = await seed(page, request);
        await page.goto("/app/#/list/Issue");
        await expect(page.getByRole("region", { name: "Chat" })).toBeVisible();
        await expect(page.locator("a.row").first()).toBeVisible();
        await audit(page, theme, `a list with Chat, ${size.width} px`);
        await page.goto(`/app/#/lookup/${issue}`);
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await audit(page, theme, `a detail with Chat, ${size.width} px`);
      });

      test("Chat with a conversation open, and its History menu open", async ({ page, request }) => {
        const { issue } = await seed(page, request);
        await page.goto(`/app/#/lookup/${issue}`);
        await expect(page.getByText("Two things:")).toBeVisible();
        await audit(page, theme, `a conversation, ${size.width} px`);
        await page.getByRole("button", { name: "History" }).click();
        await expect(page.getByRole("menuitemradio", { name: /What changed/ })).toBeVisible();
        await audit(page, theme, `the History menu, ${size.width} px`);
      });
    });
  }
}

// The control: the audit above would see a violation in Chat's panel were there one.
test("the audit finds an unnamed button put inside Chat", async ({ page }) => {
  await page.goto("/app/#/list/Issue");
  await expect(page.getByRole("region", { name: "Chat" })).toBeVisible();
  await page.evaluate(() => document.querySelector(".chat-panel")!.append(document.createElement("button")));
  const { violations } = await new AxeBuilder({ page }).withTags(WCAG).analyze();
  expect(violations.map((violation) => violation.id)).toContain("button-name");
});
