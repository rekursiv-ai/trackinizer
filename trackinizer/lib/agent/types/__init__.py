"""The vocabulary the agent layers share.

A catalog DECLARES what a model accepts, a session RECORDS what it did, and a
harness REQUESTS both -- three layers naming one set of things. Each type was
defined more than once before this package existed, so a value added to one
spelling silently failed to reach the others.

These modules may depend on each other; nothing else in ``agent`` may be
depended on BY them. Reach into the one that defines what you need:

- ``trackinizer.lib.agent.types.capability`` -- one model's ceilings and prices as a
  catalog row states them, plus every knob a request selects from it.
- ``trackinizer.lib.agent.types.cost`` -- token counts, prices, and the price catalog.
- ``trackinizer.lib.agent.types.sessions`` -- the provider-neutral session record.
"""

from trackinizer.lib.agent.types import capability, cost, sessions


__all__ = ["capability", "cost", "sessions"]
