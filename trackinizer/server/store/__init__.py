""":class:`Store` and its mixin modules.

The persistence layer for trackinizer. :class:`Store` (in :mod:`.core`)
composes mixins -- submit / read / edit / edge / session / cascade -- each
living in its own module here, over the shared lifecycle and embedding base.

This package exposes nothing at its root: import each name straight from the
module that defines it. See ``STYLE.md`` "import from the definition module".
"""
