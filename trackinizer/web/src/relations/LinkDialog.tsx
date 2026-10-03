import { useQueries, useQueryClient } from "@tanstack/react-query";
import { Dialog } from "radix-ui";
import { type KeyboardEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";
import type { Detail } from "../api/detail";
import { useMeta, useWriteMode } from "../app/boot";
import { keyCaps } from "../commands/registry";
import { detailQueries } from "../detail/queries";
import { jumpsFor, parseSearch } from "../palette/grammar";
import { SERVER_SECTION } from "../palette/options";
import { cachedInquiries, type Found, useServerSearch } from "../palette/sources";
import { StateGlyphs } from "../ui/glyphs";
import { Icon, type IconName } from "../ui/icons";
import { KindIcon, kindLook } from "../ui/kinds";
import { useToast } from "../ui/toast";
import { useWrite } from "../writes/useWrite";
import { WriteButton } from "../writes/WriteButton";
import { WriteStatus } from "../writes/WriteStatus";
import { addRelationEdit } from "./edits";
import { type RelationChoice, relationChoices, relationEdge } from "./topology";
// The picker is the palette's dialog with a context line and ticks.
import "../palette/palette.css";
import "./relations.css";

/**
 * Add relations to `detail`'s inquiry, as the mock's link palette does: pick the
 * relation, then one or more inquiries, then Add. Only the relations the edge
 * topology allows this kind are offered, and only inquiries of the kinds the
 * other end admits, never this inquiry or one already related that way.
 *
 * `preset` skips the first step, as Supersede with an existing inquiry does.
 * Focus goes back to `returnTo` on close.
 */
export function LinkDialog({
  detail,
  preset,
  returnTo,
  onClose,
}: {
  detail: Detail;
  preset: RelationChoice | null;
  returnTo: HTMLElement | null;
  onClose: () => void;
}) {
  const [choice, setChoice] = useState(preset);
  const content = useRef<HTMLDivElement>(null);
  return (
    <Dialog.Root open onOpenChange={(open) => open || onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="pal-backdrop" />
        <Dialog.Content
          ref={content}
          className="palette"
          aria-describedby={undefined}
          // The context line comes first but is not where typing goes.
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            content.current?.querySelector("input")?.focus();
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (returnTo?.isConnected) returnTo.focus();
          }}
        >
          <Dialog.Title className="sr-only">{preset ? `${preset.label}…` : "Add relation"}</Dialog.Title>
          {choice ? (
            <Targets
              key={`${choice.edgeKind}:${choice.direction}`}
              detail={detail}
              choice={choice}
              onBack={preset ? undefined : () => setChoice(null)}
              onDone={onClose}
            />
          ) : (
            <Choices detail={detail} onPick={setChoice} />
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** The first step: which relation, each named from this inquiry, with the kinds it links to. */
function Choices({ detail, onPick }: { detail: Detail; onPick: (choice: RelationChoice) => void }) {
  const { edges, kinds } = useMeta();
  const [text, setText] = useState("");
  const needle = text.trim().toLowerCase();
  const rows = relationChoices(detail.self.kind, edges)
    .filter((choice) => choice.label.toLowerCase().includes(needle))
    .map((choice) => ({
      key: `${choice.edgeKind}:${choice.direction}`,
      section: "Relation",
      pick: () => onPick(choice),
      body: (
        <>
          <Icon name={choice.direction === "out" ? "arrowR" : "arrowL"} size={15} />
          <span className="lbl">{choice.label}…</span>
          <span className="keys">
            {choice.targetKinds.length === kinds.length
              ? "any inquiry"
              : choice.targetKinds.map((kind) => kindLook(kind).plural.toLowerCase()).join(", ")}
          </span>
        </>
      ),
    }));
  return (
    <>
      <Context detail={detail}>Add relation</Context>
      <ComboList label="Relation" placeholder="Relation type…" icon="link" text={text} onText={setText} rows={rows} />
      {rows.length ? null : <div className="pal-empty">No relation matches “{text.trim()}”</div>}
      <div className="pal-foot">
        <span>
          <kbd>↑</kbd>
          <kbd>↓</kbd> navigate
        </span>
        <span>
          <kbd>↵</kbd> select
        </span>
        <span>
          <kbd>esc</kbd> close
        </span>
      </div>
    </>
  );
}

/** An inquiry that can be picked; a `Kind#seq` jump not yet loaded has no title or status. */
type Target = Pick<Found, "id" | "kind" | "seq"> & Partial<Pick<Found, "title" | "status" | "judgement">>;

/**
 * The second step: search and tick inquiries, then add one relation to each,
 * one request at a time. Enter ticks the active row, ⌘↵ adds, and Backspace in
 * an empty box goes back a step.
 *
 * Inquiries the app has loaded show while typing, `Kind#seq` and UUID jumps are
 * looked up, and the server is searched per admitted kind after a pause, as in
 * the palette. Rows ticked earlier stay listed, under Picked, when a new search
 * no longer shows them.
 *
 * Each add refetches both ends. If one fails, those added are untick and the
 * failed one stays ticked, marked, with the server's message; Add then tries
 * the rest again. While the adds run, the rows take no ticks and the step
 * stays: what is ticked is what is added.
 */
function Targets({
  detail,
  choice,
  onBack,
  onDone,
}: {
  detail: Detail;
  choice: RelationChoice;
  onBack?: () => void;
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const writer = useWrite();
  const toast = useToast();
  const writable = useWriteMode() === "enabled";
  const [text, setText] = useState("");
  const [picks, setPicks] = useState<ReadonlyMap<string, Target>>(new Map());
  const [failed, setFailed] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const self = detail.self;
  const q = text.trim();
  const linked = new Set((choice.direction === "out" ? detail.edges : detail.backlinks)[choice.edgeKind]?.map((peer) => peer.id));
  const offered = (target: Target) => choice.targetKinds.includes(target.kind) && target.id !== self.id && !linked.has(target.id);
  const cached = cachedInquiries(queryClient).filter(offered);
  const jumps = jumpsFor(q, choice.targetKinds);
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
  const { searches } = useServerSearch(text, choice.targetKinds, { immediate: false });

  const rows: Row[] = [];
  const listed = new Set<string>();
  const list = (section: string, target: Target) => {
    if (listed.has(target.id) || !offered(target)) return;
    listed.add(target.id);
    rows.push(targetRow(section, target));
  };
  const targetRow = (section: string, target: Target): Row => ({
    key: target.id,
    section,
    checked: picks.has(target.id),
    disabled: adding,
    pick: () => {
      if (adding) return;
      if (target.id === failed) setFailed(null);
      setPicks((held) => (held.has(target.id) ? without(held, target.id) : new Map(held).set(target.id, target)));
    },
    body: <TargetBody target={target} picked={picks.has(target.id)} failed={target.id === failed} />,
  });
  for (const route of jumps) {
    const found = cached.find((row) => (route.name === "ref" ? row.kind === route.kind && row.seq === route.seq : row.id === route.id));
    if (found) list("Jump to", found);
  }
  for (const query of fetched) if (query.data) list("Jump to", query.data.self);
  const matches = q ? parseSearch(q) : () => true;
  cached
    .filter((found) => !listed.has(found.id) && matches(found))
    .slice(0, q ? 8 : 30)
    .forEach((found) => list("Inquiries", found));
  for (const search of searches) search.hits?.forEach((found) => list(SERVER_SECTION, found));
  const unlisted = [...picks.values()].filter((target) => !listed.has(target.id)).map((target) => targetRow("Picked", target));

  const missing = [
    ...refs.filter((_, index) => lookups[index]?.isError).map((route) => `${route.kind}#${route.seq}`),
    ...ids.filter((_, index) => fetched[index]?.isError),
  ];
  const failures = [...new Set(searches.flatMap((search) => (search.error ? [search.error.message] : [])))];
  const status = searches.some((search) => search.pending)
    ? "Searching the server…"
    : [...missing.map((ref) => `No ${ref}.`), ...failures].join(" ");

  const add = async () => {
    if (!picks.size || !writable || adding) return;
    setFailed(null);
    setAdding(true);
    const targets = [...picks.values()];
    let added = 0;
    for (const target of targets) {
      // Null too once the picker is closed, which stops the adds.
      const result = await writer.run(addRelationEdit(relationEdge(self.id, choice, target.id)));
      if (result === null) {
        setFailed(target.id);
        break;
      }
      added += 1;
      setPicks((held) => without(held, target.id));
    }
    setAdding(false);
    const what = `${refOf(self)} ${choice.label.toLowerCase()}`;
    if (added === 1) toast(`Linked: ${what} ${refOf(targets[0]!)}`);
    else if (added > 1) toast(`Linked ${added} inquiries: ${what} each`);
    if (added === targets.length) onDone();
  };

  return (
    <>
      <Context detail={detail}>
        {choice.label}
        <Icon name="arrowR" size={12} />
        pick one or more {choice.targetKinds.map((kind) => kindLook(kind).plural.toLowerCase()).join(", ")}
      </Context>
      <ComboList
        label={`${choice.label}: search inquiries`}
        placeholder="Search inquiries to link, or type Issue#412…"
        icon="link"
        text={text}
        onText={setText}
        rows={[...unlisted, ...rows]}
        multi
        onKey={(event) => {
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
            event.preventDefault();
            void add();
          } else if (event.key === "Backspace" && text === "" && onBack && !adding) {
            event.preventDefault();
            onBack();
          }
        }}
      />
      {rows.length + unlisted.length === 0 && q && !searches.some((search) => search.pending) ? (
        <div className="pal-empty">No inquiries to link for “{q}”</div>
      ) : null}
      <p className="pal-status" role="status">
        {status}
      </p>
      <div className="lk-status">
        <WriteStatus state={writer.state} />
      </div>
      <div className="pal-foot">
        <span>
          <kbd>↑</kbd>
          <kbd>↓</kbd> navigate
        </span>
        <span>
          <kbd>↵</kbd> tick
        </span>
        {onBack ? (
          <span>
            <kbd>⌫</kbd> back
          </span>
        ) : null}
        <span>
          <kbd>esc</kbd> close
        </span>
        <WriteButton className="btn primary" pending={adding} disabled={!picks.size || !writable} onClick={() => void add()}>
          {picks.size ? `Add ${picks.size} relation${picks.size === 1 ? "" : "s"}` : "Add relations"} <kbd>{keyCaps("$mod+Enter")[0]}</kbd>
        </WriteButton>
      </div>
    </>
  );
}

function TargetBody({ target, picked, failed }: { target: Target; picked: boolean; failed: boolean }) {
  return (
    <>
      <span className={picked ? "pal-check on" : "pal-check"} aria-hidden="true">
        {picked ? <Icon name="check" size={11} /> : null}
      </span>
      <KindIcon kind={target.kind} size={15} />
      <span className="ref-t">{refOf(target)}</span>
      {target.status ? <StateGlyphs status={target.status} judgement={target.judgement} /> : null}
      <span className="lbl">{target.title ?? ""}</span>
      {failed ? <span className="lk-err">Not added</span> : null}
    </>
  );
}

/** The inquiry the dialog adds relations to, above its input. */
function Context({ detail, children }: { detail: Detail; children: ReactNode }) {
  return (
    <div className="pal-ctx">
      <span className="lk-self">
        <KindIcon kind={detail.self.kind} size={12} />
        {refOf(detail.self)}
      </span>
      {children}
    </div>
  );
}

/** One row of a step's listbox, under its section's heading. */
type Row = {
  readonly key: string;
  readonly section: string;
  readonly body: ReactNode;
  /** Ticked, in a list that ticks several. */
  readonly checked?: boolean;
  /** Takes no pick for now. */
  readonly disabled?: boolean;
  readonly pick: () => void;
};

/**
 * A search box over a listbox, in the palette's WAI-ARIA pattern: focus stays
 * in the box, the arrows move the active row (tracked by key, so rows arriving
 * later never move it), Enter picks it, and `onKey` gets every other key.
 * `multi` makes each row's selected state its tick.
 */
function ComboList({
  label,
  placeholder,
  icon,
  text,
  onText,
  rows,
  multi = false,
  onKey,
}: {
  label: string;
  placeholder: string;
  icon: IconName;
  text: string;
  onText: (text: string) => void;
  rows: readonly Row[];
  multi?: boolean;
  onKey?: (event: KeyboardEvent<HTMLInputElement>) => void;
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
      event.preventDefault();
      if (rows.length) setActiveKey(rows[(active + step + rows.length) % rows.length]!.key);
    } else if (event.key === "Enter" && !event.metaKey && !event.ctrlKey) {
      event.preventDefault();
      if (active >= 0) pick(rows[active]!);
    } else {
      onKey?.(event);
    }
  };
  const sections: { section: string; rows: { row: Row; index: number }[] }[] = [];
  rows.forEach((row, index) => {
    const last = sections.at(-1);
    if (last?.section === row.section) last.rows.push({ row, index });
    else sections.push({ section: row.section, rows: [{ row, index }] });
  });
  return (
    <>
      <div className="pal-input">
        <Icon name={icon} size={18} />
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
      <div className="pal-list" id={`${id}-list`} role="listbox" aria-label={label} aria-multiselectable={multi || undefined}>
        {sections.map(({ section, rows: shown }, n) => (
          <div key={`${n}-${section}`} role="group" aria-labelledby={`${id}-section-${n}`}>
            <div className="pal-sec" id={`${id}-section-${n}`}>
              {section}
            </div>
            {shown.map(({ row, index }) => (
              <div
                key={row.key}
                id={`${id}-${index}`}
                role="option"
                aria-selected={multi ? Boolean(row.checked) : index === active}
                aria-disabled={row.disabled || undefined}
                className={index === active ? "pal-item is-active" : "pal-item"}
                // Keep focus in the box, where the arrows and Enter work.
                onMouseDown={(event) => event.preventDefault()}
                onPointerMove={() => row.key !== activeKey && setActiveKey(row.key)}
                onClick={() => pick(row)}
              >
                {row.body}
              </div>
            ))}
          </div>
        ))}
      </div>
    </>
  );
}

function without<Value>(map: ReadonlyMap<string, Value>, key: string): ReadonlyMap<string, Value> {
  const rest = new Map(map);
  rest.delete(key);
  return rest;
}

function refOf({ kind, seq }: { kind: string; seq: number }): string {
  return `${kind}#${seq}`;
}
