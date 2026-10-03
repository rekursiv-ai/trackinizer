import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { useMarkdownWarm } from "./Markdown";

// The renderer, in place of react-markdown: notes the texts it renders, which is all warming does with it.
// Each file has the app's modules afresh (src/testSetup.ts), so Markdown starts cold here.
const rendered = vi.hoisted(() => ({ sources: [] as string[] }));
vi.mock("react-markdown", () => ({
  default: ({ children }: { children: string }) => {
    rendered.sources.push(children);
    return null;
  },
}));

afterEach(cleanup);

test("Markdown warms the first time a view asks, after the task that asked, and a later view finds it warm at once", async () => {
  const seen: boolean[] = [];
  function Asker() {
    seen.push(useMarkdownWarm());
    return null;
  }
  render(<Asker />);
  expect([seen, rendered.sources]).toEqual([[false], []]);
  await waitFor(() => expect(seen.at(-1)).toBe(true), { interval: 1 });
  expect(rendered.sources).toHaveLength(4);
  cleanup();
  seen.length = 0;
  render(<Asker />);
  expect(seen).toEqual([true]);
  expect(rendered.sources).toHaveLength(4);
});
