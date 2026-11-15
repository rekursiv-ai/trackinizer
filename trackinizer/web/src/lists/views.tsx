/**
 * How a list lays out its rows: List, the flat list every kind has, or, on Issue
 * lists, the views that follow `narrows` (track LV): Streams, which groups rows
 * by the top of their ancestry, Outline, which nests them under their parents,
 * and Columns, which drills from root Issues down through their children.
 */
export type View = "list" | "streams" | "outline" | "columns" | "code";

/**
 * A view the list's stored state keeps. Columns lives in the hash instead, with
 * its path (`#/list/Issue?view=columns&path=…`), so Back steps through it and
 * a plain `#/list/Issue` never opens it.
 */
export type StoredView = Exclude<View, "columns">;

/** The views a list of `kinds` offers: the new views follow `narrows`, which joins Issue to Issue. */
export function offeredViews(kinds: readonly string[]): readonly View[] {
  if (kinds.length !== 1) return ["list"];
  if (kinds[0] === "Issue") return ["list", "streams", "outline", "columns"];
  // Code shows no rows at all: it is the session's own source, which belongs beside the
  // changes recorded in it rather than in a place of its own.
  return kinds[0] === CODE_KIND ? ["list", "code"] : ["list"];
}

/** The kind whose list offers Code: a session's source sits beside the changes made in it. */
export const CODE_KIND = "CodeChange";

/** Whether `value` is a view the stored state keeps, as one read back may not be. */
export function isStoredView(value: unknown): value is StoredView {
  return value === "list" || value === "streams" || value === "outline" || value === "code";
}

/** The switch between the `offered` views, in the list's tools; none when there is one. */
export function ViewSwitch({
  offered,
  view,
  onChange,
}: {
  offered: readonly View[];
  view: View;
  onChange: (view: View) => void;
}) {
  if (offered.length < 2) return null;
  return (
    <div className="view-seg" role="group" aria-label="View">
      {offered.map((value) => (
        <button key={value} type="button" aria-pressed={value === view} onClick={() => onChange(value)}>
          {VIEW_NAMES[value]}
        </button>
      ))}
    </div>
  );
}

const VIEW_NAMES: { readonly [view in View]: string } = {
  list: "List",
  streams: "Streams",
  outline: "Outline",
  columns: "Columns",
  code: "Code",
};
