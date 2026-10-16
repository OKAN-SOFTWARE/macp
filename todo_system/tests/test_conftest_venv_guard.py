"""Unit tests for the ``installed_wheel`` venv-guard helpers in ``conftest.py``."""

from __future__ import annotations

from todo_system.tests.conftest import _manifest_diff, _venv_manifest


def test_manifest_diff_empty_when_unchanged(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").write_text("stub")

    before = _venv_manifest(tmp_path)
    after = _venv_manifest(tmp_path)

    assert _manifest_diff(before, after) == []


def test_manifest_diff_reports_added_and_modified_paths(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").write_text("stub")

    before = _venv_manifest(tmp_path)

    (tmp_path / "bin" / "python").write_text("changed")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "new_module.py").write_text("print('hi')")

    after = _venv_manifest(tmp_path)
    diff = _manifest_diff(before, after)

    assert diff == sorted(["bin/python", "lib/new_module.py"])


def test_manifest_diff_ignores_pycache_and_pyc(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").write_text("stub")

    before = _venv_manifest(tmp_path)

    pycache = tmp_path / "lib" / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "module.cpython-311.pyc").write_text("bytecode")
    (tmp_path / "lib" / "module.pyc").write_text("bytecode")

    after = _venv_manifest(tmp_path)

    assert _manifest_diff(before, after) == []
