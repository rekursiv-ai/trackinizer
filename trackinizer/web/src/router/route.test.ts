import { expect, test } from "vitest";
import { formatRoute, parseHash, type Route } from "./route";

const KINDS = ["Issue", "Artifact", "CodeChange"];
const ID = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";

test("every route formats to a hash that parses back to it", () => {
  const routes: Route[] = [
    { name: "list", kind: "CodeChange" },
    { name: "ref", kind: "Issue", seq: 412 },
    { name: "lookup", id: ID },
    { name: "activity" },
    { name: "settings" },
    { name: "admin" },
    { name: "search", q: "retry jitter" },
    { name: "search", q: "" },
    { name: "new", kind: "CodeChange" },
    { name: "graph" },
    { name: "graph", focus: { ref: { kind: "Issue", seq: 21919 }, hops: 2 } },
    { name: "graph", focus: { ref: { id: ID }, hops: "all" } },
    { name: "graph", grouped: false },
    { name: "graph", focus: { ref: { kind: "Issue", seq: 7 }, hops: 1 }, grouped: false },
  ];
  for (const route of routes) {
    expect(parseHash(formatRoute(route), KINDS), formatRoute(route)).toEqual(route);
  }
});

test("an encoded ? or / in a search survives routing (COLD-11)", () => {
  for (const q of ["what?", "a/b", "50%", "title:re ^x?y$", "Issue#12"]) {
    const hash = formatRoute({ name: "search", q });
    expect(hash.slice("#/search/".length)).not.toMatch(/[?/#]/);
    expect(parseHash(hash, KINDS)).toEqual({ name: "search", q });
  }
  // Split before decoding: the encoded ? stays in the query text.
  expect(parseHash("#/search/why%3Fnot", KINDS)).toEqual({ name: "search", q: "why?not" });
  // An unencoded / joins the parts; a literal ? starts the hash's own query.
  expect(parseHash("#/search/a/b", KINDS)).toEqual({ name: "search", q: "a/b" });
  expect(parseHash("#/search/a?lens=x", KINDS)).toEqual({ name: "search", q: "a" });
});

test("the old UI's links redirect to their v2 routes", () => {
  expect(parseHash(`#/inquiry/${ID}`, KINDS)).toEqual({ name: "lookup", id: ID });
  expect(parseHash("#/recent", KINDS)).toEqual({ name: "activity" });
  expect(parseHash("#/search?q=why%3F%20not", KINDS)).toEqual({ name: "search", q: "why? not" });
  expect(parseHash("#/search?q=a+b", KINDS)).toEqual({ name: "search", q: "a b" });
  expect(parseHash("#/search", KINDS)).toEqual({ name: "search", q: "" });
  // The old UI's New menu links to the create form the way v2 does.
  expect(parseHash("#/new/Issue", KINDS)).toEqual({ name: "new", kind: "Issue" });
  expect(formatRoute({ name: "new", kind: "Issue" })).toBe("#/new/Issue");
});

test("a bare #/list or #/new opens the first kind, Issues, as the old UI's did (B6)", () => {
  for (const hash of ["#/list", "#/list/"]) expect(parseHash(hash, KINDS), hash).toEqual({ name: "list", kind: "Issue" });
  for (const hash of ["#/new", "#/new/"]) expect(parseHash(hash, KINDS), hash).toEqual({ name: "new", kind: "Issue" });
});

test("a list's Columns view keeps its path, the seqs selected column by column, in the hash (LV2)", () => {
  const drilled: Route = { name: "list", kind: "Issue", columns: [21933, 21934] };
  expect(formatRoute(drilled)).toBe("#/list/Issue?view=columns&path=21933,21934");
  expect(parseHash("#/list/issue?view=columns&path=21933,21934", KINDS)).toEqual(drilled);
  expect(formatRoute({ name: "list", kind: "Issue", columns: [] })).toBe("#/list/Issue?view=columns");
  expect(parseHash("#/list/Issue?view=columns", KINDS)).toEqual({ name: "list", kind: "Issue", columns: [] });
  // A path that names no seq names no view; another view is no part of the hash.
  expect(parseHash("#/list/Issue?view=columns&path=21933,x", KINDS).name).toBe("notFound");
  expect(parseHash("#/list/Issue?view=outline", KINDS)).toEqual({ name: "list", kind: "Issue" });
});

test("#/console opens the console and formats back to itself", () => {
  expect(parseHash("#/console", KINDS)).toEqual({ name: "console" });
  expect(formatRoute({ name: "console" })).toBe("#/console");
  expect(parseHash("#/console/x", KINDS)).toEqual({ name: "notFound", hash: "#/console/x" });
});

test("#/graph opens the graph and formats back to itself", () => {
  expect(parseHash("#/graph", KINDS)).toEqual({ name: "graph" });
  expect(formatRoute({ name: "graph" })).toBe("#/graph");
  expect(parseHash("#/graph/x", KINDS)).toEqual({ name: "notFound", hash: "#/graph/x" });
});

test("the graph keeps its focus, by Kind/seq or id, its hops and whether it is grouped in the hash", () => {
  const focused: Route = { name: "graph", focus: { ref: { kind: "Issue", seq: 21919 }, hops: 2 } };
  expect(formatRoute(focused)).toBe("#/graph?focus=Issue/21919&hops=2");
  expect(parseHash("#/graph?focus=Issue/21919&hops=2", KINDS)).toEqual(focused);
  expect(formatRoute({ name: "graph", focus: { ref: { id: ID }, hops: "all" } })).toBe(`#/graph?focus=${ID}&hops=all`);
  // A kind in any case, and no hops, which is one: canonical once rewritten.
  expect(parseHash("#/graph?focus=issue/7", KINDS)).toEqual({ name: "graph", focus: { ref: { kind: "Issue", seq: 7 }, hops: 1 } });
  // An id in capitals is the server's lower-case id.
  expect(parseHash(`#/graph?focus=${ID.toUpperCase()}`, KINDS)).toEqual({ name: "graph", focus: { ref: { id: ID }, hops: 1 } });
  // Hops with nothing to focus on are dropped.
  expect(parseHash("#/graph?hops=3", KINDS)).toEqual({ name: "graph" });
  // Grouped by root is the default; one web, ungrouped, comes last, after any focus.
  expect(formatRoute({ name: "graph", focus: { ref: { kind: "Issue", seq: 7 }, hops: 2 }, grouped: false })).toBe(
    "#/graph?focus=Issue/7&hops=2&group=none",
  );
  expect(formatRoute({ name: "graph", grouped: false })).toBe("#/graph?group=none");
  expect(parseHash("#/graph?group=none", KINDS)).toEqual({ name: "graph", grouped: false });
  // The hash grouping had before it was the default still opens grouped, canonical once rewritten.
  expect(parseHash("#/graph?group=root", KINDS)).toEqual({ name: "graph" });
  expect(parseHash("#/graph?focus=Issue/7&hops=2&group=root", KINDS)).toEqual({ name: "graph", focus: { ref: { kind: "Issue", seq: 7 }, hops: 2 } });
  for (const hash of ["#/graph?focus=Ticket/1", "#/graph?focus=Issue/x", "#/graph?focus=not-a-uuid", "#/graph?focus=Issue/1&hops=4", "#/graph?group=kind"]) {
    expect(parseHash(hash, KINDS), hash).toEqual({ name: "notFound", hash });
  }
});

test("an Artifact has only the canonical lookup route", () => {
  expect(parseHash(`#/lookup/${ID}`, KINDS)).toEqual({ name: "lookup", id: ID });
  expect(parseHash(`#/lookup/${ID.toUpperCase()}`, KINDS)).toEqual({ name: "lookup", id: ID });
  expect(parseHash(`#/report/${ID}`, KINDS)).toEqual({ name: "notFound", hash: `#/report/${ID}` });
});

test("an empty hash is the graph, the home view, whatever the kinds", () => {
  for (const hash of ["", "#", "#/"]) {
    expect(parseHash(hash, KINDS)).toEqual({ name: "graph" });
    expect(parseHash(hash, [])).toEqual({ name: "graph" });
  }
});

test("kinds match in any case and come back as the server spells them", () => {
  expect(parseHash("#/list/codechange", KINDS)).toEqual({ name: "list", kind: "CodeChange" });
  expect(parseHash("#/ref/ISSUE/7", KINDS)).toEqual({ name: "ref", kind: "Issue", seq: 7 });
  expect(parseHash("#/list/Issue/", KINDS)).toEqual({ name: "list", kind: "Issue" });
});

test("a hash that names nothing is not found and keeps its text", () => {
  for (const hash of [
    "#/list/Nope",
    "#/ref/Issue/x",
    "#/ref/Issue/-1",
    "#/ref/Issue/1/2",
    // Past 2^53 a seq would round to another one (WEB-05).
    "#/ref/Issue/9007199254740993",
    "#/lookup/not-a-uuid",
    "#/inquiry/",
    "#/activity/extra",
    "#/settings/tokens",
    "#/admin/users",
    "#/me",
    "#/search/%E0%A4%A",
    "#/new/Ticket",
    "#/new/Issue/2",
  ]) {
    expect(parseHash(hash, KINDS), hash).toEqual({ name: "notFound", hash });
  }
  expect(formatRoute({ name: "notFound", hash: "#/me" })).toBe("#/me");
});
