import { useMemo } from "react";
import { useMeta } from "../app/boot";
import { useTabState } from "../app/tabState";
import { dateTime, relativeTime, useMinuteClock } from "../detail/time";
import { LiveFailureBar, useLiveActivity } from "../live";
import { Markdown } from "../markdown/Markdown";
import { formatRoute } from "../router/route";
import { Bar } from "../ui/bars";
import { ReadFailure } from "../ui/failure";
import { Avatar, RefChip } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { KindIcon } from "../ui/kinds";
import { EmptyState, ViewHeader } from "../ui/view";
import "../detail/timeline.css";
import "./activity.css";
import {
  dayLabel,
  type FeedItem,
  feedItems,
  isTab,
  KIND_TABS,
  type Mention,
  mentioned,
  type Tab,
  tabKinds,
} from "./feed";
import { type Subject, useChangePages, useSubjects } from "./queries";

/**
 * Recent changes, `#/activity`: a tab per set of change kinds, and All, which
 * holds every tab's. Each tab reads its kinds' `change_log` in one request,
 * newest first, 50 at a time. Its tab and pages last for this browser tab, so
 * Back returns to the feed as it was.
 */
export function ActivityView() {
  const { edges, kinds: inquiryKinds } = useMeta();
  const [state, setState] = useTabState<ActivityState>(STORAGE_KEY, readState, () => ({ tab: "all", pages: 1 }));
  const kinds = tabKinds(state.tab);
  const loaded = useChangePages(kinds, state.pages);
  const liveFailure = useLiveActivity(state.tab);
  const labels = useMemo(
    () => Object.fromEntries(Object.entries(edges).map(([edgeKind, rule]) => [edgeKind, rule.forward])),
    [edges],
  );
  const items = loaded.ready ? feedItems(loaded.rows, labels) : [];
  // Oldest mention first: lookups go 13 ids at a time, keyed by their ids, and
  // live changes join at the top, so only the newest lookup changes. Newest
  // first, every line at the top would shift and refetch them all.
  const subjects = useSubjects(mentioned(items.toReversed()));
  const now = useMinuteClock();
  // A page that failed is the last one asked for, and asking for one more would wait on it: ask it again.
  const loadMore = () => (loaded.error ? loaded.retry() : setState((s) => ({ ...s, pages: s.pages + 1 })));

  return (
    <div className="view activity">
      <ViewHeader icon={<Icon name="activity" />} title="Activity">
        <nav className="tabs" aria-label="Change kind">
          {[{ tab: "all", label: "All" } as const, ...KIND_TABS].map(({ tab, label }) => (
            <button
              key={tab}
              type="button"
              className="tab"
              aria-current={state.tab === tab ? "page" : undefined}
              onClick={() => setState((s) => (s.tab === tab ? s : { tab, pages: 1 }))}
            >
              {label}
            </button>
          ))}
        </nav>
      </ViewHeader>
      {loaded.error && items.length > 0 ? (
        <Bar kind="stale">
          Could not load all of Activity: {loaded.error.message}
          <button type="button" className="btn ghost" onClick={loaded.retry}>
            Retry
          </button>
        </Bar>
      ) : null}
      <LiveFailureBar failure={liveFailure} />
      {subjects.error ? (
        <Bar kind="stale">
          Could not look up the inquiries named: {subjects.error.message}
          <button type="button" className="btn ghost" onClick={subjects.retry}>
            Retry
          </button>
        </Bar>
      ) : null}
      <div className="scroll" aria-busy={loaded.loading}>
        {loaded.error && items.length === 0 ? <ReadFailure error={loaded.error} retry={loaded.retry} /> : null}
        <div className="feed">
          {!loaded.ready ? (
            <p className="feed-note">Loading…</p>
          ) : items.length === 0 ? (
            !loaded.error && (
              <EmptyState icon={<Icon name="activity" size={24} />} title="No activity">
                <p>{state.tab === "all" ? "Nothing has changed yet." : "No changes of this kind yet."}</p>
              </EmptyState>
            )
          ) : (
            <>
              {days(items, now).map(({ label, items: day }) => (
                <section key={label} aria-label={label}>
                  <h2 className="feed-day">{label}</h2>
                  <ol className="feed-list">
                    {day.map((item) => (
                      <Line key={item.change.id} item={item} subjects={subjects.byId} kinds={inquiryKinds} now={now} />
                    ))}
                  </ol>
                </section>
              ))}
              <div className="feed-foot">
                {loaded.more && (
                  <button type="button" className="btn" disabled={loaded.loading} onClick={loadMore}>
                    {loaded.loading ? "Loading…" : "Load more"}
                  </button>
                )}
                <span>{items.length} shown</span>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/** One change: who, what, on which inquiry, why, and when. */
function Line({
  item,
  subjects,
  kinds,
  now,
}: {
  item: FeedItem;
  subjects: ReadonlyMap<string, Subject>;
  kinds: readonly string[];
  now: number;
}) {
  const { change, subject, phrase } = item;
  const title = subjects.get(subject.id)?.title;
  return (
    <li className="feed-row">
      <Avatar actor={change.actor} size={20} />
      <div className="tl-body">
        <b>{change.actor}</b>{" "}
        {phrase.map((part, index) =>
          typeof part === "string" ? part : <MentionLink key={`${index}:${part.id}`} mention={part} subjects={subjects} />,
        )}{" "}
        <span className="muted">on</span> <MentionLink mention={subject} subjects={subjects} />
        {title !== undefined && <span className="feed-title"> {clip(title)}</span>}
        {change.reason ? (
          <blockquote>
            <Markdown source={change.reason} kinds={kinds} />
          </blockquote>
        ) : null}
      </div>
      <time className="tl-time" dateTime={change.created} title={dateTime(change.created)}>
        {relativeTime(change.created, now)}
      </time>
    </li>
  );
}

/**
 * A named inquiry: its ref once looked up; until then, or when it has been
 * purged, a link by id, which opens it or says it is gone.
 */
function MentionLink({ mention, subjects }: { mention: Mention; subjects: ReadonlyMap<string, Subject> }) {
  const found = subjects.get(mention.id);
  if (found) return <RefChip kind={found.kind} seq={found.seq} title={found.title} />;
  return (
    <a className="ref" href={formatRoute({ name: "lookup", id: mention.id })} title={mention.id}>
      <KindIcon kind={mention.kind} size={12} />
      {`${mention.kind} ${mention.id.slice(0, 8)}`}
    </a>
  );
}

/** Consecutive lines of one day, under that day's label. */
function days(items: readonly FeedItem[], now: number): { label: string; items: FeedItem[] }[] {
  const out: { label: string; items: FeedItem[] }[] = [];
  for (const item of items) {
    const label = dayLabel(item.change.created, now);
    if (out.at(-1)?.label === label) out.at(-1)!.items.push(item);
    else out.push({ label, items: [item] });
  }
  return out;
}

/** A title as the mock quotes it in a line: at most 70 characters, counted by code point so none is cut in two. */
function clip(title: string): string {
  const chars = [...title];
  return chars.length > 70 ? `${chars.slice(0, 69).join("")}…` : title;
}

/** The feed's state in this browser tab: its tab, and how many pages it has loaded. */
type ActivityState = { readonly tab: Tab; readonly pages: number };

/** A stored state, or null when it is not one this build can show, such as an older build's tab. */
function readState(saved: unknown): ActivityState | null {
  if (typeof saved !== "object" || saved === null) return null;
  const { tab, pages } = saved as { [field: string]: unknown };
  return isTab(tab) && Number.isSafeInteger(pages) && (pages as number) >= 1 ? { tab, pages: pages as number } : null;
}

const STORAGE_KEY = "trackinizer.v2.activity";
