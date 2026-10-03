import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { serveViews, showList, up, viewButton } from "../lists/viewTesting";
import { issue } from "../live/testing";

/** The outline's lines as `<indent>#ref title`, `older` after an ancestor outside the page; then the No parent group. */
async function outlined(): Promise<string[]> {
  await waitFor(() => expect(document.querySelector(".o-line")).not.toBeNull());
  return [...document.querySelectorAll<HTMLElement>(".o-line, .o-group")].map((line) => {
    if (line.classList.contains("o-group")) return line.textContent!;
    const ref = line.querySelector(".row-ref")!.textContent;
    const older = line.querySelector(".o-older") ? " older" : "";
    return `${"  ".repeat(Number(line.dataset.depth))}${ref}${older}`;
  });
}

/** Open the Issue list and switch it to Outline. */
async function showOutline() {
  showList();
  fireEvent.click(await viewButton("Outline"));
}

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("an Issue list switches between List and Outline, and Outline reads the page's ancestry", async () => {
  const sent = serveViews([issue(10), issue(11)], { 11: [up(10, 11)] });
  showList();
  const outline = await viewButton("Outline");
  expect((await viewButton("List")).getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(outline);
  expect(await outlined()).toEqual(["#10", "  #11"]);
  expect(outline.getAttribute("aria-pressed")).toBe("true");
  const read = new URLSearchParams(sent.at(-1)!.query);
  expect([read.get("ancestors"), read.getAll("fields")]).toEqual(["narrows", ["id"]]);
  // The outline is its own grouping.
  expect(screen.queryByTitle("Group loaded rows")).toBeNull();
  fireEvent.click(await viewButton("List"));
  await waitFor(() => expect(document.querySelector(".o-line")).toBeNull());
  expect(document.querySelectorAll("a.row")).toHaveLength(2);
});

test("other kinds keep the flat list, with no switch", async () => {
  serveViews([], {});
  showList("Paper");
  await screen.findByText("Nothing here");
  expect(document.querySelector(".view-seg")).toBeNull();
});

test("rows nest under their parents; parents outside the page show older, and a single-child chain of them is one line", async () => {
  // #1 › #2 are outside the page, #2 the only child of #1; #20 and #21 narrow #2, #22 narrows #21.
  serveViews([issue(20), issue(21), issue(22)], {
    20: [up(2, 20), up(1, 2)],
    21: [up(2, 21), up(1, 2)],
    22: [up(21, 22), up(2, 21), up(1, 2)],
  });
  await showOutline();
  expect(await outlined()).toEqual(["#1 › #2 older", "  #21", "    #22", "  #20"]);
  const older = document.querySelector(".o-older")!;
  expect(older.querySelector(".row-title")!.textContent).toBe("Issue 1 › Issue 2");
  expect(older.getAttribute("href")).toBe("#/ref/Issue/2");
});

test("rows with no parent go in a No parent group at the end, which collapses", async () => {
  serveViews([issue(30), issue(31), issue(32)], { 30: [], 31: [], 32: [up(5, 32)] });
  await showOutline();
  expect(await outlined()).toEqual(["#5 older", "  #32", "No parent2", "#31", "#30"]);
  fireEvent.click(document.querySelector(".o-group")!);
  expect(await outlined()).toEqual(["#5 older", "  #32", "No parent2"]);
});

test("← and → collapse and expand the focused line, j and k move over older lines too, and Enter opens one", async () => {
  serveViews([issue(40), issue(41)], { 40: [up(4, 40)], 41: [up(4, 41)] });
  await showOutline();
  expect(await outlined()).toEqual(["#4 older", "  #41", "  #40"]);
  const focused = () => document.querySelector('[aria-current="true"]')!.closest<HTMLElement>(".o-line")!.querySelector(".row-ref")!.textContent;
  expect(focused()).toBe("#4");
  const user = userEvent.setup();
  await user.keyboard("{ArrowLeft}");
  expect(await outlined()).toEqual(["#4 older"]);
  expect(document.querySelector(".o-line")!.textContent).toContain("2 issues");
  await user.keyboard("{ArrowRight}");
  expect(await outlined()).toEqual(["#4 older", "  #41", "  #40"]);
  await user.keyboard("j");
  expect(focused()).toBe("#41");
  await user.keyboard("k");
  await user.keyboard("{Enter}");
  expect(location.hash).toBe("#/ref/Issue/4");
});

test("the view is kept with the list's state, so the list opens again as an outline", async () => {
  serveViews([issue(50), issue(51)], { 51: [up(50, 51)] });
  await showOutline();
  await outlined();
  cleanup();
  showList();
  expect(await outlined()).toEqual(["#50", "  #51"]);
});

test("a list state stored before the switch existed opens as the List, its filters kept", async () => {
  const saved = { tab: "all", choices: [], grouping: "none", ordering: "created", pages: {}, collapsed: [], focus: null };
  sessionStorage.setItem("trackinizer.v2.list.Issue", JSON.stringify(saved));
  const sent = serveViews([issue(60)], {});
  showList();
  await waitFor(() => expect(document.querySelectorAll("a.row")).toHaveLength(1));
  expect(new URLSearchParams(sent[0]!.query).getAll("filter")).toEqual([]);
  expect((await viewButton("List")).getAttribute("aria-pressed")).toBe("true");
});
