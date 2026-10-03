import { expect, test } from "vitest";
import { jumpsFor, parseSearch } from "./grammar";

const ROWS = [
  { title: "Retry the flaky upload", description: "Seen on run 42." },
  { title: "Issue 12 follow-up", description: null },
  { title: "Don't retry forever", description: "Cap it at three tries." },
  { title: "Relation far end" },
  { title: 'say "hi" twice', description: "Mentions title:x in passing." },
];

function titles(q: string): string[] {
  const test = parseSearch(q);
  return ROWS.filter(test).map((row) => row.title);
}

test("bare terms AND together over title and description, ignoring case", () => {
  expect(titles("RETRY")).toEqual(["Retry the flaky upload", "Don't retry forever"]);
  expect(titles("retry tries")).toEqual(["Don't retry forever"]);
  expect(titles("run 42")).toEqual(["Retry the flaky upload"]);
});

test("a field term is a regex only the server runs, so no loaded row matches it (WEB-02, WEB-14)", () => {
  // Postgres runs POSIX regexes, which JavaScript's disagree with (`\\m`,
  // `[[:digit:]]`, `\\b`), under a time budget a browser tab cannot impose.
  for (const q of ["title:\\d+", "description:^seen", "title:^r", "retry title:(a+)+$", "title:[[:digit:]]"]) {
    expect(titles(q), q).toEqual([]);
  }
});

test('only " groups a phrase; an apostrophe is an ordinary character', () => {
  expect(titles('"the flaky"')).toEqual(["Retry the flaky upload"]);
  expect(titles('"flaky the"')).toEqual([]);
  expect(titles("don't")).toEqual(["Don't retry forever"]);
  expect(titles('"" retry')).toEqual(["Retry the flaky upload", "Don't retry forever"]);
});

test('a quote adds nothing, "" inside a phrase too, as the server reads it', () => {
  // The server reads `"say ""hi"""` as `say hi`, and `""""` as no term at all.
  expect(titles('"say ""hi"""')).toEqual([]);
  expect(titles('"say "')).toEqual(['say "hi" twice']);
  expect(titles('""""')).toEqual([]);
});

test("a field name counts quoted too, since the server drops quotes before it looks", () => {
  expect(titles('"title:x"')).toEqual([]);
  expect(titles('title":"x')).toEqual([]);
  expect(titles('"x in"')).toEqual(['say "hi" twice']);
});

test("terms split only on space, tab, CR and LF, as the server splits them", () => {
  expect(titles("run 42")).toEqual([]);
  expect(titles("run\t42")).toEqual(["Retry the flaky upload"]);
});

test("text the server refuses, or with no terms, matches nothing", () => {
  expect(titles('"retry')).toEqual([]);
  expect(titles("title:")).toEqual([]);
  expect(titles("   ")).toEqual([]);
  expect(titles('""')).toEqual([]);
});

test("a ref names its kind, or every kind its prefix starts; a UUID names its inquiry", () => {
  const kinds = ["Issue", "Belief", "WebResult", "WebSearch"];
  expect(jumpsFor("issue#412", kinds)).toEqual([{ name: "ref", kind: "Issue", seq: 412 }]);
  expect(jumpsFor(" Iss # 7 ", kinds)).toEqual([{ name: "ref", kind: "Issue", seq: 7 }]);
  expect(jumpsFor("web#3", kinds).map((route) => route.name === "ref" && route.kind)).toEqual([
    "WebResult",
    "WebSearch",
  ]);
  expect(jumpsFor("Paper#3", kinds)).toEqual([]);
  expect(jumpsFor("0B6F7C1E-2F7A-4C55-9D7E-1F0E6B1D2A33", kinds)).toEqual([
    { name: "lookup", id: "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33" },
  ]);
  expect(jumpsFor("retry#3 now", kinds)).toEqual([]);
  // A seq past 2^53 would round to another inquiry's, so it names none (WEB-05).
  expect(jumpsFor(`Issue#${BigInt(Number.MAX_SAFE_INTEGER) + 2n}`, kinds)).toEqual([]);
  expect(jumpsFor(`Issue#${Number.MAX_SAFE_INTEGER}`, kinds)).toEqual([{ name: "ref", kind: "Issue", seq: Number.MAX_SAFE_INTEGER }]);
});
