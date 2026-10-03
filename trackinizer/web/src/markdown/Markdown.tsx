import type { Root } from "mdast";
import { findAndReplace } from "mdast-util-find-and-replace";
import { type ComponentProps, memo, useMemo } from "react";
import ReactMarkdown, { type Components, type ExtraProps } from "react-markdown";
import remarkGfm from "remark-gfm";
import { useCopy } from "../ui/toast";
import { hasFence, rehypeJsonBlocks } from "./code";
import { CodeBlock, CodeFrame, KindsContext, useHighlighter } from "./CodeBlock";
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
  const highlighter = useHighlighter(fenced)?.rehypeHighlightCode;
  return (
    <div className={className}>
      <KindsContext value={kinds}>
        {json ? (
          <CodeFrame text={source.trim()} what="JSON">
            <JsonView root={json} kinds={kinds} />
          </CodeFrame>
        ) : (
          <ReactMarkdown
            remarkPlugins={[remarkGfm, remarkPaths, [remarkRefs, { kinds }], ...(breaks ? [remarkBreaks] : [])]}
            rehypePlugins={highlighter ? [rehypeJsonBlocks, highlighter] : [rehypeJsonBlocks]}
            urlTransform={safeUrl}
            components={images ? COMPONENTS : NO_IMAGE_COMPONENTS}
          >
            {source}
          </ReactMarkdown>
        )}
      </KindsContext>
    </div>
  );
});

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
