// The console minimap's time arithmetic: which span of the history the band
// shows, how it scrolls and zooms, its bars and its ticks. Times are ms since
// the epoch; nothing here reads the clock.

/** The band's zoom levels: an hour, a day or a week. */
export const ZOOMS = ["hour", "day", "week"] as const;
export type Zoom = (typeof ZOOMS)[number];

/** What the band shows: its zoom, and its right edge, or `null` while it follows now. */
export type View = { readonly zoom: Zoom; readonly end: number | null };

/** The history the band scrolls over: from `first` to now. */
export type History = { readonly first: number; readonly now: number };

/** A stretch of time, from `start` to `end`. */
export type Span = { readonly start: number; readonly end: number };

/** The bars for a span: `count` buckets of `seconds` each, from `since` (a multiple of the bucket) to `until`. */
export type Bucketing = { readonly since: number; readonly until: number; readonly seconds: number; readonly count: number };

/** A labelled time on the band's axis. */
export type Tick = { readonly at: number; readonly label: string };

/**
 * The span `view` shows: its zoom's length, ending at its right edge or now,
 * and starting no earlier than the history, so a band scrolled back to its
 * start moves on with it.
 */
export function spanOf(view: View, history: History): Span {
  const end = edgeOf(view.zoom, view.end ?? history.now, history);
  return { start: end - ZOOM_MS[view.zoom], end };
}

/**
 * `view` scrolled so its right edge is at `end`, but its left edge no earlier
 * than the history's start; an edge at now or later follows now again.
 */
export function moveTo(view: View, end: number, history: History): View {
  const edge = edgeOf(view.zoom, end, history);
  return { zoom: view.zoom, end: edge >= history.now ? null : edge };
}

/**
 * `view` at `zoom`, keeping the time `fixed` where it is along the band (the
 * feed's newest record, say); with none, or one off the band, the right edge
 * stays. A live band zoomed about now stays live.
 */
export function zoomTo(view: View, zoom: Zoom, history: History, fixed: number | null): View {
  const span = spanOf(view, history);
  const onBand = fixed !== null && fixed >= span.start && fixed <= span.end;
  const kept = onBand ? fixed : span.end;
  return moveTo({ zoom, end: view.end }, kept + (1 - fractionOf(span, kept)) * ZOOM_MS[zoom], history);
}

/** The scroll track's span: the history, or the band's span when that is longer. */
export function trackOf(view: View, history: History): Span {
  const span = spanOf(view, history);
  return { start: Math.min(history.first, span.start), end: Math.max(history.now, span.end) };
}

/** The time `fraction` of the way along `span`. */
export function at(span: Span, fraction: number): number {
  return span.start + fraction * (span.end - span.start);
}

/** How far along `span` `time` is: 0 at its start, 1 at its end, beyond them outside it. */
export function fractionOf(span: Span, time: number): number {
  return (time - span.start) / (span.end - span.start);
}

/** The part of the band `shown` covers, as fractions of it; null when it is off the band or unknown. */
export function markOf(span: Span, shown: Span | null): { readonly left: number; readonly right: number } | null {
  if (!shown || shown.end < span.start || shown.start > span.end) return null;
  return { left: Math.max(0, fractionOf(span, shown.start)), right: Math.min(1, fractionOf(span, shown.end)) };
}

/**
 * The bars for `span` on a band `widthPx` wide: as many as fit `BAR_PX` apart,
 * each a round length, aligned to multiples of it so a bar stays put as the
 * band scrolls; one under the server's cap, since a read asks for one more.
 */
export function bucketsFor(span: Span, widthPx: number): Bucketing {
  const bars = Math.min(MAX_BUCKETS - 1, Math.max(2, Math.floor(widthPx / BAR_PX)));
  // One bar fewer than fit, since aligning both ends can add one.
  const least = (span.end - span.start) / (bars - 1) / 1000;
  const seconds = BUCKET_SECONDS.find((step) => step >= least) ?? Math.ceil(least / DAY_S) * DAY_S;
  const ms = seconds * 1000;
  const since = Math.floor(span.start / ms) * ms;
  const until = Math.ceil(span.end / ms) * ms;
  return { since, until, seconds, count: (until - since) / ms };
}

/**
 * The axis's ticks for `span` on a band `widthPx` wide: about `TICK_PX` apart,
 * at round local times, each labelled `HH:MM`, or by its date at midnight.
 */
export function ticks(span: Span, widthPx: number): Tick[] {
  const least = ((span.end - span.start) * TICK_PX) / Math.max(widthPx, TICK_PX);
  const step = TICK_MS.find((ms) => ms >= least) ?? Math.ceil(least / DAY_MS) * DAY_MS;
  const found: Tick[] = [];
  if (step < DAY_MS) {
    // Local, so ticks fall on the hour in a zone half an hour off UTC too.
    const offset = -new Date(span.start).getTimezoneOffset() * 60_000;
    for (let time = Math.ceil((span.start + offset) / step) * step - offset; time <= span.end; time += step) {
      found.push({ at: time, label: labelOf(time) });
    }
    return found;
  }
  // By the calendar, since a local day is not always 24 hours long.
  const day = new Date(span.start);
  day.setHours(0, 0, 0, 0);
  if (day.getTime() < span.start) day.setDate(day.getDate() + 1);
  for (; day.getTime() <= span.end; day.setDate(day.getDate() + step / DAY_MS)) {
    found.push({ at: day.getTime(), label: DATE.format(day) });
  }
  return found;
}

/** `HH:MM`, or the date at midnight. */
function labelOf(time: number): string {
  const local = new Date(time);
  return local.getHours() || local.getMinutes() ? CLOCK.format(local) : DATE.format(local);
}

/** `end` moved to where a band at `zoom` ends no later than now, and starts no earlier than the history when it fits. */
function edgeOf(zoom: Zoom, end: number, history: History): number {
  return Math.min(history.now, Math.max(history.first + ZOOM_MS[zoom], end));
}

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;
const DAY_S = DAY_MS / 1000;

const ZOOM_MS = { hour: HOUR_MS, day: DAY_MS, week: 7 * DAY_MS } as const;

/** The least width of a bar and the gap after it, in px. */
const BAR_PX = 6;

/** The most buckets the server answers for (`buckets`). */
const MAX_BUCKETS = 1000;

/** Round bucket lengths, in seconds; past the last, whole days. */
const BUCKET_SECONDS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10_800, 21_600, 43_200, 86_400];

/** The least room for a tick's label, in px. */
const TICK_PX = 160;

/** Round tick steps; past the last, whole days. */
const TICK_MS = [1, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 10_080].map((minutes) => minutes * MINUTE_MS);

const CLOCK = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const DATE = new Intl.DateTimeFormat("en", { month: "short", day: "numeric" });
