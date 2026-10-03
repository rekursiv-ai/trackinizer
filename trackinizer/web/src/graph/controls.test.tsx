import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ALL_NODES } from "../api/graph";
import { LimitMenu } from "./controls";

beforeEach(() => {
  // jsdom has none; the menu scrolls its active option into view.
  Element.prototype.scrollIntoView = () => {};
});

afterEach(() => {
  cleanup();
});

/** Open the Nodes menu at `limit`, type `text` and press Enter; returns what it offered and what it picked. */
function typeLimit(limit: number, text: string) {
  const onLimit = vi.fn();
  render(<LimitMenu limit={limit} onLimit={onLimit} />);
  fireEvent.click(screen.getByRole("button", { name: /^Nodes:/ }));
  const box = screen.getByRole("combobox", { name: "Nodes to show…" });
  fireEvent.change(box, { target: { value: text } });
  const offered = screen.queryAllByRole("option").map((option) => option.textContent);
  fireEvent.keyDown(box, { key: "Enter" });
  cleanup();
  return { offered, picked: onLimit.mock.calls.map(([count]) => count) };
}

test("a typed count picks exactly that many, never a preset that holds its digits: 50 is 50, not 5,000", () => {
  expect(typeLimit(1000, "50")).toEqual({ offered: ["50 nodes"], picked: [50] });
  expect(typeLimit(1000, "500")).toEqual({ offered: ["500 nodes"], picked: [500] });
  expect(typeLimit(1000, "10")).toEqual({ offered: ["10 nodes"], picked: [10] });
});

test("a preset typed as a count, as 1,000, 5,000 or 5k, picks that preset", () => {
  expect(typeLimit(100, "1,000")).toEqual({ offered: ["1k nodes"], picked: [1000] });
  expect(typeLimit(100, "5,000")).toEqual({ offered: ["5k nodes"], picked: [5000] });
  expect(typeLimit(100, "5k")).toEqual({ offered: ["5k nodes"], picked: [5000] });
  expect(typeLimit(2500, "2,500")).toEqual({ offered: ["2,500 nodes"], picked: [2500] });
});

test("text that is no count matches the options' names", () => {
  expect(typeLimit(1000, "all")).toEqual({ offered: ["All nodes"], picked: [ALL_NODES] });
  expect(typeLimit(1000, "nodes").offered).toEqual(["100 nodes", "1k nodes", "5k nodes", "All nodes"]);
});
