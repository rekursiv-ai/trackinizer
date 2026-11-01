"""``trax canvas``: read a canvas, change what it shows, or mark records on it.

An agent run placed on a canvas finds it in ``$TRAX_CANVAS`` (:func:`canvas_from_env`),
so the command may leave the id out. An app built on the server serves the canvas
routes; the server alone answers each with a 404.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol, cast, override

import argparse
import json
import uuid

from treekle import MutablePlainTree, PlainTree, from_plain, json_loads

from trackinizer.client.errors import ClientError
from trackinizer.trax.commands import Command, HelpPage
from trackinizer.trax.context import env
from trackinizer.trax.render import echo


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from trackinizer.client.client import Client


__all__ = ["Canvas", "canvas_from_env"]


def canvas_from_env(environ: Mapping[str, str]) -> uuid.UUID | None:
    """Return the canvas a run started from: ``TRAX_CANVAS``, else its ``LC_`` twin.

    Args:
      environ: The process environment.

    Returns:
      canvas: The canvas id; ``None`` when neither names a valid one.

    """
    for name in ("TRAX_CANVAS", "LC_TRAX_CANVAS"):
        value = environ.get(name, "").strip()
        if value:
            try:
                return uuid.UUID(value)
            except ValueError:
                return None
    return None


class Canvas(Command):
    """Read or operate the visual canvas paired with an agent."""

    names = ("canvas",)
    help = HelpPage(
        usage="trax canvas CANVAS_UUID [show TYPE | navigate ROUTE | highlight UUID[,UUID...] | hide UUID | focus UUID | place UUID PLACEMENT | mark X1,Y1 X2,Y2 | mark remove MARK_ID | mark clear]",
        summary="Read a canvas, change its visible modules, move its page, or mark records.",
        options=(
            (
                "--record UUID",
                "Record target for a shown visual; a box's, UUID[,UUID...].",
            ),
            (
                "--page HASH",
                "With `mark clear`, clear only the boxes shown on page HASH.",
            ),
            (
                "--param KEY=VALUE",
                (
                    "Parameter of a shown visual, repeatable; its value is read as "
                    "the type the visual catalog gives that parameter."
                ),
            ),
        ),
        examples=(
            "trax canvas 11111111-1111-1111-1111-111111111111",
            "trax canvas UUID show trax.subgraph --record RECORD_UUID --placement side",
            "trax canvas UUID show trax.timeline --record RECORD_UUID --param direction_limit=5",
            "trax canvas UUID navigate '#/list/Issue'",
            "trax canvas UUID highlight RECORD_UUID,RECORD_UUID",
            "trax canvas UUID hide chat",
            "trax canvas UUID hide INSTANCE_UUID",
            "trax canvas UUID mark 0.1,0.2 0.4,0.5 --record RECORD_UUID",
            "trax canvas UUID mark remove MARK_UUID",
            "trax canvas UUID mark clear",
            "trax canvas UUID mark clear --page '#/graph'",
            "trax canvas UUID theme to light",
            "trax canvas UUID chat_side to right",
            "trax canvas UUID theme to null",
        ),
        notes=(
            "Mutations fetch the current revision before applying one operation.",
            (
                "mark draws a red box on the canvas, which people see and draw on too, "
                "and which stays until someone clicks it away; its corners are X,Y "
                "fractions (0 to 1) of the stage. It shows on the page the canvas is on. "
                "`trax canvas UUID` lists the boxes with their ids and what each covers."
            ),
            (
                "KEY to VALUE sets the canvas owner's preference KEY (VALUE read as JSON "
                "when it parses, else as text; null puts it back to its default)."
            ),
        ),
    )

    @classmethod
    @override
    def make_parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="trax canvas", description=cls.__doc__)
        parser.add_argument("canvas_id", nargs="?")
        parser.add_argument(
            "action",
            nargs="?",
            metavar="|".join((*_CANVAS_ACTIONS, "KEY")),
            help=f"one of {', '.join(_CANVAS_ACTIONS)}, or a preference KEY followed by: to VALUE",
        )
        parser.add_argument("subject", nargs="*")
        parser.add_argument("--record")
        parser.add_argument(
            "--placement",
            choices=("main", "left", "side", "floating"),
        )
        parser.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
        parser.add_argument("--page")
        return parser

    @classmethod
    @override
    def run(
        cls,
        verb: str,
        args: argparse.Namespace,
        client_factory: Callable[[], Client],
    ) -> None:
        del verb
        flags = _with_default_canvas(cast(_CanvasArgs, args))
        canvas_id = _canvas_uuid(flags.canvas_id or "", "canvas")
        client = client_factory()
        if flags.action is None:
            record = flags.record or ""
            if (
                flags.subject
                or record
                or flags.placement is not None
                or flags.param
                or flags.page is not None
            ):
                raise ClientError("canvas read does not accept operation arguments")
            _print_canvas(client.read_canvas(canvas_id))
            return
        if _is_preference(flags):
            updated = client.set_preferences(
                canvas_id,
                change={flags.action or "": _preference_value(flags.subject[1])},
            )
            _print_canvas(updated)
            return
        if flags.action not in _CANVAS_ACTIONS:
            raise ClientError(
                f"unknown canvas action {flags.action!r}: use one of "
                f"{', '.join(_CANVAS_ACTIONS)}, or KEY to VALUE",
            )
        if flags.action == "highlight":
            updated = client.highlight(canvas_id, ids=_highlight_ids(flags))
            _print_canvas(updated)
            return
        if flags.action == "mark":
            _print_canvas(_mark(client, canvas_id=canvas_id, args=flags))
            return
        if _is_chat(flags):
            updated = client.chat_window(canvas_id, hidden=flags.action == "hide")
            _print_canvas(updated)
            return
        parameters = (
            _visual_parameters(client, visual_type=flags.subject[0])
            if flags.action == "show" and flags.param and len(flags.subject) == 1
            else {}
        )
        operation = _canvas_operation(flags, parameters=parameters)
        if operation["kind"] == "navigate":
            updated = client.navigate(canvas_id, route=str(operation["route"]))
        else:
            updated = client.apply_canvas_operation(
                canvas_id,
                operation=operation,
            )
        _print_canvas(updated)


def _canvas_operation(
    args: _CanvasArgs,
    *,
    parameters: Mapping[str, str],
) -> dict[str, object]:
    """Build one validated canvas operation from CLI arguments."""
    subject = list(args.subject)
    operation: dict[str, object]
    if args.param and args.action != "show":
        raise ClientError("--param is accepted only with show")
    if args.action == "show":
        if len(subject) != 1:
            raise ClientError("show requires one visual type")
        operation = {"kind": "show", "visual_type": subject[0]}
        if args.placement is not None:
            operation["placement"] = args.placement
        if args.record is not None:
            operation["record_id"] = str(_canvas_uuid(args.record, "record"))
        if args.param:
            operation["params"] = _canvas_params(args.param, parameters=parameters)
    elif args.action == "navigate":
        if len(subject) != 1 or args.record is not None or args.placement is not None:
            raise ClientError("navigate requires one route")
        operation = {"kind": "navigate", "route": subject[0]}
    elif args.action in {"hide", "focus"}:
        if len(subject) != 1 or args.record is not None or args.placement is not None:
            raise ClientError(f"{args.action} requires one instance UUID")
        operation = {
            "kind": args.action,
            "instance_id": str(_canvas_uuid(subject[0], "instance")),
        }
    else:
        if (
            len(subject) != 2
            or args.record is not None
            or args.placement is not None
            or subject[1] not in {"main", "left", "side", "floating"}
        ):
            raise ClientError("place requires an instance UUID and placement")
        operation = {
            "kind": "place",
            "instance_id": str(_canvas_uuid(subject[0], "instance")),
            "placement": subject[1],
        }
    return operation


def _mark(
    client: Client,
    *,
    canvas_id: uuid.UUID,
    args: _CanvasArgs,
) -> dict[str, PlainTree]:
    """Draw, remove or clear red boxes: ``mark X1,Y1 X2,Y2``, ``mark remove ID``, ``mark clear``."""
    subject = args.subject
    if args.placement is not None or args.param:
        raise ClientError("mark takes no --placement or --param")
    if subject == ["clear"] and args.record is None:
        return client.apply_canvas_operation(
            canvas_id,
            operation={"kind": "clear_marks"}
            | ({} if args.page is None else {"page": args.page}),
        )
    if args.page is not None:
        raise ClientError("--page goes with mark clear only")
    if subject[:1] == ["remove"] and args.record is None:
        if len(subject) != 2:
            raise ClientError("mark remove takes one MARK_ID")
        return client.apply_canvas_operation(
            canvas_id,
            operation={
                "kind": "remove_mark",
                "id": str(_canvas_uuid(subject[1], "mark")),
            },
        )
    if len(subject) != 2 or subject[0] in {"clear", "remove"}:
        raise ClientError("mark takes X1,Y1 X2,Y2, or remove MARK_ID, or clear")
    mark: dict[str, object] = {"points": [_mark_point(point) for point in subject]}
    if args.record is not None:
        mark["covers"] = {
            "records": [
                str(_canvas_uuid(item, "record"))
                for item in args.record.split(",")
                if item
            ],
        }
    return client.apply_canvas_operation(
        canvas_id,
        operation={"kind": "mark", "mark": mark},
    )


def _mark_point(text: str) -> dict[str, float]:
    """Read ``X,Y`` as fractions of the stage."""
    try:
        x, y = text.split(",")
        return {"x": float(x), "y": float(y)}
    except ValueError as error:
        raise ClientError(
            f"a point is X,Y fractions of the stage, not {text!r}",
        ) from error


def _is_preference(args: _CanvasArgs) -> bool:
    """Whether the words are ``KEY to VALUE``: set the canvas owner's preference KEY."""
    if args.action in _CANVAS_ACTIONS or args.subject[:1] != ["to"]:
        return False
    if (
        len(args.subject) != 2
        or args.record is not None
        or args.placement is not None
        or args.param
    ):
        raise ClientError(f"{args.action} to takes one VALUE and no other argument")
    return True


def _preference_value(text: str) -> MutablePlainTree:
    """Read a preference's value: as JSON when it parses (``true``, ``null``), else as text."""
    try:
        return json_loads(text)
    except json.JSONDecodeError:
        return text


def _is_chat(args: _CanvasArgs) -> bool:
    """Whether the action is `hide chat` or `show chat`: the tab's Chat window."""
    if args.action not in {"hide", "show"} or args.subject != ["chat"]:
        return False
    if args.record is not None or args.placement is not None or args.param:
        raise ClientError(f"{args.action} chat takes no other argument")
    return True


def _highlight_ids(args: _CanvasArgs) -> list[uuid.UUID]:
    """Read ``highlight``'s one comma-separated UUID list; an empty one clears."""
    if len(args.subject) != 1 or args.record is not None or args.placement is not None:
        raise ClientError("highlight requires UUID[,UUID...] ('' clears)")
    return [_canvas_uuid(item, "record") for item in args.subject[0].split(",") if item]


def _canvas_params(
    pairs: list[str],
    *,
    parameters: Mapping[str, str],
) -> dict[str, str | int | bool]:
    """Read ``KEY=VALUE`` pairs, each value as the type the catalog gives its key."""
    params: dict[str, str | int | bool] = {}
    for pair in pairs:
        key, equals, value = pair.partition("=")
        if not equals or not key.isidentifier():
            raise ClientError(
                f"--param needs KEY=VALUE with a plain name, got {pair!r}",
            )
        if key in params:
            raise ClientError(f"--param {key} was given more than once")
        if key not in parameters:
            raise ClientError(
                f"--param {key} is not a parameter of this visual; it takes: "
                f"{', '.join(parameters) or 'none'}",
            )
        params[key] = _canvas_param_value(key=key, value=value, kind=parameters[key])
    return params


def _canvas_param_value(*, key: str, value: str, kind: str) -> str | int | bool:
    """Read ``value`` as a catalog parameter ``kind``: string, integer or boolean."""
    if kind == "integer":
        if not (value.isascii() and value.removeprefix("-").isdecimal()):
            raise ClientError(f"--param {key} needs an integer, got {value!r}")
        return int(value)
    if kind == "boolean":
        if value not in {"true", "false"}:
            raise ClientError(f"--param {key} needs true or false, got {value!r}")
        return value == "true"
    return value


def _visual_parameters(client: Client, *, visual_type: str) -> dict[str, str]:
    """Return the parameters a visual takes, by name, with their catalog types."""
    catalog = from_plain(client.get("/api/visuals"), dict[str, object])
    visuals = from_plain(catalog.get("visuals"), list[dict[str, object]], default=[])
    for visual in visuals:
        if visual.get("type") == visual_type:
            schemas = from_plain(
                visual.get("parameter_schema"),
                dict[str, dict[str, object]],
                default={},
            )
            return {
                name: from_plain(schema.get("type"), str, default="")
                for name, schema in schemas.items()
            }
    raise ClientError(f"{visual_type!r} is not a visual in the catalog")


def _print_canvas(payload: object) -> None:
    """Print canvas and visual identifiers in a compact readable form."""
    state = from_plain(payload, dict[str, object])
    canvas_id = from_plain(state.get("id"), str, default="unknown")
    revision = from_plain(state.get("revision"), int, default=0)
    focused = from_plain(state.get("focused_instance"), str, default="none")
    echo(f"canvas {canvas_id} revision {revision} focused {focused}")
    preferences = from_plain(state.get("preferences"), dict[str, object], default={})
    if preferences:
        echo(
            "  preferences "
            + " ".join(
                f"{key}={json.dumps(value)}"
                for key, value in sorted(preferences.items())
            ),
        )
    visuals = from_plain(state.get("visuals"), list[dict[str, object]], default=[])
    if not visuals:
        echo("  (no visuals)")
    for visual in visuals:
        instance_id = from_plain(visual.get("id"), str, default="unknown")
        visual_type = from_plain(visual.get("type"), str, default="unknown")
        placement = from_plain(visual.get("placement"), str, default="main")
        record_id = from_plain(visual.get("record_id"), str, default="")
        target = f" record={record_id}" if record_id else ""
        echo(f"  {visual_type} {instance_id} {placement}{target}")
    _print_marks(state)


def _print_marks(state: Mapping[str, object]) -> None:
    """Print every box with its id, page and what it covers."""
    labels = from_plain(state.get("record_labels"), dict[str, str], default={})
    for mark in from_plain(state.get("marks"), list[dict[str, object]], default=[]):
        covers = from_plain(mark.get("covers"), dict[str, list[str]], default={})
        named = [labels.get(each, each) for each in covers.get("records", [])]
        named += covers.get("instances", [])
        over = " over the window" if mark.get("window") is True else ""
        echo(
            f"  box {mark.get('id')} by {mark.get('author')}{over} "
            f"on {mark.get('page') or 'no page'} covers: {', '.join(named) or 'nothing'}",
        )


def _canvas_uuid(value: str, label: str) -> uuid.UUID:
    """Parse one UUID and report malformed command-line identifiers clearly."""
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise ClientError(f"{label} is not a valid UUID: {value!r}") from error


_CANVAS_ACTIONS: Final = (
    "show",
    "navigate",
    "highlight",
    "hide",
    "focus",
    "place",
    "mark",
)
"""The operations a canvas takes by name; any other word starts ``KEY to VALUE``."""


class _CanvasArgs(Protocol):
    canvas_id: str | None
    action: str | None
    subject: list[str]
    record: str | None
    placement: str | None
    param: list[str]
    page: str | None


# ``trax canvas show trax.subgraph`` parses ``show`` as the id; when that is not a
# UUID and ``$TRAX_CANVAS`` (or ``$LC_TRAX_CANVAS``) names a canvas, the words
# shift right by one and the canvas is the id.
def _with_default_canvas(args: _CanvasArgs) -> _CanvasArgs:
    """Fill in the id an agent run's session was placed on when the command names none."""
    named = args.canvas_id
    if named is not None:
        try:
            _ = uuid.UUID(named)
        except ValueError:
            pass
        else:
            return args
    canvas = canvas_from_env(
        {
            name: value
            for name in ("TRAX_CANVAS", "LC_TRAX_CANVAS")
            if (value := env(name)) is not None
        },
    )
    if canvas is None:
        if named is None:
            raise ClientError(
                "no canvas: give its UUID, or run inside a recorded agent run "
                "placed on a canvas ($TRAX_CANVAS)",
            )
        return args
    if named is not None:
        args.action, args.subject = (
            named,
            [word for word in (args.action, *args.subject) if word is not None],
        )
    args.canvas_id = str(canvas)
    return args
