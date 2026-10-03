// Test helpers for Settings and Admin; only tests import this file.
import * as inspector from "node:inspector/promises";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { onTestFinished } from "vitest";
import type { AllowlistEntry, User } from "../api/admin";
import type { Profile, Role, Token } from "../api/me";
import { type Sent, stubFetch } from "../api/testing";
import { type Meta, MetaContext, ProfileContext } from "../app/boot";
import { Session, SessionContext } from "../app/session";
import { ToastProvider } from "../ui/toast";
import { rolesUpTo } from "./roles";

export const KINDS = ["Issue", "Belief", "CodeChange"];

const META: Meta = { kinds: KINDS, enums: { inquiry_kind_all: KINDS }, fieldOwners: {}, edges: {} };

/** A signed-in user of `role`: Ada, whose id is `u-ada`. */
export function profile(role: Role): Profile {
  return { user_id: "u-ada", email: "ada@example.com", name: "Ada", role, last_login: "2026-09-26T10:00:00+00:00", visual_workspace_enabled: false };
}

/** Render `ui` with the app's providers for `who`; `queryClient` is its cache. */
export function renderScreen(ui: ReactNode, who: Profile, queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return render(
    <QueryClientProvider client={queryClient}>
      <SessionContext value={new Session(() => {})}>
        <ToastProvider>
          <MetaContext value={META}>
            <ProfileContext value={who}>{ui}</ProfileContext>
          </MetaContext>
        </ToastProvider>
      </SessionContext>
    </QueryClientProvider>,
  );
}

/**
 * `waitFor` options for a condition no DOM change signals (a request sent, the
 * clipboard written): poll every 5 ms, not the default 50.
 */
export const FAST = { interval: 5 } as const;

/** Give `navigator` a clipboard for this test; returns what is written to it. */
export function stubClipboard(): string[] {
  const copied: string[] = [];
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: async (text: string) => void copied.push(text) },
  });
  onTestFinished(() => {
    Reflect.deleteProperty(navigator, "clipboard");
  });
  return copied;
}

/**
 * Every value `fn` closes over, as text (objects as JSON), read through V8's
 * inspector: what a function a cache keeps still holds. The global and module
 * scopes, which hold the whole page, are left out.
 */
export async function closedOver(fn: (...args: never[]) => unknown): Promise<string> {
  const session = new inspector.Session();
  session.connect();
  Reflect.set(globalThis, PROBE, fn);
  try {
    const { result } = await session.post("Runtime.evaluate", { expression: `globalThis[${JSON.stringify(PROBE)}]` });
    const { internalProperties = [] } = await session.post("Runtime.getProperties", { objectId: result.objectId! });
    const scopes = internalProperties.find((property) => property.name === "[[Scopes]]")!.value!.objectId!;
    const texts: string[] = [];
    for (const scope of (await session.post("Runtime.getProperties", { objectId: scopes })).result) {
      if (!/^(Closure|Block)/.test(scope.value?.description ?? "")) continue;
      for (const { value } of (await session.post("Runtime.getProperties", { objectId: scope.value!.objectId! })).result) {
        if (value?.objectId === undefined) texts.push(String(value?.value));
        else texts.push(String((await session.post("Runtime.callFunctionOn", { objectId: value.objectId, functionDeclaration: AS_TEXT, returnByValue: true })).result.value));
      }
    }
    return texts.join("\n");
  } finally {
    Reflect.deleteProperty(globalThis, PROBE);
    session.disconnect();
  }
}

const PROBE = "__closedOverProbe";
const AS_TEXT = "function () { try { return JSON.stringify(this); } catch { return String(this); } }";

/** A token as the server lists it. */
export function token(n: number, fields: Partial<Token> = {}): Token {
  return {
    id: `t${n}`,
    name: `token ${n}`,
    prefix: `trk_000${n}`,
    role: "writer",
    created_at: "2026-09-20T10:00:00+00:00",
    last_used_at: null,
    revoked_at: null,
    ...fields,
  };
}

/** A user as the admin list shows them. */
export function user(id: string, email: string, fields: Partial<User> = {}): User {
  return {
    id,
    email,
    name: email.split("@")[0]!,
    role: "writer",
    status: "active",
    created_at: "2026-09-01T10:00:00+00:00",
    last_login: null,
    ...fields,
  };
}

/**
 * A trackinizer server for account routes, applying writes as the handlers in
 * `server/api/auth_routes.py` and `admin_routes.py` do: token roles capped at
 * the caller's (403), no self-demotion, self-disable or self-delete, and no
 * change leaving no active admin (409), a duplicate allowlist entry (409, in
 * the words of the server's unique-violation handler), a blank one (422), and a
 * missing or revoked token (404). `answers` take the next writes over; `hold`
 * holds the next write until released; reads of a path in `failing` answer
 * 503; `rows` are what the owner-names list request returns.
 */
export function serveAccount(caller: Profile, seed: { tokens?: Token[]; users?: User[]; allowlist?: AllowlistEntry[] } = {}) {
  const server = {
    tokens: seed.tokens ?? [],
    users: seed.users ?? [],
    allowlist: seed.allowlist ?? [],
    rows: [] as { owner: string | null }[],
    answers: [] as ((request: Request) => Response | Promise<Response>)[],
    failing: new Set<string>(),
    held: [] as Promise<void>[],
    sent: [] as Sent[],
    /** The writes sent, as `METHOD path` with the body. */
    writes: () =>
      server.sent.filter((request) => request.method !== "GET").map(({ method, path, body }) => ({ call: `${method} ${path}`, body })),
    reads: (path: string) => server.sent.filter((request) => request.method === "GET" && request.path === path).length,
    /** Hold the next write, then answer it as usual once the returned function is called. */
    hold: () => {
      let release = () => {};
      server.held.push(new Promise((resolve) => (release = resolve)));
      return release;
    },
  };
  server.sent = stubFetch(async (request) => {
    const path = decodeURIComponent(new URL(request.url).pathname);
    if (request.method === "GET" && server.failing.has(path)) return refuse(503, "database unavailable");
    if (request.method !== "GET") {
      await server.held.shift();
      const scripted = server.answers.shift();
      if (scripted) return scripted(request);
    }
    const text = await request.clone().text();
    const body = (text ? JSON.parse(text) : {}) as { name?: string; role?: Role; email_or_pattern?: string; enabled?: boolean };
    return answer(server, caller, `${request.method} ${path}`, body);
  });
  return server;
}

type Server = ReturnType<typeof serveAccount>;

function answer(server: Server, caller: Profile, call: string, body: { name?: string; role?: Role; email_or_pattern?: string; enabled?: boolean }): Response {
  const [method, path] = call.split(" ") as [string, string];
  const parts = path.split("/").slice(1);
  if (call === "GET /api/me/profile") return Response.json(caller);
  if (call === "PUT /api/me/visual-workspace") {
    caller.visual_workspace_enabled = body.enabled ?? false;
    return Response.json({ enabled: caller.visual_workspace_enabled });
  }
  if (call === "GET /api/me/tokens") return Response.json({ tokens: server.tokens });
  if (call === "GET /api/admin/users") return Response.json({ users: server.users });
  if (call === "GET /api/admin/allowlist") return Response.json({ entries: server.allowlist });
  if (call === "GET /api/inquiries") return Response.json(server.rows);
  // The page `fetch` lands on after following the logout's redirect.
  if (call === "POST /auth/logout") return new Response("<!doctype html><title>Sign in</title>", { headers: { "content-type": "text/html" } });
  if (call === "POST /api/me/tokens") {
    if (!body.name) return refuse(422, [{ loc: ["body", "name"], msg: "String should have at least 1 character" }]);
    const role = body.role ?? (caller.role as Role);
    if (!rolesUpTo(caller.role).includes(role)) return refuse(403, `requested role '${role}' exceeds ceiling '${caller.role}'`);
    const made = token(server.tokens.length + 1, { name: body.name, role, prefix: `trk_new${server.tokens.length + 1}` });
    server.tokens = [made, ...server.tokens];
    return Response.json({ id: made.id, name: made.name, prefix: made.prefix, role, secret: `${made.prefix}SECRET-${made.id}` });
  }
  if (parts[1] === "me" && parts[2] === "tokens") {
    const held = server.tokens.find((candidate) => candidate.id === parts[3] && candidate.revoked_at === null);
    if (!held) return refuse(404, "token not found");
    if (parts[4] === "revoke") {
      server.tokens = server.tokens.map((kept) => (kept === held ? { ...kept, revoked_at: "2026-09-27T09:00:00+00:00" } : kept));
      return Response.json({ ok: true });
    }
    if (!rolesUpTo(caller.role).includes(body.role!)) return refuse(403, `requested role '${body.role}' exceeds ceiling '${caller.role}'`);
    server.tokens = server.tokens.map((kept) => (kept === held ? { ...kept, role: body.role! } : kept));
    return Response.json({ ok: true, role: body.role });
  }
  if (caller.role !== "admin") return refuse(403, "admin role required");
  if (parts[1] === "admin" && parts[2] === "users") return userWrite(server, caller, method, parts[3]!, parts[4], body.role);
  if (call === "POST /api/admin/allowlist") {
    const entry = body.email_or_pattern!.trim().toLowerCase();
    if (!entry) return refuse(422, "allowlist entry cannot be blank");
    if (server.allowlist.some((kept) => kept.email_or_pattern === entry)) {
      return refuse(409, "unique constraint violated");
    }
    server.allowlist = [{ email_or_pattern: entry, role: body.role!, added_by: caller.user_id, added_at: "2026-09-27T09:00:00+00:00" }, ...server.allowlist];
    return Response.json({ ok: true, email_or_pattern: entry, role: body.role });
  }
  if (parts[1] === "admin" && parts[2] === "allowlist") {
    const held = server.allowlist.find((kept) => kept.email_or_pattern === parts[3]);
    if (!held) return refuse(404, "allowlist entry not found");
    server.allowlist = method === "DELETE" ? server.allowlist.filter((kept) => kept !== held) : server.allowlist.map((kept) => (kept === held ? { ...kept, role: body.role! } : kept));
    return Response.json(method === "DELETE" ? { ok: true } : { ok: true, role: body.role });
  }
  return refuse(404, `no route ${call}`);
}

function userWrite(server: Server, caller: Profile, method: string, id: string, action: string | undefined, role: Role | undefined): Response {
  const target = server.users.find((kept) => kept.id === id);
  const self = id === caller.user_id;
  if (self && action === "role" && role !== "admin") return refuse(409, "Demoting one's own admin role is not permitted.");
  if (self && action === "disable") return refuse(409, "Disabling one's own account is not permitted.");
  if (self && method === "DELETE") return refuse(409, "Deleting one's own account is not permitted.");
  if (!target) return refuse(404, "user not found");
  const losesAdmin = target.role === "admin" && target.status === "active" && (action === "disable" || method === "DELETE" || (action === "role" && role !== "admin"));
  const otherAdmins = server.users.filter((kept) => kept !== target && kept.role === "admin" && kept.status === "active").length;
  if (losesAdmin && otherAdmins === 0) return refuse(409, "last_admin: refusing to leave the org without an active admin.");
  if (method === "DELETE") {
    server.users = server.users.filter((kept) => kept !== target);
    return new Response(null, { status: 204 });
  }
  const changed = action === "role" ? { role: role! } : { status: action === "disable" ? "disabled" : "active" };
  server.users = server.users.map((kept) => (kept === target ? { ...kept, ...changed } : kept));
  return Response.json({ ok: true, ...changed });
}

function refuse(status: number, detail: unknown): Response {
  return Response.json({ detail }, { status });
}
