"""Guard which monorepo packages trackinizer core may import.

Trackinizer is exported as a public package, so each ``loop.*`` package it
imports must ship with it or be a public sibling. Private libraries that plug
into core (access control, billing, usage) do so through the addon seam, and
never the other way round. This test walks every module of this package, tests
included, and fails on any ``loop.*`` import whose top-level package is not on
the allowlist. An allowlist, not a list of the private names: a new private
library is refused without naming it here, and this file names nothing the
export's leak check would refuse.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

import ast


_CWD: Final = Path(__file__).resolve().parent

if TYPE_CHECKING:
    from collections.abc import Iterator


# The top-level ``loop.<package>`` names core may import. Each is exported, or
# is a public sibling package core depends on.
_ALLOWED: Final = frozenset(
    {
        "configgle",
        "copybarista",
        "lib",
        "sagent",
        "trackinizer",
        "treekle",
    },
)


# Calls that import the module their first argument names, by their last name, so
# ``importlib.import_module`` and a bare ``import_module`` both count.
_IMPORTING_CALLS: Final = frozenset({"lazy_import", "import_module", "__import__"})


def _imported_modules(path: Path, *, package: tuple[str, ...]) -> Iterator[str]:
    """Yield each module ``path`` imports, absolute, by statement or by call."""
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # ``from . import x`` resolves against the module's own package, each
            # further dot one package up.
            base = package[: len(package) - node.level + 1] if node.level else ()
            named = () if node.module is None else tuple(node.module.split("."))
            # ``from loop import x`` imports the package ``loop.x``, not ``loop``.
            yield from (".".join((*base, *named, alias.name)) for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id in _IMPORTING_CALLS)
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in _IMPORTING_CALLS
                )
            )
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            yield node.args[0].value


def _violations(root: Path, *, package: tuple[str, ...] = ()) -> list[str]:
    """Name every ``loop.*`` package imported under ``root`` outside the allowlist."""
    return sorted(
        f"{path.relative_to(root)} imports {'.'.join(parts[:2])}"
        for path in root.rglob("*.py")
        for module in _imported_modules(
            path,
            package=(*package, *path.relative_to(root).parent.parts),
        )
        if (parts := module.split("."))[0] == "loop"
        and len(parts) > 1
        and parts[1] not in _ALLOWED
    )


def test_core_imports_only_allowlisted_monorepo_packages() -> None:
    violations = _violations(_CWD, package=("loop", "trackinizer"))
    assert not violations, "trackinizer imports a package off its allowlist:\n" + (
        "\n".join(violations)
    )


def test_an_import_of_a_package_off_the_allowlist_is_caught(tmp_path: Path) -> None:
    private = "loop.paywall"
    (tmp_path / "module.py").write_text(
        f"import {private}\n"
        f"from {private}.ledger import charge\n"
        f'proxy = lazy_import("{private}.store")\n'
        "from . import sibling\n",
    )
    assert _violations(tmp_path) == [f"module.py imports {private}"] * 3


def test_every_spelling_of_an_import_is_caught(tmp_path: Path) -> None:
    # A module one package below the root names a sibling of its package with two
    # dots: one dot is its own package, the second its parent.
    (tmp_path / "module.py").write_text(
        "from loop import paywall\n"
        'paywall = importlib.import_module("loop.paywall")\n'
        'paywall = __import__("loop.paywall")\n'
        "from .. import paywall\n"
        "from ..paywall import charge\n"
        "from . import sibling\n",
    )
    assert (
        _violations(tmp_path, package=("loop", "trackinizer"))
        == [
            "module.py imports loop.paywall",
        ]
        * 5
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
