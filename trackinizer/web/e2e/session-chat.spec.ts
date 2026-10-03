import type { APIRequestContext, Locator, Page } from "@playwright/test";
import { expect, failedResource, test } from "./fixtures";

// Messaging a live AgentSession from the foot of its transcript. The test stands
// in for the session's `trax run`: a drain that returns at once (`GET
// .../inbound?wait_sec=0`) marks the session polled and reads what the page
// sent, and the agent's turns are appended as its capture appends them. The
// page itself never drains.

/** Start a session as `trax run` does, with one captured turn; its id, seq, owner and file. */
async function startSession(request: APIRequestContext, title: string) {
  const started = await request.post("/api/sessions/start", { data: { cli: "claude", title } });
  expect(started.ok(), await started.text()).toBe(true);
  const { id } = await started.json();
  const file = { name: "chat.jsonl", irId: crypto.randomUUID() };
  await capture(request, id, file, 0, [{ kind: "UserMessage", content: "Start on the flaky test." }]);
  const { self } = await (await request.get(`/api/web/get/${id}`)).json();
  return { id: id as string, seq: self.seq as number, owner: self.owner as string, file };
}

/** Append `turns` from record `from` on, as the session's capture does. */
async function capture(
  request: APIRequestContext,
  id: string,
  file: { name: string; irId: string },
  from: number,
  turns: readonly { kind: string; content: string }[],
) {
  const records = turns.map(({ kind, content }, k) => ({ idx: from + k, kind, payload: { content }, text: content }));
  const manifest = { name: file.name, metadata: {}, ir_id: file.irId, format: "claude", records: from + turns.length };
  const appended = await request.post(`/api/sessions/${id}/records`, { data: { name: file.name, manifest, records } });
  expect(appended.ok(), await appended.text()).toBe(true);
}

/** Take what is queued for the session, as its `trax run` does; the first take marks it polled. */
async function drain(request: APIRequestContext, id: string): Promise<{ text: string; source: string }[]> {
  const drained = await request.get(`/api/sessions/${id}/inbound?wait_sec=0`);
  expect(drained.ok(), await drained.text()).toBe(true);
  return (await drained.json()).messages;
}

/** Open the session's detail once the live stream is connected; its transcript section. */
async function openSession(page: Page, seq: number): Promise<Locator> {
  const subscribed = page.waitForResponse((response) => response.url().includes("/api/web/subscribe"));
  await page.goto(`/app/#/ref/AgentSession/${seq}`);
  await subscribed;
  return page.locator('[data-section="transcript"]');
}

test("a message from a live session's transcript reaches its trax run, and the reply shows live", async ({ page }) => {
  const { email } = await (await page.request.get("/api/me/profile")).json();
  const session = await startSession(page.request, "E2E session chat");
  expect(await drain(page.request, session.id)).toEqual([]);
  const transcript = await openSession(page, session.seq);
  const box = transcript.getByRole("textbox", { name: "Message" });
  await box.fill("Please summarize what you changed.");
  await box.press("Enter");
  await expect(transcript.locator(".composer-receipt")).toHaveText(`Queued for ${session.owner}`);
  await expect(box).toHaveValue("");
  expect(await drain(page.request, session.id)).toEqual([
    expect.objectContaining({ text: "Please summarize what you changed.", source: email }),
  ]);
  // `trax run` typed it into the CLI, which logged it and answered.
  await capture(page.request, session.id, session.file, 1, [
    { kind: "UserMessage", content: `${email}: Please summarize what you changed.` },
    { kind: "AssistantMessage", content: "I replaced time.time() with time.monotonic()." },
  ]);
  await expect(transcript.locator('[data-part="0"] [data-idx="2"]')).toContainText("I replaced time.time() with time.monotonic().");
});

test("a session that ends while its detail is open takes no more messages", async ({ page }) => {
  const session = await startSession(page.request, "E2E session chat, ended");
  await drain(page.request, session.id);
  const transcript = await openSession(page, session.seq);
  await expect(transcript.getByRole("textbox", { name: "Message" })).toBeVisible();
  const ended = await page.request.post(`/api/sessions/${session.id}/end`, { data: {} });
  expect(ended.ok(), await ended.text()).toBe(true);
  await expect(transcript.getByText("The session has ended, so it takes no messages.")).toBeVisible();
  await expect(transcript.getByRole("textbox", { name: "Message" })).toHaveCount(0);
  // And the server refuses a message sent anyway.
  expect((await page.request.post(`/api/sessions/${session.id}/inbound`, { data: { text: "Too late." } })).status()).toBe(409);
});

test("a send whose answer is lost, retried, reaches the agent once", async ({ page, allowErrors }) => {
  const session = await startSession(page.request, "E2E session chat, retried");
  await drain(page.request, session.id);
  const keys: string[] = [];
  let lost = false;
  // The first send reaches the server, which queues it, but its answer never reaches the page.
  await page.route(`**/api/sessions/${session.id}/inbound`, async (route) => {
    keys.push(route.request().headers()["idempotency-key"] ?? "");
    if (lost) return route.continue();
    lost = true;
    await route.fetch();
    return route.abort();
  });
  // The browser's own line for the request whose answer was cut.
  allowErrors(failedResource(`/api/sessions/${session.id}/inbound`, "ERR_FAILED"));
  const transcript = await openSession(page, session.seq);
  const box = transcript.getByRole("textbox", { name: "Message" });
  await box.fill("Run the suite again.");
  await box.press("Enter");
  await expect(transcript.getByRole("alert")).toContainText("Could not send");
  await transcript.getByRole("button", { name: "Retry message" }).click();
  await expect(transcript.locator(".composer-receipt")).toHaveText(`Queued for ${session.owner}`);
  expect(keys).toHaveLength(2);
  expect(keys[1]).toBe(keys[0]);
  expect((await drain(page.request, session.id)).map((message) => message.text)).toEqual(["Run the suite again."]);
});

test("a send to a session no trax run is polling says so, and keeps the draft", async ({ page, allowErrors }) => {
  const session = await startSession(page.request, "E2E session chat, unpolled");
  // The browser's own line for the server's 409.
  allowErrors(failedResource(`/api/sessions/${session.id}/inbound`, 409));
  const transcript = await openSession(page, session.seq);
  const box = transcript.getByRole("textbox", { name: "Message" });
  await box.fill("Anyone there?");
  await box.press("Enter");
  await expect(transcript.getByRole("alert")).toHaveText("Not connected: trax run is not polling this session (No active inbound poller).");
  await expect(box).toHaveValue("Anyone there?");
});
