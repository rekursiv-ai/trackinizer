import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { ChatPartial, ChatPartialState } from "../api/chats";
import { stubFetch } from "../api/testing";
import { type ChatBubble, nextBubble, useChatPartial } from "./chatPartial";

afterEach(() => {
  // Unmounted, a hook aborts its read: one left mounted would read through the next test's fetch.
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const writing = (turn: string, text: string, done = false): ChatPartial => ({ turn, text, done });

test("no partial shows no bubble", () => {
  expect(nextBubble({ turn: "t1", text: "so far" }, null, null)).toBeNull();
});

test("a partial of the turn whose answer is stored shows no bubble", () => {
  expect(nextBubble(null, writing("t1", "late"), "t1")).toBeNull();
});

test("a finished turn's text shows only where its bubble already is", () => {
  expect(nextBubble(null, writing("t1", "the answer", true), null)).toBeNull();
  expect(nextBubble({ turn: "t0", text: "before" }, writing("t1", "the answer", true), null)).toBeNull();
  expect(nextBubble({ turn: "t1", text: "the ans" }, writing("t1", "the answer", true), null)).toEqual({ turn: "t1", text: "the answer" });
});

test("any other partial shows its text", () => {
  expect(nextBubble(null, writing("t2", "It is"), "t1")).toEqual({ turn: "t2", text: "It is" });
});

/**
 * The server's partial of session `s1`, which a test changes as the assistant would
 * (any other session holds none); a read whose `after` is the held seq waits for a
 * change, as the server holds it.
 */
function server(answer: (request: Request) => Response | null = () => null) {
  let state: ChatPartialState = { seq: 0, partial: null };
  let wake = () => {};
  const sent = stubFetch(async (request) => {
    const refused = answer(request);
    if (refused) return refused;
    // Even a read answered at once takes a turn of the event loop, as a real one does, so a
    // client that asks again at once fails its test instead of starving it.
    await new Promise((resolve) => setTimeout(resolve, 0));
    const url = new URL(request.url);
    const held = () => (url.pathname === "/api/sessions/s1/partial" ? state : { seq: 0, partial: null });
    while (held().seq === Number(url.searchParams.get("after"))) {
      await new Promise<void>((resolve, reject) => {
        wake = resolve;
        request.signal.addEventListener("abort", () => reject(request.signal.reason));
      });
    }
    return Response.json(held());
  });
  return {
    sent,
    write: (partial: ChatPartial | null) => act(async () => {
      state = { seq: state.seq + 1, partial };
      wake();
    }),
  };
}

type Props = { readonly sessionId: string | null; readonly active: boolean; readonly answers: number };

/** The hook under `props`, and every value it rendered, each with the answers it was rendered under. */
function follow(props: Props) {
  const rendered: [number, ChatBubble | null][] = [];
  const hook = renderHook(({ sessionId, active, answers }: Props) => {
    const bubble = useChatPartial(sessionId, { active, answers });
    rendered.push([answers, bubble]);
    return bubble;
  }, { initialProps: props });
  return { ...hook, rendered };
}

test("nothing is read while no answer is owed; then each read asks after the seq the last one answered", async () => {
  const { sent, write } = server();
  const { result, rerender } = follow({ sessionId: "s1", active: false, answers: 0 });
  await act(() => Promise.resolve());
  expect(sent).toEqual([]);
  rerender({ sessionId: "s1", active: true, answers: 0 });
  await waitFor(() => expect(sent).toHaveLength(1), { interval: 1 });
  await write(writing("t1", "It is"));
  await waitFor(() => expect(result.current).toEqual({ turn: "t1", text: "It is" }), { interval: 1 });
  await waitFor(() => expect(sent.length).toBeGreaterThan(1), { interval: 1 });
  expect(sent.slice(0, 2).map(({ path, query }) => `${path}${query}`)).toEqual([
    "/api/sessions/s1/partial?after=0&wait_sec=25", "/api/sessions/s1/partial?after=1&wait_sec=25"]);
  // Idle, it shows none; another session starts afresh, with none of the first one's bubble.
  rerender({ sessionId: "s1", active: false, answers: 0 });
  expect(result.current).toBeNull();
  rerender({ sessionId: "s2", active: true, answers: 0 });
  expect(result.current).toBeNull();
  await waitFor(() => expect(sent).toHaveLength(3), { interval: 1 });
  expect(sent[2]).toMatchObject({ path: "/api/sessions/s2/partial", query: "?after=0&wait_sec=25" });
});

test("a stored answer takes the bubble away in the render that counts it, and its turn never shows again", async () => {
  const { sent, write } = server();
  const { result, rerender, rendered } = follow({ sessionId: "s1", active: true, answers: 0 });
  await write(writing("t1", "The answer"));
  await waitFor(() => expect(result.current).toEqual({ turn: "t1", text: "The answer" }), { interval: 1 });
  rerender({ sessionId: "s1", active: true, answers: 1 });
  expect(rendered.filter(([answers, bubble]) => answers === 1 && bubble !== null)).toEqual([]);
  // The turn's last word comes after its answer: it is a leftover.
  const reads = sent.length;
  await write(writing("t1", "The answer, again"));
  await waitFor(() => expect(sent.length).toBeGreaterThan(reads), { interval: 1 });
  expect(result.current).toBeNull();
  await write(writing("t2", "Next"));
  await waitFor(() => expect(result.current).toEqual({ turn: "t2", text: "Next" }), { interval: 1 });
});

test("a failed read is read again after 1 s, and a 404 ends the reads", async () => {
  vi.useFakeTimers();
  let answers = [Response.json({ detail: "busy" }, { status: 503 }), Response.json({ detail: { code: "chat_partials_off" } }, { status: 404 })];
  const { sent } = server(() => {
    const [next, ...rest] = answers;
    answers = rest;
    return next ?? null;
  });
  follow({ sessionId: "s1", active: true, answers: 0 });
  await vi.advanceTimersByTimeAsync(0);
  expect(sent).toHaveLength(1);
  await vi.advanceTimersByTimeAsync(999);
  expect(sent).toHaveLength(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(sent).toHaveLength(2);
  await vi.advanceTimersByTimeAsync(60_000);
  expect(sent).toHaveLength(2);
});
