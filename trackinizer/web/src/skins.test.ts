import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "vitest";
import { cssRules, declarations } from "./cssRules";
import { SKINS } from "./theme";
import { must } from "./must";

// Read from disk: Vitest serves a stylesheet as an empty string.
const base = readFileSync(join(import.meta.dirname, "styles.css"), "utf8");
const skins = readFileSync(join(import.meta.dirname, "skins.css"), "utf8");

/** The custom properties the top-level rule `selector` declares, by name. */
function tokens(css: string, selector: string): Map<string, string> {
  return new Map(
    [...declarations(css, selector)].filter(([name]) => name.startsWith("--")),
  );
}

/** WCAG 2's contrast ratio of two `#rrggbb` colours. */
function contrast(a: string, b: string): number {
  const luminance = (hex: string) => {
    const [r, g, bl] = [1, 3, 5].map((at) => {
      const c = Number.parseInt(hex.slice(at, at + 2), 16) / 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * must(r) + 0.7152 * must(g) + 0.0722 * must(bl);
  };
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (must(light) + 0.05) / (must(dark) + 0.05);
}

const fixed = SKINS.map((each) => each.skin).filter((skin) => skin !== "default");

/** Every colour token the dark theme defines: what a skin must define too, or it shows the theme's beside its own. */
const colours = [...tokens(base, ":root")]
  .filter(([, value]) => value.startsWith("#") || value.startsWith("var("))
  .map(([name]) => name);

test.each(fixed)("the %s skin redefines every colour token the theme does", (skin) => {
  const own = tokens(skins, `:root[data-skin="${skin}"]`);
  expect(colours.filter((name) => !own.has(name))).toEqual([]);
});

test.each(fixed)(
  "in the %s skin, text, text that steps back and error text meet WCAG AA's 4.5:1 on every surface",
  (skin) => {
    const own = tokens(skins, `:root[data-skin="${skin}"]`);
    const surfaces = [
      "--bg",
      "--surface",
      "--surface-raised",
      "--surface-hover",
      "--surface-overlay",
    ].map((name) => must(own.get(name)));
    for (const ink of ["--text", "--muted", "--error-text"]) {
      expect(
        Math.min(...surfaces.map((surface) => contrast(must(own.get(ink)), surface))),
        ink,
      ).toBeGreaterThanOrEqual(4.5);
    }
  },
);

test.each(fixed)(
  "in the %s skin, a primary button's white text reads at 4.5:1",
  (skin) => {
    const own = tokens(skins, `:root[data-skin="${skin}"]`);
    expect(contrast("#ffffff", must(own.get("--accent")))).toBeGreaterThanOrEqual(4.5);
  },
);

test("every skin the menu offers has its rule, and the stylesheet holds no other", () => {
  const ruled = cssRules(skins).flatMap((rule) =>
    rule.selectors.flatMap(
      (selector) => /^:root\[data-skin="([a-z]+)"\]$/.exec(selector)?.[1] ?? [],
    ),
  );
  expect(ruled).toEqual(fixed);
});
