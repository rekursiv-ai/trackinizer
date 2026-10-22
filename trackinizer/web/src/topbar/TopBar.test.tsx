import { act, cleanup, fireEvent, getByLabelText, render } from "@testing-library/react";
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
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function setHover(matches: boolean) {
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
}

test("the top bar opens at the pointer edge and its pin keeps it open", () => {
  setHover(false);
  localStorage.setItem("trackinizer.topbar.pinned", "0");
  vi.useFakeTimers();
  const { container } = render(<TopBar stations={false} />);
  const header = container.querySelector<HTMLElement>("header.topbar")!;
  const edge = container.querySelector<HTMLButtonElement>(".topbar-edge")!;
  expect(header.className).toContain("topbar-auto-hidden");
  fireEvent.pointerMove(edge, { pointerType: "mouse" });
  act(() => { vi.advanceTimersByTime(120); });
  expect(header.className).toContain("topbar-auto-open");
  fireEvent.click(container.querySelector<HTMLButtonElement>(".topbar-pin")!);
  fireEvent.pointerLeave(header, { pointerType: "mouse" });
  act(() => { vi.advanceTimersByTime(400); });
  expect(header.className).toContain("topbar-auto-open");
});

test("the bar starts pinned, and the pinned toggle is named Unpin app bar", () => {
  setHover(false);
  const { container } = render(<TopBar stations={false} />);
  expect(container.querySelector("header.topbar")!.className).toContain("topbar-auto-open");
  expect(getByLabelText(container, "Unpin app bar").getAttribute("aria-pressed")).toBe("true");
});

test("unpinning is remembered across reloads, and a stored unpin is honoured", () => {
  setHover(false);
  const first = render(<TopBar stations={false} />);
  fireEvent.click(getByLabelText(first.container, "Unpin app bar"));
  expect(first.container.querySelector("header.topbar")!.className).toContain("topbar-auto-hidden");
  first.unmount();
  const second = render(<TopBar stations={false} />);
  expect(second.container.querySelector("header.topbar")!.className).toContain("topbar-auto-hidden");
  expect(getByLabelText(second.container, "Keep app bar open").getAttribute("aria-pressed")).toBe("false");
});

test("the pin sits just before the workstreams", () => {
  setHover(false);
  const { container } = render(<TopBar stations />);
  const pin = container.querySelector(".topbar-pin")!;
  const stations = container.querySelector("[aria-label='Workstreams']")!;
  expect(pin.compareDocumentPosition(stations) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(pin.nextElementSibling).toBe(stations.parentElement);
});

test("Machines sits right before the Workbench", () => {
  setHover(false);
  const { container } = render(<TopBar stations={false} />);
  const buttons = [...container.querySelectorAll("header.topbar button")];
  const at = (selector: string) => buttons.indexOf(container.querySelector<HTMLButtonElement>(selector)!);
  const machines = at("[aria-label='Machines']");
  expect(at(".topbar-pin")).toBeGreaterThanOrEqual(0);
  expect([machines, at("[aria-label='Workbench']")]).toEqual([machines, machines + 1]);
});

test("a touch tap at the edge opens the bar and a tap elsewhere closes it", () => {
  setHover(true);
  localStorage.setItem("trackinizer.topbar.pinned", "0");
  const { container } = render(<TopBar stations={false} />);
  const header = container.querySelector<HTMLElement>("header.topbar")!;
  fireEvent.click(container.querySelector<HTMLButtonElement>(".topbar-edge")!);
  expect(header.className).toContain("topbar-auto-open");
  fireEvent.pointerDown(document.body, { pointerType: "touch" });
  expect(header.className).toContain("topbar-auto-hidden");
});
