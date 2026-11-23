import { beforeEach, expect, test, vi } from "vitest";
import { getChatHead, sendChatLine } from "../api/chats";
import { listSessionParts, readSessionRecords, type SessionRecord } from "../api/sessions";
import { CANVAS_ASK } from "../visuals/chatLines";
import { askTheChat } from "./askChat";

vi.mock("../api/chats", async (original) => ({
  ...(await original<typeof import("../api/chats")>()),
  sendChatLine: vi.fn(),
  getChatHead: vi.fn(),
}));
vi.mock("../api/sessions", async (original) => ({
  ...(await original<typeof import("../api/sessions")>()),
  listSessionParts: vi.fn(),
  readSessionRecords: vi.fn(),
}));

const SESSION = "2f122d3d-1270-4d07-b5c4-94ebbc2e9424";
const CONVERSATION = "c49528a3-36ce-4f3e-9feb-ff22d087f158";
const TARGET = { workspaceId: "c48eb08d-3b1b-45a7-bea8-75582d6fd99f", conversationId: CONVERSATION, page: "#/blocks" };

/** A record as the records route returns one: `content` is where a claude capture puts it. */
function said(idx: number, content: string): SessionRecord {
  return { idx, kind: "AssistantMessage", payload: { content } } as unknown as SessionRecord;
}

/** One tool call, which says the turn that wrote the message before it was not over. */
function tool(idx: number): SessionRecord {
  return { idx, kind: "ToolCall", payload: {} } as unknown as SessionRecord;
}

/** Stands in for the question's text until the send that writes it has happened. */
const ASKED = "$asked";

/** The capture of the question reaching the agent, under the mark this send wrote. */
function delivery(idx: number): SessionRecord {
  return { idx, kind: "UserMessage", payload: { content: ASKED } } as unknown as SessionRecord;
}

/** What the send actually posted — only knowable once `askTheChat` has called it. */
function sentText(): string {
  const [[line]] = vi.mocked(sendChatLine).mock.calls;
  return line.text;
}

/** The delivery record with the real posted text in it, for a mock that builds its own pages. */
function asDelivered(record: SessionRecord): SessionRecord {
  return { ...record, payload: { content: sentText() } } as SessionRecord;
}

/** Serve one page of records per read, in the order the capture wrote them. */
function pages(...turn: SessionRecord[][]) {
  const queue = [...turn];
  vi.mocked(readSessionRecords).mockImplementation(async () =>
    (queue.shift() ?? []).map((r) =>
      (r.payload as { content?: string })?.content === ASKED
        ? ({ ...r, payload: { content: sentText() } } as SessionRecord)
        : r,
    ),
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(sendChatLine).mockResolvedValue({ conversation_id: CONVERSATION, workspace_id: TARGET.workspaceId, session_id: SESSION });
  vi.mocked(getChatHead).mockResolvedValue({ session_id: SESSION } as never);
  vi.mocked(listSessionParts).mockResolvedValue([{ part: 0 } as never]);
});

test("the question is posted marked for the canvas, so the chat column leaves it alone", async () => {
  // There is no side channel: the server refuses a line sent to the session directly.
  // The line is posted and marked, and `visuals/chatLines.ts` drops a marked line.
  pages([], [delivery(1), said(2, "the clamp arm.")]);

  await expect(askTheChat(TARGET, "which arm?")).resolves.toMatchObject({ text: "the clamp arm." });
  const [[line]] = vi.mocked(sendChatLine).mock.calls;
  expect(line.text).toMatch(/^\[canvas [0-9a-f]{8}\] which arm\?$/);
  expect(line.text.startsWith(CANVAS_ASK)).toBe(true);
}, 20_000);

test("an answer the agent was already writing is not shown under the next question", async () => {
  // The agent takes one turn at a time. Asking while it is still finishing the previous
  // question delivers this one late, and its reply to the OLD question lands first —
  // which, read as this question's, looks exactly like a canned answer. Nothing counts
  // until this question itself is seen in the transcript.
  pages(
    [],                                                       // the baseline, before the send
    [said(1, "the answer to the question BEFORE this one")],  // the turn already running
    [tool(2)],
    [delivery(3)],                                            // now the agent is free
    [said(4, "the answer to THIS one")],
  );

  await expect(askTheChat(TARGET, "and now?")).resolves.toMatchObject({ text: "the answer to THIS one" });
}, 30_000);

test("the reply is read from the part the attached agent writes, not the part the question landed in", async () => {
  // A chat's session gains a part per run that attaches to it, and the agent files its
  // turn in ITS part. A read pinned to part 0 reported nothing while the answer sat in 3.
  vi.mocked(listSessionParts).mockResolvedValue([0, 1, 2, 3].map((part) => ({ part }) as never));
  let polls = 0;
  vi.mocked(readSessionRecords).mockImplementation(async (_s, { part }) => {
    if (part !== 3) return [];
    polls += 1;
    return polls === 2 ? [asDelivered(delivery(34)), said(35, "2 + 2 is 4.")] : [];
  });

  const reply = await askTheChat(TARGET, "what is 2+2?");
  expect(reply.text).toBe("2 + 2 is 4.");
  expect(reply.conversationId).toBe(CONVERSATION);
}, 20_000);

test("a part opened after the question is read whole: that is an agent attaching to answer", async () => {
  // Nothing was attached when the question went out, so the part that answers does not
  // exist yet. It appears mid-wait, and its records start before any bound we hold.
  let attached = false;
  vi.mocked(listSessionParts).mockImplementation(async () =>
    (attached ? [0, 1] : [0]).map((part) => ({ part }) as never),
  );
  vi.mocked(readSessionRecords).mockImplementation(async (_s, { part, afterIdx }) => {
    if (part === 0) { attached = true; return []; }
    return part === 1 && afterIdx < 0 ? [asDelivered(delivery(0)), said(1, "answered from the new part")] : [];
  });

  await expect(askTheChat(TARGET, "why?")).resolves.toMatchObject({ text: "answered from the new part" });
}, 20_000);

test("a poll that times out is retried, not printed as a failure under the question", async () => {
  // The app keeps this origin's connections busy, so a read can outlast its own deadline
  // while the agent is mid-answer. Giving up on the first one would say "Not answered"
  // about an answer that arrives two seconds later.
  let reads = 0;
  vi.mocked(readSessionRecords).mockImplementation(async () => {
    reads += 1;
    if (reads === 2) throw new Error("No response within 15 s.");
    return reads === 3 ? [asDelivered(delivery(1)), said(2, "late but answered")] : [];
  });

  await expect(askTheChat(TARGET, "still there?")).resolves.toMatchObject({ text: "late but answered" });
}, 25_000);

test("a message the agent follows with more work is not the answer; its last word is", async () => {
  // The composer narrates before it works. Taking its first message put "Composing."
  // under a question that asked for one number, while the number itself — written a
  // minute later, after three more tool calls — only ever appeared in the chat column.
  pages(
    [],
    [delivery(0), said(1, "Composing. Reading the brief first.")],
    [tool(2)],
    [said(3, "0.30 — the gap between the paper edges at 0.60 and the clamp arm at 0.90.")],
  );

  await expect(askTheChat(TARGET, "one number?")).resolves.toMatchObject({
    text: "0.30 — the gap between the paper edges at 0.60 and the clamp arm at 0.90.",
  });
}, 25_000);
