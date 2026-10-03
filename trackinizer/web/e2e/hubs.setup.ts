import { writeFileSync } from "node:fs";
import { expect, test as setup } from "@playwright/test";
import { DETAIL_CHILDREN, hubsFile, PERF_CHILDREN } from "./hubs";

// The two hubs the detail specs read, made before any spec file starts: the
// batch that links a hub's children holds PGlite's one writer for as long as it
// runs, and its cost grows faster than the hub (61 children took 4.0 s, 120 took
// 16.6 s, measured on the Mac; 60 took 16 s on a loaded 128-core host). Made in a
// spec's beforeAll, it ran beside the other spec files' first page loads and
// stalled them past their 10 s.

setup("the hubs the detail specs read", async ({ request }) => {
  // Seeding, not a budget: the two batches ran 32 s on that loaded host.
  setup.setTimeout(120_000);
  const post = async (items: object[], edges: object[]) => {
    const response = await request.post("/api/inquiries/batch", {
      data: { items: items.map((item) => ({ ...item, idempotency_key: crypto.randomUUID() })), edges },
    });
    expect(response.ok(), await response.text()).toBe(true);
    return ((await response.json()) as { ids: string[] }).ids;
  };

  // detail.spec.ts: one hub, one of whose children also narrows a second parent,
  // and a Belief with a proving Paper.
  const issue = (title: string, extra: object = {}) => ({ kind: "Issue", title, ...extra });
  const [second, belief, paper] = [DETAIL_CHILDREN + 1, DETAIL_CHILDREN + 2, DETAIL_CHILDREN + 3];
  const detail = await post(
    [
      issue("Detail hub", {
        description: "See Belief#1, [the plan](https://example.com/plan) and `Issue#1` in code.",
        priority: 10,
      }),
      ...Array.from({ length: DETAIL_CHILDREN }, (_, k) => issue(`Hub child ${k}`)),
      issue("Second parent"),
      { kind: "Belief", title: "Detail belief", judgement: "proven", confidence: 0.9 },
      { kind: "Paper", title: "Detail paper" },
    ],
    [
      ...Array.from({ length: DETAIL_CHILDREN }, (_, k) => ({
        from_index: k + 1,
        to_index: 0,
        edge_kind: "narrows",
        ...(k === 0 && { priority: 0 }),
      })),
      { from_index: 1, to_index: second, edge_kind: "narrows" },
      { from_index: paper, to_index: belief, edge_kind: "proves", valence: 0.8 },
    ],
  );

  // phase1/performance.spec.ts: each child also counts as produced by the hub,
  // the provenance the first structural edge infers, and one Artifact it
  // produced, so the detail draws 121 relation rows in two groups.
  // Digits only: a hex tag can hold "c4", and the palette spec's search for
  // "C4 hub" then finds this hub too.
  const tag = String(crypto.getRandomValues(new Uint32Array(1))[0]);
  const [perf] = await post(
    [
      { kind: "Issue", title: `Perf hub ${tag}`, description: "A hub with **many** children." },
      ...Array.from({ length: PERF_CHILDREN }, (_, n) => ({ kind: "Issue", title: `Perf child ${n} ${tag}` })),
      { kind: "Artifact", title: `Perf artifact ${tag}` },
    ],
    [
      ...Array.from({ length: PERF_CHILDREN }, (_, n) => ({ from_index: n + 1, to_index: 0, edge_kind: "narrows" })),
      { from_index: PERF_CHILDREN + 1, to_index: 0, edge_kind: "produced_by" },
    ],
  );

  writeFileSync(hubsFile(setup.info()), JSON.stringify({ detail, perf }));
});
