import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type PointerEvent, type RefObject, useContext, useEffect, useEffectEvent, useRef, useState } from "react";
import type { HistogramRead } from "../api/histogram";
import type { FeedFilters } from "../api/sessions";
import { dateTime } from "../detail/time";
import { LiveContext } from "../live";
import { ReadFailure } from "../ui/failure";
import { HistogramLive, histogramQuery, iso, spanRead } from "./histogram";
import { at, bucketsFor, fractionOf, markOf, moveTo, type Span, spanOf, ticks, trackOf, type View, ZOOMS, zoomTo } from "./timeline";
import "./Minimap.css";

export type MinimapProps = {
  /** The feed's filters: the band counts the records they pass. */
  readonly filters: FeedFilters;
  /**
   * The time the feed's records on screen span, oldest to newest (ISO), with
   * `until` null while the feed follows now; null while it shows none.
   */
  readonly shown: { readonly since: string; readonly until: string | null } | null;
  /** A click: move the feed to `time` (ISO). */
  readonly onSeek: (time: string) => void;
  /** A drag: show the window from `since` to `until` (ISO) in the feed. */
  readonly onWindow: (since: string, until: string) => void;
};

/**
 * The console's minimap: how many records each stretch of time holds, as bars
 * over an hour, a day or a week of the last 7 days, as far back as the server's
 * histogram counts.
 *
 * The wheel and the track below it scroll across those 7 days; at now, the
 * band follows now and its newest bar keeps current with the stream. It
 * marks the time the feed shows and lights the bars there. A click moves the
 * feed to the time under the pointer; a drag shows the window dragged across.
 */
export function Minimap({ filters, shown, onSeek, onWindow }: MinimapProps) {
  const now = useClock();
  const [view, setView] = useState<View>({ zoom: "hour", end: null });
  const root = useRef<HTMLDivElement>(null);
  const width = useWidth(root);
  const history = { first: now - HISTORY_MS, now };
  const span = spanOf(view, history);
  // Read once the view stays put, not for every step of a scroll.
  const settled = useSettled(view);
  const bucketing = bucketsFor(spanOf(settled, history), width);
  const read = spanRead(filters, bucketing, history.first);
  const counts = useQuery({
    ...histogramQuery(read),
    enabled: width > 0,
    // The counts shown stay while the next load, as the band scrolls; coarser
    // ones, after a zoom in, would stretch one bar across the band.
    placeholderData: (shown) => (shown && shown.bucket_seconds <= bucketing.seconds ? shown : undefined),
  });
  useLiveBand(settled.end === null && view.end === null ? read : null);
  const feed = shown && { start: Date.parse(shown.since), end: shown.until === null ? now : Date.parse(shown.until) };
  const mark = markOf(span, feed);
  const track = trackOf(view, history);
  const along = (event: PointerEvent<HTMLElement>) =>
    Math.min(1, Math.max(0, (event.clientX - event.currentTarget.getBoundingClientRect().left) / width));

  const scroll = useEffectEvent((event: WheelEvent) => {
    event.preventDefault();
    const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
    const px = event.deltaMode === WheelEvent.DOM_DELTA_LINE ? delta * LINE_PX : delta;
    setView((view) => {
      const { start, end } = spanOf(view, history);
      return moveTo(view, end + (px / width) * (end - start), history);
    });
  });
  useEffect(() => {
    // Not React's onWheel, which listens passively: a horizontal swipe the
    // band does not cancel would take the browser back a page.
    const element = root.current!;
    const listener = (event: WheelEvent) => scroll(event);
    element.addEventListener("wheel", listener, { passive: false });
    return () => element.removeEventListener("wheel", listener);
  }, []);

  /** Where a press on the band started, while it is held. */
  const pick = useRef<{ x: number; from: number; dragged: boolean } | null>(null);
  const [picking, setPicking] = useState<Span | null>(null);
  /** The pointer's time on the track less the band's right edge, while the track is held. */
  const grab = useRef<number | null>(null);

  return (
    <div className="minimap" ref={root}>
      <div className="minimap-head">
        <div className="minimap-zooms" role="group" aria-label="Zoom">
          {ZOOMS.map((zoom) => (
            <button
              key={zoom}
              type="button"
              aria-pressed={view.zoom === zoom}
              onClick={() => setView(zoomTo(view, zoom, history, feed?.end ?? null))}
            >
              {ZOOM_LABELS[zoom]}
            </button>
          ))}
        </div>
        {view.end !== null ? (
          <button type="button" className="minimap-now" onClick={() => setView({ zoom: view.zoom, end: null })}>
            Now
          </button>
        ) : null}
      </div>
      <div
        className="minimap-band"
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          event.currentTarget.setPointerCapture(event.pointerId);
          pick.current = { x: event.clientX, from: along(event), dragged: false };
        }}
        onPointerMove={(event) => {
          const held = pick.current;
          if (!held) return;
          held.dragged ||= Math.abs(event.clientX - held.x) >= DRAG_PX;
          const to = along(event);
          if (held.dragged) setPicking({ start: Math.min(held.from, to), end: Math.max(held.from, to) });
        }}
        onPointerUp={(event) => {
          const held = pick.current;
          pick.current = null;
          setPicking(null);
          if (!held) return;
          const to = along(event);
          if (held.dragged || Math.abs(event.clientX - held.x) >= DRAG_PX) {
            onWindow(iso(at(span, Math.min(held.from, to))), iso(at(span, Math.max(held.from, to))));
          } else onSeek(iso(at(span, to)));
        }}
        onPointerCancel={() => {
          pick.current = null;
          setPicking(null);
        }}
      >
        {mark ? <div className="minimap-mark" style={{ left: percent(mark.left), width: percent(mark.right - mark.left) }} /> : null}
        {picking ? (
          <div className="minimap-pick" style={{ left: percent(picking.start), width: percent(picking.end - picking.start) }} />
        ) : null}
        {counts.error && !counts.data ? (
          <ReadFailure error={counts.error} retry={() => void counts.refetch()} />
        ) : width > 0 && counts.data ? (
          <svg className="minimap-bars" viewBox={`0 0 ${width} ${BAND_PX}`} preserveAspectRatio="none" aria-hidden="true">
            <Bars counts={counts.data.counts} bucketMs={counts.data.bucket_seconds * 1000} span={span} width={width} feed={feed} />
          </svg>
        ) : null}
      </div>
      <div className="minimap-axis" aria-hidden="true">
        {ticks(span, width).map((tick) => (
          <span key={tick.at} style={{ left: percent(fractionOf(span, tick.at)) }}>
            {tick.label}
          </span>
        ))}
      </div>
      <div
        className="minimap-track"
        role="slider"
        tabIndex={0}
        aria-label="Place in history"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(fractionOf(track, span.end) * 100)}
        aria-valuetext={view.end === null ? "Now" : dateTime(iso(span.end))}
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          event.currentTarget.setPointerCapture(event.pointerId);
          const time = at(track, along(event));
          // Held by the thumb, the band moves with the pointer; pressed elsewhere, it centres there.
          grab.current = time >= span.start && time <= span.end ? time - span.end : (span.start - span.end) / 2;
          setView(moveTo(view, time - grab.current, history));
        }}
        onPointerMove={(event) => {
          const held = grab.current;
          if (held === null) return;
          const time = at(track, along(event));
          setView((view) => moveTo(view, time - held, history));
        }}
        onPointerUp={() => (grab.current = null)}
        onPointerCancel={() => (grab.current = null)}
        onKeyDown={(event) => {
          const step = (span.end - span.start) / 4;
          const end = new Map([
            ["ArrowLeft", span.end - step],
            ["ArrowRight", span.end + step],
            ["Home", -Infinity],
            ["End", Infinity],
          ]).get(event.key);
          if (end === undefined) return;
          event.preventDefault();
          setView(moveTo(view, end, history));
        }}
      >
        <div
          className="minimap-thumb"
          style={{ left: percent(fractionOf(track, span.start)), width: percent((span.end - span.start) / (track.end - track.start)) }}
        />
      </div>
    </div>
  );
}

/** One bar for each bucket with records, as tall as its count against the tallest shown; lit where the feed is. */
function Bars({
  counts,
  bucketMs,
  span,
  width,
  feed,
}: {
  counts: readonly { readonly start: string; readonly count: number }[];
  bucketMs: number;
  span: Span;
  width: number;
  feed: Span | null;
}) {
  const shown = counts
    .map(({ start, count }) => ({ start: Date.parse(start), count }))
    .filter(({ start, count }) => count > 0 && start + bucketMs > span.start && start < span.end);
  const most = Math.max(1, ...shown.map(({ count }) => count));
  const barWidth = Math.max(1, (bucketMs / (span.end - span.start)) * width - GAP_PX);
  return shown.map(({ start, count }) => {
    const height = (count * BAND_PX) / most;
    const lit = feed !== null && start <= feed.end && start + bucketMs > feed.start;
    return (
      <rect
        key={start}
        className={lit ? "minimap-bar on" : "minimap-bar"}
        x={fractionOf(span, start) * width}
        y={BAND_PX - height}
        width={barWidth}
        height={height}
      />
    );
  });
}

/** Keep the counts `read` reads current with the stream while the band follows now; null while it is scrolled back. */
function useLiveBand(read: HistogramRead | null): void {
  const hub = useContext(LiveContext);
  const client = useQueryClient();
  const [live] = useState(() => new HistogramLive(client));
  useEffect(() => live.follow(read));
  useEffect(() => (hub ? hub.register({ update: (batch) => live.update(batch) }).dispose : undefined), [hub, live]);
}

/** `value`, once it has stayed the same for `SETTLE_MS`. */
function useSettled<T>(value: T): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    if (value === settled) return;
    const timer = setTimeout(() => setSettled(value), SETTLE_MS);
    return () => clearTimeout(timer);
  }, [value, settled]);
  return settled;
}

/** The client clock, every `CLOCK_MS`, so a live band moves with now. */
function useClock(): number {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), CLOCK_MS);
    return () => clearInterval(timer);
  }, []);
  return now;
}

/** `element`'s content width, 0 until it is laid out. */
function useWidth(element: RefObject<HTMLElement | null>): number {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => setWidth(entry!.contentRect.width));
    observer.observe(element.current!);
    return () => observer.disconnect();
  }, [element]);
  return width;
}

function percent(fraction: number): string {
  return `${fraction * 100}%`;
}

const ZOOM_LABELS = { hour: "Hour", day: "Day", week: "Week" } as const;

/** How far back the band reaches: the server's histogram counts only the last 7 days. */
const HISTORY_MS = 7 * 24 * 3_600_000;

/** The band's height, in px. */
const BAND_PX = 44;

/** The gap after each bar, in px. */
const GAP_PX = 1;

/** How far the pointer moves before a press is a drag, not a click, in px. */
const DRAG_PX = 4;

/** A wheel line, for a wheel that scrolls by lines, in px. */
const LINE_PX = 16;

/** How long the view stays put before the band reads its span. */
const SETTLE_MS = 150;

/** How often a live band moves with now. */
const CLOCK_MS = 5_000;
