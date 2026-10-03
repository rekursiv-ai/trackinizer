import { editableFields } from "../api/fields";
import {
  type BatchEdge,
  type BatchItem,
  type CreateBody,
  type InquiryKind,
  isInquiryKind,
} from "../api/inquiries";
import type { EdgeTopology, FieldOwners } from "../api/meta";
import { type Field, isUnset, kindFields } from "../detail/fields";
import { inputKind, parseInput } from "../editors/values";
import { type RelationChoice, relationChoices } from "../relations/topology";
import type { Route } from "../router/route";
import { kindLook } from "../ui/kinds";
import { type Edit, short } from "../writes/edits";
import { batchRequest, createRequest, type WriteRequest } from "../writes/requests";

/** A kind a person can create here: every kind but AgentSession. */
export type CreatableKind = Exclude<InquiryKind, "AgentSession">;

/**
 * The kinds a person can create here, by any path (a create form, or a new
 * inquiry that supersedes one): the server's, less AgentSession, which `trax
 * run` records and nobody types in (the old UI's `VIEW_ONLY_KINDS`). A kind this
 * build's schema does not name has no create body to type, so it is left out.
 */
export function creatableKinds(kinds: readonly string[]): CreatableKind[] {
  return kinds.filter((kind): kind is CreatableKind => isInquiryKind(kind) && kind !== "AgentSession");
}

/** The kind New makes from `route`: the kind it shows, when that can be created, else the first that can. */
export function createKindFor(route: Route, creatable: readonly InquiryKind[]): InquiryKind | undefined {
  const shown = "kind" in route ? route.kind : null;
  return creatable.find((kind) => kind === shown) ?? creatable[0];
}

/** A relation a new inquiry can be created with: an edge, or a field that lists other inquiries' ids. */
export type RelationOption = {
  /** `narrows:out`, `proves:in`, or `field:codechanges`. */
  readonly key: string;
  /** Read from the new inquiry, in the server's words: `Narrows`, `Proved by`. */
  readonly label: string;
  /** The kinds the other end may be, PascalCase. */
  readonly targetKinds: readonly string[];
  /** The edge, stored child to parent, and which end the new inquiry is; a list field has none. */
  readonly edge?: Pick<RelationChoice, "edgeKind" | "direction">;
};

/** An inquiry a relation points at. */
export type Target = { readonly id: string; readonly kind: InquiryKind; readonly seq: number; readonly title?: string };

/** One relation picked on the form. */
export type Related = { readonly option: RelationOption; readonly target: Target };

/**
 * An unsaved inquiry: what the form holds until Create. It is plain data, so
 * it can wait in `localStorage` while the user signs in again.
 */
export type Draft = {
  readonly kind: InquiryKind;
  readonly title: string;
  readonly description: string;
  /** What is typed in each of the kind's own fields, by field name. */
  readonly fields: { readonly [field: string]: string };
  /** The chips' values, by field name; `null` and `[]` are unset. */
  readonly chips: { readonly [field: string]: unknown };
  readonly relations: readonly Related[];
};

/** The fields the form sets through chips, as the mock's create dialog does, in its order. */
export const CHIPS = ["judgement", "status", "priority", "issue_kind", "owner", "subscribers", "labels"] as const;

/** A new draft of `kind`: its chips and fields at their defaults, nothing else set. */
export function emptyDraft(kind: InquiryKind): Draft {
  return { kind, title: "", description: "", fields: fieldDefaults(kind), chips: chipDefaults(kind), relations: [] };
}

/**
 * `draft` as a draft of `kind`: the title, the description and the chips both
 * kinds have keep their values, as do relations the new kind can take to the
 * same inquiries. Fields of the old kind alone go.
 */
export function switchKind(draft: Draft, kind: InquiryKind, edges: EdgeTopology): Draft {
  const chips = chipDefaults(kind);
  const options = new Map(relationOptions(kind, edges).map((option) => [option.key, option]));
  return {
    ...emptyDraft(kind),
    title: draft.title,
    description: draft.description,
    chips: { ...chips, ...Object.fromEntries(Object.entries(draft.chips).filter(([name]) => name in chips)) },
    relations: draft.relations.flatMap(({ option, target }) => {
      const same = options.get(option.key);
      return same?.targetKinds.includes(target.kind) ? [{ option: same, target }] : [];
    }),
  };
}

/** The draft Create more leaves: the chips kept, the rest empty for the next inquiry. */
export function nextDraft(draft: Draft): Draft {
  return { ...emptyDraft(draft.kind), chips: draft.chips };
}

/**
 * Every relation an inquiry of `kind` can be created with: each the edge
 * topology gives the kind, in the server's order, then its fields that list
 * other inquiries.
 */
export function relationOptions(kind: InquiryKind, edges: EdgeTopology): RelationOption[] {
  const choices = relationChoices(kind, edges).map(({ edgeKind, direction, label, targetKinds }) => ({
    key: `${edgeKind}:${direction}`,
    label,
    targetKinds,
    edge: { edgeKind, direction },
  }));
  const lists = Object.entries(ID_LISTS[kind] ?? {}).map(([field, target]) => ({
    key: `field:${field}`,
    label: kindLook(target).plural,
    targetKinds: [target],
  }));
  return [...choices, ...lists];
}

/**
 * The fields of `kind` the form asks for, in the detail's order: those only this
 * kind has, less the chips and the fields that list inquiries, which are
 * relations here.
 *
 * Every such field with an edit route is also a field of the kind's create body,
 * with the same value type (checked against the schema on 2026-09-27); the server
 * refuses any other key with 422, and an empty field is never sent.
 */
export function formFields(kind: InquiryKind, fieldOwners: FieldOwners): Field[] {
  const owner = kind.toLowerCase();
  const stub = { id: "", kind, seq: 0, title: "", status: "", created: "", modified: "" };
  return kindFields(stub, fieldOwners).filter(
    ({ name, route }) =>
      route && fieldOwners[name] === owner && !(CHIPS as readonly string[]).includes(name) && !(name in (ID_LISTS[kind] ?? {})),
  );
}

/**
 * The one write that creates `draft`'s inquiry, or what stops it being sent.
 *
 * It is `POST /api/inquiries/<kind>` when the kind's create body takes every
 * relation picked, else one `POST /api/inquiries/batch` whose edges carry the
 * rest from the new row: either way the inquiry and all its relations land
 * together or not at all. The edit resolves with the new inquiry's id; the
 * inquiries at the other ends refetch, since they show the new relation. Its
 * toast says it was created, even when the form was closed meanwhile.
 */
export function createEdit(draft: Draft, fields: readonly Field[]): { readonly edit: Edit<string> } | { readonly error: string } {
  const body: { [field: string]: unknown } = { title: draft.title.replace(/\s+/g, " ").trim() };
  const description = draft.description.trim();
  if (description) body.description = description;
  const errors: string[] = [];
  for (const field of fields) {
    const parsed = parseField(field, draft.fields[field.name] ?? "");
    if ("error" in parsed) errors.push(`${field.look.label}: ${parsed.error}`);
    else if (!isUnset(parsed.value)) body[field.name] = parsed.value;
  }
  if (errors.length) return { error: errors.join("\n") };
  for (const [name, value] of Object.entries(draft.chips)) if (!isUnset(value)) body[name] = value;
  const edges: BatchEdge[] = [];
  for (const { option, target } of draft.relations) {
    const inline = INLINE[draft.kind]?.[option.key];
    if (inline) appendLists(body, inline(target));
    else edges.push(batchEdge(option, target));
  }
  // The keys and value types come from the schema: the kind's fields from the
  // field-type map, the chips from the fields they are named for, and the
  // relations from `INLINE`, whose shapes `tsc` checks. The server checks again.
  const request = edges.length
    ? mapResult(batchRequest([{ ...body, kind: draft.kind } as BatchItem], edges), ({ ids }) => ids[0]!)
    : mapResult(createRequest(draft.kind, body as CreateBody<InquiryKind>), ({ id }) => id);
  const done = `Created ${kindLook(draft.kind).one} “${short(String(body.title))}”`;
  return { edit: { request, touches: draft.relations.map(({ target }) => target.id), creates: (id) => [id], done } };
}

/** Append each list in `lists` to `body`'s list of the same name. */
function appendLists(body: { [field: string]: unknown }, lists: object): void {
  for (const [name, items] of Object.entries(lists)) body[name] = [...asArray(body[name]), ...asArray(items)];
}

function asArray(value: unknown): readonly unknown[] {
  return Array.isArray(value) ? value : [];
}

/** A field's text as the value to send: `null` when empty, and a list field's comma-separated items as a list. */
function parseField(field: Field, text: string): { readonly value: unknown } | { readonly error: string } {
  if (field.route!.value.type !== "array") return parseInput(text, inputKind(field.route!, field.look));
  return { value: text.split(",").map((item) => item.trim()).filter(Boolean) };
}

/**
 * Each chip `kind` has, at the value a new draft starts from: the mock's and the
 * old UI's defaults (P2, a task, active, unproven), and unset for the rest, so an
 * owner is only ever set by choice.
 */
function chipDefaults(kind: InquiryKind): { readonly [field: string]: unknown } {
  const routes = editableFields(kind);
  return Object.fromEntries(
    CHIPS.filter((name) => routes[name]).map((name) => [name, CHIP_DEFAULTS[name] ?? (routes[name]!.value.type === "array" ? [] : null)]),
  );
}

const CHIP_DEFAULTS: { readonly [field: string]: unknown } = {
  status: "active",
  priority: 20,
  issue_kind: ["task"],
  judgement: "unproven",
};

/**
 * The text each of `kind`'s fields starts with: the old UI's 0.5 confidence for
 * a Belief, which the server would otherwise store as no confidence at all
 * (parity bug B8). The rest start empty.
 */
function fieldDefaults(kind: InquiryKind): { readonly [field: string]: string } {
  const routes = editableFields(kind);
  return Object.fromEntries(Object.entries(FIELD_DEFAULTS).filter(([name]) => routes[name]));
}

const FIELD_DEFAULTS: { readonly [field: string]: string } = { confidence: "0.5" };

/**
 * Each kind's fields that hold other inquiries' ids, with the kind they name.
 * The schema types them as lists of UUIDs and cannot say which kind; the server
 * checks each id names one (`validate_list_references`).
 */
const ID_LISTS: { readonly [Kind in InquiryKind]?: { readonly [field in keyof CreateBody<Kind>]?: InquiryKind } } = {
  Experiment: { codechanges: "CodeChange" },
};

/** A kind's relations its create body takes itself, by option key, as the body's own fields. */
type Inline<Kind extends InquiryKind> = { readonly [option: string]: (target: Target) => Partial<CreateBody<Kind>> };

/**
 * The relations each kind's create body takes, so the one create request holds
 * them (`wire/bodies.py`); any other is a batch edge. A `narrows` goes with no
 * priority under its parent, and a citation with the server's default valence;
 * both can be annotated on the detail afterwards.
 */
const INLINE: { readonly [Kind in InquiryKind]?: Inline<Kind> } = {
  Issue: {
    "narrows:out": (target) => ({ narrows: [[target.id, null]] }),
    "requires:out": (target) => ({ requires: [target.id] }),
  },
  Belief: {
    "proves:in": (target) => ({ proved_by: [citation(target)] }),
    "favors:in": (target) => ({ favored_by: [citation(target)] }),
  },
  Experiment: {
    "field:codechanges": (target) => ({ codechanges: [target.id] }),
  },
};

/** A citation by `target`; the topology admits no Issue as a citer, and the server checks it too. */
function citation({ id, kind }: Target) {
  if (kind === "Issue") throw new Error("An issue cannot cite a claim.");
  return { artifact_id: id, artifact_kind: kind };
}

/** The batch edge between the new row, item 0, and `target`, stored child to parent. */
function batchEdge(option: RelationOption, target: Target): BatchEdge {
  const { edgeKind, direction } = option.edge!;
  // The edge kind is the server's own, from `/api/meta/edges`.
  const edge_kind = edgeKind as BatchEdge["edge_kind"];
  return direction === "out" ? { edge_kind, from_index: 0, to_id: target.id } : { edge_kind, from_id: target.id, to_index: 0 };
}

/**
 * A create `request`, resolving with `pick` of its result. It has no
 * `reconcile`: the server replays a create's key.
 */
function mapResult<Result, Picked>({ route, key, send }: WriteRequest<Result>, pick: (result: Result) => Picked): WriteRequest<Picked> {
  return { route, key, send: () => send().then(pick) };
}
