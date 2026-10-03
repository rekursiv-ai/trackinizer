import type { Ancestor, InquiryRow } from "../api/inquiries";

/** One Issue in an outline: a row the list loaded, or an ancestor of one outside the page. */
export type OutlineNode = {
  readonly id: string;
  readonly kind: string;
  readonly seq: number;
  readonly title: string;
  readonly status: string;
  /** The list's row; null for an ancestor the page does not hold, shown dimmed as older. */
  readonly row: InquiryRow | null;
};

/** One line of an outline. */
export type OutlineLine = {
  /**
   * One node, or a chain of older ancestors, each the only child of the one
   * before, compressed into one line as VS Code's compact folders are. What
   * nests under the last node follows the line.
   */
  readonly nodes: readonly OutlineNode[];
  readonly depth: number;
  /** Lines nest under it, so it expands and collapses. */
  readonly parent: boolean;
  readonly expanded: boolean;
  /** The list's rows under it, at any depth. */
  readonly below: number;
  /** The first node's other parents, beside the one it shows under. */
  readonly alsoUnder: readonly OutlineNode[];
};

/** A page of rows nested under their `narrows` parents. */
export type Outline = {
  /** The lines shown, top to bottom. */
  readonly lines: readonly OutlineLine[];
  /** Rows with no parent and nothing under them, for the No parent group, in the list's order. */
  readonly orphans: readonly InquiryRow[];
};

/**
 * Nest `rows`, in the list's order, under their `narrows` parents from
 * `ancestry` (each row's, as the list route sends it with `ancestors=narrows`).
 *
 * Each Issue shows once: under its first parent, naming the others, so a row
 * keeps one place to focus. Trees, and the lines under each, come in the order
 * of the first row they hold. A row whose ancestry is not in `ancestry` yet is
 * held back rather than shown under No parent, from which it would move. The
 * lines under an id in `collapsed` are left out.
 */
export function buildOutline(
  rows: readonly InquiryRow[],
  ancestry: ReadonlyMap<string, readonly Ancestor[]>,
  collapsed: ReadonlySet<string>,
): Outline {
  const read = rows.filter((row) => ancestry.has(row.id));
  const { nodes, parents } = gather(rows, read, ancestry);
  const under = placeUnder(nodes, parents);
  const rank = new Map<string, number>();
  const below = new Map<string, number>();
  read.forEach((row, index) => {
    for (let id: string | undefined = row.id; id !== undefined; id = under.get(id)) {
      if (!rank.has(id)) rank.set(id, index);
      if (id !== row.id) below.set(id, (below.get(id) ?? 0) + 1);
    }
  });
  const byRank = (a: string, b: string) => rank.get(a)! - rank.get(b)!;
  const children = new Map<string, string[]>();
  const roots: string[] = [];
  for (const id of rank.keys()) {
    const parent = under.get(id);
    if (parent === undefined) roots.push(id);
    else children.set(parent, [...(children.get(parent) ?? []), id]);
  }
  for (const listed of children.values()) listed.sort(byRank);
  const tree = { nodes, parents, under, below, collapsed, children: (id: string) => children.get(id) ?? [] };
  const lines: OutlineLine[] = [];
  const orphans: InquiryRow[] = [];
  for (const id of roots.toSorted(byRank)) {
    const { row } = nodes.get(id)!;
    if (row && !children.has(id)) orphans.push(row);
    else addLines(tree, id, 0, lines);
  }
  return { lines, orphans };
}

/** What `addLines` reads of the nested rows. */
type Tree = {
  readonly nodes: ReadonlyMap<string, OutlineNode>;
  readonly parents: ReadonlyMap<string, readonly string[]>;
  readonly under: ReadonlyMap<string, string>;
  readonly below: ReadonlyMap<string, number>;
  readonly collapsed: ReadonlySet<string>;
  /** The ids shown under one, in order. */
  readonly children: (id: string) => readonly string[];
};

/**
 * Every node, the rows first and then the ancestors the `read` rows name, and
 * each node's parents in the order the ancestries name them.
 */
function gather(
  rows: readonly InquiryRow[],
  read: readonly InquiryRow[],
  ancestry: ReadonlyMap<string, readonly Ancestor[]>,
): { nodes: Map<string, OutlineNode>; parents: Map<string, string[]> } {
  const nodes = new Map<string, OutlineNode>(
    rows.map((row) => [row.id, { id: row.id, kind: row.kind, seq: row.seq, title: row.title, status: row.status, row }]),
  );
  const parents = new Map<string, string[]>();
  for (const row of read) {
    for (const { child_ids, ...up } of ancestry.get(row.id)!) {
      if (!nodes.has(up.id)) nodes.set(up.id, { ...up, row: null });
      for (const child of child_ids) {
        const known = parents.get(child) ?? [];
        if (!known.includes(up.id)) parents.set(child, [...known, up.id]);
      }
    }
  }
  return { nodes, parents };
}

/**
 * The parent each node shows under: its first, or the next when that one is
 * below it already. `narrows` can hold a cycle, which would nest forever.
 */
function placeUnder(nodes: ReadonlyMap<string, OutlineNode>, parents: ReadonlyMap<string, readonly string[]>): Map<string, string> {
  const under = new Map<string, string>();
  const reaches = (from: string, target: string) => {
    for (let id: string | undefined = from; id !== undefined; id = under.get(id)) if (id === target) return true;
    return false;
  };
  for (const id of nodes.keys()) {
    const parent = parents.get(id)?.find((candidate) => !reaches(candidate, id));
    if (parent !== undefined) under.set(id, parent);
  }
  return under;
}

/** Add the line for `id` at `depth`, and the lines under it unless it is collapsed. */
function addLines(tree: Tree, id: string, depth: number, lines: OutlineLine[]): void {
  const chain = [tree.nodes.get(id)!];
  for (;;) {
    const last = chain.at(-1)!;
    const [only, ...more] = tree.children(last.id);
    // Only older ancestors compress, and only one that shows nowhere else.
    if (last.row || only === undefined || more.length > 0) break;
    const next = tree.nodes.get(only)!;
    if (next.row || tree.parents.get(only)!.length > 1) break;
    chain.push(next);
  }
  const last = chain.at(-1)!;
  const children = tree.children(last.id);
  const expanded = !tree.collapsed.has(last.id);
  const shownUnder = tree.under.get(chain[0]!.id);
  lines.push({
    nodes: chain,
    depth,
    parent: children.length > 0,
    expanded,
    below: tree.below.get(chain[0]!.id) ?? 0,
    alsoUnder: (tree.parents.get(chain[0]!.id) ?? []).filter((parent) => parent !== shownUnder).map((parent) => tree.nodes.get(parent)!),
  });
  if (expanded) for (const child of children) addLines(tree, child, depth + 1, lines);
}
