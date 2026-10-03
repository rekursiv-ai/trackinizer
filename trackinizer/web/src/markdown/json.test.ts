import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, expect, test, vi } from "vitest";
import { type JsonNode, parseJson } from "./json";

// A synthetic job result, as an Artifact's description holds one: one line of
// JSON with eight metrics.
const RESULT = readFileSync(join(import.meta.dirname, "testdata/job_result.json"), "utf8");

afterEach(() => {
  vi.restoreAllMocks();
});

/** The entry `key` of an object node. */
function entry(node: JsonNode | undefined, key: string): JsonNode | undefined {
  return node?.type === "object" ? node.entries.find(([name]) => name === key)?.[1] : undefined;
}

test("a whole text that is a JSON object parses, keys in order and numbers as written", () => {
  const root = parseJson(`\n  ${RESULT}\n`);
  expect(root?.type).toBe("object");
  expect(root?.type === "object" && root.entries.map(([key]) => key)).toEqual([
    "classification",
    "error",
    "experiment_id",
    "job_id",
    "payload",
    "result_filename",
    "result_sha256",
    "schema_version",
    "submission_category",
  ]);
  expect(entry(root, "classification")).toEqual({ type: "string", value: "MEASURED" });
  expect(entry(root, "schema_version")).toEqual({ type: "number", text: "1" });
  const payload = entry(root, "payload");
  expect(entry(payload, "eval/train_sec")).toEqual({ type: "number", text: "600.0" });
  expect(entry(payload, "eval/eval_sec")).toEqual({ type: "number", text: "25.768875706878884" });
});

test("what JSON.parse would change shows as written: a long integer, integer-like keys, a repeated key", () => {
  const root = parseJson('{"b": 9007199254740993, "2": -1.50e+3, "1": [true, false, null], "b": "again"}');
  expect(root).toEqual({
    type: "object",
    entries: [
      ["b", { type: "number", text: "9007199254740993" }],
      ["2", { type: "number", text: "-1.50e+3" }],
      ["1", { type: "array", items: ["true", "false", "null"].map((text) => ({ type: "literal", text })) }],
      ["b", { type: "string", value: "again" }],
    ],
  });
});

test("strings and keys are decoded, escapes included", () => {
  const root = parseJson(String.raw`["a\nb é \"q\" \\ \/", {"k\"ey": ""}]`);
  expect(root).toEqual({
    type: "array",
    items: [
      { type: "string", value: 'a\nb é "q" \\ /' },
      { type: "object", entries: [['k"ey', { type: "string", value: "" }]] },
    ],
  });
});

test("anything else is not JSON: a scalar, Markdown that starts with a bracket, invalid or trailing text", () => {
  for (const text of ["", "42", '"x"', "true", "[the plan](https://example.com/plan)", "{bad", '{"a": 1} and more', "[1, 2,]", "{'a': 1}", "[NaN]"]) {
    expect(parseJson(text), text).toBeUndefined();
  }
});

test("only a text that starts with { or [ is ever parsed", () => {
  const parse = vi.spyOn(JSON, "parse");
  for (const text of ['Result: {"a": 1}', "# Heading\n\n[1]", "plain words"]) expect(parseJson(text)).toBeUndefined();
  expect(parse).not.toHaveBeenCalled();
});

test("nesting deeper than the call stack allows still parses", () => {
  const depth = 20_000;
  let node = parseJson(`${"[".repeat(depth)}${"]".repeat(depth)}`);
  let levels = 0;
  while (node?.type === "array") {
    levels += 1;
    node = node.items[0];
  }
  expect(levels).toBe(depth);
});
