import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useContext, useEffect, useRef } from "react";
import { MetaContext } from "../app/boot";
import { HighlightContext } from "../app/highlights";
import { detailQueries } from "../detail/queries";
import { useHighlightMentions } from "../settings/highlightMentions";
import { type NamedRow, namedRows } from "./chatRefs";

/**
 * One answer of the open conversation: its record's key and its Markdown, and whether
 * the turn it ended already pointed at rows with the agent's Highlight tool.
 */
export type Answer = { readonly key: string; readonly text: string; readonly pointed: boolean };

/**
 * Light up on the canvas the rows an answer cites, the moment it arrives, through
 * the store the agent's Highlight tool feeds (`HighlightStore`): the newest answer
 * that cites any replaces the marks, one that cites none leaves them. An answer whose
 * turn called Highlight leaves the marks alone: the agent chose its rows, and the
 * tool is for those the answer does not name. Refs are resolved here, together, and
 * each row lights as its own lookup returns, so a slow or failing one holds up no
 * other; a row already seen costs no request (`detailQueries.ref` never goes stale),
 * and a row named by its id none.
 *
 * `answers` is null until the conversation's records are read. The answers there
 * when `scope` (the open session) first loads are history and light nothing, and
 * so are those that arrive while the user has the switch off in Settings; turning
 * it on later lights only answers that come after. A lookup under way when the
 * switch goes off, or a newer answer arrives, is dropped.
 */
export function useAnswerHighlights(answers: readonly Answer[] | null, scope: string | null): void {
  const store = useContext(HighlightContext);
  const queryClient = useQueryClient();
  const kinds = useContext(MetaContext)?.kinds;
  const [mentions] = useHighlightMentions();
  const seen = useRef<{ readonly scope: string | null; readonly keys: Set<string> } | null>(null);
  const turn = useRef(0);
  const switchOn = useRef(mentions);
  switchOn.current = mentions;
  const kindsRef = useRef(kinds ?? []);
  kindsRef.current = kinds ?? [];
  useEffect(() => {
    if (answers === null) return;
    if (seen.current?.scope !== scope) {
      seen.current = { scope, keys: new Set(answers.map((answer) => answer.key)) };
      turn.current += 1;
      return;
    }
    const held = seen.current.keys;
    const fresh = answers.filter((answer) => !held.has(answer.key));
    for (const answer of fresh) held.add(answer.key);
    if (!store || !switchOn.current) return;
    const rows = fresh.filter((answer) => !answer.pointed)
      .map((answer) => namedRows(answer.text, kindsRef.current)).findLast((named) => named.length > 0);
    if (!rows) return;
    const mine = ++turn.current;
    const found: (string | null)[] = rows.map(() => null);
    rows.forEach((row, at) => void idOf(queryClient, row).then((id) => {
      if (id === null || mine !== turn.current || !switchOn.current) return;
      found[at] = id;
      store.set(found.filter((each) => each !== null));
    }));
  }, [answers, scope, store, queryClient]);
}

/** The id of `row`, or null when it does not resolve. A failure is not retried: the rows glow now or not at all. */
async function idOf(queryClient: QueryClient, row: NamedRow): Promise<string | null> {
  if ("id" in row) return row.id;
  try {
    return await queryClient.fetchQuery({ ...detailQueries.ref(row.kind, row.seq), retry: false });
  } catch {
    return null;
  }
}
