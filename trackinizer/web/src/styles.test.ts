import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "vitest";

// Read from disk: Vitest serves a stylesheet, `?raw` included, as an empty
// string, and jsdom's `URL` resolves a path against a file URL as http.
const css = readFileSync(join(import.meta.dirname, "styles.css"), "utf8");

/** The declarations of the rule `selector` opens at the start of a line, by property. */
function declarations(selector: string): Map<string, string> {
  const start = css.indexOf(`\n${selector} {`);
  expect(start, `no rule for ${selector}`).toBeGreaterThan(-1);
  const body = css.slice(css.indexOf("{", start) + 1, css.indexOf("}", start));
  return new Map(
    body
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .split(";")
      .map((line) => line.split(":").map((part) => part.trim()))
      .filter(([property]) => property)
      .map(([property, ...value]) => [property!, value.join(":")]),
  );
}

test("a shortcut on a button takes its border from the theme, so the light theme draws it too", () => {
  const token = /^var\((--[\w-]+)\)$/.exec(declarations(".btn kbd").get("border-color") ?? "")?.[1];
  expect(token, "border-color is a theme token").toBeDefined();
  const dark = declarations(":root").get(token!);
  // The mock's border, which shows only on a dark or coloured button.
  expect(dark).toBe("#ffffff30");
  const light = declarations(':root[data-theme="light"]').get(token!);
  expect(light).toBeDefined();
  expect(light).not.toBe(dark);
});

/** WCAG 2's contrast ratio of two `#rrggbb` colours. */
function contrast(a: string, b: string): number {
  const luminance = (hex: string) => {
    const [r, g, b] = [1, 3, 5].map((at) => {
      const c = Number.parseInt(hex.slice(at, at + 2), 16) / 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
  };
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (light! + 0.05) / (dark! + 0.05);
}

test("no text takes the tertiary ink, under WCAG AA's 4.5:1 on every surface; text that steps back takes --muted", () => {
  for (const theme of [":root", ':root[data-theme="light"]']) {
    const tokens = declarations(theme);
    const surfaces = ["--bg", "--surface", "--surface-raised", "--surface-hover"].map((name) => tokens.get(name)!);
    expect(Math.min(...surfaces.map((surface) => contrast(tokens.get("--muted")!, surface)))).toBeGreaterThanOrEqual(4.5);
    expect(Math.min(...surfaces.map((surface) => contrast(tokens.get("--ink-tertiary")!, surface)))).toBeLessThan(4.5);
  }
  expect(textIn("--ink-tertiary")).toEqual([]);
});

test("text that steps back meets WCAG AA's 4.5:1 on a selected row, the accent's tint over the view or a panel", () => {
  for (const theme of [":root", ':root[data-theme="light"]']) {
    const tokens = declarations(theme);
    for (const surface of ["--bg", "--surface"]) {
      const selected = over(tokens.get("--accent-soft")!, tokens.get(surface)!);
      expect(contrast(tokens.get("--muted")!, selected), `${theme} --accent-soft over ${surface}`).toBeGreaterThanOrEqual(4.5);
    }
  }
});

test("no text takes --accent, under 4.5:1 on the dark theme's surfaces; text in the accent's blue takes --accent-hover", () => {
  const dark = declarations(":root");
  for (const surface of ["--bg", "--surface", "--surface-raised", "--surface-hover"]) {
    expect(contrast(dark.get("--accent")!, dark.get(surface)!), surface).toBeLessThan(4.5);
    for (const tokens of [dark, new Map([...dark, ...declarations(':root[data-theme="light"]')])]) {
      expect(contrast(tokens.get("--accent-hover")!, tokens.get(surface)!), surface).toBeGreaterThanOrEqual(4.5);
    }
  }
  // A mark (the logo, a glyph) is a graphic, which WCAG holds to 3:1, not text.
  expect(textIn("--accent").filter((line) => !/-mark\b[^{]*\{/.test(line))).toEqual([]);
});

test("each agent's colour in the console meets WCAG AA's 4.5:1 on its feed, in both themes", () => {
  const rules = [...readFileSync(join(import.meta.dirname, "console/console.css"), "utf8").matchAll(/^\.console-actor\.a(\d) \{ color: var\((--[\w-]+)\); \}$/gm)];
  // The console picks one of eight by a hash of the agent's name.
  expect(rules.map(([, hue]) => Number(hue))).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
  const dark = declarations(":root");
  for (const tokens of [dark, new Map([...dark, ...declarations(':root[data-theme="light"]')])]) {
    for (const [rule, , token] of rules) {
      expect(contrast(tokens.get(token!)!, tokens.get("--bg")!), rule).toBeGreaterThanOrEqual(4.5);
    }
  }
});

/** Each line of the app's stylesheets that colours text with `token`, as `path:line` and the line. */
function textIn(token: string): string[] {
  const src = import.meta.dirname;
  const colour = new RegExp(`(^|[\\s;{])color:\\s*var\\(${token}\\)`);
  return readdirSync(src, { recursive: true, encoding: "utf8" })
    .filter((path) => path.endsWith(".css"))
    .flatMap((path) =>
      readFileSync(join(src, path), "utf8")
        .split("\n")
        .flatMap((line, index) => (colour.test(line) ? [`${path}:${index + 1} ${line.trim()}`] : [])),
    );
}

/** The opaque colour `rgba`, a `#rrggbbaa` colour, makes laid over `base`, as the browser blends it. */
function over(rgba: string, base: string): string {
  const alpha = Number.parseInt(rgba.slice(7, 9), 16) / 255;
  const channel = (hex: string, at: number) => Number.parseInt(hex.slice(at, at + 2), 16);
  return `#${[1, 3, 5].map((at) => Math.round(channel(rgba, at) * alpha + channel(base, at) * (1 - alpha)).toString(16).padStart(2, "0")).join("")}`;
}

test("each code colour meets WCAG AA's 4.5:1 on every surface, and each theme sets its own", () => {
  const dark = declarations(":root");
  const light = declarations(':root[data-theme="light"]');
  const names = [...dark.keys()].filter((name) => name.startsWith("--code-"));
  expect(names).toEqual(["--code-key", "--code-string", "--code-number", "--code-literal", "--code-keyword"]);
  for (const tokens of [dark, new Map([...dark, ...light])]) {
    for (const name of names) {
      expect(light.has(name), `${name} in the light theme`).toBe(true);
      for (const surface of ["--bg", "--surface", "--surface-raised", "--surface-hover"]) {
        const ratio = contrast(tokens.get(name)!, tokens.get(surface)!);
        expect(ratio, `${name} on ${surface}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  }
});

/** The colour a primary button's background is: a token, or a token mixed with black, as `color-mix` does in sRGB. */
function background(value: string, tokens: Map<string, string>): string {
  const token = /^var\((--[\w-]+)\)$/.exec(value);
  if (token) return tokens.get(token[1]!)!;
  const [, name, percent] = /^color-mix\(in srgb, var\((--[\w-]+)\) (\d+)%, #000\)$/.exec(value)!;
  const hex = tokens.get(name!)!;
  return `#${[1, 3, 5].map((at) => Math.round((Number.parseInt(hex.slice(at, at + 2), 16) * Number(percent)) / 100).toString(16).padStart(2, "0")).join("")}`;
}

test("white text on a primary button meets WCAG AA's 4.5:1 in both themes, at rest and hovered", () => {
  for (const theme of [":root", ':root[data-theme="light"]']) {
    const tokens = new Map([...declarations(":root"), ...declarations(theme)]);
    for (const rule of [".btn.primary", ".btn.primary:hover"]) {
      const fill = background(declarations(rule).get("background")!, tokens);
      expect(contrast("#ffffff", fill), `${theme} ${rule} ${fill}`).toBeGreaterThanOrEqual(4.5);
    }
  }
});
