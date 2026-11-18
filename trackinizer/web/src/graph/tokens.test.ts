import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "vitest";
import { SKINS } from "../theme";

// Read from disk: Vitest serves a stylesheet as an empty string.
const graph = readFileSync(join(import.meta.dirname, "tokens.css"), "utf8");
const skins = readFileSync(join(import.meta.dirname, "..", "skins.css"), "utf8");
const base = readFileSync(join(import.meta.dirname, "..", "styles.css"), "utf8");

/** Each rule of `css` at the start of a line: its selectors and the `#rrggbb` tokens it declares. */
function rules(css: string): { readonly selectors: readonly string[]; readonly tokens: ReadonlyMap<string, string> }[] {
  return [...css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/^([^{}\n][^{}]*)\{([^}]*)\}/gm)].map((match) => ({
    selectors: match[1]!.split(",").map((selector) => selector.trim()),
    tokens: new Map([...match[2]!.matchAll(/(--[\w-]+):\s*(#[0-9a-f]{6})\b/gi)].map((token) => [token[1]!, token[2]!])),
  }));
}

/** The tokens `css` gives the root under `selectors` (any of them matching), later rules over earlier ones. */
function resolve(css: string, matches: (selector: string) => boolean): Map<string, string> {
  const tokens = new Map<string, string>();
  for (const rule of rules(css)) if (rule.selectors.some(matches)) for (const [name, value] of rule.tokens) tokens.set(name, value);
  return tokens;
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

/** A node's and a status ring's colours: what the graph draws over its background. */
const drawn = (tokens: ReadonlyMap<string, string>) => [...tokens].filter(([name]) => /^--g-(kind|status)-/.test(name));

const dark = { graph: resolve(graph, (selector) => selector === ":root"), bg: resolve(base, (selector) => selector === ":root").get("--bg")! };
const light = {
  graph: resolve(graph, (selector) => selector === ":root" || selector.startsWith(':root[data-theme="light"]')),
  bg: resolve(base, (selector) => selector === ':root[data-theme="light"]').get("--bg")!,
};

const fixed = SKINS.map((each) => each.skin).filter((skin) => skin !== "default");

test.each(fixed)("under the %s skin the graph draws every node and ring at least as plainly as the theme of its brightness does", (skin) => {
  const own = `:root[data-skin="${skin}"]`;
  const bg = resolve(skins, (selector) => selector === own).get("--bg")!;
  const tokens = resolve(graph, (selector) => selector === ":root" || selector === own);
  const theme = contrast(bg, "#ffffff") < contrast(bg, "#000000") ? light : dark;
  // A node, and the active and invalid rings, at WCAG's 3:1 for graphics; a ring the theme draws faint on
  // purpose (complete, abandoned) at least as plain as the theme draws it.
  for (const [name, value] of drawn(tokens)) {
    const floor = Math.min(3, contrast(theme.graph.get(name)!, theme.bg));
    expect(contrast(value, bg), `${name} ${value} on ${bg}`).toBeGreaterThanOrEqual(floor);
  }
});

test("the light theme's graph colours never reach a fixed skin through the theme", () => {
  const lightRules = rules(graph).filter((rule) => rule.selectors.some((selector) => selector.includes('data-theme="light"')));
  expect(lightRules.flatMap((rule) => rule.selectors.filter((selector) => selector.includes('data-theme="light"')))).toEqual([':root[data-theme="light"]:not([data-skin])']);
});
