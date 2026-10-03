import { useQueries, useQueryClient } from "@tanstack/react-query";
import { type MouseEvent, useEffect, useRef } from "react";
import type { Detail, Peer } from "../api/detail";
import type { InquiryRow } from "../api/inquiries";
import { useCommands } from "../commands/registry";
import { detailQueries } from "../detail/queries";
import { useLiveDetail } from "../live";
import { formatRoute } from "../router/route";
import { useRouter } from "../router/router";
import { ReadFailure } from "../ui/failure";
import { StatusGlyph } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { KindIcon } from "../ui/kinds";
import "./columns.css";

/** An Issue a column lists. */
type Item = Pick<Peer, "id" | "kind" | "seq" | "title" | "status">;

/** One column: what it lists, which of them `path` selects, and the outputs under the last. */
type Column = {
  readonly heading: string;
  readonly items: readonly Item[];
  readonly selected: number | null;
  readonly outputs: readonly Peer[];
  readonly loading: boolean;
  readonly error: Error | null;
  readonly retry: () => void;
};

/**
 * An Issue list as columns (LV2), as Finder's column view: the first lists the
 * list's `roots`, its rows, which the list asked for with `narrows isnull`;
 * each next one the children of the Issue selected in the one before, from the
 * `/api/web/get` backlinks the detail reads too; the last adds that Issue's
 * outputs. `path` is the seqs selected, column by column, as the hash keeps them.
 *
 * Every selection is a hash: a click or → goes a column deeper, ← a column back,
 * and Back steps back the way it came; j and k move within the deepest column
 * without adding to the history, and Enter opens the Issue selected last. Each
 * column stays current as an open detail does.
 */
export function ColumnsView({ kind, roots, path }: { kind: string; roots: readonly InquiryRow[]; path: readonly number[] }) {
  const { navigate } = useRouter();
  const queryClient = useQueryClient();
  const refs = useQueries({ queries: path.map((seq) => detailQueries.ref(kind, seq)) });
  const details = useQueries({
    queries: refs.map((ref) => ({ ...detailQueries.detail(ref.data ?? ""), enabled: ref.data !== undefined })),
  });
  const columns: Column[] = [
    { heading: "Root issues", items: roots, selected: path[0] ?? null, outputs: [], loading: false, error: null, retry: () => {} },
    ...path.map((seq, index) => {
      const read = refs[index]?.error ? refs[index] : details[index];
      return childColumn(seq, details[index]?.data, path[index + 1] ?? null, index === path.length - 1, {
        error: read?.error ?? null,
        retry: () => void read?.refetch(),
      });
    }),
  ];
  const select = (column: number, item: Item, replace = false) => {
    // A seq never moves to another row, so the link's lookup is known already.
    queryClient.setQueryData(detailQueries.ref(item.kind, item.seq).queryKey, item.id);
    navigate({ name: "list", kind, columns: [...path.slice(0, column), item.seq] }, { replace });
  };
  const deepest = Math.max(0, path.length - 1);
  const move = (step: number) => {
    const { items, selected } = columns[deepest]!;
    const index = items.findIndex((item) => item.seq === selected);
    const next = items[Math.min(items.length - 1, Math.max(0, index + step))];
    if (next) select(deepest, next, true);
  };
  useCommands([
    { id: "columns.next", title: "Next in the column", keys: ["j", "ArrowDown"], repeat: true, run: () => move(1) },
    { id: "columns.previous", title: "Previous in the column", keys: ["k", "ArrowUp"], repeat: true, run: () => move(-1) },
    {
      id: "columns.in",
      title: "Into the next column",
      keys: ["ArrowRight"],
      run: () => {
        const first = columns[path.length]?.items[0];
        if (first) select(path.length, first);
      },
    },
    {
      id: "columns.out",
      title: "Back a column",
      keys: ["ArrowLeft"],
      run: () => path.length > 0 && navigate({ name: "list", kind, columns: path.slice(0, -1) }),
    },
    {
      id: "columns.open",
      title: "Open the selected issue",
      keys: ["Enter", "o"],
      run: () => path.length > 0 && navigate({ name: "ref", kind, seq: path.at(-1)! }),
    },
  ]);
  const shelf = useRef<HTMLDivElement>(null);
  const at = path.join(",");
  useEffect(() => {
    // The deepest columns in view, as Finder's scroll along as they drill. Not by
    // scrollIntoView, which scrolls every box around them too, the page's included.
    const root = shelf.current;
    if (root) root.scrollLeft = root.scrollWidth;
  }, [at]);
  const ids = details.flatMap((detail) => detail.data?.self.id ?? []);
  return (
    <div className="c-columns" ref={shelf}>
      {ids.map((id) => (
        <KeepCurrent key={id} id={id} />
      ))}
      {columns.map((column, index) => (
        <ColumnList
          // By depth: the column at a depth shows another Issue's children as the path changes.
          key={index}
          column={column}
          rows={index === 0}
          link={(item) => ({ name: "list", kind, columns: [...path.slice(0, index), item.seq] })}
          onSelect={(item) => select(index, item)}
        />
      ))}
    </div>
  );
}

/** The column after selecting `seq`: its children, which `selected` names one of, and, when `last`, its outputs. */
function childColumn(
  seq: number,
  detail: Detail | undefined,
  selected: number | null,
  last: boolean,
  read: { error: Error | null; retry: () => void },
): Column {
  const children = detail?.backlinks.narrows ?? [];
  const shown = new Set(children.map((child) => child.id));
  // Most children both narrow and were produced by their parent: they show once, as children.
  const outputs = last ? (detail?.backlinks.produced_by ?? []).filter((output) => !shown.has(output.id)) : [];
  const loading = !detail && !read.error;
  return { heading: detail ? `Under ${detail.self.title}` : `Under #${seq}`, items: children, selected, outputs, loading, ...read };
}

function ColumnList({
  column,
  rows,
  link,
  onSelect,
}: {
  column: Column;
  /** The list's own rows, which the live layer watches on screen. */
  rows: boolean;
  link: (item: Item) => Parameters<typeof formatRoute>[0];
  onSelect: (item: Item) => void;
}) {
  const empty = !column.loading && !column.error && column.items.length === 0 && column.outputs.length === 0;
  return (
    <section className="c-column" aria-label={column.heading}>
      <h3 className="c-heading">{column.heading}</h3>
      {column.loading && <p className="c-note">Loading…</p>}
      {column.error && <ReadFailure error={column.error} retry={column.retry} />}
      {column.items.map((item) => (
        <a
          key={item.id}
          className="c-item"
          href={formatRoute(link(item))}
          data-row={rows ? item.id : undefined}
          aria-current={item.seq === column.selected ? "true" : undefined}
          // The focus stays where it was, as on a list's buttons: a link the mouse
          // focused would take Enter and select itself again, where Enter opens the
          // deepest selection. Tab still focuses each link, and there Enter selects it.
          onMouseDown={(event: MouseEvent) => event.preventDefault()}
          onClick={(event: MouseEvent) => {
            // A plain click selects; one with a modifier opens a tab or window, as links do.
            if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            event.preventDefault();
            onSelect(item);
          }}
        >
          <StatusGlyph status={item.status} />
          <span className="c-ref">#{item.seq}</span>
          <span className="c-title">{item.title}</span>
          <Icon name="chevR" size={14} className="c-chev" />
        </a>
      ))}
      {empty && <p className="c-note">No issues narrow it.</p>}
      {column.outputs.length > 0 && <h4 className="c-subheading">Outputs</h4>}
      {column.outputs.map((output) => (
        <a key={output.id} className="c-output" href={formatRoute({ name: "ref", kind: output.kind, seq: output.seq })}>
          <KindIcon kind={output.kind} size={14} />
          {output.title}
        </a>
      ))}
    </section>
  );
}

/** Keep the detail of `id` current while its column shows. */
function KeepCurrent({ id }: { id: string }) {
  useLiveDetail(id);
  return null;
}
