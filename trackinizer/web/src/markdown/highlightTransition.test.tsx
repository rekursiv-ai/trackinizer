import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ToastProvider } from "../ui/toast";
import { Markdown } from "./Markdown";

// React's own startTransition, watched: the highlighter's arrival recolours what
// drew before it in a background render React can interrupt.
const transitions = vi.hoisted(() => ({ count: 0 }));
vi.mock("react", async (original) => {
  const react = await original<typeof import("react")>();
  return {
    ...react,
    startTransition: (scope: () => void) => {
      transitions.count += 1;
      react.startTransition(scope);
    },
  };
});
// The highlighter, in place of its chunk: marks each tree it runs over.
vi.mock("./highlight", () => ({
  prepare: async () => {},
  rehypeHighlightCode: () => (tree: { children: unknown[] }) => {
    tree.children.push({ type: "element", tagName: "hr", properties: { className: ["highlighted"] }, children: [] });
  },
}));

afterEach(cleanup);

test("code drawn before the highlighter arrived takes its colours in a background render", async () => {
  const { container } = render(
    <ToastProvider>
      <Markdown source={"```py\nx = 1\n```"} kinds={[]} />
    </ToastProvider>,
  );
  expect([container.querySelector(".highlighted"), transitions.count]).toEqual([null, 0]);
  await vi.waitFor(() => expect(container.querySelector(".highlighted")).not.toBeNull(), { interval: 5 });
  expect(transitions.count).toBe(1);
});
