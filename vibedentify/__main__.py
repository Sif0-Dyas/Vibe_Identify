"""Development entry point:  python -m vibedentify

Serves real Essentia analysis on http://localhost:5005. Set FAKE_ANALYZER=1
for instant fake results with no models loaded. For production use a WSGI
server against ``wsgi:app`` instead of this dev server.
"""
import logging
import os

from . import create_app

log = logging.getLogger("vibedentify")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = create_app()
    if os.environ.get("FAKE_ANALYZER") == "1":
        log.info("FAKE_ANALYZER=1 -- serving fake results (GUI test mode, no Essentia).")
    log.info("Vibedentify running -> http://localhost:5005")
    app.run(host=os.environ.get("GENRE_HOST", "127.0.0.1"),
            port=5005, debug=False, threaded=True)


if __name__ == "__main__":
    main()
