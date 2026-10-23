import { useSyncExternalStore } from "react";
import { APP_BASE } from "../base";

/**
 * An account this browser has been signed in as: what the account menu shows of
 * it once another is signed in. Kept in this browser only, since it spans the
 * accounts and the server knows each alone.
 */
export type RememberedAccount = {
  readonly email: string;
  readonly name: string;
  readonly role: string;
  /** When this browser last saw it signed in, as an ISO time. */
  readonly seen: string;
};

/** Whether an account is a person's own or an organisation's, and the organisation's domain. */
export type AccountKind = { readonly kind: "personal" | "work"; readonly org: string };

/** The `localStorage` key of the accounts this browser remembers. */
export const ACCOUNTS_KEY = "trackinizer.v2.accounts";

/** The most accounts the menu remembers; the one seen longest ago goes first. */
export const MAX_ACCOUNTS = 8;

/** Mail hosts anyone may sign up at: an address there is a person's own, not an organisation's. */
const PERSONAL_HOSTS = new Set([
  "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "icloud.com", "me.com",
  "yahoo.com", "proton.me", "protonmail.com", "fastmail.com", "aol.com",
]);

const listeners = new Set<() => void>();

/** What `readAccounts` last returned, so a hook reading it renders only on a change. */
let cached: { readonly raw: string | null; readonly accounts: readonly RememberedAccount[] } = { raw: null, accounts: [] };

/** The accounts this browser remembers, the one seen last first; none when storage is unavailable. */
export function readAccounts(): readonly RememberedAccount[] {
  let raw: string | null;
  try {
    raw = localStorage.getItem(ACCOUNTS_KEY);
  } catch {
    return cached.accounts;
  }
  if (raw === cached.raw) return cached.accounts;
  cached = { raw, accounts: parse(raw) };
  return cached.accounts;
}

/**
 * Remember `account` as signed in now: first in the list, its name and role as
 * they are now, the oldest dropped past `MAX_ACCOUNTS`.
 */
export function rememberAccount(account: Omit<RememberedAccount, "seen">, now: Date = new Date()): void {
  const kept = readAccounts().filter((each) => each.email !== account.email);
  write([{ email: account.email, name: account.name, role: account.role, seen: now.toISOString() }, ...kept].slice(0, MAX_ACCOUNTS));
}

/** Forget `email` in this browser; its account itself is untouched. */
export function forgetAccount(email: string): void {
  write(readAccounts().filter((each) => each.email !== email));
}

/** The remembered accounts, kept current as they change here or in another tab. */
export function useAccounts(): readonly RememberedAccount[] {
  return useSyncExternalStore(subscribe, readAccounts);
}

/** Whether `email` is a person's own address or an organisation's, by its domain. */
export function accountKind(email: string): AccountKind {
  const org = email.slice(email.lastIndexOf("@") + 1).toLowerCase();
  return { kind: PERSONAL_HOSTS.has(org) ? "personal" : "work", org };
}

/** Where signing in as another account starts: Google's chooser, with `email` picked when given. */
export function signInUrl(email?: string): string {
  const next = `next=${encodeURIComponent(APP_BASE)}`;
  return email ? `/auth/login?${next}&login_hint=${encodeURIComponent(email)}` : `/auth/login?${next}`;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  const fromAnotherTab = (event: StorageEvent) => { if (event.key === ACCOUNTS_KEY) listener(); };
  addEventListener("storage", fromAnotherTab);
  return () => {
    listeners.delete(listener);
    removeEventListener("storage", fromAnotherTab);
  };
}

function write(accounts: readonly RememberedAccount[]): void {
  try {
    localStorage.setItem(ACCOUNTS_KEY, JSON.stringify(accounts));
  } catch { /* Storage can be unavailable; then the menu remembers no other account. */ }
  listeners.forEach((listener) => listener());
}

/** The stored list, keeping only well-formed entries: another build or a hand edit may have left anything. */
function parse(raw: string | null): readonly RememberedAccount[] {
  let value: unknown;
  try {
    value = raw === null ? [] : JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(value)) return [];
  return value.filter((each): each is RememberedAccount => typeof each === "object" && each !== null
    && typeof each.email === "string" && typeof each.name === "string"
    && typeof each.role === "string" && typeof each.seen === "string");
}
