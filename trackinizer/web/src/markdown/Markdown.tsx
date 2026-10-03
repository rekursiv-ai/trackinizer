import type { Root } from "mdast";
import { findAndReplace } from "mdast-util-find-and-replace";
import { type ComponentProps, memo, useEffect, useMemo, useState } from "react";
import ReactMarkdown, { type Components, type ExtraProps, type Options } from "react-markdown";
import remarkGfm from "remark-gfm";
import { useCopy } from "../ui/toast";
import { hasFence, rehypeJsonBlocks } from "./code";
import { CodeBlock, CodeFrame, KindsContext, useHighlighter } from "./CodeBlock";
import type { rehypeHighlightCode } from "./highlight";
import { parseJson } from "./json";
import { JsonView } from "./JsonView";
import { Link } from "./Link";
import { remarkPaths } from "./remarkPaths";
import { remarkRefs } from "./remarkRefs";
import { safeUrl } from "./safeUrl";
import "./markdown.css";

/**
 * Markdown from the server, rendered straight to React elements.
 *
 * No HTML string is ever built, and raw HTML in the source shows as its text
 * (react-markdown's default without a raw-HTML plugin). Links and images keep
 * only `http://`, `https://` and in-app `#/` targets (`safeUrl`); `Kind#seq` refs
 * and UUIDs in text become in-app links (`remarkRefs`), and absolute paths code
 * that copies them (`remarkPaths`). `kinds` are the server's inquiry kinds.
 * `images={false}` renders an image as inert text (alt and URL), so untrusted
 * text, such as an agent's reply, cannot make the browser fetch a URL. `breaks`
 * keeps each line of a paragraph, as a chat keeps a message's (GitHub's
 * comments, Slack); without it, as in a description, lines reflow.
 *
 * A source that is all one JSON object or array shows as a JSON view, as does a
 * code block that is (`rehypeJsonBlocks`). Other code blocks take highlight.js's
 * colours once its chunk has loaded, which a source with a fenced block asks for
 * (`useHighlighter`). A source is checked for JSON once, not on every render.
 */
export const Markdown = memo(function Markdown({
  source,
  kinds,
  className = "md",
  images = true,
  breaks = false,
}: {
  source: string;
  kinds: readonly string[];
  className?: string;
  images?: boolean;
  breaks?: boolean;
}) {
  const [json, fenced] = useMemo(() => {
    const parsed = parseJson(source);
    return [parsed, !parsed && hasFence(source)] as const;
  }, [source]);
  const highlight = useHighlighter(fenced)?.rehypeHighlightCode;
  return (
    <div className={className}>
      <KindsContext value={kinds}>
        {json ? (
          <CodeFrame text={source.trim()} what="JSON">
            <JsonView root={json} kinds={kinds} />
          </CodeFrame>
        ) : (
          <ReactMarkdown {...markdownOptions({ kinds, images, breaks, highlight })}>{source}</ReactMarkdown>
        )}
      </KindsContext>
    </div>
  );
});

/**
 * Whether Markdown has rendered before on this page, warming it, the first time
 * a view asks, with a few small texts, each in a task of its own.
 *
 * Markdown's first render runs code the page has not run yet, which costs more
 * than any later render, and React cannot split one component's render: with
 * the CPU slowed 4x on an M-series Mac, a transcript's first reply drew in a
 * task of 38 ms cold against 20 warmed, and on a Xeon a transcript's first draw
 * was one task of 78 to 90 ms. A view that would render Markdown at once waits
 * for this.
 */
export function useMarkdownWarm(): boolean {
  const [warm, setWarm] = useState(warmed);
  useEffect(() => {
    if (warm) return;
    let mounted = true;
    void warmMarkdown().then(() => {
      if (mounted) setWarm(true);
    });
    return () => {
      mounted = false;
    };
  }, [warm]);
  return warm;
}

let warmed = false;
let warming: Promise<void> | undefined;

/** Render each of `WARM_UP` in a task of its own, once, however often asked. */
function warmMarkdown(): Promise<void> {
  return (warming ??= (async () => {
    for (const [source, breaks] of WARM_UP) {
      await new Promise((resolve) => setTimeout(resolve));
      // The pipeline alone, as a render runs it: its result, React elements, goes nowhere.
      ReactMarkdown({ ...markdownOptions({ kinds: ["Issue"], images: false, breaks }), children: source });
    }
    warmed = true;
  })());
}

/**
 * What `useMarkdownWarm` renders, a task each, and whether with `breaks`: between
 * them, each kind of block and span a reply or a description holds.
 */
const WARM_UP: readonly (readonly [string, boolean])[] = [
  ["Hello *there*.", false],
  ["# Plan\n\n- one **two** `three`\n- [four](https://example.com) Issue#4 at /opt/x/y\n\n> said", false],
  ["| a | b |\n|---|---|\n| c | d |", false],
  ["```py\nx = 1\n```\n\n1. first\n2. second\n\n---\n\nhttps://example.com\n00000000-0000-4000-8000-000000000001", true],
];

/** react-markdown's options for a `Markdown` of these props; `highlight` colours code, once the highlighter has loaded. */
function markdownOptions({
  kinds,
  images,
  breaks,
  highlight,
}: {
  kinds: readonly string[];
  images: boolean;
  breaks: boolean;
  highlight?: typeof rehypeHighlightCode;
}): Options {
  return {
    remarkPlugins: [remarkGfm, remarkPaths, [remarkRefs, { kinds }], ...(breaks ? [remarkBreaks] : [])],
    rehypePlugins: highlight ? [rehypeJsonBlocks, highlight] : [rehypeJsonBlocks],
    urlTransform: safeUrl,
    components: images ? COMPONENTS : NO_IMAGE_COMPONENTS,
  };
}

// Headings step down two levels: the page's title is the one h1, and its
// sections are h2s, so a description's `#` must not outrank either.
const COMPONENTS: Components = {
  a: Link,
  code: CodeSpan,
  pre: CodeBlock,
  h1: "h3",
  h2: "h4",
  h3: "h5",
  h4: "h6",
  h5: "h6",
  h6: "h6",
};

/** Each newline in a paragraph as a line break, as remark-breaks makes one. */
function remarkBreaks() {
  return (tree: Root) => findAndReplace(tree, [[/\r?\n/g, () => ({ type: "break" as const })]]);
}

/** Code, or a path (`remarkPaths`): code that copies its path on a click. */
function CodeSpan({ className, children }: ComponentProps<"code"> & ExtraProps) {
  return className === "md-path" ? <CopyPath path={String(children)} /> : <code className={className}>{children}</code>;
}

function CopyPath({ path }: { path: string }) {
  const copy = useCopy();
  return (
    <button type="button" className="md-path" aria-label={`Copy path ${path}`} title="Copy path" onClick={() => void copy(path, "Copied the path")}>
      <code>{path}</code>
    </button>
  );
}

/** An image as text: its alt and URL, never an element the browser loads. */
function InertImage({ src, alt }: ComponentProps<"img"> & ExtraProps) {
  return <span className="md-dead-link">{[alt, src ? `(${String(src)})` : ""].filter(Boolean).join(" ")}</span>;
}

const NO_IMAGE_COMPONENTS: Components = { ...COMPONENTS, img: InertImage };
