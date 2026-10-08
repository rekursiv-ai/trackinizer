import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError } from "../api/client";
import { getEvidenceTimeline, type EvidenceTimeline, type TimelineRecord } from "../api/timeline";
import { Timeline } from "./Timeline";
import { timeAxis, timelineRows, ticks } from "./timelineLayout";
import { WorkspaceActionsProvider } from "./workspaceActions";
import { canvasActions } from "./testing";

vi.mock("../api/timeline", () => ({ getEvidenceTimeline: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.location.hash = "";
});

function record(kind: TimelineRecord["kind"], seq: number, title: string, created: string, status = "active"): TimelineRecord {
  return { id: `${kind}-${seq}`, kind, seq, title, status, created, modified: created, description: null, outcome: null };
}

const belief = (seq: number) => record("Belief", seq, `Claim ${seq}`, "2026-09-28T00:00:00Z");

const timeline: EvidenceTimeline = {
  target: record("Issue", 706, "Representation learning", "2026-09-29T00:00:00Z"),
  issue: record("Issue", 706, "Representation learning", "2026-09-29T00:00:00Z"),
  leads: [
    record("Issue", 688, "Achieve 100%", "2026-01-02T00:00:00Z"),
    record("Issue", 311, "Goal superseded", "2026-09-26T00:00:00Z"),
  ],
  selected_result: null,
  root_results: [{
    record: record("Experiment", 656, "Recent root result", "2026-09-30T00:00:00Z", "complete"),
    evidence: [], evidence_truncated: false,
  }],
  root_results_truncated: true,
  directions: [{
    issue: record("Issue", 710, "Test learned policy", "2026-09-27T00:00:00Z"),
    results: [{
      record: { ...record("Experiment", 655, "Backplay result", "2026-09-28T12:00:00Z", "complete"),
        outcome: "Held-out games cleared 0/25 under the 100x cap." },
      evidence: [
        { claim: belief(467), edge_kind: "proves", valence: 0.8, note: "Matched seed" },
        { claim: belief(450), edge_kind: "favors", valence: -0.5, note: null },
        { claim: belief(451), edge_kind: "favors", valence: 0, note: null },
        { claim: belief(452), edge_kind: "favors", valence: null, note: null },
      ],
      evidence_truncated: false,
    }],
    results_truncated: false,
  }, {
    issue: record("Issue", 711, "Second direction", "2026-09-28T00:00:00Z"),
    results: [], results_truncated: false,
  }],
  directions_truncated: false,
  unresolved_questions: [],
};

const instance = {
  id: "timeline-instance", type: "trax.timeline", version: 1, placement: "main" as const,
  record_id: "Issue-706", params: { direction_limit: 6, results_per_direction: 2 },
};

function renderTimeline(children: ReactNode, operate = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={client}>
    <WorkspaceActionsProvider value={canvasActions({ operate })}>
      {children}
    </WorkspaceActionsProvider>
  </QueryClientProvider>);
  return { ...view, operate };
}

const view = (record_id: string | null = "Issue-706") =>
  <Timeline instance={{ ...instance, record_id }} workspace={null} onWorkspaceChanged={vi.fn()} focused />;

test("draws leads above the record and its directions below, in that order", async () => {
  vi.mocked(getEvidenceTimeline).mockResolvedValue(timeline);
  const { container } = renderTimeline(view());
  await screen.findByRole("link", { name: /Issue#710/ });
  const rows = [...container.querySelectorAll("[data-role]")].map((row) =>
    `${row.getAttribute("data-role")}:${row.getAttribute("data-ref")}`);
  expect(rows).toEqual([
    "lead:Issue#688", "lead:Issue#311", "record:Issue#706",
    "direction:Issue#710", "direction:Issue#711",
  ]);
  expect(getEvidenceTimeline).toHaveBeenCalledWith("Issue-706", {
    directionLimit: 6, resultsPerDirection: 2, signal: expect.any(AbortSignal),
  });
  expect(screen.getByText("Showing the latest 2 results of this record.")).toBeTruthy();
});

test("pins leads at the axis edge so an old lead leaves the time axis alone", () => {
  const rows = timelineRows(timeline);
  const axis = timeAxis(rows, 1000);
  expect(axis.t0).toBeGreaterThan(Date.parse("2026-09-20T00:00:00Z"));
  expect(axis.step).toBeGreaterThanOrEqual(1);
  const marks = ticks(axis).map((time) => axis.x(new Date(time).toISOString()));
  for (let i = 1; i < marks.length; i++) expect(marks[i]! - marks[i - 1]!).toBeGreaterThanOrEqual(64);
});

test.each([600, 1400])("lays the chart out at its real width of %i px", (available) => {
  const rows = timelineRows(timeline);
  const axis = timeAxis(rows, available);
  expect(axis.width).toBe(available);
  expect(axis.labelW).toBe(Math.round(Math.min(330, Math.max(170, available * 0.32))));
  expect(axis.axisL).toBe(axis.labelW + 90);
  const marks = ticks(axis).map((time) => axis.x(new Date(time).toISOString()));
  expect(marks.length).toBeGreaterThan(1);
  for (let i = 1; i < marks.length; i++) expect(marks[i]! - marks[i - 1]!).toBeGreaterThanOrEqual(64);
  for (const row of rows.filter((item) => item.role !== "lead")) {
    expect(axis.x(row.record.created)).toBeGreaterThan(axis.axisL);
    expect(axis.x(row.record.created)).toBeLessThan(available);
  }
  expect(axis.titleChars).toBe(Math.floor((axis.labelW - 30) / 6.6));
});

test("a narrow window keeps the minimum width and scrolls instead of squeezing", () => {
  const axis = timeAxis(timelineRows(timeline), 300);
  expect(axis.width).toBe(520);
  expect(axis.labelW).toBe(170);
});

test("the svg is as wide as the window and clips titles to the label column", async () => {
  const long = { ...timeline, directions: [{ ...timeline.directions[0]!,
    issue: record("Issue", 712, "A very long direction title that cannot fit the column", "2026-09-27T00:00:00Z") }] };
  vi.mocked(getEvidenceTimeline).mockResolvedValue(long);
  const { container } = renderTimeline(<Timeline instance={{ ...instance }} workspace={null}
    onWorkspaceChanged={vi.fn()} focused width={600} />);
  await screen.findByRole("link", { name: /Issue#712/ });
  const svg = container.querySelector("svg.tl-chart")!;
  expect(svg.getAttribute("width")).toBe("600");
  expect(svg.getAttribute("viewBox")).toMatch(/^0 0 600 /);
  const title = container.querySelector("[data-ref='Issue#712'] .tl-row-title")!.textContent!;
  expect(title.endsWith("…")).toBe(true);
  expect(title.length).toBeLessThanOrEqual(Math.floor((192 - 30) / 6.6));
});

test("colours each result's evidence by the sign of its valence", async () => {
  vi.mocked(getEvidenceTimeline).mockResolvedValue(timeline);
  const { container } = renderTimeline(view());
  await screen.findByRole("link", { name: /Issue#710/ });
  const marks = [...container.querySelectorAll(".tl-mark")].map((mark) =>
    `${mark.getAttribute("data-sign")}:${mark.querySelector("text")?.textContent}`);
  expect(marks).toEqual(["for:+0.8", "against:−0.5", "neutral:0", "neutral:n/a"]);
});

test("hover names a record's ref, title, status and date", async () => {
  vi.mocked(getEvidenceTimeline).mockResolvedValue(timeline);
  renderTimeline(view());
  const link = await screen.findByRole("link", { name: /Issue#710/ });
  expect(link.querySelector("title")?.textContent)
    .toBe("Issue#710 · Test learned policy\nactive · 2026-09-27 00:00");
  const result = screen.getByRole("link", { name: /Experiment#655/ });
  expect(result.querySelector("title")?.textContent).toContain("Held-out games cleared 0/25 under the 100x cap.");
});

test("a card click moves the page and re-centres the window on that record", async () => {
  vi.mocked(getEvidenceTimeline).mockResolvedValue(timeline);
  const { operate } = renderTimeline(view());
  fireEvent.click(await screen.findByRole("link", { name: /Issue#710/ }));
  expect(window.location.hash).toBe("#/ref/Issue/710");
  expect(operate).toHaveBeenCalledWith({ kind: "show", visual_type: "trax.timeline", record_id: "Issue-710" });
  fireEvent.click(screen.getByRole("link", { name: /Experiment#655/ }));
  expect(window.location.hash).toBe("#/ref/Experiment/655");
  expect(operate).toHaveBeenLastCalledWith({ kind: "show", visual_type: "trax.timeline", record_id: "Experiment-655" });
});

test("clicking the record the window already centres on does nothing", async () => {
  vi.mocked(getEvidenceTimeline).mockResolvedValue(timeline);
  const { operate } = renderTimeline(view());
  fireEvent.click(await screen.findByRole("link", { name: /Issue#706/ }));
  expect(operate).not.toHaveBeenCalled();
});

test("an Experiment target rides on the Issue that produced it", async () => {
  const producer = timeline.issue!;
  const selected = timeline.directions[0]!.results[0]!;
  vi.mocked(getEvidenceTimeline).mockResolvedValue({
    ...timeline, target: selected.record, issue: producer, selected_result: selected,
  });
  const { container } = renderTimeline(view("Experiment-655"));
  await screen.findByRole("link", { name: /Issue#710/ });
  const row = container.querySelector("[data-role=record]");
  expect(row?.getAttribute("data-ref")).toBe("Issue#706");
  expect(container.querySelector(".tl-result.tl-selected")?.getAttribute("data-ref")).toBe("Experiment#655");
});

test("a record of any other kind is the record row, alone when nothing links it", async () => {
  vi.mocked(getEvidenceTimeline).mockResolvedValue({
    ...timeline, target: record("Paper", 4, "A paper", "2026-09-29T00:00:00Z"), issue: null, leads: [],
    root_results: [], root_results_truncated: false, directions: [],
  });
  const { container } = renderTimeline(view("Paper-4"));
  await screen.findByRole("link", { name: /Paper#4/ });
  expect([...container.querySelectorAll("[data-role]")].map((row) => row.getAttribute("data-role"))).toEqual(["record"]);
  expect(screen.getByText("Nothing else is linked to this record yet.")).toBeTruthy();
});

test("does not request data until the visual has a record target", () => {
  renderTimeline(view(null));
  expect(screen.getByText("Choose a record to show its lineage and timeline.")).toBeTruthy();
  expect(getEvidenceTimeline).not.toHaveBeenCalled();
});

test("shows loading, then an error that can retry", async () => {
  vi.mocked(getEvidenceTimeline).mockRejectedValueOnce(new ApiError(500, "boom"));
  renderTimeline(view());
  expect(screen.getByText("Loading lineage and timeline…")).toBeTruthy();
  expect(await screen.findByText(/Could not load lineage and timeline\./)).toBeTruthy();
  vi.mocked(getEvidenceTimeline).mockResolvedValue(timeline);
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(await screen.findByRole("link", { name: /Issue#710/ })).toBeTruthy();
});
