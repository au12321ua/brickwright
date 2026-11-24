"""Shared fixtures for the brickwright test suite."""

import numpy as np
import pytest

from brickwright.config import Config
from brickwright.record import Record
from brickwright.tools import rendering

# ------- Creating records


@pytest.fixture
def config(tmp_path):
    """A Config rooted at an isolated temp records dir."""
    return Config(records_dir=tmp_path / "records")


@pytest.fixture
def record(config):
    """A freshly created record, seeded from the build template."""
    return Record.create(config, prompt="build a wall")


@pytest.fixture
def make_record(config):
    """Factory: create a record whose build.py is replaced by ``source``."""

    def _make(source):
        rec = Record.create(config, prompt="build a wall")
        rec.write_build(source)
        return rec

    return _make


@pytest.fixture
def png_bytes():
    """A tiny stand-in PNG payload for image-prompt tests."""
    return b"\x89PNG\r\n fake"


# ------- Faking resource-intensive steps


@pytest.fixture
def in_process(monkeypatch):
    """Run a record's build in-process instead of spawning a child process.

    Tests that are not about the subprocess boundary use this to skip that cost.
    Tests of the boundary must NOT use it: they need a real child process.
    """
    from brickwright.tools import build_execution

    monkeypatch.setattr(
        build_execution,
        "run_in_subprocess",
        lambda fn, args, timeout: fn(*args),
    )


@pytest.fixture
def subprocess_raises(monkeypatch):
    """Factory: make ``run_in_subprocess`` raise ``exc``.

    Lets a caller's error-wrapping be tested without spawning a real child.
    """
    from brickwright.tools import build_execution

    def _set(exc):
        def _raise(fn, args, timeout):
            raise exc

        monkeypatch.setattr(build_execution, "run_in_subprocess", _raise)

    return _set


@pytest.fixture
def fake_render_pipeline(monkeypatch):
    """Stub the heavy steps so render() runs without ldraw2mesh or a GPU."""
    monkeypatch.setattr(rendering, "_convert", lambda ldr, glb: None)
    monkeypatch.setattr(rendering, "_convert_connectivity", lambda ldr, glb: None)
    tile = np.zeros((4, 4, 4), dtype=np.uint8)
    monkeypatch.setattr(rendering, "_view_arrays", lambda glb, *, size=512: [tile] * 4)
    monkeypatch.setattr(
        rendering, "_angle_array", lambda glb, view_dir, up, *, size=512: tile
    )


# ------- Source code for a build.py file


_BUILD_TEMPLATE = """\
from bricklayer import PLATE, Color, LegoBuild, Position, brick

from brickwright import TestContext

{}

def run_tests():
    ctx = TestContext(create_build())
    ctx.check_no_floating()
    ctx.check_no_collisions()
    return ctx.report()
"""

_VALID = """\
def create_build():
    m = LegoBuild()
    m.add(brick(4, 2, color=Color.RED))
    return m
"""

_FLOATING = """\
def create_build():
    m = LegoBuild()
    m.add(brick(1, 1, color=Color.BLUE), position=Position(20, 0, 0))
    m.add(brick(4, 2, color=Color.RED), position=Position(0, 0, 1))
    return m
"""

_RAISES = """\
def create_build():
    raise RuntimeError("boom")
"""

_INFINITE = """\
def create_build():
    while True:
        pass
"""


@pytest.fixture
def valid_build():
    """Source for a build that is created and passes the builtin checks."""
    return _BUILD_TEMPLATE.format(_VALID)


@pytest.fixture
def floating_build():
    """Source for a build that is created but fails the no_floating check."""
    return _BUILD_TEMPLATE.format(_FLOATING)


@pytest.fixture
def raising_build():
    """Source whose create_build() raises RuntimeError("boom")."""
    return _BUILD_TEMPLATE.format(_RAISES)


@pytest.fixture
def infinite_build():
    """Source whose create_build() never returns."""
    return _BUILD_TEMPLATE.format(_INFINITE)
