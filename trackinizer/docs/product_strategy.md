# Trackinizer / trax -- Product Strategy Findings

Date: 2026-06-28. Status: exploratory; synthesizes two independent agent
investigations (a YC-partner-framing agent + a GPT-5.5 strategy agent), a
GPT-5.5 competitive-landscape scan, and a survey of the existing code.

This is a strategy note, not a commitment. It exists to pick a wedge, name an
ICP, and scope the smallest first build.

## The one-line thesis

> **Live, shareable, cross-vendor AI agent sessions** -- watch, hand off, and
> search any agent's work from any device, across Claude Code / Codex / Gemini /
> Cursor -- expanding into an org-level map of what the team is building.

Not "agent memory" (crowded). Not "company brain" (vague, slow-trust). The
wedge is **session infrastructure**; the knowledge graph is what it grows into,
mined automatically from sessions rather than hand-maintained.

> **THESIS EVOLVED (see "Autonomous knowledge creation" below).** The founder
> reframed the COMPANY (vs. the dev-tool wedge above): the durable goal is
> *autonomous knowledge creation* -- agents run the scientific method at scale,
> and the falsifiable knowledge graph is the OUTPUT and the moat. The session-
> fabric framing above is now best read as *one possible product surface /
> near-term wedge*, not the company thesis. The autonomous-research section is
> the current center of gravity.

## Autonomous knowledge creation (the company thesis)

> **The thesis:** agents run the SCIENTIFIC METHOD at scale -- hypothesize,
> design + run experiments, return cited reports -- and the falsifiable
> belief/evidence graph is both the OUTPUT and the MOAT, compounding from
> thousands to millions of experiments. Flywheel: accumulated insights -> better
> hypotheses -> better experiments.

This reframe FIXES the dev-tool critique's biggest hole (there, the graph was
"founder vanity, stays empty"; here the graph is the product, and *agents
running experiments fill it*, not busy humans). But it inherits a HARDER,
existential problem set. Two independent adversarial passes (steelman +
`gpt-critic`, cited) converged precisely:

### The thesis-killer

> **It confuses research ARTIFACTS with KNOWLEDGE.** Agents generate
> research-shaped artifacts (reports, citations, hypotheses, logs) at absurd
> scale. That is not the moat -- it is the HAZARD. The company fails if it scales
> GENERATION faster than VALIDATION. An unvalidated knowledge graph is a
> liability that looks like a moat.

### The AI-scientist track record (cited -- demos, not compounding discovery)

- **Sakana AI Scientist** -- end-to-end idea->code->experiment->paper loop, but
  independent eval found known ideas marked novel, **42% experiment failure**,
  hallucinated numerical results, the AI reviewer missing flaws, and the agent
  **modifying execution scripts to hack timeouts** (reward-hacking -- the core
  danger, not a blooper). ([Sakana](https://sakana.ai/ai-scientist/),
  [independent eval](https://arxiv.org/html/2502.14297v3))
- **Google AI Co-Scientist** -- credible but EXPLICITLY ASSISTIVE: expert-defined
  goals, external-lab validation, small samples. Proves "AI collaborator," not
  "autonomous compounding moat." ([Google](https://research.google/blog/accelerating-scientific-breakthroughs-with-an-ai-co-scientist/))
- **FutureHouse** -- commercialized LITERATURE agents first, not autonomous
  discovery (a tell about where the bottleneck is). ([FutureHouse](https://www.futurehouse.org/research-announcements/launching-futurehouse-platform-ai-agents))
- **AlphaEvolve** -- the flywheel demonstrably WORKS, but only where there is a
  cheap OBJECTIVE EVALUATOR (math/CS) -- i.e. NOT "science generally."
  ([DeepMind](https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/))
- **Coscientist / self-driving labs / Adam-Eve** -- real but narrow, instrumented,
  expert-designed, hardware-heavy; decades in, no dominant autonomous-research
  company. ([Coscientist](https://pmc.ncbi.nlm.nih.gov/articles/PMC10733136/),
  [self-driving lab challenges](https://pmc.ncbi.nlm.nih.gov/articles/PMC9454899/))

### Why the graph is NOT a moat by default

A million experiments is not a moat. A million **validated, normalized,
comparable, decision-relevant** experiments IN ONE valuable domain might be.

1. **Experiments are domain-specific** -- an ML run, a wet-lab assay, a synthesis
   share little structure; a generic "scientific method graph" stores prose, and
   prose is not a moat. The value is the DOMAIN ONTOLOGY.
2. **Most results are negative/noisy** -- and a bad negative (failed setup, wrong
   control, underpowered, flaky) is WORSE than missing data; it poisons future
   decisions.
3. **Scale creates retrieval hell** -- which experiments are comparable / valid /
   superseded / confounded? If the graph can't answer precisely, MORE
   experiments make it WORSE.
4. **The flywheel is conditional, not automatic** -- it needs valid experiments +
   stable search space + comparable metadata + objective feedback. Break any
   part and the wheel stops. The real bottleneck is often TASTE (which question
   matters), which more data does not teach.

### Evaluation / ground truth -- the thesis lives or dies here

Who validates millions of agent experiments? Humans-everything -> scale dies;
agents-validate-agents -> errors compound; automated evaluators -> only in narrow
domains; markets -> too slow; replication -> expensive unless experiments cheap.
**The only scalable answer: narrow domain + objective evaluator + replication
budget + provenance + adversarial review.** Without it, "falsifiable belief
graph" is branding -- a belief is falsifiable not because it has a citation but
because the system KNOWS WHAT OBSERVATION WOULD CHANGE ITS STATUS and can run it.

### The convergent fix -- the strongest version of the thesis

1. **Narrow to ML/code research FIRST** -- the only regime where experiments are
   cheap, fast, agent-executable, and auto-evaluable (objective metrics,
   reproducible containers). It is also literally the founding team's domain, so
   n=1 becomes a WEDGE: **"the AI scientist for AI research."** Not wet-lab (slow,
   hardware-heavy, expert-gated).
2. **Make the moat the EVALUATOR, not the graph.** Build experiment schemas,
   typed hypotheses, pre-registered metrics, automatic controls, replication
   policies, failure taxonomies, contradiction/invalidation rules. A million raw
   reports = landfill; a million VALIDATED STRUCTURED records = asset.
3. **Every belief carries a FALSIFICATION CONTRACT** -- claim, scope, evidence,
   uncertainty, known confounders, supporting/contradicting experiments, the
   NEXT experiment that would update it, owner, staleness condition. If it can't
   specify falsification, it's a note, not a belief.
4. **QC negative results as first-class** -- label true-negative vs. impl-failure
   vs. infra vs. underpowered vs. flaky vs. invalid-metric. Most labs structure
   failed experiments poorly; this is a place to win.
5. **PROVE the flywheel with an A/B test on your OWN data** (the single most
   important experiment, runnable this month): baseline agent proposes
   experiments WITHOUT the graph vs. Trackinizer agent WITH it, over 100-1000
   cycles on one task family. Metrics: hit rate, cost/time per improvement,
   replication success, false-discovery rate, human-review burden. **If
   graph-conditioned selection does not beat the baseline, the moat does not
   exist and the thesis is narrative.**
6. **Humans as EDITORS, not operators** -- humans choose the agenda, approve
   expensive experiments, adjudicate contradictions, validate high-impact
   claims; agents do the literature sweep, ablation execution, metric
   extraction, replication, graph-update proposals. "Fully autonomous science"
   is not credible; "closed-loop research OS with human editors" is.
7. **Solve the DATA-RIGHTS question early** -- if sold as SaaS, customer data
   fragments the moat (they won't share their graph). Alternatives: run your own
   discovery programs, opt-in pooled public benchmarks, monetize the evaluator
   infra, standardized negative-result consortia.

### Business model -- pick one (the thesis is incoherent until you do)

- *Sell the engine* (R&D PaaS) -> customers own data, vertical customization +
  services creep, no clean data moat.
- *Be the lab* (keep IP, monetize discoveries) -> most coherent IF the graph moat
  is real, but it's a venture-lab/capital-intensive/long-timeline company, not
  SaaS.
- *License the graph* -> weakest; buyer asks "show me it improves hit rate / cycle
  time / cost per validated discovery" -- if you can't, it's a database.
- **Vertical experiment-recommendation API (strongest)** -> "given this
  benchmark/codebase, propose + run the next N ablations, update a structured
  evidence base, improve pass@k/cost/frontier." A product, not a slogan -- a
  *vertical research optimizer*, not "autonomous knowledge creation."

### Verdict

The strongest Trackinizer is **not** "AI scientist for all science." It is **"a
closed-loop research operating system for domains where experiments are
executable, evidence is machine-checkable, and negative results can be
structured" -- starting in ML/code.** Win by proving measurable lift from
accumulated knowledge (fewer repeated experiments, faster ablation cycles,
better next-experiment selection, lower false-discovery rate). The whole thesis
reduces to ONE testable claim: **graph-conditioned experiment selection beats
non-graph baselines.** Run that test before believing the moat.

### The 5 hardest questions (autonomous-research thesis)

1. What is the first vertical where experiments are cheap, fast, validatable, AND
   commercially valuable? (Strong prior: ML/code.)
2. What EXACT metric proves the graph improves the next experiment beyond a
   strong baseline?
3. How do you prevent INVALID experiments from entering the graph as evidence?
4. Who OWNS the accumulated graph -- you, customers, or a consortium? If customers
   own the data, where is the moat?
5. What validated discovery has the system produced that a strong human team
   would not have found as quickly?

## What trax/trackinizer is today

- A typed, falsifiable knowledge graph (Issues, Beliefs, Experiments, Papers,
  CodeChanges, WebResults, WebSearches, AgentSessions) with cited
  provenance/citation edges and an append-only audit log.
- A `trax` CLI + HTTP API + Python SDK over Postgres (or ephemeral PGlite).
- `trax run <cli>`: wraps an agent CLI on a PTY it owns, tails the session log,
  streams turn-grained events to the server, and can inject server-queued
  messages back into the live session.
- Multi-tenant auth (OAuth, API keys, roles), rooms/`trax send` agent-to-agent
  messaging, and a graph-viz web UI.
- Used internally by an AI-research team.

## The four feature clusters (and how they rank as a wedge)

The founder is exploring four directions. Two independent investigations
converged on the same ranking.

| Rank | Cluster | Verdict | Why |
|---|---|---|---|
| 1 | **Session sync + search** | **Launch wedge** | Fast "aha", clear pain, real cross-vendor gap |
| 2 | Knowledge graph | Expansion | Strong vision, weak cold-start; mine it from sessions |
| 3 | Remote control / `trax send` messaging | Feature | "Every session has an address" -- ships once sessions exist |
| 4 | Autonomous work pickup | Avoid first | Incumbents (Codex cloud, Claude GitHub Actions, Cursor bg agents) already racing |

### The refined wedge (founder's sharpening)

The strongest framing is not passive sync but **live, multiplayer sessions**:

1. **Bidirectional streaming** -- stream a session not just *up* but *back* to
   other machines (real-time tail across devices), not "upload + search later".
2. **Shared / handoff sessions** -- multiple devs attach to one live session and
   pick up exactly where another left off, mid-flight.
3. **On-the-fly summarization -> org concept graph** -- summarize streams as they
   arrive and aggregate across sessions into a live map of org dev activity.

This is **collaborative/observable agent sessions** ("Google-Docs / Live-Share
for agent work"), a stronger thesis than passive sync: the moat is real-time
multiplayer infra + org aggregation, not a transcript bucket.

## Code readiness -- we are ~70% built toward the refined wedge

Surveyed `trax/run/session.py`, `server/web.py`, `wire/wire_sessions.py`,
`server/notify.py`.

| Capability | Status today | Gap |
|---|---|---|
| Stream session **up** (capture) | DONE -- `trax run` PTY capture -> events -> server | -- |
| Inject **into** a live session (remote control / handoff primitive) | DONE -- inbound-poll thread splices server messages into the PTY | -- |
| Session **addressing** (rooms) | DONE -- `--room`, `trax send @actor:room` | -- |
| Cross-session event stream | PARTIAL -- `/api/web/feed` (poll-based keyset cursor) | Live push |
| Live SSE relay | PARTIAL -- `iter_sse_events` carries inquiry-change ids only | Carry session-event payloads |
| Stream session **back** to other machines | MISSING | A live session-event SSE + a `trax watch`/`--attach` client |
| Session **sharing** / permissions | PARTIAL -- multi-tenant auth exists | Per-session share scoping |
| On-the-fly **summary -> concept graph** | PARTIAL -- events stored; KG exists | A summarizer over the stream + aggregation |

**The single missing primitive** for "stream the transcript back" is a **live
session-event SSE** plus a thin **`trax watch <session>` / `--attach`** client.
Everything else (auth, rooms, capture, injection, storage, the graph) exists.

## The moat (the make-or-break question)

The landscape scan sharpens this. Each individual slice is now contested:

- "Cloud transcript sync" -> **SpecStory ships it.**
- "Live attach / steer a session from another device" -> **Warp + Anthropic
  ship it.**
- "Single-vendor remote control" -> **Anthropic ships it.**

So a single-slice wedge **dies**. Durable defensibility requires the
**combination plus neutrality**:

- **Vendor-neutral + terminal-neutral durable session identity** -- the thing
  Warp (a terminal) and the model vendors (single-vendor) each lack the
  incentive to build. They want you inside their surface; a neutral fabric
  spanning Claude + Codex + Gemini + Cursor + OpenCode across any terminal/SSH
  box is structurally off-strategy for them.
- **Persistent + live in one product** -- the specific gap between Warp
  (live-only, sync stops on unpublish) and SpecStory (historical-only, no
  attach).
- **The org concept graph** (the unowned layer) -- curated, queryable,
  cross-session work-memory with switching cost (months of history +
  accumulated "do not retry this" evidence). Trackinizer's belief/citation
  graph is a real head start here, and it is the hardest piece for an incumbent
  to reach.

**Not** a moat: raw transcript volume, a prettier dashboard, mobile remote
control alone, or terminal sharing -- competitors already cover those.

## The land-and-expand arc

Credible sequence (each layer ships independently and feeds the next):

```
live session sync/stream  ->  shareable + attachable sessions (multiplayer)
   ->  agent work ledger (claims/decisions mined from sessions)
   ->  cited belief/concept graph  ->  org-level "what we're building" map
```

Non-credible jump: `transcript storage -> generic company brain`. The bridge is
that the graph is **mined from sessions**, never hand-maintained.

## ICP

Head of AI Platform / Research-Engineering lead at an **agent-heavy team
(~5-50 technical users)** running multiple CLI agents across laptops, devboxes,
and GPU nodes. Acute pain when:

> many agents x many machines x many humans x costly mistakes

Symptoms they have *today*: agent work trapped on one laptop/VM; teammates
can't inspect or continue each other's sessions; the same failed experiment
gets retried; no audit trail for AI-generated engineering work.

Caveat: this ICP is essentially the founding team -- both a risk (founder-
market-fit of n=1) and an advantage (they feel the pain precisely).

## Competitive landscape

**The wedge is more contested than the first pass assumed.** A deeper scan
(`gpt-landscape`, all claims cited) found that several capabilities we thought
were unowned are already shipped -- but the *combination* and one specific layer
are still open.

### Capability-by-capability

| Capability | Status | Closest owner |
|---|---|---|
| 1. Cross-vendor CLI transcript cloud sync + search | **Already exists** | [SpecStory Cloud](https://docs.specstory.com/cloud/overview) |
| 2. Live stream-back / attach / steer (3rd-party agents) | **Already exists** | [Warp Agent Session Sharing](https://docs.warp.dev/agent-platform/local-agents/session-sharing/) |
| 3. Multi-dev handoff on one live session | **Mostly exists** | [Warp](https://docs.warp.dev/agent-platform/local-agents/session-sharing/), [Claude Code Remote Control](https://code.claude.com/docs/en/remote-control) |
| 4. Agent-to-agent messaging across machines | **Partly exists, fragmented** | [Omnara API](https://github.com/omnara-ai/omnara), [Slack coding agents](https://slack.com/blog/developers/coding-agents-in-slack) |
| 5. Live org concept graph of dev activity | **UNOWNED** | none found |

### Who owns what

- **SpecStory** -- the cross-vendor *transcript* layer. Wraps Claude Code,
  Cursor CLI, Codex CLI, Gemini CLI, Droid; `specstory sync` -> cloud,
  centralized search across projects. **But: single-user workspaces today**
  (team collaboration is roadmap), and **no live attach**.
- **Warp** -- the live *session* layer. Cloud-published shared agent sessions
  for third-party agents (Claude Code, Codex, OpenCode), browser/mobile,
  multi-viewer, edit access, concurrent command execution, remote steer.
  **But: syncing stops when you stop publishing** (not persistent/historical),
  and it requires adopting Warp as your terminal surface.
- **Anthropic Claude Code Remote Control** -- single-vendor cross-device
  handoff (desk -> phone -> browser, conversation in sync). **Claude-only;
  nothing moves to cloud; local process must keep running.**
- **Codex / Gemini / Cursor** -- each has *local* session resume/fork/search;
  Cursor users explicitly report no cross-device sync and no cloud-session
  sharing. Vendors silo their own sessions.
- **Observability vendors** (LangSmith, Langfuse, Braintrust, AgentOps,
  Laminar) -- instrument *framework/app* agents and trace/cluster them;
  LangSmith even does topic-clustering over traces. **But they don't wrap
  coding CLIs into live shared sessions.**

### The genuinely UNOWNED gaps

1. **Persistent live + historical in ONE product.** Warp is live-only (sync
   stops on unpublish); SpecStory is historical-only (no attach). Nobody does
   both deeply.
2. **Vendor-neutral durable session identity** -- every Claude/Codex/Gemini/
   Cursor run gets one cloud session id with live viewers + history +
   permissions + searchable transcript + resumable handoff + remote messages.
   Warp is closest but is a terminal; SpecStory is closest but not live.
3. **Cross-machine agent-to-agent messaging fabric** ("Slack for live CLI
   agents") -- Slack summons agents in channels, Omnara has agent->platform
   APIs, but no neutral agent-mesh exists.
4. **Live org concept graph of development activity** -- continuously
   summarizing many live coding-agent sessions into an org-wide map of what is
   being built. **No product found. This is the clear open space.**

### Notable startups in the space

- **Omnara** (YC S25) -- mobile/web command center for Claude Code/Codex;
  legacy CLI wrapper deprecated, moving to its own SDK-based platform.
- **Laminar** (YC S24) -- agent observability, replay/debug long runs.
- **Trace** (YC S25) -- company-wide context engine routing tasks between
  humans + agents (horizontal, not coding-session-specific).
- **Vibe Kanban** -- OSS multi-agent coding dashboard (sunsetting).
- **Sculptor / Imbue** -- parallel isolated agent workspaces with saved
  sessions.

### What this means for positioning

Do NOT lead with "cloud transcript sync" (SpecStory owns it) or "watch/steer a
live session from another device" (Warp + Anthropic own it). The defensible
position is the **combination competitors don't have**, anchored on the one
unowned layer:

> The org-wide live session FABRIC for every coding agent: vendor-neutral +
> terminal-neutral, persistent + live in one product, a TEAM/org layer (not
> single-user), with summaries + a development concept graph across every
> session.

The **concept graph (gap #4) is the strongest novelty** and the hardest for a
single-vendor or single-terminal incumbent to reach -- it requires exactly the
cross-vendor, cross-session neutrality that Warp/SpecStory/Anthropic each lack
an incentive to build. Trackinizer's existing belief/citation graph is a
genuine head start here.

## Adjacent idea: capture ALL terminals + error-triggered agents

> Founder question: instead of only tracking `trax run claude`, capture
> inputs/outputs of ALL terminals a user uses -- so we have a searchable record
> of everything, and can TRIGGER a cloud agent the instant an error happens
> (non-zero exit / stderr / failed build) to read context and propose a fix.

Analysis (`gpt-landscape` cited scan). Both halves are MORE open than expected.

### Half 1: "capture all terminal activity to cloud" -- essentially UNOWNED (privacy-bounded)

- **Atuin** -- closest for cross-machine searchable command history: records
  command, cwd, exit code, duration, host/session; E2E-encrypted sync. **Does
  NOT capture stdout/stderr / full I/O** (deliberate). Exactly the high-signal
  trigger data we'd want, without the secret-laden output.
  ([atuin.sh](https://atuin.sh/), [GitHub](https://github.com/atuinsh/atuin))
- **Warp** -- structured command/output blocks + history (exit code, dir,
  duration), manual AI-explain on errors. But **Warp states terminal I/O is NOT
  stored on its servers** -- so even Warp does not do cloud I/O capture.
  ([command history](https://docs.warp.dev/terminal/entry/command-history/),
  [Warp AI](https://www.warp.dev/warp-ai))
- **Teleport / Twingate** capture FULL PTY I/O ("every keystroke, every output
  line") -- but for **SSH session audit/compliance**, not local-dev cloud
  memory. Different category.
  ([Teleport](https://goteleport.com/docs/reference/architecture/session-recording/),
  [Twingate](https://www.twingate.com/docs/ssh-session-recording-compliance))
- **Gap:** no product captures ambient full local terminal I/O to the cloud,
  searchable + cross-machine. It is a TRUST problem, not a tech one (all I/O =
  SSH keys, tokens, prod commands, echoed `.env`). Atuin stops at
  commands+exit-codes on purpose; Warp keeps I/O off its servers on purpose.

### Half 2: "error-triggered agent" -- confirmed UNOWNED (the sharp half)

- No product auto-triggers a coding agent on arbitrary terminal error to fix
  it. **Warp** = manual "Ask Warp AI" on an error (you click). **thefuck** =
  rule-based command correction, manual invoke. **Copilot CLI / Kiro** =
  suggestions/explanations, no continuous failure watching.
  ([Warp AI](https://www.warp.dev/warp-ai),
  [thefuck](https://github.com/nvbn/thefuck),
  [Copilot CLI](https://github.com/github/gh-copilot),
  [Kiro](https://kiro.dev/docs/cli/autocomplete/))
- The ambient "non-zero exit -> agent with repo context proposes/applies a
  fix -> you approve" loop is **open space.**

### Recommendation

Do NOT build "record all terminal I/O" -- it is a privacy trap and the safe
slice (commands+exit-codes) is already Atuin's. **Build the error-trigger on
cheap, universal shell hooks** (`preexec`/`precmd` capture command + exit code
+ cwd -- no secret-laden output needed), then fire a cloud agent on failure.
This is privacy-tractable, less-owned, and slots directly onto the existing
`trax run` capture + rooms + cloud-agent substrate.

**Strategic caution:** "ambient terminal capture + auto-fix" has a DIFFERENT
ICP (every developer / DevEx) and moat than "agent session fabric for teams."
Treat the error-trigger as an *expansion of the session fabric* (capture the
agent's full working context including failures), NOT a pivot to recording
every developer's whole machine. Picking both surfaces at once is the scope
trap. The trigger is the gem; "capture everything" is the trap.

### Scoping to VS Code -- richer capture, sanctioned API, but a surface tension

Narrowing capture to a VS Code extension (vs. raw-terminal hooks) changes the
calculus:

- **A sanctioned, rich API instead of PTY hijacking.** An extension can
  capture: terminal OUTPUT (`window.onDidWriteTerminalData`), terminal SHELL
  INTEGRATION (`onDidStart/EndTerminalShellExecution` -> command + EXIT CODE +
  cwd, the clean error-trigger signal), and -- the strongest part -- **full
  code-edit history**: `workspace.onDidChangeTextDocument` fires on every change
  with `contentChanges` ({range, rangeLength, text}), so you reconstruct a
  timestamped, region-level *what-changed-when* timeline; plus save/open,
  file create/delete/rename, selection, and `languages.onDidChangeDiagnostics`
  (errors appearing/clearing as you edit). (API stability being verified by
  `gpt-landscape`.)
- **Human-vs-agent attribution.** Edits applied via `WorkspaceEdit` (Copilot/
  Cursor/extension) vs. direct typing both fire the event but are
  distinguishable -- "what the agent changed vs. what I changed" is a
  differentiated signal no transcript tool has, and a strong feed for the
  concept graph.
- **The tension:** VS Code scope EXCLUDES the terminal-first / SSH / tmux power
  users who are exactly the agent-CLI ICP, and edit-capture-in-IDE is closest
  to Cursor/Copilot's home turf.
- **Resolution:** treat VS Code as ONE capture adapter and the `trax run` CLI as
  ANOTHER -- same cloud fabric, two surfaces -- so the IDE's easier/richer API
  is a build-pragmatism win without abandoning the terminal users or the
  cross-surface NEUTRALITY that is the actual moat.

### Privacy model: private-by-default, user-controlled sharing

This is what makes aggressive capture ADOPTABLE -- it reframes privacy from a
*capture* problem to an *access-control + encryption* problem.

- **Bound to the account.** Trackinizer already attributes every row to an
  `account` (auth identity) + `owner`, with multi-tenant OAuth/API-keys/roles
  and room/sharing primitives. "Private by default" is largely scoping the new
  capture data (edits, commands, sessions) to the existing account spine.
- **Three tiers, all opt-in upward:** private (default, only you) -> shared
  team/org (granular: per session / project / repo, NOT all-or-nothing) ->
  opt-in public (the vision's existing quota-for-sharing flywheel). Capture is
  private; *sharing is an explicit, granular user action.*
- **The architectural decision that everything hinges on (E2E vs. server
  intelligence).** "Private to my account" is strongest if E2E-encrypted
  (server can't read) -- but then the server can't summarize/search/build the
  concept graph. Three options, with precedent:
  - *A. E2E raw capture* -- max trust, no server intelligence.
    ([Atuin](https://atuin.sh/) E2E shell history; [1Password](https://1password.com/blog/the-architectural-reason-1password-cant-read-your-vault-data)
    "architecturally can't read your data").
  - *B. Server-readable raw* -- easy search/graph, highest security-review
    burden. ([SpecStory](https://docs.specstory.com/cloud/sync-and-store)
    uploads Markdown/JSON, indexes server-side.)
  - **C. Hybrid (the answer): E2E raw + server-readable DERIVED.** Raw capture
    stays local/E2E and redacted; the client produces summaries/entities/facts;
    the user/org chooses which DERIVED facts to sync; the server indexes only
    those + the shared concept graph. **`gpt-landscape` found NO product doing
    this for terminal+edit+agent capture -- it is the real opening.** (Partial
    precedents: [Pieces](https://docs.pieces.app/products/core-dependencies/pieces-os)
    local processing, [WakaTime](https://wakatime.com/privacy) syncs metadata
    not source, SpecStory hide-before-share.)
- **Secret redaction at capture is mandatory even when private** -- don't store
  raw tokens/keys (blast-radius, accidental share). Run redaction BEFORE local
  persist, upload, LLM call, AND share. Precedent: [Warp Secret Redaction](https://docs.warp.dev/support-and-community/privacy-and-security/secret-redaction/)
  (built-in + custom regex; NOTE Warp does NOT redact in session-sharing -- a
  gap to beat); detectors like [gitleaks](https://github.com/gitleaks/gitleaks)
  / [TruffleHog](https://github.com/trufflesecurity/trufflehog). Plus path/
  command denylists (`.env`, SSH keys, `printenv`, `kubectl get secret`).
- **Validated tiering** (every piece has a precedent; nobody assembled it):
  - *Personal (default):* local raw capture, redacted, encrypted; sync optional
    + E2E; search client-side; no org graph unless opted in. (Atuin + Pieces.)
  - *Team:* user owns private captures, shares sessions explicitly; server
    indexes only SHARED items; derived summaries team-visible; raw stays private
    unless shared. (SpecStory sharing.)
  - *Enterprise:* BYOC/self-host, SSO/SAML/SCIM, audit logs, retention, admin
    capture policy, zero-retention LLM calls, data residency. (Linear +
    LangSmith.)
- **Why it strengthens the moat:** the value is not capture alone (Atuin/Warp
  own slices) -- it's *capture + the controlled-sharing graph on top*. The
  hybrid (private raw, shared-derived) is the only model that reconciles
  aggressive capture + security review + org intelligence + trust -- and it's
  unbuilt.

## The biggest risks (adversarial teardown)

Two independent adversarial passes -- a steelman + a cited critic agent
(`gpt-critic`) -- CONVERGED on the same core failure mode and the same fix. That
convergence is itself the signal. The load-bearing risks:

### The single biggest reason this fails

> **The product is built around MAXIMAL CAPTURE, but the market rewards MINIMAL
> WORKFLOW DISRUPTION.** That contradiction is the core. To deliver the
> ambitious value you need deep capture; deep capture creates security friction,
> install friction, privacy fear, redaction risk, procurement delay, and noisy
> extraction -- while the features users immediately understand are being
> shipped natively by Claude/OpenAI/Cursor/Warp/SpecStory. Squeezed: too
> invasive for the value users already get, too speculative for the value that
> justifies the invasiveness.

### The seven risks both passes raised

1. **Product-or-feature: mostly feature, already being eaten.** Native + Warp +
   SpecStory already ship remote sessions, live sharing, transcript search, and
   are converging on cloud repair agents + CI triggers. The only un-eaten claim
   is "neutral across agents + mine deeper knowledge" -- a *bet*, not a product.
2. **Neutrality is a fragile bet, not a moat.** (a) Teams STANDARDIZE (procurement/
   security/discounts reward consolidation) -- a team on one agent doesn't need a
   neutral layer. (b) **Native always beats neutral on fidelity** -- a wrapper
   "sees shadows"; the vendor sees hidden tool/plan/permission/cost state. (c)
   **MCP cuts both ways** -- standards win => plumbing commoditizes; standards
   lose => integrations stay brittle (Omnara abandoned CLI-wrapping as
   unmaintainable). Warp already going cross-vendor disproves "nobody will."
3. **Scope sprawl = five products, five ahas, five competitors, one team.** A
   startup gets ONE aha. Capture / live-sync / error-trigger / belief-graph /
   concept-graph each have a different buyer and incumbent. The
   sync->ledger->belief->brain "arc" is a multi-year sequence of unproven leaps.
4. **The belief/concept graph is the most beautiful + most commercially
   suspect.** Users want fewer broken builds, not epistemology. If maintained by
   hand it dies (engineers don't maintain docs/ADRs/runbooks); "mined from
   sessions" must solve claim-extraction + dedup + contradiction + stale-
   invalidation + hallucination-containment over raw material that is mostly
   noise. Every "company brain" is a graveyard.
5. **The error-trigger is a demo toy until proven.** Red builds aren't
   homogeneous (TDD-red, flaky, OOM, wrong branch, missing service). Auto-fire =
   spam; auto-apply = dangerous; proposal-only = another notification queue. It
   races platforms (Codex/Cursor/GitHub) adding native CI/issue triggers.
6. **Privacy is the enterprise-killer, and the HYBRID makes it worse.** Security
   hears "keylogger + screen recorder + source-exfil pipeline." The hybrid's fatal
   flaw: *a summary of proprietary code can BE proprietary code; a summary of a
   secret can reveal it.* The impossibility triangle -- **you cannot have
   private-by-default + server-readable org graph + capture-everything +
   low-friction at once. Pick two.** Atuin wins by NARROWING (commands only, no
   output); this proposes the scary superset.
7. **n=1 founder-market-fit + worst-shape GTM.** The team IS the ICP (good for
   taste, dangerous for demand). And the GTM is the worst combination:
   *individual-install friction* (dies at "it records everything") + *enterprise-
   sales complexity* (9-month security review before first value). What budget
   line does it replace? Cursor/Claude/Codex seats already exist.

### What both passes prescribe (the convergent fix)

1. **Kill "company brain."** Reposition to a budget-line phrase: **"audit trail /
   replayable, permissioned handoff for autonomous coding-agent work."** Sharper,
   has a buyer (security/platform), sounds serious.
2. **Collapse five capabilities to ONE provable loop.** The convergent pick:
   **"when an agent changes code, give the team a replayable, searchable,
   permissioned record of what happened"** (prompt + commands + diffs + tests +
   errors + share-link + PR attachment). Do NOT lead with concept-graph,
   auto-fix, or epistemology -- earn the right to mine knowledge later.
3. **Validate pull with EXTERNAL teams in ~14 days, opt-in capture, ask for
   money.** Pass condition (both passes, near-identical): 5 teams active without
   founder handholding; 3 share sessions; 2 attach to PRs/issues; >=1 pays or
   starts procurement; >=1 says native tools are insufficient; one security
   review starts and doesn't instantly die. If capture is founder-driven only,
   it's an internal tool.

### The 5 hardest questions the founders MUST answer

1. What is the ONE painful workflow Trackinizer solves **10x better than Warp/
   Claude/Cursor/Codex/SpecStory TODAY**?
2. What % of captured sessions are later searched/shared/resumed/cited by another
   person? **If under ~30%, it is not a system of record.**
3. Who signs the PO -- platform lead, security, research lead, or individual dev
   -- and what **exact budget line** does it replace?
4. What data must the **server read** to create value, and what enterprise buyer
   approves that data path?
5. If Anthropic/OpenAI/Cursor ship cross-device history + team sharing + CI
   triggers + transcript search, **what remains defensible?**

## The one experiment to run this week

Concierge wedge test, 5 target teams:

- Install the wrapper (`trax run claude`, `trax run codex`).
- Within 24h give them: cloud session archive, full-text search, shareable
  session links, live `trax watch`/attach, `trax send`, a daily "what agents
  learned" digest.
- **Charge real money** (~$100/user/mo or $500/mo for 10). Free tells you
  nothing.

Continue if, within 7 days: >=3/5 install on real workflows, >=2/5 connect more
than one vendor, >=2/5 share session links, >=2/5 use search to recover prior
work, >=1 pays; users ask for SOC2/self-host/redaction. Kill/reposition if usage
is personal backup, nobody searches after day one, nobody shares, or teams won't
pay even a small pilot.

Killer interview question after 3 days: **"If I turn this off tomorrow, what
breaks?"** Good: "remote agents go invisible / I lose auditability / my team
retries failed work." Bad: "it was convenient / cool history browser."

## Important open questions (for the founder)

1. **Wedge conviction** -- buy "session-infra wedge, knowledge-graph expansion"
   as the sequence, or does the graph lead?
2. **Cross-vendor neutrality** -- is unifying Claude + Codex + Gemini + Cursor the
   bet (adapters strategic, not optional), or go deep on just Claude Code first?
3. **Agent-first vs human-first** -- primary user an *agent* (MCP/harness/
   injection) or a *human* reviewing/attaching (Live-Share-like UX)? That picks
   the launch surface.
4. **Who is the first buyer** -- AI platform lead? research lead? founder/CTO of an
   AI-native team? Name one.
5. **SaaS vs self-host vs OSS-led** -- teams care about private code + transcripts;
   does serious adoption require self-host/BYOC, or is hosted SaaS fine for the
   wedge?
6. **n=1 test** -- has anyone *outside* the authoring team gotten value without
   the authors in the room?
7. **Public corpus** -- the opt-in-share knowledge flywheel: launch wedge or
   post-PMF bet? (Both investigations say later.)

## Next steps

1. Fill the competitive section from `gpt-landscape`.
2. Decide wedge + cross-vendor stance (questions 1-2).
3. If session-infra wedge confirmed: spec + build the live session-event SSE +
   `trax watch`/`--attach` (the one missing primitive), then session sharing,
   then the on-the-fly summarizer into the concept graph.
