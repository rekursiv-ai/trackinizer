import { afterEach, expect, test, vi } from "vitest";
import { acknowledgeRules, createToken, getProfile, listTokens, revokeToken, setTokenRole, signOut, type Token } from "./me";
import { stubFetch } from "./testing";

const KEY = "5d3c2b1a-0f9e-4d8c-8b7a-6e5d4c3b2a19";
const JSON_BODY = { "content-type": "application/json" };

afterEach(() => {
  vi.unstubAllGlobals();
});

test("the profile is a GET of /api/me/profile with no body", async () => {
  const ada = {
    user_id: "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33",
    email: "ada@example.com",
    name: "Ada",
    role: "viewer",
    last_login: null,
  };
  const sent = stubFetch(() => Response.json(ada));
  expect(await getProfile()).toEqual(ada);
  expect(sent).toEqual([
    { method: "GET", path: "/api/me/profile", query: "", headers: {}, body: undefined },
  ]);
});

test("the token list is a GET of /api/me/tokens, read from its tokens field", async () => {
  const token: Token = {
    id: KEY,
    name: "laptop trax",
    prefix: "trk_ab12",
    role: "writer",
    created_at: "2026-09-20T10:00:00+00:00",
    last_used_at: null,
    revoked_at: null,
  };
  const sent = stubFetch(() => Response.json({ tokens: [token] }));
  expect(await listTokens()).toEqual([token]);
  expect(sent).toEqual([{ method: "GET", path: "/api/me/tokens", query: "", headers: {}, body: undefined }]);
});

test("each token write sends its body and no idempotency key, since the server keeps none", async () => {
  const made = { id: KEY, name: "ci", prefix: "trk_ab12", role: "viewer", secret: "trk_ab12secret" };
  const sent = stubFetch((request) => Response.json(request.url.endsWith("/api/me/tokens") ? made : { ok: true }));
  expect(await createToken({ name: "ci", role: "viewer" })).toEqual(made);
  await createToken({ name: "default role" });
  await setTokenRole(KEY, "writer");
  await revokeToken(KEY);
  expect(sent).toEqual([
    { method: "POST", path: "/api/me/tokens", query: "", headers: JSON_BODY, body: { name: "ci", role: "viewer" } },
    { method: "POST", path: "/api/me/tokens", query: "", headers: JSON_BODY, body: { name: "default role" } },
    { method: "PUT", path: `/api/me/tokens/${KEY}/role`, query: "", headers: JSON_BODY, body: { role: "writer" } },
    { method: "POST", path: `/api/me/tokens/${KEY}/revoke`, query: "", headers: {}, body: undefined },
  ]);
});

test("sign out is a POST of /auth/logout that does not read the page it is sent to", async () => {
  const sent = stubFetch(() => new Response("<!doctype html><title>Sign in</title>", { headers: { "content-type": "text/html" } }));
  await signOut();
  expect(sent).toEqual([{ method: "POST", path: "/auth/logout", query: "", headers: {}, body: undefined }]);
});

test("sign out does not follow the server's redirect to /, which a server without the old UI answers 404 (B2)", async () => {
  const sent = stubFetch((request) =>
    request.redirect === "manual" ? new Response(null, { status: 302, headers: { location: "/" } }) : new Response("Not Found", { status: 404 }),
  );
  await signOut();
  expect(sent.map((request) => `${request.method} ${request.path}`)).toEqual(["POST /auth/logout"]);
});

test("a refused token write is an ApiError with the server's message", async () => {
  stubFetch(() => Response.json({ detail: "requested role 'admin' exceeds ceiling 'writer'" }, { status: 403 }));
  await expect(createToken({ name: "x", role: "admin" })).rejects.toMatchObject({
    status: 403,
    detail: "requested role 'admin' exceeds ceiling 'writer'",
  });
});

// Compile-time checks: `tsc --noEmit` reads them, and nothing calls this.
export async function rejected(): Promise<void> {
  // A role the server does not know,
  // @ts-expect-error
  await createToken({ name: "x", role: "owner" });
  // an unknown key, which the server would drop without a word,
  // @ts-expect-error
  await createToken({ name: "x", actor: "me" });
  // and a token without a label.
  // @ts-expect-error
  await createToken({ role: "viewer" });
  // @ts-expect-error
  await setTokenRole(KEY, "root");
}

test("agreeing to the rules is a PUT of /api/me/acknowledge naming the version read, answered with when and to what", async () => {
  const agreed = { acknowledged_at: "2026-10-09T08:00:00+00:00", acknowledged_rules_version: "2026-10-08T12:00:00+00:00" };
  const sent = stubFetch(() => Response.json(agreed));
  expect(await acknowledgeRules("2026-10-08T12:00:00+00:00")).toEqual(agreed);
  expect(sent).toEqual([
    {
      method: "PUT",
      path: "/api/me/acknowledge",
      query: "",
      headers: { "content-type": "application/json" },
      body: { rules_version: "2026-10-08T12:00:00+00:00" },
    },
  ]);
});
