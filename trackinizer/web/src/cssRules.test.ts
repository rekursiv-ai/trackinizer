import { expect, test } from "vitest";
import { cssRules, declarations } from "./cssRules";
import { must } from "./must";

const ONE_LINE = `
.a, .b { color: var(--x); margin: 0 auto; }
@media (max-width: 600px) { .a { color: red; } }
`;

const WRAPPED = `
/* a comment { with braces; } */
.a,
.b {
  color: var(--x);
  margin:
    0
    auto;
}
@media (max-width: 600px) {
  .a {
    color: red;
  }
}
`;

test("a rule reads the same however it is wrapped", () => {
  expect(cssRules(WRAPPED)).toEqual(cssRules(ONE_LINE));
  expect(cssRules(ONE_LINE)).toEqual([
    {
      within: [],
      selectors: [".a", ".b"],
      declarations: new Map([
        ["color", "var(--x)"],
        ["margin", "0 auto"],
      ]),
    },
    {
      within: ["@media (max-width: 600px)"],
      selectors: [".a"],
      declarations: new Map([["color", "red"]]),
    },
  ]);
});

test("a declaration's semicolon or comma inside quotes or parentheses does not cut it", () => {
  const [rule] = cssRules(
    `a[title="x, y"] { background: url("a;b"); font: 1px "c;d"; }`,
  );
  expect(must(rule).selectors).toEqual(['a[title="x, y"]']);
  expect(must(rule).declarations).toEqual(
    new Map([
      ["background", 'url("a;b")'],
      ["font", '1px "c;d"'],
    ]),
  );
});

test("declarations finds the top-level rule by its whole selector list, and names a missing one", () => {
  expect(declarations(WRAPPED, ".a, .b").get("color")).toBe("var(--x)");
  expect(() => declarations(WRAPPED, ".a")).toThrow("no rule for .a");
});
