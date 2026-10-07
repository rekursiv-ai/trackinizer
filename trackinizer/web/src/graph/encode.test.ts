import { afterEach, expect, test } from "vitest";
import { linkLook, nodeLook, readPalette } from "./encode";
import { PALETTE } from "./testing";

afterEach(() => {
  document.documentElement.removeAttribute("style");
});

/** A node as the look reads it: an active Issue with no edges unless `fields` say otherwise. */
const shape = (fields: Partial<Parameters<typeof nodeLook>[0]> = {}) => ({
  kind: "Issue",
  status: "active",
  degree: 0,
  root: false,
  ...fields,
});

test("a node fills with its kind's hue and rings with its status's; an unknown kind or status falls back", () => {
  expect(nodeLook(shape(), PALETTE)).toMatchObject({ fill: "issue-hue", ring: "active-ring", fillAlpha: 1, ringAlpha: 1 });
  expect(nodeLook(shape({ kind: "Gadget", status: "paused" }), PALETTE)).toMatchObject({ fill: "fallback", ring: "fallback" });
});

test("a node's radius grows with the square root of its degree up to 14; a root Issue is the largest", () => {
  const radius = (fields: Partial<Parameters<typeof nodeLook>[0]>) => nodeLook(shape(fields), PALETTE).radius;
  expect(radius({ degree: 0 })).toBe(4);
  expect(radius({ degree: 4 })).toBeCloseTo(8.4);
  expect(radius({ degree: 100 })).toBe(14);
  expect(radius({ root: true })).toBe(16);
  expect(radius({ root: true, degree: 100 })).toBe(18);
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

test("a node gets no halo, the strong one as the focus or the selection, the match one as a search match, or the highlight one as pointed at", () => {
  expect(nodeLook(shape(), PALETTE).halo).toBeNull();
  expect(nodeLook(shape(), PALETTE, { halo: "strong" }).halo).toBe("halo");
  expect(nodeLook(shape(), PALETTE, { halo: "match", dimmed: true }).halo).toBe("match");
  expect(nodeLook(shape(), PALETTE, { halo: "highlight" }).halo).toBe("highlight");
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
    ["--amber", "#bbbbbb"],
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
    highlight: "#bbbbbb",
  });
});
