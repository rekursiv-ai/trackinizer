import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError } from "../api/client";
import { refusedDetails } from "../debug/details";
import { stubClipboard } from "../debug/testing";
import { ReadFailure } from "./failure";

afterEach(cleanup);

test("a read that failed shows the message with Retry, and Copy details beside it", async () => {
  const copied = stubClipboard();
  const retry = vi.fn();
  const sent = { method: "GET", path: "/api/inquiries", id: "5d3c2b1a-0f9e-4d8c-8b7a-6e5d4c3b2a19", at: "", ms: 12, attempt: 3 };
  render(<ReadFailure error={new ApiError(503, "database unavailable", null, sent)} retry={retry} />);
  // The alert's text is the message and Retry; the icon adds none.
  expect(screen.getByRole("alert").textContent).toBe("database unavailableRetry");
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(retry).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "Copy details" }));
  await waitFor(() => expect(copied).toHaveLength(1));
  expect(copied[0]).toMatch(/^Trackinizer web app: database unavailable\n/);
  expect(copied[0]).toContain(`\nfailed: request method=GET path=/api/inquiries status=503 ms=12 request_id=${sent.id} attempt=3`);
  expect(screen.getByRole("button", { name: "Details copied" })).toBeTruthy();
});

// The details can hold text a user wrote (the message shown), which nothing may
// log: the console gets only where to find them, and trackinizer.details() gives
// them when asked.
test("a browser that refuses the clipboard keeps the details for trackinizer.details(), and logs none of them", async () => {
  const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  render(<ReadFailure error={new ApiError(0, "No response within 15 s.", "timeout")} retry={() => {}} />);
  // jsdom has no clipboard, as a page over plain http from another machine has none.
  fireEvent.click(screen.getByRole("button", { name: "Copy details" }));
  await screen.findByRole("button", { name: "Not copied: see the console" });
  expect(warn.mock.calls.flat().join(" ")).not.toContain("No response within 15 s.");
  expect(String(warn.mock.calls[0]![0])).toContain("trackinizer.details()");
  expect(refusedDetails()).toMatch(/^Trackinizer web app: No response within 15 s\.\n/);
  warn.mockRestore();
});
