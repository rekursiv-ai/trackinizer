"""The :class:`BeliefStrength` derived value type."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class BeliefStrength:
    """How much the citation graph leans on a Belief or Experiment.

    Computed by :meth:`~trackinizer.server.store.read._ReadMixin.strength_for`
    from currently-true ``proves`` citations only (``design.md``'s named but
    unbuilt "emergent authority"). Purely derived and read-only: it never
    writes ``Belief.judgement`` or ``confidence`` -- "the system never forms
    the verdict" applies here exactly as it does to citations themselves.
    A human comparing this against a Belief's own asserted ``confidence`` is
    the intended use; the two are expected to sometimes disagree, and that
    disagreement is the point.
    """

    strength: float
    """Euler-based argumentation strength in ``[0, 1]``.

    ``0.5`` (the neutral base score every Belief/Experiment starts at) means
    either no currently-true ``proves`` evidence exists, or its support and
    attack exactly cancel. Above 0.5 means the graph leans toward the claim;
    below means it leans against.
    """
