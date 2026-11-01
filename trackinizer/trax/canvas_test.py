"""``trax canvas``: what each form sends to the canvas, and what it refuses."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import re
import uuid

from treekle import from_plain

import pytest

from trackinizer.client.client import Client
from trackinizer.client.errors import ClientError
from trackinizer.trax import cli


if TYPE_CHECKING:
    from collections.abc import Sequence


_CATALOG: dict[str, object] = {
    "default_visual": "trax.browse",
    "visuals": [
        {"type": "trax.browse", "parameter_schema": {}},
        {"type": "trax.subgraph", "parameter_schema": {}},
        {
            "type": "x.demo",
            "parameter_schema": {
                "label": {"type": "string"},
                "count": {"type": "integer"},
                "flag": {"type": "boolean"},
            },
        },
    ],
}


class _CanvasClient:
    """A canvas that records each operation and navigation sent to it."""

    def __init__(self) -> None:
        self.operations: list[dict[str, object]] = []
        self.navigations: list[str] = []
        self.highlights: list[list[uuid.UUID]] = []
        self.chat_windows: list[bool] = []
        self.preferences: list[dict[str, object]] = []
        self.reads: list[str] = []
        self.canvas = uuid.uuid4()

    def get(self, path: str) -> dict[str, object]:
        self.reads.append(path)
        return _CATALOG

    def read_canvas(self, canvas_id: uuid.UUID) -> dict[str, object]:
        self.reads.append(f"/api/canvases/{canvas_id}")
        return self._state(8, visuals=[])

    def apply_canvas_operation(
        self,
        canvas_id: uuid.UUID,
        *,
        operation: dict[str, object],
    ) -> dict[str, object]:
        self._at(canvas_id)
        self.operations.append(operation)
        if operation["kind"] != "mark":
            return self._state(9, visuals=[])
        record = str(self.canvas)
        return {
            **self._state(9, visuals=[]),
            "record_labels": {record: "Experiment#12 Measured tails"},
            "marks": [
                {
                    "id": "m1",
                    "author": "agent",
                    "page": "#/graph",
                    "covers": {"records": [record], "instances": ["inst-1"]},
                },
                {"id": "m2", "author": "jp", "covers": {}, "window": True},
            ],
        }

    def navigate(
        self,
        canvas_id: uuid.UUID,
        *,
        route: str,
    ) -> dict[str, object]:
        self._at(canvas_id)
        self.navigations.append(route)
        return self._state(8, visuals=[])

    def highlight(
        self,
        canvas_id: uuid.UUID,
        *,
        ids: Sequence[uuid.UUID],
    ) -> dict[str, object]:
        self._at(canvas_id)
        self.highlights.append(list(ids))
        return self._state(8, visuals=[])

    def chat_window(
        self,
        canvas_id: uuid.UUID,
        *,
        hidden: bool,
    ) -> dict[str, object]:
        self._at(canvas_id)
        self.chat_windows.append(hidden)
        return self._state(8, visuals=[])

    def set_preferences(
        self,
        canvas_id: uuid.UUID,
        *,
        change: dict[str, object],
    ) -> dict[str, object]:
        self._at(canvas_id)
        self.preferences.append(dict(change))
        return {
            **self._state(8, visuals=[]),
            "preferences": {"theme": "light", "highlight_mentions": False},
        }

    def _state(self, revision: int, *, visuals: list[object]) -> dict[str, object]:
        return {"id": str(self.canvas), "revision": revision, "visuals": visuals}

    def _at(self, canvas_id: uuid.UUID) -> None:
        """Refuse a call that names any canvas but this one."""
        assert canvas_id == self.canvas, canvas_id


def _canvas(client: _CanvasClient, *arguments: str) -> None:
    cli.parse_and_run(
        ["canvas", str(client.canvas), *arguments],
        client_factory=lambda: cast(Client, client),
    )


def test_canvas_with_no_action_reads_and_renders_the_canvas(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _CanvasClient()

    _canvas(client)

    assert client.reads == [f"/api/canvases/{client.canvas}"]
    assert f"canvas {client.canvas} revision 8" in capsys.readouterr().out


def _bare(client: _CanvasClient, *arguments: str) -> None:
    """Run `trax canvas` with no id."""
    cli.parse_and_run(
        ["canvas", *arguments],
        client_factory=lambda: cast(Client, client),
    )


def test_canvas_with_no_id_uses_the_canvas_the_session_was_placed_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _CanvasClient()
    monkeypatch.setenv("TRAX_CANVAS", str(client.canvas))
    instance = uuid.uuid4()

    _bare(client)
    _bare(client, "show", "trax.subgraph", "--record", str(uuid.uuid4()))
    _bare(client, "place", str(instance), "left")

    assert client.reads[0] == f"/api/canvases/{client.canvas}"
    shown, placed = client.operations
    assert shown["kind"] == "show"
    assert shown["visual_type"] == "trax.subgraph"
    assert (placed["kind"], placed["instance_id"]) == ("place", str(instance))


def test_canvas_with_no_id_reads_the_ssh_forwarded_canvas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _CanvasClient()
    monkeypatch.delenv("TRAX_CANVAS", raising=False)
    monkeypatch.setenv("LC_TRAX_CANVAS", str(client.canvas))

    _bare(client)

    assert client.reads[0] == f"/api/canvases/{client.canvas}"


def test_canvas_with_no_id_and_no_canvas_says_what_to_give(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRAX_CANVAS", raising=False)
    monkeypatch.delenv("LC_TRAX_CANVAS", raising=False)

    refusal = (
        "no canvas: give its UUID, or run inside a recorded agent run placed on a "
        "canvas ($TRAX_CANVAS)"
    )
    with pytest.raises(ClientError, match=f"^{re.escape(refusal)}$"):
        _bare(_CanvasClient())


def test_an_explicit_canvas_id_wins_over_the_placed_canvas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _CanvasClient()
    monkeypatch.setenv("TRAX_CANVAS", str(uuid.uuid4()))

    _canvas(client)

    assert client.reads == [f"/api/canvases/{client.canvas}"]


def test_canvas_show_sends_one_operation_through_the_clients_one_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _CanvasClient()
    record = uuid.uuid4()
    instance = uuid.uuid4()

    _canvas(
        client,
        "show",
        "trax.subgraph",
        "--record",
        str(record),
        "--placement",
        "side",
    )
    _canvas(client, "hide", str(instance))
    _canvas(client, "focus", str(instance))
    _canvas(client, "place", str(instance), "floating")
    _canvas(client, "place", str(instance), "left")

    assert client.operations == [
        {
            "kind": "show",
            "visual_type": "trax.subgraph",
            "placement": "side",
            "record_id": str(record),
        },
        {"kind": "hide", "instance_id": str(instance)},
        {"kind": "focus", "instance_id": str(instance)},
        {"kind": "place", "instance_id": str(instance), "placement": "floating"},
        {"kind": "place", "instance_id": str(instance), "placement": "left"},
    ]
    assert client.reads == []
    assert f"canvas {client.canvas} revision 9" in capsys.readouterr().out


def test_canvas_show_reads_each_param_as_the_type_the_catalog_gives_it() -> None:
    """A value that looks like a number is still a string where the catalog says so."""
    client = _CanvasClient()

    _canvas(
        client,
        "show",
        "x.demo",
        "--param",
        "label=123",
        "--param",
        "count=-5",
        "--param",
        "flag=true",
    )
    _canvas(client, "show", "x.demo", "--param", "flag=false")

    assert client.operations == [
        {
            "kind": "show",
            "visual_type": "x.demo",
            "params": {"label": "123", "count": -5, "flag": True},
        },
        {"kind": "show", "visual_type": "x.demo", "params": {"flag": False}},
    ]


def test_canvas_show_without_params_does_not_read_the_catalog() -> None:
    client = _CanvasClient()

    _canvas(client, "show", "trax.subgraph")

    assert "/api/visuals" not in client.reads


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["show", "x.demo", "--param", "label"], "KEY=VALUE"),
        (["show", "x.demo", "--param", "=x"], "KEY=VALUE"),
        (["show", "x.demo", "--param", "a b=x"], "KEY=VALUE"),
        (
            ["show", "x.demo", "--param", "label=a", "--param", "label=b"],
            "more than once",
        ),
        (["show", "x.demo", "--param", "color=red"], "takes: label, count, flag"),
        (["show", "trax.browse", "--param", "route=#/graph"], "takes: none"),
        (["show", "x.demo", "--param", "count=many"], "needs an integer"),
        (["show", "x.demo", "--param", "flag=yes"], "needs true or false"),
        (["show", "nothing.here", "--param", "a=b"], "not a visual in the catalog"),
        (["hide", str(uuid.uuid4()), "--param", "label=a"], "only with show"),
        (["--param", "label=a"], "does not accept operation arguments"),
    ],
)
def test_canvas_refuses_a_param_it_cannot_send(
    arguments: list[str],
    message: str,
) -> None:
    client = _CanvasClient()

    with pytest.raises(ClientError, match=message):
        _canvas(client, *arguments)

    assert client.operations == []


def test_canvas_navigate_moves_the_page_without_an_operation() -> None:
    client = _CanvasClient()

    _canvas(client, "navigate", "#/search/a%3Fb")

    assert client.navigations == ["#/search/a%3Fb"]
    assert client.operations == []
    assert client.reads == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["navigate"],
        ["navigate", "#/graph", "#/console"],
        ["navigate", "#/graph", "--record", str(uuid.uuid4())],
    ],
)
def test_canvas_navigate_takes_exactly_one_route(arguments: list[str]) -> None:
    client = _CanvasClient()

    with pytest.raises(ClientError, match="navigate requires one route"):
        _canvas(client, *arguments)

    assert client.navigations == []


def test_canvas_hide_or_show_chat_moves_the_window_without_an_operation() -> None:
    client = _CanvasClient()

    _canvas(client, "hide", "chat")
    _canvas(client, "show", "chat")

    assert client.chat_windows == [True, False]
    assert client.operations == []
    assert client.reads == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["hide", "chat", "--record", str(uuid.uuid4())],
        ["show", "chat", "--placement", "main"],
        ["show", "chat", "--param", "hops=2"],
    ],
)
def test_canvas_chat_takes_no_other_argument(arguments: list[str]) -> None:
    client = _CanvasClient()

    with pytest.raises(ClientError, match="chat takes no other argument"):
        _canvas(client, *arguments)

    assert client.chat_windows == []
    assert client.reads == []


def test_canvas_chat_is_not_a_verb_of_its_own() -> None:
    client = _CanvasClient()

    with pytest.raises(ClientError, match="unknown canvas action 'chat'"):
        _canvas(client, "chat", "hide")

    assert client.chat_windows == []


def test_canvas_key_to_value_sets_the_owners_preference_as_json_or_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _CanvasClient()

    _canvas(client, "theme", "to", "light")
    _canvas(client, "highlight_mentions", "to", "false")
    _canvas(client, "theme", "to", "null")

    assert client.preferences == [
        {"theme": "light"},
        {"highlight_mentions": False},
        {"theme": None},
    ]
    assert client.operations == []
    assert client.reads == []
    assert (
        '  preferences highlight_mentions=false theme="light"'
        in capsys.readouterr().out.splitlines()
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["theme", "to"],
        ["theme", "to", "light", "dark"],
        ["theme", "to", "light", "--record", str(uuid.uuid4())],
        ["theme", "to", "light", "--placement", "main"],
        ["theme", "to", "light", "--param", "a=b"],
    ],
)
def test_canvas_key_to_takes_one_value_and_nothing_else(
    arguments: list[str],
) -> None:
    client = _CanvasClient()

    with pytest.raises(
        ClientError,
        match=r"^theme to takes one VALUE and no other argument$",
    ):
        _canvas(client, *arguments)

    assert client.preferences == []


def test_canvas_highlight_sends_the_ids_without_an_operation() -> None:
    client = _CanvasClient()
    first, second = uuid.uuid4(), uuid.uuid4()

    _canvas(client, "highlight", f"{first},{second}")
    _canvas(client, "highlight", "")

    assert client.highlights == [[first, second], []]
    assert client.operations == []
    assert client.reads == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["highlight"],
        ["highlight", str(uuid.uuid4()), str(uuid.uuid4())],
        ["highlight", str(uuid.uuid4()), "--record", str(uuid.uuid4())],
        ["highlight", str(uuid.uuid4()), "--placement", "main"],
    ],
)
def test_canvas_highlight_takes_one_list_of_uuids(arguments: list[str]) -> None:
    client = _CanvasClient()
    refusal = "highlight requires UUID[,UUID...] ('' clears)"

    with pytest.raises(ClientError, match=f"^{re.escape(refusal)}$"):
        _canvas(client, *arguments)

    assert client.highlights == []


def test_canvas_highlight_refuses_a_ref_that_is_not_a_uuid() -> None:
    client = _CanvasClient()

    with pytest.raises(ClientError, match=r"^record is not a valid UUID: 'Issue#12'$"):
        _canvas(client, "highlight", "Issue#12")

    assert client.highlights == []


def test_canvas_rejects_a_malformed_id_by_name_and_value() -> None:
    """A mistyped canvas id is named in the error, not swallowed."""
    with pytest.raises(
        ClientError,
        match=r"^canvas is not a valid UUID: 'not-a-uuid'$",
    ):
        cli.parse_and_run(
            ["canvas", "not-a-uuid"],
            client_factory=lambda: cast(Client, _RecordingCanvas(uuid.uuid4())),
        )


def test_canvas_param_is_refused_off_show() -> None:
    """Only a shown visual takes params."""
    canvas_id = uuid.uuid4()
    client = _RecordingCanvas(canvas_id)
    with pytest.raises(ClientError, match="only with show"):
        cli.parse_and_run(
            [
                "canvas",
                str(canvas_id),
                "hide",
                str(uuid.uuid4()),
                "--param",
                "hops=3",
            ],
            client_factory=lambda: cast(Client, client),
        )
    assert client.operations == []


class _RecordingCanvas:
    """A canvas at revision 8 that keeps each operation posted to it."""

    def __init__(self, canvas_id: uuid.UUID) -> None:
        self.state: dict[str, object] = {
            "id": str(canvas_id),
            "revision": 8,
            "visuals": [],
            "focused_instance": None,
        }
        self.operations: list[object] = []

    def get(self, path: str) -> dict[str, object]:
        del path
        return self.state

    def post(self, path: str, *, body: object) -> dict[str, object]:
        del path
        self.operations.append(from_plain(body, dict[str, object])["operation"])
        return self.state


def _operation(client: _CanvasClient) -> dict[str, object]:
    """Return the one operation the client was asked to apply."""
    [operation] = client.operations
    return operation


def test_canvas_mark_draws_a_box_from_two_corners_and_names_its_records() -> None:
    client = _CanvasClient()
    record = uuid.uuid4()

    _canvas(client, "mark", "0.1,0.2", "0.4,0.5")
    _canvas(client, "mark", "0,0", "1,1", "--record", f"{record},{record}")

    plain, covered = client.operations
    corners = [{"x": 0.1, "y": 0.2}, {"x": 0.4, "y": 0.5}]
    assert plain == {"kind": "mark", "mark": {"points": corners}}
    assert covered == {
        "kind": "mark",
        "mark": {
            "points": [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 1.0}],
            "covers": {"records": [str(record)] * 2},
        },
    }


def test_canvas_mark_remove_and_clear_send_their_operations() -> None:
    first, second = _CanvasClient(), _CanvasClient()
    mark_id = uuid.uuid4()

    _canvas(first, "mark", "remove", str(mark_id))
    _canvas(second, "mark", "clear")

    assert _operation(first) == {"kind": "remove_mark", "id": str(mark_id)}
    assert _operation(second) == {"kind": "clear_marks"}


def test_canvas_mark_clear_page_names_the_page_to_clear() -> None:
    client = _CanvasClient()

    _canvas(client, "mark", "clear", "--page", "#/graph")

    assert _operation(client) == {"kind": "clear_marks", "page": "#/graph"}


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["mark"], "mark takes X1,Y1 X2,Y2"),
        (["mark", "0,0"], "mark takes X1,Y1 X2,Y2"),
        (["mark", "0,0", "1,1", "2,2"], "mark takes X1,Y1 X2,Y2"),
        (["mark", "clear", "now"], "mark takes X1,Y1 X2,Y2"),
        (["mark", "remove"], "mark remove takes one MARK_ID"),
        (["mark", "remove", "not-a-uuid"], "mark is not a valid UUID"),
        (
            ["mark", "remove", str(uuid.uuid4()), "--record", str(uuid.uuid4())],
            "mark takes X1,Y1 X2,Y2, or remove MARK_ID, or clear",
        ),
        (["mark", "0,0", "1,1", "--record", "Issue#7"], "record is not a valid UUID"),
        (["mark", "0,0", "left,right"], "a point is X,Y fractions"),
        (["mark", "0,0", "0.5"], "a point is X,Y fractions"),
        (["mark", "0,0", "0.1,0.2,0.3"], "a point is X,Y fractions"),
        (
            ["mark", "0,0", "1,1", "--placement", "main"],
            "mark takes no --placement or --param",
        ),
        (["mark", "0,0", "1,1", "--page", "#/graph"], "--page goes with mark clear"),
        (
            ["mark", "remove", str(uuid.uuid4()), "--page", "#/graph"],
            "--page goes with",
        ),
        (["mark", "clear", "--record", str(uuid.uuid4())], "mark takes X1,Y1"),
    ],
)
def test_canvas_mark_refuses_a_malformed_command(
    arguments: list[str],
    message: str,
) -> None:
    client = _CanvasClient()

    # Anchored: each message is the start of the refusal, word for word.
    with pytest.raises(ClientError, match=f"^{re.escape(message)}"):
        _canvas(client, *arguments)

    assert client.operations == []


def test_canvas_prints_each_box_with_its_id_page_what_it_covers_and_a_window_s_own(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _CanvasClient()

    _canvas(client, "mark", "0.3,0.3", "0.5,0.5")

    lines = capsys.readouterr().out.splitlines()
    assert lines[-2:] == [
        "  box m1 by agent on #/graph covers: Experiment#12 Measured tails, inst-1",
        "  box m2 by jp over the window on no page covers: nothing",
    ]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
