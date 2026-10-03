import { QueryClient } from "@tanstack/react-query";
import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { Detail } from "../api/detail";
import type { FocusGraph, FocusNode } from "../api/graph";
import { type Sent, stubFetch } from "../api/testing";
import { edge, node } from "../graph/testing";
import { LiveHub } from "../live/hub";
import { detail, peer, renderDetail, row, uuid } from "./testing";

// Issue 1 narrows Issue 3 and is narrowed by Issue 2, which Issue 4 narrows.
const SHOWN: Detail = detail(row("Issue", 1), { edges: { narrows: [peer("Issue", 3)] }, backlinks: { narrows: [peer("Issue", 2)] } });

/** Inquiry `n`, `hops` from the focus, as the focus read sends it. */
function at(n: number, hops: number, fields: Partial<FocusNode> = {}): FocusNode {
  return { ...node(n), hops, ...fields };
}

const AROUND: FocusGraph = {
  nodes: [at(1, 0), at(2, 1), at(3, 1), at(4, 2, { status: "complete" })],
  edges: [edge(2, 1), edge(1, 3), edge(4, 2)],
};

/** The focus read's answer: a neighbourhood, or a failure made afresh for each read. */
let neighbourhood: FocusGraph | (() => Response);
let sent: Sent[];

beforeEach(() => {
  history.replaceState(null, "", "#/ref/Issue/1");
  neighbourhood = AROUND;
  // The graph's tokens, named for what they colour; jsdom has no stylesheet.
  const root = document.documentElement;
  root.style.setProperty("--g-kind-Issue", "issue-hue");
  root.style.setProperty("--g-status-active", "active-ring");
  root.style.setProperty("--g-status-complete", "complete-ring");
  root.style.setProperty("--g-edge-narrows", "narrows-hue");
  root.style.setProperty("--text", "halo");
  sent = serve(SHOWN);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.documentElement.removeAttribute("style");
});

/** Answer the detail of `shown`, its `Kind#seq` lookup, and the focus read with `neighbourhood`. */
function serve(shown: Detail): Sent[] {
  const { id, kind, seq } = shown.self;
  return stubFetch((request) => {
    const path = new URL(request.url).pathname;
    if (path === "/api/web/graph") return typeof neighbourhood === "function" ? neighbourhood() : Response.json(neighbourhood);
    if (path === `/api/web/get/${id}`) return Response.json(shown);
    if (path === `/api/inquiries/${kind}/${seq}`) return Response.json({ id, kind });
    return Response.json({ detail: "not found" }, { status: 404 });
  });
}

const preview = () => document.querySelector<HTMLElement>(".rail-graph");
/** The nodes drawn, by id. */
const drawn = () => [...document.querySelectorAll<SVGGElement>(".rail-graph [data-id]")];
const graphReads = () => sent.filter((request) => request.path === "/api/web/graph");

test("the rail opens with the inquiry's neighbourhood two hops out, read by focus, drawn as the graph draws it", async () => {
  renderDetail({ kind: "Issue", seq: 1 });
  await waitFor(() => expect(drawn().length).toBe(4));
  expect(graphReads().map((request) => request.query)).toEqual([`?focus=${uuid(1)}&hops=2&limit=60`]);
  // Above Parents, at the rail's top.
  const parents = screen.getByRole("region", { name: "Parents" });
  expect(preview()!.compareDocumentPosition(parents) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(preview()!.parentElement!.firstElementChild).toBe(preview());
  // Hovering a node names it, by its native title.
  expect(drawn().map((item) => item.querySelector("title")!.textContent)).toEqual([
    "Issue#1 · Issue 1",
    "Issue#2 · Issue 2",
    "Issue#3 · Issue 3",
    "Issue#4 · Issue 4",
  ]);
  // The graph's look (encode.ts): the kind's fill, the status's ring, the focus's halo, and a fade two hops out.
  const disc = (n: number) => document.querySelector(`.rail-graph [data-id="${uuid(n)}"] .disc`)!;
  expect([disc(1).getAttribute("fill"), disc(1).getAttribute("stroke")]).toEqual(["issue-hue", "active-ring"]);
  expect(document.querySelector(`.rail-graph [data-id="${uuid(1)}"] .halo`)!.getAttribute("stroke")).toBe("halo");
  expect(document.querySelectorAll(".rail-graph .halo")).toHaveLength(1);
  expect(disc(4).getAttribute("stroke")).toBe("complete-ring");
  expect(disc(4).getAttribute("stroke-opacity")).toBe("0.7");
  expect([...document.querySelectorAll(".rail-graph line")].map((line) => line.getAttribute("stroke"))).toEqual(["narrows-hue", "narrows-hue", "narrows-hue"]);
  expect(preview()!.textContent).toContain("4 nodes within 2 hops");
});

test("the preview is one link, to the graph focused on the inquiry two hops out, named for it", async () => {
  renderDetail({ kind: "Issue", seq: 1 });
  const link = await screen.findByRole("link", { name: "Open in graph: Issue#1, 2 hops" });
  expect(link.getAttribute("href")).toBe("#/graph?focus=Issue/1&hops=2");
  expect(within(link).queryAllByRole("link")).toEqual([]);
  expect(link.querySelector("svg")!.getAttribute("aria-hidden")).toBe("true");
});

test("a neighbourhood the read's limit cut short says so", async () => {
  neighbourhood = { nodes: [at(1, 0), ...Array.from({ length: 59 }, (_, n) => at(n + 2, 1))], edges: [] };
  renderDetail({ kind: "Issue", seq: 1 });
  await waitFor(() => expect(drawn().length).toBe(60));
  expect(preview()!.textContent).toContain("60+ nodes within 2 hops");
});

test("an inquiry with no relations shows no preview, and reads none", async () => {
  sent = serve(detail(row("Issue", 1)));
  renderDetail({ kind: "Issue", seq: 1 });
  await screen.findByRole("heading", { name: /^Activity/ });
  expect(preview()).toBeNull();
  expect(graphReads()).toEqual([]);
});

test("in the graph's Peek, where the link would lead, there is no preview and no read", async () => {
  history.replaceState(null, "", "#/graph");
  renderDetail({ kind: "Issue", seq: 1 });
  await screen.findByRole("heading", { name: /^Activity/ });
  expect(preview()).toBeNull();
  expect(graphReads()).toEqual([]);
});

test("the preview draws in a render after the page's first, so it never holds the detail's first paint", async () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // Even read before, as on a second visit, it waits for the page.
  queryClient.setQueryData(["graph", "focus", uuid(1), 2], AROUND);
  // What each commit showed: an observer runs after every commit, before any later task.
  const commits: [boolean, number][] = [];
  const observer = new MutationObserver(() => commits.push([document.querySelector("h1") !== null, drawn().length]));
  observer.observe(document.body, { childList: true, subtree: true });
  renderDetail({ kind: "Issue", seq: 1 }, queryClient);
  await waitFor(() => expect(drawn().length).toBe(4));
  observer.disconnect();
  expect(commits.find(([page]) => page)).toEqual([true, 0]);
});

test("a read that fails says why under the preview, with Copy details; the link still leads to the graph", async () => {
  // A 4xx, which the client does not retry.
  neighbourhood = () => Response.json({ detail: "No inquiry has that id." }, { status: 404 });
  renderDetail({ kind: "Issue", seq: 1 });
  expect((await screen.findByRole("alert")).textContent).toContain("No inquiry has that id.");
  expect(within(preview()!).getByRole("button", { name: /Copy details/ })).toBeTruthy();
  expect(screen.getByRole("link", { name: "Open in graph: Issue#1, 2 hops" })).toBeTruthy();
});

test("a change to the inquiry, as adding or removing a relation writes on both ends, reads its neighbourhood again", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, advanceTimeDelta: 2 });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const hub = new LiveHub(queryClient);
  renderDetail({ kind: "Issue", seq: 1 }, queryClient, { hub });
  await waitFor(() => expect(drawn().length).toBe(4));
  neighbourhood = { ...AROUND, nodes: [...AROUND.nodes, at(5, 1)], edges: [...AROUND.edges, edge(5, 1)] };
  hub.change(uuid(1));
  await act(() => vi.advanceTimersByTimeAsync(1_000));
  await waitFor(() => expect(drawn().length).toBe(5));
  expect(graphReads()).toHaveLength(2);
  hub.stop();
});
