"""Vibedentify — Flask + Essentia music genre / BPM / key analyzer.

The application is assembled by the :func:`create_app` factory so that tests
(and any WSGI server) get a fresh, independently-configured instance.
"""

import os

from flask import Flask

from . import config  # noqa: F401 -- imported first so .env loads before db/routes read env
from .db import init_db
from .routes import bp

__all__ = ["create_app"]


def create_app():
    app = Flask(__name__)  # templates/ and static/ live inside this package
    # Guard against a giant upload exhausting memory (configurable).
    app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "512")) * 1024 * 1024
    init_db()  # create tables if the DB is new
    app.register_blueprint(bp)
    return app
