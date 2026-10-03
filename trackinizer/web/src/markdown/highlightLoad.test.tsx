import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ToastProvider } from "../ui/toast";
import { Markdown } from "./Markdown";

// The highlighter, in place of its chunk: counts its loads, and marks each tree it runs over.
const loads = vi.hoisted(() => ({ count: 0 }));
vi.mock("./highlight", () => {
  loads.count += 1;
  return {
    rehypeHighlightCode: () => (tree: { children: unknown[] }) => {
      tree.children.push({ type: "element", tagName: "hr", properties: { className: ["highlighted"] }, children: [] });
    },
  };
});

afterEach(cleanup);

function renderMd(source: string): HTMLElement {
  return render(
    <ToastProvider>
      <Markdown source={source} kinds={[]} />
    </ToastProvider>,
  ).container;
}

test("the highlighter loads only for Markdown with a code fence, once, and then applies at once", async () => {
  renderMd("Some `inline code` and\n\n    an indented line.");
  renderMd('{"a": "```"}');
  await new Promise((resolve) => setTimeout(resolve));
  expect(loads.count).toBe(0);

  const first = renderMd("```sh\nls\n```");
  expect(first.querySelector(".highlighted")).toBeNull();
  await vi.waitFor(() => expect(first.querySelector(".highlighted")).not.toBeNull(), { interval: 5 });
  // A later block needs no wait.
  expect(renderMd("~~~\nls\n~~~").querySelector(".highlighted")).not.toBeNull();
  expect(loads.count).toBe(1);
});
