import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { MetaContext, type Meta } from "../app/boot";
import { currentTopic, setTopic } from "../graph/topic";
import { ComposedView, type ComposedFlow } from "./compose";

/** What `ui/ask.tsx` does with a node when a reader points at it. */
function itemOf(node: string): Record<string, unknown> {
  const element = screen
    .getAllByText((_, el) => (el as HTMLElement | null)?.dataset?.node === node)[0]!
    .closest<HTMLElement>("[data-item]")!;
  return JSON.parse(element.dataset.item!) as Record<string, unknown>;
}

function draw(flow: ComposedFlow) {
  render(<ComposedView flow={flow} />);
}

test("a figure too big to carry whole is sampled, not dropped: the question still has data to work from", () => {
  // The cap used to replace the item with `{node, note: "item too large to carry whole"}`,
  // and a reader who pointed at a chart and asked what it showed was told the JSON had not
  // been sent and to point at something smaller — the surface blaming them for its own
  // limit. Every label survives now, and the series keep a head and a count.
  const points = Array.from({ length: 4000 }, (_, step) => ({ x: step, y: step / 4000 }));
  draw({
    title: "sampled",
    compose: [{
      node: "figure",
      figure: {
        kind: "series",
        label: "test accuracy against step",
        curves: [{ name: "decay (wd=1.0)", points }],
      },
    }],
  } as unknown as ComposedFlow);

  const item = itemOf("figure");
  expect(item.note).toBeUndefined();
  const figure = item.figure as { label: string; curves: { name: string; points: unknown[] }[] };
  expect(figure.label).toBe("test accuracy against step");
  expect(figure.curves[0]!.name).toBe("decay (wd=1.0)");
  // A head of the series, and a count of what was left behind — enough to say what it is.
  expect(figure.curves[0]!.points.length).toBeLessThan(points.length);
  expect(JSON.stringify(figure.curves[0]!.points)).toMatch(/\d+ more/);
});

test("an item that fits is carried exactly, so an answer quotes the number the page drew", () => {
  draw({
    title: "whole",
    compose: [{ node: "prose", text: "The clamp arm ends at 1.30% against a 1.03% chance rate." }],
  } as unknown as ComposedFlow);

  expect(itemOf("prose")).toEqual({
    node: "prose",
    text: "The clamp arm ends at 1.30% against a 1.03% chance rate.",
  });
});

afterEach(() => {
  cleanup();
  setTopic(null);
});

test("a drawn composition says what it is about, and stops saying it once it is taken down", () => {
  // What the rail's Graph opens on (`graph/topic`). Without a `MetaContext` above it
  // the app knows no kinds, so a `Belief#651` is not yet a ref — this is the state the
  // canvas is in before boot answers, and it must name nothing rather than guess.
  const flow = {
    title: "Necessary, or alongside? Belief#651 against its own series",
    prompt: "The graph claims weight-decay-driven norm reduction is necessary",
    compose: [{ node: "prose", text: "The clamp arm is an intervention." }],
  } as unknown as ComposedFlow;
  const drawn = render(<ComposedView flow={flow} />);
  expect(currentTopic()).toBeNull();
  drawn.unmount();

  // With the kinds known, the title's own ref is the subject.
  render(
    <MetaContext value={{ kinds: ["Belief", "Issue"] } as unknown as Meta}>
      <ComposedView flow={flow} />
    </MetaContext>,
  );
  expect(currentTopic()).toEqual({ kind: "Belief", seq: 651 });
  cleanup();
  expect(currentTopic()).toBeNull();
});
