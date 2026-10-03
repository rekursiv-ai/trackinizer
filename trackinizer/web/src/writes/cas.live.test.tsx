// The write layer against a real server, with the Python client as a second
// user. Skipped unless TRACKINIZER_LIVE_URL names a server started with
// `python -m trackinizer.server --ephemeral --no-auth --port <port>`, since
// it writes to that server's database:
//   TRACKINIZER_LIVE_URL=http://127.0.0.1:<port> ./npm test -- src/writes/cas.live.test.tsx
import { execFileSync } from "node:child_process";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { editableFields } from "../api/fields";
import { keyed } from "../api/idempotency";
import { createInquiry } from "../api/inquiries";
import { detailQueries } from "../detail/queries";
import { ToastProvider } from "../ui/toast";
import { fieldEdit } from "./edits";
import { useWrite } from "./useWrite";
import { WriteStatus } from "./WriteStatus";

const LIVE = process.env.TRACKINIZER_LIVE_URL;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test.skipIf(!LIVE)(
  "a status change after another user's gets 409, then the dialog, then lands on Save mine",
  async () => {
    const statuses = proxyTo(LIVE!);
    const { id } = await createInquiry("Issue", keyed({ title: "D1 live compare-and-set" }));
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ToastProvider>
          <StatusControl id={id} />
        </ToastProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByRole("status", { name: "Status" }).textContent).toBe("active"));

    // From the repository root, where uv finds the project that has the client.
    const root = execFileSync("git", ["rev-parse", "--show-toplevel"], { encoding: "utf8" }).trim();
    execFileSync("uv", ["--quiet", "run", "--frozen", "python", "-c", SECOND_USER, LIVE!, id], { cwd: root });
    fireEvent.click(screen.getByRole("button", { name: "Complete" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Status changed" }, { timeout: 5000 });
    expect(statuses.filter(({ method }) => method === "PUT").map(({ status }) => status)).toEqual([409]);
    expect([...dialog.querySelectorAll("dt, dd")].map((part) => part.textContent)).toEqual([
      "Now, by josh",
      "abandoned",
      "Yours",
      "complete",
    ]);

    fireEvent.click(within(dialog).getByRole("button", { name: "Save mine" }));
    await screen.findByText("Status set to complete", {}, { timeout: 5000 });
    expect(statuses.filter(({ method }) => method === "PUT").map(({ status }) => status)).toEqual([409, 200]);
    const stored = await (await fetch(`${location.origin}/api/inquiries/${id}`)).json();
    expect(stored.status).toBe("complete");
  },
  60_000,
);

/** Josh abandons the Issue with the Python client, by compare-and-set from active. */
const SECOND_USER = `
import sys, uuid
from trackinizer.client.client import Client
with Client(sys.argv[1]) as client:
    client.transition_status(uuid.UUID(sys.argv[2]), expected_from="active", to="abandoned", actor="josh")
`;

function StatusControl({ id }: { id: string }) {
  const query = useQuery(detailQueries.detail(id));
  const writer = useWrite();
  const self = query.data?.self;
  return (
    <div>
      <output aria-label="Status">{self?.status}</output>
      <button
        type="button"
        onClick={() =>
          self &&
          void writer.run(
            fieldEdit({ id, field: "status", route: editableFields("Issue").status!, label: "Status", from: self.status, to: "complete" }),
          )
        }
      >
        Complete
      </button>
      <WriteStatus state={writer.state} />
    </div>
  );
}

/** Send the app's requests to `origin`; returns each one's method and status, in order. */
function proxyTo(origin: string): { method: string; status: number }[] {
  const answered: { method: string; status: number }[] = [];
  const realFetch = globalThis.fetch;
  vi.stubGlobal("fetch", async (input: Request | string) => {
    const request = new Request(input);
    const url = new URL(request.url);
    const body = request.method === "GET" ? undefined : await request.text();
    const response = await realFetch(new URL(url.pathname + url.search, origin), {
      method: request.method,
      headers: request.headers,
      body,
    });
    answered.push({ method: request.method, status: response.status });
    return response;
  });
  return answered;
}
