import { parseSeq, type Route } from "../router/route";

/** The text a search reads on an inquiry; `undefined` when the palette never loaded it. */
export type Searchable = {
  readonly title: string;
  readonly description?: string | null;
};

/**
 * Whether a row matches `q`, read with the server's grammar (`_parse_query` in
 * `server/web.py`), so the palette lists a row it already holds only when the
 * server would list it too.
 *
 * Terms split on space, tab, CR and LF, and must all match. `"` groups a phrase,
 * anywhere in a term, and adds nothing itself: `""` inside a phrase closes it and
 * opens another. `'` and `\` are ordinary characters. Each term is a
 * case-insensitive substring of the title or the description; a row whose
 * description the palette never loaded (a relation's far end) matches through
 * its title alone. Text the server refuses (an unclosed quote, an empty field
 * value) or with no terms matches nothing.
 *
 * So does any `title:RE` or `description:RE` term, quoted or not: the server
 * runs it as a POSIX regex under a time budget, and only it can say what it
 * matches. JavaScript's regexes read `\b`, `\m` and `[[:digit:]]` otherwise,
 * and a pattern such as `(a+)+$` would stall this tab with no budget to stop it.
 */
export function parseSearch(q: string): (row: Searchable) => boolean {
  const tokens = tokenize(q);
  if (tokens === null || tokens.length === 0 || tokens.some(isFieldTerm)) return () => false;
  const needles = tokens.map((token) => token.toLowerCase());
  return (row) =>
    needles.every(
      (needle) => row.title.toLowerCase().includes(needle) || (row.description ?? "").toLowerCase().includes(needle),
    );
}

/** A route to one inquiry: by kind and seq, or by id. */
export type InquiryRoute = Extract<Route, { name: "ref" } | { name: "lookup" }>;

/**
 * The inquiries `q` names outright, as routes: `Issue#412` (a kind's name or the
 * start of one, in any case) or a UUID. A name that is exactly a kind names only
 * that kind; a prefix names every kind it starts, in `kinds`' order.
 */
export function jumpsFor(q: string, kinds: readonly string[]): InquiryRoute[] {
  const text = q.trim();
  if (UUID.test(text)) return [{ name: "lookup", id: text.toLowerCase() }];
  const ref = /^([a-z]+)\s*#\s*(\d+)$/i.exec(text);
  if (!ref) return [];
  const [, name, digits] = ref;
  const seq = parseSeq(digits!);
  if (seq === null) return [];
  const typed = name!.toLowerCase();
  const exact = kinds.filter((kind) => kind.toLowerCase() === typed);
  const named = exact.length ? exact : kinds.filter((kind) => kind.toLowerCase().startsWith(typed));
  return named.map((kind) => ({ name: "ref", kind, seq }));
}

/**
 * Split `q` into terms as the server's `shlex` does with `"` as the only quote and
 * no escape character, or `null` for an unclosed quote. Terms split on space,
 * tab, CR and LF outside phrases; quotes are dropped. Empty terms (`""`) drop out.
 */
function tokenize(q: string): string[] | null {
  const tokens: string[] = [];
  let token = "";
  let quoted = false;
  for (const char of q) {
    if (char === '"') {
      quoted = !quoted;
    } else if (quoted || !" \t\r\n".includes(char)) {
      token += char;
    } else {
      if (token) tokens.push(token);
      token = "";
    }
  }
  if (quoted) return null;
  if (token) tokens.push(token);
  return tokens;
}

/** A `title:` or `description:` term, read with its quotes dropped, as the server reads it. */
function isFieldTerm(token: string): boolean {
  const colon = token.indexOf(":");
  const field = colon > 0 ? token.slice(0, colon) : "";
  return field === "title" || field === "description";
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
