**Decision (supersedes the rationale below).** The provenance edge is named
`produced_by` (child's view) and `produces` (parent's view), NOT `descends` /
`descended_by`. This breaks the active/passive naming rule, but provenance
is the one relation with no natural child-as-subject active verb in English (see
the rationale below), so grammatical naturalness wins over mathematical elegance.
Everywhere below that says `descend` should be read as `produced_by` / `produces`.

```
                              OLDER  (parent)

    {narrows,requires}     {produced_by,supersedes}     {proves,favors}
            ▲                         ▲                        ▲
            │                         │                        │
          Issue ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄▷ Inquiry ◁┄┄┄┄┄┄┄┄┄ {Belief,Experiment}
            │                         │                        │
            ▼                         ▼                        ▼
{narrowed_by,required_by}  {produces,superseded_by}  {proved_by,favored_by}

                               NEWER  (child)
```

* "Parents" are always "older" than "children" where "older" is on each edge's own clock: creation-time for all edges except `require`, which is completion-time (do-time).
* Issue and Artifact are each an Inquiry. Experiment and Belief are each an Artifact.
* Any Inquiry can be produced_by one or more older Inquiries (its origins); the parent's view is produces.
* Any Inquiry can be superseded_by one or more others (M:N knowledge-surgery).
* An Issue can be narrowed_by (broader→narrower decomposition) or required_by (it's the prerequisite another waits on) -- both Issue→Issue.
* proved,favored edges carry valence ∈ [−1,1] (polarity = sign, weight = magnitude; 0 neutral, default 0.5; never unset). trax `disprove`/`disfavor` store the negated value (default −0.5).
* supersede is M:N knowledge-surgery (replace/coarsen/split/merge); produced_by is pure origin (child came to be from parent).
* prove is load-bearing (votes in the proof predicate); favor is context (informs but does not vote).
* "requires" is "do-time" whereas everything else is "origin-time". nevertheless, A requires B implies B is "do-time" older than A.

## Rationale: why the provenance verb was hard (and why we settled on `produced_by`)

The provenance edge needs a verb for "A came to be from B." To match the other edge names (`narrow`, `require`, `supersede`, `prove`, `favor`), it would have to be a bare transitive verb with child-as-subject. English has no such word -- possibly because a child-as-subject origin verb is anti-causal: the cause is the object, not the subject, which the active voice resists. The closest coinage was `descend`, which has a property its rivals lack: **`A descends from B` stays unambiguous even with `from` omitted.** Its rivals don't. `A derives B` flips to "A produces B." `A stems B` flips to "A halts B" (*stem the tide*). `A springs B` loses the origin sense entirely. Only `descend` keeps its meaning bare, because its sole other transitive sense is physical (*descend the stairs*), which an abstract inquiry can never trigger. We nonetheless rejected `descends` (see the Decision above) for the grammatically natural `produced_by` / `produces` pair: the passive child-view and active parent-view are both standard English, so provenance simply keeps the one passive name and needs no coinage to defend.

| word | form | why rejected |
|---|---|---|
| produces (as forward verb) | `A produces B` | parent→child direction; wrong as the child's forward edge. Adopted only as the inverse view of `produced_by` -- see chosen row. |
| extends | `A extends B` | smuggles "builds-upon"; over-claims for pure origin |
| educes | `A educes B` | means parent→child ("A draws out B"); wrong direction |
| derives | `A derives B` | bare transitive flips to "A produces B"; needs `from` |
| originates | `A originates B` | transitive means "A creates B"; needs `from` |
| presupposes | `A presupposes B` | claims necessity; false for search→paper |
| requires | `A requires B` | taken -- the sequencing edge (do-time), not provenance |
| reflects | `A reflects B` | resemblance, not origin |
| owes | `A owes B` | needs "owes its X to B"; weak bare transitive |
| credits | `A credits B` | attribution flavor, not origin |
| follows | `A follows B` | pure sequence; no causality |
| ensues | `A ensues B` | intransitive; transitive sense obsolete |
| inherits | `A inherits B` | needs "from"; bare = receives B as the thing inherited |
| succeeds | `A succeeds B` | collides with supersede's "successor" |
| scions | `A scions B` | noun-verbing; obscure/hacky |
| sources | `A sources B` | collides with code "source"; hacky |
| bears / parents | `B bears A` | parent→child, or flips kinship (subject = elder) |
| stems | `A stems B` | flips to "A halts B" (*stem the tide*); needs `from` |
| springs | `A springs B` | loses origin sense bare; needs `from` |
| ascends | `A ascends B` | wrong tree-direction ("rises above"); muddies parent/child |
| descends | `A descends B` | best coinage, but a coinage; overruled for `produced_by` (see Decision above) |
| **produced_by / produces** | `A produced_by B` / `B produces A` | **chosen** -- standard English passive/active pair; breaks the append-`s` rule but needs no coinage |
