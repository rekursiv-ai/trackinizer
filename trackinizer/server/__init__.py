"""Trackinizer server: storage, HTTP routes, and the running app.

Imports ``types`` and ``wire`` plus server-only deps (FastAPI, asyncpg).
The publishable client never imports this package, which keeps the client
free of server dependencies.
"""
