"""The trackinizer type model: dataclasses, Protocols, and column metadata.

Nothing here imports from outside the package, so ``types/`` stands on its
own. The shapes declared here are what every other module builds on top of
over Postgres and HTTP.

This package exposes nothing at its root: import each name straight from the
submodule that defines it (per ``STYLE.md`` "import from the definition
module"). The model is big enough that a flat namespace would hide where each
name lives:

  - :mod:`.change_log` -- ``Change``, ``Snapshot``.
  - :mod:`.columns` -- ``ColumnSpec``, ``Row``, ``column_specs``.
  - :mod:`.cost` -- ``Cost``.
  - :mod:`.edges` -- ``Edge``, ``EdgeKindPolicy``, ``EDGE_POLICIES``.
  - :mod:`.embedder` -- the ``Embedder`` Protocol.
  - :mod:`.errors` -- ``ConflictError``.
  - :mod:`.inquiries` -- the ``Inquiry`` hierarchy.

The HTTP bodies and route table live in the sibling
:mod:`trackinizer.wire` package, which imports ``types``
and never the other way around.
"""
