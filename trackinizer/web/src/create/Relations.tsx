import { useQueries, useQueryClient } from "@tanstack/react-query";
import { Popover } from "radix-ui";
import { type KeyboardEvent, type ReactNode, useEffect, useId, useState } from "react";
import { type InquiryKind, isInquiryKind } from "../api/inquiries";
import { useMeta } from "../app/boot";
import { detailQueries } from "../detail/queries";
import { jumpsFor, parseSearch } from "../palette/grammar";
import { SERVER_SECTION } from "../palette/options";
import { cachedInquiries, useServerSearch } from "../palette/sources";
import { StateGlyphs } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { KindIcon, kindLook } from "../ui/kinds";
import type { Related, RelationOption, Target } from "./draft";

/**
 * The draft's relations: those picked so far, each with its Remove button, and
 * Add relation, which picks the relation, then ticks inquiries at its other end,
 * as the detail's link picker does. Only the relations `options` offers, and only
 * inquiries of the kinds the other end admits, are offered. `disabled` turns
 * the buttons off.
 */
export function Relations({
  options,
  relations,
  disabled,
  onChange,
}: {
  options: readonly RelationOption[];
  relations: readonly Related[];
  disabled: boolean;
  onChange: (relations: readonly Related[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [option, setOption] = useState<RelationOption | null>(null);
  const toggle = (option: RelationOption, target: Target) => {
    const same = (related: Related) => related.option.key === option.key && related.target.id === target.id;
    onChange(relations.some(same) ? relations.filter((related) => !same(related)) : [...relations, { option, target }]);
  };
  return (
    <div className="cr-rels">
      {relations.length ? (
        <ul className="cr-rel-list" aria-label="Relations">
          {relations.map(({ option, target }) => (
            <li key={`${option.key}:${target.id}`} className="cr-rel">
              <span className="cr-rel-k">{option.label}</span>
              <KindIcon kind={target.kind} size={13} />
              <span className="mono">{refOf(target)}</span>
              <span className="cr-rel-t">{target.title ?? ""}</span>
              <button
                type="button"
                className="icon-btn"
                aria-label={`Remove ${option.label.toLowerCase()} ${refOf(target)}`}
                disabled={disabled}
                onClick={() => toggle(option, target)}
              >
                <Icon name="x" size={13} />
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      <Popover.Root
        open={open}
        onOpenChange={(next) => {
          setOpen(next);
          if (!next) setOption(null);
        }}
      >
        <Popover.Trigger asChild>
          <button type="button" className="chip-btn" disabled={disabled}>
            <Icon name="link" size={13} />
            Add relation
          </button>
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content className="list-menu cr-picker" align="start" sideOffset={4} aria-label={option ? `${option.label}…` : "Add relation…"}>
            {option ? (
              <Targets
                key={option.key}
                option={option}
                picked={new Set(relations.filter((related) => related.option.key === option.key).map(({ target }) => target.id))}
                onPick={(target) => toggle(option, target)}
                onBack={() => setOption(null)}
              />
            ) : (
              <Choices options={options} onPick={setOption} />
            )}
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    </div>
  );
}

/** The first step: which relation, each named from the new inquiry, with the kinds it links to. */
function Choices({ options, onPick }: { options: readonly RelationOption[]; onPick: (option: RelationOption) => void }) {
  const { kinds } = useMeta();
  const [text, setText] = useState("");
  const needle = text.trim().toLowerCase();
  const rows = options
    .filter((option) => option.label.toLowerCase().includes(needle))
    .map((option) => ({
      key: option.key,
      pick: () => onPick(option),
      body: (
        <>
          <Icon name={option.edge?.direction === "in" ? "arrowL" : "arrowR"} size={14} />
          <span className="lbl">{option.label}…</span>
          <span className="hint">
            {option.targetKinds.length === kinds.length
              ? "any inquiry"
              : option.targetKinds.map((kind) => kindLook(kind).plural.toLowerCase()).join(", ")}
          </span>
        </>
      ),
    }));
  return <Combo label="Add relation…" text={text} onText={setText} rows={rows} empty={`No relation matches “${text.trim()}”`} />;
}

/**
 * The second step: search and tick inquiries at the other end. Inquiries the app
 * has loaded show while typing, a `Kind#seq` or UUID is looked up, and the server
 * is searched per admitted kind after a pause, as in the palette. Backspace in
 * an empty box goes back a step.
 */
function Targets({
  option,
  picked,
  onPick,
  onBack,
}: {
  option: RelationOption;
  picked: ReadonlySet<string>;
  onPick: (target: Target) => void;
  onBack: () => void;
}) {
  const queryClient = useQueryClient();
  const [text, setText] = useState("");
  const q = text.trim();
  const admitted = <Found extends { readonly kind: string }>(found: Found): found is Found & { readonly kind: InquiryKind } =>
    isInquiryKind(found.kind) && option.targetKinds.includes(found.kind);
  const cached = cachedInquiries(queryClient).filter(admitted);
  const jumps = jumpsFor(q, option.targetKinds);
  const refs = jumps.flatMap((route) =>
    route.name === "ref" && !cached.some((found) => found.kind === route.kind && found.seq === route.seq) ? [route] : [],
  );
  const lookups = useQueries({ queries: refs.map((route) => ({ ...detailQueries.ref(route.kind, route.seq), retry: false })) });
  // A `Kind#seq` lookup names only the id: its detail, as a UUID's, gives the title a target shows.
  const ids = [
    ...jumps.flatMap((route) => (route.name === "lookup" && !cached.some((found) => found.id === route.id) ? [route.id] : [])),
    ...lookups.flatMap((lookup) => (lookup.data ? [lookup.data] : [])),
  ];
  const fetched = useQueries({ queries: ids.map((id) => ({ ...detailQueries.detail(id), retry: false })) });
  const { searches } = useServerSearch(text, option.targetKinds, { immediate: false });

  const rows: Row[] = [];
  const listed = new Set<string>();
  const list = (section: string, target: Target & { readonly status?: string; readonly judgement?: string | null }) => {
    if (listed.has(target.id)) return;
    listed.add(target.id);
    rows.push({
      key: target.id,
      section,
      checked: picked.has(target.id),
      pick: () => onPick({ id: target.id, kind: target.kind, seq: target.seq, title: target.title }),
      body: (
        <>
          <KindIcon kind={target.kind} size={14} />
          <span className="ref-t">{refOf(target)}</span>
          {target.status ? <StateGlyphs status={target.status} judgement={target.judgement} /> : null}
          <span className="lbl">{target.title ?? ""}</span>
          {picked.has(target.id) ? <Icon name="check" size={14} className="chk" /> : null}
        </>
      ),
    });
  };
  for (const route of jumps) {
    const found = cached.find((row) => (route.name === "ref" ? row.kind === route.kind && row.seq === route.seq : row.id === route.id));
    if (found) list("Jump to", found);
  }
  for (const query of fetched) if (query.data && admitted(query.data.self)) list("Jump to", query.data.self);
  const matches = q ? parseSearch(q) : () => true;
  cached
    .filter((found) => matches(found))
    .slice(0, q ? 8 : 30)
    .forEach((found) => list("Inquiries", found));
  for (const search of searches) search.hits?.filter(admitted).forEach((found) => list(SERVER_SECTION, found));

  const missing = [
    ...refs.filter((_, index) => lookups[index]?.isError).map((route) => `${route.kind}#${route.seq}`),
    ...ids.filter((_, index) => fetched[index]?.isError),
  ].map((name) => `No ${name}.`);
  const failures = [...new Set(searches.flatMap((search) => (search.error ? [search.error.message] : [])))];
  const status = searches.some((search) => search.pending) ? "Searching the server…" : [...missing, ...failures].join(" ");
  return (
    <>
      <Combo
        label={`${option.label}: search inquiries`}
        placeholder="Search, or type Issue#412…"
        text={text}
        onText={setText}
        rows={rows}
        multi
        onBack={onBack}
        empty={q && !status ? `No ${option.targetKinds.length === 1 ? kindLook(option.targetKinds[0]!).plural.toLowerCase() : "inquiries"} match “${q}”` : ""}
      />
      <p className="cr-picker-status" role="status">
        {status}
      </p>
    </>
  );
}

/** One row of a step's listbox, under its section's heading if it has one. */
type Row = {
  readonly key: string;
  readonly section?: string;
  readonly body: ReactNode;
  /** Ticked, in a list that ticks several. */
  readonly checked?: boolean;
  readonly pick: () => void;
};

/**
 * A search box over a listbox, as the app's menus are: focus stays in the box,
 * the arrows move the active row (kept by key, so rows arriving later never move
 * it), Enter picks it, and Backspace in an empty box calls `onBack`. `multi` makes
 * each row's selected state its tick.
 */
function Combo({
  label,
  placeholder = label,
  text,
  onText,
  rows,
  multi = false,
  onBack,
  empty,
}: {
  label: string;
  placeholder?: string;
  text: string;
  onText: (text: string) => void;
  rows: readonly Row[];
  multi?: boolean;
  onBack?: () => void;
  empty: string;
}) {
  const id = useId();
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const found = rows.findIndex((row) => row.key === activeKey);
  const active = found < 0 && rows.length ? 0 : found;
  useEffect(() => {
    document.getElementById(`${id}-${active}`)?.scrollIntoView({ block: "nearest" });
  }, [id, active]);
  const pick = (row: Row) => {
    setActiveKey(row.key);
    row.pick();
  };
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    const step = { ArrowDown: 1, ArrowUp: -1 }[event.key];
    if (step !== undefined) {
      if (rows.length) setActiveKey(rows[(active + step + rows.length) % rows.length]!.key);
    } else if (event.key === "Enter") {
      if (active >= 0) pick(rows[active]!);
    } else if (event.key === "Backspace" && text === "" && onBack) {
      onBack();
    } else {
      return;
    }
    event.preventDefault();
  };
  return (
    <>
      <div className="menu-search">
        <Icon name="search" size={14} />
        <input
          autoFocus
          role="combobox"
          aria-label={label}
          aria-expanded="true"
          aria-controls={`${id}-list`}
          aria-autocomplete="list"
          aria-activedescendant={active < 0 ? undefined : `${id}-${active}`}
          autoComplete="off"
          spellCheck={false}
          placeholder={placeholder}
          value={text}
          onChange={(event) => {
            onText(event.target.value);
            setActiveKey(null);
          }}
          onKeyDown={onKeyDown}
        />
      </div>
      <div className="menu-list" id={`${id}-list`} role="listbox" aria-label={label} aria-multiselectable={multi || undefined}>
        {rows.map((row, index) => (
          <div key={row.key} role="presentation">
            {row.section && row.section !== rows[index - 1]?.section ? (
              <div className="cr-sec" role="presentation">
                {row.section}
              </div>
            ) : null}
            <div
              id={`${id}-${index}`}
              role="option"
              aria-selected={multi ? Boolean(row.checked) : index === active}
              className={index === active ? "menu-item is-active" : "menu-item"}
              // Keep focus in the box, where the arrows and Enter work.
              onMouseDown={(event) => event.preventDefault()}
              onMouseMove={() => row.key !== activeKey && setActiveKey(row.key)}
              onClick={() => pick(row)}
            >
              {row.body}
            </div>
          </div>
        ))}
        {rows.length === 0 && empty ? <div className="menu-empty">{empty}</div> : null}
      </div>
    </>
  );
}

function refOf({ kind, seq }: { readonly kind: string; readonly seq: number }): string {
  return `${kind}#${seq}`;
}
