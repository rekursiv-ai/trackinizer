// THE LOOP. A question goes in, a composed canvas comes out.
//
// Until now every answer on this canvas was produced by a human pasting a long preamble
// — the law, the catalog, how to reach the graph, where to write — in front of a short
// question. That preamble is not the user's job and never was: it is the same every
// time, which is the definition of something the product should inject.
//
// So this is the whole product loop in one place. The researcher types a sentence. The
// context is attached here. The agent reads the law, queries the graph, chooses its own
// nodes, writes the composition, and checks its own work. Nothing about the ANSWER is
// decided in this file — only what every answer is entitled to know before it starts.
//
//   node scripts/ask.ts "what should I run next?"
//   node scripts/ask.ts "why did we make CodeChange#143?"
//   node scripts/ask.ts "experiment 344 — why does cosine's lead grow?"
import { spawn } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const web = resolve(here, "..");
const repo = resolve(web, "..");
const skills = join(repo, "trax", "docs", "skills", "trax");

/** Where the canvas reads its answer from. The route fetches this at runtime, so a new
 *  composition is live the moment it lands — no build, which is what makes the loop
 *  feel like a conversation rather than a deploy. */
const OUT = join(web, "dist", "blocks-latest.json");

/** The graph the lab actually runs on. Read from the environment so a different
 *  workspace is a different `TRACKINIZER_URL`, never an edit to this file. */
const GRAPH = process.env.TRACKINIZER_URL ?? "http://127.0.0.1:8765";
/** Where the SESSION syncs, which is the server the web app is talking to — that sync
 *  is what puts the agent's thinking, its bash calls and its tool rows in the chat
 *  column beside the canvas. It is separate from the graph URL because the records can
 *  live in one place and the open app in another; when they are the same server, these
 *  are the same value and nothing changes. */
const SESSION = process.env.ASK_SESSION_URL ?? GRAPH;
/**
 * The graph CLI: `TRAX_BIN` where it is named outright, else this checkout's own venv,
 * else PATH as the trax skill assumes. The venv step is what makes a fresh clone work
 * with no setup — `pip install -e .` puts trax there and nowhere else — and it is this
 * repo's own directory, not a guess about what sits beside it.
 */
const venvTrax = resolve(repo, "..", ".venv", "bin", "trax");
const TRAX = process.env.TRAX_BIN ?? (existsSync(venvTrax) ? venvTrax : "trax");

/**
 * The three documents every answer is composed against, INLINED rather than cited.
 *
 * They used to be passed as three file paths under "read these first", which is a
 * request, not a guarantee. Measured: an agent that already believes it knows the
 * catalog does not spend a tool call to re-read 45KB of it, and the failure is silent —
 * the composition validates, the page renders, and the reader gets prose where a figure
 * was the answer, built from a half-remembered subset of the nodes. The same goes for
 * the CLI skill: a run that guesses at trax grammar writes 422s instead of rows.
 *
 * So the loop no longer depends on the agent's judgement about whether to look. The
 * bytes are in the prompt. They are identical on every run, which makes them a cached
 * prefix rather than a per-run cost, and they are read from disk at spawn time, so an
 * edit to the catalog is live on the next question with no step in between.
 */
const DOCS = ["DESIGN.md", "LAW.md", "SKILL.md"] as const;

function brief(): string {
  return DOCS.map((name) => {
    const path = join(skills, name);
    // A missing doc is a broken install, not a degraded run: composing without the
    // catalog is exactly the outcome the inlining exists to prevent, so fail loudly
    // here rather than let the agent improvise twenty-eight nodes from memory.
    let text: string;
    try {
      text = readFileSync(path, "utf8");
    } catch {
      process.stderr.write(`ask: cannot read ${path} — the canvas brief is part of the loop\n`);
      process.exit(2);
    }
    return [`===== BEGIN ${name} (${path}) =====`, text.trimEnd(), `===== END ${name} =====`].join("\n");
  }).join("\n\n");
}

/**
 * Everything an answer is entitled to know before it starts.
 *
 * Deliberately NOT in here: which nodes to use, what the layout should be, what the
 * answer should say. Those are the agent's, and a preamble that decides them turns the
 * canvas back into a template with extra steps.
 */
function context(): string {
  return [
    "You are answering on a live research canvas.",
    "",
    "Compose your answer as JSON and write it to:",
    `  ${OUT}`,
    "Its envelope is a shape the brief defines; this file does not restate it.",
    "",
    "Three documents follow IN FULL, below, before the question. They are not references",
    "to go and fetch — they are already here, and they are the ones you compose against:",
    "DESIGN.md is generated from the app's own source (every node the catalog registers,",
    "the library behind each one, its exact fields, and when to pick it over its",
    "neighbour); LAW.md is the method for arriving at an answer, and it carries no",
    "settings, because how a pane looks is the app's decision, already made in its CSS;",
    "SKILL.md is the trax grammar you will write the graph with. Read all three in full",
    "in THIS turn, before you compose — not from memory of a previous run. Which nodes",
    "answer this question is your call, and the call is the work. An entry not in the",
    "brief cannot be asked for.",
    "",
    "The lab's knowledge graph is live and is ALWAYS the context — issues, beliefs,",
    "experiments, papers, code changes, agent sessions, and the valenced edges between",
    "them. The researcher will not ask you to consult it; they will just ask their",
    "question. Discover it yourself:",
    `  export TRACKINIZER_URL=${GRAPH}`,
    `  ${TRAX} --help            # every kind, every verb, every field`,
    "Edges carry a valence; a NEGATIVE one argues against the claim.",
    "",
    "Whether a surface can be interrogated — brushed, opened, turned — is a property of",
    "the catalog entry that draws it, so the brief answers that and this file does not.",
    "A surface the reader can interrogate beats one they can only read, whenever the",
    "interaction is the subject's own behaviour and not decoration.",
    "",
    // Measured: a run with the whole catalog inlined still spent ten turns and ~15k output
    // tokens grepping `compose.tsx`, reading `drawing.tsx` and hunting `ChartView` in
    // `chart.tsx` to find out how a node draws — the exact files DESIGN.md is generated
    // from. Output is sequential and is the loop's whole cost, so a turn that re-derives a
    // documented field is minutes the researcher waits for nothing.
    "DESIGN.md IS GENERATED FROM THOSE COMPONENTS AND IS AUTHORITATIVE ON THEM. Do not open",
    "`src/ui/` to learn what a node draws, what fields it takes or how it renders — the",
    "answer is already below, and `web/src/ui/brief.test.ts` fails the build when the two",
    "disagree. Read the source only when you intend to CHANGE it, or when the brief and a",
    "render you are looking at genuinely contradict each other — then say which won.",
    "",
    // Same measurement: `lsof` to find the port, two reads of `verify-answer.ts` to learn
    // its contract, and a read of a previous answer to infer the envelope. All three are
    // known here at spawn time and cost nothing to state.
    "The environment, so no turn is spent discovering it:",
    `  the server serving this app and the graph:  ${GRAPH}`,
    `  the composition you write:                  ${OUT}`,
    `  the graph CLI:                              ${TRAX}`,
    `  the verifier, which takes a png path:       cd ${web} && node scripts/verify-answer.ts /tmp/ask.png`,
    "The verifier prints what it found and exits non-zero on a fault; it needs no reading.",
    "A previous composition on disk is a previous answer, not the schema — the envelope is",
    "in the brief below, and copying an old file's shape copies its mistakes too.",
    "",
    "Use only real records and real numbers. Never invent a ref, a figure or a cost. If",
    "you compute something, say it was computed and show the formula. Label measured",
    "versus computed.",
    "",
    "Then run the verifier named above, fix whatever it reports, and re-run until clean.",
    "Then look at /tmp/ask.png yourself, as a reader who did not write it: the render is",
    "what was claimed, and the JSON always looks right. Name what is off and fix it.",
    "",
    "Do NOT run `npm run build`: the canvas fetches the JSON at runtime.",
  ].join("\n");
}

const question = process.argv.slice(2).join(" ").trim();
if (question === "") {
  process.stderr.write('ask: say something — node scripts/ask.ts "what should I run next?"\n');
  process.exit(2);
}

// Order matters. The docs sit BETWEEN the instructions and the question: the agent is
// told what it is composing, gets the catalog and the law and the grammar in full, and
// only then sees the sentence it has to answer — so the question is read by something
// that already holds the twenty-eight nodes, rather than by something that will decide
// later whether to go and look them up.
const docs = brief();
const prompt = `${context()}\n\n${docs}\n\n[THE RESEARCHER ASKED]\n\n${question}\n`;

/**
 * The thinking budget, which is what this loop's latency actually is.
 *
 * Measured on a question with nothing to run — no page to build, no computation, no graph
 * read, permissions bypassed so nothing blocked: 843s wall, 133,121 output tokens, of
 * which ~8,600 were the tool calls and prose and the other ~124,500 were thinking. Input
 * was 59 tokens; the whole 110KB brief came from cache. So the wait is not fetching, not
 * context, and not the brief — it is the model thinking fifteen times more than it writes,
 * and output is generated serially.
 *
 * A composition needs a plan, not a treatise. `ASK_THINKING` raises it back for a question
 * that earns it; `0` turns extended thinking off entirely.
 */
const THINKING = process.env.ASK_THINKING ?? "8000";

process.stderr.write(
  `[ask] ${question}\n[ask] graph ${GRAPH}\n[ask] brief ${DOCS.join(" + ")} inlined, ${docs.length} chars`
  + `\n[ask] thinking budget ${THINKING}\n`,
);
const started = Date.now();

// Routed through `trax run claude`, not a bare `claude` spawn. That wrapper is what
// makes this an IDE rather than a script: it captures the session and syncs it to the
// server, so the same answer lands in the chat thread AND on the canvas, under one
// permission model, with the run itself recorded as an AgentSession the graph can cite
// later. A bare spawn produces a canvas with no conversation attached to it.
//
// Set ASK_DIRECT=1 to bypass the wrapper — useful when the server is down and you only
// want the canvas.
// The answer needs three trees: this app (to write and verify), the skills dir (the
// law and the catalog), and the checkout that owns the graph CLI. A run scoped to
// `web/` alone cannot read its own law — measured, not assumed: the first run of this
// script was denied on every graph call AND on LAW.md, and correctly refused to
// substitute stale fixtures for live records.
// A second checkout is a local arrangement, not the shape of the product: name it with
// `ASK_ADD_DIR` where one exists, and the run is scoped to this repo alone where it does
// not. A default pointing at a sibling directory works on one machine and nowhere else.
/**
 * ASK_FAST=1 — one call, no tools, no loop.
 *
 * The loop's cost is turns, not context: measured on a question with nothing to run, 31
 * turns and 133k output tokens for a 17KB answer, 843s wall. Capping thinking took that
 * to 461s and no further — because the remaining time is the agentic cycle itself,
 * generate → run → read → generate, which is serial by construction.
 *
 * A question that needs no tool does not need that cycle. Here the agent gets the brief
 * and the question, has no tools to call, and emits the composition as its reply, which
 * this file writes. No bash, no verifier, no re-reading its own output. What it gives up
 * is real and is the whole trade: nothing is measured, nothing is looked up in the graph,
 * and nobody looks at the render before the reader does. For "what does the store say"
 * or "run this and show me", take the loop.
 */
const fast = process.env.ASK_FAST === "1";

const extra = process.env.ASK_ADD_DIR;
const claudeArgs = fast ? [
  "--print",
  // No `--allowedTools`: with none granted the model answers from the brief and the
  // question alone, which is the point — a tool call here would be a turn, and turns are
  // the thing being removed.
  "--permission-mode", "default",
  `${prompt}\n\nThere are no tools on this run. Reply with the composition JSON and nothing\n`
  + "else: no preamble, no fences, no commentary. It is written to disk exactly as you\n"
  + "send it, so anything that is not JSON breaks the page.\n",
] : [
  "--print",
  "--add-dir", repo,
  ...(extra ? ["--add-dir", extra] : []),
  // The answer has to RUN two things: the graph CLI, and the verifier that checks its
  // own work. `--add-dir` grants reads, not exec — measured: a run with the dirs added
  // still had every `trax` call denied, and said so rather than composing from stale
  // fixtures. These two are the whole exec surface an answer needs.
  "--allowedTools", `Bash(${TRAX}:*)`, "--allowedTools", "Bash(node:*)",
  "--permission-mode", process.env.ASK_PERMISSION ?? "acceptEdits",
  prompt,
];

const direct = process.env.ASK_DIRECT === "1";
const [bin, args] = direct
  ? ["claude", claudeArgs]
  : [TRAX, ["run", "--as", process.env.ASK_ACTOR ?? "Canvas", "claude", "--", ...claudeArgs]];


const child = spawn(bin, args, {
  // In fast mode the reply IS the answer, so stdout is captured and written here; in the
  // loop the agent writes the file itself and stdout is its running commentary.
  stdio: ["ignore", fast ? "pipe" : "inherit", "inherit"],
  cwd: web,
  // The wrapper syncs the session to SESSION; the agent's own graph calls name GRAPH
  // explicitly in the context above, so the two never fight over one variable.
  env: { ...process.env, TRACKINIZER_URL: direct ? GRAPH : SESSION, MAX_THINKING_TOKENS: THINKING },
});

let reply = "";
child.stdout?.on("data", (chunk: Buffer) => { reply += chunk.toString(); });

child.on("exit", (code) => {
  const took = ((Date.now() - started) / 1000).toFixed(1);
  if (fast && code === 0) {
    // A model told to send only JSON still sometimes wraps it in a fence, so the brace
    // span is taken rather than trusted. Parsed before writing: a half-written answer on
    // disk is a broken page, and the error belongs here where it can be read.
    const body = reply.slice(reply.indexOf("{"), reply.lastIndexOf("}") + 1);
    try {
      writeFileSync(OUT, JSON.stringify(JSON.parse(body)));
    } catch (error) {
      process.stderr.write(`[ask] the reply was not a composition: ${String(error)}\n`);
      process.stderr.write(reply.slice(0, 400) + "\n");
      process.exit(3);
    }
  }
  process.stderr.write(`\n[ask] ${code === 0 ? "composed" : `failed (${code})`} in ${took}s → ${OUT}\n`);
  process.exit(code ?? 1);
});
