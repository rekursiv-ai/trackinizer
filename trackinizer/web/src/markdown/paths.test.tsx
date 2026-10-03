import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { stubClipboard } from "../debug/testing";
import { ToastProvider } from "../ui/toast";
import { Markdown } from "./Markdown";

afterEach(cleanup);

function renderMd(source: string): HTMLElement {
  return render(
    <ToastProvider>
      <Markdown source={source} kinds={["Issue"]} />
    </ToastProvider>,
  ).container;
}

/** The paths `source` shows as Copy path buttons, in order. */
function paths(source: string): string[] {
  const shown = [...renderMd(source).querySelectorAll("button.md-path")].map((button) => button.textContent ?? "");
  cleanup();
  return shown;
}

test.each([
  ["Results: `/opt/scratch/runs/sweeps/nightly-regression-sweep-20260101-0900/attempt-001/`", ["/opt/scratch/runs/sweeps/nightly-regression-sweep-20260101-0900/attempt-001/"]],
  ["Results: /opt/scratch/runs/sweeps/attempt-001", ["/opt/scratch/runs/sweeps/attempt-001"]],
  ["see ~/src/README.md, then (/opt/a/b) and \"/opt/c/d\".", ["~/src/README.md", "/opt/a/b", "/opt/c/d"]],
  ["wrote /opt/a/b.", ["/opt/a/b"]],
  ["wrote /opt/a/b/.", ["/opt/a/b/"]],
  // Not paths: one segment, ratios, money, and/or, a URL's path, a host's path, a path in a longer code span or a block.
  ["/goal and /opt and ~/src", []],
  ["5/10 new iterations; $26.16/$150 spend; and/or 10/02/2026", []],
  ["https://example.com/opt/a/b and <https://example.com/x/y>", []],
  ["host:/opt/scratch/artifacts", []],
  ["run `cat /opt/a/b`", []],
  ["```\n/opt/a/b\n```", []],
  ["[the run](https://example.com) /opt/a/b", ["/opt/a/b"]],
])("%s", (source, want) => {
  expect(paths(source)).toEqual(want);
});

test("a path is a button named for it that copies it, and says so", async () => {
  const copied = stubClipboard();
  renderMd("Results: /opt/scratch/runs/attempt-001/ and `~/src/x`");
  // By label, not by role: a role query walks the accessibility tree, which cost this test about 50 ms of its 100.
  const button = screen.getByLabelText("Copy path /opt/scratch/runs/attempt-001/");
  expect([button.tagName, button.getAttribute("type"), button.querySelector("code")?.textContent]).toEqual([
    "BUTTON",
    "button",
    "/opt/scratch/runs/attempt-001/",
  ]);
  expect(screen.getByLabelText("Copy path ~/src/x").tagName).toBe("BUTTON");
  // A click alone: user-event would put its own clipboard in place of the stub.
  fireEvent.click(button);
  await waitFor(() => expect(copied).toEqual(["/opt/scratch/runs/attempt-001/"]));
  expect(await screen.findByText("Copied the path")).toBeTruthy();
});
