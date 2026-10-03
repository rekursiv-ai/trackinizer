import { kindLook } from "../ui/kinds";
import type { Command } from "./registry";

/**
 * The Create commands: New for each of `kinds`, in the palette's Create section,
 * with C on the one for `current`, the kind on screen. `open` opens its form.
 */
export function createCommands(kinds: readonly string[], current: string | undefined, open: (kind: string) => void): Command[] {
  return kinds.map((kind) => ({
    id: `create.${kind}`,
    title: `New ${kindLook(kind).one}`,
    section: "Create",
    ...(kind === current && { keys: ["c"] }),
    run: () => open(kind),
  }));
}
