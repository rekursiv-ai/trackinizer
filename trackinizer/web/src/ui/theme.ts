// THE THEME, AS A LIBRARY READS IT.
//
// A chart library cannot read a CSS custom property: it wants `#b8b8b8` at the moment
// it draws. So the tokens are resolved off the live document here, once per render,
// and handed to Vega and uPlot as their own config. A skin switch re-renders the node,
// which re-resolves — which is why no colour is written down twice.
//
// This is the whole reason a library's default look never reaches the page: the
// renderer never passes a colour of its own, it passes these.

/** The design tokens a chart needs, resolved to the values the document computes now. */
export type ThemeInk = {
  /** What is read: a series, a figure, a bar. */
  readonly text: string;
  /** What only locates: an axis label, a tick, a count. */
  readonly machine: string;
  /** The hairline: a grid line, an axis domain, a rule. */
  readonly line: string;
  /** The heavier hairline, for a border that has to hold an edge. */
  readonly lineStrong: string;
  /** The surface a node sits on, for a mark that needs to knock out of it. */
  readonly surface: string;
  /** The categorical ramp: the station groups, which is the app's own order. */
  readonly groups: readonly string[];
  readonly mono: string;
  readonly sans: string;
};

/** Read one custom property off `element`, trimmed. */
const token = (styles: CSSStyleDeclaration, name: string): string => styles.getPropertyValue(name).trim();

/**
 * The tokens as the document computes them right now.
 *
 * Read off `documentElement`, because that is where the theme and the skin are both
 * set, and a node deep in a floating window still inherits from it.
 */
export function themeInk(element: Element = document.documentElement): ThemeInk {
  const styles = getComputedStyle(element);
  return {
    text: token(styles, "--text"),
    machine: token(styles, "--ink-machine"),
    line: token(styles, "--line"),
    lineStrong: token(styles, "--line-strong"),
    surface: token(styles, "--surface"),
    // Four is what the app's own top bar cycles through, so a fifth series is a
    // signal the answer wants fewer series and not a wider ramp.
    groups: [1, 2, 3, 4].map((at) => token(styles, `--group-${at}`)),
    mono: token(styles, "--mono"),
    sans: token(styles, "--sans"),
  };
}

/**
 * Vega-Lite's config, built from the theme.
 *
 * Everything Vega would otherwise decide: the axis ink and face, the grid hairline,
 * the categorical ramp, the absence of a title and a legend border. The spec an agent
 * writes carries data, marks and encoding; it never carries a colour, and this is what
 * makes that true rather than merely asked for.
 */
export function vegaConfig(ink: ThemeInk): Record<string, unknown> {
  // Figures are machine, so every number Vega prints takes the mono face. 10.5px is
  // the section label's size, which is the tier an axis label belongs to.
  const label = { labelFont: ink.mono, labelFontSize: 10.5, labelColor: ink.machine };
  return {
    background: "transparent",
    // No `title` key at all. A chart's own title would be a second heading under the
    // Section that named it, but `title: null` is not how to say so: vega-lite
    // destructures `{ anchor, … }` off the title config while compiling, so a null
    // throws there and the figure never draws. Absent is the way to leave it out.
    font: ink.sans,
    axis: {
      ...label,
      // An axis title names a unit, so it locates: machine ink, machine face.
      titleFont: ink.mono,
      titleFontSize: 10.5,
      titleFontWeight: 400,
      titleColor: ink.machine,
      titlePadding: 8,
      domainColor: ink.line,
      tickColor: ink.line,
      gridColor: ink.line,
      // A grid is a hairline under the marks, never a band across them.
      gridWidth: 1,
      labelPadding: 6,
    },
    // A bar chart's own baseline is the only domain worth drawing; the value axis
    // reads off its grid.
    axisY: { domain: false, ticks: false, gridDash: [] },
    axisX: { grid: false },
    legend: {
      ...label,
      titleFont: ink.mono,
      titleFontSize: 10.5,
      titleFontWeight: 400,
      titleColor: ink.machine,
      symbolType: "square",
      symbolSize: 56,
      orient: "bottom",
      direction: "horizontal",
      offset: 12,
    },
    range: { category: [...ink.groups] },
    // One series is the common case and it takes the ink, not a hue: a single-series
    // chart coloured blue says "blue" about nothing. A hue arrives only where the spec
    // encodes a category, which is what `range` above is for.
    mark: { color: ink.text, tooltip: true },
    bar: { cornerRadius: 0 },
    point: { filled: true, size: 36 },
    line: { strokeWidth: 1.6 },
    rule: { color: ink.line },
    view: { stroke: null, continuousWidth: 400, continuousHeight: 200 },
  };
}

/** uPlot's axis and grid options, built from the same tokens. */
export function seriesAxes(ink: ThemeInk): readonly Record<string, unknown>[] {
  const axis = {
    stroke: ink.machine,
    font: `400 10.5px ${ink.mono}`,
    labelFont: `400 10.5px ${ink.mono}`,
    ticks: { stroke: ink.line, width: 1, size: 4 },
    grid: { stroke: ink.line, width: 1 },
    gap: 6,
  };
  // x then y, which is the order uPlot indexes them in.
  return [axis, { ...axis, grid: { ...axis.grid } }];
}
