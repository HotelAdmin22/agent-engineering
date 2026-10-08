"""Docker-marked tests run only when asked for: `uv run pytest -m docker`.

A plain `uv run pytest` stays fast and needs no Docker. `-m docker` needs a
running daemon and the sandbox image for the current tree (build_image).
"""

import pytest


def pytest_collection_modifyitems(config, items):
    markexpr = config.getoption("-m") or ""
    if "docker" in markexpr and "not docker" not in markexpr:
        return
    skip = pytest.mark.skip(reason="docker test: run with -m docker")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)
