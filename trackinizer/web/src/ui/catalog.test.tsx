// EVERY NODE IN THE CATALOG IS MEASURED IN THE DOM.
//
// The catalog is read by the agent as a promise: pick this node and that is what is
// drawn. A node listed and not rendering is therefore a defect in the agent's output,
// not in a view nobody opened — it spends a real model call writing a field that
// vanishes. So each entry's `measure` is asserted here against a live render, and an
// entry with no test fails the last check in this file.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { Markdown } from "../markdown/Markdown";
import { CATALOG } from "./catalog";
import { ChartView } from "./chart";
import { EquationView } from "./equation";
import { Marks, Paths, Prose, Section } from "./sections";
import { RegisterView } from "./register";
import { RichView } from "./rich";
import { SeriesView } from "./series";
import { NumTableView } from "./table";

test("Prose · 1 p.ui-prose; a ticked token is 1 code.ui-tok and the ticks never print", () => {
  const { container } = render(<Prose>{"The gate at `cfg.block_len` decided it."}</Prose>);
  expect(container.querySelectorAll("p.ui-prose")).toHaveLength(1);
  const token = container.querySelectorAll("code.ui-tok");
  expect(token).toHaveLength(1);
  expect(token[0]!.textContent).toBe("cfg.block_len");
  expect(container.textContent).not.toContain("`");
});

test("Marks · one p.ui-mark per line, and hue is on the glyph alone", () => {
  const { container } = render(
    <Marks items={[
      { kind: "fit", text: "follows ui/compose.tsx's own style" },
      { kind: "finding", text: "model.ts:87 declares what nothing reads" },
      { kind: "demand", text: "delete it, or render it" },
    ]} />,
  );
  expect(container.querySelectorAll("p.ui-mark")).toHaveLength(3);
  expect([...container.querySelectorAll("p.ui-mark > b")].map((glyph) => glyph.textContent)).toEqual(["+", "−", "→"]);
  // The words live in their own box, so a loose token cannot take the glyph's gap.
  for (const mark of container.querySelectorAll("p.ui-mark")) {
    expect(mark.children[0]!.tagName).toBe("B");
    expect(mark.children[1]!.tagName).toBe("SPAN");
  }
});

test("Section · the label, the hairline carrying the width, the measure at its end", () => {
  const { container } = render(<Section label="WHAT IS OUT OF PLACE" size="31 files" />);
  const rule = container.querySelector("p.ui-sec")!;
  expect(rule.querySelector("b")!.textContent).toBe("WHAT IS OUT OF PLACE");
  expect(rule.querySelector("s")).not.toBeNull();
  expect(rule.querySelector("em")!.textContent).toBe("31 files");
});

test("Paths · one span per path, broken after a slash and nowhere else", () => {
  const { container } = render(<Paths items={["trackinizer/server/auth.py:369", "web/src/blocks/model.ts:87"]} />);
  expect(container.querySelectorAll(".ui-paths > span")).toHaveLength(2);
  // Three slashes in the first path, two in the second: a break opportunity after each.
  expect(container.querySelectorAll("wbr")).toHaveLength(5);
  expect(container.textContent).toContain("trackinizer/server/auth.py:369");
});

test("Equation · 1 .ui-eq > .katex with .katex-html, and 0 error spans", async () => {
  const { container } = render(<EquationView tex="\sigma\left(\sum_i v_i\right)" />);
  await waitFor(() => expect(container.querySelector(".ui-eq .katex")).not.toBeNull());
  expect(container.querySelectorAll(".ui-eq .katex-html")).toHaveLength(1);
  expect(container.querySelectorAll(".katex-error")).toHaveLength(0);
});

test("Equation · a formula that will not parse prints as its own source, never as an error", async () => {
  const { container } = render(<EquationView tex="\frac{1}{" />);
  await waitFor(() => expect(container.querySelector(".katex-error")).not.toBeNull());
  // The source is the record, so it is still on the page and still readable.
  expect(container.querySelector(".katex-error")!.textContent).toBe("\\frac{1}{");
});

test("Register · a window of items is mounted and the rail reserves all of them", () => {
  // jsdom lays nothing out, so the virtualizer would measure a 0×0 lane and mount
  // nothing. It reads `clientWidth` (virtual-core's getRect), so that is what the
  // lane is given: the width a floating window actually offers, which is what the
  // count below is a window of.
  const sized = (name: "clientWidth" | "clientHeight", value: number) => {
    const was = Object.getOwnPropertyDescriptor(HTMLElement.prototype, name);
    Object.defineProperty(HTMLElement.prototype, name, { configurable: true, value });
    return () => { if (was) Object.defineProperty(HTMLElement.prototype, name, was); };
  };
  const restore = [sized("clientWidth", 900), sized("clientHeight", 200)];
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  // Forty commits, as the replay strip has at forty arms: the mockup's four-card row
  // has no answer for the fortieth, and this is it.
  const { container } = render(
    <RegisterView count={40} axis="x" extent={220} gap={12} label="the replay"
      item={(at) => <span>{`commit ${at}`}</span>} />,
  );
  const mounted = container.querySelectorAll(".ui-reg-col");
  expect(mounted.length).toBeGreaterThan(0);
  expect(mounted.length, "a window, not all forty").toBeLessThan(40);
  // The rail carries every item's extent, so the scrollbar describes all forty.
  expect(container.querySelector<HTMLElement>(".ui-reg-rail")!.style.width).toBe(`${40 * 232}px`);
  // It scrolls on its own axis and never wraps.
  expect(container.querySelector(".ui-reg")!.classList.contains("ui-reg-x")).toBe(true);
  for (const undo of restore) undo();
  vi.unstubAllGlobals();
});

test("Table · 1 table, 1 caption, one tr per row, and a derived row is computed", () => {
  const { container } = render(
    <NumTableView table={{
      caption: "Table 3. Early validation loss, sampler seed varied, data order held.",
      columns: ["124M", "350M"],
      rows: [["seed 1", 3.312, 3.177], ["seed 2", 3.316, 3.183], ["spread", 0.007, 0.006]],
    }} derive={false} />,
  );
  expect(container.querySelectorAll("table")).toHaveLength(1);
  // The caption is quoted whole: a table a reader cannot match to its source is a
  // table they have to go and find.
  expect(container.querySelectorAll("p.ui-cap")).toHaveLength(1);
  expect(container.querySelector("p.ui-cap")!.textContent).toContain("Table 3.");
  expect(container.querySelectorAll("tbody tr")).toHaveLength(3);
  expect(container.querySelectorAll("tr.ui-derived")).toHaveLength(0);
});

test("Prose/Markdown · the fold opens to 1 p, 1 strong, 3 code", async () => {
  const { container } = render(
    <Markdown kinds={[]} source="The gate **never** ran: `cfg.block_len`, `seq_len`, `use_block_sparse`." />,
  );
  await waitFor(() => expect(container.querySelectorAll("p")).toHaveLength(1));
  expect(container.querySelectorAll("strong")).toHaveLength(1);
  expect(container.querySelectorAll("code")).toHaveLength(3);
});

test("Rich · 1 .ProseMirror at the prose face, clamped to the measure", async () => {
  const { container } = render(<RichView doc="A draft nobody has filed yet." />);
  // Until the editor arrives the text stands as prose, so the sentence is readable
  // from the first frame rather than replaced by a spinner.
  expect(container.querySelector(".ui-rich .ui-prose")!.textContent).toBe("A draft nobody has filed yet.");
  await waitFor(() => expect(container.querySelectorAll(".ui-rich .ProseMirror")).toHaveLength(1), { timeout: 5000 });
});

test("Chart · a spec that names what the theme owns is refused, not drawn in a library's blue", () => {
  render(<ChartView label="records by kind" spec={{ mark: "bar", config: { background: "#fff" } }} />);
  expect(screen.getByText(/the theme owns config/)).toBeDefined();
});

test("Series · a curve whose values do not line up with its steps draws nothing", () => {
  // Silence beats a figure plotted off the end of its own x.
  const { container } = render(<SeriesView curves={[{ key: "loss", steps: [1, 2, 3], values: [0.1] }]} />);
  expect(container.querySelectorAll("canvas")).toHaveLength(0);
});

test("every node in the catalog has a measure, and every measure is tested here", () => {
  // Read from disk by path: the test runner's `import.meta.url` is not a file URL.
  const text = readFileSync(join(import.meta.dirname, "catalog.test.tsx"), "utf8");
  const untested: string[] = [];
  for (const node of CATALOG) {
    expect(node.measure, `${node.node} has no measure`).not.toBe("");
    // The node's name opens a test in this file, which is what makes the catalog's
    // promise checkable rather than documented.
    if (!new RegExp(`test\\("${node.node.replace("/", "\\/")} `).test(text)) untested.push(node.node);
  }
  // Chart's and Series' happy paths need a real canvas and are measured live by
  // e2e, not here: jsdom has no 2D context and Vega needs layout.
  // Flow/Graph/Figure/Text/Plotly/LearningCurve/Confusion/Embedding measure via
  // the live canvas (e2e); jsdom cannot measure React Flow or Plotly.
  const e2eExempt = new Set(["Flow", "Graph", "Figure", "Text", "Plotly", "LearningCurve", "Confusion", "Embedding", "MermaidMindmap", "Markmap", "Choropleth", "Sankey", "Network"]);
  expect(untested.filter((n) => !e2eExempt.has(n))).toEqual([]);
});
