import { QueryObserver } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import type { Detail } from "../api/detail";
import { detailQueries } from "../detail/queries";
import { DetailLive } from "./detail";
import { testClient, uuid } from "./testing";

const FOCUS = uuid(1);
const PARENT = uuid(2);
const CHILD = uuid(3);
const ELSEWHERE = uuid(4);

afterEach(() => {
  vi.useRealTimers();
});

const peer = (id: string) => ({ id, kind: "Issue", seq: 0, title: "", status: "active" });

/** A detail of `FOCUS` with a parent and a child, and the reads around it, each counting its fetches. */
function openDetail() {
  const client = testClient();
  const fetched: string[] = [];
  const detail: Detail = {
    self: { id: FOCUS, kind: "Issue", seq: 1, title: "Focus", status: "active", created: "", modified: "" },
    edges: { narrows: [peer(PARENT)] },
    backlinks: { narrowed_by: [peer(CHILD)] },
    changes: [],
  };
  const reads = [
    [detailQueries.detail(FOCUS).queryKey, detail],
    [detailQueries.confidence(FOCUS).queryKey, 0.5],
    [["metrics", FOCUS], []],
    [["session", FOCUS, "parts"], []],
    [["search", FOCUS, "Issue"], []],
    [detailQueries.detail(ELSEWHERE).queryKey, detail],
  ] as const;
  for (const [key, data] of reads) {
    client.setQueryData(key, data);
    new QueryObserver(client, {
      queryKey: key,
      queryFn: () => {
        fetched.push(key[0]);
        return data;
      },
      // Fresh, so mounting it fetches nothing; enabled, as a view's reads are.
      staleTime: Infinity,
    }).subscribe(() => {});
  }
  return { client, fetched, live: new DetailLive(client, FOCUS) };
}

test("a batch with the focus refetches every read keyed by it, never a search", async () => {
  const { live, fetched } = openDetail();
  await live.update({ ids: new Set([FOCUS]), gap: false });
  expect(fetched.toSorted()).toEqual(["confidence", "detail", "metrics", "session"]);
});

test("a batch with a neighbour the detail shows refetches the detail and its evidence confidence", async () => {
  const { live, fetched } = openDetail();
  await live.update({ ids: new Set([PARENT]), gap: false });
  expect(fetched.toSorted()).toEqual(["confidence", "detail"]);
  fetched.length = 0;
  await live.update({ ids: new Set([CHILD]), gap: false });
  expect(fetched.toSorted()).toEqual(["confidence", "detail"]);
});

test("a batch with neither refetches nothing; a gap recovery refetches like the focus", async () => {
  const { live, fetched } = openDetail();
  await live.update({ ids: new Set([ELSEWHERE]), gap: false });
  expect(fetched).toEqual([]);
  await live.update({ ids: new Set(), gap: true });
  expect(fetched.toSorted()).toEqual(["confidence", "detail", "metrics", "session"]);
});

test("a session's id rereads its parts listing, never the records its transcript holds, which follow the listing (B4)", async () => {
  const { client, live, fetched } = openDetail();
  const records = ["session", FOCUS, "records", 0] as const;
  client.setQueryData(records, []);
  new QueryObserver(client, {
    queryKey: records,
    queryFn: () => {
      fetched.push("records");
      return [];
    },
    staleTime: Infinity,
  }).subscribe(() => {});
  await live.update({ ids: new Set([FOCUS]), gap: false });
  await live.update({ ids: new Set(), gap: true });
  expect(fetched.toSorted()).toEqual(["confidence", "confidence", "detail", "detail", "metrics", "metrics", "session", "session"]);
});

test("a fetch already under way is joined, not cancelled, then run once more", async () => {
  const { client, live } = openDetail();
  let answer = "before";
  const started: string[] = [];
  const unmount = new QueryObserver(client, {
    queryKey: ["detail", FOCUS],
    queryFn: async () => {
      started.push(answer);
      await new Promise((resolve) => setTimeout(resolve, 10));
      return { ...(client.getQueryData<Detail>(["detail", FOCUS]) as Detail), self: { title: answer } };
    },
    staleTime: Infinity,
  }).subscribe(() => {});
  vi.useFakeTimers();
  // A fetch that started before the change...
  const underWay = client.refetchQueries({ queryKey: ["detail", FOCUS] });
  answer = "after";
  // ...is joined, and a second one catches the change.
  const update = live.update({ ids: new Set([FOCUS]), gap: false });
  await vi.advanceTimersByTimeAsync(100);
  await Promise.all([underWay, update]);
  // Joined: the fetch under way ran to its end, then one more. Cancelling would
  // have started a third.
  expect(started).toEqual(["before", "after"]);
  expect(client.getQueryData<{ self: { title: string } }>(["detail", FOCUS])!.self.title).toBe("after");
  unmount();
});

test("a batch that comes while the detail's first read is under way reads it once more, as that read may predate it (CR-LIVE-S1)", async () => {
  const client = testClient();
  const answers: ((title: string) => void)[] = [];
  const started: number[] = [];
  const unmount = new QueryObserver(client, {
    queryKey: detailQueries.detail(FOCUS).queryKey,
    queryFn: () =>
      new Promise<Detail>((answer) => {
        started.push(Date.now());
        answers.push((title) =>
          answer({
            self: { id: FOCUS, kind: "Issue", seq: 1, title: "Focus", status: "active", created: "", modified: "" },
            edges: { narrows: [{ ...peer(PARENT), title }] },
            backlinks: {},
            changes: [],
          }),
        );
      }),
    staleTime: Infinity,
  }).subscribe(() => {});
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(started).toHaveLength(1);
  // The parent is renamed while the first read is under way; that read answers with its old title.
  const update = new DetailLive(client, FOCUS).update({ ids: new Set([PARENT]), gap: false });
  answers[0]!("Old title");
  await new Promise((resolve) => setTimeout(resolve, 0));
  answers[1]?.("New title");
  await update;
  expect(client.getQueryData<Detail>(detailQueries.detail(FOCUS).queryKey)!.edges.narrows![0]!.title).toBe("New title");
  unmount();
});
