"""Configuration & constants derived from environment variables.
This module has no project-internal imports -- it is the base of the graph.
"""

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("vibedentify")


def _apply_dotenv(path, env=None):
    """Load KEY=VALUE pairs from ``path`` into ``env`` (default os.environ) and
    return the names it set. Real environment variables always win -- only keys
    ABSENT from ``env`` are set. Tolerant parsing: blank lines and ``#`` comments
    are skipped, an optional ``export`` prefix and surrounding quotes on the value
    are accepted, malformed lines (no ``=`` / empty key) are ignored silently. A
    missing file is a silent no-op."""
    env = os.environ if env is None else env
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    applied = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, sep, val = line.partition("=")
        key = key.strip()
        if not sep or not key:  # malformed -> skip silently
            continue
        if key not in env:  # real environment always wins
            env[key] = val.strip().strip("\"'")
            applied[key] = env[key]
    return applied


def _load_dotenv():
    """Auto-load the project-root .env at startup (dependency-free) so the dev
    server picks up secrets/config without a manual ``source``. Skipped under
    pytest so a developer's local .env never leaks into the test run -- the parser
    itself is unit-tested directly via _apply_dotenv."""
    if "pytest" not in sys.modules:
        _apply_dotenv(Path(__file__).resolve().parent.parent / ".env")


_load_dotenv()  # must run BEFORE the env-derived constants below

MODEL_DIR = Path(os.environ.get("MODEL_DIR", Path.home() / "essentia_models"))
MODELS = {
    "discogs-effnet-bs64-1.pb": "https://essentia.upf.edu/models/feature-extractors/discogs-effnet/discogs-effnet-bs64-1.pb",
    "genre_discogs400-discogs-effnet-1.pb": "https://essentia.upf.edu/models/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.pb",
    "genre_discogs400-discogs-effnet-1.json": "https://essentia.upf.edu/models/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.json",
}
AUDIO_EXTS = {
    ".mp3",
    ".flac",
    ".m4a",
    ".mp4",
    ".aac",
    ".alac",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".aif",
    ".aiff",
    ".aifc",
    ".wma",
    ".wv",
    ".ape",
    ".mpc",
    ".dsf",
}
FAKE = os.environ.get("FAKE_ANALYZER") == "1"

# Optional external metadata-lookup API credentials (the 🔎 per-row lookup).
# Absent -> that source is simply skipped; the feature degrades per-source.
# Discogs auth: a personal access token OR a consumer key + secret (either works
# for /database/search); the token wins if both are set.
DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN", "").strip()
DISCOGS_KEY = os.environ.get("DISCOGS_KEY", "").strip()
DISCOGS_SECRET = os.environ.get("DISCOGS_SECRET", "").strip()
LASTFM_KEY = os.environ.get("LASTFM_KEY", "").strip()
