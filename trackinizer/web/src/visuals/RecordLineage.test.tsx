import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { stubFetch } from "../api/testing";
import { row } from "../detail/testing";
import { RecordLineage } from "./RecordLineage";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test.each(["in", "out"])("Paper citations preserve their %s direction", async (direction) => {
  const target = row("Paper", 1, { title: "Selected paper", description: null, outcome: null });
  const cited = row("Paper", 2, { title: "Linked paper", description: null, outcome: null });
  stubFetch((request) => {
    const url = new URL(request.url);
    const match = url.searchParams.get("direction") === direction && url.searchParams.getAll("edge_kind").includes("cites_paper");
    return Response.json({ target, records: match ? [cited] : [], edges: match ? [{
      from_id: direction === "out" ? target.id : cited.id,
      to_id: direction === "out" ? cited.id : target.id, edge_kind: "cites_paper", note: "Bibliography", valence: null,
    }] : [], next_cursor: null });
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><RecordLineage recordId={target.id} /></QueryClientProvider>);
  expect(await screen.findByRole("link", { name: /Linked paper/ })).toBeTruthy();
  expect(screen.getByText(direction === "out" ? "Paper#1 → cites_paper → Paper#2 · Bibliography" : "Paper#2 → cites_paper → Paper#1 · Bibliography")).toBeTruthy();
});
