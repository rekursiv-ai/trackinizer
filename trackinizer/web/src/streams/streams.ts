import type { Ancestor, InquiryRow } from "../api/inquiries";

/** An Issue a stream names: its root goal, a row's parent, another root. */
export type StreamIssue = Omit<Ancestor, "child_ids">;

/** One row in a stream. */
export type StreamRow = {
  readonly row: InquiryRow;
  /** The row's parent on its way up to this stream's root, when that is not the root. */
  readonly parent: StreamIssue | null;
  /** The row's other roots, whose streams list it too. */
  readonly alsoUnder: readonly StreamIssue[];
};

/** The page's rows under one root goal, or, with no root, those with no parent. */
export type Stream = {
  /** The top of the rows' `narrows` ancestry; null for the No parent group. */
  readonly root: StreamIssue | null;
  /** Newest first, each once. */
  readonly rows: readonly StreamRow[];
  readonly active: number;
  /** Rows complete. */
  readonly done: number;
  /** When the newest row was created. */
  readonly newest: string;
};

/**
 * Group `rows`, newest first, by root goal: the top of each one's `narrows`
 * ancestry in `ancestry`, as the list route sends it with `ancestors=narrows`.
 *
 * Streams come in the order of their newest row, then the No parent group. A
 * row under several roots is listed under each, and once under each however
 * many ways it reaches it. A row with no parent heads a stream of its own when
 * other rows are under it, and is listed there. A row whose ancestry is not in
 * `ancestry` yet is held back rather than shown under No parent, from which it
 * would move.
 */
export function buildStreams(rows: readonly InquiryRow[], ancestry: ReadonlyMap<string, readonly Ancestor[]>): Stream[] {
  const read = rows.flatMap((row) => {
    const ancestors = ancestry.get(row.id);
    return ancestors ? [{ row, ancestors, roots: rootsOf(ancestors) }] : [];
  });
  const heads = new Set(read.flatMap(({ roots }) => roots.map((root) => root.id)));
  const streams = new Map<string, { root: StreamIssue; rows: StreamRow[] }>();
  const loose: StreamRow[] = [];
  const add = (root: StreamIssue, listed: StreamRow) => {
    const stream = streams.get(root.id) ?? { root, rows: [] };
    streams.set(root.id, stream);
    stream.rows.push(listed);
  };
  for (const { row, ancestors, roots } of read) {
    if (roots.length > 0) {
      for (const root of roots) {
        add(root, { row, parent: parentToward(row.id, root.id, ancestors), alsoUnder: roots.filter((other) => other !== root) });
      }
    } else if (heads.has(row.id)) {
      add(issueOf(row), { row, parent: null, alsoUnder: [] });
    } else {
      loose.push({ row, parent: null, alsoUnder: [] });
    }
  }
  return [...streams.values(), ...(loose.length > 0 ? [{ root: null, rows: loose }] : [])].map(({ root, rows: listed }) => ({
    root,
    rows: listed,
    active: listed.filter(({ row }) => row.status === "active").length,
    done: listed.filter(({ row }) => row.status === "complete").length,
    newest: listed[0]!.row.created,
  }));
}

/**
 * The tops of a row's ancestry: the ancestors nothing above narrows. In a cycle
 * every ancestor has a parent, and the farthest one read stands for the top.
 */
function rootsOf(ancestors: readonly Ancestor[]): StreamIssue[] {
  const narrowing = new Set(ancestors.flatMap((up) => up.child_ids));
  const tops = ancestors.filter((up) => !narrowing.has(up.id));
  return (tops.length > 0 ? tops : ancestors.slice(-1)).map(issueOf);
}

/** The row's parent on its first way up to `rootId`; null when that parent is the root. */
function parentToward(rowId: string, rootId: string, ancestors: readonly Ancestor[]): StreamIssue | null {
  const parents = (id: string) => ancestors.filter((up) => up.child_ids.includes(id));
  const reaches = (from: string) => {
    // Breadth first, each Issue once: `narrows` can hold a cycle.
    const queue = [from];
    for (const id of queue) {
      if (id === rootId) return true;
      for (const up of parents(id)) if (!queue.includes(up.id)) queue.push(up.id);
    }
    return false;
  };
  const parent = parents(rowId).find((up) => reaches(up.id));
  return parent && parent.id !== rootId ? issueOf(parent) : null;
}

function issueOf({ id, kind, seq, title, status }: StreamIssue): StreamIssue {
  return { id, kind, seq, title, status };
}
