import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { DrawingView } from "./drawing";

// SMIL on `transform`, which Chrome animates. On `cx`, `r`, `x` or `y` it silently freezes,
// which is the trap the brief names.
const SWEEP =
  '<g><circle cx="10" cy="10" r="4"/>' +
  '<animateTransform attributeName="transform" type="translate" values="10 10;90 10" ' +
  'dur="4s" repeatCount="indefinite"/></g>';

let timeline: string[] = [];

const stop = () => { timeline.push("pause"); };
const run = () => { timeline.push("unpause"); };

/** jsdom implements no SVG animation, so the only two methods a real browser would have are
 *  installed here — which is also the point: this app's guard is what reaches SMIL, because
 *  the stylesheet's `animation: none` cannot. */
const withTimeline = () => {
  (SVGSVGElement.prototype as unknown as Record<string, unknown>).pauseAnimations = stop;
  (SVGSVGElement.prototype as unknown as Record<string, unknown>).unpauseAnimations = run;
};
const withoutTimeline = () => {
  for (const name of ["pauseAnimations", "unpauseAnimations"]) {
    delete (SVGSVGElement.prototype as unknown as Record<string, unknown>)[name];
  }
};

const askForLessMotion = (matches: boolean) => {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: () => ({
      matches,
      media: "",
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
};

afterEach(() => {
  cleanup();
  withoutTimeline();
  timeline = [];
});

test("a reduced-motion request stops a drawing's own animation", () => {
  withTimeline();
  askForLessMotion(true);
  render(<DrawingView body={SWEEP} viewBox="0 0 100 30" label="a sweep across the marks" />);
  expect(timeline).toEqual(["pause"]);
});

test("a reader who asked for nothing leaves the timeline running", () => {
  withTimeline();
  askForLessMotion(false);
  render(<DrawingView body={SWEEP} viewBox="0 0 100 30" label="a sweep across the marks" />);
  expect(timeline).toEqual([]);
});

test("a geometry property the engine does drive is not reported as frozen", () => {
  // Was asserting the opposite, against a Chrome that could not animate the SVG2 geometry
  // properties. It can now — measured in this app's own Chromium, `cx` travelled 199.98 → 33.97
  // in 600 ms with the timeline running — so the caption this used to demand was a false
  // report printed under a figure that was moving the whole time.
  const sliding = render(
    <DrawingView
      body='<circle cx="10" cy="10" r="4"><animate attributeName="cx" values="10;90" dur="2s" repeatCount="indefinite"/></circle>'
      viewBox="0 0 100 30"
      label="a mark that slides"
    />,
  );
  expect(sliding.container.querySelector("figcaption")).toBeNull();
  const moving = render(<DrawingView body={SWEEP} viewBox="0 0 100 30" label="a sweep across the marks" />);
  expect(moving.container.querySelector("figcaption")).toBeNull();
});

test("what the sandbox strips is still named under the drawing", () => {
  // The honest-report machinery stays: `mpath` survives the element allow-list but its
  // `href` does not, so a motion path referenced by id silently goes nowhere. Measured:
  // the mark stayed put while its `path=`-carrying twin ran the same curve.
  const stripped = render(
    <DrawingView
      body='<path id="t" d="M 0 20 Q 50 0 100 20" fill="none"/><circle r="3"><animateMotion dur="3s"><mpath href="#t"/></animateMotion></circle>'
      viewBox="0 0 100 30"
      label="a mark that tries to follow a path by reference"
    />,
  );
  expect(stripped.container.querySelector("figcaption")?.textContent).toContain("mpath[href]");
});
