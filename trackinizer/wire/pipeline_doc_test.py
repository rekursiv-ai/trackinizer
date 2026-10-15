"""Tests for the pipeline document's shape, bounds, labels and mapping rule."""

from __future__ import annotations

from dataclasses import replace

import json

from treekle import json_loads

import pytest

from trackinizer.wire.pipeline_doc import (
    PIPELINE_LABEL,
    PIPELINE_OF,
    Flow,
    PipelineDoc,
    Stage,
    dump_pipeline,
    load_pipeline,
    stage_of_key,
    stage_of_path,
)


def _stage(
    id: str,
    *,
    paths: tuple[str, ...] = (),
    keys: tuple[str, ...] = (),
) -> Stage:
    return Stage(id=id, name=id.title(), role=f"Does {id}.", paths=paths, keys=keys)


def _doc() -> PipelineDoc:
    return PipelineDoc(
        base="iter-00",
        stages=(
            _stage("data", paths=("data/", "load.py"), keys=("data.",)),
            _stage("train", paths=("train.py",), keys=("optim.", "seed")),
        ),
        flows=(Flow(source="data", target="train", label="batches"),),
    )


def test_a_pipeline_document_reads_back_as_written() -> None:
    doc = replace(_doc(), flows=(Flow(source="data", target="train"),))

    assert load_pipeline(dump_pipeline(doc)) == doc


def test_the_json_is_compact_sorted_and_holds_exactly_the_contract_keys() -> None:
    text = dump_pipeline(replace(_doc(), flows=(Flow(source="data", target="train"),)))

    assert text == (
        '{"base":"iter-00","flows":[{"label":"","source":"data","target":"train"}],'
        '"kind":"pipeline",'
        '"stages":[{"id":"data","keys":["data."],"name":"Data",'
        '"paths":["data/","load.py"],"role":"Does data."},'
        '{"id":"train","keys":["optim.","seed"],"name":"Train",'
        '"paths":["train.py"],"role":"Does train."}],"version":1}'
    )


def test_a_flow_written_without_a_label_reads_back_with_an_empty_one() -> None:
    tree = json_loads(dump_pipeline(_doc()), dict[str, object])
    tree["flows"] = [{"source": "data", "target": "train"}]

    assert load_pipeline(json.dumps(tree)).flows == (
        Flow(source="data", target="train"),
    )


@pytest.mark.parametrize(
    ("stage", "named"),
    [
        ({"id": "Data"}, "stage id"),
        ({"id": ""}, "stage id"),
        ({"id": "a" * 33}, "stage id"),
        ({"name": "n" * 41}, "name"),
        ({"role": "r" * 201}, "role"),
        ({"paths": ["p"] * 201}, "paths"),
        ({"keys": ["k"] * 201}, "keys"),
    ],
    ids=["upper", "empty", "id-33", "name-41", "role-201", "paths-201", "keys-201"],
)
def test_a_stage_past_a_bound_is_refused(stage: dict[str, object], named: str) -> None:
    tree = json_loads(dump_pipeline(_doc()), dict[str, object])
    first = {**json_loads(json.dumps(tree["stages"]), list[dict[str, object]])[0]}
    tree["stages"] = [first | stage]
    tree["flows"] = []

    with pytest.raises(ValueError, match=named):
        _ = load_pipeline(json.dumps(tree))


def test_a_stage_at_its_bounds_is_read() -> None:
    stage = Stage(
        id="a" * 32,
        name="n" * 40,
        role="r" * 200,
        paths=("p",) * 200,
        keys=("k",) * 200,
    )
    doc = PipelineDoc(
        base="iter-00",
        stages=(stage, *(_stage(f"s{n}") for n in range(11))),
        flows=(Flow(source="s0", target="s1", label="l" * 40),) * 30,
    )

    assert load_pipeline(dump_pipeline(doc)) == doc


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ({"stages": []}, "1 to 12 stages"),
        ({"stages": [_stage(f"s{n}") for n in range(13)], "flows": []}, "1 to 12"),
        ({"stages": [_stage("data"), _stage("data")], "flows": []}, "unique"),
        ({"flows": [Flow(source="data", target="train")] * 31}, "flows holds 31"),
        ({"flows": [Flow(source="data", target="eval")]}, "unknown stage"),
        ({"flows": [Flow(source="test", target="train")]}, "unknown stage"),
        ({"flows": [Flow(source="data", target="data")]}, "itself"),
    ],
    ids=[
        "no-stage",
        "13-stages",
        "same-id",
        "31-flows",
        "unknown-target",
        "unknown-source",
        "self-loop",
    ],
)
def test_a_document_past_a_bound_or_with_a_broken_reference_is_refused(
    change: dict[str, list[Stage] | list[Flow]],
    named: str,
) -> None:
    doc = _doc()
    stages = change.get("stages", doc.stages)
    flows = change.get("flows", doc.flows)

    with pytest.raises(ValueError, match=named):
        _ = PipelineDoc(
            base=doc.base,
            stages=tuple(s for s in stages if isinstance(s, Stage)),
            flows=tuple(f for f in flows if isinstance(f, Flow)),
        )


def test_a_flow_label_past_its_bound_is_refused() -> None:
    with pytest.raises(ValueError, match="label holds 41"):
        _ = Flow(source="data", target="train", label="l" * 41)


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ({"kind": "files"}, "'kind'"),
        ({"version": 2}, "cannot read 2"),
        ({"session": "x"}, "unknown field"),
    ],
    ids=["kind-not-pipeline", "version-2", "unknown-field"],
)
def test_a_document_of_another_shape_is_not_read_as_a_pipeline(
    change: dict[str, object],
    named: str,
) -> None:
    tree = json_loads(dump_pipeline(_doc()), dict[str, object]) | change

    with pytest.raises(ValueError, match=named):
        _ = load_pipeline(json.dumps(tree))


def _ruled() -> PipelineDoc:
    return PipelineDoc(
        base="iter-00",
        stages=(
            _stage("short", paths=("src/", "a/x.py"), keys=("optim.", "train.lr")),
            _stage("long", paths=("src/model/", "a/"), keys=("optim.adam.",)),
            _stage("tie", paths=("src/", "src"), keys=("optim.", "optimizer")),
        ),
        flows=(),
    )


@pytest.mark.parametrize(
    ("path", "stage"),
    [
        ("a/x.py", "short"),
        ("a/y.py", "long"),
        ("src/model/net.py", "long"),
        ("src/train.py", "short"),
        ("ab/x.py", ""),
        ("src", "tie"),
        ("srcx/y.py", ""),
        ("other.py", ""),
    ],
    ids=[
        "exact-beats-prefix",
        "prefix",
        "longer-prefix",
        "tie-to-first",
        "a/-not-ab/",
        "exact-without-slash",
        "no-slash-no-prefix",
        "unmapped",
    ],
)
def test_a_path_maps_to_one_stage_by_the_shared_rule(path: str, stage: str) -> None:
    assert stage_of_path(_ruled(), path) == stage


@pytest.mark.parametrize(
    ("key", "stage"),
    [
        ("train.lr", "short"),
        ("optim.lr", "short"),
        ("optim.adam.beta", "long"),
        ("optimizer", "tie"),
        ("optim", ""),
        ("seed", ""),
    ],
    ids=[
        "exact",
        "tie-to-first",
        "longer",
        "optim.-not-optimizer",
        "no-dot",
        "unmapped",
    ],
)
def test_a_key_maps_to_one_stage_by_the_shared_rule(key: str, stage: str) -> None:
    assert stage_of_key(_ruled(), key) == stage


def test_the_labels_are_the_contracts() -> None:
    assert (PIPELINE_LABEL, PIPELINE_OF) == ("pipeline", "pipeline:of:")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
