"""HTTP boundary for trackinizer.

Routes stay thin: validate the wire body with pydantic, call one
``Store`` method, serialize the result. Anything non-trivial belongs in
``store.py``, not in a route.

This package exposes nothing at its root: import each name straight from the
module that defines it (the FastAPI app lives in :mod:`.app`). See ``STYLE.md``
"import from the definition module".
"""
