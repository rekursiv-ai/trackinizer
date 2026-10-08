import { afterEach, expect, test } from "vitest";
import { linkLook, nodeLook, panelKind, panelTint, panelValence, readPalette } from "./encode";
import { PALETTE } from "./testing";

afterEach(() => {
  document.documentElement.removeAttribute("style");
});

/** A node as the look reads it: an active Issue with no edges unless `fields` say otherwise. */
const shape = (fields: Partial<Parameters<typeof nodeLook>[0]> = {}) => ({
  kind: "Issue",
  status: "active",
  degree: 0,
  authority: 0,
  root: false,
  ...fields,
});

test("a node fills with its kind's hue and rings with its status's; an unknown kind or status falls back", () => {
  expect(nodeLook(shape(), PALETTE)).toMatchObject({ fill: "issue-hue", ring: "active-ring", fillAlpha: 1, ringAlpha: 1 });
  expect(nodeLook(shape({ kind: "Gadget", status: "paused" }), PALETTE)).toMatchObject({ fill: "fallback", ring: "fallback" });
});

test("a node's radius is its kind's base, lifted by its authority; the mass stays small", () => {
  const radius = (fields: Partial<Parameters<typeof nodeLook>[0]>) => nodeLook(shape(fields), PALETTE).radius;
  expect(radius({ authority: 0 })).toBe(2.9);
  expect(radius({ authority: 0.5 })).toBeCloseTo(2.9 + 6.5 * 0.5);
  expect(radius({ authority: 1 })).toBeCloseTo(2.9 + 6.5);
});

test("an abandoned or invalid node recedes to a quarter", () => {
  for (const status of ["abandoned", "invalid"]) {
    expect(nodeLook(shape({ status }), PALETTE)).toMatchObject({ fillAlpha: 0.25, ringAlpha: 0.25, ring: `${status}-ring` });
  }
});

test("a node dimmed outside a focus recedes further than a retired one, so the two never look alike (S12), and loses a proven dot", () => {
  expect(nodeLook(shape(), PALETTE, { dimmed: true })).toMatchObject({ fillAlpha: 0.14, ringAlpha: 0.14, ring: "active-ring" });
  expect(nodeLook(shape({ status: "abandoned" }), PALETTE, { dimmed: true })).toMatchObject({ fillAlpha: 0.14, ringAlpha: 0.14 });
  expect(nodeLook(shape({ kind: "Belief", judgement: "proven" }), PALETTE, { dimmed: true }).dot).toBeNull();
  expect(nodeLook(shape({ kind: "Belief", judgement: "disproven" }), PALETTE, { dimmed: true })).toMatchObject({ fillAlpha: 0, ringAlpha: 0.14 });
});

test("a node two or more hops out fades to 0.7 of its look; a retired one keeps its own look inside the focus", () => {
  expect(nodeLook(shape(), PALETTE, { far: true })).toMatchObject({ fillAlpha: 0.7, ringAlpha: 0.7 });
  expect(nodeLook(shape({ status: "invalid" }), PALETTE, { far: true })).toMatchObject({ fillAlpha: expect.closeTo(0.175), ringAlpha: expect.closeTo(0.175) });
  expect(nodeLook(shape({ status: "invalid" }), PALETTE)).toMatchObject({ fillAlpha: 0.25, ringAlpha: 0.25 });
});

test("a node gets no halo, the strong one as the focus or the selection, or the match one as a search match", () => {
  expect(nodeLook(shape(), PALETTE).halo).toBeNull();
  expect(nodeLook(shape(), PALETTE, { halo: "strong" }).halo).toBe("halo");
  expect(nodeLook(shape(), PALETTE, { halo: "match", dimmed: true }).halo).toBe("match");
});

test("a Belief shows its confidence as fill and its judgement as the glyph", () => {
  const belief = (fields: Partial<Parameters<typeof nodeLook>[0]>) => nodeLook(shape({ kind: "Belief", ...fields }), PALETTE);
  expect(belief({ confidence: 0.5 })).toMatchObject({ fill: "belief-hue", fillAlpha: expect.closeTo(0.675), wash: null, dot: null });
  expect(belief({ confidence: 0 }).fillAlpha).toBeCloseTo(0.35);
  // Disproven is hollow, ringed in its own hue.
  expect(belief({ judgement: "disproven", confidence: 0.9 })).toMatchObject({ fillAlpha: 0, ring: "belief-hue", ringAlpha: 1 });
  // Undecidable is half-toned: the background washed over at 0.4 of the fill.
  expect(belief({ judgement: "undecidable", confidence: 1 })).toMatchObject({ fillAlpha: 1, wash: "background", washAlpha: 0.4 });
  // Proven has an inner dot, unless it recedes.
  expect(belief({ judgement: "proven" }).dot).toBe("complete-ring");
  expect(belief({ judgement: "proven", status: "invalid" }).dot).toBeNull();
  // Another kind's stray confidence is not a Belief's.
  expect(nodeLook(shape({ confidence: 0 }), PALETTE).fillAlpha).toBe(1);
});

test("a labelled node carries its label in the text colour over the background, cut at 40 characters", () => {
  expect(nodeLook(shape(), PALETTE).label).toBeNull();
  expect(nodeLook(shape(), PALETTE, { label: "Achieve 100% on ARC" }).label).toEqual({ text: "Achieve 100% on ARC", color: "halo", outline: "background" });
  expect(nodeLook(shape(), PALETTE, { label: "x".repeat(41) }).label?.text).toBe(`${"x".repeat(39)}…`);
});

test("an edge with a valence draws in the support or against colour, as wide as its weight", () => {
  expect(linkLook({ kind: "proves", valence: 0.8 }, 50, PALETTE)).toEqual({ color: "support", width: expect.closeTo(3.55), dash: null });
  expect(linkLook({ kind: "favors", valence: -0.5 }, 0, PALETTE)).toEqual({ color: "against", width: expect.closeTo(2.5), dash: [4, 3] });
});

test("any other edge draws in its kind's hue, heavier by its busier end's degree, at most 5", () => {
  expect(linkLook({ kind: "narrows" }, 4, PALETTE)).toEqual({ color: "narrows-hue", width: expect.closeTo(1.95), dash: null });
  expect(linkLook({ kind: "cites_paper" }, 1_000, PALETTE)).toEqual({ color: "cites-hue", width: 5, dash: null });
  expect(linkLook({ kind: "supersedes" }, 0, PALETTE).color).toBe("fallback");
});

test("an edge dimmed around a focus draws in the faint colour, valence or not", () => {
  expect(linkLook({ kind: "narrows" }, 4, PALETTE, true).color).toBe("faint");
  expect(linkLook({ kind: "proves", valence: 0.8 }, 0, PALETTE, true).color).toBe("faint");
});

test("the palette is the theme's tokens for every kind, edge kind and status, or the fallback where one is unset", () => {
  const root = document.documentElement;
  for (const [name, value] of [
    ["--g-kind-Issue", "#111111"],
    ["--g-edge-cites_paper", "#222222"],
    ["--g-status-active", "#333333"],
    ["--g-fallback", "#444444"],
    ["--bg", "#555555"],
    ["--l-evidence", "#666666"],
    ["--l-against", "#777777"],
    ["--g-faint", "#888888"],
    ["--text", "#999999"],
    ["--accent-hover", "#aaaaaa"],
    ["--g-duo-record", "#bbbbbb"],
    ["--g-duo-belief", "#cccccc"],
  ]) {
    root.style.setProperty(name!, ` ${value}`);
  }
  expect(readPalette(root, { kinds: ["Issue", "Paper"], edgeKinds: ["cites_paper", "narrows"], statuses: ["active"] })).toEqual({
    kinds: { Issue: "#111111", Paper: "#444444" },
    edges: { cites_paper: "#222222", narrows: "#444444" },
    status: { active: "#333333" },
    fallback: "#444444",
    background: "#555555",
    support: "#666666",
    against: "#777777",
    faint: "#888888",
    halo: "#999999",
    match: "#aaaaaa",
    duoRecord: "#bbbbbb",
    duoBelief: "#cccccc",
  });
});

test("the graph's own valence and ground tokens beat the app's list colours", () => {
  const root = document.documentElement;
  for (const [name, value] of [
    ["--g-vpos", "#9fd4a8"],
    ["--g-vneg", "#f5b07a"],
    ["--g-bg", "#000"],
    ["--g-kind-Issue", "#6e8efb"],
    ["--l-evidence", "#666666"],
    ["--l-against", "#777777"],
    ["--bg", "#555555"],
  ]) {
    root.style.setProperty(name!, value);
  }
  const palette = readPalette(root, { kinds: ["Issue"], edgeKinds: [], statuses: [] });
  expect([palette.support, palette.against, palette.background]).toEqual(["#9fd4a8", "#f5b07a", "#000"]);
});

test("a panel wears a reference's hue one step lighter than the canvas draws it", () => {
  // The design's PCOL and PVPOS/PVNEG: `Kind#seq` in prose is small text, and the
  // disc's hue at that size reads muddy, so each is lifted toward white.
  const palette = { ...PALETTE, kinds: { Issue: "#6e8efb" } };
  expect(panelKind(palette, "Issue")).toBe("#97aefc");
  expect(panelTint("#6e8efb")).toBe("#97aefc");
  expect(panelValence("#9fd4a8")).toBe("#b2ddb9");
  expect(panelValence("#f5b07a")).toBe("#f7c095");
});
