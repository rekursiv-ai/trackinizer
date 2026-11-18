// PLOTLY: interactive scientific figure (scatter, heatmap, treemap, embedding, 3D).
import { useEffect, useRef } from "react";
// The full lib entry, not `lib/core`: core registers only a handful of trace types, and
// this node's whole claim is the long tail (heatmap, treemap, sunburst, 3D).
import Plotly from "plotly.js/lib/index";

export type PlotlyTrace = {
  // marker.line is the only nested type; brief closure needs explicit naming

  readonly type?: string;
  readonly x?: readonly number[];
  readonly y?: readonly number[];
  readonly z?: readonly number[];
  readonly mode?: string;
  readonly name?: string;
  readonly marker?: { readonly size?: number; readonly color?: string; readonly colorscale?: string; readonly showscale?: boolean };
  readonly text?: readonly string[];
  readonly labels?: readonly string[];
  readonly parents?: readonly string[];
  readonly values?: readonly number[];
  readonly line?: { readonly color?: string };
  readonly fill?: string;
};

export type PlotlyData = readonly PlotlyTrace[];
export type PlotlyLayout = {
  readonly xaxis?: { readonly title?: string; readonly range?: readonly [number, number] };
  readonly yaxis?: { readonly title?: string; readonly range?: readonly [number, number] };
  readonly zaxis?: { readonly title?: string };
  readonly title?: string;
  readonly showlegend?: boolean;
};

export function PlotlyView({ data, layout, label }: {
  readonly data: PlotlyData;
  readonly layout?: PlotlyLayout;
  readonly label?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    Plotly.newPlot(ref.current, data as any, {
      ...layout,
      paper_bgcolor: "transparent",
      font: { color: "currentColor", family: "JetBrains Mono, monospace" },
      margin: { t: 40, r: 20, b: 40, l: 60 },
      plot_bgcolor: "transparent",
    } as any, { responsive: true, displayModeBar: true });
  }, [data, layout]);

  return (
    <div className="ui-plotly" role="img" aria-label={label ?? "plotly figure"}>
      <div ref={ref} style={{ width: "100%", height: "100%" }} />
    </div>
  );
}
