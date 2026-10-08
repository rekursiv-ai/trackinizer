import { afterEach, expect, test } from "vitest";
import { startTrail, validHash, visited } from "./trail";

let stop = () => {};
afterEach(() => {
  stop();
  history.replaceState(null, "", "#/");
});

function go(hash: string) {
  history.replaceState(null, "", hash);
  dispatchEvent(new HashChangeEvent("hashchange"));
}

function start(hash = "#/graph") {
  history.replaceState(null, "", hash);
  stop = startTrail();
}

test("the trail starts with the page the app opened on and follows every hash after it", () => {
  start("#/list/Issue");
  go("#/ref/Issue/7");
  go("#/activity");
  expect(visited()).toEqual(["#/list/Issue", "#/ref/Issue/7", "#/activity"]);
});

test("a hash visited twice in a row is one entry; coming back later is another", () => {
  start("#/graph");
  go("#/graph");
  go("#/console");
  go("#/console");
  go("#/graph");
  expect(visited()).toEqual(["#/graph", "#/console", "#/graph"]);
});

test("a hash that is not #/-prefixed or is over 512 characters is dropped", () => {
  start("#/graph");
  for (const bad of ["#", "#top", `#/search/${"a".repeat(510)}`]) go(bad);
  go("#/settings");
  expect(visited()).toEqual(["#/graph", "#/settings"]);
});

test("a hash is valid up to 512 characters and with no whitespace or control character", () => {
  expect(validHash(`#/${"a".repeat(510)}`)).toBe(true);
  expect(validHash(`#/${"a".repeat(511)}`)).toBe(false);
  for (const bad of ["#/a b", "#/a\tb", "#/a\nb", "#/a\u0007b", "#/a\u007fb", "graph", "#graph"]) expect(validHash(bad), JSON.stringify(bad)).toBe(false);
});

test("the trail keeps the last 9 hashes, oldest first", () => {
  start("#/graph");
  for (let n = 1; n <= 11; n += 1) go(`#/ref/Issue/${n}`);
  expect(visited()).toEqual(Array.from({ length: 9 }, (_, index) => `#/ref/Issue/${index + 3}`));
});

test("stopping the trail ends the listening, and starting it again begins afresh", () => {
  start("#/graph");
  stop();
  go("#/console");
  expect(visited()).toEqual([]);
  start("#/settings");
  expect(visited()).toEqual(["#/settings"]);
});
