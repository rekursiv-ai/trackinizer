import { QueryClient } from "@tanstack/react-query";
import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { editableFields } from "../api/fields";
import { stubFetch } from "../api/testing";
import { CommandRegistry } from "../commands/registry";
import { stubLayout } from "../editors/testing";
import { openedAt } from "./queries";
import { change, detail, META, PROFILE, peer, renderDetail, row, serveDetails, uuid } from "./testing";

beforeEach(() => {
  history.replaceState(null, "", "#/ref/Issue/1");
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

/** The properties panel's value for the field labelled `label`. */
function prop(label: string): HTMLElement {
  // By its label: a query by role works out the style of the whole detail first.
  const term = within(screen.getByLabelText("Properties", { selector: "aside" })).getByText(label, {
    selector: "dt",
  });
  return term.nextElementSibling as HTMLElement;
}

/** The relation group labelled `label`. Role queries are slow on a hub's rows, so this walks the DOM. */
function group(label: string) {
  const heading = [...document.querySelectorAll(".rel-group-h span")].find((span) => span.textContent === label);
  return within(heading!.closest<HTMLElement>('[role="group"]')!);
}

/** The links in the relation group labelled `label`. */
function groupLinks(label: string): HTMLAnchorElement[] {
  const heading = [...document.querySelectorAll(".rel-group-h span")].find((span) => span.textContent === label);
  return [...heading!.closest('[role="group"]')!.querySelectorAll("a")];
}

/** The rail section `name` ("Parents", "Children"). */
function section(name: string): HTMLElement {
  return screen.getByRole("region", { name });
}

/** The peers in rail section `name`, each as its link's text and the names of the edges joining it. */
function rail(name: string): [string | null, (string | null)[]][] {
  return [...section(name).querySelectorAll(".rail-peer")].map((item) => [
    item.querySelector(".rail-link")!.textContent,
    [...item.querySelectorAll(".edge-name")].map((edge) => edge.textContent),
  ]);
}

test("a Kind#seq link resolves its id, then shows the inquiry", async () => {
  const root = row("Issue", 1, { description: "See Belief#2 and [the plan](https://example.com/p)." });
  const sent = serveDetails([detail(root)]);
  renderDetail({ kind: "Issue", seq: 1 });
  expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Issue number 1");
  expect(sent.map((request) => request.path)).toEqual(["/api/inquiries/Issue/1", `/api/web/get/${root.id}`]);
  const description = screen.getByText(/See/).closest("p")!;
  expect(within(description).getByRole("link", { name: "Belief#2" }).getAttribute("href")).toBe("#/ref/Belief/2");
  expect(within(description).getByRole("link", { name: "the plan" }).getAttribute("href")).toBe("https://example.com/p");
});

test("a Kind#seq link to a row a list holds opens with one read, the detail, by the row's id (SP6)", async () => {
  const held = row("Issue", 7);
  const sent = serveDetails([detail(held)]);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(["inquiries", "list", [], "Issue", 50, 0, null], [row("Issue", 6), held]);
  renderDetail({ kind: "Issue", seq: 7 }, queryClient);
  expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Issue number 7");
  expect(sent.map((request) => request.path)).toEqual([`/api/web/get/${held.id}`]);
});

test("a Kind#seq link to a relation an open detail shows opens by the relation's id (SP6)", async () => {
  const child = row("Issue", 8);
  const sent = serveDetails([detail(child)]);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(["detail", uuid(1)], detail(row("Issue", 1), { backlinks: { narrows: [peer("Issue", 8)] } }));
  renderDetail({ kind: "Issue", seq: 8 }, queryClient);
  expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Issue number 8");
  expect(sent.map((request) => request.path)).toEqual([`/api/web/get/${child.id}`]);
});

test("a lookup link reads the inquiry by id in one request", async () => {
  const belief = row("Belief", 5);
  const sent = serveDetails([detail(belief)]);
  renderDetail({ id: belief.id });
  expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Belief number 5");
  expect(screen.getByRole("link", { name: "Beliefs" }).getAttribute("href")).toBe("#/list/Belief");
  expect(sent.map((request) => request.path)).toContain(`/api/web/get/${belief.id}`);
  expect(sent.map((request) => request.path)).not.toContain("/api/inquiries/Belief/5");
});

test("a detail draws its page in a render after the one its inquiry lands in, which shows the frame", async () => {
  const belief = row("Belief", 5);
  serveDetails([detail(belief)]);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // Each commit: whether the inquiry had landed, and whether the page showed.
  const commits: string[] = [];
  const onCommit = () => {
    const landed = queryClient.getQueryData(["detail", belief.id]) !== undefined;
    const commit = `landed ${landed}, page ${document.querySelector(".d-scroll") !== null}`;
    if (commit !== commits.at(-1)) commits.push(commit);
  };
  renderDetail({ id: belief.id }, queryClient, { onCommit });
  await screen.findByRole("heading", { level: 1, name: "Belief number 5" });
  expect(commits).toEqual(["landed false, page false", "landed true, page false", "landed true, page true"]);
});

test("an Artifact lookup link shows its published content", async () => {
  const artifact = row("Artifact", 7);
  stubFetch((request) => {
    const path = new URL(request.url).pathname;
    if (path === `/api/web/get/${artifact.id}`) return Response.json(detail(artifact));
    if (path === `/api/artifacts/${artifact.id}/content`) return Response.json({
      revision: 1,
      artifact_id: artifact.id,
      issue_id: row("Issue", 1).id,
      title: "ARC3 atlas",
      summary: "Measured directions",
      author: "ada@example.com",
      created_at: "2026-09-30T10:00:00Z",
      scope: "team",
      format: "structured",
      html: null,
      citations: [],
      sections: [{ title: "Representation", summary: "Two matched results", details: "Methods", findings: [] }],
    });
    return Response.json({ detail: "not found" }, { status: 404 });
  });
  renderDetail({ id: artifact.id });
  // The content is a chunk of its own. Waited for inside act, it shows at once;
  // outside it, React keeps the fallback on screen for 300 ms first.
  await screen.findByText("Loading Artifact…");
  await act(() => vi.dynamicImportSettled());

  expect(await screen.findByRole("heading", { name: "ARC3 atlas" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Representation" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "Link to this Artifact" }).getAttribute("href"))
    .toBe(`#/lookup/${artifact.id}`);
});

test("an ordinary Artifact keeps its detail page when no content is published", async () => {
  const artifact = row("Artifact", 8);
  serveDetails([detail(artifact)]);
  renderDetail({ id: artifact.id });

  expect(await screen.findByRole("heading", { name: "Artifact number 8" })).toBeTruthy();
  await waitFor(() => expect(screen.queryByText("Loading Artifact…")).toBeNull());
  expect(screen.queryByText(/Could not load this Artifact revision/)).toBeNull();
});

test("a hub's 64 children show once each, by both edges, ten first and then all", { tags: ["manual"] }, async () => {
  const children = Array.from({ length: 64 }, (_, k) => peer("Issue", k + 2));
  serveDetails([detail(row("Issue", 1), { backlinks: { narrows: children, produced_by: children } })]);
  renderDetail({ kind: "Issue", seq: 1 });
  await screen.findByRole("heading", { level: 1 });
  expect(rail("Children")).toHaveLength(10);
  await userEvent.click(within(section("Children")).getByRole("button", { name: "Show all 64" }));
  await waitFor(() => expect(rail("Children")).toHaveLength(64));
  expect(rail("Children").every(([, edges]) => edges.join() === "narrowed_by,produces")).toBe(true);
  expect(screen.queryByRole("region", { name: /Other relations/ })).toBeNull();
});

test("a group over 100 shows 100 rows, then all on Show all", { tags: ["manual"] }, async () => {
  const evidence = Array.from({ length: 150 }, (_, k) => peer("Paper", k + 2));
  serveDetails([detail(row("Belief", 1), { backlinks: { proves: evidence } })]);
  renderDetail({ kind: "Belief", seq: 1 });
  await screen.findByRole("heading", { level: 1 });
  await waitFor(() => expect(groupLinks("Proved by")).toHaveLength(100));
  await userEvent.click(screen.getByText("Show all 150"));
  await waitFor(() => expect(groupLinks("Proved by")).toHaveLength(150));
});

describe("every field of the kind shows, set or not", () => {
  test.each(META.kinds)("%s", async (kind) => {
    const bare = row(kind, 1);
    serveDetails([detail(bare)]);
    const view = renderDetail({ kind, seq: 1 });
    await screen.findByRole("heading", { level: 1 });
    const owner = kind.toLowerCase();
    const names = new Set([
      ...Object.keys(editableFields(kind)),
      ...Object.keys(META.fieldOwners).filter((name) => META.fieldOwners[name] === owner),
      ...Object.keys(bare),
    ]);
    // Shown as the title, the header's dates and the Details identity rows, and
    // the cost axes' nesting, whose two fields are schema names above.
    for (const shownElsewhere of ["title", "id", "kind", "seq", "created", "modified", "marginal_cost"]) {
      names.delete(shownElsewhere);
    }
    const missing = [...names].filter((name) => !view.container.querySelector(`[data-field="${name}"]`));
    expect(missing).toEqual([]);
  });
});

test("a long description renders a part at a time, then whole", async () => {
  const notes = Array.from({ length: 15 }, (_, k) => `## Note ${k + 1}\n\n${"Measured the list route again. ".repeat(33)}`);
  serveDetails([detail(row("Issue", 1, { description: notes.join("\n\n") }))]);
  renderDetail({ kind: "Issue", seq: 1 });
  await screen.findByRole("heading", { level: 1 });
  const description = document.querySelector<HTMLElement>('[data-field="description"]')!;
  await waitFor(() => expect(description.querySelectorAll(".md-part")).toHaveLength(3));
  expect([...description.querySelectorAll("h4")].map((heading) => heading.textContent)).toEqual(
    notes.map((_, k) => `Note ${k + 1}`),
  );
});

test("unset fields say so, including the plan's No priority and No type", async () => {
  serveDetails([detail(row("Issue", 1))]);
  renderDetail({ kind: "Issue", seq: 1 });
  await screen.findByRole("heading", { level: 1 });
  expect(prop("Priority").textContent).toBe("No priority");
  expect(prop("Type").textContent).toBe("No type");
  expect(prop("Owner").textContent).toBe("—");
  expect(prop("Labels").textContent).toBe("—");
  expect(prop("Agent cost").textContent).toBe("None recorded");
  expect(screen.getByRole("region", { name: "Done when" }).textContent).toBe("Done whenNot set");
  expect(screen.getByText("No description.")).toBeTruthy();
});

test("an AgentSession with no end reads Live, as the old UI's live did; an ended one, its end", async () => {
  serveDetails([
    detail(row("AgentSession", 1, { started: "2026-09-24T10:00:00+00:00" })),
    detail(row("AgentSession", 2, { ended: "2026-09-24T11:30:00+00:00" })),
  ]);
  renderDetail({ kind: "AgentSession", seq: 1 });
  await screen.findByRole("heading", { level: 1 });
  expect(prop("Ended").textContent).toBe("Live");
  cleanup();
  renderDetail({ kind: "AgentSession", seq: 2 });
  await screen.findByRole("heading", { level: 1 });
  expect(prop("Ended").textContent).not.toBe("Live");
  expect(prop("Ended").textContent).toMatch(/2026/);
});

test("set fields show their values, with safe outside links", async () => {
  const paper = row("Paper", 3, {
    authors: ["Ada Lovelace", "Alan Turing"],
    source: 'arXiv:2401.1" onmouseover="window.__injected=1',
    publish_date: "2024-01-05T00:00:00+00:00",
    labels: ["ml"],
    abstract: "About **layouts**.",
  });
  serveDetails([detail(paper)]);
  const view = renderDetail({ kind: "Paper", seq: 3 });
  await screen.findByRole("heading", { level: 1 });
  expect(prop("Authors").textContent).toBe("Ada LovelaceAlan Turing");
  expect(prop("Published").textContent).toBe("Jan 5, 2024");
  const open = within(prop("Source")).getByRole("link", { name: "Open Source" });
  expect(open.getAttribute("href")).toBe('https://arxiv.org/abs/2401.1" onmouseover="window.__injected=1');
  // COLD-12: text stays text; no element gains a handler.
  expect(view.container.querySelectorAll("[onmouseover]")).toHaveLength(0);
  expect(within(screen.getByRole("region", { name: "Abstract" })).getByText("layouts").tagName).toBe("STRONG");
});

test("a byline that names one author twice shows both, each its own row (R2-X2)", async () => {
  const error = vi.spyOn(console, "error").mockImplementation(() => {});
  serveDetails([detail(row("Paper", 1, { authors: ["Ada Lovelace", "Ada Lovelace", "Alan Turing"] }))]);
  renderDetail({ kind: "Paper", seq: 1 });
  await screen.findByRole("heading", { level: 1 });
  expect([...prop("Authors").querySelectorAll(".kind-tag")].map((tag) => tag.textContent)).toEqual([
    "Ada Lovelace",
    "Ada Lovelace",
    "Alan Turing",
  ]);
  expect(error.mock.calls.flat().join(" ")).not.toMatch(/same key/);
  error.mockRestore();
});

test("the rail lists each parent and child once, with its title, status and every edge name (COLD-16)", async () => {
  // A real record's shape: a parent it both narrows and was produced by, a second
  // parent, and two outputs; plus a citation, which the rail leaves out.
  const pickup = { title: "ARC3 pickup", status: "complete" };
  serveDetails([
    detail(row("Issue", 937, { description: "The plan." }), {
      edges: {
        narrows: [peer("Issue", 760, pickup), peer("Issue", 4, { title: "Second parent" })],
        produced_by: [peer("Issue", 760, pickup)],
      },
      backlinks: {
        produced_by: [peer("AgentSession", 632, { title: "The session" }), peer("Experiment", 589, { title: "spc001" })],
      },
    }),
  ]);
  renderDetail({ kind: "Issue", seq: 937 });
  await screen.findByRole("heading", { level: 1 });
  expect(rail("Parents")).toEqual([
    ["Second parent Issue#4", ["narrows"]],
    ["ARC3 pickup Issue#760", ["narrows", "produced_by"]],
  ]);
  expect(rail("Children")).toEqual([
    ["The session AgentSession#632", ["produces"]],
    ["spc001 Experiment#589", ["produces"]],
  ]);
  const pickupLink = within(section("Parents")).getByRole("link", { name: /ARC3 pickup/ });
  expect(pickupLink.getAttribute("href")).toBe("#/ref/Issue/760");
  expect(within(pickupLink).getByRole("img", { name: "Complete" })).toBeTruthy();
  // The rail is the one place for these edges: no other list repeats them, and
  // the eyebrow names the kind, not the parents.
  expect(screen.queryByRole("region", { name: /Other relations/ })).toBeNull();
  expect(within(document.querySelector<HTMLElement>(".eyebrow")!).queryAllByRole("link")).toEqual([]);
});

test("the header and text fields, then the rail and properties, then config, other relations and activity", async () => {
  // One column when narrow, in this order: the rail stacks under the text, not between the title and it.
  serveDetails([
    detail(row("Experiment", 1, { description: "The plan.", outcome: "It held.", config: { lr: 1 } }), {
      edges: { produced_by: [peer("Issue", 2)] },
      backlinks: { proves: [peer("Paper", 3)] },
    }),
  ]);
  renderDetail({ kind: "Experiment", seq: 1 });
  const order = [
    await screen.findByRole("heading", { level: 1 }),
    screen.getByText("The plan."),
    screen.getByRole("region", { name: "Outcome" }),
    section("Parents"),
    section("Children"),
    screen.getByRole("complementary", { name: "Properties" }),
    document.querySelector<HTMLElement>('[data-field="config"]')!,
    screen.getByRole("region", { name: /^Other relations/ }),
    await screen.findByRole("heading", { name: /^Activity/ }),
  ];
  const follows = order.slice(1).map((next, k) => Boolean(order[k]!.compareDocumentPosition(next) & Node.DOCUMENT_POSITION_FOLLOWING));
  expect(follows).toEqual(order.slice(1).map(() => true));
});

test("the rail's button in the top bar, or ], collapses the rail and expands it again", async () => {
  serveDetails([detail(row("Issue", 1), { backlinks: { narrows: [peer("Issue", 2)] } })]);
  renderDetail({ kind: "Issue", seq: 1 });
  await screen.findByRole("heading", { level: 1 });
  // Found once: a query by role names every button on the page, some 10 ms a call.
  // The button stays the same element as it collapses and expands the rail.
  const toggle = screen.getByRole("button", { name: /parents, children and properties$/ });
  expect(toggle.closest(".detail-top")).not.toBeNull();
  expect(document.getElementById(toggle.getAttribute("aria-controls")!)!.contains(section("Children"))).toBe(true);
  await userEvent.click(toggle);
  expect(toggle.getAttribute("aria-label")).toBe("Expand parents, children and properties");
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByRole("region", { name: "Children" })).toBeNull();
  expect(screen.queryByRole("complementary", { name: "Properties" })).toBeNull();
  await userEvent.keyboard("]");
  expect(rail("Children").map(([link]) => link)).toEqual(["Issue number 2 Issue#2"]);
});

describe("Show in graph links to the graph focused on the inquiry, two hops out", () => {
  test.each([
    { kind: "Issue", seq: 1 },
    { kind: "Experiment", seq: 5 },
  ])("$kind#$seq", async ({ kind, seq }) => {
    serveDetails([detail(row(kind, seq))]);
    renderDetail({ kind, seq });
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getByRole("link", { name: "Show in graph" }).getAttribute("href")).toBe(`#/graph?focus=${kind}/${seq}&hops=2`);
  });
});

test("Show in graph is a palette command under the inquiry's Kind#seq, with no key, and goes there", async () => {
  const commands = new CommandRegistry();
  serveDetails([detail(row("Belief", 4))]);
  renderDetail({ kind: "Belief", seq: 4 }, undefined, { commands });
  await screen.findByRole("heading", { level: 1 });
  const show = commands.getCommands().find((command) => command.title === "Show in graph");
  expect([show?.section, show?.keys]).toEqual(["Belief#4", undefined]);
  act(() => show!.run());
  expect(location.hash).toBe("#/graph?focus=Belief/4&hops=2");
});

test("Peek beside a list offers Show in graph; Peek in the graph, where it would lead, does not", async () => {
  // Peek holds the detail's commands in a registry of its own, so the route is what tells them apart.
  history.replaceState(null, "", "#/list/Issue");
  const listed = new CommandRegistry();
  serveDetails([detail(row("Issue", 1))]);
  renderDetail({ kind: "Issue", seq: 1 }, undefined, { commands: listed });
  await screen.findByRole("heading", { level: 1 });
  expect(screen.getByRole("link", { name: "Show in graph" })).toBeTruthy();
  expect(listed.getCommands().map((command) => command.title)).toContain("Show in graph");
  cleanup();
  history.replaceState(null, "", "#/graph");
  const graphed = new CommandRegistry();
  renderDetail({ kind: "Issue", seq: 1 }, undefined, { commands: graphed });
  await screen.findByRole("heading", { level: 1 });
  expect(screen.queryByRole("link", { name: "Show in graph" })).toBeNull();
  expect(graphed.getCommands().map((command) => command.title)).not.toContain("Show in graph");
});

test("with no relations, each section says so and nothing else lists relations", async () => {
  serveDetails([detail(row("Issue", 1))]);
  renderDetail({ kind: "Issue", seq: 1 });
  await screen.findByRole("heading", { level: 1 });
  expect(section("Parents").querySelector(".rail-none")?.textContent).toBe("None");
  expect(section("Children").querySelector(".rail-none")?.textContent).toBe("None");
  expect(screen.queryByRole("region", { name: /Other relations/ })).toBeNull();
});

test("a section shows its first ten peers, then all on Show all", async () => {
  const children = Array.from({ length: 12 }, (_, k) => peer("Issue", k + 2));
  serveDetails([detail(row("Issue", 1), { backlinks: { narrows: children } })]);
  renderDetail({ kind: "Issue", seq: 1 });
  await screen.findByRole("heading", { level: 1 });
  expect(within(section("Children")).getByText("12", { selector: ".count" })).toBeTruthy();
  expect(rail("Children")).toHaveLength(10);
  await userEvent.click(within(section("Children")).getByRole("button", { name: "Show all 12" }));
  await waitFor(() => expect(rail("Children")).toHaveLength(12));
  expect(within(section("Children")).queryByRole("button", { name: /Show all/ })).toBeNull();
});

test("+ add in each section opens the Add relation picker; a viewer has none", async () => {
  stubLayout();
  serveDetails([detail(row("Issue", 1))]);
  renderDetail({ kind: "Issue", seq: 1 });
  await screen.findByRole("heading", { level: 1 });
  const add = within(section("Children")).getByRole("button", { name: "Add child" });
  expect(add.textContent).toBe("+ add");
  expect(within(section("Parents")).getByRole("button", { name: "Add parent" })).toBeTruthy();
  await userEvent.click(add);
  expect(await screen.findByRole("dialog", { name: "Add relation" })).toBeTruthy();
  cleanup();
  serveDetails([detail(row("Issue", 1))]);
  renderDetail({ kind: "Issue", seq: 1 }, undefined, { profile: { ...PROFILE, role: "viewer" } });
  await screen.findByRole("heading", { level: 1 });
  expect(within(section("Parents")).queryByRole("button")).toBeNull();
  expect(within(section("Children")).queryByRole("button")).toBeNull();
});

test("other relations hold only the edge kinds the rail does not show", async () => {
  serveDetails([
    detail(row("Belief", 1), {
      backlinks: { proves: [peer("Paper", 3)], favors: [peer("Issue", 7)], produced_by: [peer("Issue", 8)] },
    }),
  ]);
  renderDetail({ kind: "Belief", seq: 1 });
  await screen.findByRole("heading", { level: 1 });
  const other = screen.getByRole("region", { name: "Other relations 2" });
  expect([...other.querySelectorAll(".rel-group-h span:not(.count)")].map((label) => label.textContent)).toEqual([
    "Proved by",
    "Favored by",
  ]);
  expect(rail("Children")).toEqual([["Issue number 8 Issue#8", ["produces"]]]);
});

test("a child's priority glyph is its edge priority under this parent (COLD-17)", async () => {
  serveDetails([
    detail(row("Issue", 1, { priority: 10 }), {
      backlinks: { narrows: [peer("Issue", 2, { priority: 0 }), peer("Issue", 3)] },
      edges: { narrows: [peer("Issue", 8, { priority: 30 })] },
    }),
  ]);
  renderDetail({ kind: "Issue", seq: 1 });
  await screen.findByRole("heading", { level: 1 });
  const [edged, plain] = section("Children").querySelectorAll<HTMLElement>(".rail-edges");
  expect(within(edged!).getByRole("img", { name: "P0 Critical" })).toBeTruthy();
  expect(within(plain!).queryByRole("img", { name: /^P\d|No priority/ })).toBeNull();
  // On a parent the edge priority is this Issue's own under it: a tag, not the parent's glyph.
  const parent = section("Parents").querySelector<HTMLElement>(".rail-peer")!;
  expect(within(parent).queryByRole("img", { name: /^P\d/ })).toBeNull();
  expect(within(parent).getByText("p30")).toBeTruthy();
  expect(within(prop("Priority")).getByRole("img", { name: "P1 High" })).toBeTruthy();
});

test("cost names its scope, keeps sub-cent values, and reads none recorded at zero", async () => {
  serveDetails([detail(row("Issue", 1, { marginal_cost: { agent_usd: 0.0042, resource_usd: 0 } })), detail(row("Belief", 2))]);
  renderDetail({ kind: "Issue", seq: 1 });
  await screen.findByRole("heading", { level: 1 });
  expect(document.querySelector(".d-cost")!.textContent).toBe("Cost of this issue $0.0042");
  expect(prop("Agent cost").textContent).toBe("$0.0042");
  expect(prop("Resource cost").textContent).toBe("None recorded");
  cleanup();
  renderDetail({ kind: "Belief", seq: 2 });
  await screen.findByRole("heading", { level: 1 });
  expect(document.querySelector(".d-cost")!.textContent).toBe("Cost of this belief none recorded");
});

test("invalid status and undecidable judgement render as themselves (COLD-14)", async () => {
  const belief = row("Belief", 2, { status: "invalid", judgement: "undecidable", confidence: 0.4 });
  serveDetails([
    detail(belief, {
      backlinks: {
        proves: [peer("Belief", 6, { status: "invalid", judgement: "undecidable", valence: -0.5 })],
        favors: [peer("Issue", 7, { status: "invalid" })],
      },
    }),
  ]);
  renderDetail({ kind: "Belief", seq: 2 });
  await screen.findByRole("heading", { level: 1 });
  expect(prop("Status").textContent).toBe("Invalid");
  expect(within(prop("Status")).getByRole("img", { name: "Invalid" })).toBeTruthy();
  expect(prop("Judgement").textContent).toBe("Undecidable");
  const cited = group("Proved by").getByRole("link");
  expect(within(cited).getAllByRole("img").map((img) => img.getAttribute("aria-label"))).toEqual([
    "Undecidable",
    "Invalid",
  ]);
  expect(within(group("Favored by").getByRole("link")).getAllByRole("img").map((img) => img.getAttribute("aria-label"))).toEqual(
    ["Invalid"],
  );
});

test("evidence confidence shows beside the author's, under its own name", async () => {
  const belief = row("Belief", 2, { judgement: "proven", confidence: 0.9 });
  const sent = serveDetails([detail(belief, { backlinks: { proves: [peer("Paper", 3, { valence: 0.8 })] } })], 0.69);
  renderDetail({ kind: "Belief", seq: 2 });
  await screen.findByRole("heading", { level: 1 });
  expect(prop("Author conf.").textContent).toBe("0.90");
  await waitFor(() => expect(prop("Evidence").textContent).toBe("0.69"));
  const terms = within(screen.getByRole("complementary", { name: "Properties" }))
    .getAllByRole("term")
    .map((term) => term.textContent);
  expect(terms.indexOf("Evidence")).toBe(terms.indexOf("Author conf.") + 1);
  expect(sent.map((request) => request.path)).toContain(`/api/inquiries/${belief.id}/confidence`);
});

test("with no proves edge in, evidence confidence is the neutral value and says so", async () => {
  serveDetails([detail(row("Experiment", 1))], 0.5);
  renderDetail({ kind: "Experiment", seq: 1 });
  await screen.findByRole("heading", { level: 1 });
  await waitFor(() => expect(prop("Evidence").textContent).toBe("0.50no proves yet"));
});

test("evidence confidence that fails says why with Retry, first read or refresh, and keeps its value (READ-03)", async () => {
  const belief = row("Belief", 2);
  let refuse = true;
  stubFetch((request) => {
    const path = new URL(request.url).pathname;
    if (path === `/api/web/get/${belief.id}`) return Response.json(detail(belief));
    return refuse ? Response.json({ detail: "statement timeout" }, { status: 500 }) : Response.json({ confidence: 0.69 });
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderDetail({ id: belief.id }, queryClient);
  await waitFor(() => expect(prop("Evidence").textContent).toBe("statement timeoutRetry"));
  refuse = false;
  await userEvent.click(within(prop("Evidence")).getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(prop("Evidence").textContent).toBe("0.69no proves yet"));
  refuse = true;
  await act(() => queryClient.refetchQueries({ queryKey: ["confidence", belief.id] }));
  await waitFor(() => expect(within(prop("Evidence")).getByRole("alert").textContent).toBe("Could not refresh: statement timeoutRetry"));
  expect(prop("Evidence").textContent).toMatch(/^0\.69/);
});

test("opening a detail marks it opened, for the palette's recent list (WEB-09)", async () => {
  const belief = row("Belief", 5);
  serveDetails([detail(belief)]);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  expect(openedAt(queryClient, belief.id)).toBe(0);
  renderDetail({ id: belief.id }, queryClient);
  await screen.findByRole("heading", { level: 1 });
  expect(openedAt(queryClient, belief.id)).toBeGreaterThan(0);
});

test("a kind no proves edge can reach asks for no evidence confidence", async () => {
  const sent = serveDetails([detail(row("Issue", 1))]);
  renderDetail({ kind: "Issue", seq: 1 });
  await screen.findByRole("heading", { level: 1 });
  expect(sent.some((request) => request.path.endsWith("/confidence"))).toBe(false);
  expect(screen.queryByText("Evidence", { selector: "dt" })).toBeNull();
});

test("activity reads oldest first, names fields and relations, and folds alerts at their newest time", async () => {
  // Minutes after the changes, so each line's time reads relative, not as a date.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-09-24T10:10:00+00:00"));
  const self = row("Issue", 1, { priority: 10 });
  const child = peer("Issue", 2);
  const changes = [
    change(self, 1, { kind: "created" }),
    change(self, 2, {
      kind: "issue_priority",
      old: { issue_priority: 20 },
      new: { issue_priority: 10 },
      reason: "Blocks Belief#4.",
    }),
    change(self, 3, { kind: "labels", old: { labels: ["a"] }, new: { labels: ["a", "b"] } }),
    change(self, 4, { kind: "edge_added", new: { peer_id: child.id, peer_kind: "Issue", peer_edge_kind: "narrows" } }),
    change(self, 5, {
      kind: "dependency_changed",
      actor: "librarian",
      new: { peer_id: child.id, peer_kind: "Issue", peer_edge_kind: "narrows" },
    }),
    change(self, 6, {
      kind: "dependency_changed",
      actor: "librarian",
      new: { peer_id: "5f3a9c1e-0000-4000-8000-000000000099", peer_kind: "Issue", peer_edge_kind: "narrows" },
    }),
  ].reverse();
  serveDetails([detail(self, { backlinks: { narrows: [child] }, changes })]);
  renderDetail({ kind: "Issue", seq: 1 });
  // Activity renders after the rest of the page, in a deferred render.
  await screen.findByRole("heading", { name: /^Activity/ });
  const lines = [...document.querySelectorAll(".timeline > li")].map(
    (item) => (item.querySelector("summary") ?? item.querySelector(".tl-body"))!.textContent,
  );
  expect(lines).toEqual([
    "ada@example.com created this · 9m ago",
    "ada@example.com changed Priority from 20 to 10 · 8m agoBlocks Belief#4.",
    "ada@example.com added b to Labels · 7m ago",
    "ada@example.com added relation Narrowed by Issue#2 · 6m ago",
    "librarian 2 upstream changes · 4m ago",
  ]);
  expect(screen.getByRole("link", { name: "Belief#4" }).getAttribute("href")).toBe("#/ref/Belief/4");
  const alerts = document.querySelector(".timeline details")!;
  expect([...alerts.querySelectorAll("a")].map((link) => [link.textContent, link.getAttribute("href")])).toEqual([
    ["Issue#2", "#/ref/Issue/2"],
    ["Issue 5f3a9c1e", "#/lookup/5f3a9c1e-0000-4000-8000-000000000099"],
  ]);
});

test("a missing inquiry says it was deleted or purged", async () => {
  serveDetails([]);
  renderDetail({ kind: "Issue", seq: 404 });
  expect(await screen.findByText("Deleted or purged")).toBeTruthy();
  expect(screen.getByText(/is not in Trackinizer/).textContent).toBe("Issue#404 is not in Trackinizer.");
});

test("an open inquiry purged meanwhile says so on its next read", async () => {
  const self = row("Issue", 1);
  let purged = false;
  stubFetch(() => (purged ? Response.json({ detail: "not found" }, { status: 404 }) : Response.json(detail(self))));
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderDetail({ id: self.id }, queryClient);
  await screen.findByRole("heading", { level: 1 });
  purged = true;
  await act(() => queryClient.refetchQueries());
  expect(await screen.findByText("Deleted or purged")).toBeTruthy();
  expect(screen.queryByRole("heading", { level: 1 })).toBeNull();
});

test("a refetch that fails keeps the loaded inquiry, marked stale, with Retry", async () => {
  const self = row("Issue", 1);
  let failing = false;
  stubFetch(() => (failing ? Response.json({ detail: "statement timeout" }, { status: 400 }) : Response.json(detail(self))));
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderDetail({ id: self.id }, queryClient);
  await screen.findByRole("heading", { level: 1 });
  failing = true;
  await act(() => queryClient.refetchQueries());
  expect((await screen.findByRole("status")).textContent).toBe("Could not refresh Issue#1: statement timeoutRetry");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Issue number 1");
  failing = false;
  await userEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
});

test("a failed read shows the server's message, and Retry loads it", async () => {
  const self = row("Issue", 1);
  let refuse = true;
  stubFetch((request) =>
    refuse
      ? Response.json({ detail: "statement timeout" }, { status: 400 })
      : new URL(request.url).pathname.startsWith("/api/web/get/")
        ? Response.json(detail(self))
        : Response.json({ detail: "not found" }, { status: 404 }),
  );
  renderDetail({ id: self.id });
  expect((await screen.findByRole("alert")).textContent).toBe("statement timeout");
  refuse = false;
  await userEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Issue number 1");
});

test("Purge shows in the danger style in the inquiry's ⋯ menu", async () => {
  stubLayout();
  serveDetails([detail(row("Issue", 1))]);
  renderDetail({ kind: "Issue", seq: 1 });
  await screen.findByRole("heading", { level: 1 });
  await userEvent.click(screen.getByRole("button", { name: "Issue#1 actions" }));
  expect(screen.getByRole("option", { name: "Purge…" }).classList.contains("danger")).toBe(true);
  expect(screen.getByRole("option", { name: /^Add relation…/ }).classList.contains("danger")).toBe(false);
});

test("Esc goes back to the kind's list", async () => {
  serveDetails([detail(row("Paper", 1))]);
  renderDetail({ kind: "Paper", seq: 1 });
  await screen.findByRole("heading", { level: 1 });
  await userEvent.keyboard("{Escape}");
  expect(location.hash).toBe("#/list/Paper");
});
