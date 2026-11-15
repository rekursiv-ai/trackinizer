import { type CSSProperties, type PointerEvent as ReactPointerEvent, type ReactNode, type KeyboardEvent as ReactKeyboardEvent, Suspense, useId, useRef, useState } from "react";
import { CommandRegistry, CommandRegistryContext } from "../commands/registry";
import { formatRoute } from "../router/route";
import { DetailView } from "../router/views";
import { Icon } from "./icons";
import { type Panel, type PanelSpec, PanelStrip, PanelToggle } from "./panel";
import "./peek.css";

/** Peek as a panel: on the right of a list or the graph, and `]` collapses or expands it. */
export const PEEK: PanelSpec = { id: "peek", name: "Peek", side: "right", keys: ["]"] };

/** Where the last drag left Peek's width, kept for the next tab. */
const WIDTH_KEY = "trackinizer.peek.width";

/** The narrowest Peek: the design's own `#insp`, below which its rows ellipsise. */
const MIN_WIDTH = 340;

/** The widest worth offering: past it the panel is the page, and the view behind it is gone. */
const MAX_WIDTH = 920;

/** How far an arrow key on the edge moves it. */
const KEY_STEP = 24;

function storedWidth(): number | null {
  const raw = Number(window.localStorage.getItem(WIDTH_KEY));
  return Number.isFinite(raw) && raw >= MIN_WIDTH && raw <= MAX_WIDTH ? raw : null;
}

const clampWidth = (px: number) => Math.round(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, px)));

/**
 * An inquiry's detail in a panel beside a list or the graph, with its ref and
 * buttons that collapse and close it; the caller's Escape closes it. Collapsed
 * (`panel`), a strip that keeps the ref and the close button takes its place, and
 * the detail stays mounted, hidden, so an edit begun in it is there on expanding.
 *
 * `body` replaces the detail with the caller's own read of the record — the
 * graph's panel, which is one view of the record, not the detail with insights
 * stacked on it. Given a body, the caller owns the commands too: `actions` and
 * the Open link move out of the bar and into `foot`, its own row at the panel's
 * foot. `crumbs` joins the bar either way: the graph puts the thread's name there.
 *
 * Its left edge is a handle: drag it, or arrow it from the keyboard, to trade
 * panel for the view behind it. One width serves every Peek and the tab keeps it;
 * `onResizeEnd` lets a caller re-frame what it draws behind the panel.
 */
export function Peek({
  row,
  panel,
  onClose,
  crumbs = null,
  actions = null,
  body = null,
  foot = null,
  onResizeEnd = null,
}: {
  row: { readonly kind: string; readonly seq: number };
  panel: Panel;
  onClose: () => void;
  crumbs?: ReactNode;
  actions?: ReactNode;
  body?: ReactNode;
  foot?: ReactNode;
  onResizeEnd?: (() => void) | null;
}) {
  const ref = { name: "ref", kind: row.kind, seq: row.seq } as const;
  const id = useId();
  const [width, setWidth] = useState<number | null>(storedWidth);
  const aside = useRef<HTMLElement | null>(null);
  /** The drag in flight: where the pointer started and the width then; null at rest. */
  const drag = useRef<{ x: number; w: number } | null>(null);
  // The panel is anchored to its host's right edge, so what is left of that edge
  // bounds the drag: a panel wider than its host hangs off the page.
  const room = () => {
    const host = aside.current?.parentElement?.getBoundingClientRect().width ?? window.innerWidth;
    return clampWidth(Math.min(MAX_WIDTH, host - 96));
  };
  const setSize = (px: number) => {
    const next = clampWidth(Math.min(px, room()));
    setWidth(next);
    window.localStorage.setItem(WIDTH_KEY, String(next));
  };
  const startDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { x: event.clientX, w: aside.current?.getBoundingClientRect().width ?? MIN_WIDTH };
  };
  const moveDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (drag.current === null) return;
    setSize(drag.current.w + (drag.current.x - event.clientX));
  };
  const endDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (drag.current === null) return;
    drag.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
    onResizeEnd?.();
  };
  const keyDrag = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    // Left pulls the edge with it, which is a wider panel; Home gives it back.
    if (event.key === "ArrowLeft") setSize((width ?? MIN_WIDTH) + KEY_STEP);
    else if (event.key === "ArrowRight") setSize((width ?? MIN_WIDTH) - KEY_STEP);
    else if (event.key === "Home") setSize(MIN_WIDTH);
    else return;
    event.preventDefault();
  };
  // The detail's keys are a page's (its Escape goes back to the list), and being
  // mounted last they would win over the caller's own Escape, which closes the peek.
  // A registry of its own, bound to no keys, keeps the peek from acting as a page.
  const [detailCommands] = useState(() => new CommandRegistry());
  const close = (
    <button type="button" className="icon-btn" onClick={onClose} aria-label="Close peek">
      <Icon name="x" />
    </button>
  );
  const collapsed = panel.collapsed;
  return (
    <>
      {collapsed ? (
        <PanelStrip panel={panel} id={id} label="Peek" className="peek-strip">
          {close}
          <span className="panel-strip-text mono">
            {row.kind}#{row.seq}
          </span>
        </PanelStrip>
      ) : null}
      {/* Collapsed, the strip takes its id; it drops `.peek`, whose display: flex
          would show it and whose rules (graph.css, live.css) place an open Peek;
          and its bar goes, as two toggles would share the one `Panel.button`. */}
      <aside
        ref={aside}
        id={collapsed ? undefined : id}
        className={collapsed ? undefined : "peek"}
        style={{ "--peek-w": width === null ? undefined : `${width}px` } as CSSProperties}
        aria-label="Peek"
        hidden={collapsed}
      >
        {collapsed ? null : (
          <>
            {/* The edge itself, not a bar drawn beside it: it takes the pointer
                before the canvas under it does, and the arrows move it by a step. */}
            <div
              className="peek-edge"
              role="separator"
              aria-orientation="vertical"
              aria-label="Resize Peek"
              tabIndex={0}
              aria-valuenow={width ?? undefined}
              aria-valuemin={MIN_WIDTH}
              aria-valuemax={MAX_WIDTH}
              onPointerDown={startDrag}
              onPointerMove={moveDrag}
              onPointerUp={endDrag}
              onPointerCancel={endDrag}
              onKeyDown={keyDrag}
            />
            <div className="peek-bar">
              {crumbs}
              <span className="mono">
                {row.kind}#{row.seq}
              </span>
              <span className="spacer" />
              {body === null ? (
                <>
                  {actions}
                  <a className="btn ghost" href={formatRoute(ref)}>
                    Open
                  </a>
                </>
              ) : null}
              <PanelToggle panel={panel} controls={id} />
              {close}
            </div>
          </>
        )}
        <CommandRegistryContext value={detailCommands}>
          {body === null ? (
            // The detail's code is a chunk of its own (src/router/views.ts); the peek's bar shows while it loads.
            <Suspense>
              <DetailView target={{ kind: row.kind, seq: row.seq }} />
            </Suspense>
          ) : (
            body
          )}
          {foot === null ? null : <div className="peek-foot">{foot}</div>}
        </CommandRegistryContext>
      </aside>
    </>
  );
}
