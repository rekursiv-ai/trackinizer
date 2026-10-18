import type { components } from "../api/generated/schema";
import type { Bounds } from "./markGeometry";

type Covers = components["schemas"]["MarkCovers"];

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The most the server keeps of each. */
const MAX_INSTANCES = 12;
const MAX_RECORDS = 20;

/**
 * Where a record's id is on the page: lists, outlines and columns (`data-row`),
 * graph nodes (`data-id`) and the timeline (`data-record`). A value that is not a
 * uuid (`Kind#seq`, a field name) names no record.
 */
const RECORD_HOOKS = "[data-row],[data-id],[data-record]";

/** Whether two spans share at least half of the shorter one, so a loose edge is not a cover. */
const shares = (start: number, end: number, from: number, to: number) =>
  Math.min(end, to) - Math.max(start, from) >= Math.min(end - start, to - from) / 2;

const reaches = (element: Element, box: { left: number; top: number; right: number; bottom: number }) => {
  const rect = element.getBoundingClientRect();
  return shares(rect.left, rect.right, box.left, box.right) && shares(rect.top, rect.bottom, box.top, box.bottom);
};

/**
 * The visual instances and the records a mark covers: those on `stage` that the
 * box `bounds` (fractions of the stage) overlaps by half of the smaller of the two,
 * across and down, in page order. A box drawn a little past a row does not take in
 * the next one. Read when the mark is drawn, so an agent learns what the person
 * circled, not where.
 */
export function coversOf(stage: HTMLElement, bounds: Bounds): Covers {
  const origin = stage.getBoundingClientRect();
  const box = {
    left: origin.left + bounds.left * origin.width,
    right: origin.left + bounds.right * origin.width,
    top: origin.top + bounds.top * origin.height,
    bottom: origin.top + bounds.bottom * origin.height,
  };
  const instances = [...stage.querySelectorAll<HTMLElement>("[data-visual-instance]")]
    .filter((element) => reaches(element, box))
    .map((element) => element.dataset.visualInstance!);
  const records = new Set<string>();
  for (const element of stage.querySelectorAll(RECORD_HOOKS)) {
    const id = element.getAttribute("data-row") ?? element.getAttribute("data-id") ?? element.getAttribute("data-record") ?? "";
    if (UUID.test(id) && reaches(element, box)) records.add(id.toLowerCase());
  }
  return { instances: [...new Set(instances)].slice(0, MAX_INSTANCES), records: [...records].slice(0, MAX_RECORDS) };
}
