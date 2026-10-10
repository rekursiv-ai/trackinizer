import { type APIRequestContext, expect, type Page } from "@playwright/test";

/** What the server's initial canvas holds: the page, and Chat at the side. */
export async function resetCanvas(request: APIRequestContext): Promise<void> {
  const read = async <T>(response: { ok(): boolean; text(): Promise<string>; json(): Promise<unknown> }) => {
    expect(response.ok(), await response.text()).toBe(true);
    return (await response.json()) as T;
  };
  type State = { id: string; revision: number; visuals: { id: string; type: string }[] };
  let state = await read<State>(await request.post("/api/workspaces"));
  const apply = async (operation: object) => {
    state = await read<State>(await request.post(`/api/workspaces/${state.id}/operations`, {
      headers: { "Idempotency-Key": crypto.randomUUID() }, data: { revision: state.revision, operation },
    }));
  };
  for (const visual of state.visuals.filter((held) => held.type !== "trax.browse")) {
    await apply({ kind: "hide", instance_id: visual.id });
  }
  await apply({ kind: "show", visual_type: "trax.chat", placement: "side" });
}

/** Keep the page's event sources where the script can reach them. */
export function exposeEventSources() {
  const Original = window.EventSource;
  const held: EventSource[] = [];
  Object.assign(window, { heldEventSources: held });
  window.EventSource = class extends Original {
    constructor(url: string | URL, init?: EventSourceInit) {
      super(url, init);
      held.push(this);
    }
  };
}

/** Hand the page's workspace stream a `navigate` frame to `route`, stamped now. */
export async function pushNavigate(page: Page, route: string): Promise<void> {
  await page.evaluate((route) => {
    const sources = (window as unknown as { heldEventSources: EventSource[] }).heldEventSources;
    const stream = sources.find((source) => source.url.includes("/events"))!;
    stream.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "navigate", route, t: Date.now() }) }));
  }, route);
}
