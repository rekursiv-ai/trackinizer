"""The v1 footprint policy: prose F(+E), machine-output heads, bodies blobbed.

The ``docs/private/session_indexing.md`` footprint table as a
:class:`~trackinizer.server.semantic_mapper.SemanticMapper`. Four
classes of record:

- EMBEDDED PROSE -- messages, tool-call args, compaction summaries, web
  results: the whole text, term-searched AND embedded, chunked when long.
- FTS-ONLY PROSE -- system prompts, thinking, incomplete records: the whole
  text on the term surface only, no vector. ``SystemMessage`` moved here by
  operator decision 2026-09-20 (system prompts are near-duplicate spam);
  ``Thinking`` and ``IncompleteRecord`` are prose that is not worth a vector.
- HEADED -- shell/script output, stream captures, and file write/edit
  receipts: the first :data:`HEAD_CHARS` characters, term-searched but NOT
  embedded (machine output = F(head)+B per the doc). The verdict line
  ("Script completed", the traceback, the path) lives in the head; the tail
  is replay material.
- SILENT -- file reads (the repo is the searchable source of file content),
  telemetry (``AgentStatusResult`` etc.), and anything unrecognized: no units.

``Thinking`` is F-only prose here: its stored text is already only the
readable plaintext -- ``search_text`` excludes sealed bytes by construction.

An ``embed=False`` unit rides the tsvector surface and stores no
``session_embeddings`` row. Freshness is tracked by ``session_index_state``,
NOT by the presence of an embedding row, so an fts-only record is swept once
and then reads up-to-date (``store/session_embed.py``).
"""

from __future__ import annotations

from typing import Final

from trackinizer.server.semantic_mapper import IndexUnit


__all__ = [
    "CHUNK_CHARS",
    "CHUNK_OVERLAP_CHARS",
    "HEAD_CHARS",
    "FootprintMapper",
]


HEAD_CHARS: Final = 2_000
"""Head kept from machine output. A dial, not architecture: measured live
(2026-09-19), the largest single tool result is 42 kB of already-truncated
script output whose verdict is in line one."""

CHUNK_CHARS: Final = 1_500
"""Chunk size for long prose, the unit the vector-count estimate in the
design doc was measured at."""

CHUNK_OVERLAP_CHARS: Final = 200
"""Overlap between adjacent chunks, so a sentence split across a boundary
still matches one of them."""


class FootprintMapper:
    """The session_indexing.md footprint table, applied per record kind."""

    # KEEP the name "footprint-v1" across this policy narrowing (SystemMessage,
    # Thinking, IncompleteRecord and the stream/tool heads dropping to
    # embed=False). ``session_embeddings`` keys on ``(model, mapper)``, so a new
    # name would orphan every existing vector and force a re-embed of the ~1.26M
    # still-valid ones. The narrowed policy makes the current vectors a strict
    # SUBSET of what v1 would produce today; a subset is correct to keep, and the
    # corpus-cleanup SQL prunes only the rows the narrowing removed. Bump the name
    # only for a change that would make an EXISTING vector wrong, not merely
    # surplus.
    name = "footprint-v1"

    @property
    def indexed_kinds(self) -> frozenset[str]:
        """Kinds that yield >=1 unit for non-empty text (everything else is ()).

        The mapper is the authority on what is indexed; the backfill scanner and
        its pending-count bind THIS set (plus a non-empty-text check) into their
        SQL predicate, so a never-indexed kind (telemetry: ``AgentStatusResult``,
        ``ContextState``, ``TokenUsage``, ...) or an empty-text row is never
        counted pending forever. Sharing the set here keeps the predicate from
        drifting off :meth:`units`. A superset of :attr:`embedded_kinds`: an
        fts-only kind is indexed (term surface + freshness marker) but unembedded.
        """
        return _EMBEDDED_PROSE | _FTS_ONLY_PROSE | _HEADED

    @property
    def embedded_kinds(self) -> frozenset[str]:
        """Kinds that yield >=1 ``embed=True`` unit (the vector surface).

        The subset of :attr:`indexed_kinds` whose units get vectors -- the
        F+E prose rows. Machine-output heads and fts-only prose are indexed but
        not embedded, so they are absent here. The corpus-cleanup SQL prunes
        ``session_embeddings`` rows whose kind left this set.
        """
        return _EMBEDDED_PROSE

    def units(self, *, kind: str, text: str) -> tuple[IndexUnit, ...]:
        """Return the indexable units for one stored record.

        Args:
          kind: The ``session_records.kind`` (a record class name).
          text: The row's stored search projection.

        Returns:
          units: Chunked prose (embedded or fts-only), a single head, or
            nothing, per the ``session_indexing.md`` footprint table.

        """
        if not text:
            return ()
        if kind in _EMBEDDED_PROSE:
            return _chunked(text, embed=True)
        if kind in _FTS_ONLY_PROSE:
            return _chunked(text, embed=False)
        if kind in _HEADED:
            return (IndexUnit(text=text[:HEAD_CHARS], field="head", embed=False),)
        return ()


# Prose that is both term-searched and embedded (doc footprint table, "F,E").
_EMBEDDED_PROSE: Final = frozenset(
    {
        "UserMessage",
        "AssistantMessage",
        "AgentToAgentMessage",
        "ToolCall",
        "ContextCompaction",
        "WebFetchResult",
        "WebSearchResults",
    },
)

# Prose kept on the term surface only, never embedded (doc footprint table, "F").
# SystemMessage: operator decision 2026-09-20 -- system prompts are near-duplicate
# spam, so they term-search but do not earn a vector.
_FTS_ONLY_PROSE: Final = frozenset(
    {
        "SystemMessage",
        "Thinking",
        "IncompleteRecord",
    },
)

# Machine output: the first HEAD_CHARS term-searched, the body blobbed, no vector
# (doc footprint table, "F(head)+B"). Stream captures are machine output too.
_HEADED: Final = frozenset(
    {
        "ShellCommandResult",
        "UncategorizedToolResult",
        "FileWriteResult",
        "FileEditResult",
        "Stdout",
        "Stderr",
        "Stdin",
    },
)


def _chunked(text: str, *, embed: bool) -> tuple[IndexUnit, ...]:
    """Split prose into overlapping chunks; one unit when it fits."""
    if len(text) <= CHUNK_CHARS:
        return (IndexUnit(text=text, embed=embed),)
    step = CHUNK_CHARS - CHUNK_OVERLAP_CHARS
    return tuple(
        IndexUnit(
            text=text[start : start + CHUNK_CHARS],
            chunk=ordinal,
            # Terms already ride on the tsvector via chunk 0's whole-text
            # row; later chunks exist for the vector surface only.
            fts=ordinal == 0,
            embed=embed,
        )
        for ordinal, start in enumerate(range(0, len(text), step))
        if text[start : start + CHUNK_CHARS]
    )
