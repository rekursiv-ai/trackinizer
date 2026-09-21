"""Session-search embedders: one module per model, plus the registry.

Each model lives in its own module (``qwen3_4b``, ``qwen3_0p6b``, ...) following
the same shape: a lazy ``transformers`` load behind a module ``_load`` seam (so
fake-model tests never download weights), ``embed`` / ``embed_batch`` /
``embed_query`` on the query/document asymmetry the model card documents, and a
``weights_present()`` cache check. :mod:`registry` maps each stable ``name`` to a
zero-arg factory so config-time imports pull no torch.
"""

from __future__ import annotations
