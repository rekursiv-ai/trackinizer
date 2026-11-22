"""The pipeline document: a campaign's stages, what flows between them, and what each holds.

    PipelineDoc     # the stages, each with its files and config keys, and the flows
    dump_pipeline   # ... as the JSON text an Artifact's description holds
    load_pipeline   # ... read back
    stage_of_path   # the stage a file of the campaign's code belongs to
    stage_of_key    # the stage a config key belongs to

A producer files the document as an Artifact labelled :data:`PIPELINE_LABEL` and
``pipeline:of:<brief Issue id>`` (:data:`PIPELINE_OF`), and a reader finds and keeps it
by the same rules as the files document (:mod:`.files_doc`). A path or key maps to the
stage that lists it exactly, else to the one with the longest prefix of it, a
directory ending in ``/`` or a key ending in ``.``; a tie goes to the earlier stage. The
web app's view maps them by this same rule. The document holds no time, so a pipeline
read again unchanged writes the same text.

This package is part of the publishable client distribution, so it must not
import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, Literal

import re

from treekle import json_dumps, json_loads


if TYPE_CHECKING:
    from collections.abc import Sequence


__all__ = [
    "PIPELINE_LABEL",
    "PIPELINE_OF",
    "Flow",
    "PipelineDoc",
    "Stage",
    "dump_pipeline",
    "load_pipeline",
    "stage_of_key",
    "stage_of_path",
]


PIPELINE_LABEL: Final = "pipeline"
"""The label of every pipeline document."""

PIPELINE_OF: Final = "pipeline:of:"
"""The prefix of the label that links a document to its campaign's brief Issue, by id."""

_STAGE_ID: Final = re.compile(r"[a-z0-9-]{1,32}")


@dataclass(frozen=True, slots=True, kw_only=True)
class Stage:
    """One step of the pipeline.

    Attributes:
      id: Its name in the flows: lowercase letters, digits and ``-``, at most 32.
      name: What it is called, such as ``Data loading``; at most 40 characters.
      role: One sentence saying what it does; at most 200 characters.
      paths: Files of the campaign's code it holds: a relative path, or a directory
        ending in ``/``; at most 200.
      keys: Config keys it reads, dotted where nested as the campaign view names
        them, or a prefix ending in ``.``; at most 200.

    """

    id: str
    name: str
    role: str
    paths: tuple[str, ...]
    keys: tuple[str, ...]

    def __post_init__(self) -> None:
        """Refuse a stage past a bound."""
        if not _STAGE_ID.fullmatch(self.id):
            raise ValueError(f"stage id {self.id!r} is not [a-z0-9-]{{1,32}}.")
        _bound("name", self.name, 40)
        _bound("role", self.role, 200)
        _bound("paths", self.paths, 200)
        _bound("keys", self.keys, 200)


@dataclass(frozen=True, slots=True, kw_only=True)
class Flow:
    """What passes from one stage to another.

    Attributes:
      source: The stage it leaves.
      target: The stage it reaches.
      label: What passes, such as ``batches``; at most 40 characters.

    """

    source: str
    target: str
    label: str = ""

    def __post_init__(self) -> None:
        """Refuse a label past its bound."""
        _bound("label", self.label, 40)


@dataclass(frozen=True, slots=True, kw_only=True)
class PipelineDoc:
    """A campaign's pipeline as read from one iteration's code.

    Attributes:
      base: The iteration it was read from, such as ``iter-00``.
      stages: From 1 to 12 stages, ids unique, in reading order.
      flows: At most 30, each between two different stages it names.
      version: The shape's version; a reader refuses any other, as the page does.

    """

    base: str
    stages: tuple[Stage, ...]
    flows: tuple[Flow, ...]
    version: Literal[1] = 1
    kind: Literal["pipeline"] = "pipeline"

    def __post_init__(self) -> None:
        """Refuse a document past a bound, or a flow to a stage it does not hold."""
        if len(self.stages) < 1 or len(self.stages) > 12:
            raise ValueError("a pipeline holds 1 to 12 stages.")
        ids = {stage.id for stage in self.stages}
        if len(ids) < len(self.stages):
            raise ValueError("stage ids must be unique.")
        _bound("flows", self.flows, 30)
        for flow in self.flows:
            if {flow.source, flow.target} - ids:
                raise ValueError(f"flow {flow.source}->{flow.target}: unknown stage.")
            if flow.source == flow.target:
                raise ValueError(f"flow {flow.source} leads to itself.")


def dump_pipeline(doc: PipelineDoc) -> str:
    """Return the document as the compact JSON text an Artifact's description holds.

    Args:
      doc: The document.

    Returns:
      text: Plain objects, arrays and scalars with no type tags, keys sorted.

    """
    return json_dumps(
        doc,
        target=PipelineDoc,
        separators=(",", ":"),
        sort_keys=True,
    )


def load_pipeline(text: str) -> PipelineDoc:
    """Read a pipeline document back.

    Args:
      text: What :func:`dump_pipeline` wrote.

    Returns:
      doc: The document.

    Raises:
      ValueError: ``text`` is not a pipeline document: another kind or version, a
        field it does not know, a bound broken, or a flow to a stage it does not hold.

    """
    doc = json_loads(text, PipelineDoc)
    # A read restores state as written; building again runs each bound check.
    return replace(
        doc,
        stages=tuple(replace(stage) for stage in doc.stages),
        flows=tuple(replace(flow) for flow in doc.flows),
    )


def stage_of_path(doc: PipelineDoc, path: str) -> str:
    """Return the id of the stage a file of the code maps to, or ``""`` if none."""
    return _entry_stage(path, held=[(s.id, s.paths) for s in doc.stages], end="/")


def stage_of_key(doc: PipelineDoc, key: str) -> str:
    """Return the id of the stage a config key maps to, or ``""`` if none."""
    return _entry_stage(key, held=[(s.id, s.keys) for s in doc.stages], end=".")


def _bound(name: str, value: Sequence[object], most: int) -> None:
    """Refuse ``value`` when it is longer than ``most``."""
    if len(value) > most:
        raise ValueError(f"{name} holds {len(value)}, more than {most}.")


def _entry_stage(
    name: str,
    *,
    held: Sequence[tuple[str, tuple[str, ...]]],
    end: str,
) -> str:
    """Return the stage whose entry is ``name``, else whose prefix of it is longest."""
    found = [
        (entry == name, len(entry), -at, stage)
        for at, (stage, entries) in enumerate(held)
        for entry in entries
        if entry == name or (entry.endswith(end) and name.startswith(entry))
    ]
    return max(found)[3] if found else ""
