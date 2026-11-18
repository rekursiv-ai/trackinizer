import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { CrashBoundary } from "./CrashBoundary";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function Broken(): never {
  throw new Error("it broke");
}

test("a crash shows its message with Copy details and Reload, inside the frame it is given", () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  const reload = vi.fn();
  render(<CrashBoundary reload={reload} frame={(crash) => <section aria-label="Frame">{crash}</section>}><Broken /></CrashBoundary>);
  const frame = screen.getByRole("region", { name: "Frame" });
  expect(frame.querySelector("[role=alert]")?.textContent).toBe("it broke");
  expect(screen.getByRole("button", { name: "Copy details" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Reload" }));
  expect(reload).toHaveBeenCalledOnce();
});

test("a crash stays until the reset key changes, then the children render again", () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  const ui = (resetKey: string, broken: boolean) => (
    <CrashBoundary reload={vi.fn()} resetKey={resetKey}>{broken ? <Broken /> : <p>fine</p>}</CrashBoundary>
  );
  const view = render(ui("#/a", true));
  expect(screen.getByRole("alert").textContent).toBe("it broke");
  view.rerender(ui("#/a", false));
  expect(screen.queryByText("fine")).toBeNull();
  view.rerender(ui("#/b", false));
  expect(screen.getByText("fine")).toBeTruthy();
});
