import { useEffect, useState } from "react";
import { useProfile } from "../app/boot";
import { useDraftSaver } from "../app/session";

/**
 * An open editor's text, and the stored value editing began from (`undefined`
 * when unset), which a save checks the stored value against.
 */
export type Draft = { readonly text: string; readonly base: unknown };

/**
 * The draft of field `field` of inquiry `id`: null while its editor is closed.
 *
 * It lives in the editor's own state, never in the cached row, so a refetch
 * or a re-render leaves it alone. A draft saved when the session ended comes
 * back open once, on the next load of this field.
 */
export function useDraft(id: string, field: string): [Draft | null, (draft: Draft | null) => void] {
  const { email } = useProfile();
  const [draft, setDraft] = useState<Draft | null>(() => savedDrafts(email)[draftKey(id, field)] ?? null);
  useEffect(() => {
    // It is open now, and saved again if the session ends while it is.
    forgetDraft(email, draftKey(id, field));
  }, [email, id, field]);
  return [draft, setDraft];
}

/** Save `draft` to this browser if the session ends while it is open, for `useDraft` to reopen. */
export function useDraftKeeper(id: string, field: string, draft: Draft): void {
  const { email } = useProfile();
  useDraftSaver(() => {
    const drafts = { ...savedDrafts(email), [draftKey(id, field)]: draft };
    localStorage.setItem(storageKey(email), JSON.stringify(drafts));
  });
}

function draftKey(id: string, field: string): string {
  return `${id}:${field}`;
}

/** One key per user, so a draft never opens for someone else signing in on this browser. */
function storageKey(email: string): string {
  return `trackinizer.v2.drafts.${email}`;
}

function savedDrafts(email: string): { readonly [key: string]: Draft } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(localStorage.getItem(storageKey(email)) ?? "{}");
  } catch {
    // Storage off, or text that is not JSON: there is nothing to reopen.
    return {};
  }
  // A draft another build saved, before a deploy, may have another shape: only
  // one with its text reopens.
  if (typeof parsed !== "object" || parsed === null) return {};
  return Object.fromEntries(
    Object.entries(parsed as { [key: string]: unknown }).filter(
      ([, draft]) => typeof draft === "object" && draft !== null && "text" in draft && typeof draft.text === "string",
    ),
  ) as { [key: string]: Draft };
}

function forgetDraft(email: string, key: string): void {
  const drafts = savedDrafts(email);
  if (!(key in drafts)) return;
  const rest = Object.fromEntries(Object.entries(drafts).filter(([other]) => other !== key));
  try {
    if (Object.keys(rest).length) localStorage.setItem(storageKey(email), JSON.stringify(rest));
    else localStorage.removeItem(storageKey(email));
  } catch {
    // Storage off: the draft cannot come back twice, since it was never kept.
  }
}
