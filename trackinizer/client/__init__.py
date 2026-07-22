"""Standalone HTTP client SDK for trackinizer.

Imports only ``types``, ``wire``, and ``httpx``. It must never reach into
the server (``api`` / ``store`` / ``server``), ``fastapi``, ``asyncpg``,
or the CLI (``trax``), so ``types/`` + ``wire/`` + ``client/`` stay a
self-contained, separately-publishable distribution.
"""
