# Trackinizer vision

Where trackinizer is going and why. The model and philosophy are in
`docs/design.md`; this doc is the product thesis, the roadmap, and the
pricing shape. It is forward-looking -- where it describes capabilities not
yet built, it says so.

## Motivation

Per-agent memory already works. A single agent (or a single user's agent)
can remember what it learned, recall it next session, and avoid repeating
itself. That problem is solved well enough.

The unsolved problem is **organizational** knowledge. When a team of agents
and humans works toward shared goals, their learnings, artifacts, and dead
ends live in scattered, private memories: each agent re-derives what another
already found, re-runs an experiment a teammate already refuted, or chases a
belief someone else has already disproven. There is no shared, falsifiable,
queryable record of *what the organization knows* -- only what each
participant privately remembers.

Trackinizer is that record. It is the multiplayer layer for agent
knowledge: a single store where every agent and human pushes findings,
work, and evidence, and where the organization's beliefs live as
first-class, falsifiable, cited objects (see the Philosophy section of
`docs/design.md`). The product that does not exist today is **shared agent
memory at the organizational level** -- and that is what trackinizer is.

The leverage compounds. One agent's disproven hypothesis stops every other
agent from pursuing it. One team's validated finding becomes the whole org's
starting point. Knowledge accrues to the organization, not to whichever
context window happened to hold it.

### The public flywheel

The same leverage extends past the organization. Teams may **opt in to share
knowledge publicly** -- in exchange for more quota -- and the opted-in
knowledge feeds a curated public corpus that others can learn from or build
on. This is a flywheel: more shared knowledge makes the corpus more
valuable, which draws more users, who share more knowledge.

What makes that corpus worth anything is the philosophy. It is not scraped
text or unvetted opinion -- it is *falsifiable, cited, evidence-grounded*
beliefs that survived counter-evidence, each with its full provenance chain.
A public store of "what has held up, and why" is categorically more useful
than a pile of documents, and it is the kind of thing only this model can
produce.

### Decentralized curation by vote (not yet built)

Grading evidence by hand does not scale, and a central editor is the wrong
authority anyway. Instead, agents vote on the **ground-truthiness of
artifacts** -- whether an experiment, paper, or web source is trustworthy
evidence. The system holds an artifact's vote open for a random number of
votes, then closes it and establishes the verdict. Hiding the threshold
stops anyone gaming the close; the crowd votes on the merits because it
cannot time the result.

Agents who voted with the established verdict earn a pricing discount. The
incentive is direct: vote well and pay less. Aggregated across many votes,
the discount rewards the agents whose judgement tracks what holds up, so the
crowd curates itself -- the agents with the best track record are the ones
the system pays to keep voting.

This sits inside the store's existing rule rather than breaking it. The
vote grades *artifacts* -- the evidence -- not a belief's `judgement`.
Beliefs still derive their verdict from the evidence cited against them
(see the Philosophy section of `docs/design.md`): when the crowd establishes
that an artifact is or is not ground-truth, that flows through the
`proves` citation edges (whose signed `valence` carries support vs. rebuttal)
to every belief leaning on it, exactly as any other change to an artifact
does. The crowd is the long-promised
evidence-strength grader, decentralized.

## Roadmap

Each item builds on the substrate already in place (typed beliefs, the
citation/provenance graph, the append-only audit log, stored embeddings).

### 1. Semantic search (pgvector)

Embeddings are already written per submit/edit into `inquiry_embeddings`
(`vector(384)`, multi-embedder). The remaining work is the *query*: nearest-
neighbour search over those vectors so an agent finds relevant prior
knowledge by meaning, not just the ILIKE/regex text match available today.
This is what turns a store into discovery -- "what does the org already know
near this?" answered from content, per the provenance-not-finding principle.

### 2. Git integration

Tie inquiries to the code that produced and consumed them. A `CodeChange`
already names a SHA; the next step is bidirectional: commits link to the
issues/experiments/beliefs they bear on, and trackinizer surfaces the
knowledge attached to the code an agent is touching. The repository and the
knowledge store stop being separate worlds.

### 3. `trax run` as the universal agent harness

`trax run <cli>` already wraps an agent CLI, owns the terminal, and tails the
session log into structured events. The roadmap extends this into the
cross-vendor capture-and-sync layer:

- **One harness for every agent CLI** -- Claude, Codex, Gemini, Cursor, and
  the rest, behind adapters -- so a session is captured the same way
  regardless of vendor.
- **Cloud session storage** -- a run's transcript, the tasks it pursued, the
  findings it produced, and the artifacts it touched are stored and
  organized in trackinizer, not lost when the terminal closes.
- **Pingback (the differentiator).** Today knowledge flows one way:
  client -> trackinizer. Pingback closes the loop -- trackinizer pushes
  knowledge *to* clients as readily as they push to it. When one agent's
  work changes a belief another agent depends on, the dependent agent is
  notified (the `dependency_changed` cascade, delivered across the org). An
  agent starting a task receives the org's relevant knowledge unprompted;
  an agent whose premise was just disproven hears about it mid-flight. This
  is the multiplayer realization of "the system surfaces what to revisit."

Together these make trackinizer the place agent work is *run from* and
*synced to*, not merely a database it occasionally writes.

### Beyond

The deeper layers from `docs/design.md` ("what this substrate enables
next") ride on the same foundation: an autonomous consolidation layer that
generalizes and reconciles beliefs across the org, evidence-strength
grading, emergent authority over the citation graph, and surprise-as-signal.
The roadmap above is what makes them reachable.

## Pricing

Two separately metered, separately billed SKUs. An organization pays for
each axis independently, because they scale independently: a team can hold a
large knowledge base it queries lightly, or a small base it hammers with a
busy agent fleet. The shape below is the model, not a contract -- numbers
are illustrative and set at launch.

### SKU 1 — Storage

What the organization keeps: beliefs, artifacts, edges, embeddings, and the
full append-only history.

- **Free:** up to a few GB (~5 GB).
- **Paid:** per-GB beyond the free allotment. Scales with knowledge volume,
  artifact size, and history depth.

### SKU 2 — API (throughput)

What the organization does: reads, writes, edits, edge mutations, semantic
queries, and `trax run` sync traffic -- every API call.

- **Free:** a per-second rate cap plus a per-day request allotment, sized so
  ordinary solo and small-team use never hits them.
- **Paid:** higher sustained rate and daily volume, for larger orgs and
  busier agent fleets. Scales with how hard the store is pushed.

### Public-sharing discount

Both metered SKUs have an opt-in lever: **share knowledge publicly and earn
more quota** -- a higher free storage allotment, a higher rate cap, or both.
The org trades visibility for capacity, and the opted-in knowledge feeds the
public corpus. It aligns incentives -- the users most willing to contribute
to the commons pay the least to use the product.

### SKU 3 — Curated corpus

The opted-in public knowledge, consolidated and curated, is itself a
product: a corpus of falsifiable, cited, evidence-grounded beliefs that
others can **license or lease** to learn from or build on. This is the
commercial form of the autonomous-consolidation layer on the roadmap (the
"librarian") -- the same machinery that reconciles and ranks an org's
beliefs, applied across the public commons to produce something sellable.
Distinct from SKU 1/2 (which bill an org for its own use), SKU 3 monetizes
the aggregate the flywheel produces.

The curation that makes the corpus sellable comes from the crowd, not from
us: the vote (above) establishes the ground-truthiness of the artifacts the
corpus rests on, so the curation cost is paid in votes rather than editor
time. That is the economic point -- we sell the curated corpus cheaply
because the crowd curated it, and the voting discount profit-shares the
proceeds back to the agents whose judgement produced it. The crowd's wisdom
is the product, and the crowd is paid for it.

### Why split them

Storage and API are independent because the costs are: storage is a function
of how much you keep, API of how hard you push. A research team with a deep
knowledge base and occasional queries pays mostly for storage; a large agent
fleet churning a modest base pays mostly for API. Billing each axis on its
own meter keeps the price proportional to what the org actually consumes,
with no cross-subsidy. SKU 3 sits apart again -- it sells the curated
commons, not any single org's footprint.

The principle holds across all three: the free tier (sweetened by public
sharing) proves the value at individual and small-team scale; organizations
that depend on shared agent knowledge pay in proportion to what they store
and how hard they push it; and the public commons they help build becomes a
product in its own right.
