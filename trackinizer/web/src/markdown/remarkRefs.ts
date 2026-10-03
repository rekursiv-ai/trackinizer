import type { Link, Nodes, Root } from "mdast";
import { findAndReplace, type FindAndReplaceTuple } from "mdast-util-find-and-replace";
import { formatRoute, parseSeq } from "../router/route";

/**
 * A remark plugin that links `Kind#seq` refs and inquiry UUIDs.
 *
 * It rewrites text nodes only, so a ref inside code, a URL, raw HTML or a link's
 * own text stays as written, and no link ever nests in another (COLD-13). A ref
 * links to `#/ref/<Kind>/<seq>`, a UUID to `#/lookup/<uuid>`. `kinds` are the
 * server's inquiry kinds, matched as trax writes them (`Issue#4`, not `issue#4`);
 * a ref right after a word character or another `#` (`x#Issue#4`) is not one.
 * A link written as a ref, its text the ref and its target that ref in the app
 * (`[Issue#4](https://<host>/app/#/ref/Issue/4)`, as agents copy one), is the
 * ref's own link too.
 */
export function remarkRefs({ kinds }: { kinds: readonly string[] }) {
  const patterns: FindAndReplaceTuple[] = [[UUID, uuidLink]];
  // With no kinds the pattern would be `()#(\d+)` and link every bare `#4`.
  const names = kinds.map((kind) => kind.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
  if (kinds.length) patterns.unshift([new RegExp(`(?<![\\w#])(${names})#(\\d+)\\b`, "g"), refLink]);
  return (tree: Root) => {
    if (kinds.length) refLinks(tree, new RegExp(`^(${names})#(\\d+)$`));
    findAndReplace(tree, patterns, { ignore: ["link", "linkReference"] });
  };
}

const UUID = /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi;

/** A ref's route in the app, alone or after the app's address: `https://<host>/app/#/ref/Issue/4`, or `/#/ref/…` for the old UI's. */
const APP_REF = /^(?:https?:\/\/[^/?#\s]+(?:\/app)?\/)?(#\/ref\/.*)$/;

/**
 * Each link in `node` whose text is one ref (`ref`) and whose target is that
 * ref's route, in the app here or at its address on any host (`/` or `/app/`),
 * made the ref's own link, in place.
 */
function refLinks(node: Nodes, ref: RegExp): void {
  if (node.type !== "link") {
    if ("children" in node) for (const child of node.children) refLinks(child, ref);
    return;
  }
  const [only, ...rest] = node.children;
  if (only?.type !== "text" || rest.length) return;
  const named = ref.exec(only.value);
  const seq = named && parseSeq(named[2]!);
  if (!named || seq === null) return;
  const route = formatRoute({ name: "ref", kind: named[1]!, seq });
  if (APP_REF.exec(node.url)?.[1] === route) Object.assign(node, link(route, only.value, null));
}

/** A ref's link; `false` leaves a seq no inquiry can have as text. */
function refLink(text: string, kind: string, digits: string): Link | false {
  const seq = parseSeq(digits);
  return seq === null ? false : link(formatRoute({ name: "ref", kind, seq }), text, null);
}

/** A UUID shows its first eight characters, as the old UI's links did. */
function uuidLink(text: string): Link {
  const id = text.toLowerCase();
  return link(formatRoute({ name: "lookup", id }), text.slice(0, 8), id);
}

function link(url: string, text: string, title: string | null): Link {
  return {
    type: "link",
    url,
    title,
    // The class marks a link this plugin made: Markdown source cannot set one,
    // since raw HTML is never rendered.
    data: { hProperties: { className: ["ref"] } },
    children: [{ type: "text", value: text }],
  };
}
