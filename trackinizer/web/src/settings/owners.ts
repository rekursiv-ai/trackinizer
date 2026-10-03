/** An owner name found on rows under my account, and on how many. */
export type OwnerName = { readonly name: string; readonly rows: number };

/** The owners of `rows`, most frequent first, then by name. */
export function ownerNames(rows: readonly { readonly owner: string | null }[]): OwnerName[] {
  const counts = new Map<string, number>();
  for (const { owner } of rows) {
    if (owner) counts.set(owner, (counts.get(owner) ?? 0) + 1);
  }
  return [...counts]
    .map(([name, count]) => ({ name, rows: count }))
    .sort((a, b) => b.rows - a.rows || a.name.localeCompare(b.name));
}
