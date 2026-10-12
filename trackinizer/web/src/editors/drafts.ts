import { useCallback, useEffect, useState } from "react";
import { useProfile } from "../app/boot";
import { useDraftSaver } from "../app/session";
import { choosePreferencesSoon, type Preferences, preferences } from "../preferences";
import { keepFieldDraftHere } from "../state/local";
import { sameJson } from "../state/value";

/**
 * An open editor's text, and the stored value editing began from (`undefined`
 * when unset), which a save checks the stored value against.
 */
export type Draft = { readonly text: string; readonly base: unknown };

/**
 * The draft of field `field` of inquiry `id`: null while its editor is closed.
 *
 * It lives in the editor's own state, never in the cached row, so a refetch
 * or a re-render leaves it alone. A draft kept in the user's preferences (see
 * `useDraftKeeper`) comes back open on the next load of this field, in any
 * browser; setting it to null, as a send or a discard does, forgets it there.
 */
export function useDraft(id: string, field: string): [Draft | null, (draft: Draft | null) => void] {
  const key = draftKey(id, field);
  const [draft, setDraft] = useState<Draft | null>(() => preferences().drafts[key] ?? null);
  const set = useCallback(
    (next: Draft | null) => {
      setDraft(next);
      if (next === null) choosePreferencesSoon(keepDraft(preferences(), key, null));
    },
    [key],
  );
  return [draft, set];
}

/**
 * Keep the open `draft` in the user's preferences once typing pauses, for
 * `useDraft` to reopen. An editor opened and not typed in keeps nothing. If the
 * session ends while it is open, the server can no longer take it: it waits in this
 * browser and the next boot sends it (`state/local.ts`).
 */
export function useDraftKeeper(id: string, field: string, draft: Draft): void {
  const { email } = useProfile();
  const key = draftKey(id, field);
  useDraftSaver(() => keepFieldDraftHere(email, key, draft));
  const [opened] = useState(draft.text);
  useEffect(() => {
    if (draft.text === opened && !(key in preferences().drafts)) return;
    choosePreferencesSoon(keepDraft(preferences(), key, draft));
  }, [key, opened, draft]);
}

function draftKey(id: string, field: string): string {
  return `${id}:${field}`;
}

/** The change that puts `draft` at `key` of the kept drafts, or forgets `key` for null; none when they already are so. */
function keepDraft(now: Preferences, key: string, draft: Draft | null): Partial<Preferences> {
  const kept = now.drafts[key];
  if (draft === null) {
    return kept === undefined ? {} : { drafts: Object.fromEntries(Object.entries(now.drafts).filter(([other]) => other !== key)) };
  }
  // JSON has no undefined: an unset base is kept as null, which `sameValue` reads as unset too.
  const wire = { text: draft.text, base: draft.base ?? null };
  return kept !== undefined && sameJson(kept, wire) ? {} : { drafts: { ...now.drafts, [key]: wire } };
}
