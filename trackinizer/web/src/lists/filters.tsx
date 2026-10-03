import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { InquiryRow } from "../api/inquiries";
import type { Enums } from "../api/meta";
import { useMeta, useProfile } from "../app/boot";
import { type ChoiceField, filterFields, type ListQuery, NO_PRIORITY } from "../query/query";
import {
  Avatar,
  capitalize,
  JudgementGlyph,
  LabelDot,
  PRIORITY_NAMES,
  PriorityGlyph,
  StatusGlyph,
} from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { KindIcon, kindLook } from "../ui/kinds";
import { Menu, type MenuOption } from "./Menu";

/** What the Filter menu calls each field. */
export const FIELD_NAMES: { readonly [field in ChoiceField]: string } = {
  kind: "Kind",
  status: "Status",
  priority: "Priority",
  judgement: "Judgement",
  owner: "Owner",
  labels: "Label",
};

/**
 * The Filter menu: pick a field, then tick its values.
 *
 * Kind-specific fields are offered only on lists that hold that kind. Owners and
 * labels are the ones on rows of the list's kinds that any list loaded, plus Me
 * and anything typed, since the server has no list of them.
 */
export function FilterMenu({
  query,
  rows,
  onToggle,
}: {
  query: ListQuery;
  rows: readonly InquiryRow[];
  onToggle: (field: ChoiceField, value: string) => void;
}) {
  const meta = useMeta();
  const { email } = useProfile();
  const queryClient = useQueryClient();
  const [field, setField] = useState<ChoiceField | null>(null);
  const fields = filterFields(query, meta.fieldOwners);
  const options = field
    ? valueOptions(field, { query, rows: [...rows, ...loadedRows(queryClient, query.kinds)], enums: meta.enums, email })
    : fields.map((f) => ({ value: f, label: FIELD_NAMES[f], icon: <Icon name="filter" size={14} /> }));
  const typedValue = field === "owner" || field === "labels" ? field : null;
  return (
    <Menu
      label={field ? `${FIELD_NAMES[field]} is…` : "Filter by…"}
      trigger={
        <button type="button" className="btn ghost">
          <Icon name="filter" size={14} />
          Filter
        </button>
      }
      options={options}
      multi
      onPick={(value) => {
        if (field) onToggle(field, value);
        else setField(fields.find((f) => f === value) ?? null);
      }}
      create={typedValue ? (typed) => `${FIELD_NAMES[typedValue]} is “${typed}”` : undefined}
      onBack={field ? () => setField(null) : undefined}
      onClose={() => setField(null)}
    />
  );
}

/** The chips under the tools: one per filtered field, each removable. */
export function FilterChips({
  query,
  onRemove,
  onClear,
}: {
  query: ListQuery;
  onRemove: (field: ChoiceField) => void;
  onClear: () => void;
}) {
  if (query.choices.length === 0) return null;
  return (
    <div className="fchips">
      {query.choices.map(({ field, values }) => (
        <span key={field} className="fchip">
          <span className="k">{FIELD_NAMES[field]}</span>
          {values.length > 1 ? "is any of" : "is"} {values.map((value) => valueName(field, value)).join(", ")}
          <button type="button" aria-label={`Remove the ${FIELD_NAMES[field]} filter`} onClick={() => onRemove(field)}>
            <Icon name="x" size={12} />
          </button>
        </span>
      ))}
      <button type="button" className="btn ghost" onClick={onClear}>
        Clear
      </button>
    </div>
  );
}

/** How a chip names one ticked value. */
function valueName(field: ChoiceField, value: string): string {
  if (field === "kind") return kindLook(value).plural;
  if (field === "status" || field === "judgement") return capitalize(value);
  if (field === "priority") return PRIORITY_NAMES[Number(value)] ?? "No priority";
  return value;
}

/**
 * The rows of `kinds` on every list page the cache holds. A filter narrows the
 * rows shown, and a label or owner it left out is still one to offer: picking a
 * second label after a first would otherwise mean typing it.
 */
function loadedRows(queryClient: QueryClient, kinds: readonly string[]): InquiryRow[] {
  return queryClient
    .getQueriesData<InquiryRow[]>({ queryKey: ["inquiries", "list"] })
    .flatMap(([, page]) => page ?? [])
    .filter((row) => kinds.includes(row.kind));
}

/** The values the menu offers for `field`, the ticked ones checked. */
function valueOptions(
  field: ChoiceField,
  { query, rows, enums, email }: { query: ListQuery; rows: readonly InquiryRow[]; enums: Enums; email: string },
): MenuOption[] {
  const ticked = query.choices.find((choice) => choice.field === field)?.values ?? [];
  const option = (value: string, label: string, icon?: MenuOption["icon"], hint?: string): MenuOption => ({
    value,
    label,
    icon,
    hint,
    checked: ticked.includes(value),
  });
  switch (field) {
    case "kind":
      return query.kinds.map((kind) => option(kind, kindLook(kind).plural, <KindIcon kind={kind} />));
    case "status":
      return (enums.status ?? [])
        .filter((status) => query.tab === "all" || status !== "active")
        .map((status) => option(status, capitalize(status), <StatusGlyph status={status} />));
    case "judgement":
      return (enums.judgement ?? []).map((j) => option(j, capitalize(j), <JudgementGlyph judgement={j} />));
    case "priority":
      return [
        ...PRIORITY_NAMES.map((name, band) => option(String(band), name, <PriorityGlyph priority={band * 10} />)),
        option(NO_PRIORITY, "No priority", <PriorityGlyph priority={null} />),
      ];
    case "owner": {
      const others = distinct([...rows.map((row) => row.owner ?? ""), ...ticked]).filter((o) => o !== email);
      return [
        option(email, "Me", <Avatar actor={email} size={16} />, email),
        ...others.map((owner) => option(owner, owner, <Avatar actor={owner} size={16} />)),
      ];
    }
    case "labels":
      return distinct([...rows.flatMap((row) => row.labels ?? []), ...ticked]).map((label) =>
        option(label, label, <LabelDot label={label} />),
      );
  }
}

/** The non-empty values, once each, alphabetically. */
function distinct(values: readonly string[]): string[] {
  return [...new Set(values)]
    .filter(Boolean)
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
}
