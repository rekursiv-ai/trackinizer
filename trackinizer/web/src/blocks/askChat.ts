// THE ANSWERING SIDE OF THE ASK BAR.
//
// `ui/ask.tsx` has rendered an answer under the question since it was written — a `q`
// line, an `a` line, a `thinking…` while it waits. Nothing ever filled it in: the bar
// fired the question at the chat column and kept a handle to a reply no one wrote, so
// the box cleared on Enter and the reader was left looking at the page they asked about.
//
// This is the writer it was waiting for. The question reaches the agent that composed
// the page, and the reply is read back off its session records and handed to the bar.
// One question, one answer, in place.
//
// It is asked QUIETLY. A question about one node carries that node's composed JSON, and
// posting it as an ordinary chat line filled the column with a machine-readable payload
// and then `Working…` — a second, worse place to read an answer already on the page. The
// line is marked with `CANVAS_ASK` instead and the column leaves it to the canvas.
import { getChatHead, sendChatLine } from "../api/chats";
import { listSessionParts, readSessionRecords, type SessionRecord } from "../api/sessions";
import { CANVAS_ASK } from "../visuals/chatLines";

/** How long to wait for the assistant to open the conversation before giving up. */
const OPEN_TIMEOUT_MS = 30_000;
/**
 * The whole exchange's ceiling: a follow-up that runs longer belongs in the column.
 * Six minutes, because the agent on the other end is the one that composed the page —
 * measured at four, because it reads the graph before it answers a question about it.
 */
const REPLY_TIMEOUT_MS = 360_000;
/** How often the watch reads: two requests a poll, so not so often that it starves itself. */
const POLL_MS = 2_000;
/** How long a message must stand as the agent's last word before it is read as the answer. */
const SETTLE_MS = 6_000;

/** A conversation to ask in: the station, and the thread it already has (null starts one). */
export type AskTarget = {
  readonly workspaceId: string;
  readonly conversationId: string | null;
  /** The route the asker is on, which the server records with the line. */
  readonly page: string | null;
};

/**
 * Ask `question` of the agent on the station and return its reply.
 *
 * Resolves with the text; throws with a sentence the bar can print. The caller decides
 * what to do with a new conversation id — a station that had none now has one, because
 * the fallback had to open the thread to find anyone to ask.
 */
export async function askTheChat(
  target: AskTarget,
  question: string,
  { signal }: { readonly signal?: AbortSignal } = {},
): Promise<{ readonly text: string; readonly conversationId: string }> {
  // There is exactly one way in. Posting straight to the session the run is polling is
  // refused by name: "A science chat takes lines through POST /api/chats, which names the
  // poster and the conversation; a line sent to its session directly has neither." So the
  // question is posted as a line and MARKED as the canvas's, and the column leaves a
  // marked line to the canvas (`visuals/chatLines.ts`). The record is still written, so
  // the thread read back later holds the whole exchange.
  // The mark carries an id for THIS question. The agent is one turn at a time: ask while
  // it is still finishing the last question and its answer to that one lands first, and
  // a watch that takes the next message it sees prints the old answer under the new
  // question — measured, twice, and it reads exactly like a canned reply. The id is how
  // the watch finds where this question was delivered and refuses anything before it.
  const asked = `${CANVAS_ASK} ${crypto.randomUUID().slice(0, 8)}]`;

  // Where the transcript stood BEFORE the question went out. Read after the send, the
  // question is already in the records and the bound steps straight over it, leaving the
  // watch waiting for a delivery it has already passed.
  const open = target.conversationId ? await getChatHead(target.conversationId, { signal }) : null;
  const before = open?.session_id ? await baselineOf(open.session_id, signal) : null;

  const sent = await sendChatLine(
    {
      workspaceId: target.workspaceId,
      text: `${asked} ${question}`,
      conversationId: target.conversationId,
      page: target.page,
      trail: [],
    },
    crypto.randomUUID(),
  );
  const conversationId = sent.conversation_id;

  // The session is there at once when the thread is already open; a new conversation has
  // none until the assistant picks it up, and the server answers 404 the whole time.
  let sessionId = sent.session_id ?? null;
  const openBy = Date.now() + OPEN_TIMEOUT_MS;
  while (sessionId === null) {
    if (signal?.aborted) throw new Error("the question was withdrawn");
    if (Date.now() > openBy) throw new Error("the assistant did not pick the question up");
    await sleep(500, signal);
    const head = await getChatHead(sent.conversation_id, { signal });
    sessionId = head?.session_id ?? null;
  }
  // A thread that had no session has a new one, whose part is new and short, so reading
  // it whole is how the question is found there.
  const start = before !== null && open?.session_id === sessionId ? before : await emptyBaseline(sessionId, signal);
  return { ...(await watch(sessionId, asked, start, signal)), conversationId };
}

/** Where each watched part stands now: the newest, and how far it has been read. */
type Baseline = { readonly from: number; readonly bounds: ReadonlyMap<number, number> };

/** The newest part and its end — everything already written, which this question is not. */
async function baselineOf(sessionId: string, signal?: AbortSignal): Promise<Baseline> {
  const from = newestPartOf(await listSessionParts(sessionId, { signal }));
  return { from, bounds: new Map([[from, lastIdxOf(await records(sessionId, from, -1, signal), -1)]]) };
}

/** The newest part, read from its start: for a session whose transcript is this exchange. */
async function emptyBaseline(sessionId: string, signal?: AbortSignal): Promise<Baseline> {
  const from = newestPartOf(await listSessionParts(sessionId, { signal }));
  return { from, bounds: new Map([[from, -1]]) };
}

/**
 * Read the agent's answer to the question marked `asked` off `sessionId`'s records.
 *
 * Nothing counts until the question itself is seen in the transcript: the agent takes one
 * turn at a time, so a question asked while it is still working is delivered late, and
 * everything it says in between belongs to the question before.
 */
async function watch(
  sessionId: string,
  asked: string,
  start: Baseline,
  signal?: AbortSignal,
): Promise<{ readonly text: string }> {

  // The reply is read off the session's records, not the chat's partial stream. The
  // partial is the SERVER's assistant writing; a station answered by `trax run claude
  // --attach` is a CLI on the other end, and it files `AssistantMessage` records instead
  // — measured: its partial stays `{"seq":0,"partial":null}` for the whole exchange while
  // the records fill up. Reading the wrong one waits forever on a reply already written.
  //
  // And it files them in ITS OWN PART. A chat's session gains a part per run that
  // attaches to it, so the part the question lands in is almost never the part the
  // answer lands in — measured: the question at part 0, the reply at part 3 idx 34,
  // written while a poller pinned to part 0 reported nothing for two minutes. So the
  // watch spans every part from the newest at send time on, and takes up a part that
  // appears mid-wait, which is what an agent attaching to answer looks like from here.
  //
  // Each part's bound is where it stood when the question went out, so only what the
  // agent says AFTER it counts; a part opened later is read whole.
  const { from, bounds } = start;
  const watching = new Map<number, number>(bounds);
  const replyBy = Date.now() + REPLY_TIMEOUT_MS;
  let delivered = false;
  let answer: string | null = null;
  let settledAt = 0;
  for (;;) {
    if (signal?.aborted) throw new Error("the question was withdrawn");
    if (Date.now() > replyBy) throw new Error("the assistant did not answer in time");
    await sleep(POLL_MS, signal);
    // A poll that times out is a slow read, not a failed exchange. The app holds the
    // origin's connections open — an events stream per station, a chat's long poll —
    // and on HTTP/1.1 a read can sit behind them for longer than its own deadline. A
    // throw here would print `Not answered` under a question the agent is mid-answer
    // on, so the poll is dropped and the next one asks again, up to the exchange's
    // own ceiling, which is the only deadline the reader is owed.
    try {
      for (const part of await listSessionParts(sessionId, { signal })) {
        if (part.part >= from && !watching.has(part.part)) watching.set(part.part, -1);
      }
      for (const [part, afterIdx] of [...watching]) {
        const fresh = await records(sessionId, part, afterIdx, signal);
        if (fresh.length === 0) continue;
        watching.set(part, lastIdxOf(fresh, afterIdx));
        // Not every message the agent writes is the answer. It narrates — "reading the
        // brief first", "composing" — and then goes on working, so the FIRST message of
        // a turn is usually a progress line: measured, a reader who asked for one number
        // got "Composing." while the number arrived a minute later, in the column. A
        // message counts as the answer once it has stood as the agent's last word
        // through `SETTLE_MS`; a tool call after it says the turn was not over.
        for (const record of fresh) {
          // Where the question was delivered. Until it is found, every message belongs to
          // whatever the agent was already doing; after it, the turn is this question's.
          // A run that attaches later writes into a newer part, so a higher part counts
          // as after it too.
          if (!delivered) { delivered = textOf(record).includes(asked); continue; }
          if (record.kind === "AssistantMessage" && textOf(record) !== "") {
            answer = textOf(record);
            settledAt = Date.now();
          } else if (record.kind === "ToolCall") {
            answer = null;
          }
        }
      }
    } catch (slow) {
      if (signal?.aborted) throw slow;
    }
    if (answer !== null && Date.now() - settledAt >= SETTLE_MS) return { text: answer };
  }
}

/** One page of a part's newest records; `-1` reads it from the start. */
function records(sessionId: string, part: number, afterIdx: number, signal?: AbortSignal): Promise<SessionRecord[]> {
  return readSessionRecords(sessionId, { part, afterIdx, limit: 200 }, { signal });
}

/** The last part of a session: the one a run attaching now writes into. Part `-1` holds
 *  turns backfilled from the retired event log, so an empty listing starts at 0, not -1. */
function newestPartOf(parts: readonly { readonly part: number }[]): number {
  return parts.reduce((high, p) => (p.part > high ? p.part : high), 0);
}

/** The highest `idx` in a page, or `bound` when the page is empty. */
function lastIdxOf(page: readonly SessionRecord[], bound: number): number {
  return page.reduce((high, r) => (typeof r.idx === "number" && r.idx > high ? r.idx : high), bound);
}

/** What an `AssistantMessage` said, whatever the capture called the field. */
function textOf(record: SessionRecord): string {
  const payload = (record as { payload?: Record<string, unknown> }).payload ?? {};
  const text = payload.text ?? payload.content ?? "";
  return typeof text === "string" ? text.trim() : "";
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => { clearTimeout(timer); reject(new Error("aborted")); }, { once: true });
  });
}
