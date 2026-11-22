"""Per-CLI adapters that turn a session-log line into an ``Event``.

This package exposes nothing at its root: import each name straight from the
module that defines it -- the ``Adapter`` protocol and ``Event`` record live in
:mod:`.base`, each concrete adapter in its own module. See ``STYLE.md`` "import
from the definition module".
"""
