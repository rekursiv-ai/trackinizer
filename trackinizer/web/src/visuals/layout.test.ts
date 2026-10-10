import { describe, expect, it } from "vitest";
import { chatHome, orderVisuals } from "./layout";

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

  it("docks Chat where it was placed, main strip or either side; a stored floating reads as the right", () => {
    expect([chatHome("main"), chatHome("left"), chatHome("side"), chatHome("floating"), chatHome(undefined)])
      .toEqual(["main", "left", "side", "side", "side"]);
  });
});
