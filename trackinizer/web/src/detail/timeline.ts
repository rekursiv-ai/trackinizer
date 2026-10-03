import type { Change, Detail, Peer, Snapshot } from "../api/detail";
import type { EdgeTopology } from "../api/meta";
import { type Field, isUnset, usd } from "./fields";
import { sentence } from "./relationGroups";

/** One line of the activity: a change, or a run of upstream alerts folded into one. */
export type ActivityItem =
  | { readonly type: "change"; readonly change: Change }
  | { readonly type: "alerts"; readonly changes: readonly Change[] };

/** An inquiry a change names: the loaded neighbour when it is still linked. */
export type Mention = { readonly id: string; readonly kind: string; readonly peer: Peer | undefined };

/** What a change did, as words and the inquiries it names. */
export type Phrase = readonly (string | Mention)[];

/**
 * The changes oldest first, with each run of consecutive `dependency_changed`
 * alerts folded into one item.
 *
 * The server sends the newest first. Alerts are the server's re-assessment
 * notes on a parent when a child changes, and on a hub they would otherwise
 * bury every other line.
 */
export function activityItems(changes: readonly Change[]): ActivityItem[] {
  const items: ActivityItem[] = [];
  for (const change of [...changes].reverse()) {
    const last = items.at(-1);
    if (change.kind !== ALERT) items.push({ type: "change", change });
    else if (last?.type === "alerts") items[items.length - 1] = { type: "alerts", changes: [...last.changes, change] };
    else items.push({ type: "alerts", changes: [change] });
  }
  return items;
}

/**
 * Describe `change` in words, from its kind and its old and new snapshots.
 *
 * A field edit names the field as the detail labels it, and a list edit what it
 * added and removed; long text is not quoted. An edge event names the relation
 * as read from this inquiry, when the edge is still there to tell its direction.
 */
export function describeChange(
  change: Change,
  { detail, topology, fields }: { detail: Detail; topology: EdgeTopology; fields: readonly Field[] },
): Phrase {
  switch (change.kind) {
    case "created":
      return ["created this"];
    case "purged":
      return ["purged this"];
    case "marginal_cost":
      return [costText(change)];
    case "edge_added":
    case "edge_removed":
    case "edge_annotation_changed":
    case ALERT:
      return edgePhrase(change, detail, topology);
  }
  const field = fieldOf(change, fields);
  if (!field) return [sentence(change.kind)];
  const [before, after] = [change.old[change.kind], change.new[change.kind]];
  const { label, place } = field.look;
  if (Array.isArray(before) || Array.isArray(after)) return [listText(label, before, after)];
  if (isUnset(after)) return [`cleared ${label}`];
  if (place === "text" || place === "json") return [isUnset(before) ? `set ${label}` : `edited ${label}`];
  return [isUnset(before) ? `set ${label} to ${short(after)}` : `changed ${label} from ${short(before)} to ${short(after)}`];
}

const ALERT = "dependency_changed";

/**
 * The field a change edited. Its kind is the field's flat storage name: bare for
 * a field every kind has or one that already names its kind (`issue_kind`),
 * `<kind>_<field>` otherwise (`issue_priority`).
 */
function fieldOf(change: Change, fields: readonly Field[]): Field | undefined {
  const prefix = `${change.subject_kind.toLowerCase()}_`;
  const bare = change.kind.startsWith(prefix) ? change.kind.slice(prefix.length) : change.kind;
  return fields.find((field) => field.name === change.kind) ?? fields.find((field) => field.name === bare);
}

function edgePhrase(change: Change, detail: Detail, topology: EdgeTopology): Phrase {
  const side: Snapshot = change.new.peer_id ? change.new : change.old;
  const id = String(side.peer_id);
  const peerKind = String(side.peer_kind);
  const edgeKind = String(side.peer_edge_kind);
  const rule = topology[edgeKind];
  const direction = edgeDirection({ change, detail, id, edgeKind, peerKind, rule });
  // A removed edge's neighbour may still be linked by another edge, which names it.
  const linked = [...Object.values(detail.edges), ...Object.values(detail.backlinks)].flat();
  const mention: Mention = { id, kind: peerKind, peer: linked.find((peer) => peer.id === id) };
  if (change.kind === ALERT) return ["upstream change in ", mention];
  const verb = { edge_added: "added", edge_removed: "removed" }[change.kind] ?? "annotated";
  // The change row does not say which end this inquiry was on; unknown, the
  // phrase names the edge kind alone rather than guess a direction.
  const relation =
    direction === null
      ? `${verb} a ${edgeKind.replaceAll("_", " ")} relation with `
      : `${verb} relation ${sentence((direction === "out" ? rule?.forward : rule?.inverse) ?? edgeKind)} `;
  if (verb !== "annotated") return [relation, mention];
  const edits = Object.keys({ ...change.old, ...change.new })
    .filter((key) => key.startsWith("edge_") && short(change.old[key]) !== short(change.new[key]))
    .map((key) => `${key.slice("edge_".length)} ${short(change.old[key])} → ${short(change.new[key])}`);
  return [relation, mention, ...(edits.length ? [`: ${edits.join(", ")}`] : [])];
}

/**
 * Which end of an edge this inquiry is: `out` when it is the child, `in` the
 * parent, or null when nothing says. An edge still there says. For one since
 * removed: the server writes the child's half first, caused by nothing for a
 * user's own edge, and the parent's caused by it, so an uncaused half is the
 * child's, as the Activity feed reads it. A caused half may be either, and then
 * only the kinds the edge joins can say, when this pair fits one way alone (a
 * Belief is only ever proved, never proves). Issue to Issue fits both.
 */
function edgeDirection({
  change,
  detail,
  id,
  edgeKind,
  peerKind,
  rule,
}: {
  change: Change;
  detail: Detail;
  id: string;
  edgeKind: string;
  peerKind: string;
  rule: EdgeTopology[string] | undefined;
}): "out" | "in" | null {
  if (detail.edges[edgeKind]?.some((peer) => peer.id === id)) return "out";
  if (detail.backlinks[edgeKind]?.some((peer) => peer.id === id)) return "in";
  if (change.caused_by === null) return "out";
  if (!rule) return null;
  const out = rule.from_kinds.includes(change.subject_kind) && rule.to_kinds.includes(peerKind);
  const inbound = rule.to_kinds.includes(change.subject_kind) && rule.from_kinds.includes(peerKind);
  return out === inbound ? null : out ? "out" : "in";
}

function listText(label: string, before: unknown, after: unknown): string {
  const [was, now] = [asList(before), asList(after)];
  const added = now.filter((item) => !was.includes(item));
  const removed = was.filter((item) => !now.includes(item));
  const parts = [
    ...(added.length ? [`added ${added.join(", ")} to ${label}`] : []),
    ...(removed.length ? [`removed ${removed.join(", ")} from ${label}`] : []),
  ];
  return parts.length ? parts.join("; ") : `edited ${label}`;
}

/** The spend a change added, per axis: the difference between its two sides. */
function costText(change: Change): string {
  const spend = (axis: string) => costOf(change.new, axis) - costOf(change.old, axis);
  const parts = [
    ["agent", spend("agent_usd")],
    ["resource", spend("resource_usd")],
  ] as const;
  const shown = parts.filter(([, delta]) => delta !== 0);
  return `recorded cost ${(shown.length ? shown : parts).map(([axis, delta]) => `${axis} ${money(delta)}`).join(", ")}`;
}

function costOf(side: Snapshot, axis: string): number {
  const cost = side.marginal_cost;
  const value = typeof cost === "object" && cost !== null ? (cost as { [axis: string]: unknown })[axis] : 0;
  return typeof value === "number" ? value : 0;
}

function money(delta: number): string {
  return `${delta < 0 ? "−" : "+"}${usd(Math.abs(delta))}`;
}

function asList(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

/** A value in a line of text: `none` when unset, and at most 60 characters. */
function short(value: unknown): string {
  if (isUnset(value)) return "none";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > 60 ? `${text.slice(0, 59)}…` : text;
}
