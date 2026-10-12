import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { stubFetch } from "../api/testing";
import type { Draft } from "../create/draft";
import { DEFAULT_PREFERENCES, type Preferences, preferences, receivePreferences } from "../preferences";
import { adoptLocalUserData, keepCreateDraftHere, keepFieldDraftHere } from "./local";
import { EMPTY_STATE, storageKey } from "./value";

const EMAIL = "ada@example.com";
const VIEW = { id: "v1", name: "Mine", request: { kinds: ["Issue"], filters: [{ field: "owner", op: "is" as const, value: "ada" }] } };
const TILES = { chat: { collapsed: true, place: null } };

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  receivePreferences(DEFAULT_PREFERENCES);
});

/** A server that merges what it is sent over the preferences, as `PATCH /api/me/preferences` does. */
function server() {
  let kept = preferences();
  return stubFetch(async (request) => {
    kept = { ...kept, ...(await request.json() as Partial<Preferences>) };
    return Response.json(kept);
  });
}

const bodies = (sent: ReturnType<typeof stubFetch>) => sent.map(({ body }) => body);

function holdState(parts: Partial<typeof EMPTY_STATE>) {
  localStorage.setItem(storageKey(EMAIL), JSON.stringify({ ...EMPTY_STATE, ...parts }));
}

test("an old browser's state merges into the user's, is sent once, and leaves only its tiles", async () => {
  receivePreferences({ ...DEFAULT_PREFERENCES, stars: ["s1"], people: { dan: { name: "Daniel", type: "person" } } });
  holdState({
    stars: ["s1", "s2"],
    views: [VIEW],
    aliases: ["ada"],
    people: { dan: { name: "Dan", type: "person" }, eve: { name: "Eve", type: "agent" } },
    read: { boundary: "2026-10-01T00:00:00Z", marks: ["c1"] },
    ui: { collapsed: ["browse"], lens: "graph", tiles: TILES },
  });
  const sent = server();
  await adoptLocalUserData(EMAIL);
  expect(bodies(sent)).toEqual([
    {
      stars: ["s1", "s2"],
      views: [VIEW],
      aliases: ["ada"],
      people: { dan: { name: "Daniel", type: "person" }, eve: { name: "Eve", type: "agent" } },
      read: { boundary: "2026-10-01T00:00:00Z", marks: ["c1"] },
      collapsed: ["browse"],
      lens: "graph",
    },
  ]);
  expect(JSON.parse(localStorage.getItem(storageKey(EMAIL))!)).toEqual({ ...EMPTY_STATE, ui: { ...EMPTY_STATE.ui, tiles: TILES } });
  // The next boot finds nothing to send.
  await adoptLocalUserData(EMAIL);
  expect(sent).toHaveLength(1);
});

test("a state with no tiles is removed; folded sections and the lens the server holds are kept", async () => {
  receivePreferences({ ...DEFAULT_PREFERENCES, collapsed: ["theirs"], lens: "table" });
  holdState({ stars: ["s"], ui: { collapsed: ["mine"], lens: "graph", tiles: {} } });
  const sent = server();
  await adoptLocalUserData(EMAIL);
  expect(bodies(sent)).toEqual([{ stars: ["s"] }]);
  expect(localStorage.length).toBe(0);
});

test("a state this build cannot read is left alone, and the drafts beside it still come over", async () => {
  const unreadable = JSON.stringify({ version: 3, stars: ["from a newer build"] });
  localStorage.setItem(storageKey(EMAIL), unreadable);
  keepFieldDraftHere(EMAIL, "i1:title", { text: "Words", base: "Old" });
  const sent = server();
  await adoptLocalUserData(EMAIL);
  expect(bodies(sent)).toEqual([{ drafts: { "i1:title": { text: "Words", base: "Old" } } }]);
  expect(localStorage.getItem(storageKey(EMAIL))).toBe(unreadable);
  expect(localStorage.length).toBe(1);
});

test("drafts kept here replace the server's draft of the same field and leave its others", async () => {
  receivePreferences({ ...DEFAULT_PREFERENCES, drafts: { "i1:title": { text: "Server", base: null }, "i2:title": { text: "Other", base: null } } });
  keepFieldDraftHere(EMAIL, "i1:title", { text: "Here", base: undefined });
  keepFieldDraftHere(EMAIL, "i3:venue", { text: "ICML", base: "NeurIPS" });
  localStorage.setItem(`trackinizer.v2.drafts.${EMAIL}`, JSON.stringify({ ...JSON.parse(localStorage.getItem(`trackinizer.v2.drafts.${EMAIL}`)!), "i4:x": { nothing: 1 } }));
  const sent = server();
  await adoptLocalUserData(EMAIL);
  expect(bodies(sent)).toEqual([
    {
      drafts: {
        "i1:title": { text: "Here", base: null },
        "i2:title": { text: "Other", base: null },
        "i3:venue": { text: "ICML", base: "NeurIPS" },
      },
    },
  ]);
  expect(localStorage.length).toBe(0);
});

const CREATE: Draft = {
  kind: "Issue",
  title: "Half",
  description: "",
  fields: {},
  chips: { priority: 20 },
  relations: [{ option: { key: "narrows:out", label: "Narrows", targetKinds: ["Issue"], edge: { edgeKind: "narrows", direction: "out" } }, target: { id: "i1", kind: "Issue", seq: 1, title: "Parent" } }],
};

test("the create form's draft kept here is sent with its relations by option key, and replaces the server's", async () => {
  receivePreferences({ ...DEFAULT_PREFERENCES, create_draft: { kind: "Belief", title: "Server", description: "", fields: {}, chips: {}, relations: [] } });
  keepCreateDraftHere(EMAIL, CREATE);
  const sent = server();
  await adoptLocalUserData(EMAIL);
  expect(bodies(sent)).toEqual([
    {
      create_draft: {
        kind: "Issue",
        title: "Half",
        description: "",
        fields: {},
        chips: { priority: 20 },
        relations: [{ option: "narrows:out", target: { id: "i1", kind: "Issue", seq: 1, title: "Parent" } }],
      },
    },
  ]);
  expect(localStorage.length).toBe(0);
});

test("a create draft another build left in another shape is dropped, and nothing is sent for it", async () => {
  localStorage.setItem(`trackinizer.v2.create.${EMAIL}`, JSON.stringify({ kind: "Issue", title: "Old shape" }));
  const sent = server();
  await adoptLocalUserData(EMAIL);
  expect(sent).toEqual([]);
  expect(localStorage.length).toBe(0);
});

test("a send that fails keeps everything for the next load; another user's keys are never touched", async () => {
  holdState({ stars: ["s"] });
  keepFieldDraftHere(EMAIL, "i1:title", { text: "Words", base: null });
  localStorage.setItem(storageKey("grace@example.com"), JSON.stringify({ ...EMPTY_STATE, stars: ["g"] }));
  stubFetch(() => Response.json({ detail: "offline" }, { status: 503 }));
  await adoptLocalUserData(EMAIL);
  expect(localStorage.length).toBe(3);

  const sent = server();
  await adoptLocalUserData(EMAIL);
  expect(sent).toHaveLength(1);
  expect(Object.keys(localStorage)).toEqual([storageKey("grace@example.com")]);
});

test("nothing held sends nothing; storage that cannot be read is no failure", async () => {
  const sent = server();
  await adoptLocalUserData(EMAIL);
  expect(sent).toEqual([]);
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new DOMException("denied", "SecurityError");
  });
  await adoptLocalUserData(EMAIL);
  expect(sent).toEqual([]);
  vi.restoreAllMocks();
});

test("a draft kept as the session ends joins the ones already kept, and storage that is full loses only itself", () => {
  keepFieldDraftHere(EMAIL, "i1:title", { text: "A", base: null });
  keepFieldDraftHere(EMAIL, "i2:title", { text: "B", base: null });
  expect(JSON.parse(localStorage.getItem(`trackinizer.v2.drafts.${EMAIL}`)!)).toEqual({
    "i1:title": { text: "A", base: null },
    "i2:title": { text: "B", base: null },
  });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new DOMException("full", "QuotaExceededError");
  });
  expect(() => keepFieldDraftHere(EMAIL, "i3:title", { text: "C", base: null })).not.toThrow();
  expect(() => keepCreateDraftHere(EMAIL, CREATE)).not.toThrow();
  vi.restoreAllMocks();
});
