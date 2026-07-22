"""The single definition of the trackinizer HTTP API surface.

Holds the request/response bodies, filter and reference shapes, and the
route table. The FastAPI server registers from it and the client builds
requests from it, so neither can drift from the other.

This package imports only ``types``; it must never reach into the server
(``api`` / ``store`` / ``server``), ``fastapi``, ``asyncpg``, or the CLI
(``trax``). That boundary is what lets ``types/`` + ``wire/`` ship as a
standalone client distribution.

Submodules:
  - :mod:`.routes` -- the derived route table and path templates.
  - :mod:`.bodies` -- inquiry submit/edit request bodies.
  - :mod:`.edge_bodies` -- edge request bodies.
  - :mod:`.filters` -- list-query filter shapes.
  - :mod:`.refs` -- row addressing forms.
  - :mod:`.row_filter` -- row-vs-``Filter`` predicate shared by the
    server list route and the CLI test fake.
"""
