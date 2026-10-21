import { ToolStep } from "../detail/transcript/RecordBody";
import { recordView } from "../detail/transcript/records";
import type { ToolExchange } from "./chatLines";

/**
 * A tool call between Chat's lines, as one collapsed step (the transcript's
 * `ToolStep`): the tool, what it acted on and how it went, then its state: running
 * until a result answers it, then done, or failed when the result says so. Opened, it
 * shows the call's arguments and the result, clipped to its head and tail until asked.
 * A failure starts open.
 */
export function ChatToolCard({ step, kinds }: { readonly step: ToolExchange; readonly kinds: readonly string[] }) {
  const call = recordView(step.call);
  const result = step.result && recordView(step.result);
  const state = result === null ? "running" : result.failed ? "failed" : "done";
  return <article className="chat-line chat-line-tool" data-state={state}>
    <ToolStep call={call} result={result} kinds={kinds} compact aside={<span className="chat-tool-state">{state}</span>} />
  </article>;
}
