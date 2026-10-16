import { afterEach, expect, test } from "vitest";
import { ACCOUNTS_KEY, accountKind, forgetAccount, MAX_ACCOUNTS, readAccounts, rememberAccount, signInUrl } from "./accounts";

afterEach(() => localStorage.clear());

const at = (minute: number) => new Date(Date.UTC(2026, 9, 15, 12, minute));

test("the account signed in last stands first, its name and role as they are now, each once", () => {
  rememberAccount({ email: "ada@example.com", name: "Ada", role: "writer" }, at(0));
  rememberAccount({ email: "lead@acme.co", name: "Lead", role: "admin" }, at(1));
  rememberAccount({ email: "ada@example.com", name: "Ada L", role: "admin" }, at(2));
  expect(readAccounts()).toEqual([
    { email: "ada@example.com", name: "Ada L", role: "admin", seen: at(2).toISOString() },
    { email: "lead@acme.co", name: "Lead", role: "admin", seen: at(1).toISOString() },
  ]);
});

test("past the most it keeps, the account seen longest ago is dropped", () => {
  for (let n = 0; n <= MAX_ACCOUNTS; n += 1) rememberAccount({ email: `a${n}@acme.co`, name: "", role: "viewer" }, at(n));
  const kept = readAccounts().map((each) => each.email);
  expect(kept).toHaveLength(MAX_ACCOUNTS);
  expect(kept).not.toContain("a0@acme.co");
  expect(kept[0]).toBe(`a${MAX_ACCOUNTS}@acme.co`);
});

test("forgetting an account removes it from this browser's list alone", () => {
  rememberAccount({ email: "a@acme.co", name: "", role: "viewer" }, at(0));
  rememberAccount({ email: "b@acme.co", name: "", role: "viewer" }, at(1));
  forgetAccount("a@acme.co");
  expect(readAccounts().map((each) => each.email)).toEqual(["b@acme.co"]);
});

test("a stored list that is not one, or holds a malformed entry, reads as what is well formed", () => {
  localStorage.setItem(ACCOUNTS_KEY, "not json");
  expect(readAccounts()).toEqual([]);
  localStorage.setItem(ACCOUNTS_KEY, JSON.stringify([{ email: "a@acme.co", name: "A", role: "admin", seen: "x" }, { email: 4 }, null]));
  expect(readAccounts().map((each) => each.email)).toEqual(["a@acme.co"]);
});

test("an address at a mail host anyone joins is personal; any other is its organisation's", () => {
  expect(accountKind("ming.chiu@Gmail.com")).toEqual({ kind: "personal", org: "gmail.com" });
  expect(accountKind("lead@acme-robotics.com")).toEqual({ kind: "work", org: "acme-robotics.com" });
});

test("signing in as a remembered account asks Google to pick it; adding one asks for none", () => {
  expect(signInUrl("lead@acme.co")).toBe("/auth/login?next=%2Fapp%2F&login_hint=lead%40acme.co");
  expect(signInUrl()).toBe("/auth/login?next=%2Fapp%2F");
});
