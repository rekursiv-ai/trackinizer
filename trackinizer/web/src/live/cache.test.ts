import { QueryObserver } from "@tanstack/react-query";
import { expect, test } from "vitest";
import { refetchFresh } from "./cache";
import { testClient } from "./testing";

test("a refetch runs once more only for the queries already fetching when it began", async () => {
  const client = testClient();
  const calls = { idle: 0, busy: 0 };
  const answers: (() => void)[] = [];
  new QueryObserver(client, { queryKey: ["x", "idle"], queryFn: async () => ++calls.idle, staleTime: Infinity }).subscribe(() => {});
  new QueryObserver(client, {
    queryKey: ["x", "busy"],
    queryFn: () => new Promise<number>((answer) => answers.push(() => answer(++calls.busy))),
    staleTime: Infinity,
  }).subscribe(() => {});
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(client.isFetching({ queryKey: ["x"] })).toBe(1);
  expect(calls.idle).toBe(1);
  // "busy" is fetching: the refetch joins that fetch, which may predate the change, then runs it once more.
  const done = refetchFresh(client, { queryKey: ["x"] });
  answers.shift()!();
  await new Promise((resolve) => setTimeout(resolve, 0));
  answers.shift()!();
  await done;
  expect(calls).toEqual({ idle: 2, busy: 2 });
});

test("a refetch that fails rejects, so the stream layer tries it again (CR-LIVE-04)", async () => {
  const client = testClient();
  let fail = true;
  new QueryObserver(client, {
    queryKey: ["x"],
    queryFn: async () => {
      if (fail) throw new Error("503");
      return 1;
    },
    staleTime: Infinity,
  }).subscribe(() => {});
  await new Promise((resolve) => setTimeout(resolve, 0));
  await expect(refetchFresh(client, { queryKey: ["x"] })).rejects.toThrow("503");
  fail = false;
  await expect(refetchFresh(client, { queryKey: ["x"] })).resolves.toBeUndefined();
});
