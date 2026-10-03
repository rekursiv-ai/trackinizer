import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { PriorityPicker } from "./Priority";
import { stubLayout } from "./testing";

beforeEach(stubLayout);

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** A create form's draft: a priority held in state, never written. */
function Draft() {
  const [priority, setPriority] = useState<number | null>(20);
  return (
    <PriorityPicker
      value={priority ?? undefined}
      onPick={setPriority}
      trigger={<button type="button">{priority === null ? "No priority" : `Priority ${priority}`}</button>}
    />
  );
}

test("the exact priority works on a create form's draft (COLD-10)", async () => {
  render(<Draft />);
  fireEvent.click(screen.getByRole("button", { name: "Priority 20" }));
  expect(screen.getByRole("option", { name: "P2 Medium" }).getAttribute("aria-selected")).toBe("true");
  fireEvent.click(screen.getByRole("option", { name: /^Exact number…/ }));
  const exact = await screen.findByRole("spinbutton", { name: "Exact priority" });
  expect((exact as HTMLInputElement).value).toBe("20");
  await waitFor(() => expect(document.activeElement).toBe(exact));
  fireEvent.change(exact, { target: { value: "7" } });
  fireEvent.submit(exact.closest("form")!);
  expect(await screen.findByRole("button", { name: "Priority 7" })).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Priority 7" }));
  expect(screen.getByRole("option", { name: "P0 Critical" }).getAttribute("aria-selected")).toBe("true");
  fireEvent.click(screen.getByRole("option", { name: /^Exact number…/ }));
  fireEvent.change(await screen.findByRole("spinbutton", { name: "Exact priority" }), { target: { value: "" } });
  fireEvent.submit(screen.getByRole("spinbutton", { name: "Exact priority" }).closest("form")!);
  expect(await screen.findByRole("button", { name: "No priority" })).toBeTruthy();
});
