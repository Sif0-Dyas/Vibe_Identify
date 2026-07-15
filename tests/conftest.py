"""Shared pytest fixtures.

Every test runs with FAKE_ANALYZER=1 (no Essentia / no model loads) and a
throwaway SQLite database, so the suite never touches real models or the
user's ~/genre_v2.db. Config is read at import time, so we set the env vars
first and reload the package per test for full isolation.
"""
import os
import sys
import tempfile

import pytest


@pytest.fixture()
def client():
    os.environ["FAKE_ANALYZER"] = "1"
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["GENRE_DB"] = tmp.name

    for name in list(sys.modules):          # force a clean import per test
        if name == "vibedentify" or name.startswith("vibedentify."):
            del sys.modules[name]
    import vibedentify

    app = vibedentify.create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c

    os.unlink(tmp.name)
