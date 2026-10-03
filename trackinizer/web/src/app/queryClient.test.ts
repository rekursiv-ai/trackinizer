import { expect, test, vi } from "vitest";
import { ApiError } from "../api/client";
import { recentEvents } from "../debug/log";
import { createQueryClient, retryRead } from "./queryClient";

test("reads retry twice on a timeout, a dropped network or a 5xx, never on a 4xx", () => {
  const timeout = new ApiError(0, "No response within 15 s.", "timeout");
  const unavailable = new ApiError(503, "database unavailable");
  expect([0, 1, 2].map((failures) => retryRead(failures, timeout))).toEqual([true, true, false]);
  expect([0, 1, 2].map((failures) => retryRead(failures, unavailable))).toEqual([true, true, false]);
  for (const status of [400, 401, 403, 404, 409, 422]) {
    expect(retryRead(0, new ApiError(status, "no")), String(status)).toBe(false);
  }
  expect(retryRead(0, new TypeError("bug"))).toBe(false);
  const delay = createQueryClient(vi.fn()).getDefaultOptions().queries?.retryDelay;
  expect(typeof delay === "function" && [0, 1].map((failures) => delay(failures, timeout))).toEqual([
    1000, 3000,
  ]);
});

test("a 401 from a read or a write ends the session; other failures do not", async () => {
  const onUnauthorized = vi.fn();
  const client = createQueryClient(onUnauthorized);
  const fail = (status: number) => () => Promise.reject(new ApiError(status, "no"));
  await client.fetchQuery({ queryKey: ["a"], queryFn: fail(403) }).catch(() => {});
  expect(onUnauthorized).not.toHaveBeenCalled();
  await client.fetchQuery({ queryKey: ["b"], queryFn: fail(401) }).catch(() => {});
  expect(onUnauthorized).toHaveBeenCalledTimes(1);
  await client
    .getMutationCache()
    .build(client, { mutationFn: fail(401) })
    .execute(undefined)
    .catch(() => {});
  expect(onUnauthorized).toHaveBeenCalledTimes(2);
});

test("a 403 from a read or a write is logged, with the request's id", async () => {
  vi.spyOn(console, "warn").mockImplementation(() => {});
  const client = createQueryClient(vi.fn());
  const sent = { method: "PUT", path: "/api/admin/users/u1/role", id: "5d3c2b1a-0f9e-4d8c-8b7a-6e5d4c3b2a19", at: "", ms: 3, attempt: 1 };
  const refused = () => Promise.reject(new ApiError(403, "admin role required", null, sent));
  await client.getMutationCache().build(client, { mutationFn: refused }).execute(undefined).catch(() => {});
  expect(recentEvents().at(-1)).toMatchObject({ level: "warn", event: "role.refused", fields: { status: 403, request_id: sent.id } });
  vi.restoreAllMocks();
});
