import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { CommandRegistry, CommandRegistryContext, Shortcuts, useCommandList, useCommands } from "../commands/registry";
import { type PanelSpec, PanelStrip, PanelToggle, panelCommand, usePanel } from "./panel";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  sessionStorage.clear();
});

const RESULTS: PanelSpec = { id: "test.results", name: "search results", side: "left", keys: ["["] };

/** A panel as a view holds it: its toggle in a bar, the panel hidden while collapsed, its command mounted. */
function View({ spec = RESULTS }: { spec?: PanelSpec }) {
  const panel = usePanel(spec);
  useCommands([panelCommand(panel)]);
  return (
    <>
      <PanelToggle panel={panel} controls="results" />
      <aside id="results" aria-label="Search results" hidden={panel.collapsed}>
        <button type="button">A match</button>
      </aside>
    </>
  );
}

function Titles() {
  return <output>{useCommandList().map((command) => `${command.title} (${command.section})`).join(",")}</output>;
}

function show(view = <View />) {
  render(
    <CommandRegistryContext value={new CommandRegistry()}>
      <Shortcuts />
      <Titles />
      {view}
    </CommandRegistryContext>,
  );
  return userEvent.setup();
}

const toggle = () => screen.getByRole("button", { name: /search results$/ });

test("a panel starts expanded, and its toggle collapses and expands it, saying which it will do", async () => {
  const user = show();
  expect(toggle().getAttribute("aria-label")).toBe("Collapse search results");
  expect(toggle().getAttribute("aria-expanded")).toBe("true");
  expect(toggle().getAttribute("aria-controls")).toBe("results");
  expect(screen.getByRole("complementary", { name: "Search results" })).toBeTruthy();
  await user.click(toggle());
  expect(toggle().getAttribute("aria-label")).toBe("Expand search results");
  expect(toggle().getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByRole("complementary", { name: "Search results" })).toBeNull();
  await user.click(toggle());
  expect(toggle().getAttribute("aria-expanded")).toBe("true");
});

test("a panel opened on demand starts collapsed", () => {
  show(<View spec={{ ...RESULTS, startsCollapsed: true }} />);
  expect(toggle().getAttribute("aria-expanded")).toBe("false");
});

test("its key toggles it too, and the palette lists the command under Panels by what it will do; the title names the key", async () => {
  const user = show();
  expect(toggle().title).toBe("Collapse search results ([)");
  expect(screen.getByRole("status").textContent).toBe("Collapse search results (Panels)");
  // user-event spells the [ key "[[".
  await user.keyboard("[[");
  expect(toggle().getAttribute("aria-expanded")).toBe("false");
  expect(screen.getByRole("status").textContent).toBe("Expand search results (Panels)");
  await user.keyboard("[[");
  expect(toggle().getAttribute("aria-expanded")).toBe("true");
});

test("whether a panel is collapsed is kept for the tab, so it comes back as it was left; a kept value this build cannot read starts it afresh", async () => {
  const user = show();
  await user.click(toggle());
  cleanup();
  show();
  expect(toggle().getAttribute("aria-expanded")).toBe("false");
  cleanup();
  sessionStorage.setItem("trackinizer.v2.panel.test.results", JSON.stringify("yes"));
  show();
  expect(toggle().getAttribute("aria-expanded")).toBe("true");
});

/** Another view that reads the same panel, as the graph reads the app's sidebar to frame again. */
function Reader() {
  return <p>{usePanel(RESULTS).collapsed ? "collapsed" : "expanded"}</p>;
}

test("every view holding one panel sees it collapse at once", async () => {
  const user = show(
    <>
      <View />
      <Reader />
    </>,
  );
  expect(screen.getByText(/ed$/).textContent).toBe("expanded");
  await user.click(toggle());
  expect(screen.getByText(/ed$/).textContent).toBe("collapsed");
});

test("with storage refused, a panel still collapses and expands, unsaved", async () => {
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new DOMException("Full", "QuotaExceededError");
  });
  const user = show();
  await user.click(toggle());
  expect(toggle().getAttribute("aria-expanded")).toBe("false");
  await user.click(toggle());
  expect(toggle().getAttribute("aria-expanded")).toBe("true");
});

/** A pop-in panel: collapsed, it leaves a strip in its place. */
function PopIn() {
  const panel = usePanel({ ...RESULTS, side: "right" });
  useCommands([panelCommand(panel)]);
  return panel.collapsed ? (
    <PanelStrip panel={panel} id="peek" label="Peek">
      <span>Issue#7</span>
    </PanelStrip>
  ) : (
    <aside id="peek" aria-label="Peek">
      <PanelToggle panel={panel} controls="peek" />
      <a href="#/ref/Issue/7">Open</a>
    </aside>
  );
}

test("a collapsed pop-in panel leaves a strip: the same landmark, holding the button that expands it and what it holds", async () => {
  const user = show(<PopIn />);
  await user.click(toggle());
  const strip = screen.getByRole("complementary", { name: "Peek" });
  expect(strip.classList).toContain("panel-strip");
  expect(strip.classList).toContain("is-right");
  expect(strip.textContent).toBe("Issue#7");
  expect(toggle().getAttribute("aria-controls")).toBe(strip.id);
  // The button that went had the focus; the one that took its place takes it.
  expect(document.activeElement).toBe(toggle());
  await user.click(toggle());
  expect(screen.getByRole("complementary", { name: "Peek" }).classList).not.toContain("panel-strip");
  expect(document.activeElement).toBe(toggle());
});

test("a panel that comes back collapsed takes no focus", () => {
  sessionStorage.setItem("trackinizer.v2.panel.test.results", "true");
  show(<PopIn />);
  expect(toggle().getAttribute("aria-expanded")).toBe("false");
  expect(document.activeElement).toBe(document.body);
});

test("a panel collapsed by its key while it holds the focus hands the focus to the button that expands it", async () => {
  const user = show();
  screen.getByRole("button", { name: "A match" }).focus();
  await user.keyboard("[[");
  expect(toggle().getAttribute("aria-expanded")).toBe("false");
  expect(document.activeElement).toBe(toggle());
});

test("a pop-in panel's key hands the focus it holds to the button that takes its place, either way, and leaves a focus outside it be", async () => {
  const user = show(
    <>
      <button type="button">Outside</button>
      <PopIn />
    </>,
  );
  screen.getByRole("button", { name: "Outside" }).focus();
  await user.keyboard("[[");
  expect(screen.getByRole("complementary", { name: "Peek" }).classList).toContain("panel-strip");
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Outside" }));
  await user.keyboard("[[");
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Outside" }));
  screen.getByRole("link", { name: "Open" }).focus();
  await user.keyboard("[[");
  expect(document.activeElement).toBe(toggle());
  expect(toggle().getAttribute("aria-label")).toBe("Expand search results");
  await user.keyboard("[[");
  expect(document.activeElement).toBe(toggle());
  expect(toggle().getAttribute("aria-label")).toBe("Collapse search results");
});
