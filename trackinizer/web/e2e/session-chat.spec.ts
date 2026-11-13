import type { APIRequestContext, Locator, Page } from "@playwright/test";
import { expect, failedResource, test, streamOpened } from "./fixtures";

// Messaging an AgentSession from the foot of its transcript. A line goes to the
// session's agent process (`POST .../inputs`), and this suite runs none, so every
// session here is one no agent serves: its turns are appended as its capture
// appends them.

/** Start a session as the agent runner does, with one captured turn; its id, seq, owner and file. */
async function startSession(request: APIRequestContext, title: string) {
  const started = await request.post("/api/sessions/start", {
    data: { cli: "claude", title },
  });
  expect(started.ok(), await started.text()).toBe(true);
  const { id } = await started.json();
  const file = { name: "chat.jsonl", irId: crypto.randomUUID() };
  await capture(request, id, file, 0, [
    { kind: "UserMessage", content: "Start on the flaky test." },
  ]);
  const { self } = await (await request.get(`/api/web/get/${id}`)).json();
  return {
    id: id as string,
    seq: self.seq as number,
    owner: self.owner as string,
    file,
  };
}

/** Append `turns` from record `from` on, as the session's capture does. */
async function capture(
  request: APIRequestContext,
  id: string,
  file: { name: string; irId: string },
  from: number,
  turns: readonly { kind: string; content: string }[],
) {
  const records = turns.map(({ kind, content }, k) => ({
    idx: from + k,
    kind,
    payload: { content },
    text: content,
  }));
  const manifest = {
    name: file.name,
    metadata: {},
    ir_id: file.irId,
    format: "claude",
    records: from + turns.length,
  };
  const appended = await request.post(`/api/sessions/${id}/records`, {
    data: { name: file.name, manifest, records },
  });
  expect(appended.ok(), await appended.text()).toBe(true);
}

/** Open the session's detail once the live stream is connected; its transcript section. */
async function openSession(page: Page, seq: number): Promise<Locator> {
  const subscribed = streamOpened(page);
  await page.goto(`/trax/ref/AgentSession/${seq}`);
  await subscribed;
  return page.locator('[data-section="transcript"]');
}

/** A line as the page sends one. */
const line = (text: string) => ({
  kind: "UserMessage",
  payload: { "py/object": "UserMessage", content: text, extra: {} },
});

test("a session that ends while its detail is open takes no more messages", async ({
  page,
}) => {
  const session = await startSession(page.request, "E2E session chat, ended");
  const transcript = await openSession(page, session.seq);
  await expect(transcript.getByRole("textbox", { name: "Message" })).toBeVisible();
  const ended = await page.request.post(`/api/sessions/${session.id}/end`, {
    data: {},
  });
  expect(ended.ok(), await ended.text()).toBe(true);
  await expect(
    transcript.getByText("The session has ended, so it takes no messages."),
  ).toBeVisible();
  await expect(transcript.getByRole("textbox", { name: "Message" })).toHaveCount(0);
  // And the server refuses a message sent anyway: no agent serves it.
  expect(
    (
      await page.request.post(`/api/sessions/${session.id}/inputs`, {
        data: line("Too late."),
      })
    ).status(),
  ).toBe(409);
});

test("a send to a session no agent serves says so, and keeps the draft", async ({
  page,
  allowErrors,
}) => {
  const session = await startSession(page.request, "E2E session chat, no agent");
  // The browser's own line for the server's 409.
  allowErrors(failedResource(`/api/sessions/${session.id}/inputs`, 409));
  const transcript = await openSession(page, session.seq);
  const box = transcript.getByRole("textbox", { name: "Message" });
  await box.fill("Anyone there?");
  await box.press("Enter");
  await expect(transcript.getByRole("alert")).toHaveText(
    "No agent is running in this session, so nothing was sent.",
  );
  await expect(box).toHaveValue("Anyone there?");
});
