import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { PROFILE, renderDetail, row } from "../detail/testing";
import { stubLayout } from "../editors/testing";
import { cacheWith, serveGraph, type StoredEdge } from "./testing";

const SELF = row("Issue", 1);
const CHILD = row("Issue", 2, { title: "The child" });
const ORIGIN = row("Issue", 3, { title: "The origin" });

beforeEach(() => {
  history.replaceState(null, "", "#/ref/Issue/1");
  stubLayout();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function open(rows: Parameters<typeof serveGraph>[0], edges: StoredEdge[], options?: Parameters<typeof renderDetail>[2]) {
  const server = serveGraph(rows, edges);
  renderDetail({ id: rows[0]!.id }, cacheWith(rows), options);
  await screen.findByRole("heading", { level: 1 });
  return server;
}

/**
 * Press `action` ("Annotate", "Remove") on the first row of `relation`, as its
 * button names it ("narrows Issue#3"), once focusing the row's link has mounted it.
 */
function rowAction(action: string, relation: string) {
  fireEvent.focus(relationLink(relation));
  fireEvent.click(screen.getByRole("button", { name: `${action} ${relation}` }));
}

/**
 * The link of the first row of `relation` ("narrowed by Issue#2"): in the rail,
 * the line that names the edge (`narrowed_by`) and then the ref; in other
 * relations, the row that starts with the ref.
 */
function relationLink(relation: string): HTMLElement {
  const words = relation.split(" ");
  const ref = words.pop()!;
  const rail = `${words.join("_")} ${ref}`;
  return [...document.querySelectorAll<HTMLElement>(".rel-link")].find(
    (link) => link.textContent!.startsWith(rail) || link.textContent!.startsWith(ref),
  )!;
}

/** Press a toast's action from the keyboard: jsdom has no pointer capture for Radix's swipe. */
async function pressToast(name: string) {
  screen.getByRole("button", { name }).focus();
  await userEvent.keyboard("{Enter}");
}

test("a row's actions mount when it is focused or hovered", async () => {
  await open([SELF, CHILD], [{ from: CHILD.id, kind: "narrows", to: SELF.id }]);
  expect(screen.queryByRole("button", { name: "Annotate narrowed by Issue#2" })).toBeNull();
  fireEvent.pointerOver(relationLink("narrowed by Issue#2"));
  expect(screen.getByRole("button", { name: "Annotate narrowed by Issue#2" })).toBeTruthy();
});

test("a viewer gets no row actions", async () => {
  await open([SELF, CHILD], [{ from: CHILD.id, kind: "narrows", to: SELF.id }], { profile: { ...PROFILE, role: "viewer" } });
  fireEvent.focus(relationLink("narrowed by Issue#2"));
  expect(screen.getByRole("region", { name: "Children" }).querySelectorAll("button")).toHaveLength(0);
  expect(screen.queryByRole("button", { name: "Issue#1 actions" })).toBeNull();
});

test("a touch mounts a row's actions by a tap on their place, and that tap presses none of them", async () => {
  await open([SELF, CHILD], [{ from: CHILD.id, kind: "narrows", to: SELF.id }]);
  const link = relationLink("narrowed by Issue#2");
  // A touch's pointerover comes with its tap, whose click would land on what mounts under it.
  fireEvent.pointerOver(link, { pointerType: "touch" });
  expect(screen.queryByRole("button", { name: "Remove narrowed by Issue#2" })).toBeNull();
  fireEvent.click(link.parentElement!.querySelector(".rel-actions")!);
  expect(screen.getByRole("button", { name: "Remove narrowed by Issue#2" })).toBeTruthy();
  expect(screen.queryByRole("form")).toBeNull();
});

test("offline, adding, annotating and removing are off", async () => {
  vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
  await open([SELF, CHILD], [{ from: CHILD.id, kind: "narrows", to: SELF.id }]);
  fireEvent.focus(relationLink("narrowed by Issue#2"));
  const buttons = within(screen.getByRole("region", { name: "Children" })).getAllByRole("button");
  expect(buttons.map((button) => [button.textContent || button.getAttribute("aria-label"), button.hasAttribute("disabled")])).toEqual([
    ["+ add", true],
    ["Annotate narrowed by Issue#2", true],
    ["Remove narrowed by Issue#2", true],
  ]);
  expect(screen.getByRole("button", { name: "Issue#1 actions" }).hasAttribute("disabled")).toBe(true);
});

test("a note is set with PUT, and Undo on its toast puts the old one back", { tags: ["manual"] }, async () => {
  const server = await open([SELF, CHILD], [{ from: CHILD.id, kind: "narrows", to: SELF.id, note: "old" }]);
  const note = openNote("narrowed by Issue#2");
  fireEvent.change(note, { target: { value: "blocks the release" } });
  fireEvent.click(within(note.closest("form")!).getByRole("button", { name: "Save" }));
  await screen.findByText("Note set to blocks the release");
  await pressToast("Undo");
  await waitFor(() => expect(server.writes()).toHaveLength(2));
  // The input follows the stored note back, so a later Save cannot re-apply the undone one.
  await waitFor(() => expect(note).toHaveProperty("value", "old"));
  const path = `/api/edges/${CHILD.id}/narrows/${SELF.id}/note`;
  expect(server.writes()).toEqual([
    { call: `PUT ${path}`, body: { value: "blocks the release" } },
    { call: `PUT ${path}`, body: { value: "old" } },
  ]);
});

test("an emptied note is cleared with DELETE", async () => {
  const server = await open([SELF, CHILD], [{ from: CHILD.id, kind: "narrows", to: SELF.id, note: "old" }]);
  const note = openNote("narrowed by Issue#2");
  fireEvent.change(note, { target: { value: " " } });
  fireEvent.click(within(note.closest("form")!).getByRole("button", { name: "Save" }));
  await screen.findByText("Cleared Note");
  expect(server.writes()).toEqual([{ call: `DELETE /api/edges/${CHILD.id}/narrows/${SELF.id}/note`, body: {} }]);
});

/** Open the annotations of `relation` and return its note's input. */
function openNote(relation: string): HTMLElement {
  rowAction("Annotate", relation);
  return within(screen.getByRole("group", { name: `Annotations of ${relation}` })).getByRole("textbox", { name: "Note" });
}

test("a label added is one PATCH", async () => {
  const server = await open([SELF, CHILD], [{ from: CHILD.id, kind: "narrows", to: SELF.id, labels: ["keep"] }]);
  rowAction("Annotate", "narrowed by Issue#2");
  const panel = screen.getByRole("group", { name: "Annotations of narrowed by Issue#2" });
  fireEvent.click(within(panel).getByRole("button", { name: "Label" }));
  const search = screen.getByRole("combobox", { name: "Add a label…" });
  fireEvent.change(search, { target: { value: "urgent" } });
  fireEvent.keyDown(search, { key: "Enter" });
  await screen.findByText("Added label urgent");
  const labels = `/api/edges/${CHILD.id}/narrows/${SELF.id}/labels`;
  expect(server.writes()).toEqual([{ call: `PATCH ${labels}`, body: { op: "add", value: "urgent" } }]);
});

test("a label removed is one PATCH", async () => {
  const server = await open([SELF, CHILD], [{ from: CHILD.id, kind: "narrows", to: SELF.id, labels: ["keep"] }]);
  rowAction("Annotate", "narrowed by Issue#2");
  const panel = screen.getByRole("group", { name: "Annotations of narrowed by Issue#2" });
  fireEvent.click(await within(panel).findByRole("button", { name: "Remove label keep" }));
  await screen.findByText("Removed label keep");
  const labels = `/api/edges/${CHILD.id}/narrows/${SELF.id}/labels`;
  expect(server.writes()).toEqual([{ call: `PATCH ${labels}`, body: { op: "sub", value: "keep" } }]);
});

test("a child's priority under this parent is a PUT; it has no valence", async () => {
  const server = await open([SELF, CHILD], [{ from: CHILD.id, kind: "narrows", to: SELF.id, priority: 30 }]);
  rowAction("Annotate", "narrowed by Issue#2");
  const panel = screen.getByRole("group", { name: "Annotations of narrowed by Issue#2" });
  expect(within(panel).queryByRole("slider")).toBeNull();
  const priority = within(within(panel).getByRole("group", { name: "Priority" })).getByRole("button");
  expect(priority.textContent).toBe("P3 Low (30)");
  fireEvent.click(priority);
  fireEvent.click(screen.getByRole("option", { name: "P1 High" }));
  await waitFor(() => expect(priority.textContent).toBe("P1 High (10)"));
  const path = `/api/edges/${CHILD.id}/narrows/${SELF.id}/priority`;
  expect(server.writes()).toEqual([{ call: `PUT ${path}`, body: { value: 10 } }]);
});

test("a child's No priority under this parent is a DELETE", async () => {
  const server = await open([SELF, CHILD], [{ from: CHILD.id, kind: "narrows", to: SELF.id, priority: 10 }]);
  rowAction("Annotate", "narrowed by Issue#2");
  const panel = screen.getByRole("group", { name: "Annotations of narrowed by Issue#2" });
  const priority = within(within(panel).getByRole("group", { name: "Priority" })).getByRole("button");
  expect(priority.textContent).toBe("P1 High (10)");
  fireEvent.click(priority);
  fireEvent.click(screen.getByRole("option", { name: "No priority" }));
  await waitFor(() => expect(priority.textContent).toBe("No priority"));
  const path = `/api/edges/${CHILD.id}/narrows/${SELF.id}/priority`;
  expect(server.writes()).toEqual([{ call: `DELETE ${path}`, body: {} }]);
});

test("a citation's valence is a PUT, Undo puts the old one back, and a citation has no priority", { tags: ["manual"] }, async () => {
  const claim = row("Belief", 1);
  const evidence = row("Artifact", 2, { title: "Rerun" });
  history.replaceState(null, "", "#/ref/Belief/1");
  const server = await open([claim, evidence], [{ from: evidence.id, kind: "proves", to: claim.id, valence: 0.5 }]);
  rowAction("Annotate", "proved by Artifact#2");
  const panel = screen.getByRole("group", { name: "Annotations of proved by Artifact#2" });
  expect(within(panel).queryByRole("group", { name: "Priority" })).toBeNull();
  const valence = within(panel).getByRole("slider", { name: /Valence/ });
  fireEvent.change(valence, { target: { value: "-0.4" } });
  expect(within(panel).getByText("-0.4").tagName).toBe("OUTPUT");
  fireEvent.click(within(valence.closest("form")!).getByRole("button", { name: "Save" }));
  await screen.findByText("Valence set to -0.4");
  await pressToast("Undo");
  await waitFor(() => expect(valence).toHaveProperty("value", "0.5"));
  const path = `/api/edges/${evidence.id}/proves/${claim.id}/valence`;
  expect(server.writes()).toEqual([
    { call: `PUT ${path}`, body: { value: -0.4 } },
    { call: `PUT ${path}`, body: { value: 0.5 } },
  ]);
});

/** Issue#1 narrows Issue#3, and was produced by it. */
const NARROWS_AND_PROVENANCE: StoredEdge[] = [
  { from: SELF.id, kind: "narrows", to: ORIGIN.id },
  { from: SELF.id, kind: "produced_by", to: ORIGIN.id },
];

test("Remove asks first and names the provenance edge that stays; Escape closes the step only", async () => {
  await open([SELF, ORIGIN], NARROWS_AND_PROVENANCE);
  rowAction("Remove", "narrows Issue#3");
  const confirm = screen.getByRole("form", { name: "Remove Issue#1 narrows Issue#3" });
  expect(document.activeElement).toBe(within(confirm).getByRole("button", { name: "Cancel" }));
  expect(within(confirm).getByRole("note").textContent).toMatch(/^Issue#1 stays produced by Issue#3\. That provenance relation is separate/);
  fireEvent.keyDown(within(confirm).getByRole("button", { name: "Cancel" }), { key: "Escape" });
  expect(screen.queryByRole("form")).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Remove narrows Issue#3" }));
  // Escape closed the step only; the detail is still open.
  expect(location.hash).toBe("#/ref/Issue/1");
});

test("Remove sends the reason given, and the provenance edge stays", async () => {
  const server = await open([SELF, ORIGIN], NARROWS_AND_PROVENANCE);
  rowAction("Remove", "narrows Issue#3");
  const confirm = screen.getByRole("form", { name: "Remove Issue#1 narrows Issue#3" });
  fireEvent.change(within(confirm).getByRole("textbox", { name: "Reason" }), { target: { value: "duplicate" } });
  fireEvent.click(within(confirm).getByRole("button", { name: "Remove" }));
  await screen.findByText("Removed: Issue#1 narrows Issue#3");
  expect([...document.querySelectorAll(".edge-name")].map((edge) => edge.textContent)).toEqual(["produced_by"]);
  expect(server.writes()).toEqual([{ call: `DELETE /api/edges/${SELF.id}/narrows/${ORIGIN.id}`, body: { reason: "duplicate" } }]);
});

test("removing the provenance edge itself leaves nothing behind to name", async () => {
  await open([SELF, ORIGIN], [{ from: SELF.id, kind: "produced_by", to: ORIGIN.id }]);
  rowAction("Remove", "produced by Issue#3");
  expect(within(screen.getByRole("form", { name: "Remove Issue#1 produced by Issue#3" })).queryByRole("note")).toBeNull();
});

/** Open Belief#1, proved by Artifact#2 with a note and a valence; returns the server and the annotations panel. */
async function openCitation() {
  const claim = row("Belief", 1);
  const evidence = row("Artifact", 2);
  history.replaceState(null, "", "#/ref/Belief/1");
  const server = await open([claim, evidence], [{ from: evidence.id, kind: "proves", to: claim.id, valence: 0.5, note: "old" }]);
  rowAction("Annotate", "proved by Artifact#2");
  return { server, path: `/api/edges/${evidence.id}/proves/${claim.id}`, panel: screen.getByRole("group", { name: "Annotations of proved by Artifact#2" }) };
}

test("a note being saved takes no more input, so its save cannot overwrite it (REV-D3-01)", async () => {
  const { server, path, panel } = await openCitation();
  const note = within(panel).getByRole("textbox", { name: "Note" }) as HTMLInputElement;
  fireEvent.change(note, { target: { value: "first" } });
  const release = server.hold("PUT", `${path}/note`);
  fireEvent.click(within(note.closest("form")!).getByRole("button", { name: "Save" }));
  await within(note.closest("form")!).findByText("Saving…");
  expect(note.readOnly).toBe(true);
  expect(within(note.closest("form")!).getByRole("button", { name: "Save" }).getAttribute("aria-disabled")).toBe("true");
  release();
  await screen.findByText("Note set to first");
  expect(note.readOnly).toBe(false);
});

test("a valence being saved takes no more input, so its save cannot overwrite it (REV-D3-01)", async () => {
  const { server, path, panel } = await openCitation();
  const valence = within(panel).getByRole("slider", { name: /Valence/ }) as HTMLInputElement;
  fireEvent.change(valence, { target: { value: "-0.4" } });
  const again = server.hold("PUT", `${path}/valence`);
  fireEvent.click(within(valence.closest("form")!).getByRole("button", { name: "Save" }));
  await within(valence.closest("form")!).findByText("Saving…");
  expect(valence.disabled).toBe(true);
  again();
  await screen.findByText("Valence set to -0.4");
});

/** Open the annotations of Issue#2 narrowing Issue#1, labelled `keep` at P3; returns the server and the panel. */
async function openLabelledChild() {
  const server = await open([SELF, CHILD], [{ from: CHILD.id, kind: "narrows", to: SELF.id, labels: ["keep"], priority: 30 }]);
  rowAction("Annotate", "narrowed by Issue#2");
  return { server, panel: screen.getByRole("group", { name: "Annotations of narrowed by Issue#2" }) };
}

test("while an edge's label saves, its label control reads as off and opens nothing (R4-F08, BR-03)", async () => {
  const { server, panel } = await openLabelledChild();
  const release = server.hold("PATCH", `/api/edges/${CHILD.id}/narrows/${SELF.id}/labels`);
  fireEvent.click(within(panel).getByRole("button", { name: "Remove label keep" }));
  await within(panel).findByText("Saving…");
  const add = within(panel).getByRole("button", { name: "Label" });
  expect(add.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(add);
  expect(screen.queryByRole("combobox")).toBeNull();
  release();
  await screen.findByText("Removed label keep");
  expect(server.writes().map((write) => write.call.split(" ")[0])).toEqual(["PATCH"]);
});

test("while an edge's priority saves, its priority control reads as off and opens nothing (R4-F08, BR-03)", async () => {
  const { server, panel } = await openLabelledChild();
  const priority = within(within(panel).getByRole("group", { name: "Priority" })).getByRole("button");
  fireEvent.click(priority);
  const held = server.hold("PUT", `/api/edges/${CHILD.id}/narrows/${SELF.id}/priority`);
  fireEvent.click(screen.getByRole("option", { name: "P1 High" }));
  await within(panel).findByText("Saving…");
  await act(() => new Promise((resolve) => setTimeout(resolve)));
  expect(priority.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(priority);
  expect(screen.queryByRole("option", { name: "P0 Critical" })).toBeNull();
  held();
  await waitFor(() => expect(priority.textContent).toBe("P1 High (10)"));
  expect(server.writes().map((write) => write.call.split(" ")[0])).toEqual(["PUT"]);
});

test("Escape pressed while an input method composes does not close the remove step (REV-D4-01's class)", async () => {
  await open([SELF, ORIGIN], [{ from: SELF.id, kind: "narrows", to: ORIGIN.id }]);
  rowAction("Remove", "narrows Issue#3");
  const confirm = screen.getByRole("form", { name: "Remove Issue#1 narrows Issue#3" });
  fireEvent.keyDown(within(confirm).getByRole("textbox", { name: "Reason" }), { key: "Escape", isComposing: true });
  expect(screen.getByRole("form", { name: "Remove Issue#1 narrows Issue#3" })).toBeTruthy();
});
