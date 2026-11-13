// Tests read stylesheets from disk to check what jsdom cannot lay out. They
// read them through this module, by rule and declaration, so how a formatter
// wraps a rule never changes what a test sees.

/** One style rule of a stylesheet. */
export interface CssRule {
  /** The prelude of each at-rule enclosing it, outermost first: `@media (...)`. */
  readonly within: readonly string[];
  readonly selectors: readonly string[];
  /** Each property's value, whitespace collapsed; a later declaration wins. */
  readonly declarations: ReadonlyMap<string, string>;
}

/** At-rules whose block holds rules rather than declarations. */
const GROUPING = /^@(media|supports|layer|container|keyframes|document|scope)\b/;

const collapse = (text: string) => text.replace(/\s+/g, " ").trim();

/** `text` cut at each `separator` outside parentheses and quotes. */
function split(text: string, separator: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let quote = "";
  let start = 0;
  for (let at = 0; at < text.length; at++) {
    const char = text[at];
    if (quote) {
      if (char === quote) quote = "";
    } else if (char === '"' || char === "'") quote = char;
    else if (char === "(") depth++;
    else if (char === ")") depth--;
    else if (char === separator && depth === 0) {
      parts.push(text.slice(start, at));
      start = at + 1;
    }
  }
  parts.push(text.slice(start));
  return parts;
}

/** Every style rule of `css`, in source order, nested ones included. */
export function cssRules(css: string): CssRule[] {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const rules: CssRule[] = [];
  const within: string[] = [];
  let prelude = 0;
  for (let at = 0; at < text.length; at++) {
    const char = text[at];
    if (char === ";") prelude = at + 1;
    else if (char === "}") {
      within.pop();
      prelude = at + 1;
    } else if (char === "{") {
      const head = collapse(text.slice(prelude, at));
      if (GROUPING.test(head)) {
        within.push(head);
        prelude = at + 1;
        continue;
      }
      const close = text.indexOf("}", at);
      const declarations = new Map<string, string>();
      for (const declaration of split(text.slice(at + 1, close), ";")) {
        const colon = declaration.indexOf(":");
        if (colon > 0)
          declarations.set(
            declaration.slice(0, colon).trim(),
            collapse(declaration.slice(colon + 1)),
          );
      }
      rules.push({
        within: [...within],
        selectors: split(head, ",").map(collapse),
        declarations,
      });
      at = close;
      prelude = close + 1;
    }
  }
  return rules;
}

/** The declarations of the first top-level rule whose selector list is `selector`. */
export function declarations(
  css: string,
  selector: string,
): ReadonlyMap<string, string> {
  const wanted = split(selector, ",").map(collapse).join(", ");
  const rule = cssRules(css).find(
    (each) => each.within.length === 0 && each.selectors.join(", ") === wanted,
  );
  if (!rule) throw new Error(`no rule for ${selector}`);
  return rule.declarations;
}
