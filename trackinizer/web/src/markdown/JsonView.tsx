import type { Paragraph } from "mdast";
import { type ReactNode, useMemo, useState } from "react";
import { Icon } from "../ui/icons";
import type { JsonNode } from "./json";
import { Link } from "./Link";
import { remarkRefs } from "./remarkRefs";
import "./JsonView.css";

/**
 * A JSON object or array, pretty-printed: one entry a line, numbers as written,
 * keys, strings, numbers, and true, false and null in the code colours. A nested
 * object or array folds behind its key's button, and starts folded when it holds
 * over 50 entries or lies three levels down. An object of numbers alone shows as
 * a table of key and value, each key under the prefix before its first `/`, as
 * W&B and TensorBoard group scalars. Refs and UUIDs in strings link as they do in
 * Markdown (`remarkRefs`).
 */
export function JsonView({ root, kinds }: { root: JsonNode; kinds: readonly string[] }) {
  const link = useMemo(() => linker(kinds), [kinds]);
  return (
    <div className="jv" role="group" aria-label="JSON">
      <Value node={root} name={undefined} depth={0} last link={link} />
    </div>
  );
}

/** A string's text, with its refs and UUIDs as links. */
type Linker = (text: string) => ReactNode;

/**
 * Where a value sits: its key in an object, its index in an array, or undefined
 * for the root.
 */
type Name = string | number | undefined;

/** One value, after its key when it has one; the last in its object or array takes no comma. */
function Value({ node, name, depth, last, link }: { node: JsonNode; name: Name; depth: number; last: boolean; link: Linker }) {
  if (node.type === "object" || node.type === "array") {
    return <Branch node={node} name={name} depth={depth} last={last} link={link} />;
  }
  return (
    <div className="jv-line">
      {typeof name === "string" ? <Key name={name} /> : null}
      {node.type === "string" ? (
        <span className="jv-string">&quot;{link(node.value)}&quot;</span>
      ) : (
        <span className={node.type === "number" ? "jv-number" : "jv-literal"}>{node.text}</span>
      )}
      {last ? null : ","}
    </div>
  );
}

/** An object or array: its entries, or, folded, a count of them. */
function Branch({
  node,
  name,
  depth,
  last,
  link,
}: {
  node: Extract<JsonNode, { type: "object" | "array" }>;
  name: Name;
  depth: number;
  last: boolean;
  link: Linker;
}) {
  const size = node.type === "object" ? node.entries.length : node.items.length;
  const [open, setOpen] = useState(depth === 0 || (size <= FOLD_ENTRIES && depth < FOLD_DEPTH));
  const [start, end] = node.type === "object" ? ["{", "}"] : ["[", "]"];
  const comma = last ? null : ",";
  const head =
    name === undefined || !size ? (
      typeof name === "string" ? <Key name={name} /> : null
    ) : (
      <>
        <button
          type="button"
          className="jv-toggle"
          aria-expanded={open}
          aria-label={typeof name === "string" ? name : `item ${name}`}
          onClick={() => setOpen(!open)}
        >
          <Icon name={open ? "chevD" : "chevR"} size={12} />
          {typeof name === "string" ? <span className="jv-key">&quot;{name}&quot;</span> : null}
        </button>
        {typeof name === "string" ? ": " : null}
      </>
    );
  if (!size || !open) {
    return (
      <div className="jv-line">
        {head}
        {start}
        {size ? "…" : null}
        {end}
        {comma}
        {size ? (
          <span className="jv-count">
            {" "}
            {size} {node.type === "object" ? (size === 1 ? "key" : "keys") : size === 1 ? "item" : "items"}
          </span>
        ) : null}
      </div>
    );
  }
  const numbers = node.type === "object" ? numberGroups(node.entries) : undefined;
  return (
    <>
      <div className="jv-line">
        {head}
        {start}
      </div>
      <div className="jv-kids">
        {node.type === "array" ? (
          node.items.map((item, index) => (
            <Value key={index} node={item} name={index} depth={depth + 1} last={index === size - 1} link={link} />
          ))
        ) : numbers ? (
          <NumberTable groups={numbers} />
        ) : (
          node.entries.map(([key, value], index) => (
            <Value key={index} node={value} name={key} depth={depth + 1} last={index === size - 1} link={link} />
          ))
        )}
      </div>
      <div className="jv-line">
        {end}
        {comma}
      </div>
    </>
  );
}

/** An object or array of more entries than this starts folded. */
const FOLD_ENTRIES = 50;
/** An object or array this many levels below the root starts folded. */
const FOLD_DEPTH = 3;

function Key({ name }: { name: string }) {
  return (
    <>
      <span className="jv-key">&quot;{name}&quot;</span>:{" "}
    </>
  );
}

/**
 * An object's entries as key and number, grouped by the prefix before each key's
 * first `/`; undefined unless every value is a number.
 */
function numberGroups(entries: readonly (readonly [string, JsonNode])[]): Map<string, [string, string][]> | undefined {
  const groups = new Map<string, [string, string][]>();
  for (const [key, value] of entries) {
    if (value.type !== "number") return undefined;
    const slash = key.indexOf("/");
    const [prefix, rest] = slash > 0 ? [key.slice(0, slash), key.slice(slash + 1)] : ["", key];
    if (!groups.has(prefix)) groups.set(prefix, []);
    groups.get(prefix)!.push([rest, value.text]);
  }
  return groups;
}

/** An object of numbers as a table of key and value, each group under its prefix. */
function NumberTable({ groups }: { groups: Map<string, [string, string][]> }) {
  return (
    <table className="jv-table">
      {[...groups].map(([prefix, rows]) => (
        <tbody key={prefix} className={prefix ? "jv-group" : undefined}>
          {prefix ? (
            <tr>
              <th colSpan={2} scope="rowgroup">
                {prefix}/
              </th>
            </tr>
          ) : null}
          {rows.map(([key, number], index) => (
            <tr key={index}>
              <th scope="row">{key}</th>
              <td className="num">{number}</td>
            </tr>
          ))}
        </tbody>
      ))}
    </table>
  );
}

/** The `Linker` for `kinds`: `remarkRefs` run over a string as over a paragraph of text. */
function linker(kinds: readonly string[]): Linker {
  const refs = remarkRefs({ kinds });
  return (text) => {
    const paragraph: Paragraph = { type: "paragraph", children: [{ type: "text", value: text }] };
    refs({ type: "root", children: [paragraph] });
    return paragraph.children.map((node, index) =>
      node.type === "link" ? (
        <Link key={index} href={node.url} className="ref" title={node.title ?? undefined}>
          {node.children.map((child) => (child.type === "text" ? child.value : "")).join("")}
        </Link>
      ) : node.type === "text" ? (
        node.value
      ) : null,
    );
  };
}
