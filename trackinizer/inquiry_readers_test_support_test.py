"""The reader scan finds every spelling of a read and judges classifications."""

from __future__ import annotations

from typing import TYPE_CHECKING

import ast

from trackinizer.inquiry_readers_test_support import (
    calls_policy,
    misfiling,
    readers,
    unclassified,
)


if TYPE_CHECKING:
    from pathlib import Path

    from trackinizer.inquiry_readers_test_support import Class


def test_a_reader_is_caught_in_every_spelling(tmp_path: Path) -> None:
    (tmp_path / "plain.py").write_text(
        'SQL = "SELECT * FROM inquiries WHERE id = $1"\n',
    )
    (tmp_path / "joined.py").write_text(
        'SQL = "SELECT c.id FROM edges e JOIN change_log c ON c.id = e.from_id"\n',
    )
    (tmp_path / "composed.py").write_text('SQL = vetted_sql("SELECT * FROM ", table)\n')
    (tmp_path / "escaped.py").write_text('SQL = "SELECT *\\nFROM\\tinquiries"\n')
    (tmp_path / "unicode.py").write_text('SQL = "SELECT * FROM\\x20inquiries"\n')
    (tmp_path / "adjacent.py").write_text('SQL = ("SELECT * FROM " "inquiries")\n')
    (tmp_path / "field.py").write_text('SQL = f"SELECT * FROM {table} WHERE x"\n')
    (tmp_path / "continued.py").write_text('SQL = "SELECT * FROM \\\n"\n')
    (tmp_path / "unrelated.py").write_text('SQL = "SELECT 1 FROM users"\n')
    (tmp_path / "prose.py").write_text('"""Read rows FROM inquiries, in prose."""\n')
    (tmp_path / "reader_test.py").write_text('SQL = "SELECT * FROM inquiries"\n')
    (tmp_path / "reader_test_support.py").write_text(
        'SQL = "SELECT * FROM inquiries"\n',
    )
    (tmp_path / "conftest.py").write_text('SQL = "SELECT * FROM inquiries"\n')
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "copy.py").write_text('SQL = "SELECT * FROM inquiries"\n')
    (tmp_path / "skipped").mkdir()
    (tmp_path / "skipped" / "reader.py").write_text('SQL = "SELECT * FROM inquiries"\n')
    (tmp_path / "kept").mkdir()
    (tmp_path / "kept" / "reader.py").write_text('SQL = "SELECT * FROM change_log"\n')

    found = readers(tmp_path, skip=frozenset({"skipped"}))

    assert sorted(found) == [
        "adjacent.py",
        "composed.py",
        "continued.py",
        "escaped.py",
        "field.py",
        "joined.py",
        "kept/reader.py",
        "plain.py",
        "unicode.py",
    ]
    assert all(isinstance(tree, ast.Module) for tree in found.values())


def test_a_reader_names_the_policy_only_by_calling_its_filter() -> None:
    assert calls_policy(ast.parse("inquiry_read_filter(r, policy=p)\n"))
    assert calls_policy(ast.parse("store.change_read_filter('c', params=[])\n"))
    assert calls_policy(ast.parse("policy.read_filter('inquiry', 'i')\n"))
    assert not calls_policy(ast.parse('SQL = "SELECT * FROM inquiries"\n'))
    assert not calls_policy(ast.parse("read_filtered = 1\n"))


def test_only_an_unclassified_reader_is_named_unclassified(tmp_path: Path) -> None:
    for name, text in {
        "new.py": 'SQL = "SELECT * FROM inquiries"\n',
        "trusted.py": 'SQL = "SELECT * FROM change_log"\n',
        "prose.py": '"""Read rows FROM inquiries, in prose."""\n',
        "unrelated.py": 'SQL = "SELECT 1 FROM users"\n',
    }.items():
        (tmp_path / name).write_text(text)
    (tmp_path / "skipped").mkdir()
    (tmp_path / "skipped" / "reader.py").write_text('SQL = "SELECT * FROM inquiries"\n')
    classified: dict[str, tuple[Class, str]] = {"trusted.py": ("trusted", "worker")}
    skip = frozenset({"skipped"})

    assert unclassified(tmp_path, classified, skip=skip) == ("new.py",)
    assert unclassified(tmp_path, {}, skip=skip) == ("new.py", "trusted.py")
    assert unclassified(tmp_path, classified) == ("new.py", "skipped/reader.py")


def test_misfiling_names_stale_and_unwired_modules(tmp_path: Path) -> None:
    for name, text in {
        "wired.py": 'SQL = "SELECT * FROM inquiries"\ninquiry_read_filter(r)\n',
        "unwired.py": 'SQL = "SELECT * FROM inquiries"\n',
        "prose.py": '"""Read rows FROM inquiries, in prose."""\n',
    }.items():
        (tmp_path / name).write_text(text)

    assert misfiling(tmp_path / "wired.py", "policy") is None
    assert misfiling(tmp_path / "unwired.py", "trusted") is None
    assert misfiling(tmp_path / "unwired.py", "policy") == "unwired"
    assert misfiling(tmp_path / "prose.py", "trusted") == "stale"
    assert misfiling(tmp_path / "gone.py", "policy") == "stale"


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
