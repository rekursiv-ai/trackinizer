import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { TopBar } from "./TopBar";

vi.mock("../commands/palette", () => ({ usePalette: () => ({ show: vi.fn() }) }));
vi.mock("../create/ComposeButton", () => ({ ComposeButton: () => null }));
vi.mock("../app/workbenchSlot", () => ({ useWorkbenchPresent: () => false, WorkbenchSlot: () => null }));
vi.mock("./AccountMenu", () => ({ AccountMenu: () => null }));
vi.mock("./GoMenu", () => ({ GoMenu: () => null }));
vi.mock("./WorkstreamBar", () => ({ WorkstreamBar: () => <nav aria-label="Workstreams" /> }));
vi.mock("./useDismiss", () => ({ useDismiss: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function setHover(matches: boolean) {
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
}

test("the top bar opens at the pointer edge and a click on it keeps it open", () => {
  setHover(false);
  vi.useFakeTimers();
  const { container } = render(<TopBar stations={false} />);
  const header = container.querySelector<HTMLElement>("header.topbar")!;
  const edge = container.querySelector<HTMLButtonElement>(".topbar-edge")!;
  expect(header.className).toContain("topbar-auto-hidden");
  fireEvent.pointerMove(edge, { pointerType: "mouse" });
  act(() => { vi.advanceTimersByTime(120); });
  expect(header.className).toContain("topbar-auto-open");
  fireEvent.click(header);
  expect(header.className).toContain("topbar-pinned");
  fireEvent.pointerLeave(header, { pointerType: "mouse" });
  act(() => { vi.advanceTimersByTime(400); });
  expect(header.className).toContain("topbar-auto-open");
});

test("a second click unpins the bar, which then hides", () => {
  setHover(false);
  vi.useFakeTimers();
  const { container } = render(<TopBar stations={false} />);
  const header = container.querySelector<HTMLElement>("header.topbar")!;
  fireEvent.click(header);
  fireEvent.click(header);
  expect(header.className).not.toContain("topbar-pinned");
  fireEvent.pointerLeave(header, { pointerType: "mouse" });
  act(() => { vi.advanceTimersByTime(400); });
  expect(header.className).toContain("topbar-auto-hidden");
});

test("a click on a control of the bar does not pin it", () => {
  setHover(false);
  const { container } = render(<TopBar stations={false} />);
  const header = container.querySelector<HTMLElement>("header.topbar")!;
  fireEvent.click(container.querySelector<HTMLButtonElement>('button[aria-label="Search"]')!);
  expect(header.className).not.toContain("topbar-pinned");
});

test("a touch tap at the edge opens the bar and a tap elsewhere closes it", () => {
  setHover(true);
  const { container } = render(<TopBar stations={false} />);
  const header = container.querySelector<HTMLElement>("header.topbar")!;
  fireEvent.click(container.querySelector<HTMLButtonElement>(".topbar-edge")!);
  expect(header.className).toContain("topbar-auto-open");
  fireEvent.pointerDown(document.body, { pointerType: "touch" });
  expect(header.className).toContain("topbar-auto-hidden");
});
