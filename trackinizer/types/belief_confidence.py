"""Derived belief confidence: a log-odds fold of currently-true citations.

The confidence of a Belief/Experiment is computed from the ``proves`` graph,
not asserted by a human. Each citation contributes a signed log-odds nudge
weighted by the citing node's own confidence, the nudges sum (independent
evidence compounds in log-odds space), and a logistic maps the total back to
``(0, 1)``:

    confidence = sigmoid( sum( citation_confidence * valence ) )

``0.5`` is neutral (no evidence, or support and attack exactly cancel), above
0.5 leans toward the claim, below leans against. Symmetric: a proof and an
equal-magnitude disproof cancel exactly. Purely derived and read-only -- it
never writes back to any stored field, so it can disagree with a human's prior,
which is the intended use.
"""

from __future__ import annotations

import math


__all__ = ["fold_confidence", "logistic"]


def logistic(x: float) -> float:
    """Return the logistic ``1 / (1 + e**-x)`` without overflowing either tail.

    Splitting on the sign of ``x`` keeps ``math.exp``'s argument non-positive,
    so a large-magnitude log-odds sum returns its analytic limit (0 or 1)
    rather than raising ``OverflowError``.

    Args:
      x: Log-odds.

    Returns:
      probability: ``1 / (1 + e**-x)`` in ``(0, 1)``.

    """
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


def fold_confidence(log_odds: float) -> float:
    """Map an accumulated citation log-odds sum to a confidence in ``(0, 1)``.

    Args:
      log_odds: Sum of ``citation_confidence * valence`` over currently-true
        ``proves`` citations. ``0.0`` for a node with none.

    Returns:
      confidence: ``sigmoid(log_odds)``; ``0.5`` at ``0.0``.

    """
    return logistic(log_odds)
