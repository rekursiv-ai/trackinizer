
// Which account this page was read as, and which the browser is signed in as now. One browser shares one session
// cookie across its tabs: a tab left open while another tab signs in as someone else would show the first account and
// act as the second. So every request names the page's account (`X-Page-Account`), and each stream its user
// (`page_user`), and Trackinizer refuses one whose session is another's (409 `other_account`); and each tab says whom
// it read in `localStorage`, where every other tab hears it and stops showing an account the browser is no longer
// signed in as (`AccountChanged`). What this app keeps in the browser is kept by account already (`state/local.ts`).

/** The header naming the account a page was read as (Trackinizer's `PAGE_ACCOUNT_HEADER`). */
export const PAGE_ACCOUNT_HEADER = "X-Page-Account";

/**
 * The query parameter naming, by id, the user a page was read as (Trackinizer's `PAGE_USER_PARAM`), for its streams:
 * an `EventSource` sends no header, and an id keeps the email out of URLs and logs.
 */
export const PAGE_USER_PARAM = "page_user";

/**
 * The key, in `localStorage`, of the account the browser is signed in as, as the tab that read it last found. Empty
 * once the browser signed out; absent where no tab has said yet, which tells nothing. This app's own: another app this
 * origin serves forgets what it kept for an account as its own key changes, which a write here would skip.
 */
export const SIGNED_IN_KEY = "trackinizer.signed-in";

/** What a switch of accounts leaves this page: whom it was read as, and whom the browser is signed in as now, if any. */
export type Switched = { readonly page: string; readonly now: string | null };

/**
 * Bind this page to `email`, the account its profile was read as, whose user is `userId`: each request names it from
 * now on, and each stream its user (`pageUrl`), and every other tab hears the browser is signed in as it. The profile
 * is read again only with the header, so a page is never bound to a second account: a refusal comes first.
 */
export function readAs(email: string, userId: string): void {
  page = email;
  pageUser = userId;
  left = null;
  if (stored() !== email) write(email);
  // Without storage no tab hears another, and this one would read an absent key as a sign-out.
  watching = stored() === email;
  if (!hearing) addEventListener("storage", heard);
  hearing = true;
  for (const listener of [...listeners]) listener();
}

/** The account this page was read as, which its requests name; null before its profile was read. */
export function pageAccount(): string | null {
  return page;
}

/**
 * `url` naming the user this page was read as, for a request that cannot carry the header (a stream): Trackinizer
 * refuses it, 409, once the browser is signed in as another. Unchanged before the profile was read.
 */
export function pageUrl(url: string): string {
  if (pageUser === null) return url;
  const named = new URL(url);
  named.searchParams.set(PAGE_USER_PARAM, pageUser);
  return named.href;
}

/** Say every other tab's page is not the browser's now: it signed out here. */
export function signedOut(): void {
  write("");
}

/**
 * Whom this page was read as and whom the browser is signed in as now, once the two differ: by another tab's word, or a
 * request's refusal (`refused`). It holds until a reload, even if the browser signs back in as the page's account: the
 * page may have read the other's meanwhile. Null while the page is the browser's account's, or before it is read.
 */
export function switched(): Switched | null {
  // Read from the browser's storage, not only from the other tab's event, which reaches this tab later.
  if (left === null && page !== null && watching) {
    const now = stored();
    if (typeof now === "string" && now !== page) left = { page, now: now || null };
  }
  return left;
}

/** Hear that a request was refused because the browser is signed in as another account than this page's. */
export function refused(): void {
  if (page === null || left !== null) return;
  const now = watching ? stored() : undefined;
  // The browser's word may lag the refusal, still naming this page's account; then who is signed in is not known.
  left = { page, now: typeof now === "string" && now !== page ? now || null : null };
  for (const listener of [...listeners]) listener();
}

/** Call `listener` whenever `switched` may answer anew: as a request is refused, or another tab says whom it read. */
export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => void listeners.delete(listener);
}

let page: string | null = null;
let pageUser: string | null = null;
let left: Switched | null = null;
let watching = false;
let hearing = false;
const listeners = new Set<() => void>();

/** Another tab said whom the browser is signed in as, or emptied its storage. */
function heard(event: StorageEvent): void {
  if (event.key !== SIGNED_IN_KEY && event.key !== null) return;
  for (const listener of [...listeners]) listener();
}

/** The account the browser names, null when it names none, undefined when it cannot be read. */
function stored(): string | null | undefined {
  try {
    return localStorage.getItem(SIGNED_IN_KEY);
  } catch {
    return undefined;
  }
}

function write(account: string): void {
  try {
    localStorage.setItem(SIGNED_IN_KEY, account);
  } catch { /* Storage is unavailable: the requests' header still keeps the page to its account. */ }
}
