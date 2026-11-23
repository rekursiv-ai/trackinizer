import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { Change, Detail } from "../api/detail";
import type { FocusGraph, FocusNode } from "../api/graph";
import { type Sent, stubFetch } from "../api/testing";
import type { WorkspaceState } from "../api/workspaces";
import { MetaContext, type Meta } from "../app/boot";
import { META, edge, node, uuid } from "../graph/testing";
import { testClient } from "../live/testing";
import { type FoldBox, CLAIM_LIMIT, FOLD_LIMIT, Knowledge, SOURCE_LIMIT, edgeName, foldClaims, foldParams, formation, wireRuns } from "./Knowledge";
import { canvasActions } from "./testing";
import { type WorkspaceActions, WorkspaceActionsProvider } from "./workspaceActions";

/**
 * `/api/meta/edges` as this build's server answers it. A wire's word and a source's
 * relation come from this table, never from the stored kind, so the fold's tests read
 * the words off it and a rename moves every surface at once.
 */
const LABELS: Meta = {
  ...META,
  edges: {
    ...META.edges,
    proves: { ...META.edges.proves, forward: "supports", inverse: "supported_by" },
    favors: { ...META.edges.favors, forward: "suggests", inverse: "suggested_by" },
    produced_by: { ...META.edges.produced_by, forward: "came_from", inverse: "led_to" },
  },
};

/** Belief `n`, `hops` from the focus. */
function belief(n: number, hops: number, fields: Partial<FocusNode> = {}): FocusNode {
  return { ...node(n, { kind: "Belief", title: `Claim ${n}`, ...fields }), hops };
}

/**
 * The record is belief 1; paper 2 and belief 3 a hop out, belief 4 two hops out,
 * belief 5 three. Every wire's far end came back in the read, so each block
 * counts its own: belief 1 and 3 hold two, belief 4 one.
 */
const AROUND: FocusGraph = {
  nodes: [belief(1, 0, { confidence: 0.9, judgement: "proven" }), { ...node(2, { kind: "Paper", title: "Paper 2" }), hops: 1 }, belief(3, 1, { confidence: 0.4 }), belief(4, 2), belief(5, 3)],
  edges: [edge(2, 1, "proves", { valence: 0.9 }), edge(2, 3, "favors", { valence: -0.8 }), edge(1, 4, "produced_by"), edge(5, 3, "proves", { valence: 0.5 })],
};

/** The change that put a wire in: who wrote it, the day, and the reason they gave. */
const ASSERT: Change = {
  id: uuid(500),
  created: node(1).created,
  actor: "dhanya",
  kind: "edge_added",
  subject_kind: "Belief",
  caused_by: null,
  reason: "warmup rerun",
  old: {},
  new: { peer_id: uuid(1), peer_kind: "Belief", peer_edge_kind: "proves" },
};

/** Belief 1 as its own row answers it: two papers point at it, and it points at belief 4. */
const SHEET: Detail = {
  self: { id: uuid(1), kind: "Belief", seq: 1, title: "Claim 1", status: "active", created: node(1).created, modified: node(1).created, judgement: "proven", confidence: 0.9, description: "A 2k-step warmup prevents early divergence." },
  edges: { produced_by: [{ id: uuid(4), kind: "Belief", seq: 4, title: "Claim 4", status: "active" }] },
  backlinks: {
    proves: [{ id: uuid(2), kind: "Paper", seq: 2, title: "Paper 2", status: "active", valence: 0.9 }],
    favors: [{ id: uuid(9), kind: "Paper", seq: 9, title: "Paper 9", status: "active", valence: -0.8, note: "Ran one seed." }],
  },
  changes: [ASSERT],
};

/**
 * A record's own row, keyed by the id read: each says something different, so a block's
 * body and a source's line are provably the text of the record named on them and not
 * one sheet shown twice. How many records point at it varies the same way, so a card's
 * count is read off its own row and not off the walk.
 */
function sheetFor(id: string): Detail {
  const seq = Number(id.slice(-12));
  return {
    ...SHEET,
    self: { ...SHEET.self, id, seq, description: `The body of record ${seq}.` },
    backlinks: seq % 2 === 0 ? { proves: SHEET.backlinks.proves } : SHEET.backlinks,
  };
}

let sent: Sent[];
let graph: FocusGraph;

beforeEach(() => {
  graph = AROUND;
  sent = stubFetch((request) => {
    const path = new URL(request.url).pathname;
    const read = "/api/web/get/";
    return Response.json(path.startsWith(read) ? sheetFor(decodeURIComponent(path.slice(read.length))) : graph);
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const foldReads = () => sent.filter((request) => request.path === "/api/web/graph");
const detailReads = () => sent.filter((request) => request.path.startsWith("/api/web/get/"));
/** The records whose rows were read, by first asking. A row read twice counts once. */
const readRows = () => [...new Set(detailReads().map((request) => decodeURIComponent(request.path.slice("/api/web/get/".length))))];

function instance(recordId: string | null, params: WorkspaceState["visuals"][number]["params"] = {}): WorkspaceState["visuals"][number] {
  return { id: "knowledge-instance", type: "trax.knowledge", version: 1, placement: "main", record_id: recordId, params };
}

function actions(fields: Partial<WorkspaceActions> = {}): WorkspaceActions {
  return canvasActions({ visualTypes: new Set(["trax.knowledge"]), ...fields });
}

function show(pane: WorkspaceState["visuals"][number], workspace: WorkspaceActions | null = actions()) {
  const client = testClient();
  const tree = (shown: WorkspaceState["visuals"][number]) => (
    <QueryClientProvider client={client}>
      <MetaContext value={LABELS}>
        <WorkspaceActionsProvider value={workspace}>
          <Knowledge instance={shown} workspace={null} onWorkspaceChanged={() => {}} focused />
        </WorkspaceActionsProvider>
      </MetaContext>
    </QueryClientProvider>
  );
  const view = render(tree(pane));
  return { ...view, show: (next: WorkspaceState["visuals"][number]) => view.rerender(tree(next)) };
}

/**
 * jsdom lays nothing out, so the boxes are handed to the fold: `K#n` where the styles
 * would have put it. Only a run with a gutter to turn in is drawn, and only a drawn
 * run can be clicked, so a test of a wire has to say where its two blocks landed.
 */
function layOut(boxes: Readonly<Record<string, FoldBox>>) {
  const rect = (one: FoldBox): DOMRect => ({ ...one, x: one.left, y: one.top, height: one.bottom - one.top, right: one.left + one.width, toJSON: () => ({}) }) as DOMRect;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.classList.contains("knowledge-body")) return rect({ top: 0, bottom: 700, left: 0, width: 1200 });
    const slot = this.querySelector(".ui-block-slot")?.textContent ?? "";
    return boxes[slot] ? rect(boxes[slot]!) : rect({ top: 0, bottom: 0, left: 0, width: 0 });
  });
}

/** Every block drawn, in the order the fold put them: each ends with its record count. */
async function cards(): Promise<HTMLElement[]> {
  // By class and not by role: a block's machine line carries no count until its own row
  // arrives, and a claim left open hides the whole fold from assistive tech anyway.
  const find = () => [...document.querySelectorAll<HTMLElement>(".knowledge-card")];
  await waitFor(() => expect(find().length).toBeGreaterThan(0));
  return find();
}

/** What one block shows: its slot, its headline, the body its row carries, its address line.
 *  The parts are the shared block's (ui/block.tsx), not the fold's own class names. */
function shown(card: HTMLElement) {
  const at = (part: string) => card.querySelector(`.ui-block-${part}`)?.textContent ?? "";
  return { slot: at("slot"), claim: at("claim"), text: at("line"), foot: at("foot") };
}

test("one read a hop past its reach draws one block per record within it, most-wired first", async () => {
  show(instance(uuid(1)));
  const blocks = await cards();
  expect(blocks).toHaveLength(4);
  expect(foldReads().map((request) => request.query)).toEqual([`?focus=${uuid(1)}&hops=3&limit=${FOLD_LIMIT}`]);
  // The walk carries a title and a status alone, so a block's body and its count of
  // records are read from its own row: one row per block, in the order drawn. Every kind
  // the read brings is a block, so the paper stands beside the claim it argues for, and
  // the number printed is the row's own count of records pointing at it.
  await waitFor(() => expect(blocks.map(shown)).toEqual([
    { slot: "K#1", claim: "Claim 1", text: "The body of record 1.", foot: "Belief#1 · 2 records" },
    { slot: "K#2", claim: "Paper 2", text: "The body of record 2.", foot: "Paper#2 · 1 record" },
    { slot: "K#3", claim: "Claim 4", text: "The body of record 4.", foot: "Belief#4 · 1 record" },
    { slot: "K#4", claim: "Claim 3", text: "The body of record 3.", foot: "Belief#3 · 2 records" },
  ]));
  expect(readRows()).toEqual([uuid(1), uuid(2), uuid(4), uuid(3)]);
  // Down the page is how far the walk along the stored edges stands from the record the
  // window was opened on: the anchor alone, then its paper and the claim it produced,
  // then the belief a hop further on.
  expect([...document.querySelectorAll(".knowledge-tier")].map((tier) => [...tier.querySelectorAll(".ui-block-slot")].map((slot) => slot.textContent)))
    .toEqual([["K#1"], ["K#2", "K#3"], ["K#4"]]);
  // The header is a title and one way out: reach is a parameter the window was given,
  // not something a reader is asked to choose here.
  expect(screen.queryByRole("group", { name: "Hops" })).toBeNull();
  expect(document.querySelectorAll(".knowledge-h a")).toHaveLength(1);
});

test("the blocks it draws are every record within reach, the anchor included, in the walk's own order", () => {
  const blocks = foldClaims(AROUND, uuid(1), 2);
  expect(blocks.map((block) => block.claim.seq)).toEqual([1, 2, 4, 3]);
  expect(blocks[0]!.wires.map((wire) => `${wire.peer.seq}:${wire.onto}`)).toEqual(["2:true", "4:false"]);
  // A wire shows only when both its ends came back in the read: without paper 2 the two
  // wires that ran to it are gone, the belief under it climbs a row, and holds nothing.
  const missing = { ...AROUND, nodes: AROUND.nodes.filter((row) => row.seq !== 2) };
  expect(foldClaims(missing, uuid(1), 2).map((block) => `${block.claim.seq}:${block.wires.map((wire) => wire.peer.seq)}`)).toEqual(["1:4", "4:1", "3:"]);
  // The reach counts the store's hops, not the walk's steps: the paper one edge out and
  // the belief it argues for are both blocks; the record two edges out is not.
  expect(foldClaims(AROUND, uuid(1), 1).map((block) => block.claim.seq)).toEqual([1, 2, 3]);
});

test("a run points up only from a record standing below its parent, and every other pair is said by the child's footer", () => {
  // Belief 1 supports belief 3, so the pair stands on one row and paper 2 supports the anchor from below.
  const rests = { ...AROUND, edges: [...AROUND.edges, edge(1, 3, "proves", { valence: 0.6 })] };
  const blocks = foldClaims(rests, uuid(1), 2);
  expect(blocks.map((block) => `${block.claim.seq}:${block.tier}`)).toEqual(["1:0", "3:1", "2:1", "4:1"]);
  const one = blocks.find((block) => block.claim.seq === 1)!;
  expect(one.rests.map((wire) => `${wire.peer.seq}:${wire.edge.edge_kind}`)).toEqual(["3:proves", "4:produced_by"]);
  expect(one.under.map((wire) => wire.peer.seq)).toEqual([2]);
  // The parent of an edge whose child stands on its own row draws no run up a gutter
  // and prints nothing: the pair is drawn across the row instead.
  const three = blocks.find((block) => block.claim.seq === 3)!;
  expect(three.rests).toHaveLength(0);
  expect(three.under).toHaveLength(0);
  // Paper 2 supports the anchor from the row below, so that pair is a run and is kept
  // off paper 2's own footer: a relation this fold knows about is said once.
  // Paper 2 and belief 3 stand on one row, and that pair is `across` — drawn, not
  // listed. Before it had a list of its own it was in neither: the footer did not
  // print it because the parent's side is silent, and no run crossed a row, so the
  // store held an edge that the surface said nothing about anywhere.
  // The pair is held once, on the parent's side, as `under` is: paper 2 is the stored
  // child, so its own lists are empty and belief 3 carries the wire that draws it.
  const two = blocks.find((block) => block.claim.seq === 2)!;
  expect(two.rests).toHaveLength(0);
  expect(two.across).toHaveLength(0);
  expect(three.across.map((wire) => wire.peer.seq)).toEqual([2]);
});

test("at one hop it reads one edge past, and a clicked block keeps the reach the window was given", async () => {
  const workspace = actions();
  show(instance(uuid(1), { hops: 1, claim: uuid(3) }), workspace);
  expect(await cards()).toHaveLength(3);
  // The reach came from the window's own parameters, one edge past it so each block's
  // wires arrive with it, and nothing on the surface asks for a different one.
  expect(foldReads().map((request) => request.query)).toEqual([`?focus=${uuid(1)}&hops=2&limit=${FOLD_LIMIT}`]);
  // The block left open hides everything behind it, so the blocks are clicked as they
  // are: present, whatever the dialog is doing in front of them.
  fireEvent.click((await cards())[0]!);
  expect(workspace.operate).toHaveBeenCalledWith({ kind: "show", visual_type: "trax.knowledge", record_id: uuid(1), params: { hops: 1, claim: uuid(1) } });
});

test("a wire turns in the gutter above its own row, and two blocks on one row are joined across it", () => {
  const flow: FocusGraph = {
    nodes: [belief(1, 0), belief(3, 1), belief(4, 1), belief(5, 2)],
    edges: [edge(3, 1, "proves"), edge(4, 1, "favors"), edge(5, 3, "narrows"), edge(4, 3, "proves")],
  };
  const blocks = foldClaims(flow, uuid(1), 2);
  expect(blocks.map((block) => `${block.claim.seq}:${block.tier}`)).toEqual(["1:0", "3:1", "4:1", "5:2"]);
  // The page as the styles lay it out: the anchor's column rightmost, its two supports
  // in the column left of it, and the belief under one of those one column further left.
  const boxes = new Map<string, FoldBox>([
    [uuid(1), { top: 100, bottom: 202, left: 300, width: 300 }],
    [uuid(3), { top: 246, bottom: 398, left: 60, width: 300 }],
    [uuid(4), { top: 246, bottom: 398, left: 540, width: 300 }],
    [uuid(5), { top: 442, bottom: 594, left: 300, width: 300 }],
  ]);
  const runs = wireRuns(blocks, (id) => boxes.get(id), edgeName(LABELS));
  expect(runs.map((run) => run.d).sort()).toEqual([
    // Belief 3 supports the anchor: it turns in the gutter above its own row and
    // lands on the anchor's left third — belief 4's wire takes the right third, so
    // the two never share a point on the card they both hold up.
    "M 210 246 V 224 H 400 V 202",
    // Belief 5 supports belief 3 from the row below it: the word still stands in the
    // gutter above belief 5's own row, not in the middle of the span.
    "M 450 442 V 420 H 210 V 398",
    // Belief 4 supports belief 3 and both stand on one row. There is no gutter between
    // them, so the run goes across: out of belief 4's left edge at its middle, into
    // belief 3's right edge at its. It used to be drawn by nothing, which is how a
    // fold could report `0 wires` while holding an edge in its own data.
    "M 540 322 H 450 V 322 H 360",
    "M 690 246 V 224 H 500 V 202",
  ]);
  expect(runs).toHaveLength(4);
  // No run's word sits ON a card — the whole point, tested as the point it is rather
  // than by its height alone. Height alone was enough while every run was vertical and
  // so every word was in a gutter; an across run's word shares its row's band and is
  // kept clear by standing in the gap BETWEEN the two cards, which a y-only check reads
  // as a collision. The invariant never changed, only what it takes to measure it.
  for (const run of runs) {
    expect([...boxes.values()].some((one) =>
      run.y > one.top && run.y < one.bottom && run.x > one.left && run.x < one.left + one.width)).toBe(false);
  }
  // The word is the label the server gives the stored kind, read through the same table
  // the graph's key uses, and a run carries both its records for a click to open.
  expect(runs.map((run) => run.said).sort()).toEqual(["narrows", "suggests", "supports", "supports"]);
  expect(runs.map((run) => `${run.child.seq}>${run.parent.seq}:${run.edge.edge_kind}`).sort())
    .toEqual(["3>1:proves", "4>1:favors", "4>3:proves", "5>3:narrows"]);
});

test("opening a block asks for the window that names it, then reads that claim's own wires", async () => {
  const workspace = actions();
  const view = show(instance(uuid(1)), workspace);
  fireEvent.click((await cards())[0]!);
  expect(workspace.operate).toHaveBeenCalledWith({ kind: "show", visual_type: "trax.knowledge", record_id: uuid(1), params: { hops: 2, claim: uuid(1) } });
  view.show(instance(uuid(1), { claim: uuid(1) }));
  const dialog = await screen.findByRole("dialog");
  // The opened claim's row was already read for its block, so the records wired into
  // it are the only new ones, and each is read for the line it carries.
  const rows = () => [...dialog.querySelectorAll(".knowledge-sources li")];
  await waitFor(() => expect(rows()).toHaveLength(2));
  expect(readRows()).toEqual([uuid(1), uuid(2), uuid(4), uuid(3), uuid(9)]);
  // The panel opens on the library's kicker line: the block's address in the purple
  // that marks a block, and what its row answers at the far end of it.
  expect(dialog.querySelector(".ui-panel-kicker b")?.textContent).toBe("K#1");
  expect(dialog.querySelector(".ui-panel-kicker i")?.textContent).toBe("active · 2 records");
  expect(dialog.querySelector(".ui-panel-h")?.textContent).toBe("Claim 1");
  expect(screen.getByText("How it was formed").textContent).toBe("How it was formed");
  // The claim heads the spine in its own box, whole: the card behind clamps it to four lines.
  expect(dialog.querySelector(".knowledge-me .knowledge-src-line")?.textContent).toBe("The body of record 1.");
  // Its own row prints only what it rests on, in the voice the server's edge table gives
  // that kind: the records that point INTO it are the list below, and a hub's 36 of them
  // would be said twice and bury that list. This block's parent stands on its own row, so
  // no wire runs between them and this line is the only place that edge is said.
  expect(dialog.querySelector(".knowledge-into")?.textContent).toBe("→ came from K#3");
  // Strongest wire first, and a source row is its address, its own headline, the line
  // its record holds, and the machine row of the wire that brought it.
  expect(rows().map((row) => row.querySelector(".knowledge-src-h")?.textContent)).toEqual(["Paper#2Paper 2", "Paper#9Paper 9"]);
  expect(rows().map((row) => row.querySelector(".knowledge-src-line")?.textContent)).toEqual([
    "The body of record 2.", "The body of record 9.",
  ]);
  expect(rows().map((row) => row.querySelector(".knowledge-src-note")?.textContent)).toEqual(["+0.9 for", "-0.8 against · Ran one seed."]);
});

test("a hub's panel lists the strongest of its sources, says how many it left out, and reads no other row", async () => {
  const crowd = Array.from({ length: SOURCE_LIMIT + 2 }, (_, i) => ({
    id: uuid(100 + i), kind: "Paper", seq: 100 + i, title: `Paper ${100 + i}`, status: "active", valence: 1 - i / 100,
  }));
  sent = stubFetch((request) => {
    const path = new URL(request.url).pathname;
    const read = "/api/web/get/";
    if (!path.startsWith(read)) return Response.json(graph);
    const id = decodeURIComponent(path.slice(read.length));
    return Response.json(id === uuid(1) ? { ...SHEET, backlinks: { proves: crowd } } : sheetFor(id));
  });
  show(instance(uuid(1), { claim: uuid(1) }));
  const dialog = await screen.findByRole("dialog");
  const rows = () => [...dialog.querySelectorAll(".knowledge-sources li")];
  await waitFor(() => expect(rows()).toHaveLength(SOURCE_LIMIT));
  // The head line still answers with the whole count, and the list says what it cut:
  // the two facts are the panel's own, and no row beyond the ones it draws is asked for.
  expect(dialog.querySelector(".ui-panel-kicker i")?.textContent).toBe(`active · ${SOURCE_LIMIT + 2} records`);
  expect(dialog.querySelector(".knowledge-rest")?.textContent).toBe("2 more formed it");
  const read = readRows();
  expect(crowd.filter((row) => read.includes(row.id))).toHaveLength(SOURCE_LIMIT);
  expect(read).not.toContain(uuid(100 + SOURCE_LIMIT));
});

test("the records that formed a claim come off its row's backlinks, strongest wire first", () => {
  expect(formation(SHEET).map((row) => `${row.kind}:${row.peer.seq}`)).toEqual(["proves:2", "favors:9"]);
  expect(LABELS.edges.proves.forward).toBe("supports");
  // A claim with nothing on it opens to that answer, not to an empty list.
  expect(formation({ ...SHEET, backlinks: {} })).toEqual([]);
});

test("a wire opened says the two records it joins, what the store writes on it, and who put it there", async () => {
  layOut({
    "K#1": { top: 100, bottom: 202, left: 300, width: 300 },
    "K#2": { top: 246, bottom: 398, left: 60, width: 300 },
    "K#3": { top: 246, bottom: 398, left: 540, width: 300 },
    "K#4": { top: 442, bottom: 594, left: 300, width: 300 },
  });
  show(instance(uuid(1)));
  await cards();
  // Only the paper under the anchor has a run drawn to it; every other pair this fold
  // knows about is on a footer, and a footer is not a wire to click.
  const wire = screen.getByRole("button", { name: "Paper#2 supports Belief#1" });
  fireEvent.click(wire);
  const dialog = await screen.findByRole("dialog");
  // Both ends were read for their own blocks, so opening a wire asks for nothing.
  expect(readRows()).toEqual([uuid(1), uuid(2), uuid(4), uuid(3)]);
  expect(dialog.querySelector(".ui-panel-h")?.textContent).toBe("Paper#2 supports Belief#1");
  expect([...dialog.querySelectorAll(".knowledge-sec")].map((one) => one.textContent)).toEqual(["What it starts at", "What it points at", "On the wire"]);
  // Each end is the record named on it, in its own recorded words.
  expect([...dialog.querySelectorAll(".knowledge-src-line")].map((one) => one.textContent)).toEqual(["The body of record 2.", "The body of record 1."]);
  const notes = [...dialog.querySelectorAll(".knowledge-src-note")].map((one) => one.textContent);
  expect(notes[0]).toBe("+0.9 for");
  expect(notes[1]).toContain("dhanya");
  expect(notes[1]).toContain("warmup rerun");
  expect(dialog.querySelector(".knowledge-none")).toBeNull();
  fireEvent.keyDown(dialog, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});

test("a block closed with Escape opens again, so one shut panel never stills the fold", async () => {
  const view = show(instance(uuid(1), { claim: uuid(1) }));
  const panel = await screen.findByRole("dialog");
  await waitFor(() => expect(panel.querySelectorAll(".knowledge-sources li")).toHaveLength(2));
  fireEvent.keyDown(panel, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  // The station's answer to the close lands: the window holds no open block now.
  view.show(instance(uuid(1)));
  await waitFor(async () => expect((await cards())[0]!.getAttribute("aria-pressed")).toBe("false"));
  // Asking for a block after that is a new answer, and it has to come back. The close
  // leaves the panel's own hide flag set, and a mounted panel is the only thing that
  // can clear it — measured live, where one Escape kept every block shut until the
  // page was reloaded.
  fireEvent.click((await cards())[0]!);
  view.show(instance(uuid(1), { claim: uuid(1) }));
  await screen.findByRole("dialog");
});

test("its parameters fall back to two hops and no open block, and keep only a real id", () => {
  expect(foldParams({})).toEqual({ hops: 2, claim: "" });
  expect(foldParams({ hops: 9, claim: "not-an-id" })).toEqual({ hops: 2, claim: "" });
  expect(foldParams({ hops: "2", claim: 7 })).toEqual({ hops: 2, claim: "" });
  expect(foldParams({ hops: 1, claim: ` ${uuid(4).toUpperCase()} ` })).toEqual({ hops: 1, claim: uuid(4) });
});

test("a second open of the same block closes it, and a window with no claim opens nothing", async () => {
  const workspace = actions();
  const view = show(instance(uuid(1), { claim: uuid(1) }), workspace);
  const panel = await screen.findByRole("dialog");
  // Opening a block reads its own row plus every record wired into it: wait for the
  // last of those rows, so the count below is the whole set and not a snapshot mid-read.
  await waitFor(() => expect(panel.querySelectorAll(".knowledge-sources li")).toHaveLength(2));
  const opened = readRows();
  view.show(instance(uuid(1)));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  // Folding again asks for nothing new: every block's row was read while the panel was open.
  expect(readRows()).toEqual(opened);
  view.show(instance(uuid(1), { claim: uuid(1) }));
  await screen.findByRole("dialog");
  fireEvent.click((await cards())[0]!);
  expect(workspace.operate).toHaveBeenLastCalledWith({ kind: "show", visual_type: "trax.knowledge", record_id: uuid(1), params: { hops: 2, claim: "" } });
});

test("a record with nothing wired to it draws its own block alone, and a read that brings nothing says so", async () => {
  graph = { nodes: [belief(7, 0)], edges: [] };
  const view = show(instance(uuid(7)));
  expect((await cards()).map((card) => shown(card).slot)).toEqual(["K#1"]);
  expect(document.querySelectorAll(".knowledge-tier")).toHaveLength(1);
  expect(document.querySelector(".knowledge-none")).toBeNull();
  graph = { nodes: [], edges: [] };
  view.show(instance(uuid(8)));
  expect(await screen.findByText(/Nothing in the graph reaches this record/)).toBeTruthy();
  expect(document.querySelectorAll(".knowledge-card")).toHaveLength(0);
});

test("a crowded fold draws its blocks and says how many it left out", async () => {
  const crowd = Array.from({ length: CLAIM_LIMIT + 2 }, (_, i) => belief(100 + i, 1));
  graph = { nodes: [belief(1, 0), ...crowd], edges: crowd.map((row) => edge(2, Number(row.seq), "proves")) };
  show(instance(uuid(1)));
  expect(await cards()).toHaveLength(CLAIM_LIMIT);
  // The header is the fold's own mark of leaving records out: it counts what is drawn
  // against what the reach holds, so a cut page says so itself and the canvas holds
  // nothing but blocks.
  expect(screen.getByText(`${CLAIM_LIMIT} of ${crowd.length + 1} within 2 hops`)).toBeTruthy();
});

test("without a record it says to open one, and reads nothing", () => {
  show(instance(null));
  expect(screen.getByText("Open a record to see what the graph claims of it.")).toBeTruthy();
  expect(sent).toHaveLength(0);
});
