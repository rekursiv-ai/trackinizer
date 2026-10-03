import { useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  type ReactNode,
  type RefObject,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";
import { flushSync } from "react-dom";
import { type Tab, tabKinds } from "../activity/feed";
import type { InquiryRow } from "../api/inquiries";
import { takeReadAt } from "../app/prefetch";
import type { ListRequest } from "../query/query";
import { Bar, StreamStatusContext } from "../ui/bars";
import { Icon } from "../ui/icons";
import { ActivityLive } from "./activity";
import { DetailLive } from "./detail";
import { attachStream } from "./earlyStream";
import { LiveHub } from "./hub";
import { type ListSnapshot, ListLive } from "./list";
import "./live.css";
import { isPageKey } from "./rows";
import type { Failure } from "./serial";
import { watchRows } from "./visibility";


/** The live stream's hub, for the views that keep current; null outside a `LiveProvider`. */
export const LiveContext = createContext<LiveHub | null>(null);

/**
 * Keep the app's views current: take the stream main.tsx opened (or open one),
 * follow the tab's visibility and the user's input, and say whether live
 * updates are paused.
 */
export function LiveProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [hub] = useState(() => new LiveHub(queryClient));
  useEffect(() => {
    // Down until the stream first opens, so one that never does shows the paused bar too.
    hub.drop();
    const close = attachStream(hub);
    const onVisibility = () => (document.hidden ? hub.hide() : hub.show());
    const onInput = () => hub.input();
    onVisibility();
    document.addEventListener("visibilitychange", onVisibility);
    for (const type of INPUT_EVENTS) addEventListener(type, onInput, { capture: true, passive: true });
    return () => {
      close();
      document.removeEventListener("visibilitychange", onVisibility);
      for (const type of INPUT_EVENTS) removeEventListener(type, onInput, { capture: true });
      hub.stop();
    };
  }, [hub]);
  const status = useSyncExternalStore(hub.subscribeStatus, hub.status);
  return (
    <LiveContext value={hub}>
      <StreamStatusContext value={status}>{children}</StreamStatusContext>
    </LiveContext>
  );
}

/**
 * Keep the list `request` asks for, `pageSize` rows a page, current while it is
 * mounted; its rows render inside `scroller`. Returns its live state (null
 * outside a `LiveProvider`), for `LiveRows` in the scroller, and the rows it
 * `loaded` as the list shows them, with the rows merged in or away.
 */
export function useLiveList(
  request: ListRequest,
  pageSize: number,
  scroller: RefObject<HTMLElement | null>,
  loaded: readonly InquiryRow[],
): { live: ListLive | null; rows: readonly InquiryRow[] } {
  const hub = useContext(LiveContext);
  const queryClient = useQueryClient();
  const live = useMemo(
    () => (hub ? new ListLive(queryClient, request, pageSize) : null),
    [hub, queryClient, request, pageSize],
  );
  const snapshot = useSyncExternalStore(live?.subscribe ?? noSubscription, live?.getSnapshot ?? idle);
  const rows = useMemo(() => (live ? live.rows(loaded, snapshot) : loaded), [live, loaded, snapshot]);
  // Before paint, so a new row a page read brought in never shows beside a pill still counting it.
  useLayoutEffect(() => live?.settle(loaded), [live, loaded]);
  useEffect(() => {
    const root = scroller.current;
    if (!hub || !live || !root) return;
    // Its first page may be one main.tsx started reading before the stream opened.
    const registration = hub.register(live, { readAt: takeReadAt((key) => isPageKey(key, request.filters)) });
    const onScreen = watchRows(root, (entered) => registration.push(entered.filter((id) => live.isStale(id))));
    live.watch(onScreen);
    return () => {
      onScreen.dispose();
      registration.dispose();
      live.release();
    };
  }, [hub, live, scroller]);
  return { live, rows };
}

/** Keep the detail of inquiry `id` current while it is mounted. */
export function useLiveDetail(id: string): void {
  const hub = useContext(LiveContext);
  const queryClient = useQueryClient();
  useEffect(() => {
    if (!hub) return;
    // Its read may be one main.tsx started before the stream opened.
    const readAt = takeReadAt(([scope, detailId]) => scope === "detail" && detailId === id);
    return hub.register(new DetailLive(queryClient, id), { shareAs: `detail ${id}`, readAt }).dispose;
  }, [hub, queryClient, id]);
}

/** Keep the Activity feed's `tab` current while it is mounted; returns its live updates' failure, if they keep failing. */
export function useLiveActivity(tab: Tab): Failure | null {
  const hub = useContext(LiveContext);
  const queryClient = useQueryClient();
  const [failure, setFailure] = useState<Failure | null>(null);
  useEffect(() => {
    if (!hub) return;
    const live = new ActivityLive(queryClient, tabKinds(tab));
    const registration = hub.register({ update: (batch) => live.update(batch), failing: setFailure });
    return () => {
      registration.dispose();
      setFailure(null);
    };
  }, [hub, queryClient, tab]);
  return failure;
}

/** A bar saying live updates keep failing, so what shows may be stale, with Retry. */
export function LiveFailureBar({ failure }: { failure: Failure | null }) {
  if (!failure) return null;
  return (
    <Bar kind="stale">
      Live updates failed: {failure.error.message}
      <button type="button" className="btn ghost" onClick={failure.retry}>
        Retry
      </button>
    </Bar>
  );
}

/**
 * A live list's marks among its rows: the "N new" pill, and rows that no
 * longer match, dimmed.
 *
 * New rows join the list itself only when the user asks, with the pill, or
 * when the list is at its top, the pointer is not on its rows, and the user has
 * been idle for 2 s: rows never move under the pointer or the keyboard focus. The
 * pill also drops the dimmed rows and brings the user to the first new row:
 * the top, unless grouping or sorting placed it further down, in a group
 * `reveal` opens. Live updates that keep failing show a bar with Retry.
 */
export function LiveRows({
  live,
  scroller,
  reveal,
}: {
  live: ListLive | null;
  scroller: RefObject<HTMLElement | null>;
  reveal?: (rows: readonly InquiryRow[]) => void;
}) {
  const hub = useContext(LiveContext);
  const { arrivals, left, failure } = useSyncExternalStore(live?.subscribe ?? noSubscription, live?.getSnapshot ?? idle);
  useEffect(() => {
    if (!live || !hub || arrivals === 0) return;
    const mergeWhenIdle = () => {
      const root = scroller.current;
      // A pointer resting on the rows (a group of them) holds them as input
      // does, though it sends no events. One resting on the list's empty space,
      // as after picking a filter from a menu, holds nothing.
      const held = [...(root?.querySelectorAll("section") ?? [])].some((group) => group.matches(":hover"));
      if (root && root.scrollTop <= 0 && !held && hub.idleMs() >= IDLE_MS) live.merge(false);
    };
    mergeWhenIdle();
    const timer = setInterval(mergeWhenIdle, IDLE_POLL_MS);
    return () => clearInterval(timer);
  }, [live, hub, arrivals, scroller]);
  if (!live) return null;
  const showAll = () => {
    // Rendered at once, in open groups, so the new rows are there to scroll to.
    let joined = new Set<string>();
    flushSync(() => {
      const rows = live.merge(true);
      reveal?.(rows);
      joined = new Set(rows.map((row) => row.id));
    });
    const root = scroller.current;
    if (!root) return;
    const rows = [...root.querySelectorAll<HTMLElement>("[data-row]")];
    const first = rows.find((row) => joined.has(row.dataset.row ?? ""));
    if (first && first !== rows[0]) first.scrollIntoView({ block: "center" });
    else root.scrollTop = 0;
  };
  return (
    <>
      <LiveFailureBar failure={failure} />
      {left.size > 0 && <style>{dimmed(left)}</style>}
      {arrivals > 0 && (
        <div className="live-pill-slot">
          <button type="button" className="live-pill" onClick={showAll}>
            <Icon name="chevU" size={13} />
            {arrivals} new
          </button>
        </div>
      )}
    </>
  );
}

/** How long the user must be idle before new rows join a list at its top. */
const IDLE_MS = 2_000;
const IDLE_POLL_MS = 500;

const INPUT_EVENTS = ["pointerdown", "pointermove", "keydown", "wheel", "touchstart", "scroll"] as const;

const IDLE_SNAPSHOT: ListSnapshot = { arrivals: 0, left: new Set(), joined: new Map(), gone: new Set(), failure: null };

function idle(): ListSnapshot {
  return IDLE_SNAPSHOT;
}

function noSubscription(): () => void {
  return () => {};
}

/**
 * A style rule dimming the rows `ids`, by the list's `data-row` marks. Only
 * UUIDs make it into the rule, so no id can break out of the selector.
 */
function dimmed(ids: ReadonlySet<string>): string {
  const selectors = [...ids].filter((id) => UUID.test(id)).map((id) => `.row[data-row="${id}"]`);
  return selectors.length > 0 ? `${selectors.join(",")}{opacity:.45}` : "";
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
