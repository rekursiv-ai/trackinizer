import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { TopBar } from "./TopBar";

vi.mock("../commands/palette", () => ({ usePalette: () => ({ show: vi.fn() }) }));
vi.mock("../create/ComposeButton", () => ({ ComposeButton: () => null }));
vi.mock("../app/workbenchSlot", () => ({ useWorkbenchPresent: () => false, WorkbenchSlot: () => null }));
vi.mock("./AccountMenu", () => ({ AccountMenu: () => null }));
vi.mock("./GoMenu", () => ({ GoMenu: () => null }));
vi.mock("./WorkstreamBar", () => ({ WorkstreamBar: () => <nav aria-label="Workstreams" /> }));
vi.mock("./useDismiss", () => ({ useDismiss: vi.fn() }));
vi.mock("./MachinesMenu", () => ({ MachinesMenu: () => <button type="button" aria-label="Machines" /> }));

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
});

test("the bar is always there, with no hidden state and no edge to open it", () => {
  const { container } = render(<TopBar stations={false} />);
  const header = container.querySelector<HTMLElement>("header.topbar")!;
  expect(header.className).toBe("topbar");
  expect(container.querySelector(".topbar-edge")).toBeNull();
  expect(container.querySelector(".topbar-pin")).toBeNull();
});

// The bar used to hide itself and keep that choice in localStorage, while the pin that
// turned hiding off was display:none. A browser that had ever unpinned it reopened to a
// bar it could not get back. A stored unpin must now mean nothing.
test("a stored unpin from an older build no longer hides the bar", () => {
  localStorage.setItem("trackinizer.topbar.pinned", "0");
  const { container } = render(<TopBar stations={false} />);
  expect(container.querySelector("header.topbar")!.className).toBe("topbar");
});

test("Machines sits right before the Workbench", () => {
  const { container } = render(<TopBar stations={false} />);
  const buttons = [...container.querySelectorAll("header.topbar button")];
  const at = (selector: string) => buttons.indexOf(container.querySelector<HTMLButtonElement>(selector)!);
  const machines = at("[aria-label='Machines']");
  expect(machines).toBeGreaterThanOrEqual(0);
  expect([machines, at("[aria-label='Workbench']")]).toEqual([machines, machines + 1]);
});

test("the workstreams follow the name at the left of the bar", () => {
  const { container } = render(<TopBar stations />);
  const left = container.querySelector(".topbar-left")!;
  const stations = container.querySelector("[aria-label='Workstreams']")!;
  expect(left.contains(stations)).toBe(true);
});
