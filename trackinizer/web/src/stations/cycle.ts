/** A station as the cycle reads it. */
export type Cyclable = { readonly id: string; readonly name: string; readonly created_at: string };

/** The name a station gets until the user renames it. */
export const UNNAMED = /^unnamed-workstream#\d+$/;

/** The stations in the order their tabs stand: as they were made (by id when made together), so opening one reorders nothing. */
export function inTabOrder<T extends { readonly id: string; readonly created_at: string }>(stations: readonly T[]): T[] {
  return stations.toSorted((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id));
}

/** The stations the user renamed, in the order they were made (by id when made together), so opening one reorders nothing. */
export function renamed<T extends Cyclable>(stations: readonly T[]): T[] {
  return inTabOrder(stations.filter((each) => !UNNAMED.test(each.name)));
}

/**
 * The renamed station `step` places after `currentId` (1 next, -1 previous),
 * wrapping round. From a station outside the cycle, the first on a step forward
 * and the last on a step back. Null when none was renamed.
 */
export function stepStation<T extends Cyclable>(stations: readonly T[], currentId: string | null, step: 1 | -1): T | null {
  const ring = renamed(stations);
  if (ring.length === 0) return null;
  const at = ring.findIndex((each) => each.id === currentId);
  if (at < 0) return step === 1 ? ring[0]! : ring.at(-1)!;
  return ring[(at + step + ring.length) % ring.length]!;
}

/** The stations whose name holds `query`, any case; every station for a blank query. */
export function matching<T extends Cyclable>(stations: readonly T[], query: string): T[] {
  const needle = query.trim().toLowerCase();
  return stations.filter((each) => each.name.toLowerCase().includes(needle));
}
