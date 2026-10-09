import { afterEach, describe, expect, test, vi } from "vitest";
import { type BrowserState, EMPTY_STATE, exportState, mergeImport, parseState, type SavedView } from "./value";

const ISSUES: SavedView = {
  id: "v1",
  name: "My issues",
  request: { kinds: ["Issue"], filters: [{ field: "account", op: "is", value: "ada@example.com" }] },
};

function state(parts: Partial<BrowserState>): BrowserState {
  return { ...EMPTY_STATE, ...parts };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

/** New ids for copies, in order: `new1`, `new2`, … */
function ids(): () => string {
  let n = 0;
  return () => `new${++n}`;
}

describe("an import merges into this browser's state and deletes nothing", () => {
  test("stars are a union: this browser's first, then the new ones, each once", () => {
    const merged = mergeImport(state({ stars: ["a", "b"] }), state({ stars: ["b", "c"] }));
    expect(merged.stars).toEqual(["a", "b", "c"]);
  });

  test("aliases are a union", () => {
    const merged = mergeImport(state({ aliases: ["dan"] }), state({ aliases: ["Agent", "dan"] }));
    expect(merged.aliases).toEqual(["dan", "Agent"]);
  });

  test("people are a union, and a person named here keeps this browser's name", () => {
    const merged = mergeImport(
      state({ people: { "josh@example.com": { name: "Josh", type: "person" } } }),
      state({
        people: {
          "josh@example.com": { name: "Joshua", type: "person" },
          "craftax-arm": { name: "craftax-arm", type: "agent" },
        },
      }),
    );
    expect(merged.people).toEqual({
      "josh@example.com": { name: "Josh", type: "person" },
      "craftax-arm": { name: "craftax-arm", type: "agent" },
    });
  });

  test("a view with a new id is added; an identical one is skipped", () => {
    const beliefs: SavedView = { id: "v2", name: "Beliefs", request: { kinds: ["Belief"], filters: [] } };
    const merged = mergeImport(state({ views: [ISSUES] }), state({ views: [ISSUES, beliefs] }), ids());
    expect(merged.views).toEqual([ISSUES, beliefs]);
  });

  test("an identical view is skipped whatever order its keys are in", () => {
    const reordered = { request: { filters: ISSUES.request.filters, kinds: ["Issue"] }, name: "My issues", id: "v1" };
    expect(mergeImport(state({ views: [ISSUES] }), state({ views: [reordered] }), ids()).views).toEqual([ISSUES]);
  });

  test("a view whose id holds different content here is added as an imported copy with a new id", () => {
    const theirs: SavedView = { ...ISSUES, request: { ...ISSUES.request, filters: [] } };
    const merged = mergeImport(state({ views: [ISSUES] }), state({ views: [theirs] }), ids());
    expect(merged.views).toEqual([ISSUES, { ...theirs, id: "new1", name: "My issues (imported)" }]);
  });

  test("an imported copy gets a new id on a page over plain HTTP, which lacks crypto.randomUUID (B1)", () => {
    const real = globalThis.crypto;
    vi.stubGlobal("crypto", { getRandomValues: real.getRandomValues.bind(real) });
    const theirs: SavedView = { ...ISSUES, name: "Renamed" };
    const [, copy] = mergeImport(state({ views: [ISSUES] }), state({ views: [theirs] })).views;
    expect(copy!.id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  });

  test("importing the same file twice adds its imported copy once", () => {
    const theirs: SavedView = { ...ISSUES, name: "Renamed" };
    const once = mergeImport(state({ views: [ISSUES] }), state({ views: [theirs] }), ids());
    const twice = mergeImport(once, state({ views: [theirs] }), ids());
    expect(twice.views).toEqual(once.views);
    expect(twice.views.map((view) => view.name)).toEqual(["My issues", "Renamed (imported)"]);
  });

  test("read state takes the later boundary and the union of the marks", () => {
    const early = "2026-09-20T10:00:00+00:00";
    const late = "2026-09-26T08:30:00+02:00";
    for (const [mine, theirs] of [
      [early, late],
      [late, early],
    ]) {
      const merged = mergeImport(
        state({ read: { boundary: mine, marks: ["c1"] } }),
        state({ read: { boundary: theirs, marks: ["c2", "c1"] } }),
      );
      expect(merged.read).toEqual({ boundary: late, marks: ["c1", "c2"] });
    }
    expect(mergeImport(state({}), state({ read: { boundary: early, marks: [] } })).read.boundary).toBe(early);
    expect(mergeImport(state({ read: { boundary: early, marks: [] } }), state({})).read.boundary).toBe(early);
  });

  test("UI state is not imported: this browser's stays", () => {
    const mine = state({ ui: { collapsed: ["browse"], lens: "lineage", tiles: { "trax.chat": { collapsed: true, place: null } } } });
    const merged = mergeImport(mine, state({ ui: { collapsed: ["views"], lens: "details", tiles: {} } }));
    expect(merged.ui).toEqual(mine.ui);
  });

  test("everything this browser holds is still there after importing an empty state", () => {
    const mine = state({
      stars: ["a"],
      views: [ISSUES],
      aliases: ["dan"],
      people: { bob: { name: "Bob", type: "person" } },
      read: { boundary: "2026-09-20T10:00:00+00:00", marks: ["c1"] },
      ui: { collapsed: ["browse"], lens: null, tiles: {} },
    });
    expect(mergeImport(mine, EMPTY_STATE)).toEqual(mine);
  });
});

describe("parsing an export", () => {
  test("an export parses back to the state it was made from", () => {
    const full = state({
      stars: ["a"],
      views: [ISSUES],
      aliases: ["dan"],
      people: { bob: { name: "Bob", type: "person" } },
      read: { boundary: "2026-09-20T10:00:00+00:00", marks: ["c1"] },
      ui: { collapsed: ["browse"], lens: "lineage", tiles: { "trax.chat": { collapsed: true, place: { left: 12, top: 40.5 } } } },
    });
    expect(parseState(JSON.parse(exportState(full)))).toEqual(full);
  });

  test("a category the file lacks is empty", () => {
    expect(parseState({ version: 2, stars: ["a"] })).toEqual(state({ stars: ["a"] }));
  });

  test("a version 1 value, from before floating tiles were remembered, reads as the current version with none", () => {
    const old = { version: 1, stars: ["a"], ui: { collapsed: ["browse"], lens: "lineage" } };
    expect(parseState(old)).toEqual(state({ stars: ["a"], ui: { collapsed: ["browse"], lens: "lineage", tiles: {} } }));
    expect(EMPTY_STATE.version).toBe(2);
  });

  test("a file of the wrong shape is refused, saying what is wrong", () => {
    const refusals: [unknown, string][] = [
      [null, "This is not an export of Trackinizer's browser state."],
      [[1], "This is not an export of Trackinizer's browser state."],
      [{ stars: [] }, "This is not an export of Trackinizer's browser state."],
      [{ version: 3 }, "This export is version 3; this build reads version 2. Reload for the latest build."],
      [{ version: 0 }, "This export is version 0; this build reads version 2. Reload for the latest build."],
      [{ version: 1, stars: "a" }, "stars must be a list."],
      [{ version: 1, aliases: ["dan", 7] }, "aliases[1] must be text."],
      [{ version: 1, views: [{ id: "v", name: "x" }] }, "views[0].request must be an object."],
      [
        { version: 1, views: [{ id: "v", name: "x", request: { kinds: [], filters: [{ field: "f", op: "like", value: "" }] } }] },
        "views[0].request.filters[0].op must be one of is, ne, re, nre, lt, le, gt, ge, isnull, notnull.",
      ],
      [{ version: 1, people: { bob: { name: "Bob", type: "robot" } } }, "people.bob.type must be person or agent."],
      [{ version: 1, read: { boundary: 5 } }, "read.boundary must be text."],
      [{ version: 1, read: { boundary: "nonsense" } }, "read.boundary must be a time, such as 2026-09-27T10:00:00Z."],
      [{ version: 1, ui: [] }, "ui must be an object."],
      [{ version: 2, ui: { tiles: { chat: {} } } }, "ui.tiles.chat.collapsed must be true or false."],
      [{ version: 2, ui: { tiles: { chat: { collapsed: false, place: { left: "a", top: 1 } } } } }, "ui.tiles.chat.place.left must be a number."],
      [{ version: 2, ui: { tiles: { chat: { collapsed: false, place: { left: 1, top: null } } } } }, "ui.tiles.chat.place.top must be a number."],
    ];
    for (const [value, message] of refusals) {
      expect(() => parseState(value), JSON.stringify(value)).toThrow(new Error(message));
    }
  });
});
