import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";

/** One thing the server provides to run commands on, as its deployment notes it. */
export type ComputeNote = components["schemas"]["ComputeNote"];

/** What the server provides to run commands on, in the deployment's order; none when it notes none. */
export async function listCompute({ signal }: CallOptions = {}): Promise<readonly ComputeNote[]> {
  const { notes } = await send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/compute", { signal }));
  return notes;
}
