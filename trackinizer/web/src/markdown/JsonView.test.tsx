import { readFileSync } from "node:fs";
import { join } from "node:path";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test } from "vitest";
import { stubClipboard } from "../debug/testing";
import { ToastProvider } from "../ui/toast";
import { Markdown } from "./Markdown";

// A synthetic job result, as an Artifact's description holds one: one line of
// JSON with eight metrics.
const RESULT = readFileSync(join(import.meta.dirname, "testdata/job_result.json"), "utf8");
const KINDS = ["Issue", "Experiment"];

afterEach(cleanup);

function renderMd(source: string): HTMLElement {
  return render(
    <ToastProvider>
      <Markdown source={source} kinds={KINDS} />
    </ToastProvider>,
  ).container;
}

/** Each line of `view`'s JSON, as its text reads. */
function lines(view: HTMLElement): string[] {
  return [...view.querySelectorAll(".jv-line")].map((line) => line.textContent ?? "");
}

/** Each row of `table`, as the text of its cells. */
function rows(table: HTMLElement): string[][] {
  return [...table.querySelectorAll("tr")].map((row) => [...row.cells].map((cell) => cell.textContent ?? ""));
}

test("a description that is one line of JSON shows as a JSON view, one entry a line", () => {
  renderMd(RESULT);
  const view = screen.getByRole("group", { name: "JSON" });
  expect(lines(view)).toEqual([
    "{",
    '"classification": "MEASURED",',
    '"error": "",',
    // A UUID links as it does in text, by its first eight characters.
    '"experiment_id": "01434be3",',
    '"job_id": "sweep_lr_012_arm_7_d77a0cb424b63937ea0cf04256be1d97_7f6f0e",',
    '"payload": {',
    "},",
    '"result_filename": "metrics.json",',
    '"result_sha256": "5ed03a3596d96bd38451a690761d624852a968fc95c236051e4ede6fd90a5a43",',
    '"schema_version": 1,',
    '"submission_category": ""',
    "}",
  ]);
  expect(within(view).getByRole("link", { name: "01434be3" }).getAttribute("href")).toBe(
    "#/lookup/01434be3-ebf8-4ed6-9c1b-bf9c0735c9c9",
  );
});

test("an object of numbers is a table of key and value, each key under its prefix, numbers as written", () => {
  renderMd(RESULT);
  const table = within(screen.getByRole("group", { name: "JSON" })).getByRole("table");
  expect(rows(table)).toEqual([
    ["eval/"],
    ["eval_sec", "25.768875706878884"],
    ["peak_memory_gb", "18.73461248"],
    ["seed", "7.0"],
    ["steps", "2048.0"],
    ["tokens_per_step", "131072.0"],
    ["total_loss", "2.6068501220663047"],
    ["train_sec", "600.0"],
    ["val_bpb", "1.07744145321265"],
  ]);
  // Tabular figures (`.num`), right-aligned by the view's style.
  expect([...table.querySelectorAll("td")].every((cell) => cell.classList.contains("num"))).toBe(true);
  cleanup();

  renderMd('{"loss": 2.5, "eval/acc": 0.5, "train/lr": 1e-3, "eval/loss": 2.7}');
  expect(rows(screen.getByRole("table"))).toEqual([
    ["loss", "2.5"],
    ["eval/"],
    ["acc", "0.5"],
    ["loss", "2.7"],
    ["train/"],
    ["lr", "1e-3"],
  ]);
  cleanup();

  // One value that is not a number, and it is JSON again.
  renderMd('{"loss": 2.5, "note": "x"}');
  expect(screen.queryByRole("table")).toBeNull();
  expect(lines(screen.getByRole("group", { name: "JSON" }))).toEqual(["{", '"loss": 2.5,', '"note": "x"', "}"]);
});

test("keys, strings, numbers, and true, false and null each take their colour", () => {
  renderMd('[{"ok": true, "off": false, "none": null, "n": -3.5e2, "s": "x"}]');
  const view = screen.getByRole("group", { name: "JSON" });
  const tokens = (name: string) => [...view.querySelectorAll(`.jv-${name}`)].map((token) => token.textContent);
  expect(tokens("key")).toEqual(['"ok"', '"off"', '"none"', '"n"', '"s"']);
  expect(tokens("literal")).toEqual(["true", "false", "null"]);
  expect(tokens("number")).toEqual(["-3.5e2"]);
  expect(tokens("string")).toEqual(['"x"']);
});

test("an object or array folds and unfolds from the keyboard; one over 50 entries or three levels down starts folded", async () => {
  const big = Array.from({ length: 60 }, (_, k) => k);
  renderMd(JSON.stringify({ small: [1, 2], big, deep: { a: { b: { c: 1 } } } }));
  const toggle = (name: string) => screen.getByRole("button", { name });
  expect(["small", "big", "deep", "a", "b"].map((name) => toggle(name).getAttribute("aria-expanded"))).toEqual([
    "true",
    "false",
    "true",
    "true",
    "false",
  ]);
  const view = screen.getByRole("group", { name: "JSON" });
  expect(lines(view)).toContain('"big": […], 60 items');
  expect(lines(view)).toContain('"b": {…} 1 key');

  const user = userEvent.setup();
  toggle("big").focus();
  await user.keyboard("{Enter}");
  expect(toggle("big").getAttribute("aria-expanded")).toBe("true");
  expect(lines(view)).toContain("59");
  await user.keyboard(" ");
  expect(toggle("big").getAttribute("aria-expanded")).toBe("false");
  expect(lines(view)).not.toContain("59");
});

test("Copy copies the JSON as it was written", async () => {
  const copied = stubClipboard();
  renderMd(`\n${RESULT}`);
  // A click alone: user-event would put its own clipboard in place of the stub.
  fireEvent.click(screen.getByRole("button", { name: "Copy JSON" }));
  await waitFor(() => expect(copied).toEqual([RESULT.trim()]));
  expect(await screen.findByText("Copied the JSON")).toBeTruthy();
});

test("a ref in a JSON string links as it does in text", () => {
  renderMd('{"note": "Rerun after Issue#4 lands", "ref": "Experiment#12"}');
  const view = screen.getByRole("group", { name: "JSON" });
  expect(within(view).getAllByRole("link").map((link) => link.getAttribute("href"))).toEqual(["#/ref/Issue/4", "#/ref/Experiment/12"]);
  expect(lines(view)[1]).toBe('"note": "Rerun after Issue#4 lands",');
});

test("markup and script URLs in a JSON string stay text", () => {
  const root = renderMd('{"html": "<img src=x onerror=alert(1)>", "url": "javascript:alert(1)", "<b>": 1, "n": "x"}');
  expect(root.querySelector("img, b, script")).toBeNull();
  expect(root.querySelector("a")).toBeNull();
  expect(lines(screen.getByRole("group", { name: "JSON" }))[1]).toBe('"html": "<img src=x onerror=alert(1)>",');
});

test("a fenced block tagged json or jsonc, or untagged, holding JSON shows as a JSON view; the rest stays code", async () => {
  const copied = stubClipboard();
  const source = [
    "Before.",
    "```json",
    '{"a": true}',
    "```",
    "```jsonc",
    "[1, 2]",
    "```",
    "```",
    '{"untagged": true}',
    "```",
    "```json",
    '{"broken": ',
    "```",
    "```python",
    "[1, 2]",
    "```",
    "After.",
  ].join("\n");
  const root = renderMd(source);
  const views = screen.getAllByRole("group", { name: "JSON" });
  expect(views.map((view) => lines(view).join(" "))).toEqual(['{ "a": true }', "[ 1, 2 ]", '{ "untagged": true }']);
  expect([...root.querySelectorAll("pre code")].map((code) => code.textContent)).toEqual(['{"broken": \n', "[1, 2]\n"]);
  expect([...root.querySelectorAll("p")].map((paragraph) => paragraph.textContent)).toEqual(["Before.", "After."]);
  fireEvent.click(screen.getAllByRole("button", { name: "Copy JSON" })[0]!);
  await waitFor(() => expect(copied).toEqual(['{"a": true}']));
});

test("text that is not all JSON renders as Markdown, as before", () => {
  for (const source of ['[the plan](https://example.com/plan) and {"a": 1}', '{"a": 1} and more', "[1, 2] [3]"]) {
    const root = renderMd(source);
    expect(screen.queryByRole("group", { name: "JSON" })).toBeNull();
    expect(root.querySelector("p")?.textContent).toBe(source.replace("[the plan](https://example.com/plan)", "the plan"));
    cleanup();
  }
});
