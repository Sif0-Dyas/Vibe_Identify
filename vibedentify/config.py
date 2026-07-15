"""Configuration & constants derived from environment variables.
This module has no project-internal imports -- it is the base of the graph.
"""
import logging
import os
from pathlib import Path

log = logging.getLogger("vibedentify")

MODEL_DIR = Path(os.environ.get("MODEL_DIR", Path.home() / "essentia_models"))
MODELS = {
    "discogs-effnet-bs64-1.pb":
        "https://essentia.upf.edu/models/feature-extractors/discogs-effnet/discogs-effnet-bs64-1.pb",
    "genre_discogs400-discogs-effnet-1.pb":
        "https://essentia.upf.edu/models/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.pb",
    "genre_discogs400-discogs-effnet-1.json":
        "https://essentia.upf.edu/models/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.json",
}
AUDIO_EXTS = {
    ".mp3", ".flac", ".m4a", ".mp4", ".aac", ".alac", ".ogg", ".oga", ".opus",
    ".wav", ".aif", ".aiff", ".aifc", ".wma", ".wv", ".ape", ".mpc", ".dsf",
}
FAKE = os.environ.get("FAKE_ANALYZER") == "1"
