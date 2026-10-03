import type { ChangeKind, LoggedChange } from "../api/changes";
import type { Snapshot } from "../api/detail";
import { isUnset } from "../detail/fields";
import { sentence } from "../detail/relationGroups";
import { capitalize } from "../ui/glyphs";
import { kindLook } from "../ui/kinds";

/**
 * The tabs, as the mock names them, each a set of `change_log` kinds read in
 * one request: every kind the old UI's recent changes showed but the
 * `dependency_changed` alerts, most of the log, which have no tab. All reads
 * every tab's kinds, so it too is one request, and is not the raw log.
 */
export const KIND_TABS = [
  { tab: "status", label: "Status", kinds: ["status"] },
  { tab: "judgements", label: "Judgements", kinds: ["belief_judgement"] },
  { tab: "created", label: "Created", kinds: ["created", "purged"] },
  { tab: "relations", label: "Relations", kinds: ["edge_added", "edge_removed"] },
  { tab: "edits", label: "Edits", kinds: ["description", "title", "issue_priority", "labels", "owner"] },
] as const satisfies readonly { tab: string; label: string; kinds: readonly ChangeKind[] }[];

/** A tab: `all`, or one of `KIND_TABS`. */
export type Tab = "all" | (typeof KIND_TABS)[number]["tab"];

/** The change kinds a tab reads. */
export function tabKinds(tab: Tab): readonly ChangeKind[] {
  return KIND_TABS.filter((t) => tab === "all" || t.tab === tab).flatMap((t) => t.kinds);
}

/** Whether `value` names a tab, as a stored one read back may not. */
export function isTab(value: unknown): value is Tab {
  return value === "all" || KIND_TABS.some((t) => t.tab === value);
}

/** An inquiry a line names: a change's subject, or its edge's other end. */
export type Mention = { readonly id: string; readonly kind: string };

/** One line of the feed: a change, the inquiry it is about, and what it did. */
export type FeedItem = {
  readonly change: LoggedChange;
  readonly subject: Mention;
  /** Words, and the other inquiries they name. */
  readonly phrase: readonly (string | Mention)[];
};

/**
 * The feed's lines for `rows`, newest first.
 *
 * An edge change is written on both ends, the parent's row caused by the
 * child's, so a loaded pair reads as one line on the child, naming the edge as
 * the child states it: `added relation Requires Issue#398`. `labels` are the
 * server's `forward` edge labels, by edge kind.
 */
export function feedItems(rows: readonly LoggedChange[], labels: { readonly [edgeKind: string]: string }): FeedItem[] {
  const byId = new Map(rows.map((row) => [row.id, row]));
  const echoes = rows.filter((row) => {
    const cause = row.caused_by === null ? undefined : byId.get(row.caused_by);
    return cause !== undefined && isPair(cause, row);
  });
  const echoIds = new Set(echoes.map((row) => row.id));
  const paired = new Set(echoes.map((row) => row.caused_by));
  return rows
    .filter((row) => !echoIds.has(row.id))
    .map((change) => ({
      change,
      subject: { id: change.subject_id, kind: change.subject_kind },
      // Nothing causes the child's half of a user's edge, while the parent's
      // half is always caused by the child's.
      phrase: describe(change, labels, change.caused_by === null || paired.has(change.id)),
    }));
}

/** Every inquiry the lines name, once each, in order of first mention. */
export function mentioned(items: readonly FeedItem[]): Mention[] {
  const out = new Map<string, Mention>();
  for (const item of items) {
    for (const part of [item.subject, ...item.phrase]) {
      if (typeof part !== "string" && !out.has(part.id)) out.set(part.id, part);
    }
  }
  return [...out.values()];
}

/**
 * `Today`, `Yesterday`, or the weekday and date of `iso` in local time, as the
 * mock heads each day of the feed.
 */
export function dayLabel(iso: string, now: number): string {
  const day = new Date(iso);
  const days = Math.round((startOfDay(new Date(now)) - startOfDay(day)) / 86_400_000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  const sameYear = day.getFullYear() === new Date(now).getFullYear();
  return day.toLocaleDateString("en", {
    weekday: "long",
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
  });
}


/** A labels edit: the labels it added and removed. */
function labelsText(before: unknown, after: unknown): string {
  const [was, now] = [asList(before), asList(after)];
  const parts = [
    ["added", now.filter((label) => !was.includes(label))],
    ["removed", was.filter((label) => !now.includes(label))],
  ] as const;
  const said = parts
    .filter(([, labels]) => labels.length)
    .map(([verb, labels]) => `${verb} ${labels.length === 1 ? "label" : "labels"} ${labels.join(", ")}`);
  return said.length ? said.join(" and ") : "edited the labels";
}

function asList(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

function startOfDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

function isPair(child: LoggedChange, parent: LoggedChange): boolean {
  return (
    parent.caused_by === child.id &&
    parent.kind === child.kind &&
    parent.subject_id === edgeSide(child).peer_id &&
    edgeSide(parent).peer_id === child.subject_id &&
    edgeSide(parent).peer_edge_kind === edgeSide(child).peer_edge_kind
  );
}

/** The snapshot an edge change names its peer in: the new side of an added edge, the old of a removed one. */
function edgeSide(change: LoggedChange): Snapshot {
  return change.new.peer_id ? change.new : change.old;
}

/** What `change` did. A field edit's kind is the column it edited on both snapshots. */
function describe(
  change: LoggedChange,
  labels: { readonly [edgeKind: string]: string },
  child: boolean,
): readonly (string | Mention)[] {
  const [before, after] = [change.old[change.kind], change.new[change.kind]];
  switch (change.kind) {
    case "created":
      return [`created the ${kindLook(change.subject_kind).one}`];
    case "purged":
      return [`purged the ${kindLook(change.subject_kind).one}`];
    // A brief row cuts the title to 32 characters, and the line shows the current one.
    case "title":
      return ["changed the title"];
    case "issue_priority":
      return [
        isUnset(after)
          ? "cleared the priority"
          : isUnset(before)
            ? `set priority to ${String(after)}`
            : `changed priority from ${String(before)} to ${String(after)}`,
      ];
    case "owner":
      return [
        isUnset(after)
          ? "cleared the owner"
          : isUnset(before)
            ? `set the owner to ${String(after)}`
            : `changed the owner from ${String(before)} to ${String(after)}`,
      ];
    case "labels":
      return [labelsText(before, after)];
    case "status":
      return [`changed status from ${String(before)} to ${String(after)}`];
    case "belief_judgement":
      return [isUnset(after) ? "cleared the judgement" : `marked as ${capitalize(String(after))}`];
    case "description":
      return [isUnset(after) ? "cleared the description" : isUnset(before) ? "added a description" : "edited the description"];
    case "edge_added":
    case "edge_removed": {
      const side = edgeSide(change);
      const verb = change.kind === "edge_added" ? "added" : "removed";
      const edgeKind = String(side.peer_edge_kind);
      const peer = { id: String(side.peer_id), kind: String(side.peer_kind) };
      if (child) return [`${verb} relation ${sentence(labels[edgeKind] ?? edgeKind)} `, peer];
      // The parent's half alone: its row does not say which way the edge points.
      return [`${verb} a ${edgeKind.replaceAll("_", " ")} relation with `, peer];
    }
  }
  return [change.kind.replaceAll("_", " ")];
}
