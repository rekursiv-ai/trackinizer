import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { detailOf, peer, serveViews, showList, viewButton } from "../lists/viewTesting";
import { issue } from "../live/testing";

/** Each column as `<heading>: #seq …`, the selected one starred. */
function columns(): string[] {
  return [...document.querySelectorAll(".c-column")].map((column) => {
    const items = [...column.querySelectorAll(".c-item")].map(
      (item) => `${item.querySelector(".c-ref")!.textContent}${item.getAttribute("aria-current") ? "*" : ""}`,
    );
    return `${column.querySelector(".c-heading")!.textContent}:${items.map((item) => ` ${item}`).join("")}`;
  });
}

/** Wait for the columns to read `expected`. */
async function expectColumns(expected: string[]) {
  await waitFor(() => expect(columns()).toEqual(expected));
}

/** The tree the columns drill: #1 ← #2 ← #3, #2 produced Experiment #9; #4 is another root. */
function serveTree() {
  return serveViews(
    [issue(1), issue(4)],
    {},
    {
      1: detailOf(1, { narrows: [peer(2)], produced: [peer(2)] }),
      2: detailOf(2, { narrows: [peer(3)], produced: [peer(3), peer(9, "Experiment")] }),
      3: detailOf(3, {}),
      4: detailOf(4, {}),
    },
  );
}

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("Columns opens #/list/Issue?view=columns and lists the root Issues the list's filters keep, newest first", async () => {
  const sent = serveTree();
  showList();
  fireEvent.click(await viewButton("Columns"));
  await expectColumns(["Root issues: #4 #1"]);
  expect(location.hash).toBe("#/list/Issue?view=columns");
  const filters = new URLSearchParams(sent.at(-1)!.query).getAll("filter").map((raw) => JSON.parse(raw));
  expect(filters).toContainEqual({ field: "narrows", op: "isnull", value: "" });
  expect(screen.getByTitle("The same query from the CLI").textContent).toBe("trax issue status is active narrows isnull");
  // Columns keep the server's order: newest first.
  expect(screen.queryByTitle("Sort loaded rows")).toBeNull();
});

test("another view leaves Columns for the list's plain hash, so Columns never opens from it", async () => {
  serveTree();
  showList("Issue", "#/list/Issue?view=columns");
  await expectColumns(["Root issues: #4 #1"]);
  fireEvent.click(await viewButton("List"));
  await waitFor(() => expect(location.hash).toBe("#/list/Issue"));
  await waitFor(() => expect(document.querySelectorAll("a.row")).toHaveLength(2));
});

test("a deep link opens each selection's children in the next column, and the last column its outputs", async () => {
  serveTree();
  showList("Issue", "#/list/Issue?view=columns&path=1,2");
  await expectColumns(["Root issues: #4 #1*", "Under Issue 1: #2*", "Under Issue 2: #3"]);
  const outputs = document.querySelectorAll(".c-column")[2]!.querySelectorAll(".c-output");
  expect([...outputs].map((output) => output.textContent)).toEqual(["Experiment 9"]);
  expect(outputs[0]!.getAttribute("href")).toBe("#/ref/Experiment/9");
});

test("a click drills in, and → selects the first child", async () => {
  serveTree();
  showList("Issue", "#/list/Issue?view=columns");
  await expectColumns(["Root issues: #4 #1"]);
  fireEvent.click([...document.querySelectorAll(".c-item")].find((item) => item.textContent!.includes("#1"))!);
  await expectColumns(["Root issues: #4 #1*", "Under Issue 1: #2"]);
  expect(location.hash).toBe("#/list/Issue?view=columns&path=1");
  await userEvent.setup().keyboard("{ArrowRight}");
  await expectColumns(["Root issues: #4 #1*", "Under Issue 1: #2*", "Under Issue 2: #3"]);
  expect(location.hash).toBe("#/list/Issue?view=columns&path=1,2");
});

test("← steps out, j and k move in the deepest column, and Enter opens its selection", async () => {
  serveTree();
  showList("Issue", "#/list/Issue?view=columns&path=1,2");
  await expectColumns(["Root issues: #4 #1*", "Under Issue 1: #2*", "Under Issue 2: #3"]);
  const user = userEvent.setup();
  await user.keyboard("{ArrowLeft}");
  await expectColumns(["Root issues: #4 #1*", "Under Issue 1: #2"]);
  await user.keyboard("k");
  await expectColumns(["Root issues: #4* #1", "Under Issue 4:"]);
  expect(location.hash).toBe("#/list/Issue?view=columns&path=4");
  await user.keyboard("{Enter}");
  expect(location.hash).toBe("#/ref/Issue/4");
});

test("after a click on an item and →, Enter opens the selection, not the clicked item again (LV-13)", async () => {
  serveTree();
  showList("Issue", "#/list/Issue?view=columns");
  await expectColumns(["Root issues: #4 #1"]);
  const user = userEvent.setup();
  await user.click([...document.querySelectorAll<HTMLElement>(".c-item")].find((item) => item.textContent!.includes("#1"))!);
  await expectColumns(["Root issues: #4 #1*", "Under Issue 1: #2"]);
  await user.keyboard("{ArrowRight}");
  await expectColumns(["Root issues: #4 #1*", "Under Issue 1: #2*", "Under Issue 2: #3"]);
  await user.keyboard("{Enter}");
  expect(location.hash).toBe("#/ref/Issue/2");
});
