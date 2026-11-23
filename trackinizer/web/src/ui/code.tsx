// THE CODE NODE. A file on stage, highlighted, with annotations pinned to their line.
//
// The law's own composition rule for this family: "lines, with each annotation pinned
// to its line number — an annotation that floats is an annotation on the wrong line."
// This is the node that rule is about, and there was none in the catalog: a composed
// surface had no way to show a file at all, only prose about one.
//
// It is not a new highlighter. `markdown/highlight.ts` already colours text through
// lowlight for the transcript and the diff view, proven in both; this reuses that
// chunk rather than loading a second copy of highlight.js.
//
// Reading rule, stated once here so every answer obeys it: the node shows a
// SUMMARY of the file and a CAPPED excerpt (never a whole-file dump), and the
// full file opens on click in a modal. One node per file; a multi-file answer
// shows one summary card per file, not one long scroll of code.
import type { ElementContent } from "hast";
import { type ReactNode, useMemo, useState } from "react";
import { useHighlighter } from "../markdown/CodeBlock";
import { Modal } from "./frame";
import "../markdown/highlight.css";
import "./code.css";

/** One gutter chip, pinned to the line it annotates. */
export type CodeNote = {
  /** The 1-based line this chip annotates; floating notes are forbidden. */
  readonly line: number;
  readonly text: string;
};

/**
 * A file on stage. GIVE IT THE WHOLE FILE: the card cuts an 18-line window around the
 * first `highlight`, and its header opens the file entire, scrolling, those lines still
 * lit. Cut `text` down yourself and "full code" reopens what was already drawn. Two
 * regions far apart are two nodes, each with an honest `from`.
 */
export type Code = {
  /** The path, as its own header line: `nanochat/block_sparse.py`. */
  readonly path: string;
  /** The file's entire text. Not an excerpt — the node excerpts, you do not. */
  readonly text: string;
  /** Empty autodetects from the extension. */
  readonly language?: string;
  /** The file line that `text` starts at: 1 for a whole file, which is the normal case. */
  readonly from?: number;
  /** The lines to land on first, in file numbering. The card's window centres on the first. */
  readonly highlight?: readonly number[];
  readonly notes?: readonly CodeNote[];
};

/** One code excerpt, highlighted, numbered, with its gutter. */
export function CodeView({ path, text, language = "", from = 1, highlight, notes }: Code) {
  const highlighter = useHighlighter(true);
  const [open, setOpen] = useState(false);
  const coloured = useMemo(() => highlighter?.highlightLines(text, language), [highlighter, text, language]);
  const rows = text.replace(/\n$/, "").split("\n");
  const lit = new Set(highlight ?? []);
  const byLine = new Map((notes ?? []).map((note) => [note.line, note]));

  // Summary derived from the excerpt itself, never hand-written per answer:
  // the enclosing def/class of the first highlighted line, the line count,
  // and the file's own docstring when it carries one.
  const summary = useMemo(() => {
    const first = lit.size ? [...lit].sort((a, b) => a - b)[0] : null;
    const at = first !== null ? first - from : 0;
    const lineText = at >= 0 && at < rows.length ? rows[at].trim() : "";
    const fn = lineText.match(/^(?:def|class)\s+(\w+)/)?.[1];
    const doc = rows.find((r) => /""".*\S/.test(r))?.replace(/^.*"""|^"""\s*|\s*"""$/, "").trim() ?? "";
    return { fn, doc };
  }, [rows, lit, from]);

  const total = rows.length;
  const hint = `full code · ${total} lines · click to open`;

  return (
    <div className="ui-code">
      <button type="button" className="ui-code-head" onClick={() => setOpen(true)} aria-label={`open full code for ${path}`}>
        <span className="ui-code-path">{path}</span>
        <span className="ui-code-summary">
          {summary.fn ? <span className="ui-code-fn">{summary.fn}</span> : null}
          {summary.doc ? <span className="ui-code-doc">{summary.doc}</span> : null}
        </span>
        <span className="ui-code-open">{hint}</span>
      </button>
      {/* Capped excerpt: at most 18 lines either side of the first highlight,
          so a 400-line file never dumps on the surface. Still an excerpt. */}
      <pre className="ui-code-pre">
        {(() => {
          const first = lit.size ? Math.min(...lit) : from;
          const lo = Math.max(from, first - 6);
          const hi = Math.min(from + rows.length - 1, first + 11);
          return rows.slice(lo - from, hi - from + 1).map((row, k) => {
            const line = lo + k;
            const at = line - from;
            const note = byLine.get(line);
            return (
              <span className={`ui-code-row${lit.has(line) ? " is-lit" : ""}`} key={line}>
                <span className="ui-code-n">{line}</span>
                <code className="ui-code-tok">{coloured?.[at] ? <Tokens nodes={coloured[at]!} /> : row}</code>
                {/* The chip sits in the row it annotates, never after the block: a note
                    that floats below the excerpt is a note on no line in particular. */}
                {note ? <span className="ui-code-note">{note.text}</span> : null}
              </span>
            );
          });
        })()}
      </pre>
      {open ? (
        <Modal cls="knowledge-modal" title={path} note={`${total} lines · full code`} onClose={() => setOpen(false)}>
          <CodeViewInner path={path} text={text} language={language} from={from} highlight={highlight} notes={notes} />
        </Modal>
      ) : null}
    </div>
  );
}

/** The full file, inside the modal. Split from the card above so the card's
 *  cap never clips what the reader asked to see whole. */
function CodeViewInner({ path, text, language = "", from = 1, highlight, notes }: Code) {
  const highlighter = useHighlighter(true);
  const coloured = useMemo(() => highlighter?.highlightLines(text, language), [highlighter, text, language]);
  const rows = text.replace(/\n$/, "").split("\n");
  const lit = new Set(highlight ?? []);
  const byLine = new Map((notes ?? []).map((note) => [note.line, note]));
  return (
    <div className="ui-code is-full">
      <p className="ui-code-path">{path}</p>
      <pre className="ui-code-pre">
        {rows.map((row, at) => {
          const line = from + at;
          const note = byLine.get(line);
          return (
            <span className={`ui-code-row${lit.has(line) ? " is-lit" : ""}`} key={line}>
              <span className="ui-code-n">{line}</span>
              <code className="ui-code-tok">{coloured?.[at] ? <Tokens nodes={coloured[at]!} /> : row}</code>
              {note ? <span className="ui-code-note">{note.text}</span> : null}
            </span>
          );
        })}
      </pre>
    </div>
  );
}

/** A hast tree, as highlight.js coloured it, read into React. Proven in
 *  detail/transcript/Code.tsx; duplicated rather than imported because that module
 *  pulls in the transcript's own line-clipping and Copy affordances, which a composed
 *  excerpt does not carry. */
function Tokens({ nodes }: { readonly nodes: readonly ElementContent[] }): ReactNode {
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
