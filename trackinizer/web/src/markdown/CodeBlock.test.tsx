import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, expect, test } from "vitest";
import { stubClipboard } from "../debug/testing";
import { ToastProvider } from "../ui/toast";
import { Markdown } from "./Markdown";

const KINDS = ["Issue"];

// A real traceback, from `json.loads('{bad')` on Python 3.14, its paths made neutral.
const TRACEBACK = `Traceback (most recent call last):
  File "<string>", line 3, in <module>
    json.loads('{bad')
    ~~~~~~~~~~^^^^^^^^
  File "/usr/lib/python3.14/json/__init__.py", line 352, in loads
    return _default_decoder.decode(s)
           ~~~~~~~~~~~~~~~~~~~~~~~^^^
  File "/usr/lib/python3.14/json/decoder.py", line 345, in decode
    obj, end = self.raw_decode(s, idx=_w(s, 0).end())
               ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.14/json/decoder.py", line 361, in raw_decode
    obj, end = self.scan_once(s, idx)
               ~~~~~~~~~~~~~~^^^^^^^^
json.decoder.JSONDecodeError: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)`;

const PYTHON = '```python\ndef read(path):\n    return path.read_text() or "{}"\n```';

// The highlighter is a chunk of its own; loaded here, its first evaluation is not
// counted against a test's 100 ms.
beforeAll(() => import("./highlight"));

afterEach(cleanup);

function renderMd(source: string): HTMLElement {
  return render(
    <ToastProvider>
      <Markdown source={source} kinds={KINDS} />
    </ToastProvider>,
  ).container;
}

/** The code blocks under `root`, in order. */
function codes(root: HTMLElement): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>("pre code")];
}

/** Wait until the highlighter has coloured `code`. */
async function coloured(code: HTMLElement): Promise<void> {
  await waitFor(() => expect(code.querySelector("[class^=hljs-]")).not.toBeNull(), { interval: 5 });
}

/** The text of each of `code`'s tokens of the highlight.js class `name`. */
function tokens(code: HTMLElement, name: string): string[] {
  return [...code.querySelectorAll(`.hljs-${name}`)].map((token) => token.textContent ?? "");
}

test("a code block keeps its text in monospace, with a Copy button that copies it as written", async () => {
  const copied = stubClipboard();
  const root = renderMd("Run:\n\n```make\nbuild:\n\tcc -o out main.c\n```");
  expect(codes(root).map((code) => code.textContent)).toEqual(["build:\n\tcc -o out main.c\n"]);
  // A click alone: user-event would put its own clipboard in place of the stub.
  fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
  await waitFor(() => expect(copied).toEqual(["build:\n\tcc -o out main.c"]));
  expect(await screen.findByText("Copied the code")).toBeTruthy();
});

test("a code block, which scrolls sideways when wide, takes the keyboard's focus under the name Code (axe scrollable-region-focusable)", () => {
  renderMd(`Run:\n\n\`\`\`\n${"x".repeat(400)}\n\`\`\``);
  const block = screen.getByRole("group", { name: "Code" });
  expect(block.tagName).toBe("PRE");
  expect(block.tabIndex).toBe(0);
  expect(block.textContent).toBe(`${"x".repeat(400)}\n`);
});

test("a tagged block takes highlight.js's colours for its language", async () => {
  const [code] = codes(renderMd(PYTHON));
  await coloured(code!);
  expect(tokens(code!, "keyword")).toEqual(["def", "return", "or"]);
  expect(tokens(code!, "string")).toEqual(['"{}"']);
  expect(code!.textContent).toBe('def read(path):\n    return path.read_text() or "{}"\n');
});

test("an untagged Python traceback is found to be Python and coloured so", async () => {
  const [code] = codes(renderMd(`The run stopped:\n\n\`\`\`\n${TRACEBACK}\n\`\`\``));
  await coloured(code!);
  expect(code!.className).toBe("language-python");
  expect(tokens(code!, "string")).toContain('"/usr/lib/python3.14/json/__init__.py"');
  expect(tokens(code!, "number")).toContain("352");
  expect(code!.textContent).toBe(`${TRACEBACK}\n`);
});

test("prose in an untagged fence, a tag highlight.js does not know, and JSON stay uncoloured", async () => {
  const prose = "If the job fails again, rerun it with a smaller batch and report the result here.";
  const source = [`\`\`\`\n${prose}\n\`\`\``, "```pseudocode\nfor each arm: rerun if loss > 3\n```", '```json\n{"a": [1, true]}\n```', PYTHON].join("\n\n");
  const root = renderMd(source);
  const [plain, unknown, python] = codes(root);
  // Once the tagged block is coloured, the highlighter has run over every block.
  await coloured(python!);
  expect(plain!.innerHTML).toBe(`${prose}\n`);
  expect(unknown!.innerHTML).toBe("for each arm: rerun if loss &gt; 3\n");
  expect(screen.getByRole("group", { name: "JSON" }).querySelector("[class^=hljs-]")).toBeNull();
});
