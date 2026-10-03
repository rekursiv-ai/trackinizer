import { act, cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ToastProvider } from "../ui/toast";
import { Markdown } from "./Markdown";
import { markdownParts, ProgressiveMarkdown } from "./ProgressiveMarkdown";

const KINDS = ["Issue", "Belief"];

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

/** `count` sections, each a `## Section k` heading and about 1,000 characters of text. */
function sections(count: number): string {
  return Array.from({ length: count }, (_, k) => `## Section ${k + 1}\n\n${"Measured the list route again. ".repeat(33)}\n`).join("\n");
}

/** The headings rendered under `root`, in order. */
function headings(root: Element): string[] {
  return [...root.querySelectorAll("h4")].map((heading) => heading.textContent ?? "");
}

test("a source of up to 12K characters is one part", () => {
  const source = sections(11);
  expect(source.length).toBeLessThanOrEqual(12_000);
  expect(markdownParts(source)).toEqual([source]);
});

test("a longer source splits before headings into parts of about 6K characters, losing nothing", () => {
  const source = sections(15);
  const parts = markdownParts(source);
  expect(parts.length).toBe(3);
  expect(parts.slice(1).every((part) => part.startsWith("## Section "))).toBe(true);
  expect(parts.slice(0, -1).every((part) => part.length >= 6_000 && part.length < 7_200)).toBe(true);
  expect(parts.join("\n")).toBe(source);
});

test("a heading inside a code fence is never a split", () => {
  const code = Array.from({ length: 400 }, (_, k) => `# step ${k}\nmake build`).join("\n");
  const source = `${sections(7)}\n\`\`\`bash\n${code}\n\`\`\`\n\n${sections(2)}`;
  const parts = markdownParts(source);
  expect(parts.map((part) => part.split("\n")[0])).toEqual(["## Section 1", "## Section 7", "## Section 1"]);
  expect(parts[1]).toContain("# step 399\nmake build\n```");
});

test("a source with reference-style links or footnotes stays one part, so each finds its definition", () => {
  for (const tail of ["See [the plan][p].\n\n[p]: https://example.com/p", "A claim.[^1]\n\n[^1]: The source."]) {
    const source = `${sections(15)}\n${tail}`;
    expect(markdownParts(source)).toEqual([source]);
  }
});

test("the first part renders at once, and each later part in a task of its own", async () => {
  vi.useFakeTimers();
  const source = sections(15);
  const { container } = render(<ProgressiveMarkdown source={source} kinds={KINDS} />);
  expect(container.querySelectorAll(".md-part")).toHaveLength(1);
  expect(headings(container)).toEqual(["Section 1", "Section 2", "Section 3", "Section 4", "Section 5", "Section 6"]);
  await act(() => vi.advanceTimersToNextTimerAsync());
  expect(container.querySelectorAll(".md-part")).toHaveLength(2);
  await act(() => vi.advanceTimersToNextTimerAsync());
  expect(container.querySelectorAll(".md-part")).toHaveLength(3);
  const whole = render(<Markdown source={source} kinds={KINDS} />).container;
  expect(headings(container)).toEqual(headings(whole));
  expect(container.firstElementChild!.className).toBe("md");
});

test("a short source renders as one Markdown, with no parts", () => {
  const { container } = render(<ProgressiveMarkdown source={sections(2)} kinds={KINDS} />);
  expect(container.querySelector(".md-part")).toBeNull();
  expect(headings(container)).toEqual(["Section 1", "Section 2"]);
});

test("a long description that is all JSON is one part, shown as one JSON view", () => {
  const source = JSON.stringify({ notes: Array.from({ length: 30 }, (_, k) => `## Note ${k}: ${"measured again. ".repeat(30)}`) }, null, 2);
  expect(source.length).toBeGreaterThan(12_000);
  expect(markdownParts(source)).toEqual([source]);
  const { container } = render(
    <ToastProvider>
      <ProgressiveMarkdown source={source} kinds={KINDS} />
    </ToastProvider>,
  );
  expect(container.querySelectorAll(".jv")).toHaveLength(1);
  expect(container.querySelectorAll(".jv .jv-string")).toHaveLength(30);
});
