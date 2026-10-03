import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import type { InquiryRow } from "../api/inquiries";
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
