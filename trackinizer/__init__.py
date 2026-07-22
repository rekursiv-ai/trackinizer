"""Trackinizer: centralized agent database for Inquiries (Issues + Artifacts).

The Python type universe in :mod:`.types` is the design contract;
the SQL schema (``assets/schema.sql``) and the FastAPI :class:`.store.Store`
realize it. See ``docs/design.md`` for the full narrative and
``README.md`` for the module layout.
"""
