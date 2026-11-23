// THE NUMERIC TABLE, one of the library's pieces: a header row, one named row each,
// and — the part no other surface in this app draws — the row the app subtracts under
// them. Three sheets already style a table (`.md table`, `.jv-table`, `.sum-table`),
// each for its own object, and none of them may state a figure's precision; this owns
// the shape a fetched page's results table has, because it is the one table an answer
// surface carries and the one whose arithmetic a reader must be able to check.
//
// The rule it exists to hold: a cell prints at the precision the column itself prints
// at, and a derived figure is computed off the cells drawn beside it. Nothing in a flow
// can ask for a format, a width or an ink — those are here, once.
import "./table.css";

/** A table as its source prints it: the caption whole, the headers in order, one array
 *  per row with the row's own name first. A cell is a number where the source prints a
 *  figure, and the source's own text where it prints `—`. */
export type NumTable = {
  /** The table's label and caption, quoted whole: `Table 1. Benchmark results …`. */
  readonly caption: string;
  /** The column headers, in the source's order. */
  readonly columns: readonly string[];
  /** One array per row: the row's own name, then one cell per column. */
  readonly rows: readonly (readonly (string | number)[])[];
};

/** The dash a column falls back to when its source prints no figure to subtract. */
const DASH = "—";

/** A cell as a number, if the source's cell is one. */
const numeric = (cell: unknown): number | null =>
  (typeof cell === "number" && Number.isFinite(cell) ? cell : null);

/** One column read off the drawn cells: the figures it prints, the precision it prints
 *  them at, and the spread between its own extremes. */
export type Column = { readonly name: string; readonly places: number; readonly spread: string | null };

/** The precision is the column's own — the most decimals any of its cells carries — so a
 *  figure printed as `38.00` keeps its trailing zeros and a subtracted row is exact at
 *  the same place. No format constant is chosen here. */
const placesOf = (values: readonly number[]): number =>
  values.reduce((most, one) => Math.max(most, (one.toString().split(".")[1] ?? "").length), 0);

/** Every column, measured off `table.rows` — the cells this component is about to draw. */
export function columnsOf(table: NumTable): readonly Column[] {
  return table.columns.map((name, index) => {
    const values = table.rows
      .map((row) => numeric(row[index + 1]))
      .filter((one): one is number => one !== null);
    const spread = values.length < 2 ? null : (Math.max(...values) - Math.min(...values)).toFixed(placesOf(values));
    return { name, places: placesOf(values), spread };
  });
}

/** A cell as it prints: the source's own text, or the figure at its column's precision. */
export const cellText = (cell: unknown, places: number): string => {
  const one = numeric(cell);
  return one === null ? String(cell ?? DASH) : one.toFixed(places);
};

/** The columns that carry a figure, widest first — the read a sentence about the table
 *  is built from, so no prose has to repeat the arithmetic. */
export const widestFirst = (columns: readonly Column[]): readonly Column[] =>
  columns
    .filter((one) => one.spread !== null)
    .sort((a, b) => Number(b.spread) - Number(a.spread));

/** The table, and the one row beneath it that its source did not print. `derive` off is
 *  for a table whose own paper already states the spread. */
export function NumTableView({ table, derive = true }: {
  readonly table: NumTable;
  readonly derive?: boolean;
}) {
  const columns = columnsOf(table);
  return (
    <>
      <p className="ui-cap">{table.caption}</p>
      <table className="ui-table">
        <thead>
          <tr>
            <th scope="col" className="ui-corner" />
            {table.columns.map((name) => <th scope="col" key={name}>{name}</th>)}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row) => (
            <tr key={String(row[0])}>
              <th scope="row">{String(row[0])}</th>
              {columns.map((column, index) => (
                <td key={column.name}>{cellText(row[index + 1], column.places)}</td>
              ))}
            </tr>
          ))}
          {derive ? (
            <tr className="ui-derived">
              <th scope="row">spread</th>
              {columns.map((column) => <td key={column.name}>{column.spread ?? DASH}</td>)}
            </tr>
          ) : null}
        </tbody>
      </table>
    </>
  );
}
