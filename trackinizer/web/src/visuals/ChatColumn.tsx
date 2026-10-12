import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type CSSProperties, type KeyboardEvent, type PointerEvent, type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { cycleStation, newStation, openStation, setStationMenu, stationsQuery } from "../stations/actions";
import { usePageSync } from "../stations/usePageSync";
import { useStation } from "../stations/useStation";
import { useCommands } from "../commands/registry";
import { Chat } from "./Chat";
import { dropLegacySizes, fitWidth, heldWidth, MIN_CHAT, readLegacySizes } from "./canvasSizes";
import { flipChatSide, setChatFullscreen, setChatHidden, toggleChatFullscreen, toggleChatHidden, useChatColumn } from "./chatColumn";
import { useHoverOpen } from "./hoverOpen";
import { useLayout } from "./stationLayout";
import "./chatColumn.css";

/** A press on the divider: the column it resizes, where the row stood then, and the width it has now. */
type Drag = {
  readonly pointerId: number;
  readonly column: HTMLElement;
  readonly row: DOMRect;
  width: number;
};

/**
 * Chat as a column of its own beside the workbench (`children`: the bars, the
 * canvas and the page), on the side the user chose. A divider between them
 * resizes it by pointer and by arrow keys; hidden, it leaves a strip at its
 * edge that shows it again, or opens it over the workbench while the pointer
 * rests on the strip; full screen, it takes the whole row and the
 * workbench stays mounted, hidden. On a phone it stands under the page.
 *
 * Chat stays mounted through all of it, so its draft, scroll and stream are
 * kept. It owns the column's commands, which exist only where Chat does.
 */
export function ChatColumn({ children }: { readonly children: ReactNode }) {
  const { side, hidden, fullscreen } = useChatColumn();
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
    if (chat && layout.chat_ch === undefined) changeLayout({ chat_ch: Math.round((chat / unit.ch) * 100) / 100 }, { now: true });
    dropLegacySizes("chat");
  }, [workspace?.id, unit.ch > 0]);
  // The width the stylesheet gave the column while the user has set none, for the divider to tell.
  const [drawn, setDrawn] = useState(0);
  useLayoutEffect(() => setDrawn(column.current?.offsetWidth ?? 0), [width, side, hidden, fullscreen]);

  // Hidden, Chat opens over the page while the pointer rests on its strip or the
  // keyboard is inside it, and folds a moment after both have left.
  const peek = useHoverOpen(column);
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
    drag.current = { pointerId: event.pointerId, column: element, row: row.current.getBoundingClientRect(), width: element.offsetWidth };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  }

  /** Each move only sets the column's width; the page takes it once, on release. */
  function updateResize(event: PointerEvent<HTMLElement>) {
    const resize = drag.current;
    if (!resize || resize.pointerId !== event.pointerId) return;
    const dragged = side === "left" ? event.clientX - resize.row.left : resize.row.right - event.clientX;
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

  const strip = hidden && !fullscreen && (
    <button key="strip" className="chat-strip" type="button" aria-label="Show Chat"
      title="Show Chat" onClick={() => setChatHidden(false)} {...peek.handlers} />
  );
  const shown = !hidden || fullscreen || peeking;
  const parts = [
    <div key="column" ref={column} className={`chat-column${peeking ? " chat-column-peek" : ""}`} hidden={!shown}
      {...(hidden ? peek.handlers : {})}
      style={width ? { "--chat-width": `${width}px` } as CSSProperties : undefined}
      onKeyDown={(event) => { if (event.key === "Escape" && fullscreen && !event.defaultPrevented) setChatFullscreen(false); }}>
      <Chat workspace={workspace} />
    </div>,
    shown && !fullscreen && !peeking && (
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
      <div className="chat-workbench" hidden={fullscreen}>{children}</div>
      {side === "right" ? [...parts].reverse() : null}
    </div>
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
