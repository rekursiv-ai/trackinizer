import { type ComponentProps, createContext, type ReactNode, use } from "react";
import type { ExtraProps } from "react-markdown";
import { chunk, useLoaded } from "../router/lazy";
import { Icon } from "../ui/icons";
import { useCopy } from "../ui/toast";
import { textOf } from "./code";
import { JsonView } from "./JsonView";

/** The server's inquiry kinds, for the refs in a code block's JSON. */
export const KindsContext = createContext<readonly string[]>([]);

/**
 * A code block: its JSON view when `rehypeJsonBlocks` found it to be JSON, else
 * its code, in highlight.js's colours once the highlighter has run. A wide one
 * scrolls sideways, so the keyboard can focus it, under the name Code, to scroll
 * it (axe's scrollable-region-focusable).
 */
export function CodeBlock({ node, children }: ComponentProps<"pre"> & ExtraProps) {
  const kinds = use(KindsContext);
  const json = node?.data?.json;
  // Without the newline Markdown ends a block with.
  const text = node ? textOf(node).replace(/\n$/, "") : "";
  return (
    <CodeFrame text={text} what={json ? "JSON" : "code"}>
      {json ? (
        <JsonView root={json} kinds={kinds} />
      ) : (
        <pre tabIndex={0} role="group" aria-label="Code">
          {children}
        </pre>
      )}
    </CodeFrame>
  );
}

/** Code or JSON in its box, with a Copy button that copies `text`, as written. */
export function CodeFrame({ text, what, children }: { text: string; what: "code" | "JSON"; children: ReactNode }) {
  const copy = useCopy();
  return (
    <div className="md-code">
      <button
        type="button"
        className="icon-btn md-copy"
        title={`Copy ${what}`}
        aria-label={`Copy ${what}`}
        onClick={() => void copy(text, `Copied the ${what}`)}
      >
        <Icon name="copy" size={13} />
      </button>
      {children}
    </div>
  );
}

/**
 * The highlighter (`./highlight`), a chunk of its own, while `wanted`: undefined
 * until it has loaded, at once after. Markdown takes its rehype plugin, a
 * transcript its `highlightLines`. A failed load leaves code uncoloured;
 * `reloadOnChunkError` reloads the page for a deploy's missing chunk.
 */
export function useHighlighter(wanted: boolean): Highlighter | undefined {
  useLoaded(HIGHLIGHTER, wanted && !highlighter);
  return wanted ? highlighter : undefined;
}

type Highlighter = typeof import("./highlight");
let highlighter: Highlighter | undefined;
let loading: Promise<void> | undefined;
const HIGHLIGHTER = {
  preload: () =>
    (loading ??= chunk(import("./highlight")).then((module) => {
      highlighter = module;
    })),
};
