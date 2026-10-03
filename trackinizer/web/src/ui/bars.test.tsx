import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Bar, BarStack, OfflineBar, PausedBar, StreamStatusContext } from "./bars";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("the offline bar follows the browser's online state", () => {
  render(<OfflineBar />);
  expect(screen.queryByRole("status")).toBeNull();
  const online = vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
  act(() => {
    dispatchEvent(new Event("offline"));
  });
  expect(screen.getByRole("status").textContent).toContain("You are offline.");
  online.mockReturnValue(true);
  act(() => {
    dispatchEvent(new Event("online"));
  });
  expect(screen.queryByRole("status")).toBeNull();
});

test("the paused bar shows only when the stream says it is paused", () => {
  const view = render(<PausedBar />);
  expect(screen.queryByRole("status")).toBeNull();
  view.rerender(
    <StreamStatusContext value="paused">
      <PausedBar />
    </StreamStatusContext>,
  );
  expect(screen.getByRole("status").textContent).toBe("Live updates paused. Reconnecting…");
});

test("a bar inside the shell's stack draws in the stack, out of the view it is declared in", () => {
  render(
    <BarStack>
      <main aria-label="View">
        <PausedBar />
        <Bar kind="stale">Could not refresh</Bar>
        <p>A row</p>
      </main>
    </BarStack>,
    { wrapper: ({ children }) => <StreamStatusContext value="paused">{children}</StreamStatusContext> },
  );
  const bars = screen.getAllByRole("status");
  expect(bars.map((bar) => bar.textContent)).toEqual(["Live updates paused. Reconnecting…", "Could not refresh"]);
  for (const bar of bars) {
    expect(bar.parentElement!.className).toBe("bars");
    expect(screen.getByRole("main", { name: "View" }).contains(bar)).toBe(false);
  }
});
