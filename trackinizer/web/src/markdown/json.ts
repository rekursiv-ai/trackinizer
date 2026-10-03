/** A JSON value as its text wrote it: keys in order, numbers as written. */
export type JsonNode =
  | { readonly type: "object"; readonly entries: readonly (readonly [string, JsonNode])[] }
  | { readonly type: "array"; readonly items: readonly JsonNode[] }
  | { readonly type: "string"; readonly value: string }
  | { readonly type: "number"; readonly text: string }
  | { readonly type: "literal"; readonly text: "true" | "false" | "null" };

/**
 * `text` as JSON when the whole of it, trimmed, is a JSON object or array; else
 * undefined. Text that does not start with `{` or `[` is never parsed.
 *
 * `JSON.parse` decides what is JSON; the tree is built from the text's own
 * tokens, since a parsed value would round an integer past 2^53 to another
 * number, write `300.0` as `300`, put integer-like keys first and keep only the
 * last of a repeated key. It is built without recursion, so no nesting the
 * parser accepts overflows the stack.
 */
export function parseJson(text: string): JsonNode | undefined {
  const trimmed = text.trim();
  if (trimmed[0] !== "{" && trimmed[0] !== "[") return undefined;
  try {
    JSON.parse(trimmed);
  } catch {
    return undefined;
  }
  return tree(trimmed);
}

/** A string, a punctuation mark, or a number or literal; valid JSON splits into these alone. */
const TOKEN = /"(?:[^"\\]|\\.)*"|[{}[\]:,]|[^\s{}[\]:,"]+/g;

/** The tree of `text`, which `JSON.parse` has accepted. */
function tree(text: string): JsonNode {
  // The containers still open, innermost last, each with the key its next value takes.
  const open: { node: Container; key: string | undefined }[] = [];
  let root: JsonNode | undefined;
  const place = (node: JsonNode) => {
    const parent = open.at(-1);
    if (!parent) root = node;
    else if (parent.node.type === "array") parent.node.items.push(node);
    else {
      parent.node.entries.push([parent.key!, node]);
      parent.key = undefined;
    }
  };
  for (const [token] of text.matchAll(TOKEN)) {
    if (token === "{" || token === "[") {
      const node: Container = token === "{" ? { type: "object", entries: [] } : { type: "array", items: [] };
      place(node);
      open.push({ node, key: undefined });
    } else if (token === "}" || token === "]") {
      open.pop();
    } else if (token[0] === '"') {
      const value = JSON.parse(token) as string;
      const parent = open.at(-1)!;
      if (parent.node.type === "object" && parent.key === undefined) parent.key = value;
      else place({ type: "string", value });
    } else if (token === "true" || token === "false" || token === "null") {
      place({ type: "literal", text: token });
    } else if (token !== ":" && token !== ",") {
      place({ type: "number", text: token });
    }
  }
  return root!;
}

type Container = { type: "object"; entries: [string, JsonNode][] } | { type: "array"; items: JsonNode[] };
