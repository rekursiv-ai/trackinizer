import { afterEach, expect, test } from "vitest";
import { coversOf } from "./markCovers";

const A = "889ffcb2-cf44-43e7-9806-eb08428c6203";
const B = "2de97e19-2624-4e89-804e-f19e7248eec3";
const C = "c5286865-67b6-4bd8-ab51-e06e10c326c5";

afterEach(() => { document.body.innerHTML = ""; });

/** A 1000 x 500 stage at the origin holding elements, each drawn at `[left, top, width, height]`. */
function stage(children: string, boxes: Record<string, [number, number, number, number]>): HTMLElement {
  document.body.innerHTML = `<div id="stage">${children}</div>`;
  const root = document.getElementById("stage")!;
  const place = (element: Element, [left, top, width, height]: [number, number, number, number]) => {
    element.getBoundingClientRect = () => ({ left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON: () => ({}) });
  };
  place(root, [0, 0, 1000, 500]);
  for (const [id, box] of Object.entries(boxes)) place(document.getElementById(id)!, box);
  return root;
}

test("a mark covers the visuals and the records its box reaches, by each hook, in page order", () => {
  const root = stage(
    `<div id="v1" data-visual-instance="inst-1"></div><div id="v2" data-visual-instance="inst-2"></div>
     <div id="r1" data-row="${A}"></div><svg><g id="r2" data-id="${B}"></g><g id="r3" data-record="${C}"></g></svg>`,
    { v1: [0, 0, 500, 500], v2: [500, 0, 500, 500], r1: [100, 100, 200, 20], r2: [150, 300, 40, 40], r3: [800, 100, 40, 40] },
  );
  expect(coversOf(root, { left: 0.05, top: 0.1, right: 0.4, bottom: 0.9 })).toEqual({ instances: ["inst-1"], records: [A, B] });
  expect(coversOf(root, { left: 0.7, top: 0.1, right: 0.9, bottom: 0.3 })).toEqual({ instances: ["inst-2"], records: [C] });
});

test("a point inside an element covers it, and a box clear of everything covers nothing", () => {
  const root = stage(`<div id="r1" data-row="${A}"></div>`, { r1: [400, 200, 100, 50] });
  expect(coversOf(root, { left: 0.45, top: 0.45, right: 0.45, bottom: 0.45 }).records).toEqual([A]);
  expect(coversOf(root, { left: 0, top: 0, right: 0.1, bottom: 0.1 })).toEqual({ instances: [], records: [] });
});

test("a box drawn loosely around a row covers that row, not one it touches or grazes", () => {
  const root = stage(`<div id="r1" data-row="${A}"></div><div id="r2" data-row="${B}"></div>`, { r1: [0, 100, 1000, 40], r2: [0, 140, 1000, 40] });
  expect(coversOf(root, { left: 0.02, top: 0.28, right: 0.98, bottom: 0.38 }).records).toEqual([B]);
  expect(coversOf(root, { left: 0.02, top: 0.268, right: 0.98, bottom: 0.38 }).records).toEqual([B]);
  expect(coversOf(root, { left: 0.1, top: 0.23, right: 0.3, bottom: 0.33 }).records).toEqual([A, B]);
});

test("an attribute that is not a uuid names no record", () => {
  const root = stage(`<div id="r1" data-row="Experiment#12"></div><div id="r2" data-id="description"></div>`, { r1: [0, 0, 1000, 100], r2: [0, 0, 1000, 100] });
  expect(coversOf(root, { left: 0, top: 0, right: 1, bottom: 1 }).records).toEqual([]);
});

test("a mark keeps the server's most of each, and a record once", () => {
  const ids = Array.from({ length: 30 }, (_, i) => `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`);
  const html = ids.map((id, i) => `<div id="r${i}" data-row="${id}"></div><div id="d${i}" data-id="${id}"></div>`).join("")
    + Array.from({ length: 15 }, (_, i) => `<div id="v${i}" data-visual-instance="i${i}"></div>`).join("");
  const boxes = Object.fromEntries([...ids.keys()].flatMap((i) => [[`r${i}`, [0, 0, 10, 10]], [`d${i}`, [0, 0, 10, 10]]]).concat(
    Array.from({ length: 15 }, (_, i) => [`v${i}`, [0, 0, 10, 10]])) as [string, [number, number, number, number]][]);
  const covers = coversOf(stage(html, boxes), { left: 0, top: 0, right: 1, bottom: 1 });
  expect(covers.records).toHaveLength(20);
  expect(new Set(covers.records).size).toBe(20);
  expect(covers.instances).toHaveLength(12);
});
