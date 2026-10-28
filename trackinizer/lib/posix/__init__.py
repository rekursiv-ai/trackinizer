"""POSIX primitives the standard library exposes incompletely.

Kernel facilities with no usable stdlib surface: a regular file cannot be
waited on and inotify has no module at all (:mod:`trackinizer.lib.posix.follow`); a
pseudo-terminal has no async interface and no notion of "type this line into
the TUI" (:mod:`trackinizer.lib.posix.terminal`); handing a real terminal to such a
child, then getting it back intact, is :mod:`trackinizer.lib.posix.relay`; and a
child's process group, which asyncio neither signals nor stops whole, nor
keeps through a cancel, is :mod:`trackinizer.lib.posix.child`.

Stdlib only, and nothing here imports anything else in this repository.
"""
