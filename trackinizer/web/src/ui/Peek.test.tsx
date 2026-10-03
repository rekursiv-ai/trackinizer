import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { CommandRegistry, CommandRegistryContext, Shortcuts, useCommands } from "../commands/registry";
import { panelCommand, usePanel } from "./panel";
import { PEEK, Peek } from "./Peek";

// The detail registers a page's keys, Escape back to the list among them, and
// keeps a draft in its state, as its editors do.
vi.mock("../router/views", () => ({
  DetailView: ({ target }: { target: { kind: string; seq: number } }) => {
    useCommands([{ id: "detail.back", title: "Back", keys: ["Escape"], run: () => {} }]);
    const [draft, setDraft] = useState("");
    return (
      <>
        <h1>
          Detail of {target.kind}#{target.seq}
        </h1>
        <textarea aria-label="Draft" value={draft} onChange={(event) => setDraft(event.target.value)} />
      </>
    );
  },
}));

afterEach(() => {
  cleanup();
  sessionStorage.clear();
});

/** Peek as a list or the graph holds it, with its panel's state and, with `keys`, its `]`. */
function Peeking({ onClose = () => {}, keys = false }: { onClose?: () => void; keys?: boolean }) {
  const panel = usePanel(PEEK);
  useCommands(keys ? [panelCommand(panel)] : []);
  return <Peek row={{ kind: "Issue", seq: 7 }} panel={panel} onClose={onClose} />;
}

test("Peek shows the row's detail under a bar with its ref, Open and a close button", async () => {
  const user = userEvent.setup();
  const onClose = vi.fn();
  render(
    <CommandRegistryContext value={new CommandRegistry()}>
      <Peeking onClose={onClose} />
    </CommandRegistryContext>,
  );
  expect((await screen.findByRole("heading")).textContent).toBe("Detail of Issue#7");
  expect(screen.getByRole("complementary", { name: "Peek" }).textContent).toContain("Issue#7");
  expect(screen.getByRole("link", { name: "Open" }).getAttribute("href")).toBe("#/ref/Issue/7");
  await user.click(screen.getByRole("button", { name: "Close peek" }));
  expect(onClose).toHaveBeenCalledOnce();
});

test("the detail's keys stay in Peek's own registry, so its Escape never acts as the page's", async () => {
  const page = new CommandRegistry();
  render(
    <CommandRegistryContext value={page}>
      <Peeking />
    </CommandRegistryContext>,
  );
  await screen.findByRole("heading");
  expect(page.getCommands()).toEqual([]);
});

test("Peek's bar collapses it to a strip at its side that keeps its ref and close button; the strip's button expands it, detail and all", async () => {
  const user = userEvent.setup();
  const onClose = vi.fn();
  render(
    <CommandRegistryContext value={new CommandRegistry()}>
      <Peeking onClose={onClose} />
    </CommandRegistryContext>,
  );
  await screen.findByRole("heading");
  const collapse = screen.getByRole("button", { name: "Collapse Peek" });
  expect(collapse.getAttribute("aria-controls")).toBe(screen.getByRole("complementary", { name: "Peek" }).id);
  await user.click(collapse);
  const strip = screen.getByRole("complementary", { name: "Peek" });
  expect(strip.classList).toContain("panel-strip");
  expect(strip.textContent).toBe("Issue#7");
  expect(screen.queryByRole("heading")).toBeNull();
  expect(screen.queryByRole("link", { name: "Open" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Expand Peek" }));
  expect((await screen.findByRole("heading")).textContent).toBe("Detail of Issue#7");
  await user.click(screen.getByRole("button", { name: "Collapse Peek" }));
  await user.click(screen.getByRole("button", { name: "Close peek" }));
  expect(onClose).toHaveBeenCalledOnce();
});

test("collapsing Peek keeps its detail, hidden, so an edit begun there is there on expanding", async () => {
  // With no timer turn between keystrokes, which cost this test some 20 ms.
  const user = userEvent.setup({ delay: null });
  render(
    <CommandRegistryContext value={new CommandRegistry()}>
      <Peeking />
    </CommandRegistryContext>,
  );
  await user.type(await screen.findByRole("textbox", { name: "Draft" }), "Half a thought");
  await user.click(screen.getByRole("button", { name: "Collapse Peek" }));
  expect(screen.queryByRole("textbox", { name: "Draft" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Expand Peek" }));
  expect(screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Draft" }).value).toBe("Half a thought");
});

test("] collapses Peek holding the focus, and the strip's button takes it; ] there expands it, and the bar's button takes it", async () => {
  const user = userEvent.setup();
  render(
    <CommandRegistryContext value={new CommandRegistry()}>
      <Shortcuts />
      <Peeking keys />
    </CommandRegistryContext>,
  );
  await screen.findByRole("heading");
  screen.getByRole("link", { name: "Open" }).focus();
  await user.keyboard("]");
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Expand Peek" }));
  await user.keyboard("]");
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Collapse Peek" }));
});
