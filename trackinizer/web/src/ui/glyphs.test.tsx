import { cleanup, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, expect, test } from "vitest";
import { JudgementGlyph, PriorityGlyph, priorityBand, priorityName, StateGlyphs, StatusGlyph } from "./glyphs";

// The server's closed sets, as the local preview's `/api/meta/enums` returned them.
const STATUSES = ["active", "complete", "abandoned", "invalid"];
const JUDGEMENTS = ["proven", "disproven", "unproven", "undecidable"];

afterEach(cleanup);

test("every status and judgement draws as itself, each with its own mark (COLD-14)", () => {
  const drawn = (element: ReactElement) => {
    const { container } = render(element);
    const svg = container.querySelector("svg")!;
    cleanup();
    return [svg.getAttribute("aria-label"), svg.innerHTML] as const;
  };
  for (const [values, glyph] of [
    [STATUSES, (value: string) => <StatusGlyph status={value} />],
    [JUDGEMENTS, (value: string) => <JudgementGlyph judgement={value} />],
  ] as const) {
    const marks = values.map((value) => drawn(glyph(value)));
    expect(marks.map(([label]) => label)).toEqual(values.map((value) => value[0]!.toUpperCase() + value.slice(1)));
    expect(new Set(marks.map(([, markup]) => markup)).size).toBe(values.length);
  }
});

test("a value the UI never drew still shows its own name", () => {
  render(<StatusGlyph status="paused" />);
  expect(screen.getByRole("img", { name: "Paused" })).toBeTruthy();
});

test("a Belief shows its judgement, and its status once closed", () => {
  const labels = () => screen.getAllByRole("img").map((img) => img.getAttribute("aria-label"));
  const view = render(<StateGlyphs status="active" judgement="proven" />);
  expect(labels()).toEqual(["Proven"]);
  view.rerender(<StateGlyphs status="abandoned" judgement="proven" />);
  expect(labels()).toEqual(["Proven", "Abandoned"]);
  view.rerender(<StateGlyphs status="invalid" judgement={null} />);
  expect(labels()).toEqual(["Invalid"]);
  view.rerender(<StateGlyphs status="complete" />);
  expect(labels()).toEqual(["Complete"]);
});

test("priority bands are priority // 10 capped at Low; null is No priority", () => {
  expect([0, 9, 10, 25, 30, 40, 99].map(priorityBand)).toEqual([0, 0, 1, 2, 3, 3, 3]);
  expect([null, undefined].map(priorityBand)).toEqual([null, null]);
  expect([0, 9, 10, 25, 30, 40, 99, null].map(priorityName)).toEqual([
    "P0 Critical",
    "P0 Critical",
    "P1 High",
    "P2 Medium",
    "P3 Low",
    "P3 Low",
    "P3 Low",
    "No priority",
  ]);
  render(<PriorityGlyph priority={null} />);
  expect(screen.getByRole("img", { name: "No priority" })).toBeTruthy();
});
