import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  memo,
  type RefObject,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { newUuid } from "../api/idempotency";
import {
  type FeedActorFacet,
  type FeedEvent,
  type FeedFacets,
  type FeedFilters,
  type MessageTarget,
  readFeedFacets,
  sendRoutedMessage,
} from "../api/sessions";
import { useMeta, useWriteMode } from "../app/boot";
import { useCommands } from "../commands/registry";
import { Composer } from "../composer/Composer";
import { useDrawnFrom } from "../detail/transcript/drawing";
import { RecordBody } from "../detail/transcript/RecordBody";
import { isLong, recordView } from "../detail/transcript/records";
import { LiveContext, LiveFailureBar } from "../live";
import { refetchFresh } from "../live/cache";
import type { Batch, Failure, Later } from "../live/serial";
import { StreamStatusContext } from "../ui/bars";
import { ReadFailure } from "../ui/failure";
import { Icon } from "../ui/icons";
import { type PanelSpec, PanelToggle, panelCommand, usePanel } from "../ui/panel";
import { ViewHeader } from "../ui/view";
import { ACTIVE, type Active, AgentsSection, PickSection } from "./FacetsRail";
import { feedFilter, picked, remember, roomsOf, shown, toTargets } from "./facets";
import { ConsoleFeed, type FeedState, type Held, type Range } from "./feed";
import { type Level, LEVELS, levelCounts, levelOf } from "./levels";
import { MentionField } from "./MentionField";
import { Minimap } from "./Minimap";
import { parseLine, receipt, targetName } from "./send";
import { useViews } from "./views";
import { ViewsSection } from "./ViewsRail";
import "./console.css";

/**
 * The console (`#/console`): every session's captured records as one live feed,
 * each drawn as the transcript draws it, narrowed by the open view; and, for
 * writers, a line that messages agents.
 *
 * A view picks agents and rooms (by name, or by a pattern that takes in new
 * ones), CLIs, a level and a place in time, and saves itself as it changes; the
 * rail lists the views and the facets that find agents in a long list; its
 * button in the header, or `[`, collapses it for the tab. The server filters
 * the feed by the view's picks and its level's kinds (Messages, by the
 * conversation itself); the facets count over the view's window, or
 * over the last 15 m to 7 d when live. That
 * window only finds agents: a pattern picks among every agent the facets have
 * listed while the console is open (`remember`), so a narrower window drops none.
 *
 * A line with no target goes to the To chips, the view's agents that have not
 * ended, less those left out of that view; `@agent`, `@agent:room`, `@a,@b`, and
 * `@*` for every agent shown, still work, and `@` lists what it may complete
 * to, the view's own agents first (`MentionField`). One request goes per
 * target, each under a key. A retry goes to the targets the first send went
 * to, under the same keys, whatever shows by then.
 */
export function ConsoleView() {
  const hub = useContext(LiveContext);
  const paused = useContext(StreamStatusContext) === "paused";
  const mode = useWriteMode();
  const { kinds } = useMeta();
  const { views, open: view, change, openView, edit, remove } = useViews();
  const rail = usePanel(RAIL);
  useCommands([panelCommand(rail)]);
  const [active, setActive] = useState<Active>("1h");
  const facets = useFacets(view.range, active, EVERY_AGENT);
  const agents = useMemo(() => facets.data?.actors ?? [], [facets.data]);
  const seen = useSeen(agents);
  // A pattern picks among the agents the facets know, so its feed waits for them.
  const waiting = !facets.data && [...view.agents, ...view.rooms].some((pick) => pick.includes("*"));
  const filter = waiting ? undefined : feedFilter(view, seen);
  const feed = useFeed(view.range, filter, view.level);
  const state = useSyncExternalStore(feed?.subscribe ?? noSubscription, feed?.getSnapshot ?? emptyFeed);
  const [failure, setFailure] = useState<Failure | null>(null);
  useEffect(() => {
    if (!feed) return;
    void feed.load();
    if (!hub || !view.range.live) return;
    // A captured record wakes the stream with its session's id; any batch may be one.
    const registration = hub.register({ update: (batch) => feed.update(batch), failing: setFailure });
    return () => {
      registration.dispose();
      setFailure(null);
    };
  }, [feed, hub, view.range.live]);
  const visible = useMemo(() => state.held.filter(({ event }) => levelOf(event) <= view.level), [state.held, view.level]);
  // Counted under the view's picks, its records by kind are its own; with no picks, it is the read above.
  const counting = useFacets(view.range, active, filter ? { actor: filter.actor, room: filter.room, cli: filter.cli } : null);
  // A view that reads nothing counts nothing, whatever counts the last picks kept.
  const counted = filter ? counting.data : undefined;
  const counts = counted
    ? levelCounts({
        conversation: sum(counted.actors, "conversation"),
        count: sum(counted.actors, "count"),
        kinds: Object.fromEntries(counted.kinds.map(({ kind, count }) => [kind, count])),
      })
    : [];
  const showing = shown(view, seen);
  const rooms = picked(view.rooms, roomsOf(seen));
  const targets = showing.everyone ? [] : toTargets(showing.agents, rooms);
  // The chips left out of each view, by its id.
  const [leftOut, setLeftOut] = useState<ReadonlyMap<string, ReadonlySet<string>>>(new Map());
  const out = leftOut.get(view.id) ?? new Set<string>();
  const to = targets.filter((target) => !out.has(targetName(target)));
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  // Each draft's route, by the key the composer gave the draft, so its Retry
  // sends to the targets it first went to, each under the key it went under.
  const routes = useRef(new Map<string, Route>());
  const shownEvents = useMemo(() => visible.map(({ event }) => event), [visible]);
  // The line whose agent was last clicked, for the message box to address; stable, so the lines stay memoised.
  const [adding, setAdding] = useState<{ readonly line: FeedEvent } | null>(null);
  const mention = useCallback((line: FeedEvent) => setAdding({ line }), []);

  async function send(line: string, key: string): Promise<string> {
    const route = routes.current.get(key) ?? routeOf(line, shownEvents, to);
    routes.current.set(key, route);
    const results = await Promise.allSettled(route.targets.map((target) => sendRoutedMessage(target.target, route.text, target.key)));
    return receipt(route.targets.map(({ target }) => target), results);
  }

  return (
    <div className="view console">
      <ViewHeader icon={<Icon name="terminal" />} title={`Console · ${view.name}`}>
        <span className="console-mark" data-live={view.range.live && !failure && !paused}>
          {view.range.live ? (failure || paused ? "○ Offline" : "● Live") : "History"}
        </span>
        <PanelToggle panel={rail} controls="console-rail" />
      </ViewHeader>
      <LiveFailureBar failure={failure} />
      <div className="console-body">
        <aside id="console-rail" className="console-rail" aria-label="Views and filters" hidden={rail.collapsed}>
          <ViewsSection views={views} open={view} onOpen={openView} onEdit={edit} onRemove={remove} />
          {facets.error ? <ReadFailure error={facets.error} retry={() => void facets.refetch()} /> : null}
          <AgentsSection
            agents={agents}
            picks={view.agents}
            onPicks={(picks) => change({ ...view, agents: picks })}
            active={active}
            onActive={view.range.live ? setActive : null}
          />
          <PickSection
            label="Rooms"
            items={(facets.data?.rooms ?? []).map(({ room, count }) => ({ name: room, count }))}
            picks={view.rooms}
            onPicks={(picks) => change({ ...view, rooms: picks })}
            find
          />
          <PickSection label="CLI" items={clis(agents)} picks={view.clis} onPicks={(picks) => change({ ...view, clis: picks })} />
        </aside>
        <section className="console-stream">
          <div className="console-tools">
            <div className="console-seg" role="group" aria-label="Level">
              {LEVELS.map(({ level, name }) => (
                <button key={level} type="button" aria-pressed={view.level === level} onClick={() => change({ ...view, level })}>
                  {name}
                  {counts[level - 1] === undefined ? null : <span className="console-n">{counts[level - 1]!.toLocaleString("en")}</span>}
                </button>
              ))}
            </div>
            <RangeControls key={JSON.stringify(view.range)} range={view.range} onRange={(range) => change({ ...view, range })} />
          </div>
          {filter ? (
            <Minimap
              filters={filter}
              shown={visible.length ? { since: visible[0]!.event.created, until: view.range.live ? null : visible.at(-1)!.event.created } : null}
              onSeek={(time) => change({ ...view, range: { live: false, since: time, until: null } })}
              onWindow={(since, until) => change({ ...view, range: { live: false, since, until } })}
            />
          ) : null}
          <div
            className="scroll console-feed"
            ref={scroller}
            onScroll={(event) => {
              const { scrollHeight, scrollTop, clientHeight } = event.currentTarget;
              stick.current = scrollHeight - scrollTop - clientHeight < 40;
            }}
          >
            {state.error ? <ReadFailure error={state.error} retry={() => void (state.held.length ? feed?.more() : feed?.load())} /> : null}
            <FeedNote filter={filter} state={state} empty={!visible.length} />
            <Lines
              key={`${JSON.stringify(filter)}|${view.level}`}
              held={visible}
              kinds={kinds}
              onMention={mode === "hidden" ? null : mention}
              scroller={scroller}
              stick={stick}
            />
            {!view.range.live && state.more ? (
              <button type="button" className="btn console-more" onClick={() => void feed?.more()}>
                Load more
              </button>
            ) : null}
          </div>
          {mode !== "hidden" ? (
            <div className="console-send">
              {targets.length ? (
                <ToChips to={to} onLeaveOut={(target) => setLeftOut(new Map([...leftOut, [view.id, new Set([...out, targetName(target)])]]))} />
              ) : null}
              <Composer
                send={send}
                check={(line) => {
                  const parsed = parseLine(line, shownEvents, to);
                  return "problem" in parsed ? parsed.problem : "";
                }}
                target="console"
                enabled={mode === "enabled"}
                placeholder={
                  to.length
                    ? `Message ${to.length === 1 ? "1 agent" : `${to.length} agents`}… (@agent, @agent:room and @* still work)`
                    : "@agent message · @agent:room message · @a,@b message · @* message to every agent shown"
                }
                failure={(error) => error.message}
                field={(props, edit) => (
                  <MentionField
                    field={props}
                    edit={edit}
                    context={{ agents: seen, lines: shownEvents, shown: showing.everyone ? [] : showing.agents.map(({ actor }) => actor), rooms }}
                    adding={adding}
                  />
                )}
              />
            </div>
          ) : null}
        </section>
      </div>
    </div>
  );
}

/** `iso` as the local `HH:MM:SS`. */
export function clock(iso: string): string {
  return CLOCK.format(new Date(iso));
}

/**
 * The feed of `range` that `filter` lets through: none while `filter` waits for
 * the facets (undefined) or picks no agent (null). A new feed only when what it
 * reads changes, since the view and the facets are new objects on every change.
 * + Output and All read the same records, so their feed fills its first read
 * with records + Output shows, the fewer.
 */
function useFeed(range: Range, filter: FeedFilters | null | undefined, level: Level): ConsoleFeed | null {
  const client = useQueryClient();
  const reads = JSON.stringify([range, filter ?? null, Math.min(level, 3)]);
  return useMemo(() => {
    const [kept, picks, filling] = JSON.parse(reads) as [Range, FeedFilters | null, Level];
    return picks ? new ConsoleFeed(client, kept, picks, filling) : null;
  }, [client, reads]);
}

/** Every agent the facets have listed while the console is open, one row per name (`remember`). */
function useSeen(agents: readonly FeedActorFacet[]): FeedActorFacet[] {
  const [known, setKnown] = useState<ReadonlyMap<string, FeedActorFacet>>(() => new Map());
  const seen = useMemo(() => remember(known, agents), [known, agents]);
  useEffect(() => setKnown(seen), [seen]);
  return useMemo(() => [...seen.values()], [seen]);
}

/**
 * The facets over the view's window, under `scope` (none read while it is null):
 * a history range's own window, but at most the 7 days before its end (now, for
 * an open one), or, live, the last `active` of the past, read
 * again as the stream says records arrive, at most every 10 s, since each read
 * counts the whole window; and every minute, so that what falls out of the
 * window drops out of it even when nothing arrives.
 */
function useFacets(range: Range, active: Active, scope: FeedFilters | null) {
  const client = useQueryClient();
  const hub = useContext(LiveContext);
  const live = range.live;
  const since = range.live ? null : range.since;
  const until = range.live ? null : range.until;
  const scopeText = JSON.stringify(scope);
  const queryKey = useMemo(
    () => ["console", "facets", live ? active : [since, until], scopeText],
    [live, active, since, until, scopeText],
  );
  const back = ACTIVE.find(({ value }) => value === active)!.ms;
  const query = useQuery({
    queryKey,
    enabled: scope !== null,
    refetchInterval: live ? 60_000 : false,
    // The last counts stay while a new window or new picks load, rather than blank.
    placeholderData: keepPreviousData,
    queryFn: ({ signal }): Promise<FeedFacets> => {
      const picks = scope ?? EVERY_AGENT;
      if (!live) {
        // A facets read counts every record in its window: the whole history, which
        // a range with no start would read, took 8.8 s on 9 million records.
        const earliest = (until ? Date.parse(until) : Date.now()) - RANGE_FACETS_MS;
        const start = since && Date.parse(since) >= earliest ? since : new Date(earliest).toISOString();
        return readFeedFacets({ since: start, until: until ?? undefined, ...picks }, { signal });
      }
      return readFeedFacets({ since: new Date(Date.now() - back).toISOString(), ...picks }, { signal });
    },
  });
  useEffect(() => {
    if (!hub || !live || scope === null) return;
    let readAt = Date.now();
    const update = async ({ gap }: Batch): Promise<Later | null> => {
      const wait = readAt + 10_000 - Date.now();
      if (!gap && wait > 0) return { afterMs: wait };
      readAt = Date.now();
      await refetchFresh(client, { type: "active", queryKey, exact: true });
      return null;
    };
    const registration = hub.register({ update });
    return () => registration.dispose();
    // `scope` is in `queryKey`, as text, so the effect follows it there.
  }, [hub, client, queryKey, live, scope === null]);
  return query;
}

/** A facets read of every agent, room and CLI. */
const EVERY_AGENT: FeedFilters = { actor: [], room: [], cli: [] };

/** The rail as a panel: on the left, and `[` collapses or expands it. */
const RAIL: PanelSpec = { id: "console.rail", name: "views and filters", side: "left", keys: ["["] };

/** The most of a history range the facets count: the 7 days before its end. */
const RANGE_FACETS_MS = 7 * 24 * 3_600_000;

/** Why the feed shows nothing, when it does not: still loading, no agent picked, or nothing matching. */
function FeedNote({ filter, state, empty }: { filter: FeedFilters | null | undefined; state: FeedState; empty: boolean }) {
  if (filter === null) return <p className="unset">No agent matches this view's picks.</p>;
  if (filter === undefined || (state.loading && !state.held.length)) return <p className="unset">Loading…</p>;
  return !state.error && empty ? <p className="unset">No records match this view.</p> : null;
}

/** Where a line with no target goes, each chip with a button that leaves it out of this view's sends. */
export function ToChips({ to, onLeaveOut }: { to: readonly MessageTarget[]; onLeaveOut: (target: MessageTarget) => void }) {
  return (
    <div className="console-to" role="group" aria-label="To">
      <span className="console-label">To</span>
      <ul className="console-picked">
        {to.map((target) => (
          <li key={targetName(target)} className="console-chip">
            <span className={`console-actor console-name a${hue(target.actor)}`} title={targetName(target)}>
              {targetName(target)}
            </span>
            <button type="button" aria-label={`Leave out ${targetName(target)}`} onClick={() => onLeaveOut(target)}>
              <Icon name="x" size={11} />
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * The lines, the newest drawn first (`useDrawnFrom`), and kept at the bottom
 * while the reader is there. Mounted afresh when the view's filter or level
 * changes, so a newly shown set draws newest first too.
 */
function Lines({
  held,
  kinds,
  onMention,
  scroller,
  stick,
}: {
  held: readonly Held[];
  kinds: readonly string[];
  onMention: ((line: FeedEvent) => void) | null;
  scroller: RefObject<HTMLDivElement | null>;
  stick: RefObject<boolean>;
}) {
  const from = useDrawnFrom(held[0]?.n ?? 0, held.at(-1)?.n ?? -1);
  useLayoutEffect(() => {
    const element = scroller.current;
    if (element && stick.current) element.scrollTop = element.scrollHeight;
  });
  return (
    <ol className="console-lines">
      {held.map((one) => (one.n >= from ? <ConsoleLine key={one.key} held={one} kinds={kinds} onMention={onMention} /> : null))}
    </ol>
  );
}

/**
 * One record: when it happened (its own timestamp, as the transcript shows, or
 * when the server stored it, lacking one) and whose session, then its rooms and
 * what it is, on a line of their own, and its body as the transcript draws it.
 * Capture stores records in batches, so the stored time would stamp a batch's
 * lines alike; the feed still runs in the order the server stored them. Every
 * long message shows its first lines until asked, the agent's own too, which a
 * transcript shows whole: among many agents' lines, one long reply would bury
 * the rest. Given `onMention`, the agent is a button that asks for a message to
 * it, as a chat's name does.
 */
export const ConsoleLine = memo(function ConsoleLine({
  held,
  kinds,
  onMention,
}: {
  held: Held;
  kinds: readonly string[];
  onMention: ((line: FeedEvent) => void) | null;
}) {
  const { event, record } = held;
  const drawn = recordView(record);
  const view = drawn.shape === "message" ? { ...drawn, long: isLong(drawn.text) } : drawn;
  const at = event.timestamp ?? event.created;
  const agent = `console-actor console-name a${hue(event.actor)}`;
  return (
    <li className={`turn console-line turn-${view.shape}`}>
      <div className="turn-h">
        <time dateTime={at}>{clock(at)}</time>
        {onMention ? (
          <button type="button" className={agent} title={event.actor} aria-label={`Message ${event.actor}`} onClick={() => onMention(event)}>
            {event.actor}
          </button>
        ) : (
          <b className={agent} title={event.actor}>
            {event.actor}
          </b>
        )}
      </div>
      <div className="turn-h">
        {event.rooms?.length ? (
          <span className="console-rooms">
            [
            <span className="console-name" title={event.rooms.join(", ")}>
              {event.rooms.join(", ")}
            </span>
            ]
          </span>
        ) : null}
        <span>{view.label}</span>
        {/* A tool step's own line says where it acted and how it went (`ToolStep`). */}
        {!view.tool && "source" in view && view.source ? <span className="mono turn-src">{view.source}</span> : null}
        {!view.tool && "meta" in view && view.meta ? <span className="turn-meta">{view.meta}</span> : null}
      </div>
      <RecordBody view={view} kinds={kinds} />
    </li>
  );
});

/** Live, or a window from and to local times (either may be open), its boxes starting from `range`'s own. */
function RangeControls({ range, onRange }: { range: Range; onRange: (range: Range) => void }) {
  const [since, setSince] = useState(range.live ? "" : localOf(range.since));
  const [until, setUntil] = useState(range.live ? "" : localOf(range.until));
  const [unset, setUnset] = useState(false);
  return (
    <div className="console-range" role="group" aria-label="Range">
      <button
        type="button"
        className="pill"
        aria-pressed={range.live}
        onClick={() => {
          setUnset(false);
          if (!range.live) onRange({ live: true });
        }}
      >
        Live
      </button>
      <input type="datetime-local" aria-label="From" value={since} onChange={(event) => setSince(event.currentTarget.value)} />
      <input type="datetime-local" aria-label="To" value={until} onChange={(event) => setUntil(event.currentTarget.value)} />
      <button
        type="button"
        className="btn"
        onClick={() => {
          setUnset(!since && !until);
          if (since || until) onRange({ live: false, since: isoOf(since), until: isoOf(until) });
        }}
      >
        Apply
      </button>
      {unset ? <span role="status">Set a from or to time first.</span> : null}
    </div>
  );
}

/** Where a line goes: its text, and each target with the idempotency key it goes under. */
type Route = { readonly text: string; readonly targets: readonly { readonly target: MessageTarget; readonly key: string }[] };

/** `line` read against the events `shown` and the To targets (`parseLine`), each under a fresh key; a line it cannot send throws why. */
function routeOf(line: string, shown: readonly FeedEvent[], to: readonly MessageTarget[]): Route {
  const parsed = parseLine(line, shown, to);
  if ("problem" in parsed) throw new Error(parsed.problem);
  return { text: parsed.text, targets: parsed.targets.map((target) => ({ target, key: newUuid() })) };
}

/** Each CLI among `agents`, with its records, the busiest first. */
function clis(agents: readonly FeedActorFacet[]): { name: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const { cli, count } of agents) if (cli) counts.set(cli, (counts.get(cli) ?? 0) + count);
  return [...counts].map(([name, count]) => ({ name, count })).toSorted((a, b) => b.count - a.count);
}

function sum(agents: readonly FeedActorFacet[], field: "count" | "conversation"): number {
  return agents.reduce((total, agent) => total + agent[field], 0);
}

/** A colour for `actor`, the same on every visit: one of eight, from its name. */
function hue(actor: string): number {
  let hash = 0;
  for (const char of actor) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return hash % 8;
}

/** A `datetime-local` value, a local time, as ISO; null when empty. */
function isoOf(local: string): string | null {
  return local ? new Date(local).toISOString() : null;
}

/** An ISO time as a `datetime-local` value, to the minute; `""` for none. */
function localOf(iso: string | null): string {
  if (!iso) return "";
  const time = new Date(iso);
  return new Date(time.getTime() - time.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

function noSubscription(): () => void {
  return () => {};
}

const EMPTY_FEED: FeedState = { held: [], loading: false, error: null, more: false };

function emptyFeed(): FeedState {
  return EMPTY_FEED;
}

const CLOCK = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
