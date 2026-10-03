import type { ElementContent, Root } from "hast";
import { common, createLowlight } from "lowlight";
import { codeBlocks } from "./code";
import "./highlight.css";

/** highlight.js with its `common` languages, aliases included (`sh`, `py`, `ts`, `yml`). */
const lowlight = createLowlight(common);

/**
 * The languages an untagged block can be found to be, ties going to the first.
 *
 * SQL is left out: its keywords are English words, so one sentence of prose
 * scored 9 as SQL, over a real query's 7, and a Python traceback scored 9 as SQL
 * against 5 as Python. With all of `common`, plain Python came out as CSS.
 */
const DETECTED = ["python", "python-repl", "shell", "bash", "typescript", "javascript", "yaml", "diff"];

/**
 * A rehype plugin that colours each code block but JSON (`rehypeJsonBlocks`)
 * with highlight.js, through lowlight, which gives a tree rather than an HTML
 * string. A tagged block is coloured as its tag says, or not at all when
 * highlight.js has no such language; an untagged one as `detect` finds it.
 */
export function rehypeHighlightCode() {
  return (tree: Root) => {
    for (const { pre, code, language, text } of codeBlocks(tree)) {
      if (pre.data?.json) continue;
      const result = language === undefined ? detect(text) : lowlight.registered(language) ? lowlight.highlight(language, text) : undefined;
      if (!result) continue;
      code.children = result.children.filter((child): child is ElementContent => child.type !== "doctype");
      if (language === undefined) code.properties.className = [`language-${result.data?.language}`];
    }
  };
}

/**
 * `text` coloured as `language`, one list of nodes a line, each line's nodes
 * nested as highlight.js nests them, so a string or comment over several lines
 * keeps its colour on each: as `detect` finds it when `language` is `""`.
 * Undefined when highlight.js has no such language, nothing was found, or the
 * text is over `HIGHLIGHT_LIMIT` characters, past which highlighting would hold
 * the page (Codex's TUI likewise skips a diff past 10,000 lines).
 */
export function highlightLines(text: string, language: string): ElementContent[][] | undefined {
  if (text.length > HIGHLIGHT_LIMIT) return undefined;
  const result = language ? (lowlight.registered(language) ? lowlight.highlight(language, text) : undefined) : detect(text);
  return result && splitLines(result.children.filter((child): child is ElementContent => child.type !== "doctype"));
}

/**
 * The most characters `highlightLines` colours. 200,000 characters of Python
 * (5,400 lines) took 5.4 ms in Node on an M-series Mac, so about 20 ms with the
 * CPU slowed 4x; past it, the lines it would draw cost more than the colours.
 */
const HIGHLIGHT_LIMIT = 200_000;

/** `nodes` cut at each newline into lines, every element that spans a cut repeated on each side of it. */
function splitLines(nodes: readonly ElementContent[]): ElementContent[][] {
  const lines: ElementContent[][] = [[]];
  for (const node of nodes) {
    const parts: ElementContent[][] =
      node.type === "text"
        ? node.value.split("\n").map((value) => (value ? [{ type: "text", value }] : []))
        : node.type === "element"
          ? splitLines(node.children).map((children) => (children.length ? [{ ...node, children }] : []))
          : [[node]];
    parts.forEach((part, k) => {
      if (k) lines.push([]);
      lines.at(-1)!.push(...part);
    });
  }
  return lines;
}

/**
 * The colouring of untagged `text`: as Python when it is a traceback, which
 * highlight.js has no language for and scores low (its file paths show as
 * strings, its line numbers as numbers); else the best of `DETECTED`, when
 * highlight.js's relevance is at least 3 and one for every five words.
 *
 * Relevance alone grows with length, prose's too: at 5, 37% of 300 prose
 * paragraphs from trax descriptions coloured. One per five words coloured 8%
 * of them, and 57%, 83% and 84% of 3-, 8- and 20-line samples of the repo's
 * Python, TypeScript, shell and YAML; pseudo-code with math stayed plain.
 */
function detect(text: string): Root | undefined {
  if (/^\s*Traceback \(most recent call last\):/.test(text)) return lowlight.highlight("python", text);
  const result = lowlight.highlightAuto(text, { subset: DETECTED });
  const relevance = result.data?.relevance ?? 0;
  return relevance >= 3 && relevance * 5 >= text.trim().split(/\s+/).length ? result : undefined;
}
