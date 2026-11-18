import { act, cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import type { InquiryRow } from "../api/inquiries";
import { HighlightContext, HighlightStore } from "../app/highlights";
import { issue } from "../live/testing";
import { Row } from "./Row";

afterEach(() => {
  cleanup();
});

function show(row: InquiryRow): HTMLElement {
  render(<Row row={row} mixed={false} focused={false} now={Date.parse(row.modified)} onFocus={() => {}} />);
  return document.querySelector("a.row")!;
}

test("a session's cost under a cent shows its digits, not $0.00 (PA4)", () => {
  const session = { ...issue(1), kind: "AgentSession", cli: "claude", ended: null, marginal_cost: { agent_usd: 0.0042, resource_usd: 0 } };
  expect(show(session).querySelector(".num")!.textContent).toBe("$0.0042");
});

test("an Issue row shows its priority number beside its band (PA4)", () => {
  expect(show(issue(2, { priority: 25 })).querySelector(".row-pri")!.textContent).toBe("25");
});

test("an Issue row with no priority shows no number", () => {
  expect(show(issue(3, { priority: null })).querySelector(".row-pri")).toBeNull();
});

test("an Issue row shows every type it has, task included (PA4)", () => {
  const types = [...show(issue(4, { issue_kind: ["task", "bug"] })).querySelectorAll(".kind-tag")].map((tag) => tag.textContent);
  expect(types).toEqual(["task", "bug"]);
});

test("a row shows every label, not the first two and a count (PA4)", () => {
  const labels = [...show(issue(5, { labels: ["a", "b", "c", "d"] })).querySelectorAll(".label-chip")].map((chip) => chip.textContent);
  expect(labels).toEqual(["a", "b", "c", "d"]);
});
test("a row the assistant points at takes the highlight class and a visible mark; the others none", () => {
  const highlights = new HighlightStore();
  const rows = [issue(6), issue(7)];
  render(
    <HighlightContext value={highlights}>
      {rows.map((row) => <Row key={row.id} row={row} mixed={false} focused={false} now={Date.parse(row.modified)} onFocus={() => {}} />)}
    </HighlightContext>,
  );
  const state = () => [...document.querySelectorAll("a.row")].map((link) => [link.classList.contains("is-highlighted"), link.querySelector(".row-mark") !== null]);
  expect(state()).toEqual([[false, false], [false, false]]);
  act(() => highlights.set([rows[1]!.id]));
  expect(state()).toEqual([[false, false], [true, true]]);
  expect(document.querySelector(".row-mark")!.getAttribute("aria-label")).toBe("Pointed out");
  act(() => highlights.set([]));
  expect(state()).toEqual([[false, false], [false, false]]);
});

test("a row outside a canvas is never marked", () => {
  expect(show(issue(8)).classList.contains("is-highlighted")).toBe(false);
});
