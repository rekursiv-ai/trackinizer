import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError } from "../api/client";
import { stubClipboard } from "../debug/testing";
import { type ShowToast, ToastProvider, useToast } from "./toast";

afterEach(cleanup);

function renderToasts(): ShowToast {
  const handle: { show?: ShowToast } = {};
  function Grab() {
    handle.show = useToast();
    return null;
  }
  render(
    <ToastProvider>
      <Grab />
    </ToastProvider>,
  );
  return handle.show!;
}

test("a toast shows its message, with Retry or Undo only when given", async () => {
  const show = renderToasts();
  const retry = vi.fn();
  const undo = vi.fn();
  act(() => {
    show("Copied Issue#12");
    show("Could not save the title", { retry });
    show("Priority set to P1 High", { undo });
  });
  expect(screen.getByText("Copied Issue#12")).toBeTruthy();
  expect(screen.getAllByRole("button").map((button) => button.getAttribute("aria-label") ?? button.textContent)).toEqual([
    "Retry",
    "Copy details",
    "Undo",
  ]);

  // Pressed from the keyboard: a pointer press runs Radix's swipe handler, which
  // calls pointer capture, and jsdom has none.
  const user = userEvent.setup();
  screen.getByRole("button", { name: "Retry" }).focus();
  await user.keyboard("{Enter}");
  expect(retry).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("Could not save the title")).toBeNull();
  screen.getByRole("button", { name: "Undo" }).focus();
  await user.keyboard("{Enter}");
  expect(undo).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("Priority set to P1 High")).toBeNull();
  expect(screen.getByText("Copied Issue#12")).toBeTruthy();
});

test("a failure shows a cross where a success shows a check, with or without Retry", () => {
  const show = renderToasts();
  act(() => {
    show("Title set to Renamed");
    show("Not saved: the server failed.", { retry: () => {} });
    show("Not saved: josh changed Status to complete", { failed: true });
  });
  const mark = (message: string) => screen.getByText(message).closest(".toast")!.querySelector(".ic")!.getAttribute("class");
  expect(mark("Title set to Renamed")).toBe("ic");
  expect(mark("Not saved: the server failed.")).toBe("ic failed");
  expect(mark("Not saved: josh changed Status to complete")).toBe("ic failed");
});

test("a failure offers Copy details, with what failed; a success does not", async () => {
  const copied = stubClipboard();
  const show = renderToasts();
  const sent = { method: "PUT", path: "/api/inquiries/x/title", id: "5d3c2b1a-0f9e-4d8c-8b7a-6e5d4c3b2a19", at: "", ms: 3, attempt: 4 };
  act(() => {
    show("Title set to Renamed");
    show("Not saved: the server failed (database unavailable).", { retry: () => {}, error: new ApiError(503, "database unavailable", null, sent) });
  });
  const copy = screen.getByRole("button", { name: "Copy details" });
  expect(copy.closest(".toast")!.textContent).toContain("Not saved");
  // A click alone: a pointer press would run Radix's swipe handler (see above), and
  // user-event would put its own clipboard in place of the stub.
  fireEvent.click(copy);
  await waitFor(() => expect(copied).toHaveLength(1));
  expect(copied[0]).toMatch(/^Trackinizer web app: Not saved: the server failed \(database unavailable\)\.\n/);
  expect(copied[0]).toContain(`request_id=${sent.id} attempt=4`);
  // The toast stays, saying it copied.
  expect(screen.getByRole("button", { name: "Details copied" })).toBeTruthy();
});
