import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  PAGE_USER_PARAM,
  pageAccount,
  pageUrl,
  readAs,
  refused,
  SIGNED_IN_KEY,
  signedOut,
  subscribe,
  switched,
} from "./signedIn";

const ADA = "ada@example.com";
const SAM = "sam@example.com";
const USER_ID = "00000000-0000-4000-8000-000000000001";

beforeEach(() => localStorage.clear());

afterEach(() => vi.restoreAllMocks());

/** Another tab of the browser says it is signed in as `email` now, or signed out with "". */
function anotherTabSays(email: string) {
  localStorage.setItem(SIGNED_IN_KEY, email);
  dispatchEvent(new StorageEvent("storage", { key: SIGNED_IN_KEY, newValue: email }));
}

// First: before a profile is read, the page names nobody and nothing has moved.
test("a page not yet read names no account and no user, and no word of another tab moves it", () => {
  anotherTabSays(SAM);
  expect(pageAccount()).toBeNull();
  expect(pageUrl("https://trax.test/api/web/subscribe")).toBe(
    "https://trax.test/api/web/subscribe",
  );
  expect(switched()).toBeNull();
  refused();
  expect(switched()).toBeNull();
});

test("a page read as an account says so to the browser, and names its user, not its email, in a stream's URL", () => {
  readAs(ADA, USER_ID);
  expect(pageAccount()).toBe(ADA);
  expect(localStorage.getItem(SIGNED_IN_KEY)).toBe(ADA);
  const url = new URL(pageUrl("https://trax.test/api/canvases/c/events?since=1"));
  expect([
    url.pathname,
    url.searchParams.get("since"),
    url.searchParams.get(PAGE_USER_PARAM),
  ]).toEqual(["/api/canvases/c/events", "1", USER_ID]);
  expect(url.href).not.toContain("ada");
  expect(switched()).toBeNull();
});

test("another tab signing in as someone else moves the page for good, even once the browser is the page's again", () => {
  readAs(ADA, USER_ID);
  const heard = vi.fn();
  const stop = subscribe(heard);
  anotherTabSays(SAM);
  expect(heard).toHaveBeenCalledOnce();
  expect(switched()).toEqual({ page: ADA, now: SAM });
  anotherTabSays(ADA);
  expect(switched()).toEqual({ page: ADA, now: SAM });
  stop();
  anotherTabSays(SAM);
  expect(heard).toHaveBeenCalledTimes(2);
});

test("a sign-out in another tab moves the page, and names nobody signed in", () => {
  readAs(ADA, USER_ID);
  signedOut();
  expect(localStorage.getItem(SIGNED_IN_KEY)).toBe("");
  expect(switched()).toEqual({ page: ADA, now: null });
});

test("storage emptied in another tab tells nothing: the page stays", () => {
  readAs(ADA, USER_ID);
  localStorage.clear();
  dispatchEvent(new StorageEvent("storage", { key: null }));
  expect(switched()).toBeNull();
});

test("a refusal moves the page, naming whom the browser says it is signed in as when that is another", () => {
  readAs(ADA, USER_ID);
  const heard = vi.fn();
  const stop = subscribe(heard);
  localStorage.setItem(SIGNED_IN_KEY, SAM);
  refused();
  stop();
  expect(heard).toHaveBeenCalledOnce();
  expect(switched()).toEqual({ page: ADA, now: SAM });
  readAs(ADA, USER_ID);
  refused();
  expect(switched()).toEqual({ page: ADA, now: null });
});

test("without storage no tab is heard, and the page moves only on a refusal", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new DOMException("denied", "SecurityError");
  });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new DOMException("denied", "SecurityError");
  });
  readAs(ADA, USER_ID);
  expect(pageAccount()).toBe(ADA);
  expect(switched()).toBeNull();
  signedOut();
  refused();
  expect(switched()).toEqual({ page: ADA, now: null });
});
