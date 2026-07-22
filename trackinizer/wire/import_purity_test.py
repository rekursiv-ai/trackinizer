"""Guard the publishable-client import boundary.

``types/`` + ``wire/`` + ``client/`` must form a self-contained client
distribution: none of them may import the ``server`` package (which owns
the store, api, web, notify, auth, migrations, ... modules), the heavy
server deps (``fastapi`` / ``asyncpg`` / ``uvicorn`` / ``starlette``), or
the ``trax`` CLI. This test walks the real import graph of each package
so a stray ``import`` is caught here, not when a downstream client
install fails to resolve a server module.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import ast
import pkgutil


_PKG_ROOT = Path(__file__).resolve().parent.parent
_BASE = "trackinizer"

# Top-level packages a publishable client must never pull in.
# ``_forbidden_hit`` compares the *first* path segment after the package
# base, so these are the package names (``server`` covers every
# server-only module -- store, api, web, notify, auth, primitives, ...;
# ``trax`` covers the CLI). Second-level module names would be inert.
_FORBIDDEN_INTERNAL: frozenset[str] = frozenset(
    {
        "server",
        "trax",
    }
)
_FORBIDDEN_THIRD_PARTY: frozenset[str] = frozenset(
    {
        "fastapi",
        "asyncpg",
        "uvicorn",
        "starlette",
    }
)

# ``client`` legitimately uses httpx; ``wire`` and ``types`` do not, but
# httpx is allowed package-wide since it is a client transport dep, not a
# server dep.
_CLIENT_PACKAGES: tuple[str, ...] = ("types", "wire", "client")


def _module_files(package: str) -> Iterator[Path]:
    pkg_dir = _PKG_ROOT / package
    # ``walk_packages`` yields submodules but never the package ``__init__.py``
    # itself, so a forbidden import there would evade the boundary guard. Yield
    # every ``__init__.py`` under the package (the package root and any
    # subpackage) up front, then the walked submodules.
    yield from pkg_dir.rglob("__init__.py")
    for info in pkgutil.walk_packages([str(pkg_dir)], prefix=""):
        if info.name.endswith("_test"):
            continue
        path = pkg_dir / f"{info.name.replace('.', '/')}.py"
        if path.is_file():
            yield path


def _imported_roots(path: Path) -> set[str]:
    """Return the set of imported module dotted-paths in ``path``."""
    tree = ast.parse(path.read_text(), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            out.add(node.module)
    return out


def _forbidden_hit(imported: str) -> str | None:
    """Name the violated boundary for ``imported``, or ``None`` if clean."""
    top = imported.split(".", maxsplit=1)[0]
    if top in _FORBIDDEN_THIRD_PARTY:
        return imported
    if imported.startswith(f"{_BASE}."):
        tail = imported[len(_BASE) + 1 :].split(".", maxsplit=1)[0]
        if tail in _FORBIDDEN_INTERNAL:
            return imported
    return None


def test_module_files_includes_package_init() -> None:
    """The walked set must include each package's ``__init__.py``.

    ``pkgutil.walk_packages`` yields submodules but NOT the package
    ``__init__.py`` itself, so a forbidden ``import server`` / ``import
    fastapi`` in ``wire/__init__.py`` (or ``types`` / ``client``) would slip
    past the purity guard. Each package ships an ``__init__.py``, so it must be
    among the files the boundary check walks.
    """
    for package in _CLIENT_PACKAGES:
        init = _PKG_ROOT / package / "__init__.py"
        assert init.is_file(), f"{package} has no __init__.py"
        assert init in set(_module_files(package)), (
            f"{package}/__init__.py is not walked by the purity guard"
        )


def test_client_packages_do_not_import_server_or_cli() -> None:
    violations: list[str] = [
        f"{path.relative_to(_PKG_ROOT)} imports {hit}"
        for package in _CLIENT_PACKAGES
        for path in _module_files(package)
        for imported in _imported_roots(path)
        if (hit := _forbidden_hit(imported)) is not None
    ]
    assert not violations, (
        "client-distribution import boundary breached:\n"
        + "\n".join(sorted(violations))
    )


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
