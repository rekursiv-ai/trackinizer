import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "vitest";
import { SKINS } from "./theme";

// Read from disk: Vitest serves a stylesheet as an empty string.
const base = readFileSync(join(import.meta.dirname, "styles.css"), "utf8");
const skins = readFileSync(join(import.meta.dirname, "skins.css"), "utf8");

/** The custom properties the rule `selector` opens at the start of a line declares, by name. */
function tokens(css: string, selector: string): Map<string, string> {
  const start = css.indexOf(`\n${selector} {`);
  expect(start, `no rule for ${selector}`).toBeGreaterThan(-1);
  const body = css.slice(css.indexOf("{", start) + 1, css.indexOf("}", start)).replace(/\/\*[\s\S]*?\*\//g, "");
  return new Map(
    body.split(";").map((line) => line.split(":").map((part) => part.trim()))
      .filter(([name]) => name?.startsWith("--"))
      .map(([name, ...value]) => [name!, value.join(":")]),
  );
}

/** WCAG 2's contrast ratio of two `#rrggbb` colours. */
function contrast(a: string, b: string): number {
  const luminance = (hex: string) => {
    const [r, g, bl] = [1, 3, 5].map((at) => {
      const c = Number.parseInt(hex.slice(at, at + 2), 16) / 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r! + 0.7152 * g! + 0.0722 * bl!;
  };
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (light! + 0.05) / (dark! + 0.05);
}

const fixed = SKINS.map((each) => each.skin).filter((skin) => skin !== "default");

/** Every colour token the dark theme defines: what a skin must define too, or it shows the theme's beside its own. */
const colours = [...tokens(base, ":root")].filter(([, value]) => value.startsWith("#") || value.startsWith("var(")).map(([name]) => name);

test.each(fixed)("the %s skin redefines every colour token the theme does", (skin) => {
  const own = tokens(skins, `:root[data-skin="${skin}"]`);
  expect(colours.filter((name) => !own.has(name))).toEqual([]);
});

test.each(fixed)("in the %s skin, text, text that steps back and error text meet WCAG AA's 4.5:1 on every surface", (skin) => {
  const own = tokens(skins, `:root[data-skin="${skin}"]`);
  const surfaces = ["--bg", "--surface", "--surface-raised", "--surface-hover", "--surface-overlay"].map((name) => own.get(name)!);
  for (const ink of ["--text", "--muted", "--error-text"]) {
    expect(Math.min(...surfaces.map((surface) => contrast(own.get(ink)!, surface))), ink).toBeGreaterThanOrEqual(4.5);
  }
});

test.each(fixed)("in the %s skin, a primary button's white text reads at 4.5:1", (skin) => {
  const own = tokens(skins, `:root[data-skin="${skin}"]`);
  expect(contrast("#ffffff", own.get("--accent")!)).toBeGreaterThanOrEqual(4.5);
});

test("every skin the menu offers has its rule, and the stylesheet holds no other", () => {
  const ruled = [...skins.matchAll(/^:root\[data-skin="([a-z]+)"\] \{/gm)].map((match) => match[1]);
  expect(ruled).toEqual(fixed);
});
