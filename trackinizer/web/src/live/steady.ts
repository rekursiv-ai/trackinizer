import { useState } from "react";

/**
 * `sections` laid out as they were, while the same rows are loaded under the
 * same `layout` (say, the grouping and ordering): each row keeps its place, and
 * shows its latest data.
 *
 * A live update changes rows in place, so a new priority must not move its row
 * to another group or position under the pointer or the keyboard focus. The
 * fresh layout is taken when `layout` changes, or when rows come or go, which
 * happens only on something the user did (a tab, a filter, Load more) or on the
 * "N new" pill, whose rows arrive through `ListLive.merge`.
 */
export function useSteadyLayout<Row extends { readonly id: string }, Section extends { readonly rows: readonly Row[] }>(
  sections: readonly Section[],
  layout: string,
): readonly Section[] {
  const [kept, setKept] = useState(() => ({ sections, layout, ids: idsOf(sections) }));
  const fresh = new Map(sections.flatMap((section) => section.rows.map((row) => [row.id, row] as const)));
  if (layout !== kept.layout || fresh.size !== kept.ids.size || [...fresh.keys()].some((id) => !kept.ids.has(id))) {
    // Stored during render, as React's "storing information from previous
    // renders" does, so the fresh layout shows in this very render.
    setKept({ sections, layout, ids: new Set(fresh.keys()) });
    return sections;
  }
  return kept.sections.map((section) => ({ ...section, rows: section.rows.map((row) => fresh.get(row.id)!) }));
}

function idsOf(sections: readonly { readonly rows: readonly { readonly id: string }[] }[]): Set<string> {
  return new Set(sections.flatMap((section) => section.rows.map((row) => row.id)));
}
