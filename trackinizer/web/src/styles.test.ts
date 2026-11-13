import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "vitest";
import { cssRules, declarations } from "./cssRules";
import { must } from "./must";

// Read from disk: Vitest serves a stylesheet, `?raw` included, as an empty
// string, and jsdom's `URL` resolves a path against a file URL as http.
const css = readFileSync(join(import.meta.dirname, "styles.css"), "utf8");

test("a shortcut on a button takes its border from the theme, so the light theme draws it too", () => {
  const token = /^var\((--[\w-]+)\)$/.exec(
    declarations(css, ".btn kbd").get("border-color") ?? "",
  )?.[1];
  expect(token, "border-color is a theme token").toBeDefined();
  const dark = declarations(css, ":root").get(must(token));
  // The mock's border, which shows only on a dark or coloured button.
  expect(dark).toBe("#ffffff30");
  const light = declarations(css, ':root[data-theme="light"]').get(must(token));
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
    return 0.2126 * must(r) + 0.7152 * must(g) + 0.0722 * must(b);
  };
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (must(light) + 0.05) / (must(dark) + 0.05);
}

test("no text takes the tertiary ink, under WCAG AA's 4.5:1 on every surface; text that steps back takes --muted", () => {
  for (const theme of [":root", ':root[data-theme="light"]']) {
    const tokens = declarations(css, theme);
    const surfaces = ["--bg", "--surface", "--surface-raised", "--surface-hover"].map(
      (name) => must(tokens.get(name)),
    );
    expect(
      Math.min(
        ...surfaces.map((surface) => contrast(must(tokens.get("--muted")), surface)),
      ),
    ).toBeGreaterThanOrEqual(4.5);
    expect(
      Math.min(
        ...surfaces.map((surface) =>
          contrast(must(tokens.get("--ink-tertiary")), surface),
        ),
      ),
    ).toBeLessThan(4.5);
  }
  expect(textIn("--ink-tertiary")).toEqual([]);
});

test("text that steps back meets WCAG AA's 4.5:1 on a selected row, the accent's tint over the view or a panel", () => {
  for (const theme of [":root", ':root[data-theme="light"]']) {
    const tokens = declarations(css, theme);
    for (const surface of ["--bg", "--surface"]) {
      const selected = over(
        must(tokens.get("--accent-soft")),
        must(tokens.get(surface)),
      );
      expect(
        contrast(must(tokens.get("--muted")), selected),
        `${theme} --accent-soft over ${surface}`,
      ).toBeGreaterThanOrEqual(4.5);
    }
  }
});

test("no text takes --accent, under 4.5:1 on the dark theme's surfaces; text in the accent's blue takes --accent-hover", () => {
  const dark = declarations(css, ":root");
  for (const surface of ["--bg", "--surface", "--surface-raised", "--surface-hover"]) {
    expect(
      contrast(must(dark.get("--accent")), must(dark.get(surface))),
      surface,
    ).toBeLessThan(4.5);
    for (const tokens of [
      dark,
      new Map([...dark, ...declarations(css, ':root[data-theme="light"]')]),
    ]) {
      expect(
        contrast(must(tokens.get("--accent-hover")), must(tokens.get(surface))),
        surface,
      ).toBeGreaterThanOrEqual(4.5);
    }
  }
  // A mark (the logo, a glyph) is a graphic, which WCAG holds to 3:1, not text.
  expect(textIn("--accent").filter((rule) => !/-mark\b/.test(rule))).toEqual([]);
});

test("each agent's colour in the console meets WCAG AA's 4.5:1 on its feed, in both themes", () => {
  const rules = cssRules(
    readFileSync(join(import.meta.dirname, "console/console.css"), "utf8"),
  ).flatMap((rule) => {
    const hue = /^\.console-actor\.a(\d)$/.exec(rule.selectors.join(", "))?.[1];
    const token = /^var\((--[\w-]+)\)$/.exec(rule.declarations.get("color") ?? "")?.[1];
    return hue && token ? [[must(rule.selectors[0]), hue, token] as const] : [];
  });
  // The console picks one of eight by a hash of the agent's name.
  expect(rules.map(([, hue]) => Number(hue))).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
  const dark = declarations(css, ":root");
  for (const tokens of [
    dark,
    new Map([...dark, ...declarations(css, ':root[data-theme="light"]')]),
  ]) {
    for (const [rule, , token] of rules) {
      expect(
        contrast(must(tokens.get(token)), must(tokens.get("--bg"))),
        rule,
      ).toBeGreaterThanOrEqual(4.5);
    }
  }
});

/** Each rule of the app's stylesheets that colours text with `token`, as its path and selectors. */
function textIn(token: string): string[] {
  const src = import.meta.dirname;
  const colour = new RegExp(`^var\\(${token}\\)`);
  return readdirSync(src, { recursive: true, encoding: "utf8" })
    .filter((path) => path.endsWith(".css"))
    .flatMap((path) =>
      cssRules(readFileSync(join(src, path), "utf8"))
        .filter((rule) => colour.test(rule.declarations.get("color") ?? ""))
        .map((rule) => `${path} ${rule.selectors.join(", ")}`),
    );
}

/** The opaque colour `rgba`, a `#rrggbbaa` colour, makes laid over `base`, as the browser blends it. */
function over(rgba: string, base: string): string {
  const alpha = Number.parseInt(rgba.slice(7, 9), 16) / 255;
  const channel = (hex: string, at: number) =>
    Number.parseInt(hex.slice(at, at + 2), 16);
  return `#${[1, 3, 5]
    .map((at) =>
      Math.round(channel(rgba, at) * alpha + channel(base, at) * (1 - alpha))
        .toString(16)
        .padStart(2, "0"),
    )
    .join("")}`;
}

test("each code colour meets WCAG AA's 4.5:1 on every surface, and each theme sets its own", () => {
  const dark = declarations(css, ":root");
  const light = declarations(css, ':root[data-theme="light"]');
  const names = [...dark.keys()].filter((name) => name.startsWith("--code-"));
  expect(names).toEqual([
    "--code-key",
    "--code-string",
    "--code-number",
    "--code-literal",
    "--code-keyword",
  ]);
  for (const tokens of [dark, new Map([...dark, ...light])]) {
    for (const name of names) {
      expect(light.has(name), `${name} in the light theme`).toBe(true);
      for (const surface of [
        "--bg",
        "--surface",
        "--surface-raised",
        "--surface-hover",
      ]) {
        const ratio = contrast(must(tokens.get(name)), must(tokens.get(surface)));
        expect(ratio, `${name} on ${surface}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  }
});

/** The colour a primary button's background is: a token, or a token mixed with black, as `color-mix` does in sRGB. */
function background(value: string, tokens: Map<string, string>): string {
  const token = /^var\((--[\w-]+)\)$/.exec(value);
  if (token) return must(tokens.get(must(token[1])));
  const [, name, percent] = must(
    /^color-mix\(in srgb, var\((--[\w-]+)\) (\d+)%, #000\)$/.exec(value),
  );
  const hex = must(tokens.get(must(name)));
  return `#${[1, 3, 5]
    .map((at) =>
      Math.round((Number.parseInt(hex.slice(at, at + 2), 16) * Number(percent)) / 100)
        .toString(16)
        .padStart(2, "0"),
    )
    .join("")}`;
}

test("white text on a primary button meets WCAG AA's 4.5:1 in both themes, at rest and hovered", () => {
  for (const theme of [":root", ':root[data-theme="light"]']) {
    const tokens = new Map([
      ...declarations(css, ":root"),
      ...declarations(css, theme),
    ]);
    for (const rule of [".btn.primary", ".btn.primary:hover"]) {
      const fill = background(must(declarations(css, rule).get("background")), tokens);
      expect(
        contrast("#ffffff", fill),
        `${theme} ${rule} ${fill}`,
      ).toBeGreaterThanOrEqual(4.5);
    }
  }
});

/** Every file under `dir` whose name ends with one of `endings`, but test files. */
function sources(dir: string, endings: readonly string[]): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory())
      return entry.name === "generated" ? [] : sources(path, endings);
    return endings.some((ending) => entry.name.endsWith(ending)) &&
      !/\.test\.tsx?$/.test(entry.name)
      ? [path]
      : [];
  });
}

test("every var(--token) a stylesheet or a component uses is a token some stylesheet defines, or one with a fallback", () => {
  const files = sources(import.meta.dirname, [".css", ".tsx", ".ts"]);
  const defined = new Set(
    sources(import.meta.dirname, [".css"]).flatMap((file) =>
      [...readFileSync(file, "utf8").matchAll(/(--[\w-]+)\s*:/g)].map((match) =>
        must(match[1]),
      ),
    ),
  );
  // Custom properties a component sets inline, as `style={{ "--name": … }}`, count as defined.
  for (const file of files.filter((name) => /\.tsx?$/.test(name))) {
    for (const match of readFileSync(file, "utf8").matchAll(/["'](--[\w-]+)["']\s*:/g))
      defined.add(must(match[1]));
  }
  const undefinedUses = files.flatMap((file) =>
    [...readFileSync(file, "utf8").matchAll(/var\((--[\w-]+)\s*([,)])/g)]
      .filter(([, name, end]) => end === ")" && !defined.has(must(name)))
      .map(([, name]) => `${file.slice(import.meta.dirname.length + 1)}: ${name}`),
  );
  expect([...new Set(undefinedUses)]).toEqual([]);
});

test("a danger button draws from the theme's tokens, and the light theme's own shade never reaches a fixed skin", () => {
  const danger = declarations(css, ".btn.danger");
  expect(danger.get("color")).toBe("var(--error-text)");
  for (const property of ["background", "border-color"])
    expect(danger.get(property)).toMatch(/var\(--red\)/);
  // The light theme's shade is for the default skin alone: a skin sets its own red, which the rule above takes.
  expect(cssRules(css).flatMap((rule) => rule.selectors)).not.toContain(
    ':root[data-theme="light"] .btn.danger',
  );
  expect(
    declarations(css, ':root[data-theme="light"]:not([data-skin]) .btn.danger').get(
      "color",
    ),
  ).toBe("var(--red)");
});
