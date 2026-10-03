import { afterEach, expect, test, vi } from "vitest";
import {
  addAllowlistEntry,
  deleteUser,
  disableUser,
  enableUser,
  listAllowlist,
  listUsers,
  removeAllowlistEntry,
  setAllowlistRole,
  setUserRole,
} from "./admin";
import { stubFetch } from "./testing";

const USER = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";
const JSON_BODY = { "content-type": "application/json" };

afterEach(() => {
  vi.unstubAllGlobals();
});

test("the lists are GETs, read from their users and entries fields", async () => {
  const user = { id: USER, email: "ada@example.com", name: "Ada", role: "writer", status: "active", created_at: "2026-09-20T10:00:00+00:00", last_login: null };
  const entry = { email_or_pattern: "*@example.com", role: "viewer", added_by: null, added_at: "2026-09-20T10:00:00+00:00" };
  const sent = stubFetch((request) =>
    Response.json(request.url.endsWith("/users") ? { users: [user] } : { entries: [entry] }),
  );
  expect(await listUsers()).toEqual([user]);
  expect(await listAllowlist()).toEqual([entry]);
  expect(sent.map(({ method, path, body }) => [method, path, body])).toEqual([
    ["GET", "/api/admin/users", undefined],
    ["GET", "/api/admin/allowlist", undefined],
  ]);
});

test("each user write goes to its own route, with no idempotency key, since the server keeps none", async () => {
  const sent = stubFetch((request) => (request.method === "DELETE" ? new Response(null, { status: 204 }) : Response.json({ ok: true })));
  await setUserRole(USER, "viewer");
  await disableUser(USER);
  await enableUser(USER);
  expect(await deleteUser(USER)).toBeUndefined();
  expect(sent).toEqual([
    { method: "PUT", path: `/api/admin/users/${USER}/role`, query: "", headers: JSON_BODY, body: { role: "viewer" } },
    { method: "POST", path: `/api/admin/users/${USER}/disable`, query: "", headers: {}, body: undefined },
    { method: "POST", path: `/api/admin/users/${USER}/enable`, query: "", headers: {}, body: undefined },
    { method: "DELETE", path: `/api/admin/users/${USER}`, query: "", headers: {}, body: undefined },
  ]);
});

test("allowlist writes put the entry in the path percent-encoded, so a wildcard reaches the server whole", async () => {
  const sent = stubFetch((request) =>
    Response.json(request.method === "POST" ? { ok: true, email_or_pattern: "*@example.com", role: "writer" } : { ok: true }),
  );
  expect(await addAllowlistEntry({ email_or_pattern: " *@Example.com", role: "writer" })).toEqual({
    email_or_pattern: "*@example.com",
    role: "writer",
  });
  await setAllowlistRole("*@example.com", "viewer");
  await removeAllowlistEntry("ada+test@example.com");
  expect(sent).toEqual([
    { method: "POST", path: "/api/admin/allowlist", query: "", headers: JSON_BODY, body: { email_or_pattern: " *@Example.com", role: "writer" } },
    { method: "PUT", path: "/api/admin/allowlist/*%40example.com/role", query: "", headers: JSON_BODY, body: { role: "viewer" } },
    { method: "DELETE", path: "/api/admin/allowlist/ada%2Btest%40example.com", query: "", headers: {}, body: undefined },
  ]);
});

test("a refusal is an ApiError with the server's message", async () => {
  stubFetch(() => Response.json({ detail: "Demoting one's own admin role is not permitted." }, { status: 409 }));
  await expect(setUserRole(USER, "writer")).rejects.toMatchObject({
    status: 409,
    detail: "Demoting one's own admin role is not permitted.",
  });
});

// Compile-time checks: `tsc --noEmit` reads them, and nothing calls this.
export async function rejected(): Promise<void> {
  // @ts-expect-error A role the server does not know.
  await setUserRole(USER, "owner");
  // @ts-expect-error An allowlist entry needs its role.
  await addAllowlistEntry({ email_or_pattern: "a@b.c" });
  // @ts-expect-error No unknown keys: the server would drop them without a word.
  await addAllowlistEntry({ email_or_pattern: "a@b.c", role: "viewer", note: "x" });
}
