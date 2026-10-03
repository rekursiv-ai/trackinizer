import { startTransition, useEffect, useMemo, useState } from "react";
import { Markdown } from "./Markdown";

/**
 * Markdown rendered a part at a time (`markdownParts`): the first part at once,
 * each later one in a task of its own, as a transition. A 128 KB description
 * parsed in one task took 527 ms with the CPU slowed 4x, so no task now parses
 * more than one part. A source of one part renders as `Markdown` does.
 */
export function ProgressiveMarkdown({ source, kinds }: { source: string; kinds: readonly string[] }) {
  const parts = useMemo(() => markdownParts(source), [source]);
  const [shown, setShown] = useState(1);
  useEffect(() => {
    if (shown >= parts.length) return;
    const next = setTimeout(() => startTransition(() => setShown(shown + 1)));
    return () => clearTimeout(next);
  }, [shown, parts.length]);
  if (parts.length === 1) return <Markdown source={source} kinds={kinds} />;
  return (
    <div className="md">
      {parts.slice(0, shown).map((part, index) => (
        // Keyed by place, so after an edit only the parts whose text changed parse again.
        <Markdown key={index} source={part} kinds={kinds} className="md-part" />
      ))}
    </div>
  );
}

/**
 * `source` cut into parts that each parse alone: above 12K characters, before
 * headings outside code fences, once a part holds 6K. A heading at the start of
 * a line outside a fence always starts a block, so no cut splits a list, a
 * table or code. A reference-style link or a footnote whose definition lands
 * in another part would not resolve, so a source holding `]:` or `[^` stays
 * whole.
 */
export function markdownParts(source: string): string[] {
  if (source.length <= LONG || source.includes("]:") || source.includes("[^")) return [source];
  const parts: string[] = [];
  let part: string[] = [];
  let size = 0;
  let fence = "";
  for (const line of source.split("\n")) {
    const marker = /^ {0,3}(`{3,}|~{3,})/.exec(line)?.[1];
    if (marker && !fence) fence = marker;
    // A fence closes on a run of its own character, as long or longer, with nothing after it.
    else if (marker && marker[0] === fence[0] && marker.length >= fence.length && !line.trim().slice(marker.length)) fence = "";
    else if (!fence && size >= PART && /^#{1,6}(\s|$)/.test(line)) {
      parts.push(part.join("\n"));
      part = [];
      size = 0;
    }
    part.push(line);
    size += line.length + 1;
  }
  parts.push(part.join("\n"));
  return parts;
}

/** Sources longer than this are cut. */
const LONG = 12_000;
/** A part ends at its first heading once it holds this many characters. */
const PART = 6_000;
