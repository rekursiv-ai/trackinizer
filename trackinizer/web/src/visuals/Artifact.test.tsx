import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { stubFetch } from "../api/testing";
import { Artifact, ArtifactContent, ArtifactPage, type ArtifactContentRevision } from "./Artifact";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const structuredArtifact: ArtifactContentRevision = {
  revision: 3,
  artifact_id: "artifact-1",
  issue_id: "issue-1",
  title: "Scaling directions",
  summary: "Two directions improved the held-out score.",
  author: "Grace",
  created_at: "2026-09-29T10:00:00Z",
  scope: "team",
  citations: [],
  format: "structured",
  sections: [{
    title: "Representation",
    summary: "The wider representation performed best.",
    details: "Matched runs across the frozen test split.",
    findings: [{
      claim: "Wider features improve score",
      outcome: { result: "12 wins", denominator: 16, split: "held-out" },
      uncertainty: "The sample is small.",
      citations: [{
        record_id: "belief-1",
        kind: "Belief",
        seq: 467,
        title: "Feature adds lift",
        edge_kind: "proves",
        valence: 0.8,
        note: "Matched seed comparison",
      }, {
        record_id: "belief-2",
        kind: "Belief",
        seq: 450,
        title: "Possible confound",
        edge_kind: "favors",
        valence: -0.5,
      }],
    }],
  }],
};

test("keeps section details closed while summaries and citation links stay visible", () => {
  render(<ArtifactContent artifact={structuredArtifact} />);

  expect(screen.getByRole("heading", { name: "Scaling directions" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "Link to this Artifact" }).getAttribute("href"))
    .toBe("#/lookup/artifact-1");
  expect(screen.getByText("The wider representation performed best.")).toBeTruthy();
  expect(screen.getByText("12 wins · n=16 · held-out")).toBeTruthy();
  expect(screen.getByText("Section details").closest("details")?.open).toBe(false);
  expect(screen.getByText("Uncertainty and method").closest("details")?.open).toBe(false);

  const supports = screen.getByRole("link", { name: "Belief#467 Feature adds lift" });
  expect(supports.getAttribute("href")).toBe("#/lookup/belief-1");
  expect(supports.closest("details")).toBeNull();
  expect(screen.getByText("proves · supports (0.8)")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Belief#450 Possible confound" }).getAttribute("href"))
    .toBe("#/lookup/belief-2");
  expect(screen.getByText("favors · argues against (-0.5)")).toBeTruthy();
});

test("embeds custom HTML from the sandboxed page route and links to it as a full page", () => {
  const id = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";
  const artifact: ArtifactContentRevision = {
    ...structuredArtifact,
    artifact_id: id,
    format: "html",
    html: "<script>draw()</script>",
  };
  render(<ArtifactContent artifact={artifact} />);

  const frame = screen.getByTitle("Scaling directions Artifact content");
  expect(frame.tagName).toBe("IFRAME");
  expect(frame.getAttribute("sandbox")).toBe("allow-scripts allow-popups");
  expect(frame.getAttribute("referrerpolicy")).toBe("no-referrer");
  expect(frame.getAttribute("src")).toBe(`/api/artifacts/${id}/html`);
  expect(frame.hasAttribute("srcdoc")).toBe(false);

  const full = screen.getByRole("link", { name: "Open full page" });
  expect(full.getAttribute("href")).toBe(`/api/artifacts/${id}/html`);
  expect(full.getAttribute("target")).toBe("_blank");
  expect(full.getAttribute("rel")).toBe("noopener noreferrer");
});

test("a structured Artifact has no full-page link", () => {
  render(<ArtifactContent artifact={structuredArtifact} />);
  expect(screen.queryByRole("link", { name: "Open full page" })).toBeNull();
});

test("a shared link reads its exact revision and renders the artifact", async () => {
  const id = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";
  let requested = "";
  stubFetch((request) => {
    requested = new URL(request.url).pathname;
    return Response.json({ ...structuredArtifact, artifact_id: id, html: null, citations: [], scope: "team" });
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><ArtifactPage id={id} /></QueryClientProvider>);
  expect(await screen.findByRole("heading", { name: "Scaling directions" })).toBeTruthy();
  expect(screen.getByText("Shared with team")).toBeTruthy();
  expect(requested).toBe(`/api/artifacts/${id}/content`);
});

test("remounting an immutable revision reuses its cached result", async () => {
  const id = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";
  let reads = 0;
  stubFetch(() => {
    reads += 1;
    return Response.json({ ...structuredArtifact, artifact_id: id, html: null });
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const page = <QueryClientProvider client={queryClient}><ArtifactPage id={id} /></QueryClientProvider>;
  const first = render(page);
  expect(await screen.findByRole("heading", { name: "Scaling directions" })).toBeTruthy();
  first.unmount();
  render(page);
  await waitFor(() => expect(queryClient.isFetching()).toBe(0));
  expect(reads).toBe(1);
});

test("keeps citation keys unique across signed edges for the same claim", () => {
  const sameCitation = {
    record_id: "source-1", kind: "Paper", seq: 99, title: "Atlas source",
    claim_id: "claim-1", claim_kind: "Belief", claim_seq: 42,
    claim_title: "Scaling helps", edge_kind: "proves", valence: 0.8,
  };
  const artifact = {
    ...structuredArtifact,
    citations: [sameCitation, sameCitation, {
      record_id: "source-1", kind: "Paper", seq: 99, title: "Atlas source",
      claim_id: "claim-1", claim_kind: "Belief", claim_seq: 42,
      claim_title: "Scaling helps", edge_kind: "favors", valence: -0.5,
    }],
  };
  const error = vi.spyOn(console, "error").mockImplementation(() => {});

  try {
    render(<ArtifactContent artifact={artifact} />);

    expect(screen.getAllByRole("link", { name: "Paper#99 Atlas source" })).toHaveLength(3);
    expect(error.mock.calls.flat().join(" ")).not.toContain("same key");
  } finally {
    error.mockRestore();
  }
});

test("shows top-level artifact citations and the claim weighed by signed evidence", () => {
  const artifact = {
    ...structuredArtifact,
    citations: [{ record_id: "source-1", kind: "Paper", seq: 99, title: "Atlas source" }],
  };
  if (artifact.format !== "structured") throw new Error("Expected structured fixture");
  const sections = artifact.sections.map((section) => ({
    ...section,
    findings: section.findings.map((finding) => ({
      ...finding,
      citations: finding.citations.map((citation) => ({
        ...citation, claim_id: "claim-1", claim_kind: "Belief", claim_seq: 42, claim_title: "Scaling helps",
      })),
    })),
  }));
  render(<ArtifactContent artifact={{ ...artifact, sections }} />);
  expect(screen.getByRole("link", { name: "Paper#99 Atlas source" })).toBeTruthy();
  expect(screen.getAllByRole("link", { name: "Belief#42 Scaling helps" })).toHaveLength(2);
});

test("every accepted section remains available", () => {
  if (structuredArtifact.format !== "structured") throw new Error("Expected structured fixture");
  const sections = Array.from({ length: 24 }, (_, index) => ({
    ...structuredArtifact.sections[0]!, title: `Direction ${index + 1}`,
  }));
  render(<ArtifactContent artifact={{ ...structuredArtifact, sections }} />);
  expect(screen.getByRole("heading", { name: "Direction 24" })).toBeTruthy();
});

test("the canvas Artifact visual accepts a real record id", async () => {
  const id = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";
  stubFetch(() => Response.json({ ...structuredArtifact, artifact_id: id, html: null, citations: [], scope: "team" }));
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><Artifact workspace={null} focused={false} onWorkspaceChanged={vi.fn()}
    instance={{ id: "artifact", type: "trax.artifact", version: 1, placement: "main", record_id: id, params: {} }} /></QueryClientProvider>);
  expect(await screen.findByRole("heading", { name: "Scaling directions" })).toBeTruthy();
  expect(screen.queryByText(/Open an Artifact/)).toBeNull();
});
