"""A bare ``pip install trackinizer`` carries the client side only.

``trax``, the client, the wire models and the types need no server package.
FastAPI, asyncpg, torch and the rest of the ``server`` extra install only with
``trackinizer[server]``. These tests keep that true: every client-side module
imports with the extra's packages unimportable, and the ``trackinizer`` command
explains a missing extra instead of printing a traceback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import ast
import json
import subprocess
import sys
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from treekle import from_plain, json_loads

import pytest

from trackinizer.server import entry


_CWD: Final = Path(__file__).resolve().parent

# The package this test lives in; the monorepo and the export name it differently.
_BASE: Final = __name__.rpartition(".")[0]

# The directory that holds the top-level package, which a child interpreter
# puts on its path to import the same tree this test runs in.
_ROOT: Final = _CWD.parents[_BASE.count(".")]

_CLIENT_PACKAGES: Final = ("client", "trax", "types", "wire")

# Only ``server/`` imports the embedder protocol, and it names torch.
_SERVER_ONLY_MODULES: Final = frozenset({"types.embedder"})

# The monorepo imports configgle as a sibling package under another name. The
# export rewrites that name to the published one, which repeats the first entry.
_CONFIGGLE: Final = ("configgle", "configgle")

# Runs in a child interpreter. The finder goes in before the first import, and
# raises what a missing package raises, ``ModuleNotFoundError`` with its name.
_PRELUDE: Final = """
import importlib
import json
import sys

root, blocked = sys.argv[1], json.loads(sys.argv[2])
sys.path.insert(0, root)


def hit(name):
    return any(name == b or name.startswith(b + ".") for b in blocked)


class Blocker:
    def find_spec(self, name, path=None, target=None):
        if hit(name):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


preloaded = sorted(name for name in sys.modules if hit(name))
if preloaded:
    raise SystemExit(f"already imported before the test began: {preloaded}")
sys.meta_path.insert(0, Blocker())
"""

_IMPORT_PROBE: Final = (
    _PRELUDE
    + """
failures = {}
for module in sys.argv[3:]:
    try:
        importlib.import_module(module)
    except ImportError as err:
        failures[module] = f"{type(err).__name__}: {err}"
print(json.dumps(failures))
"""
)

_ENTRY_PROBE: Final = (
    _PRELUDE
    + """
raise SystemExit(importlib.import_module(sys.argv[3]).main())
"""
)


@pytest.mark.cli_python_subprocess
def test_client_side_modules_import_without_server_only_packages() -> None:
    """The client side imports with no package of the ``server`` extra installed.

    Every module under ``client/``, ``trax/``, ``types/`` and ``wire/`` imports in
    a fresh interpreter whose finder refuses each package of the ``server``
    extra. One module that imports FastAPI, asyncpg or torch at import time
    would make a bare ``pip install trackinizer`` fail on ``trax``.
    """
    modules = _client_side_modules()
    assert {f"{_BASE}.{name}" for name in ("trax.cli", "client.client")} <= set(modules)

    failures = _import_failures(modules, root=_ROOT)

    assert not failures, (
        "client-side modules need a server-only package:\n"
        + "\n".join(
            f"{module}: {reason}" for module, reason in sorted(failures.items())
        )
    )


@pytest.mark.cli_python_subprocess
def test_import_probe_reports_a_module_that_needs_a_server_only_package(
    tmp_path: Path,
) -> None:
    """The probe names a module that imports a blocked package, and only that one."""
    (tmp_path / "leaky.py").write_text("import torch\n")
    (tmp_path / "lazy.py").write_text("def embed():\n    import torch\n")
    (tmp_path / "plain.py").write_text("import json\n")

    failures = _import_failures(["leaky", "lazy", "plain"], root=tmp_path)

    assert failures == {"leaky": "ModuleNotFoundError: No module named 'torch'"}


def test_client_side_modules_never_name_a_server_only_package() -> None:
    """No module the client side can reach imports a package of the ``server`` extra.

    The import probe sees only what runs at import time. This scan follows every
    import statement of the client side, in a function body or a ``lazy_import``
    string too, through the first-party modules they name. A package a client
    function imports later, such as ``watchdog`` on macOS from the filesystem
    follower, would fail ``trax run`` on a bare install, and the probe cannot see it.
    """
    modules = _client_side_modules()

    reached = _blocked_imports(
        modules,
        root=_ROOT,
        blocked=_blocked_names(),
        skip=frozenset(f"{_BASE}.{name}" for name in _SERVER_ONLY_MODULES),
    )

    assert not reached, "the client side needs a server-only package:\n" + "\n".join(
        f"{module} imports {', '.join(sorted(names))}"
        for module, names in sorted(reached.items())
    )


def test_static_scan_follows_later_lazy_and_relative_imports(tmp_path: Path) -> None:
    """The scan reports an import in a function, a ``lazy_import`` and a first-party chain."""
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "root.py").write_text(
        "from . import follower\nimport pkg.lazy, pkg.skipped\n",
    )
    (package / "follower.py").write_text(
        "def watch():\n    from watchdog.observers import fsevents\n",
    )
    (package / "lazy.py").write_text('fastapi = lazy_import("fastapi.routing")\n')
    (package / "typed.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    import torch\n"
        "else:\n    import json\n",
    )
    (package / "skipped.py").write_text("import transformers\n")
    (package / "plain.py").write_text("import json\nfrom . import typed\n")

    reached = _blocked_imports(
        ["pkg.root", "pkg.plain"],
        root=tmp_path,
        blocked=frozenset({"fastapi", "torch", "transformers", "watchdog"}),
        skip=frozenset({"pkg.skipped"}),
    )

    assert reached == {
        "pkg.follower": {"watchdog"},
        "pkg.lazy": {"fastapi"},
    }


def test_missing_extra_guard_names_every_package_of_the_server_extra() -> None:
    """``entry.SERVER_ONLY`` is the ``server`` extra, by import name."""
    assert _server_only_packages() == entry.SERVER_ONLY


@pytest.mark.cli_python_subprocess
def test_server_command_explains_a_missing_extra() -> None:
    """Without FastAPI, the entry point prints the install line, exits 2, no traceback."""
    result = subprocess.run(  # noqa: S603 -- argv is this interpreter and a fixed probe.
        [
            sys.executable,
            "-I",
            "-c",
            _ENTRY_PROBE,
            str(_ROOT),
            json.dumps(["fastapi"]),
            f"{_BASE}.server.entry",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2, result.stderr
    assert result.stderr == "the server needs: pip install 'trackinizer[server]'\n"
    assert not result.stdout


def _client_side_modules() -> list[str]:
    """Return the dotted name of every module of the client side, tests excluded."""
    modules: list[str] = []
    for package in _CLIENT_PACKAGES:
        for path in sorted((_CWD / package).rglob("*.py")):
            relative = path.relative_to(_CWD).with_suffix("")
            name = ".".join(
                relative.parts[:-1] if relative.name == "__init__" else relative.parts,
            )
            if (
                relative.name in {"conftest", "__main__"}
                or relative.name.endswith("_test")
                or name in _SERVER_ONLY_MODULES
                # A script beside the package, not a module of it.
                or not all(
                    (_CWD.joinpath(*relative.parts[:depth]) / "__init__.py").is_file()
                    for depth in range(1, len(relative.parts))
                )
            ):
                continue
            modules.append(f"{_BASE}.{name}")
    return modules


def _blocked_names() -> frozenset[str]:
    """Return the dotted names a bare install cannot import."""
    return frozenset({*_server_only_packages(), *_CONFIGGLE})


# Reads source, so an import anywhere in a module counts: at module scope, in a
# function, or as the string a ``lazy_import`` or ``import_module`` call names. An
# import under ``if TYPE_CHECKING`` never runs and does not count. A module named in
# ``skip`` is not followed.
def _blocked_imports(
    modules: list[str],
    *,
    root: Path,
    blocked: frozenset[str],
    skip: frozenset[str],
) -> dict[str, set[str]]:
    """Return the blocked names each module reachable from ``modules`` imports."""
    reached: dict[str, set[str]] = {}
    pending = list(modules)
    seen = set(modules)
    while pending:
        module = pending.pop()
        path = _module_path(module, root=root)
        assert path is not None, module
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        for name in _imported_names(path, package=package):
            hit = _blocked_prefix(name, blocked)
            if hit is not None:
                reached.setdefault(module, set()).add(hit)
                continue
            # Importing a.b.c also imports a and a.b.
            parts = name.split(".")
            for end in range(1, len(parts) + 1):
                parent = ".".join(parts[:end])
                if (
                    parent not in seen
                    and parent not in skip
                    and _module_path(parent, root=root) is not None
                ):
                    seen.add(parent)
                    pending.append(parent)
    return reached


def _blocked_prefix(name: str, blocked: frozenset[str]) -> str | None:
    """Return the blocked name that ``name`` is or lies inside, if any."""
    for blocked_name in blocked:
        if name == blocked_name or name.startswith(blocked_name + "."):
            return blocked_name
    return None


def _module_path(module: str, *, root: Path) -> Path | None:
    """Return the source file of the first-party ``module`` under ``root``, if any."""
    base = root.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imported_names(path: Path, *, package: str) -> set[str]:
    """Return every dotted module name ``path`` imports, however and wherever."""
    names: set[str] = set()
    for node in _executed_nodes(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module
            if node.level:
                # ``from . import x`` is the package; each further dot is its parent.
                base = package.rsplit(".", node.level - 1)[0]
                source = base if module is None else f"{base}.{module}"
            else:
                source = "" if module is None else module
            names.add(source)
            names.update(f"{source}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Attribute):
                called = callee.attr
            elif isinstance(callee, ast.Name):
                called = callee.id
            else:
                called = ""
            if called in {"lazy_import", "import_module"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.add(first.value)
    return names


def _executed_nodes(tree: ast.AST) -> list[ast.AST]:
    """Return every node of ``tree`` outside an ``if TYPE_CHECKING`` body."""
    found: list[ast.AST] = []
    pending: list[ast.AST] = [tree]
    while pending:
        node = pending.pop()
        found.append(node)
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            pending.extend(node.orelse)
        else:
            pending.extend(ast.iter_child_nodes(node))
    return found


def _is_type_checking(test: ast.expr) -> bool:
    """Return whether ``test`` is ``TYPE_CHECKING`` or ``typing.TYPE_CHECKING``."""
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _import_failures(modules: list[str], *, root: Path) -> dict[str, str]:
    """Import ``modules`` in a child interpreter that cannot import the server extra."""
    blocked = [*_server_only_packages(), *_CONFIGGLE]
    result = subprocess.run(  # noqa: S603 -- argv is this interpreter and a fixed probe.
        [
            sys.executable,
            "-I",
            "-c",
            _IMPORT_PROBE,
            str(root),
            json.dumps(sorted(blocked)),
            *modules,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json_loads(result.stdout, dict[str, str])


def _server_only_packages() -> frozenset[str]:
    """Return the import name of each requirement of the ``server`` extra."""
    # The monorepo keeps the extra in the export's pyproject delta; the exported
    # package has the rendered pyproject.toml at its root instead.
    for candidate in (
        _CWD / ".export" / "pyproject.delta.toml",
        _CWD.parent / "pyproject.toml",
    ):
        if candidate.is_file():
            project = from_plain(
                tomllib.loads(candidate.read_text())["project"],
                dict[str, object],
            )
            # The delta also holds operations on the base's extras, as ``!name = true``.
            extras = from_plain(project["optional-dependencies"], dict[str, object])
            return frozenset(
                canonicalize_name(Requirement(requirement).name).replace("-", "_")
                for requirement in from_plain(extras["server"], list[str])
            )
    raise FileNotFoundError("no pyproject.toml or pyproject.delta.toml found")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
