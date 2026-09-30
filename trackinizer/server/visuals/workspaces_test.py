"""Visual operations preserve context and reject malformed module input."""

from __future__ import annotations

import uuid

from pydantic import ValidationError

import pytest

from trackinizer.server.visuals.catalog import (
    ParameterDescription,
    VisualCatalogBody,
    VisualDescription,
    default_catalog,
)
from trackinizer.server.visuals.workspaces import (
    ApplyWorkspaceOperation,
    ShowVisual,
    WorkspaceData,
    WorkspaceMessageRequest,
    apply_operation,
    initial_data,
)


def test_show_reuses_visual_type_and_updates_its_context() -> None:
    """Reopening Chat updates its target without orphaning a duplicate tile."""
    catalog = default_catalog()
    original = apply_operation(
        initial_data(catalog),
        ShowVisual(kind="show", visual_type="trax.chat"),
        catalog,
    )
    chat_id = original.visuals[1].id
    record_id = uuid.uuid4()
    reopened = apply_operation(
        original,
        ShowVisual(kind="show", visual_type="trax.chat", record_id=record_id),
        catalog,
    )
    assert len(reopened.visuals) == 2
    assert reopened.visuals[1].id == chat_id
    assert reopened.visuals[1].record_id == record_id
    assert reopened.focused_instance == chat_id


def test_show_uses_bounded_catalog_defaults_and_requires_record() -> None:
    """A contributed module can be enabled with its safe default parameters."""
    catalog = VisualCatalogBody(
        default_visual="trax.subgraph",
        visuals=[
            VisualDescription(
                type="trax.subgraph",
                version=1,
                title="Subgraph",
                description="Bounded lineage.",
                requires=["record"],
                default_size="wide",
                parameter_schema={
                    "depth": ParameterDescription(
                        type="integer",
                        default=2,
                        minimum=1,
                        maximum=3,
                    ),
                },
            ),
        ],
    )
    with pytest.raises(ValueError, match="record"):
        apply_operation(
            WorkspaceData(visuals=[]),
            ShowVisual(kind="show", visual_type="trax.subgraph"),
            catalog,
        )
    record_id = uuid.uuid4()
    shown = apply_operation(
        WorkspaceData(visuals=[]),
        ShowVisual(kind="show", visual_type="trax.subgraph", record_id=record_id),
        catalog,
    )
    assert shown.visuals[0].params == {"depth": 2}
    assert shown.visuals[0].record_id == record_id


def test_show_rejects_unbounded_string_parameter() -> None:
    """A large agent parameter cannot be copied into every receipt."""
    catalog = VisualCatalogBody(
        default_visual="trax.search",
        visuals=[
            VisualDescription(
                type="trax.search",
                version=1,
                title="Search",
                description="Search records.",
                requires=[],
                default_size="wide",
                parameter_schema={
                    "query": ParameterDescription(
                        type="string",
                        default="",
                        max_length=8,
                    ),
                },
            ),
        ],
    )
    with pytest.raises(ValueError, match="maximum length"):
        apply_operation(
            WorkspaceData(visuals=[]),
            ShowVisual(
                kind="show",
                visual_type="trax.search",
                params={"query": "ninechars"},
            ),
            catalog,
        )


def test_operation_rejects_misspelled_record_context() -> None:
    """Unknown fields fail instead of silently discarding agent context."""
    with pytest.raises(ValidationError, match="recordId"):
        ApplyWorkspaceOperation.model_validate(
            {
                "revision": 0,
                "operation": {
                    "kind": "show",
                    "visual_type": "trax.chat",
                    "recordId": str(uuid.uuid4()),
                },
            },
        )


def test_chat_artifact_uses_the_same_record_target() -> None:
    """Chat uses the Artifact ID as its sole target and rejects a second shape."""
    artifact_id = uuid.uuid4()
    shown = apply_operation(
        initial_data(default_catalog()),
        ShowVisual(kind="show", visual_type="trax.chat", record_id=artifact_id),
        default_catalog(),
    )
    assert shown.visuals[-1].record_id == artifact_id
    with pytest.raises(ValidationError):
        ShowVisual.model_validate(
            {
                "kind": "show",
                "visual_type": "trax.chat",
                "record_id": str(artifact_id),
                "report_target": {"artifact_id": str(artifact_id)},
            },
        )
    with pytest.raises(ValidationError):
        WorkspaceMessageRequest.model_validate(
            {
                "text": "hello",
                "expected_report_target": {"artifact_id": str(artifact_id)},
            },
        )


def test_artifact_visual_requires_record_target() -> None:
    """An Artifact tile uses the same record target as other visuals."""
    catalog = default_catalog()
    with pytest.raises(ValueError, match="record target"):
        apply_operation(
            initial_data(catalog),
            ShowVisual(kind="show", visual_type="trax.artifact"),
            catalog,
        )
    artifact_id = uuid.uuid4()
    shown = apply_operation(
        initial_data(catalog),
        ShowVisual(kind="show", visual_type="trax.artifact", record_id=artifact_id),
        catalog,
    )
    assert shown.visuals[-1].record_id == artifact_id


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
