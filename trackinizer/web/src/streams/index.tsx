import type { InquiryRow } from "../api/inquiries";
import type { Selection } from "../bulk/selection";
import { useAncestry } from "../lists/ancestry";
import { Row, shortAge } from "../lists/Row";
import { formatRoute } from "../router/route";
import { ReadFailure } from "../ui/failure";
import { StatusGlyph } from "../ui/glyphs";
import { buildStreams, type Stream } from "./streams";
import "./streams.css";

/** An Issue list's rows as streams, with what the list's keys and selection need. */
export type StreamsView = {
  readonly streams: readonly Stream[];
  /** The streams shown whole, by `streamKey`. */
  readonly whole: ReadonlySet<string>;
  /** The rows shown, each once, in order: what j and k move through, and what can be selected. */
  readonly order: readonly InquiryRow[];
  readonly rows: readonly InquiryRow[];
  /** Nothing to show yet: the ancestry's first read is on its way. */
  readonly pending: boolean;
  readonly error: Error | null;
  readonly retry: () => void;
};

/**
 * The streams of `rows`, the rows a list shows, newest first, grouped by root
 * goal (LV3), their ancestry read and kept current while mounted; null, reading
 * nothing, when `rows` is. `toggled` is the list's stored keys, among them those
 * of the streams shown whole.
 */
export function useStreams(rows: readonly InquiryRow[] | null, toggled: readonly string[]): StreamsView | null {
  const { ancestry, pending, error, retry } = useAncestry(rows && rows.map((row) => row.id));
  if (!rows) return null;
  const streams = buildStreams(rows, ancestry);
  const whole = new Set(toggled);
  // A row under two roots shows in both streams, and is one row to move to or select.
  const shown = new Map(streams.flatMap((stream) => shownRows(stream, whole).map(({ row }) => [row.id, row] as const)));
  const order = [...shown.values()];
  return { streams, whole, order, rows: order, pending, error, retry };
}

/**
 * The streams in a list: under each root goal's header, its newest few rows,
 * then how many more, which shows the rest; the No parent group last.
 */
export function StreamsRows({
  view,
  focusedId,
  now,
  onFocus,
  onToggle,
  selection,
}: {
  view: StreamsView;
  focusedId: string | null;
  now: number;
  onFocus: (id: string) => void;
  onToggle: (key: string) => void;
  selection: Selection;
}) {
  if (view.error && view.streams.length === 0) return <ReadFailure error={view.error} retry={view.retry} />;
  if (view.pending) return <p className="list-loading">Loading…</p>;
  return view.streams.map((stream) => {
    const shown = shownRows(stream, view.whole);
    const hidden = stream.rows.length - shown.length;
    return (
      <section key={streamKey(stream)} className="s-stream" aria-label={stream.root?.title ?? "No parent"}>
        <StreamHead stream={stream} now={now} />
        {shown.map(({ row, parent, alsoUnder }) => (
          <Row
            key={row.id}
            row={row}
            mixed={false}
            focused={row.id === focusedId}
            now={now}
            onFocus={onFocus}
            selected={selection.ids.has(row.id)}
            onSelect={selection.enabled ? selection.select : undefined}
            context={parent?.title}
            note={
              alsoUnder.length > 0 ? (
                <span className="s-also">also under {alsoUnder.map(({ seq }) => `#${seq}`).join(", ")}</span>
              ) : null
            }
          />
        ))}
        {hidden > 0 && (
          <button type="button" className="s-more" onClick={() => onToggle(streamKey(stream))}>
            {hidden} more in this stream
          </button>
        )}
      </section>
    );
  });
}

/** A stream's header: its root goal, the page's count of rows under it, active and done, and its newest row's age. */
function StreamHead({ stream: { root, rows, active, done, newest }, now }: { stream: Stream; now: number }) {
  const part = (count: number) => `${(100 * count) / rows.length}%`;
  return (
    <div className="s-head">
      {root && <StatusGlyph status={root.status} />}
      <span className="s-title">
        {root ? <a href={formatRoute({ name: "ref", kind: root.kind, seq: root.seq })}>{root.title}</a> : "No parent"}
      </span>
      {root && <span className="s-ref">#{root.seq}</span>}
      <span className="s-counts">
        {rows.length === 1 ? "1 issue" : `${rows.length} issues`} · {active} active · {done} done · newest{" "}
        {shortAge(newest, now)}
      </span>
      <span className="s-bar" aria-hidden="true">
        <i className="s-active" style={{ width: part(active) }} />
        <i className="s-done" style={{ width: part(done) }} />
      </span>
    </div>
  );
}

/** A stream's rows on screen: its newest few, or all once shown whole. */
function shownRows(stream: Stream, whole: ReadonlySet<string>): Stream["rows"] {
  return whole.has(streamKey(stream)) ? stream.rows : stream.rows.slice(0, FEW);
}

/** The list's stored key for a stream shown whole, by its root; no Issue id is `none`. */
function streamKey(stream: Stream): string {
  return `streams:${stream.root?.id ?? "none"}`;
}

/** How many of its newest rows a stream shows before "N more in this stream". */
const FEW = 3;
