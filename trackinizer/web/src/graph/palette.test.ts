import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { useThemePalette } from "./palette";
import { META } from "./testing";

afterEach(() => {
  cleanup();
  document.documentElement.style.removeProperty("--g-kind-Issue");
  delete document.documentElement.dataset.skin;
  document.documentElement.dataset.theme = "dark";
});

test("the palette is read again when the theme or the skin changes, so a node takes the new colour", async () => {
  document.documentElement.dataset.theme = "dark";
  const root = document.documentElement.style;
  root.setProperty("--g-kind-Issue", "#111111");
  const { result } = renderHook(() => useThemePalette(META));
  expect(result.current.kinds.Issue).toBe("#111111");
  root.setProperty("--g-kind-Issue", "#222222");
  // A MutationObserver reports in a microtask: the act waits for it.
  await act(async () => { document.documentElement.dataset.skin = "matrix"; });
  expect(result.current.kinds.Issue).toBe("#222222");
  root.setProperty("--g-kind-Issue", "#333333");
  await act(async () => { document.documentElement.dataset.theme = "light"; });
  expect(result.current.kinds.Issue).toBe("#333333");
});
