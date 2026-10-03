import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, expect, type Mock, test, vi } from "vitest";
import { listCommands, type ListHandlers } from "./list";
import {
  type Command,
  CommandRegistry,
  CommandRegistryContext,
  keyCaps,
  Shortcuts,
  useCommandList,
  useCommands,
} from "./registry";

afterEach(cleanup);

function Mount({ commands }: { commands: readonly Command[] }) {
  useCommands(commands);
  return null;
}

function Titles() {
  return <output>{useCommandList().map((command) => command.title).join(",")}</output>;
}

function setup(children: ReactNode) {
  render(
    <CommandRegistryContext value={new CommandRegistry()}>
      <Shortcuts />
      {children}
    </CommandRegistryContext>,
  );
  return { user: userEvent.setup() };
}

function handlers(): { [K in keyof ListHandlers]: Mock<() => void> } {
  return { next: vi.fn(), previous: vi.fn(), open: vi.fn(), close: vi.fn(), peek: vi.fn() };
}

test("list keys run their handlers, and not while typing in a field", async () => {
  const list = handlers();
  const { user } = setup(
    <>
      <Mount commands={listCommands(list)} />
      <input aria-label="Title" />
    </>,
  );
  await user.keyboard("jjk{Enter}{Escape} ");
  expect([list.next, list.previous, list.open, list.close, list.peek].map((f) => f.mock.calls.length)).toEqual([
    2, 1, 1, 1, 1,
  ]);

  await user.type(screen.getByLabelText("Title"), "jk ");
  expect((screen.getByLabelText("Title") as HTMLInputElement).value).toBe("jk ");
  expect(list.next).toHaveBeenCalledTimes(2);
});

test("a digit typed in a menu's search box types the digit (COLD-15)", async () => {
  const pick = vi.fn();
  const { user } = setup(
    <>
      <Mount commands={[{ id: "pick.1", title: "Pick first", keys: ["1"], run: pick }]} />
      <div role="menu">
        <input aria-label="Filter" />
        <button type="button">Item</button>
      </div>
    </>,
  );
  await user.type(screen.getByLabelText("Filter"), "12");
  expect((screen.getByLabelText("Filter") as HTMLInputElement).value).toBe("12");
  screen.getByText("Item").focus();
  await user.keyboard("1");
  expect(pick).not.toHaveBeenCalled();
});

test("a listbox the keyboard is in keeps the keys it takes, and the rest reach the shortcuts", async () => {
  const [next, focus] = [vi.fn(), vi.fn()];
  const { user } = setup(
    <>
      <Mount
        commands={[
          { id: "next", title: "Next", keys: ["j"], run: next },
          { id: "focus", title: "Focus", keys: ["."], run: focus },
        ]}
      />
      <ul role="listbox" aria-label="Roots" tabIndex={0} onKeyDown={(event) => event.key === "j" && event.preventDefault()} />
    </>,
  );
  screen.getByRole("listbox").focus();
  await user.keyboard("j.");
  expect(next).not.toHaveBeenCalled();
  expect(focus).toHaveBeenCalledTimes(1);
});

test("a global command fires from inside a field; held keys repeat only when allowed", async () => {
  const toggle = vi.fn();
  const list = handlers();
  const { user } = setup(
    <>
      <Mount commands={[{ id: "palette", title: "Palette", keys: ["$mod+k"], global: true, run: toggle }]} />
      <Mount commands={listCommands(list)} />
      <input aria-label="Query" />
    </>,
  );
  await user.click(screen.getByLabelText("Query"));
  await user.keyboard("{Control>}k{/Control}");
  expect(toggle).toHaveBeenCalledTimes(1);
  expect((screen.getByLabelText("Query") as HTMLInputElement).value).toBe("");

  screen.getByLabelText("Query").blur();
  await user.keyboard("{Control>}{k>3/}{/Control}{j>3/}");
  expect(toggle).toHaveBeenCalledTimes(2);
  expect(list.next).toHaveBeenCalledTimes(3);
});

test("Enter on a focused button presses the button, not the list", async () => {
  const list = handlers();
  const press = vi.fn();
  const { user } = setup(
    <>
      <Mount commands={listCommands(list)} />
      <button type="button" onClick={press}>
        Save
      </button>
    </>,
  );
  screen.getByText("Save").focus();
  await user.keyboard("{Enter}j");
  expect(press).toHaveBeenCalledTimes(1);
  expect(list.open).not.toHaveBeenCalled();
  expect(list.next).toHaveBeenCalledTimes(1);
});

test("the latest mounted command wins an id and its key; unmounting it restores the other", async () => {
  const first: Command = { id: "a", title: "First", keys: ["x"], run: vi.fn() };
  const second: Command = { id: "a", title: "Second", keys: ["x"], run: vi.fn() };
  const tree = (both: boolean) => (
    <>
      <Mount commands={[first]} />
      {both && <Mount commands={[second]} />}
      <Titles />
    </>
  );
  const registry = new CommandRegistry();
  const wrap = (children: ReactNode) => (
    <CommandRegistryContext value={registry}>
      <Shortcuts />
      {children}
    </CommandRegistryContext>
  );
  const user = userEvent.setup();
  const view = render(wrap(tree(true)));
  expect(screen.getByRole("status").textContent).toBe("Second");
  await user.keyboard("x");
  expect([first.run, second.run].map((run) => vi.mocked(run).mock.calls.length)).toEqual([0, 1]);

  view.rerender(wrap(tree(false)));
  expect(screen.getByRole("status").textContent).toBe("First");
  await user.keyboard("x");
  expect([first.run, second.run].map((run) => vi.mocked(run).mock.calls.length)).toEqual([1, 1]);
});

test("a key bound by a global and a later local command runs only the later one (WEB-11)", async () => {
  const global: Command = { id: "palette", title: "Palette", keys: ["$mod+k"], global: true, run: vi.fn() };
  const local: Command = { id: "list.k", title: "Local", keys: ["$mod+k"], run: vi.fn() };
  const { user } = setup(
    <>
      <Mount commands={[global]} />
      <Mount commands={[local]} />
    </>,
  );
  await user.keyboard("{Control>}k{/Control}");
  expect([global.run, local.run].map((run) => vi.mocked(run).mock.calls.length)).toEqual([0, 1]);
});

test("a new handler closure does not re-register; a new title does", () => {
  const registry = new CommandRegistry();
  const added = vi.spyOn(registry, "add");
  const calls: string[] = [];
  function Host({ title, tag }: { title: string; tag: string }) {
    useCommands([{ id: "c", title, run: () => calls.push(tag) }]);
    return null;
  }
  const view = render(
    <CommandRegistryContext value={registry}>
      <Host title="One" tag="first" />
    </CommandRegistryContext>,
  );
  view.rerender(
    <CommandRegistryContext value={registry}>
      <Host title="One" tag="second" />
    </CommandRegistryContext>,
  );
  expect(added).toHaveBeenCalledTimes(1);
  act(() => registry.getCommands()[0]!.run());
  expect(calls).toEqual(["second"]);
  view.rerender(
    <CommandRegistryContext value={registry}>
      <Host title="Two" tag="second" />
    </CommandRegistryContext>,
  );
  expect(added).toHaveBeenCalledTimes(2);
  expect(registry.getCommands().map((command) => command.title)).toEqual(["Two"]);
});

test("⌘↵ reads as this platform's: Ctrl+Enter off a Mac, ⌘↵ on one, and no caption spells it itself (WEB-19)", () => {
  expect(keyCaps("$mod+Enter")).toEqual(["Ctrl+Enter"]);
  vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
  expect(keyCaps("$mod+Enter")).toEqual(["⌘↵"]);
  vi.restoreAllMocks();
  const src = join(import.meta.dirname, "..");
  const spelled = readdirSync(src, { recursive: true, encoding: "utf8" })
    .filter((path) => path.endsWith(".tsx") && !path.endsWith(".test.tsx"))
    .filter((path) => /<kbd>⌘/.test(readFileSync(join(src, path), "utf8")));
  expect(spelled).toEqual([]);
});
