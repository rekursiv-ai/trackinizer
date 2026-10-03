import { describe, expect, it } from "vitest";
import { orderVisuals } from "./layout";

describe("canvas placement", () => {
  const visuals = [
    { id: "side", placement: "side" },
    { id: "float", placement: "floating" },
    { id: "main", placement: "main" },
  ] as const;

  it("places main visuals before side visuals and floating visuals last", () => {
    expect(orderVisuals(visuals, null).map((visual) => visual.id)).toEqual(["main", "side", "float"]);
  });

  it("brings the focused floating visual to the front", () => {
    const withTwoFloats = [...visuals, { id: "float2", placement: "floating" as const }];
    expect(orderVisuals(withTwoFloats, "float").map((visual) => visual.id)).toEqual([
      "main", "side", "float2", "float",
    ]);
  });
});
