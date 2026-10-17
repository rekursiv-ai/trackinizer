import { describe, expect, it } from "vitest";
import { arrangeRecordPanes, orderVisuals } from "./layout";

describe("canvas placement", () => {
  const visuals = [
    { id: "side", placement: "side" },
    { id: "float", placement: "floating" },
    { id: "left", placement: "left" },
    { id: "main", placement: "main" },
  ] as const;

  it("places main visuals before the visuals at either side and floating visuals last", () => {
    expect(orderVisuals(visuals, null).map((visual) => visual.id)).toEqual(["main", "left", "side", "float"]);
  });

  it("brings the focused floating visual to the front", () => {
    const withTwoFloats = [...visuals, { id: "float2", placement: "floating" as const }];
    expect(orderVisuals(withTwoFloats, "float").map((visual) => visual.id)).toEqual([
      "main", "left", "side", "float2", "float",
    ]);
  });
});

describe("record panes", () => {
  const pane = (id: string, placement: "main" | "left" | "side" | "floating" = "left") => ({ id, type: "trax.timeline", placement, record_id: `r-${id}` });
  const placed = (visuals: readonly { readonly id: string; readonly placement?: string }[]) => visuals.map(({ id, placement }) => [id, placement]);

  it("fills the canvas with one and stands two side by side, whatever their stored place", () => {
    expect(placed(arrangeRecordPanes([pane("a")], null))).toEqual([["a", "main"]]);
    expect(placed(arrangeRecordPanes([pane("a", "side"), pane("b")], null))).toEqual([["a", "main"], ["b", "main"]]);
  });

  it("puts the focused one of three big on the left and stacks the others on the right in order", () => {
    expect(placed(arrangeRecordPanes([pane("a"), pane("b"), pane("c")], "c"))).toEqual([["a", "side"], ["b", "side"], ["c", "main"]]);
    expect(placed(arrangeRecordPanes([pane("a"), pane("b"), pane("c")], null))).toEqual([["a", "main"], ["b", "side"], ["c", "side"]]);
  });

  it("leaves floating windows, and any canvas holding the page or more panes, as placed", () => {
    expect(placed(arrangeRecordPanes([pane("a"), pane("f", "floating")], null))).toEqual([["a", "main"], ["f", "floating"]]);
    const withPage = [{ id: "p", type: "trax.browse", placement: "main" as const, record_id: null }, pane("a")];
    expect(placed(arrangeRecordPanes(withPage, null))).toEqual([["p", "main"], ["a", "left"]]);
    const four = ["a", "b", "c", "d"].map((id) => pane(id));
    expect(placed(arrangeRecordPanes(four, null))).toEqual(four.map(({ id }) => [id, "left"]));
  });
});
