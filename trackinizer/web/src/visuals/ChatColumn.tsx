import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type CSSProperties, type KeyboardEvent, type PointerEvent, type ReactNode, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { cycleStation, newStation, openStation, setStationMenu, stationsQuery } from "../stations/actions";
import { usePageSync } from "../stations/usePageSync";
import { useStation } from "../stations/useStation";
import { useCommands } from "../commands/registry";
import { Icon } from "../ui/icons";
import { Chat } from "./Chat";
import { dropLegacySizes, fitWidth, heldWidth, MIN_CHAT, readLegacySizes } from "./canvasSizes";
import { type ChatSide, flipChatSide, setChatFullscreen, setChatHidden, toggleChatFullscreen, toggleChatHidden, useChatColumn } from "./chatColumnState";
import { useHoverOpen } from "./hoverOpen";
import { useLayout } from "./stationLayout";
import "./chatColumn.css";

/**
 * A press on the divider: the column it resizes, where the row stood then, the
 * width it has now, and how far into the divider the press landed, so the
 * column keeps its width until the pointer moves.
 */
type Drag = {
  readonly pointerId: number;
  readonly column: HTMLElement;
  readonly row: DOMRect;
  readonly grab: number;
  width: number;
};

/**
 * Chat as a column of its own beside the workbench (`children`: the bars, the
 * canvas and the page), on the side the user chose. The app's sidebar rail
 * (`rail`) stands at the workbench's left edge, inside it: right of Chat when
 * Chat is at the left, leftmost when it is at the right. A `sheet` (Settings,
 * Admin) opens over the page from the rail's edge, or over the row where the
 * workbench is hidden. A divider between Chat and the workbench
 * resizes it by pointer and by arrow keys; hidden, it leaves a strip at its
 * edge that shows it again, or opens it over the workbench while the pointer
 * rests on the strip, and a click in it pins it there; full screen, it takes the whole row and the
 * workbench stays mounted, hidden. On a phone Chat is the whole app, shown
 * even if hidden elsewhere, and the workbench stays mounted, hidden.
 *
 * Chat stays mounted through all of it, so its draft, scroll and stream are
 * kept. It owns the column's commands, which exist only where Chat does.
 */
export function ChatColumn({
  children,
  rail = null,
  sheet = null,
}: {
  readonly children: ReactNode;
  readonly rail?: ReactNode;
  readonly sheet?: ReactNode;
}) {
  const { side, hidden, fullscreen } = useChatColumn();
  const phone = usePhone();
  const queryClient = useQueryClient();
  const workspace = useStation().data ?? null;
  const stations = useQuery(stationsQuery).data ?? [];
  usePageSync();
  // The tab is titled by its station, as a terminal's is by its directory.
  useEffect(() => {
    if (workspace) document.title = `${workspace.name} · Trackinizer`;
  }, [workspace?.name]);
  const row = useRef<HTMLDivElement>(null);
  const column = useRef<HTMLDivElement>(null);
  const drag = useRef<Drag | null>(null);
  // Chat's width is kept in `ch`, the width of a character of its own font (a
  // terminal Chat is 80 columns), and fitted to this row: never under the least
  // Chat needs, never over seven tenths of the row.
  const [layout, changeLayout] = useLayout();
  const [unit, setUnit] = useState({ ch: 0, row: 0 });
  const width = layout.chat_ch && unit.ch ? fitWidth(layout.chat_ch * unit.ch, unit.row, MIN_CHAT) : undefined;
  useLayoutEffect(() => {
    const measure = () => {
      const ch = chWidth(row.current, column.current);
      const rowWidth = row.current?.clientWidth ?? 0;
      setUnit((previous) => (previous.ch === ch && previous.row === rowWidth ? previous : { ch, row: rowWidth }));
    };
    measure();
    addEventListener("resize", measure);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    if (row.current) observer?.observe(row.current);
    return () => {
      removeEventListener("resize", measure);
      observer?.disconnect();
    };
  }, []);
  // A browser that kept Chat's width in pixels before the station did: the first
  // station it shows takes it, unless it already holds one, and the browser forgets it.
  useEffect(() => {
    if (!workspace || !unit.ch) return;
    const { chat } = readLegacySizes();
    if (chat && workspace.layout?.chat_ch == null) {
      changeLayout({ chat_ch: Math.round((chat / unit.ch) * 100) / 100 }, { now: true, onSaved: () => dropLegacySizes("chat") });
    } else dropLegacySizes("chat");
  }, [workspace?.id, unit.ch > 0]);
  // The width the stylesheet gave the column while the user has set none, for the divider to tell.
  const [drawn, setDrawn] = useState(0);
  useLayoutEffect(() => setDrawn(column.current?.offsetWidth ?? 0), [width, side, hidden, fullscreen]);

  // Hidden, Chat opens over the page while the pointer rests on its strip or the
  // keyboard is inside it, and folds a moment after both have left.
  const peek = useHoverOpen(column, { enabled: hidden });
  const peeking = hidden && !fullscreen && peek.open;
  useLayoutEffect(() => {
    if (hidden) peek.set(false);
    // `peek.set` is a new function each render; only entering the hidden state resets it.
  }, [hidden]);

  useCommands([
    { id: "chat.toggle", title: hidden ? "Show Chat" : "Hide Chat", keys: ["$mod+\\"], section: "Chat", global: true, run: toggleChatHidden },
    { id: "chat.fullscreen", title: fullscreen ? "Leave full screen Chat" : "Chat full screen", keys: ["Alt+Enter"], section: "Chat", global: true, run: toggleChatFullscreen },
    { id: "chat.side", title: "Move Chat to the other side", section: "Chat", run: flipChatSide },
    { id: "station.next", title: "Next renamed station", keys: ["g ]"], section: "Stations", run: () => void cycleStation(queryClient, 1) },
    { id: "station.previous", title: "Previous renamed station", keys: ["g ["], section: "Stations", run: () => void cycleStation(queryClient, -1) },
    { id: "station.search", title: "Search stations", keys: ["g s"], section: "Stations", run: () => { setChatHidden(false); setStationMenu(true); } },
    { id: "station.new", title: "New station", section: "Stations", run: () => void newStation(queryClient) },
    ...stations.map((each) => ({ id: `station.open.${each.id}`, title: `Open station ${each.name}`, section: "Stations", run: () => void openStation(queryClient, each.id) })),
    ...(fullscreen ? [{ id: "chat.fullscreen.exit", title: "Leave full screen Chat", keys: ["Escape"], run: () => setChatFullscreen(false) }] : []),
  ]);

  function keep(next: number) {
    if (unit.ch) changeLayout({ chat_ch: Math.round((next / unit.ch) * 100) / 100 });
  }

  function startResize(event: PointerEvent<HTMLElement>) {
    const element = column.current;
    if (!row.current || !element || event.button !== 0) return;
    const bounds = row.current.getBoundingClientRect();
    const grab = reach(event.clientX, { side, row: bounds }) - element.offsetWidth;
    drag.current = { pointerId: event.pointerId, column: element, row: bounds, grab, width: element.offsetWidth };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  }

  /** Each move only sets the column's width; the page takes it once, on release. */
  function updateResize(event: PointerEvent<HTMLElement>) {
    const resize = drag.current;
    if (!resize || resize.pointerId !== event.pointerId) return;
    const dragged = reach(event.clientX, { side, row: resize.row }) - resize.grab;
    resize.width = heldWidth(dragged, resize.row.width, MIN_CHAT);
    resize.column.style.setProperty("--chat-width", `${resize.width}px`);
  }

  function stopResize(event: PointerEvent<HTMLElement>) {
    const resize = drag.current;
    if (resize?.pointerId !== event.pointerId) return;
    drag.current = null;
    keep(resize.width);
  }

  /** The arrow keys move the divider 16 px, 48 with Shift, toward the side the arrow points. */
  function resizeWithKeyboard(event: KeyboardEvent<HTMLElement>) {
    const toward = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
    if (!toward || !row.current || !column.current) return;
    event.preventDefault();
    const wider = side === "left" ? toward : -toward;
    keep(heldWidth(column.current.offsetWidth + wider * (event.shiftKey ? 48 : 16), row.current.clientWidth, MIN_CHAT));
  }

  const strip = hidden && !fullscreen && !phone && (
    <button key="strip" className="chat-strip" type="button" aria-label="Show Chat"
      title="Show Chat" onClick={() => setChatHidden(false)} {...peek.handlers}>
      <Icon name="chat" />
    </button>
  );
  const shown = !hidden || fullscreen || peeking || phone;
  const parts = [
    <div key="column" ref={column} className={`chat-column${peeking ? " chat-column-peek" : ""}`} hidden={!shown}
      style={width ? { "--chat-width": `${width}px` } as CSSProperties : undefined}
      // A click anywhere in the opened Chat pins it, where it was opened. The pin waits for the click to bubble up
      // here, so what was clicked answers it first: React renders between the capture and the bubble, and a pin in
      // the capture would turn the Pin button under the pointer into Hide before its own click came.
      onClick={peeking ? () => setChatHidden(false) : undefined}
      onKeyDown={(event) => { if (event.key === "Escape" && fullscreen && !event.defaultPrevented) setChatFullscreen(false); }}>
      <Chat workspace={workspace} />
    </div>,
    shown && !fullscreen && !peeking && !phone && (
      <div key="edge" className="chat-column-edge" role="separator" aria-orientation="vertical" tabIndex={0}
        aria-label="Resize Chat" aria-valuemin={MIN_CHAT} aria-valuenow={width ?? drawn}
        title="Drag to resize; use the arrow keys to resize"
        onPointerDown={startResize} onPointerMove={updateResize} onPointerUp={stopResize} onPointerCancel={stopResize}
        onKeyDown={resizeWithKeyboard} />
    ),
    strip,
  ];
  return (
    <div ref={row} className={`chat-row chat-row-${side}${fullscreen ? " chat-row-fullscreen" : ""}`}>
      {side === "left" ? parts : null}
      <div className="chat-workbench" hidden={fullscreen || phone}>
        {rail}
        <div className="chat-stage">
          {children}
          {fullscreen || phone ? null : sheet}
        </div>
      </div>
      {side === "right" ? [...parts].reverse() : null}
      {fullscreen || phone ? sheet : null}
    </div>
  );
}

/** How far `x` lies from the row's edge on Chat's side: the width a column ending at `x` would have. */
function reach(x: number, { side, row }: { readonly side: ChatSide; readonly row: DOMRect }): number {
  return side === "left" ? x - row.left : row.right - x;
}

/** A phone's width: the stylesheet's breakpoint for the sidebar's drawer, where Chat is the whole app. */
const PHONE = "(max-width: 760px)";

/** Whether the window is a phone's width, followed as it changes; never where there is no `matchMedia`, as in a test. */
function usePhone(): boolean {
  return useSyncExternalStore(
    (changed) => {
      if (typeof matchMedia !== "function") return () => {};
      const query = matchMedia(PHONE);
      query.addEventListener("change", changed);
      return () => query.removeEventListener("change", changed);
    },
    () => typeof matchMedia === "function" && matchMedia(PHONE).matches,
  );
}

/** The width of one character in Chat's font, in pixels; a stand-in where nothing is laid out, as in a test. */
const FALLBACK_CH = 8;

/**
 * The width of one `0` in the column's font. The probe stands in the row, which
 * is always shown, with the column's font size: a hidden column has no width,
 * but its font size is still computed.
 */
function chWidth(row: HTMLElement | null, column: HTMLElement | null): number {
  if (!row || !column) return 0;
  const probe = document.createElement("div");
  probe.style.cssText = `position:absolute;visibility:hidden;width:100ch;height:0;padding:0;border:0;font-size:${getComputedStyle(column).fontSize}`;
  row.append(probe);
  const ch = probe.offsetWidth / 100;
  probe.remove();
  return ch || FALLBACK_CH;
}
