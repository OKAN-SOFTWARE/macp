"""The conftest ``installed_wheel`` fixture builds the wheel once per pytest session.

The seven ``test_installed_wheel_smoke*`` modules alias it as ``installed``; this
module pins the count and the attribute set they rely on. It lives apart from
them so their collected test counts stay what they were before the fixture
moved into ``conftest.py``. Under any subset of the suite (``-k``, single
files) the count is still exactly one: requesting the fixture here builds it if
nothing earlier did.
"""

from __future__ import annotations


def test_wheel_is_built_once_per_session(installed_wheel, installed_wheel_builds):
    assert installed_wheel_builds == [installed_wheel.venv], installed_wheel_builds
    assert installed_wheel.python == installed_wheel.venv / "bin" / "python"
    assert installed_wheel.python.exists(), installed_wheel.python
    assert installed_wheel.wheel.suffix == ".whl", installed_wheel.wheel
    assert [i.filename for i in installed_wheel.infos] == installed_wheel.names
