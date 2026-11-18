import { parseSeq } from "../router/route";

/** A row an answer names: by its kind and seq (`Issue#12`), or by its id. */
export type NamedRow = { readonly kind: string; readonly seq: number } | { readonly id: string };

/** The most rows one highlight takes, as the server bounds it. */
export const MOST_NAMED = 50;

const UUID = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}";

/**
 * The rows `text`, a Markdown answer, cites: every `Kind#seq` of the kinds the app
 * knows and every UUID, in the order first named, once each, at most 50.
 *
 * It reads what the page shows as a ref, which is what the person sees as cited
 * (`remarkRefs`): a ref glued to a word or another `#` is not one, and neither
 * is one in code. Code, which quotes a ref as syntax or as an example, is a fenced
 * block (an unclosed one runs to the end, as Markdown renders it) or an inline
 * span. A link's text cites its ref, since the answer names the row; its target and
 * a bare address do not, since a `#Issue#8` in an address is a fragment.
 */
export function namedRows(text: string, kinds: readonly string[]): readonly NamedRow[] {
  const names = kinds.map((kind) => kind.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
  // With no kinds the pattern would be `()#(\d+)` and name every bare `#4`.
  const refs = names ? `(?<![\\w#])(${names})#(\\d+)\\b|` : "";
  const pattern = new RegExp(`${refs}\\b(${UUID})\\b`, "g");
  const rows = new Map<string, NamedRow>();
  for (const found of uncited(text).matchAll(pattern)) {
    if (rows.size === MOST_NAMED) break;
    const row = rowOf(found, names !== "");
    if (row) rows.set("id" in row ? row.id : `${row.kind}#${row.seq}`, row);
  }
  return [...rows.values()];
}

function rowOf(found: RegExpMatchArray, hasKinds: boolean): NamedRow | null {
  if (!hasKinds) return { id: found[1]!.toLowerCase() };
  if (found[3] !== undefined) return { id: found[3].toLowerCase() };
  const seq = parseSeq(found[2]!);
  return seq === null ? null : { kind: found[1]!, seq };
}

/** `text` less the code and the addresses that cite nothing, each replaced by a space. */
function uncited(text: string): string {
  return text
    .replace(FENCED, "\n")
    .replace(INLINE_CODE, " ")
    .replace(LINK_TARGET, "]")
    .replace(AUTOLINK, " ")
    .replace(ADDRESS, " ");
}

/** A fenced block: to its closing fence, or to the end of the text when it has none. */
const FENCED = /^ {0,3}(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:^ {0,3}\1[`~]*[ \t]*$|(?![\s\S]))/gm;
/** A code span: a run of backticks to the next run of as many. */
const INLINE_CODE = /(?<!`)(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)/g;
/** The `(target "title")` after a link's text. */
const LINK_TARGET = /\]\([^)\s]*(?:\s+(?:"[^"]*"|'[^']*'))?\s*\)/g;
const AUTOLINK = /<[a-z][a-z0-9+.-]*:[^\s<>]*>/gi;
const ADDRESS = /\bhttps?:\/\/[^\s<>)\]]+/gi;
