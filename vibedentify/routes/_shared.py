"""Shared routing state: the Blueprint, optional loopback auth, and the
cross-domain helpers (used by both the map and library routes)."""

import hmac
import os
from pathlib import Path

from flask import Blueprint, abort, request

bp = Blueprint("main", __name__)


# --- optional loopback auth (used by the Windows desktop shell) --------------
# When GENRE_TOKEN is set, every request must (a) be addressed to a loopback Host
# -- which defeats DNS-rebinding, where a malicious site resolves its own domain
# to 127.0.0.1 to reach this server from a browser -- and (b) carry the secret,
# via the ?k= on the first navigation, thereafter via an httponly cookie. This
# keeps other local processes and browser-based attacks from driving the API.
# Unset (the default: the normal browser workflow and the tests) => no auth, so
# behaviour is completely unchanged.
_AUTH_TOKEN = os.environ.get("GENRE_TOKEN", "")
_TOKEN_COOKIE = "vibe_token"  # nosec B105  # cookie NAME (not a secret); the value is _AUTH_TOKEN
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


@bp.before_request
def _loopback_guard():
    if not _AUTH_TOKEN:
        return  # auth disabled -> unchanged behaviour
    host = (request.host or "").rsplit(":", 1)[0]
    if host not in _LOOPBACK_HOSTS:
        abort(403)  # not addressed to loopback -> likely DNS-rebinding
    supplied = request.cookies.get(_TOKEN_COOKIE) or request.args.get("k", "")
    if not hmac.compare_digest(supplied, _AUTH_TOKEN):
        abort(403)


@bp.after_request
def _promote_token_cookie(resp):
    # Turn a valid ?k= (the first navigation) into an httponly, same-site cookie
    # so later requests authenticate on their own, without the token in the URL.
    if _AUTH_TOKEN and hmac.compare_digest(request.args.get("k", ""), _AUTH_TOKEN):
        resp.set_cookie(_TOKEN_COOKIE, _AUTH_TOKEN, httponly=True, samesite="Strict", path="/")
    return resp


# ---------------------------------------------------------------------------
# Genre Map -- interactive constellation of the entire scanned library.
# /map returns every track + nearest-neighbour edges; /similar/<h> powers the
# popup. Both lean on the same 1280-d embeddings that drive vibes.
# ---------------------------------------------------------------------------
def _artist_of(payload, title, filename):
    """Best-effort artist: prefer the file's `artist` metadata tag (stored under
    payload['tags']['tag']); otherwise parse it out of an 'Artist - Title' name."""
    tag = ((payload or {}).get("tags") or {}).get("tag") or {}
    tagged = (tag.get("artist") or tag.get("albumartist") or "").strip()
    if tagged:
        return tagged
    base = (title or "") or (Path(filename).stem if filename else "")
    return base.split(" - ", 1)[0].strip() if " - " in base else ""


def _dominant_style(payload):
    """Salience winner (v2 identity), falling back to the top flat style.
    A manual override (set via POST /override) wins outright."""
    if payload.get("override"):
        return payload["override"], 1.0
    sal = payload.get("salience") or []
    if sal:
        return sal[0].get("style"), round(float(sal[0].get("score", 0)), 4)
    st = payload.get("styles") or []
    if st:
        return st[0].get("style"), round(float(st[0].get("score", 0)), 4)
    return None, 0.0


def _second_style(payload, top_style, top_score):
    """The runner-up style + its weight relative to the top read, for colour
    blending on the map (a track that's partly a 2nd genre leans toward its
    colour). Returns [style2, weight2] with weight2 in [0, 0.5], or None when
    there's an override or no distinct runner-up."""
    if payload.get("override"):
        return None
    ranked = payload.get("salience") or payload.get("styles") or []
    for s in ranked:
        st, sc = s.get("style"), float(s.get("score", 0) or 0)
        if st and st != top_style and sc > 0:
            denom = (top_score or 0) + sc
            return [st, round(sc / denom, 3) if denom else 0.0]
    return None
