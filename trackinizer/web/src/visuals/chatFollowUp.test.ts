import { expect, test } from "vitest";
import { followUpSuggested } from "./chatFollowUp";

test("a recap that names a next experiment, however it heads the line, suggests a follow-up", () => {
  for (const text of [
    "## Results\nThe climb held.\n\n## Next experiment\nRaise the effort to high.",
    "**Follow-up**: try the astra model on the same split.",
    "Next experiment: widen the matrix shard.",
    "### Next steps\n- rerun on fresh seeds",
    "Follow up — confirm on the held-out set.",
    "## Next experiments\n1. bigger batch\n2. longer warmup",
  ]) expect(followUpSuggested(text)).toBe(true);
});

test("prose that merely mentions a next step or a follow-up effect does not suggest one", () => {
  for (const text of [
    "The next step is to wait for the GPU to free up.",
    "We saw no follow-up effect once the baseline was frozen.",
    "## Results\nThe run plateaued at iteration four; nothing new to try.",
    "Next steps: nothing — the window closed.",
    "",
  ]) expect(followUpSuggested(text)).toBe(false);
});
