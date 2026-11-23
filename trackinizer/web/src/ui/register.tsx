// THE REGISTER NODE. A strip or a list longer than its lane, mounted a window at a time.
//
// This is the node the law's strip rule is about: `the replay · 6 arms` is four cards
// at six arms and forty at forty, and the mockup's row has no answer for the fortieth.
// A strip scrolls on its own axis, never wraps into a second row, and never drops its
// tail — so the axis is declared and the virtualizer owns the measure. Forty commits
// cost five mounted cards and one spacer.
import { type ReactNode, useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import "./nodes.css";

/** A strip of items along one axis, or a list down the page. */
export type Register = {
  /** How many items there are in total — not how many are mounted. */
  readonly count: number;
  /** The axis it runs on. A strip is `x`; a list down a pane is `y`. */
  readonly axis: "x" | "y";
  /** A strip's own height, in pixels. Every item in it is positioned absolutely —
   *  there is no content for the lane to measure a height from — so an `x` register
   *  with no height collapses to zero and its items exist in the DOM but draw nothing.
   *  Ignored on `y`, where the rail's own height is the list's total length. */
  readonly height?: number;
  /** One item's extent along that axis, in pixels: a card's width, a row's height.
   *  The measure the virtualizer reserves, so the scrollbar is right before anything
   *  past the window has rendered. */
  readonly extent: number;
  /** The gap between two items, which the extent above does not include. */
  readonly gap?: number;
  /** Item `at`, drawn only while it is within reach of the window. */
  readonly item: (at: number) => ReactNode;
  /** What the strip is, for a screen reader. */
  readonly label: string;
};

/**
 * One register.
 *
 * `overscan` is three: a reader flicking a strip sideways outruns a render, and three
 * items either side is what keeps the edge of the window from arriving blank without
 * mounting a second screenful.
 */
export function RegisterView({ count, axis, extent, gap = 0, item, label, height }: Register) {
  const scroller = useRef<HTMLDivElement>(null);
  const horizontal = axis === "x";
  const virtual = useVirtualizer({
    count,
    horizontal,
    getScrollElement: () => scroller.current,
    estimateSize: () => extent + gap,
    overscan: 3,
  });

  return (
    <div
      className={`ui-reg${horizontal ? " ui-reg-x" : ""}`}
      ref={scroller}
      role="list"
      aria-label={label}
      style={horizontal && height !== undefined ? { height: `${height}px` } : undefined}
    >
      {/* The rail carries the whole measure, so the scrollbar describes all `count`
          items and not the handful that are mounted. */}
      <div
        className="ui-reg-rail"
        style={horizontal
          ? { width: `${virtual.getTotalSize()}px`, height: "100%" }
          : { height: `${virtual.getTotalSize()}px` }}
      >
        {virtual.getVirtualItems().map((row) => (
          <div
            className={horizontal ? "ui-reg-col" : "ui-reg-row"}
            key={row.key}
            role="listitem"
            // The item is placed by transform rather than by a margin, because a gap
            // the virtualizer did not measure leaves the last item short of the end.
            style={horizontal
              ? { width: `${extent}px`, transform: `translateX(${row.start}px)` }
              : { transform: `translateY(${row.start}px)` }}
          >
            {item(row.index)}
          </div>
        ))}
      </div>
    </div>
  );
}
