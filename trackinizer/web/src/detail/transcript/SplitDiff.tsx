// One file's diff, side by side as GitHub's split view draws it, or unified.
import type { ElementContent } from "hast";
import { type ReactNode, useMemo, useState } from "react";
import { useHighlighter } from "../../markdown/CodeBlock";
import { Diff } from "./Code";
import { diffParts, type PatchFile, type Side, sideTexts, splitRows } from "./splitDiff";
import { languageOf } from "./steps";
import "./transcript.css";
import "./splitDiff.css";

/** How a diff is drawn: its two sides beside each other, or one column of marked lines. */
export type DiffMode = "split" | "unified";

/** Where a viewer's choice of mode is kept, for every diff they open. */
const MODE_KEY = "trax.diff-mode";

/** The viewer's diff mode, split until they choose; kept in this browser when it lets us. */
export function useDiffMode(): readonly [DiffMode, (mode: DiffMode) => void] {
  const [mode, setMode] = useState<DiffMode>(() => {
    try {
      return window.localStorage.getItem(MODE_KEY) === "unified" ? "unified" : "split";
    } catch {
      return "split";
    }
  });
  const choose = (next: DiffMode) => {
    setMode(next);
    try {
      window.localStorage.setItem(MODE_KEY, next);
    } catch {
      // A browser that keeps nothing still switches this view.
    }
  };
  return [mode, choose];
}

/** The two buttons that switch a diff between split and unified. */
export function DiffModeToggle({ mode, onChange }: { readonly mode: DiffMode; readonly onChange: (mode: DiffMode) => void }) {
  return (
    <span className="sd-toggle" role="group" aria-label="Diff layout">
      {(["split", "unified"] as const).map((each) => (
        <button key={each} type="button" aria-pressed={mode === each} onClick={() => onChange(each)}>
          {each === "split" ? "Split" : "Unified"}
        </button>
      ))}
    </span>
  );
}

/** The most rows one file draws; the rest are counted. */
export const DIFF_ROWS = 400;

/**
 * One file's diff: its hunks, at most `maxRows` rows of them, each side numbered and
 * highlighted for the file's language. Its header lines are its caller's to show.
 */
export function FileDiff({
  file,
  mode,
  language = languageOf(file.path),
  maxRows = DIFF_ROWS,
}: {
  readonly file: PatchFile;
  readonly mode: DiffMode;
  readonly language?: string;
  readonly maxRows?: number;
}) {
  if (file.body.length === 0) return null;
  return mode === "split" ? <Split body={file.body} language={language} maxRows={maxRows} /> : <Unified body={file.body} language={language} maxRows={maxRows} />;
}

function Unified({ body, language, maxRows }: { readonly body: readonly string[]; readonly language: string; readonly maxRows: number }) {
  const { lines } = useMemo(() => diffParts(body.join("\n")), [body]);
  const shown = useMemo(() => lines.slice(0, maxRows), [lines, maxRows]);
  return (
    <>
      <Diff lines={shown} language={language} />
      {lines.length > shown.length ? <p className="sd-more">{`… ${lines.length - shown.length} more lines`}</p> : null}
    </>
  );
}

function Split({ body, language, maxRows }: { readonly body: readonly string[]; readonly language: string; readonly maxRows: number }) {
  const highlighter = useHighlighter(Boolean(language));
  const rows = useMemo(() => splitRows(body), [body]);
  const sides = useMemo(() => {
    if (!highlighter) return null;
    const texts = sideTexts(body);
    return { old: highlighter.highlightLines(texts.old, language), new: highlighter.highlightLines(texts.new, language) };
  }, [highlighter, body, language]);
  const shown = rows.slice(0, maxRows);
  return (
    <>
      <table className="sd-split" aria-label="Side-by-side diff">
        <colgroup>
          <col className="sd-num" />
          <col />
          <col className="sd-num" />
          <col />
        </colgroup>
        <tbody>
          {shown.map((row, index) =>
            row.kind === "hunk" ? (
              // Rows repeat, so only their place names them.
              <tr key={index} className="sd-hunk">
                <td colSpan={4}>{row.text}</td>
              </tr>
            ) : (
              <tr key={index}>
                <Cell side={row.left} tokens={row.left ? sides?.old?.[row.left.at] : undefined} />
                <Cell side={row.right} tokens={row.right ? sides?.new?.[row.right.at] : undefined} />
              </tr>
            ),
          )}
        </tbody>
      </table>
      {rows.length > shown.length ? <p className="sd-more">{`… ${rows.length - shown.length} more rows`}</p> : null}
    </>
  );
}

/** One side of a row: its number and its line, or an empty half where the other side has a line alone. */
function Cell({ side, tokens }: { readonly side: Side | null; readonly tokens: readonly ElementContent[] | undefined }) {
  if (!side) {
    return (
      <>
        <td className="sd-n sd-empty" />
        <td className="sd-code sd-empty" />
      </>
    );
  }
  return (
    <>
      <td className={`sd-n sd-${side.kind}`}>{side.n}</td>
      <td className={`sd-code sd-${side.kind}`}>
        <span className="sd-mark">{side.kind === "del" ? "-" : side.kind === "add" ? "+" : " "}</span>
        {tokens ? <Tokens nodes={tokens} /> : side.text}
      </td>
    </>
  );
}

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
