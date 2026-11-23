// THE SERIES NODE. A metric curve over steps, drawn by uPlot.
//
// Vega draws a figure; uPlot draws a long one. An Experiment's metric series is tens
// of thousands of points and arrives already ordered, which is the one case where a
// canvas beats SVG by enough to carry a second library — the rest of the catalog's
// figures are Chart's.
import { useEffect, useRef } from "react";
import { seriesAxes, themeInk } from "./theme";
import "./nodes.css";

/** One curve: its name, and the value at each step. */
export type Curve = {
  /** The metric as the record keys it: `grad_norm`, `loss`. */
  readonly key: string;
  /** The steps, ascending — the x of every point. */
  readonly steps: readonly number[];
  /** The value at each step, the same length as `steps`. */
  readonly values: readonly number[];
};

/** A metric series over steps. */
export type Series = {
  readonly curves: readonly Curve[];
  /** The x axis's unit, as the record counts it: `step`. */
  readonly unit?: string;
  readonly height?: number;
};

/**
 * One metric series.
 *
 * The width follows the element, re-measured on resize, because a floating window is
 * resized by the reader and a canvas sized once is a canvas that is wrong afterwards.
 */
export function SeriesView({ curves, unit = "step", height = 180 }: Series) {
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const drawn = curves.filter((curve) => curve.steps.length === curve.values.length && curve.steps.length > 0);
    if (drawn.length === 0 || !host.current) return;
    const element = host.current;
    let live = true;
    let chart: { setSize: (size: { width: number; height: number }) => void; destroy: () => void } | undefined;
    let observer: ResizeObserver | undefined;

    void (async () => {
      const [uPlot] = await Promise.all([import("uplot"), import("uplot/dist/uPlot.min.css")]);
      if (!live) return;
      const ink = themeInk();
      // uPlot takes one x and one y per row, so every curve is aligned on the first
      // one's steps. A curve on its own steps is its own series node, not a second
      // line here, because two x axes in one frame is a figure nobody can read.
      const data = [drawn[0]!.steps, ...drawn.map((curve) => curve.values)];
      chart = new uPlot.default(
        {
          width: element.clientWidth || 400,
          height,
          // The Section above already named the figure.
          title: undefined,
          cursor: { y: false },
          legend: { live: true },
          axes: [...seriesAxes(ink)] as never,
          scales: { x: { time: false } },
          series: [
            { label: unit },
            ...drawn.map((curve, at) => ({
              label: curve.key,
              // The ramp separates curves, which is what a hue is for here. One curve
              // has nothing to separate from, so it takes the reading ink.
              stroke: drawn.length > 1 ? ink.groups[at % ink.groups.length] : ink.text,
              width: 1.6,
              points: { show: false },
            })),
          ],
        } as never,
        data as never,
        element,
      );
      observer = new ResizeObserver(() => { chart?.setSize({ width: element.clientWidth, height }); });
      observer.observe(element);
    })();

    return () => {
      live = false;
      observer?.disconnect();
      chart?.destroy();
    };
  }, [curves, unit, height]);

  return <div className="ui-series" ref={host} />;
}
