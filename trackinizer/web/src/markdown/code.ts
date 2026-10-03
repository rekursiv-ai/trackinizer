import type { Element, ElementContent, Root } from "hast";
import { type JsonNode, parseJson } from "./json";

declare module "hast" {
  interface ElementData {
    /** On a code block's `pre`: its JSON, which shows as a JSON view (`rehypeJsonBlocks`). */
    json?: JsonNode;
  }
}

/** A code block: its `pre`, the `code` in it, the language its fence names, and its text. */
export type CodeBlockNode = { readonly pre: Element; readonly code: Element; readonly language: string | undefined; readonly text: string };

/** Whether `source` has a fenced code block, which is when the highlighter loads. */
export function hasFence(source: string): boolean {
  return /^ {0,3}(?:`{3,}|~{3,})/m.test(source);
}

/** Every code block under `tree`, outermost first. */
export function* codeBlocks(tree: Root | Element): Generator<CodeBlockNode> {
  for (const child of tree.children) {
    if (child.type !== "element") continue;
    const code = child.tagName === "pre" ? child.children[0] : undefined;
    if (code?.type === "element" && code.tagName === "code") {
      const classes = Array.isArray(code.properties.className) ? code.properties.className.map(String) : [];
      const language = classes.find((name) => name.startsWith("language-"))?.slice("language-".length).toLowerCase();
      yield { pre: child, code, language, text: textOf(code) };
    } else {
      yield* codeBlocks(child);
    }
  }
}

/**
 * A rehype plugin that marks each code block that is JSON with its tree: one
 * tagged `json` or `jsonc`, or untagged, whose whole text parses (`parseJson`).
 */
export function rehypeJsonBlocks() {
  return (tree: Root) => {
    for (const { pre, language, text } of codeBlocks(tree)) {
      const json = language === undefined || language === "json" || language === "jsonc" ? parseJson(text) : undefined;
      if (json) pre.data = { ...pre.data, json };
    }
  };
}

/** The text in `node`: a code block's, with the newline Markdown ends it with. */
export function textOf(node: ElementContent | Element): string {
  if (node.type === "text") return node.value;
  return node.type === "element" ? node.children.map(textOf).join("") : "";
}
