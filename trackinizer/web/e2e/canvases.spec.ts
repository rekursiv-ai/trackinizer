import type { Page } from "@playwright/test";
import { expect, failedResource, test } from "./fixtures";
import { resetCanvas } from "./canvasState";

test.use({ canvas: true });

test.beforeEach(async ({ request }) => {
  await resetCanvas(request);
});

async function open(page: Page): Promise<void> {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/trax/list/Issue");
  await expect(page.locator(".chat-column").getByRole("region", { name: "Chat" })).toBeVisible();
}

async function rename(page: Page, name: string): Promise<void> {
  const title = page.locator(".chat-name-button");
  await title.click();
  const field = page.getByRole("textbox", { name: "Chat name" });
  await field.fill(name);
  await field.press("Enter");
  await expect(title).toHaveAttribute("aria-label", name);
}

test("a duplicate chat name shows the 409 note and keeps the name field open", async ({ page, allowErrors }) => {
  allowErrors(failedResource("/api/canvases/", 409));
  await open(page);
  const first = `chat-${crypto.randomUUID().slice(0, 8)}`;
  await rename(page, first);
  await page.locator(".chat-name-button").click();
  await page.getByRole("button", { name: "+ New chat" }).click();
  await expect(page.locator(".chat-name-button")).toHaveAttribute("aria-label", "Scratch chat");
  await page.locator(".chat-name-button").click();
  const field = page.getByRole("textbox", { name: "Chat name" });
  await field.fill(first);
  await field.press("Enter");
  await expect(page.getByRole("status")).toHaveText("You already have a chat with that name");
  await expect(field).toBeVisible();
});
