import { type Draft as CreateDraft, keptDraft, type Related } from "../create/draft";
import type { Draft } from "../editors/drafts";
import { choosePreferences, type Preferences, preferences, receivePreferences } from "../preferences";
import { type BrowserState, EMPTY_STATE, mergeImport, parseState, sameJson, stateOf, storageKey } from "./value";

/**
 * What this browser holds for the user until the server can take it.
 *
 * The user's own data lives in their preferences on the server. Two things leave
 * a copy here: a browser from before that move, which kept all of it under
 * these keys, and a session that ended with a draft open, whose sign-in page
 * leaves no time to send it, and which the server would refuse anyway. The next
 * boot sends what is here (`adoptLocalUserData`) and forgets it.
 */

/** Keep `draft`, an open editor's, here for `adoptLocalUserData`; the session is ending. */
export function keepFieldDraftHere(email: string, key: string, draft: Draft): void {
  try {
    localStorage.setItem(draftsKey(email), JSON.stringify({ ...heldDrafts(email), [key]: draft }));
  } catch {
    // Storage off or full: the draft was written to the server as the user typed, up to the last pause.
  }
}

/** Keep `draft`, the create form's, here for `adoptLocalUserData`. */
export function keepCreateDraftHere(email: string, draft: CreateDraft): void {
  try {
    localStorage.setItem(createKey(email), JSON.stringify(draft));
  } catch {
    // Storage off or full: as for an editor's draft.
  }
}

/**
 * Send what this browser holds for `email` to the server, once, then forget it.
 *
 * - stars, names, people, saved views and read state merge into the user's
 *   (`mergeImport`: nothing the server holds is dropped);
 * - folded sections and the lens come over unless the server holds some;
 * - drafts come over and replace a draft of the same field, and the create
 *   form's replaces the server's: a copy here was kept when its session ended,
 *   or by an older build, and which of two is newer cannot be told.
 *
 * A state this build cannot read is left alone, `ui.tiles` stays in its place,
 * and a send that fails keeps everything for the next boot.
 */
export async function adoptLocalUserData(email: string): Promise<void> {
  let held: { state: string | null; drafts: string | null; create: string | null };
  try {
    held = { state: localStorage.getItem(storageKey(email)), drafts: localStorage.getItem(draftsKey(email)), create: localStorage.getItem(createKey(email)) };
  } catch {
    return;
  }
  const state = held.state === null ? null : readableState(held.state);
  const change: Partial<Preferences> = {
    ...(state ? stateChange(state) : {}),
    ...(held.drafts === null ? {} : draftsChange(held.drafts)),
    ...(held.create === null ? {} : createChange(held.create)),
  };
  const shown = preferences();
  if (Object.keys(change).length > 0 && !(await choosePreferences(change))) {
    // The page shows the merge, but the server has none of it: show what it holds, so the next
    // boot finds the difference again instead of taking the copy here for sent.
    receivePreferences(shown);
    return;
  }
  try {
    localStorage.removeItem(draftsKey(email));
    localStorage.removeItem(createKey(email));
    if (state && movedOut(state)) {
      const tiles = state.ui.tiles;
      if (Object.keys(tiles).length === 0) localStorage.removeItem(storageKey(email));
      else localStorage.setItem(storageKey(email), JSON.stringify({ ...EMPTY_STATE, ui: { ...EMPTY_STATE.ui, tiles } }));
    }
  } catch {
    /* Storage can be unavailable; then there is nothing to forget either. */
  }
}

/** The state held here, or null when this build cannot read it. */
function readableState(text: string): BrowserState | null {
  try {
    return parseState(JSON.parse(text));
  } catch {
    return null;
  }
}

/** Whether `state` holds anything the preferences now keep: all of it but `ui.tiles`. */
function movedOut(state: BrowserState): boolean {
  return !sameJson({ ...state, ui: { ...state.ui, tiles: {} } }, EMPTY_STATE);
}

/** The keys `state` changes in the preferences the server holds, merged. */
function stateChange(state: BrowserState): Partial<Preferences> {
  const now = preferences();
  const merged = mergeImport(stateOf(now, {}), state);
  const wanted: Partial<Preferences> = {
    stars: merged.stars,
    views: merged.views,
    aliases: merged.aliases,
    people: merged.people,
    read: merged.read,
    collapsed: now.collapsed.length > 0 ? now.collapsed : state.ui.collapsed,
    lens: now.lens ?? state.ui.lens,
  };
  return Object.fromEntries(Object.entries(wanted).filter(([key, value]) => !sameJson(value, now[key as keyof Preferences])));
}

function draftsChange(text: string): Partial<Preferences> {
  const drafts = { ...preferences().drafts, ...heldDraftsIn(text) };
  return sameJson(drafts, preferences().drafts) ? {} : { drafts };
}

function createChange(text: string): Partial<Preferences> {
  let held: unknown;
  try {
    held = JSON.parse(text);
  } catch {
    return {};
  }
  // One in another shape was kept by another build, before a deploy: it does not reopen.
  if (!isCreateDraft(held)) return {};
  const kept = keptDraft(held);
  return sameJson(kept, preferences().create_draft) ? {} : { create_draft: kept };
}

/** One key per user, so a draft never opens for someone else signing in on this browser. */
function draftsKey(email: string): string {
  return `trackinizer.v2.drafts.${email}`;
}

function createKey(email: string): string {
  return `trackinizer.v2.create.${email}`;
}

function heldDrafts(email: string): { readonly [key: string]: Draft } {
  try {
    return heldDraftsIn(localStorage.getItem(draftsKey(email)) ?? "{}");
  } catch {
    // Storage off: there is nothing held.
    return {};
  }
}

/** The editor drafts in `text`: those another build may have saved in another shape are left out, any with its text reopen. */
function heldDraftsIn(text: string): { readonly [key: string]: Draft } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return {};
  }
  if (typeof parsed !== "object" || parsed === null) return {};
  return Object.fromEntries(
    Object.entries(parsed as { [key: string]: unknown })
      .filter(([, draft]) => typeof draft === "object" && draft !== null && "text" in draft && typeof draft.text === "string")
      .map(([key, draft]) => [key, { text: (draft as Draft).text, base: (draft as Draft).base ?? null }]),
  );
}

/** Whether `value` has the shape `keepCreateDraftHere` writes, down to what the form reads of each relation. */
function isCreateDraft(value: unknown): value is CreateDraft {
  const draft = (value ?? {}) as { readonly [part in keyof CreateDraft]?: unknown };
  const relations = Array.isArray(draft.relations) ? (draft.relations as readonly Partial<Related>[]) : null;
  return (
    typeof draft.kind === "string" &&
    typeof draft.title === "string" &&
    typeof draft.description === "string" &&
    isRecord(draft.fields) &&
    Object.values(draft.fields).every((text) => typeof text === "string") &&
    isRecord(draft.chips) &&
    relations !== null &&
    relations.every(
      ({ option, target }) =>
        typeof option?.key === "string" &&
        typeof option.label === "string" &&
        Array.isArray(option.targetKinds) &&
        typeof target?.id === "string" &&
        typeof target.kind === "string" &&
        typeof target.seq === "number",
    )
  );
}

function isRecord(value: unknown): value is { readonly [key: string]: unknown } {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
