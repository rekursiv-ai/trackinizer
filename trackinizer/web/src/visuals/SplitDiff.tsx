// A diff side by side, as GitHub's split view draws it, or unified: one file, or a patch of many.
import { type QueryKey, useQuery } from "@tanstack/react-query";
import type { ElementContent } from "hast";
import { Fragment, type ReactNode, useMemo, useState } from "react";
import { Diff } from "../detail/transcript/Code";
import { languageOf } from "../detail/transcript/steps";
import { useHighlighter } from "../markdown/CodeBlock";
import { ReadFailure } from "../ui/failure";
import { diffParts } from "./filesDoc";
import { fileLines, hunksOf, type OpenGap, type PatchFile, patchFiles, type Shown, type Side, sideTexts, splitRows, withShown } from "./splitDiff";
import "../detail/transcript/transcript.css";
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
 * Where a file's whole text comes from, so its diff can show the unchanged lines it
 * left out. Nothing reads it until the reader asks for lines, and then once.
 */
export type FileSource = {
  /** The read's key in the query cache. */
  readonly key: QueryKey;
  /** The file as the diff's new side holds it; outside its hunks the old side holds the same lines. */
  readonly read: (signal: AbortSignal) => Promise<string>;
};

/** The unchanged lines one press shows; a gap between hunks that hides no more shows whole. */
export const EXPAND_LINES = 20;

/**
 * One file's diff: its hunks, at most `maxRows` rows of them, each side numbered and
 * highlighted for the file's language. Its header lines are its caller's to show.
 * With a `source`, a row above each hunk and below the last shows the unchanged
 * lines between them, as GitHub's Files changed does.
 */
export function FileDiff({
  file,
  mode,
  language = languageOf(file.path),
  maxRows = DIFF_ROWS,
  source,
}: {
  readonly file: PatchFile;
  readonly mode: DiffMode;
  readonly language?: string;
  readonly maxRows?: number;
  readonly source?: FileSource;
}) {
  if (file.body.length === 0) return null;
  if (source) return <Expandable body={file.body} mode={mode} language={language} maxRows={maxRows} source={source} />;
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

/** The rows a diff draws between its hunks: a gap's expander above each hunk, by its place, and one below the last. */
type GapRows = { readonly above: readonly ReactNode[]; readonly below: ReactNode };

function Split({ body, language, maxRows, gaps }: { readonly body: readonly string[]; readonly language: string; readonly maxRows: number; readonly gaps?: GapRows }) {
  const highlighter = useHighlighter(Boolean(language));
  const rows = useMemo(() => splitRows(body), [body]);
  const sides = useMemo(() => {
    if (!highlighter) return null;
    const texts = sideTexts(body);
    return { old: highlighter.highlightLines(texts.old, language), new: highlighter.highlightLines(texts.new, language) };
  }, [highlighter, body, language]);
  const shown = rows.slice(0, maxRows);
  // A `\ No newline` row is a hunk row too; only a header starts a hunk.
  let hunk = 0;
  const hunkOf = shown.map((row) => (row.kind === "hunk" && row.text.startsWith("@@") ? hunk++ : -1));
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
              <Fragment key={index}>
                {gaps?.above[hunkOf[index]!] ? <GapRow>{gaps.above[hunkOf[index]!]}</GapRow> : null}
                <tr className="sd-hunk">
                  <td colSpan={4}>{row.text}</td>
                </tr>
              </Fragment>
            ) : (
              <tr key={index}>
                <Cell side={row.left} tokens={row.left ? sides?.old?.[row.left.at] : undefined} />
                <Cell side={row.right} tokens={row.right ? sides?.new?.[row.right.at] : undefined} />
              </tr>
            ),
          )}
          {gaps?.below && rows.length === shown.length ? <GapRow>{gaps.below}</GapRow> : null}
        </tbody>
      </table>
      {rows.length > shown.length ? <p className="sd-more">{`… ${rows.length - shown.length} more rows`}</p> : null}
    </>
  );
}

function GapRow({ children }: { readonly children: ReactNode }) {
  return (
    <tr className="sd-gap">
      <td colSpan={4}>{children}</td>
    </tr>
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

/**
 * A diff whose gaps a reader can show, `EXPAND_LINES` at a time from the hunk beside
 * them, or whole when that is all a gap between two hunks hides. The first press
 * reads the file; a press before it is read shows its lines once it is. A diff whose
 * hunks do not hold what their headers count (one cut short) draws as it is.
 */
function Expandable({
  body,
  mode,
  language,
  maxRows,
  source,
}: {
  readonly body: readonly string[];
  readonly mode: DiffMode;
  readonly language: string;
  readonly maxRows: number;
  readonly source: FileSource;
}) {
  const hunks = useMemo(() => hunksOf(body), [body]);
  const [asked, setAsked] = useState<{ readonly body: readonly string[]; readonly shown: readonly Shown[] } | null>(null);
  const read = useQuery({ queryKey: source.key, queryFn: ({ signal }) => source.read(signal), enabled: asked?.body === body, staleTime: Infinity });
  const lines = useMemo(() => (read.data === undefined ? null : fileLines(read.data)), [read.data]);
  // What was shown of another diff's gaps says nothing of this one's.
  const shown = asked?.body === body ? asked.shown : NONE_SHOWN;
  const drawn = useMemo(() => hunks && withShown(hunks, shown, lines), [hunks, shown, lines]);
  const drawnLines = useMemo(() => drawn?.hunks.map((hunk) => hunk.lines) ?? [], [drawn]);
  const drawnBody = useMemo(() => drawnLines.flat(), [drawnLines]);
  if (!hunks || !drawn) return mode === "split" ? <Split body={body} language={language} maxRows={maxRows} /> : <Unified body={body} language={language} maxRows={maxRows} />;
  const busy = asked?.body === body && lines === null;
  const show = (gap: OpenGap, how: Expand) => {
    const next = Array.from({ length: hunks.length + 1 }, (_, k) => shown[k] ?? NOTHING_SHOWN);
    const { down, up } = next[gap.index]!;
    next[gap.index] = how === "down" ? { down: down + EXPAND_LINES, up } : how === "up" ? { down, up: up + EXPAND_LINES } : { down: down + (gap.hidden ?? 0), up };
    setAsked({ body, shown: next });
  };
  const controls = (gap: OpenGap | null) => (gap ? <GapControls gap={gap} busy={busy} reading={busy && read.isFetching} onShow={show} /> : null);
  const gaps = { above: drawn.hunks.map(({ above }) => controls(above)), below: controls(drawn.below) };
  return (
    <>
      {read.error ? <ReadFailure error={read.error} retry={() => void read.refetch()} /> : null}
      {mode === "split" ? (
        <Split body={drawnBody} language={language} maxRows={maxRows} gaps={gaps} />
      ) : (
        <UnifiedHunks hunks={drawnLines} language={language} maxRows={maxRows} gaps={gaps} />
      )}
    </>
  );
}

const NONE_SHOWN: readonly Shown[] = [];
const NOTHING_SHOWN: Shown = { down: 0, up: 0 };

/** How a press shows a gap's lines: under the hunk above, over the hunk below, or all of them. */
type Expand = "down" | "up" | "all";

const EXPANDS: Readonly<Record<Expand, { readonly label: string; readonly icon: string }>> = {
  down: { label: "Expand down", icon: "↓" },
  up: { label: "Expand up", icon: "↑" },
  all: { label: "Expand all", icon: "↕" },
};

/**
 * What a gap's row offers: over the first hunk, its lines upward; under the last,
 * downward; between two, both, or all at once when one press would show them all.
 */
function GapControls({
  gap,
  busy,
  reading,
  onShow,
}: {
  readonly gap: OpenGap;
  readonly busy: boolean;
  readonly reading: boolean;
  readonly onShow: (gap: OpenGap, how: Expand) => void;
}) {
  const few = gap.hidden !== null && gap.hidden <= EXPAND_LINES;
  const hows: readonly Expand[] = gap.place === "top" ? ["up"] : gap.place === "bottom" ? ["down"] : few ? ["all"] : ["down", "up"];
  return (
    <span className="sd-gap-row">
      {hows.map((how) => (
        <button key={how} type="button" className="sd-expand" disabled={busy} onClick={() => onShow(gap, how)}>
          <span aria-hidden="true">{EXPANDS[how].icon}</span>
          {EXPANDS[how].label}
        </button>
      ))}
      <span className="sd-hidden">{reading ? "Reading the file…" : gap.hidden === null ? "" : `${gap.hidden} unchanged ${gap.hidden === 1 ? "line" : "lines"}`}</span>
    </span>
  );
}

/**
 * Unified, a hunk at a time, each its own column of numbered lines, so a gap's row
 * stands between two; at most `maxRows` lines in all.
 */
function UnifiedHunks({ hunks, language, maxRows, gaps }: { readonly hunks: readonly (readonly string[])[]; readonly language: string; readonly maxRows: number; readonly gaps: GapRows }) {
  const parts = useMemo(() => hunks.map((lines) => diffParts(lines.join("\n")).lines), [hunks]);
  const total = parts.reduce((sum, lines) => sum + lines.length, 0);
  let left = maxRows;
  return (
    <>
      {parts.map((lines, k) => {
        const shown = lines.slice(0, Math.max(0, left));
        left -= lines.length;
        return shown.length > 0 ? (
          // Hunks repeat, so only their place names them.
          <Fragment key={k}>
            {gaps.above[k] ? <div className="sd-gap">{gaps.above[k]}</div> : null}
            <Diff lines={shown} language={language} />
          </Fragment>
        ) : null;
      })}
      {gaps.below && left >= 0 ? <div className="sd-gap">{gaps.below}</div> : null}
      {left < 0 ? <p className="sd-more">{`… ${total - maxRows} more lines`}</p> : null}
    </>
  );
}

/** Header lines that only name the file; the title shows its path instead. */
const NAMING = /^(diff |--- |\+\+\+ |index )/;

/**
 * A patch of one or more files, each under its path: a header line that says more
 * than the path (`Binary files … differ`, `… too large to diff`, a note) beneath it,
 * then its diff. An empty patch says the two sides hold the same code.
 */
export function PatchView({ patch, mode, label = "Code diff" }: { readonly patch: string; readonly mode: DiffMode; readonly label?: string }) {
  const files = useMemo(() => patchFiles(patch), [patch]);
  if (files.length === 0) return <p className="sd-more">No file differs.</p>;
  return (
    <div className="sd-patch" role="group" aria-label={label}>
      {files.map((file, index) => {
        const notes = file.head.filter((line) => !NAMING.test(line));
        return (
          // Two notes can name no path, so the place names each file.
          <section key={index} className="sd-file" aria-label={file.path || "Note"}>
            {file.path ? <h4 className="sd-path">{file.path}</h4> : null}
            {notes.length > 0 ? <pre className="sd-note">{notes.join("\n")}</pre> : null}
            <FileDiff file={file} mode={mode} />
          </section>
        );
      })}
    </div>
  );
}
