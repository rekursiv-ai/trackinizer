import { renderHook } from "@testing-library/react";
import { expect, test } from "vitest";
import { useSteadyLayout } from "./steady";

type Row = { id: string; band: number };
type Section = { value: number; rows: Row[] };

/** Rows grouped by band, as a list lays them out fresh. */
function layOut(rows: Row[]): Section[] {
  return [...new Set(rows.map((row) => row.band))].toSorted().map((value) => ({
    value,
    rows: rows.filter((row) => row.band === value),
  }));
}

const shape = (sections: readonly Section[]) => sections.map((s) => `${s.value}:${s.rows.map((r) => `${r.id}${r.band}`).join(",")}`);

test("rows keep their places while the same rows are loaded, and show their latest data", () => {
  const rows = [
    { id: "a", band: 2 },
    { id: "b", band: 2 },
    { id: "c", band: 3 },
  ];
  const { result, rerender } = renderHook(({ sections, layout }) => useSteadyLayout(sections, layout), {
    initialProps: { sections: layOut(rows), layout: "band" },
  });
  expect(shape(result.current)).toEqual(["2:a2,b2", "3:c3"]);
  // "a" moved to band 0 on the server: it stays where it was, showing band 0.
  const moved = [{ id: "a", band: 0 }, rows[1]!, rows[2]!];
  rerender({ sections: layOut(moved), layout: "band" });
  expect(shape(result.current)).toEqual(["2:a0,b2", "3:c3"]);
  // A new layout asked for by the user takes the fresh one.
  rerender({ sections: layOut(moved), layout: "band, reversed" });
  expect(shape(result.current)).toEqual(["0:a0", "2:b2", "3:c3"]);
});

test("rows coming or going take the fresh layout", () => {
  const rows = [
    { id: "a", band: 2 },
    { id: "b", band: 3 },
  ];
  const { result, rerender } = renderHook(({ sections }) => useSteadyLayout(sections, "band"), {
    initialProps: { sections: layOut(rows) },
  });
  const moved = [{ id: "a", band: 3 }, rows[1]!];
  rerender({ sections: layOut(moved) });
  expect(shape(result.current)).toEqual(["2:a3", "3:b3"]);
  rerender({ sections: layOut([...moved, { id: "c", band: 1 }]) });
  expect(shape(result.current)).toEqual(["1:c1", "3:a3,b3"]);
});
