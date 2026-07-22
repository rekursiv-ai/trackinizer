"""Thin HTTP client for a running trackinizer server.

Server URLs live in named profiles. ``trax profile`` shows the active one;
``trax profile foo`` shows a named one; adding ``token TOKEN`` mutates it.

The grammar is subject-first. A bare subject shows the current view; adding
a selector picks a row; adding a field projects it; adding ``is value``
mutates. Trailing ``del`` deletes rows, removes list values, or unlinks edges.
"""
