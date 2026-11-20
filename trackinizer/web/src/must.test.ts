import { expect, test } from "vitest";
import { must } from "./must";

test("a present value passes through, falsy ones included", () => {
  const object = { a: 1 };
  expect(must(object)).toBe(object);
  expect([must(0), must(""), must(false), must(Number.NaN)]).toEqual([
    0,
    "",
    false,
    Number.NaN,
  ]);
});

test("null and undefined throw, naming which", () => {
  expect(() => must(null)).toThrow(new TypeError("Expected a value, got null"));
  expect(() => must(undefined)).toThrow(
    new TypeError("Expected a value, got undefined"),
  );
});
