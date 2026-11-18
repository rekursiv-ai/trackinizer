import { afterEach, expect, test } from "vitest";
import { languageNameFor, mountEditor } from "./editor";

const hosts: HTMLElement[] = [];

function host(): HTMLElement {
  const element = document.createElement("div");
  document.body.append(element);
  hosts.push(element);
  return element;
}

afterEach(() => {
  for (const element of hosts.splice(0)) element.remove();
});

test("a path picks its grammar, and an unknown one reads as plain text rather than failing", async () => {
  expect(await languageNameFor("src/app.ts")).toBe("typescript");
  // TSX is the TypeScript grammar with JSX switched on, so it reports the same name.
  expect(await languageNameFor("src/App.tsx")).toBe("typescript");
  expect(await languageNameFor("lib/util.py")).toBe("python");
  expect(await languageNameFor("src/main.css")).toBe("css");
  expect(await languageNameFor("package.json")).toBe("json");
  // No grammar fits these, and that must be plain text, not a crash or a wrong parser.
  expect(await languageNameFor("Makefile")).toBeNull();
  expect(await languageNameFor("LICENSE")).toBeNull();
  expect(await languageNameFor("data.parquet")).toBeNull();
});

test("the extension decides, not the path, and it is matched without case", async () => {
  expect(await languageNameFor("deep/nested/dir.ts/thing.py")).toBe("python");
  expect(await languageNameFor("SRC/APP.TS")).toBe("typescript");
});

test("the mounted editor is read-only in both senses, so no caret ever promises a write", () => {
  // `EditorState.readOnly` refuses document transactions; `EditorView.editable` takes the
  // DOM out of contentEditable. The pane's whole asymmetry rests on both being set.
  const parent = host();
  const editor = mountEditor(parent, "a.ts", "const a = 1;\n");
  const content = parent.querySelector(".cm-content")!;
  expect(content.getAttribute("contenteditable")).toBe("false");
  expect(parent.querySelector(".cm-editor")).toBeTruthy();
  editor.destroy();
});

test("the document is the file's text, and showing another file replaces it rather than appending", () => {
  const parent = host();
  const editor = mountEditor(parent, "a.ts", "const a = 1;\n");
  expect(parent.querySelector(".cm-content")!.textContent).toContain("const a = 1;");

  editor.show("b.py", "x = 2\n");
  const text = parent.querySelector(".cm-content")!.textContent!;
  expect(text).toContain("x = 2");
  expect(text).not.toContain("const a = 1;");
  editor.destroy();
});

test("one view serves every file: showing another does not mount a second editor", () => {
  // Remounting per file would drop the find panel and the scroll position handling, which
  // is the reason `show` exists at all rather than a fresh `mountEditor` each time.
  const parent = host();
  const editor = mountEditor(parent, "a.ts", "const a = 1;\n");
  editor.show("b.py", "x = 2\n");
  editor.show("c.css", "a { color: red }\n");
  expect(parent.querySelectorAll(".cm-editor")).toHaveLength(1);
  editor.destroy();
});

test("destroying takes the view out of the DOM, so a closed pane leaves nothing behind", () => {
  const parent = host();
  mountEditor(parent, "a.ts", "const a = 1;\n").destroy();
  expect(parent.querySelector(".cm-editor")).toBeNull();
});

test("an empty file mounts rather than throwing, which a new or truncated file can be", () => {
  const parent = host();
  const editor = mountEditor(parent, "empty.txt", "");
  expect(parent.querySelector(".cm-editor")).toBeTruthy();
  editor.destroy();
});
