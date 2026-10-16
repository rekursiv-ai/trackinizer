import { useQueries, useQueryClient } from "@tanstack/react-query";
import { useLayoutEffect, useRef } from "react";
import { inquiryKinds, listInquiries } from "../api/inquiries";
import { useMeta } from "../app/boot";
import { type Completion, type Target, useCompletion } from "../composer/CompletionField";
import type { FieldProps } from "../composer/Composer";
import { detailQueries } from "../detail/queries";
import { jumpsFor, parseSearch } from "../palette/grammar";
import { type Found, useLoaded, useServerSearch } from "../palette/sources";
import { KindIcon } from "../ui/kinds";

/** The most records the list shows. */
const MOST = 8;

/**
 * Chat's message box (Composer's `field`): `@` at the start or after
 * whitespace opens a list of records to cite, and a pick puts `Kind#seq` in the
 * `@` word's place. An `@` inside a word (an email address) opens nothing.
 *
 * With nothing after `@`: recently opened records, then loaded ones. With text:
 * the record a `Kind#seq` names exactly, once the server finds it (a missing
 * one is never offered); loaded records that match (`matching`); then, after
 * the palette's pause, the server's title and description hits
 * (`useServerSearch`) and the newest records of each kind the text's leading
 * letters are in (`@Ag`, `@session`: AgentSessions), which a cold cache has none
 * of. The server's rows the list showed for this `@` stay while the next answer
 * is on its way, as long as they still match, so refining the text does not
 * empty the list; once it answers, its answer stands. Each record shows once,
 * eight at most. Bare `@` offers a hint when no records are loaded; after any
 * text, the list shows only when there are matches. Keys are `useCompletion`'s.
 */
export function RecordField({ field, edit }: { field: FieldProps; edit: (text: string) => void }) {
  const { kinds } = useMeta();
  const queryClient = useQueryClient();
  const { target, render } = useCompletion({ field, edit, find: recordAt });
  const q = target?.query.trim() ?? "";
  const { cached, recent } = useLoaded(queryClient);
  const { searches, waiting } = useServerSearch(q, kinds, { immediate: false });
  const refs = target ? jumpsFor(q, kinds).flatMap((route) => (route.name === "ref" ? [route] : [])) : [];
  const unloaded = refs.filter((route) => !cached.some((found) => found.kind === route.kind && found.seq === route.seq));
  const lookups = useQueries({ queries: unloaded.map((route) => ({ ...detailQueries.ref(route.kind, route.seq), retry: false })) });
  const details = useQueries({
    queries: lookups.flatMap((lookup) => (lookup.data ? [{ ...detailQueries.detail(lookup.data), retry: false }] : [])),
  });

  const letters = /^[a-z]+/i.exec(q)?.[0].toLowerCase() ?? "";
  const named = target && letters && !waiting ? kinds.filter((kind) => kind.toLowerCase().includes(letters)) : [];
  const listed = useQueries({
    queries: named.map((kind) => ({
      queryKey: ["record-field", "kind", kind],
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        listInquiries({ kinds: inquiryKinds([kind]), filters: [], limit: MOST, offset: 0, fields: ["id", "kind", "seq", "title", "status", "modified"] }, { signal }),
      staleTime: 30_000,
      retry: false,
    })),
  });
  // The server's rows the list last showed, for this `@` only: offered again while the next answer is on its way.
  // Written after a render commits, so a render React discards never stands for what the list showed.
  const shown = useRef<{ readonly start: number; readonly rows: readonly Found[] } | null>(null);
  const busy = waiting || searches.some((search) => search.pending) || listed.some((list) => list.isPending) || lookups.some((lookup) => lookup.isPending);

  const rows: Found[] = [];
  const add = (found: Found) => {
    if (!rows.some((each) => each.id === found.id)) rows.push(found);
  };
  if (target && !q) {
    recent.forEach(add);
    cached.forEach(add);
  } else if (target) {
    const matches = matching(q);
    refs.forEach((route) => {
      const found = cached.find((each) => each.kind === route.kind && each.seq === route.seq);
      if (found) add(found);
    });
    details.forEach(({ data }) => {
      if (data) add({ id: data.self.id, kind: data.self.kind, seq: data.self.seq, title: data.self.title, status: data.self.status });
    });
    cached.filter(matches).forEach(add);
    // The search's hits are the server's answer to the text, field regexes included, which the page cannot run.
    searches.forEach((search) => search.hits?.forEach(add));
    // A kind's newest rows answer only its name: each must still hold the text, a `#seq` included.
    listed.forEach((list) => list.data?.filter(matches).forEach(add));
    const held = shown.current?.start === target.start ? shown.current.rows : [];
    if (busy) held.filter(matches).forEach(add);
  }
  const offered = rows.slice(0, MOST);
  const showing = target && q ? { start: target.start, rows: offered.filter((found) => !cached.some((each) => each.id === found.id)) } : null;
  useLayoutEffect(() => {
    shown.current = showing;
  });
  return render(offered.map(optionOf), "Records", q ? undefined : "Type to search records…");
}

/**
 * Whether a record matches `q`: its `Kind#seq` holds it, in any case (`@ag`,
 * `@session#4`), or its title or description does, in the server's grammar
 * (which no field regex matches here; only the server runs those).
 */
function matching(q: string): (found: Found) => boolean {
  const needle = q.toLowerCase();
  const text = parseSearch(q);
  return (found) => `${found.kind}#${found.seq}`.toLowerCase().includes(needle) || text(found);
}

/** The `@` word at `caret`, when its `@` opens the text or follows whitespace. */
function recordAt(value: string, caret: number): Target | null {
  const start = value.slice(0, caret).search(/(?<=^|\s)@\S*$/);
  if (start < 0) return null;
  const space = value.slice(start).search(/\s/);
  return { start, end: space < 0 ? value.length : start + space, query: value.slice(start + 1, caret) };
}

function optionOf(found: Found): Completion {
  const ref = `${found.kind}#${found.seq}`;
  return {
    key: found.id,
    title: ref,
    insert: ref,
    label: (
      <>
        <KindIcon kind={found.kind} size={15} />
        <span className="ref-t">{ref}</span>
        <span className="lbl">{found.title}</span>
      </>
    ),
  };
}
