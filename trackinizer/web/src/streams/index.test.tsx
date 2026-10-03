import { cleanup, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { serveViews, showList, up, viewButton } from "../lists/viewTesting";
import { issue } from "../live/testing";

/** Each stream as `<root title>: #seq …`, a row's mark in brackets after it. */
async function streamed(): Promise<string[]> {
  await waitFor(() => expect(document.querySelector(".s-stream")).not.toBeNull());
  return [...document.querySelectorAll<HTMLElement>(".s-stream")].map((stream) => {
    const rows = [...stream.querySelectorAll("a.row")].map((row) => {
      const marks = [...row.querySelectorAll(".row-ctx, .s-also")].map((mark) => mark.textContent!.trim());
      return [row.querySelector(".row-ref")!.textContent, ...marks.map((mark) => `[${mark}]`)].join(" ");
    });
    return `${stream.querySelector(".s-title")!.textContent}: ${rows.join(" ")}`;
  });
}

/** Open the Issue list and switch it to Streams. */
async function showStreams() {
  showList();
  fireEvent.click(await viewButton("Streams"));
}

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("the switch reads List · Streams · Outline · Columns, and Streams groups the page's rows by root goal, newest group first", async () => {
  serveViews([issue(25), issue(24), issue(23), issue(22), issue(21)], {
    25: [up(2, 25)],
    24: [],
    23: [up(11, 23), up(1, 11)],
    22: [up(1, 22)],
    21: [up(2, 21)],
  });
  showList();
  await viewButton("Streams");
  expect([...document.querySelectorAll(".view-seg button")].map((button) => button.textContent)).toEqual([
    "List",
    "Streams",
    "Outline",
    "Columns",
  ]);
  fireEvent.click(await viewButton("Streams"));
  // A row names its parent when that is not the root.
  expect(await streamed()).toEqual([
    "Issue 2: #25 #21",
    "Issue 1: #23 [· in Issue 11] #22",
    "No parent: #24",
  ]);
  expect(document.querySelector(".s-stream .s-head a")!.getAttribute("href")).toBe("#/ref/Issue/2");
});

test("a stream's header counts its rows, active and done, with a status bar and the newest row's age", async () => {
  const threeHoursAgo = new Date(Date.now() - 3 * 3_600_000).toISOString();
  serveViews([issue(32, { created: threeHoursAgo }), issue(31, { status: "complete" }), issue(30, { status: "abandoned" })], {
    32: [up(1, 32)],
    31: [up(1, 31)],
    30: [up(1, 30)],
  });
  await showStreams();
  await streamed();
  const head = document.querySelector(".s-head")!;
  expect(head.querySelector(".s-counts")!.textContent).toBe("3 issues · 1 active · 1 done · newest 3h");
  const bar = [...head.querySelectorAll<HTMLElement>(".s-bar i")].map((part) => part.style.width);
  expect(bar).toEqual([`${100 / 3}%`, `${100 / 3}%`]);
});

test("a stream shows its newest three rows, then how many more, which shows them all", async () => {
  serveViews(
    [issue(45), issue(44), issue(43), issue(42), issue(41)],
    Object.fromEntries([41, 42, 43, 44, 45].map((seq) => [seq, [up(1, seq)]])),
  );
  await showStreams();
  expect(await streamed()).toEqual(["Issue 1: #45 #44 #43"]);
  const more = document.querySelector<HTMLButtonElement>(".s-more")!;
  expect(more.textContent).toBe("2 more in this stream");
  fireEvent.click(more);
  expect(await streamed()).toEqual(["Issue 1: #45 #44 #43 #42 #41"]);
  expect(document.querySelector(".s-more")).toBeNull();
});

test("a row under two roots is listed under each, marked, and j moves through it once; the list counts it once", async () => {
  serveViews([issue(52), issue(51)], { 52: [up(1, 52), up(2, 52)], 51: [up(2, 51)] });
  await showStreams();
  expect(await streamed()).toEqual(["Issue 1: #52 [also under #2]", "Issue 2: #52 [also under #1] #51"]);
  expect(document.querySelector(".list-foot")!.textContent).toContain("2 loaded");
  const focused = () => [...new Set([...document.querySelectorAll('a.row[aria-current="true"]')].map((row) => row.querySelector(".row-ref")!.textContent))];
  expect(focused()).toEqual(["#52"]);
  await userEvent.setup().keyboard("j");
  expect(focused()).toEqual(["#51"]);
});

test("rows with no parent go in a No parent group at the end; the view is kept with the list's state", async () => {
  serveViews([issue(62), issue(61), issue(60)], { 62: [], 61: [up(5, 61)], 60: [] });
  await showStreams();
  expect(await streamed()).toEqual(["Issue 5: #61", "No parent: #62 #60"]);
  expect(document.querySelectorAll(".s-stream")[1]!.querySelector(".s-head a")).toBeNull();
  cleanup();
  showList();
  expect(await streamed()).toEqual(["Issue 5: #61", "No parent: #62 #60"]);
});
