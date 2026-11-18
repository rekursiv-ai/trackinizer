import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { testClient } from "../live/testing";
import { stubFetch } from "./testing";
import { useTrustedRead } from "./trusted";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** A trusted read of document `id` that answers the accounts it was handed, counting its reads. */
function trustedOf(id: string | null, { trustFails = false } = {}) {
  const world = { trustFails, reads: 0 };
  const sent = stubFetch(() => (world.trustFails ? Response.json({ detail: "down" }, { status: 500 }) : Response.json({ summary_accounts: ["svc"] })));
  const client = testClient();
  const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children);
  const hook = renderHook(
    () =>
      useTrustedRead(id !== null, (accounts) => ({
        queryKey: ["doc", id, accounts],
        queryFn: () => {
          world.reads += 1;
          return { id, accounts };
        },
      })),
    { wrapper },
  );
  return { ...hook, world, sent, client };
}

test("the document is read through the accounts the profile trusts", async () => {
  const { result, client } = trustedOf("d1");
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data).toEqual({ id: "d1", accounts: ["svc"] });
  expect(result.current.error).toBeNull();
  // One trust read, under the key every trusted reader shares.
  expect(client.getQueryData(["me", "trust"])).toEqual({ audiences: [], accounts: ["svc"] });
});

test("disabled reads neither trust nor the document", async () => {
  const { result, sent, world } = trustedOf(null);
  await Promise.resolve();
  expect(sent).toEqual([]);
  expect(world.reads).toBe(0);
  expect(result.current.data).toBeUndefined();
});

test("a trust failure is the read's error, and refetch retries trust then the document", async () => {
  const { result, world } = trustedOf("d1", { trustFails: true });
  await waitFor(() => expect(result.current.error).not.toBeNull());
  expect(world.reads).toBe(0);
  world.trustFails = false;
  act(() => result.current.refetch());
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.error).toBeNull();
  expect(world.reads).toBe(1);
});

test("with trust in hand, refetch reads the document again", async () => {
  const { result, world } = trustedOf("d1");
  await waitFor(() => expect(world.reads).toBe(1));
  act(() => result.current.refetch());
  await waitFor(() => expect(world.reads).toBe(2));
});
