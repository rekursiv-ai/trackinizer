import type { components } from "./generated/schema";

/**
 * What a chat's run says it is doing, as the server reports it: `idle` (waiting for the
 * person's next line), `running` (on a turn), `requires_action` (an agent waits for the
 * person to answer an ask) or `taken_over` (someone opened the CLI in a terminal, which
 * stopped the headless run, and Chat refuses lines until it is reattached). A canvas's
 * partner carries it as `state`; so do an entry of the canvas list and an agent of the
 * console. A server that does not send it is read as saying nothing, so these take any
 * object and read the field themselves.
 */
export type RunState = components["schemas"]["RunState"];

const STATES: readonly string[] = ["idle", "running", "requires_action", "taken_over"] satisfies readonly RunState[];

/** The run state `value` carries, or null when it carries none the web knows. */
export function runStateOf(value: unknown): RunState | null {
  const said = typeof value === "object" && value !== null ? (value as { readonly state?: unknown }).state : undefined;
  return typeof said === "string" && STATES.includes(said) ? (said as RunState) : null;
}

/** Whether the chat waits on the person; null when the server does not say. */
export function waitingOf(value: unknown): boolean | null {
  const state = runStateOf(value);
  return state === null ? null : state === "requires_action";
}

/** Whether the chat's run was taken over in a terminal. */
export function takenOver(value: unknown): boolean {
  return runStateOf(value) === "taken_over";
}
