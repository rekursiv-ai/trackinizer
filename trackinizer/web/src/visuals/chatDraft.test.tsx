import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { Profile } from "../api/me";
import { ProfileContext } from "../app/boot";
import { Session, SessionContext } from "../app/session";
import { DEFAULT_PREFERENCES, preferences, receivePreferences, sendWaiting } from "../preferences";
import { adoptLocalUserData } from "../state/local";
import { resetPreferences } from "../state/testing";
import { useChatDraft } from "./chatDraft";

const ADA = "ada@example.com";
const BOB = "bob@example.com";
const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";

let sent: unknown[] = [];
let page = new Session(vi.fn());

beforeEach(() => {
  localStorage.clear();
  sent = [];
  page = new Session(vi.fn());
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const change = (await request.json()) as object;
      sent.push(change);
      return Response.json({ ...DEFAULT_PREFERENCES, ...preferences(), ...change });
    }),
  );
});

afterEach(async () => {
  cleanup();
  await resetPreferences();
  vi.unstubAllGlobals();
});

function mount(canvasId: string | null, { email = ADA, session = page } = {}) {
  const profile = { email } as Profile;
  const wrapper = ({ children }: { children: ReactNode }) => (
    <SessionContext value={session}>
      <ProfileContext value={profile}>{children}</ProfileContext>
    </SessionContext>
  );
  return renderHook(({ id }: { id: string | null }) => useChatDraft(id), { wrapper, initialProps: { id: canvasId } });
}

test("a draft is kept per canvas and comes back on switch and remount", () => {
  const view = mount(A);
  act(() => view.result.current.edit("hello a"));
  expect(preferences().drafts[`chat:${A}`]).toEqual({ text: "hello a", base: null });
  view.rerender({ id: B });
  expect(view.result.current.text).toBe("");
  act(() => view.result.current.edit("hello b"));
  view.rerender({ id: A });
  expect(view.result.current.text).toBe("hello a");
  view.unmount();
  expect(mount(B).result.current.text).toBe("hello b");
});

test("a canvas's kept draft opens from preferences that arrive later", () => {
  const view = mount(A);
  act(() => receivePreferences({ ...DEFAULT_PREFERENCES, drafts: { [`chat:${A}`]: { text: "from server", base: null } } }));
  expect(view.result.current.text).toBe("from server");
});

test("typed text survives a failed save and the server's snapshot without it", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "down" }, { status: 503 })));
  vi.spyOn(console, "error").mockImplementation(() => {});
  const view = mount(A);
  act(() => view.result.current.edit("unsaved"));
  await act(() => sendWaiting());
  act(() => receivePreferences(DEFAULT_PREFERENCES));
  expect(preferences().drafts).toEqual({});
  expect(view.result.current.text).toBe("unsaved");
});

test("a clear taken before a remote edit leaves the newer remote draft", async () => {
  const view = mount(A);
  act(() => view.result.current.edit("sent"));
  const clear = view.result.current.clear;
  await act(() => sendWaiting());
  act(() => receivePreferences({ ...DEFAULT_PREFERENCES, drafts: { [`chat:${A}`]: { text: "remote", base: null } } }));
  act(() => clear());
  expect(view.result.current.text).toBe("remote");
  expect(preferences().drafts[`chat:${A}`]).toEqual({ text: "remote", base: null });
});

test("text typed with no canvas does not follow into a canvas", () => {
  const view = mount(null);
  act(() => view.result.current.edit("loose"));
  view.rerender({ id: A });
  expect(view.result.current.text).toBe("");
  view.rerender({ id: null });
  expect(view.result.current.text).toBe("");
});

test("clearing to empty forgets the draft", () => {
  const view = mount(A);
  act(() => view.result.current.edit("x"));
  act(() => view.result.current.edit(""));
  expect(`chat:${A}` in preferences().drafts).toBe(false);
});

test("clear captured at send clears its own canvas after a switch, never the other", () => {
  const view = mount(A);
  act(() => view.result.current.edit("to a"));
  const clear = view.result.current.clear;
  view.rerender({ id: B });
  act(() => view.result.current.edit("to b"));
  act(() => clear());
  expect(view.result.current.text).toBe("to b");
  expect(`chat:${A}` in preferences().drafts).toBe(false);
  view.rerender({ id: A });
  expect(view.result.current.text).toBe("");
});

test("clear does nothing after an intervening edit, even back to the same text", () => {
  const view = mount(A);
  act(() => view.result.current.edit("same"));
  const stale = view.result.current.clear;
  act(() => view.result.current.edit("later"));
  act(() => stale());
  expect(view.result.current.text).toBe("later");
  const again = view.result.current.clear;
  act(() => view.result.current.edit("away"));
  act(() => view.result.current.edit("later"));
  act(() => again());
  expect(view.result.current.text).toBe("later");
  expect(preferences().drafts[`chat:${A}`]).toEqual({ text: "later", base: null });
  act(() => view.result.current.clear());
  expect(view.result.current.text).toBe("");
});

test("clear survives a remount when nothing was typed since", () => {
  const view = mount(A);
  act(() => view.result.current.edit("sent"));
  const clear = view.result.current.clear;
  view.unmount();
  const again = mount(A);
  act(() => clear());
  expect(again.result.current.text).toBe("");
});

test("a session that ends keeps the draft for the next boot", async () => {
  const session = new Session(vi.fn());
  const view = mount(A, { session });
  act(() => view.result.current.edit("unsent"));
  session.leaveForLogin();
  await sendWaiting();
  receivePreferences({ ...DEFAULT_PREFERENCES, drafts: {} });
  expect(preferences().drafts).toEqual({});
  await adoptLocalUserData(ADA);
  expect(preferences().drafts[`chat:${A}`]).toEqual({ text: "unsent", base: null });
});

test("a full drafts store keeps the text here and says so", () => {
  const full = Object.fromEntries(Array.from({ length: 200 }, (_, i) => [`other:${i}`, { text: "x", base: null }]));
  receivePreferences({ ...DEFAULT_PREFERENCES, drafts: full });
  const view = mount(A);
  expect(view.result.current.problem).toBeNull();
  act(() => view.result.current.edit("overflow"));
  expect(view.result.current.text).toBe("overflow");
  expect(view.result.current.problem).toMatch(/200/);
  expect(Object.keys(preferences().drafts)).toHaveLength(200);
  expect(preferences().drafts["other:0"]).toBeDefined();
  view.unmount();
  expect(mount(A).result.current.text).toBe("overflow");
});

test("another user signing in on this page never sees the draft", async () => {
  const view = mount(A);
  act(() => view.result.current.edit("ada's"));
  const clear = view.result.current.clear;
  view.unmount();
  await sendWaiting();
  receivePreferences(DEFAULT_PREFERENCES);
  const bob = mount(A, { email: BOB });
  expect(bob.result.current.text).toBe("");
  act(() => bob.result.current.edit("bob's"));
  act(() => clear());
  expect(bob.result.current.text).toBe("bob's");
  expect(preferences().drafts[`chat:${A}`]).toEqual({ text: "bob's", base: null });
});

test("an old send cannot clear the same text kept for another user", () => {
  const view = mount(A);
  act(() => view.result.current.edit("same"));
  const clear = view.result.current.clear;
  view.unmount();
  act(() => receivePreferences({ ...DEFAULT_PREFERENCES, drafts: { [`chat:${A}`]: { text: "same", base: null } } }));
  const bob = mount(A, { email: BOB });
  act(() => clear());
  expect(bob.result.current.text).toBe("same");
  expect(preferences().drafts[`chat:${A}`]).toEqual({ text: "same", base: null });
});

test("leaving for login keeps every canvas's unsaved text, not only the shown one", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "down" }, { status: 503 })));
  vi.spyOn(console, "error").mockImplementation(() => {});
  const session = new Session(vi.fn());
  const view = mount(A, { session });
  act(() => view.result.current.edit("text a"));
  await act(() => sendWaiting());
  view.rerender({ id: B });
  act(() => view.result.current.edit("text b"));
  session.leaveForLogin();
  view.unmount();
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => Response.json({ ...preferences(), ...((await request.json()) as object) })));
  await sendWaiting();
  receivePreferences({ ...DEFAULT_PREFERENCES, drafts: {} });
  await adoptLocalUserData(ADA);
  expect(preferences().drafts).toEqual({ [`chat:${A}`]: { text: "text a", base: null }, [`chat:${B}`]: { text: "text b", base: null } });
  expect(mount(A, { session: new Session(vi.fn()) }).result.current.text).toBe("text a");
});

test("a failed save says the draft is not saved; a later save clears it", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "down" }, { status: 503 })));
  vi.spyOn(console, "error").mockImplementation(() => {});
  const view = mount(A);
  act(() => view.result.current.edit("lost?"));
  expect(view.result.current.problem).toBeNull();
  await act(() => sendWaiting());
  expect(view.result.current.problem).toMatch(/^Draft not saved/);
  expect(view.result.current.text).toBe("lost?");
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => Response.json({ ...preferences(), ...((await request.json()) as object) })));
  act(() => view.result.current.edit("lost? no"));
  await act(() => sendWaiting());
  expect(view.result.current.problem).toBeNull();
});

test("another user on this page never sees an earlier user's save failure", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "down" }, { status: 503 })));
  vi.spyOn(console, "error").mockImplementation(() => {});
  const ada = mount(A);
  act(() => ada.result.current.edit("ada's"));
  await act(() => sendWaiting());
  expect(ada.result.current.problem).toMatch(/^Draft not saved/);
  ada.unmount();
  act(() => receivePreferences({ ...DEFAULT_PREFERENCES, drafts: { [`chat:${A}`]: { text: "bob's kept", base: null } } }));
  const bob = mount(A, { email: BOB });
  expect(bob.result.current.text).toBe("bob's kept");
  expect(bob.result.current.problem).toBeNull();
});

test("a draft a failed save left behind goes with the next save, or keeps its warning", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "down" }, { status: 503 })));
  vi.spyOn(console, "error").mockImplementation(() => {});
  const view = mount(A);
  act(() => view.result.current.edit("text a"));
  await act(() => sendWaiting());
  act(() => receivePreferences({ ...DEFAULT_PREFERENCES, drafts: {} }));
  view.rerender({ id: B });
  expect(view.result.current.problem).toBeNull();
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    const change = (await request.json()) as object;
    sent.push(change);
    return Response.json({ ...DEFAULT_PREFERENCES, ...preferences(), ...change });
  }));
  act(() => view.result.current.edit("text b"));
  await act(() => sendWaiting());
  expect(sent).toHaveLength(1);
  expect(sent[0]).toEqual({ drafts: { [`chat:${A}`]: { text: "text a", base: null }, [`chat:${B}`]: { text: "text b", base: null } } });
  view.rerender({ id: A });
  expect(view.result.current.text).toBe("text a");
  expect(view.result.current.problem).toBeNull();
  act(() => receivePreferences({ ...DEFAULT_PREFERENCES, drafts: {} }));
  expect(view.result.current.problem).toMatch(/^Draft not saved/);
});

test("at the limit, editing a kept draft keeps its place from an earlier overflow", () => {
  const full = Object.fromEntries([[`chat:${A}`, { text: "kept a", base: null }], ...Array.from({ length: 199 }, (_, i) => [`other:${i}`, { text: "x", base: null }])]);
  receivePreferences({ ...DEFAULT_PREFERENCES, drafts: full });
  const view = mount(B);
  act(() => view.result.current.edit("overflow b"));
  view.rerender({ id: A });
  act(() => view.result.current.edit("edited a"));
  expect(preferences().drafts[`chat:${A}`]).toEqual({ text: "edited a", base: null });
  expect(`chat:${B}` in preferences().drafts).toBe(false);
  expect(Object.keys(preferences().drafts)).toHaveLength(200);
});

test("a delete the server did not take goes again with the next save", async () => {
  receivePreferences({ ...DEFAULT_PREFERENCES, drafts: { [`chat:${A}`]: { text: "old", base: null } } });
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "down" }, { status: 503 })));
  vi.spyOn(console, "error").mockImplementation(() => {});
  const view = mount(A);
  act(() => view.result.current.edit(""));
  await act(() => sendWaiting());
  act(() => receivePreferences({ ...DEFAULT_PREFERENCES, drafts: { [`chat:${A}`]: { text: "old", base: null } } }));
  expect(view.result.current.text).toBe("");
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    const change = (await request.json()) as object;
    sent.push(change);
    return Response.json({ ...DEFAULT_PREFERENCES, ...preferences(), ...change });
  }));
  view.rerender({ id: B });
  act(() => view.result.current.edit("b"));
  await act(() => sendWaiting());
  expect(sent).toEqual([{ drafts: { [`chat:${B}`]: { text: "b", base: null } } }]);
});

test.each([
  ["newer text", "new", { text: "new", base: null }],
  ["a clear", "", undefined],
])("after a failed adoption, %s here is what the next boot keeps", async (_, typed, kept) => {
  const first = new Session(vi.fn());
  const one = mount(A, { session: first });
  act(() => one.result.current.edit("old"));
  first.leaveForLogin();
  one.unmount();
  await sendWaiting();
  receivePreferences({ ...DEFAULT_PREFERENCES, drafts: {} });
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "down" }, { status: 503 })));
  vi.spyOn(console, "error").mockImplementation(() => {});
  await adoptLocalUserData(ADA);
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => Response.json({ ...preferences(), ...((await request.json()) as object) })));
  const two = mount(A, { session: new Session(vi.fn()) });
  act(() => two.result.current.edit("draft"));
  act(() => two.result.current.edit(typed));
  await act(() => sendWaiting());
  two.unmount();
  await adoptLocalUserData(ADA);
  expect(preferences().drafts[`chat:${A}`]).toEqual(kept);
});

test("a failed adoption still brings back untouched held text on the next boot", async () => {
  const first = new Session(vi.fn());
  const one = mount(A, { session: first });
  act(() => one.result.current.edit("held"));
  first.leaveForLogin();
  one.unmount();
  await sendWaiting();
  receivePreferences({ ...DEFAULT_PREFERENCES, drafts: {} });
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "down" }, { status: 503 })));
  vi.spyOn(console, "error").mockImplementation(() => {});
  await adoptLocalUserData(ADA);
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => Response.json({ ...preferences(), ...((await request.json()) as object) })));
  const two = mount(B, { session: new Session(vi.fn()) });
  act(() => two.result.current.edit("other"));
  await act(() => sendWaiting());
  await adoptLocalUserData(ADA);
  expect(preferences().drafts[`chat:${A}`]).toEqual({ text: "held", base: null });
});

test("typing writes this browser's held drafts only when it holds this canvas's", () => {
  const held = `trackinizer.v2.drafts.${ADA}`;
  localStorage.setItem(held, JSON.stringify({ "other:x": { text: "held", base: null } }));
  const view = mount(A);
  const write = vi.spyOn(Storage.prototype, "setItem");
  for (const typed of ["a", "ab", "abc"]) act(() => view.result.current.edit(typed));
  expect(write.mock.calls.filter(([name]) => name === held)).toHaveLength(0);
  write.mockRestore();
  expect(JSON.parse(localStorage.getItem(held)!)).toEqual({ "other:x": { text: "held", base: null } });
});

test("a copy another tab holds after this one forgot its own is forgotten on the next edit here", () => {
  const view = mount(A);
  act(() => view.result.current.edit("here"));
  const held = `trackinizer.v2.drafts.${ADA}`;
  localStorage.setItem(held, JSON.stringify({ [`chat:${A}`]: { text: "other tab", base: null }, "other:x": { text: "keep", base: null } }));
  dispatchEvent(new StorageEvent("storage", { key: held }));
  act(() => view.result.current.edit("here again"));
  expect(JSON.parse(localStorage.getItem(held)!)).toEqual({ "other:x": { text: "keep", base: null } });
});

test("an unchanged draft sends nothing", async () => {
  const view = mount(A);
  act(() => view.result.current.edit("same"));
  await act(() => sendWaiting());
  const count = sent.length;
  act(() => view.result.current.edit("same"));
  await act(() => sendWaiting());
  expect(sent).toHaveLength(count);
});

test("no canvas: a stale clear does nothing after an edit away and back", () => {
  const view = mount(null);
  act(() => view.result.current.edit("same"));
  const stale = view.result.current.clear;
  act(() => view.result.current.edit("away"));
  act(() => view.result.current.edit("same"));
  act(() => stale());
  expect(view.result.current.text).toBe("same");
  act(() => view.result.current.clear());
  expect(view.result.current.text).toBe("");
});

test("no canvas: text is local and nothing is kept", () => {
  const view = mount(null);
  act(() => view.result.current.edit("loose"));
  expect(view.result.current.text).toBe("loose");
  expect(preferences().drafts).toEqual({});
  act(() => view.result.current.clear());
  expect(view.result.current.text).toBe("");
});
