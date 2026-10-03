import type { ElementContent } from "hast";
import { type ReactNode, useMemo, useState } from "react";
import { CodeFrame, useHighlighter } from "../../markdown/CodeBlock";
import { parseJson } from "../../markdown/json";
import { JsonView } from "../../markdown/JsonView";
import { type AnsiRun, ansiReader, ansiRuns, clipLines, type DiffLine } from "./records";

/**
 * A tool's output: a JSON object or array as a JSON view; anything else with its
 * ANSI colours, its first 20 and last 40 lines, where a failure shows, and the
 * rest in place on request.
 */
export function Output({ text, failed, kinds }: { text: string; failed: boolean; kinds: readonly string[] }) {
  const [whole, setWhole] = useState(false);
  const json = useMemo(() => parseJson(text), [text]);
  if (json) {
    return (
      <CodeFrame text={text.trim()} what="JSON">
        <JsonView root={json} kinds={kinds} />
      </CodeFrame>
    );
  }
  const clip = whole ? { head: text, hidden: 0, tail: "" } : clipLines(text);
  // One reader over the whole output, the lines left out included, so the tail
  // keeps the colours they left on.
  const read = ansiReader();
  const head = read(clip.head);
  read(text.slice(clip.head.length, text.replace(/\n$/, "").length - clip.tail.length));
  const tail = read(clip.tail);
  return (
    <pre className={failed ? "tool-out failed" : "tool-out"}>
      <Ansi runs={head} />
      {clip.hidden ? (
        <>
          {"\n"}
          <button type="button" className="tr-show" onClick={() => setWhole(true)}>
            Show {clip.hidden.toLocaleString("en")} more lines
          </button>
          {"\n"}
          <Ansi runs={tail} />
        </>
      ) : null}
    </pre>
  );
}

/**
 * Code in its language, a line a row, each numbered when `numbers` gives them,
 * with Copy: its first 20 and last 40 lines until asked, as `Output`. It colours
 * once the highlighter has loaded, and only while drawn, so a closed step costs
 * nothing; `language` `""` lets highlight.js find it.
 */
export function Code({ text, language, numbers = null }: { text: string; language: string; numbers?: readonly number[] | null }) {
  const [whole, setWhole] = useState(false);
  const highlighter = useHighlighter(true);
  const coloured = useMemo(() => highlighter?.highlightLines(text, language), [highlighter, text, language]);
  const rows = text.replace(/\n$/, "").split("\n");
  const hidden = rows.length - 60;
  const shown = whole || hidden <= 0 ? rows.keys() : [...range(0, 20), -1, ...range(rows.length - 40, rows.length)];
  return (
    <CodeFrame text={text} what="code">
      <pre className="tr-code">
        {[...shown].map((k) =>
          k < 0 ? (
            <span key="gap" className="ln">
              <button type="button" className="tr-show" onClick={() => setWhole(true)}>
                Show {hidden.toLocaleString("en")} more lines
              </button>
            </span>
          ) : (
            <span key={k} className="ln" data-n={numbers?.[k]}>
              {coloured?.[k] ? <Tokens nodes={coloured[k]} /> : rows[k]}
            </span>
          ),
        )}
      </pre>
    </CodeFrame>
  );
}

/**
 * A unified diff, a line a row: each numbered when the provider said where it
 * is, its `+` or `-` on a tinted row, and its code in `language` once the
 * highlighter has loaded. The old file's lines (context and removed) and the new
 * one's (context and added) are each coloured as one text, so a string over
 * several lines keeps its colour, and one side's never runs into the other's.
 */
export function Diff({ lines, language }: { lines: readonly DiffLine[]; language: string }) {
  const highlighter = useHighlighter(Boolean(language));
  const sides = useMemo(() => {
    const side = (kind: "del" | "add") =>
      lines
        .filter((line) => line.kind === "ctx" || line.kind === kind)
        .map((line) => line.text.slice(1))
        .join("\n");
    return highlighter && { del: highlighter.highlightLines(side("del"), language), add: highlighter.highlightLines(side("add"), language) };
  }, [highlighter, lines, language]);
  let [old, next] = [0, 0];
  return (
    <pre className="tr-diff">
      {lines.map((line, k) => {
        // A context line is on both sides; it takes the new side's colours.
        if (line.kind === "ctx") old++;
        const tokens = line.kind === "hunk" ? undefined : line.kind === "del" ? sides?.del?.[old++] : sides?.add?.[next++];
        return (
          <span key={k} className={`ln d-${line.kind}`} data-n={line.line ?? undefined}>
            {tokens ? (
              <>
                <span className="d-mark">{line.text[0]}</span>
                <Tokens nodes={tokens} />
              </>
            ) : line.kind === "hunk" ? (
              line.text
            ) : (
              <>
                <span className="d-mark">{line.text[0]}</span>
                {line.text.slice(1)}
              </>
            )}
          </span>
        );
      })}
    </pre>
  );
}

/**
 * `text` a row a line, each with its ANSI colours and styles (`ansiRuns`), a
 * colour opened on one line holding on the next.
 */
export function AnsiLines({ text }: { text: string }) {
  const lines: { text: string; style: string }[][] = [[]];
  for (const run of ansiRuns(text)) {
    run.text.split("\n").forEach((part, k) => {
      if (k) lines.push([]);
      if (part) lines.at(-1)!.push({ text: part, style: run.style });
    });
  }
  return lines.map((runs, k) => (
    <span key={k} className="ln">
      <Ansi runs={runs} />
    </span>
  ));
}

/** Text in its ANSI colours and styles, as runs (`ansiRuns`), never as markup it holds. */
export function Ansi({ runs }: { runs: readonly AnsiRun[] }) {
  return runs.map((run, k) =>
    run.style ? (
      <span key={k} className={run.style}>
        {run.text}
      </span>
    ) : (
      run.text
    ),
  );
}

/** highlight.js's tokens as spans of its classes; only text and class names reach the page. */
function Tokens({ nodes }: { nodes: readonly ElementContent[] }): ReactNode {
  return nodes.map((node, k) =>
    node.type === "text" ? (
      node.value
    ) : node.type === "element" ? (
      <span key={k} className={Array.isArray(node.properties.className) ? node.properties.className.join(" ") : undefined}>
        <Tokens nodes={node.children} />
      </span>
    ) : null,
  );
}

/** The integers from `start` up to, not including, `end`. */
function range(start: number, end: number): number[] {
  return Array.from({ length: end - start }, (_, k) => start + k);
}
