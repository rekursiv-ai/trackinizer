// THE AGENT'S DESIGN BRIEF, GENERATED FROM THE ANSWER TYPES.
//
// OpenUI's one good idea: the component catalog is the schema, and the system
// prompt is a projection of it, so the brief cannot drift from what the renderer
// accepts and an unregistered shape is impossible to ask for. We take that and
// none of its runtime: the source of truth here is the TypeScript the renderers
// already check themselves against, and the JSDoc on each field is the rule the
// distiller has to obey — altitude, how many, what each one opens when clicked.
//
// A union is printed as ARM PER LINE with the rule written above that arm, because
// an answer is a choice and a choice is the whole job: `Item`'s 24 arms are the
// ladder the model climbs to decide whether a question wants a paragraph, a table,
// a chart, a 3D scene or a video, and every one of those arms carries its own
// "pick this over X when…". Folded onto one line — which is what this file did
// until the arms were printed — the ladder is a list of names and the model cannot
// see a reason in it, so it defaults to prose and the canvas stays empty.
//
// Writes trax/docs/skills/trax/DESIGN.md when run as a script, which `npm run build`
// does before the wheel is packed — so the brief an installed agent reads is the one the
// current types produced. web/src/ui/brief.test.ts fails if the file on disk is not what
// the types say right now, if a field of a shape is missing from it, if a printed type
// references a shape the brief never defines, and if it passes its byte ceiling, because
// an agent reads the whole file before it writes one answer and every field's rule is a
// line of that read.
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";
import { readFileSync, writeFileSync } from "node:fs";
import ts from "typescript";
import { CATALOG } from "../src/ui/catalog.ts";

/** One field of a template, as the brief states it. */
type Field = { readonly name: string; readonly type: string; readonly doc: string };

/** One arm of a union the agent chooses between: the tag it picks by, what it holds,
 *  the rule for picking it over its neighbours, and the fields of a literal payload. */
type Arm = { readonly tag: string; readonly tagKey: string; readonly holds: string; readonly doc: string; readonly fields: readonly Field[] };

/** One template or sub-shape: its name, when the app reads an answer as this, and its fields. */
type Shape = {
  readonly name: string;
  readonly doc: string;
  readonly fields: readonly Field[];
  readonly arms: readonly Arm[];
};

/** The types the brief covers, in the order the agent should read them.
 *
 *  Every shape an arm holds is listed here, not only the shapes the top level names:
 *  `Item` points at `Code`, `Figure`, `ImageItem`, `IllustrationItem`, `Scene` and
 *  `Drawing`, and a brief that prints a name and never defines it sends the agent to
 *  guess at the payload of the one node it chose. `brief.test.ts` checks that closure
 *  rather than trusting this list to be complete. */
export const SOURCES: readonly { readonly file: string; readonly shapes: readonly string[] }[] = [
  // The open form, read first: a surface built from the catalog's own nodes. `Item` is
  // the grammar everything else on a pane is assembled out of too.
  { file: "src/ui/compose.tsx", shapes: ["ComposedFlow", "Item"] },
  // What each visual arm holds, in the order the arms ask for them: a figure's four
  // kinds, then the code, image, video and geometry nodes.
  { file: "src/ui/figure.tsx", shapes: ["Figure"] },
  { file: "src/ui/plotly.tsx", shapes: ["PlotlyTrace", "PlotlyData", "PlotlyLayout"] },
  { file: "src/ui/recharts.tsx", shapes: ["RechartsData", "RechartsType"] },
  { file: "src/ui/chart.tsx", shapes: ["Chart"] },
  { file: "src/ui/series.tsx", shapes: ["Series", "Curve"] },
  { file: "src/ui/equation.tsx", shapes: ["Equation"] },
  { file: "src/ui/table.tsx", shapes: ["NumTable", "Column"] },
  { file: "src/ui/code.tsx", shapes: ["Code", "CodeNote"] },
  { file: "src/ui/image.tsx", shapes: ["ImageItem"] },
  { file: "src/ui/illustration.tsx", shapes: ["IllustrationItem"] },
  // The node that carries geometry the agent authored itself. Without it the brief
  // describes only surfaces that are READ, and an agent has no way to discover that it
  // may draw the marks at all — the same gap that hid the open composition grammar
  // until `compose.tsx` was added here.
  { file: "src/ui/drawing.tsx", shapes: ["Drawing"] },
  { file: "src/ui/flow.tsx", shapes: ["FlowBlock", "FlowRelation"] },
  { file: "src/blocks/model.ts", shapes: ["KnowledgeFlow", "KnowledgeBlock", "ChainRow", "FlowEdge", "BlockStatus", "EdgeKind"] },
  // The library's own vocabulary, because a review's marks are the only field an
  // agent picks a verdict with.
  { file: "src/ui/sections.tsx", shapes: ["MarkKind", "Mark"] },
];

/** The primitives' own file: what the app owns and the agent may not ask for. */
const PRIMITIVES = "src/ui/sections.tsx";

/** This script's directory and the web root, as paths: the module reads its sources
 *  when it is imported, and the test runner's `URL` global resolves a relative
 *  specifier against a `file:` base to garbage, so no URL arithmetic happens here. */
const here = dirname(fileURLToPath(import.meta.url));
const web = join(here, "..");
export const briefPath = join(web, "..", "trax", "docs", "skills", "trax", "DESIGN.md");

const shapes = new Map<string, Shape>();
const docs: string[] = [];

/** A file's `import { Scene as SceneSpec }` renames: the name a source uses locally is
 *  not the name the brief can define, so a reference is resolved back to the exported
 *  one before it prints. Unresolved, the brief would say `SceneSpec` and then explain
 *  `Scene` — a dead end for the agent reading it, which is the same class of hole as a
 *  payload shape that was never listed. */
function aliasesOf(sf: ts.SourceFile): Map<string, string> {
  const out = new Map<string, string>();
  for (const statement of sf.statements) {
    if (!ts.isImportDeclaration(statement) || !statement.importClause) continue;
    const bindings = statement.importClause.namedBindings;
    if (!bindings || !ts.isNamedImports(bindings)) continue;
    for (const binding of bindings.elements) {
      if (ts.isImportSpecifier(binding) && binding.propertyName) out.set(binding.name.text, binding.propertyName.text);
    }
  }
  return out;
}

/** The keys that name which arm an object is: the brief prints them as the arm's tag
 *  rather than as a field, because an agent never chooses a `node` value and then
 *  chooses a node — the tag IS the choice. */
const TAGS = new Set(["node", "kind"]);

for (const source of SOURCES) {
  const text = readFileSync(join(web, source.file), "utf8");
  const sf = ts.createSourceFile(source.file, text, ts.ScriptTarget.ES2023, true);
  const aliases = aliasesOf(sf);
  const read = (node: ts.TypeNode): string => renderType(node, sf, aliases);
  for (const statement of sf.statements) {
    if (!ts.isTypeAliasDeclaration(statement) || !ts.isTypeLiteralNode(statement.type)) continue;
    const name = statement.name.text;
    if (!source.shapes.includes(name)) continue;
    shapes.set(name, {
      name,
      doc: jsDoc(statement, sf),
      fields: statement.type.members.filter(ts.isPropertySignature).map((property) => ({
        name: optionalName(property, sf),
        type: read(property.type!),
        doc: jsDoc(property, sf),
      })),
      arms: [],
    });
  }
  for (const statement of sf.statements) {
    if (!ts.isTypeAliasDeclaration(statement) || ts.isTypeLiteralNode(statement.type)) continue;
    const name = statement.name.text;
    if (!source.shapes.includes(name)) continue;
    const arms = armsOf(statement.type, sf, aliases);
    shapes.set(name, {
      name,
      doc: jsDoc(statement, sf),
      // A union of objects gets one line per arm; anything else stays one line, and a
      // bare name (`MarkKind = "fit" | "finding" | …`) is already the whole rule.
      fields: arms.length ? [] : [{ name: "", type: read(statement.type), doc: "" }],
      arms,
    });
  }
}

/** A union's arms, each with the rule written above it in the source. */
function armsOf(node: ts.TypeNode, sf: ts.SourceFile, aliases: Map<string, string>): Arm[] {
  if (!ts.isUnionTypeNode(node)) return [];
  const arms = node.types.map((member, index) => {
    const tag: string[] = [];
    // WHICH key the tag is written under. `Item` discriminates on `node` and `Figure`
    // on `kind`, and the brief used to print only the VALUE for both — so an agent
    // reading `- \`table\` · { table: NumTable }` had no way to know the object needs
    // `kind: "table"`. Every figure composed from that reading was dropped at runtime
    // with "this figure was not drawn: undefined is not a node this app renders",
    // silently, because a composition that loses one item still renders the rest.
    const tagKeys: string[] = [];
    const fields: Field[] = [];
    const refs: string[] = [];
    for (const part of flatten(member)) {
      if (ts.isTypeLiteralNode(part)) {
        for (const property of part.members.filter(ts.isPropertySignature)) {
          const name = property.name.getText(sf);
          if (TAGS.has(name)) {
            tag.push(property.type ? renderType(property.type, sf, aliases).replace(/^"|"$/g, "") : "");
            tagKeys.push(name);
            continue;
          }
          fields.push({ name: optionalName(property, sf), type: renderType(property.type!, sf, aliases), doc: jsDoc(property, sf) });
        }
      } else if (ts.isTypeReferenceNode(part)) {
        refs.push(renderType(part, sf, aliases));
      } else {
        fields.push({ name: "", type: renderType(part, sf, aliases), doc: "" });
      }
    }
    const inline = fields.length ? `{ ${fields.map((field) => `${field.name}: ${field.type}`).join(", ")} }` : "";
    return {
      tag: tag.filter(Boolean).join(" | "),
      tagKey: tagKeys[0] ?? "",
      holds: [inline, ...refs].filter(Boolean).join(" & "),
      doc: armDoc(node, index, sf),
      fields,
    };
  });
  // An arm the agent picks by a tag is a CHOICE and gets a line each. `EdgeKind` and
  // the other vocabulary unions are lists of names, not decisions, and a member with no
  // tag is that kind of union — giving it a row and a bullet would be noise with a
  // heading on it, so the shape keeps its single line.
  return arms.every((arm) => arm.tag) ? arms : [];
}

/** `{ node: "code" } & Code` is one arm written as two parts; an arm that is only a
 *  literal or only a reference flattens to itself. */
function flatten(node: ts.TypeNode): ts.TypeNode[] {
  if (ts.isParenthesizedTypeNode(node)) return flatten(node.type);
  if (ts.isIntersectionTypeNode(node)) return node.types.flatMap(flatten);
  return [node];
}

/** A union or reference type as the brief prints it; an inline object literal is
 *  written out in full, because a name the brief never defines is a dead end for
 *  the agent reading it. */
function renderType(node: ts.TypeNode, sf: ts.SourceFile, aliases: Map<string, string>): string {
  if (ts.isTypeReferenceNode(node)) {
    const name = node.typeName.getText(sf);
    const inner = node.typeArguments?.[0];
    if (inner && (name === "readonly" || name === "ReadonlyArray" || name === "Array")) return `${renderType(inner, sf, aliases)}[]`;
    // `Readonly<T>` is the app's promise that an answer is not mutated behind a render;
    // an agent writing JSON has nothing to do with it, and printing it would put a name
    // in the brief that is neither a shape nor a value it can write.
    if (name === "Readonly" && inner) return renderType(inner, sf, aliases);
    if (name === "Record") return `Record<${(node.typeArguments ?? []).map((one) => renderType(one, sf, aliases)).join(", ")}>`;
    return aliases.get(name) ?? name;
  }
  if (ts.isTypeLiteralNode(node)) {
    return `{ ${node.members.filter(ts.isPropertySignature).map((p) => `${p.name.getText(sf)}: ${renderType(p.type!, sf, aliases)}`).join(", ")} }`;
  }
  // A union inside an array keeps its parentheses: `string | number[]` reads as an
  // array of numbers or a string, which is not the row an agent is being asked for.
  if (ts.isArrayTypeNode(node)) {
    const inner = renderType(node.elementType, sf, aliases);
    return ts.isUnionTypeNode(node.elementType) ? `(${inner})[]` : `${inner}[]`;
  }
  if (ts.isUnionTypeNode(node)) return node.types.map((one) => renderType(one, sf, aliases)).join(" | ");
  if (ts.isLiteralTypeNode(node)) return `"${node.literal.getText(sf).replace(/^"|"$/g, "")}"`;
  return node.getText(sf);
}

/** A JSDoc block's text, stripped of its leading stars and folded to one line. */
function plain(block: string): string {
  return block
    .split("\n")
    .map((line) => line.replace(/^\s*\*?\s?/, "").trim())
    .filter(Boolean)
    // `@param` and friends are for the repo's readers, not for the agent: the sentence
    // above the tags is the rule.
    .filter((line) => !line.startsWith("@"))
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
}

/** A field's name, with the `?` the source carries: whether an answer may leave a
 *  field out is part of its rule, and a brief that prints `size` for `size?` sends
 *  the agent setting a value it was never asked for. */
function optionalName(property: ts.PropertySignature, sf: ts.SourceFile): string {
  return `${property.name.getText(sf)}${property.questionToken ? "?" : ""}`;
}

/** The rule written above one arm of a union.
 *
 *  Not `node.jsDoc`, and not the node's own leading trivia: the parser puts a member's
 *  `pos` AFTER its `|`, and an author writes the rule ABOVE the bar, so the sentence
 *  that says when to pick one arm over its neighbour belongs to no node in the tree. It is
 *  read off the text between the previous arm's end and this arm's first token — which
 *  is the one range that holds it, and the reason the brief printed 24 bare names for
 *  as long as it did. */
function armDoc(union: ts.UnionTypeNode, index: number, sf: ts.SourceFile): string {
  const arm = union.types[index]!;
  const from = index === 0 ? union.pos : union.types[index - 1]!.end;
  const between = sf.text.slice(from, arm.getStart(sf));
  return [...between.matchAll(/\/\*\*([\s\S]*?)\*\//g)].map((match) => plain(match[1]!)).join(" ");
}

/** A declaration's JSDoc, folded to one line, with the tags left out: the sentence
 *  above them is the rule, and the parser keeps it on the node it documents. */
function jsDoc(node: ts.Node, sf: ts.SourceFile): string {
  const blocks = (node as { readonly jsDoc?: readonly ts.JSDoc[] }).jsDoc ?? [];
  const text = blocks
    .map((block) => (typeof block.comment === "string" ? block.comment : block.comment?.map((one) => one.getText(sf)).join("")))
    .filter((one): one is string => !!one)
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
  return text;
}

for (const name of SOURCES.flatMap((source) => source.shapes)) {
  const shape = shapes.get(name);
  if (!shape) throw new Error(`${name} is listed in this script and is no longer a type the brief can read`);
  docs.push(renderShape(shape));
}

/** The catalog as the app registers it: every node, the library that draws it, what it
 *  renders and when to pick it over its neighbour. Printed from `CATALOG` rather than
 *  retyped, so the brief cannot name a node the app does not have or omit one it does —
 *  and so the loop that injects this context never carries a capability list of its own.
 *  Each entry's DOM measure is deliberately NOT printed: it is what `catalog.test.tsx`
 *  asserts, not something an answer can write, and the brief's ceiling is spent on what
 *  the agent can act on. */
const nodes = [
  "## What exists to build with",
  "",
  ...CATALOG.map(
    (n) =>
      `- \`${n.node}\` · ${n.library} — ${n.renders} *When:* ${n.pick}` +
      (n.composition ? ` *Composition:* ${n.composition}` : ""),
  ),
  "",
];

function renderShape(shape: Shape): string {
  // Said once per union rather than on every arm: the key costs ~9 bytes a line
  // repeated, and this brief is read whole under a byte ceiling.
  const key = shape.arms.find((arm) => arm.tagKey)?.tagKey ?? "";
  const picks = key ? `Each arm is written with \`${key}\` set to its name below.\n` : "";
  const head = `## ${shape.name}\n${shape.doc ? `${shape.doc}\n` : ""}${picks}`;
  const lines = shape.arms.length
    ? shape.arms.map((arm) => {
        const own = arm.fields.filter((field) => field.name && field.doc);
        return [
          `- \`${arm.tag}\`${arm.holds ? ` · ${arm.holds}` : ""}${arm.doc ? ` — ${arm.doc}` : ""}`,
          // A literal payload keeps its own field rules; a named one (`Code`, `Scene`)
          // states them under its own heading, so printing them twice is noise.
          ...own.map((field) => `  - \`${field.name}\` · ${field.type} — ${field.doc}`),
        ].join("\n");
      })
    : shape.fields.map((field) => (field.name ? `- \`${field.name}\` · ${field.type}${field.doc ? ` — ${field.doc}` : ""}` : `- ${field.type}`));
  return `${head}${lines.join("\n")}\n`;
}

const primitives = ts.createSourceFile(PRIMITIVES, readFileSync(join(web, PRIMITIVES), "utf8"), ts.ScriptTarget.ES2023, true);
const owned = primitives.statements
  .filter(ts.isFunctionDeclaration)
  .map((fn) => `- \`${fn.name!.getText()}\` — ${jsDoc(fn, primitives)}`);

/** Every shape the brief defines, by the name an agent sees after `## `. */
export const defined: readonly string[] = SOURCES.flatMap((source) => source.shapes);

/** One arm the agent chooses between, as the rule test reads it: the shape it belongs
 *  to, the tag it picks by, what it holds and the sentence that says when. */
export type Choice = { readonly shape: string; readonly tag: string; readonly holds: string; readonly doc: string };

export const choices: readonly Choice[] = [...shapes.values()].flatMap((shape) =>
  shape.arms.map((arm) => ({ shape: shape.name, tag: arm.tag, holds: arm.holds, doc: arm.doc })),
);

/** Every string the brief prints in a TYPE position — a field's type, an arm's payload.
 *  A name in here that `defined` does not hold is a dead end: the agent is told its node
 *  holds a `Scene` and then handed no fields for one, which is how a chosen node comes
 *  back as a guess. `brief.test.ts` checks the closure instead of trusting `SOURCES`. */
export const printedTypes: readonly string[] = [...shapes.values()].flatMap((shape) => [
  ...shape.fields.map((field) => field.type),
  ...shape.arms.flatMap((arm) => [arm.holds, ...arm.fields.map((field) => field.type)]),
]);

/** The brief itself. Importing this module reads the types and builds the text; only
 *  running it as a script writes the file, so web/src/ui/brief.test.ts can compare what
 *  is on disk with what the types say. */

/** The figure menu exists because one failure kept recurring in real answers: an agent
 *  that had drawn a `series` drew a `series` again, with different nouns, until three
 *  unrelated answers read as one template. The catalog holds ~20 visual kinds and the
 *  agent was reaching for two. This menu maps the shape of the rows to the mark, and
 *  demands the loser's case in the label, so variety is a checkable step rather than a
 *  hope. It sits directly after the catalog list, where the node names are still open. */
const figureMenu = [
  "## The figure menu: the mark follows the shape",
  "",
  "A figure kind is chosen by the shape of the rows, never by the previous answer's habit:",
  "three answers drawing `series` with different nouns are one template, and a reader",
  "feels that before they can name it. Before placing a `figure`, name the shape of the",
  "data, take the mark from this menu, and when two kinds would both carry it, say in the",
  "label why the loser lost. The app owns ink, so variety cannot come from colour: two",
  "surfaces differing only in palette are one surface drawn twice. The type is yours.",
  "",
  "- One quantity over a genuinely continuous span → `series` (line): the line asserts",
  "  interpolation between points, so the span must really be continuous.",
  "- A few arms compared at separate points → `chart` bars or dot plot: no line through",
  "  0.3, 1.2 and 5 ns, nothing exists at 0.4.",
  "- The story is the delta between two arms → bars of the difference, or a slope chart.",
  "- A winner plus its margin per row → `table` with the margin beside it; bars only when",
  "  the ranking is itself the reading.",
  "- Density or value over a 2D field → `chart` rect marks, or a Plotly heatmap.",
  "- Part-to-whole, or movement between stages → Sankey or a stacked area.",
  "- Many categories over one period → recharts radar or bar; a share of a whole → pie.",
  "- Points in high dimensions, named by cluster → `embedding`.",
  "- Training runs over steps → `learningcurve`; classification verdicts → `confusion`.",
  "- Surfaces, volumes, anything a reader orbits → Plotly 3D.",
  "- A mechanism that runs, or an evolution no frozen frame holds → `drawing` with SMIL,",
  "  or a published `embed`.",
  "- Records and their wires → `graph`, which is not a chart at all.",
  "",
];

export const brief = [
  "# How an answer is drawn",
  "",
  "<!-- Generated by web/scripts/design-brief.ts from the web app's own types. Do not",
  "     edit: the fields, their order and the rule on each come from the source, and",
  "     web/src/ui/brief.test.ts fails if this file and the types disagree. -->",
  "",
  "You never write markup, a colour, a glyph or a position. You write one JSON answer and",
  "the app renders it with the design it owns. What is yours: which nodes the question asks",
  "for, how many items it holds, the order they read in, the composition, and what",
  "each item opens when a reader clicks it. Fill every shape below with real records —",
  "an item whose provenance you cannot name is an item you have not written yet.",
  "",
  "## The order to think in",
  "",
  "Written here rather than derived from a type: no TypeScript union says in what order to",
  "reason, and an answer written in one leap comes out as a paragraph, because a paragraph",
  "is the shortest thing that finishes a sentence. Five passes, in this order.",
  "",
  "1. **Read the question.** Name what would settle it — a quantity, a comparison, a",
  "   mechanism, a location in a file, a chain of who-said-what, a decision. One noun, not",
  "   a sentence; everything after this follows it.",
  "2. **Read the graph around it, before you plan anything.** Pull the records that answer",
  "   it AND what is wired to them: which claim this one supports, which run produced it,",
  "   which experiment refutes it, which prior it answers. Two facts get decided here and",
  "   nowhere else — what the answer may claim (a record you did not open is a record you",
  "   cannot cite), and which parts belong together (a wire between two records is why they",
  "   share a `card`, or a tier of the same `flow`, instead of two unrelated sections). You",
  "   assert no edge; the store holds them. Reading them is what makes this a graph answer",
  "   rather than a search result.",
  "3. **Plan the canvas, then fill it.** The layout that is yours is the reading order and",
  "   the grouping — ink, position and size are the app's. Decide what reads first, which",
  "   items sit in one container, which share a `row` (lead / fill / trail), which belong",
  "   across a `tier` of `card`s, a `flow` you wire, or the `graph` the store wires; and",
  "   where prose keeps the reading measure, not the full span. Say the plan back as one",
  "   line per part: part · node · how many · what it opens when clicked.",
  "4. **Design each part separately, against its own rule.** Each line of that plan is a",
  "   decision made from the arm's own `*When:*` rule below and from nothing else. This file",
  "   maps no subject onto a node: a mapping here is a lookup an answer performs instead of",
  "   a judgement it makes, and two answers handed the same lookup and the same records come",
  "   out the same page. The arms carry the distinctions where each node is registered.",
  "   Most answers hold several kinds; a surface using one node for all of them has thrown",
  "   away the parts that were not prose. Count what the records hold before dressing it and",
  "   write that count into `budget`: the app counts the render and checks it, and a guessed",
  "   number is the one part no later step can repair.",
  "5. **Render it once, as one answer.** The order is yours and this file gives none. What",
  "   decides it is the question: a reader's first glance should land on whatever carries",
  "   this answer's weight — a quantity, a disagreement, a mechanism, a queue, an address —",
  "   and step 2 is where you learned which. An order stated here is the quickest way to",
  "   make every answer the same page. The opening item is the author's as well: a page may",
  "   open on a `drawing`, a `figure`, a `graph`, a `tier` or an `embed` when one of those",
  "   carries the weight, and nothing requires it to open on a heading and prose. A `heading` per part a reader would name, nothing",
  "   under a heading that does not belong to it. Then check what a first pass gets wrong:",
  "   is every number and name here on the records, and is any node here only because the",
  "   lane looked empty? A node you cannot cite is deleted, not reworded.",
  "",
"No one-size-fits-all blueprint is offered: a question about a knowledge cluster",
  "does not want the same opening, grouping or density as a question about a",
  "measurement. The sequence — the leading node, the grouping, the density,",
  "the depth of a card — is derived fresh for every answer from the records in",
  "step 2, and the derivation must be re-checkable from those records. A",
  "composition that recurs across unrelated answers with only nouns swapped",
  "is a template, not a decision the answer made.",
  "The surface is the answer's own compromise. A parts list is resolved by",
  "the reader's eye, so the plan in step 3 is kept to the parts themselves,",
  "one node per weight, the order the weight gives the first glance, and the",
  "top-level budget. It is never a template for the next answer.",
  "**No sequence is selected for you**: do not assume a heading-then-prose-then-figure-then-table-then-visual order. Derive the opening item, grouping, and density from the answer's weight in step 2 for every surface. A repeated first → second → third pattern across answers is a hardcoded design, not an authored composition.",
  "Never use a tier of two cards for competing hypotheses; use flow/graph/drawing/figure for the mechanism.",
  "**Pre-display verification gate**: before publishing, render live, screenshot full height, run verify-canvas.ts + manual review for lines crossing labels, box overlaps, clipping, zero-size elements, refused nodes, on-palette ink. Fix defects or name them explicitly — never display a known-broken surface.",
  "**The steps cost reasoning, not calls.** One read of the graph, one write of the flow,",
  "one publish. A surface that arrives late is not one that thought too little; it is one",
  "that round-tripped. So do not re-fetch what is already in hand, do not draw half the",
  "answer and then go looking for the rest, and do not publish twice: by the time you write",
  "the JSON, steps 1 to 4 are finished and the only thing left is typing it.",
  "",
  ...nodes,
  ...figureMenu,
  ...docs,
  "## UI dynamism: proactive visual composition",
  "",
  "An answer on this canvas is a graphical instrument, never a chat transcript or a wall of prose.",
  "When answering a question, never default to text paragraphs or raw markdown tables. Proactively reach",
  "for the catalog's custom components to present structure, metrics, comparisons, and mechanisms:",
  "",
  "- **Never dump the answer into Chat.** Chat is strictly the conversation control log and executive index —",
  "  it must hold at most one or two sentences pointing the reader to the canvas. Every table, sweep,",
  "  quantitative comparison, hypothesis card, and knowledge graph belongs ON THE CANVAS. An agent that dumps",
  "  markdown tables, detailed numerical sweeps, and lists into Chat while leaving the Canvas sparse has",
  "  completely inverted the product.",
  "- **Metrics and sweeps go in `figure` tables, never text or chat.** Tabular figures, benchmark results,",
  "  hyperparameter sweeps, ablation runs, turnover numbers, and performance metrics MUST use `figure` with `kind: \"table\"`",
  "  and a `NumTable` payload (`caption`, `columns`, `rows`). Never print markdown tables in Chat or monospace ASCII grids.",
  "  The app automatically measures column widths, formats decimal precision, and renders derived spreads.",
  "- **Competing hypotheses and alternatives sit in a `tier` of `card`s.** When comparing multiple leads,",
  "  branches, models, or candidate explanations, do not describe them sequentially in prose. Group them side-by-side",
  "  across a `tier` of `card`s with `lane` set (e.g. `36`–`45ch`). Each card holds its own composition —",
  "  headings, prose, metrics, and marks — allowing the reader to compare branches at a glance.",
  "- **Knowledge, claims, and relations proactively embed `graph` within the canvas.** When an answer touches",
  "  beliefs, issues, experiments, or causal formation wires, PROACTIVELY include a `graph` node naming the focal record",
  "  (`{ node: \"graph\", record: UUID, hops: 2 }`). Do not wait for the user to ask for the graph, and never isolate",
  "  it onto a separate full page: embedding it renders the interactive React Flow knowledge canvas with claim cards,",
  "  evidence badges, and formation lines directly inside the canvas stream alongside the other blocks.",
  "  The `graph` node auto-populates each block's title and description from the store — you do not author them.",
  "- **When YOU want to author the graph content, use `flow` instead of `graph`.** `flow` is the agent-authored",
  "  knowledge graph: you compose each block explicitly with items of your own writing. Use `flow` when the answer",
  "  demands analytical narrative per node — a causal explanation, a mechanism summary, a method description —",
  "  rather than a raw store readout. In a `flow`, every block MUST carry the full anatomy below; a bare-title",
  "  node is an empty shell that deprives the reader of context and is never acceptable.",
  "- **Knowledge graph node anatomy: title, short description, slot, and provenance.** Every block in a `flow`",
  "  MUST carry full substantive anatomy, never just a bare title:",
  "  1. **Slot badge (`K#1`, `K#2`, ...)**: Machine address identifying the block's reading rank in the fold.",
  "  2. **Title (heading or bold prose)**: Prominent 1–3 line headline stating the belief, question, method, or finding.",
  "  3. **Short description (prose item, 2–4 lines)**: Concrete technical substance the AI authors from the records —",
  "     empirical metrics, sample windows, mechanism specifics, ablation findings, or method steps.",
  "     A node without a short description is an empty shell that fails the reader.",
  "  4. **Provenance footer (chrome text)**: Record kind, sequence number, and evidence count.",
  "  5. **Labeled orthogonal wires**: Every wire between nodes carries an explicit relational verb (`supports`,",
  "     `came from`, `rests on`, `narrows`, `both rest on one run`) along orthogonal step paths.",
  "- **Mechanisms and spatial structures proactively generate visuals.** If a mechanism has geometric or",
  "  spatial form, draw it with `drawing` (inline SVG with coordinates and units). If a system has dynamic",
  "  simulation, state transitions, or 3D geometry requiring interactive controls or WebGL/Three.js, PROACTIVELY",
  "  author a self-contained HTML page, publish it via `POST /api/artifacts/content` (`format: \"html\"`), and mount",
  "  it with `embed` (`{ node: \"embed\", id: ARTIFACT_ID, title: \"...\", shows: \"...\" }`) without waiting to be asked.",
  "- **Deep technical substance, never superficial summaries.** Do not write a two-line high-level gloss and stop.",
  "  The canvas demands the full depth of technical evidence: exact parameters, statistical distributions (z-scores,",
  "  percentiles), drawdown durations, turnover percentages, and mechanism attributions across rich structured blocks.",
  "",
  "## Composing a component",
  "",
  "A container and the items inside it ARE the component you design — there is no second",
  "vocabulary of widgets to ask for. Each is catalogued below with the one rule that picks it.",
  "The app draws them in its ink, at the sizes its tests hold, and the bar for a new shape is",
  "the bar you would hold in a codebase:",
  "",
  "- **One component by default.** Split a surface into containers only when a piece genuinely",
  "  repeats and carries its own content — four blocks of the same shape, not two that happen to",
  "  look alike — a grid of boxes saying one sentence is not a design.",
  "- **Variations stay in the same composition.** A second reading of the same records is",
  "  another tier, another slot, another `chip` offering the other lens — not a new page with the",
  "  first one deleted. Name each variant so a reader can point at it: \"the middle one\" is not",
  "  an address, and the next turn of this conversation needs one.",
  "- **Nothing is invented because the shape is missing.** If no container holds what the records",
  "  say, name the gap or author the shape — a component with no counterpart in the records is an",
  "  invention a reader will trust and you cannot cite.",
  "- **Say what would look different.** Before emitting, name the one thing that would change if",
  "  this question were about another record. If nothing would change, you have generated a",
  "  template rather than an answer, and only you can see that.",
  "",
  "## Drawing a shape yourself",
  "",
  "`drawing` holds any geometry writable as SVG, in the same frame as the prose: no publish, no",
  "CDN. Read `graph` first: when the shape wanted is the records and what connects them, the",
  "store holds that already, and draws it truer than typed geometry can.",
  "",
  "- **A third axis carries the argument or it goes.** Records differing on a third field is not",
  "  a reason to draw it; a claim that only holds because of that field is. If hiding the axis",
  "  changes nothing a reader concludes, it is decoration, and the flat form reads better.",
  "- **One projection function, called by everything** — marks, ticks, stems and label anchors",
  "  alike. Offsets typed per label are how a caption runs off the viewBox.",
  "- **A coordinate the record never filed gets a stated place** — an edge, a band, a line",
  "  reading `not filed`. A mark at a guessed depth is an invented number.",
  "- **A drawing may move, and what will not is reported.** Which property a mark travels",
  "  through is yours to pick: a named trick here is a lookup, and two answers handed one",
  "  trick draw one page. What this renderer cannot run prints under it. State the period:",
  "  a sweep nobody can time is decoration.",
  "",
  "## The space you are given",
  "",
  "The lane is a fact before the first node is chosen: the pane's width the app holds, the",
  "item's height you name. Both of this surface's real failures are about space and not ink —",
  "a page filling a third of its lane and leaving the rest silent, and a page filling all of",
  "it with one idea per screen.",
  "",
  "- **Fill the lane, or shrink it.** Two short facts stacked read as two pages; in one `row`",
  "  they read as one. Spend the width on what is read together, and the full span on what must",
  "  be read alone — a chart, a table, a paragraph at its measure.",
  "- **High informational density: zero dead voids.** The canvas is an analytical instrument, not",
  "  an airy marketing page. Never leave massive blank margins or stack lone single lines with huge",
  "  vertical gaps. When presenting metrics, sweeps, or alternatives, pack the width with multi-column",
  "  `figure` tables (`NumTable`) or side-by-side `tier` cards (`lane: 36`–`45ch`). A lone chart stranded",
  "  with vast voids around it looks abandoned: flank or follow it with a telemetry table or comparative cards.",
  "- **Keep Chat lean; give the substance to the canvas.** Chat is an executive ticker (1–2 sentences",
  "  maximum). Never write markdown tables, multi-paragraph factor sweeps, or bulleted audit lists in Chat.",
  "  All data, tables, cards, and evidence graphs belong in the canvas composition.",
  "- **One largest thing.** Rank is position, measure and weight, not a heading ladder: the",
  "  biggest type on a surface carries this answer's weight, and there is at most one of it.",
  "  A page opening on its conclusion set enormous is a hero wearing a heading — it spends the",
  "  first screen saying one sentence and leaves nothing under it to look at.",
  "- **A figure answers in its caption.** One line on what the drawing settles. A caption that",
  "  restates the axes describes a picture instead of finishing it.",
  "- **Draw a small thing small.** A figure under a sentence belongs at that sentence's size:",
  "  an `embed` given the height its marks need, set between two items, not a tab of its own.",
  "  A page earns a whole tab when it has to run, step, answer a drag, or be pointed into.",
  "",
  "## A page you write yourself",
  "",
  "Nothing registered holds the shape — a mechanism that has to run, step, or answer a drag —",
  "so you write the page and the app hosts it. One publish, not a rebuild: post the HTML to",
  "`/api/artifacts/content` with `format: \"html\"`, an `Idempotency-Key` header and the",
  "`issue_id` it belongs to, take the `artifact_id` the response returns, and name it in an",
  "`embed` item with one line saying what it shows. Re-publishing with `previous_artifact_id`",
  "mints a NEW id and a `supersedes` edge: mount the newest.",
  "",
  "The frame is a sandbox and its policy is not negotiable; read it as the constraints that",
  "decide the code before you write any:",
  "",
  "- **Scripts run, in an opaque origin.** The page sees none of the app's storage, cookies or",
  "  DOM, and inherits no ink and no face from it: the page's own stylesheet is the only one.",
  "- **Nothing can be fetched, and nothing is nested.** `connect-src` is none and there are no",
  "  workers, so every number goes into the page as text, at the record's own precision.",
  "- **A library loads from five origins only** — cdnjs, jsdelivr's `/npm/`, unpkg, Tailwind's",
  "  and jQuery's CDNs — pinned to a version, or the page you checked is not the page a reader",
  "  gets. Its scene is built in the page, because nothing arrives after load.",
  "- **A link cannot leave the frame.** No top navigation is allowed, so an `href` at a record",
  "  address sits dead; cite by printing the kind and the id as text.",
  "",
  "What a first draft has to get right, because a seat never sees its own render:",
  "",
  "- **Copy the app's inks; never invent a palette.** Read the literals — background, raised",
  "  surface, the two hairlines, the reader's ink, the machine ink, the one accent — out of the",
  "  host's stylesheet and paste them at the digits it holds, never rounded toward a tidy number.",
  "  A page whose grey sits a hair off the surface hosting it reads as an advertisement pasted",
  "  into someone's instrument. Cannot read the stylesheet? Say so in the `embed` line.",
  "- **Fit the frame to the marks, from the marks' own coordinates.** A hand-typed view is how",
  "  six real coordinates arrive as three dots in a corner.",
  "- **Labels are document text over the canvas, not texture baked at mount** — a texture turns to",
  "  mush the moment the reader zooms.",
  "- **Print the figures as text beside the picture.** A canvas carries no semantics, so a screen",
  "  reader gets shapes and nothing else, and a CDN that fails to answer leaves a blank frame.",
  "- **Design for the lane you mount into.** The item's `height` is the frame's height: a page",
  "  drawn taller than the lane is cut, and the verifier fails a frame too short to read.",
  "",
  "A page is for geometry, never for content the answer holds: a composition wears the theme and",
  "carries the addresses a page cannot.",
  "",
  "## What the app owns",
  "",
  "These are the primitives a surface is assembled from. Their ink, glyph and spacing are",
  "fixed by the theme and tested; they are listed so you know what a field becomes on screen,",
  "not so you can ask for it.",
  "",
  ...owned,
  "",
].join("\n");

/** True when this file is the entry point, and so is being run to publish the brief. */
const isMain = import.meta.url === (process.argv[1] ? pathToFileURL(process.argv[1]).href : "");
if (isMain) {
  writeFileSync(briefPath, brief);
  const arms = [...shapes.values()].reduce((total, shape) => total + shape.arms.length, 0);
  console.log(`${briefPath} · ${Buffer.byteLength(brief)} bytes · ${docs.length} shapes · ${arms} choices · ${owned.length} primitives`);
}
