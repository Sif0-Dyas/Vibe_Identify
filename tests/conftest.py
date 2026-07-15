"""Shared pytest fixtures.

Every test runs with FAKE_ANALYZER=1 (no Essentia / no model loads) and a
throwaway SQLite database, so the suite never touches real models or the user's
~/genre_v2.db. The app module reads GENRE_DB / FAKE_ANALYZER at import time, so
we set them *before* importing and re-import fresh per test for isolation.
"""
import importlib
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

    sys.modules.pop("genre_gui", None)          # force a clean import per test
    genre_gui = importlib.import_module("genre_gui")
    genre_gui.app.config.update(TESTING=True)

    with genre_gui.app.test_client() as c:
        yield c

    os.unlink(tmp.name)
