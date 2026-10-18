import { cleanup, render } from "@testing-library/react";
import { useContext } from "react";
import { afterEach, expect, test } from "vitest";
import { CaptureContext, CaptureRegistry, type CaptureSource } from "./stageRegistry";

afterEach(cleanup);

const source: CaptureSource = { stage: document.createElement("div") };

test("the registry offers the source set last, and nothing once withdrawn", () => {
  const registry = new CaptureRegistry();
  expect(registry.get()).toBeNull();
  registry.set(source);
  expect(registry.get()).toBe(source);
  registry.set(null);
  expect(registry.get()).toBeNull();
});

test("outside a stream there is no registry to offer a stage to", () => {
  let seen: CaptureRegistry | null | undefined;
  function Probe() {
    seen = useContext(CaptureContext);
    return null;
  }
  render(<Probe />);
  expect(seen).toBeNull();
});
