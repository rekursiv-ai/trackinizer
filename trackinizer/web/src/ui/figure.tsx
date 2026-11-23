// THE FIGURE: whichever catalog node the answer asked for, in one place.
//
// A block's formation is where a figure belongs — a card is a button, and a chart
// inside a button is a control a reader cannot use. What opens when a block is clicked
// has the room and the licence: the claim, the records behind it, and the figure those
// records are a measurement of.
//
// This is the one dispatch in the app that turns the agent's pick into a node, so it
// is also the one place the catalog is enforced at runtime: a `kind` nothing here
// handles prints as a refusal naming it, because an answer that silently loses a
// figure is an answer whose author believes it shipped.
import { lazy, Suspense } from "react";
import type { Chart } from "./chart";
import type { Equation } from "./equation";
import type { Series } from "./series";
import type { NumTable } from "./table";
import { NumTableView } from "./table";
import type { PlotlyData, PlotlyLayout } from "./plotly";
import type { RechartsData, RechartsType } from "./recharts";

// Each node's library loads with the node and never before: the first-load budget
// (scripts/bundle-size.ts, 250 KB gzip) is what holds this, and a static import here
// would put Vega and KaTeX on the first paint of a page that draws neither.
const ChartView = lazy(async () => ({ default: (await import("./chart")).ChartView }));
const EquationView = lazy(async () => ({ default: (await import("./equation")).EquationView }));
const SeriesView = lazy(async () => ({ default: (await import("./series")).SeriesView }));
const PlotlyView = lazy(async () => ({ default: (await import("./plotly")).PlotlyView }));
const RechartsView = lazy(async () => ({ default: (await import("./recharts")).RechartsView }));

/** A figure an answer places: the catalog node, and what that node needs. */
export type Figure =
  | ({ readonly kind: "chart" } & Chart)
  | ({ readonly kind: "series" } & Series)
  | ({ readonly kind: "equation" } & Equation)
  | { readonly kind: "table"; readonly table: NumTable; readonly derive?: boolean }
  /** Interactive scientific figure (scatter, heatmap, treemap, sunburst, embedding, 3D) for scientists who need zoom/pan/hover and 40+ Plotly chart types. */
  | { readonly kind: "plotly"; readonly data: PlotlyData; readonly layout?: PlotlyLayout; readonly label?: string }
  /** Rich interactive chart (Area, Bar, Line, Radar, Radial, Pie) for dense multi-axis comparison when Vega-Lite is not enough. */
  | { readonly kind: "recharts"; readonly type: RechartsType; readonly data: RechartsData; readonly label?: string }
  /** Multi-arm learning curve over steps with hover crosshair for comparing training runs. */
  | { readonly kind: "learningcurve"; readonly curves: Series["curves"]; readonly unit?: string; readonly height?: number }
  /** Confusion matrix, ROC, and PR curves as first-class evaluation figures. */
  | { readonly kind: "confusion"; readonly matrix: readonly (readonly number[])[]; readonly labels: readonly string[]; readonly title?: string }
  /** UMAP / t-SNE / PCA embedding scatter colored by cluster, with hover identity. */
  | { readonly kind: "embedding"; readonly points: readonly { readonly x: number; readonly y: number; readonly label: string; readonly cluster?: string }[]; readonly title?: string };

/**
 * One figure.
 *
 * A node still loading shows nothing rather than a spinner: these sit under a claim
 * that is already readable, and a placeholder where a figure will be is a second
 * thing for the eye to resolve on a pane whose point is the first.
 */
export function FigureView({ figure }: { readonly figure: Figure }) {
  switch (figure.kind) {
    case "table":
      return <NumTableView table={figure.table} derive={figure.derive ?? false} />;
    case "chart":
      return <Suspense fallback={null}><ChartView spec={figure.spec} label={figure.label} /></Suspense>;
    case "series":
      return <Suspense fallback={null}><SeriesView curves={figure.curves} unit={figure.unit} height={figure.height} /></Suspense>;
    case "equation":
      return <Suspense fallback={null}><EquationView tex={figure.tex} display={figure.display} /></Suspense>;
    case "plotly":
      return <Suspense fallback={null}><PlotlyView data={figure.data} layout={figure.layout} label={figure.label} /></Suspense>;
    case "recharts":
      return <Suspense fallback={null}><RechartsView type={figure.type} data={figure.data} label={figure.label} /></Suspense>;
    case "learningcurve":
      return <Suspense fallback={null}><SeriesView curves={figure.curves} unit={figure.unit} height={figure.height} /></Suspense>;
    case "confusion": {
      const max = Math.max(...figure.matrix.flat());
      return (
        <div className="ui-confusion">
          {figure.title ? <p className="ui-cap">{figure.title}</p> : null}
          <div className="ui-confusion-grid" role="img" aria-label="confusion matrix">
            {figure.matrix.map((row, i) => (
              <div key={i} className="ui-confusion-row">
                {row.map((v, j) => (
                  <div key={j} className="ui-confusion-cell" style={{ opacity: 0.2 + (v / (max || 1)) * 0.8 }} title={`${figure.labels[i]} → ${figure.labels[j]}: ${v}`}>
                    {v}
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>
      );
    }
    case "embedding": {
      const clusters = [...new Set(figure.points.map((p) => p.cluster ?? "all"))];
      const byCluster = clusters.map((c) => figure.points.filter((p) => (p.cluster ?? "all") === c));
      return (
        <Suspense fallback={null}>
          <PlotlyView
            data={byCluster.map((pts, i) => ({
              type: "scatter",
              x: pts.map((p) => p.x),
              y: pts.map((p) => p.y),
              mode: "markers",
              name: clusters[i],
              text: pts.map((p) => p.label),
              marker: { size: 8 + i },
            }))}
            layout={{ title: figure.title, showlegend: true }}
            label={figure.title}
          />
        </Suspense>
      );
    }
    default:
      // Exhaustive today; a node added to the catalog and not to this switch lands
      // here, named, rather than vanishing out of the answer.
      return <p className="ui-note">{`This figure was not drawn: ${(figure as { readonly kind: string }).kind} is not a node this app renders.`}</p>;
  }
}
