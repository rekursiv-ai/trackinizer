import type { Nodes, Root } from "mdast";
import { findAndReplace } from "mdast-util-find-and-replace";

/**
 * A remark plugin that marks absolute paths, which Markdown draws as code that
 * copies its path on a click (`CodeSpan`): a path in text, and an inline code
 * span that is one path, become inline code of class `md-path`.
 *
 * A path opens a word with `/` or `~/` and has two segments or more. A path
 * inside a URL or after a host (`host:/opt/x`) does not open a word, nor does
 * a ratio's (`5/10`, `$26.16/$150`) or `and/or`; the punctuation after one
 * (`/opt/x/y.`) is not part of it. A path in a link's text, or in a longer code
 * span or a code block, is left alone.
 */
export function remarkPaths() {
  return (tree: Root) => {
    findAndReplace(tree, [[IN_TEXT, (path: string) => ({ type: "inlineCode", value: path })]], { ignore: ["link", "linkReference"] });
    mark(tree);
  };
}

/** A path's shape: segments of path characters, maybe a final `/`, not ending in punctuation. */
const PATH = String.raw`~?(?:\/[\w.@+-]+){2,}\/?(?<![.,;:!?])`;

/** A path in text: after the start, a space, or an opening bracket or quote. */
const IN_TEXT = new RegExp(`(?<![^\\s([{"'“‘])${PATH}`, "g");

const WHOLE = new RegExp(`^${PATH}$`);

/** Each inline code span in `node` that is one path, marked `md-path`. */
function mark(node: Nodes): void {
  if (node.type === "inlineCode" && WHOLE.test(node.value)) node.data = { hProperties: { className: ["md-path"] } };
  else if ("children" in node) for (const child of node.children) mark(child);
}
