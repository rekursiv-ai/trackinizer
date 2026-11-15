import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { type Sent, stubFetch } from "../api/testing";
import { createQueryClient } from "../app/queryClient";
import { CodePane } from "./CodePane";
import type { CodeScope } from "./scope";

const RECORD = "11111111-1111-1111-1111-111111111111";
const OWNER = "me@acme.co";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** A snapshot answer for `paths`, and the text of each file asked for. */
function serve({
  paths = [],
  text = "",
  missing = [],
}: { readonly paths?: readonly string[]; readonly text?: string; readonly missing?: readonly string[] } = {}) {
  return stubFetch((request) => {
    const url = new URL(request.url);
    if (url.pathname.endsWith("/snapshot") && request.method === "GET") {
      if (paths.length === 0) return new Response("null", { status: 404 });
      return Response.json({
        record_id: RECORD,
        parent_record_id: null,
        private: true,
        file_count: paths.length,
        total_bytes: 0,
        partial: "",
        updated_at: "2026-11-15T00:00:00+00:00",
        files: paths.map((path) => ({ path, sha256: "a".repeat(64), size: text.length })),
      });
    }
    if (url.pathname.endsWith("/file")) {
      return Response.json({ path: url.searchParams.get("path"), sha256: "a".repeat(64), text });
    }
    if (url.pathname.endsWith("/blobs/missing")) return Response.json({ missing: [...missing] });
    if (url.pathname.endsWith("/api/code/blobs")) return Response.json({ stored: missing.length });
    if (url.pathname.endsWith("/snapshot")) {
      return Response.json({
        record_id: RECORD,
        parent_record_id: null,
        private: true,
        file_count: 1,
        total_bytes: 0,
        partial: "",
        updated_at: "2026-11-15T00:00:00+00:00",
      });
    }
    return Response.json({});
  });
}

const SESSION: CodeScope = { record: RECORD, kind: "session" };

function draw({ scope = SESSION, owner = OWNER }: { readonly scope?: CodeScope | null; readonly owner?: string | null } = {}) {
  return render(
    <QueryClientProvider client={createQueryClient(() => {})}>
      <CodePane scope={scope} owner={owner} />
    </QueryClientProvider>,
  );
}

test("with no session and no workstream there is nowhere to file code, and the import is refused rather than offered", async () => {
  serve();
  draw({ scope: null });
  expect(await screen.findByText("Nowhere to file code")).toBeTruthy();
  // The button must not invite a click that cannot land.
  expect(screen.getByRole("button", { name: /Import folder/ })).toHaveProperty("disabled", true);
});

test("a session with no snapshot says so and names both ways in, since a bare pane reads as broken", async () => {
  serve();
  draw();
  expect(await screen.findByText("No code for this session")).toBeTruthy();
  expect(screen.getByText(/Drop a folder anywhere on this pane, or use Import folder/)).toBeTruthy();
  expect(screen.getByRole("button", { name: /Import folder/ })).toHaveProperty("disabled", false);
});

test("the pane says it is read-only, which is the asymmetry it exists to hold", async () => {
  // The reader supplies the tree and the agent writes inside it. A caret here would
  // promise a write the reader does not own, so there must be no editable control at all.
  serve({ paths: ["src/app.ts"], text: "export const a = 1;\n" });
  const { container } = draw();
  expect(await screen.findByText("Read-only")).toBeTruthy();
  expect(container.querySelector("textarea")).toBeNull();
  expect(container.querySelector("input:not([type=file])")).toBeNull();
  expect(container.querySelector("[contenteditable]")).toBeNull();
});

test("the snapshot's paths draw as a tree, and picking a file reads it and numbers its lines", async () => {
  serve({ paths: ["README.md", "src/app.ts"], text: "const a = 1;\nconst b = 2;\n" });
  draw();
  // Folders are their own nodes, named with the trailing slash `fileTree` gives them;
  // the leaf carries the file's name alone.
  expect(await screen.findByText("src/")).toBeTruthy();
  expect(screen.getByText("2 files")).toBeTruthy();
  const leaf = screen.getByRole("button", { name: "app.ts" });
  await userEvent.click(leaf);

  // The file's own path heads it as a breadcrumb, so a scrolled file still says which it is,
  // and every line is numbered.
  const crumbs = await screen.findByText("app.ts", { selector: ".cp-crumb-last" });
  expect(crumbs.closest(".cp-crumbs")!.textContent).toContain("src");
  expect(screen.getByText("2 lines")).toBeTruthy();
  await waitFor(() => expect(screen.getByText("1", { selector: ".cp-n" })).toBeTruthy());
  expect(screen.getByText("2", { selector: ".cp-n" })).toBeTruthy();
});

test("a file is read by path from the session's own record, not from a guess at the tree", async () => {
  const sent: Sent[] = serve({ paths: ["src/app.ts"], text: "ok\n" });
  draw();
  await userEvent.click(await screen.findByRole("button", { name: "app.ts" }));
  await waitFor(() => expect(sent.some((each) => each.path.endsWith("/file"))).toBe(true));
  const read = sent.find((each) => each.path.endsWith("/file"))!;
  expect(read.path).toBe(`/api/code/records/${RECORD}/file`);
  expect(read.query).toContain("path=src%2Fapp.ts");
});

test("a snapshot the uploader cut carries its note forward, so a thin tree is explained", async () => {
  stubFetch((request) => {
    if (new URL(request.url).pathname.endsWith("/snapshot")) {
      return Response.json({
        record_id: RECORD,
        parent_record_id: null,
        private: true,
        file_count: 1,
        total_bytes: 3,
        partial: "3 files past the 20000-file limit were left out",
        updated_at: "2026-11-15T00:00:00+00:00",
        files: [{ path: "a.ts", sha256: "a".repeat(64), size: 3 }],
      });
    }
    return Response.json({});
  });
  draw();
  expect(await screen.findByText("3 files past the 20000-file limit were left out")).toBeTruthy();
});

test("a failed snapshot read is reported with a retry, not shown as an empty tree", async () => {
  // An empty state here would claim the session has no code when the truth is unknown.
  stubFetch(() => new Response("nope", { status: 500 }));
  draw();
  expect(await screen.findByRole("button", { name: /Retry/ })).toBeTruthy();
  expect(screen.queryByText("No code for this session")).toBeNull();
});
