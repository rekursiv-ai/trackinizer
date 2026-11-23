# What a good answer looks like

`DESIGN.md` says what the app can draw — every node the catalog registers, the library
behind it, its fields, and what each holds. This says how to think your way to an answer
with them. Read both first: the brief is the vocabulary, this is the method, and neither
chooses for you. What is checkable is checked (`web/src/ui/law.test.ts`), and what that
file checks is that this stays a method — a way of reasoning, not a stylesheet.

The whole law in one line: **a pane is a measurement, not a presentation.** Every mark on
it either says something a reader reads or locates the record it came from. One that does
neither is chrome, and chrome is what makes an answer look generated.

## what you can reach

You are answering on a live canvas over a real store: every record the lab holds, and the
valenced edges between them — a negative one arguing against the claim it points from. The
researcher never tells you to consult it; they just ask their question. So the graph is the
context, always: pull the records that bear on the question and what is wired to them before
you plan a single item. What the answer may claim is bounded by what you opened, and which
parts belong together is decided by the wires, not by you. Every kind, verb and field the
store has is in its CLI's `--help` — you have it. An edge you did not read is an edge you
may not draw.

## the brief you answer against

State these before the steps. Each left blank is decided by habit instead of by you.

1. **The question**, in the asker's own words.
2. **Who reads it, and what they do next** — spend a run, merge a PR. The next action
   decides what goes at the top.
3. **The register** — instrument or document. Decided here, before any content.
4. **The spine** — two to four parts, named.
5. **The states** — what this shows with nothing, with one record, with ten thousand. A
   surface that only works at the middle size will embarrass you on a Monday.
6. **The records and figures**, by address and real number — never a placeholder: a surface
   composed against `123` assumes the wrong widths.
7. **The noise budget** — how much is allowed, counted in the units this answer has. Put it
   in the answer as `budget`, one ceiling per item you intend to use, written before the
   items: the verifier counts what actually drew and fails a page over its own ceiling. So
   budget tight and cut to it — anything that still survives a smaller number was never
   carrying the answer, however much it felt like evidence while you were writing it.
8. **What must not be built here** — the thing a generic answer would reach for and this
   one must refuse. Name it: the rules below forbid everywhere; this says what is wrong
   *here*.

## how to arrive at one

Do not emit items in the order they occur to you: the content comes out right and the page
wrong — a figure under the wrong heading, marks attached to the last point instead of their
part. Plan, then fill.

**1. One sentence.** State the answer before drawing it; it becomes the lead and everything
else supports it. Cannot? Not ready.

**2. Instrument or document.** *Document* convinces someone; *instrument* lets a reader read
values off a machine. Never both at once — a page half one is neither, and the reader cannot
tell which they are holding.

**3. Spine — two to four parts, named first.** Each answers one question. A fifth means two
questions; split or drop the weakest. Never open a part for one sentence.

**4. Per part, the one node that carries its evidence.** Chosen for the shape of that
evidence — the catalog says what exists and what each holds. A second node repeating the
first makes a surface longer, not clearer.

**5. Attach each item to its part, not to the item before it.**

**6. Audit before emitting:**
- one lead; two to four parts; no sub-point that is its parent's only child.
- no section restating the one above — rows and a figure of the same rows is one of them,
  not both.
- a part's title says what it concludes, never how much it holds. A count beside a heading
  is a census the reader cannot act on and the figure beside it already shows: counts of
  what a pane holds belong in provenance, where their job is to name what the answer stands
  on.
- every number has its unit; every count has integer ticks; every card names what it stands
  on; an axis carries at most ~8 tick labels.
- read the headings alone — if they are not a summary of the answer, the spine is wrong and
  no prose under them fixes it.
- last, against the render: is there anything you would correct after seeing this page? A
  reader sees one surface and a seat never sees its own, so correct it before you write it.

## what is not yours to decide

Sizes, faces, inks, radii, alignment and everything else about how a pane LOOKS belong to
the app. They live in its stylesheets, are held by its tests, and are deliberately absent
here: a setting written in two places drifts, and a law that quotes CSS hands you something
to imitate instead of something to fill in. The brief guarantees the same from the other
side — generated from the source, so it lists exactly what the app registers, and an entry
that is not there cannot be asked for. One surface sits outside this: a page you authored
and published, whose stylesheet the app cannot reach into. It is the one place a colour is
yours to write, and the reasoning below still binds there.

The choice that is yours is the whole job: which of what is registered answers this
question, how many of them, in what order, what each opens. Where nothing registered holds
the shape, author it yourself rather than bending the nearest entry — a surface forced into
a shape it does not have is how two unrelated answers come out looking the same.

## numbers, provenance, addresses

Every figure is the one the source printed, with its unit, at the source's precision. A
count has no fraction and declares integer ticks (`"axis": {"tickMinStep": 1, "format":
"d"}`). Never round for the look of it — not `~3.2k`, not `about 95%`, not `several runs`: a
rounded figure cannot be checked against its record, and an unchecked number is merely
plausible. Where two figures are comparable the component divides them — you write both
sides and their units, never the ratio itself.

Every card ends in a footer naming what it stands on, in machine ink:
`3 records · Issue#15 ×2 · step 17,900`. A block with no footer is unfinished. This is the
one place the ledger's own shape belongs — the addresses, the counts, what is null. Above
it, a line reads as the finding, not the account of how it was reached: the assertion, not
its category (*"a module with no twin"*, never *"test-coverage concerns"*). When the store
holds nothing, say so; an honest block naming the gap beats an invented one, and `unpriced`
— the ledger never priced this shape — is itself a finding.

The address is the interface. Print it whole and break it after a slash and nowhere else, so
a long path stays typeable. Never ellipsis what a reader might need to type.

## what the question is asking for

A researcher never says "analyse the graph" — the graph is always there. They say "what
should I run next on this", "what did my research prove", "what does this change affect",
"show me how this works". The question can be anything, so do not match it against a list of
known shapes. Derive the surface from it:

**1. What will the reader DO with the answer?** Spend a run, drop a claim, merge a change,
understand a mechanism. The next action is the whole design: an answer that will be acted on
is built as a set of options with the cost and consequence of each; an answer that will be
understood is built as the thing itself.

**2. What is the shape of what you have to say?** Not the topic — the shape. One quantity
over a span is a figure; several candidates with different consequences is a list in
consequence order; a claim and the records under it is a claim with its ledger; a mechanism
is the mechanism, drawn.

When what connects the records IS the finding, hand over the territory the store already
holds and let the reader walk it, rather than asserting each wire in your own words. An
asserted reading is one sentence read once; a territory is re-read by whoever asks next,
and its valences come from records instead of from you.

**3. Order by consequence, never by convenience.** Whatever the question, put first the row
that changes what the reader does next. For candidate actions that is what each would
settle, not what each costs — a cheap action that closes nothing is worth less than an
expensive one that closes two. Cost and recency break ties; they never lead.

**4. Show the half that argues against.** Every edge carries a valence, and a surface that
renders only the supporting records is advocacy. Dissent goes in the same list, in the same
order, not in a footnote. A reader who cannot see it will find it later and distrust
everything else you drew.

**5. Say what the ledger cannot.** An unpriced action says `not priced`; an unproven claim
says so with its confidence; a figure nobody measured is absent, not estimated. The gap is
part of the answer.

Resolve "this", "my" and "it" against the records the researcher is looking at. A pronoun
with no referent is a question to ask back, not to guess at.

## what earns what

**A hand-drawn mark** is earned by a value a reader can place directly: write the geometry
from the records' own numbers and name every axis and unit inside the figure. A library
earns its place only when its scales, legends or real sampling materially improve the
result, never when it decorates a number that reads in one line. **Motion** is earned by a rate or an evolution no frozen
frame holds; a turning camera is not motion. **Live beats filmed** wherever the reader might
want to look from somewhere else — a clip is finished the moment it exists, a surface that
still runs can be interrogated. **Density** is earned by real coverage: draw a region filled
where the data fills it, few marks where there are genuinely few. **Nothing is invented to
fill a slot** — no guessed address, no estimated price, no figure never fetched; a thing you
could not find is said in a sentence, not faked as a measurement. A thousand balls reads as
nothing: a mark the reader cannot name carries no information, however true the wire.

**A page you write yourself** is earned by geometry the catalog does not hold — a shape, a
running mechanism, three real dimensions. Two of its laws come from the sandbox rather than
from this app's courtesy: the page may fetch nothing, so every figure it shows is one you put
into it as text, and it inherits no ink, so a colour there is yours to choose and still has to
be named. The rest is the law as it reads anywhere else: draw the coordinates the records
filed and nothing between them, and solve the frame from the data instead of posing it — a
camera typed by hand is how six real coordinates arrive as a picture of empty background. The
brief carries how to write and publish one.

Two sizes decide the rest: how many items a surface holds, and the order they read. Say both
outright rather than leaving them to be derived.

## a relation is drawn, not described

When the answer is **how one thing came from another** — what formed a claim, what a change
broke, how a result was arrived at — the relation is the finding. Sentences carry a relation
only by naming both ends and the direction every time they mention it; four related records
cost a dozen such sentences, and the reader has to hold the whole set in their head before
the shape appears. Drawn, the shape is the first thing they see. Draw it.

Two of the things you can reach draw relations, and they answer different questions. One
states **your** reading: you place each end, you write the word on each arrow, and what
appears is the case you are making. The other hands over **the store's** territory around a
record — you name the record, the store says what bears on it and with what sign, and the
reader finds their own way in. Choose by whose reading is being done. Both are routed and
dragged by the library behind them, so neither is a picture of a territory: they are the
territory, and the reader may pull it around.

Boxes with nothing drawn between them are the worst of both — the cost of a drawing with the
information of a list. If what you have is a set of records with nothing joining them, that
absence IS the finding, and it is one sentence rather than a diagram of disconnected boxes.

## a territory is part of an answer, not the answer

Unless the reader asked to look at the territory itself, it is one element among several and
usually not the largest. The question is answered by what was found; the neighbourhood it was
found in is the evidence behind that. Handing the whole surface to it gives the reader back
the distillation that was your job. Bound its height, and put the finding above it.

The corollary: a surface carrying one kind of thing is nearly always wrong. An answer that
measured something shows the measurement drawn; one that compared arms sets them in a table
where a reader's eye can run down a column; one that ran code shows the code and what it
printed; one that traced a formation draws the relation. A column of paragraphs with a number
in each is a report nobody designed — if every item on a surface reads as a paragraph, the
composition step did not happen.

## four seats, not one agent

One agent writing a whole answer trades truth for fit — dropping a record because a card grew
tall, keeping a part that says nothing because the spine looked thin. Four seats, each
accountable for a different failure.

**Librarian** — returns the records that bear on the question. Owns: none missed. Never: what
they say or where they go.

**Author** — turns records into claims. Owns: every sentence true, every figure traceable to
a row. Never: space — no shortening a claim to fit a card, no dropped source.

**Designer** — picks register, spine, nodes, composition. Owns: the page. Never: content — no
softened claim, no dropped record, no rounded number. A page too small for what the author
wrote is a message back to them.

**Critic** — reads this file against the *rendered* surface, names what is off in the law's
own words. Owns: the verdict. Never: the fix.

The critic earns its seat because **an author cannot see their own page.** A surface reads as
intended to whoever planned it, so its awkwardness is invisible from that seat and obvious
from any other. Review the render, never the JSON — the JSON always looks right.

## comparing two things

When the question sets two things side by side, the difference between them is the
finding, and the visual must carry it as **data, not as styling**: a difference grid beside
the pair, overlaid profiles drawn to one shared scale, paired rows where each row is one
subject. Two surfaces that differ only in ink — this one grey, that one blue — are one
surface drawn twice, and the reader leaves unable to say what changed. So before
composing, name the **internal quantity that differs** and how the encoding makes it
visible. Attention is the standing example: softmax makes every query's row compete
globally through `exp`, so its rows *peak* and their tails die; the kernel form sums
features with no competition, so its rows *smear*. Peak against smear on one scale, the
absolute difference in its own grid, is the comparison. Styling was never it. Then audit
against one question: if the reader swapped the two ink colours, would any conclusion
change? If not, the comparison is not in the visual yet. And when the difference is a
*process*, animate the process: run both computations live on the same inputs and draw
each step as it computes — scores, transform, normalize — never two end states cut
together. An end state says what differed; the animation says why.

## animate by frames, not by effects

An animation is a sequence of states, so write the states before drawing anything: for
each beat, list its frames — what sits where, what changed since the frame before, which
counter moved. Give every element a **home it keeps across frames** (a slot, a lane, a
slab region drawn to its name) and make movement a transition between homes, never a
wiggle. When something moves into a store, draw the store's interior — slots, addresses,
what evicted to make room — and show it land; storage the reader cannot see is storage
the reader cannot believe. Hold the per-frame invariants: nothing overlaps, every label
legible at the frame's own resolution, and the canvas rendered at the device's pixel
ratio — a soft canvas reads as broken. **A label owns a reserved rect, always.** A label is
never floated over content that moves or grows: lay text out in stacked, measured rows —
caption strips below the canvas, counters in their own column, slot names under their
slots — and where a plate is unavoidable, draw the plate. In a self-authored page,
assert the invariant in code: two text rects that intersect mean the layout is wrong,
not unlucky. The test of a frame: a reader can narrate it —
"now K3 is in SRAM, Q is parked, O is half-accumulated" — in one sentence. A frame that
animates without being narratable animates without informing.

## look at the field's own rendering first

When the object has a canonical form — an attention matrix is a tiled heatmap with axes
and a colour scale; a metric over steps is a line chart — read the canonical form before
composing yours. The field converged on it because it reads, and a drawing that re-litigates
the form spends the reader's attention on your rendering instead of on the data. Weight
values that span decades need a stated nonlinear scale and a colourbar; a linear scale
on small weights is static. This is research, not imitation: the canonical form tells you
what a reader expects to read off, which is the encoding's job.

## what never belongs in an answer

Emoji. An icon you invented for a label. A pill badge. A hero opening. A rounded figure. Each
of these is content, so no stylesheet can stop it and no test will catch it — which is why
the list is here rather than in the CSS. They are the marks a reader recognises as a page
that was generated rather than written. How a pane is dressed — shadow, gradient, radius,
centring, a literal colour — you cannot get wrong *in the catalog*, which accepts content and
lets the app decide the rest. A page you published is the one surface where nothing dresses
it for you: a gradient there, or a hue its own key does not name, is as wrong as an emoji and
harder to spot.
