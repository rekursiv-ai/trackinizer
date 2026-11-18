import { answerCapture } from "../api/workspaces";
import { captureStage } from "./captureStage";
import type { CaptureRegistry } from "./stageRegistry";

/** How long a hidden tab holds back, so the tab the person is looking at answers first. */
export const HIDDEN_WAIT_MS = 400;

/** Take a picture of the open canvas, if there is one, and answer the agent's request with it. */
export async function answerRequest(
  workspaceId: string,
  request: string,
  registry: CaptureRegistry,
  options: { readonly hidden: boolean },
): Promise<void> {
  if (options.hidden) await new Promise((resolve) => setTimeout(resolve, HIDDEN_WAIT_MS));
  const source = registry.get();
  const answer = source ? await captureStage(source) : { error: "No canvas is on screen: the person is on a page outside it." };
  try {
    await answerCapture(workspaceId, request, answer);
  } catch {
    // Another tab answered first, or the request is over. `send` has logged it.
  }
}
