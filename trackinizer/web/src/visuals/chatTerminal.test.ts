import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "vitest";

// Read from disk: Vitest serves a stylesheet as an empty string.
const css = readFileSync(join(import.meta.dirname, "chatTerminal.css"), "utf8");

test("the palette is GNOME Terminal's own, not the bright colours typed into Slack", () => {
  for (const hex of ["#171421", "#c01c28", "#26a269", "#a2734c", "#12488b", "#a347ba", "#2aa1b3", "#d0cfcc",
    "#5e5c64", "#f66151", "#33d17a", "#e9ad0c", "#2a7bde", "#c061cb", "#33c7de", "#ffffff"]) {
    expect(css).toContain(hex);
  }
  for (const wrong of ["#2468bc", "#b159bb", "#2fb9ce", "#f2f2f2"]) expect(css).not.toContain(wrong);
});

test("every rule is scoped to the Chat panel, or the column whose width it sets, in the Terminal style", () => {
  const selectors = css.replace(/\/\*[\s\S]*?\*\//g, "").split("}").map((rule) => rule.split("{")[0]!.trim()).filter(Boolean);
  expect(selectors.length).toBeGreaterThan(20);
  for (const list of selectors) {
    for (const selector of list.split(",")) expect(selector.trim()).toMatch(/^:root\[data-chat-style="terminal"\] \.chat-(panel|column)\b/);
  }
});

test("the 8-bit constraints hold: no radius, shadow, gradient, blur, transition or animation is drawn", () => {
  expect(css.replace(/\/\*[\s\S]*?\*\//g, "")).not.toMatch(/gradient|blur\(|:hover\s*\{[^}]*(background|transform|box-shadow)/);
  expect(css).toMatch(/border-radius: 0;/);
  expect(css).toMatch(/transition: none; animation: none;/);
});
