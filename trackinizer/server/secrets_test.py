"""The file secret backend keeps values under one root and readable by one group."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import errno
import os
import stat

import pytest

from trackinizer.lib.userdirs import data_dir
from trackinizer.server.config import ConfigError
from trackinizer.server.secrets import (
    FileSecrets,
    SecretNotFoundError,
    SecretRef,
    parse_secrets,
    validate_ref,
)


if TYPE_CHECKING:
    from collections.abc import Iterator

    from trackinizer.wire.wire_variables import Layer


_VALUE: Final = "s3cret-value"


@pytest.fixture
def umask() -> Iterator[None]:
    """Restore the process umask after a test that sets it."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def test_a_value_round_trips_and_overwrites(tmp_path: Path) -> None:
    store = FileSecrets(tmp_path / "secrets")
    store.put(_ref(), _VALUE)
    assert store.get(_ref()) == _VALUE

    store.put(_ref(), "café\nsecond line")

    assert store.get(_ref()) == "café\nsecond line"


def test_a_missing_secret_is_not_found(tmp_path: Path) -> None:
    with pytest.raises(SecretNotFoundError, match="API_TOKEN"):
        FileSecrets(tmp_path / "secrets").get(_ref())


def test_deleting_removes_the_value_and_an_absent_one_is_fine(tmp_path: Path) -> None:
    store = FileSecrets(tmp_path / "secrets")
    store.delete(_ref())
    store.put(_ref(), _VALUE)

    store.delete(_ref())
    store.delete(_ref())

    with pytest.raises(SecretNotFoundError):
        store.get(_ref())


@pytest.mark.parametrize("name", ["../x", "a/b", "", "1A", "A" * 129, "A\n", "a b"])
def test_a_bad_name_never_touches_the_disk(tmp_path: Path, name: str) -> None:
    root = tmp_path / "secrets"
    store = FileSecrets(root)
    for call in (
        lambda: store.put(_ref(name), _VALUE),
        lambda: store.get(_ref(name)),
        lambda: store.delete(_ref(name)),
    ):
        with pytest.raises(ValueError, match="secret name"):
            call()
    assert not root.exists()


def test_a_bad_layer_never_touches_the_disk(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    with pytest.raises(ValueError, match="secret layer"):
        FileSecrets(root).put(_ref(layer="../etc"), _VALUE)
    assert not root.exists()


def test_each_layer_and_owner_has_its_own_value(tmp_path: Path) -> None:
    store = FileSecrets(tmp_path / "secrets")
    refs = {
        _ref(): "org",
        _ref(layer="machine", owner="box-1"): "machine",
        _ref(layer="user", owner="ana@example.com"): "user",
    }
    for ref, value in refs.items():
        store.put(ref, value)

    assert {ref: store.get(ref) for ref in refs} == refs


@pytest.mark.parametrize(
    ("owner", "directory"),
    [
        ("../escape", "..%2Fescape"),
        ("a/b", "a%2Fb"),
        (".", "%2E"),
        ("..", "%2E%2E"),
        ("_", "%5F"),
        ("ana@example.com", "ana%40example.com"),
        ("ana/../../x", "ana%2F..%2F..%2Fx"),
        ("\u00e9\x00", "%C3%A9%00"),
    ],
)
def test_an_owner_cannot_leave_its_directory(
    tmp_path: Path,
    owner: str,
    directory: str,
) -> None:
    root = tmp_path / "secrets"
    store = FileSecrets(root)
    ref = _ref(layer="user", owner=owner)
    store.put(ref, _VALUE)
    store.put(_ref(layer="user", owner=""), "empty owner")

    files = {p for p in root.rglob("*") if p.is_file()}

    assert files == {
        root / "user" / directory / "API_TOKEN",
        root / "user" / "_" / "API_TOKEN",
    }
    assert store.get(ref) == _VALUE


def test_the_empty_owner_and_an_underscore_owner_differ(tmp_path: Path) -> None:
    store = FileSecrets(tmp_path / "secrets")
    store.put(_ref(layer="user", owner=""), "empty")
    store.put(_ref(layer="user", owner="_"), "underscore")

    assert store.get(_ref(layer="user", owner="")) == "empty"
    assert store.get(_ref(layer="user", owner="_")) == "underscore"


@pytest.mark.parametrize("mask", [0o077, 0o000])
def test_modes_hold_under_any_umask(tmp_path: Path, umask: None, mask: int) -> None:
    del umask
    os.umask(mask)
    root = tmp_path / "nested" / "secrets"
    FileSecrets(root).put(_ref(layer="user", owner="ana"), _VALUE)

    leaf = root / "user" / "ana"
    assert _mode(leaf / "API_TOKEN") == 0o640
    assert [_mode(d) for d in (root, root / "user", leaf)] == [0o750] * 3
    assert _mode(root.parent) == 0o750
    assert list(leaf.iterdir()) == [leaf / "API_TOKEN"]


def test_an_existing_group_inheriting_root_keeps_its_bit(tmp_path: Path) -> None:
    """A setgid root passes its group to every directory the store creates."""
    root = tmp_path / "secrets"
    root.mkdir()
    root.chmod(0o2750)

    FileSecrets(root).put(_ref(), _VALUE)

    assert stat.S_IMODE((root / "org").stat().st_mode) == 0o2750
    assert stat.S_IMODE((root / "org" / "_").stat().st_mode) == 0o2750
    assert stat.S_IMODE(root.stat().st_mode) == 0o2750


def test_a_failed_write_leaves_no_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileSecrets(tmp_path / "secrets")
    store.put(_ref(), "old")

    def refuse(self: Path, target: Path) -> Path:
        del self, target
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(OSError, match="disk full"):
        store.put(_ref(), "new")
    monkeypatch.undo()

    assert store.get(_ref()) == "old"
    assert [p.name for p in (tmp_path / "secrets" / "org" / "_").iterdir()] == [
        "API_TOKEN",
    ]


def test_a_symlinked_directory_never_leads_outside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    (root / "org").symlink_to(outside)
    store = FileSecrets(root)

    for call in (
        lambda: store.put(_ref(), _VALUE),
        lambda: store.get(_ref()),
        lambda: store.delete(_ref()),
    ):
        with pytest.raises(ValueError, match="outside the secrets root"):
            call()

    assert list(outside.iterdir()) == []


def test_a_symlinked_root_is_followed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    store = FileSecrets(tmp_path / "link")

    store.put(_ref(), _VALUE)

    assert store.get(_ref()) == _VALUE
    assert (real / "org" / "_" / "API_TOKEN").read_text() == _VALUE


def test_a_symlinked_file_is_not_read(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    store = FileSecrets(root)
    store.put(_ref(), _VALUE)
    victim = tmp_path / "victim"
    victim.write_text("other data")
    leaf = root / "org" / "_" / "API_TOKEN"
    leaf.unlink()
    leaf.symlink_to(victim)

    with pytest.raises(OSError, match="symbolic links") as refused:
        store.get(_ref())
    store.put(_ref(), "replacement")

    assert refused.value.errno == errno.ELOOP
    assert victim.read_text() == "other data"
    assert store.get(_ref()) == "replacement"


@pytest.mark.parametrize("mode", [0o777, 0o755, 0o751, 0o2775])
def test_a_root_open_to_others_is_closed_on_first_write(
    tmp_path: Path,
    mode: int,
) -> None:
    root = tmp_path / "secrets"
    root.mkdir()
    root.chmod(mode)

    FileSecrets(root).put(_ref(), _VALUE)

    assert _mode(root) == mode & ~0o007


def test_an_owner_too_long_for_a_directory_name_is_refused(tmp_path: Path) -> None:
    store = FileSecrets(tmp_path / "secrets")
    longest = "x" * 200

    store.put(_ref(layer="machine", owner=longest), _VALUE)

    for owner in (longest + "x", "é" * 70):
        with pytest.raises(ValueError, match="secret owner"):
            store.put(_ref(layer="machine", owner=owner), _VALUE)
    assert store.get(_ref(layer="machine", owner=longest)) == _VALUE


def test_parse_file_uses_the_user_data_directory() -> None:
    backend = parse_secrets("file")
    assert isinstance(backend, FileSecrets)

    backend.put(_ref(), _VALUE)

    assert (data_dir() / "rekursiv-ai" / "trackinizer" / "secrets" / "org").is_dir()


def test_parse_file_with_a_path_uses_that_path(tmp_path: Path) -> None:
    backend = parse_secrets(f"file:{tmp_path / 'vault'}")
    assert isinstance(backend, FileSecrets)

    backend.put(_ref(), _VALUE)

    assert (tmp_path / "vault" / "org" / "_" / "API_TOKEN").read_text() == _VALUE


def test_parse_none_disables_the_store() -> None:
    assert parse_secrets("none") is None


@pytest.mark.parametrize("spec", ["", "vault", "file:", "file:relative/dir", "FILE"])
def test_parse_rejects_anything_else_naming_the_forms(spec: str) -> None:
    with pytest.raises(ConfigError, match=r"file.*file:/abs/path.*none"):
        parse_secrets(spec)


def test_parse_hands_the_rest_of_a_scheme_spec_to_its_factory(
    tmp_path: Path,
) -> None:
    made: list[str] = []
    backend = FileSecrets(tmp_path)

    def factory(rest: str) -> FileSecrets:
        made.append(rest)
        return backend

    assert parse_secrets("mem:a:b/c", schemes={"mem": factory}) is backend
    assert made == ["a:b/c"]


def test_a_scheme_never_shadows_the_built_in_forms(tmp_path: Path) -> None:
    def refuse(rest: str) -> FileSecrets:
        raise AssertionError(rest)

    backend = parse_secrets(f"file:{tmp_path}", schemes={"file": refuse})

    assert isinstance(backend, FileSecrets)
    assert parse_secrets("none", schemes={"none": refuse}) is None
    with pytest.raises(ConfigError, match="unknown secrets backend"):
        parse_secrets("file:relative/dir", schemes={"file": refuse})


@pytest.mark.parametrize("spec", ["mem", "other:x", "MEM:x", ":x"])
def test_parse_refuses_a_spec_whose_scheme_is_not_given_and_names_the_given(
    spec: str,
    tmp_path: Path,
) -> None:
    def mem(rest: str) -> FileSecrets:
        del rest
        return FileSecrets(tmp_path)

    schemes = {"mem": mem}

    with pytest.raises(ConfigError, match=r"'file'.*'mem:<value>'.*'none'"):
        parse_secrets(spec, schemes=schemes)


def test_parse_without_schemes_refuses_a_scheme_spec() -> None:
    with pytest.raises(ConfigError, match="unknown secrets backend 'mem:x'"):
        parse_secrets("mem:x")


@pytest.mark.parametrize(
    ("layer", "name", "message"),
    [
        ("../etc", "API_TOKEN", "secret layer"),
        ("org", "../x", "secret name"),
        ("org", "", "secret name"),
    ],
)
def test_validate_ref_refuses_a_bad_layer_or_name(
    layer: str,
    name: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_ref(_ref(name, layer=layer))


def _ref(
    name: str = "API_TOKEN",
    *,
    layer: str = "org",
    owner: str = "",
) -> SecretRef:
    return SecretRef(layer=cast("Layer", layer), owner=owner, name=name)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
