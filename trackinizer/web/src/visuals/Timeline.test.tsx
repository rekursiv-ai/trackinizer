import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { getEvidenceTimeline, type EvidenceTimeline } from "../api/timeline";
import { Timeline } from "./Timeline";
import { WorkspaceActionsProvider } from "./workspaceActions";

vi.mock("../api/timeline", () => ({ getEvidenceTimeline: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const timeline: EvidenceTimeline = {
  target: {
    id: "root-id", kind: "Issue", seq: 21706, title: "Representation learning",
    status: "active", created: "2026-09-29T00:00:00Z", modified: "2026-09-29T00:00:00Z",
    description: null, outcome: null,
  },
  issue: {
    id: "root-id", kind: "Issue", seq: 21706, title: "Representation learning",
    status: "active", created: "2026-09-29T00:00:00Z", modified: "2026-09-29T00:00:00Z",
    description: null, outcome: null,
  },
  selected_result: null,
  root_results: [{
    record: {
      id: "recent-root-experiment", kind: "Experiment", seq: 17656, title: "Recent root result",
      status: "complete", created: "2026-09-29T00:00:00Z", modified: "2026-09-29T00:00:00Z",
      description: null, outcome: "Latest root measurement",
    }, evidence: [], evidence_truncated: false,
  }],
  root_results_truncated: true,
  directions: [{
    issue: {
      id: "direction-id", kind: "Issue", seq: 710, title: "Test learned policy",
      status: "active", created: "2026-09-27T00:00:00Z", modified: "2026-09-28T00:00:00Z",
      description: null, outcome: null,
    },
    results: [{
      record: {
        id: "experiment-id", kind: "Experiment", seq: 17655, title: "Backplay result",
        status: "complete", created: "2026-09-28T12:00:00Z", modified: "2026-09-29T00:00:00Z",
        description: null,
        outcome: "Held-out games cleared 0/25 under the 100x cap.",
      },
      evidence: [
        { claim: {
          id: "belief-positive", kind: "Belief", seq: 467, title: "Feature adds no lift",
          status: "active", created: "2026-09-28T00:00:00Z", modified: "2026-09-28T00:00:00Z",
          description: null, outcome: null,
        }, edge_kind: "proves", valence: 0.8, note: "Matched seed" },
        { claim: {
          id: "belief-negative", kind: "Belief", seq: 450, title: "Backplay stalls early",
          status: "active", created: "2026-09-28T00:00:00Z", modified: "2026-09-28T00:00:00Z",
          description: null, outcome: null,
        }, edge_kind: "favors", valence: -0.5, note: null },
        { claim: {
          id: "belief-neutral", kind: "Belief", seq: 2451, title: "Neutral claim",
          status: "active", created: "2026-09-28T00:00:00Z", modified: "2026-09-28T00:00:00Z",
          description: null, outcome: null,
        }, edge_kind: "favors", valence: 0, note: null },
        { claim: {
          id: "belief-unscored", kind: "Belief", seq: 2452, title: "Unscored claim",
          status: "active", created: "2026-09-28T00:00:00Z", modified: "2026-09-28T00:00:00Z",
          description: null, outcome: null,
        }, edge_kind: "favors", valence: null, note: null },
      ],
      evidence_truncated: false,
    }],
    results_truncated: false,
  }],
  directions_truncated: false,
  unresolved_questions: [],
};

test("renders dated directions, outcomes, linked signed evidence, and closed details", async () => {
  vi.mocked(getEvidenceTimeline).mockResolvedValue(timeline);
  const revealRecord = vi.fn().mockResolvedValue(true);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const instance = {
    id: "timeline-instance", type: "trax.timeline", version: 1, placement: "main" as const,
    record_id: "root-id", params: { direction_limit: 6, results_per_direction: 2 },
  };
  render(<QueryClientProvider client={client}>
    <WorkspaceActionsProvider value={{ busy: false, writeError: null, revealRecord,
      connectSession: vi.fn() }}>
      <Timeline instance={instance} workspace={null} onWorkspaceChanged={vi.fn()} focused />
    </WorkspaceActionsProvider>
  </QueryClientProvider>);

  expect(await screen.findByRole("link", { name: "Issue#710 Test learned policy" })).toBeTruthy();
  expect(screen.getByText("2026-09-27")).toBeTruthy();
  expect(screen.getByText("2026-09-28")).toBeTruthy();
  expect(screen.getByText("Held-out games cleared 0/25 under the 100x cap.")).toBeTruthy();
  expect(screen.getByText("Latest root measurement")).toBeTruthy();
  expect(screen.getByText("Showing the latest 2 root results.")).toBeTruthy();
  expect(getEvidenceTimeline).toHaveBeenCalledWith("root-id", {
    directionLimit: 6, resultsPerDirection: 2, signal: expect.any(AbortSignal),
  });
  const details = screen.getAllByText("Evidence and details")[1]?.closest("details");
  expect(details?.open).toBe(false);

  fireEvent.click(screen.getAllByText("Evidence and details")[1]!);
  expect(await screen.findByText("proves · supports (0.8)")).toBeTruthy();
  expect(screen.getByText("favors · argues against (-0.5)")).toBeTruthy();
  expect(screen.getByText("favors · neutral (0)")).toBeTruthy();
  expect(screen.getByText("favors · valence not recorded")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Belief#467 Feature adds no lift" }).getAttribute("href"))
    .toBe("#/lookup/belief-positive");
});

test("does not request timeline data until the visual has a record target", () => {
  vi.mocked(getEvidenceTimeline).mockResolvedValue(timeline);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}>
    <Timeline instance={{ id: "timeline-instance", type: "trax.timeline", version: 1,
      placement: "main", record_id: null, params: {} }} workspace={null}
      onWorkspaceChanged={vi.fn()} focused />
  </QueryClientProvider>);
  expect(screen.getByText("Choose a record to show its evidence timeline.")).toBeTruthy();
  expect(getEvidenceTimeline).not.toHaveBeenCalled();
});

test("shows an orphan Experiment without an Issue empty state", async () => {
  vi.mocked(getEvidenceTimeline).mockResolvedValue({
    ...timeline,
    target: { ...timeline.target, id: "orphan-experiment", kind: "Experiment" },
    issue: null,
    selected_result: timeline.directions[0]!.results[0]!,
    root_results: [],
    root_results_truncated: false,
    directions: [],
    directions_truncated: false,
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}>
    <Timeline instance={{ id: "timeline-instance", type: "trax.timeline", version: 1,
      placement: "main", record_id: "orphan-experiment", params: {} }} workspace={null}
      onWorkspaceChanged={vi.fn()} focused />
  </QueryClientProvider>);

  expect(await screen.findByText("Selected result")).toBeTruthy();
  expect(screen.queryByText("No directions or results are linked to this Issue yet.")).toBeNull();
});

test("explains unsupported target records", async () => {
  const { ApiError } = await import("../api/client");
  vi.mocked(getEvidenceTimeline).mockRejectedValue(
    new ApiError(422, "Timeline supports Issue and Experiment records only."),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}>
    <Timeline instance={{ id: "timeline-instance", type: "trax.timeline", version: 1,
      placement: "main", record_id: "belief-id", params: {} }} workspace={null}
      onWorkspaceChanged={vi.fn()} focused />
  </QueryClientProvider>);
  expect(await screen.findByText(/Evidence timeline supports Issue and Experiment records only\./)).toBeTruthy();
});
